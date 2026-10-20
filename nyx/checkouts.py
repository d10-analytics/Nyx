"""Read-only collector for the git state of the configured checkout folders.

Every fact comes from a git command run through one runner, or from a direct
look at the files git keeps inside a repository's own directories. Nothing here
fetches, writes or locks anything. Local commands run with every transport
denied. The one exception is the optional remote check: a single
``git ls-remote`` per distinct remote, the only call that lifts the transport
denial (so git applies the user's own transport policy and credentials), and it
transfers no objects.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .catalog import MAX_OUTPUT_BYTES
from .models import (
    CHECKOUTS_DIGEST_OMITTED,
    CHECKOUTS_SCHEMA_VERSION,
    OPERATIONS,
    _safe_text,
    branch_sort_key,
    canonical_digest,
    clone_sort_key,
    diagnostic_sort_key,
    folder_sort_key,
    natural_key,
    repository_sort_key,
    worktree_sort_key,
)
from .state import _is_reparse_or_link

CALL_CAP_SECONDS = 10.0
MAX_PARALLEL_CLONES = 8
GIT_FLOOR = (2, 36)

_ON_WINDOWS = os.name == "nt"
_GIT_EXECUTABLE = "git.exe" if _ON_WINDOWS else "git"
_VERSION_LINE = re.compile(r"git version (?P<version>[0-9]+(?:\.[0-9]+)+\S*)")
_NUMERIC_PREFIX = re.compile(r"[0-9]+(?:\.[0-9]+)+")
_CONFIG_PATTERN = (
    r"^(remote\..*\.(url|promisor)|extensions\.partialclone|url\..*\.insteadof"
    r"|core\.sshcommand)$"
)
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_PROBE_CHUNK = 200
_ZERO_OBJECT = re.compile(r"0+")
_TRUE_VALUES = frozenset({"", "true", "yes", "on", "1"})

# Used only to start the one call that asks git which variables redirect a
# repository, and when that call cannot answer.
_FALLBACK_LOCAL_ENVIRONMENT = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_GRAFT_FILE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_INTERNAL_SUPER_PREFIX",
        "GIT_NAMESPACE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_QUARANTINE_PATH",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    }
)


class CheckoutsOutputTooLargeError(ValueError):
    """The rendered checkout document would exceed ``MAX_OUTPUT_BYTES``."""

    category = "checkouts_output_too_large"


# ---------------------------------------------------------------------------
# The one git runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Outcome:
    code: int | None
    out: bytes = b""
    err: bytes = b""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out

    def failure(self) -> str:
        """Return the item status this failed call maps to."""
        if self.timed_out:
            return "timed_out"
        if b"dubious ownership" in self.err:
            return "unsafe_ownership"
        return "failed"


def _resolve_git() -> str | None:
    """Find git among the absolute ``PATH`` entries only."""
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        candidate = os.path.join(entry, _GIT_EXECUTABLE)
        if os.path.isfile(candidate) and (_ON_WINDOWS or os.access(candidate, os.X_OK)):
            return candidate
    return None


class _GitRunner:
    """Start every git child with the same isolation, budget and cleanup."""

    def __init__(self, executable: str, deadline_ns: int, call_cap: float) -> None:
        self._executable = executable
        self._deadline_ns = deadline_ns
        self._call_cap = call_cap
        self._removed = _FALLBACK_LOCAL_ENVIRONMENT

    def learn_local_environment(self) -> None:
        outcome = self.run(["rev-parse", "--local-env-vars"])
        if outcome.ok:
            names = {
                line.strip()
                for line in outcome.out.decode("utf-8", "replace").splitlines()
                if line.strip()
            }
            if names:
                self._removed = frozenset(names)

    def _environment(self, ceiling: str | None) -> dict[str, str]:
        removed = {name.upper() for name in self._removed} | {
            "GIT_CEILING_DIRECTORIES",
            "GIT_NAMESPACE",
        }
        environment = {
            key: value
            for key, value in os.environ.items()
            if (key.upper() if _ON_WINDOWS else key) not in removed
            and key not in removed
        }
        environment.update(
            {
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "",
                "SSH_ASKPASS_REQUIRE": "never",
                "GCM_INTERACTIVE": "never",
                "GIT_ALLOW_PROTOCOL": "",
                "LC_ALL": "C",
            }
        )
        if ceiling is not None:
            environment["GIT_CEILING_DIRECTORIES"] = ceiling
        return environment

    def run(
        self,
        arguments: Sequence[str],
        *,
        path: str | None = None,
        ceiling: str | None = None,
        overlay: Mapping[str, str | None] | None = None,
    ) -> _Outcome:
        """Run one git command; ``overlay`` is applied after the scrub and fixed
        variables of this call only, and a ``None`` value removes a variable."""
        remaining = (self._deadline_ns - time.monotonic_ns()) / 1e9
        timeout = min(self._call_cap, remaining)
        if timeout <= 0:
            return _Outcome(None, timed_out=True)
        environment = self._environment(ceiling)
        for name, value in (overlay or {}).items():
            if value is None:
                environment.pop(name, None)
            else:
                environment[name] = value
        command = [self._executable, "--no-optional-locks", "-c", "core.fsmonitor=false"]
        if path is not None:
            command += ["-C", path]
        command += list(arguments)
        # Output goes to temporary files, not pipes, so a program git started
        # and left behind cannot keep this call waiting after git is killed.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    env=environment,
                )
            except OSError:
                return _Outcome(None)
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                return _Outcome(None, timed_out=True)
            out.seek(0)
            err.seek(0)
            return _Outcome(process.returncode, out.read(), err.read())


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _decode(data: bytes) -> str:
    # Explicit on every OS: Windows' default handler would raise on bytes
    # that are not valid in the filesystem encoding.
    return data.decode(sys.getfilesystemencoding(), "surrogateescape")


def _representable(text: str) -> bool:
    return _safe_text(text)


def _lstat(path: str) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except OSError:
        return None


def _exists(path: str) -> bool:
    return _lstat(path) is not None


def _utc_text(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _object_or_none(value: str) -> str | None:
    return None if not value or _ZERO_OBJECT.fullmatch(value) else value


def _parse_version(output: bytes) -> str | None:
    lines = output.decode("utf-8", "replace").splitlines()
    match = _VERSION_LINE.match(lines[0].strip()) if lines else None
    return match.group("version") if match else None


def _meets_floor(version: str) -> bool:
    numeric = _NUMERIC_PREFIX.match(version)
    if numeric is None:
        return False
    parts = tuple(int(part) for part in numeric.group(0).split("."))
    return parts[: len(GIT_FLOOR)] >= GIT_FLOOR


# ---------------------------------------------------------------------------
# Remote normalization
# ---------------------------------------------------------------------------

_URL_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*)://(.*)", re.S)
_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:[\\/]")
_DEFAULT_PORTS = {"ssh": "22", "git": "9418", "http": "80", "https": "443"}
_HOME_PREFIX = re.compile(r"~[^/]*/")


def _local_remote(path: str, base: str) -> tuple[str, str]:
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    resolved = os.path.realpath(path).removesuffix(".git")
    return f"file:{resolved}", resolved


def _network_remote(scheme: str | None, authority: str, path: str) -> tuple[str, str]:
    authority = authority.rpartition("@")[2]
    port = ""
    if authority.startswith("["):
        host, _, rest = authority[1:].partition("]")
        host = f"[{host}]"
        if rest.startswith(":") and rest[1:].isdigit():
            port = rest[1:]
    else:
        host, separator, tail = authority.rpartition(":")
        if separator and tail.isdigit():
            port = tail
        else:
            host = authority
    host = host.lower()
    if port == _DEFAULT_PORTS.get(scheme or "ssh") and scheme in (None, *_DEFAULT_PORTS):
        port = ""
    path = re.split(r"[?#]", path, maxsplit=1)[0].lstrip("/")
    path = _HOME_PREFIX.sub("", path, count=1)
    path = path.removesuffix("/").removesuffix(".git")
    key_host = host + (f":{port}" if port else "")
    return f"{key_host}/{path}", path or key_host


def normalize_remote(url: str, base: str) -> tuple[str, str] | None:
    """Return the group ``(key, display)`` for a remote URL, never its credentials."""
    url = url.strip()
    if not url:
        return None
    scheme_match = _URL_SCHEME.fullmatch(url)
    if scheme_match is not None:
        scheme = scheme_match.group(1).lower()
        rest = scheme_match.group(2)
        authority, _, path = rest.partition("/")
        if scheme == "file":
            local = unquote("/" + path)
            if re.fullmatch(r"/[A-Za-z]:/.*", local):
                local = local[1:]
            return _local_remote(local, base)
        return _network_remote(scheme, authority, path)
    if _WINDOWS_DRIVE.match(url):
        return _local_remote(url, base)
    colon = url.find(":")
    slash = url.find("/")
    if colon > 0 and (slash == -1 or colon < slash):
        host_part = url[:colon]
        if len(host_part) == 1 and host_part.isalpha():
            return _local_remote(url, base)
        return _network_remote(None, host_part, url[colon + 1 :])
    return _local_remote(url, base)


def _rewrite_instead_of(url: str, rules: Iterable[tuple[str, str]]) -> str:
    best: tuple[str, str] | None = None
    for prefix, base in rules:
        if prefix and url.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, base)
    return url if best is None else best[1] + url[len(best[0]) :]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    path: str
    real: str
    enclosing: str | None


@dataclass
class _Discovery:
    folders: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[_Candidate] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)


def _has_git_entry(path: str) -> bool:
    details = _lstat(os.path.join(path, ".git"))
    return details is not None and (stat.S_ISDIR(details.st_mode) or stat.S_ISREG(details.st_mode))


def _discover(folders: Iterable[str]) -> _Discovery:
    result = _Discovery()
    seen_real: set[str] = set()

    def add(path: str, enclosing: str | None) -> None:
        real = os.path.realpath(path)
        if real not in seen_real:
            seen_real.add(real)
            result.candidates.append(_Candidate(path, real, enclosing))

    for folder in sorted({os.fspath(item) for item in folders}, key=natural_key):
        if not _representable(folder):
            result.diagnostics.append({"code": "unrepresentable_path", "path": None})
            continue
        try:
            details = os.stat(folder)
        except (FileNotFoundError, NotADirectoryError):
            result.folders.append({"path": folder, "status": "missing"})
            continue
        except OSError:
            result.folders.append({"path": folder, "status": "unreadable"})
            continue
        if not stat.S_ISDIR(details.st_mode):
            result.folders.append({"path": folder, "status": "not_directory"})
            continue
        try:
            with os.scandir(folder) as scan:
                names = sorted(entry.name for entry in scan)
        except OSError:
            result.folders.append({"path": folder, "status": "unreadable"})
            continue
        result.folders.append({"path": folder, "status": "ok"})
        if _has_git_entry(folder):
            add(folder, None)
        for name in names:
            child = os.path.join(folder, name)
            child_details = _lstat(child)
            if (
                child_details is None
                or _is_reparse_or_link(Path(child), child_details)
                or not stat.S_ISDIR(child_details.st_mode)
                or not _has_git_entry(child)
            ):
                continue
            if not _representable(child):
                result.diagnostics.append({"code": "unrepresentable_path", "path": folder})
                continue
            add(child, folder)
    return result


@dataclass(frozen=True)
class _Identity:
    candidate: _Candidate
    status: str  # ok | not_a_repository | unsafe_ownership | failed | timed_out
    toplevel: str = ""
    common: str = ""
    shallow: bool = False


def _identify(runner: _GitRunner, candidate: _Candidate) -> _Identity:
    outcome = runner.run(
        [
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-common-dir",
            "--absolute-git-dir",
            "--is-bare-repository",
            "--is-shallow-repository",
        ],
        path=candidate.real,
        ceiling=os.path.dirname(candidate.real),
    )
    if not outcome.ok:
        if not outcome.timed_out and b"not a git repository" in outcome.err:
            return _Identity(candidate, "not_a_repository")
        return _Identity(candidate, outcome.failure())
    lines = _decode(outcome.out).split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) != 5:
        return _Identity(candidate, "failed")
    toplevel = os.path.realpath(lines[0])
    if toplevel != candidate.real:
        return _Identity(candidate, "not_a_repository")
    return _Identity(
        candidate,
        "ok",
        toplevel=toplevel,
        common=os.path.realpath(lines[1]),
        shallow=lines[4] == "true",
    )


# ---------------------------------------------------------------------------
# One clone
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CloneContext:
    """What a live comparison needs from one clone's local collection."""

    cwd: str
    ceiling: str
    partial: bool
    shallow: bool
    local: dict[str, tuple[str, str]]
    cached: dict[str, str]
    ssh_configured: bool
    common: str = ""


