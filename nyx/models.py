"""Typed validation for the producer's catalog protocol.

The tracker consumes only the fields it renders: package identity, lifecycle,
declared values, diagnostics, program membership, and direct prerequisites.
Every other producer field is accepted as part of the fixed shape and then
discarded, so the wire contract stays strict without carrying unused concepts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

SCHEMA_VERSION = 8

LIFECYCLES = frozenset(
    {
        "under_development",
        "queue",
        "in_progress",
        "needs_fixes",
        "awaiting_retrospective",
        "done",
        "archive",
    }
)

DECLARED_FIELDS = ("title", "target_project", "status")
REPORTED_FIELD_KEYS = frozenset({"name", "value"})
MAX_REPORTED_FIELDS = 64
# Both Python and the browser cap one piece of text at this many UTF-16 code
# units; every text bound below measures with the shared counter.
MAX_TEXT_UNITS = 1024

DIAGNOSTIC_CODES = frozenset(
    {
        "invalid_package",
        "unreadable_anchor",
        "nonregular_anchor",
        "changed_during_read",
        "discovery_unavailable",
        "duplicate_program_id",
    }
)

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "catalog_digest",
        "configuration_revision",
        "inventory",
        "terminal_stages",
        "identity_coverage",
        "program_coverage",
        "discovery_diagnostics",
        "entries",
        "programs",
    }
)
_ENTRY_KEYS = frozenset(
    {"package_id", "package_path", "project", "stage", "declared", "reported_fields", "relationship"}
)
_RELATIONSHIP_KEYS = frozenset(
    {
        "participation",
        "claims",
        "prerequisites",
        "direct_prerequisite_state",
        "program",
        "superseded_by",
    }
)
_EDGE_KEYS = frozenset(
    {
        "kind",
        "target_package_id",
        "claim_name",
        "observed_state",
        "observed_evidence_ref",
        "resolved_state",
        "reason",
    }
)
_CLAIM_EDGE_KEYS = frozenset(
    {
        "kind",
        "target_package_id",
        "claim_name",
        "observed_state",
        "observed_evidence_ref",
        "resolved_state",
        "reason",
    }
)
_COMPLETION_EDGE_KEYS = frozenset(
    {
        "kind",
        "target_package_id",
        "observed_stage",
        "resolved_state",
        "reason",
    }
)
_PROGRAM_KEYS = frozenset({"program_id", "title", "resolution"})
_COVERAGE_KEYS = frozenset({"state", "diagnostics"})
_INVENTORY_KEYS = frozenset({"projects", "stages"})
_INVENTORY_PROJECT_KEYS = frozenset({"name", "availability"})
_INVENTORY_STAGE_KEYS = frozenset({"project", "stage", "availability"})
_CLAIM_KEYS = frozenset({"name", "state", "evidence_ref"})
_SUCCESSOR_KEYS = frozenset({"package_id", "resolution"})
_CATALOG_PROGRAM_KEYS = frozenset({"program_id", "title", "member_package_ids"})
_DIAGNOSTIC_KEYS = frozenset({"code", "message"})
_COVERAGE_STATES = frozenset({"complete", "incomplete"})
_OBSERVED_STATES = frozenset({"satisfied", "unsatisfied", "unknown"})
_DIRECT_PREREQUISITE_STATES = _OBSERVED_STATES | {
    "no_declared_prerequisites", "relationship_unavailable"
}
_EDGE_REASONS = frozenset(
    {
        "claim_satisfied",
        "claim_unsatisfied",
        "claim_unknown",
        "missing_claim",
        "invalid_claim",
        "missing_target",
        "duplicate_target",
        "identity_coverage_incomplete",
        "target_unreadable",
        "target_changed_during_read",
        "target_invalid_identity",
        "self_edge",
        "invalid_prerequisite",
        "completion_satisfied",
        "completion_unsatisfied",
        "completion_policy_needed",
        "completion_policy_invalid",
    }
)
_CLAIM_EDGE_REASONS = _EDGE_REASONS - {
    "completion_satisfied",
    "completion_unsatisfied",
    "completion_policy_needed",
    "completion_policy_invalid",
}
_COMPLETION_EDGE_REASONS = frozenset(
    {
        "completion_satisfied",
        "completion_unsatisfied",
        "completion_policy_needed",
        "completion_policy_invalid",
        "missing_target",
        "duplicate_target",
        "identity_coverage_incomplete",
        "target_unreadable",
        "target_changed_during_read",
        "target_invalid_identity",
        "self_edge",
        "invalid_prerequisite",
    }
)
_CLAIM_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")
_PROVENANCE = re.compile(
    r"(?:git-object-sha1:[0-9a-f]{40}|git-object-sha256:[0-9a-f]{64}|sha256:[0-9a-f]{64})"
)


class ProtocolError(ValueError):
    """A producer response does not conform to the reviewed protocol."""


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class CatalogEntry:
    """One displayed package, reduced to the fields the board renders."""

    package_path: str
    project: str
    stage: str
    declared: dict[str, str | None]
    reported_fields: tuple[dict[str, str], ...]
    relationship_data: dict[str, Any]
    package_id: str | None = None
    program_id: str | None = None
    program_title: str | None = None
    prerequisites: tuple[dict[str, Any], ...] = ()
    direct_prerequisite_state: str = "relationship_unavailable"

    @property
    def lifecycle(self) -> str:
        """Return the historical normalized stage name for internal callers."""
        return self.stage.lower()

    def as_dict(self) -> dict[str, Any]:
        return {
            "declared": dict(self.declared),
            "reported_fields": [dict(item) for item in self.reported_fields],
            "package_id": self.package_id,
            "package_path": self.package_path,
            "project": self.project,
            "stage": self.stage,
            "relationship": self.relationship_data,
        }


@dataclass(frozen=True)
class Catalog:
    entries: tuple[CatalogEntry, ...]
    discovery_diagnostics: tuple[Diagnostic, ...]
    catalog_digest: str
    terminal_stages: tuple[str, ...]
    identity_coverage: dict[str, Any]
    program_coverage: dict[str, Any]
    programs: tuple[dict[str, Any], ...]
    inventory: dict[str, Any]
    schema_version: int = SCHEMA_VERSION
    configuration_revision: str | None = None

    def as_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "catalog_digest": self.catalog_digest if include_digest else None,
            "configuration_revision": self.configuration_revision,
            "discovery_diagnostics": [
                item.as_dict() for item in self.discovery_diagnostics
            ],
            "entries": [item.as_dict() for item in self.entries],
            "identity_coverage": self.identity_coverage,
            "inventory": self.inventory,
            "program_coverage": self.program_coverage,
            "programs": [dict(item) for item in self.programs],
            "schema_version": self.schema_version,
            "terminal_stages": list(self.terminal_stages),
        }
        if not include_digest:
            result.pop("catalog_digest")
        return result


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ProtocolError(f"invalid JSON number: {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ProtocolError(f"invalid JSON number: {value}")
    return number


def load_strict_json(payload: bytes | str, name: str) -> Any:
    """Decode one JSON document, rejecting duplicate keys and non-finite numbers."""
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ProtocolError(f"{name} is not UTF-8") from error
    try:
        return json.loads(
            payload,
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (json.JSONDecodeError, RecursionError) as error:
        raise ProtocolError(f"{name} is not valid JSON") from error


def canonical_digest(
    value: Mapping[str, Any], omit: tuple[str, ...] = ("catalog_digest",)
) -> str:
    """Compute the producer-compatible digest, omitting the named top-level fields."""
    payload = {key: item for key, item in value.items() if key not in omit}
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _object(value: Any, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ProtocolError(f"{name} must be an object")
    return value


def _string(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str:
        raise ProtocolError(f"{name} must be a string or null")
    return value


def _keys(value: Mapping[str, Any], expected: frozenset[str], name: str) -> None:
    if set(value) != expected:
        raise ProtocolError(f"{name} has an invalid shape")


def _diagnostics(value: Any, name: str) -> tuple[Diagnostic, ...]:
    if type(value) is not list:
        raise ProtocolError(f"{name} must be a list")
    result: list[Diagnostic] = []
    for index, raw in enumerate(value):
        item = _object(raw, f"{name}[{index}]")
        _keys(item, _DIAGNOSTIC_KEYS, f"{name}[{index}]")
        code = _string(item["code"], f"{name}[{index}].code")
        message = _string(item["message"], f"{name}[{index}].message")
        if code not in DIAGNOSTIC_CODES or not _safe_text(message):
            raise ProtocolError(f"{name}[{index}] is not a safe diagnostic")
        result.append(Diagnostic(code, message))
    if [(item.code, item.message) for item in result] != sorted(
        (item.code, item.message) for item in result
    ):
        raise ProtocolError(f"{name} is not in canonical order")
    return tuple(result)


def _package_path(value: Any) -> str:
    path = _string(value, "package_path")
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or _text_units(path) > MAX_TEXT_UNITS
        or any(
            ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF
            for c in path
        )
    ):
        raise ProtocolError("package_path must be a nonempty relative POSIX path")
    parts = PurePosixPath(path).parts
    if (
        not parts
        or PurePosixPath(path).as_posix() != path
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ProtocolError("package_path must be a normalized relative path")
    return path


def _uuid4(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str:
        raise ProtocolError(f"{name} must be a UUIDv4 or null")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise ProtocolError(f"{name} must be a canonical UUIDv4") from None
    if (
        parsed.version != 4
        or parsed.variant != "specified in RFC 4122"
        or str(parsed) != value
    ):
        raise ProtocolError(f"{name} must be a canonical UUIDv4")
    return value


def _claim_name(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or _CLAIM_NAME.fullmatch(value) is None:
        raise ProtocolError(f"{name} must be a normalized claim name")
    return value


def _provenance(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or _PROVENANCE.fullmatch(value) is None:
        raise ProtocolError(f"{name} must be a valid provenance reference")
    return value


def _coverage(value: Any, name: str) -> None:
    item = _object(value, name)
    _keys(item, _COVERAGE_KEYS, name)
    state = _string(item["state"], f"{name}.state")
    if state not in _COVERAGE_STATES:
        raise ProtocolError(f"{name}.state is invalid")
    _diagnostics(item["diagnostics"], f"{name}.diagnostics")


def _component(value: Any, name: str) -> str:
    if not _safe_component(value):
        raise ProtocolError(f"{name} must be one safe path component")
    return value


def _inventory(value: Any) -> tuple[dict[str, Any], set[str], set[tuple[str, str]]]:
    item = _object(value, "inventory")
    _keys(item, _INVENTORY_KEYS, "inventory")
    projects_value = item["projects"]
    stages_value = item["stages"]
    if type(projects_value) is not list or type(stages_value) is not list:
        raise ProtocolError("inventory projects and stages must be lists")

    projects: list[dict[str, str]] = []
    project_names: set[str] = set()
    for index, raw in enumerate(projects_value):
        name = f"inventory.projects[{index}]"
        project = _object(raw, name)
        _keys(project, _INVENTORY_PROJECT_KEYS, name)
        project_name = _component(project["name"], f"{name}.name")
        availability = _string(project["availability"], f"{name}.availability")
        if availability not in {"complete", "incomplete"}:
            raise ProtocolError(f"{name}.availability is invalid")
        if project_name in project_names:
            raise ProtocolError("inventory.projects must be unique and name-sorted")
        project_names.add(project_name)
        projects.append({"name": project_name, "availability": availability})
    if [project["name"] for project in projects] != sorted(project_names):
        raise ProtocolError("inventory.projects must be unique and name-sorted")

    stages: list[dict[str, str]] = []
    stage_pairs: set[tuple[str, str]] = set()
    for index, raw in enumerate(stages_value):
        name = f"inventory.stages[{index}]"
        stage = _object(raw, name)
        _keys(stage, _INVENTORY_STAGE_KEYS, name)
        project_name = _component(stage["project"], f"{name}.project")
        stage_name = _component(stage["stage"], f"{name}.stage")
        availability = _string(stage["availability"], f"{name}.availability")
        if availability not in {"complete", "incomplete"}:
            raise ProtocolError(f"{name}.availability is invalid")
        pair = (project_name, stage_name)
        if project_name not in project_names or pair in stage_pairs:
            raise ProtocolError("inventory.stages must reference unique admitted projects")
        stage_pairs.add(pair)
        stages.append({
            "project": project_name,
            "stage": stage_name,
            "availability": availability,
        })
    if [(stage["project"], stage["stage"]) for stage in stages] != sorted(stage_pairs):
        raise ProtocolError("inventory.stages must be unique and pair-sorted")
    return {"projects": projects, "stages": stages}, project_names, stage_pairs


def _terminal_stages(value: Any) -> tuple[str, ...]:
    if type(value) is not list:
        raise ProtocolError("terminal_stages must be a list")
    terminal_stages = tuple(
        _component(stage, f"terminal_stages[{index}]")
        for index, stage in enumerate(value)
    )
    if list(terminal_stages) != sorted(terminal_stages) or len(terminal_stages) != len(set(terminal_stages)):
        raise ProtocolError("terminal_stages must be unique and sorted")
    return terminal_stages


def _claims(value: Any, name: str) -> None:
    if type(value) is not list:
        raise ProtocolError(f"{name} must be a list")
    names: list[str] = []
    for index, raw in enumerate(value):
        item_name = f"{name}[{index}]"
        item = _object(raw, item_name)
        _keys(item, _CLAIM_KEYS, item_name)
        claim_name = _claim_name(item["name"], f"{item_name}.name")
        state = _string(item["state"], f"{item_name}.state")
        if state not in _OBSERVED_STATES:
            raise ProtocolError(f"{item_name}.state is invalid")
        _provenance(item["evidence_ref"], f"{item_name}.evidence_ref", nullable=True)
        names.append(claim_name)
    if names != sorted(names) or len(names) != len(set(names)):
        raise ProtocolError(f"{name} must be unique and name-sorted")


def _successor(value: Any, name: str) -> None:
    item = _object(value, name)
    _keys(item, _SUCCESSOR_KEYS, name)
    _uuid4(item["package_id"], f"{name}.package_id", nullable=True)
    resolution = _string(item["resolution"], f"{name}.resolution")
    if resolution not in {"not_declared", "resolved", "unknown"}:
        raise ProtocolError(f"{name}.resolution is invalid")


def _catalog_program(value: Any, name: str) -> str:
    item = _object(value, name)
    _keys(item, _CATALOG_PROGRAM_KEYS, name)
    program_id = _uuid4(item["program_id"], f"{name}.program_id")
    title = _string(item["title"], f"{name}.title")
    if not 1 <= len(title) <= 120 or any(
        ord(character) < 32 or ord(character) == 127 for character in title
    ):
        raise ProtocolError(f"{name}.title is invalid")
    member_ids = item["member_package_ids"]
    if type(member_ids) is not list:
        raise ProtocolError(f"{name}.member_package_ids must be a list")
    members = [
        _uuid4(member_id, f"{name}.member_package_ids[{index}]")
        for index, member_id in enumerate(member_ids)
    ]
    if members != sorted(members):
        raise ProtocolError(f"{name}.member_package_ids must be sorted")
    return program_id


def _edge(value: Any, name: str) -> dict[str, Any]:
    item = _object(value, name)
    kind = _string(item.get("kind"), f"{name}.kind")
    if kind == "claim":
        _keys(item, _CLAIM_EDGE_KEYS, name)
    elif kind == "completion":
        _keys(item, _COMPLETION_EDGE_KEYS, name)
    else:
        raise ProtocolError(f"{name}.kind is invalid")
    target = _uuid4(item["target_package_id"], f"{name}.target_package_id", nullable=True)
    resolved_state = _string(item["resolved_state"], f"{name}.resolved_state")
    if resolved_state not in _OBSERVED_STATES:
        raise ProtocolError(f"{name}.resolved_state is invalid")
    reason = _string(item["reason"], f"{name}.reason")
    if reason not in _EDGE_REASONS:
        raise ProtocolError(f"{name}.reason is invalid")
    if kind == "claim":
        claim_name = _claim_name(item["claim_name"], f"{name}.claim_name", nullable=True)
        observed_state = _string(item["observed_state"], f"{name}.observed_state", nullable=True)
        if observed_state is not None and observed_state not in _OBSERVED_STATES:
            raise ProtocolError(f"{name}.observed_state is invalid")
        evidence_ref = _provenance(
            item["observed_evidence_ref"],
            f"{name}.observed_evidence_ref",
            nullable=True,
        )
        if reason not in _CLAIM_EDGE_REASONS:
            raise ProtocolError(f"{name}.reason does not match kind")
        return {
            "kind": "claim",
            "target_package_id": target,
            "claim_name": claim_name,
            "observed_state": observed_state,
            "observed_evidence_ref": evidence_ref,
            "resolved_state": resolved_state,
            "reason": reason,
        }
    observed_stage = _string(item["observed_stage"], f"{name}.observed_stage", nullable=True)
    if observed_stage is not None and not _safe_component(observed_stage):
        raise ProtocolError(f"{name}.observed_stage is invalid")
    if reason not in _COMPLETION_EDGE_REASONS:
        raise ProtocolError(f"{name}.reason does not match kind")
    return {
        "kind": "completion",
        "target_package_id": target,
        "observed_stage": observed_stage,
        "resolved_state": resolved_state,
        "reason": reason,
    }


def _program(value: Any, name: str) -> tuple[str | None, str | None]:
    item = _object(value, name)
    _keys(item, _PROGRAM_KEYS, name)
    program_id = _uuid4(item["program_id"], f"{name}.program_id", nullable=True)
    resolution = _string(item["resolution"], f"{name}.resolution")
    if resolution not in {"not_declared", "resolved", "unknown"}:
        raise ProtocolError(f"{name}.resolution is invalid")
    title = _string(item["title"], f"{name}.title", nullable=True)
    if title is not None and (
        not 1 <= len(title) <= 120
        or any(ord(character) < 32 or ord(character) == 127 for character in title)
    ):
        raise ProtocolError(f"{name}.title is invalid")
    if resolution == "resolved" and program_id is not None:
        return program_id, title
    return None, None


def _relationship(
    value: Any, index: int
) -> tuple[str | None, str | None, tuple[dict[str, Any], ...], str]:
    name = f"entries[{index}].relationship"
    item = _object(value, name)
    _keys(item, _RELATIONSHIP_KEYS, name)
    participation = _string(item["participation"], f"{name}.participation")
    if participation not in {"available", "legacy", "invalid"}:
        raise ProtocolError(f"{name}.participation is invalid")
    direct_state = _string(item["direct_prerequisite_state"], f"{name}.direct_prerequisite_state")
    if direct_state not in _DIRECT_PREREQUISITE_STATES:
        raise ProtocolError(f"{name}.direct_prerequisite_state is invalid")
    _claims(item["claims"], f"{name}.claims")
    program_id, program_title = _program(item["program"], f"{name}.program")
    _successor(item["superseded_by"], f"{name}.superseded_by")
    prerequisites_value = item["prerequisites"]
    if type(prerequisites_value) is not list:
        raise ProtocolError(f"{name}.prerequisites must be a list")
    prerequisites = tuple(
        _edge(raw, f"{name}.prerequisites[{edge_index}]")
        for edge_index, raw in enumerate(prerequisites_value)
    )
    edge_sort = [
        (
            edge["target_package_id"] or "",
            edge["kind"],
            edge.get("claim_name") or "",
            edge.get("observed_stage") or "",
        )
        for edge in prerequisites
    ]
    if edge_sort != sorted(edge_sort):
        raise ProtocolError(f"{name}.prerequisites must be sorted")
    if participation != "available":
        return None, None, (), "relationship_unavailable"
    return program_id, program_title, prerequisites, direct_state


def _text_units(value: str) -> int:
    """Count a string's length in UTF-16 code units, surrogates included."""
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)


