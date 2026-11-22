"""Shared specification header parser.

Reads the header of a specification anchor: the bounded UTF-8 text before the
first ``## `` item heading, the single-value metadata lines, and the
package-owned relationship rows (``Package ID``, ``Program Membership``,
``Superseded By``, ``Prerequisite``, ``Completion Prerequisite`` and
``Claim``).  Also holds the lexical rules of a git branch name and the retired
relationship labels the package validator rejects in a live package.  Depends
only on the standard library; every function is pure.
"""
from __future__ import annotations

import re
import uuid

_HEADER_BYTE_LIMIT = 65536
_UTF8_BOM = b"\xef\xbb\xbf"
# Byte forms of every separator recognized by str.splitlines(); a header cut
# must follow one of them (or start the file after an optional BOM).
_HEADER_CUT_SEPARATORS = (
    b"\r", b"\n", b"\x0b", b"\x0c", b"\x1c", b"\x1d", b"\x1e",
    b"\xc2\x85", b"\xe2\x80\xa8", b"\xe2\x80\xa9",
)
_CANONICAL_UUID = re.compile(
    r"(?<![0-9A-Fa-f])([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})(?![0-9A-Fa-f])"
)
_CLAIM_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_PROVENANCE = re.compile(
    r"^(?:git-object-sha1:[0-9a-f]{40}|git-object-sha256:[0-9a-f]{64}|sha256:[0-9a-f]{64})$"
)
_HEADER_LABELS = (
    "Package ID", "Program Membership", "Superseded By", "Prerequisite",
    "Completion Prerequisite", "Claim",
)
# Retired relationship labels: the first has a replacement row, the second is
# deleted.  This tuple is their only definition, for the validator rule that
# rejects them in a live package.
RETIRED_HEADER_LABELS = ("Prerequisite", "Claim")
_HEADER_FIELD = re.compile(
    r"^\s*\*{0,2}(" + "|".join(_HEADER_LABELS) + r")\*{0,2}\s*:\s?(.*)"
)
_BRANCH_FORBIDDEN = frozenset(" ~^:?*[\\`<>")
_BRANCH_FORBIDDEN_SEQUENCES = ("..", "@{", "//")


def metadata_values(lines: list[str], label: str) -> list[str]:
    """Return every value of a ``Label:`` metadata line before the first item heading.

    The value is trimmed with ``rstrip`` rather than a trailing-whitespace
    pattern, which backtracks quadratically over a long run of spaces.
    """
    pattern = re.compile(
        rf"^\s*\*{{0,2}}{re.escape(label)}\*{{0,2}}\s*:\s*(.*)",
        re.IGNORECASE,
    )
    values: list[str] = []
    for line in lines:
        if line.startswith("## "):
            break
        if match := pattern.match(line):
            values.append(match.group(1).rstrip())
    return values


def metadata_value(lines: list[str], label: str) -> str | None:
    """Return the first value of a ``Label:`` metadata line, or None."""
    values = metadata_values(lines, label)
    return values[0] if values else None


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


def _catalog_header_cut(data: bytes) -> int | None:
    """Return the byte offset of the header cut, or None when there is none."""
    start = len(_UTF8_BOM) if data.startswith(_UTF8_BOM) else 0
    if data[start:start + 3] == b"## ":
        return start
    index = data.find(b"## ", start)
    while index != -1:
        for separator in _HEADER_CUT_SEPARATORS:
            if index >= len(separator) and data[index - len(separator):index] == separator:
                return index
        index = data.find(b"## ", index + 1)
    return None


def _catalog_anchor_text(data: bytes, *, cut: bool) -> str | None:
    """Return the bounded anchor's UTF-8 text, or None when it is invalid.

    A single leading BOM is stripped, the header bound is enforced over the
    original byte offsets, and item headers are cut at the byte-level boundary
    before the strict decode so body bytes never affect the header.
    """
    if cut:
        offset = _catalog_header_cut(data)
        if offset is None:
            if len(data) > _HEADER_BYTE_LIMIT:
                return None
            header = data
        elif offset > _HEADER_BYTE_LIMIT:
            return None
        else:
            header = data[:offset]
    else:
        if len(data) > _HEADER_BYTE_LIMIT:
            return None
        header = data
    if header.startswith(_UTF8_BOM):
        header = header[len(_UTF8_BOM):]
    try:
        return header.decode("utf-8")
    except UnicodeDecodeError:
        return None


