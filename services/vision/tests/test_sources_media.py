from pathlib import Path

import cv2
import numpy as np
import pytest

from services.vision.camera_sources import VideoFileSource, redact_value
from services.vision.capture import FramePacket, LatestFrameQueue
from services.vision.events import EventMediaWriter


ROOT = Path(__file__).resolve().parents[3]


def test_credentials_are_redacted_recursively():
    source = "rtsp://admin:p%40ss@example.local:554/stream?channel=1"
    safe = redact_value({"source": source, "password": "plain", "nested": {"device_token": "abc"}})
    assert "plain" not in str(safe)
    assert "p%40ss" not in str(safe)
    assert "abc" not in str(safe)
    assert "admin:***@example.local" in safe["source"]
    message = redact_value("connect failed for rtsp://admin:secret@example.local/live?token=abcd&channel=1")
    assert "secret" not in message and "abcd" not in message
    assert "token=%2A%2A%2A" in message


def test_video_source_is_explicit_test_playback_and_reads_frame():
    source = VideoFileSource(ROOT / "demo/sample-videos/object-memory-demo.avi", {"loop": False, "realtime": False})
    assert source.connect()
    frame = source.read_frame()
    assert frame is not None and frame.shape[:2] == (480, 640)
    assert source.get_metadata()["test_playback"] is True
    source.disconnect()


def test_video_loop_marks_a_new_stream_epoch_before_replayed_frame():
    source = VideoFileSource(ROOT / "demo/sample-videos/object-memory-demo.avi", {"loop": True, "realtime": False})
    assert source.connect()
    try:
        frame_count = source.get_metadata()["frame_count"]
        for _ in range(frame_count):
            assert source.read_frame() is not None
        replayed = source.read_frame()
        discontinuity = source.consume_stream_discontinuity()
        assert replayed is not None
        assert discontinuity == (1, "video_loop")
        assert source.get_metadata()["stream_epoch"] == 1
    finally:
        source.disconnect()


def test_latest_queue_discards_old_frames():
    queue = LatestFrameQueue(maxsize=2)
    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    for sequence in range(5):
        queue.put_latest(FramePacket(frame, float(sequence), float(sequence), sequence))
    assert queue.dropped == 3
    assert queue.get().sequence == 3
    assert queue.get().sequence == 4


@pytest.mark.parametrize('strided', [False, True])
def test_media_writer_creates_real_image_and_playable_clip(tmp_path, strided):
    writer = EventMediaWriter(tmp_path)
    frames = []
    for index in range(8):
        frame = np.full((120, 160, 3), 240, dtype=np.uint8)
        cv2.circle(frame, (20 + index * 12, 60), 12, (20, 100, 220), -1)
        frames.append(np.repeat(frame, 2, axis=1)[:, ::2] if strided else frame)
    originals = [frame.copy() for frame in frames]
    image = writer.write_image("event-a", frames[3])
    clip = writer.write_clip("event-a", frames, 4)
    assert image and image.is_file() and image.stat().st_size > 100
    assert clip.path and clip.path.is_file() and clip.frame_count == 8
    capture = cv2.VideoCapture(str(clip.path))
    ok, decoded = capture.read()
    capture.release()
    assert ok and decoded is not None
    assert writer.sha256(image)
    assert clip.sha256 == writer.sha256(clip.path)
    for frame, original in zip(frames, originals):
        np.testing.assert_array_equal(frame, original)
    assert not list((tmp_path / "event-images").glob("*.tmp*"))
    assert not list((tmp_path / "event-clips").glob("*.tmp*"))


def test_media_writer_failure_releases_writer_and_removes_temporary_files(tmp_path, monkeypatch):
    writer = EventMediaWriter(tmp_path)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)

    class BrokenWriter:
        released = False

        def isOpened(self):
            return True

        def write(self, _frame):
            raise RuntimeError("encode failed")

        def release(self):
            self.released = True

    broken = BrokenWriter()
    monkeypatch.setattr("imageio_ffmpeg.get_ffmpeg_exe", lambda: (_ for _ in ()).throw(RuntimeError("no ffmpeg")))
    monkeypatch.setattr(cv2, "VideoWriter", lambda *_args, **_kwargs: broken)
    result = writer.write_clip("event-failure", [frame], 5)
    assert result.path is None
    assert broken.released is True
    assert list((tmp_path / "event-clips").iterdir()) == []


def test_image_becomes_visible_only_through_atomic_replace(tmp_path, monkeypatch):
    writer = EventMediaWriter(tmp_path)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    replacements = []
    real_replace = __import__("os").replace

    def record_replace(source, destination):
        source = Path(source)
        destination = Path(destination)
        assert source.is_file()
        assert not destination.exists()
        replacements.append((source.name, destination.name))
        real_replace(source, destination)

    monkeypatch.setattr("services.vision.events.media.os.replace", record_replace)
    result = writer.write_image("atomic-event", frame)
    assert result and result.is_file()
    assert replacements and replacements[0][1] == "atomic-event.jpg"
