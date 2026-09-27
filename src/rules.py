from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_consignment(actor, data, lookup):
    if data.get("origin") == data.get("destination"):
        raise ValidationError("origin and destination must differ")
    source_id = data.get("source_batch_id")
    if source_id:
        if data.get("id") == source_id:
            raise ValidationError("source_batch_id cannot reference itself")
        if not _find_one(lookup, "consignment", "id", source_id):
            raise ValidationError("unknown source batch: " + str(source_id))
    facility_id = data.get("facility_id")
    if facility_id and not _find_one(lookup, "facility", "id", facility_id):
        raise ValidationError("unknown receiving facility: " + str(facility_id))


def _validate_quarantine(actor, entity, data, lookup):
    if not data.get("pest_found"):
        raise ValidationError("pest_found must be true for quarantine")
    return {"quarantined_by": actor.user_id}


def _validate_confirm_review(actor, entity, data, lookup):
    return {"review_result": "confirmed", "reviewed_by": actor.user_id}


def _validate_clear_review(actor, entity, data, lookup):
    previous = entity.get("data", {}).get("review_previous_status")
    if previous not in ("declared", "inspected", "registered", "traced"):
        raise ValidationError("no restorable previous status recorded")
    return {
        "next_status": previous,
        "review_result": "cleared",
        "reviewed_by": actor.user_id,
    }


def _validate_release(actor, entity, data, lookup):
    if data.get("pest_found"):
        raise ValidationError("pest-positive consignment cannot be released")
    if data.get("treatment") not in ("none", "completed", "certified"):
        raise ValidationError("release requires a valid treatment state")
    return {"released_by": actor.user_id}


def trace_downstream(consignments, start_id):
    pending = [start_id]
    visited = set()
    result = []
    while pending:
        current = pending.pop(0)
        if current in visited:
            continue
        visited.add(current)
        result.append(current)
        for item in consignments:
            if item.get("parent_id") == current:
                pending.append(item.get("id"))
    return result


CUSTOM_CREATE = {'consignment': _validate_consignment}
CUSTOM_TRANSITIONS = {('consignment', 'quarantine'): _validate_quarantine, ('consignment', 'release'): _validate_release, ('consignment', 'confirm_review'): _validate_confirm_review, ('consignment', 'clear_review'): _validate_clear_review, ('facility', 'confirm_review'): _validate_confirm_review, ('facility', 'clear_review'): _validate_clear_review}


class RuleEngine:
    ALIASES = {'consignments': 'consignment', 'facilities': 'facility'}
    INITIAL_STATUS = {'consignment': 'declared', 'facility': 'registered'}
    TRANSITIONS = {'consignment': {'inspect': (('declared',), 'inspected'), 'quarantine': (('inspected',), 'quarantined'), 'release': (('inspected',), 'released'), 'destroy': (('quarantined',), 'destroyed'), 'recheck': (('quarantined',), 'inspected'), 'mark_review': (('declared', 'inspected'), 'pending_review'), 'confirm_review': (('pending_review',), 'quarantined'), 'clear_review': (('pending_review',), None)}, 'facility': {'trace': (('registered',), 'traced'), 'mark_review': (('registered', 'traced'), 'pending_review'), 'confirm_review': (('pending_review',), 'restricted'), 'clear_review': (('pending_review',), None)}}
    CREATE_REQUIRED = {'consignment': ('code', 'origin', 'destination'), 'facility': ('name', 'address')}
    ACTION_REQUIRED = {('consignment', 'inspect'): ('inspector', 'inspection_result'), ('consignment', 'quarantine'): ('pest_found', 'sample_id'), ('consignment', 'release'): ('pest_found', 'treatment'), ('consignment', 'destroy'): ('method', 'witnessed_by'), ('consignment', 'recheck'): ('sample_id',), ('consignment', 'confirm_review'): ('sample_id',), ('consignment', 'clear_review'): ('reason',), ('facility', 'clear_review'): ('reason',), ('facility', 'trace'): ('consignment_ids',)}
    CREATE_ROLES = {'consignment': ('admin', 'inspector'), 'facility': ('admin', 'quarantine')}
    ROLE_ACTIONS = {'inspect': ('admin', 'inspector'), 'quarantine': ('admin', 'quarantine'), 'release': ('admin', 'quarantine'), 'destroy': ('admin', 'quarantine'), 'recheck': ('admin', 'inspector'), 'trace': ('admin', 'quarantine'), 'mark_review': ('admin', 'quarantine'), 'confirm_review': ('admin', 'quarantine'), 'clear_review': ('admin', 'quarantine')}

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
            next_status = extra.pop("next_status", None) or next_status
            patch.update(extra)
        if not next_status:
            raise InvalidTransition("no target status for action %s" % action)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
