"""Append-only prospective predictions, scored only when future traffic arrives.

This local ledger is independent of fitting. Issued model versions, forecasts and
baselines cannot be rewritten after the corresponding targets become visible.
"""
from datetime import datetime, timedelta, timezone
import hashlib, json, math, os, sqlite3
from pathlib import Path

PATH = Path(__file__).resolve().parents[1]/'data/forecast_evaluation.db'
TOLERANCE_SECONDS = 180


def database_path(path=None):
    """Resolve at call time so isolated runtimes never share the default ledger."""
    if path is not None:
        return Path(path)
    configured = os.environ.get('CROSSFLOW_FORECAST_EVALUATION_DB', '').strip()
    if configured:
        return Path(configured)
    if os.environ.get('VERCEL') or os.environ.get('AWS_LAMBDA_FUNCTION_NAME'):
        return Path('/tmp/crossflow-ai/forecast_evaluation.db')
    return PATH


def _utc(value):
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if stamp.tzinfo is None or stamp.utcoffset() is None: raise ValueError('Evaluation requires explicit UTC offsets.')
    return stamp.astimezone(timezone.utc)


def _connection(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db=sqlite3.connect(path,timeout=5)
    db.row_factory=sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS issued_forecasts (
      id TEXT PRIMARY KEY, issued_at TEXT NOT NULL, origin_at TEXT NOT NULL, target_at TEXT NOT NULL,
      model_version TEXT NOT NULL, corridor TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
      horizon INTEGER NOT NULL, candidate REAL NOT NULL, selected REAL NOT NULL, persistence REAL NOT NULL,
      actual REAL, actual_at TEXT, matched_at TEXT, closed INTEGER NOT NULL DEFAULT 0)''')
    db.execute('CREATE INDEX IF NOT EXISTS forecast_pending ON issued_forecasts(actual, target_at)')
    return db


def record(value, issued_at, path=None):
    path=database_path(path)
    issued_at=_utc(issued_at); origin=_utc(value['generated_at'])
    if not timedelta(0)<=issued_at-origin<=timedelta(seconds=30): raise ValueError('Only newly issued predictions can enter the ledger.')
    report=value['model']['report']
    if report['synthetic_rows']!=0 or report['training_source']!='observed_traffic_archive' or _utc(report['trained_at'])>origin:
        raise ValueError('Only earlier real-data models are eligible.')
    fingerprint=value['model'].get('artifact_sha256')
    if fingerprint is not None and (not isinstance(fingerprint,str) or len(fingerprint)!=64 or any(c not in '0123456789abcdef' for c in fingerprint)):
        raise ValueError('Invalid saved model fingerprint.')
    version=fingerprint or hashlib.sha256(json.dumps({'trained_at':report['trained_at'],'dataset':report['dataset_sha256'],
        'selection':[(h['horizon_minutes'],h['selected_strategy'],h.get('forest_candidate')) for h in report['horizons']]},sort_keys=True).encode()).hexdigest()
    inserted=0
    with _connection(path) as db:
        for point in value['points']:
            if not 0<=(origin-_utc(point['observed_at'])).total_seconds()<=180: continue
            for row in point['forecasts']:
                if row['horizon_minutes'] not in (15,30,60): raise ValueError('Unsupported forecast horizon.')
                numbers=[row['forest_candidate_score'],row['mean_score'],point['current_index']]
                if any(isinstance(n,bool) or not isinstance(n,(int,float)) or not math.isfinite(n) or not 0<=n<=100 for n in numbers):raise ValueError('Invalid prediction score.')
                target=origin+timedelta(minutes=row['horizon_minutes'])
                key=hashlib.sha256(f"{version}:{origin.isoformat()}:{point['corridor_id']}:{point['latitude']}:{point['longitude']}:{row['horizon_minutes']}".encode()).hexdigest()
                inserted+=db.execute('INSERT OR IGNORE INTO issued_forecasts(id,issued_at,origin_at,target_at,model_version,corridor,latitude,longitude,horizon,candidate,selected,persistence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (key,issued_at.isoformat(),origin.isoformat(),target.isoformat(),version,point['corridor_id'],point['latitude'],point['longitude'],row['horizon_minutes'],*numbers)).rowcount
    return inserted


def score(records, now, path=None):
    path=database_path(path)
    from services.congestion_math import speed_ratio_congestion_score
    now=_utc(now)
    records=sorted((r for r in records if r.observed and r.observed_at<=now),key=lambda r:r.observed_at)
    updated=0
    with _connection(path) as db:
        pending=db.execute('SELECT * FROM issued_forecasts WHERE closed=0 AND target_at<=? ORDER BY target_at LIMIT 5000',(now.isoformat(),)).fetchall()
        for row in pending:
            target=_utc(row['target_at']);issued=_utc(row['issued_at'])
            actual=next((r for r in records if r.corridor_id==row['corridor'] and round(r.latitude,5)==round(row['latitude'],5)
                and round(r.longitude,5)==round(row['longitude'],5) and issued<r.observed_at and 0<=(r.observed_at-target).total_seconds()<=TOLERANCE_SECONDS),None)
            if actual is None:
                if now>target+timedelta(seconds=TOLERANCE_SECONDS):
                    db.execute('UPDATE issued_forecasts SET closed=1 WHERE id=? AND actual IS NULL',(row['id'],))
                continue
            updated+=db.execute('UPDATE issued_forecasts SET actual=?, actual_at=?, matched_at=?, closed=1 WHERE id=? AND actual IS NULL',
                (speed_ratio_congestion_score(actual.speed_ratio),actual.observed_at.isoformat(),now.isoformat(),row['id'])).rowcount
    return updated


def _summary(path=None):
    path=database_path(path)
    if not path.exists():return {'status':'awaiting_predictions','issued':0,'matched':0,'matched_days':0,'horizons':[]}
    # Dashboard reads must not run CREATE TABLE/INDEX or initialize a ledger.
    # Collectors own schema creation and writes; this view remains available
    # when a deployed evaluation archive is mounted read-only.
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=5) as db:
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        total=db.execute('SELECT count(*) FROM issued_forecasts').fetchone()[0]
        matched,day_count=db.execute('SELECT count(*),count(DISTINCT substr(actual_at,1,10)) FROM issued_forecasts WHERE actual IS NOT NULL').fetchone()
        rows=db.execute('SELECT horizon,actual,candidate,selected,persistence,actual_at,model_version FROM issued_forecasts WHERE actual IS NOT NULL ORDER BY actual_at DESC LIMIT 100000').fetchall()
        cohorts=[dict(r) for r in db.execute('SELECT model_version,count(*) AS issued,sum(actual IS NOT NULL) AS matched FROM issued_forecasts GROUP BY model_version ORDER BY max(issued_at) DESC LIMIT 100')]
    horizons=[]
    for h in (15,30,60):
        group=[r for r in rows if r['horizon']==h]
        if not group:continue
        count=len(group)
        errors={name:sum(abs(r['actual']-r[name]) for r in group)/count for name in ('candidate','selected','persistence')}
        horizons.append({'horizon_minutes':h,'samples':count,'candidate_mae_points':errors['candidate'],
            'selected_mae_points':errors['selected'],'persistence_mae_points':errors['persistence'],
            'within_5_points_fraction':sum(abs(r['actual']-r['candidate'])<=5 for r in group)/count})
    return {'status':'collecting_prospective_evidence','issued':total,'matched':matched,'matched_days':day_count,'horizons':horizons,
            'metrics_window':'latest_100000_matched_predictions; counts_are_all_time; test_days_use_UTC',
            'model_cohorts':cohorts,
            'evaluation_design':'prequential system evaluation across logged hourly model versions; never retroactively refit an issued prediction',
            'target_tolerance_seconds':TOLERANCE_SECONDS,'field_accuracy_validated':False,
            'scope':'Predictions saved before their future measurements; provider point speeds, not independent ground-truth sensors. At least seven days needed before judging improvement.'}


def summary(path=None):
    try:
        return _summary(path)
    except (sqlite3.Error, OSError):
        return {'status':'evaluation_unavailable','issued':0,'matched':0,'matched_days':0,'horizons':[]}
