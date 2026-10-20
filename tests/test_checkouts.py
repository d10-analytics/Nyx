"""Behavioral proofs for the checkout facts collector, against real repositories.

Every state is produced by git itself in a temporary directory under an
isolated home, so neither the developer's nor the runner's git configuration
changes any outcome.  Timeouts are lowered only through collector parameters.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from nyx import checkouts, cli, models

ON_WINDOWS = os.name == "nt"
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")

GLOBAL_CONFIG = """\
[user]
\tname = Test
\temail = test@example.com
[init]
\tdefaultBranch = main
[commit]
\tgpgsign = false
[protocol "file"]
\tallow = always
[advice]
\tdetachedHead = false
"""


@pytest.fixture(autouse=True)
def isolated_git(tmp_path_factory, monkeypatch):
    """Point every git child at an empty home that owns the only global config."""
    home = tmp_path_factory.mktemp("home")
    (home / ".gitconfig").write_text(GLOBAL_CONFIG)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in list(os.environ):
        if name.startswith("GIT_") and name not in {"GIT_CONFIG_NOSYSTEM"}:
            monkeypatch.delenv(name)
    return home


def git(cwd: Path | str, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {arguments} failed: {result.stderr}")
    return result


def real(path: Path | str) -> str:
    return os.path.realpath(path)


def commit_file(repo: Path, name: str, content: str, message: str | None = None) -> str:
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message or f"edit {name}")
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def add_packed_refs(repository: Path, refs: dict[str, str]) -> None:
    """Add references through ``packed-refs`` so no ref needs its own file.

    A loose ref is a file whose path grows with the ref name, so long or many
    names hit host path limits; a packed ref costs no path.  The file is
    written as exact bytes because git rejects a ref line ending in CR.
    """
    git(repository, "pack-refs", "--all")
    packed_file = repository / ".git" / "packed-refs"
    entries = {
        line.split(" ", 1)[1]: line.split(" ", 1)[0]
        for line in packed_file.read_bytes().decode("ascii").split("\n")
        if line and line[0] not in "#^"
    }
    entries.update(refs)
    # Peel lines are not kept, so the header claims only sorted order and git
    # peels on demand.
    packed_file.write_bytes(
        (
            "# pack-refs with: sorted \n"
            + "".join(f"{sha} {name}\n" for name, sha in sorted(entries.items()))
        ).encode("ascii")
    )
    packed = packed_file.read_bytes()
    assert packed.endswith(b"\n") and b"\r" not in packed


def make_remote(root: Path, name: str = "remote.git", branch: str = "trunk") -> Path:
    """A bare remote whose default branch is ``branch`` and that has a commit."""
    bare = root / name
    git(root, "init", "-q", "--bare", "-b", branch, str(bare))
    seed = root / f"seed-{name}"
    git(root, "init", "-q", "-b", branch, str(seed))
    commit_file(seed, "file.txt", "one\n", "seed")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "origin", branch)
    return bare


def clone(remote: Path | str, destination: Path, *options: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    git(destination.parent, "clone", "-q", *options, str(remote), str(destination))
    return destination


def collect(
    folders: list[Path | str],
    *,
    seconds: float = 60.0,
    **options: Any,
) -> dict[str, Any]:
    """Run the real collector and return the parsed (strictly validated) document."""
    text = checkouts.collect_checkouts(
        [str(folder) for folder in folders],
        deadline_ns=time.monotonic_ns() + int(seconds * 1e9),
        **options,
    )
    return models.parse_checkouts(text)


def all_clones(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [clone for repo in document["repositories"] for clone in repo["clones"]]


def find_clone(document: dict[str, Any], path: Path | str) -> dict[str, Any]:
    wanted = real(path)
    for item in all_clones(document):
        if item["path"] == wanted:
            return item
    raise AssertionError(f"no clone at {wanted}: {[c['path'] for c in all_clones(document)]}")


def find_worktree(clone_document: dict[str, Any], path: Path | str) -> dict[str, Any]:
    wanted = real(path)
    for item in clone_document["worktrees"]:
        if item["path"] == wanted:
            return item
    raise AssertionError(f"no worktree at {wanted}")


def main_worktree(clone_document: dict[str, Any]) -> dict[str, Any]:
    return next(item for item in clone_document["worktrees"] if item["main"])


def branch_of(clone_document: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in clone_document["branches"] if item["name"] == name)


def assert_unreadable(worktree: dict[str, Any]) -> None:
    assert worktree["head"] is None
    assert worktree["changes"] is None
    assert worktree["operations"] is None
    assert worktree["index_lock"] is None


# ---------------------------------------------------------------------------
# AC-03 discovery and identity
# ---------------------------------------------------------------------------


def test_discovery_orders_clones_and_nests_outside_worktrees(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    clone2 = clone(remote, folder / "Repo Clone 2")
    clone10 = clone(remote, folder / "Repo Clone 10")
    third = clone(remote, tmp_path / "third")
    (folder / "linked").symlink_to(third, target_is_directory=True)
    (folder / "plain").mkdir()
    nested = clone(remote, folder / "outer" / "inner")
    outside = tmp_path / "outside-worktree"
    git(clone2, "worktree", "add", "-q", str(outside), "-b", "outside")

    document = collect([folder])

    assert len(document["repositories"]) == 1
    clones = document["repositories"][0]["clones"]
    assert [item["path"] for item in clones] == [real(clone2), real(clone10)]
    assert [item["path"] for item in clones[0]["worktrees"]] == [real(clone2), real(outside)]
    assert [item["main"] for item in clones[0]["worktrees"]] == [True, False]
    seen = {item["path"] for item in all_clones(document)}
    seen |= {wt["path"] for item in all_clones(document) for wt in item["worktrees"]}
    assert real(third) not in seen and real(nested) not in seen
    assert document["diagnostics"] == []
    assert document["folders"] == [{"path": str(folder), "status": "ok"}]


def test_configured_repository_lists_once_and_flags_a_fake_child(tmp_path):
    remote = make_remote(tmp_path)
    repository = clone(remote, tmp_path / "configured")
    (repository / "c" / ".git" / "info").mkdir(parents=True)

    document = collect([repository])

    assert document["diagnostics"] == [
        {"code": "not_a_repository", "path": str(repository / "c")}
    ]
    assert [item["path"] for item in all_clones(document)] == [real(repository)]


def test_configured_folder_symlink_is_followed(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    only = clone(remote, folder / "only")
    link = tmp_path / "link-to-folder"
    link.symlink_to(folder, target_is_directory=True)

    document = collect([link])

    assert [item["path"] for item in all_clones(document)] == [real(only)]
    assert document["folders"] == [{"path": str(link), "status": "ok"}]


def test_linked_worktree_of_a_bare_repository_reports_the_bare_directory(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    git(remote, "worktree", "add", "-q", str(folder / "bare-linked"), "trunk")

    document = collect([folder])

    [item] = all_clones(document)
    assert item["path"] == real(remote)
    assert item["bare"] is True
    assert [(wt["path"], wt["main"]) for wt in item["worktrees"]] == [
        (real(folder / "bare-linked"), False)
    ]
    assert main_worktree_count(item) == 0


def main_worktree_count(clone_document: dict[str, Any]) -> int:
    return sum(1 for item in clone_document["worktrees"] if item["main"])


def test_worktree_without_its_git_file_is_prunable_and_unattached(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "repo")
    gone = folder / "gone"
    git(repository, "worktree", "add", "-q", str(gone), "-b", "gone")
    (gone / ".git").unlink()

    document = collect([folder])

    [item] = all_clones(document)
    worktree = find_worktree(item, gone)
    assert worktree["prunable"] is True
    assert worktree["status"] == "failed"
    assert worktree["changes"] is None
    assert_unreadable(worktree)
    assert document["diagnostics"] == []
    assert find_worktree(item, repository)["changes"] == {
        "staged": 0, "unstaged": 0, "untracked": 0, "conflicted": 0
    }


def test_unusable_configured_folders_are_reported(tmp_path):
    missing = tmp_path / "missing"
    regular = tmp_path / "regular"
    regular.write_text("file")
    forbidden = tmp_path / "forbidden"
    forbidden.mkdir()
    statuses = {str(missing): "missing", str(regular): "not_directory"}
    can_deny = not ON_WINDOWS and hasattr(os, "geteuid") and os.geteuid() != 0
    if can_deny:
        forbidden.chmod(0)
        statuses[str(forbidden)] = "unreadable"
    try:
        document = collect([missing, regular, forbidden])
    finally:
        forbidden.chmod(stat.S_IRWXU)
    reported = {item["path"]: item["status"] for item in document["folders"]}
    assert reported[str(missing)] == "missing"
    assert reported[str(regular)] == "not_directory"
    assert reported[str(forbidden)] == ("unreadable" if can_deny else "ok")
    assert document["repositories"] == []


def test_candidate_whose_work_tree_is_elsewhere_is_not_a_repository(tmp_path):
    # Git succeeds from the candidate yet names another folder as its top level:
    # only the top-level comparison keeps that folder out of the document.
    folder = tmp_path / "folder"
    folder.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    redirected = folder / "redirected"
    git(folder, "init", "-q", "-b", "trunk", str(redirected))
    git(redirected, "config", "core.worktree", str(elsewhere))
    shown = git(redirected, "rev-parse", "--show-toplevel").stdout.strip()
    assert real(shown) == real(elsewhere), "control: git reports the other folder"

    document = collect([folder])

    assert all_clones(document) == []
    assert document["diagnostics"] == [
        {"code": "not_a_repository", "path": str(redirected)}
    ]


@pytest.mark.skipif(not ON_WINDOWS, reason="directory junctions are a Windows feature")
def test_directory_junction_child_is_not_a_candidate(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    third = clone(remote, tmp_path / "third")
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(folder / "junction"), str(third)],
        check=True, capture_output=True,
    )
    document = collect([folder])
    assert all_clones(document) == []


# ---------------------------------------------------------------------------
# AC-04 per-checkout state
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    """One clone of a fresh remote inside a configured folder."""
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    return clone(remote, folder / "repo")


def state_of(repository: Path, folder: Path | None = None) -> dict[str, Any]:
    document = collect([folder or repository.parent])
    return find_clone(document, repository)


def conflicting_branches(repository: Path) -> str:
    """Leave ``other`` and the current branch editing the same line differently."""
    base = git(repository, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    git(repository, "checkout", "-q", "-b", "other")
    commit_file(repository, "file.txt", "other\n", "other edit")
    git(repository, "checkout", "-q", base)
    commit_file(repository, "file.txt", "mine\n", "my edit")
    return base


def test_status_counts_follow_git_status_semantics(repo):
    commit_file(repo, "m.txt", "m\n")
    commit_file(repo, "u.txt", "u\n")
    commit_file(repo, "old.txt", "old\n")
    (repo / "m.txt").write_text("m1\n")
    git(repo, "add", "m.txt")
    (repo / "m.txt").write_text("m2\n")  # the "1 MM" path
    (repo / "u.txt").write_text("u1\n")  # unstaged only
    git(repo, "mv", "old.txt", "new.txt")  # a staged rename: two staged paths
    (repo / "staged.txt").write_text("s\n")
    git(repo, "add", "staged.txt")
    (repo / "untracked.txt").write_text("x\n")

    changes = main_worktree(state_of(repo))["changes"]

    assert changes == {"staged": 4, "unstaged": 2, "untracked": 1, "conflicted": 0}


@pytest.mark.parametrize(
    ("trigger", "expected"),
    [
        ("merge", ["merge"]),
        ("rebase", ["rebase"]),
        ("am", ["am"]),
        ("cherry_pick", ["cherry_pick"]),
        ("revert", ["revert"]),
        ("bisect_merge", ["bisect", "merge"]),
    ],
)
def test_in_progress_operations_come_from_git_directory_markers(repo, trigger, expected):
    if trigger == "merge":
        conflicting_branches(repo)
        git(repo, "merge", "other", check=False)
    elif trigger == "rebase":
        conflicting_branches(repo)
        git(repo, "rebase", "other", check=False)
    elif trigger == "am":
        base = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        git(repo, "checkout", "-q", "-b", "patched")
        commit_file(repo, "file.txt", "patched\n", "patched edit")
        patch = repo.parent / "change.patch"
        patch.write_text(git(repo, "format-patch", "-1", "--stdout").stdout)
        git(repo, "checkout", "-q", base)
        commit_file(repo, "file.txt", "diverged\n", "diverged edit")
        git(repo, "am", str(patch), check=False)
    elif trigger == "cherry_pick":
        conflicting_branches(repo)
        git(repo, "cherry-pick", "other", check=False)
    elif trigger == "revert":
        first = commit_file(repo, "file.txt", "first\n")
        commit_file(repo, "file.txt", "second\n")
        git(repo, "revert", "--no-edit", first, check=False)
    else:
        start = git(repo, "rev-parse", "HEAD").stdout.strip()
        conflicting_branches(repo)
        git(repo, "bisect", "start", "HEAD", start)
        git(repo, "checkout", "-q", "--detach", "HEAD")
        git(repo, "merge", "other", check=False)

    worktree = main_worktree(state_of(repo))

    assert worktree["operations"] == expected
    if trigger != "am":
        assert worktree["changes"]["conflicted"] == 1


def test_conflicted_path_counts_only_as_conflicted(repo):
    conflicting_branches(repo)
    git(repo, "merge", "other", check=False)

    changes = main_worktree(state_of(repo))["changes"]

    assert changes == {"staged": 0, "unstaged": 0, "untracked": 0, "conflicted": 1}


def test_clean_checkout_has_no_operations_and_no_lock(repo):
    worktree = main_worktree(state_of(repo))
    assert worktree["operations"] == []
    assert worktree["index_lock"] is False


def test_index_lock_is_read_from_each_worktrees_own_git_directory(repo, tmp_path):
    linked = tmp_path / "linked-wt"
    git(repo, "worktree", "add", "-q", str(linked), "-b", "linked")
    (repo / ".git" / "index.lock").write_text("")

    item = state_of(repo)
    assert find_worktree(item, repo)["index_lock"] is True
    assert find_worktree(item, linked)["index_lock"] is False

    (repo / ".git" / "index.lock").unlink()
    [git_directory] = list((repo / ".git" / "worktrees").iterdir())
    (git_directory / "index.lock").write_text("")

    item = state_of(repo)
    assert find_worktree(item, repo)["index_lock"] is False
    assert find_worktree(item, linked)["index_lock"] is True


def test_detached_head_has_no_branch_and_its_sha(repo):
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "-q", "--detach")

    assert main_worktree(state_of(repo))["head"] == {"branch": None, "sha": sha}


def test_stash_count_counts_every_stash(repo):
    for index in range(2):
        (repo / "file.txt").write_text(f"edit {index}\n")
        git(repo, "stash", "push", "-q")

    assert state_of(repo)["stash_count"] == 2


def test_locked_and_prunable_are_separate_flags(repo, tmp_path):
    locked = tmp_path / "locked-wt"
    pruned = tmp_path / "pruned-wt"
    both = tmp_path / "locked-gone-wt"
    git(repo, "worktree", "add", "-q", str(locked), "-b", "locked")
    git(repo, "worktree", "lock", str(locked))
    git(repo, "worktree", "add", "-q", str(pruned), "-b", "pruned")
    git(repo, "worktree", "add", "-q", str(both), "-b", "both")
    git(repo, "worktree", "lock", str(both))
    shutil.rmtree(pruned)
    shutil.rmtree(both)

    item = state_of(repo)

    held = find_worktree(item, locked)
    assert held["locked"] is True and held["prunable"] is False and held["status"] == "ok"
    gone = find_worktree(item, pruned)
    assert gone["prunable"] is True and gone["locked"] is False
    assert gone["status"] == "failed" and gone["changes"] is None
    assert_unreadable(gone)
    kept = find_worktree(item, both)
    assert kept["locked"] is True and kept["prunable"] is False
    assert kept["status"] == "failed" and kept["changes"] is None
    assert_unreadable(kept)


def test_checked_out_in_branch_order_and_main_first(repo, tmp_path):
    for name in ("b10", "b2"):
        git(repo, "branch", name)
    linked = tmp_path / "aaa-linked"
    git(repo, "worktree", "add", "-q", str(linked), "-b", "topic")

    item = state_of(repo)

    assert [branch["name"] for branch in item["branches"]] == ["b2", "b10", "topic", "trunk"]
    assert branch_of(item, "trunk")["checked_out_in"] == [real(repo)]
    assert branch_of(item, "topic")["checked_out_in"] == [real(linked)]
    assert branch_of(item, "b2")["checked_out_in"] == []
    assert [wt["path"] for wt in item["worktrees"]] == [real(repo), real(linked)]


def test_stale_worktree_path_reused_by_another_clone_belongs_only_to_that_clone(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "code"
    first = clone(remote, folder / "c1")
    second = clone(remote, folder / "c2")
    shared = folder / "wt"
    git(first, "worktree", "add", "-q", str(shared), "-b", "f1")
    shutil.rmtree(shared)
    git(second, "worktree", "add", "-q", str(shared), "-b", "f2")

    document = collect([folder])

    owner = find_clone(document, second)
    assert [item["path"] for item in owner["worktrees"]].count(real(shared)) == 1
    taken = find_worktree(owner, shared)
    assert taken["head"]["branch"] == "f2"
    assert taken["status"] == "ok"
    assert branch_of(owner, "f2")["checked_out_in"] == [real(shared)]
    stale = find_clone(document, first)
    assert [item["path"] for item in stale["worktrees"]] == [real(first)]
    assert branch_of(stale, "f1")["checked_out_in"] == []
    assert sum(
        item["path"] == real(shared)
        for entry in all_clones(document)
        for item in entry["worktrees"]
    ) == 1
    assert cli.format_checkouts(document)


def test_worktree_of_a_moved_clone_stays_listed_as_failed(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "m"
    original = clone(remote, folder / "c")
    outside = tmp_path / "outside_mv"
    git(original, "worktree", "add", "-q", str(outside), "-b", "mv")
    moved = folder / "c-moved"
    original.rename(moved)

    document = collect([folder])

    owner = find_clone(document, moved)
    worktree = find_worktree(owner, outside)
    assert worktree["status"] == "failed" and worktree["changes"] is None
    assert worktree["head"] is None
    assert branch_of(owner, "mv")["checked_out_in"] == [real(outside)]
    assert sum(
        item["path"] == real(outside)
        for entry in all_clones(document)
        for item in entry["worktrees"]
    ) == 1


def replace_git_marker(worktree: Path, content: str) -> None:
    """Make ``<worktree>/.git`` a plain file holding ``content``.

    Some hosts leave the marker read-only or otherwise not directly writable
    after ``git worktree add``, so remove it first instead of writing over it.
    Another process can briefly hold a fresh file open, so a permission error
    is retried a few times; any other error is raised at once.
    """
    marker = worktree / ".git"
    attempts = 3
    for attempt in range(attempts):
        try:
            if marker.is_dir() and not marker.is_symlink():
                shutil.rmtree(marker)
            elif marker.exists() or marker.is_symlink():
                try:
                    os.chmod(marker, stat.S_IWRITE)
                except OSError:
                    pass
                marker.unlink()
            marker.write_text(content)
            return
        except PermissionError as error:
            if attempt + 1 < attempts:
                time.sleep(0.2)
                continue
            try:
                state = os.stat(marker)
            except OSError as stat_error:
                state = f"unavailable ({stat_error})"
            raise AssertionError(
                f"cannot replace {marker}: {error!r}; is_dir={marker.is_dir()}; stat={state}"
            ) from error


@pytest.mark.parametrize("content", ["garbage\n", "gitdir:\n", "gitdir:   \n"])
def test_worktree_with_a_malformed_git_file_stays_listed_as_failed(repo, tmp_path, content):
    linked = tmp_path / "broken-wt"
    git(repo, "worktree", "add", "-q", str(linked), "-b", "broken")
    replace_git_marker(linked, content)

    item = state_of(repo)

    worktree = find_worktree(item, linked)
    assert worktree["status"] == "failed" and worktree["changes"] is None
    assert_unreadable(worktree)
    assert branch_of(item, "broken")["checked_out_in"] == [real(linked)]


def test_locked_worktree_inside_a_dirty_clone_does_not_report_the_clone(repo):
    (repo / "dirty.txt").write_text("dirty\n")
    inside = repo / "inside"
    git(repo, "worktree", "add", "-q", str(inside), "-b", "inside")
    git(repo, "worktree", "lock", str(inside))
    (inside / ".git").unlink()

    item = state_of(repo)

    worktree = find_worktree(item, inside)
    assert worktree["locked"] is True and worktree["prunable"] is False
    assert worktree["status"] == "failed"
    assert worktree["changes"] is None
    assert_unreadable(worktree)
    assert main_worktree(item)["changes"]["untracked"] >= 1


def test_unborn_head_reports_its_branch_without_a_sha(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    fresh = folder / "fresh"
    git(folder, "init", "-q", "-b", "start", str(fresh))

    item = state_of(fresh, folder)

    assert main_worktree(item)["head"] == {"branch": "start", "sha": None}
    assert item["branches"] == []
    assert item["status"] == "ok"


# ---------------------------------------------------------------------------
# Scripts that prove a command ran (POSIX only: they need a shell)
# ---------------------------------------------------------------------------

posix_only = pytest.mark.skipif(ON_WINDOWS, reason="needs a POSIX shell script")


def marker_script(directory: Path, name: str, marker: Path, then: str = "") -> Path:
    """An executable that records that it ran, then optionally runs ``then``."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text(f'#!/bin/sh\necho ran >> "{marker}"\n{then}\n')
    script.chmod(0o755)
    return script


