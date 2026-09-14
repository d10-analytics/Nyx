"""Dependency-light canonical catalog projection engine."""
from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

MAX_OUTPUT_BYTES = 2 * 1024 * 1024

CATALOG_LIFECYCLE_DIRECTORIES = {
    "Under_Development": "under_development",
    "Queue": "queue",
    "In_Progress": "in_progress",
    "Needs_Fixes": "needs_fixes",
    "Awaiting_Retrospective": "awaiting_retrospective",
    "Done": "done",
    "Archive": "archive",
}
CATALOG_BOARD_LIFECYCLES = {
    "under_development", "queue", "needs_fixes", "awaiting_retrospective",
}
CATALOG_HIDDEN_STAGES = sorted(
    directory
    for directory, lifecycle in CATALOG_LIFECYCLE_DIRECTORIES.items()
    if lifecycle not in CATALOG_BOARD_LIFECYCLES
)
CATALOG_DIAGNOSTIC_MESSAGES = {
    "invalid_package": "invalid package",
    "unreadable_anchor": "unreadable anchor",
    "nonregular_anchor": "nonregular anchor",
    "changed_during_read": "changed during read",
    "discovery_unavailable": "discovery unavailable",
    "invalid_package_id": "invalid package identity",
    "duplicate_package_id": "duplicate package identity",
    "invalid_claim": "invalid claim",
    "invalid_provenance": "invalid provenance",
    "duplicate_claim": "duplicate claim",
    "invalid_prerequisite": "invalid prerequisite",
    "invalid_program_membership": "invalid program membership",
    "invalid_superseded_by": "invalid successor declaration",
    "target_unreadable": "target unreadable",
    "target_changed_during_read": "target changed during read",
    "target_invalid_identity": "target has invalid identity",
    "missing_program_descriptor": "missing program descriptor",
    "duplicate_program_id": "duplicate program identity",
    "missing_successor": "missing successor target",
    "successor_cycle": "successor cycle detected",
    "relationship_cycle": "relationship cycle detected",
    "transitive_diagnostics_truncated": "transitive diagnostics truncated",
}

_CANONICAL_UUID = re.compile(
    r"(?<![0-9A-Fa-f])([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})(?![0-9A-Fa-f])"
)
_CLAIM_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_PROVENANCE = re.compile(
    r"^(?:git-object-sha1:[0-9a-f]{40}|git-object-sha256:[0-9a-f]{64}|sha256:[0-9a-f]{64})$"
)
_HEADER_FIELD = re.compile(
    r"^\s*\*{0,2}(Package ID|Program Membership|Superseded By|Prerequisite|Claim)\*{0,2}\s*:\s?(.*?)\s*$"
)

def _metadata_value(lines: list[str], label: str) -> str | None:
    pattern = re.compile(
        rf"^\s*\*{{0,2}}{re.escape(label)}\*{{0,2}}\s*:\s*(.*?)\s*$",
        re.IGNORECASE,
    )
    for line in lines:
        if line.startswith("## "):
            break
        match = pattern.match(line)
        if match:
            return match.group(1)
    return None


def _catalog_relative(path: Path, spec_root: Path) -> str:
    """Return the stable POSIX path exposed by the catalog protocol."""
    return path.relative_to(spec_root).as_posix()


def _catalog_diagnostic(code: str, package_path: str) -> dict[str, str]:
    return {
        "code": code,
        "message": f"{CATALOG_DIAGNOSTIC_MESSAGES[code]}: {package_path}",
    }


def _catalog_declared(lines: list[str]) -> dict[str, str | None]:
    title: str | None = None
    for line in lines:
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            title = match.group(1)
            break

    values: dict[str, str | None] = {
        "closure": _metadata_value(lines, "Closure"),
        "human_sanity_decision": _metadata_value(lines, "Human Sanity Decision"),
        "sanity_recommendation": _metadata_value(lines, "Sanity Recommendation"),
        "status": _metadata_value(lines, "Status"),
        "target_project": None,
        "title": title,
    }
    target_repo = _metadata_value(lines, "Target repo")
    if target_repo:
        try:
            target_path = Path(target_repo)
            if target_path.name and target_path.name not in {".", ".."}:
                values["target_project"] = target_path.name
        except (OSError, ValueError):
            pass
    return values


def _catalog_readable(path: Path, *, directory: bool = False) -> bool:
    """Honor ordinary permission bits even when the producer runs as root."""
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    read_bits = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
    if not mode & read_bits:
        return False
    if directory:
        execute_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        return bool(mode & execute_bits)
    return True


