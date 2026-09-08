"""Explicit recovery with real isolated SQLite/bundle checks and fake USB only."""
from __future__ import annotations

import json
from pathlib import Path
import threading

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from apps.api.app.firmware import FirmwareService, AutoUsbRecoveryRequest
from apps.api.app.firmware_usb import AutoUsbService
from apps.api.app.main import create_app
from apps.api.tests.test_firmware_usb_xiao import fixture_bundle, helper, MODEL, MAC
from apps.api.tests.test_firmware_xiao_api import BRIDGE, PORT


@pytest.fixture
def fw(tmp_path, monkeypatch):
    service = FirmwareService(tmp_path / 'isolated.sqlite', None, tmp_path / 'data', runtime_mode='REAL')
    service.project_root = tmp_path / 'repo'
    fixture_bundle(service.project_root)
    contract = helper()
    digest = contract.bundle(service.project_root, board_model=MODEL)[0]
    import apps.api.app.firmware_usb as module
    monkeypatch.setattr(module, 'usb_helper', lambda root: contract)
    monkeypatch.setattr(service, 'discover_ports', lambda: [dict(PORT)])
    monkeypatch.setattr(service, '_verify_backend_listener', lambda url: url)
    monkeypatch.setattr(service.auto_usb, '_start_listener', lambda: None)
    monkeypatch.setattr(service.auto_usb, '_tool', lambda payload: pytest.fail('Unexpected physical tool invocation'))
    binding = {'enabled': True, 'board_model': MODEL, 'mac_address': MAC, 'bridge': BRIDGE,
        'manifest_sha256': digest, 'firmware_version': 'contract-xiao', 'authorized_at': 'original-contract-authorization',
        'backend_url': 'http://192.168.1.20:8018', 'device_id': 'omcam-020000000001',
        'device_name': 'Contract board', 'room_name': 'Contract room'}
    service.auto_usb._save(binding)
    jobs = []
    def queue(kind, worker, *args):
        job_id = f'contract-job-{len(jobs)+1}'
        jobs.append((kind, worker, args, job_id))
        return {'id': job_id}
    monkeypatch.setattr(service, '_create_job', queue)
    yield service, binding, jobs
    service.shutdown()


def request(binding, action='retry_install', **overrides):
    return AutoUsbRecoveryRequest(**{'action': action, 'port': PORT['device'], 'board_model': MODEL,
        'mac_address': MAC, 'manifest_sha256': binding['manifest_sha256'],
        'binding_authorized_at': binding['authorized_at'], 'ssid': 'contract-private-network',
        'password': 'contract-private-password', 'authorize_recovery': True,
        'authorize_overwrite': action == 'retry_install', **overrides})


def receipt(service, binding, status='failed', flashed_at=None):
    with service.connect() as connection:
        connection.execute('INSERT INTO firmware_usb_receipts(mac,manifest_sha256,board_model,status,flashed_at,linked_at,job_id,error) VALUES(?,?,?,?,?,?,?,?)',
            (MAC, binding['manifest_sha256'], MODEL, status, flashed_at, 'original-linked-time' if status=='linked' else None,
             'original-job', 'original-contract-error'))


def run_queued(jobs, index=0):
    _, worker, args, job_id = jobs[index]
    return worker(job_id, *args)


@pytest.mark.parametrize('value', [False, None, 1, 'true'])
def test_recovery_consent_is_literal_boolean_true(value):
    with pytest.raises(ValueError):
        request({'manifest_sha256': 'a'*64, 'authorized_at': 'stamp'}, authorize_recovery=value)


@pytest.mark.parametrize('action,authorize', [('retry_install', False), ('retry_install', 1),
    ('retry_install', 'true'), ('reprovision', True)])
def test_unknown_write_requires_separate_exact_overwrite_authorization(action, authorize):
    with pytest.raises(ValueError):
        request({'manifest_sha256': 'a'*64, 'authorized_at': 'stamp'}, action, authorize_overwrite=authorize)


