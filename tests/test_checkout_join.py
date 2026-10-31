"""The package list of the checkouts document: collector inputs and scan states."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from test_checkouts import (  # noqa: F401  (fixtures that isolate git and GitHub state)
    GITHUB_URL,
    LAST_GIT_SECOND,
    cache_entries,
    clone,
    collect,
    commit_file,
    doc_copy,
    git,
    import_branches,
    install_fake_gh,
    isolated_git,
    land,
    last_author,
    linked,
    local_names,
    make_remote,
    nyx_home,
    outer_github_environment,
    path_with,
    point_origin_at,
    posix_only,
    pr_answer,
    pr_node,
    real,
    redigest,
    refusal_of,
    rev,
    shared_world_build,
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
    """Run the real collector with a package scan and return the parsed document.

    ``seconds`` (default 60) is the collection deadline."""
    seconds = options.pop("seconds", 60.0)
    if options.get("remote_checks"):
        # A networked check is taken in a whole second after the last git command of the
        # test, so a tracking ref that moved earlier is older than the check.
        time.sleep(max(0.0, LAST_GIT_SECOND[0] + 1 - time.time()))
    text = checkouts.collect_checkouts(
        [str(folder) for folder in folders],
        deadline_ns=time.monotonic_ns() + int(seconds * 1e9),
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
    # `spec/alpha` was created at the default head with no commit of its own.
    assert first["code_status"]["text"] == f"in progress {DOT} no commits yet"
    assert second["code_status"] is None
    assert [r["clones"][0]["path"] for r in document["repositories"]] == [real(repository)]


def test_without_a_completion_setting_no_package_is_finished(world):
    folder, _repository, root = world

    document = collect_packages([folder], root)

    assert document["package_scan"]["finished_policy"] == "not_configured"
    assert [p["finished"] for p in document["packages"]] == [False, False]


@pytest.mark.parametrize("completed", [(), ("Shipped", "Shipped"), ("bad/stage",)])
def test_an_invalid_or_empty_completion_setting_finishes_no_package(world, completed):
    folder, _repository, root = world

    document = collect_packages([folder], root, completed)

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
    root = package_root(tmp_path, {"w": targets["busy_work"]})
    # No branch is declared, so neither package has a reference or a package-branch entry.
    make_package(root, "Drafts", "n", f"Target repo: {targets['busy']}\nBranch: not applicable \u2014 x\n")
    document = json.loads(
        checkouts.collect_checkouts(
            [str(targets["folder"])],
            deadline_ns=time.monotonic_ns() + 60 * 10**9,
            package_scan=checkouts.PackageScan(root),
        )
    )
    busy = real(targets["busy"])
    package = document["packages"][target_index(document, "w")]
    assert_target(package["target"], "clone", busy)
    unreferenced = document["packages"][target_index(document, "n")]
    assert_target(unreferenced["target"], "clone", busy)
    assert unreferenced["branch"]["status"] == "not_applicable"
    package["target"] = {"status": "worktree", "clone": busy, "worktree": real(targets["busy_work"])}
    document["checkouts_digest"] = models.canonical_digest(
        document, omit=models.CHECKOUTS_DIGEST_OMITTED
    )

    with pytest.raises(models.ProtocolError, match="not listed"):
        models.parse_checkouts(document)

    package["target"] = {"status": "clone", "clone": busy, "worktree": None}
    unreferenced["target"] = {"status": "clone", "clone": "/fictional/nowhere", "worktree": None}
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
    assert [p["code_status"] for p in document["packages"]] == [None] * 4
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
    """A ``git`` stand-in that logs each call's arguments on one line, separated by U+001F."""
    log = tmp_path / "arguments.log"
    # The whole line is built first and appended with one write, so calls that run
    # concurrently cannot tear each other's lines.
    body = (
        'sep=$(printf "\\037"); line=""; for argument in "$@"; do line="$line$argument$sep"; done; '
        f'printf "%s\\n" "$line" >> "{log}"'
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
def test_without_inode_numbers_a_target_still_resolves_by_its_real_path(targets, monkeypatch):
    real_os = checkouts.os

    class NoInodes:
        """The ``os`` module as a file system that reports no inode numbers."""

        def __getattr__(self, name: str) -> Any:
            return getattr(real_os, name)

        @staticmethod
        def stat(*args: Any, **kwargs: Any) -> Any:
            details = real_os.stat(*args, **kwargs)
            return real_os.stat_result((details.st_mode, 0, *tuple(details)[2:10]))

    monkeypatch.setattr(checkouts, "os", NoInodes())
    assert checkouts._path_key(str(targets["main"]))[0] == "path", "control: the fallback runs"
    named = {
        "main": targets["main"],
        "busy": targets["busy"],
        "linked": targets["linked"],
        "plain": targets["folder"] / "Plain",
        "elsewhere": targets["elsewhere"],
    }
    if "alias" in targets:
        named["alias"] = targets["alias"]

    document = collect_packages(
        [targets["folder"]], package_root(targets["folder"].parent, named)
    )

    found = resolve(document)
    assert_target(found["main"], "clone", real(targets["main"]))
    assert_target(found["busy"], "clone", real(targets["busy"]))
    assert_target(found["linked"], "worktree", real(targets["main"]), real(targets["linked"]))
    assert_target(found["plain"], "not_git")
    assert_target(found["elsewhere"], "outside_folders")
    if "alias" in named:
        assert_target(found["alias"], "clone", real(targets["main"]))


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


def build_matching(tmp_path: Path) -> dict[str, Path]:
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


@pytest.fixture
def matching(tmp_path):
    return build_matching(tmp_path)


@pytest.fixture(scope="module")
def shared_matching(tmp_path_factory):
    with shared_world_build(tmp_path_factory, "matching") as (directory, _home, _patch):
        layout = build_matching(directory)
        document = collect_packages([layout["folder"]], layout["root"])
    # The unmutated document is accepted, so each refusal below comes from its mutation.
    assert models.parse_checkouts(json.dumps(redigest(doc_copy(document))))
    return layout, document


def path_of(name: str) -> str:
    return f"Fictional/Drafts/{name}"


def test_a_branch_is_matched_by_repository_group_and_not_by_name(collected, collected_layout):
    document = collected

    a = clone_at(document, collected_layout["a"])
    assert a["verdict_base"] is not None and a["containment"] == "complete"
    assert references_of(a, "topic") == [(path_of("target"), "target_clone")]
    assert references_of(clone_at(document, collected_layout["b"]), "topic") == [
        (path_of("target"), "sibling_clone")
    ]
    # Clone C shares the branch name with another repository's package, and the
    # package that targets C is the only one it serves.
    assert references_of(clone_at(document, collected_layout["c"]), "topic") == [
        (path_of("on_c"), "target_clone")
    ]
    assert references_of(clone_at(document, collected_layout["d"]), "topic") == [
        (path_of("on_d"), "target_clone")
    ]
    assert entries_of(repository_of(document, collected_layout["a"])) == [
        ("topic", [path_of("target")])
    ]
    assert entries_of(repository_of(document, collected_layout["c"])) == [
        ("topic", [path_of("on_c")])
    ]
    assert entries_of(repository_of(document, collected_layout["d"])) == [
        ("topic", [path_of("on_d")])
    ]


def test_a_helper_branch_contained_in_a_package_branch_is_folded_under_it(collected, collected_layout):
    document = collected

    for member in (collected_layout["a"], collected_layout["b"]):
        helper = [b for b in clone_at(document, member)["branches"] if b["name"] == "helper"]
        assert [b["contained_in"] for b in helper] == ["topic"]
        assert references_of(clone_at(document, member), "helper") == [
            (path_of("target"), "contained")
        ]
    # The other repository's helper folds under its own topic and serves only
    # the package that targets that repository.
    assert references_of(clone_at(document, collected_layout["c"]), "helper") == [
        (path_of("on_c"), "contained")
    ]
    assert references_of(clone_at(document, collected_layout["a"]), "trunk") == []


def test_packages_that_name_no_declared_branch_match_nothing(collected):
    document = collected

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


def test_a_package_naming_the_default_branch_is_not_matched(collected, collected_layout):
    document = collected

    package = {p["package_path"]: p for p in document["packages"]}[path_of("default")]
    assert set(package["branch"]) == models._PACKAGE_BRANCH_KEYS
    assert package["branch"]["status"] == "default_branch"
    assert package["branch"]["name"] is None
    assert path_of("default") not in {
        path
        for entry in repository_of(document, collected_layout["a"])["package_branches"]
        for path in entry["packages"]
    }
    for member in (collected_layout["a"], collected_layout["b"]):
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


def test_the_parser_refuses_a_reference_its_package_does_not_back(collected, collected_layout):
    document = collected
    a_index = document["repositories"].index(repository_of(document, collected_layout["a"]))

    def topic(doc: dict[str, Any], member: Path) -> dict[str, Any]:
        (found,) = [b for b in clone_at(doc, member)["branches"] if b["name"] == "topic"]
        return found

    def helper(doc: dict[str, Any], member: Path) -> dict[str, Any]:
        (found,) = [b for b in clone_at(doc, member)["branches"] if b["name"] == "helper"]
        return found

    def sibling_as_target(doc):
        topic(doc, collected_layout["b"])["packages"][0]["basis"] = "target_clone"

    def target_as_sibling(doc):
        topic(doc, collected_layout["a"])["packages"][0]["basis"] = "sibling_clone"

    def other_repository(doc):
        topic(doc, collected_layout["c"])["packages"][0]["package_path"] = path_of("target")

    def not_contained(doc):
        helper(doc, collected_layout["a"])["packages"][0]["basis"] = "contained"
        helper(doc, collected_layout["a"])["contained_in"] = None

    def unknown_package(doc):
        topic(doc, collected_layout["a"])["packages"][0]["package_path"] = "Fictional/Drafts/zz"

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


def test_the_parser_refuses_misplaced_or_misordered_package_references(collected, collected_layout, stacked):
    document = collected
    stack = collect_packages([stacked["folder"]], stacked["root"])

    def branch_of(doc: dict[str, Any], member: Path, name: str) -> dict[str, Any]:
        (found,) = [b for b in clone_at(doc, member)["branches"] if b["name"] == name]
        return found

    def entries(doc: dict[str, Any], member: Path) -> list[dict[str, Any]]:
        return repository_of(doc, member)["package_branches"]

    def helper_as_target(doc):
        branch_of(doc, collected_layout["a"], "helper")["packages"][0]["basis"] = "target_clone"

    def helper_as_sibling(doc):
        branch_of(doc, collected_layout["b"], "helper")["packages"][0]["basis"] = "sibling_clone"

    def sibling_in_another_repository(doc):
        branch_of(doc, collected_layout["c"], "topic")["packages"] = [
            {"package_path": path_of("target"), "basis": "sibling_clone"}
        ]

    def folded_into_another_repository(doc):
        branch_of(doc, collected_layout["c"], "helper")["packages"] = [
            {"package_path": path_of("target"), "basis": "contained"}
        ]

    def empty_entry(doc):
        entries(doc, collected_layout["a"])[0]["packages"] = []

    def repeated_package(doc):
        listed = entries(doc, collected_layout["a"])[0]["packages"]
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


# ---------------------------------------------------------------------------
# The remote copy of a package branch
# ---------------------------------------------------------------------------


def tree_fingerprint(repository: Path) -> dict[str, bytes]:
    """The bytes of every file in the repository's git directory: refs, reflogs,
    configuration and object files alike."""
    top = repository if (repository / "HEAD").is_file() else repository / ".git"
    return {
        os.path.relpath(member, top): member.read_bytes()
        for member in sorted(top.rglob("*"))
        if member.is_file()
    }


def remote_of(document: dict[str, Any], member: Path, name: str = "topic") -> dict[str, Any]:
    """The remote copy of one package branch, with its shape checked."""
    (entry,) = [e for e in repository_of(document, member)["package_branches"] if e["name"] == name]
    assert set(entry) == models._PACKAGE_BRANCH_ENTRY_KEYS
    remote = entry["remote"]
    if remote is not None:
        assert set(remote) == models._REMOTE_COPY_KEYS
    return remote


def assert_remote(
    remote: dict[str, Any], state: str, sha: str | None = None, basis: str | None = None
) -> None:
    assert remote["state"] == state
    assert remote["sha"] == sha
    assert remote["basis"] == basis


class RemoteWorld:
    """Clones A (the package target) and B of one remote, each holding ``topic``, a clone
    outside the checkout folders that can move the remote, and a package for ``topic``."""

    def __init__(self, tmp_path: Path, home: Path, *, fetched: bool = True) -> None:
        self.home = home
        (home / ".nyx").mkdir(exist_ok=True)
        served = tmp_path / "served"
        served.mkdir()
        self.remote = make_remote(served)
        self.folder = tmp_path / "folder"
        self.folder.mkdir()
        self.a = clone(self.remote.as_uri(), self.folder / "a")
        self.b = clone(self.remote.as_uri(), self.folder / "b")
        self.outside = clone(self.remote.as_uri(), tmp_path / "outside")
        git(self.a, "checkout", "-q", "-b", "topic")
        commit_file(self.a, "topic.txt", "one\n")
        git(self.a, "push", "-q", "-u", "origin", "topic")
        git(self.a, "checkout", "-q", "trunk")
        if fetched:
            git(self.b, "fetch", "-q", "origin")
            git(self.b, "branch", "topic", "origin/topic")
        self.root = tmp_path / "spec"
        make_package(self.root, "Drafts", "topic", f"Target repo: {self.a}\nBranch: topic\n")

    def tracking(self, member: Path) -> str:
        return rev(member, "refs/remotes/origin/topic")

    def advance_remote(self, name: str) -> str:
        """Push a new ``topic`` commit from the clone outside the folders."""
        git(self.outside, "fetch", "-q", "origin")
        git(self.outside, "checkout", "-q", "-B", "topic", "origin/topic")
        sha = commit_file(self.outside, name, "x\n")
        git(self.outside, "push", "-q", "origin", "topic")
        return sha

    def push_from(self, member: Path, name: str) -> str:
        """Bring ``member``'s ``topic`` up to the remote's, commit once and push."""
        git(member, "fetch", "-q", "origin")
        git(member, "checkout", "-q", "topic")
        git(member, "merge", "-q", "--ff-only", "origin/topic")
        sha = commit_file(member, name, "x\n")
        git(member, "push", "-q", "origin", "topic")
        git(member, "checkout", "-q", "trunk")
        return sha

    def collect(self, **options: Any) -> dict[str, Any]:
        """Collect with the folder's packages and prove no repository changed."""
        members = [m for m in (self.a, self.b, self.outside, self.remote) if m.exists()]
        before = {m: tree_fingerprint(m) for m in members}
        document = collect_packages([self.folder], self.root, **options)
        assert {m: tree_fingerprint(m) for m in members} == before
        return document


@pytest.fixture
def remote_world(tmp_path):
    # The autouse fixture has already pointed HOME at the isolated account home.
    return RemoteWorld(tmp_path, Path(os.environ["HOME"]))


def test_a_networked_collection_gives_the_remote_tip_and_then_absent(remote_world):
    world = remote_world
    live_tip = world.advance_remote("live.txt")
    assert world.tracking(world.a) != live_tip != world.tracking(world.b)

    present = world.collect(remote_checks=True)

    assert_remote(remote_of(present, world.a), "present", live_tip, "live")
    git(world.remote, "branch", "-q", "-D", "topic")

    deleted = world.collect(remote_checks=True)

    assert_remote(remote_of(deleted, world.a), "absent")
    assert world.tracking(world.a) != live_tip, "control: the clones still hold the old tip"


def test_without_network_the_record_answers_unless_a_clone_pushed_after_the_check(remote_world):
    world = remote_world
    saved = world.advance_remote("saved.txt")
    networked = world.collect(remote_checks=True)
    assert_remote(remote_of(networked, world.a), "present", saved, "live")
    # Control: both clones' tracking refs are older than the record.
    assert saved not in (world.tracking(world.a), world.tracking(world.b))

    older = world.collect()

    assert older["remote_checks"] == "off"
    assert repository_of(older, world.a)["remote"]["check"]["status"] == "recorded"
    assert_remote(remote_of(older, world.a), "present", saved, "recorded")

    from_a = world.push_from(world.a, "from-a.txt")
    after_a = world.collect()

    assert world.tracking(world.b) != from_a, "control: B still holds the old tip"
    assert_remote(remote_of(after_a, world.a), "present", from_a, "cached")

    time.sleep(1.1)
    from_b = world.push_from(world.b, "from-b.txt")
    after_b = world.collect()

    assert from_b != from_a
    assert_remote(remote_of(after_b, world.a), "present", from_b, "cached")


def test_a_clone_that_moved_its_ref_to_the_saved_tip_does_not_override_the_record(remote_world):
    world = remote_world
    saved = world.advance_remote("saved.txt")
    world.collect(remote_checks=True)
    git(world.a, "fetch", "-q", "origin")
    assert world.tracking(world.a) == saved

    document = world.collect()

    assert_remote(remote_of(document, world.a), "present", saved, "recorded")


def test_a_saved_absence_yields_to_a_clone_that_pushed_the_branch_later(remote_world):
    world = remote_world
    git(world.remote, "branch", "-q", "-D", "topic")
    absent = world.collect(remote_checks=True)
    assert_remote(remote_of(absent, world.a), "absent")

    assert_remote(remote_of(world.collect(), world.a), "absent")
    git(world.a, "checkout", "-q", "topic")
    pushed = commit_file(world.a, "again.txt", "x\n")
    git(world.a, "push", "-q", "origin", "topic")
    git(world.a, "checkout", "-q", "trunk")

    assert_remote(remote_of(world.collect(), world.a), "present", pushed, "cached")


def use_fresh_home(tmp_path: Path, monkeypatch, home: Path) -> Path:
    fresh = tmp_path / "fresh-home"
    fresh.mkdir()
    shutil.copy(home / ".gitconfig", fresh / ".gitconfig")
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(fresh))
    return fresh


