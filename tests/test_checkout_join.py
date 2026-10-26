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
    commit_file,
    doc_copy,
    git,
    import_branches,
    isolated_git,
    make_remote,
    outer_github_environment,
    path_with,
    posix_only,
    real,
    redigest,
    refusal_of,
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
    assert_target(first["target"], "clone", real(repository))
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
    unreferenced = doc_copy(present)
    for repository in unreferenced["repositories"]:
        repository["package_branches"] = []
        for member in repository["clones"]:
            for branch in member["branches"]:
                branch["packages"] = []
    assert document["repositories"] == unreferenced["repositories"]
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
    assert_target(tabbed["target"], "missing")
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


def assert_target(
    target: dict[str, Any], status: str, clone: str | None = None, worktree: str | None = None
) -> None:
    """Compare a target field by field, through the parser's own key set."""
    assert set(target) == models._PACKAGE_TARGET_KEYS
    assert target["status"] == status
    assert target["clone"] == clone
    assert target["worktree"] == worktree


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

    assert_target(resolve(document)["variant"], "clone", real(targets["main"]))


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
    assert_target(package["target"], "clone", busy)
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
    found = resolve(document)
    for name, status in {
        "exists": "unchecked",
        "gone": "unchecked",
        "relative": "invalid",
        "none": "absent",
    }.items():
        assert_target(found[name], status)


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
    assert_target(found["clone"], "clone", real(targets["main"]))
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
    assert_target(found["fake"], "not_git")
    assert_target(found["redirected"], "not_git")


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
        assert_target(found[name], status)
    lines = cli.format_checkouts(document)
    busy = found["busy"]["clone"]
    assert f"{json.dumps(busy)}  too_many_branches" in lines
    assert any(line.startswith(json.dumps(found["bare"]["clone"]) + "  ") for line in lines)


def test_a_path_that_cannot_name_anything_is_missing(tmp_path):
    long_name = tmp_path / ("x" * 300)
    folder = tmp_path / "folder"
    folder.mkdir()
    root = package_root(tmp_path, {"long": long_name})

    document = collect_packages([folder], root)

    assert_target(resolve(document)["long"], "missing")
    assert checkouts._unmatched_target_status("/fictional/nul\0byte") == "missing"


@posix_only
def test_a_symbolic_link_loop_is_missing(tmp_path):
    first = tmp_path / "loop-a"
    second = tmp_path / "loop-b"
    first.symlink_to(second)
    second.symlink_to(first)

    assert checkouts._unmatched_target_status(str(first)) == "missing"


@posix_only
@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root ignores modes")
def test_a_path_that_cannot_be_examined_is_not_a_git_checkout(tmp_path):
    closed = tmp_path / "closed"
    (closed / "inner").mkdir(parents=True)
    closed.chmod(0)
    try:
        status = checkouts._unmatched_target_status(str(closed / "inner"))
    finally:
        closed.chmod(0o700)

    assert status == "not_git"


# ---------------------------------------------------------------------------
# Branch matching
# ---------------------------------------------------------------------------


def references_of(clone_document: dict[str, Any], name: str) -> list[tuple[str, str]]:
    """The (package_path, basis) pairs of one local branch, field by field."""
    (branch,) = [b for b in clone_document["branches"] if b["name"] == name]
    pairs = []
    for reference in branch["packages"]:
        assert set(reference) == models._BRANCH_PACKAGE_KEYS
        pairs.append((reference["package_path"], reference["basis"]))
    return pairs


def entries_of(repository: dict[str, Any]) -> list[tuple[str, list[str]]]:
    pairs = []
    for entry in repository["package_branches"]:
        assert set(entry) == models._PACKAGE_BRANCH_ENTRY_KEYS
        pairs.append((entry["name"], entry["packages"]))
    return pairs


def clone_at(document: dict[str, Any], path: Path | str) -> dict[str, Any]:
    (found,) = [
        c for r in document["repositories"] for c in r["clones"] if c["path"] == real(path)
    ]
    return found


def repository_of(document: dict[str, Any], path: Path | str) -> dict[str, Any]:
    (found,) = [
        r
        for r in document["repositories"]
        if any(c["path"] == real(path) for c in r["clones"])
    ]
    return found


