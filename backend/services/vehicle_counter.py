"""Local, opt-in experimental image detector; importing this never loads torch.

Counts visible vehicle boxes in one image. It does not estimate road flow, queue
length, speed, unique vehicles over time, or validated Batam congestion.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import threading
import time
import warnings
from pathlib import Path
from typing import Any

DEFAULT_MODEL_DIR = Path(__file__).resolve().parents[1] / "data/research/detector_training"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 12_000_000
MAX_IMAGE_SIDE = 6000
COCO_VEHICLES = {2: "bicycle", 3: "car", 4: "motorcycle", 6: "bus", 8: "truck"}


def polygon_roi(roi: list[list[float]] | None) -> list[tuple[float, float]] | None:
    if roi is None:
        return None
    if not isinstance(roi, list) or not 3 <= len(roi) <= 32:
        raise ValueError("ROI must contain 3 to 32 normalized [x, y] vertices")
    points = []
    for pair in roi:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("ROI vertices must be [x, y] pairs")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in pair):
            raise ValueError("ROI coordinates must be finite numbers between 0 and 1")
        points.append((float(pair[0]), float(pair[1])))
    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1]))
    if len(set(points)) != len(points):
        raise ValueError("ROI must not repeat vertices")
    if abs(area) < 1e-9:
        raise ValueError("ROI must have nonzero area")
    # Reject crossing nonadjacent edges; a single simple ROI has clear semantics.
    def orientation(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    def on_segment(a, b, point):
        return (abs(orientation(a, b, point)) <= 1e-12
                and min(a[0], b[0]) <= point[0] <= max(a[0], b[0])
                and min(a[1], b[1]) <= point[1] <= max(a[1], b[1]))
    for i, a in enumerate(points):
        b = points[(i + 1) % len(points)]
        if a == b:
            raise ValueError("ROI has a zero-length edge")
        for j in range(i + 1, len(points)):
            if j == i + 1 or (i == 0 and j == len(points) - 1):
                continue
            c, d = points[j], points[(j + 1) % len(points)]
            intersects = (orientation(a, b, c) * orientation(a, b, d) < 0 and orientation(c, d, a) * orientation(c, d, b) < 0)
            if intersects or on_segment(a, b, c) or on_segment(a, b, d) or on_segment(c, d, a) or on_segment(c, d, b):
                raise ValueError("ROI must not cross itself")
    return points


def inside_roi(x: float, y: float, points: list[tuple[float, float]] | None) -> bool:
    if points is None:
        return True
    inside = False
    for a, b in zip(points, points[1:] + points[:1]):
        cross = (x - a[0]) * (b[1] - a[1]) - (y - a[1]) * (b[0] - a[0])
        if abs(cross) <= 1e-12 and min(a[0], b[0]) <= x <= max(a[0], b[0]) and min(a[1], b[1]) <= y <= max(a[1], b[1]):
            return True
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            inside = not inside
    return inside


def filter_detections(output: dict, *, width: int, height: int, threshold: float,
                      vehicle_classes: dict[int, str], nms_iou: float = .5,
                      roi: list[tuple[float, float]] | None = None) -> list[dict[str, Any]]:
    """Identical generic-vehicle NMS in offline evaluation and app inference."""
    import torch
    from torchvision.ops import nms
    boxes = output["boxes"].detach().cpu().clone()
    scores = output["scores"].detach().cpu()
    labels = output["labels"].detach().cpu()
    keep = torch.isfinite(boxes).all(1) & torch.isfinite(scores) & (scores >= threshold)
    keep &= torch.tensor([int(v) in vehicle_classes for v in labels], dtype=torch.bool)
    boxes[:, [0, 2]] = boxes[:, [0, 2]].clamp(0, width)
    boxes[:, [1, 3]] = boxes[:, [1, 3]].clamp(0, height)
    keep &= (boxes[:, 2] > boxes[:, 0]) & (boxes[:, 3] > boxes[:, 1])
    boxes, scores, labels = boxes[keep], scores[keep], labels[keep]
    indices = nms(boxes, scores, nms_iou).tolist()
    result = []
    for index in indices:
        x1, y1, x2, y2 = map(float, boxes[index])
        if not inside_roi((x1 + x2) / (2 * width), (y1 + y2) / (2 * height), roi):
            continue
        normalized = [x1 / width, y1 / height, x2 / width, y2 / height]
        result.append({"box_xyxy": [x1, y1, x2, y2], "box": normalized,
                       "box_normalized_xyxy": normalized,
                       "score": float(scores[index]), "class_id": int(labels[index]),
                       "class_name": vehicle_classes[int(labels[index])]})
        if len(result) >= 100:
            break
    return result


class VehicleCounter:
    def __init__(self, model_dir: Path | None = None, *, allow_experimental: bool = False):
        self.model_dir = Path(model_dir) if model_dir else DEFAULT_MODEL_DIR
        self.allow_experimental = allow_experimental
        self._lock = threading.Lock()
        self._model = None
        self._selection = None

    def status(self) -> dict[str, Any]:
        selection_path = self.model_dir / "selection_frozen_before_reused_diagnostic.json"
        available = selection_path.is_file() and (self.model_dir / "model.pt").is_file()
        metadata = {}
        if available:
            try:
                metadata = json.loads(selection_path.read_text())
                if not isinstance(metadata, dict):
                    metadata = {}
                available = isinstance(metadata.get("model_sha256"), str)
            except (OSError, ValueError):
                available = False
                metadata = {}
        return {"status": "experimental_available" if available and self.allow_experimental else "disabled" if available else "unavailable",
                "artifacts_available": available, "enabled": available and self.allow_experimental,
                "model_id": metadata.get("selected_model"), "architecture": metadata.get("architecture"),
                "score_threshold": metadata.get("score_threshold"), "model_sha256": metadata.get("model_sha256"),
                "production_eligible": False, "batam_validated": False,
                "target": "visible_vehicle_boxes_per_image", "supports_speed": False,
                "supports_queue_length": False, "supports_unique_vehicle_flow": False,
                "scope": "Research preview; South Asian dashcam development and reused US aerial diagnostic only",
                "license_clearance": "Research use documented; production weight/data rights not certified"}

    def _load(self):
        if not self.allow_experimental:
            raise RuntimeError("Experimental vehicle counter is disabled")
        if self._model is not None:
            return
        import torch
        from torchvision.models.detection import ssdlite320_mobilenet_v3_large, fasterrcnn_resnet50_fpn_v2
        path = self.model_dir / "model.pt"
        selection = json.loads((self.model_dir / "selection_frozen_before_reused_diagnostic.json").read_text())
        if hashlib.sha256(path.read_bytes()).hexdigest() != selection["model_sha256"]:
            raise RuntimeError("Vehicle detector checkpoint integrity check failed")
        if selection["architecture"] not in {"ssdlite320_mobilenet_v3_large", "fasterrcnn_resnet50_fpn_v2"} or selection["num_classes"] not in (3, 91):
            raise RuntimeError("Unsupported detector architecture")
        if selection["architecture"] == "ssdlite320_mobilenet_v3_large":
            model = ssdlite320_mobilenet_v3_large(weights=None, weights_backbone=None,
                        num_classes=selection["num_classes"], score_thresh=.001, detections_per_img=300)
        else:
            model = fasterrcnn_resnet50_fpn_v2(weights=None, weights_backbone=None,
                        num_classes=selection["num_classes"], box_score_thresh=.001, box_detections_per_img=300)
        model.load_state_dict(torch.load(path, map_location="cpu", weights_only=True))
        self._model = model.eval()
        self._selection = selection

    def count_image(self, image_bytes: bytes, *, roi: list[list[float]] | None = None) -> dict[str, Any]:
        if not self.allow_experimental:
            raise RuntimeError("Experimental vehicle counter is disabled")
        if not isinstance(image_bytes, bytes) or not 0 < len(image_bytes) <= MAX_IMAGE_BYTES:
            raise ValueError("Image must be nonempty bytes and at most 8 MiB")
        polygon = polygon_roi(roi)
        # Optional dependencies stay lazy; no API contacts or automatic downloads.
        from PIL import Image, ImageOps, UnidentifiedImageError
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(image_bytes)) as image:
                    if image.format not in {"JPEG", "PNG", "WEBP"}:
                        raise ValueError("Expected JPEG, PNG, or WebP image")
                    width, height = image.size
                    if width < 2 or height < 2 or width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE or width * height > MAX_IMAGE_PIXELS:
                        raise ValueError("Image dimensions exceed detector bounds")
                    if getattr(image, "n_frames", 1) != 1:
                        raise ValueError("Animated images are not accepted")
                    image.load()
                    rgb = ImageOps.exif_transpose(image).convert("RGB")
                    width, height = rgb.size
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
            raise ValueError("Image cannot be safely decoded") from error
        import torch
        from torchvision.transforms.functional import to_tensor
        started = time.monotonic()
        with self._lock:
            self._load()
            with torch.inference_mode():
                output = self._model([to_tensor(rgb)])[0]
            selection = self._selection
            detections = filter_detections(output, width=width, height=height,
                threshold=selection["score_threshold"], nms_iou=selection["vehicle_nms_iou"],
                vehicle_classes={int(k): v for k, v in selection["vehicle_classes"].items()}, roi=polygon)
        elapsed_ms = (time.monotonic() - started) * 1000
        return {"schema": "crossflow.vehicle_counter.v1", "status": "experimental", "experimental": True,
                "vehicle_count": len(detections), "detections": detections,
                "image_width": width, "image_height": height, "image": {"width": width, "height": height}, "roi_applied": polygon is not None,
                "roi_rule": "box center inside/on normalized polygon",
                "latency_ms_including_first_load": elapsed_ms, "latency_ms": elapsed_ms,
                "model_sha256": selection["model_sha256"], "model_name": selection["selected_model"],
                "model_id": selection["selected_model"],
                "score_threshold": selection["score_threshold"], "production_eligible": False,
                "max_detections": 100, "may_be_truncated": len(detections) >= 100,
                "scores_are_calibrated_accuracy": False,
                "batam_validated": False, "count_is_flow_or_queue": False,
                "scope": self.status()["scope"]}
