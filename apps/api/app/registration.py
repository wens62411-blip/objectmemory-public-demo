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
from collections import defaultdict
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
        return self.add_batch(item_id, [content], capture_source=capture_source)[0]

    def add_batch(self, item_id: str, contents: list[bytes], *, capture_source=None):
        if not self.db.get('items', item_id):
            raise RegistrationError('物品不存在。')
        with self.lock:
            references = self.db.list('item_reference_images', {'item_id': item_id})
            by_digest = {row.get('sha256'): row for row in references}
            digests = [hashlib.sha256(content).hexdigest() for content in contents]
            incoming = dict(zip(digests, contents))
            if len(references) + len(incoming.keys() - by_digest.keys()) > self.MAX_REFERENCES:
                raise RegistrationError('整批未保存：每件物品最多 12 张参考照片，请减少本次选择或删除旧角度。')
            # Validate every new image before writes. Keep compressed display
            # bytes and proposals, not a batch of full-resolution frames.
            prepared = {}
            for digest, content in incoming.items():
                if digest in by_digest:
                    continue
                frame, display_bytes = self.decode(content)
                suggestions, suggestion_error = self._suggest(frame)
                prepared[digest] = (display_bytes, frame.shape[:2], suggestions, suggestion_error)
                del frame
            staged_paths, records = [], []
            try:
                for digest, (display_bytes, (height, width), suggestions, suggestion_error) in prepared.items():
                    content = incoming[digest]
                    reference_id = uuid4().hex
                    original = self._write(f'{reference_id}-original' + ('.png' if content.startswith(b'\x89PNG') else '.jpg'), content)
                    staged_paths.append(original)
                    display = self._write(f'{reference_id}-display.jpg', display_bytes)
                    staged_paths.append(display)
                    records.append(('item_reference_images', {
                        'item_id': item_id, 'path': display, 'original_path': original,
                        'sha256': digest, 'width': width, 'height': height,
                        'features': None, 'region': None, 'region_confirmed': False,
                        'suggested_regions': suggestions, 'status': 'image_saved', 'crop_path': None,
                        'capture_source': capture_source,
                        'quality': {'suggestion_error': suggestion_error, 'target_confirmation_required': True},
                    }, reference_id))
                if records:
                    previous = self.db.get('item_recognition_profiles', item_id) or {}
                    records.append(('item_recognition_profiles', {
                        'item_id': item_id,
                        'profile_version': int(previous.get('profile_version') or 0) + len(records),
                        'status': 'images_saved', 'embeddings': [], 'reference_ids': [],
                        'dimension': None, 'error': None,
                    }, item_id))
                    # References and profile invalidation are one transaction;
                    # a failed batch must not report an error after partial save.
                    saved = self.db.save_many(records)
            except Exception:
                for url in staged_paths:
                    self._path(url).unlink(missing_ok=True)
                raise
            if records:
                self._loaded_versions.pop(item_id, None)
                self._matcher = None
            result = []
            new_rows = {row['sha256']: row for row in saved[:-1]} if records else {}
            for digest in digests:
                if digest in by_digest:
                    result.append({**self.public_reference(by_digest[digest]), 'duplicate': True})
                else:
                    by_digest[digest] = new_rows[digest]
                    result.append(self.public_reference(by_digest[digest]))
            return result

    @staticmethod
    def public_reference(row):
        result = {key: value for key, value in row.items() if key != 'features'}
        result['quality'] = {**(row.get('quality') or {}),
                             'target_confirmation_required': not bool(row.get('region_confirmed'))}
        return result

    def invalidate(self, item_id):
        row = self.db.save('item_recognition_profiles', self._invalidated_profile(item_id), item_id)
        self._loaded_versions.pop(item_id, None)
        self._matcher = None
        return row

    def _invalidated_profile(self, item_id):
        previous = self.db.get('item_recognition_profiles', item_id) or {}
        version = int(previous.get('profile_version') or 0) + 1
        return {
            'item_id': item_id, 'profile_version': version, 'status': 'images_saved',
            'embeddings': [], 'reference_ids': [], 'dimension': None, 'error': None,
        }

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
            updated, _ = self.db.save_many([
                ('item_reference_images', {
                    'region': [x, y, w, h], 'region_confirmed': True,
                    'status': 'region_confirmed', 'features': None,
                }, reference_id),
                ('item_recognition_profiles', self._invalidated_profile(item_id), item_id),
            ])
            self._loaded_versions.pop(item_id, None)
            self._matcher = None
            return self.public_reference(updated)

    def preparation(self):
        status_path = self.model_root / 'appearance-status.json'
        try:
            if status_path.is_file() and status_path.stat().st_size <= 16384:
                return json.loads(status_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'status': 'unknown', 'error': '本地准备状态暂时不可读'}
        return {'status': 'not_prepared'}

    def profile(self, item_id, engines=None, *, item=None, preparation=None):
        if item is None:
            item = self.items_for_inference([self.db.get('items', item_id) or {'id': item_id}])[0]
        row = item['appearance_profile'] or {}
        refs = item['reference_images']
        version = int(row.get('profile_version') or 0)
        loaded = [camera_id for camera_id, engine in (engines or {}).items()
                  if row.get('status') == 'ready' and getattr(engine, 'loaded_profile_versions', {}).get(item_id) == version
                  and getattr(engine, '_applied_profile_revision', None) == getattr(engine, '_profile_revision', None)]
        # Surface the saved detector evidence; GET does not rerun inference or
        # claim that a failed category proposal invalidates a user's identity.
        from services.vision.detectors.appearance import normalize_category
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
            # The registration validator is not a running inference camera.
            'loaded_profile_version': version if loaded else None, 'loaded_camera_ids': loaded,
            'validated_profile_version': self._loaded_versions.get(item_id) if row.get('status') == 'ready' else None,
            'error': row.get('error'),
            'quality_warnings': warnings,
            'model_preparation': self.preparation() if preparation is None else preparation,
            'model_runtime': self._encoder.health() if self._encoder is not None else {'status': 'not_loaded', 'available': False},
        }

    def items_for_inference(self, items=None):
        items = self.db.list('items') if items is None else items
        if not items:
            return []
        ids = [item['id'] for item in items]
        profiles = {row['id']: row for row in self.db.list('item_recognition_profiles', {'id': ids}, limit=len(ids))}
        references = defaultdict(list)
        for row in self.db.list('item_reference_images', {'item_id': ids}, limit=100000):
            references[row['item_id']].append(row)
        return [{**item, 'reference_images': references[item['id']], 'appearance_profile': profiles.get(item['id'])} for item in items]

    def build(self, item_id, *, on_building=None):
        with self.lock:
            refs = self.db.list('item_reference_images', {'item_id': item_id})
            confirmed = [ref for ref in refs if ref.get('region_confirmed') and ref.get('region')]
            if not confirmed:
                raise RegistrationError('至少上传一张照片并确认目标框；保存图片并不等于已建立识别档案。')
            prior = self.db.get('item_recognition_profiles', item_id) or self.invalidate(item_id)
            self.db.save('item_recognition_profiles', {'status': 'building', 'error': None}, item_id)
            self._loaded_versions.pop(item_id, None)
            self._matcher = None
            staged_urls = []
            try:
                # Tell active engines to invalidate the old generation before
                # any expensive feature work; a failed build must not leave
                # the old matcher looking like a newly loaded profile.
                if on_building is not None:
                    on_building()
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
                embeddings, reference_ids, records = [], [], []
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
                    # Never overwrite an earlier generation before the complete
                    # replacement validates and commits. Originals are untouched.
                    crop_url = self._write(f"{ref['id']}-{uuid4().hex}-crop.jpg", encoded.tobytes())
                    staged_urls.append(crop_url)
                    records.append(('item_reference_images', {'crop_path': crop_url, 'status': 'featured',
                        'features': {'backend': health['model_id'], 'model_version': health['model_version'], 'vector': vector}}, ref['id']))
                    embeddings.append(vector)
                    reference_ids.append(ref['id'])
                candidate = {
                    'item_id': item_id, 'profile_version': int(prior['profile_version']), 'status': 'ready',
                    'model_id': health['model_id'], 'model_version': health['model_version'],
                    'dimension': len(embeddings[0]), 'embeddings': embeddings,
                    'reference_ids': reference_ids, 'error': None,
                }
                # Materialize the same matcher contract used by live inference.
                # Validate in memory while concurrent readers still see building,
                # not a ready row whose matcher may subsequently reject it.
                from services.vision.detectors.appearance import ProfileMatcher
                item = self.db.get('items', item_id)
                if not item:
                    raise RegistrationError('物品已不存在，未保存识别档案。')
                matcher = ProfileMatcher([{**item, 'appearance_profile': candidate}], self.encoder)
                if matcher.loaded_profile_versions.get(item_id) != int(prior['profile_version']):
                    raise RegistrationError('特征已生成，但识别服务拒绝该档案版本，未标记可识别。')
                self.db.save_many([*records, ('item_recognition_profiles', candidate, item_id)])
                self._matcher = matcher
                self._loaded_versions[item_id] = int(prior['profile_version'])
            except BaseException as exc:
                for url in staged_urls:
                    try:self._path(url).unlink(missing_ok=True)
                    except OSError:pass
                self.db.save('item_recognition_profiles', {'status': 'failed', 'embeddings': [],
                    'reference_ids': [], 'dimension': None, 'error': str(exc)}, item_id)
                self._loaded_versions.pop(item_id, None)
                if not isinstance(exc, Exception):
                    raise
                raise RegistrationError(f'建立档案失败：{exc}') from exc
            # Only retire known derived crops after the replacement transaction;
            # failed builds retain all prior files, and unknown user paths stay.
            for ref in confirmed:
                old_url = ref.get('crop_path')
                if old_url and str(old_url).endswith('-crop.jpg'):
                    try:
                        old_path = self._path(old_url)
                        if old_path.name.startswith(ref['id'] + '-'):
                            old_path.unlink(missing_ok=True)
                    except (OSError, RegistrationError):pass
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
