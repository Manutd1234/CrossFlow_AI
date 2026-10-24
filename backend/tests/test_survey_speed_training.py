"""Real survey fit isolation and bounded portable inference regressions."""
import copy
from datetime import datetime
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest
from sklearn.ensemble import ExtraTreesRegressor, RandomForestRegressor

from services.survey_speed_model import (
    SITE_ID, SOURCE_PDF_SHA256, canonical, features, load_survey_speed_model,
    predict_survey_speed, save_survey_speed_model, validate_artifact,
)

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("train_batam_speed", ROOT / "scripts/train_batam_speed.py")
training = importlib.util.module_from_spec(spec)
spec.loader.exec_module(training)


@pytest.fixture(scope="module")
def trained():
    rows, manifest = training.load_source(ROOT / "backend/data/research")
    report, payload = training.train(rows, manifest)
    return rows, manifest, report, payload


def test_day3_labels_never_change_model_selection_or_final_fitted_artifact(trained):
    rows, manifest, report, payload = trained
    changed = copy.deepcopy(rows)
    for row in changed:
        if row["start"].day == 18:
            row["speed"] = 150
    after_report, after_payload = training.train(changed, manifest)
    assert canonical(after_payload) == canonical(payload)
    assert after_report["selection"] == report["selection"]
    assert after_report["test_metrics"] != report["test_metrics"]
    assert report["splits"]["fit"]["rows"] == 16
    assert report["splits"]["validation"]["rows"] == 16
    assert report["splits"]["test"]["rows"] == 16
    assert len(payload["history"]) == 32
    assert {row["date"] for row in payload["history"]} == {"2023-02-16", "2023-02-17"}


def test_numeric_artifact_roundtrip_reproduces_every_held_out_prediction(tmp_path, trained):
    _, _, report, payload = trained
    path = tmp_path / "model.json"
    save_survey_speed_model(payload, path)
    loaded = load_survey_speed_model(path)
    assert loaded == payload
    for row in report["predictions"]:
        result = predict_survey_speed(loaded, site_id=SITE_ID, source_sha256=SOURCE_PDF_SHA256,
                                     interval_start=row["interval_start"], direction=row["direction"])
        assert result["predicted_speed_kph"] == pytest.approx(row["selected_model_kph"], abs=1e-12)
        assert result["production_eligible"] is False


@pytest.mark.parametrize("name,klass,depth", [("random_forest_depth_3", RandomForestRegressor, 3),
                                              ("extra_trees_depth_2", ExtraTreesRegressor, 2)])
def test_exported_numeric_tree_predictions_match_fitted_sklearn(trained, name, klass, depth):
    rows, _, _, _ = trained
    fit = [r for r in rows if r["start"].day < 18]
    test = [r for r in rows if r["start"].day == 18]
    x = np.asarray([features(r["start"], r["direction"]) for r in fit])
    estimator = klass(n_estimators=48, min_samples_leaf=2, n_jobs=1, random_state=42, max_depth=depth)
    estimator.fit(x, [r["speed"] for r in fit])
    expected = estimator.predict([features(r["start"], r["direction"]) for r in test])
    actual = training.predict_rows(training.fit_numeric(name, fit), fit, test)
    np.testing.assert_allclose(actual, expected, atol=1e-12, rtol=1e-12)


@pytest.mark.parametrize("change", [
    {"site_id": "another-batam-road"}, {"source_sha256": "0" * 64},
    {"interval_start": "2026-10-24T07:00:00+07:00"},
    {"interval_start": "2023-02-18T10:00:00+07:00"},
    {"interval_start": "2023-02-18T07:00:00+08:00"},
    {"interval_start": "2023-02-18T07:00:01+07:00"}, {"direction": "northbound"},
])
def test_inference_rejects_network_current_date_and_unknown_window_extrapolation(trained, change):
    kwargs = dict(site_id=SITE_ID, source_sha256=SOURCE_PDF_SHA256,
                  interval_start="2023-02-18T07:00:00+07:00", direction="from_east")
    kwargs.update(change)
    with pytest.raises(ValueError, match="outside"):
        predict_survey_speed(trained[3], **kwargs)


def test_target_measurements_and_derived_fields_never_enter_features(trained):
    rows = copy.deepcopy(trained[0])
    before = [features(r["start"], r["direction"]) for r in rows]
    for row in rows:
        row.update(speed=200, travel_time=999, reference_speed=1, volume=999999, density=999)
    after = [features(r["start"], r["direction"]) for r in rows]
    np.testing.assert_array_equal(before, after)
    assert trained[2]["feature_contract"]["names"] == ["hour_sin", "hour_cos", "direction_from_west"]


def test_causal_slot_baselines_cannot_consume_target_day_history(trained):
    fit = [r for r in trained[0] if r["start"].day < 18]
    target = [r for r in trained[0] if r["start"].day == 18]
    before = training.predict_rows({"kind": "historical_slot_mean"}, fit, target)
    after = training.predict_rows({"kind": "historical_slot_mean"}, fit + target, target)
    assert before == after


def test_artifact_rejects_future_labels_unsafe_tree_bounds_and_corruption(tmp_path, trained):
    payload = copy.deepcopy(trained[3])
    payload["history"][0]["date"] = "2023-02-18"
    with pytest.raises(ValueError, match="boundary"):
        validate_artifact(payload)
    payload = copy.deepcopy(trained[3])
    payload["model"] = {"kind": "tree_ensemble", "trees": [{"left": [0], "right": [0], "feature": [0], "threshold": [1.0], "value": [25.0]}]}
    with pytest.raises(ValueError, match="topology"):
        validate_artifact(payload)
    path = tmp_path / "model.json"
    save_survey_speed_model(trained[3], path)
    document = json.loads(path.read_text())
    document["payload"]["model_name"] = "changed"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="checksum"):
        load_survey_speed_model(path)


def test_flagged_source_row_sensitivity_keeps_selected_family_and_no_promotion(trained):
    report = trained[2]
    assert report["flagged_row_sensitivity"]["removed_fit_rows"] == 1
    assert report["flagged_row_sensitivity"]["retuned"] is False
    assert report["flagged_row_sensitivity"]["selected_model_unchanged"] == report["selected_model"]
    assert report["production_eligible"] is False
    assert report["serving_model_replaced"] is False
    assert report["application_history_modified"] is False
    assert report["synthetic_training_rows"] == 0
