#!/usr/bin/env python3
"""Frozen new-data benchmark. Foreign road results never replace Batam telemetry."""
from __future__ import annotations
import csv, hashlib, io, json, math, sys, zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np
from sklearn.base import clone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'backend'))
from services.traffic_candidate_selection import fit_candidates, encode_target, decode_prediction

SOURCE = ROOT/'backend/data/traffic-imports/historical-expansion-2026-10-24'
OUTPUT = ROOT/'docs/verification/historical-expansion-backtest-2026-10-24.json'


def metrics(actual, predicted):
    a, p = np.asarray(actual), np.asarray(predicted)
    if not len(a): return {'samples': 0, 'mae_kph': None, 'rmse_kph': None, 'r2': None, 'within_5_kph': None}
    e = a-p
    denominator = float(np.sum((a-a.mean())**2))
    return {'samples':len(a), 'mae_kph':float(np.abs(e).mean()), 'rmse_kph':float(np.sqrt(np.mean(e**2))),
            'r2':1-float(np.sum(e**2))/denominator if denominator>1e-10 else None,
            'within_5_kph':float(np.mean(np.abs(e)<=5))}


def day_block_interval(actual, predicted, baseline, days, replicates=2000):
    unique = np.unique(days)
    groups = np.array([[np.abs(actual[days==d]-predicted[days==d]).sum(),
                        np.abs(actual[days==d]-baseline[days==d]).sum(), np.sum(days==d)] for d in unique])
    if len(groups)<7: return {'status':'insufficient_independent_days','days':len(groups),'ci95':None}
    indexes = np.random.default_rng(20261024).integers(0,len(groups),(replicates,len(groups)))
    totals = groups[indexes].sum(axis=1)
    deltas = (totals[:,1]-totals[:,0])/totals[:,2]
    return {'status':'estimated','days':len(groups),'replicates':replicates,
            'metric':'persistence_MAE_minus_candidate_MAE_kph; positive favors candidate',
            'difference_kph':float((groups[:,1].sum()-groups[:,0].sum())/groups[:,2].sum()),
            'ci95':np.quantile(deltas,[.025,.975]).tolist(),
            'method':'paired whole-source-day bootstrap preserves within-day/road dependence; inter-day dependence remains possible'}


def load_guangzhou():
    path=SOURCE/'guangzhou-Data.zip'
    meta=json.loads((SOURCE/'guangzhou-metadata.json').read_text())
    if meta['metadata']['license']['id']!='cc-by-4.0':raise ValueError('Reviewed dataset licence differs')
    expected=next(r for r in json.loads((SOURCE/'acquisition.json').read_text()) if r['name']==path.name)['sha256']
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('Acquired ZIP checksum differs')
    grid=np.full((61*144,214),np.nan,dtype=np.float32)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        info=z.getinfo('Data/traffic_speed_data.csv')
        if info.file_size>40_000_000:raise ValueError('CSV exceeds reviewed size')
        for row in csv.DictReader(io.TextIOWrapper(z.open(info),encoding='utf-8-sig',newline='')):
            road,day,slot=(int(row[k]) for k in ('road_id','day_id','time_id'))
            speed=float(row['speed'])
            if not 1<=road<=214 or not 1<=day<=61 or not 1<=slot<=144 or not math.isfinite(speed) or not 0<speed<=200:
                raise ValueError('Invalid original speed record')
            t=(day-1)*144+slot-1
            if np.isfinite(grid[t,road-1]):raise ValueError('Duplicate road/window')
            grid[t,road-1]=speed
    return grid,expected


def temporal_features(grid):
    t,r=grid.shape
    pieces=[]; names=[]
    for lag in (0,1,2,3,6,12,144,1008):
        x=np.full_like(grid,np.nan)
        x[lag:]=grid[:t-lag] if lag else grid
        missing=~np.isfinite(x)
        pieces.extend((np.where(missing,grid,x),missing.astype(float)))
        names.extend((f'prior_{lag*10}min_kph',f'prior_{lag*10}min_missing'))
    valid=np.isfinite(grid); sums=np.vstack((np.zeros((1,r)),np.cumsum(np.where(valid,grid,0),axis=0,dtype=float)))
    squares=np.vstack((np.zeros((1,r)),np.cumsum(np.where(valid,grid**2,0),axis=0,dtype=float)))
    counts=np.vstack((np.zeros((1,r)),np.cumsum(valid,axis=0)))
    end=np.arange(t)+1
    for size in (3,6,12,144):
        start=np.maximum(0,end-size); n=counts[end]-counts[start]
        mean=(sums[end]-sums[start])/np.maximum(n,1)
        std=np.sqrt(np.maximum(0,(squares[end]-squares[start])/np.maximum(n,1)-mean**2))
        pieces.extend((mean,std));names.extend((f'prior_{size*10}min_mean',f'prior_{size*10}min_std'))
    # Anonymous source IDs are identifiers, never claimed geographic adjacency.
    pieces.append(np.broadcast_to(np.arange(r),(t,r))); names.append('anonymous_road_id')
    return np.stack(pieces,axis=2).astype(np.float32),names


