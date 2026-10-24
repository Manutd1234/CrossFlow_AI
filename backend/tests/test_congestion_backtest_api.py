"""Read-only, server-owned backtest API and persistence adapter boundaries."""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException, Response
from pydantic import ValidationError


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

import main as api_main  # noqa: E402
from services import historical_store, routing_intelligence_store  # noqa: E402
from services.traffic_observations import SpatialTrafficObservation  # noqa: E402


NOW = datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc)


def observation(index: int, source: str = "probe_gps") -> SpatialTrafficObservation:
    return SpatialTrafficObservation.create(
        corridor_id="corridor-1", observed_at=NOW - timedelta(minutes=15 * index),
        latitude=1.10, longitude=104.0, actual_speed_kph=25,
        free_flow_speed_kph=50, source=source,
        upstream_event_id=f"api-backtest-{source}-{index}",
    )


class CongestionBacktestApiTests(unittest.TestCase):
    def setUp(self) -> None:
        environment = patch.dict(os.environ, {"CROSSFLOW_ADMIN_TOKEN": "token"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def call(self):
        response = Response()
        result = api_main.api_congestion_backtest(response, None, "token")
        self.assertIn("no-store", response.headers["Cache-Control"])
        return result

    def test_unauthorized_request_never_reads_or_evaluates_private_history(self) -> None:
        with patch.object(
            historical_store, "get_spatial_training_dataset",
        ) as read, patch.object(api_main, "backtest_congestion") as evaluate:
            with self.assertRaises(HTTPException) as caught:
                api_main.api_congestion_backtest(Response(), None, "wrong-token")
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIn("no-store", caught.exception.headers["Cache-Control"])
        read.assert_not_called()
        evaluate.assert_not_called()

    def test_actual_local_snapshot_excludes_modelled_rows_and_preserves_live_model(self) -> None:
        store = historical_store.HistoryStore(":memory:", now_provider=lambda: NOW)
        self.addCleanup(store.close)
        store.ingest_spatial_batch((observation(1), observation(2), observation(3, "synthetic")), now=NOW)
        before = copy.deepcopy(api_main.forecaster.metrics)
        with patch.object(api_main.clock, "now", return_value=NOW), patch.object(
            historical_store, "get_spatial_training_dataset", wraps=store.get_spatial_training_dataset,
        ) as read, patch.object(
            api_main.forecaster, "retrain_from_observations",
            side_effect=AssertionError("must not replace live model"),
        ):
            result = self.call()
        read.assert_called_once_with(limit=2000, now=NOW)
        self.assertEqual(result["backtest"]["status"], "insufficient_observations")
        self.assertEqual(result["backtest"]["eligible_rows"], 2)
        self.assertEqual(result["backtest"]["observed_rows"], 2)
        self.assertFalse(result["backtest"]["model_replaced"])
        self.assertEqual(result["evaluation_snapshot"]["loaded_rows"], 3)
        self.assertEqual(result["evaluation_snapshot"]["excluded_modelled_rows"], 1)
        self.assertEqual(result["evaluation_snapshot"]["horizons_minutes"], [15, 30, 60])
        self.assertEqual(result["evaluation_snapshot"]["durability"], "local_sqlite")
        self.assertEqual(api_main.forecaster.metrics, before)

    def test_explicit_shared_adapter_receives_the_bound_without_local_fallback(self) -> None:
        reader = Mock(return_value=(observation(1),))
        shared = SimpleNamespace(get_spatial_training_dataset=reader)
        with patch.object(
            routing_intelligence_store, "shared_store_enabled", return_value=True,
        ), patch.object(
            api_main, "_shared_routing_store_required", return_value=shared,
        ), patch.object(historical_store, "get_spatial_training_dataset") as local:
            result = self.call()
        reader.assert_called_once_with(limit=2000)
        local.assert_not_called()
        self.assertEqual(result["evaluation_snapshot"]["durability"], "shared_supabase")

    def test_empty_history_is_explicitly_insufficient_without_accuracy_claim(self) -> None:
        with patch.object(historical_store, "get_spatial_training_dataset", return_value=[]):
            result = self.call()
        self.assertEqual(result["backtest"]["status"], "insufficient_observations")
        self.assertEqual(result["backtest"]["observed_rows"], 0)
        self.assertEqual(result["backtest"]["horizons"], [])
        self.assertTrue(result["backtest"]["requires_measured_data_for_accuracy_claim"])

    def test_client_cannot_supply_records_or_change_evaluation_policy(self) -> None:
        for override in (
            {"records": []}, {"include_modelled": True},
            {"horizons_minutes": [1]}, {"limit": 20000},
        ):
            with self.subTest(override=override), self.assertRaises(ValidationError):
                api_main.CongestionBacktestRequest(**override)

    def test_bad_store_iterator_is_consumed_at_most_bound_plus_one(self) -> None:
        consumed = []

        def unbounded():
            while True:
                consumed.append(1)
                yield observation(1)

        with patch.object(
            historical_store, "get_spatial_training_dataset", return_value=unbounded(),
        ), patch.object(api_main, "backtest_congestion") as evaluate:
            with self.assertRaises(HTTPException) as caught:
                self.call()
        self.assertEqual(len(consumed), 2001)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("no-store", caught.exception.headers["Cache-Control"])
        evaluate.assert_not_called()

    def test_local_and_shared_storage_errors_are_sanitized_and_not_fallbacks(self) -> None:
        for use_shared in (False, True):
            with self.subTest(shared=use_shared), patch.object(
                routing_intelligence_store, "shared_store_enabled", return_value=use_shared,
            ), patch.object(
                historical_store, "get_spatial_training_dataset",
                side_effect=sqlite3.OperationalError("private-db-path"),
            ) as local, patch.object(
                api_main, "_shared_routing_store_required",
                side_effect=routing_intelligence_store.RoutingIntelligenceStoreUnavailable("private-db-path"),
            ), patch.object(api_main, "backtest_congestion") as evaluate:
                with self.assertRaises(HTTPException) as caught:
                    self.call()
                self.assertEqual(caught.exception.status_code, 503)
                self.assertNotIn("private-db-path", caught.exception.detail)
                self.assertIn("no-store", caught.exception.headers["Cache-Control"])
                if use_shared:
                    local.assert_not_called()
                evaluate.assert_not_called()

    def test_corrupt_provenance_is_rejected_without_echoing_private_rows(self) -> None:
        forged = replace(observation(1), observed=False)
        with patch.object(historical_store, "get_spatial_training_dataset", return_value=[forged]):
            with self.assertRaises(HTTPException) as caught:
                self.call()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("no-store", caught.exception.headers["Cache-Control"])
        self.assertNotIn(forged.observation_key, caught.exception.detail)

    def test_evaluation_receives_fixed_horizons_and_observed_only_policy(self) -> None:
        rows = (observation(1),)
        with patch.object(
            historical_store, "get_spatial_training_dataset", return_value=rows,
        ), patch.object(api_main, "backtest_congestion", return_value={}) as evaluate:
            self.call()
        evaluate.assert_called_once_with(rows, horizons_minutes=(15, 30, 60), include_modelled=False)


if __name__ == "__main__":
    unittest.main()