@dataclass
class _CloneOutcome:
    clone: dict[str, Any] | None
    remote: tuple[str, str] | None = None
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    context: _CloneContext | None = None


@dataclass(frozen=True)
class _ListedWorktree:
    path: str
    head: str | None
    branch: str | None
    bare: bool
    locked: bool
    prunable: bool


def _unavailable_clone(path: str, status: str) -> dict[str, Any]:
    return {
        "path": path,
        "bare": None,
        "status": status,
        "partial_clone": None,
        "shallow": None,
        "cached_default": None,
        "local_default": None,
        "cached_vs_live": None,
        "stash_count": None,
        "worktrees": None,
        "branches": None,
    }


def _comparison(
    relation: str,
    ahead: int | None = None,
    behind: int | None = None,
    basis: str = "cached",
) -> dict[str, Any]:
    return {
        "basis": basis,
        "relation": relation,
        "ahead": ahead,
        "behind": behind,
        "objects_from": None,
    }


def _counted_comparison(outcome: _Outcome, basis: str) -> dict[str, Any]:
    """Read a ``rev-list --left-right --count`` outcome as a comparison."""
    fields = outcome.out.split()
    if outcome.timed_out or (basis == "live" and outcome.code is None):
        return _comparison("unknown", basis=basis)
    if outcome.ok and len(fields) == 2 and all(f.isdigit() for f in fields):
        ahead, behind = int(fields[0]), int(fields[1])
        if ahead == 0 and behind == 0:
            # Different objects naming the same commits (for example a tag
            # object): nothing was counted, so say only that they differ.
            return _comparison("differs", basis=basis)
        relation = "diverged" if ahead and behind else "ahead" if ahead else "behind"
        return _comparison(relation, ahead, behind, basis)
    return _comparison("differs", basis=basis)


