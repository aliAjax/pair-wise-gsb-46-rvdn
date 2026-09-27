import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, NoDestination, PermissionDenied


DISPATCHER = Actor("dispatcher-1", "dispatcher")
ADMIN = Actor("admin-1", "admin")
ALS_TASK = {'patient_priority': 'critical', 'distance_km': 7.5, 'eta_minutes': 9, 'required_capability': 'ALS', 'vehicle_capability': 'ALS', 'location': 'East Gate'}
BLS_TASK = {'patient_priority': 'stable', 'distance_km': 3.0, 'eta_minutes': 6, 'required_capability': 'BLS', 'vehicle_capability': 'BLS', 'location': 'West Gate'}


class DestinationRoutingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self._seq = 0

    def tearDown(self):
        self.temp.cleanup()

    def add_hospital(self, code, capability, beds, drive):
        return self.service.create_hospital(ADMIN, {'code': code, 'name': 'Hospital-%s' % code, 'capability': capability, 'total_beds': beds, 'drive_minutes': drive})

    def create_task(self, data=None):
        self._seq += 1
        return self.service.create(DISPATCHER, "EMG-%05d" % self._seq, data or ALS_TASK)

    def hospital(self, hospital_id):
        return self.service.repository.get_hospital(hospital_id)

    def test_capability_filter_then_fastest_within_window(self):
        bls_only = self.add_hospital("H-BLS", "BLS", 5, 3)
        als = self.add_hospital("H-ALS", "ALS", 2, 15)
        record = self.create_task(ALS_TASK)
        self.assertEqual(record["payload"]["destination_hospital_id"], als["id"])
        self.assertEqual(record["payload"]["destination"], "Hospital-H-ALS")
        self.assertEqual(record["payload"]["destination_drive_minutes"], 15)
        self.assertEqual(record["payload"]["bed_status"], "held")
        self.assertIsNone(record["payload"]["reroute_reason"])
        record = self.create_task(BLS_TASK)
        self.assertEqual(record["payload"]["destination_hospital_id"], bls_only["id"])

    def test_reject_when_no_capable_hospital(self):
        self.add_hospital("H-BLS", "BLS", 5, 3)
        with self.assertRaises(NoDestination):
            self.create_task(ALS_TASK)
        with self.assertRaises(NoDestination):
            self.create_task()  # 目录为空同样拒绝

    def test_reject_when_all_beyond_20_minutes(self):
        self.add_hospital("H-FAR", "ALS", 5, 25)
        with self.assertRaises(NoDestination):
            self.create_task()

    def test_reroute_when_fastest_full(self):
        full = self.add_hospital("H-FULL", "ALS", 1, 5)
        backup = self.add_hospital("H-BACK", "ALS", 2, 12)
        self.service.adjust_hospital_beds(ADMIN, full["id"], {"delta": -1})
        record = self.create_task()
        self.assertEqual(record["payload"]["destination_hospital_id"], backup["id"])
        self.assertIn("床位已满", record["payload"]["reroute_reason"])
        self.assertIn("Hospital-H-FULL", record["payload"]["reroute_reason"])
        timeline = self.service.timeline(DISPATCHER, record["id"])
        created = timeline[0]
        self.assertEqual(created["details"]["destination"], "Hospital-H-BACK")
        self.assertEqual(created["details"]["bed_status"], "held")
        self.assertIn("床位已满", created["details"]["reroute_reason"])

    def test_reject_when_capable_hospitals_full(self):
        full = self.add_hospital("H-FULL", "ALS", 1, 5)
        self.service.adjust_hospital_beds(ADMIN, full["id"], {"delta": -1})
        with self.assertRaises(NoDestination):
            self.create_task()
        self.assertEqual(self.service.stats(DISPATCHER), {})

    def test_cancel_releases_bed(self):
        hospital = self.add_hospital("H-ALS", "ALS", 1, 8)
        record = self.create_task()
        self.assertEqual(self.hospital(hospital["id"])["available_beds"], 0)
        record = self.service.act(DISPATCHER, record["id"], record["version"], "cancel", {"cancel_reason": "误报"})
        self.assertEqual(record["payload"]["bed_status"], "released")
        self.assertEqual(self.hospital(hospital["id"])["available_beds"], 1)
        timeline = self.service.timeline(DISPATCHER, record["id"])
        cancel_event = timeline[-1]
        self.assertEqual(cancel_event["action"], "cancel")
        self.assertEqual(cancel_event["details"]["bed_action"], "release")
        self.assertEqual(cancel_event["details"]["destination"], "Hospital-H-ALS")

    def test_handover_consumes_bed_without_release(self):
        hospital = self.add_hospital("H-ALS", "ALS", 1, 8)
        record = self.create_task()
        record = self.service.act(DISPATCHER, record["id"], record["version"], "assign", {"vehicle_available": True, "vehicle_id": "AMB-01"})
        record = self.service.act(Actor("medic", "paramedic"), record["id"], record["version"], "enroute", {"traffic_level": "low"})
        record = self.service.act(Actor("medic", "paramedic"), record["id"], record["version"], "arrive", {"on_scene": True})
        record = self.service.act(Actor("medic", "paramedic"), record["id"], record["version"], "transport", {})
        record = self.service.act(Actor("coord", "hospital_coordinator"), record["id"], record["version"], "handover", {"handover_accepted": True})
        self.assertEqual(record["state"], "closed")
        self.assertEqual(record["payload"]["bed_status"], "consumed")
        self.assertEqual(self.hospital(hospital["id"])["available_beds"], 0)
        timeline = self.service.timeline(DISPATCHER, record["id"])
        self.assertEqual(timeline[-1]["details"]["bed_action"], "consume")

    def test_concurrent_last_bed_single_winner_and_reroute(self):
        first = self.add_hospital("H-FIRST", "ALS", 1, 5)
        backup = self.add_hospital("H-BACK", "ALS", 1, 12)
        results, errors = [], []

        def submit(ref):
            try:
                results.append(self.service.create(DISPATCHER, ref, ALS_TASK))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=submit, args=("EMG-C%d" % i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        destinations = {record["payload"]["destination_hospital_id"] for record in results}
        self.assertEqual(destinations, {first["id"], backup["id"]})
        rerouted = [record for record in results if record["payload"]["destination_hospital_id"] == backup["id"]][0]
        self.assertIn("床位已满", rerouted["payload"]["reroute_reason"])
        self.assertEqual(self.hospital(first["id"])["available_beds"], 0)
        self.assertEqual(self.hospital(backup["id"])["available_beds"], 0)

    def test_concurrent_last_bed_loser_rejected_when_no_alternative(self):
        self.add_hospital("H-ONLY", "ALS", 1, 5)
        results, errors = [], []

        def submit(ref):
            try:
                results.append(self.service.create(DISPATCHER, ref, ALS_TASK))
            except NoDestination as exc:
                errors.append(exc)

        threads = [threading.Thread(target=submit, args=("EMG-D%d" % i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)

    def test_hospital_directory_maintenance(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_hospital(Actor("medic", "paramedic"), {'code': 'H-X', 'name': 'X', 'capability': 'BLS', 'total_beds': 1, 'drive_minutes': 5})
        hospital = self.add_hospital("H-ALS", "ALS", 3, 9)
        self.assertEqual(hospital["available_beds"], 3)
        updated = self.service.update_hospital(ADMIN, hospital["id"], {"drive_minutes": 14, "capability": "ALS"})
        self.assertEqual(updated["drive_minutes"], 14)
        adjusted = self.service.adjust_hospital_beds(ADMIN, hospital["id"], {"delta": -2})
        self.assertEqual(adjusted["available_beds"], 1)
        with self.assertRaises(Conflict):
            self.service.adjust_hospital_beds(ADMIN, hospital["id"], {"delta": 5})
        listed = self.service.list_hospitals(DISPATCHER)
        self.assertEqual(len(listed), 1)
        inactive = self.service.update_hospital(ADMIN, hospital["id"], {"active": False})
        self.assertFalse(inactive["active"])
        with self.assertRaises(NoDestination):
            self.create_task()


if __name__ == "__main__":
    unittest.main()
