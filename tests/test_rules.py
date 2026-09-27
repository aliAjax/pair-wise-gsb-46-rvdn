import unittest

from src.domain import Actor, ValidationError
from src.rules import DomainRules


CREATE_DATA = {'patient_priority': 'critical', 'distance_km': 7.5, 'eta_minutes': 9, 'required_capability': 'ALS', 'vehicle_capability': 'ALS', 'hospital_beds': 4, 'destination': 'City Hospital', 'location': 'East Gate'}
FLOW = [('assign', 'dispatcher', {'vehicle_available': True, 'vehicle_id': 'AMB-07'}, 'assigned'), ('enroute', 'paramedic', {'traffic_level': 'medium'}, 'enroute'), ('arrive', 'paramedic', {'on_scene': True}, 'onscene'), ('transport', 'paramedic', {'destination_beds': 2}, 'transporting'), ('handover', 'hospital_coordinator', {'handover_accepted': True}, 'closed')]


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_prepare_create(self):
        prepared = self.rules.prepare_create(CREATE_DATA)
        self.assertEqual(prepared["sla_minutes"], 8)
        self.assertTrue(prepared["capability_ok"])
        self.assertGreater(prepared["priority_score"], 100)

    def test_action_calculation(self):
        action, role, data, expected_state = FLOW[0]
        data = dict(data)
        data["hospital"] = {"id": 1, "name": "City Hospital", "drive_minutes": 12}
        record = {"id": 1, "state": self.rules.INITIAL_STATE, "payload": self.rules.prepare_create(CREATE_DATA)}
        state, payload, summary = self.rules.apply_action(record, action, data)
        self.assertEqual(state, expected_state)
        self.assertEqual(payload["assigned_vehicle_id"], "AMB-07")
        self.assertEqual(payload["hospital_name"], "City Hospital")
        self.assertTrue(payload["bed_reserved"])

    def test_invalid_input(self):
        invalid = dict(CREATE_DATA)
        invalid["patient_priority"] = 'unknown'
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(invalid)

    def test_hospital_ranking_filters_capability_and_drive_window(self):
        hospitals = [
            {"id": 1, "name": "BLS点", "capabilities": ["BLS"], "available_beds": 3, "drive_minutes": 5},
            {"id": 2, "name": "ALS近", "capabilities": ["ALS"], "available_beds": 0, "drive_minutes": 10},
            {"id": 3, "name": "ALS远", "capabilities": ["BLS", "ALS"], "available_beds": 2, "drive_minutes": 15},
            {"id": 4, "name": "ALS超窗", "capabilities": ["ALS"], "available_beds": 2, "drive_minutes": 25},
        ]
        ranked = self.rules.rank_hospitals(hospitals, "ALS", 20)
        self.assertEqual([h["id"] for h in ranked], [2, 3])
        ranked_bls = self.rules.rank_hospitals(hospitals, "BLS", 20)
        self.assertEqual([h["id"] for h in ranked_bls], [1, 2, 3])

    def test_validate_hospital(self):
        hospital = self.rules.validate_hospital({"name": "General", "capabilities": ["BLS", "ALS"], "total_beds": 3, "drive_minutes": 9})
        self.assertEqual(hospital["available_beds"], 3)
        with self.assertRaises(ValidationError):
            self.rules.validate_hospital({"name": "Bad", "capabilities": ["ICU"], "total_beds": 1, "drive_minutes": 5})
        with self.assertRaises(ValidationError):
            self.rules.validate_hospital({"name": "Bad", "capabilities": ["BLS"], "total_beds": 1, "available_beds": 2, "drive_minutes": 5})