def _has_ssh_command(data: bytes) -> bool:
    return any(
        _decode(record).partition("\n")[0] == "core.sshcommand"
        for record in data.split(b"\0")
        if record
    )


def _parse_config(data: bytes) -> tuple[dict[str, str], list[tuple[str, str]], bool]:
    origin: dict[str, str] = {}
    rules: list[tuple[str, str]] = []
    partial = False
    for record in data.split(b"\0"):
        if not record:
            continue
        key, _, value = _decode(record).partition("\n")
        if key == "remote.origin.url":
            origin["url"] = value
        elif key == "extensions.partialclone":
            partial = partial or bool(value)
        elif key.startswith("remote.") and key.endswith(".promisor"):
            partial = partial or value.strip().lower() in _TRUE_VALUES
        elif key.startswith("url.") and key.endswith(".insteadof"):
            rules.append((value, key[len("url.") : -len(".insteadof")]))
    return origin, rules, partial


def _parse_worktrees(data: bytes) -> list[_ListedWorktree]:
    entries: list[dict[str, Any]] = []
    for token in data.split(b"\0"):
        if not token:
            continue
        text = _decode(token)
        if text.startswith("worktree "):
            # Git spells paths with forward slashes on every host; the document
            # uses the host's native spelling so clone, worktree and folder
            # paths agree and match the paths callers already hold.
            entries.append({"path": os.path.normpath(text[len("worktree ") :])})
        elif entries:
            label, _, value = text.partition(" ")
            entries[-1][label] = value
    return [
        _ListedWorktree(
            path=entry["path"],
            head=_object_or_none(entry.get("HEAD", "")),
            branch=entry.get("branch"),
            bare="bare" in entry,
            locked="locked" in entry,
            prunable="prunable" in entry,
        )
        for entry in entries
    ]


def _fetch_head_names_origin(path: str, matches: Callable[[str], bool]) -> bool:
    try:
        with open(path, "rb") as handle:
            content = handle.read()
    except OSError:
        return False
    for raw_line in content.split(b"\n"):
        fields = _decode(raw_line).split("\t", 2)
        if len(fields) != 3:
            continue
        description = fields[2]
        source: str | None = description
        for prefix in ("branch '", "tag '", "remote-tracking branch '", "'"):
            if description.startswith(prefix):
                end = description.find("' of ", len(prefix))
                source = description[end + len("' of ") :] if end >= 0 else None
                break
        if source and matches(source):
            return True
    return False