def push_branch(repository: Path, name: str) -> None:
    git(repository, "push", "-q", "origin", name)


@pytest.fixture
def matching(tmp_path):
    """Clones A and B of remote R, clone C of remote S, a clone D with no remote."""
    remote_r = make_remote(tmp_path, "r.git")
    remote_s = make_remote(tmp_path, "s.git")
    folder = tmp_path / "folder"
    folder.mkdir()
    a = clone(remote_r, folder / "a")
    b = clone(remote_r, folder / "b")
    c = clone(remote_s, folder / "c")
    d = folder / "d"
    git(folder, "init", "-q", "-b", "trunk", str(d))
    commit_file(d, "file.txt", "one\n", "seed")
    for repository in (a, b, c, d):
        git(repository, "checkout", "-q", "-b", "helper")
        commit_file(repository, "helper.txt", "helper\n", "helper work")
        git(repository, "checkout", "-q", "-b", "topic")
        commit_file(repository, "topic.txt", "topic\n", "topic work")
        git(repository, "checkout", "-q", "trunk")
    root = tmp_path / "spec"
    headers = {
        "target": f"Target repo: {a}\nBranch: topic\n",
        "proposed": f"Target repo: {a}\nProposed branch: topic\n",
        "inapplicable": f"Target repo: {a}\nBranch: not applicable \u2014 x\n",
        "default": f"Target repo: {a}\nBranch: trunk\n",
        "missing": f"Target repo: {tmp_path / 'nowhere'}\nBranch: topic\n",
        "on_c": f"Target repo: {c}\nBranch: topic\n",
        "on_d": f"Target repo: {d}\nBranch: topic\n",
    }
    for name, header in headers.items():
        make_package(root, "Drafts", name, header)
    return {"folder": folder, "root": root, "a": a, "b": b, "c": c, "d": d}


def path_of(name: str) -> str:
    return f"Fictional/Drafts/{name}"


def test_a_branch_is_matched_by_repository_group_and_not_by_name(matching):
    document = collect_packages([matching["folder"]], matching["root"])

    a = clone_at(document, matching["a"])
    assert a["verdict_base"] is not None and a["containment"] == "complete"
    assert references_of(a, "topic") == [(path_of("target"), "target_clone")]
    assert references_of(clone_at(document, matching["b"]), "topic") == [
        (path_of("target"), "sibling_clone")
    ]
    # Clone C shares the branch name with another repository's package, and the
    # package that targets C is the only one it serves.
    assert references_of(clone_at(document, matching["c"]), "topic") == [
        (path_of("on_c"), "target_clone")
    ]
    assert references_of(clone_at(document, matching["d"]), "topic") == [
        (path_of("on_d"), "target_clone")
    ]
    assert entries_of(repository_of(document, matching["a"])) == [
        ("topic", [path_of("target")])
    ]
    assert entries_of(repository_of(document, matching["c"])) == [
        ("topic", [path_of("on_c")])
    ]
    assert entries_of(repository_of(document, matching["d"])) == [
        ("topic", [path_of("on_d")])
    ]


def test_a_helper_branch_contained_in_a_package_branch_is_folded_under_it(matching):
    document = collect_packages([matching["folder"]], matching["root"])

    for member in (matching["a"], matching["b"]):
        helper = [b for b in clone_at(document, member)["branches"] if b["name"] == "helper"]
        assert [b["contained_in"] for b in helper] == ["topic"]
        assert references_of(clone_at(document, member), "helper") == [
            (path_of("target"), "contained")
        ]
    # The other repository's helper folds under its own topic and serves only
    # the package that targets that repository.
    assert references_of(clone_at(document, matching["c"]), "helper") == [
        (path_of("on_c"), "contained")
    ]
    assert references_of(clone_at(document, matching["a"]), "trunk") == []


