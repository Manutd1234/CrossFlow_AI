"""Experimental, observed-only forecasting; never changes the serving model.

``evaluate_causal_training`` fits bounded residual forests over persistence for
15/30/60 minutes. All current/lag/trend inputs existed at the forecast origin;
calendar inputs describe the known target time. Four chronological target
windows separate fit, model selection, calibration and final test. Calibrated
intervals are empirical: temporal dependence prevents a coverage guarantee.

The caller owns source/licence verification. Provider observations are excluded
unless explicitly authorized. Explicit availability timestamps are required for
promotion; bare observations can support only an assumption-labelled experiment.
No database, singleton, provider API, file write or executable artifact loader
is used here. The returned report is an experiment, not a production release.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from itertools import islice
import json
import math
from typing import Any

import numpy as np

from services.congestion_math import speed_ratio_congestion_score
from services.traffic_observations import (
    CongestionEstimatorConfig, ObservationConflictError, SpatialTrafficObservation,
    validate_spatial_observation,
)

MAX_INPUT_ROWS = 100_000
HORIZONS = (15, 30, 60)
CORRIDORS = tuple(f"corridor-{index + 1}" for index in range(5))
PROVIDER_SOURCES = frozenset({"tomtom_live"})
_GAP_MINUTES = 30
_PURGE_MINUTES = 60
_MODEL_SPECS = (
    {"name": "regularized", "max_depth": 8, "min_samples_leaf": 12},
    {"name": "adaptive", "max_depth": 10, "min_samples_leaf": 5},
)
FEATURE_NAMES = (
    "current_score", "current_age_minutes",
    "lag15_score", "lag15_missing", "lag30_score", "lag30_missing",
    "lag60_score", "lag60_missing", "trend_points_per_hour",
    "recent30_mean", "recent30_std", "recent30_count",
    "recent120_mean", "recent120_std", "recent120_count",
    "target_hour_sin", "target_hour_cos", "target_weekday_sin", "target_weekday_cos",
    "forecast_horizon_minutes", "prior_free_flow_speed_kph",
    *(f"is_{corridor}" for corridor in CORRIDORS),
)


@dataclass(frozen=True)
class _Example:
    target: SpatialTrafficObservation
    origin: datetime
    features: tuple[float, ...]
    actual: float
    persistence: float
    blended: float
    prior_free_flow: float


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime.")
    return value.astimezone(timezone.utc)


def _location(row: SpatialTrafficObservation):
    return row.corridor_id, round(row.latitude, 5), round(row.longitude, 5)


def _select(records, *, now, max_rows, allow_provider_training, availability_times):
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= MAX_INPUT_ROWS:
        raise ValueError(f"max_rows must be an integer in [1, {MAX_INPUT_ROWS}].")
    if not isinstance(allow_provider_training, bool):
        raise ValueError("allow_provider_training must be boolean.")
    if availability_times is not None and not isinstance(availability_times, Mapping):
        raise ValueError("availability_times must map observation keys to aware datetimes.")
    materialized = tuple(islice(iter(records), max_rows + 1))
    if len(materialized) > max_rows:
        raise ValueError(f"Causal evaluation is limited to {max_rows} input rows.")
    unique = {}
    for value in materialized:
        row = validate_spatial_observation(value)
        previous = unique.get(row.observation_key)
        if previous is not None and previous.immutable_payload() != row.immutable_payload():
            raise ObservationConflictError("Causal evaluation contains conflicting identities.")
        unique[row.observation_key] = row
    excluded = Counter()
    eligible, available = [], {}
    observed_rows = [row for row in unique.values() if row.observed and row.corridor_id in CORRIDORS]
    for row in unique.values():
        if not row.observed:
            excluded["non_observed"] += 1
            continue
        if row.corridor_id not in CORRIDORS:
            excluded["unsupported_corridor"] += 1
            continue
        if row.observed_at > now:
            excluded["future_observation"] += 1
            continue
        if row.source in PROVIDER_SOURCES and not allow_provider_training:
            excluded["provider_training_not_authorized"] += 1
            continue
        if availability_times is not None:
            if row.observation_key not in availability_times:
                excluded["missing_availability"] += 1
                continue
            at = _aware(availability_times[row.observation_key], "availability timestamp")
            if at > now:
                excluded["not_available_at_evaluation"] += 1
                continue
        else:
            at = row.observed_at
        available[row.observation_key] = at
        eligible.append(row)
    eligible.sort(key=lambda row: (row.observed_at, row.observation_key))
    selected = {}
    for row in eligible:
        bucket = int(row.observed_at.timestamp()) // 900
        selected.setdefault((*_location(row), bucket), row)
    rows = list(selected.values())
    metadata = {
        "input_rows": len(materialized), "unique_rows": len(unique),
        "duplicate_rows": len(materialized) - len(unique),
        "observed_supported_rows": len(observed_rows),
        "source_counts": dict(Counter(row.source for row in observed_rows)),
        "eligible_rows": len(eligible), "selected_rows": len(rows),
        "selected_source_counts": dict(Counter(row.source for row in rows)),
        "excluded": dict(excluded),
        "first_at": rows[0].observed_at.isoformat() if rows else None,
        "last_at": rows[-1].observed_at.isoformat() if rows else None,
        "span_hours": (rows[-1].observed_at - rows[0].observed_at).total_seconds() / 3600 if rows else 0.0,
        "distinct_local_dates": len({row.local_observed_at.date() for row in rows}),
        "local_hour_bins": sorted({row.local_observed_at.hour for row in rows}),
        "sampling": "first_original_per_location_per_15min_utc_bucket",
        "location_identity": "corridor_and_coordinates_rounded_to_5_decimals_not_edge_map_matching",
        "availability": "explicit_ingestion_times" if availability_times is not None else "assumed_observation_time_unverified",
        "provider_training_authorized_by_caller": allow_provider_training,
        "identity_list_sha256": hashlib.sha256("\n".join(row.observation_key for row in rows).encode()).hexdigest(),
        "canonical_payload_and_availability_sha256": hashlib.sha256(json.dumps(
            [{"measurement": row.immutable_payload(), "available_at": available[row.observation_key].isoformat()}
             for row in rows], sort_keys=True, separators=(",", ":"), default=str, allow_nan=False,
        ).encode()).hexdigest(),
    }
    return rows, available, metadata


def _partitions(rows):
    times = sorted({row.observed_at for row in rows})
    if len(times) < 8:
        return {name: [] for name in ("fit", "validation", "calibration", "holdout")}
    validation_at, calibration_at, holdout_at = (times[int(len(times) * fraction)] for fraction in (.5, .65, .8))
    purge = timedelta(minutes=_PURGE_MINUTES)
    return {
        "fit": [row for row in rows if row.observed_at < validation_at - purge],
        "validation": [row for row in rows if validation_at <= row.observed_at < calibration_at - purge],
        "calibration": [row for row in rows if calibration_at <= row.observed_at < holdout_at - purge],
        "holdout": [row for row in rows if row.observed_at >= holdout_at],
    }


def _window(rows):
    return {
        "rows": len(rows), "start_at": rows[0].observed_at.isoformat() if rows else None,
        "end_at": rows[-1].observed_at.isoformat() if rows else None,
        "corridors": sorted({row.corridor_id for row in rows}),
    }


def _canonical_history_blend(current, history):
    """Existing default history baseline, on rows already canonically checked.

    Revalidating and rehashing every historical row for every forecast would
    repeat up to38 million canonical parses at the public input bound. This
    uses the same default decay and rounding after one validation in selection.
    It is deliberately separate from any model fitting or selected features.
    """
    config = CongestionEstimatorConfig()
    local = current.local_observed_at
    current_hour = local.hour + local.minute / 60
    current_ratio = min(1.0, max(.05, current.speed_ratio))
    total, ratio_sum = 0.0, 0.0
    for row in history:
        age = (current.observed_at - row.observed_at).total_seconds()
        if age < 0 or row.observation_key == current.observation_key:
            continue
        sample_local = row.local_observed_at
        hour_distance = abs(current_hour - (sample_local.hour + sample_local.minute / 60)) % 24
        hour_distance = min(hour_distance, 24 - hour_distance)
        day_distance = abs(local.weekday() - sample_local.weekday()) % 7
        day_distance = min(day_distance, 7 - day_distance)
        weight = row.confidence * math.exp(-math.log(2) * age / 86400 / config.history_half_life_days)
        weight *= math.exp(-hour_distance / config.hour_distance_scale)
        weight *= math.exp(-day_distance / config.weekday_distance_scale)
        if weight >= config.minimum_history_weight:
            total += weight
            ratio_sum += min(1.0, max(.05, row.speed_ratio)) * weight
    if not total:
        return round(speed_ratio_congestion_score(current_ratio), 2)
    blend = min(.65, .25 + total / (total + 8)) * (1 - .35 * current.confidence)
    ratio = (1 - blend) * current_ratio + blend * ratio_sum / total
    return round(speed_ratio_congestion_score(ratio), 2)


def _examples(rows, targets, horizon, available, *, label_available_before=None):
    """Build forecasts from bounded histories that were available at origin.

    Selection density is at most one row/location/15min bucket. Looking back
    129 selected rows therefore covers every eligible point in the 120-minute
    feature window, even when late arrivals are removed. It also bounds work.
    """
    by_location = defaultdict(list)
    for row in rows:
        by_location[_location(row)].append(row)
    times = {key: [row.observed_at for row in values] for key, values in by_location.items()}
    result, skipped = [], Counter()
    for target in targets:
        if label_available_before is not None and available[target.observation_key] >= label_available_before:
            skipped["late_label_at_partition_freeze"] += 1
            continue
        origin = target.observed_at - timedelta(minutes=horizon)
        key = _location(target)
        history = by_location[key]
        end = bisect_right(times[key], origin)
        history = [row for row in history[max(0, end - 129):end]
                   if available[row.observation_key] <= origin]
        if not history or (origin - history[-1].observed_at).total_seconds() > _GAP_MINUTES * 60:
            skipped["missing_or_stale_current"] += 1
            continue
        current = history[-1]
        score = speed_ratio_congestion_score(current.speed_ratio)
        age = (origin - current.observed_at).total_seconds() / 60
        features = [score, age]
        for lookback in (15, 30, 60):
            cutoff = origin - timedelta(minutes=lookback)
            lag = next((row for row in reversed(history) if row.observed_at <= cutoff), None)
            missing = lag is None or (cutoff - lag.observed_at).total_seconds() > _GAP_MINUTES * 60
            features.extend((score if missing else speed_ratio_congestion_score(lag.speed_ratio), float(missing)))
        previous = next((row for row in reversed(history[:-1])
                         if row.observed_at < current.observed_at and
                         (current.observed_at - row.observed_at).total_seconds() <= 3600), None)
        trend = 0.0 if previous is None else (score - speed_ratio_congestion_score(previous.speed_ratio)) / ((current.observed_at - previous.observed_at).total_seconds() / 3600)
        features.append(float(np.clip(trend, -200, 200)))
        for lookback in (30, 120):
            values = [speed_ratio_congestion_score(row.speed_ratio) for row in history
                      if row.observed_at >= origin - timedelta(minutes=lookback)]
            features.extend((float(np.mean(values)), float(np.std(values)), float(len(values))))
        local = target.local_observed_at
        hour_angle = 2 * math.pi * (local.hour + local.minute / 60 + local.second / 3600) / 24
        day_angle = 2 * math.pi * local.weekday() / 7
        features.extend((math.sin(hour_angle), math.cos(hour_angle), math.sin(day_angle), math.cos(day_angle),
                         float(horizon), current.free_flow_speed_kph,
                         *(float(current.corridor_id == corridor) for corridor in CORRIDORS)))
        result.append(_Example(target, origin, tuple(features),
                               speed_ratio_congestion_score(target.speed_ratio), score,
                               _canonical_history_blend(current, history[:-1]), current.free_flow_speed_kph))
    return result, dict(skipped)


def _fit_estimator(examples, specification):
    # Lazy import: insufficient archives and unauthorized provider rows do not
    # construct or fit any estimator, nor import the serving forecaster.
    from sklearn.ensemble import RandomForestRegressor
    model = RandomForestRegressor(
        n_estimators=40, max_depth=specification["max_depth"],
        min_samples_leaf=specification["min_samples_leaf"], n_jobs=1,
        random_state=42,
    )
    model.fit(np.asarray([row.features for row in examples]),
              np.asarray([row.actual - row.persistence for row in examples]))
    return model


def _predict(model, examples):
    if not examples:
        return np.empty(0)
    residual = model.predict(np.asarray([row.features for row in examples]))
    return np.clip(np.asarray([row.persistence for row in examples]) + residual, 0, 95)


def _errors(actual, predicted):
    if not len(actual):
        return {"samples": 0, "mae_points": None, "rmse_points": None, "bias_points": None}
    error = np.asarray(predicted) - np.asarray(actual)
    return {"samples": len(actual), "mae_points": float(np.mean(np.abs(error))),
            "rmse_points": float(np.sqrt(np.mean(error ** 2))), "bias_points": float(np.mean(error))}


def _fingerprint(model):
    digest = hashlib.sha256()
    for estimator in model.estimators_:
        tree = estimator.tree_
        for value in (tree.children_left, tree.children_right, tree.feature, tree.threshold, tree.value):
            digest.update(np.asarray(value, dtype="<f8").tobytes())
    return digest.hexdigest()


def _severity(actual, predicted):
    labels = lambda values: np.where(np.asarray(values) >= 70, 2, np.where(np.asarray(values) >= 40, 1, 0))
    truth, forecast = labels(actual), labels(predicted)
    confusion = np.zeros((3, 3), dtype=int)
    for first, second in zip(truth, forecast):
        confusion[first, second] += 1
    f1 = []
    for index in range(3):
        denominator = int(confusion[index].sum() + confusion[:, index].sum())
        f1.append(2 * int(confusion[index, index]) / denominator if denominator else 0.0)
    return {"label_definition": "speed_ratio_derived_thresholds_40_and_70_not_independent_annotations",
            "labels": ["SMOOTH", "HEAVY", "CRITICAL"], "confusion_actual_by_predicted": confusion.tolist(),
            "accuracy": float(np.mean(truth == forecast)), "macro_f1_all_three_classes": float(np.mean(f1))}


def evaluate_causal_training(
    records: Iterable[SpatialTrafficObservation], *, now: datetime | None = None,
    allow_provider_training: bool = False, max_rows: int = MAX_INPUT_ROWS,
    availability_times: Mapping[str, datetime] | None = None,
) -> dict[str, Any]:
    """Return an observed-only experiment report; never publish or save a model.

    ``availability_times`` maps canonical observation keys to ingestion times.
    Missing keys are excluded when a mapping is supplied. This is the time the
    local collector received a row; a delayed retrospective import therefore
    cannot masquerade as an online historical forecast. Setting the provider
    flag is an assertion that the caller verified the relevant training rights.
    """
    now = _aware(now or datetime.now(timezone.utc), "evaluation time")
    rows, available, selection = _select(records, now=now, max_rows=max_rows,
                                       allow_provider_training=allow_provider_training,
                                       availability_times=availability_times)
    partitions = _partitions(rows)
    reasons = []
    report = {
        "schema_version": 1, "engine": "crossflow_causal_residual_rf_experimental_v1",
        "generated_at": now.isoformat(), "status": "insufficient_observations",
        "training_performed": False, "promotion_eligible": False,
        "model_replaced": False, "artifact_saved": False, "reasons": reasons,
        "selection": selection, "splits": {name: _window(values) for name, values in partitions.items()},
        "horizons": [], "feature_names": list(FEATURE_NAMES),
        "policy": {"min_span_days": 7, "min_local_dates": 7, "min_hour_bins": 18,
                   "min_partition_rows": {"fit": 100, "validation": 50, "calibration": 50, "holdout": 100},
                   "required_corridors": list(CORRIDORS), "purge_minutes": _PURGE_MINUTES,
                   "horizons_minutes": list(HORIZONS), "max_current_age_minutes": _GAP_MINUTES,
                   "min_horizon_samples": 30, "min_corridor_horizon_samples": 5,
                   "min_mae_improvement_fraction": .05, "max_corridor_mae_regression_fraction": .05,
                   "min_interval_coverage": .70, "interval_target": .80,
                   "forest_trees": 40, "forest_max_depth": 10, "fit_workers": 1},
        "assumptions": [
            "Experimental candidate only; no serving model or road cost is changed.",
            "No synthetic/modelled rows; provider training requires caller-verified rights.",
            "Timestamp groups are separated with 60-minute purges before the next forecast origins.",
            "Validation alone selects model configuration; final fit uses fit plus validation labels only.",
            "Calibration alone determines interval radius; final test labels do not select or calibrate models.",
            "Current, lag, trend and history inputs require observation and availability times at/before origin.",
            "Fit, validation and calibration labels must arrive strictly before the next partition's earliest forecast origin.",
            "Bare observation timestamps are an unverified availability assumption and cannot support promotion.",
            "Intervals have no coverage guarantee under temporal dependence.",
            "Speed-ratio scores floor at .05 and cap at 95; speed error is also reported in km/h.",
            "Travel-time and image vehicle-count/queue labels are absent; those tasks are not trained here.",
            "Locations match rounded coordinates, not directed road edges; all-street coverage is not established.",
        ],
    }
    if selection["excluded"].get("provider_training_not_authorized"):
        reasons.append("provider_rows_excluded_training_rights_not_authorized")
    if selection["span_hours"] < 7 * 24:
        reasons.append("observed_span_below_7_days")
    if selection["distinct_local_dates"] < 7:
        reasons.append("insufficient_distinct_local_days")
    if len(selection["local_hour_bins"]) < 18:
        reasons.append("insufficient_hour_of_day_coverage")
    for name, minimum in report["policy"]["min_partition_rows"].items():
        partition = partitions[name]
        if len(partition) < minimum:
            reasons.append(f"insufficient_{name}_rows")
        if {row.corridor_id for row in partition} != set(CORRIDORS):
            reasons.append(f"{name}_does_not_cover_all_five_corridors")
    blocking = [reason for reason in reasons if reason != "provider_rows_excluded_training_rights_not_authorized"]
    if blocking:
        if not rows and selection["excluded"].get("provider_training_not_authorized"):
            report["status"] = "provider_training_not_authorized"
        return report
    # Provider exclusion is disclosed but need not prevent an eligible model
    # based exclusively on other authorized, sufficiently complete sources.
    if availability_times is None:
        reasons.append("availability_unverified_promotion_blocked")
    for horizon in HORIZONS:
        freezes = {
            "fit": partitions["validation"][0].observed_at - timedelta(minutes=horizon),
            "validation": partitions["calibration"][0].observed_at - timedelta(minutes=horizon),
            "calibration": partitions["holdout"][0].observed_at - timedelta(minutes=horizon),
        }
        examples = {name: _examples(rows, targets, horizon, available, label_available_before=freezes.get(name))
                    for name, targets in partitions.items()}
        outcome = {"horizon_minutes": horizon,
                   "partition_pairs": {name: len(values[0]) for name, values in examples.items()},
                   "skipped": {name: values[1] for name, values in examples.items()},
                   "label_availability_freeze_exclusive": {name: value.isoformat() for name, value in freezes.items()},
                   "partition_pair_corridors": {name: sorted({row.target.corridor_id for row in values[0]})
                                                for name, values in examples.items()}}
        report["horizons"].append(outcome)
        missing_corridors = False
        for name, minimum in (("fit", 100), ("validation", 30), ("calibration", 50), ("holdout", 30)):
            if len(examples[name][0]) < minimum:
                reasons.append(f"horizon_{horizon}_insufficient_{name}_forecast_pairs")
            if set(outcome["partition_pair_corridors"][name]) != set(CORRIDORS):
                reasons.append(f"horizon_{horizon}_{name}_pairs_do_not_cover_all_five_corridors")
                missing_corridors = True
        if missing_corridors or any(len(examples[name][0]) < minimum for name, minimum in (("fit", 100), ("validation", 30), ("calibration", 50), ("holdout", 30))):
            outcome["status"] = "insufficient_forecast_pairs"
            continue
        fit, validation, calibration, holdout = (examples[name][0] for name in ("fit", "validation", "calibration", "holdout"))
        validation_actual = [row.actual for row in validation]
        candidates = []
        for specification in _MODEL_SPECS:
            model = _fit_estimator(fit, specification)
            report["training_performed"] = True
            score = _errors(validation_actual, _predict(model, validation))["mae_points"]
            candidates.append((score, specification))
        validation_mae, specification = min(candidates, key=lambda item: item[0])
        model = _fit_estimator(fit + validation, specification)
        calibration_prediction = _predict(model, calibration)
        residuals = np.sort(np.abs(calibration_prediction - np.asarray([row.actual for row in calibration])))
        rank = min(len(residuals), math.ceil((len(residuals) + 1) * .80))
        radius = float(residuals[rank - 1])
        predicted = _predict(model, holdout)
        actual = np.asarray([row.actual for row in holdout])
        lower, upper = np.clip(predicted - radius, 0, 95), np.clip(predicted + radius, 0, 95)
        coverage = float(np.mean((lower <= actual) & (actual <= upper)))
        candidate_metrics = _errors(actual, predicted)
        persistence = _errors(actual, [row.persistence for row in holdout])
        blended = _errors(actual, [row.blended for row in holdout])
        speed_prediction = np.asarray([row.prior_free_flow for row in holdout]) * (1 - predicted / 100)
        speed_actual = np.asarray([row.target.actual_speed_kph for row in holdout])
        speed_error = speed_prediction - speed_actual
        per_corridor = {}
        for corridor in CORRIDORS:
            indexes = [index for index, row in enumerate(holdout) if row.target.corridor_id == corridor]
            values = _errors(actual[indexes], predicted[indexes])
            baseline = _errors(actual[indexes], [holdout[index].persistence for index in indexes])
            per_corridor[corridor] = {"candidate": values, "persistence": baseline}
            if len(indexes) < 5:
                reasons.append(f"horizon_{horizon}_insufficient_{corridor}_samples")
            if values["mae_points"] is not None and values["mae_points"] > baseline["mae_points"] * 1.05:
                reasons.append(f"horizon_{horizon}_regressed_vs_persistence_{corridor}")
        for name, baseline in (("persistence", persistence), ("current_history_blend", blended)):
            if baseline["mae_points"] <= 0 or candidate_metrics["mae_points"] > baseline["mae_points"] * .95:
                reasons.append(f"horizon_{horizon}_not_better_than_{name}")
        if coverage < .70:
            reasons.append(f"horizon_{horizon}_interval_coverage_below_policy")
        outcome.update({
            "status": "complete", "model": {**specification, "algorithm": "RandomForestRegressor_residual_over_persistence",
                                                   "n_estimators": 40, "model_sha256": _fingerprint(model)},
            "validation_mae_points": validation_mae,
            "validation_candidates": [{"configuration": spec["name"], "mae_points": value} for value, spec in candidates],
            "candidate": candidate_metrics, "persistence": persistence, "current_history_blend": blended,
            "speed": {"samples": len(holdout), "mae_kph": float(np.mean(np.abs(speed_error))),
                      "rmse_kph": float(np.sqrt(np.mean(speed_error ** 2))),
                      "prediction_free_flow_reference": "latest_available_prior_measurement_not_target_reference"},
            "severity": _severity(actual, predicted), "per_corridor": per_corridor,
            "interval": {"method": "calibration_only_symmetric_absolute_error_order_statistic",
                         "calibration_pairs": len(calibration), "radius_points": radius,
                         "nominal_coverage": .80, "holdout_coverage": coverage,
                         "mean_width_points": float(np.mean(upper - lower)), "coverage_guaranteed": False},
            "feature_importances": {name: float(value) for name, value in zip(FEATURE_NAMES, model.feature_importances_)},
            "fit_target_end_at": (fit + validation)[-1].target.observed_at.isoformat(),
            "calibration_target_start_at": calibration[0].target.observed_at.isoformat(),
            "holdout_target_start_at": holdout[0].target.observed_at.isoformat(),
        })
    release_reasons = [reason for reason in reasons if reason != "provider_rows_excluded_training_rights_not_authorized"]
    report["promotion_eligible"] = report["training_performed"] and not release_reasons and all(item.get("status") == "complete" for item in report["horizons"])
    report["status"] = "eligible_experimental_candidate" if report["promotion_eligible"] else "evaluated" if report["training_performed"] else "insufficient_forecast_pairs"
    return report
