"""Explicit local profile-version migration; preserves confirmed crops/originals.

Only the existing REAL database is backed up and only mismatched confirmed
profiles are rebuilt via the real API. No images leave localhost.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.vision.detectors.appearance import MODEL_ID, MODEL_VERSION


def originals():
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (ROOT / 'data' / 'registered-items').glob('*-original.*') if path.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8018')
    parser.add_argument('--apply', action='store_true', help='Backup once, then explicitly rebuild incompatible profiles')
    args = parser.parse_args()
    if urlsplit(args.url).hostname not in {'127.0.0.1', 'localhost', '::1'}:
        parser.error('Only localhost is allowed')
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'apply': args.apply,
              'model_version': MODEL_VERSION, 'household_images_exported': False, 'profiles': []}
    with httpx.Client(base_url=args.url, timeout=60, trust_env=False) as client:
        session = client.get('/api/session'); session.raise_for_status()
        if session.json().get('runtime_mode') != 'REAL':
            raise RuntimeError('This migration only targets the existing REAL service')
        database = ROOT / 'data/database/objectmemory.sqlite'
        storage = client.get('/api/storage/status'); storage.raise_for_status()
        if Path(storage.json().get('database_path') or '').resolve() != database.resolve():
            raise RuntimeError('Server database does not match the database being backed up')
        response = client.get('/api/items'); response.raise_for_status()
        selected = []
        for item in response.json():
            response = client.get(f"/api/items/{item['id']}/profile"); response.raise_for_status()
            profile = response.json()
            if profile.get('confirmed_reference_count', 0) and (
                profile.get('model_version') != MODEL_VERSION or profile.get('model_id') != MODEL_ID
            ):
                selected.append((item['id'], profile))
        report['selected_count'] = len(selected)
        before = originals()
        if args.apply and selected:
            destination = ROOT / 'data/backups/pre-audit-latest.sqlite'
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix('.sqlite.pending')
            for path in (database, destination, temporary):
                if path.is_symlink() or not path.resolve().is_relative_to((ROOT / 'data').resolve()):
                    raise RuntimeError('Unsafe database backup path')
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(temporary)) as backup:
                    source.backup(backup)
                    if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise RuntimeError('Backup failed integrity check; no profiles changed')
            os.replace(temporary, destination)
            report['backup'] = str(destination.relative_to(ROOT))
            report['backup_bytes'] = destination.stat().st_size
            for item_id, prior in selected:
                response = client.post(f'/api/items/{item_id}/profile/build')
                response.raise_for_status()
                updated = response.json()
                if updated.get('registration_status') != 'ready' or updated.get('model_version') != MODEL_VERSION:
                    raise RuntimeError('Profile did not become ready under the new model contract')
                report['profiles'].append({'item_id': item_id, 'before_version': prior['profile_version'],
                    'after_version': updated['profile_version'], 'model_version': updated['model_version'],
                    'confirmed_reference_count': updated['confirmed_reference_count']})
        report['originals_count'] = len(before)
        report['originals_unchanged'] = before == originals()
        if not report['originals_unchanged']:
            raise RuntimeError('Reference originals changed unexpectedly')
    output = ROOT / 'data/verification' / ('phone-profile-migration.json' if args.apply else 'phone-profile-migration-check.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
