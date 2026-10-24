#!/usr/bin/env python3
"""Offline pixel-to-annotation-count transfer diagnostic; not a detector.

Uses existing licensed local images only, trains on Visaitech and evaluates the
fixed final model on TRANSSET. No network, pretrained models or augmentation.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import sklearn
from PIL import Image, __version__ as PILLOW_VERSION
from sklearn.ensemble import ExtraTreesRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

REPO = Path(__file__).resolve().parents[1]
WORKSPACE = REPO.parent
SOURCE_ROOT = WORKSPACE / "research/batam-traffic/datasets/round4_open/vision"
DEFAULT_OUTPUT = REPO / "backend/data/research/vision_training"
SCHEMA = "crossflow.visual_count_transfer.v1"
RANDOM_SEED = 20261024


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def count_yolo(path: Path, *, vehicle_class_ids: set[int], all_class_ids: set[int]) -> tuple[int, int]:
    count, boxes = 0, 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        values = line.split()
        if len(values) != 5:
            raise ValueError(f"Expected five YOLO fields: {path}")
        class_value, *box = map(float, values)
        class_id = int(class_value)
        if class_id != class_value or class_id not in all_class_ids:
            raise ValueError(f"Unverified class ID: {path}")
        if not all(math.isfinite(v) and 0 <= v <= 1 for v in box) or min(box[2:]) <= 0:
            raise ValueError(f"Invalid YOLO box: {path}")
        boxes += 1
        count += int(class_id in vehicle_class_ids)
    return count, boxes


def pixel_features(image: Image.Image) -> tuple[np.ndarray, list[str]]:
    """Fixed RGB/texture summaries computed exclusively from image pixels."""
    rgb = np.asarray(image.convert("RGB").resize((128, 128), Image.Resampling.BILINEAR), dtype=float) / 255
    gray = rgb @ np.array([.299, .587, .114])
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    gx[:, 1:-1] = (gray[:, 2:] - gray[:, :-2]) / 2
    gy[1:-1] = (gray[2:] - gray[:-2]) / 2
    magnitude = np.hypot(gx, gy)
    bins = np.floor(np.mod(np.arctan2(gy, gx), np.pi) / np.pi * 8).astype(int).clip(0, 7)
    saturation = np.divide(rgb.max(axis=2) - rgb.min(axis=2), rgb.max(axis=2),
                           out=np.zeros_like(gray), where=rgb.max(axis=2) > 0)
    names: list[str] = []
    output: list[float] = []
    for row in range(4):
        for column in range(4):
            section = (slice(row * 32, (row + 1) * 32), slice(column * 32, (column + 1) * 32))
            patch, light, mag = rgb[section], gray[section], magnitude[section]
            prefix = f"cell_{row}_{column}_"
            values = list(patch.mean(axis=(0, 1))) + list(patch.std(axis=(0, 1)))
            labels = ["red_mean", "green_mean", "blue_mean", "red_std", "green_std", "blue_std"]
            values += [float(light.mean()), float(light.std()), float(mag.mean()), float(mag.std()),
                       float(saturation[section].mean()), float((mag > .08).mean()), float((light < .02).mean())]
            labels += ["gray_mean", "gray_std", "gradient_mean", "gradient_std", "saturation_mean", "edge_fraction", "black_fraction"]
            histogram = np.bincount(bins[section].ravel(), weights=mag.ravel(), minlength=8)
            histogram = histogram / max(float(histogram.sum()), 1e-12)
            values += list(histogram)
            labels += [f"gradient_orientation_{i}" for i in range(8)]
            output.extend(float(value) for value in values)
            names.extend(prefix + name for name in labels)
    histogram, _ = np.histogram(gray, bins=16, range=(0, 1), density=False)
    output.extend(float(value / gray.size) for value in histogram)
    names.extend(f"global_gray_histogram_{i}" for i in range(16))
    result = np.asarray(output)
    if not np.isfinite(result).all():
        raise ValueError("Nonfinite image features")
    return result, names


def perceptual_hashes(image: Image.Image) -> tuple[int, int]:
    gray = image.convert("L")
    small = np.asarray(gray.resize((9, 8), Image.Resampling.BILINEAR))
    difference = small[:, 1:] > small[:, :-1]
    # Small fixed DCT basis, without another dependency or learned parameters.
    square = np.asarray(gray.resize((32, 32), Image.Resampling.BILINEAR), dtype=float)
    basis = np.cos(np.pi / 32 * (np.arange(32)[None, :] + .5) * np.arange(8)[:, None])
    low_frequency = basis @ square @ basis.T
    median = np.median(low_frequency.ravel()[1:])
    perceptual = low_frequency > median
    perceptual[0, 0] = False
    def pack(bits: np.ndarray) -> int:
        value = 0
        for bit in bits.ravel():
            value = (value << 1) | int(bit)
        return value
    return pack(difference), pack(perceptual)


def potential_duplicate(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (a["image_sha256"] == b["image_sha256"] or a["decoded_rgb_sha256"] == b["decoded_rgb_sha256"]
            or ((int(a["dhash"], 16) ^ int(b["dhash"], 16)).bit_count() <= 4
                and (int(a["phash"], 16) ^ int(b["phash"], 16)).bit_count() <= 4))


def load_source(root: Path, directory: str) -> tuple[list[dict[str, Any]], np.ndarray, list[str]]:
    source = root / directory
    file_manifest = json.loads((source / "files_manifest.json").read_text())
    verified = {record["local_path"]: record for record in file_manifest["files"]}
    frames = [json.loads(line) for line in (source / "frame_counts.jsonl").read_text().splitlines() if line]
    rows: list[dict[str, Any]] = []
    features: list[np.ndarray] = []
    names: list[str] = []
    for frame in frames:
        image_path, label_path = root / frame["image"], root / frame["label"]
        for relative in (frame["image"], frame["label"]):
            record = verified.get(relative)
            if record is None or digest(root / relative) != record["sha256"]:
                raise ValueError(f"Source manifest mismatch: {relative}")
        classes = set(range(4 if directory == "visaitech_mixed_traffic" else 8))
        vehicles = classes - {0} if directory == "visaitech_mixed_traffic" else classes
        count, boxes = count_yolo(label_path, vehicle_class_ids=vehicles, all_class_ids=classes)
        if count != frame["vehicle_count_excluding_pedestrians"] or boxes != frame["box_count"]:
            raise ValueError(f"Count differs from audited source labels: {label_path}")
        with Image.open(image_path) as image:
            if image.size != (frame["width"], frame["height"]):
                raise ValueError("Source dimensions changed")
            vector, feature_names = pixel_features(image)
            dhash, phash = perceptual_hashes(image)
            decoded_hash = hashlib.sha256(image.convert("RGB").tobytes()).hexdigest()
        names = feature_names
        features.append(vector)
        rows.append({"source": directory, "image": frame["image"], "label": frame["label"],
                     "source_video_group": frame["source_video_group"],
                     "author_split": "train" if "/train/" in frame["image"] else "val" if "/val/" in frame["image"] else "not_released",
                     "target_count": count, "all_annotation_boxes": boxes,
                     "image_sha256": digest(image_path), "label_sha256": digest(label_path),
                     "decoded_rgb_sha256": decoded_hash, "dhash": f"{dhash:016x}", "phash": f"{phash:016x}"})
    return rows, np.asarray(features), names


def grouping_and_duplicate_audit(train: list[dict[str, Any]], test: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    parents = {row["source_video_group"]: row["source_video_group"] for row in train}
    def find(group: str) -> str:
        while parents[group] != group:
            group = parents[group]
        return group
    pairs: list[dict[str, Any]] = []
    for i, first in enumerate(train):
        for second in train[i + 1:]:
            if potential_duplicate(first, second):
                cross_video = first["source_video_group"] != second["source_video_group"]
                if cross_video:
                    one, two = sorted([find(first["source_video_group"]), find(second["source_video_group"])])
                    parents[two] = one
                pairs.append({"a": first["image"], "b": second["image"], "cross_video_groups": cross_video})
    groups = np.asarray([find(row["source_video_group"]) for row in train])
    excluded: list[int] = []
    cross_pairs: list[dict[str, Any]] = []
    for index, candidate in enumerate(test):
        for training in train:
            if potential_duplicate(training, candidate):
                excluded.append(index)
                cross_pairs.append({"train": training["image"], "test": candidate["image"]})
    test_keep = np.asarray([index not in excluded for index in range(len(test))])
    within_test_pairs = []
    for i, first in enumerate(test):
        for second in test[i + 1:]:
            if potential_duplicate(first, second):
                within_test_pairs.append({"a": first["image"], "b": second["image"]})
    return groups, test_keep, {
        "rule": "Exact source bytes or exact decoded RGB, or BOTH 64-bit dHash and DCT pHash Hamming distances <= 4",
        "train_potential_duplicate_pairs": pairs,
        "train_cross_video_merge_pairs": sum(pair["cross_video_groups"] for pair in pairs),
        "train_original_video_group_count": len(parents), "effective_validation_groups": sorted(set(groups)),
        "cross_source_potential_duplicate_pairs": cross_pairs,
        "test_images_excluded_for_training_similarity": [test[index]["image"] for index in sorted(set(excluded))],
        "within_test_potential_duplicate_pairs": within_test_pairs,
        "limitation": "Hash screening does not prove independent captures or detect every crop/near-duplicate. TRANSSET sessions are not released; within-test images are correlated, not independent trials.",
    }


def candidates() -> dict[str, Any]:
    result = {f"ridge_{alpha:g}": make_pipeline(StandardScaler(), Ridge(alpha=alpha)) for alpha in (10., 100., 1000.)}
    for leaf in (2, 5):
        result[f"extra_trees_leaf_{leaf}"] = ExtraTreesRegressor(n_estimators=128, max_depth=6, min_samples_leaf=leaf,
                                                               max_features=.5, n_jobs=2, random_state=RANDOM_SEED)
    return result


def metric(actual: np.ndarray, prediction: np.ndarray) -> dict[str, Any]:
    if not len(actual) or not np.isfinite(prediction).all():
        raise ValueError("Expected nonempty finite evaluation pairs")
    absolute = np.abs(prediction - actual)
    return {"samples": len(actual), "mae_vehicles_per_frame": float(absolute.mean()),
            "rmse_vehicles_per_frame": float(np.sqrt(np.mean((prediction - actual) ** 2))),
            "bias_vehicles_per_frame": float(np.mean(prediction - actual)),
            "actual_mean_vehicles_per_frame": float(actual.mean()),
            "predicted_mean_vehicles_per_frame": float(prediction.mean()),
            "rounded_exact_count_pct": float(100 * np.mean(np.rint(prediction) == actual)),
            "within_one_vehicle_pct": float(100 * np.mean(absolute <= 1))}


def run(root: Path, output: Path) -> dict[str, Any]:
    started = time.monotonic()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"Refusing to overwrite visual experiment: {output}")
    output.mkdir(parents=True, exist_ok=True)
    source_manifest = json.loads((root / "manifest.json").read_text())
    releases = source_manifest["datasets"]
    if len(releases) != 2 or any(row["data_license"]["spdx"] != "CC-BY-4.0" for row in releases):
        raise ValueError("Expected explicitly image-and-annotation licensed sources")
    visa_readme = (root / "visaitech_mixed_traffic/README.md").read_text()
    trans_readme = (root / "TRANSSET_README.md").read_text()
    if "CC BY 4.0" not in visa_readme or "CC BY 4.0" not in trans_readme:
        raise ValueError("Missing primary dataset license evidence")
    train, x_train, names = load_source(root, "visaitech_mixed_traffic")
    test, x_test_all, test_names = load_source(root, "transset")
    if names != test_names or len(train) != 48 or len(test) != 32:
        raise ValueError("Unexpected frozen source subset dimensions")
    groups, test_keep, duplicate_audit = grouping_and_duplicate_audit(train, test)
    write_json(output / "duplicate_audit.json", duplicate_audit)
    unique_groups = sorted(set(groups))
    if len(unique_groups) < 2 or not test_keep.any():
        raise ValueError("Too few independent validation groups or no untainted source-disjoint holdout")
    y_train = np.asarray([row["target_count"] for row in train], dtype=float)
    # Holdout labels cannot participate in selection, fitting, or normalization.
    all_names = [*candidates(), "constant_training_mean", "constant_training_median"]
    validation_predictions = {name: np.full(len(train), np.nan) for name in all_names}
    fold_records = []
    with threadpool_limits(limits=2):
        for group in unique_groups:
            fit = groups != group
            validation = ~fit
            estimators = candidates()
            for name, estimator in estimators.items():
                estimator.fit(x_train[fit], y_train[fit])
                validation_predictions[name][validation] = np.maximum(estimator.predict(x_train[validation]), 0)
            validation_predictions["constant_training_mean"][validation] = y_train[fit].mean()
            validation_predictions["constant_training_median"][validation] = np.median(y_train[fit])
            for name in all_names:
                fold_records.append({"held_out_group": str(group), "model": name,
                                     "fit_images": int(fit.sum()), "validation_images": int(validation.sum()),
                                     **metric(y_train[validation], validation_predictions[name][validation])})
    validation_scores = {}
    for name in all_names:
        if not np.isfinite(validation_predictions[name]).all():
            raise AssertionError("Incomplete source-video-group validation predictions")
        validation_scores[name] = {"macro_group_mae_vehicles_per_frame": float(np.mean([
            record["mae_vehicles_per_frame"] for record in fold_records if record["model"] == name])),
            "pooled": metric(y_train, validation_predictions[name])}
    model_names = list(candidates())
    selected_model = min(model_names, key=lambda name: (validation_scores[name]["macro_group_mae_vehicles_per_frame"], name))
    preferred_strategy = min(all_names, key=lambda name: (validation_scores[name]["macro_group_mae_vehicles_per_frame"], name))
    selection = {"selected_model": selected_model, "validation_preferred_strategy": preferred_strategy,
                 "selection_metric": "Mean MAE across whole source-video/duplicate-component held-out groups",
                 "hyperparameters_fixed_before_holdout": True, "holdout_labels_used_for_selection": False,
                 "validation_scores": validation_scores, "folds": fold_records}
    write_json(output / "selection_frozen_before_holdout.json", selection)
    estimator = candidates()[selected_model]
    with threadpool_limits(limits=2):
        estimator.fit(x_train, y_train)
    model = {"schema": SCHEMA, "estimator": estimator, "feature_names": names,
             "target": "static_image_vehicle_count", "production_eligible": False,
             "source_train": "visaitech/vehicle-mixed-traffic-detection", "selected_model": selected_model,
             "constant_training_mean": float(y_train.mean()), "constant_training_median": float(np.median(y_train)),
             "feature_extraction": "128x128 RGB, 4x4 fixed cells, color/gradient summaries and gray histogram; no filenames/labels/groups as features",
             "sklearn_version": sklearn.__version__, "numpy_version": np.__version__}
    joblib.dump(model, output / "model.joblib", compress=3)
    reloaded = joblib.load(output / "model.joblib")
    with threadpool_limits(limits=2):
        if not np.array_equal(estimator.predict(x_train[:8]), reloaded["estimator"].predict(x_train[:8])):
            raise AssertionError("Artifact prediction changed after reload")
    # Only now score the source-disjoint TRANSSET subset; no subsequent tuning.
    x_test = x_test_all[test_keep]
    retained_test = [row for row, keep in zip(test, test_keep) if keep]
    y_test = np.asarray([row["target_count"] for row in retained_test], dtype=float)
    with threadpool_limits(limits=2):
        predictions = np.maximum(estimator.predict(x_test), 0)
    baselines = {"constant_training_mean": np.full(len(y_test), y_train.mean()),
                 "constant_training_median": np.full(len(y_test), np.median(y_train))}
    test_metrics = metric(y_test, predictions)
    baseline_metrics = {name: metric(y_test, values) for name, values in baselines.items()}
    with (output / "predictions.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["image", "actual_vehicle_boxes", "predicted_count", *baselines])
        writer.writeheader()
        for index, row in enumerate(retained_test):
            writer.writerow({"image": row["image"], "actual_vehicle_boxes": int(y_test[index]),
                             "predicted_count": float(predictions[index]),
                             **{name: float(values[index]) for name, values in baselines.items()}})
    with (output / "validation.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["image", "source_video_group", "effective_validation_group", "actual_vehicle_boxes", *all_names])
        writer.writeheader()
        for index, row in enumerate(train):
            writer.writerow({"image": row["image"], "source_video_group": row["source_video_group"],
                             "effective_validation_group": str(groups[index]), "actual_vehicle_boxes": int(y_train[index]),
                             **{name: float(values[index]) for name, values in validation_predictions.items()}})
    np.savez_compressed(output / "pixel_features.npz", train=x_train, test=x_test_all, feature_names=np.asarray(names))
    write_json(output / "frame_manifest.json", {"training_frames": train, "holdout_frames": test,
               "holdout_kept": [bool(value) for value in test_keep],
               "notice": "File paths, source groups and annotations establish provenance/splits/targets only; none are pixel features"})
    improvement = {name: 100 * (values["mae_vehicles_per_frame"] - test_metrics["mae_vehicles_per_frame"]) / values["mae_vehicles_per_frame"]
                   if values["mae_vehicles_per_frame"] else None for name, values in baseline_metrics.items()}
    report = {"schema": SCHEMA, "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
              "training_performed": True, "target": "static_image_vehicle_count",
              "scope": "Non-Batam source-disjoint pixel-count transfer diagnostic",
              "production_eligible": False, "geography_is_batam": False, "transfer_benchmark_only": True,
              "selected_model": selected_model, "validation_preferred_strategy": preferred_strategy,
              "test_metrics": test_metrics, "baseline_metrics": baseline_metrics,
              "test_mae_improvement_pct_vs_baselines": improvement,
              "model_beats_both_constant_baselines": all(value > 0 for value in improvement.values()),
              "split": {"training_source": "Visaitech, South Asia (exact country/city undisclosed)",
                        "final_holdout_source": "TRANSSET, two North Carolina highway locations, USA",
                        "training_images": len(train), "training_vehicle_boxes": int(y_train.sum()),
                        "training_nonvehicle_boxes_excluded": sum(row["all_annotation_boxes"] - row["target_count"] for row in train),
                        "original_training_video_groups": sorted({row["source_video_group"] for row in train}),
                        "effective_validation_group_count": len(unique_groups),
                        "holdout_images_available": len(test), "holdout_images_scored": len(retained_test),
                        "holdout_vehicle_boxes_scored": int(y_test.sum()),
                        "source_disjoint": True, "random_frame_split": False,
                        "holdout_session_provenance": "Not released; filename ordinals are not timestamps. Frames may be correlated.",
                        "author_split_handling": "All Visaitech frames are development data; leave-whole-video/duplicate-component-out validation replaces the author split without crossing video groups"},
              "validation": selection,
              "features": {"count": len(names), "names": names, "fixed_before_holdout": True,
                           "pixel_only": True, "source_or_camera_ids_used_as_features": False,
                           "target_annotations_used_as_features": False,
                           "resize": "128x128 bilinear RGB feature input only; aspect ratio not preserved; no new image files",
                           "augmentation": False, "pretrained_weights": False,
                           "preprocessing": "Ridge StandardScaler fit separately within each training fold, then full Visaitech development only; ExtraTrees sees raw fixed features"},
              "artifact_reload_prediction_parity_passed": True,
              "sources": [{"native_id": row["native_id"], "publisher": row["publisher"], "url": row["url"],
                           "license": row["license"], "data_license": row["data_license"]} for row in releases],
              "source_manifest_sha256": digest(root / "manifest.json"),
              "limitations": ["80 images form a tiny development/transfer diagnostic, not a robust learned vehicle detector.",
                              "Global/cell pixel summaries can learn background, viewpoint, illumination or redaction rather than vehicles; there are no predicted bounding boxes.",
                              "South Asian dashcam and US aerial highway views differ sharply; taxonomy also shifts from wheeler groups including bicycles to eight car/truck classes.",
                              "Targets count supplied vehicle boxes, not newly audited true vehicle counts or unique vehicles across frames.",
                              "Original Visaitech source videos may themselves share a capture session; video-disjoint and hash checks do not prove independence.",
                              "TRANSSET session/video mapping is unavailable; no IID confidence intervals or independent-frame statistical significance is claimed.",
                              "No Batam, queue, line-crossing flow, speed, travel time, tracking or congestion labels are used or validated.",
                              "No holdout-driven tuning is permitted; this TRANSSET subset is now consumed for the fixed experiment.",
                              "No network calls, pretrained downloads, synthetic photos, crops or augmentation were used."],
              "environment": {"python": platform.python_version(), "numpy": np.__version__, "sklearn": sklearn.__version__,
                              "pillow": PILLOW_VERSION, "joblib": joblib.__version__, "random_seed": RANDOM_SEED,
                              "max_threads": 2, "wall_seconds": round(time.monotonic() - started, 3)}}
    write_json(output / "report.json", report)
    readme = f"""# Static image vehicle-count transfer diagnostic

