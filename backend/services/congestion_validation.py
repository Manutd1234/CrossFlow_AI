"""Offline congestion evaluation with explicit time and provenance boundaries.

Nothing here writes traffic history or replaces the running model. Stress
scenarios establish calculation invariants, never measured traffic accuracy.
Backtests forecast held-out speed observations using only information that
would have existed at the forecast origin.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import islice
import math
from typing import Any, Iterable

import numpy as np

from models.congestion_model import CongestionForecaster, SpatialPredictionContext
from services.congestion_math import speed_ratio_congestion_score
from services.traffic_observations import (
    ObservationConflictError,
    SpatialTrafficObservation,
    estimate_speed_ratio_congestion,
    validate_spatial_observation,
)

MAX_BACKTEST_ROWS = 20_000
SUPPORTED_CORRIDORS = {f"corridor-{index + 1}": index for index in range(5)}


def _metrics(actual: list[float], predicted: list[float]) -> dict[str, Any]:
    if not actual:
        return {"samples": 0, "mae_points": None, "rmse_points": None,
                "bias_points": None}
    errors = np.asarray(predicted) - np.asarray(actual)
    return {
        "samples": len(actual),
        "mae_points": round(float(np.mean(np.abs(errors))), 4),
        "rmse_points": round(float(np.sqrt(np.mean(errors ** 2))), 4),
        "bias_points": round(float(np.mean(errors)), 4),
    }


def backtest_congestion(
    records: Iterable[SpatialTrafficObservation],
    *,
    horizons_minutes: tuple[int, ...] = (15, 30, 60),
    include_modelled: bool = False,
    max_current_age_minutes: int = 30,
) -> dict[str, Any]:
    """Frozen chronological holdout compared with two prior-only baselines.

    All records with the split timestamp are assigned to holdout together.
    The candidate's training snapshot strictly predates the first forecast
    origin. Persistence and history blend can consume new measurements during
    holdout, but only measurements at/before each origin, never the target.
    Weather/ferry inputs absent from observations remain declared defaults.
    """
    if not horizons_minutes or any(
        isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 180
        for value in horizons_minutes
    ) or len(set(horizons_minutes)) != len(horizons_minutes):
        raise ValueError("Horizons must be unique integer minutes in [1, 180].")
    if isinstance(max_current_age_minutes, bool) or not isinstance(
        max_current_age_minutes, int
    ) or not 1 <= max_current_age_minutes <= 180:
        raise ValueError("Maximum current age must be integer minutes in [1, 180].")
    materialized = tuple(islice(iter(records), MAX_BACKTEST_ROWS + 1))
    if len(materialized) > MAX_BACKTEST_ROWS:
        raise ValueError(f"Backtests are limited to {MAX_BACKTEST_ROWS} observations.")
    unique: dict[str, SpatialTrafficObservation] = {}
    for item in materialized:
        item = validate_spatial_observation(item)
        previous = unique.get(item.observation_key)
        if previous and previous.immutable_payload() != item.immutable_payload():
            raise ObservationConflictError("Backtest contains conflicting observation identities.")
        unique[item.observation_key] = item
    eligible = sorted((
        item for item in unique.values()
        if item.corridor_id in SUPPORTED_CORRIDORS
        and (include_modelled or item.observed)
    ), key=lambda item: (item.observed_at, item.observation_key))
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "insufficient_observations",
        "validation_scope": ("synthetic_or_mixed_chronological_backtest"
                             if include_modelled else "observed_chronological_backtest"),
        "input_rows": len(materialized),
        "unique_rows": len(unique),
        "eligible_rows": len(eligible),
        "observed_rows": sum(item.observed for item in eligible),
        "source_counts": dict(Counter(item.source for item in eligible)),
        "requires_measured_data_for_accuracy_claim": True,
        "model_replaced": False,
        "assumptions": [
            "weather=clear; ferry proximity unknown because observations do not persist these inputs",
            "missing capacity and terminal distance use declared corridor defaults",
            "speed-ratio congestion target is clipped to [0,95] points",
            "observed timestamps are treated as availability timestamps; ingestion delays are unknown",
            "current baselines require the same corridor and location rounded to 5 decimals (about 1m); this is not edge map matching",
        ],
        "horizons": [],
        "readiness": {
            "eligible_rows": len(eligible),
            "minimum_unique_observations": 150,
            "minimum_prior_training_observations": 100,
            "synthetic_fallback_allowed": False,
            "promotion_evaluated": False,
        },
    }
    if len(eligible) < 150:
        report["reason"] = "At least 150 eligible unique observations are required."
        return report
    holdout_start = eligible[int(len(eligible) * 0.8)].observed_at
    training_deadline = holdout_start - timedelta(minutes=max(horizons_minutes))
    training = [item for item in eligible if item.observed_at < training_deadline]
    targets = [item for item in eligible if item.observed_at >= holdout_start]
    if len(training) < 100:
        report["reason"] = "Fewer than 100 observations strictly before the first forecast origin."
        return report
    candidate = CongestionForecaster(train_baseline=False)
    candidate_metrics = candidate.retrain_from_observations(
        training, include_modelled=include_modelled,
    )
    if candidate_metrics.get("last_retrain_skipped_reason"):
        report["reason"] = "A chronological candidate could not be fit; no synthetic fallback was used."
        return report
    # Candidate retraining reserves its own final 20% for calibration. The
    # outer holdout remains entirely separate from both those partitions.
    report.update({
        "status": "complete",
        "training_input_rows": len(training),
        "training_input_end_exclusive": training_deadline.isoformat(),
        "training_input_last_at": training[-1].observed_at.isoformat(),
        "outer_holdout_start": holdout_start.isoformat(),
        "outer_holdout_rows": len(targets),
        "candidate_calibration": candidate_metrics.get("calibration"),
        "candidate_fit_rows": int(len(training) * 0.8),
    })
    def spatial_key(item: SpatialTrafficObservation) -> tuple[str, float, float]:
        return (item.corridor_id, round(item.latitude, 5), round(item.longitude, 5))

    by_location: dict[tuple[str, float, float], list[SpatialTrafficObservation]] = defaultdict(list)
    for item in eligible:
        by_location[spatial_key(item)].append(item)
    timestamps = {key: [item.observed_at for item in values]
                  for key, values in by_location.items()}
    for horizon in horizons_minutes:
        actual: list[float] = []
        forest: list[float] = []
        persistence: list[float] = []
        blended: list[float] = []
        contexts: list[SpatialPredictionContext] = []
        corridor_actual: dict[str, list[float]] = defaultdict(list)
        corridor_predicted: dict[str, list[float]] = defaultdict(list)
        target_corridors: list[str] = []
        skipped = 0
        for target in targets:
            origin = target.observed_at - timedelta(minutes=horizon)
            location = spatial_key(target)
            history = by_location[location]
            end = bisect_right(timestamps[location], origin)
            if not end or (origin - history[end - 1].observed_at).total_seconds() > max_current_age_minutes * 60:
                skipped += 1
                continue
            current = history[end - 1]
            # Bounded prior comparable history, never including the target.
            estimate = estimate_speed_ratio_congestion(current, history[max(0, end - 129):end - 1])
            actual.append(speed_ratio_congestion_score(target.speed_ratio))
            persistence.append(speed_ratio_congestion_score(current.speed_ratio))
            blended.append(estimate.congestion_score)
            target_local = target.local_observed_at
            contexts.append(replace(
                SpatialPredictionContext.from_observation(
                    current, corridor_idx=SUPPORTED_CORRIDORS[current.corridor_id],
                ),
                hour_float=target_local.hour + target_local.minute / 60.0 + target_local.second / 3600.0,
                day_of_week=target_local.weekday(),
            ))
            target_corridors.append(target.corridor_id)
        covered = 0
        for start in range(0, len(contexts), 4096):
            predictions = candidate.predict_spatial_batch(contexts[start:start + 4096])
            for index, prediction in enumerate(predictions, start):
                forest.append(prediction.mean_score)
                covered += prediction.p10_score <= actual[index] <= prediction.p90_score
                corridor_actual[target_corridors[index]].append(actual[index])
                corridor_predicted[target_corridors[index]].append(prediction.mean_score)
        report["horizons"].append({
            "horizon_minutes": horizon,
            "status": "complete" if actual else "insufficient_current_observations",
            "skipped_missing_or_stale_current": skipped,
            "forest": _metrics(actual, forest),
            "persistence": _metrics(actual, persistence),
            "current_history_blend": _metrics(actual, blended),
            "p10_p90_coverage": round(covered / len(actual), 4) if actual else None,
            "per_corridor": {key: _metrics(corridor_actual[key], corridor_predicted[key])
                             for key in sorted(corridor_actual)},
        })
    report["status"] = ("complete" if any(h["forest"]["samples"] for h in report["horizons"])
                        else "insufficient_current_observations")
    return report


def prediction_invariant_failures(predictions: Iterable[Any]) -> dict[str, int]:
    """Reusable numerical contract checks for stress runs and regressions."""
    failures: Counter[str] = Counter()
    for item in predictions:
        values = (item.mean_score, item.std_score, item.p10_score, item.p90_score,
                  item.risk_adjusted_score, item.expected_speed_ratio,
                  item.speed_ratio_std, item.upper_bound_delay_mins,
                  item.ferry_surge_adjustment_points)
        if not all(math.isfinite(value) for value in values):
            failures["non_finite"] += 1
        if not 0 <= item.p10_score <= item.mean_score <= item.p90_score <= 100:
            failures["quantile_order"] += 1
        if not item.mean_score <= item.risk_adjusted_score <= 100:
            failures["risk_bounds"] += 1
        if not 0.05 <= item.expected_speed_ratio <= 1:
            failures["speed_bounds"] += 1
        if item.std_score < 0 or item.speed_ratio_std < 0 or item.upper_bound_delay_mins < 0:
            failures["negative_uncertainty_or_delay"] += 1
        if not 0 <= item.ferry_surge_adjustment_points <= 15.001:
            failures["surge_cap"] += 1
        payload = item.router_payload(free_flow_travel_seconds=600.0)
        if payload["p90_speed_ratio"] > payload["expected_speed_ratio"] or payload["p90_delay_s"] < 0:
            failures["router_downside_bound"] += 1
    return dict(failures)
