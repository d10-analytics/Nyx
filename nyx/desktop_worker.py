"""Console-capable private worker entry for the desktop application.

This entry owns no catalog behavior.  It delegates to the existing catalog
worker so a packaged application runs the same inherited-object validation,
configuration load, catalog scan, and byte protocol as the module entry.  It
never imports Qt and therefore cannot open a second application window.
"""

from __future__ import annotations

from . import worker


def main(argv: list[str] | None = None) -> int:
    """Run the shared catalog worker entry and return its category code."""

    return worker.worker_entrypoint(argv)


if __name__ == "__main__":  # pragma: no cover - packaged console entry
    raise SystemExit(main())


__all__ = ["main"]
