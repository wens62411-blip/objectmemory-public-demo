#!/usr/bin/env python3
"""Backward-compatible entry point for the Virtual ESP32-CAM tool."""
from tools.virtual_esp32cam import DeviceSimulator, SimulatorState, claim, heartbeat
from tools.virtual_esp32cam.__main__ import main

__all__ = ["DeviceSimulator", "SimulatorState", "claim", "heartbeat", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
