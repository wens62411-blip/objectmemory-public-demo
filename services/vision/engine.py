from __future__ import annotations

import math
import queue
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from .acceptance import (
    ACCEPTANCE_POST_SECONDS,
    ACCEPTANCE_PRE_SECONDS,
    AcceptanceConfig,
    AcceptanceRun,
    AcceptanceState,
)
from .camera_sources import create_camera_source
from .capture import CaptureWorker, FramePacket, PipelineDiagnostics
from .geometry import FrameGeometry
from .hand_actions import HandActionAnalyzer
from .hand_interaction import HandObjectInteractionTracker
from .scene_guard import SceneGuard
from .person_tracking import AnonymousPersonTracker, person_configuration
from .detectors.person_pose import MediaPipePersonPose, PersonPoseConfig
from .detectors import ArucoDetectorBackend, MediaPipeHandDetector, NanoDetDetectorBackend
from .detectors.base import Detection
from .events import EventMediaWriter, ItemMotionStateMachine, MotionObservation, StateEvent
from .trackers import StableIdentityTracker, TrackedObservation
from .zones import Zone, ZoneManager


EventCallback = Callable[[dict[str, Any], np.ndarray, list[np.ndarray] | None], Any]
TrackCallback = Callable[[dict[str, Any]], None]
AcceptanceStatusCallback = Callable[[dict[str, Any]], Any]


@dataclass
class _PendingEvent:
    event: dict[str, Any]
    evidence_frame: np.ndarray
    frames: list[np.ndarray]
    started_monotonic: float
    finalize_at: float
    target_seconds: float
    clip_started_monotonic: float | None = None
    clip_last_monotonic: float | None = None
    max_frames: int | None = None
    buffered_bytes: int = 0


@dataclass(slots=True)
class RingFrame:
    """A source-bound evidence frame with legacy tuple-unpacking support."""

    monotonic_time: float
    wall_time: float
    sequence: int
    source_session_id: str
    reconnect_epoch: int
    frame: np.ndarray

    def __iter__(self):
        # Older tests and extensions used ``for timestamp, frame in ring``.
        yield self.monotonic_time
        yield self.frame


