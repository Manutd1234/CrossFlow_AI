"""Real serving-model simulation, fixed resource bounds and operator access."""

from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from fastapi import HTTPException, Response
from pydantic import ValidationError

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

import main as api_main  # noqa: E402
from services import analytics_simulation as simulation, clock  # noqa: E402


NOW = datetime(2026, 10, 23, 16, 30, tzinfo=timezone.utc)  # Friday23:30 WIB


class AnalyticsSimulationTests(unittest.TestCase):
    def test_real_serving_ensemble_supplies_quantiles_with_fixed_work_bound(self):
        before = deepcopy(simulation.forecaster.metrics)
        with patch.object(simulation.forecaster, "predict_spatial_batch", wraps=simulation.forecaster.predict_spatial_batch) as infer:
            result = simulation.simulate_analytics(NOW, weather=1)
        contexts = infer.call_args.args[0]
        self.assertEqual(len(contexts), 135)
        self.assertEqual(result["prediction_count"], 135)
        self.assertEqual(result["engine"]["algorithm"], "RandomForestRegressor")
        self.assertEqual(result["engine"]["estimator_count"], len(simulation.forecaster.model.estimators_))
        self.assertEqual(result["engine"]["feature_count"], simulation.forecaster.model.n_features_in_)
        self.assertTrue(result["engine"]["is_trained"])
        self.assertEqual(len(result["hourly_forecast"]), 24)
        self.assertEqual(len(result["corridors"]), 5)
        self.assertEqual(before, simulation.forecaster.metrics)
        for corridor in result["corridors"]:
            for row in corridor["hourly_forecast"]:
                self.assertLessEqual(0, row["p10_score"])
                self.assertLessEqual(row["p10_score"], row["mean_score"])
                self.assertLessEqual(row["mean_score"], row["p90_score"])
                self.assertLessEqual(row["p90_score"], 100)
        self.assertEqual(result["model"]["training_data_source"], before["training_data_source"])
        self.assertIn("not_a_joint", result["aggregate_quantile_method"])

    def test_clock_rollover_uses_actual_calendar_and_weather_features(self):
        with patch.object(simulation.forecaster, "predict_spatial_batch", wraps=simulation.forecaster.predict_spatial_batch) as infer:
            result = simulation.simulate_analytics(NOW, weather=2, corridor_ids=["corridor-1"])
        contexts = infer.call_args.args[0]
        self.assertEqual(len(contexts), 27)
        self.assertEqual(contexts[0].day_of_week, 4)
        self.assertEqual(contexts[1].day_of_week, 5)
        self.assertEqual(contexts[0].hour_float, 23.5)
        self.assertEqual(contexts[1].hour_float, 0.5)
        self.assertTrue(all(context.weather == 2 for context in contexts[:24]))
        self.assertEqual([context.weather for context in contexts[-3:]], [0, 1, 2])
        direct = simulation.forecaster.predict_spatial(contexts[0])
        self.assertEqual(result["hourly_forecast"][0]["mean_score"], direct.mean_score)
        self.assertEqual(result["hourly_forecast"][0]["p10_score"], direct.p10_score)
        self.assertEqual(datetime.fromisoformat(result["hourly_forecast"][1]["timestamp"]) - datetime.fromisoformat(result["hourly_forecast"][0]["timestamp"]), timedelta(hours=1))

    def test_published_ferry_minutes_feed_the_serving_adjustment(self):
        with patch.object(simulation.ferry_schedule, "ferry_surge_for_port", return_value=(1, {"minutes_until_departure": 12})), \
                patch.object(simulation.forecaster, "predict_spatial_batch", wraps=simulation.forecaster.predict_spatial_batch) as infer:
            simulation.simulate_analytics(NOW, corridor_ids=["corridor-1"])
        self.assertTrue(all(context.minutes_until_ferry_departure == 12 for context in infer.call_args.args[0]))

    def test_invalid_selectors_and_client_work_overrides_are_rejected(self):
        for payload in ({"weather": 3}, {"weather": True}, {"corridor_ids": []}, {"corridor_ids": ["corridor-1"] * 6}, {"corridor_ids": ["corridor-1", "corridor-1"]}, {"corridor_ids": ["unknown"]}, {"horizon_hours": 1_000_000}, {"records": []}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                api_main.AnalyticsSimulationRequest(**payload)
        with patch.object(simulation.forecaster, "predict_spatial_batch") as infer:
            with self.assertRaises(ValueError):
                simulation.simulate_analytics(NOW, corridor_ids=[])
        infer.assert_not_called()

    def test_operator_guard_runs_before_model_work(self):
        for status in (401, 403, 503):
            with self.subTest(status=status), patch.object(api_main.identity, "require_route_operator", side_effect=api_main.identity.auth_error(status, "Denied")), \
                    patch.object(api_main, "simulate_analytics") as infer:
                with self.assertRaises(HTTPException) as caught:
                    api_main.api_analytics_simulation(Response(), None, "Bearer fixture")
                self.assertEqual(caught.exception.status_code, status)
                self.assertIn("no-store", caught.exception.headers["Cache-Control"])
                infer.assert_not_called()

    def test_api_labels_read_only_simulation_and_uses_server_time(self):
        response = Response()
        with patch.object(api_main.identity, "require_route_operator") as guard, \
                patch.object(api_main, "simulate_analytics", return_value={"prediction_count": 27}) as infer, \
                clock.frozen(NOW):
            payload = api_main.api_analytics_simulation(response, api_main.AnalyticsSimulationRequest(weather=2, corridor_ids=["corridor-1"]), "Bearer fixture")
        guard.assert_called_once_with("Bearer fixture")
        infer.assert_called_once_with(NOW, weather=2, corridor_ids=["corridor-1"])
        self.assertEqual(payload["data_source"], "simulation")
        self.assertFalse(payload["observed"])
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_model_failure_is_a_sanitized_uncacheable_error(self):
        with patch.object(api_main.identity, "require_route_operator"), \
                patch.object(api_main, "simulate_analytics", side_effect=RuntimeError("private service key")):
            with self.assertRaises(HTTPException) as caught:
                api_main.api_analytics_simulation(Response())
        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn("private", caught.exception.detail)
        self.assertIn("no-store", caught.exception.headers["Cache-Control"])


if __name__ == "__main__":
    unittest.main()
