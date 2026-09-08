from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apps.api.app.mode_lock import RuntimeModeLease
from scripts import event_audit


ROOT = Path(__file__).resolve().parents[3]


def _load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit_snapshot = _load_script("objectmemory_audit_snapshot", "audit-snapshot.py")
cleanup_artifacts = _load_script("objectmemory_cleanup_artifacts", "cleanup-audit-artifacts.py")
firmware_builder = _load_script("objectmemory_cleanup_firmware_builder", "firmware-build.py")


@pytest.fixture(params=["cleanup", "firmware"])
def pid_probe(request):
    return cleanup_artifacts.process_alive if request.param == "cleanup" else firmware_builder.ArtifactBuildLock._pid_alive


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only process-handle regression")
def test_windows_pid_probe_never_signals_or_ends_its_live_test_child(pid_probe, monkeypatch, tmp_path):
    # Only this test's newly created child is inspected. No external PID or
    # existing camera/backend process is queried, signalled or stopped.
    def forbidden_signal(*_args):
        pytest.fail("A process-alive query must never call os.kill on Windows")

    monkeypatch.setattr(os, "kill", forbidden_signal)
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", "import time; print('READY', flush=True); time.sleep(2)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        assert child.stdout.readline().strip() == "READY"
        runtime_path = None
        build_lock = None
        if pid_probe is cleanup_artifacts.process_alive:
            data_root = _redirect_cleanup_root(monkeypatch, tmp_path)
            runtime_path = data_root / "temporary" / "runtime-stability-20000101-000000-abcdef12"
            runtime_path.mkdir(parents=True)
            (runtime_path / "backend.pid").write_text(str(child.pid), encoding="utf-8")
            with pytest.raises(RuntimeError, match="still belongs to live PID"):
                cleanup_artifacts.runtime_directory(runtime_path.name)
        else:
            lock_path = tmp_path / "test-build.lock"
            lock_path.write_text(json.dumps({"pid": child.pid}), encoding="utf-8")
            os.utime(lock_path, (1, 1))
            build_lock = firmware_builder.ArtifactBuildLock(lock_path)
            assert build_lock._break_stale() is False
            assert lock_path.is_file()
        for _ in range(20):
            assert pid_probe(child.pid) is True
            assert child.poll() is None
        assert child.wait(timeout=10) == 0  # Natural exit, not a termination call.
        for _ in range(4):
            assert pid_probe(child.pid) is False
        if runtime_path:
            assert cleanup_artifacts.runtime_directory(runtime_path.name) == runtime_path.resolve()
        if build_lock:
            assert build_lock._break_stale() is True
            assert not build_lock.path.exists()
    finally:
        if child.poll() is None:
            # Failure cleanup is restricted to this Popen-owned test child.
            child.terminate()
            child.wait(timeout=10)
        child.stdout.close()
        child.stderr.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows process-open error contract")
@pytest.mark.parametrize("error,expected", [(5, True), (0, True), (6, True), (87, False)])
def test_windows_pid_probe_access_denied_and_unknown_fail_closed(pid_probe, monkeypatch, error, expected):
    import ctypes

    kernel32 = SimpleNamespace(OpenProcess=Mock(return_value=None), WaitForSingleObject=Mock(), CloseHandle=Mock())
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error)
    monkeypatch.setattr(os, "kill", lambda *_args: pytest.fail("Windows must not signal a PID"))
    assert pid_probe(123456) is expected  # Mocked API: never queries this PID.
    kernel32.OpenProcess.assert_called_once_with(0x00100000 | 0x1000, False, 123456)
    kernel32.WaitForSingleObject.assert_not_called()
    kernel32.CloseHandle.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle wait/close contract")
