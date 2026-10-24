"""Optional external collector → HTTPS API numeric aggregate delivery.

Only an explicit operator-configured endpoint receives the CCTV-only token.
Redirects are refused; media, source URLs and arbitrary metadata are rejected.
"""
from __future__ import annotations

import json
import os
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


class CCTVDeliveryError(RuntimeError):
    def __init__(self, code, retryable):
        super().__init__(code)
        self.code, self.retryable = code, retryable


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def configured():
    return bool(os.environ.get("CROSSFLOW_CCTV_INGEST_URL", "").strip()
                or os.environ.get("CROSSFLOW_CCTV_INGEST_TOKEN", "").strip())


def _configuration():
    raw = os.environ.get("CROSSFLOW_CCTV_INGEST_URL", "").strip()
    token = os.environ.get("CROSSFLOW_CCTV_INGEST_TOKEN", "").strip()
    try:
        parsed = urlparse(raw)
        valid = (parsed.scheme == "https" and parsed.hostname and parsed.username is None
                 and parsed.password is None and parsed.port in (None, 443)
                 and parsed.path == "/api/internal/cctv-observations" and not parsed.query
                 and not parsed.params and not parsed.fragment
                 and 32 <= len(token) <= 8192 and all(33 <= ord(c) <= 126 for c in token))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise CCTVDeliveryError("delivery_configuration", False)
    return raw, token


def publish(evidence, *, opener=None):
    """Post a single numeric observation; neither exception nor receipt leaks auth."""
    # Do not accept an arbitrary dict as a way around the numeric-only boundary.
    from services.cctv_store import EVIDENCE_FIELDS, MAX_EVIDENCE_BYTES
    if not isinstance(evidence, dict) or set(evidence) - EVIDENCE_FIELDS or evidence.get("images_retained") is not False:
        raise CCTVDeliveryError("delivery_rejected", False)
    try:
        body = json.dumps(evidence, allow_nan=False, separators=(",", ":")).encode()
    except (ValueError, TypeError, RecursionError) as error:
        raise CCTVDeliveryError("delivery_rejected", False) from error
    if not 0 < len(body) <= MAX_EVIDENCE_BYTES:
        raise CCTVDeliveryError("delivery_rejected", False)
    url, token = _configuration()
    request = Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "X-Crossflow-Cctv-Token": token, "User-Agent": "CrossFlow-numeric-camera-collector/1.0"})
    if opener is None:
        import certifi
        opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())))
    try:
        with opener.open(request, timeout=2.) as response:
            raw = response.read(4097)
            if len(raw) > 4096 or not 200 <= response.status < 300:
                raise CCTVDeliveryError("delivery_rejected", False)
            payload = json.loads(raw)
        receipt = payload.get("cctv_observation") if isinstance(payload, dict) else None
        if (not isinstance(receipt, dict) or receipt.get("accepted") is not True
                or receipt.get("camera_id") != evidence.get("camera_id")
                or receipt.get("observed_at") != evidence.get("captured_at")):
            raise CCTVDeliveryError("delivery_rejected", False)
        return {"accepted": True, "idempotent": receipt.get("idempotent") is True}
    except HTTPError as error:
        retryable = error.code in {408, 425, 429, 500, 502, 503, 504}
        raise CCTVDeliveryError("delivery_unavailable" if retryable else "delivery_rejected", retryable) from error
    except (TimeoutError, URLError, OSError) as error:
        raise CCTVDeliveryError("delivery_unavailable", True) from error
    except (ValueError, TypeError, UnicodeError) as error:
        raise CCTVDeliveryError("delivery_rejected", False) from error