def test_packages_that_name_no_declared_branch_match_nothing(matching):
    document = collect_packages([matching["folder"]], matching["root"])

    served = {
        reference["package_path"]
        for repository in document["repositories"]
        for member in repository["clones"]
        for branch in member["branches"]
        for reference in branch["packages"]
    }
    listed = {
        path
        for repository in document["repositories"]
        for entry in repository["package_branches"]
        for path in entry["packages"]
    }
    by_name = {p["package_path"]: p for p in document["packages"]}
    assert by_name[path_of("proposed")]["branch"]["status"] == "absent"
    assert by_name[path_of("inapplicable")]["branch"]["status"] == "not_applicable"
    assert by_name[path_of("missing")]["target"]["status"] == "missing"
    expected = {path_of("target"), path_of("on_c"), path_of("on_d")}
    assert served == expected
    assert listed == expected


def test_a_package_naming_the_default_branch_is_not_matched(matching):
    document = collect_packages([matching["folder"]], matching["root"])

    package = {p["package_path"]: p for p in document["packages"]}[path_of("default")]
    assert set(package["branch"]) == models._PACKAGE_BRANCH_KEYS
    assert package["branch"]["status"] == "default_branch"
    assert package["branch"]["name"] is None
    assert path_of("default") not in {
        path
        for entry in repository_of(document, matching["a"])["package_branches"]
        for path in entry["packages"]
    }
    for member in (matching["a"], matching["b"]):
        assert all(
            reference[0] != path_of("default")
            for branch in clone_at(document, member)["branches"]
            for reference in references_of(clone_at(document, member), branch["name"])
        )


def test_without_a_known_default_the_base_of_any_clone_names_it(matching, tmp_path):
    a, b = matching["a"], matching["b"]
    git(a, "push", "-q", "origin", "trunk:master")
    git(a, "fetch", "-q", "origin")
    git(b, "fetch", "-q", "origin")
    git(a, "remote", "set-head", "origin", "trunk")
    git(b, "remote", "set-head", "origin", "master")
    root = tmp_path / "defaults"
    make_package(root, "Drafts", "other", f"Target repo: {a}\nBranch: master\n")

    document = collect_packages([matching["folder"]], root)

    repository = repository_of(document, a)
    assert repository["remote"]["check"]["status"] == "off"
    assert repository["default_branch"]["name"] is None
    assert {
        (clone_at(document, member)["verdict_base"]["name"])
        for member in (a, b)
    } == {"trunk", "master"}
    (package,) = document["packages"]
    assert package["branch"]["status"] == "default_branch"
    assert package["branch"]["name"] is None
    assert repository["package_branches"] == []


def test_a_package_targeting_a_worktree_serves_the_branch_of_its_clone(matching, tmp_path):
    a = matching["a"]
    worktree = tmp_path / "linked-checkout"
    git(a, "worktree", "add", "-q", "-b", "other", str(worktree), "trunk")
    root = tmp_path / "worktrees"
    make_package(root, "Drafts", "w", f"Target repo: {worktree}\nBranch: topic\n")

    document = collect_packages([matching["folder"]], root)

    (package,) = document["packages"]
    assert package["target"]["status"] == "worktree"
    assert references_of(clone_at(document, a), "topic") == [
        ("Fictional/Drafts/w", "target_clone")
    ]
    assert references_of(clone_at(document, matching["b"]), "topic") == [
        ("Fictional/Drafts/w", "sibling_clone")
    ]


def test_several_packages_naming_one_branch_share_its_entry(matching, tmp_path):
    root = tmp_path / "shared"
    make_package(root, "Drafts", "one", f"Target repo: {matching['a']}\nBranch: topic\n")
    make_package(root, "Shelf", "two", f"Target repo: {matching['b']}\nBranch: topic\n")

    document = collect_packages([matching["folder"]], root)

    assert entries_of(repository_of(document, matching["a"])) == [
        ("topic", ["Fictional/Drafts/one", "Fictional/Shelf/two"])
    ]
    assert references_of(clone_at(document, matching["a"]), "topic") == [
        ("Fictional/Drafts/one", "target_clone"),
        ("Fictional/Shelf/two", "sibling_clone"),
    ]
    assert references_of(clone_at(document, matching["b"]), "topic") == [
        ("Fictional/Drafts/one", "sibling_clone"),
        ("Fictional/Shelf/two", "target_clone"),
    ]


