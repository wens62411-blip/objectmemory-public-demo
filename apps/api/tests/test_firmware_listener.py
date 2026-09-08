"""Listener preflight tests; none perform USB writes or attest physical boards."""
from __future__ import annotations

import importlib.util
import ipaddress
import os
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi import HTTPException
from fastapi.testclient import TestClient

from apps.api.app.firmware import FirmwareService, InstallRequest, ProvisionRequest
from apps.api.app.main import create_app, lan_addresses


def launcher():
    spec = importlib.util.spec_from_file_location("listener_test_launcher", Path(__file__).resolve().parents[3] / "scripts" / "serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def firmware(tmp_path, provider=None):
    return FirmwareService(tmp_path / "db.sqlite", None, tmp_path / "data", listener_info_provider=provider)


def install_request():
    return InstallRequest(port="COM7", ssid="Home", password="", backend_url="http://192.0.2.222:8018", device_name="Camera", room_name="Room")


def provision_request():
    return ProvisionRequest(**install_request().model_dump(), pairing_code="OM-ABC234", device_id="omcam-test")


def test_session_does_not_advertise_lan_from_environment_intent(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_ALLOW_LAN", "1")
    monkeypatch.setenv("OM_PORT", "8047")
    with TestClient(create_app(data_dir=tmp_path / "data", testing=True)) as client:
        result = client.get("/api/session").json()
        assert result["lan_enabled"] is False
        assert result["listener_verified"] is False
        assert result["lan_urls"] == []


@pytest.mark.parametrize("operation", ["install", "provision"])
def test_localhost_preflight_rejects_before_enrollment_job_or_port_access(tmp_path, monkeypatch, operation):
    fw = firmware(tmp_path)
    touched = []
    for method in ("validated_port", "create_enrollment", "_create_job", "_builder_process", "_open_serial"):
        monkeypatch.setattr(fw, method, lambda *args, name=method, **kwargs: touched.append(name))
    with pytest.raises(HTTPException) as raised:
        if operation == "install":fw.start_install(install_request())
        else:fw.start_provision(provision_request())
    assert raised.value.status_code == 409
    assert "-Lan" in str(raised.value.detail)
    assert touched == []
    with fw.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM firmware_jobs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM device_enrollments").fetchone()[0] == 0


def test_plain_build_and_flash_do_not_require_lan(tmp_path, monkeypatch):
    fw = firmware(tmp_path)
    monkeypatch.setattr(fw, "validated_port", lambda port: port)
    monkeypatch.setattr(fw, "_create_job", lambda kind, *args: {"job_type": kind})
    assert fw.start_build()["job_type"] == "build"
    assert fw.start_flash("COM7")["job_type"] == "flash"


@pytest.mark.parametrize("operation", ["install", "provision"])
def test_queued_worker_rechecks_listener_before_hardware_mutation(tmp_path, monkeypatch, operation):
    fw = firmware(tmp_path)
    calls = []
    monkeypatch.setattr(fw, "_builder_process", lambda *args, **kwargs: calls.append("flash"))
    monkeypatch.setattr(fw, "_open_serial", lambda *args: calls.append("serial"))
    with pytest.raises(HTTPException):
        if operation == "install":fw._install_worker("not-created", install_request().model_dump())
        else:fw._provision_worker("not-created", provision_request().model_dump())
    assert calls == []


def test_launcher_requires_owned_listening_socket_not_requested_host():
    module = launcher()
    server = SimpleNamespace(started=True, should_exit=False, servers=[])
    assert module.listener_metadata(server, "0.0.0.0", 8018)["listener_verified"] is False
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server.servers = [SimpleNamespace(sockets=[listener])]
        assert module.listener_metadata(server, "0.0.0.0", 8018)["listener_verified"] is False
        listener.listen()
        state = module.listener_metadata(server, "0.0.0.0", 8018)
        assert state["listener_bindings"] == [{"host": "127.0.0.1", "port": listener.getsockname()[1]}]
        server.should_exit = True
        assert module.listener_metadata(server, "0.0.0.0", 8018)["listener_verified"] is False


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
def test_real_uvicorn_session_and_same_process_backend_preflight(tmp_path, host):
    """Actual FastAPI + Uvicorn sockets + SQLite, not a mocked HTTP response."""
    module = launcher()
    server = None
    app = create_app(data_dir=tmp_path / "data", testing=False, runtime_mode="REAL",
                     listener_info_provider=lambda: module.listener_metadata(server, host, 0))
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=0, access_log=False, log_level="error"))
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 8
        while not server.started and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        with httpx.Client(trust_env=False) as client:
            response = client.get(f"http://127.0.0.1:{port}/api/session", timeout=3)
            preflight_response=client.get(f"http://127.0.0.1:{port}/api/acceptance/preflight?camera_id=not-started&companion_url=http://192.0.2.222:8047",timeout=3)
        assert preflight_response.status_code==200
        preflight=preflight_response.json()
        assert preflight['ready'] is False
        assert 'pairing_pin' not in preflight
        assert response.status_code == 200
        session = response.json()
        assert session["listener_verified"] is True
        assert session["lan_enabled"] is (host == "0.0.0.0")
        fw = app.state.firmware_service
        if host == "127.0.0.1":
            assert session["lan_urls"] == []
            assert preflight['companion_url'] is None
            with pytest.raises(HTTPException):fw._verify_backend_listener(f"http://192.0.2.222:{port}")
        else:
            assert session["lan_url_status"] == "candidate_device_reachability_unverified"
            assert preflight['companion_url']==session['lan_urls'][0]+'/companion/validation-marker'
            assert preflight['companion_access_status']=='candidate_device_reachability_unverified'
            for invalid in (f"http://192.0.2.222:{port}", f"http://192.0.2.222:{port + 1}"):
                with pytest.raises(HTTPException):fw._verify_backend_listener(invalid)
            candidates = []
            for value in lan_addresses():
                try:address = ipaddress.ip_address(value)
                except ValueError:continue
                if address.version == 4 and address.is_private and not address.is_loopback:
                    candidates.append(str(address))
            assert candidates, "This live LAN test needs one real assigned private IPv4 address"
            backend = f"http://{candidates[0]}:{port}"
            assert backend in session["lan_urls"]
            assert fw._verify_backend_listener(backend) == backend
            for suffix in ("/api", "/?token=unused"):
                with pytest.raises(HTTPException):fw._verify_backend_listener(backend + suffix)
            original = fw.listener_info_provider
            fw.listener_info_provider = lambda: {**original(), "process_id": os.getpid() + 1}
            with pytest.raises(HTTPException):fw._verify_backend_listener(backend)
    finally:
        server.should_exit = True
        worker.join(timeout=8)
        assert not worker.is_alive()
