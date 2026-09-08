"""XIAO routing/identity contracts. No physical serial reads or flash writes."""
from __future__ import annotations

import hashlib
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from apps.api.app.firmware import AutoUsbBindingRequest, FirmwareService, FIRMWARE_BOARDS
from apps.api.app.main import create_app

MODEL = "xiao_esp32s3_sense"
MAC = "02:00:00:00:00:01"
BRIDGE = {"vid": 0x303A, "pid": 0x1001, "serial_number": "contract-device", "location": None}
PORT = {**BRIDGE, "eligible": True, "device": "COM7"}


@pytest.fixture
def fw(tmp_path, monkeypatch):
    service = FirmwareService(tmp_path / "test.sqlite", None, tmp_path / "data", runtime_mode="REAL")
    service.project_root = tmp_path / "repo"
    monkeypatch.setattr(service, "discover_ports", lambda: [PORT])
    monkeypatch.setattr(service, "_verify_backend_listener", lambda value: value)
    monkeypatch.setattr(service.auto_usb, "_start_listener", lambda: None)
    try:
        yield service
    finally:
        service.shutdown()


def write_status_bundle(service, model):
    board = FIRMWARE_BOARDS[model]
    artifact = service.project_root / board["build"] / "firmware.bin"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(model.encode())
    manifest = service.project_root / board["manifest"]
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"board_model": model, "environment": board["environment"],
        "artifacts": [{"name": artifact.name, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}]}), encoding="utf-8")
    return artifact, manifest


def bound():
    return {"enabled": True, "board_model": MODEL, "mac_address": MAC, "bridge": BRIDGE,
        "manifest_sha256": "a" * 64, "firmware_version": "contract-only",
        "backend_url": "http://192.168.1.20:8018", "device_id": "omcam-020000000001",
        "device_name": "Contract XIAO", "room_name": "Contract room"}


def helper_contract(monkeypatch):
    import apps.api.app.firmware_usb as module
    calls = []
    helper = SimpleNamespace(bundle=lambda *args, **kwargs: calls.append(kwargs.get("board_model")) or
        ("a" * 64, {"firmware_version": "contract-only"}, {}),
        matching_port=lambda records, bridge: "COM7", bridge_identity=lambda record: BRIDGE)
    monkeypatch.setattr(module, "usb_helper", lambda root: helper)
    return calls


def test_status_hashes_selected_board_only_and_rejects_cross_bundle(fw):
    ai, _ = write_status_bundle(fw, "ai_thinker_esp32cam")
    xiao, manifest = write_status_bundle(fw, MODEL)
    assert fw.firmware_manifest()["artifacts"][0]["path"] == str(ai)
    result = fw.firmware_manifest(MODEL)
    assert result["compile_passed"] and result["artifacts"][0]["path"] == str(xiao)
    assert result["board_model"] == MODEL and not result["physical_flash_performed"]
    data = json.loads(manifest.read_text()); data["board_model"] = "ai_thinker_esp32cam"
    manifest.write_text(json.dumps(data))
    assert fw.firmware_manifest(MODEL)["manifest_verification_error"] == "manifest_board_mismatch"


def test_status_and_build_api_validate_board_whitelist(tmp_path, monkeypatch):
    app = create_app(data_dir=tmp_path / "api-test", testing=True)
    with TestClient(app) as client:
        assert client.get("/api/session").status_code == 200
        service = app.state.firmware_service
        monkeypatch.setattr(service, "discover_ports", lambda: [])
        selected = []
        monkeypatch.setattr(service, "start_build", lambda clean, model: selected.append(model) or {"job_type": "build"})
        response = client.get("/api/firmware/status", params={"board_model": MODEL})
        assert response.status_code == 200, response.text
        assert response.json()["board_model"] == MODEL
        assert client.get("/api/firmware/status", params={"board_model": "../../outside"}).status_code == 422
        assert client.post("/api/firmware/build", json={"board_model": MODEL}).status_code == 200
        assert selected == [MODEL]
        assert client.post("/api/firmware/build", json={"board_model": "--flash COM7"}).status_code == 422
        assert selected == [MODEL]


