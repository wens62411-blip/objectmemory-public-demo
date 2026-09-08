from __future__ import annotations

import cv2
import numpy as np
import pytest

from services.vision.camera_sources.browser import BrowserCameraSource


def _connected_source(camera_id: str) -> tuple[BrowserCameraSource, str]:
    source = BrowserCameraSource(camera_id, {"camera_id": camera_id})
    assert source.connect()
    return source, source.sender_connected()


def test_oversized_low_byte_jpeg_is_rejected_before_decode(monkeypatch):
    ok, encoded = cv2.imencode(
        ".jpg",
        np.zeros((2161, 8, 3), dtype=np.uint8),
        [cv2.IMWRITE_JPEG_QUALITY, 20],
    )
    assert ok
    content = encoded.tobytes()
    assert 100 <= len(content) < 100_000
    source, sender_token = _connected_source("oversized-jpeg")
    decode_called = False

    def unexpected_decode(*_args, **_kwargs):
        nonlocal decode_called
        decode_called = True
        raise AssertionError("oversized JPEG reached cv2.imdecode")

    monkeypatch.setattr(cv2, "imdecode", unexpected_decode)
    try:
        with pytest.raises(ValueError, match="不能超过"):
            source.push_jpeg(content, sender_token)
        assert decode_called is False
    finally:
        source.disconnect()


def test_normal_jpeg_passes_bounded_header_and_decoder_checks():
    image = np.full((120, 160, 3), (20, 140, 220), dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    source, sender_token = _connected_source("normal-jpeg")
    try:
        result = source.push_jpeg(encoded.tobytes(), sender_token)
        assert result["width"] == 160
        assert result["height"] == 120
        frame = source.read_frame()
        assert frame is not None and frame.shape[:2] == (120, 160)
    finally:
        source.disconnect()
