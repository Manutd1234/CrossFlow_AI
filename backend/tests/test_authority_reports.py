"""Official report parsing, bounded refresh and last-good local persistence."""

import json
import sys
import tempfile
import unittest
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services.authority_reports import AuthorityReportStore, parse_report
from services.ferry_refresh import FetchedSource


def report_html(period="Januari-Agustus 2026", domestic="3.075.059"):
    return f"""<html><script>999999 KAPAL (CALL)</script>
    <h2>Kinerja Operasional</h2><h2>{period}</h2>
    <div>51.733</div><h4>KAPAL (CALL)</h4>
    <div>13.638.408</div><h4>KAPAL (GT)</h4>
    <div>{domestic}</div><h4>PENUMPANG DOMESTIK (Orang)</h4>
    <div>3.643.016</div><h4>PENUMPANG INTERNASIONAL (Orang)</h4>
    <h2>Peta Wilayah Kerja</h2>999 KAPAL (CALL)</html>""".encode()


def fetched(body):
    return FetchedSource(body, 200, "https://batamport.bpbatam.go.id/pelabuhan-penumpang/", {})


class AuthorityReportTests(unittest.TestCase):
    def test_labels_period_and_script_exclusion(self):
        report = parse_report(report_html())
        self.assertEqual(report["period_start"], "2026-01")
        self.assertEqual(report["period_end"], "2026-08")
        self.assertEqual([metric["value"] for metric in report["metrics"]], [51733, 3075059, 3643016])

    def test_new_source_period_and_values_are_not_fixed(self):
        report = parse_report(report_html("Januari–September 2026", "3.999.001"))
        self.assertEqual(report["period_end"], "2026-09")
        self.assertEqual(report["metrics"][1]["value"], 3999001)

    def test_incomplete_ambiguous_and_reversed_source_rejected(self):
        for body in (
            report_html().replace(b"PENUMPANG DOMESTIK", b"unknown"),
            report_html().replace(b"<h2>Peta", b"1 PENUMPANG DOMESTIK <h2>Peta"),
            report_html("September-Januari 2026"),
            report_html(domestic="12.34"),
            b"<p>No operational report available</p>",
        ):
            with self.subTest(body=body[:80]), self.assertRaises(ValueError):
                parse_report(body)

    def test_wordpress_representation_can_supply_verified_content(self):
        body = json.dumps({"content": {"rendered": report_html().decode()}}).encode()
        self.assertEqual(parse_report(body)["metrics"][0]["value"], 51733)

    def test_totals_from_a_later_period_cannot_complete_an_earlier_empty_report(self):
        for earlier_period in (b"Januari-Agustus 2025", b"Januari-Agustus 2026"):
            body = report_html("Januari-September 2026").replace(
                b"<h2>Januari-September 2026</h2>",
                b"<h2>" + earlier_period + b"</h2><p>Report unavailable</p>"
                b"<h2>Januari-September 2026</h2>",
            )
            with self.subTest(earlier_period=earlier_period), self.assertRaises(ValueError):
                parse_report(body)

    def test_duplicate_period_headings_are_ambiguous_even_when_counts_appear_once(self):
        body = report_html().replace(
            b"<div>51.733</div>", b"<h3>Januari-Agustus 2026</h3><div>51.733</div>",
        )
        with self.assertRaises(ValueError):
            parse_report(body)

    def test_cache_refresh_failure_and_reopen_retain_actual_prior_report(self):
        now = datetime(2026, 10, 23, 10, tzinfo=timezone.utc)
        calls = []
        def fetch(_):
            calls.append(1)
            return fetched(report_html())
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "reports.db")
            store = AuthorityReportStore(path)
            first = store.get(now, fetch=fetch, monotonic=0)
            self.assertEqual(first["status"], "fresh")
            self.assertEqual(len(first["stored_reports"]), 1)
            again = store.get(now + timedelta(seconds=1), fetch=fetch, monotonic=1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(again["fetched_at"], first["fetched_at"])
            def broken(_):
                return fetched(b"<p>Temporary challenge instead of a report</p>")
            stale = AuthorityReportStore(path).get(now + timedelta(minutes=6), fetch=broken, monotonic=360)
            self.assertEqual(stale["status"], "stale")
            self.assertEqual(stale["report"], first["report"])
            self.assertEqual(len(stale["stored_reports"]), 1)

    def test_changed_reporting_period_is_archived_without_inventing_previous_periods(self):
        now = datetime(2026, 10, 23, 10, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            store = AuthorityReportStore(str(Path(directory) / "reports.db"))
            store.get(now, fetch=lambda _: fetched(report_html()), monotonic=0)
            result = store.get(now + timedelta(minutes=6), fetch=lambda _: fetched(report_html("Januari-September 2026")), monotonic=360)
            self.assertEqual(len(result["stored_reports"]), 2)
            self.assertEqual(result["report"]["period_end"], "2026-09")

    def test_first_fetch_failure_has_no_seeded_report(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AuthorityReportStore(str(Path(directory) / "reports.db"))
            result = store.get(fetch=lambda _: fetched(b"offline"), monotonic=0)
            self.assertEqual(result["status"], "unavailable")
            self.assertIsNone(result["report"])
            self.assertEqual(result["stored_reports"], [])

    def test_storage_write_failure_does_not_claim_durable_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AuthorityReportStore(str(Path(directory) / "reports.db"))
            with patch.object(store, "_save", side_effect=sqlite3.OperationalError("disk full")):
                result = store.get(fetch=lambda _: fetched(report_html()), monotonic=0)
            self.assertEqual(result["status"], "fresh")
            self.assertFalse(result["storage"]["available"])
            self.assertFalse(result["storage"]["durable"])
            self.assertEqual(result["report"]["metrics"][0]["value"], 51733)

    def test_cached_read_cannot_promote_an_unsaved_report_to_durable(self):
        # Mock reads/writes so this tests the durable-path classification too,
        # without writing anywhere outside the disposable test fixtures.
        store = AuthorityReportStore("/var/lib/crossflow/reports.db")
        now = datetime(2026, 10, 23, 10, tzinfo=timezone.utc)
        with patch.object(store, "_history", return_value=[]), \
                patch.object(store, "_save", side_effect=sqlite3.OperationalError("disk full")), \
                patch("services.authority_reports.Path.resolve", return_value=Path(store.path)):
            first = store.get(now, fetch=lambda _: fetched(report_html()), monotonic=0)
            cached = store.get(now + timedelta(seconds=1), monotonic=1)
        self.assertFalse(first["storage"]["latest_report_persisted"])
        self.assertFalse(cached["storage"]["latest_report_persisted"])
        self.assertFalse(cached["storage"]["durable"])
        self.assertTrue(cached["storage"]["available"])
        self.assertEqual(cached["stored_reports"], [])
        self.assertEqual(cached["report"], first["report"])

    def test_previous_saved_report_does_not_make_a_new_unsaved_report_persisted(self):
        now = datetime(2026, 10, 23, 10, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            store = AuthorityReportStore(str(Path(directory) / "reports.db"))
            saved = store.get(now, fetch=lambda _: fetched(report_html()), monotonic=0)
            self.assertTrue(saved["storage"]["latest_report_persisted"])
            with patch.object(store, "_save", side_effect=sqlite3.OperationalError("disk full")):
                newer = store.get(now + timedelta(minutes=6), fetch=lambda _: fetched(report_html("Januari-September 2026")), monotonic=360)
            cached = store.get(now + timedelta(minutes=6, seconds=1), monotonic=361)
            self.assertEqual(cached["report"]["period_end"], "2026-09")
            self.assertEqual(cached["stored_reports"][0]["period_end"], "2026-08")
            self.assertFalse(newer["storage"]["latest_report_persisted"])
            self.assertFalse(cached["storage"]["latest_report_persisted"])
            self.assertFalse(cached["storage"]["durable"])

    def test_reopening_a_saved_report_restores_only_its_actual_persistence(self):
        now = datetime(2026, 10, 23, 10, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "reports.db")
            AuthorityReportStore(path).get(now, fetch=lambda _: fetched(report_html()), monotonic=0)
            reopened = AuthorityReportStore(path).get(now + timedelta(minutes=6), fetch=lambda _: fetched(b"unavailable"), monotonic=360)
        self.assertEqual(reopened["status"], "stale")
        self.assertTrue(reopened["storage"]["latest_report_persisted"])

    def test_memory_connection_does_not_claim_its_discarded_rows_were_saved(self):
        result = AuthorityReportStore(":memory:").get(fetch=lambda _: fetched(report_html()), monotonic=0)
        self.assertFalse(result["storage"]["latest_report_persisted"])
        self.assertFalse(result["storage"]["durable"])
        self.assertEqual(result["stored_reports"], [])


if __name__ == "__main__":
    unittest.main()
