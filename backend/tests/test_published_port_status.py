"""Published port references must never invent operational measurements."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

from services import clock, ferry_schedule


class PublishedPortStatusTests(unittest.TestCase):
    def test_unknown_operations_remain_null_while_terminal_references_are_retained(self):
        now = datetime(2026, 10, 23, 8, 0, tzinfo=clock.BATAM_TZ)
        ports = ferry_schedule.get_published_port_status(now)
        self.assertEqual(len(ports), len(ferry_schedule.PORT_METADATA))
        for port in ports:
            with self.subTest(port=port["port_name"]):
                self.assertTrue(port["terminal_code"])
                self.assertTrue(port["official_reference_url"].startswith("https://"))
                for field in (
                    "passenger_queue_mins", "customs_processing_mins", "freight_clearance_mins",
                    "active_berths", "total_berths", "status",
                ):
                    self.assertIsNone(port[field])
                self.assertFalse(port["observed"])
                self.assertEqual(port["data_source"], "published_schedule")
                self.assertNotIn("estimate_basis", port)

    def test_next_departure_is_derived_from_the_current_published_schedule(self):
        now = datetime(2026, 10, 23, 8, 0, tzinfo=clock.BATAM_TZ)
        for port in ferry_schedule.get_published_port_status(now):
            sailings = ferry_schedule.generate_sailings(now, horizon_hours=2, ports=[port["port_name"]])
            self.assertEqual(port["next_sailing_in_mins"], sailings[0]["minutes_until_departure"] if sailings else None)
            self.assertEqual(port["next_operator"], sailings[0]["operator"] if sailings else None)

    def test_no_departure_does_not_become_an_invented_vessel_or_live_status(self):
        with patch.object(ferry_schedule, "generate_sailings", return_value=[]):
            ports = ferry_schedule.get_published_port_status(clock.now())
        self.assertTrue(all(port["next_sailing_in_mins"] is None for port in ports))
        self.assertTrue(all(port["next_vessel"] is None and port["status"] is None for port in ports))

    def test_existing_engineering_estimate_function_is_not_reused_for_public_actuals(self):
        with patch.object(ferry_schedule, "get_port_intelligence", side_effect=AssertionError("scenario must not be used")):
            ports = ferry_schedule.get_published_port_status(clock.now() + timedelta(hours=1))
        self.assertEqual(len(ports), 4)


if __name__ == "__main__":
    unittest.main()
