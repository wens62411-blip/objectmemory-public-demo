"""Local computer-vision runtime for ObjectMemory.

The package deliberately keeps camera I/O, detection, tracking and event
generation independent so a demo-safe ArUco detector can always be used when
optional AI dependencies are unavailable.
"""

from .engine import VisionEngine
from .zones import ZoneManager

__all__ = ["VisionEngine", "ZoneManager"]
