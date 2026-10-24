#!/usr/bin/env python3
"""Train a causal, offline Paris hourly-flow transfer benchmark.

This is deliberately separate from the Batam production engine. No Paris result
establishes Batam speed, travel-time, queue, or congestion accuracy. Run from any
directory with the repository Python environment; no downloads are performed.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import platform
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import joblib
import numpy as np
import sklearn
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from threadpoolctl import threadpool_limits

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
DEFAULT_SOURCE = WORKSPACE_ROOT / "research/batam-traffic/datasets/round4_open/forecasting/normalized/paris_hourly.csv"
DEFAULT_OUTPUT = REPO_ROOT / "backend/data/research/forecast_training"
PARIS = ZoneInfo("Europe/Paris")
SOURCE_SHA256 = "dc3db788c3937713d6d62c71a5f6cb9dcd3bc2b36724af74064f113b7e653583"
SCHEMA = "crossflow.research_forecasting.v1"
RANDOM_SEED = 20261024
LAGS = (1, 2, 3, 6, 12, 24, 48, 168)
WINDOWS = (3, 6, 24, 168)
BASELINES = ("persistence", "daily_seasonal", "weekly_seasonal", "training_calendar_profile")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class Grid:
    times: list[datetime]
    arc_ids: list[str]
    values: np.ndarray
    source_rows: int
    source_null_targets: int
    absent_source_keys: int


def load_grid(path: Path) -> Grid:
    """Preserve measured zeros, absent keys and null counts as distinct facts."""
    rows: dict[tuple[datetime, str], float] = {}
    null_count = 0
    with path.open(newline="", encoding="utf-8") as handle:
        for record in csv.DictReader(handle):
            timestamp = datetime.fromisoformat(record["timestamp_end_utc"].replace("Z", "+00:00"))
            if timestamp.utcoffset() is None or timestamp.minute or timestamp.second or timestamp.microsecond:
                raise ValueError("Expected explicitly zoned, whole-hour timestamps")
            timestamp = timestamp.astimezone(timezone.utc)
            key = (timestamp, record["arc_id"])
            if key in rows:
                raise ValueError(f"Duplicate source arc/hour: {key}")
            target = record["volume_vehicles_hour"].strip()
            number = float(target) if target else math.nan
            if target and (not math.isfinite(number) or number < 0):
                raise ValueError(f"Invalid measured count: {key}")
            null_count += int(not target)
            rows[key] = number
    if not rows:
        raise ValueError("No source measurements")
    arc_ids = sorted({arc for _, arc in rows}, key=int)
    first, last = min(timestamp for timestamp, _ in rows), max(timestamp for timestamp, _ in rows)
    times = [first + timedelta(hours=i) for i in range(int((last - first).total_seconds() // 3600) + 1)]
    values = np.full((len(times), len(arc_ids)), np.nan, dtype=np.float64)
    arc_lookup = {arc: i for i, arc in enumerate(arc_ids)}
    for (timestamp, arc), target in rows.items():
        index = int((timestamp - first).total_seconds() // 3600)
        values[index, arc_lookup[arc]] = target
    return Grid(times, arc_ids, values, len(rows), null_count, values.size - len(rows))


def causal_features(grid: Grid) -> tuple[np.ndarray, list[str], dict[int, int]]:
    """Every count feature uses a timestamp strictly before its target hour.

    Reindexing the explicit hourly grid before shifting prevents a gap from
    accidentally turning the previous available observation into a one-hour lag.
    """
    hours, arcs = grid.values.shape
    local = [timestamp.astimezone(PARIS) for timestamp in grid.times]
    names = ["arc_category", "hour_sin", "hour_cos", "weekday_sin", "weekday_cos", "is_weekend"]
    pieces = [
        np.broadcast_to(np.arange(arcs), (hours, arcs)).astype(float),
        np.broadcast_to(np.array([math.sin(2 * math.pi * t.hour / 24) for t in local])[:, None], (hours, arcs)),
        np.broadcast_to(np.array([math.cos(2 * math.pi * t.hour / 24) for t in local])[:, None], (hours, arcs)),
        np.broadcast_to(np.array([math.sin(2 * math.pi * t.weekday() / 7) for t in local])[:, None], (hours, arcs)),
        np.broadcast_to(np.array([math.cos(2 * math.pi * t.weekday() / 7) for t in local])[:, None], (hours, arcs)),
        np.broadcast_to(np.array([int(t.weekday() >= 5) for t in local])[:, None], (hours, arcs)),
    ]
    lag_columns: dict[int, int] = {}
    lag_arrays: dict[int, np.ndarray] = {}
    for lag in LAGS:
        shifted = np.full_like(grid.values, np.nan)
        if lag < hours:
            shifted[lag:] = grid.values[:-lag]
        lag_columns[lag] = len(names)
        names.append(f"q_lag_{lag}h")
        lag_arrays[lag] = shifted
        pieces.append(shifted)
    observed = np.isfinite(grid.values)
    cumulative_count = np.vstack([np.zeros((1, arcs)), np.cumsum(observed, axis=0)])
    numeric = np.where(observed, grid.values, 0)
    cumulative_sum = np.vstack([np.zeros((1, arcs)), np.cumsum(numeric, axis=0)])
    cumulative_squares = np.vstack([np.zeros((1, arcs)), np.cumsum(numeric * numeric, axis=0)])
    ends = np.arange(hours)
    for window in WINDOWS:
        starts = np.maximum(0, ends - window)
        count = cumulative_count[ends] - cumulative_count[starts]
        summed = cumulative_sum[ends] - cumulative_sum[starts]
        squared = cumulative_squares[ends] - cumulative_squares[starts]
        mean = np.divide(summed, count, out=np.full_like(summed, np.nan), where=count > 0)
        variance = np.divide(squared, count, out=np.full_like(squared, np.nan), where=count > 0) - mean * mean
        names.extend([f"q_prior_{window}h_mean", f"q_prior_{window}h_std", f"q_prior_{window}h_missing_fraction"])
        pieces.extend([mean, np.sqrt(np.maximum(variance, 0)), 1 - count / window])
    names.extend(["q_lag_1h_minus_2h", "q_lag_1h_minus_24h"])
    pieces.extend([lag_arrays[1] - lag_arrays[2], lag_arrays[1] - lag_arrays[24]])
    return np.stack(pieces, axis=2).reshape(hours * arcs, len(names)), names, lag_columns


def split_masks(grid: Grid) -> dict[str, np.ndarray]:
    warmup_end = grid.times[0] + timedelta(hours=max(LAGS))
    validation_start = datetime(2026, 9, 1, tzinfo=PARIS).astimezone(timezone.utc)
    test_start = datetime(2026, 9, 17, tzinfo=PARIS).astimezone(timezone.utc)
    times = np.array(grid.times, dtype=object)
    observed = np.isfinite(grid.values).reshape(-1)
    within = {
        "train": (times >= warmup_end) & (times < validation_start),
        "validation": (times >= validation_start) & (times < test_start),
        "test": times >= test_start,
    }
    return {name: np.repeat(mask, len(grid.arc_ids)) & observed for name, mask in within.items()}


def metrics(actual: np.ndarray, predicted: np.ndarray) -> dict[str, Any]:
    actual, predicted = np.asarray(actual, dtype=float), np.asarray(predicted, dtype=float)
    if len(actual) == 0 or not np.isfinite(actual).all() or not np.isfinite(predicted).all():
        raise ValueError("Metrics require nonempty, finite paired observations")
    error = predicted - actual
    absolute = np.abs(error)
    denominator = np.abs(actual).sum()
    symmetric_denominator = np.abs(actual) + np.abs(predicted)
    symmetric = np.divide(2 * absolute, symmetric_denominator, out=np.zeros_like(absolute), where=symmetric_denominator > 0)
    return {
        "rows": len(actual),
        "mae_vehicles_per_hour": float(absolute.mean()),
        "rmse_vehicles_per_hour": float(np.sqrt(np.mean(error * error))),
        "wape_pct": float(100 * absolute.sum() / denominator) if denominator else None,
        "smape_pct": float(100 * symmetric.mean()),
        "bias_vehicles_per_hour": float(error.mean()),
        "median_absolute_error": float(np.median(absolute)),
        "p90_absolute_error": float(np.quantile(absolute, .9)),
        "actual_mean_vehicles_per_hour": float(actual.mean()),
        "measured_zero_target_rows": int((actual == 0).sum()),
    }


def fit_profile(grid: Grid, fit_mask: np.ndarray) -> dict[str, Any]:
    """A fallback/calendar profile learned only from the designated fit slice."""
    targets = grid.values.reshape(-1)
    indices = np.flatnonzero(fit_mask)
    arcs = len(grid.arc_ids)
    arc_values: dict[int, list[float]] = {}
    hour_values: dict[tuple[int, int], list[float]] = {}
    week_values: dict[tuple[int, int, int], list[float]] = {}
    for index in indices:
        t, arc = divmod(int(index), arcs)
        local = grid.times[t].astimezone(PARIS)
        value = float(targets[index])
        arc_values.setdefault(arc, []).append(value)
        hour_values.setdefault((arc, local.hour), []).append(value)
        week_values.setdefault((arc, local.weekday(), local.hour), []).append(value)
    return {
        "global_median": float(np.median(targets[fit_mask])),
        "arc_medians": {k: float(np.median(v)) for k, v in arc_values.items()},
        "hour_medians": {k: float(np.median(v)) for k, v in hour_values.items()},
        "week_hour_medians": {k: float(np.median(v)) for k, v in week_values.items()},
        "fitted_rows": int(fit_mask.sum()),
    }


def baseline_predictions(grid: Grid, features: np.ndarray, lag_columns: dict[int, int], mask: np.ndarray,
                         profile: dict[str, Any]) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    selected = np.flatnonzero(mask)
    arcs = len(grid.arc_ids)
    arc_medians = np.array([profile["arc_medians"].get(int(i % arcs), profile["global_median"]) for i in selected])
    output: dict[str, np.ndarray] = {}
    coverage: dict[str, Any] = {}
    orders = {"persistence": [1, 24, 168], "daily_seasonal": [24, 168, 1], "weekly_seasonal": [168, 24, 1]}
    for name, order in orders.items():
        values = arc_medians.copy()
        # Reverse filling leaves the first nonmissing lag in the declared order.
        for lag in reversed(order):
            candidate = features[mask, lag_columns[lag]]
            values = np.where(np.isfinite(candidate), candidate, values)
        first = features[mask, lag_columns[order[0]]]
        available = np.column_stack([np.isfinite(features[mask, lag_columns[lag]]) for lag in order])
        output[name] = values
        coverage[name] = {"primary_lag_available_rows": int(np.isfinite(first).sum()),
                          "primary_lag_missing_rows": int((~np.isfinite(first)).sum()),
                          "all_declared_lags_missing_rows": int((~available.any(axis=1)).sum()),
                          "fallback_order_hours": order,
                          "last_resort": "fit-slice arc median, then fit-slice global median"}
    values = []
    exact = 0
    for i in selected:
        t, arc = divmod(int(i), arcs)
        local = grid.times[t].astimezone(PARIS)
        key = (arc, local.weekday(), local.hour)
        exact += int(key in profile["week_hour_medians"])
        value = profile["week_hour_medians"].get(key, profile["hour_medians"].get((arc, local.hour),
                profile["arc_medians"].get(arc, profile["global_median"])))
        values.append(value)
    output["training_calendar_profile"] = np.asarray(values)
    coverage["training_calendar_profile"] = {"exact_arc_weekday_hour_profile_rows": exact,
                                            "fallback_rows": len(selected) - exact,
                                            "fitted_rows": profile["fitted_rows"]}
    return output, coverage


def make_candidates(feature_count: int) -> dict[str, Any]:
    numeric = list(range(1, feature_count))

    def preprocessing(scale: bool) -> ColumnTransformer:
        steps: list[tuple[str, Any]] = [("impute", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True))]
        if scale:
            steps.append(("scale", StandardScaler()))
        return ColumnTransformer([
            ("arc", OneHotEncoder(handle_unknown="ignore", sparse_output=False), [0]),
            ("numeric", Pipeline(steps), numeric),
        ])

    candidates: dict[str, Any] = {}
    for leaves, loss in [(15, "squared_error"), (31, "squared_error"), (31, "absolute_error")]:
        name = f"hist_gradient_boosting_{leaves}_{loss}"
        candidates[name] = HistGradientBoostingRegressor(
            loss=loss, learning_rate=.06, max_iter=220, max_leaf_nodes=leaves,
            min_samples_leaf=25, l2_regularization=20, categorical_features=[0],
            early_stopping=False, random_state=RANDOM_SEED,
        )
    candidates["extra_trees_128"] = Pipeline([
        ("features", preprocessing(False)),
        ("regressor", ExtraTreesRegressor(n_estimators=128, max_depth=18, min_samples_leaf=3,
                                          max_features=.8, n_jobs=2, random_state=RANDOM_SEED)),
    ])
    for alpha in [10., 100.]:
        candidates[f"ridge_{alpha:g}"] = Pipeline([
            ("features", preprocessing(True)), ("regressor", Ridge(alpha=alpha)),
        ])
    return candidates


def predict_nonnegative(estimator: Any, features: np.ndarray) -> np.ndarray:
    return np.maximum(np.asarray(estimator.predict(features), dtype=float), 0)


def split_receipt(grid: Grid, mask: np.ndarray) -> dict[str, Any]:
    indices = np.flatnonzero(mask)
    arcs = len(grid.arc_ids)
    keys = "\n".join(f"{iso(grid.times[int(i // arcs)])},{grid.arc_ids[int(i % arcs)]}" for i in indices)
    return {"observed_target_rows": int(mask.sum()),
            "first_target_end_utc": iso(grid.times[int(indices[0] // arcs)]),
            "last_target_end_utc": iso(grid.times[int(indices[-1] // arcs)]),
            "ordered_target_keys_sha256": hashlib.sha256(keys.encode()).hexdigest()}


def run(source: Path, output: Path, *, allow_existing: bool = False) -> dict[str, Any]:
    if not source.is_file():
        raise FileNotFoundError(source)
    source_digest = sha256(source)
    if source_digest != SOURCE_SHA256:
        raise ValueError("Input hash differs from the audited Paris normalization; review source before training")
    if output.exists() and any(output.iterdir()) and not allow_existing:
        raise FileExistsError(f"Refusing to overwrite existing run: {output}; use --replace for an explicit rerun")
    output.mkdir(parents=True, exist_ok=True)
    prior_state_path = output / "execution_state.json"
    if prior_state_path.exists():
        prior_state = json.loads(prior_state_path.read_text())
        previous_attempts = int(prior_state["execution_attempt_number"])
    else:
        previous_attempts = int((output / "selection_frozen_before_holdout.json").exists())
    execution_state = {"execution_attempt_number": previous_attempts + 1,
                       "prior_frozen_selection_without_complete_report": bool(previous_attempts and not (output / "report.json").exists()),
                       "protocol_or_hyperparameters_changed_after_holdout": False,
                       "status": "running"}
    write_json(prior_state_path, execution_state)
    started = time.monotonic()
    grid = load_grid(source)
    features, feature_names, lag_columns = causal_features(grid)
    target = grid.values.reshape(-1)
    masks = split_masks(grid)
    if not all(mask.any() for mask in masks.values()):
        raise ValueError("One or more chronological splits are empty")
    if np.any(masks["train"] & masks["validation"]) or np.any(masks["validation"] & masks["test"]):
        raise AssertionError("Splits overlap")
    training_profile = fit_profile(grid, masks["train"])
    validation_predictions, validation_baseline_coverage = baseline_predictions(grid, features, lag_columns, masks["validation"], training_profile)
    validation_scores: dict[str, Any] = {
        name: {"kind": "baseline", **metrics(target[masks["validation"]], prediction)}
        for name, prediction in validation_predictions.items()
    }
    candidates = make_candidates(len(feature_names))
    with threadpool_limits(limits=2):
        for name, estimator in candidates.items():
            model_start = time.monotonic()
            estimator.fit(features[masks["train"]], target[masks["train"]])
            prediction = predict_nonnegative(estimator, features[masks["validation"]])
            validation_predictions[name] = prediction
            validation_scores[name] = {"kind": "trained_model", **metrics(target[masks["validation"]], prediction),
                                       "fit_and_validation_seconds": round(time.monotonic() - model_start, 3),
                                       "configuration": estimator.get_params(deep=False).__repr__()}
            print(json.dumps({"phase": "validation", "name": name, "mae": validation_scores[name]["mae_vehicles_per_hour"]}), flush=True)
    ordered = sorted(validation_scores, key=lambda name: (validation_scores[name]["mae_vehicles_per_hour"], name))
    selected = ordered[0]
    best_learned = min(candidates, key=lambda name: (validation_scores[name]["mae_vehicles_per_hour"], name))
    best_baseline = min(BASELINES, key=lambda name: (validation_scores[name]["mae_vehicles_per_hour"], name))
    # The entire choice, features, dates and hyperparameters are now frozen.
    selection = {"selection_metric": "validation MAE, then deterministic name tie-break",
                 "selected_forecaster": selected, "best_trained_model": best_learned,
                 "best_validation_baseline": best_baseline,
                 "validation_rank_order": ordered,
                 "test_used_for_selection": False,
                 "final_refit": "Refit only the best validation trained candidate on train + validation; holdout targets excluded; retain it even if a baseline wins selection"}
    write_json(output / "selection_frozen_before_holdout.json", selection)
    development = masks["train"] | masks["validation"]
    development_profile = fit_profile(grid, development)
    # Preserve the strongest learned model even when validation prefers a baseline.
    final_estimator = make_candidates(len(feature_names))[best_learned]
    with threadpool_limits(limits=2):
        final_estimator.fit(features[development], target[development])
    artifact = {"schema": SCHEMA, "scope": "Paris hourly-flow transfer benchmark only",
                "production_eligible": False, "geography_is_batam": False,
                "target": "hourly_loop_flow_vehicles", "forecast_horizon_hours": 1,
                "selected_forecaster": selected, "trained_model_name": best_learned,
                "estimator": final_estimator, "feature_names": feature_names,
                "arc_ids": grid.arc_ids, "fallback_profile": development_profile,
                "source_sha256": source_digest, "random_seed": RANDOM_SEED,
                "numpy_version": np.__version__, "sklearn_version": sklearn.__version__,
                "calibration_absolute_residual_p90": float(np.quantile(np.abs(target[masks["validation"]] - validation_predictions[selected]), .9))}
    joblib.dump(artifact, output / "model.joblib", compress=3)
    reloaded = joblib.load(output / "model.joblib")
    reload_probe = features[np.flatnonzero(development)[:32]]
    with threadpool_limits(limits=2):
        if not np.array_equal(predict_nonnegative(final_estimator, reload_probe),
                              predict_nonnegative(reloaded["estimator"], reload_probe)):
            raise AssertionError("Saved artifact changed development-set predictions on reload")
    holdout_predictions, holdout_baseline_coverage = baseline_predictions(grid, features, lag_columns, masks["test"], development_profile)
    with threadpool_limits(limits=2):
        holdout_predictions[best_learned] = predict_nonnegative(final_estimator, features[masks["test"]])
    holdout_scores = {name: {"kind": "baseline" if name in BASELINES else "trained_model",
                            **metrics(target[masks["test"]], predicted)}
                      for name, predicted in holdout_predictions.items()}
    selected_prediction = holdout_predictions[selected]
    calibration = artifact["calibration_absolute_residual_p90"]
    lower, upper = np.maximum(selected_prediction - calibration, 0), selected_prediction + calibration
    interval = {"calibration_split": "validation only, before final train + validation refit",
                "nominal_coverage_pct": 90,
                "absolute_residual_quantile_vehicles_per_hour": calibration,
                "holdout_empirical_coverage_pct": float(100 * np.mean((target[masks["test"]] >= lower) & (target[masks["test"]] <= upper))),
                "mean_interval_width_vehicles_per_hour": float(np.mean(upper - lower)),
                "guarantee": False,
                "limitation": "Serial dependence, distribution shift and model refit invalidate an exchangeability-based coverage guarantee"}
    per_arc: list[dict[str, Any]] = []
    empty_holdout_arc_ids: list[str] = []
    holdout_indices = np.flatnonzero(masks["test"])
    for arc_index, arc in enumerate(grid.arc_ids):
        selected_arc = holdout_indices % len(grid.arc_ids) == arc_index
        if not selected_arc.any():
            empty_holdout_arc_ids.append(arc)
        for name, prediction in holdout_predictions.items():
            arc_metrics = metrics(target[masks["test"]][selected_arc], prediction[selected_arc]) if selected_arc.any() else {
                key: 0 if key == "rows" else None for key in holdout_scores[name] if key != "kind"}
            per_arc.append({"arc_id": arc, "forecaster": name, **arc_metrics})
    with (output / "per_arc_metrics.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(per_arc[0]))
        writer.writeheader()
        writer.writerows(per_arc)
    with gzip.open(output / "test_predictions.csv.gz", "wt", newline="", encoding="utf-8") as handle:
        fieldnames = ["arc_id", "target_end_utc", "forecast_origin_utc", "actual_flow_vehicles_per_hour",
                      "selected_forecaster", "selected_prediction", "interval90_lower", "interval90_upper", *holdout_predictions]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row, flat in enumerate(holdout_indices):
            t, arc = divmod(int(flat), len(grid.arc_ids))
            writer.writerow({"arc_id": grid.arc_ids[arc], "target_end_utc": iso(grid.times[t]),
                             "forecast_origin_utc": iso(grid.times[t] - timedelta(hours=1)),
                             "actual_flow_vehicles_per_hour": float(target[flat]), "selected_forecaster": selected,
                             "selected_prediction": float(selected_prediction[row]),
                             "interval90_lower": float(lower[row]), "interval90_upper": float(upper[row]),
                             **{name: float(prediction[row]) for name, prediction in holdout_predictions.items()}})
    selected_metrics = holdout_scores[selected]
    improvements = {}
    for name in BASELINES:
        baseline_mae = holdout_scores[name]["mae_vehicles_per_hour"]
        improvements[name] = 100 * (baseline_mae - selected_metrics["mae_vehicles_per_hour"]) / baseline_mae if baseline_mae else None
    report = {
        "schema": SCHEMA, "created_at_utc": iso(datetime.now(timezone.utc)),
        "status": "trained_and_held_out_evaluated", "geography": "Paris, France: 24 deterministically selected road arcs",
        "geography_is_batam": False, "transfer_benchmark_only": True, "production_eligible": False,
        "target": "hourly_loop_flow_vehicles", "forecast_horizon_hours": 1,
        "not_validated_targets": ["Batam traffic", "road speed", "travel time", "congestion severity", "camera vehicle counts", "queues"],
        "source": {"path": str(source.relative_to(WORKSPACE_ROOT)) if source.is_relative_to(WORKSPACE_ROOT) else str(source),
                   "sha256": source_digest, "publisher": "Direction de la Voirie et des Déplacements, Ville de Paris",
                   "url": "https://opendata.paris.fr/explore/dataset/comptages-routiers-permanents/",
                   "license": "ODbL", "license_url": "https://opendatacommons.org/licenses/odbl/",
                   "source_rows": grid.source_rows, "source_null_flow_rows": grid.source_null_targets,
                   "absent_hour_arc_keys": grid.absent_source_keys, "explicit_grid_rows": grid.values.size,
                   "arc_ids": grid.arc_ids, "arc_count": len(grid.arc_ids), "grid_hours": len(grid.times),
                   "first_target_end_utc": iso(grid.times[0]), "last_target_end_utc": iso(grid.times[-1]),
                   "timestamp_semantics": "End of preceding one-hour source loop elaboration interval"},
        "protocol": {"mode": "Rolling one-hour-ahead prequential forecasting of existing arcs",
                     "prediction_time": "At target end minus one hour, assuming prior-hour count has already arrived",
                     "missing_targets": "Excluded from supervised fit/evaluation; never filled with zero",
                     "missing_features": "Native NaN for histogram boosting; fit-slice-only median and missing indicators for other estimators",
                     "warmup_hours_excluded_from_training": max(LAGS),
                     "train_local_dates": ["2026-08-08", "2026-08-31"],
                     "validation_local_dates": ["2026-09-01", "2026-09-16"],
                     "holdout_local_dates": ["2026-09-17", "2026-09-30"],
                     "calendar_timezone": "Europe/Paris",
                     "preprocessing_refit": "Train slice during selection; train + validation only during final fit",
                     "holdout_protocol": "One frozen final holdout, no feature or hyperparameter changes based on its scores",
                     "features_frozen_before_holdout": True, "online_lag_updates": "Observed earlier validation/test counts may enter later predictions; no multi-day open-loop claim",
                     "same_holdout_targets_for_every_forecaster": True,
                     "source_state_and_reference_dates_used_as_features": False},
        "features": {"names": feature_names, "count": len(feature_names),
                     "lineage": "Only arc category, known target calendar and source flow observations at strictly earlier exact UTC hourly timestamps",
                     "count_lag_hours": list(LAGS), "rolling_windows_hours": list(WINDOWS),
                     "rolling_window_closure": "[target - window hours, target), excluding target count",
                     "unused_source_fields": ["occupancy_pct", "source_traffic_state", "source_arc_state", "quality_status", "reference date_debut/date_fin", "geometries"]},
        "splits": {name: split_receipt(grid, mask) for name, mask in masks.items()},
        "selection": selection, "validation_scores": validation_scores,
        "artifact_reload_prediction_parity_passed": True,
        "holdout_scores": holdout_scores, "selected_holdout_metrics": selected_metrics,
        "selected_holdout_mae_improvement_pct_vs_baselines": improvements,
        "selected_macro_arc_mae": float(np.mean([row["mae_vehicles_per_hour"] for row in per_arc if row["forecaster"] == selected and row["rows"] > 0])),
        "arcs_with_no_observed_holdout_targets": empty_holdout_arc_ids,
        "execution": execution_state,
        "baseline_coverage": {"validation": validation_baseline_coverage, "holdout": holdout_baseline_coverage},
        "interval90": interval,
        "limitations": [
            "Foreign-city transfer benchmark; not a trained or validated Batam congestion predictor.",
            "Only 61 calendar days and 24 smallest active arc IDs, not a representative city or seasonal sample.",
            "No independently checked per-row detector fault flags; nonmissing source counts can still contain measurement error.",
            "Zero publication latency is assumed because actual detector publication latency is unavailable.",
            "Autocorrelated road-hour errors are not independent vehicle samples; no IID confidence claims.",
            "No holidays, incidents, weather or road-change data were acquired for these measurements.",
            "No generalization test to previously unseen roads; all holdout arcs occur in training.",
            "The held-out period is now consumed for this fixed experiment; changing features after seeing these scores requires a new untouched holdout.",
            "Interval calibration shares the model-selection validation slice and is not a separate conformal calibration set.",
            "Hourly detector flow is neither exact camera vehicle count nor measured road speed, travel time, queue length or independently labeled congestion severity.",
            "The serialized scikit-learn model is a local trusted research artifact; retain source attribution/license and exact environment when reusing it.",
        ],
        "environment": {"python": platform.python_version(), "numpy": np.__version__, "sklearn": sklearn.__version__,
                        "joblib": joblib.__version__, "random_seed": RANDOM_SEED, "max_compute_threads": 2,
                        "training_wall_seconds": round(time.monotonic() - started, 3)},
    }
    execution_state["status"] = "complete"
    write_json(prior_state_path, execution_state)
    write_json(output / "report.json", report)
    readme = f"""# Paris hourly-flow transfer benchmark

