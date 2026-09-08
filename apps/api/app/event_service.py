"""Authenticity gate and idempotent movement-event persistence."""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import cv2
import numpy as np
from PIL import Image
from services.vision.release_evidence import verified_release

from .db import Database, now
from .runtime_mode import RuntimeMode, provenance_for_camera, validate_source_for_mode


CONFIRMED_TYPES = {"movement", "manual_correction"}
ALLOWED_DETECTOR_BACKENDS = {"aruco", "nanodet", "dinov2_reference_patches"}
ALLOWED_TRACKER_BACKENDS = {"stable_identity", "aruco_acceptance_state_machine"}
ALLOWED_DETECTION_MODES = {"aruco", "experimental", "aruco_screen_validation"}
UNATTESTED_REAL_STREAMS = {"browser_camera", "rtsp", "onvif", "mjpeg", "esp32_unverified"}
SAFE_EVENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_ENCODED_KEYFRAME_MAE = 16.0
MAX_ENCODED_FRAME_MAE = 18.0
MAX_ENCODED_MARKER_POSITION_ERROR_PX = 3.0
MIN_ACCEPTANCE_FRAME_RATE_RATIO = 0.70


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _evidence_timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    return _parse_timestamp(value)


def _point_in_polygon(point: tuple[float, float], polygon: list[list[float]]) -> bool:
    """Return whether one normalized point lies inside/on a frozen zone."""
    x, y = point
    inside = False
    for index, first in enumerate(polygon):
        second = polygon[(index + 1) % len(polygon)]
        x1, y1 = float(first[0]), float(first[1])
        x2, y2 = float(second[0]), float(second[1])
        cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
        if (
            abs(cross) <= 1e-9
            and min(x1, x2) - 1e-9 <= x <= max(x1, x2) + 1e-9
            and min(y1, y2) - 1e-9 <= y <= max(y1, y2) + 1e-9
        ):
            return True
        if (y1 > y) != (y2 > y):
            intersection_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < intersection_x:
                inside = not inside
    return inside


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class EvidenceRejected(ValueError):
    """A candidate did not meet the confirmed-event evidence contract."""


