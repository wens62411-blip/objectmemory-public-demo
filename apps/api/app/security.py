import hashlib
import ipaddress
import re
import os
import secrets
import socket
import time
from urllib.parse import urlsplit, urlunsplit
from fastapi import HTTPException, Request, WebSocket
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


def redact(value):
    text = str(value)
    text = re.sub(r'(://)[^/@\s]+@',r'\1***@',text)
    text = re.sub(r'(?i)(password|passwd|token|pairing_code)([\s"\x27:=]+)([^\s,}"\x27&]+)',r'\1\2***',text)
    return re.sub(r'OM-[A-Z0-9]{6,}', 'OM-******', text)


def local_url(value, protocols=('http','https','rtsp'), resolve=True):
    try:
        u = urlsplit(value)
        if u.scheme not in protocols or not u.hostname or u.fragment:
            raise ValueError()
        # Fixed literal addresses avoid a second DNS lookup redirecting camera credentials.
        addresses = [u.hostname]
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not (ip.is_private or ip.is_loopback or ip.is_link_local) or ip.is_unspecified or ip.is_multicast:
                raise ValueError()
        return value
    except (ValueError,OSError):
        raise HTTPException(422,'请输入你有权使用的摄像头局域网 IP 地址，例如 192.168.1.20；不支持公网地址或可变化的域名。')


class Sessions:
    def __init__(self, testing=False):
        self.tokens = {}
        self.pin = f'{secrets.randbelow(100000000):08d}'
        self.testing = testing
        self.failures = {}
        self.dev_origin=os.environ.get('OM_DEV_ORIGIN','')

    def allowed_host(self, host):
        """Accept local names/literal LAN addresses, never DNS-rebindable names.

        Validate before URL reconstruction as well as before WebSocket accept;
        reject duplicate Host fields at the transport boundary separately.
        """
        if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9.\[\]:-]+', host):
            return False
        try:
            parsed = urlsplit('http://' + host)
            if parsed.netloc != host or host.endswith(':'):
                return False
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                return False
            name = parsed.hostname
            if name == 'localhost' or (self.testing and name == 'testserver'):
                return True
            address = ipaddress.ip_address(name)
            return (address.is_loopback or address.is_private or address.is_link_local) and not (address.is_unspecified or address.is_multicast)
        except (ValueError, TypeError):
            return False

    def allowed_origin(self,origin,host,client_host,scheme=None):
        if not self.allowed_host(host):
            return False
        if not origin:
            return True  # Authenticated non-browser clients have no Origin.
        try:
            parsed=urlsplit(origin)
            if (parsed.scheme not in {'http','https'} or parsed.path or parsed.query or parsed.fragment
                    or not self.allowed_host(parsed.netloc) or origin != f'{parsed.scheme}://{parsed.netloc}'):
                return False
        except (ValueError, TypeError):
            return False
        if parsed.netloc==host and (scheme is None or parsed.scheme==scheme):
            return True
        return bool(self.dev_origin) and origin==self.dev_origin and self.dev_origin in {'http://127.0.0.1:5173','http://localhost:5173'} and client_host in {'127.0.0.1','::1'}

    def local(self, request):
        host = request.client.host if request.client else ''
        return host in {'127.0.0.1','::1'} or (self.testing and host == 'testclient')

    def issue(self):
        value=secrets.token_urlsafe(32)
        self.tokens[hashlib.sha256(value.encode()).hexdigest()]=time.time()+86400
        return value

    def valid(self, value):
        return bool(value) and self.tokens.get(hashlib.sha256(value.encode()).hexdigest(),0)>time.time()

    def verify_pin(self,host,pin):
        count,until=self.failures.get(host,(0,0))
        if until>time.time() and count>=5:
            raise HTTPException(429,'尝试次数过多，请一分钟后重试。')
        if not secrets.compare_digest(str(pin),self.pin):
            self.failures[host]=(count+1 if until>time.time() else 1,time.time()+60)
            raise HTTPException(401,'访问码不正确，请在运行物忆的电脑上查看。')
        self.failures.pop(host,None)

    async def websocket(self, ws:WebSocket, *, require_origin: bool = True):
        if len(ws.headers.getlist('host')) != 1 or not self.allowed_host(ws.headers.get('host')):
            await ws.close(code=4403)
            return False
        if not self.valid(ws.cookies.get('om_session')):
            await ws.close(code=4401)
            return False
        origin=ws.headers.get('origin')
        if require_origin and not origin:
            await ws.close(code=4403)
            return False
        if not self.allowed_origin(origin,ws.headers.get('host'),ws.client.host if ws.client else '',
                                   'https' if ws.scope.get('scheme')=='wss' else 'http'):
            await ws.close(code=4403)
            return False
        await ws.accept()
        return True


class LocalSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self,app,sessions):
        super().__init__(app)
        self.sessions=sessions

    async def dispatch(self,request,call_next):
        if len(request.headers.getlist('host')) != 1 or not self.sessions.allowed_host(request.headers.get('host')):
            return JSONResponse({'detail':'访问地址无效，请使用 localhost 或本机局域网 IP 地址。'},400)
        # Routing uses the ASGI path. Never make authorization depend on a URL
        # reconstructed from an untrusted HTTP header (including old Starlette).
        path=request.scope['path']
        origin=request.headers.get('origin')
        if request.method not in {'GET','HEAD','OPTIONS'} and not self.sessions.allowed_origin(origin,request.headers.get('host'),request.client.host if request.client else '',request.scope.get('scheme')):
            return JSONResponse({'detail':'请从物忆页面进行操作，跨站请求已阻止。'},403)
        public=path in {'/api/health','/api/session','/api/device-enrollment/claim'} or bool(re.fullmatch(r'/api/devices/[^/]+/heartbeat',path))
        if (path.startswith('/api/') or path.startswith('/media/')) and not public and not self.sessions.valid(request.cookies.get('om_session')):
            return JSONResponse({'detail':'请先在本机打开物忆，或输入本机显示的局域网访问码。'},401)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='same-origin'
        if path.startswith(('/api/','/media/')):
            response.headers['Cache-Control']='no-store'
        return response
