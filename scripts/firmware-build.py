#!/usr/bin/env python3
"""Build (and optionally upload) the ESP32-CAM firmware from an ASCII workspace.

The repository may live below a Chinese Windows user name. PlatformIO's Python
front end supports that, but some vendor tools still receive paths through old
Windows APIs. This helper copies only the firmware sources to a short, writable
ASCII path, keeps PlatformIO's core/packages there, and copies evidence back.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


FIRMWARE_VERSION = "0.1.0"
ENVIRONMENT = "esp32cam"
BUILD_PROFILES = {
    "esp32cam": {"board_type": "ai_thinker_esp32cam", "target_board": "AI Thinker ESP32-CAM (esp32cam)",
        "chip": "esp32", "image_chip_id": 0, "flash_size_mb": 4, "psram_size_mb": 4,
        "build_directory": "build", "artifact_subdirectory": None,
        "flash_offsets": {"bootloader.bin": 0x1000, "partitions.bin": 0x8000,
                          "boot_app0.bin": 0xE000, "firmware.bin": 0x10000}},
    "seeed_xiao_esp32s3": {"board_type": "xiao_esp32s3_sense", "target_board": "Seeed XIAO ESP32S3 Sense",
        "chip": "esp32s3", "image_chip_id": 9, "flash_size_mb": 8, "psram_size_mb": 8,
        "build_directory": "build-xiao-esp32s3-sense", "artifact_subdirectory": "xiao-esp32s3-sense",
        "flash_offsets": {"bootloader.bin": 0, "partitions.bin": 0x8000,
                          "boot_app0.bin": 0xE000, "firmware.bin": 0x10000}},
}
BLUETOOTH_MARKERS = ("bluetooth", "bthenum", "standard serial over bluetooth", "蓝牙")
LOCK_STALE_SECONDS = 6 * 60 * 60


class ArtifactBuildLock:
    """Small cross-process lock protecting the published firmware bundle."""

    def __init__(self, path: Path, *, timeout: float = 0.0):
        self.path = path
        self.timeout = max(0.0, float(timeout))
        self.token = uuid.uuid4().hex
        self.acquired = False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if pid == os.getpid():
            return True
        if os.name == "nt":
            # os.kill(pid, 0) calls TerminateProcess on Windows. Inspect only an
            # explicitly read-only process handle; unknown/access-denied means live.
            import ctypes
            from ctypes import wintypes

            try:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                open_process = kernel32.OpenProcess
                open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
                open_process.restype = wintypes.HANDLE
                wait = kernel32.WaitForSingleObject
                wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
                wait.restype = wintypes.DWORD
                close = kernel32.CloseHandle
                close.argtypes = (wintypes.HANDLE,)
                close.restype = wintypes.BOOL
                if pid > 0xFFFFFFFF:
                    return True  # Do not truncate an unknown identifier to a DWORD.
                handle = open_process(0x00100000 | 0x1000, False, pid)
                if not handle:
                    # With a valid positive DWORD PID and fixed rights, 87 means
                    # that process no longer exists. Every other error is unknown.
                    return ctypes.get_last_error() != 87
                try:
                    # Only WAIT_OBJECT_0 proves termination. WAIT_TIMEOUT,
                    # WAIT_FAILED and unexpected values must block cleanup.
                    return wait(handle, 0) != 0
                finally:
                    close(handle)
            except (OSError, AttributeError, ValueError):
                return True
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False

    def _break_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return False
        if age < LOCK_STALE_SECONDS:
            return False
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            pid = int(value.get("pid") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pid = 0
        if self._pid_alive(pid):
            return False
        try:
            self.path.unlink()
            return True
        except OSError:
            return False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._break_stale():
                    continue
                if time.monotonic() >= deadline:
                    raise RuntimeError("另一个固件构建正在运行；当前成功版本未被修改。")
                time.sleep(0.1)
                continue
            payload = json.dumps({"pid": os.getpid(), "token": self.token, "created_at": time.time()}).encode("utf-8")
            write_error = None
            try:
                os.write(descriptor, payload)
                os.fsync(descriptor)
            except OSError as exc:
                write_error = exc
            finally:
                os.close(descriptor)
            if write_error is not None:
                self.path.unlink(missing_ok=True)
                raise write_error
            self.acquired = True
            return self

    def __exit__(self, *_):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if self.acquired and value.get("token") == self.token:
                self.path.unlink(missing_ok=True)
        except (OSError, ValueError, json.JSONDecodeError):
            pass


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _verified_bundle(directory: Path) -> dict | None:
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return None
    names = set()
    for item in artifacts:
        if not isinstance(item, dict) or not item.get("name") or not item.get("sha256"):
            return None
        name = Path(str(item["name"])).name
        path = directory / name
        if not path.is_file() or _sha256(path) != item["sha256"]:
            return None
        names.add(name)
    if "firmware.bin" not in names or manifest.get("compile_passed") is False:
        return None
    return manifest


def _archive_current(current: Path, public_root: Path) -> Path | None:
    manifest = _verified_bundle(current)
    if not manifest:
        return None
    builds = public_root / "builds"
    builds.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"[^0-9A-Za-z]+", "", str(manifest.get("built_at") or "")) or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    destination = builds / stamp
    if destination.exists():
        destination = builds / f"{stamp}-{uuid.uuid4().hex[:8]}"
    staging = builds / f".archive-{uuid.uuid4().hex}"
    staging.mkdir(parents=False, exist_ok=False)
    try:
        archived_artifacts = []
        for item in manifest["artifacts"]:
            name = Path(str(item["name"])).name
            shutil.copy2(current / name, staging / name)
            archived_artifacts.append({**item, "name": name, "path": str(destination / name)})
        if (current / "build.log").is_file():
            shutil.copy2(current / "build.log", staging / "build.log")
        archived = {
            **manifest,
            "artifacts": archived_artifacts,
            "archived_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "build_log": str(destination / "build.log"),
            "manifest_path": str(destination / "manifest.json"),
            "build_manifest_path": str(destination / "manifest.json"),
        }
        _write_json_atomic(staging / "manifest.json", archived)
        os.replace(staging, destination)
        return destination
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _prune_success_archives(public_root: Path, keep: int = 1) -> None:
    builds = public_root / "builds"
    if not builds.is_dir():
        return
    successful = [path for path in builds.iterdir() if path.is_dir() and not path.name.startswith(".") and _verified_bundle(path)]
    successful.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    for path in successful[max(0, keep):]:
        try:
            shutil.rmtree(path)
        except OSError:
            pass


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def firmware_python(repo: Path | None = None) -> Path:
    repo = repo or repository_root()
    relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    interpreter = repo / ".venv-firmware" / relative
    if not interpreter.is_file():
        raise RuntimeError(
            "缺少隔离的 PlatformIO 环境。请先运行 scripts/bootstrap.ps1 创建 .venv-firmware。"
        )
    return interpreter


def _is_ascii(path: Path) -> bool:
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _writable_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        marker = path / f".write-{os.getpid()}-{uuid.uuid4().hex}"
        marker.write_text("ok", encoding="ascii")
        marker.unlink()
        return True
    except OSError:
        return False


def safe_build_root() -> Path:
    configured = os.environ.get("OBJECT_MEMORY_BUILD_ROOT")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    if os.name == "nt":
        public = Path(os.environ.get("PUBLIC", r"C:\Users\Public"))
        candidates.extend((public / "ObjectMemoryBuild", Path(r"C:\ObjectMemoryBuild")))
    candidates.append(Path(tempfile.gettempdir()) / "ObjectMemoryBuild")
    for candidate in candidates:
        resolved = candidate.resolve()
        if _is_ascii(resolved) and _writable_directory(resolved):
            return resolved
    raise RuntimeError("找不到可写的英文临时构建目录，请设置 OBJECT_MEMORY_BUILD_ROOT。")


def enumerate_eligible_ports() -> dict[str, dict]:
    try:
        from serial.tools import list_ports
    except ImportError as exc:
        raise RuntimeError("缺少 pyserial，无法安全验证串口。") from exc
    result: dict[str, dict] = {}
    for port in list_ports.comports():
        haystack = " ".join(
            str(value or "")
            for value in (port.device, port.description, port.hwid, port.manufacturer, port.product)
        ).lower()
        bluetooth = any(marker in haystack for marker in BLUETOOTH_MARKERS)
        eligible = port.vid is not None and port.pid is not None and not bluetooth
        result[port.device.upper() if os.name == "nt" else port.device] = {
            "device": port.device,
            "eligible": eligible,
            "description": port.description or "",
            "reason": "蓝牙虚拟串口不能用于刷写" if bluetooth else (
                "无法确认这是 USB 串口设备" if not eligible else None
            ),
        }
    return result


def validate_upload_port(port: str) -> str:
    key = port.upper() if os.name == "nt" else port
    found = enumerate_eligible_ports().get(key)
    if not found:
        raise RuntimeError("所选串口当前不存在，请重新检测设备。")
    if not found["eligible"]:
        raise RuntimeError(found["reason"] or "所选串口不能用于刷写。")
    return found["device"]


def platformio_python_prefix(interpreter: Path) -> list[str]:
    if os.name != "nt":
        return [str(interpreter), "-m", "platformio"]
    # Python 3.12's optional WMI OS-version query can block before PIO starts.
    # Only this build process uses platform's real WinAPI/ver fallback; neither
    # Windows services nor site-packages are changed and no version is forged.
    # The code is constant: project paths, environment and port remain argv.
    entry = ("import sys; sys.modules['_wmi'] = None; import runpy; "
             "runpy.run_module('platformio', run_name='__main__')")
    return [str(interpreter), "-B", "-c", entry]


def platformio_command(project_dir: Path, *, target: str | None = None,
                       port: str | None = None, clean: bool = False,
                       interpreter: Path | None = None, environment: str = ENVIRONMENT) -> list[str]:
    if environment not in BUILD_PROFILES or target not in {None, "upload"}:
        raise ValueError("Only a fixed ObjectMemory firmware environment/target is supported")
    pio_python = interpreter or firmware_python()
    command = [*platformio_python_prefix(pio_python), "run", "--project-dir", str(project_dir),
               "--environment", environment]
    if clean:
        command.extend(("--target", "clean"))
    elif target:
        command.extend(("--target", target))
    if port:
        command.extend(("--upload-port", port))
    return command


def _copy_source(source: Path, destination: Path) -> None:
    ignore = shutil.ignore_patterns(".pio", "build", "build-*", ".build-*", "*.log", "__pycache__")
    shutil.copytree(source, destination, ignore=ignore)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _platformio_version(interpreter: Path, env: dict[str, str]) -> str:
    completed = subprocess.run(
        [*platformio_python_prefix(interpreter), "--version"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", timeout=15, shell=False, check=False,
        env=env,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unavailable"


def _memory_usage(build_log: Path) -> dict[str, dict[str, int | float] | None]:
    """Extract PlatformIO's actual size report instead of inventing estimates."""
    try:
        text = build_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"ram": None, "flash": None}
    result: dict[str, dict[str, int | float] | None] = {"ram": None, "flash": None}
    pattern = re.compile(
        r"^(RAM|Flash):\s+\[[^\]]+\]\s+([0-9.]+)%\s+"
        r"\(used\s+(\d+)\s+bytes\s+from\s+(\d+)\s+bytes\)", re.MULTILINE,
    )
    for kind, percent, used, total in pattern.findall(text):
        result[kind.lower()] = {
            "used_bytes": int(used),
            "total_bytes": int(total),
            "percent": float(percent),
        }
    return result


