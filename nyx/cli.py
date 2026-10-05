"""The three user-facing Nyx lifecycle commands."""

from __future__ import annotations

import argparse
import json
import sys

from . import runtime, state

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
    args = _parser().parse_args(argv)
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
