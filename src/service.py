from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, DomainError, NotFoundError
from .rules import RuleEngine, recompute_event


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        self.migrate()

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def _next_report_code(self):
        value = self.repository.next_counter("report")
        return "RP-%06d" % value

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)

        embedded_reports = None
        if kind == "report" and not payload.get("code"):
            payload["code"] = self._next_report_code()
        if kind == "event":
            embedded_reports = payload.pop("reports", None)

        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})

        if kind == "event" and embedded_reports:
            self._create_reports_for_event(actor, entity_id, embedded_reports)
            self._recompute_event(actor, entity_id)

        if kind == "report" and payload.get("event_id"):
            self._recompute_event(actor, payload["event_id"])

        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def _create_reports_for_event(self, actor, event_id, reports):
        event = self.repository.get_entity(event_id)
        origin_time = event["data"].get("origin_time") if event else None
        for item in reports:
            report_data = {
                "station": item.get("station"),
                "observed_at": item.get("observed_at") or origin_time,
                "event_id": event_id,
                "amplitude": item.get("amplitude"),
                "time_offset": item.get("time_offset"),
                "distance_km": item.get("distance_km"),
            }
            self.create(actor, "report", report_data)

    def _recompute_event(self, actor, event_id):
        event = self.repository.get_entity(event_id)
        if not event or event["kind"] != "event":
            return None
        reports = self.repository.find_entities("report", "event_id", event_id)
        patch, next_status = recompute_event(event, reports)
        merged = dict(event["data"])
        merged.update(patch)
        updated = self.repository.update_entity(event_id, None, next_status, merged)
        if next_status != event["status"]:
            self.audit.record(
                event_id,
                actor,
                "recompute",
                event["status"],
                next_status,
                {"patch": patch},
            )
        return updated

    def assign_report(self, actor, report_id, event_id, expected_version=None):
        report = self.repository.get_entity(report_id)
        if not report or report["kind"] != "report":
            raise NotFoundError("report not found: " + str(report_id))
        expected = int(expected_version) if expected_version is not None else report["version"]
        next_status, patch = self.rules.validate_transition(
            actor, report, "assign", {"event_id": event_id}, self._lookup
        )
        new_data = dict(report["data"])
        new_data.update(patch)
        try:
            updated = self.repository.update_entity(report_id, expected, next_status, new_data)
        except ConflictError as exc:
            current = self.repository.get_entity(report_id)
            raise ConflictError(
                "version conflict: expected %s, found %s" % (expected, current["version"]),
                details={
                    "report_id": report_id,
                    "report_code": current["data"].get("code"),
                    "current_event_id": current["data"].get("event_id"),
                    "current_version": current["version"],
                },
            )
        self.audit.record(
            report_id,
            actor,
            "assign",
            report["status"],
            updated["status"],
            {"event_id": event_id},
        )
        source_id = report["data"].get("event_id")
        if source_id:
            self._recompute_event(actor, source_id)
        if event_id:
            self._recompute_event(actor, event_id)
        return updated

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "report" and action == "assign":
            payload = data or {}
            return self.assign_report(
                actor, entity_id, payload.get("event_id"), expected_version
            )
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def batch_import(self, actor, kind, items, batch_key=None):
        kind = self.rules.normalize_kind(kind)
        if not batch_key:
            batch_key = str(uuid4())
        results = []
        for item in items:
            ref = item.get("ref") or item.get("client_ref") or str(uuid4())
            existing_id = self.repository.get_batch_item(batch_key, ref)
            if existing_id:
                results.append({"ref": ref, "entity_id": existing_id, "status": "skipped"})
                continue
            data = item.get("data", item)
            try:
                entity = self.create(actor, kind, data)
                self.repository.save_batch_item(batch_key, ref, entity["id"])
                results.append({"ref": ref, "entity_id": entity["id"], "status": "created"})
            except DomainError as exc:
                results.append({
                    "ref": ref,
                    "status": "failed",
                    "error": str(exc),
                    "type": type(exc).__name__,
                })
        return {"batch_key": batch_key, "results": results}

    def migrate(self):
        actor = Actor("system", "admin")
        events = self.repository.list_entities(kind="event")
        migrated = 0
        for event in events:
            reports = event["data"].get("reports")
            if not reports:
                continue
            self._create_reports_for_event(actor, event["id"], reports)
            merged = dict(event["data"])
            merged.pop("reports", None)
            self.repository.update_entity(event["id"], None, event["status"], merged)
            self._recompute_event(actor, event["id"])
            migrated += 1
        return {"migrated_events": migrated}

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
