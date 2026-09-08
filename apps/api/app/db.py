"""SQLite repository with migrations, mode scoping and bounded lock retries.

``events`` is a compatibility alias only. New code persists complete movement
episodes in ``movement_events``; observations belong in ``item_current_state``.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA: dict[str, dict[str, str]] = {
    "cameras": {"name":"TEXT","room_name":"TEXT","installation":"TEXT","source_type":"TEXT","source":"TEXT","config":"JSON","enabled":"BOOL","inference_fps":"REAL","save_clips":"BOOL","runtime_mode":"TEXT"},
    "zones": {"camera_id":"TEXT","name":"TEXT","points":"JSON","priority":"INTEGER","enabled":"BOOL","scene_metadata":"JSON"},
    "scenes": {"camera_id":"TEXT","scene_version":"INTEGER","snapshot_id":"TEXT","camera_signature":"TEXT","geometry":"JSON","source_session_id":"TEXT","source_frame":"INTEGER","source_timestamp":"TEXT","source_type":"TEXT","is_simulated":"BOOL","runtime_mode":"TEXT","screenshot_path":"TEXT","screenshot_sha256":"TEXT","surfaces":"JSON","proposals":"JSON","calibration_status":"TEXT","proposal_error":"TEXT","invalid_reason":"TEXT","world_geometry":"JSON","previous_version":"JSON"},
    "items": {"name":"TEXT","type":"TEXT","description":"TEXT","owner":"TEXT","color":"TEXT","features":"TEXT","aliases":"JSON","aruco_id":"INTEGER","ring_enabled":"BOOL"},
    "item_reference_images": {"item_id":"TEXT","path":"TEXT","sha256":"TEXT","features":"JSON","width":"INTEGER","height":"INTEGER","original_path":"TEXT","region":"JSON","region_confirmed":"BOOL","suggested_regions":"JSON","status":"TEXT","crop_path":"TEXT","capture_source":"JSON","quality":"JSON"},
    "item_recognition_profiles": {"item_id":"TEXT","profile_version":"INTEGER","status":"TEXT","model_id":"TEXT","model_version":"TEXT","dimension":"INTEGER","embeddings":"JSON","reference_ids":"JSON","error":"TEXT"},
    "tracks": {"camera_id":"TEXT","item_id":"TEXT","state":"TEXT","zone_id":"TEXT","zone_name":"TEXT","center":"JSON","history":"JSON","last_seen":"TEXT","confidence":"REAL","runtime_mode":"TEXT","source_type":"TEXT","is_simulated":"BOOL","source_session_id":"TEXT"},
    "item_current_state": {"item_id":"TEXT","current_camera":"TEXT","current_room":"TEXT","current_zone":"TEXT","current_position":"JSON","last_seen_at":"TEXT","last_confirmed_placed_at":"TEXT","evidence_ingested_at":"TEXT","confidence":"REAL","evidence_event_id":"TEXT","runtime_mode":"TEXT","source_type":"TEXT","is_simulated":"BOOL","source_session_id":"TEXT","source_attestation_id":"TEXT","validation_run_id":"TEXT","status":"TEXT","last_observed":"JSON","last_confirmed_placement":"JSON","location_hypotheses":"JSON"},
    "movement_events": {
        "event_id":"TEXT","movement_session_id":"TEXT","idempotency_key":"TEXT","source_window_key":"TEXT","manual_reference_event_id":"TEXT","validation_run_id":"TEXT","scenario_index":"INTEGER","item_id":"TEXT","item_name":"TEXT","event_type":"TEXT","camera_id":"TEXT","camera_name":"TEXT","room_name":"TEXT","runtime_mode":"TEXT","source_type":"TEXT","is_simulated":"BOOL","source_session_id":"TEXT","source_attestation_id":"TEXT","source_frame_start":"INTEGER","source_frame_end":"INTEGER","before_frame_index":"INTEGER","after_frame_index":"INTEGER","clip_source_frame_start":"INTEGER","clip_source_frame_end":"INTEGER","clip_source_timestamp_start":"TEXT","clip_source_timestamp_end":"TEXT","clip_fps":"REAL","clip_required_pre_seconds":"REAL","clip_required_post_seconds":"REAL","clip_collected_post_seconds":"REAL","clip_post_roll_complete":"BOOL","source_timestamp_start":"TEXT","source_timestamp_end":"TEXT","ingested_at":"TEXT","detector_backend":"TEXT","tracker_backend":"TEXT","detection_mode":"TEXT","aruco_id":"INTEGER","trajectory":"JSON","from_zone":"TEXT","from_zone_id":"TEXT","to_zone":"TEXT","to_zone_id":"TEXT","zone_id":"TEXT","zone_name":"TEXT","previous_zone":"TEXT","new_zone":"TEXT","from_position":"JSON","to_position":"JSON","final_position":"JSON","confidence":"REAL","evidence_status":"TEXT","evidence_type":"TEXT","pickup_evidence":"JSON","placement_evidence":"JSON","source_continuity_ok":"BOOL","reconnect_epoch":"INTEGER","reconnect_epoch_changed":"BOOL","stable_before":"BOOL","stable_after":"BOOL","meaningful_position_change":"BOOL","before_screenshot":"TEXT","after_screenshot":"TEXT","screenshot_path":"TEXT","before_screenshot_sha256":"TEXT","after_screenshot_sha256":"TEXT","before_frame_sha256":"TEXT","after_frame_sha256":"TEXT","screenshot_sha256":"TEXT","clip_path":"TEXT","clip_sha256":"TEXT","created_by":"TEXT","pinned":"BOOL","manually_corrected":"BOOL","human_review_status":"TEXT","notes":"TEXT","timestamp_start":"TEXT","timestamp_end":"TEXT","started_at":"TEXT","ended_at":"TEXT","final_status":"TEXT"
    },
    "event_media": {"event_id":"TEXT","owner_current_state_id":"TEXT","runtime_mode":"TEXT","source_session_id":"TEXT","path":"TEXT","kind":"TEXT","role":"TEXT","sha256":"TEXT","metadata":"JSON","status":"TEXT","pending_delete_at":"TEXT","deleted_at":"TEXT","exported":"BOOL"},
    "acceptance_suites": {"suite_id":"TEXT","runtime_mode":"TEXT","contract_version":"TEXT","contract":"JSON","item_id":"TEXT","camera_id":"TEXT","camera_config_sha256":"TEXT","camera_source_sha256":"TEXT","aruco_id":"INTEGER","zone_a_id":"TEXT","zone_b_id":"TEXT","zone_a":"JSON","zone_b":"JSON","thresholds":"JSON","source_type":"TEXT","is_simulated":"BOOL","created_by":"TEXT"},
    "acceptance_retention_receipts": {"runtime_mode":"TEXT","event_id":"TEXT","validation_run_id":"TEXT","suite_id":"TEXT","item_id":"TEXT","camera_id":"TEXT","source_session_id":"TEXT","policy":"TEXT","reason":"TEXT","status":"TEXT","verified_at":"TEXT","completed_at":"TEXT","evidence":"JSON","evidence_sha256":"TEXT","created_by":"TEXT"},
    "acceptance_runs": {"validation_run_id":"TEXT","suite_id":"TEXT","contract_version":"TEXT","runtime_mode":"TEXT","status":"TEXT","trial_kind":"TEXT","scenario_index":"INTEGER","minimum_duration_seconds":"REAL","item_id":"TEXT","camera_id":"TEXT","source_type":"TEXT","is_simulated":"BOOL","source_session_id":"TEXT","reconnect_epoch":"INTEGER","aruco_id":"INTEGER","detection_mode":"TEXT","origin_zone_id":"TEXT","destination_zone_id":"TEXT","expected_from_zone":"TEXT","expected_to_zone":"TEXT","origin_zone":"JSON","destination_zone":"JSON","thresholds":"JSON","user_executed":"BOOL","marker_status":"JSON","engine_status":"JSON","progress":"JSON","created_by":"TEXT","started_at":"TEXT","ended_at":"TEXT","cancel_reason":"TEXT","outcome":"TEXT","failure_reason":"TEXT","event_id":"TEXT","event_count_before":"INTEGER","event_count_after":"INTEGER","confirmation_latency_ms":"REAL","result":"JSON"},
    "source_sessions": {"camera_id":"TEXT","runtime_mode":"TEXT","source_type":"TEXT","is_simulated":"BOOL","started_at":"TEXT","ended_at":"TEXT","first_frame":"INTEGER","last_frame":"INTEGER","last_frame_at":"TEXT","status":"TEXT","continuity_ok":"BOOL","metadata":"JSON"},
    "retention_reports": {"runtime_mode":"TEXT","policy":"TEXT","trigger":"TEXT","dry_run":"BOOL","started_at":"TEXT","ended_at":"TEXT","deleted_events":"INTEGER","deleted_images":"INTEGER","deleted_clips":"INTEGER","deleted_other":"INTEGER","bytes_before":"INTEGER","bytes_after":"INTEGER","details":"JSON"},
    "companions": {"item_id":"TEXT","last_seen":"TEXT","online":"BOOL"},
    "settings": {"value":"JSON"},
}

ALIASES = {"events": "movement_events"}
MODE_SCOPED = {"cameras", "scenes", "tracks", "item_current_state", "movement_events", "event_media", "acceptance_suites", "acceptance_runs", "acceptance_retention_receipts", "source_sessions", "retention_reports"}


class Database:
    def __init__(self, path: Path, runtime_mode: str = "REAL", *, retries: int = 4):
        self.path = Path(path)
        self.runtime_mode = str(runtime_mode).upper()
        self.retries = max(1, min(int(retries), 8))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def table(self, table: str) -> str:
        table = ALIASES.get(table, table)
        if table not in SCHEMA:
            raise ValueError(f"unsupported table: {table}")
        return table

    def _run_retry(self, fn):
        delay = 0.03
        for attempt in range(self.retries):
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == self.retries - 1:
                    raise
                time.sleep(delay)
                delay *= 2

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _migrate(self) -> None:
        def operation():
            with self.connect() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
                for table, fields in SCHEMA.items():
                    columns = ", ".join(f'"{name}" {"TEXT" if kind == "JSON" else "INTEGER" if kind == "BOOL" else kind}' for name, kind in fields.items())
                    conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, {columns})')
                    existing = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
                    for name, kind in fields.items():
                        if name not in existing:
                            sql_type = "TEXT" if kind == "JSON" else "INTEGER" if kind == "BOOL" else kind
                            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {sql_type}')
                conn.executescript("""
                    CREATE INDEX IF NOT EXISTS idx_movement_item_time ON movement_events(item_id, timestamp_start DESC);
                    CREATE INDEX IF NOT EXISTS idx_movement_camera_time ON movement_events(camera_id, timestamp_start DESC);
                    CREATE INDEX IF NOT EXISTS idx_movement_mode ON movement_events(runtime_mode, is_simulated, timestamp_start DESC);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_movement_event_id ON movement_events(event_id) WHERE event_id IS NOT NULL;
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_movement_idempotency ON movement_events(idempotency_key) WHERE idempotency_key IS NOT NULL;
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_movement_source_window ON movement_events(source_window_key) WHERE source_window_key IS NOT NULL;
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_movement_validation_run ON movement_events(validation_run_id) WHERE validation_run_id IS NOT NULL;
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_current_item_mode ON item_current_state(item_id, runtime_mode);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_items_aruco ON items(aruco_id) WHERE aruco_id IS NOT NULL;
                    CREATE INDEX IF NOT EXISTS idx_reference_item_time ON item_reference_images(item_id, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_media_event ON event_media(event_id, status);
                    CREATE INDEX IF NOT EXISTS idx_source_session ON source_sessions(runtime_mode, camera_id, started_at DESC);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_acceptance_suite_id ON acceptance_suites(suite_id) WHERE suite_id IS NOT NULL;
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_acceptance_retention_receipt_event ON acceptance_retention_receipts(runtime_mode,event_id);
                    CREATE INDEX IF NOT EXISTS idx_acceptance_suite_scope ON acceptance_suites(runtime_mode,item_id,camera_id,created_at DESC);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_acceptance_validation_run ON acceptance_runs(validation_run_id) WHERE validation_run_id IS NOT NULL;
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_acceptance_suite_scenario ON acceptance_runs(suite_id,scenario_index) WHERE suite_id IS NOT NULL;
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_acceptance_active_camera ON acceptance_runs(camera_id) WHERE status='ACTIVE';
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_acceptance_active_item ON acceptance_runs(item_id) WHERE status='ACTIVE';
                    CREATE INDEX IF NOT EXISTS idx_acceptance_status ON acceptance_runs(runtime_mode,status,created_at DESC);
                """)
                conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(2, ?)", (now(),))
                conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(3, ?)", (now(),))
        self._run_retry(operation)

    def _encode(self, table: str, data: dict[str, Any]) -> dict[str, Any]:
        result = {key: value for key, value in data.items() if key in SCHEMA[table]}
        if table in MODE_SCOPED and "runtime_mode" in SCHEMA[table]:
            result.setdefault("runtime_mode", self.runtime_mode)
        for key, value in list(result.items()):
            kind = SCHEMA[table][key]
            if kind == "JSON":
                result[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":")) if value is not None else None
            elif kind == "BOOL":
                result[key] = int(bool(value)) if value is not None else None
        return result

    def _decode(self, table: str, row: sqlite3.Row | None):
        if row is None:
            return None
        result = dict(row)
        for key, kind in SCHEMA[table].items():
            if kind == "JSON" and result.get(key) is not None:
                try:
                    result[key] = json.loads(result[key])
                except (TypeError, json.JSONDecodeError):
                    result[key] = None
            elif kind == "BOOL":
                result[key] = bool(result.get(key))
        return result

    def _scope_sql(self, table: str, filters=None, *, unscoped: bool = False) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if not unscoped and table in MODE_SCOPED and "runtime_mode" in SCHEMA[table]:
            clauses.append('"runtime_mode"=?')
            values.append(self.runtime_mode)
            if self.runtime_mode == "REAL" and "is_simulated" in SCHEMA[table]:
                clauses.append('COALESCE("is_simulated",1)=0')
            if self.runtime_mode == "REAL" and "source_type" in SCHEMA[table]:
                clauses.append('COALESCE("source_type",\'unknown\') NOT IN (\'unknown\',\'mock\',\'demo_seed\',\'test_fixture\',\'video_file\',\'virtual_esp32\',\'esp32_unverified\')')
        for key, value in (filters or {}).items():
            if (key not in SCHEMA[table] and key not in {'id', 'created_at', 'updated_at'}) or value is None:
                continue
            candidates = value if isinstance(value, (list, tuple)) else [value]
            clauses.append(f'"{key}" IN ({",".join("?" for _ in candidates)})' if candidates else '0')
            values.extend(int(candidate) if SCHEMA[table].get(key) == "BOOL" and candidate is not None else candidate for candidate in candidates)
        return clauses, values

    def get(self, table: str, record_id: str, *, unscoped: bool = False):
        table = self.table(table)
        clauses, values = self._scope_sql(table, unscoped=unscoped)
        clauses.insert(0, '"id"=?')
        values.insert(0, record_id)
        with self.connect() as conn:
            row = conn.execute(f'SELECT * FROM "{table}" WHERE ' + " AND ".join(clauses), values).fetchone()
        return self._decode(table, row)

    def list(self, table: str, filters=None, limit: int = 1000, order: str | None = None, *, unscoped: bool = False):
        table = self.table(table)
        fields = SCHEMA[table]
        clauses, values = self._scope_sql(table, filters, unscoped=unscoped)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        order = order if order in {"created_at", "updated_at", *fields} else "created_at"
        cap = min(max(int(limit), 1), 100000)
        with self.connect() as conn:
            rows = conn.execute(f'SELECT * FROM "{table}"{where} ORDER BY "{order}" DESC LIMIT ?', [*values, cap]).fetchall()
        return [self._decode(table, row) for row in rows]

    def count(self, table: str, filters=None, *, unscoped: bool = False) -> int:
        table = self.table(table)
        clauses, values = self._scope_sql(table, filters, unscoped=unscoped)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"{where}', values).fetchone()[0])

    def event_counts(self, day: str) -> tuple[int, int]:
        clauses, values = self._scope_sql('movement_events')
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        # Offset-bearing timestamps use the machine's local day; legacy naive
        # timestamps already describe local time. Invalid dates count only in total.
        with self.connect() as conn:
            row = conn.execute('''SELECT COUNT(*), COALESCE(SUM(CASE
                WHEN length(timestamp_start)>10 AND (substr(timestamp_start, -6, 1) IN ('+', '-') OR upper(substr(timestamp_start, -1))='Z')
                THEN date(timestamp_start, 'localtime') ELSE date(timestamp_start) END = ?), 0)
                FROM movement_events''' + where, [day, *values]).fetchone()
        return tuple(row)

    def save(self, table: str, data: dict[str, Any], record_id: str | None = None):
        return self.save_many([(table, data, record_id)])[0]

    def save_many(self, records: Iterable[tuple[str, dict[str, Any], str | None]]):
        """Commit related rows together, including their returned snapshots.

        Materialize once so lock retries neither consume an iterator twice nor
        generate new identifiers. Reads/decode happen before the commit: a caller
        can safely remove newly staged files when this operation raises.
        """
        statements = [self._save_statement(table, data, record_id) for table, data, record_id in records]
        if not statements:
            return []

        def operation():
            result = []
            with self.connect() as conn:
                for table, record_id, sql, values in statements:
                    conn.execute(sql, values)
                    result.append(self._decode(table, conn.execute(
                        f'SELECT * FROM "{table}" WHERE id=?', [record_id]).fetchone()))
            return result

        return self._run_retry(operation)

    def _save_statement(self, table, data, record_id=None):
        table = self.table(table)
        record_id = str(record_id or data.get("id") or uuid4().hex)
        stamp = now()
        insert = {"id": record_id, "created_at": stamp, "updated_at": stamp, **self._encode(table, data)}
        update_keys = [key for key in insert if key not in {"id", "created_at"}]
        sql = f'INSERT INTO "{table}" (' + ",".join(f'"{key}"' for key in insert) + ") VALUES (" + ",".join("?" for _ in insert) + ") " + 'ON CONFLICT("id") DO UPDATE SET ' + ",".join(f'"{key}"=excluded."{key}"' for key in update_keys)
        return table, record_id, sql, list(insert.values())

    def save_current_state_if_newer(self, data: dict[str, Any], record_id: str, *, observation_media: dict | None = None, expected_profile: dict | None = None):
        """Upsert an observation without allowing delayed callbacks to rewind state.

        ``last_seen_at`` is the primary event-time order.  ``evidence_ingested_at``
        breaks ties, which makes retries deterministic while still letting an
        explicit correction at the same source timestamp replace an older row.
        """
        stamp = now()
        encoded = self._encode("item_current_state", data)
        encoded.setdefault("evidence_ingested_at", stamp)
        insert = {"id": str(record_id), "created_at": stamp, "updated_at": stamp, **encoded}
        update_keys = [key for key in insert if key not in {"id", "created_at"}]
        sql = (
            'INSERT INTO "item_current_state" ('
            + ",".join(f'"{key}"' for key in insert)
            + ") VALUES ("
            + ",".join("?" for _ in insert)
            + ') ON CONFLICT("id") DO UPDATE SET '
            + ",".join(f'"{key}"=excluded."{key}"' for key in update_keys)
            + ' WHERE COALESCE(julianday(excluded."last_seen_at"),-1) > COALESCE(julianday("item_current_state"."last_seen_at"),-1)'
            + ' OR (COALESCE(julianday(excluded."last_seen_at"),-1) = COALESCE(julianday("item_current_state"."last_seen_at"),-1)'
            + ' AND COALESCE(excluded."evidence_ingested_at",\'\') >= COALESCE("item_current_state"."evidence_ingested_at",\'\'))'
        )
        def commit():
            with self.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                # Registration invalidation and observation commit serialize
                # on SQLite, not on two independently checked Python clocks.
                # Never persist a photo identity after its profile changed in
                # the interval between callback validation and this transaction.
                if expected_profile is not None:
                    profile=conn.execute('SELECT status,profile_version,model_version FROM item_recognition_profiles WHERE id=?',
                        (data.get('item_id'),)).fetchone()
                    if (profile is None or profile['status']!='ready'
                        or profile['profile_version']!=expected_profile.get('profile_version')
                        or profile['model_version']!=expected_profile.get('model_version')):
                        return False
                changed = conn.execute(sql, list(insert.values())).rowcount
                if changed and observation_media:
                    media = {'id': observation_media['id'], 'created_at': stamp, 'updated_at': stamp,
                             **self._encode('event_media', observation_media)}
                    conn.execute('UPDATE event_media SET status=\'pending_delete\',pending_delete_at=?,updated_at=? '
                                 'WHERE owner_current_state_id=? AND runtime_mode=? AND status=\'active\'',
                                 (stamp, stamp, str(record_id), self.runtime_mode))
                    conn.execute('INSERT INTO event_media (' + ','.join(f'"{key}"' for key in media)
                                 + ') VALUES (' + ','.join('?' for _ in media) + ')', list(media.values()))
                return True
        if self._run_retry(commit) is False:
            return None
        return self.get("item_current_state", str(record_id), unscoped=True)

    def bind_current_state_attestation(
        self,
        record_id: str,
        *,
        camera_id: str,
        source_session_id: str,
        attestation_id: str,
    ) -> bool:
        """Attach server-issued ESP32 proof only to the observation just written.

        The camera/session predicates make the follow-up update fail closed when
        a newer observation wins between EventService's upsert and this call.
        """
        if not all((record_id, camera_id, source_session_id, attestation_id)):
            return False
        changed = self._run_retry(
            lambda: self._execute(
                'UPDATE "item_current_state" SET "source_attestation_id"=?,"updated_at"=? '
                'WHERE "id"=? AND "runtime_mode"=? AND "current_camera"=? '
                'AND "source_session_id"=? AND "source_type"=\'esp32_real\'',
                (
                    attestation_id,
                    now(),
                    record_id,
                    self.runtime_mode,
                    camera_id,
                    source_session_id,
                ),
            )
        )
        return bool(changed)

    def insert_idempotent(self, table: str, data: dict[str, Any], unique_field: str = "idempotency_key"):
        table = self.table(table)
        if unique_field not in SCHEMA[table] or not data.get(unique_field):
            raise ValueError("missing idempotency key")
        encoded = self._encode(table, data)
        record_id = str(data.get("id") or data.get("event_id") or uuid4().hex)
        stamp = now()
        insert = {"id": record_id, "created_at": stamp, "updated_at": stamp, **encoded}
        sql = f'INSERT OR IGNORE INTO "{table}" (' + ",".join(f'"{key}"' for key in insert) + ") VALUES (" + ",".join("?" for _ in insert) + ")"
        self._run_retry(lambda: self._execute(sql, list(insert.values())))
        with self.connect() as conn:
            row = conn.execute(f'SELECT * FROM "{table}" WHERE "{unique_field}"=?', (encoded[unique_field],)).fetchone()
        return self._decode(table, row)

    def insert_event_bundle_idempotent(
        self,
        event: dict[str, Any],
        media_rows: Iterable[dict[str, Any]],
        current_state: dict[str, Any],
        current_state_id: str,
        *,
        acceptance_run_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically persist one event, its media registry, and current state.

        Media files are written before this database transaction so SQLite never
        has to hold a write lock while an encoder runs.  The caller owns those
        files until this method commits and must remove them if ``inserted`` is
        false or an exception is raised.
        """
        unique_field = "idempotency_key"
        if not event.get(unique_field):
            raise ValueError("missing idempotency key")
        event_id = str(event.get("id") or event.get("event_id") or uuid4().hex)
        acceptance_run_id = str(acceptance_run_id or "").strip() or None
        if acceptance_run_id and str(event.get("validation_run_id") or "").strip() != acceptance_run_id:
            raise ValueError("acceptance run identity mismatch")
        materialized_media = [dict(row) for row in media_rows]
        event_encoded = self._encode("movement_events", event)
        stamp = now()
        event_insert = {"id": event_id, "created_at": stamp, "updated_at": stamp, **event_encoded}
        event_sql = (
            'INSERT OR IGNORE INTO "movement_events" ('
            + ",".join(f'"{key}"' for key in event_insert)
            + ") VALUES ("
            + ",".join("?" for _ in event_insert)
            + ")"
        )

        def operation() -> tuple[dict[str, Any], bool]:
            with self.connect() as conn:
                inserted = conn.execute(event_sql, list(event_insert.values())).rowcount == 1
                row = conn.execute(
                    f'SELECT * FROM "movement_events" WHERE "{unique_field}"=?',
                    (event_encoded[unique_field],),
                ).fetchone()
                if row is None and event_encoded.get("source_window_key"):
                    # A competing process may have won the immutable source
                    # window while carrying different caller UUIDs or claimed
                    # semantics.  Return that row so EventService can compare
                    # raw keyframe hashes/metadata and either deduplicate or
                    # reject the conflict; never insert a second event.
                    row = conn.execute(
                        'SELECT * FROM "movement_events" WHERE "source_window_key"=?',
                        (event_encoded["source_window_key"],),
                    ).fetchone()
                if row is None and event_encoded.get("validation_run_id"):
                    # The per-run unique index may win before the semantic or
                    # source-window key.  Return that authoritative event so a
                    # same-payload callback is idempotent and a conflicting one
                    # can be rejected by EventService.
                    row = conn.execute(
                        'SELECT * FROM "movement_events" WHERE "validation_run_id"=?',
                        (event_encoded["validation_run_id"],),
                    ).fetchone()
                if row is None:
                    raise sqlite3.IntegrityError("idempotent event insert returned no row")
                if not inserted:
                    return self._decode("movement_events", row), False

                acceptance_run = None
                if acceptance_run_id:
                    acceptance_run = conn.execute(
                        'SELECT * FROM "acceptance_runs" WHERE "id"=?',
                        (acceptance_run_id,),
                    ).fetchone()
                    if not acceptance_run:
                        raise sqlite3.IntegrityError("acceptance run does not exist")
                    acceptance_suite = conn.execute(
                        'SELECT * FROM "acceptance_suites" WHERE "id"=? AND "runtime_mode"=\'REAL\'',
                        (acceptance_run["suite_id"],),
                    ).fetchone() if acceptance_run["suite_id"] else None
                    if (
                        not acceptance_suite
                        or acceptance_run["status"] != "ACTIVE"
                        or not bool(acceptance_run["user_executed"])
                        or acceptance_run["event_id"] is not None
                        or acceptance_run["runtime_mode"] != "REAL"
                        or acceptance_run["source_type"] != "opencv_camera"
                        or bool(acceptance_run["is_simulated"])
                        or acceptance_run["item_id"] != event_encoded.get("item_id")
                        or acceptance_run["camera_id"] != event_encoded.get("camera_id")
                        or acceptance_run["source_session_id"] != event_encoded.get("source_session_id")
                        or acceptance_run["detection_mode"] != "aruco_screen_validation"
                        or acceptance_run["trial_kind"] != "movement"
                        or acceptance_run["expected_from_zone"] != event_encoded.get("from_zone")
                        or acceptance_run["expected_to_zone"] != event_encoded.get("to_zone")
                        or event_encoded.get("aruco_id") is None
                        or int(acceptance_run["aruco_id"]) != int(event_encoded["aruco_id"])
                        or int(acceptance_run["reconnect_epoch"] or 0) != int(event_encoded.get("reconnect_epoch") or 0)
                        or acceptance_run["contract_version"] != acceptance_suite["contract_version"]
                        or acceptance_run["item_id"] != acceptance_suite["item_id"]
                        or acceptance_run["camera_id"] != acceptance_suite["camera_id"]
                    ):
                        raise sqlite3.IntegrityError("acceptance run is not active for this evidence")
                    roles = [str(media.get("role") or "") for media in materialized_media]
                    paths = [str(media.get("path") or "") for media in materialized_media]
                    role_kinds = {(str(media.get("role") or ""), str(media.get("kind") or "")) for media in materialized_media}
                    media_bound = all(
                        str(media.get("event_id") or "") == str(event.get("event_id") or event_id)
                        and str(media.get("runtime_mode") or "") == "REAL"
                        and str(media.get("source_session_id") or "") == str(event_encoded.get("source_session_id") or "")
                        and bool(media.get("path")) and bool(media.get("sha256"))
                        for media in materialized_media
                    )
                    if (
                        sorted(roles) != ["after", "before", "clip"] or len(set(paths)) != 3
                        or role_kinds != {("before", "image"), ("after", "image"), ("clip", "clip")}
                        or not media_bound
                    ):
                        raise sqlite3.IntegrityError("acceptance evidence requires distinct before, after and clip media")

                # Encode and insert each dependent row only after the event row
                # exists inside this transaction.  Any exception rolls all of
                # them back together, including the event itself.
                for media in materialized_media:
                    media_id = str(media.get("id") or uuid4().hex)
                    media_insert = {
                        "id": media_id,
                        "created_at": stamp,
                        "updated_at": stamp,
                        **self._encode("event_media", media),
                    }
                    conn.execute(
                        'INSERT INTO "event_media" ('
                        + ",".join(f'"{key}"' for key in media_insert)
                        + ") VALUES ("
                        + ",".join("?" for _ in media_insert)
                        + ")",
                        list(media_insert.values()),
                    )

                state_insert = {
                    "id": str(current_state_id),
                    "created_at": stamp,
                    "updated_at": stamp,
                    **self._encode("item_current_state", current_state),
                }
                if event_encoded.get("source_attestation_id"):
                    state_insert["source_attestation_id"] = event_encoded["source_attestation_id"]
                existing_state = conn.execute(
                    'SELECT * FROM "item_current_state" WHERE "id"=?',
                    (str(current_state_id),),
                ).fetchone()
                if existing_state is not None and not bool(event.get("manually_corrected")):
                    def instant_at_least(candidate: Any, baseline: Any) -> bool:
                        return bool(conn.execute(
                            "SELECT COALESCE(julianday(?),-1) >= COALESCE(julianday(?),-1)",
                            (candidate, baseline),
                        ).fetchone()[0])

                    # Media post-roll is written asynchronously. Observations
                    # newer than the placement can therefore reach current
                    # state before the confirmed event transaction. Attach the
                    # event as the latest confirmed evidence without rewinding
                    # the newer observed coordinates, status or last-seen time.
                    if not instant_at_least(state_insert.get("last_seen_at"), existing_state["last_seen_at"]):
                        for key in (
                            "current_camera", "current_room", "current_zone", "current_position",
                            "last_seen_at", "confidence", "source_type", "is_simulated",
                            "source_session_id", "status",
                        ):
                            state_insert[key] = existing_state[key]
                    # An out-of-order older confirmation must not replace a
                    # newer evidence link even when its database write arrives
                    # later. These fields advance on confirmation time, not on
                    # callback/ingestion time.
                    if not instant_at_least(
                        state_insert.get("last_confirmed_placed_at"),
                        existing_state["last_confirmed_placed_at"],
                    ):
                        for key in ("last_confirmed_placed_at", "evidence_event_id", "evidence_ingested_at", "last_confirmed_placement"):
                            state_insert[key] = existing_state[key]
                if existing_state is not None:
                    # A confirmation owns its placement evidence, not the
                    # independent latest observation snapshot or hypotheses.
                    for key in ('last_observed', 'location_hypotheses'):
                        state_insert[key] = existing_state[key]
                update_keys = [key for key in state_insert if key not in {"id", "created_at"}]
                conn.execute(
                    'INSERT INTO "item_current_state" ('
                    + ",".join(f'"{key}"' for key in state_insert)
                    + ") VALUES ("
                    + ",".join("?" for _ in state_insert)
                    + ') ON CONFLICT("id") DO UPDATE SET '
                    + ",".join(f'"{key}"=excluded."{key}"' for key in update_keys),
                    list(state_insert.values()),
                )
                if acceptance_run is not None:
                    completed_at = now()
                    latency = conn.execute(
                        "SELECT MAX(0,(julianday(?)-julianday(?))*86400000.0)",
                        (completed_at, acceptance_run["started_at"]),
                    ).fetchone()[0]
                    event_count_after = int(conn.execute(
                        "SELECT COUNT(*) FROM movement_events WHERE runtime_mode='REAL' AND item_id=?",
                        (event_encoded.get("item_id"),),
                    ).fetchone()[0])
                    result = {
                        "event_id": event.get("event_id") or event_id,
                        "from_zone": event.get("from_zone"), "to_zone": event.get("to_zone"),
                        "source_session_id": event.get("source_session_id"),
                        "detection_mode": event.get("detection_mode"),
                        "aruco_id": event.get("aruco_id"),
                        "before_screenshot": event.get("before_screenshot"),
                        "after_screenshot": event.get("after_screenshot"),
                        "before_screenshot_sha256": event.get("before_screenshot_sha256"),
                        "after_screenshot_sha256": event.get("after_screenshot_sha256"),
                        "before_frame_sha256": event.get("before_frame_sha256"),
                        "after_frame_sha256": event.get("after_frame_sha256"),
                        "clip_path": event.get("clip_path"),
                        "clip_sha256": event.get("clip_sha256"),
                    }
                    changed = conn.execute(
                        "UPDATE acceptance_runs SET status='PASSED',outcome='PASSED',failure_reason=NULL,"
                        "event_id=?,event_count_after=?,confirmation_latency_ms=?,result=?,ended_at=?,updated_at=? "
                        "WHERE id=? AND status='ACTIVE' AND user_executed=1 AND event_id IS NULL",
                        (
                            event.get("event_id") or event_id, event_count_after, float(latency or 0),
                            json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                            completed_at, completed_at, acceptance_run_id,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise sqlite3.IntegrityError("acceptance run transition lost")
                return self._decode("movement_events", row), True

        return self._run_retry(operation)

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.connect() as conn:
            cursor = conn.execute(sql, tuple(params))
            return int(cursor.rowcount)

    def delete(self, table: str, record_id: str, *, unscoped: bool = False) -> int:
        return self.delete_many(table, [record_id], unscoped=unscoped)

    def delete_many(self, table: str, record_ids: Iterable[str], *, unscoped: bool = False) -> int:
        table = self.table(table)
        clauses, values = self._scope_sql(table, unscoped=unscoped)
        clauses.insert(0, '"id"=?')
        # Materialize once so a lock retry can replay one-shot iterables in full.
        parameters = [(record_id, *values) for record_id in record_ids]
        if not parameters:
            return 0
        def operation():
            with self.connect() as conn:
                return conn.executemany(f'DELETE FROM "{table}" WHERE ' + " AND ".join(clauses), parameters).rowcount
        return self._run_retry(operation)