def _linked_git_directories(common: str) -> dict[str, str]:
    """Map the real path of each linked worktree to its own git directory."""
    result: dict[str, str] = {}
    root = os.path.join(common, "worktrees")
    try:
        with os.scandir(root) as scan:
            names = [entry.name for entry in scan]
    except OSError:
        return result
    for name in names:
        directory = os.path.join(root, name)
        try:
            with open(os.path.join(directory, "gitdir"), "rb") as handle:
                pointer = _decode(handle.read(4096)).split("\n", 1)[0].strip()
        except OSError:
            continue
        if not pointer:
            continue
        if not os.path.isabs(pointer):
            pointer = os.path.join(directory, pointer)
        result[os.path.realpath(os.path.dirname(pointer))] = directory
    return result


def _claimed_elsewhere(path: str, git_directory: str) -> bool:
    """Whether another repository now owns the worktree path.

    Only a `.git` directory, or a `gitdir:` pointer to an existing git
    directory other than this clone's own, is such a claim. A missing,
    unreadable or malformed `.git`, an empty pointer, or a pointer to a
    directory that is gone leaves the worktree listed so its status fails.
    """
    marker = os.path.join(path, ".git")
    if os.path.isdir(marker):
        return True
    try:
        with open(marker, "rb") as handle:
            first = _decode(handle.read(4096)).split("\n", 1)[0].strip()
    except OSError:
        return False
    if not first.startswith("gitdir:"):
        return False
    pointer = first[len("gitdir:") :].strip()
    if not pointer:
        return False
    if not os.path.isabs(pointer):
        pointer = os.path.join(path, pointer)
    target = os.path.realpath(pointer)
    return os.path.isdir(target) and target != os.path.realpath(git_directory)


def _operations(git_directory: str) -> list[str]:
    found = set()
    if _exists(os.path.join(git_directory, "MERGE_HEAD")):
        found.add("merge")
    if _exists(os.path.join(git_directory, "rebase-merge")):
        found.add("rebase")
    if _exists(os.path.join(git_directory, "rebase-apply")):
        found.add(
            "am"
            if _exists(os.path.join(git_directory, "rebase-apply", "applying"))
            else "rebase"
        )
    if _exists(os.path.join(git_directory, "CHERRY_PICK_HEAD")):
        found.add("cherry_pick")
    if _exists(os.path.join(git_directory, "REVERT_HEAD")):
        found.add("revert")
    if _exists(os.path.join(git_directory, "BISECT_LOG")):
        found.add("bisect")
    return [name for name in OPERATIONS if name in found]


def _count_status(data: bytes) -> dict[str, int]:
    staged = unstaged = untracked = conflicted = 0
    for record in data.split(b"\0"):
        if record.startswith(b"1 ") and len(record) >= 4:
            staged += record[2:3] != b"."
            unstaged += record[3:4] != b"."
        elif record.startswith(b"u "):
            conflicted += 1
        elif record.startswith(b"? "):
            untracked += 1
    return {
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "conflicted": conflicted,
    }


def _worktree_document(
    runner: _GitRunner,
    listed: _ListedWorktree,
    *,
    main: bool,
    git_directory: str | None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "path": listed.path,
        "main": main,
        "head": None,
        "locked": listed.locked,
        "prunable": listed.prunable,
        "status": "failed",
        "changes": None,
        "operations": None,
        "index_lock": None,
    }
    if listed.prunable:
        return document
    real = os.path.realpath(listed.path)
    outcome = runner.run(
        ["status", "--porcelain=2", "-z", "--untracked-files=normal", "--no-renames"],
        path=real,
        ceiling=os.path.dirname(real),
    )
    if not outcome.ok:
        document["status"] = outcome.failure()
        return document
    if git_directory is None or not os.path.isdir(git_directory):
        return document
    branch = None
    if listed.branch is not None and listed.branch.startswith("refs/heads/"):
        branch = listed.branch[len("refs/heads/") :]
    if branch is None and listed.head is None:
        return document
    document.update(
        {
            "status": "ok",
            "head": {"branch": branch, "sha": listed.head},
            "changes": _count_status(outcome.out),
            "operations": _operations(git_directory),
            "index_lock": _exists(os.path.join(git_directory, "index.lock")),
        }
    )
    return document


