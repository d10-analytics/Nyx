"""The shared specification header parser has one owner and one branch rule."""
from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

import pytest

from nyx import catalog, spec_header

NYX_DIR = Path(__file__).resolve().parents[1] / "nyx"
OWNER = "spec_header.py"

OWNED_DEFINITIONS = frozenset(
    {
        "_UTF8_BOM",
        "_HEADER_BYTE_LIMIT",
        "_HEADER_CUT_SEPARATORS",
        "_catalog_header_cut",
        "_catalog_anchor_text",
        "_header",
        "read_header",
        "_metadata_value",
        "_metadata_values",
        "metadata_value",
        "metadata_values",
        "_HEADER_LABELS",
        "RETIRED_HEADER_LABELS",
        "_HEADER_FIELD",
        "_CANONICAL_UUID",
        "_uuid",
        "_uuid_candidates",
        "_parse_header",
        "parse_header",
    }
)
HEADER_LABELS = (
    "Package ID",
    "Program Membership",
    "Superseded By",
    "Completion Prerequisite",
)
RETIRED_HEADER_LABELS = ("Prerequisite", "Claim")
FORBIDDEN_CHARACTERS = " ~^:?*[\\`<>"


@cache
def _modules() -> dict[str, ast.Module]:
    paths = sorted(NYX_DIR.rglob("*.py"))
    assert OWNER in {path.name for path in paths}
    return {
        path.relative_to(NYX_DIR).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for path in paths
    }


def _defined_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(
            node.target, ast.Name
        ):
            names.add(node.target.id)
    return names


def _code_string_literals(tree: ast.Module) -> set[str]:
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }


def test_header_definitions_have_one_owner() -> None:
    modules = _modules()
    assert {"_catalog_header_cut", "_uuid", "parse_header"} <= _defined_names(modules[OWNER])
    stray = {
        module: sorted(found)
        for module, tree in modules.items()
        if module != OWNER
        and (found := OWNED_DEFINITIONS & _defined_names(tree))
    }
    assert stray == {}


def test_owner_defines_the_public_names() -> None:
    defined = _defined_names(_modules()[OWNER])
    assert {
        "read_header",
        "parse_header",
        "metadata_value",
        "metadata_values",
        "branch_name_is_lexical",
        "_HEADER_BYTE_LIMIT",
        "_HEADER_LABELS",
        "_HEADER_FIELD",
        "_uuid",
        "_uuid_candidates",
    } <= defined


def test_header_label_literals_have_one_owner() -> None:
    modules = _modules()
    labels = set(HEADER_LABELS) | set(RETIRED_HEADER_LABELS)
    assert labels <= _code_string_literals(modules[OWNER])
    stray = {
        module: sorted(labels & _code_string_literals(tree))
        for module, tree in modules.items()
        if module != OWNER and labels & _code_string_literals(tree)
    }
    assert stray == {}


RE_PATTERN_FUNCTIONS = frozenset(
    {"match", "search", "compile", "findall", "fullmatch", "finditer", "sub", "split"}
)
BOLD_MARKER = r"\*{0,2}"
LABEL_COLON = r"\s*:"


def _literal_text(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_literal_text(part) for part in node.values)
    return ""


def _header_label_patterns(tree: ast.Module) -> list[int]:
    """Return the lines of re calls whose pattern reads a ``Label:`` header line."""
    assigned: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.setdefault(target.id, []).append(node.value)
    found = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.func.attr in RE_PATTERN_FUNCTIONS
            and node.args
        ):
            continue
        pattern = node.args[0]
        candidates = (
            assigned.get(pattern.id, []) if isinstance(pattern, ast.Name) else [pattern]
        )
        for candidate in candidates:
            text = _literal_text(candidate)
            if BOLD_MARKER in text and LABEL_COLON in text:
                found.append(node.lineno)
    return found


def test_validator_builds_no_header_label_pattern() -> None:
    assert _header_label_patterns(_modules()["spec_validation.py"]) == []


def test_header_label_pattern_guard_detects_a_private_label_regex() -> None:
    source = (
        "import re\n"
        "def f(label, line):\n"
        "    pattern = rf'^\\s*\\*{{0,2}}{re.escape(label)}\\*{{0,2}}\\s*:'\n"
        "    return re.match(pattern, line, re.IGNORECASE)\n"
        "def g(line):\n"
        "    return re.search(r'^\\s*\\*{0,2}Status\\*{0,2}\\s*:', line)\n"
        "def h(line):\n"
        "    return re.match(r'^- `([^`]+)`', line)\n"
    )
    assert _header_label_patterns(ast.parse(source)) == [4, 6]


