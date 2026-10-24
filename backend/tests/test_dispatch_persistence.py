"""Issue a dispatch code only after a route store confirms the write."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

import main as api_main  # noqa: E402
from services import route_run_store, route_store  # noqa: E402


class DispatchPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = route_store.RouteStore(":memory:")
        self.addCleanup(self.store.close)
        replacement = patch.object(api_main, "_ROUTE_STORE", self.store)
        replacement.start()
        self.addCleanup(replacement.stop)

    def persist(self):
        return api_main._persist_route_response(
            api_main.RouteRequest(corridor_id="corridor-1", vehicle_type="COMMUTER"),
            "optimize-route",
            {
                "route_type": "ROAD_ROUTE",
                "route_geometry": [[104.0125, 1.1465], [104.0554, 1.1318]],
                "total_eta_mins": 12.5,
            },
            datetime.now(timezone.utc), created_by="operator-1",
        )

    def test_failed_writes_return_a_preview_without_an_assignable_code_or_history(self) -> None:
        with patch.object(self.store, "save", side_effect=OSError("storage failed")), \
                patch.object(route_run_store, "save", return_value=False):
            preview = self.persist()
        self.assertFalse(preview["dispatch_available"])
        self.assertIsNone(preview["route_code"])
        self.assertRegex(preview["route_id"], r"^[0-9a-f]{64}$")
        self.assertEqual(preview["total_eta_mins"], 12.5)
        self.assertEqual(len(preview["route_geometry"]), 2)
        self.assertEqual(preview["route_storage"]["source"], "unavailable")
        self.assertFalse(preview["route_storage"]["durable"])
        self.assertIsNone(self.store.get(preview["route_id"]))
        self.assertEqual(self.store.recent(created_by="operator-1"), [])

    def test_confirmed_local_write_issues_a_code_when_shared_storage_fails(self) -> None:
        with patch.object(route_run_store, "save", return_value=False):
            planned = self.persist()
        self.assertTrue(planned["dispatch_available"])
        self.assertRegex(planned["route_code"], r"^[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{7}$")
        self.assertEqual(planned["route_storage"], self.store.storage_metadata())
        self.assertFalse(planned["route_storage"]["shared"])
        self.assertEqual(planned["route_storage"]["durability"], "process_memory")
        self.assertEqual(self.store.get(planned["route_code"])["route_id"], planned["route_id"])
        self.assertEqual(self.store.recent(created_by="operator-1")[0]["route_code"], planned["route_code"])

    def test_confirmed_shared_write_issues_a_code_when_local_storage_fails(self) -> None:
        captured = {}

        def save_shared(*arguments, **_kwargs):
            captured.update(arguments[4])
            return True

        with patch.object(self.store, "save", side_effect=OSError("storage failed")), \
                patch.object(route_run_store, "save", side_effect=save_shared):
            planned = self.persist()
        self.assertTrue(planned["dispatch_available"])
        self.assertEqual(planned["route_storage"], {
            "source": "shared_supabase", "shared": True, "durable": True,
            "durability": "persistent_shared_store", "scope": "shared",
        })
        self.assertEqual(captured["route_code"], planned["route_code"])
        with patch.object(route_run_store, "configured", return_value=True), \
                patch.object(route_run_store, "get_lookup_result", return_value=(captured, True)):
            retrieved = api_main.api_get_route(planned["route_code"])
        self.assertEqual(retrieved["route_storage"], planned["route_storage"])
        self.assertTrue(retrieved["dispatch_available"])

    def test_a_shared_exception_does_not_hide_a_confirmed_local_dispatch(self) -> None:
        with patch.object(route_run_store, "save", side_effect=RuntimeError("storage failed")):
            planned = self.persist()
        self.assertTrue(planned["dispatch_available"])
        self.assertEqual(planned["route_storage"]["source"], "local_sqlite")
        self.assertEqual(api_main.api_get_route(planned["route_code"])["route_geometry"], planned["route_geometry"])

    def test_unconfirmed_writes_do_not_count_as_saved(self) -> None:
        with patch.object(self.store, "save", return_value=None), \
                patch.object(route_run_store, "save", return_value=None):
            planned = self.persist()
        self.assertFalse(planned["dispatch_available"])
        self.assertIsNone(planned["route_code"])

    def test_serverless_local_dispatch_declares_its_single_host_ephemeral_scope(self) -> None:
        with patch.dict(os.environ, {"VERCEL": "1"}), \
                patch.object(route_run_store, "save", return_value=False):
            planned = self.persist()
        self.assertTrue(planned["dispatch_available"])
        self.assertFalse(planned["route_storage"]["shared"])
        self.assertFalse(planned["route_storage"]["durable"])
        self.assertEqual(planned["route_storage"]["scope"], "single_host")


if __name__ == "__main__":
    unittest.main()
