"""Temporary-source recovery and safe child-protocol supervision; no live media."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import random
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import cctv_capture as capture
from services.cctv_analysis import CameraConfig
from services.cctv_runtime import CCTVRuntime, parse_worker_event, _worker_environment

START = datetime(2026, 10, 24, tzinfo=timezone.utc)
URL = "https://private-camera.example/live.m3u8?secret=must-not-log"
ROI = ((0., 0.), (1., 0.), (1., 1.), (0., 1.))


def event(**changed):
    return {"schema": "crossflow.cctv_worker_event.v1", "camera_id": "15", "event": "retrying",
            "stage": "playlist_fetch", "code": "source_timeout", **changed}


class RuntimeProtocolTests(unittest.TestCase):
    def test_parser_drops_urls_exception_text_frames_and_unapproved_keys(self):
        raw = event(stream_url=URL, message="private exception", image_base64="frames", api_key="secret", accepted=3)
        parsed = parse_worker_event(json.dumps(raw).encode(), "15")
        self.assertEqual(parsed["code"], "source_timeout")
        self.assertEqual(parsed["accepted"], 3)
        self.assertNotIn("secret", json.dumps(parsed))
        self.assertNotIn("private", json.dumps(parsed))
        self.assertNotIn("image_base64", parsed)

    def test_parser_rejects_oversize_nonfinite_wrong_camera_and_unknown_codes(self):
        for raw in (b"x" * 4097, b'{"x":NaN}', json.dumps(event(camera_id="other")).encode(),
                    json.dumps(event(code="https://private.example/secret")).encode(),
                    json.dumps(event(event=[])).encode(), json.dumps(event(retry_in_seconds=True)).encode(),
                    json.dumps(event(accepted=-1)).encode()):
            with self.subTest(raw=raw[:80]):
                self.assertIsNone(parse_worker_event(raw, "15"))

    def test_success_receipt_normalizes_without_copying_nested_private_fields(self):
        parsed = parse_worker_event(json.dumps({"ok": True, "result": {"accepted": 5, "frames": 5, "stream_url": URL}}).encode(), "15")
        self.assertEqual(parsed["event"], "completed")
        self.assertEqual(parsed["accepted"], 5)
        self.assertNotIn("secret", json.dumps(parsed))

    def test_environment_drops_api_credentials_and_preserves_store_and_tls_paths(self):
        with patch.dict("os.environ", {"CROSSFLOW_CCTV_DB": "/local/store.sqlite", "SSL_CERT_FILE": "/local/ca.pem",
                                        "OPENAI_API_KEY": "secret", "SUPABASE_SERVICE_ROLE_KEY": "secret"}, clear=True):
            env = _worker_environment()
        self.assertEqual(env["CROSSFLOW_CCTV_DB"], "/local/store.sqlite")
        self.assertEqual(env["SSL_CERT_FILE"], "/local/ca.pem")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("SUPABASE_SERVICE_ROLE_KEY", env)

    def test_internal_ring_is_bounded_and_snapshots_do_not_mutate_state(self):
        runtime = CCTVRuntime()
        for i in range(30):
            runtime._record({"camera_id": "15", "event": "progress", "stage": "evidence_store", "code": "ok", "accepted": i})
        snapshot = runtime.diagnostics()
        self.assertEqual(len(snapshot["collectors"][0]["recent_events"]), 20)
        snapshot["collectors"][0]["last_event"]["code"] = "modified"
        self.assertEqual(runtime.diagnostics()["collectors"][0]["last_event"]["code"], "ok")

    def test_reader_drains_oversized_lines_and_keeps_next_safe_event(self):
        class Process:
            stdout = io.BytesIO(b"x" * 9000 + b"\n" + json.dumps(event(event="progress", stage="evidence_store", code="ok")).encode() + b"\n")
        runtime = CCTVRuntime()
        runtime._read_events(Process(), "15")
        self.assertEqual(runtime.diagnostics()["collectors"][0]["last_event"]["event"], "progress")

    def test_supervisor_retains_safe_child_failure_and_uses_one_hour_rotation(self):
        class Stop:
            flag = False
            def is_set(self): return self.flag
            def wait(self, _seconds): self.flag = True; return True
        class Process:
            def __init__(self): self.stdout = io.BytesIO(json.dumps(event(event="failed", code="source_http_rejected")).encode() + b"\n")
            def poll(self): return 2
            def wait(self, timeout=None): return 2
        runtime = CCTVRuntime()
        runtime._stop = Stop()
        with patch("services.cctv_runtime.subprocess.Popen", return_value=Process()) as popen, self.assertLogs("services.cctv_runtime", level="WARNING") as logs:
            self.assertEqual(runtime._collect_once(Path("/local/python"), "15"), "failed")
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--max-seconds") + 1], "3600")
        self.assertIsNotNone(popen.call_args.kwargs["stdout"])
        self.assertEqual(runtime.diagnostics()["collectors"][0]["last_failure"]["code"], "source_http_rejected")
        self.assertNotIn("secret", " ".join(logs.output))


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PIL import Image
        except ImportError:
            raise unittest.SkipTest("Optional collector/Pillow runtime required")
        rng = random.Random(31)
        image = Image.new("L", (64, 36))
        image.putdata([rng.randrange(20, 235) for _ in range(64 * 36)])
        data = io.BytesIO()
        image.convert("RGB").save(data, "JPEG", quality=95)
        cls.jpeg = data.getvalue()

    def test_temporary_playlist_timeout_preserves_background_and_temporal_window(self):
        config = CameraConfig("15", enabled=True, analysis_authorized=True, authorization_reference="engineering-only", roi=ROI,
                              minimum_samples=3, minimum_window_seconds=2, stationary_seconds=3, congestion_persistence_seconds=2)
        row = {"camera_id": "15", "analysis_config": asdict(config), "stream_url": URL,
               "collector_config": {"fixed_view_confirmed": True, "background_region": ROI,
                                    "source_clock_validation_reference": "engineering-clock", "frame_interval_seconds": 2.}}
        class Clock:
            value = 0.
            def now(self): return START + timedelta(seconds=7 + self.value)
            def mono(self): return self.value
            def sleep(self, seconds): self.value += max(seconds, .001)
        class Counter:
            calls = 0
            def count_image(self, _image, *, roi):
                self.calls += 1
                return {"detections": [], "image_width": 640, "image_height": 480, "model_id": "fixture-detector"}
        clock, counter, received, events, playlists = Clock(), Counter(), [], [], [0]
        def fetch(url, maximum):
            if url == URL:
                playlists[0] += 1
                if playlists[0] == 2:
                    raise URLError(TimeoutError("private network detail"))
                offset = 0 if playlists[0] == 1 else 8
                return ("#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:" + (START + timedelta(seconds=offset)).isoformat() +
                        f"\n#EXTINF:8,\nsegment{offset}.ts\n").encode()
            return b"local-fixture"
        def decode(_data, segment, interval):
            return [(segment.captured_at + timedelta(seconds=t), self.jpeg) for t in range(7)]
        def ingest(evidence, *, now):
            received.append(evidence)
            return {"accepted": True}
        result = capture.collect_camera(row, max_seconds=5, fetch=fetch, decode=decode, counter=counter, ingest=ingest,
                                        utcnow=clock.now, monotonic=clock.mono, sleep=clock.sleep, event_sink=events.append)
        self.assertEqual(result["source_retries"], 1)
        self.assertEqual(len([r for r in received if "camera_stability_unknown" in r["quality"]["reasons"]]), 1)
        self.assertGreater(received[-1]["window_seconds"], received[6]["window_seconds"])
        self.assertTrue(received[-1]["quality"]["usable"])
        self.assertTrue(any(e["code"] == "source_timeout" and e["event"] == "retrying" for e in events))
        self.assertNotIn("private", json.dumps(events))
        self.assertFalse(result["images_retained"])

    def test_http_segment_expiry_retries_but_auth_rejections_remain_fatal(self):
        expired = HTTPError(URL, 404, "private detail", None, None)
        self.assertEqual(capture.safe_error(expired, "segment_fetch"), ("segment_expired", True))
        self.assertEqual(capture.safe_error(expired, "playlist_fetch"), ("source_http_rejected", False))
        denied = HTTPError(URL, 403, "private detail", None, None)
        self.assertEqual(capture.safe_error(denied, "playlist_fetch"), ("source_http_rejected", False))

    def test_stalled_source_is_retryable_without_redating_old_segment(self):
        monitor = capture.ObservedPlaylistClock(10)
        with self.assertRaises(capture.CapturePending):
            monitor.resolve(1, URL, 8, START)
        original = monitor.resolve(2, URL, 8, START + timedelta(seconds=8))
        with self.assertRaises(capture.CaptureBlocked) as caught:
            monitor.resolve(2, URL, 8, START + timedelta(seconds=20))
        self.assertEqual(capture.safe_error(caught.exception, "playlist_parse"), ("source_stalled", True))
        self.assertEqual(monitor.segment.captured_at, original.captured_at)


if __name__ == "__main__":
    unittest.main()
