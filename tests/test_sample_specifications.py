"""Exercise the shipped sample through the real catalog."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nyx import catalog
from nyx.models import canonical_digest, parse_catalog

SAMPLE_ROOT = Path(__file__).parents[1] / "examples" / "sample-specifications"
PROGRAM_ID = "99999999-9999-4999-8999-999999999999"
PREREQUISITE_ID = "55555555-5555-4555-8555-555555555555"


def _entry_by_path(value: dict[str, object], package_path: str) -> dict[str, object]:
    entries = value["entries"]
    assert isinstance(entries, list)
    return next(entry for entry in entries if entry["package_path"] == package_path)


@pytest.mark.parametrize("hidden_stages", [(), ("Done",)], ids=["all-stages", "hide-done"])
def test_sample_catalog_resolves_stages_program_and_prerequisites(hidden_stages) -> None:
    value = json.loads(catalog.build_catalog(SAMPLE_ROOT, hidden_stages=hidden_stages))

    assert value["schema_version"] == 5
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
                ("Trail_Web", "Awaiting_Retrospective"),
                ("Trail_Web", "Needs_Fixes"),
                ("Trail_Web", "Queue"),
                ("Trail_Web", "Ready_For_Review"),
                ("Trail_Web", "Testing"),
                ("Trail_Web", "Under_Development"),
            )
        ],
    }
    assert value["visibility"] == {
        "hidden_stages": list(hidden_stages),
            "visible_entry_count": 5 if hidden_stages else 6,
        "hidden_entry_count": 1 if hidden_stages else 0,
    }
    assert value["identity_coverage"] == {"state": "complete", "diagnostics": []}
    assert value["program_coverage"] == {"state": "complete", "diagnostics": []}
    assert value["discovery_diagnostics"] == []

    expected_entries = {
        "Trail_Web/Under_Development/plan": (
            "Plan the route preview",
            "11111111-1111-4111-8111-111111111111",
            "Under_Development",
            True,
        ),
        "Trail_Web/Queue/delivery": (
            "Build the route preview",
            "22222222-2222-4222-8222-222222222222",
            "Queue",
            True,
        ),
        "Trail_Web/Needs_Fixes/repair": (
            "Fix saved-route navigation",
            "33333333-3333-4333-8333-333333333333",
            "Needs_Fixes",
            True,
        ),
        "Trail_Web/Awaiting_Retrospective/review": (
            "Review the route search",
            "44444444-4444-4444-8444-444444444444",
            "Awaiting_Retrospective",
            True,
        ),
        "Trail_Web/Testing/verify": (
            "Verify the route preview",
            "66666666-6666-4666-8666-666666666666",
            "Testing",
            True,
        ),
        "Trail_API/Done/record": (
            "Define the route API contract",
            PREREQUISITE_ID,
            "Done",
            not hidden_stages,
        ),
    }
    assert {
        entry["package_path"] for entry in value["entries"]
    } == set(expected_entries)
    for package_path, (title, package_id, stage, board_visible) in expected_entries.items():
        entry = _entry_by_path(value, package_path)
        assert entry["declared"]["title"] == title
        assert entry["declared"]["target_project"] == package_path.split("/")[0]
        assert entry["package_id"] == package_id
        assert entry["stage"] == stage
        assert entry["board_visible"] is board_visible
        assert entry["diagnostics"] == []
        assert entry["transitive_diagnostics"] == []

    plan = _entry_by_path(value, "Trail_Web/Under_Development/plan")
    delivery = _entry_by_path(value, "Trail_Web/Queue/delivery")
    hidden = _entry_by_path(value, "Trail_API/Done/record")
    assert plan["relationship"]["program"] == {
        "program_id": PROGRAM_ID,
        "title": "Route preview",
        "resolution": "resolved",
        "diagnostics": [],
    }
    assert delivery["relationship"]["program"] == plan["relationship"]["program"]
    assert plan["relationship"]["direct_prerequisite_state"] == "satisfied"
    assert delivery["relationship"]["direct_prerequisite_state"] == "unknown"
    assert plan["relationship"]["prerequisites"] == [
        {
            "kind": "claim",
            "target_package_id": PREREQUISITE_ID,
            "claim_name": "contract-ready",
            "observed_state": "satisfied",
            "observed_evidence_ref": "sha256:" + "f" * 64,
            "resolved_state": "satisfied",
            "reason": "claim_satisfied",
        }
    ]
    assert delivery["relationship"]["prerequisites"] == [
        {
            "kind": "claim",
            "target_package_id": PREREQUISITE_ID,
            "claim_name": "implementation-ready",
            "observed_state": "unsatisfied",
            "observed_evidence_ref": None,
            "resolved_state": "unsatisfied",
            "reason": "claim_unsatisfied",
        },
        {
            "kind": "completion",
            "target_package_id": PREREQUISITE_ID,
            "observed_stage": "Done",
            "resolved_state": "unknown",
            "reason": "completion_policy_needed",
        },
    ]
    verify = _entry_by_path(value, "Trail_Web/Testing/verify")
    assert verify["relationship"]["program"] == plan["relationship"]["program"]
    assert verify["relationship"]["direct_prerequisite_state"] == "satisfied"
    assert verify["relationship"]["prerequisites"] == [
        {
            "kind": "claim",
            "target_package_id": PREREQUISITE_ID,
            "claim_name": "contract-ready",
            "observed_state": "satisfied",
            "observed_evidence_ref": "sha256:" + "f" * 64,
            "resolved_state": "satisfied",
            "reason": "claim_satisfied",
        }
    ]
    assert hidden["relationship"]["claims"] == [
        {
            "name": "contract-ready",
            "state": "satisfied",
            "evidence_ref": "sha256:" + "f" * 64,
            "diagnostics": [],
        },
        {
            "name": "implementation-ready",
            "state": "unsatisfied",
            "evidence_ref": None,
            "diagnostics": [],
        },
    ]
    assert value["programs"] == [
        {
            "program_id": PROGRAM_ID,
            "title": "Route preview",
            "member_package_ids": [
                "11111111-1111-4111-8111-111111111111",
                "22222222-2222-4222-8222-222222222222",
                "66666666-6666-4666-8666-666666666666",
            ],
            "diagnostics": [],
        }
    ]

    legacy = dict(value, schema_version=4)
    legacy["catalog_digest"] = canonical_digest(legacy)
    with pytest.raises(ValueError):
        parse_catalog(legacy)


@pytest.mark.parametrize(
    ("completed_stage_names", "completion_state", "completion_reason", "direct_state"),
    [
        (None, "unknown", "completion_policy_needed", "unknown"),
        (("Done",), "satisfied", "completion_satisfied", "unsatisfied"),
    ],
    ids=["without-completion-policy", "with-done-completion-policy"],
)
def test_sample_mixed_relationships_follow_explicit_completion_policy(
    completed_stage_names, completion_state, completion_reason, direct_state
) -> None:
    kwargs = {}
    if completed_stage_names is not None:
        kwargs["completed_stage_names"] = completed_stage_names
    value = json.loads(catalog.build_catalog(SAMPLE_ROOT, **kwargs))
    delivery = _entry_by_path(value, "Trail_Web/Queue/delivery")

    assert delivery["relationship"]["direct_prerequisite_state"] == direct_state
    assert delivery["relationship"]["prerequisites"] == [
        {
            "kind": "claim",
            "target_package_id": PREREQUISITE_ID,
            "claim_name": "implementation-ready",
            "observed_state": "unsatisfied",
            "observed_evidence_ref": None,
            "resolved_state": "unsatisfied",
            "reason": "claim_unsatisfied",
        },
        {
            "kind": "completion",
            "target_package_id": PREREQUISITE_ID,
            "observed_stage": "Done",
            "resolved_state": completion_state,
            "reason": completion_reason,
        },
    ]
