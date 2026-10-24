"""YOLO26 adapter boundaries; software fixtures do not establish Batam accuracy."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.yolo26_counter import (COCO_VEHICLES, SELECTION_FILE, YOLO26VehicleCounter,
                                     decode_image, postprocess_detections, runtime_model_id)


def selection(data):
    return {"schema": "crossflow.yolo26_vehicle_selection.v1", "architecture": "yolo26n", "ultralytics_version": "8.4.184",
            "model_sha256": hashlib.sha256(data).hexdigest(), "model_id": "yolo26n-fixture", "selected_model": "fixture",
            "production_eligible": False, "batam_validated": False, "human_certified": False, "annotation_origin": "ai_visual_blind",
            "min_vehicle_long_side_px": 12, "min_vehicle_short_side_px": 4, "imgsz": 640, "license": "AGPL-3.0",
            "score_threshold": .4, "num_classes": 80, "vehicle_classes": {str(k): v for k, v in COCO_VEHICLES.items()}}


class AdapterBoundaries(unittest.TestCase):
    def write_artifacts(self, directory, **changed):
        root = Path(directory)
        data = b"trusted-fixture" * 10_000
        (root / "model.pt").write_bytes(data)
        metadata = {**selection(data), **changed}
        (root / SELECTION_FILE).write_text(json.dumps(metadata))
        return root, metadata

    def test_disabled_by_default_rejects_before_decode_optional_import_or_weights(self):
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            YOLO26VehicleCounter().count_image(b"not-image")

    def test_missing_model_status_is_unavailable_without_optional_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            status = YOLO26VehicleCounter(directory, allow_experimental=True).status()
        self.assertEqual(status["status"], "unavailable")
        self.assertFalse(status["enabled"])
        self.assertFalse(status["batam_validated"])

    def test_status_discloses_unvalidated_and_license_scope_even_with_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _ = self.write_artifacts(directory)
            service = YOLO26VehicleCounter(root, allow_experimental=True)
            status = service.status()
            self.assertEqual(status["status"], "experimental_available")
            self.assertTrue(status["artifacts_available"])
            self.assertFalse(status["production_eligible"])
            self.assertEqual(status["license"], "AGPL-3.0")
            self.assertIsNone(service._model)

    def test_unsafe_metadata_and_changed_provenance_are_rejected(self):
        for changed in ({"num_classes": True}, {"vehicle_classes": {"0": "person"}}, {"score_threshold": float("nan")},
                        {"production_eligible": True}, {"human_certified": True}, {"model_id": "../escape"},
                        {"ultralytics_version": "latest"}, {"license": "commercial-clearance-assumed"}):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root, _ = self.write_artifacts(directory, **changed)
                self.assertEqual(YOLO26VehicleCounter(root, allow_experimental=True).status()["status"], "unavailable")

    def test_checkpoint_hash_is_checked_before_ultralytics_can_deserialize_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root, metadata = self.write_artifacts(directory)
            (root / "model.pt").write_bytes(b"tampered" * 20_000)
            service = YOLO26VehicleCounter(root, allow_experimental=True)
            with patch.dict("sys.modules", {"ultralytics": None}), self.assertRaisesRegex(RuntimeError, "integrity"):
                service._load(metadata)
            self.assertIsNone(service._model)

    def test_live_selection_change_requires_worker_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root, metadata = self.write_artifacts(directory)
            service = YOLO26VehicleCounter(root, allow_experimental=True)
            service._model = object()
            service._selection = metadata
            with self.assertRaisesRegex(RuntimeError, "restart"):
                service._load({**metadata, "model_sha256": "0" * 64})

    def test_runtime_identity_binds_threshold_resolution_and_vehicle_policy(self):
        metadata = selection(b"weights")
        initial = runtime_model_id(metadata)
        for changed in ({"score_threshold": .25}, {"imgsz": 1280}, {"num_classes": 1, "vehicle_classes": {"0": "vehicle"}}):
            self.assertNotEqual(initial, runtime_model_id({**metadata, **changed}))

    def test_image_byte_bounds_are_checked_before_pillow(self):
        for value in (b"", "not-bytes", b"x" * (4 * 1024 * 1024 + 1)):
            with self.subTest(value_type=type(value).__name__), patch.dict("sys.modules", {"PIL": None}), self.assertRaises(ValueError):
                decode_image(value)

    def test_generic_nms_merges_car_and_truck_boxes_and_excludes_person(self):
        boxes = [[10, 10, 40, 40], [11, 11, 39, 39], [50, 10, 70, 30], [0, 0, 10, 10]]
        result = postprocess_detections(boxes, [.9, .8, .7, .99], [2, 7, 3, 0], width=100, height=100,
                                        classes=COCO_VEHICLES, threshold=.4)
        self.assertEqual(len(result), 2)
        self.assertEqual([v["class_name"] for v in result], ["car", "motorcycle"])
        self.assertEqual(result[0]["box"], [.1, .1, .4, .4])

    def test_native_size_policy_not_confidence_controls_tiny_vehicle_exclusion(self):
        result = postprocess_detections([[0, 0, 11, 4], [0, 0, 12, 4], [20, 20, 30, 23]], [.99] * 3, [2] * 3,
                                        width=100, height=100, classes=COCO_VEHICLES, threshold=.4)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["box_xyxy"], [0., 0., 12., 4.])

    def test_roi_uses_normalized_centres_and_nonfinite_outputs_are_removed(self):
        result = postprocess_detections([[0, 0, 20, 20], [70, 70, 90, 90], [float("nan"), 0, 20, 20]], [.9] * 3, [2] * 3,
                                        width=100, height=100, classes=COCO_VEHICLES, threshold=.4,
                                        roi=[(0, 0), (.5, 0), (.5, .5), (0, .5)])
        self.assertEqual(len(result), 1)

    def test_box_lengths_and_max_detection_bounds_are_enforced(self):
        with self.assertRaises(ValueError):
            postprocess_detections([[0, 0, 20, 20]], [], [], width=100, height=100, classes=COCO_VEHICLES, threshold=.4)
        boxes = [[i * 20, 0, i * 20 + 12, 10] for i in range(110)]
        result = postprocess_detections(boxes, [.9] * 110, [2] * 110, width=3000, height=100, classes=COCO_VEHICLES, threshold=.4)
        self.assertEqual(len(result), 100)


class AdapterImageBoundaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PIL import Image
        except ImportError:
            raise unittest.SkipTest("Optional YOLO runtime requires Pillow")

    def test_corrupt_and_animated_images_are_rejected(self):
        from PIL import Image
        with self.assertRaises(ValueError):
            decode_image(b"not-image")
        data = io.BytesIO()
        Image.new("RGB", (20, 20)).save(data, "GIF")
        with self.assertRaises(ValueError):
            decode_image(data.getvalue())

    def test_safe_image_exif_orientation_is_applied(self):
        from PIL import Image
        data = io.BytesIO()
        image = Image.new("RGB", (20, 30), "red")
        exif = Image.Exif()
        exif[274] = 6
        image.save(data, "JPEG", exif=exif)
        decoded = decode_image(data.getvalue())
        self.assertEqual(decoded.size, (30, 20))

    def test_inference_contract_has_no_image_persistence_and_consistent_count(self):
        from PIL import Image
        class Values:
            def __init__(self, value): self.value = value
            def cpu(self): return self
            def tolist(self): return self.value
        class Boxes:
            xyxy, conf, cls = Values([[1., 1., 19., 19.]]), Values([.9]), Values([2.])
            def __len__(self): return 1
        class Result: boxes = Boxes()
        class Model:
            def predict(self, **kwargs):
                self.kwargs = kwargs
                return [Result()]
        with tempfile.TemporaryDirectory() as directory:
            root, metadata = AdapterBoundaries().write_artifacts(directory)
            service = YOLO26VehicleCounter(root, allow_experimental=True)
            service._model, service._selection = Model(), metadata
            data = io.BytesIO()
            Image.new("RGB", (20, 20), "red").save(data, "PNG")
            result = service.count_image(data.getvalue())
            self.assertEqual(result["vehicle_count"], len(result["detections"]))
            self.assertEqual(result["vehicle_count"], 1)
            self.assertFalse(service._model.kwargs["save"])
            self.assertFalse(service._model.kwargs["save_txt"])
            self.assertFalse(service._model.kwargs["save_crop"])
            self.assertFalse(result["production_eligible"])
            self.assertFalse(result["scores_are_calibrated_accuracy"])


if __name__ == "__main__":
    unittest.main()
