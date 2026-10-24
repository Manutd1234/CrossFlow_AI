"""Authorized CCTV frame-stream worker: bounded JSONL in, numeric evidence out.

The collector supplies timestamped JPEG/PNG/WebP frames via stdin; this worker
never opens a URL, records footage, saves images, or downloads model weights.
Run in the existing optional vision environment. Camera configuration is a trusted
operator-controlled local file containing CameraConfig fields, not HTTP input.
Each line: {captured_at, image_base64, camera_stable: true|false|null}.
Camera stability must come from the collector's fixed-view monitoring; absent
stability evidence blocks congestion. Brightness checks alone cannot certify it.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from services.cctv_analysis import CCTVAnalyzer, CameraConfig

MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_LINE_BYTES = 6 * 1024 * 1024


def _emit(value):
    print(json.dumps(value, allow_nan=False, separators=(",", ":")), flush=True)


def process_frames(config, model_dir, input_stream, *, counter=None, now=None, detector_backend="yolo26"):
    """Process a trusted collector's bounded stream; optional injection for tests."""
    if not config.permission_ready:
        _emit({"ok": False, "code": "camera_disabled" if not config.enabled else "authorization_missing",
               "camera_id": config.camera_id, "images_retained": False})
        return 2
    analyzer = CCTVAnalyzer(config)
    if counter is None:
        with contextlib.redirect_stdout(sys.stderr):
            import torch
            torch.set_num_threads(2)
            if detector_backend == "yolo26":
                from services.yolo26_counter import YOLO26VehicleCounter
                counter = YOLO26VehicleCounter(model_dir=model_dir, allow_experimental=True)
            elif detector_backend == "legacy_detector":
                from vehicle_counter_worker import counter_for_model_dir
                counter = counter_for_model_dir(model_dir)
            else:
                raise ValueError("Unsupported camera detector")
    while True:
        raw = input_stream.readline(MAX_LINE_BYTES + 1)
        if not raw:
            break
        if len(raw) > MAX_LINE_BYTES:
            # Do not let the remainder of one oversized frame become new records.
            while raw and not raw.endswith(b"\n"):
                raw = input_stream.readline(MAX_LINE_BYTES + 1)
            analyzer._reset_temporal()
            _emit({"ok": False, "code": "frame_too_large", "camera_id": config.camera_id})
            continue
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or not isinstance(request.get("image_base64"), str):
                raise ValueError("Invalid frame request")
            stable = request.get("camera_stable")
            if stable is not None and type(stable) is not bool:
                raise ValueError("Invalid stability evidence")
            image_bytes = base64.b64decode(request["image_base64"], validate=True)
            if not 0 < len(image_bytes) <= MAX_IMAGE_BYTES:
                raise ValueError("Invalid frame size")
            # The detector performs strict format, pixel and animation validation
            # first. Reopen the already validated bytes only for a brightness proxy.
            with contextlib.redirect_stdout(sys.stderr):
                detected = counter.count_image(image_bytes, roi=[list(p) for p in config.roi] if config.roi else None)
                from PIL import Image, ImageOps, ImageStat
                with Image.open(io.BytesIO(image_bytes)) as image:
                    gray = ImageOps.exif_transpose(image).convert("L").resize((32, 32))
                    stats = ImageStat.Stat(gray)
                    exposure_ok = 15 <= stats.mean[0] <= 245 and stats.stddev[0] >= 4
                observed = analyzer.ingest({"captured_at": request.get("captured_at"),
                    "detections": detected["detections"], "frame_width": detected["image_width"],
                    "frame_height": detected["image_height"], "detector_id": detected["model_id"],
                    "camera_stable": stable, "exposure_ok": exposure_ok,
                    "may_be_truncated": detected.get("may_be_truncated", False),
                    "timestamp_basis": request.get("timestamp_basis", "source_clock")}, now=now() if callable(now) else now)
            _emit({"ok": True, "evidence": observed})
        except (ValueError, KeyError, TypeError, OSError):
            analyzer._reset_temporal()
            _emit({"ok": False, "code": "invalid_frame", "camera_id": config.camera_id})
        except Exception:
            analyzer._reset_temporal()
            _emit({"ok": False, "code": "detector_unavailable", "camera_id": config.camera_id})
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera-config", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, default=None)
    parser.add_argument("--detector-backend", choices=("yolo26", "legacy_detector"), default="yolo26")
    args = parser.parse_args()
    try:
        if args.camera_config.stat().st_size > 64 * 1024:
            raise ValueError("Configuration exceeds bounds")
        config = CameraConfig.from_dict(json.loads(args.camera_config.read_bytes()))
    except (OSError, ValueError, TypeError):
        _emit({"ok": False, "code": "invalid_camera_configuration"})
        return 2
    return process_frames(config, args.model_dir, sys.stdin.buffer, detector_backend=args.detector_backend)


if __name__ == "__main__":
    raise SystemExit(main())
