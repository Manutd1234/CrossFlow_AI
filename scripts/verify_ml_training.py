from pathlib import Path
from datetime import datetime,timezone
import csv,gzip,hashlib,json,math,re,subprocess,tempfile
from urllib.parse import unquote
root=Path(__file__).resolve().parents[2];repo=root/'CrossFlow_AI';data=repo/'backend/data/research';issues=[]
def read(p):return json.loads(p.read_text())
def check(ok,label):
 if not ok:issues.append(label)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
run=read(data/'ml_training/report.json');status=read(data/'ml_training/status.json');manifest=read(data/'ml_training/manifest.json')
for row in manifest['files']:
 p=(data/'ml_training'/row['path']).resolve()
 check(p.is_file() and p.stat().st_size==row['bytes'] and sha(p)==row['sha256'],'artifact:'+row['path'])
check(len(status['experiments'])==3,'experiment_count')
check(all(x['training_performed'] and x['production_eligible'] is False for x in status['experiments']),'training_and_scope')
check(not status['model_activated'] and not status['citywide_Batam_accuracy_validated'],'promotion_scope')
for x in run['archive_diagnostics']:
 check(not x['evaluation']['training_performed'],'provider_or_insufficient_fit')
 check(not x['evaluation']['model_replaced'],'provider_model_mutation')
metrics={}
for name,filename,actual,prediction,field in [
 ('speed_training','predictions.csv','actual_kph','selected_model_kph','mae_kph'),
 ('forecast_training','test_predictions.csv.gz','actual_flow_vehicles_per_hour','selected_prediction','mae_vehicles_per_hour'),
 ('vision_training','predictions.csv','actual_vehicle_boxes','predicted_count','mae_vehicles_per_frame')]:
 p=data/name/filename
 with (gzip.open(p,'rt') if p.suffix=='.gz' else p.open()) as f:rows=list(csv.DictReader(f))
 mae=sum(abs(float(r[actual])-float(r[prediction])) for r in rows)/len(rows)
 report=read(data/name/'report.json');expected=(report['selected_holdout_metrics'] if name=='forecast_training' else report['test_metrics'])[field]
 check(math.isclose(mae,expected,rel_tol=1e-10,abs_tol=1e-10),'saved_metric:'+name)
 metrics[name]=dict(rows=len(rows),recomputed_mae=mae,metric_unit=field)
check(status['experiments'][2]['beats_all_reported_baselines'] is False,'vision_failure_not_hidden')
progress=read(root/'research/batam-traffic/collection_progress.json')
check(progress['mixed_catalogue_entries']==1026 and progress['local_photographic_candidates_at_most']==182,'unchanged_acquisition_units')
check(progress['training_started'] and progress['backtesting_started'],'current_authorization_status')
check(read(root/'research/batam-traffic/summary.json')['collection_progress']==progress,'research_summary_sync')
node=Path('/Users/ian/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node')
scripts=re.findall(r'<script\b[^>]*>(.*?)</script>',(root/'research/batam-traffic/index.html').read_text(),re.S)
with tempfile.TemporaryDirectory() as td:
 p=Path(td)/'catalogue.js';p.write_text('\n'.join(scripts))
 result=subprocess.run([str(node),'--check',str(p)],capture_output=True,text=True)
 check(result.returncode==0,'catalogue_js_syntax')
for p in [repo/'docs/ML_ENGINE_TRAINING_2026-10-24.md',root/'research/batam-traffic/ML_TRAINING_PROGRESS.md']:
 for target in re.findall(r'\]\(([^)]+)\)',p.read_text()):
  target=target.strip('<>').split('#')[0]
  if target and not re.match(r'^[a-zA-Z]+:',target):check((p.parent/unquote(target)).exists(),'doc_link:'+target)
counts=dict(survey_speed=15,hourly_forecast=7,visual_counts=6,causal_forecast=17,snapshot_integrity=4,results_api_including_ASGI=4,existing_observed_pipeline=38)
report=dict(verified_at_utc=datetime.now(timezone.utc).isoformat(),status='passed' if not issues else 'failed',
 recorded_tests_passed=sum(counts.values()),test_groups=counts,
 test_evidence='Test groups executed during the training session; this verifier rechecks saved artifacts/metrics and does not rerun training or the test suite.',optional_vision_runtime_without_Pillow='clean_module_skip',
 saved_prediction_metrics=metrics,receipts_rechecked=len(manifest['files']),catalogue_javascript='passed' if not result.returncode else 'failed',
 source_acquisition_counts_unchanged=True,serving_model_activated=False,provider_models_fitted=False,issues=issues,
 code_receipts=[dict(path=str(p.relative_to(root)),sha256=sha(p)) for p in [repo/'scripts/train_ml_engine.py',repo/'backend/services/causal_forecasting.py',repo/'backend/services/training_experiments.py',repo/'backend/main.py']])
(data/'ml_training/verification.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
if issues:raise SystemExit(1)