class VisionEngine:
    """A complete local capture -> detect -> track -> event pipeline."""

    def __init__(
        self,
        camera: dict[str, Any],
        items: list[dict[str, Any]],
        zones: list[dict[str, Any]],
        settings: dict[str, Any],
        on_event: EventCallback,
        on_track: TrackCallback | None = None,
        on_acceptance_status: AcceptanceStatusCallback | None = None,
        appearance_encoder=None,
    ) -> None:
        self.camera = dict(camera)
        # Preserve the configured input before adapters normalize file paths.
        # Runtime rejects late observations if a PATCH has replaced this source.
        self._observation_camera_snapshot = {
            key: deepcopy(camera.get(key)) for key in ("source_type", "source", "config")
        }
        self.items = [dict(item) for item in items]
        self.settings = dict(settings)
        self.runtime_mode = str(self.settings.get("runtime_mode") or "REAL").upper()
        self.diagnostic_mode = bool(self.settings.get("diagnostic_mode", False))
        self.source_type = self._map_source_type(
            str(self.settings.get("source_type") or self.camera.get("source_type") or "unknown"),
            bool((self.camera.get("config") or {}).get("simulated", False)),
        )
        self.is_simulated = bool(
            (self.camera.get("config") or {}).get("simulated", False)
            or self.runtime_mode in {"DEMO", "TEST"}
            or self.source_type in {"video_file", "virtual_esp32", "demo_seed", "test_fixture"}
        )
        self.on_event = on_event
        self.on_track = on_track
        self.on_acceptance_status = on_acceptance_status
        self.repo_root = Path(__file__).resolve().parents[2]
        if self.camera.get("source_type") == "video":
            source_path = Path(str(self.camera.get("source") or "demo/sample-videos/object-memory-demo.avi"))
            if not source_path.is_absolute():
                candidate = (self.repo_root / source_path).resolve()
                self.camera["source"] = str(candidate)
        self.source = create_camera_source(self.camera)
        self.capture = CaptureWorker(self.source, queue_size=int(self.settings.get("capture_queue_size", 2)), diagnostic_mode=self.diagnostic_mode)
        self.camera_id = str(self.camera.get("id") or "camera")
        self.camera_name = str(self.camera.get("name") or "摄像头")
        self.room_name = str(self.camera.get("room_name") or "未命名房间")
        self.zone_manager = ZoneManager(zones, camera_id=self.camera_id, room_name=self.room_name)
        self.item_by_id = {str(item["id"]): item for item in self.items if item.get("id") is not None}
        self.item_by_marker = {
            int(item["aruco_id"]): item for item in self.items
            if item.get("aruco_id") is not None and str(item.get("aruco_id")).isdigit()
        }
        self.aruco = ArucoDetectorBackend(self.item_by_marker)
        model_path = Path(str(self.settings.get("model_path") or self.repo_root / "data" / "models" / "object_detection_nanodet_2022nov.onnx"))
        self.experimental = NanoDetDetectorBackend(model_path, confidence=float(self.settings.get("experimental_confidence", 0.35))) if self.settings.get("detection_mode") == "experimental" else None
        from .detectors.phone_shape import PhoneShapeProposer
        self.phone_shape = PhoneShapeProposer(self.settings.get('phone_shape')) if self.settings.get('phone_shape_enabled', True) else None
        self._phone_shape_error = None
        self._profile_lock = threading.RLock()
        self._pending_profile_items = None
        self._profile_revision = 0
        self._applied_profile_revision = 0
        self._appearance_encoder = appearance_encoder
        self.reference_matcher = None
        self.reference_patches = None
        self._issued_reference_patch_detections = {}
        self.loaded_profile_versions = {}
        self._recognition_error = None
        self._reference_patch_error = None
        self._raw_model_detections = []
        self._pipeline_counts = {'processed_frames': 0, 'object_model_frames': 0, 'hand_model_frames': 0}
        self._recognition_stage_errors = {}
        self._recognition_snapshot = None
        from .preview_tracking import PreviewTracker
        self.preview_tracker = PreviewTracker(self.settings.get('preview_tracking'))
        self._tracking_snapshot = None
        self._action_snapshot = None
        self.hand_actions = HandActionAnalyzer(self.settings)
        self.hand_interactions = HandObjectInteractionTracker(self.settings)
        self._item_interactions = {}
        self._hand_action_error = None
        self._snapshot_error = None
        self._stage_timings = {}
        self._frame_geometry = None
        self.scene_requires_review = False
        self.scene_guard = SceneGuard(self.settings.get('scene_check_interval_seconds', 3), self.settings.get('scene_shift_threshold', .04))
        self._pending_scene_update = None
        self._recognition_candidates = []
        from .observation import ObservationGate
        self.observation_gate = ObservationGate(
            min_frames=self.settings.get('observation_min_frames', 3),
            max_gap=self.settings.get('max_frame_gap_seconds', .75),
            max_speed=self.settings.get('observation_max_speed', 1.5),
            min_confidence=self.settings.get('min_confidence', .55),
        )
        if self.settings.get('detection_mode') == 'experimental':
            self._load_reference_matcher()
        hand_model = Path(str(self.settings.get("hand_model_path") or self.repo_root / "data" / "models" / "hand_landmarker.task"))
        self._hand_model_path = hand_model
        self._hands_enabled = bool(self.settings.get("hand_detection_enabled", True))
        self.hands = self._new_hand_detector()
        self._hands_closed = False
        self.tracker = StableIdentityTracker(float(self.settings.get("track_recovery_seconds", 12.0)))
        self.state_machines = {
            item_id: ItemMotionStateMachine(
                min_detection_frames=int(self.settings.get("min_detection_frames", 3)),
                min_stable_frames=int(self.settings.get("min_stable_frames", 5)),
                max_frame_gap_seconds=float(self.settings.get("max_frame_gap_seconds", 0.75)),
                min_move_distance=float(self.settings.get("min_move_distance", 0.04)),
                same_zone_move_distance=float(self.settings.get("same_zone_move_distance", 0.10)),
                min_confidence=float(self.settings.get("min_confidence", 0.55)),
                stable_speed=float(self.settings.get("stable_speed", 0.025)),
                stable_position_jitter=float(self.settings.get("stable_position_jitter", 0.018)),
                min_stable_seconds=float(self.settings.get("min_stable_seconds", self.settings.get("static_seconds", 1.5))),
                placement_release_seconds=float(self.settings.get("hand_interaction_release_stable_seconds", 5.0)),
                static_seconds=float(self.settings.get("static_seconds", 1.5)),
                movement_speed=float(self.settings.get("movement_speed", 0.035)),
                occluded_seconds=float(self.settings.get("occluded_seconds", 0.6)),
                lost_seconds=float(self.settings.get("lost_seconds", 8.0)),
            )
            for item_id in self.item_by_id
        }
        data_dir = Path(str(self.settings.get("media_root") or self.settings.get("data_dir") or self.repo_root / "data"))
        self.media = EventMediaWriter(data_dir)
        self.inference_fps = max(0.5, min(30.0, float(self.camera.get("inference_fps") or self.settings.get("inference_fps", 5.0))))
        preview_config = self.camera.get("config") or {}
        requested_preview_fps = float(self.camera.get("preview_fps") or preview_config.get("preview_fps") or self.settings.get("preview_fps", 30.0))
        self.preview_target_fps = max(1.0, min(60.0, requested_preview_fps)) if math.isfinite(requested_preview_fps) else 30.0
        self._preview_max_width = max(160, min(1920, int(preview_config.get("preview_max_width", 1280))))
        self._preview_jpeg_quality = max(40, min(95, int(preview_config.get("preview_jpeg_quality", 75))))
        self.pre_seconds = max(0.0, min(10.0, float(self.settings.get("pre_seconds", 5.0))))
        self.post_seconds = max(0.0, min(10.0, float(self.settings.get("post_seconds", 5.0))))
        self.save_clips = bool(self.camera.get("save_clips", True)) and bool(self.settings.get("save_clips", True))
        self._ring_capacity = max(
            10,
            math.ceil((max(self.pre_seconds, ACCEPTANCE_PRE_SECONDS) + 2.0) * self.inference_fps),
        )
        self._ring_max_bytes = max(4, int(self.settings.get("evidence_buffer_max_mb", 64))) * 1024 * 1024
        self._evidence_max_width = max(160, int(self.settings.get("evidence_max_width", 640)))
        self._ring_bytes = 0
        self.ring: deque[RingFrame] = deque()
        self._ring_lock = threading.RLock()
        self.pending: list[_PendingEvent] = []
        self._preview_snapshot: dict[str, Any] | None = None
        self._preview_sequence = 0
        self._preview_pending_frames = 0
        self._preview_times: deque[float] = deque(maxlen=120)
        self._preview_diagnostics = PipelineDiagnostics(self.diagnostic_mode,
            ("slot_wait_ms", "arrival_interval_ms", "source_age_at_read_ms", "pending_wait_ms", "tracker_ms", "copy_ms", "draw_ms",
             "resize_ms", "banner_ms", "jpeg_ms", "latest_lock_wait_ms", "latest_lock_hold_ms", "processing_ms", "source_to_publish_ms"),
            ("published", "pending_replaced", "source_invalidated", "profile_invalidated", "duplicate_source_frame",
             "stale_input", "stale_output", "encoding_error", "stopped"),
            ("source_revisions_observed", "source_revisions_overwritten", "source_revisions_deferred"))
        self._preview_thread: threading.Thread | None = None
        self._preview_error: str | None = None
        self._latest_lock = threading.RLock()
        self._pending_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._processed = 0
        self._processing_times: deque[float] = deque(maxlen=120)
        self._inference_times: deque[float] = deque(maxlen=120)
        self._metrics_lock = threading.RLock()
        self._last_process_at = 0.0
        self._last_error: str | None = None
        self._last_track_emit: dict[str, float] = {}
        self._last_track_emit_state: dict[str, str] = {}
        self._track_history: dict[str, deque[dict[str, Any]]] = {}
        self._visual_history: dict[str, deque[tuple[int, int]]] = {}
        self._active_source_session: str | None = None
        self._active_reconnect_epoch = 0
        self._last_frame_sequence = 0
        self._media_job_capacity = max(1, int(self.settings.get("max_pending_media_jobs", 2)))
        self._max_pending_events = max(self._media_job_capacity, int(self.settings.get("max_pending_event_clips", self._media_job_capacity * 2)))
        self._media_slots = threading.BoundedSemaphore(self._media_job_capacity)
        self._media_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"media-{self.camera_id}")
        self._media_futures: set[Future[None]] = set()
        self._media_lock = threading.RLock()
        self._media_executor_closed = False
        self._event_ids: set[str] = set()
        self._simulated_fingerprints: set[tuple[Any, ...]] = set()
        self._simulated_fingerprint_order: deque[tuple[Any, ...]] = deque(maxlen=256)
        self._acceptance_lock = threading.RLock()
        self._acceptance_runs: dict[str, AcceptanceRun] = {}
        self._acceptance_run_order: deque[str] = deque(maxlen=32)
        self._acceptance_seen_run_ids: set[str] = set()
        self._current_acceptance_run_id: str | None = None
        self._acceptance_status_notifications: dict[str, tuple[str, str, int, bool]] = {}
        self._person_config = person_configuration(self.settings.get('person_pose'))
        self._person_tracker = AnonymousPersonTracker(max_people=self._person_config['max_people'],settings=self._person_config['tracking'])
        self._person_lock = threading.RLock()
        self._person_pose = None
        self._person_revision = 0
        self._person_snapshot = None
        self._person_times: deque[float] = deque(maxlen=60)
        self._person_health = {'enabled':self._person_enabled(),'phase':'waiting_for_person' if self._person_enabled() else 'disabled',
                               'available':False,'error':None,'last_latency_ms':None,'inference_fps':0.}

    def _person_enabled(self):
        return self._active_detection_mode() == 'experimental' and self._person_config['enabled'] is True

    def _new_person_pose(self):
        return MediaPipePersonPose(config=PersonPoseConfig(**{key:value for key,value in self._person_config.items() if key != 'tracking'}))

    def _reset_persons(self):
        with self._person_lock:
            self._person_revision += 1
            self._person_snapshot = None
            self._person_tracker.reset()
            self._person_times.clear()
            self._person_health['inference_fps'] = 0.

    def _close_person_pose(self):
        self._reset_persons()
        model, self._person_pose = self._person_pose, None
        error = None
        if model is not None:
            try:
                model.close()
            except Exception as exc:
                error = str(exc)
        with self._person_lock:
            self._person_times.clear()
            self._person_health.update(phase='stopped',available=False,error=error,inference_fps=0.)

    def _person_scene_version(self):
        if self.scene_requires_review or self._pending_scene_update is not None:
            return None
        zones = [zone for zone in self.zone_manager.zones if zone.enabled and zone.support_surface_confirmed]
        if any(type(zone.scene_version) is not int or zone.scene_version <= 0 for zone in zones):
            return None
        versions = {zone.scene_version for zone in zones}
        return next(iter(versions)) if len(versions) == 1 else None

    def person_snapshot(self):
        """Actual anonymous detections only; reads never load a pose model."""
        if self._stop_event.is_set() or self.capture.health().get('status') in {'error','offline','stopped','stalled','disconnected','reconnecting'}:
            self._reset_persons()
            return []
        with self._person_lock:
            snapshot = self._person_snapshot
            if snapshot is None or self._pending_scene_update is not None:
                return []
            age = time.monotonic()-snapshot['created_at']
            if not 0 <= age <= self._person_config['tracking']['snapshot_max_age_seconds']:
                return []
            rows = deepcopy(snapshot['persons'])
            if self.scene_requires_review:
                for row in rows:
                    row['scene_version'] = None
            return rows

    def _person_health_snapshot(self, capture_health, sampled_at):
        with self._person_lock:
            result = deepcopy(self._person_health)
            snapshot = self._person_snapshot
            fresh = snapshot is not None and 0 <= sampled_at-snapshot['created_at'] <= self._person_config['tracking']['snapshot_max_age_seconds']
            unavailable = self._stop_event.is_set() or capture_health.get('status') in {'error','offline','stopped','stalled','disconnected','reconnecting'}
            result['current_person_count'] = len(snapshot['persons']) if fresh and not unavailable else 0
            result['inference_fps'] = self._measured_rate(list(self._person_times),sampled_at) if fresh and not unavailable else 0.
            if self._person_enabled() and unavailable and result.get('phase') != 'stopped':
                result['phase'] = 'source_unavailable'
            elif self._person_enabled() and snapshot is not None and not fresh:
                result['phase'] = 'stale'
            return result

    def _process_persons(self, frame, detections, packet):
        # This optional, independent path must never erase object/hand results,
        # including unexpected native model failures or malformed output.
        try:
            self._process_person_frame(frame,detections,packet)
        except Exception as exc:
            self._reset_persons()
            with self._person_lock:
                self._person_health.update(phase='failed',error=f'人物处理失败：{str(exc)[:240]}',last_latency_ms=None)

    def _process_person_frame(self, frame, detections, packet):
        if not self._person_enabled():
            self._reset_persons()
            return
        started = time.perf_counter()
        with self._person_lock:
            revision = self._person_revision
        height,width = frame.shape[:2]
        boxes = []
        for detection in detections:
            if detection.label != 'person':
                continue
            try:
                x,y,w,h = [float(value) for value in detection.bbox]
                score = float(detection.confidence)
                if not all(math.isfinite(v) for v in (x,y,w,h,score)) or min(w,h) <= 0 or not 0 <= score <= 1:
                    continue
                left,top,right,bottom = max(0.,x/width),max(0.,y/height),min(1.,(x+w)/width),min(1.,(y+h)/height)
                if right <= left or bottom <= top:
                    continue
                boxes.append({'label':'person','bbox':[left,top,right-left,bottom-top],
                              'confidence':score,'detector_backend':(detection.metadata or {}).get('backend') or 'nanodet'})
            except (TypeError,ValueError):
                continue
        boxes = sorted(boxes,key=lambda value:value['confidence'],reverse=True)[:self._person_config['max_people']]
        rows = [{**box,'feet_visible':False,'foot_point':None,'foot_confidence':None,'posture':'unknown',
                 'position_status':'ground_unconfirmed','ground_contact_confirmed':False,'pose_status':'no_pose'} for box in boxes]
        error = None
        if boxes:
            try:
                if self._person_pose is None:
                    with self._person_lock:
                        self._person_health.update(phase='loading',error=None)
                    self._person_pose = self._new_person_pose()
                posed = self._person_pose.detect(frame,[{'label':'person','bbox':box['bbox'],'confidence':box['confidence']} for box in boxes])
                if len(posed) != len(boxes):
                    raise ValueError('person pose result count does not match same-frame detector boxes')
                for index,(row,pose) in enumerate(zip(rows,posed)):
                    if pose.get('person_index') != index or pose.get('bbox') != row['bbox']:
                        raise ValueError('person pose result is not bound to the original person box')
                    visibility,presence = pose.get('foot_min_visibility'),pose.get('foot_min_presence')
                    confidence = min(visibility,presence) if all(type(v) in (int,float) and math.isfinite(v) and 0 <= v <= 1 for v in (visibility,presence)) else None
                    upright = pose.get('posture_state') == 'upright_candidate'
                    row.update({**pose,'detector_backend':row['detector_backend'],
                        'feet_visible':pose.get('foot_point_visible') is True and upright and confidence is not None,
                        'foot_confidence':confidence,'posture':'standing' if upright else 'unknown'})
                model_health = self._person_pose.health()
                error = model_health.get('error')
                with self._person_lock:
                    if model_health.get('last_latency_ms') is not None:
                        self._person_times.append(time.monotonic())
                    self._person_health = {**model_health,'phase':'failed' if error else model_health.get('status','ready'),
                        'enabled':True,'inference_fps':self._measured_rate(list(self._person_times),time.monotonic())}
            except Exception as exc:
                error = f'人物姿态处理失败：{str(exc)[:240]}'
                # Keep real semantic body boxes, but discard any partial pose.
                rows = [{**box,'feet_visible':False,'foot_point':None,'foot_confidence':None,'posture':'unknown',
                         'position_status':'ground_unconfirmed','ground_contact_confirmed':False,'pose_status':'failed','reason':error} for box in boxes]
        else:
            with self._person_lock:
                self._person_health.update(phase='no_person',last_latency_ms=None,inference_fps=0.)
        with self._person_lock:
            self._stage_timings['person_pose'] = round((time.perf_counter()-started)*1000,2)
            if error:
                self._person_health.update(phase='failed',error=error,last_latency_ms=None,inference_fps=0.)
            if revision != self._person_revision or self._stop_event.is_set() or self._pending_scene_update is not None:
                return
            try:
                tracked = self._person_tracker.update(rows,packet.source_session_id,packet.sequence,packet.monotonic_time)
            except Exception as exc:
                self._person_tracker.reset()
                tracked = []
                self._person_health.update(phase='failed',error=f'人物短期跟踪失败：{exc}')
            source = {'camera_id':self.camera_id,'source_session_id':packet.source_session_id,'source_frame':packet.sequence,
                'source_timestamp':datetime.fromtimestamp(packet.wall_time,timezone.utc).isoformat(),'runtime_mode':self.runtime_mode,
                'source_type':self.source_type,'is_simulated':self.is_simulated,'scene_version':self._person_scene_version(),
                'coordinate_space':'source_normalized','bbox_format':'source_normalized_xywh'}
            self._person_snapshot = {'created_at':packet.monotonic_time,'persons':[{**row,**source} for row in tracked]}

    @staticmethod
    def _map_source_type(source_type: str, simulated: bool) -> str:
        value = str(source_type).strip().lower()
        if simulated and value in {"esp32", "mjpeg", "virtual_esp32"}:
            return "virtual_esp32"
        return {
            "webcam": "opencv_camera",
            "opencv": "opencv_camera",
            "browser": "browser_camera",
            "browser_camera": "browser_camera",
            "rtsp": "rtsp",
            "onvif": "onvif",
            "mjpeg": "mjpeg",
            "esp32": "esp32_real",
            "esp32_real": "esp32_real",
            "screen": "authorized_screen_capture",
            "authorized_screen_capture": "authorized_screen_capture",
            "video": "video_file",
            "video_file": "video_file",
            "virtual_esp32": "virtual_esp32",
            "demo_seed": "demo_seed",
            "test_fixture": "test_fixture",
        }.get(value, value or "unknown")

    def _active_detection_mode(self) -> str:
        requested = str(self.settings.get("detection_mode", "aruco"))
        # A failed photo model is an explicit unavailable photo model. It is
        # never permission to use a marker and call it photo recognition.
        return "experimental" if requested == "experimental" else "aruco"

    def _load_reference_matcher(self):
        try:
            from .detectors.appearance import AppearanceEncoder, ProfileMatcher
            if self._appearance_encoder is None:
                self._appearance_encoder = AppearanceEncoder(self.repo_root / 'data/models/dinov2-small.onnx')
            self.reference_matcher = ProfileMatcher(
                self.items, self._appearance_encoder,
                threshold=float(self.settings.get('reference_match_threshold', .82)),
                margin=float(self.settings.get('reference_match_margin', .06)),
            )
            available = self._appearance_encoder.health().get('available')
            self.loaded_profile_versions = dict(self.reference_matcher.loaded_profile_versions) if available else {}
            self._recognition_error = self._appearance_encoder.health().get('error')
            self.reference_patches = None
            self._reference_patch_error = None
            if available and self.settings.get('reference_patch_enabled', True):
                from .detectors.reference_patch import ReferencePatchProposer
                try:
                    self.reference_patches = ReferencePatchProposer(self.items, self._appearance_encoder,
                        reference_root=Path(self.settings.get('reference_root') or self.repo_root / 'data/registered-items'),
                        settings=self.settings.get('reference_patch'))
                except Exception as exc:
                    # Exemplar-localization failure is not permission to discard
                    # a valid category detector + CLS identity matcher.
                    self._reference_patch_error = f'参考局部定位加载失败：{exc}'
        except Exception as exc:
            self.reference_matcher = None
            self.reference_patches = None
            self.loaded_profile_versions = {}
            self._recognition_error = f'外观模型不可用：{exc}'

    def reload_profiles(self, items, appearance_encoder=None, enable=False):
        """Apply at an inference frame boundary, without reopening the camera."""
        with self._profile_lock:
            self._profile_revision += 1
            if self._pending_profile_items is not None:
                _old_items, old_encoder, old_enable = self._pending_profile_items
                appearance_encoder = appearance_encoder or old_encoder
                enable = enable or old_enable
            self._pending_profile_items = (deepcopy(items), appearance_encoder, bool(enable))
            self.loaded_profile_versions = {}
            self._recognition_snapshot = None
            self.preview_tracker.invalidate()
            with self._latest_lock:
                self._tracking_snapshot = None
        # Unsubmitted photo episodes must not outlive the identity generation.
        # Already submitted jobs perform the same check immediately before the
        # backend callback, whose transaction independently checks the profile.
        with self._pending_lock:
            self.pending = [pending for pending in self.pending
                if pending.event.get('detection_mode') != 'experimental'
                or pending.event.get('profile_generation') == self._profile_revision]

    def _apply_profile_updates(self):
        with self._profile_lock:
            pending, self._pending_profile_items = self._pending_profile_items, None
            revision = self._profile_revision
        if pending is None:
            return
        items, encoder, enable = pending
        if encoder is not None:
            self._appearance_encoder = encoder
        self.items = items
        self.item_by_id = {str(item['id']): item for item in items}
        self.item_by_marker = {int(item['aruco_id']): item for item in items if item.get('aruco_id') is not None}
        self.aruco = ArucoDetectorBackend(self.item_by_marker)
        if enable:
            self.settings['detection_mode'] = 'experimental'
        if self._active_detection_mode() == 'experimental':
            if self.experimental is None:
                model = self.settings.get('model_path') or self.repo_root / 'data/models/object_detection_nanodet_2022nov.onnx'
                self.experimental = NanoDetDetectorBackend(model, confidence=float(self.settings.get('experimental_confidence', .35)))
            self._load_reference_matcher()
        # New appearance evidence changes identity semantics. Re-establish a
        # baseline; never turn a profile edit into a movement episode.
        template = dict(self.settings)
        self.state_machines = {item_id: ItemMotionStateMachine(
            min_detection_frames=int(template.get('min_detection_frames', 3)),
            min_stable_frames=int(template.get('min_stable_frames', 5)),
            max_frame_gap_seconds=float(template.get('max_frame_gap_seconds', .75)),
            min_move_distance=float(template.get('min_move_distance', .04)),
            same_zone_move_distance=float(template.get('same_zone_move_distance', .10)),
            min_confidence=float(template.get('min_confidence', .55)),
            stable_speed=float(template.get('stable_speed', .025)),
            stable_position_jitter=float(template.get('stable_position_jitter', .018)),
            min_stable_seconds=float(template.get('min_stable_seconds', 1.5)),
            placement_release_seconds=float(template.get('hand_interaction_release_stable_seconds', 5.0)),
            static_seconds=float(template.get('static_seconds', 1.5)),
            movement_speed=float(template.get('movement_speed', .035)),
            occluded_seconds=float(template.get('occluded_seconds', .6)),
            lost_seconds=float(template.get('lost_seconds', 8.0)),
        ) for item_id in self.item_by_id}
        self.tracker.reset()
        self._last_track_emit.clear()
        self._last_track_emit_state.clear()
        self._track_history.clear()
        self._visual_history.clear()
        self.observation_gate.reset()
        self._reset_hand_actions()
        with self._profile_lock:
            self._applied_profile_revision = revision
            if revision != self._profile_revision:
                self.loaded_profile_versions = {}

    def recognition_snapshot(self):
        capture = self.capture.health()
        if capture.get('status')=='error':
            return None
        with self._profile_lock:
            snapshot = self._recognition_snapshot
            if snapshot is None or self._applied_profile_revision != self._profile_revision or self._stop_event.is_set() or time.monotonic() - snapshot['created_at'] > 3:
                return None
            if capture.get('capture_thread_alive') and (
                capture.get('status') not in {'online', 'ready'}
                or capture.get('source_session_id') != snapshot.get('source_session_id')
                or capture.get('reconnect_epoch') != snapshot.get('reconnect_epoch')
            ):
                return None  # A recent result can still belong to the pre-reconnect source.
            return deepcopy(snapshot)

    def _recognition_diagnostics(self, packet: FramePacket) -> dict:
        """Same-frame bounded facts, never a second inference or generated boxes."""
        capture = self.capture.health()
        accepted = sum(row.get('accepted') is True for row in self._recognition_candidates)
        value = {'source_session_id': packet.source_session_id, 'source_frame': packet.sequence,
            'profile_generation': self._applied_profile_revision,
            'capture_frames_received': capture.get('source_frame_sequence')
                if capture.get('source_session_id') == packet.source_session_id else None,
            **self._pipeline_counts, 'loaded_profile_count': len(self.loaded_profile_versions),
            'loaded_profile_versions': dict(self.loaded_profile_versions),
            'raw_detection_count': len(self._raw_model_detections),
            'raw_object_count': sum(row['category'] != 'person' for row in self._raw_model_detections),
            'candidate_count': len(self._recognition_candidates), 'identity_accepted_count': accepted,
            'identity_rejected_count': len(self._recognition_candidates) - accepted,
            'stage_errors': dict(self._recognition_stage_errors), 'stage_timings_ms': dict(self._stage_timings)}
        if self.diagnostic_mode:
            value['raw_detections'] = deepcopy(self._raw_model_detections[:100])
            value['raw_detections_truncated'] = len(self._raw_model_detections) > 100
        return value

    def tracking_snapshot(self):
        """One latest measured preview, not a persisted observation or event."""
        if self._stop_event.is_set() or self.capture.health().get('status') == 'error':
            return None
        with self._latest_lock:
            snapshot = self._tracking_snapshot
            if snapshot is None or time.monotonic()-snapshot['created_at'] > .75:
                return None
            return {**snapshot,'candidates':[row for row in snapshot['candidates']
                if row.get('profile_generation') == self._profile_revision == self._applied_profile_revision]}

    def _reset_hand_actions(self):
        self.hand_actions.reset()
        self.hand_interactions.reset()
        self._item_interactions.clear()
        with self._profile_lock:
            self._action_snapshot = None

    def _safe_hand_health(self):
        try:
            return self.hands.health()
        except Exception as exc:
            return {'enabled': self._hands_enabled, 'available': False, 'status': 'failed',
                    'error': f'手部状态读取失败：{exc}'}

    def _update_hand_interaction(self, item_id, bbox, actions, packet, *, detected=True, healthy=True):
        try:
            return self.hand_interactions.update(item_id, bbox, actions, packet.monotonic_time,
                packet.source_session_id, packet.sequence, detected=detected, hand_model_healthy=healthy)
        except Exception as exc:
            # Auxiliary failure cannot erase an accepted object observation or
            # turn the last released result into evidence for this frame.
            self._hand_action_error = f'手物动作分析失败：{exc}'
            prior = self._item_interactions.get(item_id, {})
            self.hand_interactions.forget(item_id)
            return {'hand_id': None, 'holding_status': 'holding_uncertain' if prior.get('holding_status') in
                    {'co_moving','holding_uncertain','release_candidate'} else 'not_established',
                    'release_observed': False, 'co_motion_frames': 0, 'reason': 'interaction_analysis_failed',
                    'source_session_id': packet.source_session_id, 'source_frame': packet.sequence,
                    'source_timestamp': packet.monotonic_time, 'item_detected': detected,
                    'hand_model_healthy': False}

    def action_snapshot(self):
        """Small, same-source metadata only; no JPEG copying or extra inference."""
        health = self._safe_hand_health()
        empty = {'camera_id': self.camera_id, 'fresh': False, 'hands': [], 'interactions': [],
                 'hand_status': health, 'profile_count': len(self.loaded_profile_versions),
                 'source_session_id': None, 'source_frame': None, 'timestamp': None}
        capture = self.capture.health()
        with self._profile_lock:
            snapshot = self._action_snapshot
            now = time.monotonic()
            limit = max(1., min(3., 2.5/self.inference_fps))
            diagnostics = self._action_freshness_diagnostics(snapshot, capture, now, limit)
            if diagnostics['freshness_reason'] != 'fresh':
                # Keep unavailable source identifiers/actions empty. Numerical
                # diagnostics describe the rejected snapshot, never evidence.
                return {**empty, 'reason': 'stale_frame', 'diagnostics': diagnostics}
            result = deepcopy(snapshot)
        for key in ('created_at', 'published_at', 'inference_started_at'):
            result.pop(key, None)
        return {**empty, **result, 'fresh': True, 'diagnostics': diagnostics}

    def _action_freshness_diagnostics(self, snapshot, capture, now, limit):
        """Bounded metadata from one snapshot read; never copy health errors."""
        row = snapshot or {}
        def number(value):
            return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e12 else None
        def milliseconds(later, earlier):
            a, b = number(later), number(earlier)
            value = (a-b)*1000 if a is not None and b is not None else None
            return round(value, 3) if value is not None and 0 <= value <= 1e9 else None
        source_age = milliseconds(now, row.get('created_at'))
        session_matches = bool(row.get('source_session_id') and
                               row.get('source_session_id') == capture.get('source_session_id')) if snapshot else None
        epoch_matches = (row.get('reconnect_epoch') == capture.get('reconnect_epoch')
                         and type(row.get('reconnect_epoch')) is int) if snapshot else None
        status = capture.get('status')
        if not isinstance(status, str) or status not in {'online', 'ready', 'stopped', 'ended', 'error', 'offline', 'recovering',
                          'reconnecting', 'disconnected', 'starting', 'connecting', 'frame_read_failed'}:
            status = 'unknown'
        if snapshot is None:
            reason = 'no_snapshot'
        elif self._stop_event.is_set():
            reason = 'engine_stopped'
        elif not self._applied_profile_revision == self._profile_revision == row.get('profile_generation'):
            reason = 'profile_changed'
        elif status not in {'online', 'ready'}:
            reason = 'capture_unavailable'
        elif not session_matches:
            reason = 'source_session_changed'
        elif not epoch_matches:
            reason = 'reconnect_epoch_changed'
        elif source_age is None:
            reason = 'invalid_source_timestamp'
        # Compare raw monotonic values, not the rounded diagnostic value.
        elif now-row['created_at'] > limit:
            reason = 'source_frame_expired'
        else:
            reason = 'fresh'
        return {'freshness_reason': reason, 'freshness_limit_ms': round(limit*1000, 3),
            'source_age_ms': source_age, 'result_age_ms': milliseconds(now, row.get('published_at')),
            'inference_duration_ms': milliseconds(row.get('published_at'), row.get('inference_started_at')),
            'source_to_result_ms': milliseconds(row.get('published_at'), row.get('created_at')),
            'capture_status': status, 'capture_source_frame': number(capture.get('source_frame_sequence')),
            'snapshot_source_frame': number(row.get('source_frame')),
            'profile_generation': number(self._profile_revision),
            'applied_profile_generation': number(self._applied_profile_revision),
            'snapshot_profile_generation': number(row.get('profile_generation')),
            'source_session_matches': session_matches, 'reconnect_epoch_matches': epoch_matches}

    def update_scene(self, zones, frame=None, *, confirmed=True):
        self._reset_persons()
        with self._profile_lock:
            self._pending_scene_update = (deepcopy(zones), frame.copy() if frame is not None else None, confirmed)

    def _apply_scene_update(self):
        with self._profile_lock:
            update, self._pending_scene_update = self._pending_scene_update, None
        if update is None:
            return
        zones, frame, confirmed = update
        self.zone_manager.replace(zones)
        self.zone_manager.reset_tracks()
        self.scene_requires_review = not confirmed
        if frame is not None:
            self.scene_guard.establish(frame)
        self._finalize_all(stopped_early=True)
        self._reset_hand_actions()
        for machine in self.state_machines.values():
            machine.reset_source_session(self._active_source_session or '', self._active_reconnect_epoch, 'scene_edited')

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        if self._preview_thread and self._preview_thread.is_alive():
            self._last_error = "上一代预览线程仍在释放，暂不重新打开摄像头。"
            return False
        if self._media_executor_closed:
            self._media_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"media-{self.camera_id}")
            self._media_executor_closed = False
        try:
            connected = self.capture.start()
        except Exception:
            self.capture.stop()
            self._close_hands()
            self._close_person_pose()
            raise
        if not connected:
            self._last_error = self.capture.error
            self.capture.stop()
            self._close_hands()
            self._close_person_pose()
            return False
        if self._hands_closed:
            self.hands = self._new_hand_detector()
            self._hands_closed = False
        self._stop_event.clear()
        self._reset_persons()
        with self._person_lock:
            self._person_health.update(phase='waiting_for_person' if self._person_enabled() else 'disabled',error=None)
        with self._latest_lock:
            self._preview_snapshot = None
            self._tracking_snapshot = None
            self._preview_times.clear()
            self._preview_error = None
        self.preview_tracker.invalidate()
        with self._metrics_lock:
            self._inference_times.clear()
            self._processing_times.clear()
        self._preview_thread = threading.Thread(target=self._run_preview, name=f"preview-{self.camera_id}", daemon=True)
        self._preview_thread.start()
        self._thread = threading.Thread(target=self._run, name=f"vision-{self.camera_id}", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop_event.set()
        self._interrupt_acceptance("vision_engine_stopped")
        self.capture.stop()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self._thread = None
        preview_thread = self._preview_thread
        if preview_thread and preview_thread is not threading.current_thread():
            preview_thread.join(timeout=2.0)
        self._preview_thread = preview_thread if preview_thread and preview_thread.is_alive() else None
        with self._latest_lock:
            self._preview_snapshot = None
            self._tracking_snapshot = None
            self._preview_times.clear()
        self.preview_tracker.invalidate()
        self._finalize_all(stopped_early=True)
        if not self._media_executor_closed:
            self._media_executor.shutdown(wait=True, cancel_futures=False)
            self._media_executor_closed = True
        self._close_hands()
        self._close_person_pose()

    def _new_hand_detector(self):
        return MediaPipeHandDetector(enabled=self._hands_enabled, model_path=self._hand_model_path,
            max_hands=int(self.settings.get('max_hands', 2)),
            min_detection_confidence=float(self.settings.get('hand_detection_confidence', .55)),
            min_presence_confidence=float(self.settings.get('hand_presence_confidence', .5)),
            min_tracking_confidence=float(self.settings.get('hand_tracking_confidence', .5)))

    def _close_hands(self) -> None:
        if not self._hands_closed:
            self._hands_closed = True
            self.hands.close()

    def get_jpeg(self) -> bytes | None:
        snapshot = self.get_preview_snapshot()
        return snapshot["jpeg"] if snapshot else None

    def get_preview_snapshot(self) -> dict[str, Any] | None:
        """Return JPEG and its actual source identity atomically.

        ``created_at`` is monotonic seconds, ``source_timestamp`` Unix seconds.
        Reading this shared cache neither captures nor encodes another frame.
        """
        if self.capture.health().get('status')=='error':
            return None
        with self._latest_lock:
            if self._preview_snapshot is None or self._stop_event.is_set():
                return None
            if time.monotonic() - self._preview_snapshot["created_at"] > 2.0:
                return None
            return dict(self._preview_snapshot)

    @staticmethod
    def _measured_rate(timestamps: list[float], sampled_at: float) -> float:
        recent = [value for value in timestamps if sampled_at - value <= 2.0]
        if len(recent) < 2 or recent[-1] <= recent[0]:
            return 0.0
        return (len(recent) - 1) / (recent[-1] - recent[0])

    def _run_preview(self) -> None:
        """Encode newest capture frames without entering the inference pipeline."""
        revision = 0
        next_at = 0.0
        pending = None
        last_published = None
        self._preview_pending_frames = 0
        interval = 1.0 / self.preview_target_fps
        diagnostics = getattr(self, '_preview_diagnostics', None)
        diagnostic = diagnostics is not None and diagnostics.enabled
        if diagnostic:
            diagnostics.clear()
        previous_arrival = None
        while not self._stop_event.is_set():
            # QPC controls preview deadlines only; packet age remains on the
            # unchanged source monotonic clock. Half-frame tolerance alone did
            # not save early burst frames. Keep ONE unpublished latest packet
            # until its deadline, replacing it on fresh source notifications.
            wait_started = time.perf_counter()
            timeout = min(.25, max(0.0, next_at-wait_started)) if pending else .25
            latest = self.capture.preview_frames.latest(revision, timeout=timeout)
            arrived_at = time.perf_counter()
            if latest is not None:
                previous_revision = revision
                revision, packet = latest
                if pending and diagnostic:
                    previous_packet, previous_generation, previous_at, previous_metrics, previous_counts = pending
                    outcome = 'source_invalidated' if previous_packet.source_session_id != packet.source_session_id else 'profile_invalidated' if previous_generation != self._profile_revision else 'pending_replaced'
                    previous_metrics['pending_wait_ms'] = (arrived_at-previous_at)*1000
                    diagnostics.record(outcome, previous_metrics, previous_counts)
                metrics = counts = None
                if diagnostic:
                    metrics = {'slot_wait_ms': (arrived_at-wait_started)*1000,
                        'source_age_at_read_ms': (time.monotonic()-packet.monotonic_time)*1000}
                    if previous_arrival is not None:
                        metrics['arrival_interval_ms'] = (arrived_at-previous_arrival)*1000
                    previous_arrival = arrived_at
                    counts = {'source_revisions_observed': 1,
                        'source_revisions_overwritten': max(0, revision-previous_revision-1),
                        'source_revisions_deferred': int(arrived_at+1e-9 < next_at)}
                pending = (packet, self._profile_revision, arrived_at, metrics, counts)
                self._preview_pending_frames = 1
            if pending is None or arrived_at+1e-9 < next_at:
                continue
            packet, preview_revision, pending_at, metrics, counts = pending
            pending = None
            self._preview_pending_frames = 0
            measured_started = time.perf_counter()
            started = time.monotonic()
            if diagnostic:
                metrics['pending_wait_ms'] = (measured_started-pending_at)*1000
            capture_stopped = getattr(self.capture, 'stop_event', None)
            if (self._stop_event.is_set() or (capture_stopped is not None and capture_stopped.is_set())
                    or getattr(self.capture, 'source_session_id', packet.source_session_id) != packet.source_session_id):
                if diagnostic:
                    diagnostics.record('source_invalidated', metrics, counts)
                continue
            if preview_revision != self._profile_revision:
                if diagnostic:
                    diagnostics.record('profile_invalidated', metrics, counts)
                continue
            if last_published and packet.source_session_id == last_published[0] and packet.sequence <= last_published[1]:
                if diagnostic:
                    diagnostics.record('duplicate_source_frame', metrics, counts)
                continue
            if started - packet.monotonic_time > 2.0:
                if diagnostic:
                    diagnostics.record('stale_input', metrics, counts)
                continue
            # Preserve phase for small wakeup jitter, but never accumulate
            # catch-up credits after a long stall. No pending means no replay.
            next_at = next_at + interval if next_at and measured_started-next_at < interval else measured_started + interval
            try:
                tracker_started = time.perf_counter() if diagnostic else 0.0
                tracked_candidates = self.preview_tracker.update(packet.frame, packet.source_session_id,
                    packet.sequence, packet.monotonic_time)
                copy_started = time.perf_counter() if diagnostic else 0.0
                frame = packet.frame.copy()
                draw_started = time.perf_counter() if diagnostic else 0.0
                # Static zones may be drawn on a fresh preview. Old detection
                # boxes must not be pasted onto a different source frame.
                self._draw_zones(frame)
                resize_started = time.perf_counter() if diagnostic else 0.0
                if frame.shape[1] > self._preview_max_width:
                    height = max(1, round(frame.shape[0] * self._preview_max_width / frame.shape[1]))
                    frame = cv2.resize(frame, (self._preview_max_width, height), interpolation=cv2.INTER_AREA)
                banner_started = time.perf_counter() if diagnostic else 0.0
                label = "SIMULATED SOURCE - LIVE PREVIEW" if self.is_simulated else "LIVE PREVIEW - INFERENCE SAMPLED SEPARATELY"
                if self.camera.get("source_type") == "screen":
                    label = "AUTHORIZED SCREEN REGION - LIVE PREVIEW"
                cv2.rectangle(frame, (0, 0), (min(frame.shape[1], 610), 30), (250, 250, 250), -1)
                cv2.putText(frame, label, (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (30, 80, 110), 1, cv2.LINE_AA)
                jpeg_started = time.perf_counter() if diagnostic else 0.0
                ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self._preview_jpeg_quality])
                if diagnostic:
                    encoded_at = time.perf_counter()
                    metrics.update(tracker_ms=(copy_started-tracker_started)*1000,
                        copy_ms=(draw_started-copy_started)*1000, draw_ms=(resize_started-draw_started)*1000,
                        resize_ms=(banner_started-resize_started)*1000, banner_ms=(jpeg_started-banner_started)*1000,
                        jpeg_ms=(encoded_at-jpeg_started)*1000)
                if not ok:
                    raise RuntimeError("JPEG 编码失败")
                if self._stop_event.is_set():
                    if diagnostic:
                        diagnostics.record('stopped', metrics, counts)
                    break
                published_at = time.monotonic()
                if published_at - packet.monotonic_time > 2.0:
                    if diagnostic:
                        diagnostics.record('stale_output', metrics, counts)
                    continue
                if (getattr(self.capture, 'source_session_id', packet.source_session_id) != packet.source_session_id
                        or (capture_stopped is not None and capture_stopped.is_set())):
                    if diagnostic:
                        diagnostics.record('source_invalidated', metrics, counts)
                    continue
                jpeg = encoded.tobytes()
                lock_started = time.perf_counter() if diagnostic else 0.0
                with self._latest_lock:
                    lock_acquired = time.perf_counter() if diagnostic else 0.0
                    self._preview_sequence += 1
                    self._preview_times.append(published_at)
                    self._preview_snapshot = {
                        "jpeg": jpeg,
                        "sequence": self._preview_sequence,
                        "source_frame_sequence": packet.sequence,
                        "source_session_id": packet.source_session_id,
                        "source_timestamp": packet.wall_time,
                        "created_at": published_at,
                    }
                    self._tracking_snapshot = {
                        'jpeg': jpeg, 'created_at': published_at, 'width': frame.shape[1], 'height': frame.shape[0],
                        'camera_id': self.camera_id, 'source_frame': packet.sequence,
                        'source_session_id': packet.source_session_id,
                        'source_timestamp': datetime.fromtimestamp(packet.wall_time, timezone.utc).isoformat(),
                        'runtime_mode': self.runtime_mode, 'source_type': self.source_type, 'is_simulated': self.is_simulated,
                        'candidates': [row for row in tracked_candidates if row.get('profile_generation') == self._profile_revision == self._applied_profile_revision] if preview_revision == self._profile_revision else [], 'hands': [],
                        'hand_status': {'status': 'sampled_separately'},
                        'coordinate_space': 'source_normalized', 'display_only': True,
                        'tracker_backend': 'lk_forward_backward',
                        'inference_fps': self._measured_rate(list(self._inference_times), published_at),
                        'preview_target_fps': self.preview_target_fps,
                        'model': {'candidate_backend': 'nanodet' if self._active_detection_mode() == 'experimental' else 'aruco',
                                  'registered_reference_backend': self.reference_patches.backend if self.reference_patches else None,
                                  'identity_backend': 'dinov2' if self.reference_matcher is not None else 'marker_id' if self._active_detection_mode() == 'aruco' else 'unavailable'},
                    }
                    self._preview_error = None
                last_published = (packet.source_session_id, packet.sequence)
                if diagnostic:
                    completed = time.perf_counter()
                    metrics.update(latest_lock_wait_ms=(lock_acquired-lock_started)*1000,
                        latest_lock_hold_ms=(completed-lock_acquired)*1000,
                        processing_ms=(completed-measured_started)*1000,
                        source_to_publish_ms=((started-packet.monotonic_time)+(completed-measured_started))*1000)
                    diagnostics.record('published', metrics, counts)
            except Exception as exc:
                # Preview failures do not change evidence or kill inference.
                with self._latest_lock:
                    self._preview_error = f"预览编码失败：{exc}"
                if diagnostic:
                    metrics['processing_ms'] = (time.perf_counter()-measured_started)*1000
                    diagnostics.record('encoding_error', metrics, counts)
        if pending and diagnostic:
            diagnostics.record('stopped', pending[3], pending[4])
        self._preview_pending_frames = 0

    def _ring_seconds(self, source_session_id: str | None = None) -> float:
        with self._ring_lock:
            entries = [
                entry for entry in self.ring
                if source_session_id is None or entry.source_session_id == source_session_id
            ]
            if len(entries) < 2:
                return 0.0
            return max(0.0, entries[-1].monotonic_time - entries[0].monotonic_time)

    def _acceptance_status_locked(self, run: AcceptanceRun) -> dict[str, Any]:
        value = run.status()
        value["ring_seconds"] = round(self._ring_seconds(run.config.source_session_id), 6)
        return value

    def _notify_acceptance_status_locked(self, run: AcceptanceRun) -> None:
        """Persist terminal engine truth without depending on browser polling.

        The callback is intentionally best-effort and deduplicated.  A status
        mirror failure must never turn an otherwise healthy capture loop into
        a source restart, but it is exposed through engine health.
        """
        if self.on_acceptance_status is None:
            return
        signature = (run.state.value, run.reason, int(run.event_count), bool(run.completed_without_event))
        if self._acceptance_status_notifications.get(run.config.validation_run_id) == signature:
            return
        try:
            persisted = self.on_acceptance_status(self._acceptance_status_locked(run))
            if persisted is not None and persisted is not False:
                self._acceptance_status_notifications[run.config.validation_run_id] = signature
        except Exception as exc:
            self._last_error = f"验收终态落库失败：{exc}"

    def _interrupt_acceptance_run_locked(
        self,
        run: AcceptanceRun,
        reason: str,
        *,
        wall_time: float | None = None,
        monotonic_time: float | None = None,
    ) -> None:
        run.interrupt(reason, wall_time=wall_time, monotonic_time=monotonic_time)
        if run.terminal:
            self._notify_acceptance_status_locked(run)

    def arm_acceptance(self, config: dict[str, Any]) -> dict[str, Any]:
        """Arm one authenticated REAL/OpenCV validation run.

        The API remains responsible for authorizing the caller.  The vision
        boundary independently verifies that its actual, currently running
        source and immutable source-session binding match the supplied config.
        """
        parsed = AcceptanceConfig.from_dict(config)
        if self.runtime_mode != "REAL" or self.source_type != "opencv_camera" or self.is_simulated:
            raise RuntimeError("真实验收只允许 REAL 模式下的非模拟 OpenCV 摄像头")
        if str(self.camera.get("source_type") or "").lower() not in {"webcam", "opencv"}:
            raise RuntimeError("当前采集适配器不是本机 OpenCV 摄像头")
        if parsed.camera_id != self.camera_id:
            raise ValueError("验收 camera_id 与当前视觉引擎不一致")
        item = self.item_by_id.get(parsed.item_id)
        if item is None:
            raise ValueError("验收 item_id 未注册到当前视觉引擎")
        try:
            registered_aruco = int(item.get("aruco_id"))
        except (TypeError, ValueError) as exc:
            raise ValueError("目标物品没有有效 ArUco ID") from exc
        if registered_aruco != parsed.aruco_id or self.item_by_marker.get(parsed.aruco_id) is not item:
            raise ValueError("验收 aruco_id 与目标物品注册信息不一致")
        configured_zones = {zone.id: zone for zone in self.zone_manager.zones if zone.enabled}
        for supplied, label in ((parsed.origin_zone, "origin_zone"), (parsed.destination_zone, "destination_zone")):
            configured = configured_zones.get(supplied.id)
            if (
                configured is None
                or configured.name != supplied.name
                or len(configured.points) != len(supplied.points)
                or any(math.dist(left, right) > 1e-9 for left, right in zip(configured.points, supplied.points))
            ):
                raise ValueError(f"验收 {label} 与当前摄像头已启用区域配置不一致")
        capture_health = self.capture.health()
        if not (self._thread and self._thread.is_alive() and capture_health.get("capture_thread_alive")):
            raise RuntimeError("当前 OpenCV 摄像头视觉链路未运行")
        if capture_health.get("status") not in {"online", "ready", "streaming"}:
            raise RuntimeError("当前 OpenCV 摄像头尚未持续输出真实帧")
        session_id = self._active_source_session or str(capture_health.get("source_session_id") or "")
        reconnect_epoch = self._active_reconnect_epoch if self._active_source_session else int(capture_health.get("reconnect_epoch") or 0)
        if parsed.source_session_id != session_id or parsed.reconnect_epoch != reconnect_epoch:
            raise ValueError("验收 source_session_id/reconnect_epoch 与当前真实取流代际不一致")

        with self._acceptance_lock:
            current = self._acceptance_runs.get(self._current_acceptance_run_id or "")
            if current is not None and (
                not current.terminal
                or (current.state == AcceptanceState.MOVEMENT_CONFIRMED and current.event_count == 0)
            ):
                raise RuntimeError("当前摄像头已有进行中的真实验收")
            if parsed.validation_run_id in self._acceptance_seen_run_ids:
                raise ValueError("validation_run_id 已使用，禁止重放或重复发射")
            if len(self._acceptance_run_order) == self._acceptance_run_order.maxlen:
                expired = self._acceptance_run_order.popleft()
                if expired != self._current_acceptance_run_id:
                    self._acceptance_runs.pop(expired, None)
            run = AcceptanceRun(parsed, armed_wall_time=time.time(), armed_monotonic=time.monotonic())
            self._acceptance_runs[parsed.validation_run_id] = run
            self._acceptance_seen_run_ids.add(parsed.validation_run_id)
            self._acceptance_run_order.append(parsed.validation_run_id)
            self._current_acceptance_run_id = parsed.validation_run_id
            return self._acceptance_status_locked(run)

    def cancel_acceptance(self, run_id: str, reason: str) -> dict[str, Any]:
        normalized = str(run_id or "").strip()
        if not normalized:
            raise ValueError("validation_run_id 不能为空")
        why = str(reason or "manual_cancelled").strip() or "manual_cancelled"
        with self._acceptance_lock:
            run = self._acceptance_runs.get(normalized)
            if run is None:
                raise KeyError(normalized)
            run.cancel(why, wall_time=time.time(), monotonic_time=time.monotonic())
            with self._pending_lock:
                self.pending = [
                    pending for pending in self.pending
                    if pending.event.get("validation_run_id") != normalized
                ]
            return self._acceptance_status_locked(run)

    def acceptance_status(self, run_id: str | None = None) -> dict[str, Any] | None:
        with self._acceptance_lock:
            normalized = str(run_id).strip() if run_id is not None else self._current_acceptance_run_id
            if not normalized:
                return None
            run = self._acceptance_runs.get(normalized)
            return self._acceptance_status_locked(run) if run is not None else None

    def _interrupt_acceptance(
        self,
        reason: str,
        packet: FramePacket | None = None,
        run_id: str | None = None,
    ) -> None:
        with self._acceptance_lock:
            run = self._acceptance_runs.get(run_id or self._current_acceptance_run_id or "")
            if run is None:
                return
            run.interrupt(
                reason,
                wall_time=packet.wall_time if packet is not None else time.time(),
                monotonic_time=packet.monotonic_time if packet is not None else time.monotonic(),
            )
            if run.terminal:
                self._notify_acceptance_status_locked(run)

    def _monitor_acceptance_source(self) -> None:
        with self._acceptance_lock:
            run = self._acceptance_runs.get(self._current_acceptance_run_id or "")
            if run is None:
                return
            if run.terminal and not (run.state == AcceptanceState.MOVEMENT_CONFIRMED and run.event_count == 0):
                self._notify_acceptance_status_locked(run)
                return
            capture_health = self.capture.health()
            status = str(capture_health.get("status") or "").lower()
            if status in {"error", "ended", "stopped", "recovering", "reconnecting", "frame_read_failed"}:
                self._interrupt_acceptance_run_locked(
                    run,
                    f"capture_status_{status}",
                    wall_time=time.time(),
                    monotonic_time=time.monotonic(),
                )
                return
            if time.monotonic() - run.updated_monotonic > run.config.thresholds.max_frame_gap_seconds:
                self._interrupt_acceptance_run_locked(
                    run,
                    "capture_frame_timeout",
                    wall_time=time.time(),
                    monotonic_time=time.monotonic(),
                )

    def health(self) -> dict[str, Any]:
        capture_health = self.capture.health()
        with self._pending_lock:
            pending_count = len(self.pending)
        with self._media_lock:
            media_jobs = len(self._media_futures)
        sampled_at = time.monotonic()
        with self._metrics_lock:
            processing_times = list(self._processing_times)
            inference_times = list(self._inference_times)
        average_processing = sum(processing_times) / len(processing_times) if processing_times else 0.0
        effective_fps = self._measured_rate(inference_times, sampled_at) if self._thread and self._thread.is_alive() else 0.0
        with self._latest_lock:
            preview_snapshot = dict(self._preview_snapshot) if self._preview_snapshot else {}
            preview_fps = self._measured_rate(list(self._preview_times), sampled_at) if self._preview_thread and self._preview_thread.is_alive() else 0.0
            preview_error = self._preview_error
        fresh_for = max(2.0, 3.0 / self.inference_fps)
        jpeg_fresh = bool(preview_snapshot) and sampled_at - preview_snapshot["created_at"] <= 2.0
        inference_fresh = bool(inference_times) and sampled_at - inference_times[-1] <= fresh_for
        if capture_health.get("status") == "error":
            status = "error"
        elif self._thread and self._thread.is_alive() and jpeg_fresh and inference_fresh:
            status = "ready"
        elif capture_health.get("status") == "ended":
            status = "ended"
        elif self._thread and self._thread.is_alive():
            status = "connecting" if not preview_snapshot else "stalled"
        else:
            status = capture_health.get("status", "stopped")
        machine_diagnostics = {item_id: machine.diagnostics() for item_id, machine in self.state_machines.items()}
        visible_machine_diagnostics = machine_diagnostics if self.diagnostic_mode else {
            item_id: {
                "state": value["state"],
                "candidate_active": value["candidate"] is not None,
                "last_rejection": value["last_rejection"],
            }
            for item_id, value in machine_diagnostics.items()
        }
        acceptance = self.acceptance_status()
        return {
            **capture_health,
            "status": status,
            "fps": round(effective_fps, 2),
            "capture_fps": round(float(capture_health.get("fps") or 0), 2) if capture_health.get("capture_thread_alive") else 0.0,
            "preview_fps": round(preview_fps, 2),
            "inference_fps": round(effective_fps, 2),
            "preview_target_fps": self.preview_target_fps,
            "inference_target_fps": self.inference_fps,
            "preview_sequence": preview_snapshot.get("sequence", 0),
            "preview_frame_sequence": preview_snapshot.get("source_frame_sequence"),
            "preview_source_session_id": preview_snapshot.get("source_session_id"),
            "preview_age_ms": round(max(0.0, sampled_at - preview_snapshot["created_at"]) * 1000, 1) if preview_snapshot else None,
            "preview_thread_alive": bool(self._preview_thread and self._preview_thread.is_alive()),
            "preview_error": preview_error,
            "preview_pipeline_diagnostics": self._preview_diagnostics.snapshot(),
            "preview_pending_frames": self._preview_pending_frames,
            "latency_ms": round(average_processing * 1000, 1),
            "pending_events": pending_count,
            "pending_media_jobs": media_jobs,
            "processed_frames": self._processed,
            "frame_sequence": self._last_frame_sequence,
            "source_session_id": self._active_source_session or capture_health.get("source_session_id"),
            "reconnect_epoch": self._active_reconnect_epoch,
            "runtime_mode": self.runtime_mode,
            "source_type": self.source_type,
            "is_simulated": self.is_simulated,
            "detection_mode": self._active_detection_mode(),
            "requested_detection_mode": self.settings.get("detection_mode", "aruco"),
            "fallback_mode": None,
            "source_label": "模拟设备 · 测试回放" if self.is_simulated else ("测试回放" if self.camera.get("source_type") == "video" else "本地实时画面"),
            "simulated": self.is_simulated,
            "test_playback": self.camera.get("source_type") == "video",
            "mode_label": (
                "照片身份识别（需现场验证）" if self._active_detection_mode() == "experimental"
                else "稳定标签模式"
            ),
            "detector": (self.experimental.health() if self.experimental else {'available': False, 'name': 'nanodet', 'error': '候选模型未加载'}) if self._active_detection_mode() == "experimental" else self.aruco.health(),
            "appearance": self._appearance_encoder.health() if self._appearance_encoder else {'available': False, 'error': self._recognition_error or '照片模型未启用'},
            "loaded_profile_versions": dict(self.loaded_profile_versions),
            "reference_patches": self.reference_patches.health() if self.reference_patches else {'available': False, 'enabled': False},
            "recognition_error": self._recognition_error,
            "phone_shape": {'enabled': self.phone_shape is not None, 'backend': 'phone_shape_proposal',
                            'category_evidence': 'geometry_only', 'confirmed_movement_allowed': False,
                            'error': self._phone_shape_error},
            "hands": self._safe_hand_health(),
            "person_pose": self._person_health_snapshot(capture_health,sampled_at),
            "hand_action_error": self._hand_action_error,
            "observation_media_error": self._snapshot_error,
            "stage_timings_ms": dict(self._stage_timings),
            "frame_geometry": self._frame_geometry.to_dict() if self._frame_geometry else None,
            "scene_requires_review": self.scene_requires_review,
            "scene_stability": dict(self.scene_guard.last_result),
            "diagnostic_mode": self.diagnostic_mode,
            "state_machines": visible_machine_diagnostics,
            "ring_buffer_frames": len(self.ring),
            "ring_buffer_bytes": self._ring_bytes,
            "acceptance": acceptance,
            "error": self._last_error or capture_health.get("error"),
        }

    def _run(self) -> None:
        interval = 1.0 / self.inference_fps
        while not self._stop_event.is_set():
            try:
                packet = self.capture.frames.get(timeout=0.5)
            except queue.Empty:
                self._monitor_acceptance_source()
                continue
            if packet.monotonic_time - self._last_process_at < interval * 0.8:
                continue
            self._last_process_at = packet.monotonic_time
            started = time.perf_counter()
            try:
                self._process(packet)
                with self._metrics_lock:
                    self._processed += 1
                    self._inference_times.append(time.monotonic())
            except Exception as exc:
                self._last_error = f"视觉处理失败：{exc}"
            with self._metrics_lock:
                self._processing_times.append(time.perf_counter() - started)

    def _switch_source_session(self, packet: FramePacket) -> None:
        session_id = packet.source_session_id or self._active_source_session or f"manual:{self.camera_id}"
        epoch = int(packet.reconnect_epoch)
        if session_id == self._active_source_session and epoch == self._active_reconnect_epoch:
            return
        if self._active_source_session is not None:
            self._interrupt_acceptance("source_session_changed", packet)
            # A confirmed episode may still be collecting post-roll. It is safe
            # to persist only the frames already observed in its old session.
            self._finalize_all(stopped_early=True)
            if any(zone.support_surface_confirmed for zone in self.zone_manager.zones):
                self.scene_requires_review = True
                self.zone_manager.replace([])
        self._active_source_session = session_id
        self._active_reconnect_epoch = epoch
        self._pipeline_counts = {'processed_frames': 0, 'object_model_frames': 0, 'hand_model_frames': 0}
        self.observation_gate.reset()
        self._reset_hand_actions()
        self._reset_persons()
        self.tracker.reset()
        self.zone_manager.reset_tracks()
        for machine in self.state_machines.values():
            machine.reset_source_session(session_id, epoch, "source_session_changed")
        with self._ring_lock:
            self.ring.clear()
            self._ring_bytes = 0
        self._track_history.clear()
        self._visual_history.clear()
        self._last_track_emit.clear()
        self._last_track_emit_state.clear()

    def _evidence_frame(self, frame: np.ndarray) -> np.ndarray:
        height, width = frame.shape[:2]
        if width <= self._evidence_max_width:
            return frame.copy()
        scale = self._evidence_max_width / float(width)
        return cv2.resize(frame, (self._evidence_max_width, max(1, round(height * scale))), interpolation=cv2.INTER_AREA)

    def _append_ring(self, packet: FramePacket | float, frame: np.ndarray) -> np.ndarray:
        evidence = self._evidence_frame(frame)
        if isinstance(packet, FramePacket):
            entry = RingFrame(
                monotonic_time=packet.monotonic_time,
                wall_time=packet.wall_time,
                sequence=packet.sequence,
                source_session_id=packet.source_session_id or self._active_source_session or "",
                reconnect_epoch=packet.reconnect_epoch,
                frame=evidence,
            )
        else:
            # Compatibility for older local extensions that supplied only a timestamp.
            entry = RingFrame(
                monotonic_time=float(packet),
                wall_time=float(packet),
                sequence=self._last_frame_sequence,
                source_session_id=self._active_source_session or "",
                reconnect_epoch=self._active_reconnect_epoch,
                frame=evidence,
            )
        protected_cutoff: float | None = None
        with self._acceptance_lock:
            run = self._acceptance_runs.get(self._current_acceptance_run_id or "")
            if (
                run is not None
                and (run.movement_started is not None or run.exit_candidate is not None)
                and run.event_count == 0
                and run.state not in {AcceptanceState.ABORTED, AcceptanceState.CAMERA_INTERRUPTED}
            ):
                protected_from = run.movement_started or run.exit_candidate
                protected_cutoff = float(protected_from["monotonic_time"]) - ACCEPTANCE_PRE_SECONDS
        # Ordinary movements now settle for five seconds. Protect their actual
        # stable origin while carrying/settling, not just a sliding pre-roll
        # window that would silently turn the whole clip into a static tail.
        for machine in tuple(self.state_machines.values()):
            episode = machine.episode
            if (episode is not None and episode.source_session_id == entry.source_session_id
                    and episode.reconnect_epoch == entry.reconnect_epoch):
                origin = episode.from_baseline.monotonic_timestamp
                protected_cutoff = origin if protected_cutoff is None else min(protected_cutoff, origin)
        with self._ring_lock:
            self.ring.append(entry)
            self._ring_bytes += evidence.nbytes
            while self.ring and (len(self.ring) > self._ring_capacity or self._ring_bytes > self._ring_max_bytes):
                next_entry = self.ring[1] if len(self.ring) > 1 else None
                if (
                    protected_cutoff is not None
                    and self.ring[0].source_session_id == entry.source_session_id
                    and (
                        self.ring[0].monotonic_time >= protected_cutoff
                        or (
                            self.ring[0].monotonic_time <= protected_cutoff
                            and (
                                next_entry is None
                                or next_entry.source_session_id != entry.source_session_id
                                or next_entry.monotonic_time > protected_cutoff
                            )
                        )
                    )
                    and self._ring_bytes <= self._ring_max_bytes
                ):
                    break
                removed = self.ring.popleft()
                self._ring_bytes -= removed.frame.nbytes
        return evidence

    def _process(self, packet: FramePacket) -> None:
        inference_started_at = time.monotonic()
        self._apply_profile_updates()
        self._apply_scene_update()
        revision = self._applied_profile_revision
        self._switch_source_session(packet)
        self._pipeline_counts['processed_frames'] += 1
        self._raw_model_detections = []
        self._recognition_stage_errors = {}
        self._stage_timings = {}
        self._last_frame_sequence = packet.sequence
        frame = packet.frame
        height, width = frame.shape[:2]
        if self._frame_geometry is None or (width, height) != (self._frame_geometry.source_width, self._frame_geometry.source_height):
            if self._frame_geometry is not None:
                self.scene_requires_review = True
                self._finalize_all(stopped_early=True)
                self.observation_gate.reset()
                self._reset_hand_actions()
                self._reset_persons()
                self.tracker.reset()
                self.zone_manager.replace([])
                for machine in self.state_machines.values():
                    machine.reset_source_session(packet.source_session_id, packet.reconnect_epoch, 'frame_geometry_changed')
                with self._ring_lock:
                    self.ring.clear(); self._ring_bytes = 0
            self._frame_geometry = FrameGeometry(width, height)
        if not self.scene_requires_review and self.scene_guard.check(frame, packet.monotonic_time):
            self.scene_requires_review = True
            self._finalize_all(stopped_early=True)
            self.observation_gate.reset()
            self._reset_hand_actions()
            self._reset_persons()
            self.tracker.reset()
            self.zone_manager.replace([])
            for machine in self.state_machines.values():
                machine.reset_source_session(packet.source_session_id, packet.reconnect_epoch, 'camera_view_changed')
        stage_started = time.perf_counter()
        active_mode = self._active_detection_mode()
        person_detections = []
        if active_mode == "experimental":
            try:
                if self.experimental is None or not self.experimental.health().get('available'):
                    raise RuntimeError((self.experimental.health() if self.experimental else {}).get('error') or '候选物体模型未加载')
                detections = self.experimental.detect(frame)
                self._pipeline_counts['object_model_frames'] += 1
                self._raw_model_detections = [{'category': det.label, 'raw_class_id': det.raw_id,
                    'bbox': [det.bbox[0]/width, det.bbox[1]/height, det.bbox[2]/width, det.bbox[3]/height],
                    'coordinate_space': 'source_normalized_xywh', 'detector_score': float(det.confidence)}
                    for det in detections]
                person_detections = [detection for detection in detections if detection.label == 'person']
                self._stage_timings['object_detection'] = round((time.perf_counter()-stage_started)*1000, 2)
                reference_started = time.perf_counter()
                try:
                    detections = self._supplement_reference_patches(frame, detections)
                    if self.reference_patches is not None:
                        self._reference_patch_error = self.reference_patches.health().get('error')
                except Exception as exc:
                    self._issued_reference_patch_detections.clear()
                    self._reference_patch_error = f'参考局部定位处理失败：{exc}'
                if self._reference_patch_error:
                    self._recognition_stage_errors['reference_patch_localization'] = self._reference_patch_error
                self._stage_timings['reference_patch_localization'] = round((time.perf_counter()-reference_started)*1000, 2)
                shape_started = time.perf_counter()
                try:
                    if self.phone_shape is not None:
                        detections = self.phone_shape.supplement(frame, detections)
                    self._phone_shape_error = None
                except Exception as exc:
                    # A failed auxiliary proposal must not erase a valid model
                    # candidate or trigger a guessed identity.
                    self._phone_shape_error = f'外形候选暂不可用：{exc}'
                    self._recognition_stage_errors['phone_shape_proposal'] = self._phone_shape_error
                self._stage_timings['phone_shape_proposal'] = round((time.perf_counter()-shape_started)*1000, 2)
                match_started = time.perf_counter()
                self._assign_reference_identities(frame, detections)
                if self._recognition_error:
                    self._recognition_stage_errors['identity_matching'] = self._recognition_error
                self._stage_timings['identity_matching'] = round((time.perf_counter()-match_started)*1000, 2)
            except Exception as exc:
                detections = []
                self._recognition_candidates = []
                self._recognition_error = f'照片识别处理失败：{exc}'
                self._recognition_stage_errors['object_recognition'] = self._recognition_error
        else:
            detections = self.aruco.detect(frame)
            self._stage_timings['object_detection'] = round((time.perf_counter()-stage_started)*1000, 2)
            self._stage_timings['identity_matching'] = 0
        # Independent actual person boxes; identity assignment/filtering must
        # not convert hands or class-index identities into human tracks.
        self._process_persons(frame,person_detections,packet)
        # Person class indices are not stable identity and do not belong in
        # the registered-object state machine or its historical event stream.
        if active_mode == 'experimental':
            person_objects = {id(detection) for detection in person_detections}
            detections = [detection for detection in detections if id(detection) not in person_objects]
        hand_started = time.perf_counter()
        try:
            hands = self.hands.detect(frame, timestamp_ms=round(packet.monotonic_time*1000)) if isinstance(self.hands, MediaPipeHandDetector) else self.hands.detect(frame)
            if self._safe_hand_health().get('available'):
                self._pipeline_counts['hand_model_frames'] += 1
        except Exception as exc:
            # Hand assistance failing must not erase valid object observations.
            self.hands.error = f'手部处理失败：{exc}'
            self._recognition_stage_errors['hands'] = self.hands.error
            hands = []
        self._stage_timings['hands'] = round((time.perf_counter()-hand_started)*1000, 2)
        actions_started = time.perf_counter()
        hand_health = self._safe_hand_health()
        hand_healthy = bool(hand_health.get('available') and not hand_health.get('error'))
        self._hand_action_error = None
        try:
            actions = self.hand_actions.update(hands if hand_healthy else [], frame.shape,
                                              packet.source_session_id, packet.sequence, packet.monotonic_time)
        except Exception as exc:
            actions = []
            hand_healthy = False
            self._hand_action_error = f'手势分析失败：{exc}'
            hand_health = {**hand_health, 'available': False, 'status': 'failed', 'error': self._hand_action_error}
            self.hand_actions.reset('analysis_failed')
        self._stage_timings['hand_actions'] = round((time.perf_counter()-actions_started)*1000, 2)
        if revision != self._profile_revision:
            return  # A profile changed during inference; do not publish old identity.
        tracker_started = time.perf_counter()
        tracked, missing = self.tracker.update(detections, frame.shape, packet.monotonic_time)
        self._stage_timings['tracking'] = round((time.perf_counter()-tracker_started)*1000, 2)
        annotated = frame.copy()
        self._draw_zones(annotated)
        if self.settings.get('show_hands', True):
            self._draw_hands(annotated, hands)
        try:
            raw_ok, raw_jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            self._snapshot_error = None if raw_ok else '当前观察截图编码失败'
        except Exception as exc:
            raw_ok, raw_jpeg = False, None
            self._snapshot_error = f'当前观察截图编码失败：{exc}'
        if not raw_ok:
            with self._profile_lock:
                self._recognition_snapshot = None
        if raw_ok:
            with self._profile_lock:
                if revision != self._profile_revision:
                    return
                source_timestamp = datetime.fromtimestamp(packet.wall_time, timezone.utc).isoformat()
                frame_persons = [person for person in self.person_snapshot()
                    if person.get('source_frame') == packet.sequence
                    and person.get('source_session_id') == packet.source_session_id
                    and person.get('source_timestamp') == source_timestamp]
                # Allowlist display-only fields: an LK-transported body box
                # must never carry an old foot point or world-position proof.
                person_candidates = [{'category':'person','entity_type':'person',
                    'person_track_id':person['person_track_id'],'bbox':list(person['bbox']),
                    'detector_score':person.get('confidence'),'proposal_backend':person.get('detector_backend'),
                    'accepted':False,'item_id':None,'best_item_id':None,'best_score':None,
                    'identity_kind':'anonymous_short_lived_iou_track','category_evidence':'model',
                    'source_frame':packet.sequence,'source_session_id':packet.source_session_id,
                    'visual_only':True,'observation_evidence':False,'tracking_status':'detected'}
                    for person in frame_persons]
                self._recognition_snapshot = {
                    'jpeg': raw_jpeg.tobytes(), 'created_at': time.monotonic(),
                    'source_session_id': packet.source_session_id, 'source_frame': packet.sequence,
                    'reconnect_epoch': packet.reconnect_epoch,
                    'source_timestamp': datetime.fromtimestamp(packet.wall_time, timezone.utc).isoformat(), 'width': frame.shape[1], 'height': frame.shape[0],
                    'runtime_mode': self.runtime_mode, 'source_type': self.source_type, 'is_simulated': self.is_simulated,
                    'model': {**(self._appearance_encoder.health() if self._appearance_encoder else {}),
                        'candidate_backend': 'nanodet' if active_mode == 'experimental' else 'aruco',
                        'registered_reference_backend': self.reference_patches.backend if self.reference_patches else None,
                        'auxiliary_candidate_backend': 'phone_shape_proposal' if active_mode == 'experimental' and self.phone_shape else None,
                        'status': 'failed' if self._recognition_error else 'ready', 'error': self._recognition_error},
                    'loaded_profiles': [{'item_id': key, 'name': self.item_by_id[key].get('name'), 'profile_version': version} for key, version in self.loaded_profile_versions.items() if key in self.item_by_id],
                    'candidates': (deepcopy(self._recognition_candidates) + person_candidates) if active_mode == 'experimental' else [],
                    'persons': frame_persons,
                    'geometry': self._frame_geometry.to_dict(), 'coordinate_space': 'source_normalized',
                    'hand_count': len(hands), 'hands': [{
                        'landmarks': list(hand.landmarks), 'bbox_pixels': list(hand.bbox),
                        'bbox_normalized': [hand.bbox[0]/width, hand.bbox[1]/height, hand.bbox[2]/width, hand.bbox[3]/height],
                        'handedness': getattr(hand,'handedness',None), 'handedness_score': getattr(hand,'handedness_score',None),
                        'presence_score': None, 'source_timestamp_ms': getattr(hand,'source_timestamp_ms',None),
                        'timestamp_ms': getattr(hand,'timestamp_ms',None),
                    } for hand in hands],
                    'hand_status': deepcopy(hand_health),
                    'pipeline_diagnostics': self._recognition_diagnostics(packet),
                }
                self.preview_tracker.offer(frame, self._recognition_snapshot['candidates'],
                    packet.source_session_id, packet.sequence, packet.monotonic_time, profile_generation=revision)
        for observation in tracked:
            history = self._visual_history.setdefault(observation.identity, deque(maxlen=24))
            if observation.recovered:
                history.clear()  # never draw an inferred path through occlusion
            history.append((round(observation.center[0]), round(observation.center[1])))
            if len(history) > 1:
                cv2.polylines(annotated, [np.asarray(history, dtype=np.int32)], False, (70, 150, 230), 2, cv2.LINE_AA)
            self._draw_track(annotated, observation)
        mode_text = "SIMULATED DEVICE - TEST PLAYBACK" if self.is_simulated else ("TEST PLAYBACK" if self.camera.get("source_type") == "video" else "LOCAL LIVE")
        if self.camera.get("source_type") == "screen":
            mode_text = "AUTHORIZED SCREEN REGION"
            cv2.rectangle(annotated, (2, 2), (annotated.shape[1] - 3, annotated.shape[0] - 3), (40, 145, 240), 4)
        detector_text = "EXPERIMENTAL AI MODE" if active_mode == "experimental" else ("TAG FALLBACK MODE" if active_mode == "aruco_fallback" else "STABLE TAG MODE")
        cv2.rectangle(annotated, (0, 0), (min(annotated.shape[1], 480), 34), (250, 250, 250), -1)
        cv2.putText(annotated, f"{mode_text} | {detector_text}", (9, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.54, (30, 80, 110), 1, cv2.LINE_AA)
        evidence_frame = self._append_ring(packet, annotated)
        acceptance_item_id = self._acceptance_item_for_frame()
        self._update_acceptance(packet, detections, frame.shape, evidence_frame)
        self._append_pending(packet, evidence_frame)

        observed_ids: set[str] = set()
        for track in tracked:
            if track.identity not in self.state_machines:
                continue
            observed_ids.add(track.identity)
            zone, _transition = self.zone_manager.update_track(track.track_id, track.center_norm, packet.monotonic_time)
            hand_near = self._hand_near(track, hands, frame.shape)
            x, y, box_width, box_height = track.bbox
            interaction = self._update_hand_interaction(track.identity,
                (x/width, y/height, box_width/width, box_height/height), actions, packet, healthy=hand_healthy)
            self._item_interactions[track.identity] = interaction
            motion = MotionObservation(
                center_norm=track.center_norm,
                speed_norm_s=track.speed_norm_s,
                velocity_norm_s=track.velocity_norm_s,
                zone_id=zone.id if zone else None,
                zone_name=zone.name if zone else "未定义区域",
                confidence=track.confidence,
                hand_near=hand_near,
                detection_mode=track.detection_mode,
                source_frame=packet.sequence,
                source_timestamp=packet.wall_time,
                source_session_id=self._active_source_session,
                reconnect_epoch=packet.reconnect_epoch,
                hand_model_healthy=hand_healthy,
                hand_interaction=deepcopy(interaction),
                support_surface_confirmed=bool(zone and zone.support_surface_confirmed and not self.scene_requires_review
                    and self.scene_guard.last_result.get('status')=='stable'),
            )
            events = self.state_machines[track.identity].update(
                motion,
                packet.monotonic_time,
                source_session_id=self._active_source_session,
                reconnect_epoch=packet.reconnect_epoch,
                frame_sequence=packet.sequence,
                source_timestamp=packet.wall_time,
            )
            for state_event in events:
                if track.identity != acceptance_item_id:
                    self._queue_event(track, state_event, packet, evidence_frame)
            if active_mode == 'experimental':
                self.observation_gate.observe(track.identity, motion, packet.monotonic_time,
                                              track.metadata.get('identity_evidence') or {})
            self._emit_track(track, zone, hand_near, packet)
        for identity, machine in self.state_machines.items():
            if identity not in observed_ids and identity in missing:
                self._item_interactions[identity] = self._update_hand_interaction(identity, None, actions,
                    packet, detected=False, healthy=hand_healthy)
                self.observation_gate.missing(identity)
                track = self.tracker.track(identity)
                state_events = machine.update(
                    None,
                    packet.monotonic_time,
                    source_session_id=self._active_source_session,
                    reconnect_epoch=packet.reconnect_epoch,
                    frame_sequence=packet.sequence,
                    source_timestamp=packet.wall_time,
                )
                if track:
                    synthetic_track = TrackedObservation(
                        track.track_id, identity, track.last_detection.label, track.last_detection.bbox,
                        track.last_detection.center, track.center_norm, (0.0, 0.0), 0.0,
                        track.last_detection.confidence, track.last_detection.detection_mode,
                        track.last_detection.raw_id, track.age_frames, False, track.last_detection.metadata,
                    )
                    for state_event in state_events:
                        if identity != acceptance_item_id:
                            self._queue_event(synthetic_track, state_event, packet, annotated)
                    self._emit_missing_track(synthetic_track, machine, packet)

        self.hand_interactions.expire(packet.monotonic_time)
        # Publish actions once the entire frame has completed. Never marry a
        # fresh hand to an older object frame or a replaced profile generation.
        interactions = [{**deepcopy(value), 'item_id': key, 'item_name': self.item_by_id[key].get('name'),
                         'state': value.get('holding_status', 'not_established')}
                        for key, value in self._item_interactions.items()
                        if key in self.item_by_id and value.get('source_frame') == packet.sequence]
        with self._profile_lock:
            if revision == self._profile_revision:
                self._action_snapshot = {'camera_id': self.camera_id, 'created_at': packet.monotonic_time,
                    'source_session_id': packet.source_session_id, 'source_frame': packet.sequence,
                    'reconnect_epoch': packet.reconnect_epoch, 'profile_generation': revision,
                    'inference_started_at': inference_started_at, 'published_at': time.monotonic(),
                    'timestamp': datetime.fromtimestamp(packet.wall_time, timezone.utc).isoformat(),
                    'runtime_mode': self.runtime_mode, 'source_type': self.source_type, 'is_simulated': self.is_simulated,
                    'hand_status': hand_health, 'hands': deepcopy(actions), 'interactions': interactions,
                    'profile_count': len(self.loaded_profile_versions)}
                if self._recognition_snapshot and self._recognition_snapshot.get('source_frame') == packet.sequence:
                    self._recognition_snapshot['hand_actions'] = deepcopy(actions)
                    self._recognition_snapshot['interactions'] = deepcopy(interactions)

    def _acceptance_item_for_frame(self) -> str | None:
        with self._acceptance_lock:
            run = self._acceptance_runs.get(self._current_acceptance_run_id or "")
            if run is None or run.terminal:
                return None
            return run.config.item_id

    def _update_acceptance(
        self,
        packet: FramePacket,
        detections: list,
        frame_shape: tuple[int, ...],
        evidence_frame: np.ndarray,
    ) -> None:
        with self._acceptance_lock:
            run = self._acceptance_runs.get(self._current_acceptance_run_id or "")
            if run is None:
                return
            if run.state == AcceptanceState.MOVEMENT_CONFIRMED and run.event_count == 0:
                run.observe_postroll(packet)
                if run.terminal and run.state != AcceptanceState.MOVEMENT_CONFIRMED:
                    self._notify_acceptance_status_locked(run)
                return
            if run.terminal:
                self._notify_acceptance_status_locked(run)
                return
            matching = [
                detection for detection in detections
                if detection.detection_mode == "aruco" and detection.raw_id == run.config.aruco_id
            ]
            target = max(matching, key=lambda detection: detection.confidence, default=None)
            confirmed = run.observe(
                packet,
                target,
                frame_shape,
                ring_seconds=self._ring_seconds(run.config.source_session_id),
            )
            if run.terminal and not (
                run.state == AcceptanceState.MOVEMENT_CONFIRMED and run.event_count == 0
            ):
                self._notify_acceptance_status_locked(run)
            if confirmed:
                self._enqueue_acceptance_event_locked(run, packet, evidence_frame)
                machine = self.state_machines.get(run.config.item_id)
                if machine is not None:
                    machine.reset_source_session(
                        run.config.source_session_id,
                        run.config.reconnect_epoch,
                        "real_acceptance_confirmed",
                    )

    def _enqueue_acceptance_event_locked(
        self,
        run: AcceptanceRun,
        packet: FramePacket,
        after_frame: np.ndarray,
    ) -> None:
        """Queue an acceptance event while ``_acceptance_lock`` is held."""
        if (
            run.event_count
            or run.movement_started is None
            or run.confirmed_after is None
            or run.baseline is None
            or run.before_evidence is None
        ):
            return
        movement_monotonic = float(run.movement_started["monotonic_time"])
        clip_start = movement_monotonic - ACCEPTANCE_PRE_SECONDS
        with self._ring_lock:
            session_entries = [
                entry for entry in self.ring
                if entry.source_session_id == run.config.source_session_id
                and entry.monotonic_time <= packet.monotonic_time
            ]
            # Start at the last observed frame at or before the exact five-second
            # boundary.  Starting with the first frame *after* the boundary
            # creates a small but real evidence gap at normal camera cadences and
            # makes the backend correctly reject the clip as shorter than 5 s.
            start_index = next(
                (index for index in range(len(session_entries) - 1, -1, -1)
                 if session_entries[index].monotonic_time <= clip_start),
                None,
            )
            selected = session_entries[start_index:] if start_index is not None else []
        if not selected or selected[0].monotonic_time > clip_start:
            self._interrupt_acceptance_run_locked(
                run,
                "five_second_pre_roll_not_available_at_confirmation",
                wall_time=packet.wall_time,
                monotonic_time=packet.monotonic_time,
            )
            return
        before_index = next(
            (index for index, entry in enumerate(selected) if entry.sequence == run.before_frame_sequence),
            None,
        )
        if before_index is None:
            self._interrupt_acceptance_run_locked(
                run,
                "stable_origin_before_frame_missing_from_ring",
                wall_time=packet.wall_time,
                monotonic_time=packet.monotonic_time,
            )
            return
        after_index = next(
            (index for index, entry in reversed(list(enumerate(selected))) if entry.sequence == packet.sequence),
            None,
        )
        if after_index is None:
            self._interrupt_acceptance_run_locked(
                run,
                "confirmed_after_frame_missing_from_ring",
                wall_time=packet.wall_time,
                monotonic_time=packet.monotonic_time,
            )
            return
        clip_frames = [entry.frame.copy() for entry in selected]
        if clip_frames[before_index] is after_frame or np.shares_memory(clip_frames[before_index], after_frame):
            self._interrupt_acceptance_run_locked(
                run,
                "before_after_frame_alias_detected",
                wall_time=packet.wall_time,
                monotonic_time=packet.monotonic_time,
            )
            return

        item = self.item_by_id[run.config.item_id]
        trajectory_observations = [
            dict(point) for point in run.trajectory
            if int(point.get("frame_sequence") or -1) >= int(run.before_frame_sequence or -1)
        ]
        if (
            not trajectory_observations
            or trajectory_observations[0].get("frame_sequence") != run.before_frame_sequence
        ):
            trajectory_observations.insert(0, dict(run.before_evidence))
        trajectory = [dict(point) for point in trajectory_observations if not point.get("lost")]
        confidence_values = [float(point["confidence"]) for point in trajectory if not point.get("lost") and point.get("confidence") is not None]
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"objectmemory:real-acceptance:{run.config.validation_run_id}"))
        timestamp_start = datetime.fromtimestamp(run.before_evidence["wall_time"], timezone.utc).isoformat()
        timestamp_end = datetime.fromtimestamp(packet.wall_time, timezone.utc).isoformat()
        origin_zone = run.config.origin_zone.as_dict()
        destination_zone = run.config.destination_zone.as_dict()
        event = {
            "id": event_id,
            "event_id": event_id,
            "movement_session_id": event_id,
            "validation_run_id": run.config.validation_run_id,
            "trial_kind": run.config.trial_kind,
            "scenario_index": run.config.scenario_index,
            "item_id": run.config.item_id,
            "item_name": item.get("name") or f"ArUco {run.config.aruco_id:03d}",
            "aruco_id": run.config.aruco_id,
            "event_type": "movement",
            "camera_id": self.camera_id,
            "camera_name": self.camera_name,
            "room_name": self.room_name,
            "runtime_mode": "REAL",
            "source_type": "opencv_camera",
            "is_simulated": False,
            "source_session_id": run.config.source_session_id,
            "reconnect_epoch": run.config.reconnect_epoch,
            "source_frame_start": run.before_evidence["frame_sequence"],
            "source_frame_end": packet.sequence,
            "source_timestamp_start": timestamp_start,
            "source_timestamp_end": timestamp_end,
            "timestamp_start": timestamp_start,
            "timestamp_end": timestamp_end,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "detector_backend": "aruco",
            "tracker_backend": "aruco_acceptance_state_machine",
            "detection_mode": "aruco_screen_validation",
            "from_zone": origin_zone["name"],
            "from_zone_id": origin_zone["id"],
            "previous_zone": origin_zone["name"],
            "to_zone": destination_zone["name"],
            "to_zone_id": destination_zone["id"],
            "new_zone": destination_zone["name"],
            "zone_id": destination_zone["id"],
            "zone_name": destination_zone["name"],
            "origin_zone": origin_zone,
            "destination_zone": destination_zone,
            # Calibration remains an independent diagnostic snapshot. The
            # episode endpoint must describe its actual designated before
            # frame, which may follow a permitted adjustment within the origin.
            "from_position": list(run.before_evidence["center_norm"]),
            "to_position": list(run.confirmed_after["center_norm"]),
            "final_position": list(run.confirmed_after["center_norm"]),
            "confidence": min(confidence_values, default=0.0),
            "evidence_type": "aruco_screen_validation",
            "evidence_status": "confirmed",
            "final_status": "confirmed_placed",
            "source_continuity_ok": True,
            "reconnect_epoch_changed": False,
            "stable_before": True,
            "stable_after": True,
            "meaningful_position_change": True,
            "trajectory": trajectory,
            "trajectory_observations": trajectory_observations,
            "baseline": dict(run.baseline),
            "thresholds": run.status()["thresholds"],
            "calibration_snapshot": dict(run.calibration_snapshot or {}),
            "pickup_evidence": dict(run.movement_started),
            "placement_evidence": dict(run.confirmed_after),
            "before_frame": dict(run.before_evidence),
            "after_frame": dict(run.confirmed_after),
            "before_frame_index": before_index,
            "after_frame_index": after_index,
            "clip_source_frame_start": selected[0].sequence,
            "clip_source_frame_end": selected[-1].sequence,
            "clip_source_timestamp_start": datetime.fromtimestamp(selected[0].wall_time, timezone.utc).isoformat(),
            "clip_source_timestamp_end": datetime.fromtimestamp(selected[-1].wall_time, timezone.utc).isoformat(),
            "clip_fps": (
                (len(selected) - 1) / (selected[-1].monotonic_time - selected[0].monotonic_time)
                if len(selected) > 1 and selected[-1].monotonic_time > selected[0].monotonic_time
                else self.inference_fps
            ),
            "clip_required_pre_seconds": ACCEPTANCE_PRE_SECONDS,
            "clip_required_post_seconds": ACCEPTANCE_POST_SECONDS,
            "screenshot_path": None,
            "screenshot_sha256": None,
            "clip_path": None,
            "clip_sha256": None,
            "human_review_status": "unreviewed",
            "created_by": "vision_pipeline",
            "pinned": False,
            "manually_corrected": False,
            "notes": run.reason,
        }
        pending = _PendingEvent(
            event=event,
            evidence_frame=after_frame.copy(),
            frames=clip_frames,
            started_monotonic=packet.monotonic_time,
            finalize_at=packet.monotonic_time + ACCEPTANCE_POST_SECONDS,
            target_seconds=ACCEPTANCE_PRE_SECONDS + ACCEPTANCE_POST_SECONDS,
            clip_started_monotonic=selected[0].monotonic_time,
            clip_last_monotonic=selected[-1].monotonic_time,
        )
        with self._pending_lock:
            if len(self.pending) >= self._max_pending_events:
                self._interrupt_acceptance_run_locked(
                    run,
                    "acceptance_media_queue_full",
                    wall_time=packet.wall_time,
                    monotonic_time=packet.monotonic_time,
                )
                self._last_error = "后置片段队列已满，真实验收事件未持久化"
                return
            self.pending.append(pending)

    def _draw_zones(self, frame: np.ndarray) -> None:
        for index, (zone, points) in enumerate(self.zone_manager.pixel_polygons(frame.shape[1], frame.shape[0])):
            polygon = np.asarray(points, dtype=np.int32)
            color = ((67 + index * 71) % 220, (156 + index * 47) % 220, (92 + index * 83) % 220)
            cv2.polylines(frame, [polygon], True, color, 2, cv2.LINE_AA)
            x, y = polygon[0]
            cv2.putText(frame, f"ZONE {index + 1}", (int(x) + 3, max(48, int(y) + 18)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)

    @staticmethod
    def _draw_hands(frame: np.ndarray, hands: list) -> None:
        for hand in hands:
            x, y, width, height = hand.bbox
            cv2.rectangle(frame, (x, y), (x + width, y + height), (64, 180, 255), 1)
            for nx, ny in hand.landmarks:
                cv2.circle(frame, (round(nx * frame.shape[1]), round(ny * frame.shape[0])), 2, (64, 180, 255), -1)

    @staticmethod
    def _draw_track(frame: np.ndarray, track: TrackedObservation) -> None:
        x, y, width, height = track.bbox
        color = (46, 170, 80) if track.detection_mode == "aruco" else (230, 145, 40)
        cv2.rectangle(frame, (x, y), (x + width, y + height), color, 2)
        marker = str(track.raw_id) if track.raw_id is not None else track.label
        label = 'PHONE-SHAPED? (unconfirmed)' if track.metadata.get('category_evidence') == 'geometry_only' else f"ID {marker} {track.confidence:.2f}"
        cv2.putText(frame, label, (x, max(45, y - 7)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2, cv2.LINE_AA)
        cv2.circle(frame, (round(track.center[0]), round(track.center[1])), 4, color, -1)

    def _supplement_reference_patches(self, frame: np.ndarray, detections: list) -> list:
        """A separate exemplar route; never relabel a COCO remote as a phone."""
        self._issued_reference_patch_detections.clear()
        if self.reference_patches is None or not self.reference_patches.health().get('available'):
            return detections
        from .detectors.appearance import normalize_category
        from .detectors.reference_patch import _overlap
        result = list(detections)
        proposals = self.reference_patches.detect(frame)

        def can_share_semantic(det, proposal):
            if (det.metadata.get('backend') == self.reference_patches.backend
                    or det.metadata.get('category_evidence') == 'geometry_only'
                    or normalize_category(det.label) != normalize_category(proposal.get('category'))):
                return False
            iou, containment = _overlap(det.bbox, proposal['bbox'])
            a, b = det.bbox[2]*det.bbox[3], proposal['bbox'][2]*proposal['bbox'][3]
            return (iou >= float(self.settings.get('reference_patch_fusion_iou', .5))
                    or (containment >= float(self.settings.get('reference_patch_fusion_containment', .9))
                        and min(a, b)/max(a, b, 1) >= float(self.settings.get('reference_patch_fusion_min_area_ratio', .2))))

        for proposal in proposals:
            item_id = proposal.get('best_item_id')
            x, y, width, height = proposal['bbox']
            x1, y1 = max(0, math.floor(x)), max(0, math.floor(y))
            x2, y2 = min(frame.shape[1], math.ceil(x+width)), min(frame.shape[0], math.ceil(y+height))
            if min(x2-x1, y2-y1) < int(self.settings.get('identity_min_crop_pixels', 32)):
                continue
            eligible = (proposal.get('eligible') is True and item_id in self.item_by_id
                        and self.loaded_profile_versions.get(item_id) == proposal.get('profile_version'))
            # A prompt's group label alone is never identity. The local route
            # requires a connected set of distinct foreground descriptors,
            # rejection by reference background and inter-item/spatial checks.
            proof = {**proposal, 'accepted': eligible, 'item_id': item_id if eligible else None,
                'proposal_only': not eligible, 'identity_method': 'registered_foreground_background_patch_consensus',
                'score_kind': 'localized_patch_cosine_not_probability',
                'threshold': self.reference_patches.config.top_patch_threshold,
                'margin': self.reference_patches.config.identity_margin,
                'rejection': proposal.get('rejection') if not eligible else None}
            if eligible:
                # Fuse duplicate boxes from two algorithms on this SAME image;
                # never merge two spatially separate candidates or two items.
                overlapping = [det for det in detections if can_share_semantic(det, proposal)]
                if len(overlapping) == 1:
                    semantic = overlapping[0]
                    # Local descriptors often cover just the screen/camera
                    # housing. A unique encompassing semantic object is not a
                    # second physical item. Use its measured full-object box.
                    if sum(other.get('eligible') is True and can_share_semantic(semantic, other)
                           for other in proposals) == 1:
                        x1, y1, sw, sh = semantic.bbox
                        x2, y2 = x1+sw, y1+sh
                        proof.update(localization_method='semantic_box_with_reference_patch_region',
                                     semantic_bbox=list(semantic.bbox), semantic_backend=semantic.metadata.get('backend'))
                        result = [det for det in result if det is not semantic]
                    else:
                        proof['fusion_rejection'] = 'semantic_contains_multiple_reference_regions'
                elif len(overlapping) > 1:
                    proof.update(accepted=False, item_id=None, proposal_only=True,
                                 rejection='multiple_semantic_containers')
            detection = Detection(identity=f'candidate:reference:{item_id}:{len(result)}',
                label=str(proposal.get('category') or 'registered object'), bbox=(x1,y1,x2-x1,y2-y1),
                center=((x1+x2)/2,(y1+y2)/2), confidence=float(proposal['score']),
                detection_mode='experimental', metadata={'backend': self.reference_patches.backend,
                    'category_evidence': 'registered_reference_patch', 'reference_patch_proof': proof})
            result.append(detection)
            self._issued_reference_patch_detections[id(detection)] = (frame, detection.bbox, deepcopy(proof))
        return result

    def _assign_reference_identities(self, frame: np.ndarray, detections: list) -> None:
        # Generic boxes define candidates; the local reference matcher assigns
        # a registered identity only above its explicit threshold.
        self._recognition_candidates = []
        issued_patches = self._issued_reference_patch_detections
        self._issued_reference_patch_detections = {}  # one call, including exception paths
        appearance_health = self._appearance_encoder.health() if self._appearance_encoder else {}
        self._recognition_error = None if appearance_health.get('available') else appearance_health.get('error') or '外观模型尚未加载'
        height_px, width_px = frame.shape[:2]
        assignments = {}
        for detection in detections:
            if detection.label == 'person':
                continue
            x, y, width, height = detection.bbox
            crop = frame[max(0, y):max(0, y) + height, max(0, x):max(0, x) + width]
            category = detection.label
            shape_only = detection.metadata.get('category_evidence') == 'geometry_only'
            patch_proof = detection.metadata.get('reference_patch_proof')
            if detection.metadata.get('backend') == 'dinov2_reference_patches' and isinstance(patch_proof, dict):
                issued = issued_patches.get(id(detection))
                if (issued is not None and issued[0] is frame
                        and issued[1] == detection.bbox and issued[2] == patch_proof
                        and self.reference_patches is not None
                        and self.reference_patches.health().get('available')
                        and self.loaded_profile_versions.get(patch_proof.get('best_item_id')) == patch_proof.get('profile_version')):
                    matched = dict(patch_proof)
                else:
                    matched = {'accepted': False, 'item_id': None, 'rejection': 'unbound_reference_patch_proof'}
                if not matched.get('accepted'):
                    detection.identity = f'generic:unverified-reference:{id(detection)}'
                    detection.metadata.pop('identity_evidence', None)
            elif shape_only:
                # Public negative tests found visually similar phone faces can
                # all pass one appearance profile. A rectangle is not category
                # evidence: this auxiliary route is display-only, not a way to
                # bypass the neural detector or create an identity observation.
                matched = {'accepted': False, 'item_id': None, 'rejection': 'shape_requires_category_confirmation',
                           'best_score': None, 'second_score': None, 'appearance_match_attempted': False}
            elif min(width, height) < int(self.settings.get('identity_min_crop_pixels', 32)):
                matched = {'accepted': False, 'rejection': 'insufficient_detail', 'best_score': None, 'second_score': None}
            elif self.reference_matcher is None:
                matched = {'accepted': False, 'rejection': 'profile_or_model_not_ready'}
            else:
                matched = self.reference_matcher.match(crop, category=category)
                if matched.get('error'):
                    self._recognition_error = str(matched['error'])
            proposal = {'proposal_backend': detection.metadata.get('backend') or 'nanodet',
                        'category_evidence': 'geometry_only' if shape_only else detection.metadata.get('category_evidence', 'model'),
                        'geometry_score': detection.metadata.get('geometry_score') if shape_only else None}
            matched = {**matched, **proposal}
            item_id = matched.get('item_id') if matched.get('accepted') else None
            score = float(matched.get('best_score') or 0)
            candidate = {
                **matched, 'bbox': [x/width_px, y/height_px, width/width_px, height/height_px],
                'category': category, 'detector_score': None if shape_only else detection.confidence,
                'best_item_name': self.item_by_id.get(matched.get('best_item_id'), {}).get('name'),
                'second_item_name': self.item_by_id.get(matched.get('second_item_id'), {}).get('name'),
                'margin': matched.get('gap'), 'rejection_reason': matched.get('rejection'),
            }
            self._recognition_candidates.append(candidate)
            if item_id and item_id in self.item_by_id:
                detection.metadata.update({'generic_label': category, 'reference_score': score, 'detector_score': None if shape_only else detection.confidence, 'identity_evidence': matched})
                detection.identity = item_id
                detection.label = str(self.item_by_id[item_id].get("name") or detection.label)
                # Identity strength is not a calibrated correctness probability.
                detection.confidence = score
                assignments.setdefault(item_id, []).append((detection, candidate))
        for item_id, competing in assignments.items():
            if len(competing) > 1:
                # One reference matching two physical candidates is ambiguous;
                # never silently take the strongest and call both one phone.
                for detection, candidate in competing:
                    detection.identity = f'generic:ambiguous:{id(detection)}'
                    candidate.update({'accepted': False, 'item_id': None, 'rejection': 'multiple_spatial_candidates', 'rejection_reason': 'multiple_spatial_candidates'})
                    detection.metadata['identity_evidence'] = {**detection.metadata['identity_evidence'],
                        'accepted': False, 'item_id': None, 'rejection': 'multiple_spatial_candidates'}
        self._issued_reference_patch_detections.clear()

    def _hand_near(self, track: TrackedObservation, hands: list, frame_shape: tuple[int, ...]) -> bool:
        diagonal = math.hypot(frame_shape[1], frame_shape[0])
        margin = diagonal * float(self.settings.get('hand_object_margin', .02))
        x, y, width, height = track.bbox
        for hand in hands:
            for px, py in hand.landmarks:
                px, py = px*frame_shape[1], py*frame_shape[0]
                dx = max(x-px, 0, px-(x+width))
                dy = max(y-py, 0, py-(y+height))
                if math.hypot(dx, dy) <= margin:
                    return True  # proximity only, not a proven grip/contact
        return False

    def _emit_track(self, track: TrackedObservation, zone: Zone | None, hand_near: bool, packet: FramePacket) -> None:
        if self.on_track is None:
            return
        # Bound callback pressure even if inference FPS is raised.
        machine = self.state_machines[track.identity]
        photo_proof = self.observation_gate.verified.get(track.identity) if track.detection_mode == 'experimental' else None
        verified = bool(photo_proof and photo_proof['motion'].source_frame == packet.sequence) if track.detection_mode == 'experimental' else machine.observation_verified
        emission_state = f"{machine.state.value}:{verified}"
        last_emit = self._last_track_emit.get(track.identity, 0.0)
        if self._last_track_emit_state.get(track.identity) == emission_state and packet.monotonic_time - last_emit < min(1.0, 1.0 / min(self.inference_fps, 5.0)):
            return
        self._last_track_emit[track.identity] = packet.monotonic_time
        self._last_track_emit_state[track.identity] = emission_state
        item = self.item_by_id[track.identity]
        timestamp = datetime.fromtimestamp(packet.wall_time, timezone.utc).isoformat()
        history = self._track_history.setdefault(track.identity, deque(maxlen=30))
        if track.recovered:
            history.clear()
        history.append({"center": list(track.center_norm), "zone_id": zone.id if zone else None, "timestamp": timestamp,
                        "source_frame":packet.sequence,"source_session_id":packet.source_session_id,"recovered": track.recovered})
        payload = {
            "track_id": track.track_id, "item_id": track.identity, "item_name": item.get("name"),
            "camera_id": self.camera_id, "center": list(track.center_norm), "bbox": list(track.bbox),
            "zone_id": zone.id if zone else None, "zone_name": zone.name if zone else "未定义区域",
            "state": machine.state.value, "confidence": track.confidence, "speed": track.speed_norm_s,
            "hand_near": hand_near, "detection_mode": track.detection_mode,
            "holding_status": self._item_interactions.get(track.identity, {}).get('holding_status', 'not_established'),
            "hand_interaction": deepcopy(self._item_interactions.get(track.identity, {})),
            "hand_model_healthy": bool(self._safe_hand_health().get('available') and not self._safe_hand_health().get('error')),
            "timestamp": timestamp, "last_seen": timestamp, "history": list(history), "recovered": track.recovered,
            "runtime_mode": self.runtime_mode,
            "source_type": self.source_type,
            "is_simulated": self.is_simulated,
            "source_session_id": self._active_source_session,
            "source_frame": packet.sequence,
            "source_timestamp": timestamp,
            "reconnect_epoch": packet.reconnect_epoch,
            "detector_backend": self._track_detector_backend(track),
            "tracker_backend": "stable_identity",
            "state_diagnostics": machine.diagnostics(),
            "observation_verified": verified,
            "observation_source_frame": packet.sequence if verified else None,
            "identity_evidence": deepcopy(track.metadata.get('identity_evidence')),
            "scene_version": zone.scene_version if zone and not self.scene_requires_review else None,
            "support_surface_id": zone.id if zone else None,
            "support_surface_confirmed": bool(zone and zone.support_surface_confirmed and not self.scene_requires_review),
            # A 2-D box overlapping furniture does not establish 3-D contact.
            "support_contact_confirmed": False,
        }
        try:
            self.on_track(payload)
        except Exception as exc:
            self._last_error = f"轨迹回调失败：{exc}"

    def _emit_missing_track(
        self,
        track: TrackedObservation,
        machine: ItemMotionStateMachine,
        packet: FramePacket,
    ) -> None:
        """Propagate honest current-state changes without creating history."""
        photo_proof = self.observation_gate.verified.get(track.identity) if track.detection_mode == 'experimental' else None
        observation = photo_proof['motion'] if photo_proof else machine.last_verified_observation
        if self.on_track is None or observation is None or machine.state.value not in {"OCCLUDED", "LOST"}:
            return
        emission_state = f"{machine.state.value}:True"
        last_emit = self._last_track_emit.get(track.identity, 0.0)
        if self._last_track_emit_state.get(track.identity) == emission_state and packet.monotonic_time - last_emit < min(1.0, 1.0 / min(self.inference_fps, 5.0)):
            return
        self._last_track_emit[track.identity] = packet.monotonic_time
        self._last_track_emit_state[track.identity] = emission_state
        updated_at = datetime.fromtimestamp(packet.wall_time, timezone.utc).isoformat()
        last_seen = datetime.fromtimestamp(
            observation.source_timestamp if observation.source_timestamp is not None else packet.wall_time,
            timezone.utc,
        ).isoformat()
        item = self.item_by_id[track.identity]
        payload = {
            "track_id": track.track_id,
            "item_id": track.identity,
            "item_name": item.get("name"),
            "camera_id": self.camera_id,
            "center": list(observation.center_norm),
            "bbox": list(track.bbox),
            "zone_id": observation.zone_id,
            "zone_name": observation.zone_name or "未定义区域",
            "state": machine.state.value,
            "confidence": observation.confidence,
            "speed": 0.0,
            "hand_near": False,
            "holding_status": self._item_interactions.get(track.identity, {}).get('holding_status', 'not_established'),
            "hand_interaction": deepcopy(self._item_interactions.get(track.identity, {})),
            "hand_model_healthy": bool(self._safe_hand_health().get('available') and not self._safe_hand_health().get('error')),
            "detection_mode": observation.detection_mode,
            "timestamp": updated_at,
            "last_seen": last_seen,
            "history": list(self._track_history.get(track.identity, ())),
            "recovered": False,
            "runtime_mode": self.runtime_mode,
            "source_type": self.source_type,
            "is_simulated": self.is_simulated,
            "source_session_id": self._active_source_session,
            "source_frame": packet.sequence,
            "source_timestamp": updated_at,
            "reconnect_epoch": packet.reconnect_epoch,
            "detector_backend": self._track_detector_backend(track),
            "tracker_backend": "stable_identity",
            "state_diagnostics": machine.diagnostics(),
            "observation_verified": True,
            "observation_source_frame": observation.source_frame,
            "identity_evidence": deepcopy(photo_proof['identity'] if photo_proof else track.metadata.get('identity_evidence')),
        }
        try:
            self.on_track(payload)
        except Exception as exc:
            self._last_error = f"遮挡状态回调失败：{exc}"

    def _track_detector_backend(self, track: TrackedObservation) -> str:
        if track.metadata.get('category_evidence') == 'geometry_only':
            return 'phone_shape_proposal'
        if track.metadata.get('backend') == 'dinov2_reference_patches':
            return 'dinov2_reference_patches'
        return 'nanodet' if self._active_detection_mode() == 'experimental' else 'aruco'

    def _queue_event(self, track: TrackedObservation, state_event: StateEvent, packet: FramePacket, annotated: np.ndarray) -> None:
        # Current observations are delivered through on_track. Only a complete
        # evidence-gated movement episode may enter historical persistence.
        if track.metadata.get('category_evidence') == 'geometry_only':
            return  # No confirmation or clip from shape-only category evidence.
        if not bool(self.settings.get("record_events", True)):
            return
        if state_event.event_type != "movement":
            self._last_error = f"拒绝非完整运动事件：{state_event.event_type}"
            return
        event_id = state_event.movement_session_id or str(uuid.uuid4())
        if event_id in self._event_ids:
            self._last_error = f"拒绝重复 movement_session_id：{event_id}"
            return
        # Select evidence by immutable source facts, never by "last N seconds".
        # An overlong movement can exhaust the bounded ring; in that case keep
        # last-seen state, but refuse to manufacture a before frame from its tail.
        with self._ring_lock:
            selected = [entry for entry in self.ring
                if entry.source_session_id == state_event.source_session_id
                and entry.reconnect_epoch == packet.reconnect_epoch
                and state_event.source_frame_start <= entry.sequence <= state_event.source_frame_end]
        max_gap = float(self.settings.get("max_frame_gap_seconds", .75))
        valid_endpoints = (len(selected) >= 2 and packet.source_session_id == state_event.source_session_id
            and packet.sequence == state_event.source_frame_end
            and selected[0].sequence == state_event.source_frame_start
            and selected[-1].sequence == state_event.source_frame_end
            and abs(selected[0].wall_time - state_event.source_timestamp_start) <= 1e-6
            and abs(selected[-1].wall_time - state_event.source_timestamp_end) <= 1e-6
            and abs(selected[-1].monotonic_time - packet.monotonic_time) <= 1e-6
            and np.array_equal(selected[-1].frame, annotated))
        continuous = valid_endpoints and all(
            left.sequence < right.sequence and 0 < right.monotonic_time - left.monotonic_time <= max_gap
            and 0 < right.wall_time - left.wall_time <= max_gap + 1e-6
            for left, right in zip(selected, selected[1:]))
        if not continuous:
            self._last_error = "确认移动的起点/终点或连续视频帧缺失；只保留最后看到，不保存不完整历史证据"
            return
        frames = [entry.frame.copy() for entry in selected]
        buffered_bytes = sum(value.nbytes for value in frames)
        if buffered_bytes > self._ring_max_bytes:
            self._last_error = "确认移动视频超过证据缓冲上限；只保留最后看到"
            return
        fingerprint = (
            track.identity,
            state_event.previous_zone_id,
            state_event.new_zone_id,
            tuple(round(value, 2) for value in (state_event.from_position or ())),
            tuple(round(value, 2) for value in (state_event.to_position or ())),
        )
        if self.is_simulated and fingerprint in self._simulated_fingerprints:
            self._last_error = "模拟/视频循环中的同一运动已去重"
            return
        self._event_ids.add(event_id)
        if self.is_simulated:
            if len(self._simulated_fingerprint_order) == self._simulated_fingerprint_order.maxlen:
                expired = self._simulated_fingerprint_order.popleft()
                self._simulated_fingerprints.discard(expired)
            self._simulated_fingerprint_order.append(fingerprint)
            self._simulated_fingerprints.add(fingerprint)

        item = self.item_by_id.get(track.identity, {"name": track.label})
        timestamp_start = datetime.fromtimestamp(state_event.source_timestamp_start or packet.wall_time, timezone.utc).isoformat()
        timestamp_end = datetime.fromtimestamp(state_event.source_timestamp_end or packet.wall_time, timezone.utc).isoformat()
        ingested_at = datetime.now(timezone.utc).isoformat()
        event = {
            "id": event_id, "event_id": event_id, "item_id": track.identity,
            "item_name": item.get("name") or track.label, "event_type": state_event.event_type,
            "camera_id": self.camera_id, "camera_name": self.camera_name, "room_name": self.room_name,
            "zone_id": state_event.new_zone_id, "zone_name": state_event.new_zone_name or "未定义区域",
            "timestamp_start": timestamp_start, "timestamp_end": timestamp_end,
            "movement_session_id": event_id,
            "runtime_mode": self.runtime_mode,
            "source_type": self.source_type,
            "is_simulated": self.is_simulated,
            "source_session_id": state_event.source_session_id or self._active_source_session,
            "source_frame_start": state_event.source_frame_start,
            "source_frame_end": state_event.source_frame_end,
            "reconnect_epoch": packet.reconnect_epoch,
            "source_timestamp_start": timestamp_start,
            "source_timestamp_end": timestamp_end,
            "ingested_at": ingested_at,
            "detector_backend": self._track_detector_backend(track),
            "tracker_backend": "stable_identity",
            "confidence": state_event.confidence, "evidence_type": state_event.evidence_type,
            "previous_zone": state_event.previous_zone_name, "new_zone": state_event.new_zone_name,
            "from_zone": state_event.previous_zone_name,
            "to_zone": state_event.new_zone_name,
            "from_zone_id": state_event.previous_zone_id,
            "to_zone_id": state_event.new_zone_id,
            "from_position": list(state_event.from_position) if state_event.from_position else None,
            "to_position": list(state_event.to_position) if state_event.to_position else None,
            "final_position": list(state_event.to_position) if state_event.to_position else None,
            "pickup_evidence": dict(state_event.pickup_evidence),
            "placement_evidence": dict(state_event.placement_evidence),
            "evidence_status": state_event.evidence_status,
            "source_continuity_ok": True,
            "reconnect_epoch_changed": False,
            "stable_before": True,
            "stable_after": True,
            "meaningful_position_change": (
                state_event.previous_zone_id != state_event.new_zone_id
                or (
                    state_event.from_position is not None
                    and state_event.to_position is not None
                    and math.dist(state_event.from_position, state_event.to_position)
                    >= float(self.settings.get("same_zone_move_distance", 0.10))
                )
            ),
            "screenshot_path": None,
            "screenshot_sha256": None,
            "clip_path": None,
            "clip_sha256": None,
            "detection_mode": track.detection_mode,
            "identity_evidence": deepcopy(track.metadata.get('identity_evidence')) if track.detection_mode == 'experimental' else None,
            "profile_generation": self._applied_profile_revision if track.detection_mode == 'experimental' else None,
            "human_review_status": "unreviewed",
            "created_by": "vision_engine",
            "pinned": False,
            "manually_corrected": False,
            "notes": state_event.notes,
            "source_bound_clip": True,
            "before_frame_index": 0,
            "after_frame_index": len(frames) - 1,
            "clip_source_frame_start": selected[0].sequence,
            "clip_source_frame_end": selected[-1].sequence,
            "clip_source_timestamp_start": timestamp_start,
            "clip_source_timestamp_end": timestamp_end,
            "clip_fps": (len(frames) - 1) / (selected[-1].monotonic_time - selected[0].monotonic_time),
            "clip_collected_post_seconds": 0.,
            "clip_frame_sources": [{"source_session_id": entry.source_session_id,
                "reconnect_epoch": entry.reconnect_epoch, "sequence": entry.sequence,
                "source_timestamp": datetime.fromtimestamp(entry.wall_time, timezone.utc).isoformat()}
                for entry in selected],
        }
        target = self.post_seconds if self.save_clips else 0.0
        if track.detection_mode == 'experimental':
            # The release gate already observed the object's full five-second
            # settling tail. Persist now; do not silently make the user wait a
            # second five-second post-roll before the location is recorded.
            target = 0.0
        pending = _PendingEvent(event, frames[-1].copy(), frames, packet.monotonic_time,
            packet.monotonic_time + target, selected[-1].monotonic_time - selected[0].monotonic_time + target,
            clip_started_monotonic=selected[0].monotonic_time, clip_last_monotonic=selected[-1].monotonic_time,
            max_frames=len(frames) + math.ceil((target + 1.) * self.inference_fps), buffered_bytes=buffered_bytes)
        with self._pending_lock:
            if len(self.pending) >= self._max_pending_events:
                self._event_ids.discard(event_id)
                if self.is_simulated:
                    self._simulated_fingerprints.discard(fingerprint)
                    try:
                        self._simulated_fingerprint_order.remove(fingerprint)
                    except ValueError:
                        pass
                self._last_error = "后置片段队列已满，确认事件未持久化"
                return
            self.pending.append(pending)
        if target <= 0:
            self._finalize_due(packet.monotonic_time)

    def _append_pending(self, packet: FramePacket | float, frame: np.ndarray) -> None:
        timestamp = packet.monotonic_time if isinstance(packet, FramePacket) else float(packet)
        with self._acceptance_lock:
            acceptance_max_gaps = {
                run_id: run.config.thresholds.max_frame_gap_seconds
                for run_id, run in self._acceptance_runs.items()
            }
        with self._pending_lock:
            rejected: list[_PendingEvent] = []
            for pending in self.pending:
                validation_run_id = str(pending.event.get("validation_run_id") or "")
                source_bound = pending.event.get("source_bound_clip") is True
                within_postroll_boundary = timestamp <= pending.finalize_at + 0.25
                if source_bound:
                    if (not isinstance(packet, FramePacket)
                            or packet.source_session_id != pending.event.get("source_session_id")
                            or packet.reconnect_epoch != pending.event.get("reconnect_epoch")):
                        rejected.append(pending)
                        self._last_error = "移动后置视频来源已改变；未保存跨来源证据"
                        continue
                    if packet.sequence <= pending.event["clip_source_frame_end"]:
                        continue  # Replayed capture frames do not extend post-roll.
                    max_gap = float(self.settings.get("max_frame_gap_seconds", .75))
                    if (pending.clip_last_monotonic is None
                            or not 0 < timestamp - pending.clip_last_monotonic <= max_gap):
                        rejected.append(pending)
                        self._last_error = "移动后置视频断流；未保存不连续证据"
                        continue
                    within_postroll_boundary = True
                if validation_run_id:
                    # Include the first continuous source frame crossing the
                    # five-second boundary, even at a legal slower cadence.
                    # The old fixed 250 ms tail rejected a complete 5.31 s
                    # buffer at 590 ms/frame although its source gap was valid.
                    within_postroll_boundary = (
                        isinstance(packet, FramePacket)
                        and packet.source_session_id == pending.event.get("source_session_id")
                        and packet.reconnect_epoch == pending.event.get("reconnect_epoch")
                        and pending.clip_last_monotonic is not None
                        and 0 < timestamp - pending.clip_last_monotonic
                        <= acceptance_max_gaps.get(validation_run_id, 0.0)
                    )
                if timestamp > pending.started_monotonic and within_postroll_boundary:
                    if pending.event.get("validation_run_id"):
                        max_frames = max(
                            len(pending.frames) + math.ceil((ACCEPTANCE_POST_SECONDS + 1.0) * self.inference_fps),
                            math.ceil((ACCEPTANCE_PRE_SECONDS + ACCEPTANCE_POST_SECONDS + 1.0) * self.inference_fps),
                        )
                    else:
                        max_frames = pending.max_frames or max(2, math.ceil((self.pre_seconds + self.post_seconds + 1.0) * self.inference_fps))
                    if source_bound and (len(pending.frames) >= max_frames
                            or pending.buffered_bytes + frame.nbytes > self._ring_max_bytes):
                        rejected.append(pending)
                        self._last_error = "移动后置视频超过证据缓冲上限；未保存截断证据"
                        continue
                    if len(pending.frames) < max_frames:
                        pending.frames.append(frame.copy())
                        pending.buffered_bytes += frame.nbytes
                        if isinstance(packet, FramePacket) and (validation_run_id or source_bound):
                            pending.clip_last_monotonic = packet.monotonic_time
                            pending.event["clip_source_frame_end"] = packet.sequence
                            pending.event["clip_source_timestamp_end"] = datetime.fromtimestamp(packet.wall_time, timezone.utc).isoformat()
                            if source_bound:
                                pending.event["clip_frame_sources"].append({
                                    "source_session_id": packet.source_session_id,
                                    "reconnect_epoch": packet.reconnect_epoch, "sequence": packet.sequence,
                                    "source_timestamp": pending.event["clip_source_timestamp_end"]})
                            if (
                                pending.clip_started_monotonic is not None
                                and packet.monotonic_time > pending.clip_started_monotonic
                                and len(pending.frames) > 1
                            ):
                                pending.event["clip_fps"] = (
                                    (len(pending.frames) - 1)
                                    / (packet.monotonic_time - pending.clip_started_monotonic)
                                )
                            pending.event["clip_collected_post_seconds"] = max(
                                0.0,
                                packet.monotonic_time - pending.started_monotonic,
                            )
            if rejected:
                rejected_ids = {id(value) for value in rejected}
                self.pending = [value for value in self.pending if id(value) not in rejected_ids]
        self._finalize_due(timestamp)

    def _finalize_due(self, timestamp: float) -> None:
        due: list[_PendingEvent] = []
        with self._pending_lock:
            remaining: list[_PendingEvent] = []
            for pending in self.pending:
                (due if timestamp >= pending.finalize_at else remaining).append(pending)
            self.pending = remaining
        for pending in due:
            self._finalize(pending, stopped_early=False)

    def _finalize_all(self, stopped_early: bool) -> None:
        with self._pending_lock:
            pending, self.pending = self.pending, []
        for item in pending:
            self._finalize(item, stopped_early=stopped_early)

    def _finalize(self, pending: _PendingEvent, stopped_early: bool) -> None:
        validation_run_id = str(pending.event.get("validation_run_id") or "")
        if not self._event_profile_is_current(pending.event):
            self._last_error = '物品档案已更新，已取消旧档案的待保存移动事件'
            return
        if stopped_early:
            # A source switch/stop invalidates continuity and may truncate the
            # promised post-roll.  Do not persist a confirmed event whose clip
            # spans an interrupted source session; the new session must rebuild
            # its own stable baseline.
            self._last_error = f"事件 {pending.event.get('event_id')} 因摄像头中断取消；未保存不完整证据"
            if validation_run_id:
                self._interrupt_acceptance("post_roll_interrupted_before_persistence", run_id=validation_run_id)
            return
        context = self._acceptance_lock if validation_run_id else nullcontext()
        with context:
            acceptance_run = self._acceptance_runs.get(validation_run_id) if validation_run_id else None
            if validation_run_id and (
                acceptance_run is None
                or acceptance_run.state != AcceptanceState.MOVEMENT_CONFIRMED
                or acceptance_run.event_count != 0
            ):
                self._last_error = f"验收事件 {pending.event.get('event_id')} 已取消或已发射"
                return
            if validation_run_id:
                collected_post = float(pending.event.get("clip_collected_post_seconds") or 0.0)
                if collected_post + 1e-6 < ACCEPTANCE_POST_SECONDS:
                    self._last_error = f"验收事件 {pending.event.get('event_id')} 缺少完整 5 秒后置证据"
                    if acceptance_run is not None:
                        self._interrupt_acceptance_run_locked(
                            acceptance_run,
                            "five_second_post_roll_incomplete",
                        )
                    return
                pending.event["clip_post_roll_complete"] = True
            if self._media_executor_closed:
                self._last_error = "媒体执行器已关闭，事件未持久化"
                if acceptance_run is not None:
                    self._interrupt_acceptance_run_locked(
                        acceptance_run,
                        "media_executor_closed_before_emit",
                    )
                return
            if not self._media_slots.acquire(blocking=False):
                # Failing closed is preferable to writing a confirmed event without
                # its promised evidence. Health exposes the rejection.
                self._last_error = "媒体任务队列已满，确认事件未持久化"
                if acceptance_run is not None:
                    self._interrupt_acceptance_run_locked(
                        acceptance_run,
                        "media_worker_queue_full_before_emit",
                    )
                return
            try:
                future = self._media_executor.submit(self._persist_event, pending)
            except Exception as exc:
                self._media_slots.release()
                self._last_error = f"媒体任务提交失败：{exc}"
                if acceptance_run is not None:
                    self._interrupt_acceptance_run_locked(
                        acceptance_run,
                        "media_submit_failed_before_emit",
                    )
                return
            with self._media_lock:
                self._media_futures.add(future)
            future.add_done_callback(
                lambda completed, run_id=validation_run_id: self._media_done(completed, run_id)
            )

    def _media_done(self, future: Future[object], validation_run_id: str = "") -> None:
        with self._media_lock:
            self._media_futures.discard(future)
        self._media_slots.release()
        try:
            future.result()
        except Exception as exc:
            self._last_error = f"媒体任务失败：{exc}"
            if validation_run_id:
                self._interrupt_acceptance(
                    "event_persistence_failed",
                    run_id=validation_run_id,
                )
            return
        if validation_run_id:
            with self._acceptance_lock:
                run = self._acceptance_runs.get(validation_run_id)
                if run is not None and run.state == AcceptanceState.MOVEMENT_CONFIRMED and run.event_count == 0:
                    # Confirmation is not complete until EventService has
                    # atomically committed the event, its three media rows and
                    # the current-state update.  A queued Future is not proof.
                    run.mark_event_emitted()
                    self._notify_acceptance_status_locked(run)

    def _event_profile_is_current(self, event: dict) -> bool:
        if event.get('detection_mode') != 'experimental':
            return True
        generation = event.get('profile_generation')
        with self._profile_lock:
            return (type(generation) is int
                    and generation == self._applied_profile_revision == self._profile_revision)

    def _persist_event(self, pending: _PendingEvent) -> object:
        event_id = pending.event["event_id"]
        try:
            if not self._event_profile_is_current(pending.event):
                raise RuntimeError('物品档案已更新；旧身份媒体任务未提交后端')
            # The callback receives raw in-process frames only. EventService is
            # the sole owner of media destinations, hashes and provenance
            # bindings, so a vision payload cannot point at unrelated files.
            persisted = self.on_event(pending.event, pending.evidence_frame, pending.frames)
            if persisted is False:
                raise RuntimeError(f"事件 {event_id} 未通过后端证据门")
            return persisted
        except Exception as exc:
            self._last_error = f"事件回调失败：{exc}"
            raise