def test_catalog_branch_classifier_calls_the_shared_lexical_rule() -> None:
    tree = _modules()["catalog.py"]
    classifier = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_is_branch_name"
    )
    callees = {
        node.func.id
        for node in ast.walk(classifier)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "branch_name_is_lexical" in callees


def test_forbidden_branch_characters_have_one_owner() -> None:
    modules = _modules()
    assert FORBIDDEN_CHARACTERS in _code_string_literals(modules[OWNER])
    stray = [
        module
        for module, tree in modules.items()
        if module != OWNER and FORBIDDEN_CHARACTERS in _code_string_literals(tree)
    ]
    assert stray == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("`spec/fictional-board`", catalog.BranchHead("declared", "spec/fictional-board")),
        ("not applicable (fictional reason)", catalog.BranchHead("not_applicable", None)),
        pytest.param("a" * 2000, catalog.BranchHead("invalid", None), id="over-long-name"),
        ("Not Applicable — r", catalog.BranchHead("not_applicable", None)),
    ],
)
def test_branch_head_verdicts_are_unchanged(raw: str, expected: catalog.BranchHead) -> None:
    assert catalog._branch_head(raw) == expected


def test_lexical_rule_has_no_text_bound() -> None:
    long_name = "a" * 2000
    assert spec_header.branch_name_is_lexical(long_name) is True
    assert catalog._is_branch_name(long_name) is False


@pytest.mark.parametrize(
    "name", ["a b", "a~b", "a..b", "a@{b", "a//b", "@", "-a", "/a", "a/", "a.", "a/.b", "a.lock", "a\x07b"]
)
def test_lexical_rule_rejects_names_git_refuses(name: str) -> None:
    assert spec_header.branch_name_is_lexical(name) is False


def test_metadata_readers_return_every_value_and_the_first() -> None:
    lines = ["# Title", "Status: one  ", "**Status:** two", "Other: x", "## Body", "Status: late"]
    assert spec_header.metadata_values(lines, "Status") == ["one", "** two"]
    assert spec_header.metadata_value(lines, "Status") == "one"
    assert spec_header.metadata_value(lines, "Missing") is None


def test_read_header_cuts_at_the_first_item_heading() -> None:
    lines, diagnostic = spec_header.read_header(b"\xef\xbb\xbf# T\nStatus: x\n## Body\nBranch: y\n")
    assert (lines, diagnostic) == (["# T", "Status: x"], None)
    assert spec_header.read_header(b"\xff\xfe")[1] == "invalid_package"


