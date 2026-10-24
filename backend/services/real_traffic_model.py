"""Observed-only short-horizon models with untouched temporal test data.

The limited local archive may support a provisional experiment, not a field
accuracy certification. Validation selects RF versus persistence; calibration
alone sets intervals. Synthetic rows never enter this pipeline.
"""
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from itertools import islice
from pathlib import Path
import sqlite3
import shutil
import tempfile
import threading

import numpy as np

from services import live_weather
from services.measured_road_features import ROAD_FEATURE_NAMES, road_features, feature_vector as road_vector
from services.observed_model_artifact import NumericRandomForest
from services.traffic_candidate_selection import fit_candidates, encode_target, decode_prediction
from services.traffic_observations import SpatialTrafficObservation, validate_spatial_observation
from services.congestion_math import speed_ratio_congestion_score

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / 'backend/data/real_traffic_model.json'
HORIZONS = (15, 30, 60)
CORRIDORS = tuple(f'corridor-{i}' for i in range(1, 6))
FEATURE_NAMES = ('current_index', 'current_age_minutes', 'lag_15', 'lag_15_missing',
                 'lag_30', 'lag_30_missing', 'lag_60', 'lag_60_missing',
                 'prior_30min_mean', 'prior_30min_std', 'prior_120min_mean', 'prior_120min_std',
                 'target_hour_sin', 'target_hour_cos', 'target_weekday_sin', 'target_weekday_cos',
                 'horizon_minutes', 'measured_free_flow_kph',
                 *(f'is_{name}' for name in CORRIDORS), *ROAD_FEATURE_NAMES, *live_weather.WEATHER_FEATURE_NAMES)
SCHEMA = 'crossflow.observed_short_horizon.v1'
_lock = threading.Lock()
_loaded = None
_refresh_lock = threading.Lock()


def _score(row):
    return speed_ratio_congestion_score(row.speed_ratio)


def _location(row):
    return row.corridor_id, round(row.latitude, 5), round(row.longitude, 5)


def snapshot(path=None, *, limit=50_000):
    if path is None:
        from services import historical_store
        path = Path(historical_store._DB_PATH)
    path = Path(path).resolve()
    if not path.is_file():
        return [], {}, {'status': 'archive_unavailable', 'stored_rows': 0}
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        total = db.execute('SELECT count(*) FROM spatial_observations').fetchone()[0]
        raw = db.execute('SELECT * FROM spatial_observations ORDER BY observed_at_us DESC, observation_key LIMIT ?', (limit,)).fetchall()
    records, available = [], {}
    for item in reversed(raw):
        if item['observed'] not in (0, 1) or item['reviewed'] not in (0, 1):
            raise ValueError('Invalid archive provenance flags.')
        row = SpatialTrafficObservation(**{name: item[name] for name in (
            'observation_key', 'latitude', 'longitude', 'actual_speed_kph', 'free_flow_speed_kph',
            'source', 'provenance', 'confidence', 'road_class', 'capacity_vph', 'terminal_distance_km',
            'local_timezone_offset_minutes', 'upstream_event_id')}, corridor_id=item['corridor'],
            observed_at=datetime.fromtimestamp(item['observed_at_us'] / 1e6, timezone.utc),
            observed=bool(item['observed']), reviewed=bool(item['reviewed']))
        row = validate_spatial_observation(row)
        if row.observed and row.corridor_id in CORRIDORS:
            records.append(row)
            available[row.observation_key] = max(row.observed_at, datetime.fromtimestamp(item['ingested_at_us'] / 1e6, timezone.utc))
    return records, available, {'status': 'read_only_local_sqlite', 'stored_rows': total,
                                'loaded_rows': len(records), 'truncated': total > len(raw)}


