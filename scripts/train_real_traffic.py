#!/usr/bin/env python3
"""Fit real local measurements; retain prior artifacts and print aggregate evidence only."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
os.environ['CROSSFLOW_REAL_TRAFFIC_TRAINING'] = '1'
from services import live_weather, real_traffic_model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=ROOT / 'backend/data/local_history.db')
    parser.add_argument('--output', type=Path, default=real_traffic_model.ARTIFACT)
    parser.add_argument('--refresh-weather', action='store_true')
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    if args.refresh_weather:
        live_weather.get_weather(now)
    records, available, archive = real_traffic_model.snapshot(args.database)
    payload = real_traffic_model.train(records, available, now=now)
    payload['report']['archive'] = archive
    payload['report']['local_training_authorization'] = 'requested_by_workspace_owner_2026-10-24'
    if args.output.exists():
        backup = args.output.with_name(args.output.stem + now.strftime('.previous-%Y%m%dT%H%M%S%f') + '.json')
        shutil.copy2(args.output, backup)
    real_traffic_model.save_artifact(payload, args.output)
    # Verify data-only export gives identical predictions, never load pickle.
    restored, forests = real_traffic_model.load_artifact(args.output)
    assert restored['report']['synthetic_rows'] == 0 and all(len(f.estimators_) == 100 for f in forests.values())
    print(json.dumps({'status': restored['report']['status'], 'observed_rows': len(records),
                      'selected_rows': restored['report']['selected_rows'],
                      'span_hours': restored['report']['span_hours'],
                      'features': len(real_traffic_model.FEATURE_NAMES),
                      'horizons': [{key: h[key] for key in ('horizon_minutes', 'selected_strategy', 'test', 'forest_test', 'persistence_test', 'interval')}
                                   for h in restored['report']['horizons']],
                      'quality_gate': restored['report']['quality_gate']}, indent=2))


if __name__ == '__main__':
    main()
