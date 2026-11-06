"""The package list of the checkouts document: collector inputs and scan states."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from test_checkouts import (  # noqa: F401  (fixtures that isolate git and GitHub state)
    DELAYED_PATCH_WORK,
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
    object_files,
    outer_github_environment,
    path_with,
    point_origin_at,
    posix_only,
    pr_answer,
    pr_node,
    real,
    rederive,
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
        repository["free_clones"] = []
        for member in repository["clones"]:
            member["availability"] = {"state": "unknown", "reasons": [], "assigned_to": []}
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
    assert_target(tabbed["target"], "missing", within_folders=False)
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
    target: dict[str, Any],
    status: str,
    clone: str | None = None,
    worktree: str | None = None,
    within_folders: bool | None = None,
) -> None:
    """Compare a target field by field, through the parser's own key set."""
    assert set(target) == models._PACKAGE_TARGET_KEYS
    assert target["status"] == status
    assert target["clone"] == clone
    assert target["worktree"] == worktree
    assert target["within_folders"] is within_folders, target


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
        "within_folders": None,
    }
    assert resolved("usable", ("id", 1, 20), index, fail) == {
        "status": "worktree", "clone": "/fictional/clone", "worktree": "/fictional/work",
        "within_folders": None,
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
    package["target"] = {
        "status": "worktree",
        "clone": busy,
        "worktree": real(targets["busy_work"]),
        "within_folders": None,
    }
    document["checkouts_digest"] = models.canonical_digest(
        document, omit=models.CHECKOUTS_DIGEST_OMITTED
    )

    with pytest.raises(models.ProtocolError, match="not listed"):
        models.parse_checkouts(document)

    package["target"] = {"status": "clone", "clone": busy, "worktree": None, "within_folders": None}
    unreferenced["target"] = {
        "status": "clone",
        "clone": "/fictional/nowhere",
        "worktree": None,
        "within_folders": None,
    }
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
    assert_target(found["fake"], "not_git", within_folders=True)
    assert_target(found["redirected"], "not_git", within_folders=True)


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
    # A missing path in a sibling directory whose name starts with the folder's name.
    sibling = tmp_path / f"{folder.name}-other"
    sibling.mkdir()
    wanted["sibling"] = sibling / "x"
    # A missing path named through a directory link to the folder, when the host makes one.
    link = tmp_path / "folder-link"
    try:
        link.symlink_to(folder, target_is_directory=True)
        wanted["linked_missing"] = link / "No Such Folder"
    except (OSError, NotImplementedError):
        pass

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
    unmatched: dict[str, tuple[str, bool | None]] = {
        "inside": ("not_git", True),
        "missing": ("missing", True),
        "plain": ("not_git", True),
        "outside": ("outside_folders", None),
        "gone": ("missing", False),
        "sibling": ("missing", False),
    }
    if "linked_missing" in wanted:
        unmatched["linked_missing"] = ("missing", True)
    for name, (status, within) in unmatched.items():
        assert_target(found[name], status, within_folders=within)
    for name in named:
        if name in found:
            assert found[name]["within_folders"] is None, name

    def refusal(mutate) -> str:
        changed = doc_copy(document)
        mutate(changed["packages"][package_index(changed, "missing")]["target"], changed)
        return refusal_of(changed)

    clone_index = package_index(document, "clone")

    def value_on_clone(target, changed) -> None:
        changed["packages"][clone_index]["target"]["within_folders"] = True

    for name, mutate, message in (
        ("null", lambda t, d: t.update(within_folders=None), "within_folders"),
        ("clone value", value_on_clone, "within_folders"),
        ("non-boolean", lambda t, d: t.update(within_folders="yes"), "within_folders"),
        ("missing key", lambda t, d: t.pop("within_folders"), "target"),
    ):
        assert message in refusal(mutate), name
    bare_availability = availability_of(document, targets["bare"])
    assert (bare_availability["state"], bare_availability["reasons"]) == (
        "occupied", ["linked_worktrees"]
    )
    assert bare_availability["assigned_to"] == [
        {"package_path": path_of("bare"), "basis": "target_repo"},
        {"package_path": path_of("barework"), "basis": "target_repo_worktree"},
    ]
    # git cannot read the holder's bare directory as a work tree, so the clone is not ok
    assert clone_at(document, targets["holder"])["status"] == "failed"
    holder_availability = availability_of(document, targets["holder"])
    assert (holder_availability["state"], holder_availability["reasons"]) == ("unknown", [])
    lines = cli.format_checkouts(document)
    busy = found["busy"]["clone"]
    assert f"{json.dumps(busy)}  unknown  too_many_branches" in lines
    assert any(line.startswith(json.dumps(found["bare"]["clone"]) + "  ") for line in lines)


def test_a_path_that_cannot_name_anything_is_missing(tmp_path):
    long_name = tmp_path / ("x" * 300)
    folder = tmp_path / "folder"
    folder.mkdir()
    root = package_root(tmp_path, {"long": long_name})

    document = collect_packages([folder], root)

    assert_target(resolve(document)["long"], "missing", within_folders=False)
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
    assert_target(found["plain"], "not_git", within_folders=True)
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


def remote_verdict_of(
    document: dict[str, Any], member: Path, name: str = "topic"
) -> dict[str, Any] | None:
    """The remote verdict of one package branch entry, with its shape checked."""
    (entry,) = [e for e in repository_of(document, member)["package_branches"] if e["name"] == name]
    assert set(entry) == models._PACKAGE_BRANCH_ENTRY_KEYS
    verdict = entry["remote_verdict"]
    if verdict is not None:
        assert set(verdict) == models._VERDICT_KEYS
    return verdict


def remote_unknown(reason: str) -> dict[str, Any]:
    return {"state": "unknown", "rung": None, "reason": reason, "pull_request": None}


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


def branch_pr_facts(document: dict[str, Any]) -> dict[tuple[str, str], tuple[Any, ...]]:
    """Every local branch's pull requests and verdict facts, by clone path and name."""
    return {
        (clone["path"], branch["name"]): (
            branch["pull_requests"],
            branch["verdict"],
            branch["contained_in"],
            branch["removable"],
        )
        for repository in document["repositories"]
        for clone in repository["clones"]
        for branch in clone["branches"]
    }


def requested_names(body: bytes) -> list[str]:
    """The branch names one recorded ``gh`` request asked about, in alias order."""
    variables = json.loads(body)["variables"]
    aliases = len(variables) - 2
    assert set(variables) == {"owner", "name"} | {f"h{index}" for index in range(aliases)}
    return [variables[f"h{index}"] for index in range(aliases)]


def one_gh_request(fake, before: set[Path]) -> bytes:
    """The body of the one ``gh`` request recorded after ``before``."""
    fresh = set(fake.state.glob("call.*")) - before
    assert len(fresh) == 1, f"one gh call per repository, not {len(fresh)}"
    [directory] = fresh
    return (directory / "body").read_bytes()


@posix_only
def test_the_pull_request_query_names_a_package_branch_no_clone_holds(
    status_world, tmp_path, monkeypatch
):
    w = status_world
    w.github()
    w.branch(w.a, "topic", push=True)
    topic_tip = rev(w.a, "topic")
    git(w.a, "branch", "plain", "trunk")
    land(w.a, "landed")
    w.branch(w.a, "topic-r", push=True)
    remote_tip = rev(w.a, "topic-r")
    git(w.a, "branch", "-q", "-D", "topic-r")
    w.package("remote-only", "topic-r")
    local = local_names(w.a, w.b)
    assert "topic-r" not in local, "control: no clone holds a local copy of the package branch"
    union = sorted(set(local) | {"topic-r"})
    pulled = {
        "plain": [pr_node(3, "open", UNKNOWN_HEAD)],
        "topic": [pr_node(7, "open", topic_tip)],
        "topic-r": [pr_node(4, "open", remote_tip)],
    }
    fake = install_fake_gh(tmp_path, monkeypatch, stderr="lookup failed\n", code=1)

    # A failed call loses the whole group's listing, package name or not.
    failed = w.collect(github_lookup=True)
    key = failed["repositories"][0]["remote"]["key"]
    assert key.startswith("github.com/"), "control: one GitHub group"
    assert failed["repositories"][0]["pr_lookup"] == {
        "status": "unavailable",
        "reason": "failed",
        "checked_at": None,
    }
    assert all(
        branch["pull_requests"] == []
        for clone in failed["repositories"][0]["clones"]
        for branch in clone["branches"]
    ), "the failed call leaves no pull requests"
    saved = json.loads(nyx_home(Path(os.environ["HOME"])).read_text())
    assert saved["remote_checks"][key]["pull_requests"] is None

    cache = nyx_home(use_fresh_home(tmp_path, monkeypatch, Path(os.environ["HOME"])))
    failed_alone = collect([w.folder], remote_checks=True, github_lookup=True)
    assert failed_alone["repositories"][0]["pr_lookup"] == failed["repositories"][0]["pr_lookup"]
    assert branch_pr_facts(failed) == branch_pr_facts(failed_alone)

    # With an answer, the one call asks about the sorted union and keeps each name's
    # listing; every local fact equals the same collection without a package scan.
    fake.replay(stdout=pr_answer(union, pulled))
    before = set(fake.state.glob("call.*"))
    widened = w.collect(github_lookup=True)
    assert requested_names(one_gh_request(fake, before)) == union
    assert widened["repositories"][0]["pr_lookup"] == {
        "status": "live",
        "reason": None,
        "checked_at": None,
    }
    stored = json.loads(cache.read_text())["remote_checks"][key]["pull_requests"]
    assert set(stored["by_head"]) == set(union)
    assert stored["by_head"]["topic-r"] == [
        {"number": 4, "state": "open", "head": remote_tip, "base": "trunk"}
    ]
    facts = branch_pr_facts(widened)
    assert facts[(real(w.a), "topic")][0] == [
        {"number": 7, "state": "open", "head_matches": True}
    ]
    assert facts[(real(w.a), "plain")][0] == [
        {"number": 3, "state": "open", "head_matches": False}
    ]
    assert facts[(real(w.a), "trunk")][0] == []
    assert facts[(real(w.a), "landed")][1]["state"] == "merged", "control: a merged local branch"
    assert facts[(real(w.a), "topic")][1]["state"] == "not_merged", "control: an unmerged one"

    fake.replay(stdout=pr_answer(sorted(local), pulled))
    before = set(fake.state.glob("call.*"))
    local_alone = collect([w.folder], remote_checks=True, github_lookup=True)
    assert requested_names(one_gh_request(fake, before)) == sorted(local)
    assert branch_pr_facts(local_alone) == facts


# ---------------------------------------------------------------------------
# Remote-only verdicts: a merged request proves a tip no clone holds
# ---------------------------------------------------------------------------


@posix_only
def test_a_remote_only_tip_reads_the_lowest_merged_request_into_the_default_base(
    status_world, tmp_path, monkeypatch
):
    w = status_world
    w.branch(w.a, "topic", push=True)
    tip = w.push_from_outside("topic")
    w.branch(w.a, "settled", push=True)
    assert rev(w.a, "topic") != tip, "control: no configured clone holds the pushed tip"
    for name in ("topic", "settled"):
        w.package(name, name)
    w.github()
    names = sorted(set(local_names(w.a, w.b)) | {"topic", "settled"})
    fake = install_fake_gh(tmp_path, monkeypatch)
    members = (w.a, w.b)
    before = {member: object_files(member) for member in members}

    # A merged request into another base proves nothing for this branch.
    fake.replay(
        stdout=pr_answer(names, {"topic": [pr_node(2, "merged", tip, base="release")]})
    )
    elsewhere = w.collect(github_lookup=True)

    assert_remote(remote_of(elsewhere, w.a), "present", tip, "live")
    assert remote_verdict_of(elsewhere, w.a) == remote_unknown("objects_missing")
    # A package branch whose remote tip equals a local copy's has no remote verdict.
    assert remote_verdict_of(elsewhere, w.a, "settled") is None

    # The lowest merged request into the default base decides, not the first listed
    # and not a lower request into another base.
    fake.replay(
        stdout=pr_answer(
            names,
            {
                "topic": [
                    pr_node(5, "merged", tip),
                    pr_node(3, "merged", tip),
                    pr_node(2, "merged", tip, base="release"),
                ]
            },
        )
    )
    proved = w.collect(github_lookup=True)

    assert remote_verdict_of(proved, w.a) == {
        "state": "merged", "rung": "pull_request", "reason": None, "pull_request": 3,
    }
    assert remote_verdict_of(proved, w.a, "settled") is None
    assert {member: object_files(member) for member in members} == before


@posix_only
def test_without_a_remote_head_the_one_clone_base_name_proves_the_remote_only_tip(
    status_world, tmp_path, monkeypatch
):
    w = status_world
    w.branch(w.a, "topic", push=True)
    tip = w.push_from_outside("topic")
    git(w.a, "push", "-q", "origin", "trunk:master")
    for member in (w.a, w.b):
        git(member, "fetch", "-q", "origin")
    git(w.a, "remote", "set-head", "origin", "trunk")
    git(w.b, "remote", "set-head", "origin", "master")
    # The bare remote's HEAD names a branch the remote does not have.
    git(w.remote, "symbolic-ref", "HEAD", "refs/heads/gone")
    w.package("topic", "topic")
    w.github()
    install_fake_gh(
        tmp_path,
        monkeypatch,
        stdout=pr_answer(
            sorted(set(local_names(w.a, w.b)) | {"topic"}),
            # Either clone's base name would match one of these two requests, so the
            # split below is skipped for naming several, not for lacking a proof.
            {
                "topic": [
                    pr_node(7, "merged", tip),
                    pr_node(6, "merged", tip, base="master"),
                ]
            },
        ),
    )

    split = w.collect(github_lookup=True)

    repository = repository_of(split, w.a)
    assert repository["default_branch"] == {"name": None, "sha": None}
    assert {
        clone_at(split, member)["verdict_base"]["name"] for member in (w.a, w.b)
    } == {"trunk", "master"}
    # The head-equality proof is skipped for several base names, but A's own base name
    # still proves the tip through its holder ladder; A is the first in natural order.
    assert remote_verdict_of(split, w.a) == {
        "state": "merged", "rung": "pull_request", "reason": None, "pull_request": 7,
    }

    git(w.b, "remote", "set-head", "origin", "trunk")
    agreed = w.collect(github_lookup=True)

    assert {
        clone_at(agreed, member)["verdict_base"]["name"] for member in (w.a, w.b)
    } == {"trunk"}
    assert remote_verdict_of(agreed, w.a) == {
        "state": "merged", "rung": "pull_request", "reason": None, "pull_request": 7,
    }


@posix_only
def test_without_a_remote_head_the_agreed_base_name_proves_a_tip_no_clone_holds(
    status_world, tmp_path, monkeypatch
):
    w = status_world
    w.branch(w.a, "topic", push=True)
    for member in (w.a, w.b):
        git(member, "fetch", "-q", "origin")
        git(member, "remote", "set-head", "origin", "trunk")
    # The bare remote's HEAD names a branch the remote does not have, so no answer sets
    # the default branch while both clones' verdict bases name trunk.
    git(w.remote, "symbolic-ref", "HEAD", "refs/heads/gone")
    # The tip is pushed after both clones last fetched, so no configured clone holds it.
    tip = w.push_from_outside("topic")
    w.package("topic", "topic")
    w.github()
    install_fake_gh(
        tmp_path,
        monkeypatch,
        stdout=pr_answer(
            sorted(set(local_names(w.a, w.b)) | {"topic"}),
            {"topic": [pr_node(7, "merged", tip)]},
        ),
    )

    proved = w.collect(github_lookup=True)

    repository = repository_of(proved, w.a)
    assert repository["default_branch"] == {"name": None, "sha": None}
    assert {
        clone_at(proved, member)["verdict_base"]["name"] for member in (w.a, w.b)
    } == {"trunk"}, "control: the clones' verdict bases agree on one name"
    assert tip not in {
        rev(w.a, "refs/remotes/origin/topic"), rev(w.b, "refs/remotes/origin/topic")
    }, "control: neither clone holds the tip by ref"
    assert all(
        git(member, "cat-file", "-e", tip, check=False).returncode != 0
        for member in (w.a, w.b)
    ), "control: neither clone holds the tip's objects"
    # The one agreed base name is the proof's base even though no remote answer set it.
    assert remote_verdict_of(proved, w.a) == {
        "state": "merged", "rung": "pull_request", "reason": None, "pull_request": 7,
    }


@posix_only
def test_a_tip_no_clone_holds_reads_the_reason_of_its_probes_when_the_bases_differ(
    status_world, tmp_path, monkeypatch
):
    w = status_world
    w.branch(w.a, "topic", push=True)
    git(w.a, "push", "-q", "origin", "trunk:master")
    for member in (w.a, w.b):
        git(member, "fetch", "-q", "origin")
    git(w.a, "remote", "set-head", "origin", "trunk")
    git(w.b, "remote", "set-head", "origin", "master")
    git(w.remote, "symbolic-ref", "HEAD", "refs/heads/gone")
    # The tip is pushed after both clones last fetched, so no configured clone holds it.
    tip = w.push_from_outside("topic")
    w.package("topic", "topic")
    w.github()
    install_fake_gh(
        tmp_path,
        monkeypatch,
        stdout=pr_answer(
            sorted(set(local_names(w.a, w.b)) | {"topic"}),
            {
                "topic": [
                    pr_node(7, "merged", tip),
                    pr_node(6, "merged", tip, base="master"),
                ]
            },
        ),
    )

    absent = w.collect(github_lookup=True)

    assert {
        clone_at(absent, member)["verdict_base"]["name"] for member in (w.a, w.b)
    } == {"trunk", "master"}
    assert tip not in {rev(w.a, "refs/remotes/origin/topic"), rev(w.b, "refs/remotes/origin/topic")}, (
        "control: neither clone holds the tip by ref"
    )
    # Several base names skip the head-equality proof, and no clone holds the object.
    assert remote_verdict_of(absent, w.a) == remote_unknown("objects_missing")

    # A failed probe is a failed call, not a missing object.
    with path_with(
        monkeypatch,
        stand_in(tmp_path, "failing-probe", 'case "$*" in *"--no-walk"*) exit 128;; esac'),
    ):
        failed = w.collect(github_lookup=True)
    assert remote_verdict_of(failed, w.a) == remote_unknown("failed")

    # A cut probe of one clone outranks a failed probe of the other.
    with path_with(
        monkeypatch,
        stand_in(
            tmp_path,
            "cut-then-failing-probe",
            'case "$*" in *"Repo Clone A"*"--no-walk"*) exec sleep 60;;'
            ' *"--no-walk"*) exit 128;; esac',
        ),
    ):
        cut = w.collect(github_lookup=True, call_cap_seconds=1.0)
    assert remote_verdict_of(cut, w.a) == remote_unknown("deadline")
    assert local_verdicts(cut) == local_verdicts(absent)


def test_a_package_branch_with_no_remote_copy_has_no_remote_verdict(status_world):
    w = status_world
    w.branch(w.a, "unpushed")
    w.package("unpushed", "unpushed")
    w.package("ghost", "ghost")

    document = w.collect()

    # A branch never pushed and a branch nobody holds have no present remote copy.
    for name in ("unpushed", "ghost"):
        assert remote_of(document, w.a, name)["state"] != "present"
        assert remote_verdict_of(document, w.a, name) is None


# ---------------------------------------------------------------------------
# Remote-only verdicts from a holder clone
# ---------------------------------------------------------------------------


def local_verdicts(document: dict[str, Any]) -> dict[tuple[str, str], tuple[Any, Any]]:
    """Every local branch's verdict and containment, by clone path and branch name."""
    return {
        (clone["path"], branch["name"]): (branch["verdict"], branch["contained_in"])
        for repository in document["repositories"]
        for clone in repository["clones"]
        for branch in clone["branches"]
    }


def probes_naming(log: Path, tip: str) -> list[list[str]]:
    """Every logged object probe of ``tip``."""
    return [
        call
        for call in logged_calls(log)
        if "rev-list" in call and "--no-walk" in call and tip in call
    ]


def probes_from(log: Path, member: Path, tip: str) -> list[list[str]]:
    """The logged object probes of ``tip`` that ran in ``member``'s clone."""
    return [
        call
        for call in probes_naming(log, tip)
        if "-C" in call and real(call[call.index("-C") + 1]) == real(member)
    ]


def first_parent_calls(log: Path) -> list[list[str]]:
    """Every logged first-parent listing of a verdict base."""
    return [
        call
        for call in logged_calls(log)
        if "rev-list" in call and "--first-parent" in call
    ]


@posix_only
def test_a_holder_with_the_tip_as_an_object_decides_it_and_a_cut_run_reads_the_entry(
    status_world, tmp_path, monkeypatch
):
    """The delayed first run is cut, the undelayed second decides from A's object probe
    and stores its entry, and the delayed third reads that entry."""
    w = status_world
    w.branch(w.a, "topic", commits=2, push=True)
    tip = rev(w.a, "topic")
    # A lands the work by a squash merge and pushes it, then drops both its local branch
    # and its own tracking ref, keeping the objects the probe has to find.
    git(w.a, "merge", "-q", "--squash", "topic")
    git(w.a, "commit", "-q", "-m", "land topic")
    git(w.a, "push", "-q", "origin", "trunk")
    git(w.a, "branch", "-q", "-D", "topic")
    git(w.a, "branch", "-q", "-dr", "origin/topic")
    # A local branch a cut of the remote-only pass must not disturb.
    git(w.a, "branch", "spare", "trunk~1")
    w.package("topic", "topic")
    # The chain starts on an account home that holds no saved verdict.
    cache = nyx_home(Path(os.environ["HOME"]))
    assert not cache.exists(), "control: the account home holds no saved verdict"

    slow_tip = stand_in(
        tmp_path, "slow-tip", f'case "$*" in *"{tip}"*) exec sleep 30;; esac'
    )
    with path_with(monkeypatch, slow_tip):
        cut = w.collect(seconds=3.0)

    # A cut probe of the clone with a base is not a missing object.
    assert remote_verdict_of(cut, w.a) == remote_unknown("deadline")
    key = f"{tip}.{clone_at(cut, w.a)['verdict_base']['sha']}"
    assert key not in (cache_entries(cache) if cache.exists() else {}), (
        "control: no collection stored a verdict for this tip and base"
    )

    recorder, log = argument_recorder(tmp_path)
    with path_with(monkeypatch, recorder):
        decided = w.collect()

    assert remote_verdict_of(decided, w.a) == {
        "state": "merged",
        "rung": "squash_equivalent",
        "reason": None,
        "pull_request": None,
    }
    assert probes_from(log, w.a, tip), "the decided verdict comes from A's object probe"
    assert cache_entries(cache).get(key) == "squash_equivalent"
    assert local_verdicts(cut) == local_verdicts(decided)

    work_log = tmp_path / "patch-work.log"
    slow_work = stand_in(
        tmp_path,
        "slow-work",
        DELAYED_PATCH_WORK.replace(
            "exec sleep 30", f'printf "%s\\n" "$*" >> "{work_log}"; exec sleep 30'
        ),
    )
    with path_with(monkeypatch, slow_work):
        stored = w.collect(seconds=3.0)

    assert work_log.exists() and work_log.read_text().strip(), (
        "control: a patch rung ran and the deadline cut it, so only the entry decides"
    )
    assert remote_verdict_of(stored, w.a) == remote_verdict_of(decided, w.a)
    assert local_verdicts(stored) == local_verdicts(decided)


class HolderWorld:
    """One remote with four clones in the folder: A (the target of every package), B,
    a shallow clone whose path sorts first, and a clone with no verdict base.

    Every package branch of the first root has no local copy. ``topic`` is squash-merged
    with A's tracking ref kept; ``topic2`` is squash-merged with A's tracking ref deleted
    and the shallow clone holding it; ``base-less-shared`` is squash-merged, held by A and
    the base-less clone; ``base-less-only`` is squash-merged and only the base-less clone
    holds it. ``empty-pushed`` has no commit of its own, ``fast-forward`` was fast-forward
    merged and ``merge-commit`` was merged by a merge commit. A second root names one
    branch per remote-copy Code Status rule: ``topic-r`` has an open request, ``unheld``
    no holder and no request, ``pr-proved`` a merged request, ``merged-behind`` a local
    copy behind a remote another clone merged by a merge commit,
    ``squash-then-extended`` a squash-merged local copy under an extended remote copy,
    and ``waiting-remote`` a finished package's branch a clone holds and judges not
    merged. One module-scoped build serves every collection the holder rules are proved
    with.
    """

    names = (
        "topic",
        "topic2",
        "empty-pushed",
        "fast-forward",
        "merge-commit",
        "base-less-shared",
        "base-less-only",
    )
    extra_names = (
        "merged-behind",
        "squash-then-extended",
        "topic-r",
        "unheld",
        "pr-proved",
        "waiting-remote",
    )

    def __init__(self, base: Path) -> None:
        self.folder = base / "folder"
        self.folder.mkdir()
        served = base / "served"
        served.mkdir()
        self.remote = make_remote(served)
        self.a = clone(self.remote, self.folder / "Repo Clone 1")
        self.b = clone(self.remote, self.folder / "Repo Clone 2")
        self.n = clone(self.remote, self.folder / "Repo No Base")
        self.outside = clone(self.remote, base / "outside")
        self.sha: dict[str, str] = {}
        self.count = 0
        self.build(base)

    def commit(self, member: Path) -> str:
        self.count += 1
        return commit_file(member, f"change-{self.count}.txt", f"{self.count}\n")

    def land(self, name: str, *, keep_ref: bool) -> str:
        """Push a two-commit branch, squash-merge it into trunk, push trunk and drop the
        local branch, keeping or deleting A's own tracking ref of the name."""
        git(self.a, "checkout", "-q", "-b", name, "trunk")
        self.commit(self.a)
        self.commit(self.a)
        tip = rev(self.a, name)
        git(self.a, "push", "-q", "origin", name)
        git(self.a, "checkout", "-q", "trunk")
        git(self.a, "merge", "-q", "--squash", name)
        git(self.a, "commit", "-q", "-m", f"land {name}")
        git(self.a, "push", "-q", "origin", "trunk")
        git(self.a, "branch", "-q", "-D", name)
        if not keep_ref:
            git(self.a, "update-ref", "-d", f"refs/remotes/origin/{name}", check=False)
        return tip

    def build(self, base: Path) -> None:
        w = self
        w.sha["topic"] = w.land("topic", keep_ref=True)
        w.sha["topic2"] = w.land("topic2", keep_ref=False)
        # A branch pushed with no commit of its own: its tip is the default history.
        git(w.a, "push", "-q", "origin", "trunk:refs/heads/empty-pushed")
        git(w.a, "update-ref", "-d", "refs/remotes/origin/empty-pushed", check=False)
        w.sha["empty-pushed"] = git(w.a, "rev-parse", "trunk").stdout.strip()
        # One branch fast-forward merged into the default branch, one merged by a merge.
        for name in ("fast-forward", "merge-commit"):
            git(w.a, "checkout", "-q", "-b", name, "trunk")
            w.commit(w.a)
            w.sha[name] = rev(w.a, name)
            git(w.a, "push", "-q", "origin", name)
            git(w.a, "checkout", "-q", "trunk")
            if name == "fast-forward":
                git(w.a, "merge", "-q", "--ff-only", name)
            else:
                git(w.a, "merge", "-q", "--no-ff", "-m", f"merge {name}", name)
            git(w.a, "push", "-q", "origin", "trunk")
            git(w.a, "branch", "-q", "-D", name)
            git(w.a, "update-ref", "-d", f"refs/remotes/origin/{name}", check=False)
        # Two branches only a clone outside the folder ever pushes.
        for name in ("base-less-shared", "base-less-only"):
            git(w.outside, "fetch", "-q", "origin")
            git(w.outside, "checkout", "-q", "-B", name, "origin/trunk")
            w.commit(w.outside)
            w.commit(w.outside)
            w.sha[name] = rev(w.outside, name)
            git(w.outside, "push", "-q", "origin", name)
            git(w.outside, "checkout", "-q", "trunk")
        # A lands one of them and keeps its tracking ref; the other it never fetches.
        git(w.a, "fetch", "-q", "origin", "+refs/heads/base-less-shared:refs/remotes/origin/base-less-shared")
        git(w.a, "merge", "-q", "--squash", "origin/base-less-shared")
        git(w.a, "commit", "-q", "-m", "land base-less-shared")
        git(w.a, "push", "-q", "origin", "trunk")
        # The base-less clone holds both tips by ref but no default branch at all.
        for name in ("base-less-shared", "base-less-only"):
            git(w.n, "fetch", "-q", "origin", f"+refs/heads/{name}:refs/remotes/origin/{name}")
        git(w.n, "update-ref", "-d", "refs/remotes/origin/trunk")
        git(w.n, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        # The shallow clone sorts first, shares A's base and holds topic2's tip by ref.
        # A local path clone ignores --depth, so it clones through the remote's URI.
        w.s = clone(w.remote.as_uri(), w.folder / "A Shallow", "--depth", "1")
        git(
            w.s,
            "fetch",
            "-q",
            "--depth",
            "1",
            "origin",
            "+refs/heads/topic2:refs/remotes/origin/topic2",
        )
        # A squash-merges a branch it keeps locally, and the remote copy gains one commit
        # that B holds only through its tracking ref.
        name = "squash-then-extended"
        git(w.a, "checkout", "-q", "-b", name, "trunk")
        w.commit(w.a)
        w.commit(w.a)
        git(w.a, "push", "-q", "origin", name)
        git(w.a, "checkout", "-q", "trunk")
        git(w.a, "merge", "-q", "--squash", name)
        git(w.a, "commit", "-q", "-m", f"land {name}")
        git(w.a, "push", "-q", "origin", "trunk")
        git(w.outside, "fetch", "-q", "origin")
        git(w.outside, "checkout", "-q", "-B", name, f"origin/{name}")
        w.commit(w.outside)
        w.sha[name] = rev(w.outside, name)
        git(w.outside, "push", "-q", "origin", name)
        git(w.outside, "checkout", "-q", "trunk")
        git(
            w.b,
            "fetch",
            "-q",
            "origin",
            "+refs/heads/trunk:refs/remotes/origin/trunk",
            f"+refs/heads/{name}:refs/remotes/origin/{name}",
        )
        # A keeps a local copy the remote moves past; B merges the remote tip by a merge
        # commit and deletes its own copy, so it holds the tip only by its tracking ref.
        name = "merged-behind"
        git(w.a, "checkout", "-q", "-b", name, "trunk")
        w.commit(w.a)
        w.commit(w.a)
        git(w.a, "push", "-q", "origin", name)
        git(w.a, "checkout", "-q", "trunk")
        git(w.outside, "fetch", "-q", "origin")
        git(w.outside, "checkout", "-q", "-B", name, f"origin/{name}")
        w.commit(w.outside)
        w.sha[name] = rev(w.outside, name)
        git(w.outside, "push", "-q", "origin", name)
        git(w.outside, "checkout", "-q", "trunk")
        git(
            w.b,
            "fetch",
            "-q",
            "origin",
            "+refs/heads/trunk:refs/remotes/origin/trunk",
            f"+refs/heads/{name}:refs/remotes/origin/{name}",
        )
        git(w.b, "checkout", "-q", "trunk")
        git(w.b, "merge", "-q", "--ff-only", "origin/trunk")
        git(w.b, "checkout", "-q", "-B", name, f"origin/{name}")
        git(w.b, "checkout", "-q", "trunk")
        git(w.b, "merge", "-q", "--no-ff", "-m", f"merge {name}", name)
        git(w.b, "push", "-q", "origin", "trunk")
        git(w.b, "branch", "-q", "-D", name)
        # A branch B holds only through its tracking ref and judges not merged.
        name = "waiting-remote"
        git(w.outside, "fetch", "-q", "origin")
        git(w.outside, "checkout", "-q", "-B", name, "origin/trunk")
        w.commit(w.outside)
        w.sha[name] = rev(w.outside, name)
        git(w.outside, "push", "-q", "origin", name)
        git(w.outside, "checkout", "-q", "trunk")
        git(
            w.b,
            "fetch",
            "-q",
            "origin",
            "+refs/heads/trunk:refs/remotes/origin/trunk",
            f"+refs/heads/{name}:refs/remotes/origin/{name}",
        )
        # Branches no configured clone ever fetches: one with an open request, one with
        # none, and one a merged request proves.
        for name in ("unheld", "topic-r", "pr-proved"):
            git(w.outside, "fetch", "-q", "origin")
            git(w.outside, "checkout", "-q", "-B", name, "origin/trunk")
            w.commit(w.outside)
            w.sha[name] = rev(w.outside, name)
            git(w.outside, "push", "-q", "origin", name)
            git(w.outside, "checkout", "-q", "trunk")
        for member in (w.a, w.b, w.s, w.n):
            point_origin_at(member, w.remote, GITHUB_URL)
        w.root = base / "spec"
        for name in w.names:
            make_package(
                w.root, "Drafts", name, f"Target repo: {w.a}\nBranch: {name}\n"
            )
        w.baseless_root = base / "baseless-spec"
        for name in ("base-less-shared", "base-less-only"):
            make_package(
                w.baseless_root, "Drafts", name, f"Target repo: {w.a}\nBranch: {name}\n"
            )
        w.squash_root = base / "squash-spec"
        for name in ("topic", "topic2"):
            make_package(
                w.squash_root, "Drafts", name, f"Target repo: {w.a}\nBranch: {name}\n"
            )
        w.extra_root = base / "extra-spec"
        for name in w.extra_names:
            # The finished package waits for a merge no request proves.
            stage = "Shipped" if name == "waiting-remote" else "Drafts"
            make_package(
                w.extra_root, stage, name, f"Target repo: {w.a}\nBranch: {name}\n"
            )

    def collect(self, root: Path | None, **options: Any) -> dict[str, Any]:
        """Collect the folder with ``root``'s packages, proving no repository changed."""
        members = (self.a, self.b, self.s, self.n, self.outside, self.remote)
        before = {member: tree_fingerprint(member) for member in members}
        document = collect_packages([self.folder], root, **options)
        assert {member: tree_fingerprint(member) for member in members} == before
        return document


def holder_recorder(directory: Path, name: str) -> tuple[Path, Path]:
    """An argument recorder of its own, so each collection's calls are read apart."""
    where = directory / name
    where.mkdir()
    return argument_recorder(where)


@pytest.fixture(scope="module")
def holder_world(tmp_path_factory):
    with shared_world_build(tmp_path_factory, "holder-world") as (directory, home, patch):
        w = HolderWorld(directory)
        # The base-less clone is no holder, and no saved remote check can give it a base:
        # this run goes first, on the build home that holds no saved check yet.
        recorder, log = holder_recorder(directory, "baseless")
        with path_with(patch, recorder):
            w.baseless = w.collect(w.baseless_root, remote_checks=False)
        w.baseless_calls = log
        w.baseless_plain = w.collect(None, remote_checks=False)
        w.cache = nyx_home(home)
        # The first networked collection: no stored verdict answers topic2's tip, so an
        # implementation that skipped the probe for a shallow ref holder would read it
        # unknown/incomplete_clone.
        recorder, log = holder_recorder(directory, "recorded")
        with path_with(patch, recorder):
            w.recorded = w.collect(w.root, remote_checks=True)
        w.recorded_calls = log
        w.plain = w.collect(None, remote_checks=True)
        # The same world collected for branches whose tips only the squash rung decides.
        recorder, log = holder_recorder(directory, "squash")
        with path_with(patch, recorder):
            w.squash = w.collect(w.squash_root, remote_checks=True)
        w.squash_calls = log
        # One first-parent listing that fails, and one the per-call cap cuts.
        with path_with(
            patch,
            stand_in(
                directory,
                "failing-listing",
                'case "$*" in *"rev-list --first-parent"*) exit 128;; esac',
            ),
        ):
            w.failed = w.collect(w.root, remote_checks=True)
        with path_with(
            patch,
            stand_in(
                directory,
                "slow-listing",
                'case "$*" in *"rev-list --first-parent"*) exec sleep 60;; esac',
            ),
        ):
            w.cut = w.collect(w.root, remote_checks=True, call_cap_seconds=1.0)
        # A merged request into the default branch proves an otherwise ambiguous tip.
        names = sorted(set(local_names(w.a, w.b, w.s, w.n)) | set(w.names))
        fake = install_fake_gh(
            directory,
            patch,
            stdout=pr_answer(
                names, {"fast-forward": [pr_node(4, "merged", w.sha["fast-forward"])]}
            ),
        )
        w.github = w.collect(w.root, remote_checks=True, github_lookup=True)
        fake.replay(stdout=pr_answer(sorted(local_names(w.a, w.b, w.s, w.n)), {}))
        w.plain_github = w.collect(None, remote_checks=True, github_lookup=True)
        # The remote-copy Code Status cases share one more collection of the same world,
        # with the live listing that answers their open and their merged request.
        requested = sorted(set(local_names(w.a, w.b, w.s, w.n)) | set(w.extra_names))
        fake.replay(
            stdout=pr_answer(
                requested,
                {
                    "topic-r": [pr_node(4, "open", w.sha["topic-r"])],
                    "pr-proved": [pr_node(6, "merged", w.sha["pr-proved"])],
                },
            )
        )
        w.extra = w.collect(
            w.extra_root,
            completed=FINISHED_STAGES,
            remote_checks=True,
            github_lookup=True,
        )
    return w


SQUASH_MERGED = {
    "state": "merged",
    "rung": "squash_equivalent",
    "reason": None,
    "pull_request": None,
}
MERGED_ANCESTOR = {
    "state": "merged",
    "rung": "ancestor",
    "reason": None,
    "pull_request": None,
}


@posix_only
def test_a_complete_ref_holder_decides_its_tip_without_an_object_probe(holder_world):
    w = holder_world

    assert remote_verdict_of(w.recorded, w.a, "topic") == SQUASH_MERGED
    assert probes_naming(w.recorded_calls, w.sha["topic"]) == [], (
        "a complete clone holds the tip by ref, so nothing probes it"
    )


@posix_only
def test_a_shallow_ref_holder_does_not_skip_the_probe_of_a_complete_clone(holder_world):
    w = holder_world
    repository = repository_of(w.recorded, w.a)
    assert [clone["path"] for clone in repository["clones"]][0] == real(w.s), (
        "control: the shallow clone sorts first"
    )
    assert clone_at(w.recorded, w.s)["shallow"] is True
    assert clone_at(w.recorded, w.s)["verdict_base"] is not None, (
        "control: the shallow clone has a base"
    )
    assert rev(w.s, "refs/remotes/origin/topic2") == w.sha["topic2"], (
        "control: the shallow clone holds the tip by ref"
    )
    assert probes_from(w.recorded_calls, w.s, w.sha["topic2"]) == [], (
        "the clone that holds the tip by ref is not probed for it"
    )

    # A shallow ref holder does not stop the probe: A's object makes A the holder, and a
    # shallow ladder would only report incomplete_clone.
    assert remote_verdict_of(w.recorded, w.a, "topic2") == SQUASH_MERGED
    assert probes_from(w.recorded_calls, w.a, w.sha["topic2"]), (
        "the verdict comes from A's probe, so a shallow ref holder skipped nothing"
    )


@posix_only
def test_a_clone_without_a_base_is_no_holder_and_a_base_less_tip_reads_no_base(
    holder_world,
):
    w = holder_world
    assert clone_at(w.baseless, w.n)["verdict_base"] is None, (
        "control: the clone has no verdict base"
    )

    # A complete clone also holds this tip by ref, so it decides it: the base-less clone
    # is never a candidate holder.
    assert remote_verdict_of(w.baseless, w.a, "base-less-shared") == SQUASH_MERGED
    assert probes_naming(w.baseless_calls, w.sha["base-less-shared"]) == [], (
        "a complete ref holder skips the probe"
    )

    # Only the base-less clone holds this tip, by ref: the clones with a base are probed
    # and none of them holds it.
    assert remote_verdict_of(w.baseless, w.a, "base-less-only") == remote_unknown("no_base")
    assert probes_from(w.baseless_calls, w.a, w.sha["base-less-only"])
    assert probes_from(w.baseless_calls, w.b, w.sha["base-less-only"])


class ObjectOnlyWorld:
    """One remote with two clones that keep a base and two that do not.

    ``trunk`` moves on the remote after every clone is made, so no clone holds the live
    base commit; the two no-base clones drop their remote-tracking copy of it, and the
    two others keep theirs as a stale base. Two package branches are pushed only by a
    clone outside the folder, and no configured clone holds either by ref: the first
    no-base clone fetches one tip as an object, and the second never fetches it. The
    base-less clones are last in natural order, the holder before the other.
    """

    names = ("object", "cut")

    def __init__(self, base: Path) -> None:
        self.folder = base / "folder"
        self.folder.mkdir()
        served = base / "served"
        served.mkdir()
        self.remote = make_remote(served)
        self.a = clone(self.remote, self.folder / "Repo Clone 1")
        self.b = clone(self.remote, self.folder / "Repo Clone 2")
        self.n = clone(self.remote, self.folder / "Repo No Base 1")
        self.other = clone(self.remote, self.folder / "Repo No Base 2")
        self.outside = clone(self.remote, base / "outside")
        self.sha: dict[str, str] = {}
        self.build(base)

    def build(self, base: Path) -> None:
        w = self
        for name in w.names:
            git(w.outside, "fetch", "-q", "origin")
            git(w.outside, "checkout", "-q", "-B", name, "origin/trunk")
            w.sha[name] = commit_file(w.outside, f"{name}.txt", f"{name}\n")
            git(w.outside, "push", "-q", "origin", name)
            git(w.outside, "checkout", "-q", "trunk")
        # The default branch moves past every clone after the branches were pushed, so
        # the live base commit is one no clone holds.
        commit_file(w.outside, "trunk.txt", "trunk\n")
        git(w.outside, "push", "-q", "origin", "trunk")
        # The two no-base clones keep no remote-tracking copy of the default branch, so
        # they have neither the remote's current commit nor a cached base.
        for member in (w.n, w.other):
            git(member, "update-ref", "-d", "refs/remotes/origin/trunk", check=False)
            git(member, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD", check=False)
        # The first no-base clone holds one tip as an object, with no ref naming it.
        git(w.n, "fetch", "-q", "origin", w.sha["object"])
        w.object_root = base / "object-spec"
        make_package(
            w.object_root, "Drafts", "object", f"Target repo: {w.a}\nBranch: object\n"
        )
        w.cut_root = base / "cut-spec"
        make_package(w.cut_root, "Drafts", "cut", f"Target repo: {w.a}\nBranch: cut\n")

    def collect(self, root: Path, **options: Any) -> dict[str, Any]:
        """Collect the folder with ``root``'s packages, proving no repository changed."""
        members = (self.a, self.b, self.n, self.other, self.outside, self.remote)
        before = {member: tree_fingerprint(member) for member in members}
        document = collect_packages([self.folder], root, **options)
        assert {member: tree_fingerprint(member) for member in members} == before
        return document


@pytest.fixture
def object_only_world(tmp_path):
    return ObjectOnlyWorld(tmp_path)


@posix_only
def test_a_base_less_clone_holding_the_tip_only_as_an_object_reads_no_base(
    object_only_world, tmp_path, monkeypatch
):
    w = object_only_world
    shim, log = argument_recorder(tmp_path)
    with path_with(monkeypatch, shim):
        document = w.collect(w.object_root, remote_checks=True)

    paths = [clone["path"] for clone in repository_of(document, w.a)["clones"]]
    assert paths[-2:] == [real(w.n), real(w.other)], (
        "control: the two base-less clones are probed in this order"
    )
    assert clone_at(document, w.n)["verdict_base"] is None, "control: no verdict base"
    assert clone_at(document, w.other)["verdict_base"] is None, "control: no verdict base"
    assert clone_at(document, w.a)["verdict_base"] is not None, (
        "control: a clone with a base"
    )
    # The first base-less clone holds the tip as an object only, and no clone of the
    # repository holds it by ref or, with a base, as an object.
    assert git(w.n, "cat-file", "-e", w.sha["object"]).returncode == 0
    assert git(w.other, "cat-file", "-e", w.sha["object"], check=False).returncode != 0
    for member in (w.a, w.b):
        assert git(member, "cat-file", "-e", w.sha["object"], check=False).returncode != 0
    for member in (w.a, w.b, w.n, w.other):
        assert not git(member, "branch", "--list", "object").stdout.strip()
        assert (
            git(
                member,
                "rev-parse",
                "--verify",
                "-q",
                "refs/remotes/origin/object",
                check=False,
            ).returncode
            != 0
        )
    assert probes_from(log, w.n, w.sha["object"]), "the object holder is probed"
    assert probes_from(log, w.other, w.sha["object"]), "the other base-less clone is probed"

    # Both base-less clones were probed for the tip, and the absent answer of the second
    # must not hide the first's present one: the reason is no_base, not objects_missing.
    assert remote_verdict_of(document, w.a, "object") == remote_unknown("no_base")


@posix_only
def test_a_cut_base_less_probe_reads_deadline_not_objects_missing(
    object_only_world, tmp_path, monkeypatch
):
    w = object_only_world
    tip = w.sha["cut"]
    plain = w.collect(w.cut_root, remote_checks=True)
    assert remote_verdict_of(plain, w.a, "cut") == remote_unknown("objects_missing"), (
        "control: every probe reported the object absent"
    )

    # The cut call is the base-less clone's own probe of the tip; the log proves the
    # delaying wrapper saw no other clone's probe of it.
    log = tmp_path / "cut-calls.log"
    record = (
        'sep=$(printf "\\037"); line=""; '
        'for argument in "$@"; do line="$line$argument$sep"; done'
    )
    shim = stand_in(
        tmp_path,
        "cut-baseless-probe",
        f'{record}; printf "%s\\n" "$line" >> "{log}"; '
        f'case "$*" in *"Repo No Base 1"*"{tip}"*) exec sleep 60;; esac',
    )
    with path_with(monkeypatch, shim):
        cut = w.collect(w.cut_root, remote_checks=True, call_cap_seconds=1.0)

    delayed = [
        call
        for call in probes_naming(log, tip)
        if any("Repo No Base 1" in argument for argument in call)
    ]
    assert delayed == probes_from(log, w.n, tip), "only the base-less clone's probe is delayed"
    assert len(delayed) == 1
    assert probes_from(log, w.a, tip) and probes_from(log, w.b, tip), (
        "the clones with a base probed the tip and were not cut"
    )
    assert remote_verdict_of(cut, w.a, "cut") == remote_unknown("deadline")
    assert local_verdicts(cut) == local_verdicts(plain)


@posix_only
def test_a_tip_on_the_default_history_is_ambiguous_and_a_merge_commit_stays_merged(
    holder_world,
):
    w = holder_world
    repository = repository_of(w.recorded, w.a)
    assert not (
        set(w.names)
        & {branch["name"] for clone in repository["clones"] for branch in clone["branches"]}
    ), "control: every package branch has no local copy"

    for name in ("empty-pushed", "fast-forward"):
        assert remote_verdict_of(w.recorded, w.a, name) == remote_unknown("ambiguous_tip")
        assert_status(w.recorded, name, "merge state unknown", "unknown")
    assert remote_verdict_of(w.recorded, w.a, "merge-commit") == MERGED_ANCESTOR
    assert remote_verdict_of(w.recorded, w.a, "topic") == SQUASH_MERGED


@posix_only
def test_one_first_parent_listing_runs_per_holder_with_ancestor_tips(holder_world):
    w = holder_world

    assert len(first_parent_calls(w.recorded_calls)) == 1, (
        "one listing for the holder that has ancestor tips"
    )
    assert first_parent_calls(w.squash_calls) == [], (
        "a holder with only squash tips reads no first-parent history"
    )
    # Control: the squash-only collection did decide both tips by its holder ladder.
    for name in ("topic", "topic2"):
        assert remote_verdict_of(w.squash, w.a, name) == SQUASH_MERGED


@posix_only
def test_a_first_parent_listing_that_fails_or_is_cut_leaves_the_ancestor_tips_unknown(
    holder_world,
):
    w = holder_world

    for document, reason in ((w.failed, "failed"), (w.cut, "deadline")):
        for name in ("empty-pushed", "fast-forward", "merge-commit"):
            assert remote_verdict_of(document, w.a, name) == remote_unknown(reason)
        assert remote_verdict_of(document, w.a, "topic") == SQUASH_MERGED, (
            "control: a tip the squash rung decides needs no first-parent history"
        )


@posix_only
def test_a_merged_pull_request_proves_an_otherwise_ambiguous_tip(holder_world):
    w = holder_world

    assert remote_verdict_of(w.github, w.a, "fast-forward") == {
        "state": "merged",
        "rung": "pull_request",
        "reason": None,
        "pull_request": 4,
    }
    assert remote_verdict_of(w.github, w.a, "empty-pushed") == remote_unknown(
        "ambiguous_tip"
    ), "the proof is the tip's own head, not an ambiguous tip beside it"


@posix_only
def test_every_holder_collection_leaves_every_local_branch_unchanged(holder_world):
    w = holder_world

    # The wrappers above intercept only calls the package pass makes, so the one
    # collection without a package scan is the same collection for each of them.
    for with_packages, without_packages in (
        (w.baseless, w.baseless_plain),
        (w.recorded, w.plain),
        (w.squash, w.plain),
        (w.failed, w.plain),
        (w.cut, w.plain),
        (w.github, w.plain_github),
    ):
        assert local_verdicts(with_packages) == local_verdicts(without_packages)


# ---------------------------------------------------------------------------
# The Code Status of a remote copy whose tip no local copy holds
# ---------------------------------------------------------------------------


@posix_only
def test_a_remote_only_branch_reads_its_open_request_from_the_listing(holder_world):
    w = holder_world
    repository = repository_of(w.extra, w.a)
    assert not any(
        branch["name"] == "topic-r"
        for clone in repository["clones"]
        for branch in clone["branches"]
    ), "control: no local copy lists the request, so the listing alone proves it"

    assert_status(w.extra, "topic-r", "PR #4 open", "pr_open", push="pushed", pull_request=4)


@posix_only
def test_a_remote_only_tip_no_clone_holds_reads_merge_state_unknown(holder_world):
    w = holder_world

    assert remote_verdict_of(w.extra, w.a, "unheld") == remote_unknown("objects_missing")
    assert_status(w.extra, "unheld", "merge state unknown", "unknown")


@posix_only
def test_a_merged_request_proves_a_remote_only_tip_merged(holder_world):
    w = holder_world

    assert remote_verdict_of(w.extra, w.a, "pr-proved") == {
        "state": "merged",
        "rung": "pull_request",
        "reason": None,
        "pull_request": 6,
    }
    # Nothing local is left to clean, so the status is plain merged; collect_packages
    # parsed it, so the document is valid.
    assert_status(
        w.extra, "pr-proved", "merged", "merged", cleanup={"worktrees": 0, "branches": 0}
    )


@posix_only
def test_a_local_copy_behind_a_merged_remote_the_other_clone_holds_reads_cleanup_pending(
    holder_world,
):
    w = holder_world
    (local_copy,) = [
        branch
        for branch in clone_at(w.extra, w.a)["branches"]
        if branch["name"] == "merged-behind"
    ]
    remote = remote_of(w.extra, w.a, "merged-behind")
    assert remote["sha"] == w.sha["merged-behind"], (
        "control: the remote moved past the local copy"
    )
    assert local_copy["sha"] != remote["sha"], "control: the tips differ"
    # The other clone holds the tip only through its tracking ref, and merged it by a
    # merge commit, so it is off its base's first-parent history.
    assert not any(
        branch["name"] == "merged-behind" for branch in clone_at(w.extra, w.b)["branches"]
    ), "control: the other clone has no local copy"
    assert rev(w.b, "refs/remotes/origin/merged-behind") == w.sha["merged-behind"]
    assert remote_verdict_of(w.extra, w.a, "merged-behind") == MERGED_ANCESTOR

    assert_status(
        w.extra,
        "merged-behind",
        f"merged {DOT} cleanup pending (1 branch)",
        "merged",
        cleanup={"worktrees": 0, "branches": 1},
    )


@posix_only
def test_a_squash_merged_local_copy_behind_an_extended_remote_reads_pushed(holder_world):
    w = holder_world
    remote = remote_of(w.extra, w.a, "squash-then-extended")
    assert remote["sha"] == w.sha["squash-then-extended"]
    assert relation_of(w.extra, w.a, "squash-then-extended") == ("behind", 0, 1)
    assert not any(
        branch["name"] == "squash-then-extended"
        for branch in clone_at(w.extra, w.b)["branches"]
    ), "control: the other clone has no local copy"
    assert rev(w.b, "refs/remotes/origin/squash-then-extended") == w.sha[
        "squash-then-extended"
    ]
    verdict = remote_verdict_of(w.extra, w.a, "squash-then-extended")
    assert (verdict["state"], verdict["rung"]) == ("not_merged", "none")

    assert_status(
        w.extra,
        "squash-then-extended",
        f"in progress {DOT} pushed",
        "in_progress",
        push="pushed",
    )


@posix_only
def test_a_finished_package_waiting_for_a_remote_only_branch_reads_awaiting_merge(
    holder_world,
):
    w = holder_world
    assert not any(
        branch["name"] == "waiting-remote" for branch in clone_at(w.extra, w.a)["branches"]
    ), "control: A has no local copy of the finished package's branch"
    assert remote_verdict_of(w.extra, w.a, "waiting-remote")["state"] == "not_merged"

    assert_status(
        w.extra,
        "waiting-remote",
        "awaiting merge (no PR)",
        "awaiting_merge",
        stage="Shipped",
    )
    (item,) = [
        entry for entry in w.extra["attention"] if entry["branch"] == "waiting-remote"
    ]
    assert (item["class"], item["code"]) == ("awaiting_merge", "awaiting_merge")
    assert item["repository"] == repository_of(w.extra, w.a)["remote"]["key"]
    assert item["packages"] == ["Fictional/Shipped/waiting-remote"]
    assert (item["pull_request"], item["count"]) == (None, None)
    # derive_attention stays the one derivation of the list the parsed document carries.
    assert models.derive_attention(w.extra)["attention"] == w.extra["attention"]


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
    # The remote copy's squash-merged verdict is the package's now, and with no local
    # copy left there is nothing to clean up: the status is plain merged, and the
    # document that carries it parses.
    assert_status(
        remote_only, "clone", "merged", "merged", cleanup={"worktrees": 0, "branches": 0}
    )

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
    assert_target(collected["packages"][index]["target"], "missing", within_folders=False)

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
        doc["packages"][index]["target"].update(status="missing", within_folders=False)

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


def test_a_plain_merged_status_is_accepted_with_no_cleanup_counts(status_document):
    assert code_status_of(status_document, "landed")["cleanup"] == {"worktrees": 0, "branches": 1}
    document = doc_copy(status_document)
    set_status_retexted("landed", cleanup={"worktrees": 0, "branches": 0})(document)

    parsed = models.parse_checkouts(redigest(document))

    status = code_status_of(parsed, "landed")
    assert status["text"] == "merged"
    assert status["cleanup"] == {"worktrees": 0, "branches": 0}


def test_a_merged_status_refuses_a_null_cleanup_or_both_counts_positive(status_document):
    index = package_index(status_document, "landed")
    for cleanup, message in (
        (None, f"packages[{index}].code_status.cleanup is set exactly for merged"),
        (
            {"worktrees": 1, "branches": 1},
            f"packages[{index}].code_status.cleanup must not have both counts positive for merged",
        ),
    ):
        assert message in reference_mutation(
            status_document, set_status_retexted("landed", cleanup=cleanup)
        ), cleanup


def test_a_status_that_is_not_merged_refuses_cleanup(status_document):
    index = package_index(status_document, "landed")

    def in_progress(document):
        set_status_retexted(
            "landed", state="in_progress", push="pushed", cleanup={"worktrees": 1, "branches": 0}
        )(document)

    assert f"packages[{index}].code_status.cleanup is set exactly for merged" in (
        reference_mutation(status_document, in_progress)
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


def test_schema_8_is_accepted_schema_7_refused_and_no_commits_yet_is_strict(status_document):
    """One collection serves every proof: each case mutates its own copy."""
    assert status_document["schema_version"] == models.CHECKOUTS_SCHEMA_VERSION == 8
    assert models.parse_checkouts(json.dumps(status_document))["schema_version"] == 8

    old = doc_copy(status_document)
    old["schema_version"] = 7
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
# Parser invariants: the remote verdict is stored exactly when it applies
# ---------------------------------------------------------------------------

def branch_entry_of(repository: dict[str, Any], name: str) -> dict[str, Any]:
    """One package branch entry of a repository, by the branch it names."""
    (entry,) = [item for item in repository["package_branches"] if item["name"] == name]
    return entry


def test_the_parser_refuses_a_remote_verdict_beside_a_local_copy_at_the_tip(status_document):
    (repository,) = status_document["repositories"]
    entry = branch_entry_of(repository, "both")
    entry_index = repository["package_branches"].index(entry)
    copy = entry["remote"]
    target = package_of(status_document, "both")["target"]["clone"]
    local = clone_at(status_document, target)
    assert copy["state"] == "present", "control: the remote answered for the branch"
    assert copy["sha"] in {item["sha"] for item in local["branches"] if item["name"] == "both"}, (
        "control: a local copy holds the remote tip"
    )
    assert entry["remote_verdict"] is None, "control: the accepted document stores no verdict"
    assert models.parse_checkouts(json.dumps(status_document))

    def stored(document):
        document["repositories"][0]["package_branches"][entry_index]["remote_verdict"] = dict(
            MERGED_ANCESTOR
        )

    assert (
        f"repositories[0].package_branches[{entry_index}].remote_verdict "
        "must be null when a local copy holds the remote tip"
    ) in reference_mutation(status_document, stored)


def test_the_parser_refuses_a_null_remote_verdict_for_an_unmatched_present_tip(status_document):
    (repository,) = status_document["repositories"]
    entry = branch_entry_of(repository, "ahead")
    entry_index = repository["package_branches"].index(entry)
    copy = entry["remote"]
    target = package_of(status_document, "ahead")["target"]["clone"]
    local = clone_at(status_document, target)
    assert copy["state"] == "present", "control: the remote answered for the branch"
    assert copy["sha"] not in {item["sha"] for item in local["branches"] if item["name"] == "ahead"}, (
        "control: no local copy holds the remote tip"
    )
    assert entry["remote_verdict"] is not None, "control: the accepted document stores a verdict"
    assert models.parse_checkouts(json.dumps(status_document))

    def dropped(document):
        document["repositories"][0]["package_branches"][entry_index]["remote_verdict"] = None

    assert (
        f"repositories[0].package_branches[{entry_index}].remote_verdict "
        "is required for a remote tip no local copy holds"
    ) in reference_mutation(status_document, dropped)


def test_the_parser_refuses_a_remote_verdict_without_a_present_remote_copy(status_document):
    (repository,) = status_document["repositories"]
    entry = branch_entry_of(repository, "zero")
    entry_index = repository["package_branches"].index(entry)
    assert entry["remote"]["state"] == "absent", "control: the branch was never pushed"
    assert entry["remote_verdict"] is None, "control: the accepted document stores no verdict"
    assert models.parse_checkouts(json.dumps(status_document))

    def stored(document):
        document["repositories"][0]["package_branches"][entry_index]["remote_verdict"] = dict(
            MERGED_ANCESTOR
        )

    assert (
        f"repositories[0].package_branches[{entry_index}].remote_verdict "
        "requires a present remote copy"
    ) in reference_mutation(status_document, stored)


def test_the_parser_refuses_a_remote_verdict_on_an_unknown_remote_copy(collected, collected_layout):
    repository = repository_of(collected, collected_layout["a"])
    entry = branch_entry_of(repository, "topic")
    index = collected["repositories"].index(repository)
    entry_index = repository["package_branches"].index(entry)
    assert entry["remote"]["state"] == "unknown", "control: no answer names a tip"
    assert entry["remote_verdict"] is None, "control: the accepted document stores no verdict"

    def stored(document):
        document["repositories"][index]["package_branches"][entry_index]["remote_verdict"] = dict(
            MERGED_ANCESTOR
        )

    assert (
        f"repositories[{index}].package_branches[{entry_index}].remote_verdict "
        "requires a present remote copy"
    ) in reference_mutation(collected, stored)


def test_the_parser_refuses_a_remote_verdict_in_a_repository_without_a_remote(
    collected, collected_layout
):
    repository = repository_of(collected, collected_layout["d"])
    entry = branch_entry_of(repository, "topic")
    index = collected["repositories"].index(repository)
    entry_index = repository["package_branches"].index(entry)
    assert repository["remote"] is None and entry["remote"] is None, "control: no remote"
    assert entry["remote_verdict"] is None, "control: the accepted document stores no verdict"
    assert models.parse_checkouts(json.dumps(collected))

    def stored(document):
        document["repositories"][index]["package_branches"][entry_index]["remote_verdict"] = dict(
            MERGED_ANCESTOR
        )

    assert (
        f"repositories[{index}].package_branches[{entry_index}].remote_verdict requires a remote"
    ) in reference_mutation(collected, stored)


def test_the_parser_refuses_a_remote_only_reason_on_a_local_branch_verdict(
    collected, collected_layout
):
    """The two reasons only a remote verdict may carry are refused on a local copy's
    verdict, which a clone without a base leaves ``unknown``/``no_base``."""
    repository = repository_of(collected, collected_layout["d"])
    index = collected["repositories"].index(repository)
    (clone_document,) = repository["clones"]
    branch_index = next(
        position
        for position, item in enumerate(clone_document["branches"])
        if item["name"] == "topic"
    )
    assert clone_document["branches"][branch_index]["verdict"] == remote_unknown("no_base"), (
        "control: a clone with no verdict base"
    )
    assert models.parse_checkouts(json.dumps(collected))

    for reason in ("objects_missing", "ambiguous_tip"):

        def changed(document, reason=reason):
            document["repositories"][index]["clones"][0]["branches"][branch_index]["verdict"][
                "reason"
            ] = reason

        assert (
            f"repositories[{index}].clones[0].branches[{branch_index}].verdict.reason is invalid"
            in reference_mutation(collected, changed)
        ), reason


def test_the_parser_refuses_a_remote_only_reason_on_a_worktree_verdict(tmp_path):
    """A detached checkout of a clone without a base carries its own verdict, and the two
    remote-only reasons are refused there too."""
    folder = tmp_path / "folder"
    folder.mkdir()
    solo = folder / "solo"
    git(folder, "init", "-q", "-b", "trunk", str(solo))
    commit_file(solo, "one.txt", "one\n", "one")
    git(solo, "branch", "work")
    linked(solo, tmp_path, "detached", "--detach", "work")
    document = collect_packages([folder], None)
    (repository,) = document["repositories"]
    (clone_document,) = repository["clones"]
    assert clone_document["verdict_base"] is None, "control: no remote gives no base"
    worktree_index = next(
        position
        for position, item in enumerate(clone_document["worktrees"])
        if not item["main"]
    )
    assert clone_document["worktrees"][worktree_index]["verdict"] == remote_unknown("no_base")
    assert models.parse_checkouts(json.dumps(document))

    for reason in ("objects_missing", "ambiguous_tip"):

        def changed(document, reason=reason):
            document["repositories"][0]["clones"][0]["worktrees"][worktree_index]["verdict"][
                "reason"
            ] = reason

        assert (
            f"repositories[0].clones[0].worktrees[{worktree_index}].verdict.reason is invalid"
            in reference_mutation(document, changed)
        ), reason


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


# ---------------------------------------------------------------------------
# World W: one real-git world shared by the base-drift and attention proofs
# ---------------------------------------------------------------------------

WORLD_SHIPPED = frozenset({"drift-finished", "on-c", "done-a", "done-b", "shared-a"})
WORLD_ABSENT = "0" * 40


class WorldW:
    """The layout of world W: repositories, commits and the four collections."""

    folder: Path
    root: Path
    c: dict[str, Path]
    sha: dict[str, str]
    cache: Path
    offline: dict[str, Any]
    live: dict[str, Any]
    budget: dict[str, Any]
    expired: dict[str, Any]
    calls: list[dict[str, Any]]


def world_package(document: dict[str, Any], name: str) -> dict[str, Any]:
    stage = "Shipped" if name in WORLD_SHIPPED else "Drafts"
    (found,) = [
        p for p in document["packages"] if p["package_path"] == f"Fictional/{stage}/{name}"
    ]
    return found


def build_world_w(base: Path) -> WorldW:
    w = WorldW()
    served = base / "served"
    served.mkdir()
    w.folder = base / "folder"
    w.folder.mkdir()
    remote = make_remote(served)
    c: dict[str, Path] = {}
    sha: dict[str, str] = {}
    sha["O"] = git(remote, "rev-parse", "trunk").stdout.strip()
    c["A"] = clone(remote, w.folder / "Repo Clone 1")
    c["B"] = clone(remote, w.folder / "Repo Clone 2")
    sha["X"] = commit_file(c["B"], "x.txt", "x\n", "X")
    git(c["B"], "push", "-q", "origin", "trunk")
    git(c["B"], "checkout", "-q", "-b", "side")
    sha["X2"] = commit_file(c["B"], "x2.txt", "x2\n", "X2")
    git(c["B"], "checkout", "-q", "trunk")
    c["C"] = clone(remote, w.folder / "Repo Clone 3")
    sha["Y"] = commit_file(c["C"], "y.txt", "y\n", "Y")
    git(c["C"], "push", "-q", "origin", "trunk")
    c["D"] = clone(remote, w.folder / "Repo Clone 10")
    c["E"] = clone(remote, w.folder / "Repo Clone 4")
    sha["Z"] = commit_file(c["E"], "z.txt", "z\n", "Z")
    c["S"] = clone(remote.as_uri(), w.folder / "Repo Shallow", "--depth", "1")
    c["U"] = clone(remote, w.folder / "Repo Crowded")
    import_branches(c["U"], models.CHECKOUT_BRANCH_CAP + 1)
    c["L"] = w.folder / "Repo Local"
    c["L"].mkdir()
    git(c["L"], "init", "-q", "-b", "trunk")
    commit_file(c["L"], "l.txt", "l\n", "local")
    (w.folder / "Plain").mkdir()

    c["W"] = clone(remote, w.folder / "Repo Work")
    work = c["W"]
    git(work, "checkout", "-q", "-b", "topic", "trunk")
    commit_file(work, "t1.txt", "t1\n")
    git(work, "push", "-q", "origin", "topic")
    commit_file(work, "t2.txt", "t2\n")
    git(work, "branch", "helper", "topic~1")
    git(work, "branch", "landed", sha["X"])
    git(work, "branch", "spec/fresh", "trunk")
    git(work, "branch", "--track", "tracking", "origin/trunk")
    git(work, "checkout", "-q", "-b", "local-only", "trunk")
    commit_file(work, "lo.txt", "lo\n")
    for name in ("done", "shared"):
        git(work, "checkout", "-q", "-b", name, "trunk")
        commit_file(work, f"{name}.txt", f"{name}\n")
        git(work, "push", "-q", "origin", name)
    git(work, "checkout", "-q", "topic")
    (work / "file.txt").write_text("stashed\n")
    git(work, "stash", "push", "-q")
    c["wt"] = linked(work, base, "wt", "-b", "wt", "trunk")
    (c["wt"] / "untracked.txt").write_text("untracked\n")

    w.c, w.sha = c, sha
    w.root = base / "spec"
    outside = base / "outside" / "Nowhere"

    def declare(name: str, header: str) -> None:
        stage = "Shipped" if name in WORLD_SHIPPED else "Drafts"
        make_package(w.root, stage, name, header)

    def target(key: str) -> str:
        return f"Target repo: {c[key]}\n"

    for name, key, held in (
        ("drift-moved", "A", "X"),
        ("drift-twin", "A", "X"),
        ("drift-off", "A", "X2"),
        ("drift-equal", "A", "Y"),
        ("drift-held", "W", "X"),
        ("drift-shallow", "S", "X"),
        ("drift-unreadable", "U", "X"),
        ("drift-local", "L", "X"),
        ("drift-finished", "A", "X"),
    ):
        declare(name, target(key) + f"Baseline: {sha[held]}\n")
    declare("drift-unheld", target("A") + f"Baseline: {WORLD_ABSENT}\n")
    declare("baseline-invalid", target("A") + "Baseline: c3e197a\n")
    declare("branch-invalid", target("A") + "Branch: spec/x (proposed)\n")
    declare("default", target("A") + "Branch: trunk\n")
    declare("on-worktree", f"Target repo: {c['wt']}\n")
    declare("on-c", target("C"))
    declare("fresh", target("W") + "Branch: spec/fresh\n")
    for name, branch in (("done-a", "done"), ("done-b", "done"), ("shared-a", "shared"), ("shared-b", "shared")):
        declare(name, target("W") + f"Branch: {branch}\n")
    declare("missing-in", f"Target repo: {w.folder / 'Missing Clone'}\nBranch: spec/missing\n")
    declare("missing-na", f"Target repo: {w.folder / 'Gone Clone'}\nBranch: not applicable — moved\n")
    declare("missing-out", f"Target repo: {outside}\nBranch: spec/out\n")
    declare("plain-in", f"Target repo: {w.folder / 'Plain'}\nBranch: spec/plain\n")
    declare("relative", "Target repo: Minotaur\n")
    return w


class RecordedRuns:
    """What the recording replacement of ``_GitRunner.run`` saw of each call."""

    def __init__(self, patch: pytest.MonkeyPatch) -> None:
        self.calls: list[dict[str, Any]] = []
        self.active = False
        original = checkouts._GitRunner.run
        recorded = self

        def run(runner, arguments, **options):
            if recorded.active:
                arguments = list(arguments)
                attributes = [
                    a.split("=", 1)[1] for a in arguments if a.startswith("core.attributesFile=")
                ]
                recorded.calls.append(
                    {
                        "arguments": arguments,
                        "path": options.get("path"),
                        "overlay": dict(options.get("overlay") or {}),
                        "scratch_exists": [
                            os.path.isdir(os.path.dirname(value)) for value in attributes
                        ],
                    }
                )
            return original(runner, arguments, **options)

        patch.setattr(checkouts._GitRunner, "run", run)


@pytest.fixture(scope="module")
def world_w(tmp_path_factory):
    with shared_world_build(tmp_path_factory, "world-w") as (directory, home, patch):
        w = build_world_w(directory)
        w.cache = nyx_home(home)
        scan = ("Shipped",)
        w.offline = collect_packages([w.folder], w.root, scan, remote_checks=False)
        recorded = RecordedRuns(patch)
        recorded.active = True
        w.live = collect_packages([w.folder], w.root, scan, remote_checks=True)
        recorded.active = False
        w.calls = recorded.calls
        with pytest.MonkeyPatch.context() as spent:
            spent.setattr(checkouts, "VERDICT_RESERVE_NS", 60 * 10**9)
            w.budget = collect_packages(
                [w.folder], w.root, scan, remote_checks=True, seconds=60.0
            )
        w.expired = collect_packages([w.folder], w.root, scan, seconds=-1.0)
    return w


def drift_of(document: dict[str, Any], name: str) -> dict[str, Any] | None:
    return world_package(document, name)["baseline"]["drift"]


def lender_path(w: WorldW, key: str | None) -> str | None:
    return real(w.c[key]) if key is not None else None


def drift_row(w: WorldW, basis, relation, ahead, behind, lender) -> dict[str, Any]:
    return {
        "basis": basis,
        "relation": relation,
        "ahead": ahead,
        "behind": behind,
        "objects_from": lender_path(w, lender),
    }


def test_world_w_reports_each_baseline_and_its_live_drift_from_the_target_base(world_w):
    w = world_w
    document = w.live

    assert document["repositories"], "control: git ran"
    expected = {
        "drift-moved": ("live", "behind", 0, 1, "B"),
        "drift-twin": ("live", "behind", 0, 1, "B"),
        "drift-off": ("live", "diverged", 1, 1, "B"),
        "drift-equal": ("live", "equal", 0, 0, None),
        "drift-unheld": ("live", "differs", None, None, None),
        "drift-held": ("live", "behind", 0, 1, None),
        "drift-shallow": ("live", "differs", None, None, None),
        "drift-unreadable": ("cached", "absent", None, None, None),
        "drift-local": ("cached", "absent", None, None, None),
    }
    for name, row in expected.items():
        assert world_package(document, name)["baseline"]["drift"] == drift_row(w, *row), name
    for name in ("drift-finished", "baseline-invalid", "branch-invalid", "default", "fresh",
                 "on-worktree", "on-c", "missing-in", "plain-in", "relative"):
        assert drift_of(document, name) is None, name
    shas = {
        "drift-moved": ("declared", w.sha["X"]),
        "drift-twin": ("declared", w.sha["X"]),
        "drift-off": ("declared", w.sha["X2"]),
        "drift-equal": ("declared", w.sha["Y"]),
        "drift-unheld": ("declared", WORLD_ABSENT),
        "drift-held": ("declared", w.sha["X"]),
        "drift-finished": ("declared", w.sha["X"]),
        "baseline-invalid": ("invalid", None),
        "default": ("absent", None),
        "relative": ("absent", None),
    }
    for name, (status, value) in shas.items():
        baseline = world_package(document, name)["baseline"]
        assert (baseline["status"], baseline["sha"]) == (status, value), name
        assert set(baseline) == models._BASELINE_KEYS


def test_world_w_groups_the_target_clones_the_drift_proofs_rely_on(world_w):
    w = world_w
    document = w.live

    group = repository_of(document, w.c["A"])
    members = {c["path"]: c for c in group["clones"]}
    for key in ("B", "C", "D", "E", "S", "U", "W"):
        assert real(w.c[key]) in members, key
    assert real(w.c["L"]) not in members
    assert [Path(c["path"]).name for c in group["clones"][:5]] == [
        "Repo Clone 1", "Repo Clone 2", "Repo Clone 3", "Repo Clone 4", "Repo Clone 10"
    ]
    assert members[real(w.c["U"])]["status"] == "too_many_branches"
    assert members[real(w.c["S"])]["shallow"] is True
    base = clone_at(document, w.c["A"])["verdict_base"]
    assert (base["sha"], base["basis"], base["objects_from"]) == (
        w.sha["Y"], "live", real(w.c["C"])
    )
    assert clone_at(document, w.c["L"])["verdict_base"] is None
    assert repository_of(document, w.c["L"])["remote"] is None
    held = clone_at(document, w.c["W"])["verdict_base"]
    assert (held["sha"], held["objects_from"]) == (w.sha["Y"], None)


def test_world_w_offline_drift_is_counted_against_the_cached_base_of_the_target(world_w):
    w = world_w
    document = w.offline

    assert document["remote_checks"] == "off"
    assert clone_at(document, w.c["A"])["verdict_base"]["sha"] == w.sha["O"]
    expected = {
        "drift-moved": ("cached", "ahead", 1, 0, "B"),
        "drift-twin": ("cached", "ahead", 1, 0, "B"),
        "drift-off": ("cached", "ahead", 2, 0, "B"),
        "drift-equal": ("cached", "ahead", 2, 0, "C"),
        "drift-unheld": ("cached", "differs", None, None, None),
        "drift-held": ("cached", "behind", 0, 1, None),
    }
    for name, row in expected.items():
        assert drift_of(document, name) == drift_row(w, *row), name
    # the parser accepts the lender under the cached basis for drift
    assert models.parse_checkouts(json.dumps(redigest(doc_copy(document))))


def test_every_document_of_world_w_parses_and_comes_back_unchanged(world_w):
    for document in (world_w.offline, world_w.live, world_w.budget, world_w.expired):
        assert models.parse_checkouts(json.dumps(document))["packages"] == document["packages"]


def drift_calls(w: WorldW) -> list[dict[str, Any]]:
    """The recorded base-drift counts: a pinned ``rev-list --left-right --count`` from a
    declared Baseline to the verdict base of the clone it ran in."""
    baselines = {
        p["baseline"]["sha"] for p in w.live["packages"] if p["baseline"]["sha"] is not None
    }
    bases = {c["path"]: c["verdict_base"] for r in w.live["repositories"] for c in r["clones"]}
    targets = {
        p["target"]["clone"]
        for p in w.live["packages"]
        if p["baseline"]["sha"] is not None and p["target"]["clone"] is not None
    }
    found = []
    for call in w.calls:
        arguments = call["arguments"]
        if arguments[-3:-1] != ["--left-right", "--count"] or "rev-list" not in arguments:
            continue
        left, _, right = arguments[-1].partition("...")
        base = bases.get(real(call["path"]))
        if (
            real(call["path"]) in targets
            and left in baselines
            and base is not None
            and right == base["sha"]
            and left != right
        ):
            assert "--no-replace-objects" in arguments
            found.append(call)
    return found


def test_the_drift_counts_run_after_the_no_commit_calls_inside_the_scratch_directory(world_w):
    w = world_w
    calls = w.calls

    counts = drift_calls(w)
    reflogs = [
        index
        for index, call in enumerate(calls)
        if "reflog" in call["arguments"] and "show" in call["arguments"]
        and call["arguments"][-1].startswith("refs/heads/")
    ]
    assert any(
        calls[i]["arguments"][-1] == "refs/heads/spec/fresh"
        and real(calls[i]["path"]) == real(w.c["W"])
        for i in reflogs
    ), "control: the no-commit check ran for the fresh branch"
    first = min(calls.index(call) for call in counts)
    assert first > max(reflogs)
    for call in counts:
        assert call["scratch_exists"] == [True]
        arguments = call["arguments"]
        pairs = list(zip(arguments, arguments[1:]))
        assert ("-c", "core.quotePath=true") in pairs
        assert arguments[0] == "--no-replace-objects"
        assert set(call["overlay"]) - {"GIT_ALTERNATE_OBJECT_DIRECTORIES"} == {
            "GIT_GRAFT_FILE", "GIT_ATTR_NOSYSTEM", "GIT_NO_LAZY_FETCH"
        }
    listed = {c["path"] for r in w.live["repositories"] for c in r["clones"]}
    assert {real(call["path"]) for call in counts} <= listed


def test_one_count_runs_for_each_clone_and_baseline_with_the_alternates_of_both_lenders(world_w):
    w = world_w

    counts = drift_calls(w)

    seen = sorted(
        (call["path"], call["arguments"][-1].partition("...")[0]) for call in counts
    )
    assert seen == sorted(
        [
            (real(w.c["A"]), w.sha["X"]),
            (real(w.c["A"]), w.sha["X2"]),
            (real(w.c["W"]), w.sha["X"]),
        ]
    )
    objects = {key: os.path.join(real(w.c[key]), ".git", "objects") for key in "ABC"}

    def alternates(path: str, baseline: str) -> list[str]:
        (call,) = [
            c for c in counts
            if c["path"] == path and c["arguments"][-1].startswith(baseline)
        ]
        value = call["overlay"].get("GIT_ALTERNATE_OBJECT_DIRECTORIES")
        return [] if value is None else [os.path.realpath(p) for p in value.split(os.pathsep)]

    a = real(w.c["A"])
    assert sorted(alternates(a, w.sha["X2"])) == sorted([objects["B"], objects["C"]])
    assert sorted(alternates(a, w.sha["X"])) == sorted([objects["B"], objects["C"]])
    assert alternates(real(w.c["W"]), w.sha["X"]) == []


def test_a_spent_verdict_budget_leaves_the_counted_drift_unknown_and_the_rest_decided(world_w):
    w = world_w
    document = w.budget

    assert document["remote_checks"] == "on"
    assert clone_at(document, w.c["A"])["verdict_base"]["basis"] == "live", "control"
    unknown = {"basis": "live", "relation": "unknown", "ahead": None, "behind": None,
               "objects_from": None}
    for name in ("drift-moved", "drift-twin", "drift-off", "drift-unheld", "drift-held"):
        assert drift_of(document, name) == unknown, name
    assert drift_of(document, "drift-equal") == drift_row(w, "live", "equal", 0, 0, None)
    assert drift_of(document, "drift-shallow") == drift_row(w, "live", "differs", None, None, None)
    for name in ("drift-unreadable", "drift-local"):
        assert drift_of(document, name) == drift_row(w, "cached", "absent", None, None, None)
    assert "checkout_cache_write_failed" not in {d["code"] for d in document["diagnostics"]}


def test_an_expired_deadline_leaves_git_too_old_and_every_drift_null(world_w):
    w = world_w
    document = w.expired

    assert document["git"] == {"status": "too_old", "version": None}
    assert document["repositories"] == []
    assert len(document["packages"]) == len(w.live["packages"])
    for package in document["packages"]:
        live = next(p for p in w.live["packages"] if p["package_path"] == package["package_path"])
        assert package["baseline"] == {**live["baseline"], "drift": None}
        assert package["target"]["status"] == (
            "invalid" if package["package_path"].endswith("relative") else "unchecked"
        )
        assert package["target"]["within_folders"] is None


def test_the_collections_of_world_w_leave_the_cache_in_its_format(world_w):
    w = world_w

    saved = json.loads(w.cache.read_text())

    assert saved["schema_version"] == 3
    assert set(saved) == {"schema_version", "verdicts", "remote_checks"}


def mutate_baseline(name: str, **changes: Any):
    def change(document: dict[str, Any], _w: WorldW) -> None:
        world_package(document, name)["baseline"].update(changes)

    return change


def mutate_drift(name: str, **changes: Any):
    def change(document: dict[str, Any], _w: WorldW) -> None:
        world_package(document, name)["baseline"]["drift"].update(changes)

    return change


def give_drift(name: str, donor: str):
    def change(document: dict[str, Any], _w: WorldW) -> None:
        world_package(document, name)["baseline"]["drift"] = doc_copy(drift_of(document, donor))

    return change


def drop_drift(name: str):
    def change(document: dict[str, Any], _w: WorldW) -> None:
        world_package(document, name)["baseline"]["drift"] = None

    return change


def lend_from(name: str, key: str):
    """Name a world clone (by key) or, for an unknown key, that path as the lender."""

    def change(document: dict[str, Any], w: WorldW) -> None:
        world_package(document, name)["baseline"]["drift"]["objects_from"] = (
            real(w.c[key]) if key in w.c else key
        )

    return change


def test_the_parser_refuses_a_baseline_or_drift_the_collected_facts_contradict(world_w):
    w = world_w
    assert models.parse_checkouts(json.dumps(redigest(doc_copy(w.live)))), "control"
    absent = {"relation": "absent", "ahead": None, "behind": None, "objects_from": None}
    equal = {"relation": "equal", "ahead": 0, "behind": 0, "objects_from": None}
    behind = {"relation": "behind", "ahead": 0, "behind": 1, "objects_from": None}
    refused = {
        "sha on an invalid baseline": (
            mutate_baseline("baseline-invalid", sha=w.sha["X"]), "baseline.sha"),
        "null sha on a declared baseline": (
            mutate_baseline("drift-moved", sha=None), "baseline.sha"),
        "an upper-case sha on a declared baseline": (
            mutate_baseline("drift-moved", sha=w.sha["X"].upper()), "baseline.sha"),
        "a short all-zero sha on a declared baseline": (
            mutate_baseline("drift-moved", sha="0" * 39), "baseline.sha"),
        "drift on a finished package": (
            give_drift("drift-finished", "drift-moved"), "baseline.drift"),
        "null drift on an unfinished declared package": (
            drop_drift("drift-moved"), "baseline.drift"),
        "basis unlike the base's": (
            mutate_drift("drift-moved", basis="cached"), "baseline.drift.basis"),
        "absent with a base": (mutate_drift("drift-moved", **absent), "baseline.drift.relation"),
        "counted without a base": (mutate_drift("drift-local", **behind), "baseline.drift.relation"),
        "equal below a different base": (
            mutate_drift("drift-moved", **equal), "baseline.drift.relation"),
        "behind on the base itself": (
            mutate_drift("drift-equal", **behind), "baseline.drift.relation"),
        "counts that contradict the relation": (
            mutate_drift("drift-moved", ahead=3), "baseline.drift counts contradict"),
        "a lender that is the target itself": (
            lend_from("drift-moved", "A"), "baseline.drift.objects_from"),
        "a lender of another repository": (
            lend_from("drift-moved", "L"), "baseline.drift.objects_from"),
        "a lender that is not ok": (
            lend_from("drift-moved", "U"), "baseline.drift.objects_from"),
        "a lender that is no clone": (
            lend_from("drift-moved", str(w.folder / "Nowhere")), "baseline.drift.objects_from"),
    }
    for label, (change, fragment) in refused.items():
        document = doc_copy(w.live)
        change(document, w)
        message = refusal_of(document)
        assert message.startswith("packages[") and fragment in message, (label, message)


def test_the_parser_names_the_package_whose_baseline_it_refuses(world_w):
    w = world_w
    document = doc_copy(w.live)
    mutate_baseline("drift-off", sha=None)(document, w)

    index = document["packages"].index(world_package(document, "drift-off"))

    assert refusal_of(document).startswith(f"packages[{index}].baseline.sha")


def test_the_lender_of_a_branch_comparison_still_needs_a_live_or_recorded_basis(world_w):
    w = world_w
    document = doc_copy(w.offline)
    clone_document = clone_at(document, w.c["W"])
    branch = next(b for b in clone_document["branches"] if b["vs_remote"] is not None)
    assert branch["vs_remote"]["basis"] == "cached", "control"

    branch["vs_remote"]["objects_from"] = real(w.c["B"])

    assert "objects_from requires the live or recorded basis" in refusal_of(document)


def test_every_drift_probe_is_pinned_and_each_clone_is_asked_once(world_w):
    """The presence probe and the lender search of the drift phase carry the verdict
    settings, ask each clone once, and no call runs where drift is decided without git."""
    w = world_w
    calls = w.calls
    last_reflog = max(
        index
        for index, call in enumerate(calls)
        if "reflog" in call["arguments"] and call["arguments"][-1].startswith("refs/heads/")
    )
    phase = calls[last_reflog + 1 :]
    probes = [
        call for call in phase
        if "--no-walk" in call["arguments"] and "--ignore-missing" in call["arguments"]
    ]
    listed = {c["path"] for r in w.live["repositories"] for c in r["clones"]}

    by_clone: dict[str, list[list[str]]] = {}
    for call in probes:
        asked = sorted(item for item in call["arguments"] if len(item) == 40 and "=" not in item)
        by_clone.setdefault(real(call["path"]), []).append(asked)
    a_baselines = sorted([w.sha["X"], w.sha["X2"], WORLD_ABSENT])
    assert by_clone[real(w.c["A"])] == [a_baselines], "one probe for A's distinct Baselines"
    assert by_clone[real(w.c["B"])] == [a_baselines], "one lender probe in B"
    assert by_clone[real(w.c["C"])] == [a_baselines], "the unheld one reaches C once"
    # W is probed for its own Baseline and, in A's search, as a lender; the jobs run
    # in parallel, so the two probes come in either order.
    assert sorted(by_clone[real(w.c["W"])]) == sorted([[w.sha["X"]], a_baselines])
    for call in phase:
        arguments = call["arguments"]
        assert "rev-list" in arguments and real(call["path"]) in listed, arguments
        assert arguments[0] == "--no-replace-objects", arguments
        assert ("-c", "core.quotePath=true") in list(zip(arguments, arguments[1:]))
        assert call["scratch_exists"] == [True]
        assert set(call["overlay"]) - {"GIT_ALTERNATE_OBJECT_DIRECTORIES"} == {
            "GIT_GRAFT_FILE", "GIT_ATTR_NOSYSTEM", "GIT_NO_LAZY_FETCH"
        }
        assert real(call["path"]) not in {real(w.c[key]) for key in ("S", "U", "L")}


# ---------------------------------------------------------------------------
# Availability and ranking
# ---------------------------------------------------------------------------


def availability_of(document: dict[str, Any], path: Path | str) -> dict[str, Any]:
    return clone_at(document, path)["availability"]


def free_names(document: dict[str, Any], key_path: Path | str) -> list[str]:
    return [Path(p).name for p in repository_of(document, key_path)["free_clones"]]


def world_availability(w: WorldW, document: dict[str, Any]) -> None:
    state = {key: availability_of(document, w.c[key])["state"] for key in "ACWSUL"}
    assert state == {
        "A": "assigned", "C": "free", "W": "occupied", "S": "assigned", "U": "unknown",
        "L": "unknown",
    }
    unfinished_a = sorted(
        f"Fictional/Drafts/{name}"
        for name in (
            "drift-moved", "drift-twin", "drift-off", "drift-equal", "drift-unheld",
            "baseline-invalid", "branch-invalid", "default",
        )
    )
    assert availability_of(document, w.c["A"]) == {
        "state": "assigned",
        "reasons": [],
        "assigned_to": [{"package_path": p, "basis": "target_repo"} for p in unfinished_a],
    }
    assert availability_of(document, w.c["C"]) == {
        "state": "free", "reasons": [], "assigned_to": []
    }
    assert availability_of(document, w.c["W"]) == {
        "state": "occupied",
        "reasons": ["not_on_default_branch", "linked_worktrees"],
        "assigned_to": [
            {"package_path": "Fictional/Drafts/drift-held", "basis": "target_repo"},
            {"package_path": "Fictional/Drafts/fresh", "basis": "target_repo"},
            {"package_path": "Fictional/Drafts/on-worktree", "basis": "target_repo_worktree"},
            {"package_path": "Fictional/Drafts/shared-b", "basis": "target_repo"},
        ],
    }
    for key in "UL":
        assert availability_of(document, w.c[key])["reasons"] == [], key
    assert availability_of(document, w.c["S"])["state"] == "assigned"


def test_world_w_live_clones_are_free_assigned_occupied_or_unknown(world_w):
    w = world_w
    world_availability(w, w.live)
    # the finished package names C, and C is still free
    assert world_package(w.live, "on-c")["target"]["clone"] == real(w.c["C"])


def test_world_w_ranks_its_free_clones_by_the_distance_of_their_default_branch(world_w):
    w = world_w
    live = repository_of(w.live, w.c["A"])
    assert [Path(p).name for p in live["free_clones"]] == [
        "Repo Clone 3", "Repo Clone 10", "Repo Clone 2", "Repo Clone 4"
    ]
    expected = {
        "Repo Clone 3": ("equal", 0, 0), "Repo Clone 10": ("equal", 0, 0),
        "Repo Clone 2": ("behind", 0, 1), "Repo Clone 4": ("ahead", 1, 0),
    }
    for item in live["clones"]:
        name = Path(item["path"]).name
        if name in expected:
            distance = item["local_default"]["vs_remote"]
            assert (distance["relation"], distance["ahead"], distance["behind"]) == expected[name]
    assert w.live["repositories"][-1]["free_clones"] == []

    world_availability(w, w.offline)
    offline = repository_of(w.offline, w.c["A"])
    assert [Path(p).name for p in offline["free_clones"]] == [
        "Repo Clone 2", "Repo Clone 3", "Repo Clone 10", "Repo Clone 4"
    ]
    cached = {
        Path(item["path"]).name: item["local_default"]["vs_cached"]["relation"]
        for item in offline["clones"]
        if item["path"] in offline["free_clones"]
    }
    assert cached == {
        "Repo Clone 2": "equal", "Repo Clone 3": "equal", "Repo Clone 10": "equal",
        "Repo Clone 4": "ahead",
    }
    assert all(r["free_clones"] == [] for r in w.offline["repositories"] if r["remote"] is None)


def main_of(clone_document: dict[str, Any]) -> dict[str, Any]:
    (found,) = [item for item in clone_document["worktrees"] if item["main"]]
    return found


def off_branch(clone_document: dict[str, Any]) -> None:
    main_of(clone_document)["head"]["branch"] = "elsewhere"


def detach(clone_document: dict[str, Any]) -> None:
    main = main_of(clone_document)
    main["head"]["branch"] = None
    main["verdict"] = {"state": "merged", "rung": "ancestor", "reason": None, "pull_request": None}


def add_linked(clone_document: dict[str, Any]) -> None:
    clone_document["worktrees"].append(
        {
            "path": clone_document["path"] + "-linked", "main": False, "head": None,
            "locked": False, "prunable": True, "status": "failed", "changes": None,
            "operations": None, "index_lock": None, "verdict": None, "removable": None,
        }
    )


def dirty(clone_document: dict[str, Any]) -> None:
    main_of(clone_document)["changes"]["untracked"] = 1


def mid_operation(clone_document: dict[str, Any]) -> None:
    main_of(clone_document)["operations"] = ["merge"]


def unreadable_main(clone_document: dict[str, Any]) -> None:
    main = main_of(clone_document)
    main.update(status="failed", head=None, changes=None, operations=None, index_lock=None, verdict=None)


def unreadable_clone(clone_document: dict[str, Any]) -> None:
    clone_document.update(
        status="failed", bare=None, partial_clone=None, shallow=None, cached_default=None,
        local_default=None, cached_vs_remote=None, stash_count=None, worktrees=None,
        branches=None, verdict_base=None, containment=None, removable_count=None,
    )


def no_default_name(clone_document: dict[str, Any]) -> None:
    clone_document["verdict_base"] = None
    clone_document["local_default"] = None


def apply_all(*changes):
    def change(clone_document: dict[str, Any]) -> None:
        for each in changes:
            each(clone_document)

    return change


ALL_REASONS = ["not_on_default_branch", "linked_worktrees", "uncommitted_changes", "operation_in_progress"]

# (label, change to clone C, state, reasons, whether the changed document is otherwise valid)
AVAILABILITY_ROWS = (
    ("not on the default branch", off_branch, "occupied", ["not_on_default_branch"], True),
    ("detached head", detach, "occupied", ["detached_head"], True),
    ("linked worktree", add_linked, "occupied", ["linked_worktrees"], True),
    ("uncommitted changes", dirty, "occupied", ["uncommitted_changes"], True),
    ("operation in progress", mid_operation, "occupied", ["operation_in_progress"], True),
    (
        "every reason",
        apply_all(off_branch, add_linked, dirty, mid_operation),
        "occupied", ALL_REASONS, True,
    ),
    (
        "every reason with a detached head",
        apply_all(detach, add_linked, dirty, mid_operation),
        "occupied",
        ["detached_head", "linked_worktrees", "uncommitted_changes", "operation_in_progress"],
        True,
    ),
    ("main worktree not ok", unreadable_main, "unknown", [], True),
    ("clone not ok", unreadable_clone, "unknown", [], True),
    (
        "main worktree not ok beside a reason",
        apply_all(add_linked, unreadable_main), "unknown", [], True,
    ),
    ("no default name on a non-bare clone", no_default_name, "unknown", [], False),
    (
        "no default name beside a dirty tree",
        apply_all(no_default_name, dirty), "occupied", ["uncommitted_changes"], False,
    ),
    (
        "no default name beside a linked worktree",
        apply_all(no_default_name, add_linked), "occupied", ["linked_worktrees"], False,
    ),
)


def clone_c(document: dict[str, Any], w: WorldW) -> dict[str, Any]:
    return clone_at(document, w.c["C"])


def test_each_reason_for_a_busy_clone_is_derived_alone_and_in_order(world_w):
    w = world_w
    for label, change, state, reasons, _valid in AVAILABILITY_ROWS:
        document = doc_copy(w.live)
        change(clone_c(document, w))
        derived = models.derive_attention(document)["availability"][real(w.c["C"])]
        assert (derived["state"], derived["reasons"]) == (state, reasons), label
        assert derived["assigned_to"] == [], label


def test_a_derived_availability_parses_and_an_underived_one_is_refused(world_w):
    w = world_w
    for label, change, _state, _reasons, valid in AVAILABILITY_ROWS:
        if not valid:
            continue
        document = doc_copy(w.live)
        change(clone_c(document, w))
        assert "contradicts" in refusal_of(doc_copy(document)), label
        assert models.parse_checkouts(json.dumps(redigest(rederive(document)))), label


def test_a_bare_clone_is_decided_by_its_linked_worktrees_alone(world_w):
    w = world_w
    document = doc_copy(w.live)
    bare = clone_c(document, w)
    bare.update(bare=True, verdict_base=None, local_default=None)
    bare["worktrees"] = []
    assert models.derive_attention(document)["availability"][bare["path"]]["state"] == "free"
    world_package(document, "default")["target"]["clone"] = bare["path"]
    assigned = models.derive_attention(document)["availability"][bare["path"]]
    assert (assigned["state"], assigned["reasons"]) == ("assigned", [])
    add_linked(bare)
    derived = models.derive_attention(document)["availability"][bare["path"]]
    assert (derived["state"], derived["reasons"]) == ("occupied", ["linked_worktrees"])


def test_an_unavailable_scan_makes_every_clone_unknown_and_an_incomplete_one_does_not(world_w):
    w = world_w
    document = doc_copy(w.live)
    document["package_scan"]["status"] = "unavailable"
    document["packages"] = []
    derived = models.derive_attention(document)
    assert {a["state"] for a in derived["availability"].values()} == {"unknown"}
    assert {tuple(a["reasons"]) for a in derived["availability"].values()} == {()}
    assert all(free == [] for free in derived["free_clones"])

    derived_live = models.derive_attention(w.live)
    incomplete = doc_copy(w.live)
    incomplete["package_scan"]["status"] = "incomplete"
    assert models.derive_attention(incomplete)["availability"] == derived_live["availability"]
    assert models.derive_attention(incomplete)["free_clones"] == derived_live["free_clones"]
    incomplete = rederive(incomplete)
    assert models.parse_checkouts(json.dumps(redigest(incomplete)))


def test_only_an_unfinished_package_assigns_a_clone(world_w):
    w = world_w
    document = doc_copy(w.live)
    package = world_package(document, "on-c")
    assert package["finished"] is True
    assert availability_of(document, w.c["C"])["state"] == "free"

    package["finished"] = False
    derived = models.derive_attention(document)["availability"][real(w.c["C"])]
    assert derived == {
        "state": "assigned",
        "reasons": [],
        "assigned_to": [{"package_path": package["package_path"], "basis": "target_repo"}],
    }
    assert "contradicts" in refusal_of(doc_copy(document))
    assert models.parse_checkouts(json.dumps(redigest(rederive(document))))

    started = doc_copy(w.live)
    world_package(started, "drift-finished")["finished"] = False
    names = [
        a["package_path"]
        for a in models.derive_attention(started)["availability"][real(w.c["A"])]["assigned_to"]
    ]
    assert "Fictional/Shipped/drift-finished" in names


def extra_free_clones(document: dict[str, Any], w: WorldW, count: int) -> list[str]:
    group = repository_of(document, w.c["A"])
    template = clone_c(document, w)
    for number in range(1, count + 1):
        copy = doc_copy(template)
        copy["path"] = template["path"].replace("Repo Clone 3", f"Repo Extra {number}")
        copy["worktrees"][0]["path"] = copy["path"]
        group["clones"].append(copy)
    return [real(w.folder / f"Repo Extra {number}") for number in range(1, count + 1)]


def set_distance(clone_document: dict[str, Any], spec: Any) -> None:
    local = clone_document["local_default"]
    if spec == "no default":
        clone_document["local_default"] = None
        return
    if spec[0] == "cached":
        local["vs_remote"] = None
        local["vs_cached"].update(
            relation=spec[1], ahead=spec[2], behind=spec[3], objects_from=None
        )
        return
    relation, ahead, behind = spec
    local["vs_remote"] = {
        "basis": "live", "relation": relation, "ahead": ahead, "behind": behind,
        "objects_from": None,
    }


RANKING_ROWS = (
    (
        "every relation in turn",
        {
            "Repo Clone 2": ("absent", None, None), "Repo Clone 3": ("unknown", None, None),
            "Repo Clone 4": ("differs", None, None), "Repo Clone 10": ("diverged", 1, 1),
            "Repo Extra 1": ("ahead", 1, 0), "Repo Extra 2": ("behind", 0, 1),
            "Repo Extra 3": ("equal", 0, 0),
        },
        ["Repo Extra 3", "Repo Extra 2", "Repo Extra 1", "Repo Clone 10", "Repo Clone 4",
         "Repo Clone 3", "Repo Clone 2"],
    ),
    (
        "commits apart add up, a missing count is last",
        {
            "Repo Clone 2": ("diverged", 3, 0), "Repo Clone 3": ("diverged", 2, 2),
            "Repo Clone 4": ("diverged", 0, 3), "Repo Clone 10": ("diverged", None, None),
        },
        ["Repo Clone 2", "Repo Clone 4", "Repo Clone 3", "Repo Clone 10"],
    ),
    (
        "a tie falls to natural path order",
        {name: ("equal", 0, 0) for name in ("Repo Clone 2", "Repo Clone 3", "Repo Clone 4", "Repo Clone 10")},
        ["Repo Clone 2", "Repo Clone 3", "Repo Clone 4", "Repo Clone 10"],
    ),
    (
        "the cached comparison stands in for a missing remote one",
        {
            "Repo Clone 2": ("behind", 0, 1), "Repo Clone 3": ("cached", "equal", 0, 0),
            "Repo Clone 4": ("cached", "absent", None, None), "Repo Clone 10": ("ahead", 1, 0),
        },
        ["Repo Clone 3", "Repo Clone 2", "Repo Clone 10", "Repo Clone 4"],
    ),
    (
        "no local default branch counts as absent",
        {
            "Repo Clone 2": "no default", "Repo Clone 3": ("unknown", None, None),
            "Repo Clone 4": ("equal", 0, 0), "Repo Clone 10": ("equal", 0, 0),
        },
        ["Repo Clone 4", "Repo Clone 10", "Repo Clone 3", "Repo Clone 2"],
    ),
)


def test_free_clones_are_ranked_by_relation_distance_and_path(world_w):
    w = world_w
    for label, distances, expected in RANKING_ROWS:
        document = doc_copy(w.live)
        extra_free_clones(document, w, 3 if "Repo Extra 1" in distances else 0)
        group = repository_of(document, w.c["A"])
        by_name = {Path(item["path"]).name: item for item in group["clones"]}
        for name, spec in distances.items():
            set_distance(by_name[name], spec)
        index = document["repositories"].index(group)
        names = [Path(p).name for p in models.derive_attention(document)["free_clones"][index]]
        assert names == expected, label


def test_a_reranked_group_parses_only_when_its_free_clones_were_rederived(world_w):
    w = world_w
    document = doc_copy(w.live)
    set_distance(clone_at(document, w.c["B"]), ("equal", 0, 0))
    assert free_names(document, w.c["A"]) == [
        "Repo Clone 3", "Repo Clone 10", "Repo Clone 2", "Repo Clone 4"
    ]
    assert "free_clones" in refusal_of(doc_copy(document))
    parsed = models.parse_checkouts(json.dumps(redigest(rederive(document))))
    assert free_names(parsed, w.c["A"]) == [
        "Repo Clone 2", "Repo Clone 3", "Repo Clone 10", "Repo Clone 4"
    ]


def test_the_parser_refuses_availability_and_free_clones_the_facts_contradict(world_w):
    w = world_w
    assert models.parse_checkouts(json.dumps(redigest(doc_copy(w.live)))), "control"
    c_path = real(w.c["C"])

    def reasoned(document):
        availability_of(document, w.c["C"])["reasons"] = ["linked_worktrees"]

    def finished_assigned(document):
        availability_of(document, w.c["C"])["assigned_to"] = [
            {"package_path": world_package(document, "on-c")["package_path"], "basis": "target_repo"}
        ]

    def state_changed(document):
        availability_of(document, w.c["C"])["state"] = "assigned"

    def basis_changed(document):
        availability_of(document, w.c["W"])["assigned_to"][2]["basis"] = "target_repo"

    def missing_clone(document):
        repository_of(document, w.c["A"])["free_clones"].remove(c_path)

    def out_of_order(document):
        repository_of(document, w.c["A"])["free_clones"].reverse()

    def extra_clone(document):
        repository_of(document, w.c["A"])["free_clones"].append(real(w.c["W"]))

    def unknown_key(document):
        availability_of(document, w.c["C"])["extra"] = 1

    def missing_key(document):
        del clone_c(document, w)["availability"]

    def missing_free(document):
        del repository_of(document, w.c["A"])["free_clones"]

    refused = {
        "a free clone with a reason": (reasoned, "availability"),
        "assigned_to naming a finished package": (finished_assigned, "availability"),
        "a state the facts do not give": (state_changed, "availability"),
        "a basis the target does not give": (basis_changed, "availability"),
        "free_clones missing a free clone": (missing_clone, "free_clones"),
        "free_clones out of order": (out_of_order, "free_clones"),
        "free_clones naming an occupied clone": (extra_clone, "free_clones"),
        "an unknown availability key": (unknown_key, "availability"),
        "a missing availability": (missing_key, "invalid shape"),
        "a missing free_clones": (missing_free, "invalid shape"),
    }
    for label, (change, fragment) in refused.items():
        document = doc_copy(w.live)
        change(document)
        message = refusal_of(document)
        assert fragment in message, (label, message)


# ---------------------------------------------------------------------------
# The Needs attention list
# ---------------------------------------------------------------------------

ATTENTION_CODES = (
    "branch_unpushed", "branch_not_pushed", "default_branch_ahead", "stash",
    "uncommitted_changes", "awaiting_merge", "removable", "base_moved",
    "target_repo_missing", "target_repo_not_git", "branch_invalid", "upstream_mismatch",
    "never_fetched", "no_origin_remote", "checkout_unavailable",
    "finished_stages_not_configured", "package_scan_incomplete", "package_scan_unavailable",
)
ATTENTION_CLASSES = {
    **dict.fromkeys(ATTENTION_CODES[:5], "unique_work"),
    "awaiting_merge": "awaiting_merge",
    "removable": "cleanup_ready",
    "base_moved": "base_moved",
    **dict.fromkeys(ATTENTION_CODES[8:], "configuration"),
}


def attention_item(code: str, **fields: Any) -> dict[str, Any]:
    return {
        "class": ATTENTION_CLASSES[code], "code": code, "repository": None, "clone": None,
        "worktree": None, "branch": None, "packages": [], "count": None, "pull_request": None,
        **fields,
    }


def natural_text(text: str | None) -> list[Any]:
    if text is None:
        return []
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", text)]


def attention_order(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        ATTENTION_CODES.index(item["code"]), natural_text(item["clone"]),
        natural_text(item["worktree"]), natural_text(item["branch"]),
        item["packages"][0] if item["packages"] else "",
    )


def world_attention(w: WorldW, document: dict[str, Any]) -> list[dict[str, Any]]:
    """The items world W's live document is meant to raise, in the order they are listed."""
    key = repository_of(document, w.c["A"])["remote"]["key"]
    assert key is not None

    def at(code: str, clone: str, **fields: Any) -> dict[str, Any]:
        return attention_item(code, repository=key, clone=real(w.c[clone]), **fields)

    def package(name: str) -> str:
        return world_package(document, name)["package_path"]

    return [
        at("branch_unpushed", "W", branch="topic", count=1),
        at("branch_not_pushed", "B", branch="side"),
        at("branch_not_pushed", "W", branch="local-only"),
        at("default_branch_ahead", "E", branch="trunk", count=1),
        at("stash", "W", count=1),
        at("uncommitted_changes", "W", worktree=real(w.c["wt"]), count=1),
        attention_item(
            "awaiting_merge", repository=key, branch="done",
            packages=[package("done-a"), package("done-b")],
        ),
        at("removable", "W", count=2),
        at("base_moved", "A", packages=[package("drift-moved")], count=1),
        at("base_moved", "A", packages=[package("drift-twin")], count=1),
        at("base_moved", "W", packages=[package("drift-held")], count=1),
        attention_item("target_repo_missing", packages=[package("missing-in")]),
        attention_item("target_repo_not_git", packages=[package("plain-in")]),
        at("branch_invalid", "A", packages=[package("branch-invalid")]),
        at("upstream_mismatch", "W", branch="tracking"),
        *[at("never_fetched", name) for name in ("A", "B", "C", "E", "D", "S", "W")],
        attention_item("no_origin_remote", clone=real(w.c["L"])),
        at("checkout_unavailable", "U"),
    ]


def test_world_w_attention_lists_each_code_once_in_class_code_and_location_order(world_w):
    w = world_w
    expected = world_attention(w, w.live)
    assert w.live["attention"] == expected
    assert models.derive_attention(w.live)["attention"] == expected
    assert expected == sorted(expected, key=attention_order)
    assert "base_moved" not in [item["code"] for item in w.budget["attention"]]


def drop_where(items: list[dict[str, Any]], **match: Any) -> list[dict[str, Any]]:
    return [i for i in items if any(i[key] != value for key, value in match.items())]


def attention_after(
    base: list[dict[str, Any]], drop: tuple[dict[str, Any], ...] = (), add: tuple[dict[str, Any], ...] = ()
) -> list[dict[str, Any]]:
    kept = base
    for match in drop:
        kept = drop_where(kept, **match)
    return sorted([*kept, *add], key=attention_order)


def test_the_parser_refuses_an_attention_list_the_facts_contradict(world_w):
    w = world_w
    assert models.parse_checkouts(json.dumps(redigest(doc_copy(w.live)))), "control"

    def codes(document):
        return [i["code"] for i in document["attention"]]

    def dropped(document):
        document["attention"].pop(0)

    def dropped_awaiting(document):
        document["attention"].pop(codes(document).index("awaiting_merge"))

    def swapped(document):
        items = document["attention"]
        items[0], items[1] = items[1], items[0]

    def pulled(document):
        document["attention"][codes(document).index("stash")]["pull_request"] = 7

    def counted(document):
        document["attention"][0]["count"] += 1

    def miscounted(document):
        document["attention"][0]["count"] = True

    def nowhere(document):
        document["attention"][0]["clone"] = real(w.folder / "Nowhere")

    def added(document):
        document["attention"].append(doc_copy(document["attention"][-1]))

    def retyped(document):
        document["attention"] = {}

    for change in (
        dropped, dropped_awaiting, swapped, pulled, counted, miscounted, nowhere, added, retyped
    ):
        document = doc_copy(w.live)
        change(document)
        assert "attention" in refusal_of(document), change.__name__

    document = doc_copy(w.live)
    del document["attention"]
    assert "invalid shape" in refusal_of(document)


def derived_attention(document: dict[str, Any]) -> list[dict[str, Any]]:
    return models.derive_attention(document)["attention"]


def parses_rederived(document: dict[str, Any]) -> None:
    assert models.parse_checkouts(json.dumps(redigest(rederive(doc_copy(document)))))


def restate(document: dict[str, Any], names: tuple[str, ...], **changes: Any) -> None:
    for name in names:
        status = world_package(document, name)["code_status"]
        status.update(changes)
        status["text"] = models.code_status_text(status)


def test_a_pull_request_on_the_finished_branch_is_listed_and_an_unread_scan_lists_none(world_w):
    w = world_w
    base = world_attention(w, w.live)
    done = ("done-a", "done-b")
    awaiting = {"code": "awaiting_merge"}

    document = doc_copy(w.live)
    restate(document, done, state="pr_open", pull_request=7, push="pushed")
    (item,) = [i for i in base if i["code"] == "awaiting_merge"]
    expected = attention_after(base, (awaiting,), (dict(item, pull_request=7),))
    assert derived_attention(document) == expected
    parses_rederived(document)

    for form in ({}, {"state": "pr_open", "pull_request": 7, "push": "pushed"}):
        document = doc_copy(w.live)
        restate(document, done, **form)
        document["package_scan"]["status"] = "incomplete"
        expected = attention_after(base, (awaiting,), (attention_item("package_scan_incomplete"),))
        assert derived_attention(document) == expected, form
        assert derived_attention(document)[-1]["code"] == "package_scan_incomplete"
        parses_rederived(document)


def test_an_unset_finished_stage_policy_and_targets_outside_the_checkout_folders(world_w):
    w = world_w
    base = world_attention(w, w.live)

    document = doc_copy(w.live)
    document["package_scan"]["finished_policy"] = "not_configured"
    expected = attention_after(base, (), (attention_item("finished_stages_not_configured"),))
    assert derived_attention(document) == expected
    assert expected[-1]["code"] == "finished_stages_not_configured"
    assert expected[-2]["code"] == "checkout_unavailable"
    parses_rederived(document)

    missing = {"code": "target_repo_missing"}
    for label, changes in (
        ("outside the folders", {"status": "outside_folders", "within_folders": None}),
        ("invalid", {"status": "invalid", "within_folders": None}),
        ("missing but not in a checkout folder", {"within_folders": False}),
    ):
        document = doc_copy(w.live)
        world_package(document, "missing-in")["target"].update(changes)
        assert derived_attention(document) == attention_after(base, (missing,)), label
        parses_rederived(document)


def test_a_main_worktree_with_changes_or_only_an_operation_is_listed_with_its_own_path(world_w):
    w = world_w
    base = world_attention(w, w.live)
    c_path = real(w.c["C"])
    key = base[0]["repository"]
    for label, changes, operations, count in (
        ("changes", {"staged": 1, "unstaged": 1, "untracked": 0, "conflicted": 1}, [], 3),
        ("an operation only", {"staged": 0, "unstaged": 0, "untracked": 0, "conflicted": 0},
         ["rebase"], 0),
    ):
        document = doc_copy(w.live)
        main = main_of(clone_at(document, w.c["C"]))
        main["changes"], main["operations"] = changes, operations
        item = attention_item(
            "uncommitted_changes", repository=key, clone=c_path, worktree=c_path, count=count
        )
        expected = attention_after(base, (), (item,))
        assert derived_attention(document) == expected, label
        assert expected.index(item) == 5, label
        parses_rederived(document)


def test_a_linked_worktree_that_cannot_be_read_lists_nothing_and_leaves_its_clone_available(world_w):
    w = world_w
    base = world_attention(w, w.live)
    document = doc_copy(w.live)
    linked_worktree = next(i for i in clone_at(document, w.c["W"])["worktrees"] if not i["main"])
    linked_worktree.update(
        status="failed", head=None, changes=None, operations=None, index_lock=None, verdict=None
    )
    expected = attention_after(base, ({"code": "uncommitted_changes"},))
    after = derived_attention(rederive(doc_copy(document)))
    assert after == expected
    assert not [i for i in after if i["code"] == "checkout_unavailable" and i["clone"] == real(w.c["W"])]
    parses_rederived(document)


def named_branch(document: dict[str, Any], clone_key: Any, name: str) -> dict[str, Any]:
    (found,) = [b for b in clone_at(document, clone_key)["branches"] if b["name"] == name]
    return found


def compare(relation: str, ahead: int | None, behind: int | None) -> dict[str, Any]:
    return {"basis": "live", "relation": relation, "ahead": ahead, "behind": behind, "objects_from": None}


def fix_branch(name: str, **changes: Any):
    def change(document: dict[str, Any], w: WorldW) -> None:
        named_branch(document, w.c["W"], name).update(changes)

    return change


def fix_default(clone_key: str, vs_remote: Any, vs_cached: Any = None):
    def change(document: dict[str, Any], w: WorldW) -> None:
        local = clone_at(document, w.c[clone_key])["local_default"]
        local["vs_remote"] = vs_remote
        if vs_cached is not None:
            local["vs_cached"] = vs_cached

    return change


def fix_removable(**changes: Any):
    """Edit the Removable verdicts and references of the branches and worktree of W."""

    def change(document: dict[str, Any], w: WorldW) -> None:
        work = clone_at(document, w.c["W"])
        for name, state in changes.get("branches", {}).items():
            named_branch(document, w.c["W"], name)["removable"] = state
        for field, value in changes.get("worktree", {}).items():
            (linked_item,) = [i for i in work["worktrees"] if not i["main"]]
            linked_item[field] = value
        for name, packages in changes.get("references", {}).items():
            named_branch(document, w.c["W"], name)["packages"] = packages
        for name in changes.get("finished", ()):
            world_package(document, name)["finished"] = True

    return change


def reference(name: str, basis: str) -> dict[str, str]:
    return {"package_path": f"Fictional/Drafts/{name}", "basis": basis}


def free_to_remove() -> dict[str, Any]:
    return {"removable": True, "blockers": []}


def fix_package(name: str, **changes: Any):
    def change(document: dict[str, Any], w: WorldW) -> None:
        package = world_package(document, name)
        for field, value in changes.items():
            if isinstance(value, dict):
                package[field].update(value)
            else:
                package[field] = value

    return change


def unread_clone_b(document: dict[str, Any], w: WorldW) -> None:
    unreadable_clone(clone_at(document, w.c["B"]))


def linked_in_b(document: dict[str, Any], w: WorldW) -> None:
    (source,) = [i for i in clone_at(document, w.c["W"])["worktrees"] if not i["main"]]
    extra = doc_copy(source)
    extra["path"] = "/zz/linked-b"
    clone_at(document, w.c["B"])["worktrees"].append(extra)


def scan_is(status: str, policy: str):
    def change(document: dict[str, Any], w: WorldW) -> None:
        document["package_scan"].update(status=status, finished_policy=policy)

    return change


def key_items(w: WorldW, code: str, clone: str, **fields: Any) -> dict[str, Any]:
    key = repository_of(w.live, w.c["A"])["remote"]["key"]
    return attention_item(code, repository=key, clone=real(w.c[clone]), **fields)


# (label, change, items it removes, items it adds)
def attention_rows(w: WorldW) -> tuple[Any, ...]:
    drift_behind = {"drift": {"basis": "live", "relation": "behind", "ahead": 0, "behind": 2, "objects_from": None}}

    def drift(name: str, relation: str):
        return fix_package(name, baseline={"drift": {"relation": relation}})

    not_pushed = {"code": "branch_not_pushed", "branch": "local-only"}
    unpushed = {"code": "branch_unpushed", "branch": "topic"}
    ahead_default = {"code": "default_branch_ahead"}
    removable_w = {"code": "removable"}
    return (
        ("a branch ahead of its remote copy", fix_branch("local-only", vs_remote=compare("ahead", 4, 0)),
         (not_pushed,), (key_items(w, "branch_unpushed", "W", branch="local-only", count=4),)),
        ("a diverged branch", fix_branch("local-only", vs_remote=compare("diverged", 2, 1)),
         (not_pushed,), (key_items(w, "branch_unpushed", "W", branch="local-only", count=2),)),
        ("a branch level with its remote copy", fix_branch("local-only", vs_remote=compare("equal", 0, 0)),
         (not_pushed,), ()),
        ("a branch behind its remote copy", fix_branch("local-only", vs_remote=compare("behind", 0, 3)),
         (not_pushed,), ()),
        ("a branch with no comparison", fix_branch("local-only", vs_remote=None), (not_pushed,), ()),
        ("a merged branch", fix_branch("local-only", verdict={"state": "merged", "rung": "ancestor", "reason": None, "pull_request": None}),
         (not_pushed,), ()),
        ("a branch with no verdict still counts", fix_branch("local-only", verdict=None), (), ()),
        ("a contained branch", fix_branch("local-only", contained_in="topic"), (not_pushed,), ()),
        ("the verdict base", lambda d, w_: clone_at(d, w_.c["W"])["verdict_base"].update(name="local-only"),
         (not_pushed,), ()),
        ("a pushed branch that is merged", fix_branch("topic", verdict={"state": "merged", "rung": "ancestor", "reason": None, "pull_request": None}),
         (unpushed,), ()),
        ("a default branch diverged", fix_default("E", compare("diverged", 5, 2)),
         (ahead_default,), (key_items(w, "default_branch_ahead", "E", branch="trunk", count=5),)),
        ("a default branch behind", fix_default("E", compare("behind", 0, 1)), (ahead_default,), ()),
        ("a default branch level", fix_default("E", compare("equal", 0, 0)), (ahead_default,), ()),
        ("a default branch ahead by the cached comparison", fix_default("C", None, compare("ahead", 2, 0)),
         (), (key_items(w, "default_branch_ahead", "C", branch="trunk", count=2),)),
        ("a default branch with no comparison", lambda d, w_: clone_at(d, w_.c["E"]).update(local_default=None),
         (ahead_default,), ()),
        ("no stash", lambda d, w_: clone_at(d, w_.c["W"]).update(stash_count=0), ({"code": "stash"},), ()),
        ("three stashes", lambda d, w_: clone_at(d, w_.c["W"]).update(stash_count=3),
         ({"code": "stash"},), (key_items(w, "stash", "W", count=3),)),
        ("a fresh branch whose package is finished is counted",
         fix_removable(finished=("fresh",)), (removable_w,), (key_items(w, "removable", "W", count=3),)),
        ("a fresh branch held by a sibling copy is not counted",
         fix_removable(references={"spec/fresh": [reference("fresh", "sibling_clone")]}), (), ()),
        ("a fresh branch only contained by the package is counted",
         fix_removable(references={"spec/fresh": [reference("fresh", "contained")]}),
         (removable_w,), (key_items(w, "removable", "W", count=3),)),
        ("a removable linked worktree is counted",
         fix_removable(worktree={"removable": free_to_remove()}),
         (removable_w,), (key_items(w, "removable", "W", count=3),)),
        ("a removable linked worktree on a branch an unfinished package uses is not counted",
         fix_removable(worktree={"removable": free_to_remove()},
                       references={"wt": [reference("on-worktree", "target_clone")]}),
         (), ()),
        ("an unreadable linked worktree is held through the branch that lists it",
         fix_removable(worktree={"removable": free_to_remove(), "status": "failed", "head": None},
                       references={"wt": [reference("on-worktree", "target_clone")]}),
         ({"code": "uncommitted_changes"},), ()),
        ("nothing removable",
         fix_removable(branches={"landed": {"removable": False, "blockers": ["not_merged"]},
                                 "tracking": {"removable": False, "blockers": ["not_merged"]}}),
         (removable_w,), ()),
        ("a finished package on the branch no longer holds it",
         fix_package("done-b", finished=False), ({"code": "awaiting_merge"},), ()),
        ("a branch whose packages are merged",
         lambda d, w_: restate(d, ("done-a", "done-b"), state="merged"), ({"code": "awaiting_merge"},), ()),
        ("a branch whose packages are in progress",
         lambda d, w_: restate(d, ("done-a", "done-b"), state="in_progress"), ({"code": "awaiting_merge"},), ()),
        ("a finished package", fix_package("drift-moved", finished=True), ({"packages": ["Fictional/Drafts/drift-moved"]},), ()),
        *[
            (f"drift {relation}", drift("drift-moved", relation), ({"packages": ["Fictional/Drafts/drift-moved"], "code": "base_moved"},), ())
            for relation in ("equal", "ahead", "diverged", "differs", "unknown", "absent")
        ],
        ("a base moved on a linked worktree target and an invalid branch there",
         fix_package("on-worktree", baseline={"status": "declared", "sha": WORLD_ABSENT, **drift_behind},
                     branch={"status": "invalid", "name": "x"}),
         (),
         (key_items(w, "base_moved", "W", worktree=real(w.c["wt"]), packages=["Fictional/Drafts/on-worktree"], count=2),
          key_items(w, "branch_invalid", "W", worktree=real(w.c["wt"]), packages=["Fictional/Drafts/on-worktree"]))),
        ("a missing target of a finished package", fix_package("missing-in", finished=True),
         ({"code": "target_repo_missing"},), ()),
        ("a missing target with no applicable branch", fix_package("plain-in", branch={"status": "not_applicable", "name": None}),
         ({"code": "target_repo_not_git"},), ()),
        ("an invalid branch of a finished package", fix_package("branch-invalid", finished=True),
         ({"code": "branch_invalid"},), ()),
        ("a clone fetched since it was cloned",
         lambda d, w_: clone_at(d, w_.c["C"])["cached_default"].update(fetched_since_clone=True),
         ({"code": "never_fetched", "clone": real(w.c["C"])},), ()),
        ("a worktree of an earlier clone follows by clone before worktree", linked_in_b,
         (), (key_items(w, "uncommitted_changes", "B", worktree="/zz/linked-b", count=1),)),
        ("an invalid branch of a package with no clone target", fix_package("missing-in", branch={"status": "invalid", "name": "x"}),
         (), ()),
        ("an unavailable scan without the finished-stage setting",
         scan_is("unavailable", "not_configured"),
         ({"code": "awaiting_merge"},), (attention_item("package_scan_unavailable"),)),
        ("an unavailable scan with the setting",
         scan_is("unavailable", "configured"),
         ({"code": "awaiting_merge"},), (attention_item("package_scan_unavailable"),)),
        ("an incomplete scan without the setting",
         scan_is("incomplete", "not_configured"),
         ({"code": "awaiting_merge"},),
         (attention_item("finished_stages_not_configured"), attention_item("package_scan_incomplete"))),
        ("a clone that cannot be read raises only itself", unread_clone_b,
         ({"clone": real(w.c["B"])},),
         (key_items(w, "checkout_unavailable", "B"),)),
        ("a clone that cannot be read keeps its packages' items",
         lambda d, w_: unreadable_clone(clone_at(d, w_.c["A"])),
         ({"clone": real(w.c["A"]), "code": "never_fetched"},),
         (key_items(w, "checkout_unavailable", "A"),)),
        ("an added tracking mismatch", lambda d, w_: named_branch(d, w_.c["W"], "done").update(upstream_mismatch=True),
         (), (key_items(w, "upstream_mismatch", "W", branch="done"),)),
    )


def test_every_attention_rule_raises_and_withholds_what_the_facts_say(world_w):
    w = world_w
    base = world_attention(w, w.live)
    for label, change, drop, add in attention_rows(w):
        document = doc_copy(w.live)
        change(document, w)
        expected = attention_after(base, drop, add)
        assert derived_attention(document) == expected, label


def test_awaiting_merge_entries_of_two_repositories_order_by_branch_then_first_package(world_w):
    """Entries are derived repository by repository, so across repositories only the branch
    and first-package keys put them in order."""
    w = world_w
    base = world_attention(w, w.live)
    (done,) = [i for i in base if i["code"] == "awaiting_merge"]
    repositories = w.live["repositories"]
    assert repositories.index(repository_of(w.live, w.c["A"])) < repositories.index(
        repository_of(w.live, w.c["L"])
    )
    for label, branch, package_path in (
        ("an earlier branch with a later package", "alpha", "Fictional/Shipped/zz-late"),
        ("the same branch with an earlier first package", "done", "Fictional/Shipped/aa-early"),
    ):
        document = doc_copy(w.live)
        extra = doc_copy(world_package(document, "done-a"))
        extra["package_path"] = package_path
        extra["branch"]["name"] = branch
        extra["target"].update(status="clone", clone=real(w.c["L"]), worktree=None)
        document["packages"].append(extra)
        repository_of(document, w.c["L"])["package_branches"] = [
            {"name": branch, "remote": None, "packages": [package_path]}
        ]
        added = attention_item("awaiting_merge", branch=branch, packages=[package_path])
        position = base.index(done)
        assert derived_attention(document) == [*base[:position], added, *base[position:]], label


def test_an_unreadable_clone_without_origin_raises_only_its_unavailability(world_w):
    w = world_w
    base = world_attention(w, w.live)
    document = doc_copy(w.live)
    unreadable_clone(clone_at(document, w.c["L"]))
    path = real(w.c["L"])
    expected = attention_after(
        base, ({"clone": path},), (attention_item("checkout_unavailable", clone=path),)
    )
    assert derived_attention(document) == expected
    parses_rederived(document)


def plain_world_lines(w: WorldW) -> tuple[list[str], list[str]]:
    """The plain lines of world W's live document, split into attention lines and the rest."""
    lines = cli.format_checkouts(w.live)
    count = len(w.live["attention"])
    return lines[:count], lines[count:]


def test_world_w_plain_output_starts_with_its_attention_lines_in_document_order(world_w):
    w = world_w
    literal = json.dumps
    path = {name: literal(real(w.c[name])) for name in w.c}
    drafts = "Fictional/Drafts/"
    attention, rest = plain_world_lines(w)

    assert attention == [
        f'attention branch_unpushed {path["W"]} branch "topic" count 1',
        f'attention branch_not_pushed {path["B"]} branch "side"',
        f'attention branch_not_pushed {path["W"]} branch "local-only"',
        f'attention default_branch_ahead {path["E"]} branch "trunk" count 1',
        f'attention stash {path["W"]} count 1',
        f'attention uncommitted_changes {path["W"]} worktree {path["wt"]} count 1',
        'attention awaiting_merge branch "done" packages '
        '"Fictional/Shipped/done-a","Fictional/Shipped/done-b"',
        f'attention removable {path["W"]} count 2',
        f'attention base_moved {path["A"]} packages "{drafts}drift-moved" count 1',
        f'attention base_moved {path["A"]} packages "{drafts}drift-twin" count 1',
        f'attention base_moved {path["W"]} packages "{drafts}drift-held" count 1',
        f'attention target_repo_missing packages "{drafts}missing-in"',
        f'attention target_repo_not_git packages "{drafts}plain-in"',
        f'attention branch_invalid {path["A"]} packages "{drafts}branch-invalid"',
        f'attention upstream_mismatch {path["W"]} branch "tracking"',
        *[f"attention never_fetched {path[name]}" for name in ("A", "B", "C", "E", "D", "S", "W")],
        f'attention no_origin_remote {path["L"]}',
        f"attention checkout_unavailable {path['U']}",
    ]
    assert len(attention) == len(w.live["attention"])
    assert not any(line.startswith("attention") for line in rest)


def test_world_w_clone_lines_carry_their_availability_right_after_the_path(world_w):
    w = world_w
    path = {name: json.dumps(real(w.c[name])) for name in w.c}
    _attention, rest = plain_world_lines(w)

    def element(name: str) -> str:
        [line] = [item for item in rest if item.startswith(path[name] + "  ")]
        return line.split("  ")[1]

    assigned_to_a = ",".join(
        json.dumps(f"Fictional/Drafts/{name}")
        for name in (
            "baseline-invalid", "branch-invalid", "default", "drift-equal", "drift-moved",
            "drift-off", "drift-twin", "drift-unheld",
        )
    )
    assert {name: element(name) for name in "ABCDEWLSU"} == {
        "A": f"assigned {assigned_to_a}",
        "B": "free 3",
        "C": "free 1",
        "D": "free 2",
        "E": "free 4",
        "W": "occupied not_on_default_branch,linked_worktrees",
        "L": "unknown",
        "S": 'assigned "Fictional/Drafts/drift-shallow"',
        "U": "unknown",
    }
    status_only = f"{path['U']}  unknown  too_many_branches"
    assert status_only in rest
    # A linked worktree line has no availability element.
    [linked] = [item for item in rest if item.startswith("  " + path["wt"] + "  ")]
    assert linked == (
        f"  {path['wt']}  branch wt  staged 0 unstaged 0 untracked 1 conflicted 0"
        "  blocker not_clean"
    )
    # Folder, git and diagnostic lines (none here) never fall between the attention lines.
    assert [item for item in rest if item.startswith(("git ", "folder ", "diagnostic "))] == []


def test_a_document_with_an_empty_attention_list_prints_attention_none_first(world_w):
    document = doc_copy(world_w.live)
    document["attention"] = []
    document["git"] = {"status": "missing", "version": None}

    lines = cli.format_checkouts(document)

    assert lines[:2] == ["attention none", "git missing"]
    assert sum(line.startswith("attention") for line in lines) == 1


def test_an_attention_line_prints_each_element_exactly_when_its_field_is_set(world_w):
    document = doc_copy(world_w.live)
    merge = next(item for item in document["attention"] if item["code"] == "awaiting_merge")
    merge["pull_request"] = 17
    merge["packages"] = ["Fictional/Shipped/done-a"]
    document["attention"] = [
        merge,
        attention_item("never_fetched"),
        attention_item("target_repo_missing"),
    ]

    lines = cli.format_checkouts(document)

    assert lines[:3] == [
        'attention awaiting_merge branch "done" packages "Fictional/Shipped/done-a" pr 17',
        "attention never_fetched",
        "attention target_repo_missing",
    ]
    zero = attention_item("uncommitted_changes", clone="/c", worktree="/w", count=0)
    document["attention"] = [zero]
    assert cli.format_checkouts(document)[0] == 'attention uncommitted_changes "/c" worktree "/w" count 0'


def test_a_worktree_precedes_packages_and_every_token_is_an_ascii_json_literal(world_w):
    document = doc_copy(world_w.live)
    document["attention"] = [
        attention_item(
            "base_moved", clone="/c", worktree="/w", packages=['Fictional/Drafts/caf\u00e9"x'],
            count=2,
        ),
        attention_item("upstream_mismatch", clone="/c", branch='caf\u00e9"x'),
    ]
    shallow = world_w.c["S"]
    clone_at(document, shallow)["availability"]["assigned_to"] = [
        {"package_path": 'Fictional/Drafts/caf\u00e9"x', "basis": "target_repo"}
    ]

    lines = cli.format_checkouts(document)

    assert lines[:2] == [
        r'attention base_moved "/c" worktree "/w" packages "Fictional/Drafts/caf\u00e9\"x" count 2',
        r'attention upstream_mismatch "/c" branch "caf\u00e9\"x"',
    ]
    [line] = [item for item in lines if item.startswith(json.dumps(real(shallow)) + "  ")]
    assert line.split("  ")[1] == r'assigned "Fictional/Drafts/caf\u00e9\"x"'


def test_a_free_rank_counts_within_its_own_repository(world_w):
    w = world_w
    document = doc_copy(w.live)
    local = w.c["L"]
    clone_at(document, local)["availability"] = {"state": "free", "reasons": [], "assigned_to": []}
    repository_of(document, local)["free_clones"] = [real(local)]

    lines = cli.format_checkouts(document)

    def element(name: str) -> str:
        literal = json.dumps(real(w.c[name]))
        [line] = [item for item in lines if item.startswith(literal + "  ")]
        return line.split("  ")[1]

    assert [element(name) for name in ("C", "D", "B", "E", "L")] == [
        "free 1", "free 2", "free 3", "free 4", "free 1",
    ]
