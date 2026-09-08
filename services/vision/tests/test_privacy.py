from pathlib import Path
import time

import numpy as np

from services.vision.capture import FramePacket
from services.vision.engine import VisionEngine
from services.vision.events import StateEvent
from services.vision.trackers import TrackedObservation


ROOT = Path(__file__).resolve().parents[3]


def buffer_source_frames(engine, frame, session, *, start=0.):
    """Controlled frame evidence; an empty ring cannot stand in for a clip."""
    for sequence in range(1, 11):
        timestamp = start + (sequence - 1) / 9
        packet = FramePacket(frame, timestamp, 1_700_000_000 + timestamp, sequence, session)
        engine._append_ring(packet, frame)
    return packet


def test_record_events_false_writes_no_media_and_calls_no_callback(tmp_path):
    callbacks = []
    engine = VisionEngine(
        {"id": "cam", "name": "camera", "room_name": "客厅", "source_type": "video", "source": str(ROOT / "demo/sample-videos/object-memory-demo.avi")},
        [{"id": "phone", "name": "我的手机", "aruco_id": 1}], [],
        {"data_dir": str(tmp_path), "record_events": False, "show_hands": False},
        lambda *args: callbacks.append(args),
    )
    track = TrackedObservation(
        "track:phone", "phone", "我的手机", (10, 10, 40, 40), (30, 30), (.2, .3),
        (0, 0), 0, .98, "aruco", 1, 5,
    )
    event = StateEvent("seen", .95, "aruco_motion", "desk", "桌面", "desk", "桌面", "test")
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    engine._queue_event(track, event, FramePacket(frame, 1.0, 1.0, 1), frame)
    assert callbacks == []
    assert engine.pending == []
    assert list((tmp_path / "event-images").glob("*")) == []
    assert list((tmp_path / "event-clips").glob("*")) == []


def test_simulated_loop_rejects_duplicate_movement_fingerprint(tmp_path):
    callbacks = []
    engine = VisionEngine(
        {
            "id": "cam",
            "name": "video",
            "room_name": "客厅",
            "source_type": "video",
            "source": str(ROOT / "demo/sample-videos/object-memory-demo.avi"),
        },
        [{"id": "phone", "name": "我的手机", "aruco_id": 1}],
        [],
        {
            "media_root": str(tmp_path),
            "runtime_mode": "DEMO",
            "source_type": "video_file",
            "record_events": True,
            "show_hands": False,
            "save_clips": False,
            "post_seconds": 0,
        },
        lambda event, *_args: callbacks.append(event),
    )
    track = TrackedObservation(
        "track:phone", "phone", "我的手机", (10, 10, 40, 40), (30, 30), (.7, .3),
        (0, 0), 0, .98, "aruco", 1, 5,
    )
    frame = np.zeros((100, 100, 3), dtype=np.uint8)

    def movement(movement_id: str, source_session: str, *, start=0.) -> StateEvent:
        return StateEvent(
            "movement", .95, "aruco_motion", "desk", "桌面", "sofa", "沙发", "confirmed",
            movement_session_id=movement_id,
            source_session_id=source_session,
            source_frame_start=1,
            source_frame_end=10,
            source_timestamp_start=1_700_000_000 + start,
            source_timestamp_end=1_700_000_001 + start,
            from_position=(.2, .3),
            to_position=(.7, .3),
        )

    first = buffer_source_frames(engine, frame, "loop-one")
    engine._queue_event(track, movement("movement-one", "loop-one"), first, frame)
    second = buffer_source_frames(engine, frame, "loop-two", start=1.)
    engine._queue_event(track, movement("movement-two", "loop-two", start=1.), second, frame)
    engine.stop()
    assert len(callbacks) == 1
    assert callbacks[0]["movement_session_id"] == "movement-one"
    assert not list((tmp_path / "event-images").glob("*.jpg"))


def test_stop_waits_for_media_future_and_closes_executor(tmp_path, monkeypatch):
    callbacks = []
    engine = VisionEngine(
        {
            "id": "cam",
            "name": "video",
            "source_type": "video",
            "source": str(ROOT / "demo/sample-videos/object-memory-demo.avi"),
        },
        [{"id": "phone", "name": "我的手机", "aruco_id": 1}],
        [],
        {
            "media_root": str(tmp_path),
            "runtime_mode": "TEST",
            "source_type": "video_file",
            "show_hands": False,
            "save_clips": False,
            "post_seconds": 0,
        },
        lambda event, *_args: callbacks.append(event),
    )
    original = engine.on_event

    def delayed_callback(*args, **kwargs):
        time.sleep(.15)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "on_event", delayed_callback)
    track = TrackedObservation(
        "track:phone", "phone", "我的手机", (10, 10, 40, 40), (30, 30), (.7, .3),
        (0, 0), 0, .98, "aruco", 1, 5,
    )
    event = StateEvent(
        "movement", .95, "aruco_motion", "desk", "桌面", "sofa", "沙发", "confirmed",
        movement_session_id="slow-media-event",
        source_session_id="source-session",
        source_frame_start=1,
        source_frame_end=10,
        source_timestamp_start=1_700_000_000,
        source_timestamp_end=1_700_000_001,
        from_position=(.2, .3),
        to_position=(.7, .3),
    )
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    packet = buffer_source_frames(engine, frame, "source-session")
    engine._queue_event(track, event, packet, frame)
    started = time.monotonic()
    engine.stop()
    assert time.monotonic() - started >= .1
    assert len(callbacks) == 1
    assert engine._media_executor_closed is True
    assert engine._media_futures == set()
    assert not list((tmp_path / "event-images").glob("*.tmp*"))
