"""Hotspot display pressure must use modelled congestion, not watch ranking."""
from datetime import datetime, timezone
import copy
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import live_traffic
from services.observed_conditions import pressure_index, pressure_band_condition

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
        self.assertEqual(zone["pressure_condition"], "SLOWING")
        self.assertEqual(zone["pressure_condition_policy"], "congestion_pressure_bands_v1")
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
        display_fields = {"pressure_index", "pressure_condition", "pressure_condition_policy"}
        self.assertEqual({key: value for key, value in ordinary.items() if key not in display_fields},
                         {key: value for key, value in modified.items() if key not in display_fields})

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

    def test_modelled_hotspot_pressure_bands_follow70_and80_boundaries(self):
        for pressure, condition in ((69.9, "SLOWING"), (70., "HEAVY"), (79.9, "HEAVY"), (80., "CRITICAL")):
            with self.subTest(pressure=pressure), patch.object(live_traffic, "pressure_index", return_value=pressure):
                zone = self.panbil()
                self.assertEqual(zone["pressure_condition"], condition)
                self.assertEqual(zone["congestion_index"], 10.2)
                self.assertEqual(zone["selection_score"], 45.5)

    def ranked_catalogue(self, rows):
        # A complete supplied snapshot needs neither a forecasting module nor
        # model inference. Keep this ordering test isolated from those stores.
        unused_forecast = Mock(side_effect=AssertionError("ranking requested a forecast"))
        with patch.dict(sys.modules, {"services.simulator": SimpleNamespace(get_live_corridor_telemetry=unused_forecast)}), \
                patch.object(live_traffic, "_score_map_hotspot", side_effect=lambda config, *_: copy.deepcopy(rows[config["zone_id"]])):
            result = live_traffic.get_corridor_map_hotspots(NOW, corridor_scores=self.signals())
        unused_forecast.assert_not_called()
        return result

    def ranking_rows(self):
        rows = {}
        for index, config in enumerate(live_traffic.CORRIDOR_MAP_HOTSPOTS_CONFIG):
            row = live_traffic._score_map_hotspot(config, NOW, self.signals())
            raw = float(index + 1)
            row.update(congestion_index=raw, pressure_index=pressure_index(raw),
                       pressure_condition=pressure_band_condition(pressure_index(raw)), selection_score=100. - index)
            rows[row["zone_id"]] = row
        return rows

    def test_display_pressure_ranks_descend_without_replacing_planning_ranks(self):
        rows = self.ranking_rows()
        result = self.ranked_catalogue(rows)
        self.assertEqual([row["pressure_index"] for row in result], sorted((row["pressure_index"] for row in result), reverse=True))
        self.assertEqual([row["pressure_rank"] for row in result], list(range(1, 31)))
        self.assertEqual(result[0]["selection_rank"], 30)
        self.assertEqual(result[0]["watch_priority"], "HEAVY")
        planning = sorted(result, key=lambda row: row["selection_rank"])
        self.assertEqual([row["selection_score"] for row in planning], sorted((row["selection_score"] for row in rows.values()), reverse=True))
        for row in planning:
            self.assertEqual(row["watch_priority"], "CRITICAL" if row["selection_rank"] <= 20 else "HEAVY")

    def test_saturated_pressure_ties_use_zone_id_instead_of_planning_rank(self):
        rows = self.ranking_rows()
        for row in rows.values():
            row.update(congestion_index=95., pressure_index=100., pressure_condition="CRITICAL")
        result = self.ranked_catalogue(rows)
        self.assertEqual([row["zone_id"] for row in result], sorted(rows))
        self.assertEqual([row["pressure_rank"] for row in result], list(range(1, 31)))

    def test_missing_invalid_and_incoherent_pressure_metadata_is_unranked_and_last(self):
        rows = self.ranking_rows()
        ids = sorted(rows)
        invalid = [None, True, float("nan"), "70", 101.]
        for zone_id, value in zip(ids, invalid):
            rows[zone_id]["pressure_index"] = value
        rows[ids[5]].pop("pressure_index")
        rows[ids[6]]["pressure_policy"] = None
        rows[ids[7]]["pressure_condition"] = "CRITICAL"
        unknown = set(ids[:8])
        result = self.ranked_catalogue(rows)
        self.assertEqual({row["zone_id"] for row in result[-8:]}, unknown)
        self.assertEqual([row["zone_id"] for row in result[-8:]], sorted(unknown))
        self.assertTrue(all(row["pressure_rank"] is None for row in result[-8:]))
        self.assertEqual([row["pressure_rank"] for row in result[:-8]], list(range(1, 23)))


if __name__ == "__main__":
    unittest.main()