_ID_A = "11111111-1111-4111-8111-111111111111"
_ID_B = "22222222-2222-4222-9222-222222222222"
_NAME_64 = "a" + "b-" * 31 + "c"
_HEADER_ROWS = {
    # One header holding both a retired row and every completion-row form: only
    # the four completion rows become prerequisites, in header order.
    "ac04": [
        "Prerequisite: " + _ID_B + " | release",
        "Claim: release | satisfied | sha256:" + "a" * 64,
        "Completion Prerequisite: " + _ID_B,
        "Completion Prerequisite: " + _ID_B + " | release",
        "Completion Prerequisite: not-a-uuid",
        "Completion Prerequisite:",
    ],
    "ident": ["  **Package ID**:" + _ID_A, "Program Membership:" + _ID_B, "**Superseded By**: " + _ID_B + "  "],
    "lower_label": ["package id: " + _ID_A, "claim: x | unknown", "prerequisite: " + _ID_A + " | x"],
    "lookaround": ["Package ID: 0" + _ID_A + " " + _ID_B + "f", "Package ID: x"],
    "claims": [
        "Claim: has-hyphen | unsatisfied",
        "Claim: " + _NAME_64 + " | unknown",
        "Claim: " + "a" * 65 + " | unknown",
        "Claim: Upper | unknown",
        "Claim: s1 | satisfied | git-object-sha1:" + "a" * 40,
        "Claim: s2 | satisfied | git-object-sha1:" + "a" * 39,
        "Claim: s3 | satisfied | git-object-sha256:" + "b" * 64,
        "Claim: s4 | satisfied | sha256:" + "c" * 64,
        "Claim: s5 | satisfied | sha256:" + "c" * 63,
        "Claim: s6 | satisfied | sha256:" + "c" * 64 + "x",
        "Claim: dup | unknown",
        "Claim: dup | unsatisfied",
    ],
    "prereq": [
        "Prerequisite: " + _ID_A + " | claim-a",
        "Prerequisite: " + _ID_A + " | Bad",
        "Prerequisite: " + _ID_A,
        "Completion Prerequisite: " + _ID_B,
        "Completion Prerequisite: " + _ID_B + " | x",
        "Completion Prerequisite: bad",
        "**Completion Prerequisite**: " + _ID_A,
    ],
    "retired": [
        "Prerequisite: " + _ID_A + " | release",
        "Claim: release | satisfied | sha256:" + "a" * 64,
        "Claim: release | unsatisfied",
    ],
    "malformed_completions": [
        "Completion Prerequisite: not-a-uuid",
        "Completion Prerequisite: " + _ID_B + " | claim",
        "Completion Prerequisite: " + _ID_B + " | release",
        "Completion Prerequisite:",
        "Completion Prerequisite: " + _ID_B + " " + _ID_A,
    ],
    "duplicate_package_id": ["Package ID: " + _ID_A, "Package ID: " + _ID_A],
    "two_package_ids": ["Package ID: " + _ID_A, "Package ID: " + _ID_B],
    "trailing_text": ["Package ID: " + _ID_A + " extra"],
    "version": [
        "Package ID: 11111111-1111-1111-8111-111111111111",
        "Program Membership: 11111111-1111-4111-7111-111111111111",
    ],
    "self_succ": ["Package ID: " + _ID_A, "Superseded By: " + _ID_A],
}
_EMPTY_RELATIONSHIP = {
    "prerequisites": [],
    "program": {"program_id": None, "resolution": "not_declared", "title": None},
    "superseded_by": {"package_id": None, "resolution": "not_declared"},
}
_PARSED_HEADERS = {
    "ac04": [
        {
            "prerequisites": [
                {"target_package_id": _ID_B},
                {"target_package_id": None},
                {"target_package_id": None},
                {"target_package_id": None},
            ],
            "program": {"program_id": None, "resolution": "not_declared", "title": None},
            "superseded_by": {"package_id": None, "resolution": "not_declared"},
        },
        [],
        None,
    ],
    "ident": [
        {
            "prerequisites": [],
            "program": {"program_id": _ID_B, "resolution": "unknown", "title": None},
            "superseded_by": {"package_id": _ID_B, "resolution": "unknown"},
        },
        [_ID_A],
        _ID_A,
    ],
    "lower_label": [dict(_EMPTY_RELATIONSHIP), [], None],
    "lookaround": [dict(_EMPTY_RELATIONSHIP), [], None],
    "claims": [dict(_EMPTY_RELATIONSHIP), [], None],
    "prereq": [
        {
            "prerequisites": [
                {"target_package_id": _ID_B},
                {"target_package_id": None},
                {"target_package_id": None},
                {"target_package_id": _ID_A},
            ],
            "program": {"program_id": None, "resolution": "not_declared", "title": None},
            "superseded_by": {"package_id": None, "resolution": "not_declared"},
        },
        [],
        None,
    ],
    "retired": [dict(_EMPTY_RELATIONSHIP), [], None],
    "malformed_completions": [
        {
            "prerequisites": [{"target_package_id": None}] * 5,
            "program": {"program_id": None, "resolution": "not_declared", "title": None},
            "superseded_by": {"package_id": None, "resolution": "not_declared"},
        },
        [],
        None,
    ],
    "duplicate_package_id": [dict(_EMPTY_RELATIONSHIP), [_ID_A], None],
    "two_package_ids": [dict(_EMPTY_RELATIONSHIP), [_ID_A, _ID_B], None],
    "trailing_text": [dict(_EMPTY_RELATIONSHIP), [_ID_A], None],
    "version": [
        {
            "prerequisites": [],
            "program": {"program_id": None, "resolution": "unknown", "title": None},
            "superseded_by": {"package_id": None, "resolution": "not_declared"},
        },
        [],
        None,
    ],
    "self_succ": [
        {
            "prerequisites": [],
            "program": {"program_id": None, "resolution": "not_declared", "title": None},
            "superseded_by": {"package_id": None, "resolution": "unknown"},
        },
        [_ID_A],
        _ID_A,
    ],
}


def test_header_labels_are_the_four_live_rows() -> None:
    assert spec_header._HEADER_LABELS == HEADER_LABELS
    assert HEADER_LABELS == (
        "Package ID", "Program Membership", "Superseded By", "Completion Prerequisite",
    )
    assert spec_header.RETIRED_HEADER_LABELS == RETIRED_HEADER_LABELS
    assert not set(HEADER_LABELS) & set(RETIRED_HEADER_LABELS)
    for label in RETIRED_HEADER_LABELS:
        assert spec_header._HEADER_FIELD.match(f"{label}: {_ID_A} | release") is None


@pytest.mark.parametrize("case", sorted(_HEADER_ROWS))
def test_parse_header_returns_the_relationship_candidates_and_package_id(case: str) -> None:
    relationship, candidates, package_id = spec_header.parse_header(_HEADER_ROWS[case])
    assert [relationship, sorted(candidates), package_id] == _PARSED_HEADERS[case]


def test_metadata_values_match_the_original_line_pattern() -> None:
    lines = [
        "# T",
        "status: lower",
        "  **Status**: bold  ",
        "**Status:** colon-inside",
        "Status:",
        "Status: a \u00a0\x85\x1c\u3000\t",
        "Target repo:x",
        "## Body",
        "Status: late",
    ]
    assert spec_header.metadata_values(lines, "Status") == ["lower", "bold", "** colon-inside", "", "a"]
    assert spec_header.metadata_value(lines, "Target repo") == "x"
    assert spec_header.metadata_value(["Status: "], "Status") == ""
