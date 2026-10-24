"""One local experimental inference; stdin/stdout JSON, no image persistence."""
import base64
import contextlib
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
MAX_IMAGE_BYTES=4*1024*1024


def counter_for_model_dir(model_dir):
    from services.vehicle_counter import VehicleCounter
    alias=model_dir/'selection_frozen_before_reused_diagnostic.json'
    try:
        if alias.stat().st_size>128*1024:raise RuntimeError('Detector selection exceeds bounds')
        metadata=json.loads(alias.read_bytes())
    except (OSError,ValueError) as error:
        raise RuntimeError('Detector selection is unavailable') from error
    if not isinstance(metadata,dict):raise RuntimeError('Invalid detector selection')
    pilot=(model_dir.name=='batam_detector_training'
        or (model_dir/'selection_frozen_before_batam_test.json').exists()
        or metadata.get('schema')=='crossflow.batam_vehicle_selection.v1'
        or 'min_vehicle_long_side_px' in metadata)
    if pilot:
        from services.batam_vehicle_counter import BatamVehicleCounter
        return BatamVehicleCounter(model_dir,allow_experimental=True)
    return VehicleCounter(model_dir,allow_experimental=True)


def main():
    if len(sys.argv)!=3 or sys.argv[1] not in ('--status','--count'):
        raise SystemExit(2)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            model_dir=Path(sys.argv[2])
            counter=counter_for_model_dir(model_dir)
            if sys.argv[1]=='--status':
                result={'ok':True,'result':counter.status()}
            else:
                raw=sys.stdin.buffer.read(MAX_IMAGE_BYTES*2+1)
                if len(raw)>MAX_IMAGE_BYTES*2:raise ValueError('Image request exceeds the size limit')
                request=json.loads(raw)
                data=base64.b64decode(request['image_base64'],validate=True)
                if not 1<=len(data)<=MAX_IMAGE_BYTES:raise ValueError('Invalid image size')
                import torch
                torch.set_num_threads(2)
                result={'ok':True,'result':counter.count_image(data,roi=request.get('roi'))}
        print(json.dumps(result,allow_nan=False,separators=(',',':')))
    except (ValueError,KeyError,TypeError):
        print(json.dumps({'ok':False,'code':'invalid_image','message':'Use a valid supported image within the image and pixel limits.'}))
    except Exception:
        print(json.dumps({'ok':False,'code':'unavailable','message':'The experimental detector is unavailable.'}))


if __name__=='__main__':main()
