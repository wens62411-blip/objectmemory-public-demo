from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass(slots=True)
class Detection:
    identity: str
    label: str
    bbox: tuple[int, int, int, int]
    center: tuple[float, float]
    confidence: float
    detection_mode: str
    raw_id: int | str | None = None
    corners: list[tuple[float, float]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class DetectorBackend(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def detect(self, frame: np.ndarray) -> list[Detection]: ...

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "available": True, "error": None}

