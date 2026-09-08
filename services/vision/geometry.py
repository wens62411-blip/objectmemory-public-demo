"""Explicit frame geometry; source pixels remain the single coordinate authority.

Continuous pixel-edge coordinates span [0,width] x [0,height]. Drawing code alone
may clip to raster indices. Order: crop -> clockwise rotation -> mirror -> scale
-> leading padding. This class maps coordinates and never alters image pixels.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

import numpy as np


def _pair(value: Any, name: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) for v in value):
        raise ValueError(f"{name} must contain two finite numbers")
    return float(value[0]), float(value[1])


def _dimension(value: Any, name: str) -> int:
    if type(value) is not int or not 0 < value <= 1_000_000:
        raise ValueError(f"{name} must be a positive bounded integer")
    return value


@dataclass(frozen=True, slots=True)
class FrameGeometry:
    source_width: int
    source_height: int
    target_width: int | None = None
    target_height: int | None = None
    crop_offset: tuple[float, float] = (0.0, 0.0)
    crop_size: tuple[float, float] | None = None
    rotation_degrees: int = 0
    mirror_x: bool = False
    scale: tuple[float, float] = (1.0, 1.0)
    pad: tuple[float, float] = (0.0, 0.0)
    _matrix: tuple[tuple[float, ...], ...] = field(init=False, repr=False)
    _inverse: tuple[tuple[float, ...], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        width = _dimension(self.source_width, "source_width")
        height = _dimension(self.source_height, "source_height")
        offset = _pair(self.crop_offset, "crop_offset")
        size = _pair(self.crop_size if self.crop_size is not None else (width - offset[0], height - offset[1]), "crop_size")
        scale, pad = _pair(self.scale, "scale"), _pair(self.pad, "pad")
        if min(offset) < 0 or min(size) <= 0 or offset[0] + size[0] > width or offset[1] + size[1] > height:
            raise ValueError("crop must have positive size and lie inside the source frame")
        if min(scale) < 1e-9 or max(scale) > 1_000_000 or max(abs(value) for value in pad) > 1_000_000_000:
            raise ValueError("scale must be positive and bounded")
        if type(self.rotation_degrees) is not int or self.rotation_degrees not in (0, 90, 180, 270):
            raise ValueError("rotation_degrees must be 0, 90, 180 or 270 clockwise")
        if type(self.mirror_x) is not bool:
            raise ValueError("mirror_x must be boolean")
        for key, value in (("crop_offset", offset), ("crop_size", size), ("scale", scale), ("pad", pad)):
            object.__setattr__(self, key, value)
        crop = np.asarray([[1, 0, -offset[0]], [0, 1, -offset[1]], [0, 0, 1]], np.float64)
        rotations = {
            0: [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
            90: [[0, -1, size[1]], [1, 0, 0], [0, 0, 1]],
            180: [[-1, 0, size[0]], [0, -1, size[1]], [0, 0, 1]],
            270: [[0, 1, 0], [-1, 0, size[0]], [0, 0, 1]],
        }
        rotated_width, rotated_height = size[::-1] if self.rotation_degrees in (90, 270) else size
        mirror = np.asarray([[-1, 0, rotated_width], [0, 1, 0], [0, 0, 1]], np.float64) if self.mirror_x else np.eye(3)
        scaling = np.asarray([[scale[0], 0, pad[0]], [0, scale[1], pad[1]], [0, 0, 1]], np.float64)
        matrix = scaling @ mirror @ np.asarray(rotations[self.rotation_degrees], np.float64) @ crop
        object.__setattr__(self, "_matrix", tuple(tuple(float(v) for v in row) for row in matrix))
        object.__setattr__(self, "_inverse", tuple(tuple(float(v) for v in row) for row in np.linalg.inv(matrix)))
        target_width = self.target_width if self.target_width is not None else math.ceil(rotated_width * scale[0] + 2 * pad[0])
        target_height = self.target_height if self.target_height is not None else math.ceil(rotated_height * scale[1] + 2 * pad[1])
        object.__setattr__(self, "target_width", _dimension(target_width, "target_width"))
        object.__setattr__(self, "target_height", _dimension(target_height, "target_height"))

    @classmethod
    def fit(cls, source_width: int, source_height: int, target_width: int, target_height: int,
            *, mode: str = "contain", crop_offset=(0.0, 0.0), crop_size=None,
            rotation_degrees: int = 0, mirror_x: bool = False) -> FrameGeometry:
        base = cls(source_width, source_height, crop_offset=crop_offset, crop_size=crop_size,
                   rotation_degrees=rotation_degrees, mirror_x=mirror_x)
        _dimension(target_width, "target_width")
        _dimension(target_height, "target_height")
        width, height = base.crop_size[::-1] if rotation_degrees in (90, 270) else base.crop_size
        if mode == "stretch":
            scale, pad = (target_width / width, target_height / height), (0.0, 0.0)
        elif mode in ("contain", "cover"):
            factor = (min if mode == "contain" else max)(target_width / width, target_height / height)
            scale = (factor, factor)
            pad = ((target_width - width * factor) / 2, (target_height - height * factor) / 2)
        else:
            raise ValueError("fit mode must be contain, cover or stretch")
        return cls(source_width, source_height, target_width, target_height, base.crop_offset, base.crop_size,
                   rotation_degrees, mirror_x, scale, pad)

    @staticmethod
    def _points(points: Any) -> np.ndarray:
        values = np.asarray(points, dtype=np.float64)
        if values.ndim != 2 or values.shape[1] != 2 or not np.isfinite(values).all():
            raise ValueError("points must be finite Nx2 coordinates")
        return values

    def source_pixels_to_target(self, points: Any) -> np.ndarray:
        values = self._points(points)
        matrix = np.asarray(self._matrix)
        return values @ matrix[:2, :2].T + matrix[:2, 2]

    def target_pixels_to_source(self, points: Any) -> np.ndarray:
        values = self._points(points)
        matrix = np.asarray(self._inverse)
        return values @ matrix[:2, :2].T + matrix[:2, 2]

    def source_normalized_to_target(self, points: Any) -> np.ndarray:
        return self.source_pixels_to_target(self._points(points) * (self.source_width, self.source_height))

    def target_to_source_normalized(self, points: Any) -> np.ndarray:
        return self.target_pixels_to_source(points) / (self.source_width, self.source_height)

    def _box(self, bbox: Any, *, inverse: bool, normalized: bool) -> tuple[float, float, float, float]:
        values = np.asarray(bbox, np.float64)
        if values.shape != (4,) or not np.isfinite(values).all() or min(values[2:]) <= 0:
            raise ValueError("bbox must be finite xywh with positive width and height")
        if normalized and not inverse:
            values = values * (self.source_width, self.source_height, self.source_width, self.source_height)
        x, y, width, height = values
        corners = [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]
        mapped = self.target_pixels_to_source(corners) if inverse else self.source_pixels_to_target(corners)
        if normalized and inverse:
            mapped = mapped / (self.source_width, self.source_height)
        low, high = mapped.min(axis=0), mapped.max(axis=0)
        return float(low[0]), float(low[1]), float(high[0] - low[0]), float(high[1] - low[1])

    def source_bbox_to_target(self, bbox: Any, *, normalized: bool = False) -> tuple[float, float, float, float]:
        return self._box(bbox, inverse=False, normalized=normalized)

    def target_bbox_to_source(self, bbox: Any, *, normalized: bool = False) -> tuple[float, float, float, float]:
        return self._box(bbox, inverse=True, normalized=normalized)

    def to_dict(self) -> dict[str, Any]:
        return {"coordinate_space": "source_normalized", "pixel_convention": "continuous_pixel_edges",
                "transform_order": ["crop", "clockwise_rotation", "mirror_x", "scale", "pad"],
                "source_width": self.source_width, "source_height": self.source_height,
                "target_width": self.target_width, "target_height": self.target_height,
                "crop_offset": list(self.crop_offset), "crop_size": list(self.crop_size),
                "rotation_degrees": self.rotation_degrees, "mirror_x": self.mirror_x,
                "scale": list(self.scale), "pad": list(self.pad), "source_to_target_matrix": [list(row) for row in self._matrix]}