def repository_fingerprint(repository: Path) -> dict[str, Any]:
    config = repository / ".git" / "config"
    if not config.exists():
        config = repository / "config"
    return {
        "refs": git(repository, "for-each-ref", "--format=%(refname) %(objectname)").stdout,
        "config": config.read_bytes(),
        "objects": git(repository, "count-objects", "-v").stdout,
    }


def index_snapshot(repository: Path) -> tuple[bytes, int]:
    index = repository / ".git" / "index"
    return index.read_bytes(), index.stat().st_mtime_ns


# ---------------------------------------------------------------------------
# AC-05 read-only, non-interfering, no network
# ---------------------------------------------------------------------------


def touch_tracked_file(repository: Path) -> None:
    target = repository / "file.txt"
    later = time.time_ns() + 5_000_000_000
    os.utime(target, ns=(later, later))


def test_collection_does_not_refresh_the_index_or_take_its_lock(repo):
    touch_tracked_file(repo)
    before = index_snapshot(repo)
    document = collect([repo.parent])
    assert index_snapshot(repo) == before
    assert not (repo / ".git" / "index.lock").exists()
    # The same state, read by an ordinary status, does rewrite the index: the
    # unchanged index above is therefore caused by the read-only invocation.
    git(repo, "status", "--porcelain")
    assert index_snapshot(repo) != before
    assert main_worktree(find_clone(document, repo))["changes"]["unstaged"] == 0


@posix_only
def test_repository_fsmonitor_and_injected_configuration_never_run(repo, tmp_path, monkeypatch):
    marker = tmp_path / "fsmonitor-marker"
    script = marker_script(tmp_path / "scripts", "monitor", marker)
    clean = collect([repo.parent])

    git(repo, "config", "core.fsmonitor", str(script))
    git(repo, "status", "--porcelain")
    assert marker.exists(), "control: plain git runs the configured monitor"
    marker.unlink()
    collect([repo.parent])
    assert not marker.exists()
    git(repo, "config", "--unset", "core.fsmonitor")

    decoy = clone(repo, tmp_path / "decoy")
    with monkeypatch.context() as environment:
        environment.setenv("GIT_CONFIG_PARAMETERS", f"'core.fsmonitor={script}'")
        git(repo, "status", "--porcelain")
        assert marker.exists(), "control: the injected setting reaches plain git"
        marker.unlink()
        environment.setenv("GIT_INDEX_FILE", str(decoy / ".git" / "index"))
        environment.setenv("GIT_DIR", str(decoy / ".git"))
        environment.setenv("GIT_WORK_TREE", str(decoy))
        injected = collect([repo.parent])

    assert not marker.exists()
    assert injected["checkouts_digest"] == clean["checkouts_digest"]


