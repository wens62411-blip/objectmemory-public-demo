from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from apps.api.app.firmware import (
    DeviceClaim,
    FirmwareShutdownTimeout,
    FirmwareService,
    HeartbeatInput,
    hash_device_token,
    parse_serial_protocol_line,
    port_record,
    redact_firmware_log,
    ProvisionRequest,
    InstallRequest,
    VirtualDeviceStart,
)
from apps.api.app.main import create_app
from apps.api.app.mode_lock import RuntimeModeBusy


ROOT = Path(__file__).resolve().parents[3]


def load_script(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Dataclasses inspect sys.modules while resolving postponed annotations.
    import sys
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def service(tmp_path, callback=None, stop_callback=None, runtime_mode="REAL"):
    return FirmwareService(tmp_path / "object-memory.sqlite3", callback, tmp_path / "data", stop_callback, runtime_mode)


def claim_input(base_url="http://127.0.0.1:8766", code="OM-ABC234", device_id="omcam-test"):
    return DeviceClaim(
        device_id=device_id,
        pairing_code=code,
        mac_address="02:00:00:00:00:01",
        firmware_version="0.1.0",
        ip_address="127.0.0.1",
        stream_url=base_url + "/stream",
        capture_url=base_url + "/capture",
        capabilities=["camera", "mjpeg", "capture"],
    )


def heartbeat_input(ip="127.0.0.1"):
    return HeartbeatInput(uptime=10, wifi_rssi=-51, free_heap=100_000,
                          camera_status="ready", stream_status="ready",
                          firmware_version="0.1.0", ip_address=ip)


class FakeProvisionSerial:
    def __init__(self, *lines: str):
        self.lines = [line.encode("utf-8") + b"\n" for line in lines]
        self.writes: list[bytes] = []
        self.closed = False

    def reset_input_buffer(self):
        return None

    def write(self, value: bytes):
        self.writes.append(value)

    def flush(self):
        return None

    def readline(self, _limit: int):
        return self.lines.pop(0) if self.lines else b""

    def close(self):
        self.closed = True


def test_serial_protocol_parser_and_length_limit():
    kind, ack = parse_serial_protocol_line('OMACK:{"success":true,"device_id":"omcam-a"}\n')
    assert kind == "ack" and ack["device_id"] == "omcam-a"
    ready_line = (
        'OMREADY:{"device_id":"omcam-a","camera_id":"cam-a","ip":"127.0.0.1",'
        '"stream_url":"http://127.0.0.1:8766/stream",'
        '"capture_url":"http://127.0.0.1:8766/capture"}'
    )
    assert parse_serial_protocol_line(ready_line)[0] == "ready"
    assert parse_serial_protocol_line("boot:0x13") is None
    with pytest.raises(ValueError, match="超过长度"):
        parse_serial_protocol_line("OMLOG:" + json.dumps({"event": "x" * 3000}))
    with pytest.raises(ValueError, match="字段不完整"):
        parse_serial_protocol_line('OMREADY:{"device_id":"only-one-field"}')


def test_log_redaction_covers_wifi_pairing_and_tokens():
    raw = 'password="hunter123" pairing_code=OM-ABC234 device_token:secret-value http://user:pass@host/'
    redacted = redact_firmware_log(raw)
    assert "hunter123" not in redacted
    assert "ABC234" not in redacted
    assert "secret-value" not in redacted
    assert "user:pass" not in redacted


def test_physical_provisioning_rejects_loopback_backend():
    with pytest.raises(ValueError, match="局域网 IP"):
        ProvisionRequest(port="COM7", ssid="Home", password="password123",
                         backend_url="http://127.0.0.1:8018", pairing_code="OM-ABC234",
                         device_id="omcam-a", device_name="客厅", room_name="客厅")


def test_physical_port_classification_rejects_bluetooth_and_unknown():
    bluetooth = SimpleNamespace(device="COM5", description="Standard Serial over Bluetooth link",
                                hwid="BTHENUM", vid=None, pid=None, serial_number=None,
                                manufacturer="Microsoft", product=None)
    usb = SimpleNamespace(device="COM7", description="USB-SERIAL CH340", hwid="USB VID:PID=1A86:7523",
                          vid=0x1A86, pid=0x7523, serial_number="A1", manufacturer="wch.cn",
                          product="USB2.0-Serial")
    unknown = SimpleNamespace(device="COM8", description="Communications Port", hwid="ACPI",
                              vid=None, pid=None, serial_number=None, manufacturer=None, product=None)
    assert port_record(bluetooth)["eligible"] is False
    assert "蓝牙" in port_record(bluetooth)["rejection_reason"]
    assert port_record(usb)["eligible"] is True
    assert port_record(unknown)["eligible"] is False


@pytest.mark.parametrize("runtime_mode", ["DEMO", "TEST"])
def test_non_real_modes_cannot_touch_physical_flash_or_serial(tmp_path, monkeypatch, runtime_mode):
    fw = service(tmp_path, runtime_mode=runtime_mode)
    monkeypatch.setattr(fw, "validated_port", lambda _port: pytest.fail("mode gate must run before port access"))
    provision = ProvisionRequest(
        port="COM7", ssid="Home", password="password123", backend_url="http://192.168.1.2:8018",
        pairing_code="OM-ABC234", device_id="omcam-a", device_name="客厅", room_name="客厅",
    )
    install = InstallRequest(
        port="COM7", ssid="Home", password="password123", backend_url="http://192.168.1.2:8018",
        device_name="客厅", room_name="客厅",
    )
    for operation in (lambda: fw.start_flash("COM7"), lambda: fw.start_provision(provision), lambda: fw.start_install(install)):
        with pytest.raises(HTTPException) as error:
            operation()
        assert error.value.status_code == 409


def test_pairing_code_is_single_use_and_only_token_hash_is_stored(tmp_path):
    callback_calls = []
    fw = service(tmp_path, lambda device: callback_calls.append(device) or "cam-real")
    enrollment = fw.create_enrollment("客厅摄像头", "客厅")
    request = claim_input(code=enrollment["pairing_code"])
    result = fw.claim(request)
    token = result["device_token"]
    assert result["camera_id"].startswith("cam_")
    assert result["hardware_verified"] is False
    assert result["hardware_type"] == "unverified"
    assert result["source_type"] == "esp32_unverified"
    assert callback_calls == []
    device = fw.get_device(request.device_id)
    assert device["camera_id"] is None
    assert device["hardware_verified"] is False
    assert device["hardware_type"] == "unverified"
    with fw.connect() as connection:
        row = connection.execute("SELECT token_hash FROM firmware_devices WHERE id=?", (request.device_id,)).fetchone()
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in row["token_hash"]
    with pytest.raises(HTTPException) as reused:
        fw.claim(request)
    assert reused.value.status_code == 409
    assert fw.heartbeat(request.device_id, token, heartbeat_input())["success"] is True
    with pytest.raises(HTTPException) as unauthorized:
        fw.heartbeat(request.device_id, "wrong-token-that-is-long-enough-123", heartbeat_input())
    assert unauthorized.value.status_code == 401


def test_reenrollment_rotates_token_but_preserves_camera_binding(tmp_path):
    callback_camera_ids, stopped_camera_ids = [], []

    def bind(device):
        callback_camera_ids.append(device["camera_id"])
        return device["camera_id"]

    fw = service(tmp_path, bind, stopped_camera_ids.append)
    first_code = fw.create_enrollment("客厅", "客厅")["pairing_code"]
    first = fw.claim(claim_input(code=first_code))
    second_code = fw.create_enrollment("客厅", "客厅")["pairing_code"]
    second = fw.claim(claim_input(code=second_code))
    assert second["camera_id"] == first["camera_id"]
    assert second["device_token"] != first["device_token"]
    assert callback_camera_ids == []
    assert stopped_camera_ids == [first["camera_id"]]
    assert fw.get_device("omcam-test")["hardware_verified"] is False
    with pytest.raises(HTTPException):
        fw.heartbeat("omcam-test", first["device_token"], heartbeat_input())
    assert fw.heartbeat("omcam-test", second["device_token"], heartbeat_input())["success"]


def test_expired_pairing_code_is_rejected(tmp_path):
    fw = service(tmp_path)
    enrollment = fw.create_enrollment("书房", "书房")
    with fw.connect() as connection:
        connection.execute("UPDATE device_enrollments SET expires_at=?",
                           ("2000-01-01T00:00:00+00:00",))
    with pytest.raises(HTTPException) as error:
        fw.claim(claim_input(code=enrollment["pairing_code"]))
    assert error.value.status_code == 410


def test_pairing_claim_rate_limit_blocks_repeated_guessing(tmp_path):
    fw = service(tmp_path)
    for index in range(8):
        request = claim_input(code=f"OM-BAD{index:03d}")
        with pytest.raises(HTTPException) as error:
            fw.claim(request, "192.168.1.50")
        assert error.value.status_code == 401
    with pytest.raises(HTTPException) as limited:
        fw.claim(claim_input(code="OM-BAD999"), "192.168.1.50")
    assert limited.value.status_code == 429


def test_pairing_claim_is_atomic_under_concurrency(tmp_path):
    fw = service(tmp_path)
    enrollment = fw.create_enrollment("并发测试设备", "测试房间")
    request = claim_input(code=enrollment["pairing_code"])
    outcomes = []
    lock = threading.Lock()

    def attempt():
        try:
            fw.claim(request)
            value = "claimed"
        except HTTPException as exc:
            value = exc.status_code
        with lock:
            outcomes.append(value)

    workers = [threading.Thread(target=attempt) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert sorted(outcomes, key=str) == sorted(["claimed", 409], key=str)


def test_unverified_real_claim_and_heartbeat_never_bind_and_revoke_stops_pending_id(tmp_path):
    starts, stops = [], []
    fw = service(tmp_path, lambda device: starts.append(device) or "cam-1", lambda camera_id: stops.append(camera_id))
    enrollment = fw.create_enrollment("客厅", "客厅")
    result = fw.claim(claim_input(code=enrollment["pairing_code"]))
    assert starts == []
    fw.heartbeat("omcam-test", result["device_token"], heartbeat_input())
    assert starts == []
    fw.heartbeat("omcam-test", result["device_token"], heartbeat_input())
    assert starts == []
    fw.revoke("omcam-test")
    assert stops[-1] == "omcam-test"
    with pytest.raises(HTTPException):
        fw.heartbeat("omcam-test", result["device_token"], heartbeat_input())


def test_matching_usb_serial_omready_is_the_only_real_hardware_promotion(tmp_path, monkeypatch):
    starts = []
    fw = service(tmp_path, lambda device: starts.append(device) or device["camera_id"])
    # Protocol-unit scope only; live listener preflight has separate integration
    # coverage in test_firmware_listener.py and must not be inferred from serial mocks.
    monkeypatch.setattr(fw, "_verify_backend_listener", lambda url: url)
    enrollment = fw.create_enrollment("客厅物理板", "客厅")
    request = claim_input(code=enrollment["pairing_code"])
    claimed = fw.claim(request)
    assert starts == [] and fw.get_device(request.device_id)["hardware_verified"] is False

    ready = json.dumps({
        "device_id": request.device_id,
        "camera_id": claimed["camera_id"],
        "ip": request.ip_address,
        "stream_url": request.stream_url,
        "capture_url": request.capture_url,
    }, separators=(",", ":"))
    serial = FakeProvisionSerial(
        f'OMACK:{{"success":true,"device_id":"{request.device_id}"}}',
        "OMREADY:" + ready,
    )
    probes = []
    monkeypatch.setattr(fw, "validated_port", lambda port: "COM7" if port == "COM7" else None)
    monkeypatch.setattr(fw, "_open_serial", lambda _port, _deadline: serial)
    monkeypatch.setattr(fw, "_schedule_probe", probes.append)
    monkeypatch.setattr("apps.api.app.firmware.time.sleep", lambda _seconds: None)
    result = fw._provision_worker("job-not-persisted", {
        "port": "COM7", "timeout_seconds": 20, "ssid": "Home",
        "password": "password123", "backend_url": "http://192.168.1.2:8018",
        "pairing_code": enrollment["pairing_code"], "device_id": request.device_id,
        "device_name": "客厅物理板", "room_name": "客厅",
    })
    device = fw.get_device(request.device_id)
    assert result["hardware_verification"]["verification_method"] == "usb_serial_omready"
    assert device["hardware_verified"] is True
    assert device["hardware_type"] == "physical"
    assert device["source_type"] == "esp32_real"
    assert device["camera_id"] == claimed["camera_id"]
    assert starts and starts[0]["hardware_verified"] is True
    assert probes == [request.device_id]
    assert serial.closed is True


def test_mismatched_usb_serial_omready_stays_unverified_and_unbound(tmp_path, monkeypatch):
    starts = []
    fw = service(tmp_path, lambda device: starts.append(device) or device["camera_id"])
    monkeypatch.setattr(fw, "_verify_backend_listener", lambda url: url)
    enrollment = fw.create_enrollment("客厅物理板", "客厅")
    request = claim_input(code=enrollment["pairing_code"])
    fw.claim(request)
    serial = FakeProvisionSerial(
        f'OMACK:{{"success":true,"device_id":"{request.device_id}"}}',
        "OMREADY:" + json.dumps({
            "device_id": request.device_id, "camera_id": "cam-attacker",
            "ip": request.ip_address, "stream_url": request.stream_url,
            "capture_url": request.capture_url,
        }, separators=(",", ":")),
    )
    monkeypatch.setattr(fw, "validated_port", lambda _port: "COM7")
    monkeypatch.setattr(fw, "_open_serial", lambda _port, _deadline: serial)
    monkeypatch.setattr("apps.api.app.firmware.time.sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="camera_id"):
        fw._provision_worker("job-not-persisted", {
            "port": "COM7", "timeout_seconds": 20, "ssid": "Home", "password": "",
            "backend_url": "http://192.168.1.2:8018", "pairing_code": enrollment["pairing_code"],
            "device_id": request.device_id, "device_name": "客厅物理板", "room_name": "客厅",
        })
    assert fw.get_device(request.device_id)["hardware_verified"] is False
    assert starts == []


def test_old_non_simulated_device_migrates_to_unverified(tmp_path):
    fw = service(tmp_path)
    enrollment = fw.create_enrollment("旧设备", "客厅")
    request = claim_input(code=enrollment["pairing_code"])
    fw.claim(request)
    with sqlite3.connect(fw.db_path) as connection:
        connection.execute("ALTER TABLE firmware_devices DROP COLUMN serial_verified_at")
        connection.execute("ALTER TABLE firmware_devices DROP COLUMN verification_method")
        connection.execute("ALTER TABLE firmware_devices DROP COLUMN hardware_verified")
    migrated = service(tmp_path)
    device = migrated.get_device(request.device_id)
    assert device["hardware_verified"] is False
    assert device["hardware_type"] == "unverified"
    assert device["source_type"] == "esp32_unverified"
    assert device["camera_id"] is None


def test_real_camera_api_cannot_self_assert_esp32_hardware(tmp_path):
    app = create_app(data_dir=tmp_path / "data", runtime_mode="REAL")
    with TestClient(app) as client:
        client.cookies.set("om_session", app.state.runtime.sessions.issue())
        response = client.post("/api/cameras", json={
            "name": "伪造 ESP32", "room_name": "客厅", "source_type": "esp32",
            "source": "http://127.0.0.1:8766/stream",
            "config": {"device_id": "omcam-fake", "capture_url": "http://127.0.0.1:8766/capture",
                       "hardware_verified": True, "verification_method": "usb_serial_omready"},
        })
        assert response.status_code == 409
        assert app.state.runtime.db.list("cameras") == []

        legacy = app.state.runtime.db.save("cameras", {
            "name": "旧 ESP32", "room_name": "客厅", "source_type": "esp32",
            "source": "http://127.0.0.1:8766/stream",
            "config": {"device_id": "omcam-fake", "capture_url": "http://127.0.0.1:8766/capture",
                       "hardware_verified": True, "verification_method": "usb_serial_omready"},
            "enabled": True, "inference_fps": 5, "save_clips": True, "runtime_mode": "REAL",
        }, "cam-fake")
        displayed = client.get(f'/api/cameras/{legacy["id"]}').json()
        assert displayed["source_type_provenance"] == "esp32_unverified"
        assert displayed["hardware_type"] == "unverified"
        assert displayed["hardware_verified"] is False
        assert displayed["config"]["hardware_verified"] is False
        assert displayed["config"]["verification_method"] is None
        assert client.post(f'/api/cameras/{legacy["id"]}/start').status_code == 409

        # Even a legacy/direct database row cannot surface as a default REAL
        # event while its ESP32 provenance is unverified.
        app.state.runtime.db.save("events", {
            "event_id": "evt-unverified", "item_id": "item-a", "camera_id": legacy["id"],
            "event_type": "movement", "runtime_mode": "REAL", "source_type": "esp32_unverified",
            "is_simulated": False, "evidence_status": "confirmed",
        }, "evt-unverified")
        assert app.state.runtime.db.list("events") == []
        assert len(app.state.runtime.db.list("events", unscoped=True)) == 1


def test_real_network_claim_api_creates_only_unverified_device_not_camera(tmp_path):
    app = create_app(data_dir=tmp_path / "data", runtime_mode="REAL")
    with TestClient(app) as client:
        client.cookies.set("om_session", app.state.runtime.sessions.issue())
        enrollment = client.post("/api/device-enrollment/create", json={
            "device_name": "网络自报设备", "room_name": "客厅",
        })
        assert enrollment.status_code == 200
        request = claim_input(code=enrollment.json()["pairing_code"])
        claimed = client.post("/api/device-enrollment/claim", json=request.model_dump())
        assert claimed.status_code == 200
        assert claimed.json()["source_type"] == "esp32_unverified"
        assert claimed.json()["hardware_verified"] is False
        devices = client.get("/api/devices").json()
        assert len(devices) == 1
        assert devices[0]["hardware_type"] == "unverified"
        assert devices[0]["source_type"] == "esp32_unverified"
        assert devices[0]["camera_id"] is None
        assert client.get("/api/cameras").json() == []


def test_command_is_queued_then_delivered_exactly_once(tmp_path):
    fw = service(tmp_path, lambda _: "cam-1")
    enrollment = fw.create_enrollment("客厅", "客厅")
    result = fw.claim(claim_input(code=enrollment["pairing_code"]))
    queued = fw.queue_command("omcam-test", "reboot")
    assert queued["status"] == "queued"
    first = fw.heartbeat("omcam-test", result["device_token"], heartbeat_input())
    second = fw.heartbeat("omcam-test", result["device_token"], heartbeat_input())
    assert [item["command"] for item in first["commands"]] == ["reboot"]
    assert second["commands"] == []


def test_simulated_device_serves_real_jpeg_and_mjpeg_for_probe(tmp_path):
    simulator_module = load_script("object_memory_simulator", "scripts/simulate-device.py")
    simulator = simulator_module.DeviceSimulator("omcam-test", "明确标注的测试设备", "演示房间", port=0)
    simulator.start()
    try:
        fw = service(tmp_path, lambda _: "cam-simulator", runtime_mode="TEST")
        enrollment = fw.create_enrollment("明确标注的测试设备", "演示房间")
        request = claim_input(base_url=simulator.state.base_url, code=enrollment["pairing_code"])
        request.capabilities.append("simulator")
        result = fw.claim(request)
        fw.heartbeat(request.device_id, result["device_token"], heartbeat_input())
        deadline = time.monotonic() + 8
        device = fw.get_device(request.device_id)
        while time.monotonic() < deadline and device["stream_status"] != "ready":
            time.sleep(0.1)
            device = fw.get_device(request.device_id)
        assert device["simulated"] is True
        assert device["stream_status"] == "ready"
        assert device["current_fps"] and device["current_fps"] > 0
        assert simulator.state.frames >= 2
    finally:
        simulator.close()


def test_split_control_and_stream_ports_follow_firmware_contract(tmp_path):
    """Port 80 controls stay usable while the firmware-style MJPEG runs elsewhere."""
    simulator_module = load_script("object_memory_split_simulator", "scripts/simulate-device.py")
    simulator = simulator_module.DeviceSimulator(
        "omcam-split", "双端口测试设备", "演示房间", port=0, split_stream=True,
    )
    simulator.start()
    try:
        redirect = httpx.get(simulator.state.base_url + "/stream", follow_redirects=False, timeout=2)
        assert redirect.status_code == 307
        assert redirect.headers["location"] == simulator.state.stream_url

        fw = service(tmp_path, lambda _: "cam-split", runtime_mode="TEST")
        enrollment = fw.create_enrollment("双端口测试设备", "演示房间")
        request = claim_input(base_url=simulator.state.base_url,
                              code=enrollment["pairing_code"], device_id="omcam-split")
        request.stream_url = simulator.state.stream_url
        request.capabilities.append("simulator")
        result = fw.claim(request)
        fw.heartbeat(request.device_id, result["device_token"], heartbeat_input())
        deadline = time.monotonic() + 8
        device = fw.get_device(request.device_id)
        while time.monotonic() < deadline and device["stream_status"] != "ready":
            time.sleep(0.1)
            device = fw.get_device(request.device_id)
        assert device["stream_status"] == "ready"
        assert device["current_fps"] and device["current_fps"] > 0
        # A live MJPEG probe must not starve the control server.
        assert httpx.get(simulator.state.base_url + "/health", timeout=2).json()["online"] is True
    finally:
        simulator.close()


def test_build_job_preserves_truthful_subprocess_result(tmp_path, monkeypatch):
    fw = service(tmp_path)
    expected = {"firmware_version": "0.1.0", "artifacts": [{"name": "firmware.bin"}]}
    monkeypatch.setattr(fw, "_builder_process", lambda job_id, **kwargs: (0, expected))
    job = fw.start_build()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = fw.get_job(job["id"])
        if job["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.02)
    assert job["status"] == "succeeded"
    assert job["result"] == expected


def test_lifespan_keeps_mode_lease_until_slow_firmware_worker_finishes(tmp_path, monkeypatch):
    """A timed-out serial/build worker may finish, but never after a replacement app starts."""
    data_root = tmp_path / "shared-data"
    app = create_app(data_dir=data_root, runtime_mode="TEST")
    firmware = app.state.firmware_service
    worker_started = threading.Event()
    allow_worker_to_finish = threading.Event()

    def slow_build(_job_id: str):
        worker_started.set()
        assert allow_worker_to_finish.wait(5), "test did not release the slow firmware worker"
        return {"finished_by": "old-app"}

    monkeypatch.setattr(firmware, "_build_worker", slow_build)
    firmware.shutdown_timeout_seconds = 0.05
    client = TestClient(app)
    client.__enter__()
    try:
        job = firmware.start_build()
        assert worker_started.wait(2)

        with pytest.raises(FirmwareShutdownTimeout, match="仍有固件后台任务"):
            client.__exit__(None, None, None)

        assert app.state.runtime.mode_lease.held is True
        with pytest.raises(RuntimeModeBusy):
            create_app(data_dir=data_root, runtime_mode="TEST")
        with pytest.raises(HTTPException) as stopped:
            firmware.start_build()
        assert stopped.value.status_code == 503
    finally:
        allow_worker_to_finish.set()

    deadline = time.monotonic() + 3
    while app.state.runtime.mode_lease.held and time.monotonic() < deadline:
        time.sleep(0.02)
    assert app.state.runtime.mode_lease.held is False
    assert firmware.get_job(job["id"])["status"] == "succeeded"

    replacement = create_app(data_dir=data_root, runtime_mode="TEST")
    replacement.state.runtime.mode_lease.close()


def test_shutdown_tracks_probe_and_rejects_every_background_entry_point(tmp_path, monkeypatch):
    firmware = service(tmp_path / "real")
    probe_started = threading.Event()
    allow_probe_to_finish = threading.Event()
    shutdown_finished = threading.Event()

    def slow_probe(_device_id: str):
        probe_started.set()
        assert allow_probe_to_finish.wait(3)

    monkeypatch.setattr(firmware, "_probe_device", slow_probe)
    firmware._schedule_probe("device-a")
    assert probe_started.wait(1)

    closer = threading.Thread(
        target=lambda: (firmware.shutdown(timeout_seconds=2), shutdown_finished.set()),
        name="test-firmware-shutdown",
    )
    closer.start()
    try:
        time.sleep(0.05)
        assert shutdown_finished.is_set() is False
    finally:
        allow_probe_to_finish.set()
        closer.join(3)
    assert shutdown_finished.is_set() is True

    provision = ProvisionRequest(
        port="COM7", ssid="Home", password="password123", backend_url="http://192.168.1.2:8018",
        pairing_code="OM-ABC234", device_id="omcam-a", device_name="客厅", room_name="客厅",
    )
    install = InstallRequest(
        port="COM7", ssid="Home", password="password123", backend_url="http://192.168.1.2:8018",
        device_name="客厅", room_name="客厅",
    )
    monkeypatch.setattr(firmware, "validated_port", lambda _port: pytest.fail("closed service touched a serial port"))
    for operation in (
        firmware.start_build,
        lambda: firmware.start_flash("COM7"),
        lambda: firmware.start_provision(provision),
        lambda: firmware.start_install(install),
        lambda: firmware._schedule_probe("device-b"),
    ):
        with pytest.raises(HTTPException) as rejected:
            operation()
        assert rejected.value.status_code == 503

    demo = service(tmp_path / "demo", runtime_mode="DEMO")
    demo.shutdown()
    monkeypatch.setattr("apps.api.app.firmware.subprocess.Popen", lambda *_args, **_kwargs: pytest.fail("closed service started a virtual process"))
    with pytest.raises(HTTPException) as virtual_rejected:
        demo.start_virtual_device(VirtualDeviceStart())
    assert virtual_rejected.value.status_code == 503


def test_shutdown_waits_for_and_closes_the_tracked_virtual_process(tmp_path, monkeypatch):
    class FakeVirtualProcess:
        def __init__(self):
            self.returncode = None
            self.terminated = False
            self.wait_timeouts = []

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            self.wait_timeouts.append(timeout)
            self.returncode = 0
            return 0

        def kill(self):
            self.returncode = -9

    process = FakeVirtualProcess()
    monkeypatch.setattr("apps.api.app.firmware.subprocess.Popen", lambda *_args, **_kwargs: process)
    firmware = service(tmp_path, runtime_mode="DEMO")

    started = firmware.start_virtual_device(VirtualDeviceStart())
    assert started["running"] is True
    result = firmware.shutdown(timeout_seconds=1)

    assert result["shutdown"] is True
    assert process.terminated is True
    assert process.wait_timeouts == [8]
    assert firmware.virtual_process is None
    assert firmware.virtual_log_handle is None


def test_platformio_command_is_an_argument_array():
    builder = load_script("object_memory_firmware_builder", "scripts/firmware-build.py")
    project = Path("C:/Users/Public/ObjectMemoryBuild/source")
    isolated = Path("C:/ObjectMemory/.venv-firmware/Scripts/python.exe")
    command = builder.platformio_command(project, target="upload", port="COM7", interpreter=isolated)
    assert isinstance(command, list)
    prefix = builder.platformio_python_prefix(isolated)
    assert command[:len(prefix)] == prefix
    assert command[len(prefix):len(prefix)+2] == ["run", "--project-dir"]
    assert command[-4:] == ["--target", "upload", "--upload-port", "COM7"]
    assert all(isinstance(argument, str) for argument in command)


def test_platformio_memory_report_is_parsed_from_real_log_shape(tmp_path):
    builder = load_script("object_memory_firmware_usage", "scripts/firmware-build.py")
    log = tmp_path / "build.log"
    log.write_text(
        "RAM:   [==        ]  15.8% (used 51640 bytes from 327680 bytes)\n"
        "Flash: [=====     ]  54.6% (used 1073685 bytes from 1966080 bytes)\n",
        encoding="utf-8",
    )
    usage = builder._memory_usage(log)
    assert usage["ram"] == {"used_bytes": 51640, "total_bytes": 327680, "percent": 15.8}
    assert usage["flash"] == {"used_bytes": 1073685, "total_bytes": 1966080, "percent": 54.6}


def _seed_current_firmware_bundle(repo: Path, payload: bytes, built_at: str = "2026-09-01T00:00:00Z"):
    current=repo/"firmware"/"esp32cam"/"build"
    public=repo/"artifacts"/"firmware"
    (repo/"firmware"/"esp32cam"/"src").mkdir(parents=True,exist_ok=True)
    (repo/"firmware"/"esp32cam"/"platformio.ini").write_text("[env:esp32cam]\n",encoding="utf-8")
    current.mkdir(parents=True,exist_ok=True);public.mkdir(parents=True,exist_ok=True)
    artifact=current/"firmware.bin";artifact.write_bytes(payload)
    digest=hashlib.sha256(payload).hexdigest()
    manifest={"firmware_version":"0.1.0","built_at":built_at,"status":"succeeded","compile_passed":True,"artifacts":[{"name":"firmware.bin","path":str(artifact),"sha256":digest,"size":len(payload)}],"build_log":str(current/"build.log"),"manifest_path":str(public/"manifest.json"),"build_manifest_path":str(current/"manifest.json")}
    (current/"build.log").write_text("prior success\n",encoding="utf-8")
    (current/"manifest.json").write_text(json.dumps(manifest),encoding="utf-8")
    (public/"manifest.json").write_text(json.dumps(manifest),encoding="utf-8")
    return current,public


def _configure_fake_firmware_build(builder,monkeypatch,repo:Path,build_root:Path,payloads:list[bytes],return_code:int=0):
    boot_app = build_root/"platformio-core"/"packages"/"framework-arduinoespressif32"/"tools"/"partitions"/"boot_app0.bin"
    boot_app.parent.mkdir(parents=True, exist_ok=True)
    boot_app.write_bytes(b"contract-test-boot-app-only-not-physical-firmware")
    monkeypatch.setattr(builder,"repository_root",lambda:repo)
    monkeypatch.setattr(builder,"safe_build_root",lambda:build_root)
    monkeypatch.setattr(builder,"firmware_python",lambda _repo=None:repo/"fake-python.exe")
    monkeypatch.setattr(builder,"platformio_command",lambda *args,**kwargs:["platformio","run"])
    monkeypatch.setattr(builder,"_platformio_version",lambda *args:"PlatformIO Core 6.test")
    calls={"count":0}
    def run(_command,cwd,_env,log_handle):
        assert (repo/"artifacts"/"firmware"/".build.lock").is_file()
        log_handle.write("RAM: [=] 1.0% (used 1 bytes from 100 bytes)\n")
        if return_code:
            return return_code
        output=cwd/"firmware"/".pio"/"build"/builder.ENVIRONMENT
        output.mkdir(parents=True,exist_ok=True)
        payload=payloads[min(calls["count"],len(payloads)-1)];calls["count"]+=1
        (output/"firmware.bin").write_bytes(payload)
        return 0
    monkeypatch.setattr(builder,"_run_and_tee",run)


def test_firmware_build_archives_only_previous_success_and_publishes_atomically(tmp_path,monkeypatch):
    builder=load_script("object_memory_atomic_firmware_builder", "scripts/firmware-build.py")
    repo=tmp_path/"repo";current,public=_seed_current_firmware_bundle(repo,b"initial")
    _configure_fake_firmware_build(builder,monkeypatch,repo,tmp_path/"build-root",[b"build-one",b"build-two"])

    first=builder.execute(flash=False,port=None,clean=False)
    assert first["compile_passed"] is True and first["published"] is True
    assert (current/"firmware.bin").read_bytes()==b"build-one"
    assert json.loads((public/"manifest.json").read_text(encoding="utf-8"))==json.loads((current/"manifest.json").read_text(encoding="utf-8"))
    assert len([path for path in (public/"builds").iterdir() if path.is_dir()])==1

    time.sleep(.02)
    builder.execute(flash=False,port=None,clean=False)
    archives=[path for path in (public/"builds").iterdir() if path.is_dir()]
    assert len(archives)==1
    assert (archives[0]/"firmware.bin").read_bytes()==b"build-one"
    assert (current/"firmware.bin").read_bytes()==b"build-two"
    assert not (public/".build.lock").exists()


def test_firmware_build_lock_rejects_concurrent_publisher(tmp_path):
    builder=load_script("object_memory_firmware_build_lock", "scripts/firmware-build.py")
    lock=tmp_path/"artifacts"/"firmware"/".build.lock"
    with builder.ArtifactBuildLock(lock):
        assert lock.is_file()
        with pytest.raises(RuntimeError,match="另一个固件构建"):
            with builder.ArtifactBuildLock(lock):
                pass
        assert lock.is_file()
    assert not lock.exists()


def test_failed_firmware_build_preserves_current_success_and_releases_lock(tmp_path,monkeypatch):
    builder=load_script("object_memory_failed_firmware_builder", "scripts/firmware-build.py")
    repo=tmp_path/"repo";current,public=_seed_current_firmware_bundle(repo,b"known-good")
    before={str(path.relative_to(repo)):path.read_bytes() for path in repo.rglob("*") if path.is_file()}
    _configure_fake_firmware_build(builder,monkeypatch,repo,tmp_path/"build-root",[b"unused"],return_code=7)
    with pytest.raises(RuntimeError,match="exit code 7"):
        builder.execute(flash=False,port=None,clean=False)
    after={str(path.relative_to(repo)):path.read_bytes() for path in repo.rglob("*") if path.is_file()}
    assert after==before
    assert (current/"firmware.bin").read_bytes()==b"known-good"
    assert (public/"manifest.json").is_file()
    assert not (public/".build.lock").exists()


def test_failed_firmware_publish_rolls_back_current_and_public_manifest(tmp_path,monkeypatch):
    builder=load_script("object_memory_publish_rollback_builder", "scripts/firmware-build.py")
    repo=tmp_path/"repo";current,public=_seed_current_firmware_bundle(repo,b"known-good")
    old_current=(current/"manifest.json").read_bytes();old_public=(public/"manifest.json").read_bytes()
    _configure_fake_firmware_build(builder,monkeypatch,repo,tmp_path/"build-root",[b"candidate"])
    atomic_write=builder._write_json_atomic
    def fail_public(path,value):
        if path==public/"manifest.json" and value.get("published") is True:
            raise OSError("simulated public manifest replace failure")
        return atomic_write(path,value)
    monkeypatch.setattr(builder,"_write_json_atomic",fail_public)
    with pytest.raises(OSError,match="simulated public manifest"):
        builder.execute(flash=False,port=None,clean=False)
    assert (current/"firmware.bin").read_bytes()==b"known-good"
    assert (current/"manifest.json").read_bytes()==old_current
    assert (public/"manifest.json").read_bytes()==old_public
    assert not list((repo/"firmware"/"esp32cam").glob(".build-rollback-*"))
    assert not (public/".build.lock").exists()


def test_firmware_manifest_rehashes_artifacts_and_does_not_infer_board_from_usb_bridge(tmp_path, monkeypatch):
    fw = service(tmp_path)
    fw.project_root = tmp_path / "repo"
    source = fw.project_root / "firmware" / "esp32cam" / "src"
    build = fw.project_root / "firmware" / "esp32cam" / "build"
    public = fw.project_root / "artifacts" / "firmware"
    source.mkdir(parents=True)
    build.mkdir(parents=True)
    public.mkdir(parents=True)
    (source / "main.cpp").write_text("void setup(){}", encoding="utf-8")
    artifact = build / "firmware.bin"
    artifact.write_bytes(b"actual-platformio-output")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (public / "manifest.json").write_text(json.dumps({
        "firmware_version": "0.1.0",
        "built_at": "2026-09-04T00:00:00Z",
        "artifacts": [{"name": "firmware.bin", "path": "untrusted/path", "sha256": digest,
                       "size": artifact.stat().st_size}],
    }), encoding="utf-8")
    monkeypatch.setattr(fw, "discover_ports", lambda: [{
        "device": "COM7", "eligible": True, "vid": 0x1A86, "pid": 0x7523,
    }])
    result = fw.firmware_manifest()
    assert result["compile_passed"] is True
    assert result["manifest_hashes_verified"] is True
    assert result["physical_usb_serial_detected"] is True
    assert result["suspected_esp32_usb_serial"] is True
    assert result["physical_board_detected"] is False
    assert result["physical_flash_performed"] is False
    assert result["serial_provision_performed"] is False
    assert result["real_video_verified"] is False
    assert "常见 USB 串口桥" in result["physical_status_note"]

    manifest = json.loads((public / "manifest.json").read_text(encoding="utf-8"))
    manifest.update({
        "physical_flash_performed": True,
        "physical_flash_evidence": {"port": "COM7", "completed_at": "2026-09-04T00:01:00Z"},
        "physical_board_detected": True,
        "physical_board_evidence": {"chip": "ESP32-D0WD"},
    })
    (public / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    confirmed = fw.firmware_manifest()
    assert confirmed["physical_board_detected"] is True
    assert confirmed["physical_flash_performed"] is True
    assert "带证据的物理板识别与真机刷写记录" in confirmed["physical_status_note"]


def test_virtual_identity_is_stable_unique_and_explicitly_labelled():
    simulator_module = load_script("object_memory_virtual_identity", "scripts/simulate-device.py")
    from tools.virtual_esp32cam.device import _virtual_mac
    first = simulator_module.DeviceSimulator("omcam-first", "虚拟一", "客厅", port=0)
    assert first.state.mac_address == _virtual_mac("omcam-first")
    assert first.state.mac_address != _virtual_mac("omcam-second")
    first.start()
    try:
        health = httpx.get(first.state.base_url + "/health", timeout=3).json()
        descriptor = httpx.get(first.state.base_url + "/device", timeout=3).json()
        assert health["simulated"] is True and health["hardware_type"] == "virtual"
        assert descriptor["simulated"] is True and descriptor["video_source"] == "video"
        jpeg = httpx.get(first.state.base_url + "/capture", timeout=3).content
        assert jpeg.startswith(b"\xff\xd8") and len(jpeg) > 1000
    finally:
        first.close()
