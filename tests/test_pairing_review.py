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
        self.vet = Actor("vet-1", "veterinarian")
        self.coordinator = Actor("coord-1", "coordinator")

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

    def _approved_pairing(self, sire, dam):
        pairing = self._pairing(sire, dam)
        return self.service.transition(
            self.coordinator, pairing["id"], "approve", {"approvals": ["coord-1"]}
        )

    def test_approve_records_parent_versions(self):
        sire = self._animal("M-1", "male")
        dam = self._animal("F-1", "female")
        self.service.transition(self.vet, sire["id"], "quarantine_animal", {"reason": "check"})
        self.service.transition(self.vet, sire["id"], "release_quarantine", {})
        approved = self._approved_pairing(sire, dam)
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["data"]["sire_version"], 3)
        self.assertEqual(approved["data"]["dam_version"], 1)
        self.assertEqual(approved["data"]["approved_by"], "coord-1")
        self.assertEqual(approved["data"]["inbreeding_coefficient"], 0.0)

    def test_approve_blocked_for_quarantined_parent(self):
        sire = self._animal("M-1", "male")
        dam = self._animal("F-1", "female")
        self.service.transition(self.vet, sire["id"], "quarantine_animal", {"reason": "flu"})
        pairing = self._pairing(sire, dam)
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.coordinator, pairing["id"], "approve", {"approvals": ["coord-1"]}
            )
        self.assertIn("隔离", str(ctx.exception))
        self.assertIn("M-1", str(ctx.exception))
        self.assertEqual(self.service.get(pairing["id"])["status"], "proposed")

    def test_approve_blocked_for_deceased_parent(self):
        sire = self._animal("M-1", "male")
        dam = self._animal("F-1", "female")
        self.service.transition(self.vet, dam["id"], "mark_deceased", {"cause": "age"})
        pairing = self._pairing(sire, dam)
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.coordinator, pairing["id"], "approve", {"approvals": ["coord-1"]}
            )
        self.assertIn("死亡", str(ctx.exception))
        self.assertIn("F-1", str(ctx.exception))

    def test_approve_blocked_for_close_relatives(self):
        sire = self._animal("M-1", "male")
        dam = self._animal("F-1", "female", sire_id=sire["id"])
        pairing = self._pairing(sire, dam)
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.coordinator, pairing["id"], "approve", {"approvals": ["coord-1"]}
            )
        self.assertIn("亲缘", str(ctx.exception))

    def test_quarantine_returns_pairing_for_review(self):
        sire = self._animal("M-1", "male")
        dam = self._animal("F-1", "female")
        approved = self._approved_pairing(sire, dam)
        self.service.transition(self.vet, sire["id"], "quarantine_animal", {"reason": "flu"})
        pairing = self.service.get(approved["id"])
        self.assertEqual(pairing["status"], "proposed")
        self.assertIn("隔离", pairing["data"]["review_reason"])
        self.assertIn("M-1", pairing["data"]["review_reason"])
        # 退回待复核期间不能再安排完成
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.coordinator, pairing["id"], "complete", {"offspring_ids": ["o-1"]}
            )
        actions = [entry["action"] for entry in self.service.audit_log(pairing["id"])]
        self.assertIn("auto_return", actions)
        # 解除隔离不会自动恢复，需要协调员重新批准
        self.service.transition(self.vet, sire["id"], "release_quarantine", {})
        self.assertEqual(self.service.get(pairing["id"])["status"], "proposed")
        reapproved = self.service.transition(
            self.coordinator, pairing["id"], "approve", {"approvals": ["coord-1"]}
        )
        self.assertEqual(reapproved["status"], "approved")
        self.assertEqual(reapproved["data"]["sire_version"], 3)
        self.assertEqual(reapproved["data"]["review_reason"], "")

    def test_deceased_parent_returns_pairing_for_review(self):
        sire = self._animal("M-1", "male")
        dam = self._animal("F-1", "female")
        approved = self._approved_pairing(sire, dam)
        self.service.transition(self.vet, dam["id"], "mark_deceased", {"cause": "illness"})
        pairing = self.service.get(approved["id"])
        self.assertEqual(pairing["status"], "proposed")
        self.assertIn("死亡", pairing["data"]["review_reason"])
        self.assertIn("F-1", pairing["data"]["review_reason"])
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.coordinator, pairing["id"], "complete", {"offspring_ids": ["o-1"]}
            )

    def test_complete_blocked_when_parent_unfit(self):
        sire = self._animal("M-1", "male")
        dam = self._animal("F-1", "female")
        approved = self._approved_pairing(sire, dam)
        unfit_sire = dict(sire)
        unfit_sire["status"] = "quarantined"
        animals = {sire["id"]: unfit_sire, dam["id"]: dam}

        def lookup(kind, field, value):
            animal = animals.get(value)
            return [animal] if animal else []

        with self.assertRaises(ValidationError) as ctx:
            self.service.rules.validate_transition(
                self.coordinator, approved, "complete", {"offspring_ids": ["o-1"]}, lookup
            )
        self.assertIn("隔离", str(ctx.exception))

    def test_pairing_create_rejects_unknown_parent(self):
        sire = self._animal("M-1", "male")
        with self.assertRaises(ValidationError):
            self.service.create(
                self.coordinator,
                "pairing",
                {"proposed_by": "coord-1", "sire_id": sire["id"], "dam_id": "missing"},
            )


if __name__ == "__main__":
    unittest.main()
