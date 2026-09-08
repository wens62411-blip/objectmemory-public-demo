"""Real local HTTP/MJPEG and SQLite tests; synthetic frames, not physical hardware."""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from apps.api.app.firmware import FirmwareService, camera_read_token, hash_device_token
from apps.api.app.main import Runtime, create_app, public_camera_config
from apps.api.tests.test_firmware import claim_input, heartbeat_input
from services.vision.camera_sources import Esp32CamSource
from services.vision.tests.test_network_and_capture import _Esp32Handler


TOKEN = "synthetic-device-token-for-contract-only"
READ_TOKEN = camera_read_token(hash_device_token(TOKEN))


class AuthenticatedHandler(_Esp32Handler):
    def _json(self, value):
        if "camera_status" in value:value["camera_auth"] = getattr(self.server,"auth_capability","hmac-sha256-v1")
        if "device_id" in value:value["device_id"] = "omcam-test"
        if getattr(self.server,"echo_secret",False):
            value.update(diagnostic_note=self.server.read_token,firmware_version=self.server.read_token)
        if getattr(self.server,"oversized_json",False):value["large"]="x"*(65*1024)
        super()._json(value)

    def do_GET(self):
        self.server.requests.append((self.path,self.headers.get("Authorization")))
        if self.path != "/health" and self.headers.get("Authorization") != "Bearer " + self.server.read_token:
            self.send_error(401, "Camera read authorization required")
            return
        mode=getattr(self.server,"stream_mode",None)
        if self.path=="/stream" and mode:
            self.send_response(200);self.send_header("Content-Type","multipart/x-mixed-replace; boundary=frame")
            if mode=="compressed":self.send_header("Content-Encoding","gzip")
            self.end_headers()
            try:
                if mode=="drip":
                    for _ in range(100):self.wfile.write(b"-");self.wfile.flush();time.sleep(.02)
                elif mode=="truncated":self.wfile.write(b"\xff\xd8truncated");self.wfile.flush()
                elif mode=="compressed":self.wfile.write(b"not-an-identity-body");self.wfile.flush()
                elif mode=="one_frame":
                    payload=b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"+self.jpeg+b"\r\n"
                    for offset in range(0,len(payload),128):self.wfile.write(payload[offset:offset+128]);self.wfile.flush()
                    self.server.release_rest.wait(1.5)
            except (BrokenPipeError,ConnectionResetError):pass
            return
        super().do_GET()


@contextmanager
def camera_server():
    server = ThreadingHTTPServer(("127.0.0.1",0),AuthenticatedHandler)
    server.requests=[];server.read_token=READ_TOKEN
    server.release_rest=threading.Event()
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:yield server,f"http://127.0.0.1:{server.server_port}"
    finally:server.release_rest.set();server.shutdown();server.server_close();worker.join(2)


def test_camera_token_is_purpose_separated_and_matches_shared_hmac_contract():
    expected=hmac.new(hashlib.sha256(TOKEN.encode()).digest(),b"objectmemory/camera-read/v1",hashlib.sha256).hexdigest()
    assert READ_TOKEN == expected and len(READ_TOKEN)==64
    assert READ_TOKEN not in {TOKEN,hash_device_token(TOKEN)}
    for invalid in (None,"",TOKEN,"not-a-valid-hash"):
        with pytest.raises(ValueError):camera_read_token(invalid)


def test_authenticated_mjpeg_uses_headers_not_config_url_or_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY","http://127.0.0.1:1");monkeypatch.setenv("NO_PROXY","")
    with camera_server() as (server,base):
        source=Esp32CamSource(base+"/stream",{"capture_url":base+"/capture","board_model":"xiao_esp32s3_sense"})
        source.set_camera_read_token(READ_TOKEN)
        try:
            assert source.connect(),source.health_check()
            frame=source.read_frame()
            assert frame is not None and frame.shape[:2]==(120,160)
            assert source.test_endpoints()["video_normal"] is True
            assert {path for path,_ in server.requests} >= {"/health","/device","/stream","/capture"}
            assert all(header=="Bearer "+READ_TOKEN for _,header in server.requests)
            assert READ_TOKEN not in json.dumps(source.get_metadata())
            assert READ_TOKEN not in json.dumps(source.health_check())
            assert not source.config.get("camera_read_token") and READ_TOKEN not in source.source
            capture=source.capture
        finally:source.disconnect()
        assert not capture.isOpened()


