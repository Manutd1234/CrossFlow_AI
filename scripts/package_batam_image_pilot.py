#!/usr/bin/env python3
"""Create a deterministic research ZIP only after the integrated pilot QA passes.

Explicit input allowlists retain public source and annotation provenance, one
selected checkpoint, and reproducible training code. This command never trains,
opens network connections, changes app configuration, or copies private archives.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

REPO = Path(__file__).resolve().parents[1]
WORKSPACE = REPO.parent
DATASET = WORKSPACE / "research/batam-traffic/datasets/round10_batam_vehicle_images"
TRAINING = REPO / "backend/data/research/batam_detector_training"
DEFAULT_INTEGRATED_QA = TRAINING / "app_preview_qa.json"
BASE_COUNTER_SHA256 = "ff5a3934d085d96d3526d190aeb343873f3667d1abdd939bb022abccd777f4f7"
DESTINATION = WORKSPACE / "research/batam-image-counting-pilot.zip"
PREFIX = "batam-image-counting-pilot/"
ZIP_DATE = (2026, 10, 24, 0, 0, 0)
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pyc", ".tmp", ".pem", ".key"}
FORBIDDEN_PARTS = {".git", ".venv", "node_modules", "__pycache__"}
WEIGHT_SUFFIXES = {".pt", ".pth", ".bin", ".safetensors"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load(path: Path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()


def child(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("Expected a nonempty relative receipt path")
    raw = root / relative
    path = raw.resolve()
    if raw.is_symlink() or not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"Receipt input absent, symlinked, or outside its directory: {relative}")
    return path


def require_passed(path: Path) -> dict:
    result = load(path)
    if not isinstance(result, dict) or not (result.get("passed") is True or result.get("status") == "passed"):
        raise ValueError(f"Passed QA is required before packaging: {path}")
    if result.get("issues") or result.get("failures"):
        raise ValueError(f"QA still reports unresolved issues: {path}")
    return result


def verify_receipts(root: Path, manifest: dict) -> None:
    rows = manifest.get("files")
    if not isinstance(rows, list) or not rows:
        raise ValueError("A nonempty artifact receipt manifest is required")
    seen = set()
    for row in rows:
        path = child(root, row["path"])
        if path in seen:
            raise ValueError("Duplicate artifact manifest path")
        seen.add(path)
        if digest(path) != row["sha256"] or ("bytes" in row and path.stat().st_size != row["bytes"]):
            raise ValueError(f"Artifact receipt mismatch: {path}")


def collect_files(dataset_qa_path: Path, integrated_qa_path: Path, training_dir: Path):
    # These gates are intentionally checked before ZIP creation or output writes.
    dataset_qa = require_passed(dataset_qa_path)
    integrated_qa = require_passed(integrated_qa_path)
    training_qa = require_passed(training_dir / "qa.json")
    if digest(REPO / "backend/services/vehicle_counter.py") != BASE_COUNTER_SHA256:
        raise ValueError("The previously frozen base detector service changed")
    report = load(training_dir / "report.json")
    artifact_manifest = load(training_dir / "manifest.json")
    verify_receipts(training_dir, artifact_manifest)
    if report.get("production_eligible") is not False or report.get("human_certified") is not False:
        raise ValueError("This package must remain an explicitly uncertified experimental pilot")
    if report.get("google_comparison_performed") is not False:
        raise ValueError("No Google comparison is part of this research package")
    split_path, annotations_path = DATASET / "split_v2.json", DATASET / "annotation_manifest.json"
    split, annotations = load(split_path), load(annotations_path)
    split_sha, annotation_sha = digest(split_path), digest(annotations_path)
    if (integrated_qa.get("source_split_sha256") != split_sha
            or integrated_qa.get("report_sha256") != digest(training_dir / "report.json")):
        raise ValueError("Integrated QA refers to different source or training result bytes")
    if (integrated_qa.get("production_eligible") is not False
            or integrated_qa.get("batam_validated") is not False
            or integrated_qa.get("old_app_default_model_retained") is not True
            or integrated_qa.get("app_release_pointer_written") is not False
            or integrated_qa.get("model_promoted") is not False):
        raise ValueError("This failed pilot must retain the prior experimental app default")
    for record in (report, artifact_manifest):
        if record.get("split_sha256") != split_sha or record.get("annotation_manifest_sha256") != annotation_sha:
            raise ValueError("Training result does not bind the current frozen source/annotation cohort")
    if annotations.get("split_sha256") != split_sha or annotations.get("human_certified") is not False:
        raise ValueError("Annotation manifest differs from the disclosed pilot")
    if artifact_manifest.get("script_sha256") != digest(REPO / "scripts/train_batam_vehicle_detector.py"):
        raise ValueError("Frozen training script differs from the packaged source")
    model_path = training_dir / "model.pt"
    if artifact_manifest.get("model_sha256") != digest(model_path):
        raise ValueError("Selected checkpoint differs from the frozen manifest")
    freeze = load(training_dir / "selection_frozen_before_batam_test.json")
    if freeze.get("model_sha256") != digest(model_path) or freeze.get("test_labels_read_before_freeze") is not False:
        raise ValueError("Candidate selection was not frozen before test access")
    if digest(training_dir / "selection_frozen_before_reused_diagnostic.json") != digest(training_dir / "selection_frozen_before_batam_test.json"):
        raise ValueError("Service alias differs from the frozen Batam selection")
    if report.get("test_selection_sha256") != digest(training_dir / "selection_frozen_before_batam_test.json"):
        raise ValueError("Report refers to a different test selection")

    files, omissions = set(), []

    def add(raw: Path) -> None:
        raw = Path(raw)
        path = raw.resolve()
        if raw.is_symlink() or not path.is_relative_to(WORKSPACE.resolve()) or not path.is_file():
            raise ValueError(f"Unsafe or absent package input: {raw}")
        if (path.suffix.lower() in FORBIDDEN_SUFFIXES or path.name.startswith(".env")
                or FORBIDDEN_PARTS.intersection(path.parts) or path.name == ".DS_Store"):
            raise ValueError(f"Forbidden package input: {raw}")
        if path == REPO / "backend/main.py":
            raise ValueError("Do not package the full unrelated application entrypoint")
        files.add(path)

    for path in (dataset_qa_path, integrated_qa_path):
        add(path)
    source_images = set()
    identities, file_hashes, pixel_hashes, authors = set(), set(), set(), {}
    for row in split["images"]:
        path = child(DATASET, row["path"])
        if digest(path) != row["image_sha256"]:
            raise ValueError("Source image changed from the preinference split")
        if (row["image_id"] in identities or row["image_sha256"] in file_hashes
                or row["decoded_sha256"] in pixel_hashes):
            raise ValueError("Duplicate canonical capture/bytes/pixels in source cohort")
        identities.add(row["image_id"])
        file_hashes.add(row["image_sha256"])
        pixel_hashes.add(row["decoded_sha256"])
        if row["photographer"] in authors and authors[row["photographer"]] != row["split"]:
            raise ValueError("Photographer spans training and held-out source partitions")
        authors[row["photographer"]] = row["split"]
        if not row.get("license_url") or not row.get("source_url"):
            raise ValueError("Every canonical image needs source attribution and license evidence")
        source_images.add(path)
        add(path)
    if len(source_images) != 50:
        raise ValueError("Expected exactly 50 frozen source-candidate images")
    included_images, boxes = Counter(), Counter()
    excluded = []
    for partition, receipt in annotations["files"].items():
        path = child(DATASET, receipt["path"])
        if digest(path) != receipt["sha256"]:
            raise ValueError("Frozen annotation bytes changed")
        add(path)
        payload = load(path)
        rows = payload["images"] if isinstance(payload, dict) else payload
        for row in rows:
            if row.get("decision") == "exclude":
                if not row.get("exclusion_reason"):
                    raise ValueError("Exclusions need their preinference reason")
                excluded.append(row["image_id"])
            else:
                included_images[partition] += 1
                boxes[partition] += len(row["boxes_xyxy"])
    if dict(included_images) != report["included_image_counts"] or dict(boxes) != report["included_annotated_vehicle_boxes"]:
        raise ValueError("Reported image/box counts differ from frozen annotations")
    if (dict(included_images) != {"train": 9, "validation": 3, "test": 4}
            or dict(boxes) != {"train": 56, "validation": 22, "test": 18} or len(excluded) != 34):
        raise ValueError("Unexpected pilot cohort; update documentation with a separately reviewed version")
    policy = split["split_policy"]
    if digest(child(DATASET, policy["path"])) != policy["sha256"]:
        raise ValueError("Preannotation author split policy changed")
    for receipt in annotations.get("review_inputs", []):
        if digest(child(DATASET, receipt["path"])) != receipt["sha256"]:
            raise ValueError("An original annotation review changed")

    # Public acquisition metadata/evidence, all review provenance and derived
    # inspection images remain explicit; unrelated raw candidates are omitted.
    for path in sorted(DATASET.rglob("*")):
        if not path.is_file() or FORBIDDEN_PARTS.intersection(path.parts) or path.name == ".DS_Store":
            continue
        suffix = path.suffix.lower()
        if suffix in {".json", ".jsonl", ".md", ".py", ".html", ".txt", ".csv"}:
            add(path)
        elif suffix in IMAGE_SUFFIXES and path.resolve() not in source_images:
            relative = path.relative_to(DATASET).as_posix()
            if "inspection" in relative or "contact_sheet" in relative or relative.endswith("first_batch_sheet.jpg") or relative == "annotation_example.png":
                add(path)
            else:
                omissions.append({"path": path.relative_to(WORKSPACE).as_posix(),
                                  "reason": "Raw discovery candidate or contact derivative is outside the frozen 50-source cohort; not an accepted extra traffic photo."})

    # Preserve every training receipt/report, but only the one selected checkpoint.
    for path in sorted(training_dir.rglob("*")):
        if not path.is_file() or FORBIDDEN_PARTS.intersection(path.parts) or path.name == ".DS_Store":
            continue
        if path.suffix.lower() in WEIGHT_SUFFIXES and path.resolve() != model_path.resolve():
            omissions.append({"path": path.relative_to(WORKSPACE).as_posix(),
                              "reason": "Trial checkpoint omitted; its frozen hash and metadata remain, and one selected model.pt is included."})
        elif path.suffix.lower() in {".json", ".jsonl", ".md", ".txt", ".csv", ".log"} or path.resolve() == model_path.resolve():
            add(path)
    for name in ("train_batam_vehicle_detector.py", "package_batam_image_pilot.py", "requirements-detector-training.txt", "vehicle_counter_worker.py", "verify_batam_preview.py"):
        add(REPO / "scripts" / name)
    for name in ("vehicle_counter.py", "batam_vehicle_counter.py", "vehicle_count_preview.py"):
        add(REPO / "backend/services" / name)
    for name in ("test_batam_vehicle_detector_training.py", "test_vehicle_counter.py", "test_batam_vehicle_counter.py"):
        add(REPO / "backend/tests" / name)
    add(REPO / "docs/ML_BATAM_IMAGE_PILOT_2026-10-24.md")
    for name in ("ml_training_progress.json", "ML_TRAINING_PROGRESS.md", "training_readiness.json", "collection_progress.json"):
        add(WORKSPACE / "research/batam-traffic" / name)
    for name in ("components/operations/VehicleCountPreview.tsx", "components/operations/VehicleCountPreview.css",
                 "components/operations/VehicleCountPreview.test.tsx", "services/vehicleCounting.ts", "services/vehicleCounting.test.ts"):
        add(REPO / "frontend/src" / name)
    release = REPO / "backend/data/research/vehicle_counter_preview_release.json"
    if release.is_file():
        release_data = load(release)
        if release_data.get("production_eligible") is not False:
            raise ValueError("Optional release receipt must remain an experimental preview")
        add(release)
    selected_weights = [path for path in files if path.suffix.lower() in WEIGHT_SUFFIXES]
    if selected_weights != [model_path.resolve()]:
        raise ValueError("Exactly one selected checkpoint must be bundled")
    context = {
        "report": report, "split_sha256": split_sha, "annotation_manifest_sha256": annotation_sha,
        "dataset_qa": dataset_qa, "training_qa": training_qa, "integrated_qa": integrated_qa,
        "source_candidate_images": 50, "included_images": dict(included_images),
        "included_boxes": dict(boxes), "preinference_excluded_images": len(excluded),
        "newly_downloaded_batam_photos": 3, "new_native_metadata_ids": 2,
        "prior_metadata_only_new_pixels": 1, "new_photos_used_for_training": 2,
        "new_photo_vehicle_boxes_used_for_training": 7,
    }
    return sorted(files), omissions, context


def zip_info(relative: str) -> zipfile.ZipInfo:
    item = zipfile.ZipInfo(PREFIX + relative, ZIP_DATE)
    item.create_system = 3
    item.compress_type = zipfile.ZIP_DEFLATED
    item.external_attr = 0o100644 << 16
    return item


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-qa", type=Path, default=DATASET / "dataset_qa.json")
    parser.add_argument("--integrated-qa", type=Path, default=DEFAULT_INTEGRATED_QA)
    parser.add_argument("--training-dir", type=Path, default=TRAINING)
    parser.add_argument("--destination", type=Path, default=DESTINATION)
    parser.add_argument("--verify-only", action="store_true", help="Validate all gates and receipts without creating a ZIP")
    args = parser.parse_args()
    files, omissions, context = collect_files(args.dataset_qa, args.integrated_qa, args.training_dir)
    members = {path.relative_to(WORKSPACE).as_posix(): {"sha256": digest(path), "bytes": path.stat().st_size} for path in files}
    start = (
        "Batam vehicle-image counting pilot, 24 October 2026.\n"
        "Start with CrossFlow_AI/docs/ML_BATAM_IMAGE_PILOT_2026-10-24.md.\n"
        "50 frozen source-candidate photographs were reviewed; 16 included photos have 96 blind assistant-authored visible-vehicle boxes. "
        "Training/validation/test are 9/3/4 photos and 56/22/18 boxes. 34 candidates were excluded before inference.\n"
        "Three new Batam photographs were downloaded: two new native IDs and one prior metadata-only record. "
        "Two new photos with seven boxes entered training. Larger versions, derivatives and reused sources are not new captures.\n"
        "Fine-tuned heads did not win validation. Selection retained the unchanged pretrained COCO detector at score 0.4. "
        "The four-photo Batam test had MAE 3.0 vehicles/image and 0/4 exact counts, identical to the fixed prior-detector reference. "
        "No app accuracy improvement or model promotion is claimed; the previous app model remains the default.\n"
        "Photographers remain disjoint across partitions. Test labels were opened only after model/threshold selection froze. "
        "The four-photo test is now consumed; it cannot be used for further tuning or a fresh accuracy claim.\n"
        "Annotations are AI visual reviews, not independently human-certified ground truth. No 99-100%, Google superiority, "
        "citywide traffic, flow, speed or queue accuracy is established. The model remains experimental.\n"
        "This focused update is not a standalone app. It includes one selected model and metadata for omitted trials, "
        "source evidence, annotation provenance and training/serving helpers. No credentials, database, private provider rows, "
        "full modified backend entrypoint or unrelated app files are included.\n"
        "Per-image CC BY and CC BY-SA attribution/license obligations remain. COCO-derived weight production rights are not certified.\n"
        "FILES.sha256 covers every other archived member. The external archive_info.json records the complete member hashes including FILES.sha256.\n"
    ).encode()
    generated = {"START_HERE.txt": start}
    package = {
        "schema": "crossflow.batam_vehicle_pilot_package.v1",
        "source_report_generated_at": context["report"].get("generated_at"),
        "split_sha256": context["split_sha256"], "annotation_manifest_sha256": context["annotation_manifest_sha256"],
        "canonical_source_images": context["source_candidate_images"], "included_images": context["included_images"],
        "included_vehicle_boxes": context["included_boxes"], "preinference_excluded_images": context["preinference_excluded_images"],
        "newly_downloaded_batam_photos": 3, "new_native_metadata_ids": 2, "prior_metadata_only_new_pixels": 1,
        "new_photos_used_for_training": 2, "new_photo_vehicle_boxes_used_for_training": 7,
        "selected_model": context["report"].get("selected_model"), "model_sha256": digest(args.training_dir / "model.pt"),
        "selected_model_is_finetuned": context["report"].get("selected_model_is_finetuned"),
        "batam_test_metrics": context["report"].get("batam_group_holdout_pilot"),
        "fixed_reference_metrics": context["report"].get("existing_app_fixed_threshold_reference"),
        "app_model_promoted": context["report"].get("application_model_promoted"),
        "human_certified": False, "production_eligible": False, "google_comparison_performed": False,
        "trial_checkpoints_included": 0, "selected_checkpoints_included": 1,
        "prior_frozen_base_counter_sha256": BASE_COUNTER_SHA256,
        "private_provider_rows_included": False, "app_activation_performed_by_packager": False,
        "omissions": omissions, "payload_files": members,
    }
    generated["PACKAGE.json"] = json_bytes(package)
    expected = {name: row["sha256"] for name, row in members.items()}
    expected.update({name: hashlib.sha256(data).hexdigest() for name, data in generated.items()})
    generated["FILES.sha256"] = "".join(f"{sha}  {name}\n" for name, sha in sorted(expected.items())).encode()
    expected["FILES.sha256"] = hashlib.sha256(generated["FILES.sha256"]).hexdigest()
    if args.verify_only:
        print(json.dumps({"passed": True, "zip_created": False, "payload_files": len(files), "total_members": len(expected),
                          "canonical_source_images": 50, "selected_checkpoints": 1}, indent=2))
        return
    destination = args.destination.resolve()
    if not destination.is_relative_to((WORKSPACE / "research").resolve()) or destination.suffix != ".zip":
        raise ValueError("Write the pilot ZIP inside this workspace research folder")
    temporary = destination.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            for path in files:
                relative = path.relative_to(WORKSPACE).as_posix()
                with path.open("rb") as source, archive.open(zip_info(relative), "w", force_zip64=True) as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
            for name in sorted(generated):
                archive.writestr(zip_info(name), generated[name])
        with zipfile.ZipFile(temporary) as archive:
            if len(archive.namelist()) != len(expected) or len(set(archive.namelist())) != len(expected):
                raise ValueError("Archive member count/uniqueness mismatch")
            corrupt = archive.testzip()
            if corrupt is not None:
                raise ValueError(f"Archive CRC failed: {corrupt}")
            for relative, checksum in expected.items():
                h = hashlib.sha256()
                with archive.open(PREFIX + relative) as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        h.update(chunk)
                if h.hexdigest() != checksum:
                    raise ValueError(f"Archive member SHA256 failed: {relative}")
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    checksum = digest(destination)
    info = {
        "schema": "crossflow.batam_vehicle_pilot_archive_receipt.v1", "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "path": destination.name, "bytes": destination.stat().st_size, "sha256": checksum, "members": len(expected),
        "crc_and_every_member_sha256": "passed", "member_sha256": expected,
        "canonical_source_images": 50, "included_images": context["included_images"], "annotated_vehicle_boxes": context["included_boxes"],
        "selected_checkpoints": 1, "trial_checkpoints": 0, "private_provider_rows_included": False,
        "human_certified": False, "production_eligible": False,
    }
    destination.with_suffix(".zip.sha256").write_text(checksum + "  " + destination.name + "\n")
    destination.with_suffix(".archive_info.json").write_bytes(json_bytes(info))
    print(json.dumps({key: value for key, value in info.items() if key != "member_sha256"}, indent=2))


if __name__ == "__main__":
    main()
