"""Controlled detector/pose fixtures test engine wiring, not physical people.

No model, camera, or business result is claimed real by these unit contracts.
The mapper test additionally exercises real isolated SQLite and homography.
Actual NanoDet/MediaPipe public-file evaluation is test_person_pose.py's opt-in.
"""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
import time

import cv2
import numpy as np
import pytest

from apps.api.tests.test_scene_mapping import context, save_3d
from services.vision.capture import FramePacket
from services.vision.detectors.base import Detection
from services.vision.detectors.hands import HandObservation
from services.vision.engine import VisionEngine


class PoseFixture:
    def __init__(self, controls):
        self.controls = controls
        self.closed = False
        self.calls = []

    def detect(self, frame, boxes):
        assert not self.closed
        self.calls.append((frame,deepcopy(boxes)))
        if self.controls.pose_error:
            raise MemoryError('explicit isolated pose allocation fault')
        rows = []
        for index,box in enumerate(boxes):
            x,y,w,h = box['bbox']
            rows.append({**box,'person_index':index,'foot_point':[x+w/2,y+h*.94] if self.controls.feet else None,
                'foot_point_visible':self.controls.feet,'foot_min_visibility':.89,'foot_min_presence':.82,
                'posture_state':'upright_candidate' if self.controls.feet else 'unknown',
                'pose_status':'fixture_visible_feet' if self.controls.feet else 'fixture_no_feet',
                'landmarks':[], 'pose_backend':'explicit_pose_unit_fixture', 'ground_contact_confirmed':False})
        if self.controls.mismatch:
            rows[0]['bbox'] = [.1,.1,.1,.1]
        return rows

    def health(self):
        return {'available':not self.closed,'status':'ready','error':None,'last_latency_ms':.1}

    def close(self):
        self.closed = True


@pytest.fixture
def factory(tmp_path,monkeypatch):
    engines = []
    monkeypatch.setattr(cv2,'VideoCapture',lambda *args,**kwargs: pytest.fail('Unit fixture must not open any source'))
    def build(*,mode='experimental',config=None,zones=None,camera=None):
        controls = SimpleNamespace(boxes=[(64,24,64,192)],feet=False,pose_error=False,mismatch=False,models=[],
                                   hand_calls=0,tracks=[],tracker_labels=[],sequence=0,source_status='online',
                                   source_started=time.monotonic()-.5)
        class ObjectsFixture:
            def __init__(self,*args,**kwargs): pass
            def health(self): return {'available':True,'error':None}
            def detect(self,frame):
                return [Detection(f'generic:person:{i}','person',box,(box[0]+box[2]/2,box[1]+box[3]/2),.9,
                        'experimental',metadata={'backend':'explicit_detector_fixture'}) for i,box in enumerate(controls.boxes)] + [
                    Detection('generic:phone:0','cell phone',(220,60,40,70),(240,95),.92,'experimental')]
        monkeypatch.setattr('services.vision.engine.NanoDetDetectorBackend',ObjectsFixture)
        camera = camera or {'id':'person-fixture-camera','source_type':'video','source':str(tmp_path/'never-opened.avi')}
        engine = VisionEngine(camera,[{'id':'unit-phone','name':'unit phone','type':'phone'}],zones or [],
            {'runtime_mode':'TEST','detection_mode':mode,'phone_shape_enabled':False,'hand_detection_enabled':False,
             'person_pose':config or {},'save_clips':False,'media_root':str(tmp_path/f'media-{len(engines)}')},
            lambda *_: pytest.fail('Unit person frames cannot create movement events'),on_track=controls.tracks.append,
            appearance_encoder=SimpleNamespace(health=lambda:{'available':False,'error':'explicit unused appearance fixture'}))
        engine.reference_matcher = SimpleNamespace(match=lambda *_args,**_kwargs:{'accepted':True,'item_id':'unit-phone',
            'profile_version':1,'best_score':.95,'second_score':.1,'gap':.85})
        def new_pose():
            model = PoseFixture(controls)
            controls.models.append(model)
            return model
        monkeypatch.setattr(engine,'_new_person_pose',new_pose)
        engine.hands.close()
        def hands(frame):
            controls.hand_calls += 1
            return [HandObservation((20,20),(10,10,20,20),[(.1,.1)]*21)]
        engine.hands = SimpleNamespace(detect=hands,health=lambda:{'available':True,'error':None},close=lambda:None)
        monkeypatch.setattr(engine.capture,'health',lambda:{'status':controls.source_status})
        original_update = engine.tracker.update
        def object_tracking(detections,*args):
            controls.tracker_labels.append([d.label for d in detections])
            return original_update(detections,*args)
        monkeypatch.setattr(engine.tracker,'update',object_tracking)
        engines.append(engine)
        def process(*,session='unit-person-session',shape=(240,320,3),stamp=None,wall=None):
            controls.sequence += 1
            # Controlled source cadence, not consecutive coarse Windows clock
            # reads which can legitimately be equal and rejected as replay.
            packet = FramePacket(np.full(shape,100,np.uint8),stamp if stamp is not None else controls.source_started+controls.sequence*.05,
                                 wall if wall is not None else time.time(),controls.sequence,session,0)
            engine._process(packet)
            return packet
        return SimpleNamespace(engine=engine,controls=controls,process=process)
    yield build
    for engine in engines:
        engine.stop()


