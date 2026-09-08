from __future__ import annotations

import ipaddress
import os
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import cv2
import httpx
import numpy as np

from .base import redact_value
from .opencv_stream import OpenCvCaptureSource


def _bounded_http_bytes(url, headers, timeout, limit):
    """Private camera control response: identity encoding, byte/time bounds."""
    budget=max(.1,min(float(timeout),30.0));deadline=time.monotonic()+budget
    with httpx.Client(headers=headers,timeout=httpx.Timeout(min(budget,1.0),connect=budget),
                      trust_env=False,follow_redirects=False) as client:
        with client.stream("GET",url) as response:
            response.raise_for_status()
            if response.headers.get("content-encoding","identity").lower() not in {"","identity"}:
                raise ValueError("摄像头不允许压缩 HTTP 响应")
            content=bytearray()
            for chunk in response.iter_raw():
                if time.monotonic()>=deadline:raise ValueError("摄像头响应总读取时间超限")
                if len(content)+len(chunk)>limit:raise ValueError("摄像头响应超过读取上限")
                content.extend(chunk)
            return bytes(content)


def _decode_bounded_camera_jpeg(jpeg: bytes):
    # Inspect JPEG SOF dimensions before asking native OpenCV to allocate an
    # image. A small compressed response must not expand to arbitrary memory.
    if len(jpeg)>2_000_000 or not jpeg.startswith(b"\xff\xd8"):
        raise ValueError("摄像头 JPEG 超过读取上限或无效")
    cursor=2
    while cursor+4<=len(jpeg):
        if jpeg[cursor]!=0xff:break
        while cursor<len(jpeg) and jpeg[cursor]==0xff:cursor+=1
        if cursor>=len(jpeg):break
        marker=jpeg[cursor];cursor+=1
        if marker in {0xd8,0xd9,0x01} or 0xd0<=marker<=0xd7:continue
        length=int.from_bytes(jpeg[cursor:cursor+2],"big")
        if length<2 or cursor+length>len(jpeg):break
        if marker in {0xc0,0xc1,0xc2,0xc3,0xc5,0xc6,0xc7,0xc9,0xca,0xcb,0xcd,0xce,0xcf}:
            if length<8:break
            height=int.from_bytes(jpeg[cursor+3:cursor+5],"big")
            width=int.from_bytes(jpeg[cursor+5:cursor+7],"big")
            if not (0<width<=4096 and 0<height<=4096 and width*height<=8_000_000):
                raise ValueError("摄像头 JPEG 像素尺寸超过读取上限")
            return cv2.imdecode(np.frombuffer(jpeg,np.uint8),cv2.IMREAD_COLOR)
        cursor+=length
    raise ValueError("摄像头 JPEG 缺少有效尺寸")


class _AuthenticatedMjpegCapture:
    """Small VideoCapture-compatible HTTP reader with private per-client headers.

    Never use process-global FFmpeg options or credential-bearing URLs. No
    redirects/proxies, no unbounded frame queue, and release closes the stream.
    """
    MAX_FRAME_BYTES = 2_000_000

    def __init__(self, url: str, headers: dict[str, str], timeout: float):
        self.frame_timeout = max(.1, min(float(timeout), 30.0))
        self.client = httpx.Client(headers=headers, timeout=httpx.Timeout(min(self.frame_timeout,1.0), connect=self.frame_timeout),
                                   follow_redirects=False, trust_env=False)
        self.response = None
        self.buffer = bytearray()
        self.width = self.height = 0
        try:
            self.response = self.client.send(self.client.build_request("GET", url), stream=True)
            self.response.raise_for_status()
            if "multipart/" not in self.response.headers.get("content-type", "").lower():
                raise ValueError("摄像头没有返回 MJPEG 视频流")
            if self.response.headers.get("content-encoding","identity").lower() not in {"","identity"}:
                raise ValueError("摄像头视频不允许压缩 HTTP 编码")
            self.chunks = self.response.iter_raw()
        except BaseException:
            self.release()
            raise

    def isOpened(self):
        return self.response is not None and not self.response.is_closed

    def set(self, *_args):
        return False

    def get(self, prop):
        if prop == cv2.CAP_PROP_FRAME_WIDTH:return self.width
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:return self.height
        return 0

    def read(self):
        deadline=time.monotonic()+self.frame_timeout
        try:
            while self.isOpened():
                if time.monotonic()>=deadline:
                    raise ValueError("摄像头单帧总读取时间超限")
                start = self.buffer.find(b"\xff\xd8")
                end = self.buffer.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                if end >= 0:
                    jpeg = bytes(self.buffer[start:end + 2])
                    del self.buffer[:end + 2]
                    frame = _decode_bounded_camera_jpeg(jpeg)
                    if frame is None:return False, None
                    self.height,self.width = frame.shape[:2]
                    return True, frame
                chunk = next(self.chunks)
                if time.monotonic()>=deadline:
                    raise ValueError("摄像头单帧总读取时间超限")
                if len(self.buffer) + len(chunk) > self.MAX_FRAME_BYTES:
                    raise ValueError("摄像头视频帧超过读取上限")
                self.buffer.extend(chunk)
        except StopIteration:
            self.release()
            return False, None
        except BaseException:
            self.release()
            raise
        return False, None

    def release(self):
        response,self.response = self.response,None
        if response is not None:response.close()
        self.client.close()
        self.buffer.clear()


