"""Small fixed forest comparison; validation labels alone choose configuration.

Residual targets are shifted/scaled into 0..100 so artifacts remain inert bounded
numeric trees. Their decoded change is added to the latest actual measurement.
"""
import numpy as np
from sklearn.ensemble import RandomForestRegressor

CANDIDATES = (
    {'name': 'absolute_original', 'transform': 'absolute', 'depth': 8, 'leaf': 3, 'weight': 1.},
    {'name': 'absolute_regularized', 'transform': 'absolute', 'depth': 10, 'leaf': 20, 'weight': 1.},
    {'name': 'residual_025', 'transform': 'shifted_residual', 'depth': 10, 'leaf': 20, 'weight': .25},
    {'name': 'residual_050', 'transform': 'shifted_residual', 'depth': 10, 'leaf': 20, 'weight': .5},
    {'name': 'residual_100', 'transform': 'shifted_residual', 'depth': 10, 'leaf': 20, 'weight': 1.},
)


def encode_target(actual, baseline, transform):
    values = np.asarray(actual, dtype=float)
    return (values - np.asarray(baseline) + 100.) / 2. if transform == 'shifted_residual' else values


def decode_prediction(predicted, baseline, specification):
    predicted, baseline = np.asarray(predicted), np.asarray(baseline)
    if specification.get('target_transform', 'absolute') == 'shifted_residual':
        predicted = baseline + specification.get('residual_weight', 1.) * (2. * predicted - 100.)
    return np.clip(predicted, 0., 100.)


def fit_candidates(x, y, baseline, validation_x, validation_y, validation_baseline):
    """Only development partitions are accepted. Calibration/test stay outside."""
    candidates, fitted = [], {}
    for config in CANDIDATES:
        transform = config['transform']
        key = (transform, config['depth'], config['leaf'])
        if key not in fitted:
            model = RandomForestRegressor(n_estimators=100, max_depth=config['depth'],
                min_samples_leaf=config['leaf'], random_state=42, n_jobs=1)
            model.fit(x, encode_target(y, baseline, transform))
            fitted[key] = model
        model = fitted[key]
        specification = {'candidate': config['name'], 'target_transform': transform,
                         'residual_weight': config['weight'], 'parameters': model.get_params()}
        predicted = decode_prediction(model.predict(validation_x), validation_baseline, specification)
        candidates.append((float(np.mean(np.abs(np.asarray(validation_y) - predicted))), model, specification))
    # Fixed order breaks ties; no test-score inspection or retry-until-win loop.
    error, selected, specification = min(candidates, key=lambda value: value[0])
    evidence = [{'candidate': item[2]['candidate'], 'validation_mae': item[0]} for item in candidates]
    return selected, specification, evidence
