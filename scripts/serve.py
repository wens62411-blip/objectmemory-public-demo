"""Native Windows launcher; browser opens only after the server answers health."""
import argparse
import asyncio
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def run_server(server):
    if sys.platform=='win32':
        # CPython 3.12's Proactor close callback can raise WinError 10054
        # before detaching the transport, leaving Server.wait_closed pending.
        # This launcher is single-process; encoding/firmware subprocesses run
        # in ordinary worker threads, not asyncio subprocess transports.
        # Scope this supported socket loop to one run without global policies,
        # stdlib patches, or suppressed connection errors.
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(server.serve())
    else:
        server.run()

def listener_metadata(server, requested_host, requested_port):
    """Describe only listening sockets owned by this live Uvicorn instance."""
    from apps.api.app.main import lan_addresses
    bindings=[]
    if server is not None and server.started and not server.should_exit:
        for listener in getattr(server,'servers',[]):
            for bound_socket in listener.sockets or []:
                try:
                    if bound_socket.getsockopt(socket.SOL_SOCKET,socket.SO_ACCEPTCONN)!=1:continue
                    address=bound_socket.getsockname()
                    bindings.append({'host':str(address[0]),'port':int(address[1])})
                except OSError:continue
    return {'listener_verified':bool(bindings),'listener_bindings':bindings,
            'requested_host':requested_host,'requested_port':requested_port,
            'process_id':os.getpid(),'local_addresses':lan_addresses()}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8018)
    parser.add_argument('--mode',choices=('REAL','DEMO','TEST'),default=os.environ.get('OM_RUNTIME_MODE','REAL').upper())
    parser.add_argument('--lan',action='store_true',help='Explicitly bind all interfaces; enables PIN-protected LAN access.')
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--dev',action='store_true')
    parser.add_argument('--shutdown-file',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--shutdown-ack-file',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--pid-file',type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args()
    host='0.0.0.0' if args.lan or os.environ.get('OM_ALLOW_LAN')=='1' else '127.0.0.1'
    if not 1024<=args.port<=65535:parser.error('port must be 1024-65535')
    with socket.socket() as probe:
        if os.name=='nt':probe.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        try:probe.bind((host,args.port))
        except OSError:
            print(f'Port {args.port} is busy. Use start.ps1 -Port 8019 or stop the earlier ObjectMemory instance.',file=sys.stderr)
            return 1
    os.chdir(ROOT)
    os.environ['OM_PORT']=str(args.port)
    os.environ['OM_RUNTIME_MODE']=args.mode
    if args.dev:os.environ['OM_DEV_ORIGIN']='http://127.0.0.1:5173'
    from apps.api.app.main import create_app,lan_addresses
    server=None
    app=create_app(runtime_mode=args.mode,listener_info_provider=lambda:listener_metadata(server,host,args.port))
    if args.pid_file:
        pid_file=args.pid_file.resolve()
        pid_file.parent.mkdir(parents=True,exist_ok=True)
        temporary=pid_file.with_suffix(pid_file.suffix+'.tmp')
        temporary.write_text(str(os.getpid())+'\n',encoding='ascii')
        os.replace(temporary,pid_file)
    import uvicorn
    child=None
    if args.dev:
        env=os.environ.copy();env['OM_API_PORT']=str(args.port)
        child=subprocess.Popen(['npm.cmd','run','dev','--','--host','127.0.0.1'],cwd=ROOT/'apps'/'web',env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    url=f'http://127.0.0.1:{5173 if args.dev else args.port}'
    print(f'ObjectMemory: {url}',flush=True)
    if host=='0.0.0.0':
        for address in lan_addresses():
            if address not in {'localhost','127.0.0.1','::1'}:print(f'LAN: http://{address}:{args.port}',flush=True)
        print('LAN access was explicitly enabled. Use the generated administrator PIN.',flush=True)
    print(f'Runtime mode: {args.mode}. Ctrl+C stops this server.',flush=True)
    if not args.no_browser:
        def open_when_ready():
            for _ in range(100):
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/api/health',timeout=1):pass
                    webbrowser.open(url);return
                except OSError:time.sleep(.25)
        threading.Thread(target=open_when_ready,daemon=True).start()
    class ShutdownAwareServer(uvicorn.Server):
        async def shutdown(self,sockets=None):
            # Uvicorn drains HTTP connections before lifespan shutdown. Let
            # infinite MJPEG/status subscriptions finish before that wait,
            # whether shutdown came from Ctrl+C or the launcher request file.
            app.state.runtime.shutdown_requested.set()
            await super().shutdown(sockets)

    server=ShutdownAwareServer(uvicorn.Config(app,host=host,port=args.port,access_log=False))
    shutdown_ack_file=args.shutdown_ack_file.resolve() if args.shutdown_ack_file else None
    if shutdown_ack_file:
        try:shutdown_ack_file.unlink(missing_ok=True)
        except OSError:pass
    if args.shutdown_file:
        shutdown_file=args.shutdown_file.resolve()
        try:shutdown_file.unlink(missing_ok=True)
        except OSError:pass
        def stop_when_requested():
            while not server.should_exit:
                if shutdown_file.is_file():
                    server.should_exit=True
                    return
                time.sleep(.2)
        threading.Thread(target=stop_when_requested,name='objectmemory-graceful-shutdown',daemon=True).start()
    try:run_server(server)
    finally:
        if child:child.terminate()
        # This acknowledgement is stronger than a failed health request: it is
        # emitted only after Uvicorn ran FastAPI's lifespan shutdown and the
        # runtime-mode lease is no longer held.  E2E cleanup fails closed when a
        # non-cancellable worker deliberately keeps that lease alive.
        if shutdown_ack_file:
            lease=getattr(getattr(app.state,'runtime',None),'mode_lease',None)
            deadline=time.monotonic()+5
            while lease is not None and lease.held and time.monotonic()<deadline:
                time.sleep(.05)
            if lease is None or not lease.held:
                shutdown_ack_file.parent.mkdir(parents=True,exist_ok=True)
                temporary=shutdown_ack_file.with_suffix(shutdown_ack_file.suffix+'.tmp')
                temporary.write_text('stopped\n',encoding='ascii')
                os.replace(temporary,shutdown_ack_file)
    return 0

if __name__=='__main__':raise SystemExit(main())
