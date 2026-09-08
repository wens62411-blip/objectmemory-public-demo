"""Real loopback HTTP/MJPEG + isolated SQLite; generated JPEG fixtures, not hardware."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import cv2
import numpy as np
import pytest

from apps.api.app.firmware import FirmwareService, camera_read_token, hash_device_token
from apps.api.tests.test_camera_read_auth import AuthenticatedHandler
from apps.api.tests.test_firmware import claim_input


class ProbeStreamHandler(AuthenticatedHandler):
    def do_GET(self):
        if self.path != "/stream":
            return super().do_GET()
        self.server.requests.append((self.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != "Bearer " + self.server.read_token:
            self.send_error(401)
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        try:
            for jpeg in self.server.stream_frames:
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


@contextmanager
def probe_server(frames):
    server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeStreamHandler)
    server.requests = []
    server.read_token = "not-set-before-test-enrollment"
    server.stream_frames = frames
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


@pytest.mark.parametrize("stream_kind", ["valid", "marker_only", "no_scan", "oversized_pixels", "valid_then_invalid"])
def test_firmware_probe_requires_decodable_bounded_stream_jpegs(tmp_path, monkeypatch, stream_kind, record_property):
    # The capture endpoint always serves a genuine generated JPEG. Stream
    # variants deliberately test that a good snapshot cannot validate bad video.
    valid = ProbeStreamHandler.jpeg
    assert cv2.imdecode(np.frombuffer(valid, np.uint8), cv2.IMREAD_COLOR) is not None
    no_scan = valid[:valid.index(b"\xff\xda")] + b"\xff\xd9"
    assert cv2.imdecode(np.frombuffer(no_scan, np.uint8), cv2.IMREAD_COLOR) is None
    oversized = b"\xff\xd8\xff\xc0\x00\x11\x08\x7f\xff\x7f\xff" + b"\x00" * 10 + b"\xff\xd9"
    candidates = {
        "valid": [valid] * 3,
        "marker_only": [b"\xff\xd8not-a-jpeg\xff\xd9"] * 3,
        "no_scan": [no_scan] * 3,
        "oversized_pixels": [oversized] * 3,
        "valid_then_invalid": [valid, no_scan, valid],
    }
    with probe_server(candidates[stream_kind]) as (server, base):
        fw = FirmwareService(tmp_path / "probe.sqlite", None, tmp_path / "data", runtime_mode="REAL")
        # Suppress only the background scheduler; this test calls the actual
        # synchronous probe, HTTP client, JPEG decoder and SQLite writer.
        monkeypatch.setattr(fw, "_schedule_probe", lambda *_args: None)
        try:
            code = fw.create_enrollment("Synthetic probe", "Fixture room")["pairing_code"]
            result = fw.claim(claim_input(base_url=base, code=code))
            server.read_token = camera_read_token(hash_device_token(result["device_token"]))
            started = time.monotonic()
            fw._probe_device("omcam-test")
            elapsed = time.monotonic() - started
            record_property("loopback_probe_elapsed_ms", round(elapsed * 1000, 2))
            device = fw.get_device("omcam-test")
            assert {path for path, _ in server.requests} == {"/health", "/device", "/capture", "/stream"}
            assert all(header == "Bearer " + server.read_token for _, header in server.requests)
            assert device["stream_status"] == ("ready" if stream_kind == "valid" else "error")
            if stream_kind != "valid":
                assert device["current_fps"] is None
                assert device["recent_error"]
                assert server.read_token not in device["recent_error"]
                assert elapsed < 3.0
            assert not device["hardware_verified"]
        finally:
            fw.shutdown()
