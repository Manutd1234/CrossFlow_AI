"""Observed-only analytics over one bounded, validated spatial snapshot.

Reading this view never records a provider response or creates modelled rows.
The legacy synthetic history table is deliberately outside this data path.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from itertools import islice
from typing import Any

from services import clock, historical_store, routing_intelligence_store
from services.congestion_math import speed_ratio_congestion_score
from services.traffic_observations import validate_spatial_observation


MAX_HISTORY_SNAPSHOT_ROWS = 10_000
_SHARED_STORE = routing_intelligence_store.SupabaseRoutingIntelligenceStore()


class AnalyticsHistoryUnavailable(RuntimeError):
    """The observed-history snapshot could not be read or validated safely."""


def get_observed_history(
    corridor_id: str,
    days: int,
    now: datetime,
    *,
    shared_store=None,
) -> dict[str, Any]:
    """Aggregate measurements for Batam calendar days, including today.

    Selection is explicitly limited to the newest validated spatial rows.
    Empty buckets represent missing observations, never an estimated score.
    """
    if corridor_id not in historical_store.CORRIDOR_IDS:
        raise ValueError("Unknown corridor.")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 30:
        raise ValueError("History window must be 1..30 days.")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("History reference time must include a timezone.")
    local_now = now.astimezone(clock.BATAM_TZ)
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    try:
        shared = routing_intelligence_store.shared_store_enabled()
        if shared:
            if not routing_intelligence_store.configured():
                raise AnalyticsHistoryUnavailable("Observed history is unavailable.")
            store = shared_store if shared_store is not None else _SHARED_STORE
            records = store.get_spatial_training_dataset(limit=MAX_HISTORY_SNAPSHOT_ROWS)
            storage = {
                "engine": "supabase_postgresql",
                "durability": "shared_supabase",
                "durable": True,
                "shared_across_instances": True,
                "fallback_to_memory": False,
            }
        else:
            records = historical_store.get_spatial_training_dataset(
                limit=MAX_HISTORY_SNAPSHOT_ROWS, now=now,
            )
            storage = historical_store.get_storage_metadata()
        snapshot = tuple(islice(iter(records), MAX_HISTORY_SNAPSHOT_ROWS + 1))
        if len(snapshot) > MAX_HISTORY_SNAPSHOT_ROWS:
            raise AnalyticsHistoryUnavailable("Observed history is unavailable.")
        unique = {}
        for record in snapshot:
            item = validate_spatial_observation(record)
            previous = unique.get(item.observation_key)
            if previous is not None and previous.immutable_payload() != item.immutable_payload():
                raise AnalyticsHistoryUnavailable("Observed history is unavailable.")
            unique[item.observation_key] = item
    except Exception:
        # Neither SQL/transport details nor source-controlled values belong in
        # a public error. The API maps this typed failure to an uncacheable503.
        raise AnalyticsHistoryUnavailable("Observed history is temporarily unavailable.") from None

    selected = [
        item for item in unique.values()
        if item.observed and item.corridor_id == corridor_id
        and start <= item.observed_at <= now
    ]
    hourly: dict[int, list[float]] = defaultdict(list)
    daily: dict[str, list[float]] = defaultdict(list)
    for item in selected:
        civil_time = item.observed_at.astimezone(clock.BATAM_TZ)
        score = speed_ratio_congestion_score(item.speed_ratio)
        hourly[civil_time.hour].append(score)
        daily[civil_time.date().isoformat()].append(score)
    source_counts = dict(sorted(Counter(item.source for item in selected).items()))
    latest = max((item.observed_at for item in selected), default=None)
    age = max(0, int((now - latest).total_seconds())) if latest is not None else None
    freshness = "empty" if age is None else "current" if age <= 15 * 60 else "recent" if age <= 24 * 60 * 60 else "stale"
    return {
        "corridor_id": corridor_id,
        "days_requested": days,
        "hourly_profile": [
            {
                "hour": hour,
                "avg_score": round(sum(hourly[hour]) / len(hourly[hour]), 1) if hourly[hour] else None,
                "sample_count": len(hourly[hour]),
            }
            for hour in range(24)
        ],
        "weekly_trend": [
            {"date": day, "avg_score": round(sum(values) / len(values), 1), "sample_count": len(values)}
            for day, values in sorted(daily.items())
        ],
        "history_metadata": {
            "window_days": days,
            "window_basis": "Batam calendar days including the current day",
            "window_start_at": start.isoformat(),
            "window_end_at": now.astimezone(timezone.utc).isoformat(),
            "selection_scope": "observed_only",
            "snapshot_selection": "latest_bounded_validated_spatial_rows_then_corridor_window_filter",
            "snapshot_max_rows": MAX_HISTORY_SNAPSHOT_ROWS,
            "snapshot_loaded_rows": len(snapshot),
            "snapshot_may_be_truncated": len(snapshot) == MAX_HISTORY_SNAPSHOT_ROWS,
            "sample_count": len(selected),
            "observed": bool(selected),
            "contains_observed_samples": bool(selected),
            "source_counts": source_counts,
            "sources": {
                source: {"sample_count": count, "observed": True}
                for source, count in source_counts.items()
            },
            "latest_sample_at": latest.isoformat() if latest is not None else None,
            "latest_sample_age_seconds": age,
            "freshness": freshness,
            "freshness_basis": "latest observed sample in the selected Batam calendar-day window",
            "storage": storage,
            "synthetic_seed": {
                "source": historical_store.SYNTHETIC_SOURCE,
                "version": historical_store.SYNTHETIC_SEED_VERSION,
                "days": historical_store.SYNTHETIC_SEED_DAYS,
                "timezone": "WIB (UTC+07:00)",
                "generated_for_date": None,
                "observed": False,
                "included": False,
            },
        },
    }
