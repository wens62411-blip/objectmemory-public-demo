"""Clean-checkout automatic builds; ROM is mocked, never physical acceptance."""
import json

import pytest
from fastapi import HTTPException

from apps.api.app.firmware import AutoUsbBindingRequest, FirmwareService
from apps.api.app import firmware_usb
from apps.api.tests.test_firmware_usb import BRIDGE, MAC, PORT, ROOT, create_bundle
from apps.api.tests.test_firmware_usb_xiao import fixture_bundle


@pytest.fixture
def clean_board(tmp_path, monkeypatch):
    service = FirmwareService(tmp_path / "isolated.sqlite", None, tmp_path / "data", runtime_mode="REAL")
    service.project_root = tmp_path / "source"
    helper = firmware_usb.usb_helper(ROOT)
    monkeypatch.setattr(firmware_usb, "usb_helper", lambda root: helper)
    monkeypatch.setattr(service, "discover_ports", lambda: [dict(PORT)])
    monkeypatch.setattr(service, "_verify_backend_listener", lambda url: url)
    monkeypatch.setattr(service.auto_usb, "_start_listener", lambda: None)
    try:
        yield service
    finally:
        service.shutdown()


def request(model="xiao_esp32s3_sense"):
    return AutoUsbBindingRequest(port=PORT["device"], ssid="test-network", password="test-only-123",
        backend_url="http://192.168.1.20:8018", device_name="Test camera", room_name="Test room",
        board_model=model, authorize_fixed_firmware=True)


@pytest.mark.parametrize("model,make", [("xiao_esp32s3_sense", fixture_bundle), ("ai_thinker_esp32cam", create_bundle)])
def test_missing_bundle_queues_then_builds_and_verifies_before_usb(clean_board, monkeypatch, model, make):
    service = clean_board
    flow = []
    monkeypatch.setattr(service, "_create_job", lambda *args: {"id": "queued-build", "job_type": args[0]})
    assert service.auto_usb.bind(request(model))["job_type"] == "usb_bind"
    assert service.auto_usb._binding() is None
    def build(job, *, board_model):
        assert board_model == model
        flow.append("build")
        make(service.project_root)
        return 0, {"compile_passed": True}
    def identify(payload):
        flow.append(payload["operation"])
        assert payload["board_model"] == model
        return {"chip": "ESP32-S3" if model == "xiao_esp32s3_sense" else "ESP32",
                "mac_address": MAC, "board_model": model, "physical_flash_performed": False}
    monkeypatch.setattr(service, "_builder_process", build)
    monkeypatch.setattr(service.auto_usb, "_tool", identify)
    result = service.auto_usb._bind_worker("job", request(model).model_dump(), BRIDGE)
    assert flow == ["build", "identify"]
    assert len(result["binding"]["manifest_sha256"]) == 64
    assert result["physical_flash_performed"] is False
    assert "test-only-123" not in json.dumps(result)


def test_build_failure_never_opens_usb_or_saves_binding(clean_board, monkeypatch):
    service = clean_board
    monkeypatch.setattr(service, "_builder_process", lambda *args, **kwargs: (1, None))
    monkeypatch.setattr(service.auto_usb, "_tool", lambda *_: pytest.fail("USB touched after build failure"))
    with pytest.raises(RuntimeError, match="自动编译失败"):
        service.auto_usb._bind_worker("job", request().model_dump(), BRIDGE)
    assert service.auto_usb._binding() is None


def test_corrupt_existing_bundle_is_not_silently_rebuilt(clean_board, monkeypatch):
    service = clean_board
    folder, _, _ = fixture_bundle(service.project_root)
    (folder / "firmware.bin").write_bytes(b"corrupt")
    monkeypatch.setattr(service, "_builder_process", lambda *_a, **_k: pytest.fail("corruption concealed"))
    with pytest.raises(HTTPException, match="固定固件清单"):
        service.auto_usb.bind(request())
    assert service.auto_usb._binding() is None


def test_shutdown_during_compile_prevents_usb_access(clean_board, monkeypatch):
    service = clean_board
    def build(*args, **kwargs):
        fixture_bundle(service.project_root)
        service.auto_usb.stop.set()
        return 0, {"compile_passed": True}
    monkeypatch.setattr(service, "_builder_process", build)
    monkeypatch.setattr(service.auto_usb, "_tool", lambda *_: pytest.fail("USB touched after shutdown"))
    with pytest.raises(RuntimeError, match="已关闭"):
        service.auto_usb._bind_worker("job", request().model_dump(), BRIDGE)
    assert service.auto_usb._binding() is None
