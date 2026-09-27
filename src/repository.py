"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .domain import Conflict, NoDestination, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hospitals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    total_beds INTEGER NOT NULL,
                    available_beds INTEGER NOT NULL,
                    drive_minutes INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bed_reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    hospital_id INTEGER NOT NULL REFERENCES hospitals(id),
                    status TEXT NOT NULL,
                    reroute_reason TEXT,
                    created_at TEXT NOT NULL,
                    released_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_reservations_record ON bed_reservations(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_reservations_hospital ON bed_reservations(hospital_id, status);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _hospital_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["active"] = bool(item["active"])
        return item

    def create_order(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, selector: Callable[[List[Dict[str, Any]], str], Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]]) -> Dict[str, Any]:
        """接单并在同一事务内完成目的地分流与占床。

        selector返回(医院, 改选记录)；占床使用条件更新，最后一张床被并发任务
        抢走时自动改选下一家合格医院，没有任何合格医院则拒绝接单。
        """
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute("SELECT * FROM hospitals WHERE active=1").fetchall()
                hospitals = [self._hospital_row(row) for row in rows]
                reroutes: List[Dict[str, Any]] = []
                chosen: Optional[Dict[str, Any]] = None
                reserved = False
                for _ in range(len(hospitals) + 1):
                    chosen, found = selector(hospitals, payload["required_capability"])
                    known = {(item["hospital_id"], item["reason"]) for item in reroutes}
                    for item in found:
                        if (item["hospital_id"], item["reason"]) not in known:
                            reroutes.append(item)
                            known.add((item["hospital_id"], item["reason"]))
                    if chosen is None:
                        break
                    cursor = connection.execute(
                        "UPDATE hospitals SET available_beds=available_beds-1, updated_by=?, updated_at=? WHERE id=? AND available_beds>0",
                        (actor_id, now, chosen["id"]),
                    )
                    if cursor.rowcount == 1:
                        chosen["available_beds"] = int(chosen["available_beds"]) - 1
                        reserved = True
                        break
                    reroutes.append({"hospital_id": chosen["id"], "hospital_name": chosen["name"], "reason": "最后一张床被并发任务占用"})
                    for hospital in hospitals:
                        if hospital["id"] == chosen["id"]:
                            hospital["available_beds"] = 0
                if not reserved or chosen is None:
                    connection.rollback()
                    if reroutes:
                        raise NoDestination("能力匹配的医院床位已满，拒绝接单")
                    raise NoDestination("没有具备%s救治能力的医院，拒绝接单" % payload["required_capability"])
                reroute_reason = "；".join("%s:%s" % (item["hospital_name"], item["reason"]) for item in reroutes) or None
                payload = dict(payload)
                payload.update({
                    "destination": chosen["name"],
                    "destination_hospital_id": chosen["id"],
                    "destination_drive_minutes": chosen["drive_minutes"],
                    "bed_status": "held",
                    "reroute_reason": reroute_reason,
                })
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO bed_reservations(record_id,hospital_id,status,reroute_reason,created_at) VALUES(?,?,?,?,?)",
                    (record_id, chosen["id"], "held", reroute_reason, now),
                )
                details = {
                    "state": state,
                    "destination": chosen["name"],
                    "destination_hospital_id": chosen["id"],
                    "destination_drive_minutes": chosen["drive_minutes"],
                    "bed_status": "held",
                    "reroute_reason": reroute_reason,
                }
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], bed_action: Optional[str] = None) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            if bed_action in {"release", "consume"}:
                reservation = connection.execute(
                    "SELECT * FROM bed_reservations WHERE record_id=? AND status='held' ORDER BY id DESC LIMIT 1",
                    (record_id,),
                ).fetchone()
                if reservation is not None:
                    new_status = "released" if bed_action == "release" else "consumed"
                    connection.execute(
                        "UPDATE bed_reservations SET status=?, released_at=? WHERE id=?",
                        (new_status, now, reservation["id"]),
                    )
                    if bed_action == "release":
                        connection.execute(
                            "UPDATE hospitals SET available_beds=MIN(available_beds+1, total_beds), updated_by=?, updated_at=? WHERE id=?",
                            (actor_id, now, reservation["hospital_id"]),
                        )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def create_hospital(self, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO hospitals(code,name,capability,total_beds,available_beds,drive_minutes,active,updated_by,updated_at) VALUES(?,?,?,?,?,?,1,?,?)",
                    (data["code"], data["name"], data["capability"], data["total_beds"], data["total_beds"], data["drive_minutes"], actor_id, now),
                )
                row = connection.execute("SELECT * FROM hospitals WHERE id=?", (int(cursor.lastrowid),)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("医院编码已存在") from exc
        return self._hospital_row(row)

    def get_hospital(self, hospital_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
        if row is None:
            raise NotFound("医院不存在")
        return self._hospital_row(row)

    def list_hospitals(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM hospitals ORDER BY drive_minutes, id").fetchall()
        return [self._hospital_row(row) for row in rows]

    def update_hospital(self, hospital_id: int, changes: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("医院不存在")
            current = self._hospital_row(row)
            merged = dict(current)
            merged.update(changes)
            if "total_beds" in changes and "available_beds" not in changes:
                merged["available_beds"] = max(0, min(int(current["available_beds"]), int(changes["total_beds"])))
            connection.execute(
                "UPDATE hospitals SET name=?,capability=?,total_beds=?,available_beds=?,drive_minutes=?,active=?,updated_by=?,updated_at=? WHERE id=?",
                (merged["name"], merged["capability"], int(merged["total_beds"]), int(merged["available_beds"]), int(merged["drive_minutes"]), 1 if merged["active"] else 0, actor_id, now, hospital_id),
            )
            result = connection.execute("SELECT * FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
            connection.commit()
        return self._hospital_row(result)

    def adjust_hospital_beds(self, hospital_id: int, delta: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("医院不存在")
            current = self._hospital_row(row)
            available = int(current["available_beds"]) + int(delta)
            if available < 0 or available > int(current["total_beds"]):
                connection.rollback()
                raise Conflict("床位调整超出范围")
            connection.execute(
                "UPDATE hospitals SET available_beds=?, updated_by=?, updated_at=? WHERE id=?",
                (available, actor_id, now, hospital_id),
            )
            result = connection.execute("SELECT * FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
            connection.commit()
        return self._hospital_row(result)

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