def examples(grid, features, horizon, phase):
    step=horizon//10
    origin=np.arange(0,len(grid)-step)
    target=origin+step
    # 33 fit + 7 validation + 7 calibration + 14 final days, chosen before outcomes.
    bounds={'fit':(0,33*144),'validation':(33*144,40*144),'calibration':(40*144,47*144),'test':(47*144,61*144)}
    start,end=bounds[phase]
    valid=(origin>=start+(6 if start else 12))&(target<end)
    origin=origin[valid];target=target[valid]
    o,road=np.nonzero(np.isfinite(grid[origin])&np.isfinite(grid[target]))
    origins,targets=origin[o],target[o]
    if phase=='fit':
        stride=max(1,math.ceil(len(origins)/40000));origins,targets,road=origins[::stride],targets[::stride],road[::stride]
    clock=((targets+1)%144)/144*2*np.pi
    weekday=((targets//144+0)%7)/7*2*np.pi # 2016-08-01 was Monday
    x=np.column_stack((features[origins,road],np.sin(clock),np.cos(clock),np.sin(weekday),np.cos(weekday)))
    # Scale targets only for the common bounded tree encoding; original speeds
    # above 100 km/h remain valid and are never clipped to 100 km/h.
    return {'x':x,'y':grid[targets,road]/2., 'baseline':grid[origins,road]/2., 'origin':origins,'target':targets,'road':road,'day':targets//144}


def benchmark_guangzhou():
    grid,checksum=load_guangzhou();features,names=temporal_features(grid)
    report={'source_url':'https://zenodo.org/records/1205229','license':'CC BY 4.0',
        'attribution':'Xinyu Chen, Yixian Chen and Zhaocheng He (2018), Urban Traffic Speed Dataset of Guangzhou, China, DOI 10.5281/zenodo.1205229',
        'geography':'Guangzhou, China; not Batam','days':61,'roads':214,'original_speed_rows':int(np.isfinite(grid).sum()),
        'missing_road_windows':int(np.isnan(grid).sum()),'sha256':checksum,'synthetic_rows':0,'unit':'km/h',
        'clock_basis':'Published day/time-window identifiers; assume interval mean available at its end; source publication latency unknown',
        'feature_names':names+['target_hour_sin','target_hour_cos','target_weekday_sin','target_weekday_cos'],
        'scope':'Independent methodology benchmark, no canonical Batam history changes or foreign model activation', 'horizons':[]}
    for horizon in (10,30,60):
        print('Guangzhou horizon',horizon,flush=True)
        parts={name:examples(grid,features,horizon,name) for name in ('fit','validation','calibration','test')}
        f,v,c,t=(parts[name] for name in ('fit','validation','calibration','test'))
        model,spec,candidates=fit_candidates(f['x'],f['y'],f['baseline'],v['x'],v['y'],v['baseline'])
        print('selected',spec['candidate'],flush=True)
        refit=clone(model)
        refit.fit(np.vstack((f['x'],v['x'])),encode_target(np.concatenate((f['y'],v['y'])),np.concatenate((f['baseline'],v['baseline'])),spec['target_transform']))
        cal=2*decode_prediction(refit.predict(c['x']),c['baseline'],spec)
        residual=np.sort(np.abs(2*c['y']-cal));radius=float(residual[min(len(residual)-1,math.ceil((len(residual)+1)*.8)-1)])
        pred=2*decode_prediction(refit.predict(t['x']),t['baseline'],spec)
        a=2*t['y'];p=2*t['baseline']
        spec['target_speed_scale_kph']=2
        candidates=[{**candidate,'validation_mae_kph':candidate['validation_mae']*2} for candidate in candidates]
        row={'horizon_minutes':horizon,'selected_candidate':spec,'validation_candidates':candidates,
            'candidate':metrics(a,pred),'persistence':metrics(a,p),'paired_day_bootstrap':day_block_interval(a,pred,p,t['day']),
            'partitions':{name:{'pairs':len(e['y']),'first_origin_index':int(e['origin'].min()),'last_origin_index':int(e['origin'].max()),'latest_target_index':int(e['target'].max()),'source_days':sorted((np.unique(e['day'])+1).tolist())} for name,e in parts.items()},
            'interval':{'nominal':.8,'radius_kph':radius,'test_coverage':float(np.mean((a>=np.clip(pred-radius,0,200))&(a<=np.clip(pred+radius,0,200)))),'calibration_pairs':len(c['y']),'distribution_free_guarantee':False},
            'per_day':[{'source_day':int(d+1),'candidate':metrics(a[t['day']==d],pred[t['day']==d]),'persistence':metrics(a[t['day']==d],p[t['day']==d])} for d in np.unique(t['day'])],
            'per_road':[{'anonymous_road_id':road+1,'candidate':metrics(a[t['road']==road],pred[t['road']==road]),'persistence':metrics(a[t['road']==road],p[t['road']==road])} for road in range(214)],
            'slow_traffic_under_20kph':{'candidate':metrics(a[a<20],pred[a<20]),'persistence':metrics(a[a<20],p[a<20])}}
        for label,lag in [('previous_day',144),('previous_week',1008)]:
            ix=t['target']-lag; valid=(ix>=0)&(ix<=t['origin']); historical=grid[np.maximum(ix,0),t['road']];valid&=np.isfinite(historical)
            row[label]={'baseline':metrics(a[valid],historical[valid]),'candidate_same_rows':metrics(a[valid],pred[valid]),'persistence_same_rows':metrics(a[valid],p[valid])}
        assert f['target'].max()<v['origin'].min() and v['target'].max()<c['origin'].min() and c['target'].max()<t['origin'].min()
        report['horizons'].append(row)
        print('MAE',row['candidate']['mae_kph'],row['persistence']['mae_kph'],'CI',row['paired_day_bootstrap']['ci95'],flush=True)
    return report


def main():
    report={'evaluated_at':datetime.now(timezone.utc).isoformat(),'protocol':'historical-expansion-protocol-2026-10-24.json','guangzhou':benchmark_guangzhou()}
    OUTPUT.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    print(OUTPUT,flush=True)

if __name__=='__main__':main()
