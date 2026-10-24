#!/usr/bin/env python3
"""Local real-data experiments: no network, synthetic fallback, DB writes or deployment."""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile

for setting in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ.setdefault(setting, '1')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
MAX_ARCHIVE_ROWS = 100_000


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        file.write((json.dumps(value, indent=2, allow_nan=False)+'\n').encode())
        temporary = Path(file.name)
    temporary.replace(path)


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda: file.read(1_048_576), b''):
            value.update(chunk)
    return value.hexdigest()


def archive_snapshot(path, *, now, limit=MAX_ARCHIVE_ROWS):
    """Read a consistent SQLite snapshot, retaining availability; never create a store."""
    from services.traffic_observations import SpatialTrafficObservation, validate_spatial_observation
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ARCHIVE_ROWS:
        raise ValueError('Invalid archive row limit')
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Snapshot time must be timezone-aware')
    if not path.is_file():
        return [], {}, dict(status='not_present', loaded_rows=0, database_name=path.name)
    db = sqlite3.connect(path.absolute().as_uri()+'?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='spatial_observations'").fetchone():
            return [], {}, dict(status='no_spatial_table', loaded_rows=0, database_name=path.name)
        required = {'observed_at_us', 'ingested_at_us', 'corridor', 'observation_key'}
        if not required <= {row[1] for row in db.execute('PRAGMA table_info(spatial_observations)')}:
            raise ValueError('Archive lacks identity/availability fields')
        total = db.execute('SELECT count(*) FROM spatial_observations').fetchone()[0]
        raw = db.execute('SELECT * FROM spatial_observations ORDER BY observed_at_us DESC, observation_key LIMIT ?', (limit,)).fetchall()
    finally:
        db.close()
    rows, available = [], {}
    for item in reversed(raw):
        if item['observed'] not in (0, 1) or item['reviewed'] not in (0, 1):
            raise ValueError('Noncanonical provenance flags')
        fields = {name: item[name] for name in (
            'observation_key','latitude','longitude','actual_speed_kph','free_flow_speed_kph',
            'source','provenance','confidence','road_class','capacity_vph','terminal_distance_km',
            'local_timezone_offset_minutes','upstream_event_id')}
        row = SpatialTrafficObservation(**fields, corridor_id=item['corridor'],
            observed_at=datetime.fromtimestamp(item['observed_at_us']/1_000_000, timezone.utc),
            observed=bool(item['observed']), reviewed=bool(item['reviewed']))
        rows.append(validate_spatial_observation(row))
        available[row.observation_key] = datetime.fromtimestamp(item['ingested_at_us']/1_000_000, timezone.utc)
    observed = [row for row in rows if row.observed and row.observed_at <= now]
    times = sorted(row.observed_at for row in observed)
    metadata = dict(status='read_only_snapshot', database_name=path.name, loaded_rows=len(rows),
        total_stored_rows=total, truncated=total>len(rows), source_counts=dict(Counter(row.source for row in rows)),
        first_observed_at=times[0].isoformat() if times else None, last_observed_at=times[-1].isoformat() if times else None,
        span_hours=(times[-1]-times[0]).total_seconds()/3600 if times else 0,
        distinct_WIB_dates=len({row.local_observed_at.date() for row in observed}),
        distinct_WIB_hour_bins=len({row.local_observed_at.hour for row in observed}),
        availability_timestamp_retained=True, raw_rows_exported=False, provider_training_authorized=False)
    return rows, available, metadata


def run_experiment(script, output, *, reuse, replace=False, interpreter=None):
    if not reuse:
        command=[interpreter or sys.executable, str(ROOT/'scripts'/script), '--output-dir', str(output)]
        if replace and script=='research_forecasting.py': command.append('--replace')
        subprocess.run(command, cwd=ROOT, check=True)
    path = output/'report.json'
    if not path.is_file() or path.stat().st_size > 4*1024*1024:
        raise ValueError('Missing or oversized experiment report: '+output.name)
    report = json.loads(path.read_text())
    if report.get('production_eligible') is not False:
        raise ValueError('Research results cannot be activated by this runner')
    manifest=json.loads((output/'manifest.json').read_text())
    for item in manifest['files']:
        file=(output/item['path']).resolve()
        file.relative_to(output.resolve())
        if not file.is_file() or file.stat().st_size!=item['bytes'] or digest(file)!=item['sha256']:
            raise ValueError('Experiment artifact checksum mismatch: '+item['path'])
    if script=='train_batam_speed.py':
        source=ROOT/'backend/data/research/batam-gajah-mada-2023.csv'
        expected=report['source']['csv_sha256']
    elif script=='train_visual_counts.py':
        source_root=ROOT.parent/'research/batam-traffic/datasets/round4_open/vision'
        source=source_root/'manifest.json'
        expected=report['source_manifest_sha256']
        frames=json.loads((output/'frame_manifest.json').read_text())
        for group in ('training_frames','holdout_frames'):
            for row in frames[group]:
                for field in ('image','label'):
                    member=(source_root/row[field]).resolve();member.relative_to(source_root.resolve())
                    if digest(member)!=row[field+'_sha256']: raise ValueError('Vision source changed')
    else:
        source=ROOT.parent/'research/batam-traffic/datasets/round4_open/forecasting/normalized/paris_hourly.csv'
        expected=report['source']['sha256']
    if digest(source)!=expected: raise ValueError('Experiment source changed')
    return report


def compact_experiment(key,value):
    foreign=key=='foreign_hourly_counts'
    if foreign:
        model=value['selection']['selected_forecaster']
        metrics=value['selected_holdout_metrics']
        baselines={name:score for name,score in value['holdout_scores'].items() if score['kind']=='baseline'}
        trained=value['status']=='trained_and_held_out_evaluated'
    else:
        model=value['selected_model'];metrics=value['test_metrics'];baselines=value['baseline_metrics']
        trained=value['training_performed']
    metric_name='mae_vehicles_per_hour' if foreign else 'mae_vehicles_per_frame' if key=='foreign_visual_counts' else 'mae_kph'
    beats=all(metrics[metric_name]<baseline[metric_name] for baseline in baselines.values())
    return dict(id=key,target=value['target'],selected_model=model,test_metrics=metrics,baseline_metrics=baselines,
        beats_all_reported_baselines=beats,validation_preferred_strategy=value.get('validation_preferred_strategy',model),
        production_eligible=False,training_performed=trained,
        scope='foreign_transfer_benchmark' if foreign else 'foreign_image_count_transfer' if key=='foreign_visual_counts' else 'single_Batam_survey_site')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT/'backend/data/research/ml_training')
    parser.add_argument('--reuse-results', action='store_true', help='Aggregate finished experiments without repeating holdouts')
    parser.add_argument('--replace-results', action='store_true', help='Explicitly replace previous experiments with the same frozen protocol')
    parser.add_argument('--skip-transfer', action='store_true')
    parser.add_argument('--skip-archive', action='store_true')
    parser.add_argument('--skip-vision', action='store_true')
    parser.add_argument('--vision-python', default=shutil.which('python3') or sys.executable, help='Python with Pillow/NumPy/sklearn for image features')
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    speed_dir, count_dir, vision_dir = [ROOT/'backend/data/research'/name for name in ('speed_training','forecast_training','vision_training')]
    if args.reuse_results and args.replace_results: parser.error('Choose reuse or replacement, not both')
    folders=[speed_dir]+([] if args.skip_transfer else [count_dir])+([] if args.skip_vision else [vision_dir])
    if not args.reuse_results and not args.replace_results and any((p/'report.json').exists() for p in folders):
        parser.error('Completed experiments exist. Use --reuse-results, or explicit --replace-results to repeat the frozen protocol.')
    if args.replace_results and not args.skip_vision and (vision_dir/'report.json').exists():
        parser.error('The visual source-disjoint holdout is frozen. Use --reuse-results, or --skip-vision --replace-results for the other fixed protocols.')
    if not args.skip_vision and not args.reuse_results:
        subprocess.run([args.vision_python,'-c','import PIL, numpy, sklearn'], check=True)
    speed = run_experiment('train_batam_speed.py', speed_dir, reuse=args.reuse_results, replace=args.replace_results)
    count = None if args.skip_transfer else run_experiment('research_forecasting.py', count_dir, reuse=args.reuse_results, replace=args.replace_results)
    vision=None if args.skip_vision else run_experiment('train_visual_counts.py',vision_dir,reuse=args.reuse_results,replace=args.replace_results,interpreter=args.vision_python)
    archives = []
    if not args.skip_archive:
        from services.causal_forecasting import evaluate_causal_training
        for name in ('local_history.db','congestion_history.db'):
            rows, available, snapshot = archive_snapshot(ROOT/'backend/data'/name, now=now)
            evaluation = evaluate_causal_training(rows, now=now, max_rows=MAX_ARCHIVE_ROWS,
                availability_times=available, allow_provider_training=False)
            archives.append(dict(snapshot=snapshot, evaluation=evaluation))
    experiments = dict(batam_site_speed=speed, foreign_hourly_counts=count, foreign_visual_counts=vision)
    report = dict(schema_version=1, schema='crossflow.ml_training_run.v1', generated_at=now.isoformat(),
        training_authorized_on_local='2026-10-24', experiments=experiments, training_started=True,
        this_invocation_reused_completed_experiments=args.reuse_results, archive_diagnostics=archives,
        model_activated=False, citywide_Batam_accuracy_validated=False, camera_model_trained=False, batam_camera_model_trained=False,
        foreign_image_count_model_trained=bool(vision and vision['training_performed']),
        camera_reason='No sourced annotated Batam camera sessions; unlabelled photos/maps are not vehicle/queue ground truth.',
        speed_and_travel_time='Single-site speed means only; no measured route travel-time forecast claim.',
        congestion_severity='Causal residual engine implemented; rights and temporal coverage gates remain enforced.',
        synthetic_training_rows_added=0, network_requests=0, database_writes=0,
        limitations=[
            'Static graph edges are not independent observed traffic labels.',
            'Foreign hourly counts do not establish Batam speed, severity, camera counts or queues.',
            'Batam survey means are retrospective and previously inspected; no prospective validation claim.',
            'Routing cost_s, speed-limit attributes and synthetic generator outputs are not training labels.',
            'No model is automatically promoted or deployed.'])
    status = dict(schema_version=1, status='completed', generated_at=now.isoformat(), training_started=True,
        model_activated=False, citywide_Batam_accuracy_validated=False, camera_model_trained=False, batam_camera_model_trained=False,
        foreign_image_count_model_trained=bool(vision and vision['training_performed']),
        provider_models_fitted=False, report='report.json', experiments=[compact_experiment(key,value) for key,value in experiments.items() if value is not None])
    output = args.output_dir.absolute()
    write_json(output/'report.json',report)
    write_json(output/'status.json',status)
    files = [output/name for name in ('report.json','status.json')]
    for folder in (speed_dir, count_dir if count else None, vision_dir if vision else None):
        if folder:
            files.extend(p for p in folder.rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    receipts = [dict(path=os.path.relpath(p,output), bytes=p.stat().st_size, sha256=digest(p)) for p in files]
    write_json(output/'manifest.json',dict(schema_version=1,files=receipts,training_started=True,
        model_activated=False,raw_provider_rows_included=False))
    print(json.dumps(status,indent=2,allow_nan=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
