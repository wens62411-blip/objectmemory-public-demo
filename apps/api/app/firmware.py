"""ESP32-CAM build, enrollment, provisioning, and device lifecycle API.

Only the public claim and heartbeat routes accept device traffic. All build,
flash, enrollment creation, and device-management routes remain behind the
application's administrator session middleware.
"""
from __future__ import annotations

import contextlib
import atexit
import functools

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, WebSocket
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator


router = APIRouter()

PAIRING_TTL_SECONDS = 600
HEARTBEAT_INTERVAL_SECONDS = 15
OFFLINE_AFTER_SECONDS = 45
MAX_JOB_LOG_LINES = 500
MAX_SERIAL_LINE = 2048
FIRMWARE_SHUTDOWN_TIMEOUT_SECONDS = 30.0
FIRMWARE_HASH_BUFFER_BYTES = 64 * 1024
FIRMWARE_MANIFEST_MAX_BYTES = 256 * 1024
FIRMWARE_MANIFEST_MAX_ARTIFACTS = 16
FIRMWARE_ARTIFACT_MAX_BYTES = 64 * 1024 * 1024
PAIRING_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
BLUETOOTH_MARKERS = ("bluetooth", "bthenum", "standard serial over bluetooth", "蓝牙")
BoardModel = Literal["ai_thinker_esp32cam", "xiao_esp32s3_sense"]
DEFAULT_BOARD_MODEL = "ai_thinker_esp32cam"
FIRMWARE_BOARDS = {
    "ai_thinker_esp32cam": {"chip": "ESP32", "environment": "esp32cam",
        "manifest": "artifacts/firmware/manifest.json", "build": "firmware/esp32cam/build"},
    "xiao_esp32s3_sense": {"chip": "ESP32-S3", "environment": "seeed_xiao_esp32s3",
        "manifest": "artifacts/firmware/xiao-esp32s3-sense/manifest.json",
        "build": "firmware/esp32cam/build-xiao-esp32s3-sense"},
}


def firmware_board(board_model: str) -> dict[str, str]:
    if board_model not in FIRMWARE_BOARDS:
        raise HTTPException(422, "只支持已配置的 AI Thinker ESP32-CAM 或 Seeed XIAO ESP32S3 Sense。")
    return FIRMWARE_BOARDS[board_model]


class FirmwareShutdownTimeout(RuntimeError):
    """Raised when a non-cancellable firmware operation outlives bounded shutdown."""


def firmware_operation(method):
    """Count one synchronous firmware mutation across its complete transaction."""
    @functools.wraps(method)
    def guarded(self, *args, **kwargs):
        with self._operation_guard():
            return method(self, *args, **kwargs)
    return guarded


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def redact_firmware_log(value: Any) -> str:
    text = str(value)
    text = re.sub(r"(?i)(\bBearer\s+)[^\s,\"'<>}]+", r"\1***", text)
    text = re.sub(r"(?i)(password|passwd|device_token|token)([\s\"'=:\\]+)([^\s,}\"'&]+)", r"\1\2***", text)
    text = re.sub(r"(?i)(pairing_code)([\s\"'=:\\]+)([^\s,}\"'&]+)", r"\1\2***", text)
    text = re.sub(r"OM-[A-Z0-9]{4,}", "OM-******", text)
    text = re.sub(r"(://)[^/@\s]+@", r"\1***@", text)
    return text[:2000]


def hash_device_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def camera_read_token(token_hash: str) -> str:
    """Purpose-separated read-only camera credential; never persist or expose it."""
    if not isinstance(token_hash, str) or re.fullmatch(r"[0-9a-f]{64}", token_hash) is None:
        raise ValueError("摄像头凭据未配置或已撤销")
    return hmac.new(bytes.fromhex(token_hash), b"objectmemory/camera-read/v1", hashlib.sha256).hexdigest()


def validate_local_device_url(value: str, *, expected_ip: str | None = None) -> str:
    """Accept explicit HTTP URLs on private/link-local/loopback IPs only."""
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        if parsed.fragment or len(value) > 300:
            raise ValueError
        if parsed.hostname == "localhost":
            address = ipaddress.ip_address("127.0.0.1")
        else:
            address = ipaddress.ip_address(parsed.hostname)
        if not (address.is_private or address.is_loopback or address.is_link_local):
            raise ValueError
        if address.is_unspecified or address.is_multicast:
            raise ValueError
        if expected_ip and address != ipaddress.ip_address(expected_ip):
            raise ValueError
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "", parsed.query, "")).rstrip("/")
    except (ValueError, TypeError):
        raise ValueError("只允许用户设备明确提供的局域网 HTTP 地址") from None


def validate_backend_for_physical_device(value: str) -> str:
    normalized = validate_local_device_url(value)
    host = urlsplit(normalized).hostname
    address = ipaddress.ip_address("127.0.0.1" if host == "localhost" else host)
    if address.is_loopback:
        raise ValueError("ESP32 无法使用 127.0.0.1；请选择物忆电脑的局域网 IP 地址")
    return normalized


def _replace_url_ip(value: str, ip_address: str) -> str:
    parsed = urlsplit(value)
    port = f":{parsed.port}" if parsed.port else ""
    host = f"[{ip_address}]" if ":" in ip_address else ip_address
    return urlunsplit((parsed.scheme, host + port, parsed.path, parsed.query, ""))


