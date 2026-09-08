#!/usr/bin/env python3
"""Generate deterministic, clearly-labelled ObjectMemory demo assets.

Outputs are generated locally and contain no captured household imagery. The
AVI is a synthetic *test playback* that drives the same ArUco/state-machine
pipeline as a real camera; it must never be presented as live video.
"""

from __future__ import annotations

import argparse
import base64
import os
import subprocess
from pathlib import Path

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
MARKER_DIR = ROOT / "demo" / "markers"
VIDEO_DIR = ROOT / "demo" / "sample-videos"
SEED_DIR = ROOT / "demo" / "seed-data"


ITEMS = {
    1: "我的手机",
    2: "我的钥匙",
    3: "蓝色钱包",
}


def generate_marker(marker_id: int, pixels: int = 720) -> Path:
    if not hasattr(cv2, "aruco"):
        raise RuntimeError("需要 opencv-contrib-python 才能生成 ArUco 标签")
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    marker = cv2.aruco.generateImageMarker(dictionary, marker_id, pixels)
    canvas = np.full((pixels + 120, pixels + 120), 255, dtype=np.uint8)
    canvas[60 : 60 + pixels, 60 : 60 + pixels] = marker
    path = MARKER_DIR / f"aruco-{marker_id:03d}.png"
    ok, encoded = cv2.imencode(".png", canvas)
    if not ok:
        raise RuntimeError(f"标签 {marker_id} PNG 编码失败")
    path.write_bytes(encoded.tobytes())
    return path


def generate_a4_svg(marker_paths: dict[int, Path]) -> Path:
    # 210 x 297 mm, three 58 mm markers with generous white quiet zones.
    x_positions = [12, 76, 140]
    images: list[str] = []
    for marker_id, x in zip(sorted(marker_paths), x_positions):
        encoded = base64.b64encode(marker_paths[marker_id].read_bytes()).decode("ascii")
        label = ITEMS[marker_id]
        images.append(
            f'<rect x="{x}" y="35" width="58" height="73" rx="2" fill="white" stroke="#ccd6dc"/>'
            f'<image x="{x + 4}" y="39" width="50" height="50" href="data:image/png;base64,{encoded}"/>'
            f'<text x="{x + 29}" y="96" text-anchor="middle" font-size="5" font-family="Microsoft YaHei, sans-serif">{marker_id:03d} · {label}</text>'
            f'<text x="{x + 29}" y="103" text-anchor="middle" font-size="3.3" fill="#52636d" font-family="Microsoft YaHei, sans-serif">DICT_4X4_50 · 稳定标签模式</text>'
        )
    svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" viewBox="0 0 210 297">
  <rect width="210" height="297" fill="white"/>
  <text x="105" y="18" text-anchor="middle" font-size="8" font-weight="bold" font-family="Microsoft YaHei, sans-serif">物忆 ObjectMemory 演示标签</text>
  <text x="105" y="27" text-anchor="middle" font-size="4" fill="#52636d" font-family="Microsoft YaHei, sans-serif">按 100% 比例打印，请保留标签四周白边</text>
  {''.join(images)}
  <line x1="12" y1="122" x2="198" y2="122" stroke="#dde4e8"/>
  <text x="12" y="133" font-size="4.2" font-family="Microsoft YaHei, sans-serif">演示映射</text>
  <text x="12" y="142" font-size="3.8" font-family="Microsoft YaHei, sans-serif">001 = 我的手机　　002 = 我的钥匙　　003 = 蓝色钱包</text>
  <text x="12" y="154" font-size="3.6" fill="#52636d" font-family="Microsoft YaHei, sans-serif">本页标签用于早期验证和现场稳定演示。请勿把测试回放描述为实时摄像头。</text>
