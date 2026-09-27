from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine, trace_downstream


def _review_progress(nodes):
    progress = {"total": len(nodes), "source": 0, "pending": 0, "confirmed": 0, "cleared": 0, "untracked": 0}
    for node in nodes:
        if node.get("is_source"):
            progress["source"] += 1
            continue
        result = node.get("review_result")
        if result in ("pending", "confirmed", "cleared"):
            progress[result] += 1
        else:
            progress["untracked"] += 1
    return progress


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
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
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
        if entity["kind"] == "consignment" and action == "quarantine":
            self._flag_downstream(actor, updated)
        return updated

    def _flag_downstream(self, actor, source):
        """确认虫害后，按来源关系逐级标记所有下游批次和涉及设施为待复核。"""
        consignments = self.repository.list_entities(kind="consignment")
        links = [
            {"id": item["id"], "parent_id": item["data"].get("source_batch_id")}
            for item in consignments
        ]
        downstream_ids = trace_downstream(links, source["id"])[1:]
        by_id = {item["id"]: item for item in consignments}
        facility_ids = []
        if source["data"].get("facility_id"):
            facility_ids.append(source["data"]["facility_id"])
        for consignment_id in downstream_ids:
            target = by_id[consignment_id]
            if target["status"] not in ("declared", "inspected"):
                continue
            self.transition(
                actor,
                consignment_id,
                "mark_review",
                {
                    "outbreak_source": source["id"],
                    "review_previous_status": target["status"],
                    "review_result": "pending",
                },
            )
            if target["data"].get("facility_id"):
                facility_ids.append(target["data"]["facility_id"])
        for facility_id in dict.fromkeys(facility_ids):
            facility = self.repository.get_entity(facility_id)
            if facility and facility["status"] in ("registered", "traced"):
                self.transition(
                    actor,
                    facility_id,
                    "mark_review",
                    {
                        "outbreak_source": source["id"],
                        "review_previous_status": facility["status"],
                        "review_result": "pending",
                    },
                )

    def trace_view(self, source_id):
        """返回从源头批次出发的整条传播关系与复核处理进度。"""
        source = self.repository.get_entity(source_id)
        if not source or source["kind"] != "consignment":
            raise NotFoundError("consignment not found: " + source_id)
        consignments = self.repository.list_entities(kind="consignment")
        by_id = {item["id"]: item for item in consignments}
        children = {}
        for item in consignments:
            parent = item["data"].get("source_batch_id")
            if parent:
                children.setdefault(parent, []).append(item["id"])
        batches = []
        facility_ids = []
        queue = [(source_id, 0)]
        visited = set()
        while queue:
            current, depth = queue.pop(0)
            if current in visited or current not in by_id:
                continue
            visited.add(current)
            entity = by_id[current]
            facility_id = entity["data"].get("facility_id")
            if facility_id:
                facility_ids.append(facility_id)
            batches.append(
                {
                    "id": entity["id"],
                    "code": entity["data"].get("code"),
                    "status": entity["status"],
                    "depth": depth,
                    "is_source": entity["id"] == source_id,
                    "parent_id": entity["data"].get("source_batch_id"),
                    "facility_id": facility_id,
                    "review_result": entity["data"].get("review_result"),
                }
            )
            for child_id in children.get(current, []):
                queue.append((child_id, depth + 1))
        facilities = []
        for facility_id in dict.fromkeys(facility_ids):
            facility = self.repository.get_entity(facility_id)
            if facility:
                facilities.append(
                    {
                        "id": facility["id"],
                        "name": facility["data"].get("name"),
                        "status": facility["status"],
                        "review_result": facility["data"].get("review_result"),
                    }
                )
        return {
            "source": source,
            "batches": batches,
            "facilities": facilities,
            "progress": {
                "batches": _review_progress(batches),
                "facilities": _review_progress(facilities),
            },
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
