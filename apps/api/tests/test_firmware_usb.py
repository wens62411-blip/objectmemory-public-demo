"""USB safety contracts with fake ROM/ports, real isolated SQLite; NOT hardware acceptance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from apps.api.app.firmware import AutoUsbBindingRequest, FirmwareService
from apps.api.app.firmware_usb import usb_helper
from apps.api.app.main import create_app

ROOT = Path(__file__).resolve().parents[3]
MAC = "02:00:00:00:00:01"
BRIDGE = {"vid": 0x1a86, "pid": 0x7523, "serial_number": None, "location": "1-2"}
PORT = {**BRIDGE, "device": "COM7", "eligible": True}


def create_bundle(root):
    folder = root / "firmware/esp32cam/build"
    public = root / "artifacts/firmware"
    folder.mkdir(parents=True); public.mkdir(parents=True)
    records = []
    helper = usb_helper(ROOT)
    entries = [(1, 2, 0x9000, 0x5000), (1, 0, 0xe000, 0x2000),
               (0, 0x10, 0x10000, 0x1e0000), (0, 0x11, 0x1f0000, 0x1e0000),
               (1, 0x82, 0x3d0000, 0x20000), (1, 3, 0x3f0000, 0x10000)]
    for name in helper.OFFSETS:
        data = b"contract-only-not-flashable-firmware"
        if name in {"bootloader.bin", "firmware.bin"}:
            header = bytearray(24)
            header[0], header[1] = 0xe9, 1
            header[3] = 0x20  # Fixed 4 MB header, matching original board.
            struct.pack_into("<H", header, 12, 0)  # Original ESP32 image header.
            data = bytes(header) + data
        if name == "partitions.bin":
            data = b"".join(struct.pack("<HBBII16sI", 0x50aa, *entry, b"contract", 0) for entry in entries)
        (folder / name).write_bytes(data)
        records.append({"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    manifest = {"compile_passed": True, "environment": "esp32cam", "target_board": "AI Thinker ESP32-CAM (esp32cam)",
                "firmware_version": "contract-test", "artifacts": records}
    (public / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return helper.bundle(root)[0]


def test_fixed_bundle_rejects_changed_hash_incomplete_and_duplicate(tmp_path):
    digest = create_bundle(tmp_path)
    helper = usb_helper(ROOT)
    assert helper.bundle(tmp_path, digest)[0] == digest
    path = tmp_path / "artifacts/firmware/manifest.json"
    original = path.read_text()
    path.write_text(original + " ")
    with pytest.raises(ValueError, match="changed"):
        helper.bundle(tmp_path, digest)
    path.write_text(original)
    data = json.loads(original); data["artifacts"].append(data["artifacts"][0])
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="exactly one"):
        helper.bundle(tmp_path)
    path.write_text(original)
    (tmp_path / "firmware/esp32cam/build/boot_app0.bin").unlink()
    with pytest.raises(FileNotFoundError): helper.bundle(tmp_path)


def test_artifact_size_partition_and_hash_fail_closed(tmp_path):
    create_bundle(tmp_path)
    helper = usb_helper(ROOT)
    binary = tmp_path / "firmware/esp32cam/build/firmware.bin"
    binary.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="size|hash"):
        helper.bundle(tmp_path)
    assert helper.SIZE_LIMITS["bootloader.bin"] + int(helper.OFFSETS["bootloader.bin"], 16) == 0x8000
    assert helper.SIZE_LIMITS["firmware.bin"] + 0x10000 == 0x1f0000


def test_cheap_ch340_location_supported_but_identity_unknown_multiple_rejected():
    helper = usb_helper(ROOT)
    assert helper.bridge_identity(PORT) == BRIDGE
    assert helper.matching_port([PORT], BRIDGE) == "COM7"
    assert helper.matching_port([{**PORT, "device": "COM9"}], BRIDGE) == "COM9"
    for records in ([], [PORT, {**PORT, "device": "COM8"}], [{**PORT, "location": "1-3"}],
                    [{**PORT, "eligible": False}], [{**PORT, "location": None}]):
        with pytest.raises(ValueError): helper.matching_port(records, BRIDGE)
    serial_record = {**PORT, "serial_number": "bridge-only-1"}
    assert helper.bridge_identity(serial_record)["location"] is None


class FakeRom:
    CHIP_NAME = "ESP32"
    secure_download_mode = False
    def __init__(self): self.closed = False; self.reset = False; self._port = SimpleNamespace(close=self.close)
    def close(self): self.closed = True
    def read_mac(self): return bytes.fromhex(MAC.replace(":", ""))
    def get_secure_boot_enabled(self): return False
    def get_flash_encryption_enabled(self): return False
    def get_chip_spi_pads(self): return (0, 0, 0, 0, 0)
    def flash_spi_attach(self, value): assert value == 0
    def flash_id(self): return 0x1640ef
    def hard_reset(self): self.reset = True


@pytest.mark.parametrize("changed", ["mac", "chip", "secure", "flash_size", "wiring"])
def test_wrong_rom_never_writes_or_erases_and_always_closes(tmp_path, changed):
    helper = usb_helper(ROOT); rom = FakeRom(); writes = []
    if changed == "chip": rom.CHIP_NAME = "ESP32-S3"
    if changed == "secure": rom.secure_download_mode = True
    if changed == "flash_size": rom.flash_id = lambda: 0x1540ef
    if changed == "wiring": rom.get_chip_spi_pads = lambda: (1, 2, 3, 4, 5)
    tool = SimpleNamespace(detect_chip=lambda **kwargs: rom, main=lambda *args, **kwargs: writes.append(args))
    with pytest.raises(ValueError):
        helper.connected_operation(tool, "COM7", "02:00:00:00:00:ff" if changed == "mac" else MAC,
                                   {name: b"test" for name in helper.OFFSETS}, tmp_path)
    assert writes == [] and rom.closed
    assert list(tmp_path.iterdir()) == []


def test_rom_handle_is_reused_for_exact_fixed_offsets(tmp_path):
    helper = usb_helper(ROOT); rom = FakeRom(); calls = []
    def write(arguments, esp):
        assert esp is rom
        assert "--force" not in arguments and "erase_flash" not in arguments
        assert arguments.count("write_flash") == 1
        for name, offset in helper.OFFSETS.items():
            index = arguments.index(offset)
            assert Path(arguments[index + 1]).read_bytes() == b"contract"
        calls.append(arguments)
    tool = SimpleNamespace(detect_chip=lambda **kwargs: rom, main=write)
    result = helper.connected_operation(tool, "COM7", MAC, {name: b"contract" for name in helper.OFFSETS}, tmp_path)
    assert result["physical_flash_performed"] and len(calls) == 1 and rom.closed


@pytest.fixture
def auto(tmp_path, monkeypatch):
    service = FirmwareService(tmp_path / "isolated.sqlite", None, tmp_path / "data", runtime_mode="REAL")
    controller = service.auto_usb
    monkeypatch.setattr(controller, "_start_listener", lambda: None)
    monkeypatch.setattr(service, "_verify_backend_listener", lambda value: value)
    monkeypatch.setattr(service, "discover_ports", lambda: [dict(PORT)])
    import apps.api.app.firmware_usb as module
    helper = usb_helper(ROOT)
    monkeypatch.setattr(helper, "bundle", lambda *args: ("a" * 64, {"firmware_version": "test"}, {}))
    monkeypatch.setattr(module, "usb_helper", lambda root: helper)
    yield service, controller
    service.shutdown()


def binding():
    return {"enabled": True, "board_model": "ai_thinker_esp32cam", "bridge": BRIDGE, "mac_address": MAC,
            "manifest_sha256": "a" * 64, "firmware_version": "test", "backend_url": "http://192.168.1.20:8018",
            "device_id": "omcam-020000000001", "device_name": "Contract device", "room_name": "Contract room"}


def test_default_closed_and_unknown_bridge_never_opens(auto, monkeypatch):
    service, controller = auto; tools = []
    monkeypatch.setattr(controller, "_tool", lambda payload: tools.append(payload))
    controller.tick()
    assert not controller.status()["enabled"] and tools == []
    assert controller.status()["physical_source_verified"] is False
    assert controller.status()["source_type"] == "esp32_unverified"
    controller._save(binding()); controller.credentials = {"ssid": "Test", "password": "secret123"}
    monkeypatch.setattr(service, "discover_ports", lambda: [{**PORT, "location": "unbound-socket"}])
    controller.tick()
    assert tools == [] and controller._receipt(binding()) is None


def test_tick_reserves_once_in_sqlite_before_background_job_and_does_not_store_password(auto, monkeypatch):
    service, controller = auto; jobs = []
    controller._save(binding()); controller.credentials = {"ssid": "Test", "password": "secret123"}
    def job(kind, worker, *args):
        assert controller._receipt(binding())["status"] == "reserved"
        jobs.append((kind, args)); return {"id": "test-job"}
    monkeypatch.setattr(service, "_create_job", job)
    for _ in range(5): controller.tick()
    assert len(jobs) == 1
    controller.seen = False; controller.tick()
    assert len(jobs) == 1
    with service.connect() as connection:
        contents = "".join(str(tuple(row)) for row in connection.execute("SELECT * FROM firmware_usb_binding"))
        contents += "".join(str(tuple(row)) for row in connection.execute("SELECT * FROM firmware_usb_receipts"))
    assert "secret123" not in contents and "Test" not in contents


def test_successful_same_version_replug_skips_flash(auto, monkeypatch):
    service, controller = auto; jobs = []
    controller._save(binding())
    with service.connect() as connection:
        connection.execute("INSERT INTO firmware_usb_receipts(mac,manifest_sha256,status,flashed_at) VALUES(?,?,'linked','actual-tool-time-in-contract')", (MAC, "a" * 64))
    monkeypatch.setattr(service, "_create_job", lambda kind, worker, *args: jobs.append(args) or {"id": "resume-job"})
    controller.tick()
    assert len(jobs) == 1 and jobs[0][-1] is True
    assert jobs[0][-2] == {}


def test_restart_pending_binding_needs_credentials_and_no_flash(auto, monkeypatch):
    service, controller = auto; calls = []
    controller._save(binding())
    monkeypatch.setattr(service, "_create_job", lambda *args: calls.append(args))
    controller.tick()
    assert calls == [] and "重新绑定" in controller.notice


def test_disable_clears_credentials_and_future_work(auto, monkeypatch):
    _, controller = auto
    controller._save(binding()); controller.credentials = {"password": "secret"}
    controller.disable()
    assert not controller.status()["enabled"] and controller.credentials is None
    controller.tick()
    assert controller._receipt(binding()) is None


def test_first_explicit_binding_starts_without_another_unplug_and_saves_no_wifi(auto, monkeypatch):
    service, controller = auto; jobs = []
    request = AutoUsbBindingRequest(port="COM7", ssid="contract-wifi", password="secret123", backend_url="http://192.168.1.20:8018",
                                   device_name="Contract board", room_name="Contract room", board_model="ai_thinker_esp32cam", authorize_fixed_firmware=True)
    monkeypatch.setattr(controller, "_tool", lambda payload: {"chip": "ESP32", "mac_address": MAC, "physical_flash_performed": False})
    result = controller._bind_worker("contract-job", request.model_dump(), BRIDGE)
    assert result["physical_flash_performed"] is False and controller.seen is False
    assert "secret123" not in json.dumps(controller._binding())
    monkeypatch.setattr(service, "_create_job", lambda *args: jobs.append(args) or {"id": "auto-first"})
    controller.tick()
    assert len(jobs) == 1 and jobs[0][0] == "usb_auto"


@pytest.mark.parametrize("consent", [False, 1, "true", None])
def test_flash_authorization_requires_literal_boolean_true(consent):
    with pytest.raises(ValueError):
        AutoUsbBindingRequest(port="COM7", ssid="Test", backend_url="http://192.168.1.20:8018", device_name="Test", room_name="Test",
                              board_model="ai_thinker_esp32cam", authorize_fixed_firmware=consent)


def test_flash_failure_is_persisted_and_does_not_retry(auto, monkeypatch):
    service, controller = auto
    controller._save(binding())
    with service.connect() as connection:
        connection.execute("INSERT INTO firmware_usb_receipts(mac,manifest_sha256,status) VALUES(?,?,'reserved')", (MAC, "a" * 64))
    attempts = []
    def fail(payload): attempts.append(payload); raise RuntimeError("test tool failure")
    monkeypatch.setattr(controller, "_tool", fail)
    with pytest.raises(RuntimeError, match="test tool"):
        controller._install_worker("job", binding(), "COM7", {"ssid": "test"}, False)
    assert len(attempts) == 1 and controller._receipt(binding())["status"] == "failed"
    controller.tick()
    assert len(attempts) == 1


def test_controller_full_contract_links_only_after_rom_flash_and_serial_ready(auto, monkeypatch):
    service, controller = auto; calls = []
    controller._save(binding())
    with service.connect() as connection:
        connection.execute("INSERT INTO firmware_usb_receipts(mac,manifest_sha256,status) VALUES(?,?,'reserved')", (MAC, "a" * 64))
    def tool(payload):
        calls.append(payload["operation"])
        return {"chip": "ESP32", "mac_address": MAC, "physical_flash_performed": payload["operation"] == "flash", "manifest_sha256": "a" * 64}
    monkeypatch.setattr(controller, "_tool", tool)
    monkeypatch.setattr(service, "get_device", lambda *args, **kwargs: {"mac_address": MAC, "simulated": False})
    def ready(*args):
        assert controller._receipt(binding())["flashed_at"]
        calls.append("serial-ready")
        return {"hardware_verification": "fake-serial-for-contract-only"}
    monkeypatch.setattr(controller, "_read_ready", ready)
    controller._install_worker("job", binding(), "COM7", {}, False)
    assert calls == ["flash", "serial-ready"]
    assert controller._receipt(binding())["status"] == "linked"
    controller._install_worker("job2", binding(), "COM7", {}, True)
    assert calls == ["flash", "serial-ready", "identify", "serial-ready"]


def test_network_mac_mismatch_never_promoted_to_physical(auto, monkeypatch):
    service, _ = auto
    monkeypatch.setattr(service, "get_device", lambda *args, **kwargs: {"simulated": False, "mac_address": "02:00:00:00:00:ff"})
    with pytest.raises(RuntimeError, match="MAC"):
        service._verify_serial_ready_and_bind("COM7", {"device_id": "omcam-test", "verified_chip_mac": MAC}, {"device_id": "omcam-test"})
    with service.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM firmware_devices WHERE hardware_verified=1").fetchone()[0] == 0


def test_real_listener_is_registered_and_joined_without_touching_ports(tmp_path, monkeypatch):
    service = FirmwareService(tmp_path / "isolated.sqlite", None, tmp_path / "data")
    touched = []
    monkeypatch.setattr(service, "discover_ports", lambda: touched.append(True) or [])
    service.auto_usb._start_listener()
    worker = service.auto_usb.worker
    assert worker.is_alive() and worker in service._workers
    service.shutdown(timeout_seconds=2)
    assert not worker.is_alive() and touched == [] and service.auto_usb.stop.is_set()


def test_overview_uses_only_matching_fixed_bundle_receipt_not_just_job_success(tmp_path, monkeypatch):
    digest = create_bundle(tmp_path / "repo")
    service = FirmwareService(tmp_path / "isolated.sqlite", None, tmp_path / "data")
    service.project_root = tmp_path / "repo"
    monkeypatch.setattr(service, "discover_ports", lambda: [])
    bound = {**binding(), "manifest_sha256": digest}
    service.auto_usb._save(bound)
    try:
        assert service.firmware_manifest()["physical_flash_performed"] is False
        with service.connect() as connection:
            connection.execute("INSERT INTO firmware_usb_receipts(mac,manifest_sha256,status,flashed_at) VALUES(?,?,'flashed','contract-time')", (MAC, digest))
        status = service.firmware_manifest()
        assert status["physical_flash_performed"] is True
        assert status["bound_usb_flash_evidence"]["manifest_sha256"] == digest
        assert status["physical_board_detected"] is False  # no currently connected board proof
        assert service.auto_usb.status()["source_type"] == "esp32_unverified"  # flash is not serial/video attestation
        path = tmp_path / "repo/artifacts/firmware/manifest.json"
        path.write_text(path.read_text() + " ")
        assert service.firmware_manifest()["physical_flash_performed"] is False
    finally:
        service.shutdown()


def test_device_bearer_token_cannot_enable_disable_or_query_admin_usb(tmp_path):
    with TestClient(create_app(data_dir=tmp_path / "test-data", testing=True)) as client:
        client.cookies.clear()
        for method, path in [("get", "/api/firmware/auto-usb"), ("post", "/api/firmware/auto-usb/bind"), ("post", "/api/firmware/auto-usb/disable")]:
            response = getattr(client, method)(path, headers={"Authorization": "Bearer device-token-not-admin"})
            assert response.status_code == 401


def test_real_fastapi_test_mode_routes_reject_physical_actions(tmp_path):
    with TestClient(create_app(data_dir=tmp_path / "test-data", testing=True)) as client:
        client.get("/api/session")
        status = client.get("/api/firmware/auto-usb")
        assert status.status_code == 200 and status.json()["runtime_mode"] == "TEST"
        assert status.json()["is_simulated"] is True and status.json()["source_type"] == "esp32_unverified"
        assert status.json()["enabled"] is False
        assert client.post("/api/firmware/auto-usb/disable").status_code == 409


def test_real_cli_help_and_invalid_input_never_access_hardware():
    script = ROOT / "scripts/firmware-usb.py"
    result = subprocess.run([sys.executable, "-B", str(script), "--help"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and "never port autodetection" in result.stdout
    result = subprocess.run([sys.executable, "-B", str(script)], input='{"operation":"erase_flash"}', capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and "Invalid USB operation" in result.stderr


class ChallengeSerial:
    """Protocol fake only; never opens a physical serial port."""
    def __init__(self, identity="valid", disconnect_after_config=False):
        self.identity = identity
        self.disconnect_after_config = disconnect_after_config
        self.writes = []
        self.lines = []
        self.closed = False
        self.reset_count = 0

    def reset_input_buffer(self):
        self.reset_count += 1
        self.lines.clear()

    def write(self, data):
        self.writes.append(data)
        if data.startswith(b"OMWHO:"):
            nonce = data.decode().strip().split(":", 1)[1]
            if self.identity == "timeout": return
            response = {"nonce": nonce, "mac_address": MAC, "protocol": 1}
            if self.identity == "wrong_mac": response["mac_address"] = "02:00:00:00:00:ff"
            if self.identity == "old_nonce": response["nonce"] = "old-boot-nonce"
            if self.identity == "missing_mac": response.pop("mac_address")
            if self.identity == "wrong_protocol": response["protocol"] = True
            self.lines.append(b"OMIDENT:" + json.dumps(response).encode() + b"\n")
            if self.identity == "malformed": self.lines[-1] = b"OMIDENT:not-json\n"
            if self.identity == "oversized": self.lines[-1] = b"OMIDENT:" + b"x" * 2050
        if data.startswith(b"OMCFG:"):
            if self.disconnect_after_config:
                self.lines.append(OSError("contract disconnect"))
                return
            payload = json.loads(data[6:])
            self.lines.extend([
                ('OMACK:' + json.dumps({"success": True, "device_id": payload["device_id"]})).encode(),
                ('OMREADY:' + json.dumps({"device_id": payload["device_id"], "camera_id": "contract-camera",
                 "ip": "192.168.1.8", "stream_url": "http://192.168.1.8/stream",
                 "capture_url": "http://192.168.1.8/capture"})).encode(),
            ])

    def readline(self, _limit):
        result = self.lines.pop(0) if self.lines else b""
        if isinstance(result, Exception): raise result
        return result

    def flush(self): pass
    def close(self): self.closed = True


def challenge_payload():
    return {"port": "COM7", "timeout_seconds": 45, "ssid": "contract-private-ssid",
            "password": "contract-password-never-send-to-wrong-board", "backend_url": "http://192.168.1.20:8018",
            "pairing_code": "OM-CONTRACT", "device_id": "omcam-test", "device_name": "Contract", "room_name": "Contract"}


def serial_contract_setup(auto, monkeypatch, connections):
    import itertools
    import serial
    service, _ = auto
    clock = itertools.count(0, .1)
    monkeypatch.setattr("apps.api.app.firmware.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("apps.api.app.firmware.time.sleep", lambda seconds: None)
    monkeypatch.setattr(service, "validated_port", lambda port: port)
    pending = iter(connections)
    opened = []
    def open_port(**kwargs):
        assert kwargs["port"] == "COM7"
        connection = next(pending); opened.append(connection)
        return connection
    monkeypatch.setattr(serial, "Serial", open_port)
    monkeypatch.setattr(service, "_verify_serial_ready_and_bind", lambda *args: {"contract_only": True})
    return service, opened


@pytest.mark.parametrize("fault", ["wrong_mac", "old_nonce", "missing_mac", "timeout", "wrong_protocol", "malformed", "oversized"])
def test_auto_provision_never_sends_any_configuration_before_fresh_mac_challenge(auto, monkeypatch, fault):
    connection = ChallengeSerial(fault)
    service, opened = serial_contract_setup(auto, monkeypatch, [connection])
    with pytest.raises(RuntimeError):
        service._provision_worker("contract-job", challenge_payload(), expected_mac=MAC, expected_bridge=BRIDGE)
    assert opened == [connection] and connection.closed
    assert connection.reset_count == 1 and connection.writes
    assert all(line.startswith(b"OMWHO:") for line in connection.writes)
    assert b"contract-password" not in b"".join(connection.writes)


def test_bound_bridge_change_or_missing_identity_never_opens_port(auto, monkeypatch):
    service, opened = serial_contract_setup(auto, monkeypatch, [])
    with pytest.raises(RuntimeError, match="身份"):
        service._provision_worker("contract", challenge_payload(), expected_mac=MAC)
    monkeypatch.setattr(service, "discover_ports", lambda: [{**PORT, "location": "other-socket"}])
    with pytest.raises(ValueError, match="changed"):
        service._provision_worker("contract", challenge_payload(), expected_mac=MAC, expected_bridge=BRIDGE)
    assert opened == []


def test_bridge_changed_during_challenge_never_gets_configuration(auto, monkeypatch):
    connection = ChallengeSerial()
    service, _ = serial_contract_setup(auto, monkeypatch, [connection])
    monkeypatch.setattr(service, "discover_ports", lambda: [dict(PORT)] if not connection.writes else [{**PORT, "location": "other"}])
    with pytest.raises(ValueError, match="changed"):
        service._provision_worker("contract", challenge_payload(), expected_mac=MAC, expected_bridge=BRIDGE)
    assert connection.closed and all(line.startswith(b"OMWHO:") for line in connection.writes)


def test_shutdown_during_challenge_closes_without_credentials(auto, monkeypatch):
    connection = ChallengeSerial()
    service, opened = serial_contract_setup(auto, monkeypatch, [connection])
    def sleep(_seconds): service.auto_usb.stop.set()
    monkeypatch.setattr("apps.api.app.firmware.time.sleep", sleep)
    with pytest.raises(RuntimeError, match="退出"):
        service._provision_worker("contract", challenge_payload(), expected_mac=MAC, expected_bridge=BRIDGE)
    assert opened == [connection] and connection.closed and connection.writes == []


def test_serial_control_line_failure_closes_partial_handle(auto, monkeypatch):
    import serial
    connection = ChallengeSerial()
    service, _ = serial_contract_setup(auto, monkeypatch, [])
    class ControlFailure:
        @property
        def dtr(self): return False
        @dtr.setter
        def dtr(self, _value): raise OSError("contract DTR failure")
        def close(self): connection.close()
    monkeypatch.setattr(serial, "Serial", lambda **kwargs: ControlFailure())
    with pytest.raises(RuntimeError, match="无法打开串口"):
        service._open_serial("COM7", .5, expected_bridge=BRIDGE)
    assert connection.closed


@pytest.mark.parametrize("second_identity", ["valid", "wrong_mac", "old_nonce", "timeout"])
def test_reconnected_application_must_pass_new_challenge_before_any_new_credentials(auto, monkeypatch, second_identity):
    first = ChallengeSerial(disconnect_after_config=True)
    second = ChallengeSerial(second_identity)
    service, opened = serial_contract_setup(auto, monkeypatch, [first, second])
    if second_identity == "valid":
        result = service._provision_worker("contract", challenge_payload(), expected_mac=MAC, expected_bridge=BRIDGE)
        assert result["hardware_verification"]["contract_only"] is True
        assert any(line.startswith(b"OMCFG:") for line in second.writes)
    else:
        with pytest.raises(RuntimeError):
            service._provision_worker("contract", challenge_payload(), expected_mac=MAC, expected_bridge=BRIDGE)
        assert all(line.startswith(b"OMWHO:") for line in second.writes)
    assert opened == [first, second] and first.closed and second.closed
    assert first.writes[0].startswith(b"OMWHO:") and second.writes[0].startswith(b"OMWHO:")
    assert first.writes[0] != second.writes[0]  # Replay from the former handle cannot unlock this one.
    assert any(line.startswith(b"OMCFG:") for line in first.writes)


def test_new_authorized_wifi_reprovisions_existing_database_device_through_mac_guard(auto, monkeypatch):
    connection = ChallengeSerial()
    service, opened = serial_contract_setup(auto, monkeypatch, [connection])
    controller = service.auto_usb
    controller._save(binding())
    with service.connect() as db:
        db.execute("INSERT INTO firmware_usb_receipts(mac,manifest_sha256,status,flashed_at) VALUES(?,?,'linked','contract-time')", (MAC, "a" * 64))
    monkeypatch.setattr(controller, "_tool", lambda payload: {"chip": "ESP32", "mac_address": MAC, "physical_flash_performed": False})
    monkeypatch.setattr(service, "get_device", lambda *args, **kwargs: {"mac_address": MAC, "simulated": False})
    monkeypatch.setattr(controller, "_read_ready", lambda *args: pytest.fail("New credentials must not be ignored because of an old database row"))
    credentials = {"ssid": "newly-authorized-network", "password": "newly-authorized-password"}
    result = controller._install_worker("contract", binding(), "COM7", credentials, True)
    assert result["same_version_skipped_flash"] is True and opened == [connection]
    assert connection.closed and connection.writes[0].startswith(b"OMWHO:")
    configurations = [json.loads(line[6:]) for line in connection.writes if line.startswith(b"OMCFG:")]
    assert len(configurations) == 1 and configurations[0]["ssid"] == credentials["ssid"]
    assert configurations[0]["password"] == credentials["password"]
    with service.connect() as db:
        enrollment = db.execute("SELECT * FROM device_enrollments").fetchall()
        assert len(enrollment) == 1  # A fresh administrator enrollment, not a stale pairing code.
    assert configurations[0]["pairing_code"].startswith("OM-")


def test_credential_free_existing_board_replug_reads_ready_without_reenrollment(auto, monkeypatch):
    service, controller = auto
    controller._save(binding())
    with service.connect() as db:
        db.execute("INSERT INTO firmware_usb_receipts(mac,manifest_sha256,status,flashed_at) VALUES(?,?,'linked','contract-time')", (MAC, "a" * 64))
    operations = []
    monkeypatch.setattr(controller, "_tool", lambda payload: operations.append(payload["operation"]) or {"chip": "ESP32", "mac_address": MAC, "physical_flash_performed": False})
    monkeypatch.setattr(service, "get_device", lambda *args, **kwargs: {"mac_address": MAC, "simulated": False})
    monkeypatch.setattr(controller, "_read_ready", lambda *args: operations.append("read_ready") or {"contract_only": True})
    monkeypatch.setattr(service, "_provision_worker", lambda *args, **kwargs: pytest.fail("Replug must not write new Wi-Fi configuration"))
    result = controller._install_worker("contract", binding(), "COM7", {}, True)
    assert operations == ["identify", "read_ready"] and result["same_version_skipped_flash"] is True
    with service.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM device_enrollments").fetchone()[0] == 0
