"""The retired upload endpoints never read images or start the detector."""
import asyncio
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")
import main as api
from services import vehicle_count_preview as bridge

PREVIEW_PATH = "/api/analytics/vehicle-count-preview"


def asgi_request(method="POST", path=PREVIEW_PATH, *, chunks=(b"image",),
                 content_type="image/jpeg", content_length=None, authorization="Bearer operator", extra_headers=()):
    """Drive ASGI directly; count actual body reads, including short-circuit cases."""
    state = {"reads": 0, "messages": []}
    headers = []
    for key, value in (("content-type", content_type), ("content-length", content_length),
                       ("authorization", authorization)):
        if value is not None:
            headers.append((key.encode(), str(value).encode()))

    headers.extend((key.lower().encode(), value.encode()) for key, value in extra_headers)

    async def run():
        index = 0
        async def receive():
            nonlocal index
            state["reads"] += 1
            if index >= len(chunks):
                await asyncio.Event().wait()
            chunk = chunks[index]
            index += 1
            return {"type": "http.request", "body": chunk, "more_body": index < len(chunks)}
        async def send(message):
            state["messages"].append(message)
        await api.app({
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": method, "scheme": "http",
            "root_path": "", "path": path, "raw_path": path.encode(), "query_string": b"",
            "server": ("test", 80), "client": ("test", 1), "headers": headers,
        }, receive, send)
    asyncio.run(asyncio.wait_for(run(), timeout=5))
    start = next(m for m in state["messages"] if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in state["messages"] if m["type"] == "http.response.body")
    state.update(status=start["status"], headers=dict(start["headers"]), payload=json.loads(payload))
    return state


class VehicleCountPreviewRetirementTests(unittest.TestCase):
    def test_retired_upload_and_status_never_read_or_start_detector(self):
        for method, path in (("POST", PREVIEW_PATH), ("GET", PREVIEW_PATH + "/status")):
            with self.subTest(method=method), patch.object(api.identity, "require_route_operator"), patch.object(bridge, "count_preview") as count, patch.object(bridge, "preview_status") as status:
                result = asgi_request(method=method, path=path)
            self.assertEqual(result["status"], 410)
            self.assertEqual(result["reads"], 0)
            self.assertIn("CCTV observations", result["payload"]["detail"])
            self.assertIn(b"no-store", result["headers"][b"cache-control"])
            count.assert_not_called()
            status.assert_not_called()

    def test_retirement_still_checks_operator_access_before_body(self):
        for method, path in (("POST", PREVIEW_PATH), ("GET", PREVIEW_PATH + "/status")):
            with self.subTest(method=method), patch.object(api.identity, "require_route_operator", side_effect=HTTPException(403, "Denied")), patch.object(bridge, "count_preview") as count:
                result = asgi_request(method=method, path=path, authorization="Bearer invalid")
            self.assertEqual(result["status"], 403)
            self.assertEqual(result["reads"], 0)
            self.assertIn(b"no-store", result["headers"][b"cache-control"])
            count.assert_not_called()


if __name__ == "__main__":
    unittest.main()
