#!/usr/bin/env python3
"""Frozen, small, source-scoped Batam aggregate-count diagnostics.

These experiments predict published interval flow counts, not image objects,
queues, current traffic or citywide traffic. No executable model is serialized.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
DEFAULT_OUTPUT = ROOT / "backend/data/research/batam_counts_training"
SOURCE_SPEC = {
    "sekupang_15min": {
        "source_id": "BATAM-014",
        "csv": "research/batam-traffic/datasets/paper_observations/round3/observations.csv",
        "csv_sha256": "39a96d4fddba284b9106b84666589eb28ea8c65770e8305d779d1e4018b0d35c",
        "pdf_sha256": "97fbbc1da8acafabbaea5589a75937e92ca78e6a0b87b5a104401162a8d053c9",
        "rows": 216,
        "metric": "vehicle_count",
        "unit": "vehicles_in_15_minute_interval",
        "minutes": 15,
        "site_id": "batam-sekupang-four-approach-published-2023",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "license_evidence_url": "https://journal.uib.ac.id/index.php/jce/article/view/9076",
        "attribution": "Uswatun Azizatul Syarqiah and Yusra Aulia Sari (2024), Optimizing Traffic Management at Batam City Intersections: Analysis of Congestion Causes and Mitigation Strategies, Journal of Civil Engineering and Planning 5(1), DOI 10.37253/jcep.v5i1.9076. Table 4.1 individually transcribed; time offsets, provenance and diagnostics added. No author endorsement.",
        "features": ["prior_same_series_count", "approach", "movement", "vehicle_class"],
        "fit_groups": ["2023-12-17T06:45:00+07:00", "2023-12-17T07:00:00+07:00", "2023-12-17T07:15:00+07:00"],
        "validation_groups": ["2023-12-17T07:30:00+07:00"],
        "test_groups": ["2023-12-17T07:45:00+07:00"],
        "presets": ["persistence", "series_mean", "class_mean", "shrink_25", "shrink_50", "shrink_75", "global_ratio", "ridge_residual_1", "ridge_residual_10"],
    },
    "gajah_mada_uturn_hourly": {
        "source_id": "BATAM-EXT-005",
        "csv": "research/batam-traffic/datasets/paper_observations/observations.csv",
        "csv_sha256": "3dd9ac0b72739cea9a605118e938a98c12d13e452027c55a3c174b88290051ed",
        "pdf_sha256": "2e37e52e90ea8450359e3d5391e7a1626a304b85d3c8cf59808e1513d4eeb681",
        "rows": 72,
        "metric": "u_turn_vehicle_count",
        "unit": "vehicles_in_1_hour_interval",
        "minutes": 60,
        "site_id": "batam-gajah-mada-kemilau-bangsa-uturn-published-2023",
        "license": "CC BY-SA 4.0",
        "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "license_evidence_url": "https://www.journal.unrika.ac.id/index.php/sigmateknika/article/view/5642/0",
        "attribution": "Harry Kurniawan and M. Khairi Anwar (2023), Pengaruh U-Turn terhadap Kinerja Lalu Lintas pada Ruas Jalan Gajah Mada Depan Simpang Sekolah Kemilau Bangsa Kota Batam, Sigma Teknika 6(2), 479–492. Table 12, p488, raw class count cells retained including subtotal inconsistencies. Time offsets, provenance and diagnostics added. No author endorsement. Derived count artifacts are distributed under CC BY-SA 4.0.",
        "features": ["prior_survey_day_same_clock_series_count", "direction", "vehicle_class", "clock_slot"],
        "fit_groups": ["2023-02-16"],
        "validation_groups": ["2023-02-17"],
        "test_groups": ["2023-02-18"],
        "presets": ["persistence", "series_mean", "class_mean", "shrink_25", "shrink_50", "shrink_75"],
    },
}
BASELINES = ("persistence", "series_mean", "class_mean")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_sha(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def write_json(path, payload):
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_source(name, workspace=WORKSPACE):
    spec = SOURCE_SPEC[name]
    path = workspace / spec["csv"]
    if sha256(path) != spec["csv_sha256"]:
        raise ValueError("Source CSV checksum differs from the frozen review.")
    selected = [r for r in csv.DictReader(path.open())
                if r["source_record_id"] == spec["source_id"] and r["metric"] == spec["metric"]]
    if len(selected) != spec["rows"]:
        raise ValueError("Unexpected number of reviewed source count cells.")
    rows = []
    for raw in selected:
        if raw["source_sha256"] != spec["pdf_sha256"] or raw["unit"] != spec["unit"]:
            raise ValueError("Source identity or measurement unit mismatch.")
        pdf = Path(raw["source_file"])
        if not pdf.is_absolute():
            pdf = workspace / pdf
        if not pdf.is_file() or sha256(pdf) != spec["pdf_sha256"]:
            raise ValueError("Source PDF checksum mismatch.")
        start = datetime.fromisoformat(raw.get("interval_start_iso8601") or f"{raw['survey_date']}T{raw['time_start']}:00+07:00")
        end = datetime.fromisoformat(raw.get("interval_end_iso8601") or f"{raw['survey_date']}T{raw['time_end']}:00+07:00")
        value = float(raw["value"])
        if not math.isfinite(value) or value < 0 or not value.is_integer():
            raise ValueError("Expected finite, nonnegative published integer counts.")
        if end - start != timedelta(minutes=spec["minutes"]) or raw["measurement_kind"] != "observed_field_count":
            raise ValueError("Unexpected observed-count interval.")
        if name == "sekupang_15min":
            key = (raw["approach_text"], raw["movement"], raw["vehicle_class"])
            group = start.isoformat()
        else:
            key = (raw["direction_text"], raw["time_start"], raw["vehicle_class"])
            group = start.date().isoformat()
        rows.append({"id": raw["observation_id"], "source_id": raw["source_record_id"], "start": start,
                     "end": end, "value": value, "key": key, "class": raw["vehicle_class"], "group": group,
                     "quality_flags": raw["quality_flags"], "source_url": raw["source_url"],
                     "pdf_page": raw["pdf_page"], "table": raw["table"]})
    identities = [(r["start"], r["key"]) for r in rows]
    if len(identities) != len(set(identities)) or len({r["id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate source interval/series identity.")
    rows.sort(key=lambda r: (r["start"], r["key"]))
    expected_groups = 6 if name == "sekupang_15min" else 3
    expected_cells = 36 if name == "sekupang_15min" else 24
    if len(Counter(r["group"] for r in rows)) != expected_groups or set(Counter(r["group"] for r in rows).values()) != {expected_cells}:
        raise ValueError("The reviewed complete timestamp groups have changed.")
    return rows


def transitions(name, rows):
    """Return strict earlier same-series observations; never fill gaps."""
    by_key = defaultdict(list)
    for row in rows:
        by_key[row["key"]].append(row)
    pairs = []
    for group in by_key.values():
        group.sort(key=lambda r: r["start"])
        for previous, target in zip(group, group[1:]):
            expected_delta = timedelta(minutes=15) if name == "sekupang_15min" else timedelta(days=1)
            if target["start"] - previous["start"] != expected_delta:
                continue
            if previous["end"] > target["start"]:
                raise ValueError("A previous interval must finish by forecast origin.")
            pairs.append({"previous": previous, "target": target})
    return sorted(pairs, key=lambda p: (p["target"]["start"], p["target"]["key"]))


def metrics(actual, predicted):
    if not actual or len(actual) != len(predicted):
        raise ValueError("Metrics require paired, nonempty observations.")
    errors = np.asarray(predicted, dtype=float) - np.asarray(actual, dtype=float)
    if not np.all(np.isfinite(errors)):
        raise ValueError("Non-finite count predictions.")
    denominator = float(np.sum(actual))
    return {"cells": len(actual), "mae_vehicles_per_source_interval": float(np.mean(np.abs(errors))),
            "rmse_vehicles_per_source_interval": float(np.sqrt(np.mean(errors ** 2))),
            "wape_percent": 100 * float(np.sum(np.abs(errors))) / denominator if denominator else None,
            "actual_vehicles_in_evaluated_cells": denominator,
            "bias_vehicles_per_source_interval": float(np.mean(errors))}


def series_token(key):
    return json.dumps(key, ensure_ascii=False, separators=(",", ":"))


def feature_vector(previous_value, key, vocab):
    # Constant/calendar columns are deliberately absent in this one-session fit.
    return [previous_value, math.log1p(previous_value)] + [float(key[i] == value) for i, value in vocab]


def fit_model(name, preset, historical_rows, fit_pairs):
    series = defaultdict(list)
    classes = defaultdict(list)
    for r in historical_rows:
        series[series_token(r["key"])].append(r["value"])
        classes[r["class"]].append(r["value"])
    model = {"kind": preset, "series_means": {k: float(np.mean(v)) for k, v in series.items()},
             "class_means": {k: float(np.mean(v)) for k, v in classes.items()}}
    if preset == "global_ratio":
        denom = sum(p["previous"]["value"] for p in fit_pairs)
        model["ratio"] = sum(p["target"]["value"] for p in fit_pairs) / denom if denom else 1.0
    elif preset.startswith("ridge_residual_"):
        if not fit_pairs:
            raise ValueError("Ridge requires prior labeled transitions.")
        vocab = [(i, v) for i in range(3) for v in sorted({p["target"]["key"][i] for p in fit_pairs})]
        x = np.asarray([feature_vector(p["previous"]["value"], p["target"]["key"], vocab) for p in fit_pairs])
        y = np.asarray([p["target"]["value"] - p["previous"]["value"] for p in fit_pairs])
        mean = x.mean(axis=0)
        scale = x.std(axis=0)
        scale[scale == 0] = 1.0
        z = (x - mean) / scale
        intercept = float(y.mean())
        alpha = float(preset.rsplit("_", 1)[1])
        coef = np.linalg.solve(z.T @ z + alpha * np.eye(z.shape[1]), z.T @ (y - intercept))
        model.update({"vocab": vocab, "mean": mean.tolist(), "scale": scale.tolist(),
                      "coefficients": coef.tolist(), "intercept": intercept, "alpha": alpha})
    return model


def model_predict(model, previous_value, key):
    kind = model["kind"]
    if kind == "persistence":
        prediction = previous_value
    elif kind == "series_mean":
        prediction = model["series_means"][series_token(key)]
    elif kind == "class_mean":
        prediction = model["class_means"][key[-1]]
    elif kind.startswith("shrink_"):
        weight = int(kind.split("_")[1]) / 100
        prediction = (1 - weight) * previous_value + weight * model["series_means"][series_token(key)]
    elif kind == "global_ratio":
        prediction = previous_value * model["ratio"]
    elif kind.startswith("ridge_residual_"):
        x = feature_vector(previous_value, key, model["vocab"])
        residual = model["intercept"] + sum(c * (v - m) / s for c, v, m, s in zip(model["coefficients"], x, model["mean"], model["scale"], strict=True))
        prediction = previous_value + residual
    else:
        raise ValueError("Unknown numeric count-model kind.")
    if not math.isfinite(prediction):
        raise ValueError("Non-finite numeric model output.")
    return max(0.0, float(prediction))


def predict_scoped(payload, *, site_id, source_sha256, interval_start, key,
                   previous_count, previous_interval_end, previous_available_at, forecast_origin):
    """A deliberately restricted research inference contract, without app wiring."""
    if payload.get("format") != "crossflow_batam_published_count_numeric_v1":
        raise ValueError("Unsupported model artifact format.")
    if site_id != payload["site_id"] or source_sha256 != payload["source_pdf_sha256"]:
        raise ValueError("Count model site/source mismatch.")
    start = datetime.fromisoformat(interval_start)
    origin = datetime.fromisoformat(forecast_origin)
    end = datetime.fromisoformat(previous_interval_end)
    available = datetime.fromisoformat(previous_available_at)
    if any(t.tzinfo is None or t.utcoffset() is None for t in (start, origin, end, available)):
        raise ValueError("Explicit timezone offsets are required.")
    if start.isoformat() not in payload["allowed_test_intervals"] or origin != start:
        raise ValueError("Inference is limited to frozen retrospective target intervals.")
    key = tuple(key)
    if key not in [tuple(v) for v in payload["series_keys"]]:
        raise ValueError("Unknown source approach/movement/class/clock key.")
    expected = {r["target_interval_start"]: r for r in payload["expected_previous_intervals"]}[start.isoformat()]
    if end.isoformat() != expected["previous_interval_end"] or end > origin or available < end or available > origin:
        raise ValueError("Previous count must be complete and available by forecast origin.")
    if not isinstance(previous_count, (int, float)) or isinstance(previous_count, bool) or not math.isfinite(previous_count) or previous_count < 0:
        raise ValueError("Previous count must be finite and nonnegative.")
    return {"predicted_count": model_predict(payload["model"], float(previous_count), key),
            "unit": payload["unit"], "production_eligible": False,
            "scope": payload["scope"], "source_id": payload["source_id"]}


def protocol_payload():
    return {"schema_version": 1, "created_at": utc_now(), "client_date": "2026-10-24",
            "sources": SOURCE_SPEC, "selection": "minimum validation MAE, then RMSE, then preset declaration order",
            "test_used_for_tuning": False, "refit": "frozen winner fit with fit + validation targets; test targets never fitted",
            "temporal_observation_availability": "Retrospective idealized publication simulation: a count is assumed known at the end of its source interval. Original field or publication delivery latency is unavailable; no operational latency claim.",
            "primary_metric": "MAE in vehicles per original published source interval",
            "external_study_holdout": "Not pooled: intersection turning counts and U-turn hourly counts have different geometry, movements, classes and duration.",
            "repeat_policy": "Existing protocol/results cannot be overwritten by this script. Reusing a holdout does not provide independent evidence.",
            "production_eligible": False, "no_accuracy_percent_or_google_comparison": True}


def run_experiment(name, rows, protocol_sha256, freeze_selection=None):
    spec = SOURCE_SPEC[name]
    pairs = transitions(name, rows)
    fit_pairs = [p for p in pairs if p["target"]["group"] in spec["fit_groups"]]
    validation = [p for p in pairs if p["target"]["group"] in spec["validation_groups"]]
    test = [p for p in pairs if p["target"]["group"] in spec["test_groups"]]
    fit_history = [r for r in rows if r["group"] in spec["fit_groups"]]
    if name == "sekupang_15min":
        fit_history = [r for r in rows if r["end"] <= min(p["target"]["start"] for p in validation)]
    if not validation or not test or max(r["end"] for r in fit_history) > min(p["target"]["start"] for p in validation):
        raise ValueError("Chronological split/history contract failed.")
    validation_scores = []
    for index, preset in enumerate(spec["presets"]):
        model = fit_model(name, preset, fit_history, fit_pairs)
        predictions = [model_predict(model, p["previous"]["value"], p["target"]["key"]) for p in validation]
        validation_scores.append({"model": preset, "declaration_order": index,
                                  "metrics": metrics([p["target"]["value"] for p in validation], predictions)})
    winner = min(validation_scores, key=lambda s: (s["metrics"]["mae_vehicles_per_source_interval"], s["metrics"]["rmse_vehicles_per_source_interval"], s["declaration_order"]))["model"]
    final_history = [r for r in rows if r["end"] <= min(p["target"]["start"] for p in test)]
    final_pairs = fit_pairs + validation
    model = fit_model(name, winner, final_history, final_pairs)
    # Save a selection receipt BEFORE reading final test values into metrics.
    selection = {"winner": winner, "protocol_sha256": protocol_sha256, "validation_scores": validation_scores,
                 "final_model_sha256": canonical_sha(model), "frozen_before_test": True}
    if freeze_selection is not None:
        freeze_selection(selection)
    expected = sorted({(p["target"]["start"].isoformat(), p["previous"]["end"].isoformat()) for p in test})
    payload = {"schema_version": 1, "format": "crossflow_batam_published_count_numeric_v1",
               "experiment": name, "scope": "retrospective_published_single_site_count_diagnostic_only",
               "site_id": spec["site_id"], "source_id": spec["source_id"], "source_pdf_sha256": spec["pdf_sha256"],
               "source_csv_sha256": spec["csv_sha256"], "protocol_sha256": protocol_sha256,
               "unit": spec["unit"], "model_name": winner, "model": model,
               "series_keys": sorted({r["key"] for r in rows}),
               "allowed_test_intervals": sorted({p["target"]["start"].isoformat() for p in test}),
               "expected_previous_intervals": [{"target_interval_start": a, "previous_interval_end": b} for a, b in expected],
               "training_rows": len(final_history), "training_target_rows": len(final_pairs) if name == "sekupang_15min" else len(final_history),
               "source_license": spec["license"], "license_url": spec["license_url"], "attribution": spec["attribution"],
               "production_eligible": False}
    predicted = []
    for pair in test:
        p, t = pair["previous"], pair["target"]
        predicted.append(predict_scoped(payload, site_id=spec["site_id"], source_sha256=spec["pdf_sha256"],
                                       interval_start=t["start"].isoformat(), key=t["key"], previous_count=p["value"],
                                       previous_interval_end=p["end"].isoformat(), previous_available_at=p["end"].isoformat(),
                                       forecast_origin=t["start"].isoformat())["predicted_count"])
    baseline_predictions = {b: [model_predict(fit_model(name, b, final_history, final_pairs), p["previous"]["value"], p["target"]["key"]) for p in test] for b in BASELINES}
    actual = [p["target"]["value"] for p in test]
    baseline_metrics = {b: metrics(actual, values) for b, values in baseline_predictions.items()}
    test_metrics = metrics(actual, predicted)
    persistence_mae = baseline_metrics["persistence"]["mae_vehicles_per_source_interval"]
    by_class = {}
    for cls in sorted({p["target"]["class"] for p in test}):
        indexes = [i for i, p in enumerate(test) if p["target"]["class"] == cls]
        by_class[cls] = metrics([actual[i] for i in indexes], [predicted[i] for i in indexes])
    predictions = []
    for i, p in enumerate(test):
        target, previous = p["target"], p["previous"]
        predictions.append({"source_id": spec["source_id"], "observation_id": target["id"],
                            "interval_start": target["start"].isoformat(), "interval_end": target["end"].isoformat(),
                            "series_key_json": series_token(target["key"]), "vehicle_class": target["class"],
                            "prior_observation_id": previous["id"], "prior_interval_end": previous["end"].isoformat(),
                            "previous_count": previous["value"], "actual_count": actual[i], "selected_prediction": predicted[i],
                            **{b + "_prediction": baseline_predictions[b][i] for b in BASELINES},
                            "unit": spec["unit"], "source_quality_flags": target["quality_flags"]})
    split = {"candidate_fit": {"groups": spec["fit_groups"], "raw_history_cells": len(fit_history), "labeled_transitions": len(fit_pairs)},
             "validation": {"groups": spec["validation_groups"], "target_cells": len(validation)},
             "final_refit": {"raw_history_cells": len(final_history), "labeled_transitions": len(final_pairs)},
             "test": {"groups": spec["test_groups"], "target_cells": len(test)},
             "whole_source_timestamp_groups": True, "random_row_split": False}
    report = {"schema_version": 1, "experiment": name, "source_id": spec["source_id"], "site_id": spec["site_id"],
              "target": spec["metric"], "unit": spec["unit"], "training_performed": True, "selected_model": winner,
              "source_cells": len(rows), "unique_survey_dates": len({r["start"].date() for r in rows}),
              "test_metrics": test_metrics, "baseline_metrics": baseline_metrics, "per_class_test_metrics": by_class,
              "improvement_over_persistence_mae_percent": 100 * (1 - test_metrics["mae_vehicles_per_source_interval"] / persistence_mae) if persistence_mae else None,
              "selection": selection, "splits": split, "feature_contract": {"allowed": spec["features"],
                  "availability": "all count features from completed strictly prior source intervals, assumed available at interval end",
                  "excluded": ["target_count", "target_interval_other_class_counts", "target_PCU", "saturation", "density", "target_speed", "queue", "target_travel_time"]},
              "source_quality_flags": dict(Counter(r["quality_flags"] for r in rows)),
              "license": {k: spec[k] for k in ("license", "license_url", "license_evidence_url", "attribution")},
              "production_eligible": False, "serving_model_replaced": False, "application_history_modified": False,
              "synthetic_training_rows": 0, "fresh_prospective_evaluation": False,
              "limitations": ["Published aggregate cells were already collected and audited. The test is a retrospective diagnostic, not new prospective independent evidence.",
                  "Only one intersection/day (Sekupang) or one U-turn/three days (Gajah Mada); related class and movement cells share source timestamps.",
                  "Published individual cells are retained despite inconsistent later summaries. Original count sheets and arrival latencies are unavailable.",
                  "Counts are interval flows, not image vehicle counts, camera annotations, physical queues, road speeds or severity labels.",
                  "No cross-site, present-day, rainy-night, event or citywide validation; no operational 99–100% accuracy or comparison with Google is supported.",
                  "Test targets remain excluded from final refit; production eligibility remains false regardless of baseline performance."]}
    return report, payload, predictions, selection


def write_csv(path, rows):
    if not rows:
        raise ValueError("Cannot write empty results.")
    with Path(path).open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--prepare-only", action="store_true", help="Write frozen protocol before evaluating any final test.")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    protocol_path = output / "protocol.json"
    if args.prepare_only:
        if protocol_path.exists():
            raise SystemExit("Protocol already exists; refusing overwrite.")
        write_json(protocol_path, protocol_payload())
        print(json.dumps({"prepared": str(protocol_path), "sha256": sha256(protocol_path)}))
        return
    if not protocol_path.is_file():
        raise SystemExit("First run with --prepare-only to freeze protocol and source hashes.")
    frozen = json.loads(protocol_path.read_text())
    if frozen["sources"] != SOURCE_SPEC or frozen["test_used_for_tuning"] is not False:
        raise SystemExit("The frozen source/protocol contract has changed.")
    if (output / "report.json").exists():
        raise SystemExit("Results already exist; refusing test-driven repeated tuning or overwrite.")
    all_reports, all_sources = [], []
    for name in SOURCE_SPEC:
        directory = output / name
        directory.mkdir(exist_ok=False)
        rows = load_source(name)
        report, model, predictions, selection = run_experiment(
            name, rows, sha256(protocol_path),
            freeze_selection=lambda s: write_json(directory / "selection.json", s))
        write_json(directory / "model.json", model)
        write_json(directory / "report.json", report)
        write_csv(directory / "predictions.csv", predictions)
        write_csv(directory / "validation.csv", [{"model": r["model"], **r["metrics"]} for r in selection["validation_scores"]])
        write_csv(directory / "source_cells.csv", [{"source_id": r["source_id"], "observation_id": r["id"], "interval_start": r["start"].isoformat(),
                                                   "interval_end": r["end"].isoformat(), "series_key_json": series_token(r["key"]),
                                                   "count": r["value"], "unit": SOURCE_SPEC[name]["unit"], "quality_flags": r["quality_flags"],
                                                   "source_url": r["source_url"], "pdf_page": r["pdf_page"], "table": r["table"]} for r in rows])
        all_reports.append(report)
        all_sources.append({**SOURCE_SPEC[name], "new_to_this_count_experiment": len(rows),
                            "fitted_source_cells": report["splits"]["final_refit"]["raw_history_cells"],
                            "final_heldout_target_cells": report["splits"]["test"]["target_cells"],
                            "new_observations_acquired": 0})
    report = {"schema_version": 1, "generated_at": utc_now(), "client_date": "2026-10-24",
              "training_performed": True, "source_count": 2, "published_count_cells": sum(r["source_cells"] for r in all_reports),
              "newly_downloaded_count_observations": 0, "newly_used_for_count_experiments": sum(r["source_cells"] for r in all_reports),
              "fitted_source_cells": sum(r["splits"]["final_refit"]["raw_history_cells"] for r in all_reports),
              "final_heldout_target_cells": sum(r["splits"]["test"]["target_cells"] for r in all_reports),
              "experiments": all_reports, "production_eligible": False, "serving_model_replaced": False,
              "unified_accuracy_percent": None, "google_comparison_performed": False,
              "reason_no_unified_metric": "Different source interval durations, source scopes, movement geometries and count classes are not pooled."}
    write_json(output / "report.json", report)
    write_json(output / "sources.json", {"sources": all_sources,
        "excluded_sources": [{"source_id": "BATAM-EXT-010/BATAM-EXT-016/BATAM-EXT-018", "cells": 33,
                              "reason": "Source time/header/unit or peak-only issues; no compatible complete chronological series for this frozen experiment."},
                             {"source_id": "BATAM-013", "cells": 24, "reason": "Only four all-approach timestamps and a conflicting angkot total; not an equivalent approach/movement holdout."},
                             {"source_id": "BATAM-022", "cells": 6, "reason": "Only one hourly timestamp and direction percentage inconsistencies; no temporal transitions."},
                             {"source_id": "BATAM-EXT-001", "cells": 108, "reason": "Repository access does not establish an explicit open training/reuse license; narrative/date conflicts also remain."}],
        "license_checked_on_client_date": "2026-10-24", "batam014_primary_license_rechecked": True,
        "batam_ext005_license_basis": "Existing hash-verified research/speed_training source manifest and reviewed publisher page; separate CC BY-SA attribution retained."})
    files = [{"path": str(p.relative_to(output)), "bytes": p.stat().st_size, "sha256": sha256(p)}
             for p in sorted(output.rglob("*")) if p.is_file() and p.name not in {"manifest.json", "qa.json", "README.md"}]
    manifest = {"schema_version": 1, "training_performed": True, "protocol_sha256": sha256(protocol_path),
                "production_eligible": False, "source_count": 2, "published_count_cells": 288,
                "files": files, "reproduce": "CrossFlow_AI/.venv/bin/python CrossFlow_AI/scripts/train_batam_counts.py --prepare-only; then run without --prepare-only in a new output directory",
                "environment": {"python": sys.version, "numpy": np.__version__},
                "script_sha256": sha256(Path(__file__)), "source_inputs_unchanged": all(sha256(WORKSPACE / s["csv"]) == s["csv_sha256"] for s in SOURCE_SPEC.values())}
    write_json(output / "manifest.json", manifest)
    print(json.dumps({"published_count_cells": report["published_count_cells"], "experiments": [{"name": r["experiment"], "selected_model": r["selected_model"], "test_metrics": r["test_metrics"], "baseline_metrics": r["baseline_metrics"]} for r in all_reports], "production_eligible": False}))


if __name__ == "__main__":
    main()
