"""Isolated filesystem checks: never inspect or prune the real firmware tree."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import os

import pytest

from apps.api.app import retention as module


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    root.mkdir()
    monkeypatch.setattr(module, '__file__', str(root / 'apps/api/app/retention.py'))
    return root


def service(project, mode, data_root=None):
    data = data_root if data_root is not None else project / 'data'
    data.mkdir(parents=True, exist_ok=True)
    return module.RetentionService(SimpleNamespace(runtime_mode=mode), data, data / 'media',
                                   recover_pending_on_start=False)


@pytest.mark.parametrize('mode', ['REAL', 'DEMO'])
def test_only_canonical_data_runtime_manages_shared_firmware(project, mode):
    assert service(project, mode).manage_project_firmware is True
    isolated = service(project, mode, project / 'data/verification/isolated-runtime')
    assert isolated.manage_project_firmware is False


def test_test_mode_never_manages_shared_firmware_even_at_canonical_path(project):
    assert service(project, 'TEST').manage_project_firmware is False


def put(project, relative, value):
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return path


def seed_firmware(project):
    return [
        put(project, 'artifacts/firmware/manifest.json', b'ai-public'),
        put(project, 'artifacts/firmware/builds/previous/firmware.bin', b'ai-archive'),
        put(project, 'artifacts/firmware/xiao-esp32s3-sense/manifest.json', b'xiao-public'),
        put(project, 'artifacts/firmware/xiao-esp32s3-sense/builds/previous/firmware.bin', b'xiao-archive'),
        put(project, 'firmware/esp32cam/build/firmware.bin', b'ai-current'),
        put(project, 'firmware/esp32cam/build-xiao-esp32s3-sense/firmware.bin', b'xiao-current'),
    ]


def test_canonical_statistics_include_both_current_bundles_and_archives_exactly_once(project):
    paths = seed_firmware(project)
    paths.append(put(project, 'data/firmware-builds/local/build.log', b'local-build'))
    runtime = service(project, 'REAL')
    expected = sum(path.stat().st_size for path in paths)
    assert runtime._firmware_artifact_bytes() == expected
    assert runtime._managed_total() == expected


@pytest.mark.parametrize('mode', ['REAL', 'DEMO', 'TEST'])
def test_isolated_runtime_does_not_count_or_prune_shared_bundles(project, mode):
    paths = seed_firmware(project)
    # Old-looking files without a valid manifest must also survive an isolated
    # runtime's startup retention. No real data directory is ever passed here.
    for board in ['artifacts/firmware/builds', 'artifacts/firmware/xiao-esp32s3-sense/builds']:
        paths.append(put(project, board + '/older/manifest.json', b'{"compile_passed":true}'))
        paths.append(put(project, board + '/newer/manifest.json', b'{"compile_passed":true}'))
    before = {str(path): path.read_bytes() for path in paths}
    isolated_data = project / 'isolated-data'
    local = put(isolated_data, 'firmware-builds/local/build.log', b'isolated-owned')
    runtime = service(project, mode, isolated_data)
    assert runtime._firmware_artifact_bytes() == local.stat().st_size
    assert runtime._managed_total() == local.stat().st_size
    runtime._trim_firmware_builds()
    assert {str(path): path.read_bytes() for path in paths} == before


def test_canonical_data_reparse_entry_cannot_enable_shared_cleanup(project, monkeypatch):
    data = project / 'data'
    original = module._is_link_or_reparse
    monkeypatch.setattr(module, '_is_link_or_reparse', lambda path: Path(path) == data or original(path))
    assert service(project, 'REAL').manage_project_firmware is False


def test_alias_to_canonical_data_does_not_gain_shared_cleanup_authority(project):
    data = project / 'data'
    data.mkdir()
    alias = project / 'data-alias'
    try:
        alias.symlink_to(data, target_is_directory=True)
    except OSError as error:
        pytest.skip(f'Symlink creation unavailable on this test host: {error.winerror if hasattr(error, "winerror") else error.errno}')
    assert service(project, 'REAL', alias).manage_project_firmware is False


def test_common_build_lock_keeps_all_bundles_untouched(project):
    paths = seed_firmware(project)
    paths.append(put(project, 'artifacts/firmware/.build.lock', b'explicit-test-lock'))
    before = {str(path): path.read_bytes() for path in paths}
    service(project, 'REAL')._trim_firmware_builds()
    assert {str(path): path.read_bytes() for path in paths} == before


def test_canonical_runtime_never_prunes_shared_archives_without_builder_lock(project):
    paths = seed_firmware(project)
    for board in ['artifacts/firmware/builds', 'artifacts/firmware/xiao-esp32s3-sense/builds']:
        for name in ['older', 'newer', 'unknown-user-folder']:
            paths.append(put(project, f'{board}/{name}/manifest.json', b'{"compile_passed":true}'))
    before = {str(path): path.read_bytes() for path in paths}
    service(project, 'REAL')._trim_firmware_builds()
    assert {str(path): path.read_bytes() for path in paths} == before


def legacy_bundle(project, name, age):
    folder = project / 'data/firmware-builds' / name
    blob = b'explicit-test-build-only'
    put(folder, 'firmware.bin', blob)
    put(folder, 'manifest.json', json.dumps({'compile_passed': True, 'artifacts': [
        {'name': 'firmware.bin', 'sha256': hashlib.sha256(blob).hexdigest()}
    ]}).encode())
    os.utime(folder, (age, age))
    return folder


def test_local_legacy_cleanup_keeps_latest_two_and_unknown_user_files(project):
    old = legacy_bundle(project, 'old', 100)
    middle = legacy_bundle(project, 'middle', 200)
    latest = legacy_bundle(project, 'latest', 300)
    unknown = legacy_bundle(project, 'with-user-file', 90)
    put(unknown, 'do-not-delete.txt', b'user-owned')
    damaged = legacy_bundle(project, 'mismatched-hash', 80)
    put(damaged, 'firmware.bin', b'does-not-match-manifest')
    invalid = put(project, 'data/firmware-builds/no-valid-manifest/notes.txt', b'unknown-purpose')
    service(project, 'REAL')._trim_firmware_builds()
    assert not old.exists()
    assert middle.exists() and latest.exists()
    assert (unknown / 'do-not-delete.txt').read_bytes() == b'user-owned'
    assert damaged.exists() and invalid.exists()


@pytest.mark.parametrize('linked_entry', ['root', 'folder', 'manifest', 'artifact'])
def test_local_legacy_link_or_reparse_entries_are_not_deleted(project, monkeypatch, linked_entry):
    old = legacy_bundle(project, 'old', 100)
    legacy_bundle(project, 'middle', 200)
    legacy_bundle(project, 'latest', 300)
    candidate = {'root': old.parent, 'folder': old, 'manifest': old / 'manifest.json', 'artifact': old / 'firmware.bin'}[linked_entry]
    original = module._is_link_or_reparse
    monkeypatch.setattr(module, '_is_link_or_reparse', lambda path: Path(path) == candidate or original(path))
    service(project, 'REAL')._trim_firmware_builds()
    assert (old / 'firmware.bin').read_bytes() == b'explicit-test-build-only'
