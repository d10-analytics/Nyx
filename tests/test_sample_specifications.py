"""Exercise the shipped sample through the real catalog."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nyx import catalog
from nyx.models import SCHEMA_VERSION, canonical_digest, parse_catalog

SAMPLE_ROOT = Path(__file__).parents[1] / "examples" / "sample-specifications"
SAMPLE_README = SAMPLE_ROOT / "README.md"
PROGRAM_ID = "99999999-9999-4999-8999-999999999999"
CONTRACT_ID = "55555555-5555-4555-8555-555555555555"
IMPLEMENTATION_ID = "77777777-7777-4777-8777-777777777777"
COMPLETED_STAGE_POLICIES = [(), ("Done",)]
PLAN_ID = "11111111-1111-4111-8111-111111111111"
DELIVERY_ID = "22222222-2222-4222-8222-222222222222"
VERIFY_ID = "66666666-6666-4666-8666-666666666666"

# Every work item the sample ships, keyed by package path: its title, package
# ID, literal stage directory, served prerequisite and program resolution.
WORK_ITEMS = {
    "Trail_API/Done/record": (
        "Define the route API contract",
        CONTRACT_ID,
        "Done",
        [],
        "not_declared",
    ),
    "Trail_API/In_Progress/implementation": (
        "Build the route API",
        IMPLEMENTATION_ID,
        "In_Progress",
        [],
        "not_declared",
    ),
    "Trail_Web/Awaiting_Retrospective/review": (
        "Review the route search",
        "44444444-4444-4444-8444-444444444444",
        "Awaiting_Retrospective",
        [],
        "not_declared",
    ),
    "Trail_Web/Needs_Fixes/repair": (
        "Fix saved-route navigation",
        "33333333-3333-4333-8333-333333333333",
        "Needs_Fixes",
        [],
        "not_declared",
    ),
    "Trail_Web/Queue/delivery": (
        "Build the route preview",
        DELIVERY_ID,
        "Queue",
        [{"target_package_id": IMPLEMENTATION_ID, "reason": "resolved"}],
        "resolved",
    ),
    "Trail_Web/Testing/verify": (
        "Verify the route preview",
        VERIFY_ID,
        "Testing",
        [{"target_package_id": CONTRACT_ID, "reason": "resolved"}],
        "resolved",
    ),
    "Trail_Web/Under_Development/plan": (
        "Plan the route preview",
        PLAN_ID,
        "Under_Development",
        [{"target_package_id": CONTRACT_ID, "reason": "resolved"}],
        "resolved",
    ),
}


def _serve(completed_stages: tuple[str, ...] = ()) -> dict[str, object]:
    return json.loads(
        catalog.build_catalog(SAMPLE_ROOT, completed_stage_names=completed_stages)
    )


def _entry_by_path(value: dict[str, object], package_path: str) -> dict[str, object]:
    entries = value["entries"]
    assert isinstance(entries, list)
    return next(entry for entry in entries if entry["package_path"] == package_path)


def _readme_work_titles() -> list[str]:
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in SAMPLE_README.read_text(encoding="utf-8").splitlines()
        if line.startswith("| ")
    ]
    assert rows, "the walkthrough lists its work items in a table"
    header = rows[0]
    assert header == ["Project", "Stage", "Work"]
    separator = rows[1]
    assert len(separator) == len(header)
    assert all(cell and set(cell) <= {"-", ":"} for cell in separator)
    work = header.index("Work")
    return [cells[work] for cells in rows[2:]]


@pytest.mark.parametrize(
    "completed_stages",
    COMPLETED_STAGE_POLICIES,
    ids=["all-stages", "completed-done"],
)
def test_sample_catalog_serves_whole_work_items_and_whole_item_prerequisites(
    completed_stages: tuple[str, ...],
) -> None:
    value = _serve(completed_stages)

    assert value["schema_version"] == SCHEMA_VERSION
    assert value["configuration_revision"] is None
    assert value["inventory"] == {
        "projects": [
            {"name": "Trail_API", "availability": "complete"},
            {"name": "Trail_Mobile", "availability": "complete"},
            {"name": "Trail_Web", "availability": "complete"},
        ],
        "stages": [
            {"project": project, "stage": stage, "availability": "complete"}
            for project, stage in (
                ("Trail_API", "Done"),
                ("Trail_API", "In_Progress"),
                ("Trail_Web", "Awaiting_Retrospective"),
                ("Trail_Web", "Needs_Fixes"),
                ("Trail_Web", "Queue"),
                ("Trail_Web", "Ready_For_Review"),
                ("Trail_Web", "Testing"),
                ("Trail_Web", "Under_Development"),
            )
        ],
    }
    assert value["terminal_stages"] == list(completed_stages)
    assert "visibility" not in value
    assert value["identity_coverage"] == {"state": "complete", "diagnostics": []}
    assert value["program_coverage"] == {"state": "complete", "diagnostics": []}
    assert value["discovery_diagnostics"] == []

    entries = value["entries"]
    assert isinstance(entries, list)
    assert [entry["package_path"] for entry in entries] == sorted(WORK_ITEMS)
    for package_path, expected in WORK_ITEMS.items():
        title, package_id, stage, prerequisites, program_resolution = expected
        entry = _entry_by_path(value, package_path)
        project = package_path.split("/")[0]
        assert entry["declared"]["title"] == title
        assert entry["declared"]["target_project"] == project
        assert entry["package_id"] == package_id
        assert entry["project"] == project
        assert entry["stage"] == stage
        assert set(entry) == {
            "package_id",
            "package_path",
            "project",
            "stage",
            "declared",
            "reported_fields",
            "relationship",
        }
        assert entry["reported_fields"] == []
        assert set(entry["relationship"]) == {"prerequisites", "program"}
        assert entry["relationship"]["prerequisites"] == prerequisites
        assert entry["relationship"]["program"]["resolution"] == program_resolution

    program = {
        "program_id": PROGRAM_ID,
        "title": "Route preview",
        "resolution": "resolved",
    }
    for package_path in (
        "Trail_Web/Under_Development/plan",
        "Trail_Web/Queue/delivery",
        "Trail_Web/Testing/verify",
    ):
        assert _entry_by_path(value, package_path)["relationship"]["program"] == program
    assert value["programs"] == [
        {
            "program_id": PROGRAM_ID,
            "title": "Route preview",
            "member_package_ids": [PLAN_ID, DELIVERY_ID, VERIFY_ID],
        }
    ]

    assert parse_catalog(value).as_dict() == value
    legacy = dict(value, schema_version=4)
    legacy["catalog_digest"] = canonical_digest(legacy)
    with pytest.raises(ValueError):
        parse_catalog(legacy)


def test_completed_stages_change_no_served_entry() -> None:
    without_policy = _serve()
    with_done = _serve(("Done",))

    assert without_policy["terminal_stages"] == []
    assert with_done["terminal_stages"] == ["Done"]
    assert without_policy["catalog_digest"] != with_done["catalog_digest"]
    assert without_policy["entries"] == with_done["entries"]


def test_readme_table_titles_equal_the_served_work_items() -> None:
    titles = _readme_work_titles()
    served = [entry["declared"]["title"] for entry in _serve()["entries"]]

    assert len(titles) == 7
    assert len(set(titles)) == len(titles)
    assert sorted(titles) == sorted(served)


def test_readme_retells_the_sample_for_whole_work_items() -> None:
    text = " ".join(SAMPLE_README.read_text(encoding="utf-8").split())

    assert "seven work items" in text
    assert "names the whole API contract work item" in text
    assert "names no repository" in text
    assert "every arrow stays outstanding" in text


def test_sample_specifications_declare_no_retired_header_row() -> None:
    specifications = sorted(SAMPLE_ROOT.rglob("spec.md"))

    assert len(specifications) == len(WORK_ITEMS)
    for document in specifications:
        for line in document.read_text(encoding="utf-8").splitlines():
            assert not line.startswith(("Prerequisite:", "Claim:")), (document, line)


def test_sample_text_never_names_a_claim() -> None:
    specifications = sorted(SAMPLE_ROOT.rglob("spec.md"))
    for document in [SAMPLE_README, *specifications]:
        assert "claim" not in document.read_text(encoding="utf-8").lower(), document
