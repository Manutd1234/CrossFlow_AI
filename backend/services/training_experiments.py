"""Read bounded offline training metadata; never fit or deserialize model code."""
import hashlib
import json
from pathlib import Path

RESULTS = Path(__file__).resolve().parents[1]/'data/research/ml_training'


def training_experiment_status(directory=RESULTS):
    status, manifest = directory/'status.json', directory/'manifest.json'
    if not status.is_file() and not manifest.is_file():
        return dict(schema_version=1,status='not_run',training_started=False,model_activated=False,experiments=[])
    if not status.is_file() or not manifest.is_file() or status.stat().st_size>65536 or manifest.stat().st_size>262144:
        raise ValueError('Offline experiment summary unavailable')
    with manifest.open('rb') as file:
        manifest_raw = file.read(262145)
    if len(manifest_raw)>262144:
        raise ValueError('Oversized experiment manifest')
    receipt = json.loads(manifest_raw)
    if not isinstance(receipt,dict) or receipt.get('schema_version')!=1 or receipt.get('model_activated') is not False:
        raise ValueError('Unsupported experiment manifest')
    files = receipt.get('files')
    if not isinstance(files,list) or any(not isinstance(row,dict) for row in files):
        raise ValueError('Invalid experiment receipts')
    entries = [row for row in files if row.get('path')=='status.json']
    with status.open('rb') as file:
        raw = file.read(65537)
    if len(raw)>65536:
        raise ValueError('Oversized experiment summary')
    if len(entries)!=1 or entries[0].get('sha256')!=hashlib.sha256(raw).hexdigest() or entries[0].get('bytes')!=len(raw):
        raise ValueError('Offline experiment summary checksum mismatch')
    result = json.loads(raw)
    if not isinstance(result,dict) or not isinstance(result.get('experiments'),list) or any(not isinstance(item,dict) for item in result['experiments']):
        raise ValueError('Invalid experiment summary')
    if (result.get('schema_version')!=1 or result.get('status')!='completed'
        or result.get('model_activated') is not False or result.get('citywide_Batam_accuracy_validated') is not False
        or any(item.get('production_eligible') is not False for item in result.get('experiments',[]))):
        raise ValueError('Invalid offline experiment scope')
    return result
