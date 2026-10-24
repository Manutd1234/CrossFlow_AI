"""Local opt-in YOLO26 vehicle adapter; optional libraries load only on inference.

Only trusted, hash-pinned local checkpoints are accepted. This module never
fetches a checkpoint, contacts a cloud API, saves an image, or calls detector
confidence a measured accuracy. Ultralytics code/weights use AGPL-3.0; see the
retained official license receipt before distributing a derivative application.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import warnings

from services.vehicle_counter import inside_roi, polygon_roi

DEFAULT_MODEL_DIR = Path(__file__).resolve().parents[1] / "data/research/yolo26_batam_refinement"
SELECTION_FILE = "selection_frozen_before_reused_batam_test.json"
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_PIXELS = 12_000_000
COCO_VEHICLES = {1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
MIN_LONG_SIDE, MIN_SHORT_SIDE = 12, 4


def inference_policy(metadata):
    return {"schema": "crossflow.yolo26_inference_policy.v1", "architecture": metadata["architecture"],
            "ultralytics_version": metadata["ultralytics_version"], "num_classes": metadata["num_classes"],
            "vehicle_classes": metadata["vehicle_classes"], "imgsz": metadata["imgsz"],
            "score_threshold": metadata["score_threshold"], "generic_vehicle_nms_iou": .5,
            "min_vehicle_long_side_px": MIN_LONG_SIDE, "min_vehicle_short_side_px": MIN_SHORT_SIDE,
            "max_detections": 100, "image_orientation": "EXIF_transpose", "roi_rule": "normalized_box_center"}


def inference_policy_sha256(metadata):
    return hashlib.sha256(json.dumps(inference_policy(metadata), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def runtime_model_id(metadata):
    # Camera validation binds the complete inference policy, not only weights.
    return metadata["architecture"] + "-" + metadata["model_sha256"][:16] + "-p" + inference_policy_sha256(metadata)[:16]


def _overlap(a, b):
    intersection = max(0., min(a[2], b[2]) - max(a[0], b[0])) * max(0., min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / union if union > 0 else 0.


def postprocess_detections(boxes, scores, class_ids, *, width, height, classes, threshold, roi=None):
    """Shared offline/serving policy: generic NMS, native pixel size, ROI centre."""
    if not (len(boxes) == len(scores) == len(class_ids)) or len(boxes) > 1000:
        raise ValueError("Invalid YOLO26 detection lengths")
    candidates = []
    for box, score, class_id in zip(boxes, scores, class_ids):
        if (not isinstance(box, (list, tuple)) or len(box) != 4
                or type(score) not in (int, float) or not math.isfinite(score) or not threshold <= score <= 1
                or type(class_id) not in (int, float) or not math.isfinite(class_id)
                or int(class_id) != class_id or int(class_id) not in classes
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in box)):
            continue
        box = [max(0., min(width if i % 2 == 0 else height, float(v))) for i, v in enumerate(box)]
        w, h = box[2] - box[0], box[3] - box[1]
        if min(w, h) < MIN_SHORT_SIDE - 1e-6 or max(w, h) < MIN_LONG_SIDE - 1e-6:
            continue
        candidates.append((float(score), box, int(class_id)))
    candidates.sort(key=lambda row: (-row[0], row[1], row[2]))
    retained = []
    for score, box, class_id in candidates:
        if any(_overlap(box, previous["box_xyxy"]) > .5 for previous in retained):
            continue
        normalized = [v / (width if i % 2 == 0 else height) for i, v in enumerate(box)]
        if not inside_roi((normalized[0] + normalized[2]) / 2, (normalized[1] + normalized[3]) / 2, roi):
            continue
        retained.append({"box_xyxy": box, "box": normalized, "box_normalized_xyxy": normalized,
                         "score": score, "class_id": class_id, "class_name": classes[class_id]})
        if len(retained) >= 100:
            break
    return retained


def decode_image(image_bytes):
    if not isinstance(image_bytes, bytes) or not 0 < len(image_bytes) <= MAX_IMAGE_BYTES:
        raise ValueError("Expected an image within the 4 MiB bound")
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image_bytes)) as image:
                if (image.format not in {"JPEG", "PNG", "WEBP"} or getattr(image, "n_frames", 1) != 1
                        or min(image.size) < 2 or max(image.size) > 6000 or image.width * image.height > MAX_IMAGE_PIXELS):
                    raise ValueError("Unsupported image format or dimensions")
                image.load()
                return ImageOps.exif_transpose(image).convert("RGB")
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValueError("Image cannot be safely decoded") from error


class YOLO26VehicleCounter:
    def __init__(self, model_dir=None, *, allow_experimental=False):
        self.model_dir = Path(model_dir) if model_dir else DEFAULT_MODEL_DIR
        self.allow_experimental = allow_experimental
        self._model = None
        self._selection = None
        self._lock = threading.Lock()

    def _metadata(self):
        path = self.model_dir / SELECTION_FILE
        try:
            if path.stat().st_size > 128 * 1024:
                raise ValueError("Selection exceeds bounds")
            value = json.loads(path.read_bytes())
        except (OSError, ValueError) as error:
            raise RuntimeError("YOLO26 selection metadata is unavailable") from error
        if (not isinstance(value, dict) or value.get("schema") != "crossflow.yolo26_vehicle_selection.v1"
                or value.get("architecture") not in {"yolo26n", "yolo26s", "yolo26m"} or value.get("ultralytics_version") != "8.4.184"
                or not isinstance(value.get("model_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["model_sha256"])
                or value.get("production_eligible") is not False or value.get("batam_validated") is not False
                or value.get("human_certified") is not False or value.get("annotation_origin") != "ai_visual_blind"
                or value.get("min_vehicle_long_side_px") != MIN_LONG_SIDE or value.get("min_vehicle_short_side_px") != MIN_SHORT_SIDE
                or type(value.get("num_classes")) is not int or value["num_classes"] not in (1, 80)
                or type(value.get("imgsz")) is not int or value["imgsz"] not in (640, 960, 1280) or value.get("license") != "AGPL-3.0"
                or type(value.get("score_threshold")) not in (int, float) or not math.isfinite(value["score_threshold"])
                or not 0 < value["score_threshold"] < 1):
            raise RuntimeError("Unsupported YOLO26 serving policy")
        expected = {str(k): v for k, v in COCO_VEHICLES.items()} if value.get("num_classes") == 80 else {"0": "vehicle"} if value.get("num_classes") == 1 else None
        if value.get("vehicle_classes") != expected or expected is None:
            raise RuntimeError("Unsupported YOLO26 class mapping")
        _identifier = value.get("model_id")
        if not isinstance(_identifier, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", _identifier):
            raise RuntimeError("Invalid YOLO26 model identity")
        try:
            size = (self.model_dir / "model.pt").stat().st_size
            if not 100_000 <= size <= 100 * 1024 * 1024:
                raise RuntimeError("YOLO26 checkpoint size exceeds bounds")
        except OSError as error:
            raise RuntimeError("YOLO26 checkpoint is unavailable") from error
        return value

    def status(self):
        try:
            metadata = self._metadata()
            available = True
        except RuntimeError:
            metadata, available = {}, False
        return {"status": "experimental_available" if available and self.allow_experimental else "disabled" if available else "unavailable",
                "enabled": available and self.allow_experimental, "artifacts_available": available,
                "architecture": metadata.get("architecture"), "imgsz": metadata.get("imgsz"), "model_id": runtime_model_id(metadata) if available else None,
                "weights_selection_id": metadata.get("model_id"), "inference_policy_sha256": inference_policy_sha256(metadata) if available else None, "model_sha256": metadata.get("model_sha256"),
                "production_eligible": False, "batam_validated": False, "human_certified": False,
                "supports_speed": False, "supports_queue_length": False, "supports_unique_vehicle_flow": False,
                "license": "AGPL-3.0", "license_url": "https://www.ultralytics.com/license",
                "scope": "Local YOLO26 counter; small assistant-labeled Batam pilot, camera validation pending"}

    def _load(self, metadata):
        if self._model is not None:
            if metadata["model_sha256"] != self._selection["model_sha256"] or inference_policy_sha256(metadata) != inference_policy_sha256(self._selection):
                raise RuntimeError("YOLO26 selection changed; restart the camera worker")
            return
        path = self.model_dir / "model.pt"
        if hashlib.sha256(path.read_bytes()).hexdigest() != metadata["model_sha256"]:
            raise RuntimeError("YOLO26 checkpoint integrity check failed")
        # Inference never auto-installs, phones home, or downloads missing weights.
        os.environ["YOLO_OFFLINE"] = "true"
        os.environ["YOLO_AUTOINSTALL"] = "false"
        os.environ.setdefault("YOLO_CONFIG_DIR", str(self.model_dir / "runtime_config"))
        os.environ.setdefault("MPLCONFIGDIR", str(self.model_dir / "runtime_config/matplotlib"))
        import ultralytics
        if ultralytics.__version__ != metadata["ultralytics_version"]:
            raise RuntimeError("YOLO26 runtime version differs from frozen training receipt")
        from ultralytics import YOLO, settings
        settings.update({"sync": False, "clearml": False, "comet": False, "dvc": False, "mlflow": False,
                         "raytune": False, "tensorboard": False, "wandb": False})
        self._model = YOLO(str(path), task="detect")
        self._selection = metadata

    def count_image(self, image_bytes, *, roi=None):
        if not self.allow_experimental:
            raise RuntimeError("Experimental YOLO26 counter is disabled")
        polygon = polygon_roi(roi)
        rgb = decode_image(image_bytes)
        metadata = self._metadata()
        started = time.monotonic()
        with self._lock:
            self._load(metadata)
            results = self._model.predict(source=rgb, imgsz=metadata["imgsz"], conf=metadata["score_threshold"],
                iou=.5, max_det=300, classes=[int(k) for k in metadata["vehicle_classes"]], agnostic_nms=True,
                device="cpu", verbose=False, save=False, save_txt=False, save_crop=False, stream=False)
            boxes = results[0].boxes
            detections = postprocess_detections(boxes.xyxy.cpu().tolist(), boxes.conf.cpu().tolist(), boxes.cls.cpu().tolist(),
                width=rgb.width, height=rgb.height, classes={int(k): v for k, v in metadata["vehicle_classes"].items()},
                threshold=metadata["score_threshold"], roi=polygon)
        latency = (time.monotonic() - started) * 1000
        return {"schema": "crossflow.vehicle_counter.v1", "status": "experimental", "experimental": True,
                "vehicle_count": len(detections), "detections": detections,
                "image_width": rgb.width, "image_height": rgb.height, "image": {"width": rgb.width, "height": rgb.height},
                "model_id": runtime_model_id(metadata), "weights_selection_id": metadata["model_id"],
                "inference_policy_sha256": inference_policy_sha256(metadata), "model_name": metadata["selected_model"], "model_sha256": metadata["model_sha256"],
                "latency_ms": latency, "latency_ms_including_first_load": latency, "max_detections": 100,
                "may_be_truncated": len(detections) >= 100 or len(boxes) >= 300, "roi_applied": polygon is not None,
                "production_eligible": False, "batam_validated": False, "human_certified": False,
                "scores_are_calibrated_accuracy": False, "count_is_flow_or_queue": False,
                "license": "AGPL-3.0", "scope": self.status()["scope"]}
