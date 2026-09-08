"""Local reference-photo registration. Saved bytes are not a ready identity.

Originals survive retention; only explicitly confirmed target crops become
appearance features. Model preparation never happens inside an HTTP request.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import threading
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError


class RegistrationError(ValueError):
    pass


class RegistrationService:
    MAX_BYTES = 12 * 1024 * 1024
    MAX_PIXELS = 24_000_000
    MAX_REFERENCES = 12

    def __init__(self, db, data_root: Path, model_root: Path, encoder=None):
        self.db = db
        self.root = Path(data_root).resolve()
        self.folder = self.root / 'registered-items'
        self.folder.mkdir(parents=True, exist_ok=True)
        if self.folder.is_symlink() or (getattr(self.folder, 'is_junction', lambda: False)()) or self.folder.resolve().parent != self.root:
            raise RegistrationError('参考图片目录不安全。')
        self.model_root = Path(model_root)
        self._encoder = encoder
        self._suggestion_detector = None
        self.lock = threading.RLock()
        self._loaded_versions = {}
        self._matcher = None

    @property
    def encoder(self):
        with self.lock:
            if self._encoder is None:
                from services.vision.detectors.appearance import AppearanceEncoder
                self._encoder = AppearanceEncoder(self.model_root / 'dinov2-small.onnx')
            return self._encoder

    def _path(self, url: str) -> Path:
        if (self.folder.is_symlink() or getattr(self.folder, 'is_junction', lambda: False)()
                or self.folder.resolve() != self.root / 'registered-items'):
            raise RegistrationError('参考图片目录已变化或越界，已停止文件操作。')
        prefix = '/media/registered-items/'
        if not str(url or '').startswith(prefix):
            raise RegistrationError('非法参考图片路径。')
        name = str(url)[len(prefix):]
        if not name or Path(name).name != name or '/' in name or '\\' in name:
            raise RegistrationError('非法参考图片路径。')
        path = self.folder / name
        if path.is_symlink() or path.resolve().parent != self.folder.resolve():
            raise RegistrationError('参考图片路径越界。')
        return path

    def _write(self, name: str, content: bytes) -> str:
        url = f'/media/registered-items/{name}'
        path = self._path(url)
        temporary = path.with_name(f'{path.name}.{uuid4().hex}.tmp')
        try:
            temporary.write_bytes(content)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return url

    @classmethod
    def decode(cls, content: bytes):
        if not content or len(content) > cls.MAX_BYTES:
            raise RegistrationError('每张照片必须非空且不超过 12 MB。')
        if not (content.startswith(b'\xff\xd8\xff') or content.startswith(b'\x89PNG\r\n\x1a\n')):
            raise RegistrationError('仅支持真实 JPEG/PNG；HEIC、WebP 请先转换，未保存不支持的文件。')
        try:
            with Image.open(io.BytesIO(content)) as source:
                if source.width * source.height > cls.MAX_PIXELS:
                    raise RegistrationError('图片不得超过 2400 万像素。')
                source.load()
                oriented = ImageOps.exif_transpose(source).convert('RGB')
                if min(oriented.size) < 32:
                    raise RegistrationError('照片尺寸太小，至少需要 32×32 像素。')
                normalized = io.BytesIO()
                oriented.save(normalized, format='JPEG', quality=95)
                frame = cv2.cvtColor(np.asarray(oriented), cv2.COLOR_RGB2BGR)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise RegistrationError('照片损坏或无法解码。') from exc
        return frame, normalized.getvalue()

    def _suggest(self, frame):
        try:
            if self._suggestion_detector is None:
                from services.vision.detectors.nanodet import NanoDetDetectorBackend
                self._suggestion_detector = NanoDetDetectorBackend(self.model_root / 'object_detection_nanodet_2022nov.onnx')
            health = self._suggestion_detector.health()
            if not health.get('available'):
                return [], str(health.get('error') or '候选模型尚未准备')
            h, w = frame.shape[:2]
            proposals = []
            detections = self._suggestion_detector.detect(frame)
            settings = (self.db.get('settings', 'main') or {}).get('value') or {}
            suggestion_error = None
            if settings.get('phone_shape_enabled', True):
                try:
                    from services.vision.detectors.phone_shape import PhoneShapeProposer
                    detections = PhoneShapeProposer(settings.get('phone_shape')).supplement(frame, detections)
                except Exception as exc:
                    # Auxiliary limits/failures must not discard successful
                    # primary model proposals for this uploaded image.
                    suggestion_error = f'手机外形建议暂不可用，已保留主模型建议；可手动确认目标：{exc}'
            for detection in detections:
                if detection.label == 'person':
                    continue
                x, y, bw, bh = detection.bbox
                proposals.append({'bbox': [x/w, y/h, bw/w, bh/h], 'label': detection.label, 'score': detection.confidence,
                                  'proposal_backend': detection.metadata.get('backend'),
                                  'category_evidence': detection.metadata.get('category_evidence', 'model')})
            return sorted(proposals, key=lambda value: value['score'], reverse=True)[:12], suggestion_error
        except Exception as exc:
            return [], f'自动框建议失败，请手动确认目标：{exc}'

    def add(self, item_id: str, content: bytes, capture_source=None):
        if not self.db.get('items', item_id):
            raise RegistrationError('物品不存在。')
        frame, display_bytes = self.decode(content)
        digest = hashlib.sha256(content).hexdigest()
        with self.lock:
            references = self.db.list('item_reference_images', {'item_id': item_id})
            for existing in references:
                if existing.get('sha256') == digest:
                    return {**self.public_reference(existing), 'duplicate': True}
            if len(references) >= self.MAX_REFERENCES:
                raise RegistrationError('每件物品最多 12 张参考照片，请先删除不需要的角度。')
            reference_id = uuid4().hex
            original = self._write(f'{reference_id}-original' + ('.png' if content.startswith(b'\x89PNG') else '.jpg'), content)
            display = None
            try:
                display = self._write(f'{reference_id}-display.jpg', display_bytes)
                suggestions, suggestion_error = self._suggest(frame)
                row = self.db.save('item_reference_images', {
                    'item_id': item_id, 'path': display, 'original_path': original,
                    'sha256': digest, 'width': frame.shape[1], 'height': frame.shape[0],
                    'features': None, 'region': None, 'region_confirmed': False,
                    'suggested_regions': suggestions, 'status': 'image_saved', 'crop_path': None,
                    'capture_source': capture_source,
                    'quality': {'suggestion_error': suggestion_error, 'target_confirmation_required': True},
                }, reference_id)
                self.invalidate(item_id)
                return self.public_reference(row)
            except Exception:
                for url in (original, display):
                    if url:
                        self._path(url).unlink(missing_ok=True)
                raise

    def add_batch(self, item_id: str, contents: list[bytes]):
        # Capacity and all image validation precede writes under the same lock.
        # Duplicate bytes within a batch count once, just like existing photos.
        with self.lock:
            for content in contents:
                self.decode(content)
            existing = self.db.list('item_reference_images', {'item_id': item_id})
            digests = {row.get('sha256') for row in existing}
            incoming = {hashlib.sha256(content).hexdigest() for content in contents}
            if len(existing) + len(incoming - digests) > self.MAX_REFERENCES:
                raise RegistrationError('整批未保存：每件物品最多 12 张参考照片，请减少本次选择或删除旧角度。')
            return [self.add(item_id, content) for content in contents]

    @staticmethod
    def public_reference(row):
        result = {key: value for key, value in row.items() if key != 'features'}
        result['quality'] = {**(row.get('quality') or {}),
                             'target_confirmation_required': not bool(row.get('region_confirmed'))}
        return result

    def invalidate(self, item_id):
        previous = self.db.get('item_recognition_profiles', item_id) or {}
        version = int(previous.get('profile_version') or 0) + 1
        self._loaded_versions.pop(item_id, None)
        self._matcher = None
        return self.db.save('item_recognition_profiles', {
            'item_id': item_id, 'profile_version': version, 'status': 'images_saved',
            'embeddings': [], 'reference_ids': [], 'dimension': None, 'error': None,
        }, item_id)

    def confirm_region(self, item_id, reference_id, region, confirmed):
        if confirmed is not True or not isinstance(region, list) or len(region) != 4:
            raise RegistrationError('请明确确认目标框，格式为归一化 x、y、宽、高。')
        try:
            x, y, w, h = [float(value) for value in region]
        except (TypeError, ValueError):
            raise RegistrationError('目标框必须是有效数值。')
        if not all(math.isfinite(value) for value in (x, y, w, h)) or min(x, y) < 0 or min(w, h) <= 0 or x+w > 1.000001 or y+h > 1.000001:
            raise RegistrationError('目标框必须位于图片范围内。')
        with self.lock:
            row = self.db.get('item_reference_images', reference_id)
            if not row or row.get('item_id') != item_id:
                raise RegistrationError('参考照片不属于这个物品。')
            if w*row['width'] < 32 or h*row['height'] < 32:
                raise RegistrationError('目标细节不足：框内至少需要 32×32 像素，请补充近一些的照片。')
            updated = self.db.save('item_reference_images', {
                'region': [x, y, w, h], 'region_confirmed': True,
                'status': 'region_confirmed', 'features': None,
            }, reference_id)
            self.invalidate(item_id)
            return self.public_reference(updated)

    def profile(self, item_id, engines=None):
        row = self.db.get('item_recognition_profiles', item_id) or {}
        refs = self.db.list('item_reference_images', {'item_id': item_id})
        version = int(row.get('profile_version') or 0)
        loaded = []
        for camera_id, engine in (engines or {}).items():
            if getattr(engine, 'loaded_profile_versions', {}).get(item_id) == version and row.get('status') == 'ready':
                loaded.append(camera_id)
        preparation = {'status': 'not_prepared'}
        status_path = self.model_root / 'appearance-status.json'
        try:
            if status_path.is_file() and status_path.stat().st_size <= 16384:
                preparation = json.loads(status_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            preparation = {'status': 'unknown', 'error': '本地准备状态暂时不可读'}
        # Surface the saved detector evidence; GET does not rerun inference or
        # claim that a failed category proposal invalidates a user's identity.
        from services.vision.detectors.appearance import normalize_category
        item = self.db.get('items', item_id) or {}
        category = normalize_category(item.get('type'))
        warnings = []
        if refs and category == 'phone' and not any(
            normalize_category(proposal.get('label')) == category
            and proposal.get('category_evidence') != 'geometry_only'
            for ref in refs for proposal in (ref.get('suggested_regions') or [])
        ):
            warnings.append('已保存的参考照片中，内置检测器尚未找出手机候选框。档案已建立不等于现场已识别；请查看现场检测结果，并核对参考框是否包含完整手机、是否带入过多背景。')
        return {
            'registration_status': row.get('status') or ('images_saved' if refs else 'needs_photos'),
            'profile_version': version, 'model_id': row.get('model_id'), 'model_version': row.get('model_version'),
            'reference_count': len(refs), 'ready_reference_count': len(row.get('reference_ids') or []),
            'confirmed_reference_count': sum(bool(ref.get('region_confirmed')) for ref in refs),
            'loaded_profile_version': version if loaded else self._loaded_versions.get(item_id), 'loaded_camera_ids': loaded,
            'error': row.get('error'),
            'quality_warnings': warnings,
            'model_preparation': preparation,
            'model_runtime': self._encoder.health() if self._encoder is not None else {'status': 'not_loaded', 'available': False},
        }

    def items_for_inference(self):
        result = []
        for item in self.db.list('items'):
            profile = self.db.get('item_recognition_profiles', item['id'])
            result.append({**item, 'reference_images': self.db.list('item_reference_images', {'item_id': item['id']}), 'appearance_profile': profile})
        return result

    def build(self, item_id):
        with self.lock:
            refs = self.db.list('item_reference_images', {'item_id': item_id})
            confirmed = [ref for ref in refs if ref.get('region_confirmed') and ref.get('region')]
            if not confirmed:
                raise RegistrationError('至少上传一张照片并确认目标框；保存图片并不等于已建立识别档案。')
            prior = self.db.get('item_recognition_profiles', item_id) or self.invalidate(item_id)
            self.db.save('item_recognition_profiles', {'status': 'building', 'error': None}, item_id)
            try:
                health = self.encoder.health()
                if not health.get('available'):
                    # An explicit build retry after local preparation may open
                    # the newly installed model; never retry/download per frame.
                    from services.vision.detectors.appearance import AppearanceEncoder
                    if isinstance(self._encoder, AppearanceEncoder):
                        self._encoder = AppearanceEncoder(self.model_root / 'dinov2-small.onnx')
                        health = self._encoder.health()
                if not health.get('available'):
                    raise RegistrationError(str(health.get('error') or '外观模型未就绪，请先准备本地模型。'))
                if prior.get('model_version') and (
                    prior.get('model_id') != health['model_id']
                    or prior['model_version'] != health['model_version']
                ):
                    # Changing preprocessing changes the meaning of every vector.
                    # Invalidate the old generation before encoding; an explicit
                    # retry of this same generation must not advance it again.
                    prior = self.invalidate(item_id)
                    self.db.save('item_recognition_profiles', {
                        'status': 'building', 'model_id': health['model_id'],
                        'model_version': health['model_version'],
                    }, item_id)
                embeddings, reference_ids = [], []
                for ref in confirmed:
                    content = self._path(ref['path']).read_bytes()
                    frame = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
                    if frame is None:
                        raise RegistrationError('参考图片无法读取，请重新上传。')
                    x, y, w, h = ref['region']
                    height, width = frame.shape[:2]
                    crop = frame[round(y*height):min(height, round((y+h)*height)), round(x*width):min(width, round((x+w)*width))]
                    vector = self.encoder.encode(crop)
                    ok, encoded = cv2.imencode('.jpg', crop, [cv2.IMWRITE_JPEG_QUALITY, 95])
                    if not ok:
                        raise RegistrationError('目标裁剪保存失败。')
                    crop_url = self._write(f"{ref['id']}-crop.jpg", encoded.tobytes())
                    self.db.save('item_reference_images', {'crop_path': crop_url, 'status': 'featured', 'features': {'backend': health['model_id'], 'model_version': health['model_version'], 'vector': vector}}, ref['id'])
                    embeddings.append(vector)
                    reference_ids.append(ref['id'])
                self.db.save('item_recognition_profiles', {
                    'item_id': item_id, 'profile_version': int(prior['profile_version']), 'status': 'ready',
                    'model_id': health['model_id'], 'model_version': health['model_version'],
                    'dimension': len(embeddings[0]), 'embeddings': embeddings,
                    'reference_ids': reference_ids, 'error': None,
                }, item_id)
                # Materialize the same matcher contract used by live inference.
                from services.vision.detectors.appearance import ProfileMatcher
                matcher = ProfileMatcher(self.items_for_inference(), self.encoder)
                if matcher.loaded_profile_versions.get(item_id) != int(prior['profile_version']):
                    raise RegistrationError('特征已生成，但识别服务拒绝该档案版本，未标记可识别。')
                self._matcher = matcher
                self._loaded_versions[item_id] = int(prior['profile_version'])
            except Exception as exc:
                self.db.save('item_recognition_profiles', {'status': 'failed', 'embeddings': [], 'error': str(exc)}, item_id)
                self._loaded_versions.pop(item_id, None)
                raise RegistrationError(f'建立档案失败：{exc}') from exc
        return self.profile(item_id)

    def delete_reference(self, item_id, reference_id):
        with self.lock:
            row = self.db.get('item_reference_images', reference_id)
            if not row or row.get('item_id') != item_id:
                raise RegistrationError('参考照片不存在。')
            paths = {self._path(row[key]) for key in ('path', 'original_path', 'crop_path') if row.get(key)}
            # Resolve every path before mutation; no user-controlled escape.
            for path in paths:
                path.unlink(missing_ok=True)
            self.db.delete('item_reference_images', reference_id)
            self.invalidate(item_id)

    def delete_profile(self, item_id):
        with self.lock:
            for reference in self.db.list('item_reference_images', {'item_id': item_id}):
                self.delete_reference(item_id, reference['id'])
            self.db.delete('item_recognition_profiles', item_id)
            self._loaded_versions.pop(item_id, None)
