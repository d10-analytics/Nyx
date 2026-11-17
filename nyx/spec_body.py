"""Section, table, definition, reference and task parsing for specification text.

Pure helpers over the lines of a specification: section bodies, Markdown
tables, stable-ID definitions and references, task blocks and file-boundary
rows, and the path helpers the verification rules share.  Depends only on the
standard library; every function is pure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

ID_PATTERN = re.compile(
    r"(?<![A-Z0-9-])(R-\d+|AR-\d+|F-\d+|D-\d+|AC-\d+|T\d+)(?![A-Z0-9-])"
)


STABLE_ID_RANGE = re.compile(
    r"`?(?P<prefix>AR|AC|R|F|D)-(?P<start>\d+)`?\s*"
    r"(?:through|to|[-–—])\s*"
    r"`?(?P=prefix)-(?P<end>\d+)`?",
    re.IGNORECASE,
)


TASK_HEADING = re.compile(r"^###\s+(T\d+)\s+[—-]\s+(.+?)\s*$")


REFERENCE_TOKEN = re.compile(r"(?<![A-Za-z0-9-])((?:AR|R|F|D)-\d+)(?![A-Za-z0-9-])")


# Anchored at column 0: an indented line that begins with an ID is a wrapped
# continuation or a nested note, never a new definition.
DEFINITION_LINE = re.compile(
    r"^(?:-\s+(?:\[[ xX]\]\s+)?)?(?:\*\*|`)?"
    r"((?:AC|AR|R|F|D)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)"
    r"(?:\*\*|`)?\s*(?:[—–:-]|$)"
)


TABLE_SEPARATOR = re.compile(r"^\|[\s:|-]+\|\s*$")


TASK_DEPENDS = re.compile(r"^-\s+Depends on:\s*(.+?)\s*$", re.IGNORECASE)


TASK_OWNS = re.compile(r"^-\s+Owns:\s*(.+?)\s*$", re.IGNORECASE)


TASK_BOUNDARIES = re.compile(r"^-\s+Boundaries:\s*(.+?)\s*$", re.IGNORECASE)


@dataclass
class Task:
    task_id: str
    dependencies: set[str] = field(default_factory=set)
    owns: set[str] = field(default_factory=set)
    boundaries: set[str] = field(default_factory=set)


def _parse_csv_ids(value: str, prefix: str) -> set[str]:
    if value.strip().lower() == "none":
        return set()
    return {item for item in ID_PATTERN.findall(value) if item.startswith(prefix)}


def _parse_boundaries(value: str) -> set[str]:
    return {match.strip() for match in re.findall(r"`([^`]+)`", value)}


def parse_tasks(lines: list[str]) -> dict[str, Task]:
    tasks: dict[str, Task] = {}
    current: Task | None = None
    collecting_boundaries = False
    for line in lines:
        heading = TASK_HEADING.match(line)
        if heading:
            current = Task(heading.group(1))
            tasks[current.task_id] = current
            collecting_boundaries = False
            continue
        if current is None:
            continue
        if line.startswith("## ") or (line.startswith("### ") and not heading):
            current = None
            collecting_boundaries = False
            continue
        if match := TASK_DEPENDS.match(line):
            current.dependencies = _parse_csv_ids(match.group(1), "T")
            collecting_boundaries = False
        elif match := TASK_OWNS.match(line):
            current.owns = _parse_csv_ids(match.group(1), "AC-")
            collecting_boundaries = False
        elif match := TASK_BOUNDARIES.match(line):
            current.boundaries = _parse_boundaries(match.group(1))
            collecting_boundaries = True
        elif collecting_boundaries and line.startswith(("  ", "\t")):
            current.boundaries.update(_parse_boundaries(line))
        elif line.startswith("- ") or not line.strip():
            collecting_boundaries = False
    return tasks


def strip_blanks(lines: list[str]) -> list[str]:
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def section_body(lines: list[str], heading: str) -> list[str]:
    body: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == heading:
            inside = True
            continue
        if inside:
            if line.startswith("## "):
                break
            body.append(line)
    return strip_blanks(body)


def task_block(lines: list[str], task_id: str) -> list[str]:
    block: list[str] = []
    inside = False
    for line in lines:
        heading = TASK_HEADING.match(line)
        if heading:
            if heading.group(1) == task_id:
                inside = True
                block.append(line)
                continue
            if inside:
                break
        if inside:
            if line.startswith(("## ", "### ")):
                break
            block.append(line)
    return strip_blanks(block)


def collect_definitions(lines: list[str]) -> dict[str, list[str]]:
    """Map every `AC-NN`/`D-NN`/`F-NN`/`R-NN`/`AR-NN` ID to its defining block.

    A block runs from its column-0 definition line to the next definition,
    heading, or `---` rule, and keeps any table inside it; a table outside
    every block is ignored.
    """
    definitions: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or stripped == "---":
            current = None
            continue
        match = DEFINITION_LINE.match(line)
        if match:
            current = match.group(1)
            definitions.setdefault(current, []).append(line.rstrip())
            continue
        if current:
            definitions[current].append(line.rstrip())
    return {key: strip_blanks(list(value)) for key, value in definitions.items()}


def referenced_ids(text: str) -> list[str]:
    ordered: list[str] = []
    for match in STABLE_ID_RANGE.finditer(text):
        prefix = match.group("prefix").upper()
        start = int(match.group("start"))
        end = int(match.group("end"))
        if end < start:
            continue
        width = max(len(match.group("start")), len(match.group("end")))
        for number in range(start, end + 1):
            identifier = f"{prefix}-{number:0{width}d}"
            if identifier not in ordered and not identifier.startswith("AC-"):
                ordered.append(identifier)
    for match in REFERENCE_TOKEN.finditer(text):
        identifier = match.group(1)
        if identifier not in ordered:
            ordered.append(identifier)
    return ordered


def transitive_referenced_ids(
    text: str,
    definitions: dict[str, list[str]],
    excluded: set[str] | None = None,
) -> list[str]:
    """Expand references found inside referenced definition blocks to closure."""
    excluded = excluded or set()
    ordered: list[str] = []
    pending = list(referenced_ids(text))
    while pending:
        identifier = pending.pop(0)
        if identifier in excluded or identifier in ordered:
            continue
        ordered.append(identifier)
        if identifier not in definitions:
            continue
        nested = referenced_ids("\n".join(definitions[identifier]))
        for reference in nested:
            if (
                reference != identifier
                and reference not in excluded
                and reference not in ordered
            ):
                pending.append(reference)
    return ordered


def table_rows(lines: list[str], header: str) -> list[list[str]]:
    for index, line in enumerate(lines):
        if line.rstrip() != header:
            continue
        rows: list[list[str]] = []
        candidates = lines[index + 1 :]
        for offset, candidate in enumerate(candidates):
            stripped = candidate.rstrip()
            if not stripped.startswith("|"):
                if rows:
                    break
                if not stripped:
                    continue
                break
            if TABLE_SEPARATOR.match(stripped):
                continue
            following = (
                candidates[offset + 1].rstrip() if offset + 1 < len(candidates) else ""
            )
            if rows and TABLE_SEPARATOR.match(following):
                break
            rows.append([cell.strip() for cell in stripped.strip("|").split("|")])
        return rows
    return []


def explicit_authority_path(token: str) -> bool:
    source = token.split(":", 1)[0]
    return "/" in source or bool(
        re.search(r"\.(?:cfg|ini|json|md|py|toml|txt|ya?ml)$", source)
    )


def source_path_tokens(text: str) -> set[str]:
    paths: set[str] = set()
    for token in re.findall(r"`([^`]+)`", text):
        # Inline code also carries exact shell commands in verification tables.
        # A source-path reference is a single token; treating a command that
        # merely contains a slash as a path can make instruction discovery walk
        # an arbitrary (and potentially overlong) pseudo-directory name.
        if any(character.isspace() for character in token):
            continue
        if explicit_authority_path(token):
            paths.add(re.sub(r":\d+(?:-\d+)?$", "", token))
    return paths


def source_path_citations(text: str) -> list[tuple[str, int | None, int | None]]:
    citations: list[tuple[str, int | None, int | None]] = []
    for token in re.findall(r"`([^`]+)`", text):
        if not explicit_authority_path(token):
            continue
        match = re.match(r"^(.*?):(\d+)(?:-(\d+))?$", token)
        if match:
            citations.append(
                (
                    match.group(1),
                    int(match.group(2)),
                    int(match.group(3) or match.group(2)),
                )
            )
        else:
            citations.append((token, None, None))
    return citations


def boundary_paths(
    lines: list[str], tasks: dict[str, Task], contract: str = "3"
) -> tuple[dict[str, set[str]], dict[tuple[str, str], str], list[str]]:
    header = "| Task | File | Access and purpose |"
    section = section_body(lines, "## File boundaries")
    rows = table_rows(section, header)
    if not rows:
        return (
            {},
            {},
            ["file boundaries must use: " + header],
        )
    writable: dict[str, set[str]] = {}
    purposes: dict[tuple[str, str], str] = {}
    listed: dict[str, set[str]] = {}
    errors: list[str] = []
    for row in rows:
        if len(row) != 3:
            errors.append("malformed file-boundary row")
            continue
        task_ids = [item for item in ID_PATTERN.findall(row[0]) if item.startswith("T")]
        paths = _parse_boundaries(row[1])
        if len(task_ids) != 1 or len(paths) != 1:
            errors.append("each file-boundary row must name one task and one file")
            continue
        task_id = task_ids[0]
        listed.setdefault(task_id, set()).update(paths)
        if row[2].lower().startswith("write:"):
            if not re.search(r"[A-Za-z0-9]", row[2][len("write:") :]):
                errors.append(f"writable boundary purpose is empty for {task_id}")
            writable.setdefault(task_id, set()).update(paths)
            purposes[(task_id, next(iter(paths)))] = row[2]
    for task_id, task in tasks.items():
        missing = sorted(task.boundaries - listed.get(task_id, set()))
        extra = sorted(listed.get(task_id, set()) - task.boundaries)
        if missing:
            errors.append(
                f"file-boundary table omits {task_id} task boundaries: "
                + ", ".join(missing)
            )
        if extra:
            errors.append(
                f"file-boundary table adds paths absent from {task_id} task boundaries: "
                + ", ".join(extra)
            )
    return writable, purposes, errors


def criterion_contract_refs(
    criterion: str, definitions: dict[str, list[str]]
) -> tuple[list[str], list[str]]:
    text = "\n".join(definitions.get(criterion, []))
    referenced = referenced_ids(text)
    requirements = [item for item in referenced if item.startswith(("R-", "AR-"))]
    decisions = [item for item in referenced if item.startswith("D-")]
    return requirements, decisions


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def boundary_rows(lines: list[str], task_id: str) -> list[str]:
    section = section_body(lines, "## File boundaries")
    token = re.compile(rf"(?<![A-Za-z0-9]){re.escape(task_id)}(?![0-9])")
    rows: list[str] = []
    heading = ""
    heading_emitted = ""
    header: list[str] = []
    header_emitted = False
    for index, line in enumerate(section):
        stripped = line.rstrip()
        if stripped.startswith("### "):
            heading = stripped
            header = []
            header_emitted = False
            continue
        if not stripped.startswith("|"):
            continue
        if TABLE_SEPARATOR.match(stripped):
            continue
        following = section[index + 1] if index + 1 < len(section) else ""
        if TABLE_SEPARATOR.match(following.rstrip()):
            header = [stripped, following.rstrip()]
            header_emitted = False
            continue
        if not token.search(stripped):
            continue
        if heading and heading_emitted != heading:
            if rows:
                rows.append("")
            rows.append(heading)
            rows.append("")
            heading_emitted = heading
            header_emitted = False
        if header and not header_emitted:
            rows.extend(header)
            header_emitted = True
        rows.append(stripped)
    return rows
