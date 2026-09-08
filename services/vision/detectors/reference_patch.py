"""Bounded registered-photo patch proposals; never final identity/event proof.

Uses the already loaded local appearance encoder, with no network, downloads,
camera ownership or database/media writes. Foreground patches compete with the
same registration photo's background, instead of requiring a COCO phone box.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageOps

from .appearance import DIMENSION, ROOT, ProfileMatcher, normalize_category


@dataclass(frozen=True)
class ReferencePatchSettings:
    max_items: int = 8
    max_references: int = 12
    background_exclusion: float = .83
    foreground_threshold: float = .80
    background_margin: float = .06
    min_patches: int = 8
    min_reference_matches: int = 6
    top_patch_count: int = 8
    top_patch_threshold: float = .82
    nms_iou: float = .35
    identity_margin: float = .06

    def __post_init__(self):
        bounds = {'max_items': (1, 32), 'max_references': (1, 12),
                  'min_patches': (8, 128), 'min_reference_matches': (6, 128),
                  'top_patch_count': (8, 128), 'background_exclusion': (.1, 1),
                  'foreground_threshold': (.80, 1), 'background_margin': (.06, 1),
                  'top_patch_threshold': (.82, 1), 'nms_iou': (.1, .9),
                  'identity_margin': (.06, 1)}
        integer_fields = {'max_items', 'max_references', 'min_patches',
                          'min_reference_matches', 'top_patch_count'}
        for key, (low, high) in bounds.items():
            value = getattr(self, key)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not low <= value <= high or (key in integer_fields and type(value) is not int)):
                raise ValueError('Invalid reference patch setting: '+key)


def _overlap(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[0]+a[2], b[0]+b[2]), min(a[1]+a[3], b[1]+b[3])
    intersection = max(0., x2-x1)*max(0., y2-y1)
    area_a, area_b = a[2]*a[3], b[2]*b[3]
    return intersection/max(1e-9, area_a+area_b-intersection), intersection/max(1e-9, min(area_a, area_b))


def _similarity(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    # These matrices have at most 256 rows and 384 columns. Avoid starting a
    # machine-wide BLAS worker team for each tiny patch-comparison operation;
    # this is local to this kernel and does not alter other libraries' threads.
    return np.einsum('ij,kj->ik', a, b, optimize=False)


class ReferencePatchProposer:
    backend = 'dinov2_reference_patches'

    def __init__(self, items: list[dict], encoder, reference_root: str | Path | None = None, settings: dict | None = None):
        self.config = ReferencePatchSettings(**(settings or {}))
        self.encoder = encoder
        self._loaded_model_id, self._loaded_model_version = encoder.model_id, encoder.model_version
        self.reference_root = Path(reference_root or ROOT/'data/registered-items').resolve()
        self.loaded_profile_versions: dict[str, int] = {}
        self.invalid_profiles: dict[str, str] = {}
        self._references: list[dict] = []
        self._error: str | None = None
        self._last_query_encodes = 0
        self._last_query_model_calls = 0
        if not encoder.health().get('available') or not callable(getattr(encoder, 'encode_patches', None)):
            self._error = 'patch_encoder_unavailable'
            return
        matcher = ProfileMatcher(items, encoder)
        self.invalid_profiles.update(matcher.invalid_profiles)
        for item in items:
            item_id = str(item.get('id') or '')
            if item_id not in matcher.loaded_profile_versions:
                continue
            if item_id in self.loaded_profile_versions:
                continue
            if len(self.loaded_profile_versions) >= self.config.max_items:
                self.invalid_profiles[item_id] = 'item_limit_exceeded'
                continue
            try:
                profile = item['appearance_profile']
                ids = profile.get('reference_ids')
                if (not isinstance(ids, list) or not 1 <= len(ids) <= self.config.max_references
                        or any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids)):
                    raise ValueError('invalid_profile_reference_ids')
                rows = item.get('reference_images')
                if not isinstance(rows, list):
                    raise ValueError('references_missing')
                indexed = {row.get('id'): row for row in rows if isinstance(row, dict)}
                if len(indexed) != len(rows):
                    raise ValueError('duplicate_reference_ids')
                built = [self._prepare_reference(item, indexed[reference_id]) for reference_id in ids]
                usable = [entry for entry in built if len(entry['foreground']) >= self.config.min_reference_matches]
                if not usable:
                    raise ValueError('insufficient_distinct_foreground_patches')
                self._references.extend(usable)
                self.loaded_profile_versions[item_id] = profile['profile_version']
            except (ValueError, TypeError, KeyError, OSError) as error:
                self.invalid_profiles[item_id] = str(error) if isinstance(error, ValueError) else type(error).__name__
            except Exception as error:
                self.invalid_profiles[item_id] = 'reference_encoding_failed:'+type(error).__name__

    def _reference_path(self, value: Any) -> Path:
        prefix = '/media/registered-items/'
        if not isinstance(value, str) or not value.startswith(prefix) or '\\' in value:
            raise ValueError('reference_path_outside_registration')
        relative = value[len(prefix):]
        if not relative or Path(relative).is_absolute():
            raise ValueError('reference_path_outside_registration')
        path = (self.reference_root/relative).resolve()
        if not path.is_relative_to(self.reference_root) or path == self.reference_root:
            raise ValueError('reference_path_outside_registration')
        if not path.is_file() or path.stat().st_size > 12*1024*1024:
            raise ValueError('reference_file_missing_or_oversized')
        return path

    def _patches(self, image):
        return self._validate_patches(image, self.encoder.encode_patches(image))

    @staticmethod
    def _validate_patches(image, encoded):
        features, xy, patch_size = encoded
        features, xy = np.asarray(features, np.float32), np.asarray(xy, np.float32)
        size = np.asarray(patch_size, np.float32)
        if (features.ndim != 2 or features.shape[1] != DIMENSION or not 1 <= len(features) <= 256
                or xy.shape != (len(features), 2) or size.shape != (2,)
                or not np.isfinite(features).all() or not np.isfinite(xy).all()
                or not np.isfinite(size).all() or np.any(size <= 0)
                or not np.allclose(np.linalg.norm(features, axis=1), 1, atol=1e-3)):
            raise ValueError('invalid_patch_encoder_contract')
        height, width = image.shape[:2]
        if np.any(xy < 0) or np.any(xy[:, 0] > width) or np.any(xy[:, 1] > height):
            raise ValueError('patch_coordinates_outside_source')
        return features, xy, size

    def _prepare_reference(self, item, row):
        if row.get('item_id') != item['id'] or type(row.get('region_confirmed')) not in (bool, int) or row['region_confirmed'] != 1:
            raise ValueError('reference_region_not_confirmed')
        profile = item['appearance_profile']
        if row['id'] not in profile['reference_ids']:
            raise ValueError('reference_profile_mismatch')
        original = self._reference_path(row.get('original_path'))
        original_bytes = original.read_bytes()
        if hashlib.sha256(original_bytes).hexdigest() != row.get('sha256'):
            raise ValueError('reference_original_hash_mismatch')
        display = self._reference_path(row.get('path'))
        content = display.read_bytes()
        with Image.open(io.BytesIO(content)) as display_header:
            if display_header.width*display_header.height > 16_777_216 or min(display_header.size) < 32:
                raise ValueError('reference_display_invalid')
            display_size = display_header.size
        # Reconstruct RegistrationService.decode's EXIF-normalized quality-95
        # display from the hash-verified original. Same-directory membership
        # alone cannot authorize swapping in an unrelated display photograph.
        with Image.open(io.BytesIO(original_bytes)) as source:
            if source.width*source.height > 16_777_216:
                raise ValueError('reference_original_oversized')
            oriented = ImageOps.exif_transpose(source).convert('RGB')
            if oriented.size != display_size:
                raise ValueError('reference_display_not_bound_to_original')
            normalized = io.BytesIO()
            oriented.save(normalized, format='JPEG', quality=95)
        displayed_image = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
        image = cv2.imdecode(np.frombuffer(normalized.getvalue(), np.uint8), cv2.IMREAD_COLOR)
        if displayed_image is None or image is None:
            raise ValueError('reference_display_invalid')
        if image.shape != displayed_image.shape or not np.array_equal(image, displayed_image):
            raise ValueError('reference_display_not_bound_to_original')
        roi = row.get('region')
        if (not isinstance(roi, list) or len(roi) != 4
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in roi)):
            raise ValueError('reference_region_invalid')
        x, y, width, height = roi
        if min(x, y) < 0 or min(width, height) <= 0 or x+width > 1 or y+height > 1:
            raise ValueError('reference_region_invalid')
        ih, iw = image.shape[:2]
        x1, y1, x2, y2 = round(x*iw), round(y*ih), round((x+width)*iw), round((y+height)*ih)
        if min(x2-x1, y2-y1) < 32:
            raise ValueError('reference_region_too_small')
        foreground, _, _ = self._patches(image[y1:y2, x1:x2])
        all_features, xy, _ = self._patches(image)
        outside = (xy[:, 0] < x1) | (xy[:, 0] > x2) | (xy[:, 1] < y1) | (xy[:, 1] > y2)
        background = all_features[outside]
        if len(background) < 8:
            raise ValueError('reference_background_insufficient')
        foreground = foreground[_similarity(foreground, background).max(axis=1) < self.config.background_exclusion]
        foreground.setflags(write=False)
        background.setflags(write=False)
        return {'item_id': item['id'], 'category': normalize_category(item['type']),
                'profile_version': profile['profile_version'], 'reference_id': row['id'],
                'original_sha256': row['sha256'], 'display_sha256': hashlib.sha256(content).hexdigest(),
                'foreground': foreground, 'background': background}

    def health(self):
        version_matches = (self.encoder.model_id == self._loaded_model_id and self.encoder.model_version == self._loaded_model_version)
        return {'available': bool(self._references) and bool(self.encoder.health().get('available')) and version_matches,
                'backend': self.backend, 'proposal_only': True, 'error': self._error if version_matches else 'encoder_version_changed',
                'loaded_profile_versions': dict(self.loaded_profile_versions),
                'invalid_profiles': dict(self.invalid_profiles), 'reference_count': len(self._references),
                'last_query_encodes': self._last_query_encodes,
                'last_query_model_calls': self._last_query_model_calls,
                'model_id': self.encoder.model_id, 'model_version': self.encoder.model_version}

    def _clusters(self, features, xy, size, reference, offset, image_shape):
        scores = _similarity(features, reference['foreground'])
        closest = scores.argmax(axis=1)
        best = scores[np.arange(len(scores)), closest]
        background = _similarity(features, reference['background']).max(axis=1)
        selected = np.flatnonzero((best >= self.config.foreground_threshold) & (best-background >= self.config.background_margin))
        if len(selected) < self.config.min_patches:
            return []
        # Eight-neighbour components on actual token-cell coordinates. Never
        # connect distant islands simply because their appearance is similar.
        grid = np.rint((xy-xy.min(axis=0))/size).astype(np.int32)
        cells = {tuple(grid[index]): int(index) for index in selected}
        components = []
        while cells:
            cell, index = cells.popitem()
            component, pending = [index], [cell]
            while pending:
                cx, cy = pending.pop()
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        neighbour = (cx+dx, cy+dy)
                        if neighbour in cells:
                            component.append(cells.pop(neighbour))
                            pending.append(neighbour)
            components.append(component)
        result = []
        ih, iw = image_shape
        for component in components:
            if len(component) < self.config.min_patches or len(set(closest[component])) < self.config.min_reference_matches:
                continue
            confidence = float(np.sort(best[component])[-self.config.top_patch_count:].mean())
            if confidence < self.config.top_patch_threshold:
                continue
            positions = xy[component] + offset
            left, top = np.maximum(0, positions.min(axis=0)-size/2)
            right, bottom = np.minimum([iw, ih], positions.max(axis=0)+size/2)
            result.append({'bbox': [float(left), float(top), float(right-left), float(bottom-top)],
                'coordinate_space': 'source_pixels', 'score': confidence, 'best_score': confidence,
                'category': reference['category'], 'best_item_id': reference['item_id'], 'item_id': None,
                'reference_id': reference['reference_id'], 'profile_version': reference['profile_version'],
                'model_id': self.encoder.model_id, 'model_version': self.encoder.model_version,
                'proposal_backend': self.backend, 'category_evidence': 'registered_reference_patch',
                'accepted': False, 'proposal_only': True, 'eligible': True, 'rejection': None,
                'patch_count': len(component), 'unique_reference_matches': len(set(closest[component])),
                'foreground_background_margin': float((best[component]-background[component]).min()),
                'reference_original_sha256': reference['original_sha256'],
                'reference_display_sha256': reference['display_sha256'],
                'identity_confirmed': False, 'observation_evidence': False})
        return result

    def detect(self, frame: np.ndarray) -> list[dict]:
        self._last_query_encodes = 0
        self._last_query_model_calls = 0
        if not self.health()['available']:
            return []
        if (not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.ndim != 3
                or frame.shape[2] != 3 or min(frame.shape[:2]) < 32 or frame.shape[0]*frame.shape[1] > 16_777_216):
            self._error = 'query_frame_invalid'
            return []
        ih, iw = frame.shape[:2]
        width = iw//2
        windows = [(0, 0, iw, ih), (0, 0, width, ih), ((iw-width)//2, 0, width, ih), (iw-width, 0, width, ih)]
        proposals = []
        try:
            batch_encoder = getattr(self.encoder, 'encode_patches_batch', None)
            images = [frame[y:y+h, x:x+w] for x, y, w, h in windows]
            if callable(batch_encoder):
                batches = batch_encoder(images)
                self._last_query_model_calls = 1
                if not isinstance(batches, (tuple, list)) or len(batches) != len(windows):
                    raise ValueError('invalid_patch_batch_contract')
            else:
                batches = [self.encoder.encode_patches(image) for image in images]
                self._last_query_model_calls = len(images)
            for (x, y, w, h), image, encoded in zip(windows, images, batches):
                features, xy, size = self._validate_patches(image, encoded)
                self._last_query_encodes += 1
                for reference in self._references:
                    proposals.extend(self._clusters(features, xy, size, reference, np.array([x, y]), (ih, iw)))
            kept = []
            for row in sorted(proposals, key=lambda candidate: (-candidate['score'], -candidate['patch_count'])):
                if not any(row['best_item_id'] == prior['best_item_id'] and
                           (_overlap(row['bbox'], prior['bbox'])[0] >= self.config.nms_iou or
                            _overlap(row['bbox'], prior['bbox'])[1] >= .7) for prior in kept):
                    kept.append(row)
            for row in kept:
                if sum(prior['best_item_id'] == row['best_item_id'] for prior in kept) > 1:
                    row.update(eligible=False, rejection='multiple_spatial_matches')
                for other in kept:
                    if row['best_item_id'] != other['best_item_id'] and _overlap(row['bbox'], other['bbox'])[1] >= .5:
                        if row['score']-other['score'] < self.config.identity_margin:
                            row.update(eligible=False, rejection='ambiguous_registered_item')
            self._error = None
            return kept
        except Exception as error:
            self._error = 'query_encoding_failed:'+type(error).__name__
            return []
