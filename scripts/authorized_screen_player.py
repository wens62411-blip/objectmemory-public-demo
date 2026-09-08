#!/usr/bin/env python3
"""Visible OpenCV player used by the authorized-region acceptance test.

This helper deliberately creates a normal, topmost desktop window.  The
acceptance runner obtains its *client* rectangle through Win32 and authorizes
only those pixels.  Playback waits on a small start file so the vision state
machine sees a stable desk position before the synthetic movement begins.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from pathlib import Path

import cv2


def enable_physical_pixel_coordinates() -> None:
    """Make Win32 client coordinates agree with MSS on scaled displays."""

    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    try:
        # PER_MONITOR_AWARE_V2.  It can fail if another library configured DPI
        # awareness first; SetProcessDPIAware remains a safe fallback.
        if not user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        try:
            user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def apply_native_window_title(display_title: str) -> int | None:
    """Set the real HWND title with the Unicode Win32 API.

    OpenCV's Windows backend treats its window-name bytes using the active code
    page on some builds, which garbles Chinese.  We retain an ASCII OpenCV key
    internally and set the user-visible title through SetWindowTextW.
    """

    if os.name != "nt":
        return None
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found: list[int] = []

    @callback_type
    def enum_window(hwnd: int, _lparam: int) -> bool:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) == os.getpid() and user32.IsWindowVisible(hwnd):
            found.append(int(hwnd))
        return True

    user32.EnumWindows(enum_window, 0)
    if not found:
        return None
    hwnd = found[0]
    if not user32.SetWindowTextW(hwnd, display_title):
        raise ctypes.WinError()
    user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0010 | 0x0040)
    return hwnd


def main() -> int:
    parser = argparse.ArgumentParser(description="物忆授权窗口采集验收播放器")
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--title", required=True)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--play-signal", required=True, type=Path)
    parser.add_argument("--stop-signal", required=True, type=Path)
    parser.add_argument("--left", type=int, default=72)
    parser.add_argument("--top", type=int, default=72)
    args = parser.parse_args()

    enable_physical_pixel_coordinates()
    video = args.video.resolve()
    if not video.is_file():
        print(f"测试回放不存在：{video}", file=sys.stderr, flush=True)
        return 2

    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        print(f"无法打开测试回放：{video}", file=sys.stderr, flush=True)
        return 3
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 10.0)
    if not 1.0 <= fps <= 60.0:
        fps = 10.0
    ok, first_frame = capture.read()
    if not ok or first_frame is None:
        capture.release()
        print("测试回放没有可读取的视频帧。", file=sys.stderr, flush=True)
        return 4

    args.ready_file.parent.mkdir(parents=True, exist_ok=True)
    window_key = f"ObjectMemoryAuthorizedCapture-{os.getpid()}"
    cv2.namedWindow(window_key, cv2.WINDOW_AUTOSIZE)
    cv2.moveWindow(window_key, args.left, args.top)
    frame_interval = 1.0 / fps
    playing = False
    next_frame_at = time.monotonic()
    ready_written = False

    try:
        while not args.stop_signal.exists():
            if not playing:
                frame = first_frame.copy()
                if args.play_signal.exists():
                    playing = True
                    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    next_frame_at = time.monotonic()
            else:
                ok, frame = capture.read()
                if not ok or frame is None:
                    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, frame = capture.read()
                    if not ok or frame is None:
                        raise RuntimeError("循环回放时无法重新读取第一帧")

            # The title bar carries the Chinese authorization label.  A thin
            # in-frame border makes the selected content obvious without
            # covering the ArUco marker that drives the acceptance event.
            display = frame.copy()
            cv2.rectangle(
                display,
                (2, 2),
                (display.shape[1] - 3, display.shape[0] - 3),
                (36, 155, 244),
                3,
            )
            cv2.imshow(window_key, display)
            try:
                cv2.setWindowProperty(window_key, cv2.WND_PROP_TOPMOST, 1)
            except cv2.error:
                pass

            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q")):
                break
            if not ready_written:
                hwnd = apply_native_window_title(args.title)
                if hwnd is None:
                    raise RuntimeError("OpenCV 窗口已创建，但 Win32 未找到其可见 HWND")
                args.ready_file.write_text(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "window_title": args.title,
                            "video": str(video),
                            "frame_width": int(display.shape[1]),
                            "frame_height": int(display.shape[0]),
                            "fps": fps,
                            "content_label": "SYNTHETIC_TEST_PLAYBACK",
                            "authorization_label": "授权窗口采集",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                ready_written = True

            if playing:
                next_frame_at += frame_interval
                delay = next_frame_at - time.monotonic()
                if delay > 0:
                    time.sleep(min(delay, frame_interval))
                elif delay < -1.0:
                    # Do not try to replay a large backlog after the desktop
                    # session stalls or the window is temporarily blocked.
                    next_frame_at = time.monotonic()
            else:
                time.sleep(0.04)
    finally:
        capture.release()
        try:
            cv2.destroyWindow(window_key)
            cv2.waitKey(1)
        except cv2.error:
            cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
