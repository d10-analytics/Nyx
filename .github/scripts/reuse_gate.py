"""Decide whether a push to main can reuse passing results for an identical tree.

Run by the workflow's ``reuse-gate`` job with the runner's ``python3``; it uses
only the standard library.  Two independent groups are decided from run records
of this workflow alone:

- ``source``: the package and suite lanes, proved by a completed successful
  ``push`` run whose ``head_branch`` is a non-empty string other than ``main``
  (a branch or a tag).
- ``desktop``: the desktop lanes, proved by a completed successful
  ``workflow_dispatch`` run.

A candidate qualifies only when its ``head_commit.tree_id`` equals the current
run's, it is not the current run, and its own ``status`` and ``conclusion`` are
``completed`` and ``success``.  No per-job data, commits data or age limit is
used.  Candidates are scanned newest first, past same-tree runs that do not
qualify, across at most two listing pages.

Failure model: expected failures (HTTP and network errors, timeouts, bad JSON,
missing fields) write ``false`` for both groups, a ``::warning::`` annotation
and a summary line, and exit 0, so the lanes run.  Any other exception exits
non-zero before the outputs file is touched.  The summary is written first and
the outputs file last, so a ``true`` output is never left behind by a failure.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TextIO

PAGE_SIZE = 100
MAX_PAGES = 2
REQUEST_TIMEOUT_SECONDS = 15
MAIN_BRANCH = "main"

SOURCE = "source"
DESKTOP = "desktop"
GROUPS = (SOURCE, DESKTOP)
GROUP_LABELS = {
    SOURCE: "package and suite lanes",
    DESKTOP: "desktop lanes",
}
GROUP_OUTPUTS = {SOURCE: "reuse_source", DESKTOP: "reuse_desktop"}
GROUP_EVENTS = {SOURCE: "push", DESKTOP: "workflow_dispatch"}

Client = Callable[[str], Any]


class ExpectedFailure(Exception):
    """A fetch or parse failure that must fail open instead of failing the job."""


def read_tree(record: Any) -> str:
    """Return a run record's commit tree hash, for the current run and candidates alike."""
    if not isinstance(record, Mapping):
        raise ExpectedFailure("a run record is not an object")
    commit = record.get("head_commit")
    tree = commit.get("tree_id") if isinstance(commit, Mapping) else None
    if not isinstance(tree, str) or not tree:
        raise ExpectedFailure(f"run {record.get('id')!r} has no head_commit.tree_id")
    return tree


def _qualifies(group: str, record: Mapping[str, Any]) -> bool:
    if record.get("event") != GROUP_EVENTS[group]:
        return False
    if group == SOURCE:
        branch = record.get("head_branch")
        return isinstance(branch, str) and bool(branch) and branch != MAIN_BRANCH
    return True


def _is_evidence_run(record: Any, current_id: Any) -> bool:
    return (
        isinstance(record, Mapping)
        and record.get("status") == "completed"
        and record.get("conclusion") == "success"
        and record.get("id") != current_id
    )


class Decision:
    """Per-group outcome: the matched run record, or the reason none matched."""

    def __init__(self) -> None:
        self.matches: dict[str, Mapping[str, Any]] = {}
        self.reasons: dict[str, str] = {}


def decide(
    current: Mapping[str, Any],
    pages: Sequence[Sequence[Any]] | Callable[[int], Sequence[Any]],
) -> Decision:
    """Match candidate run records against the current run, per group.

    ``pages`` is a callable returning the candidate records of page ``n`` and is
    called only while a group is unmatched and the page limit is not reached.
    A sequence of pages is also accepted.  Raises ``ExpectedFailure`` when a
    record cannot be read.
    """
    fetch_page = pages if callable(pages) else (lambda number: pages[number - 1])
    tree = read_tree(current)
    current_id = current.get("id")
    decision = Decision()
    scanned = 0
    for number in range(1, MAX_PAGES + 1):
        if len(decision.matches) == len(GROUPS):
            break
        page = fetch_page(number)
        for record in page:
            scanned += 1
            if not _is_evidence_run(record, current_id):
                continue
            if read_tree(record) != tree:
                continue
            for group in GROUPS:
                if group not in decision.matches and _qualifies(group, record):
                    decision.matches[group] = record
            if len(decision.matches) == len(GROUPS):
                break
        if len(page) < PAGE_SIZE:
            break
    for group in GROUPS:
        if group not in decision.matches:
            decision.reasons[group] = (
                f"no completed successful {GROUP_EVENTS[group]} run"
                f"{' on a ref other than main' if group == SOURCE else ''}"
                f" at tree {tree} among {scanned} listed runs"
            )
    return decision


