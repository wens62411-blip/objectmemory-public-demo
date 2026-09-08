#!/usr/bin/env python3
"""Real Windows acceptance for the explicitly authorized screen rectangle.

The test programmatically selects a visible OpenCV window owned by this test.
It does not claim a user's manual selection.  The exact Win32 client rectangle
is granted once through the production API and is the only desktop region read.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any, Callable
from uuid import uuid4

import cv2
import httpx
import numpy as np


ROOT = Path(__file__).resolve().parents[1]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def choose_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_until(predicate: Callable[[], Any], timeout: float, description: str) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.2)
    raise RuntimeError(f"等待{description}超时；最后结果={last!r}")


def enable_physical_pixel_coordinates() -> str:
    if os.name != "nt":
        return "unsupported"
    user32 = ctypes.windll.user32
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per_monitor_v2"
    except (AttributeError, OSError):
        pass
    try:
        if user32.SetProcessDPIAware():
            return "system_aware"
    except (AttributeError, OSError):
        pass
    # ERROR_ACCESS_DENIED means awareness was configured before this call.  The
    # coordinates are still queried and validated against the frame dimensions.
    return "already_configured_or_unavailable"


class Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def find_owned_window(pid: int, exact_title: str) -> int | None:
    user32 = ctypes.windll.user32
    matches: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def enum_window(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if int(window_pid.value) != pid:
            return True
        length = int(user32.GetWindowTextLengthW(hwnd))
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        if title.value == exact_title:
            matches.append(int(hwnd))
        return True

    user32.EnumWindows(enum_window, 0)
    return matches[0] if matches else None


def window_client_region(hwnd: int) -> dict[str, int]:
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError()
    top_left = Point(rect.left, rect.top)
    bottom_right = Point(rect.right, rect.bottom)
    if not user32.ClientToScreen(hwnd, ctypes.byref(top_left)):
        raise ctypes.WinError()
    if not user32.ClientToScreen(hwnd, ctypes.byref(bottom_right)):
        raise ctypes.WinError()
    return {
        "left": int(top_left.x),
        "top": int(top_left.y),
        "width": int(bottom_right.x - top_left.x),
        "height": int(bottom_right.y - top_left.y),
    }


def keep_window_visible(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    hwnd_topmost = wintypes.HWND(-1)
    swp_nomove, swp_nosize, swp_noactivate, swp_showwindow = 0x0002, 0x0001, 0x0010, 0x0040
    user32.SetWindowPos(
        hwnd,
        hwnd_topmost,
        0,
        0,
        0,
        0,
        swp_nomove | swp_nosize | swp_noactivate | swp_showwindow,
    )


def virtual_desktop() -> dict[str, int]:
    user32 = ctypes.windll.user32
    return {
        "left": int(user32.GetSystemMetrics(76)),
        "top": int(user32.GetSystemMetrics(77)),
        "width": int(user32.GetSystemMetrics(78)),
        "height": int(user32.GetSystemMetrics(79)),
    }


def validate_region_on_desktop(region: dict[str, int], desktop: dict[str, int]) -> None:
    if region["width"] < 32 or region["height"] < 32:
        raise RuntimeError(f"窗口客户区太小：{region}")
    if region["left"] < desktop["left"] or region["top"] < desktop["top"]:
        raise RuntimeError(f"窗口客户区超出虚拟桌面左上边界：{region}, desktop={desktop}")
    if region["left"] + region["width"] > desktop["left"] + desktop["width"]:
        raise RuntimeError(f"窗口客户区超出虚拟桌面右边界：{region}, desktop={desktop}")
    if region["top"] + region["height"] > desktop["top"] + desktop["height"]:
        raise RuntimeError(f"窗口客户区超出虚拟桌面下边界：{region}, desktop={desktop}")


def aruco_ids(frame: np.ndarray) -> list[int]:
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    if hasattr(cv2.aruco, "ArucoDetector"):
        ids = cv2.aruco.ArucoDetector(dictionary).detectMarkers(frame)[1]
    else:
        ids = cv2.aruco.detectMarkers(frame, dictionary)[1]
    return [] if ids is None else [int(value) for value in ids.flatten().tolist()]


def capture_exact_region(region: dict[str, int]) -> np.ndarray:
    import mss

    with mss.mss() as capture:
        shot = np.asarray(capture.grab(region))
    return cv2.cvtColor(shot, cv2.COLOR_BGRA2BGR)


def write_jpeg_unicode_path(path: Path, frame: np.ndarray) -> int:
    """Write through pathlib so Windows paths with Chinese text stay valid."""

    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise RuntimeError("OpenCV 无法编码授权区域源截图。")
    content = encoded.tobytes()
    path.write_bytes(content)
    return len(content)


def request_json(client: httpx.Client, method: str, path: str, **kwargs: Any) -> tuple[httpx.Response, Any]:
    response = client.request(method, path, **kwargs)
    if response.is_error:
        raise RuntimeError(f"{method} {path} 返回 {response.status_code}: {response.text[:800]}")
    return response, response.json()


def stop_process(process: subprocess.Popen[Any] | None, graceful_file: Path | None = None) -> bool:
    if process is None:
        return True
    if process.poll() is not None:
        return True
    if graceful_file is not None:
        try:
            graceful_file.write_text("stop\n", encoding="utf-8")
            process.wait(timeout=5)
            return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=4)
    return process.poll() is not None


def main() -> int:
    parser = argparse.ArgumentParser(description="物忆授权屏幕区域采集真实技术验收")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "diagnostics" / "authorized-screen-acceptance.json",
    )
    parser.add_argument("--timeout", type=float, default=75.0)
    args = parser.parse_args()

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    run_id = uuid4().hex[:10]
    data_dir = ROOT / "data" / "temporary" / f"authorized-screen-acceptance-{run_id}"
    data_dir.mkdir(parents=True, exist_ok=False)
    backend_log = output.parent / "authorized-screen-backend.log"
    player_log = output.parent / "authorized-screen-player.log"
    source_probe_path = output.parent / "authorized-screen-source-probe.jpg"
    annotated_preview_path = output.parent / "authorized-screen-preview.jpg"
    ready_file = data_dir / "player-ready.json"
    play_signal = data_dir / "begin-playback.signal"
    stop_signal = data_dir / "stop-player.signal"
    title = f"物忆 · 授权窗口采集技术验收 · {run_id}"
    video = ROOT / "demo" / "sample-videos" / "object-memory-demo.mp4"
    if not video.is_file():
        video = ROOT / "demo" / "sample-videos" / "object-memory-demo.avi"

    evidence: dict[str, Any] = {
        "test_type": "authorized_screen_region_technical_acceptance",
        "started_at": utc_now(),
        "passed": False,
        "stage": "initializing",
        "selection_method": "programmatic_visible_test_window",
        "user_hand_selection_claimed": False,
        "source_content": "SYNTHETIC_TEST_PLAYBACK",
        "privacy_boundary": {
            "authorized_region_only": True,
            "cookie_access_attempted": False,
            "browser_password_access_attempted": False,
            "authentication_bypass_attempted": False,
            "hidden_vendor_api_attempted": False,
        },
        "paths": {
            "data_dir": str(data_dir),
            "backend_log": str(backend_log),
            "player_log": str(player_log),
            "source_probe": str(source_probe_path),
            "annotated_preview": str(annotated_preview_path),
        },
    }
    player: subprocess.Popen[Any] | None = None
    backend: subprocess.Popen[Any] | None = None
    client: httpx.Client | None = None
    player_handle = None
    backend_handle = None
    camera_id: str | None = None
    hwnd: int | None = None
    window_process_pid: int | None = None
    exit_code = 1
    camera_stopped = False

    try:
        if os.name != "nt":
            raise RuntimeError("授权屏幕区域技术验收需要 Windows 桌面会话。")
        if not video.is_file():
            raise RuntimeError("MP4 和 AVI 测试回放都不存在，请先生成 Demo 素材。")
        evidence["stage"] = "starting_visible_player"
        evidence["dpi_awareness"] = enable_physical_pixel_coordinates()
        player_handle = player_log.open("w", encoding="utf-8", newline="\n")
        player = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "scripts" / "authorized_screen_player.py"),
                "--video",
                str(video),
                "--title",
                title,
                "--ready-file",
                str(ready_file),
                "--play-signal",
                str(play_signal),
                "--stop-signal",
                str(stop_signal),
            ],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=player_handle,
            stderr=subprocess.STDOUT,
            shell=False,
        )

        def visible_window() -> int | None:
            if player is not None and player.poll() is not None:
                raise RuntimeError(f"可见播放器提前退出：{player.returncode}")
            if not ready_file.is_file() or player is None:
                return None
            try:
                helper_pid = int(json.loads(ready_file.read_text(encoding="utf-8"))["pid"])
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                return None
            return find_owned_window(helper_pid, title)

        hwnd = int(wait_until(visible_window, 20, "可见 OpenCV 授权测试窗口"))
        keep_window_visible(hwnd)
        time.sleep(0.5)
        first_region = window_client_region(hwnd)
        time.sleep(0.25)
        region = window_client_region(hwnd)
        if first_region != region:
            raise RuntimeError(f"播放器客户区仍在变化：{first_region} -> {region}")
        desktop = virtual_desktop()
        validate_region_on_desktop(region, desktop)
        player_info = json.loads(ready_file.read_text(encoding="utf-8"))
        window_process_pid = int(player_info["pid"])
        expected_shape = (int(player_info["frame_height"]), int(player_info["frame_width"]))
        if (region["height"], region["width"]) != expected_shape:
            raise RuntimeError(
                "Win32 客户区与播放器帧尺寸不一致，不能证明像素边界精确："
                f"region={region}, frame={expected_shape}"
            )

        evidence["window"] = {
            "title": title,
            "launcher_pid": player.pid,
            "window_process_pid": window_process_pid,
            "visible": bool(ctypes.windll.user32.IsWindowVisible(hwnd)),
            "minimized": bool(ctypes.windll.user32.IsIconic(hwnd)),
            "client_region": region,
            "virtual_desktop": desktop,
            "frame_dimensions_match_client_region": True,
            "authorization_label": player_info.get("authorization_label"),
            "content_label": player_info.get("content_label"),
        }

        evidence["stage"] = "starting_isolated_backend"
        port = choose_port()
        base_url = f"http://127.0.0.1:{port}"
        environment = os.environ.copy()
        environment.update(
            {
                "OM_DATA_DIR": str(data_dir),
                "OM_PORT": str(port),
                # This acceptance uses the demo seed and a generated playback
                # window.  Keep it physically isolated from REAL data.
                "OM_RUNTIME_MODE": "DEMO",
                "OM_RUN_MODE": "DEMO",
                "PYTHONUTF8": "1",
            }
        )
        backend_handle = backend_log.open("w", encoding="utf-8", newline="\n")
        backend = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "scripts" / "serve.py"),
                "--port",
                str(port),
                "--mode",
                "DEMO",
                "--no-browser",
            ],
            cwd=str(ROOT),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=backend_handle,
            stderr=subprocess.STDOUT,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        health_client = httpx.Client(timeout=1.5, trust_env=False)

        def backend_ready() -> bool:
            if backend is not None and backend.poll() is not None:
                raise RuntimeError(f"隔离 FastAPI 提前退出：{backend.returncode}")
            try:
                response = health_client.get(base_url + "/api/health")
                return response.status_code == 200 and response.json().get("status") == "ok"
            except (httpx.HTTPError, ValueError):
                return False

        wait_until(backend_ready, 25, "隔离 FastAPI 健康检查")
        health_client.close()
        client = httpx.Client(base_url=base_url, timeout=20, trust_env=False)
        _, session = request_json(client, "GET", "/api/session")
        if not session.get("authenticated") or not session.get("local"):
            raise RuntimeError(f"本机会话没有获得屏幕授权资格：{session}")
        request_json(client, "POST", "/api/system/demo-seed")

        # Query the rectangle again immediately before authorization.  A moved
        # or resized window must invalidate this acceptance rather than silently
        # widening the capture boundary.
        latest_region = window_client_region(hwnd)
        if latest_region != region:
            raise RuntimeError(f"授权前窗口客户区已变化：{region} -> {latest_region}")

        evidence["stage"] = "authorizing_exact_rectangle"
        authorize_response, authorization = request_json(
            client, "POST", "/api/screen-capture/authorize", json={"region": region}
        )
        grant = authorization.get("capture_grant")
        if not isinstance(grant, str) or len(grant) < 16 or authorization.get("region") != region:
            raise RuntimeError("后端没有为相同矩形签发有效的一次性授权。")
        evidence["authorization"] = {
            "http_status": authorize_response.status_code,
            "local_session": True,
            "grant_returned": True,
            "grant_value_recorded": False,
            "expires_in_seconds": authorization.get("expires_in"),
            "authorized_region": region,
        }

        # Pixel access begins only after the production API has authorized the
        # exact rectangle.  The probe uses the same dictionary passed to MSS by
        # AuthorizedScreenCaptureSource and never expands it to a whole monitor.
        evidence["stage"] = "probing_exact_authorized_region"
        source_probe = capture_exact_region(region)
        if source_probe.shape[:2] != expected_shape:
            raise RuntimeError(f"MSS 返回尺寸不等于授权区域：{source_probe.shape[:2]} != {expected_shape}")
        ids = aruco_ids(source_probe)
        if 1 not in ids:
            raise RuntimeError(f"授权矩形的真实桌面截图未检测到 ArUco 001；实际 IDs={ids}")
        source_probe_bytes = write_jpeg_unicode_path(source_probe_path, source_probe)
        evidence["window"].update(
            {
                "source_probe_shape": [int(source_probe.shape[1]), int(source_probe.shape[0])],
                "source_probe_jpeg_bytes": source_probe_bytes,
                "source_probe_aruco_ids": ids,
                "pixel_capture_started_after_api_authorization": True,
            }
        )

        evidence["stage"] = "creating_screen_camera_and_zones"
        _, camera = request_json(
            client,
            "POST",
            "/api/cameras",
            json={
                "name": "授权窗口采集 · 技术验收",
                "room_name": "客厅",
                "installation": "程序化选择本机可见 OpenCV 测试窗口",
                "source_type": "screen",
                "source": "authorized-visible-test-window",
                "config": {
                    "authorized": True,
                    "region": region,
                    "capture_grant": grant,
                    "simulated": True,
                    "content_label": "测试回放",
                    "selection_method": "programmatic_visible_test_window",
                },
                "enabled": True,
                "inference_fps": 5,
                "save_clips": True,
            },
        )
        camera_id = str(camera["id"])
        if camera.get("source_type") != "screen":
            raise RuntimeError(f"创建后的摄像头来源不是 screen：{camera.get('source_type')}")
        stored_config = camera.get("config") or {}
        if stored_config.get("region") != region or "capture_grant" in stored_config:
            raise RuntimeError("一次性授权未按相同矩形消费，或授权值被错误持久化。")
        _, desk = request_json(
            client,
            "POST",
            f"/api/cameras/{camera_id}/zones",
            json={
                "name": "桌面",
                "points": [[0.0, 0.12], [0.46, 0.12], [0.46, 0.90], [0.0, 0.90]],
                "priority": 10,
                "enabled": True,
            },
        )
        _, sofa = request_json(
            client,
            "POST",
            f"/api/cameras/{camera_id}/zones",
            json={
                "name": "沙发右侧",
                "points": [[0.50, 0.12], [1.0, 0.12], [1.0, 0.90], [0.50, 0.90]],
                "priority": 10,
                "enabled": True,
            },
        )

        evidence["stage"] = "confirming_real_screen_frames"
        request_json(client, "POST", f"/api/cameras/{camera_id}/start")

        def camera_ready() -> dict[str, Any] | None:
            _, payload = request_json(client, "GET", f"/api/cameras/{camera_id}")
            health = payload.get("health") or {}
            if health.get("status") == "error":
                raise RuntimeError(f"屏幕采集引擎报错：{health.get('error')}")
            if health.get("status") == "ready" and int(health.get("processed_frames") or 0) >= 4:
                return payload
            return None

        live_camera = wait_until(camera_ready, 20, "授权区域真实画面和视觉引擎")
        frame_response = client.get(f"/api/cameras/{camera_id}/frame")
        frame_response.raise_for_status()
        if not frame_response.content.startswith(b"\xff\xd8"):
            raise RuntimeError("视觉引擎返回的并非 JPEG 画面。")
        annotated_preview_path.write_bytes(frame_response.content)
        annotated = cv2.imdecode(np.frombuffer(frame_response.content, np.uint8), cv2.IMREAD_COLOR)
        if annotated is None or annotated.shape[:2] != expected_shape:
            raise RuntimeError("视觉预览无法解码或尺寸不等于授权区域。")
        # The production engine draws an orange boundary around screen sources.
        orange = (
            (annotated[:, :, 0] < 120)
            & (annotated[:, :, 1] > 90)
            & (annotated[:, :, 1] < 210)
            & (annotated[:, :, 2] > 175)
        )
        orange_pixels = int(np.count_nonzero(orange))
        if orange_pixels < 500:
            raise RuntimeError(f"视觉预览缺少授权区域醒目标记；橙色像素={orange_pixels}")

        # Start the synthetic movement only after screen capture is stable.
        playback_started_at = utc_now()
        play_signal.write_text("begin\n", encoding="utf-8")
        evidence["stage"] = "waiting_for_demo_vision_event"

        def confirmed_movement_event() -> dict[str, Any] | None:
            _, rows = request_json(
                client,
                "GET",
                "/api/events",
                params={"camera_id": camera_id, "event_type": "movement", "limit": 100},
            )
            threshold = parse_time(playback_started_at)
            for row in rows:
                if (
                    row.get("item_name") == "我的手机"
                    and (row.get("to_zone") or row.get("zone_name")) == "沙发右侧"
                    and row.get("evidence_status") == "confirmed"
                    and row.get("final_status") == "confirmed_placed"
                    and parse_time(str(row["timestamp_start"])) >= threshold
                ):
                    return row
            return None

        event = wait_until(confirmed_movement_event, args.timeout, "授权窗口采集驱动的新 confirmed movement 事件")
        screenshot_url = event.get("screenshot_path")
        clip_url = event.get("clip_path")
        if not screenshot_url or not clip_url:
            raise RuntimeError(f"confirmed movement 事件缺少截图或录像：{event}")
        screenshot_response = client.get(str(screenshot_url))
        clip_response = client.get(str(clip_url))
        screenshot_response.raise_for_status()
        clip_response.raise_for_status()
        if not screenshot_response.content.startswith(b"\xff\xd8") or len(screenshot_response.content) < 1000:
            raise RuntimeError("事件截图不是有效的 JPEG 证据。")
        if len(clip_response.content) < 1024:
            raise RuntimeError("事件录像为空或过小。")

        _, search = request_json(client, "POST", "/api/search", json={"query": "我的手机在哪里"})
        results = search.get("results") or []
        if not results:
            raise RuntimeError("搜索没有返回我的手机。")
        confirmed = results[0].get("last_confirmed") or {}
        same_event = (
            confirmed.get("camera_id") == camera_id
            and confirmed.get("zone_name") == "沙发右侧"
            and parse_time(str(confirmed["timestamp_start"])) >= parse_time(str(event["timestamp_start"]))
        )
        if not same_event:
            raise RuntimeError(f"搜索结果未引用本次或更新的放置事件：{confirmed}")

        evidence.update(
            {
                "passed": True,
                "stage": "completed",
                "backend": {"url": base_url, "isolated_data_dir": str(data_dir)},
                "camera": {
                    "id": camera_id,
                    "name": camera.get("name"),
                    "source_type": camera.get("source_type"),
                    "stored_region_matches_authorization": True,
                    "one_time_grant_not_persisted": True,
                    "simulated_content_disclosed": bool(stored_config.get("simulated")),
                    "health": live_camera.get("health"),
                    "frame_jpeg_bytes": len(frame_response.content),
                    "annotated_frame_dimensions": [int(annotated.shape[1]), int(annotated.shape[0])],
                    "authorized_border_pixels": orange_pixels,
                    "overlay_label": "AUTHORIZED SCREEN REGION",
                    "interface_label": "授权窗口采集",
                },
                "zones": [
                    {"id": desk.get("id"), "name": desk.get("name")},
                    {"id": sofa.get("id"), "name": sofa.get("name")},
                ],
                "playback_started_at": playback_started_at,
                "vision_event": {
                    "id": event.get("id") or event.get("event_id"),
                    "item_id": event.get("item_id"),
                    "item_name": event.get("item_name"),
                    "event_type": event.get("event_type"),
                    "camera_id": event.get("camera_id"),
                    "zone_name": event.get("zone_name"),
                    "previous_zone": event.get("previous_zone"),
                    "timestamp_start": event.get("timestamp_start"),
                    "confidence": event.get("confidence"),
                    "evidence_type": event.get("evidence_type"),
                    "screenshot_path": screenshot_url,
                    "screenshot_bytes": len(screenshot_response.content),
                    "clip_path": clip_url,
                    "clip_bytes": len(clip_response.content),
                },
                "search": {
                    "query": search.get("query"),
                    "last_confirmed_camera_id": confirmed.get("camera_id"),
                    "last_confirmed_zone": confirmed.get("zone_name"),
                    "last_confirmed_timestamp": confirmed.get("timestamp_start"),
                    "same_or_newer_event": True,
                },
                "completed_at": utc_now(),
            }
        )
        exit_code = 0
    except Exception as exc:
        evidence.update(
            {
                "passed": False,
                "failed_stage": evidence.get("stage"),
                "error": repr(exc),
                "completed_at": utc_now(),
            }
        )
        print(json.dumps(evidence, ensure_ascii=False, indent=2), file=sys.stderr)
        exit_code = 1
    finally:
        if client is not None and camera_id is not None:
            try:
                stop_response = client.post(f"/api/cameras/{camera_id}/stop", timeout=15)
                camera_stopped = stop_response.status_code == 200 and (
                    (stop_response.json().get("health") or {}).get("status") == "stopped"
                )
            except (httpx.HTTPError, ValueError):
                camera_stopped = False
        if client is not None:
            client.close()
        backend_stopped = stop_process(backend)
        player_stopped = stop_process(player, stop_signal)
        if backend_handle is not None:
            backend_handle.close()
        if player_handle is not None:
            player_handle.close()
        window_closed = True
        if window_process_pid is not None:
            window_closed = find_owned_window(window_process_pid, title) is None
        cleanup = {
            "camera_stopped": camera_stopped if camera_id is not None else True,
            "backend_process_stopped": backend_stopped,
            "player_process_stopped": player_stopped,
            "visible_window_closed": window_closed,
        }
        evidence["cleanup"] = cleanup
        if evidence.get("passed") and not all(cleanup.values()):
            evidence["passed"] = False
            evidence["error"] = "验收主体通过，但没有完整释放摄像头、后端或可见窗口。"
            exit_code = 1
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    if evidence.get("passed"):
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
