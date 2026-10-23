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
import shutil
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
    CHECKOUT_BRANCH_CAP,
    CHECKOUTS_DIGEST_OMITTED,
    CHECKOUTS_SCHEMA_VERSION,
    OPERATIONS,
    ProtocolError,
    _keys,
    _object,
    _safe_text,
    branch_sort_key,
    canonical_digest,
    clone_sort_key,
    diagnostic_sort_key,
    folder_sort_key,
    load_strict_json,
    natural_key,
    repository_sort_key,
    worktree_sort_key,
)
from .state import (
    StateError,
    _is_reparse_or_link,
    checkout_cache_path,
    read_checkout_cache,
    write_checkout_cache,
)

CALL_CAP_SECONDS = 10.0
MAX_PARALLEL_CLONES = 8
GIT_FLOOR = (2, 36)
# Git before this release hashes a binary change in ``patch-id`` without its
# object IDs, so two different changes to one binary file get the same patch ID.
# It also has ``patch-id --verbatim``, which the patch rungs need to tell
# changes apart exactly.
PATCH_FLOOR = (2, 39)
OWN_COMMIT_CAP = 2000
COMPARISON_CAP = 5000
SHARED_RANGE_CAP = 5000
# Bounds of the check that a matching change landed where the branch made it:
# base commits tried per patch identity, paths in one change, paths whose
# contents must be merged, bytes of one file read for that merge, and bytes of
# file contents kept for reuse within one confirmation.
LANDING_CAP = 8
CONFIRM_PATH_CAP = 5000
MERGE_PATH_CAP = 16
MERGE_FILE_CAP = 1 << 20
MERGE_RETAIN_CAP = 8 << 20
# The verdict cache keeps at most this many entries; the file stays
# far below the reader's byte bound because an entry is at most about 190 bytes.
CACHE_ENTRY_CAP = 8192
# Verdict git calls stop this long before the deadline so the cache write has time.
VERDICT_RESERVE_NS = 500_000_000

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

    def with_deadline(self, deadline_ns: int) -> _GitRunner:
        """A runner with the same isolation and learned environment but another deadline."""
        other = _GitRunner(self._executable, deadline_ns, self._call_cap)
        other._removed = self._removed
        return other

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

    def _timeout(self) -> float:
        remaining = (self._deadline_ns - time.monotonic_ns()) / 1e9
        return min(self._call_cap, remaining)

    def _prepare(
        self,
        arguments: Sequence[str],
        path: str | None,
        ceiling: str | None,
        overlay: Mapping[str, str | None] | None,
    ) -> tuple[list[str], dict[str, str]]:
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
        return command, environment

    def run(
        self,
        arguments: Sequence[str],
        *,
        path: str | None = None,
        ceiling: str | None = None,
        overlay: Mapping[str, str | None] | None = None,
    ) -> _Outcome:
        """Run one git command; ``overlay`` is applied after the scrub and fixed
        variables of this call only, and a ``None`` value removes a variable.
        Standard input is closed."""
        timeout = self._timeout()
        if timeout <= 0:
            return _Outcome(None, timed_out=True)
        command, environment = self._prepare(arguments, path, ceiling, overlay)
        # Output goes to temporary files, not pipes, so a program git started
        # and left behind cannot keep this call waiting after git is killed.
        with (
            tempfile.TemporaryFile() as out,
            tempfile.TemporaryFile() as err,
        ):
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

    def pipeline(
        self,
        producer: Sequence[str],
        consumer: Sequence[str],
        *,
        path: str,
        ceiling: str,
        overlay: Mapping[str, str | None] | None = None,
    ) -> _Outcome:
        """Run ``producer | consumer`` as two git processes started like ``run``.

        Only the consumer's output is kept, in a temporary file; the producer
        has closed standard input. Both processes are killed on timeout, and the
        outcome succeeds only when both exit 0.
        """
        timeout = self._timeout()
        if timeout <= 0:
            return _Outcome(None, timed_out=True)
        first_command, environment = self._prepare(producer, path, ceiling, overlay)
        second_command, _ = self._prepare(consumer, path, ceiling, overlay)
        with (
            tempfile.TemporaryFile() as out,
            tempfile.TemporaryFile() as first_err,
            tempfile.TemporaryFile() as second_err,
        ):
            try:
                first = subprocess.Popen(
                    first_command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=first_err,
                    env=environment,
                )
            except OSError:
                return _Outcome(None)
            assert first.stdout is not None
            try:
                second = subprocess.Popen(
                    second_command,
                    stdin=first.stdout,
                    stdout=out,
                    stderr=second_err,
                    env=environment,
                )
            except OSError:
                first.stdout.close()
                first.kill()
                first.wait()
                return _Outcome(None)
            first.stdout.close()
            started = time.monotonic()
            try:
                second.wait(timeout=timeout)
                first.wait(timeout=max(0.0, timeout - (time.monotonic() - started)))
            except subprocess.TimeoutExpired:
                for process in (first, second):
                    process.kill()
                    process.wait()
                return _Outcome(None, timed_out=True)
            if first.returncode != 0 or second.returncode != 0:
                failing = first if first.returncode != 0 else second
                stream = first_err if failing is first else second_err
                stream.seek(0)
                return _Outcome(failing.returncode, err=stream.read())
            out.seek(0)
            return _Outcome(0, out.read())


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


def _at_least(version: str | None, floor: tuple[int, ...]) -> bool:
    numeric = _NUMERIC_PREFIX.match(version) if version is not None else None
    if numeric is None:
        return False
    parts = tuple(int(part) for part in numeric.group(0).split("."))
    return parts[: len(floor)] >= floor


