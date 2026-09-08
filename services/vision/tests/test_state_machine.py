from services.vision.events import ItemMotionStateMachine, ItemState, MotionObservation


def observation(
    x: float,
    *,
    frame: int,
    timestamp: float,
    session: str = "session-a",
    zone_id: str = "desk",
    zone_name: str = "桌面",
    speed: float = 0.0,
    velocity: tuple[float, float] = (0.0, 0.0),
    confidence: float = 0.97,
    hand: bool = False,
) -> MotionObservation:
    return MotionObservation(
        (x, 0.5),
        speed,
        zone_id,
        zone_name,
        confidence,
        velocity,
        hand,
        "aruco",
        frame,
        1_700_000_000.0 + timestamp,
        session,
        0,
    )


def machine(**overrides) -> ItemMotionStateMachine:
    settings = {
        "min_detection_frames": 3,
        "min_stable_frames": 3,
        "min_stable_seconds": 0.2,
        "max_frame_gap_seconds": 2.0,
        "min_move_distance": 0.04,
        "same_zone_move_distance": 0.10,
        "min_confidence": 0.7,
        "stable_speed": 0.02,
        "stable_position_jitter": 0.015,
        "occluded_seconds": 0.2,
    }
    settings.update(overrides)
    return ItemMotionStateMachine(**settings)


def feed(target: ItemMotionStateMachine, value: MotionObservation, timestamp: float):
    return target.update(value, timestamp)


def establish_baseline(target: ItemMotionStateMachine, *, session: str = "session-a", start_frame: int = 1):
    events = []
    for offset, timestamp in enumerate((0.0, 0.1, 0.2, 0.3)):
        events += feed(target, observation(0.2, frame=start_frame + offset, timestamp=timestamp, session=session), timestamp)
    assert target.baseline is not None
    assert events == []
    return start_frame + 4


def test_stationary_for_five_simulated_minutes_emits_no_history_event():
    target = machine(min_stable_seconds=1.0, max_frame_gap_seconds=1.5)
    events = []
    for frame in range(301):
        timestamp = float(frame)
        events += feed(target, observation(0.2, frame=frame + 1, timestamp=timestamp), timestamp)
    assert events == []
    assert target.state in {ItemState.VISIBLE_STATIC, ItemState.HAND_NEAR}
    assert target.diagnostics()["candidate"] is None


def test_small_same_zone_adjustment_updates_baseline_but_emits_no_event():
    target = machine()
    frame = establish_baseline(target)
    events = []
    for x in (0.22, 0.24, 0.24, 0.24, 0.24):
        timestamp = frame * 0.1
        events += feed(target, observation(x, frame=frame, timestamp=timestamp), timestamp)
        frame += 1
    assert events == []
    assert target.baseline is not None
    assert target.baseline.position[0] == 0.24


def test_cross_zone_move_emits_exactly_one_complete_movement_episode():
    target = machine()
    frame = establish_baseline(target)
    events = []
    for x in (0.30, 0.48, 0.70):
        timestamp = frame * 0.1
        events += feed(
            target,
            observation(x, frame=frame, timestamp=timestamp, speed=0.4, velocity=(0.4, 0.0), hand=True),
            timestamp,
        )
        frame += 1
    for _ in range(4):
        timestamp = frame * 0.1
        events += feed(
            target,
            observation(x, frame=frame, timestamp=timestamp, zone_id="sofa", zone_name="沙发右侧"),
            timestamp,
        )
        frame += 1

    assert [event.event_type for event in events] == ["movement"]
    event = events[0]
    assert event.previous_zone_name == "桌面"
    assert event.new_zone_name == "沙发右侧"
    assert event.source_session_id == "session-a"
    assert event.source_frame_start < event.source_frame_end
    assert event.source_timestamp_start < event.source_timestamp_end
    assert event.from_position == (0.2, 0.5)
    assert event.to_position == (0.7, 0.5)
    assert event.pickup_evidence["zone_id"] == "desk"
    assert event.placement_evidence["stable_frames"] >= 3
    assert event.evidence_status == "confirmed"
    assert target.state == ItemState.PLACED_CONFIRMED