@pytest.mark.parametrize('field,value', [('board_model', 'ai_thinker_esp32cam'), ('mac_address','02:00:00:00:00:99'),
    ('manifest_sha256','b'*64), ('binding_authorized_at','stale-version'), ('port','COM8')])
def test_mismatched_target_revision_or_port_never_reserves_or_opens(fw, field, value):
    service, binding, jobs = fw
    receipt(service, binding)
    with pytest.raises(HTTPException) as error:
        service.auto_usb.recover(request(binding, **{field:value}))
    assert error.value.status_code == 409 and jobs == []
    assert service.auto_usb._receipt(binding)['status'] == 'failed'


@pytest.mark.parametrize('mode', ['TEST', 'DEMO'])
def test_other_runtime_modes_cannot_recover(fw, mode):
    service, binding, jobs = fw
    receipt(service, binding)
    service.runtime_mode = mode
    with pytest.raises(HTTPException) as error:
        service.auto_usb.recover(request(binding))
    assert error.value.status_code == 409 and not jobs


def test_changed_bundle_or_usb_bridge_rejected_before_reservation(fw, monkeypatch):
    service, binding, jobs = fw
    receipt(service, binding)
    manifest = service.project_root/'artifacts/firmware/xiao-esp32s3-sense/manifest.json'
    original = manifest.read_bytes()
    manifest.write_bytes(original+b' ')
    with pytest.raises(HTTPException): service.auto_usb.recover(request(binding))
    manifest.write_bytes(original)
    monkeypatch.setattr(service, 'discover_ports', lambda: [{**PORT, 'serial_number': 'other-board'}])
    with pytest.raises(HTTPException): service.auto_usb.recover(request(binding))
    assert not jobs


@pytest.mark.parametrize('status', ['flashed','linked','failed','interrupted'])
def test_proved_flash_only_reprovisions_and_preserves_success_times(fw, monkeypatch, status):
    service, binding, jobs = fw
    receipt(service, binding, status, 'original-flash-time')
    controller = service.auto_usb
    assert controller.status()['recovery']['allowed_actions'] == ['reprovision']
    with pytest.raises(HTTPException): controller.recover(request(binding))
    calls = []
    def tool(payload):
        calls.append(payload['operation'])
        assert payload['mac_address'] == MAC and payload['bridge'] == BRIDGE and payload['board_model'] == MODEL
        return {'mac_address':MAC,'chip':'ESP32-S3','board_model':MODEL,'physical_flash_performed':False}
    monkeypatch.setattr(controller, '_tool', tool)
    monkeypatch.setattr(service, 'get_device', lambda *args, **kwargs: {'mac_address':MAC,'simulated':False})
    def configure(*args, **kwargs):
        assert kwargs['expected_mac'] == MAC and kwargs['expected_bridge'] == BRIDGE
        calls.append('provision'); return {'contract_only':True}
    monkeypatch.setattr(service, '_provision_worker', configure)
    result = controller.recover(request(binding, 'reprovision'))
    assert result['recovery_action']=='reprovision'
    final = run_queued(jobs)
    assert calls == ['identify','provision'] and final['physical_flash_performed'] is False
    assert controller._receipt(binding)['flashed_at'] == 'original-flash-time'
    assert controller.status()['recovery']['last_attempt']['status'] == 'succeeded'
    with service.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM firmware_usb_receipts').fetchone()[0] == 1
        previous = json.loads(connection.execute('SELECT previous_receipt FROM firmware_usb_recoveries').fetchone()[0])
        assert previous['flashed_at'] == 'original-flash-time' and previous['job_id']=='original-job'


