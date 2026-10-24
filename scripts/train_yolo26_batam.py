#!/usr/bin/env python3
"""Bounded YOLO26 transfer-learning experiment on the frozen Batam photo pilot.

No downloader, no annotation generation, no stream collection, no production
promotion. The four Batam test photos were consumed by an earlier experiment;
this run reports them only as reused diagnostics after validation selection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import sys
import time
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))
from services.yolo26_counter import COCO_VEHICLES, MIN_LONG_SIDE, MIN_SHORT_SIDE, SELECTION_FILE, postprocess_detections
from train_batam_vehicle_detector import (checked_path, digest, load_annotations, load_protocol,
                                          metrics, select_validation, verify_unchanged)

SOURCE = REPO.parent / "research/batam-traffic/datasets/round10_batam_vehicle_images"
OUTPUT = REPO / "backend/data/research/yolo26_batam_training"
PRETRAINED_SHA256 = "9b09cc8bf347f0fc8a5f7657480587f25db09b34bf33b0652110fb03a8ad4fef"
THRESHOLDS = (.25, .4, .55)
SEED = 20261024


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def verify_pixels(protocol, row):
    from PIL import Image, ImageOps
    path = checked_path(protocol["root"], row["path"])
    if digest(path) != row["image_sha256"]:
        raise ValueError("Frozen image bytes changed")
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        if image.size != (row["width"], row["height"]) or hashlib.sha256(image.tobytes()).hexdigest() != row["decoded_sha256"]:
            raise ValueError("Frozen image pixels changed")
        return image.copy()


def prepare_development_dataset(protocol, rows_by_split, output):
    """Only train/validation files enter Ultralytics' training dataset."""
    root = output / "development_dataset"
    if set(rows_by_split) != {"train", "validation"}:
        raise ValueError("Training preparation accepts development partitions only")
    receipts = []
    for partition, rows in rows_by_split.items():
        for sub in ("images", "labels"):
            (root / sub / partition).mkdir(parents=True, exist_ok=True)
        for index, row in enumerate(rows):
            verify_pixels(protocol, row)
            stem = f"image_{index:03d}"
            destination = root / "images" / partition / (stem + ".jpg")
            # Canonical source images are JPEGs; copy bytes rather than re-encode.
            shutil.copyfile(checked_path(protocol["root"], row["path"]), destination)
            lines = []
            for x1, y1, x2, y2 in row["vehicle_boxes"]:
                lines.append(f"0 {(x1+x2)/(2*row['width']):.10f} {(y1+y2)/(2*row['height']):.10f} {(x2-x1)/row['width']:.10f} {(y2-y1)/row['height']:.10f}")
            label = root / "labels" / partition / (stem + ".txt")
            label.write_text("\n".join(lines) + ("\n" if lines else ""))
            receipts.append({"image_id": row["image_id"], "source_group": row["source_group"], "split": partition,
                             "license": row["license"], "license_url": row.get("license_url"), "source_url": row["source_url"],
                             "image_path": str(destination.relative_to(output)), "image_sha256": digest(destination),
                             "label_path": str(label.relative_to(output)), "label_sha256": digest(label), "vehicle_count": len(lines)})
    dataset = root / "dataset.yaml"
    dataset.write_text(f"path: {json.dumps(str(root.resolve()))}\ntrain: images/train\nval: images/validation\nnames:\n  0: vehicle\n")
    save(output / "development_dataset_receipt.json", {"derived_exports_not_new_observations": True, "images": receipts,
                                                       "dataset_sha256": digest(dataset), "test_exported": False})
    return dataset


def predict_rows(model, protocol, rows, classes, *, imgsz=640):
    predicted, latencies = [], []
    for row in rows:
        image = verify_pixels(protocol, row)
        started = time.monotonic()
        result = model.predict(source=image, imgsz=imgsz, conf=min(THRESHOLDS), iou=.5, classes=list(classes),
                               max_det=300, agnostic_nms=True, device="cpu", verbose=False,
                               save=False, save_txt=False, save_crop=False, stream=False)[0]
        latencies.append((time.monotonic() - started) * 1000)
        boxes = result.boxes
        predicted.append({"boxes": boxes.xyxy.cpu().tolist(), "scores": boxes.conf.cpu().tolist(), "classes": boxes.cls.cpu().tolist()})
    return predicted, latencies


