from __future__ import annotations

import math
from typing import Any, Iterable

import cv2
import numpy as np


def compute_hsv_feature(image: np.ndarray) -> list[float]:
    """Compute the API's stable 16H x 8S, L2-normalized descriptor."""
    if image is None or image.size == 0:
        raise ValueError("参考图片为空")
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    histogram = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256]).reshape(-1).astype(np.float32)
    norm = float(np.linalg.norm(histogram))
    if norm > 0:
        histogram /= norm
    return [float(value) for value in histogram]


def cosine_match(left: Iterable[float], right: Iterable[float]) -> float:
    a = np.asarray(list(left), dtype=np.float32)
    b = np.asarray(list(right), dtype=np.float32)
    if a.shape != (128,) or b.shape != (128,):
        raise ValueError("HSV 特征必须为 128 维")
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denominator) if denominator else 0.0


class HsvReferenceMatcher:
    """Match candidate crops against API-provided reference vectors.

    This color descriptor is intentionally labelled experimental. It is useful
    for differently colored wallets/phones but cannot distinguish identical
    items on its own.
    """

    def __init__(self, items: list[dict[str, Any]], threshold: float = 0.82) -> None:
        self.threshold = float(threshold)
        self.features: dict[str, list[list[float]]] = {}
        for item in items:
            item_id = str(item.get("id") or "")
            vectors = []
            for reference in item.get("reference_images") or []:
                feature = reference.get("features") if isinstance(reference, dict) else None
                if isinstance(feature, dict) and feature.get("backend") == "hsv-histogram-v1":
                    vector = feature.get("vector")
                    if isinstance(vector, list) and len(vector) == 128:
                        vectors.append([float(value) for value in vector])
            if item_id and vectors:
                self.features[item_id] = vectors

    @property
    def available(self) -> bool:
        return bool(self.features)

    def match(self, crop: np.ndarray) -> tuple[str | None, float]:
        if not self.features or crop is None or crop.size == 0:
            return None, 0.0
        query = compute_hsv_feature(crop)
        best_id, best_score = None, -1.0
        for item_id, vectors in self.features.items():
            score = max(cosine_match(query, vector) for vector in vectors)
            if score > best_score:
                best_id, best_score = item_id, score
        return (best_id, best_score) if best_score >= self.threshold else (None, best_score)
