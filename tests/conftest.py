"""Shared loopback-port isolation for tests that start the Nyx service.

The service binds the fixed public port held in ``runtime.PORT``.  Modules that
start the service opt into ``isolated_port`` so they never contend for that
port with a running Nyx; the few deliberate fixed-port proofs carry the
``fixed_port`` marker and skip when the port is already held.

Tests that run a daemon on a thread go through ``managed_daemon_run`` so a
failing assertion still shuts the daemon down and joins its thread.

The ``--nyx-shard=I/N`` option splits one collected suite across ``N`` runs.
``I`` is the 0-based shard index (``0 <= I < N``, no leading zeros) and ``N``
the shard count; a run keeps the collected tests whose 0-based position ``i``
satisfies ``i % N == I`` and reports the rest as deselected.  The selection
applies after ``-k``, ``-m`` and ``--deselect``, so it shards whatever those
chose.  A malformed value, including an empty one, stops the session before
collection with a usage error (exit 4) that names the option and the value.
Without the option nothing is deselected and pytest runs everything.  The
option lives on the command line only: child pytest sessions started by tests
never receive it and collect in full.
"""

from __future__ import annotations

import re
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from nyx import runtime

# The public fixed port, captured before any test patches ``runtime.PORT``.
SERVICE_PORT = runtime.PORT
SERVICE_PORT_HELD_REASON = "the fixed Nyx control port is held by another process"
LOOPBACK_DENIED_REASON = "sandbox does not permit loopback sockets"


SHARD_OPTION = "--nyx-shard"
_SHARD_PATTERN = re.compile(r"(0|[1-9][0-9]*)/([1-9][0-9]*)")


def parse_shard(value: str) -> tuple[int, int]:
    """Return ``(index, total)`` for a ``I/N`` shard value.

    The grammar is strict: 0-based ``I`` and ``N >= 1`` as plain decimal
    numbers without leading zeros, signs or whitespace, and ``I < N``.  Any
    other value, the empty string included, raises ``pytest.UsageError`` naming
    the option and the received value.
    """

    match = _SHARD_PATTERN.fullmatch(value)
    if match is not None:
        index, total = int(match.group(1)), int(match.group(2))
        if index < total:
            return index, total
    raise pytest.UsageError(
        f"{SHARD_OPTION} expects I/N with 0 <= I < N (for example 0/2), got {value!r}"
    )


def select_shard(items: list[Any], index: int, total: int) -> tuple[list[Any], list[Any]]:
    """Split ``items`` into the shard ``index`` of ``total`` and the rest.

    The shard keeps the items at positions ``i`` with ``i % total == index``;
    both lists keep the original order.
    """

    selected = items[index::total]
    chosen = {id(item) for item in selected}
    return selected, [item for item in items if id(item) not in chosen]


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        SHARD_OPTION,
        action="store",
        default=None,
        metavar="I/N",
        help="run only shard I (0-based) of N: the tests whose collection index i has i %% N == I",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "fixed_port: the test uses the real fixed Nyx port; module port isolation is off",
    )
    value = config.getoption("nyx_shard", default=None)
    if value is not None:
        parse_shard(value)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[Any]) -> None:
    value = config.getoption("nyx_shard", default=None)
    if value is None:
        return
    selected, deselected = select_shard(items, *parse_shard(value))
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = selected


def _free_loopback_port() -> int:
    """Return a loopback port that was free when probed.

    Another process can claim the port before the service binds it; that
    collision surfaces as the service's port-conflict failure, never a retry.
    """

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except PermissionError:
        pytest.skip(LOOPBACK_DENIED_REASON)
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    except PermissionError:
        pytest.skip(LOOPBACK_DENIED_REASON)
    finally:
        probe.close()


@pytest.fixture
def isolated_port(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> int:
    """Point ``runtime.PORT``, ``runtime.URL`` and the conflict message at a free port.

    The three names change together through the runtime's own formula and are
    restored after the test.  A ``fixed_port`` test keeps the public port.
    """

    if request.node.get_closest_marker("fixed_port") is not None:
        return runtime.PORT
    port = _free_loopback_port()
    url, message = runtime._port_bindings(port)
    monkeypatch.setattr(runtime, "PORT", port)
    monkeypatch.setattr(runtime, "URL", url)
    monkeypatch.setattr(runtime, "PORT_UNAVAILABLE_MESSAGE", message)
    return port


def distinct_free_port() -> int:
    """Return a free loopback port other than the test's own ``runtime.PORT``."""

    while True:
        port = _free_loopback_port()
        if port != runtime.PORT:
            return port


def skip_if_service_port_is_held() -> None:
    """Skip a fixed-port proof when another process already owns the public port."""

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except PermissionError:
        pytest.skip(LOOPBACK_DENIED_REASON)
    try:
        # Mirror the service's listener, which allows address reuse: TIME_WAIT
        # sockets left by earlier requests must not block the probe, while a
        # live listener on the port still makes it fail.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", SERVICE_PORT))
    except PermissionError:
        pytest.skip(LOOPBACK_DENIED_REASON)
    except OSError:
        pytest.skip(SERVICE_PORT_HELD_REASON)
    finally:
        probe.close()


class DaemonRun:
    """A daemon's ``run()`` executing on a test-owned thread."""

    def __init__(self, daemon: Any) -> None:
        self.daemon = daemon
        self.returncode: int | None = None
        self.thread = threading.Thread(
            target=self._run, name="nyx-test-daemon-run", daemon=False
        )

    def _run(self) -> None:
        self.returncode = self.daemon.run()

    def join(self, timeout: float) -> int | None:
        """Wait up to ``timeout`` seconds and return ``run()``'s exit code, if any."""

        self.thread.join(timeout=timeout)
        return self.returncode

    def is_alive(self) -> bool:
        return self.thread.is_alive()


@contextmanager
def managed_daemon_run(
    daemon: Any,
    *,
    shutdown_timeout: float = 10.0,
    join_timeout: float = 10.0,
    retry_interval: float = 0.05,
) -> Iterator[DaemonRun]:
    """Run ``daemon.run()`` on a thread that is always shut down and joined.

    Use it innermost, inside the temporary directory and any ``_paths`` patch,
    so cleanup still sees the daemon's state.  On exit, however the block
    ends, ``daemon.shutdown`` is called and called again while it reports
    ``"timeout"`` until ``shutdown_timeout`` elapses; the thread is then joined
    for at most ``join_timeout`` seconds and the test fails if it is still
    alive.  Repeated shutdown is safe, so a block that already stopped the
    daemon costs one immediate call.  Gates the test holds must be released in
    the test's own ``finally`` inside the block, before this cleanup runs.
    """

    run = DaemonRun(daemon)
    run.thread.start()
    try:
        yield run
    finally:
        deadline = time.monotonic() + shutdown_timeout
        result = None
        try:
            result = daemon.shutdown(deadline)
            while result == "timeout" and time.monotonic() < deadline:
                time.sleep(retry_interval)
                result = daemon.shutdown(deadline)
        finally:
            run.thread.join(timeout=join_timeout)
            if run.thread.is_alive():
                pytest.fail(
                    "daemon run thread is still alive after shutdown "
                    f"(last shutdown result: {result!r})",
                    pytrace=False,
                )
