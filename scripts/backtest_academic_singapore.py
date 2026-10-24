#!/usr/bin/env python3
"""Offline, geographically separate benchmark on published LTA speed bands.

Reads inert numeric/JSON data only. Never imports publisher code, loads pickle,
writes the app's traffic database, or publishes a serving model. Scores are in
ordinal band units, not km/h and not evidence of Batam forecasting accuracy.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import tempfile

import numpy as np
from sklearn.ensemble import RandomForestRegressor

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "https://github.com/EYLeong/traffic_prediction"
EXPECTED_SHA256 = {
    "dataset.npy": "83a54790c3e7e4b16ca5dab1510b614e559abf1a3ad3ff760d39b04926991d0f",
    "metadata.json": "74c43f17282d3c62c7fbbcca0f783ab739a663a419ff636d8b170f60ca47faca",
    "timestamps.json": "85fdb55c15ae9d47aca36c825425e738bcd1ea5fd28312f7f7f7e49479ee009f",
}
HORIZONS = (15, 30, 60)
STEPS = np.array((3, 6, 12))
LAGS = (0, 1, 3, 6, 12)


def load_source(directory: Path):
    for name, checksum in EXPECTED_SHA256.items():
        path = directory / name
        if path.stat().st_size > 12 * 1024 * 1024:
            raise ValueError("Academic input exceeds the supported size.")
        if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise ValueError(f"Reviewed source checksum differs: {name}")
    array = np.load(directory / "dataset.npy", allow_pickle=False)
    timestamps = json.loads((directory / "timestamps.json").read_text())
    metadata = json.loads((directory / "metadata.json").read_text())
    if array.shape != (53, 3, 7497) or not np.isfinite(array).all():
        raise ValueError("Unexpected reviewed academic data shape or values.")
    dates = [datetime.strptime(timestamps[str(i)], "%a_%b_%d_%Y_%H_%M_%S")
             for i in range(len(timestamps))]
    # The source has no timezone. Use a source-clock axis for relative timing;
    # never silently convert the filenames into claimed UTC/SGT observations.
    seconds = np.array([(d - datetime(1970, 1, 1)).total_seconds()
                        for d in dates], dtype=np.int64)
    if len(dates) != array.shape[2] or not (np.diff(seconds) > 0).all():
        raise ValueError("Source timestamps are incomplete or not increasing.")
    if set(metadata) != {str(i) for i in range(53)}:
        raise ValueError("Road metadata does not match source entities.")
    for item in metadata.values():
        for field in ("start_pos", "end_pos"):
            lat, lng = map(float, item[field].split())
            if not (1.20 < lat < 1.48 and 103.60 < lng < 104.10):
                raise ValueError("Road metadata is outside Singapore.")
    return array, dates, seconds, metadata


def valid_origins(bands: np.ndarray, seconds: np.ndarray) -> np.ndarray:
    if (bands.ndim != 2 or bands.shape[1] != len(seconds)
            or not np.isfinite(bands).all()
            or not ((bands >= 1) & (bands <= 8) & (bands == np.rint(bands))).all()):
        raise ValueError("Speed-band labels must be original integer bands 1–8.")
    if not (np.diff(seconds) > 0).all():
        raise ValueError("Observation clock must be strictly increasing.")
    # Keep only continuous observed windows. Do not interpolate a missing
    # snapshot or call a distant row a fifteen-minute target by its row number.
    return np.array([i for i in range(12, len(seconds) - 12)
                     if (np.abs(np.diff(seconds[i-12:i+13]) - 300) <= 45).all()
                     and (np.abs(seconds[i+STEPS] - seconds[i] - STEPS*300) <= 60).all()], dtype=int)


def partitions(origins: np.ndarray, seconds: np.ndarray):
    if len(origins) < 100:
        raise ValueError("Insufficient continuous windows for this benchmark.")
    fit_boundary = int(len(seconds) * .60)
    test_boundary = int(len(seconds) * .80)
    fit = origins[seconds[origins + STEPS[-1]] < seconds[fit_boundary]]
    calibration = origins[(seconds[origins] >= seconds[fit_boundary])
                          & (seconds[origins + STEPS[-1]] < seconds[test_boundary])]
    test = origins[seconds[origins] >= seconds[test_boundary]]
    if min(map(len, (fit, calibration, test))) < 20:
        raise ValueError("A chronological partition is too short.")
    assert seconds[fit[-1] + STEPS[-1]] < seconds[calibration[0]]
    assert seconds[calibration[-1] + STEPS[-1]] < seconds[test[0]]
    return fit, calibration, test


def examples(array: np.ndarray, dates, origins: np.ndarray):
    bands = array[:, 0, :]
    roads = bands.shape[0]
    # Source clock phase only: its timezone is not independently documented.
    clock = np.array([dates[i].hour * 60 + dates[i].minute for i in origins])
    weekday = np.array([dates[i].weekday() for i in origins])
    columns = [bands[:, origins-lag].T.reshape(-1) for lag in LAGS]
    columns.extend([
        np.tile(np.arange(roads), len(origins)),
        array[:, 1, origins].T.reshape(-1),
        array[:, 2, origins].T.reshape(-1),
        np.repeat(np.sin(2*np.pi*clock/1440), roads),
        np.repeat(np.cos(2*np.pi*clock/1440), roads),
        np.repeat(np.sin(2*np.pi*weekday/7), roads),
        np.repeat(np.cos(2*np.pi*weekday/7), roads),
    ])
    x = np.column_stack(columns).astype(np.float32)
    y = np.column_stack([bands[:, origins+step].T.reshape(-1) for step in STEPS])
    persistence = np.repeat(bands[:, origins].T.reshape(-1)[:, None], 3, axis=1)
    return x, y, persistence


def scores(actual, predicted):
    difference = np.asarray(predicted) - actual
    return {
        "samples": int(len(actual)),
        "mae_band_units": round(float(np.abs(difference).mean()), 6),
        "rmse_band_units": round(float(np.sqrt((difference**2).mean())), 6),
        "rounded_band_accuracy": round(float((np.rint(predicted) == actual).mean()), 6),
        "within_one_band": round(float((np.abs(np.rint(predicted) - actual) <= 1).mean()), 6),
    }


def run(directory: Path, *, max_training_rows: int = 80000):
    array, dates, seconds, metadata = load_source(directory)
    bands = array[:, 0, :]
    origins = valid_origins(bands, seconds)
    full_fit, calibration, test = partitions(origins, seconds)
    stride = max(1, math.ceil(len(full_fit) * len(metadata) / max_training_rows))
    fit = full_fit[::stride]
    x_fit, y_fit, _ = examples(array, dates, fit)
    x_cal, y_cal, _ = examples(array, dates, calibration)
    x_test, y_test, persistence = examples(array, dates, test)
    parameters = dict(n_estimators=80, max_depth=12, min_samples_leaf=12,
                      random_state=42, n_jobs=1)
    forest = RandomForestRegressor(**parameters)
    forest.fit(x_fit, y_fit)
    cal_prediction = forest.predict(x_cal)
    prediction = forest.predict(x_test)
    rows = []
    # Yesterday's comparison uses only points available before the forecast
    # origin and reports the common matched subset separately.
    for column, (horizon, step) in enumerate(zip(HORIZONS, STEPS, strict=True)):
        residual = np.abs(y_cal[:, column] - cal_prediction[:, column])
        q_level = min(1.0, math.ceil((len(residual)+1)*.8)/len(residual))
        radius = float(np.quantile(residual, q_level, method="higher"))
        low = np.maximum(1, prediction[:, column]-radius)
        high = np.minimum(8, prediction[:, column]+radius)
        desired = seconds[test+step] - 86400
        indexes = np.searchsorted(seconds, desired)
        indexes = np.clip(indexes, 1, len(seconds)-1)
        previous = indexes-1
        indexes = np.where(np.abs(seconds[previous]-desired) < np.abs(seconds[indexes]-desired), previous, indexes)
        eligible = (np.abs(seconds[indexes]-desired) <= 60) & (seconds[indexes] <= seconds[test])
        common = np.repeat(eligible, len(metadata))
        historical = bands[:, indexes[eligible]].T.reshape(-1)
        row = {
            "horizon_minutes": horizon,
            "random_forest": scores(y_test[:, column], prediction[:, column]),
            "persistence": scores(y_test[:, column], persistence[:, column]),
            "interval_nominal_coverage": .8,
            "interval_empirical_coverage": round(float(((y_test[:, column] >= low) & (y_test[:, column] <= high)).mean()), 6),
            "calibration_radius_band_units": round(radius, 6),
            "prior_day_common_subset": {
                "random_forest": scores(y_test[common, column], prediction[common, column]),
                "persistence": scores(y_test[common, column], persistence[common, column]),
                "prior_day_same_source_clock": scores(y_test[common, column], historical),
            },
            "per_road": [{
                "link_id": metadata[str(road)]["LinkID"],
                "road_name": metadata[str(road)]["RoadName"],
                "random_forest": scores(y_test[road::len(metadata), column], prediction[road::len(metadata), column]),
                "persistence": scores(y_test[road::len(metadata), column], persistence[road::len(metadata), column]),
            } for road in range(len(metadata))],
        }
        rows.append(row)
    def window(group):
        return {"forecast_origins":len(group), "rows":len(group)*len(metadata),
                "first_origin_source_clock":dates[group[0]].isoformat(),
                "last_origin_source_clock":dates[group[-1]].isoformat(),
                "latest_target_source_clock":dates[group[-1]+STEPS[-1]].isoformat()}
    return {
        "schema_version":1, "evaluated_at":datetime.now(timezone.utc).isoformat(),
        "scope":"independent Singapore academic benchmark; not Batam field validation",
        "source_url":SOURCE, "data_sha256":EXPECTED_SHA256,
        "source_signal":"LTA Traffic Speed Bands; ordinal integer labels 1–8, not exact km/h",
        "data_provider":"Singapore Land Transport Authority, collected by SUTD student research project",
        "source_license":"LTA API datasets governed by Singapore Open Data Licence1.0; publisher code not reused",
        "license_url":"https://datamall.lta.gov.sg/content/datamall/en/SingaporeOpenDataLicence.html",
        "source_provenance":"Publisher directly copies SpeedBand from snapshots in inspected get_features; original API payloads unavailable for independent revalidation.",
        "timestamp_basis":"Source filenames have no timezone; relative source-clock chronology retained; UTC/SGT offset not asserted.",
        "source_first_timestamp":dates[0].isoformat(), "source_last_timestamp":dates[-1].isoformat(),
        "source_calendar_days":len({d.date() for d in dates}), "roads":len(metadata),
        "snapshots":len(dates), "speed_band_records":int(bands.size),
        "continuous_eligible_origins":len(origins),
        "chronology":{"fit":window(fit),"calibration":window(calibration),"test":window(test),
                      "maximum_forecast_minutes":60,"training_origin_stride":stride,
                      "latest_fit_label_precedes_first_calibration_origin":True,
                      "latest_calibration_label_precedes_first_test_origin":True},
        "model":{"kind":"RandomForestRegressor","parameters":parameters,"features":"Five prior bands, road identity/category/length, published source-clock phase and weekday; no future target/context embeddings."},
        "results":rows,
        "limitations":["27 days of old Singapore data from53 selected road links; no Batam observations in this benchmark.",
                       "The dataset represents speed intervals, so no exact-speed or road-travel-time accuracy is claimed.",
                       "Timezone and observation availability delay were not supplied; benchmark assumes each snapshot was available at its source timestamp.",
                       "Training is bounded and hyperparameters were fixed before examining held-out scores; no claim of optimality.",
                       "Temporal/road dependence means empirical interval coverage has no distribution-free guarantee.",
                       "No raw vehicle counts, weather or ferry impacts were learned or validated."],
        "synthetic_training_rows":0,"external_code_or_pickle_executed":False,
        "application_history_modified":False,"serving_model_replaced":False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT/"backend/data/traffic-imports/singapore-sutd")
    parser.add_argument("--output", type=Path, default=ROOT/"docs/verification/singapore-academic-backtest.json")
    args = parser.parse_args()
    report = run(args.data)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(report, indent=2, allow_nan=False)+"\n"
    with tempfile.NamedTemporaryFile(mode="w", dir=args.output.parent, delete=False) as staged:
        staged.write(encoded)
        temporary = Path(staged.name)
    temporary.replace(args.output)
    print(json.dumps({"report":str(args.output),"scope":report["scope"],
                      "records":report["speed_band_records"],
                      "results":[{k:v for k,v in row.items() if k not in {"per_road","prior_day_common_subset"}} for row in report["results"]],
                      "serving_model_replaced":False}, indent=2))


if __name__ == "__main__":
    main()