def _safe_text(value: Any, max_units: int = MAX_TEXT_UNITS) -> bool:
    """Match browser text safety, measuring length in UTF-16 code units."""
    return (
        type(value) is str
        and bool(value)
        and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
        and _text_units(value) <= max_units
    )


def _safe_component(value: Any) -> bool:
    """Match browser component safety: bounded text holding one path segment."""
    return (
        _safe_text(value, MAX_TEXT_UNITS)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
    )


def _reported_fields(value: Any, name: str) -> tuple[dict[str, str], ...]:
    if type(value) is not list or len(value) > MAX_REPORTED_FIELDS:
        raise ProtocolError(f"{name} must be a bounded list")
    result = []
    seen = set()
    previous = None
    for raw in value:
        item = _object(raw, name)
        _keys(item, REPORTED_FIELD_KEYS, name)
        if not all(_safe_text(item[key]) for key in REPORTED_FIELD_KEYS):
            raise ProtocolError(f"{name} must contain safe text")
        field_name = item["name"]
        folded = field_name.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
        if folded in seen or (previous is not None and previous >= field_name):
            raise ProtocolError(f"{name} must be unique and name-sorted")
        seen.add(folded)
        previous = field_name
        result.append(dict(item))
    return tuple(result)


def _entry(value: Any, index: int) -> CatalogEntry:
    item = _object(value, f"entries[{index}]")
    _keys(item, _ENTRY_KEYS, f"entries[{index}]")
    package_id = _uuid4(item["package_id"], f"entries[{index}].package_id", nullable=True)
    project = _component(item["project"], f"entries[{index}].project")
    stage = _component(item["stage"], f"entries[{index}].stage")
    declared = _object(item["declared"], f"entries[{index}].declared")
    _keys(declared, frozenset(DECLARED_FIELDS), f"entries[{index}].declared")
    declared_values = {
        field: _string(
            declared[field], f"entries[{index}].declared.{field}", nullable=True
        )
        for field in DECLARED_FIELDS
    }
    program_id, program_title, prerequisites, direct_state = _relationship(
        item["relationship"], index
    )
    for field, value in declared_values.items():
        if value not in (None, "") and not _safe_text(value):
            raise ProtocolError(f"entries[{index}].declared.{field} must be safe text")
    reported_fields = _reported_fields(item["reported_fields"], f"entries[{index}].reported_fields")
    package_path = _package_path(item["package_path"])
    parts = package_path.split("/")
    if len(parts) < 2 or project != parts[0] or stage != parts[1]:
        raise ProtocolError(f"entries[{index}] has inconsistent project/stage path")
    return CatalogEntry(
        package_path=package_path,
        project=project,
        stage=stage,
        declared=declared_values,
        reported_fields=reported_fields,
        package_id=package_id,
        program_id=program_id,
        program_title=program_title,
        prerequisites=prerequisites,
        direct_prerequisite_state=direct_state,
        relationship_data={
            "participation": item["relationship"]["participation"],
            "claims": item["relationship"]["claims"],
            "prerequisites": [dict(edge) for edge in item["relationship"]["prerequisites"]],
            "direct_prerequisite_state": item["relationship"]["direct_prerequisite_state"],
            "program": item["relationship"]["program"],
            "superseded_by": item["relationship"]["superseded_by"],
        },
    )


