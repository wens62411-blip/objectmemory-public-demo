"""Current photo route -> isolated SQLite/media, never physical action proof.

The encoder, category detector, local-reference proposal, identity matcher,
tracker, observation gate, Runtime callback and storage are production code.
Input images are explicitly SYNTHETIC translations of one pinned historical
camera image, with controlled timestamps. They are not a filmed sequence and
must not be reported as real tracking, camera FPS or pickup/placement evidence.
"""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import time

import cv2
import numpy as np
import pytest

from apps.api.app.main import Runtime
from apps.api.app.runtime_mode import RuntimeMode, runtime_layout
from services.vision.capture import FramePacket
from services.vision.detectors.appearance import AppearanceEncoder, DEFAULT_MODEL_PATH
from services.vision.engine import VisionEngine


ROOT = Path(__file__).resolve().parents[3]
CASE = ROOT / "data/verification/phone-complete-20260906/physical-phone-case.json"


@pytest.mark.skipif(not CASE.is_file() or not DEFAULT_MODEL_PATH.is_file(),
                   reason="Pinned local registration/query/model required; no synthetic model substitute")
def test_actual_reference_patch_identity_survives_runtime_proof_and_test_storage(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("reference_patch_readonly_registration", ROOT / "scripts/verify-physical-phone-frame.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    case = json.loads(CASE.read_text(encoding="utf-8"))
    target_id = case["target_item_id"]
    items, current_settings, protected = verifier.read_registration(ROOT, target_id)
    source_path, source = verifier.load_frame(ROOT, case["positive"])
    verifier.protect_detection_inputs([source_path], protected)
    runtime = Runtime(runtime_layout(ROOT, tmp_path / "isolated", RuntimeMode.TEST), testing=True)
    references = tmp_path / "isolated" / "registered-items"
    references.mkdir(parents=True, exist_ok=True)
    for original in protected:
        if original.is_relative_to(ROOT / "data/registered-items"):
            shutil.copy2(original, references / original.name)
    for item in items:
        runtime.db.save("items", {key: value for key, value in item.items()
                                  if key not in {"reference_images", "appearance_profile"}}, item["id"])
        for reference in item.get("reference_images", []):
            runtime.db.save("item_reference_images", reference, reference["id"])
        if item.get("appearance_profile"):
            runtime.db.save("item_recognition_profiles", item["appearance_profile"], item["id"])
    camera = runtime.db.save("cameras", {"name": "SYNTHETIC translated historical still / no capture",
        "source_type": "video", "source": str(tmp_path / "never-opened.avi"),
        "room_name": "controlled TEST", "config": {}, "enabled": False, "inference_fps": 5,
        "save_clips": False}, "reference-route-test-camera")
    encoder = AppearanceEncoder()
    engine = None
    callbacks = []
    pixel_hashes = []
    try:
        assert encoder.health()["available"], encoder.health()
        def forbidden_capture(*_args, **_kwargs):
            pytest.fail("The controlled route test must never open a camera or video capture")
        monkeypatch.setattr(cv2, "VideoCapture", forbidden_capture)
        engine = VisionEngine(camera, items, [], {
            **runtime.settings(), **current_settings, "runtime_mode": "TEST",
            "media_root": str(runtime.media), "reference_root": str(references),
            "hand_detection_enabled": False, "show_hands": False, "person_detection_enabled": False,
            "save_clips": False, "observation_min_frames": 3,
        }, lambda *_args: pytest.fail("Synthetic still translations cannot establish physical placement"),
            appearance_encoder=encoder)
        assert engine.reference_patches.health()["available"], engine.reference_patches.health()
        runtime.engines[camera["id"]] = engine
        def receive(payload):
            callbacks.append(deepcopy(payload))
            runtime.track(payload, expected_engine=engine)
        engine.on_track = receive
        monotonic_start, wall_start = time.monotonic(), time.time()
        height, width = source.shape[:2]
        for sequence, shift in enumerate((0, 2, 4), 1):
            pixels = cv2.warpAffine(source, np.float32([[1, 0, shift], [0, 1, 0]]),
                                    (width, height), borderMode=cv2.BORDER_CONSTANT)
            pixel_hashes.append(hashlib.sha256(pixels.tobytes()).hexdigest())
            packet = FramePacket(pixels, monotonic_start + sequence * .5,
                wall_start + sequence * .5, sequence, "SYNTHETIC-translated-still-route", 0)
            engine._process(packet)
            accepted = [row for row in engine._recognition_candidates
                        if row.get("accepted") and row.get("best_item_id") == target_id]
            assert len(accepted) == 1, engine._recognition_candidates
            assert accepted[0]["category_evidence"] == "registered_reference_patch"
            assert accepted[0]["proposal_backend"] == "dinov2_reference_patches"
            assert engine.capture.thread is None
            if sequence < 3:
                assert runtime.db.count("item_current_state") == runtime.db.count("event_media") == 0
        assert len(set(pixel_hashes)) == 3
        state = runtime.db.get("item_current_state", f"TEST:{target_id}")
        assert state is not None, {"callbacks": callbacks, "health": engine.health()}
        assert state["status"] == "last_seen" and state["runtime_mode"] == "TEST" and state["is_simulated"] is True
        assert runtime.db.count("movement_events") == runtime.db.count("events") == 0
        assert runtime.db.count("event_media") == 1
        identity = callbacks[-1]["identity_evidence"]
        profile = runtime.db.get("item_recognition_profiles", target_id)
        proof = engine.observation_gate.verified[target_id]
        assert identity["accepted"] and proof["identity"]["accepted"]
        assert identity["profile_version"] == proof["identity"]["profile_version"] == profile["profile_version"]
        assert identity["model_version"] == proof["identity"]["model_version"] == profile["model_version"]
        observed = state["last_observed"]
        assert observed["source_frame"] == observed["screenshot_source_frame"] == 3
        assert observed["source_session_id"] == "SYNTHETIC-translated-still-route"
        image = runtime.event_service.media_path(observed["screenshot_path"])
        content = image.read_bytes()
        assert hashlib.sha256(content).hexdigest() == observed["screenshot_sha256"]
        assert content == engine.recognition_snapshot()["jpeg"]
        assert cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR) is not None
        assert not list((runtime.media / "event-clips").glob("*"))
    finally:
        runtime.engines.clear()
        if engine is not None:
            engine.stop()
        encoder.close()
        assert all(verifier.file_hash(path) == digest for path, digest in protected.items())
        # These are owned TEST copies, not the user's registration originals.
        temporary_data = tmp_path / "isolated"
        assert temporary_data.resolve().is_relative_to(tmp_path.resolve())
        shutil.rmtree(temporary_data)