def test_person_pose_is_lazy_and_uses_only_same_frame_actual_person_boxes(factory):
    test = factory()
    assert test.engine._person_pose is None
    packet = test.process()
    rows = test.engine.person_snapshot()
    assert len(rows) == len(test.controls.models) == 1
    frame,boxes = test.controls.models[0].calls[0]
    assert frame is packet.frame and boxes == [{'label':'person','bbox':[.2,.1,.2,.8],'confidence':.9}]
    row = rows[0]
    assert row['bbox'] == [.2,.1,.2,.8] and row['bbox_format'] == 'source_normalized_xywh'
    assert row['source_frame'] == packet.sequence and row['source_session_id'] == packet.source_session_id
    assert datetime.fromisoformat(row['source_timestamp']).timestamp() == pytest.approx(packet.wall_time)
    assert row['runtime_mode'] == 'TEST' and row['is_simulated'] is True and row['source_type'] == 'video_file'
    assert row['detector_backend'] == 'explicit_detector_fixture'
    assert row['feet_visible'] is False and row['foot_point'] is None and row['posture'] == 'unknown'
    assert row['scene_version'] is None and row['ground_contact_confirmed'] is False
    assert test.controls.tracker_labels == [['unit phone']]
    assert test.engine.recognition_snapshot()['hand_count'] == 1
    assert test.engine.recognition_snapshot()['candidates'][0]['accepted'] is True
    assert all(track['item_id'] == 'unit-phone' for track in test.controls.tracks)


@pytest.mark.parametrize('mode,enabled', [('aruco',True),('experimental',False)])
def test_disabled_or_aruco_never_loads_pose_even_with_person_fixture(factory,mode,enabled):
    test = factory(mode=mode,config={'enabled':enabled})
    test.process()
    assert test.controls.models == [] and test.engine.person_snapshot() == []
    assert test.engine.health()['person_pose']['enabled'] is False


def test_maximum_people_bounded_and_no_person_frame_does_not_load_model(factory):
    test = factory()
    test.controls.boxes = []
    test.process()
    assert test.controls.models == [] and test.engine.person_snapshot() == []
    assert test.engine.health()['person_pose']['phase'] == 'no_person'
    test.controls.boxes = [(10,10,40,180),(80,10,40,180),(150,10,40,180)]
    test.process()
    assert len(test.engine.person_snapshot()) == 2, test.engine.health()['person_pose']
    assert len(test.controls.models[0].calls[0][1]) == 2
    test.controls.boxes = []
    test.process()
    assert test.engine.person_snapshot() == []
    assert test.engine.health()['person_pose']['last_latency_ms'] is None
    assert len(test.controls.models[0].calls) == 1


@pytest.mark.parametrize('fault',['exception','wrong_box','unexpected_adapter'])
def test_pose_failure_does_not_erase_objects_or_hands_and_never_reuses_foot(factory,monkeypatch,fault):
    test = factory()
    test.controls.feet = True
    test.process()
    assert test.engine.person_snapshot()[0]['feet_visible'] is True
    if fault == 'exception':
        test.controls.pose_error = True
    elif fault == 'wrong_box':
        test.controls.mismatch = True
    else:
        def broken(*_): raise MemoryError('explicit full person adapter fault')
        monkeypatch.setattr(test.engine,'_process_person_frame',broken)
    packet = test.process()
    rows = test.engine.person_snapshot()
    assert rows == [] if fault == 'unexpected_adapter' else all(row['feet_visible'] is False and row['foot_point'] is None for row in rows)
    snapshot = test.engine.recognition_snapshot()
    assert snapshot['source_frame'] == packet.sequence and snapshot['hand_count'] == 1
    assert snapshot['candidates'][0]['accepted'] is True and test.controls.hand_calls == 2
    assert test.engine.health()['person_pose']['phase'] == 'failed'


