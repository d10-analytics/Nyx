"""Tests of the specification validator rules."""
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

from nyx import spec_validation


class ValidateWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "AGENTS.md").write_text("test authority\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write(self, name: str, content: str) -> Path:
        path = self.root / name
        path.write_text(
            content.replace("Target repo: /tmp/repo", f"Target repo: {self.repo}"),
            encoding="utf-8",
        )
        return path

    def test_valid_spec_passes(self) -> None:
        spec = self.write("spec.md", VALID_SPEC)
        self.assertEqual([], VALIDATOR.validate_spec(spec))

    def test_contract_two_requires_closure_matrix(self) -> None:
        start = VALID_SPEC.index("## Closure matrix")
        end = VALID_SPEC.index("## Verification")
        spec = self.write("spec.md", VALID_SPEC[:start] + VALID_SPEC[end:])
        errors = VALIDATOR.validate_spec(spec)
        self.assertTrue(
            any("missing required section: ## Closure matrix" in e for e in errors)
        )

    def test_under_development_package_accepts_legacy_contract_two(self) -> None:
        package = self.root / "specs" / "under_development" / "feature"
        package.mkdir(parents=True)
        spec = package / "spec.md"
        spec.write_text(
            VALID_SPEC,
            encoding="utf-8",
        )
        self.assertEqual([], VALIDATOR.validate_spec(spec))

    def test_contract_two_remains_supported_outside_active_critique(self) -> None:
        spec = self.write("spec.md", VALID_SPEC)
        self.assertEqual([], VALIDATOR.validate_spec(spec))

    def test_valid_contract_three_uses_generated_traceability(self) -> None:
        spec = self.write("spec.md", VALID_SPEC_V3)
        self.assertEqual([], VALIDATOR.validate_spec(spec))
        self.assertNotIn("## Closure matrix", VALID_SPEC_V3)

    def test_contract_three_requires_complete_reconciled_preflight(self) -> None:
        spec = self.write(
            "spec.md",
            VALID_SPEC_V3.replace(
                "| Commit order | `T01` dependency and boundaries | RECONCILED |\n",
                "| Commit order | `T01` dependency and boundaries | OPEN |\n",
            ),
        )
        errors = VALIDATOR.validate_spec(spec)
        self.assertTrue(any("'Commit order' is not RECONCILED" in e for e in errors))

    def test_contract_three_requires_criterion_requirement_and_decision_refs(
        self,
    ) -> None:
        spec = self.write(
            "spec.md",
            VALID_SPEC_V3.replace("Under `R-01` and `D-01`, ", ""),
        )
        errors = VALIDATOR.validate_spec(spec)
        self.assertTrue(any("AC-01 must cite" in e for e in errors))

    def test_contract_three_requires_owned_ac_in_each_writable_purpose(self) -> None:
        spec = self.write(
            "spec.md",
            VALID_SPEC_V3.replace(
                "Write: natural trigger proof for `AC-01`",
                "Write: natural trigger proof",
            ),
        )
        errors = VALIDATOR.validate_spec(spec)
        self.assertTrue(any("purpose must cite an owned AC ID" in e for e in errors))

    def test_contract_three_rejects_stored_closure_matrix(self) -> None:
        spec = self.write(
            "spec.md",
            VALID_SPEC_V3.replace(
                "## Closure preflight",
                "## Closure matrix\n\nOld duplicate.\n\n## Closure preflight",
            ),
        )
        errors = VALIDATOR.validate_spec(spec)
        self.assertTrue(any("remove stored ## Closure matrix" in e for e in errors))

    def test_contract_three_queue_requires_recorded_human_gates(self) -> None:
        package = self.root / "specs" / "queue" / "feature"
        package.mkdir(parents=True)
        spec = package / "spec.md"
        spec.write_text(
            VALID_SPEC_V3.replace("Closure: approved", "Closure: pending").replace(
                "Human Sanity Decision: AFFIRMED", "Human Sanity Decision: PENDING"
            ),
            encoding="utf-8",
        )
        errors = VALIDATOR.validate_package(package)
        self.assertTrue(any("requires Closure: approved" in error for error in errors))
        self.assertTrue(
            any("affirmative human sanity decision" in error for error in errors)
        )

    def test_contract_three_queue_rejects_affirmed_nonproceed_recommendation(
        self,
    ) -> None:
        package = self.root / "specs" / "queue" / "feature"
        package.mkdir(parents=True)
        spec = package / "spec.md"
        spec.write_text(
            VALID_SPEC_V3.replace(
                "Target repo: /tmp/repo", f"Target repo: {self.repo}"
            ).replace(
                "Sanity Recommendation: IMPLEMENT", "Sanity Recommendation: REJECT"
            ),
            encoding="utf-8",
        )
        errors = VALIDATOR.validate_package(package)
        self.assertTrue(any("cannot affirm and queue" in error for error in errors))

    def test_contract_three_requires_complete_sanity_metadata(self) -> None:
        content = VALID_SPEC_V3
        for line in (
            "Sanity Recommendation: IMPLEMENT\n",
            "Sanity Baseline: abc123\n",
            "Human Sanity Decision: AFFIRMED\n",
        ):
            content = content.replace(line, "")
        errors = VALIDATOR.validate_spec(self.write("spec.md", content))
        self.assertTrue(
            any("requires Sanity Recommendation" in error for error in errors)
        )

    def test_proceed_to_design_accepts_queue_only_with_existing_human_gates(
        self,
    ) -> None:
        package = self.root / "specs" / "queue" / "feature"
        package.mkdir(parents=True)
        spec = package / "spec.md"
        content = VALID_SPEC_V3.replace(
            "Target repo: /tmp/repo", f"Target repo: {self.repo}"
        ).replace(
            "Sanity Recommendation: IMPLEMENT",
            "Sanity Recommendation: PROCEED_TO_DESIGN",
        )
        spec.write_text(content, encoding="utf-8")
        self.assertEqual([], VALIDATOR.validate_package(package))

        spec.write_text(
            content.replace("Closure: approved", "Closure: pending").replace(
                "Human Sanity Decision: AFFIRMED", "Human Sanity Decision: PENDING"
            ),
            encoding="utf-8",
        )
        errors = VALIDATOR.validate_package(package)
        self.assertTrue(any("requires Closure: approved" in error for error in errors))
        self.assertTrue(
            any("affirmative human sanity decision" in error for error in errors)
        )

    def test_verification_matrix_requires_exactly_eight_cells(self) -> None:
        spec = self.write(
            "spec.md",
            VALID_SPEC_V3.replace(
                "| Full suite | `AGENTS.md` | `pytest` | pre-queue | capable-host local | skip known-incompatible sandbox | no | no |",
                "| Full suite | `AGENTS.md` |",
            ),
        )
        errors = VALIDATOR.validate_spec(spec)
        self.assertTrue(any("exactly 8 cells" in error for error in errors))

    def test_contract_three_rejects_missing_verification_authority_file(self) -> None:
        spec = self.write(
            "spec.md",
            VALID_SPEC_V3.replace("`AGENTS.md`", "`missing-ci.yml`"),
        )
        errors = VALIDATOR.validate_spec(spec)
        self.assertTrue(
            any("cites missing authority file" in error for error in errors)
        )

    def test_excerpt_validation_expands_references_transitively(self) -> None:
        spec = self.write(
            "spec.md",
            VALID_SPEC_V3.replace(
                "- `D-01` — Use the existing owner.",
                "- `D-01` — Use the existing owner under `F-99`.",
            ),
        )
        errors = VALIDATOR.validate_spec(spec)
        self.assertTrue(
            any("T01 excerpt cites undefined IDs: F-99" in error for error in errors)
        )

    def test_multiline_task_boundaries_match_boundary_table(self) -> None:
        spec = self.write(
            "spec.md",
            VALID_SPEC.replace(
                "- Boundaries: `src/a.py`, `tests/test_a.py`.",
                "- Boundaries: `src/a.py`,\n  `tests/test_a.py`.",
            ),
        )
        self.assertEqual([], VALIDATOR.validate_spec(spec))

    def test_contract_two_rejects_ranged_stable_ids(self) -> None:
        spec = self.write(
            "spec.md",
            VALID_SPEC.replace("`R-01`, `D-01`", "`R-01`–`R-03`, `D-01`"),
        )
        errors = VALIDATOR.validate_spec(spec)
        self.assertTrue(any("explicit stable-ID expansion" in e for e in errors))

    def test_contract_two_rejects_free_form_verification_authority(self) -> None:
        spec = self.write(
            "spec.md",
            VALID_SPEC.replace(
                "| Full suite | `AGENTS.md` |", "| Full suite | predecessor contract |"
            ),
        )
        errors = VALIDATOR.validate_spec(spec)
        self.assertTrue(any("free-form authority" in e for e in errors))

    def test_contract_two_rejects_undefined_verification_authority(self) -> None:
        spec = self.write(
            "spec.md",
            VALID_SPEC.replace(
                "| Full suite | `AGENTS.md` |", "| Full suite | `F-99` |"
            ),
        )
        errors = VALIDATOR.validate_spec(spec)
        self.assertTrue(any("undefined authority F-99" in e for e in errors))

    def test_contract_two_requires_exact_writable_boundary_mapping(self) -> None:
        spec = self.write(
            "spec.md",
            VALID_SPEC.replace(
                "`src/a.py`: `ExistingOwner.apply` implementation; `tests/test_a.py`: natural trigger proof",
                "`tests/missing.py`: output check",
            ),
        )
        errors = VALIDATOR.validate_spec(spec)
        self.assertTrue(any("non-writable or unbounded paths" in e for e in errors))
        self.assertTrue(
            any("omits writable file-purpose ownership" in e for e in errors)
        )

    def test_contract_two_requires_task_and_boundary_table_paths_to_match(self) -> None:
        spec = self.write(
            "spec.md",
            VALID_SPEC.replace(
                "| T01 | `tests/test_a.py` | Write: natural trigger proof for `AC-01` |\n",
                "",
            ),
        )
        errors = VALIDATOR.validate_spec(spec)
        self.assertTrue(
            any("file-boundary table omits T01 task boundaries" in e for e in errors)
        )

    def test_contract_two_requires_every_reconciled_invariant_class(self) -> None:
        spec = self.write(
            "spec.md",
            VALID_SPEC.replace(
                "| Commit order | Production-first and test-first | T01 decomposition | RECONCILED |\n",
                "",
            ),
        )
        errors = VALIDATOR.validate_spec(spec)
        self.assertTrue(any("'commit order' row; found 0" in e for e in errors))

    def test_partial_sanity_contract_fails(self) -> None:
        spec = self.write(
            "spec.md",
            VALID_SPEC.replace("Sanity Baseline: abc123\n", ""),
        )
        errors = VALIDATOR.validate_spec(spec)
        self.assertTrue(any("incomplete sanity contract" in error for error in errors))

    def test_sanity_baseline_must_match_approved_baseline(self) -> None:
        spec = self.write(
            "spec.md",
            VALID_SPEC.replace("Sanity Baseline: abc123", "Sanity Baseline: def456"),
        )
        errors = VALIDATOR.validate_spec(spec)
        self.assertTrue(
            any("Sanity Baseline must match Baseline" in error for error in errors)
        )

    def test_approved_spec_cannot_have_pending_sanity_decision(self) -> None:
        spec = self.write(
            "spec.md",
            VALID_SPEC.replace(
                "Human Sanity Decision: AFFIRMED",
                "Human Sanity Decision: PENDING",
            ),
        )
        errors = VALIDATOR.validate_spec(spec)
        self.assertTrue(any("cannot be PENDING" in error for error in errors))

    def test_sanity_reviewed_spec_requires_verification_matrix(self) -> None:
        spec = self.write(
            "spec.md",
            VALID_SPEC.replace(
                "| Lane | Authority | Command | Required stage | Runner | Sandbox policy | CI-only | Push required |\n",
                "",
            ),
        )
        errors = VALIDATOR.validate_spec(spec)
        self.assertTrue(any("verification matrix header" in error for error in errors))

    def test_duplicate_definition_fails(self) -> None:
        spec = self.write(
            "spec.md",
            VALID_SPEC.replace(
                "- `R-01` — Do the thing.",
                "- `R-01` — Do the thing.\n- `R-01` — Duplicate.",
            ),
        )
        self.assertTrue(
            any(
                "duplicate ID definitions: R-01" in error
                for error in VALIDATOR.validate_spec(spec)
            )
        )

    def test_missing_primary_owner_fails(self) -> None:
        spec = self.write(
            "spec.md", VALID_SPEC.replace("- Owns: `AC-01`.", "- Owns: none.")
        )
        errors = VALIDATOR.validate_spec(spec)
        self.assertTrue(
            any(
                "AC-01 must have exactly one primary task owner" in error
                for error in errors
            )
        )

    def test_dependency_cycle_fails(self) -> None:
        content = VALID_SPEC.replace("- Depends on: none.", "- Depends on: `T02`.")
        content = content.replace(
            "## Verification",
            """### T02 — Complete the thing
- Objective: Complete it.
- Owns: `AC-02`.
- Depends on: `T01`.
- Boundaries: `src/b.py`, `tests/test_b.py`.
- Risk: standard.
- Implementation: Complete the behavior.
- Behavioral proof: Assert the exact result.
## Verification""",
        ).replace(
            "## Tasks",
            "## Tasks\n\n- [ ] `AC-02` — placeholder\n",
        )
        spec = self.write("spec.md", content)
        self.assertTrue(
            any(
                "task dependency cycle" in error
                for error in VALIDATOR.validate_spec(spec)
            )
        )


class AuthorityFileTests(unittest.TestCase):
    """A relative authority path resolves against the Target repo."""

    AUTHORITY = "fictional-authority.md"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.spec_root = Path(self.temp_dir.name) / "Specification_Library"
        self.repo = Path(self.temp_dir.name) / "repository"
        self.repo.mkdir()
        self.authority = self.repo / self.AUTHORITY
        self.authority.write_text("first\nsecond\nthird\n", encoding="utf-8")
        self.package = self.spec_root / "Aurora" / "Queue" / "group" / "feature"
        self.package.mkdir(parents=True)

    def errors(self, citation: str) -> list[str]:
        spec = self.package / "spec.md"
        spec.write_text(
            VALID_SPEC_V3.replace("Target repo: /tmp/repo", f"Target repo: {self.repo}")
            .replace("| Full suite | `AGENTS.md` |", f"| Full suite | `{citation}` |"),
            encoding="utf-8",
        )
        return spec_validation.validate_spec(spec, self.spec_root)

    def test_authority_file_is_found_under_the_target_repo_only(self) -> None:
        self.assertFalse((Path.cwd() / self.AUTHORITY).exists())
        self.assertEqual([], self.errors(self.AUTHORITY))
        self.authority.unlink()
        self.assertIn(
            "verification lane 'Full suite' cites missing authority file: "
            f"{self.authority}",
            self.errors(self.AUTHORITY),
        )

    def test_cited_line_range_must_lie_inside_the_authority_file(self) -> None:
        for citation in ("1-3", "2", "3-3", "1"):
            with self.subTest(citation=citation):
                self.assertEqual([], self.errors(f"{self.AUTHORITY}:{citation}"))
        for citation, reported in (
            ("1-4", "1-4"),
            ("4", "4-4"),
            ("0-2", "0-2"),
            ("3-2", "3-2"),
        ):
            with self.subTest(citation=citation):
                self.assertIn(
                    "verification lane 'Full suite' authority range is outside file: "
                    f"{self.authority}:{reported}",
                    self.errors(f"{self.AUTHORITY}:{citation}"),
                )


class BranchVerdictTests(unittest.TestCase):
    """The validator keeps its own verdict on every recorded branch value."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.spec_root = Path(self.temp_dir.name) / "Specification_Library"
        self.repo = Path(self.temp_dir.name) / "repository"
        self.repo.mkdir()
        (self.repo / "AGENTS.md").write_text("test authority\n", encoding="utf-8")
        self.package = self.spec_root / "Aurora" / "Queue" / "group" / "feature"
        self.package.mkdir(parents=True)

    def branch_errors(self, value: str) -> list[str]:
        spec = self.package / "spec.md"
        spec.write_text(
            VALID_SPEC_V3.replace("Target repo: /tmp/repo", f"Target repo: {self.repo}")
            .replace("Branch: spec/example", f"Branch: {value}"),
            encoding="utf-8",
        )
        return [
            error
            for error in spec_validation.validate_spec(spec, self.spec_root)
            if error.startswith("invalid Branch: value:")
        ]

    def test_invalid_values_keep_their_verdict(self) -> None:
        for value in (
            "`spec/fictional-board`",
            "not applicable (fictional reason)",
            "Not Applicable \u2014 r",
            "",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    [f"invalid Branch: value: {value}"], self.branch_errors(value)
                )

    def test_valid_values_keep_their_verdict(self) -> None:
        long_name = "spec/" + "a" * 1995
        self.assertEqual(2000, len(long_name))
        for value in (
            long_name,
            "not applicable \u2014 fictional reason",
            "spec/fictional-board",
        ):
            with self.subTest(value=value[:40]):
                self.assertEqual([], self.branch_errors(value))

    def test_branch_value_check_calls_the_shared_lexical_rule(self) -> None:
        module = Path(__file__).parents[1] / "nyx" / "spec_validation.py"
        match = re.search(
            r"(?ms)^def _branch_value_is_valid\(.*?(?=^def )",
            module.read_text(encoding="utf-8"),
        )
        self.assertIsNotNone(match)
        self.assertIn("branch_name_is_lexical(value)", match.group(0))
