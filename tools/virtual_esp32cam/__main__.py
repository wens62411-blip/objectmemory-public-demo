"""CLI entry point for ``python -m tools.virtual_esp32cam``."""
from __future__ import annotations

import argparse
import threading
from pathlib import Path

import httpx

from .device import DEFAULT_DATA, DEFAULT_VIDEO, run_virtual_device


def main() -> int:
    parser = argparse.ArgumentParser(description="ObjectMemory Virtual ESP32-CAM (not physical hardware)")
    parser.add_argument("--backend-url", default="http://127.0.0.1:8018")
    parser.add_argument("--pairing-code", help="one-time code; omitted with --auto-enroll")
    parser.add_argument("--auto-enroll", action="store_true", help="create a real one-time code through the local API")
    parser.add_argument("--device-id", help="stable virtual ID; generated and persisted when omitted")
    parser.add_argument("--device-name", default="虚拟 ESP32-CAM")
    parser.add_argument("--room-name", default="客厅")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--split-stream", action="store_true")
    parser.add_argument("--source", choices=("video", "webcam"), default="video")
    parser.add_argument("--video-path", type=Path, default=DEFAULT_VIDEO)
    parser.add_argument("--camera-index", type=int, default=0)
    parser.add_argument("--fps", type=float, default=5.0)
    parser.add_argument("--binding-file", type=Path,
                        default=DEFAULT_DATA / "temporary" / "virtual-devices" / "default-binding.json")
    parser.add_argument("--status-file", type=Path,
                        default=DEFAULT_DATA / "temporary" / "virtual-devices" / "status.json")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.source == "webcam" and not 0 <= args.camera_index <= 16:
        parser.error("camera index must be between 0 and 16")
    if not 1 <= args.fps <= 15:
        parser.error("fps must be between 1 and 15")

    stop_event = threading.Event()
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(__import__("signal"), name, None)
        if signum is not None:
            try:
                __import__("signal").signal(signum, lambda *_: stop_event.set())
            except (ValueError, OSError):
                pass

    print("ObjectMemory Virtual ESP32-CAM starting (explicitly not physical hardware).", flush=True)
    try:
        return run_virtual_device(
            backend_url=args.backend_url.rstrip("/"), device_name=args.device_name,
            room_name=args.room_name, host=args.host, port=args.port,
            split_stream=args.split_stream, source=args.source, video_path=args.video_path,
            camera_index=args.camera_index, fps=args.fps, pairing_code=args.pairing_code,
            auto_enroll=args.auto_enroll, binding_path=args.binding_file.resolve(),
            status_path=args.status_file.resolve(), once=args.once, stop_event=stop_event,
            requested_device_id=args.device_id,
        )
    except httpx.HTTPError as exc:
        response = getattr(exc, "response", None)
        detail = response.text[:300] if response is not None else str(exc)
        print(f"Virtual device protocol failed: {detail}", flush=True)
        return 1
    except Exception as exc:
        print(f"Virtual device stopped with error: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
