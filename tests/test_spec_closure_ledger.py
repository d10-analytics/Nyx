"""Tests of the specification closure-ledger rules."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from spec_fixtures import (  # noqa: F401
    CLEAN_LEDGER,
    VALID_SPEC,
    VALID_SPEC_V3,
    VALIDATOR,
    with_ledger,
)


class ClosureLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "AGENTS.md").write_text("Run pytest.\n", encoding="utf-8")
        self.base = (
            VALID_SPEC_V3.split("## Closure review")[0]
            .replace("Closure ledger: 1\n", "")
            .replace("/tmp/repo", str(self.root))
        )

    def check(self, ledger: dict, *, approved: bool = True) -> list[str]:
        path = self.root / "spec.md"
        text = with_ledger(self.base, ledger)
        if not approved:
            text = text.replace("Closure: approved", "Closure: pending")
        path.write_text(text, encoding="utf-8")
        return VALIDATOR.validate_package(path)

    def repaired(self) -> dict:
        ledger = copy.deepcopy(CLEAN_LEDGER)
        ledger["attempts"][0].update(result="FAIL", findings=["CL-01"])
        repair = copy.deepcopy(ledger["attempts"][0])
        repair.update(
            id=2,
            mode="REPAIR_VERIFICATION",
            round=1,
            spec_identity="sha256:repaired-input",
            reviewer="closer-2",
            result="PASS",
        )
        ledger["attempts"].append(repair)
        ledger["findings"].append(
            {
                "id": "CL-01",
                "origin_attempt": 1,
                "material": True,
                "disposition": "ACCEPT",
                "rationale": "Unchanged consumer bypasses required owner.",
                "status": "RESOLVED",
                "verification_attempt": 2,
                "evidence": "Caller X reaches fallback Y without admission; D-01 violated.",
                "repair_evidence": "Before/after owner and caller excerpts at reviewed identities.",
                "verification_evidence": "Independent closer replayed counterexample; natural-trigger assertion excludes bypass.",
            }
        )
        return ledger

    def test_clean_broad_review_and_empty_pending_draft(self) -> None:
        self.assertEqual([], self.check(CLEAN_LEDGER))
        empty = {"attempts": [], "findings": [], "resumptions": []}
        self.assertEqual([], self.check(empty, approved=False))
        self.assertTrue(any("broad review" in e for e in self.check(empty)))

    def test_accepted_repair_is_verified_by_later_independent_review(self) -> None:
        self.assertEqual([], self.check(self.repaired()))
        for field, value, message in (
            ("verification_attempt", None, "independent verification"),
            ("verification_attempt", 1, "independent verification"),
            ("verification_attempt", 99, "undefined verification"),
            ("repair_evidence", "", "repair evidence"),
            ("verification_evidence", "", "independent verification"),
            ("status", "OPEN", "unresolved finding"),
            ("status", "CLOSED", "RESOLVED"),
        ):
            with self.subTest(field=field, value=value):
                ledger = self.repaired()
                ledger["findings"][0][field] = value
                self.assertTrue(any(message in e for e in self.check(ledger)))

    def test_rejected_and_nonmaterial_deferred_broad_findings_can_close(self) -> None:
        for disposition in ("REJECT", "DEFER"):
            ledger = self.repaired()
            ledger["attempts"].pop()
            ledger["findings"][0].update(
                disposition=disposition,
                material=False,
                status="CLOSED",
                verification_attempt=None,
            )
            self.assertEqual([], self.check(ledger))
            ledger["findings"][0]["rationale"] = ""
            self.assertTrue(any("rationale" in e for e in self.check(ledger)))

    def test_partial_verification_survives_other_findings(self) -> None:
        ledger = self.repaired()
        ledger["attempts"][1]["result"] = "FAIL"
        # The accepted finding was independently resolved; an incidental preference was rejected.
        ledger["attempts"][1]["findings"].append("CL-02")
        other = copy.deepcopy(ledger["findings"][0])
        other.update(
            id="CL-02",
            origin_attempt=2,
            material=False,
            disposition="REJECT",
            status="CLOSED",
            verification_attempt=None,
        )
        ledger["findings"].append(other)
        self.assertEqual([], self.check(ledger))

    def test_material_deferral_and_pending_decision_cannot_close(self) -> None:
        for disposition in ("DEFER", "NEEDS_USER_DECISION"):
            ledger = self.repaired()
            ledger["findings"][0].update(disposition=disposition, status="CLOSED")
            self.assertTrue(self.check(ledger))
        ledger["findings"][0]["status"] = "OPEN"
        self.assertEqual([], self.check(ledger, approved=False))
        self.assertTrue(any("unresolved" in e for e in self.check(ledger)))

    def test_ids_references_and_review_identities(self) -> None:
        changes = (
            ("id", 1, "consecutive"),
            ("round", 0, "consecutive"),
            ("reviewer", "planner-1", "independent"),
            ("reviewer", "closer-1", "independent"),
            ("reviewer", "", "reviewer identity"),
            ("baseline", "", "missing baseline"),
            ("spec_identity", "", "missing spec_identity"),
            ("findings", ["CL-99"], "undefined finding"),
            ("findings", ["CL-01", "CL-01"], "duplicate"),
            ("result", "UNKNOWN", "invalid result"),
            ("mode", "anything", "invalid mode"),
        )
        for field, value, expected in changes:
            with self.subTest(field=field, value=value):
                ledger = self.repaired()
                ledger["attempts"][1][field] = value
                self.assertTrue(any(expected in e for e in self.check(ledger)))
        ledger = self.repaired()
        ledger["findings"].append(copy.deepcopy(ledger["findings"][0]))
        self.assertTrue(
            any("duplicate closure finding" in e for e in self.check(ledger))
        )

    def test_second_round_reservation_can_resume_but_cannot_reset_budget(self) -> None:
        ledger = self.repaired()
        second = copy.deepcopy(ledger["attempts"][1])
        second.update(id=3, round=2, reviewer="", result="INTERRUPTED")
        ledger["attempts"].append(second)
        self.assertEqual([], self.check(ledger, approved=False))
        self.assertTrue(any("terminal" in e for e in self.check(ledger)))
        # Same reservation, recovered evidence, fresh reviewer: no third round consumed.
        second.update(reviewer="closer-3", result="PASS")
        self.assertEqual([], self.check(ledger))
        third = copy.deepcopy(second)
        third.update(id=4, round=3, reviewer="closer-4")
        ledger["attempts"].append(third)
        self.assertTrue(any("budget" in e for e in self.check(ledger)))
        ledger["resumptions"].append(
            {
                "before_attempt": 4,
                "repair_limit": 3,
                "allow_broad_review": False,
                "authority": "User approved one further round at message 42.",
                "diagnosis": "Repair omitted sibling; enumerate sibling class before verification.",
            }
        )
        self.assertEqual([], self.check(ledger))
        ledger["resumptions"][0]["authority"] = ""
        self.assertTrue(any("authority" in e for e in self.check(ledger)))

    def test_unfinished_attempt_cannot_be_silently_replaced(self) -> None:
        ledger = self.repaired()
        ledger["attempts"][1]["result"] = "INTERRUPTED"
        later = copy.deepcopy(ledger["attempts"][1])
        later.update(id=3, round=2, result="PASS", reviewer="closer-3")
        ledger["attempts"].append(later)
        ledger["findings"][0]["verification_attempt"] = 3
        self.assertTrue(any("unfinished predecessor" in e for e in self.check(ledger)))
        ledger["resumptions"].append(
            {
                "before_attempt": 3,
                "repair_limit": 2,
                "allow_broad_review": False,
                "authority": "User authorized continuation from recovered findings.",
                "diagnosis": "Lost repair handoff; recovered scope and findings reviewed with user.",
            }
        )
        self.assertEqual([], self.check(ledger))

    def test_permission_to_resume_does_not_replace_completed_broad_discovery(
        self,
    ) -> None:
        ledger = self.repaired()
        ledger["attempts"][0]["result"] = "INTERRUPTED"
        ledger["resumptions"].append(
            {
                "before_attempt": 2,
                "repair_limit": 2,
                "allow_broad_review": False,
                "authority": "User permitted repairing recovered findings.",
                "diagnosis": "Discovery handoff was incomplete.",
            }
        )
        self.assertTrue(any("terminal broad review" in e for e in self.check(ledger)))

    def test_duplicate_metadata_and_duplicate_json_block_are_rejected(self) -> None:
        text = with_ledger(self.base, CLEAN_LEDGER)
        for malformed in (
            text.replace("Closure ledger: 1", "Closure ledger: 1\nClosure ledger: 1"),
            text.replace("Closure: approved", "Closure: pending\nClosure: approved"),
            text + "\n```json\n{}\n```\n",
        ):
            self.assertTrue(VALIDATOR._validate_closure_ledger(malformed.splitlines()))

    def test_new_broad_review_requires_explicit_authority(self) -> None:
        ledger = copy.deepcopy(CLEAN_LEDGER)
        later = copy.deepcopy(ledger["attempts"][0])
        later.update(id=2, reviewer="closer-2")
        ledger["attempts"].append(later)
        self.assertTrue(any("repeated broad" in e for e in self.check(ledger)))
        ledger["resumptions"].append(
            {
                "before_attempt": 2,
                "repair_limit": 2,
                "allow_broad_review": True,
                "authority": "User authorized broad review of changed design.",
                "diagnosis": "New owner changes previous design assumptions.",
            }
        )
        self.assertEqual([], self.check(ledger))

    def test_malformed_data_reports_errors_instead_of_crashing(self) -> None:
        for value in (None, [], {"attempts": None, "findings": [], "resumptions": []}):
            self.assertTrue(self.check(value))
        for field, value in (("id", True), ("findings", [None]), ("result", 5)):
            ledger = copy.deepcopy(CLEAN_LEDGER)
            ledger["attempts"][0][field] = value
            self.assertTrue(self.check(ledger))
        self.assertTrue(
            VALIDATOR._validate_closure_ledger(
                [
                    "Closure ledger: 1",
                    "## Closure review",
                    "```json",
                    '{"attempts": [], "attempts": []}',
                    "```",
                ]
            )
        )

    def test_approved_and_queued_packages_require_migration_but_history_is_readable(
        self,
    ) -> None:
        for lifecycle, content, valid in (
            ("under_development", self.base, False),
            ("queue", self.base, False),
            ("queue", VALID_SPEC, False),
            ("done", self.base, True),
            ("under_development", VALID_SPEC, True),
        ):
            with self.subTest(lifecycle=lifecycle, legacy="contract: 2" in content):
                p = self.root / lifecycle / "feature" / "spec.md"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                errors = VALIDATOR.validate_package(p)
                self.assertEqual(
                    not valid, any("Closure ledger" in e for e in errors), errors
                )
        text = with_ledger(self.base, CLEAN_LEDGER).replace(
            "Closure ledger: 1", "Closure ledger: 9"
        )
        self.assertTrue(
            any(
                "unsupported Closure ledger" in e
                for e in VALIDATOR._validate_closure_ledger(text.splitlines())
            )
        )
