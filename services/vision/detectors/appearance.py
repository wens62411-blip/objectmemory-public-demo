"""Pinned local DINOv2 features. No network, auto-download, or HSV fallback."""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

MANIFEST = json.loads(Path(__file__).with_name("appearance-model.json").read_text(encoding="utf-8"))
MODEL_ID = MANIFEST["model_id"]
MODEL_VERSION = MANIFEST["model_version"]
DIMENSION = int(MANIFEST["dimension"])
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODEL_PATH = ROOT / MANIFEST["default_path"]


class AppearanceEncodingError(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def preprocess_crop(crop: np.ndarray) -> np.ndarray:
    """Application v2: retain the entire object crop, fit/pad to 224 RGB.

    This intentionally differs from the upstream BitImageProcessor's shortest
    edge 256 / center224 default. Its features MUST use the v2 model_version;
    vectors produced with the old center crop are not compatible profiles.
    """
    if not isinstance(crop, np.ndarray) or crop.dtype != np.uint8 or crop.ndim != 3 or crop.shape[2] != 3:
        raise ValueError("appearance crop must be a uint8 BGR HWC image")
    height, width = crop.shape[:2]
    if min(height, width) < 1 or height * width > 16_777_216:
        raise ValueError("appearance crop is empty or exceeds the bounded pixel limit")
    if max(height, width) > 32 * min(height, width):
        raise ValueError("appearance crop aspect ratio exceeds the bounded input ratio")
    scale = min(224 / width, 224 / height)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    rgb = Image.fromarray(np.ascontiguousarray(crop[:, :, ::-1]))
    resized = rgb.resize(size, Image.Resampling.BICUBIC)
    padded = Image.new('RGB', (224, 224), (0, 0, 0))
    padded.paste(resized, ((224 - size[0]) // 2, (224 - size[1]) // 2))
    pixels = np.asarray(padded, dtype=np.float32) / np.float32(255)
    pixels = (pixels - np.asarray([0.485, 0.456, 0.406], np.float32)) / np.asarray([0.229, 0.224, 0.225], np.float32)
    return np.ascontiguousarray(pixels.transpose(2, 0, 1)[None], dtype=np.float32)


@dataclass
class _SessionBundle:
    session: Any
    lock: threading.Lock = field(default_factory=threading.Lock)


_SESSION_CACHE: weakref.WeakValueDictionary[str, _SessionBundle] = weakref.WeakValueDictionary()
_CACHE_LOCK = threading.Lock()


def _appearance_threads() -> int:
    """Bound CPU work per session; invalid overrides use the hardware default."""
    default = min(4, max(1, os.cpu_count() or 1))
    try:
        requested = int(os.environ.get('OM_APPEARANCE_THREADS', str(default)))
    except (TypeError, ValueError):
        return default
    return requested if 1 <= requested <= 4 else default


class AppearanceEncoder:
    model_id = MODEL_ID
    model_version = MODEL_VERSION
    version = MODEL_VERSION
    dimension = DIMENSION

    def __init__(self, model_path: str | Path | None = None) -> None:
        self.model_path = Path(model_path) if model_path is not None else DEFAULT_MODEL_PATH
        self._bundle: _SessionBundle | None = None
        self._error: str | None = None
        self._error_code: str | None = None
        self._last_ms: float | None = None
        self._encoded = 0
        self._intra_op_num_threads = _appearance_threads()
        self._metrics_lock = threading.Lock()
        try:
            if not self.model_path.is_file():
                self._error_code = "model_missing"
                raise FileNotFoundError("Local appearance model is missing; run the explicit preparation command")
            if self.model_path.stat().st_size != MANIFEST["bytes"] or file_sha256(self.model_path) != MANIFEST["sha256"]:
                self._error_code = "model_hash_mismatch"
                raise ValueError("Local appearance model does not match the pinned size/SHA-256")
            try:
                import onnxruntime as ort
            except ImportError as exc:
                self._error_code = "runtime_missing"
                raise RuntimeError("Optional onnxruntime dependencies are unavailable") from exc
            key = str(self.model_path.resolve()) + ":" + MANIFEST["sha256"] + ":threads=" + str(self._intra_op_num_threads)
            with _CACHE_LOCK:
                bundle = _SESSION_CACHE.get(key)
                if bundle is None:
                    ort.disable_telemetry_events()
                    options = ort.SessionOptions()
                    options.intra_op_num_threads = self._intra_op_num_threads
                    options.inter_op_num_threads = 1
                    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                    session = ort.InferenceSession(str(self.model_path), sess_options=options, providers=["CPUExecutionProvider"])
                    inputs = session.get_inputs()
                    outputs = {output.name: output for output in session.get_outputs()}
                    if len(inputs) != 1 or inputs[0].name != MANIFEST["input_name"] or inputs[0].type != "tensor(float)":
                        raise ValueError("Unexpected ONNX input contract")
                    if MANIFEST["output_name"] not in outputs or outputs[MANIFEST["output_name"]].shape[-1] != DIMENSION:
                        raise ValueError("Unexpected ONNX output contract")
                    bundle = _SessionBundle(session)
                    _SESSION_CACHE[key] = bundle
                self._bundle = bundle
        except Exception as exc:
            self._error = str(exc)
            self._error_code = self._error_code or "model_load_failed"

    def health(self) -> dict[str, Any]:
        with self._metrics_lock:
            return {"available": self._bundle is not None, "status": "ready" if self._bundle is not None else "unavailable",
                    "model_id": self.model_id, "model_version": self.model_version, "version": self.version,
                    "dimension": self.dimension, "model_path": str(self.model_path), "sha256": MANIFEST["sha256"],
                    "provider": "CPUExecutionProvider", "offline_only": True, "error": self._error,
                    "intra_op_num_threads": self._intra_op_num_threads,
                    "error_code": self._error_code, "encoded_crops": self._encoded, "last_encode_ms": self._last_ms}

    def encode(self, crop: np.ndarray) -> list[float]:
        bundle = self._bundle
        if bundle is None:
            raise AppearanceEncodingError(self._error or "Appearance encoder is unavailable")
        pixels = preprocess_crop(crop)
        started = time.perf_counter()
        try:
            with bundle.lock:
                output = bundle.session.run([MANIFEST["output_name"]], {MANIFEST["input_name"]: pixels})[0]
            if output.ndim != 3 or output.shape[0] != 1 or output.shape[2] != self.dimension:
                raise ValueError("Unexpected feature output dimensions")
            feature = np.asarray(output[0, 0], dtype=np.float64)
            norm = float(np.linalg.norm(feature))
            if not np.all(np.isfinite(feature)) or not math.isfinite(norm) or norm <= 1e-12:
                raise ValueError("Appearance model produced invalid features")
            vector = (feature / norm).astype(np.float32).tolist()
            with self._metrics_lock:
                self._encoded += 1
                self._last_ms = round((time.perf_counter() - started) * 1000, 3)
                self._error = self._error_code = None
            return vector
        except Exception as exc:
            with self._metrics_lock:
                self._error, self._error_code = str(exc), "inference_failed"
            raise AppearanceEncodingError(str(exc)) from exc

    def encode_patches(self, frame: np.ndarray) -> tuple[np.ndarray, np.ndarray, tuple[float, float]]:
        """Return spatial descriptors in ORIGINAL image coordinates.

        These are patch similarities, not the CLS profile vector or a class
        probability. Padding tokens are excluded; no additional model loads,
        resizing policy, network access, or caller-provided target boxes.
        """
        return self.encode_patches_batch([frame])[0]

    def encode_patches_batch(self, frames: list[np.ndarray]) -> list[tuple[np.ndarray, np.ndarray, tuple[float, float]]]:
        """One bounded ONNX call, identical per-window preprocessing/geometry."""
        if not isinstance(frames, (list, tuple)) or not 1 <= len(frames) <= 4:
            raise ValueError('Spatial batch must contain one to four images')
        bundle = self._bundle
        if bundle is None:
            raise AppearanceEncodingError(self._error or 'Appearance encoder is unavailable')
        pixels = np.concatenate([preprocess_crop(frame) for frame in frames], axis=0)
        started = time.perf_counter()
        try:
            with bundle.lock:
                output = bundle.session.run([MANIFEST['output_name']], {MANIFEST['input_name']: pixels})[0]
            if output.shape != (len(frames), 257, self.dimension) or not np.isfinite(output).all():
                raise ValueError('Unexpected spatial feature output dimensions or values')
            result = [self._spatial_features(frame, row) for frame, row in zip(frames, output)]
            with self._metrics_lock:
                self._encoded += len(frames)
                self._last_ms = round((time.perf_counter() - started) * 1000, 3)
                self._error = self._error_code = None
            return result
        except Exception as exc:
            with self._metrics_lock:
                self._error, self._error_code = str(exc), 'inference_failed'
            raise AppearanceEncodingError(str(exc)) from exc

    @staticmethod
    def _spatial_features(frame, output):
        features = np.array(output[1:], dtype=np.float32, copy=True)
        norms = np.linalg.norm(features, axis=1, keepdims=True)
        if (norms <= 1e-12).any():
            raise ValueError('Appearance model produced empty patch descriptors')
        features /= norms
        height, width = frame.shape[:2]
        scale = min(224 / width, 224 / height)
        resized_width, resized_height = max(1, round(width * scale)), max(1, round(height * scale))
        left, top = (224 - resized_width) // 2, (224 - resized_height) // 2
        yy, xx = np.mgrid[:16, :16]
        centers = np.column_stack(((xx.ravel() + .5) * 14, (yy.ravel() + .5) * 14))
        valid = ((centers[:, 0] > left + 7) & (centers[:, 0] < left + resized_width - 7)
                 & (centers[:, 1] > top + 7) & (centers[:, 1] < top + resized_height - 7))
        coordinates = ((centers[valid] - [left, top]) * [width / resized_width, height / resized_height]).astype(np.float32)
        return features[valid], coordinates, (14 * width / resized_width, 14 * height / resized_height)

    def close(self) -> None:
        with self._metrics_lock:
            self._bundle = None
            self._error, self._error_code = "Appearance encoder was closed", "closed"


_CATEGORY_ALIASES = {
    "phone": "phone", "cell phone": "phone", "cellphone": "phone", "smartphone": "phone",
    "smart phone": "phone", "mobile phone": "phone", "手机": "phone", "电话": "phone",
    "wallet": "wallet", "purse": "wallet", "钱包": "wallet",
    "key": "keys", "keys": "keys", "钥匙": "keys",
    "cup": "cup", "mug": "cup", "杯子": "cup", "水杯": "cup",
    "bottle": "bottle", "瓶子": "bottle", "水瓶": "bottle",
    "remote": "remote", "remote control": "remote", "遥控器": "remote",
    "book": "book", "书": "book", "laptop": "laptop", "笔记本电脑": "laptop",
    "backpack": "backpack", "背包": "backpack", "handbag": "handbag", "手提包": "handbag",
}


def normalize_category(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        return None
    normalized = " ".join(value.casefold().replace("_", " ").replace("-", " ").split())
    # Unknown names may match only the identical normalized name, never another category.
    return _CATEGORY_ALIASES.get(normalized, normalized)


class ProfileMatcher:
    """Open-set cosine matching; conservative engineering gates, not accuracy claims."""
    def __init__(self, items: list[dict], encoder: AppearanceEncoder, threshold: float = 0.80, margin: float = 0.06) -> None:
        self.encoder = encoder
        self.threshold, self.margin = float(threshold), float(margin)
        if not math.isfinite(self.threshold) or not -1 <= self.threshold <= 1 or not math.isfinite(self.margin) or not 0 <= self.margin <= 2:
            raise ValueError("Invalid appearance threshold or margin")
        self.loaded_profile_versions: dict[str, int] = {}
        self.invalid_profiles: dict[str, str] = {}
        self._profiles: list[tuple[str, str, int, np.ndarray]] = []
        if not encoder.health().get("available"):
            return
        seen: set[str] = set()
        for item in items:
            item_id = str(item.get("id") or "")
            profile = item.get("appearance_profile")
            if not item_id or item_id in seen:
                if item_id:
                    self.invalid_profiles[item_id] = "duplicate_item_id"
                    self.loaded_profile_versions.pop(item_id, None)
                    self._profiles = [row for row in self._profiles if row[0] != item_id]
                continue
            seen.add(item_id)
            if not isinstance(profile, dict) or profile.get("status") != "ready":
                self.invalid_profiles[item_id] = "profile_not_ready"
                continue
            try:
                version = profile.get("profile_version")
                if type(version) is not int or version < 1:
                    raise ValueError("invalid_profile_version")
                if profile.get("model_id") != encoder.model_id or profile.get("model_version") != encoder.model_version:
                    raise ValueError("model_version_mismatch")
                if type(profile.get("dimension")) is not int or profile["dimension"] != encoder.dimension:
                    raise ValueError("dimension_mismatch")
                category = normalize_category(item.get("type"))
                if category is None:
                    raise ValueError("invalid_item_category")
                embeddings = profile.get("embeddings")
                if not isinstance(embeddings, list) or not 1 <= len(embeddings) <= 32:
                    raise ValueError("invalid_embeddings")
                if any(not isinstance(row, list) or len(row) != encoder.dimension for row in embeddings):
                    raise ValueError("dimension_mismatch")
                if any(type(value) not in (int, float) for row in embeddings for value in row):
                    raise ValueError("features_must_be_numbers_not_strings_or_booleans")
                matrix = np.asarray(embeddings, np.float64)
                if not np.isfinite(matrix).all() or not np.allclose(np.linalg.norm(matrix, axis=1), 1, atol=1e-3, rtol=0):
                    raise ValueError("features_not_finite_unit_vectors")
                matrix.setflags(write=False)
                self._profiles.append((item_id, category, version, matrix))
                self.loaded_profile_versions[item_id] = version
            except (TypeError, ValueError, OverflowError) as exc:
                self.invalid_profiles[item_id] = str(exc)

    @property
    def available(self) -> bool:
        return bool(self.loaded_profile_versions) and bool(self.encoder.health().get("available"))

    def match(self, crop: np.ndarray, category: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"accepted": False, "item_id": None, "best_item_id": None, "second_item_id": None,
            "best_score": None, "second_score": None, "gap": None, "rejection": None, "profile_version": None,
            "model_id": self.encoder.model_id, "model_version": self.encoder.model_version,
            "threshold": self.threshold, "margin": self.margin, "score_kind": "cosine_similarity_not_probability"}
        if not self.encoder.health().get("available"):
            return {**result, "rejection": "encoder_unavailable", "error": self.encoder.health().get("error")}
        if not self._profiles:
            return {**result, "rejection": "no_ready_profiles"}
        canonical = normalize_category(category) if category is not None else None
        if category is not None and canonical is None:
            return {**result, "rejection": "unsupported_category"}
        profiles = [row for row in self._profiles if canonical is None or row[1] == canonical]
        if not profiles:
            return {**result, "rejection": "no_ready_profiles_for_category"}
        try:
            vector = np.asarray(self.encoder.encode(crop), np.float64)
            if vector.shape != (self.encoder.dimension,) or not np.isfinite(vector).all() or not np.isclose(np.linalg.norm(vector), 1, atol=1e-3):
                raise ValueError("Encoder returned invalid unit features")
            scores = sorted(((float(np.clip(np.max(matrix @ vector), -1, 1)), item_id, version)
                             for item_id, _category, version, matrix in profiles), key=lambda row: (-row[0], row[1]))
        except Exception as exc:
            return {**result, "rejection": "encoder_error", "error": str(exc)}
        best = scores[0]
        second = scores[1] if len(scores) > 1 else None
        gap = best[0] - second[0] if second else None
        result.update(best_item_id=best[1], best_score=best[0], profile_version=best[2],
                      second_item_id=second[1] if second else None, second_score=second[0] if second else None, gap=gap)
        if best[0] < self.threshold:
            result["rejection"] = "below_threshold"
        elif second and gap < self.margin:
            result["rejection"] = "ambiguous_identity"
        else:
            result.update(accepted=True, item_id=best[1])
        return result
