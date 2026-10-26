"""The package list of the checkouts document: collector inputs and scan states."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from test_checkouts import (  # noqa: F401  (fixtures that isolate git and GitHub state)
    clone,
    git,
    import_branches,
    isolated_git,
    make_remote,
    outer_github_environment,
    path_with,
    posix_only,
    real,
    stand_in,
)

from nyx import checkout_join, checkouts, cli, models

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
    assert first["target"] == {"status": "clone", "clone": real(repository), "worktree": None}
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
    assert tabbed["target"] == {"status": "missing", "clone": None, "worktree": None}
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


def package_root(tmp_path: Path, targets: dict[str, Path | str | None]) -> Path:
    """One package per name, each with a branch and, unless ``None``, a target."""
    root = tmp_path / "targets"
    for name, target in targets.items():
        line = "" if target is None else f"Target repo: {target}\n"
        make_package(root, "Drafts", name, f"{line}Branch: spec/{name}\n")
    return root


def resolve(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        package["package_path"].rsplit("/", 1)[1]: package["target"]
        for package in document["packages"]
    }


def clone_target(clone_path: Path | str) -> dict[str, Any]:
    return {"status": "clone", "clone": real(clone_path), "worktree": None}


@pytest.fixture
def targets(tmp_path):
    """The checkout folder, the repositories around it and the packages naming them."""
    remote = make_remote(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    layout: dict[str, Any] = {"folder": folder, "outside": outside}

    main = clone(remote, folder / "Repo Clone 2")
    git(main, "worktree", "add", "-q", "-b", "linked", str(outside / "Repo Work"), "trunk")
    git(main, "worktree", "add", "-q", "-b", "gone", str(outside / "Gone Work"), "trunk")
    shutil.rmtree(outside / "Gone Work")
    if hasattr(os, "symlink"):
        try:
            os.symlink(main, folder / "Alias", target_is_directory=True)
            layout["alias"] = folder / "Alias"
        except OSError:
            pass

    bare = outside / "Bare Repo.git"
    git(outside, "clone", "-q", "--bare", str(remote), str(bare))
    git(bare, "worktree", "add", "-q", "-b", "bare-linked", str(folder / "Bare Work"), "trunk")

    holder = folder / "Bare Holder"
    holder.mkdir()
    git(holder, "init", "-q", "--bare", "-b", "trunk", str(holder / ".git"))

    busy = clone(remote, folder / "Busy")
    git(busy, "worktree", "add", "-q", "-b", "busy-linked", str(folder / "Busy Work"), "trunk")
    import_branches(busy, models.CHECKOUT_BRANCH_CAP)

    (folder / "Plain").mkdir()
    elsewhere = clone(remote, outside / "Elsewhere")
    layout.update(
        main=main,
        linked=outside / "Repo Work",
        gone=outside / "Gone Work",
        bare=bare,
        bare_work=folder / "Bare Work",
        holder=holder,
        busy=busy,
        busy_work=folder / "Busy Work",
        elsewhere=elsewhere,
    )
    return layout


@posix_only
def test_every_target_resolves_to_the_checkout_at_its_path(targets, tmp_path, monkeypatch):
    folder = targets["folder"]
    wanted: dict[str, Path | str | None] = {
        "clone": targets["main"],
        "linked": targets["linked"],
        "bare": targets["bare"],
        "barework": targets["bare_work"],
        "holder": targets["holder"],
        "busy": targets["busy"],
        "busywork": targets["busy_work"],
        "inside": targets["main"] / "src",
        "missing": folder / "No Such Folder",
        "plain": folder / "Plain",
        "outside": targets["elsewhere"],
        "gone": targets["gone"],
    }
    if "alias" in targets:
        wanted["alias"] = targets["alias"]
    (targets["main"] / "src").mkdir()
    root = package_root(tmp_path, wanted)
    shim, log = recording_with_directory(tmp_path)

    with path_with(monkeypatch, shim):
        document = collect_packages([folder], root)

    found = resolve(document)
    documented = {c["path"]: c for r in document["repositories"] for c in r["clones"]}
    assert documented[real(targets["busy"])]["status"] == "too_many_branches"
    assert documented[str(targets["holder"])]["status"] == "failed"
    assert found["clone"] == clone_target(targets["main"])
    assert found["linked"] == {
        "status": "worktree",
        "clone": real(targets["main"]),
        "worktree": real(targets["linked"]),
    }
    if "alias" in targets:
        assert found["alias"] == clone_target(targets["main"])
    assert found["bare"] == clone_target(targets["bare"])
    assert found["barework"] == {
        "status": "worktree",
        "clone": real(targets["bare"]),
        "worktree": real(targets["bare_work"]),
    }
    assert found["holder"] == {"status": "clone", "clone": str(targets["holder"]), "worktree": None}
    assert found["busy"] == clone_target(targets["busy"])
    assert found["busywork"] == clone_target(targets["busy"])
    assert found["inside"] == {"status": "not_git", "clone": None, "worktree": None}
    assert found["missing"]["status"] == "missing"
    assert found["plain"]["status"] == "not_git"
    assert found["outside"] == {"status": "outside_folders", "clone": None, "worktree": None}
    assert found["gone"]["status"] == "missing"
    calls = log.read_text().splitlines()
    assert calls
    assert not [c for c in calls if c.startswith(real(targets["elsewhere"]) + "\t")]
    assert not [c for c in calls if c.startswith(str(targets["main"] / "src") + "\t")]


def recording_with_directory(tmp_path: Path) -> tuple[Path, Path]:
    """A ``git`` stand-in that logs the directory and arguments of each call."""
    log = tmp_path / "directories.log"
    body = f'printf "%s\\t%s\\n" "$(pwd -P)" "$*" >> "{log}"'
    return stand_in(tmp_path, "directory-recorder", body), log


def test_a_listed_linked_worktree_resolves_to_a_worktree_even_inside_the_folder(targets):
    document = collect_packages(
        [targets["folder"]], package_root(targets["folder"].parent, {"w": targets["bare_work"]})
    )

    assert resolve(document)["w"]["status"] == "worktree"
    clones = {c["path"]: c for r in document["repositories"] for c in r["clones"]}
    assert real(targets["bare"]) in clones
    assert clones[real(targets["bare"])]["status"] == "ok"


@pytest.mark.skipif(sys.platform not in ("win32", "darwin"), reason="needs a case-folding host")
def test_a_clone_path_in_another_letter_case_resolves_to_the_clone(targets, tmp_path):
    variant = Path(str(targets["main"]).swapcase())
    if not variant.exists():
        pytest.skip("the test volume does not fold case")
    root = package_root(tmp_path, {"variant": variant})

    document = collect_packages([targets["folder"]], root)

    assert resolve(document)["variant"] == clone_target(targets["main"])


def test_the_join_compares_keys_not_strings():
    index = checkout_join.TargetIndex(
        clones={("id", 1, 10): "/fictional/clone"},
        worktrees={("id", 1, 20): ("/fictional/clone", "/fictional/work")},
        not_git=frozenset({("id", 1, 30)}),
    )

    def fail() -> str:
        raise AssertionError("a matched target is not classified from the filesystem")

    resolved = checkout_join.resolve_target
    assert resolved("usable", ("id", 1, 10), index, fail) == {
        "status": "clone", "clone": "/fictional/clone", "worktree": None,
    }
    assert resolved("usable", ("id", 1, 20), index, fail) == {
        "status": "worktree", "clone": "/fictional/clone", "worktree": "/fictional/work",
    }
    assert resolved("usable", ("id", 1, 30), index, fail)["status"] == "not_git"
    assert resolved("usable", ("path", "/fictional/clone"), index, lambda: "missing")[
        "status"
    ] == "missing"
    assert resolved("usable", None, index, lambda: "outside_folders")["status"] == (
        "outside_folders"
    )
    assert resolved("usable", ("id", 1, 10), None, fail)["status"] == "unchecked"
    assert resolved("invalid", ("id", 1, 10), index, fail)["status"] == "invalid"
    assert resolved("absent", None, None, fail)["status"] == "absent"
    with pytest.raises(ValueError):
        resolved("usable", None, index, lambda: "clone")


def test_a_worktree_of_an_unreadable_clone_is_refused_by_the_parser(targets, tmp_path):
    document = json.loads(
        checkouts.collect_checkouts(
            [str(targets["folder"])],
            deadline_ns=time.monotonic_ns() + 60 * 10**9,
            package_scan=checkouts.PackageScan(
                package_root(tmp_path, {"w": targets["busy_work"]})
            ),
        )
    )
    package = document["packages"][0]
    busy = real(targets["busy"])
    assert package["target"] == {"status": "clone", "clone": busy, "worktree": None}
    package["target"] = {"status": "worktree", "clone": busy, "worktree": real(targets["busy_work"])}
    document["checkouts_digest"] = models.canonical_digest(
        document, omit=models.CHECKOUTS_DIGEST_OMITTED
    )

    with pytest.raises(models.ProtocolError, match="not listed"):
        models.parse_checkouts(document)

    package["target"] = {"status": "clone", "clone": "/fictional/nowhere", "worktree": None}
    document["checkouts_digest"] = models.canonical_digest(
        document, omit=models.CHECKOUTS_DIGEST_OMITTED
    )
    with pytest.raises(models.ProtocolError, match="names no clone"):
        models.parse_checkouts(document)


def unchecked_world(tmp_path: Path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    existing = clone(remote, folder / "listed")
    root = package_root(
        tmp_path,
        {
            "exists": existing,
            "gone": tmp_path / "no" / "such" / "place",
            "relative": "listed",
            "none": None,
        },
    )
    return folder, root


def assert_unchecked(document: dict[str, Any], git_status: str) -> None:
    assert document["git"]["status"] == git_status
    assert document["repositories"] == []
    assert document["package_scan"]["status"] == "complete"
    assert {k: v["status"] for k, v in resolve(document).items()} == {
        "exists": "unchecked",
        "gone": "unchecked",
        "relative": "invalid",
        "none": "absent",
    }
    assert all(t["clone"] is None and t["worktree"] is None for t in resolve(document).values())


def test_without_git_a_usable_target_is_unchecked(tmp_path, monkeypatch):
    folder, root = unchecked_world(tmp_path)
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    assert_unchecked(collect_packages([folder], root), "missing")


@posix_only
def test_with_an_old_git_a_usable_target_is_unchecked(tmp_path, monkeypatch):
    folder, root = unchecked_world(tmp_path)
    old = tmp_path / "old-git"
    old.mkdir()
    script = old / "git"
    script.write_text("#!/bin/sh\necho 'git version 2.35.9'\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{old}{os.pathsep}{os.environ['PATH']}")

    assert_unchecked(collect_packages([folder], root), "too_old")


def argument_recorder(tmp_path: Path) -> tuple[Path, Path]:
    """A ``git`` stand-in that logs each call's arguments, separated by U+001F."""
    log = tmp_path / "arguments.log"
    body = (
        f'for argument in "$@"; do printf "%s\\037" "$argument" >> "{log}"; done; '
        f'printf "\\n" >> "{log}"'
    )
    return stand_in(tmp_path, "argument-recorder", body), log


