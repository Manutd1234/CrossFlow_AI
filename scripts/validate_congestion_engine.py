"""Reproducible local stress test and chronological congestion backtest.

Run with the project's venv. No network, database writes, model publication,
or background schedule. Output distinguishes invariants from real accuracy.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from itertools import islice
import json
from pathlib import Path
import sqlite3
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from models.congestion_model import CongestionForecaster, SpatialPredictionContext
from services.congestion_validation import backtest_congestion, prediction_invariant_failures
from services.traffic_observations import ROAD_CLASSES, SpatialTrafficObservation


def stress_test(*, scenarios: int, seed: int, batch_size: int) -> dict:
    if scenarios < 2 or scenarios % 2 or not 2 <= batch_size <= 8192 or batch_size % 2:
        raise ValueError("Scenarios and batch size must be positive even integers; batch size <=8192.")
    rng = np.random.default_rng(seed)
    model = CongestionForecaster()
    failures: Counter[str] = Counter()
    tested = 0
    start = time.monotonic()
    road_classes = sorted(ROAD_CLASSES)
    pair_counts: Counter[str] = Counter()
    for offset in range(0, scenarios, batch_size):
        contexts = []
        comparisons = []
        for index in range(min(batch_size, scenarios - offset) // 2):
            context = SpatialPredictionContext(
                hour_float=float(rng.uniform(0, 24)),
                day_of_week=int(rng.integers(0, 7)),
                weather=int(rng.integers(0, 3)),
                corridor_idx=int(rng.integers(0, 5)),
                road_class=road_classes[int(rng.integers(0, len(road_classes)))],
                capacity_vph=float(rng.uniform(100, 8_000)),
                terminal_distance_km=float(rng.uniform(0, 30)),
                free_flow_speed_kph=float(rng.uniform(5, 130)),
                risk_aversion=0.0,
            )
            kind = (offset // 2 + index) % 3
            if kind == 0:
                contexts.extend((context, replace(context, risk_aversion=3.0)))
                comparisons.append("risk_monotonicity")
            elif kind == 1:
                contexts.extend((context, replace(context, minutes_until_ferry_departure=0.0)))
                comparisons.append("surge_monotonicity")
            else:
                imminent = replace(context, minutes_until_ferry_departure=0.0)
                contexts.extend((imminent, replace(imminent, minutes_until_ferry_departure=45.0)))
                comparisons.append("surge_decay")
        predictions = model.predict_spatial_batch(contexts)
        failures.update(prediction_invariant_failures(predictions))
        for index, kind in enumerate(comparisons):
            left, right = predictions[2 * index:2 * index + 2]
            pair_counts[kind] += 1
            if kind == "risk_monotonicity":
                valid = left.mean_score == right.mean_score and right.risk_adjusted_score >= left.risk_adjusted_score
            elif kind == "surge_monotonicity":
                valid = right.mean_score >= left.mean_score and right.p90_score >= left.p90_score
            else:
                valid = left.mean_score >= right.mean_score and left.p90_score >= right.p90_score
            if not valid:
                failures[kind] += 1
        tested += len(predictions)
        if tested % 100_000 < batch_size or tested == scenarios:
            print(f"Stress progress: {tested:,}/{scenarios:,}; failures={sum(failures.values())}", flush=True)
        if failures:
            break
    duration = time.monotonic() - start
    return {
        "status": "passed" if tested == scenarios and not failures else "failed",
        "requested_scenarios": scenarios,
        "evaluated_scenarios": tested,
        "seed": seed,
        "batch_size": batch_size,
        "elapsed_seconds": round(duration, 3),
        "scenarios_per_second": round(tested / duration, 1),
        "paired_checks": dict(pair_counts),
        "failures": dict(failures),
        "scope": "randomized production-forest inference and router-cost invariants, not end-to-end journey solves",
        "coverage": {"corridors": 5, "road_classes": road_classes,
                     "days": 7, "weather_states": 3,
                     "capacity_vph_range": [100, 8000], "free_flow_kph_range": [5, 130],
                     "terminal_distance_km_range": [0, 30],
                     "properties": ["finite outputs", "ordered quantiles", "bounded speed ratios",
                                    "nonnegative delays", "risk monotonicity", "ferry surge cap and decay",
                                    "P90 downside no faster than expected speed"]},
        "measured_traffic_accuracy_established": False,
    }


def load_csv(path: Path) -> list[SpatialTrafficObservation]:
    records = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in islice(csv.DictReader(handle), 20_001):
            records.append(SpatialTrafficObservation.create(
                corridor_id=row["corridor_id"],
                observed_at=datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00")),
                latitude=float(row["latitude"]), longitude=float(row["longitude"]),
                actual_speed_kph=float(row["actual_speed_kph"]),
                free_flow_speed_kph=float(row["free_flow_speed_kph"]),
                source=row["source"], reviewed=row.get("reviewed", "").lower() == "true",
                road_class=row.get("road_class") or "unclassified",
                capacity_vph=float(row["capacity_vph"]) if row.get("capacity_vph") else None,
                terminal_distance_km=float(row["terminal_distance_km"]) if row.get("terminal_distance_km") else None,
                upstream_event_id=row.get("upstream_event_id") or None,
            ))
    return records


def load_history_read_only(path: Path) -> list[SpatialTrafficObservation]:
    """Read existing typed observations without creating/seeding the database."""
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT * FROM spatial_observations ORDER BY observed_at_us DESC LIMIT 20001"
        ).fetchall()
    finally:
        con.close()
    records = []
    for row in rows:
        records.append(SpatialTrafficObservation(
            observation_key=row["observation_key"], corridor_id=row["corridor"],
            observed_at=datetime.fromtimestamp(row["observed_at_us"] / 1_000_000, tz=timezone.utc),
            latitude=row["latitude"], longitude=row["longitude"],
            actual_speed_kph=row["actual_speed_kph"], free_flow_speed_kph=row["free_flow_speed_kph"],
            source=row["source"], provenance=row["provenance"], confidence=row["confidence"],
            observed=bool(row["observed"]), reviewed=bool(row["reviewed"]),
            road_class=row["road_class"], capacity_vph=row["capacity_vph"],
            terminal_distance_km=row["terminal_distance_km"],
            local_timezone_offset_minutes=row["local_timezone_offset_minutes"],
            upstream_event_id=row["upstream_event_id"],
        ))
    return records


def synthetic_replay(seed: int) -> list[SpatialTrafficObservation]:
    """Independent labelled fixture, never ingested or called measured data."""
    rng = np.random.default_rng(seed)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    records = []
    for step in range(7 * 24 * 4):
        when = start + timedelta(minutes=15 * step)
        local_hour = (when.hour + 7 + when.minute / 60) % 24
        for corridor in range(5):
            peak = (22 * np.exp(-((local_hour - 8) / 1.5) ** 2)
                    + 30 * np.exp(-((local_hour - 18) / 1.8) ** 2))
            # Not the model's generator: serial demand, slow trend, sinusoidal
            # mid-day component and independent noise expose forecast errors.
            score = float(np.clip(18 + 0.8 * corridor + peak + 8 * np.sin(step / 47)
                                  + rng.normal(0, 4), 0, 95))
            records.append(SpatialTrafficObservation.create(
                corridor_id=f"corridor-{corridor + 1}", observed_at=when,
                latitude=1.10 + corridor * 0.01, longitude=104.0,
                actual_speed_kph=50 * (1 - score / 100), free_flow_speed_kph=50,
                road_class="primary", source="synthetic",
                upstream_event_id=f"replay-{step}-{corridor}",
            ))
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=20261023)
    parser.add_argument("--batch-size", type=int, default=4096)
    parser.add_argument("--history-db", type=Path)
    parser.add_argument("--observations-csv", type=Path)
    parser.add_argument("--synthetic-replay", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
              "engine_source_sha256": hashlib.sha256((ROOT / "backend/models/congestion_model.py").read_bytes()).hexdigest(),
              "stress": stress_test(scenarios=args.scenarios, seed=args.seed, batch_size=args.batch_size)}
    if args.history_db:
        report["local_observed_backtest"] = backtest_congestion(load_history_read_only(args.history_db))
    if args.observations_csv:
        report["csv_backtest"] = backtest_congestion(load_csv(args.observations_csv))
    if args.synthetic_replay:
        report["synthetic_replay_backtest"] = backtest_congestion(synthetic_replay(args.seed), include_modelled=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(f"Report saved to {args.output}")
    return 0 if report["stress"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
