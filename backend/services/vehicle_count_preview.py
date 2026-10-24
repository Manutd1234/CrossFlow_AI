"""Bounded local detector bridge for an operator preview, separate from live traffic."""
import base64
import json
import math
import os
from pathlib import Path
import subprocess
import threading

ROOT=Path(__file__).resolve().parents[2]
MAX_IMAGE_BYTES=4*1024*1024
MAX_REPLY_BYTES=512*1024
INFERENCE_TIMEOUT_SECONDS=45
_LOCK=threading.Lock()
PREVIEW_RELEASE_PATH=ROOT/'backend/data/research/vehicle_counter_preview_release.json'


class PreviewUnavailable(RuntimeError):pass
class PreviewBusy(RuntimeError):pass
class PreviewInputError(ValueError):pass


def configuration():
    runtime=Path(os.environ.get('CROSSFLOW_VISION_PYTHON',str(ROOT/'.venv-vision/bin/python'))).expanduser()
    default_model=ROOT/'backend/data/research/detector_training'
    # A local, bounded receipt may opt into the one reviewed experimental pilot.
    # No request can choose this path or turn on an operational traffic model.
    try:
        if PREVIEW_RELEASE_PATH.stat().st_size<=8192:
            release=json.loads(PREVIEW_RELEASE_PATH.read_bytes())
            if (isinstance(release,dict) and release.get('schema')=='crossflow.vehicle_preview_release.v1'
                and release.get('model_directory')=='batam_detector_training'
                and release.get('preview_gate_passed') is True
                and release.get('production_eligible') is False and release.get('batam_validated') is False):
                default_model=ROOT/'backend/data/research/batam_detector_training'
    except (OSError,ValueError):
        pass
    model=Path(os.environ.get('CROSSFLOW_VISION_MODEL_DIR',str(default_model))).expanduser()
    if not runtime.is_absolute():runtime=ROOT/runtime
    if not model.is_absolute():model=ROOT/model
    return runtime,model


def _invoke(mode,data=None):
    runtime,model=configuration()
    if not runtime.is_file() or not model.is_dir():raise PreviewUnavailable('The local detector runtime is not configured.')
    if os.environ.get('VERCEL') or os.environ.get('AWS_LAMBDA_FUNCTION_NAME'):
        raise PreviewUnavailable('This detector requires a dedicated local inference runtime.')
    request=None if data is None else json.dumps({'image_base64':base64.b64encode(data).decode('ascii')}).encode()
    try:
        # No shell, download, provider request or request-controlled executable/path.
        result=subprocess.run([str(runtime),str(ROOT/'scripts/vehicle_counter_worker.py'),mode,str(model)],
            input=request,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=INFERENCE_TIMEOUT_SECONDS,
            env={**os.environ,'OMP_NUM_THREADS':'2','MKL_NUM_THREADS':'2'})
    except (OSError,subprocess.TimeoutExpired) as error:
        raise PreviewUnavailable('The detector could not complete this preview. Try a smaller image.') from error
    if result.returncode or len(result.stdout)>MAX_REPLY_BYTES:
        raise PreviewUnavailable('The detector returned an invalid response.')
    try:reply=json.loads(result.stdout)
    except (ValueError,TypeError) as error:raise PreviewUnavailable('The detector response was incomplete.') from error
    if not isinstance(reply,dict) or reply.get('ok') is not True:
        if isinstance(reply,dict) and reply.get('code')=='invalid_image':raise PreviewInputError('Use a valid JPEG, PNG or WebP image within the pixel limit.')
        raise PreviewUnavailable('The detector model is not available for preview.')
    value=reply.get('result')
    if not isinstance(value,dict):raise PreviewUnavailable('The detector response was incomplete.')
    return value


def preview_status():
    try:
        value=_invoke('--status')
        return {'available':bool(value.get('available',value.get('ready',value.get('artifacts_available') and value.get('enabled')))),
            'experimental':True,'production_eligible':False,'batam_validated':False,
            'model_id':str(value.get('model_id','vehicle_detector_experimental')),
            'scope':'single_image_vehicle_count_preview',
            'message':'Experimental detector; Batam camera accuracy and queues are not validated.',
            'detector_status':value}
    except PreviewUnavailable as error:
        return {'available':False,'experimental':True,'production_eligible':False,'batam_validated':False,
            'scope':'single_image_vehicle_count_preview','message':str(error)}


