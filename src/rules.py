"""急救车调度与目的地分流领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "received"
CREATE_ROLES = {'dispatcher'}
ACTION_ROLES = {'assign': {'dispatcher'}, 'enroute': {'dispatcher', 'paramedic'}, 'arrive': {'paramedic'}, 'transport': {'paramedic', 'hospital_coordinator'}, 'handover': {'paramedic', 'hospital_coordinator'}, 'cancel': {'dispatcher'}}
TRANSITIONS = {'assign': {'received': 'assigned'}, 'enroute': {'assigned': 'enroute'}, 'arrive': {'enroute': 'onscene'}, 'transport': {'onscene': 'transporting'}, 'handover': {'transporting': 'closed'}, 'cancel': {'received': 'cancelled', 'assigned': 'cancelled', 'enroute': 'cancelled'}}
HOSPITAL_ROLES = {'dispatcher', 'hospital_coordinator'}
CAPABILITY_LEVELS = {"BLS": 1, "ALS": 2}
MAX_DRIVE_MINUTES = 20


def select_destination(hospitals: Iterable[Dict[str, Any]], required_capability: str, max_drive_minutes: int = MAX_DRIVE_MINUTES) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """按能力筛选后从车程不超过max_drive_minutes的医院里选最快且有床的一家。

    返回(选中医院, 改选记录)。排在前面的医院若床位已满会被跳过并记入改选记录，
    没有任何能力匹配的医院时返回(None, [])。
    """
    required_level = CAPABILITY_LEVELS.get(required_capability, 0)
    capable = [h for h in hospitals if int(h.get("active", 1)) and CAPABILITY_LEVELS.get(h.get("capability"), 0) >= required_level]
    if not capable:
        return None, []
    ranked = sorted(capable, key=lambda h: (int(h["drive_minutes"]), int(h["id"])))
    reroutes: List[Dict[str, Any]] = []
    for hospital in ranked:
        if int(hospital["drive_minutes"]) > max_drive_minutes:
            break
        if int(hospital["available_beds"]) > 0:
            return hospital, reroutes
        reroutes.append({"hospital_id": hospital["id"], "hospital_name": hospital["name"], "reason": "床位已满"})
    return None, reroutes


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        all_roles.update(HOSPITAL_ROLES)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_manage_hospitals(self, role: str) -> bool:
        return role == "admin" or role in HOSPITAL_ROLES

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        choice(p, "patient_priority", ["critical", "urgent", "stable"])
        number(p, "distance_km", 0)
        integer(p, "eta_minutes", 1, 240)
        choice(p, "required_capability", ["BLS", "ALS"])
        choice(p, "vehicle_capability", ["BLS", "ALS"])
        text(p, "location")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        weights = {"critical": 100, "urgent": 60, "stable": 25}
        p["priority_score"] = round(weights[p["patient_priority"]] + float(p["distance_km"]) - float(p["eta_minutes"]) * 0.5, 2)
        p["sla_minutes"] = {"critical": 8, "urgent": 20, "stable": 45}[p["patient_priority"]]
        p["capability_ok"] = p["required_capability"] == p["vehicle_capability"] or p["vehicle_capability"] == "ALS"
        return p

    def validate_hospital(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "code")
        text(p, "name")
        choice(p, "capability", ["BLS", "ALS"])
        integer(p, "total_beds", 0)
        integer(p, "drive_minutes", 0, 240)
        return p

    def validate_hospital_update(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        changes: Dict[str, Any] = {}
        if "name" in payload:
            changes["name"] = text(payload, "name")
        if "capability" in payload:
            changes["capability"] = choice(payload, "capability", ["BLS", "ALS"])
        if "total_beds" in payload:
            changes["total_beds"] = integer(payload, "total_beds", 0)
        if "drive_minutes" in payload:
            changes["drive_minutes"] = integer(payload, "drive_minutes", 0, 240)
        if "active" in payload:
            changes["active"] = 1 if boolean(payload, "active") else 0
        if not changes:
            raise ValidationError("没有可更新的字段")
        return changes

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        vehicle_id = payload.get("assigned_vehicle_id")
        if vehicle_id:
            for item in existing:
                if item["state"] in {"closed", "cancelled"}:
                    continue
                if item["payload"].get("assigned_vehicle_id") == vehicle_id:
                    raise Conflict("同一车辆存在未结束任务")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "assign":
            if not boolean(data, "vehicle_available"):
                raise ValidationError("车辆当前不可用")
            if not p["capability_ok"]:
                raise ValidationError("车辆能力不满足病人需求")
            changes["assigned_vehicle_id"] = text(data, "vehicle_id")
            changes["assigned"] = True
            summary = "已完成派车"
        elif action == "enroute":
            traffic = choice(data, "traffic_level", ["low", "medium", "high"])
            factor = {"low": 1.0, "medium": 1.2, "high": 1.5}[traffic]
            changes["revised_eta_minutes"] = round(float(p["eta_minutes"]) * factor, 1)
            changes["traffic_level"] = traffic
            summary = "车辆已出发"
        elif action == "arrive":
            changes["on_scene"] = boolean(data, "on_scene")
            if not changes["on_scene"]:
                raise ValidationError("到场信息未确认")
            summary = "车辆已到场"
        elif action == "transport":
            if p.get("bed_status") != "held":
                raise ValidationError("目的地没有已锁定的床位")
            changes["transporting"] = True
            summary = "开始转运至%s" % p.get("destination", "目的地医院")
        elif action == "handover":
            if not boolean(data, "handover_accepted"):
                raise ValidationError("医院尚未接收")
            changes["handover_accepted"] = True
            changes["bed_status"] = "consumed"
            summary = "交接完成，%s床位已核销" % p.get("destination", "目的地医院")
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            if p.get("bed_status") == "held":
                changes["bed_status"] = "released"
                summary = "任务取消，已释放%s床位" % p.get("destination", "目的地医院")
            else:
                summary = "任务取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    def bed_action_for(self, action: str, payload: Dict[str, Any]) -> Optional[str]:
        """状态动作对床位台账的影响：取消释放、交接核销。"""
        if payload.get("bed_status") != "held":
            return None
        if action == "cancel":
            return "release"
        if action == "handover":
            return "consume"
        return None
