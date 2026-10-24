"""Cloud-store contracts with no network, production registry or traffic claims."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services import cctv_shared_store as shared, cctv_store as store
from services.supabase_server import SupabaseServerConfig, SupabaseServerError
from backend.tests.test_cctv_store import NOW, configured, evidence, row
import json

CONFIG = SupabaseServerConfig("https://example.supabase.co", "sb_secret_fixture")


class SharedCCTVTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"CROSSFLOW_CCTV_STORE": "supabase"}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_local_default_and_serverless_shared_requirement(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(shared.enabled())
        for environment in ({"VERCEL": "1"}, {"VERCEL_ENV": "production"}):
            with patch.dict(os.environ, environment, clear=True):
                self.assertTrue(shared.enabled())
                with patch.dict(os.environ, {"CROSSFLOW_CCTV_STORE": "local"}):
                    with self.assertRaises(shared.SharedCCTVUnavailable):
                        shared.enabled()
        with patch.dict(os.environ, {"CROSSFLOW_CCTV_STORE": "unexpected"}):
            with self.assertRaises(shared.SharedCCTVUnavailable):
                shared.enabled()

    def test_missing_configuration_and_transport_failure_fail_closed(self):
        with patch.object(shared.supabase_server, "load_server_config", return_value=None):
            with self.assertRaises(shared.SharedCCTVUnavailable):
                shared.latest(["camera-1"])
        with patch.object(shared.supabase_server, "load_server_config", return_value=CONFIG), \
             patch.object(shared.supabase_server, "request_json", side_effect=SupabaseServerError("private-detail")):
            with self.assertRaises(shared.SharedCCTVUnavailable) as caught:
                shared.latest(["camera-1"])
        self.assertNotIn("private-detail", str(caught.exception))

    def test_latest_is_bounded_filtered_and_does_not_request_media(self):
        record = evidence()
        with patch.object(shared, "_request", return_value=[{"camera_id": "camera-1", "captured_at": record["captured_at"], "evidence_json": record}]) as request:
            result = shared.latest(["camera-1"])
        self.assertEqual(result["camera-1"]["evidence"], record)
        call = request.call_args.kwargs
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["query"]["camera_id"], "in.(camera-1)")
        self.assertEqual(call["query"]["select"], "camera_id,captured_at,evidence_json")
        self.assertEqual(shared.latest([]), {})
        for invalid in (["x),camera_id.not.is.null"], ["camera-1"] * 129):
            with self.assertRaises(shared.SharedCCTVUnavailable):
                shared.latest(invalid)

    def test_latest_rejects_duplicate_unregistered_and_malformed_rows(self):
        valid = {"camera_id": "camera-1", "captured_at": "2026-10-24T00:00:00Z", "evidence_json": {}}
        for value in ({}, [valid, valid], [{**valid, "camera_id": "unregistered"}],
                      [{**valid, "evidence_json": "bad"}], [{**valid, "stream_url": "private"}]):
            with self.subTest(value=value), patch.object(shared, "_request", return_value=value):
                with self.assertRaises(shared.SharedCCTVUnavailable):
                    shared.latest(["camera-1"])

    def test_atomic_ingest_receipts_preserve_watermarks_and_privacy(self):
        clean = evidence()
        receipt = {"status": "accepted", "camera_id": clean["camera_id"],
                   "observed_at": clean["captured_at"], "idempotent": False}
        with patch.object(shared, "_request", return_value=receipt) as request:
            result = shared.ingest(clean, "a" * 64, stored_at=clean["captured_at"])
        self.assertTrue(result["accepted"])
        self.assertFalse(result["idempotent"])
        self.assertEqual(request.call_args.kwargs["rest_path"], shared.INGEST_PATH)
        self.assertNotIn("detections", request.call_args.kwargs["payload"]["p_evidence"])
        for value, error in (({"status": "conflict"}, shared.SharedCCTVConflict),
                             ({"status": "capacity"}, shared.SharedCCTVUnavailable),
                             ({**receipt, "idempotent": "false"}, shared.SharedCCTVUnavailable),
                             ({**receipt, "observed_at": "wrong"}, shared.SharedCCTVUnavailable)):
            with self.subTest(value=value), patch.object(shared, "_request", return_value=value):
                with self.assertRaises(error):
                    shared.ingest(clean, "a" * 64, stored_at=clean["captured_at"])

    def test_store_uses_shared_rows_but_recomputes_current_registry_and_expiry(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = Path(folder) / "registry.json"
            database = Path(folder) / "must-not-be-created.sqlite"
            registry.write_text(json.dumps({"schema_version": 1, "cameras": [row(configured())]}))
            clean = evidence()
            remote = {"camera-1": {"captured_at": clean["captured_at"], "evidence": clean}}
            with patch.dict(os.environ, {"CROSSFLOW_CCTV_REGISTRY": str(registry), "CROSSFLOW_CCTV_DB": str(database)}), \
                 patch.object(shared, "latest", return_value=remote), \
                 patch.object(shared, "ingest", return_value={"accepted": True}) as ingest:
                self.assertTrue(store.ingest_evidence(clean, now=NOW)["accepted"])
                self.assertTrue(ingest.called)
                self.assertEqual(store.camera_status(now=NOW)["summary"]["live"], 1)
                from datetime import timedelta
                self.assertEqual(store.camera_status(now=NOW + timedelta(seconds=60))["summary"]["stale"], 1)
                changed = row(configured(enabled=False))
                registry.write_text(json.dumps({"schema_version": 1, "cameras": [changed]}))
                self.assertEqual(store.camera_status(now=NOW)["summary"]["live"], 0)
            self.assertFalse(database.exists())

    def test_shared_failure_never_falls_back_to_local_camera_evidence(self):
        with patch.object(shared, "latest", side_effect=shared.SharedCCTVUnavailable("safe")), \
             patch.object(store, "_db_path") as local:
            with self.assertRaises(store.CCTVStoreUnavailable):
                store._read_latest([{"camera_id": "camera-1"}])
        local.assert_not_called()

    def test_schema_restricts_browser_roles_and_atomic_immutable_watermark(self):
        sql = (store.DATA_DIR / "cctv_observations.sql").read_text().lower()
        for required in ("enable row level security", "from public, anon, authenticated", "to service_role",
                         "pg_advisory_xact_lock", "for update", "p_captured_at <= previous.captured_at",
                         "p_evidence_sha256 = previous.evidence_sha256", "security invoker"):
            self.assertIn(required, sql)


if __name__ == "__main__":
    unittest.main()
