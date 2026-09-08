"""Create a reproducible, read-only ObjectMemory repository snapshot.

The output intentionally contains structure and counts, never row contents or
environment values.  It is safe to attach to an audit report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_SUFFIXES = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
FIRMWARE_SUFFIXES = {".bin", ".elf", ".map", ".hex"}
SOURCE_EXCLUDES = {"node_modules", ".venv", ".venv-firmware", "__pycache__", ".pytest_cache", "dist", ".pio"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT.parent), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return result.stdout.strip()


def files_under(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file()]


def size_record(paths: list[Path]) -> dict[str, int]:
    sizes = []
    for path in paths:
        try:
            sizes.append(path.stat().st_size)
        except OSError:
            continue
    return {"files": len(sizes), "bytes": sum(sizes)}


def _fingerprint(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_size, stat.st_mtime_ns, getattr(stat, "st_ino", 0)


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _copy_matches(source: Path, copied: Path) -> bool:
    return (
        source.is_file()
        and copied.is_file()
        and source.stat().st_size == copied.stat().st_size
        and _digest(source) == _digest(copied)
    )


def readonly_snapshot(path: Path, *, max_attempts: int = 5) -> tuple[sqlite3.Connection, dict[str, Any]]:
    """Create a consistent in-memory snapshot without creating source sidecars."""
    resolved = path.resolve()
    wal = Path(str(resolved) + "-wal")
    metadata: dict[str, Any] = {
        "strategy": None,
        "wal_present": False,
        "wal_bytes": 0,
        "source_sidecars_created": False,
        "copy_attempts": 0,
    }
    memory = None
    last_error = "source changed during snapshot capture"
    for attempt in range(1, max_attempts + 1):
        metadata["copy_attempts"] = attempt
        database_before = _fingerprint(resolved)
        wal_before = _fingerprint(wal)
        if database_before is None:
            raise FileNotFoundError(resolved)
        if wal_before is None or wal_before[0] == 0:
            source = sqlite3.connect(resolved.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=5)
            candidate = sqlite3.connect(":memory:")
            try:
                source.backup(candidate)
            except BaseException:
                candidate.close()
                raise
            finally:
                source.close()
            database_after = _fingerprint(resolved)
            wal_after = _fingerprint(wal)
            if database_before == database_after and (wal_after is None or wal_after[0] == 0):
                memory = candidate
                metadata.update(
                    {"strategy": "immutable_base", "wal_present": wal_after is not None, "wal_bytes": 0}
                )
                break
            candidate.close()
            last_error = "database changed or a non-empty WAL appeared during immutable capture"
            time.sleep(0.025)
            continue
        try:
            with tempfile.TemporaryDirectory(prefix="objectmemory-snapshot-") as folder:
                copied = Path(folder) / resolved.name
                copied_wal = Path(str(copied) + "-wal")
                shutil.copyfile(resolved, copied)
                shutil.copyfile(wal, copied_wal)
                if not _copy_matches(resolved, copied) or not _copy_matches(wal, copied_wal):
                    last_error = "source database or WAL changed during snapshot copy"
                    time.sleep(0.025)
                    continue
                after = (_fingerprint(resolved), _fingerprint(wal))
                if (database_before, wal_before) != after:
                    last_error = "source database or WAL changed during snapshot verification"
                    time.sleep(0.025)
                    continue
                source = sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True, timeout=5)
                try:
                    if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise sqlite3.DatabaseError("copied WAL snapshot failed integrity_check")
                    candidate = sqlite3.connect(":memory:")
                    try:
                        source.backup(candidate)
                    except BaseException:
                        candidate.close()
                        raise
                    memory = candidate
                finally:
                    source.close()
                metadata.update(
                    {"strategy": "stable_wal_copy", "wal_present": True, "wal_bytes": wal_before[0]}
                )
                break
        except FileNotFoundError:
            last_error = "WAL disappeared during snapshot copy"
            time.sleep(0.025)
            continue
    if memory is None:
        raise RuntimeError(f"unable to obtain a consistent SQLite snapshot: {last_error}")
    memory.row_factory = sqlite3.Row
    memory.execute("PRAGMA query_only=ON")
    if memory.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        memory.close()
        raise sqlite3.DatabaseError("in-memory audit snapshot failed integrity_check")
    return memory, metadata


def db_snapshot(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": path.relative_to(ROOT).as_posix(),
        "bytes": path.stat().st_size,
    }
    try:
        connection, snapshot = readonly_snapshot(path)
        result["snapshot"] = snapshot
        table_rows = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        result["integrity_check"] = connection.execute("PRAGMA integrity_check").fetchone()[0]
        result["tables"] = {
            row["name"]: {
                "count": connection.execute(
                    f'SELECT COUNT(*) FROM "{row["name"].replace(chr(34), chr(34) * 2)}"'
                ).fetchone()[0],
                "ddl": row["sql"],
                "columns": [dict(column) for column in connection.execute(f'PRAGMA table_info("{row["name"]}")')],
            }
            for row in table_rows
        }
        connection.close()
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def build_snapshot(phase: str) -> dict[str, Any]:
    all_files = files_under(ROOT)
    data_files = files_under(ROOT / "data")
    source_files = [
        path for path in all_files
        if not any(part in SOURCE_EXCLUDES for part in path.relative_to(ROOT).parts)
        and "data" not in path.relative_to(ROOT).parts[:1]
    ]
    images = [path for path in data_files if path.suffix.lower() in IMAGE_SUFFIXES]
    videos = [path for path in data_files if path.suffix.lower() in VIDEO_SUFFIXES]
    logs = [path for path in all_files if path.suffix.lower() == ".log"]
    firmware_roots = (
        ROOT / "firmware" / "esp32cam" / "build",
        ROOT / "artifacts" / "firmware",
        ROOT / "data" / "firmware-builds",
    )
    firmware_files = [
        path for folder in firmware_roots for path in files_under(folder)
        if path.suffix.lower() in FIRMWARE_SUFFIXES
    ]
    primary_databases = [
        path for path in sorted((ROOT / "data" / "database").glob("*"))
        if path.is_file()
        and path.name.lower().endswith((".sqlite", ".sqlite3", ".db"))
        and not path.name.lower().endswith(("-wal", "-shm"))
    ]
    return {
        "schema_version": 1,
        "phase": phase,
        "captured_at": utc_now(),
        "workspace": str(ROOT),
        "git": {
            "branch": run_git("branch", "--show-current"),
            "head": run_git("rev-parse", "HEAD"),
            "status": run_git("status", "--short", "--branch"),
            "diff_stat": run_git("diff", "--stat"),
            "checkpoint": "f9e1205 chore: checkpoint ObjectMemory before authenticity audit",
            "pre_checkpoint_state": "unborn master; object-memory was untracked; unrelated sibling directories were and remain untracked",
        },
        "storage": {
            "project_total": size_record(all_files),
            "project_source_excluding_dependencies_and_data": size_record(source_files),
            "data_total": size_record(data_files),
            "screenshots_and_images": size_record(images),
            "recordings_and_videos": size_record(videos),
            "logs": size_record(logs),
            "firmware_build_artifacts": size_record(firmware_files),
        },
        "databases": [db_snapshot(path) for path in primary_databases],
        "limitations": [
            "Counts describe files present at capture time; they do not imply authenticity.",
            "The pre-audit repository had no prior commit, so Git had no tracked diff for the untracked ObjectMemory tree.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("before", "after"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(build_snapshot(args.phase), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
