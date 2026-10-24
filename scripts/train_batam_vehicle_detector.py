#!/usr/bin/env python3
"""One bounded Batam detector experiment with a sealed, source-group holdout.

This program has no downloader, annotation generator, app promotion, or test-driven
search. Assistant-authored visual annotations remain distinct from certified human
ground truth. Importing the protocol/metric helpers does not import Torch or PIL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import shutil
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO.parent / "research/batam-traffic/datasets/round10_batam_vehicle_images"
OUTPUT = REPO / "backend/data/research/batam_detector_training"
SPLITS = ("train", "validation", "test")
EPOCHS = (2, 5, 10)
THRESHOLDS = (.2, .3, .4, .5, .6)
MIN_VEHICLE_LONG_SIDE_PX = 12
MIN_VEHICLE_SHORT_SIDE_PX = 4
SEED = 20261024
ARCHITECTURE = "fasterrcnn_resnet50_fpn_v2"
PRETRAINED_SHA256 = "dd69338a24b8d7381807e247652bdc356325bcbaf1cd3e092e00e0a1a58706bf"
COCO_VEHICLES = {2: "bicycle", 3: "car", 4: "motorcycle", 6: "bus", 8: "truck"}
CORRECT_FREEZE = "selection_frozen_before_batam_test.json"
SERVICE_FREEZE = "selection_frozen_before_reused_diagnostic.json"


def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path: Path, value) -> None:
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def checked_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("Expected a nonempty dataset-relative path")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Dataset path escapes source directory")
    return path


def is_sha256(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def load_protocol(split_path: Path, annotations_path: Path) -> dict:
    """Read metadata only. In particular, this never opens the test label file."""
    root = split_path.parent.resolve()
    split = json.loads(split_path.read_text())
    if split.get("schema") != "crossflow.batam_vehicle_split.v1":
        raise ValueError("Unknown Batam split schema")
    rows = split.get("images")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Split must contain image metadata")
    policy = split.get("split_policy")
    if not isinstance(policy, dict) or not is_sha256(policy.get("sha256")):
        raise ValueError("A retained preannotation split policy and SHA256 are required")
    policy_path = checked_path(root, policy.get("path"))
    if digest(policy_path) != policy["sha256"]:
        raise ValueError("Preannotation split policy changed")
    ids, bytes_seen, pixels_seen, groups, authors = set(), {}, {}, {}, {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid split row")
        ident, group, partition = row.get("image_id"), row.get("source_group"), row.get("split")
        if not isinstance(ident, str) or not ident or ident in ids:
            raise ValueError("Image identifiers must be nonempty and unique")
        ids.add(ident)
        if partition not in SPLITS or not isinstance(group, str) or not group:
            raise ValueError("Every image needs a source group and fixed partition")
        if group in groups and groups[group] != partition:
            raise ValueError("Source/capture group overlaps partitions")
        groups[group] = partition
        if row.get("geography") != "Batam" or row.get("training_eligible") is not True:
            raise ValueError("Only explicitly eligible Batam images may be used")
        if not row.get("source_id") or not row.get("license"):
            raise ValueError("Source identity and retained license evidence are required")
        for key in ("source_id", "photographer"):
            if key not in row:
                continue
            identity = row[key]
            if not isinstance(identity, str) or not identity:
                raise ValueError("Source/photographer identifiers must be nonempty strings")
            author_key = (key, identity)
            if author_key in authors and authors[author_key] != partition:
                raise ValueError("Source identity or photographer overlaps partitions")
            authors[author_key] = partition
        for dimension in ("width", "height"):
            if type(row.get(dimension)) is not int or not 2 <= row[dimension] <= 6000:
                raise ValueError("Invalid image dimensions")
        if row["width"] * row["height"] > 12_000_000:
            raise ValueError("Image exceeds serving pixel bounds")
        checked_path(root, row.get("path"))
        for key, seen in (("image_sha256", bytes_seen), ("decoded_sha256", pixels_seen)):
            value = row.get(key)
            if not is_sha256(value):
                raise ValueError(f"Invalid {key}")
            if value in seen:
                raise ValueError("Duplicate image bytes or decoded pixels; consolidate before freezing")
            seen[value] = ident
    counts = Counter(row["split"] for row in rows)
    if any(counts[name] == 0 for name in SPLITS):
        raise ValueError("A disjoint nonempty training, validation and test split is required")
    annotation_manifest = json.loads(annotations_path.read_text())
    if annotation_manifest.get("schema") != "crossflow.batam_vehicle_annotations.v1":
        raise ValueError("Unknown annotation manifest schema")
    if annotation_manifest.get("split_sha256") != digest(split_path):
        raise ValueError("Annotations do not bind the unchanged frozen split")
    if (annotation_manifest.get("annotation_origin") != "ai_visual_blind"
            or annotation_manifest.get("human_certified") is not False):
        raise ValueError("Expected explicitly disclosed blind assistant visual annotations")
    files = annotation_manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(SPLITS):
        raise ValueError("Exactly three separate annotation files are required")
    label_paths = set()
    for partition in SPLITS:
        row = files[partition]
        if not isinstance(row, dict) or not is_sha256(row.get("sha256")):
            raise ValueError("Every annotation partition needs its committed SHA256")
        path = checked_path(root, row.get("path"))
        if path in label_paths:
            raise ValueError("Annotation files must be physically separate")
        label_paths.add(path)
    return {"root": root, "split_path": split_path.resolve(), "annotations_path": annotations_path.resolve(),
            "split_sha256": digest(split_path), "annotation_manifest_sha256": digest(annotations_path),
            "split": split, "annotation_manifest": annotation_manifest, "images": rows, "exclusions": {}}


def verify_unchanged(protocol: dict) -> None:
    if digest(protocol["split_path"]) != protocol["split_sha256"]:
        raise ValueError("Frozen split changed during experiment")
    if digest(protocol["annotations_path"]) != protocol["annotation_manifest_sha256"]:
        raise ValueError("Annotation manifest changed during experiment")
    policy = protocol["split"]["split_policy"]
    if digest(checked_path(protocol["root"], policy["path"])) != policy["sha256"]:
        raise ValueError("Preannotation split policy changed during experiment")
    # Recheck development bytes without opening sealed test labels.
    for partition in ("train", "validation"):
        row = protocol["annotation_manifest"]["files"][partition]
        if digest(checked_path(protocol["root"], row["path"])) != row["sha256"]:
            raise ValueError("Development annotation bytes changed during experiment")


def load_annotations(protocol: dict, partition: str, *, freeze_path: Path | None = None) -> list[dict]:
    """Parse only the requested label partition, with a seal before test access."""
    if partition not in SPLITS:
        raise ValueError("Unknown annotation partition")
    verify_unchanged(protocol)
    if partition == "test":
        if freeze_path is None:
            raise ValueError("Test annotations are sealed until validation selection is frozen")
        validate_freeze(protocol, freeze_path)
    expected = protocol["annotation_manifest"]["files"][partition]
    path = checked_path(protocol["root"], expected["path"])
    if digest(path) != expected["sha256"]:
        raise ValueError("Annotation bytes changed from committed hash")
    annotations = json.loads(path.read_text())
    annotations = annotations.get("images") if isinstance(annotations, dict) else annotations
    if not isinstance(annotations, list):
        raise ValueError("Expected an annotation images array")
    metadata = {row["image_id"]: row for row in protocol["images"] if row["split"] == partition}
    seen, result = set(), []
    for item in annotations:
        if not isinstance(item, dict) or item.get("image_id") not in metadata or item["image_id"] in seen:
            raise ValueError("Annotation image IDs differ from the fixed partition")
        seen.add(item["image_id"])
        if item.get("annotation_origin", "ai_visual_blind") != "ai_visual_blind" or item.get("human_certified", False) is not False:
            raise ValueError("Annotation provenance differs from disclosed policy")
        if item.get("decision") == "exclude" or str(item.get("review_status", "")).startswith("excluded_"):
            reason = item.get("exclusion_reason")
            if (item.get("decision") != "exclude" or item.get("review_status") not in {"reviewed", "excluded_ambiguous", "excluded_off_domain"}
                    or not isinstance(reason, str) or not reason.strip()):
                raise ValueError("Excluded images need a frozen explicit decision, recognized category and reason")
            protocol["exclusions"][item["image_id"]] = {"image_id": item["image_id"], "split": partition, "reason": reason,
                                                        "review_status": item["review_status"],
                                                        "annotation_sha256": expected["sha256"]}
            continue
        if item.get("unresolved_above_threshold_ambiguity", False):
            raise ValueError("Resolve or exclude ambiguous images before inference, never after test results")
        boxes = item.get("boxes_xyxy")
        if not isinstance(boxes, list) or len(boxes) > 100:
            raise ValueError("Expected at most 100 normalized visible-vehicle boxes")
        row = metadata[item["image_id"]]
        actual = []
        for box in boxes:
            if (not isinstance(box, list) or len(box) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in box)
                    or box[0] >= box[2] or box[1] >= box[3]):
                raise ValueError("Invalid normalized xyxy annotation")
            actual.append([box[0] * row["width"], box[1] * row["height"], box[2] * row["width"], box[3] * row["height"]])
            box_width, box_height = actual[-1][2] - actual[-1][0], actual[-1][3] - actual[-1][1]
            if (min(box_width, box_height) < MIN_VEHICLE_SHORT_SIDE_PX - 1e-6
                    or max(box_width, box_height) < MIN_VEHICLE_LONG_SIDE_PX - 1e-6):
                raise ValueError("Annotation is below the frozen visible-extent size policy")
        result.append({**row, "vehicle_boxes": actual, "annotation_notes": item.get("notes", "")})
    if seen != set(metadata):
        raise ValueError("Annotations must cover every image, including verified empty frames")
    if not result:
        raise ValueError("Each partition needs an included unambiguous image")
    return result


def validate_freeze(protocol: dict, path: Path) -> dict:
    if not path.is_file():
        raise ValueError("Missing validation selection freeze")
    frozen = json.loads(path.read_text())
    if (frozen.get("schema") != "crossflow.batam_vehicle_selection.v1"
            or frozen.get("split_sha256") != protocol["split_sha256"]
            or frozen.get("annotation_manifest_sha256") != protocol["annotation_manifest_sha256"]
            or frozen.get("test_labels_read_before_freeze") is not False
            or frozen.get("production_eligible") is not False):
        raise ValueError("Selection seal does not match the fixed protocol")
    model_path = path.parent / "model.pt"
    if not is_sha256(frozen.get("model_sha256")) or digest(model_path) != frozen["model_sha256"]:
        raise ValueError("Selected checkpoint differs from frozen hash")
    if (frozen.get("architecture") != ARCHITECTURE or frozen.get("num_classes") not in (3, 91)
            or frozen.get("score_threshold") not in THRESHOLDS
            or frozen.get("vehicle_nms_iou") != .5
            or frozen.get("min_vehicle_long_side_px") != MIN_VEHICLE_LONG_SIDE_PX
            or frozen.get("min_vehicle_short_side_px") != MIN_VEHICLE_SHORT_SIDE_PX):
        raise ValueError("Frozen serving configuration is outside the declared protocol")
    validation = frozen.get("validation")
    if not isinstance(validation, dict) or validation.get("partition") != "validation":
        raise ValueError("Selection must record validation-only model selection")
    if (validation.get("candidate") != frozen.get("selected_model")
            or validation.get("score_threshold") != frozen.get("score_threshold")):
        raise ValueError("Selected candidate/threshold differs from validation receipt")
    expected_classes = {str(k): v for k, v in (COCO_VEHICLES if frozen["num_classes"] == 91 else {1: "vehicle"}).items()}
    if frozen.get("vehicle_classes") != expected_classes:
        raise ValueError("Frozen class mapping differs from the selected head")
    return frozen


def overlap(first, second) -> float:
    left, top = max(first[0], second[0]), max(first[1], second[1])
    right, bottom = min(first[2], second[2]), min(first[3], second[3])
    intersection = max(0., right - left) * max(0., bottom - top)
    union = (first[2] - first[0]) * (first[3] - first[1]) + (second[2] - second[0]) * (second[3] - second[1]) - intersection
    return intersection / union if union > 0 else 0.


def metrics(predicted: list[list[dict]], rows: list[dict]) -> dict:
    if not rows or len(predicted) != len(rows):
        raise ValueError("Aligned nonempty predictions and labels required")
    errors = [len(boxes) - len(row["vehicle_boxes"]) for boxes, row in zip(predicted, rows)]
    groups = sorted({row["source_group"] for row in rows})
    group_errors = {group: [error for row, error in zip(rows, errors) if row["source_group"] == group] for group in groups}
    matched = [set() for _ in rows]
    true_positive = false_positive = 0
    ranked = sorted(((float(box["score"]), i, box["box_xyxy"]) for i, boxes in enumerate(predicted) for box in boxes), reverse=True)
    for _, index, box in ranked:
        choices = [(overlap(box, actual), k) for k, actual in enumerate(rows[index]["vehicle_boxes"]) if k not in matched[index]]
        best_iou, best_index = max(choices, default=(0., -1))
        if best_iou >= .5:
            matched[index].add(best_index)
            true_positive += 1
        else:
            false_positive += 1
    total = sum(len(row["vehicle_boxes"]) for row in rows)
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.
    recall = true_positive / total if total else 0.
    per_group = {group: {"frames": len(values), "mae_vehicles_per_frame": statistics.mean(abs(x) for x in values),
                         "exact_count_pct": 100 * statistics.mean(x == 0 for x in values)} for group, values in group_errors.items()}
    return {"count": {"frames": len(rows), "source_groups": len(groups), "mae_vehicles_per_frame": statistics.mean(abs(x) for x in errors),
                      "macro_source_group_mae": statistics.mean(row["mae_vehicles_per_frame"] for row in per_group.values()),
                      "rmse_vehicles_per_frame": math.sqrt(statistics.mean(x * x for x in errors)),
                      "bias_vehicles_per_frame": statistics.mean(errors), "exact_count_pct": 100 * statistics.mean(x == 0 for x in errors),
                      "within_one_vehicle_pct": 100 * statistics.mean(abs(x) <= 1 for x in errors),
                      "actual_total": total, "predicted_total": sum(map(len, predicted))},
            "detection_iou_50": {"precision": precision, "recall": recall, "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.,
                                 "true_positive": true_positive, "false_positive": false_positive, "false_negative": total - true_positive},
            "per_source_group": per_group}


def select_validation(grid: list[dict]) -> dict:
    if not grid or any(row.get("partition") != "validation" for row in grid):
        raise ValueError("Only validation measurements may select a model or threshold")
    return min(grid, key=lambda row: (row["metrics"]["count"]["macro_source_group_mae"],
                                      -row["metrics"]["detection_iou_50"]["f1"], -row["score_threshold"], row["candidate"]))


def tensor_rows(protocol: dict, rows: list[dict]) -> list[dict]:
    from PIL import Image, ImageOps
    from torchvision.transforms.functional import to_tensor
    result = []
    for row in rows:
        path = checked_path(protocol["root"], row["path"])
        if digest(path) != row["image_sha256"]:
            raise ValueError("Image bytes changed from committed hash")
        with Image.open(path) as original:
            if getattr(original, "n_frames", 1) != 1:
                raise ValueError("Animated images are not accepted")
            image = ImageOps.exif_transpose(original).convert("RGB")
            if image.size != (row["width"], row["height"]):
                raise ValueError("Image dimensions differ after EXIF transpose")
            if hashlib.sha256(image.tobytes()).hexdigest() != row["decoded_sha256"]:
                raise ValueError("Decoded pixels changed from committed hash")
            result.append({**row, "tensor": to_tensor(image)})
    return result


def build(classes: int, path: Path):
    import torch
    from torchvision.models.detection import fasterrcnn_resnet50_fpn_v2
    model = fasterrcnn_resnet50_fpn_v2(weights=None, weights_backbone=None, num_classes=classes,
                                     box_score_thresh=.001, box_detections_per_img=300)
    model.load_state_dict(torch.load(path, map_location="cpu", weights_only=True))
    return model


def predict(model, rows):
    import torch
    model.eval()
    predictions, timings = [], []
    with torch.inference_mode():
        for row in rows:
            started = time.monotonic()
            raw = model([row["tensor"]])[0]
            timings.append((time.monotonic() - started) * 1000)
            predictions.append({key: value.detach().cpu() for key, value in raw.items()})
    return predictions, timings


def apply_size_policy(detections: list[dict]) -> list[dict]:
    """Use the annotation inclusion rule in native decoded-image pixels.

    This is a pilot evaluation rule, separate from the old app's unfiltered count.
    Both the selected candidate and fixed-checkpoint reference use this same rule.
    """
    retained = []
    for detection in detections:
        box = detection["box_xyxy"]
        if not all(math.isfinite(value) for value in box):
            continue
        width, height = box[2] - box[0], box[3] - box[1]
        if (min(width, height) >= MIN_VEHICLE_SHORT_SIDE_PX - 1e-6
                and max(width, height) >= MIN_VEHICLE_LONG_SIDE_PX - 1e-6):
            retained.append(detection)
    return retained


def filtered(outputs, rows, threshold, classes):
    sys.path.insert(0, str(REPO / "backend"))
    from services.vehicle_counter import filter_detections
    return [apply_size_policy(filter_detections(raw, width=row["width"], height=row["height"], threshold=threshold,
                                               vehicle_classes=classes, nms_iou=.5)) for raw, row in zip(outputs, rows)]


def head_model(pretrained):
    import torch
    from torchvision.models.detection.faster_rcnn import FastRCNNPredictor
    original = pretrained.roi_heads.box_predictor
    replacement = FastRCNNPredictor(original.cls_score.in_features, 3)
    with torch.no_grad():
        for destination, indexes in ((0, [0]), (1, list(COCO_VEHICLES)), (2, [1])):
            replacement.cls_score.weight[destination].copy_(original.cls_score.weight[indexes].mean(0))
            replacement.cls_score.bias[destination].copy_(original.cls_score.bias[indexes].mean(0))
            if destination == 1:
                replacement.cls_score.bias[destination].add_(math.log(len(COCO_VEHICLES)))
            replacement.bbox_pred.weight.reshape(3, 4, -1)[destination].copy_(original.bbox_pred.weight.reshape(91, 4, -1)[indexes].mean(0))
            replacement.bbox_pred.bias.reshape(3, 4)[destination].copy_(original.bbox_pred.bias.reshape(91, 4)[indexes].mean(0))
    pretrained.roi_heads.box_predictor = replacement
    for parameter in pretrained.parameters():
        parameter.requires_grad = False
    for parameter in replacement.parameters():
        parameter.requires_grad = True
    return pretrained


def run(split_path: Path, annotations_path: Path, output: Path, pretrained_path: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Use a new output directory; frozen experiments are never overwritten")
    protocol = load_protocol(split_path, annotations_path)
    if digest(pretrained_path) != PRETRAINED_SHA256:
        raise ValueError("Expected unchanged official pretrained COCO FasterRCNN checkpoint")
    # Only training and validation labels are opened before immutable selection.
    train = tensor_rows(protocol, load_annotations(protocol, "train"))
    validation = tensor_rows(protocol, load_annotations(protocol, "validation"))
    if not any(row["vehicle_boxes"] for row in train):
        raise ValueError("Training must contain at least one positive annotated vehicle")
    import torch
    import torchvision
    torch.set_num_threads(2)
    torch.manual_seed(SEED)
    random.seed(SEED)
    torch.use_deterministic_algorithms(True)
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    plan = {"schema": "crossflow.batam_vehicle_training_plan.v1", "generated_at": datetime.now(timezone.utc).isoformat(),
            "script_sha256": digest(Path(__file__)),
            "split_sha256": protocol["split_sha256"], "annotation_manifest_sha256": protocol["annotation_manifest_sha256"],
            "architecture": ARCHITECTURE, "num_finetuned_classes": 3, "label_mapping": {"0": "background", "1": "vehicle", "2": "unused initialized pedestrian"},
            "source_groups": {name: sorted({row["source_group"] for row in protocol["images"] if row["split"] == name}) for name in SPLITS},
            "image_counts": dict(Counter(row["split"] for row in protocol["images"])), "seed": SEED, "device": "cpu", "threads": 2,
            "included_development_images": {"train": len(train), "validation": len(validation)},
            "included_development_vehicle_boxes": {"train": sum(len(row["vehicle_boxes"]) for row in train),
                                                     "validation": sum(len(row["vehicle_boxes"]) for row in validation)},
            "training_updates": max(EPOCHS) * math.ceil(len(train) / 2),
            "candidates": ["pretrained_COCO", *[f"batam_head_epoch_{epoch}" for epoch in EPOCHS]], "epochs": EPOCHS, "thresholds": THRESHOLDS,
            "optimizer": "AdamW", "learning_rate": .0003, "weight_decay": .0001, "batch_size": 2,
            "trainable": "ROI box predictor only; entire backbone, RPN, box head, and normalization statistics frozen",
            "augmentation": "seeded random horizontal flip training only", "pretrained_sha256": PRETRAINED_SHA256,
            "min_vehicle_long_side_px": MIN_VEHICLE_LONG_SIDE_PX, "min_vehicle_short_side_px": MIN_VEHICLE_SHORT_SIDE_PX,
            "count_scope": "size_filtered_visible_vehicle_boxes_per_image",
            "selection": "Lowest validation macro source-group count MAE; then highest IoU50 detection F1; then highest threshold; then lexical candidate name",
            "test_protocol": "Read test labels and run inference only after model and threshold selection are frozen. Never select or retrain from this test result.",
            "annotation_origin": "ai_visual_blind", "human_certified": False, "production_eligible": False,
            "app_reference": "Existing pretrained COCO checkpoint at fixed score .4 with the same native-pixel 12/4 size filter; this is not the unfiltered app response. Test reference is not eligible for post-test selection"}
    save(output / "training_plan.json", plan)
    pretrained = build(91, pretrained_path)
    raw, timings = predict(pretrained, validation)
    outputs = {"pretrained_COCO": raw}
    candidate_timings = {"pretrained_COCO": timings}
    classes = {"pretrained_COCO": COCO_VEHICLES}
    paths = {"pretrained_COCO": pretrained_path}
    model = head_model(pretrained)
    optimizer = torch.optim.AdamW(model.roi_heads.box_predictor.parameters(), lr=.0003, weight_decay=.0001)
    losses = []
    for epoch in range(1, max(EPOCHS) + 1):
        model.train()
        # Freezing parameters alone does not freeze BatchNorm statistics.
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
                boxes = torch.tensor(row["vehicle_boxes"], dtype=torch.float32).reshape(-1, 4)
                if random.random() < .5:
                    image = image.flip(-1)
                    boxes[:, [0, 2]] = row["width"] - boxes[:, [2, 0]]
                images.append(image)
                targets.append({"boxes": boxes, "labels": torch.ones(len(boxes), dtype=torch.int64)})
            optimizer.zero_grad(set_to_none=True)
            terms = model(images, targets)
            loss = terms["loss_classifier"] + terms["loss_box_reg"]
            if not torch.isfinite(loss):
                raise ValueError("Nonfinite training loss")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.roi_heads.box_predictor.parameters(), 10.)
            optimizer.step()
            epoch_losses.append(float(loss.detach()))
        losses.append({"epoch": epoch, "mean_roi_training_loss": statistics.mean(epoch_losses)})
        print(json.dumps(losses[-1]), flush=True)
        if epoch in EPOCHS:
            name = f"batam_head_epoch_{epoch}"
            path = output / f"{name}.pt"
            torch.save(model.state_dict(), path)
            outputs[name], candidate_timings[name] = predict(model, validation)
            paths[name], classes[name] = path, {1: "vehicle"}
    save(output / "training_losses.json", losses)
    grid = [{"partition": "validation", "candidate": name, "score_threshold": threshold,
             "metrics": metrics(filtered(raw, validation, threshold, classes[name]), validation)}
            for name, raw in outputs.items() for threshold in THRESHOLDS]
    save(output / "validation_grid.json", grid)
    selected = select_validation(grid)
    name = selected["candidate"]
    shutil.copyfile(paths[name], output / "model.pt")
    verify_unchanged(protocol)
    frozen = {"schema": "crossflow.batam_vehicle_selection.v1", "generated_at": datetime.now(timezone.utc).isoformat(),
              "split_sha256": protocol["split_sha256"], "annotation_manifest_sha256": protocol["annotation_manifest_sha256"],
              "selected_model": name, "architecture": ARCHITECTURE, "num_classes": 91 if name == "pretrained_COCO" else 3,
              "vehicle_classes": {str(k): v for k, v in classes[name].items()}, "score_threshold": selected["score_threshold"],
              "vehicle_nms_iou": .5, "model_sha256": digest(output / "model.pt"), "validation": selected,
              "min_vehicle_long_side_px": MIN_VEHICLE_LONG_SIDE_PX, "min_vehicle_short_side_px": MIN_VEHICLE_SHORT_SIDE_PX,
              "count_scope": "size_filtered_visible_vehicle_boxes_per_image",
              "training_plan_sha256": digest(output / "training_plan.json"),
              "selection_metric": plan["selection"], "test_labels_read_before_freeze": False, "production_eligible": False,
              "batam_validated": False, "annotation_origin": "ai_visual_blind", "human_certified": False,
              "scope": "Small Batam visual-label pilot; native-pixel vehicle extent long side >=12px and short side >=4px, no certified human benchmark or production promotion",
              "pretrained_weight_terms": "TorchVision warns of dataset-derived terms; original COCO image rights vary, production clearance not certified"}
    save(output / CORRECT_FREEZE, frozen)
    # Current app loader uses this historical filename. This alias is byte-identical,
    # not a claim that this new Batam pilot is the previous foreign diagnostic.
    shutil.copyfile(output / CORRECT_FREEZE, output / SERVICE_FREEZE)
    print(json.dumps({"event": "selection_frozen_before_test", "selected_model": name,
                      "score_threshold": frozen["score_threshold"], "model_sha256": frozen["model_sha256"],
                      "validation": selected["metrics"], "test_labels_read": False}), flush=True)
    evaluate_test(protocol, output, pretrained_path, started=started, candidate_timings=candidate_timings,
                  runtime={"torch": torch.__version__, "torchvision": torchvision.__version__, "python": sys.version})


def evaluate_test(protocol: dict, output: Path, pretrained_path: Path, *, started=None, candidate_timings=None, runtime=None) -> None:
    """One frozen candidate and the predeclared existing-app reference, no search."""
    if (output / "report.json").exists() or (output / "test_opened.json").exists():
        raise FileExistsError("Test evaluation is single-use; do not adapt to the consumed holdout")
    freeze_path = output / CORRECT_FREEZE
    frozen = validate_freeze(protocol, freeze_path)
    freeze_sha = digest(freeze_path)
    if digest(output / SERVICE_FREEZE) != freeze_sha:
        raise ValueError("Serving alias differs from immutable test selection")
    if digest(output / "training_plan.json") != frozen.get("training_plan_sha256"):
        raise ValueError("Training protocol changed after validation selection")
    if digest(pretrained_path) != PRETRAINED_SHA256:
        raise ValueError("Reference checkpoint differs from predeclared hash")
    # An attempt marker prevents partially failed test runs from silently becoming
    # a second tuning opportunity. Repair requires an explicit separate audit.
    save(output / "test_opened.json", {"generated_at": datetime.now(timezone.utc).isoformat(), "selection_sha256": freeze_sha,
                                      "test_annotation_sha256": protocol["annotation_manifest"]["files"]["test"]["sha256"],
                                      "purpose": "Single-use source-group Batam pilot; not certified human ground truth"})
    test = tensor_rows(protocol, load_annotations(protocol, "test", freeze_path=freeze_path))
    chosen = build(frozen["num_classes"], output / "model.pt")
    raw, timing = predict(chosen, test)
    chosen_boxes = filtered(raw, test, frozen["score_threshold"], {int(k): v for k, v in frozen["vehicle_classes"].items()})
    selected_metrics = metrics(chosen_boxes, test)
    del chosen
    reference = build(91, pretrained_path)
    baseline_raw, baseline_timing = predict(reference, test)
    baseline_boxes = filtered(baseline_raw, test, .4, COCO_VEHICLES)
    baseline_metrics = metrics(baseline_boxes, test)
    del reference
    # Fixed-candidate decode/service parity is a reproducibility check, not another
    # selection pass. The old service output receives the declared pilot size rule.
    from services.vehicle_counter import VehicleCounter
    counter = VehicleCounter(output, allow_experimental=True)
    parity = []
    for row, expected in list(zip(test, chosen_boxes))[:3]:
        response = counter.count_image(checked_path(protocol["root"], row["path"]).read_bytes())
        scoped = apply_size_policy(response["detections"])
        same = len(scoped) == len(expected) and all(
            max(abs(a - b) for a, b in zip(actual["box_xyxy"], reference_box["box_xyxy"])) <= 1e-4
            and abs(actual["score"] - reference_box["score"]) <= 1e-6
            for actual, reference_box in zip(scoped, expected))
        parity.append({"image_id": row["image_id"], "passed": same, "raw_service_count": response["vehicle_count"],
                       "size_filtered_pilot_count": len(scoped), "cpu_latency_ms": response["latency_ms"]})
        if not same:
            save(output / "service_parity.json", {"passed": False, "frames": parity})
            raise ValueError("Frozen candidate service/decode/size-policy parity failed")
    save(output / "service_parity.json", {"passed": True, "frames": parity,
                                         "scope": "Existing experimental service plus the frozen native-pixel 12/4 size filter"})
    if digest(freeze_path) != freeze_sha or digest(output / SERVICE_FREEZE) != freeze_sha:
        raise ValueError("Selection changed after test access")
    if digest(output / "training_plan.json") != frozen["training_plan_sha256"]:
        raise ValueError("Training protocol changed after test access")
    verify_unchanged(protocol)
    test_receipt = protocol["annotation_manifest"]["files"]["test"]
    if digest(checked_path(protocol["root"], test_receipt["path"])) != test_receipt["sha256"]:
        raise ValueError("Test annotations changed during evaluation")
    for row in protocol["images"]:
        if digest(checked_path(protocol["root"], row["path"])) != row["image_sha256"]:
            raise ValueError("Source image bytes changed during experiment")
    selected_mae, baseline_mae = selected_metrics["count"]["mae_vehicles_per_frame"], baseline_metrics["count"]["mae_vehicles_per_frame"]
    plan = json.loads((output / "training_plan.json").read_text())
    if digest(Path(__file__)) != plan["script_sha256"]:
        raise ValueError("Training script changed while the experiment was running")
    report = {"schema": "crossflow.batam_vehicle_detector_report.v1", "generated_at": datetime.now(timezone.utc).isoformat(),
              "training_performed": True, "selected_model": frozen["selected_model"], "selected_model_is_finetuned": frozen["num_classes"] == 3,
              "score_threshold": frozen["score_threshold"], "source_image_counts": dict(Counter(row["split"] for row in protocol["images"])),
              "included_image_counts": {**plan["included_development_images"], "test": len(test)},
              "included_annotated_vehicle_boxes": {**plan["included_development_vehicle_boxes"], "test": selected_metrics["count"]["actual_total"]},
              "training_updates": plan["training_updates"],
              "preinference_annotation_exclusions": list(protocol["exclusions"].values()),
              "validation": frozen["validation"]["metrics"], "batam_group_holdout_pilot": selected_metrics,
              "existing_app_fixed_threshold_reference": baseline_metrics, "existing_app_reference_threshold": .4,
              "reference_scope": "Prior app checkpoint at .4 with the same native-pixel 12/4 inclusion filter; not the literal unfiltered app response",
              "count_scope": "size_filtered_visible_vehicle_boxes_per_image",
              "min_vehicle_long_side_px": MIN_VEHICLE_LONG_SIDE_PX, "min_vehicle_short_side_px": MIN_VEHICLE_SHORT_SIDE_PX,
              "test_mae_improvement_pct": 100 * (1 - selected_mae / baseline_mae) if baseline_mae else None,
              "beats_existing_app_reference_on_this_pilot": selected_mae < baseline_mae,
              "test_selection_sha256": freeze_sha, "split_sha256": protocol["split_sha256"],
              "annotation_manifest_sha256": protocol["annotation_manifest_sha256"],
              "annotation_origin": "ai_visual_blind", "human_certified": False,
              "production_eligible": False, "batam_validated": False, "independent_human_batam_benchmark": False,
              "google_comparison_performed": False, "application_model_promoted": False,
              "latency_ms_cpu_forward": {"selected_median": statistics.median(timing), "reference_median": statistics.median(baseline_timing),
                                         "threads": 2, "scope": "Forward only; excludes decoding, first load, queue and network"},
              "runtime": runtime or {}, "elapsed_seconds": time.monotonic() - started if started else None,
              "limitations": ["Small public-photo Batam pilot with assistant-authored blind visual boxes, not certified human ground truth.",
                              "Distinct source/capture groups prevent exact group leakage, but photographer/capture timing may be incompletely documented.",
                              "No 99-100% accuracy, Google comparison, citywide camera accuracy, road flow, queue length, or speed claim.",
                              "Only visible vehicle extents meeting the frozen size policy are annotated; occluded objects remain uncertain.",
                              "Holdout is consumed after this report; future model selection requires a new untouched source-group test.",
                              "License evidence is retained per image; COCO-derived weight production rights are not certified."]}
    save(output / "test_predictions.json", [{"image_id": row["image_id"], "source_group": row["source_group"],
                                          "actual_count": len(row["vehicle_boxes"]), "predicted_count": len(boxes),
                                          "reference_predicted_count": len(reference_boxes), "detections": boxes}
                                         for row, boxes, reference_boxes in zip(test, chosen_boxes, baseline_boxes)])
    save(output / "report.json", report)
    save(output / "qa.json", {"passed": True, "selection_unchanged_after_test": True, "test_labels_opened_after_selection": True,
                              "source_group_overlap": False, "duplicate_source_bytes_or_pixels": False,
                              "service_parity_with_size_policy": parity,
                              "annotations_human_certified": False, "app_model_promoted": False, "production_eligible": False})
    evidence = [{"path": str(path.relative_to(output)), "bytes": path.stat().st_size, "sha256": digest(path)}
                for path in sorted(output.rglob("*")) if path.is_file() and path.name != "manifest.json"]
    save(output / "manifest.json", {"schema": "crossflow.batam_vehicle_detector_artifacts.v1", "files": evidence,
                                    "split_sha256": protocol["split_sha256"], "annotation_manifest_sha256": protocol["annotation_manifest_sha256"],
                                    "script_sha256": digest(Path(__file__)), "model_sha256": frozen["model_sha256"],
                                    "production_eligible": False, "batam_validated": False})
    print(json.dumps({"selected_model": frozen["selected_model"], "threshold": frozen["score_threshold"],
                      "batam_pilot": selected_metrics, "reference": baseline_metrics, "production_eligible": False}, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", type=Path, default=SOURCE / "split.json")
    parser.add_argument("--annotations", type=Path, default=SOURCE / "annotation_manifest.json")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--pretrained", type=Path, default=REPO / "backend/data/research/detector_training/model.pt")
    args = parser.parse_args()
    run(args.split, args.annotations, args.output, args.pretrained)
