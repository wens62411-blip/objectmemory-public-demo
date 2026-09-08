#!/usr/bin/env python3
"""Run a non-mocked virtual ESP32 lifecycle against a real FastAPI process.

This acceptance takes slightly over 45 seconds on purpose: the offline result is
produced by the same heartbeat timeout used for physical devices.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx


ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = (ROOT / "data" / "temporary").resolve()


def choose_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_until(predicate, timeout: float, description: str):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.25)
    raise RuntimeError(f"等待{description}超时；最后结果={last!r}")


def read_mjpeg_frame(url: str) -> tuple[int, str]:
    buffer = b""
    with httpx.stream("GET", url, timeout=8) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        for chunk in response.iter_bytes():
            buffer = (buffer + chunk)[-2_000_000:]
            start = buffer.find(b"\xff\xd8")
            end = buffer.find(b"\xff\xd9", start + 2) if start >= 0 else -1
            if start >= 0 and end >= 0:
                frame = buffer[start:end + 2]
                return len(frame), content_type
    raise RuntimeError("MJPEG 响应中没有读取到 JPEG 帧")


def remove_isolated_tree(path: Path) -> None:
    resolved = path.resolve()
    if not resolved.is_relative_to(TEMP_ROOT) or not resolved.name.startswith("virtual-acceptance-"):
        raise RuntimeError("refusing to remove an unconfined virtual acceptance path")
    last_error: OSError | None = None
    for _ in range(40):
        try:
            shutil.rmtree(resolved)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            time.sleep(.25)
    assert last_error is not None
    raise last_error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path,
                        default=ROOT / "data" / "diagnostics" / "virtual-device-acceptance.json")
    args = parser.parse_args()
    run_id = uuid4().hex[:8]
    data_dir = ROOT / "data" / "temporary" / f"virtual-acceptance-{run_id}"
    data_dir.mkdir(parents=True, exist_ok=False)
    shutdown_file = data_dir / "request-shutdown"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    backend_log = args.output.parent / "virtual-device-backend.log"
    port = choose_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment.update({
        "OM_DATA_DIR": str(data_dir),
        "OM_PORT": str(port),
        "OM_RUNTIME_MODE": "DEMO",
        "OM_DEMO_MODE": "no-hardware-acceptance",
        "OM_RUN_MODE": "DEMO",
        "PYTHONUTF8": "1",
    })
    log_handle = backend_log.open("w", encoding="utf-8", newline="\n")
    backend = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "serve.py"),
         "--port", str(port), "--mode", "DEMO", "--no-browser",
         "--shutdown-file", str(shutdown_file)],
        cwd=str(ROOT), env=environment, stdin=subprocess.DEVNULL,
        stdout=log_handle, stderr=subprocess.STDOUT, shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    client: httpx.Client | None = None
    evidence: dict[str, object] = {
        "test_type": "virtual_device_real_protocol",
        "explicitly_not_physical_hardware": True,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "backend_url": base_url,
        "data_dir": str(data_dir),
        "backend_log": str(backend_log),
    }
    try:
        def backend_ready():
            if backend.poll() is not None:
                raise RuntimeError(f"后端提前退出：{backend.returncode}")
            try:
                response = httpx.get(base_url + "/api/health", timeout=1)
                return response.status_code == 200 and response.json().get("status") == "ok"
            except (httpx.HTTPError, ValueError):
                return False

        wait_until(backend_ready, 25, "真实 FastAPI 启动")
        client = httpx.Client(base_url=base_url, timeout=20)
        session = client.get("/api/session")
        session.raise_for_status()
        assert session.json()["authenticated"] is True
        seed = client.post("/api/system/demo-seed")
        seed.raise_for_status()
        started = client.post(
            "/api/virtual-device/start",
            json={"source": "video", "camera_index": 0,
                  "device_name": "验收虚拟 ESP32-CAM", "room_name": "客厅"},
        )
        started.raise_for_status()

        def device_ready():
            status = client.get("/api/virtual-device/status").json()
            return status if (status.get("running") and status.get("backend_online")
                              and status.get("stream_status") == "ready") else None

        first = wait_until(device_ready, 25, "虚拟设备注册、心跳和视频探测")
        device_id = first["device_id"]
        camera_id = first["camera_id"]
        devices = client.get("/api/devices").json()
        device = next(row for row in devices if row["device_id"] == device_id)
        assert device["simulated"] is True and device["hardware_type"] == "virtual"
        health = httpx.get(device["capture_url"].replace("/capture", "/health"), timeout=5)
        descriptor = httpx.get(device["capture_url"].replace("/capture", "/device"), timeout=5)
        capture = httpx.get(device["capture_url"], timeout=5)
        health.raise_for_status(); descriptor.raise_for_status(); capture.raise_for_status()
        assert health.json()["simulated"] is True
        assert descriptor.json()["device_id"] == device_id
        assert capture.content.startswith(b"\xff\xd8")
        mjpeg_size, mjpeg_type = read_mjpeg_frame(device["stream_url"])
        visual_frame = client.get(f"/api/cameras/{camera_id}/frame")
        visual_frame.raise_for_status()
        assert visual_frame.content.startswith(b"\xff\xd8")

        initial_heartbeat = device["last_heartbeat"]

        def second_heartbeat():
            rows = client.get("/api/devices").json()
            row = next(value for value in rows if value["device_id"] == device_id)
            return row if row.get("last_heartbeat") != initial_heartbeat else None

        second = wait_until(second_heartbeat, 22, "第二次 15 秒心跳")

        def placed_result():
            response = client.post("/api/search", json={"query": "我的手机在哪里"})
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("results", payload if isinstance(payload, list) else [])
            if not rows:
                return None
            result = rows[0]
            confirmed = result.get("last_confirmed") or {}
            return result if confirmed.get("event_type") == "movement" and confirmed.get("evidence_status") == "confirmed" and confirmed.get("final_status") == "confirmed_placed" else None

        placed = wait_until(placed_result, 55, "虚拟 MJPEG 驱动 confirmed movement 事件")
        screenshot_url = (placed.get("last_confirmed") or {}).get("screenshot_path")
        clip_url = (placed.get("last_confirmed") or {}).get("clip_path")
        assert screenshot_url and client.get(screenshot_url).content.startswith(b"\xff\xd8")
        clip = client.get(clip_url)
        clip.raise_for_status()
        assert len(clip.content) > 1024

        db_path = data_dir / "database" / "objectmemory-demo.sqlite"
        connection = sqlite3.connect(db_path)
        try:
            token_hash, = connection.execute(
                "SELECT token_hash FROM firmware_devices WHERE id=?", (device_id,)
            ).fetchone()
            used_pairings, = connection.execute(
                "SELECT COUNT(*) FROM device_enrollments WHERE used_at IS NOT NULL"
            ).fetchone()
        finally:
            connection.close()
        assert len(token_hash) == 64 and used_pairings >= 1

        stopped_at = time.monotonic()
        stopped = client.post("/api/virtual-device/stop")
        stopped.raise_for_status()
        assert stopped.json()["running"] is False

        def offline():
            rows = client.get("/api/devices").json()
            row = next(value for value in rows if value["device_id"] == device_id)
            return row if row["online"] is False else None

        offline_device = wait_until(offline, 52, "45 秒心跳过期离线")
        offline_elapsed = round(time.monotonic() - stopped_at, 2)
        last_heartbeat_time = datetime.fromisoformat(str(second["last_heartbeat"]).replace("Z", "+00:00"))
        heartbeat_age = round((datetime.now(timezone.utc) - last_heartbeat_time).total_seconds(), 2)
        # The 45-second rule starts at the last authenticated heartbeat.  The
        # user may click stop a few seconds later, so elapsed-since-stop can be
        # slightly shorter while elapsed-since-heartbeat must reach the rule.
        assert heartbeat_age >= 44

        restarted = client.post(
            "/api/virtual-device/start",
            json={"source": "video", "camera_index": 0,
                  "device_name": "验收虚拟 ESP32-CAM", "room_name": "客厅"},
        )
        restarted.raise_for_status()

        def online_again():
            status = client.get("/api/virtual-device/status").json()
            return status if status.get("backend_online") and status.get("stream_status") == "ready" else None

        again = wait_until(online_again, 25, "保存令牌后的重新上线")
        assert again["device_id"] == device_id and again["camera_id"] == camera_id
        evidence.update({
            "passed": True,
            "device_id": device_id,
            "camera_id": camera_id,
            "claim": {"single_use_pairing_consumed": True, "token_hash_only_in_database": True},
            "heartbeat": {"interval_configured_seconds": 15,
                          "first": initial_heartbeat, "second": second["last_heartbeat"]},
            "http_contract": {
                "health": health.status_code,
                "device": descriptor.status_code,
                "capture_jpeg_bytes": len(capture.content),
                "stream_content_type": mjpeg_type,
                "stream_jpeg_bytes": mjpeg_size,
                "vision_frame_bytes": len(visual_frame.content),
            },
            "vision_event": {
                "event_type": "movement",
                "zone_name": (placed.get("last_confirmed") or {}).get("zone_name"),
                "screenshot": screenshot_url,
                "clip": clip_url,
                "clip_bytes": len(clip.content),
            },
            "offline": {"online": offline_device["online"],
                        "elapsed_since_stop_seconds": offline_elapsed,
                        "elapsed_since_last_heartbeat_seconds": heartbeat_age,
                        "rule_seconds": 45},
            "restart": {"same_device_id": True, "same_camera_id": True,
                        "online": again["backend_online"], "stream_status": again["stream_status"]},
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        evidence.update({
            "passed": False,
            "error": repr(exc),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        print(json.dumps(evidence, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    finally:
        if client is not None:
            try:
                client.post("/api/virtual-device/stop", timeout=12)
            except httpx.HTTPError:
                pass
            client.close()
        graceful_shutdown = False
        if backend.poll() is None:
            try:
                shutdown_file.write_text("shutdown\n", encoding="utf-8")
                backend.wait(timeout=30)
                graceful_shutdown = backend.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                backend.terminate()
                try:
                    backend.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    backend.kill(); backend.wait(timeout=3)
        log_handle.close()
        evidence["backend_shutdown"] = {"graceful": graceful_shutdown, "return_code": backend.returncode}
        try:
            evidence["backend_log_tail"] = backend_log.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            evidence["backend_log_tail"] = ""
        try:
            remove_isolated_tree(data_dir)
            evidence["temporary_data_cleaned"] = True
        except OSError as cleanup_error:
            evidence["temporary_data_cleaned"] = False
            evidence["cleanup_error"] = repr(cleanup_error)
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
