"""Tests of the specification package and storage rules."""

from __future__ import annotations

import re
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

VALID_MERGE_SUMMARY = """## Title

Blend overlapping tiles instead of overwriting them

## Note

Tile placement previously kept the last writer in every overlap region, so
each overlap showed a hard seam. The renderer now accumulates weighted
contributions and normalizes once at the end.

Key changes:

- A weight-map module provides average and linear generators.
- Images blend in an in-memory accumulator and are cast once.

Not changed: alignment, coordinates, file schema, and the interface.
"""


class SpecificationStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.spec_root = self.root / "Shared drive with spaces" / "Specification_Library"
        self.workspace = self.root / "Code"
        self.repo = self.workspace / "Aurora-parallel"
        self.repo.mkdir(parents=True)
        (self.repo / "AGENTS.md").write_text("code instructions\n", encoding="utf-8")

    def package(self, stage: str, owner: str = "Aurora") -> Path:
        package = self.spec_root / owner / stage / "group" / "feature"
        package.mkdir(parents=True, exist_ok=True)
        (package / "spec.md").write_text(
            VALID_SPEC_V3.replace("Target repo: /tmp/repo", f"Target repo: {self.repo}"),
            encoding="utf-8",
        )
        return package

    def reports(self, package: Path, status: str | None = None) -> None:
        (package / "review_report.md").write_text(
            "Audit PASS with recorded evidence.\n", encoding="utf-8"
        )
        (package / "merge_summary.md").write_text(VALID_MERGE_SUMMARY, encoding="utf-8")
        if status:
            (package / "retrospective.md").write_text(
                "# Retrospective\n\nRetrospective contract: current\n"
                f"Status: {status}\n\n## Final status and exact-HEAD evidence\n\n"
                "Recorded terminal evidence at the reviewed HEAD.\n",
                encoding="utf-8",
            )

    def check(self, package: Path) -> list[str]:
        return VALIDATOR.validate_package(
            package,
            self.spec_root,
            non_package_folders=frozenset({"Blueprint_Library", "Field_Notes"}),
        )

    def test_nested_queue_requires_human_approval_and_closure(self) -> None:
        for owner in ("Aurora", "Quartz"):
            package = self.package("Queue", owner)
            self.assertEqual([], self.check(package))
            spec = package / "spec.md"
            spec.write_text(
                spec.read_text(encoding="utf-8")
                .replace("Human Sanity Decision: AFFIRMED", "Human Sanity Decision: PENDING")
                .replace("Closure: approved", "Closure: pending"),
                encoding="utf-8",
            )
            errors = self.check(package)
            self.assertIn("queue package requires Closure: approved", errors)
            self.assertIn("queue package requires an affirmative human sanity decision", errors)

    def test_in_progress_requires_approval_for_directory_and_file_intake(self) -> None:
        package = self.package("In_Progress")
        spec = package / "spec.md"
        for intake in (package, spec):
            self.assertEqual([], self.check(intake))
            self.assertEqual("in_progress", VALIDATOR.package_lifecycle(spec, self.spec_root))
        original = spec.read_text(encoding="utf-8")
        for old, new, expected in (
            (
                "Closure: approved",
                "Closure: pending",
                "in_progress package requires Closure: approved",
            ),
            (
                "Human Sanity Decision: AFFIRMED",
                "Human Sanity Decision: PENDING",
                "in_progress package requires an affirmative human sanity decision",
            ),
        ):
            spec.write_text(original.replace(old, new), encoding="utf-8")
            for intake in (package, spec):
                self.assertIn(expected, self.check(intake))

    def test_in_progress_legacy_contract_cannot_bypass_closure_ledger(self) -> None:
        package = self.package("In_Progress")
        (package / "spec.md").write_text(VALID_SPEC, encoding="utf-8")
        self.assertTrue(any("Closure ledger" in e for e in self.check(package)))

    def test_in_progress_can_resume_without_completion_reports(self) -> None:
        package = self.package("In_Progress")
        execution = self.spec_root / "Aurora" / ".pipeline" / "group" / "feature"
        execution.mkdir(parents=True)
        (execution / "run.md").write_text(
            "Interrupted; preserve recorded repair history.", encoding="utf-8"
        )
        self.assertEqual([], self.check(package))
        destination = self.spec_root / "Aurora" / "Awaiting_Retrospective" / "group" / "feature"
        destination.parent.mkdir(parents=True)
        package.rename(destination)
        self.assertIn(
            "awaiting_retrospective package is missing review_report.md",
            self.check(destination),
        )
        self.reports(destination)
        self.assertEqual([], self.check(destination))

    def test_draft_does_not_require_queue_approval(self) -> None:
        package = self.package("Under_Development")
        spec = package / "spec.md"
        spec.write_text(
            spec.read_text(encoding="utf-8")
            .replace("Human Sanity Decision: AFFIRMED", "Human Sanity Decision: PENDING")
            .replace("Closure: approved", "Closure: pending")
            .replace("Status: approved", "Status: designing"),
            encoding="utf-8",
        )
        self.assertEqual([], self.check(package))

    def test_wrong_case_or_missing_owner_cannot_bypass_stage_gates(self) -> None:
        for stage in (
            "queue",
            "QUEUE",
            "in_progress",
            "In_progress",
            "Awaiting_retro",
            "Unknown",
        ):
            errors = self.check(
                self.package(
                    stage,
                    {
                        "queue": "Cedar_Group",
                        "QUEUE": "Maple_Group",
                        "in_progress": "Birch_Group",
                        "In_progress": "Willow_Group",
                        "Awaiting_retro": "Alder_Group",
                        "Unknown": "Spruce_Group",
                    }[stage],
                )
            )
            self.assertIn(f"unknown package stage directory: {stage}", errors)
        for owner in ("Blueprint_Library", "Field_Notes"):
            self.assertTrue(
                any("package must be under" in e for e in self.check(self.package("Queue", owner)))
            )

    def test_awaiting_retrospective_requires_reports_and_closure_ledger(self) -> None:
        package = self.package("Awaiting_Retrospective")
        errors = self.check(package)
        self.assertIn("awaiting_retrospective package is missing review_report.md", errors)
        self.assertIn("awaiting_retrospective package is missing merge_summary.md", errors)
        self.reports(package)
        self.assertEqual([], self.check(package))
        (package / "spec.md").write_text(VALID_SPEC, encoding="utf-8")
        self.assertTrue(any("Closure ledger" in e for e in self.check(package)))

    def test_done_requires_ready_but_pending_retro_can_checkpoint(self) -> None:
        package = self.package("Awaiting_Retrospective")
        self.reports(package, "IMPLEMENTATION FINDINGS")
        self.assertEqual([], self.check(package))
        destination = self.spec_root / "Aurora" / "Done" / "group" / "feature"
        destination.parent.mkdir(parents=True)
        package.rename(destination)
        self.assertIn("Done package requires retrospective Status: READY", self.check(destination))
        self.reports(destination, "READY")
        self.assertEqual([], self.check(destination))
        (destination / "retrospective.md").unlink()
        self.assertIn("Done package requires a READY retrospective.md", self.check(destination))

    def test_new_done_cannot_use_legacy_contract_to_skip_approval(self) -> None:
        package = self.package("Done")
        self.reports(package, "READY")
        spec = package / "spec.md"
        spec.write_text(VALID_SPEC, encoding="utf-8")
        self.assertEqual([], self.check(package))
        spec.write_text(
            VALID_SPEC.replace("Closure: approved", "Closure: pending"), encoding="utf-8"
        )
        self.assertIn("done package requires Closure: approved", self.check(package))

    def test_permanent_artifact_gate_applies_to_file_and_directory_intake(self) -> None:
        for stage in ("Awaiting_Retrospective", "Done"):
            package = self.package(stage)
            self.reports(package, "READY")
            self.assertEqual([], self.check(package))
            (package / "run.md").write_text("operational scratch", encoding="utf-8")
            for intake in (package, package / "spec.md"):
                self.assertTrue(
                    any("non-permanent artifacts: run.md" in e for e in self.check(intake))
                )

    def attach(self, package: Path, manifest: str, *files: str) -> None:
        for name in files:
            target = package / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("attachment", encoding="utf-8")
        spec = package / "spec.md"
        spec.write_text(
            spec.read_text(encoding="utf-8") + "\n## Package attachments\n" + manifest + "\n",
            encoding="utf-8",
        )

    def test_declared_attachments_are_permanent_in_late_stages(self) -> None:
        for stage in ("Awaiting_Retrospective", "Done"):
            package = self.package(stage)
            self.reports(package, "READY")
            self.attach(
                package,
                "- `attachments/mockups/home.html` — Approved home layout.\n"
                "- `attachments/proof.txt` — Prototype timing proof.",
                "attachments/mockups/home.html",
                "attachments/proof.txt",
            )
            self.assertEqual([], self.check(package))
            self.assertEqual([], self.check(package / "spec.md"))

    def test_undeclared_or_missing_attachments_fail(self) -> None:
        package = self.package("Done")
        self.reports(package, "READY")
        self.attach(
            package,
            "- `attachments/a.png` — Declared mockup.\n"
            "- `attachments/gone.png` — Declared but absent.",
            "attachments/a.png",
            "attachments/stray.png",
        )
        errors = self.check(package)
        self.assertTrue(any("undeclared attachments: attachments/stray.png" in e for e in errors))
        self.assertTrue(any("missing attachments: attachments/gone.png" in e for e in errors))

    def test_attachment_entry_needs_purpose_and_contained_path(self) -> None:
        package = self.package("Done")
        self.reports(package, "READY")
        self.attach(
            package,
            "- `attachments/a.png`\n- `attachments/../escape.png` — Escapes.",
            "attachments/a.png",
        )
        errors = self.check(package)
        self.assertTrue(any("has no purpose: attachments/a.png" in e for e in errors))
        self.assertTrue(any("path is invalid" in e for e in errors))

    def test_attachments_path_that_is_a_file_is_non_permanent(self) -> None:
        package = self.package("Done")
        self.reports(package, "READY")
        (package / "attachments").write_text("not a folder", encoding="utf-8")
        self.assertTrue(
            any("non-permanent artifacts: attachments" in e for e in self.check(package))
        )

    def test_legacy_nested_done_remains_readable_without_new_ready_gate(self) -> None:
        package = self.root / "specs" / "done" / "Aurora" / "feature"
        package.mkdir(parents=True)
        (package / "spec.md").write_text(VALID_SPEC, encoding="utf-8")
        self.assertEqual([], self.check(package))
        (package / "run.md").write_text("scratch", encoding="utf-8")
        self.assertTrue(any("non-permanent artifacts: run.md" in e for e in self.check(package)))

    BRANCH_LINE = "Branch: spec/example\n"

    def set_branch_header(self, package: Path, header: str) -> None:
        """Replace the valid fixture's Branch line with exact header text."""
        spec = package / "spec.md"
        head, separator, body = spec.read_text(encoding="utf-8").partition("\n## ")
        head = re.sub(r"(?im)^\s*\*{0,2}(?:proposed )?branch\*{0,2}\s*:.*\n", "", head)
        head = head.replace("Target repo:", header + "Target repo:", 1)
        spec.write_text(head + separator + body, encoding="utf-8")

    def branch_errors(self, package: Path) -> list[str]:
        return [
            error
            for error in self.check(package)
            if "Branch:" in error or "Proposed branch" in error
        ]

    def stage_package(self, stage: str) -> Path:
        package = self.package(stage)
        if stage == "Awaiting_Retrospective":
            self.reports(package)
        return package

    def test_branch_header_required_in_active_stages_and_restorable(self) -> None:
        for stage, lifecycle in (
            ("Queue", "queue"),
            ("In_Progress", "in_progress"),
            (".pipeline", ".pipeline"),
            ("Needs_Fixes", "needs_fixes"),
            ("Awaiting_Retrospective", "awaiting_retrospective"),
        ):
            with self.subTest(stage=stage):
                package = self.stage_package(stage)
                self.assertEqual([], self.branch_errors(package))
                self.set_branch_header(package, "")
                self.assertEqual(
                    [f"{lifecycle} package requires one Branch: header"],
                    self.branch_errors(package),
                )
                (package / "spec.md").write_text(
                    VALID_SPEC_V3.replace("Target repo: /tmp/repo", f"Target repo: {self.repo}"),
                    encoding="utf-8",
                )
                self.assertEqual([], self.branch_errors(package))

    def test_approved_draft_requires_branch_but_pending_draft_does_not(self) -> None:
        package = self.package("Under_Development")
        self.assertEqual([], self.branch_errors(package))
        self.set_branch_header(package, "")
        self.assertEqual(
            ["under_development package requires one Branch: header"],
            self.branch_errors(package),
        )
        spec = package / "spec.md"
        spec.write_text(
            spec.read_text(encoding="utf-8").replace("Closure: approved", "Closure: pending"),
            encoding="utf-8",
        )
        self.assertEqual([], self.branch_errors(package))

    def test_pending_draft_moved_to_queue_gains_branch_error_at_destination(
        self,
    ) -> None:
        package = self.package("Under_Development")
        self.set_branch_header(package, "")
        spec = package / "spec.md"
        spec.write_text(
            spec.read_text(encoding="utf-8").replace("Closure: approved", "Closure: pending"),
            encoding="utf-8",
        )
        self.assertEqual([], self.branch_errors(package))
        destination = self.spec_root / "Aurora" / "Queue" / "group" / "feature"
        destination.parent.mkdir(parents=True)
        package.rename(destination)
        self.assertFalse(package.exists())
        self.assertEqual(
            ["queue package requires one Branch: header"],
            self.branch_errors(destination),
        )

    def test_validate_spec_in_process_enforces_branch_header(self) -> None:
        package = self.package("Queue")
        self.set_branch_header(package, "")
        self.assertIn(
            "queue package requires one Branch: header",
            VALIDATOR.validate_spec(package / "spec.md", self.spec_root),
        )

    def test_branch_value_rule_and_duplicate_in_queue(self) -> None:
        package = self.package("Queue")
        invalid = [
            "TBD later",
            "`spec/x`",
            "spec/<feature-name>",
            "spec/x.lock",
            "spec/../x",
            "-x",
            "spec/x/",
            "spec/.x",
            "spec/x~1",
            "spec/x^1",
            "spec/a:b",
            "spec/x?",
            "spec/x*",
            "spec/[x",
            "spec\\x",
            "spec/x\x01y",
            "spec//x",
            "spec/x@{1}",
            "@",
            "spec/x.",
            "/x",
            "",
            "not applicable \u2014",
            "not applicable - non-Git target",
            "spec/x\x7fy",
        ]
        for value in invalid:
            with self.subTest(invalid=value):
                self.set_branch_header(package, f"Branch: {value}".rstrip() + "\n")
                self.assertEqual([f"invalid Branch: value: {value}"], self.branch_errors(package))
                self.set_branch_header(package, self.BRANCH_LINE)
        valid = [
            "spec/x-t01",
            "main",
            "feature/a.b",
            "not applicable \u2014 non-Git target",
        ]
        for value in valid:
            with self.subTest(valid=value):
                self.set_branch_header(package, f"Branch: {value}\n")
                self.assertEqual([], self.branch_errors(package))
                self.set_branch_header(package, self.BRANCH_LINE)
        self.set_branch_header(package, self.BRANCH_LINE * 2)
        self.assertEqual(["duplicate Branch: header"], self.branch_errors(package))

    def test_branch_bold_forms_follow_unchanged_metadata_capture(self) -> None:
        package = self.package("Queue")
        cases = (
            ("**Branch:** spec/x\n", ["invalid Branch: value: ** spec/x"]),
            ("**Branch**: spec/x\n", []),
            ("**Branch**: spec/x\nBranch: spec/x\n", ["duplicate Branch: header"]),
        )
        for header, expected in cases:
            with self.subTest(header=header):
                self.set_branch_header(package, header)
                self.assertEqual(expected, self.branch_errors(package))
                self.set_branch_header(package, self.BRANCH_LINE)

    def test_branch_line_below_first_heading_is_not_counted(self) -> None:
        package = self.package("Queue")
        spec = package / "spec.md"
        spec.write_text(
            spec.read_text(encoding="utf-8").replace(
                "## Objective\n", "## Objective\nBranch: spec/other\n", 1
            ),
            encoding="utf-8",
        )
        self.assertEqual([], self.branch_errors(package))

    def test_branch_header_not_required_in_history_stages(self) -> None:
        legacy = self.root / "specs" / "done" / "Aurora" / "feature"
        legacy.mkdir(parents=True)
        (legacy / "spec.md").write_text(
            VALID_SPEC_V3.replace("Target repo: /tmp/repo", f"Target repo: {self.repo}"),
            encoding="utf-8",
        )
        packages = [legacy]
        for stage in ("Done", "Archive", "Reference"):
            package = self.package(stage)
            if stage == "Done":
                self.reports(package, "READY")
            packages.append(package)
        for package in packages:
            for header in ("", "Branch: none \u2014 historical\n"):
                with self.subTest(package=str(package), header=header):
                    self.set_branch_header(package, header)
                    self.assertEqual([], self.branch_errors(package))

    def test_closure_approved_archive_and_reference_need_no_branch(self) -> None:
        for stage in ("Archive", "Reference"):
            with self.subTest(stage=stage):
                package = self.package(stage)
                spec = package / "spec.md"
                self.assertIn("Closure: approved", spec.read_text(encoding="utf-8"))
                self.set_branch_header(package, "")
                self.assertEqual([], self.branch_errors(package))

    def test_retired_proposed_branch_header_rejected_in_every_stage(self) -> None:
        expected = ["retired header Proposed branch: is not accepted; use Branch:"]
        for stage in ("Under_Development", "Queue", "Done", "Archive"):
            for spelling in (
                "Proposed branch: spec/x",
                "**Proposed branch:** spec/x",
                "proposed branch: spec/x",
            ):
                with self.subTest(stage=stage, spelling=spelling):
                    package = self.package(stage)
                    if stage == "Done":
                        self.reports(package, "READY")
                    spec = package / "spec.md"
                    text = spec.read_text(encoding="utf-8")
                    if stage == "Under_Development":
                        text = text.replace("Closure: approved", "Closure: pending")
                    spec.write_text(
                        text.replace(self.BRANCH_LINE, self.BRANCH_LINE + spelling + "\n"),
                        encoding="utf-8",
                    )
                    self.assertEqual(expected, self.branch_errors(package))
        package = self.package("Queue")
        spec = package / "spec.md"
        spec.write_text(
            spec.read_text(encoding="utf-8").replace(
                "## Objective\n", "## Objective\nProposed branch: spec/x\n", 1
            ),
            encoding="utf-8",
        )
        self.assertEqual([], self.branch_errors(package))
