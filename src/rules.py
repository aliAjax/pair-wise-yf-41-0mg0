from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_station(actor, data, lookup):
    if not data.get("code"):
        raise ValidationError("station code is required")


def _validate_event(actor, data, lookup):
    if not data.get("title"):
        raise ValidationError("event title is required")


def _validate_report(actor, data, lookup):
    if not data.get("observed_at"):
        raise ValidationError("report observed_at is required")
    event_id = data.get("event_id")
    if event_id:
        target = _find_one(lookup, "event", "id", event_id)
        if not target:
            raise ValidationError("report event not found: " + str(event_id))


def _validate_associate(actor, entity, data, lookup):
    reports = lookup("report", "event_id", entity["id"]) or []
    if len(reports) < 2:
        raise ValidationError("two reports are required for association")
    return {"associated_count": len(reports)}


def _validate_review(actor, entity, data, lookup):
    reports = lookup("report", "event_id", entity["id"]) or []
    amplitudes = [
        item["data"].get("amplitude")
        for item in reports
        if item["data"].get("amplitude") is not None
    ]
    computed = magnitude_median(amplitudes) if amplitudes else None
    patch = {"station_count": len(reports)}
    if data.get("magnitude") is None and computed is not None:
        patch["magnitude"] = computed
    return patch


def _validate_publish(actor, entity, data, lookup):
    reports = lookup("report", "event_id", entity["id"]) or []
    snapshot = {
        "title": entity["data"].get("title"),
        "origin_time": entity["data"].get("origin_time"),
        "location": entity["data"].get("location"),
        "magnitude": entity["data"].get("magnitude"),
        "station_count": entity["data"].get("station_count"),
        "reports": [
            {
                "code": item["data"].get("code"),
                "station": item["data"].get("station"),
                "amplitude": item["data"].get("amplitude"),
            }
            for item in reports
        ],
        "communication_id": data.get("communication_id"),
        "published_at": utcnow(),
    }
    return {"published_snapshot": snapshot}


def _validate_report_assign(actor, entity, data, lookup):
    event_id = data.get("event_id")
    if event_id is not None:
        target = _find_one(lookup, "event", "id", event_id)
        if not target:
            raise ValidationError("target event not found: " + str(event_id))
    return {"event_id": event_id}


def associate_reports(reports, max_delta=120, max_distance=3.0):
    if not reports:
        return []
    anchor = reports[0]
    result = [anchor]
    for report in reports[1:]:
        if abs(float(report.get("time_offset", 0))) <= max_delta and float(report.get("distance_km", 0)) <= max_distance:
            result.append(report)
    return result


def magnitude_median(amplitudes):
    values = sorted(float(value) for value in amplitudes)
    if not values:
        raise ValidationError("amplitudes are required")
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0


def recompute_event(event, reports):
    """Derive magnitude/station_count from assigned reports.

    Returns (patch, next_status). Published/revised events revert to
    pending review (reviewed) when their report set changes.
    """
    station_count = len(reports)
    amplitudes = [
        item["data"].get("amplitude")
        for item in reports
        if item["data"].get("amplitude") is not None
    ]
    magnitude = magnitude_median(amplitudes) if amplitudes else None
    patch = {"station_count": station_count, "magnitude": magnitude}
    next_status = event["status"]
    if event["status"] in ("published", "revised"):
        next_status = "reviewed"
    return patch, next_status


CUSTOM_CREATE = {'station': _validate_station, 'event': _validate_event, 'report': _validate_report}
CUSTOM_TRANSITIONS = {
    ('event', 'associate'): _validate_associate,
    ('event', 'review'): _validate_review,
    ('event', 'publish'): _validate_publish,
    ('report', 'assign'): _validate_report_assign,
}


class RuleEngine:
    ALIASES = {'stations': 'station', 'events': 'event', 'reports': 'report'}
    INITIAL_STATUS = {'station': 'online', 'event': 'candidate', 'report': 'active'}
    TRANSITIONS = {
        'station': {
            'offline': (('online',), 'offline'),
            'online': (('offline',), 'online'),
        },
        'event': {
            'associate': (('candidate',), 'associated'),
            'review': (('associated',), 'reviewed'),
            'publish': (('reviewed',), 'published'),
            'revise': (('published', 'revised'), 'revised'),
            'withdraw': (('published', 'revised'), 'withdrawn'),
        },
        'report': {
            'assign': (('active',), 'active'),
        },
    }
    CREATE_REQUIRED = {
        'station': ('code', 'lat', 'lon'),
        'event': ('title', 'origin_time', 'location'),
        'report': ('observed_at',),
    }
    ACTION_REQUIRED = {
        ('station', 'offline'): ('reason',),
        ('event', 'review'): ('reviewer',),
        ('event', 'publish'): ('communication_id',),
        ('event', 'revise'): ('reason', 'magnitude'),
        ('event', 'withdraw'): ('reason',),
    }
    CREATE_ROLES = {
        'station': ('admin', 'station'),
        'event': ('admin', 'analyst'),
        'report': ('admin', 'station', 'analyst'),
    }
    ROLE_ACTIONS = {
        'offline': ('admin', 'station'),
        'online': ('admin', 'station'),
        'associate': ('admin', 'analyst'),
        'review': ('admin', 'reviewer'),
        'publish': ('admin', 'reviewer'),
        'revise': ('admin', 'reviewer'),
        'withdraw': ('admin', 'reviewer'),
        'assign': ('admin', 'analyst'),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
