"""HTTP camera used to verify the ESP32 lifecycle without claiming real hardware.

The virtual camera deliberately uses the same one-time enrollment, device token,
heartbeat, JPEG and MJPEG contracts as the firmware.  Its local binding file is
the virtual equivalent of ESP32 NVS and is kept below the ignored data directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import cv2
import httpx


ROOT = Path(__file__).resolve().parents[2]
_VIDEO_DIRECTORY = ROOT / "demo" / "sample-videos"
DEFAULT_VIDEO = (
    _VIDEO_DIRECTORY / "object-memory-demo.mp4"
    if (_VIDEO_DIRECTORY / "object-memory-demo.mp4").is_file()
    else _VIDEO_DIRECTORY / "object-memory-demo.avi"
)
DEFAULT_DATA = ROOT / "data"
HEARTBEAT_SECONDS = 15


def _virtual_mac(device_id: str) -> str:
    """Return a stable, locally administered unicast MAC for one virtual ID."""
    digest = hashlib.sha256(device_id.encode("utf-8")).digest()
    values = bytes([0x02, *digest[:5]])
    return ":".join(f"{part:02X}" for part in values)


class OpenCVFrameProvider:
    """Read either the generated demo video or an explicitly selected webcam."""

    def __init__(self, source: Literal["video", "webcam"], *, video_path: Path | None = None,
                 camera_index: int = 0, width: int = 640, height: int = 480):
        self.source = source
        self.video_path = Path(video_path or DEFAULT_VIDEO).resolve()
        self.camera_index = int(camera_index)
        self.width = int(width)
        self.height = int(height)
        self.capture: cv2.VideoCapture | None = None
        self.fps = 10.0 if source == "video" else 8.0

    def open(self) -> None:
        if self.capture is not None:
            return
        if self.source == "video":
            if not self.video_path.is_file():
                raise RuntimeError(f"测试视频不存在：{self.video_path}")
            capture = cv2.VideoCapture(str(self.video_path))
        else:
            # DSHOW avoids several MSMF startup failures on Windows.  CAP_ANY is
            # retained as a bounded fallback for machines where DSHOW is absent.
            backends = [cv2.CAP_DSHOW, cv2.CAP_ANY] if os.name == "nt" else [cv2.CAP_ANY]
            capture = None
            for backend in backends:
                candidate = cv2.VideoCapture(self.camera_index, backend)
                if candidate.isOpened():
                    capture = candidate
                    break
                candidate.release()
            if capture is None:
                raise RuntimeError(f"电脑摄像头索引 {self.camera_index} 无法打开")
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        if not capture.isOpened():
            capture.release()
            raise RuntimeError("视频来源可以创建，但 OpenCV 没有成功打开")
        measured = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        if 1 <= measured <= 60:
            self.fps = measured
        self.capture = capture

    def read(self):
        self.open()
        assert self.capture is not None
        ok, frame = self.capture.read()
        if not ok and self.source == "video":
            self.capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.capture.read()
        if not ok or frame is None or not frame.size:
            raise RuntimeError("视频来源已打开，但没有读到图像帧")
        if frame.shape[1] != self.width or frame.shape[0] != self.height:
            frame = cv2.resize(frame, (self.width, self.height), interpolation=cv2.INTER_AREA)
        # The banner is kept away from the moving marker.  Every exported frame
        # remains visibly attributable to the non-physical virtual device.
        cv2.rectangle(frame, (0, 0), (self.width, 25), (245, 248, 247), -1)
        cv2.putText(frame, "VIRTUAL ESP32-CAM - NOT PHYSICAL HARDWARE", (8, 17),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (28, 75, 65), 1, cv2.LINE_AA)
        return frame

    def close(self) -> None:
        capture, self.capture = self.capture, None
        if capture is not None:
            capture.release()


class LatestFramePump:
    """Decode once and share the newest JPEG with all HTTP clients."""

    def __init__(self, provider: OpenCVFrameProvider, requested_fps: float = 5.0):
        self.provider = provider
        self.target_fps = max(1.0, min(float(requested_fps), 15.0, provider.fps))
        self.condition = threading.Condition()
        self.latest: bytes | None = None
        self.sequence = 0
        self.error: str | None = None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="virtual-esp32cam-frame-pump")

    def start(self) -> None:
        self.provider.open()
        self.target_fps = max(1.0, min(self.target_fps, self.provider.fps))
        self.thread.start()
        _, frame = self.wait_after(0, timeout=4)
        if frame is None:
            raise RuntimeError(self.error or "虚拟设备没有生成首帧")

    def _run(self) -> None:
        interval = 1.0 / self.target_fps
        while not self.stop_event.is_set():
            started = time.perf_counter()
            try:
                frame = self.provider.read()
                ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 84])
                if not ok:
                    raise RuntimeError("OpenCV 无法编码 JPEG")
                with self.condition:
                    self.latest = encoded.tobytes()
                    self.sequence += 1
                    self.error = None
                    self.condition.notify_all()
            except Exception as exc:
                with self.condition:
                    self.error = str(exc)
                    self.condition.notify_all()
                if self.stop_event.wait(0.5):
                    break
                continue
            self.stop_event.wait(max(0.0, interval - (time.perf_counter() - started)))

    def wait_after(self, sequence: int, timeout: float = 2.0) -> tuple[int, bytes | None]:
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.sequence <= sequence and not self.stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(remaining)
            return self.sequence, self.latest

    def snapshot(self) -> bytes:
        _, frame = self.wait_after(-1, timeout=2)
        if frame is None:
            raise RuntimeError(self.error or "当前没有图像帧")
        return frame

    def close(self) -> None:
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        if self.thread.is_alive():
            self.thread.join(timeout=3)
        self.provider.close()


@dataclass
class SimulatorState:
    device_id: str
    device_name: str
    room_name: str
    source: str = "video"
    started_at: float = field(default_factory=time.monotonic)
    frames: int = 0
    base_url: str = ""
    stream_url: str = ""
    mac_address: str = ""
    pump: LatestFramePump | None = None

    def jpeg(self) -> bytes:
        if not self.pump:
            raise RuntimeError("frame pump is not running")
        return self.pump.snapshot()


def make_handler(state: SimulatorState, *, stream_only: bool = False):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ObjectMemoryVirtualESP32/0.2"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, data: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if stream_only and self.path != "/stream":
                self._json({"detail": "stream server only"}, 404)
                return
            if self.path == "/health":
                self._json({
                    "online": True,
                    "uptime": int(time.monotonic() - state.started_at),
                    "free_heap": 120000,
                    "wifi_rssi": -45,
                    "camera_status": "ready",
                    "firmware_version": "virtual-esp32cam-0.2",
                    "simulated": True,
                    "hardware_type": "virtual",
                    "video_source": state.source,
                })
            elif self.path == "/device":
                self._json({
                    "device_id": state.device_id,
                    "device_name": state.device_name,
                    "room_name": state.room_name,
                    "ip": urlsplit(state.base_url).hostname,
                    "mac": state.mac_address,
                    "firmware_version": "virtual-esp32cam-0.2",
                    "stream_url": state.stream_url,
                    "capture_url": state.base_url + "/capture",
                    "simulated": True,
                    "hardware_type": "virtual",
                    "video_source": state.source,
                })
            elif self.path == "/capture":
                try:
                    frame = state.jpeg()
                except RuntimeError as exc:
                    self._json({"detail": str(exc)}, 503)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(frame)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(frame)
            elif self.path == "/stream":
                if not stream_only and state.stream_url != state.base_url + "/stream":
                    self.send_response(307)
                    self.send_header("Location", state.stream_url)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                sequence = -1
                try:
                    while True:
                        assert state.pump is not None
                        sequence, frame = state.pump.wait_after(sequence, timeout=3)
                        if frame is None:
                            continue
                        state.frames = max(state.frames, sequence)
                        self.wfile.write(
                            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                            + str(len(frame)).encode("ascii") + b"\r\n\r\n" + frame + b"\r\n"
                        )
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
            else:
                self._json({"detail": "not found"}, 404)

    return Handler


class DeviceSimulator:
    def __init__(self, device_id: str, device_name: str, room_name: str,
                 host: str = "127.0.0.1", port: int = 0, *, split_stream: bool = False,
                 source: Literal["video", "webcam"] = "video", video_path: Path | None = None,
                 camera_index: int = 0, fps: float = 5.0):
        provider = OpenCVFrameProvider(source, video_path=video_path, camera_index=camera_index)
        self.state = SimulatorState(
            device_id=device_id,
            device_name=device_name,
            room_name=room_name,
            source=source,
            mac_address=_virtual_mac(device_id),
            pump=LatestFramePump(provider, requested_fps=fps),
        )
        self.server = ThreadingHTTPServer((host, port), make_handler(self.state))
        actual_host, actual_port = self.server.server_address[:2]
        advertised_host = actual_host if actual_host not in {"0.0.0.0", "::"} else "127.0.0.1"
        self.state.base_url = f"http://{advertised_host}:{actual_port}"
        self.stream_server: ThreadingHTTPServer | None = None
        self.stream_thread: threading.Thread | None = None
        if split_stream:
            self.stream_server = ThreadingHTTPServer((host, 0), make_handler(self.state, stream_only=True))
            stream_host, stream_port = self.stream_server.server_address[:2]
            advertised_stream_host = stream_host if stream_host not in {"0.0.0.0", "::"} else "127.0.0.1"
            self.state.stream_url = f"http://{advertised_stream_host}:{stream_port}/stream"
            self.stream_thread = threading.Thread(
                target=self.stream_server.serve_forever, daemon=True,
                name="virtual-esp32cam-stream-server",
            )
        else:
            self.state.stream_url = self.state.base_url + "/stream"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True,
                                       name="virtual-esp32cam-control-server")

    def start(self) -> None:
        assert self.state.pump is not None
        try:
            self.state.pump.start()
            self.thread.start()
            if self.stream_thread:
                self.stream_thread.start()
        except Exception:
            self.state.pump.close()
            self.server.server_close()
            if self.stream_server:
                self.stream_server.server_close()
            raise

    def close(self) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()
        if self.thread.is_alive():
            self.thread.join(timeout=2)
        if self.stream_server and self.stream_thread:
            if self.stream_thread.is_alive():
                self.stream_server.shutdown()
            self.stream_server.server_close()
            if self.stream_thread.is_alive():
                self.stream_thread.join(timeout=2)
        if self.state.pump:
            self.state.pump.close()


def claim(backend_url: str, pairing_code: str, simulator: DeviceSimulator) -> tuple[str, str]:
    base = simulator.state.base_url
    payload = {
        "device_id": simulator.state.device_id,
        "pairing_code": pairing_code,
        "mac_address": simulator.state.mac_address,
        "firmware_version": "virtual-esp32cam-0.2",
        "ip_address": urlsplit(base).hostname,
        "stream_url": simulator.state.stream_url,
        "capture_url": base + "/capture",
        "capabilities": ["camera", "mjpeg", "capture", "simulator"],
    }
    response = httpx.post(
        backend_url.rstrip("/") + "/api/device-enrollment/claim", json=payload, timeout=15
    )
    response.raise_for_status()
    data = response.json()
    return data["device_token"], data["camera_id"]


def heartbeat(backend_url: str, token: str, simulator: DeviceSimulator) -> list[dict[str, Any]]:
    payload = {
        "uptime": int(time.monotonic() - simulator.state.started_at),
        "wifi_rssi": -45,
        "free_heap": 120000,
        "camera_status": "ready",
        "stream_status": "ready",
        "firmware_version": "virtual-esp32cam-0.2",
        "ip_address": urlsplit(simulator.state.base_url).hostname,
    }
    response = httpx.post(
        backend_url.rstrip("/") + f"/api/devices/{simulator.state.device_id}/heartbeat",
        headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=15,
    )
    response.raise_for_status()
    return response.json().get("commands", [])


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def _admin_client(backend_url: str) -> httpx.Client:
    client = httpx.Client(base_url=backend_url.rstrip("/"), timeout=15)
    response = client.get("/api/session")
    response.raise_for_status()
    if not response.json().get("authenticated"):
        client.close()
        raise RuntimeError("虚拟设备自动注册只能在运行后端的本机执行")
    return client


def _auto_enrollment(client: httpx.Client, device_name: str, room_name: str) -> str:
    response = client.post(
        "/api/device-enrollment/create",
        json={"device_name": device_name, "room_name": room_name},
    )
    response.raise_for_status()
    return response.json()["pairing_code"]


def _ensure_demo_zones(client: httpx.Client, camera_id: str) -> None:
    response = client.get(f"/api/cameras/{camera_id}/zones")
    response.raise_for_status()
    existing = {zone.get("name") for zone in response.json()}
    zones = [
        ("桌面", [[0, .12], [.46, .12], [.46, .9], [0, .9]]),
        ("沙发右侧", [[.5, .12], [1, .12], [1, .9], [.5, .9]]),
        ("地面", [[0, .91], [1, .91], [1, 1], [0, 1]]),
    ]
    for name, points in zones:
        if name not in existing:
            created = client.post(
                f"/api/cameras/{camera_id}/zones",
                json={"name": name, "points": points, "priority": 1, "enabled": True},
            )
            created.raise_for_status()


def run_virtual_device(*, backend_url: str, device_name: str, room_name: str,
                       host: str, port: int, split_stream: bool, source: Literal["video", "webcam"],
                       video_path: Path | None, camera_index: int, fps: float,
                       pairing_code: str | None, auto_enroll: bool, binding_path: Path,
                       status_path: Path, once: bool, stop_event: threading.Event,
                       requested_device_id: str | None = None) -> int:
    binding = _read_json(binding_path)
    device_id = str(requested_device_id or binding.get("device_id") or f"omcam-virtual-{uuid4().hex[:8]}")
    simulator = DeviceSimulator(
        device_id, device_name, room_name, host, port, split_stream=split_stream,
        source=source, video_path=video_path, camera_index=camera_index, fps=fps,
    )
    simulator.start()
    admin: httpx.Client | None = None
    same_endpoint = (
        binding.get("device_id") == device_id
        and
        binding.get("backend_url") == backend_url
        and binding.get("base_url") == simulator.state.base_url
        and binding.get("stream_url") == simulator.state.stream_url
    )
    token = str(binding.get("device_token") or "") if same_endpoint else ""
    camera_id = str(binding.get("camera_id") or "") if token else ""

    def status(**extra: Any) -> None:
        _atomic_json(status_path, {
            "running": not stop_event.is_set(),
            "label": "虚拟设备 · 非真实硬件",
            "hardware_type": "virtual",
            "device_id": device_id,
            "camera_id": camera_id or None,
            "source": source,
            "base_url": simulator.state.base_url,
            "stream_url": simulator.state.stream_url,
            "pid": os.getpid(),
            "last_heartbeat": None,
            "error": None,
            **extra,
        })

    status(stage="starting")
    try:
        # A saved device token is the virtual equivalent of the real device's
        # NVS credential.  It is tested before any new one-time code is issued.
        if token:
            try:
                heartbeat(backend_url, token, simulator)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 401:
                    raise
                token = ""
                camera_id = ""
        if not token:
            if not pairing_code:
                if not auto_enroll:
                    raise RuntimeError("缺少一次性配对码；也没有启用本机自动配对")
                admin = _admin_client(backend_url)
                pairing_code = _auto_enrollment(admin, device_name, room_name)
            token, camera_id = claim(backend_url, pairing_code, simulator)
            _atomic_json(binding_path, {
                "device_id": device_id,
                "device_token": token,
                "camera_id": camera_id,
                "backend_url": backend_url,
                "mac_address": simulator.state.mac_address,
                "base_url": simulator.state.base_url,
                "stream_url": simulator.state.stream_url,
            })
        if admin is None:
            admin = _admin_client(backend_url)
        _ensure_demo_zones(admin, camera_id)
        status(stage="online", last_heartbeat=time.time())
        while not stop_event.is_set():
            commands = heartbeat(backend_url, token, simulator)
            status(stage="online", last_heartbeat=time.time())
            for command in commands:
                if command.get("command") == "factory_reset":
                    binding_path.unlink(missing_ok=True)
                    stop_event.set()
                elif command.get("command") == "reboot":
                    status(stage="restarting", last_heartbeat=time.time())
            if once:
                stop_event.wait(6)
                break
            stop_event.wait(HEARTBEAT_SECONDS)
        status(running=False, stage="stopped", last_heartbeat=time.time())
        return 0
    except Exception as exc:
        status(running=False, stage="error", error=str(exc))
        raise
    finally:
        if admin:
            admin.close()
        simulator.close()
