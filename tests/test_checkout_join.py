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
    LAST_GIT_SECOND,
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
    rev,
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
    if options.get("remote_checks"):
        # A networked check is taken in a whole second after the last git command of the
        # test, so a tracking ref that moved earlier is older than the check.
        time.sleep(max(0.0, LAST_GIT_SECOND[0] + 1 - time.time()))
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


def test_with_no_record_the_clone_fetched_most_recently_answers(
    tmp_path, remote_world, monkeypatch
):
    world = remote_world
    git(world.a, "fetch", "-q", "origin")
    time.sleep(1.1)
    newest = world.advance_remote("newest.txt")
    git(world.b, "fetch", "-q", "origin")
    assert world.tracking(world.a) != newest == world.tracking(world.b)
    use_fresh_home(tmp_path, monkeypatch, world.home)

    offline = world.collect()

    assert offline["remote_checks"] == "off"
    check = repository_of(offline, world.a)["remote"]["check"]
    assert (check["status"], check["reason"]) == ("off", "remote_checks_off")
    fetched = [
        clone_at(offline, member)["cached_default"]["last_fetched_at"]
        for member in (world.a, world.b)
    ]
    assert fetched[0] < fetched[1], "control: B fetched after A"
    assert_remote(remote_of(offline, world.a), "present", newest, "cached")

    # A failed networked check with nothing saved answers the same way.
    elsewhere = world.remote.rename(tmp_path / "moved.git")
    try:
        failed = world.collect(remote_checks=True)
    finally:
        elsewhere.rename(world.remote)
    assert repository_of(failed, world.a)["remote"]["check"]["status"] == "failed"
    assert_remote(remote_of(failed, world.a), "present", newest, "cached")


def test_with_no_record_and_no_clone_ever_fetched_the_remote_copy_is_unknown(
    tmp_path, monkeypatch
):
    world = RemoteWorld(tmp_path, Path(os.environ["HOME"]), fetched=False)
    use_fresh_home(tmp_path, monkeypatch, world.home)

    document = world.collect()

    for member in (world.a, world.b):
        assert clone_at(document, member)["cached_default"]["last_fetched_at"] is None
    assert_remote(remote_of(document, world.a), "unknown")


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


def test_a_package_branch_in_a_clone_without_origin_has_no_remote_copy(matching):
    document = collect_packages([matching["folder"]], matching["root"], remote_checks=False)

    repository = repository_of(document, matching["d"])
    assert repository["remote"] is None
    assert [(e["name"], e["packages"], e["remote"]) for e in repository["package_branches"]] == [
        ("topic", [path_of("on_d")], None)
    ]
    # The group of A and B has a remote and so a remote copy.
    assert remote_of(document, matching["a"])["state"] == "unknown"


def test_the_parser_refuses_a_remote_copy_its_remote_check_cannot_have_given(remote_world):
    world = remote_world
    live = world.collect(remote_checks=True)
    recorded = world.collect()

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
        (recorded, unknown_under_a_live_check, "cannot be unknown under a recorded remote check"),
        (recorded, live_under_a_recorded_check, "basis does not match the remote check"),
        (live, missing_remote, "must be an object"),
        (live, unknown_state, "state is invalid"),
        (live, extra_key, "remote has an invalid shape"),
    ):
        assert message in reference_mutation(document, change), change.__name__


def test_the_parser_refuses_a_remote_copy_on_a_repository_without_a_remote(matching):
    document = collect_packages([matching["folder"]], matching["root"])

    def copy_without_origin(doc):
        (entry,) = repository_of(doc, matching["d"])["package_branches"]
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


def test_fetches_in_the_same_second_go_to_the_first_clone_in_natural_order(
    tmp_path, remote_world, monkeypatch
):
    world = remote_world
    git(world.a, "fetch", "-q", "origin")
    held_by_a = world.tracking(world.a)
    newest = world.advance_remote("newest.txt")
    git(world.b, "fetch", "-q", "origin")
    # The record this check saves in the old account home is not seen from the fresh one.
    recorded_elsewhere = world.collect(remote_checks=True)
    assert_remote(remote_of(recorded_elsewhere, world.a), "present", newest, "live")
    same = time.time()
    for member in (world.a, world.b):
        os.utime(member / ".git" / "FETCH_HEAD", (same, same))
    use_fresh_home(tmp_path, monkeypatch, world.home)
    assert held_by_a != newest == world.tracking(world.b)

    document = world.collect()

    fetched = {
        clone_at(document, member)["cached_default"]["last_fetched_at"]
        for member in (world.a, world.b)
    }
    assert len(fetched) == 1, "control: the fetches tie"
    assert_remote(remote_of(document, world.a), "present", held_by_a, "cached")


def test_with_no_record_a_branch_the_newest_fetch_pruned_is_absent(
    tmp_path, remote_world, monkeypatch
):
    world = remote_world
    git(world.a, "fetch", "-q", "origin")
    time.sleep(1.1)
    git(world.remote, "branch", "-q", "-D", "topic")
    git(world.b, "fetch", "-q", "--prune", "origin")
    use_fresh_home(tmp_path, monkeypatch, world.home)
    assert world.tracking(world.a)
    pruned = git(world.b, "rev-parse", "-q", "--verify", "refs/remotes/origin/topic", check=False)
    assert pruned.returncode != 0, "control: B's newest fetch pruned the ref"

    document = world.collect()

    a_fetched, b_fetched = (
        clone_at(document, member)["cached_default"]["last_fetched_at"]
        for member in (world.a, world.b)
    )
    assert a_fetched < b_fetched
    assert_remote(remote_of(document, world.a), "absent")


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
