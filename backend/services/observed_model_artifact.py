"""Bounded, data-only Random Forest artifacts; never load pickle/joblib code.

The checksum detects accidental corruption, not authorship. Paths must come
from trusted server configuration, never HTTP input. Only locally evaluated,
observed-only eligible candidates can be written or restored.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

import numpy as np
from sklearn.ensemble import RandomForestRegressor

from models.congestion_model import CongestionForecaster, OBSERVED_HISTORY_SOURCES

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_TREE_NODES = 500_000
FEATURE_COUNT = 26


class NumericRegressionTree:
    def __init__(self, data: dict, *, feature_count=FEATURE_COUNT):
        if isinstance(feature_count, bool) or not isinstance(feature_count, int) or not 1 <= feature_count <= 128:
            raise ValueError("Invalid numeric feature count.")
        self.feature_count = feature_count
        if not isinstance(data, dict) or set(data) != {"left", "right", "feature", "threshold", "value"}:
            raise ValueError("Invalid numeric tree schema.")
        if any(not isinstance(value, list) for value in data.values()):
            raise ValueError("Numeric tree arrays must be lists.")
        size = len(data["left"])
        if not 1 <= size <= MAX_TREE_NODES or any(len(value) != size for value in data.values()):
            raise ValueError("Invalid numeric tree sizes.")
        for field in ("left", "right", "feature"):
            if any(isinstance(value, bool) or not isinstance(value, int) for value in data[field]):
                raise ValueError("Numeric tree indexes must be integers.")
        for field in ("threshold", "value"):
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in data[field]):
                raise ValueError("Numeric tree values must be finite.")
        parents = np.zeros(size, dtype=int)
        depths = np.zeros(size, dtype=int)
        for index, (left, right, feature, value) in enumerate(zip(data["left"], data["right"], data["feature"], data["value"])):
            if not 0 <= value <= 100:
                raise ValueError("Numeric tree predictions must be bounded.")
            if left == right == -1:
                if feature != -2:
                    raise ValueError("Invalid numeric tree leaf.")
                continue
            if not index < left < size or not index < right < size or left == right or not 0 <= feature < feature_count:
                raise ValueError("Invalid numeric tree children or feature.")
            parents[left] += 1
            parents[right] += 1
            depths[left] = depths[index] + 1
            depths[right] = depths[index] + 1
        if parents[0] != 0 or np.any(parents[1:] != 1) or np.max(depths) > 64:
            raise ValueError("Numeric tree must be connected, acyclic and bounded in depth.")
        self.data = data
        self.left = np.asarray(data["left"], dtype=np.int64)
        self.right = np.asarray(data["right"], dtype=np.int64)
        self.feature = np.asarray(data["feature"], dtype=np.int64)
        self.threshold = np.asarray(data["threshold"], dtype=float)
        self.value = np.asarray(data["value"], dtype=float)

    def predict(self, features):
        features = np.asarray(features, dtype=np.float32)
        if features.ndim != 2 or features.shape[1] != self.feature_count or not np.all(np.isfinite(features)):
            raise ValueError("Invalid artifact prediction features.")
        nodes = np.zeros(len(features), dtype=np.int64)
        for _ in range(65):
            active = np.flatnonzero(self.left[nodes] != -1)
            if not len(active):
                return self.value[nodes]
            current = nodes[active]
            nodes[active] = np.where(features[active, self.feature[current]] <= self.threshold[current],
                                     self.left[current], self.right[current])
        raise ValueError("Numeric tree exceeded its validated depth.")


class NumericRandomForest:
    def __init__(self, trees, importances, *, feature_count=FEATURE_COUNT):
        if not isinstance(trees, list) or not 1 <= len(trees) <= 100:
            raise ValueError("Invalid numeric forest tree count.")
        if sum(len(tree.get("left", [])) for tree in trees if isinstance(tree, dict)) > MAX_TREE_NODES:
            raise ValueError("Numeric forest exceeds node budget.")
        self.estimators_ = [NumericRegressionTree(tree, feature_count=feature_count) for tree in trees]
        self.n_features_in_ = feature_count
        if not isinstance(importances, list) or len(importances) != feature_count or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1 for value in importances):
            raise ValueError("Invalid numeric forest importances.")
        self.feature_importances_ = np.asarray(importances, dtype=float)
        total = math.fsum(importances)
        if total != 0 and not math.isclose(total, 1, rel_tol=1e-6, abs_tol=1e-8):
            raise ValueError("Numeric forest importances must be normalized or all zero.")

    def get_params(self, deep=False):
        return RandomForestRegressor(n_estimators=100, max_depth=10,
                                     min_samples_leaf=5, n_jobs=1,
                                     random_state=42).get_params(deep=deep)

    def predict(self, features):
        return np.mean([tree.predict(features) for tree in self.estimators_], axis=0)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _eligible(metrics, report):
    if not isinstance(metrics, dict) or not isinstance(report, dict):
        return False
    activation = report.get("activation")
    sources = metrics.get("training_source_counts")
    if not isinstance(activation, dict) or not isinstance(sources, dict):
        return False
    return (activation.get("eligible") is True
            and metrics.get("training_data_source") == "validated_spatial_observed"
            and isinstance(metrics.get("observed_fit_rows"), int)
            and not isinstance(metrics.get("observed_fit_rows"), bool)
            and metrics["observed_fit_rows"] > 0 and bool(sources)
            and set(sources) <= OBSERVED_HISTORY_SOURCES
            and all(isinstance(count, int) and not isinstance(count, bool) and count > 0
                    for count in sources.values()))


def save_observed_model(candidate: CongestionForecaster, path, *, report: dict):
    """Atomically write a bounded numeric artifact before any live publication."""
    if not _eligible(candidate.metrics, report):
        raise ValueError("Only an eligible observed-only model can be persisted.")
    trees = []
    for estimator in candidate.model.estimators_:
        if isinstance(estimator, NumericRegressionTree):
            trees.append(copy.deepcopy(estimator.data))
        else:
            tree = estimator.tree_
            trees.append({"left": tree.children_left.tolist(), "right": tree.children_right.tolist(),
                          "feature": tree.feature.tolist(), "threshold": tree.threshold.tolist(),
                          "value": tree.value[:, 0, 0].tolist()})
    # Validate the same portable representation that a future process will use.
    importances = candidate.model.feature_importances_.tolist()
    NumericRandomForest(trees, importances)
    payload = {"schema_version": 1, "format": "crossflow_observed_rf_numeric_v1",
               "feature_count": FEATURE_COUNT, "trees": trees,
               "feature_importances": importances, "metrics": candidate.metrics,
               "interval_padding_points": candidate._interval_padding_points,
               "eligibility_report": report}
    raw = _canonical(payload)
    document = _canonical({"sha256": hashlib.sha256(raw).hexdigest(), "payload": payload})
    if len(document) > MAX_ARTIFACT_BYTES:
        raise ValueError("Numeric model artifact exceeds byte budget.")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".observed-model-", delete=False) as handle:
            temporary = handle.name
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return {"sha256": hashlib.sha256(raw).hexdigest(), "format": payload["format"], "bytes": len(document)}


def load_observed_model(path) -> CongestionForecaster:
    """Read only bounded JSON/numeric arrays; never deserialize executable code."""
    with Path(path).open("rb") as handle:
        raw = handle.read(MAX_ARTIFACT_BYTES + 1)
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError("Numeric model artifact exceeds byte budget.")
    try:
        document = json.loads(raw)
        payload = document["payload"]
        if document["sha256"] != hashlib.sha256(_canonical(payload)).hexdigest():
            raise ValueError("Numeric model artifact checksum mismatch.")
        if payload["schema_version"] != 1 or payload["format"] != "crossflow_observed_rf_numeric_v1" or payload["feature_count"] != FEATURE_COUNT:
            raise ValueError("Unsupported numeric model artifact.")
        if not _eligible(payload["metrics"], payload["eligibility_report"]):
            raise ValueError("Numeric model artifact lacks eligible observed evidence.")
        padding = payload["interval_padding_points"]
        if isinstance(padding, bool) or not isinstance(padding, (int, float)) or not math.isfinite(padding) or not 0 <= padding <= 100:
            raise ValueError("Invalid numeric model interval padding.")
        model = NumericRandomForest(payload["trees"], payload["feature_importances"])
    except (KeyError, TypeError, AttributeError, OverflowError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Malformed numeric model artifact.") from error
    result = CongestionForecaster(train_baseline=False)
    result.model = model
    result.metrics = copy.deepcopy(payload["metrics"])
    result._interval_padding_points = float(padding)
    result.is_trained = True
    result.metrics["artifact"] = {
        "format": payload["format"], "sha256": document["sha256"],
        "restored": True, "persisted": True,
        "durability": "persistent_local_file", "scope": "single_host",
    }
    return result


def restore_observed_model(forecaster: CongestionForecaster, path):
    """Explicit trusted startup operation; never invoked by a read endpoint."""
    candidate = load_observed_model(path)
    if not forecaster._retrain_lock.acquire(blocking=False):
        raise ValueError("Cannot restore while a candidate mutation is in progress.")
    try:
        with forecaster._model_lock:
            report = candidate.metrics.get("observed_training")
            if isinstance(report, dict):
                report["status"] = "activated"
                report["model_replaced"] = True
            candidate.metrics["artifact"]["restored_at"] = datetime.now(timezone.utc).isoformat()
            forecaster.model = candidate.model
            forecaster.metrics = candidate.metrics
            forecaster._interval_padding_points = candidate._interval_padding_points
            forecaster.is_trained = True
    finally:
        forecaster._retrain_lock.release()
    return candidate.metrics["artifact"]
