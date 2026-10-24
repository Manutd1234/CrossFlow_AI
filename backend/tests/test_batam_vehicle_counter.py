"""Pilot decoding stays inherited; policy and release checks require no torch."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.batam_vehicle_counter import BatamVehicleCounter, apply_count_policy
from services.vehicle_counter import VehicleCounter
from services import vehicle_count_preview as bridge


def selection():
    return {"schema": "crossflow.batam_vehicle_selection.v1", "architecture": "fasterrcnn_resnet50_fpn_v2",
            "num_classes": 3, "min_vehicle_long_side_px": 12, "min_vehicle_short_side_px": 4,
            "model_sha256": "a" * 64, "score_threshold": .4, "vehicle_nms_iou": .5, "vehicle_classes": {"1": "vehicle"},
            "annotation_origin": "ai_visual_blind", "human_certified": False,
            "production_eligible": False, "batam_validated": False}


class BatamVehicleCounterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.write_selection(selection())
        self.counter = BatamVehicleCounter(self.root, allow_experimental=True)

    def tearDown(self):
        self.directory.cleanup()

    def write_selection(self, value):
        data = json.dumps(value)
        for name in ("selection_frozen_before_batam_test.json", "selection_frozen_before_reused_diagnostic.json"):
            (self.root / name).write_text(data)

    def test_native_size_boundary_keeps_horizontal_and_vertical_boxes(self):
        rows = [{"box_xyxy": b} for b in ([0, 0, 12, 4], [0, 0, 4, 12], [0, 0, 11, 11], [0, 0, 3, 30])]
        self.assertEqual(apply_count_policy(rows), rows[:2])

    def test_invalid_extents_and_nonfinite_coordinates_fail_closed(self):
        for box in ([0, 0, 0, 12], [12, 0, 0, 4], [0, 0, float("nan"), 4], [0, 0, True, 14], [0, 0, 14]):
            with self.subTest(box=box), self.assertRaises(RuntimeError):
                apply_count_policy([{"box_xyxy": box}])

    def test_count_updates_boxes_without_changing_model_or_image(self):
        rows = [{"box_xyxy": [0, 0, 12, 4], "score": .8}, {"box_xyxy": [0, 0, 3, 30], "score": .9}]
        result = {"detections": rows, "vehicle_count": 2, "model_sha256": "frozen", "image": {"width": 100, "height": 50}}
        with patch.object(VehicleCounter, "count_image", return_value=result) as base:
            value = self.counter.count_image(b"image", roi=None)
        base.assert_called_once_with(b"image", roi=None)
        self.assertEqual(value["vehicle_count"], 1)
        self.assertEqual(value["detections"], rows[:1])
        self.assertEqual(value["model_sha256"], "frozen")
        self.assertEqual(value["image"], result["image"])
        self.assertFalse(value["human_certified"])
        self.assertFalse(value["production_eligible"])
        self.assertFalse(value["batam_validated"])

    def test_disabled_counter_rejects_before_reading_policy_or_loading(self):
        with patch.object(BatamVehicleCounter, "_pilot_metadata") as metadata, self.assertRaises(RuntimeError):
            BatamVehicleCounter(self.root).count_image(b"image")
        metadata.assert_not_called()

    def test_decoder_or_integrity_errors_propagate_without_substitute_count(self):
        with patch.object(VehicleCounter, "count_image", side_effect=ValueError("decode")), self.assertRaises(ValueError):
            self.counter.count_image(b"invalid")

    def test_wrong_annotation_origin_or_claims_reject_before_model(self):
        for key, value in (("human_certified", True), ("production_eligible", True), ("batam_validated", True),
                           ("annotation_origin", "detector_pseudo_labels"), ("min_vehicle_long_side_px", 1),
                           ("min_vehicle_short_side_px", True), ("schema", "unknown"),
                           ("score_threshold", float("nan")), ("vehicle_nms_iou", float("inf")),
                           ("score_threshold", True), ("num_classes", 3.0), ("model_sha256", "bad"),
                           ("vehicle_classes", {"1": "pedestrian"})):
            data = selection(); data[key] = value; self.write_selection(data)
            with self.subTest(key=key), patch.object(VehicleCounter, "count_image") as base, self.assertRaises(RuntimeError):
                self.counter.count_image(b"image")
            base.assert_not_called()

    def test_alias_changes_and_oversized_metadata_are_unavailable(self):
        alias = self.root / "selection_frozen_before_reused_diagnostic.json"
        for data in ("{}", " " * (128 * 1024 + 1)):
            alias.write_text(data)
            self.assertEqual(self.counter.status()["status"], "unavailable")
            self.assertFalse(self.counter.status()["enabled"])

    def test_valid_status_keeps_disabled_and_research_scope(self):
        with patch.object(VehicleCounter, "status", return_value={"status": "disabled", "enabled": False}):
            value = self.counter.status()
        self.assertEqual(value["status"], "disabled")
        self.assertIn("assistant-reviewed", value["scope"])
        self.assertFalse(value["supports_queue_length"])


class PreviewReleaseTests(unittest.TestCase):
    def test_only_explicit_safe_pilot_receipt_can_change_default(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(bridge.os.environ, {}, clear=True):
            release = Path(directory) / "release.json"
            with patch.object(bridge, "PREVIEW_RELEASE_PATH", release):
                self.assertEqual(bridge.configuration()[1].name, "detector_training")
                data = {"schema": "crossflow.vehicle_preview_release.v1", "model_directory": "batam_detector_training",
                        "preview_gate_passed": True, "production_eligible": False, "batam_validated": False}
                release.write_text(json.dumps(data))
                self.assertEqual(bridge.configuration()[1].name, "batam_detector_training")
                for key, value in (("model_directory", "../../other"), ("preview_gate_passed", False), ("production_eligible", True)):
                    invalid = {**data, key: value}; release.write_text(json.dumps(invalid))
                    self.assertEqual(bridge.configuration()[1].name, "detector_training")
                release.write_text("bad json")
                self.assertEqual(bridge.configuration()[1].name, "detector_training")

    def test_server_configuration_override_is_preserved(self):
        with patch.dict(bridge.os.environ, {"CROSSFLOW_VISION_MODEL_DIR": "backend/data/research/detector_training"}):
            self.assertEqual(bridge.configuration()[1], bridge.ROOT / "backend/data/research/detector_training")


class PilotPreviewContractTests(unittest.TestCase):
    def sample(self):
        return {"vehicle_count": 1, "image": {"width": 100, "height": 100},
                "detections": [{"class_name": "vehicle", "score": .8, "box": [0, 0, .5, .5]}],
                "count_policy": {"min_long_side_px": 12, "min_short_side_px": 4,
                                 "scope": "size_filtered_visible_vehicle_boxes_per_image"}}

    def test_policy_and_noncertification_are_visible_in_app_note(self):
        with patch.object(bridge, "_invoke", return_value=self.sample()):
            value = bridge.count_preview(b"image", "image/jpeg")
        self.assertIn("12 pixels", value["note"])
        self.assertIn("not human-certified", value["note"])
        self.assertFalse(value["batam_validated"])
        self.assertFalse(value["image_retained"])
        self.assertEqual(value["count_policy"]["min_short_side_px"], 4)

    def test_unknown_policy_does_not_silently_change_count_definition(self):
        sample = self.sample(); sample["count_policy"]["min_long_side_px"] = 1
        with patch.object(bridge, "_invoke", return_value=sample), self.assertRaises(bridge.PreviewUnavailable):
            bridge.count_preview(b"image", "image/jpeg")

    def test_truncation_warning_survives_post_cap_size_filter(self):
        sample = self.sample(); sample.update(may_be_truncated=True, max_detections=100)
        with patch.object(bridge, "_invoke", return_value=sample):
            value = bridge.count_preview(b"image", "image/jpeg")
        self.assertEqual(value["vehicle_count"], 1)
        self.assertTrue(value["may_be_truncated"])
        self.assertIn("100-box limit", value["note"])


class WorkerCounterSelectionTests(unittest.TestCase):
    def test_missing_pilot_marker_never_downgrades_to_unfiltered_service(self):
        import importlib.util
        path = Path(__file__).resolve().parents[2] / "scripts/vehicle_counter_worker.py"
        spec = importlib.util.spec_from_file_location("pilot_worker_test", path)
        worker = importlib.util.module_from_spec(spec); spec.loader.exec_module(worker)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "custom_pilot_name"; root.mkdir()
            (root / "selection_frozen_before_reused_diagnostic.json").write_text(json.dumps(selection()))
            counter = worker.counter_for_model_dir(root)
            self.assertIsInstance(counter, BatamVehicleCounter)
            self.assertFalse(counter.status()["enabled"])
            with self.assertRaises(RuntimeError):
                counter.count_image(b"image")


if __name__ == "__main__":
    unittest.main()