def last_fetched(document: dict[str, Any], member: Path) -> str | None:
    return clone_at(document, member)["cached_default"]["last_fetched_at"]


def moved_after(member: Path, other: Path) -> bool:
    """Whether ``member``'s ``origin/topic`` moved in a later second than ``other``'s."""
    return int(move_second(member)) > int(move_second(other))


def reflog_off_clone(world: RemoteWorld, name: str) -> Path:
    """A clone of the remote in the checkout folder that writes no reflog at all."""
    return clone(
        world.remote.as_uri(), world.folder / name, "-c", "core.logAllRefUpdates=false"
    )


def assert_undated(member: Path, name: str = "topic") -> None:
    """The clone holds ``origin/<name>`` but no reflog says when it moved."""
    assert git(member, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{name}").stdout.strip()
    shown = git(member, "reflog", "show", f"refs/remotes/origin/{name}").stdout.strip()
    assert shown == "", "control: the ref has no move time"


def test_with_no_record_the_ref_that_moved_last_answers(tmp_path, remote_world, monkeypatch):
    world = remote_world
    time.sleep(1.1)
    newest = world.advance_remote("newest.txt")
    git(world.b, "fetch", "-q", "origin")
    assert world.tracking(world.a) != newest == world.tracking(world.b)
    assert moved_after(world.b, world.a), "control: B's ref moved after A's"
    use_fresh_home(tmp_path, monkeypatch, world.home)

    offline = world.collect()

    assert offline["remote_checks"] == "off"
    check = repository_of(offline, world.a)["remote"]["check"]
    assert (check["status"], check["reason"]) == ("off", "remote_checks_off")
    assert_remote(remote_of(offline, world.a), "present", newest, "cached")

    # A failed networked check with nothing saved answers the same way.
    elsewhere = world.remote.rename(tmp_path / "moved.git")
    try:
        failed = world.collect(remote_checks=True)
    finally:
        elsewhere.rename(world.remote)
    assert repository_of(failed, world.a)["remote"]["check"]["status"] == "failed"
    assert_remote(remote_of(failed, world.a), "present", newest, "cached")


def test_with_no_record_a_clone_that_pushed_without_ever_fetching_answers(
    tmp_path, remote_world, monkeypatch
):
    world = remote_world
    git(world.b, "fetch", "-q", "origin")
    time.sleep(1.1)
    git(world.a, "checkout", "-q", "-b", "x", "trunk")
    pushed = commit_file(world.a, "x.txt", "x\n")
    git(world.a, "push", "-q", "-u", "origin", "x")
    git(world.a, "checkout", "-q", "trunk")
    make_package(world.root, "Drafts", "x", f"Target repo: {world.a}\nBranch: x\n")
    use_fresh_home(tmp_path, monkeypatch, world.home)

    document = world.collect(remote_checks=False)

    assert last_fetched(document, world.a) is None, "control: A never fetched"
    assert last_fetched(document, world.b) is not None, "control: B fetched"
    assert not (world.b / ".git" / "refs" / "remotes" / "origin" / "x").exists(), (
        "control: B holds no origin/x"
    )
    assert_remote(remote_of(document, world.a, "x"), "present", pushed, "cached")
    assert_status(document, "x", f"in progress {DOT} pushed", "in_progress", push="pushed")


def test_with_no_record_a_narrow_fetch_does_not_make_its_clone_the_answer(
    tmp_path, remote_world, monkeypatch
):
    world = remote_world
    time.sleep(1.1)
    newest = world.advance_remote("newest.txt")
    git(world.a, "fetch", "-q", "origin")
    time.sleep(1.1)
    git(world.b, "fetch", "-q", "origin", "trunk")
    use_fresh_home(tmp_path, monkeypatch, world.home)
    assert world.tracking(world.a) == newest != world.tracking(world.b)

    document = world.collect()

    assert last_fetched(document, world.a) < last_fetched(document, world.b), (
        "control: B's narrow fetch is the later one"
    )
    assert moved_after(world.a, world.b), "control: A's ref moved after B's"
    assert_remote(remote_of(document, world.a), "present", newest, "cached")


def test_with_no_record_a_clone_that_keeps_no_reflog_pushes_and_its_ref_answers(
    tmp_path, remote_world, monkeypatch
):
    world = remote_world
    pusher = reflog_off_clone(world, "reflog-off")
    git(pusher, "checkout", "-q", "-b", "x", "trunk")
    pushed = commit_file(pusher, "x.txt", "x\n")
    git(pusher, "push", "-q", "-u", "origin", "x")
    git(pusher, "checkout", "-q", "trunk")
    make_package(world.root, "Drafts", "x", f"Target repo: {pusher}\nBranch: x\n")
    use_fresh_home(tmp_path, monkeypatch, world.home)
    assert_undated(pusher, "x")

    document = world.collect(remote_checks=False)

    assert last_fetched(document, pusher) is None, "control: it never fetched"
    assert last_fetched(document, world.b) is not None, "control: B fetched"
    assert_remote(remote_of(document, world.a, "x"), "present", pushed, "cached")
    assert_status(document, "x", f"in progress {DOT} pushed", "in_progress", push="pushed")


def test_with_no_record_an_undated_ref_ranks_below_a_dated_one(
    tmp_path, remote_world, monkeypatch
):
    world = remote_world
    # It sorts before A and B in natural order, and holds the newest tip.
    pusher = reflog_off_clone(world, "0-reflog-off")
    git(pusher, "checkout", "-q", "-B", "topic", "origin/topic")
    newer = commit_file(pusher, "newer.txt", "x\n")
    git(pusher, "push", "-q", "origin", "topic")
    git(pusher, "checkout", "-q", "trunk")
    use_fresh_home(tmp_path, monkeypatch, world.home)
    assert_undated(pusher)
    older = world.tracking(world.a)
    assert newer != older == world.tracking(world.b)
    assert world.tracking(pusher) == newer
    assert sorted([real(pusher), real(world.a), real(world.b)])[0] == real(pusher)

    document = world.collect(remote_checks=False)

    assert_remote(remote_of(document, world.a), "present", older, "cached")


def test_with_no_record_undated_refs_tie_to_the_first_clone_in_natural_order(
    tmp_path, remote_world, monkeypatch
):
    world = remote_world
    # Two clones that keep no reflog hold different tips of ``y``; the later push is not
    # preferred, since neither ref has a move time. A and B hold no ``origin/y``.
    first = reflog_off_clone(world, "0-first")
    second = reflog_off_clone(world, "1-second")
    git(first, "checkout", "-q", "-b", "y", "trunk")
    from_first = commit_file(first, "y-first.txt", "x\n")
    git(first, "push", "-q", "-u", "origin", "y")
    git(first, "checkout", "-q", "trunk")
    time.sleep(1.1)
    git(second, "checkout", "-q", "-b", "y", "trunk")
    from_second = commit_file(second, "y-second.txt", "x\n")
    git(second, "push", "-q", "-f", "-u", "origin", "y")
    git(second, "checkout", "-q", "trunk")
    make_package(world.root, "Drafts", "y", f"Target repo: {first}\nBranch: y\n")
    use_fresh_home(tmp_path, monkeypatch, world.home)
    for member in (first, second):
        assert_undated(member, "y")
    assert rev(first, "refs/remotes/origin/y") == from_first != from_second
    assert rev(second, "refs/remotes/origin/y") == from_second
    for member in (world.a, world.b):
        assert not (member / ".git" / "refs" / "remotes" / "origin" / "y").exists()
    order = [real(m) for m in (first, second, world.a, world.b)]
    assert sorted(order) == order, "control: the first clone sorts first"

    document = world.collect(remote_checks=False)

    assert_remote(remote_of(document, world.a, "y"), "present", from_first, "cached")


def test_with_no_record_moves_in_the_same_second_go_to_the_first_clone_in_natural_order(
    tmp_path, remote_world, monkeypatch
):
    world = remote_world
    old = world.tracking(world.b)
    monkeypatch.setenv("GIT_COMMITTER_DATE", f"@{int(time.time()) + 3600} +0000")
    from_a = world.push_from(world.a, "from-a.txt")
    git(world.b, "fetch", "-q", "origin")
    git(world.b, "checkout", "-q", "-B", "topic", old)
    from_b = commit_file(world.b, "from-b.txt", "x\n")
    git(world.b, "push", "-q", "-f", "origin", "topic")
    git(world.b, "checkout", "-q", "trunk")
    monkeypatch.delenv("GIT_COMMITTER_DATE")
    use_fresh_home(tmp_path, monkeypatch, world.home)
    assert (world.tracking(world.a), world.tracking(world.b)) == (from_a, from_b)
    assert move_second(world.a) == move_second(world.b), "control: the moves tie"

    document = world.collect()

    assert_remote(remote_of(document, world.a), "present", from_a, "cached")


def test_with_no_record_a_branch_one_clone_pruned_and_another_holds_is_present(
    tmp_path, remote_world, monkeypatch
):
    world = remote_world
    time.sleep(1.1)
    git(world.remote, "branch", "-q", "-D", "topic")
    git(world.b, "fetch", "-q", "--prune", "origin")
    use_fresh_home(tmp_path, monkeypatch, world.home)
    held = world.tracking(world.a)
    assert held
    pruned = git(world.b, "rev-parse", "-q", "--verify", "refs/remotes/origin/topic", check=False)
    assert pruned.returncode != 0, "control: B's newest fetch pruned the ref"

    document = world.collect()

    assert last_fetched(document, world.a) is None or (
        last_fetched(document, world.a) < last_fetched(document, world.b)
    ), "control: the pruning clone fetched later"
    assert_remote(remote_of(document, world.a), "present", held, "cached")
    assert_status(
        document, "topic", f"in progress {DOT} push state unknown", "in_progress", push="unknown"
    )


def test_with_no_record_and_no_clone_ever_fetched_the_pusher_ref_answers(
    tmp_path, monkeypatch
):
    world = RemoteWorld(tmp_path, Path(os.environ["HOME"]), fetched=False)
    use_fresh_home(tmp_path, monkeypatch, world.home)

    document = world.collect()

    for member in (world.a, world.b):
        assert last_fetched(document, member) is None, "control: no clone ever fetched"
    assert_remote(remote_of(document, world.a), "present", world.tracking(world.a), "cached")
    assert_status(document, "topic", f"in progress {DOT} pushed", "in_progress", push="pushed")


def test_with_no_record_a_branch_no_clone_holds_is_unknown_never_absent(
    tmp_path, remote_world, monkeypatch
):
    world = remote_world
    git(world.a, "checkout", "-q", "-b", "solo", "trunk")
    commit_file(world.a, "solo.txt", "x\n")
    git(world.a, "checkout", "-q", "trunk")
    make_package(world.root, "Drafts", "solo", f"Target repo: {world.a}\nBranch: solo\n")
    use_fresh_home(tmp_path, monkeypatch, world.home)

    document = world.collect()

    assert last_fetched(document, world.b) is not None, "control: B has fetched"
    for member in (world.a, world.b):
        assert not (member / ".git" / "refs" / "remotes" / "origin" / "solo").exists()
    assert_remote(remote_of(document, world.a, "solo"), "unknown")
    assert_status(
        document, "solo", f"in progress {DOT} push state unknown", "in_progress", push="unknown"
    )


@posix_only
def test_with_no_record_a_move_time_is_read_only_for_a_ref_a_clone_holds(
    tmp_path, monkeypatch
):
    world = RemoteWorld(tmp_path, Path(os.environ["HOME"]), fetched=False)
    git(world.a, "checkout", "-q", "-b", "solo", "trunk")
    commit_file(world.a, "solo.txt", "x\n")
    git(world.a, "checkout", "-q", "trunk")
    make_package(world.root, "Drafts", "solo", f"Target repo: {world.a}\nBranch: solo\n")
    use_fresh_home(tmp_path, monkeypatch, world.home)
    shim, log = argument_recorder(tmp_path)

    with path_with(monkeypatch, shim):
        document = world.collect(remote_checks=False)

    assert_remote(remote_of(document, world.a), "present", world.tracking(world.a), "cached")
    assert_remote(remote_of(document, world.a, "solo"), "unknown")
    assert move_reads(log) == [real(world.a)], "one read, for the clone that holds the ref"
    assert move_reads(log, "solo") == [], "no read for a name no clone holds"


def test_an_already_expired_deadline_still_yields_a_document_that_parses(remote_world):
    world = remote_world
    world.collect(remote_checks=True)

    text = checkouts.collect_checkouts(
        [str(world.folder)],
        deadline_ns=time.monotonic_ns() - 1,
        remote_checks=True,
        package_scan=checkouts.PackageScan(world.root, ()),
    )

    document = models.parse_checkouts(text)
    # Not even the version call could start, so no repository was read and the package
    # is listed with its target unchecked.
    assert document["git"]["status"] == "too_old"
    assert document["repositories"] == []
    (package,) = document["packages"]
    assert package["package_path"] == "Fictional/Drafts/topic"
    assert package["target"]["status"] == "unchecked"
    assert package["branch"] == {"status": "declared", "name": "topic"}


def test_a_package_branch_in_a_clone_without_origin_has_no_remote_copy(collected, collected_layout):
    document = collected

    repository = repository_of(document, collected_layout["d"])
    assert repository["remote"] is None
    assert [(e["name"], e["packages"], e["remote"]) for e in repository["package_branches"]] == [
        ("topic", [path_of("on_d")], None)
    ]
    # The group of A and B has a remote and so a remote copy.
    assert remote_of(document, collected_layout["a"])["state"] == "unknown"


def test_the_parser_refuses_a_remote_copy_its_remote_check_cannot_have_given(
    tmp_path, remote_world, monkeypatch
):
    world = remote_world
    live = world.collect(remote_checks=True)
    recorded = world.collect()
    # With no record in the fresh account home, the check is off, or failed when networked.
    use_fresh_home(tmp_path, monkeypatch, world.home)
    off = world.collect()
    elsewhere = world.remote.rename(tmp_path / "moved.git")
    try:
        failed = world.collect(remote_checks=True)
    finally:
        elsewhere.rename(world.remote)
    assert repository_of(off, world.a)["remote"]["check"]["status"] == "off"
    assert repository_of(failed, world.a)["remote"]["check"]["status"] == "failed"

    def entry_of(document: dict[str, Any]) -> dict[str, Any]:
        (found,) = repository_of(document, world.a)["package_branches"]
        return found

    def sha_without_state(doc):
        entry_of(doc)["remote"].update(state="absent")

    def present_without_basis(doc):
        entry_of(doc)["remote"]["basis"] = None

    def present_without_sha(doc):
        entry_of(doc)["remote"]["sha"] = None

    def unknown_under_a_live_check(doc):
        entry_of(doc)["remote"].update(state="unknown", sha=None, basis=None)

    def absent_without_an_answer(doc):
        entry_of(doc)["remote"].update(state="absent", sha=None, basis=None)

    def cached_under_a_live_check(doc):
        entry_of(doc)["remote"]["basis"] = "cached"

    def live_under_a_recorded_check(doc):
        entry_of(doc)["remote"]["basis"] = "live"

    def missing_remote(doc):
        entry_of(doc)["remote"] = None

    def unknown_state(doc):
        entry_of(doc)["remote"]["state"] = "gone"

    def extra_key(doc):
        entry_of(doc)["remote"]["checked_at"] = None

    for document, change, message in (
        (live, sha_without_state, "sets sha and basis exactly for present"),
        (live, present_without_basis, "sets sha and basis exactly for present"),
        (live, present_without_sha, "sets sha and basis exactly for present"),
        (live, unknown_under_a_live_check, "cannot be unknown under a live remote check"),
        (live, cached_under_a_live_check, "basis does not match the remote check"),
        (off, absent_without_an_answer, "cannot be absent when the remote check is off"),
        (failed, absent_without_an_answer, "cannot be absent when the remote check is failed"),
        (recorded, unknown_under_a_live_check, "cannot be unknown under a recorded remote check"),
        (recorded, live_under_a_recorded_check, "basis does not match the remote check"),
        (live, missing_remote, "must be an object"),
        (live, unknown_state, "state is invalid"),
        (live, extra_key, "remote has an invalid shape"),
    ):
        assert message in reference_mutation(document, change), change.__name__


def test_the_parser_refuses_a_remote_copy_on_a_repository_without_a_remote(collected, collected_layout):
    document = collected

    def copy_without_origin(doc):
        (entry,) = repository_of(doc, collected_layout["d"])["package_branches"]
        entry["remote"] = {"state": "unknown", "sha": None, "basis": None}

    assert "must be null without a remote" in reference_mutation(document, copy_without_origin)


def logged_calls(log: Path) -> list[list[str]]:
    return [line.split("\x1f")[:-1] for line in log.read_text().splitlines()]


def move_reads(log: Path, name: str = "topic") -> list[str]:
    """The ``-C`` directory of every reflog read of ``refs/remotes/origin/<name>``."""
    return [
        real(call[call.index("-C") + 1])
        for call in logged_calls(log)
        if "-C" in call and "reflog" in call and f"refs/remotes/origin/{name}" in call
    ]


def listings(log: Path) -> int:
    return sum(1 for call in logged_calls(log) if "for-each-ref" in call)


@posix_only
def test_a_move_time_is_read_once_per_clone_and_name_and_no_listing_is_added(
    remote_world, tmp_path, monkeypatch
):
    world = remote_world
    world.advance_remote("saved.txt")
    world.collect(remote_checks=True)
    from_a = world.push_from(world.a, "from-a.txt")
    # A holds no local branch of the name: only the package asks for its move time.
    git(world.a, "branch", "-q", "-D", "topic")
    shim, with_packages = argument_recorder(tmp_path)

    with path_with(monkeypatch, shim):
        document = collect_packages([world.folder], world.root)

    assert_remote(remote_of(document, world.a), "present", from_a, "cached")
    reads = move_reads(with_packages)
    assert sorted(reads) == sorted([real(world.a), real(world.b)]), "one read per clone"

    (tmp_path / "plain").mkdir()
    shim, without_packages = argument_recorder(tmp_path / "plain")
    with path_with(monkeypatch, shim):
        checkouts.collect_checkouts(
            [str(world.folder)], deadline_ns=time.monotonic_ns() + 60 * 10**9
        )
    assert listings(without_packages) > 0, "control: the log names the listings"
    assert listings(with_packages) == listings(without_packages)


def move_second(member: Path) -> str:
    """The Unix second of the newest reflog entry of ``member``'s ``origin/topic``."""
    shown = git(
        member, "reflog", "show", "-n", "1", "--date=unix", "--format=%gd",
        "refs/remotes/origin/topic",
    ).stdout.strip()
    return shown[shown.index("@{") + 2 : -1]


def test_moves_in_the_same_second_go_to_the_first_clone_in_natural_order(
    remote_world, monkeypatch
):
    world = remote_world
    saved = world.advance_remote("saved.txt")
    world.collect(remote_checks=True)
    # Both clones move their tracking ref after the check, in one and the same second.
    monkeypatch.setenv("GIT_COMMITTER_DATE", f"@{int(time.time()) + 3600} +0000")
    from_a = world.push_from(world.a, "from-a.txt")
    git(world.b, "fetch", "-q", "origin")
    git(world.b, "checkout", "-q", "-B", "topic", saved)
    from_b = commit_file(world.b, "from-b.txt", "x\n")
    git(world.b, "push", "-q", "-f", "origin", "topic")
    git(world.b, "checkout", "-q", "trunk")
    monkeypatch.delenv("GIT_COMMITTER_DATE")
    assert (world.tracking(world.a), world.tracking(world.b)) == (from_a, from_b)
    assert move_second(world.a) == move_second(world.b), "control: the moves tie"

    document = world.collect()

    assert_remote(remote_of(document, world.a), "present", from_a, "cached")


def reflog_reads(log: Path) -> list[str]:
    """The ``-C`` directory of every ``reflog`` call, one logged line per call."""
    return [
        real(call[call.index("-C") + 1])
        for call in logged_calls(log)
        if "reflog" in call and "-C" in call
    ]


@posix_only
def test_a_move_time_is_read_only_under_a_saved_answer_and_for_a_ref_that_differs(
    remote_world, tmp_path, monkeypatch
):
    world = remote_world
    saved = world.advance_remote("saved.txt")
    # One worker, so each logged call is written whole before the next starts.
    (tmp_path / "live").mkdir()
    shim, live_log = argument_recorder(tmp_path / "live")
    with path_with(monkeypatch, shim):
        live = collect_packages([world.folder], world.root, remote_checks=True, max_parallel=1)
    assert_remote(remote_of(live, world.a), "present", saved, "live")
    assert reflog_reads(live_log) == [], "a live answer needs no move time"

    git(world.a, "fetch", "-q", "origin")
    assert world.tracking(world.a) == saved != world.tracking(world.b)
    (tmp_path / "recorded").mkdir()
    shim, recorded_log = argument_recorder(tmp_path / "recorded")
    with path_with(monkeypatch, shim):
        recorded = collect_packages([world.folder], world.root, max_parallel=1)

    assert_remote(remote_of(recorded, world.a), "present", saved, "recorded")
    assert reflog_reads(recorded_log) == [real(world.b)], "only the ref that differs is read"


@posix_only
def test_a_move_time_read_that_cannot_finish_counts_as_not_moved(
    remote_world, tmp_path, monkeypatch
):
    world = remote_world
    saved = world.advance_remote("saved.txt")
    world.collect(remote_checks=True)
    from_a = world.push_from(world.a, "from-a.txt")
    # Control: a read that finishes sees A's push.
    assert_remote(remote_of(world.collect(), world.a), "present", from_a, "cached")
    slow = stand_in(tmp_path, "slow-reflog", 'case "$*" in *" reflog "*) exec sleep 5;; esac')

    with path_with(monkeypatch, slow):
        document = world.collect(call_cap_seconds=1.0)

    assert repository_of(document, world.a)["remote"]["check"]["status"] == "recorded"
    assert_remote(remote_of(document, world.a), "present", saved, "recorded")


def test_the_parser_refuses_a_present_remote_copy_whose_sha_is_not_an_object_id(remote_world):
    world = remote_world
    live = world.collect(remote_checks=True)

    def short_sha(doc):
        (entry,) = repository_of(doc, world.a)["package_branches"]
        entry["remote"]["sha"] = entry["remote"]["sha"][:7]

    def zero_sha(doc):
        (entry,) = repository_of(doc, world.a)["package_branches"]
        entry["remote"]["sha"] = "0" * 40

    for change in (short_sha, zero_sha):
        assert "remote.sha must be an object ID" in reference_mutation(live, change), (
            change.__name__
        )


DOT = "\u00b7"


def status_fields(state, **given):
    fields = {
        "state": state,
        "pull_request": None,
        "push": None,
        "unpushed": None,
        "cleanup": None,
        "checked_out_elsewhere": [],
    }
    fields.update(given)
    return fields


@pytest.mark.parametrize(
    ("fields", "text"),
    [
        (status_fields("merged", cleanup={"worktrees": 0, "branches": 0}), "merged"),
        (
            status_fields("merged", cleanup={"worktrees": 1, "branches": 1}),
            f"merged {DOT} cleanup pending (1 worktree)",
        ),
        (
            status_fields("merged", cleanup={"worktrees": 2, "branches": 2}),
            f"merged {DOT} cleanup pending (2 worktrees)",
        ),
        (
            status_fields("merged", cleanup={"worktrees": 0, "branches": 1}),
            f"merged {DOT} cleanup pending (1 branch)",
        ),
        (
            status_fields("merged", cleanup={"worktrees": 0, "branches": 2}),
            f"merged {DOT} cleanup pending (2 branches)",
        ),
        (
            status_fields("pr_open", pull_request=7, push="unpushed", unpushed=1),
            f"PR #7 open {DOT} 1 unpushed",
        ),
        (
            status_fields("pr_open", pull_request=7, push="unpushed", unpushed=12),
            f"PR #7 open {DOT} 12 unpushed",
        ),
        (status_fields("pr_open", pull_request=7, push="pushed"), "PR #7 open"),
        (status_fields("pr_open", pull_request=7, push="unknown"), "PR #7 open"),
        (status_fields("pr_open", pull_request=7, push="not_pushed"), "PR #7 open"),
        (status_fields("awaiting_merge"), "awaiting merge (no PR)"),
        (status_fields("superseded"), f"superseded {DOT} branch retained"),
        (status_fields("in_progress", push="pushed"), f"in progress {DOT} pushed"),
        (
            status_fields("in_progress", push="unpushed", unpushed=2),
            f"in progress {DOT} 2 unpushed",
        ),
        (status_fields("in_progress", push="not_pushed"), f"in progress {DOT} not pushed"),
        (
            status_fields("in_progress", push="unknown"),
            f"in progress {DOT} push state unknown",
        ),
        (status_fields("unknown"), "merge state unknown"),
        (status_fields("no_commits_yet"), f"in progress {DOT} no commits yet"),
        (
            status_fields("no_commits_yet", checked_out_elsewhere=["/w/Solo"]),
            f"in progress {DOT} no commits yet {DOT} in Solo",
        ),
        (
            status_fields(
                "no_commits_yet", checked_out_elsewhere=["/w/Repo Clone 10", "C:\\w\\Repo Clone 9"]
            ),
            f"in progress {DOT} no commits yet {DOT} in Repo Clone 9, Repo Clone 10",
        ),
    ],
)
def test_code_status_text_for_every_state(fields, text):
    assert models.code_status_text(fields) == text


def test_code_status_text_names_other_worktrees_the_same_on_every_host():
    suffix = f" {DOT} in Repo Clone 9, Repo Clone 10"
    for paths in (
        ["/w/Repo Clone 10", "C:\\w\\Repo Clone 9"],
        ["C:\\w\\Repo Clone 9", "/w/Repo Clone 10"],
        ["/w/Repo Clone 10/", "C:\\w\\Repo Clone 9\\"],
    ):
        fields = status_fields("unknown", checked_out_elsewhere=paths)
        assert models.code_status_text(fields) == "merge state unknown" + suffix
    one = status_fields("awaiting_merge", checked_out_elsewhere=["/w/Solo"])
    assert models.code_status_text(one) == f"awaiting merge (no PR) {DOT} in Solo"
    pr = status_fields(
        "pr_open",
        pull_request=3,
        push="unpushed",
        unpushed=2,
        checked_out_elsewhere=["/a/b"],
    )
    assert models.code_status_text(pr) == f"PR #3 open {DOT} 2 unpushed {DOT} in b"
    merged = status_fields(
        "merged",
        cleanup={"worktrees": 2, "branches": 2},
        checked_out_elsewhere=["/a/x", "/a/y"],
    )
    assert (
        models.code_status_text(merged)
        == f"merged {DOT} cleanup pending (2 worktrees) {DOT} in x, y"
    )


def test_code_status_text_counts_worktrees_when_the_names_do_not_fit():
    paths = [f"/w/{letter}{'x' * 399}" for letter in "abc"]
    fields = status_fields("unknown", checked_out_elsewhere=paths)
    text = models.code_status_text(fields)
    assert text == f"merge state unknown {DOT} in 3 other worktrees"
    # Two such names still fit within 1,024 UTF-16 units, so they are listed.
    two = models.code_status_text(
        status_fields("unknown", checked_out_elsewhere=paths[:2])
    )
    assert two == f"merge state unknown {DOT} in {'a' + 'x' * 399}, {'b' + 'x' * 399}"
    assert len(two.encode("utf-16-le")) // 2 <= models.MAX_TEXT_UNITS
    # The count form is chosen on UTF-16 units, so astral characters count twice.
    astral = ["/w/" + "\U0001f600" * 260, "/w/" + "\U0001f600" * 260]
    counted = models.code_status_text(
        status_fields("unknown", checked_out_elsewhere=astral)
    )
    assert counted == f"merge state unknown {DOT} in 2 other worktrees"


def test_code_status_text_count_form_is_singular_for_one_oversized_name():
    fields = status_fields("unknown", checked_out_elsewhere=["/w/" + "x" * 1010])
    assert models.code_status_text(fields) == f"merge state unknown {DOT} in 1 other worktree"


# ---------------------------------------------------------------------------
# Code Status from real branches, remotes, pull requests and worktrees
# ---------------------------------------------------------------------------

FINISHED_STAGES = ("Shipped", "Shelf")
UNKNOWN_HEAD = "a" * 40


def write_package(root: Path, stage: str, name: str, header: str) -> None:
    directory = root / "Fictional" / stage / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "spec.md").write_text(f"# {name}\n{header}", encoding="utf-8")


