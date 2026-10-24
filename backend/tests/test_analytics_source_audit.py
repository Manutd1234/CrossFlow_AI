"""Serving metrics and historical evaluation reads preserve their real scope."""
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")
# The focused test runner uses in-memory stores and observed-only mode; these
# tests neither fit a model nor start the application's background lifespan.

import main
from services import forecast_evaluation, live_traffic


def model_status(*, eligible, selected="random_forest"):
    return {"available": True, "engine": {"algorithm": "RandomForestRegressor"}, "report": {
        "trained_at": "2026-10-24T00:00:00+00:00", "selected_rows": 123,
        "training_pairs": 99, "quality_gate": {"eligible": eligible, "reasons": []},
        "horizons": [{"horizon_minutes": 30, "selected_strategy": selected,
            "feature_importances": {"current_index": 1.0},
            "forest_test": {"samples": 40, "r2": .2, "mae_points": 8.0, "rmse_points": 10.0},
            "persistence_test": {"samples": 40, "r2": .7, "mae_points": 2.0, "rmse_points": 3.0}}],
    }}


def test_public_model_metrics_match_the_gate_selected_serving_strategy():
    for eligible, selected, serving, expected in (
        (False, "random_forest", "persistence", 2.0),
        (False, "persistence", "persistence", 2.0),
        (True, "random_forest", "random_forest", 8.0),
        (True, "persistence", "persistence", 2.0),
    ):
        with patch.dict(os.environ, {"CROSSFLOW_REAL_TRAFFIC_TRAINING": "1"}), \
                patch.object(main.real_traffic_model, "status", return_value=model_status(eligible=eligible, selected=selected)):
            metrics = main.api_model_status()["metrics"]
        assert metrics["selected_strategy"] == serving
        assert metrics["mae"] == expected
        assert metrics["metrics_scope"] == "serving_strategy_30min_chronological_holdout"
        assert metrics["candidate_metrics"]["mae_points"] == 8.0
        assert metrics["candidate_metrics_scope"] == "forest_candidate_30min_chronological_holdout"
        assert metrics["quality_gate"]["eligible"] is eligible


def test_unavailable_observed_model_does_not_invent_accuracy():
    with patch.dict(os.environ, {"CROSSFLOW_REAL_TRAFFIC_TRAINING": "1"}), \
            patch.object(main.real_traffic_model, "status", return_value={"available": False, "report": None, "engine": None}):
        metrics = main.api_model_status()["metrics"]
    assert metrics["is_trained"] is False
    assert all(metrics[key] is None for key in ("mae", "rmse", "r2_score", "selected_strategy", "candidate_metrics"))


def test_prospective_summary_reads_existing_ledger_without_writer_or_schema_changes():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "evaluation.sqlite"
        db = forecast_evaluation._connection(path)
        with db:
            db.execute("""INSERT INTO issued_forecasts
                (id,issued_at,origin_at,target_at,model_version,corridor,latitude,longitude,horizon,
                 candidate,selected,persistence,actual,actual_at,matched_at,closed)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                "fixture-ledger-row", "2026-10-24T00:00:00+00:00", "2026-10-24T00:00:00+00:00",
                "2026-10-24T00:15:00+00:00", "fixture-model", "corridor-1", 1.12, 104.03, 15,
                60.0, 50.0, 50.0, 52.0, "2026-10-24T00:15:00+00:00", "2026-10-24T00:16:00+00:00", 1,
            ))
        db.close()
        before = (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        connect = sqlite3.connect
        with patch.object(forecast_evaluation, "_connection", side_effect=AssertionError("summary must not open writer")), \
                patch.object(forecast_evaluation.sqlite3, "connect", wraps=connect) as reader:
            value = forecast_evaluation.summary(path)
        assert reader.call_args.args[0].endswith("?mode=ro")
        assert reader.call_args.kwargs["uri"] is True
        assert value["issued"] == value["matched"] == 1
        assert value["horizons"][0]["candidate_mae_points"] == 8.0
        assert value["horizons"][0]["selected_mae_points"] == 2.0
        assert before == (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())


def test_missing_or_uninitialized_ledger_is_not_created_by_an_analytics_read():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "evaluation.sqlite"
        assert forecast_evaluation.summary(path)["status"] == "awaiting_predictions"
        assert not path.exists()
        path.touch()
        assert forecast_evaluation.summary(path)["status"] == "evaluation_unavailable"
        assert path.stat().st_size == 0


def test_complete_live_provider_snapshot_does_not_depend_on_forecast_or_synthetic_inputs():
    provider = {"current_speed_kmh": 25.0, "free_flow_speed_kmh": 50.0, "confidence": .9,
                "snapshot_generated_at": "2026-10-24T00:00:00+00:00"}
    segments = {corridor_id: dict(provider) for corridor_id in live_traffic._CORRIDOR_POINTS}
    with patch.object(live_traffic, "_tomtom_segments", return_value=segments), \
            patch.object(live_traffic, "_tomtom_api_key", return_value=""), \
            patch.object(live_traffic.cctv_store, "corridor_scores", return_value={}), \
            patch("services.simulator.get_live_corridor_telemetry", side_effect=RuntimeError("observed model warming")) as forecast:
        value = live_traffic.get_live_traffic()
    forecast.assert_not_called()
    assert len(value["segments"]) == len(segments)
    assert all(row["source"] == "tomtom_live" for row in value["segments"])
    assert all(row["current_speed_kmh"] == 25.0 for row in value["segments"])
    assert len(value["zones"]) == 30
    assert all(zone["source"] == "modelled_spatial_hotspot" for zone in value["zones"])
    assert value["coverage"]["emissions_pressure_model"]["observed"] is False
    assert value["coverage"]["emissions_pressure_model"]["traffic_input"] == "resolved_corridor_signals_plus_recurring_zone_baseline"