def _collect_clone(runner: _GitRunner, members: list[_Identity]) -> _CloneOutcome:
    anchor = members[0]
    cwd = anchor.toplevel
    ceiling = os.path.dirname(cwd)
    common = anchor.common
    path = anchor.candidate.path
    enclosing = anchor.candidate.enclosing

    def call(arguments: Sequence[str]) -> _Outcome:
        return runner.run(arguments, path=cwd, ceiling=ceiling)

    configuration = call(["config", "-z", "--get-regexp", _CONFIG_PATTERN])
    if configuration.timed_out or configuration.code not in (0, 1):
        return _CloneOutcome(_unavailable_clone(path, configuration.failure()))
    origin, rules, partial = _parse_config(configuration.out)

    def remote_for(base: str) -> tuple[str, str] | None:
        return normalize_remote(origin["url"], base) if "url" in origin else None

    remote = remote_for(cwd)
    if not _remote_text_safe(remote):
        # An origin that cannot be shown safely: the clone joins no group and
        # reports none of its remote-derived facts.
        return _CloneOutcome(_unavailable_clone(path, "failed"))

    def degraded(status: str) -> _CloneOutcome:
        return _CloneOutcome(_unavailable_clone(path, status), remote)

    listing = call(["worktree", "list", "--porcelain", "-z"])
    if not listing.ok:
        return degraded(listing.failure())
    listed = _parse_worktrees(listing.out)
    if not listed:
        return degraded("failed")
    first = listed[0]
    path = first.path
    if not _representable(path):
        return _CloneOutcome(
            None, diagnostics=[{"code": "unrepresentable_path", "path": enclosing}]
        )
    bare = first.bare
    remote = remote_for(path if not bare else cwd)
    if not _remote_text_safe(remote):
        return _CloneOutcome(_unavailable_clone(path, "failed"))

    refs = call(
        [
            "for-each-ref",
            "--format=%(refname)%00%(objectname)%00%(symref)%00%(upstream)",
            "refs/heads",
            "refs/remotes/origin",
            "refs/stash",
        ]
    )
    if not refs.ok:
        return degraded(refs.failure())
    local: dict[str, tuple[str, str]] = {}
    cached: dict[str, str] = {}
    cached_head_target: str | None = None
    has_stash = False
    for line in refs.out.split(b"\n"):
        if not line:
            continue
        fields = _decode(line).split("\0")
        if len(fields) != 4:
            return degraded("failed")
        name, sha, symref, upstream = fields
        if name.startswith("refs/heads/"):
            local[name[len("refs/heads/") :]] = (sha, upstream)
        elif name == "refs/remotes/origin/HEAD":
            if symref.startswith("refs/remotes/origin/"):
                cached_head_target = symref[len("refs/remotes/origin/") :]
        elif name.startswith("refs/remotes/origin/"):
            cached[name[len("refs/remotes/origin/") :]] = sha
        elif name == "refs/stash":
            has_stash = True
    if not all(_safe_text(name) for name in local) or not all(
        _safe_text(name) for name in cached
    ):
        return degraded("failed")

    stash_count = 0
    if has_stash:
        stash = call(["rev-list", "--walk-reflogs", "--count", "refs/stash"])
        if not stash.ok or not stash.out.strip().isdigit():
            return degraded(stash.failure() if not stash.ok else "failed")
        stash_count = int(stash.out.strip())

    linked_directories = _linked_git_directories(common)
    worktrees: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    included: list[_ListedWorktree] = []
    for index, item in enumerate(listed):
        if index == 0 and bare:
            continue
        if not _representable(item.path):
            diagnostics.append({"code": "unrepresentable_path", "path": path})
            continue
        main = index == 0
        git_directory = (
            common if main else linked_directories.get(os.path.realpath(item.path))
        )
        if (
            not main
            and git_directory is not None
            and not item.prunable
            and _claimed_elsewhere(item.path, git_directory)
        ):
            continue
        worktrees.append(
            _worktree_document(runner, item, main=main, git_directory=git_directory)
        )
        included.append(item)
    worktrees.sort(key=worktree_sort_key)

    checked_out: dict[str, list[str]] = {}
    for item in included:
        if item.branch is not None and item.branch.startswith("refs/heads/"):
            checked_out.setdefault(item.branch[len("refs/heads/") :], []).append(item.path)

    has_remote = remote is not None
    memo: dict[tuple[str, str], dict[str, Any]] = {}

    def compare(local_sha: str | None, cached_sha: str | None) -> dict[str, Any]:
        if local_sha is None or cached_sha is None:
            return _comparison("absent")
        if local_sha == cached_sha:
            return _comparison("equal", 0, 0)
        if anchor.shallow:
            return _comparison("differs")
        key = (local_sha, cached_sha)
        if key not in memo:
            memo[key] = _counted_comparison(
                call(["rev-list", "--left-right", "--count", f"{local_sha}...{cached_sha}"]),
                "cached",
            )
        return memo[key]

    branches: list[dict[str, Any]] = []
    for name, (sha, upstream) in local.items():
        shown_upstream: str | None = None
        if upstream.startswith("refs/remotes/"):
            shown_upstream = upstream[len("refs/remotes/") :]
        elif upstream.startswith("refs/heads/"):
            shown_upstream = "./" + upstream[len("refs/heads/") :]
        branches.append(
            {
                "name": name,
                "sha": sha,
                "checked_out_in": sorted(checked_out.get(name, []), key=natural_key),
                "upstream": shown_upstream,
                "upstream_mismatch": (
                    has_remote
                    and upstream != ""
                    and upstream != f"refs/remotes/origin/{name}"
                ),
                "vs_remote": compare(sha, cached.get(name)) if has_remote else None,
            }
        )
    branches.sort(key=branch_sort_key)

    cached_default = None
    local_default = None
    if has_remote:
        cached_sha = cached.get(cached_head_target) if cached_head_target else None
        last_fetched = _last_fetched(common, linked_directories, origin, rules, path if not bare else cwd)
        cached_default = {
            "name": cached_head_target,
            "sha": cached_sha,
            "last_fetched_at": last_fetched,
            "fetched_since_clone": last_fetched is not None,
        }
        if cached_head_target is not None:
            local_sha = local.get(cached_head_target, (None, ""))[0]
            local_default = {
                "name": cached_head_target,
                "sha": local_sha,
                "vs_live": None,
                "vs_cached": compare(local_sha, cached_sha),
            }

    clone = {
        "path": path,
        "bare": bare,
        "status": "ok",
        "partial_clone": partial,
        "shallow": anchor.shallow,
        "cached_default": cached_default,
        "local_default": local_default,
        "cached_vs_live": None,
        "stash_count": stash_count,
        "worktrees": worktrees,
        "branches": branches,
    }
    if not _all_text_safe(clone):
        return degraded("failed")
    context = _CloneContext(
        cwd=cwd,
        ceiling=ceiling,
        partial=partial,
        shallow=anchor.shallow,
        local=local,
        cached=cached,
        ssh_configured=_has_ssh_command(configuration.out),
        common=common,
    )
    return _CloneOutcome(clone, remote, diagnostics, context)


def _remote_text_safe(remote: tuple[str, str] | None) -> bool:
    return remote is None or (_safe_text(remote[0]) and _safe_text(remote[1]))


