from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

from apps.api.app.db import Database
from apps.api.app.mode_lock import RuntimeModeLease
from scripts import event_audit


def _plan_for(database: Path) -> dict:
    rows, info = event_audit.audit_database(database)
    return event_audit.cleanup_plan({"events": rows, "databases": [info]})


def _minimal_plan(database: Path, *, table: str, record_id: str, event_id: str, runtime_mode: str = "UNKNOWN") -> dict:
    target = {
        "database": str(database.resolve()), "table": table, "id": record_id, "event_id": event_id,
        "item_id": "item", "camera_id": "camera", "runtime_mode": runtime_mode,
        "media": {"image": {"url": None}, "clip": {"url": None}},
    }
    return {
        "dry_run": True, "targets": [target], "orphan_media": [], "delete_events": 1,
        "bytes_before": database.stat().st_size, "events_by_item_after_preview": {},
    }


def _modern_mixed_database(data_root: Path) -> tuple[Database, Path]:
    database = data_root / "database" / "objectmemory.sqlite"
    db = Database(database, "REAL")
    shared = data_root / "real-media" / "event-images" / "shared.jpg"
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_bytes(b"shared evidence stays because REAL still references it")
    common = {
        "item_id": "item", "camera_id": "camera", "event_type": "movement",
        "timestamp_start": "2026-09-04T00:00:00+00:00", "timestamp_end": "2026-09-04T00:00:01+00:00",
        "screenshot_path": "/media/event-images/shared.jpg", "evidence_status": "confirmed",
        "final_status": "confirmed_placed",
    }
    db.save("events", {**common, "event_id": "demo-event", "runtime_mode": "DEMO", "source_type": "video_file", "is_simulated": True}, "demo-row")
    db.save("events", {**common, "event_id": "real-event", "runtime_mode": "REAL", "source_type": "opencv_camera", "is_simulated": False, "pinned": True}, "real-row")
    db.save("event_media", {"event_id": "demo-event", "runtime_mode": "DEMO", "path": "/media/event-images/shared.jpg", "kind": "image", "status": "active"}, "demo-media")
    db.save("event_media", {"event_id": "real-event", "runtime_mode": "REAL", "path": "/media/event-images/shared.jpg", "kind": "image", "status": "active"}, "real-media")
    # Deliberately dirty cross-mode row with the same event_id.  Cleanup must
    # not treat event_id alone as authority for UPDATE/DELETE.
    db.save("event_media", {"event_id": "demo-event", "runtime_mode": "REAL", "path": "/media/event-images/shared.jpg", "kind": "thumbnail", "status": "active"}, "real-shadow-media")
    db.save("tracks", {"item_id": "item", "camera_id": "camera", "runtime_mode": "DEMO", "source_type": "video_file", "is_simulated": True}, "demo-track")
    db.save("tracks", {"item_id": "item", "camera_id": "camera", "runtime_mode": "REAL", "source_type": "opencv_camera", "is_simulated": False}, "real-track")
    return db, shared


def test_dry_run_remains_read_only(tmp_path: Path):
    db, shared = _modern_mixed_database(tmp_path)
    before_database = db.path.read_bytes()
    before_media = shared.read_bytes()

    plan = _plan_for(db.path)

    assert plan["dry_run"] is True
    assert any(row["event_id"] == "demo-event" for row in plan["targets"])
    assert db.path.read_bytes() == before_database
    assert shared.read_bytes() == before_media
    assert not (tmp_path / "backups").exists()


def test_apply_scopes_every_delete_to_target_mode_and_keeps_user_backups(tmp_path: Path):
    db, shared = _modern_mixed_database(tmp_path)
    plan = _plan_for(db.path)
    # A cross-mode current-state row with the same evidence id must neither
    # protect the DEMO event nor be deleted with it.
    db.save("item_current_state", {"item_id": "item", "evidence_event_id": "demo-event", "runtime_mode": "REAL", "is_simulated": False}, "real-state")
    backup_dir = tmp_path / "backups"; backup_dir.mkdir()
    user_backup = backup_dir / "pre-audit-family.sqlite"; user_backup.write_bytes(b"user backup")
    user_suffix = backup_dir / "pre-audit-20260904-223046.sqlite.notes"; user_suffix.write_bytes(b"user notes")
    automatic = backup_dir / "pre-audit-20260904-223046.sqlite"; automatic.write_bytes(b"old automatic backup")
    automatic_sidecar = backup_dir / "pre-audit-20260904-223046.sqlite-wal"; automatic_sidecar.write_bytes(b"old wal")

    result = event_audit.apply_cleanup(plan, tmp_path)

    assert result["success"] is True and result["apply_status"] == "succeeded"
    assert result["deleted_events"] == 1
    assert db.get("events", "demo-row", unscoped=True) is None
    assert db.get("events", "real-row", unscoped=True) is not None
    assert db.get("event_media", "demo-media", unscoped=True) is None
    assert db.get("event_media", "real-media", unscoped=True) is not None
    assert db.get("event_media", "real-shadow-media", unscoped=True) is not None
    assert db.get("item_current_state", "real-state", unscoped=True) is not None
    assert db.get("tracks", "demo-track", unscoped=True) is None
    assert db.get("tracks", "real-track", unscoped=True) is not None
    assert shared.is_file()
    assert user_backup.read_bytes() == b"user backup"
    assert user_suffix.read_bytes() == b"user notes"
    assert not automatic.exists() and not automatic_sidecar.exists()
    latest = backup_dir / "pre-audit-latest.sqlite"
    assert latest.is_file()
    with sqlite3.connect(latest) as connection:
        assert connection.execute("SELECT COUNT(*) FROM movement_events WHERE id='demo-row'").fetchone()[0] == 1


