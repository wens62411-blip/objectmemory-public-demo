"""Bad-client protocol cleanup through the actual FastAPI WebSocket route.

Uses a controlled cache and an isolated TEST database. It is not a physical
camera, actual detector, public-file integration or 30 FPS acceptance test.
"""
import json

import pytest

from apps.api.tests.test_live_vision_stream import (
    ControlledLatestCache,
    business_counts,
    context,
    decode_packet,
    receive_message,
    route,
    wait_until,
)


@pytest.mark.parametrize('waiting_for_ack', [False, True], ids=['idle', 'awaiting-frame-ack'])
@pytest.mark.parametrize('invalid_input', ['binary', 'malformed-json'])
def test_invalid_message_always_releases_subscription_and_closes(context, waiting_for_ack, invalid_input):
    client, runtime, camera = context
    if waiting_for_ack:
        cache = ControlledLatestCache(camera)
        cache.publish(1)
        runtime.engines[camera['id']] = cache
    before = business_counts(runtime)
    with client.websocket_connect(route(camera), headers={'origin': 'http://testserver'}) as ws:
        initial = receive_message(ws)
        if waiting_for_ack:
            decode_packet(initial)
        else:
            assert json.loads(initial['text'])['type'] == 'unavailable'
        assert runtime.camera_subscribers[camera['id']] == 1
        if invalid_input == 'binary':
            # receive_json(mode='text') must not strand cleanup when input has
            # only bytes rather than the expected text key.
            ws.send_bytes(b'not-a-json-text-ack')
        else:
            ws.send_text('{ definitely not valid JSON')
        message = receive_message(ws, 1.5)
        assert message and message['type'] == 'websocket.close'
        wait_until(lambda: runtime.camera_subscribers[camera['id']] == 0)
    assert business_counts(runtime) == before
    if not waiting_for_ack:
        assert not runtime.engines
