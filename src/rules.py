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


def _pedigree_view(entity):
    view = dict(entity.get("data") or {})
    view["id"] = entity.get("id")
    return view


def _parent_name(entity):
    return (entity.get("data") or {}).get("name") or entity.get("id")


def _animal_brief(entity):
    if entity is None:
        return None
    return {
        "id": entity["id"],
        "name": _parent_name(entity),
        "status": entity["status"],
        "version": entity["version"],
    }


def assess_parents(sire, dam):
    """Return (inbreeding coefficient, risk level, blocking reasons) for a sire/dam pair."""
    blockers = []
    for label, parent in (("父本", sire), ("母本", dam)):
        if parent is None:
            blockers.append("%s未指定或不存在" % label)
            continue
        status = parent.get("status")
        name = _parent_name(parent)
        if status == "quarantined":
            blockers.append("%s %s 正在隔离" % (label, name))
        elif status == "deceased":
            blockers.append("%s %s 已死亡" % (label, name))
        elif status != "active":
            blockers.append("%s %s 当前状态（%s）不可参与繁育" % (label, name, status))
    coefficient = None
    risk_level = None
    if sire is not None and dam is not None:
        coefficient = inbreeding_coefficient(_pedigree_view(sire), _pedigree_view(dam))
        if coefficient > INBREEDING_THRESHOLD:
            risk_level = "high"
            blockers.append(
                "亲缘风险过高：近交系数 %.3f 超过上限 %.3f"
                % (coefficient, INBREEDING_THRESHOLD)
            )
        elif coefficient > 0:
            risk_level = "medium"
        else:
            risk_level = "low"
    return coefficient, risk_level, blockers


def _validate_pairing(actor, entity, data, lookup):
    stored = dict(entity.get("data") or {})
    stored.update(data)
    sire = _find_one(lookup, "animal", "id", stored.get("sire_id"))
    dam = _find_one(lookup, "animal", "id", stored.get("dam_id"))
    _, _, blockers = assess_parents(sire, dam)
    if blockers:
        raise ValidationError("无法批准配对：" + "；".join(blockers))
    return {
        "approved_by": actor.user_id,
        "sire_id": sire["id"],
        "dam_id": dam["id"],
        "sire_version": sire["version"],
        "dam_version": dam["version"],
        "review_reason": None,
    }


def _validate_pairing_complete(actor, entity, data, lookup):
    stored = entity.get("data") or {}
    sire = _find_one(lookup, "animal", "id", stored.get("sire_id"))
    dam = _find_one(lookup, "animal", "id", stored.get("dam_id"))
    _, _, blockers = assess_parents(sire, dam)
    if blockers:
        raise ValidationError("无法安排完成：" + "；".join(blockers))
    return {}


CUSTOM_CREATE = {'animal': _validate_animal}
CUSTOM_TRANSITIONS = {('pairing', 'approve'): _validate_pairing, ('pairing', 'complete'): _validate_pairing_complete}


class RuleEngine:
    ALIASES = {'animals': 'animal', 'pairings': 'pairing', 'transfers': 'transfer'}
    INITIAL_STATUS = {'animal': 'active', 'pairing': 'proposed', 'transfer': 'planned'}
    TRANSITIONS = {'animal': {'mark_deceased': (('active',), 'deceased'), 'quarantine_animal': (('active',), 'quarantined'), 'release_quarantine': (('quarantined',), 'active')}, 'pairing': {'approve': (('proposed',), 'approved'), 'reject': (('proposed',), 'rejected'), 'complete': (('approved',), 'completed')}, 'transfer': {'authorize': (('planned',), 'authorized'), 'ship': (('authorized',), 'in_transit'), 'arrive': (('in_transit',), 'completed')}}
    CREATE_REQUIRED = {'animal': ('name', 'sex'), 'pairing': ('proposed_by',), 'transfer': ('animal_id', 'from_institution', 'to_institution')}
    ACTION_REQUIRED = {('animal', 'mark_deceased'): ('cause',), ('animal', 'quarantine_animal'): ('reason',), ('pairing', 'approve'): ('sire_id', 'dam_id', 'approvals'), ('pairing', 'reject'): ('reason',), ('pairing', 'complete'): ('offspring_ids',), ('transfer', 'authorize'): ('permit_id',), ('transfer', 'ship'): ('transport_id',), ('transfer', 'arrive'): ('arrival_date',)}
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
        required = self.ACTION_REQUIRED.get((kind, action), ())
        if required:
            known = dict(entity.get("data") or {})
            known.update(data)
            self._require(known, required)
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch

    def review_pairing(self, pairing, animals_by_id):
        data = pairing.get("data") or {}
        sire = animals_by_id.get(data.get("sire_id"))
        dam = animals_by_id.get(data.get("dam_id"))
        coefficient, risk_level, blockers = assess_parents(sire, dam)
        return {
            "id": pairing["id"],
            "status": pairing["status"],
            "version": pairing["version"],
            "proposed_by": data.get("proposed_by"),
            "approved_by": data.get("approved_by"),
            "review_reason": data.get("review_reason"),
            "sire": _animal_brief(sire),
            "dam": _animal_brief(dam),
            "sire_version": data.get("sire_version"),
            "dam_version": data.get("dam_version"),
            "inbreeding_coefficient": coefficient,
            "risk_level": risk_level,
            "blockers": blockers,
            "approvable": pairing["status"] == "proposed" and not blockers,
        }


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
