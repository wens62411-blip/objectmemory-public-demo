"""Bounded real-file status hashing; USB and physical firmware are not touched."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from apps.api.app.firmware import FirmwareService


@pytest.fixture
def manifest_service(tmp_path, monkeypatch):
    service = FirmwareService(tmp_path / "test.sqlite", None, tmp_path / "data", runtime_mode="TEST")
    service.project_root = tmp_path / "repo"
    build = service.project_root / "firmware/esp32cam/build"
    public = service.project_root / "artifacts/firmware"
    build.mkdir(parents=True)
    public.mkdir(parents=True)
    artifact = build / "firmware.bin"
    artifact.write_bytes(b"real-file-hash-contract" * 10000)
    manifest = public / "manifest.json"
    manifest.write_text(json.dumps({"artifacts": [{"name": artifact.name,
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(), "size": artifact.stat().st_size}]}), encoding="utf-8")
    monkeypatch.setattr(service, "discover_ports", lambda: [])
    try:
        yield service, manifest, artifact
    finally:
        service.shutdown()


class WatchedReader:
    def __init__(self, handle, observe):
        self.handle, self.observe = handle, observe

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def read(self, size=-1):
        self.observe(size)
        return self.handle.read(size)

    def readinto(self, buffer):
        self.observe(len(buffer))
        return self.handle.readinto(buffer)

    def fileno(self):
        return self.handle.fileno()


def watch_artifact(monkeypatch, artifact, observe):
    original = Path.open
    def opened(path, *args, **kwargs):
        handle = original(path, *args, **kwargs)
        return WatchedReader(handle, observe) if path == artifact else handle
    monkeypatch.setattr(Path, "open", opened)


def test_artifact_hashing_uses_bounded_buffer(manifest_service, monkeypatch):
    service, _, artifact = manifest_service
    requests = []
    watch_artifact(monkeypatch, artifact, requests.append)
    result = service.firmware_manifest()
    assert result["manifest_hashes_verified"] is True
    assert requests and 0 < max(requests) <= 64 * 1024


@pytest.mark.parametrize("error", [MemoryError, OSError])
def test_artifact_read_failure_is_unverified_not_server_error(manifest_service, monkeypatch, error):
    service, _, artifact = manifest_service
    def fail(_size):
        raise error("injected bounded read failure")
    watch_artifact(monkeypatch, artifact, fail)
    result = service.firmware_manifest()
    assert result["compile_passed"] is False
    assert result["manifest_hashes_verified"] is False
    assert result["artifacts"][0]["hash_verified"] is False
    assert result["artifacts"][0]["verification_error"]


def test_concurrent_status_requests_do_not_hash_in_parallel(manifest_service, monkeypatch):
    service, _, artifact = manifest_service
    state = {"active": 0, "peak": 0}
    guard = threading.Lock()
    def observe(_size):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(.005)
        with guard:
            state["active"] -= 1
    watch_artifact(monkeypatch, artifact, observe)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: service.firmware_manifest(), range(6)))
    assert all(result["compile_passed"] for result in results)
    assert state["peak"] == 1


def test_same_size_artifact_change_is_not_hidden_by_status_cache(manifest_service):
    service, _, artifact = manifest_service
    assert service.firmware_manifest()["compile_passed"] is True
    artifact.write_bytes(b"x" * artifact.stat().st_size)
    assert service.firmware_manifest()["compile_passed"] is False


@pytest.mark.parametrize("artifacts", [None, "invalid", [None], [{"name": 12}], [{"name": "../firmware.bin"}], [{}] * 17])
def test_malformed_artifact_list_fails_closed(manifest_service, artifacts):
    service, manifest, _ = manifest_service
    manifest.write_text(json.dumps({"artifacts": artifacts}), encoding="utf-8")
    result = service.firmware_manifest()
    assert result["compile_passed"] is False
    assert result["manifest_hashes_verified"] is False


def test_oversized_manifest_is_bounded_and_unverified(manifest_service):
    service, manifest, _ = manifest_service
    manifest.write_bytes(b" " * (256 * 1024 + 1))
    result = service.firmware_manifest()
    assert result["compile_passed"] is False
    assert result["manifest_verification_error"] == "manifest_too_large"


def test_manifest_is_read_once_for_both_contents_and_receipt_digest(manifest_service, monkeypatch):
    service, manifest, _ = manifest_service
    original = Path.open
    reads = []
    def opened(path, *args, **kwargs):
        if path == manifest:
            reads.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", opened)
    assert service.firmware_manifest()["compile_passed"] is True
    assert len(reads) == 1


def test_invalid_unicode_hash_fails_closed(manifest_service):
    service, manifest, _ = manifest_service
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["artifacts"][0]["sha256"] = "未验证"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    assert service.firmware_manifest()["compile_passed"] is False


def test_artifact_growth_is_bounded_before_read(manifest_service, monkeypatch):
    service, _, artifact = manifest_service
    with artifact.open("wb") as handle:
        handle.truncate(64 * 1024 * 1024 + 1)
    reads = []
    watch_artifact(monkeypatch, artifact, reads.append)
    result = service.firmware_manifest()
    assert result["compile_passed"] is False
    assert reads == []


def test_failed_read_releases_lock_and_next_poll_can_verify(manifest_service, monkeypatch):
    service, _, artifact = manifest_service
    def fail(_size):
        raise MemoryError("injected read pressure")
    with monkeypatch.context() as patch:
        watch_artifact(patch, artifact, fail)
        assert service.firmware_manifest()["compile_passed"] is False
    assert service.firmware_manifest()["compile_passed"] is True
