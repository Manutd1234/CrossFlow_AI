"""Persisted routes and per-account history must remain atomic and consistent."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import route_store
from services.route_identity import route_id


class RouteStoreIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.store = route_store.RouteStore(":memory:")
        self.addCleanup(self.store.close)
        self.request = {"vehicle_type": "COMMUTER", "destination": "Batam Centre"}
        self.identity = route_id(self.request)

    def save(self, response, **options):
        return self.store.save(self.identity, response, request=self.request, route_kind="optimize-route", **options)

    def test_mismatched_response_identity_cannot_create_a_dispatch_or_history_entry(self):
        for response in ({"route_id": "a" * 64}, {"route_code": "BCDEFGH"}):
            with self.subTest(response=response), self.assertRaises(ValueError):
                self.save(response, route_code="ABCDEFG", created_by="operator")
        self.assertIsNone(self.store.get(self.identity))
        self.assertEqual(self.store.recent(), [])

    def test_rewrite_cannot_change_an_existing_code_or_its_returned_identity(self):
        response = {"route_id": self.identity, "route_code": "ABCDEFG", "eta": 10}
        self.save(response, route_code="ABCDEFG")
        with self.assertRaisesRegex(ValueError, "cannot be changed"):
            self.save({**response, "route_code": "BCDEFGH", "eta": 99}, route_code="BCDEFGH")
        self.assertEqual(self.store.get("ABCDEFG"), response)
        self.assertIsNone(self.store.get("BCDEFGH"))

    def test_byte_budget_counts_utf8_before_either_table_is_modified(self):
        with patch.object(route_store, "MAX_ROUTE_JSON_BYTES", 60), self.assertRaisesRegex(ValueError, "byte budget"):
            self.save({"note": "路" * 20})
        self.assertIsNone(self.store.get(self.identity))
        self.assertEqual(self.store.recent(), [])

    def test_history_failure_rolls_back_the_route_insert_and_allows_clean_retry(self):
        self.store._conn.execute("CREATE TRIGGER unit_history_failure BEFORE INSERT ON route_history BEGIN SELECT RAISE(ABORT, 'unit failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.save({"eta": 10}, created_by="operator")
        self.assertIsNone(self.store.get(self.identity))
        self.assertEqual(self.store.recent(), [])
        self.store._conn.execute("DROP TRIGGER unit_history_failure")
        code = self.save({"eta": 10}, created_by="operator")
        self.assertEqual(self.store.get(code), {"eta": 10})
        self.assertEqual(len(self.store.recent(created_by="operator")), 1)

    def test_concurrent_file_writes_survive_reopen_with_their_original_codes(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "routes.db")
            store = route_store.RouteStore(path)
            try:
                def write(index):
                    request = {"stop": index}
                    identity = route_id(request)
                    code = store.save(identity, {"route_id": identity, "stop": index}, request=request,
                                      route_kind="optimize-route", created_by="operator")
                    return code, index
                with ThreadPoolExecutor(max_workers=4) as workers:
                    saved = list(workers.map(write, range(20)))
                self.assertEqual(len({code for code, _ in saved}), 20)
            finally:
                store.close()
            reopened = route_store.RouteStore(path)
            try:
                for code, index in saved:
                    self.assertEqual(reopened.get(code)["stop"], index)
                self.assertEqual(len(reopened.recent(created_by="operator")), 20)
                self.assertEqual(reopened._conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                reopened.close()
