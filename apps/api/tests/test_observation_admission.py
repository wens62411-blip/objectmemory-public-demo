"""Real detector -> engine callback -> isolated SQLite observation admission.

Frames contain a generated, decodable ArUco marker, not a physical camera feed.
No capture is started, no business API is mocked, and no REAL data is touched.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path

import cv2
import numpy as np
import pytest

from apps.api.app.main import Runtime
from apps.api.app.acceptance import AcceptanceService
from apps.api.app.runtime_mode import RuntimeMode, runtime_layout
from services.vision.capture import FramePacket
from services.vision.engine import VisionEngine


WALL_START = datetime(2026, 9, 5, tzinfo=timezone.utc).timestamp()


class ObservationPipeline:
    def __init__(self, root: Path):
        self.runtime = Runtime(runtime_layout(root, root / "data", RuntimeMode.TEST), testing=True)
        self.item = self.runtime.db.save("items", {"name": "测试标记物品", "type": "phone", "aruco_id": 49})
        self.camera = self.runtime.db.save("cameras", {
            "name": "生成标记帧测试来源", "room_name": "测试房间", "source_type": "video",
            "source": str(root / "never-opened.avi"), "runtime_mode": "TEST", "enabled": True,
            "inference_fps": 5, "config": {}, "save_clips": True,
        })
        self.zone = self.runtime.db.save("zones", {
            "camera_id": self.camera["id"], "name": "测试桌面", "enabled": True,
            "points": [[0, 0], [1, 0], [1, 1], [0, 1]], "priority": 1,
        })
        self.payloads = []
        self.engine = VisionEngine(
            self.camera, [self.item], [self.zone],
            # Leave the engine's 5-frame / 1.5-second admission defaults intact.
            {"runtime_mode": "TEST", "media_root": str(self.runtime.media), "show_hands": False},
            on_event=lambda event, frame, clip: self.runtime.event(event, frame, clip, expected_engine=self.engine),
        )
        self.runtime.engines[self.camera["id"]] = self.engine
        self.engine.on_track = self.on_track
        self.sequence = 0
        self.source_frames = {}
        self.session = "generated-marker-session-a"
        self.epoch = 0
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        self.marker = cv2.aruco.generateImageMarker(dictionary, 49, 80)
        machine = self.machine
        assert machine.min_stable_frames == 5
        assert machine.min_stable_seconds == 1.5
        assert machine.occluded_seconds == .6 and machine.lost_seconds == 8

    @property
    def machine(self):
        return self.engine.state_machines[self.item["id"]]

    def on_track(self, payload):
        self.payloads.append(deepcopy(payload))
        self.runtime.track(payload, expected_engine=self.engine)

    def feed(self, elapsed: float, *, visible=True, x=120):
        frame = np.full((480, 640, 3), 255, np.uint8)
        if visible:
            frame[200:280, x:x + 80] = cv2.cvtColor(self.marker, cv2.COLOR_GRAY2BGR)
        self.sequence += 1
        self.source_frames[(self.session, self.sequence)] = frame.copy()
        self.engine._process(FramePacket(frame, 100 + elapsed, WALL_START + elapsed,
                                         self.sequence, self.session, self.epoch))
        assert self.engine._last_error is None, self.engine._last_error
        return self.state()

    def stable(self, start=0.0, *, x=120):
        for i in range(5):
            self.feed(start + i * .4, x=x)
        assert self.machine.observation_verified
        return self.state()

    def state(self):
        return self.runtime.db.get("item_current_state", f"TEST:{self.item['id']}")

    def allow_next_same_state_write(self):
        # Exercise another admitted observation without sleeping or patching
        # real clocks; the missing-state tests deliberately do NOT do this.
        self.runtime.track_writes.clear()

    def media_snapshot(self):
        return {"rows": self.runtime.db.list("event_media"), "files": {
            str(path.relative_to(self.runtime.media)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.runtime.media.rglob("*") if path.is_file()}}

    def assert_no_history_or_clips_with_one_verified_current_image(self):
        assert self.runtime.db.count("movement_events") == 0
        assert not list((self.runtime.media / "event-clips").iterdir())
        rows = self.runtime.db.list("event_media")
        files = [path for path in self.runtime.media.rglob("*") if path.is_file()]
        observed = (self.state() or {}).get("last_observed") or {}
        if not observed.get("screenshot_path"):
            # A single/unverified detection still has no state or media. The
            # new snapshot feature never relaxes the original admission proof.
            assert rows == [] and files == []
        else:
            assert len(rows) == len(files) == 1
            row = rows[0]
            assert row["event_id"] is None and row["kind"] == "image"
            assert row["role"] == "last_observed" and row["status"] == "active"
            assert row["owner_current_state_id"] == f"TEST:{self.item['id']}"
            assert row["runtime_mode"] == "TEST"
            assert row["path"] == observed["screenshot_path"]
            assert row["source_session_id"] == observed["screenshot_source_session_id"]
            assert row["metadata"]["source_frame"] == observed["screenshot_source_frame"]
            assert row["metadata"]["observed_at"] == observed["screenshot_observed_at"]
            image = self.runtime.event_service.media_path(row["path"], "image")
            assert image is not None and image == files[0]
            content = image.read_bytes()
            assert hashlib.sha256(content).hexdigest() == row["sha256"] == observed["screenshot_sha256"]
            decoded = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
            source = self.source_frames[(observed["screenshot_source_session_id"], observed["screenshot_source_frame"])]
            assert decoded is not None and decoded.shape == source.shape
            assert np.abs(decoded.astype(float) - source).mean() < 2
            _corners, ids, _rejected = cv2.aruco.ArucoDetector(
                cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)).detectMarkers(decoded)
            assert ids is not None and 49 in ids.flatten()
        assert not self.engine.pending


@pytest.fixture
def pipeline(tmp_path):
    value = ObservationPipeline(tmp_path)
    try:
        yield value
    finally:
        value.engine.stop()
        value.runtime.engines.clear()


def test_single_real_decodable_detection_cannot_create_current_state(pipeline):
    pipeline.feed(0)
    assert pipeline.payloads and pipeline.payloads[-1]["item_id"] == pipeline.item["id"]
    assert pipeline.payloads[-1]["observation_verified"] is False
    for i in range(1, 24):
        pipeline.feed(i * .4, visible=False)
    assert pipeline.state() is None
    pipeline.assert_no_history_or_clips_with_one_verified_current_image()


def test_intermittent_detection_cannot_accumulate_a_stable_baseline(pipeline):
    for i in range(45):
        pipeline.feed(i * .4, visible=i % 3 != 2)
        assert pipeline.state() is None
    assert not any(p["observation_verified"] for p in pipeline.payloads)
    pipeline.assert_no_history_or_clips_with_one_verified_current_image()


def test_five_fast_frames_still_need_default_stable_duration(pipeline):
    for i in range(5):
        pipeline.feed(i * .2)
    assert pipeline.state() is None
    assert pipeline.machine.baseline is None
    pipeline.assert_no_history_or_clips_with_one_verified_current_image()


def test_stable_observations_update_one_state_and_one_verified_image_without_history(pipeline):
    first = pipeline.stable()
    assert first is not None and first["status"] == "last_seen"
    assert first["is_simulated"] is True and first["source_type"] == "video_file"
    assert first["runtime_mode"] == "TEST"
    pipeline.allow_next_same_state_write()
    latest = pipeline.feed(2.0)
    assert latest["last_seen_at"] > first["last_seen_at"]
    assert latest["current_position"] == first["current_position"]
    assert pipeline.runtime.db.count("item_current_state") == 1
    assert latest["last_observed"]["screenshot_path"] == first["last_observed"]["screenshot_path"]
    assert latest["last_observed"]["screenshot_source_frame"] == first["last_observed"]["screenshot_source_frame"]
    pipeline.assert_no_history_or_clips_with_one_verified_current_image()


def test_occluded_and_lost_bypass_write_throttle_and_preserve_verified_sighting(pipeline):
    verified = pipeline.stable()
    # All calls execute in far less than the 1-second persistence throttle;
    # source timestamps still supply a continuous sequence of blank frames.
    pipeline.feed(2.0, visible=False)
    hidden = pipeline.feed(2.21, visible=False)
    assert hidden["status"] == "occluded"
    for i in range(1, 20):
        pipeline.feed(2.21 + i * .4, visible=False)
    lost = pipeline.state()
    assert lost["status"] == "lost"
    session = pipeline.runtime.db.get("source_sessions", pipeline.session)
    assert session["last_frame"] == pipeline.sequence
    assert session["last_frame_at"] > verified["last_seen_at"]
    for row in (hidden, lost):
        assert row["last_seen_at"] == verified["last_seen_at"]
        assert row["current_position"] == verified["current_position"]
        assert row["current_zone"] == verified["current_zone"]
    pipeline.assert_no_history_or_clips_with_one_verified_current_image()


def test_unstable_recovery_cannot_replace_last_verified_coordinates(pipeline):
    verified = pipeline.stable()
    pipeline.feed(2.0, visible=False)
    pipeline.feed(2.21, visible=False)
    pipeline.feed(2.4, x=400)
    assert pipeline.payloads[-1]["observation_verified"] is False
    pipeline.feed(2.8, visible=False)
    hidden = pipeline.feed(3.01, visible=False)
    assert hidden["status"] == "occluded"
    assert hidden["last_seen_at"] == verified["last_seen_at"]
    assert hidden["current_position"] == verified["current_position"]
    pipeline.assert_no_history_or_clips_with_one_verified_current_image()


def test_new_source_session_requires_a_fresh_stable_baseline(pipeline):
    verified = pipeline.stable()
    pipeline.session = "generated-marker-session-b"
    pipeline.epoch += 1
    pipeline.sequence = 0
    for i in range(4):
        pipeline.feed(2.0 + i * .4, x=400)
        assert pipeline.state()["source_session_id"] == verified["source_session_id"]
        assert pipeline.state()["last_seen_at"] == verified["last_seen_at"]
        assert pipeline.state()["current_position"] == verified["current_position"]
    latest = pipeline.feed(3.6, x=400)
    assert latest["source_session_id"] == pipeline.session
    assert latest["current_position"] != verified["current_position"]
    pipeline.assert_no_history_or_clips_with_one_verified_current_image()


def test_stream_gap_invalidates_baseline_even_without_session_id_change(pipeline):
    verified = pipeline.stable()
    pipeline.feed(3.0, x=400)
    assert pipeline.machine.baseline is None
    assert pipeline.state()["last_seen_at"] == verified["last_seen_at"]
    assert pipeline.state()["current_position"] == verified["current_position"]
    pipeline.assert_no_history_or_clips_with_one_verified_current_image()


def test_payload_boolean_without_internal_engine_proof_is_not_admitted(pipeline):
    pipeline.feed(0)
    forged = {**pipeline.payloads[-1], "observation_verified": True, "observation_source_frame": 1}
    assert pipeline.runtime.event_service.observe(forged) is None
    pipeline.runtime.track(forged)
    pipeline.runtime.track(forged, expected_engine=pipeline.engine)
    assert pipeline.state() is None
    pipeline.assert_no_history_or_clips_with_one_verified_current_image()


@pytest.mark.parametrize("field,value", [
    ("source_session_id", "forged-session"),
    ("observation_source_frame", 999),
    ("center", [.75, .5]),
    ("last_seen", "2099-01-01T00:00:00+00:00"),
])
def test_payload_must_match_server_verified_observation(pipeline, field, value):
    verified = pipeline.stable()
    media_before = pipeline.media_snapshot()
    pipeline.allow_next_same_state_write()
    forged = {**pipeline.payloads[-1], field: value}
    pipeline.runtime.track(forged, expected_engine=pipeline.engine)
    assert pipeline.state() == verified
    assert pipeline.media_snapshot() == media_before
    pipeline.assert_no_history_or_clips_with_one_verified_current_image()


def test_detached_engine_cannot_update_current_state(pipeline):
    verified = pipeline.stable()
    media_before = pipeline.media_snapshot()
    pipeline.runtime.engines.pop(pipeline.camera["id"])
    pipeline.allow_next_same_state_write()
    pipeline.feed(2.0)
    assert pipeline.state() == verified
    assert pipeline.media_snapshot() == media_before
    pipeline.assert_no_history_or_clips_with_one_verified_current_image()


def test_current_source_frame_must_match_internal_callback_generation(pipeline):
    verified = pipeline.stable()
    media_before = pipeline.media_snapshot()
    pipeline.allow_next_same_state_write()
    forged = {**pipeline.payloads[-1], "source_frame": 999}
    pipeline.runtime.track(forged, expected_engine=pipeline.engine)
    assert pipeline.state() == verified
    assert pipeline.media_snapshot() == media_before
    pipeline.assert_no_history_or_clips_with_one_verified_current_image()


def test_changed_camera_source_cannot_relabel_old_engine_observation(pipeline):
    verified = pipeline.stable()
    media_before = pipeline.media_snapshot()
    # Model the real camera PATCH ordering: new config is saved before restart
    # releases the old engine. A callback in this window must be rejected.
    pipeline.runtime.db.save("cameras", {"source_type": "browser", "source": "browser"}, pipeline.camera["id"])
    pipeline.allow_next_same_state_write()
    pipeline.feed(2.0)
    assert pipeline.state() == verified
    assert pipeline.media_snapshot() == media_before
    pipeline.assert_no_history_or_clips_with_one_verified_current_image()


def test_acceptance_marker_visibility_does_not_refresh_a_missing_old_bbox(pipeline):
    # Isolated acceptance presentation-contract unit: the generated TEST frames
    # are explicitly adapted to the REAL-only marker-status input contract.
    # This does not attest a webcam, start capture, or write any REAL database.
    acceptance = AcceptanceService(pipeline.runtime.db, RuntimeMode.REAL,
                                   lambda _camera: ("opencv_camera", False))

    def forward(payload):
        pipeline.on_track(payload)
        acceptance.observe_track({**payload, "runtime_mode": "REAL",
                                  "source_type": "opencv_camera", "is_simulated": False})

    pipeline.engine.on_track = forward

    def marker_status():
        return acceptance.marker_status(
            item_id=pipeline.item["id"], camera_id=pipeline.camera["id"],
            engine_health={"status": "running", "source_session_id": pipeline.session},
        )

    pipeline.stable()
    visible = marker_status()
    assert visible["recognized"] is True
    assert visible["marker_size"]["minimum"] >= 79
    pipeline.feed(2.0, visible=False)
    pipeline.feed(2.21, visible=False)
    hidden = marker_status()
    assert hidden["recognized"] is False
    assert hidden["last_observed_at"] == visible["last_observed_at"]
    assert hidden["current_frame_at"] > hidden["last_observed_at"]
    for i in range(1, 20):
        pipeline.feed(2.21 + i * .4, visible=False)
    lost = marker_status()
    assert lost["recognized"] is False
    assert lost["last_observed_at"] == visible["last_observed_at"]
    pipeline.stable(10.0)
    recovered = marker_status()
    assert recovered["recognized"] is True
    assert recovered["last_observed_at"] > visible["last_observed_at"]
    pipeline.assert_no_history_or_clips_with_one_verified_current_image()
