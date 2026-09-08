"""Fixed XIAO S3 profile contracts; no physical USB/flash operations."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
MODEL = "xiao_esp32s3_sense"
MAC = "02:00:00:00:00:01"
ENTRIES = [(1, 2, 0x9000, 0x5000), (1, 0, 0xe000, 0x2000),
           (0, 0x10, 0x10000, 0x330000), (0, 0x11, 0x340000, 0x330000),
           (1, 0x82, 0x670000, 0x180000), (1, 3, 0x7f0000, 0x10000)]
OFFSETS = {"bootloader.bin": 0, "partitions.bin": 0x8000, "boot_app0.bin": 0xe000, "firmware.bin": 0x10000}


def helper():
    spec = importlib.util.spec_from_file_location("usb_xiao_contract", ROOT / "scripts/firmware-usb.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def image(chip_id=9):
    header = bytearray(24)
    header[0] = 0xe9
    header[1] = 1
    header[3] = 0x3f  # Fixed 8 MB / 80 MHz image header, matching real build.
    struct.pack_into("<H", header, 12, chip_id)
    return bytes(header) + b"contract-only-not-flashable-image"


def fixture_bundle(root):
    folder = root / "firmware/esp32cam/build-xiao-esp32s3-sense"
    public = root / "artifacts/firmware/xiao-esp32s3-sense"
    folder.mkdir(parents=True)
    public.mkdir(parents=True)
    binaries = {"bootloader.bin": image(), "firmware.bin": image(), "boot_app0.bin": b"contract-otadata",
                "partitions.bin": b"".join(struct.pack("<HBBII16sI", 0x50aa, *entry, b"contract", 0) for entry in ENTRIES)}
    manifest = {"compile_passed": True, "environment": "seeed_xiao_esp32s3", "target_board": "Seeed XIAO ESP32S3 Sense",
                "board_model": MODEL, "board_type": MODEL, "chip": "esp32s3", "flash_size_mb": 8,
                "psram_size_mb": 8, "flash_offsets": OFFSETS, "firmware_version": "contract-xiao", "artifacts": []}
    for name, data in binaries.items():
        (folder / name).write_bytes(data)
        manifest["artifacts"].append({"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    path = public / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return folder, path, manifest


def test_xiao_profile_bundle_uses_separate_fixed_path_and_offsets(tmp_path):
    fixture_bundle(tmp_path)
    module = helper()
    digest, manifest, binaries = module.bundle(tmp_path, board_model=MODEL)
    assert len(digest) == 64 and manifest["board_model"] == MODEL
    assert set(binaries) == set(OFFSETS)
    assert module.OFFSETS["bootloader.bin"] == "0x1000"  # default remains AI Thinker
    with pytest.raises(FileNotFoundError): module.bundle(tmp_path)


@pytest.mark.parametrize("field,value", [("environment", "esp32cam"), ("target_board", "AI Thinker ESP32-CAM (esp32cam)"),
                                         ("board_model", "ai_thinker_esp32cam"), ("chip", "esp32"),
                                         ("flash_size_mb", 4), ("psram_size_mb", 2),
                                         ("flash_offsets", {**OFFSETS, "bootloader.bin": 0x1000})])
def test_xiao_manifest_cross_profile_or_changed_geometry_rejected(tmp_path, field, value):
    _, path, manifest = fixture_bundle(tmp_path)
    manifest[field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError): helper().bundle(tmp_path, board_model=MODEL)


@pytest.mark.parametrize("name", ["bootloader.bin", "firmware.bin"])
def test_rehashed_original_esp32_binary_in_xiao_manifest_is_rejected(tmp_path, name):
    folder, path, manifest = fixture_bundle(tmp_path)
    data = image(chip_id=0)
    (folder / name).write_bytes(data)
    record = next(row for row in manifest["artifacts"] if row["name"] == name)
    record.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="chip"):
        helper().bundle(tmp_path, board_model=MODEL)


def test_rehashed_wrong_partition_table_is_rejected(tmp_path):
    folder, path, manifest = fixture_bundle(tmp_path)
    data = bytearray((folder / "partitions.bin").read_bytes())
    struct.pack_into("<I", data, 2 * 32 + 8, 0x1e0000)
    (folder / "partitions.bin").write_bytes(data)
    record = next(row for row in manifest["artifacts"] if row["name"] == "partitions.bin")
    record["sha256"] = hashlib.sha256(data).hexdigest()
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="partition"):
        helper().bundle(tmp_path, board_model=MODEL)


@pytest.mark.parametrize("kind", ["boot_flash_size", "app_flash_size", "encrypted_partition"])
def test_rehashed_geometry_or_partition_flags_cannot_bypass_fixed_profile(tmp_path, kind):
    folder, path, manifest = fixture_bundle(tmp_path)
    name = "partitions.bin" if kind == "encrypted_partition" else ("bootloader.bin" if kind == "boot_flash_size" else "firmware.bin")
    data = bytearray((folder / name).read_bytes())
    if kind == "encrypted_partition":
        struct.pack_into("<I", data, 2 * 32 + 28, 1)
    else:
        data[3] = 0x2f  # Image says 4 MB even though manifest still says 8 MB.
    (folder / name).write_bytes(data)
    record = next(row for row in manifest["artifacts"] if row["name"] == name)
    record["sha256"] = hashlib.sha256(data).hexdigest()
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="flash size|partition"):
        helper().bundle(tmp_path, board_model=MODEL)


class S3Rom:
    CHIP_NAME = "ESP32-S3"
    secure_download_mode = False
    def __init__(self):
        self.actions = []
        self._port = SimpleNamespace(close=lambda: self.actions.append("close"))
    def hard_reset(self): self.actions.append("reset")
    def read_mac(self): return bytes.fromhex(MAC.replace(":", ""))
    def get_secure_boot_enabled(self): return False
    def get_flash_encryption_enabled(self): return False
    def get_chip_spi_pads(self): raise AssertionError("ESP32 pad decoding must not be used for S3")
    def flash_spi_attach(self, value): assert value == 0
    def flash_id(self): return 0x1740ef


def test_s3_identify_and_fixed_flash_reuse_same_verified_handle(tmp_path):
    rom = S3Rom()
    calls = []
    def write(arguments, esp):
        assert esp is rom
        assert arguments[arguments.index("--chip") + 1] == "esp32s3"
        for name, offset in OFFSETS.items():
            index = arguments.index(hex(offset))
            assert Path(arguments[index + 1]).name == name
        assert "--force" not in arguments and "erase_flash" not in arguments
        calls.append(arguments)
    tool = SimpleNamespace(detect_chip=lambda **kwargs: rom, main=write)
    module = helper()
    result = module.connected_operation(tool, "COM_TEST_ONLY", board_model=MODEL)
    assert result["chip"] == "ESP32-S3" and result["board_model"] == MODEL
    assert result["physical_flash_performed"] is False
    rom.actions.clear()
    result = module.connected_operation(tool, "COM_TEST_ONLY", MAC, {name: b"contract" for name in OFFSETS}, tmp_path, board_model=MODEL)
    assert result["physical_flash_performed"] is True
    assert len(calls) == 1 and rom.actions == ["close"]


@pytest.mark.parametrize("change", ["esp32", "esp32-c3", "4mb", "16mb", "secure", "encrypted", "mac"])
def test_xiao_wrong_physical_target_never_writes_and_restores_boot(tmp_path, change):
    rom = S3Rom()
    if change == "esp32": rom.CHIP_NAME = "ESP32"
    if change == "esp32-c3": rom.CHIP_NAME = "ESP32-C3"
    if change == "4mb": rom.flash_id = lambda: 0x1640ef
    if change == "16mb": rom.flash_id = lambda: 0x1840ef
    if change == "secure": rom.get_secure_boot_enabled = lambda: True
    if change == "encrypted": rom.get_flash_encryption_enabled = lambda: True
    writes = []
    tool = SimpleNamespace(detect_chip=lambda **kwargs: rom, main=lambda *args, **kwargs: writes.append(args))
    with pytest.raises(ValueError):
        helper().connected_operation(tool, "COM_TEST_ONLY", "02:00:00:00:00:ff" if change == "mac" else MAC,
                                     {name: b"contract" for name in OFFSETS}, tmp_path, board_model=MODEL)
    assert writes == [] and rom.actions == ["reset", "close"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("model", ["ESP32-S3", "../xiao", "auto", "", None])
def test_non_whitelisted_profile_rejected_before_open(model):
    calls = []
    tool = SimpleNamespace(detect_chip=lambda **kwargs: calls.append(kwargs))
    with pytest.raises(ValueError): helper().connected_operation(tool, "COM_TEST_ONLY", board_model=model)
    assert calls == []
