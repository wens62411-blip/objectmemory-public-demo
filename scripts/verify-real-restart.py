#!/usr/bin/env python3
"""Verify a real FastAPI/SQLite REAL restart without fabricating any event."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
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


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_ready(process: subprocess.Popen[bytes], base_url: str) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"REAL backend exited early with {process.returncode}")
        try:
            response = httpx.get(base_url + "/api/health", timeout=1)
            if response.status_code == 200 and response.json().get("runtime_mode") == "REAL":
                return
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(.25)
    raise RuntimeError("REAL backend did not become ready")


def db_counts(path: Path) -> dict[str, int | str]:
    connection = sqlite3.connect(path)
    try:
        return {
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
            **{
                table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in ("items", "cameras", "movement_events", "item_current_state", "tracks", "event_media")
            },
        }
    finally:
        connection.close()


def remove_isolated_tree(path: Path) -> None:
    resolved = path.resolve()
    if not resolved.is_relative_to(TEMP_ROOT) or not resolved.name.startswith("real-restart-"):
        raise RuntimeError("refusing to remove an unconfined test path")
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


def run_generation(data_root: Path, generation: int, create_item: bool) -> dict[str, object]:
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    shutdown_file = data_root / f"shutdown-{generation}"
    log_path = data_root / f"backend-{generation}.log"
    environment = os.environ.copy()
    environment.update({
        "OM_DATA_DIR": str(data_root),
        "OM_RUNTIME_MODE": "REAL",
        "OM_RUN_MODE": "REAL",
        "OM_PORT": str(port),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    with log_path.open("wb") as log_handle:
        process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "scripts" / "serve.py"),
                "--port", str(port),
                "--mode", "REAL",
                "--no-browser",
                "--shutdown-file", str(shutdown_file),
            ],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            wait_ready(process, base_url)
            with httpx.Client(base_url=base_url, timeout=20) as client:
                session = client.get("/api/session")
                session.raise_for_status()
                if session.json().get("authenticated") is not True:
                    raise RuntimeError("localhost session was not authenticated")
                if create_item:
                    created = client.post("/api/items", json={
                        "name": "重启验收手机",
                        "type": "phone",
                        "aliases": ["restart-audit-phone"],
                        "aruco_id": 49,
                        "ring_enabled": False,
                    })
                    created.raise_for_status()
                page_first = client.get("/")
                page_second = client.get("/")
                events_first = client.get("/api/events?limit=10000")
                events_second = client.get("/api/events?limit=10000")
                search = client.post("/api/search", json={"query": "重启验收手机在哪里"})
                demo_seed = client.post("/api/system/demo-seed")
                virtual = client.post("/api/virtual-device/start", json={"source": "video"})
                for response in (page_first, page_second, events_first, events_second, search):
                    response.raise_for_status()
                search_payload = search.json()
                results = search_payload.get("results") or []
                honest = bool(results) and results[0].get("last_confirmed") is None and results[0].get("answer") == "暂时没有真实摄像头产生的位置记录。"
                if len(events_first.json()) != 0 or len(events_second.json()) != 0 or not honest:
                    raise RuntimeError("REAL refresh/restart check found a fabricated event or location")
                if demo_seed.status_code != 409 or virtual.status_code != 409:
                    raise RuntimeError("REAL mode accepted a DEMO-only entry point")
                snapshot = {
                    "generation": generation,
                    "health": client.get("/api/health").json(),
                    "page_refresh_statuses": [page_first.status_code, page_second.status_code],
                    "event_counts_before_after_refresh": [len(events_first.json()), len(events_second.json())],
                    "search": search_payload,
                    "demo_seed_status": demo_seed.status_code,
                    "virtual_device_status": virtual.status_code,
                }
        finally:
            if process.poll() is None:
                shutdown_file.write_text("shutdown\n", encoding="utf-8")
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=10)
            snapshot = locals().get("snapshot", {})
            snapshot["backend_return_code"] = process.returncode
            snapshot["backend_shutdown_graceful"] = process.returncode == 0
    snapshot["database"] = db_counts(data_root / "database" / "objectmemory.sqlite")
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "audit" / "real-restart.json")
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    run_root = (TEMP_ROOT / f"real-restart-{uuid4().hex[:10]}").resolve()
    if not run_root.is_relative_to(TEMP_ROOT):
        raise RuntimeError("refusing unconfined test path")
    run_root.mkdir(parents=True)
    canonical = ROOT / "data" / "database" / "objectmemory.sqlite"
    report: dict[str, object] = {
        "started_at": now(),
        "runtime_mode": "REAL",
        "source_type": "no_video_source",
        "is_simulated": False,
        "isolated_data_root": str(run_root),
        "canonical_real_sha256_before": sha256(canonical),
    }
    exit_code = 1
    try:
        report["generations"] = [run_generation(run_root, 1, True), run_generation(run_root, 2, False)]
        reports = report["generations"]
        assert isinstance(reports, list)
        report["canonical_real_sha256_after"] = sha256(canonical)
        report["passed"] = all(
            entry.get("backend_shutdown_graceful") is True
            and entry.get("database", {}).get("integrity_check") == "ok"
            and entry.get("database", {}).get("movement_events") == 0
            and entry.get("database", {}).get("item_current_state") == 0
            and entry.get("database", {}).get("event_media") == 0
            for entry in reports
        ) and report["canonical_real_sha256_before"] == report["canonical_real_sha256_after"]
        exit_code = 0 if report["passed"] else 1
    except Exception as exc:
        report["passed"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        report["ended_at"] = now()
        try:
            remove_isolated_tree(run_root)
            report["temporary_data_cleaned"] = True
        except OSError as exc:
            report["temporary_data_cleaned"] = False
            report["cleanup_error"] = f"{type(exc).__name__}: {exc}"
            exit_code = 1
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
