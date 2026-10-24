"""Source-dated BP Batam operating reports, fetched and retained without invented totals."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from services import clock, ferry_refresh


SOURCE = next(item for item in ferry_refresh.OFFICIAL_SOURCES
              if item.source_id == "bp-batam-passenger-ports")
CACHE_SECONDS = 300
MONTHS = {name: index + 1 for index, name in enumerate((
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
))}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipped = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.skipped += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.skipped:
            self.skipped -= 1

    def handle_data(self, data):
        if not self.skipped:
            self.parts.append(data)


def parse_report(body: bytes, *, source_url: str = SOURCE.url) -> dict[str, Any]:
    """Require a reporting period and all labelled counts in the same section."""
    decoded = body.decode("utf-8", errors="strict")
    if decoded.lstrip().startswith("{"):
        decoded = json.loads(decoded)["content"]["rendered"]
    parser = _Text()
    parser.feed(decoded)
    text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    marker = re.search(r"Kinerja\s+Operasional", text, re.I)
    if marker is None:
        raise ValueError("Official operating-report section is missing.")
    section = text[marker.end():]
    # Never read unrelated footer counters as operating results.
    section = re.split(r"Peta\s+Wilayah|KANTOR\s+PUSAT", section, flags=re.I)[0]
    months = "|".join(MONTHS)
    periods = list(re.finditer(rf"\b({months})\s*[-–—]\s*({months})\s+(20\d{{2}})\b", section, re.I))
    if not periods:
        raise ValueError("Official reporting period is missing.")
    if len(periods) != 1:
        # A page may gain another report or retain an older period heading.
        # Never borrow later totals to complete the first reporting period.
        raise ValueError("Official reporting period is ambiguous.")
    period = periods[0]
    start, end = (next(number for name, number in MONTHS.items()
                       if name.casefold() == period.group(index).casefold()) for index in (1, 2))
    if start > end:
        raise ValueError("Official reporting period is inconsistent.")
    year = int(period.group(3))
    after_period = section[period.end():]
    metrics = []
    for metric_id, label, source_label, unit in (
        ("vessel_calls", "Passenger-vessel calls", r"KAPAL\s*\(\s*CALL\s*\)", "calls"),
        ("domestic_passengers", "Domestic passenger movements", r"PENUMPANG\s+DOMESTIK", "people"),
        ("international_passengers", "International passenger movements", r"PENUMPANG\s+INTERNASIONAL", "people"),
    ):
        matches = re.findall(
            rf"(?<![\d.,])(\d{{1,3}}(?:\.\d{{3}})+|\d+)(?![\d.,])\s+{source_label}(?!\w)",
            after_period, re.I,
        )
        if len(matches) != 1:
            raise ValueError(f"Official report has ambiguous or missing {metric_id}.")
        value = int(matches[0].replace(".", ""))
        if not 0 <= value <= 10**11:
            raise ValueError("Official count is outside the supported range.")
        metrics.append({"id": metric_id, "label": label, "value": value, "unit": unit})
    return {
        "publisher": "BP Batam Port Business Entity",
        "source_url": source_url,
        "period_label": period.group(0),
        "period_start": f"{year}-{start:02d}",
        "period_end": f"{year}-{end:02d}",
        "metrics": metrics,
        "content_sha256": hashlib.sha256(body).hexdigest(),
    }


class AuthorityReportStore:
    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("CROSSFLOW_AUTHORITY_DB") or str(
            Path("/tmp/crossflow-ai") / "authority_reports.db"
            if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
            else Path(__file__).resolve().parents[1] / "data" / "authority_reports.db"
        )
        self.lock = threading.RLock()
        self.last_checked_monotonic: float | None = None
        self.last_checked_at: str | None = None
        self.last_error: str | None = None
        self.last_report: dict | None = None
        self.last_report_persisted = False

    @contextmanager
    def _connect(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=2)
        try:
            with connection:
                connection.execute("""CREATE TABLE IF NOT EXISTS authority_report_snapshots (
                    report_id TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, payload TEXT NOT NULL
                )""")
                yield connection
        finally:
            connection.close()

    def _save(self, report: dict, now: datetime):
        canonical = json.dumps({key: report[key] for key in (
            "period_start", "period_end", "metrics",
        )}, sort_keys=True)
        report_id = hashlib.sha256(canonical.encode()).hexdigest()
        payload = {**report, "fetched_at": clock.iso(now)}
        with self._connect() as db:
            db.execute("INSERT INTO authority_report_snapshots VALUES (?, ?, ?) "
                       "ON CONFLICT(report_id) DO UPDATE SET fetched_at=excluded.fetched_at, payload=excluded.payload",
                       (report_id, clock.iso(now), json.dumps(payload)))
        self.last_report = payload

    def _history(self):
        with self._connect() as db:
            rows = db.execute("SELECT payload FROM authority_report_snapshots ORDER BY fetched_at DESC LIMIT 24").fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, now: datetime | None = None, *, fetch: Callable | None = None, monotonic: float | None = None):
        now = now or clock.now()
        tick = time.monotonic() if monotonic is None else monotonic
        with self.lock:
            try:
                history = self._history()
                storage_available = True
            except (OSError, sqlite3.Error, ValueError):
                history, storage_available = [], False
            if history and self.last_report is None:
                self.last_report = history[0]
                self.last_report_persisted = self.path != ":memory:"
            if self.last_checked_monotonic is None or tick - self.last_checked_monotonic >= CACHE_SECONDS:
                self.last_checked_monotonic = tick
                self.last_checked_at = clock.iso(now)
                try:
                    fetched = (fetch or ferry_refresh._fetch_source)(SOURCE)
                    if fetched.status != 200:
                        raise ValueError("Official source did not return success.")
                    report = parse_report(fetched.body, source_url=SOURCE.url)
                    self.last_report = {**report, "fetched_at": clock.iso(now)}
                    self.last_report_persisted = False
                    if storage_available:
                        try:
                            self._save(report, now)
                            # :memory: uses a new connection per operation,
                            # so closing _save's connection discards the row.
                            self.last_report_persisted = self.path != ":memory:"
                            history = self._history()
                        except (OSError, sqlite3.Error, ValueError):
                            storage_available = False
                    self.last_error = None
                except Exception:
                    self.last_error = "The official report could not be fetched or parsed; any prior verified report is retained."
            report = self.last_report
            durable = storage_available and self.last_report_persisted and self.path != ":memory:" and not str(Path(self.path).resolve()).startswith(("/tmp/", "/private/tmp/"))
            return {
                "status": "unavailable" if report is None else "stale" if self.last_error else "fresh",
                "checked_at": self.last_checked_at,
                "fetched_at": report.get("fetched_at") if report else None,
                "report": report,
                "stored_reports": history,
                "storage": {
                    "durable": durable, "scope": "single_server",
                    "available": storage_available,
                    "latest_report_persisted": self.last_report_persisted,
                },
                "error": self.last_error,
                "limitations": [
                    "Official reporting-period aggregates, not current traffic speeds or live terminal occupancy.",
                    "Only successfully parsed source reports are retained; past periods are not fabricated.",
                    "The source is checked at most every five minutes, with a bounded two-attempt fetch.",
                ],
            }


STORE = AuthorityReportStore()