class MjpegHttpSource(OpenCvCaptureSource):
    source_type = "mjpeg"

    def _open_capture(self, argument: str) -> cv2.VideoCapture:
        os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")
        params = [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(float(self.config.get("open_timeout_seconds", 5)) * 1000),
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(float(self.config.get("read_timeout_seconds", 5)) * 1000),
        ]
        try:
            return cv2.VideoCapture(argument, cv2.CAP_FFMPEG, params)
        except (TypeError, cv2.error):
            return cv2.VideoCapture(argument, cv2.CAP_FFMPEG)

    def connect(self) -> bool:
        if not str(self.source).lower().startswith(("http://", "https://")):
            self._record_error("HTTP/MJPEG 地址必须以 http:// 或 https:// 开头")
            return False
        return super().connect()


class RtspSource(OpenCvCaptureSource):
    source_type = "rtsp"

    def _open_capture(self, argument: str) -> cv2.VideoCapture:
        params = [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(float(self.config.get("open_timeout_seconds", 5)) * 1000),
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(float(self.config.get("read_timeout_seconds", 5)) * 1000),
        ]
        try:
            return cv2.VideoCapture(argument, cv2.CAP_FFMPEG, params)
        except (TypeError, cv2.error):
            return cv2.VideoCapture(argument, cv2.CAP_FFMPEG)

    def connect(self) -> bool:
        if not str(self.source).lower().startswith(("rtsp://", "rtsps://")):
            self._record_error("RTSP 地址格式不正确")
            return False
        # Prevent native FFmpeg diagnostics from echoing credential-bearing URLs.
        os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")
        try:
            cv2.setLogLevel(0)
        except AttributeError:
            pass
        return super().connect()


