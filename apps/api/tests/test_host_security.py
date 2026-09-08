"""HTTP/WS boundary regression tests, no camera, device or business API mocks."""
import pytest
from urllib.parse import urlsplit
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from apps.api.app.security import LocalSecurityMiddleware, Sessions


@pytest.fixture
def boundary():
    app = FastAPI()
    sessions = Sessions()
    app.add_middleware(LocalSecurityMiddleware, sessions=sessions)

    @app.get('/api/private')
    @app.post('/api/private')
    def private():
        return {'protected': True}

    @app.get('/api/health')
    def health():
        return {'ok': True}

    @app.websocket('/ws')
    async def ws(socket: WebSocket):
        if await sessions.websocket(socket):
            await socket.send_json({'protected': True})
            await socket.close()

    with TestClient(app, base_url='http://127.0.0.1:8018') as client:
        yield client, sessions


@pytest.mark.parametrize('host', ['127.0.0.1/ignored?x=', '127.0.0.1/#',
    '127.0.0.1?x=', '127.0.0.1#fragment', 'attacker.example',
    'user@127.0.0.1', '127.0.0.1:bad', '127.0.0.1:65536', '127.0.0.1:',
    '[::1]/ignore?', '127.0.0.1\\evil', '127.0.0.1\t', ''])
def test_untrusted_host_never_bypasses_auth(boundary, host):
    client, sessions = boundary
    result = client.get('/api/private', headers={'host': host})
    assert result.status_code == 400
    assert result.json().get('protected') is not True
    assert not sessions.tokens


@pytest.mark.parametrize('host', ['localhost:8018', '127.0.0.1:8018', '[::1]:8018', '192.168.1.20:8018'])
def test_literal_local_and_lan_hosts_keep_auth_required(boundary, host):
    client, sessions = boundary
    assert client.get('/api/health', headers={'host': host}).status_code == 200
    assert client.get('/api/private', headers={'host': host}).status_code == 401
    client.cookies.set('om_session', sessions.issue())
    assert client.get('/api/private', headers={'host': host}).status_code == 200


def test_duplicate_host_is_rejected(boundary):
    client, _ = boundary
    assert client.get('/api/private', headers=[('host', '127.0.0.1'), ('host', 'attacker.example')]).status_code == 400


@pytest.mark.parametrize('origin', ['null', 'ftp://127.0.0.1:8018', '//127.0.0.1:8018',
    'http://127.0.0.1:8018/ignored', 'http://127.0.0.1:8018?x=1', 'http://evil.example', 'http://[invalid'])
def test_malformed_origins_fail_closed_without_crashing(origin):
    assert not Sessions().allowed_origin(origin, '127.0.0.1:8018', '127.0.0.1')


def test_ws_requires_trusted_host_even_with_valid_session(boundary):
    client, sessions = boundary
    client.cookies.set('om_session', sessions.issue())
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect('/ws', headers={'host':'attacker.example','origin':'http://attacker.example'}):
            pytest.fail('untrusted websocket accepted')
    assert rejected.value.code == 4403


def test_testserver_host_is_enabled_only_in_explicit_testing():
    assert Sessions(testing=True).allowed_host('testserver')
    assert not Sessions().allowed_host('testserver')


@pytest.mark.parametrize('origin', ['http://localhost:8018', 'http://127.0.0.1:8018',
    'http://192.168.1.20:8018', 'http://10.0.0.20:8018', 'http://[::1]:8018',
    'http://[fd00::20]:8018', 'https://192.168.1.20:8018'])
def test_valid_local_lan_and_secure_websockets_still_work(boundary, origin):
    client, sessions = boundary
    client.cookies.set('om_session', sessions.issue())
    # This is an ASGI authority/scheme test, not an IPv6 socket test. The
    # installed Starlette/httpx TestClient cannot parse IPv6 transport URLs.
    parsed = urlsplit(origin)
    transport = parsed.scheme+'://127.0.0.1:8018'
    headers = {'Origin':origin, 'Host':parsed.netloc}
    assert client.post(transport+'/api/private', headers=headers).status_code == 200
    with client.websocket_connect(transport.replace('http','ws',1)+'/ws', headers=headers) as socket:
        assert socket.receive_json() == {'protected':True}


@pytest.mark.parametrize('origin', ['http://evil.example', 'https://127.0.0.1:8018'])
def test_cross_origin_or_scheme_rejected_with_valid_session(boundary, origin):
    client, sessions = boundary
    client.cookies.set('om_session', sessions.issue())
    assert client.post('/api/private', headers={'Origin':origin}).status_code == 403
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect('ws://127.0.0.1:8018/ws', headers={'Origin':origin}):
            pytest.fail('cross-origin websocket accepted')
    assert rejected.value.code == 4403
