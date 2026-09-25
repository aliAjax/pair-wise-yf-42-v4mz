import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class PairingReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.coordinator = Actor("coord-1", "coordinator")
        self.vet = Actor("vet-1", "veterinarian")

    def tearDown(self):
        self.tmp.cleanup()

    def _animal(self, name, sex, **extra):
        data = {"name": name, "sex": sex}
        data.update(extra)
        return self.service.create(self.admin, "animal", data)

    def _pairing(self, sire, dam):
        return self.service.create(
            self.coordinator,
            "pairing",
            {"proposed_by": "coord-1", "sire_id": sire["id"], "dam_id": dam["id"]},
        )

    def _review_item(self, pairing_id):
        for item in self.service.pairing_review():
            if item["id"] == pairing_id:
                return item
        self.fail("pairing not in review list: " + pairing_id)

    def test_review_shows_parents_health_and_risk(self):
        sire = self._animal("甲", "male")
        dam = self._animal("乙", "female")
        pairing = self._pairing(sire, dam)
        item = self._review_item(pairing["id"])
        self.assertEqual(item["sire"]["name"], "甲")
        self.assertEqual(item["dam"]["name"], "乙")
        self.assertEqual(item["sire"]["status"], "active")
        self.assertEqual(item["risk_level"], "low")
        self.assertEqual(item["blockers"], [])
        self.assertTrue(item["approvable"])

    def test_quarantined_parent_blocks_approval_with_reason(self):
        sire = self._animal("甲", "male")
        dam = self._animal("乙", "female")
        pairing = self._pairing(sire, dam)
        self.service.transition(self.vet, dam["id"], "quarantine_animal", {"reason": "疫病观察"})
        item = self._review_item(pairing["id"])
        self.assertFalse(item["approvable"])
        self.assertTrue(any("隔离" in reason for reason in item["blockers"]))
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.coordinator, pairing["id"], "approve", {"approvals": ["vet-1"]}
            )
        self.assertIn("隔离", str(ctx.exception))

    def test_deceased_parent_blocks_approval_with_reason(self):
        sire = self._animal("甲", "male")
        dam = self._animal("乙", "female")
        pairing = self._pairing(sire, dam)
        self.service.transition(self.vet, sire["id"], "mark_deceased", {"cause": "年老"})
        item = self._review_item(pairing["id"])
        self.assertFalse(item["approvable"])
        self.assertTrue(any("已死亡" in reason for reason in item["blockers"]))
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.coordinator, pairing["id"], "approve", {"approvals": ["vet-1"]}
            )
        self.assertIn("已死亡", str(ctx.exception))

    def test_high_inbreeding_risk_blocks_approval(self):
        sire = self._animal("父", "male")
        dam = self._animal("女", "female", sire_id=sire["id"])
        pairing = self._pairing(sire, dam)
        item = self._review_item(pairing["id"])
        self.assertEqual(item["risk_level"], "high")
        self.assertFalse(item["approvable"])
        self.assertTrue(any("亲缘风险过高" in reason for reason in item["blockers"]))
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.coordinator, pairing["id"], "approve", {"approvals": ["vet-1"]}
            )
        self.assertIn("亲缘风险过高", str(ctx.exception))

    def test_approval_records_parent_versions(self):
        sire = self._animal("甲", "male")
        dam = self._animal("乙", "female")
        pairing = self._pairing(sire, dam)
        approved = self.service.transition(
            self.coordinator, pairing["id"], "approve", {"approvals": ["vet-1"]}
        )
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["data"]["sire_version"], sire["version"])
        self.assertEqual(approved["data"]["dam_version"], dam["version"])
        self.assertEqual(approved["data"]["approved_by"], "coord-1")

    def test_parent_quarantine_returns_approved_pairing_to_review(self):
        sire = self._animal("甲", "male")
        dam = self._animal("乙", "female")
        pairing = self._pairing(sire, dam)
        self.service.transition(
            self.coordinator, pairing["id"], "approve", {"approvals": ["vet-1"]}
        )
        self.service.transition(self.vet, sire["id"], "quarantine_animal", {"reason": "外伤"})
        current = self.service.get(pairing["id"])
        self.assertEqual(current["status"], "proposed")
        self.assertIn("隔离", current["data"]["review_reason"])
        actions = [entry["action"] for entry in self.service.audit_log(pairing["id"])]
        self.assertIn("return_to_review", actions)
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.coordinator, pairing["id"], "complete", {"offspring_ids": ["o-1"]}
            )

    def test_parent_death_returns_approved_pairing_to_review(self):
        sire = self._animal("甲", "male")
        dam = self._animal("乙", "female")
        pairing = self._pairing(sire, dam)
        self.service.transition(
            self.coordinator, pairing["id"], "approve", {"approvals": ["vet-1"]}
        )
        self.service.transition(self.vet, dam["id"], "mark_deceased", {"cause": "疾病"})
        current = self.service.get(pairing["id"])
        self.assertEqual(current["status"], "proposed")
        self.assertIn("已死亡", current["data"]["review_reason"])
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.coordinator, pairing["id"], "complete", {"offspring_ids": ["o-1"]}
            )

    def test_complete_blocked_when_parent_not_active(self):
        sire = self._animal("甲", "male")
        dam = self._animal("乙", "female")
        pairing = self._pairing(sire, dam)
        self.service.transition(
            self.coordinator, pairing["id"], "approve", {"approvals": ["vet-1"]}
        )
        animal = self.repo.get_entity(sire["id"])
        self.repo.update_entity(sire["id"], None, "quarantined", animal["data"])
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.coordinator, pairing["id"], "complete", {"offspring_ids": ["o-1"]}
            )
        self.assertIn("隔离", str(ctx.exception))

    def test_reapproval_after_quarantine_release_records_new_versions(self):
        sire = self._animal("甲", "male")
        dam = self._animal("乙", "female")
        pairing = self._pairing(sire, dam)
        self.service.transition(
            self.coordinator, pairing["id"], "approve", {"approvals": ["vet-1"]}
        )
        self.service.transition(self.vet, sire["id"], "quarantine_animal", {"reason": "观察"})
        self.assertEqual(self.service.get(pairing["id"])["status"], "proposed")
        self.service.transition(self.vet, sire["id"], "release_quarantine", {})
        self.assertEqual(self.service.get(pairing["id"])["status"], "proposed")
        sire_now = self.service.get(sire["id"])
        reapproved = self.service.transition(
            self.coordinator, pairing["id"], "approve", {"approvals": ["vet-1"]}
        )
        self.assertEqual(reapproved["status"], "approved")
        self.assertEqual(reapproved["data"]["sire_version"], sire_now["version"])
        self.assertIsNone(reapproved["data"]["review_reason"])


if __name__ == "__main__":
    unittest.main()
