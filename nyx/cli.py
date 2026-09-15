"""The three user-facing Nyx lifecycle commands."""

from __future__ import annotations

import argparse
import json
import sys

from . import runtime, state


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nyx")
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--setup", metavar="SPEC_ROOT")
    commands.add_argument("--stop", action="store_true")
    commands.add_argument("--status", action="store_true")
    policy = parser.add_mutually_exclusive_group()
    policy.add_argument("--hide-stage", action="append", metavar="NAME")
    policy.add_argument("--show-all-stages", action="store_true")
    return parser


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
        hidden_stages = configuration.hidden_stages
        configuration_lines = [
            "Configuration: configured",
            f"Specification root: {_json_literal(str(root))}",
            "Hidden stages: " + _json_literal(list(hidden_stages or ())),
        ]
        configuration_diagnostic = None
    elif configuration_status == "not_configured":
        configuration_lines = [
            "Configuration: not configured",
            "Specification root: not configured",
            "Hidden stages: not configured",
        ]
        configuration_diagnostic = None
    else:
        configuration_lines = [
            "Configuration: unavailable",
            "Specification root: unavailable",
            "Hidden stages: unavailable",
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
    if (args.hide_stage is not None or args.show_all_stages) and args.setup is None:
        _parser().error("--hide-stage and --show-all-stages require --setup")
    try:
        if args.status:
            return _status_snapshot()
        if args.setup is not None:
            if args.hide_stage is not None:
                configuration = runtime.setup(args.setup, hidden_stages=args.hide_stage)
            elif args.show_all_stages:
                configuration = runtime.setup(args.setup, hidden_stages=())
            else:
                configuration = runtime.setup(args.setup)
            print(f"configured {configuration.specification_root}")
        elif args.stop:
            print(runtime.stop())
        else:
            print(runtime.start())
        return 0
    except (state.StateError, runtime.RuntimeErrorBase) as error:
        print(f"nyx: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
