"""Real TEST API/settings SQLite; controlled lifecycle stub never opens hardware."""
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from apps.api.app.main import create_app
from apps.api.app.schemas import SettingsInput
from services.vision.person_tracking import person_configuration


def test_implicit_and_explicit_person_defaults_have_one_canonical_form():
    implicit = SettingsInput().model_dump()
    assert implicit['person_pose'] == person_configuration()
    assert SettingsInput(person_pose={}).model_dump() == implicit
    assert SettingsInput.model_validate(implicit).model_dump() == implicit


@pytest.mark.parametrize('saved', [None,{}, {'max_people':1}, {'tracking':{'max_gap_seconds':.5}}])
def test_noop_and_overlay_saves_preserve_person_baseline_without_restart(tmp_path,saved):
    app = create_app(tmp_path/'person-settings',testing=True)
    runtime = app.state.runtime
    with TestClient(app) as client:
        client.get('/api/session')
        if saved is not None:
            runtime.db.save('settings',{'value':{'person_pose':saved}},'main')
        calls = []
        runtime.restart = lambda camera_id:calls.append(camera_id)
        engine = SimpleNamespace(settings={'show_hands':True},stop=lambda:None)
        runtime.engines['explicit-lifecycle-stub'] = engine
        try:
            received = client.get('/api/settings').json()
            writable = {key:value for key,value in received.items() if key in SettingsInput.model_fields}
            response = client.patch('/api/settings',json=writable)
            assert response.status_code == 200, response.text
            assert response.json()['person_pose'] == person_configuration(saved)
            assert calls == []
            assert client.patch('/api/settings',json={'show_hands':False}).status_code == 200
            assert engine.settings['show_hands'] is False and calls == []
            assert runtime.db.count('movement_events') == runtime.db.count('item_current_state') == 0
        finally:
            runtime.engines.clear()


def test_real_person_config_change_still_restarts_once(tmp_path):
    app = create_app(tmp_path/'person-settings-change',testing=True)
    runtime = app.state.runtime
    with TestClient(app) as client:
        client.get('/api/session')
        calls = []
        runtime.restart = lambda camera_id:calls.append(camera_id)
        runtime.engines['explicit-lifecycle-stub'] = SimpleNamespace(settings={},stop=lambda:None)
        try:
            response = client.patch('/api/settings',json={'person_pose':{'max_people':1}})
            assert response.status_code == 200 and calls == ['explicit-lifecycle-stub']
            assert response.json()['person_pose']['max_people'] == 1
            assert client.patch('/api/settings',json={'person_pose':{'max_people':1}}).status_code == 200
            assert calls == ['explicit-lifecycle-stub']
        finally:
            runtime.engines.clear()
