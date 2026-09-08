"""Geometry units and bounded public footage checks, not phone classification.

Black rectangles/remotes are intentional false proposals. Identity tests use
the actual local DINO model and still do not represent a camera/DB acceptance.
"""
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from services.vision.detectors.base import Detection
from services.vision.detectors.phone_shape import PhoneShapeProposer,PhoneShapeSettings,_iou

ROOT=Path(__file__).resolve().parents[3]


def rectangle(kind='black'):
    frame=np.full((480,854,3),220,np.uint8)
    cv2.rectangle(frame,(260,160),(560,285),(20,20,20),-1)
    if kind=='screen':cv2.rectangle(frame,(277,172),(543,273),(245,245,245),-1)
    if kind=='remote':
        for x in (285,325,365,405,445,485,525):
            for y in (190,230,260):cv2.circle(frame,(x,y),8,(150,150,150),-1)
    return frame


@pytest.mark.parametrize('settings',[
    {'max_width':641},{'max_candidates':4},{'max_contours':301},{'max_width':True},
    {'max_candidates':0},{'max_contours':0},{'max_width':640.0},
    {'min_aspect':float('nan')},{'nms_iou':float('inf')},{'min_area_fraction':True},
    {'dark_threshold':256},{'canny_low':150,'canny_high':50},
    {'min_area_fraction':.8,'max_area_fraction':.1},{'min_aspect':4,'max_aspect':2},
    {'max_input_pixels':16777217},{'unrecognized_option':True},
])
def test_configuration_is_finite_and_hard_bounded(settings):
    with pytest.raises(ValueError):PhoneShapeProposer(settings)


def test_settings_are_immutable_and_roundtrip():
    settings=PhoneShapeSettings(max_candidates=2)
    assert PhoneShapeProposer(settings.to_dict()).settings==settings
    with pytest.raises(AttributeError):settings.max_width=2048


@pytest.mark.parametrize('frame',[
    None,np.zeros((10,10)),np.zeros((10,10,3),np.float32),np.zeros((10,10,4),np.uint8),
    np.zeros((0,10,3),np.uint8),
    np.broadcast_to(np.zeros((1,1,3),np.uint8),(4097,4097,3)),
])
def test_invalid_or_unbounded_frame_is_not_accepted(frame):
    with pytest.raises(ValueError):PhoneShapeProposer().detect(frame)


@pytest.mark.parametrize('kind',['black','screen','remote'])
def test_rectangular_negative_can_be_proposed_but_never_claims_identity(kind):
    frame=rectangle(kind)
    original=frame.copy()
    candidates=PhoneShapeProposer().detect(frame)
    assert candidates, 'These intentionally expose geometry false positives'
    assert len(candidates)<=3
    for candidate in candidates:
        assert candidate.identity.startswith('generic:phone_shape:')
        assert candidate.label=='cell phone'  # routing only, not classifier proof
        assert candidate.metadata['category_evidence']=='geometry_only'
        assert candidate.metadata['certainty']=='candidate'
        assert candidate.metadata['backend']=='phone_shape_proposal'
        assert candidate.metadata['score_kind']=='geometry_score_not_probability'
        assert 'item_id' not in candidate.metadata and 'accepted' not in candidate.metadata
    np.testing.assert_array_equal(frame,original)


def test_source_coordinates_and_work_buffers_are_bounded():
    proposer=PhoneShapeProposer()
    candidates=proposer.detect(rectangle())
    assert max(_iou(candidate.bbox,(260,160,301,126)) for candidate in candidates)>.9
    for candidate in candidates:
        x,y,w,h=candidate.bbox
        assert 0<=x<x+w<=854 and 0<=y<y+h<=480
    stats=proposer.health()
    assert max(stats['processing_size'])<=640 and stats['contours_examined']<=300


def test_noise_and_many_rectangles_never_expand_candidate_or_contour_limits():
    frame=np.random.default_rng(173).integers(0,256,(720,1280,3),dtype=np.uint8)
    for y in (30,230,430):
        for x in (50,380,710,1040):cv2.rectangle(frame,(x,y),(min(x+200,1279),y+80),(0,0,0),-1)
    proposer=PhoneShapeProposer({'max_candidates':2,'max_contours':20})
    assert len(proposer.detect(frame))<=2
    assert proposer.health()['contours_examined']<=20
    assert max(proposer.health()['processing_size'])<=640


