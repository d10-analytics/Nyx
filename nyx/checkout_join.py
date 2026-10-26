"""Join of specification packages to the checkouts the collector read.

This module makes no git call and reads no file. It decides, from plain facts
the collector hands it, which checkout a package's ``Target repo:`` names. A
path is represented by an opaque, hashable *key*; two paths name the same
directory exactly when their keys are equal, so a symbolic link or a path
written in another letter case resolves without a string comparison.

A target is looked up in three key sets: the listed linked worktrees of readable
clones, then every clone (its document path and every discovery candidate that
belongs to it), then the discovery candidates that turned out not to be a
repository. A target that matches none of them is classified by ``fallback``,
which looks only at the filesystem: the join never causes a git call in a
directory that discovery did not admit.

A package that declares a branch is matched to branches by repository group, not
by name alone: a branch name is sought only in the clones of the repository the
package's target belongs to, and only in clones that were read (``ok``). The
default branch of a repository is never a package branch.

The remote copy of a package branch is decided here from plain facts the collector
read, under the same moved-ref rule the clones' own comparisons use
(``moved_at_or_after``): the group's remote answer when the check was live, the saved
answer unless a clone holds a tracking ref that differs from it and moved at or after
the check, and otherwise the tracking ref of the clone fetched most recently.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .models import natural_key

PathKey = tuple[Any, ...]

# Statuses a target takes without being matched against any checkout.
UNMATCHED_STATUSES = frozenset({"missing", "not_git", "outside_folders"})


@dataclass(frozen=True)
class TargetIndex:
    """The path keys of the checkouts of one collection.

    ``clones`` maps the key of a clone's document path, and of every discovery
    candidate that belongs to the clone, to that document path. ``worktrees``
    maps the key of each linked worktree listed under a readable clone to the
    pair (clone path, worktree path). ``not_git`` holds the keys of candidates
    that discovery found but git did not report as a repository top level.
    """

    clones: Mapping[PathKey, str]
    worktrees: Mapping[PathKey, tuple[str, str]]
    not_git: frozenset[PathKey]


def resolve_target(
    head_status: str,
    key: PathKey | None,
    index: TargetIndex | None,
    fallback: Callable[[], str],
) -> dict[str, Any]:
    """Return the ``target`` of one package.

    ``head_status`` is the ``Target repo:`` header status (``usable``,
    ``invalid`` or ``absent``) and ``key`` the identity of its path, ``None``
    when the path cannot be read. ``index`` is ``None`` when git is not
    available, which leaves a usable target ``unchecked``. ``fallback`` returns
    ``missing``, ``not_git`` or ``outside_folders`` for a path that matches no
    key.
    """
    if head_status != "usable":
        return _target(head_status)
    if index is None:
        return _target("unchecked")
    if key is not None:
        listed = index.worktrees.get(key)
        if listed is not None:
            return _target("worktree", listed[0], listed[1])
        clone = index.clones.get(key)
        if clone is not None:
            return _target("clone", clone)
        if key in index.not_git:
            return _target("not_git")
    status = fallback()
    if status not in UNMATCHED_STATUSES:
        raise ValueError(f"unexpected unmatched target status {status!r}")
    return _target(status)


def _target(status: str, clone: str | None = None, worktree: str | None = None) -> dict[str, Any]:
    return {"status": status, "clone": clone, "worktree": worktree}


# How a local branch serves a package.
BASIS_TARGET_CLONE = "target_clone"
BASIS_SIBLING_CLONE = "sibling_clone"
BASIS_CONTAINED = "contained"

# Where the remote copy of a package branch comes from.
REMOTE_LIVE = "live"
REMOTE_RECORDED = "recorded"
REMOTE_CACHED = "cached"


def moved_at_or_after(moved_at: int | None, at: int) -> bool:
    """Whether a tracking ref that last moved at ``moved_at`` moved at or after ``at``.

    A move time that could not be read counts as not moved.
    """
    return moved_at is not None and moved_at >= at


@dataclass(frozen=True)
class RemoteAnswer:
    """A group's remote answer: ``basis`` is ``live`` or ``recorded``, ``at`` the Unix
    second of the check, ``tips`` the whole answer (branch name to tip)."""

    basis: str
    at: int
    tips: Mapping[str, str]


@dataclass(frozen=True)
class CloneRefs:
    """One clone's own ``refs/remotes/origin/*`` tips for the package branch names,
    and the move time of each such ref the collector read (``None``: unreadable)."""

    cached: Mapping[str, str]
    moved: Mapping[str, int | None]


@dataclass(frozen=True)
class RemoteFacts:
    """What decides the remote copy for one repository group: its answer (``None``
    when the check failed or is off with nothing saved) and its clones' refs by path."""

    answer: RemoteAnswer | None
    clones: Mapping[str, CloneRefs]


