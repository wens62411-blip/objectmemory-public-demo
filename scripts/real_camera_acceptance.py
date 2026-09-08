from __future__ import annotations

import argparse
import json
import platform
import queue
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.vision.camera_manager import camera_manager
from services.vision.camera_sources import WebcamSource
from services.vision.capture import CaptureWorker


def _save_jpeg(path: Path, frame) -> int:
    ok, encoded = cv2.imencode(".jpg", frame)
    if not ok:
        raise OSError("OpenCV无法编码真实摄像头截图")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = encoded.tobytes()
    path.write_bytes(payload)
    return len(payload)


def run_acceptance(device_index: int, duration: float, output: Path, screenshot: Path) -> dict:
    started_at = datetime.now(timezone.utc)
    config = {
        "owner": "acceptance:real-camera",
        "width": 1280,
        "height": 720,
        "warmup_frames": 5,
        "warmup_seconds": 3,
        "lock_timeout_seconds": 2,
    }
    source = WebcamSource(str(device_index), config)
    worker = CaptureWorker(source, queue_size=2, reconnect_after_failures=8)
    report = {
        "started_at": started_at.isoformat(),
        "completed_at": None,
        "test_kind": "REAL_WINDOWS_WEBCAM",
        "simulated": False,
        "device_index": device_index,
        "requested_duration_seconds": duration,
        "python_version": platform.python_version(),
        "opencv_version": cv2.__version__,
        "success": False,
        "successful_frames": 0,
        "consumed_frames": 0,
        "failed_frames": 0,
        "queue_dropped_frames": 0,
        "average_fps": 0.0,
        "max_consecutive_failures": 0,
        "reconnects": 0,
        "width": 0,
        "height": 0,
        "backend": None,
        "resource_released": False,
        "restart_success": False,
        "restart_successful_frames": 0,
        "screenshot_path": None,
        "screenshot_bytes": 0,
        "frontend_chain_verified": False,
        "error": None,
    }
    first_packet_at = None
    last_packet_at = None
    last_frame = None
    try:
        if not worker.start():
            report["error"] = worker.error or source.health_check().get("error") or "摄像头启动失败"
            return report
        capture_started = time.monotonic()
        while time.monotonic() - capture_started < duration:
            try:
                packet = worker.frames.get(timeout=1.0)
            except queue.Empty:
                continue
            if first_packet_at is None:
                first_packet_at = packet.monotonic_time
            last_packet_at = packet.monotonic_time
            last_frame = packet.frame
            report["consumed_frames"] += 1
        health = worker.health()
        report.update(
            successful_frames=int(health.get("frames_read") or 0),
            failed_frames=int(health.get("source_read_failures") or 0),
            queue_dropped_frames=int(health.get("queue_dropped_frames") or 0),
            max_consecutive_failures=int(health.get("max_consecutive_failures") or 0),
            reconnects=int(health.get("reconnects") or 0),
            width=int(health.get("width") or 0),
            height=int(health.get("height") or 0),
            backend=health.get("backend"),
            status_code=health.get("status_code"),
        )
        measured = max(0.000001, (last_packet_at or 0) - (first_packet_at or 0))
        report["average_fps"] = round(max(0, report["consumed_frames"] - 1) / measured, 3) if report["consumed_frames"] > 1 else 0.0
        if last_frame is not None:
            report["screenshot_bytes"] = _save_jpeg(screenshot, last_frame)
            report["screenshot_path"] = str(screenshot.resolve())
        report["success"] = bool(report["successful_frames"] and time.monotonic() - capture_started >= duration)
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        worker.stop()
        report["resource_released"] = camera_manager.describe(camera_manager.webcam_key(device_index)) is None

    # A second open is part of acceptance; it catches handles leaked by stop().
    try:
        if worker.start():
            deadline = time.monotonic() + 8
            while report["restart_successful_frames"] < 30 and time.monotonic() < deadline:
                try:
                    worker.frames.get(timeout=1)
                    report["restart_successful_frames"] += 1
                except queue.Empty:
                    pass
            report["restart_success"] = report["restart_successful_frames"] >= 30
        else:
            report["restart_error"] = worker.error or source.health_check().get("error")
    except Exception as exc:
        report["restart_error"] = str(exc)
    finally:
        worker.stop()
        report["resource_released_after_restart"] = camera_manager.describe(camera_manager.webcam_key(device_index)) is None
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        report["success"] = bool(report["success"] and report["restart_success"] and report["resource_released"] and report["resource_released_after_restart"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="ObjectMemory真实电脑摄像头60秒验收")
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "diagnostics" / "real-camera-acceptance.json")
    parser.add_argument("--screenshot", type=Path, default=PROJECT_ROOT / "data" / "diagnostics" / "real-camera-acceptance.jpg")
    args = parser.parse_args()
    if args.duration < 5 or args.duration > 3600:
        parser.error("duration必须在5到3600秒之间")
    result = run_acceptance(args.device_index, args.duration, args.output.resolve(), args.screenshot.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
