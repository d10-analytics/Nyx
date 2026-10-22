"""The three user-facing Nyx lifecycle commands."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from . import checkouts, models, runtime, state

# The desktop hosts own their runtime through the application window.  The
# detached Linux service commands do not exist there, so their options are
# refused before any configuration, runtime record, or inherited handle changes.
_DESKTOP_LIFECYCLE_REFUSAL = (
    "the desktop application does not provide lifecycle commands; "
    "open Nyx and use its workspace controls"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nyx",
        description=f"Start the Nyx board at {runtime.URL}, or reuse the one already running.",
        epilog=(
            "--setup, --stop and --status are available on Linux only; on Windows "
            "and macOS, open the Nyx application and use its workspace controls. "
            "Run 'nyx --setup <workspace>' once before the first start."
        ),
    )
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument(
        "--setup",
        metavar="SPEC_ROOT",
        help="configure the Workspace at SPEC_ROOT",
    )
    commands.add_argument("--stop", action="store_true", help="stop the running Nyx board")
    commands.add_argument(
        "--status",
        action="store_true",
        help="report configuration and runtime state without changing either",
    )
    parser.add_argument(
        "command",
        nargs="?",
        metavar="checkouts",
        help=(
            "'checkouts' prints the state of the configured checkout folders, "
            "checking each distinct remote once"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="with 'checkouts', print the checkout document instead of one line per checkout",
    )
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="with 'checkouts', skip every remote check and report cached values only",
    )
    return parser


def _launch_desktop() -> int:
    """Route the desktop console to the shared application entry."""

    from . import desktop

    return desktop.main([])


def _retired_arguments(args: argparse.Namespace) -> list[str]:
    retired: list[str] = []
    if args.setup is not None:
        retired.append("--setup")
    if args.stop:
        retired.append("--stop")
    if args.status:
        retired.append("--status")
    return retired



def _json_literal(value: object) -> str:
    """Render a validated display value as an ASCII JSON literal."""

    return json.dumps(value, ensure_ascii=True)


_CHECKOUTS_DEADLINE_SECONDS = 60


def _head_element(head: dict[str, Any] | None) -> str | None:
    if head is None:
        return None
    branch, sha = head["branch"], head["sha"]
    if branch is None:
        return f"detached {sha}"
    if sha is None:
        return f"unborn {branch}"
    return f"branch {branch}"


def _worktree_elements(worktree: dict[str, Any]) -> list[str]:
    elements: list[str] = []
    head = _head_element(worktree["head"])
    if head is not None:
        elements.append(head)
    changes = worktree["changes"]
    if changes is not None:
        elements.append(
            f"staged {changes['staged']} unstaged {changes['unstaged']} "
            f"untracked {changes['untracked']} conflicted {changes['conflicted']}"
        )
    if worktree["operations"]:
        elements.append("operations " + ",".join(worktree["operations"]))
    return elements


def _comparison_text(comparison: dict[str, Any] | None) -> str:
    return "unknown" if comparison is None else comparison["relation"]


def _remote_and_default_elements(
    clone: dict[str, Any], remote: dict[str, Any]
) -> list[str]:
    check = remote["check"]
    live = check["status"] == "live"
    if live:
        elements = ["remote live"]
    elif check["status"] == "off":
        elements = ["remote off"]
    else:
        elements = [f"remote failed {check['reason']}"]
    basis = "live" if live else "cached"
    relations = [
        (branch["vs_remote"] or {}).get("relation") for branch in clone["branches"]
    ]
    branches = f"branches {basis} local-only {relations.count('absent')}"
    unknown = relations.count("unknown")
    if unknown:
        branches += f" unknown {unknown}"
    elements.append(branches)
    local_default = clone["local_default"]
    if local_default is None:
        elements.append("no live default" if live else "no cached default")
        return elements
    cached = clone["cached_default"]
    fetched = None if cached is None else cached["last_fetched_at"]
    fetched_text = "never fetched" if fetched is None else f"fetched {fetched}"
    vs_cached = _comparison_text(local_default["vs_cached"])
    if live:
        elements.append(
            f"default {local_default['name']} live "
            f"{_comparison_text(local_default['vs_live'])} cached {vs_cached} "
            f"tracking live {_comparison_text(clone['cached_vs_live'])} {fetched_text}"
        )
    else:
        elements.append(f"default {local_default['name']} cached {vs_cached} {fetched_text}")
    return elements


def format_checkouts(document: dict[str, Any]) -> list[str]:
    """Return the plain-output lines of a parsed checkout document."""

    lines: list[str] = []
    git = document["git"]
    if git["status"] != "available":
        lines.append(
            f"git {git['status']}" + ("" if git["version"] is None else f" {git['version']}")
        )
    for folder in document["folders"]:
        if folder["status"] != "ok":
            lines.append(f"folder {_json_literal(folder['path'])} {folder['status']}")
    for diagnostic in document["diagnostics"]:
        lines.append(f"diagnostic {diagnostic['code']} {_json_literal(diagnostic['path'])}")
    for repository in document["repositories"]:
        remote = repository["remote"]
        for clone in repository["clones"]:
            path = _json_literal(clone["path"])
            if clone["status"] != "ok":
                lines.append(f"{path}  {clone['status']}")
                continue
            elements: list[str] = []
            if not clone["bare"]:
                main = next(w for w in clone["worktrees"] if w["main"])
                if main["status"] != "ok":
                    elements.append(f"main {main['status']}")
                else:
                    elements.extend(_worktree_elements(main))
            if remote is None:
                elements.append("no origin remote")
            else:
                elements.extend(_remote_and_default_elements(clone, remote))
            lines.append("  ".join([path, *elements]))
            for worktree in clone["worktrees"]:
                if worktree["main"]:
                    continue
                wpath = _json_literal(worktree["path"])
                if worktree["status"] != "ok":
                    lines.append(f"  {wpath}  {worktree['status']}")
                else:
                    lines.append("  ".join([f"  {wpath}", *_worktree_elements(worktree)]))
    return lines


def _checkouts_command(as_json: bool, no_network: bool) -> int:
    """Print the checkout facts of the active workspace without touching runtime.

    Remote checks run unless ``no_network`` is set.
    """

    try:
        configuration = state.observe_configuration()
    except state.StateError:
        configuration = state.ConfigurationObservation("unavailable")
    if configuration.status == "unavailable":
        print("nyx: Nyx configuration is unavailable", file=sys.stderr)
        return 1
    folders = (
        configuration.configuration.checkout_folders
        if configuration.status == "configured"
        else ()
    )
    if not folders:
        print("no checkout folders are configured")
        return 0
    deadline = time.monotonic_ns() + _CHECKOUTS_DEADLINE_SECONDS * 1_000_000_000
    try:
        rendered = checkouts.collect_checkouts(
            folders, deadline_ns=deadline, remote_checks=not no_network
        )
    except checkouts.CheckoutsOutputTooLargeError as error:
        print(f"nyx: {error.category}: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        # The verdict work needs a scratch directory; without one there is no document.
        reason = error.strerror or type(error).__name__
        print(f"nyx: checkouts: unable to collect ({reason})", file=sys.stderr)
        return 1
    if as_json:
        print(rendered)
    else:
        for line in format_checkouts(models.parse_checkouts(rendered)):
            print(line)
    return 0


def _status_snapshot() -> int:
    """Print the independent configuration and runtime observations."""

    try:
        configuration = state.observe_configuration()
    except state.StateError:
        configuration = state.ConfigurationObservation("unavailable")
    try:
        runtime_observation = runtime.observe_runtime()
    except runtime.RuntimeErrorBase:
        runtime_observation = runtime.RuntimeObservation("unknown")

    configuration_status = configuration.status
    if configuration_status == "configured":
        root = configuration.specification_root
        configuration_lines = [
            "Configuration: configured",
            f"Workspace: {_json_literal(str(root))}",
        ]
        configuration_diagnostic = None
    elif configuration_status == "not_configured":
        configuration_lines = [
            "Configuration: not configured",
            "Workspace: not configured",
        ]
        configuration_diagnostic = None
    else:
        configuration_lines = [
            "Configuration: unavailable",
            "Workspace: unavailable",
        ]
        configuration_diagnostic = "configuration unavailable"

    runtime_status = runtime_observation.status
    if runtime_status == "running":
        runtime_lines = ["Runtime: running"]
        if runtime_observation.url is not None:
            runtime_lines.append(f"URL: {_json_literal(runtime_observation.url)}")
        runtime_diagnostic = None
    elif runtime_status == "not_running":
        runtime_lines = ["Runtime: not running"]
        runtime_diagnostic = None
    else:
        runtime_lines = ["Runtime: unknown"]
        runtime_diagnostic = (
            runtime_observation.diagnostic
            if runtime_observation.diagnostic in {
                runtime.RUNTIME_OPERATION_IN_PROGRESS,
                runtime.RUNTIME_STATE_UNAVAILABLE,
                runtime.RUNTIME_STATE_CHANGED,
                runtime.RUNTIME_CONTROL_TIMED_OUT,
                runtime.RUNTIME_CONTROL_UNAVAILABLE,
                runtime.RUNTIME_CONTROL_IDENTITY_MISMATCH,
                runtime.RUNTIME_UNHEALTHY,
            }
            else runtime.RUNTIME_STATE_UNAVAILABLE
        )

    for line in (*configuration_lines, *runtime_lines):
        print(line)
    if configuration_diagnostic is not None:
        print(f"Diagnostic: {configuration_diagnostic}")
    if runtime_diagnostic is not None:
        print(f"Diagnostic: {runtime_diagnostic}")

    return int(configuration_status == "unavailable" or runtime_status == "unknown")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command not in (None, "checkouts"):
        parser.error(f"unrecognized arguments: {args.command}")
    if args.command == "checkouts":
        if _retired_arguments(args):
            parser.error("checkouts cannot be combined with --setup, --stop or --status")
        return _checkouts_command(args.json, args.no_network)
    if args.json:
        parser.error("--json is only available with checkouts")
    if args.no_network:
        parser.error("--no-network is only available with checkouts")
    if runtime.desktop_host():
        if _retired_arguments(args):
            print(f"nyx: {_DESKTOP_LIFECYCLE_REFUSAL}", file=sys.stderr)
            return 2
        return _launch_desktop()
    try:
        if args.status:
            return _status_snapshot()
        if args.setup is not None:
            configuration = runtime.setup(args.setup)
            print(f"configured {configuration.specification_root}")
        elif args.stop:
            print(runtime.stop())
        else:
            print(runtime.start())
        return 0
    except state.ConfigurationMissingError:
        print(
            "nyx: Nyx configuration is missing; run 'nyx --setup <workspace>' first",
            file=sys.stderr,
        )
        return 1
    except (state.StateError, runtime.RuntimeErrorBase) as error:
        print(f"nyx: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
