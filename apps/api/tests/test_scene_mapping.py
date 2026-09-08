"""Real matrix/SQLite contracts with explicit synthetic point annotations.

No physical room, phone recognition, or foot detector accuracy is claimed.
The inherited suggestion fixture deliberately avoids model/hardware startup.
"""
from copy import deepcopy
from datetime import datetime, timezone
import math

import pytest

from apps.api.app.scene import SceneError, SceneService
from apps.api.app.scene_mapping import MappingError, MappingSettings, iso
from apps.api.tests.test_scene_service import context, body, propose, POLYGON


PHYSICAL = {"width":2.,"depth":1.,"height":.8,"thickness":.05,"origin_x":3.,"origin_z":5.,"rotation_y":0.,"unit":"relative"}
CALIBRATION = {"image_points":POLYGON,"plane_points":[[0,0],[2,0],[2,1],[0,1]],"validation_points":[]}


def save_3d(context, *, kind="table", validation=False, rotation=0):
    service, _, camera = context
    physical = {**PHYSICAL,"rotation_y":rotation}
    if kind == "floor":
        physical.update(height=0,thickness=.02)
    calibration = deepcopy(CALIBRATION)
    if validation:
        calibration["validation_points"] = [{"image":[.4,.5],"plane":[1,.5]}]
    return service.save(camera,body(propose(context),surface_type=kind,physical=physical,calibration=calibration))


def observation(scene, **changes):
    return {"item_id":"registered-item","camera_id":scene["camera_id"],"scene_version":scene["scene_version"],
        "source_session_id":scene["source_session_id"],"source_frame":20,"source_type":"video_file",
        "runtime_mode":"TEST","is_simulated":True,"observed_at":datetime.now(timezone.utc).isoformat(),
        "position":[.4,.5],"bbox":[56,54,16,12],"identity_evidence":{"accepted":True,"profile_version":1},
        "evidence_type":"observed","status":"last_seen","holding_status":"not_established",
        "zone_id":scene["surfaces"][0]["surface_id"],"support_surface_confirmed":True,
        "support_contact_confirmed":True,**changes}


def item_marker(context, scene, **changes):
    service, _, camera = context
    return service.item_markers(camera,[{"item_id":"registered-item","last_observed":observation(scene,**changes)}])[0]


def person(scene, stamp, **changes):
    return {"person_track_id":"anonymous-1","camera_id":scene["camera_id"],"scene_version":scene["scene_version"],
        "source_session_id":scene["source_session_id"],"source_frame":21,"source_timestamp":stamp,
        "source_type":"video_file","runtime_mode":"TEST","is_simulated":True,"detector_backend":"actual_person_contract_fixture",
        "bbox":[.3,.25,.2,.35],"feet_visible":False,"foot_point":None,**changes}


@pytest.mark.parametrize("kind,parts", [("table",5),("sofa",4),("cabinet",2),("floor",1),("bed",2)])
def test_real_3d_primitives_are_derived_only_from_confirmed_user_surface(context, kind, parts):
    service, db, camera = context
    scene = save_3d(context,kind=kind)
    objects = scene["world_geometry"]["objects"]
    assert len(objects) == parts
    assert {o["surface_id"] for o in objects} == {scene["surfaces"][0]["surface_id"]}
    assert all(o["shape"] == "box" and min(o["size"]) > 0 and o["estimated"] for o in objects)
    assert scene["world_geometry"]["scale_is_measured"] is False
    assert db.count("item_current_state") == db.count("movement_events") == 0
    restored = SceneService(db,service.media,service.model_root).get(camera["id"])
    assert restored["world_geometry"] == scene["world_geometry"]
    assert restored["surfaces"] == scene["surfaces"]


def test_legacy_schematic_does_not_invent_a_room(context):
    service, _, camera = context
    scene = service.save(camera,body(propose(context)))
    assert scene["calibration_status"] == "schematic"
    assert scene["world_geometry"]["objects"] == []
    assert scene["world_geometry"]["unit"] is None


def test_four_fit_points_are_not_independent_validation(context):
    scene = save_3d(context)
    surface = scene["surfaces"][0]
    assert surface["validation"]["status"] == "not_validated"
    assert surface["validation"]["max_error"] is None
    assert surface["validation"]["independent_point_count"] == 0
    marker = item_marker(context,scene)
    assert marker["position_status"] == "mapped_unvalidated"
    assert marker["map_position"] == pytest.approx([4,.8,5.5])


