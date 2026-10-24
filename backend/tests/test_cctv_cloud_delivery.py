"""Numeric-only remote collector delivery; fixtures never fetch live media."""
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from services import cctv_delivery as delivery
from services.cctv_runtime import _worker_environment, parse_worker_event
from backend.tests.test_cctv_store import evidence

TARGET = "https://cross-flow-ai.vercel.app/api/internal/cctv-observations"
TOKEN = "fixture-cctv-credential-32-characters"


class Response:
    status = 200
    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def read(self, maximum): return self.body[:maximum]


class FakeOpener:
    def __init__(self, body=None, error=None):
        self.body, self.error, self.requests = body, error, []
    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error is not None: raise self.error
        return Response(self.body)


class CloudDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"CROSSFLOW_CCTV_INGEST_URL": TARGET,
                                                   "CROSSFLOW_CCTV_INGEST_TOKEN": TOKEN}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_delivers_numeric_only_and_returns_bounded_safe_receipt(self):
        item = evidence()
        opener = FakeOpener(json.dumps({"cctv_observation": {"accepted": True, "camera_id": item["camera_id"],
                                                             "observed_at": item["captured_at"], "idempotent": False}}).encode())
        self.assertEqual(delivery.publish(item, opener=opener), {"accepted": True, "idempotent": False})
        request, timeout = opener.requests[0]
        self.assertEqual(timeout, 2.)
        self.assertEqual(request.full_url, TARGET)
        self.assertEqual(request.get_header("X-crossflow-cctv-token"), TOKEN)
        self.assertNotIn("image_base64", request.data.decode())
        self.assertEqual(json.loads(request.data), item)

    def test_incomplete_configuration_never_silently_disables_delivery(self):
        self.assertTrue(delivery.configured())
        with patch.dict(os.environ, {"CROSSFLOW_CCTV_INGEST_URL": ""}):
            self.assertTrue(delivery.configured())
            with self.assertRaises(delivery.CCTVDeliveryError) as caught:
                delivery.publish(evidence(), opener=FakeOpener())
            self.assertEqual(caught.exception.code, "delivery_configuration")
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(delivery.configured())

    def test_rejects_credentials_queries_redirects_and_wrong_endpoint(self):
        for url in ("http://example.org/api/internal/cctv-observations", TARGET + "?token=secret",
                    "https://user:secret@example.org/api/internal/cctv-observations", TARGET + "#fragment",
                    "https://example.org/other", "https://example.org:8443/api/internal/cctv-observations"):
            with self.subTest(url=url), patch.dict(os.environ, {"CROSSFLOW_CCTV_INGEST_URL": url}):
                opener = FakeOpener()
                with self.assertRaises(delivery.CCTVDeliveryError):
                    delivery.publish(evidence(), opener=opener)
                self.assertEqual(opener.requests, [])
        self.assertIsNone(delivery.NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.example"))

    def test_rejects_media_and_nonfinite_payload_before_sending(self):
        for changes in ({"image_base64": "frames"}, {"stream_url": "secret"}, {"images_retained": True},
                        {"vehicle_count": float("nan")}, {"measurement_scope": "x" * 32769}):
            with self.subTest(changes=changes):
                opener = FakeOpener()
                with self.assertRaises(delivery.CCTVDeliveryError):
                    delivery.publish({**evidence(), **changes}, opener=opener)
                self.assertEqual(opener.requests, [])

    def test_transient_failures_retry_without_exposing_request_url_or_token(self):
        for error in (URLError("private details"), TimeoutError("private"),
                      HTTPError(TARGET, 503, TOKEN, {}, None)):
            with self.subTest(error=type(error)), self.assertRaises(delivery.CCTVDeliveryError) as caught:
                delivery.publish(evidence(), opener=FakeOpener(error=error))
            self.assertTrue(caught.exception.retryable)
            self.assertEqual(str(caught.exception), "delivery_unavailable")
        with self.assertRaises(delivery.CCTVDeliveryError) as caught:
            delivery.publish(evidence(), opener=FakeOpener(error=HTTPError(TARGET, 403, TOKEN, {}, None)))
        self.assertFalse(caught.exception.retryable)

    def test_wrong_camera_oversize_or_invalid_receipts_are_not_success(self):
        for body in (b"x" * 4097, b"not json", b"{}", b'{"cctv_observation":{"accepted":true}}'):
            with self.subTest(body=body[:20]), self.assertRaises(delivery.CCTVDeliveryError):
                delivery.publish(evidence(), opener=FakeOpener(body))

    def test_child_only_receives_scoped_delivery_token_and_safe_diagnostics(self):
        with patch.dict(os.environ, {"SUPABASE_SECRET_KEY": "secret", "CROSSFLOW_ADMIN_TOKEN": "admin"}):
            environment = _worker_environment()
        self.assertEqual(environment["CROSSFLOW_CCTV_INGEST_TOKEN"], TOKEN)
        self.assertNotIn("SUPABASE_SECRET_KEY", environment)
        self.assertNotIn("CROSSFLOW_ADMIN_TOKEN", environment)
        event = {"schema": "crossflow.cctv_worker_event.v1", "camera_id": "15", "event": "retrying",
                 "stage": "evidence_store", "code": "delivery_unavailable", "token": TOKEN}
        parsed = parse_worker_event(json.dumps(event).encode(), "15")
        self.assertEqual(parsed["code"], "delivery_unavailable")
        self.assertNotIn(TOKEN, json.dumps(parsed))


if __name__ == "__main__": unittest.main()
