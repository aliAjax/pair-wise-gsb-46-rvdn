"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, text
from .repository import Repository
from .rules import DomainRules


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
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

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
        data = dict(data or {})
        selection = None
        if action == "assign":
            selection = self._select_hospital(actor, record)
            data["hospital"] = selection["hospital"]
            data["reselect_reasons"] = selection["reasons"]
        new_state, new_payload, summary = self.rules.apply_action(record, action, data)
        details = {"summary": summary, "input": data, "from": record["state"], "to": new_state}
        release_on_cancel = action == "cancel" and record["payload"].get("bed_reserved") and record["payload"].get("hospital_id")
        if release_on_cancel:
            details["released_bed"] = {"hospital_id": record["payload"]["hospital_id"], "hospital_name": record["payload"].get("hospital_name", "")}
        try:
            result = self.repository.mutate(
                record_id=record_id,
                expected_version=int(expected_version),
                state=new_state,
                payload=new_payload,
                actor_id=actor.user_id,
                action=action,
                details=details,
            )
        except Exception:
            if selection is not None:
                self.repository.release_bed(selection["hospital"]["id"])
            raise
        if release_on_cancel:
            self.repository.release_bed(record["payload"]["hospital_id"])
        return result

    def _select_hospital(self, actor: Actor, record: Dict[str, Any]) -> Dict[str, Any]:
        """按能力筛选、20分钟车程内有床、车程最快排序选院并原子占床；占床失败自动改选下一家。"""
        required = record["payload"]["required_capability"]
        candidates = self.rules.rank_hospitals(self.repository.list_hospitals(), required, self.rules.MAX_DRIVE_MINUTES)
        reasons: List[Dict[str, Any]] = []
        for hospital in candidates:
            if hospital["available_beds"] <= 0:
                reasons.append({"hospital_id": hospital["id"], "hospital_name": hospital["name"], "reason": "无可用床位"})
                continue
            if self.repository.reserve_bed(hospital["id"]):
                return {"hospital": hospital, "reasons": reasons}
            reasons.append({"hospital_id": hospital["id"], "hospital_name": hospital["name"], "reason": "床位刚被其他任务占用"})
        self.audit.note(
            record["id"],
            actor.user_id,
            "assign_rejected",
            {"summary": "%s分钟内无具备%s能力且有空床的医院，任务无法受理" % (self.rules.MAX_DRIVE_MINUTES, required), "required_capability": required, "skipped_hospitals": reasons},
        )
        raise Conflict("%s分钟内无具备%s能力且有空床的医院，任务无法受理" % (self.rules.MAX_DRIVE_MINUTES, required))

    def list_hospitals(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_hospitals()

    def get_hospital(self, actor: Actor, hospital_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_hospital(hospital_id)

    def create_hospital(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_manage_hospital(actor.role):
            raise PermissionDenied("角色无权维护医院目录")
        p = self.rules.validate_hospital(payload or {})
        return self.repository.create_hospital(p["name"], p["capabilities"], p["total_beds"], p["available_beds"], p["drive_minutes"], actor.user_id)

    def update_hospital(self, actor: Actor, hospital_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_manage_hospital(actor.role):
            raise PermissionDenied("角色无权维护医院目录")
        current = self.repository.get_hospital(hospital_id)
        fields = self.rules.validate_hospital_update(payload or {})
        merged = dict(current)
        merged.update(fields)
        if merged["available_beds"] > merged["total_beds"]:
            raise ValidationError("available_beds不能大于total_beds")
        return self.repository.update_hospital(hospital_id, fields, actor.user_id)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
