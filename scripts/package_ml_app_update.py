#!/usr/bin/env python3
"""Package the additive experimental ML preview after integrated QA passes.

Explicit allowlists exclude databases, credentials, unrelated working changes,
trial checkpoint duplicates and inspection-only thesis content. No training,
runtime installation, app activation, network request or source mutation occurs.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import zipfile

REPO = Path(__file__).resolve().parents[1]
WORKSPACE = REPO.parent
DATA = REPO / "backend/data/research"
READINESS = DATA / "ml_app_readiness"
SOURCE_CHECKS = WORKSPACE / "research/batam-traffic/datasets/round9_training_sources"
VISION = WORKSPACE / "research/batam-traffic/datasets/round4_open/vision"
PREFIX = "batam-ml-app-update/"
ZIP_DATE = (2026, 10, 24, 0, 0, 0)
TRIALS = {"ssdlite_trial", "fasterrcnn_trial"}
EXCLUDED_SOURCE_INSPECTION = {
    "evidence/polibatam_tiban_gate_bundles_original_files_thesis.pdf",
    "evidence/polibatam_tiban_gate_bundles_original_files_thesis.txt",
}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write_json(path, payload):
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def verify_manifest(folder, manifest):
    for row in manifest["files"]:
        file = (folder / row["path"]).resolve()
        if not file.is_relative_to(folder.resolve()) or not file.is_file():
            raise ValueError(f"Manifest file absent or outside its directory: {row['path']}")
        if digest(file) != row["sha256"] or ("bytes" in row and file.stat().st_size != row["bytes"]):
            raise ValueError(f"Manifest checksum mismatch: {file}")


def collect_files():
    files, omissions = set(), []

    def add(path):
        file = Path(path).resolve()
        if not file.is_relative_to(WORKSPACE.resolve()) or not file.is_file() or file.is_symlink():
            raise ValueError(f"Missing or unsupported package input: {path}")
        if file.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".pyc", ".tmp"} or file.name.startswith(".env") or "__pycache__" in file.parts:
            raise ValueError(f"Forbidden package input: {path}")
        if file.name == "main.py" and file.parent == REPO / "backend":
            raise ValueError("The full modified application entrypoint must not be packaged.")
        files.add(file)

    for name in ["train_batam_counts.py", "train_vehicle_detector.py", "train_vehicle_detector_strong.py",
                 "requirements-detector-training.txt", "vehicle_counter_worker.py", "package_ml_app_update.py"]:
        add(REPO / "scripts" / name)
    for name in ["vehicle_counter.py", "vehicle_count_preview.py"]:
        add(REPO / "backend/services" / name)
    for name in ["test_batam_count_training.py", "test_vehicle_counter.py", "test_vehicle_count_preview_api.py", "test_vehicle_count_preview_bridge.py"]:
        add(REPO / "backend/tests" / name)
    for name in ["components/operations/VehicleCountPreview.tsx", "components/operations/VehicleCountPreview.css",
                 "components/operations/VehicleCountPreview.test.tsx", "services/vehicleCounting.ts", "services/vehicleCounting.test.ts"]:
        add(REPO / "frontend/src" / name)
    add(REPO / "docs/ML_APP_READINESS_2026-10-24.md")
    for name in ["ML_TRAINING_PROGRESS.md", "ml_training_progress.json", "training_readiness.json", "collection_progress.json"]:
        add(WORKSPACE / "research/batam-traffic" / name)
    for name in ["preview_qa.json", "test_results.json", "independent_detector_audit.json", "vehicle-count-preview.png"]:
        add(REPO / "docs/verification/ml-app-2026-10-24" / name)
    add(READINESS / "status.json")

    counts = DATA / "batam_counts_training"
    verify_manifest(counts, read(counts / "manifest.json"))
    if read(counts / "qa.json")["passed"] is not True:
        raise ValueError("Count artifact QA must pass before packaging.")
    for p in sorted(counts.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix not in {".pyc", ".tmp"}:
            add(p)
    for source in read(counts / "sources.json")["sources"]:
        # Only the licensed papers and the 288-cell normalized source subsets.
        # The original composite observation CSVs contain unrelated sources.
        with (WORKSPACE / source["csv"]).open() as handle:
            row = next(r for r in csv.DictReader(handle) if r["source_record_id"] == source["source_id"] and r["metric"] == source["metric"])
        pdf = Path(row["source_file"])
        if not pdf.is_absolute():
            pdf = WORKSPACE / pdf
        if digest(pdf) != source["pdf_sha256"]:
            raise ValueError("Licensed Batam source PDF checksum mismatch.")
        add(pdf)

    detector = DATA / "detector_training"
    dm = read(detector / "manifest.json")
    verify_manifest(detector, dm)
    if read(detector / "deployment_qa.json")["passed"] is not True or dm["production_eligible"] is not False:
        raise ValueError("Expected passed deployment-integrity checks and an experimental detector.")
    for row in dm["code_files"]:
        if digest(REPO / row["path"]) != row["sha256"]:
            raise ValueError("A frozen deployment code file changed.")
    for p in sorted(detector.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or p.suffix in {".pyc", ".tmp"}:
            continue
        relative = p.relative_to(detector)
        if relative.parts[0] in TRIALS and p.suffix.lower() in {".pt", ".pth", ".bin", ".jpg", ".png", ".webp"}:
            omissions.append({"path": p.relative_to(WORKSPACE).as_posix(), "reason": "Historical trial checkpoint/image omitted; metadata retained and one stable selected checkpoint included."})
        else:
            add(p)

    add(VISION / "manifest.json")
    for folder in ["visaitech_mixed_traffic", "transset"]:
        for name in ["README.md", "files_manifest.json", "frame_counts.jsonl"]:
            if (VISION / folder / name).is_file():
                add(VISION / folder / name)
    for name in ["README.md", "TRANSSET_README.md", "cross_format_audit.json"]:
        if (VISION / name).is_file():
            add(VISION / name)
    image_ids = set()
    for name in ["development_frame_manifest.json", "diagnostic_frame_manifest.json"]:
        for row in read(detector / name):
            for key in ["image", "label"]:
                path = VISION / row[key]
                if digest(path) != row[key + "_sha256"]:
                    raise ValueError("Foreign licensed image/label checksum mismatch.")
                add(path)
            image_ids.add(row["image"])
    if len(image_ids) != 80:
        raise ValueError("Expected 48 development and 32 diagnostic images.")

    source_manifest = read(SOURCE_CHECKS / "manifest.json")
    if read(SOURCE_CHECKS / "qa.json")["status"] != "passed":
        raise ValueError("Round9 source-check QA must pass.")
    for p in sorted(SOURCE_CHECKS.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or p.suffix in {".pyc", ".tmp"}:
            continue
        relative = p.relative_to(SOURCE_CHECKS).as_posix()
        if relative in EXCLUDED_SOURCE_INSPECTION:
            omissions.append({"path": p.relative_to(WORKSPACE).as_posix(), "reason": "Inspection-only thesis is not a licensed training source; PDF and full extracted text omitted."})
        else:
            add(p)
    for row in source_manifest["source_files"]:
        file = SOURCE_CHECKS / row["path"]
        if digest(file) != row["sha256"]:
            raise ValueError("Round9 source evidence checksum mismatch.")
    return sorted(files), omissions


REPLAY_COUNTS = r'''"""Refit and check frozen counts from licensed subsets; no new accuracy evidence."""
from datetime import datetime
import csv,importlib.util,json,math
from pathlib import Path
root=Path(__file__).resolve().parent
script=root/'CrossFlow_AI/scripts/train_batam_counts.py'
spec=importlib.util.spec_from_file_location('counts_replay',script)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
folder=root/'CrossFlow_AI/backend/data/research/batam_counts_training'
checked=[]
for name in module.SOURCE_SPEC:
    directory=folder/name;rows=[]
    for r in csv.DictReader((directory/'source_cells.csv').open()):
        start=datetime.fromisoformat(r['interval_start']);key=tuple(json.loads(r['series_key_json']))
        rows.append({'id':r['observation_id'],'source_id':r['source_id'],'start':start,
            'end':datetime.fromisoformat(r['interval_end']),'value':float(r['count']),'key':key,
            'class':key[-1],'group':start.isoformat() if name=='sekupang_15min' else start.date().isoformat(),
            'quality_flags':r['quality_flags'],'source_url':r['source_url'],'pdf_page':r['pdf_page'],'table':r['table']})
    expected=json.loads((directory/'model.json').read_text())
    report,model,predictions,selection=module.run_experiment(name,rows,module.sha256(folder/'protocol.json'))
    assert json.loads(json.dumps(model))==expected, name+': frozen model mismatch'
    saved=json.loads((directory/'report.json').read_text())
    assert report['test_metrics']==saved['test_metrics'],name+': metric mismatch'
    assert selection==json.loads((directory/'selection.json').read_text()),name+': selection mismatch'
    saved_predictions=list(csv.DictReader((directory/'predictions.csv').open()))
    assert len(predictions)==len(saved_predictions)
    for actual,wanted in zip(predictions,saved_predictions,strict=True):
        assert actual['observation_id']==wanted['observation_id']
        assert math.isclose(actual['selected_prediction'],float(wanted['selected_prediction']),abs_tol=1e-12)
    checked.append({'experiment':name,'source_cells':len(rows),'verified_predictions':len(predictions)})
print(json.dumps({'status':'passed','checks':checked,'fresh_independent_accuracy_evidence':False},indent=2))
'''


def integration_excerpt():
    api = (REPO / "backend/main.py").read_text()
    start = api.index('@app.get("/api/analytics/vehicle-count-preview/status")')
    end = api.index("@app.post(OBSERVED_ANALYTICS_BACKTEST_PATH)", start)
    panel = (REPO / "frontend/src/components/operations/AIModelPanel.tsx").read_text()
    jsx = next(line.strip() for line in panel.splitlines() if "<VehicleCountPreview " in line)
    return ("Additive code already present in this local checkout. Preserve unrelated application changes.\n"
            "This is an update for the existing app: identity.require_route_operator, envelope, clock, and\n"
            "_ROUTE_NO_STORE_HEADERS are existing application dependencies. Request/Response/Header imports\n"
            "must be available from FastAPI; do not replace backend/main.py wholesale.\n\n"
            + api[start:end]
            + "\nFrontend AIModelPanel.tsx import:\nimport { VehicleCountPreview } from './VehicleCountPreview';\n\n"
            + "Insert within the authorized model panel, forwarding the existing accessToken:\n" + jsx + "\n")


def zip_info(name):
    info = zipfile.ZipInfo(PREFIX + name, ZIP_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-only", action="store_true", help="Inspect allowlisted files and refresh pending manifest without packaging.")
    args = parser.parse_args()
    status = read(READINESS / "status.json")
    if not args.manifest_only and status["verification"]["status"] != "passed":
        raise SystemExit("Integrated verification is pending; no ZIP was written.")
    if status["production_eligible"] is not False or status["google_comparison_performed"] is not False:
        raise ValueError("Experimental scope must remain explicit.")
    detector = read(DATA / "detector_training/manifest.json")
    if (status["vehicle_detector"]["model_sha256"] != detector["model_sha256"]
            or status["vehicle_detector"]["selected_model"] != detector["selected_model"]):
        raise ValueError("Readiness status differs from the selected detector manifest.")
    count_report = read(DATA / "batam_counts_training/report.json")
    if (status["count_experiments"]["final_fitting_history_cells"] != count_report["fitted_source_cells"]
            or status["count_experiments"]["final_heldout_target_cells"] != count_report["final_heldout_target_cells"]):
        raise ValueError("Readiness count allocation differs from the frozen experiment report.")
    files, omissions = collect_files()
    file_rows = [{"path": p.relative_to(WORKSPACE).as_posix(), "bytes": p.stat().st_size, "sha256": digest(p)} for p in files]
    manifest = {"schema_version": 1, "client_date": "2026-10-24", "updated_at": status["updated_at"],
                "status": status["verification"]["status"], "package_scope": "additive_experimental_ml_app_update",
                "files": file_rows, "intentionally_omitted_files": omissions,
                "public_licensed_foreign_images": 80, "public_batam_count_subset_cells": 288,
                "new_batam_vehicle_annotations": 0, "production_eligible": False,
                "private_provider_rows_included": False, "databases_or_credentials_included": False,
                "trial_checkpoint_payloads_complete": False, "source_check_evidence_complete": False,
                "source_check_evidence_note": "Original audit manifests are preserved; inspection-only thesis PDF and full extracted text are excluded from this update.",
                "old_frozen_training_reports_models_and_zips_overwritten": False}
    write_json(READINESS / "manifest.json", manifest)
    if args.manifest_only:
        print(json.dumps({"status": manifest["status"], "files": len(files), "bytes": sum(r["bytes"] for r in file_rows), "zip_written": False}))
        return
    files.append(READINESS / "manifest.json")
    expected = {p.relative_to(WORKSPACE).as_posix(): digest(p) for p in files}
    notes = {
        "START_HERE.txt": (
            "Experimental ML app update — 24 October 2026\n"
            "Read CrossFlow_AI/docs/ML_APP_READINESS_2026-10-24.md.\n"
            "One operator-requested static image can be counted with boxes; no image retention or traffic writes.\n"
            "Authenticated app mode requires the existing operator/admin bearer policy; explicit disabled/demo mode retains existing guest access.\n"
            "Detector accuracy is unvalidated in Batam. No 99–100% or Google-comparison claim is supported.\n"
            "288 published Batam count experiment cells = 228 fitting/history + 60 holdout; none are newly acquired observations.\n"
            "Foreign detector fine-tuning occurred, but validation chose the stronger pretrained checkpoint.\n"
            "This is an additive update to an existing checkout, not a standalone app or wholesale file replacement.\n"
            "Use APP_INTEGRATION.txt for the new routes/panel insertion without copying the entire modified app.\n"
            "Create persistent CrossFlow_AI/.venv-vision using scripts/requirements-detector-training.txt as documented.\n"
            "REPLAY_COUNTS.py needs Python/NumPy and verifies count refitting from the licensed subsets.\n"
            "Trial checkpoint duplicates and inspection-only thesis PDF/text are intentionally omitted; see readiness manifest.\n"
            "No credentials, private provider data, databases or user-uploaded images are included.\n"
            "All source-specific licenses and attribution remain attached; this is not a blanket license grant.\n"
        ),
        "APP_INTEGRATION.txt": integration_excerpt(),
        "REPLAY_COUNTS.py": REPLAY_COUNTS,
    }
    expected.update({name: hashlib.sha256(value.encode()).hexdigest() for name, value in notes.items()})
    notes["FILES.sha256"] = "".join(f"{sha}  {name}\n" for name, sha in sorted(expected.items()))
    expected["FILES.sha256"] = hashlib.sha256(notes["FILES.sha256"].encode()).hexdigest()
    destination = WORKSPACE / "research/batam-ml-app-update.zip"
    temporary = destination.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for path in sorted(files):
            with path.open("rb") as source, archive.open(zip_info(path.relative_to(WORKSPACE).as_posix()), "w", force_zip64=True) as member:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    member.write(chunk)
        for name, text in sorted(notes.items()):
            archive.writestr(zip_info(name), text)
    with zipfile.ZipFile(temporary) as archive:
        if len(archive.namelist()) != len(expected) or archive.testzip() is not None:
            raise ValueError("ZIP member count or CRC verification failed.")
        for name, wanted in expected.items():
            h = hashlib.sha256()
            with archive.open(PREFIX + name) as member:
                for chunk in iter(lambda: member.read(1024 * 1024), b""):
                    h.update(chunk)
            if h.hexdigest() != wanted:
                raise ValueError(f"ZIP member SHA-256 mismatch: {name}")
    temporary.replace(destination)
    sha = digest(destination)
    info = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "client_date": "2026-10-24",
            "path": destination.name, "bytes": destination.stat().st_size, "sha256": sha,
            "files": len(expected), "crc_and_all_member_sha256": "passed", "production_eligible": False,
            "private_provider_rows_included": False, "user_images_or_credentials_included": False,
            "old_training_zip_preserved": True, "verification": status["verification"]}
    destination.with_suffix(".zip.sha256").write_text(sha + "  " + destination.name + "\n")
    write_json(destination.with_suffix(".archive_info.json"), info)
    print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
