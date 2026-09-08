from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

from services.vision.camera_sources import CameraSourceAdapter, Esp32CamSource
from services.vision.capture import CaptureWorker


class _Esp32Handler(BaseHTTPRequestHandler):
    frame = np.full((120, 160, 3), (220, 235, 245), dtype=np.uint8)
    cv2.putText(frame, "ESP32 SIM", (20, 67), cv2.FONT_HERSHEY_SIMPLEX, .55, (30, 80, 120), 2)
    jpeg = cv2.imencode(".jpg", frame)[1].tobytes()

    def log_message(self, *_args):
        return

    def _json(self, value):
        payload = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        if not hasattr(self.server, "seen_paths"):
            self.server.seen_paths = []
        self.server.seen_paths.append(self.path)
        if getattr(self.server, "stream_only", False) and self.path != "/stream":
            self.send_error(404)
            return
        if self.path == "/health":
            self._json({"online": True, "camera_status": "ready", "firmware_version": "test"})
        elif self.path == "/device":
            stream_url = getattr(self.server, "external_stream_url", None) or f"{base}/stream"
            if getattr(self.server, "malicious_stream", False):
                stream_url = "http://8.8.8.8/stream"
            self._json({"device_id": "sim", "stream_url": stream_url, "capture_url": f"{base}/capture"})
        elif self.path == "/capture":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(self.jpeg)))
            self.end_headers()
            self.wfile.write(self.jpeg)
        elif self.path == "/stream":
            if getattr(self.server, "redirect_stream_url", None):
                self.send_response(307)
                self.send_header("Location", self.server.redirect_stream_url)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                for _index in range(20):
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(self.jpeg)).encode() + b"\r\n\r\n")
                    self.wfile.write(self.jpeg + b"\r\n")
                    self.wfile.flush()
                    time.sleep(.01)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_error(404)


def test_esp32_adapter_requires_and_reads_real_mjpeg_frame():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Esp32Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    source = Esp32CamSource(f"http://127.0.0.1:{server.server_port}", {"open_timeout_seconds": 2, "read_timeout_seconds": 2})
    try:
        assert source.connect(), source.health_check()
        frame = source.read_frame()
        assert frame is not None and frame.shape[:2] == (120, 160)
        result = source.test_endpoints()
        assert result["video_normal"] is True
        assert result["capture_size"] == [160, 120]
    finally:
        source.disconnect()
        server.shutdown()
        server.server_close()


def test_esp32_rejects_device_stream_redirect_to_another_public_host():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Esp32Handler)
    server.malicious_stream = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    source = Esp32CamSource(f"http://127.0.0.1:{server.server_port}")
    try:
        assert not source.connect()
        assert "不同主机" in source.health_check()["error"]
    finally:
        source.disconnect()
        server.shutdown()
        server.server_close()


def test_esp32_accepts_same_private_host_stream_on_firmware_port_81():
    # Firmware serves health/device/capture on port 80 and MJPEG on port 81.
    # Ephemeral test ports reproduce that two-port topology without privilege.
    stream_server = ThreadingHTTPServer(("127.0.0.1", 0), _Esp32Handler)
    stream_thread = threading.Thread(target=stream_server.serve_forever, daemon=True)
    stream_thread.start()
    control_server = ThreadingHTTPServer(("127.0.0.1", 0), _Esp32Handler)
    control_server.external_stream_url = f"http://127.0.0.1:{stream_server.server_port}/stream"
    control_thread = threading.Thread(target=control_server.serve_forever, daemon=True)
    control_thread.start()
    source = Esp32CamSource(f"http://127.0.0.1:{control_server.server_port}", {"test_frame_count": 2})
    try:
        assert source.connect(), source.health_check()
        frame = source.read_frame()
        assert frame is not None and frame.shape[:2] == (120, 160)
        assert source.device_info["stream_url"].endswith(f":{stream_server.server_port}/stream")
    finally:
        source.disconnect()
        control_server.shutdown()
        stream_server.shutdown()
        control_server.server_close()
        stream_server.server_close()


