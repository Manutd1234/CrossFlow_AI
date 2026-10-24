"""Optional Batam pilot adapter; preserves the earlier frozen detector service."""
from __future__ import annotations

import json
import math
import re

from services.vehicle_counter import COCO_VEHICLES, VehicleCounter

COUNT_POLICY = {"min_long_side_px": 12, "min_short_side_px": 4,
                "scope": "size_filtered_visible_vehicle_boxes_per_image"}
PILOT_SCOPE = "Small Batam photo pilot with assistant-reviewed labels; human accuracy validation pending"


def apply_count_policy(detections):
    kept = []
    for row in detections:
        box = row.get("box_xyxy")
        if (not isinstance(box, list) or len(box) != 4
                or any(type(x) not in (float, int) or not math.isfinite(x) for x in box)):
            raise RuntimeError("Invalid pilot detector box")
        width, height = box[2] - box[0], box[3] - box[1]
        if width <= 0 or height <= 0:
            raise RuntimeError("Invalid pilot detector extent")
        if min(width, height) >= 4 - 1e-6 and max(width, height) >= 12 - 1e-6:
            kept.append(row)
    return kept


class BatamVehicleCounter(VehicleCounter):
    def _pilot_metadata(self):
        path = self.model_dir / "selection_frozen_before_batam_test.json"
        alias = self.model_dir / "selection_frozen_before_reused_diagnostic.json"
        try:
            if path.stat().st_size > 128 * 1024 or alias.stat().st_size > 128 * 1024:
                raise RuntimeError("Pilot selection metadata exceeds bounds")
            raw = path.read_bytes()
            if raw != alias.read_bytes():
                raise RuntimeError("Pilot selection aliases differ")
            value = json.loads(raw)
        except (OSError, ValueError) as error:
            raise RuntimeError("Pilot selection metadata is unavailable") from error
        if (not isinstance(value, dict) or value.get("schema") != "crossflow.batam_vehicle_selection.v1"
                or value.get("architecture") != "fasterrcnn_resnet50_fpn_v2"
                or type(value.get("num_classes")) is not int or value.get("num_classes") not in (3, 91)
                or not isinstance(value.get("model_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["model_sha256"])
                or type(value.get("min_vehicle_long_side_px")) is not int or value["min_vehicle_long_side_px"] != 12
                or type(value.get("min_vehicle_short_side_px")) is not int or value["min_vehicle_short_side_px"] != 4
                or value.get("annotation_origin") != "ai_visual_blind"
                or value.get("human_certified") is not False
                or value.get("production_eligible") is not False or value.get("batam_validated") is not False):
            raise RuntimeError("Unsupported pilot selection policy")
        for key in ("score_threshold", "vehicle_nms_iou"):
            number = value.get(key)
            if type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 1:
                raise RuntimeError("Invalid pilot detector threshold")
        expected_classes = {str(k): v for k, v in (COCO_VEHICLES if value["num_classes"] == 91 else {1: "vehicle"}).items()}
        if value.get("vehicle_classes") != expected_classes:
            raise RuntimeError("Invalid pilot vehicle class mapping")
        return value

    def status(self):
        try:
            self._pilot_metadata()
            value = super().status()
        except RuntimeError:
            value = {"status": "unavailable", "artifacts_available": False, "enabled": False}
        return {**value, "scope": PILOT_SCOPE, "target": COUNT_POLICY["scope"],
                "count_policy": dict(COUNT_POLICY), "annotation_origin": "ai_visual_blind", "human_certified": False,
                "production_eligible": False, "batam_validated": False, "supports_speed": False,
                "supports_queue_length": False, "supports_unique_vehicle_flow": False}

    def count_image(self, image_bytes, *, roi=None):
        if not self.allow_experimental:
            raise RuntimeError("Experimental vehicle counter is disabled")
        self._pilot_metadata()
        result = super().count_image(image_bytes, roi=roi)
        boxes = apply_count_policy(result["detections"])
        return {**result, "vehicle_count": len(boxes), "detections": boxes,
                "count_policy": dict(COUNT_POLICY), "scope": PILOT_SCOPE,
                "annotation_origin": "ai_visual_blind", "human_certified": False,
                "production_eligible": False, "batam_validated": False}