def _catalog_fingerprint(path: Path) -> tuple[int, int, int, int, int, int]:
    info = path.lstat()
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _catalog_read_anchor(anchor: Path) -> tuple[bytes | None, str | None]:
    """Read an anchor with one retry when its identity changes during the read."""
    last_data: bytes | None = None
    for _ in range(2):
        try:
            before = _catalog_fingerprint(anchor)
        except OSError:
            return None, "unreadable_anchor"
        if stat.S_ISLNK(before[2]):
            return None, "nonregular_anchor"
        if not stat.S_ISREG(before[2]):
            return None, "nonregular_anchor"
        if not _catalog_readable(anchor):
            return None, "unreadable_anchor"
        try:
            last_data = anchor.read_bytes()
            after = _catalog_fingerprint(anchor)
        except OSError:
            return None, "unreadable_anchor"
        if before == after:
            return last_data, None
    return last_data, "changed_during_read"


def _catalog_scan_root(spec_root: Path) -> list[os.DirEntry[str]]:
    """Return root entries while containing permission and scan failures."""
    if not _catalog_readable(spec_root, directory=True):
        raise ValueError("specification root cannot be read")
    try:
        return sorted(os.scandir(spec_root), key=lambda item: item.name)
    except OSError as error:
        raise ValueError("specification root cannot be read") from error


def _uuid(value: str) -> str | None:
    """Return a canonical lowercase UUIDv4, or None for an unsafe value."""
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        value,
    ):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return value if parsed.version == 4 and str(parsed) == value else None


def _uuid_candidates(value: str) -> set[str]:
    """Collect all canonical IDs even when a Package ID scalar is malformed."""
    return {
        token
        for token in _CANONICAL_UUID.findall(value)
        if _uuid(token) is not None
    }


def _diagnostic(code: str, package_path: str | None = None) -> dict[str, str]:
    message = CATALOG_DIAGNOSTIC_MESSAGES.get(code, "catalog diagnostic")
    if package_path:
        message = f"{message}: {package_path}"
    return {"code": code, "message": message}


