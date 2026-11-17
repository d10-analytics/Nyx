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
        "_HEADER_FIELD",
        "_CLAIM_NAME",
        "_PROVENANCE",
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
    "Prerequisite",
    "Completion Prerequisite",
    "Claim",
)
# The wire protocol validates claim names and provenance references with its own
# fullmatch patterns, a separate contract from the header rows the parser reads.
PROTOCOL_PATTERNS = {"models.py": frozenset({"_CLAIM_NAME", "_PROVENANCE"})}
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
        and (found := OWNED_DEFINITIONS & _defined_names(tree) - PROTOCOL_PATTERNS.get(module, frozenset()))
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
    assert set(HEADER_LABELS) <= _code_string_literals(modules[OWNER])
    stray = {
        module: sorted(set(HEADER_LABELS) & _code_string_literals(tree))
        for module, tree in modules.items()
        if module != OWNER and set(HEADER_LABELS) & _code_string_literals(tree)
    }
    assert stray == {}


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
