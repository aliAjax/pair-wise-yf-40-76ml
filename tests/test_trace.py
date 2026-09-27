import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class TraceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.quarantine_officer = Actor("officer-1", "quarantine")

    def tearDown(self):
        self.tmp.cleanup()

    def _facility(self, name):
        return self.service.create(
            self.admin, "facility", {"name": name, "address": "County 1"}
        )

    def _consignment(self, code, source=None, facility=None):
        data = {"code": code, "origin": "Port-A", "destination": "Farm-B"}
        if source:
            data["source_batch_id"] = source
        if facility:
            data["receiving_facility_id"] = facility
        return self.service.create(self.admin, "consignment", data)

    def _quarantine(self, entity_id):
        self.service.transition(
            self.admin,
            entity_id,
            "inspect",
            {"inspector": "I-1", "inspection_result": "suspected"},
        )
        return self.service.transition(
            self.admin,
            entity_id,
            "quarantine",
            {"pest_found": True, "sample_id": "S-1"},
        )

    def _build_chain(self):
        # A -> B -> C, A -> D；A/B/C 分别流向三个设施
        f1 = self._facility("Greenhouse-1")
        f2 = self._facility("Nursery-2")
        f3 = self._facility("Greenhouse-3")
        a = self._consignment("C-A", facility=f1["id"])
        b = self._consignment("C-B", source=a["id"], facility=f2["id"])
        c = self._consignment("C-C", source=b["id"], facility=f3["id"])
        d = self._consignment("C-D", source=a["id"])
        return {"facilities": (f1, f2, f3), "a": a, "b": b, "c": c, "d": d}

    def _review_for(self, target_id):
        found = [
            item
            for item in self.service.list("review")
            if item["data"].get("target_id") == target_id
        ]
        self.assertEqual(len(found), 1, "expected exactly one review for " + target_id)
        return found[0]

    def test_quarantine_traces_downstream_and_flags_reviews(self):
        chain = self._build_chain()
        f1, f2, f3 = chain["facilities"]
        self._quarantine(chain["a"]["id"])

        for key in ("b", "c", "d"):
            flagged = self.service.get(chain[key]["id"])
            self.assertEqual(flagged["status"], "under_review")
            self.assertEqual(flagged["data"]["review_prior_status"], "declared")
        for facility in (f1, f2, f3):
            self.assertEqual(self.service.get(facility["id"])["status"], "under_review")

        reviews = self.service.list("review")
        self.assertEqual(len(reviews), 6)
        for review in reviews:
            self.assertEqual(review["status"], "pending")
            self.assertEqual(review["data"]["trace_id"], chain["a"]["id"])
        # 根批次已隔离，不为它自己建复核记录
        self.assertIsNone(
            next(
                (
                    item
                    for item in reviews
                    if item["data"].get("target_id") == chain["a"]["id"]
                ),
                None,
            )
        )

    def test_confirm_review_quarantines_batch_and_locks_facility(self):
        chain = self._build_chain()
        f1 = chain["facilities"][0]
        self._quarantine(chain["a"]["id"])

        review_b = self._review_for(chain["b"]["id"])
        self.service.transition(self.quarantine_officer, review_b["id"], "confirm")
        self.assertEqual(self.service.get(chain["b"]["id"])["status"], "quarantined")
        self.assertEqual(self.service.get(review_b["id"])["status"], "confirmed")

        review_f1 = self._review_for(f1["id"])
        self.service.transition(self.quarantine_officer, review_f1["id"], "confirm")
        self.assertEqual(self.service.get(f1["id"])["status"], "locked")

        # 确认 B 后再次追溯不产生重复复核记录（C 已有待复核记录）
        self.assertEqual(len(self.service.list("review")), 6)

    def test_exclude_review_restores_prior_status(self):
        chain = self._build_chain()
        f2 = chain["facilities"][1]
        self._quarantine(chain["a"]["id"])

        review_c = self._review_for(chain["c"]["id"])
        self.service.transition(self.quarantine_officer, review_c["id"], "exclude")
        restored = self.service.get(chain["c"]["id"])
        self.assertEqual(restored["status"], "declared")
        self.assertEqual(restored["data"]["review_decision"], "excluded")

        review_f2 = self._review_for(f2["id"])
        self.service.transition(self.quarantine_officer, review_f2["id"], "exclude")
        self.assertEqual(self.service.get(f2["id"])["status"], "registered")

    def test_decided_review_cannot_be_decided_again(self):
        chain = self._build_chain()
        self._quarantine(chain["a"]["id"])
        review_b = self._review_for(chain["b"]["id"])
        self.service.transition(self.quarantine_officer, review_b["id"], "confirm")
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.quarantine_officer, review_b["id"], "exclude")

    def test_consignment_registration_validates_references(self):
        with self.assertRaises(ValidationError):
            self._consignment("C-X", source="missing-batch")
        with self.assertRaises(ValidationError):
            self._consignment("C-Y", facility="missing-facility")
        a = self._consignment("C-A")
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "consignment",
                {
                    "id": a["id"],
                    "code": "C-Z",
                    "origin": "O",
                    "destination": "D",
                    "source_batch_id": a["id"],
                },
            )

    def test_review_requires_flaggable_target(self):
        a = self._consignment("C-A")
        self._quarantine(a["id"])
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "review",
                {"target_kind": "consignment", "target_id": a["id"]},
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin, "review", {"target_kind": "consignment", "target_id": "none"}
            )

    def test_trace_requires_quarantined_root(self):
        a = self._consignment("C-A")
        with self.assertRaises(InvalidTransition):
            self.service.trace_from(self.admin, a["id"])

    def test_trace_chain_reports_tree_and_progress(self):
        chain = self._build_chain()
        f1, f2, f3 = chain["facilities"]
        self._quarantine(chain["a"]["id"])
        self.service.transition(
            self.quarantine_officer, self._review_for(chain["b"]["id"])["id"], "confirm"
        )
        self.service.transition(
            self.quarantine_officer, self._review_for(chain["c"]["id"])["id"], "exclude"
        )

        result = self.service.trace_chain(chain["a"]["id"])
        self.assertEqual(result["root"], chain["a"]["id"])
        depths = {node["code"]: node["depth"] for node in result["nodes"]}
        self.assertEqual(depths, {"C-A": 0, "C-B": 1, "C-D": 1, "C-C": 2})
        statuses = {node["code"]: node["status"] for node in result["nodes"]}
        self.assertEqual(statuses["C-A"], "quarantined")
        self.assertEqual(statuses["C-B"], "quarantined")
        self.assertEqual(statuses["C-C"], "declared")
        self.assertEqual(statuses["C-D"], "under_review")
        self.assertEqual(
            {facility["id"] for facility in result["facilities"]},
            {f1["id"], f2["id"], f3["id"]},
        )
        self.assertEqual(
            result["progress"],
            {"pending": 4, "confirmed": 1, "excluded": 1, "total": 6},
        )


if __name__ == "__main__":
    unittest.main()