def test_uncertain_recovery_is_one_durable_attempt_and_duplicate_click_is_rejected(fw, monkeypatch):
    service, binding, jobs = fw
    receipt(service, binding, 'interrupted')
    controller = service.auto_usb
    asked = request(binding)
    controller.recover(asked)
    assert controller._receipt(binding)['status'] == 'reserved'
    assert controller.status()['recovery']['state']=='busy'
    with pytest.raises(HTTPException): controller.recover(asked)
    assert len(jobs)==1
    with service.connect() as connection:
        stored = '\n'.join(str(tuple(row)) for table in ('firmware_usb_binding','firmware_usb_receipts','firmware_usb_recoveries')
                           for row in connection.execute(f'SELECT * FROM {table}'))
    assert asked.password not in stored and asked.ssid not in stored
    calls=[]
    def tool(payload):
        calls.append(payload['operation'])
        return {'mac_address':MAC,'chip':'ESP32-S3','board_model':MODEL,'physical_flash_performed':True,
            'manifest_sha256':binding['manifest_sha256']}
    monkeypatch.setattr(controller, '_tool', tool)
    monkeypatch.setattr(service, '_provision_worker', lambda *args, **kwargs: {'contract_only':True})
    monkeypatch.setattr(service, 'get_device', lambda *args, **kwargs: {'mac_address':MAC,'simulated':False})
    run_queued(jobs)
    with pytest.raises(RuntimeError): run_queued(jobs)
    assert calls==['flash'] and jobs[0][2][-2]=={}  # sensitive worker arguments cleared
    with pytest.raises(HTTPException): controller.recover(asked)


def test_failed_recovery_does_not_retry_on_tick_or_replay(fw, monkeypatch):
    service, binding, jobs = fw
    receipt(service, binding)
    controller=service.auto_usb
    controller.recover(request(binding))
    calls=[]
    def fail(payload):
        calls.append(payload['operation']); raise RuntimeError('controlled fake USB failure')
    monkeypatch.setattr(controller,'_tool',fail)
    with pytest.raises(RuntimeError): run_queued(jobs)
    assert controller._receipt(binding)['status']=='failed'
    controller.seen=False
    for _ in range(3): controller.tick()
    assert len(jobs)==1 and calls==['flash']
    assert controller.status()['recovery']['last_attempt']['status']=='failed'


def test_queue_failure_and_restart_do_not_rearm_write(fw, monkeypatch):
    service, binding, jobs = fw
    receipt(service,binding)
    def fail(*args): raise RuntimeError('controlled queue failure')
    monkeypatch.setattr(service,'_create_job',fail)
    with pytest.raises(RuntimeError): service.auto_usb.recover(request(binding))
    assert service.auto_usb._receipt(binding)['status']=='interrupted'
    assert service.auto_usb.status()['recovery']['last_attempt']['status']=='interrupted'
    monkeypatch.setattr(AutoUsbService,'_start_listener',lambda *args:None)
    restarted=AutoUsbService(service)
    restarted.tick()
    assert restarted._receipt(binding)['status']=='interrupted'
    assert restarted.credentials is None
    restarted.close()


def test_service_restart_interrupts_queued_recovery_without_erasing_receipt(fw, monkeypatch):
    service,binding,jobs=fw
    receipt(service,binding)
    service.auto_usb.recover(request(binding))
    monkeypatch.setattr(AutoUsbService,'_start_listener',lambda *args:None)
    restarted=AutoUsbService(service)
    assert restarted._receipt(binding)['status']=='interrupted'
    assert restarted.status()['recovery']['last_attempt']['status']=='interrupted'
    with pytest.raises(RuntimeError): run_queued(jobs)
    restarted.close()


def test_verified_local_backend_change_updates_binding_only_not_target_or_receipt(fw, monkeypatch):
    service,binding,jobs=fw
    receipt(service,binding,'linked','original-flash-time')
    seen=[]
    monkeypatch.setattr(service,'_verify_backend_listener',lambda url: seen.append(url))
    service.auto_usb.recover(request(binding,'reprovision',backend_url='http://192.168.43.20:8018'))
    current=service.auto_usb._binding()
    assert seen==['http://192.168.43.20:8018'] and current['backend_url']==seen[0]
    for key in ('board_model','mac_address','manifest_sha256','bridge'): assert current[key]==binding[key]
    assert current['authorized_at']!=binding['authorized_at']
    assert service.auto_usb._receipt(current)['flashed_at']=='original-flash-time'


