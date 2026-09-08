"""Real local sockets/browser/video and the existing launcher shutdown contract.

The child subclass only reports Uvicorn's shutdown state; no business routes or
camera frames are mocked. It deliberately never opens a physical camera.
"""
from __future__ import annotations

import json
import asyncio
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import time

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[3]
CHILD = r'''
import asyncio, contextlib, json, os, signal, sys, threading, time, uvicorn
from scripts import serve

class ObservedServer(uvicorn.Server):
    async def _serve(self, sockets=None):
        print('[RUNNING_LOOP] ' + type(asyncio.get_running_loop()).__name__, flush=True)
        await super()._serve(sockets)

    async def shutdown(self, sockets=None):
        async def inspect():
            while True:
                await asyncio.sleep(1)
                print("[SHUTDOWN_STATE] " + json.dumps({
                    "loop": type(asyncio.get_running_loop()).__name__,
                    "connections": len(self.server_state.connections),
                    "tasks": len(self.server_state.tasks),
                    "server_active": [getattr(s, "_active_count", None) for s in self.servers],
                    "server_sockets_closed": [s.sockets == () for s in self.servers],
                    "pending": [{"task": str(t.get_coro()), "stack": [f.f_code.co_name for f in t.get_stack()]} for t in asyncio.all_tasks() if t is not asyncio.current_task()],
                }), flush=True)
        reporter = asyncio.create_task(inspect())
        try:
            await super().shutdown(sockets)
        finally:
            reporter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reporter

uvicorn.Server = ObservedServer
if os.environ.get('OM_PROBE_INTERRUPT_FILE'):
    def interrupt_when_requested():
        while not os.path.isfile(os.environ['OM_PROBE_INTERRUPT_FILE']):
            time.sleep(.05)
        signal.raise_signal(signal.SIGINT)
    threading.Thread(target=interrupt_when_requested,daemon=True).start()
raise SystemExit(serve.main())
'''
BROWSER = r'''
const { chromium } = require('@playwright/test');
(async () => {
  const base = process.argv[1], id = process.argv[2];
  const browser = await chromium.launch({headless:true});
  try {
    const context = await browser.newContext();
    const pages = await Promise.all(Array.from({length:5}, () => context.newPage()));
    let received = 0;
    const decodedFrames = [];
    for (let round = 0; round < 3; round++) {
      for (const page of pages) {
        await page.bringToFront();
        await page.goto(`${base}/live?camera=${id}`, {waitUntil:'domcontentloaded'});
        // VisionFrame double-buffers images. Only the decoded, actually visible
        // primary image counts, never the hidden pending slot or a stale MJPEG
        // component that no longer exists in the production frontend.
        const first = await page.waitForFunction(() => {
          const img = [...document.querySelectorAll('.vision-frame-primary .vision-frame-image > img')]
            .find(node => getComputedStyle(node).visibility === 'visible' && node.complete
              && node.naturalWidth > 0 && node.getBoundingClientRect().width > 0);
          return img?.dataset.sourceSession && Number(img.dataset.sourceFrame) > 0
            ? {session:img.dataset.sourceSession,frame:Number(img.dataset.sourceFrame)} : null;
        }, null, {timeout:10000});
        const prior = await first.jsonValue();
        const next = await page.waitForFunction(previous => {
          const img = [...document.querySelectorAll('.vision-frame-primary .vision-frame-image > img')]
            .find(node => getComputedStyle(node).visibility === 'visible' && node.complete
              && node.naturalWidth > 0 && node.getBoundingClientRect().width > 0);
          return img?.dataset.sourceSession === previous.session && Number(img.dataset.sourceFrame) > previous.frame
            ? {session:img.dataset.sourceSession,frame:Number(img.dataset.sourceFrame)} : null;
        }, prior, {timeout:5000});
        decodedFrames.push({first:prior,next:await next.jsonValue()});
        await first.dispose(); await next.dispose();
        received += await page.evaluate(async ({base,id}) => {
          return await new Promise((resolve,reject) => {
            const ws = new WebSocket(`${base.replace('http:', 'ws:')}/ws/cameras/${id}`);
            const timer = setTimeout(() => reject(new Error('status WS did not publish')), 5000);
            ws.onmessage = () => { clearTimeout(timer); window.probeSocket=ws; resolve(1); };
            ws.onerror = () => { clearTimeout(timer); reject(new Error('status WS failed')); };
          });
        }, {base,id});
      }
    }
    await context.close();
    process.stdout.write(JSON.stringify({pages:5,navigations:15,websocket_messages:received,
      decoded_frame_progress:decodedFrames,business_mock:false})+'\n');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode=1; });
'''