class Esp32CamSource(MjpegHttpSource):
    source_type = "esp32"

    def __init__(self, source: Any, config: dict[str, Any] | None = None) -> None:
        options = dict(config or {})
        source_text = str(source).rstrip("/")
        source_parsed = urlsplit(source_text)
        if source_parsed.path.endswith("/stream"):
            stream_url = source_text
        else:
            stream_base = source_text
            for suffix in ("/capture", "/device", "/health"):
                if stream_base.endswith(suffix):
                    stream_base = stream_base[:-len(suffix)]
                    break
            stream_url = f"{stream_base}/stream"
        # The firmware serves control endpoints on port 80 and MJPEG on 81.
        # Preserve the advertised stream URL, and derive the control origin
        # from capture_url supplied by enrollment rather than from port 81.
        control_text = str(options.get("capture_url") or source_text).rstrip("/")
        for suffix in ("/stream", "/capture", "/device", "/health"):
            if control_text.endswith(suffix):
                control_text = control_text[: -len(suffix)]
                break
        self.base_url = control_text
        self.capture_url = str(options.get("capture_url") or f"{self.base_url}/capture")
        self.device_info: dict[str, Any] = {}
        self.remote_health: dict[str, Any] = {}
        self._camera_read_token: str | None = None
        self._require_camera_auth = options.get("board_model") == "xiao_esp32s3_sense"
        super().__init__(stream_url, options)

    def set_camera_read_token(self, token: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", token or "") is None:
            raise ValueError("摄像头只读凭据无效")
        self.disconnect()
        self._camera_read_token = token
        self._require_camera_auth = True

    def _safe_value(self, value):
        if isinstance(value,dict):return redact_value({key:self._safe_value(item) for key,item in value.items()})
        if isinstance(value,list):return [self._safe_value(item) for item in value]
        if isinstance(value,str) and self._camera_read_token:value=value.replace(self._camera_read_token,"***")
        return redact_value(value)

    def _public_remote(self, value):
        allowed={"camera_status","camera_ready","camera_auth","firmware_version","board_type","chip",
                 "sensor_pid","sensor_name","psram_found","psram_size","flash_size","device_id","stream_url","capture_url"}
        return self._safe_value({key:item for key,item in value.items() if key in allowed})

    def _record_error(self, message):
        super()._record_error(self._safe_value(str(message)))

    def _open_capture(self, argument):
        if self._require_camera_auth:
            if not self._camera_read_token:
                raise ValueError("摄像头未配置只读凭据，不能匿名读取")
            return _AuthenticatedMjpegCapture(str(argument), self._headers(), float(self.config.get("read_timeout_seconds", 5)))
        return super()._open_capture(argument)

    @staticmethod
    def _local_host(host: str) -> bool:
        if host in {"localhost", "127.0.0.1", "::1"}:
            return True
        try:
            return ipaddress.ip_address(host).is_private
        except ValueError:
            return host.endswith(".local") or "." not in host

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._camera_read_token}"} if self._camera_read_token else {}

    def _get_json(self, endpoint: str) -> dict[str, Any]:
        timeout = float(self.config.get("http_timeout", 2.5))
        import json
        value = json.loads(_bounded_http_bytes(f"{self.base_url}{endpoint}",self._headers(),timeout,64*1024))
        if not isinstance(value, dict):
            raise ValueError("设备返回的不是 JSON 对象")
        return value

    def connect(self) -> bool:
        try:
            if self._require_camera_auth and not self._camera_read_token:
                raise ValueError("摄像头未配置只读凭据，不能匿名读取")
            base_host = urlsplit(self.base_url).hostname or ""
            if not self._local_host(base_host):
                raise ValueError("ESP32-CAM 仅允许连接本地私有网络地址")
            if urlsplit(str(self.source)).hostname != base_host or urlsplit(self.capture_url).hostname != base_host:
                raise ValueError("设备控制地址与视频地址必须位于同一私有主机")
            if self._require_camera_auth:
                if not ipaddress.ip_address(base_host).is_private:
                    raise ValueError("已鉴权摄像头必须使用固定私有 IP")
                for endpoint in (self.source, self.base_url, self.capture_url):
                    target=urlsplit(str(endpoint))
                    if target.username or target.password or target.query or target.fragment:
                        raise ValueError("已鉴权摄像头地址不得携带凭据、查询参数或片段")
            self.remote_health = self._get_json("/health")
            if self._require_camera_auth and self.remote_health.get("camera_auth") != "hmac-sha256-v1":
                raise ValueError("旧固件尚未保护摄像头视频；请更新固件，不能回退匿名读取")
            self.device_info = self._get_json("/device")
            stream_url = self.device_info.get("stream_url")
            if stream_url:
                if self._require_camera_auth and str(stream_url) != str(self.source):
                    raise ValueError("设备视频地址与绑定的固定地址不一致，拒绝转发只读凭据")
                stream_parsed = urlsplit(str(stream_url))
                base_parsed = urlsplit(self.base_url)
                if stream_parsed.scheme not in {"http", "https"} or stream_parsed.hostname != base_parsed.hostname:
                    raise ValueError("设备返回了不同主机的视频地址，已拒绝连接")
                try:
                    if not ipaddress.ip_address(stream_parsed.hostname or "").is_private:
                        raise ValueError("ESP32 视频地址不在本地私有网络")
                except ValueError as exc:
                    # Localhost is useful for the explicit device simulator.
                    if stream_parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                        raise exc
                self.source = str(stream_url)
        except Exception as exc:
            self._record_error(f"ESP32 状态接口不可用：{redact_value(str(exc))}")
            return False
        if not super().connect():
            return False
        # An opened VideoCapture is not proof of usable video. Require one real
        # decoded image before the adapter may report online/ready.
        try:
            frame = super().read_frame()
        except Exception as exc:
            self.disconnect()
            self._record_error(f"ESP32 视频首帧读取失败：{exc}")
            return False
        if frame is None:
            self.disconnect()
            self._record_error("ESP32 视频流已连接，但没有读取到有效画面")
            return False
        self._verified_frame = frame
        return True

    def read_frame(self) -> np.ndarray | None:
        verified = getattr(self, "_verified_frame", None)
        if verified is not None:
            self._verified_frame = None
            return verified
        return super().read_frame()

    def disconnect(self) -> None:
        self._verified_frame = None
        super().disconnect()

    def test_endpoints(self) -> dict[str, Any]:
        result: dict[str, Any] = {"health": False, "device": False, "capture": False, "stream": False}
        if self._require_camera_auth and not self._camera_read_token:
            return {**result,"video_normal":False,"health_error":"摄像头未配置只读凭据，不能匿名诊断"}
        try:
            result["health_data"] = self._public_remote(self._get_json("/health"))
            if self._require_camera_auth and result["health_data"].get("camera_auth")!="hmac-sha256-v1":
                raise ValueError("旧固件尚未保护摄像头视频；请更新固件，不能回退匿名诊断")
            result["health"] = True
        except Exception as exc:
            result["health_error"] = self._safe_value(str(exc))
            if self._require_camera_auth:return {**result,"video_normal":False}
        try:
            result["device_data"] = self._public_remote(self._get_json("/device"))
            result["device"] = True
        except Exception as exc:
            result["device_error"] = self._safe_value(str(exc))
        try:
            payload = _bounded_http_bytes(self.capture_url,self._headers(),float(self.config.get("http_timeout",2.5)),2_000_000)
            image = _decode_bounded_camera_jpeg(payload)
            result["capture"] = image is not None
            if image is not None:
                result["capture_size"] = [int(image.shape[1]), int(image.shape[0])]
        except Exception as exc:
            result["capture_error"] = self._safe_value(str(exc))
        if self.capture is None and not self.connect():
            result["stream"] = False
        else:
            sample_count = max(1, min(10, int(self.config.get("test_frame_count", 3))))
            frames = 0
            started = time.perf_counter()
            for _index in range(sample_count):
                if self.read_frame() is not None:
                    frames += 1
                else:
                    break
            elapsed = time.perf_counter() - started
            result["stream"] = frames >= 1
            result["stream_frames"] = frames
            result["stream_fps"] = round((frames - 1) / elapsed, 2) if frames > 1 and elapsed > 0 else 0.0
            result["stream_latency_ms"] = round(elapsed * 1000 / frames, 1) if frames else None
        result["video_normal"] = bool(result["health"] and result["device"] and result["capture"] and result["stream"])
        return result

    def health_check(self) -> dict[str, Any]:
        result = super().health_check()
        result["device"] = self._public_remote(self.device_info)
        result["remote"] = self._public_remote(self.remote_health)
        result=self._safe_value(result)
        result["camera_auth"] = "hmac-sha256-v1" if self._require_camera_auth else "legacy_or_simulated_unauthenticated"
        return result


