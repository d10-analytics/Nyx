"""Tests of the specification template shipped in docs/template_spec.md."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from nyx import spec_validation
from nyx.spec_header import metadata_values

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "docs" / "template_spec.md"
FICTIONAL_UUID = "3f2b8c1e-5d4a-4e6f-9a7b-1c2d3e4f5a6b"
FICTIONAL_COMMIT = "a" * 40
_DIAGNOSED_SENTENCE = "Invalid values are diagnosed"
_UNINTERPRETED_SENTENCE = (
    "An invalid value is not interpreted: Nyx leaves the affected relationship "
    "unresolved or unknown, and the validator does not check these fields."
)


def read_template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def collapse(text: str) -> str:
    return " ".join(text.split())


def scaffold_with_fictional_values(template: str, repository: Path) -> str:
    text = template.replace("Branch: spec/<feature-name>", "Branch: spec/aurora-sample")
    text = text.replace("/absolute/path/to/repository", str(repository))
    text = text.replace("<full 40-character lowercase commit SHA>", FICTIONAL_COMMIT)
    text = text.replace("<same full commit SHA>", FICTIONAL_COMMIT)
    text = text.replace(
        "<PROCEED_TO_DESIGN|REDUCE|REDESIGN|DEFER|REJECT>", "PROCEED_TO_DESIGN"
    )
    text = text.replace(
        "<PENDING|AFFIRMED|OVERRIDDEN|DEFERRED|REJECTED>", "AFFIRMED"
    )
    text = text.replace("Package ID: <lowercase UUIDv4>", f"Package ID: {FICTIONAL_UUID}")
    text = text.replace("<OPEN|RECONCILED>", "RECONCILED")
    return re.sub(r"<[^<>\n]+>", "fictional", text)


class SpecificationTemplateTests(unittest.TestCase):
    def test_current_template_is_minimal_deversioned_scaffold(self) -> None:
        template = read_template()
        self.assertNotIn("Critique contract", template)
        self.assertIn("Package ID: <lowercase UUIDv4>", template)
        self.assertIn("| Task | File | Access and purpose |", template)
        self.assertIn("## Closure preflight", template)
        self.assertNotIn("## Closure matrix", template)
        self.assertNotIn("- Objective:", template)
        self.assertNotIn("- Atomicity:", template)
        header = template.split("\n## ", 1)[0]
        self.assertEqual(1, len(metadata_values(header.splitlines(), "Branch")))
        self.assertIn("Branch: spec/<feature-name>", header.splitlines())
        self.assertNotIn("Proposed branch", header)
        self.assertIn("not applicable — <reason>", template)
        self.assertIn("`Closure: approved`", template)
        self.assertIn("`Proposed branch:` is retired", template)
        self.assertIn(
            "`Branch:` names the package's feature branch exactly as git spells it, without\n"
            "Markdown formatting; write `Branch: not applicable — <reason>` when the target\n"
            "is not a Git checkout. The validator requires exactly one `Branch:` line once\n"
            "`Closure: approved` is recorded and in Queue, In_Progress, `.pipeline`,\n"
            "Needs_Fixes and Awaiting_Retrospective. `Proposed branch:` is retired and\n"
            "rejected in every stage.\n",
            template,
        )

    def test_last_paragraph_names_the_ledger_reference_without_a_path(self) -> None:
        last = read_template().rstrip("\n").rsplit("\n\n", 1)[1]
        self.assertTrue(
            collapse(last).startswith(
                "<Use the closure ledger reference of the workflow for the checked schema."
            )
        )
        self.assertNotIn("/", last)
        self.assertNotIn("\\", last)
        self.assertNotIn(".md", last)

    def test_relationship_metadata_paragraph_states_invalid_values_are_not_interpreted(
        self,
    ) -> None:
        template = read_template()
        section = template.split("## Package relationship metadata", 1)[1].split("\n## ", 1)[0]
        paragraph = collapse(section)
        self.assertNotIn(_DIAGNOSED_SENTENCE, collapse(template))
        self.assertNotIn("diagnosed", paragraph)
        self.assertIn(_UNINTERPRETED_SENTENCE, paragraph)

    def test_scaffold_passes_structural_checks_as_a_queue_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "Code" / "Aurora-parallel"
            repository.mkdir(parents=True)
            spec = root / "Specs" / "Aurora" / "Queue" / "sample" / "spec.md"
            spec.parent.mkdir(parents=True)
            spec.write_text(
                scaffold_with_fictional_values(read_template(), repository),
                encoding="utf-8",
            )
            errors = spec_validation.validate_spec(spec, root / "Specs")
        self.assertEqual([], [e for e in errors if "missing required section" in e], errors)
        for fragment in ("Sanity", "Branch:", "Critique contract"):
            self.assertEqual([], [e for e in errors if fragment in e], errors)

    def test_scaffold_fails_when_a_required_section_or_branch_line_is_removed(self) -> None:
        for removed, expected in (
            ("## Closure preflight", "missing required section"),
            ("Branch: spec/<feature-name>\n", "Branch:"),
        ):
            with self.subTest(removed=removed):
                template = read_template()
                self.assertIn(removed, template)
                mutated = template.replace(removed, "", 1)
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    repository = root / "Code" / "Aurora-parallel"
                    repository.mkdir(parents=True)
                    spec = root / "Specs" / "Aurora" / "Queue" / "sample" / "spec.md"
                    spec.parent.mkdir(parents=True)
                    spec.write_text(
                        scaffold_with_fictional_values(mutated, repository),
                        encoding="utf-8",
                    )
                    errors = spec_validation.validate_spec(spec, root / "Specs")
                self.assertTrue(any(expected in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
