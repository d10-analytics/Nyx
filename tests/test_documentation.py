"""Behavioral proof for the committed fictional documentation workspace."""

from __future__ import annotations

import json
from pathlib import Path

from nyx import catalog

SAMPLE_ROOT = Path(__file__).parents[1] / "examples" / "sample-specifications"
PROGRAM_ID = "99999999-9999-4999-8999-999999999999"
PREREQUISITE_ID = "55555555-5555-4555-8555-555555555555"


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
