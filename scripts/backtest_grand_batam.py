#!/usr/bin/env python3
"""One-hour survey-speed diagnostic; never imports data into live road history."""
import csv, hashlib, json, math, sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
from sklearn.base import clone
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from services.traffic_candidate_selection import fit_candidates,encode_target,decode_prediction

RAW=ROOT.parent/'research/batam-traffic/datasets/paper_observations/round3/observations.csv'
PDF=ROOT/'backend/data/traffic-imports/historical-expansion-2026-10-24/batam-grand-mall.pdf'
OUTPUT=ROOT/'backend/data/research/batam-grand-mall-2023.csv'
EXPECTED_PDF='ff67266fb0e1cb94dbfa8cf904486bf6e8ada3699b47c7b0fa5c0d79550064b0'
EXPECTED_CSV='906848f58568987127914a43999bc553e84ca7bce1bcfb20afd701d5855e4003'


def load():
 if hashlib.sha256(PDF.read_bytes()).hexdigest()!=EXPECTED_PDF:raise ValueError('Survey PDF differs')
 if OUTPUT.exists():
  if hashlib.sha256(OUTPUT.read_bytes()).hexdigest()!=EXPECTED_CSV:raise ValueError('Reviewed survey CSV differs')
  manifest=json.loads(OUTPUT.with_suffix('.manifest.json').read_text())
  rows=[{**r,'speed_kph':float(r['speed_kph']),'measured_segment_length_m':int(r['measured_segment_length_m'])} for r in csv.DictReader(OUTPUT.open())]
  return sorted(rows,key=lambda r:(r['interval_start'],r['road'])),manifest['source_url']
 values=[r for r in csv.DictReader(RAW.open()) if r['source_record_id']=='BATAM-EXT-024' and r['metric']=='reported_mean_travel_speed']
 if len(values)!=66:raise ValueError('Unexpected reviewed table size')
 rows=[]
 for r in values:
  start,end=(datetime.fromisoformat(r[k]) for k in ('interval_start_iso8601','interval_end_iso8601'))
  if (end-start).total_seconds()!=3600 or start.utcoffset().total_seconds()!=25200:raise ValueError('Invalid original interval')
  speed=float(r['value'])
  if not math.isfinite(speed) or not 0<speed<=100:raise ValueError('Invalid speed')
  rows.append({'interval_start':start.isoformat(),'interval_end':end.isoformat(),'road':r['location_text'].split(':',1)[1],
   'speed_kph':speed,'measured_segment_length_m':int(r['measurement_distance_metres']),'quality_flags':r['quality_flags']})
 return sorted(rows,key=lambda r:(r['interval_start'],r['road'])),values[0]['source_url']


def examples(rows):
 roads=sorted({r['road'] for r in rows});out=[]
 for target in rows:
  origin=datetime.fromisoformat(target['interval_start'])
  history=[r for r in rows if r['road']==target['road'] and datetime.fromisoformat(r['interval_end'])<=origin]
  if not history or datetime.fromisoformat(history[-1]['interval_end'])!=origin:continue
  current=history[-1]
  prior=next((r for r in reversed(history) if r['interval_start'][11:16]==target['interval_start'][11:16] and r['interval_start'][:10]<target['interval_start'][:10]),None)
  second=history[-2] if len(history)>1 and (origin-datetime.fromisoformat(history[-2]['interval_end'])).total_seconds()==3600 else None
  hour=origin.hour/24*2*math.pi
  out.append({'date':target['interval_start'][:10],'origin':origin.isoformat(),'target_end':target['interval_end'],
   'x':[current['speed_kph'],second['speed_kph'] if second else current['speed_kph'],int(second is None),
    prior['speed_kph'] if prior else current['speed_kph'],int(prior is None),math.sin(hour),math.cos(hour),int(origin.weekday()>=5),
    target['measured_segment_length_m'],*[int(target['road']==road) for road in roads]],
   'y':target['speed_kph'],'persistence':current['speed_kph'],'prior_survey':prior['speed_kph'] if prior else None,'road':target['road']})
 return out


def errors(actual,predicted):
 a,p=np.asarray(actual),np.asarray(predicted);e=a-p
 return {'samples':len(a),'mae_kph':float(np.mean(abs(e))),'rmse_kph':float(np.sqrt(np.mean(e**2))),
  'within_5_kph':float(np.mean(abs(e)<=5)), 'r2':float(1-np.sum(e**2)/np.sum((a-a.mean())**2)) if np.var(a)>1e-10 else None}