def test_independent_point_and_rotation_map_to_one_world(context):
    scene = save_3d(context,validation=True,rotation=90)
    marker = item_marker(context,scene)
    assert marker["position_status"] == "mapped"
    assert marker["map_position"] == pytest.approx([3.5,.8,4])
    assert marker["frame_id"] == 20 and marker["scene_version"] == scene["scene_version"]
    assert scene["surfaces"][0]["validation"]["max_error"] < 1e-10


@pytest.mark.parametrize("changes", [
    {"image_points":[[.1,.2],[.2,.2],[.3,.2],[.4,.2]]},
    {"image_points":[[.1,.2],[.7,.8],[.7,.2],[.1,.8]]},
    {"image_points":[[.1,.2],[.100001,.2],[.100001,.200001],[.1,.200001]]},
    {"plane_points":[[0,0],[2,0],[2,0],[0,1]]},
    {"plane_points":[[0,0],[200,0],[2,1],[0,1]]},
    {"image_points":[[float("nan"),.2],[.7,.2],[.7,.8],[.1,.8]]},
    {"validation_points":[{"image":POLYGON[0],"plane":[0,0]}]},
    {"validation_points":[{"image":[.4,.5],"plane":[1,.5]}]*2},
    {"image_to_surface":[[1,0,0],[0,1,0],[0,0,1]]},
])
def test_degenerate_or_client_forged_calibration_cannot_save(context, changes):
    service, db, camera = context
    scene = propose(context)
    with pytest.raises(SceneError):
        service.save(camera,body(scene,physical=PHYSICAL,calibration={**CALIBRATION,**changes}))
    assert service.get(camera["id"]) == scene and db.count("zones") == 0


def test_failed_independent_validation_preserves_geometry_but_blocks_precision(context):
    service, _, camera = context
    calibration = {**CALIBRATION,"validation_points":[{"image":[.4,.5],"plane":[.1,.1]}]}
    scene = service.save(camera,body(propose(context),physical=PHYSICAL,calibration=calibration))
    assert scene["calibration_status"] == "validation_failed"
    marker = item_marker(context,scene)
    assert marker["map_position"] is None and marker["position_status"] == "region_only"
    assert len(scene["world_geometry"]["objects"]) == 5


@pytest.mark.parametrize("changes,reason", [
    ({"scene_version":0},"scene_version_or_source_mismatch"),
    ({"scene_version":True},"scene_version_or_source_mismatch"),
    ({"source_session_id":"restarted"},"scene_version_or_source_mismatch"),
    ({"is_simulated":False},"scene_version_or_source_mismatch"),
    ({"source_frame":0},"scene_version_or_source_mismatch"),
    ({"observed_at":"2020-01-01T00:00:00Z"},"scene_version_or_source_mismatch"),
    ({"identity_evidence":{"accepted":False}},"identity_or_observation_unverified"),
    ({"holding_status":"co_moving"},"handheld_object_is_not_on_support_plane"),
    ({"holding_status":"holding_uncertain"},"handheld_object_is_not_on_support_plane"),
])
def test_item_provenance_identity_and_held_guards(context,changes,reason):
    scene = save_3d(context,validation=True)
    marker = item_marker(context,scene,**changes)
    assert marker["map_position"] is None and marker["reason"] == reason


def test_support_region_is_not_proof_of_object_contact(context):
    scene = save_3d(context,validation=True)
    marker = item_marker(context,scene,support_contact_confirmed=False)
    assert marker["position_status"] == "region_only" and marker["map_position"] is None
    assert marker["region"]["approximate"] is True
    assert marker["region"]["world_polygon"][0] == pytest.approx([3,.8,5])


def test_surface_valid_area_and_camera_invalidation(context):
    service, _, camera = context
    scene = save_3d(context,validation=True)
    marker = item_marker(context,scene,position=[.9,.9])
    assert marker["map_position"] is None and marker["region"] is None
    service.invalidate(camera["id"],"camera moved")
    marker = item_marker(context,scene)
    assert marker["map_position"] is None
    assert marker["reason"] == "scene_version_or_source_mismatch"


def test_unreasonable_projected_jump_does_not_replace_good_mapping(context):
    scene = save_3d(context,validation=True)
    stamp = datetime.now(timezone.utc).timestamp()
    assert item_marker(context,scene,position=[.2,.3],observed_at=stamp)["map_position"] is not None
    jumped = item_marker(context,scene,position=[.65,.75],observed_at=stamp+.01)
    assert jumped["map_position"] is None and jumped["reason"] == "implausible_projection_jump"


