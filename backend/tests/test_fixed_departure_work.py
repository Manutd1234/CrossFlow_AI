"""Precise departures must not solve discarded deferral routes."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services import route_solver, router  # noqa: E402


WIB = timezone(timedelta(hours=7))
NOW = datetime(2026, 8, 7, 14, 0, tzinfo=WIB)
DEPARTURE = NOW + timedelta(minutes=10)


class FixedDepartureWorkTests(unittest.TestCase):
    def test_named_route_only_computes_the_requested_departure_conditions(self) -> None:
        with patch.object(
            route_solver, "_geometry", wraps=route_solver._geometry,
        ) as solve:
            result = route_solver.optimize_route(
                corridor_id="corridor-1", vehicle_type="COMMUTER",
                departure_at=DEPARTURE, now=NOW,
            )
        self.assertEqual(result["planned_departure"], DEPARTURE.isoformat())
        self.assertEqual(result["optimal_departure"]["recommended"], "DEPART_NOW")
        # One primary solve, then the same conditions with alternatives.
        self.assertEqual(solve.call_count, 2)
        conditions = [call.kwargs["network_congestion_score"] for call in solve.call_args_list]
        self.assertEqual(conditions[0], conditions[1])

    def test_free_route_only_computes_the_requested_departure_conditions(self) -> None:
        with patch.object(
            router, "route_between_nodes", wraps=router.route_between_nodes,
        ) as solve:
            result = route_solver.optimize_free_route(
                origin_lat=1.1465, origin_lng=104.0125,
                destination_lat=1.1318, destination_lng=104.0554,
                vehicle_type="COMMUTER", departure_at=DEPARTURE, now=NOW,
            )
        self.assertEqual(result["planned_departure"], DEPARTURE.isoformat())
        self.assertEqual(result["optimal_departure"]["recommended"], "DEPART_NOW")
        self.assertEqual(solve.call_count, 2)
        conditions = [call.kwargs["network_congestion_score"] for call in solve.call_args_list]
        self.assertEqual(conditions[0], conditions[1])


if __name__ == "__main__":
    unittest.main()
