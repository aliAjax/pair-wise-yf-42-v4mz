from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


INBREEDING_THRESHOLD = 0.125


def _validate_animal(actor, data, lookup):
    if data.get("sex") not in ("male", "female", "unknown"):
        raise ValidationError("sex must be male, female or unknown")


def _validate_pairing_create(actor, data, lookup):
    sire_id = data.get("sire_id")
    dam_id = data.get("dam_id")
    if sire_id and dam_id and sire_id == dam_id:
        raise ValidationError("父本和母本不能是同一只动物")
    for label, animal_id in (("父本", sire_id), ("母本", dam_id)):
        if animal_id and not _find_one(lookup, "animal", "id", animal_id):
            raise ValidationError("%s不存在：%s" % (label, animal_id))


def inbreeding_coefficient(sire, dam):
    if not sire or not dam:
        return 1.0
    sire_id = sire.get("id")
    dam_id = dam.get("id")
    if sire_id is None or dam_id is None:
        return 0.0
    if sire_id == dam_id:
        return 0.5
    if sire.get("sire_id") == dam_id or dam.get("sire_id") == sire_id:
        return 0.25
    return 0.0


def _animal_view(entity):
    view = dict(entity.get("data") or {})
    view["id"] = entity.get("id")
    return view


def kinship_coefficient(sire, dam):
    """Inbreeding coefficient between two animal entities (id + data)."""
    return inbreeding_coefficient(_animal_view(sire), _animal_view(dam))


def health_blockers(sire, dam):
    """Reasons a pairing must not proceed because of parent health/status."""
    blockers = []
    for label, animal in (("父本", sire), ("母本", dam)):
        if not animal:
            blockers.append("%s信息缺失" % label)
            continue
        name = (animal.get("data") or {}).get("name") or animal.get("id")
        status = animal.get("status")
        if status == "quarantined":
            blockers.append("%s「%s」正在隔离" % (label, name))
        elif status == "deceased":
            blockers.append("%s「%s」已死亡" % (label, name))
        elif status != "active":
            blockers.append("%s「%s」状态为 %s，不可繁育" % (label, name, status))
    return blockers


def pairing_blockers(sire, dam):
    """All reasons a pairing must not be approved right now."""
    blockers = health_blockers(sire, dam)
    if sire and dam:
        coefficient = kinship_coefficient(sire, dam)
        if coefficient > INBREEDING_THRESHOLD:
            blockers.append(
                "亲缘系数 %.2f 高于阈值 %.2f，近亲风险过高"
                % (coefficient, INBREEDING_THRESHOLD)
            )
    return blockers


def _resolve_parent(lookup, entity, data, field):
    animal_id = data.get(field) or (entity.get("data") or {}).get(field)
    if not animal_id:
        return None, None
    return animal_id, _find_one(lookup, "animal", "id", animal_id)


def _validate_pairing(actor, entity, data, lookup):
    sire_id, sire = _resolve_parent(lookup, entity, data, "sire_id")
    dam_id, dam = _resolve_parent(lookup, entity, data, "dam_id")
    if not sire or not dam:
        raise ValidationError("配对需要两只已登记的亲本动物")
    blockers = pairing_blockers(sire, dam)
    if blockers:
        raise ValidationError("；".join(blockers))
    return {
        "approved_by": actor.user_id,
        "sire_id": sire_id,
        "dam_id": dam_id,
        "sire_version": sire["version"],
        "dam_version": dam["version"],
        "inbreeding_coefficient": kinship_coefficient(sire, dam),
        "review_reason": "",
    }


def _validate_pairing_complete(actor, entity, data, lookup):
    stored = entity.get("data") or {}
    sire = _find_one(lookup, "animal", "id", stored.get("sire_id"))
    dam = _find_one(lookup, "animal", "id", stored.get("dam_id"))
    blockers = health_blockers(sire, dam)
    if blockers:
        raise ValidationError("无法安排完成：" + "；".join(blockers))
    return {}


CUSTOM_CREATE = {'animal': _validate_animal, 'pairing': _validate_pairing_create}
CUSTOM_TRANSITIONS = {('pairing', 'approve'): _validate_pairing, ('pairing', 'complete'): _validate_pairing_complete}


class RuleEngine:
    ALIASES = {'animals': 'animal', 'pairings': 'pairing', 'transfers': 'transfer'}
    INITIAL_STATUS = {'animal': 'active', 'pairing': 'proposed', 'transfer': 'planned'}
    TRANSITIONS = {'animal': {'mark_deceased': (('active',), 'deceased'), 'quarantine_animal': (('active',), 'quarantined'), 'release_quarantine': (('quarantined',), 'active')}, 'pairing': {'approve': (('proposed',), 'approved'), 'reject': (('proposed',), 'rejected'), 'complete': (('approved',), 'completed')}, 'transfer': {'authorize': (('planned',), 'authorized'), 'ship': (('authorized',), 'in_transit'), 'arrive': (('in_transit',), 'completed')}}
    CREATE_REQUIRED = {'animal': ('name', 'sex'), 'pairing': ('proposed_by',), 'transfer': ('animal_id', 'from_institution', 'to_institution')}
    ACTION_REQUIRED = {('animal', 'mark_deceased'): ('cause',), ('animal', 'quarantine_animal'): ('reason',), ('pairing', 'approve'): ('approvals',), ('pairing', 'reject'): ('reason',), ('pairing', 'complete'): ('offspring_ids',), ('transfer', 'authorize'): ('permit_id',), ('transfer', 'ship'): ('transport_id',), ('transfer', 'arrive'): ('arrival_date',)}
    CREATE_ROLES = {'animal': ('admin', 'registrar'), 'pairing': ('admin', 'coordinator'), 'transfer': ('admin', 'registrar')}
    ROLE_ACTIONS = {'mark_deceased': ('admin', 'veterinarian'), 'quarantine_animal': ('admin', 'veterinarian'), 'release_quarantine': ('admin', 'veterinarian'), 'approve': ('admin', 'coordinator'), 'reject': ('admin', 'coordinator'), 'complete': ('admin', 'coordinator'), 'authorize': ('admin', 'registrar'), 'ship': ('admin', 'registrar'), 'arrive': ('admin', 'registrar')}

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