Trained `{selected_model}` from pixel features of 48 licensed Visaitech frames,
then scored {len(retained_test)} source-disjoint TRANSSET aerial images.
The target is the count of original vehicle annotation boxes, excluding pedestrians.
This is **not a bounding-box detector or a Batam camera model**.

Holdout MAE: **{test_metrics['mae_vehicles_per_frame']:.3f} vehicles/frame**.
Constant training-mean MAE: {baseline_metrics['constant_training_mean']['mae_vehicles_per_frame']:.3f};
constant training-median MAE: {baseline_metrics['constant_training_median']['mae_vehicles_per_frame']:.3f}.
Model beats both baselines: **{report['model_beats_both_constant_baselines']}**.
Grouped validation preferred strategy: `{preferred_strategy}`.

Five fixed lightweight candidates are compared by leave-whole-source-video-group
out validation within Visaitech. Exact/perceptual similarity can merge related
video groups and excludes any test image resembling training. No random frame
split, pretrained model, additional photos, augmentation or network access is used.
Features are fixed RGB/gradient summaries, not annotation geometry or source IDs.
TRANSSET capture-session provenance is absent, so its frames are not independent
trials. Pixel counts can fail under viewpoint/source shift. Full limitations,
candidate metrics, source attribution and environment are in `report.json`.

