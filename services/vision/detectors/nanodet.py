from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .base import Detection, DetectorBackend
from .experimental import COCO80


class NanoDetDetectorBackend(DetectorBackend):
    """OpenCV Zoo NanoDet-m-plus-1.5x 416 detector.

    Processing follows the Apache-2.0 OpenCV Zoo reference implementation in
    ``models/object_detection_nanodet/{demo,nanodet}.py``: RGB, INTER_AREA
    letterbox, then normalization. Detection boxes are mapped back out of the
    padding before use. COCO categories include cell phone; correct input does
    not guarantee that a physical phone will be detected.
    """

    name = "opencv_zoo_nanodet_experimental"

    def __init__(self, model_path: str | Path, confidence: float = 0.35, iou_threshold: float = 0.6, *, runtime: str = 'auto') -> None:
        if runtime not in {'auto', 'opencv', 'onnxruntime'}:
            raise ValueError('Unknown NanoDet inference runtime')
        self.model_path = Path(model_path).expanduser().resolve()
        self.confidence = float(confidence)
        self.iou_threshold = float(iou_threshold)
        self.strides = (8, 16, 32, 64)
        self.image_shape = (416, 416)
        self.reg_max = 7
        self.project = np.arange(self.reg_max + 1)
        self.mean = np.asarray([103.53, 116.28, 123.675], dtype=np.float32).reshape(1, 1, 3)
        self.std = np.asarray([57.375, 57.12, 58.395], dtype=np.float32).reshape(1, 1, 3)
        self.anchors: list[np.ndarray] = []
        for stride in self.strides:
            feat_h, feat_w = self.image_shape[0] // stride, self.image_shape[1] // stride
            xv, yv = np.meshgrid(np.arange(feat_w) * stride, np.arange(feat_h) * stride)
            self.anchors.append(np.column_stack((xv.flatten() + 0.5 * (stride - 1), yv.flatten() + 0.5 * (stride - 1))))
        self.net = None
        self._ort_session = None
        self.inference_runtime = 'unavailable'
        self.error: str | None = None
        self.device = "cpu"
        try:
            if not self.model_path.is_file():
                raise FileNotFoundError(f"模型不存在：{self.model_path}；请运行 scripts/download-models.py --nanodet")
            # Use the already-installed local runtime on CPU, avoiding shared
            # OpenCV DNN worker contention with capture/preview. No model or
            # threshold changes, downloads, or new global thread settings.
            try:
                cuda_available = cv2.cuda.getCudaEnabledDeviceCount() > 0
            except Exception:
                cuda_available = False
            if runtime == 'onnxruntime' or (runtime == 'auto' and not cuda_available):
                try:
                    import onnxruntime as ort
                except ImportError:
                    if runtime == 'onnxruntime':
                        raise
                else:
                    ort.disable_telemetry_events()
                    options = ort.SessionOptions()
                    options.intra_op_num_threads = 2
                    options.inter_op_num_threads = 1
                    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                    options.add_session_config_entry('session.intra_op.allow_spinning', '0')
                    options.add_session_config_entry('session.inter_op.allow_spinning', '0')
                    # This older export lists constant initializers as inputs;
                    # ORT warns per initializer. Errors remain visible and the
                    # original, SHA-pinned model file is never rewritten.
                    options.log_severity_level = 3
                    session = ort.InferenceSession(str(self.model_path), options, providers=['CPUExecutionProvider'])
                    inputs = session.get_inputs()
                    outputs = {value.name: value for value in session.get_outputs()}
                    self._ort_outputs = ['792', '795', '814', '817', '836', '839']
                    expected_shapes = [[1, count, channels] for count in (2704, 676, 169) for channels in (80, 32)]
                    if (len(inputs) != 1 or inputs[0].name != 'input.1' or inputs[0].type != 'tensor(float)'
                            or inputs[0].shape != [1, 3, 416, 416] or set(outputs) != set(self._ort_outputs)
                            or any(outputs[name].shape != shape or outputs[name].type != 'tensor(float)'
                                   for name, shape in zip(self._ort_outputs, expected_shapes))):
                        raise ValueError('Unexpected NanoDet ONNX input/output contract')
                    self._ort_session = session
                    self.inference_runtime = 'onnxruntime_cpu'
                    return
            # Buffer input avoids OpenCV's narrow Windows path handling.
            self.net = cv2.dnn.readNetFromONNX(np.frombuffer(self.model_path.read_bytes(), dtype=np.uint8))
            self.inference_runtime = 'opencv_dnn'
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
        return {
            "name": self.name, "available": self.net is not None or self._ort_session is not None,
            "device": self.device, "model_path": str(self.model_path), "error": self.error,
            "inference_runtime": self.inference_runtime,
            "runtime_threads": 2 if self._ort_session is not None else None,
            "notice": "无标签AI实验模式：相似物品、严重遮挡和低光环境下可能出现误判",
        }

    def _forward(self, resized: np.ndarray) -> list[np.ndarray]:
        image = (resized.astype(np.float32) - self.mean) / self.std
        if self._ort_session is not None:
            tensor = np.ascontiguousarray(image.transpose(2, 0, 1)[None])
            # ORT's default order groups all class heads before box heads;
            # request interleaved heads explicitly, as OpenCV does.
            return self._ort_session.run(self._ort_outputs, {'input.1': tensor})
        assert self.net is not None
        self.net.setInput(cv2.dnn.blobFromImage(image))
        try:
            return self.net.forward(self.net.getUnconnectedOutLayersNames())
        except cv2.error:
            if self.device != "cuda":
                raise
            self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            self.device = "cpu"
            return self.net.forward(self.net.getUnconnectedOutLayersNames())

    def detect(self, frame: np.ndarray) -> list[Detection]:
        if (self.net is None and self._ort_session is None) or frame is None or frame.size == 0:
            return []
        height, width = frame.shape[:2]
        # OpenCV Zoo demo.py converts BGR capture frames to RGB *before*
        # letterboxing. A direct square resize distorts the trained geometry.
        target_height, target_width = self.image_shape
        ratio = min(target_width / width, target_height / height)
        resized_width = max(1, min(target_width, int(width * ratio)))
        resized_height = max(1, min(target_height, int(height * ratio)))
        left, top = (target_width - resized_width) // 2, (target_height - resized_height) // 2
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(rgb, (resized_width, resized_height), interpolation=cv2.INTER_AREA)
        padded = cv2.copyMakeBorder(resized, top, target_height - resized_height - top,
                                   left, target_width - resized_width - left,
                                   cv2.BORDER_CONSTANT, value=0)
        predictions = self._forward(padded)
        class_scores, bbox_predictions = predictions[::2], predictions[1::2]
        boxes_per_level: list[np.ndarray] = []
        scores_per_level: list[np.ndarray] = []
        for stride, class_score, bbox_prediction, anchors in zip(self.strides, class_scores, bbox_predictions, self.anchors):
            class_score = np.squeeze(class_score, axis=0) if class_score.ndim == 3 else class_score
            bbox_prediction = np.squeeze(bbox_prediction, axis=0) if bbox_prediction.ndim == 3 else bbox_prediction
            exponent = np.exp(bbox_prediction.reshape(-1, self.reg_max + 1))
            distances = exponent / np.sum(exponent, axis=1, keepdims=True)
            distances = np.dot(distances, self.project).reshape(-1, 4) * stride
            if class_score.shape[0] > 1000:
                keep = class_score.max(axis=1).argsort()[::-1][:1000]
                level_anchors, distances, class_score = anchors[keep], distances[keep], class_score[keep]
            else:
                level_anchors = anchors
            x1 = np.clip(level_anchors[:, 0] - distances[:, 0], 0, self.image_shape[1])
            y1 = np.clip(level_anchors[:, 1] - distances[:, 1], 0, self.image_shape[0])
            x2 = np.clip(level_anchors[:, 0] + distances[:, 2], 0, self.image_shape[1])
            y2 = np.clip(level_anchors[:, 1] + distances[:, 3], 0, self.image_shape[0])
            boxes_per_level.append(np.column_stack((x1, y1, x2, y2)))
            scores_per_level.append(class_score)
        boxes = np.concatenate(boxes_per_level, axis=0)
        scores = np.concatenate(scores_per_level, axis=0)
        class_ids = np.argmax(scores, axis=1)
        confidences = np.max(scores, axis=1)
        boxes_xywh = boxes.copy()
        boxes_xywh[:, 2:4] -= boxes_xywh[:, 0:2]
        indices = cv2.dnn.NMSBoxes(boxes_xywh.tolist(), confidences.tolist(), self.confidence, self.iou_threshold)
        results: list[Detection] = []
        scale_x, scale_y = width / resized_width, height / resized_height
        for index in np.asarray(indices).reshape(-1) if len(indices) else []:
            index = int(index)
            x1, y1, x2, y2 = boxes[index]
            x = round(float(np.clip((x1 - left) * scale_x, 0, width)))
            y = round(float(np.clip((y1 - top) * scale_y, 0, height)))
            x_end = round(float(np.clip((x2 - left) * scale_x, 0, width)))
            y_end = round(float(np.clip((y2 - top) * scale_y, 0, height)))
            if x_end <= x or y_end <= y:
                continue  # Padding is not a one-pixel source-frame object.
            class_id = int(class_ids[index])
            label = COCO80[class_id] if class_id < len(COCO80) else f"class_{class_id}"
            results.append(Detection(
                identity=f"generic:{label}:{index}", label=label,
                bbox=(x, y, x_end - x, y_end - y),
                center=((x + x_end) / 2, (y + y_end) / 2), confidence=float(confidences[index]),
                detection_mode="experimental", raw_id=class_id,
                metadata={"backend": self.name, "device": self.device, "inference_runtime": self.inference_runtime},
            ))
        return results
