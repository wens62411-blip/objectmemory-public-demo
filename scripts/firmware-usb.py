"""Bound USB ROM identity check / fixed-bundle flash, never port autodetection.

Invoked only by the administrator-authorized firmware controller. JSON arrives
on stdin, not a shell or command line. No Wi-Fi credentials enter this process.
The connected ROM object is reused for MAC verification AND writing, avoiding
a second connection to an interchangeable COM number. Requires local esptool 4.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
OFFSETS = {"bootloader.bin": "0x1000", "partitions.bin": "0x8000",
           "boot_app0.bin": "0xe000", "firmware.bin": "0x10000"}
SIZE_LIMITS = {"bootloader.bin": 0x7000, "partitions.bin": 0x1000,
               "boot_app0.bin": 0x2000, "firmware.bin": 0x1e0000}
DEFAULT_BOARD_MODEL = "ai_thinker_esp32cam"
BOARD_PROFILES = {
    DEFAULT_BOARD_MODEL: {
        "environment": "esp32cam", "target_board": "AI Thinker ESP32-CAM (esp32cam)",
        "chip": "ESP32", "tool_chip": "esp32", "image_chip_id": 0,
        "flash_size_mb": 4, "flash_id_capacity": 0x16, "image_flash_size_id": 2,
        "manifest": "artifacts/firmware/manifest.json", "build": "firmware/esp32cam/build",
        "offsets": OFFSETS, "size_limits": SIZE_LIMITS,
        "partitions": {(1, 2, 0x9000, 0x5000), (1, 0, 0xe000, 0x2000),
                       (0, 0x10, 0x10000, 0x1e0000), (0, 0x11, 0x1f0000, 0x1e0000),
                       (1, 0x82, 0x3d0000, 0x20000), (1, 3, 0x3f0000, 0x10000)},
    },
    "xiao_esp32s3_sense": {
        "environment": "seeed_xiao_esp32s3", "target_board": "Seeed XIAO ESP32S3 Sense",
        "chip": "ESP32-S3", "tool_chip": "esp32s3", "image_chip_id": 9,
        "flash_size_mb": 8, "flash_id_capacity": 0x17, "image_flash_size_id": 3,
        "manifest": "artifacts/firmware/xiao-esp32s3-sense/manifest.json",
        "build": "firmware/esp32cam/build-xiao-esp32s3-sense",
        "offsets": {"bootloader.bin": "0x0", "partitions.bin": "0x8000",
                    "boot_app0.bin": "0xe000", "firmware.bin": "0x10000"},
        "size_limits": {"bootloader.bin": 0x8000, "partitions.bin": 0x1000,
                        "boot_app0.bin": 0x2000, "firmware.bin": 0x330000},
        "partitions": {(1, 2, 0x9000, 0x5000), (1, 0, 0xe000, 0x2000),
                       (0, 0x10, 0x10000, 0x330000), (0, 0x11, 0x340000, 0x330000),
                       (1, 0x82, 0x670000, 0x180000), (1, 3, 0x7f0000, 0x10000)},
    },
}


def board_profile(board_model):
    if not isinstance(board_model, str) or board_model not in BOARD_PROFILES:
        raise ValueError("Only explicitly reviewed fixed board profiles are supported")
    return BOARD_PROFILES[board_model]


def normal_path(path: Path, boundary: Path) -> Path:
    absolute = path.absolute()
    absolute.relative_to(boundary.absolute())
    for entry in (absolute, *absolute.parents):
        if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
            raise ValueError("USB artifact paths must not contain links or junctions")
    resolved = absolute.resolve(strict=True)
    resolved.relative_to(boundary.resolve(strict=True))
    return resolved


def bundle(repo: Path = ROOT, expected: str | None = None, *, board_model=DEFAULT_BOARD_MODEL):
    profile = board_profile(board_model)
    manifest_path = normal_path(repo / profile["manifest"], repo)
    if manifest_path.stat().st_size > 65536:
        raise ValueError("Firmware manifest exceeds size limit")
    raw = manifest_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected is not None and digest != expected:
        raise ValueError("Fixed firmware manifest changed; administrator reauthorization required")
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or manifest.get("compile_passed") is not True
            or manifest.get("environment") != profile["environment"]
            or manifest.get("target_board") != profile["target_board"]):
        raise ValueError("A compiled manifest for the selected fixed board profile is required")
    fixed_offsets = {name: int(offset, 16) for name, offset in profile["offsets"].items()}
    fixed_metadata = {"board_model": board_model, "board_type": board_model, "chip": profile["tool_chip"],
                      "flash_size_mb": profile["flash_size_mb"], "flash_offsets": fixed_offsets}
    if board_model == "xiao_esp32s3_sense":
        fixed_metadata["psram_size_mb"] = 8
    for key, value in fixed_metadata.items():
        # Original manifests predate explicit metadata. When present it must
        # still match; a newly added XIAO manifest requires all target fields.
        if (board_model != DEFAULT_BOARD_MODEL or key in manifest) and manifest.get(key) != value:
            raise ValueError(f"Firmware manifest board profile mismatch: {key}")
    if "flash_offsets" in manifest and any(type(value) is not int for value in manifest["flash_offsets"].values()):
        raise ValueError("Firmware flash offsets must be fixed integer addresses")
    records = manifest.get("artifacts")
    if not isinstance(records, list) or len(records) > 8:
        raise ValueError("Invalid firmware artifact list")
    binaries = {}
    for name in profile["offsets"]:
        matches = [entry for entry in records if isinstance(entry, dict) and entry.get("name") == name]
        if len(matches) != 1:
            raise ValueError(f"Rebuild firmware: exactly one {name} must be in the manifest")
        record = matches[0]
        path = normal_path(repo / profile["build"] / name, repo)
        size = path.stat().st_size
        if not 0 < size <= profile["size_limits"][name] or record.get("size") != size:
            raise ValueError(f"Invalid artifact size: {name}")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != record.get("sha256"):
            raise ValueError(f"Firmware artifact hash mismatch: {name}")
        binaries[name] = content
    # ESP32 image format: 8-byte common header followed by the 16-byte
    # extended header; its little-endian chip_id starts at absolute byte 12.
    # esptool 4.11 ESP32ROM=0 / ESP32S3ROM=9. A rehashed wrong-board image is
    # rejected before serial is opened, independent of its manifest label.
    for name in ("bootloader.bin", "firmware.bin"):
        content = binaries[name]
        if (len(content) < 24 or content[0] != 0xe9 or not 1 <= content[1] <= 16
                or struct.unpack_from("<H", content, 12)[0] != profile["image_chip_id"]):
            raise ValueError(f"Firmware image chip header does not match the selected board: {name}")
        if content[3] >> 4 != profile["image_flash_size_id"]:
            raise ValueError(f"Firmware image flash size does not match the selected board: {name}")
    entries = []
    for offset in range(0, len(binaries["partitions.bin"]) - 31, 32):
        magic, kind, subtype, address, length = struct.unpack_from("<HBBII", binaries["partitions.bin"], offset)
        if magic == 0x50aa:
            if struct.unpack_from("<I", binaries["partitions.bin"], offset + 28)[0] != 0:
                raise ValueError("Firmware partition flags differ from the reviewed unencrypted layout")
            entries.append((kind, subtype, address, length))
    # min_spiffs.csv (AI Thinker) / fixed default_8MB.csv (XIAO), never
    # arbitrary offsets or partition definitions supplied by the request.
    required = profile["partitions"]
    if len(entries) != len(required) or set(entries) != required:
        raise ValueError("Firmware partition table does not match the reviewed fixed board layout")
    return digest, manifest, binaries


def bridge_identity(record):
    serial = str(record.get("serial_number") or "").strip()
    location = str(record.get("location") or "").strip()
    if (record.get("eligible") is not True or type(record.get("vid")) is not int
            or type(record.get("pid")) is not int or not (serial or location)):
        raise ValueError("USB bridge needs a unique serial number or stable USB location")
    return {"vid": record["vid"], "pid": record["pid"],
            "serial_number": serial or None, "location": location if not serial else None}


def matching_port(records, expected):
    # With several USB serial bridges attached the administrator must disconnect
    # others first. We do not reset/probe unrelated bridges to find an ESP32.
    eligible = [record for record in records if record.get("eligible") is True]
    if len(eligible) != 1:
        raise ValueError("Exactly one eligible USB serial bridge must be connected")
    record = eligible[0]
    if bridge_identity(record) != expected:
        raise ValueError("USB bridge identity or physical socket changed")
    return record["device"]


def physical_mac(esp):
    mac = ":".join(f"{value:02x}" for value in esp.read_mac())
    if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac) or mac in {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"}:
        raise ValueError("Invalid physical eFuse MAC")
    return mac


def release_rom(esp, *, restore_application, active_error):
    """Release our handle without hiding the original inspection/write error.

    Read-only probes temporarily enter ROM download mode: restore normal boot
    even when a compatibility check rejects the board. Once a flash command
    starts, its own reset policy owns the chip; never boot a possibly partial
    image or repeat a write from this cleanup path.
    """
    failures = []
    if restore_application:
        try:
            esp.hard_reset()
        except Exception as error:
            failures.append(("reset", error))
    try:
        esp._port.close()
    except Exception as error:
        failures.append(("close", error))
    if failures:
        message = "USB release failed (" + ", ".join(stage for stage, _ in failures) + "); board state requires manual verification"
        if active_error is not None:
            if hasattr(active_error, "add_note"):
                active_error.add_note(message)
            print(message, file=sys.stderr)
        else:
            raise RuntimeError(message) from failures[0][1]


def inspect_connected(esptool, port):
    """Identify unsupported chips without touching flash or granting consent.

    This is flash/eFuse-read-only, not zero disruption: esptool briefly enters
    the ROM loader and resets the existing application when the probe finishes.
    A native USB VID/PID or ESP32 family chip name never proves camera pinout.
    """
    esp = esptool.detect_chip(port=port, baud=115200, connect_attempts=2)
    try:
        secure = bool(esp.secure_download_mode)
        chip = str(esp.CHIP_NAME)[:64]
        mac = None
        diagnostics = []
        if secure:
            diagnostics.append("secure_download_mode_mac_not_read")
        else:
            try:
                mac = physical_mac(esp)
            except Exception:
                # Report no identity, not a fabricated/partially decoded MAC.
                # Detailed device/tool errors can contain incidental input.
                diagnostics.append("efuse_mac_unavailable")
        return {"chip": chip, "mac_address": mac, "port": port,
                "operation": "inspect", "physical_flash_performed": False,
                "persistent_memory_written": False, "connection_may_reset": True,
                "volatile_rom_setup_possible": True,
                "identity_method": "usb_rom_efuse_mac" if mac else "usb_rom_chip_only",
                "secure_download_mode": secure,
                "compatible_original_esp32": chip == "ESP32",
                "flash_eligibility_verified": False, "automatic_install_allowed": False,
                "camera_board_model_verified": False,
                "diagnostics": diagnostics}
    finally:
        release_rom(esp, restore_application=True, active_error=sys.exc_info()[1])


def connected_operation(esptool, port, expected_mac=None, binaries=None, staging=None, *, board_model=DEFAULT_BOARD_MODEL):
    """No write/erase before model + eFuse MAC checks on this exact handle."""
    profile = board_profile(board_model)
    esp = esptool.detect_chip(port=port, baud=115200, connect_attempts=2)
    flash_started = False
    try:
        if esp.CHIP_NAME != profile["chip"] or esp.secure_download_mode:
            if board_model == DEFAULT_BOARD_MODEL:
                raise ValueError("Only the original ESP32 ROM is supported for AI Thinker, not C3/S2/S3 or secure-download boards")
            raise ValueError("Selected XIAO profile requires ESP32-S3 ROM, not another chip or secure-download board")
        mac = physical_mac(esp)
        if expected_mac is not None and mac != expected_mac:
            raise ValueError("Physical board MAC differs from the authorized board; no flash performed")
        if esp.get_secure_boot_enabled() or esp.get_flash_encryption_enabled():
            raise ValueError("Secured devices are never automatically flashed")
        if board_model == DEFAULT_BOARD_MODEL and esp.get_chip_spi_pads() != (0, 0, 0, 0, 0):
            raise ValueError("Nonstandard flash wiring is not an approved AI Thinker profile")
        esp.flash_spi_attach(0)
        if esp.flash_id() >> 16 != profile["flash_id_capacity"]:
            raise ValueError(f"Automatic install requires the reviewed {profile['flash_size_mb']} MB flash size")
        result = {"chip": profile["chip"], "board_model": board_model, "flash_size_mb": profile["flash_size_mb"],
                  "camera_board_model_verified": False, "mac_address": mac, "port": port,
                  "physical_flash_performed": False, "identity_method": "usb_rom_efuse_mac"}
        if binaries is not None:
            if not expected_mac or staging is None:
                raise ValueError("Flash requires an already authorized MAC and staged bundle")
            arguments = ["--chip", profile["tool_chip"], "--port", port, "--baud", "460800",
                         "--before", "no_reset", "--after", "hard_reset", "write_flash",
                         "--flash_mode", "keep", "--flash_freq", "keep", "--flash_size", "keep"]
            for name, offset in profile["offsets"].items():
                target = staging / name
                with target.open("xb") as output:
                    output.write(binaries[name])
                arguments.extend([offset, str(target)])
            # v4 main accepts an existing ROM object and verifies written data;
            # no --force, erase_flash, arbitrary offsets, eFuse, or OTA operation.
            flash_started = True
            esptool.main(arguments, esp=esp)
            result["physical_flash_performed"] = True
        return result
    finally:
        release_rom(esp, restore_application=not flash_started, active_error=sys.exc_info()[1])


def execute(payload):
    if not isinstance(payload, dict) or payload.get("operation") not in {"inspect", "identify", "flash"}:
        raise ValueError("Invalid USB operation")
    board_model = payload.get("board_model", DEFAULT_BOARD_MODEL)
    board_profile(board_model)  # Reject unknown target before tool imports/ports.
    spec = importlib.util.spec_from_file_location("om_existing_builder", ROOT / "scripts/firmware-build.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    build_root = builder.safe_build_root()
    tool_root = normal_path(build_root / "platformio-core/packages/tool-esptoolpy", build_root)
    # PlatformIO's esptool wrapper supplies its vendored dependencies here.
    # Reuse that reviewed installation without pip/network or executing .pth.
    contrib = normal_path(tool_root / "_contrib", tool_root)
    sys.path.insert(0, str(contrib))
    sys.path.insert(0, str(tool_root))
    import esptool
    if esptool.__version__ != "4.11.0":
        raise ValueError("Automatic USB install requires reviewed local esptool 4.11.0")
    # Import the existing physical-port classification; this module is inert.
    sys.path.insert(0, str(ROOT))
    from serial.tools import list_ports
    def record(port):
        haystack = " ".join(str(getattr(port, key, "") or "") for key in ("device", "description", "hwid", "manufacturer", "product")).lower()
        return {"device": port.device, "vid": port.vid, "pid": port.pid,
                "serial_number": port.serial_number, "location": port.location,
                "eligible": port.vid is not None and port.pid is not None and not any(value in haystack for value in builder.BLUETOOTH_MARKERS)}
    port = matching_port([record(value) for value in list_ports.comports()], payload.get("bridge"))
    if port != payload.get("port"):
        raise ValueError("Selected USB port changed before opening")
    if payload["operation"] == "inspect":
        return inspect_connected(esptool, port)
    if payload["operation"] == "identify":
        return connected_operation(esptool, port, payload.get("mac_address"), board_model=board_model)
    if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", str(payload.get("mac_address") or "")):
        raise ValueError("A confirmed physical board MAC is required")
    with builder.ArtifactBuildLock(ROOT / "artifacts/firmware/.build.lock"):
        digest, manifest, binaries = bundle(expected=payload.get("manifest_sha256"), board_model=board_model)
        if not payload.get("manifest_sha256"):
            raise ValueError("A pinned manifest SHA-256 is required")
        staging = Path(tempfile.mkdtemp(prefix="bound-usb-", dir=build_root))
        try:
            result = connected_operation(esptool, port, payload["mac_address"], binaries, staging, board_model=board_model)
            return {**result, "manifest_sha256": digest, "firmware_version": manifest["firmware_version"]}
        finally:
            # Only this uniquely created staging directory, never a user path.
            shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192:
            raise ValueError("USB request too large")
        result = execute(json.loads(raw))
        print("OMUSB:" + json.dumps(result), flush=True)
        return 0
    except Exception as error:
        print(f"USB operation rejected: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