def run():
 rows,url=load();data=examples(rows)
 f=[r for r in data if r['date']<'2023-06-24'];v=[r for r in data if r['date']=='2023-06-24'];t=[r for r in data if r['date']=='2023-06-25']
 model,spec,scores=fit_candidates([r['x'] for r in f],[r['y'] for r in f],[r['persistence'] for r in f],
  [r['x'] for r in v],[r['y'] for r in v],[r['persistence'] for r in v])
 fit=f+v;model=clone(model);model.fit([r['x'] for r in fit],encode_target([r['y'] for r in fit],[r['persistence'] for r in fit],spec['target_transform']))
 predicted=decode_prediction(model.predict([r['x'] for r in t]),[r['persistence'] for r in t],spec)
 actual=[r['y'] for r in t];baseline=[r['persistence'] for r in t]
 selected=errors(actual,predicted);base=errors(actual,baseline)
 report={'scope':'Exploratory Grand Batam Mall three-road survey, not the app five-corridor model or citywide validation',
  'source_url':url,'source_pdf_sha256':EXPECTED_PDF,'source_rows':len(rows),'source_dates':sorted({r['interval_start'][:10] for r in rows}),
  'synthetic_rows':0,'unit':'km/h','horizon_minutes':60,'target':'Next source-published hourly mean speed; forecast at interval start',
  'availability_assumption':'A published interval mean is assumed available at its end; original release latency unknown',
  'train_pairs':len(f),'validation_pairs':len(v),'refit_pairs':len(fit),'final_test_pairs':len(t),'test_date':'2023-06-25',
  'selected_candidate':spec,'validation_candidates':scores,'candidate':selected,'persistence':base,
  'mae_improvement_percent':100*(base['mae_kph']-selected['mae_kph'])/base['mae_kph'] if base['mae_kph'] else None,
  'per_road':[{ 'road':road,'candidate':errors([actual[i] for i,r in enumerate(t) if r['road']==road],[predicted[i] for i,r in enumerate(t) if r['road']==road]),
               'persistence':errors([actual[i] for i,r in enumerate(t) if r['road']==road],[baseline[i] for i,r in enumerate(t) if r['road']==road])} for road in sorted({r['road'] for r in t})],
  'test_predictions':[{**{k:r[k] for k in ('origin','target_end','road','y','persistence','prior_survey')},'prediction':float(p)} for r,p in zip(t,predicted)],
  'limitations':['Only four dated survey days and one final test day; no confidence interval or production promotion.',
   'Source aggregates derive from measured segment travel time; sample sizes and direction breakdown are unreleased.',
   'Adjacent intervals and road observations are dependent.', 'No invented free-flow speeds, coordinates or canonical corridor mapping.']}
 common=[i for i,r in enumerate(t) if r['prior_survey'] is not None]
 report['prior_survey_day_common']={'baseline':errors([actual[i] for i in common],[t[i]['prior_survey'] for i in common]),
  'candidate':errors([actual[i] for i in common],[predicted[i] for i in common]),'persistence':errors([actual[i] for i in common],[baseline[i] for i in common])}
 with OUTPUT.open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
 manifest={'name':'Published hourly speed survey near Grand Batam Mall, June2023','source_url':url,'source_pdf_sha256':EXPECTED_PDF,
  'csv_sha256':hashlib.sha256(OUTPUT.read_bytes()).hexdigest(),'rows':66,'dates':report['source_dates'],'roads':3,
  'license':'Public repository factual measurement table; no explicit dataset licence located',
  'attribution':'Felany Frisca Ulandari et al., Analisis Kinerja Ruas Jalan di Sekitar Grand Batam Mall, Table5, PDFp6; dates PDFp4',
  'canonical_corridor_mapping':None,'synthetic_rows':0,'limitations':report['limitations']}
 OUTPUT.with_suffix('.manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
 (ROOT/'docs/verification/grand-batam-backtest-2026-10-24.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
 print(json.dumps({k:report[k] for k in ('candidate','persistence','mae_improvement_percent','prior_survey_day_common')},indent=2))

if __name__=='__main__':run()
