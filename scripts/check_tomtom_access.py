#!/usr/bin/env python3
"""Check existing TomTom entitlements without logging keys or creating jobs."""

import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from services import tls


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_keys() -> dict[str, str]:
    """Read only named keys; file values never become code or console output."""
    names = {"TOMTOM_API_KEY", "TOMTOM_TRAFFIC_STATS_API_KEY"}
    values = {key: os.environ[key].strip() for key in names if os.environ.get(key, "").strip()}
    for name in (".env.tomtom.local", ".env"):
        path = ROOT / name
        if not path.is_file() or path.stat().st_size > 65536:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.removeprefix("export ").partition("=")
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if sep and key in names and value and key not in values:
                values[key] = value
    return values


def _check(path: str, parameters: dict[str, str], key: str) -> dict:
    opener = build_opener(NoRedirect(), HTTPSHandler(context=tls.default_context()))
    request = Request("https://api.tomtom.com" + path + "?" + urlencode({**parameters, "key": key}),
                      headers={"Accept": "application/json", "User-Agent": "CrossFlowAI/local-entitlement-check"})
    try:
        with opener.open(request, timeout=12) as response:
            payload = response.read(1024 * 1024 + 1)
            if len(payload) > 1024 * 1024:
                return {"status": "response_limit_exceeded", "http_status": response.status}
            data = json.loads(payload)
            if path.endswith("/flowSegmentData/absolute/10/json"):
                valid = isinstance(data, dict) and isinstance(data.get("flowSegmentData"), dict)
            else:
                # A successful authenticated listing establishes entitlement;
                # retain no account job names, signed links or response body.
                valid = isinstance(data, (dict, list))
            return {"status": "available" if valid else "invalid_response", "http_status": response.status}
    except HTTPError as error:
        return {"status": "access_denied" if error.code in (401, 403) else "http_error", "http_status": error.code}
    except Exception:
        # Exception messages commonly contain request URLs with the key.
        return {"status": "connection_or_response_unavailable", "http_status": None}


def check_access() -> dict:
    keys = local_keys()
    flow = keys.get("TOMTOM_API_KEY")
    historical = keys.get("TOMTOM_TRAFFIC_STATS_API_KEY") or flow
    return {
        "schema_version": 1,
        "credentials_configured": {"flow": bool(flow), "historical": bool(historical)},
        "current_flow": _check("/traffic/services/4/flowSegmentData/absolute/10/json", {"point": "1.0542,103.9762", "unit": "kmph"}, flow) if flow else {"status": "not_configured"},
        "historical_stats": _check("/traffic/trafficstats/job/search/1", {"perPage": "1", "pageIndex": "0"}, historical) if historical else {"status": "not_configured"},
        "historical_request_scope": "read_only_entitlement_check_no_new_jobs_no_paid_confirmation",
    }


if __name__ == "__main__":
    print(json.dumps(check_access(), indent=2))