def _all_text_safe(value: Any) -> bool:
    """Check every string a clone would emit against the document's text bound."""
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, dict):
        return all(_all_text_safe(item) for item in value.values())
    if isinstance(value, list):
        return all(_all_text_safe(item) for item in value)
    return True


def _last_fetched(
    common: str,
    linked_directories: dict[str, str],
    origin: dict[str, str],
    rules: list[tuple[str, str]],
    base: str,
) -> str | None:
    """Return the newest ``FETCH_HEAD`` time that records a fetch of ``origin``."""
    if "url" not in origin:
        return None
    wanted = normalize_remote(_rewrite_instead_of(origin["url"], rules), base)
    if wanted is None:
        return None

    def matches(source: str) -> bool:
        return normalize_remote(source, base) == wanted

    newest: int | None = None
    for directory in [common, *linked_directories.values()]:
        candidate = os.path.join(directory, "FETCH_HEAD")
        details = _lstat(candidate)
        if details is None or not stat.S_ISREG(details.st_mode):
            continue
        if _fetch_head_names_origin(candidate, matches):
            seconds = details.st_mtime_ns // 1_000_000_000
            newest = seconds if newest is None else max(newest, seconds)
    return None if newest is None else _utc_text(newest)


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def _group_default_name(clones: list[dict[str, Any]]) -> str | None:
    names = {
        clone["cached_default"]["name"]
        for clone in clones
        if clone["status"] == "ok"
        and clone["cached_default"] is not None
        and clone["cached_default"]["name"] is not None
    }
    return next(iter(names)) if len(names) == 1 else None