def _features(history, origin, horizon, weather_archive):
    current = history[-1]
    score = _score(current)
    result = [score, (origin - current.observed_at).total_seconds() / 60]
    for minutes in (15, 30, 60):
        cutoff = origin - timedelta(minutes=minutes)
        lag = next((row for row in reversed(history) if row.observed_at <= cutoff), None)
        missing = lag is None or (cutoff - lag.observed_at).total_seconds() > 1800
        result.extend((score if missing else _score(lag), float(missing)))
    for minutes in (30, 120):
        values = [_score(row) for row in history if row.observed_at >= origin - timedelta(minutes=minutes)]
        result.extend((float(np.mean(values)), float(np.std(values))))
    local = (origin + timedelta(minutes=horizon)).astimezone(timezone(timedelta(hours=7)))
    hour = 2 * math.pi * (local.hour + local.minute / 60 + local.second / 3600) / 24
    day = 2 * math.pi * local.weekday() / 7
    result.extend((math.sin(hour), math.cos(hour), math.sin(day), math.cos(day), horizon,
                   current.free_flow_speed_kph, *(float(current.corridor_id == name) for name in CORRIDORS)))
    result.extend(road_vector(road_features(current.latitude, current.longitude)))
    result.extend(live_weather.feature_vector(live_weather.asof(origin, weather_archive, target=origin + timedelta(minutes=horizon)), origin))
    return result


def _examples(rows, available, targets, horizon, weather_archive, *, freeze=None):
    locations = defaultdict(list)
    for row in rows:
        locations[_location(row)].append(row)
    times = {key: [row.observed_at for row in values] for key, values in locations.items()}
    examples = []
    for target in targets:
        if freeze is not None and available[target.observation_key] >= freeze:
            continue
        origin = target.observed_at - timedelta(minutes=horizon)
        key = _location(target)
        end = bisect_right(times[key], origin)
        history = [row for row in locations[key][max(0, end - 129):end] if available[row.observation_key] <= origin]
        if not history or (origin - history[-1].observed_at).total_seconds() > 1800:
            continue
        examples.append({'features': _features(history, origin, horizon, weather_archive),
                         'actual': _score(target), 'persistence': _score(history[-1]),
                         'corridor': target.corridor_id, 'timestamp': target.observed_at.isoformat(),
                         'free_flow': history[-1].free_flow_speed_kph, 'actual_speed': target.actual_speed_kph})
    return examples


def _errors(actual, predicted):
    a, p = np.asarray(actual), np.asarray(predicted)
    if not len(a):
        return {'samples': 0, 'mae_points': None, 'rmse_points': None, 'r2': None}
    variance = float(np.sum((a - np.mean(a)) ** 2))
    return {'samples': len(a), 'mae_points': float(np.mean(np.abs(a - p))),
            'rmse_points': float(np.sqrt(np.mean((a - p) ** 2))),
            'within_5_points_fraction': float(np.mean(np.abs(a - p) <= 5.)),
            'r2': 1 - float(np.sum((a - p) ** 2)) / variance if variance > 1e-10 else None}


def _radius(actual, predicted):
    residual = sorted(abs(a - p) for a, p in zip(actual, predicted))
    if not residual:
        raise ValueError('No independent calibration samples.')
    return residual[min(len(residual), math.ceil((len(residual) + 1) * .8)) - 1]


