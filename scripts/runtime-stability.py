#!/usr/bin/env python3
"""Run an isolated REAL-mode OpenCV, WebSocket and browser soak audit.

This proves sustained capture from one local OpenCV index and resource cleanup
only.  A numeric index can still be backed by a system virtual-camera driver; it
does not hardware-attest a physical lens.  The audit
deliberately registers an ArUco id that is absent from the scene, so a passing
run must create zero item observations, events and evidence media.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import cv2
import httpx
import numpy as np
from websockets.sync.client import connect as websocket_connect


ROOT = Path(__file__).resolve().parents[1]
TEMPORARY_ROOT = (ROOT / "data" / "temporary").resolve()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def tree_stats(root: Path) -> dict[str, int]:
    files = bytes_used = 0
    if root.exists():
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    files += 1
                    bytes_used += path.stat().st_size
                except OSError:
                    pass
    return {"files": files, "bytes": bytes_used}


def process_stats(pid: int) -> dict[str, Any]:
    command = (
        f"$p=Get-Process -Id {int(pid)} -ErrorAction Stop;"
        "$p | Select-Object Id,WorkingSet64,PrivateMemorySize64,CPU,Handles,"
        "@{n='ThreadCount';e={$_.Threads.Count}} | ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    if completed.returncode:
        return {"error": completed.stderr.strip() or completed.stdout.strip()}
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"error": "process stats were not valid JSON"}


def wait_for_server(process: subprocess.Popen[Any], base_url: str, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"backend exited before readiness: {process.returncode}")
        try:
            response = httpx.get(f"{base_url}/api/health", timeout=1)
            if response.status_code == 200 and response.json().get("runtime_mode") == "REAL":
                return
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(0.25)
    raise RuntimeError("REAL backend did not become ready in 45 seconds")


def camera_sample_is_continuous(sample: dict[str, Any]) -> bool:
    """Accept live startup warm-up without accepting an offline/stale source.

    The first decoded frame can arrive while the presentation label is still
    ``connecting``.  ``status_code=STREAMING`` is the capture-layer authority;
    positive frame progress, a live owner thread and no read/error signal keep
    this fail-closed for actual disconnects.
    """
    camera = sample.get("camera") or {}
    sequence = camera.get("source_frame_sequence")
    status_live = (
        camera.get("status") in {"ready", "running", "streaming"}
        or camera.get("status_code") == "STREAMING"
    )
    return bool(
        status_live
        and camera.get("capture_thread_alive") is True
        and isinstance(sequence, int)
        and sequence > 0
        and not camera.get("error")
        and (camera.get("source_read_failures") or 0) == 0
    )


class WebSocketProbe:
    def __init__(self, url: str, cookie: str, origin: str, on_payload: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.url = url
        self.cookie = cookie
        self.origin = origin
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.messages = 0
        self.disconnects = 0
        self.errors: list[str] = []
        self.last_payload: dict[str, Any] | None = None
        self.on_payload = on_payload

    def start(self) -> None:
        def run() -> None:
            while not self.stop.is_set():
                try:
                    with websocket_connect(
                        self.url,
                        origin=self.origin,
                        additional_headers={"Cookie": self.cookie},
                        open_timeout=10,
                        close_timeout=5,
                    ) as socket:
                        while not self.stop.is_set():
                            try:
                                raw = socket.recv(timeout=2)
                            except TimeoutError:
                                continue
                            self.messages += 1
                            try:
                                self.last_payload = json.loads(raw)
                                if self.on_payload and isinstance(self.last_payload, dict):
                                    self.on_payload(self.last_payload)
                            except (TypeError, json.JSONDecodeError):
                                pass
                except Exception as exc:  # evidence collector must survive reconnects
                    self.disconnects += 1
                    if len(self.errors) < 20:
                        self.errors.append(f"{type(exc).__name__}: {exc}")
                    self.stop.wait(1)

        self.thread = threading.Thread(target=run, name="audit-camera-websocket", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=8)


class AnomalyEvidenceCollector:
    """Retain at most three diagnostic JPEGs, never claim atomic frame binding.

    The frame API exposes no frame number, source-session ID or source timestamp.
    Marker/status and camera health requests are separate observations. Even if
    their counters match, that cannot prove the JPEG is the triggering frame.
    """
    def __init__(self, client: httpx.Client, camera_id: str, item_id: str, output_dir: Path, *, frame_limit: int = 3):
        if not 0 <= frame_limit <= 3:
            raise ValueError("anomaly frame limit must be between 0 and 3")
        self.client = client
        self.camera_id = camera_id
        self.item_id = item_id
        self.output_dir = output_dir.resolve()
        self.frame_limit = frame_limit
        self.records: list[dict[str, Any]] = []
        self.first_trigger: dict[str, Any] | None = None
        self.lock = threading.Lock()

    @staticmethod
    def detected_markers(frame: np.ndarray) -> list[dict[str, Any]]:
        detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50))
        corners, ids, _rejected = detector.detectMarkers(frame)
        return [] if ids is None else [
            {"aruco_id": int(marker_id), "corners_pixels": corner.reshape(-1, 2).tolist()}
            for marker_id, corner in zip(ids.flatten().tolist(), corners)
        ]

    def observe_websocket(self, payload: dict[str, Any]) -> None:
        tracks = payload.get("tracks") or []
        matching = [row for row in tracks if isinstance(row, dict) and row.get("item_id") == self.item_id]
        trigger = {"kind": "unexpected_registered_item_current_state", "observed_at": now(), "current_states": matching} if matching else None
        self.capture(trigger, websocket_payload=payload)

    def observe_sample(self, response: httpx.Response, frame: np.ndarray, sample: dict[str, Any]) -> None:
        if self.frame_limit == 0 or len(self.records) >= self.frame_limit:
            return
        markers = self.detected_markers(frame)
        registered = [marker for marker in markers if marker["aruco_id"] == 49]
        trigger = {"kind": "registered_tag_49_in_sampled_jpeg", "observed_at": now(), "markers": registered} if registered else None
        self.capture(trigger, supplied_frame=response, sample=sample)

    def capture(self, trigger: dict[str, Any] | None, *, websocket_payload: dict[str, Any] | None = None, supplied_frame: httpx.Response | None = None, sample: dict[str, Any] | None = None) -> None:
        with self.lock:
            if self.frame_limit == 0 or len(self.records) >= self.frame_limit:
                return
            if not self.first_trigger:
                if not trigger:
                    return
                self.first_trigger = trigger
            record: dict[str, Any] = {
                "index": len(self.records) + 1, "captured_at": now(),
                "camera_id": self.camera_id, "item_id": self.item_id, "registered_aruco_id": 49,
                "trigger": self.first_trigger, "followup_trigger": trigger,
                "alignment": "not_same_frame", "not_same_frame": True,
                "alignment_reason": "The JPEG endpoint does not bind frame number, source_session_id or source timestamp; separate API/WS observations cannot prove this is the triggering inference frame.",
                "errors": [], "websocket_current_states": (websocket_payload or {}).get("tracks") or [],
                "sample_observation": sample,
            }
            # Reserve the slot before I/O. Failed attempts are bounded as well.
            self.records.append(record)

            def read_json(path: str, **kwargs: Any) -> dict[str, Any] | None:
                try:
                    response = self.client.get(path, timeout=2, **kwargs)
                    response.raise_for_status()
                    value = response.json()
                    if not isinstance(value, dict):
                        raise ValueError("response was not an object")
                    return value
                except (httpx.HTTPError, ValueError) as exc:
                    record["errors"].append(f"{path}: {type(exc).__name__}: {exc}")
                    return None

            before = read_json(f"/api/cameras/{self.camera_id}") or {}
            record["camera_health_before"] = before.get("health") or {}
            record["marker_status"] = read_json("/api/acceptance/marker-status", params={"item_id": self.item_id, "camera_id": self.camera_id})
            frame_record: dict[str, Any] = {
                "source_frame": None, "source_session_id": None, "source_timestamp": None,
                "requested_at": None if supplied_frame is not None else now(),
                "was_existing_sample_response": supplied_frame is not None,
            }
            record["frame"] = frame_record
            try:
                response = supplied_frame if supplied_frame is not None else self.client.get(f"/api/cameras/{self.camera_id}/frame", timeout=2)
                frame_record["received_at"] = now()
                response.raise_for_status()
                content = response.content
                decoded = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
                if decoded is None:
                    raise ValueError("frame response was not a decodable JPEG")
                frame_record.update({
                    "sha256": hashlib.sha256(content).hexdigest(), "width": int(decoded.shape[1]), "height": int(decoded.shape[0]),
                    "response_provenance": {name: response.headers.get(name) for name in (
                        "X-ObjectMemory-Runtime-Mode", "X-ObjectMemory-Source-Type", "X-ObjectMemory-Is-Simulated",
                    )},
                    "independent_jpeg_marker_detection": self.detected_markers(decoded),
                })
                self.output_dir.mkdir(parents=True, exist_ok=True)
                frame_path = self.output_dir / f"anomaly-{record['index']:02d}.jpg"
                frame_path.write_bytes(content)
                frame_record["path"] = str(frame_path)
            except (httpx.HTTPError, OSError, ValueError, cv2.error) as exc:
                record["errors"].append(f"frame: {type(exc).__name__}: {exc}")
            after = read_json(f"/api/cameras/{self.camera_id}") or {}
            record["camera_health_after"] = after.get("health") or {}
            try:
                self.output_dir.mkdir(parents=True, exist_ok=True)
                diagnostic_path = self.output_dir / f"anomaly-{record['index']:02d}.json"
                record["diagnostic_path"] = str(diagnostic_path)
                diagnostic_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError as exc:
                record["errors"].append(f"diagnostic_json: {type(exc).__name__}: {exc}")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "enabled": self.frame_limit > 0, "frame_limit": self.frame_limit,
                "triggered": self.first_trigger is not None, "first_trigger": self.first_trigger,
                "attempts": len(self.records), "saved_frames": sum(bool(row.get("frame", {}).get("path")) for row in self.records),
                "output_dir": str(self.output_dir) if self.records else None, "records": list(self.records),
                "diagnostic_only": True, "same_inference_frame_claimed": False,
            }


def database_snapshot(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if not path.is_file():
        return result
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        result["integrity_check"] = connection.execute("PRAGMA integrity_check").fetchone()[0]
        result["counts"] = {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ("movement_events", "item_current_state", "tracks", "event_media", "source_sessions")
        }
        connection.close()
    except sqlite3.Error as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def graceful_stop(process: subprocess.Popen[Any], shutdown_file: Path) -> tuple[bool, int | None]:
    if process.poll() is not None:
        return False, process.returncode
    try:
        shutdown_file.write_text("shutdown\n", encoding="utf-8")
        process.wait(timeout=30)
        return process.returncode == 0, process.returncode
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.send_signal(signal.SIGINT)
            process.wait(timeout=20)
            return process.returncode == 0, process.returncode
        except (OSError, subprocess.TimeoutExpired):
            pass
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        return False, process.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-seconds", type=int, default=1800)
    parser.add_argument("--sample-seconds", type=int, default=30)
    parser.add_argument("--port", type=int, default=8047)
    parser.add_argument("--camera-index", type=int, default=0)
    parser.add_argument("--output", type=Path, default=ROOT / "audit" / "runtime-stability.json")
    parser.add_argument("--skip-browser", action="store_true")
    parser.add_argument("--quick-smoke", action="store_true", help="Diagnostic only; never counts as the required 30-minute audit.")
    parser.add_argument("--anomaly-frame-limit", type=int, choices=range(4), default=3, help="Retain at most 0–3 diagnostic JPEGs after the first unexpected current-state/tag-49 observation; 0 disables sampling.")
    parser.add_argument("--anomaly-evidence-dir", type=Path, help="Persistent anomaly evidence directory, outside the generated temporary data root.")
    args = parser.parse_args()
    if args.duration_seconds <= 0 or args.sample_seconds <= 0:
        parser.error("duration and sample interval must be positive")
    if not args.quick_smoke and args.duration_seconds < 1800:
        parser.error("formal stability audit requires at least 1800 seconds; use --quick-smoke for diagnostics")
    if not args.quick_smoke and args.skip_browser:
        parser.error("formal stability audit requires the real frontend browser probe")
    output = args.output if args.output.is_absolute() else (ROOT / args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    data_root = (TEMPORARY_ROOT / f"runtime-stability-{run_id}").resolve()
    if not data_root.is_relative_to(TEMPORARY_ROOT) or not data_root.name.startswith("runtime-stability-"):
        raise RuntimeError("refusing an unconfined stability data path")
    data_root.mkdir(parents=True)
    anomaly_dir = (args.anomaly_evidence_dir or output.parent / f"{output.stem}-anomaly-{run_id}").resolve()
    if anomaly_dir == data_root or anomaly_dir.is_relative_to(data_root):
        parser.error("anomaly evidence must be outside the temporary data root that is cleaned after the run")
    base_url = f"http://127.0.0.1:{args.port}"
    report: dict[str, Any] = {
        "schema_version": 1,
        "started_at": now(),
        "requested_duration_seconds": args.duration_seconds,
        "sample_interval_seconds": args.sample_seconds,
        "runtime_mode": "REAL",
        "source_type": "opencv_camera",
        "is_simulated": False,
        "camera_index": args.camera_index,
        "scope": "local OpenCV index frame capture and long-running resource stability; no hardware identity or item-recognition claim",
        "verification_level": "quick_smoke" if args.quick_smoke else "formal_30_minute_or_longer",
        "formal_run": not args.quick_smoke,
        "browser_skipped": bool(args.skip_browser),
        "isolated_data_root": str(data_root),
        "samples": [],
        "errors": [],
    }
    environment = os.environ.copy()
    environment.update({
        "OM_DATA_DIR": str(data_root),
        "OM_RUNTIME_MODE": "REAL",
        "OM_RUN_MODE": "REAL",
        "OM_PORT": str(args.port),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    log_path = data_root / "backend.log"
    shutdown_file = data_root / "request-shutdown"
    pid_file = data_root / "backend.pid"
    log_handle = log_path.open("w", encoding="utf-8", newline="\n")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    backend = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts" / "serve.py"),
            "--port",
            str(args.port),
            "--mode",
            "REAL",
            "--no-browser",
            "--shutdown-file",
            str(shutdown_file),
            "--pid-file",
            str(pid_file),
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    browser: subprocess.Popen[Any] | None = None
    ws_probe: WebSocketProbe | None = None
    client: httpx.Client | None = None
    camera_id: str | None = None
    anomaly_collector: AnomalyEvidenceCollector | None = None
    started_monotonic = time.monotonic()
    try:
        wait_for_server(backend, base_url)
        try:
            server_pid = int(pid_file.read_text(encoding="ascii").strip())
        except (OSError, ValueError) as exc:
            raise RuntimeError("backend did not publish its actual server PID") from exc
        report["backend_process"] = {"launcher_pid": backend.pid, "server_pid": server_pid}
        client = httpx.Client(base_url=base_url, timeout=20)
        session = client.get("/api/session")
        session.raise_for_status()
        if not session.json().get("authenticated"):
            raise RuntimeError("localhost session was not authenticated")
        item = client.post("/api/items", json={
            "name": "稳定性审计标签（画面中不存在）",
            "type": "audit",
            "aliases": ["stability-audit-absent-marker"],
            "aruco_id": 49,
            "ring_enabled": False,
        })
        item.raise_for_status()
        camera = client.post("/api/cameras", json={
            "name": "本机 OpenCV 稳定性审计",
            "room_name": "审计环境",
            "installation": "临时隔离验收；仅证明本机 OpenCV 索引取帧",
            "source_type": "webcam",
            "source": str(args.camera_index),
            "config": {"index": args.camera_index},
            "enabled": True,
            "inference_fps": 5,
            "save_clips": True,
        })
        camera.raise_for_status()
        camera_id = camera.json()["id"]
        anomaly_collector = AnomalyEvidenceCollector(client, camera_id, item.json()["id"], anomaly_dir, frame_limit=args.anomaly_frame_limit)
        for zone_id, name, points in (
            ("left", "左侧", [[0, 0], [.49, 0], [.49, 1], [0, 1]]),
            ("right", "右侧", [[.51, 0], [1, 0], [1, 1], [.51, 1]]),
        ):
            response = client.post(f"/api/cameras/{camera_id}/zones", json={"name": name, "points": points, "priority": 1, "enabled": True})
            response.raise_for_status()
        start = client.post(f"/api/cameras/{camera_id}/start")
        start.raise_for_status()
        report["camera_start"] = start.json()
        cookie = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
        ws_probe = WebSocketProbe(
            f"ws://127.0.0.1:{args.port}/ws/cameras/{camera_id}", cookie, base_url,
            on_payload=anomaly_collector.observe_websocket,
        )
        ws_probe.start()
        browser_result = data_root / "browser.json"
        if not args.skip_browser:
            browser = subprocess.Popen(
                [
                    "node.exe",
                    str(ROOT / "apps" / "web" / "scripts" / "runtime-stability-browser.mjs"),
                    base_url,
                    camera_id,
                    str(args.duration_seconds * 1000),
                    str(browser_result),
                ],
                cwd=ROOT / "apps" / "web",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        soak_started = time.monotonic()
        deadline = soak_started + args.duration_seconds
        while time.monotonic() < deadline:
            sample_started = time.monotonic()
            try:
                camera_response=client.get(f"/api/cameras/{camera_id}");camera_response.raise_for_status();camera_state=camera_response.json()
                if not isinstance(camera_state,dict):raise ValueError("camera response was not an object")
                health = camera_state.get("health") or {}
                events_response=client.get(f"/api/events?camera_id={camera_id}&limit=10000");events_response.raise_for_status();events=events_response.json()
                storage_response=client.get("/api/storage/status");storage_response.raise_for_status();storage=storage_response.json()
                frame_response=client.get(f"/api/cameras/{camera_id}/frame");frame_response.raise_for_status()
                if not isinstance(events,list) or not isinstance(storage,dict):raise ValueError("events/storage response schema was invalid")
                frame_bytes=frame_response.content
                decoded_frame=cv2.imdecode(np.frombuffer(frame_bytes,np.uint8),cv2.IMREAD_COLOR)
                if decoded_frame is None:raise ValueError("camera frame response was not a decodable JPEG")
                sample = {
                    "captured_at": now(),
                    "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
                    "process": process_stats(server_pid),
                    "camera": {
                        key: health.get(key) for key in (
                            "status", "status_code", "fps", "latency_ms", "width", "height",
                            "dropped_frames", "reconnects", "source_read_failures", "consecutive_failures",
                            "frames_read", "queue_size", "queue_dropped_frames",
                            "capture_thread_alive", "source_session_id", "source_frame_sequence",
                            "source_session_started_at", "reconnect_epoch",
                            "ring_buffer_frames", "ring_buffer_bytes", "pending_media_jobs", "error",
                        )
                    },
                    "events": len(events),
                    "frame_sha256": hashlib.sha256(frame_bytes).hexdigest(),
                    "frame_width": int(decoded_frame.shape[1]),
                    "frame_height": int(decoded_frame.shape[0]),
                    "storage_bytes": storage.get("total_managed_bytes"),
                    "data_tree": tree_stats(data_root),
                }
                report["samples"].append(sample)
                anomaly_collector.observe_sample(frame_response, decoded_frame, sample)
                print(
                    f"stability {sample['elapsed_seconds']:.0f}s "
                    f"frames={health.get('source_frame_sequence')} events={len(events)} "
                    f"working_set={sample['process'].get('WorkingSet64')}",
                    flush=True,
                )
            except (httpx.HTTPError, ValueError) as exc:
                report["errors"].append(f"sample: {type(exc).__name__}: {exc}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(max(0.0, args.sample_seconds - (time.monotonic() - sample_started)), remaining))

        final_events_response=client.get(f"/api/events?camera_id={camera_id}&limit=10000");final_events_response.raise_for_status();final_events=final_events_response.json()
        search_response=client.post("/api/search", json={"query": "稳定性审计标签在哪里"});search_response.raise_for_status();search=search_response.json()
        storage_response=client.get("/api/storage/status");storage_response.raise_for_status();storage=storage_response.json()
        camera_final_response=client.get(f"/api/cameras/{camera_id}");camera_final_response.raise_for_status();camera_final=camera_final_response.json()
        if not isinstance(final_events,list) or not isinstance(search,dict) or not isinstance(storage,dict) or not isinstance(camera_final,dict):raise ValueError("final API response schema was invalid")
        report["soak_duration_seconds"] = round(time.monotonic() - soak_started, 3)
        report["final_api"] = {
            "event_count": len(final_events),
            "events": final_events,
            "search": search,
            "storage": storage,
            "camera": camera_final,
        }
        if browser:
            try:
                browser.wait(timeout=45)
            except subprocess.TimeoutExpired:
                browser.terminate()
                browser.wait(timeout=10)
            report["browser_process_exit_code"] = browser.returncode
            if browser.stdout:
                report["browser_process_output"] = browser.stdout.read()[-4000:]
        if browser_result.is_file():
            report["browser"] = json.loads(browser_result.read_text(encoding="utf-8"))
        if ws_probe:
            ws_probe.close()
            report["websocket"] = {
                "messages": ws_probe.messages,
                "disconnects": ws_probe.disconnects,
                "errors": ws_probe.errors,
                "last_payload": ws_probe.last_payload,
            }
        client.post(f"/api/cameras/{camera_id}/stop").raise_for_status()
        db_path = data_root / "database" / "objectmemory.sqlite"
        report["database"] = database_snapshot(db_path)
    except Exception as exc:
        report["errors"].append(f"fatal: {type(exc).__name__}: {exc}")
    finally:
        if ws_probe:
            ws_probe.close()
        if anomaly_collector:
            report["anomaly_diagnostics"] = anomaly_collector.snapshot()
        if client:
            if camera_id:
                try:
                    client.post(f"/api/cameras/{camera_id}/stop", timeout=10)
                except httpx.HTTPError:
                    pass
            client.close()
        if browser and browser.poll() is None:
            browser.terminate()
            try:
                browser.wait(timeout=10)
            except subprocess.TimeoutExpired:
                browser.kill()
        graceful, return_code = graceful_stop(backend, shutdown_file)
        log_handle.close()
        report["backend_shutdown"] = {"graceful": graceful, "return_code": return_code}
        try:
            report["backend_log_tail"] = log_path.read_text(encoding="utf-8", errors="replace")[-8000:]
        except OSError:
            report["backend_log_tail"] = ""
        capture = cv2.VideoCapture(args.camera_index, cv2.CAP_MSMF if os.name == "nt" else cv2.CAP_ANY)
        try:
            acquired = capture.isOpened()
            read_ok, frame = capture.read() if acquired else (False, None)
            report["post_shutdown_camera_reacquire"] = {
                "opened": acquired,
                "read_frame": bool(read_ok and frame is not None),
                "width": int(frame.shape[1]) if read_ok and frame is not None else 0,
                "height": int(frame.shape[0]) if read_ok and frame is not None else 0,
            }
        finally:
            capture.release()
        report["ended_at"] = now()
        report["observed_duration_seconds"] = round(time.monotonic() - started_monotonic, 3)
        samples = report["samples"]
        working = [sample["process"].get("WorkingSet64") for sample in samples if isinstance(sample.get("process"), dict) and isinstance(sample["process"].get("WorkingSet64"), (int, float))]
        threads = [sample["process"].get("ThreadCount") for sample in samples if isinstance(sample.get("process"), dict) and isinstance(sample["process"].get("ThreadCount"), (int, float))]
        browser_samples = (report.get("browser") or {}).get("samples") or []
        browser_heap = [sample.get("js_heap_used_bytes") for sample in browser_samples if isinstance(sample.get("js_heap_used_bytes"), (int, float))]
        final_api = report.get("final_api") or {}
        db_counts = (report.get("database") or {}).get("counts") or {}
        frame_hashes=[sample.get("frame_sha256") for sample in samples if sample.get("frame_sha256")]
        sequences=[(sample.get("camera") or {}).get("source_frame_sequence") for sample in samples]
        sessions=[(sample.get("camera") or {}).get("source_session_id") for sample in samples]
        source_failures=[int((sample.get("camera") or {}).get("source_read_failures") or 0) for sample in samples]
        queue_drops=[int((sample.get("camera") or {}).get("queue_dropped_frames") or 0) for sample in samples]
        pending_media=[int((sample.get("camera") or {}).get("pending_media_jobs") or 0) for sample in samples]
        websocket=report.get("websocket") or {};last_ws=(websocket.get("last_payload") or {}).get("camera") or {}
        search_results=(final_api.get("search") or {}).get("results") or []
        search_empty=bool(search_results) and search_results[0].get("last_confirmed") is None and search_results[0].get("answer")=="暂时没有真实摄像头产生的位置记录。"
        report["summary"] = {
            "sample_count": len(samples),
            "working_set_first_bytes": working[0] if working else None,
            "working_set_last_bytes": working[-1] if working else None,
            "working_set_delta_bytes": working[-1] - working[0] if len(working) >= 2 else None,
            "working_set_peak_bytes": max(working) if working else None,
            "thread_count_first": threads[0] if threads else None,
            "thread_count_last": threads[-1] if threads else None,
            "thread_count_peak": max(threads) if threads else None,
            "source_read_failures_max": max(source_failures) if source_failures else None,
            "queue_dropped_frames_max": max(queue_drops) if queue_drops else None,
            "pending_media_jobs_max": max(pending_media) if pending_media else None,
            "browser_heap_first_bytes": browser_heap[0] if browser_heap else None,
            "browser_heap_last_bytes": browser_heap[-1] if browser_heap else None,
            "browser_heap_delta_bytes": browser_heap[-1] - browser_heap[0] if len(browser_heap) >= 2 else None,
            "event_count": db_counts.get("movement_events"),
            "api_event_count": final_api.get("event_count"),
            "current_state_count": db_counts.get("item_current_state"),
            "media_row_count": db_counts.get("event_media"),
            "track_count": db_counts.get("tracks"),
            "physical_item_confirmations": 0,
            "local_opencv_frame_capture_only": bool(samples and (samples[-1].get("camera") or {}).get("source_frame_sequence", 0) > 0),
            "physical_camera_identity_attested": False,
            "physical_camera_frame_capture_only": False,
            "item_recognition_claimed": False,
        }
        report["acceptance"] = {
            "formal_duration_policy": bool(args.quick_smoke or (args.duration_seconds>=1800 and report.get("soak_duration_seconds",0)>=1800)),
            "duration_reached": report.get("soak_duration_seconds", 0) >= args.duration_seconds,
            "camera_streamed": bool(samples and (samples[-1].get("camera") or {}).get("source_frame_sequence", 0) > 0),
            "camera_continuous": bool(samples) and all(camera_sample_is_continuous(sample) for sample in samples),
            "frame_sequence_progressed": bool(samples) and (
                (samples[-1].get("camera") or {}).get("source_frame_sequence", 0)
                > (samples[0].get("camera") or {}).get("source_frame_sequence", -1)
            ),
            "frame_sequence_strictly_increasing": len(sequences)>=2 and all(isinstance(first,int) and isinstance(second,int) and second>first for first,second in zip(sequences,sequences[1:])),
            "frame_content_changed": len(set(frame_hashes))>=2,
            "source_session_unchanged": bool(sessions) and None not in sessions and len(set(sessions))==1,
            "no_camera_reconnect": bool(samples) and all(((sample.get("camera") or {}).get("reconnects") or 0)==0 and ((sample.get("camera") or {}).get("reconnect_epoch") or 0)==0 for sample in samples),
            "no_source_read_failures": bool(source_failures) and max(source_failures) == 0,
            "frame_queue_bounded": bool(samples) and max(
                (sample.get("camera") or {}).get("queue_size", 0) or 0 for sample in samples
            ) <= 2,
            "media_queue_bounded": bool(pending_media) and max(pending_media) <= 1,
            "api_database_event_counts_match": report["summary"]["event_count"]==report["summary"]["api_event_count"]==0,
            "zero_unobserved_events": report["summary"]["event_count"] == 0 and report["summary"]["current_state_count"]==0 and report["summary"]["track_count"]==0,
            "honest_empty_search": search_empty,
            "zero_media": report["summary"]["media_row_count"] == 0 and (final_api.get("storage") or {}).get("screenshot_size", 0) == 0 and (final_api.get("storage") or {}).get("clip_size", 0) == 0,
            "database_integrity_ok": (report.get("database") or {}).get("integrity_check") == "ok",
            "websocket_continuous_and_authentic": websocket.get("messages",0)>=max(2,len(samples)) and websocket.get("disconnects")==0 and not websocket.get("errors") and last_ws.get("id")==camera_id and last_ws.get("runtime_mode")=="REAL" and last_ws.get("is_simulated") is False,
            "frontend_loaded": bool((report.get("browser") or {}).get("live_page_visible")) if not args.skip_browser else False,
            "frontend_mode_verified": bool((report.get("browser") or {}).get("mode_banner_visible")) if not args.skip_browser else False,
            "frontend_duration_reached": bool((report.get("browser") or {}).get("elapsed_ms",0)>=args.duration_seconds*1000) if not args.skip_browser else False,
            "frontend_process_succeeded": report.get("browser_process_exit_code")==0 and not (report.get("browser") or {}).get("error") if not args.skip_browser else False,
            "frontend_no_page_errors": not (report.get("browser") or {}).get("page_errors") if not args.skip_browser else False,
            "frontend_no_console_errors": not (report.get("browser") or {}).get("console_errors") if not args.skip_browser else False,
            "frontend_no_failed_requests": not (report.get("browser") or {}).get("failed_requests") if not args.skip_browser else False,
            "backend_memory_growth_bounded": report["summary"]["working_set_delta_bytes"] is not None and report["summary"]["working_set_delta_bytes"] < 128 * 1024 * 1024,
            "backend_thread_growth_bounded": report["summary"]["thread_count_first"] is not None and report["summary"]["thread_count_last"] <= report["summary"]["thread_count_first"] + 4,
            "frontend_heap_growth_bounded": (report["summary"]["browser_heap_delta_bytes"] is not None and report["summary"]["browser_heap_delta_bytes"] < 64 * 1024 * 1024) if not args.skip_browser else False,
            "backend_stopped_cleanly": report["backend_shutdown"]["graceful"] and report["backend_shutdown"]["return_code"] == 0,
            "camera_released": bool(report["post_shutdown_camera_reacquire"].get("read_frame")),
        }
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Generated REAL audit data remains temporary. The report and at most
        # three explicitly labelled anomaly JPEG/JSON pairs live outside it.
        try:
            shutil.rmtree(data_root)
            report["temporary_data_cleaned"] = True
        except OSError as exc:
            report["temporary_data_cleaned"] = False
            report["errors"].append(f"temporary cleanup: {type(exc).__name__}: {exc}")
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    critical = report.get("acceptance") or {}
    passed = bool(critical) and all(value is True for value in critical.values()) and not report["errors"]
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
