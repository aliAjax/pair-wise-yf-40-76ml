from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError
from .rules import REVIEW_FLAGGABLE_STATUSES, RuleEngine, trace_downstream_tree


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
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        if kind == "review":
            self._flag_review_target(actor, entity)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "review" and action in ("confirm", "exclude"):
            self._check_review_target(entity)
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
        self._after_transition(actor, updated, action)
        return updated

    def _after_transition(self, actor, entity, action):
        if entity["kind"] == "consignment" and action == "quarantine":
            self.trace_from(actor, entity["id"])
        elif entity["kind"] == "review" and action in ("confirm", "exclude"):
            self._apply_review_decision(actor, entity)

    def _check_review_target(self, review):
        target_id = review["data"].get("target_id")
        target = self.repository.get_entity(target_id) if target_id else None
        if not target:
            raise NotFoundError("review target not found: " + str(target_id))
        if target["status"] != "under_review":
            raise InvalidTransition(
                "review target %s is not under review" % target_id
            )
        return target

    def _flag_review_target(self, actor, review):
        target_id = review["data"].get("target_id")
        target = self.repository.get_entity(target_id) if target_id else None
        if not target:
            return
        flaggable = REVIEW_FLAGGABLE_STATUSES.get(target["kind"], ())
        if target["status"] not in flaggable:
            return
        self.transition(
            actor,
            target["id"],
            "flag_review",
            {
                "review_prior_status": target["status"],
                "review_id": review["id"],
                "trace_id": review["data"].get("trace_id"),
            },
        )

    def _apply_review_decision(self, actor, review):
        target = self._check_review_target(review)
        confirmed = review["status"] == "confirmed"
        if confirmed:
            next_status = "quarantined" if target["kind"] == "consignment" else "locked"
        else:
            next_status = target["data"].get("review_prior_status") or (
                "declared" if target["kind"] == "consignment" else "registered"
            )
        merged = dict(target["data"])
        merged.update(
            {
                "review_id": review["id"],
                "review_decision": review["status"],
                "reviewed_by": actor.user_id,
            }
        )
        updated = self.repository.update_entity(target["id"], None, next_status, merged)
        self.audit.record(
            target["id"],
            actor,
            "review_" + review["status"],
            target["status"],
            next_status,
            {"review_id": review["id"]},
        )
        if confirmed and target["kind"] == "consignment":
            self.trace_from(actor, target["id"], trace_id=review["data"].get("trace_id"))
        return updated

    def trace_from(self, actor, consignment_id, trace_id=None):
        root = self.repository.get_entity(consignment_id)
        if not root or root["kind"] != "consignment":
            raise NotFoundError("consignment not found: " + str(consignment_id))
        if root["status"] != "quarantined":
            raise InvalidTransition(
                "trace requires a quarantined consignment, found " + root["status"]
            )
        trace_id = trace_id or consignment_id
        consignments = self.repository.list_entities(kind="consignment")
        by_id = {item["id"]: item for item in consignments}
        chain = trace_downstream_tree(self._links(consignments), consignment_id)
        pending_targets = {
            review["data"].get("target_id")
            for review in self.repository.list_entities(kind="review")
            if review["status"] == "pending"
        }
        created = []
        for node in chain:
            if node["depth"] == 0:
                continue
            review = self._ensure_review(
                actor, trace_id, by_id[node["id"]], pending_targets, depth=node["depth"]
            )
            if review:
                created.append(review)
        facility_ids = []
        for node in chain:
            facility_id = by_id[node["id"]]["data"].get("receiving_facility_id")
            if facility_id and facility_id not in facility_ids:
                facility_ids.append(facility_id)
        for facility_id in facility_ids:
            facility = self.repository.get_entity(facility_id)
            if facility:
                review = self._ensure_review(actor, trace_id, facility, pending_targets)
                if review:
                    created.append(review)
        return created

    def _ensure_review(self, actor, trace_id, target, pending_targets, depth=None):
        if target["id"] in pending_targets:
            return None
        flaggable = REVIEW_FLAGGABLE_STATUSES.get(target["kind"], ())
        if target["status"] not in flaggable:
            return None
        data = {
            "trace_id": trace_id,
            "target_kind": target["kind"],
            "target_id": target["id"],
            "reason": "downstream of quarantined consignment " + str(trace_id),
        }
        if depth is not None:
            data["depth"] = depth
        review = self.create(actor, "review", data)
        pending_targets.add(target["id"])
        return review

    @staticmethod
    def _links(consignments):
        return [
            {"id": item["id"], "parent_id": item["data"].get("source_batch_id")}
            for item in consignments
        ]

    def trace_chain(self, consignment_id):
        root = self.repository.get_entity(consignment_id)
        if not root or root["kind"] != "consignment":
            raise NotFoundError("consignment not found: " + str(consignment_id))
        consignments = self.repository.list_entities(kind="consignment")
        by_id = {item["id"]: item for item in consignments}
        chain = trace_downstream_tree(self._links(consignments), consignment_id)
        reviews = self.repository.list_entities(kind="review")

        def latest_review(target_id):
            found = [
                item for item in reviews if item["data"].get("target_id") == target_id
            ]
            if not found:
                return None
            review = found[-1]
            return {
                "id": review["id"],
                "status": review["status"],
                "trace_id": review["data"].get("trace_id"),
            }

        nodes = []
        facility_ids = []
        for node in chain:
            item = by_id[node["id"]]
            facility_id = item["data"].get("receiving_facility_id")
            if facility_id and facility_id not in facility_ids:
                facility_ids.append(facility_id)
            nodes.append(
                {
                    "id": item["id"],
                    "code": item["data"].get("code"),
                    "depth": node["depth"],
                    "status": item["status"],
                    "source_batch_id": item["data"].get("source_batch_id"),
                    "receiving_facility_id": facility_id,
                    "review": latest_review(item["id"]),
                }
            )
        facilities = []
        for facility_id in facility_ids:
            facility = self.repository.get_entity(facility_id)
            if facility:
                facilities.append(
                    {
                        "id": facility["id"],
                        "name": facility["data"].get("name"),
                        "status": facility["status"],
                        "review": latest_review(facility["id"]),
                    }
                )
        progress = {"pending": 0, "confirmed": 0, "excluded": 0, "total": 0}
        for entry in nodes + facilities:
            review = entry["review"]
            if review and review["status"] in ("pending", "confirmed", "excluded"):
                progress[review["status"]] += 1
                progress["total"] += 1
        return {
            "root": consignment_id,
            "nodes": nodes,
            "facilities": facilities,
            "progress": progress,
        }

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
