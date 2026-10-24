"""Small read-only summaries of fixed, reviewed offline benchmark reports."""
import json, math
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]/'docs/verification'


def _read(name):
    path=ROOT/name
    if not path.exists():return None
    if path.stat().st_size>2*1024*1024:raise ValueError('Benchmark report exceeds size bound')
    return json.loads(path.read_bytes())


def _metric(value):
    if not isinstance(value,dict) or not isinstance(value.get('samples'),int) or value['samples']<1:raise ValueError('Invalid benchmark count')
    for field in ('mae_kph','rmse_kph','within_5_kph'):
        x=value.get(field)
        if isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or x<0:raise ValueError('Invalid benchmark metric')
    if value['within_5_kph']>1:raise ValueError('Invalid tolerance fraction')
    return {key:value[key] for key in ('samples','mae_kph','rmse_kph','within_5_kph')}


def summary():
    results=[]
    try:
        value=_read('grand-batam-backtest-2026-10-24.json')
        if value:
            if value['synthetic_rows']!=0 or value['source_rows']!=66 or value['horizon_minutes']!=60:raise ValueError('Survey scope differs')
            results.append({'id':'grand_batam_survey','name':'Grand Batam survey','geography':'Batam, Indonesia','source_rows':66,'source_days':4,
                'source_url':value['source_url'],'scope':'Exploratory three-road survey; one test day. Not five-corridor or citywide validation.',
                'horizons':[{'horizon_minutes':60,'candidate':_metric(value['candidate']),'persistence':_metric(value['persistence']),
                 'mae_improvement_percent':value['mae_improvement_percent'],'test_days':1,'improvement_ci95_kph':None}]})
        value=_read('historical-expansion-backtest-2026-10-24.json')
        if value:
            value=value['guangzhou']
            if value['synthetic_rows']!=0 or value['geography']!='Guangzhou, China; not Batam' or value['days']!=61:raise ValueError('Foreign benchmark scope differs')
            results.append({'id':'guangzhou_speed','name':'Guangzhou speed benchmark','geography':'Guangzhou, China','source_rows':value['original_speed_rows'],
                'source_days':61,'source_url':value['source_url'],'scope':'Independent methodology test on Chinese roads. Does not establish Batam accuracy.',
                'horizons':[{'horizon_minutes':h['horizon_minutes'],'candidate':_metric(h['candidate']),'persistence':_metric(h['persistence']),
                    'mae_improvement_percent':100*(h['persistence']['mae_kph']-h['candidate']['mae_kph'])/h['persistence']['mae_kph'],
                    'test_days':h['paired_day_bootstrap']['days'],'improvement_ci95_kph':h['paired_day_bootstrap']['ci95']} for h in value['horizons']]})
        return results
    except (OSError,KeyError,ValueError,TypeError,ZeroDivisionError):
        return []
