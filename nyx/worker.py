"""Private catalog worker and direct-child cleanup owner."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import state
from ._native_claim import NativeClaim
from .catalog import scan_catalog

MAX_STDOUT_BYTES = 2 * 1024 * 1024
MAX_STDERR_BYTES = 8 * 1024
WORKER_TIMEOUT = 5.0
_READ_CHUNK_BYTES = 64 * 1024


class WorkerError(RuntimeError):
    """A safe category returned by a catalog worker."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _ParentLossObserver:
    """Terminate a desktop worker when its shell closes or disappears."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        # Python 3.12 supports non-blocking anonymous pipes on every supported
        # Nyx platform, including Windows.  Keeping the read non-blocking lets
        # normal worker completion stop and join this observer without closing
        # a descriptor out from under a blocking read in another thread.
        os.set_blocking(self.fd, False)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._observe,
            daemon=True,
            name="nyx-worker-parent-observer",
        )

    def start(self) -> None:
        self._thread.start()

    def _observe(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    data = os.read(self.fd, 1)
                except BlockingIOError:
                    self._stop.wait(0.05)
                    continue
                except OSError:
                    return
                if not data and not self._stop.is_set():
                    os._exit(7)
        finally:
            try:
                os.close(self.fd)
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self._thread.join()


def _receive_worker_inheritance(
    recovery_fd: int | None,
    recovery_path: Path | None,
    parent_liveness_fd: int | None,
) -> tuple[NativeClaim | None, _ParentLossObserver | None] | None:
    """Validate desktop inheritance before configuration can be loaded."""

    if recovery_fd is None and recovery_path is None and parent_liveness_fd is None:
        return (None, None)
    if recovery_fd is None or recovery_path is None or parent_liveness_fd is None:
        return None
    received_fd: int | None = None
    liveness_fd: int | None = None
    claim: NativeClaim | None = None
    observer: _ParentLossObserver | None = None
    succeeded = False
    try:
        received_fd = NativeClaim.receive_handle(recovery_fd)
        liveness_fd = NativeClaim.receive_handle(parent_liveness_fd, read_only=True)
        claim = NativeClaim(recovery_path)
        if not claim.adopt_received(received_fd):
            return None
        received_fd = None
        observer = _ParentLossObserver(liveness_fd)
        observer.start()
        liveness_fd = None
        succeeded = True
        return claim, observer
    except (OSError, RuntimeError, ValueError):
        return None
    finally:
        if not succeeded and claim is not None:
            claim.close()
        for fd in (received_fd, liveness_fd):
            if fd is None:
                continue
            try:
                os.close(fd)
            except OSError:
                pass


def _worker_main(
    *,
    recovery_fd: int | None = None,
    recovery_path: Path | None = None,
    parent_liveness_fd: int | None = None,
) -> int:
    """Run the catalog engine using the persisted private configuration."""

    inherited = _receive_worker_inheritance(
        recovery_fd, recovery_path, parent_liveness_fd
    )
    if inherited is None:
        if recovery_fd is not None or recovery_path is not None or parent_liveness_fd is not None:
            return 4
        claim = None
        observer = None
    else:
        claim, observer = inherited

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
    finally:
        if observer is not None:
            observer.close()
        if claim is not None:
            claim.close()


@dataclass
class _Child:
    process: subprocess.Popen[bytes]
    cancelled: bool = False
    terminal_reason: str | None = None
    finalizer_started: bool = False
    finalizer_done: threading.Event = field(default_factory=threading.Event)
    progress: threading.Event = field(default_factory=threading.Event)
    readers_complete: threading.Event = field(default_factory=threading.Event)
    reader_threads: list[threading.Thread] = field(default_factory=list)
    reader_count: int = 0
    reader_done_count: int = 0
    retained_bytes: dict[str, int] = field(default_factory=lambda: {"stdout": 0, "stderr": 0})
    captured: dict[str, bytearray] = field(
        default_factory=lambda: {"stdout": bytearray(), "stderr": bytearray()}
    )


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
        recovery_claim: NativeClaim | None = None,
        recovery_path: Path | None = None,
        parent_liveness_fd: int | None = None,
    ) -> None:
        self._command_factory = command_factory or (
            lambda: [sys.executable, "-m", "nyx.worker"]
        )
        self._timeout = timeout
        self._recovery_claim = recovery_claim
        self._recovery_path = recovery_path or (
            None if recovery_claim is None else recovery_claim.path
        )
        self._parent_liveness_fd = parent_liveness_fd
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
            spawn_kwargs = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "close_fds": True,
            }
            inherited_handles: list[int] = []
            if self._recovery_claim is not None:
                if self._recovery_claim.fd is None or self._parent_liveness_fd is None:
                    raise WorkerError("producer_unavailable")
                if os.name != "nt":
                    spawn_kwargs["pass_fds"] = (
                        self._recovery_claim.fd,
                        self._parent_liveness_fd,
                    )
                else:  # pragma: no cover - exercised by the native Windows lane
                    inherited_handles = [
                        NativeClaim.transfer_handle(self._recovery_claim.fd),
                        NativeClaim.transfer_handle(self._parent_liveness_fd),
                    ]
                    for handle in inherited_handles:
                        os.set_handle_inheritable(handle, True)
                    startup = subprocess.STARTUPINFO()
                    startup.lpAttributeList = {"handle_list": inherited_handles}
                    spawn_kwargs["startupinfo"] = startup
            process = subprocess.Popen(
                self._worker_command(),
                **spawn_kwargs,
            )
        except BaseException:
            with self._lock:
                self._reservations.remove(reservation)
            raise
        finally:
            if os.name == "nt":  # pragma: no cover - native Windows lane
                for handle in inherited_handles:
                    os.set_handle_inheritable(handle, False)

        child = _Child(process)
        with self._lock:
            self._reservations.remove(reservation)
            reservation.completed = True
            self._children.append(child)
            self._start_readers_locked(child)
            if self._closing:
                child.cancelled = True
                self._select_terminal_locked(child, "producer_cancelled")

        try:
            return self._collect(child)
        finally:
            # Cleanup is manager-owned and deliberately continues after the
            # request returns.  In particular, do not remove a child merely
            # because poll() reports exit while a reader still owns a pipe.
            with self._lock:
                if child.terminal_reason is None:
                    self._select_terminal_locked(child, "producer_failed")
                else:
                    self._start_finalizer_locked(child)

    def _worker_command(self) -> list[str]:
        """Add only the catalog worker's inherited desktop objects."""

        command = list(self._command_factory())
        if self._recovery_claim is None:
            return command
        if self._recovery_claim.fd is None or self._parent_liveness_fd is None:
            raise WorkerError("producer_unavailable")
        if self._recovery_path is None:
            raise WorkerError("producer_unavailable")
        command.extend(
            [
                "--recovery-fd",
                str(
                    self._recovery_claim.fd
                    if os.name != "nt"
                    else NativeClaim.transfer_handle(self._recovery_claim.fd)
                ),
                "--recovery-path",
                str(self._recovery_path),
                "--parent-liveness-fd",
                str(
                    self._parent_liveness_fd
                    if os.name != "nt"
                    else NativeClaim.transfer_handle(self._parent_liveness_fd)
                ),
            ]
        )
        return command

    def _start_readers_locked(self, child: _Child) -> None:
        """Start one blocking reader per pipe while the child is registered."""

        if child.reader_threads:
            return
        streams = (
            ("stdout", child.process.stdout, MAX_STDOUT_BYTES),
            ("stderr", child.process.stderr, MAX_STDERR_BYTES),
        )
        for name, stream, limit in streams:
            if stream is None:
                continue
            reader = threading.Thread(
                target=self._read_stream,
                args=(child, name, stream, limit),
                daemon=True,
                name=f"nyx-worker-{name}",
            )
            child.reader_threads.append(reader)
            child.reader_count += 1
            reader.start()

    def _read_stream(self, child: _Child, name: str, stream: object, limit: int) -> None:
        """Drain one ordinary blocking pipe, retaining only its bounded prefix."""

        try:
            while True:
                chunk = stream.read(_READ_CHUNK_BYTES)  # type: ignore[attr-defined]
                if not chunk:
                    break
                with self._lock:
                    if child.terminal_reason is not None:
                        # Once another terminal reason wins, continue draining
                        # so the sibling reader and child can reach EOF without
                        # retaining bytes that cannot be published.
                        continue
                    remaining = limit - len(child.captured[name])
                    if len(chunk) > remaining:
                        if remaining:
                            child.captured[name].extend(chunk[:remaining])
                        child.retained_bytes[name] = len(child.captured[name])
                        self._select_terminal_locked(child, "producer_output_too_large")
                        continue
                    child.captured[name].extend(chunk)
                    child.retained_bytes[name] = len(child.captured[name])
        except (OSError, ValueError):
            with self._lock:
                if child.terminal_reason is None:
                    self._select_terminal_locked(child, "producer_failed")
        finally:
            with self._lock:
                child.reader_done_count += 1
                if child.reader_done_count >= child.reader_count:
                    child.readers_complete.set()
                child.progress.set()

    def _select_terminal_locked(self, child: _Child, reason: str) -> None:
        """Select the first terminal request reason and start its finalizer."""

        if child.terminal_reason is not None:
            return
        child.terminal_reason = reason
        if reason != "producer_failed":
            child.cancelled = reason in {
                "producer_cancelled",
                "producer_timeout",
                "producer_output_too_large",
            }
        if child.process.poll() is None and reason != "producer_failed":
            try:
                child.process.terminate()
            except (OSError, ProcessLookupError):
                pass
        child.progress.set()
        self._start_finalizer_locked(child)

    def _start_finalizer_locked(self, child: _Child) -> None:
        """Start exactly one autonomous manager-owned cleanup operation."""

        if child.finalizer_started:
            return
        child.finalizer_started = True
        threading.Thread(
            target=self._finalize_child,
            args=(child,),
            daemon=True,
            name="nyx-worker-finalizer",
        ).start()

    def _finalize_child(self, child: _Child) -> None:
        """Terminate, reap, drain and close a child without request coupling."""

        try:
            if child.process.poll() is None:
                try:
                    child.process.terminate()
                except (OSError, ProcessLookupError):
                    pass
            # No request deadline is used here.  close() supplies the separate
            # manager shutdown deadline while this operation retains ownership.
            child.process.wait()
            for reader in child.reader_threads:
                reader.join()
            for stream in (child.process.stdout, child.process.stderr):
                if stream is not None:
                    stream.close()
        finally:
            with self._lock:
                if child.process.poll() is not None and child.readers_complete.is_set():
                    if child in self._children:
                        self._children.remove(child)
                    child.finalizer_done.set()
                child.progress.set()

    def _terminal_error(self, child: _Child) -> WorkerError | None:
        with self._lock:
            reason = child.terminal_reason
        if reason in {None, "success"}:
            return None
        return WorkerError(reason)

    def _collect(self, child: _Child) -> bytes:
        """Collect both pipes under one absolute request deadline."""

        deadline = time.monotonic() + self._timeout
        while True:
            error = self._terminal_error(child)
            if error is not None:
                raise error
            wait_for_process = False
            with self._lock:
                if child.readers_complete.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._select_terminal_locked(child, "producer_timeout")
                        raise WorkerError("producer_timeout")
                    wait_for_process = True

                if not wait_for_process:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._select_terminal_locked(child, "producer_timeout")
                        raise WorkerError("producer_timeout")
            if wait_for_process:
                try:
                    child.process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    with self._lock:
                        if child.terminal_reason is None:
                            self._select_terminal_locked(child, "producer_timeout")
                        reason = child.terminal_reason
                    if reason not in {None, "success"}:
                        raise WorkerError(reason)
                    continue
                with self._lock:
                    if child.terminal_reason is not None:
                        reason = child.terminal_reason
                        if reason != "success":
                            raise WorkerError(reason)
                        continue
                    if self._closing or child.cancelled:
                        self._select_terminal_locked(child, "producer_cancelled")
                        raise WorkerError("producer_cancelled")
                    if child.process.returncode == 0:
                        # Keep the final cancellation check and result capture
                        # under the manager lock so close cannot publish bytes
                        # after it has won the ordering race.
                        child.terminal_reason = "success"
                        self._start_finalizer_locked(child)
                        return bytes(child.captured["stdout"])
                    code = (
                        "producer_unavailable" if child.process.returncode == 4 else "producer_failed"
                    )
                    self._select_terminal_locked(child, code)
                    raise WorkerError(code)
            child.progress.wait(timeout=remaining)
            child.progress.clear()

    def close(self, deadline: float) -> bool:
        """Close admission and reap direct children before one shared deadline."""

        self.close_admission()
        while True:
            remaining_time = deadline - time.monotonic()
            with self._lock:
                if not self._children and not self._reservations:
                    return True
                children = tuple(self._children)
                for child in children:
                    child.cancelled = True
                    self._select_terminal_locked(child, "producer_cancelled")
            if remaining_time <= 0:
                return False
            for child in children:
                child.finalizer_done.wait(timeout=max(0.0, deadline - time.monotonic()))
                if time.monotonic() >= deadline:
                    break

    def close_admission(self) -> None:
        """Prevent new requests and cancel currently registered children."""

        with self._lock:
            self._closing = True
            for child in tuple(self._children):
                child.cancelled = True
                self._select_terminal_locked(child, "producer_cancelled")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(prog="nyx.worker")
    parser.add_argument("--recovery-fd", type=int)
    parser.add_argument("--recovery-path", type=Path)
    parser.add_argument("--parent-liveness-fd", type=int)
    options = parser.parse_args()
    raise SystemExit(
        _worker_main(
            recovery_fd=options.recovery_fd,
            recovery_path=options.recovery_path,
            parent_liveness_fd=options.parent_liveness_fd,
        )
    )


__all__ = ["CatalogWorkerManager", "WorkerError"]