def parse_catalog(payload: bytes | str | Mapping[str, Any]) -> Catalog:
    """Parse, strictly validate, and authenticate one producer response."""
    if isinstance(payload, (bytes, str)):
        raw: Any = load_strict_json(payload, "catalog")
    elif isinstance(payload, Mapping):
        raw = dict(payload)
    else:
        raise ProtocolError("catalog must be JSON object")
    catalog = _object(raw, "catalog")
    _keys(catalog, _TOP_LEVEL_KEYS, "catalog")
    if catalog["schema_version"] != SCHEMA_VERSION or (
        type(catalog["schema_version"]) is not int
    ):
        raise ProtocolError("unsupported schema version")
    digest = _string(catalog["catalog_digest"], "catalog_digest")
    if digest is None or len(digest) != 64 or any(
        c not in "0123456789abcdef" for c in digest
    ):
        raise ProtocolError("invalid catalog digest")
    configuration_revision = _string(
        catalog["configuration_revision"],
        "configuration_revision",
        nullable=True,
    )
    terminal_stages = _terminal_stages(catalog["terminal_stages"])
    inventory, _project_names, inventory_pairs = _inventory(catalog["inventory"])
    for name in ("identity_coverage", "program_coverage"):
        _coverage(catalog[name], name)
    if type(catalog["entries"]) is not list:
        raise ProtocolError("entries must be a list")
    entries = tuple(_entry(item, index) for index, item in enumerate(catalog["entries"]))
    if any((entry.project, entry.stage) not in inventory_pairs for entry in entries):
        raise ProtocolError("entries must reference admitted inventory stages")
    paths = [item.package_path for item in entries]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ProtocolError("entries must be unique and path-sorted")
    if type(catalog["programs"]) is not list:
        raise ProtocolError("programs must be a list")
    program_ids = [
        _catalog_program(program, f"programs[{index}]")
        for index, program in enumerate(catalog["programs"])
    ]
    if program_ids != sorted(program_ids) or len(program_ids) != len(set(program_ids)):
        raise ProtocolError("programs must be unique and ID-sorted")
    diagnostics = _diagnostics(catalog["discovery_diagnostics"], "discovery_diagnostics")
    if canonical_digest(catalog) != digest:
        raise ProtocolError("catalog digest mismatch")
    return Catalog(
        entries=entries,
        discovery_diagnostics=diagnostics,
        catalog_digest=digest,
        terminal_stages=terminal_stages,
        identity_coverage=dict(catalog["identity_coverage"]),
        program_coverage=dict(catalog["program_coverage"]),
        programs=tuple(dict(program) for program in catalog["programs"]),
        inventory=inventory,
        configuration_revision=configuration_revision,
    )


# ---------------------------------------------------------------------------
# Checkout facts document (schema 5)
# ---------------------------------------------------------------------------

CHECKOUTS_SCHEMA_VERSION = 5
CHECKOUTS_DIGEST_OMITTED = ("checkouts_digest", "observed_at")
# A clone lists at most this many local branches; beyond it the clone is
# reported as ``too_many_branches`` and none of its branches is examined.
CHECKOUT_BRANCH_CAP = 100

_NUMBER_RUN = re.compile(r"([0-9]+)")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_GIT_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)+\S*")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")

_CHECKOUTS_KEYS = frozenset(
    {
        "schema_version",
        "checkouts_digest",
        "observed_at",
        "git",
        "remote_checks",
        "github_lookup",
        "folders",
        "repositories",
        "diagnostics",
        "package_scan",
        "packages",
    }
)
_PACKAGE_SCAN_KEYS = frozenset({"status", "finished_policy"})
_PACKAGE_SCAN_STATUSES = frozenset({"complete", "incomplete", "unavailable"})
_FINISHED_POLICIES = frozenset({"configured", "not_configured"})
_PACKAGE_KEYS = frozenset(
    {
        "package_path",
        "package_id",
        "finished",
        "superseded",
        "target",
        "branch",
        "code_status",
    }
)
_CODE_STATUS_KEYS = frozenset(
    {
        "state",
        "text",
        "pull_request",
        "push",
        "unpushed",
        "cleanup",
        "checked_out_elsewhere",
    }
)
_CODE_STATUS_STATES = frozenset(
    {"merged", "pr_open", "awaiting_merge", "superseded", "in_progress", "unknown"}
)
_CODE_STATUS_PUSHES = frozenset({"pushed", "unpushed", "not_pushed", "unknown"})
_CLEANUP_KEYS = frozenset({"worktrees", "branches"})
_PACKAGE_TARGET_KEYS = frozenset({"status", "clone", "worktree"})
_PACKAGE_TARGET_STATUSES = frozenset(
    {
        "clone",
        "worktree",
        "missing",
        "not_git",
        "outside_folders",
        "invalid",
        "absent",
        "unchecked",
    }
)
_PACKAGE_BRANCH_KEYS = frozenset({"status", "name"})
_PACKAGE_BRANCH_STATUSES = frozenset(
    {"declared", "default_branch", "not_applicable", "invalid", "absent"}
)
_GIT_KEYS = frozenset({"status", "version"})
_GIT_STATUSES = frozenset({"available", "missing", "too_old"})
_REMOTE_CHECKS = frozenset({"on", "off"})
_GITHUB_LOOKUP = frozenset({"on", "off"})
_FOLDER_KEYS = frozenset({"path", "status"})
_FOLDER_STATUSES = frozenset({"ok", "missing", "unreadable", "not_directory"})
_CHECKOUTS_DIAGNOSTIC_KEYS = frozenset({"code", "path"})
CHECKOUTS_DIAGNOSTIC_CODES = frozenset(
    {
        "not_a_repository",
        "unrepresentable_path",
        "checkout_cache_unreadable",
        "checkout_cache_write_failed",
    }
)
_REPOSITORY_KEYS = frozenset(
    {"remote", "default_branch", "pr_lookup", "clones", "package_branches"}
)
_REMOTE_KEYS = frozenset({"key", "display", "check"})
_CHECK_KEYS = frozenset({"status", "reason"})
_CHECK_STATUSES = frozenset({"live", "recorded", "failed", "off"})
_CHECK_FAILURE_REASONS = frozenset(
    {"unreachable", "not_found", "authentication_failed", "timed_out", "failed"}
)
_DEFAULT_BRANCH_KEYS = frozenset({"name", "sha"})
_PR_LOOKUP_KEYS = frozenset({"status", "reason", "checked_at"})
_PR_LOOKUP_STATUSES = frozenset({"live", "recorded", "off", "unavailable"})
_PR_LOOKUP_FAILURE_REASONS = frozenset(
    {"gh_missing", "not_authenticated", "not_found", "rate_limited", "timed_out", "failed"}
)
_GITHUB_HOST = "github.com"
_GITHUB_ENTERPRISE_SUFFIX = ".ghe.com"
_CLONE_KEYS = frozenset(
    {
        "path",
        "bare",
        "status",
        "partial_clone",
        "shallow",
        "cached_default",
        "local_default",
        "cached_vs_remote",
        "stash_count",
        "worktrees",
        "branches",
        "verdict_base",
        "containment",
        "removable_count",
    }
)
_CONTAINMENT_OUTCOMES = frozenset({"complete", "deadline", "failed"})
_ITEM_STATUSES = frozenset({"ok", "unsafe_ownership", "failed", "timed_out"})
_CLONE_STATUSES = _ITEM_STATUSES | {"too_many_branches"}
_CACHED_DEFAULT_KEYS = frozenset({"name", "sha", "last_fetched_at", "fetched_since_clone"})
_LOCAL_DEFAULT_KEYS = frozenset({"name", "sha", "vs_remote", "vs_cached"})
_WORKTREE_KEYS = frozenset(
    {
        "path",
        "main",
        "head",
        "locked",
        "prunable",
        "status",
        "changes",
        "operations",
        "index_lock",
        "verdict",
        "removable",
    }
)
_HEAD_KEYS = frozenset({"branch", "sha"})
_CHANGE_KEYS = frozenset({"staged", "unstaged", "untracked", "conflicted"})
OPERATIONS = ("am", "bisect", "cherry_pick", "merge", "rebase", "revert")
_BRANCH_KEYS = frozenset(
    {
        "name",
        "sha",
        "checked_out_in",
        "upstream",
        "upstream_mismatch",
        "vs_remote",
        "pull_requests",
        "verdict",
        "contained_in",
        "packages",
        "removable",
    }
)
# How a local branch serves a package.
_BRANCH_PACKAGE_KEYS = frozenset({"package_path", "basis"})
_BRANCH_PACKAGE_BASES = frozenset({"target_clone", "sibling_clone", "contained"})
_PACKAGE_BRANCH_ENTRY_KEYS = frozenset({"name", "remote", "packages"})
# A package branch's remote copy: what the group's remote answered, or the clones' own
# remote-tracking refs when it did not.
_REMOTE_COPY_KEYS = frozenset({"state", "sha", "basis"})
_REMOTE_COPY_STATES = frozenset({"present", "absent", "unknown"})
_PULL_REQUEST_KEYS = frozenset({"number", "state", "head_matches"})
_PULL_REQUEST_STATES = frozenset({"open", "merged", "closed"})
# At most this many pull requests are listed for one branch; the collector's
# request and list cap are built from it.
PULL_REQUEST_LIST_CAP = 10
_COMPARISON_KEYS = frozenset({"basis", "relation", "ahead", "behind", "objects_from"})
_COMPARISON_BASES = frozenset({"live", "recorded", "cached"})
_VERDICT_PATCH_FLOOR = (2, 39)
_NUMERIC_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*")
_VERDICT_BASE_KEYS = frozenset({"name", "sha", "basis", "objects_from"})
_VERDICT_KEYS = frozenset({"state", "rung", "reason", "pull_request"})
_VERDICT_STATES = frozenset({"merged", "not_merged", "unknown"})
_VERDICT_MERGED_RUNGS = frozenset(
    {"ancestor", "pull_request", "patch_equivalent", "squash_equivalent", "tree_equal"}
)
_VERDICT_UNKNOWN_REASONS = frozenset(
    {"no_base", "incomplete_clone", "git_too_old", "history_limit", "deadline", "failed"}
)
_COMPARISON_RELATIONS = frozenset(
    {"equal", "ahead", "behind", "diverged", "differs", "absent", "unknown"}
)


