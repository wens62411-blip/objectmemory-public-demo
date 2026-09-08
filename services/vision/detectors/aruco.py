from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from .base import Detection, DetectorBackend


class ArucoDetectorBackend(DetectorBackend):
    name = "aruco"

    def __init__(self, item_by_marker: dict[int, dict[str, Any]] | None = None) -> None:
        self.item_by_marker = item_by_marker or {}
        if not hasattr(cv2, "aruco"):
            raise RuntimeError("当前 OpenCV 缺少 aruco 模块，请安装 opencv-contrib-python")
        self.dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        parameters = cv2.aruco.DetectorParameters()
        parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        parameters.minMarkerPerimeterRate = 0.02
        parameters.maxMarkerPerimeterRate = 4.0
        parameters.adaptiveThreshWinSizeMin = 3
        parameters.adaptiveThreshWinSizeMax = 33
        self.detector = cv2.aruco.ArucoDetector(self.dictionary, parameters)

    def detect(self, frame: np.ndarray) -> list[Detection]:
        if frame is None or frame.size == 0:
            return []
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        corners, ids, _rejected = self.detector.detectMarkers(gray)
        if ids is None:
            return []
        results: list[Detection] = []
        for marker_corners, marker_id_array in zip(corners, ids.flatten()):
            marker_id = int(marker_id_array)
            points = marker_corners.reshape(-1, 2)
            x, y, width, height = cv2.boundingRect(points.astype(np.float32))
            center = tuple(float(value) for value in points.mean(axis=0))
            item = self.item_by_marker.get(marker_id)
            identity = str(item.get("id")) if item else f"aruco:{marker_id}"
            label = str(item.get("name")) if item else f"ArUco {marker_id:03d}"
            perimeter = float(cv2.arcLength(points.astype(np.float32), True))
            frame_perimeter = float(2 * (frame.shape[0] + frame.shape[1]))
            size_score = min(1.0, perimeter / max(frame_perimeter * 0.08, 1.0))
            confidence = round(0.92 + 0.07 * size_score, 3)
            results.append(
                Detection(
                    identity=identity,
                    label=label,
                    bbox=(int(x), int(y), int(width), int(height)),
                    center=center,
                    confidence=confidence,
                    detection_mode="aruco",
                    raw_id=marker_id,
                    corners=[(float(px), float(py)) for px, py in points],
                    metadata={"mapped": item is not None},
                )
            )
        return results

