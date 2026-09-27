import unittest

from src.domain import Actor, ValidationError
from src.rules import DomainRules, select_destination


CREATE_DATA = {'patient_priority': 'critical', 'distance_km': 7.5, 'eta_minutes': 9, 'required_capability': 'ALS', 'vehicle_capability': 'ALS', 'location': 'East Gate'}
FLOW = [('assign', 'dispatcher', {'vehicle_available': True, 'vehicle_id': 'AMB-07'}, 'assigned'), ('enroute', 'paramedic', {'traffic_level': 'medium'}, 'enroute'), ('arrive', 'paramedic', {'on_scene': True}, 'onscene'), ('transport', 'paramedic', {}, 'transporting'), ('handover', 'hospital_coordinator', {'handover_accepted': True}, 'closed')]


def hospital(hid, capability, drive, beds, name=None):
    return {'id': hid, 'code': 'H%s' % hid, 'name': name or 'Hospital-%s' % hid, 'capability': capability, 'drive_minutes': drive, 'available_beds': beds, 'total_beds': max(beds, 1), 'active': True}


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
        record = {"id": 1, "state": self.rules.INITIAL_STATE, "payload": self.rules.prepare_create(CREATE_DATA)}
        state, payload, summary = self.rules.apply_action(record, action, data)
        self.assertEqual(state, expected_state)
        self.assertEqual(payload["assigned_vehicle_id"], "AMB-07")

    def test_invalid_input(self):
        invalid = dict(CREATE_DATA)
        invalid["patient_priority"] = 'unknown'
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(invalid)

    def test_select_destination_capability_first(self):
        hospitals = [hospital(1, "BLS", 3, 5), hospital(2, "ALS", 15, 1)]
        chosen, reroutes = select_destination(hospitals, "ALS")
        self.assertEqual(chosen["id"], 2)
        self.assertEqual(reroutes, [])
        chosen, reroutes = select_destination(hospitals, "BLS")
        self.assertEqual(chosen["id"], 1)

    def test_select_destination_window_and_reroute(self):
        hospitals = [hospital(1, "ALS", 5, 0), hospital(2, "ALS", 18, 2), hospital(3, "ALS", 30, 9)]
        chosen, reroutes = select_destination(hospitals, "ALS")
        self.assertEqual(chosen["id"], 2)
        self.assertEqual(reroutes, [{"hospital_id": 1, "hospital_name": "Hospital-1", "reason": "床位已满"}])

    def test_select_destination_none_when_no_capability_or_beds(self):
        chosen, reroutes = select_destination([hospital(1, "BLS", 3, 5)], "ALS")
        self.assertIsNone(chosen)
        self.assertEqual(reroutes, [])
        chosen, reroutes = select_destination([hospital(1, "ALS", 3, 0)], "ALS")
        self.assertIsNone(chosen)
        self.assertEqual(len(reroutes), 1)
        chosen, reroutes = select_destination([hospital(1, "ALS", 25, 3)], "ALS")
        self.assertIsNone(chosen)
        self.assertEqual(reroutes, [])