def test_xiao_compile_uses_only_fixed_environment_and_disallows_manual_flash(fw, monkeypatch):
    import apps.api.app.firmware as module
    commands = []
    monkeypatch.setattr(module.subprocess, "Popen", lambda command, **kwargs: commands.append(command) or
        SimpleNamespace(stdout=['OMRESULT:{"compile_passed":true}\n'], wait=lambda: 0))
    code, result = fw._builder_process("test-job", board_model=MODEL)
    assert code == 0 and result["compile_passed"]
    assert commands[0][-2:] == ["--environment", "seeed_xiao_esp32s3"]
    assert "--flash" not in commands[0]
    with pytest.raises(HTTPException): fw._builder_process("test-job", board_model=MODEL, flash=True, port="COM7")
    assert len(commands) == 1


@pytest.mark.parametrize("chip,model,accepted", [("ESP32-S3", MODEL, True), ("ESP32", MODEL, False), ("ESP32-S3", "ai_thinker_esp32cam", False)])
def test_bind_pins_board_and_requires_matching_rom_result(fw, monkeypatch, chip, model, accepted):
    bundles = helper_contract(monkeypatch)
    payloads = []
    monkeypatch.setattr(fw.auto_usb, "_tool", lambda payload: payloads.append(payload) or
        {"chip": chip, "board_model": model, "mac_address": MAC, "physical_flash_performed": False})
    request = AutoUsbBindingRequest(port="COM7", ssid="contract", backend_url=bound()["backend_url"],
        device_name="Contract", room_name="Room", board_model=MODEL, authorize_fixed_firmware=True)
    if accepted:
        fw.auto_usb._bind_worker("test-job", request.model_dump(), BRIDGE)
        assert fw.auto_usb._binding()["board_model"] == MODEL
    else:
        with pytest.raises(RuntimeError): fw.auto_usb._bind_worker("test-job", request.model_dump(), BRIDGE)
        assert fw.auto_usb._binding() is None
    assert bundles == [MODEL] and payloads[0]["board_model"] == MODEL


def test_receipt_from_other_board_cannot_be_reused_or_modified(fw, monkeypatch):
    helper_contract(monkeypatch)
    fw.auto_usb._save(bound())
    with fw.connect() as connection:
        connection.execute("INSERT INTO firmware_usb_receipts(mac,manifest_sha256,status,flashed_at) VALUES(?,?,'flashed','contract')", (MAC, "a" * 64))
    calls = []
    monkeypatch.setattr(fw.auto_usb, "_tool", lambda payload: calls.append(payload))
    assert fw.auto_usb._receipt(bound()) is None
    assert fw.auto_usb.status()["physical_flash_performed"] is False
    with pytest.raises(RuntimeError, match="收据"):
        fw.auto_usb._install_worker("test-job", bound(), "COM7", {}, True)
    with fw.connect() as connection:
        row = connection.execute("SELECT status,board_model,error FROM firmware_usb_receipts").fetchone()
        assert tuple(row) == ("flashed", "ai_thinker_esp32cam", None)
    assert not calls


