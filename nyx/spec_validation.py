"""Specification package validator.

Validates one specification, or one package folder, against the specification
format: required sections, the stage gates, the closure ledger and matrix,
verification authorities, task contracts and the package lifecycle folder
rules.  The functions that determine a package's stage folder
(``package_lifecycle``, ``validate_spec`` and ``validate_package``) take the
specification root explicitly and have no default root;
``validate_package_attachments`` takes the package folder, the already-read
specification lines and the lifecycle name.  The module reads no environment
variable, never writes, and depends only on the standard library and the two
sibling parsers.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from .spec_body import (
    ID_PATTERN,
    STABLE_ID_RANGE,
    Task,
    boundary_paths,
    boundary_rows,
    collect_definitions,
    criterion_contract_refs,
    explicit_authority_path,
    parse_tasks,
    section_body,
    source_path_citations,
    source_path_tokens,
    table_rows,
    task_block,
    transitive_referenced_ids,
    within,
)
from .spec_header import branch_name_is_lexical, metadata_value, metadata_values

LIFECYCLE_DIRECTORIES = {
    "Under_Development": "under_development",
    "Queue": "queue",
    "In_Progress": "in_progress",
    "Awaiting_Retrospective": "awaiting_retrospective",
    "Done": "done",
    "Needs_Fixes": "needs_fixes",
    "Archive": "archive",
    "Reference": "reference",
    ".pipeline": ".pipeline",
}


PERMANENT_FILES = {
    "spec.md",
    "review_report.md",
    "merge_summary.md",
    "materials_needed.md",
    "retrospective.md",
}


ATTACHMENTS_DIR = "attachments"


ATTACHMENTS_HEADING = "## Package attachments"


ATTACHMENT_ENTRY = re.compile(r"^- `([^`]+)`(?:\s+[—-]\s+(\S.*))?$")


SANITY_RECOMMENDATIONS = {
    "PROCEED_TO_DESIGN",
    "IMPLEMENT",
    "REDUCE",
    "REDESIGN",
    "DEFER",
    "REJECT",
}


HUMAN_SANITY_DECISIONS = {
    "PENDING",
    "AFFIRMED",
    "OVERRIDDEN",
    "DEFERRED",
    "REJECTED",
}


VERIFICATION_MATRIX_HEADER = (
    "| Lane | Authority | Command | Required stage | Runner | Sandbox policy | "
    "CI-only | Push required |"
)


SUPPORTED_CRITIQUE_CONTRACTS = {"2", "3"}


CLOSURE_TRACE_HEADER = (
    "| Criterion | Requirements and decisions | Task | "
    "Exact writable file-purpose ownership |"
)


CLOSURE_RECONCILIATION_HEADER = (
    "| Invariant class | Scope reconciled | Authority or evidence | Result |"
)


CLOSURE_PREFLIGHT_HEADER = "| Invariant class | Evidence references | Status |"


CLOSURE_INVARIANT_CLASSES = {
    "stable-id expansion and generated excerpt",
    "verification fidelity",
    "behavioral proof discrimination",
    "contract-domain coverage",
    "commit order",
}


BRANCH_GATED_LIFECYCLES = frozenset(
    {"queue", "in_progress", ".pipeline", "needs_fixes", "awaiting_retrospective"}
)


BRANCH_NOT_APPLICABLE = re.compile(r"^not applicable \u2014 \S")


def _branch_value_is_valid(value: str) -> bool:
    """Lexical subset of ``git check-ref-format --branch`` plus Markdown guards."""
    if BRANCH_NOT_APPLICABLE.match(value):
        return True
    if not value or value == "@":
        return False
    return branch_name_is_lexical(value)


def _validate_branch_header(lines: list[str], lifecycle: str) -> list[str]:
    errors: list[str] = []
    if metadata_values(lines, "Proposed branch"):
        errors.append(
            "retired header Proposed branch: is not accepted; use Branch:"
        )
    draft_approved = (
        lifecycle == "under_development"
        and (metadata_value(lines, "Closure") or "").lower() == "approved"
    )
    if lifecycle not in BRANCH_GATED_LIFECYCLES and not draft_approved:
        return errors
    branches = metadata_values(lines, "Branch")
    if not branches:
        errors.append(f"{lifecycle} package requires one Branch: header")
    elif len(branches) > 1:
        errors.append("duplicate Branch: header")
    elif not _branch_value_is_valid(branches[0]):
        errors.append(f"invalid Branch: value: {branches[0]}")
    return errors


def _validate_sanity_contract(lines: list[str], required: bool = False) -> list[str]:
    errors: list[str] = []
    labels = ("Sanity Recommendation", "Sanity Baseline", "Human Sanity Decision")
    values = {label: metadata_value(lines, label) for label in labels}
    present = {label for label, value in values.items() if value is not None}
    if not present:
        if required:
            errors.append(
                "a sanity-reviewed spec requires Sanity Recommendation, Sanity Baseline, "
                "and Human Sanity Decision"
            )
        return errors
    if len(present) != len(labels):
        missing = ", ".join(label for label in labels if label not in present)
        errors.append(f"incomplete sanity contract; missing: {missing}")
        return errors

    recommendation = values["Sanity Recommendation"]
    decision = values["Human Sanity Decision"]
    assert recommendation is not None and decision is not None
    if recommendation.upper() not in SANITY_RECOMMENDATIONS:
        errors.append(f"invalid Sanity Recommendation: {recommendation}")
    if decision.upper() not in HUMAN_SANITY_DECISIONS:
        errors.append(f"invalid Human Sanity Decision: {decision}")

    baseline = metadata_value(lines, "Baseline")
    sanity_baseline = values["Sanity Baseline"]
    if baseline and sanity_baseline and baseline != sanity_baseline:
        errors.append(
            f"Sanity Baseline must match Baseline; found {sanity_baseline} != {baseline}"
        )

    status = (metadata_value(lines, "Status") or "").lower()
    if decision.upper() == "PENDING" and status in {
        "approved",
        "queued",
        "ready",
        "ready_for_coding",
    }:
        errors.append(
            f"Human Sanity Decision cannot be PENDING when Status is {status}"
        )

    if VERIFICATION_MATRIX_HEADER not in lines:
        errors.append(
            "sanity-reviewed spec is missing the required verification matrix header"
        )
    return errors


def _find_cycle(tasks: dict[str, Task]) -> list[str] | None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str, path: list[str]) -> list[str] | None:
        if task_id in visiting:
            return path[path.index(task_id) :] + [task_id]
        if task_id in visited:
            return None
        visiting.add(task_id)
        for dependency in tasks[task_id].dependencies:
            if dependency in tasks:
                cycle = visit(dependency, path + [dependency])
                if cycle:
                    return cycle
        visiting.remove(task_id)
        visited.add(task_id)
        return None

    for task_id in tasks:
        cycle = visit(task_id, [task_id])
        if cycle:
            return cycle
    return None


def _validate_closure_ledger(lines: list[str], *, required: bool = False) -> list[str]:
    """Check recorded review transitions; evidence truth remains independently reviewed."""
    metadata = lines[
        : next(
            (i for i, line in enumerate(lines) if line.startswith("## ")), len(lines)
        )
    ]
    for label in ("Closure ledger", "Closure"):
        pattern = rf"^\s*\*{{0,2}}{re.escape(label)}\*{{0,2}}\s*:"
        if sum(bool(re.match(pattern, line, re.IGNORECASE)) for line in metadata) > 1:
            return [f"duplicate closure metadata: {label}"]
    version = metadata_value(lines, "Closure ledger")
    if version is None:
        return (
            ["Closure ledger: 1 is required; migrate retained history before closure"]
            if required
            else []
        )
    if version != "1":
        return [f"unsupported Closure ledger version: {version}"]
    body = "\n".join(section_body(lines, "## Closure review"))
    blocks = re.findall(r"^```json\s*\n(.*?)^```\s*$", body, re.MULTILINE | re.DOTALL)
    if lines.count("## Closure review") != 1 or len(blocks) != 1:
        return ["closure ledger requires one ## Closure review with one JSON block"]

    def unique_object(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        ledger = json.loads(blocks[0], object_pairs_hook=unique_object)
    except ValueError as error:
        return [f"invalid closure ledger JSON: {error}"]
    schemas = {
        "attempts": {
            "id": int,
            "mode": str,
            "round": int,
            "baseline": str,
            "spec_identity": str,
            "author": str,
            "reviewer": str,
            "result": str,
            "findings": list,
            "evidence": str,
        },
        "findings": {
            "id": str,
            "origin_attempt": int,
            "material": bool,
            "disposition": str,
            "rationale": str,
            "status": str,
            "verification_attempt": (int, type(None)),
            "evidence": str,
            "repair_evidence": str,
            "verification_evidence": str,
        },
        "resumptions": {
            "before_attempt": int,
            "repair_limit": int,
            "allow_broad_review": bool,
            "authority": str,
            "diagnosis": str,
        },
    }
    if not isinstance(ledger, dict) or set(ledger) != set(schemas):
        return ["closure ledger requires exactly attempts, findings, and resumptions"]
    errors: list[str] = []
    for name, schema in schemas.items():
        rows = ledger[name]
        if not isinstance(rows, list):
            errors.append(f"closure ledger {name} must be a list")
            continue
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict) or set(row) != set(schema):
                errors.append(f"closure ledger {name} row {index} has incorrect fields")
                continue
            for key, expected in schema.items():
                allowed = expected if isinstance(expected, tuple) else (expected,)
                if type(row[key]) not in allowed:
                    errors.append(
                        f"closure ledger {name} row {index}: invalid {key} type"
                    )
    if errors:
        return errors

    attempts = ledger["attempts"]
    findings = ledger["findings"]
    resumptions = ledger["resumptions"]
    by_attempt = {row["id"]: row for row in attempts}
    by_finding = {row["id"]: row for row in findings}
    by_resumption = {row["before_attempt"]: row for row in resumptions}
    if [row["id"] for row in attempts] != list(range(1, len(attempts) + 1)):
        errors.append(
            "closure attempt IDs must be consecutive from 1; retain interrupted attempts"
        )
    if len(by_finding) != len(findings):
        errors.append("duplicate closure finding ID")
    if len(by_resumption) != len(resumptions):
        errors.append("duplicate closure resumption before_attempt")
    for row in resumptions:
        if row["before_attempt"] not in by_attempt:
            errors.append(
                "closure resumption must reference an existing attempt reservation"
            )
        if (
            row["repair_limit"] < 2
            or not row["authority"].strip()
            or not row["diagnosis"].strip()
        ):
            errors.append(
                "closure resumption requires authority, diagnosis, and repair_limit >= 2"
            )

    round_number = 0
    repair_limit = 2
    reviewers: set[str] = set()
    broad_seen = False
    last_broad_result = None
    for index, row in enumerate(attempts):
        label = f"closure attempt {row['id']}"
        resumption = by_resumption.get(row["id"])
        if resumption:
            if resumption["repair_limit"] < repair_limit:
                errors.append(
                    f"{label}: resumption cannot reduce or reset consumed budget"
                )
            repair_limit = max(repair_limit, resumption["repair_limit"])
        if (
            index
            and attempts[index - 1]["result"] in {"IN_PROGRESS", "INTERRUPTED"}
            and not resumption
        ):
            errors.append(
                f"{label}: unfinished predecessor requires recovered same-attempt work or human resumption"
            )
        if row["mode"] == "BROAD_REVIEW":
            if row["round"] != 0:
                errors.append(f"{label}: broad review must use round 0")
            if broad_seen and not (resumption and resumption["allow_broad_review"]):
                errors.append(
                    f"{label}: repeated broad review requires explicit human scope"
                )
            broad_seen = True
            last_broad_result = row["result"]
        elif row["mode"] == "REPAIR_VERIFICATION":
            round_number += 1
            if not broad_seen or row["round"] != round_number:
                errors.append(
                    f"{label}: repair rounds must follow broad review and remain consecutive"
                )
            if round_number > repair_limit:
                errors.append(
                    f"{label}: repair round exceeds explicitly authorized budget"
                )
        else:
            errors.append(f"{label}: invalid mode")
        if row["result"] not in {"IN_PROGRESS", "INTERRUPTED", "PASS", "FAIL"}:
            errors.append(f"{label}: invalid result")
        for name in ("baseline", "spec_identity", "author", "evidence"):
            if not row[name].strip():
                errors.append(f"{label}: missing {name}")
        reviewer = row["reviewer"].strip()
        if reviewer:
            if reviewer == row["author"].strip() or reviewer in reviewers:
                errors.append(
                    f"{label}: reviewer must be independent of author and prior reviewers"
                )
            reviewers.add(reviewer)
        elif row["result"] in {"PASS", "FAIL"}:
            errors.append(f"{label}: terminal review requires reviewer identity")
        if any(type(item) is not str for item in row["findings"]):
            errors.append(f"{label}: findings must contain string IDs")
        elif (
            len(set(row["findings"])) != len(row["findings"])
            or set(row["findings"]) - by_finding.keys()
        ):
            errors.append(f"{label}: duplicate or undefined finding references")

    approved = (metadata_value(lines, "Closure") or "").lower() == "approved"
    for row in findings:
        label = f"closure finding {row['id']}"
        if not re.fullmatch(r"CL-\d+", row["id"]):
            errors.append(f"{label}: expected stable CL-number ID")
        origin = by_attempt.get(row["origin_attempt"])
        if origin is None or row["id"] not in origin["findings"]:
            errors.append(f"{label}: origin attempt must reference this finding")
        if not row["evidence"].strip() or not row["rationale"].strip():
            errors.append(
                f"{label}: counterexample evidence and disposition rationale are required"
            )
        disposition = row["disposition"]
        status = row["status"]
        if disposition not in {"ACCEPT", "REJECT", "DEFER", "NEEDS_USER_DECISION"}:
            errors.append(f"{label}: invalid disposition")
        if status not in {"OPEN", "RESOLVED", "CLOSED"}:
            errors.append(f"{label}: invalid status")
        if disposition == "DEFER" and row["material"]:
            errors.append(f"{label}: material defects cannot be deferred for closure")
        if disposition in {"REJECT", "DEFER"} and status != "CLOSED":
            errors.append(
                f"{label}: rejected/deferred findings must be CLOSED with rationale"
            )
        if disposition == "NEEDS_USER_DECISION" and status != "OPEN":
            errors.append(f"{label}: pending user decisions must remain OPEN")
        if disposition == "ACCEPT":
            if status == "CLOSED":
                errors.append(
                    f"{label}: accepted findings require RESOLVED independent verification"
                )
            if status == "RESOLVED":
                verification = by_attempt.get(row["verification_attempt"])
                if (
                    verification is None
                    or verification["id"] <= row["origin_attempt"]
                    or verification["mode"] != "REPAIR_VERIFICATION"
                    or verification["result"] not in {"PASS", "FAIL"}
                    or row["id"] not in verification["findings"]
                    or not row["repair_evidence"].strip()
                    or not row["verification_evidence"].strip()
                ):
                    errors.append(
                        f"{label}: resolution needs later independent verification covering the finding and repair evidence"
                    )
        if (
            row["verification_attempt"] is not None
            and row["verification_attempt"] not in by_attempt
        ):
            errors.append(f"{label}: undefined verification attempt")
        if approved and (status == "OPEN" or disposition == "NEEDS_USER_DECISION"):
            errors.append(f"{label}: unresolved finding prevents approved closure")
    if approved:
        if not attempts or not broad_seen:
            errors.append("approved closure requires a recorded broad review")
        elif last_broad_result not in {"PASS", "FAIL"}:
            errors.append(
                "approved closure requires a terminal broad review, not an interrupted discovery"
            )
        elif attempts[-1]["result"] not in {"PASS", "FAIL"}:
            errors.append("approved closure requires a terminal final review")
        if any(row["result"] == "FAIL" and not row["findings"] for row in attempts):
            errors.append(
                "failed closure reviews require findings and dispositions before approval"
            )
    return errors


def package_lifecycle(
    spec_path: Path,
    spec_root: Path,
    non_package_folders: frozenset[str] = frozenset(),
) -> str:
    """Resolve the stage above optional grouping directories, preserving legacy reads."""
    try:
        relative = spec_path.resolve().relative_to(spec_root.resolve())
    except ValueError:
        for parent in spec_path.resolve().parents:
            if parent.name in LIFECYCLE_DIRECTORIES.values():
                return parent.name
        return ""
    if len(relative.parts) < 4 or relative.parts[0] in non_package_folders:
        raise ValueError(
            "package must be under <spec-root>/<repository>/<stage>/<feature>/"
        )
    directory = relative.parts[1]
    if directory not in LIFECYCLE_DIRECTORIES:
        raise ValueError(f"unknown package stage directory: {directory}")
    return LIFECYCLE_DIRECTORIES[directory]


def validate_spec(
    spec_path: Path,
    spec_root: Path,
    non_package_folders: frozenset[str] = frozenset(),
) -> list[str]:
    """Validate one specification from its current on-disk text."""
    return _validate_spec_lines(
        spec_path,
        spec_path.read_text(encoding="utf-8").splitlines(),
        spec_root,
        non_package_folders,
    )


def _validate_spec_lines(
    spec_path: Path,
    lines: list[str],
    spec_root: Path,
    non_package_folders: frozenset[str] = frozenset(),
) -> list[str]:
    """Validate already-read specification lines through the shared owner."""
    errors: list[str] = []
    text = "\n".join(lines)

    required_headers = (
        "## Objective",
        "## Requirements",
        "## Anti-requirements",
        "## Verified current behavior",
        "## Decisions",
        "## File boundaries",
        "## Acceptance criteria",
        "## Tasks",
        "## Verification",
        "## Documentation and human materials",
    )
    for header in required_headers:
        if header not in lines:
            errors.append(f"missing required section: {header}")

    critique_contract = metadata_value(lines, "Critique contract")
    if (
        critique_contract is not None
        and critique_contract not in SUPPORTED_CRITIQUE_CONTRACTS
    ):
        errors.append(f"unsupported Critique contract marker: {critique_contract}")
    if critique_contract == "2" and "## Closure matrix" not in lines:
        errors.append(
            "Critique contract 2 spec is missing required section: ## Closure matrix"
        )
    if critique_contract != "2" and "## Closure preflight" not in lines:
        errors.append("missing required section: ## Closure preflight")

    errors.extend(_validate_sanity_contract(lines, required=critique_contract != "2"))

    try:
        lifecycle = package_lifecycle(spec_path, spec_root, non_package_folders)
    except ValueError as error:
        errors.append(str(error))
        lifecycle = ""
    ledger_required = lifecycle in {
        "queue",
        "in_progress",
        "awaiting_retrospective",
    } or (
        critique_contract != "2"
        and lifecycle != "done"
        and (metadata_value(lines, "Closure") or "").lower() == "approved"
    )
    errors.extend(_validate_closure_ledger(lines, required=ledger_required))
    errors.extend(_validate_branch_header(lines, lifecycle))

    definitions: list[str] = []
    for line in lines:
        definition = re.match(
            r"^(?:- (?:\[ \] )?`((?:R|AR|F|D|AC)-\d+)`|### (T\d+)\b)",
            line,
        )
        if definition:
            definitions.append(definition.group(1) or definition.group(2))
    duplicates = sorted(
        item for item, count in Counter(definitions).items() if count > 1
    )
    if duplicates:
        errors.append(f"duplicate ID definitions: {', '.join(duplicates)}")

    criteria = set(re.findall(r"^- \[ \] `(AC-\d+)`", text, re.MULTILINE))
    tasks = parse_tasks(lines)
    if not tasks:
        errors.append("no task definitions found")

    owners: Counter[str] = Counter()
    for task in tasks.values():
        unknown_dependencies = sorted(task.dependencies - tasks.keys())
        if unknown_dependencies:
            errors.append(
                f"{task.task_id} has unknown dependencies: "
                f"{', '.join(unknown_dependencies)}"
            )
        unknown_criteria = sorted(task.owns - criteria)
        if unknown_criteria:
            errors.append(
                f"{task.task_id} owns unknown criteria: {', '.join(unknown_criteria)}"
            )
        owners.update(task.owns)
        if not task.owns:
            errors.append(f"{task.task_id} owns no acceptance criteria")
        if not task.boundaries:
            errors.append(f"{task.task_id} has no parseable boundaries")

    for criterion in sorted(criteria):
        count = owners[criterion]
        if count != 1:
            errors.append(
                f"{criterion} must have exactly one primary task owner; found {count}"
            )

    if cycle := _find_cycle(tasks):
        errors.append(f"task dependency cycle: {' -> '.join(cycle)}")

    definitions_by_id = collect_definitions(lines)
    if critique_contract == "2":
        errors.extend(
            _validate_critique_contract_two(
                lines,
                tasks,
                criteria,
                definitions_by_id,
            )
        )
    elif critique_contract is None or critique_contract == "3":
        errors.extend(
            _validate_critique_contract_three(
                lines,
                tasks,
                criteria,
                definitions_by_id,
            )
        )

    return errors


def validate_package(
    package: Path,
    spec_root: Path,
    non_package_folders: frozenset[str] = frozenset(),
    *,
    spec_lines: list[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    spec_path = package if package.is_file() else package / "spec.md"
    if not spec_path.exists():
        return [f"missing spec.md: {spec_path}"]
    if spec_lines is None:
        errors.extend(validate_spec(spec_path, spec_root, non_package_folders))
        lines = spec_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = spec_lines
        errors.extend(
            _validate_spec_lines(spec_path, lines, spec_root, non_package_folders)
        )
    contract = metadata_value(lines, "Critique contract")
    try:
        lifecycle = package_lifecycle(spec_path, spec_root, non_package_folders)
    except ValueError:
        return errors
    if lifecycle in {"queue", "in_progress", "awaiting_retrospective"} or (
        lifecycle == "done" and (contract != "2" or within(spec_path, spec_root))
    ):
        closure = (metadata_value(lines, "Closure") or "").lower()
        if closure != "approved":
            errors.append(f"{lifecycle} package requires Closure: approved")
        sanity_decision = (
            metadata_value(lines, "Human Sanity Decision") or ""
        ).upper()
        if sanity_decision not in {"AFFIRMED", "OVERRIDDEN"}:
            errors.append(
                f"{lifecycle} package requires an affirmative human sanity decision"
            )
        recommendation = (metadata_value(lines, "Sanity Recommendation") or "").upper()
        if sanity_decision == "AFFIRMED" and recommendation not in {
            "PROCEED_TO_DESIGN",
            "IMPLEMENT",
            "REDUCE",
        }:
            errors.append(
                f"{lifecycle} package cannot affirm and queue "
                f"Sanity Recommendation: {recommendation or 'MISSING'}"
            )

    retrospective_path = spec_path.parent / "retrospective.md"

    if lifecycle in {"awaiting_retrospective", "done"}:
        extras = sorted(
            path.name
            for path in spec_path.parent.iterdir()
            if path.name not in PERMANENT_FILES
            and not (path.name == ATTACHMENTS_DIR and path.is_dir())
        )
        if extras:
            errors.append(
                f"{lifecycle} package contains non-permanent artifacts: {', '.join(extras)}"
            )
        errors.extend(validate_package_attachments(spec_path.parent, lines, lifecycle))
    if within(spec_path, spec_root) and lifecycle in {
        "awaiting_retrospective",
        "done",
    }:
        for name in ("review_report.md", "merge_summary.md"):
            if not (spec_path.parent / name).is_file():
                errors.append(f"{lifecycle} package is missing {name}")
        if lifecycle == "done":
            if not retrospective_path.is_file():
                errors.append("Done package requires a READY retrospective.md")
            elif (
                metadata_value(
                    retrospective_path.read_text(encoding="utf-8").splitlines(),
                    "Status",
                )
                != "READY"
            ):
                errors.append("Done package requires retrospective Status: READY")
    return errors


def validate_package_attachments(
    package: Path, lines: list[str], lifecycle: str
) -> list[str]:
    """Require the package's attachments folder to match its declared manifest.

    Each manifest entry is ``- `attachments/<path>` — <purpose>`` under the
    optional ``## Package attachments`` section of ``spec.md``.
    """
    errors: list[str] = []
    declared: set[str] = set()
    for line in section_body(lines, ATTACHMENTS_HEADING):
        match = ATTACHMENT_ENTRY.match(line.strip())
        if not match or not match.group(1).startswith(f"{ATTACHMENTS_DIR}/"):
            continue
        entry, purpose = match.group(1), match.group(2)
        parts = Path(entry).parts
        if ".." in parts or Path(entry).is_absolute() or len(parts) < 2:
            errors.append(f"{lifecycle} package attachment path is invalid: {entry}")
            continue
        if not purpose:
            errors.append(f"{lifecycle} package attachment has no purpose: {entry}")
        declared.add(Path(entry).as_posix())
    root = package / ATTACHMENTS_DIR
    present = (
        {
            path.relative_to(package).as_posix()
            for path in root.rglob("*")
            if path.is_file() or path.is_symlink()
        }
        if root.is_dir()
        else set()
    )
    undeclared = sorted(present - declared)
    if undeclared:
        errors.append(
            f"{lifecycle} package contains undeclared attachments: "
            f"{', '.join(undeclared)}"
        )
    missing = sorted(declared - present)
    if missing:
        errors.append(
            f"{lifecycle} package declares missing attachments: {', '.join(missing)}"
        )
    return errors


def _validate_verification_authorities(
    lines: list[str],
    definitions: dict[str, list[str]],
    require_existing_paths: bool = False,
) -> list[str]:
    errors: list[str] = []
    section = section_body(lines, "## Verification")
    if VERIFICATION_MATRIX_HEADER not in section:
        return errors
    rows = table_rows(section, VERIFICATION_MATRIX_HEADER)
    if not rows:
        return ["verification matrix has no rows"]
    target_value = metadata_value(lines, "Target repo")
    target = Path(target_value) if target_value else None
    for row in rows:
        if len(row) != 8:
            errors.append(
                f"verification matrix row must have exactly 8 cells; found {len(row)}"
            )
            continue
        lane, authority, command, _stage, _runner, _sandbox, ci_only, push = row
        if any(not cell.strip() for cell in row):
            errors.append(f"verification lane {lane!r} has an empty required field")
        if not re.search(r"`[^`]+`", command):
            errors.append(
                f"verification lane {lane!r} command must be exact and backticked"
            )
        if ci_only.lower() not in {"yes", "no"}:
            errors.append(f"verification lane {lane!r} CI-only must be yes or no")
        if push.lower() not in {"yes", "no"}:
            errors.append(f"verification lane {lane!r} Push required must be yes or no")
        tokens = re.findall(r"`([^`]+)`", authority)
        residue = re.sub(r"`[^`]+`", "", authority)
        if not tokens or re.sub(r"[\s,;]", "", residue):
            errors.append(
                f"verification lane {lane!r} has free-form authority; cite only "
                "defined stable IDs or explicit source paths"
            )
            continue
        for token in tokens:
            if re.fullmatch(r"(?:AR|R|F|D|AC)-\d+", token):
                if token not in definitions:
                    errors.append(
                        f"verification lane {lane!r} cites undefined authority {token}"
                    )
            elif not explicit_authority_path(token):
                errors.append(
                    f"verification lane {lane!r} has undefined authority {token!r}"
                )
            elif require_existing_paths:
                source, start, end = source_path_citations(f"`{token}`")[0]
                authority_path = Path(source)
                if not authority_path.is_absolute():
                    authority_path = (
                        (target / authority_path) if target else authority_path
                    )
                if not authority_path.is_file():
                    errors.append(
                        f"verification lane {lane!r} cites missing authority file: "
                        f"{authority_path}"
                    )
                elif start is not None and end is not None:
                    line_count = len(
                        authority_path.read_text(encoding="utf-8").splitlines()
                    )
                    if start < 1 or end < start or end > line_count:
                        errors.append(
                            f"verification lane {lane!r} authority range is outside file: "
                            f"{authority_path}:{start}-{end}"
                        )
    return errors


def _validate_closure_matrix(
    lines: list[str],
    tasks: dict[str, Task],
    criteria: set[str],
    definitions: dict[str, list[str]],
) -> list[str]:
    errors: list[str] = []
    section = section_body(lines, "## Closure matrix")
    if not section:
        return ["closure matrix is empty"]

    trace_rows = table_rows(section, CLOSURE_TRACE_HEADER)
    if not trace_rows:
        errors.append("closure matrix is missing traceability table header")
    reconciliation_rows = table_rows(section, CLOSURE_RECONCILIATION_HEADER)
    if not reconciliation_rows:
        errors.append("closure matrix is missing cross-contract table header")

    writable, _purposes, boundary_errors = boundary_paths(lines, tasks, "2")
    errors.extend(boundary_errors)
    mapped_criteria: Counter[str] = Counter()
    mapped_paths: dict[str, set[str]] = {}
    known_ids = set(definitions) | set(tasks)

    for row in trace_rows:
        if len(row) != 4:
            errors.append("malformed closure traceability row")
            continue
        criterion_ids = [
            item for item in ID_PATTERN.findall(row[0]) if item.startswith("AC-")
        ]
        requirement_ids = [
            item
            for item in ID_PATTERN.findall(row[1])
            if item.startswith(("R-", "AR-"))
        ]
        decision_ids = [
            item for item in ID_PATTERN.findall(row[1]) if item.startswith("D-")
        ]
        task_ids = [item for item in ID_PATTERN.findall(row[2]) if item.startswith("T")]
        referenced = set(ID_PATTERN.findall(" ".join(row)))
        unknown = sorted(referenced - known_ids)
        if unknown:
            errors.append(
                "closure traceability row cites undefined IDs: " + ", ".join(unknown)
            )
        if len(criterion_ids) != 1:
            errors.append(
                "each closure traceability row must name exactly one criterion"
            )
            continue
        criterion = criterion_ids[0]
        mapped_criteria[criterion] += 1
        if not requirement_ids or not decision_ids:
            errors.append(
                f"closure traceability row for {criterion} must name a requirement or "
                "anti-requirement and a decision"
            )
        if len(task_ids) != 1:
            errors.append(
                f"closure traceability row for {criterion} must name exactly one task"
            )
            continue
        task_id = task_ids[0]
        if task_id in tasks and criterion not in tasks[task_id].owns:
            errors.append(
                f"closure traceability maps {criterion} to non-owner {task_id}"
            )
        paths = source_path_tokens(row[3])
        purpose = re.sub(r"`[^`]+`", "", row[3])
        if not paths or not re.search(r"[A-Za-z]", purpose):
            errors.append(
                f"closure traceability row for {criterion} needs exact paths and purposes"
            )
        invalid_paths = sorted(paths - writable.get(task_id, set()))
        if invalid_paths:
            errors.append(
                f"closure traceability row for {criterion} names non-writable or "
                f"unbounded paths for {task_id}: {', '.join(invalid_paths)}"
            )
        mapped_paths.setdefault(task_id, set()).update(paths)

    for criterion in sorted(criteria):
        if mapped_criteria[criterion] != 1:
            errors.append(
                f"closure matrix must map {criterion} exactly once; found "
                f"{mapped_criteria[criterion]}"
            )
    for task_id, paths in writable.items():
        missing = sorted(paths - mapped_paths.get(task_id, set()))
        if missing:
            errors.append(
                f"closure matrix omits writable file-purpose ownership for {task_id}: "
                + ", ".join(missing)
            )

    seen_classes: Counter[str] = Counter()
    for row in reconciliation_rows:
        if len(row) != 4:
            errors.append("malformed closure cross-contract row")
            continue
        invariant = row[0].strip("` ").lower()
        seen_classes[invariant] += 1
        if not row[1].strip() or not row[2].strip():
            errors.append(f"closure invariant {row[0]!r} has empty scope or evidence")
        if row[3].strip().upper() != "RECONCILED":
            errors.append(f"closure invariant {row[0]!r} is not RECONCILED")
    for invariant in sorted(CLOSURE_INVARIANT_CLASSES):
        if seen_classes[invariant] != 1:
            errors.append(
                f"closure matrix must contain one {invariant!r} row; found "
                f"{seen_classes[invariant]}"
            )
    unknown_classes = sorted(set(seen_classes) - CLOSURE_INVARIANT_CLASSES)
    if unknown_classes:
        errors.append(
            "closure matrix has unsupported invariant classes: "
            + ", ".join(unknown_classes)
        )
    return errors


def _validate_task_excerpt_closure(
    spec_lines: list[str],
    tasks: dict[str, Task],
    definitions: dict[str, list[str]],
) -> list[str]:
    errors: list[str] = []
    verification = section_body(spec_lines, "## Verification")
    for task_id, task in tasks.items():
        block = task_block(spec_lines, task_id)
        owned_blocks = [
            definitions[item] for item in sorted(task.owns) if item in definitions
        ]
        boundaries = boundary_rows(spec_lines, task_id)
        scope = "\n".join(
            block
            + [line for item in owned_blocks for line in item]
            + boundaries
            + verification
        )
        referenced = transitive_referenced_ids(scope, definitions, task.owns)
        unresolved = [item for item in referenced if item not in definitions]
        if unresolved:
            errors.append(
                f"{task_id} excerpt cites undefined IDs: {', '.join(unresolved)}"
            )
    return errors


def _validate_critique_contract_two(
    lines: list[str],
    tasks: dict[str, Task],
    criteria: set[str],
    definitions: dict[str, list[str]],
) -> list[str]:
    errors: list[str] = []
    text = "\n".join(lines)
    ranges = [match.group(0) for match in STABLE_ID_RANGE.finditer(text)]
    if ranges:
        errors.append(
            "explicit stable-ID expansion is required; ranged "
            "references found: " + ", ".join(ranges)
        )
    errors.extend(_validate_verification_authorities(lines, definitions))
    errors.extend(_validate_closure_matrix(lines, tasks, criteria, definitions))
    errors.extend(_validate_task_excerpt_closure(lines, tasks, definitions))
    return errors


def _validate_closure_preflight(lines: list[str]) -> list[str]:
    section = section_body(lines, "## Closure preflight")
    rows = table_rows(section, CLOSURE_PREFLIGHT_HEADER)
    if not rows:
        return ["closure preflight is missing its required table header"]
    errors: list[str] = []
    seen: Counter[str] = Counter()
    for row in rows:
        if len(row) != 3:
            errors.append("malformed closure preflight row")
            continue
        invariant = row[0].strip("` ").lower()
        seen[invariant] += 1
        if not row[1].strip() or row[1].strip().lower() in {"none", "n/a", "tbd"}:
            errors.append(f"closure invariant {row[0]!r} lacks evidence references")
        if row[2].strip().upper() != "RECONCILED":
            errors.append(f"closure invariant {row[0]!r} is not RECONCILED")
    for invariant in sorted(CLOSURE_INVARIANT_CLASSES):
        if seen[invariant] != 1:
            errors.append(
                f"closure preflight must contain one {invariant!r} row; found "
                f"{seen[invariant]}"
            )
    unknown = sorted(set(seen) - CLOSURE_INVARIANT_CLASSES)
    if unknown:
        errors.append(
            "closure preflight has unsupported invariant classes: " + ", ".join(unknown)
        )
    return errors


def _validate_generated_traceability(
    lines: list[str],
    tasks: dict[str, Task],
    criteria: set[str],
    definitions: dict[str, list[str]],
) -> list[str]:
    errors: list[str] = []
    writable, purposes, boundary_errors = boundary_paths(lines, tasks, "3")
    errors.extend(boundary_errors)
    owners = {
        criterion: task.task_id for task in tasks.values() for criterion in task.owns
    }
    mapped: Counter[str] = Counter()
    for criterion in sorted(criteria):
        requirements, decisions = criterion_contract_refs(criterion, definitions)
        if not requirements or not decisions:
            errors.append(
                f"{criterion} must cite at least one R-/AR- requirement and one D- decision"
            )
        owner = owners.get(criterion)
        if owner is None:
            continue
        for path in sorted(writable.get(owner, set())):
            purpose = purposes.get((owner, path), "")
            cited = {
                item for item in ID_PATTERN.findall(purpose) if item.startswith("AC-")
            }
            foreign = sorted(cited - tasks[owner].owns)
            if foreign:
                errors.append(
                    f"writable boundary {path} for {owner} cites criteria not owned by "
                    f"that task: {', '.join(foreign)}"
                )
            if not cited:
                errors.append(
                    f"writable boundary {path} for {owner} purpose must cite an owned AC ID"
                )
            if criterion in cited:
                mapped[criterion] += 1
    for criterion in sorted(criteria):
        if mapped[criterion] == 0:
            errors.append(
                f"{criterion} is not mapped by any writable file-boundary purpose"
            )
    return errors


def _validate_critique_contract_three(
    lines: list[str],
    tasks: dict[str, Task],
    criteria: set[str],
    definitions: dict[str, list[str]],
) -> list[str]:
    errors: list[str] = []
    text = "\n".join(lines)
    ranges = [match.group(0) for match in STABLE_ID_RANGE.finditer(text)]
    if ranges:
        errors.append(
            "explicit stable-ID expansion is required; ranged "
            "references found: " + ", ".join(ranges)
        )
    if "## Closure matrix" in lines:
        errors.append("traceability is generated; remove stored ## Closure matrix")
    errors.extend(
        _validate_verification_authorities(
            lines, definitions, require_existing_paths=True
        )
    )
    errors.extend(_validate_generated_traceability(lines, tasks, criteria, definitions))
    errors.extend(_validate_closure_preflight(lines))
    errors.extend(_validate_task_excerpt_closure(lines, tasks, definitions))
    return errors