def status_of(document: dict[str, Any], package_path: str) -> dict[str, Any] | None:
    """The Code Status of one package, its shape and its text checked against its fields."""
    (package,) = [p for p in document["packages"] if p["package_path"] == package_path]
    status = package["code_status"]
    if status is not None:
        assert set(status) == models._CODE_STATUS_KEYS
        assert status["text"] == models.code_status_text(status)
    return status


def assert_status(
    document: dict[str, Any],
    name: str,
    text: str,
    state: str,
    *,
    stage: str = "Drafts",
    push: str | None = None,
    unpushed: int | None = None,
    pull_request: int | None = None,
    cleanup: dict[str, int] | None = None,
    elsewhere: tuple[Path, ...] = (),
) -> None:
    status = status_of(document, f"Fictional/{stage}/{name}")
    assert status is not None, f"{name} has no Code Status"
    assert status["text"] == text
    assert status["state"] == state
    assert status["push"] == push
    assert status["unpushed"] == unpushed
    assert status["pull_request"] == pull_request
    assert status["cleanup"] == cleanup
    assert status["checked_out_elsewhere"] == [real(path) for path in elsewhere]


def relation_of(document: dict[str, Any], member: Path, name: str) -> tuple[str, int, int]:
    (branch,) = [b for b in clone_at(document, member)["branches"] if b["name"] == name]
    comparison = branch["vs_remote"]
    return comparison["relation"], comparison["ahead"], comparison["behind"]