def test_apply_reaudit_preserves_new_current_state_evidence(tmp_path: Path):
    db, _shared = _modern_mixed_database(tmp_path)
    plan = _plan_for(db.path)
    db.save(
        "item_current_state",
        {"item_id": "item", "evidence_event_id": "demo-event", "runtime_mode": "DEMO", "is_simulated": True},
        "demo-state",
    )

    result = event_audit.apply_cleanup(plan, tmp_path)

    assert result["success"] is False and result["deleted_events"] == 0
    assert any(entry["stage"] == "event_reclassified" for entry in result["failures"])
    assert db.get("events", "demo-row", unscoped=True) is not None
    assert db.get("item_current_state", "demo-state", unscoped=True) is not None


def test_mixed_database_cleanup_holds_database_and_target_mode_leases(tmp_path: Path):
    db, _shared = _modern_mixed_database(tmp_path)
    plan = _plan_for(db.path)

    with RuntimeModeLease(tmp_path, "DEMO"):
        result = event_audit.apply_cleanup(plan, tmp_path)

    assert result["success"] is False and result["deleted_events"] == 0
    assert result["failures"][0]["stage"] == "runtime_mode_lease"
    assert db.get("events", "demo-row", unscoped=True) is not None
    assert not (tmp_path / "backups").exists()


def test_only_exact_legacy_database_may_delete_rows_without_runtime_mode(tmp_path: Path):
    database_dir = tmp_path / "database"; database_dir.mkdir()
    legacy = database_dir / "object_memory.sqlite3"
    with sqlite3.connect(legacy) as connection:
        connection.execute("CREATE TABLE events(id TEXT PRIMARY KEY,event_id TEXT,item_id TEXT,camera_id TEXT)")
        connection.execute("INSERT INTO events VALUES('legacy-row','legacy-event','item','camera')")
    result = event_audit.apply_cleanup(_minimal_plan(legacy, table="events", record_id="legacy-row", event_id="legacy-event"), tmp_path)
    assert result["success"] is True and result["deleted_events"] == 1
    with sqlite3.connect(legacy) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0

    arbitrary_root = tmp_path / "arbitrary"; (arbitrary_root / "database").mkdir(parents=True)
    arbitrary = arbitrary_root / "database" / "user.sqlite"
    with sqlite3.connect(arbitrary) as connection:
        connection.execute("CREATE TABLE events(id TEXT PRIMARY KEY,event_id TEXT,item_id TEXT,camera_id TEXT)")
        connection.execute("INSERT INTO events VALUES('user-row','user-event','item','camera')")
    refused = event_audit.apply_cleanup(_minimal_plan(arbitrary, table="events", record_id="user-row", event_id="user-event"), arbitrary_root)
    assert refused["success"] is False and refused["deleted_events"] == 0
    assert any(entry["stage"] == "database_apply" for entry in refused["failures"])
    with sqlite3.connect(arbitrary) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_backup_creation_failure_is_reported_before_any_delete(tmp_path: Path, monkeypatch):
    db, _shared = _modern_mixed_database(tmp_path)
    plan = _plan_for(db.path)

    def fail_replace(_source, _destination):
        raise OSError("backup destination locked")

    monkeypatch.setattr(event_audit.os, "replace", fail_replace)
    result = event_audit.apply_cleanup(plan, tmp_path)

    assert result["success"] is False and result["backup_status"] == "failed"
    assert result["deleted_events"] == 0
    assert any(entry["stage"] == "backup_create" for entry in result["failures"])
    assert db.get("events", "demo-row", unscoped=True) is not None