def update_machine_report(*, case=None, unit=None):
    target = os.environ.get("OM_SHUTDOWN_PROBE_REPORT")
    if not target:
        return
    destination = Path(target)
    summary = json.loads(destination.read_text(encoding="utf-8")) if destination.exists() else {}
    if summary.get("schema") != "objectmemory-shutdown-regression-v1":
        summary = {"schema":"objectmemory-shutdown-regression-v1","cases":[],"units":{}}
    if case is not None:
        key = (case["hold_stream_at_shutdown"],case["shutdown_kind"])
        summary["cases"] = [row for row in summary["cases"] if (row["hold_stream_at_shutdown"],row["shutdown_kind"]) != key] + [case]
    if unit is not None:
        summary["units"].update(unit)
    summary.update({"physical_camera":False,"original_soak_reclassified":False,
                    "native_proactor_race_reproduced":False,
                    "implementation":"launcher-local Windows Selector runner plus explicit pre-drain signal"})
    summary["pre_fix_native_attempts"] = []
    for name in ("graceful-refresh-before.json", "graceful-refresh-rst-before.json"):
        previous_path = ROOT / "data/verification" / name
        if previous_path.exists():
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            summary["pre_fix_native_attempts"].append({key:previous.get(key) for key in (
                "tcp_rst_count","browser_return_code","browser_stdout","subscribers_after_disconnect",
                "event_count","shutdown_seconds","return_code","shutdown_ack","elapsed_seconds",
            )})
    failed_path = ROOT / "data/verification/graceful-held-stream-before.json"
    if failed_path.exists():
        previous = json.loads(failed_path.read_text(encoding="utf-8"))
        summary["held_stream_before_fix"] = {key:previous.get(key) for key in (
            "hold_stream_at_shutdown","subscribers_with_held_stream","shutdown_seconds",
            "return_code","shutdown_ack","shutdown_timeout",
        )}
        snapshots = [line.split("[SHUTDOWN_STATE] ",1)[1] for line in previous.get("backend_log","").splitlines() if "[SHUTDOWN_STATE] " in line]
        summary["held_stream_before_fix"]["diagnostic_snapshot"] = json.loads(snapshots[0]) if snapshots else None
    summary["passed"] = len(summary["cases"]) == 3 and all(row.get("passed") is True for row in summary["cases"]) and len(summary["units"]) == 2 and all(row.get("passed") is True for row in summary["units"].values())
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")


