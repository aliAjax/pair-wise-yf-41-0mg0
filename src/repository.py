import json
import sqlite3
import uuid
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError
from .rules import (
    REASSIGN_FALLBACK_STATUS,
    derive_observed_at,
    recompute_event,
)


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_report_code
                    ON entities(json_extract(data, '$.code'))
                    WHERE kind = 'report';
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS publications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    communication_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_publications_event
                    ON publications(event_id, id);
            """)
            self._migrate_embedded_reports(connection)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _observed_at(origin_time, time_offset):
        return derive_observed_at(origin_time, time_offset)

    def _migrate_embedded_reports(self, connection):
        """Turn reports embedded in old event rows into standalone records.

        Idempotent: processed events are marked and existing report codes are
        skipped, so startup and retries never create duplicates.
        """
        event_rows = connection.execute(
            "SELECT * FROM entities WHERE kind = 'event' ORDER BY created_at, id"
        ).fetchall()
        event_rows = connection.execute(
            "SELECT * FROM entities WHERE kind = 'event' ORDER BY created_at, id"
        ).fetchall()
        for event_row in event_rows:
            event = self._entity_from_row(event_row)
            embedded = event["data"].get("reports") or []
            already_migrated = bool(event["data"].get("reports_migrated"))
            migrated_any = False
            if not already_migrated:
                for index, item in enumerate(embedded):
                    if not isinstance(item, dict):
                        continue
                    code = item.get("code") or "MIG-%s-%d" % (event["id"][:8], index + 1)
                    exists = connection.execute(
                        "SELECT 1 FROM entities WHERE kind = 'report' AND json_extract(data, '$.code') = ?",
                        (code,),
                    ).fetchone()
                    if exists:
                        migrated_any = True
                        continue
                    report_data = dict(item)
                    report_data.update(
                        {
                            "code": code,
                            "observed_at": derive_observed_at(
                                event["data"].get("origin_time"), item.get("time_offset")
                            ),
                            "event_id": event["id"],
                        }
                    )
                    now = utcnow()
                    connection.execute(
                        "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                        "VALUES (?, ?, 'active', 1, ?, 'migration', ?, ?)",
                        (
                            str(uuid.uuid4()),
                            "report",
                            json.dumps(report_data, ensure_ascii=False, sort_keys=True),
                            now,
                            now,
                        ),
                    )
                    migrated_any = True
                data = dict(event["data"])
                data["reports_migrated"] = True
            else:
                data = dict(event["data"])
            if migrated_any:
                # Recompute figures from the standalone records so migrated
                # events look exactly like events created under the new model.
                owned = self._reports_owned_by(connection, event["id"])
                data = recompute_event({"id": event["id"], "data": data}, owned)
                connection.execute(
                    "UPDATE entities SET data = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(data, ensure_ascii=False, sort_keys=True), utcnow(), event["id"]),
                )

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        if kind == "report" and field in ("code", "event_id"):
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM entities WHERE kind = 'report' AND json_extract(data, '$.' || ?) = ? ORDER BY created_at, id",
                    (field, value),
                ).fetchall()
            return [self._entity_from_row(row) for row in rows]
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def _reports_owned_by(self, connection, event_id):
        rows = connection.execute(
            "SELECT * FROM entities WHERE kind = 'report' AND json_extract(data, '$.event_id') = ? "
            "ORDER BY created_at, id",
            (event_id,),
        ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def _insert_audit(self, connection, entity_id, actor_id, actor_role, action,
                      from_status, to_status, detail, now):
        connection.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id, actor_id, actor_role, action, from_status, to_status,
                json.dumps(detail or {}, ensure_ascii=False, sort_keys=True), now,
            ),
        )

    def reassign_report(self, report_id, expected_version, new_event_id, recompute_fn, actor):
        """Move one report to another event and recompute both events atomically.

        BEGIN IMMEDIATE serializes two analysts claiming the same report: the
        first writer commits, the second fails its optimistic version check.
        The reassign audit row is written in the same transaction so a losing
        analyst can always see who claimed the report. A published event
        touched by the move falls back to pending_review.
        Returns (report, old_event_id, affected_events).
        """
        connection = self._connect()
        affected = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ? AND kind = 'report'", (report_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("report not found: " + report_id)
            report = self._entity_from_row(row)
            current_version = report["version"]
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "report version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            old_event_id = report["data"].get("event_id")

            target = connection.execute(
                "SELECT * FROM entities WHERE id = ? AND kind = 'event'", (new_event_id,)
            ).fetchone()
            if not target:
                raise NotFoundError("event not found: " + str(new_event_id))
            if old_event_id == new_event_id:
                raise ConflictError("report already belongs to event " + str(new_event_id))

            report_data = dict(report["data"])
            report_data["event_id"] = new_event_id
            now = utcnow()
            connection.execute(
                "UPDATE entities SET version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (
                    json.dumps(report_data, ensure_ascii=False, sort_keys=True),
                    now,
                    report_id,
                    current_version,
                ),
            )

            for event_id in [old_event_id, new_event_id]:
                if not event_id:
                    continue
                event_row = connection.execute(
                    "SELECT * FROM entities WHERE id = ? AND kind = 'event'", (event_id,)
                ).fetchone()
                if not event_row:
                    continue
                event = self._entity_from_row(event_row)
                owned = self._reports_owned_by(connection, event_id)
                data = recompute_fn(event, owned)
                status = event["status"]
                if status == "published":
                    status = REASSIGN_FALLBACK_STATUS
                connection.execute(
                    "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                    "WHERE id = ? AND version = ?",
                    (
                        status,
                        json.dumps(data, ensure_ascii=False, sort_keys=True),
                        now,
                        event_id,
                        event["version"],
                    ),
                )
                self._insert_audit(
                    connection, event_id, actor.user_id, actor.role, "recompute",
                    event["status"], status,
                    {
                        "reason": "report_reassigned",
                        "report_id": report_id,
                        "station_count": data.get("station_count"),
                        "magnitude": data.get("magnitude"),
                        "fell_back": status == REASSIGN_FALLBACK_STATUS,
                    },
                    now,
                )
                affected.append({"id": event_id, "status": status, "data": data})

            self._insert_audit(
                connection, report_id, actor.user_id, actor.role, "reassign",
                "active", "active",
                {"from_event_id": old_event_id, "to_event_id": new_event_id},
                now,
            )

            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(report_id), old_event_id, affected

    def last_reassign_actor(self, report_id):
        """Return the analyst who most recently claimed the report, if any."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT actor_id FROM audit_log WHERE entity_id = ? AND action = 'reassign' "
                "ORDER BY id DESC LIMIT 1",
                (report_id,),
            ).fetchone()
        return row["actor_id"] if row else None

    def save_publication(self, event_id, communication_id, version, snapshot, actor_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO publications(event_id, communication_id, version, payload, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    communication_id,
                    version,
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    actor_id,
                    utcnow(),
                ),
            )

    def list_publications(self, event_id=None):
        with self._connect() as connection:
            if event_id:
                rows = connection.execute(
                    "SELECT * FROM publications WHERE event_id = ? ORDER BY id", (event_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM publications ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "event_id": row["event_id"],
                "communication_id": row["communication_id"],
                "version": row["version"],
                "payload": json.loads(row["payload"]),
                "created_by": row["created_by"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
