"""Synthetic contract/unit checks only; NEVER evidence of physical-phone success."""
from copy import deepcopy
import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[3]
SPEC=importlib.util.spec_from_file_location('physical_phone_gate',ROOT/'scripts/verify-physical-phone-frame.py')
gate=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


@pytest.fixture
def local_case(tmp_path):
    folder=tmp_path/'data/verification/unit';folder.mkdir(parents=True)
    rows=[]
    for index in range(3):
        path=folder/f'{index}.png'
        ok,encoded=cv2.imencode('.png',np.full((80,100,3),index*70,np.uint8));assert ok
        path.write_bytes(encoded.tobytes())
        rows.append({'path':str(path.relative_to(tmp_path)),'sha256':gate.file_hash(path),
                     'reviewed_by':'assistant_visual_review','reviewed_phone_present':index==0})
    case={'contract_version':1,'target_item_id':'unit-target','positive':{
        **rows[0],'runtime_mode':'REAL','source_type':'opencv_camera','is_simulated':False,
        'source_session_id':'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee','source_frame':42,
        'source_timestamp':'2026-09-06T15:53:42+00:00','phone_roi_xywh':[10,20,30,40]},
        'negative_controls':rows[1:]}
    manifest=folder/'case.json';manifest.write_text(json.dumps(case),encoding='utf-8')
    return tmp_path,case,manifest


def prediction(box=(10,20,30,40),identity='unit-target',accepted=True):
    return {'bbox':list(box),'identity':{'accepted':accepted,'item_id':identity}}


@pytest.mark.parametrize('positive,negative,expected',[
    ([prediction()],[[],[]],True),
    ([],[[],[]],False),
    ([prediction(identity='other')],[[],[]],False),
    ([prediction(box=(70,0,20,20))],[[],[]],False),
    ([prediction()],[[],[prediction()]],False),
    ([prediction(),prediction()],[[],[]],False),
])
def test_gate_requires_localization_correct_identity_and_no_false_accepts(positive,negative,expected):
    assert gate.evaluate_predictions(positive,negative,[10,20,30,40],'unit-target')['passed'] is expected


def test_roi_is_not_passed_into_detection_or_identity_model():
    frame=np.arange(80*100*3,dtype=np.uint8).reshape(80,100,3)
    calls=[]
    class Detector:
        def detect(self,image):
            assert image is frame
            calls.append('full_frame_detect')
            return [SimpleNamespace(label='cell phone',bbox=(12,8,40,50),confidence=.7)]
    class Matcher:
        def match(self,crop,category):
            assert np.array_equal(crop,frame[8:58,12:52])
            assert category=='cell phone'
            calls.append('actual_model_box_crop')
            return {'accepted':True,'item_id':'unit-target'}
    predictions=gate.predict(frame,Detector(),Matcher(),32)
    # A wildly different reviewer ROI only changes evaluation AFTER inference.
    assert not gate.evaluate_predictions(predictions,[[],[]],[80,60,10,10],'unit-target')['passed']
    assert calls==['full_frame_detect','actual_model_box_crop']


def test_confined_paths_reject_traversal_and_external_targets(local_case):
    root,_,_=local_case
    for value in ('../outside.jpg','data/registered-items/reference.jpg',root.parent/'outside.jpg'):
        with pytest.raises(gate.InvalidCase):gate.confined(root,value,root/'data/verification',exists=False)


@pytest.mark.parametrize('change',[
    lambda c:c['positive'].update(sha256='0'*64),
    lambda c:c['positive'].update(is_simulated=True),
    lambda c:c['positive'].update(source_frame=True),
    lambda c:c['positive'].update(phone_roi_xywh=[90,0,20,20]),
    lambda c:c['negative_controls'].pop(),
    lambda c:c['negative_controls'][0].update(reviewed_phone_present=True),
    lambda c:c['negative_controls'].__setitem__(1,deepcopy(c['negative_controls'][0])),
])
def test_incomplete_or_inconsistent_cases_are_rejected(local_case,change):
    root,case,_=local_case
    change(case)
    with pytest.raises(gate.InvalidCase):gate.validate_case(root,case)


def test_valid_contract_loads_only_local_images(local_case):
    root,case,_=local_case
    positive,negative=gate.validate_case(root,case)
    assert positive[1].shape==(80,100,3) and len(negative)==2


def test_report_cannot_overwrite_case_and_returns_incomplete(local_case):
    root,_,manifest=local_case
    original=manifest.read_bytes()
    result=gate.run(manifest,manifest,root=root)
    assert result['exit_code']==2 and result['status']=='INCOMPLETE'
    assert manifest.read_bytes()==original


def test_false_accept_before_spatial_ambiguity_still_fails_negative_gate():
    rejected={'bbox':[0,0,20,20],'identity':{'accepted':False,'item_id':None},
              'raw_identity_accepted_before_ambiguity':'unit-target'}
    assert not gate.evaluate_predictions([prediction()],[[rejected],[]],[10,20,30,40],'unit-target')['passed']


def test_existing_report_is_never_overwritten(local_case):
    root,_,manifest=local_case
    output=manifest.parent/'existing.json';output.write_text('preserve',encoding='utf-8')
    result=gate.run(manifest,output,root=root)
    assert result['exit_code']==2 and output.read_text(encoding='utf-8')=='preserve'


def test_gate_does_not_silently_substitute_current_detector_model(local_case):
    root,_,_=local_case
    assert gate.current_nanodet_path(root,{}).name=='object_detection_nanodet_2022nov.onnx'
    with pytest.raises(gate.InvalidCase):gate.current_nanodet_path(root,{'model_path':'data/models/different.onnx'})


