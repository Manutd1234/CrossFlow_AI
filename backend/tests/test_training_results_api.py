"""Offline results are operator-readable metadata, never an activation endpoint."""
import asyncio
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from fastapi import HTTPException, Response

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")
import main as api


class TrainingResultsApiTests(unittest.TestCase):
    def test_http_route_exposes_only_separate_offline_results(self):
        report = {"status": "completed", "model_activated": False, "experiments": []}
        with patch.object(api.identity, "require_route_operator") as authorize, patch(
            "services.training_experiments.training_experiment_status", return_value=report
        ):
            async def request():
                sent, delivered = [], False
                async def receive():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": b"", "more_body": False}
                    await asyncio.Event().wait()
                async def send(message):
                    sent.append(message)
                await api.app({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                    "http_version": "1.1", "method": "GET", "scheme": "http", "root_path": "",
                    "path": "/api/analytics/training-results", "raw_path": b"/api/analytics/training-results",
                    "query_string": b"", "server": ("test", 80), "client": ("test", 1),
                    "headers": [(b"authorization", b"Bearer operator")]}, receive, send)
                return sent
            messages = asyncio.run(asyncio.wait_for(request(), timeout=5))
        start = next(item for item in messages if item["type"] == "http.response.start")
        body = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
        self.assertEqual(start["status"], 200)
        authorize.assert_called_once_with("Bearer operator")
        self.assertFalse(json.loads(body)["training_results"]["model_activated"])
        self.assertIn(b"no-store", dict(start["headers"])[b"cache-control"])

    def test_unauthorized_request_cannot_read_local_report(self):
        denied = HTTPException(status_code=403, detail="Denied")
        with patch.object(api.identity, "require_route_operator", side_effect=denied), patch(
            "services.training_experiments.training_experiment_status"
        ) as read:
            with self.assertRaises(HTTPException) as caught:
                api.api_training_experiment_results(Response(), "Bearer invalid")
        read.assert_not_called()
        self.assertIn("no-store", caught.exception.headers["Cache-Control"])

    def test_operator_reads_summary_without_training_or_model_mutation(self):
        report = {"status": "completed", "model_activated": False, "experiments": []}
        response = Response()
        with patch.object(api.identity, "require_route_operator"), patch(
            "services.training_experiments.training_experiment_status", return_value=report
        ) as read, patch.object(api.forecaster, "retrain_from_observations") as train:
            result = api.api_training_experiment_results(response, "Bearer operator")
        read.assert_called_once_with()
        train.assert_not_called()
        self.assertEqual(result["training_results"], report)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_corrupt_summary_returns_private_503(self):
        with patch.object(api.identity, "require_route_operator"), patch(
            "services.training_experiments.training_experiment_status", side_effect=ValueError("checksum")
        ):
            with self.assertRaises(HTTPException) as caught:
                api.api_training_experiment_results(Response(), "Bearer operator")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("no-store", caught.exception.headers["Cache-Control"])


if __name__ == "__main__":
    unittest.main()