def _replace_url_path(value: str, path: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def parse_serial_protocol_line(raw: bytes | str) -> tuple[str, dict[str, Any]] | None:
    if isinstance(raw, bytes):
        try:
            line = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("串口消息不是 UTF-8") from exc
    else:
        line = raw
    line = line.strip("\r\n")
    if not line.startswith(("OMACK:", "OMREADY:", "OMLOG:")):
        return None
    if len(line.encode("utf-8")) > MAX_SERIAL_LINE:
        raise ValueError("串口消息超过长度限制")
    prefix, payload = line.split(":", 1)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("串口 JSON 格式错误") from exc
    if not isinstance(data, dict):
        raise ValueError("串口消息必须是 JSON 对象")
    kind = prefix[2:].lower()
    if kind == "ack":
        if not isinstance(data.get("success"), bool):
            raise ValueError("OMACK 缺少 success")
        if data["success"] and not isinstance(data.get("device_id"), str):
            raise ValueError("OMACK 缺少 device_id")
        if not data["success"] and not isinstance(data.get("error"), str):
            raise ValueError("OMACK 缺少 error")
    elif kind == "ready":
        required = ("device_id", "camera_id", "ip", "stream_url", "capture_url")
        if any(not isinstance(data.get(key), str) or not data[key] for key in required):
            raise ValueError("OMREADY 字段不完整")
        ipaddress.ip_address(data["ip"])
        validate_local_device_url(data["stream_url"], expected_ip=data["ip"])
        validate_local_device_url(data["capture_url"], expected_ip=data["ip"])
    elif kind == "log":
        if not isinstance(data.get("event"), str):
            raise ValueError("OMLOG 缺少 event")
    return kind, data


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnrollmentCreate(StrictModel):
    device_name: str = Field(min_length=1, max_length=64)
    room_name: str = Field(min_length=1, max_length=64)


class DeviceClaim(StrictModel):
    device_id: str = Field(pattern=r"^[A-Za-z0-9_-]{4,64}$")
    pairing_code: str = Field(pattern=r"^OM-[A-Z0-9]{4,16}$")
    mac_address: str = Field(pattern=r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
    firmware_version: str = Field(min_length=1, max_length=32)
    ip_address: str
    stream_url: str
    capture_url: str
    capabilities: list[str] = Field(min_length=1, max_length=20)

    @field_validator("ip_address")
    @classmethod
    def private_address(cls, value: str) -> str:
        address = ipaddress.ip_address(value)
        if not (address.is_private or address.is_loopback or address.is_link_local):
            raise ValueError("设备必须使用局域网地址")
        return str(address)

    @field_validator("capabilities")
    @classmethod
    def safe_capabilities(cls, value: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[a-z0-9_-]{1,32}", item) for item in value):
            raise ValueError("capabilities 格式错误")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def matching_urls(self):
        self.stream_url = validate_local_device_url(self.stream_url, expected_ip=self.ip_address)
        self.capture_url = validate_local_device_url(self.capture_url, expected_ip=self.ip_address)
        return self


class HeartbeatInput(StrictModel):
    uptime: int = Field(ge=0)
    wifi_rssi: int = Field(ge=-127, le=0)
    free_heap: int = Field(ge=0)
    camera_status: Literal["ready", "error", "initializing"]
    stream_status: Literal["ready", "error", "initializing"]
    firmware_version: str = Field(min_length=1, max_length=32)
    ip_address: str

    @field_validator("ip_address")
    @classmethod
    def private_address(cls, value: str) -> str:
        address = ipaddress.ip_address(value)
        if not (address.is_private or address.is_loopback or address.is_link_local):
            raise ValueError("设备必须使用局域网地址")
        return str(address)


class BuildRequest(StrictModel):
    board_model: BoardModel = DEFAULT_BOARD_MODEL
    clean: bool = False


class FlashRequest(StrictModel):
    port: str = Field(min_length=3, max_length=100)


class ProvisionRequest(StrictModel):
    port: str = Field(min_length=3, max_length=100)
    ssid: str = Field(min_length=1, max_length=32)
    password: str = Field(default="", max_length=63)
    backend_url: str
    pairing_code: str = Field(pattern=r"^OM-[A-Z0-9]{4,16}$")
    device_id: str = Field(pattern=r"^[A-Za-z0-9_-]{4,64}$")
    device_name: str = Field(min_length=1, max_length=64)
    room_name: str = Field(min_length=1, max_length=64)
    timeout_seconds: int = Field(default=90, ge=20, le=180)

    @field_validator("password")
    @classmethod
    def valid_password(cls, value: str) -> str:
        if value and len(value) < 8:
            raise ValueError("Wi-Fi 密码应为空或至少 8 位")
        return value

    @field_validator("backend_url")
    @classmethod
    def local_backend(cls, value: str) -> str:
        return validate_backend_for_physical_device(value)


class InstallRequest(StrictModel):
    port: str = Field(min_length=3, max_length=100)
    ssid: str = Field(min_length=1, max_length=32)
    password: str = Field(default="", max_length=63)
    backend_url: str
    device_name: str = Field(min_length=1, max_length=64)
    room_name: str = Field(min_length=1, max_length=64)

    @field_validator("password")
    @classmethod
    def valid_password(cls, value: str) -> str:
        if value and len(value) < 8:
            raise ValueError("Wi-Fi 密码应为空或至少 8 位")
        return value

    @field_validator("backend_url")
    @classmethod
    def local_backend(cls, value: str) -> str:
        return validate_backend_for_physical_device(value)


class CommandRequest(StrictModel):
    command: Literal["reboot", "factory_reset"]


class AutoUsbBindingRequest(InstallRequest):
    board_model: BoardModel
    authorize_fixed_firmware: Literal[True]

    @field_validator("authorize_fixed_firmware", mode="before")
    @classmethod
    def explicit_boolean_authorization(cls, value):
        if value is not True:
            raise ValueError("必须明确确认只为所选板型安装本次固定固件。")
        return value


class AutoUsbRecoveryRequest(StrictModel):
    action: Literal["reprovision", "retry_install"]
    port: str = Field(min_length=3, max_length=100)
    board_model: BoardModel
    mac_address: str = Field(pattern=r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_authorized_at: str = Field(min_length=1, max_length=80)
    ssid: str = Field(min_length=1, max_length=32)
    password: str = Field(default="", max_length=63, repr=False)
    backend_url: str | None = None
    authorize_recovery: Literal[True]
    authorize_overwrite: StrictBool = False

    @field_validator("authorize_recovery", mode="before")
    @classmethod
    def explicit_recovery(cls, value):
        if value is not True:
            raise ValueError("必须明确授权恢复当前绑定板卡。")
        return value

    @field_validator("password")
    @classmethod
    def valid_password(cls, value):
        if value and len(value) < 8:
            raise ValueError("Wi-Fi 密码应为空或至少 8 位")
        return value

    @field_validator("backend_url")
    @classmethod
    def local_backend(cls, value):
        return validate_backend_for_physical_device(value) if value is not None else None

    @model_validator(mode="after")
    def exact_action_authorization(self):
        if self.authorize_overwrite != (self.action == "retry_install"):
            raise ValueError("写入不确定时必须明确授权覆盖；仅重新配网不接受覆盖授权。")
        return self


class VirtualDeviceStart(StrictModel):
    source: Literal["video", "webcam"] = "video"
    camera_index: int = Field(default=0, ge=0, le=16)
    device_name: str = Field(default="虚拟 ESP32-CAM", min_length=1, max_length=64)
    room_name: str = Field(default="客厅", min_length=1, max_length=64)


def port_record(port: Any) -> dict[str, Any]:
    haystack = " ".join(
        str(value or "") for value in (
            getattr(port, "device", ""), getattr(port, "description", ""),
            getattr(port, "hwid", ""), getattr(port, "manufacturer", ""),
            getattr(port, "product", ""),
        )
    ).lower()
    bluetooth = any(marker in haystack for marker in BLUETOOTH_MARKERS)
    has_usb_id = getattr(port, "vid", None) is not None and getattr(port, "pid", None) is not None
    eligible = has_usb_id and not bluetooth
    reason = None
    if bluetooth:
        reason = "蓝牙虚拟串口不能用于刷写"
    elif not has_usb_id:
        reason = "无法确认这是 USB 串口设备"
    return {
        "device": getattr(port, "device", ""),
        "description": getattr(port, "description", "") or "",
        "hwid": getattr(port, "hwid", "") or "",
        "vid": getattr(port, "vid", None),
        "pid": getattr(port, "pid", None),
        "serial_number": getattr(port, "serial_number", None),
        "location": getattr(port, "location", None),
        "manufacturer": getattr(port, "manufacturer", None),
        "eligible": eligible,
        "rejection_reason": reason,
    }


class FirmwareService:
    def __init__(self, db_path: Path, camera_callback: Callable[[dict[str, Any]], Any] | None,
                 data_root: Path, camera_stop_callback: Callable[[str], Any] | None = None,
                 runtime_mode: str = "REAL", listener_info_provider: Callable[[], dict[str, Any]] | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_root = Path(data_root)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.camera_callback = camera_callback
        self.camera_stop_callback = camera_stop_callback
        self.runtime_mode = str(runtime_mode).upper()
        # Only the owning launcher can supply live socket evidence. Environment
        # flags, an advertised URL, and an arbitrary remote health 200 are not it.
        self.listener_info_provider = listener_info_provider
        self.project_root = Path(__file__).resolve().parents[3]
        self.builder = self.project_root / "scripts" / "firmware-build.py"
        relative_python = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
        self.firmware_python = self.project_root / ".venv-firmware" / relative_python
        self.build_lock = threading.Lock()
        # Multiple dashboards must not multiply full-bundle hashing buffers.
        # Hashes are intentionally not cached: a same-size tamper must be seen.
        self.manifest_lock = threading.Lock()
        self.probe_lock = threading.Lock()
        self.active_probes: set[str] = set()
        self.claim_rate_lock = threading.Lock()
        self.claim_failures: dict[str, list[float]] = {}
        self._worker_condition = threading.Condition(threading.RLock())
        self._workers: set[threading.Thread] = set()
        self._active_operations = 0
        self._drained_callbacks: list[Callable[[], Any]] = []
        self._shutdown_started = False
        self._mode_clear_active = False
        self.shutdown_timeout_seconds = FIRMWARE_SHUTDOWN_TIMEOUT_SECONDS
        self.virtual_lock = threading.Lock()
        self.virtual_process: subprocess.Popen | None = None
        self.virtual_log_handle = None
        self.virtual_directory = self.data_root / "temporary" / "virtual-devices"
        self.virtual_directory.mkdir(parents=True, exist_ok=True)
        self.virtual_status_path = self.virtual_directory / "status.json"
        self.virtual_binding_path = self.virtual_directory / "default-binding.json"
        self._initialize_database()
        self.secret = self._load_server_secret()
        atexit.register(self.stop_virtual_device)
        from .firmware_usb import AutoUsbService
        self.auto_usb = AutoUsbService(self)

    def listener_status(self) -> dict[str, Any]:
        closed = {"lan_enabled": False, "listener_verified": False,
                  "listener_bindings": [], "lan_urls": [], "lan_url_status": "unverified",
                  "lan_message": "尚未验证局域网监听。请停止当前服务后使用 scripts/start.ps1 -Lan 重启；纯编译和纯刷写不受影响。"}
        try:
            raw = self.listener_info_provider() if self.listener_info_provider else {}
            if raw.get("listener_verified") is not True or raw.get("process_id") != os.getpid():
                return closed
            bindings = []
            for binding in raw.get("listener_bindings", []):
                address = ipaddress.ip_address(binding["host"])
                port = int(binding["port"])
                if 1 <= port <= 65535:
                    bindings.append({"host": str(address), "port": port})
            if not bindings:
                return closed
            enabled = any(not ipaddress.ip_address(binding["host"]).is_loopback for binding in bindings)
            urls = set()
            if enabled:
                for candidate in raw.get("local_addresses", []):
                    try:address = ipaddress.ip_address(candidate)
                    except ValueError:continue
                    if address.is_loopback or address.is_unspecified or address.is_multicast:
                        continue
                    if not (address.is_private or address.is_link_local):
                        continue
                    for binding in bindings:
                        listener = ipaddress.ip_address(binding["host"])
                        if listener.version == address.version and (listener.is_unspecified or listener == address):
                            host = f"[{address}]" if address.version == 6 else str(address)
                            urls.add(f"http://{host}:{binding['port']}")
            return {"lan_enabled": enabled, "listener_verified": True,
                    "listener_bindings": bindings, "lan_urls": sorted(urls),
                    "lan_url_status": "candidate_device_reachability_unverified" if enabled else "localhost_only",
                    "lan_message": "当前服务已监听局域网；下列地址仅为候选，尚未验证 ESP32、路由器或防火墙端到端可达。" if enabled else
                    "当前后台仅监听本机，ESP32 无法访问。请停止当前服务后使用 scripts/start.ps1 -Lan 重启；纯编译和纯刷写不受影响。"}
        except (AttributeError, KeyError, TypeError, ValueError, OSError):
            return closed

    def _verify_backend_listener(self, backend_url: str) -> str:
        """Fail before flash/provision unless this process owns the LAN endpoint.

        No synchronous self-HTTP request: the ASGI worker can safely call this
        while handling a request, and a remote server cannot impersonate health.
        """
        status = self.listener_status()
        if status["lan_enabled"] is not True or status["listener_verified"] is not True:
            raise HTTPException(409, status["lan_message"])
        try:
            normalized = validate_backend_for_physical_device(backend_url)
            parsed = urlsplit(normalized)
            address = ipaddress.ip_address(parsed.hostname)
            port = parsed.port or 80
            if parsed.path not in {"", "/"} or parsed.query:
                raise ValueError
            if not any(
                binding["port"] == port
                and ipaddress.ip_address(binding["host"]).version == address.version
                and (ipaddress.ip_address(binding["host"]).is_unspecified or binding["host"] == str(address))
                for binding in status["listener_bindings"]
            ):
                raise ValueError
            # Binding an ephemeral UDP socket proves the URL's literal IP is
            # assigned locally. It sends no packet and cannot deadlock ASGI.
            family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            with socket.socket(family, socket.SOCK_DGRAM) as probe:
                probe.bind((str(address), 0))
        except (ValueError, TypeError, OSError):
            raise HTTPException(409, "后台地址必须是当前物忆进程实际监听的本机局域网 IP 和端口；不能填写其他电脑、远端服务、错误端口或路径。") from None
        return normalized

    def _require_accepting_work_locked(self) -> None:
        if self._shutdown_started:
            raise HTTPException(503, "固件服务正在关闭，不再接受新的探测、编译或串口任务。")
        if self._mode_clear_active:
            raise HTTPException(409, "当前模式正在清空数据，固件服务暂不接受新任务。")

    def _ensure_accepting_work(self) -> None:
        with self._worker_condition:
            self._require_accepting_work_locked()

    @contextlib.contextmanager
    def _operation_guard(self):
        with self._worker_condition:
            self._require_accepting_work_locked()
            self._active_operations += 1
        try:
            yield
        finally:
            with self._worker_condition:
                self._active_operations -= 1
                self._worker_condition.notify_all()

    def _worker_finished(self, worker: threading.Thread) -> None:
        callbacks: list[Callable[[], Any]] = []
        drained_after_shutdown = False
        with self._worker_condition:
            self._workers.discard(worker)
            if self._shutdown_started and not self._workers:
                drained_after_shutdown = True
                callbacks = self._drained_callbacks
                self._drained_callbacks = []
            self._worker_condition.notify_all()
        if drained_after_shutdown:
            with contextlib.suppress(Exception):
                atexit.unregister(self.stop_virtual_device)
        for callback in callbacks:
            with contextlib.suppress(Exception):
                callback()

    def _start_background_worker_locked(self, target: Callable[[], Any], *, name: str) -> threading.Thread:
        """Register before start so shutdown cannot miss a concurrently-created worker."""
        self._require_accepting_work_locked()

        def registered_target() -> None:
            try:
                target()
            finally:
                self._worker_finished(threading.current_thread())

        worker = threading.Thread(target=registered_target, daemon=True, name=name)
        self._workers.add(worker)
        try:
            worker.start()
        except BaseException:
            self._workers.discard(worker)
            self._worker_condition.notify_all()
            raise
        return worker

    def _start_background_worker(self, target: Callable[[], Any], *, name: str) -> threading.Thread:
        with self._worker_condition:
            return self._start_background_worker_locked(target, name=name)

    @contextlib.contextmanager
    def mode_clear_guard(self):
        """Temporarily exclude firmware workers while DEMO/TEST is cleared."""
        with self._worker_condition:
            active = tuple(worker for worker in self._workers if worker.is_alive())
            if self._shutdown_started:
                raise RuntimeError("固件服务正在关闭；本次未清空数据库或媒体。")
            if active:
                names = ", ".join(sorted(worker.name for worker in active))
                raise RuntimeError(f"固件后台任务仍在运行（{names}）；本次未清空数据库或媒体。")
            if self._active_operations:
                raise RuntimeError("固件同步写入仍在执行；本次未清空数据库或媒体。")
            if self._mode_clear_active:
                raise RuntimeError("另一个当前模式清空操作正在执行；本次未清空数据库或媒体。")
            self._mode_clear_active = True
        try:
            yield
        finally:
            with self._worker_condition:
                self._mode_clear_active = False
                self._worker_condition.notify_all()

    def shutdown(
        self,
        timeout_seconds: float | None = None,
        *,
        on_drained: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        """Stop accepting work, stop the virtual process, and boundedly join workers.

        Build tools and physical serial calls are not safely cancellable halfway
        through an operation.  If the bound expires, the caller gets an explicit
        failure and may keep its runtime-mode lease until ``on_drained`` runs.
        """
        timeout = self.shutdown_timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        if timeout < 0:
            raise ValueError("shutdown timeout must not be negative")
        self.auto_usb.close()
        with self._worker_condition:
            self._shutdown_started = True
            workers = tuple(self._workers)

        # A virtual device is a tracked child process rather than a Python
        # thread.  It has its own bounded terminate/kill sequence.
        self.stop_virtual_device()

        deadline = time.monotonic() + timeout
        current = threading.current_thread()
        for worker in workers:
            if worker is current:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(remaining)

        with self._worker_condition:
            # A registered worker cannot synchronously join itself.  Treat it
            # as active and fail explicitly rather than reporting a false drain.
            active = tuple(worker for worker in self._workers if worker.is_alive())
            if active and on_drained is not None:
                self._drained_callbacks.append(on_drained)
        if active:
            names = ", ".join(sorted(worker.name for worker in active))
            raise FirmwareShutdownTimeout(
                f"有界关机等待超时；仍有固件后台任务正在执行：{names}。"
                "正在执行的编译或串口操作未被假装取消，运行模式锁必须保留到它们真正结束。"
            )
        with contextlib.suppress(Exception):
            atexit.unregister(self.stop_virtual_device)
        return {"shutdown": True, "active_workers": 0, "virtual_device_running": False}

    @contextlib.contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize_database(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS device_enrollments (
                    id TEXT PRIMARY KEY, code_hash TEXT NOT NULL UNIQUE,
                    device_name TEXT NOT NULL, room_name TEXT NOT NULL,
                    expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS firmware_devices (
                    id TEXT PRIMARY KEY, device_name TEXT NOT NULL, room_name TEXT NOT NULL,
                    mac_address TEXT NOT NULL, firmware_version TEXT NOT NULL,
                    ip_address TEXT NOT NULL, stream_url TEXT NOT NULL, capture_url TEXT NOT NULL,
                    capabilities TEXT NOT NULL, camera_id TEXT, token_hash TEXT NOT NULL,
                    token_revoked_at TEXT, last_heartbeat TEXT, online INTEGER NOT NULL DEFAULT 0,
                    uptime INTEGER, wifi_rssi INTEGER, free_heap INTEGER,
                    camera_status TEXT NOT NULL DEFAULT 'initializing',
                    reported_stream_status TEXT NOT NULL DEFAULT 'initializing',
                    stream_status TEXT NOT NULL DEFAULT 'initializing', current_fps REAL,
                    recent_error TEXT, simulated INTEGER NOT NULL DEFAULT 0,
                    hardware_verified INTEGER NOT NULL DEFAULT 0,
                    verification_method TEXT, serial_verified_at TEXT,
                    source_attestation_id TEXT,
                    attested_stream_url TEXT, attested_capture_url TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_firmware_devices_mac ON firmware_devices(mac_address);
                CREATE TABLE IF NOT EXISTS firmware_jobs (
                    id TEXT PRIMARY KEY, job_type TEXT NOT NULL, status TEXT NOT NULL,
                    progress INTEGER NOT NULL, stage TEXT NOT NULL, logs TEXT NOT NULL,
                    error TEXT, result TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS device_commands (
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL, command TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, delivered_at TEXT,
                    FOREIGN KEY(device_id) REFERENCES firmware_devices(id) ON DELETE CASCADE
                );
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(firmware_devices)")}
            if "reported_stream_status" not in columns:
                connection.execute(
                    "ALTER TABLE firmware_devices ADD COLUMN reported_stream_status TEXT NOT NULL DEFAULT 'initializing'"
                )
            if "hardware_verified" not in columns:
                # Historical network claims did not prove a physical board.  A
                # migration must therefore fail closed instead of promoting
                # every non-simulated row to physical hardware.
                connection.execute(
                    "ALTER TABLE firmware_devices ADD COLUMN hardware_verified INTEGER NOT NULL DEFAULT 0"
                )
            if "verification_method" not in columns:
                connection.execute("ALTER TABLE firmware_devices ADD COLUMN verification_method TEXT")
            if "serial_verified_at" not in columns:
                connection.execute("ALTER TABLE firmware_devices ADD COLUMN serial_verified_at TEXT")
            if "source_attestation_id" not in columns:
                connection.execute("ALTER TABLE firmware_devices ADD COLUMN source_attestation_id TEXT")
            if "attested_stream_url" not in columns:
                connection.execute("ALTER TABLE firmware_devices ADD COLUMN attested_stream_url TEXT")
            if "attested_capture_url" not in columns:
                connection.execute("ALTER TABLE firmware_devices ADD COLUMN attested_capture_url TEXT")
            connection.execute(
                "UPDATE firmware_jobs SET status='failed', stage='服务重启，任务未完成', "
                "error='物忆服务在任务执行期间重启，请重新执行。', updated_at=? "
                "WHERE status IN ('queued','running')", (iso_now(),)
            )
            # Runtime capture workers are process-local.  After an API restart,
            # a persisted "online" bit cannot prove that either the device or
            # its video reader is alive.  The next authenticated heartbeat will
            # atomically restore online state and rebind the camera.
            connection.execute(
                "UPDATE firmware_devices SET online=0,stream_status='offline',current_fps=NULL,"
                "recent_error='后台已重启，等待设备下一次心跳。',updated_at=? WHERE online=1",
                (iso_now(),),
            )

    def _load_server_secret(self) -> bytes:
        directory = self.data_root / "database"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ".device-enrollment.key"
        try:
            secret = path.read_bytes()
            if len(secret) == 32:
                return secret
        except FileNotFoundError:
            pass
        secret = secrets.token_bytes(32)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(secret)
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
        return secret

    def pairing_hash(self, code: str) -> str:
        return hmac.new(self.secret, code.encode("ascii"), hashlib.sha256).hexdigest()

    def _check_claim_rate(self, client_host: str) -> None:
        now = time.monotonic()
        with self.claim_rate_lock:
            recent = [value for value in self.claim_failures.get(client_host, []) if now - value < 60]
            self.claim_failures[client_host] = recent
            if len(recent) >= 8:
                raise HTTPException(429, "配对尝试过多，请一分钟后再试。")

    def _record_claim_failure(self, client_host: str) -> None:
        with self.claim_rate_lock:
            self.claim_failures.setdefault(client_host, []).append(time.monotonic())

    def _clear_claim_failures(self, client_host: str) -> None:
        with self.claim_rate_lock:
            self.claim_failures.pop(client_host, None)

    def _claim_error(self, client_host: str, status: int, detail: str):
        self._record_claim_failure(client_host)
        raise HTTPException(status, detail)

    @firmware_operation
    def create_enrollment(self, device_name: str, room_name: str) -> dict[str, Any]:
        for _ in range(5):
            code = "OM-" + "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(6))
            now = utc_now()
            expires = now + timedelta(seconds=PAIRING_TTL_SECONDS)
            try:
                with self.connect() as connection:
                    connection.execute(
                        "INSERT INTO device_enrollments(id,code_hash,device_name,room_name,expires_at,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (uuid4().hex, self.pairing_hash(code), device_name, room_name,
                         expires.isoformat(), now.isoformat()),
                    )
                return {"pairing_code": code, "expires_at": expires.isoformat(),
                        "expires_in": PAIRING_TTL_SECONDS}
            except sqlite3.IntegrityError:
                continue
        raise HTTPException(503, "暂时无法生成配对码，请重试。")

    def _device_dict(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data["device_id"] = data["id"]
        data["capabilities"] = json.loads(data.get("capabilities") or "[]")
        data["online"] = bool(data.get("online"))
        data["simulated"] = bool(data.get("simulated"))
        data["hardware_verified"] = bool(
            data.get("hardware_verified")
            and not data["simulated"]
            and not data.get("token_revoked_at")
            and data.get("verification_method") == "usb_serial_omready"
            and data.get("serial_verified_at")
            and data.get("source_attestation_id")
            and data.get("stream_url") == data.get("attested_stream_url")
            and data.get("capture_url") == data.get("attested_capture_url")
        )
        if data["simulated"]:
            data["hardware_type"] = "virtual"
            data["display_label"] = "虚拟设备 · 非真实硬件"
            data["source_type"] = "virtual_esp32"
            data["verification_method"] = None
            data["serial_verified_at"] = None
        elif data["hardware_verified"]:
            data["hardware_type"] = "physical"
            data["display_label"] = data.get("device_name")
            data["source_type"] = "esp32_real"
        else:
            data["hardware_type"] = "unverified"
            data["display_label"] = "来源未验证设备 · 尚未通过本机 USB 串口验证"
            data["source_type"] = "esp32_unverified"
            data["verification_method"] = None
            data["serial_verified_at"] = None
            # The pending id is needed by the board during claim/OMREADY, but
            # it must not make the admin UI link to a camera that was never
            # bound.  Internal verification reads the raw row explicitly.
            data["camera_id"] = None
        data["runtime_mode"] = self.runtime_mode
        data["is_simulated"] = data["simulated"]
        data["token_revoked"] = bool(data.pop("token_revoked_at", None))
        data.pop("token_hash", None)
        return data

    def _read_virtual_status(self) -> dict[str, Any]:
        try:
            value = json.loads(self.virtual_status_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def virtual_status(self) -> dict[str, Any]:
        with self.virtual_lock:
            process = self.virtual_process
            running = process is not None and process.poll() is None
            exit_code = None if running or process is None else process.returncode
        status = self._read_virtual_status()
        device_id = status.get("device_id")
        device = self.get_device(str(device_id)) if device_id else None
        stage = status.get("stage", "starting") if running else (
            "error" if status.get("error") else "stopped"
        )
        return {
            "running": running,
            "runtime_mode":self.runtime_mode,"source_type":"virtual_esp32","is_simulated":True,
            "label": "虚拟设备 · 非真实硬件",
            "hardware_type": "virtual",
            "source": status.get("source"),
            "device_id": device_id,
            "camera_id": status.get("camera_id") or (device or {}).get("camera_id"),
            "stage": stage,
            "backend_online": bool((device or {}).get("online")),
            "last_heartbeat": (device or {}).get("last_heartbeat") or status.get("last_heartbeat"),
            "stream_status": (device or {}).get("stream_status"),
            "base_url": status.get("base_url"),
            "stream_url": status.get("stream_url"),
            "expected_offline_after_seconds": OFFLINE_AFTER_SECONDS,
            "process_exit_code": exit_code,
            "log_path": str(self.virtual_directory / "virtual-device.log"),
            "error": status.get("error"),
        }

    def start_virtual_device(self, request: VirtualDeviceStart) -> dict[str, Any]:
        if self.runtime_mode not in {"DEMO", "TEST"}:
            raise HTTPException(409, "虚拟 ESP32 只能在 DEMO/TEST 进程中启动。")
        self._ensure_accepting_work()
        with self.virtual_lock:
            if self.virtual_process is not None and self.virtual_process.poll() is None:
                raise HTTPException(409, "虚拟 ESP32-CAM 已经在运行。")
            if self.virtual_log_handle is not None:
                self.virtual_log_handle.close()
                self.virtual_log_handle = None
            self.virtual_status_path.unlink(missing_ok=True)
            log_path = self.virtual_directory / "virtual-device.log"
            port = int(os.environ.get("OM_PORT", "8018"))
            backend_url = f"http://127.0.0.1:{port}"
            virtual_port_path = self.virtual_directory / "control-port.txt"
            try:
                virtual_port = int(virtual_port_path.read_text(encoding="ascii").strip())
                if not 1024 <= virtual_port <= 65535:
                    raise ValueError
            except (OSError, ValueError):
                with socket.socket() as available:
                    available.bind(("127.0.0.1", 0))
                    virtual_port = int(available.getsockname()[1])
                virtual_port_path.write_text(str(virtual_port), encoding="ascii")
            with socket.socket() as port_check:
                if os.name == "nt":
                    port_check.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                try:
                    port_check.bind(("127.0.0.1", virtual_port))
                except OSError as exc:
                    raise HTTPException(
                        409,
                        f"虚拟设备端口 {virtual_port} 已被占用；请先停止旧虚拟设备或关闭占用程序。",
                    ) from exc
            self.virtual_log_handle = log_path.open("a", encoding="utf-8", newline="\n")
            command = [
                sys.executable, "-m", "tools.virtual_esp32cam",
                "--backend-url", backend_url,
                "--auto-enroll",
                "--device-name", request.device_name,
                "--room-name", request.room_name,
                "--source", request.source,
                "--port", str(virtual_port),
                "--camera-index", str(request.camera_index),
                "--binding-file", str(self.virtual_binding_path),
                "--status-file", str(self.virtual_status_path),
            ]
            if request.source == "video":
                video_directory = self.project_root / "demo" / "sample-videos"
                video_path = video_directory / "object-memory-demo.mp4"
                if not video_path.is_file():
                    video_path = video_directory / "object-memory-demo.avi"
                command.extend(("--video-path", str(video_path)))
            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "1"
            try:
                # Serialize the final acceptance check and Popen publication
                # with shutdown.  A process started just before the shutdown
                # latch is set is subsequently observed and stopped by it.
                with self._worker_condition:
                    self._require_accepting_work_locked()
                    self.virtual_process = subprocess.Popen(
                        command,
                        cwd=str(self.project_root),
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=self.virtual_log_handle,
                        stderr=subprocess.STDOUT,
                        shell=False,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
            except Exception:
                self.virtual_log_handle.close()
                self.virtual_log_handle = None
                raise
        # Registration and frame probing continue asynchronously.  The status
        # endpoint is the only source of online/ready truth for the UI.
        return self.virtual_status()

    def stop_virtual_device(self) -> dict[str, Any]:
        with self.virtual_lock:
            process = self.virtual_process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        with self.virtual_lock:
            # Publish the stopped state only after the bounded process wait has
            # really completed.  On a wait/kill failure the reference remains
            # available for an explicit retry and shutdown keeps the mode lock.
            if self.virtual_process is process:
                self.virtual_process = None
            if self.virtual_log_handle is not None:
                self.virtual_log_handle.close()
                self.virtual_log_handle = None
        status_record = self._read_virtual_status()
        if status_record:
            status_record.update({"running": False, "stage": "stopped", "pid": None})
            temporary = self.virtual_status_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(status_record, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.virtual_status_path)
        result = self.virtual_status()
        result["message"] = (
            "虚拟设备进程已停止；后台会在 45 秒未收到心跳后按真实规则标记离线。"
            if process is not None else "虚拟设备没有运行。"
        )
        return result

    def _firmware_manifest_evidence(self, board_model: str = DEFAULT_BOARD_MODEL) -> tuple[dict, list, str | None, str | None]:
        """Read one bounded manifest snapshot and freshly hash its local files.

        Call only while holding manifest_lock. readinto reuses one small buffer
        across the bundle, including the much larger debug ELF. File failures
        invalidate evidence instead of turning a status poll into an HTTP 500.
        This bounds this path's allocations; it is not an overall OOM cure.
        """
        board = firmware_board(board_model)
        manifest_path = self.project_root / board["manifest"]
        try:
            with manifest_path.open("rb") as handle:
                raw = handle.read(FIRMWARE_MANIFEST_MAX_BYTES + 1)
            if len(raw) > FIRMWARE_MANIFEST_MAX_BYTES:
                return {}, [], None, "manifest_too_large"
            manifest = json.loads(raw.decode("utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError
            current_manifest_sha256 = hashlib.sha256(raw).hexdigest()
            del raw
        except MemoryError:
            return {}, [], None, "memory_pressure"
        except (OSError, ValueError, RecursionError):
            return {}, [], None, "manifest_unreadable_or_invalid"
        artifacts = manifest.get("artifacts")
        if board_model != DEFAULT_BOARD_MODEL and (manifest.get("environment") != board["environment"]
                or manifest.get("board_model") != board_model):
            return manifest, [], current_manifest_sha256, "manifest_board_mismatch"
        if not isinstance(artifacts, list) or not 0 < len(artifacts) <= FIRMWARE_MANIFEST_MAX_ARTIFACTS:
            return manifest, [], current_manifest_sha256, "invalid_artifact_list"
        checked_artifacts = []
        verification_error = None
        try:
            buffer = bytearray(FIRMWARE_HASH_BUFFER_BYTES)
        except MemoryError:
            return manifest, [], current_manifest_sha256, "memory_pressure"
        view = memoryview(buffer)
        names = set()
        for item in artifacts:
            name = item.get("name") if isinstance(item, dict) else None
            if (not isinstance(name, str) or not name or len(name) > 128
                    or name != Path(name).name or "/" in name or "\\" in name
                    or name in names):
                verification_error = "invalid_artifact_entry"
                continue
            names.add(name)
            # Never trust an arbitrary path from JSON when reading files.
            path = self.project_root / board["build"] / name
            actual_hash = None
            error = None
            try:
                if not path.is_file() or path.is_symlink():
                    raise ValueError("not a regular artifact")
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    before = os.fstat(handle.fileno())
                    if not 0 < before.st_size <= FIRMWARE_ARTIFACT_MAX_BYTES:
                        raise ValueError("artifact size outside bounded status verification")
                    total = 0
                    while count := handle.readinto(buffer):
                        total += count
                        if total > FIRMWARE_ARTIFACT_MAX_BYTES:
                            raise ValueError("artifact grew during verification")
                        digest.update(view[:count])
                    after = os.fstat(handle.fileno())
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or total != before.st_size:
                        raise ValueError("artifact changed during verification")
                actual_hash = digest.hexdigest()
            except MemoryError:
                error = "memory_pressure"
            except (OSError, ValueError):
                error = "artifact_unreadable_or_changed"
            expected_hash = item.get("sha256")
            valid = (actual_hash is not None and isinstance(expected_hash, str)
                     and re.fullmatch(r"[0-9a-f]{64}", expected_hash) is not None
                     and secrets.compare_digest(actual_hash, expected_hash))
            checked_artifacts.append({**item, "path": str(path), "hash_verified": valid,
                                      "verification_error": error if error else None if valid else "hash_mismatch"})
        return manifest, checked_artifacts, current_manifest_sha256, verification_error

    def firmware_manifest(self, board_model: str = DEFAULT_BOARD_MODEL) -> dict[str, Any]:
        board = firmware_board(board_model)
        manifest_path = self.project_root / board["manifest"]
        with self.manifest_lock:
            manifest, checked_artifacts, current_manifest_sha256, verification_error = self._firmware_manifest_evidence(board_model)
        hashes_valid = bool(checked_artifacts) and not verification_error and all(
            item["hash_verified"] for item in checked_artifacts)
        ports = self.discover_ports()
        # VID/PID means physical USB serial, not proof that it is an ESP32-CAM.
        known_bridges = {(0x1A86, 0x7523), (0x10C4, 0xEA60), (0x0403, 0x6001)}
        suspected_bridge = any(
            port.get("eligible") and (port.get("vid"), port.get("pid")) in known_bridges for port in ports
        )
        # A historical job row or device self-report is not durable evidence of
        # a physical flash. Only the signed/hashed build manifest may assert it.
        flash_performed = manifest.get("physical_flash_performed") is True and bool(manifest.get("physical_flash_evidence"))
        # Automatic installation writes a separate durable receipt so the
        # administrator-pinned build manifest never mutates after approval.
        # A bare successful job or an unrelated/historic bundle is not enough.
        auto_status = self.auto_usb.status()
        auto_binding = auto_status.get("binding") or {}
        auto_receipt = auto_status.get("receipt") or {}
        auto_flash_verified = bool(
            self.runtime_mode == "REAL" and hashes_valid and auto_receipt.get("flashed_at")
            and auto_receipt.get("status") in {"flashed", "linked"}
            and auto_binding.get("board_model") == board_model
            and auto_receipt.get("board_model", DEFAULT_BOARD_MODEL) == board_model
            and current_manifest_sha256 == auto_binding.get("manifest_sha256")
            and auto_receipt.get("mac") == auto_binding.get("mac_address")
            and auto_receipt.get("manifest_sha256") == current_manifest_sha256)
        flash_performed = flash_performed or auto_flash_verified
        # A CH340/CP210x/FTDI VID/PID identifies a USB serial bridge, not the
        # board behind it.  Only a completed physical upload is proof enough to
        # call the ESP32-CAM detected.
        physical_board_detected = flash_performed and manifest.get("physical_board_detected") is True and bool(manifest.get("physical_board_evidence"))
        return {
            **manifest,
            "board_model": board_model,
            "runtime_mode":self.runtime_mode,"source_type":"firmware_build","is_simulated":False,
            "artifacts": checked_artifacts,
            "manifest_path": str(manifest_path),
            "source_present": (self.project_root / "firmware" / "esp32cam" / "src" / "main.cpp").is_file(),
            "compile_passed": bool(manifest) and hashes_valid,
            "manifest_hashes_verified": hashes_valid,
            "manifest_verification_error": verification_error,
            "physical_usb_serial_detected": any(port.get("eligible") for port in ports),
            "suspected_esp32_usb_serial": suspected_bridge,
            "physical_board_detected": physical_board_detected,
            "physical_flash_performed": flash_performed,
            "bound_usb_flash_evidence": ({"manifest_sha256": current_manifest_sha256,
                "mac_address": auto_binding.get("mac_address"), "flashed_at": auto_receipt.get("flashed_at"),
                "verification_method": "usb_rom_mac_fixed_manifest"} if auto_flash_verified else None),
            "serial_provision_performed": (manifest.get("serial_provision_performed") is True and bool(manifest.get("serial_provision_evidence"))) or auto_status.get("physical_source_verified") is True,
            "real_video_verified": manifest.get("real_video_verified") is True and bool(manifest.get("real_video_evidence")),
            "physical_status_note": (
                "已有带证据的物理板识别与真机刷写记录；真机视频仍需独立验收。"
                if physical_board_detected
                else "检测到常见 USB 串口桥；仍需实际握手或刷写才能确认 ESP32-CAM。"
                if suspected_bridge
                else "有该固定版本的 USB 真机刷写收据；当前物理连接与视频仍需单独确认。"
                if auto_flash_verified
                else "未检测到可确认的 ESP32-CAM；未执行真机刷写。"
            ),
            "virtual_device": self.virtual_status(),
        }

    def _mark_stale_offline(self) -> None:
        with self._worker_condition:
            if self._mode_clear_active:
                return
            self._active_operations += 1
        try:
            cutoff = (utc_now() - timedelta(seconds=OFFLINE_AFTER_SECONDS)).isoformat()
            with self.connect() as connection:
                stale = connection.execute(
                    "SELECT id,camera_id FROM firmware_devices WHERE online=1 "
                    "AND (last_heartbeat IS NULL OR last_heartbeat<?)", (cutoff,)
                ).fetchall()
                connection.execute(
                    "UPDATE firmware_devices SET online=0,stream_status='offline',current_fps=NULL,updated_at=? "
                    "WHERE online=1 AND (last_heartbeat IS NULL OR last_heartbeat<?)",
                    (iso_now(), cutoff),
                )
            if self.camera_stop_callback:
                for row in stale:
                    try:
                        self.camera_stop_callback(row["camera_id"] or row["id"])
                    except Exception:
                        # Status remains truthful even if the runtime was already stopped.
                        pass
        finally:
            with self._worker_condition:
                self._active_operations -= 1
                self._worker_condition.notify_all()

    def list_devices(self) -> list[dict[str, Any]]:
        self._mark_stale_offline()
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM firmware_devices ORDER BY created_at DESC").fetchall()
        return [self._device_dict(row) for row in rows]

    def get_device(self, device_id: str, *, include_secret: bool = False) -> dict[str, Any] | None:
        self._mark_stale_offline()
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM firmware_devices WHERE id=?", (device_id,)).fetchone()
        if not row:
            return None
        return dict(row) if include_secret else self._device_dict(row)

    def _rollback_claim(self, enrollment_id: str, device_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM firmware_devices WHERE id=?", (device_id,))
            connection.execute("UPDATE device_enrollments SET used_at=NULL WHERE id=?", (enrollment_id,))

    @firmware_operation
    def claim(self, claim: DeviceClaim, client_host: str = "local-test") -> dict[str, Any]:
        self._check_claim_rate(client_host)
        code_hash = self.pairing_hash(claim.pairing_code)
        device_token = secrets.token_urlsafe(32)
        token_hash = hash_device_token(device_token)
        now = iso_now()
        claims_simulator = "simulator" in claim.capabilities
        if self.runtime_mode == "REAL" and claims_simulator:
            self._claim_error(client_host, 409, "REAL 模式不接受虚拟设备注册。")
        # Simulation identity is chosen by the isolated server process.  The
        # inverse is deliberately not true: a network client omitting the
        # ``simulator`` capability is still not proof of physical hardware.
        # REAL claims remain unverified until the local USB provisioning flow
        # observes a matching OMREADY record on an eligible serial port.
        simulated = self.runtime_mode in {"DEMO", "TEST"}
        enrollment_id = ""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            enrollment = connection.execute(
                "SELECT * FROM device_enrollments WHERE code_hash=?", (code_hash,)
            ).fetchone()
            if not enrollment:
                connection.rollback()
                self._claim_error(client_host, 401, "配对码不存在。")
            if enrollment["used_at"]:
                connection.rollback()
                self._claim_error(client_host, 409, "这个配对码已经使用过。")
            if datetime.fromisoformat(enrollment["expires_at"]) <= utc_now():
                connection.rollback()
                self._claim_error(client_host, 410, "配对码已过期，请在设备中心重新生成。")
            by_id = connection.execute(
                "SELECT mac_address,camera_id,created_at,hardware_verified FROM firmware_devices WHERE id=?", (claim.device_id,)
            ).fetchone()
            by_mac = connection.execute("SELECT id FROM firmware_devices WHERE mac_address=?", (claim.mac_address.upper(),)).fetchone()
            if by_id and by_id["mac_address"] != claim.mac_address.upper():
                connection.rollback()
                self._claim_error(client_host, 409, "设备编号已绑定到另一块硬件。")
            if by_mac and by_mac["id"] != claim.device_id:
                connection.rollback()
                self._claim_error(client_host, 409, "这块硬件已使用另一个设备编号绑定。")
            enrollment_id = enrollment["id"]
            connection.execute("UPDATE device_enrollments SET used_at=? WHERE id=? AND used_at IS NULL", (now, enrollment_id))
            camera_id = by_id["camera_id"] if by_id and by_id["camera_id"] else "cam_" + uuid4().hex[:12]
            created_at = by_id["created_at"] if by_id else now
            connection.execute(
                "INSERT OR REPLACE INTO firmware_devices("
                "id,device_name,room_name,mac_address,firmware_version,ip_address,stream_url,capture_url,"
                "capabilities,camera_id,token_hash,token_revoked_at,last_heartbeat,online,camera_status,"
                "stream_status,simulated,hardware_verified,verification_method,serial_verified_at,"
                "source_attestation_id,attested_stream_url,attested_capture_url,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (claim.device_id, enrollment["device_name"], enrollment["room_name"], claim.mac_address.upper(),
                 claim.firmware_version, claim.ip_address, claim.stream_url, claim.capture_url,
                 json.dumps(claim.capabilities), camera_id, token_hash, None, None, 0,
                 "initializing", "initializing", int(simulated), 0, None, None,
                 None, None, None, created_at, now),
            )

        device = self.get_device(claim.device_id)
        assert device is not None
        try:
            if self.runtime_mode == "REAL" and by_id and self.camera_stop_callback:
                # Re-enrollment rotates the token and resets physical proof.
                # Stop any old engine before the new serial READY is verified.
                with contextlib.suppress(Exception):
                    self.camera_stop_callback(camera_id)
            # DEMO/TEST virtual devices bind immediately inside their isolated
            # process.  A REAL network claim never creates a camera: that is
            # deferred to _verify_serial_ready_and_bind().
            if self.camera_callback and simulated:
                callback_result = self.camera_callback({
                    **device,
                    "name": device["device_name"],
                    "source_type": "esp32",
                    "source": device["stream_url"],
                    "config": {"capture_url": device["capture_url"], "device_id": device["id"]},
                    "enabled": True,
                    "inference_fps": 5,
                    "save_clips": True,
                })
                callback_camera_id = callback_result.get("id") if isinstance(callback_result, dict) else callback_result
                if callback_camera_id:
                    with self.connect() as connection:
                        connection.execute("UPDATE firmware_devices SET camera_id=?,updated_at=? WHERE id=?",
                                           (str(callback_camera_id), iso_now(), claim.device_id))
                    camera_id = str(callback_camera_id)
                else:
                    camera_id = device["camera_id"]
        except Exception as exc:
            self._rollback_claim(enrollment_id, claim.device_id)
            raise HTTPException(503, "设备已验证，但自动添加摄像头失败，请重新生成配对码。") from exc

        if simulated:
            self._schedule_probe(claim.device_id)
        self._clear_claim_failures(client_host)
        return {"success": True, "camera_id": camera_id, "device_token": device_token,
                "heartbeat_interval": HEARTBEAT_INTERVAL_SECONDS, "ota_channel": "stable",
                "runtime_mode": self.runtime_mode,
                "source_type": "virtual_esp32" if simulated else "esp32_unverified",
                "is_simulated": simulated, "hardware_verified": False,
                "hardware_type": "virtual" if simulated else "unverified"}

    def _schedule_probe(self, device_id: str) -> None:
        self._ensure_accepting_work()
        with self.probe_lock:
            if device_id in self.active_probes:
                return
            self.active_probes.add(device_id)

        def run() -> None:
            try:
                self._probe_device(device_id)
            finally:
                with self.probe_lock:
                    self.active_probes.discard(device_id)

        try:
            self._start_background_worker(run, name=f"probe-{device_id}")
        except BaseException:
            with self.probe_lock:
                self.active_probes.discard(device_id)
            raise

    def _verify_serial_ready_and_bind(
        self,
        port: str,
        expected: dict[str, Any],
        ready: dict[str, Any],
    ) -> dict[str, Any]:
        """Promote one REAL claim only after a matching local USB OMREADY.

        HTTP claim fields are self-asserted by the network client.  They may
        establish a token but cannot establish physical provenance.  The local
        provision/install worker is the only caller of this method; it has an
        eligible enumerated USB serial port and compares the serial response to
        the row already committed by the network claim.
        """
        if self.runtime_mode != "REAL":
            raise RuntimeError("物理设备串口验证只能在 REAL 进程中完成。")
        expected_board = expected.get("board_model", DEFAULT_BOARD_MODEL)
        self._validate_serial_board(ready, expected_board)
        verified_port = self.validated_port(port)
        device_id = str(expected.get("device_id") or "")
        if not device_id or ready.get("device_id") != device_id:
            raise RuntimeError("串口 READY 的 device_id 与本次配网请求不一致。")
        device = self.get_device(device_id, include_secret=True)
        if not device:
            raise RuntimeError("设备尚未通过一次性配对码完成后台注册，不能确认物理硬件。")
        if bool(device.get("simulated")):
            raise RuntimeError("模拟设备不能升级为物理硬件。")
        expected_mac = expected.get("verified_chip_mac") or expected.get("mac_address")
        if expected_mac and str(device.get("mac_address") or "").lower() != str(expected_mac).lower():
            raise RuntimeError("网络申领 MAC 与本次 USB ROM 芯片不一致，未升级为真实硬件。")
        expected_camera_id = str(device.get("camera_id") or "")
        if not expected_camera_id or ready.get("camera_id") != expected_camera_id:
            raise RuntimeError("串口 READY 的 camera_id 与后台配对记录不一致。")

        try:
            ready_ip = str(ipaddress.ip_address(str(ready.get("ip") or "")))
            ready_stream = validate_local_device_url(str(ready.get("stream_url") or ""), expected_ip=ready_ip)
            ready_capture = validate_local_device_url(str(ready.get("capture_url") or ""), expected_ip=ready_ip)
            stored_stream = validate_local_device_url(str(device.get("stream_url") or ""), expected_ip=ready_ip)
            stored_capture = validate_local_device_url(str(device.get("capture_url") or ""), expected_ip=ready_ip)
        except ValueError as exc:
            raise RuntimeError("串口 READY 的网络地址与后台配对记录不一致。") from exc
        if (
            ready_ip != str(device.get("ip_address") or "")
            or ready_stream != stored_stream
            or ready_capture != stored_capture
        ):
            raise RuntimeError("串口 READY 的 IP 或视频地址与后台配对记录不一致。")

        verified_at = iso_now()
        source_attestation_id = uuid4().hex
        with self.connect() as connection:
            connection.execute(
                "UPDATE firmware_devices SET hardware_verified=1,verification_method=?,serial_verified_at=?,"
                "source_attestation_id=?,attested_stream_url=?,attested_capture_url=?,updated_at=? "
                "WHERE id=? AND simulated=0",
                (
                    "usb_serial_omready", verified_at, source_attestation_id,
                    stored_stream, stored_capture, verified_at, device_id,
                ),
            )
        verified = self.get_device(device_id)
        if not verified or not verified.get("hardware_verified"):
            raise RuntimeError("物理硬件验证状态未能保存。")
        if self.camera_callback:
            callback_result = self.camera_callback({
                **verified,
                "name": verified["device_name"],
                "source_type": "esp32",
                "source": verified["stream_url"],
                "config": {
                    "capture_url": verified["capture_url"],
                    "device_id": verified["id"],
                    "hardware_verified": True,
                    "verification_method": "usb_serial_omready",
                    "serial_verified_at": verified_at,
                },
                "enabled": True,
                "inference_fps": 5,
                "save_clips": True,
            })
            callback_camera_id = callback_result.get("id") if isinstance(callback_result, dict) else callback_result
            if callback_camera_id and str(callback_camera_id) != expected_camera_id:
                raise RuntimeError("物理设备绑定返回了不同的 camera_id。")
        self._schedule_probe(device_id)
        return {
            "device_id": device_id,
            "camera_id": expected_camera_id,
            "hardware_verified": True,
            "verification_method": "usb_serial_omready",
            "serial_verified_at": verified_at,
            "source_attestation_id": source_attestation_id,
            "verified_port": verified_port,
        }

    def _probe_device(self, device_id: str) -> None:
        device = self.get_device(device_id, include_secret=True)
        if not device:
            return
        read_credential=None
        try:
            if device.get("token_revoked_at"):
                raise ValueError("设备凭据已撤销")
            read_credential = None if device.get("simulated") else camera_read_token(device.get("token_hash"))
            headers = {"Authorization": "Bearer " + read_credential} if read_credential else {}
            stream_url = validate_local_device_url(device["stream_url"], expected_ip=device["ip_address"])
            capture_url = validate_local_device_url(device["capture_url"], expected_ip=device["ip_address"])
            # ESP32-CAM serves bounded control requests on port 80 and keeps the
            # long-lived MJPEG response on port 81.  Derive control endpoints
            # from capture_url so a stream URL such as http://host:81/stream
            # never sends /health or /device to the streaming server.
            health_url = _replace_url_path(capture_url, "/health")
            descriptor_url = _replace_url_path(capture_url, "/device")
            from services.vision.camera_sources.network import _bounded_http_bytes, _decode_bounded_camera_jpeg
            with httpx.Client(timeout=httpx.Timeout(1.0, connect=3.0), follow_redirects=False, trust_env=False, headers=headers) as client:
                health_data = json.loads(_bounded_http_bytes(health_url,headers,6.0,64*1024))
                if not device.get("simulated") and health_data.get("camera_auth") != "hmac-sha256-v1":
                    raise ValueError("旧固件尚未保护摄像头视频；请更新固件，不能回退匿名读取")
                descriptor_data = json.loads(_bounded_http_bytes(descriptor_url,headers,6.0,64*1024))
                if descriptor_data.get("device_id") != device_id:
                    raise RuntimeError("/device 返回了不同的 device_id")
                capture = _bounded_http_bytes(capture_url,headers,6.0,2_000_000)
                if len(capture) < 128 or _decode_bounded_camera_jpeg(capture) is None:
                    raise RuntimeError("/capture 没有返回有效 JPEG")
                frame_times: list[float] = []
                buffer = b""
                started = time.perf_counter()
                with client.stream("GET", stream_url) as response:
                    response.raise_for_status()
                    if response.headers.get("content-encoding","identity").lower() not in {"","identity"}:
                        raise ValueError("摄像头视频不允许压缩 HTTP 编码")
                    for chunk in response.iter_raw():
                        buffer = (buffer + chunk)[-2_000_000:]
                        while True:
                            start = buffer.find(b"\xff\xd8")
                            end = buffer.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                            if start < 0 or end < 0:
                                break
                            # JPEG markers alone are not evidence of a video frame.
                            # Reuse the byte/pixel-bounded decoder used by /capture.
                            if _decode_bounded_camera_jpeg(buffer[start:end + 2]) is None:
                                raise RuntimeError("/stream 包含无法解码的 JPEG 图像帧")
                            frame_times.append(time.perf_counter())
                            buffer = buffer[end + 2:]
                            if len(frame_times) >= 3:
                                break
                        if len(frame_times) >= 3 or time.perf_counter() - started > 5:
                            break
                if not frame_times:
                    raise RuntimeError("/stream 在超时前没有读到图像帧")
                fps = None
                if len(frame_times) >= 2 and frame_times[-1] > frame_times[0]:
                    fps = round((len(frame_times) - 1) / (frame_times[-1] - frame_times[0]), 2)
            with self.connect() as connection:
                camera_status=health_data.get("camera_status","ready")
                if camera_status not in {"ready","error","initializing","offline"}:camera_status="unknown"
                connection.execute(
                    "UPDATE firmware_devices SET camera_status=?,stream_status='ready',current_fps=?,recent_error=NULL,updated_at=? WHERE id=?",
                    (camera_status, fps, iso_now(), device_id),
                )
        except Exception as exc:
            try:
                with self.connect() as connection:
                    connection.execute(
                        "UPDATE firmware_devices SET stream_status='error',current_fps=NULL,recent_error=?,updated_at=? WHERE id=?",
                        (redact_firmware_log(str(exc).replace(read_credential,"***") if read_credential else exc), iso_now(), device_id),
                    )
            except sqlite3.Error:
                # The app may be shutting down while a bounded probe finishes.
                pass

    @firmware_operation
    def heartbeat(self, device_id: str, token: str, heartbeat: HeartbeatInput) -> dict[str, Any]:
        device = self.get_device(device_id, include_secret=True)
        if not device or device.get("token_revoked_at") or not device.get("token_hash"):
            raise HTTPException(401, "设备令牌无效或已撤销。")
        if not secrets.compare_digest(device["token_hash"], hash_device_token(token)):
            raise HTTPException(401, "设备令牌无效或已撤销。")
        stream_url = device["stream_url"]
        capture_url = device["capture_url"]
        ip_changed = heartbeat.ip_address != device["ip_address"]
        came_online = not bool(device.get("online"))
        if ip_changed:
            stream_url = _replace_url_ip(stream_url, heartbeat.ip_address)
            capture_url = _replace_url_ip(capture_url, heartbeat.ip_address)
        now = iso_now()
        preserve_attestation = bool(
            not ip_changed
            and device.get("hardware_verified")
            and device.get("verification_method") == "usb_serial_omready"
            and device.get("serial_verified_at")
            and device.get("source_attestation_id")
            and device.get("stream_url") == device.get("attested_stream_url")
            and device.get("capture_url") == device.get("attested_capture_url")
        )
        with self.connect() as connection:
            connection.execute(
                "UPDATE firmware_devices SET firmware_version=?,ip_address=?,stream_url=?,capture_url=?,"
                "last_heartbeat=?,online=1,uptime=?,wifi_rssi=?,free_heap=?,camera_status=?,reported_stream_status=?,"
                "stream_status=?,hardware_verified=?,verification_method=?,serial_verified_at=?,"
                "source_attestation_id=?,attested_stream_url=?,attested_capture_url=?,"
                "recent_error=NULL,updated_at=? WHERE id=?",
                (heartbeat.firmware_version, heartbeat.ip_address, stream_url, capture_url, now,
                 heartbeat.uptime, heartbeat.wifi_rssi, heartbeat.free_heap,
                 heartbeat.camera_status, heartbeat.stream_status,
                 "checking" if (came_online or ip_changed) else device["stream_status"],
                 int(preserve_attestation),
                 device.get("verification_method") if preserve_attestation else None,
                 device.get("serial_verified_at") if preserve_attestation else None,
                 device.get("source_attestation_id") if preserve_attestation else None,
                 device.get("attested_stream_url") if preserve_attestation else None,
                 device.get("attested_capture_url") if preserve_attestation else None,
                 now, device_id),
            )
            commands = connection.execute(
                "SELECT id,command,created_at FROM device_commands WHERE device_id=? AND status='queued' ORDER BY created_at LIMIT 5",
                (device_id,),
            ).fetchall()
            if commands:
                connection.executemany(
                    "UPDATE device_commands SET status='delivered',delivered_at=? WHERE id=?",
                    [(now, row["id"]) for row in commands],
                )
        should_restart_camera = ip_changed or came_online
        may_bind_camera = bool(device.get("simulated")) or preserve_attestation
        if ip_changed and not bool(device.get("simulated")) and self.camera_stop_callback:
            with contextlib.suppress(Exception):
                self.camera_stop_callback(device.get("camera_id") or device_id)
        if should_restart_camera and may_bind_camera and self.camera_callback:
            current = self.get_device(device_id)
            assert current is not None
            try:
                self.camera_callback({**current, "name": current["device_name"], "source_type": "esp32",
                                      "source": stream_url, "config": {"capture_url": capture_url, "device_id": device_id},
                                      "enabled": True, "inference_fps": 5, "save_clips": True})
            except Exception as exc:
                with self.connect() as connection:
                    connection.execute("UPDATE firmware_devices SET recent_error=? WHERE id=?",
                                       (redact_firmware_log(exc), device_id))
        if may_bind_camera and (should_restart_camera or device.get("stream_status") != "ready"):
            self._schedule_probe(device_id)
        return {"success": True, "next_heartbeat_in": HEARTBEAT_INTERVAL_SECONDS,
                "commands": [dict(row) for row in commands]}

    @firmware_operation
    def revoke(self, device_id: str) -> dict[str, Any]:
        device = self.get_device(device_id)
        if not device:
            raise HTTPException(404, "没有找到这个设备。")
        now = iso_now()
        with self.connect() as connection:
            connection.execute(
                "UPDATE firmware_devices SET token_hash='',token_revoked_at=?,online=0,"
                "hardware_verified=0,verification_method=NULL,serial_verified_at=NULL,"
                "source_attestation_id=NULL,attested_stream_url=NULL,attested_capture_url=NULL,updated_at=? WHERE id=?",
                (now, now, device_id),
            )
        if self.camera_stop_callback:
            try:
                self.camera_stop_callback(device.get("camera_id") or device_id)
            except Exception:
                pass
        return {"success": True, "device_id": device_id, "status": "revoked"}

    @firmware_operation
    def queue_command(self, device_id: str, command: str) -> dict[str, Any]:
        device = self.get_device(device_id)
        if not device:
            raise HTTPException(404, "没有找到这个设备。")
        command_id = uuid4().hex
        created = iso_now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO device_commands(id,device_id,command,status,created_at) VALUES(?,?,?,?,?)",
                (command_id, device_id, command, "queued", created),
            )
        return {"accepted": True, "command_id": command_id, "command": command,
                "status": "queued", "message": "命令已排队，设备下次心跳时接收。"}

    def discover_ports(self) -> list[dict[str, Any]]:
        try:
            from serial.tools import list_ports
        except ImportError:
            return []
        return [port_record(port) for port in list_ports.comports()]

    def validated_port(self, requested: str) -> str:
        key = requested.upper() if os.name == "nt" else requested
        for record in self.discover_ports():
            record_key = record["device"].upper() if os.name == "nt" else record["device"]
            if secrets.compare_digest(record_key, key):
                if not record["eligible"]:
                    raise HTTPException(422, record["rejection_reason"] or "这个串口不能用于刷写。")
                return record["device"]
        raise HTTPException(422, "没有检测到所选 USB 串口，请拔插设备后刷新。")

    def platformio_info(self) -> dict[str, Any]:
        command = [str(self.firmware_python), "-m", "platformio", "--version"]
        try:
            completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                       timeout=10, shell=False, check=False)
            available = completed.returncode == 0
            version = completed.stdout.strip() if available else None
        except (OSError, subprocess.TimeoutExpired):
            available, version = False, None
        return {"available": available, "version": version,
                "python": str(self.firmware_python), "isolated": True}

    def platformio_device_list(self) -> list[dict[str, Any]]:
        command = [str(self.firmware_python), "-m", "platformio", "device", "list", "--json-output"]
        try:
            completed = subprocess.run(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                timeout=15, shell=False, check=False,
            )
            if completed.returncode != 0:
                return []
            data = json.loads(completed.stdout)
            if not isinstance(data, list):
                return []
            # PlatformIO output is diagnostic only; pyserial remains the authority
            # that also supplies VID/PID for the physical-port safety gate.
            return [{key: item.get(key) for key in ("port", "description", "hwid")} for item in data
                    if isinstance(item, dict)]
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return []

    def _job_dict(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data["logs"] = json.loads(data.get("logs") or "[]")
        data["result"] = json.loads(data["result"]) if data.get("result") else None
        return data

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM firmware_jobs WHERE id=?", (job_id,)).fetchone()
        return self._job_dict(row) if row else None

    def _create_job(self, job_type: str, worker: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
        job_id = uuid4().hex
        now = iso_now()

        def run() -> None:
            self._update_job(job_id, status="running", progress=1, stage="开始")
            try:
                result = worker(job_id, *args)
                self._update_job(job_id, status="succeeded", progress=100, stage="完成", result=result, error=None)
            except Exception as exc:
                self._append_job_log(job_id, f"失败：{redact_firmware_log(exc)}")
                self._update_job(job_id, status="failed", stage="失败", error=redact_firmware_log(exc))

        # Keep queue insertion and worker registration in the same lifecycle
        # critical section.  Shutdown therefore cannot leave a queued row whose
        # worker was never registered or started.
        with self._worker_condition:
            self._require_accepting_work_locked()
            with self.connect() as connection:
                connection.execute(
                    "INSERT INTO firmware_jobs(id,job_type,status,progress,stage,logs,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (job_id, job_type, "queued", 0, "等待执行", "[]", now, now),
                )
            try:
                self._start_background_worker_locked(
                    run, name=f"firmware-{job_type}-{job_id[:8]}"
                )
            except BaseException as exc:
                self._update_job(job_id, status="failed", stage="启动失败", error=redact_firmware_log(exc))
                raise
        job = self.get_job(job_id)
        assert job is not None
        return job

    def _update_job(self, job_id: str, **values: Any) -> None:
        allowed = {"status", "progress", "stage", "error", "result"}
        values = {key: value for key, value in values.items() if key in allowed}
        if "result" in values and values["result"] is not None:
            values["result"] = json.dumps(values["result"], ensure_ascii=False)
        values["updated_at"] = iso_now()
        with self.connect() as connection:
            connection.execute(
                "UPDATE firmware_jobs SET " + ",".join(f'"{key}"=?' for key in values) + " WHERE id=?",
                (*values.values(), job_id),
            )

    def _append_job_log(self, job_id: str, line: str) -> None:
        safe = redact_firmware_log(line)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT logs FROM firmware_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                connection.rollback()
                return
            logs = json.loads(row["logs"] or "[]")
            logs.append(safe)
            logs = logs[-MAX_JOB_LOG_LINES:]
            connection.execute("UPDATE firmware_jobs SET logs=?,updated_at=? WHERE id=?",
                               (json.dumps(logs, ensure_ascii=False), iso_now(), job_id))

    def _builder_process(self, job_id: str, *, flash: bool = False, port: str | None = None,
                         board_model: str = DEFAULT_BOARD_MODEL) -> tuple[int, dict[str, Any] | None]:
        board = firmware_board(board_model)
        if flash and board_model != DEFAULT_BOARD_MODEL:
            raise HTTPException(409, "XIAO 只允许通过绑定板卡的固定固件安装链路刷写。")
        command = [sys.executable, str(self.builder)]
        if board_model != DEFAULT_BOARD_MODEL:
            command.extend(("--environment", board["environment"]))
        if flash:
            command.append("--flash")
            command.extend(("--port", str(port)))
        self._append_job_log(job_id, "启动 PlatformIO 固件" + ("刷写" if flash else "编译") + "。")
        process = subprocess.Popen(
            command, cwd=str(self.project_root), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", shell=False,
        )
        assert process.stdout is not None
        result = None
        for raw in process.stdout:
            line = raw.rstrip("\r\n")
            if line.startswith("OMRESULT:"):
                try:
                    result = json.loads(line[9:])
                except json.JSONDecodeError:
                    self._append_job_log(job_id, "构建结果清单格式错误。")
            else:
                self._append_job_log(job_id, line)
        return process.wait(), result

    def start_build(self, clean: bool = False, board_model: str = DEFAULT_BOARD_MODEL) -> dict[str, Any]:
        self._ensure_accepting_work()
        firmware_board(board_model)
        if clean:
            # Clean is intentionally not exposed as arbitrary PlatformIO targets.
            raise HTTPException(422, "设备中心只执行可复现的完整编译，无需单独清理。")
        if board_model == DEFAULT_BOARD_MODEL:
            return self._create_job("build", self._build_worker)
        return self._create_job("build", self._build_worker, board_model)

    def _build_worker(self, job_id: str, board_model: str = DEFAULT_BOARD_MODEL) -> dict[str, Any]:
        with self.build_lock:
            self._update_job(job_id, progress=10, stage="编译通用固件")
            code, result = self._builder_process(job_id, board_model=board_model)
            if code != 0 or result is None:
                raise RuntimeError(f"PlatformIO 编译失败，退出码 {code}")
            return result

    def start_flash(self, requested_port: str) -> dict[str, Any]:
        self._ensure_accepting_work()
        if self.runtime_mode != "REAL":
            raise HTTPException(409, "固件刷写会访问物理 USB 串口，只允许在 REAL 模式执行。")
        port = self.validated_port(requested_port)
        return self._create_job("flash", self._flash_worker, port)

    def _flash_worker(self, job_id: str, port: str) -> dict[str, Any]:
        with self.build_lock:
            self._update_job(job_id, progress=10, stage="编译并刷写")
            for attempt in (1, 2):
                code, result = self._builder_process(job_id, flash=True, port=port)
                if code == 0 and result is not None:
                    result["flash_attempts"] = attempt
                    return result
                if attempt == 1:
                    self._append_job_log(job_id, "首次刷写失败，确认串口仍存在后自动重试一次。")
                    self.validated_port(port)
            raise RuntimeError("PlatformIO 两次刷写均失败；没有把设备标记为成功。")

    def start_provision(self, request: ProvisionRequest) -> dict[str, Any]:
        self._ensure_accepting_work()
        if self.runtime_mode != "REAL":
            raise HTTPException(409, "串口配网会访问物理 USB 设备，只允许在 REAL 模式执行。")
        self._verify_backend_listener(request.backend_url)
        port = self.validated_port(request.port)
        payload = request.model_dump()
        payload["port"] = port
        return self._create_job("provision", self._provision_worker, payload)

    def _open_serial(self, port: str, deadline: float, *, expected_bridge: dict[str, Any] | None = None):
        import serial
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            connection = None
            try:
                current_port = port
                if expected_bridge is not None:
                    from .firmware_usb import usb_helper
                    records = self.discover_ports()
                    if not any(record.get("eligible") for record in records):
                        # Native USB can disappear briefly after a board reset.
                        # Do not open an unrelated port while it is absent.
                        raise OSError("等待已绑定 USB 板重新枚举")
                    current_port = usb_helper(self.project_root).matching_port(records, expected_bridge)
                self.validated_port(current_port)
                connection = serial.Serial(port=current_port, baudrate=115200, timeout=0.25, write_timeout=3)
                connection.dtr = False
                connection.rts = False
                return connection
            except (OSError, serial.SerialException, HTTPException) as exc:
                if connection is not None:
                    with contextlib.suppress(Exception):
                        connection.close()
                last_error = exc
                time.sleep(0.5)
        raise RuntimeError(f"无法打开串口：{redact_firmware_log(last_error or 'timeout')}")

    @staticmethod
    def _validate_serial_board(record: dict[str, Any], board_model: str) -> None:
        board = firmware_board(board_model)
        if board_model == DEFAULT_BOARD_MODEL and not record.get("board_type") and not record.get("chip"):
            return  # Existing AI firmware protocol 1 predates board metadata.
        expected_chip = "esp32s3" if board["chip"] == "ESP32-S3" else "esp32"
        if record.get("board_type") != board_model or record.get("chip") != expected_chip:
            raise RuntimeError("串口应答板型或芯片与已绑定固件不一致；未确认物理板，也未发送配置。")

    def _open_bound_serial(self, port: str, deadline: float, expected_mac: str,
                           expected_bridge: dict[str, Any] | None,
                           expected_board: str = DEFAULT_BOARD_MODEL):
        """Verify the application on the SAME handle before any credential write.

        A fresh nonce prevents buffered/replayed replies from a previous boot.
        eFuse MAC continuity is not cryptographic proof against malicious hardware.
        """
        if not expected_bridge or not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", expected_mac):
            raise RuntimeError("自动配网缺少完整板卡绑定身份；未发送配置。")
        connection = self._open_serial(port, min(deadline, time.monotonic() + 15), expected_bridge=expected_bridge)
        opened_port = getattr(connection, "port", None) or port
        try:
            time.sleep(1.2)  # A bridge may reset the board when this handle opens.
            connection.reset_input_buffer()
            nonce = secrets.token_hex(16)
            challenge = ("OMWHO:" + nonce + "\n").encode("ascii")
            challenge_deadline = min(deadline, time.monotonic() + 12)
            last_challenge = float("-inf")
            while time.monotonic() < challenge_deadline:
                if self.auto_usb.stop.is_set():
                    raise RuntimeError("服务正在退出，未发送配网配置。")
                if time.monotonic() - last_challenge >= 1.0:
                    connection.write(challenge)
                    connection.flush()
                    last_challenge = time.monotonic()
                raw = connection.readline(MAX_SERIAL_LINE + 2)
                if not raw:
                    continue
                if len(raw) > MAX_SERIAL_LINE:
                    raise RuntimeError("板卡身份应答超过长度限制；未发送配置。")
                if not raw.startswith(b"OMIDENT:"):
                    continue  # ACK/READY from a former boot is not this challenge.
                try:
                    identity = json.loads(raw[8:].decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    raise RuntimeError("板卡身份应答损坏；未发送配置。") from None
                if not isinstance(identity, dict):
                    raise RuntimeError("板卡身份应答无效；未发送配置。")
                if identity.get("nonce") != nonce:
                    continue
                if (identity.get("mac_address") != expected_mac or type(identity.get("protocol")) is not int
                        or identity["protocol"] != 1):
                    raise RuntimeError("当前串口板卡 MAC 身份不匹配；未发送配置。")
                self._validate_serial_board(identity, expected_board)
                # The bridge must still match immediately after the challenge.
                from .firmware_usb import usb_helper
                if usb_helper(self.project_root).matching_port(self.discover_ports(), expected_bridge) != opened_port:
                    raise RuntimeError("板卡验证时 USB 桥已改变；未发送配置。")
                return connection
            raise RuntimeError("板卡身份挑战超时；未发送 Wi-Fi 或配对配置。")
        except BaseException:
            with contextlib.suppress(Exception):
                connection.close()
            raise

    def _provision_worker(self, job_id: str, payload: dict[str, Any], *, expected_mac: str | None = None,
                          expected_bridge: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(payload)
        self._verify_backend_listener(payload["backend_url"])
        self._update_job(job_id, progress=20, stage="串口写入配置")
        deadline = time.monotonic() + payload.pop("timeout_seconds", 90)
        port = payload.pop("port")
        # DeviceConfig intentionally rejects unknown fields. Board selection,
        # port, deadlines and recovery consent are HOST identity/authorization
        # metadata, not firmware configuration. Preserve them locally for both
        # same-handle identity checks and OMREADY verification, never on wire.
        wire_fields = ("ssid", "password", "backend_url", "pairing_code", "device_id",
                       "device_name", "room_name", "stream_quality", "frame_size")
        wire_config = {key: payload[key] for key in wire_fields if key in payload}
        line = "OMCFG:" + json.dumps(wire_config, ensure_ascii=False, separators=(",", ":")) + "\n"
        def open_connection():
            nonlocal port
            if expected_mac is not None:
                opened = self._open_bound_serial(port, deadline, expected_mac, expected_bridge,
                                                 payload.get("board_model", DEFAULT_BOARD_MODEL))
                port = getattr(opened, "port", None) or port
                return opened
            connection = self._open_serial(port, min(deadline, time.monotonic() + 20))
            try:
                time.sleep(1.2)
                connection.reset_input_buffer()
                return connection
            except BaseException:
                with contextlib.suppress(Exception):
                    connection.close()
                raise

        connection = open_connection()
        ack_received = False
        last_sent = 0.0
        sent_count = 0
        try:
            while time.monotonic() < deadline:
                if expected_mac is not None and self.auto_usb.stop.is_set():
                    raise RuntimeError("服务正在退出，停止自动配网。")
                if not ack_received and time.monotonic() - last_sent >= 2.5:
                    connection.write(line.encode("utf-8"))
                    connection.flush()
                    last_sent = time.monotonic()
                    sent_count += 1
                    if sent_count == 1:
                        self._append_job_log(job_id, "配置已写入串口；Wi-Fi 密码和配对码未写入日志。")
                try:
                    raw = connection.readline(MAX_SERIAL_LINE + 2)
                except Exception as exc:
                    # The USB serial interface can briefly disappear while the
                    # ESP32 resets after persisting configuration.
                    try:
                        connection.close()
                    except Exception:
                        pass
                    self._append_job_log(job_id, "设备重启，正在重新连接同一 USB 串口。")
                    connection = open_connection()  # Never reuse identity verification after reconnect.
                    continue
                if not raw:
                    continue
                if len(raw) > MAX_SERIAL_LINE:
                    raise RuntimeError("设备返回的串口消息超过长度限制。")
                try:
                    parsed = parse_serial_protocol_line(raw)
                except ValueError as exc:
                    self._append_job_log(job_id, f"忽略无法解析的设备消息：{exc}")
                    continue
                if parsed is None:
                    continue
                kind, data = parsed
                if kind == "log":
                    self._append_job_log(job_id, f"设备：{data.get('event')} {data.get('detail', '')}".strip())
                elif kind == "ack":
                    if not data["success"]:
                        raise RuntimeError(f"设备拒绝配置：{data.get('error', 'unknown')}")
                    if data["device_id"] != payload["device_id"]:
                        raise RuntimeError("设备 ACK 的 device_id 与请求不一致。")
                    ack_received = True
                    self._update_job(job_id, progress=65, stage="等待设备联网注册")
                    self._append_job_log(job_id, "设备已确认保存配置，正在重启、联网并注册。")
                elif kind == "ready":
                    if data["device_id"] != payload["device_id"]:
                        raise RuntimeError("设备 READY 的 device_id 与请求不一致。")
                    if not ack_received:
                        self._append_job_log(job_id, "设备重启时 ACK 未被电脑读到；READY 和后台注册已确认配置生效。")
                    verification = self._verify_serial_ready_and_bind(port, {**payload, "verified_chip_mac": expected_mac}, data)
                    self._update_job(job_id, progress=95, stage="设备已注册")
                    return {"device_id": data["device_id"], "camera_id": data["camera_id"],
                            "ip": data["ip"], "stream_url": data["stream_url"],
                            "capture_url": data["capture_url"], "ack_received": ack_received,
                            "hardware_verification": verification}
        finally:
            connection.close()
        if not ack_received:
            raise RuntimeError("串口配网超时：设备没有返回 OMACK。")
        raise RuntimeError("设备已保存配置，但在超时前没有返回 OMREADY；请检查 Wi-Fi 和后台地址。")

    def start_install(self, request: InstallRequest) -> dict[str, Any]:
        self._ensure_accepting_work()
        if self.runtime_mode != "REAL":
            raise HTTPException(409, "真机安装会刷写并访问物理 USB 串口，只允许在 REAL 模式执行。")
        self._verify_backend_listener(request.backend_url)
        port = self.validated_port(request.port)
        enrollment = self.create_enrollment(request.device_name, request.room_name)
        payload = {
            "port": port,
            "ssid": request.ssid,
            "password": request.password,
            "backend_url": request.backend_url,
            "pairing_code": enrollment["pairing_code"],
            "device_id": "omcam-" + uuid4().hex[:8],
            "device_name": request.device_name,
            "room_name": request.room_name,
            "timeout_seconds": 120,
        }
        return self._create_job("install", self._install_worker, payload)

    def _install_worker(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._verify_backend_listener(payload["backend_url"])
        port = payload["port"]
        with self.build_lock:
            self._update_job(job_id, progress=5, stage="编译并刷写通用固件")
            flash_result = None
            for attempt in (1, 2):
                code, result = self._builder_process(job_id, flash=True, port=port)
                if code == 0 and result is not None:
                    flash_result = result
                    flash_result["flash_attempts"] = attempt
                    break
                if attempt == 1:
                    self._append_job_log(job_id, "首次刷写失败，自动重试一次。")
                    self.validated_port(port)
            if flash_result is None:
                raise RuntimeError("通用固件两次刷写均失败，未执行配网。")
        self._update_job(job_id, progress=55, stage="串口配网和后台绑定")
        provisioned = self._provision_worker(job_id, dict(payload))
        return {"firmware": flash_result, "device": provisioned}


def _required_service(context: Request | WebSocket) -> FirmwareService:
    """Resolve firmware state from the concrete ASGI application.

    Routers are module globals, but services must not be: tests and embedded
    deployments legitimately host REAL and DEMO applications in one process.
    """
    service = getattr(context.app.state, "firmware_service", None)
    if service is None:
        raise HTTPException(503, "固件服务尚未初始化。")
    return service


def setup(db_path: Path | Any, camera_callback: Callable[[dict[str, Any]], Any] | None,
          data_root: Path, camera_stop_callback: Callable[[str], Any] | None = None,
          runtime_mode: str = "REAL", listener_info_provider: Callable[[], dict[str, Any]] | None = None) -> FirmwareService:
    """Create one firmware service for the caller's ASGI application."""
    resolved_db_path = Path(getattr(db_path, "path", db_path))
    return FirmwareService(resolved_db_path, camera_callback, Path(data_root), camera_stop_callback, runtime_mode, listener_info_provider)


@router.get("/api/firmware/ports")
def firmware_ports(http_request: Request):
    service = _required_service(http_request)
    return {"platformio": {**service.platformio_info(), "devices": service.platformio_device_list()},
            "ports": service.discover_ports()}


@router.get("/api/firmware/boards")
def supported_firmware_boards(http_request: Request):
    """Implementation/build inventory only; never opens serial or infers a board from VID/PID."""
    service = _required_service(http_request)
    labels = {"ai_thinker_esp32cam": ("AI Thinker ESP32-CAM", "ESP32-CAM 下载底板或 USB 转串口"),
              "xiao_esp32s3_sense": ("Seeed XIAO ESP32S3 Sense", "板载 USB-C 数据接口，需 Sense 摄像头扩展板")}
    rows = []
    for model, board in FIRMWARE_BOARDS.items():
        with service.manifest_lock:
            _, artifacts, _, error = service._firmware_manifest_evidence(model)
        source_present = all((service.project_root / path).is_file() for path in (
            "firmware/esp32cam/src/main.cpp", "firmware/esp32cam/include/board_config.h",
            "firmware/esp32cam/platformio.ini", "scripts/firmware-usb.py"))
        rows.append({"board_model": model, "label": labels[model][0], "chip": board["chip"],
            "connection": labels[model][1], "source_present": source_present,
            "build_verified": bool(artifacts) and not error and all(row.get("hash_verified") is True for row in artifacts),
            "implemented_steps": ["board_identity", "compile", "fixed_flash_verify", "serial_configure", "network_claim", "camera_read"],
            "physical_acceptance": "not_asserted_by_catalog", "auto_flash_unknown_device": False})
    return {"runtime_mode": service.runtime_mode, "source_type": "firmware_capabilities", "is_simulated": False,
            "boards": rows, "selection_policy": "restore_bound_board_else_explicit_selection",
            "notice": "仅支持列出的专用板型。已绑定板自动恢复型号；新板必须确认板上标识。串口或芯片相同不代表相机引脚、Flash、PSRAM 兼容；未知板不自动写入。"}


@router.get("/api/firmware/manifest")
def firmware_manifest(http_request: Request, board_model: BoardModel = DEFAULT_BOARD_MODEL):
    """Return verified build evidence plus explicit physical-hardware truth."""
    return _required_service(http_request).firmware_manifest(board_model)


@router.get("/api/firmware/status")
def firmware_status(http_request: Request, board_model: BoardModel = DEFAULT_BOARD_MODEL):
    """Compatibility alias used by the device center status cards."""
    return _required_service(http_request).firmware_manifest(board_model)


@router.get("/api/virtual-device/status")
def virtual_device_status(http_request: Request):
    service=_required_service(http_request)
    if service.runtime_mode not in {"DEMO","TEST"}:
        raise HTTPException(409, "虚拟 ESP32 只在 DEMO/TEST 模式可用。")
    return service.virtual_status()


@router.post("/api/virtual-device/start")
def virtual_device_start(http_request: Request, request: VirtualDeviceStart = VirtualDeviceStart()):
    service=_required_service(http_request)
    if service.runtime_mode not in {"DEMO","TEST"}:
        raise HTTPException(409, "虚拟 ESP32 只在 DEMO/TEST 模式可用。")
    return service.start_virtual_device(request)


@router.post("/api/virtual-device/stop")
def virtual_device_stop(http_request: Request):
    service=_required_service(http_request)
    if service.runtime_mode not in {"DEMO","TEST"}:
        raise HTTPException(409, "虚拟 ESP32 只在 DEMO/TEST 模式可用。")
    return service.stop_virtual_device()


@router.post("/api/firmware/build")
def firmware_build(http_request: Request, request: BuildRequest = BuildRequest()):
    return _required_service(http_request).start_build(request.clean, request.board_model)


@router.post("/api/firmware/flash")
def firmware_flash(http_request: Request, request: FlashRequest):
    return _required_service(http_request).start_flash(request.port)


@router.post("/api/firmware/provision")
def firmware_provision(http_request: Request, request: ProvisionRequest):
    return _required_service(http_request).start_provision(request)


@router.post("/api/firmware/install")
def firmware_install(http_request: Request, request: InstallRequest):
    return _required_service(http_request).start_install(request)


@router.get("/api/firmware/jobs/{job_id}")
def firmware_job(job_id: str, http_request: Request):
    job = _required_service(http_request).get_job(job_id)
    if not job:
        raise HTTPException(404, "没有找到这个固件任务。")
    return job


@router.websocket("/ws/firmware/{job_id}")
async def firmware_job_websocket(websocket: WebSocket, job_id: str):
    sessions = getattr(websocket.app.state, "sessions", None)
    if sessions is None or not await sessions.websocket(websocket,require_origin=True):
        return
    while True:
        job = _required_service(websocket).get_job(job_id)
        if not job:
            await websocket.send_json({"detail": "没有找到这个固件任务。"})
            await websocket.close(code=4404)
            return
        await websocket.send_json(job)
        if job["status"] in {"succeeded", "failed"}:
            await websocket.close(code=1000)
            return
        await asyncio.sleep(0.75)


@router.post("/api/device-enrollment/create")
def create_device_enrollment(http_request: Request, request: EnrollmentCreate):
    return _required_service(http_request).create_enrollment(request.device_name, request.room_name)


@router.post("/api/device-enrollment/claim")
def claim_device(request: DeviceClaim, http_request: Request):
    client_host = http_request.client.host if http_request.client else "unknown"
    return _required_service(http_request).claim(request, client_host)


@router.get("/api/devices")
def list_devices(http_request: Request):
    return _required_service(http_request).list_devices()


@router.get("/api/devices/{device_id}")
def get_device(device_id: str, http_request: Request):
    device = _required_service(http_request).get_device(device_id)
    if not device:
        raise HTTPException(404, "没有找到这个设备。")
    return device


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少设备令牌。")
    token = authorization[7:].strip()
    if len(token) < 24 or len(token) > 200:
        raise HTTPException(401, "设备令牌格式错误。")
    return token


@router.post("/api/devices/{device_id}/heartbeat")
def device_heartbeat(device_id: str, request: HeartbeatInput, http_request: Request,
                     authorization: str | None = Header(default=None)):
    return _required_service(http_request).heartbeat(device_id, _bearer_token(authorization), request)


@router.post("/api/devices/{device_id}/revoke")
def revoke_device(device_id: str, http_request: Request):
    return _required_service(http_request).revoke(device_id)


@router.post("/api/devices/{device_id}/commands")
def device_command(device_id: str, request: CommandRequest, http_request: Request):
    return _required_service(http_request).queue_command(device_id, request.command)


@router.post("/api/devices/{device_id}/ota")
def device_ota(device_id: str, http_request: Request):
    if not _required_service(http_request).get_device(device_id):
        raise HTTPException(404, "没有找到这个设备。")
    raise HTTPException(501, "当前通用固件尚未启用可验证回滚的 OTA；请使用 USB 刷写。")


@router.get("/api/devices/{device_id}/ota-status")
def device_ota_status(device_id: str, http_request: Request):
    if not _required_service(http_request).get_device(device_id):
        raise HTTPException(404, "没有找到这个设备。")
    return {"supported": False, "status": "unsupported", "message": "当前版本请使用 USB 安全刷写。"}


# All USB management paths stay behind the existing administrator session middleware.
# A device Bearer token is deliberately not an authorization mechanism here.
@router.get("/api/firmware/auto-usb")
def auto_usb_status(http_request: Request):
    return _required_service(http_request).auto_usb.status()


@router.post("/api/firmware/auto-usb/bind")
def auto_usb_bind(http_request: Request, request: AutoUsbBindingRequest):
    return _required_service(http_request).auto_usb.bind(request)


@router.post("/api/firmware/auto-usb/recover")
def auto_usb_recover(http_request: Request, request: AutoUsbRecoveryRequest):
    return _required_service(http_request).auto_usb.recover(request)


@router.post("/api/firmware/auto-usb/disable")
def auto_usb_disable(http_request: Request):
    return _required_service(http_request).auto_usb.disable()