@pytest.mark.parametrize("kind",["missing","wrong","raw_device_token_in_config"])
def test_no_authenticated_video_fallback_for_missing_wrong_or_configured_device_token(kind):
    with camera_server() as (server,base):
        source=Esp32CamSource(base+"/stream",{"capture_url":base+"/capture","board_model":"xiao_esp32s3_sense","device_token":TOKEN})
        if kind=="wrong":source.set_camera_read_token("b"*64)
        try:
            assert not source.connect()
            assert all(path not in {"/stream","/capture"} for path,_ in server.requests)
            assert all(header!="Bearer "+TOKEN for _,header in server.requests)
            assert TOKEN not in json.dumps(source.health_check())
        finally:source.disconnect()


def test_authenticated_stream_redirect_never_forwards_secret():
    with camera_server() as (sink,sink_base),camera_server() as (server,base):
        server.redirect_stream_url=sink_base+"/stream"
        source=Esp32CamSource(base+"/stream",{"capture_url":base+"/capture"})
        source.set_camera_read_token(READ_TOKEN)
        try:
            assert not source.connect()
            assert sink.requests==[]
            assert READ_TOKEN not in json.dumps(source.health_check())
        finally:source.disconnect()


def test_old_unprotected_firmware_is_explicitly_rejected_not_silently_read():
    with camera_server() as (server,base):
        server.auth_capability="legacy"
        source=Esp32CamSource(base+"/stream",{"capture_url":base+"/capture"})
        source.set_camera_read_token(READ_TOKEN)
        try:
            assert not source.connect()
            assert "旧固件" in source.health_check()["error"]
            assert all(path=="/health" for path,_ in server.requests)
        finally:source.disconnect()


def test_real_firmware_probe_uses_derived_header_and_camera_token_cannot_heartbeat(tmp_path,monkeypatch):
    with camera_server() as (server,base):
        fw=FirmwareService(tmp_path/"isolated.sqlite",None,tmp_path/"data",runtime_mode="REAL")
        monkeypatch.setattr(fw,"_schedule_probe",lambda *args:None)
        try:
            code=fw.create_enrollment("Contract","Room")["pairing_code"]
            result=fw.claim(claim_input(base_url=base,code=code))
            secret=result["device_token"]
            server.read_token=camera_read_token(hash_device_token(secret))
            fw._probe_device("omcam-test")
            assert fw.get_device("omcam-test")["stream_status"]=="ready"
            assert all(header=="Bearer "+server.read_token for _,header in server.requests)
            with pytest.raises(HTTPException) as rejected:fw.heartbeat("omcam-test",server.read_token,heartbeat_input())
            assert rejected.value.status_code==401
            with fw.connect() as connection:
                rows=connection.execute("SELECT * FROM firmware_devices").fetchall()
                database=json.dumps([dict(row) for row in rows])
            assert secret not in database and server.read_token not in database
            assert server.read_token not in json.dumps(fw.get_device("omcam-test"))
        finally:fw.shutdown()


def test_runtime_injection_stays_in_adapter_memory_and_public_camera_api_hides_nested_credentials(tmp_path,monkeypatch):
    app=create_app(data_dir=tmp_path/"isolated-app",testing=True)
    with TestClient(app) as client:
        client.get("/api/session")
        fw=app.state.firmware_service;runtime=app.state.runtime
        monkeypatch.setattr(fw,"_schedule_probe",lambda *args:None)
        monkeypatch.setattr(fw,"camera_callback",None)
        result=fw.claim(claim_input(code=fw.create_enrollment("Contract","Room")["pairing_code"]))
        device=fw.get_device("omcam-test",include_secret=True)
        camera={"id":result["camera_id"],"source_type":"esp32","source":device["stream_url"],
            "name":"Contract","enabled":False,"config":{"device_id":"omcam-test","capture_url":device["capture_url"]}}
        with fw.connect() as connection:connection.execute("UPDATE firmware_devices SET simulated=0,hardware_verified=1")
        source=Esp32CamSource(camera["source"],camera["config"])
        engine=SimpleNamespace(source=source,camera=camera)
        runtime._configure_esp32_read_auth(engine,camera)
        expected=camera_read_token(hash_device_token(result["device_token"]))
        assert source._headers()=={"Authorization":"Bearer "+expected}
        assert expected not in json.dumps(engine.camera) and expected not in json.dumps(source.get_metadata())
        runtime.db.save("cameras",camera,camera["id"])
        saved=runtime.db.get("cameras",camera["id"])
        assert expected not in json.dumps(saved)
        saved["config"].update({"camera_read_token":expected,"headers":{"Authorization":"Bearer "+expected},
                               "nested":{"device_token":result["device_token"],"safe":True}})
        runtime.db.save("cameras",saved,saved["id"])
        response=client.get("/api/cameras")
        assert response.status_code==200
        assert expected not in response.text and result["device_token"] not in response.text
        fw.revoke("omcam-test")
        with pytest.raises(ValueError):runtime._configure_esp32_read_auth(engine,camera)