@pytest.mark.parametrize("wait_result,expected", [(0, False), (258, True), (0xFFFFFFFF, True), (128, True)])
def test_windows_pid_probe_closes_handles_and_unknown_wait_fails_closed(pid_probe, monkeypatch, wait_result, expected):
    import ctypes

    kernel32 = SimpleNamespace(OpenProcess=Mock(return_value=42), WaitForSingleObject=Mock(return_value=wait_result), CloseHandle=Mock(return_value=1))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32)
    monkeypatch.setattr(os, "kill", lambda *_args: pytest.fail("Windows must not signal a PID"))
    assert pid_probe(123456) is expected
    kernel32.WaitForSingleObject.assert_called_once_with(42, 0)
    kernel32.CloseHandle.assert_called_once_with(42)


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    result.update(path.read_bytes())
    return result.hexdigest()


def _wal_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, value TEXT)")
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("INSERT INTO evidence(value) VALUES('committed-in-wal')")
    connection.commit()
    assert Path(str(path) + "-wal").stat().st_size > 0
    return connection


def _source_hashes(database: Path) -> dict[str, str]:
    paths = (database, Path(str(database) + "-wal"), Path(str(database) + "-shm"))
    return {str(path): _digest(path) for path in paths if path.is_file()}


def test_event_audit_snapshot_reads_committed_wal_without_touching_source_sidecars(tmp_path: Path):
    database = tmp_path / "objectmemory.sqlite"
    writer = _wal_database(database)
    try:
        before = _source_hashes(database)
        connection, metadata = event_audit.readonly_snapshot(database)
        try:
            assert connection.execute("SELECT value FROM evidence").fetchone()[0] == "committed-in-wal"
        finally:
            connection.close()
        assert metadata["strategy"] == "stable_wal_copy"
        assert metadata["wal_bytes"] > 0
        assert _source_hashes(database) == before
    finally:
        writer.close()


def test_event_audit_snapshot_without_wal_creates_no_source_sidecars(tmp_path: Path):
    database = tmp_path / "plain.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE events(id TEXT PRIMARY KEY)")
    wal = Path(str(database) + "-wal")
    shm = Path(str(database) + "-shm")
    assert not wal.exists() and not shm.exists()

    connection, metadata = event_audit.readonly_snapshot(database)
    connection.close()

    assert metadata["strategy"] == "immutable_base"
    assert not wal.exists() and not shm.exists()


def test_repository_snapshot_includes_committed_wal_and_preserves_source_files(tmp_path: Path, monkeypatch):
    database = tmp_path / "objectmemory.sqlite"
    monkeypatch.setattr(audit_snapshot, "ROOT", tmp_path.resolve())
    writer = _wal_database(database)
    try:
        before = _source_hashes(database)
        result = audit_snapshot.db_snapshot(database)

        assert result["integrity_check"] == "ok"
        assert result["tables"]["evidence"]["count"] == 1
        assert result["snapshot"]["strategy"] == "stable_wal_copy"
        assert _source_hashes(database) == before
    finally:
        writer.close()


def _sidecar_plan(database: Path, *, empty_wal: bool = False) -> list[dict[str, object]]:
    rows = []
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(database) + suffix)
        sidecar.write_bytes(b'' if suffix == '-wal' and empty_wal else suffix.encode("ascii"))
        rows.append(
            {
                "category": "transient_sqlite_sidecar",
                "path": str(sidecar.resolve()),
                "bytes": sidecar.stat().st_size,
            }
        )
    return rows