def test_backend_not_this_listener_is_rejected_without_mutation(fw, monkeypatch):
    service,binding,jobs=fw
    receipt(service,binding)
    def reject(url): raise HTTPException(409,'not this local listener')
    monkeypatch.setattr(service,'_verify_backend_listener',reject)
    with pytest.raises(HTTPException): service.auto_usb.recover(request(binding,backend_url='http://192.168.43.20:8018'))
    assert service.auto_usb._binding()==binding and not jobs


@pytest.mark.parametrize('url',['https://example.com','http://8.8.8.8:8018','http://user:pass@192.168.1.20:8018','file:///secret'])
def test_backend_cannot_be_arbitrary_destination(url):
    with pytest.raises(ValueError): request({'manifest_sha256':'a'*64,'authorized_at':'stamp'},backend_url=url)


def test_build_lock_and_disabled_binding_reject_before_queue(fw):
    service,binding,jobs=fw
    receipt(service,binding)
    with service.build_lock:
        with pytest.raises(HTTPException): service.auto_usb.recover(request(binding))
    service.auto_usb.disable()
    with pytest.raises(HTTPException): service.auto_usb.recover(request(binding))
    assert not jobs


def test_simultaneous_recovery_requests_reserve_only_one_job(fw):
    service,binding,jobs=fw
    receipt(service,binding)
    barrier=threading.Barrier(3)
    results=[]
    def recover():
        barrier.wait()
        try: service.auto_usb.recover(request(binding)); results.append('accepted')
        except HTTPException: results.append('rejected')
    threads=[threading.Thread(target=recover) for _ in range(2)]
    for thread in threads: thread.start()
    barrier.wait()
    for thread in threads: thread.join(5); assert not thread.is_alive()
    assert sorted(results)==['accepted','rejected'] and len(jobs)==1


def test_real_background_job_waits_for_committed_authorization_without_deadlock(fw, monkeypatch):
    service,binding,jobs=fw
    receipt(service,binding,'flashed','original-flash-time')
    monkeypatch.setattr(service,'_create_job',FirmwareService._create_job.__get__(service))
    called=[]
    def tool(payload):
        called.append(payload['operation'])
        return {'mac_address':MAC,'chip':'ESP32-S3','board_model':MODEL,'physical_flash_performed':False}
    monkeypatch.setattr(service.auto_usb,'_tool',tool)
    monkeypatch.setattr(service,'get_device',lambda *args,**kwargs:{'mac_address':MAC,'simulated':False})
    monkeypatch.setattr(service,'_provision_worker',lambda *args,**kwargs:{'contract_only':True})
    finished=threading.Event()
    update=service._update_job
    def track_update(job_id,**values):
        update(job_id,**values)
        if values.get('status') in {'succeeded','failed'}: finished.set()
    monkeypatch.setattr(service,'_update_job',track_update)
    job=service.auto_usb.recover(request(binding,'reprovision'))
    # Actual FirmwareService thread lifecycle, no sleeps and no physical USB.
    assert finished.wait(5), 'The real queued worker did not finish in time'
    service.shutdown(timeout_seconds=5)
    assert service.get_job(job['id'])['status']=='succeeded'
    assert called==['identify'] and service.auto_usb._receipt(binding)['flashed_at']=='original-flash-time'


