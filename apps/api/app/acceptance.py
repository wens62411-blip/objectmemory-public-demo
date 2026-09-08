"""Server-owned control plane for REAL physical movement acceptance.

This module never creates or completes movement events.  It only prepares a
bounded validation run, proves that it is attached to the currently streaming
local OpenCV source session (not hardware identity), and records observations delivered by the
server-side vision callback.  EventService/Database own the only PASSED
transition.
"""
from __future__ import annotations

import json
import hashlib
import math
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import cv2
import numpy as np

from .db import Database, now
from .runtime_mode import RuntimeMode
from .retention import acceptance_evidence_snapshot, evidence_digest


class AcceptanceRejected(ValueError):
    """A physical acceptance request failed a server-side authenticity check."""


TRIAL_KINDS = {
    "movement", "stationary", "minor_adjustment", "occlusion",
    "disconnect_reconnect",
}
SUITE_CONTRACT_VERSION = "p0-real-movement-v1"
SUITE_SCENARIOS: tuple[dict[str, Any], ...] = (
    {"scenario_index": 1, "trial_kind": "stationary", "from_zone": "A", "to_zone": "A", "minimum_duration_seconds": 300.0},
    {"scenario_index": 2, "trial_kind": "minor_adjustment", "from_zone": "A", "to_zone": "A", "minimum_duration_seconds": 15.0},
    {"scenario_index": 3, "trial_kind": "movement", "from_zone": "A", "to_zone": "B", "minimum_duration_seconds": 0.0},
    {"scenario_index": 4, "trial_kind": "movement", "from_zone": "B", "to_zone": "A", "minimum_duration_seconds": 0.0},
    {"scenario_index": 5, "trial_kind": "movement", "from_zone": "A", "to_zone": "B", "minimum_duration_seconds": 0.0},
    {"scenario_index": 6, "trial_kind": "movement", "from_zone": "B", "to_zone": "A", "minimum_duration_seconds": 0.0},
    {"scenario_index": 7, "trial_kind": "occlusion", "from_zone": "A", "to_zone": "A", "minimum_duration_seconds": 8.0},
    {"scenario_index": 8, "trial_kind": "disconnect_reconnect", "from_zone": "A", "to_zone": "A", "minimum_duration_seconds": 0.0},
    {"scenario_index": 9, "trial_kind": "movement", "from_zone": "A", "to_zone": "B", "minimum_duration_seconds": 0.0},
    {"scenario_index": 10, "trial_kind": "movement", "from_zone": "B", "to_zone": "A", "minimum_duration_seconds": 0.0},
    {"scenario_index": 11, "trial_kind": "movement", "from_zone": "A", "to_zone": "B", "minimum_duration_seconds": 0.0},
    {"scenario_index": 12, "trial_kind": "movement", "from_zone": "B", "to_zone": "A", "minimum_duration_seconds": 0.0},
    {"scenario_index": 13, "trial_kind": "movement", "from_zone": "A", "to_zone": "B", "minimum_duration_seconds": 0.0},
    {"scenario_index": 14, "trial_kind": "movement", "from_zone": "B", "to_zone": "A", "minimum_duration_seconds": 0.0},
)
SCENARIO_KINDS: dict[int, str] = {
    int(scenario["scenario_index"]): str(scenario["trial_kind"])
    for scenario in SUITE_SCENARIOS
}
TERMINAL_STATUSES = {"PASSED", "CANCELLED", "CAMERA_INTERRUPTED", "FAILED"}

_DURATION_LIMITS: dict[str, tuple[float, float, float]] = {
    "movement": (0.0, 3600.0, 0.0),
    "stationary": (300.0, 1800.0, 300.0),
    "minor_adjustment": (5.0, 300.0, 15.0),
    "occlusion": (1.0, 300.0, 8.0),
    "disconnect_reconnect": (0.0, 300.0, 0.0),
}

DEFAULT_THRESHOLDS: dict[str, int | float] = {
    "origin_stable_frames": 10,
    "destination_stable_frames": 10,
    "min_stable_seconds": 3.0,
    "stable_jitter_norm": 0.018,
    "stable_speed_norm_s": 0.025,
    "min_exit_frames": 2,
    "min_transit_frames": 3,
    "min_movement_distance_norm": 0.15,
    "min_trajectory_length_norm": 0.18,
    "occlusion_seconds": 0.6,
    "abort_lost_seconds": 8.0,
    "max_frame_gap_seconds": 0.75,
    "max_run_seconds": 180.0,
    "min_confidence": 0.55,
    "min_marker_size_px": 36,
}

_THRESHOLD_LIMITS: dict[str, tuple[float, float, type]] = {
    "origin_stable_frames": (2, 120, int),
    "destination_stable_frames": (2, 120, int),
    "min_stable_seconds": (2.0, 5.0, float),
    "stable_jitter_norm": (0.001, 0.25, float),
    "stable_speed_norm_s": (0.001, 1.0, float),
    "min_exit_frames": (1, 30, int),
    "min_transit_frames": (1, 120, int),
    "min_movement_distance_norm": (0.02, 1.0, float),
    "min_trajectory_length_norm": (0.02, 4.0, float),
    "occlusion_seconds": (0.0, 30.0, float),
    "abort_lost_seconds": (0.1, 120.0, float),
    "max_frame_gap_seconds": (0.05, 5.0, float),
    "max_run_seconds": (10.0, 1800.0, float),
    "min_confidence": (0.0, 1.0, float),
    "min_marker_size_px": (8, 2000, int),
}


def _polygon_area(points: list[list[float]]) -> float:
    return abs(sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    )) / 2.0


def _centroid(points: list[list[float]]) -> tuple[float, float]:
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


