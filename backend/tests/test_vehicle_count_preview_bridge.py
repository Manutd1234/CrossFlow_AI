"""Detector subprocess and response trust boundaries, without Torch or a model."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import vehicle_count_preview as bridge


def valid_result():
    return {"vehicle_count": 1, "image": {"width": 640, "height": 480},
            "detections": [{"class_name": "vehicle", "score": .8, "box": [.1, .2, .6, .7]}],
            "model_id": "research-model", "latency_ms": 12.0}


def worker_reply(result=None, **kwargs):
    payload = {"ok": True, "result": valid_result() if result is None else result}
    payload.update(kwargs)
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload).encode())


class VehicleCountPreviewBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "server-configured-python"
        self.runtime.write_bytes(b"fixture only; never executed")
        self.model = self.root / "server-configured-model"
        self.model.mkdir()
        self.config = patch.object(bridge, "configuration", return_value=(self.runtime, self.model))
        self.config.start()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.config.stop)
        self.addCleanup(self.environment.stop)

    def test_subprocess_uses_only_local_server_paths_and_base64_stdin(self):
        image = b"image bytes containing https://example.invalid/path and /private/local"
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        with patch.object(bridge.subprocess, "run", return_value=worker_reply()) as run:
            result = bridge.count_preview(image, "image/png")
        args, kwargs = run.call_args
        self.assertEqual(args[0], [str(self.runtime), str(bridge.ROOT/"scripts/vehicle_counter_worker.py"),
                                   "--count", str(self.model)])
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(kwargs["timeout"], bridge.INFERENCE_TIMEOUT_SECONDS)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        request = json.loads(kwargs["input"])
        self.assertEqual(set(request), {"image_base64"})
        self.assertEqual(base64.b64decode(request["image_base64"], validate=True), image)
        self.assertEqual(kwargs["env"]["OMP_NUM_THREADS"], "2")
        self.assertEqual(kwargs["env"]["MKL_NUM_THREADS"], "2")
        self.assertFalse(result["image_retained"])
        self.assertFalse(result["production_eligible"])
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")))

    def test_status_needs_no_uploaded_body_and_maps_real_worker_enabled_flags(self):
        for artifacts, enabled, expected in ((True, True, True), (True, False, False), (False, True, False)):
            with self.subTest(artifacts=artifacts, enabled=enabled), patch.object(bridge.subprocess, "run", return_value=worker_reply({"artifacts_available": artifacts, "enabled": enabled})) as run:
                result = bridge.preview_status()
            self.assertEqual(run.call_args.args[0][2], "--status")
            self.assertIsNone(run.call_args.kwargs["input"])
            self.assertEqual(result["available"], expected)
            self.assertTrue(result["experimental"])
            self.assertFalse(result["production_eligible"])
            self.assertFalse(result["batam_validated"])

    def test_missing_runtime_or_model_is_unavailable_before_subprocess(self):
        for runtime, model in ((self.root/"missing", self.model), (self.runtime, self.root/"missing")):
            with self.subTest(runtime=runtime, model=model), patch.object(bridge, "configuration", return_value=(runtime, model)), patch.object(bridge.subprocess, "run") as run:
                result = bridge.preview_status()
            self.assertFalse(result["available"])
            run.assert_not_called()

    def test_serverless_environments_are_rejected_before_subprocess(self):
        for variable in ("VERCEL", "AWS_LAMBDA_FUNCTION_NAME"):
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: "enabled"}), patch.object(bridge.subprocess, "run") as run:
                with self.assertRaises(bridge.PreviewUnavailable):
                    bridge.count_preview(b"image", "image/jpeg")
            run.assert_not_called()

    def test_worker_timeouts_os_errors_exit_failures_and_oversize_reply_fail_closed(self):
        failures = [subprocess.TimeoutExpired("worker", 45), OSError("runtime unavailable")]
        for error in failures:
            with self.subTest(error=type(error).__name__), patch.object(bridge.subprocess, "run", side_effect=error):
                with self.assertRaises(bridge.PreviewUnavailable):
                    bridge.count_preview(b"image", "image/jpeg")
        for reply in (subprocess.CompletedProcess([], 2, stdout=b""),
                      subprocess.CompletedProcess([], 0, stdout=b"x"*(bridge.MAX_REPLY_BYTES+1))):
            with self.subTest(returncode=reply.returncode), patch.object(bridge.subprocess, "run", return_value=reply):
                with self.assertRaises(bridge.PreviewUnavailable):
                    bridge.count_preview(b"image", "image/jpeg")

    def test_bad_protocol_json_types_and_failure_codes_fail_closed(self):
        for payload in (b"not-json", b"[]", b'{"ok":1,"result":{}}', b'{"ok":true,"result":[]}',
                        b'{"ok":false,"code":"unavailable"}', b'{"ok":true}'):
            with self.subTest(payload=payload), patch.object(bridge.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=payload)):
                with self.assertRaises(bridge.PreviewUnavailable):
                    bridge.count_preview(b"image", "image/jpeg")
        with patch.object(bridge.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=b'{"ok":false,"code":"invalid_image"}')):
            with self.assertRaises(bridge.PreviewInputError):
                bridge.count_preview(b"image", "image/png")

    def test_invalid_mime_and_payload_rejected_without_worker(self):
        for payload, mime in ((b"image", "image/gif"), (b"", "image/jpeg"),
                              ("file:///tmp/image", "image/jpeg"), ({"url": "https://example.invalid"}, "image/png"),
                              (bytearray(b"image"), "image/png"), (b"x"*(bridge.MAX_IMAGE_BYTES+1), "image/png")):
            with self.subTest(payload_type=type(payload).__name__, mime=mime), patch.object(bridge, "_invoke") as invoke:
                with self.assertRaises(bridge.PreviewInputError):
                    bridge.count_preview(payload, mime)
            invoke.assert_not_called()

    def test_busy_guard_does_not_queue_another_subprocess(self):
        lock = Mock()
        lock.acquire.return_value = False
        with patch.object(bridge, "_LOCK", lock), patch.object(bridge, "_invoke") as invoke:
            with self.assertRaises(bridge.PreviewBusy):
                bridge.count_preview(b"image", "image/jpeg")
        lock.acquire.assert_called_once_with(blocking=False)
        lock.release.assert_not_called()
        invoke.assert_not_called()

    def test_failed_inference_releases_single_preview_guard(self):
        with patch.object(bridge, "_invoke", side_effect=[bridge.PreviewUnavailable("failed"), valid_result()]):
            with self.assertRaises(bridge.PreviewUnavailable):
                bridge.count_preview(b"image", "image/jpeg")
            self.assertEqual(bridge.count_preview(b"next", "image/jpeg")["vehicle_count"], 1)

    def test_research_scope_and_retention_flags_cannot_be_upgraded_by_worker(self):
        value = valid_result()
        value.update(experimental=False, production_eligible=True, batam_validated=True,
                     image_retained=True, scope="production", unexpected=float("nan"),
                     model_path="/private/model.pt", training_activated=True)
        value["detections"][0].update(unexpected=float("nan"), box_xyxy=[float("nan")]*4)
        with patch.object(bridge, "_invoke", return_value=value):
            result = bridge.count_preview(b"image", "image/jpeg")
        self.assertTrue(result["experimental"])
        for flag in ("production_eligible", "batam_validated", "image_retained"):
            self.assertFalse(result[flag])
        self.assertEqual(result["scope"], "single_image_vehicle_count_preview")
        for field in ("unexpected", "model_path", "training_activated"):
            self.assertNotIn(field, result)
        self.assertEqual(set(result["detections"][0]), {"class_name", "score", "box"})
        json.dumps(result, allow_nan=False)

    def test_invalid_counts_dimensions_boxes_scores_labels_and_latency_are_rejected(self):
        invalid = []
        for count in (True, -1, 2, 1.0):
            v=valid_result();v["vehicle_count"]=count;invalid.append(v)
        for image in (None, {"width": True, "height": 2}, {"width": 6001, "height": 2},
                      {"width": 6000, "height": 6000}, {"width": 0, "height": 2}):
            v=valid_result();v["image"]=image;invalid.append(v)
        for box in (None, [0, 0, 0, 1], [0, 1, 1, 0], [0, 0, 1.1, 1],
                    [0, 0, float("nan"), 1], [True, 0, 1, 1], [0, 0, 1]):
            v=valid_result();v["detections"][0]["box"]=box;invalid.append(v)
        for score in (True, float("nan"), float("inf"), -1, 1.1, "0.9"):
            v=valid_result();v["detections"][0]["score"]=score;invalid.append(v)
        for label in ("person", "", None, [], {}):
            v=valid_result();v["detections"][0]["class_name"]=label;invalid.append(v)
        for latency in (True, -1, float("nan"), float("inf"), "fast"):
            v=valid_result();v["latency_ms"]=latency;invalid.append(v)
        for model_id in ("", [], "x"*129):
            v=valid_result();v["model_id"]=model_id;invalid.append(v)
        for index, value in enumerate(invalid):
            with self.subTest(case=index), patch.object(bridge, "_invoke", return_value=value):
                with self.assertRaises(bridge.PreviewUnavailable):
                    bridge.count_preview(b"image", "image/jpeg")

    def test_empty_and_supported_vehicle_detections_have_consistent_counts(self):
        for label in ("vehicle", "bicycle", "car", "motorcycle", "bus", "truck"):
            v=valid_result();v["detections"][0]["class_name"]=label
            with self.subTest(label=label), patch.object(bridge, "_invoke", return_value=v):
                self.assertEqual(bridge.count_preview(b"image", "image/jpeg")["vehicle_count"], 1)
        value=valid_result();value.update(vehicle_count=0, detections=[])
        with patch.object(bridge, "_invoke", return_value=value):
            self.assertEqual(bridge.count_preview(b"image", "image/jpeg")["vehicle_count"], 0)


if __name__ == "__main__":
    unittest.main()
