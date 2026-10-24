"""Frozen-source and validation-selection gates for the new YOLO26 experiment."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import train_yolo26_batam as training


class YOLO26ProtocolTests(unittest.TestCase):
    def setUp(self):
        # Release/CI checkouts deliberately omit optional local photos and
        # checkpoints. Keep the protocol gates active using artificial
        # metadata; no pixels are decoded and no model is trained here.
        folder = tempfile.TemporaryDirectory(prefix="crossflow-yolo-protocol-")
        self.addCleanup(folder.cleanup)
        self.source = Path(folder.name)
        sha = lambda value: hashlib.sha256(value).hexdigest()
        policy = self.source / "policy.json"
        policy.write_text(json.dumps({"scope": "Artificial unit-test split, not Batam accuracy evidence"}))
        images, files = [], {}
        for index, partition in enumerate(("train", "validation", "test")):
            image_id = f"artificial-{partition}"
            images.append({"image_id": image_id, "path": f"{partition}.jpg",
                "image_sha256": sha(f"artificial image {index}".encode()),
                "decoded_sha256": sha(f"artificial pixels {index}".encode()),
                "width": 100, "height": 100, "source_id": f"fixture-source-{index}",
                "source_group": f"fixture-group-{index}", "photographer": f"fixture-author-{index}",
                "split": partition, "geography": "Batam", "training_eligible": True,
                "license": "CC0-1.0 synthetic fixture only", "source_url": "https://example.org/artificial-fixture"})
            annotations = self.source / f"{partition}.json"
            annotations.write_text(json.dumps({"annotation_origin": "ai_visual_blind", "human_certified": False,
                "images": [{"image_id": image_id, "decision": "include", "boxes_xyxy": [[.1, .1, .4, .4]]}]}))
            files[partition] = {"path": annotations.name, "sha256": training.digest(annotations)}
        split = self.source / "split.json"
        split.write_text(json.dumps({"schema": "crossflow.batam_vehicle_split.v1", "images": images,
            "split_policy": {"path": policy.name, "sha256": training.digest(policy)}}))
        manifest = self.source / "annotation_manifest.json"
        manifest.write_text(json.dumps({"schema": "crossflow.batam_vehicle_annotations.v1",
            "split_sha256": training.digest(split), "annotation_origin": "ai_visual_blind",
            "human_certified": False, "files": files}))
        self.protocol = training.load_protocol(split, manifest)
        self.first = training.OUTPUT

    def local_pilot_protocol(self, *, checkpoint=False):
        required = [training.SOURCE / "split_v2.json", training.SOURCE / "annotation_manifest.json"]
        if checkpoint:
            required.extend(self.first / name for name in (
                training.SELECTION_FILE, "training_plan.json", "validation_grid.json", "model.pt"))
        if any(not path.is_file() for path in required):
            self.skipTest("Optional local Batam photo pilot/checkpoint is absent; hermetic protocol checks still run")
        # Present but malformed local artifacts must fail rather than skip.
        return training.load_protocol(required[0], required[1])

    def test_development_metadata_does_not_open_test_labels(self):
        test = (self.source / "test.json").resolve()
        def guard(original):
            def read(path, *args, **kwargs):
                if path.resolve() == test:
                    raise AssertionError("Test labels opened during development preparation")
                return original(path, *args, **kwargs)
            return read
        with patch.object(Path, "read_text", guard(Path.read_text)), patch.object(Path, "read_bytes", guard(Path.read_bytes)):
            protocol = training.load_protocol(self.protocol["split_path"], self.protocol["annotations_path"])
            train = training.load_annotations(protocol, "train")
            validation = training.load_annotations(protocol, "validation")
        self.assertEqual(len(train), 1)
        self.assertEqual(len(validation), 1)
        self.assertFalse({r["source_group"] for r in train} & {r["source_group"] for r in validation})

    def test_optional_local_pilot_retains_original_development_partition_counts(self):
        protocol = self.local_pilot_protocol()
        train, validation = training.load_annotations(protocol, "train"), training.load_annotations(protocol, "validation")
        self.assertEqual((len(train), len(validation)), (9, 3))
        self.assertFalse({r["source_group"] for r in train} & {r["source_group"] for r in validation})

    def test_test_cannot_open_before_validation_selection_exists(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises((OSError, ValueError)):
            training.load_reused_test(deepcopy(self.protocol), Path(directory))

    def test_test_data_cannot_enter_training_exports(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, "development partitions"):
            training.prepare_development_dataset(self.protocol, {"train": [], "test": []}, Path(directory))

    def test_selection_function_rejects_test_partition_metrics(self):
        with self.assertRaises(ValueError):
            training.select_validation([{"partition": "test"}])

    def test_native_box_policy_is_identical_for_offline_evaluation(self):
        raw = [{"boxes": [[0, 0, 12, 4], [0, 10, 11, 14]], "scores": [.9, .9], "classes": [2., 2.]}]
        rows = [{"width": 100, "height": 100}]
        result = training.filtered(raw, rows, training.COCO_VEHICLES, .4)
        self.assertEqual(len(result[0]), 1)
        self.assertEqual(result[0][0]["box_xyxy"], [0., 0., 12., 4.])

    def test_first_run_test_is_explicitly_reused_and_preserves_original_split(self):
        protocol = self.local_pilot_protocol(checkpoint=True)
        rows = training.load_reused_test(deepcopy(protocol), self.first)
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(len(row["vehicle_boxes"]) for row in rows), 18)
        self.assertTrue(all(row["split"] == "test" for row in rows))

    def test_weight_or_validation_receipt_tampering_prevents_test_access(self):
        for changed in ("model", "validation"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                candidate = root / "artificial-checkpoint.bin"
                candidate.write_bytes(b"Artificial integrity fixture; never deserialized")
                grid = [{"partition": "validation", "candidate": "fixture", "score_threshold": .4,
                         "metrics": {"count": {"macro_source_group_mae": 1.}, "detection_iou_50": {"f1": .5}}}]
                training.save(root / "training_plan.json", {"scope": "Artificial integrity fixture"})
                training.save(root / "validation_grid.json", grid)
                training.freeze_selection(self.protocol, root, grid[0], candidate, 80)
                self.assertEqual(len(training.load_reused_test(deepcopy(self.protocol), root)), 1)
                if changed == "model":
                    seal = json.loads((root / training.SELECTION_FILE).read_bytes())
                    seal["model_sha256"] = "0" * 64
                    (root / training.SELECTION_FILE).write_text(json.dumps(seal))
                else:
                    (root / "validation_grid.json").write_text("[]")
                with self.assertRaises(ValueError):
                    training.load_reused_test(deepcopy(self.protocol), root)

    def test_protocol_requires_retained_source_and_license_metadata(self):
        row = self.protocol["images"][0]
        self.assertEqual(row["geography"], "Batam")
        self.assertTrue(row["license"].startswith("CC"))
        self.assertTrue(row["source_url"].startswith("https://"))
        self.assertFalse(self.protocol["annotation_manifest"]["human_certified"])


if __name__ == "__main__":
    unittest.main()