class EventService:
    def __init__(self, db: Database, mode: RuntimeMode, media_root: Path, retention=None, provenance_resolver=None, acceptance=None):
        self.db = db
        self.mode = mode
        self.media_root = media_root.resolve()
        self.retention = retention
        self.provenance_resolver = provenance_resolver
        self.acceptance = acceptance
        self._observation_lock = threading.RLock()
        (self.media_root / "event-images").mkdir(parents=True, exist_ok=True)
        (self.media_root / "event-clips").mkdir(parents=True, exist_ok=True)

    def media_path(self, url: str | None, kind: str | None = None) -> Path | None:
        if not url or not isinstance(url, str) or not url.startswith("/media/"):
            return None
        relative = Path(url.removeprefix("/media/"))
        if relative.is_absolute() or ".." in relative.parts:
            return None
        path = (self.media_root / relative).resolve()
        if not path.is_relative_to(self.media_root):
            return None
        if kind == "image" and path.parent != (self.media_root / "event-images").resolve():
            return None
        if kind == "clip" and path.parent != (self.media_root / "event-clips").resolve():
            return None
        return path

    def _atomic_image(self, event_id: str, frame) -> str | None:
        if frame is None:
            return None
        ok, encoded = cv2.imencode(".jpg", frame)
        if not ok:
            return None
        target = self.media_root / "event-images" / f"{event_id}.jpg"
        # Keep the temporary basename bounded under Windows' path limit.
        # Repeating a long event id here made a valid final path unwritable.
        temporary = target.parent / f".{uuid4().hex}.tmp.jpg"
        try:
            temporary.write_bytes(encoded.tobytes())
            os.replace(temporary, target)
            return f"/media/event-images/{target.name}"
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _frame_sha256(frame) -> str:
        value = np.ascontiguousarray(frame)
        digest = hashlib.sha256()
        digest.update(str(value.dtype).encode("ascii"))
        digest.update(json.dumps(list(value.shape), separators=(",", ":")).encode("ascii"))
        digest.update(value.tobytes())
        return digest.hexdigest()

    def _minimum_confidence(self) -> float:
        row = self.db.get("settings", "main", unscoped=True) or {}
        try:
            return max(0.0, min(1.0, float((row.get("value") or {}).get("min_confidence", 0.55))))
        except (TypeError, ValueError):
            return 0.55

    def _source(self, camera: dict[str, Any], event: dict[str, Any]) -> tuple[str, bool]:
        try:
            if self.provenance_resolver is not None:
                expected_type, expected_simulated = self.provenance_resolver(camera)
                expected_simulated = bool(expected_simulated) or self.mode is not RuntimeMode.REAL
            elif camera.get("source_type") == "esp32":
                # A camera JSON row is never proof of a physical ESP32.  The
                # normal Runtime injects its firmware-registry resolver; a
                # standalone EventService must fail closed when it cannot query
                # that authoritative identity boundary.
                _generic_type, generic_simulated = provenance_for_camera(camera)
                expected_type = "virtual_esp32" if generic_simulated else "esp32_unverified"
                expected_simulated = bool(generic_simulated) or self.mode is not RuntimeMode.REAL
            else:
                expected_type, expected_simulated = validate_source_for_mode(self.mode, camera)
        except ValueError as exc:
            raise EvidenceRejected(str(exc)) from exc
        supplied_type = str(event.get("source_type") or expected_type)
        supplied_simulated = bool(event.get("is_simulated", expected_simulated))
        if supplied_type != expected_type or supplied_simulated != expected_simulated:
            raise EvidenceRejected("事件来源与摄像头来源不一致。")
        if str(event.get("runtime_mode") or self.mode.value).upper() != self.mode.value:
            raise EvidenceRejected("事件运行模式与当前进程不一致。")
        return expected_type, expected_simulated

    def _validate_physical_acceptance(
        self,
        event: dict[str, Any],
        item: dict[str, Any],
        camera: dict[str, Any],
        source_type: str,
        simulated: bool,
    ) -> dict[str, Any] | None:
        """Apply the stricter server-armed contract to P0 validation events.

        Ordinary REAL camera/attested-device episodes continue through the
        existing evidence and provenance gate.  The dedicated screen-marker
        validation mode is the only path that may claim ``validation_run_id``
        and its independent before/after evidence bundle.
        """
        if self.mode is not RuntimeMode.REAL:
            return None
        acceptance_mode = str(event.get("detection_mode") or "").strip().lower() == "aruco_screen_validation"
        if not acceptance_mode:
            if event.get("validation_run_id"):
                raise EvidenceRejected("只有真实屏幕标记验收事件可以声明 validation_run_id。")
            return None
        if source_type != "opencv_camera" or simulated:
            raise EvidenceRejected("真实屏幕标记验收只接受当前本机 OpenCV 来源。")
        if str(event.get("detector_backend") or "").strip().lower() != "aruco":
            raise EvidenceRejected("真实物品验收必须由 ArUco 检测器产生。")
        if str(event.get("tracker_backend") or "").strip().lower() != "aruco_acceptance_state_machine":
            raise EvidenceRejected("真实物品验收必须由专用验收状态机产生。")
        validation_run_id = str(event.get("validation_run_id") or "").strip()
        if not validation_run_id:
            raise EvidenceRejected("REAL 自动移动缺少 validation_run_id。")
        run = self.db.get("acceptance_runs", validation_run_id)
        if not run or run.get("status") != "ACTIVE" or not run.get("user_executed"):
            raise EvidenceRejected("真实物品验收任务未启动、未检测到目标标记或已经结束。")
        if self.acceptance is None:
            raise EvidenceRejected("验收事件缺少服务端套件合同校验器。")
        try:
            self.acceptance.require_camera_unchanged(self.acceptance.require_suite(run["suite_id"]))
        except ValueError as exc:
            raise EvidenceRejected(str(exc)) from exc
        if run.get("trial_kind") != "movement":
            raise EvidenceRejected("当前负样本场景不允许生成确认移动事件。")
        if (
            str(event.get("trial_kind") or "").strip().lower() != "movement"
            or isinstance(event.get("scenario_index"), bool)
            or int(event.get("scenario_index") or 0) != int(run.get("scenario_index") or 0)
        ):
            raise EvidenceRejected("事件场景序号或类型与验收套件合同不一致。")
        expected = {
            "runtime_mode": "REAL", "source_type": "opencv_camera", "is_simulated": False,
            "item_id": item["id"], "camera_id": camera["id"],
            "source_session_id": str(event.get("source_session_id") or ""),
            "detection_mode": "aruco_screen_validation",
        }
        if any(run.get(key) != value for key, value in expected.items()):
            raise EvidenceRejected("验收任务与事件的物品、摄像头或来源会话不一致。")
        marker_id = item.get("aruco_id")
        if marker_id is None or int(run.get("aruco_id")) != int(marker_id):
            raise EvidenceRejected("验收任务的 ArUco 标记绑定已变化。")
        supplied_marker = event.get("aruco_id")
        if supplied_marker is not None and int(supplied_marker) != int(marker_id):
            raise EvidenceRejected("事件中的 ArUco 标记与验收物品不一致。")
        event["aruco_id"] = int(marker_id)
        if (
            str(event.get("from_zone") or "") != str(run.get("expected_from_zone") or "")
            or str(event.get("to_zone") or "") != str(run.get("expected_to_zone") or "")
            or str(event.get("from_zone_id") or "") != str(run.get("origin_zone_id") or "")
            or str(event.get("to_zone_id") or "") != str(run.get("destination_zone_id") or "")
            or str(event.get("zone_id") or "") != str(run.get("destination_zone_id") or "")
        ):
            raise EvidenceRejected("事件起点或终点与验收任务划定区域不一致。")
        if event.get("origin_zone") != run.get("origin_zone") or event.get("destination_zone") != run.get("destination_zone"):
            raise EvidenceRejected("事件区域快照与验收套件冻结区域不一致。")
        if int(event.get("reconnect_epoch") or 0) != int(run.get("reconnect_epoch") or 0):
            raise EvidenceRejected("摄像头代次已变化，必须重新建立验收基线。")
        trajectory = event.get("trajectory")
        if not isinstance(trajectory, list) or len(trajectory) < 3:
            raise EvidenceRejected("真实物品验收缺少完整轨迹。")
        frame_start, frame_end = event.get("source_frame_start"), event.get("source_frame_end")
        started = _parse_timestamp(event.get("source_timestamp_start") or event.get("timestamp_start"))
        ended = _parse_timestamp(event.get("source_timestamp_end") or event.get("timestamp_end"))
        if not isinstance(frame_start, int) or not isinstance(frame_end, int) or not started or not ended:
            raise EvidenceRejected("真实物品验收的来源帧或时间范围无效。")
        prior_frame: int | None = None
        prior_time: datetime | None = None
        centers: list[tuple[float, float]] = []
        normalized_trajectory: list[dict[str, Any]] = []
        origin_zone = run.get("origin_zone") if isinstance(run.get("origin_zone"), dict) else {}
        destination_zone = run.get("destination_zone") if isinstance(run.get("destination_zone"), dict) else {}
        origin_points = origin_zone.get("points") if isinstance(origin_zone.get("points"), list) else []
        destination_points = destination_zone.get("points") if isinstance(destination_zone.get("points"), list) else []
        if len(origin_points) < 3 or len(destination_points) < 3:
            raise EvidenceRejected("验收套件缺少可复核的冻结区域多边形。")
        for point in trajectory:
            if not isinstance(point, dict):
                raise EvidenceRejected("真实物品轨迹格式无效。")
            frame_index = point.get("source_frame") if point.get("source_frame") is not None else point.get("frame_sequence")
            timestamp = _evidence_timestamp(
                point.get("source_timestamp") or point.get("frame_timestamp") or point.get("timestamp")
            )
            center = point.get("center_norm") if point.get("center_norm") is not None else point.get("center")
            if (
                not isinstance(frame_index, int) or not timestamp
                or not isinstance(center, (list, tuple)) or len(center) != 2
            ):
                raise EvidenceRejected("真实物品轨迹缺少帧号、时间或中心点。")
            try:
                normalized_center = (float(center[0]), float(center[1]))
            except (TypeError, ValueError):
                raise EvidenceRejected("真实物品轨迹中心点无效。") from None
            if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in normalized_center):
                raise EvidenceRejected("真实物品轨迹中心点不在归一化画面内。")
            if prior_frame is not None and frame_index < prior_frame:
                raise EvidenceRejected("真实物品轨迹帧号不是按时间递增。")
            if prior_time is not None and timestamp < prior_time:
                raise EvidenceRejected("真实物品轨迹时间不是按顺序递增。")
            if not frame_start <= frame_index <= frame_end or not started <= timestamp <= ended:
                raise EvidenceRejected("真实物品轨迹点超出事件来源范围。")
            prior_frame, prior_time = frame_index, timestamp
            centers.append(normalized_center)
            zone = point.get("zone") if isinstance(point.get("zone"), dict) else {}
            in_origin = _point_in_polygon(normalized_center, origin_points)
            in_destination = _point_in_polygon(normalized_center, destination_points)
            if in_origin and in_destination:
                raise EvidenceRejected("轨迹点同时落入两个验收区域，区域合同无效。")
            calculated_zone = origin_zone if in_origin else destination_zone if in_destination else {}
            supplied_zone_id = str(point.get("zone_id") or zone.get("id") or "")
            supplied_zone_name = str(point.get("zone_name") or zone.get("name") or "")
            if supplied_zone_id and supplied_zone_id != str(calculated_zone.get("id") or ""):
                raise EvidenceRejected("轨迹声明区域与冻结多边形复算结果不一致。")
            if supplied_zone_name and supplied_zone_name != str(calculated_zone.get("name") or ""):
                raise EvidenceRejected("轨迹声明区域名称与冻结多边形复算结果不一致。")
            normalized_trajectory.append({
                **point,
                "source_frame": frame_index,
                "source_timestamp": timestamp.astimezone(timezone.utc).isoformat(),
                "center": [normalized_center[0], normalized_center[1]],
                "center_norm": [normalized_center[0], normalized_center[1]],
                "zone_name": str(calculated_zone.get("name") or ""),
                "zone_id": str(calculated_zone.get("id") or ""),
            })
        if normalized_trajectory[0]["zone_id"] != run.get("origin_zone_id"):
            raise EvidenceRejected("轨迹首个稳定点不在验收起点区域。")
        if normalized_trajectory[-1]["zone_id"] != run.get("destination_zone_id"):
            raise EvidenceRejected("轨迹最后稳定点不在验收终点区域。")
        for field, expected_center in (
            ("from_position", centers[0]),
            ("to_position", centers[-1]),
            ("final_position", centers[-1]),
        ):
            supplied_center = event.get(field)
            # Origin is a mean of the stable baseline, while trajectory[0]
            # records its last actual frame. Permit only configured baseline
            # jitter, not an arbitrary/declared endpoint in another zone.
            tolerance = (
                float((run.get("thresholds") or {}).get("stable_jitter_norm", 0.025))
                if field == "from_position" else 1e-6
            )
            try:
                valid_center = (
                    isinstance(supplied_center, (list, tuple)) and len(supplied_center) == 2
                    and all(math.isfinite(float(value)) for value in supplied_center)
                    and math.dist(tuple(float(value) for value in supplied_center), expected_center) <= tolerance
                )
            except (TypeError, ValueError, OverflowError):
                valid_center = False
            if (
                not valid_center
            ):
                raise EvidenceRejected(f"{field} 与轨迹冻结端点不一致。")
        path_length = sum(math.dist(centers[index - 1], centers[index]) for index in range(1, len(centers)))
        threshold = float((run.get("thresholds") or {}).get("min_trajectory_length_norm", 0.18))
        if path_length < threshold:
            raise EvidenceRejected("真实物品轨迹长度未达到验收阈值。")
        event["trajectory"] = normalized_trajectory
        return run

    def _verified_manual_reference(self, event: dict[str, Any]) -> dict[str, Any]:
        """Resolve a correction chain to one original, non-manual movement.

        Corrections reuse the original evidence bytes.  Flattening the chain
        keeps online acceptance, offline auditing and retention dependencies on
        the same root event instead of creating a self-signed correction chain.
        """
        reference_id = str(event.get("manual_reference_event_id") or "").strip()
        if not reference_id:
            raise EvidenceRejected("人工纠正必须引用当前模式下已有的证据事件。")
        visited: set[str] = set()
        reference = None
        while reference_id:
            if reference_id in visited or len(visited) >= 32:
                raise EvidenceRejected("人工纠正证据链存在循环或异常深度。")
            visited.add(reference_id)
            reference = self.db.get("events", reference_id)
            if not reference or reference.get("evidence_status") not in {"confirmed", "media_expired"}:
                raise EvidenceRejected("人工纠正引用的原事件不存在或未经确认。")
            if str(reference.get("runtime_mode") or "").upper() != self.mode.value:
                raise EvidenceRejected("人工纠正不能跨运行模式引用事件。")
            if not (reference.get("manually_corrected") or reference.get("event_type") == "manual_correction"):
                break
            reference_id = str(reference.get("manual_reference_event_id") or "").strip()
            if not reference_id:
                rows = self.db.list("event_media", {"event_id": reference.get("event_id")}, limit=20)
                candidates = {
                    str(((row.get("metadata") or {}).get("write_binding") or {}).get("manual_reference_event_id") or "").strip()
                    for row in rows
                    if row.get("status") == "active"
                } - {""}
                if len(candidates) != 1:
                    raise EvidenceRejected("人工纠正证据链无法唯一解析到原始事件。")
                reference_id = next(iter(candidates))
        if not reference or reference.get("event_type") != "movement" or reference.get("manually_corrected"):
            raise EvidenceRejected("人工纠正必须最终引用一条原始确认移动事件。")
        rows = self.db.list("event_media", {"event_id": reference.get("event_id")}, limit=20)
        active = {(row.get("kind"), row.get("path"), row.get("sha256")) for row in rows if row.get("status") == "active"}
        image = event.get("after_screenshot") or event.get("screenshot_path")
        if not image or ("image", image, event.get("screenshot_sha256")) not in active:
            raise EvidenceRejected("人工纠正引用的截图没有活动媒体登记或哈希不一致。")
        clip = event.get("clip_path")
        if clip and ("clip", clip, event.get("clip_sha256")) not in active:
            raise EvidenceRejected("人工纠正引用的录像没有活动媒体登记或哈希不一致。")
        event["manual_reference_event_id"] = str(reference.get("event_id") or reference.get("id"))
        return reference

    def _validate_evidence(
        self,
        event: dict[str, Any],
        source_type: str,
        simulated: bool,
        *,
        manual: bool,
        write_binding: dict[str, Any] | None,
        raw_clip_frames: list[np.ndarray] | None = None,
    ) -> dict[str, Any]:
        movement_session = str(event.get("movement_session_id") or "").strip()
        source_session = str(event.get("source_session_id") or "").strip()
        frame_start, frame_end = event.get("source_frame_start"), event.get("source_frame_end")
        started = _parse_timestamp(event.get("source_timestamp_start") or event.get("timestamp_start") or event.get("started_at"))
        ended = _parse_timestamp(event.get("source_timestamp_end") or event.get("timestamp_end") or event.get("ended_at"))
        if not movement_session:
            raise EvidenceRejected("缺少 movement_session_id，候选事件不会写入历史。")
        manual_reference = self._verified_manual_reference(event) if manual else None
        if not manual:
            # A browser WebSocket can authenticate the administrator and validate
            # JPEG structure, but it cannot attest that bytes came from
            # getUserMedia instead of a file, canvas, or custom client.  Treat it
            # as an honest last-seen source only.  Otherwise a replayed demo
            # video can be promoted to a REAL confirmed movement event.
            if self.mode is RuntimeMode.REAL and source_type in UNATTESTED_REAL_STREAMS:
                raise EvidenceRejected(
                    "该视频流只能证明收到画面，不能证明来自物理摄像头；REAL 模式只更新最后看到，不创建确认移动事件。"
                )
            if not source_session:
                raise EvidenceRejected("缺少 source_session_id。")
            session = self.db.get("source_sessions", source_session, unscoped=True)
            if not session:
                raise EvidenceRejected("来源会话不存在，不能证明事件来自持续取流。")
            if (
                session.get("camera_id") != event.get("camera_id")
                or str(session.get("runtime_mode") or "").upper() != self.mode.value
                or session.get("source_type") != source_type
                or bool(session.get("is_simulated")) != simulated
            ):
                raise EvidenceRejected("事件与来源会话的摄像头、模式或来源不一致。")
            if session.get("continuity_ok") is not True or session.get("status") in {"error", "disconnected", "reconnecting"}:
                raise EvidenceRejected("来源会话断流或连续性未经确认。")
            if not isinstance(frame_start, int) or not isinstance(frame_end, int) or frame_start < 0 or frame_end <= frame_start:
                raise EvidenceRejected("来源帧范围无效。")
            if not started or not ended or ended <= started:
                raise EvidenceRejected("来源时间范围无效。")
            session_first,session_last=session.get("first_frame"),session.get("last_frame")
            if not isinstance(session_first,int) or not isinstance(session_last,int) or not session_first<=frame_start<=frame_end<=session_last:
                raise EvidenceRejected("事件帧范围不在来源会话已接收帧范围内。")
            session_started=_parse_timestamp(session.get("started_at"))
            session_last_at=_parse_timestamp(session.get("ended_at") or session.get("last_frame_at"))
            if not session_started or not session_last_at or not session_started<=started<=ended<=session_last_at:
                raise EvidenceRejected("事件时间范围不在来源会话持续时间内。")
            if not bool(event.get("source_continuity_ok", True)):
                raise EvidenceRejected("来源帧不连续。")
            if event.get("reconnect_epoch_changed"):
                raise EvidenceRejected("摄像头重连后必须重新建立稳定基线。")
            if not event.get("from_zone") or not event.get("to_zone"):
                raise EvidenceRejected("缺少移动起点或终点。")
            meaningful = event.get("from_zone") != event.get("to_zone") or bool(event.get("meaningful_position_change"))
            if not meaningful:
                raise EvidenceRejected("同一区域轻微变化不创建历史事件。")
            if not bool(event.get("stable_before", False)) or not bool(event.get("stable_after", False)):
                raise EvidenceRejected("物品没有在移动前后稳定出现。")
            try:
                confidence = float(event.get("confidence"))
            except (TypeError, ValueError):
                raise EvidenceRejected("确认移动缺少有效检测可信度。") from None
            if not 0.0 <= confidence <= 1.0 or confidence < self._minimum_confidence():
                raise EvidenceRejected("确认移动未达到配置的最低检测可信度。")
            if str(event.get("detector_backend") or "").strip().lower() not in ALLOWED_DETECTOR_BACKENDS:
                raise EvidenceRejected("确认移动缺少允许的检测器来源。")
            if str(event.get("tracker_backend") or "").strip().lower() not in ALLOWED_TRACKER_BACKENDS:
                raise EvidenceRejected("确认移动缺少允许的追踪器来源。")
            if str(event.get("detection_mode") or "").strip().lower() not in ALLOWED_DETECTION_MODES:
                raise EvidenceRejected("确认移动缺少允许的检测模式。")
            pickup=event.get("pickup_evidence")
            placement=event.get("placement_evidence")
            if not isinstance(pickup,dict) or not isinstance(placement,dict):
                raise EvidenceRejected("缺少拿起/放稳阶段的证据摘要。")
            pickup_frame=pickup.get("source_frame")
            placement_frame=placement.get("source_frame")
            if not isinstance(pickup_frame,int) or not isinstance(placement_frame,int) or not frame_start<=pickup_frame<=placement_frame<=frame_end:
                raise EvidenceRejected("拿起/放稳证据帧不在事件来源帧范围内。")
            pickup_time=_evidence_timestamp(pickup.get("source_timestamp"))
            placement_time=_evidence_timestamp(placement.get("source_timestamp"))
            if not pickup_time or not placement_time or not started<=pickup_time<=placement_time<=ended:
                raise EvidenceRejected("拿起/放稳证据时间不在事件来源时间范围内。")
        acceptance_mode = str(event.get("detection_mode") or "").strip().lower() == "aruco_screen_validation"
        after_url = event.get("after_screenshot") or event.get("screenshot_path")
        before_url = event.get("before_screenshot") or after_url
        clip_url = event.get("clip_path")
        after_path = self.media_path(after_url, "image")
        before_path = self.media_path(before_url, "image")
        clip_path = self.media_path(clip_url, "clip")
        if not after_path or not after_path.is_file():
            raise EvidenceRejected("确认事件缺少可验证关键截图。")
        if acceptance_mode and (not before_path or not before_path.is_file() or before_path == after_path):
            raise EvidenceRejected("真实验收必须包含相互独立的起点和终点截图。")
        before_path = before_path or after_path
        if not manual and (not clip_path or not clip_path.is_file()):
            raise EvidenceRejected("确认移动缺少可验证短视频。")
        for label, image_path in (("起点", before_path), ("终点", after_path)):
            try:image_frame=cv2.imdecode(np.frombuffer(image_path.read_bytes(),np.uint8),cv2.IMREAD_COLOR)
            except OSError:image_frame=None
            if image_frame is None:
                raise EvidenceRejected(f"{label}关键截图无法解码。")
        decoded_clip_frames = 0
        decoded_clip_fps = 0.0
        decoded_before_frame = None
        decoded_after_frame = None
        encoded_frame_errors: list[float] = []
        marker_bindings: list[dict[str, Any]] = []
        marker_detector = None
        if acceptance_mode:
            from services.vision.detectors.aruco import ArucoDetectorBackend
            marker_detector = ArucoDetectorBackend()
        if clip_path:
            bound_before_index = write_binding.get("before_frame_index") if write_binding else None
            bound_after_index = write_binding.get("after_frame_index") if write_binding else None
            capture=cv2.VideoCapture(str(clip_path))
            try:
                decoded_clip_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
                while True:
                    readable,clip_frame=capture.read()
                    if not readable or clip_frame is None:
                        break
                    if decoded_clip_frames == bound_before_index:
                        decoded_before_frame = clip_frame.copy()
                    if decoded_clip_frames == bound_after_index:
                        decoded_after_frame = clip_frame.copy()
                    if acceptance_mode and raw_clip_frames is not None and decoded_clip_frames < len(raw_clip_frames):
                        raw_frame = raw_clip_frames[decoded_clip_frames]
                        comparable = clip_frame
                        if comparable.shape[:2] != raw_frame.shape[:2]:
                            comparable = cv2.resize(comparable, (raw_frame.shape[1], raw_frame.shape[0]))
                        encoded_frame_errors.append(float(np.mean(cv2.absdiff(comparable, raw_frame))))
                        # Whole-image MAE can hide a frozen small target in a
                        # mostly-static room. Re-detect the bound identity in
                        # the raw AND encoded images and compare its location.
                        marker_id = int(event["aruco_id"])
                        raw_markers = [d for d in marker_detector.detect(raw_frame) if d.raw_id == marker_id]
                        keyframe = decoded_clip_frames in (bound_before_index, bound_after_index)
                        if keyframe and len(raw_markers) != 1:
                            raise EvidenceRejected("验收关键帧没有可独立解码的唯一物品标记。")
                        if raw_markers:
                            encoded_markers = [d for d in marker_detector.detect(comparable) if d.raw_id == marker_id]
                            if len(raw_markers) != 1 or len(encoded_markers) != 1:
                                raise EvidenceRejected("录像帧中的物品标记丢失、重复或被替换。")
                            raw_marker, encoded_marker = raw_markers[0], encoded_markers[0]
                            position_error = math.dist(raw_marker.center, encoded_marker.center)
                            if position_error > MAX_ENCODED_MARKER_POSITION_ERROR_PX:
                                raise EvidenceRejected("录像物品标记位置与来源帧不一致，疑似冻结或替换。")
                            if keyframe:
                                expected_center = event["trajectory"][0 if decoded_clip_frames == bound_before_index else -1]["center_norm"]
                                expected_px = (float(expected_center[0]) * raw_frame.shape[1], float(expected_center[1]) * raw_frame.shape[0])
                                if math.dist(raw_marker.center, expected_px) > MAX_ENCODED_MARKER_POSITION_ERROR_PX:
                                    raise EvidenceRejected("关键帧物品标记位置与轨迹端点不一致。")
                            marker_bindings.append({
                                "frame_index": decoded_clip_frames, "aruco_id": marker_id,
                                "raw_center": list(raw_marker.center), "encoded_center": list(encoded_marker.center),
                                "position_error_px": round(position_error, 6),
                            })
                    decoded_clip_frames += 1
            finally:
                capture.release()
            if decoded_clip_frames < 1:
                raise EvidenceRejected("事件短视频无法读取视频帧。")
        before_hash = file_sha256(before_path)
        after_hash = file_sha256(after_path)
        clip_hash = file_sha256(clip_path) if clip_path else None
        if event.get("before_screenshot_sha256") and event["before_screenshot_sha256"] != before_hash:
            raise EvidenceRejected("起点截图哈希与文件不一致。")
        if event.get("after_screenshot_sha256") and event["after_screenshot_sha256"] != after_hash:
            raise EvidenceRejected("终点截图哈希与文件不一致。")
        if event.get("screenshot_sha256") and event["screenshot_sha256"] != after_hash:
            raise EvidenceRejected("截图哈希与文件不一致。")
        if event.get("clip_sha256") and event["clip_sha256"] != clip_hash:
            raise EvidenceRejected("录像哈希与文件不一致。")
        if not manual and self.mode is RuntimeMode.REAL:
            expected_version = "event_service_atomic_v2" if acceptance_mode else "event_service_atomic_v1"
            if not write_binding or write_binding.get("binding_version") != expected_version:
                raise EvidenceRejected("REAL 确认事件的媒体必须由本次来源回调原子写入。")
            expected_binding = {
                "source_session_id": source_session,
                "source_frame_start": frame_start,
                "source_frame_end": frame_end,
                "source_timestamp_start": started.astimezone(timezone.utc).isoformat() if started else None,
                "source_timestamp_end": ended.astimezone(timezone.utc).isoformat() if ended else None,
            }
            if any(write_binding.get(key) != value for key, value in expected_binding.items()):
                raise EvidenceRejected("媒体写入证明与来源会话、帧范围或时间范围不一致。")
            if int(write_binding.get("clip_input_frames") or 0) < 2:
                raise EvidenceRejected("事件录像没有覆盖至少两个真实回调帧。")
            if acceptance_mode:
                clip_input_frames = int(write_binding.get("clip_input_frames") or 0)
                clip_written_frames = int(write_binding.get("clip_written_frames") or 0)
                if clip_written_frames != clip_input_frames or decoded_clip_frames != clip_input_frames:
                    raise EvidenceRejected("验收录像编码后帧数与来源回调帧数不一致。")
                if len(encoded_frame_errors) != clip_input_frames:
                    raise EvidenceRejected("验收录像无法逐帧绑定到本次来源回调。")
                if any(error > MAX_ENCODED_FRAME_MAE for error in encoded_frame_errors):
                    raise EvidenceRejected("验收录像存在与来源回调不一致的替换帧。")
                write_binding["clip_input_frame_sha256"] = [
                    self._frame_sha256(frame) for frame in (raw_clip_frames or [])
                ]
                write_binding["encoded_frame_mean_abs_error"] = round(
                    sum(encoded_frame_errors) / len(encoded_frame_errors), 6,
                )
                write_binding["encoded_frame_max_abs_error"] = round(max(encoded_frame_errors), 6)
                write_binding["encoded_marker_bindings"] = marker_bindings
                clip_duration = float(write_binding.get("clip_duration_seconds") or 0.0)
                required_pre = float(event.get("clip_required_pre_seconds") or 0.0)
                required_post = float(event.get("clip_required_post_seconds") or 0.0)
                if required_pre <= 0 or required_post <= 0 or clip_duration + 0.25 < required_pre + required_post:
                    raise EvidenceRejected("验收录像没有覆盖完整的前置与后置证据时长。")
                if decoded_clip_fps <= 0:
                    raise EvidenceRejected("验收录像缺少可验证帧率。")
                if decoded_before_frame is None or decoded_after_frame is None:
                    raise EvidenceRejected("验收录像无法解码前后关键帧位置。")
                decoded_images = []
                for image_path in (before_path, after_path):
                    decoded = cv2.imdecode(np.frombuffer(image_path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
                    if decoded is None:
                        raise EvidenceRejected("验收关键截图无法用于录像内容核对。")
                    decoded_images.append(decoded)
                keyframe_errors = []
                for encoded_frame, image_frame in zip(
                    (decoded_before_frame, decoded_after_frame), decoded_images,
                ):
                    if encoded_frame.shape[:2] != image_frame.shape[:2]:
                        encoded_frame = cv2.resize(encoded_frame, (image_frame.shape[1], image_frame.shape[0]))
                    keyframe_errors.append(float(np.mean(cv2.absdiff(encoded_frame, image_frame))))
                if any(error > MAX_ENCODED_KEYFRAME_MAE for error in keyframe_errors):
                    raise EvidenceRejected("验收录像中的前后关键帧与独立截图内容不一致。")
                write_binding["encoded_before_mean_abs_error"] = round(keyframe_errors[0], 6)
                write_binding["encoded_after_mean_abs_error"] = round(keyframe_errors[1], 6)
                if write_binding.get("before_screenshot_path") != before_url or write_binding.get("after_screenshot_path") != after_url:
                    raise EvidenceRejected("验收前后截图与本次视觉回调的媒体绑定不一致。")
                before_index = write_binding.get("before_frame_index")
                after_index = write_binding.get("after_frame_index")
                clip_count = int(write_binding.get("clip_input_frames") or 0)
                if (
                    isinstance(before_index, bool) or not isinstance(before_index, int)
                    or isinstance(after_index, bool) or not isinstance(after_index, int)
                    or not 0 <= before_index < after_index < clip_count
                ):
                    raise EvidenceRejected("验收录像中的前后关键帧索引无效。")
                if write_binding.get("clip_after_frame_sha256") != write_binding.get("after_frame_sha256"):
                    raise EvidenceRejected("验收录像没有覆盖终点关键帧。")
                if event.get("clip_post_roll_complete") is not True:
                    raise EvidenceRejected("验收录像没有完成终点后的证据缓冲。")
                required_post = float(event.get("clip_required_post_seconds") or 0)
                collected_post = float(event.get("clip_collected_post_seconds") or 0)
                if required_post <= 0 or collected_post + 1e-6 < required_post:
                    raise EvidenceRejected("验收录像终点后的持续时长不足。")
        binding = {
            **(write_binding or {
                "binding_version": "manual_correction_reference_v1" if manual else "external_media_v0",
                "capture_origin": "admin_manual_correction" if manual else "unverified_external_media",
                "manual_reference_event_id": manual_reference.get("event_id") if manual_reference else None,
            }),
            "runtime_mode": self.mode.value,
            "source_type": source_type,
            "is_simulated": simulated,
            "source_session_id": source_session or f"manual:{movement_session}",
            "source_frame_start": frame_start,
            "source_frame_end": frame_end,
            "source_timestamp_start": started.astimezone(timezone.utc).isoformat() if started else None,
            "source_timestamp_end": ended.astimezone(timezone.utc).isoformat() if ended else None,
            "before_screenshot": before_url,
            "before_screenshot_sha256": before_hash,
            "after_screenshot": after_url,
            "after_screenshot_sha256": after_hash,
            "screenshot_path": after_url,
            "screenshot_sha256": after_hash,
            "clip_path": clip_url,
            "clip_sha256": clip_hash,
        }
        binding["binding_sha256"] = hashlib.sha256(
            json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            "movement_session_id": movement_session,
            "source_session_id": source_session or f"manual:{movement_session}",
            "source_frame_start": frame_start,
            "source_frame_end": frame_end,
            "source_timestamp_start": started.astimezone(timezone.utc).isoformat() if started else now(),
            "source_timestamp_end": ended.astimezone(timezone.utc).isoformat() if ended else now(),
            "before_screenshot": before_url,
            "after_screenshot": after_url,
            "screenshot_path": after_url,
            "before_screenshot_sha256": before_hash,
            "after_screenshot_sha256": after_hash,
            "before_frame_sha256": (write_binding or {}).get("before_frame_sha256") or before_hash,
            "after_frame_sha256": (write_binding or {}).get("after_frame_sha256") or after_hash,
            "screenshot_sha256": after_hash,
            "clip_path": clip_url,
            "clip_sha256": clip_hash,
            "runtime_mode": self.mode.value,
            "source_type": source_type,
            "is_simulated": simulated,
            "manual_reference_event_id": (
                str(manual_reference.get("event_id") or manual_reference.get("id"))
                if manual_reference else None
            ),
            "media_write_binding": binding,
        }

    @staticmethod
    def _idempotency(event: dict[str, Any], *, manual: bool = False) -> str:
        pickup = event.get("pickup_evidence") if isinstance(event.get("pickup_evidence"), dict) else {}
        placement = event.get("placement_evidence") if isinstance(event.get("placement_evidence"), dict) else {}
        def timestamp(value: Any) -> str:
            parsed = _evidence_timestamp(value)
            return parsed.astimezone(timezone.utc).isoformat() if parsed else str(value or "")
        identity = {
            "runtime_mode": str(event.get("runtime_mode") or "").strip().upper(),
            "validation_run_id": str(event.get("validation_run_id") or "").strip(),
            "item_id": str(event.get("item_id") or "").strip(),
            "camera_id": str(event.get("camera_id") or "").strip(),
            "source_session_id": str(event.get("source_session_id") or "").strip(),
            "source_frame_start": event.get("source_frame_start"),
            "source_frame_end": event.get("source_frame_end"),
            "source_timestamp_start": timestamp(event.get("source_timestamp_start") or event.get("timestamp_start")),
            "source_timestamp_end": timestamp(event.get("source_timestamp_end") or event.get("timestamp_end")),
            "from_zone": str(event.get("from_zone") or "").strip(),
            "to_zone": str(event.get("to_zone") or "").strip(),
            "from_position": event.get("from_position"),
            "to_position": event.get("to_position") or event.get("final_position"),
            "pickup_keyframe": [pickup.get("source_frame"), timestamp(pickup.get("source_timestamp")), pickup.get("raw_frame_sha256")],
            "placement_keyframe": [placement.get("source_frame"), timestamp(placement.get("source_timestamp")), placement.get("raw_frame_sha256")],
        }
        if manual:
            # Separate administrator corrections are meaningful history.  Their
            # generated movement id distinguishes repeated human actions, while
            # automatic events deliberately do not trust a caller-random UUID.
            identity["manual_reference_event_id"] = str(event.get("manual_reference_event_id") or "").strip()
            identity["movement_session_id"] = str(event.get("movement_session_id") or "").strip()
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _source_window_key(event: dict[str, Any], *, manual: bool = False) -> str | None:
        """Database-unique identity for one physical source frame window."""
        if manual:
            return None
        identity = {
            "runtime_mode": str(event.get("runtime_mode") or "").strip().upper(),
            "item_id": str(event.get("item_id") or "").strip(),
            "camera_id": str(event.get("camera_id") or "").strip(),
            "source_session_id": str(event.get("source_session_id") or "").strip(),
            "source_frame_start": event.get("source_frame_start"),
            "source_frame_end": event.get("source_frame_end"),
        }
        if not all(value is not None and value != "" for value in identity.values()):
            return None
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _assert_replay_compatible(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
        """Reject two meanings or pixel payloads for one claimed source window.

        Event/request UUIDs are deliberately not evidence identity.  Once a
        camera session and frame window have been consumed, a retry may return
        the stored event only when all supplied source facts agree.  In
        particular, different raw keyframe bytes for the same source frames are
        treated as a provenance conflict rather than a second movement.
        """
        def trajectory_identity(value: Any) -> Any:
            if not isinstance(value, list):
                return value
            normalized = []
            for point in value:
                if not isinstance(point, dict):
                    normalized.append(point)
                    continue
                timestamp = _evidence_timestamp(
                    point.get("source_timestamp") or point.get("frame_timestamp") or point.get("timestamp")
                )
                zone = point.get("zone") if isinstance(point.get("zone"), dict) else {}
                normalized.append({
                    "source_frame": point.get("source_frame") if point.get("source_frame") is not None else point.get("frame_sequence"),
                    "source_timestamp": timestamp.astimezone(timezone.utc).isoformat() if timestamp else None,
                    "center": point.get("center_norm") if point.get("center_norm") is not None else point.get("center"),
                    "zone_id": point.get("zone_id") or zone.get("id"),
                    "zone_name": point.get("zone_name") or zone.get("name"),
                    "lost": bool(point.get("lost")),
                    "aruco_id": point.get("aruco_id"),
                    "confidence": point.get("confidence"),
                    "corners_norm": point.get("corners_norm"),
                    "source_session_id": point.get("source_session_id"),
                    "reconnect_epoch": point.get("reconnect_epoch"),
                })
            return normalized

        for key in (
            "validation_run_id", "aruco_id", "trajectory",
            "source_frame_start", "source_frame_end", "source_timestamp_start", "source_timestamp_end",
            "from_zone", "to_zone", "from_position", "to_position", "final_position",
        ):
            old_value, new_value = existing.get(key), incoming.get(key)
            if old_value is None or new_value is None:
                continue
            if key in {"source_timestamp_start", "source_timestamp_end"}:
                old_parsed, new_parsed = _evidence_timestamp(old_value), _evidence_timestamp(new_value)
                if old_parsed and new_parsed and old_parsed == new_parsed:
                    continue
            if key == "trajectory":
                old_value, new_value = trajectory_identity(old_value), trajectory_identity(new_value)
            old_json = json.dumps(old_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            new_json = json.dumps(new_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if old_json != new_json:
                raise EvidenceRejected(f"同一来源帧窗口的 {key} 与已保存事件冲突。")
        for phase in ("pickup_evidence", "placement_evidence"):
            old_phase = existing.get(phase) if isinstance(existing.get(phase), dict) else {}
            new_phase = incoming.get(phase) if isinstance(incoming.get(phase), dict) else {}
            for key in ("source_frame", "source_timestamp", "raw_frame_sha256"):
                old_value, new_value = old_phase.get(key), new_phase.get(key)
                if key == "source_timestamp":
                    old_parsed, new_parsed = _evidence_timestamp(old_value), _evidence_timestamp(new_value)
                    if old_parsed and new_parsed and old_parsed == new_parsed:
                        continue
                if old_value is not None and new_value is not None and old_value != new_value:
                    label = "像素哈希" if key == "raw_frame_sha256" else key
                    raise EvidenceRejected(f"同一来源帧窗口的关键帧{label}冲突。")

    def _source_window_replay(self, event: dict[str, Any]) -> dict[str, Any] | None:
        filters = {
            "item_id": event.get("item_id"),
            "camera_id": event.get("camera_id"),
            "source_session_id": event.get("source_session_id"),
            "source_frame_start": event.get("source_frame_start"),
            "source_frame_end": event.get("source_frame_end"),
        }
        if not all(value is not None and value != "" for value in filters.values()):
            return None
        existing = self.db.list("events", filters, limit=2)
        if not existing:
            return None
        self._assert_replay_compatible(existing[0], event)
        return existing[0]

    def record(self, event: dict[str, Any], frame=None, clip_frames=None):
        return self._record(event, frame, clip_frames, manual_authorized=False)

    def record_manual_correction(self, event: dict[str, Any]):
        """Persist a correction only from the authenticated administrator route."""
        return self._record(event, None, None, manual_authorized=True)

    def _record(self, event: dict[str, Any], frame=None, clip_frames=None, *, manual_authorized: bool):
        event = dict(event or {})
        # Normalize the complete identity before lookup, preflight deduplication,
        # idempotency hashing, evidence validation, or persistence.  Otherwise
        # concurrent callbacks that differ only by whitespace/casing can race
        # past the preflight query and receive different unique keys.
        event["runtime_mode"] = str(event.get("runtime_mode") or self.mode.value).strip().upper()
        for key in ("item_id", "camera_id", "source_session_id", "movement_session_id"):
            event[key] = str(event.get(key) or "").strip()
        if event.get("source_type") is not None:
            event["source_type"] = str(event["source_type"]).strip()
        camera = self.db.get("cameras", event["camera_id"], unscoped=True)
        item = self.db.get("items", event["item_id"], unscoped=True)
        if not camera or not item:
            raise EvidenceRejected("物品或摄像头不存在。")
        source_type, simulated = self._source(camera, event)
        event.update({"runtime_mode": self.mode.value, "source_type": source_type, "is_simulated": simulated})
        supplied_type = str(event.get("event_type") or event.get("final_status") or "")
        # Old per-state callbacks are candidates only. The vision service must
        # submit one completed episode; manual correction is the sole exception.
        if supplied_type not in CONFIRMED_TYPES:
            raise EvidenceRejected("仅接收完整 movement episode 或人工纠正；逐帧状态不会写入历史。")
        claimed_manual = supplied_type == "manual_correction" or bool(event.get("manually_corrected"))
        if claimed_manual and not manual_authorized:
            raise EvidenceRejected("视觉回调不能声明人工纠正；请使用已认证的管理员纠正接口。")
        if manual_authorized and supplied_type != "manual_correction":
            raise EvidenceRejected("管理员纠正入口只接受 manual_correction。")
        manual = bool(manual_authorized)
        validation_run_id = str(event.get("validation_run_id") or "").strip()
        candidate_acceptance_run = (
            self.db.get("acceptance_runs", validation_run_id)
            if self.mode is RuntimeMode.REAL and not manual and validation_run_id
            else None
        )
        acceptance_media_mode = bool(
            candidate_acceptance_run
            and str(event.get("detection_mode") or "").strip().lower() == "aruco_screen_validation"
        )
        event_id = str(event.get("event_id") or event.get("id") or uuid4().hex)
        if not SAFE_EVENT_ID.fullmatch(event_id):
            raise EvidenceRejected("event_id 含有不安全字符。")
        supplied_frames = list(clip_frames or [])
        source_bound_clip = event.get('source_bound_clip') is True and not manual
        if (acceptance_media_mode or source_bound_clip) and any(candidate is None or not getattr(candidate, "size", 0) for candidate in supplied_frames):
            raise EvidenceRejected("真实验收录像帧序列包含空帧，无法绑定 before_frame_index。")
        materialized_frames = [candidate for candidate in supplied_frames if candidate is not None and getattr(candidate, "size", 0)]
        before_frame = materialized_frames[0] if materialized_frames else None
        after_frame_index = len(materialized_frames) - 1 if materialized_frames else None
        if acceptance_media_mode or source_bound_clip:
            before_index = event.get("before_frame_index")
            if before_index is None:
                if event.get("before_frame_is_baseline") is not True:
                    raise EvidenceRejected("真实验收必须提供 before_frame_index 或明确声明首帧为区域A基线。")
                before_index = 0
            if isinstance(before_index, bool) or not isinstance(before_index, int) or not 0 <= before_index < len(materialized_frames):
                raise EvidenceRejected("真实验收的 before_frame_index 无效。")
            before_frame = materialized_frames[before_index]
            event["before_frame_index"] = before_index
            after_frame_index = event.get("after_frame_index")
            if (
                isinstance(after_frame_index, bool) or not isinstance(after_frame_index, int)
                or not 0 <= after_frame_index < len(materialized_frames)
                or after_frame_index <= before_index
            ):
                raise EvidenceRejected("真实验收的 after_frame_index 无效或不晚于 before 帧。")
            if frame is None or not getattr(frame, "size", 0):
                raise EvidenceRejected("真实验收缺少终点回调帧。")
            if self._frame_sha256(materialized_frames[after_frame_index]) != self._frame_sha256(frame):
                raise EvidenceRejected("after_frame_index 未指向本次终点回调帧。")
            event["after_frame_index"] = after_frame_index
        if source_bound_clip:
            sources = event.get('clip_frame_sources')
            if not isinstance(sources, list) or len(sources) != len(materialized_frames) or len(sources) < 2:
                raise EvidenceRejected('移动录像缺少逐帧来源绑定。')
            session = self.db.get('source_sessions', str(event.get('source_session_id') or ''), unscoped=True)
            session_first, session_last = (session or {}).get('first_frame'), (session or {}).get('last_frame')
            session_start = _parse_timestamp((session or {}).get('started_at'))
            session_end = _parse_timestamp((session or {}).get('ended_at') or (session or {}).get('last_frame_at'))
            last_sequence, last_time = None, None
            clip_settings = (self.db.get('settings', 'main', unscoped=True) or {}).get('value') or {}
            max_gap = float(clip_settings.get('max_frame_gap_seconds', .75))
            for source in sources:
                if not isinstance(source, dict):
                    raise EvidenceRejected('移动录像逐帧来源格式无效。')
                sequence, at = source.get('sequence'), _parse_timestamp(source.get('source_timestamp'))
                if (source.get('source_session_id') != event.get('source_session_id')
                        or source.get('reconnect_epoch') != event.get('reconnect_epoch')
                        or any(type(value) is not int for value in (sequence, session_first, session_last))
                        or not session_first <= sequence <= session_last
                        or not at or not session_start or not session_end or not session_start <= at <= session_end
                        or (last_sequence is not None and (sequence <= last_sequence or not 0 < (at - last_time).total_seconds() <= max_gap + 1e-6))):
                    raise EvidenceRejected('移动录像帧不属于同一连续来源会话。')
                last_sequence, last_time = sequence, at
            before_source, after_source = sources[event['before_frame_index']], sources[after_frame_index]
            if (before_source['sequence'] != event.get('source_frame_start')
                    or after_source['sequence'] != event.get('source_frame_end')
                    or _parse_timestamp(before_source['source_timestamp']) != _parse_timestamp(event.get('source_timestamp_start'))
                    or _parse_timestamp(after_source['source_timestamp']) != _parse_timestamp(event.get('source_timestamp_end'))
                    or sources[0]['sequence'] != event.get('clip_source_frame_start')
                    or sources[-1]['sequence'] != event.get('clip_source_frame_end')
                    or _parse_timestamp(sources[0]['source_timestamp']) != _parse_timestamp(event.get('clip_source_timestamp_start'))
                    or _parse_timestamp(sources[-1]['source_timestamp']) != _parse_timestamp(event.get('clip_source_timestamp_end'))):
                raise EvidenceRejected('移动录像前后关键帧与事件时间/帧编号不一致。')
            duration = (_parse_timestamp(sources[-1]['source_timestamp']) - _parse_timestamp(sources[0]['source_timestamp'])).total_seconds()
            event['clip_fps'] = (len(sources) - 1) / duration
        if not manual and frame is not None and getattr(frame, "size", 0) and materialized_frames:
            event["pickup_evidence"] = {
                **(event.get("pickup_evidence") if isinstance(event.get("pickup_evidence"), dict) else {}),
                "raw_frame_sha256": self._frame_sha256(before_frame),
            }
            event["placement_evidence"] = {
                **(event.get("placement_evidence") if isinstance(event.get("placement_evidence"), dict) else {}),
                "raw_frame_sha256": self._frame_sha256(frame),
            }
        if not manual:
            supplied_media = [key for key in (
                "screenshot_path", "before_screenshot", "after_screenshot", "screenshot_sha256",
                "before_screenshot_sha256", "after_screenshot_sha256", "before_frame_sha256", "after_frame_sha256",
                "clip_path", "clip_sha256",
            ) if event.get(key)]
            if supplied_media:
                raise EvidenceRejected("视觉回调不得提交预写媒体路径或哈希；证据必须由 EventService 从本次帧原子生成。")
            # A late callback from an already committed run is a replay, not a
            # second acceptance attempt.  Compare its immutable source facts
            # and raw keyframe hashes before the ACTIVE-run gate; a conflicting
            # replay is rejected, while an exact retry returns the one stored
            # event without writing randomized media again.
            if validation_run_id:
                run_events = self.db.list("events", {"validation_run_id": validation_run_id}, limit=2)
                if run_events:
                    self._assert_replay_compatible(run_events[0], event)
                    return run_events[0]
            identity = {
                "movement_session_id": str(event.get("movement_session_id") or "").strip(),
                "item_id": str(event.get("item_id") or "").strip(),
                "camera_id": str(event.get("camera_id") or "").strip(),
                "source_session_id": str(event.get("source_session_id") or "").strip(),
            }
            if all(identity.values()):
                existing = self.db.list("events", identity, limit=2)
                if existing:
                    self._assert_replay_compatible(existing[0], event)
                    return existing[0]
            source_replay = self._source_window_replay(event)
            if source_replay:
                return source_replay
        acceptance_run = None if manual else self._validate_physical_acceptance(event, item, camera, source_type, simulated)
        if acceptance_run:
            clip_started = _parse_timestamp(event.get("clip_source_timestamp_start"))
            clip_ended = _parse_timestamp(event.get("clip_source_timestamp_end"))
            pickup_at = _evidence_timestamp((event.get("pickup_evidence") or {}).get("source_timestamp"))
            placed_at = _evidence_timestamp((event.get("placement_evidence") or {}).get("source_timestamp"))
            required_pre = float(event.get("clip_required_pre_seconds") or 0.0)
            required_post = float(event.get("clip_required_post_seconds") or 0.0)
            clip_frame_start = event.get("clip_source_frame_start")
            clip_frame_end = event.get("clip_source_frame_end")
            if (
                not clip_started or not clip_ended or not pickup_at or not placed_at
                or clip_ended <= clip_started
                or clip_started > pickup_at - timedelta(seconds=required_pre)
                or clip_ended < placed_at + timedelta(seconds=required_post)
            ):
                raise EvidenceRejected("验收录像的来源时间没有覆盖移动前后证据窗口。")
            session = self.db.get("source_sessions", str(event.get("source_session_id") or ""), unscoped=True)
            session_started = _parse_timestamp((session or {}).get("started_at"))
            session_ended = _parse_timestamp((session or {}).get("ended_at") or (session or {}).get("last_frame_at"))
            session_first = (session or {}).get("first_frame")
            session_last = (session or {}).get("last_frame")
            if (
                not isinstance(clip_frame_start, int) or not isinstance(clip_frame_end, int)
                or clip_frame_end <= clip_frame_start
                or clip_frame_end - clip_frame_start + 1 < len(materialized_frames)
                or not isinstance(session_first, int) or not isinstance(session_last, int)
                or not session_first <= clip_frame_start <= clip_frame_end <= session_last
                or not session_started or not session_ended
                or not session_started <= clip_started <= clip_ended <= session_ended
            ):
                raise EvidenceRejected("验收录像帧范围不属于当前连续来源会话。")
            clip_span = (clip_ended - clip_started).total_seconds()
            actual_sample_fps = (len(materialized_frames) - 1) / clip_span
            configured_fps = max(1.0, float(camera.get("inference_fps") or 5.0))
            minimum_frames = math.ceil((required_pre + required_post) * configured_fps * MIN_ACCEPTANCE_FRAME_RATE_RATIO)
            if actual_sample_fps < 1.0 or len(materialized_frames) < minimum_frames:
                raise EvidenceRejected("验收录像的真实采样帧不足，不能证明完整移动过程。")
            # The service derives encoder FPS from server-observed source time;
            # it never trusts a callback-supplied playback rate.
            event["clip_fps"] = actual_sample_fps
        provisional_key = self._idempotency(event, manual=manual)
        semantic_match = self.db.list("events", {"idempotency_key": provisional_key}, limit=2)
        if semantic_match:
            return semantic_match[0]
        write_binding: dict[str, Any] | None = None
        clip_result = None
        generated_urls: list[str] = []
        coordination_lock = self.retention.lock if self.retention else None
        coordination_acquired = False
        try:
            if coordination_lock:
                # Retention's orphan scan uses this same lock.  Hold it before
                # the first temporary/atomic media write and through the bundle
                # commit so a valid-but-not-yet-registered file cannot be
                # mistaken for an orphan.
                coordination_lock.acquire()
                coordination_acquired = True
            if not manual:
                if validation_run_id:
                    run_events = self.db.list("events", {"validation_run_id": validation_run_id}, limit=2)
                    if run_events:
                        self._assert_replay_compatible(run_events[0], event)
                        if coordination_acquired:
                            coordination_lock.release()
                            coordination_acquired = False
                        return run_events[0]
                source_replay = self._source_window_replay(event)
                if source_replay:
                    if coordination_acquired:
                        coordination_lock.release()
                        coordination_acquired = False
                    return source_replay
            if not manual and (frame is None or not getattr(frame, "size", 0) or len(materialized_frames) < 2):
                raise EvidenceRejected("确认事件必须随本次视觉回调提供关键帧和至少两个录像帧。")
            media_write_id = f"{event_id}-{uuid4().hex[:12]}"
            if frame is not None and materialized_frames:
                before=before_frame
                if acceptance_run and before is not None:
                    before_url=self._atomic_image(f"{media_write_id}-before",before)
                    after_url=self._atomic_image(f"{media_write_id}-after",frame)
                    if before_url:generated_urls.append(before_url)
                    if after_url:generated_urls.append(after_url)
                    if before_url and after_url:
                        event["before_screenshot"]=before_url
                        event["after_screenshot"]=after_url
                        event["screenshot_path"]=after_url
                elif before is not None:
                    height=max(before.shape[0],frame.shape[0])
                    def normalized(value):
                        width=max(1,round(value.shape[1]*height/value.shape[0]))
                        return cv2.resize(value,(width,height)) if value.shape[0]!=height else value
                    contact=np.hstack((normalized(before),normalized(frame)))
                    url=self._atomic_image(media_write_id,contact)
                    if url:
                        event["screenshot_path"]=url;event["before_screenshot"]=url;event["after_screenshot"]=url
                        event["screenshot_sha256"]=None
                        generated_urls.append(url)
                        event.setdefault("pickup_evidence",{})["main_screenshot_region"]="left"
                        event.setdefault("placement_evidence",{})["main_screenshot_region"]="right"
            if materialized_frames:
                # EventMediaWriter uses temporary files and an atomic os.replace for
                # both MP4 and AVI; callers never choose the destination path.
                from services.vision.events.media import EventMediaWriter
                clip_result = EventMediaWriter(self.media_root).write_clip(media_write_id, materialized_frames, float(event.get("clip_fps") or 5.0))
                if clip_result.path:
                    event["clip_path"] = f"/media/event-clips/{clip_result.path.name}"
                    event["clip_sha256"] = clip_result.sha256
                    generated_urls.append(event["clip_path"])
            if frame is not None and materialized_frames and event.get("screenshot_path") and event.get("clip_path"):
                after_raw_sha=self._frame_sha256(frame)
                write_binding = {
                    "binding_version": "event_service_atomic_v2" if acceptance_run else "event_service_atomic_v1",
                    "writer": "EventService",
                    "capture_origin": "vision_callback",
                    "bound_at": now(),
                    "source_session_id": str(event.get("source_session_id") or ""),
                    "source_frame_start": event.get("source_frame_start"),
                    "source_frame_end": event.get("source_frame_end"),
                    "source_timestamp_start": (_parse_timestamp(event.get("source_timestamp_start") or event.get("timestamp_start") or event.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat(),
                    "source_timestamp_end": (_parse_timestamp(event.get("source_timestamp_end") or event.get("timestamp_end") or event.get("ended_at")) or datetime.min.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat(),
                    "clip_input_frames": len(materialized_frames),
                    "clip_written_frames": int(clip_result.frame_count if clip_result else 0),
                    "clip_duration_seconds": float(clip_result.duration if clip_result else 0.0),
                    "clip_format": clip_result.format if clip_result else None,
                    "clip_writer_diagnostic": clip_result.error if clip_result else "writer_not_run",
                    "clip_first_frame_sha256": self._frame_sha256(materialized_frames[0]),
                    "clip_last_frame_sha256": self._frame_sha256(materialized_frames[-1]),
                    "before_frame_index": event.get("before_frame_index",0),
                    "before_frame_sha256": self._frame_sha256(before_frame),
                    "after_frame_index": after_frame_index,
                    "after_frame_sha256": after_raw_sha,
                    "clip_after_frame_sha256": self._frame_sha256(materialized_frames[after_frame_index]),
                    "before_screenshot_path": event.get("before_screenshot"),
                    "after_screenshot_path": event.get("after_screenshot"),
                }
                if source_bound_clip:
                    # Persist proof in the existing media metadata JSON instead
                    # of adding a second unbounded trajectory/event table.
                    write_binding['source_bound_clip'] = True
                    write_binding['clip_frame_sources'] = event['clip_frame_sources']
            evidence = self._validate_evidence(
                event,
                source_type,
                simulated,
                manual=manual,
                write_binding=write_binding,
                raw_clip_frames=materialized_frames,
            )
            placement=event.get('placement_evidence') or {}
            settings=(self.db.get('settings','main') or {}).get('value') or {}
            photo_placement_supported=(placement.get('support_surface_confirmed') is True
                and placement.get('hand_model_healthy') is True and placement.get('hand_near') is False
                and placement.get('source_frame') == event.get('source_frame_end')
                and verified_release(placement.get('hand_interaction'),event.get('source_session_id'),
                                     event.get('source_frame_start'),event.get('source_frame_end'),
                                     min_stable_seconds=max(5.0,float(settings.get('hand_interaction_release_stable_seconds',5.0)))))
            final_status=('position_changed' if not manual and event.get('detection_mode')=='experimental'
                          and not photo_placement_supported else 'confirmed_placed')
            payload = {
                "event_id": event_id,
                "item_name": item.get("name"), "camera_name": camera.get("name"), "room_name": camera.get("room_name"),
                "event_type": "manual_correction" if manual else "movement",
                "timestamp_start": evidence["source_timestamp_start"], "timestamp_end": evidence["source_timestamp_end"],
                "started_at": evidence["source_timestamp_start"], "ended_at": evidence["source_timestamp_end"],
                "ingested_at": now(), "evidence_status": "confirmed", "final_status": final_status,
                "created_by": "user" if manual else "vision_pipeline",
                "pinned": False, "manually_corrected": manual,
                "human_review_status": "human_corrected" if manual else "unreviewed",
                **event, **evidence,
            }
            payload.update({
                "event_type":"manual_correction" if manual else "movement",
                "created_by":"user" if manual else "vision_pipeline",
                "manually_corrected":manual,
                "human_review_status":"human_corrected" if manual else str(event.get("human_review_status") or "unreviewed"),
                # Never inherit the referenced event's ingestion order.  A
                # correction is a new operator decision even when the source
                # evidence predates the current wall clock (or has a future
                # device timestamp), so its own ingestion instant must win.
                "ingested_at":now(),
                "evidence_status":"confirmed",
                "final_status":final_status,
                "pinned":False,
                "started_at":evidence["source_timestamp_start"],"ended_at":evidence["source_timestamp_end"],
            })
            if manual:
                # Old aliases copied from a referenced event (or separate
                # clock reads) must not override the correction's validated
                # time used by its media binding and provenance audit.
                payload["timestamp_start"] = evidence["source_timestamp_start"]
                payload["timestamp_end"] = evidence["source_timestamp_end"]
            payload["previous_zone"] = payload.get("from_zone") or payload.get("previous_zone")
            payload["new_zone"] = payload.get("to_zone") or payload.get("new_zone")
            payload["zone_name"] = payload.get("to_zone") or payload.get("zone_name")
            payload["idempotency_key"] = provisional_key
            payload["source_window_key"] = self._source_window_key(payload, manual=manual)

            media_rows=[]
            media_specs = (
                (
                    (payload.get("before_screenshot"), "image", "before", payload.get("before_screenshot_sha256")),
                    (payload.get("after_screenshot"), "image", "after", payload.get("after_screenshot_sha256")),
                    (payload.get("clip_path"), "clip", "clip", payload.get("clip_sha256")),
                )
                if acceptance_run else (
                    (payload.get("screenshot_path"), "image", "contact", payload.get("screenshot_sha256")),
                    (payload.get("clip_path"), "clip", "clip", payload.get("clip_sha256")),
                )
            )
            for url, kind, role, sha in media_specs:
                if url:
                    media_rows.append({
                        "id":hashlib.sha256(f"{event_id}:{kind}:{role}:{url}".encode()).hexdigest(),
                        "event_id":event_id,"runtime_mode":self.mode.value,"source_session_id":payload.get("source_session_id"),
                        "path":url,"kind":kind,"role":role,"sha256":sha,
                        "metadata":{"event_time_start":payload.get("timestamp_start"),"event_time_end":payload.get("timestamp_end"),"source_frame_start":payload.get("source_frame_start"),"source_frame_end":payload.get("source_frame_end"),"evidence_role":role,"main_screenshot_layout":"before_left_after_right" if role=="contact" else None,"write_binding":evidence.get("media_write_binding")},
                        "status":"active","exported":False,
                    })
            current_state = {
                "item_id":payload["item_id"], "current_camera":payload.get("camera_id"), "current_room":payload.get("room_name"),
                "current_zone":payload.get("to_zone") or payload.get("zone_name"), "current_position":payload.get("final_position"),
                "last_seen_at":payload.get("timestamp_end"), "last_confirmed_placed_at":payload.get("timestamp_end"),
                "confidence":payload.get("confidence"), "evidence_event_id":event_id, "runtime_mode":self.mode.value,
                "evidence_ingested_at":payload.get("ingested_at"),
                "source_type":source_type, "is_simulated":simulated, "source_session_id":payload.get("source_session_id"), "status":"confirmed_placed",
                "validation_run_id":payload.get("validation_run_id"),
                "last_confirmed_placement":self._placement_summary(payload),
                "location_hypotheses":[],
            }
            if final_status=='position_changed':
                previous=self.db.get('item_current_state',f"{self.mode.value}:{payload['item_id']}") or {}
                current_state.update({
                    'status':'last_seen','last_confirmed_placed_at':previous.get('last_confirmed_placed_at'),
                    'last_confirmed_placement':previous.get('last_confirmed_placement'),
                })
                if not current_state['last_confirmed_placement'] and previous.get('evidence_event_id'):
                    prior=next(iter(self.db.list('events',{'event_id':previous['evidence_event_id']},limit=1)),None)
                    if prior and prior.get('final_status')=='confirmed_placed':
                        current_state['last_confirmed_placement']=self._placement_summary(prior)
            row, inserted = self.db.insert_event_bundle_idempotent(
                payload, media_rows, current_state, f"{self.mode.value}:{payload['item_id']}",
                acceptance_run_id=str(payload.get("validation_run_id") or "") or None,
            )
            if coordination_acquired:
                coordination_lock.release()
                coordination_acquired = False
        except Exception:
            if coordination_acquired:
                coordination_lock.release()
                coordination_acquired = False
            for url in generated_urls:
                path = self.media_path(url)
                if path:
                    try:path.unlink(missing_ok=True)
                    except OSError:pass
            raise
        if not inserted:
            # A concurrent duplicate won the SQLite uniqueness race. Its media
            # is authoritative; the losing callback's randomized files are not.
            for url in generated_urls:
                path = self.media_path(url)
                if path:
                    try:path.unlink(missing_ok=True)
                    except OSError:pass
            if not manual:
                self._assert_replay_compatible(row, payload)
            return row
        if self.retention:
            self.retention.trigger_async("event")
        return row

    @staticmethod
    def _placement_summary(event):
        if not event or event.get('evidence_status')!='confirmed' or event.get('final_status')!='confirmed_placed':
            return None
        return {key:event.get(key) for key in (
            'event_id','item_id','camera_id','camera_name','room_name','zone_name','to_zone',
            'final_position','timestamp_start','timestamp_end','source_session_id','source_type',
            'runtime_mode','is_simulated','source_attestation_id','validation_run_id','detection_mode',
            'evidence_status','final_status','screenshot_path','screenshot_sha256','after_screenshot',
            'after_screenshot_sha256','clip_path','clip_sha256','manually_corrected','confidence')}

    @staticmethod
    def _position(value):
        try:
            point = [float(value['x']),float(value['y'])] if isinstance(value,dict) else [float(v) for v in value]
            return point if len(point)==2 and all(math.isfinite(v) for v in point) else None
        except (TypeError,KeyError,ValueError):
            return None

    def _atomic_observation_jpeg(self, content: bytes):
        if not isinstance(content,bytes) or not content.startswith(b'\xff\xd8\xff') or len(content)>8*1024*1024:
            raise ValueError('观察截图必须是有界 JPEG 字节')
        with Image.open(io.BytesIO(content)) as image:
            if image.width*image.height>16_777_216:
                raise ValueError('观察截图像素超限')
            image.verify()
        if cv2.imdecode(np.frombuffer(content,np.uint8),cv2.IMREAD_COLOR) is None:
            raise ValueError('观察截图无法解码')
        folder=self.media_root/'event-images'
        if folder.is_symlink() or getattr(folder,'is_junction',lambda:False)() or folder.resolve().parent!=self.media_root:
            raise ValueError('观察截图目录不安全')
        target=folder/f'observed-{uuid4().hex}.jpg'
        temporary=target.with_suffix('.jpg.tmp')
        try:
            temporary.write_bytes(content)
            os.replace(temporary,target)
        finally:
            temporary.unlink(missing_ok=True)
        return f'/media/event-images/{target.name}',hashlib.sha256(content).hexdigest()

    def observe(self, track: dict[str, Any], *, observation_verified: bool = False, observation_jpeg: bytes | None = None):
        # This proof is supplied only by the server's live engine callback.
        # A payload field claiming success is never sufficient for persistence.
        if observation_verified is not True:
            return None
        track = dict(track or {})
        camera = self.db.get("cameras", str(track.get("camera_id") or ""), unscoped=True)
        item = self.db.get("items", str(track.get("item_id") or ""), unscoped=True)
        if not camera or not item:
            return None
        source_type, simulated = self._source(camera, track)
        observed_at = track.get("last_seen") or track.get("timestamp") or now()
        source_session = str(track.get("source_session_id") or "")
        if not source_session:
            return None
        coordination_lock = self.retention.lock if self.retention else self._observation_lock
        coordination_lock.acquire()
        generated_url=None
        committed=False
        try:
            # Read the prior confirmed state inside the same event/retention
            # critical section as the conditional upsert.  Otherwise a movement
            # can commit between this read and write and a later observation can
            # incorrectly downgrade it to ``last_seen``.
            previous=self.db.get("item_current_state",f"{self.mode.value}:{item['id']}")
            previous=previous or {}
            previous_time=_evidence_timestamp(previous.get('last_seen_at'))
            observation_time=_evidence_timestamp(observed_at)
            if observation_time is None:
                return None
            if previous_time and observation_time<previous_time:
                return previous
            observed_status=str(track.get("state") or track.get("status") or "last_seen").lower()
            missing=observed_status in {'occluded','missing','lost','offline','exited_view'}
            same_source=previous.get('current_camera')==camera['id'] and previous.get('source_session_id')==source_session
            position=self._position(track.get('center'))
            prior_position=self._position(previous.get('current_position'))
            settings=(self.db.get('settings','main') or {}).get('value') or {}
            jitter=float(settings.get('stable_position_jitter',.018))
            settled=(same_source and position is not None and prior_position is not None
                     and math.dist(position,prior_position)<=jitter and not track.get('hand_near')
                     and track.get('holding_status') not in {'co_moving','holding_uncertain','release_candidate'}
                     and float(track.get('speed') or 0)<=float(settings.get('stable_speed',.025))
                     and observed_status not in {'moving','carried','pickup_candidate','placed_candidate'})
            if observed_status in {"occluded","missing","lost"}:
                if not previous or previous.get("current_camera") != camera["id"] or previous.get("source_session_id") != source_session:
                    return None
                status="occluded" if observed_status in {"occluded","missing"} else "lost"
            elif missing:
                if not same_source:return None
                status=observed_status
            elif settled and previous.get("status")=="confirmed_placed" and previous.get("current_zone")==track.get("zone_name"):
                status="confirmed_placed"
            else:
                status="last_seen"
            last_observed=dict(previous.get('last_observed') or {})
            if missing:
                last_observed.pop('snapshot_stable_since',None)
                observed_at=previous.get('last_seen_at')
                position=prior_position
                # Retain interaction evidence from the last actually observed
                # frame. Missing-frame state has its own clock and must not
                # rewrite the historical near/co-motion basis of a hypothesis.
                last_observed.update({'status':status,'state_updated_at':now(),
                    'current_interaction':{**(track.get('hand_interaction') or {}),
                        'holding_status':track.get('holding_status','not_established'), 'hand_near':False,
                        'hand_model_healthy':track.get('hand_model_healthy') is True}})
            else:
                last_observed.pop('current_interaction',None)
                last_observed={**last_observed,
                    'item_id':item['id'],'camera_id':camera['id'],'camera_name':camera.get('name'),
                    'room_name':camera.get('room_name'),'zone_name':track.get('zone_name'),'zone_id':track.get('zone_id'),
                    'position':position,'bbox':track.get('bbox'),'observed_at':observed_at,
                    'source_frame':track.get('observation_source_frame') or track.get('source_frame'),
                    'source_session_id':source_session,'source_type':source_type,'runtime_mode':self.mode.value,
                    'is_simulated':simulated,'confidence':track.get('confidence'),'status':status,
                    'detection_mode':track.get('detection_mode'),'identity_evidence':track.get('identity_evidence'),
                    'source_attestation_id':track.get('source_attestation_id'),'state_updated_at':now(),
                    'scene_version':track.get('scene_version'),
                    'support_surface_id':track.get('support_surface_id'),
                    'support_surface_confirmed':track.get('support_surface_confirmed') is True,
                    'support_contact_confirmed':track.get('support_contact_confirmed') is True,
                    'evidence_type':'observed','holding_status':track.get('holding_status','not_established'),
                    'interaction':{**(track.get('hand_interaction') or {}), 'hand_near':track.get('hand_near') is True,
                                   'hand_model_healthy':track.get('hand_model_healthy') is True},
                    'trajectory':list(track.get('history') or [])[-6:],
                }
            new_media=None
            if not missing:
                # Last-seen coordinates may update while carried. Persisting
                # a new screenshot is separate: one first observation, then
                # only a changed position that stays still with no nearby hand.
                stable_since=_evidence_timestamp(last_observed.get('snapshot_stable_since')) if settled else None
                if settled and stable_since is None:stable_since=observation_time
                last_observed['snapshot_stable_since']=stable_since.isoformat() if stable_since else None
                stable_ready=bool(stable_since and (observation_time-stable_since).total_seconds()
                    >=max(float(settings.get('min_stable_seconds',1.8)),
                          float(settings.get('hand_interaction_release_stable_seconds',5.0))))
                screenshot_position=self._position(last_observed.get('screenshot_position'))
                image_same_source=(last_observed.get('screenshot_camera_id')==camera['id']
                                   and last_observed.get('screenshot_source_session_id')==source_session)
                moved=(not image_same_source or screenshot_position is None or position is None
                       or math.dist(position,screenshot_position)>=float(settings.get('observation_snapshot_move_distance',.04)))
                due=(not last_observed.get('screenshot_path') or moved and stable_ready)
                if moved and not due:
                    # Preserve the older evidence with its own frame, position,
                    # and clock. Never relabel it as the current moving frame.
                    last_observed.update({'image_status':'previous_observation',
                        'image_error':None,'snapshot_deferred_reason':'等待新位置稳定且手离开'})
                if due and observation_jpeg is not None:
                    try:
                        generated_url,digest=self._atomic_observation_jpeg(observation_jpeg)
                        last_observed.pop('snapshot_deferred_reason',None)
                        last_observed.update({'screenshot_path':generated_url,'screenshot_sha256':digest,
                            'screenshot_source_frame':last_observed['source_frame'],'screenshot_observed_at':observed_at,
                            'screenshot_source_session_id':source_session,'screenshot_camera_id':camera['id'],
                            'screenshot_position':position,'image_status':'available','image_error':None})
                        new_media={'id':uuid4().hex,'event_id':None,'owner_current_state_id':f"{self.mode.value}:{item['id']}",
                            'runtime_mode':self.mode.value,'source_session_id':source_session,'path':generated_url,
                            'kind':'image','role':'last_observed','sha256':digest,'status':'active','exported':False,
                            'metadata':{'source_frame':last_observed['source_frame'],'observed_at':observed_at}}
                    except (OSError,ValueError,cv2.error) as exc:
                        last_observed.update({'image_status':'write_failed','image_error':str(exc)})
                elif due:
                    last_observed.update({'image_status':'not_available','image_error':'当前已验证观察没有可保存的同帧截图。'})
                if due and generated_url is None and moved and last_observed.get('screenshot_path'):
                    # A failed replacement is not permission to orphan the
                    # last available evidence. Keep its original binding and
                    # error; only a successful new-image transaction retires it.
                    last_observed.update({'image_status':'previous_observation',
                        'snapshot_deferred_reason':'新截图暂不可用，保留较早证据'})
            last_confirmed=previous.get('last_confirmed_placement')
            if not last_confirmed and previous.get('evidence_event_id'):
                linked=next(iter(self.db.list('events',{'event_id':previous['evidence_event_id']},limit=1)),None)
                last_confirmed=self._placement_summary(linked)
            from .location_hypotheses import candidate_locations
            scene=self.db.get('scenes',f"scene:{self.mode.value}:{camera['id']}")
            hypotheses=candidate_locations(last_observed,scene,status,track.get('source_timestamp') or observed_at) if missing else []
            if last_confirmed:
                last_confirmed={**last_confirmed,'superseded_as_current':bool(missing or not settled),
                                'superseded_reason':'更新的视觉观察或不可见状态' if missing or not settled else None}
            state = {
                "item_id":item["id"], "current_camera":camera["id"], "current_room":camera.get("room_name"),
                "current_zone":previous.get('current_zone') if missing else track.get("zone_name"), "current_position":position, "last_seen_at":observed_at,
                # A newer observation updates where/when the item was last
                # seen, but it is not new placement evidence. Keep the last
                # confirmed movement link until that event is explicitly
                # deleted; search can then distinguish current last-seen state
                # from the older confirmed placement without breaking the
                # current-state -> evidence retention dependency.
                "last_confirmed_placed_at":(previous or {}).get("last_confirmed_placed_at"),
                "evidence_event_id":(previous or {}).get("evidence_event_id"),
                "confidence":track.get("confidence"), "runtime_mode":self.mode.value, "source_type":source_type,
                "is_simulated":simulated, "source_session_id":source_session, "evidence_ingested_at":now(),
                "status":status,
                "last_observed":last_observed,"last_confirmed_placement":last_confirmed,
                "location_hypotheses":hypotheses,
            }
            identity=track.get('identity_evidence') or {}
            expected_profile=identity if track.get('detection_mode')=='experimental' and identity.get('accepted') is True else None
            result=self.db.save_current_state_if_newer(state, f"{self.mode.value}:{item['id']}",observation_media=new_media,expected_profile=expected_profile)
            committed=True
            if generated_url and ((result or {}).get('last_observed') or {}).get('screenshot_path')!=generated_url:
                self.media_path(generated_url,'image').unlink(missing_ok=True)
            if self.retention and new_media:
                self.retention.recover_pending()
            return result
        except Exception:
            if generated_url and not committed:
                path=self.media_path(generated_url,'image')
                if path:path.unlink(missing_ok=True)
            raise
        finally:
            coordination_lock.release()
