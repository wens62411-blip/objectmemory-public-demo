"""Unified, path-confined retention and media deletion service."""
from __future__ import annotations

import os
import hashlib
import json
import sqlite3
import stat
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .db import Database, now
from .mode_lock import RuntimeModeLease


POLICIES = {
    "MINIMAL": {"events_per_item": 2, "media_days": None},
    "BALANCED": {"events_per_item": 10, "media_days": 7},
    "FORENSIC": {"events_per_item": 1000, "media_days": None},
}


def evidence_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def acceptance_evidence_snapshot(
    db: Database, media_root: Path, event: dict[str, Any] | None, run: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Recheck live evidence before scoring or issuing a policy-deletion receipt.

    This is an integrity check of server-owned records, not a hardware identity
    attestation. Neither a stored PASSED flag nor a previous file hash suffices.
    """
    if not event:
        return None, "event_missing"
    session_id = str(event.get("source_session_id") or "")
    if (
        run.get("status") != "PASSED" or run.get("outcome") != "PASSED"
        or not run.get("user_executed") or run.get("trial_kind") != "movement"
        or not _is_confirmed_movement(event)
        or event.get('final_status')!='confirmed_placed'
        or event.get("event_id") != run.get("event_id")
        or event.get("validation_run_id") != run.get("validation_run_id")
        or event.get("runtime_mode") != "REAL" or event.get("source_type") != "opencv_camera"
        or event.get("is_simulated") is not False or event.get("detection_mode") != "aruco_screen_validation"
        or event.get("item_id") != run.get("item_id") or event.get("camera_id") != run.get("camera_id")
        or not session_id or session_id != run.get("source_session_id")
        or event.get("from_zone_id") != run.get("origin_zone_id")
        or event.get("to_zone_id") != run.get("destination_zone_id")
    ):
        return None, "event_run_binding_invalid"
    session = db.get("source_sessions", session_id)
    if not session or session.get("runtime_mode") != "REAL" or session.get("source_type") != "opencv_camera" or session.get("is_simulated") is not False or session.get("camera_id") != run.get("camera_id"):
        return None, "source_session_invalid"
    expected = {
        "before": ("image", event.get("before_screenshot"), event.get("before_screenshot_sha256")),
        "after": ("image", event.get("after_screenshot"), event.get("after_screenshot_sha256")),
        "clip": ("clip", event.get("clip_path"), event.get("clip_sha256")),
    }
    rows = db.list("event_media", {"event_id": event["event_id"]}, limit=20)
    by_role = {row.get("role"): row for row in rows}
    if len(rows) != 3 or set(by_role) != set(expected):
        return None, "media_roles_invalid"
    manifest = []
    paths: set[Path] = set()
    root = media_root.resolve()
    for role, (kind, url, expected_sha) in expected.items():
        row = by_role[role]
        if (
            row.get("status") != "active" or row.get("pending_delete_at") or row.get("deleted_at")
            or row.get("kind") != kind or row.get("path") != url
            or row.get("runtime_mode") != "REAL" or row.get("source_session_id") != session_id
            or not isinstance(expected_sha, str) or len(expected_sha) != 64 or row.get("sha256") != expected_sha
            or not isinstance(url, str) or not url.startswith("/media/")
        ):
            return None, f"{role}_registry_invalid"
        relative = Path(url.removeprefix("/media/"))
        expected_folder = "event-clips" if kind == "clip" else "event-images"
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != (expected_folder,):
            return None, f"{role}_path_unsafe"
        try:
            path = (root / relative).resolve(strict=True)
            if not path.is_relative_to(root) or path in paths or not path.is_file():
                return None, f"{role}_path_unsafe"
            with path.open("rb") as stream:
                actual_sha = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual_sha != expected_sha:
                return None, f"{role}_hash_mismatch"
        except (OSError, RuntimeError):
            return None, f"{role}_file_missing"
        paths.add(path)
        manifest.append({"role": role, "kind": kind, "path": url, "sha256": actual_sha})
    return {
        "version": "p0-policy-evidence-v1", "event_id": event["event_id"],
        "validation_run_id": run["validation_run_id"], "suite_id": run.get("suite_id"),
        "item_id": event["item_id"], "camera_id": event["camera_id"],
        "runtime_mode": "REAL", "source_type": "opencv_camera", "is_simulated": False,
        "source_session_id": session_id, "source_frame_start": event.get("source_frame_start"),
        "source_frame_end": event.get("source_frame_end"),
        "source_timestamp_start": event.get("source_timestamp_start"),
        "source_timestamp_end": event.get("source_timestamp_end"), "media": manifest,
    }, None


def _is_confirmed_movement(event: dict[str, Any]) -> bool:
    """A proved position change consumes a slot without claiming placement."""
    return (
        str(event.get("event_type") or "") == "movement"
        and str(event.get("evidence_status") or "") == "confirmed"
        and str(event.get("final_status") or "") in {'confirmed_placed','position_changed'}
    )


def _is_retained_movement(event: dict[str, Any], *, allow_media_expired: bool) -> bool:
    """Keep BALANCED history metadata after its evidence-age expiry."""
    allowed_statuses = {"confirmed", "media_expired"} if allow_media_expired else {"confirmed"}
    return (
        str(event.get("event_type") or "") == "movement"
        and str(event.get("evidence_status") or "") in allowed_statuses
        and str(event.get("final_status") or "") in {'confirmed_placed','position_changed'}
    )


def _is_link_or_reparse(path: Path) -> bool:
    """Inspect the directory entry itself without trusting its target."""
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
        return bool(reparse_flag and attributes & reparse_flag)
    except OSError:
        return False


def _lexical_child(path: Path, root: Path) -> bool:
    """Check the name is below root without following a link/reparse target."""
    try:
        candidate = Path(os.path.abspath(path))
        base = Path(os.path.abspath(root))
        return candidate != base and candidate.is_relative_to(base)
    except (OSError, ValueError):
        return False


def _resolved_child(path: Path, root: Path) -> Path | None:
    """Resolve a regular candidate and reject every escape from root."""
    try:
        base = root.resolve(strict=True)
        candidate = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return candidate if candidate != base and candidate.is_relative_to(base) else None


def _path_label(path: Path, root: Path) -> str:
    try:
        return str(Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root))))
    except (OSError, ValueError):
        return str(path.name)


def _note_skipped(report: dict[str, Any], path: Path, root: Path) -> None:
    report["skipped_unsafe"] = int(report.get("skipped_unsafe", 0)) + 1
    paths = report.setdefault("skipped_paths", [])
    label = _path_label(path, root)
    if len(paths) < 50 and label not in paths:
        paths.append(label)


def _remove_link_only(path: Path) -> bool:
    """Remove only a link/reparse directory entry, never its target tree."""
    try:
        path.unlink(missing_ok=True)
        return True
    except (IsADirectoryError, PermissionError):
        try:
            path.rmdir()
            return True
        except OSError:
            return False
    except OSError:
        return False


def _delete_confined_entry(path: Path, root: Path, report: dict[str, Any]) -> tuple[bool, int, bool]:
    """Delete a regular file or the link itself after a fresh boundary check."""
    if not _lexical_child(path, root):
        _note_skipped(report, path, root)
        return False, 0, False
    if _is_link_or_reparse(path):
        try:
            size = int(path.lstat().st_size)
        except OSError:
            size = 0
        if _remove_link_only(path):
            return True, size, True
        _note_skipped(report, path, root)
        return False, 0, True
    resolved = _resolved_child(path, root)
    if resolved is None or not resolved.is_file():
        _note_skipped(report, path, root)
        return False, 0, False
    try:
        size = int(resolved.stat().st_size)
        # Unlink the checked directory entry. If an attacker replaces it with a
        # link after resolve(), unlink still removes the link, not its target.
        path.unlink()
        return True, size, False
    except OSError:
        _note_skipped(report, path, root)
        return False, 0, False


def _walk_confined_entries(root: Path, report: dict[str, Any]) -> list[Path]:
    """Enumerate a tree without descending through links or reparse points."""
    if _is_link_or_reparse(root):
        _note_skipped(report, root, root.parent)
        return []
    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError):
        _note_skipped(report, root, root.parent)
        return []
    if not resolved_root.is_dir():
        return []
    found: list[Path] = []
    pending = [root]
    while pending:
        folder = pending.pop()
        # Revalidate before every enumeration. A concurrently substituted link
        # is never intentionally traversed; any raced child also fails resolve.
        if _is_link_or_reparse(folder):
            _note_skipped(report, folder, root)
            continue
        if folder != root:
            resolved_folder = _resolved_child(folder, resolved_root)
            if resolved_folder is None or not resolved_folder.is_dir():
                _note_skipped(report, folder, root)
                continue
        try:
            with os.scandir(folder) as scanner:
                children = [Path(entry.path) for entry in scanner]
        except OSError:
            _note_skipped(report, folder, root)
            continue
        for path in children:
            if not _lexical_child(path, root):
                _note_skipped(report, path, root)
                continue
            found.append(path)
            if _is_link_or_reparse(path):
                continue
            resolved = _resolved_child(path, resolved_root)
            if resolved is None:
                _note_skipped(report, path, root)
                continue
            try:
                if resolved.is_dir():
                    pending.append(path)
            except OSError:
                _note_skipped(report, path, root)
    return found


def tree_size(path: Path) -> int:
    if not path.exists() or _is_link_or_reparse(path):
        return 0
    root = path.resolve()
    if root.is_file():
        try:
            return root.stat().st_size
        except OSError:
            return 0
    total = 0
    for item in path.rglob("*"):
        if _is_link_or_reparse(item):
            continue
        resolved = _resolved_child(item, root)
        if resolved is None or not resolved.is_file():
            continue
        try:
            total += resolved.stat().st_size
        except OSError:
            pass
    return total


class RetentionService:
    def __init__(self, db: Database, data_root: Path, media_root: Path, *, max_storage_mb: int = 500, recover_pending_on_start: bool = True):
        self.db = db
        self.data_root = data_root.resolve()
        self.media_root = media_root.resolve()
        self.project_root = Path(__file__).resolve().parents[3]
        # Mode alone is not ownership: isolated REAL/DEMO E2E data roots must
        # neither manage nor count shared production firmware against their cap.
        canonical_data = self.project_root / "data"
        self.manage_project_firmware = (
            db.runtime_mode != "TEST"
            and Path(os.path.abspath(data_root)) == canonical_data
            and self.data_root == canonical_data.resolve()
            and not _is_link_or_reparse(canonical_data)
        )
        self.max_storage_mb = max(50, int(max_storage_mb))
        self.lock = threading.RLock()
        self._trigger_lock = threading.Lock()
        self._cleanup_thread: threading.Thread | None = None
        self.last_report: dict[str, Any] | None = None
        if recover_pending_on_start:
            self.recover_pending()

    def policy(self) -> str:
        row = self.db.get("settings", "main", unscoped=True) or {}
        return str((row.get("value") or {}).get("retention_policy") or "MINIMAL").upper()

    def set_policy(self, value: str) -> str:
        value = str(value).upper()
        if value not in POLICIES:
            raise ValueError("保留策略只能是 MINIMAL、BALANCED 或 FORENSIC。")
        row = self.db.get("settings", "main", unscoped=True) or {}
        settings = {**(row.get("value") or {}), "retention_policy": value}
        self.db.save("settings", {"value": settings}, "main")
        return value

    def _safe_media(self, url: str | None) -> Path | None:
        if not url or not str(url).startswith("/media/"):
            return None
        relative = Path(str(url).removeprefix("/media/"))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] not in {"event-images", "event-clips", "thumbnails"}:
            return None
        path = (self.media_root / relative).resolve()
        return path if path.is_relative_to(self.media_root) else None

    def status(self) -> dict[str, Any]:
        images = self.media_root / "event-images"
        clips = self.media_root / "event-clips"
        media = self.db.list("event_media", limit=100000)
        referenced = {str(row.get("path")) for row in media if row.get("status") != "deleted"}
        referenced.update(self._current_observation_paths())
        orphans = sum(1 for folder in (images, clips) for path in folder.glob("*") if path.is_file() and f"/media/{folder.name}/{path.name}" not in referenced)
        by_item = defaultdict(int)
        pinned = 0
        for event in self.db.list("events", limit=100000):
            by_item[str(event.get("item_id") or "unknown")] += 1
            pinned += int(bool(event.get("pinned")))
        latest = self.db.list("retention_reports", limit=1, order="ended_at")
        return {
            "runtime_mode": self.db.runtime_mode,
            "source_type":"storage_manager","is_simulated":self.db.runtime_mode!="REAL",
            "database_path": str(self.db.path), "database_size": tree_size(self.db.path),
            "screenshot_size": tree_size(images), "clip_size": tree_size(clips),
            "logs_size": tree_size(self.data_root / "logs"), "firmware_artifacts_size": self._firmware_artifact_bytes(),
            "events_by_item": dict(by_item), "pinned_events": pinned, "orphan_files": orphans,
            "retention_policy": self.policy(), "max_storage_mb": self.max_storage_mb,
            "total_managed_bytes": tree_size(self.data_root / "database") + tree_size(self.media_root) + tree_size(self.data_root / "logs") + self._firmware_artifact_bytes(),
            "next_cleanup_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
            "last_cleanup_report": latest[0] if latest else self.last_report,
        }

    @staticmethod
    def _binding_reference(media_rows: list[dict[str, Any]]) -> str | None:
        candidates: set[str] = set()
        for row in media_rows:
            metadata = row.get("metadata") or {}
            if not isinstance(metadata, dict):
                try:
                    metadata = json.loads(metadata)
                except (TypeError, ValueError):
                    metadata = {}
            binding = metadata.get("write_binding") or {}
            value = str(binding.get("manual_reference_event_id") or "").strip() if isinstance(binding, dict) else ""
            if value:
                candidates.add(value)
        return next(iter(candidates)) if len(candidates) == 1 else None

    def _protection_snapshot(
        self,
        events: list[dict[str, Any]] | None = None,
        media: list[dict[str, Any]] | None = None,
    ) -> dict[str, set[str]]:
        """Resolve direct and transitive evidence dependencies.

        IDs in this snapshot are public ``event_id`` values, not SQLite row
        primary keys.  A manual correction is only durable while its original
        movement and shared media registry remain available for re-audit.
        """
        events = events if events is not None else self.db.list("events", limit=100000)
        media = media if media is not None else self.db.list("event_media", limit=100000)
        by_identity: dict[str, dict[str, Any]] = {}
        by_media: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            for value in (event.get("id"), event.get("event_id")):
                if value:
                    by_identity[str(value)] = event
        for row in media:
            if row.get("status") != "deleted":
                by_media[str(row.get("event_id") or "")].append(row)
        current = {
            str(row.get("evidence_event_id"))
            for row in self.db.list("item_current_state", limit=100000)
            if row.get("evidence_event_id")
        }
        pinned = {str(row.get("event_id") or row.get("id")) for row in events if row.get("pinned")}
        manual = {
            str(row.get("event_id") or row.get("id"))
            for row in events
            if row.get("manually_corrected") or row.get("event_type") == "manual_correction"
        }
        dependencies: set[str] = set()
        for state in self.db.list('item_current_state',limit=100000):
            placement=state.get('last_confirmed_placement') or {}
            if (isinstance(placement,dict) and placement.get('event_id') in by_identity
                    and placement.get('event_id')!=state.get('evidence_event_id')):
                dependencies.add(str(placement['event_id']))
        for manual_id in manual:
            reference = by_identity.get(manual_id)
            visited: set[str] = set()
            while reference and (reference.get("manually_corrected") or reference.get("event_type") == "manual_correction"):
                reference_id = str(reference.get("manual_reference_event_id") or "").strip()
                if not reference_id:
                    reference_id = str(self._binding_reference(by_media.get(str(reference.get("event_id") or ""), [])) or "")
                if not reference_id or reference_id in visited:
                    break
                visited.add(reference_id)
                dependencies.add(reference_id)
                reference = by_identity.get(reference_id)
        return {
            "current": current,
            "pinned": pinned,
            "manual": manual,
            "dependencies": dependencies,
            "all": current | pinned | manual | dependencies,
        }

    def _current_observation_paths(self) -> set[str]:
        return {str(observed['screenshot_path'])
                for state in self.db.list('item_current_state',limit=100000)
                if isinstance(observed:=state.get('last_observed'),dict) and observed.get('screenshot_path')}

    def _plan(self) -> dict[str, Any]:
        policy_name = self.policy()
        policy = POLICIES[policy_name]
        events = self.db.list("events", limit=100000, order="timestamp_end")
        all_media = self.db.list("event_media", limit=100000)
        protection = self._protection_snapshot(events, all_media)
        current_ids = protection["current"]
        protected_ids = protection["all"]
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        delete_ids: set[str] = set()
        for event in events:
            # Only complete, evidenced placements consume the two/ten history
            # slots. A newer candidate/rejected/legacy row must never displace
            # a genuine movement. Pinned/current/manual dependencies remain
            # independently protected by ``protected_ids``.
            if not _is_retained_movement(event, allow_media_expired=policy_name != "MINIMAL"):
                if str(event.get("event_id") or event.get("id")) not in protected_ids:
                    delete_ids.add(event["id"])
                continue
            groups[str(event.get("item_id") or "unknown")].append(event)
        keep_n = int(policy["events_per_item"])
        for rows in groups.values():
            rows.sort(key=lambda row: str(row.get("timestamp_end") or row.get("created_at") or ""), reverse=True)
            current_rows = [row for row in rows if not row.get("pinned") and not row.get("manually_corrected") and row.get("event_id") in current_ids]
            slots = max(0, keep_n - len(current_rows))
            eligible = [
                row for row in rows
                if str(row.get("event_id") or row.get("id")) not in protected_ids
            ]
            for index, row in enumerate(eligible):
                if index >= slots:
                    delete_ids.add(row["id"])
        media_delete_ids: set[str] = set()
        observation_paths=self._current_observation_paths()
        media_delete_ids.update(row['id'] for row in all_media
                                if row.get('owner_current_state_id') and row.get('path') not in observation_paths)
        media_days = policy.get("media_days")
        if media_days:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=int(media_days))).isoformat()
            by_event_id = {row.get("event_id"): row for row in events}
            for media in all_media:
                event = by_event_id.get(media.get("event_id"))
                if not event or str(event.get("event_id") or event.get("id")) in protected_ids or event.get("id") in delete_ids:
                    continue
                if str(event.get("timestamp_end") or event.get("created_at") or "") < cutoff:
                    media_delete_ids.add(media["id"])
        deleted_public_ids = {str(row.get("event_id") or row.get("id")) for row in events if row.get("id") in delete_ids}
        referenced = {str(row.get("path")) for row in all_media if row.get("status") != "deleted" and row.get("event_id") not in deleted_public_ids}
        referenced.update(observation_paths)
        for event in events:
            if event.get("id") in delete_ids:
                continue
            referenced.update(
                str(event.get(field))
                for field in ("screenshot_path","before_screenshot","after_screenshot","clip_path")
                if event.get(field)
            )
        orphan_paths: list[str] = []
        orphan_cutoff=datetime.now(timezone.utc).timestamp()-300
        for folder in (self.media_root / "event-images", self.media_root / "event-clips", self.media_root / "thumbnails"):
            if not folder.exists():
                continue
            for path in folder.iterdir():
                url = f"/media/{folder.name}/{path.name}"
                # Media writers atomically rename just before the DB callback.
                # A grace window prevents cleanup from racing that hand-off.
                if path.is_file() and path.stat().st_mtime<orphan_cutoff and url not in referenced and not path.name.endswith(".tmp"):
                    orphan_paths.append(url)
        temporary: list[str] = []
        temporary_skipped: list[str] = []
        temp_root = self.data_root / "temporary" / self.db.runtime_mode.lower()
        cutoff = datetime.now(timezone.utc).timestamp() - 86400
        if _is_link_or_reparse(temp_root):
            try:
                if temp_root.lstat().st_mtime < cutoff:
                    temporary.append(str(temp_root))
            except OSError:
                temporary_skipped.append(".")
        elif temp_root.exists():
            path_report: dict[str, Any] = {"skipped_unsafe": 0, "skipped_paths": []}
            resolved_temp_root = temp_root.resolve()
            for path in _walk_confined_entries(temp_root, path_report):
                if not _lexical_child(path, temp_root):
                    temporary_skipped.append(_path_label(path, temp_root))
                    continue
                if _is_link_or_reparse(path):
                    try:
                        if path.lstat().st_mtime < cutoff:
                            temporary.append(str(path))
                    except OSError:
                        temporary_skipped.append(_path_label(path, temp_root))
                    continue
                resolved = _resolved_child(path, resolved_temp_root)
                if resolved is None:
                    temporary_skipped.append(_path_label(path, temp_root))
                    continue
                try:
                    if resolved.is_file() and resolved.stat().st_mtime < cutoff:
                        temporary.append(str(path))
                except OSError:
                    temporary_skipped.append(_path_label(path, temp_root))
            temporary_skipped.extend(path_report["skipped_paths"])
        return {"event_ids": sorted(delete_ids), "media_ids": sorted(media_delete_ids), "orphan_media": sorted(orphan_paths), "temporary_files": temporary, "unsafe_temporary_paths": sorted(set(temporary_skipped))[:50], "protected_current_event_ids": sorted(current_ids), "protected_dependency_event_ids": sorted(protection["dependencies"])}

    def scan(self) -> dict[str, Any]:
        plan = self._plan()
        return {**self.status(), "candidate_events": len(plan["event_ids"]), "candidate_orphans": len(plan["orphan_media"]), "candidate_temp_files": len(plan["temporary_files"])}

    def preview(self) -> dict[str, Any]:
        return {"dry_run": True, "runtime_mode": self.db.runtime_mode,"source_type":"storage_manager","is_simulated":self.db.runtime_mode!="REAL", "policy": self.policy(), **self._plan()}

    def trigger_async(self, trigger: str = "event") -> bool:
        """Coalesce post-event retention without blocking the vision thread."""
        with self._trigger_lock:
            if self._cleanup_thread and self._cleanup_thread.is_alive():
                return False
            self._cleanup_thread = threading.Thread(target=self.cleanup, kwargs={"trigger":trigger,"dry_run":False}, daemon=True, name="objectmemory-retention")
            self._cleanup_thread.start()
            return True

    def recover_pending(self) -> None:
        observation_paths=self._current_observation_paths()
        for media in self.db.list("event_media", {"status": "pending_delete"}, limit=100000, unscoped=True):
            if media.get('path') in observation_paths:
                continue
            path = self._safe_media(media.get("path"))
            active = [row for row in self.db.list("event_media", {"path":media.get("path")}, limit=1000, unscoped=True) if row.get("id") != media.get("id") and row.get("status") == "active"]
            try:
                if path and not active:
                    path.unlink(missing_ok=True)
            except OSError:
                continue
            # Pressure cleanup keeps the historical row but expires its media.
            # An interrupted event deletion may already have removed the row.
            event=next(iter(self.db.list("events",{"event_id":media.get("event_id")},limit=1,unscoped=True)),None) if media.get('event_id') else None
            if event:
                patch: dict[str, Any] = {}
                media_path = media.get("path")
                if media.get("kind") == "image":
                    if event.get("screenshot_path") == media_path:
                        patch.update({"screenshot_path": None, "screenshot_sha256": None})
                    if event.get("before_screenshot") == media_path:
                        patch.update({"before_screenshot": None, "before_screenshot_sha256": None})
                    if event.get("after_screenshot") == media_path:
                        patch.update({"after_screenshot": None, "after_screenshot_sha256": None})
                elif media.get("kind") == "clip" and event.get("clip_path") == media_path:
                    patch.update({"clip_path": None, "clip_sha256": None})
                if patch:
                    # Losing either independent keyframe or the clip makes the
                    # retained row metadata-only; it must not remain labelled
                    # as a fully evidenced confirmed placement.
                    patch["evidence_status"] = "media_expired"
                    self.db.save("events", patch, event["id"])
            self.db.delete("event_media", media["id"], unscoped=True)
        self._finalize_acceptance_receipts()

    def _prepare_acceptance_receipt(self, conn, event: dict[str, Any], stamp: str) -> None:
        """Record a verified deletion intent in the tombstone transaction only.

        Explicit event deletion and broken/unknown evidence never get a normal
        retention receipt. No media or extra event history is retained here.
        """
        if not _is_confirmed_movement(event) or event.get("detection_mode") != "aruco_screen_validation":
            return
        run = self.db.get("acceptance_runs", str(event.get("validation_run_id") or ""))
        if not run:
            return
        snapshot, _reason = acceptance_evidence_snapshot(self.db, self.media_root, event, run)
        if not snapshot:
            return
        receipt = {
            "runtime_mode": "REAL", "event_id": snapshot["event_id"],
            "validation_run_id": snapshot["validation_run_id"], "suite_id": snapshot["suite_id"],
            "item_id": snapshot["item_id"], "camera_id": snapshot["camera_id"],
            "source_session_id": snapshot["source_session_id"], "policy": self.policy(),
            "reason": "event_history_limit", "status": "pending_delete", "verified_at": stamp,
            "evidence": snapshot, "evidence_sha256": evidence_digest(snapshot), "created_by": "retention_service",
        }
        insert = {"id": uuid4().hex, "created_at": stamp, "updated_at": stamp, **self.db._encode("acceptance_retention_receipts", receipt)}
        conn.execute(
            'INSERT OR IGNORE INTO acceptance_retention_receipts ('
            + ",".join(f'"{key}"' for key in insert) + ") VALUES (" + ",".join("?" for _ in insert) + ")",
            list(insert.values()),
        )

    def _finalize_acceptance_receipts(self) -> None:
        """A crash can leave intent pending; finish only after actual deletion."""
        for receipt in self.db.list("acceptance_retention_receipts", {"status": "pending_delete"}, limit=100000):
            event_id = receipt.get("event_id")
            if self.db.list("events", {"event_id": event_id}, limit=1) or self.db.list("event_media", {"event_id": event_id}, limit=1):
                continue
            evidence = receipt.get("evidence") or {}
            if not isinstance(evidence, dict):
                continue
            if evidence_digest(evidence) != receipt.get("evidence_sha256"):
                continue
            manifest = evidence.get("media") or []
            if not isinstance(manifest, list) or len(manifest) != 3 or any(not isinstance(row, dict) for row in manifest):
                continue
            paths = [self._safe_media(row.get("path")) for row in manifest]
            # A path not removable by this service must not become a completed
            # deletion claim, even if a database row happened to disappear.
            if any(path is None or path.exists() for path in paths):
                continue
            self.db.save("acceptance_retention_receipts", {"status": "completed", "completed_at": now()}, receipt["id"])

    def delete_event(self, event_id: str, *, force: bool = False) -> dict[str, int]:
        """Delete one event through the same tombstone-first media protocol."""
        with self.lock:
            event = self.db.get("events", event_id)
            if not event:
                return {"deleted_events": 0, "deleted_media": 0, "protected_dependency": 0}
            public_id = str(event.get("event_id") or event.get("id"))
            if not force and public_id in self._protection_snapshot()["dependencies"]:
                return {"deleted_events": 0, "deleted_media": 0, "protected_dependency": 1}
            stamp = now()
            media = self.db.list("event_media", {"event_id": public_id}, limit=1000)
            with self.db.connect() as conn:
                for row in media:
                    conn.execute("UPDATE event_media SET status='pending_delete', pending_delete_at=?, updated_at=? WHERE id=?", (stamp, stamp, row["id"]))
                conn.execute(
                    """UPDATE item_current_state
                       SET evidence_event_id=NULL,last_confirmed_placed_at=NULL,last_confirmed_placement=NULL,
                           status=CASE WHEN status='confirmed_placed' THEN 'last_seen' ELSE status END,
                           evidence_ingested_at=?,updated_at=?
                       WHERE runtime_mode=? AND evidence_event_id=?""",
                    (stamp, stamp, self.db.runtime_mode, public_id),
                )
                for state in self.db.list('item_current_state',limit=100000):
                    placement=state.get('last_confirmed_placement') or {}
                    if isinstance(placement,dict) and placement.get('event_id')==public_id:
                        conn.execute('UPDATE item_current_state SET last_confirmed_placement=NULL,last_confirmed_placed_at=NULL WHERE id=?',
                                     (state['id'],))
            removed = 0
            for row in media:
                active = [other for other in self.db.list("event_media", {"path":row.get("path")}, limit=1000, unscoped=True) if other.get("status") == "active" and other.get("id") != row.get("id")]
                if not active:
                    path = self._safe_media(row.get("path"))
                    if path:
                        try:
                            existed = path.exists(); path.unlink(missing_ok=True); removed += int(existed)
                        except OSError:
                            continue
                self.db.delete("event_media", row["id"], unscoped=True)
            return {"deleted_events": self.db.delete("events", event["id"]), "deleted_media": removed, "protected_dependency": 0}

    def cleanup(self, *, trigger: str = "manual", dry_run: bool = False) -> dict[str, Any]:
        with self.lock:
            started = now()
            before = tree_size(self.data_root / "database") + tree_size(self.media_root)
            plan = self._plan()
            if dry_run:
                return {"dry_run": True,"runtime_mode":self.db.runtime_mode,"source_type":"storage_manager","is_simulated":self.db.runtime_mode!="REAL", "trigger": trigger, **plan}
            media_rows: dict[str, dict[str, Any]] = {}
            planned_event_ids = set(plan["event_ids"])
            # Planning is advisory. Re-read every protection immediately before
            # tombstoning so a pin/current-state/manual-reference change that
            # happened after preview can never be deleted from a stale plan.
            protection = self._protection_snapshot()
            event_ids: set[str] = set()
            deleted_public_ids: set[str] = set()
            # Phase 1: transactional tombstones. Files are not touched until the
            # transaction is committed.
            with self.db.connect() as conn:
                for event_id in planned_event_ids:
                    event = conn.execute("SELECT * FROM movement_events WHERE id=? AND runtime_mode=?", (event_id, self.db.runtime_mode)).fetchone()
                    if not event:
                        continue
                    public_id = str(event["event_id"] or event["id"])
                    if public_id in protection["all"] or bool(event["pinned"]) or bool(event["manually_corrected"]) or event["event_type"] == "manual_correction":
                        continue
                    self._prepare_acceptance_receipt(conn, self.db._decode("movement_events", event), started)
                    event_ids.add(str(event["id"]))
                    deleted_public_ids.add(public_id)
                    rows = conn.execute("SELECT * FROM event_media WHERE event_id=? AND runtime_mode=?", (public_id, self.db.runtime_mode)).fetchall()
                    for row in rows:
                        conn.execute("UPDATE event_media SET status='pending_delete', pending_delete_at=?, updated_at=? WHERE id=?", (started, started, row["id"]))
                        media_rows[row["id"]] = dict(row)
                for media_id in plan["media_ids"]:
                    row = conn.execute("SELECT * FROM event_media WHERE id=? AND runtime_mode=?", (media_id, self.db.runtime_mode)).fetchone()
                    if row and str(row["event_id"] or "") not in protection["all"] and str(row["event_id"] or "") not in deleted_public_ids:
                        conn.execute("UPDATE event_media SET status='pending_delete', pending_delete_at=?, updated_at=? WHERE id=?", (started, started, row["id"]))
                        media_rows[row["id"]] = dict(row)
            deleted_images = deleted_clips = deleted_other = 0
            # Phase 2: path-confined deletion. Shared paths stay until no active
            # registry row references them.
            for media in media_rows.values():
                url = media.get("path")
                active = [row for row in self.db.list("event_media", {"path": url}, limit=10, unscoped=True) if row.get("status") == "active" and row.get("event_id") not in deleted_public_ids]
                if not active:
                    path = self._safe_media(url)
                    try:
                        if path:
                            existed=path.exists()
                            path.unlink(missing_ok=True)
                            if existed and media.get("kind") == "image": deleted_images += 1
                            elif existed and media.get("kind") == "clip": deleted_clips += 1
                            elif existed: deleted_other += 1
                    except OSError:
                        continue
                self.db.delete("event_media", media["id"], unscoped=True)
                if media.get("event_id") not in deleted_public_ids:
                    field = "screenshot_path" if media.get("kind") == "image" else "clip_path" if media.get("kind") == "clip" else None
                    event = next((row for row in self.db.list("events", {"event_id":media.get("event_id")}, limit=1)), None) if media.get('event_id') else None
                    if field and event:
                        patch={field:None,"evidence_status":"media_expired"}
                        if media.get("kind")=="image":
                            if event.get("before_screenshot")==url:patch["before_screenshot"]=None
                            if event.get("after_screenshot")==url:patch["after_screenshot"]=None
                        self.db.save("events",patch,event["id"])
            # Phase 3: records only after evidence deletion has been attempted.
            deleted_events = self.db.delete_many("events", event_ids)
            self._finalize_acceptance_receipts()
            for url in plan["orphan_media"]:
                path = self._safe_media(url)
                try:
                    if path:
                        kind = path.parent.name
                        existed = path.exists()
                        path.unlink(missing_ok=True)
                        if existed and kind == "event-images": deleted_images += 1
                        elif existed and kind == "event-clips": deleted_clips += 1
                        elif existed: deleted_other += 1
                except OSError:
                    pass
            initial_skips = list(plan.get("unsafe_temporary_paths") or [])
            temporary_cleanup: dict[str, Any] = {"deleted_files": 0, "deleted_links": 0, "skipped_unsafe": len(initial_skips), "skipped_paths": initial_skips}
            temporary_root = (self.data_root / "temporary").resolve()
            for raw in plan["temporary_files"]:
                deleted, _size, was_link = _delete_confined_entry(Path(raw), temporary_root, temporary_cleanup)
                if deleted:
                    deleted_other += 1
                    temporary_cleanup["deleted_files"] += 1
                    temporary_cleanup["deleted_links"] += int(was_link)
            log_cleanup = self._trim_run_files(self.data_root / "logs", keep=2)
            self._trim_firmware_builds()
            pressure=self._enforce_storage_limit()
            after = tree_size(self.data_root / "database") + tree_size(self.media_root)
            deleted_images+=pressure["deleted_images"];deleted_clips+=pressure["deleted_clips"];deleted_other+=pressure["deleted_other"]
            report = {"runtime_mode":self.db.runtime_mode,"source_type":"storage_manager","is_simulated":self.db.runtime_mode!="REAL","policy":self.policy(),"trigger":trigger,"dry_run":False,"started_at":started,"ended_at":now(),"deleted_events":deleted_events,"deleted_images":deleted_images,"deleted_clips":deleted_clips,"deleted_other":deleted_other,"bytes_before":before,"bytes_after":after,"details":{"storage_limit_exceeded":pressure["remaining_bytes"] > self.max_storage_mb * 1024 * 1024,"pressure_cleanup":pressure,"temporary_cleanup":temporary_cleanup,"log_cleanup":log_cleanup}}
            report_id = uuid4().hex
            self.db.save("retention_reports", report, report_id)
            self.last_report = {"id": report_id, **report}
            return self.last_report

    @staticmethod
    def _trim_run_files(folder: Path, keep: int) -> dict[str, Any]:
        report: dict[str, Any] = {"deleted_files": 0, "deleted_links": 0, "skipped_unsafe": 0, "skipped_paths": []}
        keep = max(0, int(keep))
        if _is_link_or_reparse(folder):
            _note_skipped(report, folder, folder.parent)
            return report
        if not folder.exists():
            return report
        root = folder.resolve()
        regular_files: list[tuple[float, Path]] = []
        links: list[Path] = []
        try:
            entries = _walk_confined_entries(folder, report)
        except OSError:
            _note_skipped(report, folder, folder.parent)
            return report
        for path in entries:
            if not _lexical_child(path, folder):
                _note_skipped(report, path, folder)
                continue
            if _is_link_or_reparse(path):
                links.append(path)
                continue
            resolved = _resolved_child(path, root)
            if resolved is None:
                _note_skipped(report, path, folder)
                continue
            try:
                if resolved.is_file():
                    regular_files.append((resolved.stat().st_mtime, path))
            except OSError:
                _note_skipped(report, path, folder)
        # Links never count as retained run logs. Remove only the directory
        # entry itself; neither sorting nor deletion stats its external target.
        for path in links:
            deleted, _size, _was_link = _delete_confined_entry(path, root, report)
            if deleted:
                report["deleted_links"] += 1
        regular_files.sort(key=lambda value: value[0], reverse=True)
        for _mtime, path in regular_files[keep:]:
            deleted, _size, was_link = _delete_confined_entry(path, root, report)
            if deleted:
                report["deleted_files"] += 1
                report["deleted_links"] += int(was_link)
        return report

    def _trim_firmware_builds(self) -> None:
        # Shared AI/S3 archives have ONE owner: firmware-build.py, which keeps
        # the previous successful version under its complete .build.lock
        # transaction. Checking lock.exists() here was a TOCTOU race.
        # Only recognizable, flat legacy bundles inside this data root remain
        # eligible here. Unknown/failed folders and links are not user garbage.
        root = self.data_root / "firmware-builds"
        if _is_link_or_reparse(root) or not root.is_dir():
            return
        safe_root = root.resolve()
        if not safe_root.is_relative_to(self.data_root) or safe_root == self.data_root:
            return
        successful: list[tuple[Path, set[str]]] = []
        allowed = {"firmware.bin", "firmware.elf", "bootloader.bin", "partitions.bin", "boot_app0.bin"}
        try:
            for folder in root.iterdir():
                if _is_link_or_reparse(folder) or not folder.is_dir() or folder.name.startswith("."):
                    continue
                if _resolved_child(folder, safe_root) is None:
                    continue
                entries = list(folder.iterdir())
                if any(_is_link_or_reparse(path) or not path.is_file() for path in entries):
                    continue
                try:
                    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
                    artifacts = manifest.get("artifacts")
                    if manifest.get("compile_passed") is not True or not isinstance(artifacts, list) or not artifacts:
                        continue
                    names = {item.get("name") for item in artifacts if isinstance(item, dict)}
                    if "firmware.bin" not in names or not names.issubset(allowed) or len(names) != len(artifacts):
                        continue
                    if {path.name for path in entries} - names - {"manifest.json", "build.log"}:
                        continue
                    valid = True
                    for item in artifacts:
                        with (folder / item["name"]).open("rb") as stream:
                            if hashlib.file_digest(stream, "sha256").hexdigest() != item.get("sha256"):
                                valid = False
                                break
                    if valid:
                        successful.append((folder, {path.name for path in entries}))
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
            successful.sort(key=lambda value: value[0].stat().st_mtime_ns, reverse=True)
            for folder, expected_names in successful[2:]:
                # Do not recursively resolve/delete a newly substituted link.
                if _is_link_or_reparse(root) or _is_link_or_reparse(folder) or root.resolve() != safe_root:
                    continue
                if _resolved_child(folder, safe_root) is None:
                    continue
                entries = list(folder.iterdir())
                if {path.name for path in entries} != expected_names:
                    continue
                if any(_is_link_or_reparse(path) or not path.is_file() for path in entries):
                    continue
                report: dict[str, Any] = {}
                for path in entries:
                    _delete_confined_entry(path, safe_root, report)
                try:
                    folder.rmdir()
                except OSError:
                    pass
        except OSError:
            return

    def _firmware_artifact_bytes(self) -> int:
        local = tree_size(self.data_root / "firmware-builds")
        if not self.manage_project_firmware:
            return local
        public_root = self.project_root / "artifacts" / "firmware"
        firmware = self.project_root / "firmware" / "esp32cam"
        return local + tree_size(public_root) + sum(
            tree_size(firmware / name) for name in ("build", "build-xiao-esp32s3-sense")
        )

    def _managed_total(self) -> int:
        return tree_size(self.data_root/"database")+sum(tree_size(self.data_root/name) for name in ("test-media","demo-media","real-media"))+tree_size(self.data_root/"logs")+self._firmware_artifact_bytes()

    def _enforce_storage_limit(self) -> dict[str,int]:
        """Pressure order is TEST, old DEMO, then unprotected REAL media."""
        limit=self.max_storage_mb*1024*1024
        remaining=self._managed_total()
        counts={"deleted_images":0,"deleted_clips":0,"deleted_other":0}
        if remaining<=limit:return {**counts,"remaining_bytes":remaining}
        for mode in ("TEST","DEMO","REAL"):
            if remaining<=limit:break
            result=self._purge_mode_media(mode,remaining-limit)
            for key in counts:counts[key]+=result[key]
            remaining=self._managed_total()
        return {**counts,"remaining_bytes":remaining}

    def _purge_mode_media(self, mode: str, bytes_needed: int) -> dict[str,int]:
        names={"TEST":("objectmemory-test.sqlite","test-media"),"DEMO":("objectmemory-demo.sqlite","demo-media"),"REAL":("objectmemory.sqlite","real-media")}
        db_path=self.data_root/"database"/names[mode][0];media_root=(self.data_root/names[mode][1]).resolve()
        counts={"deleted_images":0,"deleted_clips":0,"deleted_other":0}
        if not media_root.exists() or not media_root.is_relative_to(self.data_root.resolve()) or media_root==self.data_root.resolve():return counts
        connection: sqlite3.Connection | None = None
        try:
            tables: set[str] = set()
            events: list[dict[str, Any]] = []
            media_rows: list[dict[str, Any]] = []
            protected: set[str] = set()
            observation_paths: set[str] = set()
            if db_path.is_file():
                connection=sqlite3.connect(db_path,timeout=5);connection.row_factory=sqlite3.Row
                connection.execute("PRAGMA busy_timeout=5000")
                tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if connection and {"movement_events","event_media"}.issubset(tables):
                event_columns={row[1] for row in connection.execute("PRAGMA table_info(movement_events)")}
                reference_column=",manual_reference_event_id" if "manual_reference_event_id" in event_columns else ""
                events=[dict(row) for row in connection.execute(f"SELECT id,event_id,item_id,pinned,manually_corrected,event_type,evidence_status,final_status,timestamp_end,created_at{reference_column} FROM movement_events WHERE runtime_mode=? ORDER BY COALESCE(timestamp_end,created_at)",(mode,))]
                media_rows=[dict(row) for row in connection.execute("SELECT id,event_id,path,kind,status,metadata,created_at FROM event_media WHERE runtime_mode=? AND COALESCE(status,'active')<>'deleted' ORDER BY created_at",(mode,))]
                protected={str(row["event_id"] or row["id"]) for row in events if row.get("pinned") or row.get("manually_corrected") or row.get("event_type")=="manual_correction"}
                if "item_current_state" in tables:
                    protected.update(str(row[0]) for row in connection.execute("SELECT evidence_event_id FROM item_current_state WHERE runtime_mode=? AND evidence_event_id IS NOT NULL",(mode,)))
                    state_columns={row[1] for row in connection.execute('PRAGMA table_info(item_current_state)')}
                    if 'last_observed' in state_columns:
                        for row in connection.execute('SELECT last_observed FROM item_current_state WHERE runtime_mode=?',(mode,)):
                            try:observed=json.loads(row[0] or '{}')
                            except (ValueError,TypeError):continue
                            if isinstance(observed,dict) and observed.get('screenshot_path'):
                                observation_paths.add(str(observed['screenshot_path']))
                    if 'last_confirmed_placement' in state_columns:
                        for row in connection.execute('SELECT last_confirmed_placement FROM item_current_state WHERE runtime_mode=?',(mode,)):
                            try:placement=json.loads(row[0] or '{}')
                            except (ValueError,TypeError):continue
                            if isinstance(placement,dict) and placement.get('event_id'):
                                protected.add(str(placement['event_id']))
                by_event={str(row.get("event_id") or row.get("id")):row for row in events}
                by_media: dict[str,list[dict[str,Any]]] = defaultdict(list)
                for row in media_rows:by_media[str(row.get("event_id") or "")].append(row)
                for manual_id in list(protected):
                    event=by_event.get(manual_id);visited=set()
                    while event and (event.get("manually_corrected") or event.get("event_type")=="manual_correction"):
                        reference_id=str(event.get("manual_reference_event_id") or "").strip() or str(self._binding_reference(by_media.get(str(event.get("event_id") or ""),[])) or "")
                        if not reference_id or reference_id in visited:break
                        visited.add(reference_id);protected.add(reference_id);event=by_event.get(reference_id)
                if mode=="REAL":
                    groups=defaultdict(list)
                    for row in reversed(events):
                        if _is_confirmed_movement(row):
                            groups[row.get("item_id")].append(row)
                    for rows in groups.values():protected.update(str(row["event_id"] or row["id"]) for row in rows[:2])
            registry_paths={str(row.get("path") or "") for row in media_rows}
            registry_paths.update(observation_paths)
            candidates=[row for row in media_rows if str(row.get("status") or "active")=="active" and str(row.get("event_id") or "") not in protected and row.get('path') not in observation_paths]
            freed=0
            # Orphans are reclaimed first even if their database has already
            # been removed. A five-minute grace protects the atomic writer-to-DB
            # hand-off in a concurrently starting DEMO/TEST process.
            orphan_cutoff=datetime.now(timezone.utc).timestamp()-300
            for folder_name in ("event-images","event-clips","thumbnails"):
                folder=media_root/folder_name
                if not folder.is_dir():continue
                for path in sorted((item for item in folder.iterdir() if item.is_file()),key=lambda item:item.stat().st_mtime):
                    if freed>=bytes_needed:break
                    url=f"/media/{folder_name}/{path.name}"
                    if url in registry_paths or path.stat().st_mtime>=orphan_cutoff:continue
                    resolved=path.resolve()
                    if not resolved.is_relative_to(media_root):continue
                    try:size=resolved.stat().st_size;resolved.unlink(missing_ok=True)
                    except OSError:continue
                    freed+=size
                    if folder_name=="event-images":counts["deleted_images"]+=1
                    elif folder_name=="event-clips":counts["deleted_clips"]+=1
                    else:counts["deleted_other"]+=1
            for media in candidates:
                if freed>=bytes_needed:break
                relative=Path(str(media.get("path") or "").removeprefix("/media/"))
                path=(media_root/relative).resolve()
                if not path.is_relative_to(media_root) or relative.parts[:1] not in (("event-images",),("event-clips",),("thumbnails",)):continue
                if not connection:continue
                stamp=now();connection.execute("BEGIN IMMEDIATE")
                fresh=connection.execute(
                    "SELECT id,event_id,item_id,pinned,manually_corrected,event_type,evidence_status,final_status "
                    "FROM movement_events WHERE runtime_mode=? AND event_id=?",
                    (mode,media["event_id"]),
                ).fetchone()
                current=connection.execute("SELECT 1 FROM item_current_state WHERE runtime_mode=? AND evidence_event_id=? LIMIT 1",(mode,media["event_id"])).fetchone() if "item_current_state" in tables else None
                dependent=connection.execute("SELECT 1 FROM movement_events WHERE runtime_mode=? AND manual_reference_event_id=? LIMIT 1",(mode,media["event_id"])).fetchone() if "manual_reference_event_id" in ({row[1] for row in connection.execute("PRAGMA table_info(movement_events)")}) else None
                latest_two: set[str] = set()
                if fresh and mode=="REAL":
                    latest_two={
                        str(row["event_id"] or row["id"])
                        for row in connection.execute(
                            "SELECT id,event_id FROM movement_events "
                            "WHERE runtime_mode=? AND item_id=? AND event_type='movement' "
                            "AND evidence_status='confirmed' AND final_status IN ('confirmed_placed','position_changed') "
                            "ORDER BY COALESCE(timestamp_end,created_at) DESC,id DESC LIMIT 2",
                            (mode,fresh["item_id"]),
                        )
                    }
                fresh_public_id=str(fresh["event_id"] or fresh["id"]) if fresh else ""
                if not fresh or fresh["pinned"] or fresh["manually_corrected"] or fresh["event_type"]=="manual_correction" or current or dependent or fresh_public_id in latest_two:
                    connection.rollback();continue
                changed=connection.execute("UPDATE event_media SET status='pending_delete',pending_delete_at=?,updated_at=? WHERE id=? AND runtime_mode=? AND COALESCE(status,'active')='active'",(stamp,stamp,media["id"],mode)).rowcount
                if not changed:
                    connection.rollback();continue
                connection.commit()
                shared=connection.execute("SELECT 1 FROM event_media WHERE path=? AND id<>? AND COALESCE(status,'active')='active' LIMIT 1",(media.get("path"),media["id"])).fetchone() is not None
                try:
                    existed=path.exists();deleted_file=existed and not shared;size=path.stat().st_size if deleted_file else 0
                    if not shared:path.unlink(missing_ok=True)
                except OSError:continue
                freed+=size
                connection.execute("DELETE FROM event_media WHERE id=?",(media["id"],))
                field="screenshot_path" if media.get("kind")=="image" else "clip_path" if media.get("kind")=="clip" else None
                if media.get("kind")=="image":
                    connection.execute("UPDATE movement_events SET screenshot_path=CASE WHEN screenshot_path=? THEN NULL ELSE screenshot_path END,before_screenshot=CASE WHEN before_screenshot=? THEN NULL ELSE before_screenshot END,after_screenshot=CASE WHEN after_screenshot=? THEN NULL ELSE after_screenshot END,evidence_status='media_expired',updated_at=? WHERE event_id=?",(media.get("path"),media.get("path"),media.get("path"),now(),media["event_id"]))
                elif field:connection.execute(f"UPDATE movement_events SET {field}=NULL,evidence_status='media_expired',updated_at=? WHERE event_id=?",(now(),media["event_id"]))
                connection.commit()
                if deleted_file and media.get("kind")=="image":counts["deleted_images"]+=1
                elif deleted_file and media.get("kind")=="clip":counts["deleted_clips"]+=1
                elif deleted_file:counts["deleted_other"]+=1
        except (sqlite3.Error,OSError):
            pass
        finally:
            if connection:
                try:connection.close()
                except sqlite3.Error:pass
        return counts

    @staticmethod
    def clear_isolated_mode(data_root: Path, mode: str, *, lease: RuntimeModeLease) -> dict[str, Any]:
        mode = mode.upper()
        names = {"DEMO": ("objectmemory-demo.sqlite", "demo-media"), "TEST": ("objectmemory-test.sqlite", "test-media")}
        if mode not in names:
            raise ValueError("普通清理只能清空 DEMO 或 TEST 数据。")
        root = data_root.resolve()
        if not lease.held or lease.mode != mode or lease.data_root != root:
            raise RuntimeError(f"清空 {mode} 数据前必须持有该数据根的活动模式租约。")
        db_name, media_name = names[mode]
        removed_files = removed_bytes = removed_rows = 0
        path_report: dict[str, Any] = {"deleted_links": 0, "skipped_unsafe": 0, "skipped_paths": []}
        database_entry=root/"database"/db_name
        media_entry=root/media_name
        # The database is never expected to be an indirection. Refuse rather
        # than clearing another database through an attacker-controlled link.
        if _is_link_or_reparse(database_entry):
            raise ValueError("拒绝通过链接或重解析点清空隔离数据库。")
        database=database_entry.resolve()
        if not database.is_relative_to(root) or database==root or not _lexical_child(media_entry,root):
            raise ValueError("拒绝清理数据根目录以外的路径。")
        if database.is_file():
            connection=None
            try:
                connection=sqlite3.connect(database,timeout=.5)
                connection.execute("PRAGMA busy_timeout=500")
                connection.execute("BEGIN EXCLUSIVE")
                tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                # Child/device tables precede their parents. Migration history is
                # preserved so the empty isolated DB remains immediately usable.
                order=['device_commands','firmware_devices','device_enrollments','firmware_jobs','acceptance_retention_receipts','acceptance_runs','acceptance_suites','event_media','movement_events','item_current_state','source_sessions','retention_reports','tracks','companions','zones','scenes','item_recognition_profiles','item_reference_images','cameras','items','settings']
                for table in order:
                    if table in tables:
                        removed_rows+=int(connection.execute(f'DELETE FROM "{table}"').rowcount)
                connection.commit()
            except sqlite3.OperationalError as exc:
                if connection:connection.rollback()
                raise RuntimeError(f"{mode} 数据库正被另一进程使用，本次未清理：{exc}") from exc
            finally:
                if connection:connection.close()
        if _is_link_or_reparse(media_entry):
            # Never resolve or enumerate a linked mode-media root. Removing the
            # directory entry itself is safe and leaves its target untouched.
            try:
                link_bytes=int(media_entry.lstat().st_size)
            except OSError:
                link_bytes=0
            if _remove_link_only(media_entry):
                removed_files+=1;removed_bytes+=link_bytes;path_report["deleted_links"]+=1
            else:
                _note_skipped(path_report,media_entry,root)
        elif media_entry.exists():
            media=media_entry.resolve()
            if not media.is_relative_to(root) or media==root:
                _note_skipped(path_report,media_entry,root)
            elif media.is_dir():
                try:
                    entries=_walk_confined_entries(media_entry,path_report)
                except OSError:
                    entries=[];_note_skipped(path_report,media_entry,root)
                files: list[Path] = []
                links: list[Path] = []
                folders: list[Path] = []
                for path in entries:
                    if not _lexical_child(path,media_entry):
                        _note_skipped(path_report,path,media_entry)
                        continue
                    if _is_link_or_reparse(path):
                        links.append(path)
                        continue
                    resolved=_resolved_child(path,media)
                    if resolved is None:
                        _note_skipped(path_report,path,media_entry)
                        continue
                    try:
                        if resolved.is_file():files.append(path)
                        elif resolved.is_dir():folders.append(path)
                    except OSError:
                        _note_skipped(path_report,path,media_entry)
                for path in files:
                    deleted,size,was_link=_delete_confined_entry(path,media,path_report)
                    if deleted:
                        removed_files+=1;removed_bytes+=size;path_report["deleted_links"]+=int(was_link)
                # Links and junctions are never traversed or recursively
                # removed. Delete only their own entry, deepest names first.
                for path in sorted(links,key=lambda value:len(value.parts),reverse=True):
                    deleted,size,_was_link=_delete_confined_entry(path,media,path_report)
                    if deleted:
                        removed_files+=1;removed_bytes+=size;path_report["deleted_links"]+=1
                # Only empty, freshly revalidated directories below the exact
                # mode media root are removed.
                for folder in sorted(folders,key=lambda value:len(value.parts),reverse=True):
                    if _is_link_or_reparse(folder):
                        deleted,size,_was_link=_delete_confined_entry(folder,media,path_report)
                        if deleted:
                            removed_files+=1;removed_bytes+=size;path_report["deleted_links"]+=1
                        continue
                    resolved=_resolved_child(folder,media)
                    if resolved is None or not resolved.is_dir():
                        _note_skipped(path_report,folder,media_entry)
                        continue
                    try:folder.rmdir()
                    except OSError:pass
        return {"deleted_rows":removed_rows,"deleted_files":removed_files,"deleted_bytes":removed_bytes,**path_report}
