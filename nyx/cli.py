"""The three user-facing Nyx lifecycle commands."""

from __future__ import annotations

import argparse
import sys

from . import runtime, state


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nyx")
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--setup", metavar="SPEC_ROOT")
    commands.add_argument("--stop", action="store_true")
    policy = parser.add_mutually_exclusive_group()
    policy.add_argument("--hide-stage", action="append", metavar="NAME")
    policy.add_argument("--show-all-stages", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (args.hide_stage is not None or args.show_all_stages) and args.setup is None:
        _parser().error("--hide-stage and --show-all-stages require --setup")
    try:
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