</svg>'''
    path = MARKER_DIR / "aruco-markers-a4.svg"
    path.write_text(svg, encoding="utf-8")
    return path


def _small_marker(marker_id: int, size: int = 104) -> np.ndarray:
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    core = cv2.aruco.generateImageMarker(dictionary, marker_id, size)
    bordered = cv2.copyMakeBorder(core, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)
    return cv2.cvtColor(bordered, cv2.COLOR_GRAY2BGR)


def _ease(value: float) -> float:
    value = float(np.clip(value, 0.0, 1.0))
    return value * value * (3.0 - 2.0 * value)


def generate_demo_video(path: Path, fps: int = 10, seconds: float = 14.0) -> Path:
    width, height = 640, 480
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError(f"无法创建 AVI：{path}")
    marker = _small_marker(1)
    marker_h, marker_w = marker.shape[:2]
    total = round(fps * seconds)
    start_center = np.array([150.0, 292.0])  # x=.234, desk
    end_center = np.array([485.0, 292.0])    # x=.758, sofa-right
    try:
        for frame_index in range(total):
            t = frame_index / fps
            frame = np.full((height, width, 3), (244, 247, 248), dtype=np.uint8)
            # Deliberately simple, high-contrast scene for CPU-safe detection.
            cv2.rectangle(frame, (18, 96), (292, 438), (222, 236, 230), -1)
            cv2.rectangle(frame, (320, 96), (622, 438), (229, 224, 242), -1)
            cv2.line(frame, (307, 82), (307, 450), (185, 195, 201), 2)
            cv2.putText(frame, "ZONE 1: DESK", (42, 128), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (55, 99, 78), 2, cv2.LINE_AA)
            cv2.putText(frame, "ZONE 2: SOFA RIGHT", (342, 128), cv2.FONT_HERSHEY_SIMPLEX, 0.68, (92, 70, 122), 2, cv2.LINE_AA)
            cv2.putText(frame, "SYNTHETIC TEST PLAYBACK - NOT A LIVE CAMERA", (62, 37), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (34, 84, 110), 2, cv2.LINE_AA)
            cv2.putText(frame, "ArUco 001 = demo phone", (194, 66), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (70, 80, 86), 1, cv2.LINE_AA)

            if t < 3.5:
                center = start_center.copy()
            elif t < 6.0:
                center = start_center + (end_center - start_center) * _ease((t - 3.5) / 2.5)
            else:
                center = end_center.copy()

            # A visibly synthetic hand cue approaches before movement and leaves
            # after placement. It is only visual context; P0 event tests do not
            # claim it was recognized as a biological hand.
            if 2.2 <= t < 6.8:
                if t < 3.2:
                    hand_x = int(30 + (center[0] - 88) * _ease((t - 2.2) / 1.0))
                elif t < 6.0:
                    hand_x = int(center[0] - 64)
                else:
                    hand_x = int(center[0] - 64 + 170 * _ease((t - 6.0) / 0.8))
                hand_y = int(center[1] + 68)
                cv2.ellipse(frame, (hand_x, hand_y), (54, 27), -8, 0, 360, (166, 202, 244), -1, cv2.LINE_AA)
                cv2.putText(frame, "SYNTHETIC HAND CUE", (max(8, hand_x - 84), min(height - 8, hand_y + 45)), cv2.FONT_HERSHEY_SIMPLEX, 0.39, (85, 102, 126), 1, cv2.LINE_AA)

            x = int(center[0] - marker_w / 2)
            y = int(center[1] - marker_h / 2)
            frame[y : y + marker_h, x : x + marker_w] = marker
            phase = "STATIC ON DESK" if t < 3.5 else ("MOVING" if t < 6.0 else "STATIC ON SOFA RIGHT")
            cv2.putText(frame, phase, (210, 465), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (39, 67, 78), 2, cv2.LINE_AA)
            writer.write(frame)
    finally:
        writer.release()
    if not path.is_file() or path.stat().st_size < 10_000:
        raise RuntimeError("生成的测试视频为空或损坏")
    return path


def generate_demo_mp4(source_avi: Path, path: Path) -> Path:
    """Create the browser-friendly MP4 from the locally generated AVI.

    The command is an argument array and both paths are passed verbatim, so
    Windows user names and project directories may contain Chinese characters.
    """
    try:
        import imageio_ffmpeg

        executable = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError(f"找不到项目内 FFmpeg：{exc}") from exc
    command = [
        str(executable), "-y", "-loglevel", "error", "-i", str(source_avi),
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path),
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=90, creationflags=creationflags, check=False,
    )
    if completed.returncode != 0:
        path.unlink(missing_ok=True)
        raise RuntimeError("MP4 转码失败：" + (completed.stderr[-500:] or f"退出码 {completed.returncode}"))
    capture = cv2.VideoCapture(str(path))
    try:
        ok, frame = capture.read()
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    finally:
        capture.release()
    if not ok or frame is None or not frame.size or frame_count < 100:
        path.unlink(missing_ok=True)
        raise RuntimeError("MP4 已生成，但回读验证失败")
    return path


def write_seed_manifest(video_path: Path) -> Path:
    manifest = f'''{{
  "evidence_type": "SYNTHETIC_TEST_PLAYBACK",
  "camera": {{"name": "客厅测试回放", "room_name": "客厅", "source_type": "video", "source": "demo/sample-videos/{video_path.name}"}},
  "zones": [
    {{"id": "zone-desk", "name": "桌面", "priority": 10, "points": [[0.02,0.17],[0.46,0.17],[0.46,0.94],[0.02,0.94]]}},
    {{"id": "zone-sofa-right", "name": "沙发右侧", "priority": 10, "points": [[0.50,0.17],[0.99,0.17],[0.99,0.94],[0.50,0.94]]}},
    {{"id": "zone-floor", "name": "地面", "priority": 20, "points": [[0,0.91],[1,0.91],[1,1],[0,1]]}}
  ],
  "items": [
    {{"name": "我的手机", "aruco_id": 1}},
    {{"name": "我的钥匙", "aruco_id": 2}},
    {{"name": "蓝色钱包", "aruco_id": 3}}
  ]
}}
'''
    path = SEED_DIR / "demo-manifest.json"
    path.write_text(manifest, encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="生成物忆标签和合成测试回放")
    parser.add_argument("--skip-video", action="store_true", help="只生成标签")
    args = parser.parse_args()
    MARKER_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    SEED_DIR.mkdir(parents=True, exist_ok=True)
    markers = {marker_id: generate_marker(marker_id) for marker_id in ITEMS}
    a4 = generate_a4_svg(markers)
    video = VIDEO_DIR / "object-memory-demo.avi"
    mp4 = VIDEO_DIR / "object-memory-demo.mp4"
    if not args.skip_video:
        generate_demo_video(video)
        generate_demo_mp4(video, mp4)
    manifest = write_seed_manifest(mp4 if mp4.exists() else video)
    for path in [*markers.values(), a4, video, mp4, manifest]:
        if path.exists():
            print(f"generated {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
