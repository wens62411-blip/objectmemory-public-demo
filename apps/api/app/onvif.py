"""Explicit, bounded local WS-Discovery and standard ONVIF SOAP only."""
import socket
from urllib.parse import urlsplit
from uuid import uuid4
from xml.etree import ElementTree as ET

import httpx
from fastapi import HTTPException
from .security import local_url

SOAP='http://www.w3.org/2003/05/soap-envelope'


def discover(timeout=2.0):
    probe=f'''<?xml version="1.0"?><e:Envelope xmlns:e="{SOAP}" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><e:Header><w:MessageID>uuid:{uuid4()}</w:MessageID><w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To><w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>'''
    import time
    results={}
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.IPPROTO_IP,socket.IP_MULTICAST_TTL,1)
        sock.settimeout(.25)
        sock.sendto(probe.encode(),('239.255.255.250',3702))
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            try:
                data,addr=sock.recvfrom(65535)
                root=ET.fromstring(data)
                for node in root.iter():
                    if node.tag.endswith('XAddrs') and node.text:
                        for url in node.text.split():
                            try:local_url(url)
                            except HTTPException:continue
                            results[url]={'address':url,'ip':addr[0],'name':'发现的局域网 ONVIF 摄像头'}
            except (socket.timeout,ET.ParseError):pass
    return list(results.values())


def get_stream_uri(address,username,password):
    local_url(address,('http','https'))
    # Credentials stay inside this request and the resulting local camera record.
    client=httpx.Client(auth=httpx.DigestAuth(username,password),timeout=5,trust_env=False,follow_redirects=False)
    def soap(url,action,body):
        local_url(url,('http','https'))
        xml=f'<s:Envelope xmlns:s="{SOAP}"><s:Body>{body}</s:Body></s:Envelope>'
        response=client.post(url,content=xml.encode(),headers={'Content-Type':f'application/soap+xml; charset=utf-8; action="{action}"'})
        if response.status_code in {401,403}:raise HTTPException(400,'摄像头拒绝了账号，请检查你有权使用的管理员账号和 ONVIF 权限。')
        response.raise_for_status()
        return ET.fromstring(response.content)
    try:
        result=soap(address,'http://www.onvif.org/ver10/device/wsdl/GetCapabilities','<tds:GetCapabilities xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><tds:Category>Media</tds:Category></tds:GetCapabilities>')
        media=None
        for node in result.iter():
            if node.tag.endswith('Media'):
                media=next((n.text for n in node.iter() if n.tag.endswith('XAddr')),None)
        if not media:raise HTTPException(400,'该设备未返回标准 ONVIF Media 服务地址，可尝试 RTSP 或授权区域采集。')
        profiles=soap(media,'http://www.onvif.org/ver10/media/wsdl/GetProfiles','<trt:GetProfiles xmlns:trt="http://www.onvif.org/ver10/media/wsdl"/>')
        token=next((n.attrib['token'] for n in profiles.iter() if n.tag.endswith('Profiles') and n.attrib.get('token')),None)
        if not token:raise HTTPException(400,'该设备没有可用的 ONVIF 视频配置。')
        from xml.sax.saxutils import escape
        result=soap(media,'http://www.onvif.org/ver10/media/wsdl/GetStreamUri',f'<trt:GetStreamUri xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema"><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup><trt:ProfileToken>{escape(token)}</trt:ProfileToken></trt:GetStreamUri>')
        uri=next((n.text for n in result.iter() if n.tag.endswith('Uri')),None)
        if not uri:raise HTTPException(400,'摄像头没有返回标准视频流地址。')
        local_url(uri)
        return uri
    except (httpx.HTTPError,ET.ParseError):raise HTTPException(400,'ONVIF 标准接口连接失败。请确认设备开启 ONVIF，或改用已知 RTSP 地址。')
    finally:client.close()
