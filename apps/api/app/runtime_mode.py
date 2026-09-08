"""Fail-closed runtime-mode and provenance rules for ObjectMemory."""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class RuntimeMode(StrEnum):
    REAL = "REAL"
    DEMO = "DEMO"
    TEST = "TEST"


DATABASE_NAMES = {
    RuntimeMode.REAL: "objectmemory.sqlite",
    RuntimeMode.DEMO: "objectmemory-demo.sqlite",
    RuntimeMode.TEST: "objectmemory-test.sqlite",
}

MEDIA_NAMES = {
    RuntimeMode.REAL: "real-media",
    RuntimeMode.DEMO: "demo-media",
    RuntimeMode.TEST: "test-media",
}

SOURCE_TYPES = {
    "webcam": "opencv_camera",
    "browser": "browser_camera",
    "rtsp": "rtsp",
    "onvif": "onvif",
    "mjpeg": "mjpeg",
    "screen": "authorized_screen_capture",
    "video": "video_file",
    "esp32": "esp32_real",
}

SIMULATED_SOURCE_TYPES = {"video_file", "virtual_esp32", "demo_seed", "test_fixture", "mock"}
REAL_SOURCE_TYPES = {
    "opencv_camera",
    "browser_camera",
    "rtsp",
    "onvif",
    "mjpeg",
    "esp32_real",
    "authorized_screen_capture",
}


@dataclass(frozen=True, slots=True)
class RuntimeLayout:
    mode: RuntimeMode
    data_root: Path
    database_path: Path
    media_root: Path
    temporary_root: Path
    logs_root: Path


def normalize_runtime_mode(value: str | RuntimeMode | None, *, testing: bool = False) -> RuntimeMode:
    if testing:
        return RuntimeMode.TEST
    raw = str(value or "").strip().upper()
    if not raw:
        legacy = os.environ.get("OM_RUN_MODE", "").strip().lower()
        if legacy in {"demo", "no_hardware_demo", "no-hardware-demo"}:
            return RuntimeMode.DEMO
        if legacy in {"test", "testing"}:
            return RuntimeMode.TEST
        return RuntimeMode.REAL
    modes = {mode.value: mode for mode in RuntimeMode}
    if raw not in modes:
        raise RuntimeError("OM_RUNTIME_MODE 只能是 REAL、DEMO 或 TEST；为避免混库，服务已拒绝启动。")
    return modes[raw]


def resolve_runtime_mode(*, testing: bool = False, explicit: str | RuntimeMode | None = None) -> RuntimeMode:
    return normalize_runtime_mode(explicit if explicit is not None else os.environ.get("OM_RUNTIME_MODE"), testing=testing)


def runtime_layout(root: Path, data_dir: str | Path | None, mode: RuntimeMode) -> RuntimeLayout:
    candidate = Path(data_dir or os.environ.get("OM_DATA_DIR", "data"))
    data_root = (candidate if candidate.is_absolute() else root / candidate).resolve()
    database_path = data_root / "database" / DATABASE_NAMES[mode]
    media_root = data_root / MEDIA_NAMES[mode]
    temporary_root = data_root / "temporary" / mode.value.lower()
    logs_root = data_root / "logs" / mode.value.lower()
    return RuntimeLayout(mode, data_root, database_path, media_root, temporary_root, logs_root)


def provenance_for_camera(camera: dict[str, Any]) -> tuple[str, bool]:
    config = camera.get("config") or {}
    source = SOURCE_TYPES.get(str(camera.get("source_type") or ""), "unknown")
    simulated = bool(config.get("simulated"))
    if source == "esp32_real" and simulated:
        source = "virtual_esp32"
    if source == "video_file":
        simulated = True
    return source, simulated


def validate_source_for_mode(mode: RuntimeMode, camera: dict[str, Any]) -> tuple[str, bool]:
    source, simulated = provenance_for_camera(camera)
    if mode is RuntimeMode.REAL and (simulated or source not in REAL_SOURCE_TYPES):
        raise ValueError(f"REAL 模式禁止来源 {source}；请重启到 DEMO 模式后使用测试视频或虚拟设备。")
    if mode is RuntimeMode.TEST and source == "esp32_real" and not simulated:
        # Automated tests must never contact or mutate a physical device.
        raise ValueError("TEST 模式禁止连接真实 ESP32-CAM。")
    return source, simulated or mode is not RuntimeMode.REAL


def provenance_fields(mode: RuntimeMode, source_type: str, simulated: bool) -> dict[str, Any]:
    return {
        "runtime_mode": mode.value,
        "source_type": source_type,
        "is_simulated": bool(simulated),
    }
