"""Real in-process API/SQLite contracts; engine lifecycle is an explicit stub."""
from types import SimpleNamespace
from fastapi.testclient import TestClient
from apps.api.app.main import create_app


def test_action_threshold_defaults_match_validated_settings():
    from apps.api.app.schemas import SettingsInput
    from services.vision.hand_actions import DEFAULT_SETTINGS
    from services.vision.hand_interaction import DEFAULTS
    values=SettingsInput().model_dump()
    assert {key:values[key] for key in DEFAULT_SETTINGS}==DEFAULT_SETTINGS
    assert {key:values[key] for key in DEFAULTS}==DEFAULTS


def test_legacy_hand_proximity_is_not_promoted_to_carried():
    from apps.api.app.search import location_result
    original={'status':'last_seen','last_seen_at':'2026-09-05T12:00:00Z',
              'last_observed':{'holding_status':'possibly_held','interaction':{'hand_near':True}}}
    result=location_result({'id':'old-item','name':'旧物品'},[],current_state=original)
    assert result['last_observed']['holding_status']=='nearby'
    assert result['current_state']['last_observed']['interaction']['reason']=='legacy_proximity_only'
    assert original['last_observed']['holding_status']=='possibly_held'  # no destructive migration


def test_hand_display_setting_is_not_detection_or_source_restart(tmp_path):
    app=create_app(tmp_path,testing=True)
    with TestClient(app) as client:
        client.get('/api/session')
        runtime=app.state.runtime
        restarted=[]
        runtime.restart=lambda camera_id: restarted.append(camera_id)
        engine=SimpleNamespace(settings={'show_hands':True},stop=lambda:None)
        runtime.engines['contract-stub']=engine
        try:
            response=client.patch('/api/settings',json={'show_hands':False})
            assert response.status_code==200
            assert response.json()['hand_detection_enabled'] is True
            assert engine.settings['show_hands'] is False
            assert restarted==[]
            assert client.patch('/api/settings',json=runtime.settings()).status_code==200
            assert restarted==[]
            assert client.patch('/api/settings',json={'hand_detection_enabled':False}).status_code==200
            assert restarted==['contract-stub']
            assert runtime.db.get('settings','main')['value']['hand_detection_enabled'] is False
            assert runtime.db.count('movement_events')==0
        finally:
            runtime.engines.clear()


def test_offline_actions_are_empty_and_do_not_start_camera(tmp_path):
    app=create_app(tmp_path,testing=True)
    with TestClient(app) as client:
        client.get('/api/session')
        camera=client.post('/api/cameras',json={'name':'Never opened','source_type':'webcam','source':'0'}).json()
        runtime=app.state.runtime
        for _ in range(3):
            response=client.get(f"/api/cameras/{camera['id']}/actions")
            assert response.status_code==200
            assert response.headers['cache-control']=='no-store'
            result=response.json()
            assert result['fresh'] is False and result['reason']=='camera_not_running'
            assert result['hands']==result['interactions']==[]
            assert result['runtime_mode']=='TEST' and result['is_simulated'] is True
            assert result['source_frame'] is None
        assert runtime.engines=={}
        for table in ('movement_events','item_current_state','event_media','source_sessions'):
            assert runtime.db.count(table)==0


def test_action_snapshot_from_replaced_engine_is_discarded(tmp_path):
    app=create_app(tmp_path,testing=True)
    with TestClient(app) as client:
        client.get('/api/session')
        camera=client.post('/api/cameras',json={'name':'Never opened','source_type':'webcam','source':'0'}).json()
        runtime=app.state.runtime
        def replaced():
            runtime.engines.pop(camera['id'])
            return {'fresh':True,'hands':[{'hand_id':'stale-stub'}],'interactions':[]}
        runtime.engines[camera['id']]=SimpleNamespace(action_snapshot=replaced)
        result=client.get(f"/api/cameras/{camera['id']}/actions").json()
        assert result['fresh'] is False and result['reason']=='camera_changed'
        assert result['hands']==[] and result['source_frame'] is None