This run trained a real hourly-flow forecaster on public Paris loop measurements.
It does **not** validate Batam speeds, travel times, congestion, cameras or queues.

- Selected on validation MAE: `{selected}`.
- Untouched 14-day holdout: {selected_metrics['rows']:,} observed road-hours.
- MAE: {selected_metrics['mae_vehicles_per_hour']:.3f} vehicles/hour; WAPE: {selected_metrics['wape_pct']:.3f}%.
- Persistence MAE: {holdout_scores['persistence']['mae_vehicles_per_hour']:.3f} vehicles/hour.
- Daily seasonal MAE: {holdout_scores['daily_seasonal']['mae_vehicles_per_hour']:.3f}; weekly seasonal MAE: {holdout_scores['weekly_seasonal']['mae_vehicles_per_hour']:.3f}.
- Original source has {grid.source_null_targets:,} null flow values and {grid.absent_source_keys:,} absent arc/hour keys. These remain missing, not zero.

The first seven days supply feature history. August 8–31 trains six fixed CPU
candidates, September 1–16 selects the forecaster and calibrates residuals, and
September 17–30 is evaluated once after refitting on development data. All models
and baselines use the same observed holdout targets. Forecasts roll forward one
hour at a time and may use earlier measured holdout values, never future values.
Zero publication delay is assumed. This is not an open-loop two-week forecast.

