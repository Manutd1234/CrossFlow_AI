"""Playlist-only recovery coverage must never promote traffic eligibility."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import cctv_availability as availability, cctv_store as store, cctv_runtime as runtime
from services.cctv_analysis import CameraConfig

START = datetime(2026, 10, 24, tzinfo=timezone.utc)
URL = "https://camera.example/source/live.m3u8?private=never-expose"


def playlist(sequence=1, *, ended=False, clock=None, identity=None):
    header = "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:" + str(sequence) + "\n"
    if clock:
        header += "#EXT-X-PROGRAM-DATE-TIME:" + clock.isoformat() + "\n"
    return (header + "#EXTINF:4,\n" + (identity or f"segment-{sequence}.ts") + "\n" + ("#EXT-X-ENDLIST\n" if ended else "")).encode()


def row(camera_id="camera", *, enabled=False, authorized=False, url=URL):
    config = CameraConfig(camera_id, enabled=enabled, analysis_authorized=authorized,
                          authorization_reference="engineering-fixture" if authorized else None)
    return {"camera_id": camera_id, "name": "Engineering fixture", "source_url": "https://camera.example/public",
            "stream_url": url, "analysis_config": asdict(config)}


class Clock:
    seconds = 0.
    def mono(self): return self.seconds
    def now(self): return START + timedelta(seconds=self.seconds)


class MetadataTests(unittest.TestCase):
    def test_direct_playlist_retains_only_counts_time_sequence_and_private_digest(self):
        value = availability.parse_metadata(playlist(7, clock=START), URL)
        self.assertEqual(value["sequence"], 7)
        self.assertEqual(value["segments"], 1)
        self.assertEqual(value["source_media_at"], START + timedelta(seconds=4))
        self.assertEqual(len(value["segment_identity"]), 64)
        self.assertNotIn("segment-7", str(value))
        self.assertNotIn("never-expose", str(value))

    def test_ended_playlist_is_distinct_from_missing_live_sequence(self):
        self.assertTrue(availability.parse_metadata(b"#EXTM3U\n#EXT-X-ENDLIST\n", URL)["ended"])
        with self.assertRaises(availability.SourceCheckError):
            availability.parse_metadata(b"#EXTM3U\n#EXTINF:4,\nsegment.ts\n", URL)

    def test_master_encrypted_cross_origin_and_malformed_sources_are_not_followed(self):
        cases = [b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nvariant.m3u8\n",
                 playlist().replace(b"#EXTINF:4,", b'#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF:4,'),
                 playlist().replace(b"segment-1.ts", b"https://other.example/media.ts"),
                 playlist().replace(b"#EXTINF:4,", b"#EXTINF:nan,"), b"<html>offline</html>",
                 b"x" * (availability.MAX_PLAYLIST_BYTES + 1)]
        for body in cases:
            with self.subTest(body=body[:30]), self.assertRaises(availability.SourceCheckError):
                availability.parse_metadata(body, URL)
        for url in ("http://camera.example/x", "https://user:password@camera.example/x", "file:///etc/passwd"):
            with self.assertRaises(availability.SourceCheckError):
                availability.parse_metadata(playlist(), url)

    def test_fetch_requests_only_one_playlist_with_identity_encoding_and_byte_bound(self):
        response = Mock()
        response.headers = {}
        response.read1.side_effect = [playlist(), b""]
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch.object(availability, "build_opener", return_value=opener):
            self.assertEqual(availability.fetch_playlist(URL), playlist())
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, URL)
        self.assertEqual(request.get_header("Accept-encoding"), "identity")
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 3.)
        self.assertTrue(all(call.args[0] <= 4096 for call in response.read1.call_args_list))

    def test_nonplaylist_source_urls_are_rejected_before_any_network_request(self):
        for source in ("https://camera.example/media.ts", "https://camera.example/video.mp4?source=live.m3u8",
                       "https://camera.example/live", "https://camera.example/live.m3u8/media.jpg"):
            with self.subTest(source=source), patch.object(availability, "build_opener") as opener:
                with self.assertRaises(availability.SourceCheckError):
                    availability.fetch_playlist(source)
                opener.assert_not_called()
                with self.assertRaises(availability.SourceCheckError):
                    availability.parse_metadata(playlist(), source)

    def test_media_response_headers_prevent_body_reads_but_playlist_mimes_remain_compatible(self):
        for content_type in ("video/mp2t", "video/mp4; codecs=avc1", "image/jpeg", "audio/mpeg"):
            with self.subTest(content_type=content_type):
                response, opener = Mock(), MagicMock()
                response.headers = {"Content-Type": content_type}
                opener.open.return_value.__enter__.return_value = response
                with patch.object(availability, "build_opener", return_value=opener), \
                        self.assertRaises(availability.SourceCheckError) as error:
                    availability.fetch_playlist(URL)
                self.assertEqual(error.exception.code, "media_response_not_playlist")
                response.read1.assert_not_called()
        for content_type in ("application/vnd.apple.mpegurl", "application/x-mpegURL; charset=utf-8",
                             "audio/mpegurl", "audio/x-mpegurl", "application/octet-stream", ""):
            with self.subTest(content_type=content_type):
                response, opener = Mock(), MagicMock()
                response.headers = {"Content-Type": content_type}
                response.read1.side_effect = [playlist(), b""]
                opener.open.return_value.__enter__.return_value = response
                with patch.object(availability, "build_opener", return_value=opener):
                    self.assertEqual(availability.fetch_playlist(URL), playlist())

    def test_read_deadline_and_redirects_are_bounded_without_following_media(self):
        response = Mock()
        response.headers = {}
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch.object(availability, "build_opener", return_value=opener), \
                patch.object(availability.time, "monotonic", side_effect=[0., 7.]):
            with self.assertRaises(TimeoutError):
                availability.fetch_playlist(URL)
        response.read1.assert_not_called()
        with self.assertRaises(availability.SourceCheckError):
            availability._NoRedirect().redirect_request(None, None, 302, None, None, "https://other.example/")


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.body = playlist()
        self.fetch = Mock(side_effect=lambda url: self.body)
        self.monitor = availability.AvailabilityMonitor(fetch=self.fetch, monotonic=self.clock.mono, utcnow=self.clock.now)
        self.rows = [row()]
        self.monitor.reconcile(self.rows)

    def source(self):
        return self.monitor.snapshot(self.rows, now=self.clock.now())["sources"]["camera"]

    def check(self, seconds, sequence=1, **kwargs):
        self.clock.seconds, self.body = seconds, playlist(sequence, **kwargs)
        return self.monitor.check_once()

    def test_first_playlist_needs_advance_and_frozen_playlist_never_renews_progress(self):
        self.check(0)
        self.assertEqual(self.source()["status"], "awaiting_progress")
        self.check(15, 2)
        self.assertEqual(self.source()["status"], "advancing")
        stamp = self.source()["last_progress_at"]
        self.check(30, 2)
        self.assertEqual(self.source()["last_progress_at"], stamp)
        self.clock.seconds = 46
        self.assertEqual(self.source()["status"], "stalled")
        self.assertEqual(self.source()["last_progress_at"], stamp)

    def test_ended_source_recovers_only_after_new_live_progress(self):
        self.check(0, ended=True)
        self.assertEqual(self.source()["status"], "ended")
        self.assertFalse(self.check(15, 2))
        self.check(60, 2)
        self.assertEqual(self.source()["status"], "awaiting_progress")
        self.check(75, 3)
        self.assertEqual(self.source()["status"], "advancing")

    def test_stale_or_future_source_clock_cannot_be_renewed_by_sequence_progress(self):
        self.check(0)
        self.check(15, 2, clock=START - timedelta(seconds=300))
        self.assertEqual(self.source()["status"], "stale")
        self.assertEqual(self.source()["code"], "source_clock_stale")
        self.check(30, 3, clock=START + timedelta(seconds=90))
        self.assertEqual(self.source()["status"], "stale")
        self.assertEqual(self.source()["code"], "source_clock_future")

    def test_source_clock_expires_between_checks_before_recent_sequence_progress(self):
        self.rows[0]["analysis_config"]["maximum_age_seconds"] = 30.
        self.monitor.reconcile(self.rows)
        self.check(0, clock=START)
        self.check(15, 2, clock=START)
        self.clock.seconds = 34
        self.assertEqual(self.source()["status"], "advancing")
        self.clock.seconds = 35
        value = self.source()
        self.assertEqual(value["status"], "stale")
        self.assertEqual(value["code"], "source_clock_stale")
        self.assertEqual(value["last_progress_at"], "2026-10-24T00:00:15Z")
        self.assertEqual(self.fetch.call_count, 2)  # Snapshot never performs I/O or renews evidence.
        self.check(40, 3, clock=START + timedelta(seconds=36))
        self.assertEqual(self.source()["status"], "advancing")

    def test_sequence_reset_and_segment_identity_conflict_withhold_availability(self):
        self.check(0, 10)
        self.check(15, 11)
        self.check(30, 2)
        self.assertEqual(self.source()["status"], "awaiting_progress")
        self.check(45, 3)
        self.assertEqual(self.source()["status"], "advancing")
        self.check(60, 3, identity="different.ts")
        self.assertEqual(self.source()["status"], "invalid")
        self.assertIsNone(self.source()["last_progress_at"])

    def test_permission_and_label_edits_preserve_metadata_but_source_edits_reset_it(self):
        self.check(0)
        self.check(15, 2)
        self.rows = [dict(row(enabled=True, authorized=True), name="Changed label")]
        self.monitor.reconcile(self.rows)
        self.assertEqual(self.source()["status"], "advancing")
        self.rows = [row(url="https://camera.example/new/live.m3u8")]
        self.monitor.reconcile(self.rows)
        self.assertEqual(self.source()["status"], "unchecked")

    def test_failed_checks_are_safe_and_expire_without_returning_urls(self):
        self.fetch.side_effect = URLError("private token never-expose")
        self.monitor.check_once()
        value = self.source()
        self.assertEqual(value["status"], "unreachable")
        self.assertEqual(value["consecutive_failures"], 1)
        self.assertNotIn("never-expose", json.dumps(value))
        self.clock.seconds = 91
        self.assertEqual(self.source()["status"], "check_expired")

    def test_http_response_is_not_mistaken_for_a_valid_stream(self):
        self.body = b"<html>Source unavailable</html>"
        self.monitor.check_once()
        self.assertEqual(self.source()["status"], "invalid")
        self.assertTrue(self.source()["reachable"])
        self.assertFalse(self.source()["playlist_validated"])

    def test_all_28_sources_disabled_included_are_checked_fairly_without_media_requests(self):
        self.rows = [row(str(index), enabled=index < 6, authorized=index < 6,
                         url=f"https://camera.example/{index}/live.m3u8") for index in range(28)]
        self.monitor.reconcile(self.rows)
        for _ in range(28):
            self.assertTrue(self.monitor.check_once())
        self.assertEqual(self.fetch.call_count, 28)
        self.assertEqual({call.args[0] for call in self.fetch.call_args_list}, {value["stream_url"] for value in self.rows})
        self.assertFalse(self.monitor.check_once())
        self.clock.seconds, self.body = 15, playlist(2)
        for _ in range(28):
            self.monitor.check_once()
        snapshot = self.monitor.snapshot(self.rows, now=self.clock.now())
        self.assertEqual(snapshot["summary"], {"advancing": 28})
        self.assertFalse(snapshot["policy"]["grants_inference_or_routing_eligibility"])
        self.assertNotIn(".m3u8", json.dumps(snapshot))

    def test_availability_alone_cannot_create_a_database_authorize_or_route_an_unreviewed_view(self):
        self.check(0)
        self.check(15, 2)
        with tempfile.TemporaryDirectory() as temporary:
            path, database = Path(temporary) / "registry.json", Path(temporary) / "numeric.sqlite"
            path.write_text(json.dumps({"schema_version": 1, "cameras": self.rows}))
            with patch.dict("os.environ", {"CROSSFLOW_CCTV_REGISTRY": str(path), "CROSSFLOW_CCTV_DB": str(database)}), \
                    patch.object(store.cctv_availability, "MONITOR", self.monitor):
                status = store.camera_status(now=self.clock.now())
                self.assertEqual(status["cameras"][0]["source_availability"]["status"], "advancing")
                self.assertEqual(status["cameras"][0]["status"], "permission_pending")
                self.assertIsNone(status["cameras"][0]["congestion_score"])
                self.assertEqual(store.road_segment_scores(now=self.clock.now()), [])
                self.assertFalse(database.exists())

    def test_replaced_inflight_source_cannot_publish_old_progress(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(url):
            entered.set()
            release.wait(2)
            return playlist()
        self.monitor._fetch = fetch
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.monitor.check_once)
            self.assertTrue(entered.wait(1))
            self.rows = [row(url="https://camera.example/replacement.m3u8")]
            self.monitor.reconcile(self.rows)
            release.set()
            self.assertTrue(future.result(2))
        self.assertEqual(self.source()["status"], "unchecked")

    def test_only_two_actual_workers_and_no_duplicate_start(self):
        entered, release, lock = threading.Event(), threading.Event(), threading.Lock()
        counts = [0, 0]
        def fetch(url):
            with lock:
                counts[0] += 1
                counts[1] = max(counts)
                if counts[0] == 2:
                    entered.set()
            release.wait(2)
            with lock:
                counts[0] -= 1
            return playlist()
        monitor = availability.AvailabilityMonitor(fetch=fetch)
        rows = [row(str(i)) for i in range(28)]
        monitor.reconcile(rows)
        try:
            monitor.start()
            monitor.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(monitor.check_once())
            self.assertEqual(len(monitor._threads), 2)
            self.assertEqual(monitor.snapshot(rows)["policy"]["running_check_workers"], 2)
        finally:
            release.set()
            monitor.stop()
        self.assertLessEqual(counts[1], 2)
        self.assertEqual(monitor._threads, [])

    def test_restart_waits_for_timed_out_workers_then_prunes_finished_references(self):
        entered, release, lock = threading.Event(), threading.Event(), threading.Lock()
        counts = [0, 0]
        def fetch(url):
            with lock:
                counts[0] += 1
                counts[1] = max(counts)
                if counts[0] == 2:
                    entered.set()
            release.wait(3)
            with lock:
                counts[0] -= 1
            return playlist()
        monitor = availability.AvailabilityMonitor(fetch=fetch)
        monitor.reconcile([row(str(i)) for i in range(28)])
        try:
            monitor.start()
            self.assertTrue(entered.wait(1))
            previous = list(monitor._threads)
            # A bounded shutdown can return while both requests are still in flight.
            with patch.object(previous[0], "join"), patch.object(previous[1], "join"):
                monitor.stop()
            monitor.start()
            self.assertEqual(monitor._threads, previous)
            self.assertTrue(monitor._stop.is_set())
            release.set()
            for worker in previous:
                worker.join(1)
                self.assertFalse(worker.is_alive())
            release.clear()
            entered.clear()
            monitor.start()
            self.assertTrue(entered.wait(1))
            self.assertEqual(len(monitor._threads), 2)
            self.assertTrue(all(worker not in previous and worker.is_alive() for worker in monitor._threads))
            self.assertEqual(counts[1], 2)
        finally:
            release.set()
            monitor.stop()

    def test_runtime_reconciles_disabled_sources_without_scheduling_their_inference(self):
        supervisor = runtime.CCTVRuntime()
        rows = [row("enabled", enabled=True, authorized=True), row("disabled")]
        with patch.object(runtime.cctv_store, "load_registry", return_value=rows), \
                patch.object(runtime.cctv_availability, "MONITOR", self.monitor):
            supervisor._refresh_registry()
            self.assertEqual(supervisor._schedule.camera_ids, ("enabled",))
            self.assertEqual(set(self.monitor._rows), {"enabled", "disabled"})
            self.assertFalse(rows[1]["analysis_config"]["enabled"])

    def test_metadata_monitoring_can_start_without_the_optional_vision_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict("os.environ", {"VERCEL": "", "CROSSFLOW_CCTV_ENABLED": "1", "CROSSFLOW_CCTV_PYTHON": str(Path(temporary) / "not-installed")}), \
                    patch.object(runtime.cctv_store, "load_registry", return_value=self.rows), \
                    patch.object(runtime.cctv_availability, "MONITOR", self.monitor), \
                    patch.object(self.monitor, "start") as start, \
                    patch.object(runtime.threading, "Thread") as thread:
                supervisor = runtime.CCTVRuntime()
                supervisor.start()
                start.assert_called_once()
                self.assertEqual(thread.call_count, 1)  # Registry watcher, zero detector slots.
                self.assertEqual(supervisor._processes, {})


if __name__ == "__main__":
    unittest.main()
