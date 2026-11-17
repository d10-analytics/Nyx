"""Shared fixtures for the specification validator tests.

``VALIDATOR`` forwards every attribute to ``nyx.spec_validation`` and, where a
call to ``validate_spec``, ``validate_package`` or ``package_lifecycle`` omits
the specification root, supplies one fixed path that no fixture lies under and
that is never created, so those tests classify packages by their parent folder
names.
"""
from __future__ import annotations

import json
from pathlib import Path

from nyx import spec_validation

VALID_SPEC = """# Example

Critique contract: 2
Status: approved
Branch: spec/example
Target repo: /tmp/repo
Baseline: abc123
Sanity Recommendation: IMPLEMENT
Sanity Baseline: abc123
Human Sanity Decision: AFFIRMED
Closure: approved

## Objective
Example.
## Requirements
- `R-01` — Do the thing.
## Anti-requirements
- `AR-01` — Do not do another thing.
## Verified current behavior
- `F-01` — Current fact at `src/a.py:1`.
## Decisions
- `D-01` — Use the existing owner.
## File boundaries
| Task | File | Access and purpose |
| --- | --- | --- |
| T01 | `src/a.py` | Write: existing behavior owner for `AC-01` |
| T01 | `tests/test_a.py` | Write: natural trigger proof for `AC-01` |
## Acceptance criteria
- [ ] `AC-01` — The thing is observable through the real trigger and returns the exact result.
## Tasks
### T01 — Do the thing
- Objective: Do the thing.
- Owns: `AC-01`.
- Depends on: none.
- Boundaries: `src/a.py`, `tests/test_a.py`.
- Risk: standard.
- Implementation: Extend the existing owner.
- Behavioral proof: Assert the exact result.
## Closure matrix
| Criterion | Requirements and decisions | Task | Exact writable file-purpose ownership |
| --- | --- | --- | --- |
| `AC-01` | `R-01`, `D-01` | `T01` | `src/a.py`: `ExistingOwner.apply` implementation; `tests/test_a.py`: natural trigger proof |
| Invariant class | Scope reconciled | Authority or evidence | Result |
| --- | --- | --- | --- |
| Stable-ID expansion and generated excerpt | T01 and its excerpt | Explicit IDs and generated output | RECONCILED |
| Verification fidelity | Full suite lane | `AGENTS.md` | RECONCILED |
| Behavioral proof discrimination | AC-01 trigger and owner | AC-01 assertion | RECONCILED |
| Contract-domain coverage | Applicable positive and negative cases | R-01 and AR-01 | RECONCILED |
| Commit order | Production-first and test-first | T01 decomposition | RECONCILED |
## Verification
| Lane | Authority | Command | Required stage | Runner | Sandbox policy | CI-only | Push required |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Full suite | `AGENTS.md` | `pytest` | pre-queue | capable-host local | skip known-incompatible sandbox | no | no |
- Focused checks: test A.
## Documentation and human materials
- Documentation impact: none.
"""


_V2_CLOSURE_START = VALID_SPEC.index("## Closure matrix")


_V2_CLOSURE_END = VALID_SPEC.index("## Verification")


VALID_SPEC_V3 = (
    (
        VALID_SPEC[:_V2_CLOSURE_START]
        + """## Closure preflight
| Invariant class | Evidence references | Status |
| --- | --- | --- |
| Stable-ID expansion and generated excerpt | `T01`, `AC-01` | RECONCILED |
| Verification fidelity | verification row and `AGENTS.md` | RECONCILED |
| Behavioral proof discrimination | `AC-01`, `T01` proof | RECONCILED |
| Contract-domain coverage | `R-01`, `AR-01`, `AC-01` | RECONCILED |
| Commit order | `T01` dependency and boundaries | RECONCILED |
"""
        + VALID_SPEC[_V2_CLOSURE_END:]
    )
    .replace("Critique contract: 2", "Critique contract: 3")
    .replace(
        "- [ ] `AC-01` — The thing is observable through the real trigger and returns the exact result.",
        "- [ ] `AC-01` — Under `R-01` and `D-01`, the thing is observable through the real trigger and returns the exact result.",
    )
)


CLEAN_LEDGER = {
    "attempts": [
        {
            "id": 1,
            "mode": "BROAD_REVIEW",
            "round": 0,
            "baseline": "abc123",
            "spec_identity": "sha256:reviewed-input",
            "author": "planner-1",
            "reviewer": "closer-1",
            "result": "PASS",
            "findings": [],
            "evidence": "Complete manifest reviewed; no findings.",
        }
    ],
    "findings": [],
    "resumptions": [],
}


def with_ledger(content: str, ledger: dict) -> str:
    return content.replace(
        "Closure: approved", "Closure ledger: 1\nClosure: approved"
    ) + ("\n## Closure review\n\n```json\n" + json.dumps(ledger, indent=2) + "\n```\n")


VALID_SPEC_V3 = with_ledger(VALID_SPEC_V3, CLEAN_LEDGER)


_NEVER_CREATED_ROOT = Path("/nonexistent-spec-root")
_ROOT_FUNCTIONS = ("validate_spec", "validate_package", "package_lifecycle")


class _Validator:
    def __getattr__(self, name):
        target = getattr(spec_validation, name)
        if name not in _ROOT_FUNCTIONS:
            return target

        def call(path, *args, **kwargs):
            if not args and "spec_root" not in kwargs:
                kwargs["spec_root"] = _NEVER_CREATED_ROOT
            return target(path, *args, **kwargs)

        return call


VALIDATOR = _Validator()