def filtered(raw, rows, classes, threshold):
    return [postprocess_detections(item["boxes"], item["scores"], item["classes"], width=row["width"], height=row["height"],
                                  classes=classes, threshold=threshold) for item, row in zip(raw, rows)]


def freeze_selection(protocol, output, chosen, candidate_path, num_classes, *, architecture="yolo26n", imgsz=640):
    verify_unchanged(protocol)
    shutil.copyfile(candidate_path, output / "model.pt")
    sha = digest(output / "model.pt")
    selection = {"schema": "crossflow.yolo26_vehicle_selection.v1", "generated_at": datetime.now(timezone.utc).isoformat(),
        "architecture": architecture, "ultralytics_version": "8.4.184", "selected_model": chosen["candidate"],
        "model_id": architecture + "-batam-" + sha[:16] + "-" + str(imgsz), "model_sha256": sha, "score_threshold": chosen["score_threshold"],
        "imgsz": imgsz, "num_classes": num_classes,
        "vehicle_classes": {str(k): v for k, v in (COCO_VEHICLES if num_classes == 80 else {0: "vehicle"}).items()},
        "min_vehicle_long_side_px": MIN_LONG_SIDE, "min_vehicle_short_side_px": MIN_SHORT_SIDE,
        "generic_vehicle_nms_iou": .5, "validation": chosen, "selection_rule": "Validation macro source-group count MAE, detection IoU50 F1, higher threshold, lexical candidate name",
        "split_sha256": protocol["split_sha256"], "annotation_manifest_sha256": protocol["annotation_manifest_sha256"],
        "training_plan_sha256": digest(output / "training_plan.json"), "validation_grid_sha256": digest(output / "validation_grid.json"),
        "test_labels_read_before_selection_this_run": False, "test_previously_consumed": True, "fresh_test": False,
        "license": "AGPL-3.0", "license_url": "https://www.ultralytics.com/license", "production_eligible": False,
        "batam_validated": False, "human_certified": False, "annotation_origin": "ai_visual_blind"}
    save(output / SELECTION_FILE, selection)
    return selection


def load_reused_test(protocol, output):
    """The previous test remains separate and opens only after the new seal."""
    verify_unchanged(protocol)
    seal = json.loads((output / SELECTION_FILE).read_bytes())
    if (seal.get("schema") != "crossflow.yolo26_vehicle_selection.v1" or seal.get("split_sha256") != protocol["split_sha256"]
            or seal.get("annotation_manifest_sha256") != protocol["annotation_manifest_sha256"]
            or seal.get("test_labels_read_before_selection_this_run") is not False
            or seal.get("test_previously_consumed") is not True or seal.get("fresh_test") is not False
            or seal.get("production_eligible") is not False or seal.get("human_certified") is not False
            or seal.get("score_threshold") not in THRESHOLDS
            or digest(output / "training_plan.json") != seal.get("training_plan_sha256")
            or digest(output / "model.pt") != seal.get("model_sha256")
            or digest(output / "validation_grid.json") != seal.get("validation_grid_sha256")):
        raise ValueError("YOLO26 validation seal does not match the committed experiment")
    grid = json.loads((output / "validation_grid.json").read_bytes())
    if seal.get("validation") != select_validation(grid) or seal.get("selected_model") != seal["validation"]["candidate"] or seal.get("score_threshold") != seal["validation"]["score_threshold"]:
        raise ValueError("Frozen YOLO26 selection differs from validation-only decision")
    expected = protocol["annotation_manifest"]["files"]["test"]
    path = checked_path(protocol["root"], expected["path"])
    if digest(path) != expected["sha256"]:
        raise ValueError("Previously frozen test annotations changed")
    annotations = json.loads(path.read_bytes())
    items = annotations.get("images") if isinstance(annotations, dict) else annotations
    metadata = {row["image_id"]: row for row in protocol["images"] if row["split"] == "test"}
    result, seen = [], set()
    if not isinstance(items, list):
        raise ValueError("Invalid test annotations")
    for item in items:
        if not isinstance(item, dict) or item.get("image_id") not in metadata or item["image_id"] in seen:
            raise ValueError("Test annotation identity mismatch")
        seen.add(item["image_id"])
        if item.get("annotation_origin", annotations.get("annotation_origin")) != "ai_visual_blind" or item.get("human_certified", annotations.get("human_certified")) is not False:
            raise ValueError("Test annotation provenance changed")
        if item.get("decision") == "exclude":
            if not item.get("exclusion_reason"):
                raise ValueError("Test exclusion has no frozen reason")
            continue
        if item.get("decision") != "include" or item.get("unresolved_above_threshold_ambiguity", False):
            raise ValueError("Test qualification changed")
        row = metadata[item["image_id"]]
        boxes = item.get("boxes_xyxy")
        if not isinstance(boxes, list) or len(boxes) > 100:
            raise ValueError("Invalid test boxes")
        pixel_boxes = []
        for box in boxes:
            if (not isinstance(box, list) or len(box) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in box)
                    or box[0] >= box[2] or box[1] >= box[3]):
                raise ValueError("Invalid test box coordinates")
            pixels = [v * (row["width"] if index % 2 == 0 else row["height"]) for index, v in enumerate(box)]
            width, height = pixels[2] - pixels[0], pixels[3] - pixels[1]
            if min(width, height) < MIN_SHORT_SIDE - 1e-6 or max(width, height) < MIN_LONG_SIDE - 1e-6:
                raise ValueError("Test box differs from the native pixel policy")
            pixel_boxes.append(pixels)
        result.append({**row, "vehicle_boxes": pixel_boxes})
    if seen != set(metadata) or not result:
        raise ValueError("Test annotations do not cover the unchanged partition")
    return result