def test_public_configuration_and_adapter_metadata_redact_authorization_recursively():
    secret={"headers":{"Authorization":"Bearer "+READ_TOKEN},"nested":{"camera_read_token":READ_TOKEN,"safe":1}}
    public=public_camera_config(secret)
    assert public=={"nested":{"safe":1}} and READ_TOKEN not in json.dumps(public)


def test_jpeg_pixel_budget_rejects_before_native_decoder(monkeypatch):
    from services.vision.camera_sources.network import _decode_bounded_camera_jpeg
    monkeypatch.setattr("services.vision.camera_sources.network.cv2.imdecode",lambda *args:pytest.fail("oversized JPEG reached native decoder"))
    jpeg=b"\xff\xd8\xff\xc0\x00\x11\x08\x7f\xff\x7f\xff"+b"\x00"*10+b"\xff\xd9"
    with pytest.raises(ValueError,match="像素尺寸"):_decode_bounded_camera_jpeg(jpeg)


def test_bearer_credentials_are_redacted_even_inside_error_text():
    from apps.api.app.firmware import redact_firmware_log
    from services.vision.camera_sources.base import redact_value
    text="authorization: Bearer "+READ_TOKEN
    assert READ_TOKEN not in redact_firmware_log(text)
    assert READ_TOKEN not in redact_value(text)


def test_remote_echo_cannot_leak_camera_secret_through_health_diagnostics_or_errors():
    with camera_server() as (server,base):
        server.echo_secret=True
        source=Esp32CamSource(base+"/stream",{"capture_url":base+"/capture"});source.set_camera_read_token(READ_TOKEN)
        try:
            assert source.connect(),source.health_check()
            assert READ_TOKEN not in json.dumps(source.health_check())
            assert READ_TOKEN not in json.dumps(source.test_endpoints())
            source._record_error("unexpected reason: "+READ_TOKEN)
            assert READ_TOKEN not in json.dumps(source.health_check())
        finally:source.disconnect()


def test_one_small_mjpeg_frame_decodes_without_waiting_for_stream_bulk(record_property):
    with camera_server() as (server,base):
        server.stream_mode="one_frame"
        source=Esp32CamSource(base+"/stream",{"capture_url":base+"/capture","read_timeout_seconds":1})
        source.set_camera_read_token(READ_TOKEN)
        started=time.monotonic()
        try:
            assert source.connect(),source.health_check()
            elapsed=time.monotonic()-started
            record_property("local_http_first_frame_ms",round(elapsed*1000,2))
            assert elapsed<1.0  # Server waits 1.5 s before EOF; no second frame.
            source.disconnect()
            assert source.read_frame() is None
        finally:server.release_rest.set();source.disconnect()


@pytest.mark.parametrize("mode",["drip","truncated","compressed"])
def test_incomplete_or_compressed_stream_fails_bounded_and_releases(mode,record_property):
    with camera_server() as (server,base):
        server.stream_mode=mode
        source=Esp32CamSource(base+"/stream",{"capture_url":base+"/capture","read_timeout_seconds":.16})
        source.set_camera_read_token(READ_TOKEN)
        started=time.monotonic()
        assert not source.connect()
        elapsed=time.monotonic()-started
        record_property("failure_elapsed_ms",round(elapsed*1000,2))
        assert elapsed<.8
        assert source.capture is None or not source.capture.isOpened()
        assert source.read_frame() is None
        if mode=="drip":assert "总读取时间" in source.health_check()["error"]
        source.disconnect()


def test_control_json_is_size_bounded_before_parse():
    with camera_server() as (server,base):
        server.oversized_json=True
        source=Esp32CamSource(base+"/stream",{"capture_url":base+"/capture"});source.set_camera_read_token(READ_TOKEN)
        assert not source.connect()
        assert "读取上限" in source.health_check()["error"]
        assert all(path=="/health" for path,_ in server.requests)
        source.disconnect()


def test_diagnostics_cannot_bypass_missing_token_or_legacy_capability():
    with camera_server() as (server,base):
        source=Esp32CamSource(base+"/stream",{"capture_url":base+"/capture","board_model":"xiao_esp32s3_sense"})
        assert source.test_endpoints()["video_normal"] is False and server.requests==[]
        source.set_camera_read_token(READ_TOKEN);server.auth_capability="legacy"
        assert source.test_endpoints()["video_normal"] is False
        assert all(path=="/health" for path,_ in server.requests)
        source.disconnect()