@pytest.mark.parametrize('state',['offline','error','reconnecting','disconnected','stopped'])
def test_capture_unavailable_clears_transient_person_and_rate_immediately(factory,state):
    test = factory()
    test.process()
    assert test.engine.person_snapshot()
    test.controls.source_status = state
    assert test.engine.person_snapshot() == [] and test.engine._person_snapshot is None
    assert test.engine.health()['person_pose']['current_person_count'] == 0
    assert test.engine.health()['person_pose']['inference_fps'] == 0


def test_expired_snapshot_and_replayed_frame_cannot_refresh_source_timestamp(factory):
    test = factory()
    packet = test.process(stamp=time.monotonic()-4)
    assert test.engine.person_snapshot() == []
    assert test.engine.health()['person_pose']['phase'] == 'stale'
    test.process()
    before = test.engine.person_snapshot()[0]
    test.engine._process_persons(packet.frame,[],packet)
    assert test.engine.person_snapshot() == []
    assert test.engine._person_snapshot['persons'] == []
    assert before['source_frame'] == 2


def test_source_resolution_camera_shift_and_pending_scene_clear_old_identity(factory,monkeypatch):
    test = factory()
    test.process()
    previous = test.engine.person_snapshot()[0]['person_track_id']
    for kwargs in ({'session':'unit-new-source'},{'session':'unit-new-source','shape':(280,360,3)}):
        test.process(**kwargs)
        next_id = test.engine.person_snapshot()[0]['person_track_id']
        assert next_id != previous
        previous = next_id
    test.engine.scene_requires_review = False
    monkeypatch.setattr(test.engine.scene_guard,'check',lambda *_:True)
    test.process(session='unit-new-source',shape=(280,360,3))
    assert test.engine.person_snapshot()[0]['person_track_id'] != previous
    assert test.engine.person_snapshot()[0]['scene_version'] is None
    test.engine.update_scene([])
    assert test.engine.person_snapshot() == []


def test_stop_closes_pose_and_start_recreates_once_without_new_subscriber_model(factory,monkeypatch):
    test = factory()
    test.process()
    model = test.controls.models[0]
    for _ in range(5): test.engine.person_snapshot()
    assert len(test.controls.models) == len(model.calls) == 1
    test.engine.stop()
    assert model.closed and test.engine.person_snapshot() == []
    monkeypatch.setattr(test.engine.capture,'start',lambda:True)
    monkeypatch.setattr(test.engine,'_run',lambda:None)
    monkeypatch.setattr(test.engine,'_run_preview',lambda:None)
    assert test.engine.start()
    test.process(session='unit-restarted-source')
    assert len(test.controls.models) == 2 and not test.controls.models[1].closed


def test_applied_scene_version_and_actual_foot_contract_reach_sqlite_mapper(factory,context):
    service,db,camera = context
    scene = save_3d(context,kind='floor',validation=True)
    zones = db.list('zones')
    test = factory(zones=zones,camera=camera)
    test.controls.boxes = [(48,30,32,42)]
    test.controls.feet = True
    packet = test.process(session=scene['source_session_id'],shape=(120,160,3))
    rows = test.engine.person_snapshot()
    assert rows[0]['scene_version'] == scene['scene_version']
    assert rows[0]['foot_confidence'] == .82 and rows[0]['posture'] == 'standing'
    markers = service.person_markers(camera,rows,now_timestamp=packet.wall_time+.01)
    assert len(markers) == 1 and markers[0]['position_status'] == 'mapped'
    assert markers[0]['map_position'][1] == 0
    assert markers[0]['frame_id'] == packet.sequence
    assert db.count('item_current_state') == db.count('movement_events') == 0
    test.engine.scene_requires_review = True
    assert test.engine.person_snapshot()[0]['scene_version'] is None


def test_conflicting_or_missing_confirmed_zone_versions_never_claim_current_scene(factory):
    test = factory()
    zone = {'id':'unit-floor','points':[[0,0],[1,0],[1,1],[0,1]],'scene_metadata':{
        'surface_type':'floor','confirmed_by_user':True,'calibration_status':'calibrated','scene_version':2}}
    test.engine.zone_manager.replace([zone])
    test.process()
    assert test.engine.person_snapshot()[0]['scene_version'] == 2
    for version in (3,None,True):
        other = deepcopy(zone)
        other['id'] = 'other-floor'
        other['scene_metadata']['scene_version'] = version
        test.engine.zone_manager.replace([zone,other])
        test.process()
        assert test.engine.person_snapshot(), test.engine.health()['person_pose']
        assert test.engine.person_snapshot()[0]['scene_version'] is None