def natural_key(text: str) -> tuple[Any, ...]:
    """Order text with digit runs as numbers, other runs by code point, ties by raw text."""
    parts: list[Any] = []
    for index, part in enumerate(_NUMBER_RUN.split(text)):
        if index % 2:
            digits = part.lstrip("0")
            parts.append((len(digits), digits))
        else:
            parts.append(part)
    return (tuple(parts), text)


def _plural(count: int, noun: str) -> str:
    suffix = "" if count == 1 else "es" if noun.endswith("ch") else "s"
    return f"{count} {noun}{suffix}"


def _path_name(path: str) -> str:
    """The last component of a path, splitting on both separators on every host."""
    parts = [part for part in re.split(r"[/\\]", path) if part]
    return parts[-1] if parts else path


def code_status_text(fields: Mapping[str, Any]) -> str:
    """The one text of a package's Code Status, built from the stored fields.

    ``fields`` carries ``state``, ``pull_request``, ``push``, ``unpushed``,
    ``cleanup`` and ``checked_out_elsewhere``. The text lists the other
    worktrees' names, or only their count when the names would not fit.
    """
    state = fields["state"]
    push = fields["push"]
    if state == "merged":
        text = "merged"
        cleanup = fields["cleanup"]
        if cleanup is not None and cleanup["worktrees"]:
            text += f" \u00b7 cleanup pending ({_plural(cleanup['worktrees'], 'worktree')})"
        elif cleanup is not None and cleanup["branches"]:
            text += f" \u00b7 cleanup pending ({_plural(cleanup['branches'], 'branch')})"
    elif state == "pr_open":
        text = f"PR #{fields['pull_request']} open"
        if push == "unpushed":
            text += f" \u00b7 {fields['unpushed']} unpushed"
    elif state == "awaiting_merge":
        text = "awaiting merge (no PR)"
    elif state == "superseded":
        text = "superseded \u00b7 branch retained"
    elif state == "in_progress":
        fragment = {
            "pushed": "pushed",
            "not_pushed": "not pushed",
            "unpushed": f"{fields['unpushed']} unpushed",
        }.get(push, "push state unknown")
        text = f"in progress \u00b7 {fragment}"
    else:
        text = "merge state unknown"
    paths = fields["checked_out_elsewhere"]
    if paths:
        names = sorted((_path_name(path) for path in paths), key=natural_key)
        listed = f"{text} \u00b7 in {', '.join(names)}"
        if _text_units(listed) <= MAX_TEXT_UNITS:
            return listed
        return f"{text} \u00b7 in {_plural(len(paths), 'other worktree')}"
    return text


def folder_sort_key(folder: Mapping[str, Any]) -> tuple[Any, ...]:
    return natural_key(folder["path"])


def diagnostic_sort_key(diagnostic: Mapping[str, Any]) -> tuple[Any, ...]:
    path = diagnostic["path"]
    return (
        natural_key(diagnostic["code"]),
        (0, ()) if path is None else (1, natural_key(path)),
    )


def repository_sort_key(repository: Mapping[str, Any]) -> tuple[Any, ...]:
    remote = repository["remote"]
    if remote is not None:
        return (0, natural_key(remote["display"]), natural_key(remote["key"]))
    return (1, natural_key(repository["clones"][0]["path"]))


def clone_sort_key(clone: Mapping[str, Any]) -> tuple[Any, ...]:
    return natural_key(clone["path"])


def worktree_sort_key(worktree: Mapping[str, Any]) -> tuple[Any, ...]:
    return (0 if worktree["main"] else 1, natural_key(worktree["path"]))


def branch_sort_key(branch: Mapping[str, Any]) -> tuple[Any, ...]:
    return natural_key(branch["name"])


def _nullable(value: Any, check, name: str) -> Any:
    return None if value is None else check(value, name)


def _bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ProtocolError(f"{name} must be a boolean")
    return value


