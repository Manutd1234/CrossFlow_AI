#!/usr/bin/env python3
"""Exploratory next-survey-day benchmark on 48 published Batam speed means.

No synthetic labels, random row split, interpolation, corridor remapping, model
activation, or application database writes. This small single-site diagnostic
cannot establish network or 15/30/60-minute forecast accuracy.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import math
from pathlib import Path

import numpy as np
from sklearn.ensemble import RandomForestRegressor

ROOT = Path(__file__).resolve().parents[1]
CSV_SHA256 = "8dceaa2b73169ff33aded53fc9ca7a86274f3aa72cdd4ec60f4236b3cae68e44"
PARAMETERS = dict(n_estimators=80, max_depth=3, min_samples_leaf=2, random_state=42, n_jobs=1)


def load_source(directory: Path):
    source = directory / "batam-gajah-mada-2023.csv"
    if source.stat().st_size > 100_000:
        raise ValueError("Survey exceeds reviewed size")
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != CSV_SHA256:
        raise ValueError("Survey checksum differs from the reviewed transcription")
    manifest = json.loads((directory / "batam-gajah-mada-2023.manifest.json").read_text())
    if manifest["csv_sha256"] != CSV_SHA256:
        raise ValueError("Manifest differs from reviewed transcription")
    rows, seen = [], set()
    for original in csv.DictReader(io.StringIO(raw.decode())):
        start = datetime.fromisoformat(original["interval_start"])
        end = datetime.fromisoformat(original["interval_end"])
        speed = float(original["reported_speed_kph"])
        direction = original["direction_as_published"]
        key = (start, direction)
        if (key in seen or start.utcoffset() != timedelta(hours=7)
                or end - start != timedelta(minutes=30)
                or direction not in ("from_east", "from_west")
                or not math.isfinite(speed) or not 0 < speed <= 200):
            raise ValueError("Invalid published speed observation")
        seen.add(key)
        rows.append(dict(start=start, end=end, speed=speed, direction=direction,
                         quality_flag=original["quality_flag"]))
    if len(rows) != 48 or sorted({r["start"].date().isoformat() for r in rows}) != ["2023-02-16", "2023-02-17", "2023-02-18"]:
        raise ValueError("Survey no longer matches its reviewed scope")
    return sorted(rows, key=lambda r: (r["start"], r["direction"])), manifest


def features(rows):
    # All inputs are knowable before the held-out day starts. Do not use the
    # target interval's measured travel time, volume, or derived density.
    phase = np.array([(r["start"].hour * 60 + r["start"].minute) * 2 * np.pi / 1440 for r in rows])
    return np.column_stack((np.sin(phase), np.cos(phase),
                            [int(r["direction"] == "from_west") for r in rows]))


def day_folds(rows):
    dates = sorted({r["start"].date() for r in rows})
    for day in dates[1:]:
        train = [r for r in rows if r["start"].date() < day]
        test = [r for r in rows if r["start"].date() == day]
        origin = datetime.combine(day, datetime.min.time(), tzinfo=test[0]["start"].tzinfo)
        if max(r["end"] for r in train) >= origin:
            raise ValueError("A training target crosses the forecast origin")
        yield origin, train, test


def metrics(actual, prediction):
    errors = np.asarray(prediction) - np.asarray(actual)
    return dict(samples=len(errors), mae_kph=float(np.abs(errors).mean()),
                rmse_kph=float(np.sqrt(np.square(errors).mean())))


def evaluate(rows, manifest):
    predictions, folds = [], []
    for origin, train, test in day_folds(rows):
        model = RandomForestRegressor(**PARAMETERS)
        model.fit(features(train), [r["speed"] for r in train])
        predicted = model.predict(features(test))
        previous = {(r["start"].time(), r["direction"]): r["speed"] for r in train
                    if r["start"].date() == origin.date() - timedelta(days=1)}
        actual, persistence, historical_mean = [], [], []
        for row, forecast in zip(test, predicted, strict=True):
            baseline = previous[(row["start"].time(), row["direction"])]
            actual.append(row["speed"])
            persistence.append(baseline)
            slot_mean = float(np.mean([r["speed"] for r in train if r["start"].time() == row["start"].time()
                                      and r["direction"] == row["direction"]]))
            historical_mean.append(slot_mean)
            predictions.append(dict(interval_start=row["start"].isoformat(), direction=row["direction"],
                                    actual_kph=row["speed"], random_forest_kph=float(forecast),
                                    previous_survey_day_kph=baseline, historical_slot_mean_kph=slot_mean))
        folds.append(dict(test_date=origin.date().isoformat(), forecast_origin=origin.isoformat(),
                          latest_training_interval_end=max(r["end"] for r in train).isoformat(),
                          training_rows=len(train), test_rows=len(test),
                          random_forest=metrics(actual, predicted),
                          previous_survey_day=metrics(actual, persistence),
                          historical_slot_mean=metrics(actual, historical_mean)))
    actual = [r["actual_kph"] for r in predictions]
    forest = metrics(actual, [r["random_forest_kph"] for r in predictions])
    baseline = metrics(actual, [r["previous_survey_day_kph"] for r in predictions])
    return dict(
        scope="exploratory_single_batam_site_next_survey_day", evaluated_at=datetime.now(timezone.utc).isoformat(),
        source_url=manifest["source_url"], source_pdf_url=manifest["source_pdf_url"], csv_sha256=CSV_SHA256,
        source_license=manifest["license"], license_url=manifest["license_url"], attribution=manifest["attribution"],
        source_rows=len(rows), source_calendar_days=3, synthetic_training_rows=0,
        serving_model_replaced=False, application_history_modified=False, canonical_corridor_mapping=None,
        unit="km/h", model_parameters=PARAMETERS, folds=folds, predictions=predictions,
        random_forest=forest, previous_survey_day=baseline,
        historical_slot_mean=metrics(actual, [r["historical_slot_mean_kph"] for r in predictions]),
        beats_previous_survey_day=forest["mae_kph"] < baseline["mae_kph"],
        promotion_eligible=False,
        limitations=[
            "48 published means, one 100m site, two directions, three days and peak windows only; no vehicle-level samples.",
            "Two expanding-day folds; 32 held-out interval means are correlated, not independent vehicle samples.",
            "Fixed model parameters; no tuning on either test day and no test data used during fitting.",
            "Reported speeds are retained, including one flagged speed/inverse-mean-time discrepancy; raw samples cannot resolve it.",
            "Forecasts are made at midnight for the next survey day, not 15/30/60-minute operational predictions.",
            "Exact geometry and canonical corridor mapping remain unverified. No network accuracy, uncertainty coverage or deployment claim.",
            "Insufficient geographic and temporal coverage for serving-model promotion, regardless of benchmark score.",
        ],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "backend/data/research")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/verification/batam-site-backtest.json")
    args = parser.parse_args()
    rows, manifest = load_source(args.data_dir)
    report = evaluate(rows, manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps({key: report[key] for key in ("scope", "source_rows", "random_forest", "previous_survey_day", "promotion_eligible")}))


if __name__ == "__main__":
    main()
