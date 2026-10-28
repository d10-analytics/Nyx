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
the check, and otherwise, with no answer, the tracking ref that moved last among the
readable clones that hold it. One ranking key orders both choices: a ref with a move
time before one without, then the later move, then the first clone in natural path
order. With no answer and no clone holding the ref the state is ``unknown``, never
``absent``; fetch times play no part.

Each such package also gets its Code Status. The facts are the branch's local copies (the
branches of that name in the readable clones of the repository) and its remote copy. A
remote copy whose tip equals no local copy's tip has no verdict and counts as unknown,
never merged. The fields are stored in ``code_status`` and ``models.code_status_text``
renders them, so a reader recomputes the text from the fields.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .models import code_status_text, natural_key

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
        moved_later = _newest_held(
            [
                (clone, refs)
                for clone, refs in ready
                if refs.cached.get(name) not in (None, saved)
                and moved_at_or_after(refs.moved.get(name), answer.at)
            ],
            name,
        )
        if moved_later is not None:
            return _copy("present", moved_later, REMOTE_CACHED)
        if saved is None:
            return _copy("absent")
        return _copy("present", saved, REMOTE_RECORDED)
    # No answer at all: the clones' own tracking refs are the only evidence of the remote,
    # and a missing ref proves nothing (a single-branch or shallow clone need not create
    # one on push, and a pruned ref leaves no move time), so only a held ref answers.
    held = _newest_held([(clone, refs) for clone, refs in ready if name in refs.cached], name)
    return _copy("unknown") if held is None else _copy("present", held, REMOTE_CACHED)


def _newest_held(candidates: Sequence[tuple[Mapping[str, Any], CloneRefs]], name: str) -> str | None:
    """The tip of ``origin/<name>`` in the clone whose ref ranks first, or ``None``.

    The ranking key is shared by the saved-answer override and the no-answer choice: a
    ref with a move time ranks before one without, then the later move, then the first
    clone in natural path order.
    """
    if not candidates:
        return None

    def rank(candidate: tuple[Mapping[str, Any], CloneRefs]) -> tuple[Any, ...]:
        moved = candidate[1].moved.get(name)
        return (moved is None, 0 if moved is None else -moved, natural_key(candidate[0]["path"]))

    return min(candidates, key=rank)[1].cached[name]


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


def _local_copies(repository: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    """The local branches named ``name`` in the readable clones of ``repository``."""
    return [
        branch
        for clone in repository["clones"]
        if clone["status"] == "ok"
        for branch in clone["branches"]
        if branch["name"] == name
    ]


def _verdict_state(branch: Mapping[str, Any]) -> str:
    verdict = branch["verdict"]
    return "unknown" if verdict is None else verdict["state"]


def _push_fragment(local: Sequence[Mapping[str, Any]], remote: Mapping[str, Any] | None) -> tuple[str, int | None]:
    """The push state of a branch and the unpushed commit count when it has one."""
    if remote is None:
        return "unknown", None
    if remote["state"] == "absent":
        return "not_pushed", None
    ahead = [
        copy["vs_remote"]["ahead"]
        for copy in local
        if copy["vs_remote"] is not None and copy["vs_remote"]["relation"] in ("ahead", "diverged")
    ]
    if ahead:
        return "unpushed", max(ahead)
    if all(
        copy["vs_remote"] is not None and copy["vs_remote"]["relation"] in ("equal", "behind")
        for copy in local
    ):
        return "pushed", None
    return "unknown", None


def _code_status(
    package: Mapping[str, Any],
    local: Sequence[Mapping[str, Any]],
    remote: Mapping[str, Any] | None,
    *,
    every_package_finished: bool,
) -> dict[str, Any] | None:
    """The Code Status of ``package`` from the copies of its branch, or ``None`` when the
    branch is found nowhere."""
    remote_present = remote is not None and remote["state"] == "present"
    if not local and not remote_present:
        return None
    verdicts = [_verdict_state(copy) for copy in local]
    if remote_present:
        tips = {copy["sha"]: _verdict_state(copy) for copy in reversed(local)}
        verdicts.append(tips.get(remote["sha"], "unknown"))

    held = sorted(
        {path for copy in local for path in copy["checked_out_in"]}, key=natural_key
    )
    target = package["target"]
    own = target["worktree"] if target["status"] == "worktree" else target["clone"]
    status: dict[str, Any] = {
        "state": "unknown",
        "text": "",
        "pull_request": None,
        "push": None,
        "unpushed": None,
        "cleanup": None,
        "checked_out_elsewhere": [path for path in held if path != own],
    }
    push, unpushed = _push_fragment(local, remote)
    open_pulls = [
        pull["number"]
        for copy in local
        for pull in copy["pull_requests"]
        if pull["state"] == "open"
    ]
    if all(verdict == "merged" for verdict in verdicts):
        status["state"] = "merged"
        status["cleanup"] = (
            {"worktrees": len(held), "branches": 0}
            if held
            else {"worktrees": 0, "branches": len(local)}
        )
    elif open_pulls:
        status["state"] = "pr_open"
        status["pull_request"] = max(open_pulls)
        status["push"], status["unpushed"] = push, unpushed
    elif "not_merged" in verdicts:
        if every_package_finished:
            status["state"] = "awaiting_merge"
        elif package["superseded"]:
            status["state"] = "superseded"
        else:
            status["state"] = "in_progress"
            status["push"], status["unpushed"] = push, unpushed
    status["text"] = code_status_text(status)
    return status


def join_branches(
    repositories: Sequence[dict[str, Any]],
    packages: Sequence[dict[str, Any]],
    remotes: Mapping[str, RemoteFacts],
    *,
    scan_complete: bool,
) -> None:
    """Match declared package branches to the branches of their repository.

    ``packages`` are the package entries after target resolution; ``remotes`` holds the
    remote facts by remote key. A package whose target resolved to a clone and whose
    ``Branch:`` equals its repository's default branch becomes ``default_branch`` and is
    matched to nothing. Every other such package is listed in its repository's
    ``package_branches``, with the remote copy of the branch (``None`` for a repository
    without a remote), and on each local branch of that repository's readable clones that
    carries its branch name or is folded under it, and every package whose branch has a
    copy gets its Code Status. All three are rewritten in place.

    ``scan_complete`` says whether the package scan read every package. When it did not,
    a package that is not listed may still name the branch, so no branch is concluded to be
    named only by finished packages: the cross-package conclusion is withheld and the
    package reads by the remaining rules.
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
        copies = {entry["name"]: entry["remote"] for entry in repository["package_branches"]}
        for name, named_packages in by_name.items():
            local = _local_copies(repository, name)
            every_finished = scan_complete and all(
                package["finished"] for package in named_packages
            )
            for package in named_packages:
                package["code_status"] = _code_status(
                    package, local, copies[name], every_package_finished=every_finished
                )
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
