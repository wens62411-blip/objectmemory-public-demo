"""API/controller regression tests; only the file-video test runs real capture.

No test opens a physical camera or contacts the user's running service.
"""
import threading
import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / 'camera-usability', testing=True)
    with TestClient(app) as connection:
        connection.get('/api/session')
        yield connection


def add_webcam(client, **extra):
    return client.post('/api/cameras', json={'name': '已有本机镜头', 'source_type': 'webcam', 'source': '0', **extra})


def test_duplicate_webcam_is_rejected_without_opening_or_inserting(client, monkeypatch):
    runtime = client.app.state.runtime
    monkeypatch.setattr(runtime, 'start', lambda *_: pytest.fail('duplicate must not open a camera'))
    first = add_webcam(client)
    assert first.status_code == 200
    assert first.json()['config']['index'] == 0
    duplicate = add_webcam(client, source='00')
    assert duplicate.status_code == 409
    assert '已有本机镜头' in duplicate.json()['detail']
    assert runtime.db.count('cameras') == 1
    assert runtime.db.count('events') == 0


@pytest.mark.parametrize('index', [1, '0', -1, False])
def test_webcam_source_and_config_index_must_agree(client, index):
    response = add_webcam(client, config={'index': index})
    assert response.status_code == 422
    assert client.app.state.runtime.db.count('cameras') == 0


def fake_active(runtime, camera):
    engine = SimpleNamespace(camera=dict(camera), zone_manager=SimpleNamespace(room_name='客厅'), camera_name=camera['name'], room_name='客厅', health=lambda: {'status': 'ready', 'status_code': 'STREAMING', 'source_session_id': 'unchanged-test-session', 'fps': 5})
    runtime.engines[camera['id']] = engine
    return engine


def test_rename_active_camera_does_not_reopen_or_reset_its_source(client, monkeypatch):
    camera = add_webcam(client).json()
    runtime = client.app.state.runtime
    engine = fake_active(runtime, camera)
    monkeypatch.setattr(runtime, 'restart', lambda *_: pytest.fail('metadata must not restart'))
    try:
        response = client.patch(f'/api/cameras/{camera["id"]}', json={'name': '新名称', 'installation': '书柜上方'})
        assert response.status_code == 200, response.text
        assert runtime.engines[camera['id']] is engine
        assert response.json()['health']['source_session_id'] == 'unchanged-test-session'
        assert engine.camera_name == '新名称'
    finally:
        runtime.engines.clear()


def test_rename_enabled_but_never_started_camera_does_not_start_it(client, monkeypatch):
    camera = add_webcam(client, enabled=True).json()
    runtime = client.app.state.runtime
    assert not runtime.engines
    lifecycle_calls = []
    for method in ('start', 'restart', 'stop'):
        monkeypatch.setattr(runtime, method, lambda *_args, _method=method, **_kwargs: lifecycle_calls.append(_method))
    started = time.monotonic()
    response = client.patch(f'/api/cameras/{camera["id"]}', json={'name': '仅修改名称'})
    assert response.status_code == 200, response.text
    assert lifecycle_calls == [], 'metadata must not change capture lifecycle'
    assert time.monotonic() - started < 1
    assert response.json()['name'] == '仅修改名称'
    assert response.json()['enabled'] is True
    assert response.json()['health']['status'] == 'stopped'
    assert not runtime.engines
    assert runtime.db.count('source_sessions') == 0
    assert runtime.db.count('events') == 0


def test_read_status_and_diagnostic_conflict_do_not_wait_for_driver_lifecycle_lock(client):
    camera = add_webcam(client).json()
    runtime = client.app.state.runtime
    entered = threading.Event()
    release = threading.Event()
    def occupy():
        with runtime.lock:
            entered.set()
            assert release.wait(5)
    worker = threading.Thread(target=occupy)
    worker.start()
    assert entered.wait(1)
    try:
        started = time.monotonic()
        response = client.get('/api/cameras')
        assert time.monotonic() - started < 1
        assert response.json()[0]['id'] == camera['id']
        assert response.json()[0]['health']['health_snapshot_stale'] is True
        assert response.json()[0]['health']['status'] != 'ready'
        started = time.monotonic()
        diagnostic = client.post('/api/camera-diagnostics/run', json={'indices': '0'})
        assert diagnostic.status_code == 409
        assert time.monotonic() - started < 1
    finally:
        release.set(); worker.join(2)
    assert runtime.diagnostic_lock.acquire(blocking=False)
    runtime.diagnostic_lock.release()


def test_quick_diagnostic_reuses_active_status_without_probe_start_or_stop(client, monkeypatch):
    from scripts import camera_diagnose
    camera = add_webcam(client).json()
    runtime = client.app.state.runtime
    engine = fake_active(runtime, camera)
    monkeypatch.setattr(camera_diagnose, 'run_diagnostics', lambda *_args, **_kwargs: pytest.fail('must not probe owned hardware'))
    monkeypatch.setattr(runtime, 'start', lambda *_: pytest.fail('must not restart'))
    try:
        response = client.post('/api/camera-diagnostics/run', json={'indices': '0'})
        assert response.status_code == 200
        assert response.json()['status'] == 'LIVE_STATUS'
        assert response.json()['running_cameras'][0]['id'] == camera['id']
        assert runtime.engines[camera['id']] is engine
        assert client.post('/api/camera-diagnostics/run', json={'profile': 'full'}).status_code == 409
        assert runtime.diagnostic_running is False
    finally:
        runtime.engines.clear()


