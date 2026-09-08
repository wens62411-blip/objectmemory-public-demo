from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from services.vision.capture import FramePacket
from services.vision.engine import VisionEngine, _PendingEvent


ROOT = Path(__file__).resolve().parents[3]


def test_test_video_pipeline_emits_one_complete_movement_with_raw_callback_frames(tmp_path):
    events = []
    callback_evidence = []
    tracks = []
    camera = {
        "id": "cam-demo", "name": "客厅测试回放", "room_name": "客厅",
        "source_type": "video", "source": str(ROOT / "demo/sample-videos/object-memory-demo.avi"),
        "config": {"loop": False, "realtime": True}, "inference_fps": 8, "save_clips": True,
    }
    items = [{"id": "item-phone", "name": "我的手机", "aruco_id": 1}]
    zones = [
        {"id": "zone-desk", "camera_id": "cam-demo", "name": "桌面", "points": [[.02,.17],[.46,.17],[.46,.94],[.02,.94]], "priority": 10},
        {"id": "zone-sofa", "camera_id": "cam-demo", "name": "沙发右侧", "points": [[.5,.17],[.99,.17],[.99,.94],[.5,.94]], "priority": 10},
    ]
    settings = {
        "media_root": str(tmp_path), "runtime_mode": "TEST", "source_type": "video_file",
        "detection_mode": "aruco", "inference_fps": 8,
        "min_detection_frames": 3, "min_stable_frames": 3, "min_stable_seconds": .3,
        "min_move_distance": .04, "same_zone_move_distance": .1, "max_frame_gap_seconds": .8,
        "pre_seconds": .5, "post_seconds": .4,
        "show_hands": False, "save_clips": True,
    }
    def on_event(event, frame, clip_frames):
        events.append(event)
        callback_evidence.append((frame.copy(), [value.copy() for value in clip_frames]))

    engine = VisionEngine(camera, items, zones, settings, on_event, tracks.append)
    assert engine.start(), engine.health()
    deadline = time.monotonic() + 16
    try:
        while time.monotonic() < deadline and not events:
            time.sleep(.1)
        health = engine.health()
        assert health["processed_frames"] > 20
        assert engine.get_jpeg()
    finally:
        engine.stop()
    assert len(events) == 1, events
    movement = events[0]
    assert movement["event_type"] == "movement"
    assert movement["previous_zone"] == "桌面"
    assert movement["new_zone"] == "沙发右侧"
    assert movement["runtime_mode"] == "TEST"
    assert movement["source_type"] == "video_file"
    assert movement["is_simulated"] is True
    assert movement["source_session_id"]
    assert movement["source_frame_start"] < movement["source_frame_end"]
    assert movement["source_continuity_ok"] is True
    assert movement["stable_before"] is True and movement["stable_after"] is True
    assert movement["meaningful_position_change"] is True
    assert not movement.get("screenshot_path") and not movement.get("clip_path")
    assert len(callback_evidence) == 1
    assert callback_evidence[0][0].size and len(callback_evidence[0][1]) >= 2
    assert not list((tmp_path / "event-images").glob("*"))
    assert not list((tmp_path / "event-clips").glob("*"))
    assert tracks and tracks[-1]["zone_name"] == "沙发右侧"


def test_rejected_backend_callback_rolls_back_generated_media(tmp_path):
    camera = {
        "id": "cam-reject", "name": "拒绝测试", "room_name": "测试间",
        "source_type": "video", "source": str(ROOT / "demo/sample-videos/object-memory-demo.avi"),
        "config": {"loop": False}, "inference_fps": 5, "save_clips": True,
    }
    engine = VisionEngine(
        camera,
        [],
        [],
        {"media_root": str(tmp_path), "runtime_mode": "TEST", "source_type": "video_file", "save_clips": True},
        lambda *_: False,
    )
    frame_before = np.zeros((80, 120, 3), dtype=np.uint8)
    frame_after = np.full((80, 120, 3), 255, dtype=np.uint8)
    pending = _PendingEvent(
        event={"event_id": "rejected-event", "pickup_evidence": {}, "placement_evidence": {}},
        evidence_frame=frame_after,
        frames=[frame_before, frame_after],
        started_monotonic=0,
        finalize_at=0,
        target_seconds=0,
    )
    try:
        with pytest.raises(RuntimeError, match="未通过后端证据门"):
            engine._persist_event(pending)
    finally:
        engine.stop()
    assert not list((tmp_path / "event-images").glob("*"))
    assert not list((tmp_path / "event-clips").glob("*"))
    assert "未通过后端证据门" in (engine.health()["error"] or "")


def test_source_session_switch_discards_pending_confirmation(tmp_path):
    callbacks=[]
    engine=VisionEngine(
        {"id":"interrupt","source_type":"video","source":str(ROOT / "demo/sample-videos/object-memory-demo.avi")},
        [],[],{"media_root":str(tmp_path),"runtime_mode":"TEST","source_type":"video_file","show_hands":False},
        lambda *args: callbacks.append(args),
    )
    frame=np.zeros((40,60,3),np.uint8)
    pending=_PendingEvent(
        event={"event_id":"interrupted-event"},evidence_frame=frame,
        frames=[frame.copy(),frame.copy()],started_monotonic=1.0,finalize_at=6.0,target_seconds=5.0,
    )
    engine._active_source_session="old-session"
    engine.pending.append(pending)
    try:
        engine._switch_source_session(FramePacket(frame,2.0,2.0,1,"new-session",1))
        assert callbacks==[]
        assert engine.pending==[]
        assert "摄像头中断取消" in (engine.health()["error"] or "")
    finally:
        engine.stop()
