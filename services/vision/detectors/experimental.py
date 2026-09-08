from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .base import Detection, DetectorBackend


COCO80 = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
    "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]


class OnnxYoloDetectorBackend(DetectorBackend):
    """Small optional YOLO ONNX backend for the explicitly experimental mode."""

    name = "onnx_yolo_experimental"

    def __init__(self, model_path: str | Path, confidence: float = 0.35, input_size: int = 640) -> None:
        self.model_path = Path(model_path)
        self.confidence = confidence
        self.input_size = input_size
        self.net = None
        self.error: str | None = None
        self.device = "cpu"
        try:
            if not self.model_path.is_file():
                raise FileNotFoundError(f"模型不存在：{self.model_path}")
            self.net = cv2.dnn.readNetFromONNX(np.frombuffer(self.model_path.read_bytes(), dtype=np.uint8))
            try:
                if cv2.cuda.getCudaEnabledDeviceCount() > 0:
                    self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_CUDA)
                    self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CUDA_FP16)
                    self.device = "cuda"
            except Exception:
                self.device = "cpu"
        except Exception as exc:
            self.error = str(exc)

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "available": self.net is not None, "device": self.device, "error": self.error}

    def detect(self, frame: np.ndarray) -> list[Detection]:
        if self.net is None:
            return []
        height, width = frame.shape[:2]
        side = max(height, width)
        canvas = np.zeros((side, side, 3), dtype=np.uint8)
        canvas[:height, :width] = frame
        scale = side / self.input_size
        blob = cv2.dnn.blobFromImage(canvas, 1 / 255.0, (self.input_size, self.input_size), swapRB=True, crop=False)
        self.net.setInput(blob)
        output = self.net.forward()
        predictions = np.squeeze(output)
        if predictions.ndim != 2:
            return []
        # Ultralytics ONNX may emit [84,N] or [N,84].
        if predictions.shape[0] < predictions.shape[1] and predictions.shape[0] in (84, 85):
            predictions = predictions.T
        if predictions.shape[1] < 5:
            return []
        # Reduce all class rows in NumPy; only decode boxes above the gate.
        class_ids = predictions[:, 4:].argmax(axis=1)
        scores = predictions[np.arange(len(predictions)), class_ids + 4].astype(np.float64)
        selected = ~(scores < self.confidence)
        centers, sizes = np.split(predictions[selected, :4].astype(np.float64) * scale, 2, axis=1)
        boxes = [[int(value) for value in row] for row in np.column_stack((centers - sizes / 2, sizes))]
        scores, class_ids = scores[selected].tolist(), class_ids[selected]
        keep = cv2.dnn.NMSBoxes(boxes, scores, self.confidence, 0.45)
        detections: list[Detection] = []
        for index in np.asarray(keep).reshape(-1) if len(keep) else []:
            x, y, w, h = boxes[int(index)]
            class_id = int(class_ids[int(index)])
            label = COCO80[class_id] if class_id < len(COCO80) else f"class_{class_id}"
            detections.append(Detection(
                identity=f"generic:{label}:{index}", label=label,
                bbox=(max(0, x), max(0, y), min(width - max(0, x), w), min(height - max(0, y), h)),
                center=(x + w / 2, y + h / 2), confidence=scores[int(index)],
                detection_mode="experimental", raw_id=class_id,
            ))
        return detections


class OrbReferenceMatcher:
    """Local reference-photo matcher; results remain experimental evidence."""

    def __init__(self, references: dict[str, list[str | Path]], max_features: int = 800) -> None:
        self.orb = cv2.ORB_create(nfeatures=max_features)
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        self.references: dict[str, list[np.ndarray]] = {}
        for item_id, paths in references.items():
            descriptors: list[np.ndarray] = []
            for path in paths:
                try:
                    image = cv2.imdecode(np.frombuffer(Path(path).read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
                except OSError:
                    image = None
                if image is None:
                    continue
                _keypoints, desc = self.orb.detectAndCompute(image, None)
                if desc is not None and len(desc) >= 8:
                    descriptors.append(desc)
            self.references[item_id] = descriptors

    def match(self, crop: np.ndarray) -> tuple[str | None, float]:
        if crop is None or crop.size == 0:
            return None, 0.0
        _keypoints, query = self.orb.detectAndCompute(crop, None)
        if query is None or len(query) < 8:
            return None, 0.0
        best_item, best_score = None, 0.0
        for item_id, descriptor_sets in self.references.items():
            item_score = 0.0
            for descriptors in descriptor_sets:
                pairs = self.matcher.knnMatch(query, descriptors, k=2)
                good = [pair[0] for pair in pairs if len(pair) >= 2 and pair[0].distance < 0.72 * pair[1].distance]
                score = len(good) / max(12, min(len(query), len(descriptors)))
                item_score = max(item_score, score)
            if item_score > best_score:
                best_item, best_score = item_id, item_score
        return (best_item, min(1.0, best_score)) if best_score >= 0.2 else (None, best_score)
