"""Real image/clip IO under a long Windows workspace path, no media mocks."""
from pathlib import Path

import cv2
import numpy as np

from apps.api.app.event_service import EventService
from services.vision.events.media import EventMediaWriter


EVENT_ID = 'vision-' + 'a' * 32 + '-' + 'b' * 12 + '-before'


def long_media_root(tmp_path):
    # Keep final paths below 260, but make old redundant ID+UUID temp names
    # exceed it. No registry changes or special extended-path prefixes.
    padding = 176 - len(str(tmp_path.resolve()))
    assert 1 <= padding <= 150
    root = tmp_path / ('d' * padding)
    (root / 'event-images').mkdir(parents=True)
    assert len(str(root / 'event-images' / (EVENT_ID + '.jpg'))) < 260
    return root


def test_event_service_atomic_image_with_long_valid_destination(tmp_path):
    root = long_media_root(tmp_path)
    service = EventService.__new__(EventService)
    service.media_root = root
    result = service._atomic_image(EVENT_ID, np.full((48, 64, 3), 80, np.uint8))
    path = root / 'event-images' / Path(result).name
    assert path.is_file()
    assert cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), 1).shape == (48, 64, 3)
    assert not list(root.rglob('*.tmp*'))


def test_writer_image_and_clip_with_long_valid_destination(tmp_path):
    writer = EventMediaWriter(long_media_root(tmp_path))
    frames = [np.full((48, 64, 3), 30 + i * 15, np.uint8) for i in range(8)]
    image = writer.write_image(EVENT_ID, frames[0])
    assert image is not None and image.is_file()
    clip = writer.write_clip(EVENT_ID, frames, 5)
    assert clip.path is not None and clip.path.is_file(), clip.error
    assert writer.sha256(clip.path) == clip.sha256
    capture = cv2.VideoCapture(str(clip.path))
    count = 0
    try:
        assert capture.isOpened()
        while capture.read()[0]:
            count += 1
    finally:
        capture.release()
    assert count == len(frames)
    assert not list(writer.data_dir.rglob('*.tmp*'))
