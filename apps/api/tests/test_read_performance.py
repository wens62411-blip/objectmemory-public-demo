from collections import Counter
from datetime import datetime, time, timedelta, timezone

from fastapi.testclient import TestClient

from apps.api.app.db import Database
from apps.api.app.main import create_app


def test_multi_value_filters_keep_mode_scope_and_empty_sets(tmp_path):
    db = Database(tmp_path / 'scope.sqlite', 'REAL')
    for record_id, mode, simulated in [('real', 'REAL', False), ('demo', 'DEMO', True)]:
        db.save('events', {'runtime_mode': mode, 'source_type': 'opencv_camera', 'is_simulated': simulated}, record_id)
    filters = {'id': ['demo', 'real', "x') OR 1=1 --"]}
    assert [row['id'] for row in db.list('events', filters)] == ['real']
    assert db.count('events', filters) == 1
    assert db.count('events', filters, unscoped=True) == 2
    assert db.list('events', {'id': []}, unscoped=True) == []
    assert db.count('events', {'id': []}, unscoped=True) == 0
    assert db.count('events', {'is_simulated': [True, False]}, unscoped=True) == 2


def test_event_counts_respect_local_day_offsets_invalid_dates_and_scope(tmp_path):
    db = Database(tmp_path / 'days.sqlite', 'REAL')
    today = datetime.now().astimezone().date()
    local_start = datetime.combine(today, time.min).astimezone()
    stamps = [local_start.astimezone(offset).isoformat() for offset in
              [timezone.utc, timezone(timedelta(hours=14)), timezone(timedelta(hours=-12))]]
    stamps += [today.isoformat(), f'{today}T12:00:00', (local_start - timedelta(seconds=1)).isoformat(), None, 'invalid']
    for index, stamp in enumerate(stamps):
        db.save('events', {'timestamp_start': stamp, 'source_type': 'opencv_camera', 'is_simulated': False}, str(index))
    db.save('events', {'timestamp_start': stamps[0], 'runtime_mode': 'DEMO', 'source_type': 'opencv_camera', 'is_simulated': True}, 'demo')
    db.save('events', {'timestamp_start': stamps[0], 'source_type': 'unknown', 'is_simulated': False}, 'unknown')
    assert db.event_counts(today.isoformat()) == (8, 5)


def test_health_counts_all_rows_without_decoding_event_payloads(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'health', testing=True)
    with TestClient(app) as client:
        db = app.state.runtime.db
        today = datetime.now().astimezone().date().isoformat()
        with db.connect() as conn:
            conn.executemany("INSERT INTO items(id,created_at,updated_at) VALUES (?,'','')", [(str(i),) for i in range(1005)])
            conn.executemany("""INSERT INTO movement_events(id,created_at,updated_at,runtime_mode,timestamp_start,trajectory)
                                VALUES (?,'','','TEST',?,'broken legacy JSON')""", [(str(i), today + 'T12:00:00') for i in range(10005)])
        decode = db._decode

        def no_payload_decode(table, row):
            assert table not in {'items', 'movement_events'}
            return decode(table, row)

        with monkeypatch.context() as spy:
            spy.setattr(db, '_decode', no_payload_decode)
            response = client.get('/api/health')
            assert response.status_code == 200, response.text
            stats = response.json()['stats']
            assert (stats['items'], stats['events'], stats['today_events']) == (1005, 10005, 10005)


def test_item_listing_batches_reads_and_keeps_embeddings_private(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'listing', testing=True)
    with TestClient(app) as client:
        client.get('/api/session')
        service, db = app.state.runtime.registration, app.state.runtime.db
        for index in range(40):
            item_id = str(index)
            db.save('items', {'name': f'手机 {index}', 'type': 'phone'}, item_id)
            # Older invalidated profiles have no item_id; their primary key binds them.
            db.save('item_recognition_profiles', {'status': 'ready', 'profile_version': index + 1,
                    'embeddings': [[.1, .2]], 'reference_ids': [item_id]}, item_id)
            db.save('item_reference_images', {'item_id': item_id, 'features': {'vector': [.1, .2]},
                    'region_confirmed': True, 'suggested_regions': []}, item_id)
        expected = {item['id']: client.get(f"/api/items/{item['id']}").json() for item in db.list('items')}
        calls = Counter()
        read, prepare = db.list, service.preparation

        def counted_list(table, *args, **kwargs):
            calls[table] += 1
            return read(table, *args, **kwargs)

        def counted_preparation():
            calls['preparation'] += 1
            return prepare()

        monkeypatch.setattr(db, 'list', counted_list)
        monkeypatch.setattr(service, 'preparation', counted_preparation)
        response = client.get('/api/items')
        assert response.status_code == 200, response.text
        rows = response.json()
        assert {row['id']: row for row in rows} == expected
        assert calls == {'items': 1, 'item_reference_images': 1, 'item_recognition_profiles': 1, 'preparation': 1}
        assert all('appearance_profile' not in row and 'features' not in row['reference_images'][0] for row in rows)
        # No cross-request cache: the next read must show a profile invalidation.
        service.invalidate('0')
        assert next(row for row in client.get('/api/items').json() if row['id'] == '0')['recognition_profile']['registration_status'] == 'images_saved'


def test_inference_batch_keeps_all_references_past_default_list_cap(tmp_path):
    app = create_app(tmp_path / 'references', testing=True)
    with TestClient(app):
        db, service = app.state.runtime.db, app.state.runtime.registration
        with db.connect() as conn:
            conn.executemany("INSERT INTO items(id,created_at,updated_at) VALUES (?,'','')", [(str(i),) for i in range(1000)])
            conn.executemany("INSERT INTO item_reference_images(id,item_id,created_at,updated_at) VALUES (?,?,'','')",
                             [(f'{i}:{j}', str(i)) for i in range(1000) for j in range(2)])
        items = service.items_for_inference()
        assert len(items) == 1000 and all(len(item['reference_images']) == 2 for item in items)