def _meets_floor(version: str) -> bool:
    return _at_least(version, GIT_FLOOR)


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
        "verdict_base": None,
        "containment": None,
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
        "verdict": None,
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
    # A clone beyond the supported size costs this listing and nothing more.
    if len(local) > CHECKOUT_BRANCH_CAP:
        return degraded("too_many_branches")
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
                "verdict": None,
                "contained_in": None,
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
        "verdict_base": None,
        "containment": None,
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
    base_name: str | None = None,
) -> _Base | None:
    """Replace one clone's cached comparisons with the live ones, in place.

    Returns the clone's verdict base for ``base_name``, chosen from the same
    presence pass and lender as the comparisons.
    """
    name, live_sha = answer.name, answer.sha
    cached_sha = context.cached.get(name) if name is not None else None
    local_sha = context.local.get(name, (None, ""))[0] if name is not None else None
    pairs = [(cached_sha, live_sha)]
    if name is not None:
        pairs.append((local_sha, live_sha))
    for branch in clone["branches"]:
        pairs.append((branch["sha"], answer.branches.get(branch["name"])))
    guarded = context.partial or context.shallow
    base_head = answer.branches.get(base_name) if base_name is not None else None
    if base_head is None and base_name is not None and base_name == name:
        base_head = live_sha
    held = set(context.cached.values()) | {sha for sha, _ in context.local.values()}
    probed = {right for left, right in pairs if left and right and left != right}
    if base_head is not None and base_head not in held:
        probed.add(base_head)
    wanted = sorted(probed)
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

    if base_name is None:
        return None
    cached_base = context.cached.get(base_name)
    fallback = (
        _Base(base_name, cached_base, "cached", None, None) if cached_base is not None else None
    )
    if base_head is None:
        return fallback
    if guarded:
        return _Base(base_name, base_head, "live", None, None) if base_head == cached_base else fallback
    if base_head in held or presence.get(base_head) == "present":
        return _Base(base_name, base_head, "live", None, None)
    if presence.get(base_head) == "missing":
        source = lender(base_head)
        if isinstance(source, tuple):
            sibling, sibling_context = source
            return _Base(
                base_name,
                base_head,
                "live",
                sibling["path"],
                os.path.join(sibling_context.common, "objects"),
            )
    return fallback


def _check_remotes(
    runner: _GitRunner,
    repositories: list[dict[str, Any]],
    contexts: Mapping[str, _CloneContext],
    max_parallel: int,
) -> dict[str, _Base | None]:
    """Phase 2 (one query per remote) then phase 3 (live comparisons per clone).

    Returns the verdict base of each clone the live comparisons reached."""
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
        bases: dict[str, _Base | None] = {}
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
        def apply(
            item: tuple[_CloneContext, dict[str, Any], _LiveAnswer, dict[str, Any]],
        ) -> tuple[str, _Base | None]:
            context, clone, answer, group = item
            siblings = [
                (other, contexts[other["path"]])
                for other in group["clones"]
                if other is not clone
                and other["status"] == "ok"
                and other["path"] in contexts
            ]
            cached_default = clone["cached_default"]
            base_name = (
                answer.name
                or (cached_default["name"] if cached_default is not None else None)
                or _group_default_name(group["clones"])
            )
            return clone["path"], _apply_live(
                runner, context, clone, answer, siblings, base_name
            )

        bases.update(pool.map(apply, work))
    return bases


# ---------------------------------------------------------------------------
# Merge verdicts
# ---------------------------------------------------------------------------

_PATCH_FLAGS = (
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--no-show-signature",
    "--no-renames",
    "--no-relative",
    "--diff-algorithm=myers",
    "--indent-heuristic",
    "--unified=3",
    "--inter-hunk-context=0",
    "--submodule=short",
    "--ignore-submodules=none",
    "--root",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)
_PATCH_ID = re.compile(r"([0-9a-f]{40}|[0-9a-f]{64}) ([0-9a-f]{40}|[0-9a-f]{64})")
# One raw ``diff-tree -z`` record: old and new mode, old and new object, status.
_RAW_CHANGE = re.compile(
    rb":([0-7]{6}) ([0-7]{6}) ([0-9a-f]{40}|[0-9a-f]{64}) ([0-9a-f]{40}|[0-9a-f]{64}) [A-Z][0-9]*"
)
_ZERO_MODE = re.compile(rb"0+")
# A path's mode and object before and after a change; ``None`` where it is absent.
_PathChange = tuple[tuple[bytes, bytes] | None, tuple[bytes, bytes] | None]
_OCTOPUS_CHUNK = 200


@dataclass(frozen=True)
class _Base:
    """The commit a clone's branches are judged against."""

    name: str
    sha: str
    basis: str
    objects_from: str | None
    alternate: str | None

    def document(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sha": self.sha,
            "basis": self.basis,
            "objects_from": self.objects_from,
        }


@dataclass(frozen=True)
class _VerdictSettings:
    """What every verdict command of one collection shares."""

    pinned: tuple[str, ...]
    overlay: dict[str, str | None]
    patch_rungs: bool
    scratch: str


def _verdict_settings(scratch: str, version: str | None) -> _VerdictSettings:
    """The pinned options, the overlay and the git-version gate of the patch rungs.

    The paths name files that never exist, inside the collection's scratch
    directory, so no attributes or grafts file can be read.
    """
    return _VerdictSettings(
        pinned=(
            "--no-replace-objects",
            "-c",
            "core.quotePath=true",
            "-c",
            "core.attributesFile=" + os.path.join(scratch, "no-attributes"),
        ),
        overlay={
            "GIT_GRAFT_FILE": os.path.join(scratch, "no-grafts"),
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
        },
        patch_rungs=_at_least(version, PATCH_FLOOR),
        scratch=scratch,
    )