def count_preview(data,content_type):
    if content_type not in ('image/jpeg','image/png','image/webp'):raise PreviewInputError('Choose a JPEG, PNG or WebP image.')
    if not isinstance(data,bytes) or not 1<=len(data)<=MAX_IMAGE_BYTES:raise PreviewInputError('Choose an image no larger than 4 MiB.')
    if not _LOCK.acquire(blocking=False):raise PreviewBusy('Another vehicle-count preview is running. Try again shortly.')
    try:value=_invoke('--count',data)
    finally:_LOCK.release()
    # Keep the service contract explicit; confidence scores are not accuracy.
    image=value.get('image');detections=value.get('detections');count=value.get('vehicle_count')
    if not isinstance(image,dict) or not all(isinstance(image.get(k),int) and not isinstance(image[k],bool) and 1<=image[k]<=6000 for k in ('width','height')):
        raise PreviewUnavailable('The detector returned invalid image dimensions.')
    if image['width']*image['height']>12_000_000:raise PreviewUnavailable('Image dimensions exceed detector bounds.')
    if not isinstance(detections,list) or len(detections)>1000 or isinstance(count,bool) or not isinstance(count,int) or count!=len(detections):
        raise PreviewUnavailable('The detector returned an invalid count.')
    for row in detections:
        if not isinstance(row,dict) or not isinstance(row.get('class_name'),str) or row.get('class_name') not in {'vehicle','bicycle','car','motorcycle','bus','truck'}:raise PreviewUnavailable('Invalid vehicle labels.')
        score=row.get('score');box=row.get('box')
        if isinstance(score,bool) or not isinstance(score,(int,float)) or not math.isfinite(score) or not 0<=score<=1:raise PreviewUnavailable('Invalid detector confidence.')
        if not isinstance(box,list) or len(box)!=4 or any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or not 0<=x<=1 for x in box) or not (box[0]<box[2] and box[1]<box[3]):raise PreviewUnavailable('Invalid detector boxes.')
    latency=value.get('latency_ms')
    if latency is not None and (isinstance(latency,bool) or not isinstance(latency,(int,float)) or not math.isfinite(latency) or latency<0):
        raise PreviewUnavailable('Invalid detector latency.')
    model_id=value.get('model_id','experimental_vehicle_detector')
    if not isinstance(model_id,str) or not 1<=len(model_id)<=128:raise PreviewUnavailable('Invalid detector identity.')
    policy=value.get('count_policy')
    if policy is not None and (not isinstance(policy,dict) or type(policy.get('min_long_side_px')) is not int
        or policy.get('min_long_side_px')!=12 or type(policy.get('min_short_side_px')) is not int
        or policy.get('min_short_side_px')!=4 or policy.get('scope')!='size_filtered_visible_vehicle_boxes_per_image'):
        raise PreviewUnavailable('Invalid detector count policy.')
    truncated=value.get('may_be_truncated',False)
    if not isinstance(truncated,bool) or value.get('max_detections',100)!=100:
        raise PreviewUnavailable('Invalid detector count limit.')
    result={'vehicle_count':count,'image':{'width':image['width'],'height':image['height']},
        'detections':[{'class_name':row['class_name'],'score':row['score'],'box':row['box']} for row in detections],
        'model_id':model_id,'latency_ms':latency,'experimental':True,'production_eligible':False,'batam_validated':False,
        'scope':'single_image_vehicle_count_preview','image_retained':False,
        'max_detections':100,'may_be_truncated':truncated,
        'note':'Detected vehicle boxes in this image. This is not flow, queue length, speed or verified ground truth.'}
    if policy is not None:
        result['count_policy']={'min_long_side_px':12,'min_short_side_px':4,'scope':'size_filtered_visible_vehicle_boxes_per_image'}
        result['note']='Batam photo pilot: counts boxes at least 12 pixels long and 4 pixels short. Labels were assistant-reviewed; accuracy is not human-certified. This is not flow, queue length or speed.'
    if truncated:
        result['note']+=' The detector reached its 100-box limit; this count may be incomplete.'
    return result
