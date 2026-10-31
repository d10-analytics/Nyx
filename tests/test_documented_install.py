"""Exercise the published installation commands in their native shell.

Linux keeps the documented console installation and its browser/service
lifecycle.  Windows and macOS publish the private desktop artifact instead, so
the same consumer parses that guidance and, in the desktop artifact lane,
exercises the documented application actions against the staged build rather
than a detached console daemon.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

import pytest
from test_desktop_artifact import (
    ArtifactLayout,
    _assert_claims_released,
    _claim_paths,
    _expected_catalog,
    _inert_path,
    _request,
    _running_gui,
    _sanitized_environment,
    _wait_for_board,
    _wait_for_window,
    _wait_port_free,
    _workspace,
    _write_configuration,
)
from test_wheel_install import (
    _installed_environment,
    _native_claim_probe,
    _venv_executable,
)

from nyx import catalog
from nyx._native_claim import NativeClaim

REPOSITORY_ROOT = Path(__file__).parents[1].resolve()
DOCUMENTS = ("README.md", "docs/running-nyx.md")
EXPECTED = {
    "bash": (
        "python3.12 -m venv .venv",
        ".venv/bin/python -m pip install .",
        ".venv/bin/nyx --setup examples/sample-specifications",
        ".venv/bin/nyx",
        ".venv/bin/nyx --status",
        ".venv/bin/nyx --stop",
    ),
}
BUILD_HEADING = "### Build the private artifact"
# The desktop build is bounded for a native compile, not for one command.  Each
# bound stays below its desktop job's time limit so a hung build fails here with
# its captured output instead of being cut off by the job limit.
_BUILD_TIMEOUT_MINUTES = {"powershell": 20, "bash": 10}
# The documented private artifacts and the application actions the guide
# promises.  The desktop lane consumes these paths against the staged build.
DESKTOP_ARTIFACT_PATHS = {
    "windows": r"dist\desktop\Nyx\Nyx.exe",
    "macos": "dist/desktop/Nyx.app",
}
DESKTOP_ARTIFACT_MARKERS = (
    r"dist\desktop\Nyx\Nyx.exe",
    "dist/desktop/Nyx.app",
    "build_desktop.py",
)
DESKTOP_GUIDANCE_MARKERS = (
    "private internal feasibility build",
    "not a public release",
    "workspace chooser",
    "Change workspace",
    "Quit",
    "already open",
    "Retry",
    "**Cancel** is available only while changing the workspace",
    "returns to the current workspace without quitting",
    "**Close** finishes a blocked close or a board-display failure",
)
STAGE_ORDER_GUIDANCE_MARKERS = {
    "README.md": (
        "The **Board settings** panel saves the board row order in the same "
        "account-local configuration",
        "account-local",
        "browser and desktop views",
        "workspace root",
        "presentation only",
        "does not rename or move",
    ),
    "docs/running-nyx.md": (
        "the toolbar includes a collapsed **Board settings** panel with a "
        "row-order editor",
        "Move up",
        "Move down",
        "Cancel",
        "performs no write",
        "Reset",
        "canonical inventory order",
        "absent",
        "survives browser reload",
        "supported Nyx",
        "failed Save",
    ),
    "docs/workspaces.md": (
        "root-keyed order map",
        "browser and desktop views",
        "Cancel",
        "performs no write",
        "Reset",
        "Unlisted eligible stages",
        "absent",
        "If loading account settings fails, no saved order is available, "
        "so the board uses canonical inventory order",
        "reports the problem for retry",
        "If Save fails, the board keeps displaying the prior saved order "
        "and retains the unsaved editor draft",
        "never renames or moves",
    ),
}
COMPLETION_GUIDANCE_MARKERS = {
    "README.md": (
        "Completion Prerequisite: UUID",
        "literal set of stage names",
        "Board settings",
        "workspace root",
        "evidence verification",
        "Prerequisite: UUID | claim-name",
    ),
    "docs/running-nyx.md": (
        "collapsed **Board settings**",
        "Counts as finished",
        "**Save** to persist the row order and completion policy together",
        "**Cancel** drops both kinds of unsaved change",
        "**Reset** clears the current root's row order and completed-stage set",
        "Hide terminal rows",
        "Reload board settings",
        "configuration revision",
        "moving it out reopens it",
        "recorded workflow assertion",
        "A theme change follows other open tabs",
        "The compact view (**Hide empty rows and columns**), **Hide terminal rows**, and "
        "**Click empty space to deselect** apply per tab until reload",
        '"Board settings changed elsewhere. Reload board settings to replace this unsaved draft."',
        "Choose **Reload board settings** to replace the draft",
    ),
    "docs/workspaces.md": (
        "no implicit `Done` or `Archive` rule",
        "Completion Prerequisite: UUID",
        "missing policy leaves the requirement **unknown**",
        "saved per canonical workspace root",
        "stay keyed to the workspace's original path",
        "does not carry them to the new path and does not delete them",
        "Moving the folder back to its original path restores its order and completion policy",
        "root with no saved names reports unknown",
        "A cancelled or archived item",
        "finished row",
        "mixed dependent item",
    ),
    "docs/specification-reference.md": (
        "one exact completion grammar",
        "one-part `Prerequisite: UUID` is not",
        "never uses a magic claim name",
        "collapsed **Board settings**",
        "temporarily absent names",
        "recorded workflow assertion",
    ),
}
CHECKOUT_RETIRED_WORDING = (
    "A branch created at the default branch's head with no commit of its own is "
    "already part of the default branch, so it reads `merged · cleanup pending ...` "
    "like a branch that was merged.",
    "the clone that fetched and whose newer of last fetch and last move of that "
    "remote-tracking branch is latest",
    "a push moves the pusher's remote-tracking branch but not its fetch time",
    "and is `unknown` when no clone ever fetched",
    "cached-only labels",
    "makes no network connection:",
    "never fetches or contacts a remote",
    "`default <name> <relation> cached <time>`",
    "`cached <time>`",
    "every verdict in it is `unknown` with the reason `no_base`. Every",
    "an unreadable clone, or one with no default branch name",
    "so the document is the same warm or cold",
    "it checks every branch it can before it looks anything up",
    "`deadline` means the 60 seconds ran out first",
    "finishing every check of one branch (including the `tree_equal` rung and the "
    "`not_merged` decision) before it starts the next",
    "A branch whose re-check finishes is remembered again with the time of that check",
    "so the oldest answers are always re-checked first",
    "a clone too large to check within the 60 seconds is covered a few branches at a time",
    "More than eight base commits with one identity, a file over 1 MiB, a change touching "
    "more than 5,000 files or needing more than 16 content merges make the verdict "
    "`unknown` with the reason `history_limit`, never `merged`.",
    "a landing that touched more than 5,000 paths or needed more than 16 content merges",
    "`history_limit` depends on the other branches in the clone",
    "Its only write is the verdict cache file",
    "line only when the configuration is unavailable or the document exceeds 2 MiB",
    "or, when the remote did not answer, the name the clone's own remote-tracking copy",
    "a failed call, a size bound or a clone limit",
    "the plain lines above are unchanged and show no verdicts",
    "The plain lines are otherwise unchanged.",
    "the plain lines show only the Removable summary described below, never a verdict",
    "Two cases have no verdict to wait for, so no local action clears them",
    "A worktree whose directory is gone, shown with the status `failed` and `prunable`",
    "A branch that holds nothing its larger sibling lacks is folded",
    "is fixed, by fetching, deepening a shallow clone or upgrading git",
    "`remote live`, `remote off` or `remote failed <reason>`",
    "A run that re-checks nothing leaves the file untouched",
    "Deleting it only costs time",
    "Its only lasting write is the verdict cache file",
    "when the remote did not answer or has no default branch (an empty remote, or one "
    "with a detached `HEAD`)",
    "Every branch whose check the run finishes, `ancestor` aside, is remembered again "
    "with the time of this run",
    "only for the rungs other than `ancestor`, together with the `not_merged` answer",
    "falls back to cached values and shows `remote failed <reason>`",
    "`remote off`, and every value comes from the remote-tracking branches the clone "
    "already has",
    "`<basis>` is `live` when the remote check was live and `cached` otherwise",
    "`off` means `--no-network` was given;",
    "so only the `ancestor` rung runs",
    "where no check beyond `ancestor` can run",
    "adds, separated by two spaces and in this order, three more elements",
    "including a branch whose comparison the clone's own newer remote-tracking branch "
    "decided, which is compared on the `cached` basis",
    "the default branch of the live check, else of the saved check, else",
    "when neither names one (an empty remote, or one with a detached `HEAD`), the name the "
    "clone's own remote-tracking copy points to",
    "the newest 10 pull requests from the same repository (not a fork)",
    "the newest 10 same-repository pull requests of each branch name",
)
CHECKOUT_GUIDANCE_MARKERS = {
    "docs/running-nyx.md": (
        "`nyx checkouts`",
        "`--json`",
        "one line per clone and one indented line per linked worktree",
        "nyx checkouts --no-network",
        "`--no-network` skips every remote check",
        "`--no-network` is accepted only with `checkouts`",
        "Nyx tries the eight newest base commits with the identity, and a match among "
        "them proves `merged`",
        "a file over 1 MiB that landed unchanged needs no content merge",
        "only when no commit Nyx tried matched and the check could not finish",
        "`unreachable`, `not_found`, `authentication_failed`, `timed_out` or `failed`",
        "`branches <basis> local-only <n>`",
        "`default <name> live <relation> cached <relation> tracking live <relation> "
        "fetched <time>`",
        "`default <name> cached <relation> fetched <time>`",
        "`never fetched`",
        "`no live default`",
        "`no cached default`",
        "`no origin remote`",
        "`local-only <n>`",
        "`unknown <m>`",
        "`cached` only ever labels a basis",
        "By default `nyx checkouts` is networked",
        "it asks each distinct `origin` remote once",
        "the count may be borrowed read-only from a sibling clone",
        "Nyx's own SSH runs in batch mode",
        "unless you configured an SSH command yourself",
        "`--no-network` makes no network connection at all",
        "The command is read-only",
        "writes no alternates file and never fetches",
        "Checkout folders are set per workspace",
        "A settings editor arrives with the Checkouts view",
        "cannot be combined with `--setup`, `--stop` or `--status`",
        "exits 1 with one line only when the configuration is unavailable, the document "
        "exceeds 2 MiB, or a temporary file or directory it needs cannot be made",
        "`nyx: checkouts: unable to collect (<reason>)`",
        "`no checkout folders are configured`",
        "`checkout_cache_unreadable` and `checkout_cache_write_failed`",
        "appears only in the `--json` document: the plain lines never show a branch's "
        "verdict itself, only the Removable summary described below",
        "A non-default branch is contained in another non-default local branch of the same clone",
        "A patch-identity match follows the change's patch, not the place it landed",
        "so it also groups a change whose identical hunk sits at a different place in the "
        "container's own history",
        "the grouping then names a container that does not hold the change",
        "This is a grouping only, never a statement about removal",
        "a branch folds under the container with the fewest own commits, provided that "
        "container has strictly more than the branch",
        "Two branches at the same commit never fold into each other",
        "A branch with more than 2,000 own commits neither folds nor contains",
        "`containment` is one of six values",
        "`complete`: every branch was examined",
        "`deadline`: the 60 seconds ran out first, or a single git call reached its 10-second cap",
        "`failed`: a git call failed",
        "`no_base`: the clone has no base to measure own commits from",
        "`incomplete`: the clone is shallow or partial",
        "`git_too_old`: git is older than 2.39",
        "It is a fact about data loss, not a prediction of any cleanup tool's checks",
        "`not_merged`, cleared when the branch is merged into the base",
        "`verdict_unknown`, cleared when the cause of its `unknown` verdict is fixed",
        "a later run that finishes in time with no failing git call decides a `deadline` "
        "or `failed` verdict",
        "a branch too large to check, `history_limit`, stays blocked until its commits are "
        "in the base's history or the branch changes to fit the bounds",
        "is fixed, in the same ways as a branch's, and a later run then decides it",
        "`checked_out_in_main_worktree`, cleared by switching the main worktree to another branch",
        "`worktree_not_clean` and `worktree_locked`, cleared by cleaning that worktree and "
        "unlocking it",
        "`not_clean` is cleared by committing, stashing or discarding",
        "`locked`, cleared by unlocking it",
        "A worktree's `not_merged` is cleared when its work lands on the remote's default "
        "branch and a later run sees it there",
        "Its `verdict_unknown` is cleared when the cause of the `unknown` verdict is fixed",
        "A worktree on the base branch, or on a branch with no commits yet, has no branch "
        "verdict that shows its commit is merged, so it stays `verdict_unknown` while it is "
        "on that branch",
        "a first commit on the unborn branch gives the branch a normal verdict on the next run",
        "A worktree whose directory is gone and that is not locked, shown with the status "
        "`failed` and `prunable`",
        "Git stops calling a locked worktree prunable, so a locked worktree whose directory "
        "is gone reads as unreadable: it is `not_clean` and `locked`, its branch is "
        "`worktree_not_clean` and `worktree_locked`, and its plain line is "
        "`failed blocker not_clean`",
        "Unlocking it lets a later run read it as prunable",
        "is `not_clean` for as long as it cannot be read, and stays blocked until it is "
        "readable again or is removed",
        "Ignored files never block",
        "A clone's `removable_count` gives `branches` and `worktrees`",
        "a branch and the worktree holding it count once each",
        "a clone line ends with `removable <b> branches, <w> worktrees`",
        "Each linked worktree line ends with `removable` or `blocker <first blocker>`",
        "`failed removable` is a worktree whose directory is gone and whose branch is merged",
        "keeps its `<path> <status>` line and gains no count",
        "`verdict_base` names the default branch the verdicts are judged against",
        '"base stale"',
        "which can be older than the remote until you fetch",
        "`ancestor`: the branch's commit is already in the base's history",
        "`patch_equivalent`: the branch has no merge commits",
        "`squash_equivalent`: the branch's whole change",
        "the same patch identity, whitespace included",
        "Both patch rungs also require that the base commit with the matching change made it "
        "where the branch made it",
        "indented differently or made at another place in a repeated block, never reads as merged",
        "a branch with its own root commit, sharing no history with the base, is judged the same way",
        "it writes to no repository and does not even touch the modification time of an object file",
        "can still read `not_merged` when the landing commit left a binary file",
        "`unknown` with the reason `history_limit`, never `merged`, only when no commit",
        "more than 16 content merges, or more than eight base commits with the identity",
        "a branch change or a landing that touched more than 5,000 paths",
        "`history_limit` is a cost bound, not a fact about the branch, and the bound on the "
        "shared comparison depends on the other branches in the clone",
        "`tree_equal`: the branch's file contents are identical",
        "`not_merged` has the rung `none`",
        "`no_base`",
        "A clone with no `origin` remote or no default branch name has no base",
        "neither the remote's current commit for it (in the clone or borrowed from a sibling) "
        "nor its own remote-tracking copy of that branch",
        "Every verdict in a clone with no base is `unknown` with the reason `no_base`",
        "has no branches, no worktrees and no base, so it has no verdicts at all",
        "It never replaces a verdict the run decides itself",
        "a warm run differs from a cold run only where the cold run says `unknown`",
        "It runs the size checks, the merge bases and the shared comparison for every "
        "remaining branch first, then each branch's own checks, its `tree_equal` rung and the "
        "`not_merged` decision finish before the next branch starts",
        "those with no remembered answer first, then those with the oldest remembered answer, "
        "then the newest",
        "A verdict the run decides always wins over a remembered one, and a remembered answer "
        "stands in only where that re-check does not finish",
        "is remembered again with the time of this run",
        "a clone is covered a few branches at a time over successive runs, but only within "
        "these limits",
        "A budget that ends inside the shared steps (the size checks, merge bases and shared "
        "comparison) decides no new branch",
        "Branches that can never be remembered, because one of their steps fails or reaches "
        "the 10-second cap or because they are `history_limit`, are worked first on every run",
        "The 8,192 remembered answers are shared by all clones",
        "`too_many_branches` when the clone has more than 100 local branches (the default "
        "branch included)",
        "so it shows no facts and no verdicts",
        "lends them no objects and takes no part in choosing their default branch name",
        "A checkout that could not be read has no Removable elements",
        "a remembered answer never replaces a `tree_equal` verdict that the cold run decides",
        "which can only make matching stricter",
        "so once the attributes are gone a run that decides the branch reports its own verdict",
        "`incomplete_clone` means the clone is shallow or partial",
        "`git_too_old` means git is older than 2.39",
        "the two patch rungs, `patch_equivalent` and `squash_equivalent`, need 2.39 for exact "
        "patch identities and telling two different changes to the same binary file apart",
        "`history_limit` means the branch or the comparison was too large",
        "`deadline` means one git call reached its 10-second cap or the 60 seconds, less the "
        "half second kept for the cache write, ran out",
        "`failed` means a git call failed",
        "`~/.nyx/cache/checkouts.json`",
        "The cache is used only when `~/.nyx` already exists",
        "are made in the system's temporary directory and removed before it exits",
        "A run that decides no new verdict and makes no successful remote check leaves "
        "the file untouched; every successful remote check rewrites it with that check's time",
        "Deleting it costs time, because the verdicts are recomputed, and it loses the saved "
        "remote checks: offline runs then show only the clone's own fetched refs until a "
        "networked run checks again",
        "Its only lasting write is the cache file of remembered verdicts and the last "
        "successful remote checks, which changes no repository",
        "`remote live`, `remote recorded <reason> <time>`, `remote off` or `remote failed "
        "<reason>`",
        "`recorded` means it did not get one, and Nyx used the last successful check of that "
        "remote it had saved",
        "`off` means `--no-network` was given and no check of that remote was saved",
        "`remote_checks_off` when `--no-network` was given",
        "`<basis>` is `live` under a live check, `recorded` under a recorded one and `cached` "
        "when the check is off or failed with no saved check",
        "`branches recorded` is the status of the check; under it an individual branch that "
        "the clone's own newer remote-tracking branch decided is compared on the `cached` "
        "basis",
        "Under a recorded check each comparison with the remote shows `recorded` in place of "
        "`live`",
        "`no recorded default`",
        "`pr live`, `pr recorded <reason> <time>`, `pr off <reason>` or "
        "`pr unavailable <reason>`",
        "It is on every line of a clone with an `origin` remote",
        "The branch is the default branch named by the check the verdicts rest on: the live "
        "check when it answered, otherwise the saved check",
        "and when there is no check, it is the name the clone's own remote-tracking copy "
        "points to, or else the name every clone of the remote agrees on",
        "A saved check's name is never used while the live check answered",
        "The `recorded` basis is the commit the last successful check saw for that name",
        "a saved check's commit counts as the remote's current commit here",
        "`pull_request`: GitHub lookup",
        "only the `ancestor` and `pull_request` rungs run",
        "`ancestor` and `pull_request` aside, is remembered again with the time of this run",
        "only for the rungs other than `ancestor` and `pull_request`, together with the "
        "`not_merged` answer",
        "The same file keeps the last successful remote checks",
        "A successful check replaces only its own half, and a failed check changes nothing",
        "All saved checks together are held to 2 MiB, newest first",
        "a remote whose saved check alone exceeds that is not kept and behaves as if it had none",
        "read as empty without a diagnostic and replaced on the next write",
        "falls back to its last saved check, shown as `remote recorded <reason> <time>`, or, "
        "with none saved, to the clone's cached values, shown as `remote failed <reason>`",
        "answers from the checks that earlier networked runs saved, shown as "
        "`remote recorded remote_checks_off <time>`",
        "A remote with no saved check shows `remote off`",
        "GitHub lookup is optional and off until the workspace's `github_lookup` setting "
        "turns it on",
        "found in an absolute `PATH` directory (`gh.exe` on Windows) and logged in",
        "one `gh` request per GitHub repository, however many clones share it",
        "and `--no-network` makes none",
        "the newest 10 pull requests whose head branch has each local branch name",
        "judged from the `origin` URL as configured, even when an `insteadOf` rewrite sends "
        "git elsewhere",
        "the host must be `github.com` or a `*.ghe.com` host, and the path exactly `owner/repo`",
        "Any other host, a GitHub Enterprise Server host included, is `not_github` and is "
        "never asked",
        "`setting_off`: `github_lookup` is off for the workspace",
        "`remote_checks_off`: `--no-network` was given",
        "`not_github`: the `origin` URL is not a GitHub repository",
        "`gh_missing`: no `gh` was found",
        "`not_authenticated`: `gh` has no valid login for that host",
        "`not_found`: GitHub does not show the repository to that login",
        "`rate_limited`: GitHub's rate limit was reached",
        "`timed_out`: `gh` did not answer within 10 seconds or the time left",
        "`failed`: any other failure",
        "A saved lookup stands in when the request was not made because of "
        "`remote_checks_off` or failed with any reason except `setting_off` and `not_github`",
        "each branch in `--json` lists `pull_requests`: of the newest 10 pull requests whose "
        "head branch has the branch's name, those from the same repository (not a fork), so "
        "fewer than 10 may be listed and an older same-repository request is not",
        "the same-repository pull requests among the newest 10 from any repository",
        "`head_matches`, which is true when the request's head is the branch's current commit",
        "reports `merged` when a merged pull request from the same repository into the base "
        "branch has a head branch that is a local branch at the commit",
        "A pull request is never matched on the commit alone",
        "one whose `head_matches` is false decides only if it is merged into the base and its "
        "head contains the commit; otherwise the git checks decide",
        "A commit no local branch holds takes no `pull_request` rung",
        "The rung is never remembered in the cache file",
        "Recorded values are the last successful remote check and pull-request lookup of each "
        "remote",
        "`remote.check` carries `checked_at` only when its status is `recorded`, while "
        "`pr_lookup` always carries it",
        "The clone's own newer push or fetch takes precedence",
        "its reflog shows an update at or after the saved check's time, Nyx keeps the clone's "
        "own value, on the `cached` basis",
        "when reflogs are disabled, when the branch was deleted with `git push --delete` or "
        "the remote-tracking branch pruned with `git fetch --prune`, or when a force-push "
        "moved the remote branch back to an older commit",
        "the saved value then stands until a networked run checks again",        "a size bound, a shallow or partial clone or a git older than 2.39",
    ),
    "docs/workspaces.md": (
        '"schema_version": 4',
        '"checkout_settings"',
        '"github_lookup": false',
        "`github_lookup` is the workspace's switch for the optional GitHub pull-request "
        "lookup of `nyx checkouts`",
        "The settings editor that arrives with the Checkouts view will set it",
        "The key under `checkout_settings` is the record's own `specification_root` value",
        "Each entry in `folders` must be an absolute path, and the list must be sorted",
        "write each backslash as `\\\\` in JSON",
        "Stop Nyx first",
        "`nyx --stop` on Linux, or quit the desktop application",
        "rebuild the desktop application at this version",
        "an older application refuses a schema 4 record",
        "the configuration becomes unavailable",
        "Undoing the edit restores it",
    ),
}
PORT_CONFLICT_GUIDANCE_MARKERS = (
    "**Nyx says port 8765 is already in use:**",
    "Linux", "`ss -ltnp 'sport = :8765'`",
    "macOS", "`lsof -nP -iTCP:8765 -sTCP:LISTEN`",
    "Windows PowerShell", "`Get-NetTCPConnection -LocalPort 8765 -State Listen`",
    "`OwningProcess` is the process ID",
    "Close that program", "run `nyx` again", "or choose **Retry**",
    "Nyx never switches ports",
    "Nyx runtime could not start; retry",
    "excluded or reserved port range", "no owning program",
    "`netsh interface ipv4 show excludedportrange protocol=tcp`",
)
RECOVERY_GUIDANCE_HEADING = "### Recover an unreadable runtime record"
RECOVERY_GUIDANCE_MARKERS = (
    "unreadable or malformed and has no owning instance",
    "`Runtime: unknown`",
    "`runtime state unavailable`",
    "`nyx --stop` removes that record exactly as it removes a valid stale record",
    "prints the ordinary `stopped` result",
    "no separate recovery message",
    "With no owning instance, starting with `nyx` still refuses while the record "
    "cannot be read, and its message names the recovery step and ends "
    "`; run 'nyx --stop' to recover`",
    "`; run 'nyx --stop' to recover`",
    "Recovery requires that no instance owns the runtime",
    "a live owner still refuses both start and stop",
)
EVENT_COMPLETION_EXAMPLE = (
    "# Organize the neighborhood festival\n"
    "Package ID: 123e4567-e89b-42d3-a456-426614174100\n"
    "Status: planning\n"
    "Completion Prerequisite: 123e4567-e89b-42d3-a456-426614174102\n"
    "Prerequisite: 123e4567-e89b-42d3-a456-426614174102 | venue-confirmed\n"
    "Prerequisite: 123e4567-e89b-42d3-a456-426614174102 | permit-approved"
)
_START_TIMEOUT = 90.0


def _fenced_blocks(text: str, language: str) -> list[str]:
    return re.findall(rf"^```{language}\n(.*?)^```", text, re.MULTILINE | re.DOTALL)


def _documented_commands(document: str, language: str) -> tuple[str, ...]:
    blocks = _fenced_blocks(document, language)
    assert blocks, f"missing {language} installation block"
    commands = tuple(blocks[0].splitlines())
    assert commands == EXPECTED[language], f"changed {language} installation commands"
    return commands


def _published_build_block(document: str, language: str) -> str:
    """Return the one ``language`` block published under the build heading."""

    assert document.count(BUILD_HEADING + "\n") == 1, "missing build heading"
    kept: list[str] = []
    fenced = False
    for line in document.split(BUILD_HEADING + "\n", 1)[1].splitlines(keepends=True):
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(r"#{1,3} ", line):
            break
        kept.append(line)
    section = "".join(kept)
    blocks = _fenced_blocks(section, language)
    assert len(blocks) == 1, f"expected one {language} build block, found {len(blocks)}"
    assert blocks[0].strip(), f"empty {language} build block"
    return blocks[0]


def _lane_build_language() -> str | None:
    if os.name == "nt":
        return "powershell"
    if sys.platform == "darwin":
        return "bash"
    return None


@pytest.mark.parametrize("language", EXPECTED)
def test_both_documents_publish_exact_installation_commands(language):
    blocks = [
        _documented_commands((REPOSITORY_ROOT / name).read_text(encoding="utf-8"), language)
        for name in DOCUMENTS
    ]
    assert blocks[0] == blocks[1]


@pytest.mark.parametrize(
    ("language", "index"),
    [
        (language, index)
        for language, commands in EXPECTED.items()
        for index in range(len(commands))
    ],
)
def test_document_contract_rejects_changed_or_deleted_commands(language, index):
    commands = list(EXPECTED[language])
    for replacement in ("", "echo substituted command"):
        changed = commands.copy()
        changed[index] = replacement
        document = f"```{language}\n" + "\n".join(changed) + "\n```\n"
        with pytest.raises(AssertionError, match="changed"):
            _documented_commands(document, language)


def test_readme_and_guide_publish_the_same_powershell_build_block():
    guide = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    readme_blocks = _fenced_blocks(readme, "powershell")
    assert len(readme_blocks) == 1, "README must publish exactly one PowerShell block"
    selected = _published_build_block(guide, "powershell")
    assert readme_blocks[0] == selected
    assert "scripts" in selected and "build_desktop.py" in selected


SYNTHETIC_GUIDE = (
    "# Running Nyx\n\n"
    "## Linux\n\n```bash\nlinux-first\n```\n\n"
    "{heading}\n\n"
    "```powershell\n{powershell}\n```\n\n"
    "```bash\n{macos}\n```\n\n"
    "### Open\n\n```bash\nafter-section\n```\n"
)


def _synthetic_guide(powershell="ps-build", macos="mac-build", heading=BUILD_HEADING):
    return SYNTHETIC_GUIDE.format(heading=heading, powershell=powershell, macos=macos)


def test_build_block_is_selected_by_heading_and_language_not_position():
    guide = _synthetic_guide()
    assert _published_build_block(guide, "bash") == "mac-build\n"
    assert _published_build_block(guide, "powershell") == "ps-build\n"
    # The first and last bash blocks of the document are never the build block.
    assert _published_build_block(guide, "bash") not in ("linux-first\n", "after-section\n")


def test_changing_a_command_under_the_heading_changes_the_selected_block():
    before = _published_build_block(_synthetic_guide(), "bash")
    after = _published_build_block(_synthetic_guide(macos="mac-build\nextra"), "bash")
    assert after != before
    assert after == "mac-build\nextra\n"
    # A change outside the heading leaves the selection alone.
    other = _synthetic_guide().replace("linux-first", "linux-changed")
    assert _published_build_block(other, "bash") == before


def test_build_selection_rejects_missing_heading_or_missing_language_block():
    with pytest.raises(AssertionError, match="missing build heading"):
        _published_build_block(_synthetic_guide(heading="### Something else"), "bash")
    with pytest.raises(AssertionError, match="expected one"):
        _published_build_block(_synthetic_guide().replace("```powershell", "```text"), "powershell")


def test_build_selection_keeps_comment_lines_inside_the_selected_block():
    # A shell comment inside the block looks like a heading; it must neither end
    # the section nor drop the commands after it.
    macos = "mac-venv\n# then build\nmac-build"
    guide = _synthetic_guide(macos=macos)
    assert _published_build_block(guide, "bash") == macos + "\n"


def test_real_guide_selects_the_macos_build_block_not_the_linux_one():
    guide = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    selected = _published_build_block(guide, "bash")
    linux = "\n".join(EXPECTED["bash"]) + "\n"
    assert selected != linux
    assert "build_desktop.py" in selected
    assert "venv" in selected


def test_documents_publish_the_private_desktop_guidance():
    for name in DOCUMENTS:
        document = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
        for marker in DESKTOP_ARTIFACT_MARKERS:
            assert marker in document, (name, marker)
    guide = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    for marker in DESKTOP_GUIDANCE_MARKERS:
        assert marker in guide, marker
    for name in DOCUMENTS:
        assert "**Revalidate**" not in (REPOSITORY_ROOT / name).read_text(encoding="utf-8"), name


@pytest.mark.parametrize("name", [
    "README.md", "docs/running-nyx.md", "docs/workspaces.md",
    "docs/specification-reference.md", "examples/sample-specifications/README.md",
])
def test_documents_remove_stage_hiding_and_retain_finished_rows(name):
    document = re.sub(r"\s+", " ", (REPOSITORY_ROOT / name).read_text(encoding="utf-8"))
    assert not re.search(
        r"hide-stage|show-all-stages|visible stages|stage visibility|configured-hidden|"
        r"hidden stages|hidden completed stage|hidden prerequisite|hidden or absent",
        document, re.IGNORECASE,
    )
    assert "Hide terminal rows" in document
    assert "Board settings" in document


def test_documents_publish_the_saved_stage_order_contract():
    normalized_documents = {}
    for name, markers in STAGE_ORDER_GUIDANCE_MARKERS.items():
        document = re.sub(r"\s+", " ", (REPOSITORY_ROOT / name).read_text(encoding="utf-8"))
        normalized_documents[name] = document
        for marker in markers:
            assert marker in document, (name, marker)

    obsolete_fallback = (
        "If loading or saving account settings fails, the board falls back to "
        "canonical inventory order and keeps the last saved order unchanged."
    )
    assert obsolete_fallback not in normalized_documents["docs/workspaces.md"]


def test_documents_publish_the_explicit_completion_contract():
    for name, markers in COMPLETION_GUIDANCE_MARKERS.items():
        document = re.sub(r"\s+", " ", (REPOSITORY_ROOT / name).read_text(encoding="utf-8"))
        for marker in markers:
            assert marker in document, (name, marker)


def test_documents_publish_the_checkout_contract():
    for name, markers in CHECKOUT_GUIDANCE_MARKERS.items():
        document = re.sub(r"\s+", " ", (REPOSITORY_ROOT / name).read_text(encoding="utf-8"))
        for marker in markers:
            assert marker in document, (name, marker)


def test_checkout_documentation_drops_the_cached_only_wording():
    document = re.sub(
        r"\s+", " ", (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    )
    for wording in CHECKOUT_RETIRED_WORDING:
        assert wording not in document, wording


def test_documented_header_limits_match_the_catalog():
    document = (REPOSITORY_ROOT / "docs" / "specification-reference.md").read_text(encoding="utf-8")
    normalized = re.sub(r"\s+", " ", document)
    limit = f"{catalog._HEADER_BYTE_LIMIT:,}"
    assert "A single leading UTF-8 byte order mark (BOM) is ignored" in normalized
    assert f"The header limit is {limit} bytes" in normalized
    assert f"A whole `program.md` descriptor obeys the same {limit}-byte limit" in normalized


def test_documented_output_limit_matches_the_catalog():
    document = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    heading = "## If something looks wrong"
    assert heading in document
    section = document.split(heading, 1)[1].split("\n## ", 1)[0]
    normalized = re.sub(r"\s+", " ", section)
    mebibytes = catalog.MAX_OUTPUT_BYTES / (1024 * 1024)
    limit = f"{mebibytes:g} MiB"
    assert "`producer_output_too_large`" in normalized
    assert f"larger than the {limit} catalog limit" in normalized


def test_event_walkthrough_shows_whole_item_and_named_outcomes():
    document = (REPOSITORY_ROOT / "docs" / "workspaces.md").read_text(encoding="utf-8")
    assert EVENT_COMPLETION_EXAMPLE in document
    assert "event plan's whole-item requirement" in document
    assert "literal `Done` stage" in document
    assert "separate named outcomes" in document


def test_port_conflict_guidance_matches_the_public_message():
    from nyx import runtime

    document = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    assert document.splitlines()[0] == "# Running Nyx"
    section = document.split("## If something looks wrong\n", 1)[1].split("\n## ", 1)[0]
    normalized = re.sub(r"\s+", " ", section)
    for marker in PORT_CONFLICT_GUIDANCE_MARKERS:
        assert marker in normalized, marker
    assert "```" not in section
    assert "If something looks wrong" in runtime.PORT_UNAVAILABLE_MESSAGE
    assert "Running Nyx" in runtime.PORT_UNAVAILABLE_MESSAGE


def test_documents_publish_the_invalid_record_recovery_guidance():
    document = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    # The recovery guidance must not displace the installation block: the first
    # fenced block in each published language stays exactly as documented.
    for language in EXPECTED:
        _documented_commands(document, language)
    assert RECOVERY_GUIDANCE_HEADING in document, "missing invalid-record recovery guidance"
    section = document.split(RECOVERY_GUIDANCE_HEADING, 1)[1].split("\n#", 1)[0]
    normalized = re.sub(r"\s+", " ", section)
    for marker in RECOVERY_GUIDANCE_MARKERS:
        assert marker in normalized, marker
    assert "```" not in section


_ALL_DOCUMENTS = (
    "README.md", "docs/running-nyx.md", "docs/workspaces.md",
    "docs/specification-reference.md",
)
UUID_COMMANDS = (
    "python3 -c 'import uuid; print(uuid.uuid4())'",
    "uuidgen | tr '[:upper:]' '[:lower:]'",
    "[guid]::NewGuid().ToString()",
)
UUID_RULE_MARKERS = (
    "Every UUID field (`Package ID`, `Program ID`, `Program Membership`, "
    "`Prerequisite`, `Completion Prerequisite`, and `Superseded By`) takes a "
    "lowercase version 4 UUID only",
    "An invalid `Package ID` makes the item lose its identity, so ",
    "An invalid `Prerequisite` or `Completion Prerequisite` shows "
    "**unresolved target**",
    "An invalid `Program Membership` or `Superseded By` leaves the item's "
    "identity intact",
)
PROGRAM_GRAMMAR_MARKERS = (
    "holds exactly one `Program ID` line and one `Program Title` line and no "
    "other non-empty content",
    "The ID must equal the program directory name",
    "the title must be 1–120 characters",
    "inside a canonical UUID program directory makes every program membership "
    "in the workspace **unknown**",
    "program-title search finds nothing, while **Workspace issues** names that "
    "descriptor",
    "A program directory whose name is not a lowercase version 4 UUID is "
    "skipped without a diagnostic",
)
UNRESOLVED_CHECK_FIRST = "check **Workspace issues** first for an unreadable directory or specification file that made identity coverage incomplete"
UNRESOLVED_MARKERS = (
    UNRESOLVED_CHECK_FIRST,
    "Descriptor problems affect programs only and do not make identity "
    "coverage incomplete",
    "conservative all-or-nothing resolution",
)


CORE_LABELS = (
    "Package ID", "Status", "Target repo", "Branch", "Program ID", "Program Title",
    "Program Membership", "Claim", "Prerequisite", "Completion Prerequisite",
    "Superseded By",
)
CORE_BOLD = re.compile(r"\*\*(" + "|".join(CORE_LABELS) + r")\*{0,2}:")
BRANCH_HEADER_MARKERS = (
    "| `Branch: name` | Optional. The git branch the work is done on, or a value "
    "starting with `not applicable` (then a reason) for work that has no branch. "
    "`nyx checkouts` reads it, and its value also stays searchable reported text. |",
    "`Branch` is not in that list: although it has a row in the table, its value also "
    "stays searchable reported text, retained under the same rules as any other "
    "reported field.",
    "For `nyx checkouts`, a bold label or one pair of backticks around the value is "
    "tolerated for both fields; the browser's display of `Target repo` is unchanged "
    "and keeps a backtick.",
    "It uses `Target repo` whole, as the absolute path of a clone or of a linked "
    "worktree of a clone",
    "`nyx checkouts` uses the whole absolute path.",
)
CODE_STATUS_MARKERS = (
    "A copy whose verdict is `unknown`, such as a branch in a clone with no base, "
    "counts as neither merged nor not merged. The first of these rules that matches "
    "decides the text, and the order matters:",
    "1. Every copy is merged: `merged · cleanup pending (N worktrees)`",
    "`merged · cleanup pending (N branches)`",
    "2. A pull request for the branch is open in the lookup's listing",
    "`PR #N open`, with the highest open number, and ` · N unpushed` after it only "
    "when the push state below is unpushed.",
    "3. Some copy is not merged, and then:",
    "every package that names the branch in this repository is finished: "
    "`awaiting merge (no PR)`;",
    "Nyx concludes this only when it read every package, so when a specification could "
    "not be read (`package_scan.status` is `incomplete`) a package that may share the "
    "branch is unseen and this rule never matches;",
    "there is no answer at all, the copy is read from the remote-tracking branches "
    "that the readable clones hold: the one that moved last supplies the commit",
    "a remote-tracking branch whose move time cannot be read ranks below every one "
    "that has a move time. Fetch times play no part.",
    "When no readable clone holds the remote-tracking branch, the copy is `unknown`, "
    "never `absent`",
    "Only a live or saved answer says `absent`.",
    "It is `complete` when Nyx read every specification whole, and `incomplete` when "
    "some specification could not be read whole.",
    "A package whose `spec.md` cannot be read is not listed in `packages`",
    "rule 3 below never concludes `awaiting merge (no PR)` on an `incomplete` scan.",
    "a branch deleted on the remote stays present at the tip of any clone that has "
    "not pruned it, so a merged and deleted branch can read `merge state unknown`; "
    "until a networked run answers.",
    "a branch that was never pushed reads `in progress · push state unknown`; until "
    "a networked run answers.",
    "the package has a well-formed `Superseded By:` header, whether or not its "
    "successor exists: `superseded · branch retained`;",
    "`in progress · ` followed by the push state.",
    "4. Any other case, because a copy could not be judged: `merge state unknown`.",
    "The push state is `not pushed` when the remote copy is absent; `N unpushed`",
    "`pushed` when every local copy equals or is behind its remote copy; and "
    "`push state unknown` otherwise.",
    "` · in <name>, <name>`",
    "` · in N other worktrees` instead",
    "A branch that exists only on the remote reads `merge state unknown`.",
    "0. At least one local copy exists, every local copy is a branch with no commit of "
    "its own, and the remote copy, if there is one, is at the commit of one of them: "
    "`in progress · no commits yet`.",
    "its reflog holds exactly one entry, the branch's creation "
    "(`branch: Created from ...`), and its tip is among the latest 5,000 commits of the "
    "default branch's first-parent history.",
    "so a finished or superseded package whose branch has no commit reads "
    "`in progress · no commits yet` too. Rules 1 to 4 keep their numbers.",
    "did not run, because its reflog read or its first-parent listing was cut by the "
    "time limit or failed, or because the branch moved during the run, that copy counts "
    "as unknown in rules 1 to 4 and can never make the package read "
    "`merged · cleanup pending ...`.",
    "With no other deciding copy the package reads `merge state unknown`; a copy of the "
    "same branch that is not merged, or an open pull request, still decides by rules 2 "
    "and 3.",
    "A fresh branch whose reflog gained any other entry (renamed, reset with "
    "`git branch -f`, or fast-forwarded to a newer default head) reads "
    "`merged · cleanup pending ...`, like a branch that was merged.",
    "A branch checked out from a remote branch that was fast-forward merged reads "
    "`in progress · no commits yet`.",
    "A clone with no branch reflog, such as a bare repository or one with "
    "`core.logAllRefUpdates=false`, or whose reflog has expired, reads "
    "`merged · cleanup pending ...` for such a branch.",
    "A branch with no commit of its own that is 5,000 or more first-parent commits "
    "behind the default branch reads `merged · cleanup pending ...`.",
    "Plain `merged`, without cleanup, does not occur yet: it needs a verdict for a "
    "branch that exists only on the remote, which Nyx does not compute.",
    "A clone whose status is not ok, such as one git could not read or one with too "
    "many branches, is not seen (see matching above).",
    "clones whose status is ok are searched. Any other clone, whether git could not "
    "read it or refused it, as for a clone with too many branches, contributes no "
    "branch and is not seen",
    "A package has a Code Status only when its target is a `clone` or a `worktree` and "
    "its branch is found in a readable clone or on the remote. A relative, missing or "
    "invalid target, a target outside the checkout folders, git missing or too old, a "
    "branch that is the repository's default branch, and a branch found nowhere all "
    "leave it empty.",
    "judged by the directory's device and inode, which a symbolic link or a different "
    "letter case on a case-insensitive filesystem does not change. A filesystem that "
    "reports no inode compares the real path instead, with the host's case "
    "normalization.",
    "If the whole text with every name written out would be longer than 1,024 UTF-16 "
    "code units, it says",
    "Several packages can name the same branch of the same repository",
    "An archived package therefore reads `in progress`, not `awaiting merge`, while a "
    "package that names the same branch is still unfinished.",
    "A package counts as finished only when its stage folder is one of the stages "
    "selected under **Counts as finished** for the workspace",
    "With no stage selected, `package_scan.finished_policy` is `not_configured` and "
    "no package is finished.",
    "never a package branch: the package's `branch` status is `default_branch` and it "
    "has no Code Status",
    "Nyx assumes no name.",
)
# The rule texts, in the order the section states them.
CODE_STATUS_RULE_ORDER = (
    "0. At least one local copy exists,",
    "1. Every copy is merged:",
    "2. A pull request for the branch is open",
    "3. Some copy is not merged, and then:",
    "4. Any other case,",
)
WORKSPACE_FINISHED_MARKERS = (
    "The same selection also decides which packages `nyx checkouts` treats as "
    "finished when it reads a package's Code Status",
    "with no stage selected, it treats no package as finished.",
)


def _normalized(name: str) -> str:
    return re.sub(r"\s+", " ", (REPOSITORY_ROOT / name).read_text(encoding="utf-8"))


def _section(name: str, heading: str) -> str:
    text = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
    assert heading in text, (name, heading)
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]
    return re.sub(r"\s+", " ", section)


def test_dependency_and_completion_policy_text_matches_the_board():
    readme = _normalized("README.md")
    assert "satisfied, waiting, unknown, or unavailable" not in readme
    assert "shows links to its resolved prerequisite and dependent items" in readme
    assert "**unresolved target** only when a relationship cannot be resolved" in readme
    assert "asks you to configure completed stages" not in _normalized("docs/workspaces.md")
    assert "actionable configuration explanation" not in _normalized(
        "docs/specification-reference.md")
    assert "The policy is set in **Board settings**; a missing policy" in _normalized(
        "docs/workspaces.md")
    assert ("The completion policy is configured in the collapsed **Board settings** "
            "panel") in _normalized("docs/specification-reference.md")
    assert ("a missing policy leaves the requirement **unknown**, with no prompt "
            "or explanation") in _normalized("docs/workspaces.md")
    assert ("a completion dependency is **unknown**; the board shows no prompt "
            "or explanation") in _normalized("docs/specification-reference.md")


def test_configuration_unavailable_recovery_is_one_fence_free_bullet():
    document = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    heading = "## If something looks wrong\n"
    assert heading in document
    raw = document.split(heading, 1)[1].split("\n## ", 1)[0]
    bullets = [
        re.sub(r"\s+", " ", bullet)
        for bullet in re.split(r"\n(?=- )", raw)
        if "Configuration: unavailable" in bullet
    ]
    assert len(bullets) == 1
    bullet = bullets[0]
    for marker in (
        "`nyx --status` prints `Configuration: unavailable`",
        "`nyx --setup` does not repair an existing unreadable record",
        "Run `nyx --stop`",
        "restore `~/.nyx/config/config.json` from a backup",
        "move it aside",
        "`nyx --setup <workspace>`",
        "discards the saved board settings and checkout folders",
    ):
        assert marker in bullet, marker
    assert "```" not in raw
    for forbidden in ("pkill", "killall", "taskkill", "kill -9"):
        assert forbidden not in bullet, forbidden


@pytest.mark.parametrize("name", ["docs/specification-reference.md", "docs/workspaces.md"])
def test_documents_state_the_uuid_rule_effects_and_commands(name):
    document = _normalized(name)
    for marker in UUID_RULE_MARKERS + UUID_COMMANDS:
        assert marker in document, (name, marker)
    assert "**Workspace issues** lists nothing" in document, name
    assert "invalid `Program ID`" in document, name


def test_reference_states_program_id_invalidates_the_descriptor():
    document = _normalized("docs/specification-reference.md")
    assert ("An invalid `Program ID` makes its program descriptor invalid, as "
            "described under Programs, without affecting any item's identity") in document


def test_readme_publishes_no_uuid_generation_command():
    readme = _normalized("README.md")
    for command in UUID_COMMANDS:
        assert command not in readme, command


def test_reference_programs_section_documents_the_descriptor_grammar():
    section = _section("docs/specification-reference.md", "## Programs")
    for marker in PROGRAM_GRAMMAR_MARKERS:
        assert marker in section, marker


def test_reference_scopes_bold_names_to_reported_text_and_core_labels_are_plain():
    reference = _normalized("docs/specification-reference.md")
    assert ("Bold names are documented for reported-text fields only. The core "
            "field labels in the table above are written plain") in reference
    for name in _ALL_DOCUMENTS:
        assert not CORE_BOLD.search(_normalized(name)), name


@pytest.mark.parametrize("label", CORE_LABELS)
@pytest.mark.parametrize("form", ("**{label}:** value", "**{label}**: value", "**{label}: value"))
def test_core_bold_pattern_finds_every_core_label_in_every_bold_form(label, form):
    assert CORE_BOLD.search(form.format(label=label))
    assert not CORE_BOLD.search(f"{label}: value")


@pytest.mark.parametrize(
    "text", ("**Branch:** spec/x", "**Branch**: spec/x", "**Branch: spec/x", "**Target repo:** /x")
)
def test_core_bold_pattern_finds_a_bold_branch_and_target_label(text):
    assert CORE_BOLD.search(text)


@pytest.mark.parametrize("name,section_heading,old_phrase", [
    ("docs/running-nyx.md", "## If something looks wrong\n",
     "inspect the involved `spec.md` headers"),
    ("docs/specification-reference.md", "## Diagnostics",
     "inspect the affected specification headers"),
])
def test_unresolved_target_guidance_checks_workspace_issues_first(
        name, section_heading, old_phrase):
    section = _section(name, section_heading)
    for marker in UNRESOLVED_MARKERS:
        assert marker in section, (name, marker)
    if old_phrase in section:
        assert section.index(old_phrase) > section.index(UNRESOLVED_CHECK_FIRST), name


def test_documents_name_board_settings_and_reset_clears_both_settings():
    for name in _ALL_DOCUMENTS:
        document = _normalized(name)
        assert "**Board row order**" not in document, name
        assert "Reload board row order" not in document, name
    assert "used by the bundled examples" not in _normalized("docs/workspaces.md")
    for name, phrase in (
        ("docs/workspaces.md", "clears only the current root's saved order"),
        ("docs/running-nyx.md", "clears the saved order for the current workspace only"),
        ("docs/running-nyx.md", "discards the editor's unsaved moves"),
    ):
        assert phrase not in _normalized(name), (name, phrase)
    assert ("**Reset** clears the current root's saved row order and "
            "completed-stage set") in _normalized("docs/workspaces.md")
    guide = _normalized("docs/running-nyx.md")
    assert ("**Cancel** discards the unsaved moves and unsaved completion "
            "choices") in guide
    assert ("**Reset** clears the current root's row order and completed-stage "
            "set; the board then follows the canonical inventory order") in guide
    assert "use the row-order editor to move **Queue**" in guide
    assert "**Reload board settings**" in guide
    assert "**Reload board settings**" in _normalized("docs/workspaces.md")


def test_documents_state_the_loopback_exposure_without_caller_authentication():
    for name in ("README.md", "docs/running-nyx.md"):
        document = _normalized(name)
        assert "serves one local account" not in document, name
        assert "intended for one local account" not in document, name
    readme = _normalized("README.md")
    assert "The browser is read-only;" not in readme
    assert "without caller authentication, so other local accounts on the same host" in readme
    assert "The browser never edits specification files" in readme
    for heading in ("### Desktop limits", "## If something looks wrong\n"):
        section = _section("docs/running-nyx.md", heading)
        assert "without caller authentication, so other local accounts on the same host" in section, heading
        assert "change board settings" in section, heading


def _run_command(command: str, *, root: Path, environment: dict[str, str]):
    """Run one published Linux installation command in its own bash."""

    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", command], cwd=root, env=environment,
        capture_output=True, text=True, check=False, timeout=180,
    )
    assert result.returncode == 0, (command, result.returncode, result.stdout, result.stderr)
    return result


@pytest.mark.skipif(os.name == "nt", reason="per-line commands run only on the Linux installation path")
def test_native_shell_propagates_failed_command(tmp_path):
    with pytest.raises(AssertionError, match=r"\b17\b"):
        _run_command(_native_exit(17), root=tmp_path, environment=_installed_environment(tmp_path))


_PWSH_SESSION_PREFIX = (
    "$ErrorActionPreference = 'Stop'\n"
    "$PSNativeCommandUseErrorActionPreference = $true\n"
    "trap {\n"
    "  [Console]::Error.WriteLine(($_ | Out-String))\n"
    "  if ($_.Exception -is [System.Management.Automation.NativeCommandExitException] "
    "-and $LASTEXITCODE -is [int] -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
    "  exit 1\n"
    "}\n"
)


def _session_arguments(block: str) -> list[str]:
    """Return the argument vector running ``block`` as one shell session."""

    if os.name == "nt":
        return [
            "pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
            _PWSH_SESSION_PREFIX + block,
        ]
    return ["bash", "-euo", "pipefail", "-c", block]


_TIMEOUT_REPORT_GRACE_SECONDS = 10.0


def _kill_session_tree(process: subprocess.Popen) -> None:
    """Kill the session shell and every descendant that holds its output pipes."""

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True, check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        process.kill()


def _run_session(
    block: str, *, root: Path, environment: dict[str, str] | None = None,
    timeout: float,
) -> subprocess.CompletedProcess:
    """Run ``block`` as one shell session, killing its whole process tree on timeout.

    ``subprocess.run`` kills only the shell, and the build children that keep the
    output pipes open then keep the read blocked.  The session therefore leads
    its own process group (or is killed with ``taskkill /T`` on Windows), and the
    partial output is collected with a bounded read before the timeout is raised.
    """

    arguments = _session_arguments(block)
    if shutil.which(arguments[0]) is None:
        pytest.fail(f"the lane's shell {arguments[0]!r} is not available")
    options: dict[str, object] = {}
    if os.name != "nt":
        options["start_new_session"] = True
    with subprocess.Popen(
        arguments, cwd=root, env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, **options,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as expired:
            _kill_session_tree(process)
            try:
                stdout, stderr = process.communicate(timeout=_TIMEOUT_REPORT_GRACE_SECONDS)
            except subprocess.TimeoutExpired as lingering:
                stdout, stderr = lingering.stdout, lingering.stderr
            raise subprocess.TimeoutExpired(
                arguments, timeout, output=_as_text(stdout), stderr=_as_text(stderr)
            ) from expired
        except BaseException:
            _kill_session_tree(process)
            raise
    return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)


def _as_text(output: str | bytes | None) -> str:
    if output is None:
        return ""
    return output.decode(errors="replace") if isinstance(output, bytes) else output


def test_session_fails_hard_when_the_lane_shell_is_missing(tmp_path, monkeypatch):
    shell = _session_arguments("unused")[0]
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(pytest.fail.Exception, match=rf"the lane's shell '{shell}' is not available"):
        _run_session("unused", root=tmp_path, timeout=120)


def test_session_timeout_kills_the_tree_and_reports_partial_output(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    child = (
        "import time\n"
        "print('session-started', flush=True)\n"
        f"path = {str(heartbeat)!r}\n"
        "end = time.monotonic() + 120\n"
        "while time.monotonic() < end:\n"
        "    open(path, 'a').write('x')\n"
        "    time.sleep(0.1)\n"
    )
    script = tmp_path / "outlives.py"
    script.write_text(child, encoding="utf-8")
    if os.name == "nt":
        block = f"& '{sys.executable}' '{script}'\necho after-child"
    else:
        block = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}\necho after-child"
    timeout = 3.0
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        _run_session(block, root=tmp_path, timeout=timeout)
    elapsed = time.monotonic() - started
    assert elapsed < timeout + _TIMEOUT_REPORT_GRACE_SECONDS, elapsed
    assert "session-started" in _as_text(raised.value.stdout)
    size = heartbeat.stat().st_size
    assert size > 0
    time.sleep(1.0)
    assert heartbeat.stat().st_size == size, "the foreground child survived the timeout"


def _native_exit(code: int) -> str:
    interpreter = sys.executable
    if os.name == "nt":
        return f"& '{interpreter.replace(chr(39), chr(39) * 2)}' -c 'import sys; sys.exit({code})'"
    return f"{shlex.quote(interpreter)} -c 'import sys; sys.exit({code})'"


def test_session_keeps_state_between_lines(tmp_path):
    if os.name == "nt":
        block = "$env:NYX_SESSION_PROBE = 'kept'\nWrite-Output \"seen=$env:NYX_SESSION_PROBE\""
    else:
        block = 'export NYX_SESSION_PROBE=kept\necho "seen=$NYX_SESSION_PROBE"'
    result = _run_session(block, root=tmp_path, timeout=120)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.strip() == "seen=kept"


def test_session_runs_from_the_given_root(tmp_path):
    block = "Write-Output (Get-Location).Path" if os.name == "nt" else "pwd"
    result = _run_session(block, root=tmp_path, timeout=120)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


def test_session_failing_native_command_stops_the_block_with_its_own_exit_code(tmp_path):
    block = _native_exit(17) + "\necho after-failure"
    result = _run_session(block, root=tmp_path, timeout=120)
    assert result.returncode == 17, (result.returncode, result.stdout, result.stderr)
    assert "after-failure" not in result.stdout


def test_session_without_errors_exits_zero(tmp_path):
    block = _native_exit(0) + "\necho finished"
    result = _run_session(block, root=tmp_path, timeout=120)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "finished" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="the PowerShell trap exists only on Windows lanes")
def test_session_non_native_terminating_error_exits_non_zero_with_record(tmp_path):
    # A native command exiting 0 leaves LASTEXITCODE 0, so a ``throw`` must
    # still end the session non-zero.
    block = _native_exit(0) + "\nthrow 'nyx-session-boom'\nWrite-Output after-throw"
    result = _run_session(block, root=tmp_path, timeout=120)
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert "nyx-session-boom" in result.stderr
    assert "after-throw" not in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="the PowerShell trap exists only on Windows lanes")
def test_session_native_failure_record_reaches_stderr(tmp_path):
    result = _run_session(_native_exit(23), root=tmp_path, timeout=120)
    assert result.returncode == 23, (result.returncode, result.stdout, result.stderr)
    assert result.stderr.strip()


@pytest.mark.skipif(
    os.environ.get("NYX_VERIFY_DOCUMENTED_BUILD") != "1",
    reason="the documented build runs only in the desktop documented-build step",
)
def test_documented_desktop_build_blocks_run_literally():
    language = _lane_build_language()
    if language is None:
        pytest.fail(
            "NYX_VERIFY_DOCUMENTED_BUILD=1 is set but this host has no documented "
            "build language; the documented build runs only on Windows and macOS"
        )
    guide = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    block = _published_build_block(guide, language)
    bound = _BUILD_TIMEOUT_MINUTES[language] * 60
    try:
        result = _run_session(
            block, root=REPOSITORY_ROOT, environment=dict(os.environ), timeout=bound
        )
    except subprocess.TimeoutExpired as expired:
        pytest.fail(f"the documented {language} build exceeded {bound} s: {expired.stdout!r}")
    print(result.stdout)
    print(result.stderr, file=sys.stderr)
    assert result.returncode == 0, (
        f"documented {language} build exited {result.returncode}",
        result.stdout[-4000:], result.stderr[-4000:],
    )


def _exercise_documented_linux_installation(tmp_path: Path) -> None:
    """Run the published Linux commands exactly as written."""

    blocks = [
        _documented_commands((REPOSITORY_ROOT / name).read_text(encoding="utf-8"), "bash")
        for name in DOCUMENTS
    ]
    assert blocks[0] == blocks[1]
    commands = blocks[0]
    subprocess.run(["git", "diff", "--exit-code", "HEAD"], cwd=REPOSITORY_ROOT, check=True)
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=REPOSITORY_ROOT
    ).decode().split("\0")
    root = tmp_path / "checkout"
    root.mkdir()
    for name in filter(None, tracked):
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPOSITORY_ROOT / name, destination, follow_symlinks=False)
    home = tmp_path / "home"
    home.mkdir()
    environment = _installed_environment(home)
    assert not (root / ".venv").exists()
    assert not (home / ".nyx").exists()
    for command in commands[:2]:
        _run_command(command, root=root, environment=environment)
    venv = root / ".venv"
    interpreter = _venv_executable(venv, "python")
    console = _venv_executable(venv, "nyx")
    assert console.resolve().is_relative_to(venv.resolve())
    provenance = subprocess.run(
        [str(interpreter), "-c",
         "import importlib.metadata, json, nyx, sys; "
         "d = importlib.metadata.distribution('nyx'); "
         "print(json.dumps({'module': nyx.__file__, 'prefix': sys.prefix, "
         "'direct_url': json.loads(d.read_text('direct_url.json'))}))"],
        cwd=tmp_path, env=environment, capture_output=True, text=True, check=True, timeout=30,
    )
    installed = json.loads(provenance.stdout)
    assert Path(installed["prefix"]).resolve() == venv.resolve()
    assert Path(installed["module"]).resolve().is_relative_to(venv.resolve())
    assert not installed["direct_url"].get("dir_info", {}).get("editable", False)
    source_url = urlsplit(installed["direct_url"]["url"])
    assert source_url.scheme == "file"
    assert Path(url2pathname(source_url.path)).resolve() == root.resolve()
    sample = (root / "examples" / "sample-specifications").resolve()
    setup = _run_command(commands[2], root=root, environment=environment)
    assert setup.stdout.strip() == f"configured {sample}"
    configuration = json.loads((home / ".nyx/config/config.json").read_text(encoding="utf-8"))
    assert configuration["specification_root"] == str(sample)
    assert configuration == {"schema_version": 4, "specification_root": str(sample)}
    locator = home / ".nyx/runtime/instance.json"
    claim = home / ".nyx/runtime/lease.lock"
    try:
        started = _run_command(commands[3], root=root, environment=environment)
        assert started.stdout.strip() == "http://127.0.0.1:8765/"
        assert json.loads(locator.read_text(encoding="utf-8"))["url"] == started.stdout.strip()
        status = _run_command(commands[4], root=root, environment=environment)
        assert status.stdout.splitlines() == [
            "Configuration: configured",
            f"Workspace: {json.dumps(str(sample))}",
            "Runtime: running",
            'URL: "http://127.0.0.1:8765/"',
        ]
        assert _native_claim_probe(interpreter, claim, root=tmp_path, home=home) == "busy"
        connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=10)
        try:
            connection.request("GET", "/api/catalog", headers={"Host": "127.0.0.1:8765"})
            response = connection.getresponse()
            catalog = json.loads(response.read())
            assert response.status == 200
            assert any(
                entry["declared"]["title"] == "Build the route preview"
                for entry in catalog["entries"]
            )
        finally:
            connection.close()
        _run_command(commands[5], root=root, environment=environment)
        assert not locator.exists()
        assert _native_claim_probe(interpreter, claim, root=tmp_path, home=home) == "free"
    finally:
        # Only the isolated home can authorize cleanup of this test's instance.
        if locator.exists():
            _run_command(commands[5], root=root, environment=environment)


def _documented_desktop_artifact() -> ArtifactLayout:
    staged = os.environ.get("NYX_DESKTOP_ARTIFACT")
    if not staged:
        pytest.fail(
            "NYX_VERIFY_DOCUMENTED_INSTALL=1 is set but NYX_DESKTOP_ARTIFACT is unset "
            "or empty; the private desktop artifact must be staged before the "
            "documented installation runs"
        )
    artifact = ArtifactLayout(Path(staged).resolve())
    assert artifact.executable.is_file(), f"staged application is missing: {artifact.executable}"
    documented = (
        DESKTOP_ARTIFACT_PATHS["windows"]
        if os.name == "nt"
        else DESKTOP_ARTIFACT_PATHS["macos"]
    )
    normalized = str(artifact.artifact).replace("\\", "/")
    assert documented.replace("\\", "/") in normalized, (documented, normalized)
    return artifact


def _exercise_documented_desktop_actions(tmp_path: Path) -> None:
    """Run the published desktop actions against the staged private artifact."""

    artifact = _documented_desktop_artifact()
    root = tmp_path / "documented"
    root.mkdir()
    home = root / "home"
    home.mkdir()
    decoy = _inert_path(root)
    environment = _sanitized_environment(home, decoy)

    # First launch: the chooser owns the account but serves no board.
    with _running_gui(artifact, root, environment) as gui:
        _wait_for_window(gui)
        with pytest.raises(OSError):
            _request("/api/catalog")
        assert gui.process.poll() is None, gui.diagnostics()
        assert gui.close() == 0, gui.diagnostics()
    _assert_claims_released(home)

    with _workspace(root) as workspace:
        # Open a configured workspace, serve the board, then Quit.
        _, first_configuration = _write_configuration(home, workspace)
        expected_first = json.loads(_expected_catalog(first_configuration))
        with _running_gui(artifact, root, environment) as gui:
            status, body, _ = _wait_for_board(time.monotonic() + _START_TIMEOUT, gui)
            assert status == 200
            assert json.loads(body) == expected_first
            assert gui.close() == 0, gui.diagnostics()
        _assert_claims_released(home)
        _wait_port_free(time.monotonic() + 15)

        # Change workspace: the replacement selection is served after restart.
        second = root / "second"
        shutil.copytree(workspace, second)
        shutil.rmtree(second / "Trail_API")
        _, second_configuration = _write_configuration(home, second)
        expected_second = json.loads(_expected_catalog(second_configuration))
        assert expected_first != expected_second
        with _running_gui(artifact, root, environment) as gui:
            status, body, _ = _wait_for_board(time.monotonic() + _START_TIMEOUT, gui)
            assert status == 200
            assert json.loads(body) == expected_second
            assert gui.close() == 0, gui.diagnostics()
        _assert_claims_released(home)
        _wait_port_free(time.monotonic() + 15)

        # Recovery: a surviving former worker blocks the start until it exits.
        config_file, _ = _write_configuration(home, workspace)
        before = config_file.read_bytes()
        _, recovery_path = _claim_paths(home)
        recovery_path.parent.mkdir(parents=True, exist_ok=True)
        former = NativeClaim(recovery_path)
        assert former.acquire(blocking=False)
        try:
            with _running_gui(artifact, root, environment) as gui:
                _wait_for_window(gui)
                with pytest.raises(OSError):
                    _request("/api/catalog")
                assert gui.process.poll() is None, gui.diagnostics()
                assert config_file.read_bytes() == before
                assert gui.close() == 0, gui.diagnostics()
        finally:
            former.close()
        _wait_port_free(time.monotonic() + 15)


@pytest.mark.skipif(
    os.environ.get("NYX_VERIFY_DOCUMENTED_INSTALL") != "1",
    reason="documented installation runs in its dedicated native hosted lane",
)
def test_documented_native_installation_and_lifecycle(tmp_path):
    if os.name == "nt" or sys.platform == "darwin":
        _exercise_documented_desktop_actions(tmp_path)
    else:
        _exercise_documented_linux_installation(tmp_path)


_GATE_VARIABLES = (
    "NYX_VERIFY_DOCUMENTED_INSTALL",
    "NYX_VERIFY_DOCUMENTED_BUILD",
    "NYX_EXPECTED_NATIVE_HOST",
    "NYX_REQUIRE_NATIVE_DESKTOP",
    "NYX_DESKTOP_ARTIFACT",
)


@pytest.mark.skipif(
    _lane_build_language() is not None,
    reason="the gate would start the real documented build on this host",
)
def test_documented_build_gate_fails_on_a_host_without_a_desktop_build():
    node = "tests/test_documented_install.py::test_documented_desktop_build_blocks_run_literally"
    message = (
        "NYX_VERIFY_DOCUMENTED_BUILD=1 is set but this host has no documented "
        "build language; the documented build runs only on Windows and macOS"
    )
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in _GATE_VARIABLES
    }
    environment["NYX_VERIFY_DOCUMENTED_BUILD"] = "1"
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rA", node],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    output = completed.stdout + completed.stderr
    lines = completed.stdout.strip().splitlines()
    assert completed.returncode == 1, output[-4000:]
    assert lines and "1 failed" in lines[-1], output[-4000:]
    # The summary line carries the message only on some terminals, so it pins
    # the node there and the message is pinned in the failure section.
    assert any(line.startswith(f"FAILED {node}") for line in lines), output[-4000:]
    assert f"Failed: {message}" in output, output[-4000:]


@pytest.mark.skipif(
    not (os.name == "nt" or sys.platform == "darwin"),
    reason="the gate would start the real Linux installation on this host",
)
def test_documented_install_gate_fails_without_a_staged_artifact():
    node = "tests/test_documented_install.py::test_documented_native_installation_and_lifecycle"
    message = (
        "NYX_VERIFY_DOCUMENTED_INSTALL=1 is set but NYX_DESKTOP_ARTIFACT is unset "
        "or empty; the private desktop artifact must be staged before the "
        "documented installation runs"
    )
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in _GATE_VARIABLES
    }
    environment["NYX_VERIFY_DOCUMENTED_INSTALL"] = "1"
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rA", node],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    output = completed.stdout + completed.stderr
    lines = completed.stdout.strip().splitlines()
    assert completed.returncode == 1, output[-4000:]
    assert lines and "1 failed" in lines[-1], output[-4000:]
    # The summary line carries the message only on some terminals, so it pins
    # the node there and the message is pinned in the failure section.
    assert any(line.startswith(f"FAILED {node}") for line in lines), output[-4000:]
    assert f"Failed: {message}" in output, output[-4000:]


def _assert_unique_markers(document: str, markers, name: str) -> None:
    # A marker that occurs twice would survive deleting one copy of the phrase.
    for marker in markers:
        assert document.count(marker) == 1, (name, marker)


def test_reference_documents_the_branch_header_and_whole_path_target():
    reference = _normalized("docs/specification-reference.md")
    _assert_unique_markers(reference, BRANCH_HEADER_MARKERS, "docs/specification-reference.md")
    assert "**Branch" not in reference
    assert "Only `nyx checkouts` reads it" not in reference


def test_checkout_guide_documents_every_code_status_rule_in_order_with_its_limits():
    section = _section("docs/running-nyx.md", "## Inspect checkouts")
    heading = "### Code Status of specification packages"
    assert heading in section
    code_status = section.split(heading, 1)[1]
    _assert_unique_markers(code_status, CODE_STATUS_MARKERS, "docs/running-nyx.md")
    positions = [code_status.index(rule) for rule in CODE_STATUS_RULE_ORDER]
    assert positions == sorted(positions)


def test_checkout_system_readme_states_the_no_answer_remote_copy_rule():
    readme = _normalized("docs/systems/checkouts/README.md")
    markers = (
        "the remote-tracking ref that moved last among the readable clones that hold it",
        "a ref whose move time cannot be read ranks below the others; fetch times play "
        "no part",
        "the copy is `unknown`, never `absent`: only a live or saved answer says `absent`.",
    )
    _assert_unique_markers(readme, markers, "docs/systems/checkouts/README.md")
    assert "newer of last fetch" not in readme


def test_checkout_guide_texts_equal_the_texts_the_code_builds():
    from nyx.models import code_status_text

    fields = dict(
        pull_request=None, push=None, unpushed=None, cleanup=None, checked_out_elsewhere=[]
    )
    section = _section("docs/running-nyx.md", "## Inspect checkouts")
    documented = section.split("### Code Status of specification packages", 1)[1]

    def build(state, **changes):
        return code_status_text({**fields, "state": state, **changes})

    pull_request = build("pr_open", pull_request="N")
    unpushed_pull_request = build("pr_open", pull_request="N", push="unpushed", unpushed="N")
    texts = (
        build("awaiting_merge"),
        build("superseded"),
        build("unknown"),
        build("no_commits_yet"),
        pull_request,
        unpushed_pull_request[len(pull_request):],
        build("in_progress", push="pushed")[: -len("pushed")],
        build("in_progress", push="not_pushed")[len("in progress \u00b7 "):],
        build("in_progress", push="pushed")[len("in progress \u00b7 "):],
        build("in_progress", push="unknown")[len("in progress \u00b7 "):],
        build("in_progress", push="unpushed", unpushed="N")[len("in progress \u00b7 "):],
        build("merged", cleanup={"worktrees": 2, "branches": 0}).replace(
            "2 worktrees", "N worktrees"
        ),
        build("merged", cleanup={"worktrees": 0, "branches": 2}).replace(
            "2 branches", "N branches"
        ),
    )
    for text in texts:
        assert text in documented, text
    assert build("merged", cleanup={"worktrees": 1, "branches": 0}).endswith("(1 worktree)")
    assert build("merged", cleanup={"worktrees": 0, "branches": 1}).endswith("(1 branch)")
    assert "A single one reads `1 worktree` or `1 branch`." in documented


def test_checkout_guide_promises_plain_merged_nowhere_else():
    section = _section("docs/running-nyx.md", "## Inspect checkouts")
    code_status = section.split("### Code Status of specification packages", 1)[1]
    sentence = (
        "Plain `merged`, without cleanup, does not occur yet: it needs a verdict for a "
        "branch that exists only on the remote, which Nyx does not compute."
    )
    assert sentence in code_status
    assert "`merged`" not in code_status.replace(sentence, "")


def test_workspace_guide_says_counts_as_finished_decides_checkout_packages():
    workspaces = _normalized("docs/workspaces.md")
    _assert_unique_markers(workspaces, WORKSPACE_FINISHED_MARKERS, "docs/workspaces.md")
