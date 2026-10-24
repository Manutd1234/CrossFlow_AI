"""Display-pressure policy must preserve missingness and bounded index units."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.observed_conditions import (
    PRESSURE_MULTIPLIER, PRESSURE_POLICY, PRESSURE_CONDITION_POLICY,
    pressure_band_condition, pressure_index,
)


class ObservedPressureTests(unittest.TestCase):
    def test_pressure_multiplier_and_saturation_are_explicit(self):
        self.assertEqual(PRESSURE_POLICY, "congestion_pressure_v1")
        self.assertEqual(PRESSURE_MULTIPLIER, 2.5)
        for raw, expected in ((0, 0), (15, 37.5), (20, 50), (40, 100), (95, 100), (100, 100)):
            with self.subTest(raw=raw):
                self.assertEqual(pressure_index(raw), expected)

    def test_missing_or_invalid_pressure_is_never_substituted_with_zero(self):
        for raw in (None, True, False, "15", float("nan"), float("inf"), -1, 101):
            with self.subTest(raw=raw):
                self.assertIsNone(pressure_index(raw))

    def test_warning_bands_follow_the_published_pressure_scale(self):
        self.assertEqual(PRESSURE_CONDITION_POLICY, "congestion_pressure_bands_v1")
        for pressure, condition in (
            (0., "FREE_FLOW"), (5., "FREE_FLOW"), (5.1, "SLOWING"),
            (25.5, "SLOWING"), (69.9, "SLOWING"), (70., "HEAVY"),
            (79.9, "HEAVY"), (80., "CRITICAL"), (100., "CRITICAL"),
        ):
            with self.subTest(pressure=pressure):
                self.assertEqual(pressure_band_condition(pressure), condition)
        for invalid in (None, True, "80", float("nan"), -1, 101):
            with self.subTest(invalid=invalid):
                self.assertEqual(pressure_band_condition(invalid), "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
