"""Source-bound scene sketches and explicit user-confirmed planar 3D geometry.

Only a server-selected, source-bound JPEG may propose furniture. Detection boxes
are not support surfaces. This service owns no camera or inference worker.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

import cv2
import numpy as np
from PIL import Image, UnidentifiedImageError

from .db import now
from .runtime_mode import MEDIA_NAMES, RuntimeMode, validate_source_for_mode
from .scene_mapping import MappingError, SceneProjector, calibrate, make_world_geometry, physical_geometry, proposal_draft, surface_matrix
from services.vision.geometry import FrameGeometry


class SceneError(ValueError):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


class SceneService:
    MIN_INTERVAL_SECONDS = 3.0
    MAX_JPEG_BYTES = 8 * 1024 * 1024
    MAX_PIXELS = 16_777_216
    SURFACE_TYPES = {"table", "sofa", "bed", "shelf", "cabinet", "floor", "other"}
    LABELS = {"couch": ("sofa", "沙发"), "dining table": ("table", "桌面"), "bed": ("bed", "床")}

    def __init__(self, db, media_root: Path, model_root: Path, mapping_settings=None):
        self.db = db
        self.mode = RuntimeMode(db.runtime_mode)
        self.media = Path(media_root).absolute()
        if self.media.name != MEDIA_NAMES[self.mode] or self._link(self.media):
            raise SceneError("场景图片必须使用本运行模式的独立媒体目录。")
        self.media.mkdir(parents=True, exist_ok=True)
        self.root = self.media.resolve()
        self.folder = self.media / "scene-images"
        self.folder.mkdir(exist_ok=True)
        self.model_root = Path(model_root)
        self.lock = threading.RLock()
        self._detector = None
        self._last_proposal = {}
        self.projector = SceneProjector(mapping_settings)
        self._safe_path("scene-probe.jpg")
        # Only resume explicitly requested deletions. Ordinary scene images and
        # unknown user files are never part of this startup pass.
        self.pending_cleanup = []
        for scene in self.db.list("scenes", {"calibration_status": "pending_delete"}):
            result = self.delete(scene["camera_id"])
            if result["pending_delete"]:
                self.pending_cleanup.append(scene["camera_id"])

    @staticmethod
    def _link(path):
        return path.is_symlink() or getattr(path, "is_junction", lambda: False)()

    def _safe_path(self, name):
        if (not name or Path(name).name != name or "/" in name or "\\" in name
                or self._link(self.media) or self.media.resolve() != self.root
                or self._link(self.folder) or self.folder.resolve() != self.root / "scene-images"):
            raise SceneError("场景媒体路径变化或越界，已停止文件操作。")
        path = self.folder / name
        if self._link(path) or path.resolve().parent != self.folder.resolve():
            raise SceneError("场景媒体路径越界。")
        return path

    @staticmethod
    def _signature(camera):
        value = {key: camera.get(key) for key in ("source_type", "source", "config")}
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _camera(self, camera):
        current = self.db.get("cameras", str(camera.get("id") or ""))
        if not current or self._signature(current) != self._signature(camera):
            raise SceneError("摄像头配置已变化，请重新获取画面。", 409)
        try:
            source, simulated = validate_source_for_mode(self.mode, current)
        except ValueError as exc:
            raise SceneError(str(exc)) from exc
        return current, source, simulated

    def _decode(self, content):
        if not isinstance(content, bytes) or not content.startswith(b"\xff\xd8\xff") or len(content) > self.MAX_JPEG_BYTES:
            raise SceneError("场景需要非空且不超过 8 MB 的原始 JPEG 帧。")
        try:
            with Image.open(io.BytesIO(content)) as source:
                if min(source.size) < 32 or source.width * source.height > self.MAX_PIXELS:
                    raise SceneError("场景帧尺寸超出安全范围。")
                source.verify()
            frame = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise SceneError("场景帧无法解码。")
            return frame
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError, cv2.error) as exc:
            raise SceneError("场景帧损坏或无法解码。") from exc

    @staticmethod
    def _geometry(meta, width, height):
        value = meta.get("geometry")
        if not isinstance(value, dict) or value.get("coordinate_space") != "source_normalized":
            raise SceneError("缺少原始帧坐标约定。")
        if value.get("source_width") != width or value.get("source_height") != height:
            raise SceneError("场景 JPEG 与帧几何尺寸不匹配。")
        keys = ("source_width", "source_height", "target_width", "target_height", "crop_offset", "crop_size", "rotation_degrees", "mirror_x", "scale", "pad")
        try:
            return FrameGeometry(**{key: value[key] for key in keys if key in value}).to_dict()
        except (TypeError, ValueError) as exc:
            raise SceneError("场景帧几何无效。") from exc

    def _proposals(self, frame):
        # A private cached net is used only under this service's lock: never
        # share OpenCV setInput/forward state with a live detector or matcher.
        try:
            if self._detector is None:
                from services.vision.detectors.nanodet import NanoDetDetectorBackend
                self._detector = NanoDetDetectorBackend(self.model_root / "object_detection_nanodet_2022nov.onnx")
            if not self._detector.health().get("available"):
                return [], "家具建议模型不可用；可以手动画区域，不代表检测到家具。"
            height, width = frame.shape[:2]
            proposals = []
            for detection in self._detector.detect(frame):
                if detection.label not in self.LABELS or not math.isfinite(detection.confidence):
                    continue
                x, y, w, h = [float(value) for value in detection.bbox]
                if not all(math.isfinite(value) for value in (x, y, w, h)) or min(w, h) <= 0:
                    continue
                left, top = max(0., x / width), max(0., y / height)
                right, bottom = min(1., (x+w) / width), min(1., (y+h) / height)
                if right <= left or bottom <= top:
                    continue
                kind, name = self.LABELS[detection.label]
                proposals.append({"proposal_id": uuid4().hex, "name": name, "surface_type": kind,
                    "label": detection.label, "confidence": min(1., max(0., detection.confidence)),
                    "image_polygon": [[left, top], [right, top], [right, bottom], [left, bottom]],
                    "source": "model_proposal", "confirmed_by_user": False,
                    "notice": "家具框仅为建议，不是已确认支撑面；请调整并明确确认。"})
            return sorted(proposals, key=lambda value: value["confidence"], reverse=True)[:16], None
        except Exception:
            return [], "家具建议推理失败；可以手动画区域，未伪造检测结果。"

    def _persist(self, scene, *, expected, zones=None, disable=False):
        """Scene CAS and replacement of only its owned zones share one transaction."""
        def operation():
            with self.db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                raw = connection.execute("SELECT * FROM scenes WHERE id=?", (scene["id"],)).fetchone()
                old = self.db._decode("scenes", raw)
                if (old or {}).get("scene_version", 0) != expected:
                    raise SceneError("场景已被其他操作更新，请重新加载。", 409)
                current = connection.execute("SELECT * FROM cameras WHERE id=?", (scene["camera_id"],)).fetchone()
                current = self.db._decode("cameras", current)
                if not current or current.get("runtime_mode") != self.mode.value or self._signature(current) != scene["camera_signature"]:
                    raise SceneError("摄像头配置已变化，场景未保存。", 409)
                stamp = now()
                values = {"id": scene["id"], "created_at": (old or {}).get("created_at") or stamp,
                          "updated_at": stamp, **self.db._encode("scenes", scene)}
                fields = list(values)
                connection.execute('INSERT INTO scenes (' + ','.join(fields) + ') VALUES (' + ','.join('?' for _ in fields)
                    + ') ON CONFLICT(id) DO UPDATE SET ' + ','.join(f'{key}=excluded.{key}' for key in fields if key not in {'id', 'created_at'}), list(values.values()))
                rows = connection.execute("SELECT * FROM zones WHERE camera_id=?", (scene["camera_id"],)).fetchall()
                owned = [row for row in rows if (self.db._decode("zones", row).get("scene_metadata") or {}).get("scene_id") == scene["id"]]
                if zones is not None:
                    for row in owned:
                        connection.execute("DELETE FROM zones WHERE id=? AND camera_id=?", (row["id"], scene["camera_id"]))
                    for zone in zones:
                        values = {"id": zone["id"], "created_at": stamp, "updated_at": stamp, **self.db._encode("zones", zone)}
                        connection.execute('INSERT INTO zones (' + ','.join(values) + ') VALUES (' + ','.join('?' for _ in values) + ')', list(values.values()))
                elif disable:
                    for row in owned:
                        metadata = self.db._decode("zones", row)["scene_metadata"]
                        metadata.update({"confirmed_by_user": False, "calibration_status": "needs_review"})
                        connection.execute("UPDATE zones SET enabled=0,scene_metadata=?,updated_at=? WHERE id=?", (json.dumps(metadata), stamp, row["id"]))
                # Read inside the transaction: a failed follow-up read must not
                # cause propose() to delete a successfully committed keyframe.
                return self.db._decode("scenes", connection.execute("SELECT * FROM scenes WHERE id=?", (scene["id"],)).fetchone())
        return self.db._run_retry(operation)

    def get(self, camera_id):
        return self.db.get("scenes", f"scene:{self.mode.value}:{camera_id}")

    @staticmethod
    def _history(scene):
        # One previous structural version only; never recursively nest history
        # or duplicate reference image bytes. Its frame/hash remain identifiable.
        if not scene:
            return None
        return {key:deepcopy(scene.get(key)) for key in ("scene_version","snapshot_id","source_session_id",
            "source_frame","source_timestamp","screenshot_sha256","surfaces","world_geometry","calibration_status")}

    def item_markers(self, camera, states):
        with self.lock:
            current, _source, _simulated = self._camera(camera)
            scene = self.get(current["id"])
            if not scene:
                return []
            if scene["camera_signature"] != self._signature(current):
                scene = {**scene,"invalid_reason":"camera_configuration_changed"}
            return self.projector.item_markers(scene,states)

    def person_markers(self, camera, persons=None, *, now_timestamp=None):
        with self.lock:
            current, _source, _simulated = self._camera(camera)
            scene = self.get(current["id"])
            if not scene:
                return []
            if scene["camera_signature"] != self._signature(current) or scene.get("invalid_reason"):
                self.projector.reset(current["id"])
                return []
            return self.projector.person_markers(scene,persons,now_timestamp=now_timestamp)

    def propose(self, camera, packet_meta, jpeg, *, only_if_empty=False):
        with self.lock:
            camera, source, simulated = self._camera(camera)
            # The check and write share this lock. Two browser tabs (or an
            # automatic request racing a manual save) must never replace an
            # existing scene or silently invalidate its calibrated surfaces.
            if only_if_empty and self.get(camera['id']) is not None:
                raise SceneError('场景已经存在，自动建模不会覆盖现有场景。', 409)
            meta = packet_meta
            if not isinstance(meta, dict) or meta.get("runtime_mode") != self.mode.value or meta.get("source_type") != source or meta.get("is_simulated") is not simulated:
                raise SceneError("场景帧来源与当前摄像头或运行模式不一致。")
            if meta.get("camera_id") not in (None, camera["id"]):
                raise SceneError("场景帧不属于当前摄像头。")
            if (type(meta.get("source_frame")) is not int or meta["source_frame"] < 0
                    or not isinstance(meta.get("source_session_id"), str) or not 1 <= len(meta["source_session_id"]) <= 200):
                raise SceneError("缺少有效帧序号或来源会话。")
            try:
                timestamp = datetime.fromisoformat(meta["source_timestamp"])
                if timestamp.tzinfo is None:
                    raise ValueError("timezone required")
                source_timestamp = timestamp.astimezone(timezone.utc).isoformat()
                age = time.monotonic() - float(meta["created_at"])
                if not math.isfinite(age) or not 0 <= age <= self.MIN_INTERVAL_SECONDS:
                    raise ValueError("stale frame")
            except (ValueError, TypeError, KeyError) as exc:
                raise SceneError("没有新鲜且时间完整的画面，请保持相机在线后重试。", 409) from exc
            moment = time.monotonic()
            if moment - self._last_proposal.get(camera["id"], -math.inf) < self.MIN_INTERVAL_SECONDS:
                raise SceneError("场景建议至少间隔 3 秒，未重复运行模型。", 429)
            frame = self._decode(jpeg)
            height, width = frame.shape[:2]
            if meta.get("width") != width or meta.get("height") != height:
                raise SceneError("画面字节与源帧尺寸不一致。")
            geometry = self._geometry(meta, width, height)
            previous = self.get(camera["id"])
            if previous and previous.get("calibration_status") == "pending_delete":
                raise SceneError("当前场景删除尚未完成，请先重试清理。", 409)
            if previous and previous.get("source_session_id") == meta["source_session_id"] and meta["source_frame"] <= previous["source_frame"]:
                raise SceneError("不能重复使用或倒退到已处理的场景帧。", 409)
            self._last_proposal[camera["id"]] = moment
            proposals, error = self._proposals(frame)
            draft_geometry = proposal_draft(proposals)
            old_geometry = (previous or {}).get("world_geometry") or {}
            displayed_geometry = old_geometry if old_geometry.get("objects") and old_geometry.get("status") != "draft" else draft_geometry
            name = f"scene-{uuid4().hex}.jpg"
            path = self._safe_path(name)
            temporary = self._safe_path(name + ".tmp")
            scene = {"id": f"scene:{self.mode.value}:{camera['id']}", "camera_id": camera["id"],
                "scene_version": (previous or {}).get("scene_version", 0) + 1, "snapshot_id": uuid4().hex,
                "camera_signature": self._signature(camera), "geometry": geometry,
                "source_session_id": meta["source_session_id"], "source_frame": meta["source_frame"],
                "source_timestamp": source_timestamp, "source_type": source, "is_simulated": simulated,
                "runtime_mode": self.mode.value, "screenshot_path": f"/media/scene-images/{name}",
                "screenshot_sha256": hashlib.sha256(jpeg).hexdigest(), "proposals": proposals,
                "surfaces": [{**surface, "confirmed_by_user": False, "calibration_status": "needs_review"} for surface in (previous or {}).get("surfaces", [])],
                "calibration_status": "needs_confirmation", "proposal_error": error, "invalid_reason": None,
                "world_geometry":deepcopy(displayed_geometry),"previous_version":self._history(previous)}
            if scene["world_geometry"].get("status") == "draft":
                scene["world_geometry"]["reference_frame"] = {key:scene[key] for key in ("camera_id","source_session_id","source_frame","source_timestamp","snapshot_id")}
            try:
                temporary.write_bytes(jpeg)
                os.replace(temporary, path)
                result = self._persist(scene, expected=(previous or {}).get("scene_version", 0), disable=True)
                self.projector.reset(camera["id"])
            except Exception:
                self._safe_path(name).unlink(missing_ok=True)
                raise
            finally:
                self._safe_path(name + ".tmp").unlink(missing_ok=True)
            if previous and previous.get("screenshot_path"):
                old_name = previous["screenshot_path"].removeprefix("/media/scene-images/")
                try:
                    self._safe_path(old_name).unlink(missing_ok=True)
                except (OSError, SceneError):
                    result["cleanup_warning"] = "上一张场景图未能安全删除，请管理员复核。"
            return result

    @staticmethod
    def _polygon(value):
        if not isinstance(value, list) or not 3 <= len(value) <= 32:
            raise SceneError("区域必须包含 3 到 32 个归一化顶点。")
        points = []
        for point in value:
            if not isinstance(point, (list, tuple)) or len(point) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in point):
                raise SceneError("区域坐标必须为 0 到 1 的有限数值。")
            points.append([float(point[0]), float(point[1])])
        contour = np.asarray(points, np.float32)
        if len({tuple(p) for p in points}) != len(points) or not cv2.isContourConvex(contour) or cv2.contourArea(contour) < .0001:
            raise SceneError("本轮仅支持不相交、面积有效的凸多边形，请调整区域。")
        return points

    def save(self, camera, body):
        with self.lock:
            camera, _source, _simulated = self._camera(camera)
            previous = self.get(camera["id"])
            if not previous or not isinstance(body, dict):
                raise SceneError("请先从当前相机获取场景画面。", 409)
            if set(body) - {"scene_version", "snapshot_id", "source_session_id", "source_frame", "surfaces"}:
                raise SceneError("不能通过保存请求修改场景来源或标定状态。")
            if any(body.get(key) != previous.get(key) for key in ("scene_version", "snapshot_id", "source_session_id", "source_frame")) or previous["camera_signature"] != self._signature(camera):
                raise SceneError("画面或场景版本已变化，请重新加载再确认。", 409)
            if previous.get("invalid_reason") or previous.get("calibration_status") == "pending_delete":
                raise SceneError("场景已失效，需要重新获取画面并确认。", 409)
            source_image = self._safe_path(previous["screenshot_path"].removeprefix("/media/scene-images/"))
            try:
                with source_image.open("rb") as stream:
                    source_bytes = stream.read(self.MAX_JPEG_BYTES + 1)
            except OSError as exc:
                raise SceneError("场景关键帧不可读，请重新获取画面。", 409) from exc
            if len(source_bytes) > self.MAX_JPEG_BYTES or hashlib.sha256(source_bytes).hexdigest() != previous["screenshot_sha256"]:
                raise SceneError("场景关键帧不存在或校验失败，请重新获取画面。", 409)
            values = body.get("surfaces")
            if not isinstance(values, list) or len(values) > 32:
                raise SceneError("每个场景最多 32 个区域。")
            owned = {surface["surface_id"] for surface in previous.get("surfaces") or []}
            proposals = {proposal["proposal_id"]: proposal for proposal in previous.get("proposals") or []}
            surfaces, zones, ids = [], [], set()
            for value in values:
                if not isinstance(value, dict) or value.get("confirmed_by_user") is not True:
                    raise SceneError("每个支撑区域都必须由用户明确确认。")
                surface_id = value.get("surface_id")
                if surface_id and surface_id not in owned:
                    raise SceneError("不能覆盖已有用户区域或指定其他场景的区域 ID。")
                surface_id = surface_id or uuid4().hex
                if surface_id in ids:
                    raise SceneError("不能重复提交同一个区域。")
                ids.add(surface_id)
                name, kind = value.get("name"), value.get("surface_type")
                if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or kind not in self.SURFACE_TYPES:
                    raise SceneError("请填写有效名称和支撑面类型。")
                polygon = self._polygon(value.get("image_polygon"))
                map_polygon = self._polygon(value.get("map_polygon", polygon))
                if map_polygon != polygon:
                    raise SceneError("map_polygon 为旧图像示意兼容字段；独立空间坐标请使用 physical 与 calibration 对应点。")
                height = value.get("height_optional")
                if height is not None and (type(height) not in (int, float) or not math.isfinite(height) or not 0 < height <= 1_000_000):
                    raise SceneError("高度备注须为正的有限数值，不是自动测量结果。")
                proposal_id = value.get("proposal_id")
                if proposal_id and proposal_id not in proposals:
                    raise SceneError("家具建议不属于当前画面。", 409)
                surface = {"surface_id": surface_id, "name": name.strip(), "surface_type": kind,
                    "image_polygon": polygon, "map_polygon": map_polygon, "height_optional": height,
                    "height_unit": "user_annotation", "calibration_status": "schematic",
                    "source": "model_proposal" if proposal_id else "manual", "proposal_id": proposal_id,
                    "confirmed_by_user": True}
                try:
                    if value.get("physical") is not None:
                        surface["physical"] = physical_geometry(value["physical"])
                        if kind == "floor" and surface["physical"]["height"] != 0:
                            raise MappingError("The calibrated floor must use world height zero")
                        surface["surface_to_world"] = surface_matrix(surface["physical"])
                        surface["calibration_status"] = "geometry_only"
                        if value.get("calibration") is not None:
                            calibration = calibrate(value["calibration"],surface["physical"],polygon,self.projector.settings)
                            surface["calibration"] = {key:calibration[key] for key in ("image_points","plane_points","validation_points")}
                            surface.update({key:calibration[key] for key in ("image_to_surface","validation","condition_number","calibration_status")})
                    elif value.get("calibration") is not None:
                        raise MappingError("Planar calibration requires explicit confirmed physical geometry")
                except MappingError as exc:
                    raise SceneError(str(exc)) from exc
                surfaces.append(surface)
                zones.append({"id": surface_id, "camera_id": camera["id"], "name": name.strip(), "points": polygon,
                    "priority": 0, "enabled": True, "scene_metadata": {"scene_id": previous["id"],
                    "scene_version": previous["scene_version"] + 1, "surface_type": kind, "confirmed_by_user": True,
                    "calibration_status": surface["calibration_status"], "source_session_id": previous["source_session_id"]}})
            try:
                world = make_world_geometry(surfaces)
            except MappingError as exc:
                raise SceneError(str(exc)) from exc
            states = {surface["calibration_status"] for surface in surfaces}
            status = ("validation_failed" if "validation_failed" in states else
                      "calibrated" if states == {"calibrated"} else
                      "calibrated_unvalidated" if states & {"calibrated", "calibrated_unvalidated"} else
                      "geometry_only" if world["objects"] else "schematic")
            scene = {**previous, "scene_version": previous["scene_version"] + 1, "surfaces": surfaces,
                "calibration_status": status, "invalid_reason": None,"world_geometry":world,
                "previous_version":self._history(previous)}
            scene["world_geometry"]["reference_frame"] = {key:scene[key] for key in ("camera_id","source_session_id","source_frame","source_timestamp","snapshot_id")}
            result = self._persist(scene, expected=previous["scene_version"], zones=zones)
            self.projector.reset(camera["id"])
            return result

    def invalidate(self, camera_id, reason):
        with self.lock:
            previous = self.get(camera_id)
            if not previous:
                return None
            if previous.get("calibration_status") == "pending_delete":
                return previous
            # Invalidation follows a camera edit, so bind to its new signature.
            camera = self.db.get("cameras", camera_id)
            if not camera:
                return None
            scene = deepcopy(previous)
            scene.update({"scene_version": previous["scene_version"] + 1,
                "camera_signature": self._signature(camera), "calibration_status": "needs_review",
                "invalid_reason": str(reason)[:200] or "场景需重新核对","previous_version":self._history(previous)})
            for surface in scene.get("surfaces") or []:
                surface.update({"confirmed_by_user": False, "calibration_status": "needs_review"})
            result = self._persist(scene, expected=previous["scene_version"], disable=True)
            self.projector.reset(camera_id)
            return result

    def delete(self, camera_id):
        """Two-phase deletion; a failed unlink remains visible and retryable.

        This also handles a camera removed by an older app version. A hostile
        database path is reported as pending, never followed outside our folder.
        """
        with self.lock:
            previous = self.get(camera_id)
            if not previous:
                return {"deleted_scenes": 0, "deleted_images": 0, "pending_delete": False}
            expected = previous["scene_version"]
            def mark():
                with self.db.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    count = connection.execute("UPDATE scenes SET calibration_status='pending_delete',scene_version=scene_version+1,updated_at=? WHERE id=? AND runtime_mode=? AND scene_version=?",
                        (now(), previous["id"], self.mode.value, expected)).rowcount
                    if not count:
                        raise SceneError("场景正在更新，请重新执行删除。", 409)
                    for raw in connection.execute("SELECT * FROM zones WHERE camera_id=?", (camera_id,)).fetchall():
                        metadata = self.db._decode("zones", raw).get("scene_metadata") or {}
                        if metadata.get("scene_id") == previous["id"]:
                            metadata.update({"confirmed_by_user": False, "calibration_status": "pending_delete"})
                            connection.execute("UPDATE zones SET enabled=0,scene_metadata=?,updated_at=? WHERE id=?", (json.dumps(metadata), now(), raw["id"]))
            self.db._run_retry(mark)
            deleted_image = 0
            try:
                url = previous.get("screenshot_path")
                if url:
                    if not isinstance(url, str) or not url.startswith("/media/scene-images/"):
                        raise SceneError("场景图片路径不安全。")
                    path = self._safe_path(url.removeprefix("/media/scene-images/"))
                    existed = path.is_file()
                    path.unlink(missing_ok=True)
                    deleted_image = int(existed)
            except (OSError, SceneError):
                return {"deleted_scenes": 0, "deleted_images": 0, "pending_delete": True,
                        "error": "场景图片未能安全删除，已保留待清理记录；未删除外部路径。"}
            def finish():
                with self.db.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    count = connection.execute("DELETE FROM scenes WHERE id=? AND runtime_mode=? AND scene_version=? AND calibration_status='pending_delete'",
                        (previous["id"], self.mode.value, expected + 1)).rowcount
                    if not count:
                        raise SceneError("场景删除状态已变化，请管理员复核。", 409)
                    for raw in connection.execute("SELECT * FROM zones WHERE camera_id=?", (camera_id,)).fetchall():
                        if (self.db._decode("zones", raw).get("scene_metadata") or {}).get("scene_id") == previous["id"]:
                            connection.execute("DELETE FROM zones WHERE id=?", (raw["id"],))
                    return count
            try:
                deleted = self.db._run_retry(finish)
            except Exception:
                # The file is now absent but the pending row survives. Restart
                # retry treats that absence as success and finishes atomically.
                return {"deleted_scenes": 0, "deleted_images": deleted_image, "pending_delete": True,
                        "error": "场景文件已清理，数据库收尾未完成，将在重启时重试。"}
            self._last_proposal.pop(camera_id, None)
            self.projector.reset(camera_id)
            return {"deleted_scenes": deleted, "deleted_images": deleted_image, "pending_delete": False}