def directories_of(log: Path) -> list[str]:
    """The ``-C`` directory of every logged call that names one, as a real path."""
    directories = []
    for line in log.read_text().splitlines():
        arguments = line.split("\x1f")
        if "-C" in arguments:
            directories.append(real(arguments[arguments.index("-C") + 1]))
    return directories


@posix_only
def test_git_runs_in_no_target_directory_that_discovery_did_not_admit(
    targets, tmp_path, monkeypatch
):
    # The runner names each directory with ``-C``, never through its own working
    # directory, so the log must record the arguments to see where git ran.
    folder = targets["folder"]
    (targets["main"] / "src").mkdir()
    unadmitted = {
        "outside": targets["elsewhere"],
        "inside": targets["main"] / "src",
        "plain": folder / "Plain",
    }
    root = package_root(tmp_path, {**unadmitted, "clone": targets["main"]})
    shim, log = argument_recorder(tmp_path)

    with path_with(monkeypatch, shim):
        document = collect_packages([folder], root)

    found = resolve(document)
    assert found["outside"]["status"] == "outside_folders"
    assert found["inside"]["status"] == "not_git"
    assert found["plain"]["status"] == "not_git"
    assert found["clone"] == clone_target(targets["main"])
    directories = directories_of(log)
    assert real(targets["main"]) in directories, "control: the log names each directory"
    for name, path in unadmitted.items():
        prefix = real(path)
        assert not [d for d in directories if d == prefix or d.startswith(prefix + os.sep)], name


