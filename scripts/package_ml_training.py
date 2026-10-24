"""Package reviewed training code, public inputs and models; exclude private archives."""
from datetime import datetime,timezone
import hashlib,json
from pathlib import Path
import zipfile

WORKSPACE=Path(__file__).resolve().parents[2]
REPO=WORKSPACE/'CrossFlow_AI'
DATA=REPO/'backend/data/research'


def digest(path):
 h=hashlib.sha256()
 with path.open('rb') as file:
  for part in iter(lambda:file.read(1048576),b''):h.update(part)
 return h.hexdigest()


def main():
 verification=json.loads((DATA/'ml_training/verification.json').read_text())
 assert verification['status']=='passed' and not verification['issues']
 files=set()
 def add(path):
  path=WORKSPACE/path if isinstance(path,str) else path
  if not path.is_file():raise FileNotFoundError(path)
  assert path.suffix not in ('.db','.sqlite','.sqlite3') and not path.name.startswith('.env')
  files.add(path)
 scripts=['train_ml_engine.py','train_local_mac.py','train_batam_speed.py','backtest_batam_survey.py',
          'research_forecasting.py','train_visual_counts.py','verify_ml_training.py','package_ml_training.py',
          'requirements-training.txt','requirements-vision-training.txt']
 for name in scripts:add(REPO/'scripts'/name)
 for name in ['causal_forecasting.py','survey_speed_model.py','training_experiments.py','traffic_observations.py','congestion_math.py']:
  add(REPO/'backend/services'/name)
 for name in ['test_causal_forecasting.py','test_survey_speed_training.py','test_research_forecasting.py',
              'test_visual_count_training.py','test_ml_training_pipeline.py','test_training_results_api.py']:
  add(REPO/'backend/tests'/name)
 add(REPO/'docs/ML_ENGINE_TRAINING_2026-10-24.md')
 for name in ['speed_training','forecast_training','vision_training','ml_training']:
  for path in (DATA/name).rglob('*'):
   if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc','.tmp') and path.name!='.DS_Store':add(path)
 for name in ['batam-gajah-mada-2023.csv','batam-gajah-mada-2023.manifest.json']:add(DATA/name)
 research=WORKSPACE/'research/batam-traffic'
 for name in ['ML_TRAINING_PROGRESS.md','ml_training_progress.json','training_readiness.json','acquisition_preferences.json',
              'collection_targets.json','collection_progress.json']:add(research/name)
 add(research/'datasets/round4_open/forecasting/normalized/paris_hourly.csv')
 vision=research/'datasets/round4_open/vision'
 add(vision/'manifest.json')
 for name in ['README.md','TRANSSET_README.md','cross_format_audit.json']:
  if (vision/name).is_file():add(vision/name)
 for folder in ['visaitech_mixed_traffic','transset']:
  for name in ['files_manifest.json','frame_counts.jsonl','README.md']:
   if (vision/folder/name).is_file():add(vision/folder/name)
 frames=json.loads((DATA/'vision_training/frame_manifest.json').read_text())
 for group in ['training_frames','holdout_frames']:
  for row in frames[group]:
   for key in ['image','label']:add(vision/row[key])
 expected={p.relative_to(WORKSPACE).as_posix():digest(p) for p in sorted(files)}
 api=(REPO/'backend/main.py').read_text()
 start=api.index('@app.get("/api/analytics/training-results")')
 end=api.index('@app.post(OBSERVED_ANALYTICS_BACKTEST_PATH)',start)
 notes={
 'START_HERE.txt':
  'Batam ML engine training update, 24 October 2026.\n'
  'Start with CrossFlow_AI/docs/ML_ENGINE_TRAINING_2026-10-24.md.\n'
  'Contains real-data experiment models, public licensed source subsets, code, predictions, hashes and QA. '
  'Batam speed is one historical survey site; Paris hourly counts and both visual datasets are foreign. '
  'The visual learned model fails its constant baseline. No serving model was activated; no Batam queue model is claimed.\n'
  'No private provider rows, SQLite databases, credentials or environment files are included. '
  'Original paper/source licenses and attribution remain attached; this is a local research bundle, not a blanket license grant.\n'
  'These are update files for the existing CrossFlow_AI workspace, not a standalone web application. '
  'Use the existing API environment for numeric experiments and the documented optional vision Python. '
  'The local backend/main.py already has the new operator-only endpoint. Its additive function is included in APP_INTEGRATION.txt.\n'
  'Run from CrossFlow_AI: .venv/bin/python scripts/train_ml_engine.py --reuse-results\n'
  'This validates existing model/source hashes and refreshes read-only local archive diagnostics. '
  'No account, provider request, recording, database mutation, cloud job or deployment occurs. '
  'Acquisition ZIPs from the preceding pass are historical snapshots; research/ current training ledgers supersede their earlier no-training state.\n',
 'APP_INTEGRATION.txt':'Already added to local CrossFlow_AI/backend/main.py; provided separately to preserve other local application changes.\n\n'+api[start:end],
 'FILES.sha256':''.join(sha+'  '+name+'\n' for name,sha in expected.items())}
 expected.update({name:hashlib.sha256(value.encode()).hexdigest() for name,value in notes.items()})
 destination=WORKSPACE/'research/batam-ml-engine-training.zip'
 temporary=destination.with_suffix('.zip.tmp')
 with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as out:
  for path in sorted(files):out.write(path,'batam-ml-training/'+path.relative_to(WORKSPACE).as_posix())
  for name,value in notes.items():out.writestr('batam-ml-training/'+name,value)
 with zipfile.ZipFile(temporary) as archive:
  assert len(archive.namelist())==len(expected)
  for name,sha in expected.items():assert hashlib.sha256(archive.read('batam-ml-training/'+name)).hexdigest()==sha,name
 temporary.replace(destination)
 sha=digest(destination)
 info=dict(created_at_utc=datetime.now(timezone.utc).isoformat(),path=destination.name,bytes=destination.stat().st_size,
           sha256=sha,files=len(expected),crc_and_all_member_sha256='passed',private_provider_rows_included=False,
           serving_model_activated=False,recorded_tests_passed=verification['recorded_tests_passed'])
 destination.with_suffix('.zip.sha256').write_text(sha+'  '+destination.name+'\n')
 destination.with_suffix('.archive_info.json').write_text(json.dumps(info,indent=2)+'\n')
 print(json.dumps(info,indent=2))

if __name__=='__main__':main()
