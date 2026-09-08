from .base import CameraSourceAdapter, redact_value
from .network import Esp32CamSource, MjpegHttpSource, OnvifSource, RtspSource
from .opencv_stream import VideoFileSource, WebcamSource
from .screen import AuthorizedScreenCaptureSource
from .browser import BrowserCameraSource


def create_camera_source(camera: dict):
    source_type = str(camera.get("source_type", "webcam")).lower()
    source = camera.get("source", 0)
    config = {**(camera.get("config") or {}), "camera_id": camera.get("id")}
    if camera.get("id"):
        config.setdefault("owner", f"camera:{camera['id']}")
    classes = {
        "webcam": WebcamSource,
        "video": VideoFileSource,
        "esp32": Esp32CamSource,
        "mjpeg": MjpegHttpSource,
        "rtsp": RtspSource,
        "onvif": OnvifSource,
        "screen": AuthorizedScreenCaptureSource,
        "browser": BrowserCameraSource,
    }
    if source_type not in classes:
        raise ValueError(f"不支持的摄像头来源：{source_type}")
    return classes[source_type](source, config)


__all__ = [
    "CameraSourceAdapter",
    "WebcamSource",
    "VideoFileSource",
    "Esp32CamSource",
    "MjpegHttpSource",
    "RtspSource",
    "OnvifSource",
    "AuthorizedScreenCaptureSource",
    "BrowserCameraSource",
    "create_camera_source",
    "redact_value",
]