def _sort_diagnostics(
    diagnostics: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Return diagnostics in the wire contract's canonical order."""
    return sorted(diagnostics, key=lambda item: (item["code"], item["message"]))


def _header(data: bytes) -> tuple[list[str], str | None]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [], "invalid_package"
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            break
        lines.append(line)
    return lines, None


def _parse_header(
    lines: list[str], package_path: str
) -> tuple[dict[str, object], set[str], list[dict[str, str]]]:
    """Parse only package-owned header rows and redact submitted invalid values."""
    scalar_values: dict[str, list[str]] = {
        "Package ID": [],
        "Program Membership": [],
        "Superseded By": [],
    }
    prereq_rows: list[str] = []
    claim_rows: list[str] = []
    diagnostics: list[dict[str, str]] = []
    for line in lines:
        match = _HEADER_FIELD.match(line)
        if not match:
            continue
        label, value = match.groups()
        if label in scalar_values:
            scalar_values[label].append(value)
        elif label == "Prerequisite":
            prereq_rows.append(value)
        else:
            claim_rows.append(value)

    package_candidates: set[str] = set()
    package_values = scalar_values["Package ID"]
    for value in package_values:
        package_candidates.update(_uuid_candidates(value))
    package_id: str | None = None
    package_identity_valid = len(package_values) == 1
    if not package_values:
        package_identity_valid = False
    elif package_identity_valid:
        package_id = _uuid(package_values[0].strip())
        package_identity_valid = package_id is not None
    if package_values and not package_identity_valid:
        diagnostics.append(_diagnostic("invalid_package_id", package_path))
    if len(package_values) > 1:
        diagnostics.append(_diagnostic("duplicate_package_id", package_path))

    membership: str | None = None
    membership_values = scalar_values["Program Membership"]
    if len(membership_values) == 1:
        membership = _uuid(membership_values[0].strip())
        if membership is None:
            diagnostics.append(_diagnostic("invalid_program_membership", package_path))
    elif membership_values:
        diagnostics.append(_diagnostic("invalid_program_membership", package_path))

    successor: str | None = None
    successor_values = scalar_values["Superseded By"]
    if len(successor_values) == 1:
        successor = _uuid(successor_values[0].strip())
        if successor is None or successor == package_id:
            successor = None
            diagnostics.append(_diagnostic("invalid_superseded_by", package_path))
    elif successor_values:
        diagnostics.append(_diagnostic("invalid_superseded_by", package_path))

    claims: dict[str, dict[str, object]] = {}
    parsed_prerequisites: list[dict[str, object]] = []
    malformed_prerequisite = False
    for value in claim_rows:
        parts = [part.strip() for part in value.split("|")]
        name = parts[0] if parts and _CLAIM_NAME.fullmatch(parts[0]) else None
        claim_diagnostic: list[dict[str, str]] = []
        state: str = "unknown"
        evidence_ref: str | None = None
        if len(parts) == 2 and parts[1] in {"unsatisfied", "unknown"}:
            state = parts[1]
        elif (
            len(parts) == 3
            and parts[1] == "satisfied"
            and _PROVENANCE.fullmatch(parts[2])
        ):
            state = "satisfied"
            evidence_ref = parts[2]
        elif len(parts) == 3 and len(parts) >= 2 and parts[1] == "satisfied":
            claim_diagnostic.append(_diagnostic("invalid_provenance", package_path))
        else:
            claim_diagnostic.append(_diagnostic("invalid_claim", package_path))
        if name is None:
            diagnostics.append(_diagnostic("invalid_claim", package_path))
            continue
        if name in claims:
            diagnostics.append(_diagnostic("duplicate_claim", package_path))
            claims[name]["state"] = "unknown"
            claims[name]["evidence_ref"] = None
            claims[name]["diagnostics"] = [_diagnostic("duplicate_claim", package_path)]
            continue
        if claim_diagnostic:
            diagnostics.extend(claim_diagnostic)
        claims[name] = {
            "name": name,
            "state": state if not claim_diagnostic else "unknown",
            "evidence_ref": evidence_ref if not claim_diagnostic else None,
            "diagnostics": claim_diagnostic,
        }

    for value in prereq_rows:
        parts = [part.strip() for part in value.split("|")]
        target_id = _uuid(parts[0]) if parts else None
        claim_name = (
            parts[1] if len(parts) == 2 and _CLAIM_NAME.fullmatch(parts[1]) else None
        )
        row_diagnostics: list[dict[str, str]] = []
        if len(parts) != 2 or target_id is None or claim_name is None:
            malformed_prerequisite = True
            row_diagnostics.append(_diagnostic("invalid_prerequisite", package_path))
            diagnostics.extend(row_diagnostics)
        parsed_prerequisites.append(
            {
                "target_package_id": target_id,
                "claim_name": claim_name,
                "diagnostics": row_diagnostics,
            }
        )

    relationship = {
        "participation": (
            "available"
            if package_identity_valid
            else "invalid"
            if package_values
            else "legacy"
        ),
        "claims": [claims[name] for name in sorted(claims)],
        "prerequisites": parsed_prerequisites,
        "direct_prerequisite_state": "unknown" if malformed_prerequisite else None,
        "program": {
            "program_id": membership,
            "title": None,
            "resolution": "unknown" if membership or membership_values else "not_declared",
            "diagnostics": (
                [_diagnostic("invalid_program_membership", package_path)]
                if membership_values and membership is None
                else []
            ),
        },
        "superseded_by": {
            "package_id": successor,
            "resolution": "unknown" if successor or successor_values else "not_declared",
            "diagnostics": (
                [_diagnostic("invalid_superseded_by", package_path)]
                if successor_values and successor is None
                else []
            ),
        },
    }
    if relationship["direct_prerequisite_state"] is None:
        relationship["direct_prerequisite_state"] = (
            "no_declared_prerequisites" if package_identity_valid and not parsed_prerequisites else None
        )
    return relationship, package_candidates, diagnostics


def _empty_declared() -> dict[str, str | None]:
    return {
        "title": None,
        "target_project": None,
        "status": None,
        "closure": None,
        "sanity_recommendation": None,
        "human_sanity_decision": None,
    }


_PROGRAM_FIELD = re.compile(
    r"^\s*\*{0,2}(Program ID|Program Title)\*{0,2}\s*:\s?(.*?)\s*$"
)


def _program_candidate_ids(value: str) -> set[str]:
    return _uuid_candidates(value)


def _parse_program_descriptor(
    lines: list[str], directory_id: str | None, program_path: str
) -> tuple[dict[str, object], set[str], list[dict[str, str]]]:
    values: dict[str, list[str]] = {"Program ID": [], "Program Title": []}
    for line in lines:
        match = _PROGRAM_FIELD.match(line)
        if match:
            values[match.group(1)].append(match.group(2))

    candidates: set[str] = set()
    for value in values["Program ID"]:
        candidates.update(_program_candidate_ids(value))
    diagnostics: list[dict[str, str]] = []
    has_forbidden_content = any(
        line.strip() and not _PROGRAM_FIELD.match(line) for line in lines
    )
    program_id: str | None = None
    title: str | None = None
    valid = (
        len(values["Program ID"]) == 1
        and len(values["Program Title"]) == 1
        and not has_forbidden_content
    )
    if valid:
        program_id = _uuid(values["Program ID"][0].strip())
        title_value = values["Program Title"][0]
        if (
            program_id is None
            or directory_id is None
            or program_id != directory_id
            or not 1 <= len(title_value) <= 120
            or any(ord(character) < 32 or ord(character) == 127 for character in title_value)
        ):
            valid = False
        else:
            title = title_value
    if not valid:
        diagnostics.append(_diagnostic("invalid_package", program_path))
    return (
        {
            "program_id": program_id,
            "title": title,
            "program_path": program_path,
            "candidate_ids": candidates,
            "diagnostics": diagnostics,
            "state": "partial" if diagnostics else "complete",
        },
        candidates,
        diagnostics,
    )


def _scan_programs(
    repository_path: Path,
    spec_root: Path,
    programs: list[dict[str, object]],
    diagnostics: list[dict[str, str]],
) -> None:
    namespace = repository_path / "Reference" / "Programs"
    try:
        mode = namespace.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError:
        diagnostics.append(_diagnostic("discovery_unavailable", _catalog_relative(namespace, spec_root)))
        return
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode) or not _catalog_readable(namespace, directory=True):
        diagnostics.append(_diagnostic("discovery_unavailable", _catalog_relative(namespace, spec_root)))
        return
    try:
        children = sorted(os.scandir(namespace), key=lambda item: item.name)
    except OSError:
        diagnostics.append(_diagnostic("discovery_unavailable", _catalog_relative(namespace, spec_root)))
        return
    for child in children:
        child_path = Path(child.path)
        program_path = _catalog_relative(child_path, spec_root)
        # Only a canonical UUID directory owns a program descriptor.  Other
        # Reference/Programs material is unrelated reference content and must
        # not make an otherwise complete program namespace incomplete.
        directory_id = _uuid(child.name)
        if directory_id is None:
            continue
        try:
            if child.is_symlink() or not child.is_dir(follow_symlinks=False):
                diagnostics.append(_diagnostic("invalid_package", program_path))
                continue
        except OSError:
            diagnostics.append(_catalog_diagnostic("discovery_unavailable", program_path))
            continue
        if not _catalog_readable(child_path, directory=True):
            diagnostics.append(_catalog_diagnostic("discovery_unavailable", program_path))
            continue
        descriptor = child_path / "program.md"
        try:
            descriptor_mode = descriptor.lstat().st_mode
        except OSError:
            diagnostics.append(_catalog_diagnostic("discovery_unavailable", _catalog_relative(descriptor, spec_root)))
            continue
        if stat.S_ISLNK(descriptor_mode) or not stat.S_ISREG(descriptor_mode):
            diagnostics.append(_catalog_diagnostic("invalid_package", program_path))
            continue
        data, read_diagnostic = _catalog_read_anchor(descriptor)
        if data is None:
            diagnostics.append(_catalog_diagnostic(read_diagnostic or "unreadable_anchor", program_path))
            continue
        try:
            lines = data.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            diagnostics.append(_catalog_diagnostic("invalid_package", program_path))
            continue
        parsed, candidates, parsed_diagnostics = _parse_program_descriptor(
            lines, directory_id, program_path
        )
        parsed["candidate_ids"] = candidates
        parsed["read_diagnostic"] = read_diagnostic
        if read_diagnostic:
            parsed["diagnostics"].append(_catalog_diagnostic(read_diagnostic, program_path))
            parsed["state"] = "partial"
            diagnostics.append(_catalog_diagnostic(read_diagnostic, program_path))
        programs.append(parsed)
        diagnostics.extend(parsed_diagnostics)


