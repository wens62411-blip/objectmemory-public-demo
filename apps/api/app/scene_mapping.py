"""User-confirmed planar geometry and conservative, source-bound map markers.

Image coordinates are normalized x-right/y-down. Surface coordinates are u/v
in the explicitly supplied unit. World coordinates are X/right, Y/up, Z/depth.
The orbiting display camera is never an input to these transforms.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math

import cv2
import numpy as np


class MappingError(ValueError):
    pass


@dataclass(frozen=True)
class MappingSettings:
    min_image_area: float = .001
    max_condition: float = 1e7
    validation_error_fraction: float = .03
    min_validation_distance: float = .02
    foot_confidence: float = .7
    person_live_seconds: float = 3.
    person_expire_seconds: float = 6.
    max_jump_fraction_per_second: float = 2.
    jump_reset_seconds: float = 10.
    max_persons: int = 32
    max_item_history: int = 512

    def __post_init__(self):
        for key, value in vars(self).items():
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise MappingError(f"Invalid scene mapping setting: {key}")
        if self.person_expire_seconds <= self.person_live_seconds or self.foot_confidence > 1:
            raise MappingError("Invalid person confidence/TTL limits")
        if type(self.max_persons) is not int or type(self.max_item_history) is not int or self.max_persons > 128 or self.max_item_history > 4096:
            raise MappingError("Scene marker caches must be bounded")


def number(value, name, lower, upper):
    if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
        raise MappingError(f"{name} must be a finite number in [{lower}, {upper}]")
    return float(value)


def point(value, *, normalized=False):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise MappingError("A point needs two coordinates")
    return [number(v, "point", 0 if normalized else -1e4, 1 if normalized else 1e4) for v in value]


def inside(value, polygon):
    try:
        return cv2.pointPolygonTest(np.asarray(polygon, np.float32), tuple(point(value)), False) >= 0
    except (ValueError, TypeError, cv2.error):
        return False


def timestamp(value):
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo else None
        if type(value) in (int, float) and math.isfinite(value):
            return float(value)
    except (ValueError, OverflowError):
        pass
    return None


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def physical_geometry(value):
    if not isinstance(value, dict) or set(value) - {"width", "depth", "height", "thickness", "origin_x", "origin_z", "rotation_y", "unit"}:
        raise MappingError("Unexpected physical geometry fields")
    if value.get("unit") not in {"relative", "m"}:
        raise MappingError("Geometry needs an explicit relative or m unit")
    result = {"unit": value["unit"]}
    for name in ("width", "depth", "thickness"):
        result[name] = number(value.get(name), name, .001, 100)
    result["height"] = number(value.get("height"), "height", 0, 100)
    for name in ("origin_x", "origin_z"):
        result[name] = number(value.get(name), name, -1000, 1000)
    result["rotation_y"] = number(value.get("rotation_y"), "rotation_y", -360, 360)
    if result["thickness"] > max(result["height"], .1) or result["thickness"] > min(result["width"], result["depth"]):
        raise MappingError("Thickness is incompatible with the confirmed geometry")
    return result


def surface_matrix(physical):
    angle = math.radians(physical["rotation_y"])
    c, s = math.cos(angle), math.sin(angle)
    # Local surface [u, height_delta, v, 1] -> world XYZ.
    return [[c, 0., s, physical["origin_x"]], [0., 1., 0., physical["height"]],
            [-s, 0., c, physical["origin_z"]], [0., 0., 0., 1.]]


def world_point(physical, uv, height=None):
    matrix = np.asarray(surface_matrix(physical), float)
    result = matrix @ np.asarray([uv[0], 0. if height is None else height-physical["height"], uv[1], 1.])
    return result[:3].tolist()


def apply_homography(matrix, value):
    transformed = np.asarray(matrix, float) @ np.asarray([*point(value), 1.])
    if not np.all(np.isfinite(transformed)) or abs(transformed[2]) < 1e-8:
        raise MappingError("Projection is singular or non-finite")
    return (transformed[:2]/transformed[2]).tolist()


def calibrate(value, physical, image_polygon, settings):
    if not isinstance(value, dict) or set(value) - {"image_points", "plane_points", "validation_points"}:
        raise MappingError("Calibration accepts only point correspondences, not client matrices/status")
    source = value.get("image_points")
    target = value.get("plane_points")
    if not isinstance(source, list) or not isinstance(target, list) or len(source) != 4 or len(target) != 4:
        raise MappingError("Exactly four ordered plane correspondences are required")
    source = [point(p, normalized=True) for p in source]
    target = [point(p) for p in target]
    for points, area in ((source, settings.min_image_area), (target, physical["width"]*physical["depth"]*.02)):
        contour = np.asarray(points, np.float32)
        if len({tuple(p) for p in points}) != 4 or not cv2.isContourConvex(contour) or cv2.contourArea(contour) < area:
            raise MappingError("Calibration points are repeated, crossing, collinear or poorly distributed")
    if any(not inside(p, image_polygon) for p in source):
        raise MappingError("Calibration points must lie inside the confirmed image surface")
    if any(not (0 <= u <= physical["width"] and 0 <= v <= physical["depth"]) for u, v in target):
        raise MappingError("Plane anchors are outside the confirmed physical surface")
    equations, values = [], []
    for (x, y), (u, v) in zip(source, target):
        equations += [[x,y,1,0,0,0,-u*x,-u*y],[0,0,0,x,y,1,-v*x,-v*y]]
        values += [u,v]
    a = np.asarray(equations, float)
    condition = float(np.linalg.cond(a))
    if not math.isfinite(condition) or condition > settings.max_condition:
        raise MappingError("Calibration is numerically unstable")
    try:
        matrix = np.append(np.linalg.solve(a, np.asarray(values)), 1.).reshape(3,3)
    except np.linalg.LinAlgError as exc:
        raise MappingError("Calibration has no valid homography") from exc
    if abs(np.linalg.det(matrix)) < 1e-10:
        raise MappingError("Calibration homography is singular")
    # Do not allow a projective horizon anywhere inside the valid convex area.
    denominators = [float(matrix[2] @ np.asarray([*p, 1.])) for p in source]
    if min(denominators)*max(denominators) <= 0 or min(abs(v) for v in denominators) < 1e-6:
        raise MappingError("Projection horizon intersects the valid surface")
    checks = value.get("validation_points", [])
    if not isinstance(checks, list) or len(checks) > 16:
        raise MappingError("At most 16 independent validation points are supported")
    errors, clean_checks = [], []
    for check in checks:
        if not isinstance(check, dict) or set(check) != {"image", "plane"}:
            raise MappingError("Validation needs separate image and plane points")
        image = point(check["image"], normalized=True)
        plane = point(check["plane"])
        if any(math.dist(image, previous["image"]) < settings.min_validation_distance for previous in clean_checks):
            raise MappingError("Independent validation points must not be duplicates")
        if min(math.dist(image, fitted) for fitted in source) < settings.min_validation_distance:
            raise MappingError("A fitting anchor cannot count as an independent validation point")
        if not inside(image, source) or not (0 <= plane[0] <= physical["width"] and 0 <= plane[1] <= physical["depth"]):
            raise MappingError("Validation point is outside the calibrated surface")
        errors.append(math.dist(apply_homography(matrix, image), plane))
        clean_checks.append({"image":image,"plane":plane})
    tolerance = math.hypot(physical["width"], physical["depth"])*settings.validation_error_fraction
    validation = {"status":"not_validated" if not errors else "passed" if max(errors) <= tolerance else "failed",
                  "independent_point_count":len(errors),"max_error":max(errors) if errors else None,
                  "tolerance":tolerance,"unit":physical["unit"],"fitting_points_are_validation":False}
    return {"image_points":source,"plane_points":target,"validation_points":clean_checks,
            "image_to_surface":matrix.tolist(),"condition_number":condition,"validation":validation,
            "calibration_status":"validation_failed" if validation["status"] == "failed" else
                "calibrated" if errors else "calibrated_unvalidated"}


def make_world_geometry(surfaces):
    objects, units = [], set()
    for surface in surfaces:
        physical = surface.get("physical")
        if not physical or surface.get("confirmed_by_user") is not True:
            continue
        units.add(physical["unit"])
        w, d, h, t = (physical[k] for k in ("width", "depth", "height", "thickness"))
        kind = surface["surface_type"]
        parts = [("surface", [w/2, h-t/2, d/2], [w,t,d])]
        if kind == "table" and h > t:
            leg = max(.001, min(w,d)*.06)
            parts += [(f"leg-{i}", [x,(h-t)/2,z],[leg,h-t,leg])
                      for i,(x,z) in enumerate(((leg/2,leg/2),(w-leg/2,leg/2),(w-leg/2,d-leg/2),(leg/2,d-leg/2)))]
        elif kind == "sofa":
            parts[0] = ("seat", [w/2,h/2,d/2],[w,max(h,t),d])
            back = max(t, h*.65)
            parts += [("back", [w/2,h+back/2,d-t/2],[w,back,t]),
                      ("arm-left", [t/2,h+t,d/2],[t,2*t,d]), ("arm-right", [w-t/2,h+t,d/2],[t,2*t,d])]
        elif kind in {"cabinet", "shelf", "bed"} and h > t:
            parts += [("base", [w/2,(h-t)/2,d/2],[w,h-t,d])]
        for part, local, size in parts:
            objects.append({"id":f"{surface['surface_id']}:{part}","surface_id":surface["surface_id"],
                "name":surface["name"],"surface_type":kind,"part":part,"shape":"box",
                "position":world_point(physical,[local[0],local[2]],local[1]),"size":size,
                "rotation_y":physical["rotation_y"],"source":"user_confirmed_geometry","estimated":True,
                "confirmed_by_user":True,"dimension_source":"user_input","structure_source":"simplified_type_template"})
    if len(units) > 1:
        raise MappingError("One scene cannot mix relative and metric coordinate units")
    return {"schema":"scene3d_v1","coordinate_system":"right_handed_y_up","unit":next(iter(units),None),
            "objects":objects,"status":"confirmed" if objects else "empty","scale_is_measured":False,
            "notice":"User-confirmed approximate geometry, not an automatic metric room scan"}


def proposal_draft(proposals):
    """Editable furniture volumes from *actual* detector proposals only.

    Bounding-box layout is a provisional relative-layout heuristic, NOT camera
    calibration or measured depth. Nothing here becomes a support surface.
    """
    surfaces = []
    for proposal in proposals:
        points = proposal["image_polygon"]
        left, right = min(p[0] for p in points), max(p[0] for p in points)
        top, bottom = min(p[1] for p in points), max(p[1] for p in points)
        width, depth = max(.15,(right-left)*4), max(.15,(bottom-top)*2)
        height = max(.08,min(width,depth)*({"table":.8,"sofa":.45,"bed":.3}.get(proposal["surface_type"],.8)))
        physical = {"width":width,"depth":depth,"height":height,"thickness":min(width,depth,height)*.15,
                    "origin_x":left*4,"origin_z":top*2,"rotation_y":0.,"unit":"relative"}
        proposal["physical_suggestion"] = physical
        proposal["physical_suggestion_status"] = "estimated_not_measured_not_calibrated"
        surfaces.append({"surface_id":"proposal:"+proposal["proposal_id"],"name":proposal["name"],
            "surface_type":proposal["surface_type"],"physical":physical,"confirmed_by_user":True})
    geometry = make_world_geometry(surfaces)
    geometry.update(status="draft" if surfaces else "empty",eligible_for_mapping=False,
        layout_method="image_bbox_relative_layout_heuristic_not_world_measurement",
        notice="家具候选相对空间草稿：高度、深度和布局均为估计，需用户核对，不能用于位置投影。")
    for obj in geometry["objects"]:
        obj.update(source="model_proposal_estimate",confirmed_by_user=False,
                   dimension_source="bbox_and_type_heuristic",eligible_for_mapping=False)
    return geometry


def region(surface):
    p = surface.get("physical")
    return {"surface_id":surface["surface_id"],"name":surface["name"],"approximate":True,
            "image_polygon":surface["image_polygon"],"world_polygon":[world_point(p,q) for q in
            ((0,0),(p["width"],0),(p["width"],p["depth"]),(0,p["depth"]))] if p else None}


def project(surface, image):
    if surface.get("calibration_status") not in {"calibrated", "calibrated_unvalidated"}:
        return None
    if not inside(image, surface["image_polygon"]) or not inside(image, surface.get("calibration",{}).get("image_points",[])):
        return None
    p = surface["physical"]
    uv = apply_homography(surface["image_to_surface"], image)
    if not (0 <= uv[0] <= p["width"] and 0 <= uv[1] <= p["depth"]):
        return None
    return world_point(p, uv)


class SceneProjector:
    """Bounded transient projection checks. Never owns a DB or a camera."""
    def __init__(self, settings=None):
        self.settings = settings if isinstance(settings, MappingSettings) else MappingSettings(**(settings or {}))
        self._positions = {}
        self._persons = {}

    def reset(self, camera_id):
        self._positions = {k:v for k,v in self._positions.items() if k[0] != camera_id}
        self._persons.pop(camera_id, None)

    @staticmethod
    def _base(scene, observed, identity):
        stamp = timestamp(observed.get("observed_at") or observed.get("source_timestamp"))
        return {**identity,"camera_id":observed.get("camera_id"),"scene_version":observed.get("scene_version"),
            "frame_id":observed.get("source_frame"),"source_session_id":observed.get("source_session_id"),
            "observation_timestamp":iso(stamp) if stamp is not None else None,
            "runtime_mode":observed.get("runtime_mode"),"source_type":observed.get("source_type"),
            "is_simulated":observed.get("is_simulated"),"image_position":None,"image_bbox":None,
            "map_position":None,"region":None,"position_status":"unknown","mapping_method":"none",
            "reason":"mapping_not_available","unit":(scene.get("world_geometry") or {}).get("unit"),
            "evidence_reference":None}

    @staticmethod
    def _bound(scene, observed):
        return (scene.get("invalid_reason") is None and scene.get("calibration_status") not in
                {"needs_review","needs_confirmation","pending_delete"}
            and observed.get("camera_id") == scene.get("camera_id")
            and type(observed.get("scene_version")) is int and observed.get("scene_version") == scene.get("scene_version")
            and observed.get("source_session_id") == scene.get("source_session_id")
            and observed.get("runtime_mode") == scene.get("runtime_mode")
            and observed.get("source_type") == scene.get("source_type")
            and observed.get("is_simulated") is scene.get("is_simulated")
            and type(observed.get("source_frame")) is int and observed["source_frame"] >= scene["source_frame"]
            and timestamp(observed.get("observed_at") or observed.get("source_timestamp")) is not None
            and timestamp(observed.get("observed_at") or observed.get("source_timestamp")) >= timestamp(scene["source_timestamp"]))

    def _jump(self, scene, marker, surface):
        key = (scene["camera_id"],"item:"+marker["item_id"] if marker.get("item_id") else "person:"+marker["person_track_id"])
        stamp = timestamp(marker["observation_timestamp"])
        binding = (scene["scene_version"],marker["source_session_id"])
        previous = self._positions.get(key)
        if previous and previous["binding"] == binding:
            elapsed = stamp-previous["time"]
            distance = math.dist(previous["position"],marker["map_position"])
            scale = math.hypot(surface["physical"]["width"],surface["physical"]["depth"])
            if elapsed < 0 or (elapsed < self.settings.jump_reset_seconds and distance > scale*.01
                    and (elapsed <= 0 or distance/elapsed > scale*self.settings.max_jump_fraction_per_second)):
                marker.update(map_position=None,position_status="region_only",mapping_method="region",reason="implausible_projection_jump")
                return
        self._positions[key] = {"binding":binding,"time":stamp,"position":marker["map_position"]}
        while len(self._positions) > self.settings.max_item_history:
            self._positions.pop(next(iter(self._positions)))

    def _locate(self, scene, marker, surfaces, image, allow_point):
        candidates = [surface for surface in surfaces if surface.get("confirmed_by_user") is True
                      and inside(image, surface["image_polygon"])]
        if len(candidates) != 1:
            marker["reason"] = "ambiguous_surfaces" if candidates else "outside_confirmed_surfaces"
            return marker
        surface = candidates[0]
        marker.update(region=region(surface),position_status="region_only",mapping_method="region",reason="support_contact_unconfirmed")
        if allow_point:
            try:
                mapped = project(surface,image)
            except (MappingError, KeyError, TypeError, ValueError, np.linalg.LinAlgError):
                mapped = None
            if mapped is not None:
                marker.update(map_position=mapped,mapping_method="image_to_surface_to_world",
                    position_status="mapped" if surface["calibration_status"] == "calibrated" else "mapped_unvalidated",
                    reason=None if surface["calibration_status"] == "calibrated" else "independent_accuracy_not_validated")
                self._jump(scene,marker,surface)
            else:
                marker["reason"] = "calibration_missing_invalid_or_outside_valid_range"
        return marker

    def item_markers(self, scene, states):
        markers = []
        for state in states[:self.settings.max_item_history]:
            observed = state.get("last_observed") or {}
            if not observed or observed.get("camera_id") != scene["camera_id"]:
                continue
            marker = self._base(scene,observed,{"item_id":state.get("item_id")})
            marker["observation_status"] = observed.get("status") or state.get("status")
            marker["evidence_reference"] = {"screenshot_path":observed.get("screenshot_path"),
                "sha256":observed.get("screenshot_sha256"),"frame_id":observed.get("screenshot_source_frame"),
                "source_session_id":observed.get("screenshot_source_session_id"),
                "observation_timestamp":observed.get("screenshot_observed_at")}
            try:
                marker["image_position"] = point(observed.get("position"),normalized=True)
                bbox = observed.get("bbox")
                if isinstance(bbox,(list,tuple)) and len(bbox) == 4:
                    w,h = scene["geometry"]["source_width"],scene["geometry"]["source_height"]
                    marker["image_bbox"] = [number(v,"bbox",0,1) for v in (bbox[0]/w,bbox[1]/h,bbox[2]/w,bbox[3]/h)]
                    bx,by,bw,bh = marker["image_bbox"]
                    if min(bw,bh) <= 0 or bx+bw > 1.000001 or by+bh > 1.000001:
                        raise MappingError("Object bbox exceeds the source frame")
            except (MappingError,TypeError,KeyError,ZeroDivisionError):
                marker["reason"] = "invalid_image_coordinates"
                markers.append(marker)
                continue
            if observed.get("evidence_type") != "observed" or observed.get("item_id") != state.get("item_id") or (observed.get("identity_evidence") or {}).get("accepted") is not True:
                marker["reason"] = "identity_or_observation_unverified"
            elif not self._bound(scene,observed):
                marker["reason"] = "scene_version_or_source_mismatch"
            elif observed.get("holding_status") in {"held","holding","holding_uncertain","possibly_held","carried","co_moving"}:
                marker.update(position_status="held",reason="handheld_object_is_not_on_support_plane")
            else:
                surfaces = scene.get("surfaces") or []
                surface_id = observed.get("support_surface_id") or observed.get("zone_id")
                if surface_id:
                    surfaces = [s for s in surfaces if s["surface_id"] == surface_id]
                # A confirmed surface region alone is not object-plane contact.
                supported = (observed.get("support_surface_confirmed") is True and
                             observed.get("support_contact_confirmed") is True)
                self._locate(scene,marker,surfaces,marker["image_position"],supported)
            markers.append(marker)
        return markers

    def person_markers(self, scene, persons=None, *, now_timestamp=None):
        current = timestamp(now_timestamp) if now_timestamp is not None else datetime.now(timezone.utc).timestamp()
        if current is None:
            raise MappingError("Invalid current time")
        camera_id = scene["camera_id"]
        binding = (scene["scene_version"],scene["source_session_id"])
        cache = self._persons.get(camera_id)
        if not cache or cache["binding"] != binding:
            cache = self._persons[camera_id] = {"binding":binding,"markers":{}}
        while len(self._persons) > 32:
            self._persons.pop(next(iter(self._persons)))
        if persons is not None:
            if not isinstance(persons,list) or len(persons) > self.settings.max_persons:
                raise MappingError("Too many transient person detections")
            for observed in persons:
                track_id = observed.get("person_track_id")
                stamp = timestamp(observed.get("source_timestamp"))
                if not isinstance(track_id,str) or not 1 <= len(track_id) <= 128 or stamp is None or not 0 <= current-stamp < self.settings.person_expire_seconds:
                    continue
                if not self._bound(scene,observed):
                    continue
                old = cache["markers"].get(track_id)
                if old and timestamp(old["observation_timestamp"]) >= stamp:
                    continue
                marker = self._base(scene,observed,{"person_track_id":track_id})
                try:
                    box = observed["bbox"]
                    if not isinstance(box,(list,tuple)) or len(box) != 4:
                        raise MappingError("Person bbox must be normalized XYWH")
                    x,y,w,h = [number(v,"person bbox",0,1) for v in box]
                    if min(w,h) <= 0 or x+w > 1.000001 or y+h > 1.000001:
                        raise MappingError("Invalid person bbox")
                    marker["image_bbox"] = [x,y,w,h]
                    foot = point(observed.get("foot_point"),normalized=True) if observed.get("foot_point") is not None else None
                    score = observed.get("foot_confidence")
                    reliable = (observed.get("feet_visible") is True and type(score) in (int,float)
                        and math.isfinite(score) and self.settings.foot_confidence <= score <= 1 and foot is not None
                        and x <= foot[0] <= x+w and y+.6*h <= foot[1] <= y+h and observed.get("posture") != "sitting")
                    marker["image_position"] = foot if reliable else [x+w/2,y+h/2]
                    floors = [s for s in scene.get("surfaces") or [] if s["surface_type"] == "floor"]
                    if reliable:
                        self._locate(scene,marker,floors,foot,True)
                    else:
                        # The body box bottom is NOT interpreted as a foot.
                        overlaps = [s for s in scene.get("surfaces") or [] if s.get("confirmed_by_user") is True
                                    and inside(marker["image_position"],s["image_polygon"])]
                        if len(overlaps) == 1:
                            marker.update(region=region(overlaps[0]),position_status="region_only",mapping_method="body_region_overlap")
                        marker["reason"] = "feet_not_reliably_visible_ground_unconfirmed"
                except (MappingError,KeyError,TypeError):
                    marker["reason"] = "invalid_person_geometry"
                cache["markers"][track_id] = marker
        result = []
        for track_id, marker in list(cache["markers"].items()):
            age = current-timestamp(marker["observation_timestamp"])
            if age < 0 or age >= self.settings.person_expire_seconds:
                del cache["markers"][track_id]
                continue
            value = dict(marker,age_seconds=age,freshness="live" if age <= self.settings.person_live_seconds else "stale",
                         expires_at=iso(timestamp(marker["observation_timestamp"])+self.settings.person_expire_seconds))
            if value["freshness"] == "stale":
                value["position_status"] = "stale"
            result.append(value)
        while len(cache["markers"]) > self.settings.max_persons:
            cache["markers"].pop(next(iter(cache["markers"])))
        return result[-self.settings.max_persons:]
