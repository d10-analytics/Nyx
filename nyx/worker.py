"""Private catalog worker and direct-child cleanup owner."""

from __future__ import annotations

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
        rendered = scan_catalog(configuration.specification_root, version=2)
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

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._children)

    def fetch_catalog(self) -> bytes:
        """Fetch one catalog, ensuring every spawned child is registered."""

        with self._lock:
            if self._closing:
                raise WorkerError("producer_cancelled")
            child = _Child(
                subprocess.Popen(
                    self._command_factory(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    close_fds=True,
                )
            )
            self._children.append(child)
            if self._closing:
                child.cancelled = True
                child.process.terminate()

        try:
            try:
                stdout, stderr = child.process.communicate(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                with self._lock:
                    child.cancelled = True
                child.process.terminate()
                try:
                    stdout, stderr = child.process.communicate(timeout=1.0)
                except subprocess.TimeoutExpired as error:
                    raise WorkerError("producer_timeout") from error
                raise WorkerError("producer_timeout")
            if child.cancelled:
                raise WorkerError("producer_cancelled")
            if len(stdout) > MAX_STDOUT_BYTES or len(stderr) > MAX_STDERR_BYTES:
                raise WorkerError("producer_output_too_large")
            if child.process.returncode != 0:
                if child.process.returncode == 4:
                    raise WorkerError("producer_unavailable")
                raise WorkerError("producer_failed")
            return stdout
        finally:
            with self._lock:
                if child in self._children and child.process.poll() is not None:
                    self._children.remove(child)

    def close(self, deadline: float) -> bool:
        """Close admission and reap direct children before the deadline."""

        self.close_admission()
        while time.monotonic() < deadline:
            with self._lock:
                remaining = tuple(self._children)
            if not remaining:
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
        return not self._children

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
