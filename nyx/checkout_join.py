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
    repositories: Sequence[dict[str, Any]], packages: Sequence[dict[str, Any]]
) -> None:
    """Match declared package branches to the local branches of their repository.

    ``packages`` are the package entries after target resolution. A package whose
    target resolved to a clone and whose ``Branch:`` equals its repository's
    default branch becomes ``default_branch`` and is matched to nothing. Every
    other such package is listed in its repository's ``package_branches`` and on
    each local branch of that repository's readable clones that carries its
    branch name or is folded under it. Both are rewritten in place.
    """
    group_of = {
        clone["path"]: repository
        for repository in repositories
        for clone in repository["clones"]
    }
    named: dict[int, dict[str, list[dict[str, Any]]]] = {}
    for package in packages:
        branch = package["branch"]
        target = package["target"]
        if branch["status"] != "declared" or target["status"] not in ("clone", "worktree"):
            continue
        repository = group_of.get(target["clone"])
        if repository is None:
            continue
        if branch["name"] in _default_names(repository):
            package["branch"] = {"status": "default_branch", "name": None}
            continue
        named.setdefault(id(repository), {}).setdefault(branch["name"], []).append(package)

    for repository in repositories:
        by_name = named.get(id(repository), {})
        repository["package_branches"] = [
            {"name": name, "packages": sorted(p["package_path"] for p in by_name[name])}
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
