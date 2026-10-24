"""Observed-only candidate training with a purged chronological promotion gate.

Evaluation never modifies the serving singleton or history. Weather and ferry
proximity are not present in the archive: their effects are not learned or
validated by this experiment. Evidence gates are engineering release criteria,
not a guarantee of field accuracy.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import islice
import hashlib
import json
import math
from typing import Any, Iterable

import numpy as np
from sklearn.ensemble import RandomForestRegressor

from models.congestion_model import CongestionForecaster, SpatialPredictionContext
from services.congestion_math import speed_ratio_congestion_score
from services.congestion_validation import _metrics
from services.traffic_observations import (
    ObservationConflictError, SpatialTrafficObservation,
    estimate_speed_ratio_congestion, validate_spatial_observation,
)

MAX_INPUT_ROWS = 100_000
CORRIDORS = {f"corridor-{index + 1}": index for index in range(5)}
HORIZONS = (15, 30, 60)


class ObservedTrainingBusy(RuntimeError):
    """An explicit model mutation already owns the fitting slot."""


@dataclass(frozen=True)
class ObservedTrainingPolicy:
    min_span_days: int = 7
    min_distinct_local_days: int = 7
    min_hour_bins: int = 18
    min_fit_rows: int = 100
    min_calibration_rows: int = 50
    min_holdout_rows: int = 100
    min_horizon_samples: int = 30
    min_corridor_horizon_samples: int = 5
    min_mae_improvement_fraction: float = 0.05
    max_corridor_mae_regression_fraction: float = 0.05
    min_interval_coverage: float = 0.70
    max_current_age_minutes: int = 30
    purge_minutes: int = 60
    sampling_minutes: int = 15

    def payload(self) -> dict[str, Any]:
        return {
            **self.__dict__, "horizons_minutes": list(HORIZONS),
            "required_corridors": "all_five_supported_corridors",
            "interval_target": 0.80, "max_input_rows": MAX_INPUT_ROWS,
            "purpose": "engineering_promotion_gate_not_field_accuracy_guarantee",
        }


DEFAULT_POLICY = ObservedTrainingPolicy()


def _window(rows: list[SpatialTrafficObservation]) -> dict[str, Any]:
    return {"rows": len(rows),
            "start_at": rows[0].observed_at.isoformat() if rows else None,
            "end_at": rows[-1].observed_at.isoformat() if rows else None}


def _location(row: SpatialTrafficObservation) -> tuple[str, float, float]:
    return row.corridor_id, round(row.latitude, 5), round(row.longitude, 5)


def _select(records: Iterable[SpatialTrafficObservation], *, max_rows: int,
            now: datetime, policy: ObservedTrainingPolicy):
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= MAX_INPUT_ROWS:
        raise ValueError(f"Observed evaluation max_rows must be in [1, {MAX_INPUT_ROWS}].")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Observed evaluation time must be timezone aware.")
    materialized = tuple(islice(iter(records), max_rows + 1))
    if len(materialized) > max_rows:
        raise ValueError(f"Observed evaluation is limited to {max_rows} input rows.")
    unique: dict[str, SpatialTrafficObservation] = {}
    for value in materialized:
        row = validate_spatial_observation(value)
        previous = unique.get(row.observation_key)
        if previous is not None and previous.immutable_payload() != row.immutable_payload():
            raise ObservationConflictError("Observed training contains conflicting observation identities.")
        unique[row.observation_key] = row
    eligible = sorted((row for row in unique.values()
                       if row.observed and row.corridor_id in CORRIDORS
                       and row.observed_at <= now.astimezone(timezone.utc)),
                      key=lambda row: (row.observed_at, row.observation_key))
    selected: dict[tuple[Any, ...], SpatialTrafficObservation] = {}
    for row in eligible:
        bucket = int(row.observed_at.timestamp()) // (policy.sampling_minutes * 60)
        # Keep a real original measurement, not an average labelled as observed.
        selected.setdefault((*_location(row), bucket), row)
    rows = list(selected.values())
    return rows, {
        "input_rows": len(materialized), "unique_rows": len(unique),
        "duplicate_rows": len(materialized) - len(unique),
        "eligible_rows": len(eligible), "observed_rows": len(eligible),
        "selected_rows": len(rows),
        "excluded_non_observed_rows": sum(not row.observed for row in unique.values()),
        "excluded_future_rows": sum(row.observed and row.corridor_id in CORRIDORS
                                    and row.observed_at > now.astimezone(timezone.utc)
                                    for row in unique.values()),
        "excluded_unsupported_corridor_rows": sum(row.observed and row.corridor_id not in CORRIDORS for row in unique.values()),
        "source_counts": dict(Counter(row.source for row in eligible)),
        "selected_source_counts": dict(Counter(row.source for row in rows)),
        "first_at": eligible[0].observed_at.isoformat() if eligible else None,
        "last_at": eligible[-1].observed_at.isoformat() if eligible else None,
        "span_hours": round((eligible[-1].observed_at - eligible[0].observed_at).total_seconds() / 3600, 4) if eligible else 0.0,
        "distinct_timestamps": len({row.observed_at for row in eligible}),
        "distinct_local_dates": len({row.local_observed_at.date() for row in eligible}),
        "observed_hour_bins": sorted({row.local_observed_at.hour for row in eligible}),
        "sampling_policy": "first_original_per_location_15min_utc_bucket",
        "selected_observation_sha256": hashlib.sha256(json.dumps(
            [row.immutable_payload() for row in rows], default=str,
            separators=(",", ":"), allow_nan=False,
        ).encode()).hexdigest(),
        "location_matching": "corridor_and_coordinates_rounded_to_5_decimals_not_edge_map_matching",
    }


def _partitions(rows: list[SpatialTrafficObservation], policy: ObservedTrainingPolicy):
    times = sorted({row.observed_at for row in rows})
    if len(times) < 5:
        return [], [], []
    calibration_at = times[int(len(times) * 0.6)]
    holdout_at = times[int(len(times) * 0.8)]
    purge = timedelta(minutes=policy.purge_minutes)
    return ([row for row in rows if row.observed_at < calibration_at - purge],
            [row for row in rows if calibration_at <= row.observed_at < holdout_at - purge],
            [row for row in rows if row.observed_at >= holdout_at])


def _context(current: SpatialTrafficObservation, target: SpatialTrafficObservation):
    local = target.local_observed_at
    return replace(SpatialPredictionContext.from_observation(
        current, corridor_idx=CORRIDORS[current.corridor_id]),
        hour_float=local.hour + local.minute / 60 + local.second / 3600,
        day_of_week=local.weekday())


def _predict(candidate: CongestionForecaster, contexts: list[SpatialPredictionContext]):
    predictions = []
    for start in range(0, len(contexts), 4096):
        predictions.extend(candidate.predict_spatial_batch(contexts[start:start + 4096]))
    return predictions


def _examples(rows, targets, horizon, policy, *, matched_targets=None):
    by_location = defaultdict(list)
    for row in rows:
        by_location[_location(row)].append(row)
    times = {key: [row.observed_at for row in values] for key, values in by_location.items()}
    actual, persistence, blended, contexts, corridors = [], [], [], [], []
    skipped = 0
    for target in targets:
        origin = target.observed_at - timedelta(minutes=horizon)
        history = by_location[_location(target)]
        end = bisect_right(times[_location(target)], origin)
        if not end or (origin - history[end - 1].observed_at).total_seconds() > policy.max_current_age_minutes * 60:
            skipped += 1
            continue
        current = history[end - 1]
        estimate = estimate_speed_ratio_congestion(current, history[max(0, end - 129):end - 1])
        actual.append(speed_ratio_congestion_score(target.speed_ratio))
        persistence.append(speed_ratio_congestion_score(current.speed_ratio))
        blended.append(estimate.congestion_score)
        contexts.append(_context(current, target))
        corridors.append(target.corridor_id)
        if matched_targets is not None:
            matched_targets.append(target)
    return actual, persistence, blended, contexts, corridors, skipped


def _conformal_padding(candidate, rows, calibration, policy):
    residuals = []
    for horizon in HORIZONS:
        actual, _, _, contexts, _, _ = _examples(rows, calibration, horizon, policy)
        predictions = _predict(candidate, contexts)
        residuals.extend(max(pred.p10_score - value, value - pred.p90_score, 0.0)
                         for value, pred in zip(actual, predictions))
    if not residuals:
        return None, 0
    residuals.sort()
    # Finite-sample split-conformal order statistic. Temporal dependence means
    # this is empirical calibration, not an exchangeability coverage guarantee.
    rank = min(len(residuals), math.ceil((len(residuals) + 1) * 0.80))
    return float(residuals[rank - 1]), len(residuals)


def _horizon_results(candidate: CongestionForecaster, rows: list[SpatialTrafficObservation],
                     holdout: list[SpatialTrafficObservation], policy: ObservedTrainingPolicy):
    results = []
    for horizon in HORIZONS:
        actual, persistence, blended, contexts, corridors, skipped = _examples(rows, holdout, horizon, policy)
        predictions = _predict(candidate, contexts)
        means = [prediction.mean_score for prediction in predictions]
        covered = sum(pred.p10_score <= value <= pred.p90_score for pred, value in zip(predictions, actual))
        results.append({
            "horizon_minutes": horizon,
            "status": "complete" if actual else "insufficient_current_observations",
            "serving_model": _metrics(actual, means),
            "persistence": _metrics(actual, persistence),
            "current_history_blend": _metrics(actual, blended),
            "p10_p90_coverage": round(covered / len(actual), 4) if actual else None,
            "skipped_missing_or_stale_current": skipped,
            "per_corridor": {
                corridor: {"serving_model": _metrics([actual[i] for i in range(len(actual)) if corridors[i] == corridor],
                                                     [means[i] for i in range(len(actual)) if corridors[i] == corridor]),
                           "persistence": _metrics([actual[i] for i in range(len(actual)) if corridors[i] == corridor],
                                                   [persistence[i] for i in range(len(actual)) if corridors[i] == corridor])}
                for corridor in CORRIDORS
            },
        })
    return results


def _build_candidate(records, *, now: datetime | None, max_rows: int):
    now = now or datetime.now(timezone.utc)
    policy = DEFAULT_POLICY
    rows, selection = _select(records, max_rows=max_rows, now=now, policy=policy)
    fit, calibration, holdout = _partitions(rows, policy)
    report = {
        "schema_version": 1, "generated_at": now.isoformat(),
        "status": "insufficient_observations",
        "validation_scope": "observed_chronological_backtest",
        "evaluation_subject": "private_observed_candidate_serving_pipeline",
        "running_model_evaluated": False, "model_replaced": False,
        "model_specification": {"algorithm": "RandomForestRegressor", "n_estimators": 100,
                                "max_depth": 10, "min_samples_leaf": 5,
                                "n_jobs": 1, "random_state": 42},
        "selection": selection,
        "chronology": {"fit": _window(fit), "calibration": _window(calibration),
                       "holdout": _window(holdout), "purge_minutes": policy.purge_minutes,
                       "strict_timestamp_groups": True},
        "horizons": [],
        "activation": {"eligible": False, "reasons": [], "policy": policy.payload()},
        "assumptions": [
            "Actual validated Batam speeds only; synthetic, simulated and modelled rows excluded.",
            "No random split; fit and calibration end strictly before the next partition's earliest forecast origin.",
            "Weather uses clear encoding because measurements do not record weather; weather response is not validated.",
            "Ferry proximity is unknown; the external published-timetable adjustment is not validated by this backtest.",
            "Observation times are assumed availability times; provider ingestion delays are unknown.",
            "Tree intervals are widened using calibration-only residuals; temporal dependence prevents a coverage guarantee.",
            "No observed field-accuracy claim follows merely from passing this engineering gate.",
        ],
    }
    reasons = report["activation"]["reasons"]
    if selection["span_hours"] < policy.min_span_days * 24:
        reasons.append("observed_span_below_7_days")
    if selection["distinct_local_dates"] < policy.min_distinct_local_days:
        reasons.append("insufficient_distinct_observed_local_days")
    if len(selection["observed_hour_bins"]) < policy.min_hour_bins:
        reasons.append("insufficient_observed_hour_of_day_coverage")
    for name, partition, minimum in (("fit", fit, policy.min_fit_rows),
                                      ("calibration", calibration, policy.min_calibration_rows),
                                      ("holdout", holdout, policy.min_holdout_rows)):
        if len(partition) < minimum:
            reasons.append(f"insufficient_{name}_rows")
        if set(row.corridor_id for row in partition) != set(CORRIDORS):
            reasons.append(f"{name}_does_not_cover_all_five_corridors")
    if reasons:
        return report, None
    candidate = CongestionForecaster(train_baseline=False)
    candidate.model = RandomForestRegressor(
        n_estimators=100, max_depth=10, min_samples_leaf=5,
        n_jobs=1, random_state=42,
    )
    metrics = candidate.retrain_from_observations(fit + calibration, holdout_start=calibration[0].observed_at)
    if not candidate.is_trained or metrics.get("last_retrain_skipped_reason"):
        reasons.append("candidate_fit_did_not_produce_observed_model")
        return report, None
    padding, calibration_pairs = _conformal_padding(candidate, rows, calibration, policy)
    if padding is None:
        reasons.append("insufficient_calibration_forecast_pairs")
        return report, None
    candidate._interval_padding_points = padding
    report["interval_calibration"] = {"method": "chronological_split_conformal_tree_interval",
                                      "calibration_rows": len(calibration),
                                      "calibration_forecast_pairs": calibration_pairs,
                                      "padding_points": round(padding, 6),
                                      "target_coverage": 0.80,
                                      "coverage_guaranteed": False}
    report["horizons"] = _horizon_results(candidate, rows, holdout, policy)
    for horizon in report["horizons"]:
        label = f"horizon_{horizon['horizon_minutes']}"
        if horizon["serving_model"]["samples"] < policy.min_horizon_samples:
            reasons.append(label + "_insufficient_samples")
        mae = horizon["serving_model"]["mae_points"]
        for baseline in ("persistence", "current_history_blend"):
            baseline_mae = horizon[baseline]["mae_points"]
            if mae is None or baseline_mae is None or baseline_mae <= 0 or not mae <= baseline_mae * (1 - policy.min_mae_improvement_fraction):
                reasons.append(label + "_not_better_than_" + baseline)
        if horizon["p10_p90_coverage"] is None or horizon["p10_p90_coverage"] < policy.min_interval_coverage:
            reasons.append(label + "_interval_coverage_below_policy")
        for corridor, values in horizon["per_corridor"].items():
            if values["serving_model"]["samples"] < policy.min_corridor_horizon_samples:
                reasons.append(label + "_insufficient_" + corridor + "_samples")
            model_mae = values["serving_model"]["mae_points"]
            baseline_mae = values["persistence"]["mae_points"]
            if model_mae is not None and baseline_mae is not None and model_mae > baseline_mae * (1 + policy.max_corridor_mae_regression_fraction):
                reasons.append(label + "_regressed_vs_persistence_" + corridor)
    report["activation"]["eligible"] = not reasons
    report["status"] = "eligible" if not reasons else "evaluated"
    # The untouched final holdout scores the same serving pipeline used above;
    # it does not choose features, calibrate intervals, or refit the candidate.
    evaluation_targets = []
    actual, _, _, contexts, _, _ = _examples(
        rows, holdout, 30, policy, matched_targets=evaluation_targets,
    )
    predictions = _predict(candidate, contexts)
    # No 30-minute matches means there can be no promotion. Preserve the
    # private fit metrics and explicit failed horizon reasons for the report.
    if not actual:
        candidate.metrics["observed_training"] = report
        return report, candidate
    means = np.array([pred.mean_score for pred in predictions])
    errors = means - np.asarray(actual)
    denominator = float(np.sum((np.asarray(actual) - np.mean(actual)) ** 2))
    candidate.metrics.update({
        "total_samples": len(fit) + len(calibration) + len(evaluation_targets),
        "selected_observed_rows": len(rows),
        "observed_training_rows": len(fit) + len(calibration),
        "observed_fit_rows": len(fit), "observed_calibration_rows": len(calibration),
        "observed_evaluation_rows": len(actual),
        "evaluation_horizon_minutes": 30,
        "observed_evaluation_horizon_minutes": 30,
        "training_data_source": "validated_spatial_observed",
        "validation_scope": "purged_chronological_observed_holdout",
        "evaluation_scope": "observed_serving_pipeline_weather_clear_ferry_unknown",
        "r2_score": round(1 - float(np.sum(errors ** 2)) / denominator, 4) if denominator > 0 else 0.0,
        "mae": round(float(np.mean(np.abs(errors))), 4),
        "rmse": round(float(np.sqrt(np.mean(errors ** 2))), 4),
        "holdout_source_counts": dict(Counter(row.source for row in evaluation_targets)),
        "holdout_first_observed_at": evaluation_targets[0].observed_at.isoformat(),
        "holdout_last_observed_at": evaluation_targets[-1].observed_at.isoformat(),
        "holdout_partition_first_observed_at": holdout[0].observed_at.isoformat(),
        "calibration_source_counts": dict(Counter(row.source for row in calibration)),
        "calibration_first_observed_at": calibration[0].observed_at.isoformat(),
        "calibration_last_observed_at": calibration[-1].observed_at.isoformat(),
        "calibration": {"interval": "P10-P90 tree quantiles with calibration-only conformal widening",
                        "interval_coverage_target": 0.80,
                        "interval_coverage_observed": round(sum(pred.p10_score <= y <= pred.p90_score for pred, y in zip(predictions, actual)) / len(actual), 4),
                        "mean_interval_width_points": round(float(np.mean([pred.p90_score - pred.p10_score for pred in predictions])), 4),
                        "holdout_order": "strictly_later_than_fit_and_calibration_with_60min_purge",
                        "holdout_samples": len(actual),
                        "calibration_rows": len(calibration), "padding_points": padding,
                        "coverage_guaranteed": False},
        "observed_training": report,
    })
    return report, candidate


def evaluate_observed_training(records: Iterable[SpatialTrafficObservation], *,
                               now: datetime | None = None, max_rows: int = 50_000) -> dict[str, Any]:
    """Evaluate an observed-only private candidate; never replace a live model."""
    report, _candidate = _build_candidate(records, now=now, max_rows=max_rows)
    return report


def retrain_observed_model(forecaster: CongestionForecaster,
                           records: Iterable[SpatialTrafficObservation], *,
                           now: datetime | None = None, artifact_path=None) -> dict[str, Any]:
    """Explicit mutation: validate evidence, optionally persist, then swap once."""
    if not forecaster._retrain_lock.acquire(blocking=False):
        raise ObservedTrainingBusy("Observed candidate training is already in progress.")
    try:
        report, candidate = _build_candidate(records, now=now, max_rows=MAX_INPUT_ROWS)
        if candidate is None or not report["activation"]["eligible"]:
            return report
        artifact = {"persisted": False, "durability": "process_memory", "scope": "single_process"}
        if artifact_path is not None:
            from services.observed_model_artifact import save_observed_model
            try:
                artifact = {
                    **save_observed_model(candidate, artifact_path, report=report),
                    "persisted": True, "durability": "persistent_local_file",
                    "scope": "single_host", "restored": False,
                }
            except (OSError, ValueError):
                report["activation"]["eligible"] = False
                report["activation"]["reasons"].append("candidate_artifact_persistence_failed")
                report["status"] = "evaluated"
                return report
        candidate.metrics["artifact"] = artifact
        report["artifact"] = artifact
        with forecaster._model_lock:
            report["model_replaced"] = True
            report["status"] = "activated"
            forecaster.model = candidate.model
            forecaster.metrics = candidate.metrics
            forecaster._interval_padding_points = candidate._interval_padding_points
            forecaster.is_trained = True
        return report
    finally:
        forecaster._retrain_lock.release()