def test_copied_reference_image_is_not_a_valid_detection_input(local_case):
    root,case,manifest=local_case
    source=root/case['positive']['path']
    reference=manifest.parent/'reference-copy.png';reference.write_bytes(source.read_bytes())
    protected={reference:gate.file_hash(reference)}
    with pytest.raises(gate.InvalidCase):gate.protect_detection_inputs([source],protected)
    assert list(protected)==[reference] and gate.file_hash(reference)==protected[reference]


def test_production_predict_runs_the_current_combined_recognizers_without_capture_or_event_loop():
    image = np.zeros((100, 200, 3), np.uint8)
    calls = []
    generic, combined = [object()], [object(), object()]

    class Engine:
        def __init__(self):
            self.experimental = SimpleNamespace(detect=self.detect)
            self._recognition_candidates = []
        def detect(self, frame):
            assert frame is image
            calls.append('actual_generic_detection')
            return generic
        def _supplement_reference_patches(self, frame, detections):
            assert frame is image and detections is generic
            calls.append('actual_reference_proposals')
            return combined
        def _assign_reference_identities(self, frame, detections):
            assert frame is image and detections is combined
            calls.append('production_identity_admission')
            self._recognition_candidates = [{'bbox': [.1, .2, .3, .4], 'category': 'phone',
                'proposal_backend': 'dinov2_reference_patches', 'category_evidence': 'registered_reference_patch',
                'accepted': True, 'item_id': 'unit-target', 'best_item_id': 'unit-target', 'profile_version': 7}]
        def start(self):
            pytest.fail('Offline verifier must not start a camera')
        def _process(self, *_):
            pytest.fail('Offline verifier must not enter a state/event-writing loop')

    result = gate.production_predict(image, Engine())
    assert calls == ['actual_generic_detection', 'actual_reference_proposals', 'production_identity_admission']
    assert result[0]['bbox'] == [20, 20, 60, 40]
    assert result[0]['identity']['item_id'] == 'unit-target'
    assert result[0]['proposal_backend'] == 'dinov2_reference_patches'


def test_production_predict_does_not_hide_rejected_ambiguous_negative_acceptance():
    candidate = {'bbox': [.1, .2, .3, .4], 'category': 'phone', 'best_item_id': 'unit-target',
                 'accepted': False, 'item_id': None, 'rejection': 'multiple_spatial_candidates'}
    engine = SimpleNamespace(experimental=SimpleNamespace(detect=lambda frame: []),
        _supplement_reference_patches=lambda frame, rows: rows, _assign_reference_identities=lambda frame, rows: None,
        _recognition_candidates=[candidate])
    predicted = gate.production_predict(np.zeros((100, 200, 3), np.uint8), engine)
    assert predicted[0]['raw_identity_accepted_before_ambiguity'] == 'unit-target'
    assert not gate.evaluate_predictions([prediction()], [predicted, []], [10, 20, 30, 40], 'unit-target')['passed']


def test_registration_reader_loads_all_reference_json_from_readonly_snapshot(tmp_path):
    database = tmp_path / 'data/database/objectmemory.sqlite'
    database.parent.mkdir(parents=True)
    reference = tmp_path / 'data/registered-items/original.png'
    reference.parent.mkdir()
    encoded, content = cv2.imencode('.png', np.full((60, 80, 3), 60, np.uint8))
    assert encoded
    reference.write_bytes(content.tobytes())
    ref_path = '/media/registered-items/original.png'
    with sqlite3.connect(database) as connection:
        connection.executescript('''
            CREATE TABLE items(id TEXT, type TEXT, aliases TEXT);
            CREATE TABLE item_recognition_profiles(id TEXT, status TEXT, profile_version INTEGER, embeddings TEXT, reference_ids TEXT);
            CREATE TABLE item_reference_images(id TEXT, item_id TEXT, path TEXT, original_path TEXT, sha256 TEXT,
                features TEXT, region TEXT, region_confirmed INTEGER, suggested_regions TEXT, capture_source TEXT, quality TEXT);
            CREATE TABLE settings(id TEXT, value TEXT);
        ''')
        connection.execute('INSERT INTO items VALUES (?, ?, ?)', ('unit-target', 'phone', '["mine"]'))
        connection.execute('INSERT INTO item_recognition_profiles VALUES (?, ?, ?, ?, ?)',
                           ('unit-target', 'ready', 7, '[[1,0,0]]', '["ref"]'))
        connection.execute('INSERT INTO item_reference_images VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            ('ref', 'unit-target', ref_path, ref_path, gate.file_hash(reference), '{"vector":[1,0,0]}',
             '[0.1,0.2,0.5,0.6]', 1, '[]', '{"source_type":"browser_camera"}', '{"blur":false}'))
        connection.execute('INSERT INTO settings VALUES (?, ?)', ('main', '{"detection_mode":"experimental"}'))
    before = database.read_bytes()
    items, settings, protected = gate.read_registration(tmp_path, 'unit-target')
    assert database.read_bytes() == before
    assert items[0]['aliases'] == ['mine']
    assert items[0]['appearance_profile']['reference_ids'] == ['ref']
    row = items[0]['reference_images'][0]
    assert row['region'] == [.1, .2, .5, .6]
    assert row['features'] == {'vector': [1, 0, 0]}
    assert row['suggested_regions'] == []
    assert row['capture_source'] == {'source_type': 'browser_camera'}
    assert row['quality'] == {'blur': False}
    assert protected[reference] == gate.file_hash(reference)
    assert settings['detection_mode'] == 'experimental'
