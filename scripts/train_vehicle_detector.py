#!/usr/bin/env python3
"""Bounded, reproducible offline detector-head training and reused diagnostics.

Never downloads data/weights or promotes a model. Parameters and source-video
split are fixed before running; TRANSSET is an already-consumed diagnostic.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import shutil
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
import torchvision
from PIL import Image, ImageDraw
from torchvision.models.detection import ssdlite320_mobilenet_v3_large
from torchvision.transforms.functional import to_tensor

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))
from services.vehicle_counter import COCO_VEHICLES, VehicleCounter, filter_detections

ROOT = REPO.parent / "research/batam-traffic/datasets/round4_open/vision"
OUTPUT = REPO / "backend/data/research/detector_training"
SEED = 20261024
THRESHOLDS = [.1, .2, .3, .4, .5, .6, .7]
EPOCHS = [4, 8, 12]
TRAIN_GROUPS = {"V001", "V003", "V004", "V005"}
VALIDATION_GROUPS = {"V002", "VEHICLES"}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def hashes(image):
    gray = image.convert("L")
    small = np.asarray(gray.resize((9, 8), Image.Resampling.BILINEAR))
    d = small[:, 1:] > small[:, :-1]
    square = np.asarray(gray.resize((32, 32), Image.Resampling.BILINEAR), dtype=float)
    basis = np.cos(np.pi / 32 * (np.arange(32)[None, :] + .5) * np.arange(8)[:, None])
    low = basis @ square @ basis.T
    p = low > np.median(low.ravel()[1:])
    p[0, 0] = False
    def packed(bits):
        value = 0
        for b in bits.ravel():
            value = value * 2 + int(b)
        return value
    return packed(d), packed(p)


def load(source):
    directory = ROOT / source
    inventory = json.loads((directory / "files_manifest.json").read_text())
    verified = {row["local_path"]: row["sha256"] for row in inventory["files"]}
    rows = []
    for line in (directory / "frame_counts.jsonl").read_text().splitlines():
        if not line:
            continue
        frame = json.loads(line)
        for kind in ("image", "label"):
            if digest(ROOT / frame[kind]) != verified[frame[kind]]:
                raise ValueError("Source bytes differ from retained manifest")
        with Image.open(ROOT / frame["image"]) as im:
            image = im.convert("RGB")
            if image.size != (frame["width"], frame["height"]):
                raise ValueError("Changed source dimensions")
            dh, ph = hashes(image)
            rgb_hash = hashlib.sha256(image.tobytes()).hexdigest()
            tensor = to_tensor(image)
        boxes, labels, vehicles = [], [], []
        for row in (ROOT / frame["label"]).read_text().splitlines():
            raw = list(map(float, row.split()))
            if len(raw) != 5 or int(raw[0]) != raw[0] or not all(math.isfinite(v) for v in raw):
                raise ValueError("Invalid author label")
            cls, x, y, w, h = raw
            if not 0 <= cls < (4 if source == "visaitech_mixed_traffic" else 8):
                raise ValueError("Unknown source class")
            if not all(0 <= v <= 1 for v in (x, y, w, h)) or min(w, h) <= 0:
                raise ValueError("Invalid author box")
            width, height = frame["width"], frame["height"]
            box = [max(0., (x - w / 2) * width), max(0., (y - h / 2) * height),
                   min(float(width), (x + w / 2) * width), min(float(height), (y + h / 2) * height)]
            if box[2] <= box[0] or box[3] <= box[1]:
                raise ValueError("Degenerate source box")
            is_vehicle = source != "visaitech_mixed_traffic" or cls != 0
            boxes.append(box)
            labels.append(1 if is_vehicle else 2)
            if is_vehicle:
                vehicles.append(box)
        if len(vehicles) != frame["vehicle_count_excluding_pedestrians"]:
            raise ValueError("Author count audit differs")
        rows.append({**frame, "source": source, "tensor": tensor, "boxes": boxes, "labels": labels,
                     "vehicle_boxes": vehicles, "image_sha256": verified[frame["image"]],
                     "label_sha256": verified[frame["label"]], "decoded_sha256": rgb_hash,
                     "dhash": f"{dh:016x}", "phash": f"{ph:016x}"})
    return rows


def duplicates(first, second):
    return (first["image_sha256"] == second["image_sha256"] or first["decoded_sha256"] == second["decoded_sha256"]
            or ((int(first["dhash"], 16) ^ int(second["dhash"], 16)).bit_count() <= 4
                and (int(first["phash"], 16) ^ int(second["phash"], 16)).bit_count() <= 4))


def model(classes, weights):
    result = ssdlite320_mobilenet_v3_large(weights=None, weights_backbone=None,
        num_classes=classes, score_thresh=.001, detections_per_img=300)
    result.load_state_dict(torch.load(weights, map_location="cpu", weights_only=True))
    return result


def binary_head(pretrained):
    result = ssdlite320_mobilenet_v3_large(weights=None, weights_backbone=None,
        num_classes=3, score_thresh=.001, detections_per_img=300)
    result.backbone.load_state_dict(pretrained.backbone.state_dict())
    result.head.regression_head.load_state_dict(pretrained.head.regression_head.state_dict())
    for old, new in zip(pretrained.head.classification_head.module_list, result.head.classification_head.module_list):
        new[0].load_state_dict(old[0].state_dict())
        anchors = new[1].out_channels // 3
        for suffix in ("weight", "bias"):
            original = getattr(old[1], suffix).detach().reshape(anchors, 91, *getattr(old[1], suffix).shape[1:])
            replacement = torch.stack([original[:, 0], original[:, list(COCO_VEHICLES)].mean(1), original[:, 1]], dim=1)
            getattr(new[1], suffix).data.copy_(replacement.reshape_as(getattr(new[1], suffix)))
    for parameter in result.backbone.parameters():
        parameter.requires_grad = False
    return result


def predict(detector, rows):
    detector.eval()
    outputs, durations = [], []
    with torch.inference_mode():
        for row in rows:
            started = time.monotonic()
            prediction = detector([row["tensor"]])[0]
            durations.append((time.monotonic() - started) * 1000)
            outputs.append({key: value.detach().cpu() for key, value in prediction.items()})
    return outputs, durations


def detections(outputs, rows, threshold, classes):
    return [filter_detections(output, width=row["width"], height=row["height"], threshold=threshold,
                              vehicle_classes=classes) for output, row in zip(outputs, rows)]


def overlap(first, second):
    a, b, c, d = first
    x, y, z, w = second
    intersection = max(0, min(c, z) - max(a, x)) * max(0, min(d, w) - max(b, y))
    union = (c - a) * (d - b) + (z - x) * (w - y) - intersection
    return intersection / union if union else 0


def detection_metrics(predicted, rows, iou=.5):
    ranked = sorted([(float(box["score"]), i, box["box_xyxy"]) for i, frame in enumerate(predicted) for box in frame], reverse=True)
    matched = [set() for _ in rows]
    tp, fp = [], []
    for _, index, box in ranked:
        choices = [(overlap(box, actual), k) for k, actual in enumerate(rows[index]["vehicle_boxes"]) if k not in matched[index]]
        best = max(choices, default=(0., -1))
        yes = best[0] >= iou
        if yes:
            matched[index].add(best[1])
        tp.append(int(yes))
        fp.append(int(not yes))
    total = sum(len(row["vehicle_boxes"]) for row in rows)
    cumulative_tp, cumulative_fp = np.cumsum(tp), np.cumsum(fp)
    recall = cumulative_tp / max(1, total)
    precision = cumulative_tp / np.maximum(1, cumulative_tp + cumulative_fp)
    ap = float(np.mean([max(precision[recall >= x], default=0) for x in np.linspace(0, 1, 101)]))
    true = int(sum(tp))
    false = int(sum(fp))
    p, r = true / max(1, true + false), true / max(1, total)
    return {"ap": ap, "precision": p, "recall": r, "f1": 2 * p * r / (p + r) if p + r else 0.,
            "true_positive": true, "false_positive": false, "false_negative": total - true,
            "ground_truth_boxes": total, "predicted_boxes": true + false}


def count_metrics(predicted, rows):
    actual = np.array([len(row["vehicle_boxes"]) for row in rows])
    values = np.array([len(frame) for frame in predicted])
    errors = values - actual
    groups = sorted({row["source_video_group"] for row in rows})
    macro = np.mean([np.abs(errors[[row["source_video_group"] == group for row in rows]]).mean() for group in groups])
    return {"frames": len(rows), "mae_vehicles_per_frame": float(np.abs(errors).mean()),
            "macro_source_group_mae": float(macro), "rmse_vehicles_per_frame": float(np.sqrt((errors ** 2).mean())),
            "bias_vehicles_per_frame": float(errors.mean()), "exact_count_pct": float(100 * np.mean(errors == 0)),
            "within_one_vehicle_pct": float(100 * np.mean(np.abs(errors) <= 1)),
            "actual_total": int(actual.sum()), "predicted_total": int(values.sum())}


def evaluate(outputs, rows, threshold, classes):
    selected = detections(outputs, rows, threshold, classes)
    low = detections(outputs, rows, .01, classes)
    aps = {f"{iou:.2f}": detection_metrics(low, rows, iou)["ap"] for iou in np.arange(.5, 1, .05)}
    return {"count": count_metrics(selected, rows), "detection_at_frozen_threshold_iou_50": detection_metrics(selected, rows),
            "vehicle_AP50": aps["0.50"], "vehicle_mAP50_95": float(np.mean(list(aps.values()))), "AP_by_iou": aps,
            "AP_protocol": "Single generic vehicle class, all sizes, max 100, score floor .01, 101 recall points; no crowd annotations; not the complete COCO benchmark"}


def run(output, pretrained_path):
    started = time.monotonic()
    if (output / "report.json").exists() or (output / "selection_frozen_before_reused_diagnostic.json").exists():
        raise FileExistsError("Refusing to overwrite frozen detector experiment")
    output.mkdir(parents=True, exist_ok=True)
    torch.manual_seed(SEED)
    np.random.seed(SEED)
    random.seed(SEED)
    torch.set_num_threads(2)
    torch.use_deterministic_algorithms(True)
    if not digest(pretrained_path).startswith("a79551df"):
        raise ValueError("Official pretrained weight hash prefix differs")
    source_manifest = json.loads((ROOT / "manifest.json").read_text())
    if any(row["data_license"]["spdx"] != "CC-BY-4.0" for row in source_manifest["datasets"]):
        raise ValueError("Expected original explicit image/annotation license evidence")
    # Deliberately load development source before any TRANSSET labels.
    development = load("visaitech_mixed_traffic")
    train = [row for row in development if row["source_video_group"] in TRAIN_GROUPS]
    val = [row for row in development if row["source_video_group"] in VALIDATION_GROUPS]
    cross_val = [{"train": a["image"], "validation": b["image"]} for a in train for b in val if duplicates(a, b)]
    if cross_val:
        raise ValueError("Duplicate-linked training/validation groups require a new predeclared split")
    plan = {"schema": "crossflow.vehicle_detector_plan.v1", "generated_at": datetime.now(timezone.utc).isoformat(),
            "architecture": "ssdlite320_mobilenet_v3_large", "seed": SEED, "device": "cpu", "threads": 2,
            "pretrained_sha256": digest(pretrained_path), "train_groups": sorted(TRAIN_GROUPS),
            "validation_groups": sorted(VALIDATION_GROUPS), "train_images": len(train), "validation_images": len(val),
            "train_vehicle_boxes": sum(len(row["vehicle_boxes"]) for row in train),
            "validation_vehicle_boxes": sum(len(row["vehicle_boxes"]) for row in val),
            "epochs_to_compare": EPOCHS, "threshold_grid": THRESHOLDS, "batch_size": 4,
            "optimizer": "AdamW", "learning_rate": .001, "weight_decay": .0001,
            "trainable": "classification and regression detection heads; backbone and all BatchNorm statistics frozen",
            "augmentation": "deterministic seeded random horizontal flip on training only",
            "selection": "lowest macro source-video count MAE, then highest detection F1 at IoU .5; candidate/threshold fixed before reused TRANSSET",
            "previous_consumption": "Both local datasets were used in an earlier pixel-count experiment. This is not a fresh blind holdout or Batam benchmark.",
            "geography_is_batam": False, "production_eligible": False}
    save(output / "training_plan.json", plan)
    save(output / "development_frame_manifest.json", [{k: v for k, v in row.items() if k not in {"tensor", "boxes", "labels", "vehicle_boxes"}} for row in development])
    pretrained = model(91, pretrained_path)
    candidate_outputs = {}
    candidate_classes = {}
    candidate_paths = {}
    outputs, timing = predict(pretrained, val)
    candidate_outputs["pretrained_COCO"] = outputs
    candidate_classes["pretrained_COCO"] = COCO_VEHICLES
    candidate_paths["pretrained_COCO"] = pretrained_path
    detector = binary_head(pretrained)
    optimizer = torch.optim.AdamW([p for p in detector.parameters() if p.requires_grad], lr=.001, weight_decay=.0001)
    losses, candidate_latency = [], {"pretrained_COCO": timing}
    for epoch in range(1, max(EPOCHS) + 1):
        detector.train()
        detector.backbone.eval()
        for layer in detector.modules():
            if isinstance(layer, torch.nn.modules.batchnorm._BatchNorm):
                layer.eval()
        order = list(range(len(train)))
        random.shuffle(order)
        epoch_losses = []
        for start in range(0, len(order), 4):
            images, targets = [], []
            for index in order[start:start + 4]:
                row = train[index]
                image = row["tensor"].clone()
                boxes = torch.tensor(row["boxes"], dtype=torch.float32).reshape(-1, 4)
                if random.random() < .5:
                    image = image.flip(-1)
                    boxes[:, [0, 2]] = row["width"] - boxes[:, [2, 0]]
                images.append(image)
                targets.append({"boxes": boxes, "labels": torch.tensor(row["labels"], dtype=torch.int64)})
            optimizer.zero_grad(set_to_none=True)
            terms = detector(images, targets)
            loss = sum(terms.values())
            if not torch.isfinite(loss):
                raise ValueError("Nonfinite detector loss")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(detector.head.parameters(), 10.)
            optimizer.step()
            epoch_losses.append(float(loss.detach()))
        losses.append({"epoch": epoch, "mean_training_loss": float(np.mean(epoch_losses))})
        print(json.dumps(losses[-1]), flush=True)
        if epoch in EPOCHS:
            name = f"head_finetuned_epoch_{epoch}"
            path = output / f"{name}.pt"
            torch.save(detector.state_dict(), path)
            candidate_outputs[name], candidate_latency[name] = predict(detector, val)
            candidate_classes[name] = {1: "vehicle"}
            candidate_paths[name] = path
    save(output / "training_losses.json", losses)
    grid = []
    for name, outputs in candidate_outputs.items():
        for threshold in THRESHOLDS:
            boxes = detections(outputs, val, threshold, candidate_classes[name])
            grid.append({"candidate": name, "score_threshold": threshold,
                         "count": count_metrics(boxes, val), "detection": detection_metrics(boxes, val)})
    grid.sort(key=lambda row: (row["count"]["macro_source_group_mae"], -row["detection"]["f1"], -row["score_threshold"], row["candidate"]))
    selected = grid[0]
    name = selected["candidate"]
    shutil.copyfile(candidate_paths[name], output / "model.pt")
    frozen = {"generated_at": datetime.now(timezone.utc).isoformat(), "selected_model": name,
              "architecture": "ssdlite320_mobilenet_v3_large", "num_classes": 91 if name == "pretrained_COCO" else 3,
              "vehicle_classes": candidate_classes[name], "score_threshold": selected["score_threshold"],
              "vehicle_nms_iou": .5, "model_sha256": digest(output / "model.pt"),
              "selection_metric": plan["selection"], "validation": selected,
              "production_eligible": False, "batam_validated": False, "diagnostic_labels_read_before_freeze": False,
              "pretrained_weight_terms": "TorchVision warns of data-derived terms; COCO original image rights vary. No certified production clearance."}
    save(output / "validation_grid.json", grid)
    save(output / "selection_frozen_before_reused_diagnostic.json", frozen)
    freeze_hash = digest(output / "selection_frozen_before_reused_diagnostic.json")
    # Reused diagnostic starts only after frozen model/threshold files exist.
    test_all = load("transset")
    excluded = [row["image"] for row in test_all if any(duplicates(row, prior) for prior in development)]
    test = [row for row in test_all if row["image"] not in excluded]
    within_test = [{"a": a["image"], "b": b["image"]} for i, a in enumerate(test) for b in test[i + 1:] if duplicates(a, b)]
    selected_detector = model(frozen["num_classes"], output / "model.pt")
    test_outputs, test_latency = predict(selected_detector, test)
    test_metrics = evaluate(test_outputs, test, selected["score_threshold"], candidate_classes[name])
    val_metrics = evaluate(candidate_outputs[name], val, selected["score_threshold"], candidate_classes[name])
    pretrain_metrics = evaluate(candidate_outputs["pretrained_COCO"], val, .5, COCO_VEHICLES)
    constant = float(np.median([len(row["vehicle_boxes"]) for row in train]))
    baseline_mae = float(np.mean([abs(constant - len(row["vehicle_boxes"])) for row in test]))
    prior_path = REPO / "backend/data/research/vision_training/report.json"
    prior = json.loads(prior_path.read_text())
    reused_mae = test_metrics["count"]["mae_vehicles_per_frame"]
    report = {"schema": "crossflow.vehicle_detector_training.v1", "generated_at": datetime.now(timezone.utc).isoformat(),
              "training_performed": True, "training_updates": max(EPOCHS) * math.ceil(len(train) / 4),
              "architecture": frozen["architecture"], "selected_model": name,
              "selected_model_is_finetuned": name != "pretrained_COCO", "score_threshold": selected["score_threshold"],
              "development_split": plan, "validation_selected_model": val_metrics,
              "validation_pretrained_reference_threshold_05": pretrain_metrics,
              "reused_cross_source_diagnostic": test_metrics,
              "baselines": {"constant_training_median": constant, "constant_training_median_test_mae": baseline_mae,
                            "prior_pixel_regression_test_mae": prior["test_metrics"]["mae_vehicles_per_frame"],
                            "prior_pixel_report_sha256": digest(prior_path),
                            "improvement_pct_vs_prior_pixel_regressor": 100 * (1 - reused_mae / prior["test_metrics"]["mae_vehicles_per_frame"]),
                            "beats_training_constant_median": reused_mae < baseline_mae},
              "cpu_latency_ms": {"threads": 2, "validation_median": float(np.median(candidate_latency[name])),
                                  "diagnostic_median": float(np.median(test_latency)), "diagnostic_p95": float(np.quantile(test_latency, .95)),
                                  "scope": "Single image model forward CPU; excludes decode, initialization, network, service queue"},
              "duplicate_audit": {"train_validation_duplicate_pairs": cross_val,
                                  "cross_source_excluded_images": excluded, "within_diagnostic_pairs": within_test,
                                  "rule": "Exact bytes/decoded RGB or both 64-bit dHash and pHash Hamming <=4; imperfect duplicate screen"},
              "production_eligible": False, "batam_validated": False, "google_comparison_performed": False,
              "limitations": ["No Batam ground-truth image labels; transfer diagnostics cannot establish app deployment accuracy.",
                  "Only six source videos; validation/diagnostic sources were consumed in the prior experiment.",
                  "Taxonomy merges all vehicles; per-frame visible boxes do not measure queue, flow, speed, or unique tracks.",
                  "COCO weights and fine-tune images have distinct data-derived terms; production licensing not certified.",
                  "No 99-100% accuracy claim or Google comparison is supported."],
              "runtime": {"torch": torch.__version__, "torchvision": torchvision.__version__, "numpy": np.__version__, "python": sys.version},
              "elapsed_seconds": time.monotonic() - started}
    selected_test_boxes = detections(test_outputs, test, selected["score_threshold"], candidate_classes[name])
    with (output / "diagnostic_predictions.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["image", "source_video_group", "target_count", "predicted_count", "error"])
        writer.writeheader()
        for row, boxes in zip(test, selected_test_boxes):
            actual = len(row["vehicle_boxes"])
            writer.writerow({"image": row["image"], "source_video_group": row["source_video_group"], "target_count": actual,
                             "predicted_count": len(boxes), "error": len(boxes) - actual})
    save(output / "diagnostic_detections.json", [{"image": row["image"], "detections": boxes} for row, boxes in zip(test, selected_test_boxes)])
    save(output / "diagnostic_frame_manifest.json", [{k: v for k, v in row.items() if k not in {"tensor", "boxes", "labels", "vehicle_boxes"}} for row in test])
    # End-to-end service parity includes image decode and local hash-checked load.
    counter = VehicleCounter(output, allow_experimental=True)
    parity = []
    for row, expected in list(zip(test, selected_test_boxes))[:3]:
        result = counter.count_image((ROOT / row["image"]).read_bytes())
        equal = result["vehicle_count"] == len(expected) and all(
            max(abs(a - b) for a, b in zip(actual["box_xyxy"], reference["box_xyxy"])) <= 1e-4
            and abs(actual["score"] - reference["score"]) <= 1e-6
            for actual, reference in zip(result["detections"], expected))
        if not equal:
            raise ValueError("Service / offline detector parity failed")
        parity.append({"image": row["image"], "passed": equal, "count": result["vehicle_count"],
                       "service_latency_ms": result["latency_ms_including_first_load"]})
    if digest(output / "selection_frozen_before_reused_diagnostic.json") != freeze_hash:
        raise ValueError("Frozen selection changed after reused diagnostic")
    save(output / "report.json", report)
    save(output / "qa.json", {"passed": True, "source_manifests_verified": True, "selection_unchanged_after_diagnostic": True,
                              "service_parity": parity, "all_losses_finite": True, "batam_accuracy_validated": False,
                              "production_activated": False})
    # Sample overlays show predictions (red) and retained author boxes (green).
    for row, predicted in list(zip(test, selected_test_boxes))[:2]:
        with Image.open(ROOT / row["image"]) as original:
            image = original.convert("RGB")
        draw = ImageDraw.Draw(image)
        for box in row["vehicle_boxes"]:
            draw.rectangle(box, outline="lime", width=2)
        for box in predicted:
            draw.rectangle(box["box_xyxy"], outline="red", width=2)
        image.thumbnail((800, 800))
        image.save(output / f"diagnostic_overlay_{Path(row['image']).stem}.jpg", quality=88)
    evidence = [{"path": str(path.relative_to(output)), "bytes": path.stat().st_size, "sha256": digest(path)}
                for path in sorted(output.rglob("*")) if path.is_file() and path.name != "manifest.json"]
    save(output / "manifest.json", {"schema": "crossflow.vehicle_detector_artifacts.v1", "files": evidence,
                                   "source_manifest_sha256": digest(ROOT / "manifest.json"),
                                   "model_sha256": frozen["model_sha256"], "script_sha256": digest(__file__),
                                   "service_sha256": digest(REPO / "backend/services/vehicle_counter.py"),
                                   "production_eligible": False, "batam_validated": False})
    print(json.dumps({"selected": name, "validation": val_metrics["count"], "diagnostic": test_metrics["count"],
                      "qa_passed": True, "elapsed_seconds": report["elapsed_seconds"]}, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--pretrained", type=Path, default=OUTPUT / "pretrained_coco.pt")
    arguments = parser.parse_args()
    run(arguments.output, arguments.pretrained)