def complete_reused_diagnostic(protocol, output, elapsed_seconds):
    """Resume reporting only; frozen model/threshold and training are untouched."""
    import torch
    import ultralytics
    from ultralytics import YOLO
    selection = json.loads((output / SELECTION_FILE).read_bytes())
    plan = json.loads((output / "training_plan.json").read_bytes())
    test = load_reused_test(protocol, output)
    selected = YOLO(str(output / "model.pt"), task="detect")
    classes = {int(k): v for k, v in selection["vehicle_classes"].items()}
    raw_test, test_latency = predict_rows(selected, protocol, test, classes, imgsz=selection["imgsz"])
    predictions = filtered(raw_test, test, classes, selection["score_threshold"])
    result = metrics(predictions, test)
    save(output / "reused_batam_test_predictions.json", {"fresh_test": False, "test_previously_consumed": True,
        "predictions": [{"image_id": row["image_id"], "source_group": row["source_group"], "actual_count": len(row["vehicle_boxes"]),
                         "predicted_count": len(boxes), "detections": boxes, "latency_ms": latency}
                        for row, boxes, latency in zip(test, predictions, test_latency)]})
    runtime = {"python": sys.version, "ultralytics": ultralytics.__version__, "torch": torch.__version__, "device": "cpu"}
    report = {"schema": "crossflow.yolo26_batam_report.v1", "generated_at": datetime.now(timezone.utc).isoformat(),
        "architecture": selection["architecture"], "training": plan, "selection": selection,
        "reused_batam_test": result, "test_is_fresh_independent_validation": False,
        "test_not_used_for_model_selection": True, "elapsed_seconds": elapsed_seconds,
        "test_inference_latency_ms_median": statistics.median(test_latency), "runtime": runtime,
        "production_eligible": False, "batam_validated": False, "human_certified": False,
        "limitations": ["Only nine qualified Batam training photos and three validation photos",
            "Assistant visual labels are not human-certified accuracy ground truth",
            "Four test photos were previously consumed; this is a reused diagnostic",
            "Historical still photos are not validated current CCTV, motion, congestion or speed observations",
            "No 99-100% or Google superiority conclusion is possible", "AGPL-3.0 obligations apply to derivative distribution"]}
    save(output / "report.json", report)
    files = [{"path": str(path.relative_to(output)), "bytes": path.stat().st_size, "sha256": digest(path)}
             for path in sorted(output.rglob("*")) if path.is_file() and path.name != "manifest.json" and path.suffix not in (".log", ".cache") and path.name != "verification.json" and "runtime_config" not in path.parts]
    save(output / "manifest.json", {"schema": "crossflow.yolo26_artifacts.v1", "files": files, "production_eligible": False})
    print(json.dumps({"selected_model": selection["selected_model"], "validation": selection["validation"]["metrics"], "reused_test": result,
                      "elapsed_seconds": report["elapsed_seconds"], "production_eligible": False}, indent=2), flush=True)



