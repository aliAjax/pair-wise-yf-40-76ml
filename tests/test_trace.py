import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class TraceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.quarantine = Actor("qua-1", "quarantine")
        self.facility = self.service.create(
            self.admin, "facility", {"name": "Greenhouse-1", "address": "County 1"}
        )
        # 传播链: A -> B -> C, A -> D；B 接收于 facility
        self.batch_a = self._batch("C-A")
        self.batch_b = self._batch("C-B", source=self.batch_a, facility=self.facility)
        self.batch_c = self._batch("C-C", source=self.batch_b)
        self.batch_d = self._batch("C-D", source=self.batch_a)

    def tearDown(self):
        self.tmp.cleanup()

    def _batch(self, code, source=None, facility=None):
        data = {"code": code, "origin": "Port-A", "destination": "Farm-" + code}
        if source:
            data["source_batch_id"] = source["id"]
        if facility:
            data["facility_id"] = facility["id"]
        return self.service.create(self.admin, "consignment", data)

    def _confirm_outbreak(self, batch):
        self.service.transition(
            self.admin, batch["id"], "inspect",
            {"inspector": "I-1", "inspection_result": "positive"},
        )
        return self.service.transition(
            self.quarantine, batch["id"], "quarantine",
            {"pest_found": True, "sample_id": "S-1"},
        )

    def test_register_with_source_and_facility(self):
        self.assertEqual(self.batch_b["data"]["source_batch_id"], self.batch_a["id"])
        self.assertEqual(self.batch_b["data"]["facility_id"], self.facility["id"])

    def test_unknown_source_or_facility_rejected(self):
        with self.assertRaises(ValidationError):
            self._batch("C-X", source={"id": "missing"})
        with self.assertRaises(ValidationError):
            self._batch("C-Y", facility={"id": "missing"})

    def test_outbreak_flags_downstream_batches_and_facilities(self):
        self._confirm_outbreak(self.batch_a)
        for batch in (self.batch_b, self.batch_c, self.batch_d):
            current = self.service.get(batch["id"])
            self.assertEqual(current["status"], "pending_review")
            self.assertEqual(current["data"]["review_result"], "pending")
            self.assertEqual(current["data"]["outbreak_source"], self.batch_a["id"])
            self.assertEqual(current["data"]["review_previous_status"], "declared")
        facility = self.service.get(self.facility["id"])
        self.assertEqual(facility["status"], "pending_review")
        self.assertEqual(facility["data"]["outbreak_source"], self.batch_a["id"])

    def test_terminal_batches_are_not_flagged(self):
        # D 已放行，属于终态，不应被标记为待复核
        self.service.transition(
            self.admin, self.batch_d["id"], "inspect",
            {"inspector": "I-1", "inspection_result": "clean"},
        )
        self.service.transition(
            self.quarantine, self.batch_d["id"], "release",
            {"pest_found": False, "treatment": "none"},
        )
        self._confirm_outbreak(self.batch_a)
        self.assertEqual(self.service.get(self.batch_d["id"])["status"], "released")

    def test_confirm_review_quarantines_batch(self):
        self._confirm_outbreak(self.batch_a)
        updated = self.service.transition(
            self.quarantine, self.batch_b["id"], "confirm_review",
            {"sample_id": "S-2"},
        )
        self.assertEqual(updated["status"], "quarantined")
        self.assertEqual(updated["data"]["review_result"], "confirmed")

    def test_clear_review_restores_previous_status(self):
        self._confirm_outbreak(self.batch_a)
        updated = self.service.transition(
            self.quarantine, self.batch_c["id"], "clear_review",
            {"reason": "lab test negative"},
        )
        self.assertEqual(updated["status"], "declared")
        self.assertEqual(updated["data"]["review_result"], "cleared")

    def test_clear_review_restores_facility(self):
        self._confirm_outbreak(self.batch_a)
        updated = self.service.transition(
            self.quarantine, self.facility["id"], "clear_review",
            {"reason": "no pest found on site"},
        )
        self.assertEqual(updated["status"], "registered")

    def test_review_requires_quarantine_role(self):
        self._confirm_outbreak(self.batch_a)
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"), self.batch_b["id"], "confirm_review",
                {"sample_id": "S-2"},
            )

    def test_trace_view_shows_chain_and_progress(self):
        self._confirm_outbreak(self.batch_a)
        self.service.transition(
            self.quarantine, self.batch_b["id"], "confirm_review", {"sample_id": "S-2"}
        )
        self.service.transition(
            self.quarantine, self.batch_c["id"], "clear_review", {"reason": "clean"}
        )
        view = self.service.trace_view(self.batch_a["id"])
        self.assertEqual([b["id"] for b in view["batches"]],
                         [self.batch_a["id"], self.batch_b["id"],
                          self.batch_d["id"], self.batch_c["id"]])
        depths = {b["id"]: b["depth"] for b in view["batches"]}
        self.assertEqual(depths[self.batch_c["id"]], 2)
        progress = view["progress"]["batches"]
        self.assertEqual(progress["total"], 4)
        self.assertEqual(progress["source"], 1)
        self.assertEqual(progress["confirmed"], 1)
        self.assertEqual(progress["cleared"], 1)
        self.assertEqual(progress["pending"], 1)
        self.assertEqual(len(view["facilities"]), 1)
        self.assertEqual(view["progress"]["facilities"]["pending"], 1)


if __name__ == "__main__":
    unittest.main()