def _network_pairs(examples, predicted):
    groups = defaultdict(list)
    for example, value in zip(examples, predicted):
        stamp = datetime.fromisoformat(example['timestamp']).timestamp()
        groups[int(stamp) // 900].append((example['corridor'], example['actual'], value, stamp))
    complete = [values for values in groups.values() if {item[0] for item in values} == set(CORRIDORS)
                and max(item[3] for item in values) - min(item[3] for item in values) <= 120]
    return ([float(np.mean([item[1] for item in values])) for values in complete],
            [float(np.mean([item[2] for item in values])) for values in complete])


def _trees(model):
    return [{'left': e.tree_.children_left.tolist(), 'right': e.tree_.children_right.tolist(),
             'feature': e.tree_.feature.tolist(), 'threshold': e.tree_.threshold.tolist(),
             'value': e.tree_.value.reshape(-1).tolist()} for e in model.estimators_]


def _window(rows):
    return {'rows': len(rows), 'start_at': rows[0].observed_at.isoformat() if rows else None,
            'end_at': rows[-1].observed_at.isoformat() if rows else None,
            'corridors': sorted({row.corridor_id for row in rows})}


def _partitions(rows, available, weather_archive, history_rows=None):
    # Allocate by complete, causally available timestamp groups, not by label
    # values. Collection gaps must not swallow an entire calibration window.
    groups = defaultdict(list)
    horizon_groups = []
    for horizon in HORIZONS:
        groups = defaultdict(list)
        for example in _examples(history_rows or rows, available, rows, horizon, weather_archive):
            bucket = int(datetime.fromisoformat(example['timestamp']).timestamp()) // 900
            groups[bucket].append(example)
        horizon_groups.append({key for key, values in groups.items() if {value['corridor'] for value in values} == set(CORRIDORS)})
    complete = sorted(set.intersection(*horizon_groups))
    count = max(1, int(len(complete) * .1))
    latest_available = {}
    for row in rows:
        key = int(row.observed_at.timestamp()) // 900
        latest_available[key] = max(latest_available.get(key, available[row.observation_key]), available[row.observation_key])
    partitions = {}
    cutoff = None
    for name, take in (('holdout', max(2, int(len(complete) * .15))), ('calibration', count), ('validation', count)):
        eligible = [key for key in complete if cutoff is None or latest_available[key] < cutoff]
        chosen = set(eligible[-take:])
        values = [row for row in rows if int(row.observed_at.timestamp()) // 900 in chosen]
        if not values:
            raise ValueError('Insufficient independent chronological windows.')
        partitions[name] = values
        cutoff = values[0].observed_at - timedelta(minutes=60)
        complete = [key for key in complete if key not in chosen]
    partitions['fit'] = [row for row in rows if row.observed_at < cutoff and available[row.observation_key] < cutoff]
    return {name: partitions[name] for name in ('fit', 'validation', 'calibration', 'holdout')}


def train(records, available, *, now, weather_archive=None):
    if now.tzinfo is None:
        raise ValueError('Training time must have a timezone.')
    # Revalidate, deduplicate and retain original 15-minute samples. Never pad,
    # interpolate or relabel artificial rows as observations.
    records = list(islice(iter(records), 50_001))
    if len(records) > 50_000:
        raise ValueError('Real-data training is bounded to 50000 input rows.')
    selected, identities = {}, {}
    for value in sorted(records, key=lambda row: (row.observed_at, row.observation_key)):
        row = validate_spatial_observation(value)
        previous = identities.get(row.observation_key)
        if previous and previous.immutable_payload() != row.immutable_payload():
            raise ValueError('Conflicting observation identity in real training data.')
        identities[row.observation_key] = row
        if row.observed and row.corridor_id in CORRIDORS and row.observed_at <= now and available.get(row.observation_key, now + timedelta(days=1)) <= now:
            selected.setdefault((*_location(row), int(row.observed_at.timestamp()) // 900), row)
    rows = sorted(selected.values(), key=lambda row: (row.observed_at, row.observation_key))
    history_rows = sorted((row for row in identities.values() if row.observed and row.corridor_id in CORRIDORS
                           and row.observed_at <= now and available.get(row.observation_key, now + timedelta(days=1)) <= now),
                          key=lambda row: (row.observed_at, row.observation_key))
    if len(rows) < 50:
        raise ValueError('At least 50 original observed samples are needed for a provisional fit.')
    weather_archive = weather_archive or live_weather.history()
    partitions = _partitions(rows, available, weather_archive, history_rows)
    if any(len(values) < 5 for values in partitions.values()):
        raise ValueError('Insufficient independent chronological partitions.')
    report = {'schema': SCHEMA, 'trained_at': now.isoformat(), 'training_source': 'observed_traffic_archive',
              'synthetic_rows': 0, 'input_rows': len(records),
              'raw_observed_rows': sum(row.observed and row.corridor_id in CORRIDORS and row.observed_at <= now and available.get(row.observation_key, now + timedelta(days=1)) <= now for row in identities.values()),
              'excluded_non_observed_rows': sum(not row.observed for row in identities.values()), 'selected_rows': len(rows),
              'span_hours': (rows[-1].observed_at - rows[0].observed_at).total_seconds() / 3600,
              'distinct_local_days': len({row.local_observed_at.date() for row in rows}),
              'hour_bins': sorted({row.local_observed_at.hour for row in rows}),
              'source_counts': dict(Counter(row.source for row in rows)),
              'partitions': {name: _window(values) for name, values in partitions.items()},
              'purge_minutes': 60, 'feature_names': list(FEATURE_NAMES), 'horizons': [],
              'quality_gate': {'eligible': False, 'reasons': []},
              'selection_method': 'validation_only_then_frozen_before_calibration_and_final_test',
              'evaluation_scope': 'retrospective_development_windows_previously_inspected; prospective confirmation required',
              'split_policy': 'timestamp_and_availability_only_complete_15min_groups_60min_purges',
              'dataset_sha256': hashlib.sha256(_canonical([
                  {'row': row.immutable_payload(), 'available_at': available[row.observation_key].isoformat()}
                  for row in history_rows])).hexdigest(),
              'history_feature_sampling': 'all_original_available_measurements_targets_sampled_at_15min',
              'weather_archive_sha256': hashlib.sha256(_canonical(weather_archive[1])).hexdigest(),
              'weather_training_rows': 0, 'availability': 'explicit_traffic_ingestion_and_weather_capture_times'}
    from services import router
    report['graph_revision'] = router.GRAPH_REVISION
    locations = {_location(row): row for row in rows}
    report['road_features'] = [road_features(locations[key].latitude, locations[key].longitude) | {'corridor_id': key[0]} for key in sorted(locations)]
    fitted = {}
    for horizon in HORIZONS:
        freezes = {name: partitions[next_name][0].observed_at - timedelta(minutes=horizon)
                   for name, next_name in (('fit', 'validation'), ('validation', 'calibration'), ('calibration', 'holdout'))}
        examples = {name: _examples(history_rows, available, values, horizon, weather_archive, freeze=freezes.get(name))
                    for name, values in partitions.items()}
        if any(len(examples[name]) < 5 for name in examples):
            raise ValueError(f'Insufficient independent {horizon}-minute forecast pairs.')
        fit, validation, calibration, holdout = (examples[name] for name in ('fit', 'validation', 'calibration', 'holdout'))
        model, candidate, candidate_scores = fit_candidates(
            [e['features'] for e in fit], [e['actual'] for e in fit], [e['persistence'] for e in fit],
            [e['features'] for e in validation], [e['actual'] for e in validation], [e['persistence'] for e in validation])
        validation_prediction = decode_prediction(model.predict([e['features'] for e in validation]),
                                                 [e['persistence'] for e in validation], candidate)
        rf_mae = _errors([e['actual'] for e in validation], validation_prediction)['mae_points']
        persistence_mae = _errors([e['actual'] for e in validation], [e['persistence'] for e in validation])['mae_points']
        strategy = 'random_forest' if rf_mae < persistence_mae else 'persistence'
        # Freeze the strategy before using calibration or test labels.
        final_fit = fit + validation
        model.fit([e['features'] for e in final_fit], encode_target([e['actual'] for e in final_fit],
                  [e['persistence'] for e in final_fit], candidate['target_transform']))
        def predict(examples, selected=True):
            return decode_prediction(model.predict([e['features'] for e in examples]),
                [e['persistence'] for e in examples], candidate).tolist() if strategy == 'random_forest' or not selected else [e['persistence'] for e in examples]
        radius = _radius([e['actual'] for e in calibration], predict(calibration))
        predicted = predict(holdout)
        forest_predicted = predict(holdout, selected=False)
        actual = [e['actual'] for e in holdout]
        baseline = [e['persistence'] for e in holdout]
        coverage = float(np.mean([max(0., p - radius) <= a <= min(100., p + radius) for a, p in zip(actual, predicted)]))
        network_calibration = _network_pairs(calibration, predict(calibration))
        network_radius = _radius(*network_calibration) if network_calibration[0] else None
        network_test = _network_pairs(holdout, predicted)
        network_coverage = float(np.mean([max(0., p - network_radius) <= a <= min(100., p + network_radius)
                                         for a, p in zip(*network_test)])) if network_radius is not None and network_test[0] else None
        metrics = _errors(actual, predicted)
        result = {'horizon_minutes': horizon, 'selected_strategy': strategy,
                  'forest_candidate': candidate['candidate'], 'validation_candidates': candidate_scores,
                  'validation': {'forest_mae': rf_mae, 'persistence_mae': persistence_mae},
                  'test': metrics, 'forest_test': _errors(actual, forest_predicted),
                  'persistence_test': _errors(actual, baseline),
                  'speed_mae_kph': float(np.mean([abs(e['actual_speed'] - e['free_flow'] * (1 - p / 100)) for p, e in zip(predicted, holdout)])),
                  'interval': {'method': 'calibration_only_absolute_error_order_statistic', 'target': .8,
                               'radius_points': radius, 'calibration_pairs': len(calibration),
                               'test_coverage': coverage, 'coverage_guaranteed': False,
                               'network_mean_radius_points': network_radius, 'network_calibration_groups': len(network_calibration[0]),
                               'network_test_groups': len(network_test[0]), 'network_test_coverage': network_coverage},
                  'pairs': {name: len(value) for name, value in examples.items()},
                  'per_corridor': {corridor: _errors([a for a, e in zip(actual, holdout) if e['corridor'] == corridor],
                                                    [p for p, e in zip(predicted, holdout) if e['corridor'] == corridor]) for corridor in CORRIDORS},
                  'feature_importances': dict(zip(FEATURE_NAMES, model.feature_importances_.tolist()))}
        report['horizons'].append(result)
        if horizon == 30:
            report['weather_training_rows'] = sum(e['features'][FEATURE_NAMES.index('weather_missing')] == 0 for e in final_fit)
            report['training_pairs'] = len(final_fit)
        fitted[str(horizon)] = {'trees': _trees(model), 'importances': model.feature_importances_.tolist(),
                                'target_transform': candidate['target_transform'], 'residual_weight': candidate['residual_weight'],
                                'candidate': candidate['candidate'],
                                'strategy': strategy, 'radius': radius, 'network_radius': network_radius}
    reasons = report['quality_gate']['reasons']
    if report['span_hours'] < 168 or report['distinct_local_days'] < 7 or len(report['hour_bins']) < 18:
        reasons.append('Needs at least seven days and broad hour-of-day coverage.')
    if report['weather_training_rows'] < 50:
        reasons.append('Needs more weather snapshots captured before forecast origins.')
    for result in report['horizons']:
        if result['selected_strategy'] != 'random_forest':
            reasons.append(f"{result['horizon_minutes']} min: validation still prefers the persistence baseline.")
        if result['pairs']['fit'] < 100 or result['pairs']['validation'] < 50 or result['interval']['calibration_pairs'] < 50:
            reasons.append(f"{result['horizon_minutes']} min: needs larger fit, validation and calibration windows.")
        if result['test']['samples'] < 100 or any(value['samples'] < 5 for value in result['per_corridor'].values()):
            reasons.append(f"{result['horizon_minutes']} min: insufficient independent test coverage.")
        if result['forest_test']['mae_points'] >= .95 * result['persistence_test']['mae_points']:
            reasons.append(f"{result['horizon_minutes']} min: forest has not beaten persistence by 5% on final test.")
        if result['interval']['test_coverage'] < .7:
            reasons.append(f"{result['horizon_minutes']} min: interval coverage is below 70% on final test.")
    report['quality_gate']['eligible'] = not reasons
    report['status'] = 'validated_observed_candidate' if not reasons else 'provisional_observed_model'
    return {'schema': SCHEMA, 'report': report, 'models': fitted}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def save_artifact(payload, path=ARTIFACT):
    path = Path(path)
    encoded = _canonical({'sha256': hashlib.sha256(_canonical(payload)).hexdigest(), 'payload': payload})
    if len(encoded) > 32 * 1024 * 1024:
        raise ValueError('Model exceeds artifact budget.')
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        file.write(encoded); name = Path(file.name)
    try:
        load_artifact(name)
        if path.exists():
            shutil.copy2(path, path.with_name(path.stem + '.previous.json'))
        name.replace(path)
    finally:
        name.unlink(missing_ok=True)


def load_artifact(path=ARTIFACT):
    path = Path(path)
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('Model exceeds artifact budget.')
    envelope = json.loads(path.read_bytes())
    payload = envelope['payload']
    if hashlib.sha256(_canonical(payload)).hexdigest() != envelope['sha256'] or payload.get('schema') != SCHEMA:
        raise ValueError('Observed model checksum or schema mismatch.')
    if payload['report']['feature_names'] != list(FEATURE_NAMES) or payload['report']['synthetic_rows'] != 0:
        raise ValueError('Observed feature schema mismatch.')
    from services import router
    if payload['report'].get('graph_revision') != router.GRAPH_REVISION:
        raise ValueError('Mapped road revision changed; retrain before inference.')
    if set(payload['models']) != {str(h) for h in HORIZONS}:
        raise ValueError('Invalid forecast horizons.')
    forests = {}
    for horizon, value in payload['models'].items():
        if value.get('target_transform', 'absolute') not in ('absolute', 'shifted_residual') or isinstance(value.get('residual_weight', 1.), bool) or not isinstance(value.get('residual_weight', 1.), (int, float)) or not 0 <= value.get('residual_weight', 1.) <= 1:
            raise ValueError('Invalid residual prediction decoder.')
        if value['strategy'] not in ('random_forest', 'persistence') or not isinstance(value['radius'], (int, float)) or not math.isfinite(value['radius']) or not 0 <= value['radius'] <= 100:
            raise ValueError('Invalid calibrated strategy.')
        forests[horizon] = NumericRandomForest(value['trees'], value['importances'], feature_count=len(FEATURE_NAMES))
    return payload, forests


def _model():
    global _loaded
    path = Path(os.environ.get('CROSSFLOW_REAL_MODEL_PATH') or str(ARTIFACT)).resolve()
    stamp = path.stat().st_mtime_ns
    with _lock:
        if _loaded is None or _loaded[0] != (str(path), stamp):
            payload, forests = load_artifact(path)
            _loaded = ((str(path), stamp), payload, forests)
        return _loaded[1:]


def _status(payload, forests):
    from services.real_traffic_training import TRAINER
    return {'available': True, 'report': payload['report'], 'automatic_training': TRAINER.status()['active'],
            'artifact_sha256': hashlib.sha256(_canonical(payload)).hexdigest(),
            'engine': {'algorithm': 'RandomForestRegressor', 'trees_per_horizon': {key: len(value.estimators_) for key, value in forests.items()},
                       'feature_count': len(FEATURE_NAMES), 'synthetic_rows': 0}}


def status():
    try:
        return _status(*_model())
    except (OSError, ValueError, KeyError, TypeError):
        return {'available': False, 'report': None, 'engine': None}


def serverless_persistence():
    """Ephemeral runtimes have no causal lag archive to justify candidate use."""
    return bool(os.environ.get('VERCEL') or os.environ.get('AWS_LAMBDA_FUNCTION_NAME'))


def _serverless_provider_rows(now):
    """Fresh in-memory provider inputs, without manufacturing historical rows."""
    from services import live_traffic, clock
    from services.operations_analytics import _sample
    key = live_traffic._tomtom_api_key()
    segments = live_traffic._tomtom_segments(key) if key else {}
    instant = clock.now()
    rows, available = [], {}
    for corridor, (latitude, longitude) in live_traffic._CORRIDOR_POINTS.items():
        item = segments.get(corridor) if isinstance(segments, dict) else None
        if isinstance(item, dict) and item.get('source', 'tomtom_live') != 'tomtom_live':
            continue
        sample = _sample(item, instant)
        if sample is None:
            continue
        stamp = datetime.fromisoformat(sample['as_of']).astimezone(timezone.utc)
        if stamp > instant:
            continue
        row = SpatialTrafficObservation.create(corridor_id=corridor, latitude=latitude, longitude=longitude,
            observed_at=stamp, actual_speed_kph=sample['current_speed_kmh'],
            free_flow_speed_kph=sample['free_flow_speed_kmh'], source='tomtom_live', validation_now=instant)
        rows.append(row)
        available[row.observation_key] = instant
    return rows, available, {'status': 'live_provider_request_no_historical_archive', 'stored_rows': 0,
        'loaded_rows': len(rows), 'truncated': False, 'durability': 'request_memory',
        'historical_archive_shared': False}, instant


def forecast(now):
    payload, forests = _model()
    cloud_baseline = serverless_persistence()
    if cloud_baseline:
        rows, available, archive, now = _serverless_provider_rows(now)
    else:
        rows, available, archive = snapshot()
    rows = [row for row in rows if row.observed_at <= now and available[row.observation_key] <= now]
    if not cloud_baseline and os.environ.get('CROSSFLOW_REAL_TRAFFIC_TRAINING') == '1':
        fresh = {row.corridor_id for row in rows if 0 <= (now - row.observed_at).total_seconds() <= 180}
        if fresh != set(CORRIDORS) and _refresh_lock.acquire(timeout=7):
            try:
                from services import clock, traffic_archiver
                rows, available, archive = snapshot()
                fresh = {row.corridor_id for row in rows if 0 <= (clock.now() - row.observed_at).total_seconds() <= 180}
                if fresh != set(CORRIDORS):
                    traffic_archiver.ARCHIVER.collect_once()
                now = clock.now()
                rows, available, archive = snapshot()
                rows = [row for row in rows if row.observed_at <= now and available[row.observation_key] <= now]
            finally:
                _refresh_lock.release()
    weather = live_weather.get_weather(now)
    if os.environ.get('CROSSFLOW_REAL_TRAFFIC_TRAINING') == '1':
        from services import clock
        now = clock.now()
    weather_archive = ([], []) if cloud_baseline else live_weather.history()
    locations = defaultdict(list)
    for row in rows:
        locations[_location(row)].append(row)
    points = []
    for corridor in CORRIDORS:
        candidates = [values for key, values in locations.items() if key[0] == corridor]
        if not candidates:
            continue
        history = max(candidates, key=lambda values: values[-1].observed_at)
        current = history[-1]
        age = (now - current.observed_at).total_seconds()
        if not 0 <= age <= 180:
            continue
        history = history[-129:]
        road = road_features(current.latitude, current.longitude)
        horizons = []
        for horizon in HORIZONS:
            specification = payload['models'][str(horizon)]
            forest = float(decode_prediction(forests[str(horizon)].predict([_features(history, now, horizon, weather_archive)]),
                           [_score(current)], specification)[0])
            serving_strategy = specification['strategy'] if payload['report']['quality_gate']['eligible'] and not cloud_baseline else 'persistence'
            mean = forest if serving_strategy == 'random_forest' else _score(current)
            evidence = next(value for value in payload['report']['horizons'] if value['horizon_minutes'] == horizon)
            radius = specification['radius'] if not cloud_baseline and serving_strategy == specification['strategy'] and evidence['interval']['calibration_pairs'] >= 50 and evidence['test']['samples'] >= 100 else None
            horizons.append({'horizon_minutes': horizon, 'mean_score': round(mean, 3),
                             'lower': round(max(0., mean - radius), 3) if radius is not None else None,
                             'upper': round(min(100., mean + radius), 3) if radius is not None else None,
                             'strategy': serving_strategy, 'forest_candidate_score': round(forest, 3)})
        points.append({'corridor_id': corridor, 'observed_at': current.observed_at.isoformat(),
                       'latitude': current.latitude, 'longitude': current.longitude,
                       'current_speed_kph': current.actual_speed_kph, 'free_flow_speed_kph': current.free_flow_speed_kph,
                       'current_index': round(_score(current), 3), 'road': road, 'forecasts': horizons})
    if not points:
        raise ValueError('No fresh observed traffic is available. Synthetic fallback is disabled.')
    network = []
    for index, horizon in enumerate(HORIZONS):
        mean = float(np.mean([point['forecasts'][index]['mean_score'] for point in points]))
        evidence = next(value for value in payload['report']['horizons'] if value['horizon_minutes'] == horizon)
        strategies = {point['forecasts'][index]['strategy'] for point in points}
        strategy = next(iter(strategies)) if len(strategies) == 1 else 'mixed'
        calibrated_strategy = payload['models'][str(horizon)]['strategy']
        radius = payload['models'][str(horizon)]['network_radius'] if not cloud_baseline and strategy == calibrated_strategy and len(points) == len(CORRIDORS) and evidence['interval']['network_calibration_groups'] >= 20 and evidence['interval'].get('network_test_groups', 0) >= 20 and (evidence['interval'].get('network_test_coverage') or 0) >= .7 else None
        network.append({'horizon_minutes': horizon, 'mean_score': round(mean, 3), 'strategy': strategy,
                        'lower': round(max(0., mean - radius), 3) if radius is not None else None,
                        'upper': round(min(100., mean + radius), 3) if radius is not None else None})
    from services import forecast_evaluation, historical_evaluation
    return {'generated_at': now.isoformat(), 'source': 'observed_traffic_model', 'observed': False,
            'prospective_evaluation': forecast_evaluation.summary(),
            'historical_benchmarks': historical_evaluation.summary(),
            'status': payload['report']['status'], 'model': _status(payload, forests), 'weather': weather,
            'points': points, 'network': network, 'archive': archive,
            'coverage': {'monitored_points': len(points), 'expected_points': len(CORRIDORS),
                         'configured_point_ids': list(CORRIDORS), 'sampling_scope': 'representative_provider_points',
                         'max_horizon_minutes': max(HORIZONS)},
            'limitations': (['Serverless forecasts use fresh provider points in memory and force persistence; no shared causal lag archive or cloud prospective evaluation is claimed.'] if cloud_baseline else []) + ['Provisional until temporal, baseline and interval quality gates pass.',
                            'Coverage counts configured provider sampling points, not every mapped Batam road.',
                            'When the release gate has not passed, forecasts retain the latest measured index as a persistence baseline.',
                            'Mapped OSM attributes are not field measurements of capacity or lane counts.',
                            'Road context is nearest mapped geometry, not a verified provider direction or lane-level match.',
                            'Missing tags are explicit; road capacity is not invented.',
                            'Weather effects need captured training coverage; current weather is a regional model grid.',
                            'No traffic prediction beyond 60 minutes is claimed; weather forecasts may extend further.',
                            'Intervals use independent calibration data; temporal dependence prevents a coverage guarantee.']}
