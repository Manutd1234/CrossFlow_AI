"""Single-host ferry verification receipts; never a shared deployment fallback.

Only successful semantic checks advance verification. The snapshot identifier
binds each receipt to the exact committed route/calendar artifact. No scraped
page content or credentials are retained here.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3


_available = False


def _path() -> Path | None:
    if os.environ.get("VERCEL"):
        return None
    explicit = os.environ.get("CROSSFLOW_FERRY_FRESHNESS_DB")
    history = os.environ.get("CROSSFLOW_HISTORY_DB")
    if explicit == ":memory:" or (explicit is None and history == ":memory:"):
        return None
    if explicit:
        return Path(explicit).expanduser()
    if history:
        return Path(history).expanduser().with_name("ferry_freshness.db")
    return Path(__file__).resolve().parents[1] / "data" / "ferry_freshness.db"


def _instant(value: str) -> datetime:
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Ferry verification receipt must have a timezone")
    return instant.astimezone(timezone.utc)


def _checked(row, snapshot_id):
    if row is None:
        return None
    saved_id, checked, verified = row
    if saved_id != snapshot_id or _instant(checked) < _instant(verified):
        raise ValueError("Invalid ferry verification receipt")
    return {"snapshot_id": saved_id, "latest_checked_at": checked, "last_verified_at": verified}


def load(snapshot_id: str):
    global _available
    path = _path()
    if path is None or not path.is_file():
        _available = False
        return None
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.5) as database:
            database.execute("PRAGMA query_only=ON")
            row = database.execute(
                "SELECT snapshot_id, latest_checked_at, last_verified_at "
                "FROM ferry_verification_receipts WHERE snapshot_id = ?", (snapshot_id,),
            ).fetchone()
        result = _checked(row, snapshot_id)
    except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError):
        _available = False
        return None
    _available = result is not None
    return result


def save(snapshot_id: str, checked_at: str, verified_at: str):
    global _available
    path = _path()
    if path is None:
        _available = False
        return None
    try:
        if not isinstance(snapshot_id, str) or not 1 <= len(snapshot_id) <= 300:
            raise ValueError("Invalid timetable snapshot identifier")
        _checked((snapshot_id, checked_at, verified_at), snapshot_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path, timeout=.5) as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                "CREATE TABLE IF NOT EXISTS ferry_verification_receipts ("
                "snapshot_id TEXT PRIMARY KEY, latest_checked_at TEXT NOT NULL, "
                "last_verified_at TEXT NOT NULL)"
            )
            prior = _checked(database.execute(
                "SELECT snapshot_id, latest_checked_at, last_verified_at "
                "FROM ferry_verification_receipts WHERE snapshot_id = ?", (snapshot_id,),
            ).fetchone(), snapshot_id)
            if prior is not None:
                checked_at = max((prior["latest_checked_at"], checked_at), key=_instant)
                verified_at = max((prior["last_verified_at"], verified_at), key=_instant)
            database.execute(
                "INSERT INTO ferry_verification_receipts VALUES (?, ?, ?) "
                "ON CONFLICT(snapshot_id) DO UPDATE SET "
                "latest_checked_at=excluded.latest_checked_at, last_verified_at=excluded.last_verified_at",
                (snapshot_id, checked_at, verified_at),
            )
            result = _checked((snapshot_id, checked_at, verified_at), snapshot_id)
        _available = True
        return result
    except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError):
        _available = False
        return None


def available() -> bool:
    return _available and _path() is not None


def _reset_for_tests(snapshot_id: str) -> None:
    global _available
    path = _path()
    if path is not None and path.is_file():
        try:
            with sqlite3.connect(path, timeout=.5) as database:
                database.execute("DELETE FROM ferry_verification_receipts WHERE snapshot_id = ?", (snapshot_id,))
        except sqlite3.Error:
            pass
    _available = False