@posix_only
def test_collection_contacts_no_remote_and_changes_no_repository(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    clones = [clone(remote, folder / f"clone {index}") for index in range(2)]
    marker = tmp_path / "upload-pack-marker"
    script = marker_script(tmp_path / "scripts", "uploadpack", marker, 'exec git upload-pack "$@"')
    for item in clones:
        git(item, "config", "remote.origin.uploadpack", str(script))
        (item / "scratch.txt").write_text("untracked\n")
    control = clone(remote, tmp_path / "control")
    git(control, "config", "remote.origin.uploadpack", str(script))
    git(control, "fetch", "-q")
    assert marker.exists(), "control: a fetch does run the upload-pack program"
    marker.unlink()
    before = {item: repository_fingerprint(item) for item in [*clones, remote]}

    collect([folder])

    assert not marker.exists()
    assert {item: repository_fingerprint(item) for item in [*clones, remote]} == before


def test_redirected_index_and_injected_configuration_are_each_ignored(repo, tmp_path, monkeypatch):
    # Each variable alone, with a control showing plain git honours it, so the
    # removal of every one is observable on its own.
    clean = collect([repo.parent])

    with monkeypatch.context() as environment:
        environment.setenv("GIT_INDEX_FILE", str(tmp_path / "other-index"))
        assert git(repo, "status", "--porcelain").stdout, "control: plain git reads that index"
        redirected = collect([repo.parent])
    assert redirected["checkouts_digest"] == clean["checkouts_digest"]

    injected_url = "ssh://injected.example/o/r"
    with monkeypatch.context() as environment:
        environment.setenv("GIT_CONFIG_PARAMETERS", f"'remote.origin.url={injected_url}'")
        shown = git(repo, "config", "remote.origin.url").stdout.strip()
        assert shown == injected_url, "control: the injected setting reaches plain git"
        injected = collect([repo.parent])
    assert injected["checkouts_digest"] == clean["checkouts_digest"]


# ---------------------------------------------------------------------------
# AC-06 unavailable git and refused ownership
# ---------------------------------------------------------------------------


def test_missing_git_is_reported_with_no_repositories(tmp_path, monkeypatch):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    clone(remote, folder / "repo")
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    document = collect([folder])

    assert document["git"] == {"status": "missing", "version": None}
    assert document["repositories"] == []
    assert document["folders"] == [{"path": str(folder), "status": "ok"}]


@posix_only
def test_old_git_is_reported_with_its_version_and_no_repositories(tmp_path, monkeypatch):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    clone(remote, folder / "repo")
    old = tmp_path / "old-git"
    old.mkdir()
    script = old / "git"
    script.write_text("#!/bin/sh\necho 'git version 2.35.9'\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{old}{os.pathsep}{os.environ['PATH']}")

    document = collect([folder])

    assert document["git"] == {"status": "too_old", "version": "2.35.9"}
    assert document["repositories"] == []


@posix_only
def test_unparseable_git_version_is_too_old_with_a_null_version(tmp_path, monkeypatch):
    garbage = tmp_path / "garbage-git"
    garbage.mkdir()
    script = garbage / "git"
    script.write_text("#!/bin/sh\necho 'git version garbage'\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{garbage}{os.pathsep}{os.environ['PATH']}")

    document = collect([tmp_path])

    assert document["git"] == {"status": "too_old", "version": None}


def trust_only(home: Path, trusted: list[Path]) -> None:
    with (home / ".gitconfig").open("a") as config:
        for path in trusted:
            config.write(f'[safe]\n\tdirectory = {real(path).replace(chr(92), "/")}\n')


def test_clone_git_refuses_for_ownership_is_marked_and_siblings_complete(
    tmp_path, monkeypatch, isolated_git
):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    trusted = [clone(remote, folder / "trusted a"), clone(remote, folder / "trusted b")]
    refused = clone(remote, folder / "refused")
    trust_only(isolated_git, trusted)
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")

    document = collect([folder])

    item = find_clone(document, refused)
    assert item["status"] == "unsafe_ownership"
    for fact in (
        "bare", "partial_clone", "shallow", "cached_default", "local_default",
        "cached_vs_live", "stash_count", "worktrees", "branches",
    ):
        assert item[fact] is None
    for sibling in trusted:
        assert find_clone(document, sibling)["status"] == "ok"
        assert main_worktree(find_clone(document, sibling))["status"] == "ok"


def test_linked_worktree_git_refuses_for_ownership_is_marked(
    tmp_path, monkeypatch, isolated_git
):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "repo")
    linked = tmp_path / "linked"
    git(repository, "worktree", "add", "-q", str(linked), "-b", "linked")
    trust_only(isolated_git, [repository])
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")

    item = find_clone(collect([folder]), repository)

    assert item["status"] == "ok"
    assert find_worktree(item, repository)["status"] == "ok"
    worktree = find_worktree(item, linked)
    assert worktree["status"] == "unsafe_ownership"
    assert worktree["changes"] is None
    assert_unreadable(worktree)


# ---------------------------------------------------------------------------
# AC-07 cached default branch, comparisons and freshness
# ---------------------------------------------------------------------------


def push_commit(remote: Path, branch: str, tmp_path: Path, name: str = "pusher") -> str:
    """Push one new commit to ``branch`` of ``remote`` from a scratch clone."""
    scratch = tmp_path / f"{name}-{time.monotonic_ns()}"
    clone(remote, scratch)
    git(scratch, "checkout", "-q", "-B", branch, f"origin/{branch}" if branch_exists(
        scratch, branch) else "HEAD")
    sha = commit_file(scratch, f"{branch}-{time.monotonic_ns()}.txt", "x\n")
    git(scratch, "push", "-q", "origin", branch)
    return sha


def branch_exists(repository: Path, branch: str) -> bool:
    return (
        git(repository, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}",
            check=False).returncode == 0
    )


def assert_cached_everywhere(document: dict[str, Any]) -> None:
    assert document["remote_checks"] == "off"
    for repository in document["repositories"]:
        if repository["remote"] is not None:
            assert repository["remote"]["check"] == {
                "status": "off", "reason": "remote_checks_off"
            }
            assert repository["default_branch"]["live_sha"] is None
        for item in repository["clones"]:
            if item["status"] != "ok":
                continue
            assert item["cached_vs_live"] is None
            comparisons = [branch["vs_remote"] for branch in item["branches"]]
            if item["local_default"] is not None:
                assert item["local_default"]["vs_live"] is None
                comparisons.append(item["local_default"]["vs_cached"])
            for comparison in filter(None, comparisons):
                assert comparison["basis"] == "cached"
                assert comparison["objects_from"] is None


def test_cached_default_and_local_default_for_a_fresh_clone(tmp_path):
    remote = make_remote(tmp_path, branch="trunk")
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "repo")
    sha = git(repository, "rev-parse", "origin/trunk").stdout.strip()

    document = collect([folder])

    item = find_clone(document, repository)
    assert item["cached_default"] == {
        "name": "trunk", "sha": sha, "last_fetched_at": None, "fetched_since_clone": False
    }
    assert item["local_default"]["name"] == "trunk"
    assert item["local_default"]["sha"] == sha
    assert item["local_default"]["vs_cached"] == {
        "basis": "cached", "relation": "equal", "ahead": 0, "behind": 0, "objects_from": None
    }
    assert document["repositories"][0]["default_branch"] == {"name": "trunk", "live_sha": None}
    assert_cached_everywhere(document)


def test_fetch_inside_a_linked_worktree_counts_as_a_fetch_of_the_clone(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "repo")
    linked = tmp_path / "linked"
    git(repository, "worktree", "add", "-q", str(linked), "-b", "linked")
    assert find_clone(collect([folder]), repository)["cached_default"]["last_fetched_at"] is None

    git(linked, "fetch", "-q")

    freshness = find_clone(collect([folder]), repository)["cached_default"]
    assert TIMESTAMP.fullmatch(freshness["last_fetched_at"])
    assert freshness["fetched_since_clone"] is True


def test_branch_relations_against_the_cached_origin_branches(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "repo")

    def publish(name: str) -> None:
        git(repository, "branch", name)
        git(repository, "push", "-q", "-u", "origin", name)

    publish("pushed")
    publish("ahead")
    publish("behind")
    publish("diverged")
    publish("feature")
    git(repository, "branch", "local-only")
    git(repository, "branch", "ghost")
    git(repository, "branch", "-u", "origin/trunk", "feature")
    for name in ("ahead", "diverged"):
        git(repository, "checkout", "-q", name)
        commit_file(repository, f"{name}.txt", "local\n")
    git(repository, "checkout", "-q", "trunk")
    for name in ("behind", "diverged"):
        push_commit(remote, name, tmp_path)
    git(repository, "fetch", "-q")
    # A cached same-name ref whose commit object is gone: the IDs differ and
    # nothing can be counted.
    lost = git(repository, "commit-tree", "-m", "lost", "HEAD^{tree}").stdout.strip()
    git(repository, "update-ref", "refs/remotes/origin/ghost", lost)
    loose = repository / ".git" / "objects" / lost[:2] / lost[2:]
    loose.chmod(stat.S_IRWXU)
    loose.unlink()

    item = find_clone(collect([folder]), repository)

    def relation(name: str) -> tuple[Any, ...]:
        comparison = branch_of(item, name)["vs_remote"]
        return comparison["relation"], comparison["ahead"], comparison["behind"]

    assert relation("pushed") == ("equal", 0, 0)
    assert relation("ahead") == ("ahead", 1, 0)
    assert relation("behind") == ("behind", 0, 1)
    assert relation("diverged") == ("diverged", 1, 1)
    assert relation("local-only") == ("absent", None, None)
    assert relation("ghost") == ("differs", None, None)
    assert branch_of(item, "feature")["upstream"] == "origin/trunk"
    assert branch_of(item, "feature")["upstream_mismatch"] is True
    assert branch_of(item, "pushed")["upstream"] == "origin/pushed"
    assert branch_of(item, "pushed")["upstream_mismatch"] is False
    assert branch_of(item, "trunk")["upstream_mismatch"] is False


def test_removed_origin_head_leaves_the_cached_default_unknown(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "repo")
    git(repository, "remote", "set-head", "origin", "-d")

    item = find_clone(collect([folder]), repository)

    assert item["cached_default"]["name"] is None
    assert item["cached_default"]["sha"] is None
    assert item["local_default"] is None


def test_clones_with_different_origin_heads_leave_the_group_default_null(tmp_path):
    remote = make_remote(tmp_path)
    push_commit(remote, "dev", tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    first = clone(remote, folder / "first")
    second = clone(remote, folder / "second")
    git(second, "remote", "set-head", "origin", "dev")

    document = collect([folder])

    assert document["repositories"][0]["default_branch"] == {"name": None, "live_sha": None}
    assert find_clone(document, first)["cached_default"]["name"] == "trunk"
    assert find_clone(document, second)["cached_default"]["name"] == "dev"


def test_missing_local_default_branch_is_absent(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "repo")
    git(repository, "checkout", "-q", "-b", "elsewhere")
    git(repository, "branch", "-D", "trunk")

    item = find_clone(collect([folder]), repository)

    assert item["local_default"]["name"] == "trunk"
    assert item["local_default"]["sha"] is None
    assert item["local_default"]["vs_cached"]["relation"] == "absent"
    assert item["local_default"]["vs_cached"]["ahead"] is None


def test_failed_fetch_and_other_remote_fetch_do_not_count_as_freshness(tmp_path):
    remote = make_remote(tmp_path, "failing.git")
    other = make_remote(tmp_path, "other.git")
    folder = tmp_path / "folder"
    folder.mkdir()
    failing = clone(remote, folder / "failing")
    git(failing, "fetch", "-q")
    assert find_clone(collect([folder]), failing)["cached_default"]["last_fetched_at"]
    remote.rename(tmp_path / "failing-moved.git")
    assert git(failing, "fetch", "-q", check=False).returncode != 0
    assert (failing / ".git" / "FETCH_HEAD").stat().st_size == 0
    second = clone(other, folder / "second")
    git(second, "remote", "add", "extra", str(tmp_path / "failing-moved.git"))
    git(second, "fetch", "-q", "extra")

    document = collect([folder])

    for item in (find_clone(document, failing), find_clone(document, second)):
        assert item["cached_default"]["last_fetched_at"] is None
        assert item["cached_default"]["fetched_since_clone"] is False


def test_instead_of_alias_origin_is_recognised_in_fetch_head(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "repo")
    git(repository, "config", f"url.{tmp_path.as_posix()}/.insteadOf", "alias:")
    git(repository, "remote", "set-url", "origin", "alias:remote.git")
    git(repository, "fetch", "-q", "origin")

    freshness = find_clone(collect([folder]), repository)["cached_default"]

    assert TIMESTAMP.fullmatch(freshness["last_fetched_at"])


def test_origin_path_containing_of_is_recognised_for_both_fetch_head_shapes(tmp_path):
    spaced = tmp_path / "a of b"
    spaced.mkdir()
    remote = make_remote(spaced)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "repo")
    fetch_head = repository / ".git" / "FETCH_HEAD"

    git(repository, "fetch", "-q")
    first = find_clone(collect([folder]), repository)["cached_default"]
    fetch_head.unlink()
    git(repository, "fetch", "-q", "origin", "HEAD")
    assert not fetch_head.read_text().split("\t")[-1].startswith(("branch '", "'"))
    second = find_clone(collect([folder]), repository)["cached_default"]

    assert TIMESTAMP.fullmatch(first["last_fetched_at"])
    assert TIMESTAMP.fullmatch(second["last_fetched_at"])