def test_disconnect_or_reconnect_resets_baseline_and_cannot_confirm_movement():
    target = machine()
    frame = establish_baseline(target)
    assert feed(target, observation(0.45, frame=frame, timestamp=.4, speed=.4), .4) == []
    events = []
    for index, timestamp in enumerate((0.5, 0.6, 0.7, 0.8), start=1):
        events += target.update(
            observation(0.7, frame=index, timestamp=timestamp, session="session-b", zone_id="sofa", zone_name="沙发右侧"),
            timestamp,
            reconnect_epoch=1,
        )
    assert events == []
    assert target.baseline is not None
    assert target.baseline.zone_id == "sofa"
    assert target.diagnostics()["candidate"] is None


def test_long_frame_gap_resets_baseline_instead_of_creating_placed_event():
    target = machine(max_frame_gap_seconds=0.5)
    frame = establish_baseline(target)
    target.update(observation(0.5, frame=frame, timestamp=.4, speed=.4), .4)
    events = []
    for offset in range(4):
        timestamp = 2.0 + offset * .1
        events += target.update(
            observation(0.7, frame=frame + 1 + offset, timestamp=timestamp, zone_id="sofa", zone_name="沙发右侧"),
            timestamp,
        )
    assert events == []
    assert target.baseline is not None and target.baseline.zone_id == "sofa"
    assert target.rejection_counts["frame_gap"] == 1


def test_occlusion_without_reappearance_never_confirms_placement():
    target = machine()
    frame = establish_baseline(target)
    target.update(observation(0.5, frame=frame, timestamp=.4, speed=.4), .4)
    events = []
    events += target.update(None, .5, source_session_id="session-a", frame_sequence=frame + 1)
    events += target.update(None, .8, source_session_id="session-a", frame_sequence=frame + 2)
    assert events == []
    assert target.state == ItemState.OCCLUDED
    assert target.diagnostics()["candidate"] is None


def test_duplicate_final_frame_is_idempotent_within_episode():
    target = machine()
    frame = establish_baseline(target)
    target.update(observation(.5, frame=frame, timestamp=.4, speed=.4), .4)
    frame += 1
    target.update(observation(.7, frame=frame, timestamp=.5, speed=.4, zone_id="sofa", zone_name="沙发右侧"), .5)
    emitted = []
    for timestamp in (.6, .7, .8, .9):
        frame += 1
        emitted += target.update(observation(.7, frame=frame, timestamp=timestamp, zone_id="sofa", zone_name="沙发右侧"), timestamp)
    assert len(emitted) == 1
    duplicate = target.update(
        observation(.7, frame=frame, timestamp=.9, zone_id="sofa", zone_name="沙发右侧"),
        .9,
    )
    assert duplicate == []
    assert target.diagnostics()["last_rejection"] == "duplicate_or_out_of_order_frame"


def test_same_zone_move_must_reach_the_larger_configured_threshold():
    target = machine(same_zone_move_distance=.12)
    frame = establish_baseline(target)
    target.update(observation(.35, frame=frame, timestamp=.4, speed=.4), .4)
    events = []
    for timestamp in (.5, .6, .7, .8):
        frame += 1
        events += target.update(observation(.35, frame=frame, timestamp=timestamp), timestamp)
    assert [event.event_type for event in events] == ["movement"]


def test_low_confidence_frames_cannot_establish_or_complete_evidence():
    target = machine(min_confidence=.8)
    events = []
    for frame in range(1, 8):
        timestamp = frame * .1
        events += target.update(observation(.2, frame=frame, timestamp=timestamp, confidence=.4), timestamp)
    assert events == []
    assert target.baseline is None
    assert target.state == ItemState.OCCLUDED


def test_isolated_and_interrupted_detections_never_receive_observation_proof():
    target = machine()
    for frame in range(1, 41):
        timestamp = frame * .1
        value = observation(.2, frame=frame, timestamp=timestamp) if frame % 2 else None
        assert target.update(value, timestamp, frame_sequence=frame, source_session_id="session-a") == []
        assert target.last_verified_observation is None
        assert target.observation_verified is False
        assert target.baseline is None


