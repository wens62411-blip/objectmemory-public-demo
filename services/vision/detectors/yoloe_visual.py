"""Offline evaluation adapter, deliberately NOT wired into the live engine.

YOLOE visual classes are reference groups (object0), not semantic phone labels
and not registered item identities. Only the separate appearance matcher may
assess identity; this adapter never produces an accepted item or an event.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import time

import numpy as np

WEIGHT_URL = "https://github.com/ultralytics/assets/releases/download/v8.3.0/yoloe-11s-seg.pt"
WEIGHT_BYTES = 27_803_986
WEIGHT_SHA256 = "8e439445c87338b79d9ce21dec109f4621e26df67e94d26ea1a98c1e64dce3e3"
PACKAGE_VERSION = "8.3.235"


def reference_xyxy(region, width: int, height: int) -> list[float]:
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        raise ValueError("Reference region must be normalized xywh")
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in region):
        raise ValueError("Reference region must contain finite numbers")
    x, y, w, h = region
    if width <= 0 or height <= 0 or min(x, y) < 0 or min(w, h) <= 0 or x + w > 1 or y + h > 1:
        raise ValueError("Reference region is outside its own image")
    return [x * width, y * height, (x + w) * width, (y + h) * height]


def source_box(xyxy, width: int, height: int) -> tuple[float, float, float, float]:
    values = np.asarray(xyxy, dtype=np.float64)
    if values.shape != (4,) or not np.isfinite(values).all():
        raise ValueError("Model box must be finite source-image xyxy")
    x1, y1, x2, y2 = values
    if not 0 <= x1 < x2 <= width or not 0 <= y1 < y2 <= height:
        raise ValueError("Model box does not fit the decoded source image")
    return float(x1), float(y1), float(x2 - x1), float(y2 - y1)


class YOLOEVisualProbe:
    """One CPU model load; reference prompting is explicitly separate from input.

    The caller must disable network access before constructing this optional
    adapter. Missing packages/weights fail visibly; no fallback or downloads.
    """

    def __init__(self, weight_path: Path, *, confidence: float = .35, image_size: int = 640):
        import importlib.metadata

        self.weight_path = Path(weight_path).resolve(strict=True)
        if self.weight_path.stat().st_size != WEIGHT_BYTES:
            raise ValueError("YOLOE checkpoint size is not the reviewed official asset")
        self.weight_sha256 = hashlib.sha256(self.weight_path.read_bytes()).hexdigest()
        if self.weight_sha256 != WEIGHT_SHA256:
            raise ValueError("YOLOE checkpoint SHA-256 mismatch")
        if importlib.metadata.version("ultralytics") != PACKAGE_VERSION:
            raise ValueError("YOLOE probe requires the reviewed Ultralytics version")
        if not .35 <= confidence < 1 or image_size != 640:
            raise ValueError("Evaluation uses fixed reviewed confidence >= .35 and input 640")
        import torch
        from ultralytics import YOLOE
        from ultralytics.models.yolo.yoloe import YOLOEVPSegPredictor

        torch.set_num_threads(2)
        torch.set_num_interop_threads(1)
        self._predictor = YOLOEVPSegPredictor
        started = time.perf_counter()
        self.model = YOLOE(str(self.weight_path))
        self.load_ms = (time.perf_counter() - started) * 1000
        self.confidence, self.image_size = confidence, image_size
        self._reference = None
        self._reference_hash = None
        self._prompts = None
        self._reference_id = None
        self._prompt_item_id = None
        self.predict_calls = 0
        self.prompt_calls = 0

    def set_reference(self, frame: np.ndarray, region, *, reference_id: str, prompt_item_id: str):
        if frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError("Reference must be decoded BGR uint8")
        height, width = frame.shape[:2]
        self._reference = frame.copy()
        self._reference_hash = hashlib.sha256(frame.tobytes()).hexdigest()
        self._prompts = {"bboxes": np.asarray([reference_xyxy(region, width, height)], dtype=np.float32),
                         "cls": np.asarray([0], dtype=np.int64)}
        self._reference_id, self._prompt_item_id = reference_id, prompt_item_id

    def detect(self, frame: np.ndarray) -> list[dict]:
        if self._reference is None:
            raise RuntimeError("No confirmed reference region configured")
        if hashlib.sha256(frame.tobytes()).hexdigest() == self._reference_hash:
            raise ValueError("Reference-image self-testing is forbidden")
        options = dict(source=frame, device="cpu", imgsz=self.image_size, conf=self.confidence,
                       iou=.7, max_det=100, save=False, verbose=False, augment=False)
        if self._prompts is not None:
            options.update(refer_image=self._reference, visual_prompts=self._prompts, predictor=self._predictor)
            self.prompt_calls += 1
        results = self.model.predict(**options)
        self._prompts = None  # Cross-image prompting persists; never recompute it each frame.
        self.predict_calls += 1
        if len(results) != 1 or tuple(results[0].orig_shape) != tuple(frame.shape[:2]):
            raise ValueError("YOLOE result is not bound to the supplied frame geometry")
        result = results[0]
        height, width = frame.shape[:2]
        rows = []
        for box in result.boxes:
            raw_id = int(box.cls.item())
            # Read actual model names. A prompt group is not a COCO class ID.
            raw_name = result.names[raw_id]
            if raw_id != 0:
                raise ValueError("Unexpected visual prompt group")
            bbox = source_box(box.xyxy[0].cpu().numpy(), width, height)
            rows.append({"raw_class_id": raw_id, "raw_class_name": raw_name,
                         "category": None, "category_evidence": "visual_reference_prompt",
                         "prompt_reference_id": self._reference_id, "prompt_item_id": self._prompt_item_id,
                         "item_id": None, "identity_confirmed": False, "track_id": None,
                         "bbox": list(bbox), "bbox_format": "source_pixel_xywh",
                         "detector_score": float(box.conf.item()), "proposal_backend": "yoloe_visual_probe"})
        return rows

    def health(self):
        return {"model_id": "yoloe-11s-seg", "ultralytics_version": PACKAGE_VERSION,
                "device": "cpu", "weight_path": str(self.weight_path), "weight_url": WEIGHT_URL,
                "weight_sha256": self.weight_sha256, "weight_bytes": WEIGHT_BYTES,
                "license": "AGPL-3.0; closed-source deployment requires separate license review",
                "input_size": self.image_size, "confidence_threshold": self.confidence,
                "load_ms": round(self.load_ms, 3), "model_loads": 1,
                "predict_calls": self.predict_calls, "prompt_calls": self.prompt_calls,
                "production_integrated": False}
