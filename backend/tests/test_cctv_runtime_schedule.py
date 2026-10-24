"""Bounded sampling must visit every enabled camera without weakening evidence TTLs."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import cctv_runtime as runtime


def row(camera_id, **settings):
    return {"camera_id": camera_id, "analysis_config": {
        "enabled": True, "analysis_authorized": True, "minimum_window_seconds": 10.,
        "stationary_seconds": 15., "congestion_persistence_seconds": 15., "minimum_samples": 5,
        **settings,
    }, "collector_config": {"frame_interval_seconds": 2.}, "stream_url": "private-not-in-diagnostics"}


class FakeProcess:
    def __init__(self, code=0):
        self.code = code
        self.stdout = io.BytesIO()
        self.terminated = self.killed = False

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = -15

    def kill(self):
        self.killed = True
        self.code = -9


class CameraQueueTests(unittest.TestCase):
    def test_all_44_cameras_get_a_turn_before_any_successful_camera_repeats(self):
        schedule = runtime.CameraSamplingSchedule()
        schedule.reconcile(str(i) for i in range(44))
        visited = []
        for cycle in range(22):
            first, second = schedule.acquire(cycle * 60), schedule.acquire(cycle * 60)
            self.assertIsNone(schedule.acquire(cycle * 60))
            visited.extend((first, second))
            schedule.complete(first, cycle * 60 + 60)
            schedule.complete(second, cycle * 60 + 60)
        self.assertEqual(visited, [str(i) for i in range(44)])
        self.assertEqual(schedule.acquire(1320), "0")

    def test_failed_source_releases_capacity_and_cannot_block_healthy_cameras(self):
        schedule = runtime.CameraSamplingSchedule()
        schedule.reconcile(("failed", "healthy-a", "healthy-b"))
        self.assertEqual(schedule.acquire(0), "failed")
        schedule.complete("failed", 0, failed=True)
        self.assertEqual(schedule.retry_after["failed"], 4.)
        self.assertEqual(schedule.acquire(0), "healthy-a")
        schedule.complete("healthy-a", 0)
        self.assertEqual(schedule.acquire(0), "healthy-b")
        schedule.complete("healthy-b", 0)
        self.assertEqual(schedule.acquire(0), "healthy-a")
        schedule.complete("healthy-a", 0)
        self.assertEqual(schedule.acquire(4), "healthy-b")
        schedule.complete("healthy-b", 4)
        self.assertEqual(schedule.acquire(4), "failed")
        for attempt in range(12):
            schedule.complete("failed", 10 + attempt, failed=True)
        self.assertEqual(schedule.retry_after["failed"] - 21, 60.)

    def test_registry_changes_preserve_fairness_and_remove_revoked_sources(self):
        schedule = runtime.CameraSamplingSchedule()
        schedule.reconcile(("a", "b", "c"))
        self.assertEqual(schedule.acquire(0), "a")
        schedule.reconcile(("b", "c", "d"))
        schedule.complete("a", 0, failed=True)
        self.assertNotIn("a", schedule.pending)
        self.assertNotIn("a", schedule.retry_after)
        self.assertEqual(schedule.acquire(0), "b")
        self.assertEqual(schedule.acquire(0), "c")
        schedule.complete("b", 0)
        self.assertEqual(schedule.acquire(0), "d")


class RuntimeSamplingTests(unittest.TestCase):
    def test_two_actual_slot_threads_visit_44_sources_without_parallel_overflow(self):
        supervisor = runtime.CCTVRuntime()
        supervisor._rows = {str(i): row(str(i)) for i in range(44)}
        supervisor._schedule.reconcile(supervisor._rows)
        visited, live, maximum = set(), [0], [0]
        lock = threading.Lock()
        completed = threading.Event()

        def collect(_python, camera_id, duration, _row):
            self.assertEqual(duration, 60)
            with lock:
                live[0] += 1
                maximum[0] = max(maximum[0], live[0])
                visited.add(camera_id)
            completed.wait(.002)
            with lock:
                live[0] -= 1
                if len(visited) == 44:
                    supervisor._stop.set()
                    completed.set()
            return "completed"

        with patch.object(supervisor, "_collect_once", side_effect=collect):
            threads = [threading.Thread(target=supervisor._sampling_slot, args=(Path("/fixture/python"),)) for _ in range(2)]
            for thread in threads:
                thread.start()
            self.assertTrue(completed.wait(3))
            supervisor._stop.set()
            for thread in threads:
                thread.join(1)
                self.assertFalse(thread.is_alive())
        self.assertEqual(len(visited), 44)
        self.assertLessEqual(maximum[0], 2)

    def test_continuous_capacity_preserves_hour_long_detector_sessions(self):
        supervisor = runtime.CCTVRuntime()
        self.assertEqual(supervisor._sampling_duration(row("a"), rotating=False), 3600)
        child = FakeProcess()
        with patch.object(runtime.subprocess, "Popen", return_value=child) as spawn:
            self.assertEqual(supervisor._collect_once(Path("/fixture/python"), "a"), "completed")
        self.assertEqual(spawn.call_args.args[0][-2:], ["--max-seconds", "3600"])
        self.assertTrue(child.stdout.closed)
        self.assertEqual(supervisor._processes, {})

    def test_rotation_respects_temporal_requirements_without_extending_freshness(self):
        supervisor = runtime.CCTVRuntime()
        supervisor._slice_seconds = 30
        source = row("a", minimum_window_seconds=40., stationary_seconds=50., congestion_persistence_seconds=20.)
        self.assertEqual(supervisor._sampling_duration(source, rotating=True), 80)
        self.assertEqual(source["analysis_config"]["minimum_window_seconds"], 40.)
        supervisor._rows = {str(i): row(str(i)) for i in range(44)}
        supervisor._schedule.reconcile(supervisor._rows)
        diagnostics = supervisor.diagnostics()["scheduler"]
        self.assertEqual(diagnostics["eligible_cameras"], 44)
        self.assertEqual(diagnostics["max_concurrent_collectors"], 2)
        self.assertEqual(diagnostics["nominal_cycle_seconds"], 22 * 40)
        self.assertTrue(diagnostics["freshness_limits_unchanged"])
        self.assertNotIn("private", json.dumps(diagnostics))

    def test_revoked_or_changed_registry_cancels_an_existing_child(self):
        supervisor = runtime.CCTVRuntime()
        child = FakeProcess(code=None)
        with patch.object(runtime.subprocess, "Popen", return_value=child):
            result = supervisor._collect_once(Path("/fixture/python"), "a", 60, row("a"))
        self.assertEqual(result, "cancelled")
        self.assertTrue(child.terminated)
        self.assertEqual(supervisor._processes, {})

    def test_audit_labels_do_not_restart_a_healthy_temporal_session(self):
        supervisor = runtime.CCTVRuntime()
        original = row("a")
        supervisor._rows = {"a": {**original, "name": "Updated public label", "stream_access_status": "playback_reviewed"}}
        child = FakeProcess()
        with patch.object(runtime.subprocess, "Popen", return_value=child), \
                patch.object(child, "poll", side_effect=(None, 0, 0, 0)), \
                patch.object(runtime.time, "monotonic", side_effect=(0., 1.)), \
                patch.object(supervisor._stop, "wait", return_value=False):
            result = supervisor._collect_once(Path("/fixture/python"), "a", 60, original)
        self.assertEqual(result, "completed")
        self.assertFalse(child.terminated)

    def test_source_watchdog_frees_a_stuck_slot_and_reports_a_safe_failure(self):
        supervisor = runtime.CCTVRuntime()
        source = row("a")
        supervisor._rows = {"a": source}
        child = FakeProcess(code=None)
        with patch.object(runtime.subprocess, "Popen", return_value=child), \
                patch.object(runtime.time, "monotonic", side_effect=(0., 121.)), \
                self.assertLogs("services.cctv_runtime", level="WARNING"):
            result = supervisor._collect_once(Path("/fixture/python"), "a", 60, source)
        self.assertEqual(result, "failed")
        self.assertTrue(child.terminated)
        self.assertEqual(supervisor.diagnostics()["collectors"][0]["last_failure"]["code"], "collector_deadline")

    def test_new_sources_can_rotate_a_previously_continuous_session(self):
        supervisor = runtime.CCTVRuntime()
        source = row("a")
        supervisor._rows = {"a": source, "b": row("b"), "c": row("c")}
        child = FakeProcess(code=None)
        with patch.object(runtime.subprocess, "Popen", return_value=child), \
                patch.object(runtime.time, "monotonic", side_effect=(0., 121.)):
            result = supervisor._collect_once(Path("/fixture/python"), "a", 3600, source)
        self.assertEqual(result, "rotated")
        self.assertTrue(child.terminated)
        self.assertEqual(supervisor.diagnostics()["collectors"][0]["last_event"]["code"], "sampling_slice_complete")

    def test_unresponsive_child_is_killed_and_reaped_before_its_slot_is_released(self):
        class StubbornProcess(FakeProcess):
            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                if self.code is None:
                    raise runtime.subprocess.TimeoutExpired("fixture collector", timeout)
                return self.code

        supervisor = runtime.CCTVRuntime()
        child = StubbornProcess(code=None)
        with patch.object(runtime.subprocess, "Popen", return_value=child) as spawn, \
                patch.object(runtime.time, "monotonic", side_effect=(0., 121.)), \
                self.assertLogs("services.cctv_runtime", level="WARNING"):
            result = supervisor._collect_once(Path("/fixture/python"), "a", 60)
        self.assertEqual(result, "failed")
        self.assertTrue(child.killed)
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(supervisor._processes, {})

    def test_registry_failure_fails_closed_and_recovers_without_a_server_restart(self):
        supervisor = runtime.CCTVRuntime()
        with patch.object(runtime.cctv_store, "load_registry", return_value=[row("a"), row("disabled", enabled=False)]):
            supervisor._refresh_registry()
        self.assertEqual(supervisor._schedule.camera_ids, ("a",))
        with patch.object(runtime.cctv_store, "load_registry", side_effect=runtime.cctv_store.CCTVStoreUnavailable("private location")):
            supervisor._refresh_registry()
        self.assertEqual(supervisor._schedule.camera_ids, ())
        self.assertFalse(supervisor.diagnostics()["scheduler"]["registry_available"])
        with patch.object(runtime.cctv_store, "load_registry", return_value=[row("b")]):
            supervisor._refresh_registry()
        self.assertEqual(supervisor._schedule.camera_ids, ("b",))
        self.assertNotIn("private", json.dumps(supervisor.diagnostics()))

    def test_corrected_collection_configuration_resets_old_source_backoff(self):
        supervisor = runtime.CCTVRuntime()
        original = row("a")
        with patch.object(runtime.cctv_store, "load_registry", return_value=[original]):
            supervisor._refresh_registry()
        supervisor._schedule.acquire(0)
        supervisor._schedule.complete("a", 0, failed=True)
        with patch.object(runtime.cctv_store, "load_registry", return_value=[{**original, "name": "Only a label"}]):
            supervisor._refresh_registry()
        self.assertEqual(supervisor._schedule.retry_after["a"], 4.)
        with patch.object(runtime.cctv_store, "load_registry", return_value=[{**original, "collector_config": {"frame_interval_seconds": 1.}}]):
            supervisor._refresh_registry()
        self.assertNotIn("a", supervisor._schedule.retry_after)
        self.assertEqual(supervisor._schedule.acquire(0), "a")

    def test_capacity_setting_is_bounded_and_does_not_drop_extra_camera_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            python = Path(directory) / "python"
            python.touch()
            with patch.dict(runtime.os.environ, {"VERCEL": "", "CROSSFLOW_CCTV_ENABLED": "1", "CROSSFLOW_CCTV_PYTHON": str(python), "CROSSFLOW_CCTV_MAX_CONCURRENT": "6"}), \
                    patch.object(runtime.os, "cpu_count", return_value=18), \
                    patch.object(runtime.cctv_store, "load_registry", return_value=[row(str(i)) for i in range(44)]), \
                    patch.object(runtime.cctv_availability.MONITOR, "start"), \
                    patch.object(runtime.threading, "Thread") as thread:
                supervisor = runtime.CCTVRuntime()
                supervisor.start()
        self.assertEqual(supervisor._max_concurrent, 6)
        self.assertEqual(supervisor._schedule.capacity, 6)
        self.assertEqual(len(supervisor._schedule.camera_ids), 44)
        self.assertEqual(thread.call_count, 7)  # Six detector slots plus registry monitoring.


if __name__ == "__main__":
    unittest.main()