def test_continuous_missing_frames_keep_verified_position_and_reach_lost():
    target = machine(max_frame_gap_seconds=.75, occluded_seconds=.6, lost_seconds=8)
    frame = establish_baseline(target)
    last_verified = target.last_verified_observation
    assert last_verified is not None and target.observation_verified
    # Advancing source frames do not reset the last detection's loss clock.
    for offset in range(1, 82):
        timestamp = .3 + offset * .1
        assert target.update(None, timestamp, source_session_id="session-a", frame_sequence=frame + offset) == []
        assert target.last_verified_observation is last_verified
        assert target.last_seen_at == .3
        assert target.observation_verified is False
        if offset == 6:
            assert target.state == ItemState.OCCLUDED
    assert target.state == ItemState.LOST
    assert target.rejection_counts.get("frame_gap", 0) == 0


def test_reappearance_candidate_cannot_replace_last_verified_observation():
    target = machine()
    frame = establish_baseline(target)
    previous = target.last_verified_observation
    target.update(None, .4, source_session_id="session-a", frame_sequence=frame)
    target.update(observation(.8, frame=frame + 1, timestamp=.5), .5)
    assert target.last_observation.center_norm == (.8, .5)
    assert target.last_verified_observation is previous
    assert target.observation_verified is False
    assert target.baseline is None


def test_source_frame_gap_or_new_session_revokes_previous_observation_proof():
    for source_session, timestamp in (("session-b", .4), ("session-a", 3.0)):
        target = machine(max_frame_gap_seconds=.75)
        frame = establish_baseline(target)
        assert target.last_verified_observation is not None
        target.update(observation(.8, frame=frame, timestamp=timestamp, session=source_session), timestamp)
        assert target.last_verified_observation is None
        assert target.observation_verified is False
        assert target.baseline is None


def test_held_stationary_at_destination_is_not_placed():
    target = machine()
    frame = establish_baseline(target)
    events = []
    for index, x in enumerate((.35, .55, .7, *([.7] * 40)), frame):
        timestamp = index * .1
        events += target.update(observation(x, frame=index, timestamp=timestamp,
                                            zone_id='sofa', hand=True), timestamp)
    assert events == []
    assert target.state == ItemState.CARRIED
    assert target.observation_verified  # still genuinely visible, even held


def test_photo_motion_without_support_is_not_confirmed_placement():
    target = machine()
    establish_baseline(target)
    events = []
    for frame, x in enumerate((.35, .55, .7, .7, .7, .7, .7), 5):
        value = observation(x, frame=frame, timestamp=frame*.1, zone_id='sofa')
        value.detection_mode = 'experimental'
        events += target.update(value, frame*.1)
    assert events == []
    assert target.observation_verified
    assert target.last_rejection == 'waiting_for_verified_release'
    assert target.state != ItemState.PLACED_CONFIRMED


def test_healthy_hand_model_and_surface_without_release_are_not_placement():
    target=machine()
    establish_baseline(target)
    events=[]
    for frame,x in enumerate((.35,.55,.7,.7,.7,.7,.7),5):
        value=observation(x,frame=frame,timestamp=frame*.1,zone_id='sofa')
        value.detection_mode='experimental'
        value.hand_model_healthy=value.support_surface_confirmed=True
        events+=target.update(value,frame*.1)
    assert events == []
    assert target.state != ItemState.PLACED_CONFIRMED
    assert target.observation_verified
    assert target.last_rejection == 'waiting_for_verified_release'


def test_lost_hand_after_carried_must_not_become_placed_even_if_item_static():
    target=machine()
    establish_baseline(target)
    events=[]
    for frame,x in enumerate((.35,.55,.7,*([.7]*30)),5):
        value=observation(x,frame=frame,timestamp=frame*.1,zone_id='sofa')
        value.detection_mode='experimental'
        value.hand_model_healthy=value.support_surface_confirmed=True
        value.hand_interaction={'holding_status':'co_moving' if frame<8 else 'holding_uncertain'}
        events+=target.update(value,frame*.1)
    assert events==[]
    assert target.state==ItemState.CARRIED