def _create_plain_database(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE evidence(id INTEGER)")
        connection.commit()
    finally:
        connection.close()


def _redirect_cleanup_root(monkeypatch, root: Path) -> Path:
    data_root = root / "data"
    monkeypatch.setattr(cleanup_artifacts, "ROOT", root.resolve())
    monkeypatch.setattr(cleanup_artifacts, "TEMPORARY", (data_root / "temporary").resolve())
    return data_root


def test_cleanup_refuses_canonical_sidecars_while_backend_mode_lease_is_active(tmp_path: Path, monkeypatch):
    data_root = _redirect_cleanup_root(monkeypatch, tmp_path)
    database = data_root / "database" / "objectmemory.sqlite"
    database.parent.mkdir(parents=True)
    _create_plain_database(database)
    plan = _sidecar_plan(database)

    with RuntimeModeLease(data_root, "REAL"):
        deleted, deleted_bytes, failures = cleanup_artifacts.apply_plan(plan, [])

    assert deleted == 0 and deleted_bytes == 0
    assert failures and "backend/runtime is active" in failures[0]["error"]
    assert all(Path(str(row["path"])).is_file() for row in plan)


def test_cleanup_holds_guard_and_deletes_inactive_empty_wal_sidecars(tmp_path: Path, monkeypatch):
    data_root = _redirect_cleanup_root(monkeypatch, tmp_path)
    database = data_root / "database" / "objectmemory.sqlite"
    database.parent.mkdir(parents=True)
    _create_plain_database(database)
    # Nonempty WAL, even if its writer exited, is never rebuildable cache.
    plan = _sidecar_plan(database, empty_wal=True)

    deleted, deleted_bytes, failures = cleanup_artifacts.apply_plan(plan, [])

    assert not failures
    assert deleted == 2 and deleted_bytes == sum(int(row["bytes"]) for row in plan)
    assert all(not Path(str(row["path"])).exists() for row in plan)


def test_cleanup_refuses_backup_sidecars_while_database_is_active(tmp_path: Path, monkeypatch):
    data_root = _redirect_cleanup_root(monkeypatch, tmp_path)
    database = data_root / "backups" / "pre-audit-latest.sqlite"
    writer = _wal_database(database)
    plan = [
        {
            "category": "transient_sqlite_sidecar",
            "path": str(path.resolve()),
            "bytes": path.stat().st_size,
        }
        for path in (Path(str(database) + "-wal"), Path(str(database) + "-shm"))
    ]
    try:
        deleted, deleted_bytes, failures = cleanup_artifacts.apply_plan(plan, [])
        assert all(Path(str(row["path"])).is_file() for row in plan)
    finally:
        writer.close()

    assert deleted == 0 and deleted_bytes == 0
    assert failures and "database is" in failures[0]["error"]


def _event_cleanup_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE movement_events(
                id TEXT PRIMARY KEY,event_id TEXT,runtime_mode TEXT,source_type TEXT,
                is_simulated INTEGER,pinned INTEGER,manually_corrected INTEGER,
                event_type TEXT,item_id TEXT,camera_id TEXT,created_at TEXT
            )"""
        )
        connection.execute(
            "INSERT INTO movement_events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("event-1","event-1","TEST","test_fixture",1,0,0,"movement","item-1","camera-1","2026-01-01T00:00:00+00:00"),
        )


def test_event_cleanup_apply_refuses_active_runtime_and_changes_nothing(tmp_path: Path):
    data = tmp_path / "data"
    database = data / "database" / "objectmemory-test.sqlite"
    _event_cleanup_database(database)
    report = event_audit.audit([database])
    plan = event_audit.cleanup_plan(report)
    assert plan["delete_events"] == 1

    with RuntimeModeLease(data, "TEST"):
        result = event_audit.apply_cleanup(plan, data)

    assert result["success"] is False
    assert result["deleted_events"] == 0
    assert result["failures"][0]["stage"] == "runtime_mode_lease"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM movement_events").fetchone()[0] == 1


def test_event_cleanup_reaudits_and_preserves_newly_pinned_target(tmp_path: Path):
    data = tmp_path / "data"
    database = data / "database" / "objectmemory-test.sqlite"
    _event_cleanup_database(database)
    plan = event_audit.cleanup_plan(event_audit.audit([database]))
    assert plan["delete_events"] == 1
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE movement_events SET pinned=1 WHERE id='event-1'")

    result = event_audit.apply_cleanup(plan, data)

    assert result["success"] is False
    assert result["deleted_events"] == 0
    assert any(row["stage"] == "event_reclassified" for row in result["failures"])
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT pinned FROM movement_events WHERE id='event-1'").fetchone()[0] == 1