def _assemble_repositories(outcomes: list[_CloneOutcome]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    repositories: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for outcome in outcomes:
        clone = outcome.clone
        if clone is None or clone["path"] in seen_paths:
            continue
        seen_paths.add(clone["path"])
        remote = outcome.remote
        if remote is None:
            repositories.append({"remote": None, "default_branch": None, "clones": [clone]})
            continue
        key, display = remote
        group = grouped.get(key)
        if group is None:
            group = {
                "remote": {
                    "key": key,
                    "display": display,
                    "check": {"status": "off", "reason": "remote_checks_off"},
                },
                "default_branch": {"name": None, "live_sha": None},
                "clones": [],
            }
            grouped[key] = group
            repositories.append(group)
        group["clones"].append(clone)
    for repository in repositories:
        repository["clones"].sort(key=clone_sort_key)
        if repository["remote"] is not None:
            repository["default_branch"]["name"] = _group_default_name(repository["clones"])
    repositories.sort(key=repository_sort_key)
    return repositories


# ---------------------------------------------------------------------------
# Remote checks and live comparisons
# ---------------------------------------------------------------------------

_AUTH_PHRASES = (
    "authentication failed",
    "permission denied",
    "could not read username",
    "could not read password",
)
_UNREACHABLE_PHRASES = (
    "could not resolve host",
    "failed to connect",
    "couldn't connect to server",
    "connection refused",
    "connection reset",
    "connection timed out",
    "operation timed out",
    "no route to host",
    "network is unreachable",
)
_NOT_FOUND_PHRASES = ("not found", "does not appear to be a git repository")


@dataclass(frozen=True)
class _LiveAnswer:
    """One remote's answer, or the category of its failure; never raw text."""

    reason: str | None = None
    name: str | None = None
    sha: str | None = None
    branches: Mapping[str, str] = field(default_factory=dict)


def _failure_reason(outcome: _Outcome) -> str:
    """Name a failed ``ls-remote`` by category, from stderr read under ``LC_ALL=C``."""
    if outcome.timed_out:
        return "timed_out"
    text = outcome.err.decode("utf-8", "replace").lower()
    for reason, phrases in (
        ("authentication_failed", _AUTH_PHRASES),
        ("unreachable", _UNREACHABLE_PHRASES),
        ("not_found", _NOT_FOUND_PHRASES),
    ):
        if any(phrase in text for phrase in phrases):
            return reason
    return "failed"


def _parse_listing(data: bytes) -> _LiveAnswer | None:
    """Read ``ls-remote --symref`` output; ``None`` when it is not that shape."""
    name: str | None = None
    head: str | None = None
    branches: dict[str, str] = {}
    for line in _decode(data).split("\n"):
        if not line:
            continue
        left, tab, right = line.partition("\t")
        if not tab:
            return None
        if left.startswith("ref: "):
            target = left[len("ref: ") :]
            if right == "HEAD" and name is None and target.startswith("refs/heads/"):
                candidate = target[len("refs/heads/") :]
                name = candidate if _safe_text(candidate) else None
        elif _OBJECT_ID.fullmatch(left) and left.strip("0"):
            if right == "HEAD":
                head = left
            elif right.startswith("refs/heads/"):
                branch = right[len("refs/heads/") :]
                if _safe_text(branch):
                    branches[branch] = left
        else:
            return None
    return _LiveAnswer(name=name, sha=head, branches=branches)


def _query_remote(runner: _GitRunner, context: _CloneContext) -> _LiveAnswer:
    """The group's one ``ls-remote``, run from its chosen clone and never retried."""
    # Lifting the base denial gives git back its own transport policy: the
    # caller's inherited value stands, and an unset one stays unset.
    overlay: dict[str, str | None] = {"GIT_ALLOW_PROTOCOL": os.environ.get("GIT_ALLOW_PROTOCOL")}
    if (
        "GIT_SSH_COMMAND" not in os.environ
        and "GIT_SSH" not in os.environ
        and not context.ssh_configured
    ):
        overlay["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    outcome = runner.run(
        ["ls-remote", "--symref", "origin", "HEAD", "refs/heads/*"],
        path=context.cwd,
        ceiling=context.ceiling,
        overlay=overlay,
    )
    if not outcome.ok:
        return _LiveAnswer(_failure_reason(outcome))
    answer = _parse_listing(outcome.out)
    return answer if answer is not None else _LiveAnswer("failed")


def _present_objects(
    runner: _GitRunner, context: _CloneContext, wanted: Sequence[str]
) -> dict[str, str]:
    """Classify each wanted ID as ``present``, ``missing``, ``unknown`` or ``differs``.

    The IDs are arguments because the runner closes stdin; git prints the
    present ones, in no promised order, and exits 0 when some are missing.
    """
    result: dict[str, str] = {}
    for start in range(0, len(wanted), _PROBE_CHUNK):
        chunk = list(wanted[start : start + _PROBE_CHUNK])
        outcome = runner.run(
            ["rev-list", "--no-walk", "--ignore-missing", *chunk],
            path=context.cwd,
            ceiling=context.ceiling,
        )
        if outcome.timed_out or outcome.code is None:
            state = dict.fromkeys(chunk, "unknown")
        elif not outcome.ok:
            state = dict.fromkeys(chunk, "differs")
        else:
            found = set(outcome.out.decode("ascii", "replace").split())
            state = {item: "present" if item in found else "missing" for item in chunk}
        result.update(state)
    return result


def _lendable(context: _CloneContext) -> bool:
    """Whether this clone may lend objects: complete history and a path that
    cannot be split or quoted inside ``GIT_ALTERNATE_OBJECT_DIRECTORIES``."""
    return (
        not context.partial
        and not context.shallow
        and bool(context.common)
        and os.pathsep not in context.common
        and not context.common.startswith('"')
    )


def _apply_live(
    runner: _GitRunner,
    context: _CloneContext,
    clone: dict[str, Any],
    answer: _LiveAnswer,
    siblings: Sequence[tuple[dict[str, Any], _CloneContext]] = (),
) -> None:
    """Replace one clone's cached comparisons with the live ones, in place."""
    name, live_sha = answer.name, answer.sha
    cached_sha = context.cached.get(name) if name is not None else None
    local_sha = context.local.get(name, (None, ""))[0] if name is not None else None
    pairs = [(cached_sha, live_sha)]
    if name is not None:
        pairs.append((local_sha, live_sha))
    for branch in clone["branches"]:
        pairs.append((branch["sha"], answer.branches.get(branch["name"])))
    guarded = context.partial or context.shallow
    wanted = sorted(
        {right for left, right in pairs if left and right and left != right}
    )
    presence = (
        _present_objects(runner, context, wanted) if wanted and not guarded else {}
    )
    memo: dict[tuple[str, str], dict[str, Any]] = {}
    lacking = [item for item in wanted if presence.get(item) == "missing"]
    candidates = [(c, x) for c, x in siblings if _lendable(x)]
    sibling_presence: dict[int, dict[str, str]] = {}

    def lender(right: str) -> tuple[dict[str, Any], _CloneContext] | str | None:
        """The first lendable sibling in natural order holding ``right``, or the
        state (``unknown`` or ``differs``) of a probe that could not say."""
        for index, (sibling, sibling_context) in enumerate(candidates):
            if index not in sibling_presence:
                sibling_presence[index] = _present_objects(runner, sibling_context, lacking)
            state = sibling_presence[index].get(right, "missing")
            if state == "present":
                return sibling, sibling_context
            if state != "missing":
                return state
        return None

    def borrowed(left: str, right: str) -> dict[str, Any]:
        source = lender(right)
        if source is None:
            return _comparison("differs", basis="live")
        if isinstance(source, str):
            return _comparison(source, basis="live")
        sibling, sibling_context = source
        result = _counted_comparison(
            runner.run(
                ["rev-list", "--left-right", "--count", f"{left}...{right}"],
                path=context.cwd,
                ceiling=context.ceiling,
                overlay={
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES": os.path.join(
                        sibling_context.common, "objects"
                    )
                },
            ),
            "live",
        )
        if result["relation"] in ("ahead", "behind", "diverged"):
            result["objects_from"] = sibling["path"]
        return result

    def live(left: str | None, right: str | None) -> dict[str, Any]:
        if left is None or right is None:
            return _comparison("absent", basis="live")
        if left == right:
            return _comparison("equal", 0, 0, "live")
        if guarded:
            return _comparison("differs", basis="live")
        state = presence.get(right, "missing")
        if state in ("unknown", "differs"):
            return _comparison("unknown" if state == "unknown" else "differs", basis="live")
        if (left, right) not in memo:
            if state == "present":
                memo[(left, right)] = _counted_comparison(
                    runner.run(
                        ["rev-list", "--left-right", "--count", f"{left}...{right}"],
                        path=context.cwd,
                        ceiling=context.ceiling,
                    ),
                    "live",
                )
            else:
                memo[(left, right)] = borrowed(left, right)
        return memo[(left, right)]

    clone["cached_vs_live"] = live(cached_sha, live_sha)
    if name is None:
        clone["local_default"] = None
    else:
        previous = clone["local_default"]
        if previous is not None and previous["name"] == name:
            vs_cached = previous["vs_cached"]
        elif local_sha is None or cached_sha is None:
            vs_cached = _comparison("absent")
        elif local_sha == cached_sha:
            vs_cached = _comparison("equal", 0, 0)
        elif context.shallow:
            vs_cached = _comparison("differs")
        else:
            vs_cached = _counted_comparison(
                runner.run(
                    ["rev-list", "--left-right", "--count", f"{local_sha}...{cached_sha}"],
                    path=context.cwd,
                    ceiling=context.ceiling,
                ),
                "cached",
            )
        clone["local_default"] = {
            "name": name,
            "sha": local_sha,
            "vs_live": live(local_sha, live_sha),
            "vs_cached": vs_cached,
        }
    for branch in clone["branches"]:
        branch["vs_remote"] = live(branch["sha"], answer.branches.get(branch["name"]))


