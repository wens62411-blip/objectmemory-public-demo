"""Mock/contract tests for probe safety; not real-model recognition evidence."""
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sqlite3
from types import SimpleNamespace

import numpy as np
import pytest

from services.vision.detectors.yoloe_visual import YOLOEVisualProbe, reference_xyxy, source_box


@pytest.mark.parametrize('region', [[0, 0, 0, .5], [-.1, 0, .5, .5], [.8, 0, .5, .5],
                                   [0, 0, float('nan'), .5], [0, False, .5, .5], [0, 0, .5]])
def test_reference_rejects_invalid_geometry(region):
    with pytest.raises(ValueError):
        reference_xyxy(region, 1280, 720)


def test_reference_coordinates_are_its_own_image_not_target():
    assert reference_xyxy([.25, .1, .5, .8], 100, 200) == [25, 20, 75, 180]
    assert source_box([20, 30, 80, 90], 640, 480) == (20, 30, 60, 60)


@pytest.mark.parametrize('box', [[1, 2, 1, 5], [0, 0, 1001, 10], [0, float('inf'), 10, 20]])
def test_source_coordinates_reject_invalid_outputs(box):
    with pytest.raises(ValueError):
        source_box(box, 640, 480)


def fake_probe():
    instance = object.__new__(YOLOEVisualProbe)
    instance._reference = None
    instance._prompts = None
    instance._reference_hash = None
    instance._predictor = object()
    instance.confidence = .35
    instance.image_size = 640
    instance.prompt_calls = instance.predict_calls = 0
    calls = []

    def predict(**kwargs):
        calls.append(kwargs)
        return [SimpleNamespace(orig_shape=kwargs['source'].shape[:2], names={0: 'actual-model-object0'},
                               boxes=[SimpleNamespace(cls=SimpleNamespace(item=lambda: 0),
                                    conf=SimpleNamespace(item=lambda: .75),
                                    xyxy=[SimpleNamespace(cpu=lambda: SimpleNamespace(numpy=lambda: np.array([1, 2, 30, 40])))])])]
    instance.model = SimpleNamespace(predict=predict)
    return instance, calls


def test_prompt_group_not_semantic_or_registered_identity_and_prompt_reused():
    probe, calls = fake_probe()
    reference = np.zeros((100, 200, 3), dtype=np.uint8)
    target = np.ones((480, 640, 3), dtype=np.uint8)
    probe.set_reference(reference, [.1, .2, .3, .4], reference_id='reference-id', prompt_item_id='user-item-id')
    first = probe.detect(target)[0]
    probe.detect(target)
    assert first['raw_class_name'] == 'actual-model-object0'
    assert first['raw_class_id'] == 0
    assert first['prompt_item_id'] == 'user-item-id'
    assert first['item_id'] is None and first['category'] is None
    assert first['identity_confirmed'] is False and first['track_id'] is None
    np.testing.assert_allclose(calls[0]['visual_prompts']['bboxes'], [[20, 20, 80, 60]])
    assert calls[0]['refer_image'].shape == reference.shape
    assert calls[0]['source'].shape == target.shape
    assert 'refer_image' not in calls[1] and 'visual_prompts' not in calls[1]
    assert probe.prompt_calls == 1 and probe.predict_calls == 2


def test_reference_self_test_fails_before_model_called():
    probe, calls = fake_probe()
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    probe.set_reference(frame, [.1, .2, .3, .4], reference_id='ref', prompt_item_id='item')
    with pytest.raises(ValueError, match='self-testing'):
        probe.detect(frame.copy())
    assert calls == []


def load_script():
    path = Path(__file__).resolve().parents[3] / 'scripts/probe-item-recognition.py'
    spec = importlib.util.spec_from_file_location('yoloe_probe_script', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_offline_guard_rejects_network_and_restores_socket():
    probe = load_script()
    original = socket.socket.connect
    with probe.offline_only() as attempts:
        with socket.socket() as connection:
            with pytest.raises(OSError, match='Network disabled'):
                connection.connect(('127.0.0.1', 1))
    assert attempts == ['blocked_external_connection']
    assert socket.socket.connect is original


def test_original_hash_distinct_from_display_and_database_read_only(tmp_path, monkeypatch):
    probe = load_script()
    monkeypatch.setattr(probe, 'ROOT', tmp_path)
    data = tmp_path / 'data'
    folder = data / 'registered-items'
    folder.mkdir(parents=True)
    original = folder / 'ref-original.jpg'
    display = folder / 'ref-display.jpg'
    original.write_bytes(b'original-upload')
    display.write_bytes(b'exif-normalized-display')
    database = data / 'reference.sqlite'
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript('''
            CREATE TABLE items(id TEXT, name TEXT, type TEXT);
            INSERT INTO items VALUES('item', 'My phone', 'phone');
            CREATE TABLE item_recognition_profiles(id TEXT, embeddings TEXT);
            CREATE TABLE item_reference_images(id TEXT, item_id TEXT, path TEXT, original_path TEXT,
                sha256 TEXT, region TEXT, region_confirmed INTEGER);
            CREATE TABLE settings(value TEXT);
            CREATE TABLE item_current_state(id TEXT);
            CREATE TABLE movement_events(id TEXT);
            CREATE TABLE event_media(id TEXT);
        ''')
        connection.execute('INSERT INTO item_reference_images VALUES(?,?,?,?,?,?,?)', (
            'ref', 'item', '/media/registered-items/ref-display.jpg', '/media/registered-items/ref-original.jpg',
            probe.digest(original), json.dumps([.1, .1, .5, .5]), 1))
        connection.commit()
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    result = probe.read_database(database, 'item', 'ref')
    assert result[2]['display_sha256'] == probe.digest(display)
    assert result[2]['sha256'] == probe.digest(original)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    original.write_bytes(b'tampered')
    with pytest.raises(ValueError, match='Original reference'):
        probe.read_database(database, 'item', 'ref')


def test_probe_cannot_use_or_output_data_outside_project(tmp_path, monkeypatch):
    probe = load_script()
    monkeypatch.setattr(probe, 'ROOT', tmp_path)
    with pytest.raises(ValueError, match='inside project data'):
        probe.local_data_path(tmp_path / 'data' / '..' / 'outside.jpg')