def run_refinement(source, output, epochs):
    """Second, predeclared validation-only scale/resolution and optimizer audit."""
    if any((output / name).exists() for name in ("training_plan.json", SELECTION_FILE, "report.json")):
        raise ValueError("Refusing to overwrite the refinement experiment")
    previous = REPO / "backend/data/research/yolo26_batam_training"
    checkpoints = {
        "n": (previous / "pretrained_yolo26n.pt", PRETRAINED_SHA256),
        "s": (output / "pretrained_yolo26s.pt", "646f8bc3fe0a656803d95c294f7852321748cb29d13466a1af8862e2db384a1b"),
        "m": (output / "pretrained_yolo26m.pt", "401cea9ab23ad19246ff7744859816bc599f350e93c9dd30367b6f0a0745d0b7")}
    for path, sha in checkpoints.values():
        if digest(path) != sha:
            raise ValueError("Official refinement checkpoint hash mismatch")
    protocol = load_protocol(source / "split_v2.json", source / "annotation_manifest.json")
    train, validation = load_annotations(protocol, "train"), load_annotations(protocol, "validation")
    layouts = [("n", 640), ("s", 960), ("s", 1280), ("m", 960), ("m", 1280)]
    plan = {"schema": "crossflow.yolo26_training_plan.v1", "created_at": datetime.now(timezone.utc).isoformat(),
        "protocol": "bounded_scale_resolution_and_accumulation_refinement", "epochs": epochs, "batch": 3, "nbs": 3,
        "optimizer": "SGD", "lr0": .001, "freeze_backbone_layers": 10, "training_imgsz": 640, "device": "cpu", "seed": SEED,
        "thresholds": list(THRESHOLDS), "pretrained_candidates": [{"architecture": "yolo26" + scale, "imgsz": size,
            "sha256": checkpoints[scale][1]} for scale, size in layouts],
        "fine_tuned_candidates": ["nano_nbs3_best_val_map_640", "nano_nbs3_last_640"],
        "train_images": len(train), "train_boxes": sum(len(r["vehicle_boxes"]) for r in train),
        "validation_images": len(validation), "validation_boxes": sum(len(r["vehicle_boxes"]) for r in validation),
        "split_sha256": protocol["split_sha256"], "annotation_manifest_sha256": protocol["annotation_manifest_sha256"],
        "adaptation_reason": "Nano640 validation misses distant vehicles; defaultnbs64 accumulated21microbatches. Newprotocolraisesresolution/modelscale andsetsnbs3foreverybatchweightupdates.",
        "prior_validation_receipt_sha256": digest(previous / "pretrained_validation.json"),
        "test_previously_consumed": True, "fresh_test": False, "test_not_used_for_selection": True,
        "annotation_origin": "ai_visual_blind", "human_certified": False, "production_eligible": False,
        "source_images_downloaded_this_run": 0, "augmentation_exports_are_not_new_observations": True,
        "license": "AGPL-3.0", "license_url": "https://www.ultralytics.com/license"}
    save(output / "training_plan.json", plan)
    dataset = prepare_development_dataset(protocol, {"train": train, "validation": validation}, output)
    for key, value in {"YOLO_OFFLINE": "true", "YOLO_AUTOINSTALL": "false", "YOLO_CONFIG_DIR": str(output / "runtime_config"),
                       "MPLCONFIGDIR": str(output / "runtime_config/matplotlib")}.items():
        os.environ[key] = value
    import torch
    from ultralytics import YOLO, settings
    settings.update({"sync": False, "clearml": False, "comet": False, "dvc": False, "mlflow": False,
                     "raytune": False, "tensorboard": False, "wandb": False})
    torch.set_num_threads(2)
    started, grid, candidates = time.monotonic(), [], {}
    for scale, size in layouts:
        path = checkpoints[scale][0]
        name = f"pretrained_yolo26{scale}_{size}"
        model = YOLO(str(path), task="detect")
        raw, latency = predict_rows(model, protocol, validation, COCO_VEHICLES, imgsz=size)
        candidates[name] = (path, 80, "yolo26" + scale, size)
        for threshold in THRESHOLDS:
            grid.append({"partition": "validation", "candidate": name, "score_threshold": threshold,
                         "metrics": metrics(filtered(raw, validation, COCO_VEHICLES, threshold), validation)})
        save(output / (name + "_latency.json"), {"latency_ms": latency, "imgsz": size})
        save(output / "pretrained_validation_grid.json", grid)
        print("Validation candidate", name, "minimum count MAE", min(r["metrics"]["count"]["mae_vehicles_per_frame"] for r in grid if r["candidate"] == name), flush=True)
    trainer = YOLO(str(checkpoints["n"][0]), task="detect")
    execution = {"optimizer_steps": 0, "minibatches": 0, "nbs": 3, "gradient_accumulation": None}
    def on_start(engine):
        original = engine.optimizer_step
        execution["gradient_accumulation"] = engine.accumulate
        def counted_step():
            execution["optimizer_steps"] += 1
            return original()
        engine.optimizer_step = counted_step
    def on_batch_end(_engine):
        execution["minibatches"] += 1
    trainer.add_callback("on_train_start", on_start)
    trainer.add_callback("on_train_batch_end", on_batch_end)
    trainer.train(data=str(dataset), epochs=epochs, batch=3, nbs=3, imgsz=640, device="cpu", workers=0,
        optimizer="SGD", lr0=.001, lrf=.1, momentum=.9, weight_decay=.0005, warmup_epochs=0., freeze=10,
        seed=SEED, deterministic=True, amp=False, patience=100, cache=False, mosaic=0., mixup=0., copy_paste=0.,
        fliplr=.5, scale=.2, translate=.05, plots=False, save=True, save_period=-1,
        project=str(output / "training"), name="nano_nbs3_20", exist_ok=False, verbose=False, val=True)
    run = Path(trainer.trainer.save_dir)
    save(output / "training_execution_receipt.json", execution)
    for suffix, file in (("best_val_map", "best.pt"), ("last", "last.pt")):
        name, path = "nano_nbs3_" + suffix + "_640", run / "weights" / file
        model = YOLO(str(path), task="detect")
        raw, latency = predict_rows(model, protocol, validation, {0: "vehicle"}, imgsz=640)
        candidates[name] = (path, 1, "yolo26n", 640)
        for threshold in THRESHOLDS:
            grid.append({"partition": "validation", "candidate": name, "score_threshold": threshold,
                         "metrics": metrics(filtered(raw, validation, {0: "vehicle"}, threshold), validation)})
        save(output / (name + "_latency.json"), {"latency_ms": latency, "imgsz": 640})
    save(output / "validation_grid.json", grid)
    chosen = select_validation(grid)
    path, classes, architecture, size = candidates[chosen["candidate"]]
    selection = freeze_selection(protocol, output, chosen, path, classes, architecture=architecture, imgsz=size)
    print("Refinement selection frozen", selection["selected_model"], selection["score_threshold"], flush=True)
    complete_reused_diagnostic(protocol, output, time.monotonic() - started)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--complete-reused-diagnostic", action="store_true")
    parser.add_argument("--refine", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.epochs <= 20:
        raise ValueError("This bounded protocol permits at most 20 epochs")
    output = (REPO / "backend/data/research/yolo26_batam_refinement" if args.refine and args.output == OUTPUT else args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.refine and not args.complete_reused_diagnostic:
        run_refinement(args.source, output, args.epochs)
        return
    if args.complete_reused_diagnostic:
        if (output / "report.json").exists():
            raise ValueError("Diagnostic already complete; refusing to overwrite")
        for key, value in {"YOLO_OFFLINE": "true", "YOLO_AUTOINSTALL": "false", "YOLO_CONFIG_DIR": str(output / "runtime_config"), "MPLCONFIGDIR": str(output / "runtime_config/matplotlib")}.items():
            os.environ[key] = value
        protocol = load_protocol(args.source / "split_v2.json", args.source / "annotation_manifest.json")
        complete_reused_diagnostic(protocol, output, None)
        return
    if any((output / name).exists() for name in ("training_plan.json", SELECTION_FILE, "report.json")):
        raise ValueError("Refusing to overwrite an existing YOLO26 experiment")
    pretrained = output / "pretrained_yolo26n.pt"
    if digest(pretrained) != PRETRAINED_SHA256:
        raise ValueError("Expected the retained official YOLO26n checkpoint hash")
    protocol = load_protocol(args.source / "split_v2.json", args.source / "annotation_manifest.json")
    train, validation = load_annotations(protocol, "train"), load_annotations(protocol, "validation")
    plan = {"schema": "crossflow.yolo26_training_plan.v1", "created_at": datetime.now(timezone.utc).isoformat(),
        "architecture": "yolo26n", "pretrained_sha256": PRETRAINED_SHA256, "epochs": args.epochs, "batch": 3,
        "optimizer": "SGD", "lr0": .001, "freeze_backbone_layers": 10, "imgsz": 640, "device": "cpu", "seed": SEED,
        "thresholds": list(THRESHOLDS), "candidates": ["pretrained_coco", "finetuned_best_val_map", "finetuned_last"],
        "selection": "Validation macro source-group count MAE, IoU50 F1, higher confidence threshold, lexical candidate",
        "train_images": len(train), "train_boxes": sum(len(row["vehicle_boxes"]) for row in train),
        "validation_images": len(validation), "validation_boxes": sum(len(row["vehicle_boxes"]) for row in validation),
        "split_sha256": protocol["split_sha256"], "annotation_manifest_sha256": protocol["annotation_manifest_sha256"],
        "test_previously_consumed": True, "fresh_test": False, "test_not_used_for_selection": True,
        "annotation_origin": "ai_visual_blind", "human_certified": False, "production_eligible": False,
        "source_images_downloaded_this_run": 0, "augmentation_exports_are_not_new_observations": True,
        "license": "AGPL-3.0", "license_url": "https://www.ultralytics.com/license"}
    save(output / "training_plan.json", plan)
    dataset = prepare_development_dataset(protocol, {"train": train, "validation": validation}, output)
    os.environ["YOLO_OFFLINE"] = "true"
    os.environ["YOLO_AUTOINSTALL"] = "false"
    os.environ["YOLO_CONFIG_DIR"] = str(output / "runtime_config")
    os.environ["MPLCONFIGDIR"] = str(output / "runtime_config/matplotlib")
    import torch
    from ultralytics import YOLO, settings
    import ultralytics
    if ultralytics.__version__ != "8.4.184":
        raise ValueError("Pinned Ultralytics runtime is required")
    settings.update({"sync": False, "clearml": False, "comet": False, "dvc": False, "mlflow": False,
                     "raytune": False, "tensorboard": False, "wandb": False})
    torch.set_num_threads(2)
    started = time.monotonic()
    baseline = YOLO(str(pretrained), task="detect")
    raw, latency = predict_rows(baseline, protocol, validation, COCO_VEHICLES)
    grid = [{"partition": "validation", "candidate": "pretrained_coco", "score_threshold": threshold,
             "metrics": metrics(filtered(raw, validation, COCO_VEHICLES, threshold), validation)} for threshold in THRESHOLDS]
    save(output / "pretrained_validation.json", {"grid": grid, "latency_ms": latency})
    trainer = YOLO(str(pretrained), task="detect")
    trainer.train(data=str(dataset), epochs=args.epochs, batch=3, imgsz=640, device="cpu", workers=0,
                  optimizer="SGD", lr0=.001, lrf=.1, momentum=.9, weight_decay=.0005, warmup_epochs=0.,
                  freeze=10, seed=SEED, deterministic=True, amp=False, patience=100, cache=False,
                  mosaic=0., mixup=0., copy_paste=0., fliplr=.5, scale=.2, translate=.05,
                  plots=False, save=True, save_period=-1, project=str(output / "training"), name="batam20",
                  exist_ok=False, verbose=False, val=True)
    run = Path(trainer.trainer.save_dir)
    candidates = {"pretrained_coco": (pretrained, 80), "finetuned_best_val_map": (run / "weights/best.pt", 1),
                  "finetuned_last": (run / "weights/last.pt", 1)}
    for candidate, (path, classes) in candidates.items():
        if candidate == "pretrained_coco":
            continue
        model = YOLO(str(path), task="detect")
        raw, candidate_latency = predict_rows(model, protocol, validation, {0: "vehicle"})
        for threshold in THRESHOLDS:
            grid.append({"partition": "validation", "candidate": candidate, "score_threshold": threshold,
                         "metrics": metrics(filtered(raw, validation, {0: "vehicle"}, threshold), validation)})
        save(output / f"{candidate}_validation_latency.json", {"latency_ms": candidate_latency})
    save(output / "validation_grid.json", grid)
    chosen = select_validation(grid)
    selected_path, num_classes = candidates[chosen["candidate"]]
    selection = freeze_selection(protocol, output, chosen, selected_path, num_classes)
    print("Validation selection frozen:", selection["selected_model"], selection["score_threshold"], flush=True)
    complete_reused_diagnostic(protocol, output, time.monotonic() - started)


if __name__ == "__main__":
    main()
