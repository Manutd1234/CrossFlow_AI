"""Protect chronological evidence and geography in the offline benchmark."""

from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/backtest_academic_singapore.py"
spec = importlib.util.spec_from_file_location("academic_singapore_benchmark_test", SCRIPT)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class AcademicBenchmarkChronologyTests(unittest.TestCase):
    def setUp(self):
        # Explicit unit fixtures, never reported as real source observations.
        self.seconds = np.arange(1000, dtype=np.int64) * 300
        self.bands = np.tile(np.arange(1000) % 8 + 1, (2, 1)).astype(float)

    def test_final_forecast_labels_are_purged_before_later_partitions(self):
        origins = benchmark.valid_origins(self.bands, self.seconds)
        fit, calibration, test = benchmark.partitions(origins, self.seconds)
        self.assertLess(self.seconds[fit[-1]+12], self.seconds[calibration[0]])
        self.assertLess(self.seconds[calibration[-1]+12], self.seconds[test[0]])
        self.assertFalse(set(fit) & set(calibration) or set(calibration) & set(test))

    def test_missing_snapshot_windows_are_excluded_not_interpolated(self):
        self.seconds[500:] += 900
        origins = benchmark.valid_origins(self.bands, self.seconds)
        self.assertFalse(any(i-12 < 500 <= i+12 for i in origins))
        self.assertIn(450, origins)
        self.assertIn(550, origins)

    def test_future_labels_cannot_change_predictor_features(self):
        dates = [datetime(2021, 1, 1) + timedelta(seconds=int(s)) for s in self.seconds]
        array = np.stack([self.bands, np.zeros_like(self.bands), np.ones_like(self.bands)], axis=1)
        before, labels, _ = benchmark.examples(array, dates, np.array([100]))
        array[:, 0, 112] = 8 if labels[0, 2] != 8 else 1
        after, changed_labels, _ = benchmark.examples(array, dates, np.array([100]))
        np.testing.assert_array_equal(before, after)
        self.assertFalse(np.array_equal(labels, changed_labels))

    def test_invalid_ordinal_labels_and_nonmonotonic_clock_are_rejected(self):
        self.bands[0, 10] = 0
        with self.assertRaises(ValueError):
            benchmark.valid_origins(self.bands, self.seconds)
        self.bands[0, 10] = 1
        self.seconds[10] = self.seconds[9]
        with self.assertRaises(ValueError):
            benchmark.valid_origins(self.bands, self.seconds)

    def test_changed_source_is_rejected_before_numeric_deserialization(self):
        with TemporaryDirectory() as directory:
            (Path(directory) / "dataset.npy").write_bytes(b"not-reviewed-data")
            with self.assertRaisesRegex(ValueError, "checksum differs"):
                benchmark.load_source(Path(directory))


if __name__ == "__main__":
    unittest.main()
