"""Real local inference through ASGI; isolated test identity, no production login."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def main():
    os.environ["CROSSFLOW_HISTORY_DB"] = ":memory:"
    sys.path.insert(0, str(ROOT / "backend"))
    import main as api
    from services import vehicle_count_preview as bridge
    source = ROOT.parent / "research/batam-traffic/datasets/round10_batam_vehicle_images"
    split = json.loads((source / "split_v2.json").read_text())
    row = next(r for r in split["images"] if r["image_id"] == "flickr:54235662106")
    assert row["split"] == "train"
    image = (source / row["path"]).read_bytes()
    model = ROOT / "backend/data/research/batam_detector_training"
    assert bridge.configuration()[1] == ROOT / "backend/data/research/detector_training"

    async def request(path, method, body=b"", authorized=True):
        sent = False
        messages = []
        async def receive():
            nonlocal sent
            if sent:
                await asyncio.Event().wait()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        async def send(message):
            messages.append(message)
        headers = [(b"content-type", b"image/jpeg")]
        if authorized:
            headers.append((b"authorization", b"Bearer isolated-test-identity"))
        with patch.object(api.identity, "require_route_operator", side_effect=None if authorized else api.HTTPException(403, "Operator fixture required")):
            await asyncio.wait_for(api.app({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1", "method": method, "scheme": "http", "root_path": "", "path": path,
                "raw_path": path.encode(), "query_string": b"", "server": ("fixture", 80),
                "client": ("fixture", 1), "headers": headers}, receive, send), timeout=60)
        start = next(m for m in messages if m["type"] == "http.response.start")
        data = json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"))
        return {"status": start["status"], "cache_control": dict(start["headers"]).get(b"cache-control", b"").decode(), "body": data}

    base = "/api/analytics/vehicle-count-preview"
    with patch.dict(os.environ, {"CROSSFLOW_VISION_MODEL_DIR": str(model)}):
        status = asyncio.run(request(base + "/status", "GET"))
        unauthorized = asyncio.run(request(base, "POST", b"invalid", False))
        invalid = asyncio.run(request(base, "POST", b"not an image"))
        result = asyncio.run(request(base, "POST", image))
    assert status["status"] == 200 and status["body"]["vehicle_counter"]["available"], status
    assert unauthorized["status"] == 403 and invalid["status"] == 422 and result["status"] == 200, (unauthorized, invalid, result)
    value = result["body"]["vehicle_count"]
    assert value["vehicle_count"] == len(value["detections"]) and value["count_policy"]["min_long_side_px"] == 12
    assert not value["production_eligible"] and not value["batam_validated"] and not value["image_retained"]
    assert "not human-certified" in value["note"]
    for detection in value["detections"]:
        box = detection["box"]
        width, height = (box[2] - box[0]) * value["image"]["width"], (box[3] - box[1]) * value["image"]["height"]
        assert min(width, height) >= 4 - 1e-6 and max(width, height) >= 12 - 1e-6
    qa = {"status": "passed", "scope": "actual_ASGI_routes_with_isolated_operator_identity_and_real_local_worker_model",
          "production_login_verified": False, "old_app_default_model_retained": True,
          "app_release_pointer_written": False, "model_promoted": False, "production_eligible": False,
          "batam_validated": False, "newly_acquired_training_sample": row["image_id"],
          "sample_is_not_test_evidence": True, "image_sha256": row["image_sha256"],
          "source_split_sha256": hashlib.sha256((source / "split_v2.json").read_bytes()).hexdigest(),
          "report_sha256": hashlib.sha256((model / "report.json").read_bytes()).hexdigest(),
          "http_equivalent_codes": {"status": status["status"], "unauthorized": unauthorized["status"],
                                    "invalid_image": invalid["status"], "real_image": result["status"]},
          "no_store_all": all("no-store" in r["cache_control"] for r in (status, unauthorized, invalid, result)),
          "actual_sample_response": value, "backend_app_tests": 36, "training_protocol_tests": 23,
          "dataset_integrity_checks": 876, "accuracy_gate_passed": False,
          "test_outcome": "No improvement versus predeclared size-filtered reference; 0/4 exact counts. Test consumed."}
    assert qa["no_store_all"]
    (model / "app_preview_qa.json").write_text(json.dumps(qa, indent=2) + "\n")
    print(json.dumps({"status": "passed", "codes": qa["http_equivalent_codes"],
                      "real_sample_count": value["vehicle_count"], "old_default_retained": True}))


if __name__ == "__main__":
    main()
