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
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from nyx import checkouts, models

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