`report.json` contains full protocol, split-key hashes, missingness, aggregate
metrics, validation candidate scores, limitations and environment. Per-road
metrics and holdout predictions are retained for inspection. `model.joblib`
contains the best learned candidate plus the selected forecaster identity and
fit-only fallback profile. Selection can prefer a baseline if it wins validation.
The model is intentionally ineligible for production and no app wiring is changed.

Source: Direction de la Voirie et des Déplacements, Ville de Paris,
[permanent road counts](https://opendata.paris.fr/explore/dataset/comptages-routiers-permanents/),
[ODbL](https://opendatacommons.org/licenses/odbl/). Source normalization hash:
`{source_digest}`. Derived predictions retain that attribution and license notice.

Reproduce with the recorded Python/scikit-learn environment:

```sh
.venv/bin/python scripts/research_forecasting.py --output backend/data/research/forecast_training_reproduction
```

The consumed holdout must not be reused to tune a new experiment. A future Batam
deployment requires authorized timestamped Batam observations, latency checks,
local chronological backtesting and target-specific validation.
"""
    (output / "README.md").write_text(readme, encoding="utf-8")
    files = [path for path in sorted(output.iterdir()) if path.is_file() and path.name != "manifest.json"]
    manifest = {"schema": SCHEMA, "scope": "Non-Batam offline transfer benchmark", "production_eligible": False,
                "source_sha256": source_digest, "training_script_sha256": sha256(Path(__file__).resolve()),
                "files": [{"path": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)} for path in files]}
    write_json(output / "manifest.json", manifest)
    print(json.dumps({"phase": "complete", "selected": selected, "holdout": selected_metrics,
                      "output": str(output), "wall_seconds": report["environment"]["training_wall_seconds"]}), flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", "--output-dir", dest="output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--replace", action="store_true", help="Explicitly replace this script's prior run outputs")
    args = parser.parse_args()
    run(args.source.resolve(), args.output.resolve(), allow_existing=args.replace)


if __name__ == "__main__":
    main()
