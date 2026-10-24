"""SQLite persistence for completed route responses.

The store is intentionally small and provider-neutral.  Route calculation does
not depend on it: a storage failure leaves the computed response available, and
the response metadata reports whether the local store is process-local.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from services.route_identity import (
    SHORT_ROUTE_CODE_ALPHABET,
    SHORT_ROUTE_CODE_LENGTH,
    route_id as make_route_id,
    short_route_code,
)

MAX_ROUTE_JSON_BYTES = 8 * 1024 * 1024
MAX_REQUEST_JSON_BYTES = 64 * 1024


def database_path() -> str:
    configured = os.environ.get("CROSSFLOW_ROUTE_DB", "").strip()
    if configured:
        return configured
    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return os.path.join("/tmp", "crossflow-ai", "routes.db")
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "routes.db",
    )


class RouteStore:
    """Thread-safe SQLite route store with a memory fallback."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or database_path()
        self._lock = threading.RLock()
        self._fallback_in_memory = False
        self._conn = self._connect(self.path)
        self._ensure_schema()

    def _connect(self, path: str) -> sqlite3.Connection:
        connection: Optional[sqlite3.Connection] = None
        try:
            if path != ":memory:":
                parent = os.path.dirname(path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
            connection = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            if path != ":memory:":
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
            return connection
        except (OSError, sqlite3.Error):
            if connection is not None:
                connection.close()
            self._fallback_in_memory = True
            fallback = sqlite3.connect(":memory:", check_same_thread=False)
            fallback.row_factory = sqlite3.Row
            return fallback

    def _ensure_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS routes (
                    route_id TEXT PRIMARY KEY,
                    route_code TEXT,
                    route_kind TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                str(row[1]) for row in self._conn.execute("PRAGMA table_info(routes)")
            }
            if "route_code" not in columns:
                self._conn.execute("ALTER TABLE routes ADD COLUMN route_code TEXT")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_routes_route_code "
                "ON routes(route_code) WHERE route_code IS NOT NULL",
            )
            # Keep attribution separate from content-addressed routes: two
            # accounts can plan the same path without transferring ownership
            # of either account's history entry to the other.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS route_history (
                    route_id TEXT NOT NULL,
                    account_key TEXT NOT NULL,
                    origin_name TEXT,
                    destination_name TEXT,
                    vehicle_type TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (route_id, account_key)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_route_history_account_time "
                "ON route_history(account_key, created_at DESC)",
            )

    def route_code_for(self, route_key: str) -> str:
        """Reserve a deterministic available short code for a route ID."""
        with self._lock:
            existing = self._conn.execute(
                "SELECT route_code FROM routes WHERE route_id = ?",
                (route_key,),
            ).fetchone()
            if existing is not None and existing[0]:
                return str(existing[0])
            for attempt in range(64):
                candidate = short_route_code(route_key, attempt)
                collision = self._conn.execute(
                    "SELECT route_id FROM routes WHERE route_code = ?",
                    (candidate,),
                ).fetchone()
                if collision is None or str(collision[0]) == route_key:
                    return candidate
        raise RuntimeError("Could not allocate a unique seven-character route code")

    @property
    def durable(self) -> bool:
        return not self._fallback_in_memory and self.path != ":memory:"

    def storage_metadata(self) -> dict[str, Any]:
        """Describe the single-host boundary without exposing its file path."""
        in_memory = self._fallback_in_memory or self.path == ":memory:"
        resolved = os.path.realpath(self.path)
        ephemeral = bool(
            os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
            or resolved.startswith((
                "/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/",
            ))
        )
        return {
            "source": "local_sqlite",
            "shared": False,
            "durable": not in_memory and not ephemeral,
            "durability": (
                "process_memory" if in_memory
                else "ephemeral_file" if ephemeral
                else "persistent_local_file"
            ),
            "scope": "single_host",
        }

    @staticmethod
    def _json_value(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="python")
        if isinstance(value, Mapping):
            return {str(k): RouteStore._json_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [RouteStore._json_value(v) for v in value]
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    @classmethod
    def _dumps(cls, value: Any) -> str:
        return json.dumps(
            cls._json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def save(
        self,
        route_key: str,
        response: Mapping[str, Any],
        *,
        request: Any,
        route_kind: str,
        route_code: Optional[str] = None,
        created_by: Optional[str] = None,
        origin_name: Optional[str] = None,
        destination_name: Optional[str] = None,
        vehicle_type: Optional[str] = None,
    ) -> str:
        """Idempotently persist one response under its validated identity."""
        if route_key != make_route_id(request, route_kind=route_kind):
            raise ValueError("route_id does not match the canonical route request")
        code = route_code or self.route_code_for(route_key)
        if (
            len(code) != SHORT_ROUTE_CODE_LENGTH
            or any(character not in SHORT_ROUTE_CODE_ALPHABET for character in code)
        ):
            raise ValueError("route_code must be a valid seven-character route code")
        request_json = self._dumps(request)
        response_json = self._dumps(response)
        if (response.get("route_id", route_key) != route_key
                or response.get("route_code", code) != code):
            raise ValueError("Route response identity differs from its storage identity")
        if (len(request_json.encode("utf-8")) > MAX_REQUEST_JSON_BYTES
                or len(response_json.encode("utf-8")) > MAX_ROUTE_JSON_BYTES):
            raise ValueError("Route storage payload exceeds its byte budget")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT route_code FROM routes WHERE route_id = ?", (route_key,),
            ).fetchone()
            if existing is not None and existing[0] and existing[0] != code:
                raise ValueError("A saved route's dispatch code cannot be changed")
            self._conn.execute(
                """
                INSERT INTO routes
                    (route_id, route_code, route_kind, request_json, response_json,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(route_id) DO UPDATE SET
                    route_code = COALESCE(routes.route_code, excluded.route_code),
                    response_json = excluded.response_json,
                    updated_at = excluded.updated_at
                """,
                (route_key, code, route_kind, request_json, response_json, now, now),
            )
            self._conn.execute(
                """
                INSERT INTO route_history
                    (route_id, account_key, origin_name, destination_name,
                     vehicle_type, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(route_id, account_key) DO UPDATE SET
                    origin_name = excluded.origin_name,
                    destination_name = excluded.destination_name,
                    vehicle_type = excluded.vehicle_type,
                    updated_at = excluded.updated_at
                """,
                (
                    route_key, created_by or "", origin_name, destination_name,
                    vehicle_type, now, now,
                ),
            )
        return code

    def recent(
        self,
        limit: int = 20,
        *,
        created_by: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """List summaries for one account, or one latest entry per route.

        The caller enforces administrator privileges before requesting all
        accounts. A scoped query always compares the server-verified id using
        a SQL parameter, and never includes guest or another account's runs.
        """
        bounded = max(1, min(int(limit), 100))
        account_filter = "WHERE h.account_key = ?" if created_by is not None else ""
        parameters: tuple[Any, ...] = (
            (created_by, bounded) if created_by is not None else (bounded,)
        )
        with self._lock:
            rows = self._conn.execute(
                f"""
                WITH summaries AS (
                    SELECT r.route_id, r.route_code, r.route_kind,
                           h.origin_name, h.destination_name, h.vehicle_type,
                           NULLIF(h.account_key, '') AS created_by,
                           h.created_at, h.updated_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY r.route_id
                               ORDER BY h.created_at DESC, h.account_key
                           ) AS account_rank
                    FROM routes r JOIN route_history h ON h.route_id = r.route_id
                    {account_filter}
                )
                SELECT route_id, route_code, route_kind, origin_name,
                       destination_name, vehicle_type, created_by,
                       created_at, updated_at
                FROM summaries WHERE account_rank = 1
                ORDER BY created_at DESC, route_id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, route_key: str) -> Optional[dict[str, Any]]:
        if not isinstance(route_key, str):
            return None
        if len(route_key) == 64:
            query = "SELECT response_json FROM routes WHERE route_id = ?"
        elif len(route_key) == SHORT_ROUTE_CODE_LENGTH:
            query = "SELECT response_json FROM routes WHERE route_code = ?"
        else:
            return None
        with self._lock:
            row = self._conn.execute(
                query,
                (route_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


DEFAULT_ROUTE_STORE = RouteStore()


__all__ = ["RouteStore", "DEFAULT_ROUTE_STORE", "database_path"]