Both source images and annotations are CC BY 4.0. Attribution: Visaitech,
Mixed-Traffic Vehicle Detection Dataset v0.1 (revision de35185262bda34c104a626f866bab9d9aaf67ed);
Gebre, T.; Blackston, Q.; Hashemi-Beni, L., TRANSSET v5,
doi:10.6084/m9.figshare.26082217.v5. Original source files remain unchanged.

`pixel_features.npz` contains feature arrays only, not new photos. The serialized
trusted local artifact remains research-only and requires the recorded environment.
Reproduce into a fresh directory with `python3 scripts/train_visual_counts.py --output-dir <new-path>`.
"""
    (output / "README.md").write_text(readme, encoding="utf-8")
    manifest = {"schema": SCHEMA, "training_performed": True, "production_eligible": False,
                "script_sha256": digest(Path(__file__).resolve()), "source_manifest_sha256": digest(root / "manifest.json"),
                "files": [{"path": path.name, "bytes": path.stat().st_size, "sha256": digest(path)}
                          for path in sorted(output.iterdir()) if path.is_file() and path.name != "manifest.json"]}
    write_json(output / "manifest.json", manifest)
    print(json.dumps({"status": "complete", "selected_model": selected_model, "test_metrics": test_metrics,
                      "baseline_metrics": baseline_metrics, "production_eligible": False}), flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    run(args.source_root.resolve(), args.output_dir.resolve())


if __name__ == "__main__":
    main()
