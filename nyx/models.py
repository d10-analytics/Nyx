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
# Checkout facts document (schema 2)
# ---------------------------------------------------------------------------

CHECKOUTS_SCHEMA_VERSION = 2
CHECKOUTS_DIGEST_OMITTED = ("checkouts_digest", "observed_at")

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
        "folders",
        "repositories",
        "diagnostics",
    }
)
_GIT_KEYS = frozenset({"status", "version"})
_GIT_STATUSES = frozenset({"available", "missing", "too_old"})
_REMOTE_CHECKS = frozenset({"on", "off"})
_FOLDER_KEYS = frozenset({"path", "status"})
_FOLDER_STATUSES = frozenset({"ok", "missing", "unreadable", "not_directory"})
_CHECKOUTS_DIAGNOSTIC_KEYS = frozenset({"code", "path"})
CHECKOUTS_DIAGNOSTIC_CODES = frozenset({"not_a_repository", "unrepresentable_path"})
_REPOSITORY_KEYS = frozenset({"remote", "default_branch", "clones"})
_REMOTE_KEYS = frozenset({"key", "display", "check"})
_CHECK_KEYS = frozenset({"status", "reason"})
_CHECK_STATUSES = frozenset({"live", "failed", "off"})
_CHECK_FAILURE_REASONS = frozenset(
    {"unreachable", "not_found", "authentication_failed", "timed_out", "failed"}
)
_DEFAULT_BRANCH_KEYS = frozenset({"name", "live_sha"})
_CLONE_KEYS = frozenset(
    {
        "path",
        "bare",
        "status",
        "partial_clone",
        "shallow",
        "cached_default",
        "local_default",
        "cached_vs_live",
        "stash_count",
        "worktrees",
        "branches",
        "verdict_base",
    }
)
_ITEM_STATUSES = frozenset({"ok", "unsafe_ownership", "failed", "timed_out"})
_CACHED_DEFAULT_KEYS = frozenset({"name", "sha", "last_fetched_at", "fetched_since_clone"})
_LOCAL_DEFAULT_KEYS = frozenset({"name", "sha", "vs_live", "vs_cached"})
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
    }
)
_HEAD_KEYS = frozenset({"branch", "sha"})
_CHANGE_KEYS = frozenset({"staged", "unstaged", "untracked", "conflicted"})
OPERATIONS = ("am", "bisect", "cherry_pick", "merge", "rebase", "revert")
_BRANCH_KEYS = frozenset(
    {"name", "sha", "checked_out_in", "upstream", "upstream_mismatch", "vs_remote", "verdict"}
)
_COMPARISON_KEYS = frozenset({"basis", "relation", "ahead", "behind", "objects_from"})
_COMPARISON_BASES = frozenset({"live", "cached"})
_VERDICT_BASE_KEYS = frozenset({"name", "sha", "basis", "objects_from"})
_VERDICT_KEYS = frozenset({"state", "rung", "reason"})
_VERDICT_STATES = frozenset({"merged", "not_merged", "unknown"})
_VERDICT_MERGED_RUNGS = frozenset(
    {"ancestor", "patch_equivalent", "squash_equivalent", "tree_equal"}
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


def _comparison(value: Any, name: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    item = _object(value, name)
    _keys(item, _COMPARISON_KEYS, name)
    _choice(item["basis"], _COMPARISON_BASES, f"{name}.basis")
    relation = _choice(item["relation"], _COMPARISON_RELATIONS, f"{name}.relation")
    ahead, behind = item["ahead"], item["behind"]
    _nullable(item["objects_from"], _text, f"{name}.objects_from")
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


def _verdict_base(value: Any, name: str) -> None:
    if value is None:
        return
    item = _object(value, name)
    _keys(item, _VERDICT_BASE_KEYS, name)
    _text(item["name"], f"{name}.name")
    _object_id(item["sha"], f"{name}.sha")
    basis = _choice(item["basis"], _COMPARISON_BASES, f"{name}.basis")
    lender = _nullable(item["objects_from"], _text, f"{name}.objects_from")
    if lender is not None and basis != "live":
        raise ProtocolError(f"{name}.objects_from requires the live basis")


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


def _branch(value: Any, name: str, *, has_remote: bool) -> dict[str, Any]:
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
    _comparison(item["vs_remote"], f"{name}.vs_remote", nullable=True)
    if not has_remote and (item["vs_remote"] is not None or mismatch):
        raise ProtocolError(f"{name} compares with a remote the clone does not have")
    if has_remote and item["vs_remote"] is None:
        raise ProtocolError(f"{name}.vs_remote is required for a clone with a remote")
    _verdict(item["verdict"], f"{name}.verdict", nullable=True)
    return item


def _clone(value: Any, name: str, *, has_remote: bool, worktree_paths: set[str]) -> dict[str, Any]:
    item = _object(value, name)
    _keys(item, _CLONE_KEYS, name)
    path = _text(item["path"], f"{name}.path")
    status = _choice(item["status"], _ITEM_STATUSES, f"{name}.status")
    facts = (
        "bare",
        "partial_clone",
        "shallow",
        "cached_default",
        "local_default",
        "cached_vs_live",
        "stash_count",
        "worktrees",
        "branches",
        "verdict_base",
    )
    if status != "ok":
        if any(item[field] is not None for field in facts):
            raise ProtocolError(f"{name} must leave its facts null when status is {status}")
        return item
    bare = _bool(item["bare"], f"{name}.bare")
    _bool(item["partial_clone"], f"{name}.partial_clone")
    _bool(item["shallow"], f"{name}.shallow")
    _count(item["stash_count"], f"{name}.stash_count")
    _comparison(item["cached_vs_live"], f"{name}.cached_vs_live", nullable=True)
    _verdict_base(item["verdict_base"], f"{name}.verdict_base")

    cached = item["cached_default"]
    local = item["local_default"]
    if not has_remote:
        if cached is not None or local is not None or item["cached_vs_live"] is not None:
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
            _comparison(local["vs_live"], f"{name}.local_default.vs_live", nullable=True)
            _comparison(local["vs_cached"], f"{name}.local_default.vs_cached")

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
    parsed_branches = [
        _branch(raw, f"{name}.branches[{index}]", has_remote=has_remote)
        for index, raw in enumerate(branches)
    ]
    _ordered(parsed_branches, branch_sort_key, f"{name}.branches")
    for branch in parsed_branches:
        if not set(branch["checked_out_in"]) <= local_paths:
            raise ProtocolError(f"{name} has a branch checked out in an unlisted worktree")
    return item


def _repository(value: Any, index: int, clone_paths: set[str], worktree_paths: set[str]) -> dict[str, Any]:
    name = f"repositories[{index}]"
    item = _object(value, name)
    _keys(item, _REPOSITORY_KEYS, name)
    remote = item["remote"]
    default_branch = item["default_branch"]
    if remote is None:
        if default_branch is not None:
            raise ProtocolError(f"{name} has a default branch without a remote")
    else:
        remote = _object(remote, f"{name}.remote")
        _keys(remote, _REMOTE_KEYS, f"{name}.remote")
        _text(remote["key"], f"{name}.remote.key")
        _text(remote["display"], f"{name}.remote.display")
        check = _object(remote["check"], f"{name}.remote.check")
        _keys(check, _CHECK_KEYS, f"{name}.remote.check")
        status = _choice(check["status"], _CHECK_STATUSES, f"{name}.remote.check.status")
        reason = check["reason"]
        if status == "live":
            valid = reason is None
        elif status == "off":
            valid = reason == "remote_checks_off"
        else:
            valid = type(reason) is str and reason in _CHECK_FAILURE_REASONS
        if not valid:
            raise ProtocolError(f"{name}.remote.check.reason does not match its status")
        default_branch = _object(default_branch, f"{name}.default_branch")
        _keys(default_branch, _DEFAULT_BRANCH_KEYS, f"{name}.default_branch")
        _nullable(default_branch["name"], _text, f"{name}.default_branch.name")
        _nullable(default_branch["live_sha"], _object_id, f"{name}.default_branch.live_sha")
    clones = _list(item["clones"], f"{name}.clones")
    if not clones or (remote is None and len(clones) != 1):
        raise ProtocolError(f"{name}.clones has the wrong size")
    parsed = [
        _clone(
            raw,
            f"{name}.clones[{clone_index}]",
            has_remote=remote is not None,
            worktree_paths=worktree_paths,
        )
        for clone_index, raw in enumerate(clones)
    ]
    _ordered(parsed, clone_sort_key, f"{name}.clones")
    for clone in parsed:
        if clone["path"] in clone_paths:
            raise ProtocolError("clone paths must be unique across the document")
        clone_paths.add(clone["path"])
    return item


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
    _choice(document["remote_checks"], _REMOTE_CHECKS, "remote_checks")

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
        _repository(raw_repository, index, clone_paths, worktree_paths)
        for index, raw_repository in enumerate(repositories)
    ]
    for repository in parsed:
        if repository["remote"] is not None:
            key = repository["remote"]["key"]
            if key in remote_keys:
                raise ProtocolError("remote keys must be unique across the document")
            remote_keys.add(key)
    _ordered(parsed, repository_sort_key, "repositories")
    if canonical_digest(document, omit=CHECKOUTS_DIGEST_OMITTED) != digest:
        raise ProtocolError("checkouts digest mismatch")
    return dict(document)
