"""Crop geometry fixtures verify coordinates and policy identity, not model accuracy."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import io
import math
from pathlib import Path
import random
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import cctv_capture as capture
from services import cctv_store as store
from services.cctv_analysis import CameraConfig, validate_evidence

START = datetime(2026, 10, 24, tzinfo=timezone.utc)
ROI = ((.35, .35), (.65, .35), (.65, .65), (.35, .65))


class ImagePolicyTests(unittest.TestCase):
    def test_default_policy_preserves_existing_counter_identity(self):
        self.assertEqual(store.collector_detector_id("yolo26s-fixture", {}), "yolo26s-fixture")
        self.assertEqual(store.collector_detector_id("yolo26s-fixture", {"inference_crop": None, "decode_max_dimension": 1280}), "yolo26s-fixture")

    def test_crop_and_decode_policies_have_deterministic_distinct_identities(self):
        base = "yolo26s-fixture"
        first = {"inference_crop": [0, .25, 1, .75], "decode_max_dimension": 1920}
        normalized = {"decode_max_dimension": 1920, "inference_crop": [0., .25, 1., .75], "background_max_shift_px": 1.}
        self.assertEqual(store.collector_detector_id(base, first), store.collector_detector_id(base, normalized))
        self.assertNotEqual(store.collector_detector_id(base, first), store.collector_detector_id(base, {**first, "decode_max_dimension": 1280}))
        self.assertNotEqual(store.collector_detector_id(base, first), store.collector_detector_id(base, {**first, "inference_crop": [0., .2, 1., .75]}))
        long = store.collector_detector_id("x" * 128, first)
        self.assertLessEqual(len(long), 128)
        self.assertNotEqual(long, store.collector_detector_id("x" * 127 + "y", first))

    def test_invalid_crops_and_unbounded_decode_resolutions_are_rejected(self):
        invalid = ([.2, .2, .2, .8], [.7, .1, .4, .9], [-.1, 0, 1, 1], [0, 0, 1.1, 1],
                   [True, 0, 1, 1], [0, 0, math.nan, 1], [0, 0, 1], "road")
        for value in invalid:
            with self.subTest(crop=value), self.assertRaises(ValueError):
                store._collector_config({"inference_crop": value})
        for value in (True, 1920., 0, 4096, "1920"):
            with self.subTest(resolution=value), self.assertRaises(ValueError):
                store._collector_config({"decode_max_dimension": value})

    def test_crop_must_cover_the_entire_registered_road_roi_before_network(self):
        config = CameraConfig("fixture", enabled=True, analysis_authorized=True, authorization_reference="engineering", roi=ROI)
        registered = {"analysis_config": asdict(config), "stream_url": "https://camera.example/live.m3u8",
                      "collector_config": {"inference_crop": [.4, .4, .6, .6], "fixed_view_confirmed": True,
                                           "background_region": ROI, "source_clock_validation_reference": "engineering"}}
        fetch = Mock(side_effect=AssertionError("Network must not run"))
        with self.assertRaises(capture.CaptureBlocked):
            capture.collect_camera(registered, counter=object(), fetch=fetch)
        fetch.assert_not_called()

    def test_optional_decode_resolution_is_local_bounded_and_never_upscales(self):
        completed = subprocess.CompletedProcess([], 0, stdout=b"\xff\xd8fake\xff\xd9", stderr=b"n: 0 pts: 0 pts_time:0")
        with patch.object(capture.subprocess, "run", return_value=completed) as run:
            capture.decode_segment(b"fixture", capture.Segment("https://camera.example/x.ts", START, 2.), 1., executable="/local/ffmpeg", max_dimension=1920)
        command = run.call_args.args[0]
        filters = command[command.index("-vf") + 1]
        self.assertIn("min(1920,iw)", filters)
        self.assertIn("min(1080,ih)", filters)
        self.assertEqual(command[command.index("-protocol_whitelist") + 1], "pipe")
        self.assertNotIn("https://camera.example/x.ts", command)


class InMemoryCropTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PIL import Image
        except ImportError:
            raise unittest.SkipTest("Pillow is installed in the isolated YOLO runtime")

    def jpeg(self, width=640, height=360):
        from PIL import Image
        image = Image.new("RGB", (width, height), (55, 100, 155))
        output = io.BytesIO()
        image.save(output, "JPEG", quality=95)
        return output.getvalue()

    def counter(self, detections):
        class Counter:
            calls = []
            model_id = "yolo26s-fixture"
            def count_image(self, data, *, roi):
                from PIL import Image
                with Image.open(io.BytesIO(data)) as image:
                    width, height = image.size
                self.calls.append({"roi": roi, "size": (width, height)})
                return {"detections": deepcopy(detections), "image_width": width, "image_height": height,
                        "model_id": self.model_id, "may_be_truncated": False}
        return Counter()

    def test_crop_detections_remap_to_full_frame_and_apply_the_original_roi(self):
        counter = self.counter([
            {"box": [.1, .1, .9, .9], "class_name": "car", "score": .9},
            {"box": [0., 0., .2, .2], "class_name": "car", "score": .9},
        ])
        policy = {"inference_crop": [.25, .25, .75, .75]}
        output = capture.count_frame(counter, self.jpeg(), roi=ROI, collector=policy)
        self.assertEqual(counter.calls, [{"roi": None, "size": (320, 180)}])
        self.assertEqual(output["vehicle_count"], 1)
        for actual, expected in zip(output["detections"][0]["box"], (.3, .3, .7, .7)):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual((output["image_width"], output["image_height"]), (640, 360))
        self.assertEqual(output["model_id"], store.collector_detector_id(counter.model_id, policy))
        self.assertEqual(counter.model_id, "yolo26s-fixture")

    def test_fractional_crop_rounding_uses_actual_decoded_pixel_bounds(self):
        counter = self.counter([{"box": [0., 0., 1., 1.], "class_name": "truck", "score": .8}])
        output = capture.count_frame(counter, self.jpeg(101, 79), roi=None,
                                     collector={"inference_crop": [.123, .135, .87, .91]})
        self.assertEqual(counter.calls[0]["size"], (76, 62))
        for actual, expected in zip(output["detections"][0]["box"], (12 / 101, 10 / 79, 88 / 101, 72 / 79)):
            self.assertAlmostEqual(actual, expected)

    def test_mismatched_detector_dimensions_cannot_corrupt_full_frame_geometry(self):
        counter = Mock()
        counter.count_image.return_value = {"detections": [], "image_width": 999, "image_height": 999, "model_id": "fixture"}
        with self.assertRaises(capture.CaptureBlocked):
            capture.count_frame(counter, self.jpeg(), roi=ROI, collector={"inference_crop": [.25, .25, .75, .75]})

    def test_crop_does_not_change_full_frame_stability_exposure_or_source_times(self):
        from PIL import Image
        rng = random.Random(71)
        image = Image.new("L", (64, 36))
        image.putdata([rng.randrange(20, 235) for _ in range(64 * 36)])
        body = io.BytesIO()
        image.convert("RGB").save(body, "JPEG", quality=95)
        config = CameraConfig("fixture", enabled=True, analysis_authorized=True, authorization_reference="engineering", roi=ROI,
                              minimum_samples=3, minimum_window_seconds=2, stationary_seconds=3, congestion_persistence_seconds=2)
        registered = {"camera_id": "fixture", "analysis_config": asdict(config), "stream_url": "https://camera.example/live.m3u8",
                      "collector_config": {"inference_crop": [.25, .25, .75, .75], "fixed_view_confirmed": True,
                                           "background_region": [[0., 0.], [1., 0.], [1., 1.], [0., 1.]],
                                           "frame_interval_seconds": 2., "source_clock_validation_reference": "engineering"}}
        counter = self.counter([{"box": [.25, .25, .75, .75], "class_name": "car", "score": .95}])
        received, observed_sizes, elapsed = [], [], [0.]
        def stability(monitor, gray):
            observed_sizes.append(gray.size)
            return True
        def fetch(url, maximum):
            return (b"#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:2026-10-24T00:00:00Z\n#EXTINF:8,\nsegment.ts\n"
                    if url.endswith("m3u8") else b"fixture")
        def ingest(evidence, *, now):
            received.append(validate_evidence(evidence, config, now=now))
            return {"accepted": True}
        with patch.object(capture.BackgroundMonitor, "check", stability):
            result = capture.collect_camera(registered, max_seconds=1, fetch=fetch,
                decode=lambda data, segment, interval: [(START + timedelta(seconds=i), body.getvalue()) for i in range(7)],
                counter=counter, ingest=ingest, utcnow=lambda: START + timedelta(seconds=7),
                monotonic=lambda: elapsed[0], sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + max(seconds, .001)))
        self.assertEqual(observed_sizes, [(64, 36)] * 7)
        self.assertTrue(all(call["size"] == (32, 18) for call in counter.calls))
        self.assertEqual(received[0]["captured_at"], "2026-10-24T00:00:00Z")
        self.assertEqual(received[-1]["captured_at"], "2026-10-24T00:00:06Z")
        self.assertEqual(received[-1]["vehicle_count"], 1)
        self.assertTrue(received[-1]["quality"]["usable"])
        self.assertTrue(received[-1]["detector_id"].startswith("yolo26s-fixture-view"))
        self.assertFalse(result["images_retained"])


if __name__ == "__main__":
    unittest.main()