@pytest.mark.parametrize("hold_stream,shutdown_kind", [(False,"file"),(True,"file"),(True,"sigint")])
def test_real_browser_refresh_rst_releases_subscribers_and_launcher(tmp_path, hold_stream, shutdown_kind):
    if not shutil.which("node"):
        pytest.skip("Real browser probe requires installed Node and Playwright")
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    data = tmp_path / "isolated-data"
    shutdown = tmp_path / "shutdown-request"
    interrupt = tmp_path / "interrupt-request"
    ack = tmp_path / "shutdown-complete"
    log_path = tmp_path / "backend.log"
    report = {"mode": "DEMO", "physical_camera": False, "business_mock": False, "port": port,
              "hold_stream_at_shutdown":hold_stream,"shutdown_kind":shutdown_kind,"passed":False}
    process = None
    held_connection = None
    started = time.monotonic()
    try:
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", CHILD, "--mode", "DEMO", "--port", str(port),
                 "--no-browser", "--shutdown-file", str(shutdown), "--shutdown-ack-file", str(ack)],
                cwd=ROOT, env={**os.environ,"OM_DATA_DIR":str(data),"OM_ALLOW_LAN":"0",
                               "OM_PROBE_INTERRUPT_FILE":str(interrupt) if shutdown_kind=="sigint" else ""},
                stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),
            )
            with httpx.Client(base_url=base, trust_env=False, timeout=8) as client:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    try:
                        if client.get("/api/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    assert process.poll() is None, log_path.read_text(encoding="utf-8")
                    time.sleep(.1)
                assert client.get("/api/session").status_code == 200
                assert client.patch("/api/settings", json={"show_hands":False,"record_events":False}).status_code == 200
                camera = client.post("/api/cameras", json={
                    "name":"关闭回归视频", "source_type":"video",
                    "source":str(ROOT / "demo/sample-videos/object-memory-demo.avi"),
                    "config":{"loop":True,"realtime":True}, "inference_fps":5,
                }).json()
                camera_id = camera["id"]
                assert client.post(f"/api/cameras/{camera_id}/start").status_code == 200
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and client.get(f"/api/cameras/{camera_id}/frame").status_code != 200:
                    time.sleep(.1)
                # Force real TCP RST while the server owns an active MJPEG
                # transport. No method or exception is replaced in asyncio.
                cookie = "; ".join(f"{k}={v}" for k,v in client.cookies.items())
                reset_count = 0
                for index in range(300):
                    with socket.create_connection(("127.0.0.1",port), timeout=5) as connection:
                        target = f"/api/cameras/{camera_id}/stream" if index % 3 == 0 else f"/api/cameras/{camera_id}/frame" if index % 3 == 1 else "/api/health"
                        connection.sendall((f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nCookie: {cookie}\r\nConnection: close\r\n\r\n").encode("ascii"))
                        assert connection.recv(64).startswith(b"HTTP/1.1 200")
                        connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh" if os.name=="nt" else "ii",1,0))
                        reset_count += 1
                report["tcp_rst_count"] = reset_count
                browser = subprocess.run(["node","-e",BROWSER,base,camera_id],cwd=ROOT/"apps/web",capture_output=True,text=True,timeout=60)
                report["browser_return_code"] = browser.returncode
                report["browser_stdout"] = browser.stdout
                report["browser_stderr"] = browser.stderr
                assert browser.returncode == 0, browser.stderr
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    health = client.get(f"/api/cameras/{camera_id}").json()["health"]
                    if health.get("subscribers") == 0:
                        break
                    time.sleep(.1)
                report["subscribers_after_disconnect"] = health.get("subscribers")
                report["event_count"] = len(client.get("/api/events").json())
                if hold_stream:
                    held_connection = socket.create_connection(("127.0.0.1",port), timeout=5)
                    held_connection.sendall((f"GET /api/cameras/{camera_id}/stream HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nCookie: {cookie}\r\n\r\n").encode("ascii"))
                    assert held_connection.recv(1024).startswith(b"HTTP/1.1 200")
                    report["subscribers_with_held_stream"] = client.get(f"/api/cameras/{camera_id}").json()["health"].get("subscribers")
                if shutdown_kind == "file":
                    assert client.post(f"/api/cameras/{camera_id}/stop").status_code == 200
            (interrupt if shutdown_kind=="sigint" else shutdown).write_text("shutdown\n", encoding="ascii")
            shutdown_started = time.monotonic()
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                report["shutdown_timeout"] = True
            report["shutdown_seconds"] = round(time.monotonic() - shutdown_started, 3)
            report["return_code"] = process.poll()
            report["shutdown_ack"] = ack.exists()
            if shutdown_kind == "file":
                assert process.poll() == 0 and ack.exists(), "Launcher did not complete real lifespan shutdown"
            else:
                # Uvicorn intentionally re-raises Ctrl+C after cleanup. This
                # operator-interrupt exit is not the file-shutdown exit-0 gate.
                assert process.poll() is not None and ack.exists(), "SIGINT did not complete lifespan shutdown"
                assert "Application shutdown complete" in log_path.read_text(encoding="utf-8")
            assert report["subscribers_after_disconnect"] == 0
            assert report["event_count"] == 0
            report["passed"] = True
    finally:
        if held_connection is not None:
            held_connection.close()
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        report["elapsed_seconds"] = round(time.monotonic()-started,3)
        report["backend_log"] = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        (tmp_path / "probe.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        update_machine_report(case=report)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Proactor mechanism diagnostic")
def test_fault_injected_proactor_shutdown_error_skips_server_detach():
    """Injected unit mechanism, NOT a claimed native reproduction of the soak."""
    from asyncio.proactor_events import _ProactorBasePipeTransport
    from types import SimpleNamespace

    calls = []

    class ResetSocket:
        def fileno(self):return 1
        def shutdown(self, _how):raise ConnectionResetError(10054,"test-only injected reset")
        def close(self):calls.append("socket_closed")

    transport = _ProactorBasePipeTransport.__new__(_ProactorBasePipeTransport)
    transport._called_connection_lost = False
    transport._sock = ResetSocket()
    transport._protocol = SimpleNamespace(connection_lost=lambda exc:calls.append("protocol_closed"))
    transport._server = SimpleNamespace(_detach=lambda:calls.append("server_detached"))
    try:
        with pytest.raises(ConnectionResetError,match="test-only injected reset"):
            transport._call_connection_lost(None)
        assert calls == ["protocol_closed"]
        assert transport._called_connection_lost is False
        update_machine_report(unit={"proactor_injected_mechanism":{"passed":True,"fault_injected":True,"native_race_reproduced":False,"calls":calls,"server_detached":False}})
    finally:
        # This diagnostic object never owned a real socket or event loop.
        transport._sock = None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher loop contract")
def test_windows_runner_scope_preserves_policy_and_worker_subprocess_support():
    from scripts.serve import run_server

    policy = asyncio.get_event_loop_policy()
    observations = {}

    class Server:
        async def serve(self):
            loop = asyncio.get_running_loop()
            observations["loop"] = loop
            result = await asyncio.to_thread(subprocess.run,[sys.executable,"-c","print('worker-ok')"],
                                              capture_output=True,text=True,check=True)
            observations["worker"] = result.stdout.strip()
        def run(self):raise AssertionError("Windows must use its scoped runner")

    run_server(Server())
    assert isinstance(observations["loop"], asyncio.SelectorEventLoop)
    assert observations["loop"].is_closed()
    assert observations["worker"] == "worker-ok"
    assert asyncio.get_event_loop_policy() is policy
    update_machine_report(unit={"windows_runner_scope":{"passed":True,"loop":type(observations["loop"]).__name__,"closed":True,"global_policy_unchanged":True,"worker_subprocess":observations["worker"]}})