def reference_mutation(document: dict[str, Any], change) -> str:
    mutated = doc_copy(document)
    change(mutated)
    return refusal_of(mutated)


def test_the_parser_refuses_a_reference_its_package_does_not_back(matching):
    document = collect_packages([matching["folder"]], matching["root"])
    a_index = document["repositories"].index(repository_of(document, matching["a"]))

    def topic(doc: dict[str, Any], member: Path) -> dict[str, Any]:
        (found,) = [b for b in clone_at(doc, member)["branches"] if b["name"] == "topic"]
        return found

    def helper(doc: dict[str, Any], member: Path) -> dict[str, Any]:
        (found,) = [b for b in clone_at(doc, member)["branches"] if b["name"] == "helper"]
        return found

    def sibling_as_target(doc):
        topic(doc, matching["b"])["packages"][0]["basis"] = "target_clone"

    def target_as_sibling(doc):
        topic(doc, matching["a"])["packages"][0]["basis"] = "sibling_clone"

    def other_repository(doc):
        topic(doc, matching["c"])["packages"][0]["package_path"] = path_of("target")

    def not_contained(doc):
        helper(doc, matching["a"])["packages"][0]["basis"] = "contained"
        helper(doc, matching["a"])["contained_in"] = None

    def unknown_package(doc):
        topic(doc, matching["a"])["packages"][0]["package_path"] = "Fictional/Drafts/zz"

    def wrong_name(doc):
        doc["repositories"][a_index]["package_branches"][0]["name"] = "helper"

    def entry_of_another_repository(doc):
        doc["repositories"][a_index]["package_branches"][0]["packages"] = [path_of("on_c")]

    assert models.parse_checkouts(json.dumps(redigest(doc_copy(document))))
    for change, message in (
        (sibling_as_target, "target_clone reference its package does not back"),
        (target_as_sibling, "sibling_clone reference its package does not back"),
        (other_repository, "target_clone reference its package does not back"),
        (not_contained, "contained reference its package does not back"),
        (unknown_package, "names a package the document does not list"),
        (wrong_name, "lists a package that names another branch"),
        (entry_of_another_repository, "lists a package of another repository"),
    ):
        assert message in reference_mutation(document, change), change.__name__


@pytest.fixture
def stacked(tmp_path):
    """Clone A of a remote whose branches stack: work-1 under work-9 under work-10."""
    base = tmp_path / "stacked"
    base.mkdir()
    remote = make_remote(base, "r.git")
    folder = base / "folder"
    folder.mkdir()
    a = clone(remote, folder / "a")
    for name in ("work-1", "work-9", "work-10"):
        git(a, "checkout", "-q", "-b", name)
        commit_file(a, f"{name}.txt", f"{name}\n", f"{name} work")
    git(a, "checkout", "-q", "trunk")
    root = base / "spec"
    make_package(root, "Drafts", "a-outer", f"Target repo: {a}\nBranch: work-10\n")
    make_package(root, "Drafts", "z-middle", f"Target repo: {a}\nBranch: work-9\n")
    return {"folder": folder, "root": root, "a": a}


def test_a_branch_folds_under_every_package_branch_its_chain_reaches(stacked):
    document = collect_packages([stacked["folder"]], stacked["root"])

    a = clone_at(document, stacked["a"])
    assert a["containment"] == "complete"
    assert {b["name"]: b["contained_in"] for b in a["branches"]} == {
        "trunk": None,
        "work-1": "work-9",
        "work-9": "work-10",
        "work-10": None,
    }
    outer, middle = "Fictional/Drafts/a-outer", "Fictional/Drafts/z-middle"
    assert references_of(a, "work-10") == [(outer, "target_clone")]
    # A package's own branch and a branch folded under another package's branch
    # are listed together, by package path.
    assert references_of(a, "work-9") == [(outer, "contained"), (middle, "target_clone")]
    # Two links away from work-10, work-1 still serves the package naming it.
    assert references_of(a, "work-1") == [(outer, "contained"), (middle, "contained")]
    assert references_of(a, "trunk") == []
    # Entries follow branch order (digit runs as numbers), not code-point order.
    assert entries_of(repository_of(document, stacked["a"])) == [
        ("work-9", [middle]),
        ("work-10", [outer]),
    ]


