from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError
from .rules import RuleEngine

SYSTEM_ACTOR = Actor(user_id="system", role="system")


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
        if updated["kind"] == "animal" and updated["status"] in ("quarantined", "deceased"):
            self._return_pairings_for_review(updated)
        return updated

    def _return_pairings_for_review(self, animal):
        """Approved pairings lose their effect when a parent becomes unfit."""
        name = (animal["data"] or {}).get("name") or animal["id"]
        if animal["status"] == "quarantined":
            reason = "亲本「%s」正在隔离，配对自动退回待复核" % name
        else:
            reason = "亲本「%s」已死亡，配对自动退回待复核" % name
        pairing_ids = set()
        for field in ("sire_id", "dam_id"):
            for pairing in self.repository.find_entities("pairing", field, animal["id"]):
                pairing_ids.add(pairing["id"])
        for pairing_id in pairing_ids:
            self._return_pairing_for_review(pairing_id, reason, animal)

    def _return_pairing_for_review(self, pairing_id, reason, animal):
        for _attempt in (1, 2):
            pairing = self.repository.get_entity(pairing_id)
            if not pairing or pairing["status"] != "approved":
                return
            merged = dict(pairing["data"])
            merged["review_reason"] = reason
            try:
                self.repository.update_entity(
                    pairing_id, pairing["version"], "proposed", merged
                )
            except ConflictError:
                continue
            self.audit.record(
                pairing_id,
                SYSTEM_ACTOR,
                "auto_return",
                "approved",
                "proposed",
                {
                    "reason": reason,
                    "trigger": {
                        "kind": animal["kind"],
                        "id": animal["id"],
                        "status": animal["status"],
                    },
                },
            )
            return

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