def test_diagnostic_job_returns_quickly_cancels_and_releases_lock(client, monkeypatch):
    from scripts import camera_diagnose
    entered = threading.Event()
    def controlled_probe(*_args, cancel_event, progress, **_kwargs):
        entered.set()
        progress({'completed': 0, 'message': 'controlled worker running'})
        assert cancel_event.wait(3)
        return {'status': 'CANCELLED', 'summary': {'status_code': 'CANCELLED', 'message': 'cancelled'}}
    monkeypatch.setattr(camera_diagnose, 'run_diagnostics', controlled_probe)
    started = time.monotonic()
    response = client.post('/api/camera-diagnostics/run')
    assert response.status_code == 202
    assert time.monotonic() - started < 1
    assert entered.wait(1)
    job_id = response.json()['job']['id']
    assert client.post('/api/camera-diagnostics/run').status_code == 409
    assert client.post('/api/camera-diagnostics/cancel', json={'job_id': 'stale-job'}).status_code == 409
    assert client.post('/api/camera-diagnostics/cancel', json={'job_id': job_id}).status_code == 200
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        payload = client.get('/api/camera-diagnostics').json()
        if not payload['diagnostic_running']:
            break
        time.sleep(.01)
    assert payload['job']['status'] == 'CANCELLED'
    assert payload['diagnostic_running'] is False
    runtime = client.app.state.runtime
    assert runtime.diagnostic_lock.acquire(blocking=False)
    runtime.diagnostic_lock.release()
    assert runtime.db.count('events') == 0


def test_diagnostic_worker_failure_releases_lock(client, monkeypatch):
    from scripts import camera_diagnose
    def fail(*_args, **_kwargs):
        raise RuntimeError('controlled probe failure')
    monkeypatch.setattr(camera_diagnose, 'run_diagnostics', fail)
    assert client.post('/api/camera-diagnostics/run').status_code == 202
    deadline = time.monotonic() + 2
    while client.app.state.runtime.diagnostic_running and time.monotonic() < deadline:
        time.sleep(.01)
    response = client.get('/api/camera-diagnostics').json()
    assert response['job']['status'] == 'FAILED'
    assert response['diagnostic_running'] is False
    assert 'controlled probe failure' in response['job']['message']


def test_app_shutdown_cancels_and_drains_its_diagnostic_worker(tmp_path, monkeypatch):
    from scripts import camera_diagnose
    entered = threading.Event()
    exited = threading.Event()
    def controlled_probe(*_args, cancel_event, **_kwargs):
        entered.set()
        assert cancel_event.wait(3)
        exited.set()
        return {'status': 'CANCELLED', 'summary': {'status_code': 'CANCELLED'}}
    monkeypatch.setattr(camera_diagnose, 'run_diagnostics', controlled_probe)
    app = create_app(tmp_path/'diagnostic-shutdown', testing=True)
    with TestClient(app) as connection:
        connection.get('/api/session')
        assert connection.post('/api/camera-diagnostics/run').status_code == 202
        assert entered.wait(1)
    assert exited.is_set()
    assert not app.state.runtime.diagnostic_running
    assert app.state.runtime.diagnostic_lock.acquire(blocking=False)
    app.state.runtime.diagnostic_lock.release()


def test_file_video_connection_runs_real_engine_and_decodes_its_actual_snapshot(client):
    """Real OpenCV file pipeline, not physical-camera proof or a mocked API."""
    runtime = client.app.state.runtime
    runtime.db.save('settings', {'value': {**runtime.settings(), 'show_hands': False}}, 'main')
    video = runtime.temporary / 'connection-file-fixture.avi'
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'MJPG'), 20, (160, 120))
    assert writer.isOpened()
    try:
        for index in range(80):
            frame = np.full((120, 160, 3), 220, np.uint8)
            cv2.circle(frame, (20 + index, 60), 10, (0, 0, 0), -1)
            writer.write(frame)
    finally:
        writer.release()
    camera = client.post('/api/cameras', json={'name': 'TEST 文件连接', 'source_type': 'video', 'source': str(video), 'config': {'loop': True}, 'save_clips': False}).json()
    response = client.post(f'/api/cameras/{camera["id"]}/test')
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['success'] is True
    assert payload['is_simulated'] is True and payload['runtime_mode'] == 'TEST'
    assert payload['continuous_frames_verified'] is True
    decoded = cv2.imdecode(np.frombuffer(client.get(payload['frame_url']).content, np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None and decoded.shape[:2] == (120, 160)
    assert not runtime.engines
    assert runtime.db.count('events') == 0 and runtime.db.count('item_current_state') == 0
    assert not list(runtime.media.rglob('*.jpg')) and not list(runtime.media.rglob('*.mp4'))
