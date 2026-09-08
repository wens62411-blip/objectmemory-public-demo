from __future__ import annotations

import subprocess
import os
import re
import tempfile
import uuid
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np


@dataclass(slots=True)
class ClipResult:
    path: Path | None
    frame_count: int
    duration: float
    format: str | None
    error: str | None = None
    sha256: str | None = None


class EventMediaWriter:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.image_dir = self.data_dir / "event-images"
        self.clip_dir = self.data_dir / "event-clips"
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.clip_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_event_id(event_id: str) -> str:
        value = str(event_id)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ValueError("event_id contains unsafe path characters")
        return value

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _temporary_path(directory: Path, suffix: str) -> Path:
        # Uniqueness comes from the UUID; duplicating the event id needlessly
        # exceeds Windows path limits even when the final path is valid.
        return directory / f".{uuid.uuid4().hex}.tmp{suffix}"

    def write_image(self, event_id: str, frame: np.ndarray) -> Path | None:
        safe_id = self._safe_event_id(event_id)
        path = self.image_dir / f"{safe_id}.jpg"
        temporary = self._temporary_path(self.image_dir, ".jpg")
        try:
            ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if not ok:
                return None
            temporary.write_bytes(encoded)
            os.replace(temporary, path)
            return path
        except (OSError, cv2.error):
            return None
        finally:
            temporary.unlink(missing_ok=True)

    def write_clip(self, event_id: str, frames: Iterable[np.ndarray], fps: float) -> ClipResult:
        safe_id = self._safe_event_id(event_id)
        materialized = [frame for frame in frames if frame is not None and frame.size]
        fps = max(1.0, min(30.0, float(fps or 5.0)))
        if not materialized:
            return ClipResult(None, 0, 0.0, None, "没有可写入的视频帧")
        height, width = materialized[0].shape[:2]
        normalized = [cv2.resize(frame, (width, height)) if frame.shape[:2] != (height, width) else frame for frame in materialized]
        mp4_path = self.clip_dir / f"{safe_id}.mp4"
        temporary_mp4 = self._temporary_path(self.clip_dir, ".mp4")
        try:
            import imageio_ffmpeg

            executable = imageio_ffmpeg.get_ffmpeg_exe()
            command = [
                str(executable), "-y", "-loglevel", "error", "-nostats", "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s:v", f"{width}x{height}", "-r", f"{fps:.3f}", "-i", "-",
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "25",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary_mp4),
            ]
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            with tempfile.TemporaryFile() as error_file:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=error_file, creationflags=creationflags)
                try:
                    assert process.stdin is not None
                    for frame in normalized:
                        process.stdin.write(np.ascontiguousarray(frame))
                    process.stdin.close()
                    return_code = process.wait(timeout=60)
                    error_file.seek(0)
                    stderr = error_file.read().decode("utf-8", errors="replace")
                except Exception:
                    process.kill()
                    process.wait(timeout=5)
                    raise
            if return_code == 0 and temporary_mp4.is_file() and temporary_mp4.stat().st_size > 0:
                digest = self.sha256(temporary_mp4)
                os.replace(temporary_mp4, mp4_path)
                return ClipResult(mp4_path, len(normalized), len(normalized) / fps, "mp4", sha256=digest)
            ffmpeg_error = stderr[-500:] or f"ffmpeg 退出码 {return_code}"
        except Exception as exc:
            ffmpeg_error = str(exc)
        finally:
            temporary_mp4.unlink(missing_ok=True)

        avi_path = self.clip_dir / f"{safe_id}.avi"
        temporary_avi = self._temporary_path(self.clip_dir, ".avi")
        writer: cv2.VideoWriter | None = None
        try:
            writer = cv2.VideoWriter(str(temporary_avi), cv2.VideoWriter_fourcc(*"MJPG"), fps, (width, height))
            if not writer.isOpened():
                return ClipResult(None, 0, 0.0, None, f"FFmpeg 和 OpenCV 均无法写入片段：{ffmpeg_error}")
            for frame in normalized:
                writer.write(frame)
            writer.release()
            writer = None
            if temporary_avi.is_file() and temporary_avi.stat().st_size > 0:
                digest = self.sha256(temporary_avi)
                os.replace(temporary_avi, avi_path)
                return ClipResult(
                    avi_path,
                    len(normalized),
                    len(normalized) / fps,
                    "avi",
                    f"MP4 编码失败，已降级 AVI：{ffmpeg_error}",
                    digest,
                )
            return ClipResult(None, 0, 0.0, None, "AVI 文件写入失败")
        except Exception as exc:
            return ClipResult(None, 0, 0.0, None, f"片段写入失败：{exc}")
        finally:
            if writer is not None:
                writer.release()
            temporary_avi.unlink(missing_ok=True)
