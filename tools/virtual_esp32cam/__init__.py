"""Virtual ESP32-CAM that implements the real device HTTP and backend protocol."""

from .device import (
    DeviceSimulator,
    SimulatorState,
    claim,
    heartbeat,
)

__all__ = ["DeviceSimulator", "SimulatorState", "claim", "heartbeat"]

