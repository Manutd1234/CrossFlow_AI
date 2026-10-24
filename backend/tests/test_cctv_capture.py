"""Authorized-collector engineering tests. No public CCTV media is fetched."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import random
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import cctv_capture as capture
from services.cctv_analysis import CameraConfig, validate_evidence

START = datetime(2026, 10, 24, tzinfo=timezone.utc)
URL = "https://camera.example/live/playlist.m3u8"
POLYGON = ((0., 0.), (1., 0.), (1., 1.), (0., 1.))


def playlist(**kwargs):
    return ("#EXTM3U\n#EXT-X-TARGETDURATION:8\n#EXT-X-PROGRAM-DATE-TIME:2026-10-24T00:00:00Z\n"
            "#EXTINF:8,\nsegment.ts\n").encode()


def registered(**kwargs):
    cfg = CameraConfig("fixture-camera", corridor_id="fixture-corridor", enabled=True,
                       analysis_authorized=True, authorization_reference="engineering-fixture-only", roi=POLYGON,
                       minimum_samples=3, minimum_window_seconds=2, stationary_seconds=3,
                       congestion_persistence_seconds=2, thresholds_validated=True,
                       threshold_validation_reference="engineering-threshold-fixture-only",
                       independent_validation_reference="engineering-validation-fixture-only", validated_detector_id="fixture-detector")
    return {"camera_id": cfg.camera_id, "analysis_config": asdict(cfg), "stream_url": URL,
            "collector_config": {"frame_interval_seconds": 2., "fixed_view_confirmed": True,
                                 "background_region": POLYGON, "source_clock_validation_reference": "engineering-clock-fixture-only",
                                 "background_max_shift_px": 2.}, **kwargs}


class Counter:
    def __init__(self):
        self.calls = 0

    def count_image(self, data, *, roi):
        self.calls += 1
        return {"detections": [{"box": [.1, .1, .9, .9], "score": .9, "class_name": "car"}],
                "image_width": 640, "image_height": 480, "model_id": "fixture-detector"}


class Clock:
    value = 0.

    def monotonic(self):
        return self.value

    def sleep(self, duration):
        self.value += max(duration, .001)


class PlaylistTests(unittest.TestCase):
    def test_source_program_date_time_is_used_and_relative_segments_resolve(self):
        segment = capture.parse_live_playlist(playlist(), URL)
        self.assertEqual(segment.captured_at, START)
        self.assertEqual(segment.duration, 8.)
        self.assertEqual(segment.url, "https://camera.example/live/segment.ts")

    def test_program_date_time_propagates_by_declared_segment_duration(self):
        body = playlist() + b"#EXTINF:6,\nsecond.ts\n"
        segment = capture.parse_live_playlist(body, URL)
        self.assertEqual(segment.captured_at, START + timedelta(seconds=8))
        self.assertEqual(segment.duration, 6.)

    def test_missing_clock_naive_clock_and_discontinuity_without_new_clock_rejected(self):
        for body in (playlist().replace(b"#EXT-X-PROGRAM-DATE-TIME:2026-10-24T00:00:00Z\n", b""),
                     playlist().replace(b"00:00:00Z", b"00:00:00"),
                     playlist() + b"#EXT-X-DISCONTINUITY\n#EXTINF:8,\nnew.ts\n"):
            with self.subTest(body=body), self.assertRaises(capture.CaptureBlocked):
                capture.parse_live_playlist(body, URL)

    def test_recorded_encrypted_master_crossorigin_or_unbounded_sources_refused(self):
        variants = (playlist() + b"#EXT-X-ENDLIST\n", playlist() + b'#EXT-X-KEY:METHOD=AES-128,URI="key"\n',
                    b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nother.m3u8\n",
                    playlist().replace(b"segment.ts", b"https://other.example/segment.ts"),
                    playlist().replace(b"#EXTINF:8,", b"#EXTINF:nan,"))
        for body in variants:
            with self.subTest(body=body), self.assertRaises(capture.CaptureBlocked):
                capture.parse_live_playlist(body, URL)

    def test_registered_url_requires_https_without_embedded_credentials(self):
        for url in ("http://camera.example/x.m3u8", "https://user:password@camera.example/x.m3u8", "file:///tmp/x.m3u8"):
            with self.assertRaises(capture.CaptureBlocked):
                capture._url(url)


class CaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PIL import Image
        except ImportError:
            raise unittest.SkipTest("Optional collector tests require Pillow/vision runtime")
        rng = random.Random(17)
        cls.gray = Image.new("L", (64, 36))
        cls.gray.putdata([rng.randrange(20, 235) for _ in range(64 * 36)])
        output = io.BytesIO()
        cls.gray.convert("RGB").save(output, "JPEG", quality=95)
        cls.jpeg = output.getvalue()

    def run_capture(self, row=None, body=None, frame_times=range(7), now=None, fetch=None):
        row = row or registered()
        clock, received, calls, counter = Clock(), [], [], Counter()
        now = now or (START + timedelta(seconds=7))
        def get(url, maximum):
            calls.append(url)
            return (body or playlist()) if url == URL else b"dummy-segment"
        def decode(data, segment, interval):
            return [(START + timedelta(seconds=t), self.jpeg) for t in frame_times]
        def ingest(evidence, *, now):
            checked = validate_evidence(evidence, CameraConfig.from_dict(row["analysis_config"]), now=now)
            received.append(checked)
            return {"accepted": True}
        result = capture.collect_camera(row, max_seconds=1, fetch=fetch or get, decode=decode, counter=counter,
                                        ingest=ingest, utcnow=lambda: now, monotonic=clock.monotonic, sleep=clock.sleep)
        return result, received, calls, counter

    def test_default_and_permission_missing_refuse_before_network_decoder_or_counter(self):
        for enabled, authorized, reference in ((False, False, None), (True, False, None), (True, True, None)):
            row = registered()
            row["analysis_config"].update(enabled=enabled, analysis_authorized=authorized, authorization_reference=reference)
            fetch = unittest.mock.Mock(side_effect=AssertionError("network must not run"))
            counter = Counter()
            with self.assertRaises(capture.CaptureBlocked):
                capture.collect_camera(row, fetch=fetch, counter=counter)
            fetch.assert_not_called()
            self.assertEqual(counter.calls, 0)

    def test_missing_fixed_view_background_or_clock_validation_refuses_before_network(self):
        for field, value in (("fixed_view_confirmed", False), ("background_region", None), ("source_clock_validation_reference", None)):
            row = registered()
            row["collector_config"][field] = value
            fetch = unittest.mock.Mock(side_effect=AssertionError("network must not run"))
            with self.assertRaises(capture.CaptureBlocked):
                capture.collect_camera(row, fetch=fetch, counter=Counter())
            fetch.assert_not_called()

    def test_pipeline_source_timestamps_detector_engine_and_numeric_store(self):
        result, received, calls, counter = self.run_capture()
        self.assertEqual(counter.calls, 7)
        self.assertEqual(result["frames"], 7)
        self.assertEqual(result["accepted"], 7)
        self.assertFalse(result["images_retained"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(received[0]["captured_at"], "2026-10-24T00:00:00Z")
        self.assertIn("camera_stability_unknown", received[0]["quality"]["reasons"])
        self.assertTrue(received[-1]["operational_eligible"])
        self.assertIsNotNone(received[-1]["congestion_score"])
        self.assertIsNone(received[-1]["speed_kmh"])

    def test_unvalidated_camera_never_creates_operational_score(self):
        row = registered()
        row["analysis_config"]["independent_validation_reference"] = None
        _, received, _, _ = self.run_capture(row=row)
        self.assertTrue(all(not r["operational_eligible"] for r in received))
        self.assertTrue(all(r["congestion_score"] is None for r in received))

    def test_stale_or_future_playlist_clock_blocks_before_segment_fetch(self):
        for clock in ("2026-10-23T00:00:00Z", "2026-10-25T00:00:00Z"):
            calls = []
            def fetch(url, maximum):
                calls.append(url)
                return playlist().replace(b"2026-10-24T00:00:00Z", clock.encode())
            with self.subTest(clock=clock):
                if clock.startswith("2026-10-23"):
                    result, received, _, counter = self.run_capture(fetch=fetch)
                    self.assertEqual(result["source_retries"], 1)
                    self.assertEqual(received, [])
                    self.assertEqual(counter.calls, 0)
                else:
                    with self.assertRaises(capture.CaptureBlocked):
                        self.run_capture(fetch=fetch)
            self.assertEqual(calls, [URL])

    def test_stale_decoded_frames_are_skipped_without_fabricating_current_time(self):
        result, received, _, counter = self.run_capture(frame_times=[-20])
        self.assertEqual(result["frames"], 0)
        self.assertEqual(received, [])
        self.assertEqual(counter.calls, 0)

    def test_too_close_samples_are_skipped_without_retiming_or_padding_history(self):
        # An overlapping receiver-side segment can begin only 20ms after the
        # previous sample. Preserve the analyzer's 20fps limit without killing
        # a healthy stream, running inference, or inventing a later timestamp.
        result, received, _, counter = self.run_capture(frame_times=[0, 1, 1.02, 1.05, 2, 2.03, 3])
        self.assertEqual(result["sample_interval_skips"], 2)
        self.assertEqual(result["frames"], 5)
        self.assertEqual(counter.calls, 5)
        self.assertEqual([row["captured_at"] for row in received], [
            "2026-10-24T00:00:00Z", "2026-10-24T00:00:01Z", "2026-10-24T00:00:01.050000Z",
            "2026-10-24T00:00:02Z", "2026-10-24T00:00:03Z"])
        self.assertEqual(received[-1]["frame_count"], 4)  # first stability sample does not warm the history

    def test_background_first_frame_flat_texture_translation_and_static_image(self):
        from PIL import Image, ImageChops
        monitor = capture.BackgroundMonitor(POLYGON, maximum_shift=1.)
        self.assertIsNone(monitor.check(self.gray))
        self.assertTrue(monitor.check(self.gray))
        moved = ImageChops.offset(self.gray, 3, 0)
        self.assertFalse(monitor.check(moved))
        self.assertIsNone(monitor.check(Image.new("L", (64, 36), 100)))

    def test_frame_byte_decode_is_local_and_retains_presentation_timestamps(self):
        executable = shutil.which("ffmpeg")
        if not executable:
            self.skipTest("Local ffmpeg not installed")
        fixture = subprocess.run([executable, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10",
                                  "-t", "3", "-c:v", "mpeg2video", "-f", "mpegts", "pipe:1"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=True).stdout
        segment = capture.Segment("https://camera.example/fixture.ts", START, 3.)
        frames = capture.decode_segment(fixture, segment, 1., executable=executable)
        self.assertEqual(len(frames), 3)
        self.assertEqual([round((at - START).total_seconds(), 2) for at, _ in frames], [0., 1., 2.])
        self.assertTrue(all(frame.startswith(b"\xff\xd8") for _, frame in frames))

    def test_decoder_has_no_network_protocols_and_rejects_missing_timestamps(self):
        process = subprocess.CompletedProcess([], 0, stdout=b"\xff\xd8fake\xff\xd9", stderr=b"")
        with patch.object(capture.subprocess, "run", return_value=process) as run:
            with self.assertRaises(capture.CaptureBlocked):
                capture.decode_segment(b"fixture", capture.Segment(URL, START, 3.), 1., executable="/local/ffmpeg")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-protocol_whitelist") + 1], "pipe")
        self.assertNotIn(URL, command)
        self.assertFalse(run.call_args.kwargs.get("shell", False))


if __name__ == "__main__":
    unittest.main()
