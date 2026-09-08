"""Opt-in E2E diagnostics only; the existing launcher/API remain unchanged.

This mirrors test_graceful_refresh_shutdown.py's child observer. It does not
change timeouts, cancel pending work, open hardware, or turn a failed shutdown
into a pass. Logs belong to the private local verification directory.
"""
from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
import uvicorn
from scripts import serve

if os.environ.get('OM_E2E_OBSERVE_SHUTDOWN') != '1':
    raise SystemExit('This test observer requires an explicit opt-in.')
target = Path(os.environ['OM_E2E_SHUTDOWN_LOG']).resolve()
if not target.is_relative_to((ROOT / 'data' / 'verification').resolve()):
    raise SystemExit('The shutdown observer log must stay in private verification data.')
target.parent.mkdir(parents=True, exist_ok=True)
log = target.open('w', encoding='utf-8', buffering=1)


def record(event, **values):
    log.write(json.dumps({'event': event, 'time': time.time(), 'pid': os.getpid(), **values}) + '\n')


def await_chain(task):
    chain = []
    awaited = task.get_coro()
    for _ in range(20):
        if awaited is None:
            break
        frame = getattr(awaited, 'cr_frame', None) or getattr(awaited, 'gi_frame', None)
        chain.append({'type': type(awaited).__name__, **(
            {'file': frame.f_code.co_filename, 'line': frame.f_lineno,
             'function': frame.f_code.co_name} if frame else {})})
        awaited = getattr(awaited, 'cr_await', None) or getattr(awaited, 'gi_yieldfrom', None)
    return chain


class ObservedServer(uvicorn.Server):
    async def _serve(self, sockets=None):
        record('started', loop=type(asyncio.get_running_loop()).__name__)
        await super()._serve(sockets)

    async def shutdown(self, sockets=None):
        record('shutdown_begin')
        # Native thread stacks are retained even if the event loop is blocked.
        faulthandler.dump_traceback_later(4, repeat=True, file=log)

        async def inspect_pending():
            while True:
                record('shutdown_state', connections=len(self.server_state.connections),
                    tasks=len(self.server_state.tasks),
                    server_active=[getattr(server, '_active_count', None) for server in self.servers],
                    server_sockets_closed=[server.sockets == () for server in self.servers],
                    pending=[{'task': str(task.get_coro()), 'await_chain': await_chain(task), 'stack': [
                        {'file': frame.f_code.co_filename, 'line': frame.f_lineno,
                         'function': frame.f_code.co_name} for frame in task.get_stack()]
                        } for task in asyncio.all_tasks() if task is not asyncio.current_task()])
                await asyncio.sleep(1)

        reporter = asyncio.create_task(inspect_pending())
        try:
            await super().shutdown(sockets)
            record('shutdown_complete')
        finally:
            faulthandler.cancel_dump_traceback_later()
            reporter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reporter


uvicorn.Server = ObservedServer
try:
    raise SystemExit(serve.main())
finally:
    log.close()