def test_media_delete_failure_keeps_event_and_returns_failed_apply(tmp_path: Path, monkeypatch):
    database = tmp_path / "database" / "objectmemory-demo.sqlite"
    db = Database(database, "DEMO")
    media = tmp_path / "demo-media" / "event-images" / "locked.jpg"
    media.parent.mkdir(parents=True); media.write_bytes(b"locked")
    db.save("events", {"event_id": "locked-event", "item_id": "item", "camera_id": "camera", "runtime_mode": "DEMO", "source_type": "video_file", "is_simulated": True, "screenshot_path": "/media/event-images/locked.jpg"}, "locked-row")
    db.save("event_media", {"event_id": "locked-event", "runtime_mode": "DEMO", "path": "/media/event-images/locked.jpg", "kind": "image", "status": "active"}, "locked-media")
    plan = _plan_for(database)
    original_unlink = Path.unlink

    def fail_media(path: Path, *args, **kwargs):
        if path.resolve() == media.resolve():
            raise OSError("media is locked")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_media)
    result = event_audit.apply_cleanup(plan, tmp_path)

    assert result["success"] is False and result["apply_complete"] is False
    assert result["failed_file_deletes"] == 1 and result["deleted_events"] == 0
    assert any(entry["stage"] == "event_media_delete" for entry in result["failures"])
    assert db.get("events", "locked-row", unscoped=True) is not None
    assert media.is_file()


def test_old_automatic_backup_delete_failure_cannot_report_success(tmp_path: Path, monkeypatch):
    database_dir = tmp_path / "database"; database_dir.mkdir()
    legacy = database_dir / "object_memory.sqlite3"
    with sqlite3.connect(legacy) as connection:
        connection.execute("CREATE TABLE events(id TEXT PRIMARY KEY,event_id TEXT,item_id TEXT,camera_id TEXT)")
        connection.execute("INSERT INTO events VALUES('legacy-row','legacy-event','item','camera')")
    backup_dir = tmp_path / "backups"; backup_dir.mkdir()
    old = backup_dir / "pre-audit-20260904T223046Z.sqlite"; old.write_bytes(b"old")
    original_unlink = Path.unlink

    def fail_old(path: Path, *args, **kwargs):
        if path.resolve() == old.resolve():
            raise OSError("old backup is locked")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_old)
    result = event_audit.apply_cleanup(_minimal_plan(legacy, table="events", record_id="legacy-row", event_id="legacy-event"), tmp_path)

    assert result["deleted_events"] == 1
    assert result["success"] is False and result["apply_status"] == "partial_failure"
    assert result["failed_backup_deletes"] == 1
    assert any(entry["stage"] == "old_backup_delete" for entry in result["failures"])
    assert old.is_file()


def test_automatic_backup_name_match_is_strict():
    assert event_audit.automatic_backup(Path("pre-audit-20260904-223046.sqlite"))
    assert event_audit.automatic_backup(Path("pre-audit-20260904T223046Z-acde1234.sqlite-wal"))
    for name in (
        "pre-audit-latest.sqlite", "pre-audit-family.sqlite", "pre-audit-20260904-223046.sqlite.notes",
        "pre-audit-2026-09-04.sqlite", "my-pre-audit-20260904-223046.sqlite",
    ):
        assert not event_audit.automatic_backup(Path(name))


def test_cli_writes_failure_result_and_exits_nonzero(tmp_path: Path):
    database_dir = tmp_path / "database"; database_dir.mkdir()
    database = database_dir / "user.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE events(id TEXT PRIMARY KEY,event_id TEXT,item_id TEXT,camera_id TEXT,source_type TEXT,screenshot_path TEXT,clip_path TEXT)")
        connection.execute("INSERT INTO events VALUES('demo-row','demo-event','item','camera','video_file',NULL,NULL)")
    result_path = tmp_path / "result.json"
    completed = subprocess.run(
        [
            sys.executable, str(Path(event_audit.__file__).resolve()), "--database", str(database), "--data-root", str(tmp_path),
            "--cleanup", "--apply", "--json", str(tmp_path / "audit.json"), "--csv", str(tmp_path / "audit.csv"),
            "--result", str(result_path),
        ],
        cwd=Path(event_audit.__file__).resolve().parents[1], capture_output=True, text=True, check=False,
    )
    result = __import__("json").loads(result_path.read_text(encoding="utf-8"))
    assert completed.returncode == 1
    assert result["success"] is False and result["apply_status"] == "failed"
    assert any(entry["stage"] == "database_apply" for entry in result["failures"])
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
