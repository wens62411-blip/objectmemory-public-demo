"""Artifact cleanup must not turn project-local links into user-file targets."""
from contextlib import closing, contextmanager
import importlib.util
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('cleanup_artifact_link_tests', ROOT/'scripts/cleanup-audit-artifacts.py')
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup, 'ROOT', tmp_path)
    monkeypatch.setattr(cleanup, 'TEMPORARY', tmp_path/'data/temporary')
    reference = tmp_path/'data/registered-items/original.jpg'
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b'isolated registered reference - must remain')
    return tmp_path, reference


@contextmanager
def directory_link(link, target, boundary):
    # Both ends are created by THIS tmp_path test, never a real project path.
    assert link.absolute().is_relative_to(boundary.resolve())
    assert target.resolve().is_relative_to(boundary.resolve())
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        result = subprocess.run(['pwsh', '-NoProfile', '-Command',
            '$ErrorActionPreference="Stop"; New-Item -ItemType Junction -Path $env:OM_TEST_LINK -Target $env:OM_TEST_TARGET | Out-Null'],
            env={**os.environ, 'OM_TEST_LINK': str(link), 'OM_TEST_TARGET': str(target)},
            capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, result.stderr
        assert link.is_junction()
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        yield
    finally:
        if os.name == 'nt':
            # rmdir removes this junction entry; never recurse into its target.
            if link.is_junction(): link.rmdir()
        elif link.is_symlink():
            link.unlink()


@pytest.mark.parametrize('placement', ['cache_itself', 'ancestor', 'cache_child'])
def test_original_path_ancestors_and_subtree_links_are_rejected(sandbox, placement):
    root, reference = sandbox
    if placement == 'cache_itself':
        link = root/'services/__pycache__'
    elif placement == 'ancestor':
        link = root/'services'
        (reference.parent/'__pycache__').mkdir()
    else:
        link = root/'services/__pycache__/nested'
    with directory_link(link, reference.parent, root):
        with pytest.raises(RuntimeError, match='(?i)(reparse|symlink|link)'):
            cleanup.build_plan([])
        assert reference.read_bytes() == b'isolated registered reference - must remain'


def test_cached_plan_cannot_follow_a_replaced_parent(sandbox):
    root, reference = sandbox
    cache = root/'services/__pycache__'
    cache.mkdir(parents=True)
    candidate = cache/'original.jpg'
    candidate.write_bytes(b'rebuildable fixture')
    plan = cleanup.build_plan([])
    candidate.unlink()
    cache.rmdir()
    with directory_link(cache, reference.parent, root):
        deleted, size, errors = cleanup.apply_plan(plan, [])
        assert deleted == size == 0 and errors
        assert reference.read_bytes() == b'isolated registered reference - must remain'


def test_forged_cache_row_cannot_delete_registered_image(sandbox):
    _root, reference = sandbox
    plan = [{'category': 'rebuildable_cache', 'path': str(reference), 'bytes': reference.stat().st_size}]
    deleted, size, errors = cleanup.apply_plan(plan, [])
    assert deleted == size == 0 and errors
    assert reference.is_file()


def test_real_cache_cleanup_preserves_reference(sandbox):
    root, reference = sandbox
    cached = root/'services/__pycache__/module.pyc'
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b'rebuildable bytecode')
    plan = cleanup.build_plan([])
    deleted, size, errors = cleanup.apply_plan(plan, [])
    assert deleted == 1 and size == len(b'rebuildable bytecode') and not errors
    assert not cached.exists()
    assert reference.read_bytes() == b'isolated registered reference - must remain'


def test_inactive_crash_wal_with_committed_data_is_not_a_cache(sandbox):
    root, reference = sandbox
    database = root/'data/database/objectmemory.sqlite'
    database.parent.mkdir(parents=True)
    # This child deliberately exits without SQLite.close(), leaving a real
    # committed WAL. No user database/process is involved or terminated.
    child = subprocess.run([sys.executable, '-c',
        'import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); '
        'c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA wal_autocheckpoint=0"); '
        'c.execute("CREATE TABLE evidence(value TEXT)"); c.commit(); '
        'c.execute("PRAGMA wal_checkpoint(TRUNCATE)"); '
        'c.execute("INSERT INTO evidence VALUES(\'must-survive\')"); c.commit(); os._exit(0)',
        str(database)], capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    assert child.returncode == 0
    wal, shm = Path(str(database)+'-wal'), Path(str(database)+'-shm')
    assert wal.stat().st_size > 0
    before = {path: path.read_bytes() for path in (database, wal, shm)}
    # Even an old/forged plan listing only SHM must protect the nonempty WAL's
    # companion, because byte counts from a previous scan are not proof.
    plan = [{'category': 'transient_sqlite_sidecar', 'path': str(shm), 'bytes': shm.stat().st_size}]
    deleted, size, errors = cleanup.apply_plan(plan, [])
    assert deleted == size == 0 and errors
    assert 'nonempty WAL' in errors[0]['error']
    assert {path: path.read_bytes() for path in before} == before
    assert not any(row['category'] == 'transient_sqlite_sidecar' for row in cleanup.build_plan([]))
    with closing(sqlite3.connect(database)) as check:
        assert check.execute('SELECT value FROM evidence').fetchall() == [('must-survive',)]
    assert reference.is_file()


def test_wal_grown_since_empty_scan_blocks_the_entire_stale_plan(sandbox):
    root, _reference = sandbox
    database = root/'data/database/objectmemory.sqlite'
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as check:
        check.execute('CREATE TABLE evidence(value TEXT)')
        check.commit()
    wal = Path(str(database)+'-wal')
    wal.write_bytes(b'')
    cache = root/'services/__pycache__/cache.pyc'
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b'rebuildable cache')
    plan = cleanup.build_plan([])
    wal.write_bytes(b'new nonempty WAL must be preserved even if malformed')
    deleted, size, errors = cleanup.apply_plan(plan, [])
    assert deleted == size == 0 and errors
    assert 'nonempty WAL' in errors[0]['error']
    assert wal.read_bytes().startswith(b'new nonempty WAL')
    assert cache.is_file()


def test_late_safety_refusal_does_not_hide_already_deleted_test_cache(sandbox, monkeypatch):
    root, reference = sandbox
    cache = root/'services/__pycache__'
    cache.mkdir(parents=True)
    first, second = cache/'first.pyc', cache/'second.pyc'
    first.write_bytes(b'first')
    second.write_bytes(b'second')
    plan = cleanup.build_plan([])
    original_check = cleanup.deletion_path

    def late_refusal(path, category):
        if Path(path) == second and not first.exists():
            raise RuntimeError('controlled late safety refusal')
        return original_check(path, category)

    monkeypatch.setattr(cleanup, 'deletion_path', late_refusal)
    deleted, size, errors = cleanup.apply_plan(plan, [])
    assert deleted == 1 and size == len(b'first') and errors
    assert not first.exists() and second.is_file() and reference.is_file()
