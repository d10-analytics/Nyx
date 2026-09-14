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
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

SCHEMA_VERSION = 2

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

DECLARED_FIELDS = (
    "title",
    "target_project",
    "status",
    "closure",
    "sanity_recommendation",
    "human_sanity_decision",
)

DIAGNOSTIC_CODES = frozenset(
    {
        "invalid_package",
        "unreadable_anchor",
        "nonregular_anchor",
        "changed_during_read",
        "discovery_unavailable",
        "invalid_package_id",
        "duplicate_package_id",
        "invalid_claim",
        "invalid_provenance",
        "duplicate_claim",
        "invalid_prerequisite",
        "invalid_program_membership",
        "invalid_superseded_by",
        "target_unreadable",
        "target_changed_during_read",
        "target_invalid_identity",
        "missing_program_descriptor",
        "duplicate_program_id",
        "missing_successor",
        "successor_cycle",
        "relationship_cycle",
        "transitive_diagnostics_truncated",
    }
)

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "catalog_digest",
        "identity_coverage",
        "program_coverage",
        "discovery_diagnostics",
        "entries",
        "programs",
    }
)
_ENTRY_KEYS = frozenset(
    {
        "package_id",
        "package_path",
        "lifecycle",
        "state",
        "declared",
        "diagnostics",
        "relationship",
        "transitive_diagnostics",
    }
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
        "target_package_id",
        "claim_name",
        "observed_state",
        "observed_evidence_ref",
        "resolved_state",
        "reason",
    }
)
_PROGRAM_KEYS = frozenset({"program_id", "title", "resolution", "diagnostics"})
_COVERAGE_KEYS = frozenset({"state", "diagnostics"})
_CLAIM_KEYS = frozenset({"name", "state", "evidence_ref", "diagnostics"})
_SUCCESSOR_KEYS = frozenset({"package_id", "resolution", "diagnostics"})
_CATALOG_PROGRAM_KEYS = frozenset({"program_id", "title", "member_package_ids", "diagnostics"})
_TRANSITIVE_DIAGNOSTIC_KEYS = frozenset({"origin_package_id", "code", "path_package_ids"})
_DIAGNOSTIC_KEYS = frozenset({"code", "message"})
_STATES = frozenset({"complete", "partial"})
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
    lifecycle: str
    declared: dict[str, str | None]
    diagnostics: tuple[Diagnostic, ...]
    state: str = "complete"
    package_id: str | None = None
    program_id: str | None = None
    program_title: str | None = None
    prerequisites: tuple[dict[str, Any], ...] = ()
    direct_prerequisite_state: str = "relationship_unavailable"

    def as_dict(self) -> dict[str, Any]:
        return {
            "declared": dict(self.declared),
            "diagnostics": [item.as_dict() for item in self.diagnostics],
            "lifecycle": self.lifecycle,
            "package_id": self.package_id,
            "package_path": self.package_path,
            "prerequisites": [dict(edge) for edge in self.prerequisites],
            "direct_prerequisite_state": self.direct_prerequisite_state,
            "program_id": self.program_id,
            "program_title": self.program_title,
            "state": self.state,
        }


@dataclass(frozen=True)
class Catalog:
    entries: tuple[CatalogEntry, ...]
    discovery_diagnostics: tuple[Diagnostic, ...]
    catalog_digest: str
    schema_version: int = SCHEMA_VERSION

    def as_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "discovery_diagnostics": [
                item.as_dict() for item in self.discovery_diagnostics
            ],
            "entries": [item.as_dict() for item in self.entries],
            "schema_version": self.schema_version,
        }
        if include_digest:
            result["catalog_digest"] = self.catalog_digest
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


def canonical_digest(value: Mapping[str, Any]) -> str:
    """Compute the producer-compatible digest, omitting its digest field."""
    payload = dict(value)
    payload.pop("catalog_digest", None)
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
        if (
            code not in DIAGNOSTIC_CODES
            or not message
            or len(message) > 1024
            or any(ord(c) < 32 or ord(c) == 127 for c in code + message)
        ):
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
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
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
        _diagnostics(item["diagnostics"], f"{item_name}.diagnostics")
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
    _diagnostics(item["diagnostics"], f"{name}.diagnostics")


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
    _diagnostics(item["diagnostics"], f"{name}.diagnostics")
    return program_id