def _orientation(a: list[float], b: list[float], c: list[float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a: list[float], b: list[float], point: list[float]) -> bool:
    epsilon = 1e-9
    return (
        abs(_orientation(a, b, point)) <= epsilon
        and min(a[0], b[0]) - epsilon <= point[0] <= max(a[0], b[0]) + epsilon
        and min(a[1], b[1]) - epsilon <= point[1] <= max(a[1], b[1]) + epsilon
    )


def _segments_intersect(a: list[float], b: list[float], c: list[float], d: list[float]) -> bool:
    ab_c, ab_d = _orientation(a, b, c), _orientation(a, b, d)
    cd_a, cd_b = _orientation(c, d, a), _orientation(c, d, b)
    if (ab_c > 0) != (ab_d > 0) and (cd_a > 0) != (cd_b > 0):
        return True
    return any((
        _on_segment(a, b, c), _on_segment(a, b, d),
        _on_segment(c, d, a), _on_segment(c, d, b),
    ))


def _point_in_polygon(point: list[float], polygon: list[list[float]]) -> bool:
    inside = False
    x, y = point
    for index, first in enumerate(polygon):
        second = polygon[(index + 1) % len(polygon)]
        if _on_segment(first, second, point):
            return True
        if (first[1] > y) != (second[1] > y):
            crossing = (second[0] - first[0]) * (y - first[1]) / (second[1] - first[1]) + first[0]
            if x < crossing:
                inside = not inside
    return inside


def _polygons_overlap(first: list[list[float]], second: list[list[float]]) -> bool:
    for index, a in enumerate(first):
        b = first[(index + 1) % len(first)]
        for other_index, c in enumerate(second):
            d = second[(other_index + 1) % len(second)]
            if _segments_intersect(a, b, c, d):
                return True
    return _point_in_polygon(first[0], second) or _point_in_polygon(second[0], first)


class AcceptanceService:
    def __init__(
        self,
        db: Database,
        mode: RuntimeMode,
        provenance_resolver: Callable[[dict[str, Any]], tuple[str, bool]],
        media_root: Path | None = None,
    ) -> None:
        self.db = db
        self.mode = mode
        self.provenance_resolver = provenance_resolver
        self.media_root = Path(media_root) if media_root is not None else self.db.path.parent.parent / "real-media"
        self._track_lock = threading.Lock()
        self._latest_tracks: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._preflight_samples: dict[str, tuple[float, str, int]] = {}
        # A process restart destroys the engine state that owned an ACTIVE run.
        # Fail closed instead of allowing a later source generation to consume it.
        if self.mode is RuntimeMode.REAL:
            stamp = now()
            with self.db.connect() as connection:
                connection.execute(
                    "UPDATE acceptance_runs SET status='CAMERA_INTERRUPTED',outcome='INTERRUPTED',"
                    "failure_reason='backend_restarted',ended_at=?,updated_at=? "
                    "WHERE runtime_mode='REAL' AND status='ACTIVE'",
                    (stamp, stamp),
                )

    def _require_real(self) -> None:
        if self.mode is not RuntimeMode.REAL:
            raise AcceptanceRejected("真实物品验收只允许在 REAL 模式运行。")

    @staticmethod
    def _thresholds(supplied: Any) -> dict[str, int | float]:
        if supplied is None:
            return dict(DEFAULT_THRESHOLDS)
        if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT_THRESHOLDS):
            raise AcceptanceRejected("验收阈值包含未知字段。")
        result: dict[str, int | float] = dict(DEFAULT_THRESHOLDS)
        for key, value in supplied.items():
            minimum, maximum, kind = _THRESHOLD_LIMITS[key]
            if isinstance(value, bool):
                raise AcceptanceRejected(f"验收阈值 {key} 无效。")
            try:
                parsed = kind(value)
            except (TypeError, ValueError):
                raise AcceptanceRejected(f"验收阈值 {key} 无效。") from None
            if not minimum <= parsed <= maximum:
                raise AcceptanceRejected(f"验收阈值 {key} 超出允许范围。")
            result[key] = parsed
        if float(result["abort_lost_seconds"]) <= float(result["occlusion_seconds"]):
            raise AcceptanceRejected("丢失中止时间不能短于遮挡容忍时间。")
        return result

    @staticmethod
    def _minimum_duration(trial_kind: str, supplied: Any) -> float:
        minimum, maximum, default = _DURATION_LIMITS[trial_kind]
        if supplied is None:
            return default
        if isinstance(supplied, bool):
            raise AcceptanceRejected("minimum_duration_seconds 必须是数字。")
        try:
            value = float(supplied)
        except (TypeError, ValueError):
            raise AcceptanceRejected("minimum_duration_seconds 必须是数字。") from None
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise AcceptanceRejected(
                f"{trial_kind} 的 minimum_duration_seconds 必须位于 {minimum:g} 到 {maximum:g}。"
            )
        return value

    @staticmethod
    def _zone_snapshot(zone: dict[str, Any]) -> dict[str, Any]:
        points = zone.get("points")
        if (
            not isinstance(points, list) or not 3 <= len(points) <= 64
            or any(not isinstance(point, list) or len(point) != 2 for point in points)
        ):
            raise AcceptanceRejected("验收区域坐标无效。")
        normalized: list[list[float]] = []
        for point in points:
            try:
                pair = [float(point[0]), float(point[1])]
            except (TypeError, ValueError):
                raise AcceptanceRejected("验收区域坐标无效。") from None
            if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in pair):
                raise AcceptanceRejected("验收区域必须位于摄像头画面内。")
            normalized.append(pair)
        if _polygon_area(normalized) < 0.005:
            raise AcceptanceRejected("验收区域面积过小。")
        return {"id": str(zone["id"]), "name": str(zone.get("name") or "未命名区域"), "points": normalized}

    def bind_marker(self, item_id: str, requested_id: Any = None) -> dict[str, Any]:
        self._require_real()
        item = self.db.get("items", str(item_id), unscoped=True)
        if not item:
            raise AcceptanceRejected("验收物品不存在。")
        if self.db.list("acceptance_runs", {"status": "ACTIVE"}, limit=1):
            raise AcceptanceRejected("当前有真实验收正在运行，不能重建标记映射。")
        used = {
            int(row["aruco_id"])
            for row in self.db.list("items", limit=100000, unscoped=True)
            if row.get("id") != item["id"] and row.get("aruco_id") is not None
        }
        if requested_id is None:
            marker_id = next((candidate for candidate in range(50) if candidate not in used), None)
            if marker_id is None:
                raise AcceptanceRejected("可用 ArUco 标记编号已用完。")
        else:
            if isinstance(requested_id, bool):
                raise AcceptanceRejected("ArUco 标记编号必须是 0 到 49 的整数。")
            try:
                marker_id = int(requested_id)
            except (TypeError, ValueError):
                raise AcceptanceRejected("ArUco 标记编号必须是 0 到 49 的整数。") from None
            if marker_id < 0 or marker_id > 49 or marker_id in used:
                raise AcceptanceRejected("这个 ArUco 标记编号不可用。")
        return self.db.save("items", {"aruco_id": marker_id}, item["id"])

    @staticmethod
    def marker_png(marker_id: int, size: int = 1000) -> bytes:
        if isinstance(marker_id, bool) or not 0 <= int(marker_id) <= 49:
            raise AcceptanceRejected("ArUco 标记编号必须是 0 到 49。")
        size = max(400, min(1600, int(size)))
        border = max(40, size // 9)
        marker_size = size - border * 2
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        marker = cv2.aruco.generateImageMarker(dictionary, int(marker_id), marker_size)
        canvas = np.full((size, size), 255, dtype=np.uint8)
        canvas[border:border + marker_size, border:border + marker_size] = marker
        encoded_ok, encoded = cv2.imencode(".png", canvas)
        if not encoded_ok:
            raise AcceptanceRejected("ArUco 标记图片生成失败。")
        return encoded.tobytes()

    @staticmethod
    def _contract_snapshot() -> list[dict[str, Any]]:
        return [dict(scenario) for scenario in SUITE_SCENARIOS]

    @staticmethod
    def _camera_fingerprint(camera: dict[str, Any]) -> dict[str, str]:
        # Values may contain credentials; retain only digests, never copied
        # source URLs, passwords, tokens, or a plaintext configuration snapshot.
        source = str(camera.get("source") or "")
        frozen = {key: camera.get(key) for key in (
            "source_type", "source", "config", "runtime_mode", "enabled", "inference_fps", "save_clips",
        )}
        return {
            "camera_config_sha256": evidence_digest(frozen),
            "camera_source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        }

    def require_camera_unchanged(self, suite: dict[str, Any]) -> None:
        camera = self.db.get("cameras", str(suite.get("camera_id") or ""), unscoped=True)
        if not camera or any(suite.get(key) != value for key, value in self._camera_fingerprint(camera).items()):
            raise AcceptanceRejected("验收摄像头来源或配置已改变，请重新创建套件。")

    def create_suite(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_real()
        allowed = {"item_id", "camera_id", "zone_a_id", "zone_b_id", "thresholds"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise AcceptanceRejected("验收套件包含未知字段。")
        item = self.db.get("items", str(payload.get("item_id") or ""), unscoped=True)
        camera = self.db.get("cameras", str(payload.get("camera_id") or ""), unscoped=True)
        if not item or not camera:
            raise AcceptanceRejected("验收物品或摄像头不存在。")
        source_type, simulated = self.provenance_resolver(camera)
        if source_type != "opencv_camera" or simulated:
            raise AcceptanceRejected("真实物品验收只接受本机 OpenCV 物理摄像头。")
        if item.get("aruco_id") is None:
            raise AcceptanceRejected("请先为验收物品绑定 ArUco 屏幕标记。")
        zone_a = self.db.get("zones", str(payload.get("zone_a_id") or ""), unscoped=True)
        zone_b = self.db.get("zones", str(payload.get("zone_b_id") or ""), unscoped=True)
        if (
            not zone_a or not zone_b or zone_a["id"] == zone_b["id"]
            or zone_a.get("camera_id") != camera["id"] or zone_b.get("camera_id") != camera["id"]
        ):
            raise AcceptanceRejected("验收套件必须绑定当前摄像头内两个不同的 A/B 区域。")
        if not zone_a.get("enabled") or not zone_b.get("enabled"):
            raise AcceptanceRejected("验收区域必须处于启用状态。")
        zone_a_snapshot = self._zone_snapshot(zone_a)
        zone_b_snapshot = self._zone_snapshot(zone_b)
        distance = math.dist(_centroid(zone_a_snapshot["points"]), _centroid(zone_b_snapshot["points"]))
        if distance < 0.12:
            raise AcceptanceRejected("两个验收区域中心距离过近。")
        if _polygons_overlap(zone_a_snapshot["points"], zone_b_snapshot["points"]):
            raise AcceptanceRejected("两个验收区域不能重叠或接触。")
        thresholds = self._thresholds(payload.get("thresholds"))
        thresholds["max_run_seconds"] = max(
            float(thresholds["max_run_seconds"]),
            300.0 + float(thresholds["max_frame_gap_seconds"]) + 1.0,
        )
        suite_id = uuid4().hex
        return self.db.save("acceptance_suites", {
            "suite_id": suite_id, "runtime_mode": "REAL",
            "contract_version": SUITE_CONTRACT_VERSION,
            "contract": self._contract_snapshot(),
            "item_id": item["id"], "camera_id": camera["id"], "aruco_id": int(item["aruco_id"]),
            "zone_a_id": zone_a_snapshot["id"], "zone_b_id": zone_b_snapshot["id"],
            "zone_a": zone_a_snapshot, "zone_b": zone_b_snapshot,
            "thresholds": thresholds, "source_type": "opencv_camera", "is_simulated": False,
            "created_by": "admin_acceptance_api",
            **self._camera_fingerprint(camera),
        }, suite_id)

    def require_suite(self, suite_id: str) -> dict[str, Any]:
        suite = self.db.get("acceptance_suites", str(suite_id))
        if not suite:
            raise AcceptanceRejected("验收套件不存在。")
        return suite

    def suite_summary(self, suite_id: str) -> dict[str, Any]:
        suite = self.require_suite(suite_id)
        runs = sorted(
            self.db.list("acceptance_runs", {"suite_id": suite["id"]}, limit=100),
            key=lambda run: int(run.get("scenario_index") or 0),
        )
        by_index = {int(run.get("scenario_index") or 0): run for run in runs}
        def matches_contract(run: dict[str, Any]) -> bool:
            scenario_index = int(run.get("scenario_index") or 0)
            if scenario_index not in SCENARIO_KINDS:
                return False
            scenario = SUITE_SCENARIOS[scenario_index - 1]
            origin = suite["zone_a"] if scenario["from_zone"] == "A" else suite["zone_b"]
            destination = suite["zone_a"] if scenario["to_zone"] == "A" else suite["zone_b"]
            return bool(
                run.get("suite_id") == suite["id"]
                and isinstance(suite.get("camera_config_sha256"), str) and len(suite["camera_config_sha256"]) == 64
                and isinstance(suite.get("camera_source_sha256"), str) and len(suite["camera_source_sha256"]) == 64
                and run.get("contract_version") == suite.get("contract_version") == SUITE_CONTRACT_VERSION
                and run.get("item_id") == suite.get("item_id")
                and run.get("camera_id") == suite.get("camera_id")
                and run.get("aruco_id") == suite.get("aruco_id")
                and run.get("source_type") == "opencv_camera" and run.get("is_simulated") is False
                and run.get("trial_kind") == scenario["trial_kind"]
                and float(run.get("minimum_duration_seconds") or 0) == float(scenario["minimum_duration_seconds"])
                and run.get("origin_zone_id") == origin["id"]
                and run.get("destination_zone_id") == destination["id"]
                and run.get("origin_zone") == origin and run.get("destination_zone") == destination
                and run.get("thresholds") == suite.get("thresholds")
            )
        complete_contract = (
            len(runs) == len(SUITE_SCENARIOS)
            and set(by_index) == set(SCENARIO_KINDS)
            and suite.get("contract") == self._contract_snapshot()
            and all(matches_contract(run) for run in runs)
            and all(run.get("status") in TERMINAL_STATUSES for run in runs)
        )
        movement_runs = [run for run in runs if run.get("trial_kind") == "movement"]
        for run in runs:
            run.update(self._run_evidence_status(run))
            if not matches_contract(run):
                run.update({"score_eligible": False, "evidence_integrity_reason": "suite_contract_mismatch"})
        movement_passed = sum(run["score_eligible"] and matches_contract(run) for run in movement_runs)
        negative_runs = [run for run in runs if run.get("trial_kind") != "movement"]
        negative_passed = sum(
            run["score_eligible"]
            for run in negative_runs
        )
        overall_passed = bool(
            complete_contract and len(movement_runs) == 10 and movement_passed >= 8
            and len(negative_runs) == 4 and negative_passed == 4
        )
        terminal_count = sum(run.get("status") in TERMINAL_STATUSES for run in runs)
        next_index = len(runs) + 1 if len(runs) < len(SUITE_SCENARIOS) else None
        return {
            **suite,
            "suite_status": (
                "PASSED" if overall_passed else "FAILED" if complete_contract
                else "IN_PROGRESS" if runs else "READY"
            ),
            "overall_passed": overall_passed,
            "summary": {
                "contract_complete": complete_contract,
                "created_runs": len(runs), "terminal_runs": terminal_count,
                "movement_total": len(movement_runs), "movement_passed": movement_passed,
                "movement_required": 8,
                "movement_evidence_retained": sum(run["evidence_retained"] for run in movement_runs),
                "movement_policy_deleted": sum(run["evidence_integrity_status"] == "policy_deleted" for run in movement_runs),
                "movement_evidence_invalid": sum(run["evidence_integrity_status"] == "missing_or_tampered" for run in movement_runs),
                "negative_total": len(negative_runs), "negative_passed": negative_passed,
                "negative_required": 4, "next_scenario_index": next_index,
            },
            "runs": runs,
        }

    def _run_evidence_status(self, run: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "score_eligible": False, "evidence_integrity_status": "not_passed",
            "evidence_retained": False, "retained_by_policy": False,
        }
        if run.get("status") != "PASSED" or run.get("outcome") != "PASSED":
            return result
        if run.get("trial_kind") != "movement":
            result.update({"evidence_integrity_status": "not_applicable", "score_eligible": not run.get("event_id") and int(run.get("event_count_after") or 0) == int(run.get("event_count_before") or 0)})
            return result
        if not run.get("user_executed"):
            return {**result, "evidence_integrity_status": "missing_or_tampered", "evidence_integrity_reason": "server_observed_execution_missing"}
        events = self.db.list("events", {"event_id": run.get("event_id")}, limit=1) if run.get("event_id") else []
        event = events[0] if events else None
        snapshot, reason = acceptance_evidence_snapshot(self.db, self.media_root, event, run)
        if snapshot:
            return {**result, "score_eligible": True, "evidence_integrity_status": "verified", "evidence_retained": True, "retained_by_policy": True}
        # A surviving damaged event can never be excused with an older receipt.
        receipts = self.db.list("acceptance_retention_receipts", {"event_id": run.get("event_id")}, limit=1) if not event and run.get("event_id") else []
        receipt = receipts[0] if receipts else {}
        evidence = receipt.get("evidence") or {}
        identity = ("event_id", "validation_run_id", "suite_id", "item_id", "camera_id", "source_session_id")
        manifest = evidence.get("media") if isinstance(evidence, dict) else None
        valid_receipt = bool(
            receipt.get("status") == "completed" and receipt.get("completed_at")
            and receipt.get("verified_at") and receipt.get("created_by") == "retention_service"
            and receipt.get("runtime_mode") == "REAL" and receipt.get("policy") in {"MINIMAL", "BALANCED", "FORENSIC"}
            and receipt.get("reason") == "event_history_limit" and isinstance(evidence, dict)
            and evidence.get("version") == "p0-policy-evidence-v1"
            and evidence.get("runtime_mode") == "REAL" and evidence.get("source_type") == "opencv_camera" and evidence.get("is_simulated") is False
            and all(receipt.get(key) == run.get(key) == evidence.get(key) for key in identity)
            and isinstance(manifest, list) and len(manifest) == 3
            and all(isinstance(row, dict) for row in manifest)
            and {row.get("role") for row in manifest} == {"before", "after", "clip"}
            and all(isinstance(row.get("sha256"), str) and len(row["sha256"]) == 64 for row in manifest)
            and receipt.get("evidence_sha256") == evidence_digest(evidence)
            and not self.db.list("event_media", {"event_id": run.get("event_id")}, limit=1)
        )
        if valid_receipt:
            return {**result, "score_eligible": True, "evidence_integrity_status": "policy_deleted", "retention_receipt_id": receipt["id"], "evidence_integrity_reason": "历史证据已在保留策略删除前核验；当前媒体已清理。"}
        return {**result, "evidence_integrity_status": "missing_or_tampered", "evidence_integrity_reason": reason or "trusted_policy_receipt_missing"}

    def list_suites(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return [
            self.suite_summary(suite["id"])
            for suite in self.db.list("acceptance_suites", limit=max(1, min(int(limit), 1000)))
        ]

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_real()
        allowed = {
            "suite_id", "scenario_index", "item_id", "camera_id", "trial_kind",
            "origin_zone_id", "destination_zone_id", "from_zone_id", "to_zone_id",
            "minimum_duration_seconds",
        }
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise AcceptanceRejected("验收任务包含未知字段。")
        suite = self.require_suite(str(payload.get("suite_id") or ""))
        self.require_camera_unchanged(suite)
        if (
            suite.get("contract_version") != SUITE_CONTRACT_VERSION
            or suite.get("contract") != self._contract_snapshot()
        ):
            raise AcceptanceRejected("验收套件合同版本无效，请重新创建套件。")
        raw_scenario_index = payload.get("scenario_index")
        if isinstance(raw_scenario_index, bool):
            raise AcceptanceRejected("scenario_index 必须是本验收套件中的 1 到 14。")
        try:
            scenario_index = int(raw_scenario_index)
        except (TypeError, ValueError):
            raise AcceptanceRejected("scenario_index 必须是本验收套件中的 1 到 14。") from None
        if str(raw_scenario_index).strip() != str(scenario_index) or scenario_index not in SCENARIO_KINDS:
            raise AcceptanceRejected("scenario_index 必须是本验收套件中的 1 到 14。")
        scenario = SUITE_SCENARIOS[scenario_index - 1]
        item = self.db.get("items", suite["item_id"], unscoped=True)
        camera = self.db.get("cameras", suite["camera_id"], unscoped=True)
        zone_a = self.db.get("zones", suite["zone_a_id"], unscoped=True)
        zone_b = self.db.get("zones", suite["zone_b_id"], unscoped=True)
        if not item or not camera or not zone_a or not zone_b:
            raise AcceptanceRejected("验收套件绑定的物品、摄像头或区域已不存在。")
        source_type, simulated = self.provenance_resolver(camera)
        if source_type != "opencv_camera" or simulated:
            raise AcceptanceRejected("验收套件不再绑定本机 OpenCV 物理摄像头。")
        if item.get("aruco_id") != suite.get("aruco_id"):
            raise AcceptanceRejected("验收物品的 ArUco 标记已改变，请重新创建套件。")
        if (
            not zone_a.get("enabled") or not zone_b.get("enabled")
            or self._zone_snapshot(zone_a) != suite.get("zone_a")
            or self._zone_snapshot(zone_b) != suite.get("zone_b")
        ):
            raise AcceptanceRejected("验收区域已改变或停用，请重新创建套件。")

        expected_origin = suite["zone_a"] if scenario["from_zone"] == "A" else suite["zone_b"]
        expected_destination = suite["zone_a"] if scenario["to_zone"] == "A" else suite["zone_b"]
        supplied_bindings = {
            "item_id": suite["item_id"], "camera_id": suite["camera_id"],
            "trial_kind": scenario["trial_kind"],
            "origin_zone_id": expected_origin["id"], "from_zone_id": expected_origin["id"],
            "destination_zone_id": expected_destination["id"], "to_zone_id": expected_destination["id"],
        }
        for key, expected in supplied_bindings.items():
            if key in payload and str(payload[key]) != str(expected):
                raise AcceptanceRejected(f"{key} 与验收套件第 {scenario_index} 轮合同不一致。")
        minimum_duration = float(scenario["minimum_duration_seconds"])
        if "minimum_duration_seconds" in payload:
            supplied_duration = self._minimum_duration(str(scenario["trial_kind"]), payload["minimum_duration_seconds"])
            if supplied_duration != minimum_duration:
                raise AcceptanceRejected("minimum_duration_seconds 由验收套件合同固定，不能按轮修改。")
        thresholds = self._thresholds(suite.get("thresholds"))
        validation_run_id = uuid4().hex
        stamp = now()
        run_data = {
            "validation_run_id": validation_run_id, "suite_id": suite["id"],
            "contract_version": SUITE_CONTRACT_VERSION,
            "runtime_mode": "REAL", "status": "CREATED", "trial_kind": scenario["trial_kind"],
            "scenario_index": scenario_index, "minimum_duration_seconds": minimum_duration,
            "item_id": item["id"], "camera_id": camera["id"],
            "source_type": "opencv_camera", "is_simulated": False,
            "aruco_id": int(item["aruco_id"]), "detection_mode": "aruco_screen_validation",
            "origin_zone_id": expected_origin["id"], "destination_zone_id": expected_destination["id"],
            "expected_from_zone": expected_origin["name"], "expected_to_zone": expected_destination["name"],
            "origin_zone": expected_origin, "destination_zone": expected_destination,
            "thresholds": thresholds, "user_executed": False,
            "marker_status": {"state": "not_seen", "seen": False},
            "progress": {"phase": "created", "controlled_disconnect": False, "controlled_reconnect": False},
            "outcome": "PENDING", "created_by": "admin_acceptance_api",
        }

        def operation() -> dict[str, Any]:
            with self.db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current_suite = connection.execute(
                    "SELECT id FROM acceptance_suites WHERE id=? AND runtime_mode='REAL'",
                    (suite["id"],),
                ).fetchone()
                if not current_suite:
                    raise AcceptanceRejected("验收套件已不存在。")
                previous = connection.execute(
                    "SELECT scenario_index,status FROM acceptance_runs WHERE runtime_mode='REAL' AND suite_id=? ORDER BY scenario_index",
                    (suite["id"],),
                ).fetchall()
                existing_indices = [int(row["scenario_index"]) for row in previous]
                expected_next = len(existing_indices) + 1
                if existing_indices != list(range(1, expected_next)):
                    raise AcceptanceRejected("验收套件已有不连续轮次，必须人工审计后重新创建套件。")
                if scenario_index != expected_next:
                    raise AcceptanceRejected(f"验收轮次必须严格按顺序创建；下一轮应为 {expected_next}。")
                if previous and previous[-1]["status"] not in TERMINAL_STATUSES:
                    raise AcceptanceRejected("前一轮尚未结束，不能创建下一轮。")
                event_count = int(connection.execute(
                    "SELECT COUNT(*) FROM movement_events WHERE runtime_mode='REAL' AND item_id=?",
                    (item["id"],),
                ).fetchone()[0])
                encoded = self.db._encode("acceptance_runs", {**run_data, "event_count_before": event_count})
                insert = {"id": validation_run_id, "created_at": stamp, "updated_at": stamp, **encoded}
                connection.execute(
                    'INSERT INTO "acceptance_runs" ('
                    + ",".join(f'"{key}"' for key in insert)
                    + ") VALUES (" + ",".join("?" for _ in insert) + ")",
                    list(insert.values()),
                )
                row = connection.execute("SELECT * FROM acceptance_runs WHERE id=?", (validation_run_id,)).fetchone()
                return self.db._decode("acceptance_runs", row)

        try:
            return self.db._run_retry(operation)
        except sqlite3.IntegrityError:
            raise AcceptanceRejected("这一验收套件轮次已经创建，不能重试或重复创建。") from None

    def require(self, validation_run_id: str) -> dict[str, Any]:
        run = self.db.get("acceptance_runs", str(validation_run_id))
        if not run:
            raise AcceptanceRejected("验收任务不存在。")
        return run

    def list(self, *, validation_run_id: str | None = None, suite_id: str | None = None, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {}
        if validation_run_id:
            filters["validation_run_id"] = str(validation_run_id)
        if suite_id:
            filters["suite_id"] = str(suite_id)
        if status:
            normalized = str(status).upper()
            if normalized not in {"CREATED", "ACTIVE", *TERMINAL_STATUSES}:
                raise AcceptanceRejected("验收状态筛选值无效。")
            filters["status"] = normalized
        return self.db.list("acceptance_runs", filters, limit=max(1, min(int(limit), 1000)), order="created_at")

    def active_for_camera(self, camera_id: str) -> dict[str, Any] | None:
        rows = self.db.list("acceptance_runs", {"camera_id": str(camera_id), "status": "ACTIVE"}, limit=2)
        return rows[0] if rows else None

    def preflight(self, camera_id: str, engine_health: dict[str, Any] | None) -> dict[str, Any]:
        self._require_real()
        camera = self.db.get("cameras", str(camera_id), unscoped=True)
        health = dict(engine_health or {})
        source_type, simulated = self.provenance_resolver(camera) if camera else ("unknown", True)
        session_id = str(health.get("source_session_id") or "")
        session = self.db.get("source_sessions", session_id, unscoped=True) if session_id else None
        frame_sequence = int(health.get("source_frame_sequence") or health.get("frame_sequence") or 0)
        sampled_at = time.monotonic()
        with self._track_lock:
            previous_sample = self._preflight_samples.get(str(camera_id))
            self._preflight_samples[str(camera_id)] = (sampled_at, session_id, frame_sequence)
        frame_advancing = bool(
            previous_sample
            and previous_sample[1] == session_id
            and frame_sequence > previous_sample[2]
            and sampled_at - previous_sample[0] <= 10.0
        )
        last_frame_at = None
        try:
            last_frame_at = datetime.fromisoformat(str((session or {}).get("last_frame_at") or "").replace("Z", "+00:00"))
            if last_frame_at.tzinfo is None:
                last_frame_at = last_frame_at.replace(tzinfo=timezone.utc)
            frame_age = max(0.0, (datetime.now(timezone.utc) - last_frame_at.astimezone(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            frame_age = float("inf")
        configured_fps = float((camera or {}).get("inference_fps") or health.get("fps") or 1.0)
        freshness_limit = max(2.0, min(5.0, 3.0 / max(configured_fps, 0.1)))
        # An advancing server-owned frame counter is the strongest freshness
        # signal.  A freshly persisted source-session timestamp is sufficient
        # only for the first sample; start_acceptance takes a second sample and
        # explicitly requires advancement before arming.
        fresh_frame = frame_advancing or frame_age <= freshness_limit
        checks = {
            "runtime_real": self.mode is RuntimeMode.REAL,
            "camera_exists": camera is not None,
            "physical_opencv_camera": source_type == "opencv_camera" and simulated is False,
            "engine_running": bool(engine_health) and health.get("status") in {"ready", "running", "online"},
            "frame_received": frame_sequence > 0,
            "fresh_frame": fresh_frame,
            "frame_sequence_advancing": frame_advancing,
            "source_session_current": bool(
                session and session.get("camera_id") == str(camera_id)
                and str(session.get("runtime_mode") or "").upper() == "REAL"
                and session.get("source_type") == "opencv_camera"
                and session.get("is_simulated") is False
                and session.get("status") == "streaming"
                and session.get("continuity_ok") is True
            ),
        }
        return {
            "ready": all(checks.values()), "checks": checks,
            "camera_id": str(camera_id), "source_type": source_type,
            "is_simulated": simulated, "source_session_id": session_id or None,
            "fps": float(health.get("fps") or 0),
            "frame_sequence": frame_sequence,
            "frame_age_seconds": None if not math.isfinite(frame_age) else round(frame_age, 6),
            "freshness_limit_seconds": freshness_limit,
            "reconnect_epoch": int(health.get("reconnect_epoch") or 0),
            "health_status": health.get("status") or "stopped",
        }

    def activate(self, validation_run_id: str, preflight: dict[str, Any]) -> dict[str, Any]:
        pending_run = self.require(validation_run_id)
        self.require_camera_unchanged(self.require_suite(str(pending_run.get("suite_id") or "")))
        if not preflight.get("ready"):
            raise AcceptanceRejected("摄像头真实性自检未全部通过，不能开始验收。")
        stamp = now()

        def operation() -> dict[str, Any]:
            with self.db.connect() as connection:
                run = connection.execute("SELECT * FROM acceptance_runs WHERE id=?", (str(validation_run_id),)).fetchone()
                if not run:
                    raise AcceptanceRejected("验收任务不存在。")
                if run["status"] != "CREATED":
                    raise AcceptanceRejected("只有尚未开始的验收任务可以启动。")
                if run["camera_id"] != preflight.get("camera_id"):
                    raise AcceptanceRejected("验收任务与当前摄像头不一致。")
                conflict = connection.execute(
                    "SELECT id FROM acceptance_runs WHERE status='ACTIVE' AND (camera_id=? OR item_id=?) AND id<>? LIMIT 1",
                    (run["camera_id"], run["item_id"], run["id"]),
                ).fetchone()
                if conflict:
                    raise AcceptanceRejected("同一摄像头或物品已有正在进行的验收。")
                changed = connection.execute(
                    "UPDATE acceptance_runs SET status='ACTIVE',source_session_id=?,reconnect_epoch=?,"
                    "started_at=?,updated_at=? WHERE id=? AND status='CREATED'",
                    (
                        preflight["source_session_id"], int(preflight.get("reconnect_epoch") or 0),
                        stamp, stamp, run["id"],
                    ),
                ).rowcount
                if changed != 1:
                    raise AcceptanceRejected("验收任务状态已变化，请重新创建。")
                row = connection.execute("SELECT * FROM acceptance_runs WHERE id=?", (run["id"],)).fetchone()
                return self.db._decode("acceptance_runs", row)

        return self.db._run_retry(operation)

    @staticmethod
    def arm_config(run: dict[str, Any]) -> dict[str, Any]:
        return {
            "validation_run_id": run["validation_run_id"],
            "item_id": run["item_id"], "aruco_id": int(run["aruco_id"]),
            "camera_id": run["camera_id"], "source_session_id": run["source_session_id"],
            "reconnect_epoch": int(run.get("reconnect_epoch") or 0),
            "trial_kind": run.get("trial_kind") or "movement",
            "minimum_duration_seconds": float(run.get("minimum_duration_seconds") or 0),
            "scenario_index": int(run.get("scenario_index") or 1),
            "origin_zone": run["origin_zone"], "destination_zone": run["destination_zone"],
            "thresholds": run["thresholds"],
        }

    def cancel(self, validation_run_id: str, reason: str, *, status: str = "CANCELLED") -> dict[str, Any]:
        if status not in {"CANCELLED", "CAMERA_INTERRUPTED", "FAILED"}:
            raise AcceptanceRejected("验收取消状态无效。")
        reason = str(reason or "cancelled")[:500]
        stamp = now()
        with self.db.connect() as connection:
            run = connection.execute("SELECT * FROM acceptance_runs WHERE id=?", (str(validation_run_id),)).fetchone()
            if not run:
                raise AcceptanceRejected("验收任务不存在。")
            if run["status"] in TERMINAL_STATUSES:
                return self.db._decode("acceptance_runs", run)
            outcome = "INTERRUPTED" if status == "CAMERA_INTERRUPTED" else status
            event_count = int(connection.execute(
                "SELECT COUNT(*) FROM movement_events WHERE runtime_mode=? AND item_id=?",
                (self.mode.value, run["item_id"]),
            ).fetchone()[0])
            connection.execute(
                "UPDATE acceptance_runs SET status=?,outcome=?,failure_reason=?,cancel_reason=?,"
                "ended_at=?,event_count_after=?,updated_at=? WHERE id=? AND status IN ('CREATED','ACTIVE')",
                (
                    status, outcome, reason, reason, stamp,
                    event_count, stamp, run["id"],
                ),
            )
        return self.require(validation_run_id)

    def marker_status(
        self,
        *,
        item_id: str,
        camera_id: str,
        engine_health: dict[str, Any] | None,
        validation_run_id: str | None = None,
        engine_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return only server-observed marker facts, including before a run."""
        self._require_real()
        item = self.db.get("items", str(item_id), unscoped=True)
        camera = self.db.get("cameras", str(camera_id), unscoped=True)
        if not item or not camera:
            raise AcceptanceRejected("验收物品或摄像头不存在。")
        source_type, simulated = self.provenance_resolver(camera)
        health = dict(engine_health or {})
        with self._track_lock:
            cached = self._latest_tracks.get((camera["id"], item["id"]))
        cached_age = time.monotonic() - cached[0] if cached else float("inf")
        track = dict(cached[1]) if cached and cached_age <= 3.0 else {}
        current_session = str(health.get("source_session_id") or "")
        missing = str(track.get("state") or "").upper() in {"OCCLUDED", "LOST", "MISSING", "EXITED_VIEW"}
        recognized = bool(
            source_type == "opencv_camera" and not simulated
            and health.get("status") in {"ready", "running", "online", "streaming"}
            and current_session and track.get("source_session_id") == current_session
            and track.get("item_id") == item["id"]
            and track.get("detector_backend") == "aruco"
            and not missing
        )
        status = dict(engine_status or {})
        if validation_run_id:
            run = self.require(validation_run_id)
            if run.get("item_id") != item["id"] or run.get("camera_id") != camera["id"]:
                raise AcceptanceRejected("验收任务与所查物品或摄像头不一致。")
            # Cumulative detection counts describe this run's history, not a
            # currently visible marker. A lost-frame status must not revive an
            # old bbox merely because the marker appeared earlier in the run.
            if str(status.get("marker_status") or "").upper() in {"LOST", "OCCLUDED", "CAMERA_INTERRUPTED"}:
                recognized = False
        bbox = track.get("bbox") if isinstance(track.get("bbox"), list) else None
        marker_size = None
        if bbox and len(bbox) == 4:
            marker_size = {"width": float(bbox[2]), "height": float(bbox[3]), "minimum": min(float(bbox[2]), float(bbox[3]))}
        diagnostics = track.get("state_diagnostics") if isinstance(track.get("state_diagnostics"), dict) else {}
        return {
            "recognized": recognized,
            "item_id": item["id"], "camera_id": camera["id"], "aruco_id": item.get("aruco_id"),
            "stability": {
                "state": status.get("state") or track.get("state"),
                "stable_remaining_seconds": status.get("stable_remaining_seconds"),
                "speed": track.get("speed"),
                "diagnostics": diagnostics,
            },
            "marker_size": status.get("marker_size_px") or marker_size,
            "corners": status.get("corners_norm") or track.get("corners"),
            "frame": status.get("frame_sequence") or track.get("source_frame"),
            "source_session_id": status.get("source_session_id") or track.get("source_session_id") or current_session or None,
            "reconnect_epoch": status.get("reconnect_epoch", health.get("reconnect_epoch", track.get("reconnect_epoch", 0))),
            "last_observed_at": track.get("last_seen") or status.get("frame_timestamp") or track.get("source_timestamp"),
            "current_frame_at": track.get("source_timestamp") or status.get("updated_at"),
            "server_observed": True,
            "runtime_mode": "REAL", "source_type": source_type, "is_simulated": simulated,
        }

    def sync_engine_status(self, validation_run_id: str, status: dict[str, Any] | None) -> dict[str, Any]:
        """Mirror trusted engine progress; only negative trials may pass here."""
        run = self.require(validation_run_id)
        if run.get("status") == "ACTIVE":
            try:
                self.require_camera_unchanged(self.require_suite(str(run.get("suite_id") or "")))
            except AcceptanceRejected:
                return self.cancel(validation_run_id, "camera_configuration_changed", status="CAMERA_INTERRUPTED")
        if not isinstance(status, dict):
            return run
        identity_ok = (
            status.get("validation_run_id") == run.get("validation_run_id")
            and status.get("item_id") == run.get("item_id")
            and status.get("camera_id") == run.get("camera_id")
            and status.get("source_session_id") == run.get("source_session_id")
            and int(status.get("reconnect_epoch") or 0) == int(run.get("reconnect_epoch") or 0)
        )
        if not identity_ok:
            if run.get("status") == "ACTIVE":
                return self.cancel(validation_run_id, "engine_acceptance_identity_changed", status="CAMERA_INTERRUPTED")
            return run
        marker = {
            "state": status.get("marker_status") or "not_seen",
            "seen": int(status.get("detected_frames") or 0) > 0,
            "last_seen_at": status.get("frame_timestamp"),
            "source_frame": status.get("frame_sequence"),
            "center": status.get("center_norm"),
            "corners": status.get("corners_norm"),
            "marker_size": status.get("marker_size_px"),
            "zone": status.get("current_zone"),
        }
        stamp = now()
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE acceptance_runs SET engine_status=?,marker_status=?,"
                "user_executed=CASE WHEN ? THEN 1 ELSE user_executed END,updated_at=? "
                "WHERE id=? AND status='ACTIVE'",
                (
                    json.dumps(status, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(marker, ensure_ascii=False, separators=(",", ":")),
                    1 if marker["seen"] else 0, stamp, run["id"],
                ),
            )
        run = self.require(validation_run_id)
        if run.get("status") != "ACTIVE":
            return run
        if run.get("trial_kind") == "movement":
            confirmed_pending = (
                str(status.get("state") or "") == "MOVEMENT_CONFIRMED"
                or str(status.get("marker_status") or "") == "CONFIRMED_PENDING_POSTROLL"
            )
            if status.get("terminal") and not confirmed_pending and not run.get("event_id"):
                return self.cancel(validation_run_id, str(status.get("reason") or "movement_acceptance_failed"), status="FAILED")
            return run
        if run.get("trial_kind") == "disconnect_reconnect":
            progress = dict(run.get("progress") or {})
            baseline = status.get("baseline") if isinstance(status.get("baseline"), dict) else {}
            calibration = status.get("calibration_snapshot") if isinstance(status.get("calibration_snapshot"), dict) else {}
            thresholds = run.get("thresholds") or {}
            current_zone = status.get("current_zone") if isinstance(status.get("current_zone"), dict) else {}
            stable_baseline_id = status.get("baseline_id") or baseline.get("frame_sequence")
            stable_enough = (
                str(status.get("state") or "") == "STABLE_AT_ORIGIN"
                and str(status.get("marker_status") or "") == "STABLE_AT_ORIGIN"
                and stable_baseline_id is not None
                and float(baseline.get("stable_duration_seconds") or 0) >= float(thresholds.get("min_stable_seconds") or 0)
                and int(calibration.get("sample_count") or 0) >= int(thresholds.get("origin_stable_frames") or 0)
                and int(status.get("detected_frames") or 0) >= int(thresholds.get("origin_stable_frames") or 0)
                and int(status.get("event_count") or 0) == 0
                and current_zone.get("id") == run.get("origin_zone_id")
                and float(status.get("observed_duration_seconds") or 0) >= float(run.get("minimum_duration_seconds") or 0)
            )
            provenance_ok = (
                progress.get("controlled_disconnect") is True
                and progress.get("controlled_reconnect") is True
                and progress.get("disconnected_session_id")
                and progress.get("reconnected_session_id") == run.get("source_session_id")
                and progress.get("disconnected_session_id") != progress.get("reconnected_session_id")
            )
            if stable_enough and provenance_ok and run.get("user_executed"):
                return self._pass_without_event(
                    run,
                    "controlled_disconnect_reconnect_reestablished_stable_origin_without_event",
                    status,
                )
            if status.get("terminal") and not stable_enough:
                return self.cancel(validation_run_id, str(status.get("reason") or "reconnect_baseline_not_reestablished"), status="FAILED")
            return run
        if status.get("terminal") and not status.get("negative_sample_eligible"):
            return self.cancel(validation_run_id, str(status.get("reason") or "negative_trial_failed"), status="FAILED")
        if not status.get("trial_complete") or not status.get("negative_sample_eligible") or not run.get("user_executed"):
            return run
        if float(status.get("observed_duration_seconds") or 0) < float(run.get("minimum_duration_seconds") or 0):
            return run
        return self._pass_without_event(run, str(status.get("completion_outcome") or "negative_trial_complete"), status)

    def _pass_without_event(self, run: dict[str, Any], reason: str, result: dict[str, Any]) -> dict[str, Any]:
        stamp = now()
        with self.db.connect() as connection:
            current_count = int(connection.execute(
                "SELECT COUNT(*) FROM movement_events WHERE runtime_mode='REAL' AND item_id=?",
                (run["item_id"],),
            ).fetchone()[0])
            if current_count != int(run.get("event_count_before") or 0):
                connection.execute(
                    "UPDATE acceptance_runs SET status='FAILED',outcome='FAILED',failure_reason=?,"
                    "event_count_after=?,ended_at=?,updated_at=? WHERE id=? AND status='ACTIVE'",
                    ("negative_trial_event_count_changed", current_count, stamp, stamp, run["id"]),
                )
                return self.db._decode(
                    "acceptance_runs",
                    connection.execute("SELECT * FROM acceptance_runs WHERE id=?", (run["id"],)).fetchone(),
                )
            changed = connection.execute(
                "UPDATE acceptance_runs SET status='PASSED',outcome='PASSED',failure_reason=NULL,"
                "event_count_after=?,confirmation_latency_ms=MAX(0,(julianday(?)-julianday(started_at))*86400000.0),"
                "result=?,ended_at=?,updated_at=? WHERE id=? AND status='ACTIVE' AND user_executed=1 AND event_id IS NULL",
                (
                    current_count, stamp,
                    json.dumps({"completed_without_event": True, "reason": reason, "engine_status": result}, ensure_ascii=False, separators=(",", ":")),
                    stamp, stamp, run["id"],
                ),
            ).rowcount
            if changed != 1:
                raise AcceptanceRejected("负样本验收状态已变化。")
        return self.require(run["id"])

    def begin_disconnect_reconnect(self, validation_run_id: str) -> dict[str, Any]:
        run = self.require(validation_run_id)
        if run.get("status") != "ACTIVE" or run.get("trial_kind") != "disconnect_reconnect":
            raise AcceptanceRejected("只有进行中的断开重连负样本可以执行受控重连。")
        progress = {**(run.get("progress") or {}), "phase": "disconnecting", "controlled_disconnect": True, "disconnected_session_id": run.get("source_session_id"), "disconnected_at": now()}
        return self.db.save("acceptance_runs", {"progress": progress, "engine_status": None}, run["id"])

    def rebind_after_reconnect(self, validation_run_id: str, preflight: dict[str, Any]) -> dict[str, Any]:
        run = self.require(validation_run_id)
        self.require_camera_unchanged(self.require_suite(str(run.get("suite_id") or "")))
        progress = dict(run.get("progress") or {})
        if run.get("status") != "ACTIVE" or run.get("trial_kind") != "disconnect_reconnect" or not progress.get("controlled_disconnect"):
            raise AcceptanceRejected("断开重连任务状态无效。")
        if not preflight.get("ready") or preflight.get("source_session_id") == progress.get("disconnected_session_id"):
            raise AcceptanceRejected("摄像头没有建立新的连续来源会话。")
        stamp = now()
        progress.update({"phase": "reconnected_waiting_marker", "controlled_reconnect": True, "reconnected_at": stamp, "reconnected_session_id": preflight.get("source_session_id")})
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE acceptance_runs SET source_session_id=?,reconnect_epoch=?,user_executed=0,"
                "marker_status=?,engine_status=NULL,progress=?,updated_at=? WHERE id=? AND status='ACTIVE'",
                (
                    preflight["source_session_id"], int(preflight.get("reconnect_epoch") or 0),
                    json.dumps({"state": "waiting_after_reconnect", "seen": False}, separators=(",", ":")),
                    json.dumps(progress, ensure_ascii=False, separators=(",", ":")), stamp, run["id"],
                ),
            )
        return self.require(run["id"])

    def observe_track(self, track: dict[str, Any]) -> dict[str, Any] | None:
        """Mark user execution only from the authenticated server vision callback."""
        camera_id = str(track.get("camera_id") or "")
        item_id = str(track.get("item_id") or "")
        trusted_track = (
            str(track.get("runtime_mode") or "").upper() == "REAL"
            and track.get("source_type") == "opencv_camera"
            and not bool(track.get("is_simulated"))
            and str(track.get("detector_backend") or "").lower() == "aruco"
        )
        if trusted_track and camera_id and item_id:
            with self._track_lock:
                self._latest_tracks[(camera_id, item_id)] = (time.monotonic(), dict(track))
        run = self.active_for_camera(camera_id)
        if not run:
            return None
        try:
            self.require_camera_unchanged(self.require_suite(str(run.get("suite_id") or "")))
        except AcceptanceRejected:
            interrupted = self.cancel(run["id"], "camera_configuration_changed", status="CAMERA_INTERRUPTED")
            return {"interrupted": True, "run": interrupted}
        source_session_id = str(track.get("source_session_id") or "")
        reconnect_epoch = int(track.get("reconnect_epoch") or 0)
        if source_session_id != run.get("source_session_id") or reconnect_epoch != int(run.get("reconnect_epoch") or 0):
            interrupted = self.cancel(run["id"], "source_session_changed", status="CAMERA_INTERRUPTED")
            return {"interrupted": True, "run": interrupted}
        if (
            not trusted_track
            or item_id != run.get("item_id")
        ):
            return {"interrupted": False, "run": run}
        missing = str(track.get("state") or "").upper() in {"OCCLUDED", "LOST", "MISSING", "EXITED_VIEW"}
        marker_status = {
            "state": str(track.get("state")).lower() if missing else "seen", "seen": not missing,
            "last_seen_at": track.get("last_seen") or track.get("source_timestamp") or now(),
            "source_frame": track.get("source_frame"), "center": track.get("center"),
            "corners": track.get("corners"), "zone_id": track.get("zone_id"),
            "zone_name": track.get("zone_name"), "confidence": track.get("confidence"),
            "track_id": track.get("track_id"), "machine_state": track.get("state"),
        }
        stamp = now()
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE acceptance_runs SET user_executed=1,marker_status=?,updated_at=? "
                "WHERE id=? AND status='ACTIVE' AND source_session_id=?",
                (json.dumps(marker_status, ensure_ascii=False, separators=(",", ":")), stamp, run["id"], source_session_id),
            )
        return {"interrupted": False, "run": self.require(run["id"])}
