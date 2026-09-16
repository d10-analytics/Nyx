"""Behavioral proof for the committed fictional documentation workspace."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from nyx import catalog, cli, runtime, state

REPOSITORY_ROOT = Path(__file__).parents[1]
SAMPLE_ROOT = Path(__file__).parents[1] / "examples" / "sample-specifications"
PROGRAM_ID = "99999999-9999-4999-8999-999999999999"
PREREQUISITE_ID = "55555555-5555-4555-8555-555555555555"
FICTIONAL_IDS = {
    PROGRAM_ID,
    PREREQUISITE_ID,
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
    "44444444-4444-4444-8444-444444444444",
}


def _entry_by_path(value: dict[str, object], package_path: str) -> dict[str, object]:
    entries = value["entries"]
    assert isinstance(entries, list)
    return next(entry for entry in entries if entry["package_path"] == package_path)


def test_committed_sample_catalog_proves_documented_walkthrough() -> None:
    value = json.loads(catalog.build_catalog(SAMPLE_ROOT, hidden_stages=("Done",)))

    assert value["schema_version"] == 3
    assert value["visibility"] == {
        "hidden_stages": ["Done"],
        "visible_entry_count": 4,
        "hidden_entry_count": 1,
    }
    assert value["identity_coverage"] == {"state": "complete", "diagnostics": []}
    assert value["program_coverage"] == {"state": "complete", "diagnostics": []}
    assert value["discovery_diagnostics"] == []

    expected_entries = {
        "Fictional_A/Under_Development/plan": (
            "Plan fictional onboarding",
            "11111111-1111-4111-8111-111111111111",
            "Under_Development",
            True,
        ),
        "Fictional_A/Queue/delivery": (
            "Deliver fictional onboarding",
            "22222222-2222-4222-8222-222222222222",
            "Queue",
            True,
        ),
        "Fictional_A/Needs_Fixes/repair": (
            "Repair fictional walkthrough",
            "33333333-3333-4333-8333-333333333333",
            "Needs_Fixes",
            True,
        ),
        "Fictional_A/Awaiting_Retrospective/review": (
            "Review fictional walkthrough",
            "44444444-4444-4444-8444-444444444444",
            "Awaiting_Retrospective",
            True,
        ),
        "Fictional_B/Done/record": (
            "Fictional prerequisite record",
            PREREQUISITE_ID,
            "Done",
            False,
        ),
    }
    assert {
        entry["package_path"] for entry in value["entries"]
    } == set(expected_entries)
    for package_path, (title, package_id, stage, board_visible) in expected_entries.items():
        entry = _entry_by_path(value, package_path)
        assert entry["declared"]["title"] == title
        assert entry["package_id"] == package_id
        assert entry["stage"] == stage
        assert entry["board_visible"] is board_visible
        assert entry["diagnostics"] == []
        assert entry["transitive_diagnostics"] == []

    plan = _entry_by_path(value, "Fictional_A/Under_Development/plan")
    delivery = _entry_by_path(value, "Fictional_A/Queue/delivery")
    hidden = _entry_by_path(value, "Fictional_B/Done/record")
    assert plan["relationship"]["program"] == {
        "program_id": PROGRAM_ID,
        "title": "Fictional delivery program",
        "resolution": "resolved",
        "diagnostics": [],
    }
    assert delivery["relationship"]["program"] == plan["relationship"]["program"]
    assert plan["relationship"]["direct_prerequisite_state"] == "satisfied"
    assert delivery["relationship"]["direct_prerequisite_state"] == "unsatisfied"
    assert plan["relationship"]["prerequisites"] == [
        {
            "target_package_id": PREREQUISITE_ID,
            "claim_name": "design-input",
            "observed_state": "satisfied",
            "observed_evidence_ref": "sha256:" + "f" * 64,
            "resolved_state": "satisfied",
            "reason": "claim_satisfied",
        }
    ]
    assert delivery["relationship"]["prerequisites"] == [
        {
            "target_package_id": PREREQUISITE_ID,
            "claim_name": "delivery-clearance",
            "observed_state": "unsatisfied",
            "observed_evidence_ref": None,
            "resolved_state": "unsatisfied",
            "reason": "claim_unsatisfied",
        }
    ]
    assert hidden["relationship"]["claims"] == [
        {
            "name": "delivery-clearance",
            "state": "unsatisfied",
            "evidence_ref": None,
            "diagnostics": [],
        },
        {
            "name": "design-input",
            "state": "satisfied",
            "evidence_ref": "sha256:" + "f" * 64,
            "diagnostics": [],
        },
    ]
    assert value["programs"] == [
        {
            "program_id": PROGRAM_ID,
            "title": "Fictional delivery program",
            "member_package_ids": [
                "11111111-1111-4111-8111-111111111111",
                "22222222-2222-4222-8222-222222222222",
            ],
            "diagnostics": [],
        }
    ]


def test_root_and_sample_guides_are_mutually_linked() -> None:
    root = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    sample = (SAMPLE_ROOT / "README.md").read_text(encoding="utf-8")

    root_link = REPOSITORY_ROOT / "examples" / "sample-specifications" / "README.md"
    sample_link = SAMPLE_ROOT / "../../README.md"
    assert "[fictional sample walkthrough](examples/sample-specifications/README.md)" in root
    assert "[Return to the root Nyx guide](../../README.md)" in sample
    assert root_link.resolve() == (SAMPLE_ROOT / "README.md").resolve()
    assert sample_link.resolve() == (REPOSITORY_ROOT / "README.md").resolve()


def test_root_guide_contains_the_literal_repository_workflow() -> None:
    root = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    required_lines = (
        "python3.12 -m venv .venv",
        ". .venv/bin/activate",
        "python -m pip install -e '.[test]'",
        "nyx --setup examples/sample-specifications --hide-stage Done",
        "nyx --status",
        "nyx",
        "nyx --stop",
        "nyx --setup examples/sample-specifications --show-all-stages",
    )
    for line in required_lines:
        assert line in root

    assert "http://127.0.0.1:8765/" in root
    assert "every ten seconds" in root
    assert "Apply update" in root
    assert "Refresh view" in root
    assert "stop-before-reconfiguration order is required" in root
    quick_start = root[root.index("```bash") : root.index("```", root.index("```bash") + 3)]
    assert quick_start.index("python3.12 -m venv .venv") < quick_start.index(
        ". .venv/bin/activate"
    ) < quick_start.index("python -m pip install -e '.[test]'") < quick_start.index(
        "nyx --setup examples/sample-specifications --hide-stage Done"
    ) < quick_start.index("nyx --status") < quick_start.index("\nnyx\n")
    assert root.index(
        "nyx --stop\nnyx --setup examples/sample-specifications --show-all-stages"
    ) < root.index("## Workspace and terminology")


def test_root_guide_explains_limits_workspace_and_authority() -> None:
    root = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    required_terms = (
        "Linux-local",
        "loopback-only",
        "read-only",
        "Python 3.12 or newer",
        "current account's configuration",
        "runtime state",
        "specification root / project / lifecycle / groups / package / spec.md",
        "program",
        "claim",
        "prerequisite",
        "diagnostics",
        "Find",
        "select a card",
        "Unblocked",
        "implementation",
        "approval",
        "review",
        "queue authority",
        "merge",
        "release",
        "deployment",
        "confidentiality",
        "runtime execution",
    )
    for term in required_terms:
        assert term in root

    assert not re.search(r"(?<!:)\/(?:home|tmp|opt|var|etc|proc|root)\/", root)
    assert not re.search(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        root,
        flags=re.IGNORECASE,
    )
    assert all(
        marker not in root
        for marker in ("".join(parts) for parts in (
            ("real", "-spec-root", ".invalid"),
            ("source", "-checkout", ".invalid"),
            ("provenance", "-map", ".invalid"),
            ("payload", "-capture", ".invalid"),
            ("diagnostic", "-screenshot", ".invalid"),
        ))
    )


def test_sample_documentation_is_synthetic_and_has_no_generated_artifacts() -> None:
    documentation = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(SAMPLE_ROOT.rglob("*.md"))
    )
    identifiers = set(
        re.findall(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
            documentation,
            flags=re.IGNORECASE,
        )
    )
    assert identifiers == FICTIONAL_IDS
    assert not re.search(r"(?<!:)\/(?:home|tmp|opt|var|etc|proc|root)\/", documentation)
    assert not any(
        path.name in {"catalog.json", "catalog.html", "screenshot.png", "screenshot.jpg"}
        or path.suffix in {".json", ".png", ".jpg", ".jpeg"}
        for path in SAMPLE_ROOT.rglob("*")
    )
    assert not any(path.name == "__pycache__" for path in SAMPLE_ROOT.rglob("*"))


def test_root_guide_preserves_packaging_metadata() -> None:
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    assert project["name"] == "nyx"
    assert project["description"] == "Private catalog viewer"
    assert project["requires-python"] == ">=3.12"


def test_root_guide_matches_runtime_and_browser_sources() -> None:
    root = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    browser = (REPOSITORY_ROOT / "nyx" / "static" / "app.js").read_text(encoding="utf-8")
    index = (REPOSITORY_ROOT / "nyx" / "static" / "index.html").read_text(encoding="utf-8")

    assert runtime.URL in root
    assert "const POLL_INTERVAL = 10000;" in browser
    assert 'refreshButton.textContent = available ? "Apply update" : "Refresh view";' in browser
    assert 'id="filter" type="search"' in index
    assert 'id="refresh" type="button"' in index
    assert 'id="details"' in index


def test_documented_cli_shapes_use_the_real_parser_and_dispatch_branches(capsys) -> None:
    parser = cli._parser()
    setup = parser.parse_args(
        ["--setup", "examples/sample-specifications", "--hide-stage", "Done"]
    )
    show_all = parser.parse_args(
        ["--setup", "examples/sample-specifications", "--show-all-stages"]
    )
    status = parser.parse_args(["--status"])
    stop = parser.parse_args(["--stop"])
    start = parser.parse_args([])
    assert (setup.setup, setup.hide_stage, setup.show_all_stages) == (
        "examples/sample-specifications",
        ["Done"],
        False,
    )
    assert (show_all.setup, show_all.hide_stage, show_all.show_all_stages) == (
        "examples/sample-specifications",
        None,
        True,
    )
    assert status.status is True
    assert stop.stop is True
    assert not any(vars(start).values())

    class Configuration:
        specification_root = Path("examples/sample-specifications")

    with patch.object(cli.runtime, "setup", return_value=Configuration()) as setup_call:
        assert cli.main(
            ["--setup", "examples/sample-specifications", "--hide-stage", "Done"]
        ) == 0
    setup_call.assert_called_once_with(
        "examples/sample-specifications", hidden_stages=["Done"]
    )
    with patch.object(cli.runtime, "setup", return_value=Configuration()) as show_all_call:
        assert cli.main(
            ["--setup", "examples/sample-specifications", "--show-all-stages"]
        ) == 0
    show_all_call.assert_called_once_with(
        "examples/sample-specifications", hidden_stages=()
    )
    with patch.object(cli.runtime, "start", return_value=runtime.URL) as start_call:
        assert cli.main([]) == 0
    start_call.assert_called_once_with()
    with patch.object(cli.runtime, "stop", return_value="stopped") as stop_call:
        assert cli.main(["--stop"]) == 0
    stop_call.assert_called_once_with()
    with patch.object(
        cli.state,
        "observe_configuration",
        return_value=state.ConfigurationObservation("not_configured"),
    ) as observe_configuration, patch.object(
        cli.runtime,
        "observe_runtime",
        return_value=runtime.RuntimeObservation("not_running"),
    ) as observe_runtime:
        assert cli.main(["--status"]) == 0
    observe_configuration.assert_called_once_with()
    observe_runtime.assert_called_once_with()
    capsys.readouterr()

    for arguments in (("--hide-stage", "Done"), ("--show-all-stages",)):
        with pytest.raises(SystemExit) as error:
            cli.main(list(arguments))
        assert error.value.code == 2
