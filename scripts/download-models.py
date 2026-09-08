#!/usr/bin/env python3
"""Download optional models from their official upstream repositories.

Failure is isolated from stable ArUco mode. Files are written atomically under
``data/models`` and validated before replacing an existing model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "data" / "models"

MODELS = {
    "nanodet": {
        "filename": "object_detection_nanodet_2022nov.onnx",
        "url": "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/object_detection_nanodet/object_detection_nanodet_2022nov.onnx",
        "max_bytes": 10_000_000,
        "expected_sha256": "4b82da9944b88577175ee23a459dce2e26e6e4be573def65b1055dc2d9720186",
        "validator": "opencv_dnn",
        "license": "Apache-2.0 (OpenCV Zoo model directory)",
    },
    "hands": {
        "filename": "hand_landmarker.task",
        "url": "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task",
        "max_bytes": 20_000_000,
        "expected_sha256": "fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1",
        "validator": "nonempty",
        "license": "MediaPipe model terms; see upstream model card",
    },
}


def download(name: str, force: bool = False) -> Path:
    specification = MODELS[name]
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    destination = MODEL_DIR / str(specification["filename"])
    if destination.is_file() and not force:
        existing_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
        if existing_hash != specification["expected_sha256"]:
            raise RuntimeError(f"已有模型校验失败，请删除文件或使用 --force：{destination.name}")
        print(f"verified {destination.relative_to(ROOT)} ({destination.stat().st_size} bytes, sha256={existing_hash})")
        return destination
    temporary = destination.with_suffix(destination.suffix + ".download")
    temporary.unlink(missing_ok=True)
    request = urllib.request.Request(str(specification["url"]), headers={"User-Agent": "ObjectMemory/0.1 model-downloader"})
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=45) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > int(specification["max_bytes"]):
                    raise RuntimeError("下载大小超出预期，已中止")
                digest.update(chunk)
                output.write(chunk)
        if total < 100_000:
            raise RuntimeError("下载内容过小，可能是错误页")
        if digest.hexdigest() != specification["expected_sha256"]:
            raise RuntimeError(f"SHA256 校验失败：期望 {specification['expected_sha256']}，实际 {digest.hexdigest()}")
        if specification["validator"] == "opencv_dnn":
            import cv2
            import numpy as np
            cv2.dnn.readNetFromONNX(np.frombuffer(temporary.read_bytes(), dtype=np.uint8))
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    metadata = {
        "name": name, "filename": destination.name, "source": specification["url"],
        "sha256": digest.hexdigest(), "bytes": total, "license": specification["license"],
    }
    (MODEL_DIR / f"{destination.name}.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"downloaded {destination.relative_to(ROOT)} ({total} bytes, sha256={digest.hexdigest()})")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="下载物忆可选的官方视觉模型")
    parser.add_argument("--nanodet", action="store_true", help="OpenCV Zoo COCO NanoDet")
    parser.add_argument("--hands", action="store_true", help="MediaPipe Hand Landmarker")
    parser.add_argument("--all", action="store_true", help="下载所有可选模型")
    parser.add_argument("--force", action="store_true", help="覆盖并重新校验")
    args = parser.parse_args()
    selected = [name for name in MODELS if args.all or getattr(args, name)]
    if not selected:
        parser.error("请指定 --nanodet、--hands 或 --all")
    failures = []
    for name in selected:
        try:
            download(name, args.force)
        except Exception as exc:
            failures.append((name, str(exc)))
            print(f"failed {name}: {exc}", file=sys.stderr)
    if failures:
        print("可选模型下载失败不会影响稳定标签模式。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