def _run_and_tee(command: list[str], cwd: Path, env: dict[str, str], log_handle) -> int:
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    assert process.stdout is not None
    for line in process.stdout:
        line = line.rstrip("\r\n")
        print(line, flush=True)
        log_handle.write(line + "\n")
        log_handle.flush()
    return process.wait()


def execute(*, flash: bool, port: str | None, clean: bool, environment: str = ENVIRONMENT) -> dict:
    if environment not in BUILD_PROFILES:
        raise ValueError("Only a fixed ObjectMemory firmware environment is supported")
    if flash and environment != ENVIRONMENT:
        raise RuntimeError("XIAO 安装必须使用核对 ROM 身份、芯片和固件哈希的 USB 安装流程；构建脚本不直接刷写。")
    repo = repository_root()
    common_public_root = repo / "artifacts" / "firmware"
    subdirectory = BUILD_PROFILES[environment]["artifact_subdirectory"]
    public_root = common_public_root / subdirectory if subdirectory else common_public_root
    with ArtifactBuildLock(common_public_root / ".build.lock"):
        return _execute_locked(repo, public_root, flash=flash, port=port, clean=clean, environment=environment)


def _execute_locked(repo: Path, public_root: Path, *, flash: bool, port: str | None, clean: bool,
                    environment: str = ENVIRONMENT) -> dict:
    profile = BUILD_PROFILES[environment]
    firmware = repo / "firmware" / "esp32cam"
    current = firmware / profile["build_directory"]
    build_root = safe_build_root()
    workspace = build_root / "workspaces" / f"job-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    source_copy = workspace / "firmware"
    core_dir = build_root / "platformio-core"
    workspace.mkdir(parents=True, exist_ok=False)
    core_dir.mkdir(parents=True, exist_ok=True)
    _copy_source(firmware, source_copy)

    selected_port = validate_upload_port(port) if flash and port else None
    if flash and not selected_port:
        raise RuntimeError("刷写必须指定已检测到的 USB 串口。")

    env = os.environ.copy()
    env["PLATFORMIO_CORE_DIR"] = str(core_dir)
    env["PLATFORMIO_DISABLE_PROGRESSBAR"] = "true"
    env["PYTHONUTF8"] = "1"
    isolated_interpreter = firmware_python(repo)
    command = platformio_command(
        source_copy,
        target="upload" if flash else None,
        port=selected_port,
        clean=clean,
        interpreter=isolated_interpreter,
        environment=environment,
    )
    print(f"ObjectMemory firmware workspace: {workspace}", flush=True)
    print("PlatformIO command uses an argument array; source and tool cache are in an ASCII path.", flush=True)
    try:
        staged_log = workspace / "build.log"
        with staged_log.open("w", encoding="utf-8", newline="\n") as log_handle:
            log_handle.write("command=" + json.dumps(command, ensure_ascii=False) + "\n")
            return_code = _run_and_tee(command, workspace, env, log_handle)
        if return_code != 0:
            raise RuntimeError(f"PlatformIO {'upload' if flash else 'build'} failed with exit code {return_code}")

        if clean:
            return {
                "firmware_version": FIRMWARE_VERSION,
                "environment": environment,
                "status": "clean_succeeded",
                "compile_passed": False,
                "published": False,
                "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }

        build_output = source_copy / ".pio" / "build" / environment
        publish_stage = firmware / f".build-stage-{uuid.uuid4().hex}"
        publish_stage.mkdir(parents=False, exist_ok=False)
        copied: list[dict] = []
        try:
            for name in ("firmware.bin", "bootloader.bin", "partitions.bin", "firmware.elf"):
                source_artifact = build_output / name
                if source_artifact.exists():
                    staged_target = publish_stage / name
                    shutil.copy2(source_artifact, staged_target)
                    copied.append({"name": name, "path": str(current / name), "sha256": _sha256(staged_target),
                                   "size": staged_target.stat().st_size})
            # A fixed-bundle USB install must not fetch an unmanifested OTA-data
            # initializer from a mutable tool cache at flash time.
            boot_app = core_dir / "packages" / "framework-arduinoespressif32" / "tools" / "partitions" / "boot_app0.bin"
            if not boot_app.is_file():
                raise RuntimeError("Missing boot_app0.bin; refusing to publish an incomplete ESP32 bundle.")
            shutil.copy2(boot_app, publish_stage / "boot_app0.bin")
            copied.append({"name": "boot_app0.bin", "path": str(current / "boot_app0.bin"),
                           "sha256": _sha256(publish_stage / "boot_app0.bin"), "size": boot_app.stat().st_size})
            if not any(item["name"] == "firmware.bin" for item in copied):
                raise RuntimeError("PlatformIO 返回成功，但没有找到 firmware.bin。")
            if environment != ENVIRONMENT and not set(profile["flash_offsets"]).issubset(item["name"] for item in copied):
                raise RuntimeError("Missing fixed XIAO flash artifacts; refusing to publish an incomplete bundle.")
            shutil.copy2(staged_log, publish_stage / "build.log")
            usage = _memory_usage(staged_log)
            public_manifest_path = public_root / "manifest.json"
            manifest = {
                "firmware_version": FIRMWARE_VERSION,
                "platformio_core": _platformio_version(isolated_interpreter, env),
                "environment": environment,
                "target_board": profile["target_board"],
                "board_type": profile["board_type"],
                "board_model": profile["board_type"],
                "chip": profile["chip"],
                "image_chip_id": profile["image_chip_id"],
                "flash_size_mb": profile["flash_size_mb"],
                "psram_size_mb": profile["psram_size_mb"],
                "flash_offsets": dict(profile["flash_offsets"]),
                "camera_auth": "hmac-sha256-v1",
                "platform": "espressif32@7.0.1",
                "framework": "arduino@2.0.17",
                "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "status": "succeeded",
                "compile_passed": True,
                "published": True,
                "flash_attempted": flash,
                "physical_flash_performed": flash,
                "flash_port": selected_port if flash else None,
                "ram_usage": usage["ram"],
                "flash_usage": usage["flash"],
                "artifacts": copied,
                "build_log": str(current / "build.log"),
                "manifest_path": str(public_manifest_path),
                "build_manifest_path": str(current / "manifest.json"),
            }
            _write_json_atomic(publish_stage / "manifest.json", manifest)

            # Archive only a fully hash-verified prior current bundle.  Building
            # and archiving happen under the same cross-process lock.
            _archive_current(current, public_root)
            rollback = firmware / f".build-rollback-{uuid.uuid4().hex}"
            previous_public = public_manifest_path.read_bytes() if public_manifest_path.is_file() else None
            moved_current = False
            published = False
            try:
                if current.exists():
                    os.replace(current, rollback)
                    moved_current = True
                os.replace(publish_stage, current)
                _write_json_atomic(public_manifest_path, manifest)
                published = True
            except Exception:
                if current.exists():
                    shutil.rmtree(current, ignore_errors=True)
                if moved_current and rollback.exists():
                    try:
                        os.replace(rollback, current)
                    except OSError as restore_error:
                        # Leave the rollback directory intact for manual or
                        # next-start recovery; never delete the last good build.
                        raise RuntimeError(f"发布失败且自动恢复失败；旧成功版本保留在 {rollback}") from restore_error
                if previous_public is None:
                    public_manifest_path.unlink(missing_ok=True)
                else:
                    temporary = public_manifest_path.with_name(f".{public_manifest_path.name}.{uuid.uuid4().hex}.tmp")
                    temporary.write_bytes(previous_public)
                    os.replace(temporary, public_manifest_path)
                raise
            finally:
                if published and rollback.exists():
                    shutil.rmtree(rollback, ignore_errors=True)
            _prune_success_archives(public_root, keep=1)
            return manifest
        finally:
            if publish_stage.exists():
                shutil.rmtree(publish_stage, ignore_errors=True)
    finally:
        if os.environ.get("OBJECT_MEMORY_KEEP_BUILD_WORKSPACE") != "1":
            # Only remove the unique directory created above, never the configured base.
            shutil.rmtree(workspace, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build ObjectMemory ESP32-CAM firmware safely")
    parser.add_argument("--flash", action="store_true", help="upload after compiling")
    parser.add_argument("--port", help="detected physical USB serial port")
    parser.add_argument("--clean", action="store_true", help="run PlatformIO clean only")
    parser.add_argument("--environment", choices=tuple(BUILD_PROFILES), default=ENVIRONMENT,
                        help="fixed camera board build; default preserves AI Thinker")
    args = parser.parse_args()
    if args.port and not args.flash:
        parser.error("--port is only valid with --flash")
    try:
        result = execute(flash=args.flash, port=args.port, clean=args.clean, environment=args.environment)
    except Exception as exc:  # CLI boundary: return exact, non-secret failure evidence.
        message = re.sub(r"OM-[A-Z0-9]{4,}", "OM-******", str(exc))
        print(f"ERROR: {message}", file=sys.stderr, flush=True)
        return 1
    print("OMRESULT:" + json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
