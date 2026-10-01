from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import RuleEngine, derive_observed_at, recompute_event


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

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
        # Reports may still arrive embedded in an event payload; turn each one
        # into its own report record so ownership can change without re-entry.
        embedded = payload.pop("reports", None) if kind == "event" else None
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if kind == "event" and embedded:
            self._spawn_reports(actor, entity, embedded)
            entity = self._recompute_entity(entity_id, actor)
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def _spawn_reports(self, actor, event, embedded):
        event_id = event["id"]
        origin_time = event["data"].get("origin_time")
        for index, item in enumerate(embedded):
            if not isinstance(item, dict):
                continue
            data = dict(item)
            data.setdefault("code", "MIG-%s-%d" % (str(event_id)[:8], index + 1))
            data.setdefault(
                "observed_at",
                derive_observed_at(origin_time, data.get("time_offset")),
            )
            data["event_id"] = event_id
            self.rules.validate_create(actor, "report", data, self._lookup)
            report_id = str(data.pop("id", "") or uuid4())
            report = self.repository.create_entity(
                report_id, "report", self.rules.initial_status("report"), data, actor.user_id
            )
            self.audit.record(
                report_id, actor, "create", None, report["status"], {"kind": "report"}
            )

    def _recompute_entity(self, event_id, actor=None):
        entity = self.repository.get_entity(event_id)
        reports = self._lookup("report", "event_id", event_id)
        data = recompute_event(entity, reports)
        updated = self.repository.update_entity(
            event_id, entity["version"], entity["status"], data
        )
        if actor is not None:
            self.audit.record(
                event_id, actor, "recompute", None, updated["status"],
                {"reason": "reports_changed",
                 "station_count": data.get("station_count"),
                 "magnitude": data.get("magnitude")},
            )
        return updated

    def reassign_report(self, actor, report_id, event_id, expected_version=None):
        report = self.repository.get_entity(report_id)
        if not report or report["kind"] != "report":
            raise NotFoundError("report not found: " + report_id)
        if expected_version is None:
            # A fencing token is mandatory: it is the version the analyst saw
            # when deciding the new owner. Without it two concurrent moves
            # cannot be told apart from an intentional sequential move, so
            # "first submit wins" would be unenforceable.
            raise ValidationError(
                "expected_version is required for reassignment"
            )
        expected = int(expected_version)
        # Validation also enforces role and target event existence.
        try:
            _, patch = self.rules.validate_transition(
                actor, report, "reassign", {"event_id": event_id}, self._lookup
            )
        except ConflictError as exc:
            details = dict(exc.details or {})
            details.setdefault("report_id", report_id)
            details.setdefault("code", report["data"].get("code"))
            details.setdefault("event_id", report["data"].get("event_id"))
            details.setdefault("version", report["version"])
            details.setdefault("claimed_by", self.repository.last_reassign_actor(report_id))
            raise ConflictError(str(exc), details)
        target_event_id = patch["event_id"]
        try:
            updated, old_event_id, affected = self.repository.reassign_report(
                report_id, expected, target_event_id, recompute_event, actor
            )
        except ConflictError as exc:
            # The losing analyst must see who owns the report now.
            current = self.repository.get_entity(report_id)
            details = dict(exc.details or {})
            if current:
                details.setdefault("report_id", report_id)
                details.setdefault("code", current["data"].get("code"))
                details.setdefault("event_id", current["data"].get("event_id"))
                details.setdefault("version", current["version"])
            details.setdefault(
                "claimed_by", self.repository.last_reassign_actor(report_id)
            )
            raise ConflictError(str(exc), details)
        return updated, affected

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        if entity["kind"] == "report" and action == "reassign":
            data = dict(data or {})
            updated, _affected = self.reassign_report(
                actor, entity_id, data.get("event_id"), expected_version
            )
            return updated
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        if entity["kind"] == "event" and action == "publish":
            # Freeze the outgoing version; later ownership changes cannot
            # rewrite what was already sent out.
            self.repository.save_publication(
                entity_id,
                patch.get("communication_id"),
                updated["version"],
                patch.get("snapshot"),
                actor.user_id,
            )
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": {key: value for key, value in patch.items() if key != "snapshot"}},
        )
        return updated

    def import_reports(self, actor, reports):
        """Batch-create reports; each row is independent.

        Rows already present (by code) are skipped, so retrying a failed batch
        only fills in what was never written. Invalid rows are reported and do
        not abort the rest of the batch.
        """
        if not isinstance(reports, list):
            raise ValidationError("reports must be a list")
        created, skipped, failed = [], [], []
        touched_events = set()
        for index, raw in enumerate(reports):
            if not isinstance(raw, dict):
                failed.append({"index": index, "error": "row must be an object"})
                continue
            data = dict(raw)
            code = data.get("code")
            if code and self._lookup("report", "code", code):
                skipped.append({"index": index, "code": code})
                continue
            try:
                self.rules.validate_create(actor, "report", data, self._lookup)
                report_id = str(data.pop("id", "") or uuid4())
                if self.repository.get_entity(report_id):
                    raise ConflictError("entity already exists: " + report_id)
                report = self.repository.create_entity(
                    report_id,
                    "report",
                    self.rules.initial_status("report"),
                    data,
                    actor.user_id,
                )
                self.audit.record(
                    report_id, actor, "create", None, report["status"], {"kind": "report", "batch": True}
                )
                created.append({"index": index, "code": code, "id": report_id})
                touched_events.add(data.get("event_id"))
            except ConflictError as exc:
                # Lost a race against a concurrent importer for the same code.
                skipped.append(
                    {"index": index, "code": code, "reason": str(exc)}
                )
            except Exception as exc:
                failed.append({"index": index, "code": code, "error": str(exc)})
        # Recompute every event that gained reports so magnitudes and station
        # counts reflect the imported rows immediately.
        recomputed = []
        for event_id in sorted(e for e in touched_events if e):
            if self.repository.get_entity(event_id):
                self._recompute_entity(event_id, actor)
                recomputed.append(event_id)
        return {
            "created": created,
            "skipped": skipped,
            "failed": failed,
            "total": len(reports),
            "recomputed_events": recomputed,
        }

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None, event_id=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        if kind == "report" and event_id is not None:
            items = self.repository.find_entities("report", "event_id", event_id)
            if status:
                items = [item for item in items if item["status"] == status]
            return items
        return self.repository.list_entities(kind=kind, status=status)

    def publications(self, event_id=None):
        return self.repository.list_publications(event_id=event_id)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