def read_header(data: bytes) -> tuple[list[str], str | None]:
    text = _catalog_anchor_text(data, cut=True)
    if text is None:
        return [], "invalid_package"
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            break
        lines.append(line)
    return lines, None


def parse_header(
    lines: list[str]
) -> tuple[dict[str, object], set[str]]:
    """Parse only package-owned header rows and redact submitted invalid values."""
    scalar_values: dict[str, list[str]] = {
        "Package ID": [],
        "Program Membership": [],
        "Superseded By": [],
    }
    prereq_rows: list[str] = []
    completion_rows: list[str] = []
    claim_rows: list[str] = []
    for line in lines:
        match = _HEADER_FIELD.match(line)
        if not match:
            continue
        label, value = match.groups()
        value = value.rstrip()
        if label in scalar_values:
            scalar_values[label].append(value)
        elif label == "Prerequisite":
            prereq_rows.append(value)
        elif label == "Completion Prerequisite":
            completion_rows.append(value)
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
    membership: str | None = None
    membership_values = scalar_values["Program Membership"]
    if len(membership_values) == 1:
        membership = _uuid(membership_values[0].strip())

    successor: str | None = None
    successor_values = scalar_values["Superseded By"]
    if len(successor_values) == 1:
        successor = _uuid(successor_values[0].strip())
        if successor is None or successor == package_id:
            successor = None

    claims: dict[str, dict[str, object]] = {}
    parsed_prerequisites: list[dict[str, object]] = []
    malformed_prerequisite = False
    for value in claim_rows:
        parts = [part.strip() for part in value.split("|")]
        name = parts[0] if parts and _CLAIM_NAME.fullmatch(parts[0]) else None
        invalid = False
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
        else:
            invalid = True
        if name is None:
            continue
        if name in claims:
            claims[name]["state"] = "unknown"
            claims[name]["evidence_ref"] = None
            claims[name]["invalid"] = True
            continue
        claims[name] = {
            "name": name,
            "state": state if not invalid else "unknown",
            "evidence_ref": evidence_ref if not invalid else None,
            "invalid": invalid,
        }

    for value in prereq_rows:
        parts = [part.strip() for part in value.split("|")]
        target_id = _uuid(parts[0]) if parts else None
        claim_name = (
            parts[1] if len(parts) == 2 and _CLAIM_NAME.fullmatch(parts[1]) else None
        )
        if len(parts) != 2 or target_id is None or claim_name is None:
            malformed_prerequisite = True
        parsed_prerequisites.append(
            {
                "kind": "claim",
                "target_package_id": target_id,
                "claim_name": claim_name,
                "valid": len(parts) == 2 and target_id is not None and claim_name is not None,
            }
        )

    for value in completion_rows:
        target_id = _uuid(value.strip()) if "|" not in value else None
        valid = len(value.strip()) > 0 and target_id is not None and "|" not in value
        if not valid:
            malformed_prerequisite = True
        parsed_prerequisites.append(
            {
                "kind": "completion",
                "target_package_id": target_id,
                "valid": valid,
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
        },
        "superseded_by": {
            "package_id": successor,
            "resolution": "unknown" if successor or successor_values else "not_declared",
        },
    }
    if relationship["direct_prerequisite_state"] is None:
        relationship["direct_prerequisite_state"] = (
            "no_declared_prerequisites" if package_identity_valid and not parsed_prerequisites else None
        )
    return relationship, package_candidates


def branch_name_is_lexical(value: str) -> bool:
    """Return whether ``value`` passes the lexical rules of a git branch name.

    Rejects control characters, the forbidden characters and sequences, a lone
    ``@``, a leading ``-`` or ``/``, a trailing ``/`` or ``.``, and any path
    component that starts with ``.`` or ends with ``.lock``.  It applies no
    length bound and no Markdown normalisation.
    """
    if any(
        ord(character) < 32 or ord(character) == 127 or character in _BRANCH_FORBIDDEN
        for character in value
    ):
        return False
    if any(sequence in value for sequence in _BRANCH_FORBIDDEN_SEQUENCES):
        return False
    if value == "@" or value.startswith(("-", "/")) or value.endswith(("/", ".")):
        return False
    return not any(
        component.startswith(".") or component.endswith(".lock")
        for component in value.split("/")
    )