def _transitive_diagnostics(value: Any, name: str) -> None:
    if type(value) is not list:
        raise ProtocolError(f"{name} must be a list")
    for index, raw in enumerate(value):
        item_name = f"{name}[{index}]"
        item = _object(raw, item_name)
        if set(item) == {"code"}:
            if item["code"] != "transitive_diagnostics_truncated":
                raise ProtocolError(f"{item_name} has an invalid shape")
            continue
        _keys(item, _TRANSITIVE_DIAGNOSTIC_KEYS, item_name)
        _uuid4(item["origin_package_id"], f"{item_name}.origin_package_id")
        code = _string(item["code"], f"{item_name}.code")
        if code not in DIAGNOSTIC_CODES:
            raise ProtocolError(f"{item_name}.code is invalid")
        path_ids = item["path_package_ids"]
        if type(path_ids) is not list:
            raise ProtocolError(f"{item_name}.path_package_ids must be a list")
        for path_index, package_id in enumerate(path_ids):
            _uuid4(package_id, f"{item_name}.path_package_ids[{path_index}]")


def _edge(value: Any, name: str) -> dict[str, Any]:
    item = _object(value, name)
    _keys(item, _EDGE_KEYS, name)
    target = _uuid4(item["target_package_id"], f"{name}.target_package_id", nullable=True)
    claim_name = _claim_name(item["claim_name"], f"{name}.claim_name", nullable=True)
    observed_state = _string(item["observed_state"], f"{name}.observed_state", nullable=True)
    if observed_state is not None and observed_state not in _OBSERVED_STATES:
        raise ProtocolError(f"{name}.observed_state is invalid")
    _provenance(item["observed_evidence_ref"], f"{name}.observed_evidence_ref", nullable=True)
    resolved_state = _string(item["resolved_state"], f"{name}.resolved_state")
    if resolved_state not in _OBSERVED_STATES:
        raise ProtocolError(f"{name}.resolved_state is invalid")
    reason = _string(item["reason"], f"{name}.reason")
    if reason not in _EDGE_REASONS:
        raise ProtocolError(f"{name}.reason is invalid")
    return {
        "target_package_id": target,
        "claim_name": claim_name,
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
    _diagnostics(item["diagnostics"], f"{name}.diagnostics")
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
        (edge["target_package_id"] or "", edge["claim_name"] or "")
        for edge in prerequisites
    ]
    if edge_sort != sorted(edge_sort):
        raise ProtocolError(f"{name}.prerequisites must be sorted")
    if participation != "available":
        return None, None, (), "relationship_unavailable"
    return program_id, program_title, prerequisites, direct_state


def _entry(value: Any, index: int) -> CatalogEntry:
    item = _object(value, f"entries[{index}]")
    _keys(item, _ENTRY_KEYS, f"entries[{index}]")
    package_id = _uuid4(item["package_id"], f"entries[{index}].package_id", nullable=True)
    lifecycle = _string(item["lifecycle"], f"entries[{index}].lifecycle")
    state = _string(item["state"], f"entries[{index}].state")
    if lifecycle not in LIFECYCLES or state not in _STATES:
        raise ProtocolError(f"entries[{index}] has an invalid lifecycle or state")
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
    _transitive_diagnostics(
        item["transitive_diagnostics"], f"entries[{index}].transitive_diagnostics"
    )
    return CatalogEntry(
        package_path=_package_path(item["package_path"]),
        lifecycle=lifecycle,
        declared=declared_values,
        diagnostics=_diagnostics(item["diagnostics"], f"entries[{index}].diagnostics"),
        state=state,
        package_id=package_id,
        program_id=program_id,
        program_title=program_title,
        prerequisites=prerequisites,
        direct_prerequisite_state=direct_state,
    )


def parse_catalog(payload: bytes | str | Mapping[str, Any]) -> Catalog:
    """Parse, strictly validate, and authenticate one producer response."""
    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ProtocolError("catalog is not UTF-8") from error
        try:
            raw: Any = json.loads(
                text, object_pairs_hook=_strict_pairs, parse_constant=_reject_constant
            )
        except (json.JSONDecodeError, RecursionError) as error:
            raise ProtocolError("catalog is not valid JSON") from error
    elif isinstance(payload, str):
        try:
            raw = json.loads(
                payload, object_pairs_hook=_strict_pairs, parse_constant=_reject_constant
            )
        except (json.JSONDecodeError, RecursionError) as error:
            raise ProtocolError("catalog is not valid JSON") from error
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
    for name in ("identity_coverage", "program_coverage"):
        _coverage(catalog[name], name)
    if type(catalog["entries"]) is not list:
        raise ProtocolError("entries must be a list")
    entries = tuple(_entry(item, index) for index, item in enumerate(catalog["entries"]))
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
    )
