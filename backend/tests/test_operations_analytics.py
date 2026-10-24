"""Dashboard analytics must use current measurements or explicit absence."""

from __future__ import annotations

import os
import sys
import unittest
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import Response

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

import main as api_main  # noqa: E402
from services import clock, operations_analytics as analytics  # noqa: E402
from services.congestion_math import speed_ratio_congestion_score  # noqa: E402


class CurrentOperationsAnalyticsTests(unittest.TestCase):
    now = datetime(2026, 10, 23, 9, 30, tzinfo=timezone.utc)

    def setUp(self):
        self.camera_status = patch.object(analytics.cctv_store, "camera_status", return_value={
            "status": "awaiting_feeds", "summary": {"configured": 0}, "cameras": [],
        })
        self.camera_snapshot = patch.object(analytics.cctv_routing, "snapshot", return_value=SimpleNamespace(edge_scores={}))
        self.camera_status.start()
        self.camera_snapshot.start()
        self.addCleanup(self.camera_status.stop)
        self.addCleanup(self.camera_snapshot.stop)

    def segment(self, speed=25.0, age=60, **overrides):
        return {
            "current_speed_kmh": speed, "free_flow_speed_kmh": 50.0,
            "confidence": 0.9,
            "snapshot_generated_at": clock.iso(self.now - timedelta(seconds=age)),
            **overrides,
        }

    def summary(self, segments, now=None):
        with patch.object(analytics.live_traffic, "_tomtom_segments", return_value=segments), \
                patch.object(analytics.ferry_schedule, "generate_sailings", return_value=[{}, {}]):
            return analytics.get_operations_analytics(now or self.now)

    def test_measured_points_drive_current_average_and_bottlenecks(self):
        summary = self.summary({
            "corridor-1": self.segment(speed=50.0),
            "corridor-2": self.segment(speed=0.0),
        })
        expected = round((speed_ratio_congestion_score(1.0) + speed_ratio_congestion_score(0.0)) / 2, 1)
        self.assertEqual(summary["average_congestion_index"], expected)
        self.assertEqual(summary["active_bottlenecks"], 1)
        self.assertEqual(summary["bottleneck_corridors"][0]["id"], "corridor-2")
        metadata = summary["analytics_metadata"]
        self.assertEqual(metadata["observed_corridor_count"], 2)
        self.assertEqual(metadata["expected_corridor_count"], 5)
        self.assertFalse(metadata["complete_corridor_coverage"])
        self.assertEqual(metadata["snapshot_age_seconds"], 60)
        self.assertTrue(metadata["traffic_observed"])

    def test_unavailable_measurements_do_not_become_simulation_or_zero(self):
        summary = self.summary({})
        self.assertIsNone(summary["average_congestion_index"])
        self.assertIsNone(summary["active_bottlenecks"])
        self.assertEqual(summary["overall_network_status"], "UNAVAILABLE")
        self.assertEqual(summary["alerts"], [])
        self.assertEqual(summary["api_source"], "unavailable")
        self.assertEqual(summary["analytics_metadata"]["observed_corridor_count"], 0)
        self.assertIsNone(summary["analytics_metadata"]["as_of"])

    def test_stale_future_naive_and_invalid_measurements_are_excluded(self):
        invalid = [
            self.segment(age=181), self.segment(age=-31),
            self.segment(snapshot_generated_at="2026-10-23T09:30:00"),
            self.segment(current_speed_kmh=float("nan")),
            self.segment(current_speed_kmh=251), self.segment(free_flow_speed_kmh=0),
            self.segment(current_speed_kmh=80), self.segment(confidence=1.1),
        ]
        for segment in invalid:
            with self.subTest(segment=segment):
                result = self.summary({"corridor-1": segment})
                self.assertIsNone(result["average_congestion_index"])
                self.assertEqual(result["analytics_metadata"]["observed_corridor_count"], 0)

    def test_alert_timestamps_are_actual_sample_times_not_fabricated_ages(self):
        sample = self.segment(speed=0, age=75)
        summary = self.summary({"corridor-1": sample})
        self.assertEqual(len(summary["alerts"]), 1)
        self.assertEqual(summary["alerts"][0]["timestamp"], sample["snapshot_generated_at"])
        self.assertTrue(summary["alerts"][0]["observed"])
        self.assertNotIn("Recommend dispatch", summary["alerts"][0]["message"])

    def test_unsupported_fleet_emissions_and_trends_remain_unavailable(self):
        summary = self.summary({"corridor-1": self.segment()})
        self.assertIsNone(summary["total_co2_reduced_today_kg"])
        self.assertIsNone(summary["live_co2_rate_kg_hr"])
        self.assertEqual(summary["co2_by_vehicle_type"], {})
        self.assertEqual(summary["hourly_co2_distribution"], [])
        self.assertNotIn("projected_full_day_co2_kg", summary)
        self.assertEqual(summary["operations_methodology"]["assumptions"], {})

    def camera(self, score=30, age=5, **overrides):
        return {
            "status": "estimated", "congestion_score": score,
            "maximum_age_seconds": 15,
            "observed_at": clock.iso(self.now - timedelta(seconds=age)),
            "vehicle_count": 3, "validation_status": "estimated", "operational_eligible": False,
            "stream_url": "private-stream-location", "camera_id": "private-camera-id",
            **overrides,
        }

    def test_camera_estimates_and_graph_coverage_are_separate_from_measured_speed(self):
        monitor = {"status": "active", "summary": {"configured": 2}, "cameras": [
            self.camera(score=20),
            self.camera(score=80, age=10, vehicle_count=5, status="live", validation_status="validated", operational_eligible=True),
        ]}
        with patch.object(analytics.cctv_store, "camera_status", return_value=monitor), \
                patch.object(analytics.cctv_routing, "snapshot", return_value=SimpleNamespace(edge_scores={(1, 2, 0): 80})), \
                patch.object(analytics.router, "NODES", {1: (1., 104.), 2: (1., 104.1)}), \
                patch.object(analytics.router, "ROAD_ADJ", {1: [object()], 2: [object()]}), \
                patch.object(analytics.router, "GRAPH_META", {"way_count": 1}):
            result = self.summary({"corridor-1": self.segment(speed=50)})
        self.assertEqual(result["average_congestion_index"], round(speed_ratio_congestion_score(1), 1))
        camera = result["camera_traffic_summary"]
        self.assertEqual(camera["mean_congestion_index"], 50)
        self.assertEqual(camera["vehicle_detections"], 8)
        self.assertEqual(camera["fresh_cameras"], 2)
        self.assertEqual(camera["validated_cameras"], 1)
        self.assertEqual(camera["estimated_cameras"], 1)
        self.assertFalse(camera["independently_validated"])
        self.assertFalse(camera["measured_road_speed"])
        self.assertEqual(camera["oldest_observed_at"], clock.iso(self.now - timedelta(seconds=10)))
        self.assertEqual(camera["valid_until"], clock.iso(self.now + timedelta(seconds=5)))
        coverage = result["road_network_coverage"]
        self.assertEqual(coverage["mapped_nodes"], 2)
        self.assertEqual(coverage["mapped_source_ways"], 1)
        self.assertEqual(coverage["directed_edges"], 2)
        self.assertEqual(coverage["live_camera_edge_count"], 1)
        self.assertEqual(coverage["live_camera_edge_coverage_percent"], 50)
        self.assertFalse(coverage["complete_live_coverage"])
        self.assertNotIn("private-stream-location", json.dumps(result))
        self.assertNotIn("private-camera-id", json.dumps(result))

    def test_camera_only_data_does_not_invent_speed_based_network_coverage(self):
        with patch.object(analytics.cctv_store, "camera_status", return_value={
            "status": "active", "summary": {"configured": 1}, "cameras": [self.camera()],
        }):
            result = self.summary({})
        self.assertEqual(result["camera_traffic_summary"]["mean_congestion_index"], 30)
        self.assertIsNone(result["average_congestion_index"])
        self.assertEqual(result["overall_network_status"], "UNAVAILABLE")
        self.assertFalse(result["analytics_metadata"]["traffic_observed"])

    def test_stale_future_invalid_and_unavailable_camera_views_are_excluded(self):
        for camera in (
            self.camera(age=16), self.camera(age=-1), self.camera(status="stale"),
            self.camera(score=float("nan")), self.camera(score=True),
            self.camera(observed_at="2026-10-23T09:30:00"), self.camera(maximum_age_seconds=200),
        ):
            with self.subTest(camera=camera), patch.object(analytics.cctv_store, "camera_status", return_value={
                "status": "degraded", "summary": {"configured": 1}, "cameras": [camera],
            }):
                result = self.summary({})["camera_traffic_summary"]
                self.assertEqual(result["fresh_cameras"], 0)
                self.assertIsNone(result["mean_congestion_index"])
                self.assertIsNone(result["vehicle_detections"])
                self.assertIsNone(result["valid_until"])

    def test_camera_diagnostics_failure_does_not_break_measured_traffic(self):
        with patch.object(analytics.cctv_store, "camera_status", side_effect=RuntimeError("private location")), \
                patch.object(analytics.cctv_routing, "snapshot", side_effect=RuntimeError("private location")):
            result = self.summary({"corridor-1": self.segment()})
        self.assertIsNotNone(result["average_congestion_index"])
        self.assertEqual(result["camera_traffic_summary"]["status"], "unavailable")
        self.assertIsNone(result["road_network_coverage"]["live_camera_edge_count"])
        self.assertIsNone(result["road_network_coverage"]["live_camera_edge_coverage_percent"])
        self.assertNotIn("private location", json.dumps(result))

    def test_timetable_count_recomputes_at_request_time_and_failure_is_not_zero(self):
        later = self.now + timedelta(hours=1)
        with patch.object(analytics.live_traffic, "_tomtom_segments", return_value={}), \
                patch.object(analytics.ferry_schedule, "generate_sailings", side_effect=[[{}, {}], [{}], RuntimeError("private URL")]) as timetable:
            self.assertEqual(analytics.get_operations_analytics(self.now)["scheduled_ferry_departures_next_12h"], 2)
            self.assertEqual(analytics.get_operations_analytics(later)["scheduled_ferry_departures_next_12h"], 1)
            self.assertIsNone(analytics.get_operations_analytics(later)["scheduled_ferry_departures_next_12h"])
        self.assertEqual(timetable.call_args_list[0].args, (self.now,))
        self.assertEqual(timetable.call_args_list[1].args, (later,))

    def test_api_is_uncacheable_and_does_not_claim_live_data_on_provider_failure(self):
        response = Response()
        with patch.object(analytics.live_traffic, "_tomtom_segments", side_effect=RuntimeError("private URL")), \
                patch.object(analytics.ferry_schedule, "generate_sailings", return_value=[]), \
                clock.frozen(self.now):
            payload = api_main.get_ops(response)
        self.assertEqual(payload["data_source"], "unavailable")
        self.assertIsNone(payload["average_congestion_index"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(payload["generated_at"], clock.iso(self.now))
        self.assertNotIn("private URL", str(payload))

    def test_history_api_defaults_to_observed_snapshot_not_synthetic_seed(self):
        response = Response()
        expected = {"history_metadata": {"selection_scope": "observed_only", "sample_count": 0}}
        with patch.object(api_main, "get_observed_history", return_value=expected) as observed, \
                patch.object(api_main.historical_store, "get_hourly_profile") as legacy, \
                clock.frozen(self.now):
            payload = api_main.api_historical_congestion("corridor-1", 7, response=response)
        self.assertEqual(payload["data_source"], "observed_history")
        self.assertEqual(payload["history_metadata"]["sample_count"], 0)
        self.assertEqual(observed.call_args.args, ("corridor-1", 7, self.now))
        legacy.assert_not_called()
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_history_storage_failure_is_sanitized_and_uncacheable(self):
        with patch.object(api_main, "get_observed_history", side_effect=api_main.AnalyticsHistoryUnavailable("private URL")):
            with self.assertRaises(api_main.HTTPException) as caught:
                api_main.api_historical_congestion("corridor-1", 7)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn("private URL", caught.exception.detail)
        self.assertIn("no-store", caught.exception.headers["Cache-Control"])

    def test_public_port_api_has_reference_data_without_invented_operating_values(self):
        response = Response()
        with clock.frozen(self.now):
            payload = api_main.get_port_intelligence(response)
        self.assertEqual(payload["data_source"], "published_schedule")
        self.assertGreater(len(payload["ports"]), 0)
        for port in payload["ports"]:
            for field in ("passenger_queue_mins", "customs_processing_mins", "freight_clearance_mins", "active_berths", "total_berths", "status"):
                self.assertIsNone(port[field])
            self.assertFalse(port["observed"])
            self.assertTrue(port["official_reference_url"])
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_source_refresh_uses_the_same_reference_only_port_view(self):
        expected = [{"port_name": "Batam Centre", "status": None}]
        stamp = clock.iso(self.now)
        with patch.object(api_main.ferry_refresh, "refresh_official_sources", return_value={"finished_at": stamp, "schedule_verified": False}), \
                patch.object(api_main.ferry_schedule, "record_refresh_result"), \
                patch.object(api_main.ferry_schedule, "timetable_metadata", return_value={"last_verified_at": stamp}), \
                patch.object(api_main.ferry_schedule, "generate_sailings", return_value=[]), \
                patch.object(api_main.ferry_schedule, "get_published_port_status", return_value=expected) as published, \
                patch.object(api_main.ferry_schedule, "get_port_intelligence") as scenario, \
                clock.frozen(self.now):
            payload = api_main.refresh_ferry_sources(Response())
        self.assertEqual(payload["ports"], expected)
        published.assert_called_once_with(self.now)
        scenario.assert_not_called()


if __name__ == "__main__":
    unittest.main()
