#!/usr/bin/env python3
"""Train a bounded retrospective single-site Batam speed diagnostic.

Day 1 fits fixed candidates, day 2 selects by MAE/RMSE, then the frozen winner
is refit on days 1+2 and scored once on day 3. These published data were already
benchmarked in this repository; this is not fresh prospective holdout evidence.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from sklearn.ensemble import ExtraTreesRegressor, RandomForestRegressor
from sklearn.linear_model import Ridge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
from services.survey_speed_model import (  # noqa: E402
    DIRECTIONS, FEATURE_NAMES, FORECAST_DATE, SCOPE, SITE_ID, SOURCE_CSV_SHA256,
    SOURCE_PDF_SHA256, START_TIMES, features, numeric_prediction,
    predict_survey_speed, save_survey_speed_model,
)
from backtest_batam_survey import load_source  # noqa: E402

PRESETS = (
    ("previous_survey_day_slot", {}),
    ("historical_slot_mean", {}),
    ("direction_mean", {}),
    ("ridge_alpha_1", {"alpha": 1.0}),
    ("ridge_alpha_10", {"alpha": 10.0}),
    ("random_forest_depth_2", {"max_depth": 2}),
    ("random_forest_depth_3", {"max_depth": 3}),
    ("extra_trees_depth_2", {"max_depth": 2}),
    ("extra_trees_depth_3", {"max_depth": 3}),
)
BASELINES = ("previous_survey_day_slot", "historical_slot_mean", "direction_mean")


def metric(actual, predicted):
    errors = np.asarray(predicted, dtype=float) - np.asarray(actual, dtype=float)
    return {"samples": len(errors), "mae_kph": float(np.mean(np.abs(errors))),
            "rmse_kph": float(np.sqrt(np.mean(errors ** 2)))}


def history(rows):
    return [{"date": r["start"].date().isoformat(), "start_time": r["start"].strftime("%H:%M"),
             "direction": r["direction"], "speed_kph": r["speed"]} for r in rows]


def fit_numeric(name, rows):
    """No parameter search outside the declared bounded preset list."""
    parameters = dict(PRESETS)[name]
    if name in BASELINES:
        model = {"kind": name}
        if name == "direction_mean":
            model["means"] = {d: float(np.mean([r["speed"] for r in rows if r["direction"] == d])) for d in DIRECTIONS}
        return model
    x = np.asarray([features(r["start"], r["direction"]) for r in rows])
    y = np.asarray([r["speed"] for r in rows])
    if name.startswith("ridge"):
        estimator = Ridge(**parameters).fit(x, y)
        return {"kind": "linear", "coefficients": estimator.coef_.tolist(), "intercept": float(estimator.intercept_)}
    klass = RandomForestRegressor if name.startswith("random_forest") else ExtraTreesRegressor
    estimator = klass(n_estimators=48, min_samples_leaf=2, n_jobs=1, random_state=42, **parameters).fit(x, y)
    trees = []
    for fitted_tree in estimator.estimators_:
        t = fitted_tree.tree_
        trees.append({"left": t.children_left.tolist(), "right": t.children_right.tolist(),
                      "feature": t.feature.tolist(), "threshold": t.threshold.tolist(), "value": t.value[:, 0, 0].tolist()})
    return {"kind": "tree_ensemble", "trees": trees}


def predict_rows(model, fitted_rows, targets):
    h = history(fitted_rows)
    return [numeric_prediction(model, h, r["start"], r["direction"]) for r in targets]


def _per_direction(rows, predicted):
    return {d: metric([r["speed"] for r in rows if r["direction"] == d],
                      [p for r, p in zip(rows, predicted, strict=True) if r["direction"] == d]) for d in DIRECTIONS}


def train(rows, source_manifest):
    if (source_manifest.get("source_pdf_sha256") != SOURCE_PDF_SHA256
            or source_manifest.get("csv_sha256") != SOURCE_CSV_SHA256
            or source_manifest.get("license_url") != "https://creativecommons.org/licenses/by-sa/4.0/"):
        raise ValueError("Source provenance differs from the reviewed survey manifest.")
    dates = sorted({r["start"].date().isoformat() for r in rows})
    if dates != ["2023-02-16", "2023-02-17", FORECAST_DATE]:
        raise ValueError("Training source dates differ from the reviewed experiment.")
    fit = [r for r in rows if r["start"].date().isoformat() == dates[0]]
    validation = [r for r in rows if r["start"].date().isoformat() == dates[1]]
    test = [r for r in rows if r["start"].date().isoformat() == dates[2]]
    if [len(fit), len(validation), len(test)] != [16, 16, 16]:
        raise ValueError("Expected 16 reviewed directional means per survey day.")
    if (max(r["end"] for r in fit) >= min(r["start"] for r in validation)
            or max(r["end"] for r in fit + validation) >= min(r["start"] for r in test)):
        raise ValueError("Survey target windows cross the chronological split.")
    validation_scores = []
    for name, parameters in PRESETS:
        model = fit_numeric(name, fit)
        predicted = predict_rows(model, fit, validation)
        validation_scores.append({"model": name, "parameters": parameters,
                                  "metrics": metric([r["speed"] for r in validation], predicted),
                                  "per_direction": _per_direction(validation, predicted)})
    winner = min(validation_scores, key=lambda r: (r["metrics"]["mae_kph"], r["metrics"]["rmse_kph"], r["model"]))["model"]
    # No test measurement has participated in model selection or fitting.
    final_rows = fit + validation
    final_model = fit_numeric(winner, final_rows)
    payload = {"schema_version": 1, "format": "crossflow_survey_speed_numeric_v1", "scope": SCOPE,
               "site_id": SITE_ID, "source_pdf_sha256": SOURCE_PDF_SHA256, "source_csv_sha256": SOURCE_CSV_SHA256,
               "feature_names": list(FEATURE_NAMES), "forecast_date": FORECAST_DATE,
               "directions": list(DIRECTIONS), "start_times": list(START_TIMES), "training_dates": dates[:2],
               "model_name": winner, "model": final_model, "history": history(final_rows),
               "source_license": source_manifest["license"], "license_url": source_manifest["license_url"],
               "attribution": source_manifest["attribution"], "production_eligible": False}
    predicted = [predict_survey_speed(payload, site_id=SITE_ID, source_sha256=SOURCE_PDF_SHA256,
                                     interval_start=r["start"].isoformat(), direction=r["direction"])["predicted_speed_kph"] for r in test]
    baseline_predictions = {name: predict_rows(fit_numeric(name, final_rows), final_rows, test) for name in BASELINES}
    actual = [r["speed"] for r in test]
    test_metrics = metric(actual, predicted)
    baseline_metrics = {name: metric(actual, p) for name, p in baseline_predictions.items()}
    flagged = [r for r in final_rows if r["quality_flag"] == "speed_time_difference_over_0.5kph"]
    sensitivity_rows = [r for r in final_rows if r not in flagged]
    sensitivity_prediction = predict_rows(fit_numeric(winner, sensitivity_rows), sensitivity_rows, test)
    sensitivity_metric = metric(actual, sensitivity_prediction)
    predictions = [{"interval_start": r["start"].isoformat(), "interval_end": r["end"].isoformat(),
                    "direction": r["direction"], "actual_kph": r["speed"], "selected_model_kph": p,
                    **{name + "_kph": values[i] for name, values in baseline_predictions.items()},
                    "source_quality_flag": r["quality_flag"]} for i, (r, p) in enumerate(zip(test, predicted, strict=True))]
    split = lambda name, subset: {"role": name, "rows": len(subset), "survey_dates": sorted({r["start"].date().isoformat() for r in subset}),
                                 "first_interval_start": min(r["start"] for r in subset).isoformat(),
                                 "last_interval_end": max(r["end"] for r in subset).isoformat(),
                                 "whole_day_and_both_directions_grouped": True}
    report = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "target": "road_speed_kph",
              "scope": SCOPE, "training_performed": True, "selected_model": winner, "test_metrics": test_metrics,
              "test_metrics_per_direction": _per_direction(test, predicted), "baseline_metrics": baseline_metrics,
              "baseline_metrics_per_direction": {name: _per_direction(test, values) for name, values in baseline_predictions.items()},
              "improvement_over_previous_day_mae_percent": 100 * (1 - test_metrics["mae_kph"] / baseline_metrics["previous_survey_day_slot"]["mae_kph"]),
              "production_eligible": False, "serving_model_replaced": False, "application_history_modified": False,
              "source": {"site_id": SITE_ID, "location": source_manifest["location"], "rows": len(rows),
                         "survey_days": len(dates), "csv_sha256": SOURCE_CSV_SHA256, "pdf_sha256": SOURCE_PDF_SHA256,
                         "url": source_manifest["source_url"], "pdf_url": source_manifest["source_pdf_url"],
                         "license": source_manifest["license"], "license_url": source_manifest["license_url"], "attribution": source_manifest["attribution"]},
              "license": {"name": source_manifest["license"], "url": source_manifest["license_url"], "attribution": source_manifest["attribution"]},
              "splits": {"fit": split("candidate_fit", fit), "validation": split("fixed_preset_selection", validation),
                         "final_refit": split("frozen_winner_refit", final_rows), "test": split("final_retrospective_diagnostic", test)},
              "selection": {"criterion": "validation_MAE_then_RMSE_then_name", "validation_scores": validation_scores,
                            "winner_frozen_before_test": True, "test_used_for_tuning": False,
                            "candidate_count": len(PRESETS), "random_state": 42, "ensemble_n_estimators": 48,
                            "ensemble_min_samples_leaf": 2, "all_parameters_fixed_before_run": True},
              "feature_contract": {"names": list(FEATURE_NAMES), "known_at": "midnight_WIB_before_target_survey_day",
                                   "lag_baselines": "same clock slot and direction from strictly earlier survey days",
                                   "excluded_target_interval_fields": ["travel_time", "volume", "density", "reference_speed", "speed", "queue"]},
              "flagged_row_sensitivity": {"selected_model_unchanged": winner, "retuned": False, "removed_fit_rows": len(flagged),
                                          "removed_rows": [{"interval_start": r["start"].isoformat(), "direction": r["direction"], "flag": r["quality_flag"]} for r in flagged],
                                          "test_metrics": sensitivity_metric, "mae_change_kph": sensitivity_metric["mae_kph"] - test_metrics["mae_kph"]},
              "synthetic_training_rows": 0, "predictions": predictions,
              "limitations": ["This 48-row published dataset was previously benchmarked in this repository. The split is retrospective and is not fresh prospective untouched evidence.",
                              "One 100m site, two published directions, three days and peak windows; 16 correlated means on the test day are not 16 independent vehicle samples.",
                              "Published means are not original vehicle-level speed records. One source speed/time discrepancy is retained and tested separately.",
                              "Exact survey geometry and canonical corridor mapping remain unverified. No network or current-time prediction is supported.",
                              "The selected family is frozen before day3, but model comparison on only one validation day has substantial selection uncertainty.",
                              "The calculated PKJI reference speed is not used as measured free flow; no severity or travel-time ground truth is manufactured.",
                              "No uncertainty-coverage, camera counting, queue inference or 15/30/60-minute operational accuracy claim follows from this diagnostic."]}
    return report, payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "backend/data/research")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "backend/data/research/speed_training")
    args = parser.parse_args()
    rows, source_manifest = load_source(args.data_dir)
    report, payload = train(rows, source_manifest)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    artifact_receipt = save_survey_speed_model(payload, args.output_dir / "model.json")
    report["artifact"] = {**artifact_receipt, "path": "model.json", "format": payload["format"], "scope": SCOPE}
    (args.output_dir / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    with (args.output_dir / "predictions.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(report["predictions"][0]))
        writer.writeheader(); writer.writerows(report["predictions"])
    with (args.output_dir / "validation.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["model", "samples", "mae_kph", "rmse_kph"])
        writer.writeheader()
        for candidate in report["selection"]["validation_scores"]:
            writer.writerow({"model": candidate["model"], **candidate["metrics"]})
    manifest = {"schema_version": 1, "target": report["target"], "training_performed": True,
                "selected_model": report["selected_model"], "production_eligible": False,
                "source": report["source"], "license": report["license"],
                "reproduce": "CrossFlow_AI/.venv/bin/python CrossFlow_AI/scripts/train_batam_speed.py",
                "files": [{"path": p.name, "bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                          for p in sorted(args.output_dir.iterdir()) if p.name in {"model.json", "report.json", "predictions.csv", "validation.csv"}]}
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
    print(json.dumps({k: report[k] for k in ("target", "training_performed", "selected_model", "test_metrics", "baseline_metrics", "production_eligible")}))


if __name__ == "__main__":
    main()
