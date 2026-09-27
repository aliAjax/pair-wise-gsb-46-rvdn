"""急救车调度与目的地分流领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "received"
CREATE_ROLES = {'dispatcher'}
ACTION_ROLES = {'assign': {'dispatcher'}, 'enroute': {'dispatcher', 'paramedic'}, 'arrive': {'paramedic'}, 'transport': {'paramedic', 'hospital_coordinator'}, 'handover': {'paramedic', 'hospital_coordinator'}, 'cancel': {'dispatcher'}}
TRANSITIONS = {'assign': {'received': 'assigned'}, 'enroute': {'assigned': 'enroute'}, 'arrive': {'enroute': 'onscene'}, 'transport': {'onscene': 'transporting'}, 'handover': {'transporting': 'closed'}, 'cancel': {'received': 'cancelled', 'assigned': 'cancelled', 'enroute': 'cancelled'}}
CAPABILITIES = ("BLS", "ALS")
HOSPITAL_ROLES = {'hospital_coordinator'}
MAX_DRIVE_MINUTES = 20


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    MAX_DRIVE_MINUTES = MAX_DRIVE_MINUTES

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_manage_hospital(self, role: str) -> bool:
        return role == "admin" or role in HOSPITAL_ROLES

    def hospital_covers(self, capabilities: Iterable[str], required_capability: str) -> bool:
        caps = set(capabilities)
        return "ALS" in caps or required_capability in caps

    def rank_hospitals(self, hospitals: Iterable[Dict[str, Any]], required_capability: str, max_drive_minutes: int = MAX_DRIVE_MINUTES) -> List[Dict[str, Any]]:
        candidates = [
            hospital for hospital in hospitals
            if self.hospital_covers(hospital["capabilities"], required_capability) and int(hospital["drive_minutes"]) <= max_drive_minutes
        ]
        return sorted(candidates, key=lambda hospital: (hospital["drive_minutes"], hospital["id"]))

    def _validate_capabilities(self, payload: Dict[str, Any]) -> List[str]:
        caps = text_list(payload, "capabilities", 1)
        if any(cap not in CAPABILITIES for cap in caps):
            raise ValidationError("capabilities只能是%s" % "/".join(CAPABILITIES))
        return caps

    def validate_hospital(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        p["name"] = text(p, "name")
        p["capabilities"] = self._validate_capabilities(p)
        p["total_beds"] = integer(p, "total_beds", 0)
        p["drive_minutes"] = integer(p, "drive_minutes", 1, 240)
        if p.get("available_beds") is None:
            p["available_beds"] = p["total_beds"]
        else:
            p["available_beds"] = integer(p, "available_beds", 0)
        if p["available_beds"] > p["total_beds"]:
            raise ValidationError("available_beds不能大于total_beds")
        return p

    def validate_hospital_update(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        result: Dict[str, Any] = {}
        if "name" in p:
            result["name"] = text(p, "name")
        if "capabilities" in p:
            result["capabilities"] = self._validate_capabilities(p)
        if "total_beds" in p:
            result["total_beds"] = integer(p, "total_beds", 0)
        if "drive_minutes" in p:
            result["drive_minutes"] = integer(p, "drive_minutes", 1, 240)
        if "available_beds" in p:
            result["available_beds"] = integer(p, "available_beds", 0)
        if not result:
            raise ValidationError("没有可更新的字段")
        return result

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        choice(p, "patient_priority", ["critical", "urgent", "stable"])
        number(p, "distance_km", 0)
        integer(p, "eta_minutes", 1, 240)
        choice(p, "required_capability", ["BLS", "ALS"])
        choice(p, "vehicle_capability", ["BLS", "ALS"])
        if p.get("hospital_beds") is not None:
            integer(p, "hospital_beds", 0)
        p["destination"] = optional_text(p, "destination")
        text(p, "location")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        weights = {"critical": 100, "urgent": 60, "stable": 25}
        p["priority_score"] = round(weights[p["patient_priority"]] + float(p["distance_km"]) - float(p["eta_minutes"]) * 0.5, 2)
        p["sla_minutes"] = {"critical": 8, "urgent": 20, "stable": 45}[p["patient_priority"]]
        p["capability_ok"] = p["required_capability"] == p["vehicle_capability"] or p["vehicle_capability"] == "ALS"
        return p

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
                raise ValidationError("车辆能力不满足病人所需能力")
            hospital = data.get("hospital")
            if not isinstance(hospital, dict) or not hospital.get("id") or not hospital.get("name"):
                raise ValidationError("未选择目的地医院")
            changes["assigned_vehicle_id"] = text(data, "vehicle_id")
            changes["assigned"] = True
            changes["hospital_id"] = int(hospital["id"])
            changes["hospital_name"] = str(hospital["name"])
            changes["hospital_drive_minutes"] = int(hospital["drive_minutes"])
            changes["bed_reserved"] = True
            reasons = data.get("reselect_reasons") or []
            if reasons:
                changes["reselect_reasons"] = reasons
            summary = "已派车并占床：%s（车程%s分钟）" % (hospital["name"], hospital["drive_minutes"])
            if reasons:
                summary += "；自动改选：" + "；".join("%s(%s)" % (item.get("hospital_name"), item.get("reason")) for item in reasons)
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
            if integer(data, "destination_beds", 0) <= 0:
                raise ValidationError("目的地无可用床位")
            changes["transporting"] = True
            summary = "开始转运"
        elif action == "handover":
            if not boolean(data, "handover_accepted"):
                raise ValidationError("医院尚未接收")
            changes["handover_accepted"] = True
            summary = "交接完成"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "任务取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
