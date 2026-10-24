"""A reviewed daily row must not advance unrelated timetable verification."""

from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import sys
import unittest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

from services import ferry_schedule, ferry_refresh  # noqa: E402


class ReviewedSindoSailingTests(unittest.TestCase):
    def test_reviewed_2030_is_daily_in_singapore_time_with_weekend_row_separate(self):
        route = next(route for route in ferry_schedule.FERRY_ROUTES if route.code == "sindo-hf-skp")
        self.assertEqual(route.departure_timezone, "Asia/Singapore")
        for day in (date(2026, 10, 23), date(2026, 10, 24), date(2026, 10, 25)):
            with self.subTest(day=day):
                slots = [value for _slot, value in ferry_schedule._slots(route, day)]
                late = [value for value in slots if value.strftime("%H:%M") == "20:30"]
                self.assertEqual(len(late), 1)
                self.assertEqual(late[0].utcoffset(), timedelta(hours=8))
                self.assertEqual(any(value.strftime("%H:%M") == "16:00" for value in slots), day.weekday() >= 5)

    def test_only_reviewed_operator_gets_the_new_verification_time(self):
        routes = {route.code: route for route in ferry_schedule.FERRY_ROUTES}
        for route in routes.values():
            expected = "2026-10-23T17:49:53+08:00" if route.operator == "Sindo Ferry" else "2026-08-13T00:30:50+07:00"
            self.assertEqual(route.source_last_verified_at, expected)
        self.assertEqual(ferry_schedule._TIMETABLE["last_verified_at"], "2026-08-13T00:30:50+07:00")
        metadata = ferry_schedule.committed_timetable_metadata()
        sindo = next(source for source in metadata["sources"] if source["operator"] == "Sindo Ferry")
        self.assertEqual(sindo["snapshot_verified_at"], "2026-10-23T17:49:53+08:00")
        self.assertEqual(sindo["last_verified_at"], "2026-10-23T17:49:53+08:00")

    def test_reviewed_reverse_card_uses_current_daily_slots_in_batam_time(self):
        route = next(route for route in ferry_schedule.FERRY_ROUTES if route.code == "sindo-skp-hf")
        for day in (date(2026, 10, 23), date(2026, 10, 24)):
            with self.subTest(day=day):
                slots = [value for _slot, value in ferry_schedule._slots(route, day)]
                times = [value.strftime("%H:%M") for value in slots]
                self.assertIn("17:00", times)
                self.assertIn("20:30", times)
                self.assertNotIn("17:40", times)
                self.assertNotIn("18:30", times)
                self.assertEqual(any(value.strftime("%H:%M") == "13:50" for value in slots), day.weekday() >= 5)
                self.assertTrue(all(value.utcoffset() == timedelta(hours=7) for value in slots))

    def test_all_six_supported_cards_match_independently_captured_primary_calendar(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "sindo_route_cards_2026_10_23.json").read_text())
        cards = []
        for service_id, departures in fixture["cards"].items():
            origin, destination, zone = ferry_refresh._SINDO_HEADERS[service_id]
            rows = "".join((f'<img src="/assets/ship{item["calendar_icon"]}.png"/>' if item["calendar_icon"] is not None else "") + f'<p>{item["time"]}</p>' for item in departures)
            cards.append(f'<div class="MuiCard-root"><p>{origin}</p><p>{destination}</p><p>{zone}</p>{rows}</div>')
        body = ("SINDO FERRY SCHEDULE " + "".join(cards) + " LEGENDS: Saturdays and Sundays Only Sunday Only and operated by Interlining Partner").encode()
        result = ferry_refresh._validate_sindo(body)
        self.assertEqual(set(result["validated_service_ids"]), set(ferry_refresh._SINDO_HEADERS))
        self.assertGreater(result["matched_departure_slot_count"], 40)
