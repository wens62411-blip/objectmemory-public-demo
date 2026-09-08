"""Real HTTP + FastAPI subprocess + pinned local DINO + isolated TEST SQLite.

Generated textures prove registration/persistence, never physical recognition
accuracy. No detector, encoder, business API, or server process is mocked.
No camera is configured or opened. Missing model is explicitly NOT_RUN.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

import cv2
import httpx
import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[3]
MANIFEST = json.loads((ROOT / 'services/vision/detectors/appearance-model.json').read_text(encoding='utf-8'))
MODEL = ROOT / MANIFEST['default_path']


@contextmanager
def running_backend(root: Path, generation: int, lifecycle: list):
    data = (root / 'isolated-test-data').resolve()
    assert data.is_relative_to(root.resolve())
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    shutdown = root / f'stop-{generation}'
    ack = root / f'stopped-{generation}'
    log_path = root / f'backend-{generation}.log'
    process = None
    with log_path.open('w', encoding='utf-8') as log:
        try:
            process = subprocess.Popen([
                sys.executable, '-u', str(ROOT / 'scripts/serve.py'), '--mode', 'TEST',
                '--port', str(port), '--no-browser', '--shutdown-file', str(shutdown),
                '--shutdown-ack-file', str(ack),
            ], cwd=ROOT, env={**os.environ, 'OM_DATA_DIR': str(data),
                              'OM_RUNTIME_MODE': 'TEST', 'OM_ALLOW_LAN': '0'},
                stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=90,
                              trust_env=False) as client:
                deadline = time.monotonic() + 25
                ready = False
                while time.monotonic() < deadline:
                    assert process.poll() is None, 'Owned TEST backend exited before health was ready'
                    try:
                        health = client.get('/api/health', timeout=1)
                        ready = health.status_code == 200 and health.json().get('runtime_mode') == 'TEST'
                    except httpx.HTTPError:
                        pass
                    if ready:
                        break
                    time.sleep(.1)
                assert ready, 'Owned TEST backend did not become ready'
                # Acquire a normal local session; do not log or retain the PIN.
                assert client.get('/api/session').status_code == 200
                yield client, data / 'database/objectmemory-test.sqlite'
        finally:
            if process is not None:
                forced = False
                if process.poll() is None:
                    shutdown.write_text('stop owned integration backend\n', encoding='ascii')
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        forced = True
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                lifecycle.append({'generation': generation, 'port': port,
                                  'exit_code': process.returncode, 'graceful_ack': ack.is_file(),
                                  'forced_stop': forced})
                assert not forced and process.returncode == 0 and ack.is_file(), \
                    'Owned TEST backend must shut down cleanly and release its mode lease'


def database_row(path, table, record_id):
    assert table in {'item_recognition_profiles', 'item_reference_images'}
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(f'SELECT * FROM "{table}" WHERE id=?', (record_id,)).fetchone()
        return dict(row) if row else None


def require_no_camera_or_events(client):
    assert client.get('/api/cameras').json() == []
    assert client.get('/api/events').json() == []


@pytest.mark.skipif(not MODEL.is_file(), reason='NOT_RUN: explicitly prepare the pinned local DINO model first')
def test_real_http_dino_profile_survives_restart_and_delete_invalidates(tmp_path, record_property):
    actual_model_sha = hashlib.sha256(MODEL.read_bytes()).hexdigest()
    assert MODEL.stat().st_size == MANIFEST['bytes'] and actual_model_sha == MANIFEST['sha256']
    frame = np.random.default_rng(905).integers(0, 256, (320, 420, 3), dtype=np.uint8)
    cv2.rectangle(frame, (80, 60), (340, 260), (40, 120, 220), 5)
    ok, encoded = cv2.imencode('.png', frame)
    assert ok
    photo = encoded.tobytes()
    lifecycle = []
    evidence = {'physical_camera': False, 'physical_identity_accuracy': 'NOT_TESTED',
                'business_mock': False, 'encoder_mock': False, 'runtime_mode': 'TEST',
                'model_sha256': actual_model_sha, 'lifecycle': lifecycle, 'passed': False}
    try:
        with running_backend(tmp_path, 1, lifecycle) as (client, database):
            require_no_camera_or_events(client)
            response = client.post('/api/items', json={'name': '生成纹理注册流程测试', 'type': 'phone'})
            assert response.status_code == 200, response.text
            item_id = response.json()['id']
            response = client.post(f'/api/items/{item_id}/reference-images',
                                   files=[('files', ('generated-texture.png', photo, 'image/png'))])
            assert response.status_code == 200, response.text
            reference = response.json()[0]
            assert reference['region_confirmed'] is False
            assert client.get(reference['original_path']).content == photo
            profile_url = f'/api/items/{item_id}/profile'
            assert client.get(profile_url).json()['registration_status'] == 'images_saved'
            endpoint = f"/api/items/{item_id}/reference-images/{reference['id']}"
            response = client.patch(endpoint, json={'region': [.1, .1, .8, .8], 'confirmed': True})
            assert response.status_code == 200, response.text
            started = time.monotonic()
            response = client.post(profile_url + '/build')
            assert response.status_code == 200, response.text
            profile = response.json()
            assert profile['registration_status'] == 'ready'
            assert profile['model_id'] == MANIFEST['model_id']
            assert profile['model_version'] == MANIFEST['model_version']
            assert profile['loaded_camera_ids'] == [], 'A built profile must not claim a running camera'
            saved = database_row(database, 'item_recognition_profiles', item_id)
            vectors = np.asarray(json.loads(saved['embeddings']), dtype=float)
            assert saved['dimension'] == 384 and vectors.shape == (1, 384)
            assert np.isfinite(vectors).all() and np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-3)
            saved_ref = database_row(database, 'item_reference_images', reference['id'])
            crop = client.get(saved_ref['crop_path'])
            assert crop.status_code == 200
            assert cv2.imdecode(np.frombuffer(crop.content, np.uint8), 1) is not None
            evidence.update({'item_id': item_id, 'reference_id': reference['id'],
                             'profile_version': profile['profile_version'], 'dimension': 384,
                             'feature_norm': float(np.linalg.norm(vectors[0])),
                             'build_seconds': round(time.monotonic() - started, 3),
                             'embeddings_sha256': hashlib.sha256(saved['embeddings'].encode()).hexdigest()})
            assert client.post(f'/api/items/{item_id}/recognition-test', json={'camera_id': 'absent'}).status_code in {404, 409}
            require_no_camera_or_events(client)

        with running_backend(tmp_path, 2, lifecycle) as (client, database):
            restored = client.get(profile_url)
            assert restored.status_code == 200
            assert restored.json()['registration_status'] == 'ready'
            assert restored.json()['profile_version'] == profile['profile_version']
            assert restored.json()['loaded_camera_ids'] == []
            assert restored.json()['loaded_profile_version'] is None
            persisted = database_row(database, 'item_recognition_profiles', item_id)
            assert persisted['embeddings'] == saved['embeddings']
            assert client.get(reference['original_path']).content == photo
            require_no_camera_or_events(client)
            assert client.delete(endpoint).status_code == 200
            invalidated = client.get(profile_url).json()
            assert invalidated['registration_status'] != 'ready'
            assert invalidated['profile_version'] > profile['profile_version']
            assert invalidated['ready_reference_count'] == 0
            assert invalidated['loaded_profile_version'] is None
            assert client.get(reference['original_path']).status_code == 404
            assert client.get(saved_ref['crop_path']).status_code == 404
            assert client.post(profile_url + '/build').status_code == 422
            require_no_camera_or_events(client)
            # Clean only this owned TEST database through its real API.
            cleared = client.post('/api/storage/clear-test')
            assert cleared.status_code == 200
            assert client.get('/api/items').json() == []
            assert database_row(database, 'item_recognition_profiles', item_id) is None
            require_no_camera_or_events(client)
        evidence.update({'passed': True, 'real_confirmation_events': 0, 'test_events': 0,
                         'restart_profile_persisted': True, 'delete_invalidated': True})
        record_property('encoder_mock', False)
        record_property('feature_dimension', 384)
        record_property('restart_profile_persisted', True)
        record_property('no_camera_events', 0)
    finally:
        (tmp_path / 'model-integration-result.json').write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
