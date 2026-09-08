#!/usr/bin/env python3
"""Explicit one-model preparation; --verify is offline and never downloads."""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import time
import urllib.request
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.vision.detectors.appearance import AppearanceEncoder, DEFAULT_MODEL_PATH, MANIFEST, file_sha256


def versions() -> dict[str, str]:
    return {distribution.metadata["Name"]: distribution.version for distribution in importlib.metadata.distributions()}


def pip_check() -> dict:
    completed = subprocess.run([sys.executable, "-B", "-m", "pip", "check"], capture_output=True, text=True, check=False)
    return {"exit_code": completed.returncode, "output": (completed.stdout + completed.stderr).strip()}


def install_runtime(wheel_dir: Path | None = None) -> dict:
    if Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        raise RuntimeError("Installation is allowed only in this project's .venv")
    from packaging.requirements import Requirement
    before = versions()
    before_check = pip_check()
    write_json(ROOT / "data" / "verification" / "appearance-dependencies-before.json",
               {"versions": before, "pip_check": before_check})
    if before_check["exit_code"]:
        raise RuntimeError("Existing environment has broken dependencies; refusing to change it")
    missing = []
    for line in (ROOT / "requirements-appearance.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        try:
            installed = importlib.metadata.version(requirement.name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(str(requirement))
            continue
        if not requirement.specifier.contains(installed):
            raise RuntimeError(f"Refusing to upgrade/downgrade existing {requirement.name}=={installed}")
    if missing:
        source = ["--no-index", "--find-links", str(wheel_dir.resolve())] if wheel_dir else ["--index-url", "https://pypi.org/simple"]
        subprocess.run([sys.executable, "-B", "-m", "pip", "install", "--no-deps", "--no-cache-dir",
                        "--disable-pip-version-check", "--only-binary=:all:", "--timeout", "30", "--retries", "1",
                        *source, *missing], check=True, timeout=180)
    after = versions()
    changed = {name: {"before": version, "after": after.get(name)} for name, version in before.items() if after.get(name) != version}
    after_check = pip_check()
    result = {"before": before, "after": after, "added": {name: version for name, version in after.items() if name not in before},
              "changed_or_removed": changed, "pip_check_before": before_check, "pip_check_after": after_check,
              "existing_packages_unchanged": not changed}
    write_json(ROOT / "data" / "verification" / "appearance-dependencies.json", result)
    if changed or after_check["exit_code"]:
        raise RuntimeError("Dependency verification failed: " + json.dumps(result, ensure_ascii=False))
    return result


def powershell_fetch(url: str, destination: Path, maximum_bytes: int, *, progress: bool = False) -> None:
    """Explicit Windows transport for hosts whose Python TLS route cannot reach the official source."""
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        raise RuntimeError("PowerShell transport requested but no PowerShell executable is available")
    if not url.startswith("https://"):
        raise ValueError("Only HTTPS resource URLs are accepted")
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    command = "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri " + quote(url) + " -OutFile " + quote(destination) + " -TimeoutSec 120"
    encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
    process = subprocess.Popen([executable, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    started = time.monotonic()
    try:
        while True:
            received = destination.stat().st_size if destination.exists() else 0
            if received > maximum_bytes:
                raise ValueError("Resource exceeds the pinned download size bound")
            if time.monotonic() - started > 180:
                raise TimeoutError("Official resource download exceeded 180 seconds")
            if progress:
                publish_status("downloading", received)
            try:
                _stdout, stderr = process.communicate(timeout=1)
                if process.returncode:
                    raise RuntimeError("Official resource download failed: " + stderr[-2000:])
                if not destination.is_file() or destination.stat().st_size > maximum_bytes:
                    raise ValueError("Resource is absent or exceeds its size bound")
                return
            except subprocess.TimeoutExpired:
                pass
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


def download_model(destination: Path, transport: str = "urllib") -> bool:
    destination = destination.resolve()
    model_root = (ROOT / "data" / "models").resolve()
    if not destination.is_relative_to(model_root) or destination.suffix != ".onnx":
        raise ValueError("Model destination must be an .onnx file below project data/models")
    if destination.exists():
        publish_status("verifying", destination.stat().st_size)
        if destination.stat().st_size != MANIFEST["bytes"] or file_sha256(destination) != MANIFEST["sha256"]:
            raise ValueError("Existing model has a different hash; preserved without overwriting")
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".partial")
    url = f"https://huggingface.co/{MANIFEST['model_id']}/resolve/{MANIFEST['revision']}/{MANIFEST['repository_file']}"
    digest = hashlib.sha256()
    received = 0
    reported_at = 0.0
    publish_status("downloading", 0)
    try:
        if transport == "powershell":
            powershell_fetch(url, temporary, MANIFEST["bytes"], progress=True)
            received = temporary.stat().st_size
            actual_hash = file_sha256(temporary)
        else:
            with urllib.request.urlopen(url, timeout=60) as response, temporary.open("xb") as output:
                if not response.geturl().startswith("https://"):
                    raise ValueError("Refusing a non-HTTPS model redirect")
                while block := response.read(1024 * 1024):
                    received += len(block)
                    if received > MANIFEST["bytes"]:
                        raise ValueError("Download exceeds the pinned model size")
                    digest.update(block)
                    output.write(block)
                    now = time.monotonic()
                    if now - reported_at >= 1.0:
                        publish_status("downloading", received)
                        reported_at = now
                output.flush()
                os.fsync(output.fileno())
            actual_hash = digest.hexdigest()
        publish_status("verifying", received)
        if received != MANIFEST["bytes"] or actual_hash != MANIFEST["sha256"]:
            raise ValueError("Downloaded model size/SHA-256 differs from the pinned LFS object")
        if destination.exists():
            raise RuntimeError("Model destination appeared during download; preserving it")
        # Atomic, no-overwrite publication: the target is never a partial model.
        os.link(temporary, destination)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".partial")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_status(status: str, downloaded_bytes: int = 0, *, error: str | None = None, **details) -> None:
    write_json(ROOT / "data" / "models" / "appearance-status.json", {
        "status": status, "downloaded_bytes": int(downloaded_bytes), "total_bytes": MANIFEST["bytes"],
        "model_id": MANIFEST["model_id"], "model_version": MANIFEST["model_version"],
        "sha256": MANIFEST["sha256"], "updated_at": time.time(), "error": error, **details,
    })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="Offline verification only; refuses to install/download")
    parser.add_argument("--warmup", action="store_true", help="Run one synthetic crop locally; not a real-object claim")
    parser.add_argument("--install-runtime", action="store_true", help="Add fixed missing CPU dependencies with --no-deps")
    parser.add_argument("--wheel-dir", type=Path, help="Use a locally SHA-verified wheel directory; pip remains offline")
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--download-transport", choices=("urllib", "powershell"),
                        default="powershell" if os.name == "nt" else "urllib")
    args = parser.parse_args()
    if args.verify and args.install_runtime:
        parser.error("--verify cannot be combined with --install-runtime")
    report = {"success": False, "verify_only": args.verify, "model_manifest": MANIFEST,
              "model_path": str(args.model_path), "downloaded": False, "private_images_uploaded": False,
              "physical_camera_used": False, "download_transport": args.download_transport, "started_at": time.time()}
    report_path = ROOT / "data" / "verification" / ("appearance-model-verify.json" if args.verify else "appearance-model-preparation.json")
    try:
        publish_status("verifying", args.model_path.stat().st_size if args.model_path.is_file() else 0,
                       phase="runtime_dependency_preparation" if args.install_runtime else "model_verification")
        if args.install_runtime:
            report["runtime_dependency_change"] = install_runtime(args.wheel_dir)
        if not args.verify:
            report["downloaded"] = download_model(args.model_path, args.download_transport)
            license_path = args.model_path.with_suffix(".LICENSE.txt")
            if not license_path.exists():
                temporary_license = license_path.with_name(license_path.name + "." + uuid.uuid4().hex + ".partial")
                try:
                    if args.download_transport == "powershell":
                        powershell_fetch(MANIFEST["license"]["upstream_license_url"], temporary_license, 64 * 1024)
                        license_bytes = temporary_license.read_bytes()
                    else:
                        with urllib.request.urlopen(MANIFEST["license"]["upstream_license_url"], timeout=30) as response:
                            license_bytes = response.read(64 * 1024)
                        temporary_license.write_bytes(license_bytes)
                    if b"Apache License" not in license_bytes[:200] or b"Version 2.0" not in license_bytes[:300]:
                        raise ValueError("Unexpected upstream license contents")
                    os.link(temporary_license, license_path)
                finally:
                    temporary_license.unlink(missing_ok=True)
        # Verification and inference deliberately have no Python socket access.
        publish_status("loading", args.model_path.stat().st_size if args.model_path.is_file() else 0)
        with patch("socket.socket", side_effect=RuntimeError("Network forbidden during local model verification")), \
             patch("socket.create_connection", side_effect=RuntimeError("Network forbidden during local model verification")):
            encoder = AppearanceEncoder(args.model_path)
            if not encoder.health()["available"]:
                raise RuntimeError(encoder.health()["error"])
            if args.warmup:
                publish_status("warming", MANIFEST["bytes"])
                import numpy as np
                crop = np.zeros((256, 320, 3), np.uint8)
                crop[:, :, 1] = np.arange(320, dtype=np.uint16)[None, :] % 256
                vector = encoder.encode(crop)
                report["warmup"] = {"input": "synthetic_gradient_not_real_object", "dimension": len(vector),
                                    "l2_norm": float(np.linalg.norm(vector)), "encode_ms": encoder.health()["last_encode_ms"]}
            report["encoder_health"] = encoder.health()
            report["network_blocked_verification"] = True
        report.update(success=True, sha256=file_sha256(args.model_path), bytes=args.model_path.stat().st_size)
        publish_status("ready", report["bytes"], latency_ms=report.get("warmup", {}).get("encode_ms"),
                       dimension=encoder.dimension, provider="CPUExecutionProvider")
        metadata_path = args.model_path.with_suffix(".metadata.json")
        if not args.verify:
            write_json(metadata_path, {"manifest": MANIFEST, "sha256_verified": report["sha256"], "bytes_verified": report["bytes"]})
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        publish_status("failed", args.model_path.stat().st_size if args.model_path.is_file() else 0, error=report["error"])
    report["finished_at"] = time.time()
    write_json(report_path, report)
    print(json.dumps({"success": report["success"], "report": str(report_path), "error": report.get("error"),
                      "encoder_health": report.get("encoder_health"), "warmup": report.get("warmup")}, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
