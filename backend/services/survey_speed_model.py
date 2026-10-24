"""Data-only inference for one retrospective Batam survey-speed experiment.

This artifact is deliberately independent of the serving congestion forecaster.
It predicts published interval mean speed at one surveyed site, on the single
held-out survey date, and cannot provide network forecasts or severity labels.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any

import numpy as np

SITE_ID = "batam-gajah-mada-kemilau-bangsa-published-2023"
SOURCE_PDF_SHA256 = "2e37e52e90ea8450359e3d5391e7a1626a304b85d3c8cf59808e1513d4eeb681"
SOURCE_CSV_SHA256 = "8dceaa2b73169ff33aded53fc9ca7a86274f3aa72cdd4ec60f4236b3cae68e44"
FEATURE_NAMES = ("hour_sin", "hour_cos", "direction_from_west")
DIRECTIONS = ("from_east", "from_west")
START_TIMES = ("07:00", "07:30", "08:00", "08:30", "16:00", "16:30", "17:00", "17:30")
FORECAST_DATE = "2023-02-18"
MAX_BYTES = 2_000_000
MAX_TREES = 64
MAX_TREE_NODES = 4096
SCOPE = "retrospective_single_site_next_survey_day_speed_diagnostic"


def features(interval_start: datetime, direction: str) -> list[float]:
    """Only clock time and published direction; never a target measurement."""
    if interval_start.utcoffset() != timedelta(hours=7):
        raise ValueError("Survey time must carry the published WIB (+07:00) offset.")
    if direction not in DIRECTIONS:
        raise ValueError("Unsupported published survey direction.")
    angle = (interval_start.hour * 60 + interval_start.minute) * 2 * math.pi / 1440
    return [math.sin(angle), math.cos(angle), float(direction == "from_west")]


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _number(value: Any, *, limit: float = 1_000_000) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > limit:
        raise ValueError("Artifact contains an invalid finite numeric value.")
    return float(value)


def _validate_model(model: Any) -> None:
    if not isinstance(model, dict):
        raise ValueError("Invalid numeric model.")
    kind = model.get("kind")
    if kind in {"previous_survey_day_slot", "historical_slot_mean"}:
        if set(model) != {"kind"}:
            raise ValueError("Invalid causal slot model schema.")
    elif kind == "direction_mean":
        if set(model) != {"kind", "means"} or not isinstance(model["means"], dict) or set(model["means"]) != set(DIRECTIONS):
            raise ValueError("Invalid directional mean model.")
        for value in model["means"].values():
            if not 0 <= _number(value) <= 200:
                raise ValueError("Invalid directional mean speed.")
    elif kind == "linear":
        if set(model) != {"kind", "coefficients", "intercept"} or not isinstance(model["coefficients"], list) or len(model["coefficients"]) != len(FEATURE_NAMES):
            raise ValueError("Invalid linear model schema.")
        for value in [*model["coefficients"], model["intercept"]]:
            _number(value)
    elif kind == "tree_ensemble":
        if set(model) != {"kind", "trees"} or not isinstance(model["trees"], list) or not 1 <= len(model["trees"]) <= MAX_TREES:
            raise ValueError("Invalid tree ensemble schema.")
        node_budget = 0
        for tree in model["trees"]:
            fields = {"left", "right", "feature", "threshold", "value"}
            if not isinstance(tree, dict) or set(tree) != fields or any(not isinstance(a, list) for a in tree.values()):
                raise ValueError("Invalid tree arrays.")
            size = len(tree["left"])
            node_budget += size
            if not 1 <= size <= MAX_TREE_NODES or node_budget > MAX_TREE_NODES or any(len(a) != size for a in tree.values()):
                raise ValueError("Tree node budget exceeded.")
            parents = [0] * size
            depths = [0] * size
            for i in range(size):
                left, right, feature = tree["left"][i], tree["right"][i], tree["feature"][i]
                if any(isinstance(v, bool) or not isinstance(v, int) for v in (left, right, feature)):
                    raise ValueError("Tree indexes must be integers.")
                _number(tree["threshold"][i])
                if not 0 <= _number(tree["value"][i]) <= 200:
                    raise ValueError("Tree speed is outside the reviewed bound.")
                if left == right == -1:
                    if feature != -2:
                        raise ValueError("Invalid tree leaf feature.")
                elif not (i < left < size and i < right < size and left != right and 0 <= feature < len(FEATURE_NAMES)):
                    raise ValueError("Invalid tree topology or feature.")
                else:
                    parents[left] += 1
                    parents[right] += 1
                    depths[left] = depths[right] = depths[i] + 1
            if parents[0] or any(p != 1 for p in parents[1:]) or max(depths) > 16:
                raise ValueError("Tree must be connected, acyclic and shallow.")
    else:
        raise ValueError("Unsupported model kind.")


def validate_artifact(payload: Any) -> dict:
    required = {"schema_version", "format", "scope", "site_id", "source_pdf_sha256", "source_csv_sha256", "feature_names", "forecast_date", "directions", "start_times", "training_dates", "model_name", "model", "history", "source_license", "license_url", "attribution", "production_eligible"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("Invalid survey model artifact schema.")
    if (payload["schema_version"] != 1 or isinstance(payload["schema_version"], bool)
            or payload["format"] != "crossflow_survey_speed_numeric_v1" or payload["scope"] != SCOPE
            or payload["site_id"] != SITE_ID or payload["source_pdf_sha256"] != SOURCE_PDF_SHA256
            or payload["source_csv_sha256"] != SOURCE_CSV_SHA256 or payload["feature_names"] != list(FEATURE_NAMES)
            or payload["forecast_date"] != FORECAST_DATE or payload["directions"] != list(DIRECTIONS)
            or payload["start_times"] != list(START_TIMES) or payload["training_dates"] != ["2023-02-16", "2023-02-17"]
            or payload["production_eligible"] is not False):
        raise ValueError("Artifact scope, source or training boundary differs from the reviewed experiment.")
    for field in ("model_name", "source_license", "license_url", "attribution"):
        if not isinstance(payload[field], str) or not 1 <= len(payload[field]) <= 2000:
            raise ValueError("Invalid artifact provenance text.")
    if not isinstance(payload["history"], list) or not 1 <= len(payload["history"]) <= 32:
        raise ValueError("Invalid training history size.")
    seen = set()
    for row in payload["history"]:
        if not isinstance(row, dict) or set(row) != {"date", "start_time", "direction", "speed_kph"}:
            raise ValueError("Invalid training history schema.")
        if row["date"] not in payload["training_dates"] or row["start_time"] not in START_TIMES or row["direction"] not in DIRECTIONS:
            raise ValueError("Training history exceeds the allowed survey boundary.")
        key = row["date"], row["start_time"], row["direction"]
        if key in seen or not 0 < _number(row["speed_kph"]) <= 200:
            raise ValueError("Duplicate or invalid training history.")
        seen.add(key)
    _validate_model(payload["model"])
    return payload


def save_survey_speed_model(payload: dict, path: Path) -> dict:
    validate_artifact(payload)
    document = {"sha256": hashlib.sha256(canonical(payload)).hexdigest(), "payload": payload}
    raw = canonical(document) + b"\n"
    if len(raw) > MAX_BYTES:
        raise ValueError("Survey model exceeds byte budget.")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".survey-speed-", delete=False) as handle:
            temporary = handle.name
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return {"sha256": document["sha256"], "bytes": len(raw), "path": str(path)}


def load_survey_speed_model(path: Path) -> dict:
    with Path(path).open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Survey model exceeds byte budget.")
    try:
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON field in survey model.")
                result[key] = value
            return result
        document = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(document, dict) or set(document) != {"sha256", "payload"} or document["sha256"] != hashlib.sha256(canonical(document["payload"])).hexdigest():
            raise ValueError("Survey model checksum mismatch.")
        return validate_artifact(document["payload"])
    except (KeyError, TypeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Malformed survey model artifact.") from error


def numeric_prediction(model: dict, history: list[dict], interval_start: datetime, direction: str) -> float:
    """Shared numeric implementation; caller enforces the outer site/date scope."""
    kind = model["kind"]
    if kind in {"previous_survey_day_slot", "historical_slot_mean"}:
        candidates = [r for r in history if date.fromisoformat(r["date"]) < interval_start.date()
                      and r["start_time"] == interval_start.strftime("%H:%M") and r["direction"] == direction]
        if not candidates:
            raise ValueError("No causal, same-slot observed history is available.")
        value = (max(candidates, key=lambda r: r["date"])["speed_kph"] if kind == "previous_survey_day_slot"
                 else math.fsum(r["speed_kph"] for r in candidates) / len(candidates))
    elif kind == "direction_mean":
        value = model["means"][direction]
    elif kind == "linear":
        value = model["intercept"] + math.fsum(c * x for c, x in zip(model["coefficients"], features(interval_start, direction), strict=True))
    else:
        x = np.asarray(features(interval_start, direction), dtype=np.float32)
        values = []
        for tree in model["trees"]:
            node = 0
            for _ in range(17):
                if tree["left"][node] == -1:
                    values.append(tree["value"][node])
                    break
                node = tree["left"][node] if float(x[tree["feature"][node]]) <= tree["threshold"][node] else tree["right"][node]
            else:
                raise ValueError("Tree exceeded validated depth.")
        value = math.fsum(values) / len(values)
    return min(200.0, max(0.0, float(value)))


def predict_survey_speed(payload: dict, *, site_id: str, source_sha256: str,
                         interval_start: str, direction: str) -> dict:
    validate_artifact(payload)
    try:
        start = datetime.fromisoformat(interval_start)
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid interval start.") from error
    if (site_id != SITE_ID or source_sha256 != SOURCE_PDF_SHA256 or direction not in DIRECTIONS
            or start.utcoffset() != timedelta(hours=7) or start.date().isoformat() != FORECAST_DATE
            or start.strftime("%H:%M") not in START_TIMES or start.second or start.microsecond):
        raise ValueError("Prediction is outside the reviewed site, source, survey date or clock windows.")
    value = numeric_prediction(payload["model"], payload["history"], start, direction)
    return {"predicted_speed_kph": value, "unit": "km/h", "model_name": payload["model_name"],
            "site_id": SITE_ID, "interval_start": start.isoformat(),
            "interval_end": (start + timedelta(minutes=30)).isoformat(), "direction": direction,
            "scope": SCOPE, "production_eligible": False,
            "limitation": "Historical single-site interval-mean diagnostic; no current road, citywide or congestion-severity forecast."}