@pytest.mark.parametrize('change',['binding','bundle','receipt_job','receipt_success','rom_mac','rom_chip'])
def test_queued_recovery_rechecks_authorization_and_identity_before_write_or_config(fw,monkeypatch,change):
    service,binding,jobs=fw
    receipt(service,binding)
    controller=service.auto_usb
    controller.recover(request(binding))
    calls=[]
    def tool(payload):
        calls.append(payload['operation'])
        return {'mac_address':'02:00:00:00:00:99' if change=='rom_mac' else MAC,
            'chip':'ESP32' if change=='rom_chip' else 'ESP32-S3','board_model':MODEL,
            'physical_flash_performed':payload['operation']=='flash','manifest_sha256':binding['manifest_sha256']}
    monkeypatch.setattr(controller,'_tool',tool)
    monkeypatch.setattr(service,'_provision_worker',lambda *args,**kwargs:pytest.fail('Must not provision changed/unknown board'))
    if change=='binding': controller._save({**controller._binding(),'enabled':False})
    if change=='bundle':
        path=service.project_root/'artifacts/firmware/xiao-esp32s3-sense/manifest.json'
        path.write_bytes(path.read_bytes()+b' ')
    if change=='receipt_job':
        with service.connect() as connection: connection.execute("UPDATE firmware_usb_receipts SET job_id='other-task'")
    if change=='receipt_success':
        with service.connect() as connection: connection.execute("UPDATE firmware_usb_receipts SET status='flashed',flashed_at='new-durable-success'")
        monkeypatch.setattr(service,'get_device',lambda *args,**kwargs:{'mac_address':MAC,'simulated':False})
        # The worker must select identify based on fresh durable evidence, not
        # stale queued action. A controlled boot-read error stops before config.
        def stop_read(*args): raise RuntimeError('controlled identity read failure')
        monkeypatch.setattr(controller,'_tool',lambda payload: calls.append(payload['operation']) or stop_read())
    with pytest.raises((RuntimeError, ValueError)): run_queued(jobs)
    if change in {'binding','bundle','receipt_job'}: assert calls==[]
    elif change=='receipt_success':
        assert calls==['identify'] and controller._receipt(binding)['flashed_at']=='new-durable-success'
    else: assert calls==['flash']  # Explicit tool double; helper separately gates ROM before any physical write.


def test_existing_flash_evidence_survives_configuration_failure(fw, monkeypatch):
    service,binding,jobs=fw
    receipt(service,binding,'linked','original-flash-time')
    controller=service.auto_usb
    monkeypatch.setattr(controller,'_tool',lambda payload:{'mac_address':MAC,'chip':'ESP32-S3','board_model':MODEL,'physical_flash_performed':False})
    def fail(*args,**kwargs): raise RuntimeError('controlled provisioning failure')
    monkeypatch.setattr(service,'_provision_worker',fail)
    controller.recover(request(binding,'reprovision'))
    with pytest.raises(RuntimeError): run_queued(jobs)
    row=controller._receipt(binding)
    assert row['status']=='flashed' and row['flashed_at']=='original-flash-time' and row['linked_at']=='original-linked-time'
    assert controller.status()['recovery']['allowed_actions']==['reprovision']


def test_actual_fastapi_recovery_route_requires_admin_and_refuses_test_hardware(tmp_path, monkeypatch):
    app=create_app(data_dir=tmp_path/'route-test',testing=True)
    with TestClient(app) as client:
        controller=app.state.firmware_service.auto_usb
        monkeypatch.setattr(controller,'_tool',lambda payload:pytest.fail('TEST route must not open USB'))
        body=request({'manifest_sha256':'a'*64,'authorized_at':'stamp'}).model_dump()
        client.cookies.clear()
        assert client.post('/api/firmware/auto-usb/recover',json=body).status_code==401
        assert client.post('/api/firmware/auto-usb/recover',json=body,
            headers={'Authorization':'Bearer explicit-device-token-not-admin'}).status_code==401
        assert client.get('/api/session').status_code==200
        response=client.post('/api/firmware/auto-usb/recover',json=body)
        assert response.status_code==409 and 'REAL' in response.json()['detail']
        for bad in ({**body,'authorize_overwrite':False}, {**body,'authorize_recovery':1},
                    {**body,'board_model':'../../outside'}, {**body,'arbitrary_command':'erase_flash'}):
            rejected=client.post('/api/firmware/auto-usb/recover',json=bad)
            assert rejected.status_code==422
        with app.state.firmware_service.connect() as connection:
            assert connection.execute('SELECT COUNT(*) FROM firmware_usb_recoveries').fetchone()[0]==0
            assert connection.execute('SELECT COUNT(*) FROM firmware_jobs').fetchone()[0]==0
