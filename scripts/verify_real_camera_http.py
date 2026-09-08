from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import cv2
import httpx
import numpy as np
from websockets.sync.client import connect as websocket_connect


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _ws_url(base_url: str, path: str) -> str:
    parsed = urlsplit(base_url)
    return urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc, path, "", ""))


def _cookie_header(client: httpx.Client) -> str:
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in client.cookies.jar)


def _mjpeg_frames(client: httpx.Client, path: str, target: int = 10) -> list[str]:
    hashes: list[str] = []
    buffer = bytearray()
    with client.stream("GET", path, timeout=httpx.Timeout(10, read=10)) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "multipart/x-mixed-replace" not in content_type:
            raise RuntimeError(f"MJPEG响应类型错误：{content_type}")
        for chunk in response.iter_bytes():
            buffer.extend(chunk)
            while True:
                start = buffer.find(b"\xff\xd8")
                end = buffer.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                if start < 0 or end < 0:
                    if len(buffer) > 4 * 1024 * 1024:
                        del buffer[:-1024]
                    break
                payload = bytes(buffer[start : end + 2])
                del buffer[: end + 2]
                image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
                if image is None:
                    raise RuntimeError("MJPEG包含无法解码的JPEG帧")
                hashes.append(hashlib.sha256(payload).hexdigest())
                if len(hashes) >= target:
                    return hashes
    return hashes


def run(base_url: str, duration: float, output: Path, camera_id: str | None = None, keep_camera: bool = False) -> dict:
    base_url = base_url.rstrip("/")
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "test_kind": "REAL_FASTAPI_CAMERA_CHAIN",
        "simulated": False,
        "base_url": base_url,
        "camera_id": camera_id,
        "device_index": 0,
        "test_endpoint_released": False,
        "test_snapshot_bytes": 0,
        "mjpeg_frames": 0,
        "mjpeg_unique_hashes": 0,
        "frame_samples": 0,
        "frame_unique_hashes": 0,
        "camera_ws_messages": 0,
        "camera_ws_statuses": [],
        "backend_http_chain_verified": False,
        "frontend_dom_verified": False,
        "stop_released": False,
        "restart_success": False,
        "error": None,
        "success": False,
    }
    created = False
    client = httpx.Client(base_url=base_url, timeout=15, follow_redirects=False)
    try:
        health = client.get("/api/health")
        health.raise_for_status()
        session = client.get("/api/session")
        session.raise_for_status()
        cameras = client.get("/api/cameras").json()
        if camera_id is None:
            existing = next((item for item in cameras if item.get("source_type") == "webcam" and str(item.get("source")) == "0"), None)
            if existing:
                camera_id = existing["id"]
            else:
                response = client.post(
                    "/api/cameras",
                    json={"name": "真实摄像头HTTP验收", "room_name": "验收房间", "source_type": "webcam", "source": "0", "config": {"index": 0}},
                )
                response.raise_for_status()
                camera_id = response.json()["id"]
                created = True
        report["camera_id"] = camera_id
        client.post(f"/api/cameras/{camera_id}/stop").raise_for_status()

        test_result = client.post(f"/api/cameras/{camera_id}/test")
        test_result.raise_for_status()
        test_payload = test_result.json()
        if not test_payload.get("success"):
            raise RuntimeError(test_payload.get("message") or "连接测试没有读到真实帧")
        after_test = client.get(f"/api/cameras/{camera_id}").json()
        report["test_endpoint_released"] = after_test.get("health", {}).get("status_code") == "STOPPED"
        snapshot = client.get(test_payload["frame_url"])
        snapshot.raise_for_status()
        report["test_snapshot_bytes"] = len(snapshot.content)

        started = client.post(f"/api/cameras/{camera_id}/start")
        started.raise_for_status()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = client.get(f"/api/cameras/{camera_id}").json()
            if current.get("health", {}).get("status") == "ready":
                report["start_health"] = current["health"]
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("正式启动后10秒内没有进入ready")

        mjpeg_hashes = _mjpeg_frames(client, f"/api/cameras/{camera_id}/stream", 10)
        report["mjpeg_frames"] = len(mjpeg_hashes)
        report["mjpeg_unique_hashes"] = len(set(mjpeg_hashes))

        cookie = _cookie_header(client)
        ws_values = []
        with websocket_connect(
            _ws_url(base_url, f"/ws/cameras/{camera_id}"),
            origin=base_url,
            additional_headers={"Cookie": cookie},
            open_timeout=5,
            close_timeout=2,
        ) as websocket:
            for _ in range(3):
                value = json.loads(websocket.recv(timeout=5))
                ws_values.append(value)
        report["camera_ws_messages"] = len(ws_values)
        report["camera_ws_statuses"] = [value.get("camera", {}).get("health", {}).get("status") for value in ws_values]

        hashes = set()
        sample_started = time.monotonic()
        while time.monotonic() - sample_started < duration:
            frame = client.get(f"/api/cameras/{camera_id}/frame", timeout=5)
            if frame.status_code == 200:
                report["frame_samples"] += 1
                hashes.add(hashlib.sha256(frame.content).hexdigest())
            time.sleep(0.45)
        report["frame_unique_hashes"] = len(hashes)
        report["final_health"] = client.get(f"/api/cameras/{camera_id}").json().get("health")
        report["backend_http_chain_verified"] = bool(
            report["test_endpoint_released"]
            and report["mjpeg_frames"] >= 10
            and report["mjpeg_unique_hashes"] >= 2
            and report["frame_samples"] >= max(5, int(duration))
            and report["frame_unique_hashes"] >= 2
            and report["camera_ws_messages"] >= 3
            and all(status == "ready" for status in report["camera_ws_statuses"])
        )

        client.post(f"/api/cameras/{camera_id}/stop").raise_for_status()
        stopped = client.get(f"/api/cameras/{camera_id}").json().get("health", {})
        report["stop_released"] = stopped.get("status_code") == "STOPPED" and client.get(f"/api/cameras/{camera_id}/frame").status_code == 503
        restarted = client.post(f"/api/cameras/{camera_id}/start")
        restarted.raise_for_status()
        restart_deadline = time.monotonic() + 10
        while time.monotonic() < restart_deadline:
            if client.get(f"/api/cameras/{camera_id}/frame").status_code == 200:
                report["restart_success"] = True
                break
            time.sleep(0.2)
        client.post(f"/api/cameras/{camera_id}/stop").raise_for_status()
        report["success"] = bool(report["backend_http_chain_verified"] and report["stop_released"] and report["restart_success"])
    except Exception as exc:
        report["error"] = str(exc)
        if camera_id:
            try:
                client.post(f"/api/cameras/{camera_id}/stop")
            except Exception:
                pass
    finally:
        if created and camera_id and not keep_camera:
            try:
                client.delete(f"/api/cameras/{camera_id}")
            except Exception:
                pass
        client.close()
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="通过真实FastAPI验证电脑摄像头HTTP/MJPEG/WebSocket链路")
    parser.add_argument("--url", default="http://127.0.0.1:8018")
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--camera-id")
    parser.add_argument("--keep-camera", action="store_true")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "diagnostics" / "real-camera-api-acceptance.json")
    args = parser.parse_args()
    if args.duration < 5 or args.duration > 3600:
        parser.error("duration必须在5到3600秒之间")
    result = run(args.url, args.duration, args.output.resolve(), args.camera_id, args.keep_camera)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