def _merged(rung: str) -> dict[str, Any]:
    return {"state": "merged", "rung": rung, "reason": None}


def _unknown(reason: str) -> dict[str, Any]:
    return {"state": "unknown", "rung": None, "reason": reason}


_NOT_MERGED = {"state": "not_merged", "rung": "none", "reason": None}
# The rungs a cache entry may hold; ``ancestor`` and every ``unknown`` never.
CACHE_RUNGS = frozenset({"patch_equivalent", "squash_equivalent", "tree_equal", "none"})


def _call_failure(outcome: _Outcome) -> str:
    return "deadline" if outcome.timed_out else "failed"


def _integers(data: bytes, count: int) -> list[int] | None:
    fields = data.split()
    if len(fields) != count or not all(item.isdigit() for item in fields):
        return None
    return [int(item) for item in fields]


class _Unconfirmed(Exception):
    """A step of the landing check failed or exceeded a bound; ``reason`` names it."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(eq=False)
class _Tip:
    """One distinct commit awaiting a verdict, with what the ladder learns of it.

    Tips compare and hash by identity, so sets and mappings of tips cost a hash.
    """

    sha: str
    verdict: dict[str, Any] | None = None
    error: str | None = None
    left: int = 0
    own: int = 0
    merges: int = 0
    bases: list[str] | None = None
    cached: bool = False
    order: int = 0

    def fail(self, reason: str) -> None:
        if self.error is None:
            self.error = reason


class _Ladder:
    """Decide the merge verdict of tips against one clone's base, read-only."""

    def __init__(
        self,
        runner: _GitRunner,
        context: _CloneContext,
        base: _Base,
        settings: _VerdictSettings,
        cache: Mapping[str, tuple[str, int]] | None = None,
    ) -> None:
        self._runner = runner
        self._context = context
        self._base = base
        self._settings = settings
        self._cache = cache or {}
        # Cache keys that stood in for a verdict, and entries decided by a
        # complete computation in this collection (key -> rung).
        self.used: set[str] = set()
        self.fresh: dict[str, str] = {}
        self._overlay: dict[str, str | None] = dict(settings.overlay)
        if base.alternate is not None:
            self._overlay["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = base.alternate
        # The paths each commit or range changes, kept for the clone's whole run so
        # each is read once.
        self._change_sets: dict[tuple[str, ...], dict[bytes, _PathChange]] = {}
        # File contents read for content merges, kept only within one landing
        # confirmation and below MERGE_RETAIN_CAP, so a blob may be read again.
        self._blobs: dict[str, bytes] = {}
        self._blob_bytes = 0

    def _args(self, arguments: Sequence[str]) -> list[str]:
        return [*self._settings.pinned, *arguments]

    def _git(self, arguments: Sequence[str]) -> _Outcome:
        return self._runner.run(
            self._args(arguments),
            path=self._context.cwd,
            ceiling=self._context.ceiling,
            overlay=self._overlay,
        )

    def _stream(self, producer: Sequence[str], consumer: Sequence[str]) -> _Outcome:
        """Run ``producer | consumer`` in the clone, keeping only the consumer's output."""
        return self._runner.pipeline(
            self._args(producer),
            self._args(consumer),
            path=self._context.cwd,
            ceiling=self._context.ceiling,
            overlay=self._overlay,
        )

    def _patch_ids(self, revisions: Sequence[str]) -> dict[str, str] | _Outcome:
        """Map each non-merge commit of ``revisions`` to its verbatim patch ID."""
        outcome = self._stream(
            ["log", "--no-merges", "-p", "--format=commit %H", *_PATCH_FLAGS, *revisions, "--"],
            ["patch-id", "--verbatim"],
        )
        if not outcome.ok:
            return outcome
        result: dict[str, str] = {}
        for line in outcome.out.decode("ascii", "replace").splitlines():
            matched = _PATCH_ID.fullmatch(line.strip())
            if matched is None:
                return _Outcome(1)
            result[matched.group(2)] = matched.group(1)
        return result

    def decide(
        self, branches: Sequence[tuple[str, str]], detached: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """Return the verdict of every tip in ``branches`` (name, tip) and ``detached``."""
        tips: dict[str, _Tip] = {}
        for sha in [sha for _, sha in branches] + list(detached):
            tips.setdefault(sha, _Tip(sha, order=len(tips)))
        self._ancestors(tips, branches, detached)
        incomplete = self._context.shallow or self._context.partial
        # A shallow or partial clone runs no rung beyond ``ancestor``, so its
        # lookup precedes the size checks.  Otherwise the rungs run first and a
        # cached verdict only stands in for a tip they leave ``unknown``.
        if incomplete:
            self._lookup(tips)
        pending = [tip for tip in tips.values() if tip.verdict is None and tip.error is None]
        if pending and incomplete:
            for tip in pending:
                tip.verdict = _unknown("incomplete_clone")
            pending = []
        # Every later step walks the tips in this order, so a budget that runs
        # out leaves the tips with the freshest saved answer unrecomputed.
        pending.sort(key=self._priority)
        pending = self._sizes(pending)
        mapped: dict[str, str] | None = None
        eligible: set[_Tip] = set()
        if self._settings.patch_rungs:
            eligible, mapped = self._shared(pending)
        # Each tip finishes its whole ladder before the next starts, so a budget
        # that runs out leaves only the tips with the freshest saved answers.
        for tip in pending:
            if mapped is not None and tip in eligible:
                self._patch_rungs(tip, mapped)
            # A tip whose earlier step failed stays unproven: no later rung decides it.
            if tip.verdict is None and tip.error is None and self._tree_equal(tip):
                tip.verdict = _merged("tree_equal")
            if self._settings.patch_rungs and tip.verdict is None and tip.error is None:
                # With every rung run, a tip that proved nothing is decided
                # ``not_merged`` by this run and no cached verdict replaces it.
                tip.verdict = dict(_NOT_MERGED)
        if not incomplete:
            self._lookup(tips)
        complete = self._settings.patch_rungs and not incomplete
        base_sha = self._base.sha
        result: dict[str, dict[str, Any]] = {}
        for sha, tip in tips.items():
            if tip.verdict is None:
                if tip.error is not None:
                    tip.verdict = _unknown(tip.error)
                else:
                    # Only a git below the patch floor reaches here without an
                    # error: with the rungs available every tip was decided above.
                    tip.verdict = _unknown("git_too_old")
            # A tip that recorded a failed or timed-out step never stores a verdict.
            if (
                complete
                and not tip.cached
                and tip.error is None
                and tip.verdict["rung"] in CACHE_RUNGS
                and len(sha) == len(base_sha)
            ):
                self.fresh[f"{sha}.{base_sha}"] = tip.verdict["rung"]
            result[sha] = tip.verdict
        return result

    def _priority(self, tip: _Tip) -> tuple[int, int, int]:
        """Tips with no saved answer first, then the oldest saved answer, then the newest.

        Ties keep the document order, so the work order is deterministic.
        """
        entry = self._cache.get(f"{tip.sha}.{self._base.sha}")
        if entry is None:
            return (0, 0, tip.order)
        return (1, entry[1], tip.order)

    def _lookup(self, tips: Mapping[str, _Tip]) -> None:
        """Let a cached verdict stand in for a tip left undecided or ``unknown``."""
        for sha, tip in tips.items():
            if tip.verdict is not None and tip.verdict["state"] != "unknown":
                continue
            key = f"{sha}.{self._base.sha}"
            entry = self._cache.get(key)
            if entry is None:
                continue
            tip.verdict = dict(_NOT_MERGED) if entry[0] == "none" else _merged(entry[0])
            tip.cached = True
            self.used.add(key)

    # -- step 1 ---------------------------------------------------------

    def _ancestors(
        self,
        tips: dict[str, _Tip],
        branches: Sequence[tuple[str, str]],
        detached: Sequence[str],
    ) -> None:
        base = self._base.sha
        listing = self._git(
            ["for-each-ref", f"--merged={base}", "--format=%(objectname) %(refname)", "refs/heads"]
        )
        if not listing.ok:
            for tip in tips.values():
                tip.fail(_call_failure(listing))
            return
        merged: dict[str, str] = {}
        for line in _decode(listing.out).split("\n"):
            if not line:
                continue
            sha, _, ref = line.partition(" ")
            if ref.startswith("refs/heads/"):
                merged[ref[len("refs/heads/") :]] = sha
        checked: dict[str, bool | str] = {}

        def is_ancestor(tip: _Tip) -> None:
            if tip.sha not in checked:
                outcome = self._git(["merge-base", "--is-ancestor", tip.sha, base])
                if outcome.code == 0 and not outcome.timed_out:
                    checked[tip.sha] = True
                elif outcome.code == 1 and not outcome.timed_out:
                    checked[tip.sha] = False
                else:
                    checked[tip.sha] = _call_failure(outcome)
            state = checked[tip.sha]
            if state is True:
                tip.verdict = _merged("ancestor")
            elif isinstance(state, str):
                tip.fail(state)

        for name, sha in branches:
            tip = tips[sha]
            if tip.verdict is not None:
                continue
            if sha == base or merged.get(name) == sha:
                tip.verdict = _merged("ancestor")
            elif name in merged:
                is_ancestor(tip)
        for sha in detached:
            tip = tips[sha]
            if tip.verdict is None:
                if sha == base:
                    tip.verdict = _merged("ancestor")
                else:
                    is_ancestor(tip)

    # -- step 2 ---------------------------------------------------------

    def _sizes(self, pending: list[_Tip]) -> list[_Tip]:
        base = self._base.sha
        remaining: list[_Tip] = []
        for tip in pending:
            counts = self._git(["rev-list", "--left-right", "--count", f"{base}...{tip.sha}"])
            parsed = _integers(counts.out, 2) if counts.ok else None
            if parsed is None:
                tip.fail(_call_failure(counts))
                tip.verdict = _unknown(tip.error or "failed")
                continue
            tip.left, tip.own = parsed
            if tip.own == 0:
                tip.verdict = _merged("ancestor")
                continue
            if tip.own > OWN_COMMIT_CAP or tip.left > COMPARISON_CAP:
                tip.verdict = _unknown("history_limit")
                continue
            merges = self._git(["rev-list", "--merges", "--count", f"{base}..{tip.sha}"])
            parsed = _integers(merges.out, 1) if merges.ok else None
            if parsed is None:
                tip.fail(_call_failure(merges))
                tip.verdict = _unknown(tip.error or "failed")
                continue
            tip.merges = parsed[0]
            remaining.append(tip)
        return remaining

    # -- steps 3 and 4 --------------------------------------------------

    def _merge_bases(self, tip: _Tip) -> None:
        outcome = self._git(["merge-base", "--all", self._base.sha, tip.sha])
        if outcome.ok:
            tip.bases = outcome.out.decode("ascii", "replace").split()
        elif outcome.code == 1 and not outcome.timed_out and not outcome.out.strip():
            tip.bases = []
        else:
            tip.fail(_call_failure(outcome))

    def _octopus(self, commits: list[str]) -> str | None | _Outcome:
        """The one merge base of ``commits``; ``None`` when they share none."""
        current = commits
        while True:
            groups = [
                current[start : start + _OCTOPUS_CHUNK]
                for start in range(0, len(current), _OCTOPUS_CHUNK)
            ]
            reduced: list[str] = []
            for group in groups:
                outcome = self._git(["merge-base", "--octopus", *group])
                if outcome.code == 1 and not outcome.timed_out and not outcome.out.strip():
                    return None
                fields = outcome.out.split() if outcome.ok else []
                if len(fields) != 1:
                    return outcome if not outcome.ok else _Outcome(1)
                reduced.append(fields[0].decode("ascii", "replace"))
            if len(reduced) == 1:
                return reduced[0]
            current = reduced

    def _shared(self, pending: list[_Tip]) -> tuple[set[_Tip], dict[str, str] | None]:
        """The ladder-wide patch steps; return the tips left for their own patch rungs."""
        base = self._base.sha
        for tip in pending:
            self._merge_bases(tip)
        active = {tip: None for tip in pending if tip.error is None}
        # Each round limits the active tip with the largest comparison (ties:
        # the later one), so one sort fixes the whole victim order.
        victims = sorted(active, key=lambda item: (item.left, item.order))
        shared: str | None = None
        while active:
            known = sorted({item for tip in active for item in tip.bases or []})
            ceiling_commit: str | None = None
            # Without one common merge base the shared range is the whole base
            # history, still bounded by the count below.
            if known and all(tip.bases for tip in active):
                found = self._octopus(known)
                if isinstance(found, _Outcome):
                    for tip in active:
                        tip.fail(_call_failure(found))
                    return set(), None
                ceiling_commit = found
            revisions = [base] if ceiling_commit is None else [base, "--not", ceiling_commit]
            counted = self._git(["rev-list", "--no-merges", "--count", *revisions, "--"])
            parsed = _integers(counted.out, 1) if counted.ok else None
            if parsed is None:
                for tip in active:
                    tip.fail(_call_failure(counted))
                return set(), None
            if parsed[0] <= SHARED_RANGE_CAP:
                shared = ceiling_commit
                break
            victim = victims.pop()
            victim.verdict = _unknown("history_limit")
            del active[victim]
        if not active:
            return set(), None
        mapped = self._patch_ids([base] if shared is None else [base, "--not", shared])
        if isinstance(mapped, _Outcome):
            for tip in active:
                tip.fail(_call_failure(mapped))
            return set(), None
        return set(active), mapped

    def _patch_rungs(self, tip: _Tip, mapped: dict[str, str]) -> None:
        base = self._base.sha
        listing = self._git(["rev-list", "--no-merges", f"{tip.sha}..{base}"])
        if not listing.ok:
            tip.fail(_call_failure(listing))
            return
        # The base commits (newest first) behind each patch identity.
        landings: dict[str, list[str]] = {}
        for item in listing.out.decode("ascii", "replace").split():
            if item in mapped:
                landings.setdefault(mapped[item], []).append(item)
        try:
            if tip.merges == 0:
                own = self._patch_ids([f"{base}..{tip.sha}"])
                if isinstance(own, _Outcome):
                    tip.fail(_call_failure(own))
                    return
                if (
                    len(own) == tip.own
                    and all(item in landings for item in own.values())
                    and all(
                        self._landed(lambda commit=commit: self._commit_changes(commit), identity, landings)
                        for commit, identity in own.items()
                    )
                ):
                    tip.verdict = _merged("patch_equivalent")
                    return
            if tip.bases is not None and len(tip.bases) == 1:
                identified = self._stream(
                    ["diff", *_PATCH_FLAGS, tip.bases[0], tip.sha, "--"],
                    ["patch-id", "--verbatim"],
                )
                fields = identified.out.split() if identified.ok else []
                if not identified.ok:
                    tip.fail(_call_failure(identified))
                    return
                identity = fields[0].decode("ascii", "replace") if fields else ""
                if identity in landings and self._landed(
                    lambda: self._range_changes(tip.bases[0], tip.sha), identity, landings
                ):
                    tip.verdict = _merged("squash_equivalent")
        except _Unconfirmed as failure:
            tip.fail(failure.reason)

    # -- where a matching change landed ------------------------------------

    def _landed(
        self,
        branch: Callable[[], dict[bytes, _PathChange]],
        identity: str,
        landings: Mapping[str, list[str]],
    ) -> bool:
        """Whether one base commit with this patch identity made the branch's change.

        A patch identity ignores where a change lands, so a match proves the
        change only when a commit with that identity made exactly the branch's
        change: its parent-to-commit change is the branch's change, or merging
        the branch's change into the commit's parent gives the commit.  Nothing
        is written anywhere; only objects are read and plain temporary files made.
        """
        candidates = landings[identity]
        changes = branch()
        self._forget_blobs()
        bounded = False
        for commit in candidates[:LANDING_CAP]:
            try:
                if self._same_change(changes, self._commit_changes(commit)):
                    return True
            except _Unconfirmed as failure:
                # A size, path or merge bound on one candidate leaves the older
                # ones to try; a failed call or the deadline ends the check.
                if failure.reason != "history_limit":
                    raise
                bounded = True
        if bounded or len(candidates) > LANDING_CAP:
            raise _Unconfirmed("history_limit")
        return False

    def _forget_blobs(self) -> None:
        self._blobs.clear()
        self._blob_bytes = 0

    def _commit_changes(self, commit: str) -> dict[bytes, _PathChange]:
        """The paths a non-merge commit changes, against its parent or the empty tree."""
        return self._changes(("commit", commit), ["--root", commit])

    def _range_changes(self, old: str, new: str) -> dict[bytes, _PathChange]:
        return self._changes(("range", old, new), [old, new])

    def _changes(
        self, key: tuple[str, ...], revisions: Sequence[str]
    ) -> dict[bytes, _PathChange]:
        known = self._change_sets.get(key)
        if known is not None:
            return known
        outcome = self._git(
            [
                "diff-tree", "-r", "-z", "--raw", "--no-abbrev", "--no-renames",
                "--no-commit-id", "--no-ext-diff", "--ignore-submodules=none",
                *revisions, "--",
            ]
        )  # fmt: skip
        if not outcome.ok:
            raise _Unconfirmed(_call_failure(outcome))
        fields = outcome.out.split(b"\0")
        if fields and not fields[-1]:
            fields.pop()
        if len(fields) % 2 or len(fields) // 2 > CONFIRM_PATH_CAP:
            raise _Unconfirmed("failed" if len(fields) % 2 else "history_limit")
        result: dict[bytes, _PathChange] = {}
        for index in range(0, len(fields), 2):
            matched = _RAW_CHANGE.fullmatch(fields[index])
            if matched is None:
                raise _Unconfirmed("failed")
            result[fields[index + 1]] = tuple(  # type: ignore[assignment]
                None if _ZERO_MODE.fullmatch(mode) else (mode, oid)
                for mode, oid in (matched.group(1, 3), matched.group(2, 4))
            )
        self._change_sets[key] = result
        return result

    def _blob(self, oid: bytes) -> bytes:
        name = oid.decode("ascii")
        known = self._blobs.get(name)
        if known is not None:
            return known
        size = self._git(["cat-file", "-s", name])
        counted = _integers(size.out, 1) if size.ok else None
        if counted is None:
            raise _Unconfirmed(_call_failure(size))
        if counted[0] > MERGE_FILE_CAP:
            raise _Unconfirmed("history_limit")
        content = self._git(["cat-file", "blob", name])
        if not content.ok:
            raise _Unconfirmed(_call_failure(content))
        if self._blob_bytes + len(content.out) > MERGE_RETAIN_CAP:
            self._forget_blobs()
        self._blobs[name] = content.out
        self._blob_bytes += len(content.out)
        return content.out

    def _same_change(
        self, branch: Mapping[bytes, _PathChange], landed: Mapping[bytes, _PathChange]
    ) -> bool:
        """Whether the landed commit's change is the branch's, path by path.

        A path agrees when the commit left the file as the branch's change does,
        or, for text files, when merging the branch's change into the file the
        commit started from gives the file the commit left.
        """
        if branch.keys() != landed.keys():
            return False
        merges: list[tuple[bytes, bytes, bytes, bytes]] = []
        for path, (before, after) in branch.items():
            start, end = landed[path]
            if after == end:
                continue
            regular = {b"100644", b"100755"}
            entries = (before, start, after, end)
            if any(entry is None for entry in entries):
                return False
            modes = {entry[0] for entry in entries}  # type: ignore[index]
            if len(modes) != 1 or not modes <= regular:
                return False
            # Merging onto an unchanged or already-final file cannot give the end.
            if start == before or start == after:
                return False
            merges.append(tuple(entry[1] for entry in entries))  # type: ignore[index,arg-type]
        if len(merges) > MERGE_PATH_CAP:
            raise _Unconfirmed("history_limit")
        return all(self._merges_to(*group) for group in merges)

    def _merges_to(self, before: bytes, start: bytes, after: bytes, end: bytes) -> bool:
        """Whether ``git merge-file`` of ``after`` onto ``start`` (base ``before``) is ``end``."""
        contents = [self._blob(oid) for oid in (start, before, after, end)]
        if any(b"\0" in item for item in contents[:3]):
            return False
        try:
            folder = tempfile.mkdtemp(prefix="merge-", dir=self._settings.scratch)
        except OSError as error:
            raise _Unconfirmed("failed") from error
        try:
            names = ["ours", "base", "theirs"]
            try:
                for name, item in zip(names, contents):
                    with open(os.path.join(folder, name), "wb") as handle:
                        handle.write(item)
            except OSError as error:
                raise _Unconfirmed("failed") from error
            merged = self._runner.run(
                self._args(["merge-file", "-p", "--no-diff3", *names]),
                path=folder,
                ceiling=self._settings.scratch,
                overlay=self._settings.overlay,
            )
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        if merged.timed_out or merged.code is None or merged.code < 0 or merged.code >= 128:
            raise _Unconfirmed(_call_failure(merged))
        # A positive count is the conflicts of the merge: the change is not there.
        return merged.code == 0 and merged.out == contents[3]

    # -- containment ----------------------------------------------------

    def contain(self, branches: Sequence[tuple[str, str]]) -> tuple[str, dict[str, str]]:
        """Group ``branches`` (name, recorded tip) under the branch that holds their changes.

        Returns the outcome (``complete``, ``deadline`` or ``failed``) and, for
        a complete pass, each folded branch's container; any other outcome folds
        nothing.  A branch's own commits are every commit of ``base..tip``,
        merges included; one commit list per branch and one patch stream over
        the branches within the own-commit cap answer every pair in memory.
        """
        own: dict[str, frozenset[str]] = {}
        for name, sha in branches:
            listing = self._git(
                ["rev-list", f"--max-count={OWN_COMMIT_CAP + 1}", sha, "--not", self._base.sha, "--"]
            )
            if not listing.ok:
                return _call_failure(listing), {}
            commits = listing.out.decode("ascii", "replace").split()
            if not all(_OBJECT_ID.fullmatch(item) for item in commits):
                return "failed", {}
            if 0 < len(commits) <= OWN_COMMIT_CAP:
                own[name] = frozenset(commits)
        if len(own) < 2:
            return "complete", {}
        tips = sorted({sha for name, sha in branches if name in own})
        mapped = self._patch_ids([*tips, "--not", self._base.sha])
        if isinstance(mapped, _Outcome):
            return _call_failure(mapped), {}
        identities = {
            name: frozenset(mapped[item] for item in commits if item in mapped)
            for name, commits in own.items()
        }
        ordered = sorted(own, key=lambda name: (len(own[name]), natural_key(name)))
        folded: dict[str, str] = {}
        for name in own:
            changes = own[name]
            for holder in ordered:
                if len(own[holder]) <= len(changes):
                    continue
                rest = changes - own[holder]
                if all(
                    item in mapped and mapped[item] in identities[holder] for item in rest
                ):
                    folded[name] = holder
                    break
        return "complete", folded

    # -- tree rung ------------------------------------------------------

    def _tree_equal(self, tip: _Tip) -> bool:
        tree = self._git(["rev-parse", f"{tip.sha}^{{tree}}"])
        trees = self._git(
            ["log", "--no-show-signature", "--format=%T", f"{tip.sha}..{self._base.sha}", "--"]
        )
        if not tree.ok or not trees.ok:
            tip.fail(_call_failure(tree if not tree.ok else trees))
            return False
        return tree.out.strip() in set(trees.out.split())


def _cached_base(context: _CloneContext, name: str | None) -> _Base | None:
    sha = context.cached.get(name) if name is not None else None
    return _Base(name, sha, "cached", None, None) if name is not None and sha else None


@dataclass(frozen=True)
class _CloneVerdicts:
    """What one clone's verdict work leaves for the rest of the collection."""

    used: frozenset[str] = frozenset()
    fresh: Mapping[str, str] = field(default_factory=dict)


def _assign_clone_verdicts(
    runner: _GitRunner,
    clone: dict[str, Any],
    context: _CloneContext,
    base: _Base | None,
    settings: _VerdictSettings,
    cache: Mapping[str, tuple[str, int]],
) -> _CloneVerdicts:
    """Write one clone's ``verdict_base``, every verdict and its containment.

    Returns the cache keys the verdicts used or decided.
    """
    detached = [
        worktree
        for worktree in clone["worktrees"]
        if worktree["status"] == "ok" and worktree["head"]["branch"] is None
    ]
    if base is None:
        clone["verdict_base"] = None
        clone["containment"] = "no_base"
        for branch in clone["branches"]:
            branch["verdict"] = _unknown("no_base")
        for worktree in detached:
            worktree["verdict"] = _unknown("no_base")
        return _CloneVerdicts()
    clone["verdict_base"] = base.document()
    subjects = [branch for branch in clone["branches"] if branch["name"] != base.name]
    ladder = _Ladder(runner, context, base, settings, cache) if subjects or detached else None
    result = _CloneVerdicts()
    if ladder is not None:
        decided = ladder.decide(
            [(branch["name"], branch["sha"]) for branch in subjects],
            [worktree["head"]["sha"] for worktree in detached],
        )
        for branch in subjects:
            branch["verdict"] = dict(decided[branch["sha"]])
        for worktree in detached:
            worktree["verdict"] = dict(decided[worktree["head"]["sha"]])
        result = _CloneVerdicts(frozenset(ladder.used), dict(ladder.fresh))
    # Containment runs after the verdict rungs, on the same runner and so the
    # same deadline, so the cache write keeps its reserve.  Patch equivalence
    # cannot be proved in a shallow or partial clone or below the patch floor.
    if context.shallow or context.partial:
        clone["containment"] = "incomplete"
    elif not settings.patch_rungs:
        clone["containment"] = "git_too_old"
    elif len(subjects) < 2:
        # One branch has nothing to fold under, so no git call is needed.
        clone["containment"] = "complete"
    else:
        clone["containment"], folds = ladder.contain(
            [(branch["name"], branch["sha"]) for branch in subjects]
        )
        for branch in subjects:
            branch["contained_in"] = folds.get(branch["name"])
    return result


def _assign_verdicts(
    runner: _GitRunner,
    repositories: list[dict[str, Any]],
    contexts: Mapping[str, _CloneContext],
    bases: Mapping[str, _Base | None],
    settings: _VerdictSettings,
    max_parallel: int,
    cache: Mapping[str, tuple[str, int]],
) -> dict[str, _CloneVerdicts]:
    """Give every usable clone its verdict base and verdicts, one clone per task.

    A clone the live comparisons did not reach takes its base from its cached
    ``origin/HEAD`` target, or the name its group's clones agree on.
    Returns each clone's cache keys for the caller's cache write.  ``cache`` is
    only read here; the caller writes it afterwards.
    """
    jobs: list[tuple[dict[str, Any], _CloneContext, _Base | None]] = []
    for repository in repositories:
        agreed = (
            _group_default_name(repository["clones"])
            if repository["remote"] is not None
            else None
        )
        for clone in repository["clones"]:
            context = contexts.get(clone["path"])
            if clone["status"] != "ok" or context is None:
                continue
            if clone["path"] in bases:
                base = bases[clone["path"]]
            else:
                cached_default = clone["cached_default"]
                name = (cached_default["name"] if cached_default is not None else None) or agreed
                base = _cached_base(context, name)
            jobs.append((clone, context, base))

    def decide(job: tuple[dict[str, Any], _CloneContext, _Base | None]) -> tuple[str, _CloneVerdicts]:
        clone, context, base = job
        return clone["path"], _assign_clone_verdicts(runner, clone, context, base, settings, cache)

    with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as pool:
        return dict(pool.map(decide, jobs))


_CACHE_KEY = re.compile(r"(?:[0-9a-f]{40}\.[0-9a-f]{40}|[0-9a-f]{64}\.[0-9a-f]{64})")
_CACHE_KEYS = frozenset({"schema_version", "verdicts"})
# Version 1 stored patch rungs decided by a patch identity that ignored
# whitespace and line numbers; its entries are never served.
_CACHE_SCHEMA = 2
_RETIRED_CACHE_SCHEMA = 1
_CACHE_ENTRY_KEYS = frozenset({"rung", "at"})


def _parse_cache(data: bytes) -> dict[str, tuple[str, int]]:
    """Validate the cache file strictly; any deviation raises ``ProtocolError``."""
    document = _object(load_strict_json(data, "checkout cache"), "checkout cache")
    _keys(document, _CACHE_KEYS, "checkout cache")
    version = document["schema_version"]
    if type(version) is not int or version not in (_RETIRED_CACHE_SCHEMA, _CACHE_SCHEMA):
        raise ProtocolError("checkout cache schema_version is unsupported")
    verdicts = _object(document["verdicts"], "checkout cache verdicts")
    if len(verdicts) > CACHE_ENTRY_CAP:
        raise ProtocolError("checkout cache has too many entries")
    entries: dict[str, tuple[str, int]] = {}
    for key, raw in verdicts.items():
        if _CACHE_KEY.fullmatch(key) is None:
            raise ProtocolError("checkout cache key is invalid")
        entry = _object(raw, "checkout cache entry")
        _keys(entry, _CACHE_ENTRY_KEYS, "checkout cache entry")
        rung, at = entry["rung"], entry["at"]
        if type(rung) is not str or rung not in CACHE_RUNGS:
            raise ProtocolError("checkout cache rung is invalid")
        if type(at) is not int or at < 0:
            raise ProtocolError("checkout cache time is invalid")
        entries[key] = (rung, at)
    # A retired file is well formed but holds nothing to trust; the next write
    # replaces it.
    return entries if version == _CACHE_SCHEMA else {}


def _cache_bytes(entries: Mapping[str, tuple[str, int]]) -> bytes:
    document = {
        "schema_version": _CACHE_SCHEMA,
        "verdicts": {key: {"rung": rung, "at": at} for key, (rung, at) in entries.items()},
    }
    return (
        json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _load_cache(path: str, diagnostics: list[dict[str, Any]]) -> dict[str, tuple[str, int]]:
    """Read the cache once; a missing ``~/.nyx`` or file is empty, a bad file is a diagnostic."""
    try:
        data = read_checkout_cache()
        return {} if data is None else _parse_cache(data)
    except (StateError, ProtocolError, OSError):
        diagnostics.append({"code": "checkout_cache_unreadable", "path": path})
        return {}


def _store_cache(
    path: str,
    results: Iterable[_CloneVerdicts],
    loaded: Mapping[str, tuple[str, int]],
    deadline_ns: int,
    diagnostics: list[dict[str, Any]],
) -> None:
    """Merge this collection's new verdicts into the file with one atomic write.

    Nothing decided by this run, nothing written; a verdict the run decided
    again gets a fresh time so the oldest saved answers are re-checked first
    next time. The file is read again so entries another
    process stored meanwhile survive; a lost race only loses entries, which are
    recomputed.
    """
    fresh: dict[str, str] = {}
    used: set[str] = set()
    for result in results:
        fresh.update(result.fresh)
        used |= result.used
    if not fresh:
        return
    try:
        try:
            existing = read_checkout_cache()
            current = {} if existing is None else _parse_cache(existing)
        except (StateError, ProtocolError, OSError):
            existing, current = None, {}
        now = int(time.time())
        merged = dict(current)
        for key in used:
            if key not in merged and key in loaded:
                merged[key] = loaded[key]
        # A verdict this run decided is verified now, so its entry rotates to
        # the back of the next collection's re-check order.
        for key, rung in fresh.items():
            merged[key] = (rung, now)
        kept = sorted(
            merged.items(),
            key=lambda item: (item[0] not in used and item[0] not in fresh, -item[1][1], item[0]),
        )[:CACHE_ENTRY_CAP]
        payload = _cache_bytes(dict(kept))
        if payload != existing:
            write_checkout_cache(payload, deadline_ns=deadline_ns)
    except (StateError, ValueError, OSError):
        diagnostics.append({"code": "checkout_cache_write_failed", "path": path})


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
        contexts = {
            outcome.clone["path"]: outcome.context
            for outcome in outcomes
            if outcome.clone is not None and outcome.context is not None
        }
        bases: dict[str, _Base | None] = {}
        if remote_checks:
            bases = _check_remotes(runner, repositories, contexts, max_parallel)
        # An account home that cannot be resolved, or a path the document
        # cannot carry, makes the cache unavailable: no read, no write and no
        # diagnostic, since there is no path to report.
        try:
            cache_path: str | None = str(checkout_cache_path())
        except (StateError, OSError):
            cache_path = None
        if cache_path is not None and not _representable(cache_path):
            cache_path = None
        cache = _load_cache(cache_path, diagnostics) if cache_path is not None else {}
        # Verdict calls stop early so the cache write still fits before the deadline.
        verdict_runner = runner.with_deadline(deadline_ns - VERDICT_RESERVE_NS)
        with tempfile.TemporaryDirectory(prefix="nyx-verdicts-") as scratch:
            verdicts = _assign_verdicts(
                verdict_runner,
                repositories,
                contexts,
                bases,
                _verdict_settings(scratch, git_state["version"]),
                max_parallel,
                cache,
            )
        if cache_path is not None:
            _store_cache(cache_path, verdicts.values(), cache, deadline_ns, diagnostics)

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
