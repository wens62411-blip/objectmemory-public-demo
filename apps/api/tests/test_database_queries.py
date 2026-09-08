from contextlib import contextmanager
import sqlite3

import pytest

from apps.api.app.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'queries.sqlite', 'REAL')
    for record_id, mode, source, simulated in [
        ('real', 'REAL', 'opencv_camera', False),
        ('demo', 'DEMO', 'demo_seed', True),
        ('simulated', 'REAL', 'opencv_camera', True),
        ('unknown', 'REAL', None, False),
    ]:
        database.save('events', {
            'runtime_mode': mode, 'source_type': source,
            'is_simulated': simulated, 'pinned': record_id == 'real',
        }, record_id)
    return database


@pytest.mark.parametrize('unscoped', [False, True])
@pytest.mark.parametrize('filters', [
    None, {'pinned': False}, {'pinned': True}, {'runtime_mode': 'DEMO'},
    {'source_type': None, 'invalid"column': 'ignored'},
])
def test_list_and_count_share_filters_and_mode_scope(db, unscoped, filters):
    rows = db.list('events', filters, unscoped=unscoped)
    assert db.count('events', filters, unscoped=unscoped) == len(rows)
    if not unscoped:
        assert all(row['id'] == 'real' for row in rows)


def test_bulk_delete_keeps_scope_aliases_duplicates_and_missing_ids(db):
    ids = ['real', 'real', 'missing', 'demo', 'simulated', 'unknown']
    assert db.delete_many('events', iter(ids)) == 1
    assert db.count('events', unscoped=True) == 3
    assert db.delete_many('events', iter(ids), unscoped=True) == 3
    assert db.count('events', unscoped=True) == 0


def test_bulk_delete_uses_one_transaction_for_more_than_999_ids(db, monkeypatch):
    ids = [f'item-{index}' for index in range(1200)]
    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO items(id, created_at, updated_at) VALUES (?, '', '')",
            [(record_id,) for record_id in ids],
        )
    original_connect = db.connect
    transactions = 0

    @contextmanager
    def counted_connect():
        nonlocal transactions
        transactions += 1
        with original_connect() as conn:
            yield conn

    monkeypatch.setattr(db, 'connect', counted_connect)
    assert db.delete_many('items', iter(ids)) == len(ids)
    assert transactions == 1
    assert db.delete_many('items', []) == 0
    assert transactions == 1
    assert db.count('items') == 0


def test_bulk_delete_rolls_back_entire_batch_on_failure(db):
    with db.connect() as conn:
        conn.execute("""CREATE TRIGGER prevent_delete BEFORE DELETE ON movement_events
                        WHEN OLD.id='demo' BEGIN SELECT RAISE(ABORT, 'protected'); END""")
    with pytest.raises(sqlite3.IntegrityError, match='protected'):
        db.delete_many('events', ['real', 'demo'], unscoped=True)
    assert db.count('events', unscoped=True) == 4


def test_bulk_delete_lock_retry_replays_generator_after_rollback(db, monkeypatch):
    original_connect = db.connect
    attempts = 0

    @contextmanager
    def temporarily_locked():
        nonlocal attempts
        attempts += 1
        with original_connect() as conn:
            yield conn
            if attempts == 1:
                raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(db, 'connect', temporarily_locked)
    assert db.delete_many('events', iter(['real', 'demo']), unscoped=True) == 2
    assert attempts == 2
    assert db.count('events', unscoped=True) == 2


def test_save_many_rolls_back_cross_table_batch_and_preserves_existing_rows(db):
    db.save('items', {'name': 'original'}, 'existing')
    with db.connect() as conn:
        conn.execute("""CREATE TRIGGER reject_profile BEFORE INSERT ON item_recognition_profiles
            BEGIN SELECT RAISE(ABORT, 'controlled batch failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match='controlled batch failure'):
        db.save_many(iter([
            ('items', {'name': 'changed'}, 'existing'),
            ('item_reference_images', {'item_id': 'existing'}, 'new-reference'),
            ('item_recognition_profiles', {'item_id': 'existing'}, 'existing'),
        ]))
    assert db.get('items', 'existing')['name'] == 'original'
    assert db.count('item_reference_images') == 0


def test_save_many_keeps_aliases_schema_filtering_defaults_and_lock_retries(db, monkeypatch):
    original_connect = db.connect
    attempts = 0

    @contextmanager
    def temporarily_locked():
        nonlocal attempts
        attempts += 1
        with original_connect() as conn:
            yield conn
            if attempts == 1:
                raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(db, 'connect', temporarily_locked)
    rows = db.save_many(iter([
        ('events', {'source_type': 'opencv_camera', 'is_simulated': False,
                    'trajectory': [{'x': 1}], 'not_a_column': 'ignored'}, 'saved'),
        ('items', {'name': 'new item', 'unknown_field': True}, None),
    ]))
    assert attempts == 2
    assert rows[0]['runtime_mode'] == 'REAL' and rows[0]['trajectory'] == [{'x': 1}]
    assert rows[0]['is_simulated'] is False and rows[0]['id'] == 'saved'
    assert rows[1]['id'] and rows[1]['name'] == 'new item'
    assert 'not_a_column' not in rows[0] and 'unknown_field' not in rows[1]
    assert db.count('items') == 1
    assert db.save_many([]) == []
