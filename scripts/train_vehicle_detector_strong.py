#!/usr/bin/env python3
"""Stronger, separately retained Faster R-CNN trial; never retunes on TRANSSET."""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
from torchvision.models.detection import fasterrcnn_resnet50_fpn_v2
from torchvision.models.detection.faster_rcnn import FastRCNNPredictor

import train_vehicle_detector as common

EPOCHS = [2, 5]


def build(classes, path):
    model = fasterrcnn_resnet50_fpn_v2(weights=None, weights_backbone=None, num_classes=classes,
                                      box_score_thresh=.001, box_detections_per_img=300)
    model.load_state_dict(torch.load(path, map_location="cpu", weights_only=True))
    return model


def run(output, pretrained):
    started = time.monotonic()
    if (output / "selection_frozen_before_reused_diagnostic.json").exists():
        raise FileExistsError("Refusing to overwrite frozen stronger trial")
    output.mkdir(exist_ok=True, parents=True)
    torch.set_num_threads(2)
    torch.manual_seed(common.SEED)
    torch.use_deterministic_algorithms(True)
    random.seed(common.SEED)
    if not common.digest(pretrained).startswith("dd69338a"):
        raise ValueError("Official stronger weight hash prefix differs")
    rows = common.load("visaitech_mixed_traffic")
    train = [row for row in rows if row["source_video_group"] in common.TRAIN_GROUPS]
    val = [row for row in rows if row["source_video_group"] in common.VALIDATION_GROUPS]
    if any(common.duplicates(a, b) for a in train for b in val):
        raise ValueError("Duplicate-linked video split")
    plan = {"generated_at": datetime.now(timezone.utc).isoformat(), "architecture": "fasterrcnn_resnet50_fpn_v2",
            "seed": common.SEED, "device": "cpu", "threads": 2, "epochs": EPOCHS,
            "thresholds": common.THRESHOLDS, "optimizer": "AdamW", "learning_rate": .0003, "weight_decay": .0001,
            "batch_size": 2, "train_groups": sorted(common.TRAIN_GROUPS), "validation_groups": sorted(common.VALIDATION_GROUPS),
            "train_images": len(train), "validation_images": len(val),
            "train_vehicle_boxes": sum(len(row["vehicle_boxes"]) for row in train),
            "validation_vehicle_boxes": sum(len(row["vehicle_boxes"]) for row in val),
            "image_resize": "Official default min_size800/max_size1333; service identical",
            "trainable": "ROI box classification and box regression predictor only; frozen pretrained backbone/RPN/ROI feature head and BatchNorm statistics",
            "generic_initialization": "Vehicle predictor = mean COCO bicycle/car/motorcycle/bus/truck weights; vehicle logit bias += log(5); pedestrian copied; background copied",
            "selection_rule": "lowest macro source-video validation count MAE then highest detection F1; compare pretrained plus epochs2/5 and previously selected SSDLite",
            "previous_sources_consumed": True, "TRANSSET_role": "Reused cross-source diagnostic only, never threshold/model selection",
            "adaptive_protocol_note": "Stronger architecture trial was added after seeing the small detector's poor reused TRANSSET diagnostic. It is an adaptive research trial, not a fresh prespecified blind benchmark.",
            "source_is_batam": False, "production_eligible": False,
            "pretrained_url": "https://download.pytorch.org/models/fasterrcnn_resnet50_fpn_v2_coco-dd69338a.pth",
            "pretrained_sha256": common.digest(pretrained)}
    common.save(output / "training_plan.json", plan)
    model = build(91, pretrained)
    outputs, timings = common.predict(model, val)
    candidate_outputs = {"fasterrcnn_pretrained_COCO": outputs}
    candidate_times = {"fasterrcnn_pretrained_COCO": timings}
    candidate_paths = {"fasterrcnn_pretrained_COCO": pretrained}
    candidate_classes = {"fasterrcnn_pretrained_COCO": common.COCO_VEHICLES}
    original = model.roi_heads.box_predictor
    replacement = FastRCNNPredictor(original.cls_score.in_features, 3)
    # Keep the generic vehicle head initialized close to the pretrained classes.
    with torch.no_grad():
        for destination, indexes in ((0, [0]), (1, list(common.COCO_VEHICLES)), (2, [1])):
            replacement.cls_score.weight[destination].copy_(original.cls_score.weight[indexes].mean(0))
            replacement.cls_score.bias[destination].copy_(original.cls_score.bias[indexes].mean(0))
            if destination == 1:
                replacement.cls_score.bias[destination].add_(math.log(5))
            weight = original.bbox_pred.weight.reshape(91, 4, -1)[indexes].mean(0)
            bias = original.bbox_pred.bias.reshape(91, 4)[indexes].mean(0)
            replacement.bbox_pred.weight.reshape(3, 4, -1)[destination].copy_(weight)
            replacement.bbox_pred.bias.reshape(3, 4)[destination].copy_(bias)
    model.roi_heads.box_predictor = replacement
    for parameter in model.parameters():
        parameter.requires_grad = False
    for parameter in model.roi_heads.box_predictor.parameters():
        parameter.requires_grad = True
    optimizer = torch.optim.AdamW(model.roi_heads.box_predictor.parameters(), lr=.0003, weight_decay=.0001)
    losses = []
    for epoch in range(1, max(EPOCHS) + 1):
        model.train()
        for layer in model.modules():
            if isinstance(layer, torch.nn.modules.batchnorm._BatchNorm):
                layer.eval()
        order = list(range(len(train)))
        random.shuffle(order)
        epoch_losses = []
        for start in range(0, len(order), 2):
            images, targets = [], []
            for index in order[start:start + 2]:
                row = train[index]
                image = row["tensor"].clone()
                boxes = torch.tensor(row["boxes"], dtype=torch.float32).reshape(-1, 4)
                if random.random() < .5:
                    image = image.flip(-1)
                    boxes[:, [0, 2]] = row["width"] - boxes[:, [2, 0]]
                images.append(image)
                targets.append({"boxes": boxes, "labels": torch.tensor(row["labels"], dtype=torch.int64)})
            optimizer.zero_grad(set_to_none=True)
            terms = model(images, targets)
            loss = terms["loss_classifier"] + terms["loss_box_reg"]
            if not torch.isfinite(loss):
                raise ValueError("Nonfinite stronger detector loss")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.roi_heads.box_predictor.parameters(), 10.)
            optimizer.step()
            epoch_losses.append(float(loss.detach()))
        losses.append({"epoch": epoch, "mean_roi_training_loss": float(np.mean(epoch_losses))})
        print(json.dumps(losses[-1]), flush=True)
        if epoch in EPOCHS:
            name = f"fasterrcnn_predictor_finetuned_epoch_{epoch}"
            path = output / f"{name}.pt"
            torch.save(model.state_dict(), path)
            candidate_outputs[name], candidate_times[name] = common.predict(model, val)
            candidate_paths[name] = path
            candidate_classes[name] = {1: "vehicle"}
    common.save(output / "training_losses.json", losses)
    # Prior small detector remains a legitimate predeclared validation candidate.
    previous = output.parent / "ssdlite_trial"
    old_selection = json.loads((previous / "selection_frozen_before_reused_diagnostic.json").read_text())
    old_model = common.model(old_selection["num_classes"], previous / "model.pt")
    old_name = "previous_" + old_selection["selected_model"]
    candidate_outputs[old_name], candidate_times[old_name] = common.predict(old_model, val)
    candidate_paths[old_name] = previous / "model.pt"
    candidate_classes[old_name] = {int(k): v for k, v in old_selection["vehicle_classes"].items()}
    grid = []
    for name, predictions in candidate_outputs.items():
        for threshold in common.THRESHOLDS:
            detections = common.detections(predictions, val, threshold, candidate_classes[name])
            grid.append({"candidate": name, "score_threshold": threshold, "count": common.count_metrics(detections, val),
                         "detection": common.detection_metrics(detections, val)})
    grid.sort(key=lambda row: (row["count"]["macro_source_group_mae"], -row["detection"]["f1"], -row["score_threshold"], row["candidate"]))
    selected = grid[0]
    name, threshold = selected["candidate"], selected["score_threshold"]
    shutil.copyfile(candidate_paths[name], output / "model.pt")
    architecture = "ssdlite320_mobilenet_v3_large" if name == old_name else "fasterrcnn_resnet50_fpn_v2"
    classes = old_selection["num_classes"] if name == old_name else 91 if name == "fasterrcnn_pretrained_COCO" else 3
    frozen = {"generated_at": datetime.now(timezone.utc).isoformat(), "architecture": architecture,
              "num_classes": classes, "selected_model": name, "vehicle_classes": candidate_classes[name],
              "score_threshold": threshold, "vehicle_nms_iou": .5, "model_sha256": common.digest(output / "model.pt"),
              "selection_metric": plan["selection_rule"], "validation": selected,
              "diagnostic_labels_read_before_freeze": False, "production_eligible": False, "batam_validated": False,
              "pretrained_weight_terms": "Official TorchVision warns of dataset-derived terms; no certified production clearance"}
    common.save(output / "validation_grid.json", grid)
    common.save(output / "selection_frozen_before_reused_diagnostic.json", frozen)
    freeze_hash = common.digest(output / "selection_frozen_before_reused_diagnostic.json")
    print(json.dumps({"selection_frozen": name, "threshold": threshold, "validation_mae": selected["count"]["mae_vehicles_per_frame"]}), flush=True)
    test_all = common.load("transset")
    excluded = [row["image"] for row in test_all if any(common.duplicates(row, earlier) for earlier in rows)]
    test = [row for row in test_all if row["image"] not in excluded]
    selected_model = common.model(classes, output / "model.pt") if name == old_name else build(classes, output / "model.pt")
    diagnostic_outputs, latency = common.predict(selected_model, test)
    test_metrics = common.evaluate(diagnostic_outputs, test, threshold, candidate_classes[name])
    validation_metrics = common.evaluate(candidate_outputs[name], val, threshold, candidate_classes[name])
    validation_pretrained = common.evaluate(candidate_outputs["fasterrcnn_pretrained_COCO"], val, .5, common.COCO_VEHICLES)
    base_mae = float(np.mean([abs(np.median([len(row["vehicle_boxes"]) for row in train]) - len(row["vehicle_boxes"])) for row in test]))
    old_report = json.loads((previous / "report.json").read_text())
    old_pixel_mae = old_report["baselines"]["prior_pixel_regression_test_mae"]
    report = {"schema": "crossflow.vehicle_detector_training.v2", "generated_at": datetime.now(timezone.utc).isoformat(),
              "training_performed": True, "training_updates": max(EPOCHS) * math.ceil(len(train) / 2),
              "previous_ssdlite_training_updates": old_report["training_updates"], "selected_model": name,
              "selected_model_is_finetuned": "finetuned" in name, "architecture": architecture, "score_threshold": threshold,
              "development_split": plan, "validation_selected_model": validation_metrics,
              "validation_pretrained_reference_threshold_05": validation_pretrained,
              "validation_constant_training_median_mae": float(np.mean([abs(np.median([len(row["vehicle_boxes"]) for row in train]) - len(row["vehicle_boxes"])) for row in val])),
              "reused_cross_source_diagnostic": test_metrics,
              "baselines": {"constant_training_median_test_mae": base_mae,
                            "prior_pixel_regression_test_mae": old_pixel_mae,
                            "previous_ssdlite_test_mae": old_report["reused_cross_source_diagnostic"]["count"]["mae_vehicles_per_frame"],
                            "improvement_pct_vs_prior_pixel_regressor": 100 * (1 - test_metrics["count"]["mae_vehicles_per_frame"] / old_pixel_mae),
                            "beats_training_constant_median": test_metrics["count"]["mae_vehicles_per_frame"] < base_mae},
              "cpu_latency_ms": {"threads": 2, "diagnostic_median": float(np.median(latency)), "diagnostic_p95": float(np.quantile(latency, .95)),
                                  "scope": "Single CPU model forward, excludes decode/load/queue/network"},
              "duplicate_audit": {"train_validation_duplicate_pairs": [], "cross_source_excluded_images": excluded,
                                  "within_diagnostic_pairs": [{"a": a["image"], "b": b["image"]} for i, a in enumerate(test) for b in test[i+1:] if common.duplicates(a,b)]},
              "production_eligible": False, "batam_validated": False, "google_comparison_performed": False,
              "limitations": old_report["limitations"], "runtime": old_report["runtime"], "elapsed_seconds": time.monotonic() - started}
    boxes = common.detections(diagnostic_outputs, test, threshold, candidate_classes[name])
    with (output / "diagnostic_predictions.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["image", "target_count", "predicted_count", "error"])
        writer.writeheader()
        for row, prediction in zip(test, boxes):
            target = len(row["vehicle_boxes"])
            writer.writerow({"image": row["image"], "target_count": target, "predicted_count": len(prediction), "error": len(prediction) - target})
    common.save(output / "diagnostic_detections.json", [{"image": row["image"], "detections": prediction} for row, prediction in zip(test, boxes)])
    counter = common.VehicleCounter(output, allow_experimental=True)
    parity = []
    for row, expected in list(zip(test, boxes))[:3]:
        result = counter.count_image((common.ROOT / row["image"]).read_bytes())
        equal = result["vehicle_count"] == len(expected) and all(
            max(abs(a-b) for a,b in zip(actual["box_xyxy"], reference["box_xyxy"])) <= 1e-4
            and abs(actual["score"] - reference["score"]) <= 1e-6 for actual, reference in zip(result["detections"], expected))
        if not equal:
            raise ValueError("Stronger detector service parity failed")
        parity.append({"image": row["image"], "passed": equal, "count": result["vehicle_count"], "latency_ms": result["latency_ms"]})
    if common.digest(output / "selection_frozen_before_reused_diagnostic.json") != freeze_hash:
        raise ValueError("Frozen model changed after reused diagnostic")
    common.save(output / "report.json", report)
    common.save(output / "qa.json", {"passed": True, "service_parity": parity, "selection_unchanged_after_diagnostic": True,
                                     "source_manifests_verified": True, "production_activated": False, "batam_accuracy_validated": False})
    common.save(output / "manifest.json", {"files": [{"path": str(p.relative_to(output)), "bytes": p.stat().st_size, "sha256": common.digest(p)} for p in sorted(output.rglob('*')) if p.is_file() and p.name != 'manifest.json'],
                                           "script_sha256": common.digest(__file__), "service_sha256": common.digest(common.REPO / 'backend/services/vehicle_counter.py')})
    print(json.dumps({"selected": name, "validation": validation_metrics["count"], "diagnostic": test_metrics["count"], "qa_passed": True}, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=common.OUTPUT / "fasterrcnn_trial")
    parser.add_argument("--pretrained", type=Path, default=common.OUTPUT / "fasterrcnn_trial/pretrained_coco.pt")
    args = parser.parse_args()
    run(args.output, args.pretrained)