def make_client(api_url: str, token: str | None, timeout: float = REQUEST_TIMEOUT_SECONDS) -> Client:
    """Return a GET client mapping every transport or decoding error to ExpectedFailure."""

    def get(path: str) -> Any:
        request = urllib.request.Request(
            api_url.rstrip("/") + path,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
            return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise ExpectedFailure(f"GET {path} returned HTTP {error.code}") from error
        except (OSError, ValueError, UnicodeDecodeError) as error:
            raise ExpectedFailure(f"GET {path} failed: {type(error).__name__}: {error}") from error

    return get


def _decide_from_api(env: Mapping[str, str], client: Client) -> Decision:
    repository = env["GITHUB_REPOSITORY"]
    run_id = env["GITHUB_RUN_ID"]
    current = client(f"/repos/{repository}/actions/runs/{run_id}")
    if not isinstance(current, Mapping):
        raise ExpectedFailure("the current run record is not an object")
    read_tree(current)
    workflow_id = current.get("workflow_id")
    if isinstance(workflow_id, bool) or not isinstance(workflow_id, (int, str)) or workflow_id == "":
        raise ExpectedFailure("the current run record has no workflow_id")

    def listing(number: int) -> list[Any]:
        payload = client(
            f"/repos/{repository}/actions/workflows/{workflow_id}/runs"
            f"?status=success&per_page={PAGE_SIZE}&page={number}"
        )
        runs = payload.get("workflow_runs") if isinstance(payload, Mapping) else None
        if not isinstance(runs, list):
            raise ExpectedFailure("the runs listing has no workflow_runs list")
        return runs

    return decide(current, listing)


def _summary_lines(decision: Decision, pushed_sha: str, current_tree: str | None) -> list[str]:
    lines: list[str] = []
    for group in GROUPS:
        label = GROUP_LABELS[group]
        match = decision.matches.get(group)
        if match is None:
            lines.append(f"- {label} run: {decision.reasons[group]}.")
        else:
            lines.append(
                f"- {label} reused: evidence from {GROUP_EVENTS[group]} run {match.get('id')}, "
                f"which tested SHA {match.get('head_sha')} at tree {current_tree}; "
                f"pushed SHA {pushed_sha} did not run them."
            )
    return lines


def _clean(text: str) -> str:
    return " ".join(str(text).split())


def main(
    env: Mapping[str, str] | None = None,
    client: Client | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Run the gate; return the exit status.  Unexpected exceptions propagate."""
    env = os.environ if env is None else env
    out = sys.stdout if stdout is None else stdout
    if client is None:
        client = make_client(
            env.get("GITHUB_API_URL", "https://api.github.com"),
            env.get("GITHUB_TOKEN") or env.get("GH_TOKEN"),
        )
    pushed_sha = env.get("GITHUB_SHA", "")
    summary_path = env["GITHUB_STEP_SUMMARY"]
    outputs_path = env["GITHUB_OUTPUT"]

    try:
        decision = _decide_from_api(env, client)
        tree: str | None = None
        if decision.matches:
            tree = read_tree(next(iter(decision.matches.values())))
        lines = _summary_lines(decision, pushed_sha, tree)
        results = {group: group in decision.matches for group in GROUPS}
    except ExpectedFailure as failure:
        reason = _clean(str(failure))
        print(f"::warning::reuse gate failed open, running all lanes: {reason}", file=out)
        lines = [f"- reuse gate failed open, so all lanes run: {reason}."]
        results = {group: False for group in GROUPS}

    with open(summary_path, "a", encoding="utf-8") as summary:
        summary.write("### Reuse gate\n\n" + "\n".join(lines) + "\n")
    with open(outputs_path, "a", encoding="utf-8") as outputs:
        outputs.write(
            "".join(f"{GROUP_OUTPUTS[group]}={str(results[group]).lower()}\n" for group in GROUPS)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
