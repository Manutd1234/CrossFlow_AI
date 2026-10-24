"""Bounds, class/NMS semantics and disabled-default behavior for local preview."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from services.vehicle_counter import VehicleCounter, filter_detections, inside_roi, polygon_roi


class VehicleCounterBoundaryTests(unittest.TestCase):
    def test_status_works_without_loading_optional_detector_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            service = VehicleCounter(Path(directory))
            self.assertEqual(service.status()["status"], "unavailable")
            self.assertFalse(service.status()["production_eligible"])
            self.assertFalse(service.status()["batam_validated"])
            self.assertIsNone(service._model)

    def test_disabled_counter_rejects_before_optional_imports_or_decode(self):
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            VehicleCounter().count_image(b"invalid")

    def test_invalid_status_metadata_is_unavailable_without_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "model.pt").write_bytes(b"unused")
            (root / "selection_frozen_before_reused_diagnostic.json").write_text("[]")
            counter = VehicleCounter(root, allow_experimental=True)
            self.assertEqual(counter.status()["status"], "unavailable")
            self.assertIsNone(counter._model)

    def test_payload_bounds_reject_without_loading_weights(self):
        service = VehicleCounter(allow_experimental=True)
        for payload in (b"", "not bytes", b"x" * (8 * 1024 * 1024 + 1)):
            with self.subTest(payload_type=type(payload).__name__):
                with self.assertRaises(ValueError):
                    service.count_image(payload)
        self.assertIsNone(service._model)

    def test_roi_boundary_is_included_and_outside_excluded(self):
        roi = polygon_roi([[0, 0], [1, 0], [1, .5], [0, .5]])
        self.assertTrue(inside_roi(.5, .5, roi))
        self.assertTrue(inside_roi(.5, .4, roi))
        self.assertFalse(inside_roi(.5, .6, roi))

    def test_invalid_rois_are_rejected(self):
        for roi in ([], [[0, 0], [1, 1], [2, 0]], [[0, 0], [0, 0], [1, 1]],
                    [[0, 0], [1, 1], [0, 1], [1, 0]], [[True, 0], [1, 0], [0, 1]],
                    [[0, 0], [float("nan"), 1], [1, 0]]):
            with self.subTest(roi=roi):
                with self.assertRaises(ValueError):
                    polygon_roi(roi)


class VehicleCounterRuntimeTests(unittest.TestCase):
    def setUp(self):
        try:
            import torch
            import torchvision
            from PIL import Image
        except ImportError:
            self.skipTest("Optional detector runtime not installed")

    def test_generic_nms_merges_car_truck_overlap_and_excludes_person(self):
        import torch
        output = {"boxes": torch.tensor([[10., 10., 40., 40.], [11., 11., 39., 39.], [50., 50., 80., 80.], [0., 0., 5., 5.]]),
                  "scores": torch.tensor([.9, .8, .99, .1]), "labels": torch.tensor([3, 8, 1, 3])}
        result = filter_detections(output, width=100, height=100, threshold=.3, vehicle_classes={3: "car", 8: "truck"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["class_name"], "car")
        self.assertEqual(result[0]["box"], [.1, .1, .4, .4])

    def test_roi_filters_by_center_and_rejects_nonfinite_or_degenerate_boxes(self):
        import torch
        output = {"boxes": torch.tensor([[10., 10., 40., 40.], [50., 50., 80., 80.], [0., 0., 0., 3.], [0., 0., float("inf"), 3.]]),
                  "scores": torch.tensor([.9, .8, .8, .8]), "labels": torch.tensor([1, 1, 1, 1])}
        result = filter_detections(output, width=100, height=100, threshold=.3, vehicle_classes={1: "vehicle"},
            roi=polygon_roi([[0, 0], [.5, 0], [.5, .5], [0, .5]]))
        self.assertEqual(len(result), 1)

    def test_bad_or_excessive_dimensions_rejected_before_model_load(self):
        from PIL import Image
        service = VehicleCounter(allow_experimental=True)
        with self.assertRaises(ValueError):
            service.count_image(b"not an image")
        buffer = io.BytesIO()
        Image.new("RGB", (6001, 2)).save(buffer, format="PNG")
        with self.assertRaisesRegex(ValueError, "dimensions"):
            service.count_image(buffer.getvalue())
        self.assertIsNone(service._model)

    def test_tampered_local_checkpoint_is_rejected_before_deserialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "model.pt").write_bytes(b"tampered checkpoint")
            (root / "selection_frozen_before_reused_diagnostic.json").write_text(json.dumps({"model_sha256": "0" * 64}))
            with self.assertRaisesRegex(RuntimeError, "integrity"):
                VehicleCounter(root, allow_experimental=True)._load()


if __name__ == "__main__":
    unittest.main()