def test_esp32_enrolled_stream_url_uses_capture_url_control_port_and_307_topology():
    import httpx

    stream_server = ThreadingHTTPServer(("127.0.0.1", 0), _Esp32Handler)
    stream_server.stream_only = True
    threading.Thread(target=stream_server.serve_forever, daemon=True).start()
    control_server = ThreadingHTTPServer(("127.0.0.1", 0), _Esp32Handler)
    control_base = f"http://127.0.0.1:{control_server.server_port}"
    stream_url = f"http://127.0.0.1:{stream_server.server_port}/stream"
    control_server.external_stream_url = stream_url
    control_server.redirect_stream_url = stream_url
    threading.Thread(target=control_server.serve_forever, daemon=True).start()
    source = Esp32CamSource(stream_url, {"capture_url": f"{control_base}/capture", "test_frame_count": 2})
    try:
        redirect = httpx.get(f"{control_base}/stream", follow_redirects=False, timeout=2)
        assert redirect.status_code == 307
        assert redirect.headers["location"] == stream_url
        assert source.base_url == control_base
        assert source.source == stream_url
        assert source.connect(), source.health_check()
        checks = source.test_endpoints()
        assert checks["video_normal"] is True
        assert set(["/health", "/device", "/capture"]).issubset(control_server.seen_paths)
        assert stream_server.seen_paths == ["/stream"]
    finally:
        source.disconnect()
        control_server.shutdown()
        stream_server.shutdown()
        control_server.server_close()
        stream_server.server_close()


class _ThreadBoundSource(CameraSourceAdapter):
    source_type = "thread-test"

    def __init__(self):
        super().__init__(None, {})
        self.thread_ids = []
        self.reads = 0

    def connect(self):
        self.thread_ids.append(threading.get_ident())
        return True

    def read_frame(self):
        self.thread_ids.append(threading.get_ident())
        self.reads += 1
        if self.reads > 2:
            time.sleep(.01)
        return np.zeros((20, 20, 3), dtype=np.uint8)

    def disconnect(self):
        self.thread_ids.append(threading.get_ident())


def test_capture_connect_read_disconnect_are_same_worker_thread():
    source = _ThreadBoundSource()
    worker = CaptureWorker(source)
    assert worker.start()
    packet = worker.frames.get(timeout=1)
    assert packet.frame.shape == (20, 20, 3)
    worker.stop()
    assert len(source.thread_ids) >= 3
    assert len(set(source.thread_ids)) == 1


class _ReconnectSource(CameraSourceAdapter):
    source_type = "reconnect-test"

    def __init__(self):
        super().__init__(None, {})
        self.connect_count = 0
        self.reads_this_connection = 0
        self.released = False

    def connect(self):
        self.connect_count += 1
        self.reads_this_connection = 0
        self.released = False
        return True

    def read_frame(self):
        self.reads_this_connection += 1
        if self.reads_this_connection == 1:
            return np.full((10, 10, 3), self.connect_count, dtype=np.uint8)
        return None

    def disconnect(self):
        self.released = True


def test_capture_reconnect_creates_new_session_epoch_and_releases_source():
    source = _ReconnectSource()
    worker = CaptureWorker(source, queue_size=2, reconnect_after_failures=2)
    assert worker.start()
    first = worker.frames.get(timeout=1)
    deadline = time.monotonic() + 3
    second = None
    while time.monotonic() < deadline:
        try:
            candidate = worker.frames.get(timeout=.2)
        except Exception:
            continue
        if candidate.reconnect_epoch > first.reconnect_epoch:
            second = candidate
            break
    worker.stop()
    assert second is not None
    assert first.source_session_id and second.source_session_id
    assert second.source_session_id != first.source_session_id
    assert first.reconnect_epoch == 0
    assert second.reconnect_epoch == 1
    assert second.sequence == 1
    assert worker.health()["capture_thread_alive"] is False
    assert source.released is True