# ---------------------------------------------------------------------------
# AC-08 grouping and secrets
# ---------------------------------------------------------------------------


def test_origin_urls_group_by_normalized_key_and_hide_credentials(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    urls = {
        "ssh": "ssh://git@Host:22/o/r.git",
        "scp": "git@host:o/r",
        "https": "https://someuser:s3cr3t@host/o/r/",
        "port": "ssh://host:2222/o/r",
        "path": str(remote),
        "file": remote.as_uri(),
    }
    clones = {}
    for name, url in urls.items():
        clones[name] = clone(remote, folder / name)
        git(clones[name], "remote", "set-url", "origin", url)
    bare = clone(remote, folder / "nobody")
    git(bare, "remote", "remove", "origin")

    text = checkouts.collect_checkouts(
        [str(folder)], deadline_ns=time.monotonic_ns() + 60 * 10**9
    )
    document = models.parse_checkouts(text)

    keyed = {
        repo["remote"]["key"]: [Path(c["path"]).name for c in repo["clones"]]
        for repo in document["repositories"]
        if repo["remote"] is not None
    }
    assert keyed["host/o/r"] == ["https", "scp", "ssh"]
    assert keyed["host:2222/o/r"] == ["port"]
    assert keyed[f"file:{real(remote).removesuffix('.git')}"] == ["file", "path"]
    assert "someuser" not in text and "s3cr3t" not in text
    last = document["repositories"][-1]
    assert last["remote"] is None and last["default_branch"] is None
    [orphan] = last["clones"]
    assert orphan["path"] == real(bare)
    assert orphan["cached_default"] is None and orphan["local_default"] is None
    assert orphan["cached_vs_live"] is None
    assert all(
        branch["vs_remote"] is None and branch["upstream_mismatch"] is False
        for branch in orphan["branches"]
    )
    assert [repo["remote"] is None for repo in document["repositories"]] == [
        False, False, False, True
    ]


# ---------------------------------------------------------------------------
# AC-09 document contract and parser
# ---------------------------------------------------------------------------


def redigest(document: dict[str, Any]) -> dict[str, Any]:
    document["checkouts_digest"] = models.canonical_digest(
        document, omit=models.CHECKOUTS_DIGEST_OMITTED
    )
    return document


@pytest.fixture
def sample(tmp_path):
    """A document with two ordered clones, an outside worktree, a stash and freshness."""
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    second = clone(remote, folder / "Repo Clone 2")
    tenth = clone(remote, folder / "Repo Clone 10")
    outside = tmp_path / "outside"
    git(second, "worktree", "add", "-q", str(outside), "-b", "outside")
    git(second, "branch", "never-pushed")
    commit_file(second, "ahead.txt", "ahead\n")
    (second / "file.txt").write_text("stashed\n")
    git(second, "stash", "push", "-q")
    git(tenth, "fetch", "-q")
    text = checkouts.collect_checkouts(
        [str(folder)], deadline_ns=time.monotonic_ns() + 60 * 10**9
    )
    return json.loads(text), folder


def doc_copy(document: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(document))


def first_clone(document: dict[str, Any]) -> dict[str, Any]:
    return document["repositories"][0]["clones"][0]


def test_parser_accepts_the_collector_output(sample):
    document, _folder = sample
    parsed = models.parse_checkouts(json.dumps(document))
    assert parsed["checkouts_digest"] == document["checkouts_digest"]
    assert [Path(c["path"]).name for c in all_clones(parsed)] == ["Repo Clone 2", "Repo Clone 10"]
    redigested = redigest(doc_copy(document))
    assert models.parse_checkouts(redigested)["checkouts_digest"] == document["checkouts_digest"]


def mutate_extra_top(document):
    document["extra"] = 1


def mutate_extra_nested(document):
    first_clone(document)["extra"] = 1


def mutate_missing_top(document):
    del document["folders"]


def mutate_missing_nested(document):
    del first_clone(document)["shallow"]


def mutate_clone_order(document):
    document["repositories"][0]["clones"].reverse()


def mutate_clone_repeated(document):
    duplicate = doc_copy(first_clone(document))
    duplicate.update(
        status="failed", bare=None, partial_clone=None, shallow=None, cached_default=None,
        local_default=None, cached_vs_live=None, stash_count=None, worktrees=None, branches=None,
    )
    document["repositories"].append(
        {"remote": None, "default_branch": None, "clones": [duplicate]}
    )


def mutate_enum(document):
    first_clone(document)["status"] = "bogus"


def mutate_folder_enum(document):
    document["folders"][0]["status"] = "gone"


def mutate_comparison_counts(relation):
    def mutate(document):
        comparison = first_clone(document)["branches"][0]["vs_remote"]
        comparison.update(relation=relation, ahead=1, behind=0)

    return mutate


def mutate_counts_contradict(document):
    first_clone(document)["branches"][0]["vs_remote"].update(
        relation="behind", ahead=1, behind=1
    )


def mutate_schema(value):
    def mutate(document):
        document["schema_version"] = value

    return mutate


def mutate_zero_head(document):
    main_worktree(first_clone(document))["head"]["sha"] = "0" * 40


def mutate_timestamp(field_path, value):
    def mutate(document):
        if field_path == "observed_at":
            document["observed_at"] = value
        else:
            clone_document = next(
                c for c in all_clones(document) if c["cached_default"]["last_fetched_at"]
            )
            clone_document["cached_default"]["last_fetched_at"] = value

    return mutate


def mutate_unsorted_worktrees(document):
    first_clone(document)["worktrees"].reverse()


def mutate_unsorted_branches(document):
    first_clone(document)["branches"].reverse()


def mutate_unsafe_text(document):
    first_clone(document)["path"] = "bad\x01path"


def mutate_prunable_ok(document):
    main_worktree(first_clone(document))["prunable"] = True


@pytest.mark.parametrize(
    "mutation",
    [
        mutate_extra_top,
        mutate_extra_nested,
        mutate_missing_top,
        mutate_missing_nested,
        mutate_clone_order,
        mutate_clone_repeated,
        mutate_enum,
        mutate_folder_enum,
        mutate_comparison_counts("differs"),
        mutate_comparison_counts("absent"),
        mutate_comparison_counts("unknown"),
        mutate_counts_contradict,
        mutate_schema(2),
        mutate_schema("1"),
        mutate_schema(True),
        mutate_zero_head,
        mutate_unsorted_worktrees,
        mutate_unsorted_branches,
        mutate_unsafe_text,
        mutate_prunable_ok,
        *[
            mutate_timestamp(field, value)
            for field in ("observed_at", "last_fetched_at")
            for value in (
                "2026-10-19 10:00:00Z",
                "2026-10-19T10:00:00+00:00",
                "2026-10-19T10:00:00.5Z",
                "2026-13-40T10:00:00Z",
                "2026-10-19T10:00:00",
            )
        ],
    ],
    ids=lambda mutation: getattr(mutation, "__name__", "mutation")
    + str(abs(hash(mutation)) % 1000),
)
def test_parser_rejects_a_contract_violation(sample, mutation):
    document, _folder = sample
    broken = doc_copy(document)
    mutation(broken)
    with pytest.raises(models.ProtocolError):
        models.parse_checkouts(redigest(broken))


def test_parser_rejects_a_changed_field_with_the_old_digest(sample):
    document, _folder = sample
    broken = doc_copy(document)
    first_clone(broken)["stash_count"] = 7
    with pytest.raises(models.ProtocolError, match="digest"):
        models.parse_checkouts(broken)


def test_parser_rejects_duplicate_keys_and_non_finite_numbers(sample):
    document, _folder = sample
    text = json.dumps(document)
    with pytest.raises(models.ProtocolError):
        models.parse_checkouts(text.replace('"remote_checks":', '"remote_checks":"off","remote_checks":', 1))
    for literal in ("NaN", "Infinity", "-Infinity", "1e999"):
        with pytest.raises(models.ProtocolError):
            models.parse_checkouts(text.replace('"stash_count": 1', f'"stash_count": {literal}', 1))
    assert '"stash_count": 1' in text


def test_observed_at_alone_may_change_without_a_new_digest(sample):
    document, _folder = sample
    changed = doc_copy(document)
    changed["observed_at"] = "2001-01-01T00:00:00Z"
    assert models.parse_checkouts(changed)["checkouts_digest"] == document["checkouts_digest"]


def test_unchanged_state_gives_a_new_observation_time_and_the_same_digest(repo):
    first = collect([repo.parent])
    time.sleep(1.1)
    second = collect([repo.parent])

    assert first["observed_at"] != second["observed_at"]
    assert TIMESTAMP.fullmatch(first["observed_at"])
    assert first["checkouts_digest"] == second["checkouts_digest"]


NULLABLE_CLONE_FACTS = {
    "bare": (False, 0, "", []),
    "partial_clone": (False, 0, "", []),
    "shallow": (False, 0, "", []),
    "cached_default": ({}, "", [], False),
    "local_default": ({}, "", [], False),
    "cached_vs_live": ({}, "", [], False),
    "stash_count": (0, False, "", []),
    "worktrees": ([], "", 0, False),
    "branches": ([], "", 0, False),
}
NULLABLE_WORKTREE_FACTS = {
    "head": ({}, "", [], False),
    "changes": ({}, "", [], False, 0),
    "operations": ([], "", 0, False),
    "index_lock": (False, 0, "", []),
}


def refused_clone(document: dict[str, Any]) -> dict[str, Any]:
    clone_document = first_clone(document)
    clone_document.update(
        status="failed", bare=None, partial_clone=None, shallow=None, cached_default=None,
        local_default=None, cached_vs_live=None, stash_count=None, worktrees=None, branches=None,
    )
    return clone_document


def test_parser_accepts_non_ok_items_with_null_facts(sample):
    document, _folder = sample
    broken = doc_copy(document)
    refused_clone(broken)
    other = broken["repositories"][0]["clones"][1]
    worktree = other["worktrees"][0]
    worktree.update(status="timed_out", head=None, changes=None, operations=None, index_lock=None)
    assert models.parse_checkouts(redigest(broken))


@pytest.mark.parametrize(
    ("fact", "empty"),
    [(fact, empty) for fact, forms in NULLABLE_CLONE_FACTS.items() for empty in forms],
    ids=lambda value: str(value)[:12],
)
def test_parser_rejects_empty_forms_for_clone_facts_that_must_be_null(sample, fact, empty):
    document, _folder = sample
    broken = doc_copy(document)
    refused_clone(broken)[fact] = empty
    with pytest.raises(models.ProtocolError):
        models.parse_checkouts(redigest(broken))


@pytest.mark.parametrize(
    ("fact", "empty"),
    [(fact, empty) for fact, forms in NULLABLE_WORKTREE_FACTS.items() for empty in forms],
    ids=lambda value: str(value)[:12],
)
def test_parser_rejects_empty_forms_for_worktree_facts_that_must_be_null(sample, fact, empty):
    document, _folder = sample
    broken = doc_copy(document)
    worktree = broken["repositories"][0]["clones"][1]["worktrees"][0]
    worktree.update(status="failed", head=None, changes=None, operations=None, index_lock=None)
    worktree[fact] = empty
    with pytest.raises(models.ProtocolError):
        models.parse_checkouts(redigest(broken))


def test_parser_accepts_the_unborn_head_clone(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    git(folder, "init", "-q", "-b", "start", str(folder / "fresh"))
    document = collect([folder])
    assert main_worktree(all_clones(document)[0])["head"] == {"branch": "start", "sha": None}


def test_oversized_document_is_refused_with_a_category(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "repo")
    head = git(repository, "rev-parse", "HEAD").stdout.strip()
    add_packed_refs(
        repository, {f"refs/heads/never-pushed-{n:06d}": head for n in range(10_000)}
    )
    assert len(git(repository, "for-each-ref", "refs/heads").stdout.splitlines()) == 10_001

    with pytest.raises(checkouts.CheckoutsOutputTooLargeError) as error:
        checkouts.collect_checkouts(
            [str(folder)], deadline_ns=time.monotonic_ns() + 60 * 10**9
        )

    assert error.value.category == "checkouts_output_too_large"


@pytest.mark.skipif(ON_WINDOWS, reason="a control character cannot be a Windows file name")
def test_worktree_with_a_control_character_is_omitted_with_a_diagnostic(repo, tmp_path):
    odd = tmp_path / "odd\x01name"
    fine = tmp_path / "fine"
    git(repo, "worktree", "add", "-q", str(odd), "-b", "odd")
    git(repo, "worktree", "add", "-q", str(fine), "-b", "fine")

    document = collect([repo.parent])

    item = find_clone(document, repo)
    assert [wt["path"] for wt in item["worktrees"]] == [real(repo), real(fine)]
    assert item["status"] == "ok"
    assert document["diagnostics"] == [
        {"code": "unrepresentable_path", "path": real(repo)}
    ]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux allows non-UTF-8 names")
def test_clone_with_a_non_utf8_directory_name_is_omitted_with_a_diagnostic(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    fine = clone(remote, folder / "fine")
    clone(remote, folder / os.fsdecode(b"bad-\xff-name"))

    document = collect([folder])

    assert [item["path"] for item in all_clones(document)] == [real(fine)]
    assert document["diagnostics"] == [{"code": "unrepresentable_path", "path": str(folder)}]


# ---------------------------------------------------------------------------
# AC-12 partial and shallow clones
# ---------------------------------------------------------------------------


def allow_filters(remote: Path) -> None:
    git(remote, "config", "uploadpack.allowFilter", "true")


def pack_count(repository: Path) -> int:
    match = re.search(r"^packs: (\d+)$", git(repository, "count-objects", "-v").stdout, re.M)
    assert match
    return int(match.group(1))


def guard_remote(repository: Path, tmp_path: Path) -> Path | None:
    """Make any contact with the remote's upload-pack observable (POSIX only)."""
    if ON_WINDOWS:
        return None
    marker = tmp_path / f"upload-marker-{repository.name}"
    script = marker_script(
        tmp_path / "scripts", f"uploadpack-{repository.name}", marker, 'exec git upload-pack "$@"'
    )
    git(repository, "config", "remote.origin.uploadpack", str(script))
    return marker


def test_partial_clone_counts_cached_refs_without_contacting_its_remote(tmp_path):
    remote = make_remote(tmp_path)
    allow_filters(remote)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote.as_uri(), folder / "partial", "--filter=blob:none")
    commit_file(repository, "local.txt", "local\n")
    marker = guard_remote(repository, tmp_path)
    packs = pack_count(repository)

    item = find_clone(collect([folder]), repository)

    assert item["partial_clone"] is True
    comparison = branch_of(item, "trunk")["vs_remote"]
    assert (comparison["relation"], comparison["ahead"], comparison["behind"]) == ("ahead", 1, 0)
    assert pack_count(repository) == packs
    if marker is not None:
        assert not marker.exists()


def test_shallow_clone_never_counts_past_its_boundary(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote.as_uri(), folder / "shallow", "--depth", "1")
    first = find_clone(collect([folder]), repository)
    assert first["shallow"] is True
    assert branch_of(first, "trunk")["vs_remote"]["relation"] == "equal"

    push_commit(remote, "trunk", tmp_path)
    git(repository, "fetch", "-q", "--depth", "1")
    counted = git(repository, "rev-list", "--left-right", "--count", "trunk...origin/trunk")
    assert counted.stdout.split() == ["1", "1"], "control: counting would claim a divergence"

    second = find_clone(collect([folder]), repository)

    comparison = branch_of(second, "trunk")["vs_remote"]
    assert (comparison["relation"], comparison["ahead"], comparison["behind"]) == (
        "differs", None, None
    )


def test_tree_filtered_clone_without_trees_fails_its_worktree_and_stays_offline(tmp_path):
    remote = make_remote(tmp_path)
    allow_filters(remote)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(
        remote.as_uri(), folder / "trees", "--filter=tree:0", "--no-checkout"
    )
    marker = guard_remote(repository, tmp_path)
    packs = pack_count(repository)
    assert "allow = always" in (Path(os.environ["HOME"]) / ".gitconfig").read_text()

    item = find_clone(collect([folder]), repository)

    worktree = main_worktree(item)
    assert worktree["status"] == "failed"
    assert worktree["changes"] is None
    assert_unreadable(worktree)
    assert pack_count(repository) == packs
    if marker is not None:
        assert not marker.exists()


# ---------------------------------------------------------------------------
# AC-13 executable resolution
# ---------------------------------------------------------------------------


def real_git_version() -> str:
    return git(".", "--version").stdout.split()[2]


def test_relative_path_entry_is_never_searched_for_git(tmp_path, monkeypatch):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    clone(remote, folder / "repo")
    marker = tmp_path / "relative-marker"
    expected = real_git_version()
    trap = tmp_path / "trap"
    trap.mkdir()
    if ON_WINDOWS:
        shutil.copy(os.environ["COMSPEC"], trap / "git.exe")
    else:
        marker_script(trap, "git", marker, 'exec git "$@"')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f"trap{os.pathsep}{os.environ['PATH']}")

    document = collect([folder])

    assert not marker.exists()
    assert document["git"] == {"status": "available", "version": expected}


@pytest.mark.skipif(not ON_WINDOWS, reason="Windows resolves executables differently")
def test_windows_cmd_shim_and_working_directory_executable_are_not_used(tmp_path, monkeypatch):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    clone(remote, folder / "repo")
    marker = tmp_path / "cmd-marker"
    expected = real_git_version()
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "git.cmd").write_text(f"@echo ran > \"{marker}\"\r\n@git.exe %*\r\n")
    work = tmp_path / "work"
    work.mkdir()
    shutil.copy(os.environ["COMSPEC"], work / "git.exe")
    monkeypatch.chdir(work)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")

    document = collect([folder])

    assert not marker.exists()
    assert document["git"] == {"status": "available", "version": expected}