def test_previous_structure_is_bounded_to_one_version(context):
    service, _, camera = context
    scene = save_3d(context)
    for _ in range(3):
        scene = service.save(camera,{key:scene[key] for key in ("scene_version","snapshot_id","source_session_id","source_frame","surfaces")})
    assert scene["previous_version"]["scene_version"] == scene["scene_version"]-1
    assert "previous_version" not in scene["previous_version"]


@pytest.mark.parametrize("changes", [{}, {"feet_visible":True,"foot_point":[.4,.58],"foot_confidence":.4},
    {"feet_visible":True,"foot_point":[.4,.3],"foot_confidence":.99},
    {"feet_visible":True,"foot_point":[.4,.58],"foot_confidence":.99,"posture":"sitting"}])
def test_body_box_or_unreliable_feet_never_becomes_floor_point(context, changes):
    service, db, camera = context
    scene = save_3d(context,kind="floor",validation=True)
    stamp = datetime.now(timezone.utc).timestamp()
    markers = service.person_markers(camera,[person(scene,stamp,**changes)],now_timestamp=stamp)
    assert len(markers) == 1 and markers[0]["map_position"] is None
    assert markers[0]["reason"] == "feet_not_reliably_visible_ground_unconfirmed"
    assert db.count("tracks") == db.count("item_current_state") == 0


def test_person_reliable_foot_maps_then_ttl_fades_and_expires_without_refresh(context):
    service, db, camera = context
    scene = save_3d(context,kind="floor",validation=True)
    stamp = datetime.now(timezone.utc).timestamp()
    p = person(scene,stamp,feet_visible=True,foot_point=[.4,.58],foot_confidence=.9)
    initial = service.person_markers(camera,[p],now_timestamp=stamp)[0]
    assert initial["position_status"] == "mapped" and initial["map_position"][1] == 0
    # Re-polling the same source does not refresh the actual observation time.
    stale = service.person_markers(camera,[p],now_timestamp=stamp+4)[0]
    assert stale["freshness"] == "stale" and stale["observation_timestamp"] == iso(stamp)
    assert service.person_markers(camera,now_timestamp=stamp+7) == []
    assert db.count("tracks") == 0


def test_person_changed_scene_and_old_frame_are_rejected(context):
    service, _, camera = context
    scene = save_3d(context,kind="floor")
    stamp = datetime.now(timezone.utc).timestamp()
    assert service.person_markers(camera,[person(scene,stamp,scene_version=0)],now_timestamp=stamp) == []
    assert service.person_markers(camera,[person(scene,stamp-10)],now_timestamp=stamp) == []
    assert service.person_markers(camera,[person(scene,stamp)],now_timestamp=stamp)
    service.invalidate(camera["id"],"changed")
    assert service.person_markers(camera,now_timestamp=stamp+1) == []


def test_settings_reject_unbounded_or_invalid_values():
    for values in ({"person_expire_seconds":1},{"foot_confidence":2},{"max_persons":1000},{"max_condition":math.inf}):
        with pytest.raises(MappingError):
            MappingSettings(**values)


def test_actual_proposal_contract_creates_only_unconfirmed_editable_3d_draft(context):
    service, db, camera = context
    scene = propose(context)
    # Fixture supplies exact model-like furniture outputs; this is not a real
    # room/model accuracy test. There is no geometry when no proposals exist.
    assert scene["world_geometry"]["status"] == "draft"
    assert scene["world_geometry"]["eligible_for_mapping"] is False
    assert len(scene["world_geometry"]["objects"]) == 11  # sofa 4, table 5, bed 2
    assert all(o["source"] == "model_proposal_estimate" and o["confirmed_by_user"] is False for o in scene["world_geometry"]["objects"])
    assert all(p["physical_suggestion"]["unit"] == "relative" for p in scene["proposals"])
    assert scene["surfaces"] == [] and db.count("zones") == 0
    assert service.person_markers(camera,[]) == []


def test_fresh_model_proposal_does_not_replace_previously_confirmed_geometry(context):
    service, _, camera = context
    saved = save_3d(context,rotation=35)
    refreshed = propose(context,sequence=2)
    assert refreshed["world_geometry"] == saved["world_geometry"]
    assert refreshed["calibration_status"] == "needs_confirmation"
    assert all(s["confirmed_by_user"] is False for s in refreshed["surfaces"])


def test_no_furniture_detection_cannot_invent_a_draft_room(context):
    service, _, _ = context
    service._detector.detect = lambda _frame: []  # explicitly empty unit detector
    scene = propose(context)
    assert scene["world_geometry"]["objects"] == [] and scene["world_geometry"]["status"] == "empty"