def _count(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ProtocolError(f"{name} must be a non-negative integer")
    return value


def _text(value: Any, name: str) -> str:
    if not _safe_text(value):
        raise ProtocolError(f"{name} must be safe text")
    return value


def _choice(value: Any, allowed: frozenset[str], name: str) -> str:
    if type(value) is not str or value not in allowed:
        raise ProtocolError(f"{name} is invalid")
    return value


def _timestamp(value: Any, name: str) -> str:
    if type(value) is not str or _TIMESTAMP.fullmatch(value) is None:
        raise ProtocolError(f"{name} must be a UTC timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ProtocolError(f"{name} must be a UTC timestamp") from None
    return value


def _object_id(value: Any, name: str) -> str:
    if (
        type(value) is not str
        or _OBJECT_ID.fullmatch(value) is None
        or not value.strip("0")
    ):
        raise ProtocolError(f"{name} must be an object ID")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if type(value) is not list:
        raise ProtocolError(f"{name} must be a list")
    return value


def _ordered(items: list[Any], key, name: str) -> None:
    keys = [key(item) for item in items]
    if any(left >= right for left, right in zip(keys, keys[1:])):
        raise ProtocolError(f"{name} must be unique and in canonical order")


def _basis(basis: Any, name: str, check: str | None) -> str:
    """Check a comparison's or verdict base's basis against its group's remote check.

    ``check`` is the status of the group's ``remote.check``, or ``None`` for a
    clone with no remote. The ``live`` basis needs a ``live`` check and the
    ``recorded`` basis a ``recorded`` one; ``cached`` stands under any.
    """
    basis = _choice(basis, _COMPARISON_BASES, f"{name}.basis")
    if basis in ("live", "recorded") and check != basis:
        raise ProtocolError(f"{name}.basis is {basis} without a {basis} remote check")
    return basis


# The bases a branch's comparison may take under each remote check: the saved
# check may be overridden by a newer remote-tracking ref of the clone itself.
_BRANCH_BASES: dict[str | None, frozenset[str]] = {
    "live": frozenset({"live"}),
    "recorded": frozenset({"recorded", "cached"}),
    "off": frozenset({"cached"}),
    "failed": frozenset({"cached"}),
    None: frozenset({"cached"}),
}


def _comparison(
    value: Any, name: str, *, check: str | None, nullable: bool = False
) -> None:
    if value is None and nullable:
        return
    item = _object(value, name)
    _keys(item, _COMPARISON_KEYS, name)
    basis = _basis(item["basis"], name, check)
    relation = _choice(item["relation"], _COMPARISON_RELATIONS, f"{name}.relation")
    ahead, behind = item["ahead"], item["behind"]
    lender = _nullable(item["objects_from"], _text, f"{name}.objects_from")
    if lender is not None and basis == "cached":
        raise ProtocolError(f"{name}.objects_from requires the live or recorded basis")
    if relation in {"differs", "absent", "unknown"}:
        if ahead is not None or behind is not None or item["objects_from"] is not None:
            raise ProtocolError(f"{name} must not carry counts for {relation}")
        return
    ahead = _count(ahead, f"{name}.ahead")
    behind = _count(behind, f"{name}.behind")
    expected = {
        "equal": ahead == 0 and behind == 0,
        "ahead": ahead > 0 and behind == 0,
        "behind": ahead == 0 and behind > 0,
        "diverged": ahead > 0 and behind > 0,
    }[relation]
    if not expected:
        raise ProtocolError(f"{name} counts contradict {relation}")


def _verdict(value: Any, name: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    item = _object(value, name)
    _keys(item, _VERDICT_KEYS, name)
    state = _choice(item["state"], _VERDICT_STATES, f"{name}.state")
    rung, reason = item["rung"], item["reason"]
    number = item["pull_request"]
    if state == "merged":
        _choice(rung, _VERDICT_MERGED_RUNGS, f"{name}.rung")
        valid = reason is None
    elif state == "not_merged":
        valid = rung == "none" and reason is None
    else:
        _choice(reason, _VERDICT_UNKNOWN_REASONS, f"{name}.reason")
        valid = rung is None
    if not valid:
        raise ProtocolError(f"{name} carries a rung or reason its state does not allow")
    if number is not None and (type(number) is not int or number < 1):
        raise ProtocolError(f"{name}.pull_request must be a positive integer or null")
    if (number is not None) != (rung == "pull_request"):
        raise ProtocolError(f"{name}.pull_request must be set exactly for the pull_request rung")


def _verdict_base(value: Any, name: str, *, check: str | None) -> None:
    if value is None:
        return
    item = _object(value, name)
    _keys(item, _VERDICT_BASE_KEYS, name)
    _text(item["name"], f"{name}.name")
    _object_id(item["sha"], f"{name}.sha")
    basis = _basis(item["basis"], name, check)
    lender = _nullable(item["objects_from"], _text, f"{name}.objects_from")
    if lender is not None and basis == "cached":
        raise ProtocolError(f"{name}.objects_from requires the live or recorded basis")


def _head(value: Any, name: str) -> None:
    item = _object(value, name)
    _keys(item, _HEAD_KEYS, name)
    branch = _nullable(item["branch"], _text, f"{name}.branch")
    sha = _nullable(item["sha"], _object_id, f"{name}.sha")
    if branch is None and sha is None:
        raise ProtocolError(f"{name} names neither a branch nor a commit")


def _worktree(value: Any, name: str) -> dict[str, Any]:
    item = _object(value, name)
    _keys(item, _WORKTREE_KEYS, name)
    _text(item["path"], f"{name}.path")
    _bool(item["main"], f"{name}.main")
    _bool(item["locked"], f"{name}.locked")
    prunable = _bool(item["prunable"], f"{name}.prunable")
    status = _choice(item["status"], _ITEM_STATUSES, f"{name}.status")
    nullable_fields = ("head", "changes", "operations", "index_lock", "verdict")
    if status != "ok":
        if any(item[field] is not None for field in nullable_fields):
            raise ProtocolError(f"{name} must leave its facts null when status is {status}")
        return item
    if prunable:
        raise ProtocolError(f"{name} is prunable but reports status ok")
    _head(item["head"], f"{name}.head")
    changes = _object(item["changes"], f"{name}.changes")
    _keys(changes, _CHANGE_KEYS, f"{name}.changes")
    for field in sorted(_CHANGE_KEYS):
        _count(changes[field], f"{name}.changes.{field}")
    operations = _list(item["operations"], f"{name}.operations")
    if (
        any(type(op) is not str or op not in OPERATIONS for op in operations)
        or operations != sorted(set(operations))
    ):
        raise ProtocolError(f"{name}.operations is invalid")
    _bool(item["index_lock"], f"{name}.index_lock")
    _verdict(item["verdict"], f"{name}.verdict", nullable=True)
    return item


def _pull_requests(value: Any, name: str) -> None:
    """Check one branch's pull-request list: bounded, unique, ordered by number."""
    listed = _list(value, name)
    if len(listed) > PULL_REQUEST_LIST_CAP:
        raise ProtocolError(f"{name} lists more than {PULL_REQUEST_LIST_CAP} pull requests")
    numbers: list[int] = []
    for index, raw in enumerate(listed):
        label = f"{name}[{index}]"
        item = _object(raw, label)
        _keys(item, _PULL_REQUEST_KEYS, label)
        number = item["number"]
        if type(number) is not int or number < 1:
            raise ProtocolError(f"{label}.number must be a positive integer")
        _choice(item["state"], _PULL_REQUEST_STATES, f"{label}.state")
        _bool(item["head_matches"], f"{label}.head_matches")
        numbers.append(number)
    if any(left >= right for left, right in zip(numbers, numbers[1:])):
        raise ProtocolError(f"{name} must be unique and sorted by number")


def _branch(
    value: Any, name: str, *, has_remote: bool, check: str | None
) -> dict[str, Any]:
    item = _object(value, name)
    _keys(item, _BRANCH_KEYS, name)
    _text(item["name"], f"{name}.name")
    _object_id(item["sha"], f"{name}.sha")
    paths = _list(item["checked_out_in"], f"{name}.checked_out_in")
    for index, path in enumerate(paths):
        _text(path, f"{name}.checked_out_in[{index}]")
    _ordered(paths, natural_key, f"{name}.checked_out_in")
    _nullable(item["upstream"], _text, f"{name}.upstream")
    mismatch = _bool(item["upstream_mismatch"], f"{name}.upstream_mismatch")
    _comparison(item["vs_remote"], f"{name}.vs_remote", check=check, nullable=True)
    compared = item["vs_remote"]
    if compared is not None and compared["basis"] not in _BRANCH_BASES[check]:
        raise ProtocolError(f"{name}.vs_remote.basis does not fit the {check} remote check")
    if not has_remote and (item["vs_remote"] is not None or mismatch):
        raise ProtocolError(f"{name} compares with a remote the clone does not have")
    if has_remote and item["vs_remote"] is None:
        raise ProtocolError(f"{name}.vs_remote is required for a clone with a remote")
    _pull_requests(item["pull_requests"], f"{name}.pull_requests")
    _verdict(item["verdict"], f"{name}.verdict", nullable=True)
    _nullable(item["contained_in"], _text, f"{name}.contained_in")
    references = _list(item["packages"], f"{name}.packages")
    for index, raw in enumerate(references):
        reference = _object(raw, f"{name}.packages[{index}]")
        _keys(reference, _BRANCH_PACKAGE_KEYS, f"{name}.packages[{index}]")
        _package_path(reference["package_path"])
        _choice(reference["basis"], _BRANCH_PACKAGE_BASES, f"{name}.packages[{index}].basis")
    _ordered(references, package_sort_key, f"{name}.packages")
    return item


def _verdict_state(verdict: Mapping[str, Any]) -> tuple[str, Any]:
    return verdict["state"], verdict["reason"]


def _git_below_patch_floor(version: str | None) -> bool:
    numeric = _NUMERIC_VERSION.match(version) if version is not None else None
    if numeric is None:
        return False
    parts = tuple(int(part) for part in numeric.group(0).split("."))
    return parts[: len(_VERDICT_PATCH_FLOOR)] < _VERDICT_PATCH_FLOOR


def _worktree_not_clean(worktree: Mapping[str, Any]) -> bool:
    """Say whether a linked worktree's tree holds anything a removal would lose.

    A prunable worktree has no files left, so its ``failed`` status is the
    registration's and not a tree that could not be read.
    """
    if worktree["prunable"]:
        return False
    if worktree["status"] != "ok":
        return True
    return (
        any(worktree["changes"][field] for field in _CHANGE_KEYS)
        or bool(worktree["operations"])
        or worktree["index_lock"]
    )


def _verdict_blockers(verdict: Mapping[str, Any] | None) -> list[str]:
    """Blockers a verdict puts on whatever holds that commit; none when merged."""
    if verdict is None:
        return ["verdict_unknown"]
    if verdict["state"] == "unknown":
        return ["verdict_unknown"]
    return [] if verdict["state"] == "merged" else ["not_merged"]


def _removable(blockers: list[str]) -> dict[str, Any]:
    return {"removable": not blockers, "blockers": blockers}


def derive_removable(clone: Mapping[str, Any]) -> dict[str, Any]:
    """Derive every Removable verdict of one ``ok`` clone from its collected facts.

    Returns ``{"branches", "worktrees", "removable_count"}``. ``branches`` and
    ``worktrees`` hold one entry per item of the clone's ``branches`` and
    ``worktrees`` lists, in the same order: a ``{"removable", "blockers"}``
    mapping, or ``None`` for the branch named by ``verdict_base`` and for the
    main worktree. ``removable_count`` is ``{"branches", "worktrees"}``.

    Branch blockers, in order: ``not_merged``, ``verdict_unknown``,
    ``checked_out_in_main_worktree``, ``worktree_not_clean`` and
    ``worktree_locked`` (the last two for a linked worktree that holds the
    branch). Linked-worktree blockers, in order: ``not_merged``,
    ``verdict_unknown``, ``not_clean``, ``locked``. A worktree takes the verdict
    of the branch it is on, found through its own head when its status is
    ``ok`` and through the branch's ``checked_out_in`` otherwise; a detached one
    takes its own verdict, and one whose commit cannot be read, or whose branch
    has no verdict, is ``verdict_unknown``. Ignored files never block.
    """
    base = clone["verdict_base"]
    base_name = None if base is None else base["name"]
    branches = clone["branches"]
    worktrees = clone["worktrees"]
    verdicts = {
        branch["name"]: branch["verdict"]
        for branch in branches
        if branch["name"] != base_name
    }
    by_path = {worktree["path"]: worktree for worktree in worktrees}
    main_paths = {worktree["path"] for worktree in worktrees if worktree["main"]}

    def branch_of(worktree: Mapping[str, Any]) -> str | None:
        if worktree["status"] == "ok":
            return worktree["head"]["branch"]
        for branch in branches:
            if worktree["path"] in branch["checked_out_in"]:
                return branch["name"]
        return None

    def worktree_blockers(worktree: Mapping[str, Any]) -> list[str]:
        name = branch_of(worktree)
        if name is not None:
            blockers = _verdict_blockers(verdicts.get(name))
        elif worktree["status"] == "ok":
            blockers = _verdict_blockers(worktree["verdict"])
        else:
            blockers = ["verdict_unknown"]
        if _worktree_not_clean(worktree):
            blockers.append("not_clean")
        if worktree["locked"]:
            blockers.append("locked")
        return blockers

    branch_results: list[dict[str, Any] | None] = []
    for branch in branches:
        if branch["name"] == base_name:
            branch_results.append(None)
            continue
        blockers = _verdict_blockers(branch["verdict"])
        holders = [by_path[path] for path in branch["checked_out_in"]]
        if any(path in main_paths for path in branch["checked_out_in"]):
            blockers.append("checked_out_in_main_worktree")
        if any(_worktree_not_clean(w) for w in holders if not w["main"]):
            blockers.append("worktree_not_clean")
        if any(w["locked"] for w in holders if not w["main"]):
            blockers.append("worktree_locked")
        branch_results.append(_removable(blockers))
    worktree_results = [
        None if worktree["main"] else _removable(worktree_blockers(worktree))
        for worktree in worktrees
    ]
    return {
        "branches": branch_results,
        "worktrees": worktree_results,
        "removable_count": {
            "branches": sum(1 for r in branch_results if r is not None and r["removable"]),
            "worktrees": sum(1 for r in worktree_results if r is not None and r["removable"]),
        },
    }


def _clone_verdicts(
    item: Mapping[str, Any],
    name: str,
    *,
    has_remote: bool,
    lookup_used: bool,
    git_version: str | None,
    branches: list[dict[str, Any]],
    worktrees: list[dict[str, Any]],
) -> None:
    """Check the verdicts of one ``ok`` clone against its base and its own limits.

    ``lookup_used`` says the repository's pull-request lookup is ``live`` or
    ``recorded``: only then may a branch list pull requests or a verdict
    carry the ``pull_request`` rung.
    """
    base = item["verdict_base"]
    if base is not None and not has_remote:
        raise ProtocolError(f"{name}.verdict_base requires a remote")
    merged_at: dict[str, set[int]] = {}
    for index, branch in enumerate(branches):
        if branch["pull_requests"] and not lookup_used:
            raise ProtocolError(
                f"{name}.branches[{index}].pull_requests requires a pull-request lookup"
            )
        merged_at.setdefault(branch["sha"], set()).update(
            pull["number"] for pull in branch["pull_requests"] if pull["state"] == "merged"
        )
    verdicts: list[tuple[str, Mapping[str, Any], str | None, bool]] = []
    branch_verdicts: dict[str, list[Mapping[str, Any]]] = {}
    for index, branch in enumerate(branches):
        label = f"{name}.branches[{index}].verdict"
        verdict = branch["verdict"]
        names_base = base is not None and branch["name"] == base["name"]
        if names_base != (verdict is None):
            raise ProtocolError(f"{label} must be null exactly for the verdict base branch")
        if verdict is not None:
            verdicts.append((label, verdict, branch["sha"], False))
            branch_verdicts.setdefault(branch["sha"], []).append(verdict)
    for index, worktree in enumerate(worktrees):
        label = f"{name}.worktrees[{index}].verdict"
        detached = (
            worktree["status"] == "ok"
            and worktree["head"]["branch"] is None
        )
        if detached != (worktree["verdict"] is not None):
            raise ProtocolError(f"{label} must be present exactly for a detached checkout")
        if worktree["verdict"] is not None:
            verdicts.append((label, worktree["verdict"], worktree["head"]["sha"], True))
    limited = item["shallow"] or item["partial_clone"]
    for label, verdict, commit, detached_checkout in verdicts:
        if verdict["rung"] == "pull_request":
            if not lookup_used:
                raise ProtocolError(f"{label} names a pull request without a pull-request lookup")
            # A detached checkout takes the rung only through a branch at its commit.
            if commit not in merged_at:
                raise ProtocolError(f"{label} names a pull request at a commit no branch holds")
            if verdict["pull_request"] not in merged_at[commit]:
                raise ProtocolError(
                    f"{label} names a pull request no branch at its commit lists as merged"
                )
            # One commit has one verdict: every branch that takes a verdict at it
            # carries the same one.  The verdict base branch takes none, so a
            # detached checkout it alone shares a commit with has nothing to match.
            if detached_checkout and any(
                other != verdict for other in branch_verdicts.get(commit, ())
            ):
                raise ProtocolError(
                    f"{label} differs from the verdict of a branch at its commit"
                )
        no_base = _verdict_state(verdict) == ("unknown", "no_base")
        if no_base != (base is None):
            raise ProtocolError(f"{label} contradicts the verdict base of the clone")
        reason = verdict["reason"]
        if reason == "incomplete_clone" and not limited:
            raise ProtocolError(f"{label} names an incomplete clone that is complete")
        if reason == "git_too_old" and not _git_below_patch_floor(git_version):
            raise ProtocolError(f"{label} names an old git that is not old")


def _clone_containment(
    item: Mapping[str, Any],
    name: str,
    *,
    git_version: str | None,
    branches: list[dict[str, Any]],
) -> None:
    """Check the containment outcome and every ``contained_in`` of one ``ok`` clone.

    The outcome follows one precedence: no base, then a shallow or partial
    clone, then a git below the patch floor, then the work's own outcome.
    A branch is contained only when the outcome is ``complete``.
    """
    base = item["verdict_base"]
    outcome = item["containment"]
    label = f"{name}.containment"
    if base is None:
        if outcome != "no_base":
            raise ProtocolError(f"{label} must be no_base exactly when the clone has no base")
    elif item["shallow"] or item["partial_clone"]:
        if outcome != "incomplete":
            raise ProtocolError(f"{label} must be incomplete for a shallow or partial clone")
    elif _git_below_patch_floor(git_version):
        if outcome != "git_too_old":
            raise ProtocolError(f"{label} must be git_too_old below the patch floor")
    elif type(outcome) is not str or outcome not in _CONTAINMENT_OUTCOMES:
        raise ProtocolError(f"{label} contradicts the base, depth or git version of the clone")
    containers = {branch["name"]: branch["contained_in"] for branch in branches}
    for index, branch in enumerate(branches):
        target = branch["contained_in"]
        if target is None:
            continue
        where = f"{name}.branches[{index}].contained_in"
        if outcome != "complete":
            raise ProtocolError(f"{where} must be null unless containment is complete")
        if branch["name"] == base["name"] or target == base["name"]:
            raise ProtocolError(f"{where} involves the verdict base branch")
        if target == branch["name"] or target not in containers:
            raise ProtocolError(f"{where} must name another listed branch")
    for start in containers:
        seen = {start}
        current = containers[start]
        while current is not None:
            if current in seen:
                raise ProtocolError(f"{name} has a containment cycle")
            seen.add(current)
            current = containers[current]


_REMOVABLE_KEYS = frozenset({"removable", "blockers"})
_REMOVABLE_COUNT_KEYS = frozenset({"branches", "worktrees"})


def _removable_shape(value: Any, name: str) -> None:
    """Check the shape of one ``removable`` value; its content is checked by recomputation."""
    if value is None:
        return
    item = _object(value, name)
    _keys(item, _REMOVABLE_KEYS, name)
    _bool(item["removable"], f"{name}.removable")
    blockers = _list(item["blockers"], f"{name}.blockers")
    for index, blocker in enumerate(blockers):
        _text(blocker, f"{name}.blockers[{index}]")


def _clone_removable(
    item: Mapping[str, Any],
    name: str,
    *,
    branches: list[dict[str, Any]],
    worktrees: list[dict[str, Any]],
) -> None:
    """Check every Removable value and count of one ``ok`` clone against the derivation.

    The shapes are checked first; then the document must equal what
    ``derive_removable`` returns for the clone's own facts, which also settles
    the null rules, blocker order and uniqueness, and the counts.
    """
    for index, branch in enumerate(branches):
        _removable_shape(branch["removable"], f"{name}.branches[{index}].removable")
    for index, worktree in enumerate(worktrees):
        _removable_shape(worktree["removable"], f"{name}.worktrees[{index}].removable")
    count = _object(item["removable_count"], f"{name}.removable_count")
    _keys(count, _REMOVABLE_COUNT_KEYS, f"{name}.removable_count")
    for field in sorted(_REMOVABLE_COUNT_KEYS):
        _count(count[field], f"{name}.removable_count.{field}")
    derived = derive_removable(item)
    for index, (branch, expected) in enumerate(zip(branches, derived["branches"])):
        if branch["removable"] != expected:
            raise ProtocolError(
                f"{name}.branches[{index}].removable contradicts the facts of the clone"
            )
    for index, (worktree, expected) in enumerate(zip(worktrees, derived["worktrees"])):
        if worktree["removable"] != expected:
            raise ProtocolError(
                f"{name}.worktrees[{index}].removable contradicts the facts of the clone"
            )
    if count != derived["removable_count"]:
        raise ProtocolError(f"{name}.removable_count contradicts the facts of the clone")


def _remote_copy(value: Any, name: str, *, has_remote: bool, check: str | None) -> None:
    """Validate a package branch's remote copy against its repository's remote check."""
    if not has_remote:
        if value is not None:
            raise ProtocolError(f"{name} must be null without a remote")
        return
    copy = _object(value, name)
    _keys(copy, _REMOTE_COPY_KEYS, name)
    state = _choice(copy["state"], _REMOTE_COPY_STATES, f"{name}.state")
    present = state == "present"
    if (copy["sha"] is not None) != present or (copy["basis"] is not None) != present:
        raise ProtocolError(f"{name} sets sha and basis exactly for present")
    if present:
        _object_id(copy["sha"], f"{name}.sha")
        if _choice(copy["basis"], _COMPARISON_BASES, f"{name}.basis") not in _BRANCH_BASES[check]:
            raise ProtocolError(f"{name}.basis does not match the remote check")
    elif state == "unknown" and check in ("live", "recorded"):
        raise ProtocolError(f"{name} cannot be unknown under a {check} remote check")
    elif state == "absent" and check in ("off", "failed"):
        raise ProtocolError(f"{name} cannot be absent when the remote check is {check}")


def _clone(
    value: Any,
    name: str,
    *,
    has_remote: bool,
    check: str | None,
    lookup_used: bool,
    worktree_paths: set[str],
    git_version: str | None,
) -> dict[str, Any]:
    item = _object(value, name)
    _keys(item, _CLONE_KEYS, name)
    path = _text(item["path"], f"{name}.path")
    status = _choice(item["status"], _CLONE_STATUSES, f"{name}.status")
    facts = (
        "bare",
        "partial_clone",
        "shallow",
        "cached_default",
        "local_default",
        "cached_vs_remote",
        "stash_count",
        "worktrees",
        "branches",
        "verdict_base",
        "containment",
        "removable_count",
    )
    if status != "ok":
        if any(item[field] is not None for field in facts):
            raise ProtocolError(f"{name} must leave its facts null when status is {status}")
        return item
    bare = _bool(item["bare"], f"{name}.bare")
    _bool(item["partial_clone"], f"{name}.partial_clone")
    _bool(item["shallow"], f"{name}.shallow")
    _count(item["stash_count"], f"{name}.stash_count")
    _comparison(item["cached_vs_remote"], f"{name}.cached_vs_remote", check=check, nullable=True)
    _verdict_base(item["verdict_base"], f"{name}.verdict_base", check=check)
    # Without a live or saved answer there is nothing for the clone to compare with.
    answered = check in ("live", "recorded")
    if has_remote and item["cached_vs_remote"] is not None and not answered:
        raise ProtocolError(f"{name}.cached_vs_remote requires a live or recorded remote check")

    cached = item["cached_default"]
    local = item["local_default"]
    if not has_remote:
        if cached is not None or local is not None or item["cached_vs_remote"] is not None:
            raise ProtocolError(f"{name} has remote facts without a remote")
    else:
        cached = _object(cached, f"{name}.cached_default")
        _keys(cached, _CACHED_DEFAULT_KEYS, f"{name}.cached_default")
        cached_name = _nullable(cached["name"], _text, f"{name}.cached_default.name")
        cached_sha = _nullable(cached["sha"], _object_id, f"{name}.cached_default.sha")
        fetched = _nullable(
            cached["last_fetched_at"], _timestamp, f"{name}.cached_default.last_fetched_at"
        )
        since = _bool(cached["fetched_since_clone"], f"{name}.cached_default.fetched_since_clone")
        if cached_name is None and cached_sha is not None:
            raise ProtocolError(f"{name}.cached_default has a commit without a name")
        if since != (fetched is not None):
            raise ProtocolError(f"{name}.cached_default freshness is inconsistent")
        if local is not None:
            local = _object(local, f"{name}.local_default")
            _keys(local, _LOCAL_DEFAULT_KEYS, f"{name}.local_default")
            _text(local["name"], f"{name}.local_default.name")
            _nullable(local["sha"], _object_id, f"{name}.local_default.sha")
            _comparison(
                local["vs_remote"], f"{name}.local_default.vs_remote", check=check, nullable=True
            )
            if local["vs_remote"] is not None and not answered:
                raise ProtocolError(
                    f"{name}.local_default.vs_remote requires a live or recorded remote check"
                )
            _comparison(local["vs_cached"], f"{name}.local_default.vs_cached", check=check)

    worktrees = _list(item["worktrees"], f"{name}.worktrees")
    parsed = [
        _worktree(raw, f"{name}.worktrees[{index}]") for index, raw in enumerate(worktrees)
    ]
    _ordered(parsed, worktree_sort_key, f"{name}.worktrees")
    mains = [worktree for worktree in parsed if worktree["main"]]
    if bare:
        if mains:
            raise ProtocolError(f"{name} is bare but lists a main worktree")
    elif len(mains) != 1 or mains[0]["path"] != path:
        raise ProtocolError(f"{name} must list its own path as its one main worktree")
    for worktree in parsed:
        if worktree["path"] in worktree_paths:
            raise ProtocolError("worktree paths must be unique across the document")
        worktree_paths.add(worktree["path"])

    local_paths = {worktree["path"] for worktree in parsed}
    branches = _list(item["branches"], f"{name}.branches")
    if len(branches) > CHECKOUT_BRANCH_CAP:
        raise ProtocolError(f"{name} lists more than {CHECKOUT_BRANCH_CAP} branches")
    parsed_branches = [
        _branch(raw, f"{name}.branches[{index}]", has_remote=has_remote, check=check)
        for index, raw in enumerate(branches)
    ]
    _ordered(parsed_branches, branch_sort_key, f"{name}.branches")
    for branch in parsed_branches:
        if not set(branch["checked_out_in"]) <= local_paths:
            raise ProtocolError(f"{name} has a branch checked out in an unlisted worktree")
    _clone_verdicts(
        item,
        name,
        has_remote=has_remote,
        lookup_used=lookup_used,
        git_version=git_version,
        branches=parsed_branches,
        worktrees=parsed,
    )
    _clone_containment(item, name, git_version=git_version, branches=parsed_branches)
    _clone_removable(item, name, branches=parsed_branches, worktrees=parsed)
    return item


def github_identity(remote_key: str) -> tuple[str, str, str] | None:
    """Return ``(host, owner, repository)`` when a remote key names a GitHub repository.

    The host is ``github.com`` or a ``*.ghe.com`` host and the path is exactly
    ``owner/repository``; any other key is not a GitHub remote.
    """
    parts = remote_key.split("/")
    if len(parts) != 3 or not all(parts):
        return None
    host, owner, repository = parts
    if host == _GITHUB_HOST or (
        host.endswith(_GITHUB_ENTERPRISE_SUFFIX) and len(host) > len(_GITHUB_ENTERPRISE_SUFFIX)
    ):
        return host, owner, repository
    return None


def _pr_lookup(
    value: Any,
    name: str,
    *,
    remote_key: str,
    github_lookup: bool,
    remote_checks: bool,
) -> None:
    lookup = _object(value, name)
    _keys(lookup, _PR_LOOKUP_KEYS, name)
    status = _choice(lookup["status"], _PR_LOOKUP_STATUSES, f"{name}.status")
    reason = lookup["reason"]
    # Only a saved lookup carries the time it was taken.
    if status == "recorded":
        _timestamp(lookup["checked_at"], f"{name}.checked_at")
    elif lookup["checked_at"] is not None:
        raise ProtocolError(f"{name}.checked_at must be null")
    if not github_lookup:
        # With the setting off every lookup is off, whatever the remote is.
        if status != "off":
            raise ProtocolError(f"{name} must be off when the lookup setting is off")
        if reason != "setting_off":
            raise ProtocolError(f"{name}.reason must be setting_off when the setting is off")
        return
    github = github_identity(remote_key) is not None
    if (reason == "not_github") == github:
        raise ProtocolError(f"{name}.reason is not_github exactly when the remote is not GitHub's")
    failure = type(reason) is str and reason in _PR_LOOKUP_FAILURE_REASONS
    if remote_checks:
        if reason == "remote_checks_off":
            raise ProtocolError(f"{name}.reason cannot be remote_checks_off with remote checks on")
    else:
        # Without remote checks no call is made: the lookup is off, or it is a saved one.
        if status not in ("off", "recorded"):
            raise ProtocolError(f"{name} must be off or recorded with remote checks off")
        if failure:
            raise ProtocolError(f"{name}.reason cannot be a failure with remote checks off")
    if not github:
        valid = status == "off"
    elif not remote_checks:
        valid = reason == "remote_checks_off"
    elif status == "live":
        valid = reason is None
    else:
        valid = status in ("unavailable", "recorded") and failure
    if not valid:
        raise ProtocolError(f"{name} does not match the lookup setting and the remote")


def _repository(
    value: Any,
    index: int,
    clone_paths: set[str],
    worktree_paths: set[str],
    git_version: str | None,
    *,
    github_lookup: bool,
    remote_checks: bool,
) -> dict[str, Any]:
    name = f"repositories[{index}]"
    item = _object(value, name)
    _keys(item, _REPOSITORY_KEYS, name)
    remote = item["remote"]
    default_branch = item["default_branch"]
    check_status: str | None = None
    if remote is None:
        if default_branch is not None:
            raise ProtocolError(f"{name} has a default branch without a remote")
        if item["pr_lookup"] is not None:
            raise ProtocolError(f"{name} has a pull-request lookup without a remote")
    else:
        remote = _object(remote, f"{name}.remote")
        _keys(remote, _REMOTE_KEYS, f"{name}.remote")
        _text(remote["key"], f"{name}.remote.key")
        _text(remote["display"], f"{name}.remote.display")
        check = _object(remote["check"], f"{name}.remote.check")
        recorded = check.get("status") == "recorded"
        _keys(check, _CHECK_KEYS | {"checked_at"} if recorded else _CHECK_KEYS, f"{name}.remote.check")
        status = _choice(check["status"], _CHECK_STATUSES, f"{name}.remote.check.status")
        check_status = status
        reason = check["reason"]
        if status == "live":
            valid = reason is None
        elif status == "off":
            valid = reason == "remote_checks_off"
        elif status == "recorded":
            _timestamp(check["checked_at"], f"{name}.remote.check.checked_at")
            valid = type(reason) is str and (
                reason == "remote_checks_off" or reason in _CHECK_FAILURE_REASONS
            )
        else:
            valid = type(reason) is str and reason in _CHECK_FAILURE_REASONS
        if not valid:
            raise ProtocolError(f"{name}.remote.check.reason does not match its status")
        if remote_checks:
            if reason == "remote_checks_off":
                raise ProtocolError(
                    f"{name}.remote.check.reason cannot be remote_checks_off with remote checks on"
                )
        elif status not in ("off", "recorded"):
            raise ProtocolError(f"{name}.remote.check must be off or recorded with remote checks off")
        elif reason != "remote_checks_off":
            raise ProtocolError(
                f"{name}.remote.check.reason cannot be a failure with remote checks off"
            )
        default_branch = _object(default_branch, f"{name}.default_branch")
        _keys(default_branch, _DEFAULT_BRANCH_KEYS, f"{name}.default_branch")
        _nullable(default_branch["name"], _text, f"{name}.default_branch.name")
        default_sha = _nullable(default_branch["sha"], _object_id, f"{name}.default_branch.sha")
        if default_sha is not None and status not in ("live", "recorded"):
            raise ProtocolError(f"{name}.default_branch.sha requires a live or recorded remote check")
        _pr_lookup(
            item["pr_lookup"],
            f"{name}.pr_lookup",
            remote_key=remote["key"],
            github_lookup=github_lookup,
            remote_checks=remote_checks,
        )
    lookup_used = remote is not None and item["pr_lookup"]["status"] in ("live", "recorded")
    clones = _list(item["clones"], f"{name}.clones")
    if not clones or (remote is None and len(clones) != 1):
        raise ProtocolError(f"{name}.clones has the wrong size")
    parsed = [
        _clone(
            raw,
            f"{name}.clones[{clone_index}]",
            has_remote=remote is not None,
            check=check_status,
            lookup_used=lookup_used,
            worktree_paths=worktree_paths,
            git_version=git_version,
        )
        for clone_index, raw in enumerate(clones)
    ]
    _ordered(parsed, clone_sort_key, f"{name}.clones")
    entries = _list(item["package_branches"], f"{name}.package_branches")
    for entry_index, raw_entry in enumerate(entries):
        label = f"{name}.package_branches[{entry_index}]"
        entry = _object(raw_entry, label)
        _keys(entry, _PACKAGE_BRANCH_ENTRY_KEYS, label)
        _text(entry["name"], f"{label}.name")
        _remote_copy(
            entry["remote"], f"{label}.remote", has_remote=remote is not None, check=check_status
        )
        listed = _list(entry["packages"], f"{label}.packages")
        if not listed:
            raise ProtocolError(f"{label}.packages must not be empty")
        for path in listed:
            _package_path(path)
        _ordered(listed, lambda path: (path,), f"{label}.packages")
    _ordered(entries, branch_sort_key, f"{name}.package_branches")
    for clone in parsed:
        if clone["path"] in clone_paths:
            raise ProtocolError("clone paths must be unique across the document")
        clone_paths.add(clone["path"])
    return item


def package_sort_key(package: Mapping[str, Any]) -> tuple[Any, ...]:
    """Order package entries by ``package_path`` in code-point order."""
    return (package["package_path"],)


def _package(value: Any, name: str) -> dict[str, Any]:
    item = _object(value, name)
    _keys(item, _PACKAGE_KEYS, name)
    _package_path(item["package_path"])
    _uuid4(item["package_id"], f"{name}.package_id", nullable=True)
    _bool(item["finished"], f"{name}.finished")
    _bool(item["superseded"], f"{name}.superseded")
    target = _object(item["target"], f"{name}.target")
    _keys(target, _PACKAGE_TARGET_KEYS, f"{name}.target")
    status = _choice(target["status"], _PACKAGE_TARGET_STATUSES, f"{name}.target.status")
    for key in ("clone", "worktree"):
        _nullable(target[key], _text, f"{name}.target.{key}")
    if status not in ("clone", "worktree") and target["clone"] is not None:
        raise ProtocolError(f"{name}.target.clone must be null without a clone")
    if status != "worktree" and target["worktree"] is not None:
        raise ProtocolError(f"{name}.target.worktree must be null without a worktree")
    if status in ("clone", "worktree") and target["clone"] is None:
        raise ProtocolError(f"{name}.target.clone is required for {status}")
    if status == "worktree" and target["worktree"] is None:
        raise ProtocolError(f"{name}.target.worktree is required for worktree")
    branch = _object(item["branch"], f"{name}.branch")
    _keys(branch, _PACKAGE_BRANCH_KEYS, f"{name}.branch")
    branch_status = _choice(
        branch["status"], _PACKAGE_BRANCH_STATUSES, f"{name}.branch.status"
    )
    _nullable(branch["name"], _text, f"{name}.branch.name")
    if (branch["name"] is not None) != (branch_status == "declared"):
        raise ProtocolError(f"{name}.branch.name is set exactly for declared")
    _code_status(
        item["code_status"],
        f"{name}.code_status",
        allowed=branch_status == "declared" and status in ("clone", "worktree"),
    )
    return item


def _code_status(value: Any, name: str, *, allowed: bool) -> None:
    """Check one package's Code Status: every field's type, the fields' agreement with the
    state, and the text against the one text function."""
    if value is None:
        return
    if not allowed:
        raise ProtocolError(f"{name} requires a declared branch and a resolved target")
    item = _object(value, name)
    _keys(item, _CODE_STATUS_KEYS, name)
    state = _choice(item["state"], _CODE_STATUS_STATES, f"{name}.state")
    _string(item["text"], f"{name}.text")
    _nullable(item["push"], lambda raw, label: _choice(raw, _CODE_STATUS_PUSHES, label), f"{name}.push")
    for key in ("pull_request", "unpushed"):
        number = item[key]
        if number is not None and (type(number) is not int or number < 1):
            raise ProtocolError(f"{name}.{key} must be a positive integer or null")
    cleanup = item["cleanup"]
    if cleanup is not None:
        cleanup = _object(cleanup, f"{name}.cleanup")
        _keys(cleanup, _CLEANUP_KEYS, f"{name}.cleanup")
        for key in ("worktrees", "branches"):
            _count(cleanup[key], f"{name}.cleanup.{key}")
        if state == "merged" and (cleanup["worktrees"] > 0) == (cleanup["branches"] > 0):
            raise ProtocolError(
                f"{name}.cleanup must have exactly one positive count for merged"
            )
    paths = _list(item["checked_out_elsewhere"], f"{name}.checked_out_elsewhere")
    for index, path in enumerate(paths):
        _text(path, f"{name}.checked_out_elsewhere[{index}]")
    _ordered(paths, natural_key, f"{name}.checked_out_elsewhere")
    if (item["push"] is not None) != (state in ("in_progress", "pr_open")):
        raise ProtocolError(f"{name}.push is set exactly for in_progress and pr_open")
    if (item["pull_request"] is not None) != (state == "pr_open"):
        raise ProtocolError(f"{name}.pull_request is set exactly for pr_open")
    if (cleanup is not None) != (state == "merged"):
        raise ProtocolError(f"{name}.cleanup is set exactly for merged")
    if (item["unpushed"] is not None) != (item["push"] == "unpushed"):
        raise ProtocolError(f"{name}.unpushed is a count exactly when push is unpushed")
    if item["text"] != code_status_text(item):
        raise ProtocolError(f"{name}.text does not match its fields")


def _check_package_references(
    repositories: list[dict[str, Any]], packages: list[dict[str, Any]]
) -> None:
    """Refuse a branch reference or package-branch entry no listed package backs."""
    by_path = {package["package_path"]: package for package in packages}

    def package_named(path: str, label: str) -> dict[str, Any]:
        package = by_path.get(path)
        if package is None:
            raise ProtocolError(f"{label} names a package the document does not list")
        return package

    for repo_index, repository in enumerate(repositories):
        name = f"repositories[{repo_index}]"
        paths = {clone["path"] for clone in repository["clones"]}
        for entry_index, entry in enumerate(repository["package_branches"]):
            label = f"{name}.package_branches[{entry_index}]"
            for path in entry["packages"]:
                package = package_named(path, f"{label}.packages")
                if package["branch"]["name"] != entry["name"]:
                    raise ProtocolError(f"{label} lists a package that names another branch")
                if package["target"]["clone"] not in paths:
                    raise ProtocolError(f"{label} lists a package of another repository")
        for clone_index, clone in enumerate(repository["clones"]):
            if clone["branches"] is None:
                continue
            containers = {branch["name"]: branch["contained_in"] for branch in clone["branches"]}
            for branch_index, branch in enumerate(clone["branches"]):
                label = f"{name}.clones[{clone_index}].branches[{branch_index}].packages"
                chain = _contained_names(containers, branch["name"])
                for reference in branch["packages"]:
                    package = package_named(reference["package_path"], label)
                    target_clone = package["target"]["clone"]
                    named = package["branch"]["name"]
                    basis = reference["basis"]
                    if target_clone not in paths:
                        valid = False
                    elif basis == "target_clone":
                        valid = named == branch["name"] and target_clone == clone["path"]
                    elif basis == "sibling_clone":
                        valid = named == branch["name"] and target_clone != clone["path"]
                    else:
                        valid = named in chain
                    if not valid:
                        raise ProtocolError(f"{label} has a {basis} reference its package does not back")


def _contained_names(containers: Mapping[str, str | None], start: str) -> set[str]:
    """The branch names reached from ``start`` by following ``contained_in``."""
    reached: set[str] = set()
    name = containers.get(start)
    while name is not None and name not in reached and name != start:
        reached.add(name)
        name = containers.get(name)
    return reached


def parse_checkouts(payload: bytes | str | Mapping[str, Any]) -> dict[str, Any]:
    """Parse, strictly validate, and authenticate one checkout facts document."""
    if isinstance(payload, (bytes, str)):
        raw: Any = load_strict_json(payload, "checkouts")
    elif isinstance(payload, Mapping):
        raw = dict(payload)
    else:
        raise ProtocolError("checkouts must be a JSON object")
    document = _object(raw, "checkouts")
    _keys(document, _CHECKOUTS_KEYS, "checkouts")
    if type(document["schema_version"]) is not int or (
        document["schema_version"] != CHECKOUTS_SCHEMA_VERSION
    ):
        raise ProtocolError("unsupported schema version")
    digest = _string(document["checkouts_digest"], "checkouts_digest")
    if _SHA256_HEX.fullmatch(digest) is None:
        raise ProtocolError("invalid checkouts digest")
    _timestamp(document["observed_at"], "observed_at")

    git = _object(document["git"], "git")
    _keys(git, _GIT_KEYS, "git")
    git_status = _choice(git["status"], _GIT_STATUSES, "git.status")
    version = git["version"]
    if version is not None and (
        not _safe_text(version) or _GIT_VERSION.fullmatch(version) is None
    ):
        raise ProtocolError("git.version is invalid")
    if git_status == "missing" and version is not None:
        raise ProtocolError("git.version must be null when git is missing")
    if git_status == "available" and version is None:
        raise ProtocolError("git.version is required when git is available")
    remote_checks = _choice(document["remote_checks"], _REMOTE_CHECKS, "remote_checks")
    github_lookup = _choice(document["github_lookup"], _GITHUB_LOOKUP, "github_lookup")

    folders = _list(document["folders"], "folders")
    for index, raw_folder in enumerate(folders):
        folder = _object(raw_folder, f"folders[{index}]")
        _keys(folder, _FOLDER_KEYS, f"folders[{index}]")
        _text(folder["path"], f"folders[{index}].path")
        _choice(folder["status"], _FOLDER_STATUSES, f"folders[{index}].status")
    _ordered(folders, folder_sort_key, "folders")

    diagnostics = _list(document["diagnostics"], "diagnostics")
    for index, raw_diagnostic in enumerate(diagnostics):
        diagnostic = _object(raw_diagnostic, f"diagnostics[{index}]")
        _keys(diagnostic, _CHECKOUTS_DIAGNOSTIC_KEYS, f"diagnostics[{index}]")
        _choice(
            diagnostic["code"], CHECKOUTS_DIAGNOSTIC_CODES, f"diagnostics[{index}].code"
        )
        _nullable(diagnostic["path"], _text, f"diagnostics[{index}].path")
    _ordered(diagnostics, diagnostic_sort_key, "diagnostics")

    repositories = _list(document["repositories"], "repositories")
    if git_status != "available" and repositories:
        raise ProtocolError("repositories must be empty when git is unavailable")
    clone_paths: set[str] = set()
    worktree_paths: set[str] = set()
    remote_keys: set[str] = set()
    parsed = [
        _repository(
            raw_repository,
            index,
            clone_paths,
            worktree_paths,
            version,
            github_lookup=github_lookup == "on",
            remote_checks=remote_checks == "on",
        )
        for index, raw_repository in enumerate(repositories)
    ]
    for repository in parsed:
        if repository["remote"] is not None:
            key = repository["remote"]["key"]
            if key in remote_keys:
                raise ProtocolError("remote keys must be unique across the document")
            remote_keys.add(key)
    _ordered(parsed, repository_sort_key, "repositories")

    package_scan = _object(document["package_scan"], "package_scan")
    _keys(package_scan, _PACKAGE_SCAN_KEYS, "package_scan")
    scan_status = _choice(
        package_scan["status"], _PACKAGE_SCAN_STATUSES, "package_scan.status"
    )
    _choice(
        package_scan["finished_policy"], _FINISHED_POLICIES, "package_scan.finished_policy"
    )
    packages = _list(document["packages"], "packages")
    if scan_status == "unavailable" and packages:
        raise ProtocolError("packages must be empty when the package scan is unavailable")
    listed_worktrees: dict[str, set[str] | None] = {}
    for repository in parsed:
        for listed_clone in repository["clones"]:
            listed_worktrees[listed_clone["path"]] = (
                None
                if listed_clone["worktrees"] is None
                else {
                    item["path"] for item in listed_clone["worktrees"] if not item["main"]
                }
            )
    for index, raw_package in enumerate(packages):
        package = _package(raw_package, f"packages[{index}]")
        target = package["target"]
        if target["clone"] is not None and target["clone"] not in listed_worktrees:
            raise ProtocolError(f"packages[{index}].target.clone names no clone")
        if target["worktree"] is not None:
            linked = listed_worktrees[target["clone"]]
            if linked is None or target["worktree"] not in linked:
                raise ProtocolError(
                    f"packages[{index}].target.worktree is not listed under its clone"
                )
        if git_status != "available" and package["target"]["status"] in (
            "clone",
            "worktree",
            "missing",
            "not_git",
            "outside_folders",
        ):
            raise ProtocolError(
                f"packages[{index}].target.status requires git to be available"
            )
    _ordered(packages, package_sort_key, "packages")
    _check_package_references(parsed, packages)
    if canonical_digest(document, omit=CHECKOUTS_DIGEST_OMITTED) != digest:
        raise ProtocolError("checkouts digest mismatch")
    return dict(document)