# ---------------------------------------------------------------------------
# AC-15 packaged wording and dependencies
# ---------------------------------------------------------------------------


def test_collector_sources_avoid_the_forbidden_packaged_wording():
    forbidden = (b"v" + bytes((50,)), b"survives " + b"migration", b"viewer_" + b"migration")
    for module in (checkouts, models):
        payload = Path(module.__file__).read_bytes().lower()
        for sequence in forbidden:
            assert sequence not in payload, f"{module.__name__} contains {sequence!r}"


def test_no_runtime_dependency_was_added():
    import tomllib

    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert project["project"]["dependencies"] == []


# ---------------------------------------------------------------------------
# AC-16 budgets
# ---------------------------------------------------------------------------


def make_slow_clone(remote: Path, folder: Path) -> Path:
    """A clone whose stat-dirty file makes every status run a 5 s clean filter."""
    slow = clone(remote, folder / "slow")
    commit_file(slow, "slow.txt", "slow\n", "one unpushed commit")
    (slow / ".git" / "info" / "attributes").write_text("slow.txt filter=slow\n")
    git(slow, "config", "filter.slow.clean", "sleep 5; cat")
    later = time.time_ns() + 5_000_000_000
    os.utime(slow / "slow.txt", ns=(later, later))
    return slow


