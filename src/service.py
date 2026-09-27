"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, ValidationError, integer, text
from .repository import Repository
from .rules import DomainRules, select_destination


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create_order(reference, self.rules.INITIAL_STATE, prepared, actor.user_id, select_destination)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        bed_action = self.rules.bed_action_for(action, record["payload"])
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        details = {"summary": summary, "input": data or {}, "from": record["state"], "to": new_state}
        if bed_action is not None:
            details["destination"] = record["payload"].get("destination")
            details["destination_hospital_id"] = record["payload"].get("destination_hospital_id")
            details["bed_action"] = bed_action
            details["bed_status"] = new_payload.get("bed_status")
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details=details,
            bed_action=bed_action,
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    def _ensure_hospital_role(self, actor: Actor) -> None:
        if not self.rules.role_can_manage_hospitals(actor.role):
            raise PermissionDenied("角色无权维护医院目录")

    def list_hospitals(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_hospitals()

    def create_hospital(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_hospital_role(actor)
        data = self.rules.validate_hospital(payload or {})
        return self.repository.create_hospital(data, actor.user_id)

    def update_hospital(self, actor: Actor, hospital_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_hospital_role(actor)
        changes = self.rules.validate_hospital_update(payload or {})
        return self.repository.update_hospital(hospital_id, changes, actor.user_id)

    def adjust_hospital_beds(self, actor: Actor, hospital_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_hospital_role(actor)
        delta = integer(payload or {}, "delta", -1000, 1000)
        if delta == 0:
            raise ValidationError("delta不能为0")
        return self.repository.adjust_hospital_beds(hospital_id, delta, actor.user_id)
