"""Real ASGI → registered-camera store → map checks, using engineering fixtures.

These checks establish application behavior, not traffic-model accuracy.
"""
from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from backend.tests.test_vehicle_count_preview_api import api, asgi_request
from backend.tests.test_cctv_store import NOW, configured, evidence, row
from services import cctv_store, cctv_availability, live_traffic

INGEST = "/api/internal/cctv-observations"
STATUS = "/api/analytics/cctv-status"
ADMIN = (("x-crossflow-admin-token", "engineering-machine-credential"),)


class CCTVApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.registry, self.database = root / "registry.json", root / "evidence.sqlite"
        self.registry.write_text(json.dumps({"schema_version": 1, "cameras": [row(configured())]}))
        self.environment = patch.dict(os.environ, {
            "CROSSFLOW_CCTV_REGISTRY": str(self.registry), "CROSSFLOW_CCTV_DB": str(self.database),
            "CROSSFLOW_ADMIN_TOKEN": "engineering-machine-credential",
        })
        self.environment.start()
        self.instant = patch.object(api.clock, "now", return_value=NOW)
        self.instant.start()

    def tearDown(self):
        self.instant.stop()
        self.environment.stop()
        self.tmp.cleanup()

    def request(self, payload=None, **kwargs):
        values = {"path": INGEST, "content_type": "application/json", "extra_headers": ADMIN,
                  "chunks": (json.dumps(payload if payload is not None else evidence()).encode(),)}
        values.update(kwargs)
        return asgi_request(**values)

    def assert_private(self, result):
        self.assertIn(b"no-store", result["headers"][b"cache-control"])

    def test_machine_authorization_precedes_body_read_and_ingestion(self):
        for headers in ((), (("x-crossflow-admin-token", "wrong"),)):
            with self.subTest(headers=headers), patch.object(cctv_store, "ingest_evidence") as ingest:
                result = self.request(extra_headers=headers)
            self.assertEqual(result["status"], 403)
            self.assertEqual(result["reads"], 0)
            ingest.assert_not_called()
            self.assert_private(result)
        self.assertFalse(self.database.exists())

    def test_no_configured_machine_credential_also_refuses_before_read(self):
        with patch.dict(os.environ, {"CROSSFLOW_ADMIN_TOKEN": ""}):
            result = self.request()
        self.assertEqual(result["status"], 403)
        self.assertEqual(result["reads"], 0)

    def test_scoped_collector_token_ingests_but_cannot_authorize_other_admin_actions(self):
        scoped = "engineering-cctv-only-token-32-characters"
        with patch.dict(os.environ, {"CROSSFLOW_CCTV_INGEST_TOKEN": scoped, "CROSSFLOW_ADMIN_TOKEN": ""}):
            result = self.request(extra_headers=(("x-crossflow-cctv-token", scoped),))
            self.assertEqual(result["status"], 200)
            self.assertTrue(result["payload"]["cctv_observation"]["accepted"])
            with self.assertRaises(HTTPException):
                api._require_admin(scoped)

    def test_wrong_scoped_token_refuses_before_body_read(self):
        with patch.dict(os.environ, {"CROSSFLOW_CCTV_INGEST_TOKEN": "configured-scoped-only-token", "CROSSFLOW_ADMIN_TOKEN": ""}):
            result = self.request(extra_headers=(("x-crossflow-cctv-token", "wrong"),))
        self.assertEqual(result["status"], 403)
        self.assertEqual(result["reads"], 0)
        self.assert_private(result)

    def test_operator_status_read_does_not_fetch_media_or_create_storage(self):
        with patch.object(api.identity, "require_route_operator") as authorize:
            result = asgi_request(method="GET", path=STATUS, content_type=None)
        authorize.assert_called_once_with("Bearer operator")
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["payload"]["data_source"], "cctv_traffic_observations")
        monitor = result["payload"]["camera_monitor"]
        self.assertEqual(monitor["summary"]["live"], 0)
        self.assertIsNone(monitor["cameras"][0]["vehicle_count"])
        self.assertNotIn("private-feed", json.dumps(result["payload"]))
        self.assertFalse(self.database.exists())
        self.assert_private(result)

    def test_status_requires_operator_under_configured_auth_policy(self):
        with patch.object(api.identity, "require_route_operator", side_effect=HTTPException(403, "Denied")), patch.object(cctv_store, "camera_status") as read:
            result = asgi_request(method="GET", path=STATUS, content_type=None)
        self.assertEqual(result["status"], 403)
        read.assert_not_called()
        self.assert_private(result)

    def test_operator_availability_contract_is_private_readonly_and_not_a_congestion_measurement(self):
        instant = [NOW - timedelta(seconds=20)]
        sequence = [1]
        def fetch(_url):
            return (f"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:{sequence[0]}\n#EXTINF:4,\n"
                    f"segment-{sequence[0]}.ts\n").encode()
        monitor = cctv_availability.AvailabilityMonitor(fetch=fetch, monotonic=lambda: instant[0].timestamp(), utcnow=lambda: instant[0])
        monitor.reconcile(cctv_store.load_registry())
        monitor.check_once()
        instant[0], sequence[0] = NOW, 2
        monitor.check_once()
        with patch.object(cctv_availability, "MONITOR", monitor), \
                patch.object(monitor, "_fetch") as no_network, \
                patch.object(api.identity, "require_route_operator") as authorize:
            result = asgi_request(method="GET", path=STATUS, content_type=None)
        authorize.assert_called_once_with("Bearer operator")
        no_network.assert_not_called()
        self.assertEqual(result["status"], 200)
        value = result["payload"]["camera_monitor"]
        self.assertEqual(value["availability_summary"], {"advancing": 1})
        self.assertEqual(value["availability_policy"]["registered_sources"], 1)
        self.assertFalse(value["availability_policy"]["grants_inference_or_routing_eligibility"])
        self.assertEqual(value["cameras"][0]["source_availability"]["status"], "advancing")
        self.assertIsNone(value["cameras"][0]["congestion_score"])
        self.assertNotIn(".m3u8", json.dumps(value))
        self.assertNotIn("secret", json.dumps(value))
        self.assertFalse(self.database.exists())
        self.assert_private(result)

    def test_valid_numeric_observation_reaches_status_then_map_without_fabricating_speed(self):
        result = self.request()
        self.assertEqual(result["status"], 200)
        self.assertTrue(result["payload"]["cctv_observation"]["accepted"])
        self.assert_private(result)
        with patch.object(api.identity, "require_route_operator"):
            status = asgi_request(method="GET", path=STATUS, content_type=None)["payload"]["camera_monitor"]
        self.assertEqual(status["summary"]["live"], 1)
        self.assertEqual(status["cameras"][0]["stopped_vehicles"], 1)
        with patch.object(live_traffic, "_tomtom_segments", return_value={}), patch.object(live_traffic, "_tomtom_api_key", return_value=""), patch("services.simulator.get_live_corridor_telemetry", return_value=[]):
            snapshot = asgi_request(method="GET", path="/api/live-traffic", content_type=None)["payload"]
        self.assertEqual(snapshot["data_source"], "cctv_traffic_observations")
        self.assertEqual(snapshot["overall_source"], "cctv_camera")
        camera = snapshot["segments"][0]
        self.assertEqual(camera["source"], "cctv_camera")
        self.assertEqual(camera["camera_ids"], ["camera-1"])
        self.assertEqual(camera["congestion_index"], evidence()["congestion_score"])
        self.assertEqual(camera["measurement_scope"], "registered_camera_road_view")
        self.assertIsNone(camera["current_speed_kmh"])
        self.assertIsNone(camera["free_flow_speed_kmh"])
        self.assertIsNone(camera["provider_confidence"])
        self.assertIn("CCTV", snapshot["provenance"]["traffic"])
        signals = [zone["score_breakdown"]["input_signals"].get("corridor-1") for zone in snapshot["zones"]]
        self.assertTrue(any(signal and signal["source"] == "cctv_camera" for signal in signals))
        self.assertTrue(all(zone["score_breakdown"]["observed"] is False for zone in snapshot["zones"]))

    def test_exact_replay_is_idempotent_conflict_and_out_of_order_are_409(self):
        self.assertEqual(self.request()["status"], 200)
        replay = self.request()
        self.assertTrue(replay["payload"]["cctv_observation"]["idempotent"])
        conflict = deepcopy(evidence()); conflict["measurement_scope"] = "changed"
        for payload in (conflict, evidence(offset=-1)):
            result = self.request(payload)
            self.assertEqual(result["status"], 409)
            self.assert_private(result)

    def test_unregistered_disabled_stale_or_forged_evidence_cannot_reach_map(self):
        for change in ({"camera_id": "unknown"}, {"congestion_score": 2}, {"speed_kmh": 10},
                       {"image_base64": "aGVsbG8="}, {"stream_url": "https://example.org/video.m3u8"}):
            payload = deepcopy(evidence()); payload.update(change)
            self.assertEqual(self.request(payload)["status"], 422)
        with patch.object(api.clock, "now", return_value=NOW + timedelta(seconds=16)):
            self.assertEqual(self.request()["status"], 422)
        self.assertFalse(self.database.exists())

    def test_transport_rejects_non_json_and_declared_oversize_before_read(self):
        for options, code in (({"content_type": "image/jpeg"}, 415),
                              ({"content_length": api.MAX_CCTV_EVIDENCE_BYTES + 1}, 413),
                              ({"content_length": 0}, 413), ({"content_length": "bad"}, 400)):
            with self.subTest(options=options), patch.object(cctv_store, "ingest_evidence") as ingest:
                result = self.request(**options)
            self.assertEqual(result["status"], code)
            self.assertEqual(result["reads"], 0)
            ingest.assert_not_called()
            self.assert_private(result)

    def test_stream_limit_rejects_forged_length(self):
        chunks = (b"x" * api.MAX_CCTV_EVIDENCE_BYTES, b"x", b"unread")
        with patch.object(cctv_store, "ingest_evidence") as ingest:
            result = self.request(chunks=chunks, content_length=1)
        self.assertEqual(result["status"], 413)
        self.assertEqual(result["reads"], 2)
        ingest.assert_not_called()

    def test_malformed_nonfinite_or_nonobject_json_is_rejected(self):
        for body in (b"", b"{", b"[]", b"null", b'{"score":NaN}', b"\xff",
                     b'{"camera_id":"first","camera_id":"second"}', b"[" * 2000):
            with self.subTest(body=body):
                self.assertEqual(self.request(chunks=(body,))["status"], 422)

    def test_storage_error_has_sanitized_private_503(self):
        with patch.object(cctv_store, "ingest_evidence", side_effect=cctv_store.CCTVStoreUnavailable("private/path?secret")):
            result = self.request()
        self.assertEqual(result["status"], 503)
        self.assertNotIn("secret", json.dumps(result["payload"]))
        self.assert_private(result)

    def test_stale_or_revoked_camera_yields_explicit_fallback_without_zero_camera_score(self):
        self.assertEqual(self.request()["status"], 200)
        for instant, config in ((NOW + timedelta(seconds=16), configured()),
                                (NOW, configured(analysis_authorized=False))):
            self.registry.write_text(json.dumps({"schema_version": 1, "cameras": [row(config)]}))
            with patch.object(api.clock, "now", return_value=instant), patch.object(live_traffic, "_tomtom_segments", return_value={}), patch.object(live_traffic, "_tomtom_api_key", return_value=""), patch("services.simulator.get_live_corridor_telemetry", return_value=[]):
                snapshot = live_traffic.get_live_traffic()
            self.assertEqual(snapshot["cctv_eligible_corridor_count"], 0)
            self.assertTrue(all(segment["source"] == "simulated" for segment in snapshot["segments"]))
            self.assertTrue(all(segment["camera_ids"] == [] for segment in snapshot["segments"]))

    def test_provider_delay_cannot_make_a_stale_camera_appear_fresh(self):
        self.assertEqual(self.request()["status"], 200)
        # Camera starts at age14s, but the provider returns two seconds later.
        instants = [NOW + timedelta(seconds=14), NOW + timedelta(seconds=16)]
        with patch.object(api.clock, "now", side_effect=instants), patch.object(live_traffic, "_tomtom_segments", return_value={}), patch.object(live_traffic, "_tomtom_api_key", return_value=""), patch("services.simulator.get_live_corridor_telemetry", return_value=[]):
            snapshot = live_traffic.get_live_traffic()
        self.assertEqual(snapshot["cctv_eligible_corridor_count"], 0)
        self.assertEqual(snapshot["segments"][0]["source"], "simulated")

    def test_camera_takes_precedence_without_borrowing_provider_speed(self):
        self.assertEqual(self.request()["status"], 200)
        provider = {"current_speed_kmh": 20., "free_flow_speed_kmh": 60., "confidence": .9}
        segments = {"corridor-1": provider, "corridor-2": provider}
        with patch.object(live_traffic, "_tomtom_segments", return_value=segments), patch.object(live_traffic, "_tomtom_api_key", return_value=""), patch("services.simulator.get_live_corridor_telemetry", return_value=[]):
            snapshot = live_traffic.get_live_traffic()
        self.assertEqual(snapshot["overall_source"], "cctv_camera")
        self.assertEqual(snapshot["segments"][0]["source"], "cctv_camera")
        self.assertIsNone(snapshot["segments"][0]["current_speed_kmh"])
        self.assertEqual(snapshot["segments"][1]["source"], "tomtom_live")
        self.assertEqual(snapshot["segments"][1]["current_speed_kmh"], 20.)


if __name__ == "__main__":
    unittest.main()
