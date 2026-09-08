"""Explicit synthetic boxes: anonymous association contracts, not person accuracy."""
import pytest

from apps.api.app.schemas import SettingsInput
from services.vision.person_tracking import AnonymousPersonTracker, person_configuration


def row(x=.1, y=.1, w=.2, h=.7):
    return {'bbox':[x,y,w,h],'label':'person'}


def test_person_config_defaults_are_central_and_normalized():
    config = person_configuration()
    assert config['max_people'] == 2 and config['enabled'] is True
    assert config['tracking'] == {'min_iou':.3,'ambiguity_margin':.1,'max_gap_seconds':.75,'snapshot_max_age_seconds':3.}
    assert SettingsInput(person_pose={}).person_pose == config


@pytest.mark.parametrize('value', [
    {'max_people':0},{'max_people':True},{'enabled':'yes'},{'unknown':1},
    {'tracking':None},{'tracking':{'max_gap_seconds':float('nan')}},
    {'tracking':{'snapshot_max_age_seconds':1000}},{'tracking':{'min_iou':False}},
    {'tracking':{'ambiguity_margin':0}},{'tracking':{'unknown':1}},
])
def test_invalid_person_settings_fail_before_runtime(value):
    with pytest.raises(ValueError):
        SettingsInput(person_pose=value)


def test_ids_follow_spatial_boxes_not_detection_order_or_class_index():
    tracker = AnonymousPersonTracker()
    first = tracker.update([row(.1),row(.65)],'source-a',1,100.)
    next_frame = tracker.update([row(.66),row(.11)],'source-a',2,100.2)
    assert [value['person_track_id'] for value in next_frame] == [first[1]['person_track_id'],first[0]['person_track_id']]
    assert all(value['tracking_stable'] for value in next_frame)
    assert all(value['identity_kind'] == 'anonymous_short_lived_iou_track' for value in next_frame)


def test_ambiguous_overlapping_people_get_new_ids_instead_of_wrong_continuation():
    tracker = AnonymousPersonTracker()
    before = tracker.update([row(.2),row(.21)],'source-a',1,100.)
    after = tracker.update([row(.205),row(.205)],'source-a',2,100.2)
    assert not {r['person_track_id'] for r in before} & {r['person_track_id'] for r in after}
    assert len({r['person_track_id'] for r in after}) == 2
    assert all(not value['tracking_stable'] for value in after)


@pytest.mark.parametrize('change', ['session','gap','missing'])
def test_source_gap_and_missing_frame_cannot_reuse_anonymous_identity(change):
    tracker = AnonymousPersonTracker()
    before = tracker.update([row()],'source-a',1,100.)[0]
    if change == 'missing':
        assert tracker.update([],'source-a',2,100.2) == []
    after = tracker.update([row()],'source-b' if change == 'session' else 'source-a',3,
                           101. if change == 'gap' else 100.4)[0]
    assert before['person_track_id'] != after['person_track_id']
    assert after['tracking_stable'] is False


@pytest.mark.parametrize('frame,stamp', [(1,100.2),(0,100.2),(2,100.),(True,100.2)])
def test_replays_never_refresh_current_observation(frame,stamp):
    tracker = AnonymousPersonTracker()
    tracker.update([row()],'source-a',1,100.)
    if type(frame) is bool:
        with pytest.raises(ValueError):
            tracker.update([row()],'source-a',frame,stamp)
    else:
        assert tracker.update([row()],'source-a',frame,stamp) == []
        assert tracker._timestamp == 100.


def test_invalid_geometry_resets_old_ids_and_frame_count_is_bounded():
    tracker = AnonymousPersonTracker()
    first = tracker.update([row()],'source-a',1,100.)[0]
    with pytest.raises(ValueError):
        tracker.update([row(w=float('nan'))],'source-a',2,100.2)
    after = tracker.update([row()],'source-a',3,100.4)[0]
    assert first['person_track_id'] != after['person_track_id']
    with pytest.raises(ValueError):
        tracker.update([row(),row(),row()],'source-a',4,100.6)
    assert tracker._tracks == []
