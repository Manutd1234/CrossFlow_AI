"""Observed-only fitting, causal weather, numeric artifacts and runtime guards."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

import numpy as np
from fastapi import Response, HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('CROSSFLOW_HISTORY_DB', ':memory:')
from services import real_traffic_model as model, live_weather as weather
from services.traffic_observations import SpatialTrafficObservation

NOW = datetime(2026, 10, 24, 0, 0, tzinfo=timezone.utc)


def observations():
    rows = []
    for slot in range(49):
        stamp = NOW - timedelta(hours=12) + timedelta(minutes=slot * 15)
        for corridor in range(1, 6):
            rows.append(SpatialTrafficObservation.create(corridor_id=f'corridor-{corridor}',
                latitude=1.10 + corridor * .005, longitude=104.02,
                observed_at=stamp, actual_speed_kph=25 + (slot % 5) + corridor,
                free_flow_speed_kph=50, source='loop_sensor', validation_now=NOW))
    return rows, {row.observation_key: row.observed_at + timedelta(seconds=2) for row in rows}


class RealTrafficTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.available = observations()
        cls.payload = model.train(cls.rows, cls.available, now=NOW + timedelta(minutes=1), weather_archive=([], []))

    def test_real_rows_only_and_purged_partitions_remain_distinct(self):
        report = self.payload['report']
        self.assertEqual(report['synthetic_rows'], 0)
        self.assertEqual(report['source_counts'], {'loop_sensor': 245})
        self.assertFalse(report['quality_gate']['eligible'])
        windows = report['partitions']
        for before, after in (('fit', 'validation'), ('validation', 'calibration'), ('calibration', 'holdout')):
            self.assertLess(datetime.fromisoformat(windows[before]['end_at']), datetime.fromisoformat(windows[after]['start_at']) - timedelta(minutes=60))
        self.assertEqual(report['weather_training_rows'], 0)
        self.assertEqual(len(self.payload['models']), 3)
        self.assertTrue(all(len(value['trees']) == 100 for value in self.payload['models'].values()))

    def test_holdout_labels_never_change_forests_selection_or_interval_calibration(self):
        cutoff = datetime.fromisoformat(self.payload['report']['partitions']['holdout']['start_at'])
        changed = [SpatialTrafficObservation.create(corridor_id=row.corridor_id, latitude=row.latitude, longitude=row.longitude,
                   observed_at=row.observed_at, actual_speed_kph=1, free_flow_speed_kph=row.free_flow_speed_kph,
                   source=row.source, validation_now=NOW) if row.observed_at >= cutoff else row for row in self.rows]
        availability = {new.observation_key: self.available[old.observation_key] for new, old in zip(changed, self.rows)}
        altered = model.train(changed, availability, now=NOW + timedelta(minutes=1), weather_archive=([], []))
        self.assertEqual(self.payload['models'], altered['models'])
        self.assertNotEqual(self.payload['report']['horizons'][0]['test'], altered['report']['horizons'][0]['test'])

    def test_nonobserved_and_future_inputs_cannot_become_training_rows(self):
        fake = SpatialTrafficObservation.create(corridor_id='corridor-1', latitude=1.12, longitude=104.02,
            observed_at=NOW, actual_speed_kph=1, free_flow_speed_kph=50, source='synthetic', validation_now=NOW)
        report = model.train(self.rows + [fake], self.available | {fake.observation_key: NOW},
                             now=NOW + timedelta(minutes=1), weather_archive=([], []))['report']
        self.assertEqual(report['selected_rows'], self.payload['report']['selected_rows'])
        late = {key: NOW + timedelta(days=1) for key in self.available}
        with self.assertRaises(ValueError):
            model.train(self.rows, late, now=NOW, weather_archive=([], []))

    def test_data_only_artifact_roundtrip_and_corruption_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'model.json'
            model.save_artifact(self.payload, path)
            restored, forests = model.load_artifact(path)
            for horizon in model.HORIZONS:
                actual = forests[str(horizon)].predict(np.zeros((2, len(model.FEATURE_NAMES))))
                self.assertTrue(np.isfinite(actual).all())
                self.assertEqual(len(forests[str(horizon)].estimators_), 100)
            self.assertEqual(restored['report']['synthetic_rows'], 0)
            envelope = json.loads(path.read_text()); envelope['payload']['report']['synthetic_rows'] = 1
            path.write_text(json.dumps(envelope))
            with self.assertRaises(ValueError): model.load_artifact(path)

    def test_forecast_requires_fresh_measurements_and_withholds_sparse_intervals(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'model.json'; model.save_artifact(self.payload, path)
            payload, forests = model.load_artifact(path)
            now = NOW + timedelta(minutes=1)
            with patch.object(model, '_model', return_value=(payload, forests)), \
                    patch.object(model, 'snapshot', return_value=(self.rows, self.available, {})), \
                    patch.object(weather, 'get_weather', return_value={'status': 'unavailable', 'current': None}), \
                    patch.object(weather, 'history', return_value=([], [])):
                value = model.forecast(now)
                self.assertEqual(len(value['points']), 5)
                self.assertTrue(all(row['lower'] is None for row in value['network']))
                self.assertTrue(all(row['lower'] is None for point in value['points'] for row in point['forecasts']))
                self.assertTrue(all(row['strategy'] == 'persistence' for point in value['points'] for row in point['forecasts']))
                self.assertEqual([row['horizon_minutes'] for row in value['network']], [15, 30, 60])
                self.assertTrue(all(row['strategy'] == 'persistence' for row in value['network']))
                self.assertEqual(value['coverage']['expected_points'], len(model.CORRIDORS))
                self.assertEqual(value['coverage']['configured_point_ids'], list(model.CORRIDORS))
                self.assertEqual(value['coverage']['sampling_scope'], 'representative_provider_points')
                with self.assertRaises(ValueError): model.forecast(now + timedelta(minutes=10))

    def test_forced_baseline_does_not_reuse_intervals_calibrated_for_a_different_serving_model(self):
        payload = copy.deepcopy(self.payload)
        forests = {}
        for horizon in model.HORIZONS:
            payload['models'][str(horizon)].update(strategy='random_forest', radius=4, network_radius=3, target_transform='absolute')
            evidence = next(row for row in payload['report']['horizons'] if row['horizon_minutes'] == horizon)
            evidence['interval'].update(calibration_pairs=100, network_calibration_groups=30,
                                        network_test_groups=30, network_test_coverage=.8)
            evidence['test']['samples'] = 100
            forests[str(horizon)] = SimpleNamespace(predict=Mock(return_value=np.asarray([12.345])), estimators_=[None] * 100)
        payload['report']['quality_gate']['eligible'] = False
        with patch.dict(os.environ, {'CROSSFLOW_REAL_TRAFFIC_TRAINING': '0'}), \
                patch.object(model, '_model', return_value=(payload, forests)), \
                patch.object(model, 'snapshot', return_value=(self.rows, self.available, {})), \
                patch.object(weather, 'get_weather', return_value={'status': 'unavailable', 'current': None}), \
                patch.object(weather, 'history', return_value=([], [])):
            value = model.forecast(NOW + timedelta(minutes=1))
        self.assertTrue(all(row['lower'] is None and row['upper'] is None for row in value['network']))
        for point in value['points']:
            for row in point['forecasts']:
                self.assertEqual(row['strategy'], 'persistence')
                self.assertEqual(row['mean_score'], point['current_index'])
                self.assertEqual(row['forest_candidate_score'], 12.345)
                self.assertIsNone(row['lower'])
                self.assertIsNone(row['upper'])
        self.assertTrue(all(forest.predict.call_count == len(model.CORRIDORS) for forest in forests.values()))

    def test_operator_guard_prevents_forecast_work(self):
        import main
        with patch.object(main.identity, 'require_route_operator', side_effect=HTTPException(403, 'Denied')), \
                patch.object(main.real_traffic_model, 'forecast') as compute:
            with self.assertRaises(HTTPException): main.api_real_forecast(Response(), 'Bearer fixture')
            compute.assert_not_called()

    def test_observed_only_mode_bypasses_synthetic_trees_and_assumed_ferry_adjustment(self):
        from models.congestion_model import forecaster, SpatialPredictionContext
        from services import observed_runtime_adapter as adapter
        now = datetime.now(timezone.utc)
        local = now + timedelta(hours=7)
        context = SpatialPredictionContext.from_legacy(local.hour + local.minute / 60, 0, 2, 1, 0)
        value = {'points': [{'corridor_id': 'corridor-1', 'observed_at': now.isoformat(),
                             'current_index': 20, 'forecasts': []}]}
        with patch.dict(os.environ, {'CROSSFLOW_REAL_TRAFFIC_TRAINING': '1'}), \
                patch.object(adapter, '_cache', None), patch.object(model, 'forecast', return_value=value), \
                patch.object(forecaster, '_tree_predictions', side_effect=AssertionError('Synthetic inference must not run')):
            result = forecaster.predict_spatial(context)
        self.assertEqual(result.mean_score, 20)
        self.assertEqual(result.ferry_surge_adjustment_points, 0)
        self.assertEqual(result.model_id, 'crossflow_observed_short_horizon_v1')
        self.assertIsNone(result.router_payload(free_flow_travel_seconds=100)['p90_speed_ratio'])

    def test_observed_corridor_zero_has_no_synthetic_drift_or_minimum_floor(self):
        from services import simulator
        prediction = {'current_score': 0., 'predicted_30min': 0., 'predicted_60min': 0., 'trend': 'STABLE'}
        with patch.dict(os.environ, {'CROSSFLOW_REAL_TRAFFIC_TRAINING': '1'}), \
                patch.object(simulator.forecaster, 'predict_continuous', return_value=prediction), \
                patch.object(simulator.ferry_schedule, 'ferry_surge_for_port', return_value=(0, None)), \
                patch.object(simulator.drift, 'corridor_drift', side_effect=AssertionError('Artificial drift must not run')):
            rows = simulator.get_live_corridor_telemetry(NOW, include_route_distance=False)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row['live_congestion_score'] == 0 for row in rows))

    def test_legacy_synthetic_scenario_is_disabled_in_observed_only_mode(self):
        import main
        with patch.dict(os.environ, {'CROSSFLOW_REAL_TRAFFIC_TRAINING': '1'}), \
                patch.object(main.identity, 'require_route_operator'), patch.object(main, 'simulate_analytics') as legacy:
            with self.assertRaises(HTTPException) as error: main.api_analytics_simulation(Response())
            self.assertEqual(error.exception.status_code, 409)
            legacy.assert_not_called()


class WeatherTests(unittest.TestCase):
    def test_provider_units_freshness_geography_and_numeric_bounds_are_checked(self):
        units = {'temperature_2m': '°C', 'precipitation': 'mm', 'wind_speed_10m': 'km/h'}
        current = {'time': int(NOW.timestamp()), 'weather_code': 3, 'precipitation': 0, 'temperature_2m': 27, 'wind_speed_10m': 5}
        value = {'latitude': 1.12, 'longitude': 104.04, 'utc_offset_seconds': 0,
                 'current_units': units, 'hourly_units': units, 'current': current,
                 'hourly': {'time': [int(NOW.timestamp()) + i * 3600 for i in range(48)],
                            **{name: [current[name]] * 48 for name in ('weather_code', 'precipitation', 'temperature_2m', 'wind_speed_10m')}}}
        self.assertEqual(weather.parse(value, NOW)['status'], 'fresh')
        for change in ('units', 'stale', 'foreign', 'invalid'):
            altered = copy.deepcopy(value)
            if change == 'units': altered['current_units']['wind_speed_10m'] = 'm/s'
            if change == 'stale': altered['current']['time'] -= 3600
            if change == 'foreign': altered['latitude'] = 20
            if change == 'invalid': altered['current']['temperature_2m'] = float('nan')
            with self.subTest(change=change), self.assertRaises(ValueError): weather.parse(altered, NOW)

    def test_weather_captured_later_cannot_become_an_earlier_feature(self):
        row = {'captured_at': NOW.isoformat(), 'valid_at': (NOW - timedelta(minutes=15)).isoformat(),
               'weather_class': 1, 'precipitation_mm': 2, 'temperature_c': 27, 'wind_kph': 5}
        archive = ([NOW], [row])
        self.assertIsNone(weather.asof(NOW - timedelta(seconds=1), archive))
        self.assertEqual(weather.asof(NOW, archive)['weather_class'], 1)
        self.assertIsNone(weather.asof(NOW + timedelta(hours=2), archive))
        self.assertEqual(weather.feature_vector(None, NOW)[4], 1)

    def test_only_forecasts_available_at_origin_enter_future_weather_features(self):
        row = {'captured_at': NOW.isoformat(), 'valid_at': NOW.isoformat(), 'weather_class': 0,
               'precipitation_mm': 0, 'temperature_c': 27, 'wind_kph': 5}
        forecast = {**row, 'valid_at': (NOW + timedelta(hours=1)).isoformat(), 'weather_class': 2}
        archive = ([NOW], [{**row, 'hourly': [forecast]}])
        value = weather.asof(NOW, archive, target=NOW + timedelta(hours=1))
        self.assertTrue(value['forecast_used'])
        self.assertEqual(value['weather_class'], 2)
        self.assertEqual(weather.feature_vector(value, NOW)[6], 60)
        self.assertIsNone(weather.asof(NOW - timedelta(minutes=1), archive, target=NOW + timedelta(hours=1)))


if __name__ == '__main__': unittest.main()
