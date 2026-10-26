"""The package list of the checkouts document: collector inputs and scan states."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from test_checkouts import (  # noqa: F401  (fixtures that isolate git and GitHub state)
    clone,
    git,
    isolated_git,
    make_remote,
    outer_github_environment,
    posix_only,
    real,
)

from nyx import checkouts, models

ID_A = "11111111-1111-4111-8111-111111111111"
ID_B = "22222222-2222-4222-8222-222222222222"
ID_C = "33333333-3333-4333-8333-333333333333"
ID_OLD = "44444444-4444-4444-8444-444444444444"


def make_package(root: Path, stage: str, name: str, header: str) -> Path:
    directory = root / "Fictional" / stage / name
    directory.mkdir(parents=True)
    (directory / "spec.md").write_text(f"# {name}\n{header}", encoding="utf-8")
    return directory


def specification_root(tmp_path: Path, target: Path | str) -> Path:
    """Packages in stage folders that carry no workflow meaning."""
    root = tmp_path / "spec"
    make_package(root, "Drafts", "a", f"Package ID: {ID_A}\nTarget repo: {target}\nBranch: spec/alpha\n")
    make_package(
        root,
        "Shipped",
        "b",
        f"Package ID: {ID_B}\nBranch: spec/beta\nSuperseded By: {ID_OLD}\n",
    )
    make_package(root, "Drafts", "c", f"Package ID: {ID_C}\nStatus: draft\n")
    return root


def collect_packages(
    folders: list[Path | str],
    spec_root: Path | None,
    completed: tuple[str, ...] = (),
    **options: Any,
) -> dict[str, Any]:
    """Run the real collector with a package scan and return the parsed document."""
    text = checkouts.collect_checkouts(
        [str(folder) for folder in folders],
        deadline_ns=time.monotonic_ns() + 60 * 10**9,
        package_scan=checkouts.PackageScan(spec_root, completed),
        **options,
    )
    return models.parse_checkouts(text)


@pytest.fixture
def world(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "listed")
    git(repository, "checkout", "-q", "-b", "spec/alpha", "trunk")
    git(repository, "checkout", "-q", "trunk")
    return folder, repository, specification_root(tmp_path, repository)


def test_packages_with_a_target_or_branch_are_listed_in_path_order(world):
    folder, repository, root = world

    document = collect_packages([folder], root, ("Shipped",))

    assert document["package_scan"] == {"status": "complete", "finished_policy": "configured"}
    assert [p["package_path"] for p in document["packages"]] == [
        "Fictional/Drafts/a",
        "Fictional/Shipped/b",
    ]
    first, second = document["packages"]
    assert (first["package_id"], first["finished"], first["superseded"]) == (ID_A, False, False)
    assert (second["package_id"], second["finished"], second["superseded"]) == (ID_B, True, True)
    assert first["branch"] == {"status": "declared", "name": "spec/alpha"}
    assert second["branch"] == {"status": "declared", "name": "spec/beta"}
    assert first["target"]["status"] == "unchecked"
    assert second["target"]["status"] == "absent"
    assert {p["code_status"] for p in document["packages"]} == {None}
    assert [r["clones"][0]["path"] for r in document["repositories"]] == [real(repository)]


def test_without_a_completion_setting_no_package_is_finished(world):
    folder, _repository, root = world

    document = collect_packages([folder], root)

    assert document["package_scan"]["finished_policy"] == "not_configured"
    assert [p["finished"] for p in document["packages"]] == [False, False]


@posix_only
def test_an_unreadable_specification_file_makes_the_scan_incomplete(world):
    folder, _repository, root = world
    unreadable = root / "Fictional" / "Drafts" / "c" / "spec.md"
    unreadable.chmod(0o000)
    if os.access(unreadable, os.R_OK):  # pragma: no cover - a user that ignores modes
        unreadable.chmod(0o644)
        pytest.skip("file permissions are not enforced for this user")
    try:
        document = collect_packages([folder], root, ("Shipped",))
    finally:
        unreadable.chmod(0o644)

    assert document["package_scan"]["status"] == "incomplete"


def test_a_missing_specification_root_lists_no_package_and_keeps_repositories(world, tmp_path):
    folder, _repository, root = world
    present = collect_packages([folder], root, ("Shipped",))
    gone = tmp_path / "gone"

    document = collect_packages([folder], gone, ("Shipped",))

    assert document["package_scan"] == {
        "status": "unavailable",
        "finished_policy": "configured",
    }
    assert document["packages"] == []
    assert document["repositories"] == present["repositories"]
    unset = collect_packages([folder], gone)
    assert unset["package_scan"] == {
        "status": "unavailable",
        "finished_policy": "not_configured",
    }


def test_without_a_package_scan_the_scan_is_unavailable(world):
    folder, _repository, _root = world

    text = checkouts.collect_checkouts(
        [str(folder)], deadline_ns=time.monotonic_ns() + 60 * 10**9
    )
    document = models.parse_checkouts(text)

    assert document["package_scan"] == {
        "status": "unavailable",
        "finished_policy": "not_configured",
    }
    assert document["packages"] == []


def test_no_specification_root_is_unavailable(world):
    folder, _repository, _root = world

    document = collect_packages([folder], None, ("Shipped",))

    assert document["package_scan"] == {
        "status": "unavailable",
        "finished_policy": "configured",
    }
    assert document["packages"] == []


def test_an_unusable_target_and_branch_are_reported_without_the_path(tmp_path):
    root = tmp_path / "spec"
    make_package(root, "Drafts", "relative", "Target repo: Minotaur\nBranch: not applicable\n")
    make_package(root, "Drafts", "tabbed", "Target repo: /work/a\tb\nBranch: spec/x (proposed)\n")
    folder = tmp_path / "folder"
    folder.mkdir()

    document = collect_packages([folder], root)

    relative, tabbed = document["packages"]
    assert relative["target"]["status"] == "invalid"
    assert relative["branch"] == {"status": "not_applicable", "name": None}
    assert tabbed["target"] == {"status": "unchecked", "clone": None, "worktree": None}
    assert tabbed["branch"] == {"status": "invalid", "name": None}


def test_the_parser_refuses_a_package_list_that_contradicts_the_scan(world):
    folder, _repository, root = world
    document = collect_packages([folder], root, ("Shipped",))

    def refused(change) -> str:
        broken = json.loads(json.dumps(document))
        change(broken)
        broken["checkouts_digest"] = models.canonical_digest(
            broken, omit=models.CHECKOUTS_DIGEST_OMITTED
        )
        with pytest.raises(models.ProtocolError) as error:
            models.parse_checkouts(broken)
        return str(error.value)

    assert "packages must be empty" in refused(
        lambda d: d["package_scan"].update(status="unavailable")
    )
    assert "canonical order" in refused(lambda d: d["packages"].reverse())
    assert "set exactly for declared" in refused(
        lambda d: d["packages"][0]["branch"].update(name=None)
    )
    assert "relative" in refused(
        lambda d: d["packages"][0].update(package_path="../escape")
    )
    assert "invalid shape" in refused(lambda d: d["packages"][0].pop("superseded"))
    assert "is invalid" in refused(
        lambda d: d["package_scan"].update(finished_policy="maybe")
    )