def test_a_package_on_a_refused_clone_is_served_by_its_readable_sibling(tmp_path):
    remote = make_remote(tmp_path, "r.git")
    folder = tmp_path / "folder"
    folder.mkdir()
    readable = clone(remote, folder / "readable")
    crowded = clone(remote, folder / "crowded")
    for repository in (readable, crowded):
        git(repository, "checkout", "-q", "-b", "topic")
        commit_file(repository, "topic.txt", "topic\n", "topic work")
        git(repository, "checkout", "-q", "trunk")
    import_branches(crowded, models.CHECKOUT_BRANCH_CAP)
    root = tmp_path / "spec"
    make_package(root, "Drafts", "crowded", f"Target repo: {crowded}\nBranch: topic\n")
    make_package(root, "Drafts", "default", f"Target repo: {crowded}\nBranch: trunk\n")

    document = collect_packages([folder], root)

    assert clone_at(document, crowded)["status"] == "too_many_branches"
    repository = repository_of(document, readable)
    assert repository is repository_of(document, crowded)
    assert repository["default_branch"]["name"] == "trunk"
    by_path = {p["package_path"]: p for p in document["packages"]}
    assert_target(by_path["Fictional/Drafts/crowded"]["target"], "clone", real(crowded))
    assert references_of(clone_at(document, readable), "topic") == [
        ("Fictional/Drafts/crowded", "sibling_clone")
    ]
    assert entries_of(repository) == [("topic", ["Fictional/Drafts/crowded"])]
    assert by_path["Fictional/Drafts/default"]["branch"] == {
        "status": "default_branch",
        "name": None,
    }


def test_the_parser_refuses_misplaced_or_misordered_package_references(matching, stacked):
    document = collect_packages([matching["folder"]], matching["root"])
    stack = collect_packages([stacked["folder"]], stacked["root"])

    def branch_of(doc: dict[str, Any], member: Path, name: str) -> dict[str, Any]:
        (found,) = [b for b in clone_at(doc, member)["branches"] if b["name"] == name]
        return found

    def entries(doc: dict[str, Any], member: Path) -> list[dict[str, Any]]:
        return repository_of(doc, member)["package_branches"]

    def helper_as_target(doc):
        branch_of(doc, matching["a"], "helper")["packages"][0]["basis"] = "target_clone"

    def helper_as_sibling(doc):
        branch_of(doc, matching["b"], "helper")["packages"][0]["basis"] = "sibling_clone"

    def sibling_in_another_repository(doc):
        branch_of(doc, matching["c"], "topic")["packages"] = [
            {"package_path": path_of("target"), "basis": "sibling_clone"}
        ]

    def folded_into_another_repository(doc):
        branch_of(doc, matching["c"], "helper")["packages"] = [
            {"package_path": path_of("target"), "basis": "contained"}
        ]

    def empty_entry(doc):
        entries(doc, matching["a"])[0]["packages"] = []

    def repeated_package(doc):
        listed = entries(doc, matching["a"])[0]["packages"]
        listed.append(listed[0])

    for change, message in (
        (helper_as_target, "target_clone reference its package does not back"),
        (helper_as_sibling, "sibling_clone reference its package does not back"),
        (sibling_in_another_repository, "sibling_clone reference its package does not back"),
        (folded_into_another_repository, "contained reference its package does not back"),
        (empty_entry, "package_branches[0].packages must not be empty"),
        (repeated_package, "package_branches[0].packages must be unique and in canonical order"),
    ):
        assert message in reference_mutation(document, change), change.__name__

    def swapped_entries(doc):
        entries(doc, stacked["a"]).reverse()

    def swapped_references(doc):
        branch_of(doc, stacked["a"], "work-9")["packages"].reverse()

    for change, message in (
        (swapped_entries, ".package_branches must be unique and in canonical order"),
        (swapped_references, ".packages must be unique and in canonical order"),
    ):
        assert message in reference_mutation(stack, change), change.__name__
