#!/usr/bin/env python3
"""Safely remove confirmed superseded audit artifacts and rebuildable caches.

The command is a dry-run unless ``--apply`` is supplied. It never scans the
user reference-image, model, database, backup or firmware source directories;
the only backup-directory targets are two exact transient SQLite sidecar names.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import json
import os
import re
import stat
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TEMPORARY = (ROOT / "data" / "temporary").resolve()
RUNTIME_NAME = re.compile(r"^runtime-stability-\d{8}-\d{6}-[0-9a-f]{8}$")

SUPERSEDED_FILES = (
    "data/verification/api-final.xml",
    "data/verification/bootstrap-script.log",
    "data/verification/build-script.log",
    "data/verification/dev-launcher.json",
    "data/verification/device-e2e.json",
    "data/verification/frontend-audit.json",
    "data/verification/frontend-tests.json",
    "data/verification/lan-session.json",
    "data/verification/no-hardware-demo.json",
    "data/verification/no-hardware-ui.json",
    "data/verification/running-demo.json",
    "data/verification/runtime-config-smoke.json",
    "data/verification/test-script.log",
    "data/verification/validation-summary.json",
    "data/verification/virtual-device-tests.xml",
    "data/verification/webcam-api.json",
    "data/verification/screenshots/browser-camera-probe.png",
    "data/verification/screenshots/camera-diagnostics.png",
    "data/verification/screenshots/dashboard.png",
    "data/verification/screenshots/device-center.png",
    "data/verification/screenshots/device-truth.png",
    "data/verification/screenshots/no-hardware-dashboard.png",
    "data/verification/screenshots/no-hardware-device.png",
    "data/verification/screenshots/search-result.png",
)

EXACT_DUPLICATES = (
    "data/diagnostics/camera-ui-reproduction.json",
    "data/diagnostics/camera-ui-reproduction.png",
)

SUPERSEDED_DIAGNOSTICS = (
    "data/diagnostics/virtual-device-acceptance.json",
    "data/diagnostics/virtual-device-backend.log",
    "audit/runtime-stability-quick-smoke.json",
)

REBUILDABLE_FILES = (
    "apps/web/tsconfig.app.tsbuildinfo",
    "apps/web/tsconfig.node.tsbuildinfo",
)

TRANSIENT_SQLITE_SIDECARS = (
    "data/backups/pre-audit-latest.sqlite-shm",
    "data/backups/pre-audit-latest.sqlite-wal",
    "data/database/objectmemory.sqlite-shm",
    "data/database/objectmemory.sqlite-wal",
)

CANONICAL_DATABASE_MODES = {
    "objectmemory.sqlite": "REAL",
    "objectmemory-demo.sqlite": "DEMO",
    "objectmemory-test.sqlite": "TEST",
}


def normal_path(path: Path) -> Path:
    """Inspect lexical ancestors BEFORE resolve can hide a link/junction."""
    path = Path(path)
    if '..' in path.parts:
        raise RuntimeError(f'refusing parent traversal: {path}')
    absolute = path.absolute()
    for entry in (*reversed(absolute.parents), absolute):
        try:
            metadata = entry.lstat()
        except FileNotFoundError:
            continue  # A planned, already absent regular path is harmless.
        if (stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, 'st_file_attributes', 0) & 0x400):
            raise RuntimeError(f'refusing symlink/reparse path: {entry}')
    return absolute


def confined(path: Path, parent: Path | None = None) -> Path:
    resolved = normal_path(path).resolve()
    boundary = normal_path(ROOT if parent is None else parent).resolve()
    if resolved == boundary or not resolved.is_relative_to(boundary):
        raise RuntimeError(f"refusing unconfined path: {resolved}")
    return resolved


def deletion_path(path: Path, category: str) -> Path:
    path = confined(path)
    relative = path.relative_to(normal_path(ROOT).resolve())
    # Sidecars have a separate exclusive-database/mode lease below. Never
    # permit a caller to relabel the database or a reference image as a cache.
    if category == 'transient_sqlite_sidecar':
        if relative.as_posix() not in TRANSIENT_SQLITE_SIDECARS:
            raise RuntimeError(f'refusing unexpected SQLite sidecar: {path}')
        return path
    protected = ('data/registered-items', 'data/real-media', 'data/demo-media',
                 'data/models', 'data/database', 'data/backups', 'firmware')
    if (any(relative == Path(prefix) or relative.is_relative_to(Path(prefix)) for prefix in protected)
            or any(part == 'node_modules' or part.startswith('.venv') for part in relative.parts)):
        raise RuntimeError(f'refusing protected user/runtime path: {path}')
    return path


def safe_tree(directory: Path) -> list[Path]:
    """One-level traversal only; validate every entry before inspecting it."""
    directory = confined(directory)
    if not directory.is_dir():
        return []
    entries: list[Path] = []
    pending = [directory]
    while pending:
        parent = confined(pending.pop())
        for raw in parent.iterdir():
            child = confined(raw, directory)
            entries.append(child)
            if child.is_dir():
                pending.append(child)
    return entries


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) calls TerminateProcess on Windows. Inspect only an
        # explicitly read-only process handle; unknown/access-denied means live.
        import ctypes
        from ctypes import wintypes

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            open_process.restype = wintypes.HANDLE
            wait = kernel32.WaitForSingleObject
            wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            wait.restype = wintypes.DWORD
            close = kernel32.CloseHandle
            close.argtypes = (wintypes.HANDLE,)
            close.restype = wintypes.BOOL
            if pid > 0xFFFFFFFF:
                return True  # Do not truncate an unknown identifier to a DWORD.
            handle = open_process(0x00100000 | 0x1000, False, pid)
            if not handle:
                # With a valid positive DWORD PID and fixed rights, 87 means
                # that process no longer exists. Every other error is unknown.
                return ctypes.get_last_error() != 87
            try:
                # Only WAIT_OBJECT_0 proves termination. WAIT_TIMEOUT,
                # WAIT_FAILED and unexpected values must block cleanup.
                return wait(handle, 0) != 0
            finally:
                close(handle)
        except (OSError, AttributeError, ValueError):
            return True
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


@contextmanager
def runtime_mode_guard(data_root: Path, mode: str):
    """Hold the same OS-owned lease as the backend for the whole deletion."""
    lock_path = confined(data_root / ".runtime-locks" / f"{mode.lower()}.lock", data_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    handle = lock_path.open("r+b", buffering=0)
    locked = False
    try:
        if lock_path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise RuntimeError(
                f"refusing SQLite sidecar cleanup: {mode} backend/runtime is active"
            ) from exc
        yield
    finally:
        if locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


@contextmanager
def exclusive_database_guard(database: Path):
    """Prevent any SQLite process from remaining/opening while sidecars vanish."""
    database = confined(database)
    if not database.is_file():
        raise RuntimeError(f"refusing SQLite sidecar cleanup: base database is missing: {database}")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = create_file(str(database), 0x80000000, 0, None, 3, 0x80, None)
        invalid = wintypes.HANDLE(-1).value
        if handle == invalid:
            error = ctypes.get_last_error()
            raise RuntimeError(
                f"refusing SQLite sidecar cleanup: database is open or unavailable: {database} (winerror={error})"
            )
        try:
            yield
        finally:
            close_handle(handle)
    else:
        import fcntl

        descriptor = os.open(database, os.O_RDWR)
        locked = False
        try:
            # SQLite's PENDING/RESERVED/SHARED lock bytes.  An exclusive range
            # lock conflicts with active readers and writers using the DB file.
            fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB, 512, 0x40000000, os.SEEK_SET)
            locked = True
            yield
        except OSError as exc:
            raise RuntimeError(
                f"refusing SQLite sidecar cleanup: database is active: {database}"
            ) from exc
        finally:
            if locked:
                try:
                    fcntl.lockf(descriptor, fcntl.LOCK_UN, 512, 0x40000000, os.SEEK_SET)
                except OSError:
                    pass
            os.close(descriptor)


def sqlite_sidecar_databases(plan: list[dict[str, Any]]) -> list[Path]:
    databases: set[Path] = set()
    for row in plan:
        if row.get("category") != "transient_sqlite_sidecar":
            continue
        value = str(row.get("path") or "")
        lowered = value.lower()
        suffix = "-wal" if lowered.endswith("-wal") else "-shm" if lowered.endswith("-shm") else None
        if suffix:
            databases.add(confined(Path(value[: -len(suffix)])))
    return sorted(databases, key=lambda value: str(value).lower())


def has_nonempty_wal(database: Path) -> bool:
    # An exited/crashed writer can leave committed transactions ONLY in WAL.
    # Inactivity or an exclusive base-file handle never proves it is garbage.
    wal = confined(Path(str(database) + '-wal'))
    return wal.is_file() and wal.stat().st_size > 0


def preserved_wal_databases() -> list[Path]:
    databases = {confined(ROOT / relative[:-4]) for relative in TRANSIENT_SQLITE_SIDECARS
                 if relative.endswith('-wal')}
    return sorted((path for path in databases if has_nonempty_wal(path)), key=str)


@contextmanager
def sqlite_sidecar_guards(plan: list[dict[str, Any]]):
    databases = sqlite_sidecar_databases(plan)
    with ExitStack() as stack:
        leases: set[tuple[str, str]] = set()
        for database in databases:
            mode = CANONICAL_DATABASE_MODES.get(database.name.lower())
            if mode:
                data_root = database.parent.parent.resolve()
                key = (str(data_root).lower(), mode)
                if key not in leases:
                    stack.enter_context(runtime_mode_guard(data_root, mode))
                    leases.add(key)
        for database in databases:
            stack.enter_context(exclusive_database_guard(database))
            if has_nonempty_wal(database):
                raise RuntimeError(f'refusing nonempty WAL cleanup; committed data may require recovery: {database}')
        yield


def files_in_tree(directory: Path) -> list[Path]:
    directory = confined(directory)
    return [path for path in safe_tree(directory) if path.is_file()]


def cache_directories() -> list[Path]:
    directories: set[Path] = set()
    for relative in ("apps/api", "scripts", "services", "tools"):
        root = confined(ROOT / relative)
        if root.is_dir():
            directories.update(path for path in safe_tree(root) if path.name == '__pycache__' and path.is_dir())
    for relative in ("apps/__pycache__", "tools/__pycache__", ".pytest_cache", "apps/web/test-results"):
        path = confined(ROOT / relative)
        if path.is_dir():
            directories.add(confined(path))
    return sorted(directories, key=lambda value: str(value).lower())


def runtime_directory(name: str) -> Path:
    if not RUNTIME_NAME.fullmatch(name):
        raise RuntimeError(f"invalid runtime directory name: {name}")
    path = confined(TEMPORARY / name, TEMPORARY)
    if path.parent != TEMPORARY:
        raise RuntimeError(f"runtime directory is not a direct child of temporary: {path}")
    pid_file = confined(path / "backend.pid", path)
    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = 0
        if process_alive(pid):
            raise RuntimeError(f"runtime directory still belongs to live PID {pid}: {path}")
    return path


def build_plan(runtime_names: list[str]) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []

    def add_file(path: Path, category: str) -> None:
        path = deletion_path(path, category)
        if path.is_file():
            plan.append({"category": category, "path": str(path), "bytes": path.stat().st_size})

    for relative in SUPERSEDED_FILES:
        add_file(ROOT / relative, "superseded_verification")
    for relative in EXACT_DUPLICATES:
        add_file(ROOT / relative, "exact_duplicate_diagnostic")
    for relative in SUPERSEDED_DIAGNOSTICS:
        add_file(ROOT / relative, "superseded_diagnostic")
    for relative in REBUILDABLE_FILES:
        add_file(ROOT / relative, "rebuildable_cache")
    preserved_databases = set(preserved_wal_databases())
    for relative in TRANSIENT_SQLITE_SIDECARS:
        # Retain both WAL and its SHM companion. A stale plan is checked again
        # under the exclusive guard, so zero bytes at scan time is not enough.
        if confined(ROOT / relative[:-4]) not in preserved_databases:
            add_file(ROOT / relative, "transient_sqlite_sidecar")
    for directory in cache_directories():
        for path in files_in_tree(directory):
            add_file(path, 'rebuildable_cache')
    for name in runtime_names:
        directory = runtime_directory(name)
        if directory.is_dir():
            for path in files_in_tree(directory):
                add_file(path, 'interrupted_runtime_temp')

    unique: dict[str, dict[str, Any]] = {}
    for row in plan:
        unique[row["path"].lower()] = row
    return sorted(unique.values(), key=lambda row: row["path"].lower())


def prune_empty(directory: Path) -> None:
    directory = deletion_path(directory, 'rebuildable_cache')
    if not directory.is_dir():
        return
    for child in sorted((path for path in safe_tree(directory) if path.is_dir()), key=lambda value: len(value.parts), reverse=True):
        deletion_path(child, 'rebuildable_cache')
        try:
            child.rmdir()
        except OSError:
            pass
    try:
        deletion_path(directory, 'rebuildable_cache')
        directory.rmdir()
    except OSError:
        pass


def apply_plan(plan: list[dict[str, Any]], runtime_names: list[str]) -> tuple[int, int, list[dict[str, str]]]:
    """Apply one plan atomically with respect to backend mode leases.

    If any database or runtime guard cannot be acquired, no planned file is
    removed.  The guards remain held until every SQLite sidecar deletion has
    completed, closing the check/delete race with a newly starting backend.
    """
    failures: list[dict[str, str]] = []
    deleted = 0
    deleted_bytes = 0
    try:
        # Validate the complete plan before any deletion, and repeat each path
        # check at unlink time in case a directory changed after the scan.
        for row in plan:
            deletion_path(Path(row['path']), str(row.get('category') or ''))
        with sqlite_sidecar_guards(plan):
            for row in plan:
                path = deletion_path(Path(row["path"]), str(row.get('category') or ''))
                try:
                    path.unlink(missing_ok=True)
                    deleted += 1
                    deleted_bytes += int(row["bytes"])
                except OSError as exc:
                    failures.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
    except (OSError, RuntimeError) as exc:
        failures.append({"path": "sqlite_sidecar_guard", "error": f"{type(exc).__name__}: {exc}"})
        # A path can change after the complete preflight. Keep an honest count
        # if earlier files were already removed before a later safety refusal.
        return deleted, deleted_bytes, failures
    for directory in cache_directories():
        prune_empty(directory)
    for name in runtime_names:
        directory = runtime_directory(name)
        if directory.is_dir():
            prune_empty(directory)
    prune_empty(ROOT / "data/verification/screenshots")
    return deleted, deleted_bytes, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--runtime-dir", action="append", default=[])
    args = parser.parse_args()
    plan = build_plan(args.runtime_dir)
    deleted = 0
    deleted_bytes = 0
    failures: list[dict[str, str]] = []
    if args.apply:
        deleted, deleted_bytes, failures = apply_plan(plan, args.runtime_dir)
    by_category: dict[str, dict[str, int]] = {}
    for row in plan:
        bucket = by_category.setdefault(row["category"], {"files": 0, "bytes": 0})
        bucket["files"] += 1
        bucket["bytes"] += int(row["bytes"])
    result = {
        "dry_run": not args.apply,
        "success": not failures,
        "planned_files": len(plan),
        "planned_bytes": sum(int(row["bytes"]) for row in plan),
        "deleted_files": deleted,
        "deleted_bytes": deleted_bytes,
        "categories": by_category,
        "runtime_directories": args.runtime_dir,
        "failures": failures,
        "preserved_nonempty_wal_databases": [str(path) for path in preserved_wal_databases()],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