def test_a_candidate_git_does_not_accept_as_a_repository_is_not_a_git_checkout(tmp_path):
    folder = tmp_path / "folder"
    fake = folder / "Fake"
    (fake / ".git" / "info").mkdir(parents=True)
    redirected = folder / "Redirected"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    git(folder, "init", "-q", "-b", "trunk", str(redirected))
    git(redirected, "config", "core.worktree", str(elsewhere))
    root = package_root(tmp_path, {"fake": fake, "redirected": redirected})

    document = collect_packages([folder], root)

    assert {"code": "not_a_repository", "path": str(fake)} in document["diagnostics"]
    assert {"code": "not_a_repository", "path": str(redirected)} in document["diagnostics"]
    found = resolve(document)
    assert found["fake"] == {"status": "not_git", "clone": None, "worktree": None}
    assert found["redirected"] == {"status": "not_git", "clone": None, "worktree": None}


def test_the_parser_refuses_a_worktree_its_readable_clone_does_not_list(targets, tmp_path):
    document = json.loads(
        checkouts.collect_checkouts(
            [str(targets["folder"])],
            deadline_ns=time.monotonic_ns() + 60 * 10**9,
            package_scan=checkouts.PackageScan(
                package_root(tmp_path, {"w": targets["linked"]})
            ),
        )
    )
    target = document["packages"][0]["target"]
    assert target["status"] == "worktree"
    clones = {c["path"]: c for r in document["repositories"] for c in r["clones"]}
    assert clones[target["clone"]]["status"] == "ok"

    def refusal(worktree: str) -> str:
        target["worktree"] = worktree
        document["checkouts_digest"] = models.canonical_digest(
            document, omit=models.CHECKOUTS_DIGEST_OMITTED
        )
        with pytest.raises(models.ProtocolError) as refused:
            models.parse_checkouts(document)
        return str(refused.value)

    # The clone's own main working tree, and a worktree listed under another clone.
    assert "not listed" in refusal(target["clone"])
    assert "not listed" in refusal(real(targets["bare_work"]))


