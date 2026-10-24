"""Bounded, clock-aware forecast scenarios from the actual serving ensemble."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Optional

from models.congestion_model import SpatialPredictionContext, forecaster
from services import clock, ferry_schedule
from services.route_solver import CORRIDORS


HORIZON_HOURS = 24
WEATHER_LABELS = {0: "Clear", 1: "Rain", 2: "Storm"}
_CORRIDORS = {item["id"]: item for item in CORRIDORS}


class SimulationUnavailable(RuntimeError):
    """The running model could not produce a consistent forecast snapshot."""


def simulate_analytics(
    now: datetime,
    *,
    weather: int = 0,
    corridor_ids: Optional[list[str]] = None,
) -> dict[str, Any]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Simulation time must include a timezone.")
    if isinstance(weather, bool) or weather not in WEATHER_LABELS:
        raise ValueError("Weather must be clear, rain or storm.")
    selected = list(_CORRIDORS) if corridor_ids is None else list(corridor_ids)
    if not 1 <= len(selected) <= len(_CORRIDORS) or len(set(selected)) != len(selected) \
            or any(item not in _CORRIDORS for item in selected):
        raise ValueError("Select one to five unique known corridors.")
    local_now = now.astimezone(clock.BATAM_TZ).replace(second=0, microsecond=0)
    times = [local_now + timedelta(hours=offset) for offset in range(HORIZON_HOURS)]
    ferry_contexts = {}

    def context(corridor, timestamp, weather_code):
        key = (corridor["destination_port"], timestamp)
        if key not in ferry_contexts:
            _surge, sailing = ferry_schedule.ferry_surge_for_port(key[0], timestamp)
            ferry_contexts[key] = sailing["minutes_until_departure"] if sailing else None
        legacy = SpatialPredictionContext.from_legacy(
            timestamp.hour + timestamp.minute / 60,
            int(timestamp.weekday() >= 5), weather_code, 0, corridor["corridor_idx"],
        )
        return replace(
            legacy,
            day_of_week=timestamp.weekday(),
            minutes_until_ferry_departure=ferry_contexts[key],
            spatial_source="corridor_defaults_actual_clock_published_timetable",
            defaults_applied=tuple(item for item in legacy.defaults_applied if item != "representative_day_of_week"),
        )

    contexts = [context(_CORRIDORS[key], timestamp, weather) for key in selected for timestamp in times]
    contexts.extend(context(_CORRIDORS[key], local_now, code) for code in WEATHER_LABELS for key in selected)
    before = dict(forecaster.metrics)
    try:
        predictions = forecaster.predict_spatial_batch(contexts)
    except Exception as error:
        raise SimulationUnavailable("Forecast simulation is temporarily unavailable.") from error
    engine = forecaster.runtime_info()
    if before.get("last_trained_at") != engine.get("last_trained_at") \
            or len(predictions) != len(contexts):
        raise SimulationUnavailable("Forecast simulation is temporarily unavailable.")

    def row(timestamp, prediction):
        return {
            "timestamp": timestamp.isoformat(),
            "mean_score": prediction.mean_score,
            "p10_score": prediction.p10_score,
            "p90_score": prediction.p90_score,
            "std_score": prediction.std_score,
            "status": prediction.status,
            "ferry_surge_adjustment_points": prediction.ferry_surge_adjustment_points,
        }

    corridors = [{
        "corridor_id": key,
        "corridor_name": _CORRIDORS[key]["name"],
        "hourly_forecast": [row(timestamp, predictions[index * HORIZON_HOURS + offset])
                            for offset, timestamp in enumerate(times)],
    } for index, key in enumerate(selected)]

    def average(values, name):
        return round(sum(getattr(value, name) for value in values) / len(values), 3)

    hourly = [{
        "timestamp": timestamp.isoformat(),
        "mean_score": average([predictions[index * HORIZON_HOURS + offset] for index in range(len(selected))], "mean_score"),
        "p10_score": average([predictions[index * HORIZON_HOURS + offset] for index in range(len(selected))], "p10_score"),
        "p90_score": average([predictions[index * HORIZON_HOURS + offset] for index in range(len(selected))], "p90_score"),
        "corridor_count": len(selected),
    } for offset, timestamp in enumerate(times)]
    comparison_start = len(selected) * HORIZON_HOURS
    weather_comparison = []
    for code, label in WEATHER_LABELS.items():
        values = predictions[comparison_start + code * len(selected):comparison_start + (code + 1) * len(selected)]
        weather_comparison.append({
            "weather": code, "label": label,
            **{field: average(values, field) for field in ("mean_score", "p10_score", "p90_score")},
        })
    return {
        "horizon_hours": HORIZON_HOURS, "step_minutes": 60,
        "weather": weather, "timezone": "Asia/Jakarta",
        "input_time": local_now.isoformat(),
        "prediction_count": len(predictions),
        "engine": engine,
        "hourly_forecast": hourly, "corridors": corridors,
        "weather_comparison": weather_comparison,
        "aggregate_quantile_method": "mean_of_corridor_quantiles_not_a_joint_network_interval",
        "model": {key: before.get(key) for key in (
            "last_trained_at", "training_data_source", "validation_scope", "evaluation_scope",
            "observed_training_rows", "r2_score", "mae", "rmse",
        )},
        "limitations": [
            "Model simulation, not an observed traffic archive or a current weather report.",
            "Uses the running serving ensemble with actual clock/calendar, chosen weather and published ferry proximity.",
            "Road spatial defaults are corridor assumptions, not measured road geometry.",
            "P10/P90 describe tree dispersion; synthetic holdout intervals under-cover and field calibration is unverified.",
            "Aggregate quantiles are corridor averages, not a joint network confidence interval.",
        ],
    }
