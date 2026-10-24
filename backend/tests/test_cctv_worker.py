"""Bounded collector protocol tests; no live CCTV or model weights are accessed."""
from __future__ import annotations

import base64
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import cctv_worker
from services.cctv_analysis import CameraConfig


class Counter:
    def __init__(self):
        self.calls = 0

    def count_image(self, data, *, roi):
        self.calls += 1
        if data == b"bad":
            raise ValueError("decode failure with private data")
        return {"detections": [{"box": [.1, .1, .4, .4], "score": .9, "class_name": "car"}],
                "image_width": 64, "image_height": 64, "model_id": "test-counter", "may_be_truncated": False}


class WorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PIL import Image
        except ImportError:
            raise unittest.SkipTest("Worker image-boundary tests require optional vision/Pillow runtime")
        image = Image.new("RGB", (64, 64), (100, 100, 100))
        for x in range(32):
            for y in range(64):
                image.putpixel((x, y), (200, 200, 200))
        output = io.BytesIO()
        image.save(output, "PNG")
        cls.image_base64 = base64.b64encode(output.getvalue()).decode()

    def cfg(self):
        return CameraConfig("fixture-camera", enabled=True, analysis_authorized=True,
                            authorization_reference="engineering-test-only", roi=((0, 0), (1, 0), (1, 1), (0, 1)))

    def run_worker(self, lines, *, cfg=None, counter=None):
        counter = counter or Counter()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cctv_worker.process_frames(cfg or self.cfg(), Path("unused"), io.BytesIO(lines), counter=counter,
                                              now=datetime(2026, 10, 24, tzinfo=timezone.utc))
        return code, [json.loads(line) for line in out.getvalue().splitlines()], counter

    def line(self, **kwargs):
        return json.dumps({"captured_at": "2026-10-24T00:00:00Z", "image_base64": self.image_base64,
                           "camera_stable": True, **kwargs}).encode() + b"\n"

    def test_disabled_camera_does_not_read_or_infer(self):
        code, results, counter = self.run_worker(b"not-json", cfg=CameraConfig("disabled"))
        self.assertEqual(code, 2)
        self.assertEqual(counter.calls, 0)
        self.assertEqual(results[0]["code"], "camera_disabled")

    def test_valid_image_becomes_numeric_evidence_without_image_or_stream_data(self):
        code, results, counter = self.run_worker(self.line(stream_url="should-not-appear"))
        self.assertEqual(code, 0)
        self.assertEqual(counter.calls, 1)
        self.assertTrue(results[0]["ok"])
        evidence = results[0]["evidence"]
        self.assertEqual(evidence["vehicle_count"], 1)
        self.assertFalse(evidence["images_retained"])
        self.assertNotIn("image_base64", json.dumps(evidence))
        self.assertNotIn("stream_url", json.dumps(evidence))
        self.assertFalse(evidence["operational_eligible"])

    def test_missing_stability_evidence_blocks_congestion(self):
        _, results, _ = self.run_worker(self.line(camera_stable=None))
        self.assertIn("camera_stability_unknown", results[0]["evidence"]["quality"]["reasons"])
        self.assertIsNone(results[0]["evidence"]["congestion_score"])

    def test_bad_base64_and_corrupt_frame_return_sanitized_errors_then_continue(self):
        raw = self.line(image_base64="!bad!") + self.line(image_base64=base64.b64encode(b"bad").decode()) + self.line()
        _, results, counter = self.run_worker(raw)
        self.assertEqual([r.get("code") for r in results], ["invalid_frame", "invalid_frame", None])
        self.assertEqual(counter.calls, 2)
        self.assertNotIn("private data", json.dumps(results))

    def test_oversized_line_is_drained_as_one_frame(self):
        with patch.object(cctv_worker, "MAX_LINE_BYTES", 1024):
            _, results, _ = self.run_worker(b"x" * 2500 + b"\n" + self.line())
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["code"], "frame_too_large")
        self.assertTrue(results[1]["ok"])

    def test_uniform_dark_frame_is_blocked_as_exposure(self):
        from PIL import Image
        image = Image.new("RGB", (10, 10), (0, 0, 0))
        out = io.BytesIO()
        image.save(out, "PNG")
        _, results, _ = self.run_worker(self.line(image_base64=base64.b64encode(out.getvalue()).decode()))
        self.assertIn("poor_exposure", results[0]["evidence"]["quality"]["reasons"])

    def test_duplicate_capture_is_rejected(self):
        _, results, _ = self.run_worker(self.line() + self.line())
        self.assertTrue(results[0]["ok"])
        self.assertEqual(results[1]["code"], "invalid_frame")


if __name__ == "__main__":
    unittest.main()
