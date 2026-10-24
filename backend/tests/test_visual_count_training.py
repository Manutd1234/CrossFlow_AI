"""Targets and split boundaries for the research-only visual count diagnostic."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
try:
    from PIL import Image
except ModuleNotFoundError as error:
    if error.name == "PIL":
        raise unittest.SkipTest("Optional visual-training tests require Pillow; run with the documented vision Python.") from error
    raise

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/train_visual_counts.py"
SPEC = importlib.util.spec_from_file_location("crossflow_visual_count_training", SCRIPT)
vision = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = vision
SPEC.loader.exec_module(vision)


def record(group: str, name: str, image_hash: str, phash: str = "0000000000000000", dhash: str = "0000000000000000") -> dict:
    return {"image": name, "source_video_group": group, "image_sha256": image_hash,
            "decoded_rgb_sha256": image_hash, "phash": phash, "dhash": dhash}


class VisualCountTrainingTests(unittest.TestCase):
    def test_pedestrians_are_excluded_and_vehicles_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "label.txt"
            path.write_text("0 0.5 0.5 0.1 0.1\n1 0.5 0.5 0.1 0.1\n2 0.5 0.5 0.1 0.1\n3 0.5 0.5 0.1 0.1\n")
            self.assertEqual(vision.count_yolo(path, vehicle_class_ids={1, 2, 3}, all_class_ids={0, 1, 2, 3}), (3, 4))

    def test_unverified_class_or_invalid_box_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "label.txt"
            path.write_text("4 0.5 0.5 0.1 0.1\n")
            with self.assertRaises(ValueError):
                vision.count_yolo(path, vehicle_class_ids={1}, all_class_ids={0, 1})

    def test_pixel_features_are_finite_deterministic_and_fixed_length(self):
        image = Image.fromarray(np.arange(32 * 32 * 3, dtype=np.uint8).reshape(32, 32, 3))
        first, names = vision.pixel_features(image)
        second, other_names = vision.pixel_features(image.copy())
        np.testing.assert_array_equal(first, second)
        self.assertEqual(names, other_names)
        self.assertEqual(len(first), 352)
        self.assertTrue(np.isfinite(first).all())

    def test_duplicate_videos_stay_in_same_validation_component(self):
        training = [record("video_a", "a", "same"), record("video_b", "b", "same"),
                    record("video_c", "c", "other", "ffffffffffffffff", "ffffffffffffffff")]
        test = [record("not_released", "test", "third", "aaaaaaaaaaaaaaaa", "aaaaaaaaaaaaaaaa")]
        groups, keep, audit = vision.grouping_and_duplicate_audit(training, test)
        self.assertEqual(groups[0], groups[1])
        self.assertNotEqual(groups[0], groups[2])
        self.assertTrue(keep[0])
        self.assertEqual(audit["train_cross_video_merge_pairs"], 1)

    def test_test_similarity_excluded_without_using_count_targets(self):
        training = [record("video_a", "train", "same")]
        test = [record("not_released", "duplicate", "same"),
                record("not_released", "new", "new", "ffffffffffffffff", "ffffffffffffffff")]
        _, keep, audit = vision.grouping_and_duplicate_audit(training, test)
        np.testing.assert_array_equal(keep, [False, True])
        self.assertEqual(audit["test_images_excluded_for_training_similarity"], ["duplicate"])

    def test_perceptual_similarity_requires_both_hashes(self):
        self.assertFalse(vision.potential_duplicate(record("a", "a", "a"),
            record("b", "b", "b", "ffffffffffffffff", "0000000000000000")))


if __name__ == "__main__":
    unittest.main()