def test_matching_xiao_receipt_never_marks_ai_manifest_flashed(fw):
    write_status_bundle(fw, "ai_thinker_esp32cam")
    _, manifest = write_status_bundle(fw, MODEL)
    binding = {**bound(), "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}
    fw.auto_usb._save(binding)
    with fw.connect() as connection:
        connection.execute("INSERT INTO firmware_usb_receipts(mac,manifest_sha256,board_model,status,flashed_at) VALUES(?,?,?,'flashed','contract')",
                           (MAC, binding["manifest_sha256"], MODEL))
    assert fw.firmware_manifest(MODEL)["physical_flash_performed"] is True
    assert fw.firmware_manifest()["physical_flash_performed"] is False
    assert fw.firmware_manifest(MODEL)["physical_board_detected"] is False


def test_matched_rom_mac_without_board_metadata_cannot_promote_s3(fw):
    FirmwareService._validate_serial_board({"board_type": MODEL, "chip": "esp32s3"}, MODEL)
    FirmwareService._validate_serial_board({}, "ai_thinker_esp32cam")
    with pytest.raises(RuntimeError):
        FirmwareService._validate_serial_board({"mac_address": MAC}, MODEL)


def test_xiao_tick_reserves_board_and_sends_fixed_model_to_tool(fw, monkeypatch):
    helper_contract(monkeypatch)
    fw.auto_usb._save(bound()); fw.auto_usb.credentials = {"ssid": "contract", "password": ""}
    jobs = []
    monkeypatch.setattr(fw, "_create_job", lambda *args: jobs.append(args) or {"id": "test-job"})
    fw.auto_usb.tick()
    assert len(jobs) == 1 and fw.auto_usb._receipt(bound())["board_model"] == MODEL
    payloads = []
    monkeypatch.setattr(fw.auto_usb, "_tool", lambda payload: payloads.append(payload) or
        {"chip": "ESP32-S3", "board_model": MODEL, "mac_address": MAC,
         "physical_flash_performed": True, "manifest_sha256": "a" * 64})
    monkeypatch.setattr(fw, "get_device", lambda *args: {"mac_address": MAC, "simulated": False})
    monkeypatch.setattr(fw.auto_usb, "_read_ready", lambda *args: {"contract_only": True})
    fw.auto_usb._install_worker("test-job", bound(), "COM7", {}, False)
    assert payloads[0]["board_model"] == MODEL and fw.auto_usb._receipt(bound())["status"] == "linked"


@pytest.mark.parametrize("metadata", [{}, {"board_type": "ai_thinker_esp32cam", "chip": "esp32"},
    {"board_type": MODEL, "chip": "esp32"}])
def test_xiao_ready_requires_board_metadata_before_any_hardware_promotion(fw, metadata):
    with pytest.raises(RuntimeError, match="板型"):
        fw._verify_serial_ready_and_bind("COM7", {"board_model": MODEL, "mac_address": MAC}, metadata)


@pytest.mark.parametrize("correct", [True, False])
def test_xiao_identity_challenge_requires_same_board_before_config(fw, monkeypatch, correct):
    import apps.api.app.firmware as module
    helper_contract(monkeypatch)
    writes = []
    class Serial:
        closed = False
        nonce = ""
        def reset_input_buffer(self): pass
        def write(self, value): writes.append(value); self.nonce = value.decode().strip().split(":")[1]
        def flush(self): pass
        def close(self): self.closed = True
        def readline(self, size):
            data = {"nonce": self.nonce, "protocol": 1, "mac_address": MAC}
            if correct: data.update(board_type=MODEL, chip="esp32s3")
            return b"OMIDENT:" + json.dumps(data).encode()
    connection = Serial()
    monkeypatch.setattr(fw, "_open_serial", lambda *args, **kwargs: connection)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    if correct:
        assert fw._open_bound_serial("COM7", time.monotonic()+5, MAC, BRIDGE, MODEL) is connection
        connection.close()
    else:
        with pytest.raises(RuntimeError, match="板型"):
            fw._open_bound_serial("COM7", time.monotonic()+5, MAC, BRIDGE, MODEL)
        assert connection.closed
    assert writes and all(value.startswith(b"OMWHO:") for value in writes)


@pytest.mark.parametrize("second_identity", ["valid", "wrong_mac"])
def test_same_bound_usb_can_reenumerate_but_new_handle_must_reprove_identity(fw, monkeypatch, second_identity):
    import itertools
    import serial
    from pathlib import Path
    import apps.api.app.firmware_usb as module
    from apps.api.tests.test_firmware_usb import ChallengeSerial, challenge_payload
    helper = module.usb_helper(Path(__file__).resolve().parents[3])
    monkeypatch.setattr(module, "usb_helper", lambda root: helper)
    class XiaoSerial(ChallengeSerial):
        def write(self, data):
            super().write(data)
            for index, line in enumerate(self.lines):
                if isinstance(line, bytes) and line.startswith((b"OMIDENT:", b"OMREADY:")):
                    prefix, raw = line.split(b":", 1)
                    record = json.loads(raw)
                    record.update(board_type=MODEL, chip="esp32s3")
                    self.lines[index] = prefix + b":" + json.dumps(record).encode()
    first = XiaoSerial(disconnect_after_config=True)
    second = XiaoSerial(second_identity)
    opened = []
    clock = itertools.count(0, .1)
    monkeypatch.setattr("apps.api.app.firmware.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("apps.api.app.firmware.time.sleep", lambda seconds: None)
    monkeypatch.setattr(fw, "discover_ports", lambda: [{**PORT, "device": "COM9" if first.closed else "COM7"}])
    def open_port(**kwargs):
        expected = "COM9" if first.closed else "COM7"
        assert kwargs["port"] == expected
        connection = second if first.closed else first
        connection.port = expected
        opened.append(connection)
        return connection
    monkeypatch.setattr(serial, "Serial", open_port)
    verified = []
    monkeypatch.setattr(fw, "_verify_serial_ready_and_bind", lambda port, *args: verified.append(port) or {"contract_only": True})
    payload = {**challenge_payload(), "board_model": MODEL}
    if second_identity == "valid":
        fw._provision_worker("contract", payload, expected_mac=MAC, expected_bridge=BRIDGE)
        assert verified == ["COM9"]
        assert any(line.startswith(b"OMCFG:") for line in second.writes)
    else:
        with pytest.raises(RuntimeError, match="MAC"):
            fw._provision_worker("contract", payload, expected_mac=MAC, expected_bridge=BRIDGE)
        assert verified == []
        assert all(line.startswith(b"OMWHO:") for line in second.writes)
    assert opened == [first, second] and first.closed and second.closed
    assert first.writes[0] != second.writes[0]


def test_bound_serial_waits_for_brief_usb_absence_without_opening_other_port(fw, monkeypatch):
    import itertools
    import serial
    from pathlib import Path
    import apps.api.app.firmware_usb as module
    helper = module.usb_helper(Path(__file__).resolve().parents[3])
    monkeypatch.setattr(module, "usb_helper", lambda root: helper)
    snapshots = iter([[], [{**PORT, "device": "COM9"}], [{**PORT, "device": "COM9"}]])
    monkeypatch.setattr(fw, "discover_ports", lambda: next(snapshots))
    clock = itertools.count(0, .1)
    monkeypatch.setattr("apps.api.app.firmware.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("apps.api.app.firmware.time.sleep", lambda seconds: None)
    opened = []
    monkeypatch.setattr(serial, "Serial", lambda **kwargs: opened.append(kwargs["port"]) or SimpleNamespace(port=kwargs["port"]))
    connection = fw._open_serial("COM7", 2, expected_bridge=BRIDGE)
    assert connection.port == "COM9" and opened == ["COM9"]


def test_credential_free_ready_uses_reenumerated_verified_port(fw, monkeypatch):
    ready = {"device_id": bound()["device_id"], "camera_id": "test-camera", "ip": "192.168.1.8",
        "stream_url": "http://192.168.1.8/stream", "capture_url": "http://192.168.1.8/capture",
        "board_type": MODEL, "chip": "esp32s3"}
    closed = []
    connection = SimpleNamespace(port="COM9", readline=lambda size: b"OMREADY:" + json.dumps(ready).encode(),
                                 close=lambda: closed.append(True))
    monkeypatch.setattr(fw, "_open_bound_serial", lambda *args: connection)
    ports = []
    monkeypatch.setattr(fw, "_verify_serial_ready_and_bind", lambda port, *args: ports.append(port) or {"contract_only": True})
    fw.auto_usb._read_ready("contract", bound(), "COM7")
    assert ports == ["COM9"] and closed == [True]


def test_stale_queued_flash_flag_cannot_reflash_completed_receipt(fw, monkeypatch):
    helper_contract(monkeypatch)
    fw.auto_usb._save(bound())
    with fw.connect() as connection:
        connection.execute("INSERT INTO firmware_usb_receipts(mac,manifest_sha256,board_model,status,flashed_at) VALUES(?,?,?,'flashed','contract')",
                           (MAC, "a" * 64, MODEL))
    operations = []
    def tool(payload):
        operations.append(payload["operation"])
        return {"chip": "ESP32-S3", "board_model": MODEL, "mac_address": MAC,
                "physical_flash_performed": payload["operation"] == "flash", "manifest_sha256": "a" * 64}
    monkeypatch.setattr(fw.auto_usb, "_tool", tool)
    monkeypatch.setattr(fw, "get_device", lambda *args: {"mac_address": MAC, "simulated": False})
    monkeypatch.setattr(fw.auto_usb, "_read_ready", lambda *args: {"contract_only": True})
    result = fw.auto_usb._install_worker("contract", bound(), "COM7", {}, False)
    assert operations == ["identify"]
    assert result["same_version_skipped_flash"] is True
    assert result["physical_flash_performed"] is False
