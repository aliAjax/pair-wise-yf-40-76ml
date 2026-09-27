import unittest

from src.rules import trace_downstream, trace_downstream_tree
from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")

    def test_rule_calculation_or_validation(self):
        links = [
            {"id": "1", "parent_id": None},
            {"id": "2", "parent_id": "1"},
            {"id": "3", "parent_id": "2"},
        ]
        self.assertEqual(trace_downstream(links, "1"), ["1", "2", "3"])
        self.assertEqual(
            trace_downstream_tree(links, "1"),
            [
                {"id": "1", "depth": 0},
                {"id": "2", "depth": 1},
                {"id": "3", "depth": 2},
            ],
        )
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(self.admin, {"kind": "consignment", "status": "inspected", "data": {}}, "release", {"pest_found": True, "treatment": "completed"})


if __name__ == "__main__":
    unittest.main()
