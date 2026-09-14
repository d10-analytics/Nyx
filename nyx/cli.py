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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.setup is not None:
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