class OnvifSource(RtspSource):
    """ONVIF adapter that only connects to an explicitly supplied private host.

    Discovery is intentionally opt-in through :meth:`discover`. It never probes
    public addresses and never tries alternate credentials.
    """

    source_type = "onvif"

    @staticmethod
    def _private_host(host: str) -> bool:
        try:
            return ipaddress.ip_address(host).is_private
        except ValueError:
            # Hostnames are allowed only when caller resolved/discovered them.
            return host.endswith(".local") or "." not in host

    @classmethod
    def discover(cls, timeout_seconds: float = 3.0) -> list[dict[str, Any]]:
        try:
            from wsdiscovery.discovery import ThreadedWSDiscovery  # type: ignore
            from wsdiscovery import QName  # type: ignore
        except ImportError:
            return []
        discovery = ThreadedWSDiscovery()
        discovery.start()
        try:
            services = discovery.searchServices(types=[QName("http://www.onvif.org/ver10/network/wsdl", "NetworkVideoTransmitter")], timeout=timeout_seconds)
            found: list[dict[str, Any]] = []
            for service in services:
                for address in service.getXAddrs() or []:
                    host = urlsplit(address).hostname or ""
                    if cls._private_host(host):
                        found.append({"address": address, "host": host})
            return found
        finally:
            discovery.stop()

    def connect(self) -> bool:
        parsed = urlsplit(str(self.source))
        if parsed.scheme.startswith("rtsp"):
            if not self._private_host(parsed.hostname or ""):
                self._record_error("ONVIF/RTSP 仅允许连接本地私有网络设备")
                return False
            return super().connect()
        self._record_error("请先主动发现设备并提供 ONVIF 返回的 RTSP 地址")
        return False
