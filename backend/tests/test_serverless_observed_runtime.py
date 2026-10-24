"""Serverless uses genuine current inputs without publishing or writing history."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from services import real_traffic_model as model, live_weather as weather, forecast_evaluation as ledger

NOW = datetime(2026, 10, 24, 8, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


def segment(speed=25, **changed):
    return {'current_speed_kmh': speed, 'free_flow_speed_kmh': 50, 'confidence': .9,
            'snapshot_generated_at': NOW.isoformat(), **changed}


class ServerlessObservedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload, cls.forests = model.load_artifact(ROOT / 'backend/data/observed_runtime_model.json')

    def test_deployment_pins_observed_mode_and_data_only_artifact(self):
        config = json.loads((ROOT / 'vercel.json').read_text())
        self.assertEqual(config['env']['CROSSFLOW_REAL_TRAFFIC_TRAINING'], '1')
        self.assertEqual(config['env']['CROSSFLOW_CCTV_STORE'], 'supabase')
        artifact = ROOT / config['env']['CROSSFLOW_REAL_MODEL_PATH']
        manifest = json.loads((ROOT / 'backend/data/observed_runtime_model.manifest.json').read_text())
        import hashlib
        self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), manifest['sha256'])
        self.assertFalse(manifest['raw_history_included'])
        self.assertFalse(manifest['media_included'])
        self.assertEqual(self.payload['report']['synthetic_rows'], 0)
        self.assertFalse(self.payload['report']['quality_gate']['eligible'])

    def test_fresh_provider_inputs_serve_persistence_without_local_history_or_writes(self):
        from services import live_traffic, clock, traffic_archiver
        segments = {name: segment(10 + index * 5) for index, name in enumerate(model.CORRIDORS)}
        payload = deepcopy(self.payload)
        # Even a future promoted candidate cannot bypass missing causal lags
        # in this separate serverless serving policy.
        payload['report']['quality_gate']['eligible'] = True
        for entry in payload['models'].values():
            entry.update(strategy='random_forest', radius=5, network_radius=5)
        for entry in payload['report']['horizons']:
            entry['interval'].update(calibration_pairs=100, network_calibration_groups=100,
                                     network_test_groups=100, network_test_coverage=.9)
            entry['test']['samples'] = 100
        with patch.dict(os.environ, {'VERCEL': '1', 'CROSSFLOW_REAL_TRAFFIC_TRAINING': '1'}), \
             patch.object(model, '_model', return_value=(payload, self.forests)), \
             patch.object(live_traffic, '_tomtom_api_key', return_value='server-only-fixture'), \
             patch.object(live_traffic, '_tomtom_segments', return_value=segments), \
             patch.object(clock, 'now', return_value=NOW), \
             patch.object(model, 'snapshot', side_effect=AssertionError('No local history read')), \
             patch.object(traffic_archiver.ARCHIVER, 'collect_once', side_effect=AssertionError('No archive writes')), \
             patch.object(weather, 'get_weather', return_value={'status': 'unavailable', 'current': None}), \
             patch.object(weather, 'history', side_effect=AssertionError('No local weather archive read')):
            value = model.forecast(NOW)
        self.assertEqual(len(value['points']), 5)
        self.assertFalse(value['archive']['historical_archive_shared'])
        self.assertEqual(value['archive']['stored_rows'], 0)
        self.assertEqual(value['archive']['durability'], 'request_memory')
        for point in value['points']:
            for entry in point['forecasts']:
                self.assertEqual(entry['strategy'], 'persistence')
                self.assertEqual(entry['mean_score'], point['current_index'])
                self.assertIsNone(entry['lower'])
                self.assertIsNone(entry['upper'])
        self.assertTrue(all(entry['strategy'] == 'persistence' and entry['lower'] is None for entry in value['network']))

    def test_public_metrics_name_actual_cloud_strategy_and_disable_retraining(self):
        import main
        payload = deepcopy(self.payload)
        payload['report']['quality_gate']['eligible'] = True
        horizon = next(entry for entry in payload['report']['horizons'] if entry['horizon_minutes'] == 30)
        horizon['selected_strategy'] = 'random_forest'
        with patch.dict(os.environ, {'VERCEL': '1', 'CROSSFLOW_REAL_TRAFFIC_TRAINING': '1'}), \
             patch.object(model, 'status', return_value={'available': True, 'report': payload['report'], 'engine': {}}):
            metrics = main.api_model_status()['metrics']
        self.assertEqual(metrics['selected_strategy'], 'persistence')
        self.assertEqual(metrics['mae'], horizon['persistence_test']['mae_points'])
        self.assertFalse(metrics['retraining_enabled'])

    def test_stale_future_non_provider_and_invalid_samples_cannot_become_current_inputs(self):
        from services import live_traffic, clock
        segments = {'corridor-1': segment(snapshot_generated_at=(NOW - timedelta(seconds=181)).isoformat()),
                    'corridor-2': segment(snapshot_generated_at=(NOW + timedelta(seconds=1)).isoformat()),
                    'corridor-3': segment(source='cctv_camera'),
                    'corridor-4': segment(True), 'corridor-5': segment(0)}
        with patch.object(live_traffic, '_tomtom_api_key', return_value='fixture'), \
             patch.object(live_traffic, '_tomtom_segments', return_value=segments), \
             patch.object(clock, 'now', return_value=NOW):
            rows, _available, archive, _now = model._serverless_provider_rows(NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].corridor_id, 'corridor-5')
        self.assertEqual(rows[0].actual_speed_kph, 0)
        self.assertTrue(rows[0].observed)
        self.assertEqual(archive['loaded_rows'], 1)

    def test_missing_provider_never_uses_bundled_history_or_synthetic_forecast(self):
        from services import live_traffic, clock
        with patch.dict(os.environ, {'VERCEL': '1'}), \
             patch.object(model, '_model', return_value=(self.payload, self.forests)), \
             patch.object(live_traffic, '_tomtom_api_key', return_value=''), \
             patch.object(live_traffic, '_tomtom_segments') as provider, \
             patch.object(clock, 'now', return_value=NOW), \
             patch.object(model, 'snapshot', side_effect=AssertionError('No local archive fallback')), \
             patch.object(weather, 'get_weather', return_value={'status': 'unavailable', 'current': None}), \
             patch.object(weather, 'history', return_value=([], [])):
            with self.assertRaisesRegex(ValueError, 'No fresh observed traffic'):
                model.forecast(NOW)
        provider.assert_not_called()

    def test_serverless_default_databases_use_writable_ephemeral_directory(self):
        with patch.dict(os.environ, {'VERCEL': '1'}, clear=True):
            self.assertEqual(weather.database_path(), Path('/tmp/crossflow-ai/weather_context.db'))
            self.assertEqual(ledger.database_path(), Path('/tmp/crossflow-ai/forecast_evaluation.db'))
            self.assertEqual(weather.database_path('/fixture/weather.db'), Path('/fixture/weather.db'))
            self.assertEqual(ledger.database_path('/fixture/evaluation.db'), Path('/fixture/evaluation.db'))
        with patch.dict(os.environ, {'CROSSFLOW_WEATHER_DB': '/fixture/configured.db'}, clear=True):
            self.assertEqual(weather.database_path(), Path('/fixture/configured.db'))

    def test_weather_feed_remains_available_when_archival_write_is_readonly(self):
        from services import provider_json
        value = {'status': 'fresh', 'source': 'Open-Meteo', 'current': {'valid_at': NOW.isoformat()},
                 'captured_at': NOW.isoformat(), 'hourly': []}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
        class Opener:
            def open(self, *_args, **_kwargs): return Response()
        with patch.object(weather, '_cache', None), patch.object(weather, '_attempt', 0.), \
             patch.object(weather.time, 'monotonic', return_value=100.), \
             patch.object(weather, 'build_opener', return_value=Opener()), \
             patch.object(provider_json, 'read_json', return_value={}), \
             patch.object(weather, 'parse', return_value=value), \
             patch.object(weather, '_connection', side_effect=sqlite3.OperationalError('readonly database')):
            result = weather.get_weather(NOW)
        self.assertEqual(result['status'], 'fresh')
        self.assertEqual(result['current']['valid_at'], NOW.isoformat())

    def test_evaluation_summary_does_not_create_serverless_database(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'missing/ledger.db'
            with patch.dict(os.environ, {'VERCEL': '1', 'CROSSFLOW_FORECAST_EVALUATION_DB': str(path)}):
                self.assertEqual(ledger.summary()['status'], 'awaiting_predictions')
            self.assertFalse(path.exists())


if __name__ == '__main__': unittest.main()
