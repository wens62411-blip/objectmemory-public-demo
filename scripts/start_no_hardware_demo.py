#!/usr/bin/env python3
"""Own the no-hardware demo backend and request a real virtual-device lifecycle."""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]


def port_available(port: int) -> bool:
    with socket.socket() as sock:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def wait_for_backend(process: subprocess.Popen, url: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"物忆后端启动失败，退出码 {process.returncode}")
        try:
            response = httpx.get(url + "/api/health", timeout=1)
            if response.status_code == 200 and response.json().get("status") == "ok":
                return
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(0.25)
    raise RuntimeError("30 秒内没有等到物忆后端健康检查通过")


def verify_demo(client: httpx.Client, started_at: str) -> None:
    output = ROOT / "data" / "verification" / "no-hardware-demo.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 50
    last_status: dict = {}
    while time.monotonic() < deadline:
        try:
            device = client.get("/api/virtual-device/status").json()
            last_status = device
            camera_id = device.get("camera_id")
            if not camera_id:
                time.sleep(1)
                continue
            events_response = client.get(
                "/api/events",
                params={"camera_id": camera_id, "event_type": "movement", "limit": 20},
            )
            events_response.raise_for_status()
            placed = next((event for event in events_response.json()
                           if event.get("item_name") == "我的手机"
                           and (event.get("to_zone") or event.get("zone_name")) == "沙发右侧"
                           and event.get("evidence_status") == "confirmed"
                           and event.get("final_status") == "confirmed_placed"
                           and str(event.get("timestamp_start") or "") >= started_at), None)
            result = client.post("/api/search", json={"query": "我的手机在哪里"})
            result.raise_for_status()
            rows = result.json().get("results", [])
            confirmed = (rows[0].get("last_confirmed") or {}) if rows else {}
            search_matches = bool(
                placed and confirmed.get("camera_id") == camera_id
                and confirmed.get("zone_name") == "沙发右侧"
                and str(confirmed.get("timestamp_start") or "") >= str(placed.get("timestamp_start") or "")
            )
            if (device.get("backend_online") and device.get("stream_status") == "ready"
                    and placed and search_matches):
                firmware = client.get("/api/firmware/manifest")
                firmware.raise_for_status()
                firmware_status = firmware.json()
                runtime = client.get("/api/runtime-config")
                runtime.raise_for_status()
                evidence = {
                    "passed": True,
                    "mode": "no_hardware_demo",
                    "explicitly_not_physical_hardware": True,
                    "started_at": started_at,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "device_id": device.get("device_id"),
                    "camera_id": camera_id,
                    "device_online": True,
                    "stream_status": "ready",
                    "event_id": placed.get("event_id") or placed.get("id"),
                    "event_type": placed.get("event_type"),
                    "item_name": placed.get("item_name"),
                    "zone_name": placed.get("zone_name"),
                    "event_timestamp": placed.get("timestamp_start"),
                    "search_confirmed_same_camera": True,
                    "screenshot_path": placed.get("screenshot_path"),
                    "clip_path": placed.get("clip_path"),
                    "runtime_mode": runtime.json().get("mode"),
                    "firmware": {
                        "compile_passed": firmware_status.get("compile_passed"),
                        "manifest_hashes_verified": firmware_status.get("manifest_hashes_verified"),
                        "manifest_path": firmware_status.get("manifest_path"),
                        "physical_board_detected": firmware_status.get("physical_board_detected"),
                        "physical_flash_performed": firmware_status.get("physical_flash_performed"),
                        "serial_provision_performed": firmware_status.get("serial_provision_performed"),
                        "real_video_verified": firmware_status.get("real_video_verified"),
                    },
                }
                output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
                print("无硬件闭环已就绪：虚拟设备在线、MJPEG 已验证、手机放置事件可查询。", flush=True)
                return
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
        time.sleep(1)
    output.write_text(json.dumps({
        "passed": False,
        "mode": "no_hardware_demo",
        "explicitly_not_physical_hardware": True,
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "last_device_status": last_status,
        "error": "50 秒内没有得到本次虚拟摄像头的手机沙发 confirmed movement 事件和对应搜索证据",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("无硬件服务仍在运行，但 50 秒内尚未同时看到视频就绪和 confirmed movement 事件；请查看设备页诊断。",
          file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8018)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--source", choices=("video", "webcam"), default="video")
    parser.add_argument("--camera-index", type=int, default=0)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    if not port_available(args.port):
        print(f"端口 {args.port} 已被占用。请关闭旧的物忆进程或使用 -Port 8019。", file=sys.stderr)
        return 1

    environment = os.environ.copy()
    environment["OM_PORT"] = str(args.port)
    environment["OM_RUNTIME_MODE"] = "DEMO"
    environment["OM_RUN_MODE"] = "DEMO"
    environment["OM_DEMO_MODE"] = "no-hardware"  # compatibility for older local builds
    environment["PYTHONUTF8"] = "1"
    command = [sys.executable, str(ROOT / "scripts" / "serve.py"), "--port", str(args.port), "--mode", "DEMO", "--no-browser"]
    backend = subprocess.Popen(command, cwd=str(ROOT), env=environment, shell=False)
    stop_event = threading.Event()

    def stop(*_):
        stop_event.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            try:
                signal.signal(signum, stop)
            except (ValueError, OSError):
                pass

    base_url = f"http://127.0.0.1:{args.port}"
    client: httpx.Client | None = None
    try:
        wait_for_backend(backend, base_url)
        client = httpx.Client(base_url=base_url, timeout=20)
        session = client.get("/api/session")
        session.raise_for_status()
        if not session.json().get("authenticated"):
            raise RuntimeError("本机管理员会话没有建立")
        seeded = client.post("/api/system/demo-seed")
        seeded.raise_for_status()
        demo_started_at = datetime.now(timezone.utc).isoformat()
        started = client.post(
            "/api/virtual-device/start",
            json={
                "source": args.source,
                "camera_index": args.camera_index,
                "device_name": "虚拟 ESP32-CAM",
                "room_name": "客厅",
            },
        )
        started.raise_for_status()
        print(f"物忆无硬件演示：http://127.0.0.1:{args.port}", flush=True)
        print("当前运行模式：无硬件演示", flush=True)
        print(f"视频来源：{'自动生成测试视频' if args.source == 'video' else f'电脑摄像头 {args.camera_index}'}", flush=True)
        print("ESP32 状态：虚拟设备 · 非真实硬件", flush=True)
        print("固件状态：查看设备中心实际构建清单；本脚本不会声称真机刷写。", flush=True)
        if not args.no_browser:
            webbrowser.open(base_url)
        threading.Thread(target=verify_demo, args=(client, demo_started_at), daemon=True,
                         name="no-hardware-demo-verifier").start()
        while not stop_event.wait(0.5):
            if backend.poll() is not None:
                return backend.returncode or 1
        return 0
    except (RuntimeError, httpx.HTTPError) as exc:
        print(f"无硬件演示启动失败：{exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if client is not None:
            try:
                client.post("/api/virtual-device/stop", timeout=12)
            except httpx.HTTPError:
                pass
            client.close()
        if backend.poll() is None:
            backend.terminate()
            try:
                backend.wait(timeout=10)
            except subprocess.TimeoutExpired:
                backend.kill()
                backend.wait(timeout=3)


if __name__ == "__main__":
    raise SystemExit(main())