def _check_remotes(
    runner: _GitRunner,
    repositories: list[dict[str, Any]],
    contexts: Mapping[str, _CloneContext],
    max_parallel: int,
) -> None:
    """Phase 2 (one query per remote) then phase 3 (live comparisons per clone)."""
    groups = [item for item in repositories if item["remote"] is not None]
    chosen: list[_CloneContext | None] = []
    for group in groups:
        first = next(
            (
                contexts[clone["path"]]
                for clone in group["clones"]
                if clone["status"] == "ok" and clone["path"] in contexts
            ),
            None,
        )
        chosen.append(first)

    def query(context: _CloneContext | None) -> _LiveAnswer:
        # A group with no usable clone makes no call.
        return _query_remote(runner, context) if context is not None else _LiveAnswer("failed")

    with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as pool:
        answers = list(pool.map(query, chosen))
        work: list[tuple[_CloneContext, dict[str, Any], _LiveAnswer, dict[str, Any]]] = []
        for group, answer in zip(groups, answers):
            if answer.reason is not None:
                group["remote"]["check"] = {"status": "failed", "reason": answer.reason}
                continue
            group["remote"]["check"] = {"status": "live", "reason": None}
            group["default_branch"] = {"name": answer.name, "live_sha": answer.sha}
            for clone in group["clones"]:
                if clone["status"] == "ok" and clone["path"] in contexts:
                    work.append((contexts[clone["path"]], clone, answer, group))
        # The pool is drained between the phases, so a blocked query keeps a
        # later comparison from starting.
        def apply(item: tuple[_CloneContext, dict[str, Any], _LiveAnswer, dict[str, Any]]) -> None:
            context, clone, answer, group = item
            siblings = [
                (other, contexts[other["path"]])
                for other in group["clones"]
                if other is not clone
                and other["status"] == "ok"
                and other["path"] in contexts
            ]
            _apply_live(runner, context, clone, answer, siblings)

        list(pool.map(apply, work))


def _render(document: dict[str, Any]) -> str:
    rendered = json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if len(rendered.encode("utf-8")) + 1 > MAX_OUTPUT_BYTES:
        raise CheckoutsOutputTooLargeError("checkouts output exceeds 2 MiB")
    return rendered


def collect_checkouts(
    folders: Iterable[str | os.PathLike[str]],
    *,
    deadline_ns: int,
    call_cap_seconds: float = CALL_CAP_SECONDS,
    max_parallel: int = MAX_PARALLEL_CLONES,
    remote_checks: bool = False,
) -> str:
    """Collect the checkout facts of ``folders`` as one canonical JSON document.

    ``deadline_ns`` is a ``time.monotonic_ns`` instant; calls that cannot start
    or finish by then mark their item ``timed_out`` and the document is still
    produced. ``remote_checks`` adds one ``ls-remote`` per distinct remote and
    the live comparisons built on it; left off, nothing contacts a remote.
    """
    observed_at = _utc_text(int(time.time()))
    discovery = _discover(os.fspath(folder) for folder in folders)
    git_state: dict[str, Any] = {"status": "missing", "version": None}
    repositories: list[dict[str, Any]] = []
    diagnostics = list(discovery.diagnostics)

    executable = _resolve_git()
    runner: _GitRunner | None = None
    if executable is not None:
        runner = _GitRunner(executable, deadline_ns, call_cap_seconds)
        version_outcome = runner.run(["--version"])
        version = _parse_version(version_outcome.out) if version_outcome.ok else None
        if version is not None and _meets_floor(version):
            git_state = {"status": "available", "version": version}
        else:
            git_state = {"status": "too_old", "version": version}

    if runner is not None and git_state["status"] == "available":
        runner.learn_local_environment()
        with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as pool:
            identities = list(pool.map(lambda c: _identify(runner, c), discovery.candidates))
            clones: dict[str, list[_Identity]] = {}
            outcomes: list[_CloneOutcome] = []
            for identity in identities:
                if identity.status == "not_a_repository":
                    diagnostics.append(
                        {"code": "not_a_repository", "path": identity.candidate.path}
                    )
                elif identity.status != "ok":
                    outcomes.append(
                        _CloneOutcome(
                            _unavailable_clone(identity.candidate.path, identity.status)
                        )
                    )
                else:
                    clones.setdefault(identity.common, []).append(identity)
            outcomes.extend(
                pool.map(lambda members: _collect_clone(runner, members), clones.values())
            )
        for outcome in outcomes:
            diagnostics.extend(outcome.diagnostics)
        repositories = _assemble_repositories(outcomes)
        if remote_checks:
            contexts = {
                outcome.clone["path"]: outcome.context
                for outcome in outcomes
                if outcome.clone is not None and outcome.context is not None
            }
            _check_remotes(runner, repositories, contexts, max_parallel)

    unique_diagnostics = {
        (item["code"], item["path"]): item for item in diagnostics
    }.values()
    document: dict[str, Any] = {
        "schema_version": CHECKOUTS_SCHEMA_VERSION,
        "checkouts_digest": None,
        "observed_at": observed_at,
        "git": git_state,
        "remote_checks": "on" if remote_checks else "off",
        "folders": sorted(discovery.folders, key=folder_sort_key),
        "repositories": repositories,
        "diagnostics": sorted(unique_diagnostics, key=diagnostic_sort_key),
    }
    document["checkouts_digest"] = canonical_digest(document, omit=CHECKOUTS_DIGEST_OMITTED)
    return _render(document)
