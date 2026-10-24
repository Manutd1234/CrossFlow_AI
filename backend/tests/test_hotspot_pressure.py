"""Hotspot display pressure must use modelled congestion, not watch ranking."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import live_traffic

NOW = datetime(2026, 10, 24, 18, 30, tzinfo=timezone.utc).astimezone(live_traffic.clock.BATAM_TZ)  #01:30WIB; no peak.


class HotspotPressureTests(unittest.TestCase):
    def signals(self, score=0.):
        return {corridor: {
            "congestion_index": score, "source": "tomtom_live",
            "confidence": .9, "provider_confidence": 1.,
        } for corridor in live_traffic._CORRIDOR_POINTS}

    def panbil(self, score=0.):
        config = next(row for row in live_traffic.CORRIDOR_MAP_HOTSPOTS_CONFIG if row["zone_id"] == "zone-panbil")
        return live_traffic._score_map_hotspot(config, NOW, self.signals(score))

    def test_panbil_pressure_uses_raw_modelled_congestion_and_preserves_selection_score(self):
        zone = self.panbil()
        self.assertEqual(zone["selection_score"], 45.5)
        self.assertEqual(zone["congestion_index"], 10.2)
        self.assertEqual(zone["pressure_index"], 25.5)
        self.assertEqual(zone["pressure_multiplier"], 2.5)
        self.assertEqual(zone["pressure_policy"], "congestion_pressure_v1")
        self.assertEqual(zone["pressure_basis"], "modelled_congestion_index")
        self.assertEqual(zone["source"], "modelled_spatial_hotspot")
        self.assertFalse(zone["observed"])
        self.assertEqual(zone["level"], "SMOOTH")
        self.assertTrue(zone["routing_enabled"])
        self.assertEqual(zone["modeled_emissions_pressure"]["index"], 1.)
        self.assertEqual(zone["modeled_emissions_pressure"]["queue_pressure_factor"], .0104)

    def test_high_modelled_congestion_pressure_saturates_without_mutating_raw_or_ranking(self):
        zone = self.panbil(100.)
        self.assertEqual(zone["congestion_index"], 95.2)
        self.assertEqual(zone["pressure_index"], 100.)
        self.assertEqual(zone["selection_score"], 90.5)
        self.assertEqual(zone["score_breakdown"]["published_congestion_index"], 95.2)

    def test_pressure_display_does_not_enter_existing_modelled_scores_or_emissions(self):
        ordinary = self.panbil()
        with patch.object(live_traffic, "pressure_index", return_value=99.):
            modified = self.panbil()
        self.assertEqual(modified["pressure_index"], 99.)
        self.assertEqual({key: value for key, value in ordinary.items() if key != "pressure_index"},
                         {key: value for key, value in modified.items() if key != "pressure_index"})

    def test_every_catalogue_area_supplies_policy_and_modelled_basis_without_upstream_pressure(self):
        for config in live_traffic.CORRIDOR_MAP_HOTSPOTS_CONFIG:
            with self.subTest(zone=config["zone_id"]):
                zone = live_traffic._score_map_hotspot(config, NOW, self.signals())
                self.assertEqual(zone["pressure_policy"], "congestion_pressure_v1")
                self.assertEqual(zone["pressure_multiplier"], 2.5)
                self.assertEqual(zone["pressure_basis"], "modelled_congestion_index")
                self.assertGreaterEqual(zone["pressure_index"], 0.)
                self.assertLessEqual(zone["pressure_index"], 100.)
                self.assertFalse(zone["observed"])


if __name__ == "__main__":
    unittest.main()