@pytest.fixture
def git_shim(tmp_path, monkeypatch):
    """Wrap git so every child records its process ID before becoming git."""
    log = tmp_path / "shim.log"
    if ON_WINDOWS:
        return None
    real_git = shutil.which("git")
    shim = tmp_path / "shim"
    shim.mkdir()
    script = shim / "git"
    script.write_text(f'#!/bin/sh\necho "$$ $*" >> "{log}"\nexec "{real_git}" "$@"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")
    return log


def exited_status_pids(log: Path, repository: Path) -> list[int]:
    pids = []
    for line in log.read_text().splitlines():
        pid, _, arguments = line.partition(" ")
        if f"-C {real(repository)} status" in arguments:
            pids.append(int(pid))
    return pids


def assert_exited(pid: int) -> None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    raise AssertionError(f"git child {pid} is still present after collection returned")


def test_slow_status_is_cut_by_the_per_call_cap_and_siblings_complete(tmp_path, git_shim):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    slow = make_slow_clone(remote, folder)
    sibling = clone(remote, folder / "sibling")

    started = time.monotonic()
    document = collect([folder], call_cap_seconds=1.0)
    elapsed = time.monotonic() - started

    assert elapsed < 4
    worktree = main_worktree(find_clone(document, slow))
    assert worktree["status"] == "timed_out"
    assert worktree["changes"] is None
    assert_unreadable(worktree)
    assert main_worktree(find_clone(document, sibling))["status"] == "ok"
    # With only the status call cut, the count call that follows still runs.
    assert branch_of(find_clone(document, slow), "trunk")["vs_remote"]["relation"] == "ahead"
    if git_shim is not None:
        pids = exited_status_pids(git_shim, slow)
        assert pids
        for pid in pids:
            assert_exited(pid)


def test_slow_status_is_cut_by_the_deadline_and_later_counts_are_unknown(tmp_path, git_shim):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    slow = make_slow_clone(remote, folder)
    sibling = clone(remote, folder / "sibling")

    started = time.monotonic()
    document = collect([folder], seconds=1.0)
    elapsed = time.monotonic() - started

    assert elapsed < 4
    slow_clone = find_clone(document, slow)
    worktree = main_worktree(slow_clone)
    assert worktree["status"] == "timed_out"
    assert worktree["changes"] is None
    assert_unreadable(worktree)
    comparison = branch_of(slow_clone, "trunk")["vs_remote"]
    assert comparison["relation"] == "unknown"
    assert comparison["ahead"] is None and comparison["behind"] is None
    assert comparison["objects_from"] is None
    assert main_worktree(find_clone(document, sibling))["status"] == "ok"
    if git_shim is not None:
        for pid in exited_status_pids(git_shim, slow):
            assert_exited(pid)


# ---------------------------------------------------------------------------
# Repairs: counts that contradict a relation, unbounded text, decoding, identity
# ---------------------------------------------------------------------------


def test_different_objects_naming_the_same_commits_differ_without_counts(repo):
    git(repo, "tag", "-a", "-m", "annotated", "release", "HEAD")
    tag_object = git(repo, "rev-parse", "release").stdout.strip()
    git(repo, "branch", "tagged")
    git(repo, "update-ref", "refs/remotes/origin/tagged", tag_object)
    assert git(
        repo, "rev-list", "--left-right", "--count", f"tagged...{tag_object}"
    ).stdout.split() == ["0", "0"], "control: git counts nothing between them"

    item = state_of(repo)

    comparison = branch_of(item, "tagged")["vs_remote"]
    assert (comparison["relation"], comparison["ahead"], comparison["behind"]) == (
        "differs", None, None
    )


def test_text_beyond_the_document_bound_fails_the_clone_not_the_document(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    main = clone(remote, tmp_path / "main")
    sibling = clone(remote, folder / "sibling")
    name = "/".join("x" * 203 for _ in range(5))  # 1019 characters: a safe name
    # The branch and its remote-tracking ref are packed, so the long name never
    # becomes a file path; "origin/" + name is over the bound.
    tip = commit_file(main, "long.txt", "long\n")
    git(main, "reset", "-q", "--hard", "HEAD~1")
    add_packed_refs(main, {f"refs/heads/{name}": tip, f"refs/remotes/origin/{name}": tip})
    git(main, "config", f"branch.{name}.remote", "origin")
    git(main, "config", f"branch.{name}.merge", f"refs/heads/{name}")
    upstream = git(
        main, "for-each-ref", "--format=%(upstream:short)", f"refs/heads/{name}"
    ).stdout.strip()
    assert upstream == f"origin/{name}" and len(upstream) > models.MAX_TEXT_UNITS
    linked = folder / "linked"
    git(main, "worktree", "add", "-q", "--detach", str(linked))

    document = collect([folder])

    item = find_clone(document, main)
    assert item["status"] == "failed"
    assert item["path"] == real(main), "identity does not follow the candidate that found it"
    assert item["branches"] is None and item["worktrees"] is None
    assert find_clone(document, sibling)["status"] == "ok"
    assert all(real(linked) != c["path"] for c in all_clones(document))


def test_git_output_that_is_not_valid_text_never_raises(monkeypatch):
    # Windows' fsdecode raises on such bytes; the collector must not depend on it.
    monkeypatch.setattr(os, "fsdecode", lambda data: data.decode("utf-8", "strict"))
    assert checkouts._decode(b"a\xffb") == "a\udcffb"
    assert checkouts._decode("nämé".encode()) == "nämé"


def test_origin_beyond_the_text_bound_fails_its_clone_and_the_document_parses(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    long_origin = clone(remote, folder / "long origin")
    git(long_origin, "remote", "set-url", "origin", "ssh://host/" + "o" * 1100)
    sibling = clone(remote, folder / "sibling")
    orphan = clone(remote, folder / "orphan")
    git(orphan, "remote", "remove", "origin")

    text = checkouts.collect_checkouts(
        [str(folder)], deadline_ns=time.monotonic_ns() + 60 * 10**9
    )
    document = models.parse_checkouts(text)

    groups = {c["path"]: repo for repo in document["repositories"] for c in repo["clones"]}
    failed = find_clone(document, long_origin)
    assert failed["status"] == "failed"
    for fact in (
        "bare", "partial_clone", "shallow", "cached_default", "local_default",
        "cached_vs_live", "stash_count", "worktrees", "branches",
    ):
        assert failed[fact] is None
    assert groups[real(long_origin)]["remote"] is None
    assert groups[real(long_origin)]["clones"] == [failed]
    assert "o" * 1100 not in text
    assert groups[real(sibling)]["remote"]["key"] == f"file:{real(remote).removesuffix('.git')}"
    assert find_clone(document, sibling)["status"] == "ok"
    kept = find_clone(document, orphan)
    assert kept["status"] == "ok" and groups[real(orphan)]["remote"] is None
    assert kept["cached_default"] is None and kept["local_default"] is None
    assert kept["cached_vs_live"] is None
    assert kept["branches"] and all(
        branch["vs_remote"] is None and branch["upstream_mismatch"] is False
        for branch in kept["branches"]
    )


# ---------------------------------------------------------------------------
# Remote checks: an in-test loopback smart-HTTP server and live comparisons
# ---------------------------------------------------------------------------

ADVERTISEMENT = "application/x-git-upload-pack-advertisement"
STDERR_PHRASES = (
    "does not appear to be a git repository",
    "Failed to connect",
    "Couldn't connect",
    "protocol error",
    "bad line length",
    "not allowed",
    "terminal prompts",
    "Permission denied",
    "fatal",
)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *arguments):
        pass

    def do_GET(self):
        recorder = self.server.recorder
        parts = urlsplit(self.path)
        with recorder.lock:
            recorder.requests.append((parts.path, parts.query, self.headers.get("X-Clone")))
            recorder.in_flight += 1
            recorder.max_in_flight = max(recorder.max_in_flight, recorder.in_flight)
        try:
            self._answer(recorder, parts)
        finally:
            with recorder.lock:
                recorder.in_flight -= 1

    def _send(self, status, body=b"", content_type=None, headers=()):
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _answer(self, recorder, parts):
        base = parts.path.removesuffix("/info/refs")
        bare = recorder.remotes.get(base)
        if bare is None or not parts.path.endswith("/info/refs"):
            self._send(404)
            return
        behavior = recorder.behaviors.get(base, "serve")
        if behavior == "block":
            recorder.release.wait(120)
            return
        if behavior == "401":
            self._send(401, headers=[("WWW-Authenticate", 'Basic realm="nyx"')])
            return
        if behavior == "junk":
            self._send(200, b"this is not a git advertisement\n", ADVERTISEMENT)
            return
        if isinstance(behavior, tuple) and behavior[0] == "delay":
            recorder.release.wait(behavior[1])
        elif isinstance(behavior, tuple) and behavior[0] == "hook":
            behavior[1]()
        advertised = subprocess.run(
            ["git", "upload-pack", "--stateless-rpc", "--advertise-refs", str(bare)],
            capture_output=True,
            check=True,
        ).stdout
        self._send(200, b"001e# service=git-upload-pack\n0000" + advertised, ADVERTISEMENT)


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


class Recorder:
    """Serve real upload-pack advertisements for bare remotes and log each request."""

    def __init__(self) -> None:
        self.remotes: dict[str, Path] = {}
        self.behaviors: dict[str, Any] = {}
        self.requests: list[tuple[str, str, str | None]] = []
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.release = threading.Event()
        self.server = _QuietServer(("127.0.0.1", 0), _Handler)
        self.server.recorder = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def add(self, name: str, bare: Path, behavior: Any = "serve") -> str:
        self.remotes[f"/{name}.git"] = bare
        self.behaviors[f"/{name}.git"] = behavior
        return self.url(name)

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/{name}.git"

    def discoveries(self, name: str | None = None) -> list[tuple[str, str, str | None]]:
        return [
            item
            for item in self.requests
            if item[0].endswith("/info/refs")
            and "service=git-upload-pack" in item[1]
            and (name is None or item[0] == f"/{name}.git/info/refs")
        ]

    def stop(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def recorders(monkeypatch):
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    made: list[Recorder] = []

    def make() -> Recorder:
        made.append(Recorder())
        return made[-1]

    yield make
    for recorder in made:
        recorder.stop()


def web_clone(recorder: Recorder, name: str, bare: Path, destination: Path, label: str,
              *options: str) -> Path:
    """Clone ``bare`` and point ``origin`` at its recorder path, naming the clone in a header."""
    if f"/{name}.git" not in recorder.remotes:
        recorder.add(name, bare)
    source = bare.as_uri() if options else bare
    clone(source, destination, *options)
    git(destination, "remote", "set-url", "origin", recorder.url(name))
    git(destination, "config", "http.extraHeader", f"X-Clone: {label}")
    return destination


def collect_text(folders: list[Path | str], *, seconds: float = 60.0, **options: Any):
    started = time.monotonic()
    text = checkouts.collect_checkouts(
        [str(folder) for folder in folders],
        deadline_ns=time.monotonic_ns() + int(seconds * 1e9),
        **options,
    )
    return text, models.parse_checkouts(text), time.monotonic() - started


def live(folders: list[Path | str], **options: Any) -> dict[str, Any]:
    return collect_text(folders, remote_checks=True, **options)[1]


def group_of(document: dict[str, Any], path: Path | str) -> dict[str, Any]:
    wanted = real(path)
    for group in document["repositories"]:
        if any(item["path"] == wanted for item in group["clones"]):
            return group
    raise AssertionError(f"no group holds {wanted}")


def comparisons_of(item: dict[str, Any]) -> list[dict[str, Any]]:
    found = [branch["vs_remote"] for branch in item["branches"]]
    found.append(item["cached_vs_live"])
    if item["local_default"] is not None:
        found += [item["local_default"]["vs_live"], item["local_default"]["vs_cached"]]
    return [comparison for comparison in found if comparison is not None]


def assert_fell_back_to_cached(group: dict[str, Any], reason: str) -> None:
    assert group["remote"]["check"] == {"status": "failed", "reason": reason}
    assert group["default_branch"]["live_sha"] is None
    for item in group["clones"]:
        if item["status"] != "ok":
            continue
        assert item["cached_vs_live"] is None
        if item["local_default"] is not None:
            assert item["local_default"]["vs_live"] is None
        assert {c["basis"] for c in comparisons_of(item)} <= {"cached"}


def push_by_url(repository: Path, remote: Path, branch: str) -> None:
    git(repository, "push", "-q", remote.as_uri(), branch)


def pushed_then_reset(repository: Path, remote: Path, branch: str = "trunk") -> str:
    """Commit, push by URL and reset back, so origin/<branch> stays one commit behind."""
    pushed = commit_file(repository, f"{branch}-pushed.txt", "pushed\n")
    push_by_url(repository, remote, branch)
    git(repository, "reset", "-q", "--hard", "HEAD~1")
    return pushed


def remove_tree(path: Path) -> None:
    def retry(function, target, _error):
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(path, onerror=retry)


def fingerprint(repository: Path) -> dict[str, Any]:
    state = repository_fingerprint(repository)
    state["fetch_head"] = (repository / ".git" / "FETCH_HEAD").exists()
    return state


def pack_listing(repository: Path) -> list[tuple[str, bytes]]:
    directory = repository / ".git" / "objects" / "pack"
    return sorted((item.name, item.read_bytes()) for item in directory.iterdir())


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_one_discovery_per_remote_in_natural_order_and_off_makes_none(tmp_path, recorders):
    recorder = recorders()
    served = tmp_path / "served"
    served.mkdir()
    one = make_remote(served, "one.git", "trunk")
    two = make_remote(served, "two.git", "main")
    three = make_remote(served, "three.git", "main")
    folder = tmp_path / "folder"
    folder.mkdir()
    for label in ("Repo Clone 10", "Repo Clone 2", "Repo Clone 3"):
        web_clone(recorder, "one", one, folder / label, label)
    web_clone(recorder, "two", two, folder / "Other Clone", "Other Clone")
    broken = web_clone(recorder, "three", three, folder / "Broken Clone", "Broken Clone")
    name = "/".join("x" * 203 for _ in range(5))
    tip = commit_file(broken, "long.txt", "long\n")
    git(broken, "reset", "-q", "--hard", "HEAD~1")
    add_packed_refs(broken, {f"refs/heads/{name}": tip, f"refs/remotes/origin/{name}": tip})
    git(broken, "config", f"branch.{name}.remote", "origin")
    git(broken, "config", f"branch.{name}.merge", f"refs/heads/{name}")

    off = collect([folder])
    assert recorder.requests == []
    assert_cached_everywhere(off)

    document = live([folder])

    assert document["remote_checks"] == "on"
    assert [item[0] for item in recorder.discoveries()].count("/three.git/info/refs") == 0
    assert sorted(item[0] for item in recorder.discoveries()) == [
        "/one.git/info/refs", "/two.git/info/refs"
    ]
    assert recorder.discoveries("one")[0][2] == "Repo Clone 2"
    assert group_of(document, folder / "Repo Clone 2")["remote"]["check"] == {
        "status": "live", "reason": None
    }
    assert group_of(document, folder / "Repo Clone 2")["default_branch"]["name"] == "trunk"
    assert group_of(document, folder / "Other Clone")["remote"]["check"]["status"] == "live"
    assert find_clone(document, broken)["status"] == "failed"
    assert group_of(document, broken)["remote"]["check"] == {
        "status": "failed", "reason": "failed"
    }
    assert recorder.requests and all(
        item[0] != "/three.git/info/refs" for item in recorder.requests
    )


def test_live_values_and_comparisons_without_changing_any_clone(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served, "r.git", "trunk")
    main = clone(remote, folder / "main")
    git(main, "checkout", "-q", "-b", "pushed")
    commit_file(main, "p.txt", "p\n")
    push_by_url(main, remote, "pushed")
    git(main, "checkout", "-q", "-b", "private")
    commit_file(main, "q.txt", "q\n")
    git(main, "checkout", "-q", "trunk")
    pushed = pushed_then_reset(main, remote)

    other = make_remote(served, "other.git", "trunk")
    lagging = clone(other, folder / "lagging")
    outside = clone(other, tmp_path / "outside")
    outside_head = commit_file(outside, "o.txt", "o\n")
    push_by_url(outside, other, "trunk")

    detached = make_remote(served, "detached.git", "trunk")
    detached_clone = clone(detached, folder / "detached clone")
    head = git(detached, "rev-parse", "HEAD").stdout.strip()
    git(detached, "update-ref", "--no-deref", "HEAD", head)
    empty = served / "empty.git"
    git(served, "init", "-q", "--bare", "-b", "trunk", str(empty))
    empty_clone = clone(empty, folder / "empty clone")

    clones = [main, lagging, detached_clone, empty_clone]
    before = {path: fingerprint(path) for path in clones}

    document = live([folder])

    assert {path: fingerprint(path) for path in clones} == before
    group = group_of(document, main)
    assert group["remote"]["check"] == {"status": "live", "reason": None}
    assert group["default_branch"] == {"name": "trunk", "live_sha": pushed}
    item = find_clone(document, main)
    default = item["local_default"]
    assert default["name"] == "trunk"
    assert default["vs_live"] == {
        "basis": "live", "relation": "behind", "ahead": 0, "behind": 1, "objects_from": None
    }
    assert default["vs_cached"]["basis"] == "cached"
    assert default["vs_cached"]["relation"] == "equal"
    assert item["cached_vs_live"]["relation"] == "behind"
    assert (item["cached_vs_live"]["behind"], item["cached_vs_live"]["basis"]) == (1, "live")
    assert branch_of(item, "pushed")["vs_remote"]["relation"] == "equal"
    assert branch_of(item, "pushed")["vs_remote"]["basis"] == "live"
    assert branch_of(item, "private")["vs_remote"]["relation"] == "absent"
    assert branch_of(item, "private")["vs_remote"]["basis"] == "live"

    stale = find_clone(document, lagging)["local_default"]["vs_live"]
    assert group_of(document, lagging)["default_branch"]["live_sha"] == outside_head
    assert stale == {
        "basis": "live", "relation": "differs", "ahead": None, "behind": None,
        "objects_from": None,
    }

    no_name = group_of(document, detached_clone)
    assert no_name["default_branch"] == {"name": None, "live_sha": head}
    assert find_clone(document, detached_clone)["local_default"] is None
    nothing = group_of(document, empty_clone)
    assert nothing["remote"]["check"] == {"status": "live", "reason": None}
    assert nothing["default_branch"]["live_sha"] is None


def test_presence_probe_reads_a_present_id_beside_a_missing_one(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served, "r.git", "trunk")
    holder = clone(remote, folder / "holder")
    outside = clone(remote, tmp_path / "outside")
    git(holder, "checkout", "-q", "-b", "feature")
    feature_tip = pushed_then_reset(holder, remote, "feature")
    git(holder, "checkout", "-q", "trunk")
    # The live trunk head, which the holder lacks, sorts before the feature
    # tip it holds, so reading the probe's answer by argument position would
    # misplace the present ID.
    attempt = 0
    while True:
        live_head = commit_file(outside, "o.txt", f"outside {attempt}\n")
        if live_head < feature_tip:
            break
        git(outside, "reset", "-q", "--hard", "HEAD~1")
        attempt += 1
    push_by_url(outside, remote, "trunk")

    item = find_clone(live([folder]), holder)

    assert branch_of(item, "feature")["vs_remote"] == {
        "basis": "live", "relation": "behind", "ahead": 0, "behind": 1, "objects_from": None
    }
    assert item["local_default"]["vs_live"] == {
        "basis": "live", "relation": "differs", "ahead": None, "behind": None,
        "objects_from": None,
    }


def test_failed_remotes_fall_back_to_cached_without_leaking_anything(tmp_path, recorders):
    recorder = recorders()
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    gone = make_remote(served, "gone.git")
    gone_clone = clone(gone, folder / "gone clone")
    remove_tree(gone)
    refused = clone(make_remote(served, "refused.git"), folder / "refused clone")
    port = closed_port()
    git(refused, "remote", "set-url", "origin", f"http://u:s3cret@127.0.0.1:{port}/r.git")
    junk = make_remote(served, "junk.git")
    recorder.add("junk", junk, "junk")
    junk_clone = web_clone(recorder, "junk", junk, folder / "junk clone", "junk clone")
    fine = make_remote(served, "fine.git")
    fine_clone = web_clone(recorder, "fine", fine, folder / "fine clone", "fine clone")

    text, document, _ = collect_text([folder], remote_checks=True)

    assert_fell_back_to_cached(group_of(document, gone_clone), "not_found")
    assert_fell_back_to_cached(group_of(document, refused), "unreachable")
    assert_fell_back_to_cached(group_of(document, junk_clone), "failed")
    assert group_of(document, fine_clone)["remote"]["check"]["status"] == "live"
    assert "s3cret" not in text
    assert f"http://127.0.0.1:{port}" not in text and "r.git" not in text
    assert not any(phrase in text for phrase in STDERR_PHRASES)


def test_inherited_protocol_policy_governs_the_remote_call(tmp_path, recorders, monkeypatch):
    recorder = recorders()
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    local = clone(make_remote(served, "local.git"), folder / "file clone")
    web = web_clone(recorder, "web", make_remote(served, "web.git"), folder / "web clone", "web")

    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "http")
    inherited = live([folder])
    assert_fell_back_to_cached(group_of(inherited, local), "failed")
    assert group_of(inherited, web)["remote"]["check"]["status"] == "live"

    monkeypatch.delenv("GIT_ALLOW_PROTOCOL")
    assert group_of(live([folder]), local)["remote"]["check"]["status"] == "live"

    git(local, "config", "protocol.file.allow", "never")
    configured = live([folder])
    assert_fell_back_to_cached(group_of(configured, local), "failed")
    assert group_of(configured, web)["remote"]["check"]["status"] == "live"


def test_partial_clone_that_fetched_the_live_head_still_differs_without_counts(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    allow_filters(remote)
    partial = clone(remote.as_uri(), folder / "partial", "--filter=blob:none")
    pushed = push_commit(remote, "trunk", tmp_path)
    git(partial, "fetch", "-q", "origin")
    packs = pack_count(partial)

    document = live([folder])

    item = find_clone(document, partial)
    assert item["partial_clone"] is True
    assert group_of(document, partial)["default_branch"]["live_sha"] == pushed
    assert item["local_default"]["vs_live"] == {
        "basis": "live", "relation": "differs", "ahead": None, "behind": None,
        "objects_from": None,
    }
    assert pack_count(partial) == packs


def test_query_runs_in_the_first_partial_clone_and_adds_no_pack(tmp_path, recorders):
    recorder = recorders()
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    allow_filters(remote)
    partial = web_clone(
        recorder, "shared", remote, folder / "A Partial", "A Partial", "--filter=blob:none"
    )
    pushed = push_commit(remote, "trunk", tmp_path)
    full = web_clone(recorder, "shared", remote, folder / "B Full", "B Full")
    packs = pack_listing(partial)

    document = live([folder])

    assert [item[2] for item in recorder.discoveries()] == ["A Partial"]
    assert pack_listing(partial) == packs
    group = group_of(document, partial)
    assert group["remote"]["check"] == {"status": "live", "reason": None}
    assert group["default_branch"]["live_sha"] == pushed
    assert find_clone(document, partial)["local_default"]["vs_live"]["relation"] == "differs"
    assert find_clone(document, partial)["local_default"]["vs_live"]["ahead"] is None
    assert find_clone(document, full)["local_default"]["vs_live"]["relation"] == "equal"


def test_shallow_clone_never_counts_a_live_comparison(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    shallow = clone(remote.as_uri(), folder / "shallow", "--depth", "1")
    push_commit(remote, "trunk", tmp_path)
    git(shallow, "fetch", "-q", "--depth", "1")
    counted = git(shallow, "rev-list", "--left-right", "--count", "trunk...origin/trunk")
    assert counted.stdout.split() == ["1", "1"], "control: counting would claim a divergence"

    item = find_clone(live([folder]), shallow)

    assert item["shallow"] is True
    assert item["local_default"]["vs_live"]["relation"] == "differs"
    assert {c["relation"] for c in comparisons_of(item) if c["basis"] == "live"} <= {
        "equal", "differs"
    }


def test_blocked_remote_times_out_while_another_group_stays_live(tmp_path, recorders):
    recorder = recorders()
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    stuck = make_remote(served, "stuck.git")
    recorder.add("stuck", stuck, "block")
    stuck_clone = web_clone(recorder, "stuck", stuck, folder / "stuck clone", "stuck")
    fine = web_clone(recorder, "fine", make_remote(served, "fine.git"), folder / "fine", "fine")

    _, document, elapsed = collect_text(
        [folder], remote_checks=True, seconds=8.0, call_cap_seconds=2.0
    )

    assert_fell_back_to_cached(group_of(document, stuck_clone), "timed_out")
    assert group_of(document, fine)["remote"]["check"]["status"] == "live"
    assert elapsed < 8.0


def test_remote_queries_run_at_most_max_parallel_at_once(tmp_path, recorders):
    recorder = recorders()
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    paths = []
    for number in range(4):
        bare = make_remote(served, f"slow{number}.git")
        recorder.add(f"slow{number}", bare, ("delay", 0.5))
        paths.append(web_clone(recorder, f"slow{number}", bare, folder / f"c{number}", "c"))

    _, document, _ = collect_text(
        [folder], remote_checks=True, seconds=8.0, call_cap_seconds=2.0, max_parallel=2
    )

    assert all(group_of(document, path)["remote"]["check"]["status"] == "live" for path in paths)
    assert len(recorder.discoveries()) == 4
    assert recorder.max_in_flight <= 2


def test_deadline_cuts_an_unstarted_query_and_the_collection_returns(tmp_path, recorders):
    recorder = recorders()
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    paths = []
    for number in range(2):
        bare = make_remote(served, f"stuck{number}.git")
        recorder.add(f"stuck{number}", bare, "block")
        paths.append(web_clone(recorder, f"stuck{number}", bare, folder / f"c{number}", "c"))

    _, document, elapsed = collect_text(
        [folder], remote_checks=True, seconds=8.0, max_parallel=1
    )

    assert len(recorder.discoveries()) == 1
    for path in paths:
        assert_fell_back_to_cached(group_of(document, path), "timed_out")
    assert elapsed < 10.0


def test_blocked_query_keeps_a_later_comparison_from_starting(tmp_path, recorders):
    recorder = recorders()
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served, "quick.git")
    lagging = web_clone(recorder, "quick", remote, folder / "lagging", "lagging")
    pushed = pushed_then_reset(lagging, remote)
    stuck = make_remote(served, "stuck.git")
    recorder.add("stuck", stuck, "block")
    web_clone(recorder, "stuck", stuck, folder / "stuck clone", "stuck")

    _, document, _ = collect_text(
        [folder], remote_checks=True, seconds=8.0, max_parallel=2
    )

    group = group_of(document, lagging)
    assert group["remote"]["check"]["status"] == "live"
    assert group["default_branch"]["live_sha"] == pushed
    assert find_clone(document, lagging)["local_default"]["vs_live"] == {
        "basis": "live", "relation": "unknown", "ahead": None, "behind": None,
        "objects_from": None,
    }


def test_refused_credentials_fail_once_from_the_first_clone_without_prompting(
    tmp_path, recorders
):
    recorder = recorders()
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    bare = make_remote(served, "auth.git")
    recorder.add("auth", bare, "401")
    first = web_clone(recorder, "auth", bare, folder / "Auth Clone 10", "Auth Clone 10")
    second = web_clone(recorder, "auth", bare, folder / "Auth Clone 2", "Auth Clone 2")

    _, document, _ = collect_text([folder], remote_checks=True, call_cap_seconds=2.0)

    assert_fell_back_to_cached(group_of(document, first), "authentication_failed")
    assert group_of(document, second) is group_of(document, first)
    assert [item[2] for item in recorder.discoveries()] == ["Auth Clone 2"]


@posix_only
def test_no_askpass_program_runs_for_a_refused_remote(tmp_path, recorders, monkeypatch):
    recorder = recorders()
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    bare = make_remote(served, "auth.git")
    recorder.add("auth", bare, "401")
    repository = web_clone(recorder, "auth", bare, folder / "Auth Clone 2", "Auth Clone 2")
    markers = {name: tmp_path / f"marker-{name}" for name in ("git", "ssh", "core")}
    scripts = tmp_path / "scripts"
    git_script = marker_script(scripts, "git-askpass", markers["git"], "echo secret")
    ssh_script = marker_script(scripts, "ssh-askpass", markers["ssh"], "echo secret")
    core_script = marker_script(scripts, "core-askpass", markers["core"], "echo secret")
    git(repository, "config", "core.askPass", str(core_script))
    monkeypatch.setenv("GIT_ASKPASS", str(git_script))
    monkeypatch.setenv("SSH_ASKPASS", str(ssh_script))

    document = live([folder], call_cap_seconds=2.0)

    assert group_of(document, repository)["remote"]["check"]["reason"] == "authentication_failed"
    assert not any(marker.exists() for marker in markers.values())


def test_branch_deleted_during_the_remote_query_is_never_a_counted_comparison(
    tmp_path, recorders
):
    recorder = recorders()
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    bare = make_remote(served, "shared.git")
    scratch = clone(bare, tmp_path / "scratch")
    git(scratch, "checkout", "-q", "-b", "feature")
    commit_file(scratch, "f.txt", "f\n")
    git(scratch, "push", "-q", "origin", "feature")
    web_clone(recorder, "shared", bare, folder / "A", "A")
    other = folder / "B"
    web_clone(recorder, "shared", bare, other, "B")
    git(other, "checkout", "-q", "-b", "feature", "origin/feature")
    commit_file(other, "unpushed.txt", "u\n")
    git(other, "checkout", "-q", "trunk")

    control = live([folder])
    assert branch_of(find_clone(control, other), "feature")["vs_remote"] == {
        "basis": "live", "relation": "ahead", "ahead": 1, "behind": 0, "objects_from": None
    }

    def delete_and_prune():
        git(other, "branch", "-q", "-D", "feature")
        git(other, "reflog", "expire", "--expire=now", "--all")
        git(other, "prune", "--expire=now")

    recorder.behaviors["/shared.git"] = ("hook", delete_and_prune)
    changed = live([folder])

    assert git(other, "rev-parse", "--verify", "-q", "refs/heads/feature",
               check=False).returncode != 0

    def without_feature(document):
        stripped = json.loads(json.dumps(document))
        for item in all_clones(stripped):
            if item["branches"] is not None:
                item["branches"] = [b for b in item["branches"] if b["name"] != "feature"
                                    or item["path"] != real(other)]
        stripped.pop("observed_at")
        stripped.pop("checkouts_digest")
        return stripped

    listed = [b for b in find_clone(changed, other)["branches"] if b["name"] == "feature"]
    for branch in listed:
        assert branch["vs_remote"]["relation"] in {"differs", "unknown"}
        assert branch["vs_remote"]["basis"] == "live"
        assert branch["vs_remote"]["ahead"] is None
    assert without_feature(changed) == without_feature(control)


# ---------------------------------------------------------------------------
# SSH in batch mode unless the user configured their own command (POSIX only)
# ---------------------------------------------------------------------------


def argv_logger(directory: Path, name: str, log: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text(
        f'#!/bin/sh\necho "$@" >> "{log}"\n'
        "echo 'git@example.invalid: Permission denied (publickey).' >&2\nexit 255\n"
    )
    script.chmod(0o755)
    return script


@pytest.fixture
def ssh_world(tmp_path, monkeypatch):
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(make_remote(served), folder / "ssh clone")
    git(repository, "remote", "set-url", "origin", "ssh://git@example.invalid/r.git")
    fake_log = tmp_path / "fake.log"
    argv_logger(tmp_path / "bin", "ssh", fake_log)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    user_log = tmp_path / "user.log"
    user = argv_logger(tmp_path / "user", "user-ssh", user_log)
    return folder, repository, fake_log, user, user_log


@posix_only
def test_ssh_runs_in_batch_mode_when_the_user_has_no_ssh_command(ssh_world):
    folder, repository, fake_log, _, _ = ssh_world

    document = live([folder])

    assert_fell_back_to_cached(group_of(document, repository), "authentication_failed")
    assert "-o BatchMode=yes" in fake_log.read_text()


@posix_only
@pytest.mark.parametrize("how", ["core.sshCommand", "GIT_SSH", "GIT_SSH_COMMAND"])
def test_a_user_ssh_command_runs_unmodified(ssh_world, monkeypatch, how):
    folder, repository, fake_log, user, user_log = ssh_world
    if how == "core.sshCommand":
        git(repository, "config", "core.sshCommand", str(user))
    else:
        monkeypatch.setenv(how, str(user))

    document = live([folder])

    assert_fell_back_to_cached(group_of(document, repository), "authentication_failed")
    assert not fake_log.exists()
    assert user_log.exists() and "BatchMode" not in user_log.read_text()


# ---------------------------------------------------------------------------
# Sibling borrowing: counts for objects a clone lacks, without writing to it
# ---------------------------------------------------------------------------


def object_files(repository: Path) -> list[tuple[str, int]]:
    root = repository / ".git" / "objects"
    return sorted(
        (item.relative_to(root).as_posix(), item.stat().st_size)
        for item in root.rglob("*")
        if item.is_file()
    )


def borrowed_world(tmp_path: Path, holders: tuple[str, ...] = ("holder",)):
    """A lagging clone and clones that pushed two commits the lagging one lacks."""
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    lagging = clone(remote, folder / "a lagging")
    held = []
    for name in holders:
        held.append(clone(remote, folder / name))
    first = held[0]
    commit_file(first, "one.txt", "1\n")
    head = commit_file(first, "two.txt", "2\n")
    push_by_url(first, remote, "trunk")
    for repository in held[1:]:
        git(repository, "fetch", "-q", "origin")
        git(repository, "reset", "-q", "--hard", "origin/trunk")
    return folder, remote, lagging, held, head


def test_lagging_clone_counts_with_objects_borrowed_from_a_sibling(tmp_path):
    folder, _remote, lagging, (holder,), head = borrowed_world(tmp_path)
    before = (object_files(lagging), git(lagging, "count-objects", "-v").stdout)
    assert git(lagging, "cat-file", "-e", head, check=False).returncode != 0

    document = live([folder])

    item = find_clone(document, lagging)
    assert group_of(document, lagging)["default_branch"]["live_sha"] == head
    assert item["local_default"]["vs_live"] == {
        "basis": "live", "relation": "behind", "ahead": 0, "behind": 2,
        "objects_from": real(holder),
    }
    assert item["cached_vs_live"]["objects_from"] == real(holder)
    assert (object_files(lagging), git(lagging, "count-objects", "-v").stdout) == before
    assert not (lagging / ".git" / "objects" / "info" / "alternates").exists()
    assert git(lagging, "cat-file", "-e", head, check=False).returncode != 0


def test_first_sibling_in_natural_order_that_holds_the_head_lends(tmp_path):
    folder, _remote, lagging, holders, head = borrowed_world(
        tmp_path, ("holder 10", "holder 2")
    )

    item = find_clone(live([folder]), lagging)

    assert item["local_default"]["vs_live"]["objects_from"] == real(folder / "holder 2")
    assert item["local_default"]["vs_live"]["behind"] == 2


def test_removed_holder_leaves_the_lagging_clone_with_differs(tmp_path):
    folder, _remote, lagging, (holder,), _head = borrowed_world(tmp_path)
    remove_tree(holder)

    comparison = find_clone(live([folder]), lagging)["local_default"]["vs_live"]

    assert comparison == {
        "basis": "live", "relation": "differs", "ahead": None, "behind": None,
        "objects_from": None,
    }


def test_inherited_alternates_value_never_makes_an_unborrowed_count(tmp_path, monkeypatch):
    folder, _remote, lagging, (holder,), _head = borrowed_world(tmp_path)
    kept = tmp_path / "kept"
    shutil.move(str(holder), str(kept))
    monkeypatch.setenv(
        "GIT_ALTERNATE_OBJECT_DIRECTORIES", str(kept / ".git" / "objects")
    )

    comparison = find_clone(live([folder]), lagging)["local_default"]["vs_live"]

    assert comparison["relation"] == "differs"
    assert comparison["behind"] is None and comparison["objects_from"] is None


@pytest.mark.skipif(ON_WINDOWS, reason="the slow git stand-in is a shell script")
def test_sibling_probe_cut_by_the_budget_is_unknown(tmp_path, monkeypatch):
    folder, _remote, lagging, (holder,), _head = borrowed_world(tmp_path)
    real_git = shutil.which("git")
    shim = tmp_path / "shim"
    shim.mkdir()
    script = shim / "git"
    script.write_text(
        "#!/bin/sh\n"
        f'case "$*" in *"-C {real(holder)} rev-list --no-walk"*) sleep 5;; esac\n'
        f'exec "{real_git}" "$@"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")

    comparison = find_clone(
        live([folder], call_cap_seconds=1.0), lagging
    )["local_default"]["vs_live"]

    assert comparison == {
        "basis": "live", "relation": "unknown", "ahead": None, "behind": None,
        "objects_from": None,
    }


def test_partial_and_shallow_holders_never_lend_and_gain_no_pack(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    allow_filters(remote)
    lagging = clone(remote.as_uri(), folder / "a lagging")
    partial = clone(remote.as_uri(), folder / "b partial", "--filter=blob:none")
    shallow = clone(remote.as_uri(), folder / "c shallow", "--depth", "1")
    head = push_commit(remote, "trunk", tmp_path)
    git(partial, "fetch", "-q", "origin")
    git(shallow, "fetch", "-q", "--depth", "1")
    for holder in (partial, shallow):
        assert git(holder, "cat-file", "-e", head, check=False).returncode == 0
    packs = pack_listing(partial)

    document = live([folder])

    assert group_of(document, lagging)["default_branch"]["live_sha"] == head
    assert find_clone(document, lagging)["local_default"]["vs_live"] == {
        "basis": "live", "relation": "differs", "ahead": None, "behind": None,
        "objects_from": None,
    }
    assert pack_listing(partial) == packs


def test_live_documents_parse_and_keep_a_stable_digest(tmp_path):
    folder, remote, lagging, (holder,), _head = borrowed_world(tmp_path)
    gone = clone(make_remote(tmp_path / "served", "gone.git"), folder / "gone clone")
    remove_tree(tmp_path / "served" / "gone.git")

    first_text, first, _ = collect_text([folder], remote_checks=True)
    time.sleep(1.1)
    second_text, second, _ = collect_text([folder], remote_checks=True)
    off = collect_text([folder])[1]

    assert first["schema_version"] == 1
    borrowed = find_clone(first, lagging)["local_default"]["vs_live"]
    assert borrowed["objects_from"] == real(holder)
    assert group_of(first, gone)["remote"]["check"]["reason"] == "not_found"
    assert off["remote_checks"] == "off"
    assert first["observed_at"] != second["observed_at"]
    assert first["checkouts_digest"] == second["checkouts_digest"]
    assert {k: v for k, v in first.items() if k != "observed_at"} == {
        k: v for k, v in second.items() if k != "observed_at"
    }
    for document in (first, second, off):
        for group in document["repositories"]:
            for item in group["clones"]:
                assert set(item) == set(models._CLONE_KEYS)
                for comparison in comparisons_of(item):
                    assert set(comparison) == set(models._COMPARISON_KEYS)

    push_commit(remote, "trunk", tmp_path)
    advanced = live([folder])
    assert advanced["checkouts_digest"] != first["checkouts_digest"]
