"""Private catalog worker and direct-child cleanup owner."""

from __future__ import annotations

import os
import selectors
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import state
from .catalog import scan_catalog

MAX_STDOUT_BYTES = 2 * 1024 * 1024
MAX_STDERR_BYTES = 8 * 1024
WORKER_TIMEOUT = 5.0


class WorkerError(RuntimeError):
    """A safe category returned by a catalog worker."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _worker_main() -> int:
    """Run the installed catalog engine using the persisted private config."""

    try:
        configuration = state.load_configuration()
        rendered = scan_catalog(
            configuration.specification_root,
            hidden_stages=configuration.hidden_stages,
        )
        encoded = rendered.encode("utf-8")
        if len(encoded) > MAX_STDOUT_BYTES:
            return 3
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
        return 0
    except (state.ConfigurationError, state.AccountHomeError, state.UnsupportedPlatformError):
        return 4
    except (OSError, ValueError, UnicodeError):
        return 5
    except Exception:  # noqa: BLE001 - worker boundary has a category-safe envelope
        return 6


@dataclass
class _Child:
    process: subprocess.Popen[bytes]
    cancelled: bool = False


@dataclass
class _Reservation:
    """Admission reservation held while a direct child is being spawned."""

    completed: bool = False


class CatalogWorkerManager:
    """Admit, register, cancel and reap catalog worker children."""

    def __init__(
        self,
        *,
        command_factory: Callable[[], list[str]] | None = None,
        timeout: float = WORKER_TIMEOUT,
    ) -> None:
        self._command_factory = command_factory or (
            lambda: [sys.executable, "-m", "nyx.worker"]
        )
        self._timeout = timeout
        self._lock = threading.RLock()
        self._closing = False
        self._children: list[_Child] = []
        self._reservations: list[_Reservation] = []

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._children) + len(self._reservations)

    def fetch_catalog(self) -> bytes:
        """Fetch one catalog, ensuring every spawned child is registered."""

        reservation = _Reservation()
        with self._lock:
            if self._closing:
                raise WorkerError("producer_cancelled")
            self._reservations.append(reservation)
        try:
            process = subprocess.Popen(
                self._command_factory(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
        except BaseException:
            with self._lock:
                self._reservations.remove(reservation)
            raise
        child = _Child(process)
        with self._lock:
            self._reservations.remove(reservation)
            reservation.completed = True
            self._children.append(child)
            if self._closing:
                child.cancelled = True
                child.process.terminate()

        try:
            return self._collect(child)
        finally:
            with self._lock:
                if child in self._children and child.process.poll() is not None:
                    self._children.remove(child)

    def _collect(self, child: _Child) -> bytes:
        """Read both pipes incrementally under one request deadline."""

        limits = {"stdout": MAX_STDOUT_BYTES, "stderr": MAX_STDERR_BYTES}
        captured = {"stdout": bytearray(), "stderr": bytearray()}
        selector = selectors.DefaultSelector()
        streams: dict[int, str] = {}
        for name, stream in (("stdout", child.process.stdout), ("stderr", child.process.stderr)):
            if stream is not None:
                stream_fd = stream.fileno()
                os.set_blocking(stream_fd, False)
                streams[stream_fd] = name
                selector.register(stream_fd, selectors.EVENT_READ)
        deadline = time.monotonic() + self._timeout
        try:
            while streams:
                if child.cancelled:
                    child.process.terminate()
                    raise WorkerError("producer_cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    child.cancelled = True
                    if child.process.poll() is None:
                        child.process.terminate()
                    raise WorkerError("producer_timeout")
                for key, _ in selector.select(remaining):
                    stream_fd = key.fd
                    name = streams[stream_fd]
                    try:
                        chunk = os.read(stream_fd, 64 * 1024)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(stream_fd)
                        streams.pop(stream_fd)
                        continue
                    captured[name].extend(chunk)
                    if len(captured[name]) > limits[name]:
                        child.cancelled = True
                        if child.process.poll() is None:
                            child.process.terminate()
                        raise WorkerError("producer_output_too_large")
            child.process.wait()
            if child.cancelled:
                raise WorkerError("producer_cancelled")
            if child.process.returncode != 0:
                if child.process.returncode == 4:
                    raise WorkerError("producer_unavailable")
                raise WorkerError("producer_failed")
            return bytes(captured["stdout"])
        finally:
            selector.close()
            for stream in (child.process.stdout, child.process.stderr):
                if stream is not None:
                    stream.close()

    def close(self, deadline: float) -> bool:
        """Close admission and reap direct children before the deadline."""

        self.close_admission()
        while time.monotonic() < deadline:
            with self._lock:
                remaining = tuple(self._children)
                spawning = tuple(self._reservations)
            if not remaining and not spawning:
                return True
            for child in remaining:
                try:
                    child.process.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
            with self._lock:
                for child in tuple(self._children):
                    if child.process.poll() is not None:
                        self._children.remove(child)
        with self._lock:
            return not self._children and not self._reservations

    def close_admission(self) -> None:
        """Prevent new requests and cancel currently registered children."""

        with self._lock:
            self._closing = True
            children = tuple(self._children)
            for child in children:
                child.cancelled = True
                child.process.terminate()


if __name__ == "__main__":
    raise SystemExit(_worker_main())


__all__ = ["CatalogWorkerManager", "WorkerError"]
