"""Shared loopback-port isolation for tests that start the Nyx service.

The service binds the fixed public port held in ``runtime.PORT``.  Modules that
start the service opt into ``isolated_port`` so they never contend for that
port with a running Nyx; the few deliberate fixed-port proofs carry the
``fixed_port`` marker and skip when the port is already held.

Tests that run a daemon on a thread go through ``managed_daemon_run`` so a
failing assertion still shuts the daemon down and joins its thread.
"""

from __future__ import annotations

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


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "fixed_port: the test uses the real fixed Nyx port; module port isolation is off",
    )


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