def relation_of_or_none(document: dict[str, Any], member: Path, name: str) -> str | None:
    (branch,) = [b for b in clone_at(document, member)["branches"] if b["name"] == name]
    return branch["vs_remote"]["relation"]


class StatusWorld:
    """Clones ``Repo Clone A`` (the target of every package) and ``Repo Clone B`` of one
    remote whose default branch is ``trunk``, a clone outside the checkout folders that can
    move the remote, and a specification root whose ``Shipped`` and ``Shelf`` stages are
    finished."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        # The account home that holds the saved remote checks.
        (Path(os.environ["HOME"]) / ".nyx").mkdir(exist_ok=True)
        served = tmp_path / "served"
        served.mkdir()
        self.remote = make_remote(served)
        self.folder = tmp_path / "folder"
        self.folder.mkdir()
        self.a = clone(self.remote, self.folder / "Repo Clone A")
        self.b = clone(self.remote, self.folder / "Repo Clone B")
        self.outside = clone(self.remote, tmp_path / "outside")
        self.root = tmp_path / "spec"
        self.counter = 0

    def package(
        self,
        name: str,
        branch: str,
        *,
        stage: str = "Drafts",
        target: Path | None = None,
        extra: str = "",
    ) -> None:
        write_package(
            self.root,
            stage,
            name,
            f"Target repo: {target or self.a}\nBranch: {branch}\n{extra}",
        )

    def commit(self, member: Path, count: int = 1) -> str:
        sha = ""
        for _ in range(count):
            self.counter += 1
            sha = commit_file(member, f"change-{self.counter}.txt", f"{self.counter}\n")
        return sha

    def branch(self, member: Path, name: str, commits: int = 1, *, push: bool = False) -> None:
        """Create ``name`` from ``trunk`` with ``commits`` commits and leave ``member`` on
        ``trunk``."""
        git(member, "checkout", "-q", "-b", name, "trunk")
        self.commit(member, commits)
        if push:
            git(member, "push", "-q", "origin", name)
        git(member, "checkout", "-q", "trunk")

    def take(self, member: Path, name: str, *, commits: int = 0) -> None:
        """Fetch ``name`` into ``member`` as a local branch, add commits and leave ``trunk``."""
        git(member, "fetch", "-q", "origin")
        git(member, "checkout", "-q", "-b", name, f"origin/{name}")
        self.commit(member, commits)
        git(member, "checkout", "-q", "trunk")

    def push_from_outside(self, name: str) -> str:
        """Push one more commit on ``name`` from the clone no configured folder holds."""
        git(self.outside, "fetch", "-q", "origin")
        git(self.outside, "checkout", "-q", "-B", name, f"origin/{name}")
        sha = self.commit(self.outside)
        git(self.outside, "push", "-q", "origin", name)
        return sha

    def github(self) -> None:
        for member in (self.a, self.b, self.outside):
            point_origin_at(member, self.remote, GITHUB_URL)

    def collect(self, **options: Any) -> dict[str, Any]:
        options.setdefault("remote_checks", True)
        return collect_packages([self.folder], self.root, FINISHED_STAGES, **options)


@pytest.fixture
def status_world(tmp_path):
    return StatusWorld(tmp_path)


def test_each_push_state_and_package_state_reads_its_own_text(status_world):
    w = status_world
    w.branch(w.a, "pushed", push=True)
    w.branch(w.a, "ahead", push=True)
    git(w.a, "checkout", "-q", "ahead")
    w.commit(w.a, 2)
    git(w.a, "checkout", "-q", "trunk")
    w.branch(w.a, "never")
    w.branch(w.a, "waiting")
    w.branch(w.a, "retired")
    w.branch(w.a, "both")
    for name in ("pushed", "ahead", "never"):
        w.package(name, name)
    w.package("waiting", "waiting", stage="Shipped")
    w.package("retired", "retired", extra=f"Superseded By: {ID_OLD}\n")
    w.package("both", "both", stage="Shipped", extra=f"Superseded By: {ID_OLD}\n")

    document = w.collect()

    assert_status(document, "pushed", f"in progress {DOT} pushed", "in_progress", push="pushed")
    assert_status(
        document, "ahead", f"in progress {DOT} 2 unpushed", "in_progress",
        push="unpushed", unpushed=2,
    )
    assert_status(
        document, "never", f"in progress {DOT} not pushed", "in_progress", push="not_pushed"
    )
    assert_status(
        document, "waiting", "awaiting merge (no PR)", "awaiting_merge", stage="Shipped"
    )
    assert_status(
        document, "retired", f"superseded {DOT} branch retained", "superseded"
    )
    # A finished package that names a successor is still awaiting its merge.
    assert_status(document, "both", "awaiting merge (no PR)", "awaiting_merge", stage="Shipped")


def test_a_local_copy_behind_the_remote_copy_reads_pushed(status_world):
    w = status_world
    w.branch(w.a, "topic", push=True)
    w.take(w.b, "topic", commits=1)
    git(w.b, "push", "-q", "origin", "topic")
    git(w.a, "fetch", "-q", "origin")
    w.package("topic", "topic")

    document = w.collect()

    assert relation_of(document, w.a, "topic") == ("behind", 0, 1), "control"
    assert relation_of(document, w.b, "topic") == ("equal", 0, 0), "control"
    assert_status(document, "topic", f"in progress {DOT} pushed", "in_progress", push="pushed")


def test_a_diverged_local_copy_counts_its_own_commits_as_unpushed(status_world):
    w = status_world
    w.branch(w.a, "topic", push=True)
    w.push_from_outside("topic")
    git(w.a, "fetch", "-q", "origin")
    git(w.a, "checkout", "-q", "topic")
    w.commit(w.a, 2)
    git(w.a, "checkout", "-q", "trunk")
    w.package("topic", "topic")

    document = w.collect()

    assert relation_of(document, w.a, "topic") == ("diverged", 2, 1), "control"
    assert_status(
        document, "topic", f"in progress {DOT} 2 unpushed", "in_progress",
        push="unpushed", unpushed=2,
    )


def test_the_largest_unpushed_count_of_the_local_copies_is_shown(status_world):
    w = status_world
    for name, in_a, in_b in (("first-small", 1, 3), ("first-large", 3, 1)):
        w.branch(w.a, name, push=True)
        w.take(w.b, name)
        for member, extra in ((w.a, in_a), (w.b, in_b)):
            git(member, "checkout", "-q", name)
            w.commit(member, extra)
            git(member, "checkout", "-q", "trunk")
        w.package(name, name)

    document = w.collect()

    assert relation_of(document, w.a, "first-small") == ("ahead", 1, 0), "control"
    assert relation_of(document, w.b, "first-small") == ("ahead", 3, 0), "control"
    for name in ("first-small", "first-large"):
        assert_status(
            document, name, f"in progress {DOT} 3 unpushed", "in_progress",
            push="unpushed", unpushed=3,
        )


def test_a_commit_no_configured_clone_has_fetched_leaves_the_push_state_unknown(status_world):
    w = status_world
    w.branch(w.a, "topic", push=True)
    w.push_from_outside("topic")
    w.package("topic", "topic")

    document = w.collect()

    assert relation_of(document, w.a, "topic")[0] == "differs", "control"
    assert_status(
        document, "topic", f"in progress {DOT} push state unknown", "in_progress", push="unknown"
    )


def test_a_branch_checked_out_in_another_checkout_names_it(status_world, tmp_path):
    w = status_world
    for name in ("here", "there", "linked", "both"):
        w.branch(w.a, name, push=True)
    w.take(w.b, "there")
    w.take(w.b, "both")
    git(w.b, "checkout", "-q", "there")
    first = linked(w.a, tmp_path, "linked", "linked")
    second = linked(w.a, tmp_path, "both-a", "both")
    third = linked(w.b, tmp_path, "both-b", "both")
    git(w.a, "checkout", "-q", "here")
    for name in ("here", "there", "linked", "both"):
        w.package(name, name)
    w.package("inside", "linked", target=first)

    document = w.collect()

    pushed = f"in progress {DOT} pushed"
    assert_status(document, "here", pushed, "in_progress", push="pushed")
    assert_status(
        document, "there", f"{pushed} {DOT} in Repo Clone B", "in_progress",
        push="pushed", elsewhere=(w.b,),
    )
    assert_status(
        document, "linked", f"{pushed} {DOT} in wt-linked", "in_progress",
        push="pushed", elsewhere=(first,),
    )
    # The checkout the package targets is never named, however it is reached.
    assert_status(document, "inside", pushed, "in_progress", push="pushed")
    assert_status(
        document, "both", f"{pushed} {DOT} in wt-both-a, wt-both-b", "in_progress",
        push="pushed", elsewhere=(second, third),
    )


def pull_world(w: StatusWorld, tmp_path: Path, monkeypatch, pulls: dict[str, list[dict[str, Any]]]):
    w.github()
    return install_fake_gh(
        tmp_path, monkeypatch, stdout=pr_answer(local_names(w.a, w.b), pulls)
    )


@posix_only
def test_an_open_pull_request_shows_its_number_and_only_unpushed_commits(
    status_world, tmp_path, monkeypatch
):
    w = status_world
    w.branch(w.a, "equal", push=True)
    w.branch(w.a, "one", push=True)
    w.branch(w.a, "split", push=True)
    w.branch(w.a, "behind", push=True)
    w.branch(w.a, "unsent")
    land(w.a, "landed")
    git(w.a, "checkout", "-q", "one")
    w.commit(w.a)
    git(w.a, "checkout", "-q", "trunk")
    w.push_from_outside("split")
    w.push_from_outside("behind")
    git(w.a, "fetch", "-q", "origin")
    git(w.a, "checkout", "-q", "split")
    w.commit(w.a, 2)
    git(w.a, "checkout", "-q", "trunk")
    pulls = {
        "equal": [
            pr_node(5, "closed", UNKNOWN_HEAD),
            pr_node(6, "open", UNKNOWN_HEAD),
            pr_node(7, "open", UNKNOWN_HEAD),
            pr_node(8, "closed", UNKNOWN_HEAD),
        ],
        "one": [pr_node(3, "open", UNKNOWN_HEAD)],
        "split": [pr_node(11, "open", UNKNOWN_HEAD)],
        "behind": [pr_node(13, "open", UNKNOWN_HEAD)],
        "unsent": [pr_node(12, "open", UNKNOWN_HEAD)],
        "landed": [pr_node(14, "open", UNKNOWN_HEAD)],
    }
    pull_world(w, tmp_path, monkeypatch, pulls)
    for name in pulls:
        w.package(name, name)

    document = w.collect(github_lookup=True)

    assert document["repositories"][0]["pr_lookup"]["status"] == "live", "control"
    assert relation_of(document, w.a, "split") == ("diverged", 2, 1), "control"
    assert relation_of(document, w.a, "behind") == ("behind", 0, 1), "control"
    assert_status(document, "equal", "PR #7 open", "pr_open", push="pushed", pull_request=7)
    assert_status(
        document, "one", f"PR #3 open {DOT} 1 unpushed", "pr_open",
        push="unpushed", unpushed=1, pull_request=3,
    )
    assert_status(
        document, "split", f"PR #11 open {DOT} 2 unpushed", "pr_open",
        push="unpushed", unpushed=2, pull_request=11,
    )
    assert_status(document, "behind", "PR #13 open", "pr_open", push="pushed", pull_request=13)
    assert_status(document, "unsent", "PR #12 open", "pr_open", push="not_pushed", pull_request=12)
    # Work that has already landed is merged, whatever pull request is still open.
    assert_status(
        document, "landed", f"merged {DOT} cleanup pending (1 branch)", "merged",
        cleanup={"worktrees": 0, "branches": 1},
    )


@posix_only
def test_a_recorded_pull_request_answers_when_no_network_is_used(
    status_world, tmp_path, monkeypatch
):
    w = status_world
    w.branch(w.a, "topic", push=True)
    fake = pull_world(w, tmp_path, monkeypatch, {"topic": [pr_node(9, "open", UNKNOWN_HEAD)]})
    w.package("topic", "topic")
    networked = w.collect(github_lookup=True)
    assert_status(networked, "topic", "PR #9 open", "pr_open", push="pushed", pull_request=9)
    calls = len(fake.calls())

    offline = w.collect(remote_checks=False, github_lookup=True)

    assert offline["remote_checks"] == "off"
    assert offline["repositories"][0]["pr_lookup"]["status"] == "recorded"
    assert len(fake.calls()) == calls, "control: no new gh call"
    assert_status(offline, "topic", "PR #9 open", "pr_open", push="pushed", pull_request=9)


def test_squash_merged_work_reads_cleanup_pending_until_every_copy_is_gone(
    status_world, tmp_path
):
    w = status_world
    land(w.a, "done")
    git(w.a, "push", "-q", "origin", "done")
    w.package("clone", "done")
    pending = f"merged {DOT} cleanup pending"

    kept = w.collect()

    assert_status(
        kept, "clone", f"{pending} (1 branch)", "merged", cleanup={"worktrees": 0, "branches": 1}
    )

    worktree = linked(w.a, tmp_path, "done", "done")
    w.package("inside", "done", target=worktree)
    held = w.collect()

    one_worktree = {"worktrees": 1, "branches": 0}
    assert_status(
        held, "clone", f"{pending} (1 worktree) {DOT} in wt-done", "merged",
        cleanup=one_worktree, elsewhere=(worktree,),
    )
    assert_status(held, "inside", f"{pending} (1 worktree)", "merged", cleanup=one_worktree)

    shutil.rmtree(worktree)
    stale = w.collect()

    assert "prunable" in git(w.a, "worktree", "list", "--porcelain").stdout, "control"
    assert_status(
        stale, "clone", f"{pending} (1 worktree) {DOT} in wt-done", "merged",
        cleanup=one_worktree, elsewhere=(worktree,),
    )

    git(w.a, "worktree", "prune")
    pruned = w.collect()

    assert_status(
        pruned, "clone", f"{pending} (1 branch)", "merged", cleanup={"worktrees": 0, "branches": 1}
    )

    git(w.a, "branch", "-q", "-D", "done")
    remote_only = w.collect()

    assert remote_of(remote_only, w.a, "done")["state"] == "present", "control"
    assert_status(remote_only, "clone", "merge state unknown", "unknown")

    git(w.a, "push", "-q", "origin", "--delete", "done")
    gone = w.collect()

    assert remote_of(gone, w.a, "done")["state"] == "absent", "control"
    assert status_of(gone, "Fictional/Drafts/clone") is None


def test_cleanup_counts_every_clone_and_worktree_and_uses_plurals(status_world, tmp_path):
    w = status_world
    land(w.a, "done")
    git(w.a, "push", "-q", "origin", "done")
    w.take(w.b, "done")
    w.package("clone", "done")
    pending = f"merged {DOT} cleanup pending"

    twice = w.collect()

    assert_status(
        twice, "clone", f"{pending} (2 branches)", "merged",
        cleanup={"worktrees": 0, "branches": 2},
    )

    first = linked(w.a, tmp_path, "done-a", "done")
    git(w.b, "checkout", "-q", "done")
    both = w.collect()

    assert_status(
        both, "clone", f"{pending} (2 worktrees) {DOT} in Repo Clone B, wt-done-a", "merged",
        cleanup={"worktrees": 2, "branches": 0}, elsewhere=(w.b, first),
    )


def test_work_squash_merged_in_one_clone_and_extended_in_another_is_in_progress(status_world):
    w = status_world
    land(w.a, "done")
    git(w.a, "push", "-q", "origin", "done")
    w.take(w.b, "done", commits=1)
    w.package("done", "done")

    document = w.collect()

    verdicts = {
        member: next(b for b in clone_at(document, member)["branches"] if b["name"] == "done")[
            "verdict"
        ]["state"]
        for member in (w.a, w.b)
    }
    assert verdicts == {w.a: "merged", w.b: "not_merged"}, "control"
    assert_status(
        document, "done", f"in progress {DOT} 1 unpushed", "in_progress",
        push="unpushed", unpushed=1,
    )


def test_a_branch_at_the_default_head_with_no_commit_reads_no_commits_yet(status_world):
    w = status_world
    git(w.a, "branch", "zero", "trunk")
    w.package("zero", "zero")

    document = w.collect()

    assert_status(document, "zero", f"in progress {DOT} no commits yet", "no_commits_yet")


def test_the_only_copy_in_a_shallow_clone_has_an_unknown_merge_state(status_world):
    w = status_world
    git(w.outside, "checkout", "-q", "-b", "topic", "trunk")
    w.commit(w.outside, 2)
    git(w.outside, "push", "-q", "origin", "topic")
    shallow = clone(w.remote.as_uri(), w.folder / "shallow", "--depth", "1", "--no-single-branch")
    git(shallow, "checkout", "-q", "-b", "topic", "origin/topic")
    git(shallow, "checkout", "-q", "trunk")
    w.package("topic", "topic", target=shallow)

    document = w.collect()

    member = clone_at(document, shallow)
    assert member["shallow"] is True and member["status"] == "ok", "control"
    (branch,) = [b for b in member["branches"] if b["name"] == "topic"]
    assert (branch["verdict"]["state"], branch["verdict"]["reason"]) == (
        "unknown",
        "incomplete_clone",
    ), "control"
    assert remote_of(document, shallow, "topic")["state"] == "present"
    assert_status(document, "topic", "merge state unknown", "unknown")


def test_a_remote_only_branch_and_a_clone_without_origin_read_merge_state_unknown(
    status_world,
):
    w = status_world
    solo = w.folder / "Repo Clone D"
    git(w.folder, "init", "-q", "-b", "trunk", str(solo))
    commit_file(solo, "file.txt", "one\n", "seed")
    git(solo, "checkout", "-q", "-b", "topic")
    w.commit(solo)
    git(solo, "checkout", "-q", "trunk")
    git(w.outside, "checkout", "-q", "-b", "elsewhere", "trunk")
    w.commit(w.outside)
    git(w.outside, "push", "-q", "origin", "elsewhere")
    w.package("local", "topic", target=solo)
    w.package("remote-only", "elsewhere")

    document = w.collect()

    assert repository_of(document, solo)["remote"] is None, "control"
    assert_status(document, "local", "merge state unknown", "unknown")
    assert remote_of(document, w.a, "elsewhere")["state"] == "present", "control"
    assert_status(document, "remote-only", "merge state unknown", "unknown")


def test_packages_without_a_found_declared_branch_have_no_code_status(matching):
    make_package(matching["root"], "Drafts", "invalid", f"Target repo: {matching['a']}\nBranch: spec/x (proposed)\n")
    make_package(matching["root"], "Drafts", "nowhere", f"Target repo: {matching['a']}\nBranch: absent-branch\n")

    document = collect_packages([matching["folder"]], matching["root"])

    assert status_of(document, path_of("target")) is not None, "control"
    for name in ("proposed", "inapplicable", "default", "missing", "invalid", "nowhere"):
        assert status_of(document, path_of(name)) is None, name
    by_name = {p["package_path"]: p["branch"]["status"] for p in document["packages"]}
    assert by_name[path_of("default")] == "default_branch"
    assert by_name[path_of("invalid")] == "invalid"


def test_a_sibling_with_too_many_branches_changes_no_code_status(status_world):
    w = status_world
    w.branch(w.a, "topic", push=True)
    w.package("topic", "topic")
    baseline = w.collect()

    sibling = clone(w.remote, w.folder / "sibling")
    w.take(sibling, "topic", commits=1)
    counted = w.collect()

    assert clone_at(counted, sibling)["status"] == "ok", "control"
    assert_status(
        counted, "topic", f"in progress {DOT} 1 unpushed", "in_progress", push="unpushed",
        unpushed=1,
    ), "control: the sibling's extra commit is seen while its branches are readable"

    import_branches(sibling, models.CHECKOUT_BRANCH_CAP + 1)
    capped = w.collect()

    assert clone_at(capped, sibling)["status"] == "too_many_branches"
    assert [p["code_status"] for p in capped["packages"]] == [
        p["code_status"] for p in baseline["packages"]
    ]
    assert {b["name"]: b["packages"] for b in clone_at(capped, w.a)["branches"]} == {
        b["name"]: b["packages"] for b in clone_at(baseline, w.a)["branches"]
    }
    assert repository_of(capped, w.a)["package_branches"] == repository_of(baseline, w.a)[
        "package_branches"
    ]


def test_a_remote_tip_no_local_copy_holds_has_no_verdict_even_beside_a_merged_copy(
    status_world,
):
    w = status_world
    land(w.a, "done")
    git(w.a, "push", "-q", "origin", "done")
    w.push_from_outside("done")
    w.package("done", "done")

    document = w.collect()

    assert relation_of(document, w.a, "done")[0] == "differs", "control"
    assert_status(document, "done", "merge state unknown", "unknown")


def test_without_any_remote_answer_the_push_state_is_unknown_and_not_not_pushed(status_world):
    w = status_world
    w.branch(w.a, "topic")
    w.package("topic", "topic")

    document = w.collect(remote_checks=False)

    assert remote_of(document, w.a, "topic")["state"] == "unknown", "control"
    assert relation_of_or_none(document, w.a, "topic") == "absent", "control"
    assert_status(
        document, "topic", f"in progress {DOT} push state unknown", "in_progress", push="unknown"
    )


def test_packages_sharing_a_branch_each_read_the_branch_facts(status_world):
    w = status_world
    w.branch(w.a, "topic", push=True)
    w.package("old", "topic", stage="Shelf")
    w.package("new", "topic")

    pushed = f"in progress {DOT} pushed"
    document = w.collect()

    assert_status(document, "old", pushed, "in_progress", stage="Shelf", push="pushed")
    assert_status(document, "new", pushed, "in_progress", push="pushed")
    assert entries_of(repository_of(document, w.a)) == [
        ("topic", ["Fictional/Drafts/new", "Fictional/Shelf/old"])
    ]

    (w.root / "Fictional" / "Shipped").mkdir()
    shutil.move(w.root / "Fictional" / "Drafts" / "new", w.root / "Fictional" / "Shipped" / "new")
    finished = w.collect()

    awaiting = "awaiting merge (no PR)"
    assert_status(finished, "old", awaiting, "awaiting_merge", stage="Shelf")
    assert_status(finished, "new", awaiting, "awaiting_merge", stage="Shipped")
    assert entries_of(repository_of(finished, w.a)) == [
        ("topic", ["Fictional/Shelf/old", "Fictional/Shipped/new"])
    ]

    shutil.move(w.root / "Fictional" / "Shipped" / "new", w.root / "Fictional" / "Drafts" / "new")
    w.package("old", "topic", stage="Shelf", extra=f"Superseded By: {ID_OLD}\n")
    retired = w.collect()

    assert_status(retired, "old", f"superseded {DOT} branch retained", "superseded", stage="Shelf")
    assert_status(retired, "new", pushed, "in_progress", push="pushed")


def test_work_pushed_since_a_networked_check_is_pushed_for_every_package(status_world):
    w = status_world
    w.branch(w.a, "topic", push=True)
    w.push_from_outside("topic")
    git(w.b, "fetch", "-q", "origin")
    w.push_from_outside("topic")
    w.package("topic", "topic")
    w.package("topic-2", "topic-2")
    recorded = w.collect()
    assert remote_of(recorded, w.a, "topic")["basis"] == "live", "control"

    git(w.a, "fetch", "-q", "origin")
    git(w.a, "checkout", "-q", "topic")
    git(w.a, "merge", "-q", "--ff-only", "origin/topic")
    w.commit(w.a)
    git(w.a, "push", "-q", "origin", "topic")
    git(w.a, "checkout", "-q", "-b", "topic-2", "trunk")
    w.commit(w.a)
    git(w.a, "push", "-q", "origin", "topic-2")
    git(w.a, "checkout", "-q", "trunk")

    document = w.collect(remote_checks=False)

    assert document["remote_checks"] == "off"
    older = rev(w.b, "refs/remotes/origin/topic")
    assert older != rev(w.a, "refs/remotes/origin/topic"), "control: B holds an older tip"
    for name in ("topic", "topic-2"):
        copy = remote_of(document, w.a, name)
        assert_remote(copy, "present", rev(w.a, f"refs/remotes/origin/{name}"), "cached")
        assert_status(
            document, name, f"in progress {DOT} pushed", "in_progress", push="pushed"
        )


def build_status_document(w: StatusWorld, tmp_path: Path) -> dict[str, Any]:
    w.branch(w.a, "ahead", push=True)
    git(w.a, "checkout", "-q", "ahead")
    w.commit(w.a)
    git(w.a, "checkout", "-q", "trunk")
    git(w.a, "branch", "zero", "trunk")
    land(w.a, "landed")
    w.branch(w.a, "both", push=True)
    w.take(w.b, "both")
    linked(w.a, tmp_path, "both-a", "both")
    linked(w.b, tmp_path, "both-b", "both")
    for name in ("ahead", "zero", "landed", "both"):
        w.package(name, name)
    w.package("default", "trunk")
    w.package("missing", "ahead", target=tmp_path / "nowhere")
    return w.collect()


@pytest.fixture(scope="module")
def shared_status(tmp_path_factory):
    with shared_world_build(tmp_path_factory, "status") as (directory, home, _patch):
        world = StatusWorld(directory)
        return world, build_status_document(world, directory), home


@pytest.fixture
def status_document(shared_status):
    """A private copy of the networked collection of the status world."""
    return doc_copy(shared_status[1])


def code_status_of(document: dict[str, Any], name: str) -> dict[str, Any]:
    (package,) = [p for p in document["packages"] if p["package_path"] == f"Fictional/Drafts/{name}"]
    return package["code_status"]


def package_of(document: dict[str, Any], name: str) -> dict[str, Any]:
    (package,) = [p for p in document["packages"] if p["package_path"] == f"Fictional/Drafts/{name}"]
    return package


def test_the_collected_code_statuses_are_accepted_and_come_back_unchanged(status_document):
    parsed = models.parse_checkouts(json.dumps(status_document))

    assert parsed["packages"] == status_document["packages"]
    assert code_status_of(status_document, "ahead")["unpushed"] == 1
    assert code_status_of(status_document, "landed")["cleanup"] == {"worktrees": 0, "branches": 1}
    assert code_status_of(status_document, "zero")["state"] == "no_commits_yet"
    assert len(code_status_of(status_document, "both")["checked_out_elsewhere"]) == 2
    assert code_status_of(status_document, "default") is None
    assert code_status_of(status_document, "missing") is None


def set_status(name: str, **changes: Any):
    def change(document: dict[str, Any]) -> None:
        code_status_of(document, name).update(changes)

    return change


def set_status_retexted(name: str, **changes: Any):
    """Change fields and give the status the text they now produce, so the text agrees."""

    def change(document: dict[str, Any]) -> None:
        status = code_status_of(document, name)
        status.update(changes)
        retexted(status)

    return change


def drop_status_key(name: str, key: str):
    def change(document: dict[str, Any]) -> None:
        del code_status_of(document, name)[key]

    return change


def give_status(name: str):
    def change(document: dict[str, Any]) -> None:
        package_of(document, name)["code_status"] = doc_copy(code_status_of(document, "ahead"))

    return change


def reverse_elsewhere(document: dict[str, Any]) -> None:
    code_status_of(document, "both")["checked_out_elsewhere"].reverse()


CODE_STATUS_REFUSALS = {
    "a text its fields do not give": (set_status("ahead", text="in progress"), "does not match"),
    "a text for another unpushed count": (
        set_status("ahead", unpushed=2),
        "does not match",
    ),
    "an unknown state": (set_status("ahead", state="done"), "state is invalid"),
    "an unknown push state": (set_status("ahead", push="sent"), "push is invalid"),
    "a push state on a merged status": (
        set_status("landed", push="pushed"),
        "push is set exactly for",
    ),
    "no push state on an in-progress status": (
        set_status_retexted("ahead", push=None, unpushed=None),
        "push is set exactly for",
    ),
    "a pull request on an in-progress status": (
        set_status("ahead", pull_request=4),
        "pull_request is set exactly",
    ),
    "a pull request that is not a positive integer": (
        set_status("ahead", pull_request=0),
        "positive integer",
    ),
    "a boolean unpushed count": (set_status("ahead", unpushed=True), "positive integer"),
    "an unpushed count without the unpushed push state": (
        set_status_retexted("ahead", push="pushed"),
        "unpushed is a count exactly",
    ),
    "a missing unpushed count": (
        set_status_retexted("ahead", unpushed=None),
        "unpushed is a count exactly",
    ),
    "cleanup on an in-progress status": (
        set_status("ahead", cleanup={"worktrees": 0, "branches": 1}),
        "cleanup is set exactly",
    ),
    "no cleanup on a merged status": (
        set_status_retexted("landed", cleanup=None),
        "cleanup is set exactly",
    ),
    "a negative cleanup count": (
        set_status("landed", cleanup={"worktrees": -1, "branches": 1}),
        "non-negative integer",
    ),
    "cleanup that is not an object": (set_status("landed", cleanup=[1]), "must be an object"),
    "a cleanup key missing": (
        set_status("landed", cleanup={"worktrees": 0}),
        "invalid shape",
    ),
    "other worktrees out of order": (reverse_elsewhere, "canonical order"),
    "other worktrees that are not a list": (
        set_status("both", checked_out_elsewhere="x"),
        "must be a list",
    ),
    "an other worktree that is not text": (
        set_status("both", checked_out_elsewhere=[3]),
        "safe text",
    ),
    "a text that is not a string": (set_status("ahead", text=7), "string"),
    "a missing key": (drop_status_key("ahead", "pull_request"), "invalid shape"),
    "an extra key": (set_status("ahead", extra=1), "invalid shape"),
    "a status that is not an object": (
        lambda d: package_of(d, "ahead").update(code_status="x"),
        "must be an object",
    ),
    "a status on a default-branch package": (give_status("default"), "requires a declared"),
    "a status on a package with a missing target": (give_status("missing"), "requires a declared"),
}


@pytest.mark.parametrize("case", list(CODE_STATUS_REFUSALS))
def test_the_parser_refuses_a_code_status_that_breaks_its_contract(status_document, case):
    change, message = CODE_STATUS_REFUSALS[case]
    broken = doc_copy(status_document)
    change(broken)

    assert message in refusal_of(broken)


def test_the_parser_accepts_a_status_for_every_state_the_text_function_renders(status_document):
    for state, changes in {
        "awaiting_merge": {"push": None, "unpushed": None, "text": "awaiting merge (no PR)"},
        "superseded": {
            "push": None,
            "unpushed": None,
            "text": f"superseded {DOT} branch retained",
        },
        "unknown": {"push": None, "unpushed": None, "text": "merge state unknown"},
        "pr_open": {
            "pull_request": 12,
            "push": "pushed",
            "unpushed": None,
            "text": "PR #12 open",
        },
    }.items():
        document = doc_copy(status_document)
        status = code_status_of(document, "ahead")
        status.update(state=state, **changes)
        status["text"] += f" {DOT} in " + ", ".join(
            models._path_name(path) for path in status["checked_out_elsewhere"]
        ) if status["checked_out_elsewhere"] else ""
        assert models.parse_checkouts(redigest(document))["packages"] == document["packages"], state


def test_one_copy_whose_comparison_is_undecided_leaves_the_push_state_unknown(status_world):
    # `pushed` needs every local copy `equal` or `behind`; one is not enough.
    w = status_world
    w.branch(w.a, "topic", push=True)
    shallow = clone(w.remote.as_uri(), w.folder / "shallow", "--depth", "1", "--no-single-branch")
    git(shallow, "checkout", "-q", "-b", "topic", "origin/topic")
    git(shallow, "checkout", "-q", "trunk")
    w.push_from_outside("topic")
    git(w.a, "fetch", "-q", "origin")
    git(w.a, "checkout", "-q", "topic")
    git(w.a, "merge", "-q", "--ff-only", "origin/topic")
    git(w.a, "checkout", "-q", "trunk")
    w.package("topic", "topic")

    document = w.collect()

    assert repository_of(document, shallow) is repository_of(document, w.a), "control"
    assert relation_of(document, w.a, "topic")[0] == "equal", "control"
    assert relation_of(document, shallow, "topic")[0] == "differs", "control"
    assert_status(
        document, "topic", f"in progress {DOT} push state unknown", "in_progress", push="unknown"
    )


@posix_only
def test_a_merged_pull_request_is_not_an_open_one(status_world, tmp_path, monkeypatch):
    w = status_world
    w.branch(w.a, "topic", push=True)
    pull_world(w, tmp_path, monkeypatch, {"topic": [pr_node(4, "merged", UNKNOWN_HEAD)]})
    w.package("topic", "topic")

    document = w.collect(github_lookup=True)

    (branch,) = [b for b in clone_at(document, w.a)["branches"] if b["name"] == "topic"]
    assert [(p["number"], p["state"]) for p in branch["pull_requests"]] == [(4, "merged")], (
        "control"
    )
    assert branch["verdict"]["state"] == "not_merged", "control"
    assert_status(document, "topic", f"in progress {DOT} pushed", "in_progress", push="pushed")


@posix_only
def test_an_unreadable_package_keeps_a_finished_package_from_awaiting_its_merge(status_world):
    w = status_world
    w.branch(w.a, "topic", push=True)
    w.package("old", "topic", stage="Shipped")
    w.package("new", "topic")
    unreadable = w.root / "Fictional" / "Drafts" / "new" / "spec.md"
    unreadable.chmod(0o000)
    if os.access(unreadable, os.R_OK):  # pragma: no cover - a user that ignores modes
        unreadable.chmod(0o644)
        pytest.skip("file permissions are not enforced for this user")
    try:
        document = w.collect()
    finally:
        unreadable.chmod(0o644)

    assert document["package_scan"]["status"] == "incomplete"
    assert [p["package_path"] for p in document["packages"]] == ["Fictional/Shipped/old"], (
        "control: the active package is not listed"
    )
    (old,) = document["packages"]
    assert old["finished"] is True, "control"
    assert old["code_status"]["state"] != "awaiting_merge"
    assert_status(
        document, "old", f"in progress {DOT} pushed", "in_progress", stage="Shipped", push="pushed"
    )

    complete = w.collect()

    assert complete["package_scan"]["status"] == "complete"
    assert_status(
        complete, "old", f"in progress {DOT} pushed", "in_progress", stage="Shipped", push="pushed"
    )
    (w.root / "Fictional" / "Drafts" / "new" / "spec.md").unlink()
    (w.root / "Fictional" / "Drafts" / "new").rmdir()
    alone = w.collect()
    assert alone["package_scan"]["status"] == "complete"
    assert_status(alone, "old", "awaiting merge (no PR)", "awaiting_merge", stage="Shipped")


def test_a_pushed_branch_reads_pushed_from_its_tracking_ref_without_a_remote_answer(
    status_world,
):
    # With no remote answer and no fetch, the clone's own tracking ref is the remote copy:
    # present at the tip it pushed, which its branch equals.
    w = status_world
    w.branch(w.a, "topic", push=True)
    w.package("topic", "topic")

    document = w.collect(remote_checks=False)

    tip = rev(w.a, "topic")
    assert rev(w.a, "refs/remotes/origin/topic") == tip, "control"
    assert_remote(remote_of(document, w.a, "topic"), "present", tip, "cached")
    assert relation_of(document, w.a, "topic")[0] == "equal", "control"
    assert_status(document, "topic", f"in progress {DOT} pushed", "in_progress", push="pushed")


# ---------------------------------------------------------------------------
# Parser invariants: one mutation of a collected document per refusal
# ---------------------------------------------------------------------------


def package_index(document: dict[str, Any], name: str) -> int:
    (found,) = [i for i, p in enumerate(document["packages"]) if p["package_path"] == path_of(name)]
    return found


def retexted(status: dict[str, Any]) -> None:
    """Give a changed Code Status the text its fields now produce, so only the changed
    invariant is violated."""
    status["text"] = models.code_status_text(status)


def repository_index(document: dict[str, Any], member: Path) -> int:
    return document["repositories"].index(repository_of(document, member))


@pytest.fixture
def collected(shared_matching):
    """A private copy of the default collection of the ``matching`` world."""
    return doc_copy(shared_matching[1])


@pytest.fixture
def collected_layout(shared_matching):
    return dict(shared_matching[0])


def test_a_code_status_on_a_not_applicable_branch_is_refused(collected):
    index = package_index(collected, "inapplicable")
    assert collected["packages"][index]["branch"] == {"status": "not_applicable", "name": None}
    assert collected["packages"][index]["code_status"] is None

    def give(document):
        document["packages"][index]["code_status"] = doc_copy(
            document["packages"][package_index(document, "target")]["code_status"]
        )

    assert (
        f"packages[{index}].code_status requires a declared branch and a resolved target"
        in reference_mutation(collected, give)
    )


def test_a_branch_name_on_a_branch_that_is_not_declared_is_refused(collected):
    index = package_index(collected, "inapplicable")

    def name(document):
        document["packages"][index]["branch"]["name"] = "topic"

    assert f"packages[{index}].branch.name is set exactly for declared" in reference_mutation(
        collected, name
    )


def test_a_clone_for_a_missing_target_is_refused(collected, collected_layout):
    index = package_index(collected, "missing")
    assert_target(collected["packages"][index]["target"], "missing")

    def clone_of_a_missing_target(document):
        document["packages"][index]["target"]["clone"] = real(collected_layout["a"])

    assert f"packages[{index}].target.clone must be null without a clone" in reference_mutation(
        collected, clone_of_a_missing_target
    )


def test_a_clone_target_without_its_clone_is_refused(collected):
    index = package_index(collected, "inapplicable")
    assert collected["packages"][index]["target"]["status"] == "clone"

    def no_clone(document):
        document["packages"][index]["target"]["clone"] = None

    assert f"packages[{index}].target.clone is required for clone" in reference_mutation(
        collected, no_clone
    )


def test_a_package_branch_entry_naming_an_unlisted_package_is_refused(collected, collected_layout):
    index = repository_index(collected, collected_layout["a"])

    def unlisted(document):
        document["repositories"][index]["package_branches"][0]["packages"] = [path_of("zz")]

    assert (
        f"repositories[{index}].package_branches[0].packages names a package the document "
        "does not list"
    ) in reference_mutation(collected, unlisted)


def test_two_package_branch_entries_with_one_name_are_refused(collected, collected_layout):
    index = repository_index(collected, collected_layout["a"])

    def twice(document):
        entries = document["repositories"][index]["package_branches"]
        entries.append(doc_copy(entries[0]))

    assert (
        f"repositories[{index}].package_branches must be unique and in canonical order"
        in reference_mutation(collected, twice)
    )


def pull_request_open(fields: dict[str, Any], **changes: Any):
    def change(document: dict[str, Any]) -> None:
        status = document["packages"][package_index(document, "target")]["code_status"]
        status.update(state="pr_open", **fields)
        status.update(changes)
        retexted(status)

    return change


def test_an_open_pull_request_status_without_its_number_is_refused(collected):
    index = package_index(collected, "target")
    status = collected["packages"][index]["code_status"]
    assert (status["state"], status["push"]) == ("in_progress", "unknown")

    message = reference_mutation(collected, pull_request_open({"pull_request": None}))

    assert f"packages[{index}].code_status.pull_request is set exactly for pr_open" in message


def test_an_open_pull_request_status_without_a_push_state_is_refused(collected):
    index = package_index(collected, "target")

    message = reference_mutation(collected, pull_request_open({"pull_request": 5}, push=None))

    assert f"packages[{index}].code_status.push is set exactly for in_progress and pr_open" in message


def test_an_unpushed_count_that_is_text_is_refused(collected):
    index = package_index(collected, "target")

    def text_count(document):
        status = document["packages"][index]["code_status"]
        status.update(push="unpushed", unpushed="3")

    assert (
        f"packages[{index}].code_status.unpushed must be a positive integer or null"
        in reference_mutation(collected, text_count)
    )


@pytest.fixture
def worktrees(targets, tmp_path):
    document = collect_packages(
        [targets["folder"]],
        package_root(tmp_path, {"clone": targets["main"], "linked": targets["linked"]}),
    )
    by_name = resolve(document)
    assert_target(by_name["clone"], "clone", real(targets["main"]))
    assert_target(by_name["linked"], "worktree", real(targets["main"]), real(targets["linked"]))
    return document, targets


def target_index(document: dict[str, Any], name: str) -> int:
    (found,) = [i for i, p in enumerate(document["packages"]) if p["package_path"].endswith(f"/{name}")]
    return found


def test_a_worktree_for_a_clone_target_is_refused(worktrees):
    document, layout = worktrees
    index = target_index(document, "clone")

    def with_worktree(doc):
        doc["packages"][index]["target"]["worktree"] = real(layout["linked"])

    assert f"packages[{index}].target.worktree must be null without a worktree" in (
        reference_mutation(document, with_worktree)
    )


def test_a_worktree_target_without_its_worktree_is_refused(worktrees):
    document, _layout = worktrees
    index = target_index(document, "linked")

    def without_worktree(doc):
        doc["packages"][index]["target"]["worktree"] = None

    assert f"packages[{index}].target.worktree is required for worktree" in (
        reference_mutation(document, without_worktree)
    )


def documents_with_and_without_git(tmp_path: Path, monkeypatch) -> tuple[dict, dict]:
    """The same folder and packages collected with git and, then, with no git on the path."""
    folder, root = unchecked_world(tmp_path)
    with_git = collect_packages([folder], root)
    assert with_git["repositories"] and with_git["git"]["status"] == "available"
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    without_git = collect_packages([folder], root)
    assert_unchecked(without_git, "missing")
    return with_git, without_git


def test_a_checked_target_in_a_document_without_git_is_refused(tmp_path, monkeypatch):
    _with_git, without_git = documents_with_and_without_git(tmp_path, monkeypatch)
    index = target_index(without_git, "exists")

    def checked_missing(doc):
        doc["packages"][index]["target"]["status"] = "missing"

    assert f"packages[{index}].target.status requires git to be available" in (
        reference_mutation(without_git, checked_missing)
    )


def test_a_repository_in_a_document_without_git_is_refused(tmp_path, monkeypatch):
    with_git, without_git = documents_with_and_without_git(tmp_path, monkeypatch)

    def with_repositories(doc):
        doc["repositories"] = doc_copy(with_git["repositories"])

    assert "repositories must be empty when git is unavailable" in (
        reference_mutation(without_git, with_repositories)
    )


@pytest.mark.parametrize(
    "cleanup", [{"worktrees": 0, "branches": 0}, {"worktrees": 1, "branches": 1}]
)
def test_a_merged_status_needs_exactly_one_positive_cleanup_count(status_document, cleanup):
    index = package_index(status_document, "landed")
    assert code_status_of(status_document, "landed")["cleanup"] == {"worktrees": 0, "branches": 1}

    message = reference_mutation(status_document, set_status_retexted("landed", cleanup=cleanup))

    assert (
        f"packages[{index}].code_status.cleanup must have exactly one positive count for merged"
        in message
    )


def give_no_commits(name: str):
    def change(document: dict[str, Any]) -> None:
        status = code_status_of(document, name)
        status.update(
            state="no_commits_yet",
            pull_request=None,
            push=None,
            unpushed=None,
            cleanup=None,
            checked_out_elsewhere=[],
        )
        status["text"] = f"in progress {DOT} no commits yet"

    return change


def test_schema_6_is_accepted_schema_5_refused_and_no_commits_yet_is_strict(status_document):
    """One collection serves every proof: each case mutates its own copy."""
    assert status_document["schema_version"] == models.CHECKOUTS_SCHEMA_VERSION == 6
    assert models.parse_checkouts(json.dumps(status_document))["schema_version"] == 6

    old = doc_copy(status_document)
    old["schema_version"] = 5
    assert refusal_of(old) == "unsupported schema version"

    document = doc_copy(status_document)
    give_no_commits("ahead")(document)
    parsed = models.parse_checkouts(json.dumps(redigest(document)))
    assert code_status_of(parsed, "ahead")["text"] == f"in progress {DOT} no commits yet"
    assert code_status_of(parsed, "ahead")["state"] == "no_commits_yet"

    index = package_index(status_document, "ahead")
    cases = {
        "push": (lambda s: s.update(push="pushed"), "push is set exactly for in_progress and pr_open"),
        "pull_request": (lambda s: s.update(pull_request=4), "pull_request is set exactly for pr_open"),
        "cleanup": (
            lambda s: s.update(cleanup={"worktrees": 0, "branches": 1}),
            "cleanup is set exactly for merged",
        ),
        "unpushed": (
            lambda s: s.update(unpushed=3),
            "unpushed is a count exactly when push is unpushed",
        ),
        "text": (lambda s: s.update(text="in progress"), "text does not match its fields"),
    }
    failures: list[str] = []
    for case, (change, message) in cases.items():

        def mutate(doc: dict[str, Any], change=change) -> None:
            give_no_commits("ahead")(doc)
            change(code_status_of(doc, "ahead"))

        try:
            refused = reference_mutation(status_document, mutate)
        except (Exception, pytest.fail.Exception) as error:  # an acceptance raises DID NOT RAISE
            failures.append(f"{case}: not refused ({type(error).__name__}: {error})")
            continue
        if f"packages[{index}].code_status.{message}" not in refused:
            failures.append(f"{case}: refused with a different message: {refused!r}")
    assert not failures, "no_commits_yet contradictions not refused as expected:\n" + "\n".join(
        failures
    )


# ---------------------------------------------------------------------------
# Branches with no commit of their own
# ---------------------------------------------------------------------------

NO_COMMITS = f"in progress {DOT} no commits yet"


def assert_no_commits(
    document: dict[str, Any], name: str, *, stage: str = "Drafts", suffix: str = "",
    elsewhere: tuple[Path, ...] = (),
) -> None:
    assert_status(
        document, name, NO_COMMITS + suffix, "no_commits_yet", stage=stage, elsewhere=elsewhere
    )


def test_a_branch_with_no_commit_of_its_own_reads_no_commits_yet_however_it_was_made(
    status_world, tmp_path
):
    w = status_world
    git(w.a, "branch", "plain")
    git(w.a, "branch", "shipped")
    git(w.a, "branch", "retired")
    # Documented limits: the reflog holds no creation entry, or more than one.
    git(w.a, "-c", "core.logAllRefUpdates=false", "branch", "unlogged")
    git(w.a, "branch", "before-rename")
    git(w.a, "branch", "-m", "before-rename", "renamed")
    held = tmp_path / "A-w3"
    git(w.a, "worktree", "add", "-q", "-b", "in-worktree", str(held), "trunk")
    # One commit, then back to the default branch and squash-merged: not fresh.
    git(w.a, "switch", "-q", "-c", "landed", "trunk")
    w.commit(w.a)
    git(w.a, "switch", "-q", "trunk")
    git(w.a, "merge", "-q", "--squash", "landed")
    git(w.a, "commit", "-q", "-m", "land landed")
    git(w.a, "push", "-q", "origin", "trunk")
    git(w.a, "switch", "-q", "-c", "switched", "trunk")
    for name in ("plain", "unlogged", "renamed", "in-worktree", "landed", "switched"):
        w.package(name, name)
    w.package("shipped", "shipped", stage="Shipped")
    w.package("retired", "retired", extra=f"Superseded By: {ID_OLD}\n")

    document = w.collect(remote_checks=False)

    assert_no_commits(document, "plain")
    assert_no_commits(document, "switched")  # the target's own worktree is not named
    assert_no_commits(document, "in-worktree", suffix=f" {DOT} in A-w3", elsewhere=(held,))
    assert_no_commits(document, "shipped", stage="Shipped")  # rule 0 precedes rule 3
    assert_no_commits(document, "retired")
    pending = f"merged {DOT} cleanup pending (1 branch)"
    one_branch = {"worktrees": 0, "branches": 1}
    for name in ("unlogged", "renamed", "landed"):
        assert_status(document, name, pending, "merged", cleanup=one_branch)


def test_a_branch_made_by_a_remote_branch_checkout_after_a_merge_commit_reads_merged(
    status_world,
):
    w = status_world
    git(w.a, "checkout", "-q", "-b", "b2", "trunk")
    w.commit(w.a)
    git(w.a, "push", "-q", "origin", "b2")
    git(w.a, "checkout", "-q", "trunk")
    git(w.a, "merge", "-q", "--no-ff", "-m", "merge b2", "b2")
    git(w.a, "push", "-q", "origin", "trunk")
    git(w.a, "branch", "-q", "-D", "b2")
    git(w.b, "fetch", "-q", "origin")
    git(w.b, "checkout", "-q", "b2")
    reflog = git(w.b, "reflog", "show", "--format=%gs", "refs/heads/b2").stdout.split("\n")
    assert [line for line in reflog if line] == [
        "branch: Created from refs/remotes/origin/b2"
    ], "control: one creation entry, as a fresh branch has"
    w.package("dwim", "b2", target=w.b)

    document = w.collect(remote_checks=False)

    assert_status(
        document, "dwim", f"merged {DOT} cleanup pending (1 worktree)", "merged",
        cleanup={"worktrees": 1, "branches": 0},
    )


def reflog_subjects(member: Path, name: str) -> list[str]:
    shown = git(member, "reflog", "show", "--format=%gs", f"refs/heads/{name}").stdout
    return [line for line in shown.split("\n") if line]


def test_a_branch_whose_only_reflog_entry_is_not_a_creation_reads_merged(status_world):
    w = status_world
    git(w.a, "checkout", "-q", "-b", "feat", "trunk")
    w.commit(w.a)
    git(w.a, "push", "-q", "origin", "feat")
    git(w.a, "checkout", "-q", "trunk")
    git(w.a, "merge", "-q", "--ff-only", "feat")
    git(w.a, "push", "-q", "origin", "trunk")
    git(w.a, "branch", "-q", "-D", "feat")
    git(w.b, "fetch", "-q", "origin")
    git(w.b, "fetch", "-q", "origin", "feat:feat")
    entries = reflog_subjects(w.b, "feat")
    assert len(entries) == 1 and not entries[0].startswith("branch: Created from"), (
        "control: one entry, and it is not a creation",
        entries,
    )
    assert git(w.b, "rev-list", "--count", "feat..origin/trunk").stdout.strip() == "0", (
        "control: the tip is the default branch head"
    )
    w.package("fetched", "feat", target=w.b)

    document = w.collect(remote_checks=False)

    assert_status(
        document, "fetched", f"merged {DOT} cleanup pending (1 branch)", "merged",
        cleanup={"worktrees": 0, "branches": 1},
    )


def test_a_branch_reads_no_commits_yet_only_within_the_first_parent_limit(
    status_world, monkeypatch
):
    limit = 3
    monkeypatch.setattr(checkouts, "FIRST_PARENT_LIMIT", limit)
    w = status_world
    w.commit(w.a, limit + 1)
    git(w.a, "push", "-q", "origin", "trunk")
    git(w.a, "branch", "inside", f"trunk~{limit - 1}")
    git(w.a, "branch", "outside", f"trunk~{limit}")
    for name, behind in (("inside", limit - 1), ("outside", limit)):
        counted = git(w.a, "rev-list", "--first-parent", "--count", f"{name}..trunk")
        assert counted.stdout.strip() == str(behind), (
            f"control: {name} is {behind} first-parent commits behind"
        )
        assert len(reflog_subjects(w.a, name)) == 1, "control: one creation entry"
        w.package(name, name)

    document = w.collect(remote_checks=False)

    assert_no_commits(document, "inside")
    assert_status(
        document, "outside", f"merged {DOT} cleanup pending (1 branch)", "merged",
        cleanup={"worktrees": 0, "branches": 1},
    )


def test_pushing_a_branch_with_no_commit_keeps_it_and_a_pushed_commit_makes_it_unknown(
    status_world,
):
    w = status_world
    for name in ("pushed", "moved", "split"):
        git(w.a, "branch", name)
        w.package(name, name)
    git(w.a, "push", "-q", "origin", "pushed", "moved")
    w.push_from_outside("moved")
    # B made a commit of its own on the same name; A's copy is still fresh.
    git(w.b, "checkout", "-q", "-b", "split", "trunk")
    w.commit(w.b)
    git(w.b, "checkout", "-q", "trunk")

    first = w.collect()

    assert remote_of(first, w.a, "pushed")["state"] == "present", "control"
    assert_no_commits(first, "pushed")
    assert_status(first, "moved", "merge state unknown", "unknown")
    assert_status(
        first, "split", f"in progress {DOT} not pushed", "in_progress", push="not_pushed"
    )

    git(w.outside, "checkout", "-q", "trunk")
    w.commit(w.outside)
    git(w.outside, "push", "-q", "origin", "trunk")

    second = w.collect()

    assert_no_commits(second, "pushed")
    assert_status(second, "moved", "merge state unknown", "unknown")


def branch_facts(document: dict[str, Any]) -> dict[tuple[str, str], tuple[Any, ...]]:
    return {
        (clone["path"], branch["name"]): (
            branch["verdict"], branch["contained_in"], branch["removable"]
        )
        for repository in document["repositories"]
        for clone in repository["clones"]
        for branch in clone["branches"]
    }


def test_the_no_commit_checks_change_no_repository_verdict_or_cache_entry(
    status_world, tmp_path, monkeypatch
):
    w = status_world
    home = Path(os.environ["HOME"])
    git(w.a, "branch", "plain")
    git(w.a, "branch", "before-rename")
    git(w.a, "branch", "-m", "before-rename", "renamed")
    w.branch(w.a, "work", push=True)
    land(w.a, "done")
    w.take(w.b, "work", commits=1)
    for name in ("plain", "renamed", "work", "done"):
        w.package(name, name)
    before = {member: tree_fingerprint(member) for member in (w.a, w.b)}
    with_packages_cache = nyx_home(home)

    with_packages = w.collect(remote_checks=False)

    assert code_status_of(with_packages, "plain")["state"] == "no_commits_yet", "control"
    assert {member: tree_fingerprint(member) for member in (w.a, w.b)} == before
    fresh_cache = nyx_home(use_fresh_home(tmp_path, monkeypatch, home))
    without_packages = collect([w.folder])

    assert branch_facts(with_packages) == branch_facts(without_packages)
    assert cache_entries(with_packages_cache) == cache_entries(fresh_cache)
    assert cache_entries(with_packages_cache), "control: the cache holds verdicts"


class DelayWorld:
    """Clone A holds ``fresh`` made with ``git branch``; clone B, the second of the group
    in natural order, holds the unmerged ``busy``."""

    def __init__(self, w: StatusWorld) -> None:
        self.w = w
        git(w.a, "branch", "fresh")
        w.branch(w.b, "busy")
        w.package("fresh", "fresh")

    def collect(self, **options: Any) -> dict[str, Any]:
        options.setdefault("remote_checks", False)
        options.setdefault("max_parallel", 1)
        return self.w.collect(**options)

    def baseline(self) -> dict[tuple[str, str], tuple[Any, ...]]:
        document = self.collect()
        assert_no_commits(document, "fresh")
        facts = branch_facts(document)
        assert facts[(real(self.w.a), "fresh")][0]["rung"] == "ancestor", "control"
        assert facts[(real(self.w.b), "busy")][0]["state"] == "not_merged", "control"
        return facts


@posix_only
def test_a_no_commit_check_the_deadline_cuts_leaves_the_package_unknown_and_all_verdicts(
    status_world, tmp_path, monkeypatch
):
    world = DelayWorld(status_world)
    undelayed = world.baseline()
    slow = stand_in(
        tmp_path, "slow-reflog", 'case "$*" in *"reflog show"*"refs/heads/"*) exec sleep 60;; esac'
    )
    # No saved verdict may stand in for one the deadline would cut.
    cache = nyx_home(use_fresh_home(tmp_path, monkeypatch, Path(os.environ["HOME"])))

    with path_with(monkeypatch, slow):
        document = world.collect(seconds=3.0)

    assert_status(document, "fresh", "merge state unknown", "unknown")
    assert branch_facts(document) == undelayed
    assert "checkout_cache_write_failed" not in [item["code"] for item in document["diagnostics"]]
    assert cache_entries(cache), "the cut left the cache writable and holding verdicts"
    assert_no_commits(world.collect(), "fresh")


@posix_only
@pytest.mark.parametrize(
    "body, options",
    [
        ('case "$*" in *"reflog show"*"refs/heads/fresh"*) exit 128;; esac', {}),
        (
            'case "$*" in *"rev-list --first-parent"*) exec sleep 60;; esac',
            {"call_cap_seconds": 1.0},
        ),
    ],
    ids=["reflog read fails", "first-parent listing cut"],
)
def test_a_no_commit_check_that_fails_or_is_cut_leaves_the_package_unknown(
    status_world, tmp_path, monkeypatch, body, options
):
    world = DelayWorld(status_world)
    undelayed = world.baseline()

    with path_with(monkeypatch, stand_in(tmp_path, "broken", body)):
        document = world.collect(**options)

    assert_status(document, "fresh", "merge state unknown", "unknown")
    assert branch_facts(document) == undelayed


def test_signature_output_does_not_hide_a_branch_with_no_commit(status_world, tmp_path):
    w = status_world
    keygen = shutil.which("ssh-keygen")
    if keygen is None:
        pytest.skip("needs ssh-keygen to make an SSH-signed commit offline")
    key = tmp_path / "signing-key"
    made = subprocess.run(
        [keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        capture_output=True, check=False,
    )
    signed = git(
        w.a, "-c", "gpg.format=ssh", "-c", f"user.signingkey={key}",
        "commit", "-q", "--allow-empty", "-S", "-m", "signed", check=False,
    ) if made.returncode == 0 else None
    if signed is None or signed.returncode != 0:
        pytest.skip("this host's git or ssh-keygen cannot make an SSH-signed commit")
    git(w.a, "push", "-q", "origin", "trunk")
    git(w.a, "branch", "fresh")
    git(w.a, "config", "log.showSignature", "true")
    shown = git(w.a, "reflog", "show", "-n", "2", "--format=%H%x00%gs", "refs/heads/fresh")
    assert any(
        line and "\x00" not in line for line in shown.stdout.split("\n")
    ), "control: the setting puts signature lines into the reflog output"
    w.package("fresh", "fresh")

    document = w.collect(remote_checks=False)

    assert_no_commits(document, "fresh")


@posix_only
def test_a_branch_that_moves_during_collection_is_unknown(status_world, tmp_path, monkeypatch):
    w = status_world
    held = tmp_path / "A-wb"
    git(w.a, "worktree", "add", "-q", "-b", "wb", str(held), "trunk")
    w.package("wb", "wb")
    first = w.collect(remote_checks=False)
    assert_no_commits(first, "wb", suffix=f" {DOT} in A-wb", elsewhere=(held,))
    marker = tmp_path / "moved-once"
    mover = stand_in(
        tmp_path,
        "mover",
        f'case "$*" in *"reflog show"*"refs/heads/wb"*) if [ ! -e "{marker}" ]; then '
        f': > "{marker}"; "{shutil.which("git")}" -C "{held}" commit -q --allow-empty -m moved; '
        "fi;; esac",
    )

    with path_with(monkeypatch, mover):
        document = w.collect(remote_checks=False)

    assert marker.exists(), "control: the wrapper moved the branch"
    assert_status(
        document, "wb", f"merge state unknown {DOT} in A-wb", "unknown", elsewhere=(held,)
    )


def test_a_branch_with_no_commit_reads_no_commits_yet_against_a_base_borrowed_from_a_sibling(
    status_world,
):
    w = status_world
    git(w.a, "branch", "fresh")
    w.package("fresh", "fresh")
    # The default branch moves on the remote and only clone B fetches it, so clone A
    # judges against a base whose commits it reads from B.
    git(w.outside, "checkout", "-q", "trunk")
    newer = w.commit(w.outside)
    git(w.outside, "push", "-q", "origin", "trunk")
    git(w.b, "fetch", "-q", "origin")

    document = w.collect()

    base = clone_at(document, w.a)["verdict_base"]
    assert (base["sha"], base["objects_from"]) == (newer, real(w.b)), "control"
    assert_no_commits(document, "fresh")


def test_the_shared_matching_and_status_worlds_were_built_under_their_own_isolated_home(
    shared_matching, shared_status
):
    layout, _document = shared_matching
    world, _document, home = shared_status
    assert last_author(layout["a"]) == "Test <test@example.com>"
    assert last_author(world.a) == "Test <test@example.com>"
    assert (home / ".nyx" / "cache" / "checkouts.json").is_file()


def test_a_collected_handout_is_a_private_copy(collected, shared_matching):
    _layout, document = shared_matching
    collected["repositories"].clear()
    collected["packages"].clear()
    assert document["repositories"]
    assert document["packages"]


def test_a_status_document_handout_is_a_private_copy(status_document, shared_status):
    _world, document, _home = shared_status
    status_document["repositories"].clear()
    status_document["packages"].clear()
    assert document["repositories"]
    assert document["packages"]
