"""ROM ownership/release contracts; all hardware is fake, never opens a port."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]


def helper():
    spec = importlib.util.spec_from_file_location("usb_inspection_contract", ROOT / "scripts/firmware-usb.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Rom:
    CHIP_NAME = "ESP32-S3"
    secure_download_mode = False

    def __init__(self):
        self.actions = []
        self._port = SimpleNamespace(close=lambda: self.actions.append("close"))

    def read_mac(self):
        self.actions.append("read_mac")
        return bytes.fromhex("020000000001")

    def hard_reset(self):
        self.actions.append("hard_reset")

    def get_secure_boot_enabled(self): return False
    def get_flash_encryption_enabled(self): return False
    def get_chip_spi_pads(self): return (0, 0, 0, 0, 0)
    def flash_spi_attach(self, _): self.actions.append("flash_spi_attach")
    def flash_id(self): return 0x1640ef


def tool_for(rom, main=None):
    def fail_write(*args, **kwargs):
        raise AssertionError("Read-only inspection must never write")
    return SimpleNamespace(detect_chip=lambda **kwargs: rom, main=main or fail_write)


def test_unsupported_identify_resets_before_close_and_preserves_rejection():
    rom = Rom()
    with pytest.raises(ValueError, match="Only the original ESP32"):
        helper().connected_operation(tool_for(rom), "COM_TEST_ONLY")
    assert rom.actions == ["hard_reset", "close"]


@pytest.mark.parametrize("chip", ["ESP32-S3", "ESP32-C3", "ESP32-S2", "ESP32"])
def test_inspection_returns_actual_chip_without_install_permission(chip):
    rom = Rom()
    rom.CHIP_NAME = chip
    result = helper().inspect_connected(tool_for(rom), "COM_TEST_ONLY")
    assert result["chip"] == chip
    assert result["mac_address"] == "02:00:00:00:00:01"
    assert result["compatible_original_esp32"] is (chip == "ESP32")
    assert result["physical_flash_performed"] is False
    assert result["automatic_install_allowed"] is False
    assert result["flash_eligibility_verified"] is False
    assert result["persistent_memory_written"] is False
    assert result["connection_may_reset"] is True
    assert rom.actions == ["read_mac", "hard_reset", "close"]


def test_secure_inspection_does_not_attempt_forbidden_efuse_or_flash_reads():
    rom = Rom()
    rom.secure_download_mode = True
    result = helper().inspect_connected(tool_for(rom), "COM_TEST_ONLY")
    assert result["secure_download_mode"] is True
    assert result["mac_address"] is None
    assert result["automatic_install_allowed"] is False
    assert rom.actions == ["hard_reset", "close"]


@pytest.mark.parametrize("mac", [b"", b"\0" * 6, b"\xff" * 6, b"\x01" * 7])
def test_inspection_never_returns_invalid_mac_as_identity(mac):
    rom = Rom()
    rom.read_mac = lambda: mac
    result = helper().inspect_connected(tool_for(rom), "COM_TEST_ONLY")
    assert result["mac_address"] is None
    assert result["identity_method"] == "usb_rom_chip_only"
    assert result["diagnostics"] == ["efuse_mac_unavailable"]
    assert rom.actions == ["hard_reset", "close"]


def test_reset_failure_still_closes_and_does_not_mask_model_rejection():
    rom = Rom()
    def fail_reset():
        rom.actions.append("hard_reset")
        raise OSError("reset failed")
    rom.hard_reset = fail_reset
    with pytest.raises(ValueError, match="Only the original ESP32"):
        helper().connected_operation(tool_for(rom), "COM_TEST_ONLY")
    assert rom.actions == ["hard_reset", "close"]


def test_successful_read_with_failed_reset_cannot_report_full_success():
    rom = Rom()
    def fail_reset():
        rom.actions.append("hard_reset")
        raise OSError("reset failed")
    rom.hard_reset = fail_reset
    with pytest.raises(RuntimeError, match="release"):
        helper().inspect_connected(tool_for(rom), "COM_TEST_ONLY")
    assert rom.actions == ["read_mac", "hard_reset", "close"]


def test_interrupted_flash_never_retries_or_resets_partially_written_app(tmp_path):
    module = helper()
    rom = Rom()
    rom.CHIP_NAME = "ESP32"
    calls = []
    def fail_flash(arguments, esp):
        calls.append(arguments)
        assert esp is rom
        raise RuntimeError("controlled write failure")
    with pytest.raises(RuntimeError, match="controlled write failure"):
        module.connected_operation(tool_for(rom, fail_flash), "COM_TEST_ONLY", "02:00:00:00:00:01",
                                   {name: b"contract only" for name in module.OFFSETS}, tmp_path)
    assert len(calls) == 1
    assert "hard_reset" not in rom.actions
    assert rom.actions[-1] == "close"


def test_successful_identify_restores_boot_exactly_once():
    rom = Rom()
    rom.CHIP_NAME = "ESP32"
    result = helper().connected_operation(tool_for(rom), "COM_TEST_ONLY")
    assert result["physical_flash_performed"] is False
    assert rom.actions == ["read_mac", "flash_spi_attach", "hard_reset", "close"]


def test_close_failure_cannot_mask_original_incompatibility():
    rom = Rom()
    def fail_close():
        rom.actions.append("close")
        raise OSError("controlled close failure")
    rom._port.close = fail_close
    with pytest.raises(ValueError, match="Only the original ESP32"):
        helper().connected_operation(tool_for(rom), "COM_TEST_ONLY")
    assert rom.actions == ["hard_reset", "close"]


@pytest.mark.parametrize("changed", ["none", "different_port", "different_bridge", "multiple_ports"])
def test_inspect_cli_keeps_exact_bridge_and_port_guards(tmp_path, monkeypatch, changed):
    from serial.tools import list_ports

    module = helper()
    module.ROOT = tmp_path
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/firmware-build.py").write_text(
        "from pathlib import Path\n"
        "BLUETOOTH_MARKERS = ('bluetooth',)\n"
        "def safe_build_root(): return Path(__file__).resolve().parents[1] / 'tooling'\n",
        encoding="utf-8")
    (tmp_path / "tooling/platformio-core/packages/tool-esptoolpy/_contrib").mkdir(parents=True)
    # Isolated simulated OS inventory; never calls real list_ports/detect_chip.
    bridge = {"vid": 0x303a, "pid": 0x1001, "serial_number": "contract-only", "location": None}
    record = SimpleNamespace(**bridge, device="COM_TEST_ONLY", description="USB Serial")
    rows = [record]
    if changed == "different_port": record.device = "COM_OTHER_TEST"
    if changed == "different_bridge": record.serial_number = "other-contract"
    if changed == "multiple_ports": rows.append(SimpleNamespace(**vars(record)))
    monkeypatch.setattr(list_ports, "comports", lambda: rows)
    rom = Rom()
    tool = tool_for(rom)
    tool.__version__ = "4.11.0"
    monkeypatch.setitem(sys.modules, "esptool", tool)
    monkeypatch.setattr(sys, "path", list(sys.path))
    payload = {"operation": "inspect", "bridge": bridge, "port": "COM_TEST_ONLY"}
    if changed == "none":
        result = module.execute(payload)
        assert result["operation"] == "inspect"
        assert result["chip"] == "ESP32-S3"
        assert result["automatic_install_allowed"] is False
        assert rom.actions == ["read_mac", "hard_reset", "close"]
    else:
        with pytest.raises(ValueError):
            module.execute(payload)
        assert rom.actions == []