def test_every_target_resolves_and_renders_on_every_host(targets, tmp_path):
    folder = targets["folder"]
    (targets["main"] / "src").mkdir()
    wanted: dict[str, Path | str | None] = {
        "clone": targets["main"],
        "linked": targets["linked"],
        "bare": targets["bare"],
        "barework": targets["bare_work"],
        "holder": targets["holder"],
        "busy": targets["busy"],
        "busywork": targets["busy_work"],
        "inside": targets["main"] / "src",
        "missing": folder / "No Such Folder",
        "plain": folder / "Plain",
        "outside": targets["elsewhere"],
        "gone": targets["gone"],
    }
    if "alias" in targets:
        wanted["alias"] = targets["alias"]

    document = collect_packages([folder], package_root(tmp_path, wanted))

    found = resolve(document)
    named = {
        "clone": ("main", None),
        "alias": ("main", None),
        "linked": ("main", "linked"),
        "bare": ("bare", None),
        "barework": ("bare", "bare_work"),
        "holder": ("holder", None),
        "busy": ("busy", None),
        "busywork": ("busy", None),
    }
    for name, (clone_name, worktree_name) in named.items():
        if name not in found:
            continue
        target = found[name]
        assert target["status"] == ("clone" if worktree_name is None else "worktree"), name
        assert os.path.samefile(target["clone"], targets[clone_name]), name
        if worktree_name is None:
            assert target["worktree"] is None, name
        else:
            assert os.path.samefile(target["worktree"], targets[worktree_name]), name
    unmatched = {
        "inside": "not_git",
        "missing": "missing",
        "plain": "not_git",
        "outside": "outside_folders",
        "gone": "missing",
    }
    for name, status in unmatched.items():
        assert found[name] == {"status": status, "clone": None, "worktree": None}, name
    lines = cli.format_checkouts(document)
    busy = found["busy"]["clone"]
    assert f"{json.dumps(busy)}  too_many_branches" in lines
    assert any(line.startswith(json.dumps(found["bare"]["clone"]) + "  ") for line in lines)