def test_no_candidates_on_uniform_bright_background():
    assert PhoneShapeProposer().detect(np.full((480,854,3),220,np.uint8))==[]


@pytest.mark.parametrize('label',['cell phone','phone','手机'])
def test_existing_phone_skips_extra_geometry_work(label,monkeypatch):
    proposer=PhoneShapeProposer()
    values=[Detection('generic:existing',label,(1,2,30,60),(16,32),.5,'experimental')]
    monkeypatch.setattr(proposer,'detect',lambda *_:pytest.fail('Must not duplicate an existing phone candidate'))
    assert proposer.supplement(None,values) is values


def test_merge_keeps_original_detections_and_drops_overlapping_addition():
    proposer=PhoneShapeProposer()
    frame=rectangle()
    candidate=proposer.detect(frame)[0]
    original=Detection('generic:bottle','bottle',candidate.bbox,candidate.center,.7,'experimental')
    values=[original]
    merged=proposer.supplement(frame,values)
    assert merged==values and merged[0] is original and len(values)==1


def read_public(name,second):
    path=ROOT/'data/test-assets'/f'public-phone-{name}.webm'
    metadata=ROOT/'scripts/test-assets'/f'public-phone-{name}.source.json'
    if not path.is_file() or not metadata.is_file():pytest.skip('NOT_RUN: pinned public footage unavailable')
    provenance=json.loads(metadata.read_text(encoding='utf-8'))
    assert hashlib.sha256(path.read_bytes()).hexdigest()==provenance['sha256']
    capture=cv2.VideoCapture(str(path))
    try:
        assert capture.isOpened() and capture.set(cv2.CAP_PROP_POS_MSEC,second*1000)
        ok,frame=capture.read();assert ok
        return frame,provenance
    finally:capture.release()


@pytest.mark.parametrize('second',[1,4,8,12,16,20])
def test_public_android_frames_have_three_geometric_candidates_without_roi_injection(second,record_property):
    frame,_=read_public('android',second)
    candidates=PhoneShapeProposer().detect(frame)
    # Reviewed labels are used AFTER inference, only for this small evaluation.
    expected=((60,35,209,430),(318,6,222,460),(587,33,214,432))
    assert len(candidates)==3
    assert all(any(_iou(candidate.bbox,box)>=.5 for candidate in candidates) for box in expected)
    record_property('scope','public_geometry_recall_not_identity_or_user_phone_accuracy')


def test_actual_dino_rejects_synthetic_rectangles_remotes_as_public_lotti_identity(record_property):
    from services.vision.detectors.appearance import AppearanceEncoder,DEFAULT_MODEL_PATH,ProfileMatcher
    if not DEFAULT_MODEL_PATH.is_file():pytest.skip('NOT_RUN: actual local DINO unavailable')
    reference,metadata=read_public('lotti',20)
    h,w=reference.shape[:2]
    x,y,bw,bh=[round(v*e) for v,e in zip(metadata['reference_region_xywh_normalized'],(w,h,w,h))]
    encoder=AppearanceEncoder();assert encoder.health()['available']
    try:
        feature=encoder.encode(reference[y:y+bh,x:x+bw])
        matcher=ProfileMatcher([{'id':'lotti-phone','type':'phone','appearance_profile':{
            'status':'ready','profile_version':1,'model_id':encoder.model_id,'model_version':encoder.model_version,
            'dimension':384,'embeddings':[feature]}}],encoder,threshold=.82,margin=.06)
        scores=[]
        for kind in ('black','screen','remote'):
            frame=rectangle(kind);candidates=PhoneShapeProposer().detect(frame)
            assert candidates
            for candidate in candidates:
                x,y,w,h=candidate.bbox
                result=matcher.match(frame[y:y+h,x:x+w],category=candidate.label)
                assert not result['accepted'] and result['rejection']=='below_threshold'
                scores.append(result['best_score'])
        record_property('negative_geometry_candidates',len(scores))
        record_property('maximum_dino_score',max(scores))
        record_property('scope','one_public_reference_negative_challenge_not_general_accuracy')
    finally:encoder.close()
