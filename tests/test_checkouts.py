"""Behavioral proofs for the checkout facts collector, against real repositories.

Every state is produced by git itself in a temporary directory under an
isolated home, so neither the developer's nor the runner's git configuration
changes any outcome.  Timeouts are lowered only through collector parameters.
"""

from __future__ import annotations

import contextlib
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
from collections import Counter
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
[gc]
\tauto = 0
[maintenance]
\tauto = false
[receive]
\tautogc = false
\tunpackLimit = 10000
"""


GITHUB_SCRUBBED = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GH_HOST",
    "GH_REPO",
    "GH_CONFIG_DIR",
)


@pytest.fixture(autouse=True, scope="module")
def outer_github_environment(tmp_path_factory):
    """Hold every GitHub variable in the process environment, as an outer shell
    would, before the per-test guard builds each test's environment."""
    config = tmp_path_factory.mktemp("outer-gh-config")
    (config / "hosts.yml").write_text("github.com:\n    user: outer-user\n")
    values = {name: f"outer-{name.lower()}" for name in GITHUB_SCRUBBED}
    values["GH_CONFIG_DIR"] = str(config)
    with pytest.MonkeyPatch.context() as outer:
        for name, value in values.items():
            outer.setenv(name, value)
        yield values


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
    # No test may reach the network through the user's GitHub credentials; a test
    # that needs one of these variables sets it in its own body.
    for name in GITHUB_SCRUBBED:
        monkeypatch.delenv(name, raising=False)
    return home


PINNED_HOUSEKEEPING = {
    "gc.auto": "0",
    "maintenance.auto": "false",
    "receive.autogc": "false",
    "receive.unpackLimit": "10000",
}


def test_automatic_housekeeping_is_off_in_every_repository_a_test_builds(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    remote = make_remote(served)
    work = clone(remote, tmp_path / "work")
    for repository in (remote, work):
        for key, value in PINNED_HOUSEKEEPING.items():
            assert git(repository, "config", "--get", key).stdout.strip() == value, key


def push_many_objects(work: Path, count: int) -> None:
    for number in range(count):
        (work / f"bulk-{number}.txt").write_bytes(f"bulk {number} {time.time_ns()}\n".encode())
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "bulk")
    git(work, "push", "-q", "origin", "trunk")


def test_a_push_of_more_than_the_default_unpack_limit_leaves_the_remote_loose(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    remote = make_remote(served)
    work = clone(remote, tmp_path / "work")

    push_many_objects(work, 150)

    assert len(loose_objects(remote)) > 150
    assert pack_names(remote) == []
    before = store_times(remote)
    # A second push runs receive-pack again: nothing is packed or removed.  Modification
    # times are not compared here because git freshens a loose object it receives again.
    push_many_objects(work, 150)
    after = store_times(remote)
    assert pack_names(remote) == []
    assert set(before) < set(after)


def test_the_pack_probe_sees_a_pack_when_the_remote_asks_for_one(tmp_path):
    """Control: the loose result above is the pin's doing, and the probe can see a pack."""
    served = tmp_path / "served"
    served.mkdir()
    remote = make_remote(served)
    git(remote, "config", "receive.unpackLimit", "100")
    work = clone(remote, tmp_path / "work")

    push_many_objects(work, 150)

    assert pack_names(remote) != []


# The whole second in which the last helper-run git command finished. A saved remote
# check only yields to a tracking ref that moved at or after its own whole second, so a
# networked collection waits for the next second before it takes its time.
LAST_GIT_SECOND = [0]


def git(cwd: Path | str, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, capture_output=True, text=True, check=False
    )
    LAST_GIT_SECOND[0] = int(time.time())
    if check and result.returncode != 0:
        raise AssertionError(f"git {arguments} failed: {result.stderr}")
    return result


def real(path: Path | str) -> str:
    return os.path.realpath(path)


def commit_file(repo: Path, name: str, content: str, message: str | None = None) -> str:
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exact bytes: text mode would write CRLF on Windows and make identical
    # changes differ from those made with ``write_bytes``.
    target.write_bytes(content.encode())
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
    if not packed_file.parent.exists():  # a bare repository
        packed_file = repository / "packed-refs"
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
    if options.get("remote_checks"):
        time.sleep(max(0.0, LAST_GIT_SECOND[0] + 1 - time.time()))
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


def test_worktree_whose_directory_is_gone_is_prunable_and_unattached(tmp_path):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "repo")
    gone = folder / "gone"
    git(repository, "worktree", "add", "-q", str(gone), "-b", "gone")
    shutil.rmtree(gone)

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
        "cached_vs_remote", "stash_count", "worktrees", "branches",
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
            assert repository["default_branch"]["sha"] is None
        for item in repository["clones"]:
            if item["status"] != "ok":
                continue
            assert item["cached_vs_remote"] is None
            comparisons = [branch["vs_remote"] for branch in item["branches"]]
            if item["local_default"] is not None:
                assert item["local_default"]["vs_remote"] is None
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
    assert document["repositories"][0]["default_branch"] == {"name": "trunk", "sha": None}
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

    assert document["repositories"][0]["default_branch"] == {"name": None, "sha": None}
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
    assert orphan["cached_vs_remote"] is None
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


def rederive(document: dict[str, Any]) -> dict[str, Any]:
    """Give every ``ok`` clone the Removable fields its present facts derive.

    A clone whose facts the derivation cannot read (a mutation that broke their
    shape) keeps its fields: the structural checks refuse it before they matter.
    """
    for item in all_clones(document):
        if item["status"] != "ok":
            continue
        try:
            derived = models.derive_removable(item)
        except (KeyError, TypeError):
            continue
        for branch, verdict in zip(item["branches"], derived["branches"]):
            branch["removable"] = verdict
        for worktree, verdict in zip(item["worktrees"], derived["worktrees"]):
            worktree["removable"] = verdict
        item["removable_count"] = derived["removable_count"]
    return document


def refusal_of(document: dict[str, Any]) -> str:
    """Return the message of the refusal of ``document``, which must carry a valid digest."""
    with pytest.raises(models.ProtocolError) as error:
        models.parse_checkouts(redigest(document))
    return str(error.value)


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
        local_default=None, cached_vs_remote=None, stash_count=None, worktrees=None, branches=None,
        verdict_base=None,
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
        mutate_schema(1),
        mutate_schema(2),
        mutate_schema(3),
        mutate_schema(5),
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
    assert "removable" not in refusal_of(rederive(broken))


def rename_default_branch_sha(document):
    default_branch = document["repositories"][0]["default_branch"]
    default_branch["live_sha"] = default_branch.pop("sha")


def rename_local_default_vs_remote(document):
    local = first_clone(document)["local_default"]
    local["vs_live"] = local.pop("vs_remote")


def rename_cached_vs_remote(document):
    item = first_clone(document)
    item["cached_vs_live"] = item.pop("cached_vs_remote")


@pytest.mark.parametrize(
    "rename",
    [rename_default_branch_sha, rename_local_default_vs_remote, rename_cached_vs_remote],
    ids=lambda rename: rename.__name__,
)
def test_parser_refuses_a_schema_4_document_with_an_old_field_name(sample, rename):
    document, _folder = sample
    assert models.parse_checkouts(redigest(doc_copy(document)))
    broken = doc_copy(document)
    rename(broken)
    assert "invalid shape" in refusal_of(broken)


def test_parser_refuses_a_schema_3_document_under_a_valid_digest(sample):
    document, _folder = sample
    previous = doc_copy(document)
    previous["schema_version"] = 3
    assert "schema" in refusal_of(previous)


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


def verdict_of(state, rung=None, reason=None, pull_request=None):
    return {"state": state, "rung": rung, "reason": reason, "pull_request": pull_request}


def merged_verdict():
    return verdict_of("merged", "ancestor")


def other_branches(clone_document):
    base = clone_document["verdict_base"]["name"]
    return [item for item in clone_document["branches"] if item["name"] != base]


def detach(document, verdict):
    """Detach the outside worktree of the first clone and give it ``verdict``."""
    clone_document = first_clone(document)
    worktree = next(item for item in clone_document["worktrees"] if not item["main"])
    worktree["head"] = {"branch": None, "sha": worktree["head"]["sha"]}
    worktree["verdict"] = verdict
    return worktree


def undecided_without_a_base(document):
    """Drop the first clone's base, as for a group that agrees on no default name."""
    clone_document = first_clone(document)
    clone_document["verdict_base"] = None
    clone_document["containment"] = "no_base"
    for branch in clone_document["branches"]:
        branch["verdict"] = verdict_of("unknown", reason="no_base")


def without_remote(document):
    """Reduce the first clone to a repository with no remote and so no base."""
    clone_document = first_clone(document)
    document["repositories"] = [
        {"remote": None, "default_branch": None, "pr_lookup": None, "clones": [clone_document]}
    ]
    clone_document.update(cached_default=None, local_default=None, cached_vs_remote=None)
    for branch in clone_document["branches"]:
        branch.update(vs_remote=None, upstream_mismatch=False)
    undecided_without_a_base(document)
    return clone_document


def mutate_verdict(state, rung, reason):
    def mutate(document):
        other_branches(first_clone(document))[0]["verdict"] = verdict_of(state, rung, reason)

    return mutate


def mutate_verdict_extra_key(document):
    other_branches(first_clone(document))[0]["verdict"]["extra"] = 1


def mutate_base_extra_key(document):
    first_clone(document)["verdict_base"]["extra"] = 1


def mutate_base_basis(value):
    def mutate(document):
        first_clone(document)["verdict_base"]["basis"] = value

    return mutate


def mutate_base_lender_with_cached_basis(document):
    base = first_clone(document)["verdict_base"]
    assert base["basis"] == "cached"
    base["objects_from"] = first_clone(document)["path"]


def mutate_verdict_on_the_base_branch(document):
    base = first_clone(document)["verdict_base"]["name"]
    branch_of(first_clone(document), base)["verdict"] = merged_verdict()


def mutate_null_verdict_on_another_branch(document):
    other_branches(first_clone(document))[0]["verdict"] = None


def mutate_verdict_on_a_worktree_on_a_branch(document):
    worktree = next(item for item in first_clone(document)["worktrees"] if not item["main"])
    assert worktree["head"]["branch"] is not None
    worktree["verdict"] = merged_verdict()


def mutate_verdict_on_a_non_ok_worktree(document):
    worktree = next(item for item in first_clone(document)["worktrees"] if not item["main"])
    worktree.update(
        status="failed", head=None, changes=None, operations=None, index_lock=None,
        verdict=merged_verdict(),
    )


def mutate_null_verdict_on_a_detached_worktree(document):
    detach(document, None)


def mutate_incomplete_clone_on_a_full_clone(document):
    other_branches(first_clone(document))[0]["verdict"] = verdict_of(
        "unknown", reason="incomplete_clone"
    )


def mutate_incomplete_clone_on_a_detached_worktree(document):
    detach(document, verdict_of("unknown", reason="incomplete_clone"))


def mutate_git_too_old_on_a_new_git(document):
    # Set rather than read: the collecting git differs from host to host.
    document["git"]["version"] = "2.43.0"
    mutate_git_too_old_on_a_new_git_verdict(document)


def mutate_git_too_old_on_the_floor_version(document):
    document["git"]["version"] = "2.39.0"
    mutate_git_too_old_on_a_new_git_verdict(document)


def mutate_git_too_old_on_a_newer_vendor_version(document):
    document["git"]["version"] = "2.39.1.windows.1"
    mutate_git_too_old_on_a_new_git_verdict(document)


def mutate_git_too_old_on_a_detached_worktree(document):
    detach(document, verdict_of("unknown", reason="git_too_old"))


def mutate_git_too_old_on_a_new_git_verdict(document):
    other_branches(first_clone(document))[0]["verdict"] = verdict_of(
        "unknown", reason="git_too_old"
    )


def mutate_base_null_with_a_decided_branch(document):
    undecided_without_a_base(document)
    other_branches_target = next(
        item for item in first_clone(document)["branches"] if item["name"] != "trunk"
    )
    other_branches_target["verdict"] = merged_verdict()


def mutate_base_null_with_a_decided_worktree(document):
    undecided_without_a_base(document)
    detach(document, merged_verdict())


def mutate_no_base_with_a_base(document):
    other_branches(first_clone(document))[0]["verdict"] = verdict_of("unknown", reason="no_base")


def mutate_no_base_on_a_detached_worktree_with_a_base(document):
    detach(document, verdict_of("unknown", reason="no_base"))


def mutate_base_on_a_clone_without_a_remote(document):
    clone_document = without_remote(document)
    clone_document["verdict_base"] = {
        "name": "trunk", "sha": "1" * 40, "basis": "live", "objects_from": None
    }
    for branch in clone_document["branches"]:
        branch["verdict"] = None if branch["name"] == "trunk" else merged_verdict()


def mutate_base_on_a_clone_that_is_not_ok(document):
    clone_document = first_clone(document)
    base = clone_document["verdict_base"]
    refused_clone(document)
    clone_document["verdict_base"] = base


def mutate_missing_verdict_key(document):
    del other_branches(first_clone(document))[0]["verdict"]["reason"]


REJECTED_VERDICT_DOCUMENTS = {
    "merged_without_rung": mutate_verdict("merged", None, None),
    "merged_with_rung_none": mutate_verdict("merged", "none", None),
    "merged_with_foreign_rung": mutate_verdict("merged", "history", None),
    "not_merged_with_rung_ancestor": mutate_verdict("not_merged", "ancestor", None),
    "not_merged_without_rung": mutate_verdict("not_merged", None, None),
    "unknown_with_rung": mutate_verdict("unknown", "ancestor", "failed"),
    "unknown_with_rung_none": mutate_verdict("unknown", "none", "failed"),
    "unknown_without_reason": mutate_verdict("unknown", None, None),
    "reason_on_merged": mutate_verdict("merged", "ancestor", "failed"),
    "reason_on_not_merged": mutate_verdict("not_merged", "none", "failed"),
    "foreign_state": mutate_verdict("pending", None, None),
    "foreign_reason": mutate_verdict("unknown", None, "objects_missing"),
    "extra_verdict_key": mutate_verdict_extra_key,
    "missing_verdict_key": mutate_missing_verdict_key,
    "extra_base_key": mutate_base_extra_key,
    "foreign_base_basis": mutate_base_basis("remote"),
    "null_base_basis": mutate_base_basis(None),
    "lender_under_the_cached_basis": mutate_base_lender_with_cached_basis,
    "verdict_on_the_base_branch": mutate_verdict_on_the_base_branch,
    "null_verdict_on_another_branch": mutate_null_verdict_on_another_branch,
    "verdict_on_a_worktree_on_a_branch": mutate_verdict_on_a_worktree_on_a_branch,
    "verdict_on_a_non_ok_worktree": mutate_verdict_on_a_non_ok_worktree,
    "null_verdict_on_a_detached_worktree": mutate_null_verdict_on_a_detached_worktree,
    "incomplete_clone_on_a_full_clone": mutate_incomplete_clone_on_a_full_clone,
    "incomplete_clone_on_a_detached_worktree": mutate_incomplete_clone_on_a_detached_worktree,
    "git_too_old_on_a_new_git": mutate_git_too_old_on_a_new_git,
    "git_too_old_on_the_floor_version": mutate_git_too_old_on_the_floor_version,
    "git_too_old_on_a_vendor_version": mutate_git_too_old_on_a_newer_vendor_version,
    "git_too_old_on_a_detached_worktree": mutate_git_too_old_on_a_detached_worktree,
    "null_base_with_a_decided_branch": mutate_base_null_with_a_decided_branch,
    "null_base_with_a_decided_worktree": mutate_base_null_with_a_decided_worktree,
    "no_base_with_a_base": mutate_no_base_with_a_base,
    "no_base_worktree_with_a_base": mutate_no_base_on_a_detached_worktree_with_a_base,
    "base_without_a_remote": mutate_base_on_a_clone_without_a_remote,
    "base_on_a_clone_that_is_not_ok": mutate_base_on_a_clone_that_is_not_ok,
}


@pytest.mark.parametrize("mutation", list(REJECTED_VERDICT_DOCUMENTS), ids=str)
def test_parser_rejects_a_verdict_invariant_violation_under_a_valid_digest(sample, mutation):
    document, _folder = sample
    assert models.parse_checkouts(redigest(doc_copy(document)))
    broken = doc_copy(document)
    REJECTED_VERDICT_DOCUMENTS[mutation](broken)
    assert "removable" not in refusal_of(rederive(broken))


def test_parser_accepts_every_decided_and_undecided_verdict_shape(sample):
    document, _folder = sample
    shapes = [
        verdict_of("merged", rung)
        for rung in ("ancestor", "patch_equivalent", "squash_equivalent", "tree_equal")
    ] + [verdict_of("not_merged", "none")] + [
        verdict_of("unknown", reason=reason)
        for reason in ("history_limit", "deadline", "failed")
    ]
    for shape in shapes:
        accepted = doc_copy(document)
        other_branches(first_clone(accepted))[0]["verdict"] = shape
        assert models.parse_checkouts(redigest(rederive(accepted)))


def test_parser_accepts_a_live_base_with_a_lender_and_a_clone_without_the_base_branch(sample):
    document, _folder = sample
    accepted = doc_copy(document)
    clone_document = first_clone(accepted)
    clone_document["verdict_base"].update(basis="live", objects_from=clone_document["path"])
    clone_document["branches"] = [
        item for item in clone_document["branches"] if item["name"] != "trunk"
    ]
    assert all(item["verdict"] is not None for item in clone_document["branches"])
    assert models.parse_checkouts(redigest(accepted))


def test_parser_accepts_a_detached_checkout_with_a_verdict(sample):
    document, _folder = sample
    accepted = doc_copy(document)
    detach(accepted, verdict_of("merged", "tree_equal"))
    assert models.parse_checkouts(redigest(accepted))


def test_parser_accepts_a_clone_with_a_base_and_no_other_branch(sample):
    document, _folder = sample
    accepted = doc_copy(document)
    clone_document = first_clone(accepted)
    clone_document["branches"] = [
        item for item in clone_document["branches"] if item["name"] == "trunk"
    ]
    assert clone_document["verdict_base"] is not None
    assert models.parse_checkouts(redigest(rederive(accepted)))


def test_parser_accepts_documents_without_a_base(sample):
    document, _folder = sample
    undecided = doc_copy(document)
    undecided_without_a_base(undecided)
    detach(undecided, verdict_of("unknown", reason="no_base"))
    assert models.parse_checkouts(redigest(rederive(undecided)))
    assert all(
        item["verdict"] == verdict_of("unknown", reason="no_base")
        for item in first_clone(undecided)["branches"]
    )

    remoteless = doc_copy(document)
    without_remote(remoteless)
    assert first_clone(remoteless)["verdict_base"] is None
    assert models.parse_checkouts(redigest(rederive(remoteless)))


def test_parser_accepts_limit_reasons_where_the_clone_has_the_limit(sample):
    document, _folder = sample
    limited = doc_copy(document)
    clone_document = first_clone(limited)
    clone_document["shallow"] = True
    clone_document["containment"] = "incomplete"
    other_branches(clone_document)[0]["verdict"] = verdict_of(
        "unknown", reason="incomplete_clone"
    )
    assert models.parse_checkouts(redigest(rederive(limited)))
    clone_document["shallow"] = False
    clone_document["partial_clone"] = True
    detach(limited, verdict_of("unknown", reason="incomplete_clone"))
    assert models.parse_checkouts(redigest(rederive(limited)))

    old = doc_copy(document)
    old["git"]["version"] = "2.38.1"
    for item in all_clones(old):
        item["containment"] = "git_too_old"
    other_branches(first_clone(old))[0]["verdict"] = verdict_of("unknown", reason="git_too_old")
    assert models.parse_checkouts(redigest(rederive(old)))
    old["git"]["version"] = "2.9.5"
    assert models.parse_checkouts(redigest(rederive(old)))


def test_parser_refuses_the_retired_schema_even_with_a_valid_digest(sample):
    document, _folder = sample
    retired = doc_copy(document)
    retired["schema_version"] = 1
    with pytest.raises(models.ProtocolError, match="schema"):
        models.parse_checkouts(redigest(retired))


NULLABLE_CLONE_FACTS = {
    "bare": (False, 0, "", []),
    "partial_clone": (False, 0, "", []),
    "shallow": (False, 0, "", []),
    "cached_default": ({}, "", [], False),
    "local_default": ({}, "", [], False),
    "cached_vs_remote": ({}, "", [], False),
    "stash_count": (0, False, "", []),
    "worktrees": ([], "", 0, False),
    "branches": ([], "", 0, False),
    "verdict_base": ({}, "", [], False, 0),
    "containment": ("complete", "", [], False, 0),
    "removable_count": ({}, "", [], False, 0),
}
NULLABLE_WORKTREE_FACTS = {
    "head": ({}, "", [], False),
    "changes": ({}, "", [], False, 0),
    "operations": ([], "", 0, False),
    "index_lock": (False, 0, "", []),
    "verdict": ({}, "", [], False, 0),
}


def refused_clone(document: dict[str, Any]) -> dict[str, Any]:
    clone_document = first_clone(document)
    clone_document.update(
        status="failed", bare=None, partial_clone=None, shallow=None, cached_default=None,
        local_default=None, cached_vs_remote=None, stash_count=None, worktrees=None, branches=None,
        verdict_base=None, containment=None, removable_count=None,
    )
    return clone_document


def test_parser_accepts_non_ok_items_with_null_facts(sample):
    document, _folder = sample
    broken = doc_copy(document)
    refused_clone(broken)
    other = broken["repositories"][0]["clones"][1]
    worktree = other["worktrees"][0]
    worktree.update(
        status="timed_out", head=None, changes=None, operations=None, index_lock=None, verdict=None
    )
    assert models.parse_checkouts(redigest(broken))


def with_branch_count(document: dict[str, Any], count: int) -> dict[str, Any]:
    """Give the first clone exactly ``count`` branches, each extra one a copy of a decided one."""
    clone_document = first_clone(document)
    template = other_branches(clone_document)[0]
    kept = clone_document["branches"]
    extras = [
        {**template, "name": f"zz-extra-{n:03d}", "checked_out_in": []}
        for n in range(count - len(kept))
    ]
    clone_document["branches"] = sorted([*kept, *extras], key=models.branch_sort_key)
    assert len(clone_document["branches"]) == count
    return clone_document


def test_parser_limits_an_ok_clone_to_the_branch_cap(sample):
    document, _folder = sample
    at_cap = doc_copy(document)
    with_branch_count(at_cap, models.CHECKOUT_BRANCH_CAP)
    assert models.parse_checkouts(redigest(rederive(at_cap)))

    over = doc_copy(document)
    with_branch_count(over, models.CHECKOUT_BRANCH_CAP + 1)
    with pytest.raises(models.ProtocolError, match="more than 100 branches"):
        models.parse_checkouts(redigest(over))


def test_parser_accepts_a_clone_over_the_branch_cap_with_null_facts(sample):
    document, _folder = sample
    crowded = doc_copy(document)
    refused_clone(crowded)["status"] = "too_many_branches"
    assert models.parse_checkouts(redigest(crowded))


def test_parser_refuses_a_worktree_status_of_too_many_branches(sample):
    document, _folder = sample
    broken = doc_copy(document)
    main_worktree(first_clone(broken)).update(
        status="too_many_branches", head=None, changes=None, operations=None,
        index_lock=None, verdict=None,
    )
    with pytest.raises(models.ProtocolError):
        models.parse_checkouts(redigest(broken))


@pytest.mark.parametrize(
    "fact", ["stash_count", "branches", "verdict_base", "containment", "removable_count"]
)
def test_parser_refuses_a_clone_over_the_branch_cap_with_a_fact(sample, fact):
    document, _folder = sample
    ok_clone = first_clone(doc_copy(document))
    facts = {
        name: ok_clone[name]
        for name in ("stash_count", "branches", "verdict_base", "containment", "removable_count")
    }
    broken = doc_copy(document)
    refused_clone(broken)["status"] = "too_many_branches"
    first_clone(broken)[fact] = facts[fact]
    with pytest.raises(models.ProtocolError):
        models.parse_checkouts(redigest(broken))


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
    # Each clone holds the most branches one clone may (the default and 99
    # more), all at the default's tip, and long names fill the document.
    for index in range(48):
        repository = clone(remote, folder / f"repo-{index:03d}")
        head = git(repository, "rev-parse", "HEAD").stdout.strip()
        add_packed_refs(
            repository,
            {f"refs/heads/{index:03d}-{n:03d}-" + "x" * 230: head for n in range(99)},
        )
    assert len(git(repository, "for-each-ref", "refs/heads").stdout.splitlines()) == 100

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
        "cached_vs_remote", "stash_count", "worktrees", "branches",
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
    assert kept["cached_vs_remote"] is None
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
    found.append(item["cached_vs_remote"])
    if item["local_default"] is not None:
        found += [item["local_default"]["vs_remote"], item["local_default"]["vs_cached"]]
    return [comparison for comparison in found if comparison is not None]


def assert_fell_back_to_cached(group: dict[str, Any], reason: str) -> None:
    assert group["remote"]["check"] == {"status": "failed", "reason": reason}
    assert group["default_branch"]["sha"] is None
    for item in group["clones"]:
        if item["status"] != "ok":
            continue
        assert item["cached_vs_remote"] is None
        if item["local_default"] is not None:
            assert item["local_default"]["vs_remote"] is None
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
    assert document["schema_version"] == 4
    assert group["remote"]["check"] == {"status": "live", "reason": None}
    assert group["default_branch"] == {"name": "trunk", "sha": pushed}
    assert set(group["default_branch"]) == {"name", "sha"}
    item = find_clone(document, main)
    default = item["local_default"]
    assert set(default) == {"name", "sha", "vs_remote", "vs_cached"}
    assert "cached_vs_remote" in item
    assert default["name"] == "trunk"
    assert default["vs_remote"] == {
        "basis": "live", "relation": "behind", "ahead": 0, "behind": 1, "objects_from": None
    }
    assert default["vs_cached"]["basis"] == "cached"
    assert default["vs_cached"]["relation"] == "equal"
    assert item["cached_vs_remote"]["relation"] == "behind"
    assert (item["cached_vs_remote"]["behind"], item["cached_vs_remote"]["basis"]) == (1, "live")
    assert branch_of(item, "pushed")["vs_remote"]["relation"] == "equal"
    assert branch_of(item, "pushed")["vs_remote"]["basis"] == "live"
    assert branch_of(item, "private")["vs_remote"]["relation"] == "absent"
    assert branch_of(item, "private")["vs_remote"]["basis"] == "live"

    stale = find_clone(document, lagging)["local_default"]["vs_remote"]
    assert group_of(document, lagging)["default_branch"]["sha"] == outside_head
    assert stale == {
        "basis": "live", "relation": "differs", "ahead": None, "behind": None,
        "objects_from": None,
    }

    no_name = group_of(document, detached_clone)
    assert no_name["default_branch"] == {"name": None, "sha": head}
    assert find_clone(document, detached_clone)["local_default"] is None
    nothing = group_of(document, empty_clone)
    assert nothing["remote"]["check"] == {"status": "live", "reason": None}
    assert nothing["default_branch"]["sha"] is None


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
    assert item["local_default"]["vs_remote"] == {
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
    assert group_of(document, partial)["default_branch"]["sha"] == pushed
    assert item["local_default"]["vs_remote"] == {
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
    assert group["default_branch"]["sha"] == pushed
    assert find_clone(document, partial)["local_default"]["vs_remote"]["relation"] == "differs"
    assert find_clone(document, partial)["local_default"]["vs_remote"]["ahead"] is None
    assert find_clone(document, full)["local_default"]["vs_remote"]["relation"] == "equal"


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
    assert item["local_default"]["vs_remote"]["relation"] == "differs"
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
    assert group["default_branch"]["sha"] == pushed
    assert find_clone(document, lagging)["local_default"]["vs_remote"] == {
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
    assert group_of(document, lagging)["default_branch"]["sha"] == head
    assert item["local_default"]["vs_remote"] == {
        "basis": "live", "relation": "behind", "ahead": 0, "behind": 2,
        "objects_from": real(holder),
    }
    assert item["cached_vs_remote"]["objects_from"] == real(holder)
    assert (object_files(lagging), git(lagging, "count-objects", "-v").stdout) == before
    assert not (lagging / ".git" / "objects" / "info" / "alternates").exists()
    assert git(lagging, "cat-file", "-e", head, check=False).returncode != 0


def test_first_sibling_in_natural_order_that_holds_the_head_lends(tmp_path):
    folder, _remote, lagging, holders, head = borrowed_world(
        tmp_path, ("holder 10", "holder 2")
    )

    item = find_clone(live([folder]), lagging)

    assert item["local_default"]["vs_remote"]["objects_from"] == real(folder / "holder 2")
    assert item["local_default"]["vs_remote"]["behind"] == 2


def test_removed_holder_leaves_the_lagging_clone_with_differs(tmp_path):
    folder, _remote, lagging, (holder,), _head = borrowed_world(tmp_path)
    remove_tree(holder)

    comparison = find_clone(live([folder]), lagging)["local_default"]["vs_remote"]

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

    comparison = find_clone(live([folder]), lagging)["local_default"]["vs_remote"]

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
    )["local_default"]["vs_remote"]

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

    assert group_of(document, lagging)["default_branch"]["sha"] == head
    assert find_clone(document, lagging)["local_default"]["vs_remote"] == {
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

    assert first["schema_version"] == 4
    borrowed = find_clone(first, lagging)["local_default"]["vs_remote"]
    assert borrowed["objects_from"] == real(holder)
    assert group_of(first, gone)["remote"]["check"]["reason"] == "not_found"
    assert off["remote_checks"] == "off"
    assert_cached_everywhere(off)
    off_lagging = find_clone(off, lagging)
    assert group_of(off, lagging)["default_branch"]["sha"] is None
    assert off_lagging["local_default"]["vs_remote"] is None
    assert off_lagging["cached_vs_remote"] is None
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


def git_stand_in(tmp_path: Path, body: str) -> Path:
    """A ``git`` stand-in on PATH that runs ``body`` before the real git."""
    real_git = shutil.which("git")
    shim = tmp_path / "shim"
    shim.mkdir()
    script = shim / "git"
    script.write_text(f'#!/bin/sh\n{body}\nexec "{real_git}" "$@"\n')
    script.chmod(0o755)
    return shim


@pytest.mark.skipif(ON_WINDOWS, reason="the failing git stand-in is a shell script")
def test_sibling_probe_that_fails_gives_differs_not_unknown(tmp_path, monkeypatch):
    folder, _remote, lagging, (holder,), _head = borrowed_world(tmp_path)
    shim = git_stand_in(
        tmp_path,
        f'case "$*" in *"-C {real(holder)} rev-list --no-walk"*) exit 1;; esac',
    )
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")

    comparison = find_clone(live([folder]), lagging)["local_default"]["vs_remote"]

    assert comparison == {
        "basis": "live", "relation": "differs", "ahead": None, "behind": None,
        "objects_from": None,
    }


def test_a_holder_of_another_remote_never_lends(tmp_path):
    folder, _remote, lagging, (holder,), head = borrowed_world(tmp_path)
    other = make_remote(tmp_path / "served", "other.git")
    git(holder, "remote", "set-url", "origin", str(other))
    assert git(holder, "cat-file", "-e", head, check=False).returncode == 0

    document = live([folder])

    assert group_of(document, holder) is not group_of(document, lagging)
    assert find_clone(document, lagging)["local_default"]["vs_remote"] == {
        "basis": "live", "relation": "differs", "ahead": None, "behind": None,
        "objects_from": None,
    }


@pytest.mark.skipif(ON_WINDOWS, reason="the logging git stand-in is a shell script")
def test_no_sibling_is_probed_when_the_clone_holds_the_head(tmp_path, monkeypatch):
    folder, _remote, lagging, (holder,), _head = borrowed_world(tmp_path)
    git(holder, "fetch", "-q", "origin")
    git(lagging, "fetch", "-q", "origin")
    log = tmp_path / "calls.log"
    shim = git_stand_in(tmp_path, f'printf "%s\\n" "$*" >> "{log}"')
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")

    comparison = find_clone(live([folder]), lagging)["local_default"]["vs_remote"]

    assert comparison == {
        "basis": "live", "relation": "behind", "ahead": 0, "behind": 2,
        "objects_from": None,
    }
    calls = log.read_text().splitlines()
    assert any(f"-C {real(lagging)} rev-list --no-walk" in call for call in calls)
    assert not any(f"-C {real(holder)} rev-list --no-walk" in call for call in calls)


# ---------------------------------------------------------------------------
# Merge verdicts: the base and the git ladder
# ---------------------------------------------------------------------------

MERGED_ANCESTOR = ("merged", "ancestor", None)
MERGED_PATCH = ("merged", "patch_equivalent", None)
MERGED_SQUASH = ("merged", "squash_equivalent", None)
MERGED_TREE = ("merged", "tree_equal", None)
NOT_MERGED = ("not_merged", "none", None)


def unknown(reason: str) -> tuple[str, None, str]:
    return ("unknown", None, reason)


def verdict_tuple(verdict: dict[str, Any] | None):
    if verdict is None:
        return None
    return (verdict["state"], verdict["rung"], verdict["reason"])


def verdicts_of(clone_document: dict[str, Any]) -> dict[str, Any]:
    return {
        branch["name"]: verdict_tuple(branch["verdict"])
        for branch in clone_document["branches"]
    }


def write_bytes(repo: Path, name: str, content: bytes) -> None:
    (repo / name).write_bytes(content)
    git(repo, "add", name)


def commit_bytes(repo: Path, name: str, content: bytes, message: str) -> str:
    write_bytes(repo, name, content)
    git(repo, "commit", "-q", "-m", message)
    return rev(repo, "HEAD")


def rev(repo: Path, name: str) -> str:
    return git(repo, "rev-parse", name).stdout.strip()


def apply_patch(repo: Path, patch: bytes) -> None:
    result = subprocess.run(
        ["git", "apply", "--index"], cwd=repo, input=patch, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr


SHARED_LINES = "".join(f"line {number}\n" for number in range(1, 11))

EXPECTED_VERDICTS = {
    "merged-noff": MERGED_ANCESTOR,
    "picked": MERGED_PATCH,
    "squashed": MERGED_SQUASH,
    "split": MERGED_TREE,
    "unmerged": NOT_MERGED,
    "empty": NOT_MERGED,
    "mixed": NOT_MERGED,
    "cross": NOT_MERGED,
    "bin-differs": NOT_MERGED,
    "bin-picked": MERGED_PATCH,
    "blobdiff": MERGED_PATCH,
    "trunk": None,
}


def build_verdict_world(tmp_path: Path):
    """One clone of a remote whose default is ``trunk``, with a branch per rung.

    Each merge is made the way it happens in practice and pushed, so the
    remote's ``trunk`` (the base) holds it while the branches stay local.
    """
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    work = clone(remote, folder / "work")
    write_bytes(work, "bin.dat", b"\0one\n")
    write_bytes(work, "bin2.dat", b"\0two\n")
    commit_file(work, "shared.txt", SHARED_LINES, "shared")
    git(work, "push", "-q", "origin", "trunk")

    def branch(name: str, start: str = "trunk") -> None:
        git(work, "checkout", "-q", "-b", name, start)

    branch("split")
    write_bytes(work, "t1.txt", b"t1\n")
    write_bytes(work, "t2.txt", b"t2\n")
    git(work, "commit", "-q", "-m", "add both files at once")
    branch("merged-noff")
    commit_file(work, "a.txt", "a\n")
    branch("picked")
    commit_file(work, "p1.txt", "p1\n")
    commit_file(work, "p2.txt", "p2\n")
    branch("squashed")
    for number in (1, 2, 3):
        commit_file(work, f"s{number}.txt", f"s{number}\n")
    branch("unmerged")
    commit_file(work, "u.txt", "u\n")
    branch("empty")
    git(work, "commit", "-q", "--allow-empty", "-m", "empty")
    branch("side")
    commit_file(work, "side1.txt", "side1\n")
    branch("mixed")
    commit_file(work, "m1.txt", "m1\n")
    git(work, "merge", "-q", "--no-commit", "--no-ff", "side")
    write_bytes(work, "extra.txt", b"evil merge\n")
    git(work, "commit", "-q", "-m", "merge side with a change")
    git(work, "branch", "-q", "-D", "side")
    branch("bin-differs")
    commit_bytes(work, "bin.dat", b"\0branch\n", "binary on branch")
    branch("bin-picked")
    commit_bytes(work, "bin2.dat", b"\0picked\n", "binary picked")
    branch("blobdiff")
    commit_file(
        work, "shared.txt", SHARED_LINES.replace("line 1\n", "line one\n"), "edit the first line"
    )

    git(work, "checkout", "-q", "trunk")
    # Exact bytes, like the branch's copies: a text-mode write would end the
    # lines in CRLF on Windows and the two trees would genuinely differ.
    commit_bytes(work, "t1.txt", b"t1\n", "land t1 on trunk")
    commit_bytes(work, "t2.txt", b"t2\n", "land t2 on trunk")
    commit_file(work, "d1.txt", "diverged\n")
    commit_bytes(work, "bin.dat", b"\0trunk\n", "binary on trunk")
    commit_file(
        work, "shared.txt", SHARED_LINES.replace("line 10\n", "line ten\n"), "edit the last line"
    )
    git(work, "cherry-pick", "picked~1", "picked")
    git(work, "cherry-pick", "bin-picked", "blobdiff")
    git(work, "cherry-pick", rev(work, "mixed^1"), rev(work, "mixed^2"))
    git(work, "merge", "--squash", "squashed")
    git(work, "commit", "-q", "-m", "squash squashed")
    git(work, "merge", "-q", "--no-ff", "-m", "merge merged-noff", "merged-noff")

    # A criss-cross: two merge bases, and a later trunk commit whose patch is
    # the one `git diff <first merge base> cross` prints.
    branch("cross")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "a_cc.txt", "a\n")
    git(work, "checkout", "-q", "cross")
    side_tip = commit_file(work, "b_cc.txt", "b\n")
    git(work, "merge", "-q", "--no-commit", "--no-ff", "trunk")
    write_bytes(work, "extra_cc.txt", b"evil\n")
    git(work, "commit", "-q", "-m", "cross merges trunk")
    git(work, "checkout", "-q", "trunk")
    git(work, "merge", "-q", "--no-ff", "-m", "trunk merges cross", side_tip)
    first = git(work, "merge-base", "trunk", "cross").stdout.strip()
    assert len(git(work, "merge-base", "--all", "trunk", "cross").stdout.split()) == 2
    patch = subprocess.run(
        ["git", "diff", first, "cross"], cwd=work, capture_output=True, check=True
    ).stdout
    git(work, "rm", "-q", "a_cc.txt", "b_cc.txt")
    git(work, "commit", "-q", "-m", "drop the criss-cross files")
    apply_patch(work, patch)
    git(work, "commit", "-q", "-m", "land the criss-cross patch again")
    git(work, "push", "-q", "origin", "trunk")

    worktree = tmp_path / "squashed-worktree"
    git(work, "worktree", "add", "-q", "--detach", str(worktree), "squashed")
    return folder, remote, work, worktree


def test_each_rung_proves_exactly_its_case(tmp_path):
    folder, _remote, work, worktree = build_verdict_world(tmp_path)

    document = collect([folder])

    item = find_clone(document, work)
    assert item["verdict_base"] == {
        "name": "trunk", "sha": rev(work, "origin/trunk"), "basis": "cached",
        "objects_from": None,
    }
    assert verdicts_of(item) == EXPECTED_VERDICTS
    assert branch_of(item, "squashed")["sha"] == rev(work, "squashed")
    detached = find_worktree(item, worktree)
    assert detached["head"]["branch"] is None
    assert verdict_tuple(detached["verdict"]) == MERGED_SQUASH
    assert main_worktree(item)["verdict"] is None
    base = "origin/trunk"
    for name in ("picked", "bin-picked", "blobdiff"):
        lines = git(work, "cherry", base, name).stdout.split("\n")
        assert [line for line in lines if line] and all(line.startswith("-") for line in lines if line)
    assert git(work, "cherry", base, "empty").stdout.startswith("+")
    # No earlier rung proves the later cases.
    for name in ("picked", "squashed", "split", "cross", "mixed", "empty", "bin-differs"):
        proof = git(work, "merge-base", "--is-ancestor", name, base, check=False)
        assert proof.returncode == 1
    assert len(git(work, "merge-base", "--all", base, "cross").stdout.split()) == 2


INDENT_ORIGINAL = "def main():\n    setup()\n    work()\n    cleanup()\n    finish()\n"
INDENT_BRANCH = "def main():\n    setup()\n    work()\n        cleanup()\n    finish()\n"
INDENT_BASE = "def main():\n    setup()\n    work()\ncleanup()\n    finish()\n"
JOB_BLOCK = (
    "job:\n  image: base\n  cache: none\n  retry: 1\n  script: make\n  after: done\n"
    "  notify: no\n"
)
JOB_TIMEOUT = JOB_BLOCK.replace("  script: make\n", "  script: make\n  timeout: 30\n")
JOBS_ORIGINAL = JOB_BLOCK * 3
JOBS_BRANCH = JOB_TIMEOUT + JOB_BLOCK * 2
JOBS_BASE = JOB_BLOCK + JOB_TIMEOUT + JOB_BLOCK


def patch_identity(repo: Path, commit: str, mode: str) -> str:
    shown = subprocess.run(
        ["git", "show", "--format=", *checkouts._PATCH_FLAGS, commit],
        cwd=repo, capture_output=True, check=True,
    ).stdout
    result = subprocess.run(
        ["git", "patch-id", mode], input=shown, cwd=repo, capture_output=True, check=True
    )
    return result.stdout.split()[0].decode()


def lookalike_world(tmp_path: Path, name: str, original: str, branch: str, base: str, shape: str):
    """A branch and the base each change ``name`` in a way that is the same patch
    once whitespace and line numbers are ignored, yet is a different change.

    ``shape`` ``patch`` lands the base's change as its own commit, so the
    branch could only match commit by commit; ``squash`` gives the branch two
    commits and lands them on the base as one, so only the whole branch matches.
    """
    folder, _remote, work = small_world(tmp_path)
    commit_file(work, name, original, f"add {name}")
    git(work, "push", "-q", "origin", "trunk")
    git(work, "checkout", "-q", "-b", "feature", "trunk")
    if shape == "squash":
        commit_file(work, "notes.txt", "notes\n", "add notes")
    commit_file(work, name, branch, f"change {name} on the branch")
    git(work, "checkout", "-q", "trunk")
    if shape == "squash":
        write_bytes(work, "notes.txt", b"notes\n")
    commit_file(work, name, base, f"change {name} on the base")
    commit_file(work, "diverged.txt", "x\n")
    git(work, "push", "-q", "origin", "trunk")
    return folder, work


@pytest.mark.parametrize("shape", ["patch", "squash"])
@pytest.mark.parametrize(
    ("name", "original", "branch", "base", "verbatim_differs"),
    [
        ("app.py", INDENT_ORIGINAL, INDENT_BRANCH, INDENT_BASE, True),
        ("ci.yml", JOBS_ORIGINAL, JOBS_BRANCH, JOBS_BASE, False),
    ],
    ids=["indentation", "repeated-block"],
)
def test_a_lookalike_change_elsewhere_in_the_base_is_not_merged(
    tmp_path, isolated_git, name, original, branch, base, verbatim_differs, shape
):
    cache = nyx_home(isolated_git)
    folder, work = lookalike_world(tmp_path, name, original, branch, base, shape)
    tip, landed = rev(work, "feature"), rev(work, "trunk~1")
    if shape == "patch":
        # The trap is real: the stable identities agree.  Where only whitespace
        # differs the verbatim ones do not; where only the location differs no
        # identity can tell, and the merge check alone refuses the match.
        assert patch_identity(work, tip, "--stable") == patch_identity(work, landed, "--stable")
        same = patch_identity(work, tip, "--verbatim") == patch_identity(work, landed, "--verbatim")
        assert same != verbatim_differs
    else:
        # The squashed branch's whole change has the same identity as the base's commit.
        fork = git(work, "merge-base", "feature", "trunk").stdout.strip()
        whole = subprocess.run(
            ["git", "diff", *checkouts._PATCH_FLAGS, fork, tip, "--"],
            cwd=work, capture_output=True, check=True
        ).stdout
        identity = subprocess.run(
            ["git", "patch-id", "--stable"], input=whole, cwd=work, capture_output=True, check=True
        ).stdout.split()[0].decode()
        assert identity == patch_identity(work, landed, "--stable")

    document = collect([folder])

    item = find_clone(document, work)
    assert verdicts_of(item)["feature"] == NOT_MERGED
    key = f"{tip}.{rev(work, 'origin/trunk')}"
    # What is stored under the pair is the decided ``none``, never a patch rung.
    assert cache_entries(cache)[key] == "none"


def test_a_change_the_base_later_rewrote_still_reads_merged_where_it_landed(tmp_path):
    folder, _remote, work = small_world(tmp_path)
    commit_file(work, "f.txt", "a\nb\nc\n", "add f")
    git(work, "push", "-q", "origin", "trunk")
    git(work, "checkout", "-q", "-b", "feature", "trunk")
    commit_file(work, "f.txt", "A\nb\nc\n", "capitalise a")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "d1.txt", "diverged\n")
    git(work, "cherry-pick", "feature")
    commit_file(work, "f.txt", "Z\nb\nc\n", "rewrite the same line later")
    git(work, "push", "-q", "origin", "trunk")

    document = collect([tmp_path / "folder"])

    # The base commit that landed the change made exactly the branch's change,
    # so the branch is merged whatever the base did to those lines afterwards.
    assert verdicts_of(find_clone(document, work))["feature"] == MERGED_PATCH


CHANGELOG = "# Changes\n\n- old\n"
LIST_LINES = "a\nb\nc\nd\n"


def landed_then_changed_world(tmp_path: Path, shape: str):
    """A branch whose change landed on the base, which then changed around it.

    Merging the branch into the base's current tip conflicts (or changes the
    tree) in each of these worlds, yet the base commit that landed the change
    made exactly the branch's change.
    """
    folder, _remote, work = small_world(tmp_path)
    name, original = (
        ("CHANGELOG.md", CHANGELOG) if shape == "squash-above" else ("list.txt", LIST_LINES)
    )
    commit_file(work, name, original, f"add {name}")
    git(work, "push", "-q", "origin", "trunk")
    git(work, "checkout", "-q", "-b", "feature", "trunk")
    if shape == "squash-above":
        commit_file(work, "notes.txt", "notes\n", "add notes")
        commit_file(work, name, CHANGELOG + "- feature A\n", "log feature A")
    else:
        commit_file(work, name, original.replace("b\n", "B\n"), "capitalise b")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "diverged.txt", "x\n")
    if shape == "squash-above":
        git(work, "merge", "--squash", "feature")
        git(work, "commit", "-q", "-m", "squash feature")
        commit_file(work, name, CHANGELOG + "- feature B\n- feature A\n", "log feature B above")
    else:
        git(work, "cherry-pick", "feature")
        if shape == "pick-adjacent":
            commit_file(work, name, "a\nB\nC\nd\n", "edit the next line")
        elif shape == "pick-before":
            commit_file(work, name, "a\nx\nB\nc\nd\n", "insert a line before it")
        else:
            git(work, "revert", "--no-edit", "HEAD")
    git(work, "push", "-q", "origin", "trunk")
    return folder, work


@pytest.mark.parametrize(
    ("shape", "verdict"),
    [
        ("squash-above", MERGED_SQUASH),
        ("pick-adjacent", MERGED_PATCH),
        ("pick-before", MERGED_PATCH),
        ("pick-revert", MERGED_PATCH),
    ],
)
def test_a_change_that_landed_reads_merged_however_the_base_changed_around_it_later(
    tmp_path, shape, verdict
):
    folder, work = landed_then_changed_world(tmp_path, shape)
    # The trap is real: merging the branch into the base now does not give the base.
    attempt = git(work, "merge-tree", "--write-tree", "origin/trunk", "feature", check=False)
    assert attempt.returncode == 1 or attempt.stdout.strip() != rev(work, "origin/trunk^{tree}")

    document = collect([folder])

    assert verdicts_of(find_clone(document, work))["feature"] == verdict


def test_a_branch_with_its_own_root_commit_cherry_picked_into_the_base_reads_merged(tmp_path):
    folder, _remote, work = small_world(tmp_path)
    git(work, "checkout", "-q", "--orphan", "feature")
    git(work, "rm", "-rfq", ".")
    commit_file(work, "orphan.txt", "one\n", "root of the unrelated branch")
    commit_file(work, "orphan.txt", "one\ntwo\n", "extend it")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "diverged.txt", "x\n")
    git(work, "cherry-pick", "feature~1", "feature")
    git(work, "push", "-q", "origin", "trunk")
    assert git(work, "merge-base", "feature", "trunk", check=False).returncode == 1

    document = collect([folder])

    assert verdicts_of(find_clone(document, work))["feature"] == MERGED_PATCH


@pytest.mark.parametrize("shape", ["patch", "squash"])
def test_a_lookalike_change_landed_on_a_shifted_file_is_not_merged_either(tmp_path, shape):
    """The base's file differs from the branch's, so the landing check merges contents."""
    folder, _remote, work = small_world(tmp_path)
    commit_file(work, "ci.yml", "# head\n" + JOBS_ORIGINAL, "add ci.yml")
    git(work, "push", "-q", "origin", "trunk")
    git(work, "checkout", "-q", "-b", "feature", "trunk")
    if shape == "squash":
        commit_file(work, "notes.txt", "notes\n", "add notes")
    commit_file(work, "ci.yml", "# head\n" + JOBS_BRANCH, "timeout on job 1")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "ci.yml", "# HEAD\n" + JOBS_ORIGINAL, "retitle")
    if shape == "squash":
        write_bytes(work, "notes.txt", b"notes\n")
    commit_file(work, "ci.yml", "# HEAD\n" + JOBS_BASE, "timeout on job 2")
    commit_file(work, "diverged.txt", "x\n")
    git(work, "push", "-q", "origin", "trunk")
    landed = rev(work, "trunk~1")
    fork = git(work, "merge-base", "feature", "trunk").stdout.strip()
    whole = subprocess.run(
        ["git", "diff", *checkouts._PATCH_FLAGS, fork, "feature", "--"],
        cwd=work, capture_output=True, check=True
    ).stdout
    wanted = patch_identity(work, landed, "--verbatim")
    identities = {"patch": patch_identity(work, rev(work, "feature"), "--verbatim"), "squash": subprocess.run(
        ["git", "patch-id", "--verbatim"], input=whole, cwd=work, capture_output=True, check=True
    ).stdout.split()[0].decode()}
    # The trap is real: the identities agree, so only the landing check refuses.
    assert identities[shape] == wanted

    verdicts = verdicts_of(find_clone(collect([folder]), work))

    assert verdicts["feature"] == NOT_MERGED


def shifted_world(tmp_path: Path):
    """A cherry-pick whose base file differs from the branch's, so its change
    lands at a shifted place and the landing check must merge file contents."""
    folder, _remote, work = small_world(tmp_path)
    lines = "".join(f"line {number}\n" for number in range(1, 13))
    commit_file(work, "f.txt", lines, "add f")
    git(work, "push", "-q", "origin", "trunk")
    git(work, "checkout", "-q", "-b", "feature", "trunk")
    commit_file(work, "f.txt", lines.replace("line 10\n", "line ten\n"), "edit line 10")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "f.txt", lines.replace("line 2\n", "two\nextra\n"), "grow the top")
    git(work, "cherry-pick", "feature")
    git(work, "push", "-q", "origin", "trunk")
    return folder, work


def test_a_cherry_pick_that_landed_at_a_shifted_place_reads_merged_through_a_content_merge(
    tmp_path, monkeypatch
):
    folder, work = shifted_world(tmp_path)

    with counted_git_calls(monkeypatch) as calls:
        document = collect([folder])

    assert verdicts_of(find_clone(document, work))["feature"] == MERGED_PATCH
    assert "merge-file" in calls
    assert not {"merge-tree", "init"} & set(calls)


def git_dir_of(repository: Path) -> Path:
    return repository / ".git" if (repository / ".git").exists() else repository


def file_times(repository: Path) -> dict[str, int]:
    """Modification times of every file a verdict run could touch.

    The git directory except the object store, which ``store_times`` compares
    strictly, plus the working tree.
    """
    git_dir = git_dir_of(repository)
    times: dict[str, int] = {}
    for item in git_dir.rglob("*"):
        relative = item.relative_to(git_dir).as_posix()
        if not item.is_file() or relative.split("/")[0] == "objects":
            continue
        times[f".git/{relative}"] = item.stat().st_mtime_ns
    if git_dir != repository:
        for item in repository.rglob("*"):
            relative = item.relative_to(repository).as_posix()
            if item.is_file() and relative.split("/")[0] != ".git":
                times[relative] = item.stat().st_mtime_ns
    return times


def store_times(repository: Path) -> dict[str, int]:
    """Every file of the object store, loose or packed, with its modification time."""
    objects = git_dir_of(repository) / "objects"
    return {
        item.relative_to(objects).as_posix(): item.stat().st_mtime_ns
        for item in objects.rglob("*")
        if item.is_file()
    }


def pack_names(repository: Path) -> list[str]:
    return sorted(
        item.name for item in (git_dir_of(repository) / "objects" / "pack").glob("*.pack")
    )


def loose_objects(repository: Path) -> list[str]:
    objects = git_dir_of(repository) / "objects"
    return sorted(
        item.name for item in objects.glob("[0-9a-f][0-9a-f]/*") if item.is_file()
    )


def pack_everything(repository: Path) -> None:
    """Put every object, reachable or not, in one pack and drop the loose copies."""
    names = git(
        repository, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)"
    ).stdout
    destination = git_dir_of(repository) / "objects" / "pack" / "pack"
    subprocess.run(
        ["git", "pack-objects", "-q", str(destination)],
        cwd=repository, input=names, text=True, capture_output=True, check=True,
    )
    git(repository, "prune-packed")
    assert pack_names(repository) and loose_objects(repository) == []


@pytest.mark.parametrize("packed", [False, True], ids=["loose", "packed"])
def test_the_landing_check_touches_no_object_store_not_even_a_timestamp(tmp_path, packed):
    folder, remote, work, _worktree = build_verdict_world(tmp_path)
    git(work, "push", "-q", "origin", "picked", "squashed", "bin-picked")
    first = clone(remote, tmp_path / "chain" / "first", "--shared")
    second = clone(first, folder / "second", "--shared")
    for name in ("picked", "squashed", "bin-picked"):
        git(second, "branch", name, rev(work, name))
    if packed:
        pack_everything(work)
        pack_everything(remote)
    stores = [work, remote, first, second]
    # Each variant states its own layout: nothing packs on its own.
    assert bool(pack_names(work)) == packed
    assert bool(loose_objects(work)) == (not packed)
    before = [(snapshot(store), file_times(store), store_times(store)) for store in stores]
    time.sleep(0.05)

    document = collect([folder])

    verdicts = verdicts_of(find_clone(document, second))
    assert (verdicts["picked"], verdicts["squashed"]) == (MERGED_PATCH, MERGED_SQUASH)
    for store, (content, times, objects) in zip(stores, before, strict=True):
        assert snapshot(store) == content
        assert file_times(store) == times
        # Housekeeping is pinned off, so any new, removed or touched object file is the run's.
        assert store_times(store) == objects


@pytest.mark.parametrize("packed", [False, True], ids=["loose", "packed"])
def test_the_landing_check_touches_no_lender_store_not_even_a_timestamp(tmp_path, packed):
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    holder = clone(remote, folder / "holder A")
    other = clone(remote, folder / "other B")
    git(other, "checkout", "-q", "-b", "feat")
    commit_file(other, "feat.txt", "feat\n")
    commit_file(holder, "held.txt", "held\n")
    git(holder, "fetch", "-q", str(other), "feat")
    git(holder, "cherry-pick", "FETCH_HEAD")
    git(holder, "push", "-q", "origin", "trunk")
    if packed:
        pack_everything(holder)
    stores = [holder, other, remote]
    assert bool(pack_names(holder)) == packed
    before = [(snapshot(store), file_times(store), store_times(store)) for store in stores]
    time.sleep(0.05)

    document = live([folder])

    item = find_clone(document, other)
    assert item["verdict_base"]["objects_from"] == real(holder)
    assert verdicts_of(item)["feat"] == MERGED_PATCH
    for store, (content, times, objects) in zip(stores, before, strict=True):
        assert snapshot(store) == content
        assert file_times(store) == times
        # Housekeeping is pinned off, so any new, removed or touched object file is the run's.
        assert store_times(store) == objects


@pytest.mark.skipif(ON_WINDOWS, reason="the version stand-in is a shell script")
@pytest.mark.parametrize("version", [1, 2])
def test_a_cached_verdict_from_a_retired_schema_is_never_served_and_the_next_write_replaces_it(
    tmp_path, monkeypatch, isolated_git, version
):
    cache = nyx_home(isolated_git)
    folder, work = lookalike_world(
        tmp_path, "app.py", INDENT_ORIGINAL, INDENT_BRANCH, INDENT_BASE, "patch"
    )
    key = f"{rev(work, 'feature')}.{rev(work, 'origin/trunk')}"
    old = stand_in(
        tmp_path, "old", 'case "$*" in *" --version") echo "git version 2.38.0"; exit 0;; esac'
    )
    document = {"schema_version": version, "verdicts": {key: {"rung": "patch_equivalent", "at": 5}}}
    cache.parent.mkdir(exist_ok=True)
    retired = json.dumps(document).encode()
    cache.write_bytes(retired)

    with path_with(monkeypatch, old):
        offline = collect([folder])
    # The old git cannot decide the branch, and the retired file serves nothing.
    assert verdicts_of(find_clone(offline, work))["feature"] == unknown("git_too_old")
    assert offline["diagnostics"] == []
    # A run that decides nothing and checks nothing leaves the retired file as it was.
    assert cache.read_bytes() == retired

    # The branch is decided again, and by the real answer: the change only looks alike.
    current = collect([folder], remote_checks=True)
    assert verdicts_of(find_clone(current, work))["feature"] == NOT_MERGED
    assert current["diagnostics"] == []
    replaced = json.loads(cache.read_text())
    assert set(replaced) == {"schema_version", "verdicts", "remote_checks"}
    assert replaced["schema_version"] == 3
    assert {k: v["rung"] for k, v in replaced["verdicts"].items()} == {key: "none"}
    assert replaced["verdicts"][key]["at"] > 5
    [saved_remote] = replaced["remote_checks"].values()
    assert saved_remote["heads"]["default"] == "trunk"

    # The same entry in a schema 3 file is served: only the form was retired.
    cache.write_text(json.dumps({**document, "schema_version": 3, "remote_checks": {}}))
    with path_with(monkeypatch, old):
        served = collect([folder])
    assert verdicts_of(find_clone(served, work))["feature"] == MERGED_PATCH


@pytest.mark.skipif(ON_WINDOWS, reason="needs a SHA-256 repository the lane may lack")
def test_the_patch_rungs_confirm_a_cherry_pick_in_a_sha256_repository(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = served / "remote.git"
    made = git(served, "init", "-q", "--bare", "--object-format=sha256", "-b", "trunk", str(remote),
               check=False)
    if made.returncode != 0:
        pytest.skip("this git cannot make a SHA-256 repository")
    seed = served / "seed"
    git(served, "init", "-q", "--object-format=sha256", "-b", "trunk", str(seed))
    commit_file(seed, "file.txt", "one\n", "seed")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-q", "origin", "trunk")
    work = clone(remote, folder / "work")
    git(work, "checkout", "-q", "-b", "picked", "trunk")
    commit_file(work, "p1.txt", "p1\n")
    commit_file(work, "p2.txt", "p2\n")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "d1.txt", "diverged\n")
    git(work, "cherry-pick", "picked~1", "picked")
    git(work, "push", "-q", "origin", "trunk")

    document = collect([folder])

    assert verdicts_of(find_clone(document, work))["picked"] == MERGED_PATCH


def test_the_squash_rung_streams_its_diff_and_no_run_call_carries_diff_bytes(
    tmp_path, monkeypatch
):
    folder, _remote, work, _worktree = build_verdict_world(tmp_path)
    run, pipeline = checkouts._GitRunner.run, checkouts._GitRunner.pipeline
    streamed: list[str] = []
    buffered: list[str] = []

    def recording_run(self, arguments, **options):
        if git_subcommand(arguments) in ("diff", "patch-id") or "stdin_bytes" in options:
            buffered.append(git_subcommand(arguments))
        return run(self, arguments, **options)

    def recording_pipeline(self, producer, consumer, **options):
        streamed.append(git_subcommand(producer))
        return pipeline(self, producer, consumer, **options)

    with monkeypatch.context() as patched:
        patched.setattr(checkouts._GitRunner, "run", recording_run)
        patched.setattr(checkouts._GitRunner, "pipeline", recording_pipeline)
        document = collect([folder])

    assert verdicts_of(find_clone(document, work))["squashed"] == MERGED_SQUASH
    assert "diff" in streamed
    assert buffered == []


def test_a_commit_that_differs_from_a_base_commit_only_in_whitespace_is_not_its_counterpart(
    tmp_path,
):
    folder, _remote, work = small_world(tmp_path)
    commit_file(work, "w.txt", "keep\nx\nkeep\n", "add w")
    git(work, "push", "-q", "origin", "trunk")
    git(work, "checkout", "-q", "-b", "feature", "trunk")
    commit_file(work, "w.txt", "keep\ny \nkeep\n", "y with a trailing space")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "diverged.txt", "x\n")
    commit_file(work, "w.txt", "keep\ny\nkeep\n", "y")
    commit_file(work, "w.txt", "keep\ny \nkeep\n", "add the trailing space")
    git(work, "push", "-q", "origin", "trunk")
    tip, landed = rev(work, "feature"), rev(work, "trunk~1")
    # Only whitespace tells the two commits apart, and merging the branch into
    # the base would change nothing, so only an exact identity keeps them apart.
    assert patch_identity(work, tip, "--stable") == patch_identity(work, landed, "--stable")
    assert patch_identity(work, tip, "--verbatim") != patch_identity(work, landed, "--verbatim")

    document = collect([folder])

    assert verdicts_of(find_clone(document, work))["feature"] == NOT_MERGED


@pytest.mark.skipif(ON_WINDOWS, reason="the failing git stand-in is a shell script")
@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ('case "$*" in *" diff-tree "*) exit 2;; esac', "failed"),
        ('case "$*" in *" diff-tree "*) sleep 3;; esac', "deadline"),
    ],
    ids=["landing-diff", "landing-diff-call-cap"],
)
def test_a_landing_check_that_fails_leaves_the_match_unknown_and_stores_nothing(
    tmp_path, monkeypatch, isolated_git, body, reason
):
    cache = nyx_home(isolated_git)
    folder, _remote, work, _worktree = build_verdict_world(tmp_path)
    shim = stand_in(tmp_path, "failing-landing-check", body)

    with path_with(monkeypatch, shim):
        document = collect([folder], call_cap_seconds=1.0)

    verdicts = verdicts_of(find_clone(document, work))
    stored = cache_entries(cache)
    base = rev(work, "origin/trunk")
    for name in ("picked", "squashed", "bin-picked", "blobdiff"):
        assert verdicts[name] == unknown(reason), name
        assert f"{rev(work, name)}.{base}" not in stored, name
    # Tips that never reach the landing check are decided and stored as usual.
    assert verdicts["unmerged"] == NOT_MERGED
    assert verdicts["split"] == MERGED_TREE
    assert stored[f"{rev(work, 'unmerged')}.{base}"] == "none"


@pytest.mark.skipif(ON_WINDOWS, reason="the failing git stand-in is a shell script")
@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ('case "$*" in *" cat-file "*) exit 2;; esac', "failed"),
        ('case "$*" in *" cat-file "*) sleep 3;; esac', "deadline"),
        ('case "$*" in *" merge-file "*) exit 255;; esac', "failed"),
        ('case "$*" in *" merge-file "*) sleep 3;; esac', "deadline"),
    ],
    ids=["read-blob", "read-blob-call-cap", "merge-file", "merge-file-call-cap"],
)
def test_a_content_merge_step_that_fails_leaves_the_match_unknown_and_stores_nothing(
    tmp_path, monkeypatch, isolated_git, body, reason
):
    cache = nyx_home(isolated_git)
    folder, work = shifted_world(tmp_path)
    shim = stand_in(tmp_path, "failing-content-merge", body)

    with path_with(monkeypatch, shim):
        document = collect([folder], call_cap_seconds=1.0)

    assert verdicts_of(find_clone(document, work))["feature"] == unknown(reason)
    stored = cache_entries(cache) if cache.exists() else {}
    assert f"{rev(work, 'feature')}.{rev(work, 'origin/trunk')}" not in stored


@posix_only
def test_a_symbolic_link_retargeted_where_the_base_edited_a_file_is_not_merged(
    tmp_path, isolated_git
):
    """The base's file at that path is a regular file, the branch's a symbolic link."""
    cache = nyx_home(isolated_git)
    folder, _remote, work = small_world(tmp_path)
    commit_file(work, "link", "a", "add link as a file")
    git(work, "push", "-q", "origin", "trunk")
    git(work, "checkout", "-q", "-b", "feature", "trunk")
    for target, message in (("a", "make link a symbolic link"), ("b", "retarget link")):
        (work / "link").unlink()
        (work / "link").symlink_to(target)
        git(work, "add", "link")
        git(work, "commit", "-q", "-m", message)
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "diverged.txt", "x\n")
    git(work, "cherry-pick", "feature~1")
    (work / "link").unlink()
    write_bytes(work, "link", b"a")
    git(work, "commit", "-q", "-m", "make link a file again")
    commit_file(work, "link", "b", "edit the file")
    git(work, "push", "-q", "origin", "trunk")
    # The trap is real: the retarget and the file edit share their exact identity.
    assert patch_identity(work, rev(work, "feature"), "--verbatim") == patch_identity(
        work, rev(work, "trunk"), "--verbatim"
    )

    document = collect([folder])

    assert verdicts_of(find_clone(document, work))["feature"] == NOT_MERGED
    assert cache_entries(cache)[f"{rev(work, 'feature')}.{rev(work, 'origin/trunk')}"] == "none"


def repeated_block_landings(work: Path, blocks: int, landed: list[int]) -> None:
    """``ci.yml`` with ``blocks`` identical jobs; the base times out ``landed`` jobs
    (0-based) one commit each, in that order, all with one patch identity."""
    state = [JOB_BLOCK] * blocks
    for index in landed:
        state[index] = JOB_TIMEOUT
        commit_file(work, "ci.yml", "".join(state), f"timeout on job {index + 1}")


def test_a_landing_behind_a_newer_lookalike_with_its_identity_still_reads_merged(tmp_path):
    folder, _remote, work = small_world(tmp_path)
    commit_file(work, "ci.yml", JOB_BLOCK * 3, "add ci.yml")
    git(work, "push", "-q", "origin", "trunk")
    git(work, "checkout", "-q", "-b", "feature", "trunk")
    commit_file(work, "ci.yml", JOB_TIMEOUT + JOB_BLOCK * 2, "timeout on job 1")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "diverged.txt", "x\n")
    git(work, "cherry-pick", "feature")
    commit_file(work, "ci.yml", JOB_TIMEOUT * 2 + JOB_BLOCK, "timeout on job 2")
    git(work, "push", "-q", "origin", "trunk")
    # The newest base commit with the branch's identity is the lookalike on job 2.
    identity = patch_identity(work, rev(work, "feature"), "--verbatim")
    assert patch_identity(work, rev(work, "trunk"), "--verbatim") == identity
    assert patch_identity(work, rev(work, "trunk~1"), "--verbatim") == identity

    document = collect([folder])

    assert verdicts_of(find_clone(document, work))["feature"] == MERGED_PATCH


def test_more_base_commits_with_the_identity_than_the_bound_leave_the_branch_unknown(
    tmp_path, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, _remote, work = small_world(tmp_path)
    blocks = checkouts.LANDING_CAP + 3
    commit_file(work, "ci.yml", JOB_BLOCK * blocks, "add ci.yml")
    git(work, "push", "-q", "origin", "trunk")
    git(work, "checkout", "-q", "-b", "feature", "trunk")
    commit_file(work, "ci.yml", JOB_TIMEOUT + JOB_BLOCK * (blocks - 1), "timeout on job 1")
    git(work, "checkout", "-q", "trunk")
    # One lookalike more than the bound, none of them on job 1.
    repeated_block_landings(work, blocks, list(range(1, checkouts.LANDING_CAP + 2)))
    git(work, "push", "-q", "origin", "trunk")

    document = collect([folder])

    assert verdicts_of(find_clone(document, work))["feature"] == unknown("history_limit")
    stored = cache_entries(cache) if cache.exists() else {}
    assert f"{rev(work, 'feature')}.{rev(work, 'origin/trunk')}" not in stored


def test_a_newer_candidate_over_a_bound_does_not_hide_an_older_exact_landing(tmp_path):
    folder, _remote, work = small_world(tmp_path)
    blocks = checkouts.MERGE_FILE_CAP // len(JOB_BLOCK) + 100
    state = [JOB_BLOCK] * blocks
    commit_file(work, "ci.yml", "".join(state), "add ci.yml")
    git(work, "push", "-q", "origin", "trunk")
    git(work, "checkout", "-q", "-b", "feature", "trunk")
    state[5] = JOB_TIMEOUT
    commit_file(work, "ci.yml", "".join(state), "timeout on job 6")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "diverged.txt", "x\n")
    git(work, "cherry-pick", "feature")
    # The newest candidate looks the same on another copy of the block, and,
    # starting from a file the branch never saw, confirming it needs a content
    # merge of a file over the bound.
    state.append("tail: 1\n")
    commit_file(work, "ci.yml", "".join(state), "add a tail")
    state[6] = JOB_TIMEOUT
    commit_file(work, "ci.yml", "".join(state), "timeout on job 7")
    git(work, "push", "-q", "origin", "trunk")
    assert len(JOB_BLOCK) * blocks > checkouts.MERGE_FILE_CAP
    identity = patch_identity(work, rev(work, "feature"), "--verbatim")
    assert patch_identity(work, rev(work, "trunk"), "--verbatim") == identity
    assert patch_identity(work, rev(work, "trunk~2"), "--verbatim") == identity

    document = collect([folder])

    assert verdicts_of(find_clone(document, work))["feature"] == MERGED_PATCH


def test_landing_content_merges_keep_only_a_bounded_amount_in_memory(tmp_path):
    import tracemalloc

    folder, _remote, work = small_world(tmp_path)
    rows = [f"row {number:06d} {'x' * 40}\n" for number in range(18000)]
    size = sum(len(row) for row in rows)
    assert 0.8 * checkouts.MERGE_FILE_CAP < size < checkouts.MERGE_FILE_CAP
    commit_file(work, "big.txt", "".join(rows), "add big")
    git(work, "push", "-q", "origin", "trunk")
    names = [f"f{number}" for number in range(8)]
    for number, name in enumerate(names):
        git(work, "checkout", "-q", "-b", name, "trunk")
        edited = list(rows)
        edited[9000 + number] = edited[9000 + number].upper()
        commit_file(work, "big.txt", "".join(edited), f"edit row {number}")
    git(work, "checkout", "-q", "trunk")
    base = list(rows)
    for number, name in enumerate(names):
        base[number] = base[number].upper()
        commit_file(work, "big.txt", "".join(base), f"base edit {number}")
        git(work, "cherry-pick", name)
    git(work, "push", "-q", "origin", "trunk")

    tracemalloc.start()
    try:
        document = collect([folder])
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    verdicts = verdicts_of(find_clone(document, work))
    assert all(verdicts[name] == MERGED_PATCH for name in names)
    assert peak < 6 * checkouts.MERGE_FILE_CAP, peak


def test_one_landing_confirmation_keeps_a_bounded_amount_however_much_it_reads(tmp_path):
    import tracemalloc

    folder, _remote, work = small_world(tmp_path)
    blocks = checkouts.MERGE_FILE_CAP // len(JOB_BLOCK) - 200
    size = len(JOB_BLOCK) * blocks
    assert 0.9 * checkouts.MERGE_FILE_CAP < size < checkouts.MERGE_FILE_CAP
    names = ("one.yml", "two.yml")
    states = {name: [JOB_BLOCK] * blocks for name in names}

    def commit_states(message: str) -> None:
        for name in names:
            (work / name).write_bytes("".join(states[name]).encode())
        git(work, "add", *names)
        git(work, "commit", "-q", "-m", message)

    commit_states("add jobs")
    git(work, "push", "-q", "origin", "trunk")
    git(work, "checkout", "-q", "-b", "feature", "trunk")
    for name in names:
        states[name][5] = JOB_TIMEOUT
    commit_states("timeout on job 6 of both files")
    git(work, "checkout", "-q", "trunk")
    states = {name: [JOB_BLOCK] * blocks for name in names}
    # Each of the eight landings makes the branch's change to ``one.yml`` but a
    # lookalike change to ``two.yml``, each from a file the branch never saw, so
    # every one needs a content merge of both files: one confirmation reads far
    # more distinct file contents than it may keep.
    for number in range(checkouts.LANDING_CAP):
        states["one.yml"][5] = JOB_BLOCK
        for name in names:
            states[name][100 + number] = JOB_BLOCK.replace("image: base", "image: next")
        commit_states(f"move the image of job {101 + number}")
        states["one.yml"][5] = JOB_TIMEOUT
        states["two.yml"][20 + number] = JOB_TIMEOUT
        commit_states(f"timeout on jobs 6 and {21 + number}")
    git(work, "push", "-q", "origin", "trunk")
    identity = patch_identity(work, rev(work, "feature"), "--verbatim")
    for number in range(checkouts.LANDING_CAP):
        assert patch_identity(work, rev(work, f"trunk~{2 * number}"), "--verbatim") == identity
    distinct = 2 * len(names) + 2 * len(names) * checkouts.LANDING_CAP
    assert distinct * size > 4 * checkouts.MERGE_RETAIN_CAP

    tracemalloc.start()
    try:
        document = collect([folder])
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert verdicts_of(find_clone(document, work))["feature"] == NOT_MERGED
    assert peak < checkouts.MERGE_RETAIN_CAP + 8 * checkouts.MERGE_FILE_CAP, peak


def content_merges_world(tmp_path: Path, files: int):
    """A cherry-pick of one commit editing ``files`` files, each of which the base
    edited elsewhere first, so each file needs its own content merge."""
    folder, _remote, work = small_world(tmp_path)
    lines = "".join(f"line {number}\n" for number in range(1, 13))
    names = [f"m{number:02d}.txt" for number in range(files)]
    for name in names:
        write_bytes(work, name, lines.encode())
    git(work, "commit", "-q", "-m", "add files")
    git(work, "push", "-q", "origin", "trunk")
    git(work, "checkout", "-q", "-b", "feature", "trunk")
    for name in names:
        write_bytes(work, name, lines.replace("line 10\n", "ten\n").encode())
    git(work, "commit", "-q", "-m", "edit line 10 everywhere")
    git(work, "checkout", "-q", "trunk")
    for name in names:
        write_bytes(work, name, lines.replace("line 2\n", "two\nextra\n").encode())
    git(work, "commit", "-q", "-m", "grow every top")
    git(work, "cherry-pick", "feature")
    git(work, "push", "-q", "origin", "trunk")
    return folder, work


@pytest.mark.parametrize("over", [False, True], ids=["at-bound", "over-bound"])
def test_more_content_merges_than_the_bound_leave_the_branch_unknown_and_unstored(
    tmp_path, monkeypatch, isolated_git, over
):
    cache = nyx_home(isolated_git)
    folder, work = content_merges_world(tmp_path, checkouts.MERGE_PATH_CAP + over)

    with counted_git_calls(monkeypatch) as calls:
        document = collect([folder])

    verdict = verdicts_of(find_clone(document, work))["feature"]
    key = f"{rev(work, 'feature')}.{rev(work, 'origin/trunk')}"
    stored = cache_entries(cache) if cache.exists() else {}
    if over:
        assert verdict == unknown("history_limit")
        assert key not in stored
        assert "merge-file" not in calls
    else:
        assert verdict == MERGED_PATCH
        assert stored[key] == "patch_equivalent"
        assert calls.count("merge-file") == checkouts.MERGE_PATH_CAP


def test_a_content_merge_of_a_file_over_the_bound_is_unknown_and_unstored(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, _remote, work = small_world(tmp_path)
    rows = [
        f"row {number:06d} {'x' * 40}\n" for number in range(checkouts.MERGE_FILE_CAP // 48 + 10)
    ]
    original = "".join(rows)
    assert len(original) > checkouts.MERGE_FILE_CAP
    commit_file(work, "big.txt", original, "add big")
    git(work, "push", "-q", "origin", "trunk")
    git(work, "checkout", "-q", "-b", "feature", "trunk")
    commit_file(work, "big.txt", original.replace(rows[-5], rows[-5].upper()), "edit near the end")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "big.txt", original.replace(rows[3], rows[3].upper()), "edit near the top")
    git(work, "cherry-pick", "feature")
    git(work, "push", "-q", "origin", "trunk")

    with counted_git_calls(monkeypatch) as calls:
        document = collect([folder])

    assert verdicts_of(find_clone(document, work))["feature"] == unknown("history_limit")
    assert "merge-file" not in calls
    stored = cache_entries(cache) if cache.exists() else {}
    assert f"{rev(work, 'feature')}.{rev(work, 'origin/trunk')}" not in stored


@pytest.mark.parametrize("over", [False, True], ids=["at-bound", "over-bound"])
def test_a_change_touching_more_paths_than_the_bound_is_unknown_and_unstored(
    tmp_path, monkeypatch, isolated_git, over
):
    cache = nyx_home(isolated_git)
    monkeypatch.setattr(checkouts, "CONFIRM_PATH_CAP", 3)
    folder, _remote, work = small_world(tmp_path)
    git(work, "checkout", "-q", "-b", "feature", "trunk")
    for number in range(3 + over):
        write_bytes(work, f"p{number}.txt", b"p\n")
    git(work, "commit", "-q", "-m", "add files")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "diverged.txt", "x\n")
    git(work, "cherry-pick", "feature")
    git(work, "push", "-q", "origin", "trunk")

    document = collect([folder])

    verdict = verdicts_of(find_clone(document, work))["feature"]
    stored = cache_entries(cache) if cache.exists() else {}
    key = f"{rev(work, 'feature')}.{rev(work, 'origin/trunk')}"
    if over:
        assert verdict == unknown("history_limit")
        assert key not in stored
    else:
        assert verdict == MERGED_PATCH
        assert stored[key] == "patch_equivalent"


@posix_only
def test_the_landing_check_runs_no_clone_merge_driver_and_touches_nothing_in_the_clone(
    tmp_path, isolated_git
):
    folder, work = shifted_world(tmp_path)
    marker = tmp_path / "driver-marker"
    driver = marker_script(tmp_path / "scripts", "driver", marker, "exit 1")
    git(work, "config", "merge.probe.driver", f"{driver} %O %A %B")
    (work / ".git" / "info").mkdir(exist_ok=True)
    (work / ".git" / "info" / "attributes").write_text("* merge=probe\n")
    git(work, "merge-tree", "--write-tree", "origin/trunk", "feature", check=False)
    assert marker.exists(), "control: a merge inside the clone runs its configured driver"
    marker.unlink()
    before = (snapshot(work), file_times(work))
    time.sleep(0.05)

    document = collect([folder])

    assert verdicts_of(find_clone(document, work))["feature"] == MERGED_PATCH
    assert not marker.exists()
    assert (snapshot(work), file_times(work)) == before


def test_a_deleted_tip_object_is_failed_never_merged(tmp_path):
    folder, _remote, work, _worktree = build_verdict_world(tmp_path)
    cold = verdicts_of(find_clone(collect([folder]), work))
    # A ref to an object no store of the clone holds, however it would be packed.
    absent = "1234567890abcdef1234567890abcdef12345678"
    assert git(work, "cat-file", "-e", absent, check=False).returncode != 0
    ref = work / ".git" / "refs" / "heads" / "unmerged"
    assert ref.is_file() and rev(work, "unmerged") != absent
    ref.write_text(absent + "\n")

    document = collect([folder])

    after = verdicts_of(find_clone(document, work))
    assert after["unmerged"] == unknown("failed")
    for name, verdict in after.items():
        assert verdict == cold[name] or verdict == unknown("failed")
    assert all(value[0] != "merged" for name, value in after.items() if name == "unmerged")


def snapshot(repository: Path) -> tuple[Any, ...]:
    """What a repository holds, independent of how git lays it out on disk.

    The references, the set of objects (loose or packed), the index file, every
    lock file, the configuration, and the working tree.
    """
    git_dir = git_dir_of(repository)
    objects = sorted(
        git(repository, "cat-file", "--batch-all-objects", "--batch-check").stdout.splitlines()
    )
    index = git_dir / "index"
    locks = sorted(
        item.relative_to(git_dir).as_posix()
        for item in git_dir.rglob("*.lock")
        if item.name != "maintenance.lock"
    )
    tree = {}
    if git_dir != repository:
        tree = {
            item.relative_to(repository).as_posix(): item.read_bytes()
            for item in repository.rglob("*")
            if item.is_file() and item.relative_to(repository).parts[0] != ".git"
        }
    return (
        git(repository, "for-each-ref").stdout,
        objects,
        (index.read_bytes(), index.stat().st_mtime_ns) if index.exists() else None,
        locks,
        (git_dir / "config").read_bytes(),
        tree,
    )


def test_verdicts_change_nothing_in_any_repository(tmp_path):
    folder, remote, work, _worktree = build_verdict_world(tmp_path)
    repositories = [work, remote]
    before = [(snapshot(repository), store_times(repository)) for repository in repositories]

    collect([folder])

    assert [
        (snapshot(repository), store_times(repository)) for repository in repositories
    ] == before
    assert not (work / ".git" / "index.lock").exists()


def test_the_content_of_every_repository_survives_git_housekeeping_during_a_run(
    tmp_path, monkeypatch
):
    """Tolerates housekeeping: only content is compared, never object files or their times."""
    folder, remote, work, _worktree = build_verdict_world(tmp_path)
    repositories = [work, remote]
    before = [snapshot(repository) for repository in repositories]
    expected = verdicts_of(find_clone(collect([folder]), work))
    assert [snapshot(repository) for repository in repositories] == before
    original = checkouts._GitRunner.run
    housekeeping: list[str] = []

    def run_with_housekeeping(self, arguments, **options):
        if not housekeeping:
            housekeeping.append("done")
            for repository in repositories:
                git(repository, "gc", "-q")
                git(repository, "repack", "-A", "-d", "-q", "-f")
                git(repository, "pack-refs", "--all")
                git(repository, "commit-graph", "write")
        return original(self, arguments, **options)

    monkeypatch.setattr(checkouts._GitRunner, "run", run_with_housekeeping)

    document = collect([folder])

    assert housekeeping
    assert verdicts_of(find_clone(document, work)) == expected
    assert [snapshot(repository) for repository in repositories] == before


def run_sh(body: str) -> str:
    return f"#!/bin/sh\n{body}\n"


def sign_trunk(work: Path) -> str:
    """Put a commit carrying a ``gpgsig`` header on top of ``trunk`` and push it."""
    git(work, "checkout", "-q", "trunk")
    plain = git(
        work, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "signed"
    ).stdout.strip()
    header, _, message = git(work, "cat-file", "commit", plain).stdout.partition("\n\n")
    signature = (
        "gpgsig -----BEGIN PGP SIGNATURE-----\n"
        " \n"
        " iQEzBAABCAAdFiEEnotarealsignatureAAAAAAAAAAAAAAAAAAAA=\n"
        " =AAAA\n"
        " -----END PGP SIGNATURE-----\n"
    )
    raw = f"{header}\n{signature}\n{message}".encode()
    signed = subprocess.run(
        ["git", "hash-object", "-w", "-t", "commit", "--stdin"],
        cwd=work, input=raw, capture_output=True, check=True,
    ).stdout.decode().strip()
    git(work, "update-ref", "refs/heads/trunk", signed)
    git(work, "push", "-q", "origin", "trunk")
    return signed


@pytest.mark.skipif(ON_WINDOWS, reason="the trap programs are shell scripts")
def test_configured_programs_never_run_and_do_not_change_verdicts(tmp_path):
    folder, _remote, work, _worktree = build_verdict_world(tmp_path)
    signed = sign_trunk(work)
    assert "gpgsig " in git(work, "cat-file", "commit", signed).stdout
    baseline = verdicts_of(find_clone(collect([folder]), work))
    markers = tmp_path / "markers"
    markers.mkdir()
    for name in ("gpg", "external", "textconv"):
        script = tmp_path / f"{name}.sh"
        script.write_text(run_sh(f': > "{markers}/{name}"\ncat "$1" 2>/dev/null\nexit 0'))
        script.chmod(0o755)
    git(work, "config", "log.showSignature", "true")
    git(work, "config", "gpg.program", str(tmp_path / "gpg.sh"))
    git(work, "config", "diff.external", str(tmp_path / "external.sh"))
    git(work, "config", "diff.trap.textconv", str(tmp_path / "textconv.sh"))
    git(work, "config", "color.ui", "always")
    info = work / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("*.txt diff=trap\n*.dat diff=trap\n")

    # The configuration really changes unpinned git: it runs a program.
    git(work, "log", "-p", "--ext-diff", "-2", "picked", check=False)
    git(work, "log", "-p", "-2", "picked", check=False)
    git(work, "log", "-1", "--format=%T", "trunk", check=False)
    assert {item.name for item in markers.iterdir()} >= {"external", "textconv", "gpg"}
    for item in markers.iterdir():
        item.unlink()

    document = collect([folder])

    assert list(markers.iterdir()) == []
    assert verdicts_of(find_clone(document, work)) == baseline


@pytest.mark.parametrize("trap", ["replace", "graft"])
def test_replace_refs_and_grafts_do_not_change_a_verdict(tmp_path, trap):
    folder, _remote, work, _worktree = build_verdict_world(tmp_path)
    baseline = verdicts_of(find_clone(collect([folder]), work))
    top = rev(work, "origin/trunk")
    unmerged = rev(work, "unmerged")
    if trap == "graft":
        (work / ".git" / "info").mkdir(exist_ok=True)
        (work / ".git" / "info" / "grafts").write_text(f"{top} {unmerged}\n")
    else:
        tree = rev(work, "origin/trunk^{tree}")
        forged = git(work, "commit-tree", tree, "-p", unmerged, "-m", "forged").stdout.strip()
        git(work, "replace", "-f", top, forged)
    unpinned = git(work, "merge-base", "--is-ancestor", unmerged, top, check=False)
    assert unpinned.returncode == 0

    document = collect([folder])

    assert verdicts_of(find_clone(document, work)) == baseline
    assert verdicts_of(find_clone(document, work))["unmerged"] == NOT_MERGED


@pytest.mark.skipif(ON_WINDOWS, reason="the user-global attributes path differs on Windows")
def test_user_global_attributes_do_not_change_a_patch_equivalent(tmp_path, isolated_git):
    folder, _remote, work, _worktree = build_verdict_world(tmp_path)
    (isolated_git / "git").mkdir(exist_ok=True)
    (isolated_git / "git" / "attributes").write_text("shared.txt -diff\n")
    picked = git(work, "cherry", "origin/trunk", "blobdiff").stdout
    assert picked.startswith("+")

    document = collect([folder])

    assert verdicts_of(find_clone(document, work))["blobdiff"] == MERGED_PATCH


def test_a_repository_attribute_no_longer_present_does_not_keep_a_cached_unmerged(
    tmp_path, isolated_git
):
    nyx_home(isolated_git)
    folder, _remote, work, _worktree = build_verdict_world(tmp_path)
    attributes = work / ".gitattributes"
    attributes.write_text("shared.txt -diff\n")

    stricter = collect([folder])
    assert verdicts_of(find_clone(stricter, work))["blobdiff"] == NOT_MERGED

    attributes.unlink()
    document = collect([folder])

    assert verdicts_of(find_clone(document, work))["blobdiff"] == MERGED_PATCH


def test_a_cached_merged_never_replaces_an_unmerged_the_run_decides(tmp_path, isolated_git):
    cache = nyx_home(isolated_git)
    folder, _remote, work, _worktree = build_verdict_world(tmp_path)
    base, blobdiff = rev(work, "origin/trunk"), rev(work, "blobdiff")

    first = collect([folder])
    assert verdicts_of(find_clone(first, work))["blobdiff"] == MERGED_PATCH
    assert cache_entries(cache)[f"{blobdiff}.{base}"] == "patch_equivalent"
    (work / ".git" / "info").mkdir(exist_ok=True)
    (work / ".git" / "info" / "attributes").write_text("shared.txt -diff\n")

    document = collect([folder])

    # Every rung ran and proved nothing, so the run's own answer stands.
    assert verdicts_of(find_clone(document, work))["blobdiff"] == NOT_MERGED


def test_inherited_repository_variables_are_ignored(tmp_path, monkeypatch):
    folder, _remote, work, _worktree = build_verdict_world(tmp_path)
    baseline = verdicts_of(find_clone(collect([folder]), work))
    other = tmp_path / "other"
    git(tmp_path, "init", "-q", "-b", "trunk", str(other))
    commit_file(other, "o.txt", "o\n")
    before = (snapshot(other), (other / ".git" / "index").read_bytes())
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(other / ".git" / "objects"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))

    document = collect([folder])

    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_OBJECT_DIRECTORY", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name)
    assert verdicts_of(find_clone(document, work)) == baseline
    assert (snapshot(other), (other / ".git" / "index").read_bytes()) == before


# -- the verdict base ----------------------------------------------------


def merge_world(tmp_path: Path, *, holder_inside: bool = True):
    """Clone A merges B's pushed branch on the remote; clone B is not fetched."""
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    seed = rev(remote, "trunk")
    holder = clone(remote, (folder if holder_inside else tmp_path / "away") / "holder A")
    other = clone(remote, folder / "other B")
    git(other, "checkout", "-q", "-b", "feat")
    commit_file(other, "feat.txt", "feat\n")
    git(other, "push", "-q", "origin", "feat")
    git(holder, "fetch", "-q", "origin")
    git(holder, "merge", "-q", "--no-ff", "-m", "merge feat", "origin/feat")
    git(holder, "push", "-q", "origin", "trunk")
    return folder, remote, holder, other, seed, rev(holder, "HEAD")


@pytest.mark.skipif(ON_WINDOWS, reason="the logging git stand-in is a shell script")
def test_a_branch_merged_on_the_remote_is_merged_against_the_borrowed_live_head(
    tmp_path, monkeypatch
):
    folder, _remote, holder, other, _seed, head = merge_world(tmp_path)
    before = (snapshot(other), snapshot(holder))
    assert git(other, "cat-file", "-e", head, check=False).returncode != 0
    log = tmp_path / "calls.log"
    shim = git_stand_in(tmp_path, f'printf "%s\\n" "$*" >> "{log}"')
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")

    document = live([folder])

    item = find_clone(document, other)
    assert item["verdict_base"] == {
        "name": "trunk", "sha": head, "basis": "live", "objects_from": real(holder),
    }
    assert item["verdict_base"]["objects_from"] == item["cached_vs_remote"]["objects_from"]
    assert verdicts_of(item)["feat"] == MERGED_ANCESTOR
    assert find_clone(document, holder)["verdict_base"]["objects_from"] is None
    assert (snapshot(other), snapshot(holder)) == before
    probes = [call for call in log.read_text().splitlines() if "rev-list --no-walk" in call]
    named = [call for call in probes if head in call]
    assert sum(f"-C {real(other)} " in call for call in named) == 1
    assert sum(f"-C {real(holder)} " in call for call in probes) == 1


def test_without_network_or_a_lender_the_base_is_cached_and_the_branch_unmerged(tmp_path):
    folder, _remote, holder, other, _seed, _head = merge_world(tmp_path)

    document = collect([folder])
    item = find_clone(document, other)

    assert item["verdict_base"]["basis"] == "cached"
    assert item["verdict_base"]["objects_from"] is None
    assert item["verdict_base"]["sha"] == rev(other, "origin/trunk")
    assert verdicts_of(item)["feat"] == NOT_MERGED

    away = tmp_path / "away-world"
    away.mkdir()
    folder, _remote, holder, other, _seed, _head = merge_world(away, holder_inside=False)
    item = find_clone(live([folder]), other)
    assert item["verdict_base"]["basis"] == "cached"
    assert verdicts_of(item)["feat"] == NOT_MERGED


def test_a_detached_remote_head_does_not_replace_the_cached_default_name(tmp_path):
    folder, remote, holder, other, seed, head = merge_world(tmp_path)
    third = clone(remote, folder / "third C")
    git(third, "remote", "set-head", "origin", "-d")
    git(remote, "update-ref", "--no-deref", "HEAD", seed)
    assert git(remote, "symbolic-ref", "-q", "HEAD", check=False).returncode != 0

    document = live([folder])

    for path in (other, third):
        base = find_clone(document, path)["verdict_base"]
        assert base["name"] == "trunk"
        assert base["sha"] == head
        assert base["basis"] == "live"
    assert find_clone(document, other)["verdict_base"]["objects_from"] == real(holder)
    assert verdicts_of(find_clone(document, other))["feat"] == MERGED_ANCESTOR
    assert group_of(document, other)["default_branch"]["name"] is None


def test_clones_without_a_name_or_remote_have_no_base(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    nameless = clone(remote, folder / "nameless")
    git(nameless, "branch", "feat")
    git(nameless, "update-ref", "-d", "refs/remotes/origin/trunk")
    git(nameless, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    standalone = folder / "standalone"
    git(tmp_path, "init", "-q", "-b", "trunk", str(standalone))
    commit_file(standalone, "s.txt", "s\n")
    git(standalone, "branch", "feat")
    detached = tmp_path / "standalone-detached"
    git(standalone, "worktree", "add", "-q", "--detach", str(detached), "feat")

    document = collect([folder])

    first = find_clone(document, nameless)
    assert first["verdict_base"] is None
    assert verdicts_of(first) == {"feat": unknown("no_base"), "trunk": unknown("no_base")}
    second = find_clone(document, standalone)
    assert second["verdict_base"] is None
    assert verdicts_of(second) == {"feat": unknown("no_base"), "trunk": unknown("no_base")}
    assert verdict_tuple(find_worktree(second, detached)["verdict"]) == unknown("no_base")


def test_the_default_branch_alone_and_a_deleted_default_branch(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    git(remote, "branch", "remote-only", "trunk")
    alone = clone(remote, folder / "alone")
    deleted = clone(remote, folder / "deleted")
    git(deleted, "checkout", "-q", "-b", "feat")
    git(deleted, "branch", "-q", "-D", "trunk")

    document = collect([folder])

    item = find_clone(document, alone)
    assert item["verdict_base"]["name"] == "trunk"
    assert verdicts_of(item) == {"trunk": None}
    item = find_clone(document, deleted)
    assert item["verdict_base"]["name"] == "trunk"
    assert verdicts_of(item) == {"feat": MERGED_ANCESTOR}
    live_document = live([folder])
    assert verdicts_of(find_clone(live_document, alone)) == {"trunk": None}
    assert "remote-only" not in verdicts_of(find_clone(live_document, deleted))


@pytest.mark.skipif(ON_WINDOWS, reason="the logging git stand-in is a shell script")
def test_a_shallow_clone_with_a_moved_head_uses_its_cached_ref_and_probes_nothing(
    tmp_path, monkeypatch
):
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    shallow = clone(f"file://{remote}", folder / "shallow", "--depth", "1")
    cached = rev(shallow, "origin/trunk")
    push_commit(remote, "trunk", tmp_path)
    log = tmp_path / "calls.log"
    shim = git_stand_in(tmp_path, f'printf "%s\\n" "$*" >> "{log}"')
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")

    item = find_clone(live([folder]), shallow)

    assert item["shallow"] is True
    assert item["verdict_base"] == {
        "name": "trunk", "sha": cached, "basis": "cached", "objects_from": None,
    }
    assert not any(
        f"-C {real(shallow)} " in call and "rev-list --no-walk" in call
        for call in log.read_text().splitlines()
    )


# -- incomplete clones ---------------------------------------------------


@pytest.mark.parametrize("options", [("--depth", "1"), ("--filter=blob:none",)])
def test_incomplete_clones_run_only_the_ancestor_rung_and_fetch_nothing(tmp_path, options):
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    git(remote, "config", "uploadpack.allowFilter", "true")
    push_commit(remote, "trunk", tmp_path / "s1")
    incomplete = clone(f"file://{remote}", folder / "incomplete", *options)
    git(incomplete, "checkout", "-q", "-b", "picked", "origin/trunk")
    commit_file(incomplete, "picked.txt", "picked\n")
    picker = clone(remote, tmp_path / "picker")
    commit_file(picker, "picked.txt", "picked\n", "the same change on trunk")
    git(picker, "push", "-q", "origin", "trunk")
    git(incomplete, "fetch", "-q", *(("--depth", "1") if options[0] == "--depth" else ()), "origin")
    git(incomplete, "branch", "equal", "origin/trunk")
    before = (object_files(incomplete), pack_listing(incomplete))

    document = collect([folder])

    item = find_clone(document, incomplete)
    assert item["shallow"] is (options[0] == "--depth")
    assert item["partial_clone"] is (options[0] != "--depth")
    verdicts = verdicts_of(item)
    assert verdicts["picked"] == unknown("incomplete_clone")
    assert verdicts["equal"] == MERGED_ANCESTOR
    assert (object_files(incomplete), pack_listing(incomplete)) == before


# -- caps, the git version gate and refs that move mid-collection --------


def small_world(tmp_path: Path):
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    work = clone(remote, folder / "work")
    return folder, remote, work


def test_the_own_commit_cap_limits_history_and_decides_smaller_branches(tmp_path, monkeypatch):
    folder, _remote, work = small_world(tmp_path)
    git(work, "checkout", "-q", "-b", "three")
    for number in (1, 2, 3):
        commit_file(work, f"three{number}.txt", "x\n")
    git(work, "checkout", "-q", "-b", "two", "trunk")
    for number in (1, 2):
        commit_file(work, f"two{number}.txt", "x\n")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "diverged.txt", "x\n")
    git(work, "cherry-pick", "two~1", "two")
    git(work, "push", "-q", "origin", "trunk")
    monkeypatch.setattr(checkouts, "OWN_COMMIT_CAP", 2)

    verdicts = verdicts_of(find_clone(collect([folder]), work))

    assert verdicts["three"] == unknown("history_limit")
    assert verdicts["two"] == MERGED_PATCH


def test_the_shared_range_cap_evicts_the_largest_then_later_branch(tmp_path, monkeypatch):
    folder, _remote, work = small_world(tmp_path)
    git(work, "checkout", "-q", "-b", "line-a", "trunk")
    for number in (1, 2, 3):
        commit_file(work, f"a{number}.txt", "x\n")
    git(work, "checkout", "-q", "-b", "left-fork")
    commit_file(work, "left.txt", "x\n")
    git(work, "checkout", "-q", "-b", "line-b", "trunk")
    for number in (1, 2, 3):
        commit_file(work, f"b{number}.txt", "x\n")
    git(work, "checkout", "-q", "-b", "right-fork")
    commit_file(work, "right.txt", "x\n")
    git(work, "checkout", "-q", "trunk")
    git(work, "merge", "-q", "--no-ff", "-m", "merge a", "line-a")
    git(work, "merge", "-q", "--no-ff", "-m", "merge b", "line-b")
    git(work, "push", "-q", "origin", "trunk")
    git(work, "branch", "-q", "-D", "line-a", "line-b")
    monkeypatch.setattr(checkouts, "SHARED_RANGE_CAP", 5)

    verdicts = verdicts_of(find_clone(collect([folder]), work))

    assert verdicts["left-fork"] == NOT_MERGED
    assert verdicts["right-fork"] == unknown("history_limit")
    monkeypatch.setattr(checkouts, "SHARED_RANGE_CAP", 6)
    wide = verdicts_of(find_clone(collect([folder]), work))
    assert wide["left-fork"] == wide["right-fork"] == NOT_MERGED


@pytest.mark.skipif(ON_WINDOWS, reason="the version stand-in is a shell script")
def test_git_before_the_patch_fix_skips_the_patch_rungs(tmp_path, monkeypatch):
    folder, _remote, work, _worktree = build_verdict_world(tmp_path)
    shim = git_stand_in(
        tmp_path,
        'case "$*" in *" --version") echo "git version 2.38.0"; exit 0;; esac',
    )
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")

    document = collect([folder])

    assert document["git"] == {"status": "available", "version": "2.38.0"}
    verdicts = verdicts_of(find_clone(document, work))
    for name in ("picked", "squashed", "bin-differs", "bin-picked", "blobdiff"):
        assert verdicts[name] == unknown("git_too_old")
    assert verdicts["merged-noff"] == MERGED_ANCESTOR
    assert verdicts["split"] == MERGED_TREE
    assert all(value[0] != "merged" for name, value in verdicts.items()
               if value and name not in {"merged-noff", "split"})


@pytest.mark.skipif(ON_WINDOWS, reason="the moving stand-in is a shell script")
def test_a_ref_moved_before_the_merged_listing_cannot_borrow_its_verdict(tmp_path, monkeypatch):
    folder, _remote, work, _worktree = build_verdict_world(tmp_path)
    tip = rev(work, "unmerged")
    top = rev(work, "origin/trunk")
    real_git = shutil.which("git")
    done = tmp_path / "moved"
    shim = git_stand_in(
        tmp_path,
        f'case "$*" in *"for-each-ref --merged"*) [ -e "{done}" ] || {{ : > "{done}"; '
        f'"{real_git}" -C "{work}" update-ref refs/heads/unmerged {top}; }};; esac',
    )
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")

    document = collect([folder])

    moved = branch_of(find_clone(document, work), "unmerged")
    assert done.exists()
    assert moved["sha"] == tip
    assert verdict_tuple(moved["verdict"]) == NOT_MERGED


@pytest.mark.skipif(ON_WINDOWS, reason="the moving stand-in is a shell script")
def test_a_merged_ref_moved_to_an_unmerged_commit_keeps_its_recorded_tip(tmp_path, monkeypatch):
    folder, _remote, work, _worktree = build_verdict_world(tmp_path)
    tip = rev(work, "merged-noff")
    child = git(work, "commit-tree", f"{tip}^{{tree}}", "-p", tip, "-m", "unmerged").stdout.strip()
    real_git = shutil.which("git")
    done = tmp_path / "moved"
    shim = git_stand_in(
        tmp_path,
        f'case "$*" in *"for-each-ref --merged"*) [ -e "{done}" ] || {{ : > "{done}"; '
        f'"{real_git}" -C "{work}" update-ref refs/heads/merged-noff {child}; }};; esac',
    )
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")

    document = collect([folder])

    moved = branch_of(find_clone(document, work), "merged-noff")
    assert done.exists()
    assert moved["sha"] == tip
    assert verdict_tuple(moved["verdict"]) == MERGED_ANCESTOR


def test_the_shared_range_cap_evicts_the_largest_branch_even_when_it_comes_first(
    tmp_path, monkeypatch
):
    folder, _remote, work = small_world(tmp_path)
    git(work, "checkout", "-q", "-b", "line-a", "trunk")
    commit_file(work, "a1.txt", "x\n")
    git(work, "checkout", "-q", "-b", "a-fork")
    commit_file(work, "a-fork.txt", "x\n")
    git(work, "checkout", "-q", "-b", "line-b", "trunk")
    for number in (1, 2, 3, 4):
        commit_file(work, f"b{number}.txt", "x\n")
    git(work, "checkout", "-q", "-b", "b-fork")
    commit_file(work, "b-fork.txt", "x\n")
    git(work, "checkout", "-q", "trunk")
    git(work, "merge", "-q", "--no-ff", "-m", "merge a", "line-a")
    git(work, "merge", "-q", "--no-ff", "-m", "merge b", "line-b")
    git(work, "push", "-q", "origin", "trunk")
    git(work, "branch", "-q", "-D", "line-a", "line-b")
    larger = git(work, "rev-list", "--count", "a-fork..origin/trunk").stdout.strip()
    smaller = git(work, "rev-list", "--count", "b-fork..origin/trunk").stdout.strip()
    assert (int(larger), int(smaller)) == (6, 3)
    monkeypatch.setattr(checkouts, "SHARED_RANGE_CAP", 4)

    verdicts = verdicts_of(find_clone(collect([folder]), work))

    assert verdicts["a-fork"] == unknown("history_limit")
    assert verdicts["b-fork"] == NOT_MERGED


def test_the_comparison_cap_limits_a_branch_far_behind_the_base(tmp_path, monkeypatch):
    folder, _remote, work = small_world(tmp_path)
    git(work, "checkout", "-q", "-b", "far", "trunk")
    commit_file(work, "far.txt", "x\n")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "c1.txt", "x\n")
    commit_file(work, "c2.txt", "x\n")
    git(work, "checkout", "-q", "-b", "near")
    commit_file(work, "near.txt", "x\n")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "c3.txt", "x\n")
    git(work, "push", "-q", "origin", "trunk")
    monkeypatch.setattr(checkouts, "COMPARISON_CAP", 2)

    verdicts = verdicts_of(find_clone(collect([folder]), work))

    assert verdicts["far"] == unknown("history_limit")
    assert verdicts["near"] == NOT_MERGED


def test_a_partial_clone_proves_a_strict_ancestor_without_fetching(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    git(remote, "config", "uploadpack.allowFilter", "true")
    push_commit(remote, "trunk", tmp_path / "s1")
    push_commit(remote, "trunk", tmp_path / "s2")
    partial = clone(f"file://{remote}", folder / "partial", "--filter=blob:none")
    git(partial, "branch", "older", "origin/trunk~1")
    assert rev(partial, "older") != rev(partial, "origin/trunk")
    before = (object_files(partial), pack_listing(partial))

    item = find_clone(collect([folder]), partial)

    assert item["partial_clone"] is True
    assert verdicts_of(item)["older"] == MERGED_ANCESTOR
    assert (object_files(partial), pack_listing(partial)) == before


@pytest.mark.skipif(ON_WINDOWS, reason="the failing git stand-in is a shell script")
@pytest.mark.parametrize(
    ("body", "options", "reason"),
    [
        ("exit 1", {}, "failed"),
        ("sleep 3", {"call_cap_seconds": 1.0}, "deadline"),
    ],
    ids=["failed", "timed_out"],
)
def test_a_tip_whose_patch_step_failed_is_never_decided_by_a_later_rung(
    tmp_path, monkeypatch, body, options, reason
):
    folder, _remote, work, _worktree = build_verdict_world(tmp_path)
    split = rev(work, "split")
    assert verdicts_of(find_clone(collect([folder]), work))["split"] == MERGED_TREE
    shim = git_stand_in(
        tmp_path, f'case "$*" in *"rev-list --no-merges {split}.."*) {body};; esac'
    )
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")

    document = collect([folder], **options)

    verdicts = verdicts_of(find_clone(document, work))
    assert verdicts["split"] == unknown(reason)
    assert verdicts["picked"] == MERGED_PATCH


def test_branches_of_unrelated_histories_share_the_whole_base_range(tmp_path, monkeypatch):
    folder, _remote, work = small_world(tmp_path)
    seed = rev(work, "trunk")
    git(work, "checkout", "-q", "--orphan", "second-root")
    git(work, "rm", "-q", "-rf", ".")
    second = commit_file(work, "root.txt", "second root\n")
    git(work, "checkout", "-q", "-b", "y", "second-root")
    commit_file(work, "y.txt", "y\n")
    git(work, "checkout", "-q", "-b", "x", "trunk")
    commit_file(work, "x.txt", "x\n")
    git(work, "checkout", "-q", "trunk")
    git(
        work, "merge", "-q", "--no-ff", "--allow-unrelated-histories", "-m", "join", "second-root"
    )
    git(work, "cherry-pick", "x", "y")
    git(work, "push", "-q", "origin", "trunk")
    git(work, "branch", "-q", "-D", "second-root")
    assert git(work, "merge-base", "--octopus", seed, second, check=False).returncode == 1
    assert rev(work, "x~1") == seed and rev(work, "y~1") == second

    verdicts = verdicts_of(find_clone(collect([folder]), work))

    assert verdicts == {"trunk": None, "x": MERGED_PATCH, "y": MERGED_PATCH}
    # The whole base history holds four non-merge commits; with room for three
    # the later of the two equally far branches is evicted and the other decided.
    monkeypatch.setattr(checkouts, "SHARED_RANGE_CAP", 3)
    limited = verdicts_of(find_clone(collect([folder]), work))
    assert limited == {"trunk": None, "x": MERGED_PATCH, "y": unknown("history_limit")}


# ---------------------------------------------------------------------------
# The verdict cache and the verdict deadline
# ---------------------------------------------------------------------------

REPOSITORY_ROOT = Path(__file__).parents[1].resolve()
NOT_ROOT = ON_WINDOWS or os.geteuid() != 0


def nyx_home(home: Path) -> Path:
    """Create ``~/.nyx`` in the isolated home and return the cache file path."""
    (home / ".nyx").mkdir(exist_ok=True)
    return home / ".nyx" / "cache" / "checkouts.json"


def cache_entries(cache: Path) -> dict[str, str]:
    document = json.loads(cache.read_text())
    assert document["schema_version"] == 3
    assert set(document) == {"schema_version", "verdicts", "remote_checks"}
    return {key: entry["rung"] for key, entry in document["verdicts"].items()}


def stable(document: dict[str, Any]) -> dict[str, Any]:
    """The document without what a cache diagnostic or the clock may change."""
    return {
        key: value
        for key, value in document.items()
        if key not in {"observed_at", "checkouts_digest", "diagnostics"}
    }


def stand_in(tmp_path: Path, name: str, body: str) -> Path:
    shim = tmp_path / name
    shim.mkdir()
    script = shim / "git"
    script.write_text(f'#!/bin/sh\n{body}\nexec "{shutil.which("git")}" "$@"\n')
    script.chmod(0o755)
    return shim


@contextlib.contextmanager
def path_with(monkeypatch, shim: Path):
    original = os.environ["PATH"]
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{original}")
    try:
        yield
    finally:
        monkeypatch.setenv("PATH", original)


def recording(tmp_path: Path, name: str) -> tuple[Path, Path]:
    log = tmp_path / f"{name}.log"
    return stand_in(tmp_path, name, f'printf "%s\\n" "$*" >> "{log}"'), log


def patch_work(log: Path) -> list[str]:
    calls = [line.split() for line in log.read_text().splitlines()] if log.exists() else []
    return [
        " ".join(call)
        for call in calls
        if "diff" in call or "patch-id" in call or ("log" in call and "-p" in call)
    ]


def cache_world(tmp_path: Path, tag: str = ""):
    """One clone with a branch per cached outcome, all judged against ``trunk``."""
    tmp_path.mkdir(exist_ok=True)
    folder, _remote, work = small_world(tmp_path)

    def branch(name: str) -> None:
        git(work, "checkout", "-q", "-b", name, "trunk")

    branch("both")
    commit_file(work, "both.txt", "both\n", "add both")
    branch("noff")
    commit_file(work, "noff.txt", "n\n")
    branch("picked")
    commit_file(work, "p1.txt", "p1\n")
    commit_file(work, "p2.txt", "p2\n")
    branch("unmerged")
    commit_file(work, f"u{tag}.txt", f"u{tag}\n")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "both.txt", "both\n", "land both by hand")
    commit_file(work, "d1.txt", "diverged\n")
    git(work, "cherry-pick", "picked~1", "picked")
    git(work, "merge", "-q", "--no-ff", "-m", "merge noff", "noff")
    git(work, "push", "-q", "origin", "trunk")
    return folder, work


CACHE_WORLD_VERDICTS = {
    "both": MERGED_PATCH,
    "noff": MERGED_ANCESTOR,
    "picked": MERGED_PATCH,
    "unmerged": NOT_MERGED,
    "trunk": None,
}


def cache_times(cache: Path) -> dict[str, int]:
    return {key: entry["at"] for key, entry in json.loads(cache.read_text())["verdicts"].items()}


@pytest.mark.skipif(ON_WINDOWS, reason="the recording git stand-in is a shell script")
def test_a_warm_collection_decides_again_and_stamps_what_it_verified(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, work = cache_world(tmp_path)
    first = collect([folder])
    assert verdicts_of(find_clone(first, work)) == CACHE_WORLD_VERDICTS
    base = rev(work, "origin/trunk")
    # Only complete, non-ancestor verdicts are stored.
    stored = {
        f"{rev(work, 'both')}.{base}": "patch_equivalent",
        f"{rev(work, 'picked')}.{base}": "patch_equivalent",
        f"{rev(work, 'unmerged')}.{base}": "none",
    }
    assert cache_entries(cache) == stored
    write_cache(cache, {key: 10 for key in stored}, rungs=stored)
    shim, log = recording(tmp_path, "warm")

    with path_with(monkeypatch, shim):
        second = collect([folder])

    assert second["checkouts_digest"] == first["checkouts_digest"]
    assert verdicts_of(find_clone(second, work)) == CACHE_WORLD_VERDICTS
    # The cache fills only what the run leaves unknown, so the rungs run again,
    # and every verdict the run decided again is stamped as verified now.
    assert patch_work(log) != []
    assert cache_entries(cache) == stored
    assert all(at > 10 for at in cache_times(cache).values())


@pytest.mark.skipif(ON_WINDOWS, reason="the delaying git stand-in is a shell script")
def test_a_collection_that_verifies_nothing_rewrites_nothing(tmp_path, monkeypatch, isolated_git):
    cache = nyx_home(isolated_git)
    folder, work = cache_world(tmp_path)
    collect([folder])
    before = (cache.read_bytes(), cache.stat().st_mtime_ns)
    slow = stand_in(tmp_path, "slow", DELAYED_PATCH_WORK)

    with path_with(monkeypatch, slow):
        document = collect([folder], seconds=3.0)

    # Every re-check ran out of time, so saved answers stood in and were not re-stamped.
    assert verdicts_of(find_clone(document, work)) == CACHE_WORLD_VERDICTS
    assert document["diagnostics"] == []
    assert (cache.read_bytes(), cache.stat().st_mtime_ns) == before


def invalid_cache_payloads(valid: bytes) -> dict[str, bytes]:
    document = json.loads(valid)
    good_key = next(iter(document["verdicts"]))
    return {
        "garbage": b"not json {",
        "extra_key": json.dumps({**document, "extra": 1}).encode(),
        "foreign_schema": json.dumps({**document, "schema_version": 4}).encode(),
        "ancestor_rung": json.dumps(
            {
                "schema_version": 3,
                "verdicts": {good_key: {"rung": "ancestor", "at": 1}},
                "remote_checks": {},
            }
        ).encode(),
        "key_lengths": json.dumps(
            {
                "schema_version": 3,
                "verdicts": {"a" * 40 + "." + "b" * 64: {"rung": "none", "at": 1}},
                "remote_checks": {},
            }
        ).encode(),
        "oversized": b" " * (5 * 1024 * 1024),
    }


@pytest.mark.parametrize(
    "kind",
    ["garbage", "extra_key", "foreign_schema", "ancestor_rung", "key_lengths", "oversized", "symlink"],
)
def test_an_unreadable_cache_changes_only_a_diagnostic_and_is_repaired(
    tmp_path, isolated_git, kind
):
    cache = nyx_home(isolated_git)
    folder, work = cache_world(tmp_path)
    cold = collect([folder])
    valid = cache.read_bytes()
    cache.unlink()
    if kind == "symlink":
        target = tmp_path / "elsewhere.json"
        target.write_bytes(valid)
        try:
            cache.parent.mkdir(exist_ok=True)
            cache.symlink_to(target)
        except OSError:
            pytest.skip("this lane does not grant the symbolic-link privilege")
    else:
        cache.write_bytes(invalid_cache_payloads(valid)[kind])

    document = collect([folder])

    assert stable(document) == stable(cold)
    assert document["diagnostics"] == [
        *cold["diagnostics"],
        {"code": "checkout_cache_unreadable", "path": str(cache)},
    ]
    assert not cache.is_symlink()
    assert cache_entries(cache) == cache_entries_of(valid)
    if kind == "symlink":
        assert target.read_bytes() == valid


def cache_entries_of(data: bytes) -> dict[str, str]:
    return {key: entry["rung"] for key, entry in json.loads(data)["verdicts"].items()}


@pytest.mark.skipif(not NOT_ROOT or ON_WINDOWS, reason="needs a read-only directory that binds")
def test_a_read_only_cache_directory_gives_a_write_diagnostic(tmp_path, isolated_git):
    cache = nyx_home(isolated_git)
    folder, work = cache_world(tmp_path)
    cold = collect([folder])
    cache.unlink()
    cache.parent.chmod(0o500)
    try:
        document = collect([folder])
    finally:
        cache.parent.chmod(0o700)

    assert stable(document) == stable(cold)
    assert document["diagnostics"] == [
        *cold["diagnostics"],
        {"code": "checkout_cache_write_failed", "path": str(cache)},
    ]
    assert not cache.exists()


def test_without_a_state_directory_nothing_is_read_written_or_reported(tmp_path, isolated_git):
    folder, work = cache_world(tmp_path)
    assert not (isolated_git / ".nyx").exists()

    document = collect([folder])

    assert verdicts_of(find_clone(document, work)) == CACHE_WORLD_VERDICTS
    assert document["diagnostics"] == []
    assert not (isolated_git / ".nyx").exists()


def test_a_run_with_nothing_to_store_creates_neither_cache_directory_nor_file(
    tmp_path, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, _remote, work = small_world(tmp_path)
    git(work, "checkout", "-q", "-b", "merged", "trunk")
    commit_file(work, "m.txt", "m\n")
    git(work, "checkout", "-q", "trunk")
    git(work, "merge", "-q", "--no-ff", "-m", "merge it", "merged")
    git(work, "push", "-q", "origin", "trunk")

    document = collect([folder])

    assert verdicts_of(find_clone(document, work)) == {"merged": MERGED_ANCESTOR, "trunk": None}
    assert document["diagnostics"] == []
    assert list((isolated_git / ".nyx").iterdir()) == []
    assert not cache.parent.exists()


def incomplete_world(tmp_path: Path, option: str):
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    git(remote, "config", "uploadpack.allowFilter", "true")
    push_commit(remote, "trunk", tmp_path / "s1")
    incomplete = clone(f"file://{remote}", folder / "incomplete", option)
    git(incomplete, "checkout", "-q", "-b", "other", "origin/trunk")
    commit_file(incomplete, "other.txt", "other\n")
    return folder, incomplete


@pytest.mark.parametrize("option", ["--depth=1", "--filter=blob:none"])
def test_incomplete_clones_leave_an_existing_cache_file_untouched(tmp_path, isolated_git, option):
    cache = nyx_home(isolated_git)
    cache.parent.mkdir()
    stored = json.dumps(
        {
            "schema_version": 3,
            "verdicts": {"1" * 40 + "." + "2" * 40: {"rung": "none", "at": 5}},
            "remote_checks": {},
        }
    ).encode()
    cache.write_bytes(stored)
    folder, incomplete = incomplete_world(tmp_path, option)

    document = collect([folder])

    assert verdicts_of(find_clone(document, incomplete))["other"] == unknown("incomplete_clone")
    assert document["diagnostics"] == []
    assert cache.read_bytes() == stored


def test_a_cache_hit_gives_a_shallow_clone_the_full_clones_verdict(tmp_path, isolated_git):
    cache = nyx_home(isolated_git)
    served = tmp_path / "served"
    served.mkdir()
    full_folder = tmp_path / "full"
    shallow_folder = tmp_path / "shallow"
    full_folder.mkdir()
    shallow_folder.mkdir()
    remote = make_remote(served)
    work = clone(remote, full_folder / "work")
    git(work, "checkout", "-q", "-b", "picked", "trunk")
    commit_file(work, "picked.txt", "picked\n")
    git(work, "push", "-q", "origin", "picked")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "d1.txt", "diverged\n")
    git(work, "cherry-pick", "picked")
    git(work, "push", "-q", "origin", "trunk")
    shallow = clone(f"file://{remote}", shallow_folder / "shallow", "--depth=1", "--no-single-branch")
    git(shallow, "checkout", "-q", "-b", "picked", "origin/picked")

    full = verdicts_of(find_clone(collect([full_folder]), work))
    assert full["picked"] == MERGED_PATCH
    stored = cache.read_bytes()
    cache.unlink()
    cold = find_clone(collect([shallow_folder]), shallow)
    assert cold["shallow"] is True
    assert verdicts_of(cold)["picked"] == unknown("incomplete_clone")
    cache.write_bytes(stored)

    warm = find_clone(collect([shallow_folder]), shallow)

    assert verdicts_of(warm)["picked"] == MERGED_PATCH
    assert cache.read_bytes() == stored


@pytest.mark.skipif(ON_WINDOWS, reason="the version stand-in is a shell script")
def test_git_before_the_patch_fix_stores_nothing_and_serves_cached_verdicts(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, work = cache_world(tmp_path)
    old = stand_in(
        tmp_path, "old", 'case "$*" in *" --version") echo "git version 2.38.0"; exit 0;; esac'
    )
    with path_with(monkeypatch, old):
        cold = collect([folder])
    assert not cache.parent.exists()  # not even the tree_equal verdict is stored
    assert verdicts_of(find_clone(cold, work)) == {
        "both": MERGED_TREE,
        "noff": MERGED_ANCESTOR,
        "picked": unknown("git_too_old"),
        "unmerged": unknown("git_too_old"),
        "trunk": None,
    }
    collect([folder])
    assert cache_entries(cache)

    with path_with(monkeypatch, old):
        warm = collect([folder])

    # A hit stands in for git_too_old, but never replaces a rung the cold run decides.
    assert verdicts_of(find_clone(warm, work)) == {
        "both": MERGED_TREE,
        "noff": MERGED_ANCESTOR,
        "picked": MERGED_PATCH,
        "unmerged": NOT_MERGED,
        "trunk": None,
    }


@pytest.mark.skipif(ON_WINDOWS, reason="the version stand-in is a shell script")
def test_a_stronger_cached_rung_never_changes_what_old_git_reports(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, _remote, work = small_world(tmp_path)
    git(work, "checkout", "-q", "-b", "both", "trunk")
    commit_file(work, "both.txt", "both\n", "add both")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "both.txt", "both\n", "land both by hand")
    commit_file(work, "d1.txt", "diverged\n")
    git(work, "push", "-q", "origin", "trunk")
    old = stand_in(
        tmp_path, "old", 'case "$*" in *" --version") echo "git version 2.38.0"; exit 0;; esac'
    )
    with path_with(monkeypatch, old):
        cold_old = collect([folder])
    assert verdicts_of(find_clone(cold_old, work))["both"] == MERGED_TREE
    assert not cache.parent.exists()

    real_cold = collect([folder])
    real_warm = collect([folder])
    assert verdicts_of(find_clone(real_cold, work))["both"] == MERGED_PATCH
    assert real_warm["checkouts_digest"] == real_cold["checkouts_digest"]
    assert list(cache_entries(cache).values()) == ["patch_equivalent"]

    with path_with(monkeypatch, old):
        warm_old = collect([folder])

    assert verdicts_of(find_clone(warm_old, work))["both"] == MERGED_TREE
    assert warm_old["checkouts_digest"] == cold_old["checkouts_digest"]


COLLECT_PROCESS = (
    "import sys, time\n"
    "from nyx import checkouts\n"
    "print(checkouts.collect_checkouts([sys.argv[1]], deadline_ns=time.monotonic_ns() + 60_000_000_000))\n"
)


def test_two_collector_processes_over_different_clones_both_complete(tmp_path, isolated_git):
    cache = nyx_home(isolated_git)
    folders = []
    for name in ("one", "two"):
        folder, _work = cache_world(tmp_path / name, tag=name)
        folders.append(folder)
    environment = dict(os.environ)
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", COLLECT_PROCESS, str(folder)],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            text=True,
        )
        for folder in folders
    ]
    outputs = [process.communicate(timeout=120)[0] for process in processes]

    assert [process.returncode for process in processes] == [0, 0]
    for output in outputs:
        document = models.parse_checkouts(output)
        codes = [item["code"] for item in document["diagnostics"]]
        # A replace over a file the other process holds open may fail on Windows only.
        assert codes == [] or (
            ON_WINDOWS and codes in (["checkout_cache_unreadable"], ["checkout_cache_write_failed"])
        )
        assert all(
            branch["verdict"] is not None or branch["name"] == "trunk"
            for item in all_clones(document)
            for branch in item["branches"]
        )
    assert cache_entries(cache)  # the file parses afterwards


@pytest.mark.skipif(ON_WINDOWS, reason="the lane's os.replace count is proven on POSIX")
def test_one_collection_replaces_the_cache_file_exactly_once(tmp_path, monkeypatch, isolated_git):
    cache = nyx_home(isolated_git)
    worlds = [cache_world(tmp_path / name, tag=name) for name in ("one", "two")]
    folders = [folder for folder, _work in worlds]
    real_replace = os.replace
    destinations: list[str] = []

    def counting(source, destination, *args, **kwargs):
        destinations.append(os.fspath(destination))
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", counting)

    collect(folders)

    assert destinations == [str(cache)]
    stored = cache_entries(cache)
    for _folder, work in worlds:  # each clone's newly decided verdict is in the one write
        assert f"{rev(work, 'unmerged')}.{rev(work, 'origin/trunk')}" in stored


def test_stale_temporaries_are_removed_by_the_next_write_and_recent_ones_stay(
    tmp_path, isolated_git
):
    cache = nyx_home(isolated_git)
    cache.parent.mkdir()
    old = cache.parent / ".checkouts.json.stale1"
    recent = cache.parent / ".checkouts.json.fresh1"
    other = cache.parent / ".other.json.stale1"
    for item, age in ((old, 660), (recent, 60), (other, 660)):
        item.write_text("x")
        stamp = time.time() - age
        os.utime(item, (stamp, stamp))
    folder, _work = cache_world(tmp_path)

    collect([folder])

    assert not old.exists()
    assert recent.exists() and other.exists()
    assert cache.is_file()


def branch_cache_world(tmp_path: Path, names: list[str]):
    folder, _remote, work = small_world(tmp_path)
    for name in names:
        git(work, "checkout", "-q", "-b", name, "trunk")
        commit_file(work, f"{name}.txt", f"{name}\n")
    git(work, "checkout", "-q", "trunk")
    base = rev(work, "origin/trunk")
    return folder, work, {name: f"{rev(work, name)}.{base}" for name in names}


def write_cache(
    cache: Path,
    entries: dict[str, int],
    rungs: dict[str, str] | None = None,
    remote_checks: dict[str, Any] | None = None,
) -> None:
    cache.parent.mkdir(exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "verdicts": {
                    key: {"rung": (rungs or {}).get(key, "none"), "at": at}
                    for key, at in entries.items()
                },
                "remote_checks": remote_checks or {},
            }
        )
    )


def test_eviction_keeps_the_used_entries_and_drops_the_oldest(tmp_path, monkeypatch, isolated_git):
    cache = nyx_home(isolated_git)
    folder, _work, keys = branch_cache_world(tmp_path, [f"b{n}" for n in range(1, 7)])
    base = keys["b1"].split(".")[1]
    gone = {f"{letter * 40}.{base}": at for letter, at in zip("cdef", (10, 20, 30, 40))}
    write_cache(cache, gone)
    monkeypatch.setattr(checkouts, "CACHE_ENTRY_CAP", 8)

    collect([folder])

    # The six verified entries are kept; of the four no branch uses, the newest two.
    newest = {key for key, at in gone.items() if at >= 30}
    assert set(cache_entries(cache)) == set(keys.values()) | newest


@pytest.mark.skipif(ON_WINDOWS, reason="the failing git stand-in is a shell script")
def test_eviction_drops_unused_newer_entries_and_breaks_ties_by_key(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, work, keys = branch_cache_world(tmp_path, ["b1", "b2", "b3", "b4"])
    base = keys["b1"].split(".")[1]
    stale = "c" * 40 + "." + base
    write_cache(cache, {stale: 4_000_000_000, keys["b1"]: 10, keys["b2"]: 10})
    # b1 and b2 cannot be re-checked, so their saved answers stand in (used);
    # b3 and b4 are verified by the run.
    broken = stand_in(
        tmp_path,
        "broken",
        f'case "$*" in *"...{rev(work, "b1")}"*|*"...{rev(work, "b2")}"*) exit 1;; esac',
    )
    monkeypatch.setattr(checkouts, "CACHE_ENTRY_CAP", 3)

    with path_with(monkeypatch, broken):
        collect([folder])

    assert set(cache_entries(cache)) == {keys["b3"], keys["b4"], min(keys["b1"], keys["b2"])}
    assert stale not in cache_entries(cache)


def deadline_world(tmp_path: Path):
    folder, _remote, work = small_world(tmp_path)
    for name in ("a-fast", "z-slow"):
        git(work, "checkout", "-q", "-b", name, "trunk")
        commit_file(work, f"{name}.txt", f"{name}\n")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "d1.txt", "diverged\n")
    git(work, "cherry-pick", "a-fast", "z-slow")
    git(work, "push", "-q", "origin", "trunk")
    return folder, work


DELAYED_PATCH_WORK = 'case "$*" in *" log "*|*" diff "*|*" patch-id"*) exec sleep 30;; esac'


@pytest.mark.skipif(ON_WINDOWS, reason="the delaying git stand-in is a shell script")
def test_patch_work_past_the_deadline_is_unknown_then_decided_then_cached(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, work = deadline_world(tmp_path)
    slow = stand_in(tmp_path, "slow", DELAYED_PATCH_WORK)

    with path_with(monkeypatch, slow):
        limited = collect([folder], seconds=3.0)
    assert verdicts_of(find_clone(limited, work)) == {
        "a-fast": unknown("deadline"),
        "z-slow": unknown("deadline"),
        "trunk": None,
    }
    assert not cache.exists()

    decided = collect([folder])
    assert verdicts_of(find_clone(decided, work)) == {
        "a-fast": MERGED_PATCH,
        "z-slow": MERGED_PATCH,
        "trunk": None,
    }

    with path_with(monkeypatch, slow):
        again = collect([folder], seconds=3.0)
    assert verdicts_of(find_clone(again, work)) == verdicts_of(find_clone(decided, work))
    assert again["diagnostics"] == []


@pytest.mark.skipif(ON_WINDOWS, reason="the delaying git stand-in is a shell script")
def test_the_verdict_reserve_leaves_time_to_cache_the_branch_that_finished(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, work = deadline_world(tmp_path)
    base = rev(work, "origin/trunk")
    fast, slow_tip = rev(work, "a-fast"), rev(work, "z-slow")
    slow = stand_in(
        tmp_path, "slow", f'case "$*" in *" log "*"{slow_tip}"*) exec sleep 30;; esac'
    )

    with path_with(monkeypatch, slow):
        document = collect([folder], seconds=3.0)

    assert verdicts_of(find_clone(document, work)) == {
        "a-fast": MERGED_PATCH,
        "z-slow": unknown("deadline"),
        "trunk": None,
    }
    assert document["diagnostics"] == []
    assert cache_entries(cache) == {f"{fast}.{base}": "patch_equivalent"}

    # The control: without the reserve the slow call runs to the real deadline
    # and the write has no time left, so nothing is cached.
    cache.unlink()
    monkeypatch.setattr(checkouts, "VERDICT_RESERVE_NS", 0)
    with path_with(monkeypatch, slow):
        unreserved = collect([folder], seconds=3.0)
    assert verdicts_of(find_clone(unreserved, work))["z-slow"] == unknown("deadline")
    assert unreserved["diagnostics"] == [
        {"code": "checkout_cache_write_failed", "path": str(cache)}
    ]
    assert not cache.exists()


def own_step_world(tmp_path: Path):
    """``one`` is cherry-picked onto a diverged trunk; ``other`` is unmerged."""
    folder, _remote, work = small_world(tmp_path)
    for name in ("one", "other"):
        git(work, "checkout", "-q", "-b", name, "trunk")
        commit_file(work, f"{name}.txt", f"{name}\n")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "diverged.txt", "diverged\n")
    git(work, "cherry-pick", "one")
    git(work, "push", "-q", "origin", "trunk")
    return folder, work


OWN_STEP_VERDICTS = {"one": MERGED_PATCH, "other": NOT_MERGED, "trunk": None}


@pytest.mark.skipif(ON_WINDOWS, reason="the failing git stand-in is a shell script")
@pytest.mark.parametrize(
    ("body", "options", "reason"),
    [
        ("exit 1", {}, "failed"),
        ("sleep 3", {"call_cap_seconds": 1.0}, "deadline"),
    ],
    ids=["failed", "timed_out"],
)
def test_a_failed_own_commit_step_is_unknown_and_never_cached(
    tmp_path, monkeypatch, isolated_git, body, options, reason
):
    cache = nyx_home(isolated_git)
    folder, work = own_step_world(tmp_path)
    base, one, other = rev(work, "origin/trunk"), rev(work, "one"), rev(work, "other")
    cold = collect([folder])
    assert verdicts_of(find_clone(cold, work)) == OWN_STEP_VERDICTS
    cache.unlink()
    # Only the patch IDs of ``one``'s own commits fail; its single merge base
    # would still let the squash rung decide it.
    fault = stand_in(
        tmp_path, "fault", f'case "$*" in *" log "*"{base}..{one} --"*) {body};; esac'
    )

    with path_with(monkeypatch, fault):
        faulted = collect([folder], **options)

    assert verdicts_of(find_clone(faulted, work)) == {**OWN_STEP_VERDICTS, "one": unknown(reason)}
    assert cache_entries(cache) == {f"{other}.{base}": "none"}
    warm = collect([folder])
    assert stable(warm) == stable(cold)
    assert warm["checkouts_digest"] == cold["checkouts_digest"]


def test_a_tip_that_recorded_a_failure_stores_no_verdict(tmp_path, monkeypatch, isolated_git):
    cache = nyx_home(isolated_git)
    folder, work = own_step_world(tmp_path)
    base, one, other = rev(work, "origin/trunk"), rev(work, "one"), rev(work, "other")
    original = checkouts._Ladder._patch_rungs

    # A rung that decides a tip after one of its steps failed must not reach the cache.
    def decided_after_a_failure(self, tip, mapped):
        original(self, tip, mapped)
        if tip.sha == one:
            tip.fail("failed")
            tip.verdict = checkouts._merged("squash_equivalent")

    monkeypatch.setattr(checkouts._Ladder, "_patch_rungs", decided_after_a_failure)

    document = collect([folder])

    assert verdicts_of(find_clone(document, work))["one"] == MERGED_SQUASH
    assert cache_entries(cache) == {f"{other}.{base}": "none"}


def test_a_cached_verdict_survives_a_shared_range_that_would_evict_it(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, _remote, work = small_world(tmp_path)
    git(work, "checkout", "-q", "-b", "side", "trunk")
    for number in range(1, 6):
        commit_file(work, f"s{number}.txt", "x\n")
    side_tip = rev(work, "side")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "c1.txt", "x\n")
    commit_file(work, "c2.txt", "x\n")
    git(work, "checkout", "-q", "-b", "bee", "trunk")
    commit_file(work, "bee.txt", "x\n")
    git(work, "checkout", "-q", "trunk")
    git(work, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    git(work, "push", "-q", "origin", "trunk")
    git(work, "branch", "-q", "-D", "side")
    monkeypatch.setattr(checkouts, "SHARED_RANGE_CAP", 5)

    alone = collect([folder])
    assert verdicts_of(find_clone(alone, work)) == {"bee": NOT_MERGED, "trunk": None}
    stored = cache.read_bytes()
    git(work, "checkout", "-q", "-b", "aye", side_tip)
    commit_file(work, "aye.txt", "x\n")
    cache.unlink()

    cold = collect([folder])
    assert verdicts_of(find_clone(cold, work)) == {
        "aye": NOT_MERGED, "bee": unknown("history_limit"), "trunk": None,
    }
    cache.write_bytes(stored)
    warm = collect([folder])
    assert verdicts_of(find_clone(warm, work)) == {
        "aye": NOT_MERGED, "bee": NOT_MERGED, "trunk": None,
    }


def cherry_picked_world(tmp_path: Path, names: list[str]):
    """Every branch is cherry-picked onto a diverged ``trunk``, so each is patch-equivalent."""
    folder, _remote, work = small_world(tmp_path)
    for name in names:
        git(work, "checkout", "-q", "-b", name, "trunk")
        commit_file(work, f"{name}.txt", f"{name}\n")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "diverged.txt", "diverged\n")
    git(work, "cherry-pick", *names)
    git(work, "push", "-q", "origin", "trunk")
    return folder, work


@pytest.mark.skipif(ON_WINDOWS, reason="the delaying git stand-in is a shell script")
def test_a_clone_over_budget_converges_across_short_collections(
    tmp_path, monkeypatch, isolated_git
):
    nyx_home(isolated_git)
    names = [f"b{number}" for number in range(1, 6)]
    folder, work = cherry_picked_world(tmp_path, names)
    base = rev(work, "origin/trunk")
    cases = "|".join(f'*" log "*"{base}..{rev(work, name)} --"*' for name in names)
    slow = stand_in(tmp_path, "slow", f'case "$*" in {cases}) sleep 1;; esac')

    runs = []
    with path_with(monkeypatch, slow):
        for _ in range(MOST_SHORT_COLLECTIONS):
            runs.append(verdicts_of(find_clone(collect([folder], seconds=4.0), work)))
            if unknown("deadline") not in runs[-1].values():
                break

    # The deadline path is exercised: the first collection leaves work undecided.
    assert unknown("deadline") in runs[0].values()
    # Progress is monotone: a decided tip never returns to unknown, and no
    # collection decides fewer tips than the one before it.
    for earlier, later in zip(runs, runs[1:]):
        for name, verdict in earlier.items():
            if verdict is not None and verdict[0] != "unknown":
                assert later[name] == verdict
        assert decided(later) >= decided(earlier)
    # Short collections add up to a verdict for every branch.
    assert all(runs[-1][name] == MERGED_PATCH for name in names)


MOST_SHORT_COLLECTIONS = 6


def decided(verdicts: dict[str, Any]) -> int:
    return sum(1 for verdict in verdicts.values() if verdict is not None and verdict[0] != "unknown")


@pytest.mark.skipif(ON_WINDOWS, reason="the delaying git stand-in is a shell script")
def test_an_uncached_tip_is_decided_whole_before_cached_tips_use_the_budget(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    names = [f"b{number}" for number in range(1, 6)]
    folder, work = cherry_picked_world(tmp_path, names)
    base = rev(work, "origin/trunk")
    collect([folder])
    assert len(cache_entries(cache)) == 5
    # ``u`` proves nothing; ``t`` is a commit and its revert, decided by no rung.
    git(work, "checkout", "-q", "-b", "u", "trunk")
    commit_file(work, "u.txt", "u\n")
    git(work, "checkout", "-q", "-b", "t", "trunk")
    commit_file(work, "t.txt", "t\n")
    git(work, "revert", "--no-edit", "HEAD")
    git(work, "checkout", "-q", "trunk")
    log = tmp_path / "order.log"
    cases = "|".join(f'*" log "*"{base}..{rev(work, name)} --"*' for name in [*names, "u", "t"])
    slow = stand_in(
        tmp_path, "slow", f'case "$*" in {cases}) printf "%s\\n" "$*" >> "{log}"; sleep 1;; esac'
    )

    with path_with(monkeypatch, slow):
        collect([folder], seconds=4.0)
        second = collect([folder], seconds=4.0)

    verdicts = verdicts_of(find_clone(second, work))
    assert verdicts["u"] == NOT_MERGED
    assert verdicts["t"] == NOT_MERGED
    assert all(verdicts[name] == MERGED_PATCH for name in names)
    first_calls = log.read_text().splitlines()
    tips = {rev(work, name): name for name in [*names, "u", "t"]}
    worked = [
        tips[token.split("..")[1]]
        for line in first_calls
        for token in line.split()
        if ".." in token
    ]
    # Across both runs the uncached tips are worked before any cached one.
    assert set(worked[:2]) == {"u", "t"}
    assert f"{rev(work, 'u')}.{base}" in cache_entries(cache)


@pytest.mark.skipif(ON_WINDOWS, reason="the delaying git stand-in is a shell script")
def test_an_uncached_tree_equal_tip_is_decided_before_cached_tips_use_the_budget(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    names = [f"b{number}" for number in range(1, 6)]
    folder, work = cherry_picked_world(tmp_path, names)
    # ``te`` adds two files in one commit; trunk gets them as two separate commits.
    git(work, "checkout", "-q", "-b", "te", "trunk")
    (work / "te1.txt").write_text("1\n")
    (work / "te2.txt").write_text("2\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "te")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "te1.txt", "1\n")
    commit_file(work, "te2.txt", "2\n")
    git(work, "push", "-q", "origin", "trunk")
    base, te = rev(work, "origin/trunk"), rev(work, "te")
    first = collect([folder])
    assert verdicts_of(find_clone(first, work))["te"] == ("merged", "tree_equal", None)
    document = json.loads(cache.read_text())
    assert document["verdicts"].pop(f"{te}.{base}")["rung"] == "tree_equal"
    cache.write_text(json.dumps(document))
    cases = "|".join(f'*" log "*"{base}..{rev(work, name)} --"*' for name in [*names, "te"])
    slow = stand_in(tmp_path, "slow", f'case "$*" in {cases}) sleep 1;; esac')

    with path_with(monkeypatch, slow):
        collect([folder], seconds=4.0)
        second = collect([folder], seconds=4.0)

    assert verdicts_of(find_clone(second, work))["te"] == ("merged", "tree_equal", None)
    assert cache_entries(cache)[f"{te}.{base}"] == "tree_equal"


@pytest.mark.skipif(ON_WINDOWS, reason="the recording git stand-in is a shell script")
def test_uncached_tips_are_worked_first_then_the_oldest_saved_answer(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, work, keys = branch_cache_world(tmp_path, ["n1", "n2", "n3", "n4"])
    write_cache(cache, {keys["n1"]: 200, keys["n2"]: 100})
    shim, log = recording(tmp_path, "order")

    with path_with(monkeypatch, shim):
        collect([folder])

    tips = {rev(work, name): name for name in ("n1", "n2", "n3", "n4")}
    sized = [
        tips[token.split("...")[1]]
        for line in log.read_text().splitlines()
        if "--left-right" in line
        for token in line.split()
        if "..." in token
    ]
    assert sized == ["n3", "n4", "n2", "n1"]


@pytest.mark.skipif(ON_WINDOWS, reason="the failing git stand-in is a shell script")
def test_a_saved_answer_stands_in_only_where_the_recheck_does_not_finish(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, work = own_step_world(tmp_path)
    base, one, other = rev(work, "origin/trunk"), rev(work, "one"), rev(work, "other")
    cold = collect([folder])
    assert verdicts_of(find_clone(cold, work)) == OWN_STEP_VERDICTS
    stored = {f"{one}.{base}": "patch_equivalent", f"{other}.{base}": "none"}
    assert cache_entries(cache) == stored
    write_cache(cache, {key: 10 for key in stored}, rungs=stored)
    # The re-check of ``one`` fails; ``other`` is re-checked and finishes.
    fault = stand_in(
        tmp_path, "fault", f'case "$*" in *" log "*"{base}..{one} --"*) exit 1;; esac'
    )

    with path_with(monkeypatch, fault):
        warm = collect([folder])

    assert verdicts_of(find_clone(warm, work)) == OWN_STEP_VERDICTS
    assert warm["checkouts_digest"] == cold["checkouts_digest"]
    times = cache_times(cache)
    assert times[f"{one}.{base}"] == 10
    assert times[f"{other}.{base}"] > 10


@pytest.mark.skipif(ON_WINDOWS, reason="the account home comes from HOME on POSIX only")
def test_an_unusable_account_home_leaves_the_cache_unavailable_without_a_diagnostic(
    tmp_path, isolated_git
):
    folder, work = cache_world(tmp_path)
    cold = collect([folder])
    environment = dict(os.environ)
    environment["HOME"] = "relative-home"
    environment.pop("USERPROFILE", None)
    # Git still needs its isolated global configuration.
    environment["GIT_CONFIG_GLOBAL"] = str(isolated_git / ".gitconfig")
    environment["XDG_CONFIG_HOME"] = str(isolated_git)

    completed = subprocess.run(
        [sys.executable, "-c", COLLECT_PROCESS, str(folder)],
        cwd=tmp_path,
        env={**environment, "PYTHONPATH": str(REPOSITORY_ROOT)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    document = models.parse_checkouts(completed.stdout)
    assert stable(document) == stable(cold)
    assert document["diagnostics"] == []
    assert verdicts_of(find_clone(document, work)) == CACHE_WORLD_VERDICTS
    assert not (tmp_path / "relative-home").exists()


# ---------------------------------------------------------------------------
# Supported clone size and the work a clone costs
# ---------------------------------------------------------------------------


def import_branches(repository: Path, count: int, tag: str = "b") -> list[str]:
    """Add ``count`` one-commit branches, each unmerged, in one ``git fast-import`` run.

    Every branch changes its own file, so none is an ancestor of the default
    branch, patch-equivalent to a commit on it or tree-equal to one.
    """
    root = git(repository, "rev-parse", "HEAD").stdout.strip()
    names = [f"{tag}{number:05d}" for number in range(count)]
    stream = b"".join(
        (
            f"commit refs/heads/{name}\ncommitter T <t@example.com> 1700000000 +0000\n"
            f"data {len(name) + 1}\n{name}\nfrom {root}\n"
            f"M 100644 inline {name}.txt\ndata 2\nx\n\n"
        ).encode("ascii")
        for name in names
    )
    subprocess.run(
        ["git", "fast-import", "--quiet"],
        cwd=repository,
        input=stream,
        capture_output=True,
        check=True,
    )
    return names


def git_subcommand(arguments: Any) -> str:
    """The git subcommand of a runner call, past global options and ``-c`` pairs."""
    pending = iter(arguments)
    for argument in pending:
        if argument == "-c":
            next(pending, None)
        elif not argument.startswith("-"):
            return argument
    return arguments[0] if arguments else ""


@contextlib.contextmanager
def counted_git_calls(monkeypatch):
    """Record the subcommand of every ``_GitRunner.run`` and ``pipeline`` call."""
    calls: list[str] = []
    run, pipeline = checkouts._GitRunner.run, checkouts._GitRunner.pipeline

    def counting_run(self, arguments, **options):
        calls.append(git_subcommand(arguments))
        return run(self, arguments, **options)

    def counting_pipeline(self, producer, consumer, **options):
        calls.append(git_subcommand(producer))
        return pipeline(self, producer, consumer, **options)

    with monkeypatch.context() as patched:
        patched.setattr(checkouts._GitRunner, "run", counting_run)
        patched.setattr(checkouts._GitRunner, "pipeline", counting_pipeline)
        yield calls


def count_tip_equality(monkeypatch) -> list[int]:
    """Count every ``_Tip.__eq__`` call; a list search over tips makes one per element."""
    calls = [0]
    compare = checkouts._Tip.__eq__

    def counting_eq(self, other):
        calls[0] += 1
        return compare(self, other)

    monkeypatch.setattr(checkouts._Tip, "__eq__", counting_eq)
    return calls


CLONE_FACTS = (
    "bare", "partial_clone", "shallow", "cached_default", "local_default", "cached_vs_remote",
    "stash_count", "worktrees", "branches", "verdict_base", "containment", "removable_count",
)


def test_a_clone_over_the_branch_cap_is_refused_and_its_neighbours_are_unaffected(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    remote = make_remote(served, "one.git")
    other_remote = make_remote(served, "two.git")
    folder = tmp_path / "folder"
    folder.mkdir()
    exact = clone(remote, folder / "exact")
    import_branches(exact, models.CHECKOUT_BRANCH_CAP - 1)
    crowded = clone(remote, folder / "crowded")
    import_branches(crowded, models.CHECKOUT_BRANCH_CAP)
    sibling = clone(remote, folder / "sibling")
    apart = clone(other_remote, folder / "apart")
    assert len(git(exact, "for-each-ref", "refs/heads").stdout.splitlines()) == 100
    assert len(git(crowded, "for-each-ref", "refs/heads").stdout.splitlines()) == 101

    document = collect([folder])

    at_cap = find_clone(document, exact)
    assert at_cap["status"] == "ok"
    assert len(at_cap["branches"]) == 100
    assert {
        name: verdict for name, verdict in verdicts_of(at_cap).items() if name != "trunk"
    } == {name: NOT_MERGED for name in verdicts_of(at_cap) if name != "trunk"}
    over = find_clone(document, crowded)
    assert over["status"] == "too_many_branches"
    assert [over[fact] for fact in CLONE_FACTS] == [None] * len(CLONE_FACTS)
    # The refused clone stays in its remote's group beside the sibling clone.
    [group] = [
        repository
        for repository in document["repositories"]
        if real(crowded) in [item["path"] for item in repository["clones"]]
    ]
    assert {item["path"] for item in group["clones"]} == {
        real(path) for path in (exact, crowded, sibling)
    }
    assert find_clone(document, sibling)["status"] == "ok"
    alone = collect([folder / "apart"])
    assert find_clone(document, apart) == find_clone(alone, apart)


def test_clones_over_the_branch_cap_cost_the_same_git_calls_whatever_their_branch_count(
    tmp_path, monkeypatch
):
    remote = make_remote(tmp_path)
    counts: dict[int, list[str]] = {}
    for extra in (100, 10_000):
        folder = tmp_path / f"folder-{extra}"
        folder.mkdir()
        repository = clone(remote, folder / "repo")
        head = git(repository, "rev-parse", "HEAD").stdout.strip()
        add_packed_refs(
            repository, {f"refs/heads/never-pushed-{n:06d}": head for n in range(extra)}
        )
        assert len(git(repository, "for-each-ref", "refs/heads").stdout.splitlines()) == extra + 1
        with counted_git_calls(monkeypatch) as calls:
            document = collect([folder])
        assert find_clone(document, repository)["status"] == "too_many_branches"
        counts[extra] = list(calls)

    assert sorted(counts[100]) == sorted(counts[10_000])
    assert counts[100]
    assert not {"rev-list", "merge-base", "log", "diff", "patch-id", "status"} & set(counts[100])


def test_a_clone_over_the_branch_cap_is_refused_before_its_stash_is_counted(
    tmp_path, monkeypatch
):
    remote = make_remote(tmp_path)
    counts: dict[bool, list[str]] = {}
    for stashed in (False, True):
        folder = tmp_path / f"folder-{stashed}"
        folder.mkdir()
        repository = clone(remote, folder / "repo")
        if stashed:
            (repository / "file.txt").write_text("changed\n")
            git(repository, "stash", "-q")
            # Within the cap the collector counts this stash with a git call.
            within = find_clone(collect([folder]), repository)
            assert (within["status"], within["stash_count"]) == ("ok", 1)
        head = git(repository, "rev-parse", "HEAD").stdout.strip()
        add_packed_refs(
            repository,
            {f"refs/heads/extra-{n:03d}": head for n in range(models.CHECKOUT_BRANCH_CAP)},
        )
        assert len(git(repository, "for-each-ref", "refs/heads").stdout.splitlines()) == 101
        assert bool(git(repository, "for-each-ref", "refs/stash").stdout.strip()) == stashed
        with counted_git_calls(monkeypatch) as calls:
            document = collect([folder])
        assert find_clone(document, repository)["status"] == "too_many_branches"
        counts[stashed] = sorted(calls)

    # A stash costs the over-cap clone nothing: the cap is checked first.
    assert counts[True] == counts[False]
    assert "rev-list" not in counts[True]


def test_a_cap_sized_clone_is_decided_with_work_that_grows_in_step_with_its_branches(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    remote = make_remote(tmp_path)
    calls_by_size: dict[int, Counter] = {}
    for size in (100, 50, 25):
        folder = tmp_path / f"folder-{size}"
        folder.mkdir()
        repository = clone(remote, folder / "repo")
        tips = [
            rev(repository, name) for name in import_branches(repository, size - 1, f"s{size}-")
        ]
        with monkeypatch.context() as patched:
            equality = count_tip_equality(patched)
            with counted_git_calls(patched) as calls:
                document = collect([folder], seconds=600.0)
        calls_by_size[size] = Counter(calls)
        item = find_clone(document, repository)
        assert item["status"] == "ok"
        assert len(item["branches"]) == size
        if size == 100:
            assert verdicts_of(item) == {
                "trunk": None,
                **{branch["name"]: NOT_MERGED for branch in other_branches(item)},
            }
            assert len(other_branches(item)) == 99
            assert document["diagnostics"] == []
            base = item["verdict_base"]["sha"]
            assert cache_entries(cache) == {f"{tip}.{base}": "none" for tip in tips}
            # Looking a tip up must not compare tips pairwise.
            assert equality[0] <= len(tips)

    sizes = set().union(*calls_by_size.values())
    for subcommand in sizes:
        growth_small = calls_by_size[50][subcommand] - calls_by_size[25][subcommand]
        growth_large = calls_by_size[100][subcommand] - calls_by_size[50][subcommand]
        assert growth_large == 2 * growth_small, subcommand
    assert calls_by_size[100]["rev-list"] > calls_by_size[50]["rev-list"]


@pytest.mark.skipif(ON_WINDOWS, reason="the delaying git stand-in is a shell script")
def test_no_git_process_starts_after_the_verdict_deadline_and_the_cache_write_lands(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, fast = cache_world(tmp_path)
    slow = clone(tmp_path / "served" / "remote.git", folder / "slow")
    import_branches(slow, 12)
    log = tmp_path / "calls.log"
    shim = stand_in(
        tmp_path,
        "slow-sizes",
        f'case "$*" in *"{real(slow)}"*--left-right*) printf "size\\n" >> "{log}"; sleep 2;; esac',
    )
    starts: list[int] = []
    start_process = subprocess.Popen

    def recording_start(*arguments, **options):
        starts.append(time.monotonic_ns())
        return start_process(*arguments, **options)

    # A call that returns timed out without starting a process; by then every
    # other clone has finished, so only the slow clone's ladder is running.
    idle: list[int] = []
    run, pipeline = checkouts._GitRunner.run, checkouts._GitRunner.pipeline

    def watching(call):
        def watched(self, *arguments, **options):
            before = len(starts)
            outcome = call(self, *arguments, **options)
            if outcome.timed_out and len(starts) == before:
                idle.append(time.monotonic_ns())
            return outcome

        return watched

    deadline_ns = time.monotonic_ns() + 5 * 10**9
    with path_with(monkeypatch, shim), monkeypatch.context() as patched:
        patched.setattr(subprocess, "Popen", recording_start)
        patched.setattr(checkouts._GitRunner, "run", watching(run))
        patched.setattr(checkouts._GitRunner, "pipeline", watching(pipeline))
        text = checkouts.collect_checkouts([str(folder)], deadline_ns=deadline_ns)
    finished = time.monotonic_ns()

    verdict_deadline = deadline_ns - checkouts.VERDICT_RESERVE_NS
    sizes_started = len(log.read_text().splitlines()) if log.exists() else 0
    # The ladder began and was cut short by the deadline, not finished.
    assert 1 <= sizes_started < 12
    assert max(starts) <= verdict_deadline + 100_000_000
    # Once the runner has refused a call for lack of time, no later call starts one.
    assert idle
    assert [moment for moment in starts if moment > min(idle)] == []
    assert finished < deadline_ns
    document = models.parse_checkouts(text)
    reached = verdicts_of(find_clone(document, slow))
    assert {name: reached[name] for name in reached if name != "trunk"} == {
        name: unknown("deadline") for name in reached if name != "trunk"
    }
    assert verdicts_of(find_clone(document, fast)) == CACHE_WORLD_VERDICTS
    assert document["diagnostics"] == []
    assert set(cache_entries(cache).values()) == {"patch_equivalent", "none"}


# ---------------------------------------------------------------------------
# Containment grouping
# ---------------------------------------------------------------------------


def containment_of(clone_document: dict[str, Any]) -> dict[str, str | None]:
    return {branch["name"]: branch["contained_in"] for branch in clone_document["branches"]}


def switch(work: Path, name: str, start: str) -> None:
    git(work, "checkout", "-q", "-b", name, start)


def containment_world(tmp_path: Path):
    """One clone whose branches carry no naming convention, judged against ``origin/trunk``.

    ``mid`` holds ``alpha`` by a merge commit and ``zeta`` holds ``mid`` by another
    and cherry-picks ``pick``; ``copy`` shares ``zeta``'s tip.  ``host`` holds ``solo``
    and nothing else.  ``synced`` is only a merge of a base commit.  The local
    ``trunk`` is two commits ahead of ``origin/trunk``, one a cherry-pick of ``pick``.
    """
    folder, _remote, work = small_world(tmp_path)
    commit_file(work, "t1.txt", "t1\n")
    git(work, "push", "-q", "origin", "trunk")
    older = rev(work, "trunk~1")
    switch(work, "pick", "trunk")
    commit_file(work, "pick.txt", "pick\n")
    switch(work, "alpha", "trunk")
    commit_file(work, "alpha.txt", "alpha\n")
    switch(work, "mid", "trunk")
    commit_file(work, "mid.txt", "mid\n")
    git(work, "merge", "-q", "--no-ff", "-m", "take alpha", "alpha")
    switch(work, "zeta", "trunk")
    commit_file(work, "zeta.txt", "zeta\n")
    git(work, "merge", "-q", "--no-ff", "-m", "take mid", "mid")
    git(work, "cherry-pick", "pick")
    git(work, "branch", "copy", "zeta")
    switch(work, "solo", "trunk")
    commit_file(work, "solo.txt", "solo\n")
    switch(work, "host", "trunk")
    git(work, "merge", "-q", "--no-ff", "-m", "take solo", "solo")
    switch(work, "stray", "trunk")
    commit_file(work, "stray.txt", "stray\n")
    git(work, "branch", "done", "trunk")
    switch(work, "synced", older)
    git(work, "merge", "-q", "--no-ff", "-m", "sync", "trunk")
    git(work, "checkout", "-q", "trunk")
    commit_file(work, "ahead.txt", "ahead\n")
    git(work, "cherry-pick", "pick")
    return folder, work


CONTAINMENT_WORLD_FOLDS = {
    "alpha": "mid",
    "mid": "copy",
    "pick": "copy",
    "copy": None,
    "zeta": None,
    "solo": "host",
    "host": None,
    "stray": None,
    "done": None,
    "synced": None,
    "trunk": None,
}


def test_branches_fold_under_the_nearest_branch_that_holds_all_their_commits(tmp_path):
    folder, work = containment_world(tmp_path)
    before = snapshot(work)
    listing = git(work, "worktree", "list", "--porcelain").stdout

    document = collect([folder])

    clone_document = find_clone(document, work)
    assert clone_document["containment"] == "complete"
    assert clone_document["verdict_base"]["name"] == "trunk"
    assert containment_of(clone_document) == CONTAINMENT_WORLD_FOLDS
    assert document["diagnostics"] == []
    assert snapshot(work) == before
    assert git(work, "worktree", "list", "--porcelain").stdout == listing


def test_a_default_branch_ahead_of_its_base_neither_contains_nor_folds(tmp_path):
    folder, _remote, work = small_world(tmp_path)
    commit_file(work, "t1.txt", "t1\n")
    git(work, "push", "-q", "origin", "trunk")
    commit_file(work, "t2.txt", "t2\n")
    commit_file(work, "t3.txt", "t3\n")
    switch(work, "wide", "trunk")
    commit_file(work, "wide.txt", "wide\n")

    clone_document = find_clone(collect([folder]), work)

    assert clone_document["containment"] == "complete"
    assert containment_of(clone_document) == {"trunk": None, "wide": None}


def test_the_default_branch_never_folds_even_when_the_pass_runs(tmp_path):
    """``part`` shares the local default branch's tip, two commits ahead of its base,
    and ``wide`` holds both: the pass runs and folds ``part`` but never the default branch."""
    folder, _remote, work = small_world(tmp_path)
    commit_file(work, "t1.txt", "t1\n")
    git(work, "push", "-q", "origin", "trunk")
    commit_file(work, "t2.txt", "t2\n")
    commit_file(work, "t3.txt", "t3\n")
    git(work, "branch", "part", "trunk")
    switch(work, "wide", "trunk")
    commit_file(work, "wide.txt", "wide\n")
    git(work, "checkout", "-q", "trunk")

    text = checkouts.collect_checkouts(
        [str(folder)], deadline_ns=time.monotonic_ns() + int(60 * 1e9)
    )
    clone_document = find_clone(json.loads(text), work)

    assert clone_document["verdict_base"]["name"] == "trunk"
    assert clone_document["containment"] == "complete"
    assert containment_of(clone_document) == {"part": "wide", "trunk": None, "wide": None}
    assert models.parse_checkouts(text)


def import_chain(repository: Path, branch: str, count: int) -> None:
    """Add ``branch`` as a chain of ``count`` commits from the current head."""
    root = rev(repository, "HEAD")
    stream = b"".join(
        (
            f"commit refs/heads/{branch}\ncommitter T <t@example.com> 1700000000 +0000\n"
            f"data 2\nc\n{'from ' + root + chr(10) if number == 0 else ''}"
            f"M 100644 inline c{number}.txt\ndata 2\nx\n\n"
        ).encode("ascii")
        for number in range(count)
    )
    subprocess.run(
        ["git", "fast-import", "--quiet"],
        cwd=repository,
        input=stream,
        capture_output=True,
        check=True,
    )


def test_branches_over_the_own_commit_cap_neither_fold_nor_contain(tmp_path):
    folder, _remote, work = small_world(tmp_path)
    cap = checkouts.OWN_COMMIT_CAP
    import_chain(work, "large-b", cap + 2)
    git(work, "branch", "large-a", "large-b~1")
    git(work, "branch", "tiny", f"large-b~{cap + 1}")
    assert rev(work, "tiny") == rev(work, f"large-b~{cap + 1}")
    assert git(work, "merge-base", "--is-ancestor", "tiny", "large-a", check=False).returncode == 0

    clone_document = find_clone(collect([folder]), work)

    verdicts = verdicts_of(clone_document)
    assert verdicts["large-a"] == verdicts["large-b"] == unknown("history_limit")
    assert clone_document["containment"] == "complete"
    assert containment_of(clone_document) == {
        "large-a": None,
        "large-b": None,
        "tiny": None,
        "trunk": None,
    }


def over_the_cap_world(tmp_path: Path):
    """A clone with two branches over the own-commit cap and ``tiny`` within it."""
    folder, _remote, work = small_world(tmp_path)
    cap = checkouts.OWN_COMMIT_CAP
    import_chain(work, "large-b", cap + 2)
    git(work, "branch", "large-a", "large-b~1")
    git(work, "branch", "tiny", f"large-b~{cap + 1}")
    return folder, work


def stream_tips(line: str, base: str) -> set[str]:
    """The object ids a containment patch stream walks, apart from the base."""
    return {word for word in line.split() if re.fullmatch(r"[0-9a-f]{40}", word)} - {base}


@pytest.mark.skipif(ON_WINDOWS, reason="the recording git stand-in is a shell script")
def test_the_containment_patch_stream_carries_only_branches_within_the_own_commit_cap(
    tmp_path, monkeypatch, isolated_git
):
    nyx_home(isolated_git)
    folder, work = over_the_cap_world(tmp_path)
    switch(work, "small", "trunk")
    commit_file(work, "small.txt", "small\n")
    git(work, "checkout", "-q", "trunk")
    base = rev(work, "origin/trunk")
    tips = {rev(work, name) for name in ("tiny", "small")}
    assert not tips & {rev(work, "large-a"), rev(work, "large-b")}
    shim, log = recording(tmp_path, "mixed")

    with path_with(monkeypatch, shim):
        document = collect([folder])

    item = find_clone(document, work)
    streams = containment_streams(log_lines(log, work), base)
    assert item["containment"] == "complete"
    assert len(streams) == 1
    assert stream_tips(streams[0], base) == tips


@pytest.mark.skipif(ON_WINDOWS, reason="the recording git stand-in is a shell script")
def test_a_single_branch_within_the_own_commit_cap_runs_no_patch_stream(
    tmp_path, monkeypatch, isolated_git
):
    nyx_home(isolated_git)
    folder, work = over_the_cap_world(tmp_path)
    base = rev(work, "origin/trunk")
    shim, log = recording(tmp_path, "single")

    with path_with(monkeypatch, shim):
        document = collect([folder])

    item = find_clone(document, work)
    lines = log_lines(log, work)
    assert item["containment"] == "complete"
    assert set(containment_of(item).values()) == {None}
    assert containment_streams(lines, base) == []


@pytest.mark.parametrize("trap", ["graft", "replace"])
def test_a_graft_or_replace_ref_cannot_make_a_commit_look_held(tmp_path, trap):
    folder, work = containment_world(tmp_path)
    tip, stray = rev(work, "zeta"), rev(work, "stray")
    parent = rev(work, "zeta~1")
    if trap == "graft":
        (work / ".git" / "info").mkdir(exist_ok=True)
        (work / ".git" / "info" / "grafts").write_text(f"{tip} {parent} {stray}\n")
    else:
        git(work, "replace", "--graft", tip, parent, stray)
    assert stray in git(work, "rev-list", "zeta").stdout.split()

    clone_document = find_clone(collect([folder]), work)

    assert clone_document["containment"] == "complete"
    assert containment_of(clone_document)["stray"] is None
    assert containment_of(clone_document) == CONTAINMENT_WORLD_FOLDS


def test_a_base_lent_by_a_sibling_clone_still_folds_nested_branches(tmp_path):
    folder, _remote, lagging, (holder,), _head = borrowed_world(tmp_path)
    switch(lagging, "inner", "trunk")
    commit_file(lagging, "inner.txt", "inner\n")
    switch(lagging, "outer", "inner")
    commit_file(lagging, "outer.txt", "outer\n")

    document = collect([folder], remote_checks=True)

    clone_document = find_clone(document, lagging)
    assert clone_document["verdict_base"]["objects_from"] == real(holder)
    assert clone_document["containment"] == "complete"
    assert containment_of(clone_document) == {"inner": "outer", "outer": None, "trunk": None}


# -- the pass inside the collection -----------------------------------------


def nested_world(tmp_path: Path, tag: str = "", name: str = "work"):
    """A clone with ``inner`` held by ``outer`` and an unrelated ``lone``."""
    served = tmp_path / "served"
    served.mkdir(exist_ok=True)
    folder = tmp_path / "folder"
    folder.mkdir(exist_ok=True)
    remote = served / "remote.git"
    if not remote.exists():
        make_remote(served)
    work = clone(remote, folder / name)
    switch(work, "inner", "trunk")
    commit_file(work, f"inner{tag}.txt", f"inner{tag}\n")
    switch(work, "outer", "inner")
    commit_file(work, f"outer{tag}.txt", f"outer{tag}\n")
    switch(work, "lone", "trunk")
    commit_file(work, f"lone{tag}.txt", f"lone{tag}\n")
    git(work, "checkout", "-q", "trunk")
    return folder, work


NESTED_FOLDS = {"inner": "outer", "outer": None, "lone": None, "trunk": None}


def log_lines(log: Path, repository: Path | None = None) -> list[str]:
    lines = log.read_text().splitlines() if log.exists() else []
    if repository is not None:
        lines = [line for line in lines if f" -C {real(repository)} " in line]
    return lines


def containment_lists(lines: list[str]) -> list[str]:
    return [line for line in lines if " rev-list " in line and "--max-count=" in line]


def containment_streams(lines: list[str], base: str) -> list[str]:
    """The streamed patch passes over branch tips: the revisions before ``--not <base>``."""
    found = []
    for line in lines:
        words = line.split()
        if " log " not in line or "-p" not in words or words[-3:-1] != ["--not", base]:
            continue
        if words[-4] != base:
            found.append(line)
    return found


@pytest.mark.skipif(ON_WINDOWS, reason="the recording git stand-in is a shell script")
def test_the_fold_is_computed_in_every_collection_and_follows_the_recorded_tips(
    tmp_path, monkeypatch, isolated_git
):
    nyx_home(isolated_git)
    folder, work = nested_world(tmp_path)
    base = rev(work, "origin/trunk")
    logs = []
    documents = []
    for name in ("first", "second"):
        shim, log = recording(tmp_path, name)
        with path_with(monkeypatch, shim):
            documents.append(collect([folder]))
        logs.append(log)
    first, second = documents

    assert containment_of(find_clone(first, work)) == NESTED_FOLDS
    assert second["checkouts_digest"] == first["checkouts_digest"]
    for log in logs:
        lines = log_lines(log)
        assert len(containment_lists(lines)) == 3
        assert len(containment_streams(lines, base)) == 1

    # Moving one tip between runs moves the fold: nothing is served from before.
    git(work, "checkout", "-q", "inner")
    commit_file(work, "inner-more.txt", "more\n")
    git(work, "checkout", "-q", "trunk")
    moved = collect([folder])
    assert moved["checkouts_digest"] != first["checkouts_digest"]
    assert containment_of(find_clone(moved, work)) == {
        "inner": None, "outer": None, "lone": None, "trunk": None
    }
    assert moved["diagnostics"] == []


@pytest.mark.skipif(ON_WINDOWS, reason="the moving stand-in is a shell script")
def test_a_ref_moved_after_the_branches_were_listed_leaves_the_fold_on_the_recorded_tip(
    tmp_path, monkeypatch
):
    folder, work = nested_world(tmp_path)
    tip = rev(work, "outer")
    top = rev(work, "origin/trunk")
    real_git = shutil.which("git")
    done = tmp_path / "moved"
    shim = git_stand_in(
        tmp_path,
        f'case "$*" in *"for-each-ref --merged"*) [ -e "{done}" ] || {{ : > "{done}"; '
        f'"{real_git}" -C "{work}" update-ref refs/heads/outer {top}; }};; esac',
    )
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")

    clone_document = find_clone(collect([folder]), work)

    assert done.exists()
    assert rev(work, "outer") == top
    assert branch_of(clone_document, "outer")["sha"] == tip
    assert containment_of(clone_document) == NESTED_FOLDS


FAILING_CONTAINMENT = {
    "list": ('*"--max-count"*', "exit 1", "failed"),
    "stream": ('*" log "*" --not {base} --"*', "exit 1", "failed"),
    "late-stream": ('*" log "*" --not {base} --"*', "exec sleep 30", "deadline"),
}


@pytest.mark.skipif(ON_WINDOWS, reason="the failing git stand-in is a shell script")
@pytest.mark.parametrize("step", list(FAILING_CONTAINMENT))
def test_a_containment_call_that_fails_or_runs_late_folds_nothing_and_changes_no_verdict(
    tmp_path, monkeypatch, isolated_git, step
):
    cache = nyx_home(isolated_git)
    folder, work = nested_world(tmp_path)
    base = rev(work, "origin/trunk")
    control = collect([folder], seconds=3.0)
    assert containment_of(find_clone(control, work)) == NESTED_FOLDS
    pattern, action, outcome = FAILING_CONTAINMENT[step]
    shim = stand_in(
        tmp_path, "breaking", f"case \"$*\" in {pattern.format(base=base)}) {action};; esac"
    )

    with path_with(monkeypatch, shim):
        document = collect([folder], seconds=3.0)

    clone_document = find_clone(document, work)
    assert clone_document["containment"] == outcome
    assert set(containment_of(clone_document).values()) == {None}
    assert verdicts_of(clone_document) == verdicts_of(find_clone(control, work))
    assert document["diagnostics"] == []
    assert cache.exists()


@pytest.mark.skipif(ON_WINDOWS, reason="the delaying git stand-in is a shell script")
def test_a_late_containment_pass_reports_deadline_for_its_clone_alone_and_keeps_the_cache_write(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, one = nested_world(tmp_path, "-one", "one")
    _folder, two = nested_world(tmp_path, "-two", "two")
    first = collect([folder], seconds=3.0)
    assert containment_of(find_clone(first, one)) == NESTED_FOLDS
    assert containment_of(find_clone(first, two)) == NESTED_FOLDS
    stamps = cache_times(cache)
    assert stamps
    time.sleep(1.1)
    marker = tmp_path / "delayed"
    slow = stand_in(
        tmp_path,
        "late",
        f'case "$*" in *" -C {real(two)} "*"--max-count"*) '
        f'[ -e "{marker}" ] || {{ : > "{marker}"; exec sleep 30; }};; esac',
    )

    with path_with(monkeypatch, slow):
        second = collect([folder], seconds=3.0)

    assert marker.exists()
    late = find_clone(second, two)
    assert late["containment"] == "deadline"
    assert set(containment_of(late).values()) == {None}
    assert verdicts_of(late) == verdicts_of(find_clone(first, two))
    on_time = find_clone(second, one)
    assert on_time["containment"] == "complete"
    assert containment_of(on_time) == NESTED_FOLDS
    assert second["diagnostics"] == []
    saved = json.loads(cache.read_text())
    assert set(saved) == {"schema_version", "verdicts", "remote_checks"}
    assert saved["schema_version"] == 3
    assert all(cache_times(cache)[key] > stamps[key] for key in stamps)

    # The control: a pass on the full deadline leaves the write no time.
    time.sleep(1.1)
    monkeypatch.setattr(checkouts, "VERDICT_RESERVE_NS", 0)
    marker.unlink()
    with path_with(monkeypatch, slow):
        unreserved = collect([folder], seconds=3.0)
    assert unreserved["diagnostics"] == [
        {"code": "checkout_cache_write_failed", "path": str(cache)}
    ]


def limited_world(tmp_path: Path):
    """Clones that cannot prove patch equivalence, each with a held branch."""
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    allow_filters(remote)
    commit_file(clone(remote, tmp_path / "seed"), "more.txt", "more\n")
    git(tmp_path / "seed", "push", "-q", "origin", "trunk")
    url = f"file://{remote}"
    clones = {
        "plain": clone(remote, folder / "plain"),
        "shallow": clone(url, folder / "shallow", "--depth=1"),
        "partial": clone(url, folder / "partial", "--filter=blob:none"),
        "shallow-bare": clone(url, folder / "shallow-bare", "--depth=1"),
    }
    git(clones["shallow-bare"], "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    git(clones["shallow-bare"], "update-ref", "-d", "refs/remotes/origin/trunk")
    standalone = folder / "standalone"
    git(tmp_path, "init", "-q", "-b", "trunk", str(standalone))
    commit_file(standalone, "s.txt", "s\n")
    clones["standalone"] = standalone
    for repository in clones.values():
        switch(repository, "inner", "trunk")
        commit_file(repository, "inner.txt", "inner\n")
        switch(repository, "outer", "inner")
        commit_file(repository, "outer.txt", "outer\n")
        git(repository, "checkout", "-q", "trunk")
    return folder, clones


@pytest.mark.skipif(ON_WINDOWS, reason="the recording and version stand-ins are shell scripts")
def test_each_reason_to_skip_grouping_wins_in_its_order_and_runs_no_patch_stream(
    tmp_path, monkeypatch
):
    folder, clones = limited_world(tmp_path)
    base = rev(clones["plain"], "origin/trunk")
    shim, log = recording(tmp_path, "limits")
    with path_with(monkeypatch, shim):
        document = collect([folder])

    expected = {
        "plain": "complete",
        "shallow": "incomplete",
        "partial": "incomplete",
        "shallow-bare": "no_base",
        "standalone": "no_base",
    }
    for name, outcome in expected.items():
        item = find_clone(document, clones[name])
        assert item["containment"] == outcome, name
        folds = containment_of(item)
        assert set(folds.values()) == ({"outer"} | {None} if outcome == "complete" else {None})
        streams = containment_streams(log_lines(log, clones[name]), base)
        assert len(streams) == (1 if outcome == "complete" else 0), name
    assert containment_of(find_clone(document, clones["plain"]))["inner"] == "outer"

    old_log = tmp_path / "old.log"
    recorder = stand_in(
        tmp_path,
        "old-recorder",
        f'case "$*" in *" --version") echo "git version 2.38.0"; exit 0;; esac\n'
        f'printf "%s\\n" "$*" >> "{old_log}"',
    )
    with path_with(monkeypatch, recorder):
        older = collect([folder])

    assert older["git"]["version"] == "2.38.0"
    expected_old = {**expected, "plain": "git_too_old"}
    for name, outcome in expected_old.items():
        item = find_clone(older, clones[name])
        assert item["containment"] == outcome, name
        assert set(containment_of(item).values()) == {None}
        assert containment_streams(log_lines(old_log, clones[name]), base) == []
        assert containment_lists(log_lines(old_log, clones[name])) == []


@pytest.mark.skipif(ON_WINDOWS, reason="the recording git stand-in is a shell script")
def test_a_clone_at_the_branch_cap_costs_one_commit_list_per_branch_and_one_patch_stream(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "repo")
    import_branches(repository, models.CHECKOUT_BRANCH_CAP - 1)
    base = rev(repository, "origin/trunk")
    shim, log = recording(tmp_path, "cap")

    with path_with(monkeypatch, shim):
        document = collect([folder], seconds=600.0)

    item = find_clone(document, repository)
    lines = log_lines(log, repository)
    assert item["containment"] == "complete"
    assert len(item["branches"]) == models.CHECKOUT_BRANCH_CAP
    assert set(containment_of(item).values()) == {None}
    assert len(containment_lists(lines)) <= models.CHECKOUT_BRANCH_CAP - 1
    assert len(containment_streams(lines, base)) == 1
    assert document["diagnostics"] == []
    assert len(cache_entries(cache)) == models.CHECKOUT_BRANCH_CAP - 1


# -- the parser ------------------------------------------------------------------


def folded(document: dict[str, Any]) -> dict[str, Any]:
    """Fold ``never-pushed`` under ``outside`` in the first clone of a valid sample."""
    clone_document = first_clone(document)
    assert clone_document["containment"] == "complete"
    branch_of(clone_document, "never-pushed")["contained_in"] = "outside"
    return clone_document


def test_parser_accepts_folded_branches_and_every_outcome_where_it_applies(sample):
    document, _folder = sample
    accepted = doc_copy(document)
    folded(accepted)
    assert models.parse_checkouts(redigest(rederive(accepted)))
    for outcome in ("deadline", "failed"):
        accepted = doc_copy(document)
        first_clone(accepted)["containment"] = outcome
        assert models.parse_checkouts(redigest(rederive(accepted)))
    shallow = doc_copy(document)
    first_clone(shallow).update(shallow=True, containment="incomplete")
    other_branches(first_clone(shallow))[0]["verdict"] = verdict_of(
        "unknown", reason="incomplete_clone"
    )
    assert models.parse_checkouts(redigest(rederive(shallow)))
    shallow_old = doc_copy(shallow)
    set_git_version(shallow_old, "2.38.1", keep=first_clone(shallow_old))
    assert models.parse_checkouts(redigest(rederive(shallow_old)))
    old = doc_copy(document)
    set_git_version(old, "2.38.1")
    assert models.parse_checkouts(redigest(rederive(old)))


def set_git_version(document: dict[str, Any], version: str, keep: Any = None) -> None:
    """Change the git version and settle the containment outcome of every other clone."""
    document["git"]["version"] = version
    floor = tuple(int(part) for part in version.split(".")[:2]) < (2, 39)
    for item in all_clones(document):
        if item is not keep and item["verdict_base"] is not None and not item["shallow"]:
            item["containment"] = "git_too_old" if floor else "complete"


def only_contained_in(name: str, target: Any):
    def mutate(document):
        branch_of(folded(document), name)["contained_in"] = target

    return mutate


def mutate_cycle(document):
    clone_document = folded(document)
    branch_of(clone_document, "outside")["contained_in"] = "never-pushed"


def mutate_base_branch_contained(document):
    clone_document = folded(document)
    branch_of(clone_document, "trunk")["contained_in"] = "outside"


def with_outcome(outcome: Any, *, shallow: bool | None = None, version: str | None = None,
                 no_base: bool = False, fold: bool = False):
    def mutate(document):
        if no_base:
            undecided_without_a_base(document)
        clone_document = first_clone(document)
        if shallow is not None:
            clone_document["shallow"] = shallow
            if shallow:
                for branch in other_branches(clone_document) if not no_base else []:
                    branch["verdict"] = verdict_of("unknown", reason="incomplete_clone")
        if version is not None:
            set_git_version(document, version, keep=clone_document)
            if version.startswith("2.38") and not no_base:
                for branch in other_branches(clone_document):
                    branch["verdict"] = verdict_of("unknown", reason="git_too_old")
        clone_document["containment"] = outcome
        if fold:
            branch_of(clone_document, "never-pushed")["contained_in"] = "outside"

    return mutate


def mutate_containment_missing(document):
    del first_clone(document)["containment"]


def mutate_containment_on_a_refused_clone(document):
    refused_clone(document)["containment"] = "complete"


REJECTED_CONTAINMENT_DOCUMENTS = {
    "contained_in_naming_the_base_branch": only_contained_in("never-pushed", "trunk"),
    "contained_in_naming_itself": only_contained_in("never-pushed", "never-pushed"),
    "contained_in_naming_an_unlisted_branch": only_contained_in("never-pushed", "absent"),
    "contained_in_not_text": only_contained_in("never-pushed", 7),
    "contained_in_cycle": mutate_cycle,
    "contained_in_on_the_base_branch": mutate_base_branch_contained,
    "contained_in_under_deadline": with_outcome("deadline", fold=True),
    "contained_in_under_failed": with_outcome("failed", fold=True),
    "contained_in_under_no_base": with_outcome("no_base", no_base=True, fold=True),
    "contained_in_under_incomplete": with_outcome("incomplete", shallow=True, fold=True),
    "contained_in_under_git_too_old": with_outcome("git_too_old", version="2.38.1", fold=True),
    "containment_undefined": with_outcome(None),
    "containment_key_missing": mutate_containment_missing,
    "containment_foreign_value": with_outcome("partial"),
    "containment_on_a_refused_clone": mutate_containment_on_a_refused_clone,
    "no_base_with_a_base": with_outcome("no_base"),
    "complete_without_a_base": with_outcome("complete", no_base=True),
    "incomplete_on_a_full_clone": with_outcome("incomplete"),
    "complete_on_a_shallow_clone": with_outcome("complete", shallow=True),
    "git_too_old_under_a_new_git": with_outcome("git_too_old", version="2.43.0"),
    "complete_under_an_old_git": with_outcome("complete", version="2.38.1"),
    "incomplete_on_a_shallow_clone_without_a_base": with_outcome(
        "incomplete", shallow=True, no_base=True
    ),
    "git_too_old_on_a_shallow_clone": with_outcome("git_too_old", shallow=True, version="2.38.1"),
}


@pytest.mark.parametrize("mutation", list(REJECTED_CONTAINMENT_DOCUMENTS), ids=str)
def test_parser_rejects_a_containment_invariant_violation_under_a_valid_digest(sample, mutation):
    document, _folder = sample
    assert models.parse_checkouts(redigest(doc_copy(document)))
    broken = doc_copy(document)
    REJECTED_CONTAINMENT_DOCUMENTS[mutation](broken)
    assert "removable" not in refusal_of(rederive(broken))


def test_parser_refuses_a_schema_2_document_and_reads_only_schema_4(sample):
    document, _folder = sample
    assert document["schema_version"] == 4 == models.CHECKOUTS_SCHEMA_VERSION
    previous = doc_copy(document)
    previous["schema_version"] = 2
    for clone_item in all_clones(previous):
        del clone_item["containment"]
        for branch in clone_item["branches"] or []:
            del branch["contained_in"]
    with pytest.raises(models.ProtocolError, match="schema"):
        models.parse_checkouts(redigest(previous))


# ---------------------------------------------------------------------------
# Removable recomputation by the parser
# ---------------------------------------------------------------------------


def settled_sample(document: dict[str, Any]) -> dict[str, Any]:
    """Make ``never-pushed`` and ``outside`` merged, and ``outside``'s worktree clean.

    Returns the first clone. Its Removable fields are derived from the settled
    facts, so a branch, a linked worktree and the counts are all Removable.
    """
    clone_document = first_clone(document)
    for name in ("never-pushed", "outside"):
        branch_of(clone_document, name)["verdict"] = merged_verdict()
    linked = linked_worktree(clone_document)
    linked.update(locked=False, prunable=False, status="ok")
    linked["changes"] = {"staged": 0, "unstaged": 0, "untracked": 0, "conflicted": 0}
    linked["operations"] = []
    linked["index_lock"] = False
    rederive(document)
    assert branch_of(clone_document, "never-pushed")["removable"] == {
        "removable": True, "blockers": [],
    }
    assert clone_document["removable_count"] == {"branches": 2, "worktrees": 1}
    return clone_document


def linked_worktree(clone_document: dict[str, Any]) -> dict[str, Any]:
    return next(item for item in clone_document["worktrees"] if not item["main"])


def gone_worktree(clone_document: dict[str, Any]) -> None:
    linked_worktree(clone_document).update(
        status="failed", prunable=True, head=None, changes=None, operations=None,
        index_lock=None,
    )


def set_change(field: str, value: Any):
    def mutate(clone_document):
        linked = linked_worktree(clone_document)
        if field in ("untracked", "conflicted"):
            linked["changes"][field] = value
        else:
            linked[field] = value

    return mutate


def mutate_verdict_state(clone_document):
    branch_of(clone_document, "never-pushed")["verdict"] = verdict_of("not_merged", "none")


def mutate_linked_verdict_state(clone_document):
    branch_of(clone_document, "outside")["verdict"] = verdict_of("unknown", reason="failed")


def mutate_unreadable_worktree(clone_document):
    gone_worktree(clone_document)
    linked_worktree(clone_document)["prunable"] = False


def mutate_prunable_gone_to_present(clone_document):
    gone_worktree(clone_document)
    rederive({"repositories": [{"clones": [clone_document]}]})
    assert linked_worktree(clone_document)["removable"] == {"removable": True, "blockers": []}
    linked_worktree(clone_document)["prunable"] = False


def mutate_held_branch_moves_to_main(clone_document):
    main = main_worktree(clone_document)["path"]
    branch_of(clone_document, "never-pushed")["checked_out_in"] = [main]


FACT_ONLY_REMOVABLE_MUTATIONS = {
    "branch_verdict_becomes_not_merged": mutate_verdict_state,
    "held_branch_verdict_becomes_unknown": mutate_linked_verdict_state,
    "worktree_gains_an_untracked_file": set_change("untracked", 1),
    "worktree_gains_a_conflict": set_change("conflicted", 1),
    "worktree_starts_an_operation": set_change("operations", ["merge"]),
    "worktree_gains_an_index_lock": set_change("index_lock", True),
    "worktree_is_locked": set_change("locked", True),
    "worktree_status_becomes_failed": mutate_unreadable_worktree,
    "prunable_worktree_directory_returns": mutate_prunable_gone_to_present,
    "branch_moves_to_the_main_worktree": mutate_held_branch_moves_to_main,
}


@pytest.mark.parametrize("mutation", list(FACT_ONLY_REMOVABLE_MUTATIONS), ids=str)
def test_parser_refuses_a_removable_kept_after_one_of_its_facts_changes(sample, mutation):
    document, _folder = sample
    settled_sample(document)
    assert models.parse_checkouts(redigest(doc_copy(document)))
    broken = doc_copy(document)
    FACT_ONLY_REMOVABLE_MUTATIONS[mutation](first_clone(broken))
    # Nothing but the Removable fields is stale, and every structural check passes:
    # only the recomputation can refuse the document.
    assert "contradicts the facts of the clone" in refusal_of(broken)
    models.parse_checkouts(redigest(rederive(broken)))


def blocked_branch(clone_document: dict[str, Any]) -> dict[str, Any]:
    """Give ``never-pushed`` two blockers, in the order the derivation lists them."""
    branch = branch_of(clone_document, "never-pushed")
    branch["verdict"] = verdict_of("not_merged", "none")
    branch["checked_out_in"] = [main_worktree(clone_document)["path"]]
    return branch


def mutate_true_with_a_blocker(clone_document):
    branch_of(clone_document, "never-pushed")["removable"] = {
        "removable": True, "blockers": ["not_merged"],
    }


def mutate_false_with_no_blocker(clone_document):
    branch_of(clone_document, "never-pushed")["removable"] = {
        "removable": False, "blockers": [],
    }


def mutate_worktree_true_with_a_blocker(clone_document):
    linked_worktree(clone_document)["removable"] = {
        "removable": True, "blockers": ["locked"],
    }


def mutate_worktree_false_with_no_blocker(clone_document):
    linked_worktree(clone_document)["removable"] = {"removable": False, "blockers": []}


def mutate_null_on_a_branch(clone_document):
    branch_of(clone_document, "never-pushed")["removable"] = None


def mutate_null_on_a_linked_worktree(clone_document):
    linked_worktree(clone_document)["removable"] = None


def mutate_set_on_the_main_worktree(clone_document):
    main_worktree(clone_document)["removable"] = {"removable": True, "blockers": []}


def mutate_set_on_the_base_branch(clone_document):
    base = clone_document["verdict_base"]["name"]
    branch_of(clone_document, base)["removable"] = {"removable": True, "blockers": []}


def mutate_blockers_reordered(clone_document):
    blocked_branch(clone_document)
    rederive({"repositories": [{"clones": [clone_document]}]})
    blockers = branch_of(clone_document, "never-pushed")["removable"]["blockers"]
    assert blockers == ["not_merged", "checked_out_in_main_worktree"]
    blockers.reverse()


def mutate_blockers_duplicated(clone_document):
    blocked_branch(clone_document)
    rederive({"repositories": [{"clones": [clone_document]}]})
    branch_of(clone_document, "never-pushed")["removable"]["blockers"].append("not_merged")


def mutate_blockers_unknown(clone_document):
    blocked_branch(clone_document)
    rederive({"repositories": [{"clones": [clone_document]}]})
    branch_of(clone_document, "never-pushed")["removable"]["blockers"].append("bogus")


def mutate_blockers_missing_one(clone_document):
    blocked_branch(clone_document)
    rederive({"repositories": [{"clones": [clone_document]}]})
    branch_of(clone_document, "never-pushed")["removable"]["blockers"].pop()


def mutate_count_branches_high(clone_document):
    clone_document["removable_count"]["branches"] += 1


def mutate_count_branches_low(clone_document):
    clone_document["removable_count"]["branches"] -= 1


def mutate_count_worktrees_high(clone_document):
    clone_document["removable_count"]["worktrees"] += 1


def mutate_count_worktrees_low(clone_document):
    clone_document["removable_count"]["worktrees"] -= 1


def mutate_count_missing(clone_document):
    clone_document["removable_count"] = None


def mutate_count_key_missing(clone_document):
    del clone_document["removable_count"]["worktrees"]


def mutate_count_on_a_refused_clone(clone_document):
    refused_clone({"repositories": [{"clones": [clone_document]}]})
    clone_document["removable_count"] = {"branches": 0, "worktrees": 0}


def mutate_count_negative(clone_document):
    clone_document["removable_count"]["branches"] = -1


def mutate_removable_flag_not_boolean(clone_document):
    branch_of(clone_document, "never-pushed")["removable"]["removable"] = 1


def mutate_removable_extra_key(clone_document):
    branch_of(clone_document, "never-pushed")["removable"]["extra"] = 1


def mutate_blockers_not_a_list(clone_document):
    linked_worktree(clone_document)["removable"]["blockers"] = "locked"


REJECTED_REMOVABLE_DOCUMENTS = {
    "true_with_a_blocker": mutate_true_with_a_blocker,
    "false_with_no_blocker": mutate_false_with_no_blocker,
    "worktree_true_with_a_blocker": mutate_worktree_true_with_a_blocker,
    "worktree_false_with_no_blocker": mutate_worktree_false_with_no_blocker,
    "null_on_a_non_default_branch": mutate_null_on_a_branch,
    "null_on_a_linked_worktree": mutate_null_on_a_linked_worktree,
    "set_on_the_main_worktree": mutate_set_on_the_main_worktree,
    "set_on_the_base_branch": mutate_set_on_the_base_branch,
    "blockers_reordered": mutate_blockers_reordered,
    "blockers_duplicated": mutate_blockers_duplicated,
    "blockers_unknown": mutate_blockers_unknown,
    "blockers_missing_one": mutate_blockers_missing_one,
    "branch_count_one_high": mutate_count_branches_high,
    "branch_count_one_low": mutate_count_branches_low,
    "worktree_count_one_high": mutate_count_worktrees_high,
    "worktree_count_one_low": mutate_count_worktrees_low,
    "count_missing_on_an_ok_clone": mutate_count_missing,
    "count_key_missing": mutate_count_key_missing,
    "count_negative": mutate_count_negative,
    "removable_flag_not_boolean": mutate_removable_flag_not_boolean,
    "removable_extra_key": mutate_removable_extra_key,
    "blockers_not_a_list": mutate_blockers_not_a_list,
}


@pytest.mark.parametrize("mutation", list(REJECTED_REMOVABLE_DOCUMENTS), ids=str)
def test_parser_rejects_a_removable_invariant_violation_under_a_valid_digest(sample, mutation):
    document, _folder = sample
    settled_sample(document)
    assert models.parse_checkouts(redigest(doc_copy(document)))
    broken = doc_copy(document)
    REJECTED_REMOVABLE_DOCUMENTS[mutation](first_clone(broken))
    assert "removable" in refusal_of(broken)


def test_parser_refuses_a_count_on_a_clone_that_is_not_ok(sample):
    document, _folder = sample
    settled_sample(document)
    broken = doc_copy(document)
    mutate_count_on_a_refused_clone(first_clone(broken))
    assert "must leave its facts null" in refusal_of(broken)


def test_parser_accepts_every_removable_shape_the_derivation_returns(sample):
    document, _folder = sample
    settled_sample(document)
    seen: set[tuple[bool, tuple[str, ...]]] = set()
    for name, mutate in [
        ("settled", lambda c: None),
        ("branch", mutate_verdict_state),
        ("held", mutate_linked_verdict_state),
        ("untracked", set_change("untracked", 1)),
        ("locked", set_change("locked", True)),
        ("main", mutate_held_branch_moves_to_main),
        ("two", blocked_branch),
        ("gone", gone_worktree),
    ]:
        accepted = doc_copy(document)
        clone_document = first_clone(accepted)
        mutate(clone_document)
        rederive(accepted)
        assert models.parse_checkouts(redigest(accepted)), name
        for item in (*clone_document["branches"], *clone_document["worktrees"]):
            if item["removable"] is not None:
                seen.add((item["removable"]["removable"], tuple(item["removable"]["blockers"])))
    assert (True, ()) in seen
    assert (False, ("not_merged",)) in seen
    assert any(len(blockers) > 1 for _, blockers in seen)


# ---------------------------------------------------------------------------
# Removable verdicts
# ---------------------------------------------------------------------------

BLOCKER_ONLY_NAMES = (
    "verdict_unknown",
    "checked_out_in_main_worktree",
    "worktree_not_clean",
    "worktree_locked",
    "not_clean",
)
NO_CHANGES = {"staged": 0, "unstaged": 0, "untracked": 0, "conflicted": 0}


def blockers_of(verdict: dict[str, Any] | None) -> list[str] | None:
    if verdict is None:
        return None
    assert verdict["removable"] is (verdict["blockers"] == [])
    return list(verdict["blockers"])


def removable_view(clone_document: dict[str, Any]) -> dict[str, Any]:
    """Every blocker list of a clone, keyed by branch name and worktree path."""
    count = clone_document["removable_count"]
    branches = {item["name"]: blockers_of(item["removable"]) for item in clone_document["branches"]}
    worktrees = {item["path"]: blockers_of(item["removable"]) for item in clone_document["worktrees"]}
    removable_branches = sum(1 for blockers in branches.values() if blockers == [])
    removable_worktrees = sum(1 for blockers in worktrees.values() if blockers == [])
    assert count == {"branches": removable_branches, "worktrees": removable_worktrees}
    return {"branches": branches, "worktrees": worktrees, "count": count}


def expected_view(
    work: Path,
    branches: dict[str, list[str] | None],
    worktrees: dict[Path, list[str]] | None = None,
    *,
    count: tuple[int, int],
    main: bool = True,
) -> dict[str, Any]:
    paths = {real(path): blockers for path, blockers in (worktrees or {}).items()}
    if main:
        paths[real(work)] = None
    return {
        "branches": branches,
        "worktrees": paths,
        "count": {"branches": count[0], "worktrees": count[1]},
    }


def land(work: Path, name: str, *, merged: bool = True) -> None:
    """Make branch ``name`` of two commits and, by default, squash-merge it into ``trunk``."""
    git(work, "checkout", "-q", "-b", name, "trunk")
    commit_file(work, f"{name}-1.txt", f"{name} one\n")
    commit_file(work, f"{name}-2.txt", f"{name} two\n")
    git(work, "checkout", "-q", "trunk")
    if merged:
        git(work, "merge", "--squash", name)
        git(work, "commit", "-q", "-m", f"land {name}")
        git(work, "push", "-q", "origin", "trunk")


def linked(work: Path, tmp_path: Path, name: str, *arguments: str) -> Path:
    """Add a linked worktree; ``arguments`` are any options and then the commit-ish it holds."""
    destination = tmp_path / f"wt-{name}"
    *options, commit_ish = arguments
    git(work, "worktree", "add", "-q", *options, str(destination), commit_ish)
    return destination


def removable_of(folder: Path, work: Path) -> dict[str, Any]:
    return find_clone(collect([folder]), work)


def test_a_squash_merged_branch_that_is_not_checked_out_is_removable(tmp_path):
    folder, _remote, work = small_world(tmp_path)
    land(work, "alder")
    land(work, "birch", merged=False)
    before = snapshot(work)
    listing = git(work, "worktree", "list", "--porcelain").stdout

    document = collect([folder])

    clone_document = find_clone(document, work)
    assert verdicts_of(clone_document)["alder"] == MERGED_SQUASH
    assert removable_view(clone_document) == expected_view(
        work, {"alder": [], "birch": ["not_merged"], "trunk": None}, count=(1, 0)
    )
    assert document["diagnostics"] == []
    assert snapshot(work) == before
    assert git(work, "worktree", "list", "--porcelain").stdout == listing


def test_untracked_files_block_a_worktree_and_its_branch_but_ignored_files_do_not(tmp_path):
    folder, _remote, work = small_world(tmp_path)
    for name in ("alder", "birch", "cedar"):
        land(work, name)
    (work / ".git" / "info" / "exclude").write_text("build/\n")
    dirty = linked(work, tmp_path, "dirty", "alder")
    ignoring = linked(work, tmp_path, "ignoring", "birch")
    clean = linked(work, tmp_path, "clean", "cedar")
    (dirty / "scratch.txt").write_text("x\n")
    (ignoring / "build").mkdir()
    (ignoring / "build" / "out.bin").write_text("x\n")

    clone_document = removable_of(folder, work)

    assert find_worktree(clone_document, dirty)["changes"] == {**NO_CHANGES, "untracked": 1}
    assert find_worktree(clone_document, ignoring)["changes"] == NO_CHANGES
    assert removable_view(clone_document) == expected_view(
        work,
        {"alder": ["worktree_not_clean"], "birch": [], "cedar": [], "trunk": None},
        {dirty: ["not_clean"], ignoring: [], clean: []},
        count=(2, 2),
    )


def stage(worktree: Path) -> None:
    (worktree / "file.txt").write_text("staged\n")
    git(worktree, "add", "file.txt")


def modify(worktree: Path) -> None:
    (worktree / "file.txt").write_text("unstaged\n")


def start_bisect(worktree: Path) -> None:
    git(worktree, "bisect", "start")


def stop_at_a_break(worktree: Path) -> None:
    editor = worktree.parent / "break-editor.sh"
    editor.write_text('#!/bin/sh\nprintf "break\\n" | cat - "$1" > "$1.new" && mv "$1.new" "$1"\n')
    editor.chmod(0o755)
    environment = {**os.environ, "GIT_SEQUENCE_EDITOR": str(editor)}
    result = subprocess.run(
        ["git", "rebase", "-q", "-i", "trunk"], cwd=worktree, env=environment,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def conflict_on_pop(worktree: Path) -> None:
    (worktree / "file.txt").write_text("mine\n")
    git(worktree, "stash", "-q")
    git(worktree, "reset", "-q", "--hard", "trunk")
    git(worktree, "stash", "pop", check=False)


def lock_the_index(worktree: Path) -> None:
    git_dir = Path(git(worktree, "rev-parse", "--absolute-git-dir").stdout.strip())
    (git_dir / "index.lock").write_text("")


NOT_CLEAN_SOURCES = {
    "staged": (stage, {**NO_CHANGES, "staged": 1}, [], False),
    "unstaged": (modify, {**NO_CHANGES, "unstaged": 1}, [], False),
    "bisect": (start_bisect, NO_CHANGES, ["bisect"], False),
    "conflicts": (conflict_on_pop, {**NO_CHANGES, "conflicted": 1}, [], False),
    "index_lock": (lock_the_index, NO_CHANGES, [], True),
}


@pytest.mark.parametrize("source", list(NOT_CLEAN_SOURCES))
def test_each_kind_of_unfinished_work_alone_makes_a_worktree_not_clean(tmp_path, source):
    trigger, changes, operations, index_lock = NOT_CLEAN_SOURCES[source]
    folder, _remote, work = small_world(tmp_path)
    land(work, "alder")
    commit_file(work, "file.txt", "theirs\n", "move the shared file")
    git(work, "push", "-q", "origin", "trunk")
    held = linked(work, tmp_path, "held", "alder")
    trigger(held)

    clone_document = removable_of(folder, work)

    document = find_worktree(clone_document, held)
    assert (document["changes"], document["operations"], document["index_lock"]) == (
        changes, operations, index_lock
    )
    assert document["status"] == "ok"
    assert removable_view(clone_document) == expected_view(
        work,
        {"alder": ["worktree_not_clean"], "trunk": None},
        {held: ["not_clean"]},
        count=(0, 0),
    )


@pytest.mark.skipif(ON_WINDOWS, reason="the sequence editor is a shell script")
def test_an_interactive_rebase_stopped_at_a_break_makes_a_clean_tree_not_clean(tmp_path):
    folder, _remote, work = small_world(tmp_path)
    land(work, "alder")
    commit_file(work, "later.txt", "later\n")
    git(work, "push", "-q", "origin", "trunk")
    held = linked(work, tmp_path, "held", "alder")
    stop_at_a_break(held)

    clone_document = removable_of(folder, work)

    document = find_worktree(clone_document, held)
    assert (document["changes"], document["operations"]) == (NO_CHANGES, ["rebase"])
    assert document["status"] == "ok"
    assert blockers_of(document["removable"]) == ["not_clean"]


def test_a_lock_blocks_a_clean_worktree_and_stacks_after_its_other_blockers(tmp_path):
    folder, _remote, work = small_world(tmp_path)
    for name in ("alder", "birch"):
        land(work, name)
    quiet = linked(work, tmp_path, "quiet", "alder")
    busy = linked(work, tmp_path, "busy", "birch")
    for held in (quiet, busy):
        git(work, "worktree", "lock", str(held))
    (busy / "scratch.txt").write_text("x\n")

    clone_document = removable_of(folder, work)

    assert removable_view(clone_document) == expected_view(
        work,
        {
            "alder": ["worktree_locked"],
            "birch": ["worktree_not_clean", "worktree_locked"],
            "trunk": None,
        },
        {quiet: ["locked"], busy: ["not_clean", "locked"]},
        count=(0, 0),
    )


@pytest.mark.parametrize("branch", ["alder", "birch"])
def test_a_branch_checked_out_in_the_main_worktree_is_blocked_for_it(tmp_path, branch):
    folder, _remote, work = small_world(tmp_path)
    land(work, "alder")
    land(work, "birch", merged=False)
    git(work, "checkout", "-q", branch)

    clone_document = removable_of(folder, work)

    expected = {
        "alder": ["checked_out_in_main_worktree"],
        "birch": ["not_merged"],
        "trunk": None,
    }
    if branch == "birch":
        expected = {"alder": [], "birch": ["not_merged", "checked_out_in_main_worktree"], "trunk": None}
    assert removable_view(clone_document) == expected_view(
        work, expected, count=(1 if branch == "birch" else 0, 0)
    )


def test_a_worktree_on_an_unmerged_branch_lists_the_merge_state_before_its_dirt(tmp_path):
    folder, _remote, work = small_world(tmp_path)
    land(work, "birch", merged=False)
    held = linked(work, tmp_path, "held", "birch")
    (held / "scratch.txt").write_text("x\n")

    clone_document = removable_of(folder, work)

    assert removable_view(clone_document) == expected_view(
        work,
        {"birch": ["not_merged", "worktree_not_clean"], "trunk": None},
        {held: ["not_merged", "not_clean"]},
        count=(0, 0),
    )


def test_a_detached_worktree_is_judged_by_the_commit_it_holds(tmp_path):
    folder, _remote, work = small_world(tmp_path)
    land(work, "alder")
    land(work, "birch", merged=False)
    on_merged = linked(work, tmp_path, "merged", "--detach", rev(work, "alder"))
    on_unmerged = linked(work, tmp_path, "unmerged", "--detach", rev(work, "birch"))

    clone_document = removable_of(folder, work)

    assert find_worktree(clone_document, on_merged)["head"]["branch"] is None
    assert removable_view(clone_document) == expected_view(
        work,
        {"alder": [], "birch": ["not_merged"], "trunk": None},
        {on_merged: [], on_unmerged: ["not_merged"]},
        count=(1, 1),
    )


def test_a_clone_with_no_base_blocks_everything_and_counts_nothing(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    work = folder / "work"
    git(folder, "init", "-q", "-b", "trunk", str(work))
    commit_file(work, "file.txt", "one\n")
    land(work, "alder", merged=False)
    git(work, "branch", "birch")
    held = linked(work, tmp_path, "held", "alder")
    adrift = linked(work, tmp_path, "adrift", "--detach", "birch")

    clone_document = removable_of(folder, work)

    assert clone_document["verdict_base"] is None
    assert removable_view(clone_document) == expected_view(
        work,
        {
            "alder": ["verdict_unknown"],
            "birch": ["verdict_unknown"],
            "trunk": ["verdict_unknown", "checked_out_in_main_worktree"],
        },
        {held: ["verdict_unknown"], adrift: ["verdict_unknown"]},
        count=(0, 0),
    )


def test_a_verdict_that_could_not_be_decided_blocks_a_branch_and_the_worktree_on_it(
    tmp_path, monkeypatch
):
    folder, _remote, work = small_world(tmp_path)
    land(work, "alder")
    land(work, "birch")
    held = linked(work, tmp_path, "held", "alder")
    monkeypatch.setattr(checkouts, "OWN_COMMIT_CAP", 1)

    clone_document = removable_of(folder, work)

    assert verdicts_of(clone_document)["alder"] == unknown("history_limit")
    assert removable_view(clone_document) == expected_view(
        work,
        {"alder": ["verdict_unknown"], "birch": ["verdict_unknown"], "trunk": None},
        {held: ["verdict_unknown"]},
        count=(0, 0),
    )


def test_a_worktree_on_a_branch_without_a_verdict_is_unknown(tmp_path):
    folder, _remote, work = small_world(tmp_path)
    land(work, "alder")
    git(work, "checkout", "-q", "alder")
    on_base = linked(work, tmp_path, "base", "trunk")
    unborn = linked(work, tmp_path, "unborn", "--detach", "trunk")
    git(unborn, "checkout", "-q", "--orphan", "fresh")
    git(unborn, "rm", "-rfq", ".")

    clone_document = removable_of(folder, work)

    assert find_worktree(clone_document, unborn)["head"] == {"branch": "fresh", "sha": None}
    assert removable_view(clone_document) == expected_view(
        work,
        {"alder": ["checked_out_in_main_worktree"], "trunk": None},
        {on_base: ["verdict_unknown"], unborn: ["verdict_unknown"]},
        count=(0, 0),
    )


def test_a_worktree_whose_directory_is_gone_blocks_nothing_but_its_own_verdict_and_lock(tmp_path):
    folder, _remote, work = small_world(tmp_path)
    for name in ("alder", "birch"):
        land(work, name)
    land(work, "cedar", merged=False)
    gone = linked(work, tmp_path, "gone", "alder")
    adrift = linked(work, tmp_path, "adrift", "--detach", rev(work, "birch"))
    elsewhere = linked(work, tmp_path, "elsewhere", "cedar")
    shutil.rmtree(gone)
    shutil.rmtree(adrift)
    shutil.rmtree(elsewhere)

    clone_document = removable_of(folder, work)

    for path in (gone, adrift, elsewhere):
        document = find_worktree(clone_document, path)
        assert document["prunable"] is True and document["status"] == "failed"
    assert removable_view(clone_document) == expected_view(
        work,
        {"alder": [], "birch": [], "cedar": ["not_merged"], "trunk": None},
        {gone: [], adrift: ["verdict_unknown"], elsewhere: ["not_merged"]},
        count=(2, 1),
    )


def test_a_locked_worktree_whose_directory_is_gone_is_not_prunable_and_so_not_clean(tmp_path):
    folder, _remote, work = small_world(tmp_path)
    land(work, "alder")
    held = linked(work, tmp_path, "held", "alder")
    git(work, "worktree", "lock", str(held))
    shutil.rmtree(held)

    clone_document = removable_of(folder, work)

    document = find_worktree(clone_document, held)
    assert document["locked"] is True and document["prunable"] is False
    assert document["status"] == "failed"
    assert_unreadable(document)
    assert branch_of(clone_document, "alder")["checked_out_in"] == [real(held)]
    assert removable_view(clone_document) == expected_view(
        work,
        {"alder": ["worktree_not_clean", "worktree_locked"], "trunk": None},
        {held: ["not_clean", "locked"]},
        count=(0, 0),
    )


def test_a_worktree_that_lost_only_its_git_link_still_holds_work_and_blocks(tmp_path):
    folder, _remote, work = small_world(tmp_path)
    land(work, "alder")
    held = linked(work, tmp_path, "held", "alder")
    (held / "land-note.txt").write_text("unsaved\n")
    (held / "alder-1.txt").write_text("edited\n")
    (held / ".git").unlink()
    entry = git(work, "worktree", "list", "--porcelain").stdout
    assert "prunable" in entry and held.is_dir()

    clone_document = removable_of(folder, work)

    document = find_worktree(clone_document, held)
    assert document["prunable"] is False and document["status"] == "failed"
    assert_unreadable(document)
    assert removable_view(clone_document) == expected_view(
        work,
        {"alder": ["worktree_not_clean"], "trunk": None},
        {held: ["not_clean"]},
        count=(0, 0),
    )


def test_an_unknown_verdict_state_blocks_as_not_merged():
    # Any state other than merged and unknown must read as not merged, so a
    # state added later blocks removal instead of passing as merged.
    verdict = {"state": "merged_with_local_commits", "rung": None, "reason": None}
    clone_document = {
        "verdict_base": {"name": "trunk"},
        "branches": [
            {"name": "alder", "verdict": verdict, "checked_out_in": ["/w/held"]},
            {"name": "trunk", "verdict": None, "checked_out_in": ["/w/main"]},
        ],
        "worktrees": [
            {"path": "/w/main", "main": True, "locked": False, "prunable": False, "status": "ok",
             "head": {"branch": "trunk", "sha": "0" * 39 + "1"}, "changes": NO_CHANGES,
             "operations": [], "index_lock": False, "verdict": None},
            {"path": "/w/held", "main": False, "locked": False, "prunable": False, "status": "ok",
             "head": {"branch": "alder", "sha": "0" * 39 + "2"}, "changes": NO_CHANGES,
             "operations": [], "index_lock": False, "verdict": None},
        ],
    }

    derived = models.derive_removable(clone_document)

    assert [blockers_of(item) for item in derived["branches"]] == [["not_merged"], None]
    assert [blockers_of(item) for item in derived["worktrees"]] == [None, ["not_merged"]]
    assert derived["removable_count"] == {"branches": 0, "worktrees": 0}


def test_a_worktree_with_an_unreadable_git_pointer_is_not_clean_and_so_is_its_branch(tmp_path):
    folder, _remote, work = small_world(tmp_path)
    land(work, "alder")
    broken = linked(work, tmp_path, "broken", "alder")
    replace_git_marker(broken, "garbage\n")

    clone_document = removable_of(folder, work)

    document = find_worktree(clone_document, broken)
    assert document["status"] == "failed" and document["prunable"] is False
    assert_unreadable(document)
    assert branch_of(clone_document, "alder")["checked_out_in"] == [real(broken)]
    assert removable_view(clone_document) == expected_view(
        work,
        {"alder": ["worktree_not_clean"], "trunk": None},
        {broken: ["not_clean"]},
        count=(0, 0),
    )


def test_a_bare_clone_counts_its_removable_items_and_blocks_no_branch_for_a_main_worktree(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    remote = make_remote(served)
    folder = tmp_path / "folder"
    folder.mkdir()
    work = clone(remote, tmp_path / "source")
    land(work, "alder")
    land(work, "birch", merged=False)
    land(work, "cedar")
    git(work, "push", "-q", "origin", "alder", "birch", "cedar")
    bare = tmp_path / "bare.git"
    git(tmp_path, "clone", "-q", "--bare", str(remote), str(bare))
    # The bare clone's own HEAD names a merged branch; nothing else holds it.
    git(bare, "symbolic-ref", "HEAD", "refs/heads/cedar")
    for branch in ("trunk", "alder", "birch", "cedar"):
        git(bare, "update-ref", f"refs/remotes/origin/{branch}", rev(bare, f"refs/heads/{branch}"))
    git(bare, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    held = folder / "held"
    git(bare, "worktree", "add", "-q", str(held), "alder")

    document = collect([folder])

    clone_document = find_clone(document, bare)
    assert clone_document["bare"] is True and main_worktree_count(clone_document) == 0
    assert clone_document["verdict_base"]["name"] == "trunk"
    assert removable_view(clone_document) == expected_view(
        bare,
        {"alder": [], "birch": ["not_merged"], "cedar": [], "trunk": None},
        {held: []},
        count=(2, 1),
        main=False,
    )


def worktree_state(worktree: Path) -> tuple[Any, ...]:
    """The files of one worktree and the commit it holds, read without running status."""
    files = {
        item.relative_to(worktree).as_posix(): item.read_bytes()
        for item in worktree.rglob("*")
        if item.is_file() and ".git" not in item.relative_to(worktree).parts[:1]
    }
    return files, rev(worktree, "HEAD")


def test_a_dirty_main_worktree_on_another_branch_changes_only_that_branch(tmp_path):
    folder, _remote, work = small_world(tmp_path)
    for name in ("alder", "birch", "cedar"):
        land(work, name)
    land(work, "dogwood", merged=False)
    held = linked(work, tmp_path, "held", "birch")
    (held / "scratch.txt").write_text("x\n")
    quiet = linked(work, tmp_path, "quiet", "cedar")
    before = removable_view(removable_of(folder, work))
    assert before == expected_view(
        work,
        {
            "alder": [], "birch": ["worktree_not_clean"], "cedar": [], "dogwood": ["not_merged"],
            "trunk": None,
        },
        {held: ["not_clean"], quiet: []},
        count=(2, 1),
    )
    git(work, "checkout", "-q", "alder")
    (work / "dirty.txt").write_text("x\n")
    states = {path: worktree_state(path) for path in (work, held, quiet)}
    refs = git(work, "for-each-ref").stdout
    listing = git(work, "worktree", "list", "--porcelain").stdout

    after = removable_view(removable_of(folder, work))

    assert after == {
        **before,
        "branches": {**before["branches"], "alder": ["checked_out_in_main_worktree"]},
        "count": {"branches": 1, "worktrees": 1},
    }
    assert {path: worktree_state(path) for path in states} == states
    assert git(work, "for-each-ref").stdout == refs
    assert git(work, "worktree", "list", "--porcelain").stdout == listing


def test_the_collector_names_no_blocker_that_only_the_derivation_decides():
    source = Path(checkouts.__file__).read_text()

    assert [name for name in BLOCKER_ONLY_NAMES if name in source] == []
    assert "derive_removable" in source


def test_a_branch_held_by_two_worktrees_is_blocked_by_either_of_them(tmp_path):
    folder, _remote, work = small_world(tmp_path)
    for name in ("alder", "birch"):
        land(work, name)
    first = linked(work, tmp_path, "calm", "alder")
    second = linked(work, tmp_path, "dirty", "--force", "alder")
    (second / "scratch.txt").write_text("x\n")
    open_one = linked(work, tmp_path, "calm-too", "birch")
    held_one = linked(work, tmp_path, "locked", "--force", "birch")
    git(work, "worktree", "lock", str(held_one))

    clone_document = removable_of(folder, work)

    assert branch_of(clone_document, "alder")["checked_out_in"] == [real(first), real(second)]
    assert branch_of(clone_document, "birch")["checked_out_in"] == [real(open_one), real(held_one)]
    assert removable_view(clone_document) == expected_view(
        work,
        {"alder": ["worktree_not_clean"], "birch": ["worktree_locked"], "trunk": None},
        {first: [], second: ["not_clean"], open_one: [], held_one: ["locked"]},
        count=(0, 2),
    )


# ---------------------------------------------------------------------------
# GitHub pull-request lookup: the one gh request per repository
# ---------------------------------------------------------------------------

GITHUB_URL = "https://github.com/acme/widget.git"
GITHUB_KEY = "github.com/acme/widget"
PR_NODE = {
    "number": 7,
    "state": "MERGED",
    "headRefOid": "a" * 40,
    "baseRefName": "trunk",
    "isCrossRepository": False,
}


def github_answer(count: int, node: dict[str, Any] | None = PR_NODE) -> dict[str, Any]:
    """A valid ``gh`` answer for ``count`` branch aliases."""
    repository: dict[str, Any] = {"__typename": "Repository"}
    for index in range(count):
        repository[f"b{index}"] = {"nodes": [] if node is None else [dict(node)]}
    return {"data": {"repository": repository}}


class GhCall:
    """One logged start of the fake ``gh``."""

    def __init__(self, directory: Path) -> None:
        self.args = (directory / "args").read_text().split("\n")[:-1]
        self.environment = dict(
            line.split("=", 1)
            for line in (directory / "env").read_text().split("\n")
            if "=" in line
        )
        body = directory / "body"
        self.body = body.read_bytes() if body.exists() else None
        input_file = directory / "input"
        self.input_path = input_file.read_text() if input_file.exists() else None
        self.started = int((directory / "started").read_text())


class FakeGh:
    """A POSIX shell script standing in for ``gh``: it logs the arguments, the
    environment, the request body and the start time of each call and replays a
    chosen stdout, stderr, exit code and delay."""

    def __init__(self, root: Path) -> None:
        self.bin = root / "fake-gh-bin"
        self.state = root / "fake-gh-state"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        script = self.bin / "gh"
        script.write_text(
            f"""#!/bin/sh
state="{self.state}"
call=$(mktemp -d "$state/call.XXXXXX")
printf '%s\\n' "$@" > "$call/args"
env > "$call/env"
date +%s > "$call/started"
previous=
for argument in "$@"; do
  if [ "$previous" = "--input" ]; then
    printf '%s' "$argument" > "$call/input"
    cp "$argument" "$call/body"
  fi
  previous=$argument
done
if [ -f "$state/delay" ]; then sleep "$(cat "$state/delay")"; fi
if [ -f "$state/stderr" ]; then cat "$state/stderr" >&2; fi
if [ -f "$state/stdout" ]; then cat "$state/stdout"; fi
exit "$(cat "$state/code")"
"""
        )
        script.chmod(0o755)
        self.replay()

    def replay(
        self, stdout: str | dict[str, Any] = "", stderr: str = "", code: int = 0, delay: float = 0
    ) -> None:
        text = stdout if isinstance(stdout, str) else json.dumps(stdout)
        (self.state / "stdout").write_text(text)
        (self.state / "stderr").write_text(stderr)
        (self.state / "code").write_text(str(code))
        (self.state / "delay").write_text(str(delay))

    def calls(self) -> list[GhCall]:
        return [GhCall(path) for path in sorted(self.state.glob("call.*"))]


def install_fake_gh(tmp_path: Path, monkeypatch, **replay: Any) -> FakeGh:
    """Put a fake ``gh`` first on ``PATH``."""
    fake = FakeGh(tmp_path)
    fake.replay(**replay)
    monkeypatch.setenv("PATH", f"{fake.bin}{os.pathsep}{os.environ['PATH']}")
    return fake


def git_only_path(tmp_path: Path) -> str:
    """A ``PATH`` that provides git and no ``gh``: ``gh`` may sit beside the real git."""
    real_git = shutil.which("git")
    assert real_git is not None
    directory = tmp_path / "only-git"
    directory.mkdir(exist_ok=True)
    if ON_WINDOWS:
        if (Path(real_git).parent / "gh.exe").exists():
            pytest.skip("gh.exe sits beside git.exe on this host")
        return str(Path(real_git).parent)
    wrapper = directory / "git"
    wrapper.write_text(f'#!/bin/sh\nexec "{real_git}" "$@"\n')
    wrapper.chmod(0o755)
    return str(directory)


def point_origin_at(repository: Path, remote: Path, url: str) -> None:
    """Configure ``url`` as origin while git reaches ``remote`` through ``insteadOf``."""
    git(repository, "config", "remote.origin.url", url)
    git(repository, "config", f"url.{remote.as_uri()}.insteadOf", url)


def github_collect(folder: Path, **options: Any) -> dict[str, Any]:
    options.setdefault("remote_checks", True)
    options.setdefault("github_lookup", True)
    return collect([folder], **options)


def lookup_of(document: dict[str, Any]) -> dict[str, Any]:
    [repository] = document["repositories"]
    return repository["pr_lookup"]


def lookup(status: str, reason: str | None) -> dict[str, Any]:
    return {"status": status, "reason": reason, "checked_at": None}


def two_clone_world(tmp_path: Path, url: str | None = GITHUB_URL) -> tuple[Path, Path, Path, Path]:
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    first = clone(remote, folder / "a")
    second = clone(remote, folder / "b")
    for repository, names in ((first, ["feature/x-1"]), (second, ["feature/x-1", "other"])):
        for name in names:
            git(repository, "branch", name)
        if url is not None:
            point_origin_at(repository, remote, url)
    return folder, remote, first, second


def branchless_world(tmp_path: Path) -> Path:
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    repository = clone(remote, folder / "empty")
    git(repository, "checkout", "-q", "--detach")
    git(repository, "update-ref", "-d", "refs/heads/trunk")
    point_origin_at(repository, remote, GITHUB_URL)
    return folder


def merged_world(tmp_path: Path) -> Path:
    """A GitHub-configured clone whose branch ``done`` was squash-merged into ``trunk``."""
    folder, remote, work = small_world(tmp_path)
    land(work, "done")
    point_origin_at(work, remote, GITHUB_URL)
    return folder


@posix_only
def test_one_gh_call_per_repository_carries_every_distinct_branch_name_in_a_body_file(
    tmp_path, monkeypatch
):
    folder, _remote, _first, _second = two_clone_world(tmp_path)
    names = ["feature/x-1", "other", "trunk"]
    fake = install_fake_gh(tmp_path, monkeypatch, stdout=github_answer(len(names)))
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setenv("GH_HOST", "elsewhere.example.invalid")
    monkeypatch.setenv("GH_REPO", "other/repository")

    document = github_collect(folder)

    [call] = fake.calls()
    assert call.args[:5] == ["api", "graphql", "--hostname", "github.com", "--input"]
    assert len(call.args) == 6
    assert call.input_path == call.args[5]
    assert not any(name in argument for argument in call.args for name in names)
    request = json.loads(call.body)
    assert set(request) == {"query", "variables"}
    assert request["variables"] == {
        "owner": "acme", "name": "widget", "h0": names[0], "h1": names[1], "h2": names[2],
    }
    query = request["query"]
    assert "repository(owner: $owner, name: $name)" in query
    assert "__typename" in query
    assert query.count("pullRequests(") == len(names)
    for index in range(len(names)):
        assert (
            f"b{index}: pullRequests(headRefName: $h{index}, first: 10, "
            "orderBy: {field: CREATED_AT, direction: DESC}) "
            "{ nodes { number state headRefOid baseRefName isCrossRepository } }"
        ) in query
    assert not Path(call.input_path).exists()
    assert not Path(call.input_path).parent.exists()
    environment = call.environment
    assert {name: environment.get(name) for name in (
        "GH_PROMPT_DISABLED", "GH_NO_UPDATE_NOTIFIER", "GH_SPINNER_DISABLED", "NO_COLOR",
    )} == {
        "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1",
        "GH_SPINNER_DISABLED": "1", "NO_COLOR": "1",
    }
    assert "GH_REPO" not in environment and "GH_HOST" not in environment
    assert environment["GH_TOKEN"] == "test-token"
    assert document["github_lookup"] == "on"
    [repository] = document["repositories"]
    assert repository["remote"]["key"] == GITHUB_KEY
    assert repository["remote"]["check"] == {"status": "live", "reason": None}
    assert repository["pr_lookup"] == lookup("live", None)


@posix_only
def test_a_gh_started_by_the_collector_receives_the_github_configuration_a_test_set(
    tmp_path, monkeypatch
):
    folder, *_ = two_clone_world(tmp_path)
    fake = install_fake_gh(tmp_path, monkeypatch, stdout=github_answer(3))
    config = tmp_path / "gh-config"
    config.mkdir()
    monkeypatch.setenv("GH_CONFIG_DIR", str(config))
    monkeypatch.setenv("GITHUB_TOKEN", "body-token")

    github_collect(folder)

    [call] = fake.calls()
    assert call.environment["GH_CONFIG_DIR"] == str(config)
    assert call.environment["GITHUB_TOKEN"] == "body-token"


@posix_only
def test_the_guard_removes_every_github_variable_before_the_test_body(
    tmp_path, monkeypatch, outer_github_environment
):
    # The outer environment did hold all seven, and a config directory with a hosts file.
    assert set(outer_github_environment) == set(GITHUB_SCRUBBED)
    assert (Path(outer_github_environment["GH_CONFIG_DIR"]) / "hosts.yml").is_file()
    assert [name for name in GITHUB_SCRUBBED if name in os.environ] == []
    folder, *_ = two_clone_world(tmp_path)
    fake = install_fake_gh(tmp_path, monkeypatch, stdout=github_answer(3))

    github_collect(folder)

    [call] = fake.calls()
    assert [name for name in GITHUB_SCRUBBED if name in call.environment] == []


@posix_only
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"data": {"repository": {"__typename": "Repository"}}}, lookup("live", None)),
        ({"data": {"repository": {}}}, lookup("unavailable", "failed")),
    ],
    ids=["typename", "no-typename"],
)
def test_a_repository_with_no_local_branch_still_makes_its_one_call(
    tmp_path, monkeypatch, answer, expected
):
    folder = branchless_world(tmp_path)
    fake = install_fake_gh(tmp_path, monkeypatch, stdout=answer)

    document = github_collect(folder)

    [call] = fake.calls()
    request = json.loads(call.body)
    assert request["variables"] == {"owner": "acme", "name": "widget"}
    assert "__typename" in request["query"]
    assert "pullRequests" not in request["query"]
    assert lookup_of(document) == expected


@posix_only
@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://acme.ghe.com/acme/widget.git", "acme.ghe.com"),
        ("https://gitlab.example.com/acme/widget.git", None),
        ("https://github.com/acme/widget/extra.git", None),
        (None, None),
    ],
    ids=["enterprise", "gitlab", "extra-path", "file-url"],
)
def test_only_a_github_host_with_an_owner_and_repository_path_is_called(
    tmp_path, monkeypatch, url, host
):
    # With no URL the clones keep the plain file remote they were cloned from.
    folder, *_ = two_clone_world(tmp_path, url)
    fake = install_fake_gh(tmp_path, monkeypatch, stdout=github_answer(3))

    document = github_collect(folder)

    if host is None:
        assert fake.calls() == []
        assert lookup_of(document) == lookup("off", "not_github")
    else:
        [call] = fake.calls()
        assert call.args[2:4] == ["--hostname", host]
        assert lookup_of(document) == lookup("live", None)


@posix_only
def test_a_gh_reachable_only_through_a_relative_path_entry_never_runs(tmp_path, monkeypatch):
    folder, *_ = two_clone_world(tmp_path)
    fake = FakeGh(tmp_path)
    fake.replay(stdout=github_answer(3))
    only_git = git_only_path(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f"{fake.bin.name}{os.pathsep}{only_git}")

    document = github_collect(folder)

    assert fake.calls() == []
    assert lookup_of(document) == lookup("unavailable", "gh_missing")


@pytest.mark.skipif(not ON_WINDOWS, reason="Windows resolution rules")
def test_windows_runs_only_a_gh_exe_found_on_an_absolute_path_entry(tmp_path, monkeypatch):
    folder, *_ = two_clone_world(tmp_path)
    only_git = git_only_path(tmp_path)
    marker = tmp_path / "cmd-marker"
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "gh.cmd").write_text(f'@echo ran> "{marker}"\r\n')
    working = tmp_path / "working"
    working.mkdir()
    shutil.copy(os.environ["COMSPEC"], working / "gh.exe")
    monkeypatch.chdir(working)
    monkeypatch.setenv("PATH", f"{only_git}{os.pathsep}{shim}")

    document = github_collect(folder)

    assert lookup_of(document) == lookup("unavailable", "gh_missing")
    assert not marker.exists()


@pytest.mark.skipif(not ON_WINDOWS, reason="Windows resolution rules")
def test_windows_finds_and_runs_a_gh_exe_first_on_the_path(tmp_path, monkeypatch):
    folder, *_ = two_clone_world(tmp_path)
    only_git = git_only_path(tmp_path)
    program = tmp_path / "program"
    program.mkdir()
    shutil.copy(os.environ["COMSPEC"], program / "gh.exe")
    monkeypatch.setenv("PATH", f"{program}{os.pathsep}{only_git}")

    document = github_collect(folder)

    assert lookup_of(document) == lookup("unavailable", "failed")


def valid_for_merged_world() -> dict[str, Any]:
    return github_answer(2)


@posix_only
def test_lookup_off_makes_no_call_and_reports_the_setting(tmp_path, monkeypatch):
    folder = merged_world(tmp_path)
    fake = install_fake_gh(tmp_path, monkeypatch, stdout=valid_for_merged_world())
    baseline = github_collect(folder, github_lookup=False)

    assert fake.calls() == []
    assert baseline["github_lookup"] == "off"
    assert lookup_of(baseline) == lookup("off", "setting_off")
    assert baseline["repositories"][0]["remote"]["check"]["status"] == "live"


@posix_only
def test_remote_checks_off_makes_no_call_and_reports_it(tmp_path, monkeypatch):
    folder = merged_world(tmp_path)
    fake = install_fake_gh(tmp_path, monkeypatch, stdout=valid_for_merged_world())

    document = github_collect(folder, remote_checks=False)

    assert fake.calls() == []
    assert lookup_of(document) == lookup("off", "remote_checks_off")
    assert document["repositories"][0]["remote"]["check"] == {
        "status": "off", "reason": "remote_checks_off",
    }


@posix_only
def test_a_path_with_git_and_no_gh_reports_gh_missing(tmp_path, monkeypatch):
    folder = merged_world(tmp_path)
    only_git = git_only_path(tmp_path)
    monkeypatch.setenv("PATH", only_git)

    document = github_collect(folder)

    assert lookup_of(document) == lookup("unavailable", "gh_missing")


def merged_answer_with(node_change: dict[str, Any]) -> dict[str, Any]:
    answer = github_answer(2)
    answer["data"]["repository"]["b0"]["nodes"][0].update(node_change)
    return answer


def oversized_answer() -> str:
    return json.dumps(github_answer(2)) + " " * (8 << 20)


HTTP_401 = "HTTP 401: Bad credentials (https://api.github.com/graphql)"
RATE_LIMITED_BODY = {"errors": [{"type": "RATE_LIMITED", "message": "slow down"}]}
NOT_FOUND_BODY = {
    "data": {"repository": None},
    "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}],
}
MISSING_NODES = github_answer(2)
del MISSING_NODES["data"]["repository"]["b1"]["nodes"]
NULL_REPOSITORY = {"data": {"repository": None}}


def answer_with_repository_field(name: str, value: Any) -> dict[str, Any]:
    answer = github_answer(2)
    answer["data"]["repository"][name] = value
    return answer


# Values of the wrong JSON type at each place the answer is read: each must make
# the lookup fail rather than raise while it is hashed, compared or indexed.
ODD_SHAPE_CASES = [
    ("state-list", merged_answer_with({"state": ["MERGED"]})),
    ("state-dict", merged_answer_with({"state": {"MERGED": True}})),
    ("number-list", merged_answer_with({"number": [7]})),
    ("head-object-list", merged_answer_with({"headRefOid": ["a" * 40]})),
    ("base-name-dict", merged_answer_with({"baseRefName": {"trunk": 1}})),
    ("data-list", {"data": [{"repository": None}]}),
    ("repository-list", {"data": {"repository": [{"__typename": "Repository"}]}}),
    ("typename-list", answer_with_repository_field("__typename", ["Repository"])),
    ("alias-list", answer_with_repository_field("b0", [{"nodes": []}])),
    ("nodes-object", answer_with_repository_field("b0", {"nodes": {}})),
    ("node-list", answer_with_repository_field("b0", {"nodes": [[7, "MERGED"]]})),
]

CATEGORY_CASES = [
    ("exit-4", {"code": 4}, "not_authenticated"),
    ("http-401", {"code": 1, "stderr": HTTP_401}, "not_authenticated"),
    ("auth-login", {"code": 1, "stderr": "To authenticate, run: gh auth login"}, "not_authenticated"),
    ("rate-limit-text", {"code": 1, "stderr": "API rate limit exceeded"}, "rate_limited"),
    (
        "rate-limited-type",
        {"code": 1, "stdout": RATE_LIMITED_BODY, "stderr": "something unrelated"},
        "rate_limited",
    ),
    ("not-found-type", {"code": 1, "stdout": NOT_FOUND_BODY}, "not_found"),
    ("null-repository", {"code": 0, "stdout": NULL_REPOSITORY}, "not_found"),
    ("null-repository-exit-1", {"code": 1, "stdout": NULL_REPOSITORY}, "not_found"),
    ("exit-4-beats-rate-limit", {"code": 4, "stderr": "rate limit"}, "not_authenticated"),
    (
        "rate-limit-beats-credentials",
        {"code": 1, "stderr": f"rate limit and {HTTP_401}"},
        "rate_limited",
    ),
    ("not-json", {"code": 0, "stdout": "this is not json"}, "failed"),
    ("missing-nodes", {"code": 0, "stdout": MISSING_NODES}, "failed"),
    ("draft-state", {"code": 0, "stdout": merged_answer_with({"state": "DRAFT"})}, "failed"),
    ("zero-number", {"code": 0, "stdout": merged_answer_with({"number": 0})}, "failed"),
    (
        "bad-head-object",
        {"code": 0, "stdout": merged_answer_with({"headRefOid": "not-an-object"})},
        "failed",
    ),
    (
        "cross-not-boolean",
        {"code": 0, "stdout": merged_answer_with({"isCrossRepository": "no"})},
        "failed",
    ),
    ("over-8-mib", {"code": 0, "stdout": oversized_answer()}, "failed"),
    ("exit-2", {"code": 2}, "failed"),
    (
        "error-type-list",
        {"code": 1, "stdout": {"errors": [{"type": ["RATE_LIMITED"]}]}},
        "failed",
    ),
    (
        "error-type-dict",
        {"code": 1, "stdout": {"errors": [{"type": {"RATE_LIMITED": 1}}]}},
        "failed",
    ),
    ("huge-integer-exit-0", {"code": 0, "stdout": "1" * 5000}, "failed"),
    ("huge-integer-exit-1", {"code": 1, "stdout": "1" * 5000}, "failed"),
    *((name, {"code": 0, "stdout": answer}, "failed") for name, answer in ODD_SHAPE_CASES),
]


@posix_only
def test_a_valid_answer_is_live_with_the_same_checks_and_verdicts_as_lookup_off(
    tmp_path, monkeypatch
):
    folder = merged_world(tmp_path)
    fake = install_fake_gh(tmp_path, monkeypatch, stdout=valid_for_merged_world())
    off = github_collect(folder, github_lookup=False)

    document = github_collect(folder)

    assert len(fake.calls()) == 1
    assert lookup_of(document) == lookup("live", None)
    assert document["repositories"][0]["remote"]["check"] == off["repositories"][0]["remote"]["check"]
    [work] = all_clones(document)
    assert verdict_tuple(branch_of(work, "done")["verdict"]) == MERGED_SQUASH


@posix_only
@pytest.mark.parametrize(
    ("replay", "reason"),
    [case[1:] for case in CATEGORY_CASES],
    ids=[case[0] for case in CATEGORY_CASES],
)
def test_each_gh_failure_category_comes_from_its_trigger_and_leaves_the_git_ladder_alone(
    tmp_path, monkeypatch, replay, reason
):
    folder = merged_world(tmp_path)
    fake = install_fake_gh(tmp_path, monkeypatch, **replay)
    off = github_collect(folder, github_lookup=False)

    document = github_collect(folder)

    assert len(fake.calls()) == 1
    assert lookup_of(document) == lookup("unavailable", reason)
    assert document["repositories"][0]["remote"]["check"] == off["repositories"][0]["remote"]["check"]
    [work] = all_clones(document)
    assert verdict_tuple(branch_of(work, "done")["verdict"]) == MERGED_SQUASH


@posix_only
def test_a_gh_that_outlasts_the_call_cap_times_out_while_the_remote_check_stays_live(
    tmp_path, monkeypatch
):
    folder = merged_world(tmp_path)
    install_fake_gh(tmp_path, monkeypatch, stdout=valid_for_merged_world(), delay=30)

    started = time.monotonic()
    document = github_collect(folder, call_cap_seconds=2)
    elapsed = time.monotonic() - started

    assert elapsed < 15
    assert lookup_of(document) == lookup("unavailable", "timed_out")
    assert document["repositories"][0]["remote"]["check"] == {"status": "live", "reason": None}
    [work] = all_clones(document)
    assert verdict_tuple(branch_of(work, "done")["verdict"]) == MERGED_SQUASH


@pytest.mark.parametrize(
    ("key", "identity"),
    [
        ("github.com/acme/widget", ("github.com", "acme", "widget")),
        ("acme.ghe.com/acme/widget", ("acme.ghe.com", "acme", "widget")),
        ("gitlab.example.com/acme/widget", None),
        ("github.com/acme/widget/extra", None),
        ("github.com/acme", None),
        ("github.com:8443/acme/widget", None),
        (".ghe.com/acme/widget", None),
        ("github.com//widget", None),
        ("ghe.com/acme/widget", None),
    ],
)
def test_github_identity_accepts_only_a_github_host_with_an_owner_and_repository(key, identity):
    assert models.github_identity(key) == identity


def lookup_document(sample_document, *, github_lookup, remote_checks, key, pr_lookup):
    document = doc_copy(sample_document)
    document["github_lookup"] = github_lookup
    document["remote_checks"] = remote_checks
    document["repositories"][0]["remote"]["key"] = key
    document["repositories"][0]["pr_lookup"] = pr_lookup
    return redigest(document)


def recorded_lookup(reason: str | None, checked_at: str | None = "2026-10-24T00:00:00Z"):
    return {"status": "recorded", "reason": reason, "checked_at": checked_at}


LOOKUP_ACCEPTED = [
    ("off", "on", GITHUB_KEY, lookup("off", "setting_off")),
    ("off", "off", GITHUB_KEY, lookup("off", "setting_off")),
    ("on", "on", "example.com/acme/widget", lookup("off", "not_github")),
    ("on", "off", "example.com/acme/widget", lookup("off", "not_github")),
    ("on", "off", GITHUB_KEY, lookup("off", "remote_checks_off")),
    ("on", "off", GITHUB_KEY, recorded_lookup("remote_checks_off")),
    ("on", "on", GITHUB_KEY, lookup("live", None)),
    *[
        ("on", "on", GITHUB_KEY, recorded_lookup(reason))
        for reason in (
            "gh_missing", "not_authenticated", "not_found", "rate_limited", "timed_out", "failed",
        )
    ],
    *[
        ("on", "on", GITHUB_KEY, lookup("unavailable", reason))
        for reason in (
            "gh_missing", "not_authenticated", "not_found", "rate_limited", "timed_out", "failed",
        )
    ],
]
LOOKUP_REFUSED = [
    ("off", "on", GITHUB_KEY, lookup("off", "not_github")),
    ("off", "on", GITHUB_KEY, lookup("live", None)),
    ("on", "on", "example.com/acme/widget", lookup("live", None)),
    ("on", "on", GITHUB_KEY, lookup("off", "not_github")),
    ("on", "on", GITHUB_KEY, lookup("off", "setting_off")),
    ("on", "on", GITHUB_KEY, lookup("off", "remote_checks_off")),
    ("on", "on", GITHUB_KEY, lookup("live", "failed")),
    ("on", "on", GITHUB_KEY, lookup("unavailable", None)),
    ("on", "on", GITHUB_KEY, lookup("unavailable", "setting_off")),
    ("on", "off", GITHUB_KEY, lookup("live", None)),
    ("on", "off", GITHUB_KEY, lookup("unavailable", "failed")),
    ("on", "on", GITHUB_KEY, {**lookup("live", None), "checked_at": "2026-10-24T00:00:00Z"}),
    ("on", "on", GITHUB_KEY, recorded_lookup("remote_checks_off")),
    ("on", "on", GITHUB_KEY, None),
]


@pytest.mark.parametrize(("github", "checks", "key", "pr_lookup"), LOOKUP_ACCEPTED)
def test_parser_accepts_each_lookup_status_and_reason_combination(
    sample, github, checks, key, pr_lookup
):
    document, _folder = sample
    broken = lookup_document(
        document, github_lookup=github, remote_checks=checks, key=key, pr_lookup=pr_lookup
    )
    assert models.parse_checkouts(broken)["repositories"][0]["pr_lookup"] == pr_lookup


@pytest.mark.parametrize(("github", "checks", "key", "pr_lookup"), LOOKUP_REFUSED)
def test_parser_refuses_a_lookup_status_that_contradicts_the_setting_or_the_remote(
    sample, github, checks, key, pr_lookup
):
    document, _folder = sample
    accepted = lookup_document(
        document, github_lookup="off", remote_checks="on", key=GITHUB_KEY,
        pr_lookup=lookup("off", "setting_off"),
    )
    assert models.parse_checkouts(accepted)
    broken = doc_copy(accepted)
    broken["github_lookup"] = github
    broken["remote_checks"] = checks
    broken["repositories"][0]["remote"]["key"] = key
    broken["repositories"][0]["pr_lookup"] = pr_lookup
    assert "pr_lookup" in refusal_of(broken)


def test_parser_refuses_a_lookup_on_a_repository_with_no_remote_and_a_missing_one_with_a_remote(
    sample,
):
    document, _folder = sample
    remoteless = doc_copy(document)
    without_remote(remoteless)
    remoteless["repositories"][0]["pr_lookup"] = lookup("off", "setting_off")
    assert "without a remote" in refusal_of(remoteless)
    for key in ("github_lookup",):
        missing = doc_copy(document)
        del missing[key]
        assert "invalid shape" in refusal_of(missing)
    absent = doc_copy(document)
    del absent["repositories"][0]["pr_lookup"]
    assert "invalid shape" in refusal_of(absent)


@posix_only
def test_the_request_declares_every_variable_its_aliases_use(tmp_path, monkeypatch):
    # GitHub refuses a query that uses an undeclared variable, so every real lookup
    # would fail even though the fake accepts any body.
    folder, *_ = two_clone_world(tmp_path)
    fake = install_fake_gh(tmp_path, monkeypatch, stdout=github_answer(3))

    github_collect(folder)

    [call] = fake.calls()
    query = json.loads(call.body)["query"]
    header = re.match(r"query\((?P<declarations>[^)]*)\)", query)
    assert header is not None
    declared = dict(
        item.strip().split(": ", 1) for item in header["declarations"].split(",")
    )
    assert declared == {
        "$owner": "String!", "$name": "String!",
        "$h0": "String!", "$h1": "String!", "$h2": "String!",
    }
    assert set(re.findall(r"\$\w+", query[header.end():])) == set(declared)


@posix_only
@pytest.mark.parametrize("case", ["setting-off", "remote-checks-off", "gh-missing"])
def test_a_lookup_that_makes_no_call_leaves_the_remote_check_and_the_git_ladder_alone(
    tmp_path, monkeypatch, case
):
    folder = merged_world(tmp_path)
    fake = install_fake_gh(tmp_path, monkeypatch, stdout=valid_for_merged_world())
    remote_checks = case != "remote-checks-off"
    off = github_collect(folder, github_lookup=False, remote_checks=remote_checks)
    if case == "gh-missing":
        monkeypatch.setenv("PATH", git_only_path(tmp_path))

    document = github_collect(
        folder, github_lookup=case != "setting-off", remote_checks=remote_checks
    )

    assert fake.calls() == []
    assert lookup_of(document) == {
        "setting-off": lookup("off", "setting_off"),
        "remote-checks-off": lookup("off", "remote_checks_off"),
        "gh-missing": lookup("unavailable", "gh_missing"),
    }[case]
    assert document["repositories"][0]["remote"]["check"] == off["repositories"][0]["remote"]["check"]
    [work] = all_clones(document)
    assert verdict_tuple(branch_of(work, "done")["verdict"]) == MERGED_SQUASH


@posix_only
def test_a_gh_call_is_cut_at_the_collection_deadline_before_its_call_cap(tmp_path, monkeypatch):
    folder = merged_world(tmp_path)
    install_fake_gh(tmp_path, monkeypatch, stdout=valid_for_merged_world(), delay=30)

    started = time.monotonic()
    document = github_collect(folder, seconds=4, call_cap_seconds=20)
    elapsed = time.monotonic() - started

    assert lookup_of(document) == lookup("unavailable", "timed_out")
    assert elapsed < 12


# ---------------------------------------------------------------------------
# GitHub pull requests: the facts of each branch and the pull-request rung
# ---------------------------------------------------------------------------

UNRELATED_HEAD = "b" * 40
MISSING_HEAD = "c" * 40


def pr_node(
    number: int, state: str, head: str, base: str = "trunk", cross: bool = False
) -> dict[str, Any]:
    return {
        "number": number,
        "state": state.upper(),
        "headRefOid": head,
        "baseRefName": base,
        "isCrossRepository": cross,
    }


def local_names(*repositories: Path) -> list[str]:
    """The distinct local branch names of the clones, sorted as the request sorts them."""
    names: set[str] = set()
    for repository in repositories:
        listing = git(repository, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        names.update(listing.stdout.split())
    return sorted(names)


def pr_answer(names: list[str], pulls: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """A valid ``gh`` answer: ``pulls`` by branch name, one alias per sorted name."""
    repository: dict[str, Any] = {"__typename": "Repository"}
    for index, name in enumerate(names):
        repository[f"b{index}"] = {"nodes": [dict(node) for node in pulls.get(name, [])]}
    return {"data": {"repository": repository}}


def verdict_facts(branch_or_worktree: dict[str, Any]):
    verdict = branch_or_worktree["verdict"]
    return None if verdict is None else (
        verdict["state"], verdict["rung"], verdict["reason"], verdict["pull_request"]
    )


def pr_verdict(number: int) -> tuple[str, str, None, int]:
    return ("merged", "pull_request", None, number)


NOT_MERGED_FACTS = ("not_merged", "none", None, None)


def listed(branch: dict[str, Any]) -> list[tuple[int, str, bool]]:
    return [(pull["number"], pull["state"], pull["head_matches"]) for pull in branch["pull_requests"]]


@posix_only
def test_each_branch_lists_the_same_repository_pull_requests_that_name_it(tmp_path, monkeypatch):
    folder, remote, work = small_world(tmp_path)
    point_origin_at(work, remote, GITHUB_URL)
    git(work, "checkout", "-q", "-b", "topic", "trunk")
    tip = commit_file(work, "topic.txt", "topic\n")
    git(work, "checkout", "-q", "trunk")
    git(work, "branch", "plain", "trunk")
    git(work, "branch", "many", "trunk")
    names = local_names(work)
    many = [pr_node(number, "closed", UNRELATED_HEAD) for number in range(40, 29, -1)]
    pulls = {
        "topic": [
            pr_node(9, "closed", UNRELATED_HEAD),
            pr_node(7, "open", tip),
            pr_node(5, "merged", UNRELATED_HEAD),
            pr_node(6, "merged", tip, base="release"),
            pr_node(8, "merged", tip, cross=True),
        ],
        "many": many,
    }
    fake = install_fake_gh(tmp_path, monkeypatch, stdout=pr_answer(names, pulls))

    document = github_collect(folder)

    assert lookup_of(document) == lookup("live", None)
    [item] = all_clones(document)
    assert [pull["number"] for pull in many_of(item)] == list(range(31, 41))
    assert branch_of(item, "topic")["pull_requests"] == [
        {"number": 5, "state": "merged", "head_matches": False},
        {"number": 6, "state": "merged", "head_matches": True},
        {"number": 7, "state": "open", "head_matches": True},
        {"number": 9, "state": "closed", "head_matches": False},
    ]
    # An open request, one into another base and a cross-repository one never decide.
    assert verdict_facts(branch_of(item, "topic")) == NOT_MERGED_FACTS
    # Every branch carries the list, the verdict base's and a branch with no node included.
    assert branch_of(item, "trunk")["pull_requests"] == []
    assert branch_of(item, "plain")["pull_requests"] == []
    assert all("pull_requests" in branch for branch in item["branches"])

    # Nothing is listed when the lookup is off, cannot answer, or the clone has no origin.
    off = github_collect(folder, github_lookup=False)
    assert all(b["pull_requests"] == [] for b in find_clone(off, work)["branches"])
    unchecked = github_collect(folder, remote_checks=False)
    assert lookup_of(unchecked) == lookup("off", "remote_checks_off")
    assert all(b["pull_requests"] == [] for b in find_clone(unchecked, work)["branches"])
    fake.replay(code=4)
    refused = github_collect(folder)
    assert lookup_of(refused) == lookup("unavailable", "not_authenticated")
    assert all(b["pull_requests"] == [] for b in find_clone(refused, work)["branches"])
    git(work, "remote", "remove", "origin")
    fake.replay(stdout=pr_answer(names, pulls))
    remoteless = github_collect(folder)
    [repository] = remoteless["repositories"]
    assert repository["pr_lookup"] is None
    assert all(b["pull_requests"] == [] for b in all_clones(remoteless)[0]["branches"])


def many_of(item: dict[str, Any]) -> list[dict[str, Any]]:
    return branch_of(item, "many")["pull_requests"]


def landed_world(tmp_path: Path):
    """A clone with ``done`` and ``sq`` squash-merged, ``open`` unmerged, all on GitHub."""
    folder, remote, work = small_world(tmp_path)
    land(work, "done")
    land(work, "sq")
    git(work, "checkout", "-q", "-b", "open", "trunk")
    commit_file(work, "open.txt", "open\n")
    git(work, "checkout", "-q", "trunk")
    point_origin_at(work, remote, GITHUB_URL)
    return folder, remote, work


@posix_only
def test_a_merged_pull_request_whose_head_is_the_tip_proves_the_branch_merged_uncached(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, _remote, work = landed_world(tmp_path)
    tip = rev(work, "done")
    detached = linked(work, tmp_path, "at-done", "--detach", "done")
    names = local_names(work)
    pulls = {"done": [pr_node(4, "merged", tip), pr_node(3, "merged", tip)]}
    fake = install_fake_gh(tmp_path, monkeypatch, stdout=pr_answer(names, pulls))

    document = github_collect(folder)

    item = find_clone(document, work)
    assert branch_of(item, "done")["verdict"] == {
        "state": "merged", "rung": "pull_request", "reason": None, "pull_request": 3,
    }
    assert "not_merged" not in branch_of(item, "done")["removable"]["blockers"]
    assert branch_of(item, "done")["removable"] == {"removable": True, "blockers": []}
    # Every other verdict has no pull request, and the git ladder decided them.
    assert verdict_facts(branch_of(item, "sq")) == ("merged", "squash_equivalent", None, None)
    assert verdict_facts(branch_of(item, "open")) == NOT_MERGED_FACTS
    assert branch_of(item, "trunk")["verdict"] is None
    # A linked detached worktree at the tip takes the commit's one verdict.
    linked_at_tip = find_worktree(item, detached)
    assert verdict_facts(linked_at_tip) == pr_verdict(3)
    assert "not_merged" not in linked_at_tip["removable"]["blockers"]
    # No pull-request verdict is remembered, while a git-ladder verdict is.
    base = rev(work, "origin/trunk")
    stored = cache_entries(cache)
    assert f"{tip}.{base}" not in stored
    assert stored[f"{rev(work, 'sq')}.{base}"] == "squash_equivalent"
    assert stored[f"{rev(work, 'open')}.{base}"] == "none"

    # With the lookup unable to answer and no saved listing to stand in for it, the
    # same branches have their git verdicts.
    saved = saved_file(cache)
    for record in saved["remote_checks"].values():
        record["pull_requests"] = None
    cache.write_text(json.dumps(saved))
    fake.replay(code=4)
    plain = github_collect(folder)
    plain_item = find_clone(plain, work)
    assert verdict_facts(branch_of(plain_item, "done")) == ("merged", "squash_equivalent", None, None)
    assert verdict_facts(find_worktree(plain_item, detached)) == (
        "merged", "squash_equivalent", None, None,
    )
    assert all(
        (b["verdict"] or {"pull_request": None})["pull_request"] is None
        for b in plain_item["branches"]
    )


@posix_only
def test_a_branch_the_base_already_contains_stays_ancestor_beside_a_merged_request(
    tmp_path, monkeypatch
):
    folder, remote, work = small_world(tmp_path)
    git(work, "checkout", "-q", "-b", "joined", "trunk")
    tip = commit_file(work, "joined.txt", "joined\n")
    git(work, "checkout", "-q", "trunk")
    git(work, "merge", "-q", "--no-ff", "-m", "merge joined", "joined")
    git(work, "push", "-q", "origin", "trunk")
    point_origin_at(work, remote, GITHUB_URL)
    install_fake_gh(
        tmp_path, monkeypatch,
        stdout=pr_answer(local_names(work), {"joined": [pr_node(2, "merged", tip)]}),
    )

    joined = branch_of(find_clone(github_collect(folder), work), "joined")

    assert verdict_facts(joined) == ("merged", "ancestor", None, None)
    assert listed(joined) == [(2, "merged", True)]


@posix_only
def test_branches_at_one_commit_share_the_verdict_of_the_pull_request_that_names_one(
    tmp_path, monkeypatch
):
    folder, remote, work = small_world(tmp_path)
    git(work, "checkout", "-q", "-b", "x", "trunk")
    tip = commit_file(work, "x.txt", "x\n")
    git(work, "branch", "y", "x")
    git(work, "checkout", "-q", "trunk")
    point_origin_at(work, remote, GITHUB_URL)
    install_fake_gh(
        tmp_path, monkeypatch,
        stdout=pr_answer(local_names(work), {"x": [pr_node(15, "merged", tip)]}),
    )

    item = find_clone(github_collect(folder), work)

    assert verdict_facts(branch_of(item, "x")) == pr_verdict(15)
    assert verdict_facts(branch_of(item, "y")) == pr_verdict(15)
    assert listed(branch_of(item, "x")) == [(15, "merged", True)]
    assert listed(branch_of(item, "y")) == []


@posix_only
def test_a_detached_worktree_at_a_commit_no_local_branch_holds_never_takes_the_rung(
    tmp_path, monkeypatch
):
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    first = clone(remote, folder / "a")
    second = clone(remote, folder / "b")
    git(second, "checkout", "-q", "-b", "elsewhere", "trunk")
    head = commit_file(second, "elsewhere.txt", "elsewhere\n")
    git(first, "fetch", "-q", str(second), "elsewhere")
    detached = linked(first, tmp_path, "at-head", "--detach", head)
    for repository in (first, second):
        point_origin_at(repository, remote, GITHUB_URL)
    install_fake_gh(
        tmp_path, monkeypatch,
        stdout=pr_answer(local_names(first, second), {"elsewhere": [pr_node(31, "merged", head)]}),
    )

    document = github_collect(folder)

    assert verdict_facts(find_worktree(find_clone(document, first), detached)) == NOT_MERGED_FACTS
    # The clone that holds the named branch is decided by the same request.
    assert verdict_facts(branch_of(find_clone(document, second), "elsewhere")) == pr_verdict(31)


@posix_only
def test_a_shallow_clone_takes_the_rung_before_its_incomplete_clone_verdict(
    tmp_path, monkeypatch
):
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    author = clone(remote, tmp_path / "author")
    git(author, "checkout", "-q", "-b", "topic", "trunk")
    tip = commit_file(author, "topic.txt", "topic\n")
    git(author, "push", "-q", "origin", "topic")
    shallow = clone(remote.as_uri(), folder / "shallow", "--depth=1", "--no-single-branch")
    git(shallow, "branch", "topic", "origin/topic")
    point_origin_at(shallow, remote, GITHUB_URL)
    names = local_names(shallow)
    fake = install_fake_gh(
        tmp_path, monkeypatch,
        stdout=pr_answer(names, {"topic": [pr_node(21, "merged", tip)]}),
    )

    item = find_clone(github_collect(folder), shallow)

    assert item["shallow"] is True
    assert verdict_facts(branch_of(item, "topic")) == pr_verdict(21)
    fake.replay(code=4)
    undecided = find_clone(github_collect(folder), shallow)
    assert verdict_facts(branch_of(undecided, "topic")) == (
        "unknown", None, "incomplete_clone", None,
    )


def squash_world(
    tmp_path: Path, *, first_inside: bool = True, filtered: bool = False, trunk_fetched: bool = False
):
    """Clones ``a`` and ``b`` share ``topic`` at ``tip``; ``a`` then adds two commits,
    pushes ``topic`` and squash-merges it into ``trunk``, and ``b`` fetches nothing
    (only ``trunk``, when ``trunk_fetched``: the head of ``topic`` stays missing).

    Returns the folder, the clones and the commits ``tip`` and ``head``."""
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    if filtered:
        allow_filters(remote)
    first = clone(remote, (folder if first_inside else tmp_path / "outside") / "a")
    git(first, "checkout", "-q", "-b", "topic", "trunk")
    tip = commit_file(first, "topic.txt", "topic\n")
    git(first, "push", "-q", "origin", "topic")
    if filtered:
        second = clone(remote.as_uri(), folder / "b", "--filter=blob:none")
    else:
        second = clone(remote, folder / "b")
    git(second, "branch", "topic", "origin/topic")
    commit_file(first, "topic-2.txt", "two\n")
    head = commit_file(first, "topic-3.txt", "three\n")
    git(first, "push", "-q", "origin", "topic")
    git(first, "checkout", "-q", "trunk")
    git(first, "merge", "--squash", "topic")
    git(first, "commit", "-q", "-m", "land topic")
    git(first, "push", "-q", "origin", "trunk")
    for repository in (first, second):
        point_origin_at(repository, remote, GITHUB_URL)
    if trunk_fetched:
        git(second, "fetch", "-q", "origin", "trunk")
    return folder, remote, first, second, tip, head


def squash_answer(
    clones: tuple[Path, ...], head: str, *, number: int = 12, **node: Any
) -> dict[str, Any]:
    pulls = [pr_node(number, node.pop("state", "merged"), head, **node)]
    return pr_answer(local_names(*clones), {"topic": pulls})


@posix_only
def test_a_tip_that_is_an_ancestor_of_a_merged_head_found_in_a_sibling_is_merged(
    tmp_path, monkeypatch
):
    folder, _remote, first, second, tip, head = squash_world(tmp_path)
    install_fake_gh(tmp_path, monkeypatch, stdout=squash_answer((first, second), head))
    assert git(second, "cat-file", "-e", head, check=False).returncode != 0
    before = object_files(second)

    document = github_collect(folder)

    item = find_clone(document, second)
    topic = branch_of(item, "topic")
    assert topic["sha"] == tip
    assert verdict_facts(topic) == pr_verdict(12)
    assert listed(topic) == [(12, "merged", False)]
    assert listed(branch_of(find_clone(document, first), "topic")) == [(12, "merged", True)]
    assert verdict_facts(branch_of(find_clone(document, first), "topic")) == pr_verdict(12)
    # Nothing was fetched into the lagging clone: the head was read from the sibling.
    assert object_files(second) == before
    assert git(second, "cat-file", "-e", head, check=False).returncode != 0


@pytest.mark.parametrize(
    "change",
    [{"base": "release"}, {"state": "open"}, {"state": "closed"}, {"cross": True}],
    ids=["other-base", "open", "closed", "cross-repository"],
)
@posix_only
def test_a_request_that_is_not_a_merged_same_repository_one_into_the_base_never_decides(
    tmp_path, monkeypatch, change
):
    folder, _remote, first, second, _tip, head = squash_world(tmp_path)
    install_fake_gh(tmp_path, monkeypatch, stdout=squash_answer((first, second), head, **change))

    topic = branch_of(find_clone(github_collect(folder), second), "topic")

    assert verdict_facts(topic) == NOT_MERGED_FACTS
    assert len(topic["pull_requests"]) == (0 if change.get("cross") else 1)


@posix_only
def test_unavailable_and_non_ancestor_heads_are_skipped_in_number_order(tmp_path, monkeypatch):
    folder, _remote, first, second, _tip, head = squash_world(tmp_path)
    seed = rev(second, "origin/trunk")
    nodes = [
        pr_node(12, "merged", head),
        pr_node(11, "merged", MISSING_HEAD),
        pr_node(9, "merged", seed),
    ]
    stdout = pr_answer(local_names(first, second), {"topic": nodes})
    install_fake_gh(tmp_path, monkeypatch, stdout=stdout)

    topic = branch_of(find_clone(github_collect(folder), second), "topic")

    # #9 has the tip as no ancestor, #11 is held by no clone, and #12 decides.
    assert verdict_facts(topic) == pr_verdict(12)
    assert listed(topic) == [(9, "merged", False), (11, "merged", False), (12, "merged", False)]


@posix_only
def test_commits_after_the_merge_and_a_branch_name_reused_afterwards_are_not_merged(
    tmp_path, monkeypatch
):
    folder, _remote, first, second, _tip, head = squash_world(tmp_path)
    git(second, "checkout", "-q", "topic")
    commit_file(second, "later.txt", "later\n")
    install_fake_gh(tmp_path, monkeypatch, stdout=squash_answer((first, second), head))

    document = github_collect(folder)

    topic = branch_of(find_clone(document, second), "topic")
    assert verdict_facts(topic) == NOT_MERGED_FACTS
    assert listed(topic) == [(12, "merged", False)]
    assert "not_merged" in topic["removable"]["blockers"]

    # A clone fetched after the merge that starts a new topic from the merged trunk.
    third = clone(_remote, folder / "c")
    point_origin_at(third, _remote, GITHUB_URL)
    git(third, "checkout", "-q", "-b", "topic", "trunk")
    commit_file(third, "fresh.txt", "fresh\n")
    install_fake_gh(tmp_path / "again", monkeypatch, stdout=squash_answer((first, second, third), head))
    reused = branch_of(find_clone(github_collect(folder), third), "topic")
    assert git(third, "cat-file", "-e", head, check=False).returncode == 0
    assert verdict_facts(reused) == NOT_MERGED_FACTS
    assert listed(reused) == [(12, "merged", False)]


@posix_only
def test_a_reused_branch_name_after_a_merge_commit_is_not_merged_whichever_way_ancestry_runs(
    tmp_path, monkeypatch
):
    folder, remote, work = small_world(tmp_path)
    git(work, "checkout", "-q", "-b", "topic", "trunk")
    earlier = commit_file(work, "earlier.txt", "earlier\n")
    git(work, "checkout", "-q", "trunk")
    git(work, "merge", "-q", "--no-ff", "-m", "merge topic", "topic")
    git(work, "push", "-q", "origin", "trunk")
    git(work, "branch", "-q", "-D", "topic")
    git(work, "checkout", "-q", "-b", "topic", "trunk")
    commit_file(work, "again.txt", "again\n")
    git(work, "checkout", "-q", "trunk")
    point_origin_at(work, remote, GITHUB_URL)
    # The earlier head is in the history of the base and of the new topic.
    assert git(work, "merge-base", "--is-ancestor", earlier, "topic", check=False).returncode == 0
    install_fake_gh(
        tmp_path, monkeypatch,
        stdout=pr_answer(local_names(work), {"topic": [pr_node(20, "merged", earlier)]}),
    )

    topic = branch_of(find_clone(github_collect(folder), work), "topic")

    assert verdict_facts(topic) == NOT_MERGED_FACTS
    assert listed(topic) == [(20, "merged", False)]


@posix_only
def test_a_head_held_only_by_a_clone_outside_the_folders_leaves_the_git_ladder_to_decide(
    tmp_path, monkeypatch
):
    folder, _remote, first, second, _tip, head = squash_world(tmp_path, first_inside=False)
    install_fake_gh(tmp_path, monkeypatch, stdout=squash_answer((first, second), head))

    topic = branch_of(find_clone(github_collect(folder), second), "topic")

    assert verdict_facts(topic) == NOT_MERGED_FACTS
    assert listed(topic) == [(12, "merged", False)]


def objects_snapshot(repository: Path) -> list[tuple[str, bytes]]:
    root = repository / ".git" / "objects"
    return sorted(
        (item.relative_to(root).as_posix(), item.read_bytes())
        for item in root.rglob("*")
        if item.is_file()
    )


def ladder_probe_log(tmp_path: Path, head: str) -> tuple[Path, Path]:
    """A git stand-in that logs the lazy-fetch and alternates variables of every
    verdict call that names ``head``."""
    log = tmp_path / "head-calls.log"
    body = (
        f'case "$*" in *"--no-replace-objects"*{head}*) '
        f'printf "%s|%s|%s\\n" "$GIT_NO_LAZY_FETCH" "$GIT_ALTERNATE_OBJECT_DIRECTORIES" "$*" '
        f'>> "{log}";; esac'
    )
    return stand_in(tmp_path, "probe", body), log


@pytest.mark.parametrize("trunk_fetched", [False, True], ids=["base-lent", "head-lent"])
@posix_only
def test_every_call_that_looks_for_a_head_runs_without_a_lazy_fetch(
    tmp_path, monkeypatch, trunk_fetched
):
    folder, _remote, first, second, _tip, head = squash_world(tmp_path, trunk_fetched=trunk_fetched)
    install_fake_gh(tmp_path, monkeypatch, stdout=squash_answer((first, second), head))
    shim, log = ladder_probe_log(tmp_path, head)

    with path_with(monkeypatch, shim):
        topic = branch_of(find_clone(github_collect(folder), second), "topic")

    assert verdict_facts(topic) == pr_verdict(12)
    calls = [line.split("|", 2) for line in log.read_text().splitlines()]
    probes = [call for call in calls if "rev-list --no-walk" in call[2]]
    ancestry = [call for call in calls if "merge-base --is-ancestor" in call[2]]
    # The clone alone reads the head through the sibling that lent the base; when
    # the clone holds the base, the first sibling that holds the head is asked and
    # lends its objects to the ancestry test.
    assert len(ancestry) == 1
    assert len(probes) == (2 if trunk_fetched else 1)
    assert {call[0] for call in calls} == {"1"}
    assert ancestry[0][1] == os.path.join(real(first), ".git", "objects")
    assert probes[0][1] == ("" if trunk_fetched else ancestry[0][1])


@posix_only
def test_a_blobless_clone_lacking_the_head_is_left_byte_for_byte_unchanged(
    tmp_path, monkeypatch
):
    folder, _remote, first, second, _tip, head = squash_world(
        tmp_path, first_inside=False, filtered=True
    )
    install_fake_gh(tmp_path, monkeypatch, stdout=squash_answer((first, second), head))
    shim, log = ladder_probe_log(tmp_path, head)
    before = objects_snapshot(second)

    with path_with(monkeypatch, shim):
        document = github_collect(folder)

    topic = branch_of(find_clone(document, second), "topic")
    assert find_clone(document, second)["partial_clone"] is True
    assert verdict_facts(topic) == ("unknown", None, "incomplete_clone", None)
    assert listed(topic) == [(12, "merged", False)]
    assert objects_snapshot(second) == before
    calls = [line.split("|", 2) for line in log.read_text().splitlines()]
    assert calls and {call[0] for call in calls} == {"1"}


ANCESTRY_FAILURES = {
    "failed": ("exit 128", "failed"),
    "late": ("exec sleep 30", "deadline"),
}


@pytest.mark.parametrize("case", list(ANCESTRY_FAILURES))
@posix_only
def test_a_failed_or_late_ancestry_test_fails_the_tip_and_stores_nothing(
    tmp_path, monkeypatch, isolated_git, case
):
    cache = nyx_home(isolated_git)
    action, reason = ANCESTRY_FAILURES[case]
    folder, _remote, first, second, tip, head = squash_world(tmp_path)
    install_fake_gh(tmp_path, monkeypatch, stdout=squash_answer((first, second), head))
    shim = stand_in(
        tmp_path, "ancestry", f'case "$*" in *"merge-base --is-ancestor {tip} {head}"*) {action};; esac'
    )

    with path_with(monkeypatch, shim):
        document = github_collect(folder, call_cap_seconds=1.0)

    topic = branch_of(find_clone(document, second), "topic")
    assert verdict_facts(topic) == ("unknown", None, reason, None)
    assert listed(topic) == [(12, "merged", False)]
    base = rev(second, "origin/trunk")
    assert f"{tip}.{base}" not in (cache_entries(cache) if cache.exists() else {})


PROBE_FAILURES = {
    "in-the-clone": lambda first, second, head: (
        f'case "$*" in *"-C {real(second)} "*"--no-replace-objects"*"rev-list --no-walk"*{head}*) exit 128;; esac'
    ),
    "in-the-sibling": lambda first, second, head: (
        f'case "$*" in *"-C {real(first)} "*"--no-replace-objects"*"rev-list --no-walk"*{head}*) exit 128;; esac'
    ),
}


@pytest.mark.parametrize("case", list(PROBE_FAILURES))
@posix_only
def test_a_probe_for_the_head_that_cannot_say_counts_as_an_unavailable_head(
    tmp_path, monkeypatch, case
):
    folder, _remote, first, second, _tip, head = squash_world(tmp_path, trunk_fetched=True)
    install_fake_gh(tmp_path, monkeypatch, stdout=squash_answer((first, second), head))
    shim = stand_in(tmp_path, "probe-fails", PROBE_FAILURES[case](first, second, head))

    with path_with(monkeypatch, shim):
        document = github_collect(folder)

    topic = branch_of(find_clone(document, second), "topic")
    assert verdict_facts(topic) == NOT_MERGED_FACTS
    assert listed(topic) == [(12, "merged", False)]


# -- the parser ---------------------------------------------------------------


def lookup_live(document: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``document`` whose one repository is a GitHub one with a live lookup."""
    return rederive(
        lookup_document(
            document, github_lookup="on", remote_checks="on", key=GITHUB_KEY,
            pr_lookup=lookup("live", None),
        )
    )


def merged_by_request(branch: dict[str, Any], number: int = 12, listed_on=None) -> None:
    branch["verdict"] = verdict_of("merged", "pull_request", pull_request=number)
    (listed_on or branch)["pull_requests"] = [
        {"number": number, "state": "merged", "head_matches": False}
    ]


def mutate_request_without_a_lookup(document):
    merged_by_request(branch_of(first_clone(document), "never-pushed"))


def mutate_list_without_a_lookup(document):
    branch_of(first_clone(document), "never-pushed")["pull_requests"] = [
        {"number": 3, "state": "open", "head_matches": True}
    ]


def mutate_rung_names_an_unlisted_request(document):
    branch = branch_of(first_clone(document), "never-pushed")
    merged_by_request(branch, 12)
    branch["pull_requests"] = []


def mutate_rung_names_an_open_request(document):
    branch = branch_of(first_clone(document), "never-pushed")
    merged_by_request(branch, 12)
    branch["pull_requests"][0]["state"] = "open"


def mutate_rung_names_another_commits_request(document):
    clone_document = first_clone(document)
    assert branch_of(clone_document, "trunk")["sha"] != branch_of(clone_document, "never-pushed")["sha"]
    merged_by_request(
        branch_of(clone_document, "never-pushed"), 12, listed_on=branch_of(clone_document, "trunk")
    )
    branch_of(clone_document, "never-pushed")["pull_requests"] = []


def mutate_number_with_another_rung(document):
    branch = branch_of(first_clone(document), "never-pushed")
    merged_by_request(branch)
    branch["verdict"]["rung"] = "ancestor"


def mutate_rung_without_a_number(document):
    branch = branch_of(first_clone(document), "never-pushed")
    merged_by_request(branch)
    branch["verdict"]["pull_request"] = None


def mutate_number_on_an_undecided_verdict(document):
    branch_of(first_clone(document), "never-pushed")["verdict"] = verdict_of(
        "unknown", reason="failed", pull_request=12
    )


def mutate_number_that_is_not_positive(value):
    def mutate(document):
        branch = branch_of(first_clone(document), "never-pushed")
        merged_by_request(branch)
        branch["verdict"]["pull_request"] = value

    return mutate


def mutate_detached_rung_at_an_unheld_commit(document):
    clone_document = first_clone(document)
    worktree = detach(document, verdict_of("merged", "pull_request", pull_request=12))
    worktree["head"]["sha"] = "d" * 40
    branch_of(clone_document, "never-pushed")["pull_requests"] = [
        {"number": 12, "state": "merged", "head_matches": False}
    ]


def mutate_list_entry(change):
    def mutate(document):
        branch_of(first_clone(document), "never-pushed")["pull_requests"] = [change]

    return mutate


def mutate_unsorted_list(document):
    branch_of(first_clone(document), "never-pushed")["pull_requests"] = [
        {"number": number, "state": "open", "head_matches": False} for number in (2, 1)
    ]


def mutate_repeated_number(document):
    branch_of(first_clone(document), "never-pushed")["pull_requests"] = [
        {"number": 1, "state": "open", "head_matches": False},
        {"number": 1, "state": "closed", "head_matches": False},
    ]


def mutate_eleven_requests(document):
    branch_of(first_clone(document), "never-pushed")["pull_requests"] = [
        {"number": number, "state": "open", "head_matches": False} for number in range(1, 12)
    ]


def mutate_list_missing(document):
    del branch_of(first_clone(document), "never-pushed")["pull_requests"]


GOOD_ENTRY = {"number": 3, "state": "open", "head_matches": True}
REJECTED_PULL_REQUEST_DOCUMENTS = {
    "rung_without_a_lookup": (False, mutate_request_without_a_lookup),
    "list_without_a_lookup": (False, mutate_list_without_a_lookup),
    "rung_names_an_unlisted_request": (True, mutate_rung_names_an_unlisted_request),
    "rung_names_an_open_request": (True, mutate_rung_names_an_open_request),
    "rung_names_another_commits_request": (True, mutate_rung_names_another_commits_request),
    "number_with_another_rung": (True, mutate_number_with_another_rung),
    "rung_without_a_number": (True, mutate_rung_without_a_number),
    "number_on_an_undecided_verdict": (True, mutate_number_on_an_undecided_verdict),
    "zero_number": (True, mutate_number_that_is_not_positive(0)),
    "boolean_number": (True, mutate_number_that_is_not_positive(True)),
    "text_number": (True, mutate_number_that_is_not_positive("12")),
    "detached_rung_at_an_unheld_commit": (True, mutate_detached_rung_at_an_unheld_commit),
    "unsorted_list": (True, mutate_unsorted_list),
    "repeated_number": (True, mutate_repeated_number),
    "eleven_requests": (True, mutate_eleven_requests),
    "list_missing": (True, mutate_list_missing),
    "foreign_state": (True, mutate_list_entry({**GOOD_ENTRY, "state": "draft"})),
    "text_head_matches": (True, mutate_list_entry({**GOOD_ENTRY, "head_matches": "yes"})),
    "boolean_entry_number": (True, mutate_list_entry({**GOOD_ENTRY, "number": True})),
    "zero_entry_number": (True, mutate_list_entry({**GOOD_ENTRY, "number": 0})),
    "extra_entry_key": (True, mutate_list_entry({**GOOD_ENTRY, "title": "x"})),
    "missing_entry_key": (True, mutate_list_entry({"number": 3, "state": "open"})),
}


@pytest.mark.parametrize("name", list(REJECTED_PULL_REQUEST_DOCUMENTS))
def test_parser_rejects_a_pull_request_violation_under_a_valid_digest(sample, name):
    document, _folder = sample
    lookup_used, mutation = REJECTED_PULL_REQUEST_DOCUMENTS[name]
    base = lookup_live(document) if lookup_used else doc_copy(document)
    assert models.parse_checkouts(redigest(rederive(doc_copy(base))))
    broken = doc_copy(base)
    mutation(broken)
    assert "removable" not in refusal_of(rederive(broken))


def test_parser_accepts_a_pull_request_rung_where_a_branch_at_the_commit_lists_it(sample):
    document, _folder = sample
    own = lookup_live(document)
    merged_by_request(branch_of(first_clone(own), "never-pushed"))
    assert models.parse_checkouts(redigest(rederive(own)))["repositories"][0]["pr_lookup"] == lookup(
        "live", None
    )
    # A detached worktree takes the verdict through a branch at its commit.
    shared = lookup_live(document)
    clone_document = first_clone(shared)
    branch = branch_of(clone_document, "outside")
    branch["pull_requests"] = [{"number": 12, "state": "merged", "head_matches": False}]
    detach(shared, verdict_of("merged", "pull_request", pull_request=12))
    assert models.parse_checkouts(redigest(rederive(shared)))
    # A list of ten is the bound.
    full = lookup_live(document)
    branch_of(first_clone(full), "never-pushed")["pull_requests"] = [
        {"number": number, "state": "closed", "head_matches": False} for number in range(1, 11)
    ]
    assert models.parse_checkouts(redigest(rederive(full)))


@posix_only
def test_the_first_ten_requests_in_answer_order_are_listed_and_the_base_lists_its_own(
    tmp_path, monkeypatch
):
    folder, remote, work = small_world(tmp_path)
    point_origin_at(work, remote, GITHUB_URL)
    git(work, "branch", "many", "trunk")
    trunk_tip = rev(work, "trunk")
    # The answer order is not the number order: the eleventh node has the highest
    # number, so keeping the ten highest numbers would differ from the first ten.
    answered = [5, 40, 3, 22, 17, 9, 31, 12, 2, 28, 50]
    pulls = {
        "many": [pr_node(number, "closed", UNRELATED_HEAD) for number in answered],
        "trunk": [pr_node(1, "merged", trunk_tip, base="release")],
    }
    install_fake_gh(tmp_path, monkeypatch, stdout=pr_answer(local_names(work), pulls))

    item = find_clone(github_collect(folder), work)

    assert [pull["number"] for pull in many_of(item)] == sorted(answered[:10])
    # The verdict base branch lists the requests that name it like any other branch.
    trunk = branch_of(item, "trunk")
    assert trunk["verdict"] is None
    assert listed(trunk) == [(1, "merged", True)]


LATE_PROBES = {
    "in-the-clone": lambda first, second, head: (
        f'case "$*" in *"-C {real(second)} "*"--no-replace-objects"*"rev-list --no-walk"*{head}*) exec sleep 30;; esac'
    ),
    "in-the-sibling": lambda first, second, head: (
        f'case "$*" in *"-C {real(first)} "*"--no-replace-objects"*"rev-list --no-walk"*{head}*) exec sleep 30;; esac'
    ),
}


@pytest.mark.parametrize("case", list(LATE_PROBES))
@posix_only
def test_a_probe_for_the_head_that_times_out_counts_as_an_unavailable_head(
    tmp_path, monkeypatch, case
):
    folder, _remote, first, second, _tip, head = squash_world(tmp_path, trunk_fetched=True)
    install_fake_gh(tmp_path, monkeypatch, stdout=squash_answer((first, second), head))
    shim = stand_in(tmp_path, "probe-late", LATE_PROBES[case](first, second, head))

    with path_with(monkeypatch, shim):
        document = github_collect(folder, call_cap_seconds=1.0)

    topic = branch_of(find_clone(document, second), "topic")
    assert verdict_facts(topic) == NOT_MERGED_FACTS
    assert listed(topic) == [(12, "merged", False)]


@pytest.mark.parametrize("value", [True, 1.0], ids=["boolean", "float"])
def test_parser_rejects_a_rung_number_equal_to_a_listed_one_but_not_an_integer(sample, value):
    document, _folder = sample
    base = lookup_live(document)
    branch = branch_of(first_clone(base), "never-pushed")
    merged_by_request(branch, 1)
    assert models.parse_checkouts(redigest(rederive(doc_copy(base))))
    broken = doc_copy(base)
    branch_of(first_clone(broken), "never-pushed")["verdict"]["pull_request"] = value
    assert "pull_request" in refusal_of(rederive(broken))


@posix_only
def test_the_request_limit_and_the_list_cap_both_come_from_the_parser_constant(
    tmp_path, monkeypatch
):
    cap = models.PULL_REQUEST_LIST_CAP
    folder, remote, work = small_world(tmp_path)
    point_origin_at(work, remote, GITHUB_URL)
    git(work, "branch", "many", "trunk")
    names = local_names(work)
    nodes = [pr_node(number, "closed", UNRELATED_HEAD) for number in range(cap + 5, 0, -1)]
    fake = install_fake_gh(tmp_path, monkeypatch, stdout=pr_answer(names, {"many": nodes}))

    document = github_collect(folder)

    [call] = fake.calls()
    query = json.loads(call.body)["query"]
    assert query.count(f"first: {cap},") == len(names)
    assert query.count("first:") == len(names)
    many = branch_of(find_clone(document, work), "many")["pull_requests"]
    assert [pull["number"] for pull in many] == list(range(6, cap + 6))


def refuse_lending(monkeypatch, *repositories: Path) -> None:
    """Make the lender rule refuse the clones at ``repositories``."""
    refused = {os.path.join(real(repository), ".git") for repository in repositories}
    original = checkouts._lendable
    monkeypatch.setattr(
        checkouts,
        "_lendable",
        lambda context: os.path.realpath(context.common) not in refused and original(context),
    )


@pytest.mark.parametrize("trunk_fetched", [False, True], ids=["base-lent", "head-lent"])
@posix_only
def test_a_clone_the_lender_rule_refuses_lends_to_neither_the_live_apply_nor_the_rung(
    tmp_path, monkeypatch, trunk_fetched
):
    folder, _remote, first, second, _tip, head = squash_world(tmp_path, trunk_fetched=trunk_fetched)
    install_fake_gh(tmp_path, monkeypatch, stdout=squash_answer((first, second), head))
    lent = find_clone(github_collect(folder), second)
    assert verdict_facts(branch_of(lent, "topic")) == pr_verdict(12)

    refuse_lending(monkeypatch, first)
    refused = find_clone(github_collect(folder), second)

    assert verdict_facts(branch_of(refused, "topic")) == NOT_MERGED_FACTS
    assert listed(branch_of(refused, "topic")) == [(12, "merged", False)]
    if not trunk_fetched:
        # The live apply borrows from the same candidates: without one it cannot count.
        assert lent["local_default"]["vs_remote"]["objects_from"] == real(first)
        assert refused["local_default"]["vs_remote"]["objects_from"] is None
        assert refused["local_default"]["vs_remote"]["relation"] == "differs"


@posix_only
def test_the_first_sibling_in_natural_order_lends_to_both_the_live_apply_and_the_rung(
    tmp_path, monkeypatch
):
    folder, remote, _first, second, tip, head = squash_world(
        tmp_path, first_inside=False, trunk_fetched=True
    )
    holders = []
    for name in ("holder 10", "holder 2"):
        holder = clone(remote, folder / name)
        point_origin_at(holder, remote, GITHUB_URL)
        assert git(holder, "cat-file", "-e", head, check=False).returncode == 0
        holders.append(holder)
    later, earlier = holders
    install_fake_gh(tmp_path, monkeypatch, stdout=squash_answer((second, *holders), head))
    log = tmp_path / "head-calls.log"
    shim = stand_in(
        tmp_path,
        "alternates",
        f'case "$*" in *{head}*) '
        f'printf "%s|%s\\n" "$GIT_ALTERNATE_OBJECT_DIRECTORIES" "$*" >> "{log}";; esac',
    )

    with path_with(monkeypatch, shim):
        document = github_collect(folder)

    topic = branch_of(find_clone(document, second), "topic")
    assert topic["sha"] == tip
    # Both siblings hold the head; the one first in natural order lends it, to the
    # live comparison of the branch and to the ancestry test of the rung alike.
    assert topic["vs_remote"]["relation"] == "behind"
    assert topic["vs_remote"]["objects_from"] == real(earlier)
    assert verdict_facts(topic) == pr_verdict(12)
    calls = [line.split("|", 1) for line in log.read_text().splitlines()]
    [ancestry] = [call for call in calls if "merge-base --is-ancestor" in call[1]]
    assert ancestry[0] == os.path.join(real(earlier), ".git", "objects")
    # The search stops at the first holder: the later sibling is never asked.
    assert not [call for call in calls if real(later) in call[1]]
    # Each phase asks the clone itself once, then only its siblings: the clone is
    # never among its own lender candidates.
    places = {real(path): path.name for path in (second, earlier, later)}

    def asked(ladder: bool) -> list[str]:
        return [
            name
            for _alternates, arguments in calls
            if "rev-list --no-walk" in arguments
            and ("--no-replace-objects" in arguments) == ladder
            for path, name in places.items()
            if f"-C {path} " in arguments
        ]

    assert asked(ladder=False) == [second.name, earlier.name]
    assert asked(ladder=True) == [second.name, earlier.name]


# ---------------------------------------------------------------------------
# Saved remote checks in the cache file
# ---------------------------------------------------------------------------


def records_world(tmp_path: Path):
    """A GitHub-identity remote (``hub``, configured as ``GITHUB_URL``) and a
    ``file://`` remote (``other``), each with one clone whose only extra
    branches sit at an ancestor of ``trunk``, so no run decides a verdict the
    cache could store."""
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    hub = make_remote(served, "hub.git")
    other = make_remote(served, "other.git")
    push_commit(hub, "trunk", tmp_path)
    push_commit(other, "trunk", tmp_path)
    first = clone(hub, folder / "a")
    second = clone(other, folder / "b")
    git(first, "branch", "feature/x-1")
    for repository in (first, second):
        git(repository, "branch", "older", "origin/trunk~1")
    point_origin_at(first, hub, GITHUB_URL)
    return folder, hub, other, first, second


def remote_key_of(document: dict[str, Any], path: Path) -> str:
    wanted = real(path)
    [key] = [
        repository["remote"]["key"]
        for repository in document["repositories"]
        if any(item["path"] == wanted for item in repository["clones"])
    ]
    return key


def ls_remote_log(tmp_path: Path, name: str = "heads") -> tuple[Path, Path]:
    """A git stand-in that logs the Unix second and arguments of each ``ls-remote`` start."""
    log = tmp_path / f"{name}.log"
    body = f'case "$*" in *" ls-remote "*) printf "%s %s\\n" "$(date +%s)" "$*" >> "{log}";; esac'
    return stand_in(tmp_path, name, body), log


def ls_remote_start(log: Path, repository: Path) -> int:
    [line] = [
        line for line in log.read_text().splitlines() if f"-C {real(repository)} " in line
    ]
    return int(line.split()[0])


def saved_file(cache: Path) -> dict[str, Any]:
    return json.loads(cache.read_text())


def hub_pulls(first: Path) -> dict[str, list[dict[str, Any]]]:
    return {"feature/x-1": [pr_node(7, "merged", rev(first, "feature/x-1"))]}


def hub_gh(tmp_path: Path, monkeypatch, first: Path, **replay: Any) -> FakeGh:
    stdout = replay.pop("stdout", pr_answer(local_names(first), hub_pulls(first)))
    return install_fake_gh(tmp_path, monkeypatch, stdout=stdout, **replay)


def move_away(remote: Path) -> Path:
    target = remote.with_name(remote.name + ".away")
    remote.rename(target)
    return target


@posix_only
def test_a_networked_collection_saves_each_remotes_answer_under_its_group_key(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, hub, other, first, second = records_world(tmp_path)
    push_commit(hub, "remote-only", tmp_path)
    many = [pr_node(number, "closed", UNRELATED_HEAD) for number in range(100, 87, -1)]
    pulls = {**hub_pulls(first), "older": many}
    fake = hub_gh(tmp_path, monkeypatch, first, stdout=pr_answer(local_names(first), pulls))
    shim, log = ls_remote_log(tmp_path)
    started = int(time.time())

    with path_with(monkeypatch, shim):
        document = github_collect(folder)

    saved = saved_file(cache)
    assert set(saved) == {"schema_version", "verdicts", "remote_checks"}
    assert saved["schema_version"] == 3
    other_key = remote_key_of(document, second)
    assert remote_key_of(document, first) == GITHUB_KEY
    assert other_key.startswith("file:")
    # Keyed by the configured URL's key, never by the file URL it is redirected to.
    assert set(saved["remote_checks"]) == {GITHUB_KEY, other_key}
    expected_branches = {
        line.split()[0]: line.split()[1]
        for line in git(
            hub, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads"
        ).stdout.splitlines()
    }
    assert set(expected_branches) == {"trunk", "remote-only"}
    hub_record = saved["remote_checks"][GITHUB_KEY]
    assert hub_record["heads"] == {
        "at": hub_record["heads"]["at"],
        "default": "trunk",
        "head": rev(hub, "trunk"),
        "branches": expected_branches,
    }
    [call] = fake.calls()
    assert hub_record["heads"]["at"] >= started
    assert hub_record["heads"]["at"] <= ls_remote_start(log, first)
    pull_requests = hub_record["pull_requests"]
    assert started <= pull_requests["at"] <= call.started
    assert set(pull_requests["by_head"]) == set(local_names(first))
    assert pull_requests["by_head"]["feature/x-1"] == [
        {"number": 7, "state": "merged", "head": rev(first, "feature/x-1"), "base": "trunk"}
    ]
    assert pull_requests["by_head"]["trunk"] == []
    # At most ten are kept, the first ten the answer lists, by number.
    assert [pull["number"] for pull in pull_requests["by_head"]["older"]] == list(range(91, 101))
    other_record = saved["remote_checks"][other_key]
    assert other_record["pull_requests"] is None
    assert other_record["heads"]["default"] == "trunk"
    assert other_record["heads"]["head"] == rev(other, "trunk")
    assert other_record["heads"]["branches"] == {"trunk": rev(other, "trunk")}
    assert started <= other_record["heads"]["at"] <= ls_remote_start(log, second)
    assert saved["verdicts"] == {}


@posix_only
def test_a_pull_request_verdict_is_never_stored_though_its_facts_are(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, remote, work = small_world(tmp_path)
    land(work, "done")
    point_origin_at(work, remote, GITHUB_URL)
    pulls = {"done": [pr_node(7, "merged", rev(work, "done"))]}
    install_fake_gh(tmp_path, monkeypatch, stdout=pr_answer(local_names(work), pulls))

    document = github_collect(folder)

    assert verdict_facts(branch_of(find_clone(document, work), "done")) == pr_verdict(7)
    saved = saved_file(cache)
    assert saved["verdicts"] == {}
    assert saved["remote_checks"][GITHUB_KEY]["pull_requests"]["by_head"]["done"] == [
        {"number": 7, "state": "merged", "head": rev(work, "done"), "base": "trunk"}
    ]


@posix_only
def test_a_failed_check_leaves_that_members_saved_record_as_it_was(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, hub, other, first, second = records_world(tmp_path)
    fake = hub_gh(tmp_path, monkeypatch, first, code=1)
    hub_path = hub

    # First run: neither the hub's ls-remote nor gh answers, so the hub has no record.
    away = move_away(hub_path)
    document = github_collect(folder)
    other_key = remote_key_of(document, second)
    assert set(saved_file(cache)["remote_checks"]) == {other_key}
    assert {r["remote"]["check"]["status"] for r in document["repositories"]} == {
        "failed",
        "live",
    }

    away.rename(hub_path)
    fake.replay(stdout=pr_answer(local_names(first), hub_pulls(first)))
    github_collect(folder)
    full = saved_file(cache)["remote_checks"]
    assert set(full) == {GITHUB_KEY, other_key}

    # Only the ls-remote fails: the saved heads stay, the pull requests are renewed.
    time.sleep(1.1)
    away = move_away(hub_path)
    github_collect(folder)
    renewed = saved_file(cache)["remote_checks"]
    assert renewed[GITHUB_KEY]["heads"] == full[GITHUB_KEY]["heads"]
    assert renewed[GITHUB_KEY]["pull_requests"]["at"] > full[GITHUB_KEY]["pull_requests"]["at"]
    assert renewed[other_key]["heads"]["at"] > full[other_key]["heads"]["at"]

    # Only gh fails: the saved pull requests stay, the heads are renewed.
    time.sleep(1.1)
    away.rename(hub_path)
    fake.replay(code=1)
    github_collect(folder)
    last = saved_file(cache)["remote_checks"]
    assert last[GITHUB_KEY]["pull_requests"] == renewed[GITHUB_KEY]["pull_requests"]
    assert last[GITHUB_KEY]["heads"]["at"] > renewed[GITHUB_KEY]["heads"]["at"]


@posix_only
def test_a_networked_run_that_decides_no_verdict_still_rewrites_the_file_with_the_new_checks(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, hub, other, first, second = records_world(tmp_path)
    hub_gh(tmp_path, monkeypatch, first)
    github_collect(folder)
    before = saved_file(cache)
    assert before["verdicts"] == {}
    pushed = push_commit(hub, "trunk", tmp_path)
    time.sleep(1.1)

    github_collect(folder)

    after = saved_file(cache)
    assert after["verdicts"] == {}
    old, new = before["remote_checks"][GITHUB_KEY], after["remote_checks"][GITHUB_KEY]
    assert new["heads"]["head"] == pushed != old["heads"]["head"]
    assert new["heads"]["branches"]["trunk"] == pushed
    assert new["heads"]["at"] > old["heads"]["at"]
    assert new["pull_requests"]["at"] > old["pull_requests"]["at"]


@posix_only
def test_an_offline_run_that_decides_no_verdict_leaves_the_file_untouched(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, hub, other, first, second = records_world(tmp_path)
    fake = hub_gh(tmp_path, monkeypatch, first)
    github_collect(folder)
    before = (cache.read_bytes(), cache.stat().st_mtime_ns)
    time.sleep(1.1)

    document = github_collect(folder, remote_checks=False)

    assert document["remote_checks"] == "off"
    assert len(fake.calls()) == 1
    assert (cache.read_bytes(), cache.stat().st_mtime_ns) == before


@posix_only
def test_a_networked_run_whose_every_check_fails_and_decides_no_verdict_leaves_the_file_untouched(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, hub, other, first, second = records_world(tmp_path)
    fake = hub_gh(tmp_path, monkeypatch, first)
    shim, log = ls_remote_log(tmp_path)
    with path_with(monkeypatch, shim):
        github_collect(folder)
    before = (cache.read_bytes(), cache.stat().st_mtime_ns)
    time.sleep(1.1)
    move_away(hub)
    move_away(other)
    fake.replay(code=1)

    with path_with(monkeypatch, shim):
        document = github_collect(folder)

    # Every check was made and failed; nothing was written, not even a time.
    assert len(log.read_text().splitlines()) == 4
    assert len(fake.calls()) == 2
    assert {r["remote"]["check"]["status"] for r in document["repositories"]} == {"recorded"}
    [hub_group] = [r for r in document["repositories"] if r["remote"]["key"] == GITHUB_KEY]
    assert hub_group["pr_lookup"]["status"] == "recorded"
    assert hub_group["pr_lookup"]["reason"] == "failed"
    assert (cache.read_bytes(), cache.stat().st_mtime_ns) == before


@posix_only
def test_a_new_verdict_is_written_whatever_the_checks_did_and_keeps_the_saved_records(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, hub, other, first, second = records_world(tmp_path)
    fake = hub_gh(tmp_path, monkeypatch, first)
    github_collect(folder)
    saved = saved_file(cache)
    assert saved["verdicts"] == {}
    git(first, "checkout", "-q", "-b", "unmerged", "trunk")
    commit_file(first, "unmerged.txt", "unmerged\n")
    git(first, "checkout", "-q", "trunk")
    key = f"{rev(first, 'unmerged')}.{rev(first, 'origin/trunk')}"

    # Offline: the verdict is written and every saved member keeps its time.
    offline = collect([folder])
    assert verdicts_of(find_clone(offline, first))["unmerged"] == NOT_MERGED
    offline_saved = saved_file(cache)
    assert {k: v["rung"] for k, v in offline_saved["verdicts"].items()} == {key: "none"}
    assert offline_saved["remote_checks"] == saved["remote_checks"]

    # Networked with every check failing: nothing refreshes, the records stay.
    time.sleep(1.1)
    git(first, "checkout", "-q", "-b", "second", "trunk")
    commit_file(first, "second.txt", "second\n")
    git(first, "checkout", "-q", "trunk")
    move_away(hub)
    move_away(other)
    fake.replay(code=1)
    failing = github_collect(folder)
    assert {r["remote"]["check"]["status"] for r in failing["repositories"]} == {"recorded"}
    failed_saved = saved_file(cache)
    assert len(failed_saved["verdicts"]) == 2
    assert failed_saved["remote_checks"] == saved["remote_checks"]


@posix_only
def test_a_file_replaced_during_the_run_keeps_the_newer_member_and_takes_the_runs_other_members(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, hub, other, first, second = records_world(tmp_path)
    fake = hub_gh(tmp_path, monkeypatch, first)
    document = github_collect(folder)
    other_key = remote_key_of(document, second)
    replacement = saved_file(cache)
    future = int(time.time()) + 3600
    record = replacement["remote_checks"][GITHUB_KEY]
    record["heads"]["at"] = future
    record["heads"]["head"] = "d" * 40
    record["pull_requests"] = {
        "at": record["pull_requests"]["at"] - 100,
        "by_head": {
            "feature/x-1": [
                {"number": 99, "state": "merged", "head": "f" * 40, "base": "trunk"}
            ]
        },
    }
    del replacement["remote_checks"][other_key]
    source = tmp_path / "replacement.json"
    source.write_text(json.dumps(replacement))
    staged = cache.with_name("race.tmp")
    shim = stand_in(
        tmp_path,
        "race",
        f'case "$*" in *"-C {real(first)} ls-remote"*) '
        f'cp "{source}" "{staged}" && mv "{staged}" "{cache}";; esac',
    )
    started = int(time.time())

    with path_with(monkeypatch, shim):
        github_collect(folder)

    merged = saved_file(cache)["remote_checks"]
    # The future heads survive, though the run's own answer is older.
    assert merged[GITHUB_KEY]["heads"] == record["heads"]
    # The pull requests are the run's own, not the replaced file's older record.
    own = merged[GITHUB_KEY]["pull_requests"]
    assert own["at"] >= started
    assert own["by_head"]["feature/x-1"] == [
        {"number": 7, "state": "merged", "head": rev(first, "feature/x-1"), "base": "trunk"}
    ]
    # The other remote was absent from the replacement, so the run's heads are written.
    assert merged[other_key]["heads"]["at"] >= started
    assert merged[other_key]["heads"]["head"] == rev(other, "trunk")
    assert len(fake.calls()) == 2


@posix_only
def test_a_file_replaced_after_the_cache_was_loaded_keeps_the_newer_member_and_takes_the_runs_members(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, hub, other, first, second = records_world(tmp_path)
    hub_gh(tmp_path, monkeypatch, first)
    document = github_collect(folder)
    other_key = remote_key_of(document, second)
    replacement = saved_file(cache)
    future = int(time.time()) + 3600
    replacement["remote_checks"][GITHUB_KEY]["heads"]["at"] = future
    replacement["remote_checks"][GITHUB_KEY]["heads"]["head"] = "d" * 40
    del replacement["remote_checks"][other_key]
    source = tmp_path / "replacement.json"
    source.write_text(json.dumps(replacement))
    staged = cache.with_name("race.tmp")
    log = tmp_path / "race.log"
    # Calls that carry the verdict scratch directory belong to the verdict phase,
    # which runs after the cache load.
    shim = stand_in(
        tmp_path,
        "race",
        f'case "$*" in *nyx-verdicts-*) echo "$*" >> "{log}"; '
        f'cp "{source}" "{staged}" && mv "{staged}" "{cache}";; esac',
    )
    started = int(time.time())

    with path_with(monkeypatch, shim):
        github_collect(folder)

    assert log.read_text().strip()
    merged = saved_file(cache)["remote_checks"]
    # The newer member written by the other collection survives this run's write.
    assert merged[GITHUB_KEY]["heads"] == replacement["remote_checks"][GITHUB_KEY]["heads"]
    assert merged[GITHUB_KEY]["pull_requests"]["at"] >= started
    # The remote absent from the replacement gets this run's answer.
    assert merged[other_key]["heads"]["at"] >= started
    assert merged[other_key]["heads"]["head"] == rev(other, "trunk")


def fill_file(cache: Path, count: int, remotes: int, branches: int) -> dict[str, Any]:
    """Write a maximal file: ``count`` verdicts with SHA-256 key pairs, 10-digit
    times and the longest rung name, and ``remotes`` saved remotes of distinct times."""
    keys = {f"{index:064x}.{index + 1:064x}": 1_700_000_000 + index for index in range(count)}
    records = {
        f"example.org/filler/repo-{index:03d}": {
            "heads": {
                "at": 1_600_000_000 + index,
                "default": "main",
                "head": "1" * 40,
                "branches": {f"branch-{index}-{n:04d}": f"{n + 1:040x}" for n in range(branches)},
            },
            "pull_requests": None,
        }
        for index in range(remotes)
    }
    write_cache(cache, keys, rungs=dict.fromkeys(keys, "squash_equivalent"), remote_checks=records)
    return records


def dumped(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def test_a_maximal_file_is_trimmed_to_its_bounds_dropping_the_oldest_records(
    tmp_path, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, hub, other, first, second = records_world(tmp_path)
    fillers = fill_file(cache, checkouts.CACHE_ENTRY_CAP, remotes=95, branches=400)
    assert len(dumped(fillers)) > 2 * 1024 * 1024
    assert cache.stat().st_size < 4 * 1024 * 1024
    started = int(time.time())

    document = collect([folder], remote_checks=True)

    assert document["diagnostics"] == []
    assert cache.stat().st_size <= 4 * 1024 * 1024
    saved = saved_file(cache)
    assert len(saved["verdicts"]) == checkouts.CACHE_ENTRY_CAP
    remote_checks = saved["remote_checks"]
    assert len(dumped(remote_checks)) <= 2 * 1024 * 1024
    # The run's own records are the newest, so they are kept.
    own = set(remote_checks) - set(fillers)
    assert own == {remote_key_of(document, first), remote_key_of(document, second)}
    assert all(remote_checks[key]["heads"]["at"] >= started for key in own)
    kept = {key for key in fillers if key in remote_checks}
    dropped = set(fillers) - kept
    assert kept and dropped
    # Only the oldest were left out, and the file is as full as the bound allows.
    assert max(fillers[key]["heads"]["at"] for key in dropped) < min(
        fillers[key]["heads"]["at"] for key in kept
    )
    newest = max(dropped, key=lambda key: fillers[key]["heads"]["at"])
    assert len(dumped({**remote_checks, newest: fillers[newest]})) > 2 * 1024 * 1024
    again = collect([folder])
    assert again["diagnostics"] == []


def test_the_budget_orders_ties_and_skips_an_oversized_member_without_evicting_older_ones(
    monkeypatch,
):
    def heads(at: int, tag: str) -> dict[str, Any]:
        return {"at": at, "default": "main", "head": tag * 40, "branches": {"main": tag * 40}}

    def pulls(at: int) -> dict[str, Any]:
        return {"at": at, "by_head": {f"topic-{n:03d}": [] for n in range(30)}}

    records = {
        "b": {"heads": {**heads(5, "2"), "branches": {}}, "pull_requests": None},
        "a": {"heads": heads(5, "1"), "pull_requests": pulls(5)},
        "huge": {"heads": {**heads(9, "3"), "branches": {f"n{n}": "3" * 40 for n in range(500)}},
                 "pull_requests": None},
        "old": {"heads": heads(1, "4"), "pull_requests": None},
    }
    none = None

    def only(**kept: Any) -> dict[str, Any]:
        return {
            key: {"heads": members.get("heads", none), "pull_requests": members.get("pull_requests", none)}
            for key, members in kept.items()
        }

    # Room for exactly one tied member: key "a" before "b", heads before pull_requests.
    # (The records of "a" are the larger, so the order also decides what fits.)
    fits_one = len(dumped(only(a={"heads": records["a"]["heads"]})))
    monkeypatch.setattr(checkouts, "REMOTE_CHECKS_BUDGET", fits_one)
    assert checkouts._within_budget(records) == only(a={"heads": records["a"]["heads"]})

    # Room for both of "a" and one more: the third tied member is next, never the older one.
    two = len(dumped(only(a=records["a"])))
    monkeypatch.setattr(checkouts, "REMOTE_CHECKS_BUDGET", two)
    assert checkouts._within_budget(records) == only(a=records["a"])
    three = len(dumped(only(a=records["a"], b={"heads": records["b"]["heads"]})))
    monkeypatch.setattr(checkouts, "REMOTE_CHECKS_BUDGET", three)
    assert checkouts._within_budget(records) == only(
        a=records["a"], b={"heads": records["b"]["heads"]}
    )
    # The oversized, newest member is left out; older members after it are still kept.
    four = len(dumped(only(a=records["a"], b={"heads": records["b"]["heads"]},
                           old={"heads": records["old"]["heads"]})))
    monkeypatch.setattr(checkouts, "REMOTE_CHECKS_BUDGET", four)
    assert checkouts._within_budget(records) == only(
        a=records["a"], b={"heads": records["b"]["heads"]}, old={"heads": records["old"]["heads"]}
    )


def test_the_budget_prefers_heads_to_a_smaller_pull_requests_record_of_the_same_second(
    monkeypatch,
):
    heads = {
        "at": 7,
        "default": "main",
        "head": "1" * 40,
        "branches": {f"branch-{n:02d}": "1" * 40 for n in range(8)},
    }
    pulls = {"at": 7, "by_head": {"topic": []}}
    assert len(dumped(pulls)) < len(dumped(heads))
    records = {"a": {"heads": heads, "pull_requests": pulls}}
    alone_heads = len(dumped({"a": {"heads": heads, "pull_requests": None}}))
    alone_pulls = len(dumped({"a": {"heads": None, "pull_requests": pulls}}))
    both = len(dumped(records))
    # Each member fits alone, the two do not fit together.
    assert max(alone_heads, alone_pulls) < both
    monkeypatch.setattr(checkouts, "REMOTE_CHECKS_BUDGET", alone_heads)

    assert checkouts._within_budget(records) == {"a": {"heads": heads, "pull_requests": None}}


def test_the_budget_keeps_the_first_remote_key_of_equal_tied_members(monkeypatch):
    def heads(tag: str) -> dict[str, Any]:
        return {"at": 7, "default": "main", "head": tag * 40, "branches": {"main": tag * 40}}

    records = {
        "b": {"heads": heads("2"), "pull_requests": None},
        "a": {"heads": heads("1"), "pull_requests": None},
    }
    monkeypatch.setattr(
        checkouts, "REMOTE_CHECKS_BUDGET", len(dumped({"a": records["a"]}))
    )

    assert checkouts._within_budget(records) == {"a": records["a"]}


@posix_only
def test_a_remote_whose_answer_alone_exceeds_the_budget_keeps_no_heads_record(
    tmp_path, isolated_git
):
    cache = nyx_home(isolated_git)
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    work = clone(remote, folder / "work")
    head = rev(work, "HEAD")
    names = {
        f"refs/heads/{index:05d}-" + "x" * 200: head for index in range(11_000)
    }
    add_packed_refs(remote, names)
    offline_before = collect([folder])
    assert not cache.exists()

    networked = collect([folder], remote_checks=True)

    assert networked["repositories"][0]["remote"]["check"]["status"] == "live"
    assert networked["diagnostics"] == []
    # The answer was successful, so the file is written, but it holds no heads.
    assert saved_file(cache)["remote_checks"] == {}
    offline_after = collect([folder])
    assert offline_after["diagnostics"] == []
    assert stable(offline_after) == stable(offline_before)


def without_group(document: dict[str, Any], key: str) -> dict[str, Any]:
    return {
        **document,
        "repositories": [
            group
            for group in document["repositories"]
            if group["remote"] is None or group["remote"]["key"] != key
        ],
    }


def valid_saved_file() -> dict[str, Any]:
    head = "a" * 40
    return {
        "schema_version": 3,
        "verdicts": {},
        "remote_checks": {
            GITHUB_KEY: {
                "heads": {"at": 5, "default": "trunk", "head": head, "branches": {"trunk": head}},
                "pull_requests": {
                    "at": 6,
                    "by_head": {
                        "topic": [{"number": 1, "state": "merged", "head": head, "base": "trunk"}]
                    },
                },
            }
        },
    }


def _mutated(change):
    document = valid_saved_file()
    change(document)
    return document


def _entry(document: dict[str, Any]) -> dict[str, Any]:
    return document["remote_checks"][GITHUB_KEY]


SAVED_FILE_VARIANTS = {
    "extra_top_level_key": lambda d: d.update(extra=1),
    "no_remote_checks": lambda d: d.pop("remote_checks"),
    "schema_4": lambda d: d.update(schema_version=4),
    "negative_time": lambda d: _entry(d)["heads"].update(at=-1),
    "negative_pull_request_time": lambda d: _entry(d)["pull_requests"].update(at=-1),
    "unsafe_branch_name": lambda d: _entry(d)["heads"]["branches"].update({"bad\nname": "a" * 40}),
    "unsafe_by_head_name": lambda d: _entry(d)["pull_requests"]["by_head"].update({"": []}),
    "eleven_pull_requests": lambda d: _entry(d)["pull_requests"]["by_head"].update(
        topic=[
            {"number": n, "state": "open", "head": "a" * 40, "base": "trunk"}
            for n in range(1, 12)
        ]
    ),
    "draft_state": lambda d: _entry(d)["pull_requests"]["by_head"]["topic"][0].update(
        state="draft"
    ),
    "no_member": lambda d: _entry(d).update(heads=None, pull_requests=None),
    "retired_keys_at_schema_3": lambda d: d.pop("remote_checks"),
}


@pytest.mark.parametrize("kind", ["valid", *SAVED_FILE_VARIANTS])
def test_a_schema_3_file_with_an_invalid_form_is_unreadable_and_read_as_empty(
    tmp_path, isolated_git, kind
):
    cache = nyx_home(isolated_git)
    folder, hub, other, first, second = records_world(tmp_path)
    cold = collect([folder])
    document = valid_saved_file()
    if kind != "valid":
        SAVED_FILE_VARIANTS[kind](document)
    cache.parent.mkdir(exist_ok=True)
    cache.write_text(json.dumps(document))

    result = collect([folder])

    if kind == "valid":
        assert result["diagnostics"] == cold["diagnostics"] == []
        # The file is read; its saved checks stand in for the checks an offline run
        # does not make, for the one remote it holds, and for no other.
        [saved_group] = [r for r in result["repositories"] if r["remote"]["key"] == GITHUB_KEY]
        assert saved_group["remote"]["check"] == {
            "status": "recorded",
            "reason": "remote_checks_off",
            "checked_at": "1970-01-01T00:00:05Z",
        }
        assert stable(without_group(result, GITHUB_KEY)) == stable(
            without_group(cold, GITHUB_KEY)
        )
        return
    assert stable(result) == stable(cold)
    assert result["diagnostics"] == [
        *cold["diagnostics"],
        {"code": "checkout_cache_unreadable", "path": str(cache)},
    ]


# ---------------------------------------------------------------------------
# Using the saved remote checks
# ---------------------------------------------------------------------------


def utc_text(seconds: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def as_recorded(value: Any) -> Any:
    """A document fragment with every ``live`` basis named ``recorded``."""
    if isinstance(value, dict):
        return {
            key: "recorded" if key == "basis" and item == "live" else as_recorded(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [as_recorded(item) for item in value]
    return value


def remote_bases(item: dict[str, Any]) -> set[str]:
    """The basis of every remote-derived comparison and of the verdict base of a clone."""
    found = {branch["vs_remote"]["basis"] for branch in item["branches"]}
    found.add(item["cached_vs_remote"]["basis"])
    found.add(item["local_default"]["vs_remote"]["basis"])
    found.add(item["verdict_base"]["basis"])
    return found


def group_with_key(document: dict[str, Any], key: str) -> dict[str, Any]:
    [group] = [item for item in document["repositories"] if item["remote"]["key"] == key]
    return group


class SavedWorld:
    """A GitHub-identity remote with two clones, a local-only branch, a merged pull
    request, a remote that moved ahead of one clone, and the networked run that
    saved its checks."""

    def __init__(self, tmp_path: Path, monkeypatch, isolated_git: Path) -> None:
        self.tmp_path = tmp_path
        self.cache = nyx_home(isolated_git)
        self.folder, self.remote, self.work = landed_world(tmp_path)
        git(self.work, "branch", "wip", "trunk")
        self.second = clone(self.remote, self.folder / "second")
        point_origin_at(self.second, self.remote, GITHUB_URL)
        self.head = push_commit(self.remote, "trunk", tmp_path)
        git(self.second, "fetch", "-q", "origin")
        self.names = local_names(self.work, self.second)
        self.answer = pr_answer(
            self.names, {"done": [pr_node(7, "merged", rev(self.work, "done"))]}
        )
        self.fake = install_fake_gh(tmp_path, monkeypatch, stdout=self.answer)
        self.markers = [guard_remote(path, tmp_path) for path in (self.work, self.second)]
        self.shim, self.log = ls_remote_log(tmp_path)
        with path_with(monkeypatch, self.shim):
            self.live = github_collect(self.folder)
        self.saved = saved_file(self.cache)["remote_checks"][GITHUB_KEY]

    def contacts(self) -> bool:
        """Whether any upload-pack of the remote ran since the markers were last cleared."""
        found = any(marker.exists() for marker in self.markers)
        for marker in self.markers:
            marker.unlink(missing_ok=True)
        return found

    def ls_remotes(self) -> int:
        return len(self.log.read_text().splitlines()) if self.log.exists() else 0


@posix_only
def test_an_offline_collection_answers_from_the_saved_checks_as_the_networked_run_did(
    tmp_path, monkeypatch, isolated_git
):
    world = SavedWorld(tmp_path, monkeypatch, isolated_git)
    live_group = world.live["repositories"][0]
    live_work = find_clone(world.live, world.work)
    # Control: the networked run contacted the remote and compared on live objects.
    assert world.contacts()
    assert live_work["local_default"]["vs_remote"] == {
        "basis": "live",
        "relation": "behind",
        "ahead": 0,
        "behind": 1,
        "objects_from": real(world.second),
    }
    assert verdict_facts(branch_of(live_work, "done")) == pr_verdict(7)
    contacts, gh_calls = world.ls_remotes(), len(world.fake.calls())

    with path_with(monkeypatch, world.shim):
        offline = collect([world.folder], github_lookup=True)

    assert offline["remote_checks"] == "off"
    [group] = offline["repositories"]
    assert group["remote"]["check"] == {
        "status": "recorded",
        "reason": "remote_checks_off",
        "checked_at": utc_text(world.saved["heads"]["at"]),
    }
    assert TIMESTAMP.fullmatch(group["remote"]["check"]["checked_at"])
    assert group["default_branch"] == {"name": "trunk", "sha": world.head}
    assert group["default_branch"] == live_group["default_branch"]
    assert group["pr_lookup"] == {
        "status": "recorded",
        "reason": "remote_checks_off",
        "checked_at": utc_text(world.saved["pull_requests"]["at"]),
    }
    # The remote saw nothing and no tool was started.
    assert not world.contacts()
    assert world.ls_remotes() == contacts
    assert len(world.fake.calls()) == gh_calls
    for path in (world.work, world.second):
        item = find_clone(offline, path)
        assert remote_bases(item) == {"recorded"}
        assert item["local_default"]["vs_cached"]["basis"] == "cached"
        # The same relations, counts, lenders, pull requests, verdicts and base.
        assert item == as_recorded(find_clone(world.live, path))
    # A verdict decided from saved pull-request facts is not remembered.
    stored = cache_entries(world.cache)
    assert f"{rev(world.work, 'done')}.{world.head}" not in stored
    assert set(stored.values()) <= checkouts.CACHE_RUNGS
    work = find_clone(offline, world.work)
    assert branch_of(work, "wip")["vs_remote"]["relation"] == "absent"
    assert verdict_facts(branch_of(work, "done")) == pr_verdict(7)
    assert [pull["number"] for pull in branch_of(work, "done")["pull_requests"]] == [7]
    assert work["verdict_base"]["objects_from"] == real(world.second)


@posix_only
def test_equal_collections_have_equal_digests_and_a_live_check_carries_no_time(
    tmp_path, monkeypatch, isolated_git
):
    world = SavedWorld(tmp_path, monkeypatch, isolated_git)
    first = collect([world.folder], github_lookup=True)
    time.sleep(1.1)
    second = collect([world.folder], github_lookup=True)
    assert first["checkouts_digest"] == second["checkouts_digest"]

    time.sleep(1.1)
    again = github_collect(world.folder)

    assert again["checkouts_digest"] == world.live["checkouts_digest"]
    [group] = again["repositories"]
    assert group["remote"]["check"] == {"status": "live", "reason": None}
    assert group["pr_lookup"] == {"status": "live", "reason": None, "checked_at": None}
    # The networked run renewed both saved checks, so the next offline run reads newer times.
    renewed = saved_file(world.cache)["remote_checks"][GITHUB_KEY]
    assert renewed["heads"]["at"] > world.saved["heads"]["at"]
    later = collect([world.folder], github_lookup=True)
    assert later["repositories"][0]["remote"]["check"]["checked_at"] == utc_text(
        renewed["heads"]["at"]
    )
    assert later["checkouts_digest"] != first["checkouts_digest"]


@posix_only
def test_a_networked_collection_whose_remote_moved_away_reports_the_saved_check(
    tmp_path, monkeypatch, isolated_git
):
    world = SavedWorld(tmp_path, monkeypatch, isolated_git)
    move_away(world.remote)

    document = github_collect(world.folder)

    [group] = document["repositories"]
    assert group["remote"]["check"] == {
        "status": "recorded",
        "reason": "not_found",
        "checked_at": utc_text(world.saved["heads"]["at"]),
    }
    assert group["default_branch"] == {"name": "trunk", "sha": world.head}
    # The pull-request lookup was live in the same run.
    assert group["pr_lookup"] == {"status": "live", "reason": None, "checked_at": None}
    for path in (world.work, world.second):
        assert find_clone(document, path) == as_recorded(find_clone(world.live, path))
    # A failed check wrote no time of its own.
    assert saved_file(world.cache)["remote_checks"][GITHUB_KEY]["heads"] == world.saved["heads"]


GH_FAILURES = [
    ("not_authenticated", {"code": 4}),
    ("rate_limited", {"code": 1, "stderr": "API rate limit exceeded"}),
    ("not_found", {"code": 0, "stdout": NULL_REPOSITORY}),
    ("failed", {"code": 0, "stdout": "this is not json"}),
    ("timed_out", {"code": 0, "delay": 30}),
    ("gh_missing", None),
]


@posix_only
@pytest.mark.parametrize(("reason", "replay"), GH_FAILURES, ids=[case[0] for case in GH_FAILURES])
def test_a_failed_pull_request_lookup_is_answered_from_the_saved_facts(
    tmp_path, monkeypatch, isolated_git, reason, replay
):
    world = SavedWorld(tmp_path, monkeypatch, isolated_git)
    options: dict[str, Any] = {}
    if replay is None:
        monkeypatch.setenv("PATH", git_only_path(tmp_path))
    else:
        world.fake.replay(**replay)
    if reason == "timed_out":
        options["call_cap_seconds"] = 2
    calls = len(world.fake.calls())

    document = github_collect(world.folder, **options)

    assert len(world.fake.calls()) == calls + (0 if replay is None else 1)
    [group] = document["repositories"]
    assert group["pr_lookup"] == {
        "status": "recorded",
        "reason": reason,
        "checked_at": utc_text(world.saved["pull_requests"]["at"]),
    }
    assert group["remote"]["check"] == {"status": "live", "reason": None}
    work = find_clone(document, world.work)
    assert verdict_facts(branch_of(work, "done")) == pr_verdict(7)
    assert find_clone(document, world.work) == find_clone(world.live, world.work)
    assert saved_file(world.cache)["remote_checks"][GITHUB_KEY]["pull_requests"] == (
        world.saved["pull_requests"]
    )


@posix_only
@pytest.mark.parametrize("options", [{}, {"remote_checks": True}], ids=["offline", "networked"])
def test_a_collection_with_the_lookup_off_never_uses_the_saved_pull_requests(
    tmp_path, monkeypatch, isolated_git, options
):
    world = SavedWorld(tmp_path, monkeypatch, isolated_git)
    assert verdict_facts(branch_of(find_clone(world.live, world.work), "done")) == pr_verdict(7)

    document = collect([world.folder], github_lookup=False, **options)

    [group] = document["repositories"]
    assert group["pr_lookup"] == {"status": "off", "reason": "setting_off", "checked_at": None}
    for item in all_clones(document):
        assert all(branch["pull_requests"] == [] for branch in item["branches"])
        rungs = [branch["verdict"]["pull_request"] for branch in item["branches"] if branch["verdict"]]
        assert rungs == [None] * len(rungs)
    work = find_clone(document, world.work)
    assert verdict_facts(branch_of(work, "done")) == ("merged", "squash_equivalent", None, None)


@posix_only
def test_a_saved_pull_request_listing_is_never_used_for_a_remote_that_is_not_github(
    tmp_path, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, _remote, work = small_world(tmp_path)
    land(work, "done")
    collect([folder], remote_checks=True, github_lookup=True)
    saved = saved_file(cache)
    [key] = saved["remote_checks"]
    saved["remote_checks"][key]["pull_requests"] = {
        "at": 5,
        "by_head": {
            "done": [{"number": 7, "state": "merged", "head": rev(work, "done"), "base": "trunk"}]
        },
    }
    cache.write_text(json.dumps(saved))

    document = collect([folder], github_lookup=True)

    [group] = document["repositories"]
    assert group["remote"]["check"]["status"] == "recorded"
    assert group["pr_lookup"] == {"status": "off", "reason": "not_github", "checked_at": None}
    item = find_clone(document, work)
    assert branch_of(item, "done")["pull_requests"] == []
    assert verdict_facts(branch_of(item, "done")) == ("merged", "squash_equivalent", None, None)


@posix_only
def test_a_branch_created_after_the_saved_check_is_absent_with_no_pull_requests(
    tmp_path, monkeypatch, isolated_git
):
    world = SavedWorld(tmp_path, monkeypatch, isolated_git)
    git(world.work, "branch", "late", "trunk")

    document = collect([world.folder], github_lookup=True)

    late = branch_of(find_clone(document, world.work), "late")
    assert late["vs_remote"] == {
        "basis": "recorded",
        "relation": "absent",
        "ahead": None,
        "behind": None,
        "objects_from": None,
    }
    assert late["pull_requests"] == []
    # The saved listing still serves the branches it names.
    done = branch_of(find_clone(document, world.work), "done")
    assert [pull["number"] for pull in done["pull_requests"]] == [7]


@posix_only
def test_the_saved_check_of_an_empty_remote_leaves_no_default_and_every_branch_absent(
    tmp_path, isolated_git
):
    nyx_home(isolated_git)
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    empty = served / "empty.git"
    git(served, "init", "-q", "--bare", str(empty))
    work = clone(empty, folder / "work")
    commit_file(work, "file.txt", "one\n")
    git(work, "branch", "other")
    collect([folder], remote_checks=True)

    document = collect([folder])

    [group] = document["repositories"]
    assert group["remote"]["check"]["status"] == "recorded"
    assert group["default_branch"] == {"name": None, "sha": None}
    item = find_clone(document, work)
    assert item["local_default"] is None
    assert item["cached_vs_remote"]["relation"] == "absent"
    assert item["cached_vs_remote"]["basis"] == "recorded"
    assert [
        (branch["vs_remote"]["relation"], branch["vs_remote"]["basis"])
        for branch in item["branches"]
    ] == [("absent", "recorded")] * 2


@posix_only
@pytest.mark.parametrize("kind", ["no-home", "empty-file"])
def test_with_no_saved_check_an_offline_collection_reports_the_cached_state(
    tmp_path, isolated_git, kind
):
    folder, _remote, work = landed_world(tmp_path)
    if kind == "empty-file":
        write_cache(nyx_home(isolated_git), {})
    else:
        assert not (isolated_git / ".nyx").exists()

    document = collect([folder], github_lookup=True)

    assert document["diagnostics"] == []
    [group] = document["repositories"]
    assert group["remote"]["check"] == {"status": "off", "reason": "remote_checks_off"}
    assert group["pr_lookup"] == {"status": "off", "reason": "remote_checks_off", "checked_at": None}
    assert group["default_branch"] == {"name": "trunk", "sha": None}
    item = find_clone(document, work)
    assert {comparison["basis"] for comparison in comparisons_of(item)} == {"cached"}
    assert item["verdict_base"]["basis"] == "cached"


@posix_only
def test_a_recorded_comparison_borrows_the_saved_head_from_a_sibling_clone_and_names_it(
    tmp_path, isolated_git
):
    nyx_home(isolated_git)
    folder, remote, lagging = small_world(tmp_path)
    lender = clone(remote, folder / "a-lender")
    head = push_commit(remote, "trunk", tmp_path)
    git(lender, "fetch", "-q", "origin")
    markers = [guard_remote(path, tmp_path) for path in (lagging, lender)]
    collect([folder], remote_checks=True)
    assert any(marker.exists() for marker in markers), "control: the networked run made contact"
    for marker in markers:
        marker.unlink(missing_ok=True)

    document = collect([folder])

    item = find_clone(document, lagging)
    assert item["local_default"]["vs_remote"] == {
        "basis": "recorded",
        "relation": "behind",
        "ahead": 0,
        "behind": 1,
        "objects_from": real(lender),
    }
    assert item["verdict_base"] == {
        "name": "trunk",
        "sha": head,
        "basis": "recorded",
        "objects_from": real(lender),
    }
    assert not any(marker.exists() for marker in markers)


@posix_only
def test_a_recorded_check_leaves_shallow_and_partial_clones_to_differ_without_object_calls(
    tmp_path, monkeypatch, isolated_git
):
    nyx_home(isolated_git)
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    allow_filters(remote)
    push_commit(remote, "trunk", tmp_path)
    full = clone(remote.as_uri(), folder / "a-full")
    partial = clone(remote.as_uri(), folder / "b-partial", "--filter=blob:none")
    shallow = clone(remote.as_uri(), folder / "c-shallow", "--depth=1")
    push_commit(remote, "trunk", tmp_path)
    collect([folder], remote_checks=True)
    shim, log = recording(tmp_path, "object-calls")

    with path_with(monkeypatch, shim):
        document = collect([folder])

    def object_calls(path: Path) -> list[str]:
        return [
            line
            for line in log.read_text().splitlines()
            if f"-C {real(path)} " in line and ("--ignore-missing" in line or "--left-right" in line)
        ]

    # Control: a complete clone does look for the saved head.
    assert object_calls(full)
    for path in (partial, shallow):
        assert object_calls(path) == []
        item = find_clone(document, path)
        for comparison in (
            item["cached_vs_remote"],
            item["local_default"]["vs_remote"],
            branch_of(item, "trunk")["vs_remote"],
        ):
            assert comparison == {
                "basis": "recorded",
                "relation": "differs",
                "ahead": None,
                "behind": None,
                "objects_from": None,
            }


@posix_only
def test_a_recorded_count_call_that_times_out_is_unknown_not_a_difference(
    tmp_path, monkeypatch, isolated_git
):
    nyx_home(isolated_git)
    folder, remote, work = small_world(tmp_path)
    git(work, "checkout", "-q", "-b", "feature", "trunk")
    commit_file(work, "feature.txt", "local\n")
    git(work, "checkout", "-q", "trunk")
    push_commit(remote, "feature", tmp_path)
    # The clone holds the saved commit but has no remote-tracking ref for it, so the
    # recorded comparison is the only count in the run.
    git(work, "fetch", "-q", "origin", "feature")
    git(work, "update-ref", "-d", "refs/remotes/origin/feature")
    collect([folder], remote_checks=True)
    control = branch_of(find_clone(collect([folder]), work), "feature")["vs_remote"]
    assert (control["basis"], control["relation"]) == ("recorded", "diverged")
    shim = stand_in(tmp_path, "slow-count", 'case "$*" in *"--left-right --count"*) sleep 30;; esac')

    with path_with(monkeypatch, shim):
        document = collect([folder], call_cap_seconds=2)

    comparison = branch_of(find_clone(document, work), "feature")["vs_remote"]
    assert comparison == {
        "basis": "recorded",
        "relation": "unknown",
        "ahead": None,
        "behind": None,
        "objects_from": None,
    }


@pytest.mark.parametrize("basis", ["live", "recorded"])
def test_a_count_call_that_never_ran_is_unknown_on_every_remote_basis(basis):
    never = checkouts._Outcome(None)

    assert checkouts._counted_comparison(never, basis) == {
        "basis": basis,
        "relation": "unknown",
        "ahead": None,
        "behind": None,
        "objects_from": None,
    }
    assert checkouts._counted_comparison(never, "cached")["relation"] == "differs"


@posix_only
def test_a_networked_run_that_decides_no_verdict_moves_the_head_an_offline_run_reports(
    tmp_path, monkeypatch, isolated_git
):
    cache = nyx_home(isolated_git)
    folder, hub, other, first, second = records_world(tmp_path)
    hub_gh(tmp_path, monkeypatch, first)
    github_collect(folder)
    first_time = utc_text(saved_file(cache)["remote_checks"][GITHUB_KEY]["heads"]["at"])
    time.sleep(1.1)
    # A clone outside the folders moves the remote's trunk.
    pushed = push_commit(hub, "trunk", tmp_path)
    github_collect(folder)
    after = saved_file(cache)
    assert after["verdicts"] == {}, "control: the run decided no verdict to store"
    saved_bytes = cache.read_bytes()

    document = collect([folder], github_lookup=True)

    group = group_with_key(document, GITHUB_KEY)
    assert group["default_branch"] == {"name": "trunk", "sha": pushed}
    checked_at = group["remote"]["check"]["checked_at"]
    assert checked_at == utc_text(after["remote_checks"][GITHUB_KEY]["heads"]["at"])
    assert checked_at > first_time
    assert cache.read_bytes() == saved_bytes


@posix_only
def test_each_saved_check_reports_the_time_of_its_own_record(tmp_path, monkeypatch, isolated_git):
    world = SavedWorld(tmp_path, monkeypatch, isolated_git)
    saved = saved_file(world.cache)
    heads_at = saved["remote_checks"][GITHUB_KEY]["heads"]["at"]
    pulls_at = heads_at - 7200
    saved["remote_checks"][GITHUB_KEY]["pull_requests"]["at"] = pulls_at
    world.cache.write_text(json.dumps(saved))
    assert utc_text(pulls_at) != utc_text(heads_at)

    offline = collect([world.folder], github_lookup=True)

    [group] = offline["repositories"]
    assert group["remote"]["check"]["checked_at"] == utc_text(heads_at)
    assert group["pr_lookup"]["checked_at"] == utc_text(pulls_at)


def recorded_comparison(relation: str) -> dict[str, Any]:
    return {
        "basis": "recorded",
        "relation": relation,
        "ahead": None,
        "behind": None,
        "objects_from": None,
    }


def cached_base(path: Path) -> dict[str, Any]:
    """The verdict base a clone keeps when the saved head cannot be reached: its own
    cached default, on the cached basis."""
    return {
        "name": "trunk",
        "sha": rev(path, "refs/remotes/origin/trunk"),
        "basis": "cached",
        "objects_from": None,
    }


@posix_only
def test_a_recorded_head_that_no_sibling_holds_differs_on_the_recorded_basis(
    tmp_path, isolated_git
):
    nyx_home(isolated_git)
    folder, remote, work = small_world(tmp_path)
    other = clone(remote, folder / "other")
    push_commit(remote, "trunk", tmp_path)
    collect([folder], remote_checks=True)

    document = collect([folder])

    for path in (work, other):
        item = find_clone(document, path)
        assert item["cached_vs_remote"] == recorded_comparison("differs")
        assert item["local_default"]["vs_remote"] == recorded_comparison("differs")
        assert branch_of(item, "trunk")["vs_remote"] == recorded_comparison("differs")
        assert item["verdict_base"] == cached_base(path)


RECORDED_PROBE_FAILURES = [
    ("own", "differs", "exit 3"),
    ("own", "unknown", "sleep 30"),
    ("lender", "differs", "exit 3"),
    ("lender", "unknown", "sleep 30"),
]


@posix_only
@pytest.mark.parametrize(
    ("whose", "state", "action"), RECORDED_PROBE_FAILURES, ids=[f"{w}-{s}" for w, s, _ in RECORDED_PROBE_FAILURES]
)
def test_a_recorded_comparison_whose_object_probe_cannot_say_keeps_that_state_on_the_recorded_basis(
    tmp_path, monkeypatch, isolated_git, whose, state, action
):
    nyx_home(isolated_git)
    folder, remote, lagging = small_world(tmp_path)
    lender = clone(remote, folder / "a-lender")
    push_commit(remote, "trunk", tmp_path)
    git(lender, "fetch", "-q", "origin")
    collect([folder], remote_checks=True)
    probed = lagging if whose == "own" else lender
    shim = stand_in(
        tmp_path,
        "failing-probe",
        f'case "$*" in *"-C {real(probed)} "*"--ignore-missing"*) {action};; esac',
    )

    with path_with(monkeypatch, shim):
        document = collect([folder], call_cap_seconds=2)

    item = find_clone(document, lagging)
    assert item["cached_vs_remote"] == recorded_comparison(state)
    assert item["local_default"]["vs_remote"] == recorded_comparison(state)
    assert branch_of(item, "trunk")["vs_remote"] == recorded_comparison(state)
    assert item["verdict_base"] == cached_base(lagging)


@posix_only
def test_a_guarded_clone_takes_the_recorded_basis_for_its_base_only_when_its_cached_ref_is_the_saved_head(
    tmp_path, isolated_git
):
    nyx_home(isolated_git)
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    allow_filters(remote)
    push_commit(remote, "trunk", tmp_path)
    stale = clone(remote.as_uri(), folder / "a-stale", "--depth=1")
    head = push_commit(remote, "trunk", tmp_path)
    shallow = clone(remote.as_uri(), folder / "b-shallow", "--depth=1")
    partial = clone(remote.as_uri(), folder / "c-partial", "--filter=blob:none")
    collect([folder], remote_checks=True)

    document = collect([folder])

    for path in (shallow, partial):
        assert find_clone(document, path)["verdict_base"] == {
            "name": "trunk",
            "sha": head,
            "basis": "recorded",
            "objects_from": None,
        }
    # A guarded clone behind the saved head keeps its cached base.
    item = find_clone(document, stale)
    assert item["verdict_base"] == cached_base(stale)
    assert item["verdict_base"]["sha"] != head


@posix_only
def test_the_verdict_base_is_named_by_the_saved_default_not_by_the_clones_cached_one(
    tmp_path, isolated_git
):
    nyx_home(isolated_git)
    folder, remote, work = small_world(tmp_path)
    push_commit(remote, "other", tmp_path)
    git(work, "fetch", "-q", "origin")
    git(work, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/other")
    collect([folder], remote_checks=True)

    document = collect([folder])

    item = find_clone(document, work)
    assert item["cached_default"]["name"] == "other", "control: the clone caches another default"
    [group] = document["repositories"]
    assert group["default_branch"]["name"] == "trunk"
    assert item["verdict_base"] == {
        "name": "trunk",
        "sha": group["default_branch"]["sha"],
        "basis": "recorded",
        "objects_from": None,
    }


RECORDED_TIME = "2026-10-24T00:00:00Z"
CHECK_FAILURE_REASONS = ["unreachable", "not_found", "authentication_failed", "timed_out", "failed"]


def with_check(document: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    broken = doc_copy(document)
    broken["repositories"][0]["remote"]["check"] = check
    return broken


@pytest.mark.parametrize("reason", ["remote_checks_off", *CHECK_FAILURE_REASONS])
def test_parser_accepts_a_recorded_remote_check_with_a_time_and_either_kind_of_reason(
    sample, reason
):
    document, _folder = sample
    check = {"status": "recorded", "reason": reason, "checked_at": RECORDED_TIME}

    parsed = models.parse_checkouts(redigest(with_check(document, check)))

    assert parsed["repositories"][0]["remote"]["check"] == check


@pytest.mark.parametrize(
    "checked_at", [None, "", "yesterday", "2026-13-45T25:61:61Z", "2026-10-24T00:00:00", 5]
)
def test_parser_refuses_a_recorded_remote_check_without_a_valid_time(sample, checked_at):
    document, _folder = sample
    check = {"status": "recorded", "reason": "remote_checks_off", "checked_at": checked_at}

    message = refusal_of(with_check(document, check))

    assert message == "repositories[0].remote.check.checked_at must be a UTC timestamp"


@pytest.mark.parametrize(
    "check",
    [
        {"status": "live", "reason": None},
        {"status": "off", "reason": "remote_checks_off"},
        {"status": "failed", "reason": "unreachable"},
    ],
    ids=["live", "off", "failed"],
)
def test_parser_refuses_a_time_on_a_remote_check_that_is_not_a_saved_one(sample, check):
    document, _folder = sample
    assert models.parse_checkouts(redigest(with_check(document, dict(check))))

    message = refusal_of(with_check(document, {**check, "checked_at": RECORDED_TIME}))

    assert message == "repositories[0].remote.check has an invalid shape"


@pytest.mark.parametrize(
    "reason", [None, "", "setting_off", "not_github", "bogus", "live", 7, ["failed"]]
)
def test_parser_refuses_a_recorded_remote_check_whose_reason_is_none_of_the_allowed_ones(
    sample, reason
):
    document, _folder = sample
    check = {"status": "recorded", "reason": reason, "checked_at": RECORDED_TIME}

    message = refusal_of(with_check(document, check))

    assert message == "repositories[0].remote.check.reason does not match its status"


@pytest.mark.parametrize("checked_at", [None, "", "yesterday", "2026-13-45T25:61:61Z", 5])
@pytest.mark.parametrize("reason", ["remote_checks_off", "failed"])
def test_parser_refuses_a_recorded_lookup_without_a_valid_time(sample, reason, checked_at):
    document, _folder = sample
    accepted = lookup_document(
        document,
        github_lookup="on",
        remote_checks="off" if reason == "remote_checks_off" else "on",
        key=GITHUB_KEY,
        pr_lookup=recorded_lookup(reason),
    )
    assert models.parse_checkouts(accepted)
    broken = doc_copy(accepted)
    broken["repositories"][0]["pr_lookup"]["checked_at"] = checked_at

    message = refusal_of(broken)

    assert message == "repositories[0].pr_lookup.checked_at must be a UTC timestamp"


@pytest.mark.parametrize("reason", ["bogus", "setting_off", "not_github", None])
def test_parser_refuses_a_recorded_lookup_with_a_reason_that_contradicts_the_checks(
    sample, reason
):
    document, _folder = sample
    broken = lookup_document(
        document,
        github_lookup="on",
        remote_checks="on",
        key=GITHUB_KEY,
        pr_lookup=recorded_lookup(reason),
    )

    message = refusal_of(broken)

    assert message == "repositories[0].pr_lookup does not match the lookup setting and the remote"


# ---------------------------------------------------------------------------
# A newer remote-tracking ref beats the saved check
# ---------------------------------------------------------------------------


def vs(basis: str, relation: str, ahead: int | None = None, behind: int | None = None):
    """A comparison with no lender; an ``equal`` one carries zero counts."""
    if relation == "equal":
        ahead, behind = 0, 0
    return {
        "basis": basis,
        "relation": relation,
        "ahead": ahead,
        "behind": behind,
        "objects_from": None,
    }


def push_from(work: Path, branch: str, name: str) -> str:
    """Commit once on ``branch`` of ``work`` and push it with the branch's own upstream."""
    git(work, "checkout", "-q", branch)
    sha = commit_file(work, name, "x\n")
    git(work, "push", "-q")
    return sha


class PushedWorld:
    """Two fetched clones of one ``file://`` remote, each holding ``topic`` (pushed from
    ``a``), and the networked collection that saved the remote's branches. Everything
    done afterwards happens at least a whole second after the saved check."""

    def __init__(self, tmp_path: Path, isolated_git: Path, *, reflogs: bool = True) -> None:
        self.cache = nyx_home(isolated_git)
        served = tmp_path / "served"
        served.mkdir()
        self.folder = tmp_path / "folder"
        self.folder.mkdir()
        self.remote = make_remote(served)
        self.a = clone(self.remote.as_uri(), self.folder / "a")
        self.b = clone(self.remote.as_uri(), self.folder / "b")
        if not reflogs:
            git(self.a, "config", "core.logAllRefUpdates", "false")
        git(self.a, "checkout", "-q", "-b", "topic")
        commit_file(self.a, "topic.txt", "one\n")
        git(self.a, "push", "-q", "-u", "origin", "topic")
        git(self.a, "checkout", "-q", "trunk")
        git(self.b, "fetch", "-q", "origin")
        git(self.b, "branch", "topic", "origin/topic")
        git(self.a, "fetch", "-q", "origin")
        self.trunk = rev(self.remote, "trunk")
        self.topic = rev(self.remote, "topic")
        collect([self.folder], remote_checks=True)
        self.saved = saved_file(self.cache)
        [self.key] = self.saved["remote_checks"]
        self.at = self.saved["remote_checks"][self.key]["heads"]["at"]
        time.sleep(1.1)

    def pushed(self) -> str:
        """``a`` pushes a new ``topic`` commit and ``b`` pushes a new branch ``fresh``."""
        tip = push_from(self.a, "topic", "topic-two.txt")
        git(self.b, "checkout", "-q", "-b", "fresh", "trunk")
        commit_file(self.b, "fresh.txt", "x\n")
        git(self.b, "push", "-q", "-u", "origin", "fresh")
        return tip

    def set_record_time(self, at: int) -> None:
        saved = saved_file(self.cache)
        saved["remote_checks"][self.key]["heads"]["at"] = at
        self.cache.write_text(json.dumps(saved))

    def reflog_time(self, work: Path, name: str) -> int:
        shown = git(
            work, "reflog", "show", "-n", "1", "--date=unix", "--format=%gd",
            f"refs/remotes/origin/{name}",
        ).stdout.strip()
        match = re.search(r"@\{([0-9]+)\}", shown)
        assert match, f"no reflog time for {name}: {shown!r}"
        return int(match.group(1))


@posix_only
def test_a_branch_pushed_after_the_saved_check_reads_equal_on_the_cached_basis(
    tmp_path, isolated_git
):
    world = PushedWorld(tmp_path, isolated_git)
    # Control: before any push every comparison is the saved check's.
    before = collect([world.folder])
    assert branch_of(find_clone(before, world.a), "topic")["vs_remote"] == vs("recorded", "equal")
    tip = world.pushed()
    assert rev(world.a, "topic") == tip != world.topic

    document = collect([world.folder])

    assert document["remote_checks"] == "off"
    [group] = document["repositories"]
    assert group["remote"]["check"]["status"] == "recorded"
    a_item, b_item = find_clone(document, world.a), find_clone(document, world.b)
    # The pushed branch: not ``ahead`` 1 on the saved tip.
    assert branch_of(a_item, "topic")["vs_remote"] == vs("cached", "equal")
    # A branch the saved check never saw: not ``absent``.
    assert branch_of(b_item, "fresh")["vs_remote"] == vs("cached", "equal")
    # Refs that did not move keep the saved check's answer.
    assert branch_of(b_item, "topic")["vs_remote"] == vs("recorded", "equal")
    assert branch_of(a_item, "trunk")["vs_remote"] == vs("recorded", "equal")
    assert a_item["local_default"]["vs_remote"] == vs("recorded", "equal")
    assert a_item["verdict_base"]["basis"] == "recorded"
    assert b_item["verdict_base"]["basis"] == "recorded"


@posix_only
def test_a_trunk_pushed_after_the_saved_check_moves_the_default_and_base_but_not_the_saved_head(
    tmp_path, isolated_git
):
    world = PushedWorld(tmp_path, isolated_git)
    tip = push_from(world.a, "trunk", "trunk-two.txt")
    assert rev(world.a, "refs/remotes/origin/trunk") == tip != world.trunk

    document = collect([world.folder])

    [group] = document["repositories"]
    a_item, b_item = find_clone(document, world.a), find_clone(document, world.b)
    # The group's default is the saved check's, and the clone's own tracking ref
    # is compared with it truthfully.
    assert group["default_branch"] == {"name": "trunk", "sha": world.trunk}
    assert a_item["cached_vs_remote"] == vs("recorded", "ahead", 1, 0)
    assert a_item["local_default"]["vs_remote"] == vs("cached", "equal")
    assert branch_of(a_item, "trunk")["vs_remote"] == vs("cached", "equal")
    assert a_item["verdict_base"] == {
        "name": "trunk",
        "sha": tip,
        "basis": "cached",
        "objects_from": None,
    }
    # The clone whose ref did not move keeps the saved head.
    assert b_item["cached_vs_remote"] == vs("recorded", "equal")
    assert b_item["local_default"]["vs_remote"] == vs("recorded", "equal")
    assert b_item["verdict_base"] == {
        "name": "trunk",
        "sha": world.trunk,
        "basis": "recorded",
        "objects_from": None,
    }
    line = next(
        item
        for item in cli.format_checkouts(document)
        if item.startswith(cli._json_literal(real(world.a)))
    )
    [default] = [element for element in line.split("  ") if element.startswith("default ")]
    assert re.fullmatch(
        r"default trunk cached equal cached equal tracking recorded ahead "
        r"fetched [0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z",
        default,
    ), default


@posix_only
def test_a_push_with_reflogs_disabled_leaves_the_saved_check_standing(tmp_path, isolated_git):
    world = PushedWorld(tmp_path, isolated_git, reflogs=False)
    world.pushed()
    # The push moved the ref but wrote no reflog entry, so the clone cannot say when.
    assert rev(world.a, "refs/remotes/origin/topic") == rev(world.a, "topic")
    assert git(world.a, "reflog", "show", "refs/remotes/origin/topic").stdout.strip() == ""

    document = collect([world.folder])

    a_topic = branch_of(find_clone(document, world.a), "topic")
    assert a_topic["vs_remote"] == vs("recorded", "ahead", 1, 0)
    # Another clone, whose reflogs are on, is not affected by that known limit.
    assert branch_of(find_clone(document, world.b), "fresh")["vs_remote"] == vs("cached", "equal")


@posix_only
def test_the_reflog_is_asked_once_for_each_name_whose_tracking_ref_differs_from_the_saved_one(
    tmp_path, monkeypatch, isolated_git
):
    world = PushedWorld(tmp_path, isolated_git)
    world.pushed()
    shim, log = recording(tmp_path, "reflog-offline")

    with path_with(monkeypatch, shim):
        collect([world.folder])

    asked = re.compile(
        r"-C (\S+) reflog show -n 1 --date=unix --format=%gd refs/remotes/origin/(\S+)"
    )
    calls = sorted(
        match.groups()
        for line in log.read_text().splitlines()
        if (match := asked.search(line))
    )
    assert calls == sorted([(real(world.a), "topic"), (real(world.b), "fresh")])

    # A live check answers for the remote itself and asks no clone's reflog.
    shim, log = recording(tmp_path, "reflog-networked")
    with path_with(monkeypatch, shim):
        live = collect([world.folder], remote_checks=True)
    assert not [line for line in log.read_text().splitlines() if " reflog show " in line]
    assert branch_of(find_clone(live, world.a), "topic")["vs_remote"] == vs("live", "equal")
    assert branch_of(find_clone(live, world.b), "fresh")["vs_remote"] == vs("live", "equal")


REFLOG_FAILURES = {
    "fails": 'case "$*" in *" reflog show "*) echo "fatal: cannot show" >&2; exit 128;; esac',
    "times-out": 'case "$*" in *" reflog show "*) sleep 30;; esac',
}


@posix_only
@pytest.mark.parametrize("kind", list(REFLOG_FAILURES))
def test_a_reflog_call_that_fails_or_times_out_leaves_the_saved_check_standing(
    tmp_path, monkeypatch, isolated_git, kind
):
    world = PushedWorld(tmp_path, isolated_git)
    world.pushed()
    # Control: without the stand-in the same pushes read ``cached``.
    control = collect([world.folder])
    assert branch_of(find_clone(control, world.a), "topic")["vs_remote"] == vs("cached", "equal")
    shim = stand_in(tmp_path, "reflog-" + kind, REFLOG_FAILURES[kind])

    with path_with(monkeypatch, shim):
        document = collect([world.folder], call_cap_seconds=2)

    a_item, b_item = find_clone(document, world.a), find_clone(document, world.b)
    assert branch_of(a_item, "topic")["vs_remote"] == vs("recorded", "ahead", 1, 0)
    assert branch_of(b_item, "fresh")["vs_remote"] == vs("recorded", "absent")
    assert branch_of(b_item, "topic")["vs_remote"] == vs("recorded", "equal")


@posix_only
def test_a_tracking_ref_older_than_the_saved_check_keeps_the_recorded_value(
    tmp_path, isolated_git
):
    nyx_home(isolated_git)
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    remote = make_remote(served)
    a = clone(remote.as_uri(), folder / "a")
    b = clone(remote.as_uri(), folder / "b")
    git(a, "checkout", "-q", "-b", "topic")
    commit_file(a, "topic.txt", "one\n")
    git(a, "push", "-q", "-u", "origin", "topic")
    git(b, "fetch", "-q", "origin")
    git(b, "branch", "topic", "origin/topic")
    time.sleep(1.1)
    # Another clone pushes after B's fetch, and a networked run saves that tip.
    newer = push_from(a, "topic", "topic-two.txt")
    collect([folder], remote_checks=True)
    assert rev(b, "refs/remotes/origin/topic") != newer

    document = collect([folder])

    comparison = branch_of(find_clone(document, b), "topic")["vs_remote"]
    assert comparison == {
        "basis": "recorded",
        "relation": "behind",
        "ahead": 0,
        "behind": 1,
        "objects_from": real(a),
    }


@posix_only
def test_a_tracking_ref_moved_in_the_second_of_the_saved_check_is_the_newer_one(
    tmp_path, isolated_git
):
    world = PushedWorld(tmp_path, isolated_git)
    world.pushed()
    moved = world.reflog_time(world.a, "topic")
    assert moved > world.at

    world.set_record_time(moved)
    same_second = collect([world.folder])
    world.set_record_time(moved + 1)
    later_record = collect([world.folder])

    assert branch_of(find_clone(same_second, world.a), "topic")["vs_remote"] == vs(
        "cached", "equal"
    )
    assert branch_of(find_clone(later_record, world.a), "topic")["vs_remote"] == vs(
        "recorded", "ahead", 1, 0
    )


@posix_only
def test_the_reflog_time_test_answers_no_for_a_cut_a_missing_and_a_passed_deadline(
    tmp_path, isolated_git
):
    world = PushedWorld(tmp_path, isolated_git)
    world.pushed()
    context = checkouts._CloneContext(
        cwd=real(world.a),
        ceiling=real(world.folder),
        partial=False,
        shallow=False,
        local={},
        cached={},
        ssh_configured=False,
    )
    executable = checkouts._resolve_program("git")
    assert executable is not None
    open_runner = checkouts._GitRunner(executable, time.monotonic_ns() + 60 * 10**9, 10)
    expired = checkouts._GitRunner(executable, time.monotonic_ns() - 1, 10)

    assert checkouts._ref_moved_since(open_runner, context, "topic", world.at) is True
    assert checkouts._ref_moved_since(open_runner, context, "topic", 2**40) is False
    assert checkouts._ref_moved_since(open_runner, context, "no-such-branch", 0) is False
    assert checkouts._ref_moved_since(expired, context, "topic", world.at) is False