def _declared(
    repositories: Sequence[dict[str, Any]], packages: Sequence[dict[str, Any]]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """The (repository, package) pairs of packages that declare a branch and whose
    target resolved to a clone of that repository."""
    group_of = {
        clone["path"]: repository
        for repository in repositories
        for clone in repository["clones"]
    }
    pairs = []
    for package in packages:
        target = package["target"]
        if package["branch"]["status"] != "declared" or target["status"] not in (
            "clone",
            "worktree",
        ):
            continue
        repository = group_of.get(target["clone"])
        if repository is not None:
            pairs.append((repository, package))
    return pairs


def declared_branch_names(
    repositories: Sequence[dict[str, Any]], packages: Sequence[dict[str, Any]]
) -> dict[str, frozenset[str]]:
    """The branch names packages declare, by remote key of the repository group.

    Groups without a remote are absent: they have no remote copy. The default branch
    is not excluded, since a group's default is known only after its remote check.
    """
    names: dict[str, set[str]] = {}
    for repository, package in _declared(repositories, packages):
        if repository["remote"] is not None:
            names.setdefault(repository["remote"]["key"], set()).add(package["branch"]["name"])
    return {key: frozenset(found) for key, found in names.items()}


def _copy(state: str, sha: str | None = None, basis: str | None = None) -> dict[str, Any]:
    return {"state": state, "sha": sha, "basis": basis}


def _remote_copy(repository: Mapping[str, Any], name: str, facts: RemoteFacts | None) -> dict[str, Any]:
    """The remote copy of branch ``name`` of a group that has a remote."""
    if facts is None:
        return _copy("unknown")
    answer = facts.answer
    if answer is not None and answer.basis == REMOTE_LIVE:
        tip = answer.tips.get(name)
        return _copy("absent") if tip is None else _copy("present", tip, REMOTE_LIVE)
    ready = [
        (clone, facts.clones[clone["path"]])
        for clone in repository["clones"]
        if clone["status"] == "ok" and clone["path"] in facts.clones
    ]
    if answer is not None:
        saved = answer.tips.get(name)
        newest: tuple[int, str] | None = None
        for _clone, refs in ready:
            held = refs.cached.get(name)
            moved = refs.moved.get(name)
            if held is None or held == saved or not moved_at_or_after(moved, answer.at):
                continue
            assert moved is not None
            if newest is None or moved > newest[0]:
                newest = (moved, held)
        if newest is not None:
            return _copy("present", newest[1], REMOTE_CACHED)
        if saved is None:
            return _copy("absent")
        return _copy("present", saved, REMOTE_RECORDED)
    fetched: tuple[str, CloneRefs] | None = None
    for clone, refs in ready:
        last = (clone["cached_default"] or {}).get("last_fetched_at")
        if last is not None and (fetched is None or last > fetched[0]):
            fetched = (last, refs)
    if fetched is None:
        return _copy("unknown")
    tip = fetched[1].cached.get(name)
    return _copy("absent") if tip is None else _copy("present", tip, REMOTE_CACHED)


def _default_names(repository: Mapping[str, Any]) -> frozenset[str]:
    """The names a package branch may not take in ``repository``.

    The group's known default branch name when there is one; otherwise the
    verdict base of every readable clone. No name is assumed.
    """
    known = (repository["default_branch"] or {}).get("name")
    if known is not None:
        return frozenset({known})
    return frozenset(
        clone["verdict_base"]["name"]
        for clone in repository["clones"]
        if clone["status"] == "ok" and clone["verdict_base"] is not None
    )


def _contained_chain(branches: Mapping[str, str | None], start: str) -> list[str]:
    """The names reached by following ``contained_in`` from ``start``, nearest first."""
    reached: list[str] = []
    seen = {start}
    name = branches.get(start)
    while name is not None and name not in seen:
        reached.append(name)
        seen.add(name)
        name = branches.get(name)
    return reached


def join_branches(
    repositories: Sequence[dict[str, Any]],
    packages: Sequence[dict[str, Any]],
    remotes: Mapping[str, RemoteFacts],
) -> None:
    """Match declared package branches to the branches of their repository.

    ``packages`` are the package entries after target resolution; ``remotes`` holds the
    remote facts by remote key. A package whose target resolved to a clone and whose
    ``Branch:`` equals its repository's default branch becomes ``default_branch`` and is
    matched to nothing. Every other such package is listed in its repository's
    ``package_branches``, with the remote copy of the branch (``None`` for a repository
    without a remote), and on each local branch of that repository's readable clones that
    carries its branch name or is folded under it. Both are rewritten in place.
    """
    named: dict[int, dict[str, list[dict[str, Any]]]] = {}
    for repository, package in _declared(repositories, packages):
        if package["branch"]["name"] in _default_names(repository):
            package["branch"] = {"status": "default_branch", "name": None}
            continue
        named.setdefault(id(repository), {}).setdefault(package["branch"]["name"], []).append(
            package
        )

    for repository in repositories:
        by_name = named.get(id(repository), {})
        remote = repository["remote"]
        facts = remotes.get(remote["key"]) if remote is not None else None
        repository["package_branches"] = [
            {
                "name": name,
                "remote": None if remote is None else _remote_copy(repository, name, facts),
                "packages": sorted(p["package_path"] for p in by_name[name]),
            }
            for name in sorted(by_name, key=natural_key)
        ]
        if not by_name:
            continue
        for clone in repository["clones"]:
            if clone["status"] != "ok":
                continue
            containers = {branch["name"]: branch["contained_in"] for branch in clone["branches"]}
            for branch in clone["branches"]:
                references: dict[str, str] = {}
                for package in by_name.get(branch["name"], ()):
                    references[package["package_path"]] = (
                        BASIS_TARGET_CLONE
                        if package["target"]["clone"] == clone["path"]
                        else BASIS_SIBLING_CLONE
                    )
                for reached in _contained_chain(containers, branch["name"]):
                    for package in by_name.get(reached, ()):
                        references.setdefault(package["package_path"], BASIS_CONTAINED)
                branch["packages"] = [
                    {"package_path": path, "basis": references[path]}
                    for path in sorted(references)
                ]