def _program_index(
    descriptors: list[dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    index: dict[str, list[dict[str, object]]] = {}
    for descriptor in descriptors:
        program_id = descriptor.get("program_id")
        if program_id is not None:
            index.setdefault(str(program_id), []).append(descriptor)
    return index


def _resolve_program(
    relationship: dict[str, object],
    package_path: str,
    program_index: dict[str, list[dict[str, object]]],
    program_coverage_complete: bool,
) -> None:
    context = relationship["program"]
    program_id = context["program_id"]
    if program_id is None:
        return
    if not program_coverage_complete:
        context["resolution"] = "unknown"
        context["diagnostics"] = [_catalog_diagnostic("discovery_unavailable", package_path)]
        return
    candidates = program_index.get(program_id, [])
    if not candidates:
        context["resolution"] = "unknown"
        context["diagnostics"] = [_diagnostic("missing_program_descriptor", package_path)]
    elif len(candidates) > 1:
        context["resolution"] = "unknown"
        context["diagnostics"] = [_diagnostic("duplicate_program_id", package_path)]
    else:
        descriptor = candidates[0]
        if descriptor["diagnostics"]:
            context["resolution"] = "unknown"
            context["diagnostics"] = [_diagnostic("invalid_package", package_path)]
        else:
            context["resolution"] = "resolved"
            context["title"] = descriptor["title"]


def _resolve_successor(
    relationship: dict[str, object],
    package_id: str | None,
    package_path: str,
    index: dict[str, list[dict[str, object]]],
    identity_complete: bool,
) -> None:
    context = relationship["superseded_by"]
    successor_id = context["package_id"]
    if successor_id is None:
        return
    if not identity_complete:
        context["resolution"] = "unknown"
        context["diagnostics"] = [
            _catalog_diagnostic("discovery_unavailable", package_path)
        ]
        return
    candidates = index.get(successor_id, [])
    if not candidates:
        context["resolution"] = "unknown"
        context["diagnostics"] = [_diagnostic("missing_successor", package_path)]
        return
    if len(candidates) > 1:
        context["resolution"] = "unknown"
        context["diagnostics"] = [_diagnostic("duplicate_package_id", package_path)]
        return
    target = candidates[0]
    if target.get("read_diagnostic") in {"unreadable_anchor", "nonregular_anchor", "changed_during_read"}:
        context["resolution"] = "unknown"
        context["diagnostics"] = [_diagnostic("invalid_superseded_by", package_path)]
        return
    if target.get("package_id") != successor_id or target.get("relationship") is None:
        context["resolution"] = "unknown"
        context["diagnostics"] = [_diagnostic("invalid_superseded_by", package_path)]
        return
    context["resolution"] = "resolved"

    seen: set[str] = set()
    current = successor_id
    while current is not None and current not in seen:
        seen.add(current)
        next_candidates = index.get(current, [])
        if len(next_candidates) != 1:
            break
        next_relationship = next_candidates[0].get("relationship")
        next_context = next_relationship.get("superseded_by") if next_relationship else None
        current = next_context.get("package_id") if next_context else None
    if current is not None and current in seen:
        context["diagnostics"] = [_diagnostic("successor_cycle", package_path)]


def _transitive_diagnostics(
    record: dict[str, object],
    index: dict[str, list[dict[str, object]]],
    identity_complete: bool,
) -> list[dict[str, object]]:
    found: dict[tuple[str, str, tuple[str, ...]], dict[str, object]] = {}

    def diagnostic_code(edge_reason: str) -> str:
        return {
            "duplicate_target": "duplicate_package_id",
            "identity_coverage_incomplete": "discovery_unavailable",
            "target_unreadable": "unreadable_anchor",
            "target_changed_during_read": "changed_during_read",
            "target_invalid_identity": "invalid_package_id",
            "missing_claim": "invalid_claim",
            "claim_unknown": "invalid_claim",
            "invalid_claim": "invalid_claim",
            "self_edge": "relationship_cycle",
            "invalid_prerequisite": "invalid_prerequisite",
            "missing_target": "invalid_prerequisite",
        }.get(edge_reason, "invalid_prerequisite")

    def add(origin: str | None, code: str, path: tuple[str, ...]) -> None:
        if origin is None:
            return
        key = (origin, code, path)
        if key in found:
            return
        if len(found) >= 65:
            largest = max(found)
            if key >= largest:
                return
            del found[largest]
        found[key] = {
            "origin_package_id": origin,
            "code": code,
            "path_package_ids": list(path),
        }

    root_id = record.get("package_id")
    if root_id is None:
        return []
    stack: list[tuple[dict[str, object], tuple[str, ...]]] = [
        (record, (str(root_id),))
    ]
    while stack:
        current, path = stack.pop()
        relationship = current.get("relationship")
        current_id = current.get("package_id")
        if not relationship or current_id is None:
            continue
        for row in reversed(relationship["prerequisites"]):
            target_id = row["target_package_id"]
            edge = _edge(current, row, index, identity_complete)
            edge_reason = str(edge["reason"])
            edge_path = path + ((str(target_id),) if target_id is not None else ())
            if edge_reason not in {"claim_satisfied", "claim_unsatisfied"}:
                add(str(current_id), diagnostic_code(edge_reason), edge_path)
            if target_id is None:
                continue
            candidates = index.get(target_id, [])
            if len(candidates) != 1:
                continue
            target = candidates[0]
            if target_id in path:
                add(str(target_id), "relationship_cycle", edge_path)
                continue
            stack.append((target, edge_path))
    ordered = [found[key] for key in sorted(found)]
    if len(ordered) > 64:
        ordered = ordered[:64]
        ordered.append(
            {
                "code": "transitive_diagnostics_truncated",
            }
        )
    return ordered


def _scan_stage(
    stage_path: Path,
    lifecycle: str,
    spec_root: Path,
    records: list[dict[str, object]],
    discovery_diagnostics: list[dict[str, str]],
    full_validity: Callable[[Path, list[str]], bool] | None = None,
) -> None:
    pending = [stage_path]
    while pending:
        current = pending.pop()
        if not _catalog_readable(current, directory=True):
            discovery_diagnostics.append(
                _catalog_diagnostic("discovery_unavailable", _catalog_relative(current, spec_root))
            )
            continue
        try:
            children = sorted(os.scandir(current), key=lambda item: item.name)
        except OSError:
            discovery_diagnostics.append(
                _catalog_diagnostic("discovery_unavailable", _catalog_relative(current, spec_root))
            )
            continue
        for child in children:
            child_path = Path(child.path)
            if child.name == "spec.md":
                package_path = _catalog_relative(current, spec_root)
                if child.is_symlink() or not child.is_file(follow_symlinks=False):
                    records.append(
                        {
                            "package_path": package_path,
                            "lifecycle": lifecycle,
                            "data": None,
                            "read_diagnostic": "nonregular_anchor",
                            "declared": _empty_declared(),
                            "relationship": None,
                            "candidate_ids": set(),
                            "package_id": None,
                            "diagnostics": [_catalog_diagnostic("nonregular_anchor", package_path)],
                            "state": "partial",
                        }
                    )
                else:
                    data, read_diagnostic = _catalog_read_anchor(child_path)
                    declared = _empty_declared()
                    relationship = None
                    candidates: set[str] = set()
                    diagnostics: list[dict[str, str]] = []
                    if data is not None:
                        lines, decode_diagnostic = _header(data)
                        if lines:
                            declared = _catalog_declared(lines)
                            relationship, candidates, diagnostics = _parse_header(lines, package_path)
                            if full_validity is not None:
                                try:
                                    if full_validity(child_path.parent, lines):
                                        diagnostics.append(_catalog_diagnostic("invalid_package", package_path))
                                except (OSError, UnicodeError, ValueError):
                                    diagnostics.append(_catalog_diagnostic("invalid_package", package_path))
                        else:
                            diagnostics.append(_catalog_diagnostic(decode_diagnostic or "invalid_package", package_path))
                    if read_diagnostic:
                        diagnostics.append(_catalog_diagnostic(read_diagnostic, package_path))
                    package_id = (
                        next(iter(candidates))
                        if relationship
                        and relationship["participation"] == "available"
                        and len(candidates) == 1
                        else None
                    )
                    records.append(
                        {
                            "package_path": package_path,
                            "lifecycle": lifecycle,
                            "data": data,
                            "read_diagnostic": read_diagnostic or decode_diagnostic if data is not None else "unreadable_anchor",
                            "declared": declared,
                            "relationship": relationship,
                            "candidate_ids": candidates,
                            "package_id": package_id,
                            "diagnostics": diagnostics,
                            "state": "partial" if diagnostics else "complete",
                        }
                    )
                continue
            try:
                if child.name != ".pipeline" and child.is_dir(follow_symlinks=False):
                    pending.append(child_path)
            except OSError:
                discovery_diagnostics.append(
                    _catalog_diagnostic("discovery_unavailable", _catalog_relative(child_path, spec_root))
                )


def _edge(
    source: dict[str, object],
    row: dict[str, object],
    index: dict[str, list[dict[str, object]]],
    identity_complete: bool,
) -> dict[str, object]:
    target_id = row["target_package_id"]
    claim_name = row["claim_name"]
    edge: dict[str, object] = {
        "target_package_id": target_id,
        "claim_name": claim_name,
        "observed_state": None,
        "observed_evidence_ref": None,
        "resolved_state": "unknown",
        "reason": "invalid_prerequisite",
    }
    if target_id is None or claim_name is None:
        return edge
    candidates = index.get(target_id, [])
    if not candidates:
        edge["reason"] = (
            "identity_coverage_incomplete" if not identity_complete else "missing_target"
        )
        return edge
    if len(candidates) > 1:
        edge["reason"] = "duplicate_target"
        return edge
    target = candidates[0]
    target_relationship = target.get("relationship")
    if target.get("read_diagnostic") in {"unreadable_anchor", "nonregular_anchor"}:
        edge["reason"] = "target_unreadable"
        return edge
    if target.get("read_diagnostic") == "changed_during_read":
        edge["reason"] = "target_changed_during_read"
        return edge
    if not identity_complete:
        edge["reason"] = "identity_coverage_incomplete"
    elif target.get("package_id") != target_id or not target_relationship:
        edge["reason"] = "target_invalid_identity"
    elif target is source:
        claim = next(
            (
                item
                for item in target_relationship["claims"]
                if item["name"] == claim_name
            ),
            None,
        )
        if claim is not None:
            edge["observed_state"] = claim["state"]
            edge["observed_evidence_ref"] = claim["evidence_ref"]
        edge["reason"] = "self_edge"
    else:
        claim = next(
            (
                item
                for item in target_relationship["claims"]
                if item["name"] == claim_name
            ),
            None,
        )
        if claim is None:
            edge["reason"] = "missing_claim"
        else:
            edge["observed_state"] = claim["state"]
            edge["observed_evidence_ref"] = claim["evidence_ref"]
            if claim["diagnostics"]:
                edge["reason"] = "invalid_claim"
            elif claim["state"] == "satisfied":
                edge["resolved_state"] = "satisfied"
                edge["reason"] = "claim_satisfied"
            elif claim["state"] == "unsatisfied":
                edge["resolved_state"] = "unsatisfied"
                edge["reason"] = "claim_unsatisfied"
            else:
                edge["reason"] = "claim_unknown"
    return edge


def _render_entry(
    record: dict[str, object],
    index: dict[str, list[dict[str, object]]],
    identity_complete: bool,
    program_index: dict[str, list[dict[str, object]]],
    program_coverage_complete: bool,
) -> dict[str, object]:
    relationship = record["relationship"]
    if relationship is None:
        relationship = {
            "participation": "legacy",
            "claims": [],
            "prerequisites": [],
            "direct_prerequisite_state": "relationship_unavailable",
            "program": {
                "program_id": None,
                "title": None,
                "resolution": "not_declared",
                "diagnostics": [],
            },
            "superseded_by": {
                "package_id": None,
                "resolution": "not_declared",
                "diagnostics": [],
            },
        }
    _resolve_program(
        relationship,
        str(record["package_path"]),
        program_index,
        program_coverage_complete,
    )
    _resolve_successor(
        relationship,
        record.get("package_id"),
        str(record["package_path"]),
        index,
        identity_complete,
    )
    prerequisites = relationship["prerequisites"]
    rendered_edges = [
        _edge(record, row, index, identity_complete) for row in prerequisites
    ]
    rendered_edges.sort(
        key=lambda edge: (
            str(edge["target_package_id"] or ""),
            str(edge["claim_name"] or ""),
        )
    )
    if relationship["participation"] != "available":
        direct_state = "relationship_unavailable"
    elif not rendered_edges:
        direct_state = "no_declared_prerequisites"
    elif any(edge["resolved_state"] == "unknown" for edge in rendered_edges):
        direct_state = "unknown"
    elif any(edge["resolved_state"] == "unsatisfied" for edge in rendered_edges):
        direct_state = "unsatisfied"
    else:
        direct_state = "satisfied"
    rendered_claims = []
    for claim in relationship["claims"]:
        rendered_claim = dict(claim)
        rendered_claim["diagnostics"] = _sort_diagnostics(
            list(claim["diagnostics"])
        )
        rendered_claims.append(rendered_claim)
    rendered_claims.sort(key=lambda claim: str(claim["name"]))
    rendered_program = dict(relationship["program"])
    rendered_program["diagnostics"] = _sort_diagnostics(
        list(relationship["program"]["diagnostics"])
    )
    rendered_successor = dict(relationship["superseded_by"])
    rendered_successor["diagnostics"] = _sort_diagnostics(
        list(relationship["superseded_by"]["diagnostics"])
    )
    relationship_out = {
        "participation": relationship["participation"],
        "claims": rendered_claims,
        "prerequisites": rendered_edges,
        "direct_prerequisite_state": direct_state,
        "program": rendered_program,
        "superseded_by": rendered_successor,
    }
    diagnostics = _sort_diagnostics(list(record["diagnostics"]))
    package_path = str(record["package_path"])
    path_parts = package_path.split("/")
    project, stage = path_parts[0], path_parts[1]
    return {
        "package_id": record["package_id"],
        "package_path": package_path,
        "project": project,
        "stage": stage,
        "board_visible": record["lifecycle"] in CATALOG_BOARD_LIFECYCLES,
        "state": record["state"],
        "declared": record["declared"],
        "diagnostics": diagnostics,
        "relationship": relationship_out,
        "transitive_diagnostics": _transitive_diagnostics(
            record, index, identity_complete
        ),
    }


def _build_catalog(
    spec_root: Path,
    full_validity: Callable[[Path, list[str]], bool] | None = None,
) -> str:
    """Build the relationship catalog from one captured scan."""
    if not spec_root.exists() or not spec_root.is_dir():
        raise ValueError(f"specification root is not a directory: {spec_root}")
    records: list[dict[str, object]] = []
    discovery_diagnostics: list[dict[str, str]] = []
    program_descriptors: list[dict[str, object]] = []
    program_diagnostics: list[dict[str, str]] = []
    for repository in _catalog_scan_root(spec_root):
        try:
            if not repository.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        repository_path = Path(repository.path)
        _scan_programs(
            repository_path, spec_root, program_descriptors, program_diagnostics
        )
        for directory, lifecycle in CATALOG_LIFECYCLE_DIRECTORIES.items():
            stage_path = repository_path / directory
            try:
                mode = stage_path.lstat().st_mode
            except FileNotFoundError:
                continue
            except OSError:
                discovery_diagnostics.append(
                    _catalog_diagnostic("discovery_unavailable", _catalog_relative(stage_path, spec_root))
                )
                continue
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                discovery_diagnostics.append(
                    _catalog_diagnostic("discovery_unavailable", _catalog_relative(stage_path, spec_root))
                )
                continue
            _scan_stage(
                stage_path,
                lifecycle,
                spec_root,
                records,
                discovery_diagnostics,
                full_validity,
            )

    index: dict[str, list[dict[str, object]]] = {}
    for record in records:
        for candidate in record["candidate_ids"]:
            index.setdefault(candidate, []).append(record)
    for candidate, candidates in index.items():
        if len(candidates) > 1:
            for record in candidates:
                record["diagnostics"].append(
                    _diagnostic("duplicate_package_id", record["package_path"])
                )
                record["state"] = "partial"
    discovery_diagnostics.sort(key=lambda item: (item["code"], item["message"]))
    identity_diagnostics = list(discovery_diagnostics)
    for record in records:
        if record["read_diagnostic"] in {
            "unreadable_anchor",
            "nonregular_anchor",
            "changed_during_read",
            "invalid_package",
        }:
            identity_diagnostics.append(
                _diagnostic(record["read_diagnostic"], record["package_path"])
            )
    identity_diagnostics.sort(key=lambda item: (item["code"], item["message"]))
    identity_complete = not identity_diagnostics

    program_index = _program_index(program_descriptors)
    for candidate, candidates in program_index.items():
        if len(candidates) > 1:
            for descriptor in candidates:
                descriptor["diagnostics"].append(
                    _diagnostic("duplicate_program_id", descriptor["program_path"])
                )
                descriptor["state"] = "partial"
            program_diagnostics.extend(
                _diagnostic("duplicate_program_id", descriptor["program_path"])
                for descriptor in candidates
            )
    program_diagnostics.sort(key=lambda item: (item["code"], item["message"]))
    program_coverage_complete = not program_diagnostics
    for record in records:
        relationship = record.get("relationship")
        if relationship is None:
            continue
        program_id = relationship["program"].get("program_id")
        if program_id is not None:
            for descriptor in program_index.get(program_id, []):
                descriptor.setdefault("member_package_ids", []).append(record.get("package_id"))

    board = [record for record in records if record["lifecycle"] in CATALOG_BOARD_LIFECYCLES]
    referenced_ids = {
        row["target_package_id"]
        for record in board
        for row in (record["relationship"]["prerequisites"] if record["relationship"] else [])
        if row["target_package_id"] is not None
    }
    projected = list(board)
    for target_id in sorted(referenced_ids):
        for record in index.get(target_id, []):
            if record not in projected:
                projected.append(record)
    projected.sort(key=lambda record: str(record["package_path"]))
    entries = [
        _render_entry(
            record,
            index,
            identity_complete,
            program_index,
            program_coverage_complete,
        )
        for record in projected
    ]
    emitted_program_ids = {
        record["relationship"]["program"]["program_id"]
        for record in projected
        if record.get("relationship")
        and record["relationship"]["program"].get("program_id") is not None
    }
    rendered_programs = []
    for descriptor in sorted(
        (
            item
            for item in program_descriptors
            if item.get("program_id") is not None
            and not item.get("diagnostics")
            and item["program_id"] in emitted_program_ids
        ),
        key=lambda item: str(item["program_id"]),
    ):
        rendered_programs.append(
            {
                "program_id": descriptor["program_id"],
                "title": descriptor["title"],
                "member_package_ids": sorted(
                    member
                    for member in descriptor.get("member_package_ids", [])
                    if member is not None
                ),
                "diagnostics": [],
            }
        )
    catalog: dict[str, object] = {
        "schema_version": 3,
        "catalog_digest": None,
        "visibility": {
            "hidden_stages": CATALOG_HIDDEN_STAGES,
            "visible_entry_count": len(board),
            "hidden_entry_count": len(records) - len(board),
        },
        "identity_coverage": {"state": "complete" if identity_complete else "incomplete", "diagnostics": identity_diagnostics},
        "program_coverage": {
            "state": "complete" if program_coverage_complete else "incomplete",
            "diagnostics": program_diagnostics,
        },
        "discovery_diagnostics": discovery_diagnostics,
        "entries": entries,
        "programs": rendered_programs,
    }
    digest_input = dict(catalog)
    digest_input.pop("catalog_digest")
    catalog["catalog_digest"] = sha256(
        json.dumps(digest_input, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    rendered = json.dumps(catalog, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if len(rendered.encode("utf-8")) + 1 > MAX_OUTPUT_BYTES:
        raise ValueError("catalog output exceeds 2 MiB")
    return rendered


def build_catalog(spec_root: Path) -> str:
    """Build the deterministic catalog without shared validation hooks."""
    return _build_catalog(spec_root)


def scan_catalog(
    spec_root: Path,
    *,
    _full_validity: Callable[[Path, list[str]], bool] | None = None,
) -> str:
    """Build the deterministic catalog with the private validity hook."""
    return _build_catalog(spec_root, _full_validity)
