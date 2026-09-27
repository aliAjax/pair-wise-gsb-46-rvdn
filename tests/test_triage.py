import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'patient_priority': 'critical', 'distance_km': 7.5, 'eta_minutes': 9, 'required_capability': 'ALS', 'vehicle_capability': 'ALS', 'location': 'East Gate'}


class TriageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.dispatcher = Actor("disp", "dispatcher")
        self.coordinator = Actor("coord", "hospital_coordinator")

    def tearDown(self):
        self.temp.cleanup()

    def _add_hospital(self, name, capabilities, beds, drive_minutes):
        return self.service.create_hospital(self.coordinator, {"name": name, "capabilities": capabilities, "total_beds": beds, "drive_minutes": drive_minutes})

    def _hospital(self, name):
        return {h["name"]: h for h in self.service.list_hospitals(self.dispatcher)}[name]

    def _task(self, reference, **overrides):
        data = dict(CREATE_DATA)
        data.update(overrides)
        return self.service.create(self.dispatcher, reference, data)

    def _assign(self, task, vehicle_id):
        return self.service.act(self.dispatcher, task["id"], task["version"], "assign", {"vehicle_available": True, "vehicle_id": vehicle_id})

    def test_assign_picks_fastest_capable_hospital_and_reserves_bed(self):
        self._add_hospital("BLS Near", ["BLS"], 5, 5)
        self._add_hospital("ALS Near", ["BLS", "ALS"], 2, 9)
        self._add_hospital("ALS Far", ["ALS"], 2, 18)
        task = self._assign(self._task("EMG-30001"), "AMB-01")
        self.assertEqual(task["payload"]["hospital_name"], "ALS Near")
        self.assertEqual(task["payload"]["hospital_drive_minutes"], 9)
        self.assertTrue(task["payload"]["bed_reserved"])
        self.assertEqual(self._hospital("ALS Near")["available_beds"], 1)
        timeline = self.service.timeline(self.dispatcher, task["id"])
        assign_event = [event for event in timeline if event["action"] == "assign"][0]
        self.assertEqual(assign_event["details"]["input"]["hospital"]["name"], "ALS Near")

    def test_full_hospital_triggers_reselect_with_reason(self):
        self._add_hospital("Full ALS", ["ALS"], 0, 6)
        self._add_hospital("Backup ALS", ["ALS"], 3, 14)
        task = self._assign(self._task("EMG-30002"), "AMB-01")
        self.assertEqual(task["payload"]["hospital_name"], "Backup ALS")
        reasons = task["payload"]["reselect_reasons"]
        self.assertEqual(reasons[0]["hospital_name"], "Full ALS")
        self.assertEqual(reasons[0]["reason"], "无可用床位")
        timeline = self.service.timeline(self.dispatcher, task["id"])
        assign_event = [event for event in timeline if event["action"] == "assign"][0]
        self.assertEqual(assign_event["details"]["input"]["reselect_reasons"][0]["hospital_name"], "Full ALS")
        self.assertIn("自动改选", assign_event["details"]["summary"])

    def test_assign_rejected_without_capable_hospital_in_window(self):
        self._add_hospital("BLS Only", ["BLS"], 5, 5)
        self._add_hospital("Too Far ALS", ["ALS"], 5, 25)
        task = self._task("EMG-30003")
        with self.assertRaises(Conflict):
            self._assign(task, "AMB-01")
        record = self.service.get_record(self.dispatcher, task["id"])
        self.assertEqual(record["state"], "received")
        timeline = self.service.timeline(self.dispatcher, task["id"])
        self.assertEqual(timeline[-1]["action"], "assign_rejected")

    def test_cancel_releases_reserved_bed(self):
        self._add_hospital("Solo", ["ALS"], 1, 10)
        task = self._assign(self._task("EMG-30004"), "AMB-01")
        self.assertEqual(self._hospital("Solo")["available_beds"], 0)
        task = self.service.act(self.dispatcher, task["id"], task["version"], "cancel", {"cancel_reason": "重复报警"})
        self.assertEqual(task["state"], "cancelled")
        self.assertEqual(self._hospital("Solo")["available_beds"], 1)
        timeline = self.service.timeline(self.dispatcher, task["id"])
        cancel_event = [event for event in timeline if event["action"] == "cancel"][0]
        self.assertEqual(cancel_event["details"]["released_bed"]["hospital_name"], "Solo")

    def test_last_bed_race_only_one_wins_and_loser_reselects(self):
        self._add_hospital("Near ALS", ["ALS"], 1, 8)
        self._add_hospital("Far ALS", ["ALS"], 3, 15)
        first = self._task("EMG-30005")
        second = self._task("EMG-30006")
        barrier = threading.Barrier(2)
        results = {}
        errors = {}

        def assign(key, task, vehicle_id):
            try:
                barrier.wait(timeout=10)
                results[key] = self._assign(task, vehicle_id)
            except Exception as exc:  # pragma: no cover - 断言会暴露
                errors[key] = exc

        threads = [
            threading.Thread(target=assign, args=("a", first, "AMB-01")),
            threading.Thread(target=assign, args=("b", second, "AMB-02")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(errors, {})
        by_hospital = {record["payload"]["hospital_name"]: record for record in results.values()}
        self.assertEqual(set(by_hospital), {"Near ALS", "Far ALS"})
        loser = by_hospital["Far ALS"]
        self.assertTrue(loser["payload"]["reselect_reasons"])
        self.assertEqual(self._hospital("Near ALS")["available_beds"], 0)
        self.assertEqual(self._hospital("Far ALS")["available_beds"], 2)

    def test_last_bed_loser_rejected_without_alternative(self):
        self._add_hospital("Only ALS", ["ALS"], 1, 8)
        first = self._task("EMG-30007")
        second = self._task("EMG-30008")
        self._assign(first, "AMB-01")
        with self.assertRaises(Conflict):
            self._assign(second, "AMB-02")
        record = self.service.get_record(self.dispatcher, second["id"])
        self.assertEqual(record["state"], "received")

    def test_hospital_directory_maintenance(self):
        hospital = self._add_hospital("General", ["BLS"], 2, 11)
        updated = self.service.update_hospital(self.coordinator, hospital["id"], {"available_beds": 1, "drive_minutes": 9, "capabilities": ["BLS", "ALS"]})
        self.assertEqual(updated["available_beds"], 1)
        self.assertEqual(updated["drive_minutes"], 9)
        with self.assertRaises(ValidationError):
            self.service.update_hospital(self.coordinator, hospital["id"], {"available_beds": 5})
        with self.assertRaises(PermissionDenied):
            self.service.create_hospital(Actor("medic", "paramedic"), {"name": "X", "capabilities": ["BLS"], "total_beds": 1, "drive_minutes": 5})
        with self.assertRaises(Conflict):
            self._add_hospital("General", ["BLS"], 1, 5)
