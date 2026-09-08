"""Bounded public-video regression: one target, three other phones, backgrounds.

This is NOT user-phone/physical-camera/general identity accuracy. Reference20s
is separate from held-out41/45/50/54s. Positive boxes are actual NanoDet; negative
phone ROIs are manually reviewed labels only, never claimed detector outputs.
Sources, hashes and CC-BY-SA attribution stay in scripts/test-assets/*.source.json.
"""
import hashlib
import json
from pathlib import Path

import cv2
import pytest

from services.vision.detectors.appearance import AppearanceEncoder, ProfileMatcher, DEFAULT_MODEL_PATH
from services.vision.detectors.nanodet import NanoDetDetectorBackend

ROOT = Path(__file__).resolve().parents[3]
ASSETS = ROOT/'data/test-assets'
MODEL = ROOT/'data/models/object_detection_nanodet_2022nov.onnx'


def read_frame(path, second):
    capture = cv2.VideoCapture(str(path))
    try:
        assert capture.isOpened()
        assert capture.set(cv2.CAP_PROP_POS_MSEC, second*1000)
        ok,frame=capture.read()
        assert ok and frame is not None
        return frame,int(capture.get(cv2.CAP_PROP_POS_FRAMES))-1
    finally:
        capture.release()


@pytest.fixture(scope='module')
def public_matcher():
    paths=[ASSETS/f'public-phone-{name}.webm' for name in ('lotti','android')]
    if not all(path.is_file() for path in [*paths,MODEL,DEFAULT_MODEL_PATH]):
        pytest.skip('NOT_RUN: pinned public footage or real model weights missing; no mock substitute')
    metadata=[]
    for path in paths:
        entry=json.loads((ROOT/'scripts/test-assets'/path.with_suffix('.source.json').name).read_text(encoding='utf-8'))
        assert hashlib.sha256(path.read_bytes()).hexdigest()==entry['sha256']
        metadata.append(entry)
    reference,reference_index=read_frame(paths[0],20)
    h,w=reference.shape[:2]
    x,y,bw,bh=[round(v*e) for v,e in zip(metadata[0]['reference_region_xywh_normalized'],(w,h,w,h))]
    encoder=AppearanceEncoder()
    assert encoder.health()['available'],encoder.health()
    try:
        vector=encoder.encode(reference[y:y+bh,x:x+bw])
        matcher=ProfileMatcher([{'id':'public-lotti-phone','type':'phone','appearance_profile':{
            'status':'ready','profile_version':1,'model_id':encoder.model_id,
            'model_version':encoder.model_version,'dimension':384,'embeddings':[vector]}}],
            encoder,threshold=.82,margin=.06)
        detector=NanoDetDetectorBackend(MODEL)
        assert detector.health()['available']
        yield paths,matcher,detector,reference_index,hashlib.sha256(reference.tobytes()).hexdigest()
    finally:
        encoder.close()


@pytest.mark.parametrize('second',[41,45,50,54])
def test_single_reference_matches_heldout_actual_phone_candidates(public_matcher,second,record_property):
    paths,matcher,detector,reference_index,reference_hash=public_matcher
    frame,index=read_frame(paths[0],second)
    assert index>reference_index and hashlib.sha256(frame.tobytes()).hexdigest()!=reference_hash
    candidates=[c for c in detector.detect(frame) if c.label=='cell phone']
    assert candidates, 'Actual NanoDet did not find a phone; no fixture fallback'
    results=[]
    for c in candidates:
        x,y,w,h=c.bbox
        results.append(matcher.match(frame[y:y+h,x:x+w],category=c.label))
    assert any(r['accepted'] and r['item_id']=='public-lotti-phone' for r in results)
    record_property('scope','public_video_heldout_not_user_phone_accuracy')
    record_property('best_score',max(r['best_score'] for r in results))


@pytest.mark.parametrize('second',[1,8,20])
def test_other_public_phones_are_not_the_registered_phone(public_matcher,second,record_property):
    paths,matcher,_,_,_=public_matcher
    frame,_=read_frame(paths[1],second)
    assert frame.shape[:2]==(480,854)
    scores=[]
    for x,y,w,h in ((60,35,209,430),(318,6,222,460),(587,33,214,432)):
        result=matcher.match(frame[y:y+h,x:x+w],category='cell phone')
        assert not result['accepted'] and result['rejection']=='below_threshold'
        scores.append(result['best_score'])
    record_property('scope','three_manually_labelled_other_phones_not_detector_accuracy')
    record_property('maximum_negative_score',max(scores))


@pytest.mark.parametrize('second',[41,45,50,54])
def test_public_background_crops_are_not_the_registered_phone(public_matcher,second,record_property):
    paths,matcher,_,_,_=public_matcher
    frame,_=read_frame(paths[0],second)
    scores=[]
    for x,y,w,h in ((0,0,240,160),(0,320,240,160)):
        result=matcher.match(frame[y:y+h,x:x+w],category='cell phone')
        assert not result['accepted'] and result['rejection']=='below_threshold'
        scores.append(result['best_score'])
    record_property('scope','manually_labelled_backgrounds_not_general_accuracy')
    record_property('maximum_negative_score',max(scores))
