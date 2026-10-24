"""Compatibility for road-routing callers, using measured references only.

Unsupported long horizons use an explicitly labelled latest-observed reference;
they never invoke the synthetic profile generator or add an assumed ferry surge.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading
import time

from services import real_traffic_model

_lock = threading.Lock()
_cache = None


def runtime_info():
    value = real_traffic_model.status()
    engine = value.get('engine') or {}
    return {'algorithm': 'ObservedTrafficEnsemble',
            'estimator_count': sum(engine.get('trees_per_horizon', {}).values()),
            'feature_count': engine.get('feature_count', 0),
            'is_trained': value['available'],
            'last_trained_at': value['report']['trained_at'] if value['report'] else None}


def predict(contexts, payload_factory):
    global _cache
    now = datetime.now(timezone.utc)
    with _lock:
        if _cache is None or time.monotonic() - _cache[0] > 60:
            _cache = (time.monotonic(), real_traffic_model.forecast(now))
        value = _cache[1]
    points = {point['corridor_id']: point for point in value['points']
              if (now - datetime.fromisoformat(point['observed_at'])).total_seconds() <= 180}
    results = []
    local = now.astimezone(timezone(timedelta(hours=7)))
    for context in contexts:
        point = points.get(f'corridor-{context.corridor_idx + 1}')
        if point is None:
            raise RuntimeError('Fresh observed traffic reference unavailable.')
        minutes = ((context.hour_float - local.hour - local.minute / 60) * 60) % (24 * 60)
        interval_missing = True
        if min(minutes, 1440 - minutes) < 10:
            mean, lower, upper = point['current_index'], point['current_index'], point['current_index']
            source = 'latest_measured_provider_point_reference'
        elif minutes <= 60:
            row = min(point['forecasts'], key=lambda row: abs(row['horizon_minutes'] - minutes))
            mean = row['mean_score']
            lower, upper = row['lower'], row['upper']
            interval_missing = lower is None
            # Full support bounds express missing interval evidence; they are
            # marked explicitly instead of inventing narrow calibrated bounds.
            lower, upper = (0., 100.) if lower is None else (lower, upper)
            source = 'observed_' + row['strategy'] + '_provisional_interval_evidence_pending'
        else:
            mean, lower, upper = point['current_index'], 0., 100.
            source = 'latest_observed_reference_only_forecast_beyond_60min_unavailable'
        changed = replace(context, spatial_source=source,
                          defaults_applied=tuple(context.defaults_applied) + ('one_provider_point_not_measured_edge_traffic', 'no_field_accuracy_certification')
                          + (('missing_calibration_uses_full_support_bounds_and_std_upper_bound',) if interval_missing else ()))
        prediction = payload_factory(changed, mean, mean, 50. if upper - lower == 100 else 0., lower, upper,
                                     min(100., max(0., mean + context.risk_aversion * (upper - mean))))
        results.append(replace(prediction, model_id='crossflow_observed_short_horizon_v1',
                               ferry_surge_adjustment_method='not_applied_observed_only_model'))
    return results
