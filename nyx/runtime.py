"""Per-user Nyx lifecycle ownership and authenticated local control."""

from __future__ import annotations

import base64
import errno
import http.client
import json
import os
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from . import state
from ._native_claim import NativeClaim, NativeDirectory
from .models import Catalog, ProtocolError, parse_catalog
from .server import CatalogError, TrackerServer, create_server
from .worker import CatalogWorkerManager, WorkerError

PORT = 8765
URL = f"http://127.0.0.1:{PORT}/"
SCHEMA_VERSION = 1
LOCK_TIMEOUT = 5.0
STARTUP_TIMEOUT = 5.0
SHUTDOWN_TIMEOUT = 5.0
RUNTIME_CONTROL_TIMEOUT = 1.0

RUNTIME_OPERATION_IN_PROGRESS = "runtime operation in progress"
RUNTIME_STATE_UNAVAILABLE = "runtime state unavailable"
RUNTIME_STATE_CHANGED = "runtime state changed"
RUNTIME_CONTROL_TIMED_OUT = "runtime control timed out"
RUNTIME_CONTROL_UNAVAILABLE = "runtime control unavailable"
RUNTIME_CONTROL_IDENTITY_MISMATCH = "runtime control identity mismatch"
RUNTIME_UNHEALTHY = "runtime unhealthy"

# Private exit statuses keep detached-child failures observable without retaining
# child output, exception text, filesystem paths, or capability material.
_DAEMON_FAILURES = {
    20: "received-claim-rejected",
    21: "acknowledgement-write-failed",
    22: "daemon-initialization-failed",
    23: "configuration-load-failed",
    24: "http-start-failed",
    25: "static-readiness-failed",
    26: "control-bind-failed",
    27: "control-readiness-failed",
    28: "locator-publication-failed",
    29: "serving-failed",
    30: "fixed-port-unavailable",
}


def _startup_failure(message: str, process: subprocess.Popen[bytes], phase: str) -> StartupError:
    error = StartupError(message)
    code = process.poll()
    outcome = "child-running" if code is None else _DAEMON_FAILURES.get(code, "child-exited")
    error.add_note(f"internal startup phase={phase}; outcome={outcome}; exit={code}")
    return error


class RuntimeErrorBase(RuntimeError):
    """Base class for safe lifecycle operation failures."""


class OperationBusyError(RuntimeErrorBase):
    pass


class ActiveInstanceError(RuntimeErrorBase):
    pass


class UnhealthyInstanceError(RuntimeErrorBase):
    pass


class StartupError(RuntimeErrorBase):
    pass


class PortConflictError(StartupError):
    pass


class ShutdownTimeoutError(RuntimeErrorBase):
    pass


class _ControlTimeoutError(UnhealthyInstanceError):
    pass


class _ControlUnavailableError(UnhealthyInstanceError):
    pass


class _ControlIdentityError(UnhealthyInstanceError):
    pass


@dataclass(frozen=True)
class Instance:
    instance_id: str
    url: str
    capability: str
    control: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "schema_version": SCHEMA_VERSION,
            "instance_id": self.instance_id,
            "url": self.url,
            "capability": self.capability,
            "control": self.control,
        }


class _FileLock:
    def __init__(
        self,
        path: Path,
        *,
        timeout: float = LOCK_TIMEOUT,
        deadline: float | None = None,
    ) -> None:
        self.path = path
        self.timeout = timeout
        self.deadline = deadline
        self._claim = NativeClaim(path)

    @property
    def fd(self) -> int | None:
        return self._claim.fd

    def acquire(self, *, blocking: bool = True) -> bool:
        deadline = self.deadline
        if deadline is not None and time.monotonic() >= deadline:
            raise _ControlTimeoutError("operation deadline expired")
        try:
            claim_deadline = (
                deadline
                if deadline is not None
                else (time.monotonic() + self.timeout if blocking else None)
            )
            return self._claim.acquire(
                create=True,
                blocking=blocking,
                deadline=claim_deadline,
            )
        except TimeoutError as error:
            raise _ControlTimeoutError("operation deadline expired") from error
        except OSError as error:
            raise RuntimeErrorBase("Nyx lock is unavailable") from error

    def close(self) -> None:
        self._claim.close()

    def __enter__(self) -> Self:
        if not self.acquire():
            raise OperationBusyError("another Nyx operation is in progress")
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(frozen=True)
class _Metadata:
    """The identity and contents needed to detect a lifecycle race."""

    exists: bool
    identity: tuple[int, int, int, int, int, int] | None = None
    data: bytes | None = None


class _ExistingLock:
    """A descriptor-backed lock opened without creating its path."""

    def __init__(self, path: Path, *, directory: NativeDirectory | None = None) -> None:
        self.path = path
        self.directory = directory
        self._claim = NativeClaim(
            path,
            dir_fd=None if directory is None else directory.dir_fd,
        )
        self.metadata: _Metadata | None = None

    @property
    def fd(self) -> int | None:
        return self._claim.fd

    def acquire(self) -> str:
        result = NativeClaim.probe(
            self.path,
            dir_fd=None if self.directory is None else self.directory.dir_fd,
        )
        if result != "free":
            return result
        try:
            if not self._claim.acquire(create=False, blocking=False):
                return "held"
            assert self._claim.fd is not None
            self.metadata = _metadata_from_stat(os.fstat(self._claim.fd))
            return "acquired"
        except OSError:
            return "unsafe"

    def close(self) -> None:
        self._claim.close()

    def __enter__(self) -> Self:
        if self.acquire() != "acquired":
            raise RuntimeErrorBase("existing Nyx lock could not be acquired")
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _metadata_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        getattr(details, "st_uid", 0),
        stat.S_IMODE(details.st_mode),
        details.st_size,
        details.st_mtime_ns,
    )


def _metadata_from_stat(details: os.stat_result, data: bytes | None = None) -> _Metadata:
    return _Metadata(True, _metadata_identity(details), data)


def _safe_lock_metadata(details: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return (
        stat.S_ISREG(details.st_mode)
        and not stat.S_ISLNK(details.st_mode)
        and not getattr(details, "st_file_attributes", 0) & reparse_flag
    )


def _read_descriptor(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _read_metadata(
    path: Path,
    *,
    read_data: bool = False,
    directory: NativeDirectory | None = None,
) -> _Metadata:
    try:
        details = path.lstat() if directory is None else directory.stat_child(path.name)
    except FileNotFoundError:
        return _Metadata(False)
    if not _safe_lock_metadata(details):
        raise RuntimeErrorBase("unsafe Nyx runtime state")
    data: bytes | None = None
    if read_data:
        if directory is None:
            data = path.read_bytes()
        else:
            fd = directory.open_file(path.name)
            try:
                admitted = os.fstat(fd)
                if not _safe_lock_metadata(admitted) or _metadata_identity(
                    details
                ) != _metadata_identity(admitted):
                    raise RuntimeErrorBase("unsafe Nyx runtime state")
                data = _read_descriptor(fd)
                if _metadata_identity(admitted) != _metadata_identity(os.fstat(fd)):
                    raise RuntimeErrorBase("Nyx runtime state changed during read")
            finally:
                os.close(fd)
    return _metadata_from_stat(details, data)


def _metadata_unchanged(
    path: Path,
    original: _Metadata,
    *,
    read_data: bool = False,
    directory: NativeDirectory | None = None,
) -> bool:
    try:
        current = _read_metadata(path, read_data=read_data, directory=directory)
    except (OSError, RuntimeErrorBase):
        return False
    return current == original


def _record_snapshot(
    path: Path, *, directory: NativeDirectory | None = None
) -> _Metadata:
    """Capture record metadata and bytes so equal-size rewrites are observable."""

    return _read_metadata(path, read_data=True, directory=directory)


def _record_unchanged(
    path: Path,
    original: _Metadata,
    *,
    directory: NativeDirectory | None = None,
) -> bool:
    return _metadata_unchanged(
        path,
        original,
        read_data=True,
        directory=directory,
    )


def _directory_stamp(
    path: Path, *, directory: NativeDirectory | None = None
) -> tuple[int, int, int, int, int, int, int]:
    details = path.lstat() if directory is None else directory.stat_self()
    if not stat.S_ISDIR(details.st_mode):
        raise RuntimeErrorBase("unsafe Nyx runtime state")
    return (
        details.st_dev,
        details.st_ino,
        getattr(details, "st_uid", 0),
        stat.S_IMODE(details.st_mode),
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _directory_unchanged(
    path: Path,
    original: tuple[int, int, int, int, int, int, int],
    *,
    directory: NativeDirectory | None = None,
) -> bool:
    try:
        return _directory_stamp(path, directory=directory) == original
    except (OSError, RuntimeErrorBase):
        return False


def _require_deadline(deadline: float) -> None:
    if deadline - time.monotonic() <= 0:
        raise _ControlTimeoutError("control deadline expired")


def _paths(
    *,
    create: bool = True,
    deadline: float | None = None,
    deadline_ns: int | None = None,
) -> state.StatePaths:
    return state.state_paths(create=create, deadline=deadline, deadline_ns=deadline_ns)


def _operation_lock(
    paths: state.StatePaths,
    *,
    timeout: float = LOCK_TIMEOUT,
    deadline: float | None = None,
) -> _FileLock:
    return _FileLock(paths.runtime_directory / "operation.lock", timeout=timeout, deadline=deadline)


def _lease_lock(
    paths: state.StatePaths,
    *,
    timeout: float = LOCK_TIMEOUT,
    deadline: float | None = None,
) -> _FileLock:
    return _FileLock(paths.runtime_directory / "lease.lock", timeout=timeout, deadline=deadline)


def _control_name() -> str:
    uid = getattr(os, "getuid", lambda: 0)()
    return f"\x00nyx-control-{uid}"


def _control_endpoint(port: int) -> str:
    return f"127.0.0.1:{port}"


def _parse_control_endpoint(value: str) -> tuple[int, str | tuple[str, int]]:
    """Parse the closed, published TCP control endpoint grammar.

    Control records are persisted input, so validation must reject every
    spelling outside the one endpoint form that Nyx publishes before any
    socket or capability operation can occur.
    """

    prefix = "127.0.0.1:"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValueError("control endpoint is not loopback")
    raw_port = value[len(prefix) :]
    if (
        not raw_port
        or len(raw_port) > 5
        or raw_port.startswith("0")
        or any(char < "0" or char > "9" for char in raw_port)
    ):
        raise ValueError("control endpoint port is invalid")
    port = int(raw_port)
    if port > 65535:
        raise ValueError("control endpoint port is invalid")
    return socket.AF_INET, (prefix[:-1], port)


def _record_path(paths: state.StatePaths) -> Path:
    return paths.runtime_directory / "instance.json"


def _read_instance(
    paths: state.StatePaths,
    *,
    directory: NativeDirectory | None = None,
) -> Instance:
    path = _record_path(paths)
    try:
        record = _record_snapshot(path, directory=directory)
        if not record.exists or record.data is None:
            raise FileNotFoundError(path)
        payload = json.loads(record.data.decode("utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != SCHEMA_VERSION
            or set(payload) != {"schema_version", "instance_id", "url", "capability", "control"}
            or payload.get("url") != URL
            or not all(isinstance(payload.get(key), str) and payload[key] for key in ("instance_id", "capability"))
        ):
            raise UnhealthyInstanceError("Nyx instance record is invalid")
        control = payload.get("control")
        if not isinstance(control, str):
            raise UnhealthyInstanceError("Nyx instance record is invalid")
        try:
            _parse_control_endpoint(control)
        except (TypeError, ValueError):
            raise UnhealthyInstanceError("Nyx instance record is invalid") from None
        return Instance(
            instance_id=payload["instance_id"],
            url=payload["url"],
            capability=payload["capability"],
            control=control,
        )
    except FileNotFoundError as error:
        raise UnhealthyInstanceError("Nyx instance is not ready") from error
    except (OSError, RuntimeErrorBase, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UnhealthyInstanceError("Nyx instance record is unavailable") from error


def _write_instance(
    paths: state.StatePaths,
    instance: Instance,
    *,
    deadline: float,
) -> _Metadata:
    data = (json.dumps(instance.as_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd: int | None = None
    temporary: str | None = None
    try:
        _require_deadline(deadline)
        fd, temporary = tempfile.mkstemp(prefix=".instance.json.", dir=paths.runtime_directory)
        _require_deadline(deadline)
        written = 0
        while written < len(data):
            _require_deadline(deadline)
            written += os.write(fd, data[written:])
        _require_deadline(deadline)
        os.fsync(fd)
        os.close(fd)
        fd = None
        _require_deadline(deadline)
        os.replace(temporary, _record_path(paths))
        temporary = None
        return _record_snapshot(_record_path(paths))
    except _ControlTimeoutError as error:
        raise StartupError("Nyx startup timed out before readiness publication") from error
    except OSError as error:
        raise StartupError("Nyx could not publish readiness") from error
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _remove_stale_instance(paths: state.StatePaths, *, deadline: float) -> None:
    try:
        record = _record_path(paths)
        details = record.lstat()
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
        ):
            raise UnhealthyInstanceError("Nyx instance record is unsafe")
        # A stale record is still persisted control input.  Validate it at the
        # same boundary as active lifecycle operations before removing it, so a
        # malformed endpoint cannot be silently discarded.
        _read_instance(paths)
        _require_deadline(deadline)
        record.unlink()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise RuntimeErrorBase("Nyx stale instance record cannot be removed") from error


def _peer_uid(connection: socket.socket) -> int | None:
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        return int.from_bytes(raw[4:8], "little")
    except (AttributeError, OSError):
        return None


def _send_control(
    instance: Instance,
    command: str,
    *,
    timeout: float = RUNTIME_CONTROL_TIMEOUT,
    deadline: float | None = None,
    deadline_ns: int | None = None,
) -> dict[str, Any]:
    """Send one authenticated command, checking the peer before its capability."""

    def remaining() -> float:
        value = timeout if deadline is None else min(timeout, deadline - time.monotonic())
        if value <= 0:
            raise _ControlTimeoutError("control deadline expired")
        return value

    connection: socket.socket | None = None
    try:
        family, address = _parse_control_endpoint(instance.control)
        connection = socket.socket(family, socket.SOCK_STREAM)
        connection.settimeout(remaining())
        connection.connect(address)
        if family == getattr(socket, "AF_UNIX", None) and _peer_uid(connection) != os.getuid():
            raise _ControlIdentityError("control peer identity did not match")
        connection.settimeout(remaining())
        payload = {"version": 1, "capability": instance.capability, "command": command}
        if deadline_ns is not None:
            payload["deadline_ns"] = deadline_ns
        connection.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        connection.settimeout(remaining())
        data = connection.recv(8192)
        if not data:
            raise _ControlUnavailableError("control did not respond")
        response = json.loads(data.splitlines()[0])
        if not isinstance(response, dict):
            raise _ControlUnavailableError("control response was malformed")
        if response.get("instance_id") != instance.instance_id:
            raise _ControlIdentityError("control response identity did not match")
        return response
    except _ControlTimeoutError:
        raise
    except _ControlIdentityError:
        raise
    except (TimeoutError, socket.timeout) as error:
        raise _ControlTimeoutError("control timed out") from error
    except (OSError, IndexError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise _ControlUnavailableError("control is unavailable") from error
    finally:
        if connection is not None:
            connection.close()


@dataclass(frozen=True)
class RuntimeObservation:
    """A coherent, read-only view of this account's runtime."""

    status: str
    paths: state.StatePaths | None = None
    url: str | None = None
    diagnostic: str | None = None

    @property
    def state(self) -> str:
        return self.status


def _runtime_observation(
    status: str,
    paths: state.StatePaths | None,
    diagnostic: str | None = None,
    *,
    url: str | None = None,
) -> RuntimeObservation:
    return RuntimeObservation(status=status, paths=paths, url=url, diagnostic=diagnostic)


def _runtime_unknown(paths: state.StatePaths | None, diagnostic: str) -> RuntimeObservation:
    return _runtime_observation("unknown", paths, diagnostic)


class _ObservedRuntimeTree:
    """Held existing ancestry for one read-only runtime observation."""

    def __init__(self, paths: state.StatePaths) -> None:
        self.paths = paths
        self.home: NativeDirectory | None = None
        self.state_directory: NativeDirectory | None = None
        self.runtime_directory: NativeDirectory | None = None

    @staticmethod
    def _open_managed_child(
        parent: NativeDirectory,
        name: str,
        path: Path,
    ) -> NativeDirectory | None:
        try:
            before = parent.stat_child(name)
        except FileNotFoundError:
            return None
        if state._is_reparse_or_link(path, before) or not stat.S_ISDIR(before.st_mode):
            raise state.AccountHomeError("Nyx state directory is not an ordinary directory")
        child = parent.open_child(name, path)
        try:
            after = child.stat_self()
            if state._is_reparse_or_link(path, after) or not stat.S_ISDIR(after.st_mode):
                raise state.AccountHomeError(
                    "Nyx state directory is not an ordinary directory"
                )
            if state._identity(before) != state._identity(after):
                raise state.AccountHomeError("Nyx state directory changed during admission")
            return child
        except BaseException:
            child.close()
            raise

    def open(self) -> str:
        self.home = NativeDirectory.open(self.paths.account_home, follow_links=True)
        self.state_directory = self._open_managed_child(
            self.home,
            self.paths.state_directory.name,
            self.paths.state_directory,
        )
        if self.state_directory is None:
            return "absent"
        self.runtime_directory = self._open_managed_child(
            self.state_directory,
            self.paths.runtime_directory.name,
            self.paths.runtime_directory,
        )
        return "absent" if self.runtime_directory is None else "admitted"

    def is_current(self) -> bool:
        if self.home is None:
            return False
        if self.state_directory is None:
            try:
                self.home.stat_child(self.paths.state_directory.name)
            except FileNotFoundError:
                return True
            except OSError:
                return False
            return False
        if not self.home.child_is_same(
            self.paths.state_directory.name,
            self.state_directory,
        ):
            return False
        if self.runtime_directory is None:
            try:
                self.state_directory.stat_child(self.paths.runtime_directory.name)
            except FileNotFoundError:
                return True
            except OSError:
                return False
            return False
        return self.state_directory.child_is_same(
            self.paths.runtime_directory.name,
            self.runtime_directory,
        )

    def close(self) -> None:
        for directory in (
            self.runtime_directory,
            self.state_directory,
            self.home,
        ):
            if directory is not None:
                directory.close()
        self.runtime_directory = None
        self.state_directory = None
        self.home = None


def _observe_runtime_with_paths(
    paths: state.StatePaths,
    directory: NativeDirectory,
) -> RuntimeObservation:
    operation_path = paths.runtime_directory / "operation.lock"
    lease_path = paths.runtime_directory / "lease.lock"
    record_path = _record_path(paths)

    try:
        with directory.scandir() as scanned:
            entries = tuple(scanned)
    except OSError:
        return _runtime_unknown(paths, RUNTIME_STATE_UNAVAILABLE)
    for entry in entries:
        if entry.name not in {"operation.lock", "lease.lock", "instance.json"}:
            return _runtime_unknown(paths, RUNTIME_STATE_UNAVAILABLE)
        try:
            details = entry.stat(follow_symlinks=False)
        except OSError:
            return _runtime_unknown(paths, RUNTIME_STATE_UNAVAILABLE)
        if not _safe_lock_metadata(details):
            return _runtime_unknown(paths, RUNTIME_STATE_UNAVAILABLE)
    try:
        layout_initial = _directory_stamp(paths.runtime_directory, directory=directory)
    except (OSError, RuntimeErrorBase):
        return _runtime_unknown(paths, RUNTIME_STATE_UNAVAILABLE)

    operation = _ExistingLock(operation_path, directory=directory)
    operation_state = operation.acquire()
    if operation_state == "held":
        return _runtime_unknown(paths, RUNTIME_OPERATION_IN_PROGRESS)
    if operation_state in {"unsafe", "changed"}:
        return _runtime_unknown(paths, RUNTIME_STATE_UNAVAILABLE)

    lease: _ExistingLock | None = None
    try:
        operation_initial = _read_metadata(operation_path, directory=directory)
        lease_initial = _read_metadata(lease_path, directory=directory)
        record_initial = _record_snapshot(record_path, directory=directory)
        lease = _ExistingLock(lease_path, directory=directory)
        lease_state = lease.acquire()
        if lease_state in {"unsafe", "changed"}:
            return _runtime_unknown(paths, RUNTIME_STATE_UNAVAILABLE)
        if lease_state == "held":
            # The daemon owns the lease. Its record must be valid before control.
            if not record_initial.exists:
                return _runtime_unknown(paths, RUNTIME_STATE_CHANGED)
            try:
                instance = _read_instance(paths, directory=directory)
            except UnhealthyInstanceError:
                return _runtime_unknown(paths, RUNTIME_STATE_UNAVAILABLE)
            deadline = time.monotonic() + RUNTIME_CONTROL_TIMEOUT
            try:
                response = _send_control(instance, "status", deadline=deadline)
            except _ControlTimeoutError:
                return _runtime_unknown(paths, RUNTIME_CONTROL_TIMED_OUT)
            except _ControlIdentityError:
                return _runtime_unknown(paths, RUNTIME_CONTROL_IDENTITY_MISMATCH)
            except _ControlUnavailableError:
                return _runtime_unknown(paths, RUNTIME_CONTROL_UNAVAILABLE)
            if response.get("status") == "unhealthy":
                return _runtime_unknown(paths, RUNTIME_UNHEALTHY)
            if response.get("status") != "ready" or response.get("url") != URL:
                return _runtime_unknown(paths, RUNTIME_UNHEALTHY)
            try:
                _require_deadline(deadline)
                record_stable = _record_unchanged(
                    record_path,
                    record_initial,
                    directory=directory,
                )
                _require_deadline(deadline)
                lease_stable = _metadata_unchanged(
                    lease_path,
                    lease_initial,
                    directory=directory,
                )
                _require_deadline(deadline)
                operation_stable = _metadata_unchanged(
                    operation_path,
                    operation_initial,
                    directory=directory,
                )
                _require_deadline(deadline)
                layout_stable = _directory_unchanged(
                    paths.runtime_directory,
                    layout_initial,
                    directory=directory,
                )
                _require_deadline(deadline)
            except _ControlTimeoutError:
                return _runtime_unknown(paths, RUNTIME_CONTROL_TIMED_OUT)
            if not record_stable or not lease_stable or not operation_stable or not layout_stable:
                return _runtime_unknown(paths, RUNTIME_STATE_CHANGED)
            _require_deadline(deadline)
            lease_probe = _ExistingLock(lease_path, directory=directory)
            lease_probe_state = lease_probe.acquire()
            lease_probe.close()
            if lease_probe_state != "held":
                return _runtime_unknown(paths, RUNTIME_STATE_CHANGED)
            return _runtime_observation("running", paths, url=URL)

        # There is no held lease: a valid record is stale and remains untouched.
        if lease_state == "absent":
            if record_initial.exists:
                try:
                    _read_instance(paths, directory=directory)
                except UnhealthyInstanceError:
                    return _runtime_unknown(paths, RUNTIME_STATE_UNAVAILABLE)
            if not _metadata_unchanged(
                operation_path,
                operation_initial,
                directory=directory,
            ):
                return _runtime_unknown(paths, RUNTIME_STATE_CHANGED)
            if not _metadata_unchanged(
                lease_path,
                lease_initial,
                directory=directory,
            ):
                return _runtime_unknown(paths, RUNTIME_STATE_CHANGED)
            if not _record_unchanged(
                record_path,
                record_initial,
                directory=directory,
            ):
                return _runtime_unknown(paths, RUNTIME_STATE_CHANGED)
            if not _directory_unchanged(
                paths.runtime_directory,
                layout_initial,
                directory=directory,
            ):
                return _runtime_unknown(paths, RUNTIME_STATE_CHANGED)
            return _runtime_observation("not_running", paths)

        # An existing lease descriptor was successfully acquired, so it is free.
        if record_initial.exists:
            try:
                _read_instance(paths, directory=directory)
            except UnhealthyInstanceError:
                return _runtime_unknown(paths, RUNTIME_STATE_UNAVAILABLE)
        if not _metadata_unchanged(
            operation_path,
            operation_initial,
            directory=directory,
        ):
            return _runtime_unknown(paths, RUNTIME_STATE_CHANGED)
        if not _metadata_unchanged(
            lease_path,
            lease_initial,
            directory=directory,
        ):
            return _runtime_unknown(paths, RUNTIME_STATE_CHANGED)
        if not _record_unchanged(
            record_path,
            record_initial,
            directory=directory,
        ):
            return _runtime_unknown(paths, RUNTIME_STATE_CHANGED)
        if not _directory_unchanged(
            paths.runtime_directory,
            layout_initial,
            directory=directory,
        ):
            return _runtime_unknown(paths, RUNTIME_STATE_CHANGED)
        return _runtime_observation("not_running", paths)
    finally:
        if lease is not None:
            lease.close()
        operation.close()


def observe_runtime() -> RuntimeObservation:
    """Observe runtime state without creating, changing, or cleaning up files."""

    paths: state.StatePaths | None = None
    tree: _ObservedRuntimeTree | None = None
    try:
        paths = state._fixed_state_paths()
        tree = _ObservedRuntimeTree(paths)
        admission = tree.open()
        if admission == "absent":
            if tree.is_current():
                return _runtime_observation("not_running", paths)
            return _runtime_unknown(paths, RUNTIME_STATE_CHANGED)
        assert tree.runtime_directory is not None
        result = _observe_runtime_with_paths(paths, tree.runtime_directory)
        if not tree.is_current():
            return _runtime_unknown(paths, RUNTIME_STATE_CHANGED)
        return result
    except (OSError, state.StateError, RuntimeErrorBase):
        return _runtime_unknown(paths, RUNTIME_STATE_UNAVAILABLE)
    finally:
        if tree is not None:
            tree.close()


observe_runtime_paths = observe_runtime


class _Daemon:
    def __init__(self, lease_fd: int, deadline_ns: int, *, ack_fd: int | None = None) -> None:
        self.lease_fd = lease_fd
        self.deadline_ns = deadline_ns
        self.ack_fd = ack_fd
        if time.monotonic_ns() >= deadline_ns:
            raise StartupError("Nyx startup timed out")
        self.paths = _paths(create=True, deadline_ns=deadline_ns)
        self.stop_requested = threading.Event()
        self.shutdown_lock = threading.Lock()
        self.shutdown_done = threading.Event()
        self.server: TrackerServer | None = None
        self.control: socket.socket | None = None
        self.instance: Instance | None = None
        self.workers = CatalogWorkerManager()
        self.control_thread: threading.Thread | None = None
        self.http_thread: threading.Thread | None = None
        self.shutdown_result: str = "running"
        self.catalog_admitted = False
        self.published_record: _Metadata | None = None

    def _deadline(self) -> float:
        return self.deadline_ns / 1_000_000_000

    def _provider(self) -> Catalog:
        if not self.catalog_admitted:
            raise CatalogError("producer_unavailable")
        try:
            return parse_catalog(self.workers.fetch_catalog())
        except WorkerError as error:
            raise CatalogError(error.code) from None
        except (ProtocolError, ValueError, TypeError) as error:
            raise CatalogError("producer_protocol_error") from error

    def _bind_control(self) -> socket.socket:
        control = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        control.settimeout(0.2)
        try:
            control.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            control.bind(("127.0.0.1", 0))
            control.listen(16)
        except OSError:
            control.close()
            raise StartupError("Nyx control endpoint is already occupied") from None
        return control

    def _static_ready(self) -> bool:
        connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=0.2)
        try:
            connection.request("GET", "/", headers={"Host": f"127.0.0.1:{PORT}"})
            response = connection.getresponse()
            response.read(64 * 1024)
            return response.status == 200 and response.getheader("Content-Type") == "text/html; charset=utf-8"
        except (OSError, http.client.HTTPException):
            return False
        finally:
            connection.close()

    def _serve_control(self) -> None:
        assert self.control is not None
        # The authenticated control endpoint is also the retry path when
        # cleanup outlives its shared deadline.  Keep accepting requests until
        # terminal cleanup closes the listener; stop_requested only marks that
        # cleanup has begun and must not make the endpoint disappear while the
        # daemon still owns the lease and record.
        while self.shutdown_result != "stopped":
            try:
                connection, _ = self.control.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                try:
                    connection.settimeout(1.0)
                    data = connection.recv(8192)
                    request = json.loads(data.splitlines()[0])
                    if (
                        not isinstance(request, dict)
                        or request.get("version") != 1
                        or request.get("capability") != (self.instance.capability if self.instance else None)
                    ):
                        continue
                    command = request.get("command")
                    if command == "status":
                        status = (
                            "unhealthy"
                            if self.stop_requested.is_set() or self.shutdown_result == "timeout"
                            else "ready"
                        )
                        response = {"status": status, "instance_id": self.instance.instance_id, "url": URL}
                    elif command == "stop":
                        response = {"status": "stopping", "instance_id": self.instance.instance_id, "url": URL}
                        connection.sendall((json.dumps(response) + "\n").encode())
                        requested_deadline = request.get("deadline_ns")
                        if type(requested_deadline) is not int:
                            requested_deadline = int((time.monotonic() + SHUTDOWN_TIMEOUT) * 1_000_000_000)
                        threading.Thread(
                            target=self.shutdown,
                            args=(requested_deadline / 1_000_000_000,),
                            daemon=True,
                        ).start()
                        continue
                    else:
                        continue
                    connection.sendall((json.dumps(response) + "\n").encode())
                except (OSError, IndexError, KeyError, TypeError, json.JSONDecodeError):
                    continue

    def start(self) -> None:
        if time.monotonic() >= self._deadline():
            raise StartupError("Nyx startup timed out")
        try:
            self.startup_failure_code = 23
            state.load_configuration(self.paths)
            _require_deadline(self._deadline())
            self.startup_failure_code = 24
            self.server = create_server(provider=self._provider, port=PORT)
            self.http_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.http_thread.start()
            self.startup_failure_code = 25
            while time.monotonic() < self._deadline() and not self._static_ready():
                time.sleep(0.01)
            if time.monotonic() >= self._deadline():
                raise StartupError("Nyx static server did not become ready")
            self.startup_failure_code = 26
            self.control = self._bind_control()
            self.instance = Instance(
                instance_id=secrets.token_hex(16),
                url=URL,
                capability=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("="),
                control=_control_endpoint(self.control.getsockname()[1]),
            )
            self.control_thread = threading.Thread(target=self._serve_control, daemon=True)
            self.control_thread.start()
            self.startup_failure_code = 27
            try:
                response = _send_control(self.instance, "status", deadline=self._deadline())
            except UnhealthyInstanceError as error:
                raise StartupError("Nyx control endpoint did not become ready") from error
            if response.get("status") != "ready" or response.get("url") != URL:
                raise StartupError("Nyx control endpoint did not become ready")
            self.startup_failure_code = 28
            self.published_record = _write_instance(
                self.paths,
                self.instance,
                deadline=self._deadline(),
            )
            self.catalog_admitted = True
            self.startup_failure_code = 29
            self.http_thread.join()
        except OSError as error:
            if self.startup_failure_code == 24 and error.errno in (errno.EADDRINUSE, errno.EACCES):
                raise PortConflictError("Nyx fixed port 8765 is unavailable") from None
            raise StartupError("Nyx server could not start") from error
        finally:
            if not self.stop_requested.is_set():
                self._cleanup_start_failure()

    def _cleanup_start_failure(self) -> None:
        # Publish retention before the asynchronous cleanup can finish.  The
        # cleanup thread is the sole writer of the terminal state, avoiding a
        # race that could overwrite ``stopped`` with ``timeout`` forever.
        self.shutdown_result = "timeout"
        cleanup = threading.Thread(target=self._finish_start_failure_cleanup, daemon=True)
        cleanup.start()
        cleanup.join(timeout=max(0.0, self._deadline() - time.monotonic()))

    def _finish_start_failure_cleanup(self) -> None:
        self.catalog_admitted = False
        try:
            try:
                self.workers.close_admission()
            except Exception:
                pass
            if self.server is not None:
                for cleanup in (
                    self.server.shutdown,
                    self.server.close_active_connections,
                    self.server.server_close,
                ):
                    try:
                        cleanup()
                    except Exception:
                        pass
            try:
                if self.published_record is not None and _record_unchanged(
                    _record_path(self.paths), self.published_record
                ):
                    _record_path(self.paths).unlink()
            except OSError:
                pass
            if self.control is not None:
                try:
                    self.control.close()
                except OSError:
                    pass
        finally:
            self.shutdown_result = "stopped"

    def shutdown(self, deadline: float | None = None) -> str:
        try:
            with self.shutdown_lock:
                if self.shutdown_result not in {"running", "timeout"}:
                    return self.shutdown_result
                deadline = time.monotonic() + SHUTDOWN_TIMEOUT if deadline is None else deadline
                if time.monotonic() >= deadline:
                    self.shutdown_result = "timeout"
                    return self.shutdown_result
                self.stop_requested.set()
                self.catalog_admitted = False
                self.workers.close_admission()
                if self.server is not None:
                    if time.monotonic() >= deadline:
                        self.shutdown_result = "timeout"
                        return self.shutdown_result
                    self.server.shutdown()
                    if time.monotonic() >= deadline:
                        self.shutdown_result = "timeout"
                        return self.shutdown_result
                    self.server.close_active_connections()
                    if time.monotonic() >= deadline:
                        self.shutdown_result = "timeout"
                        return self.shutdown_result
                    self.server.server_close()
                if time.monotonic() >= deadline:
                    self.shutdown_result = "timeout"
                    return self.shutdown_result
                workers_ok = self.workers.close(deadline)
                if not workers_ok or time.monotonic() >= deadline:
                    self.shutdown_result = "timeout"
                    return self.shutdown_result
                try:
                    if self.published_record is not None and _record_unchanged(
                        _record_path(self.paths), self.published_record
                    ):
                        _require_deadline(deadline)
                        _record_path(self.paths).unlink()
                    _require_deadline(deadline)
                    if self.control is not None:
                        self.control.close()
                except (OSError, _ControlTimeoutError):
                    self.shutdown_result = "timeout"
                    return self.shutdown_result
                self.shutdown_result = "stopped"
                # Closing the listener and publishing the terminal state must
                # precede joining this thread.  During an incomplete cleanup
                # the thread is the authenticated retry consumer, so joining
                # it before the state transition would consume the entire
                # retry budget waiting for a thread that is required to stay
                # alive until this exact terminal point.
                if self.control_thread is not None:
                    self.control_thread.join(timeout=max(0.0, deadline - time.monotonic()))
                return self.shutdown_result
        finally:
            self.shutdown_done.set()

    def run(self) -> int:
        completed = False
        try:
            self.start()
            completed = True
        except PortConflictError:
            return 30
        except Exception:
            return getattr(self, "startup_failure_code", 22)
        finally:
            if completed and self.stop_requested.is_set():
                self.shutdown_done.wait()
            if self.shutdown_result == "timeout":
                # Ownership stays with this daemon while direct-child cleanup
                # or connection shutdown is unresolved.
                while self.shutdown_result == "timeout":
                    time.sleep(1.0)
            try:
                os.close(self.lease_fd)
            except OSError:
                pass
        return 0


def _daemon_command(deadline_ns: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "nyx.runtime",
        "--deadline-ns",
        str(deadline_ns),
    ]


def _spawn_daemon(
    lease_fd: int,
    deadline_ns: int,
    *,
    ack_fd: int | None = None,
    claim_path: Path | None = None,
) -> subprocess.Popen[bytes]:
    command = _daemon_command(deadline_ns)
    pass_fds = [lease_fd]
    if claim_path is not None:
        command += ["--claim-path", str(claim_path)]
    kwargs: dict[str, Any] = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    if os.name == "nt":  # pragma: no cover - native Windows lane
        claim_handle = NativeClaim.transfer_handle(lease_fd)
        ack_handle = NativeClaim.transfer_handle(ack_fd) if ack_fd is not None else None
        command += ["--daemon-handle", str(claim_handle)]
        if ack_handle is not None:
            command += ["--ack-handle", str(ack_handle)]
        transfer_handles = [claim_handle]
        if ack_handle is not None:
            transfer_handles.append(ack_handle)
        for handle in transfer_handles:
            os.set_handle_inheritable(handle, True)
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {"handle_list": transfer_handles}
        kwargs.update(
            startupinfo=startup,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        try:
            return subprocess.Popen(command, **kwargs)
        finally:
            for handle in transfer_handles:
                os.set_handle_inheritable(handle, False)
    else:
        command += ["--daemon-fd", str(lease_fd)]
        if ack_fd is not None:
            command += ["--ack-fd", str(ack_fd)]
            pass_fds.append(ack_fd)
        kwargs.update(pass_fds=tuple(pass_fds), start_new_session=True)
    return subprocess.Popen(command, **kwargs)


def _wait_for_ack(
    process: subprocess.Popen[bytes], ack_fd: int, deadline: float
) -> bool:
    process._nyx_ack_outcome = "acknowledgement-deadline"
    os.set_blocking(ack_fd, False)
    while time.monotonic() < deadline:
        try:
            data = os.read(ack_fd, 1)
            if data:
                process._nyx_ack_outcome = (
                    "acknowledgement-accepted" if data == b"1" else "acknowledgement-rejected"
                )
                return data == b"1" and process.poll() is None
            process._nyx_ack_outcome = "acknowledgement-eof"
            return False
        except BlockingIOError:
            if process.poll() is not None:
                process._nyx_ack_outcome = "child-exited-before-acknowledgement"
                return False
        time.sleep(0.005)
    return False


def _terminate_and_reap(process: subprocess.Popen[bytes], deadline: float) -> bool:
    """Stop a failed child without granting it a replacement wait budget."""

    if process.poll() is not None:
        return True
    process.terminate()
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return False
    return True


def _release_claim_after_reap(process: subprocess.Popen[bytes], claim: _FileLock) -> None:
    """Retain a parent claim until a resistant failed child is actually reaped."""

    def reap() -> None:
        try:
            process.wait()
        finally:
            claim.close()

    threading.Thread(target=reap, daemon=True).start()


def _ack_received_claim(fd: int, claim_path: Path, ack_fd: int) -> bool:
    valid = NativeClaim.validate_received(fd, claim_path)
    try:
        os.write(ack_fd, b"1" if valid else b"0")
    finally:
        try:
            os.close(ack_fd)
        except OSError:
            pass
    return valid


def setup(
    specification_root: str | os.PathLike[str],
    hidden_stages: Iterable[str] | object = state._OMITTED,
) -> state.Configuration:
    """Persist setup while excluding mutation during any held lifecycle lease."""

    deadline_ns = time.monotonic_ns() + int(STARTUP_TIMEOUT * 1_000_000_000)
    deadline = deadline_ns / 1_000_000_000
    if time.monotonic_ns() >= deadline_ns:
        raise StartupError("Nyx setup timed out")
    root = state.resolve_specification_root(specification_root)
    if hidden_stages is state._OMITTED:
        requested_hidden: tuple[str, ...] | object = state._OMITTED
    else:
        requested_hidden = state._validate_hidden_stages(hidden_stages)
    try:
        paths = _paths(create=True, deadline=deadline, deadline_ns=deadline_ns)
    except TimeoutError as error:
        raise StartupError("Nyx setup timed out") from error
    with _operation_lock(paths, deadline=deadline):
        _require_deadline(deadline)
        lease = _lease_lock(paths, timeout=0.0, deadline=deadline)
        if not lease.acquire(blocking=False):
            try:
                current = state.load_configuration(paths)
            except state.StateError as error:
                raise ActiveInstanceError("active Nyx instance has no usable configuration") from error
            _require_deadline(deadline)
            effective_hidden = (
                current.hidden_stages
                if requested_hidden is state._OMITTED
                else requested_hidden
            )
            if current.specification_root != root or current.hidden_stages != effective_hidden:
                raise ActiveInstanceError("stop Nyx before changing its specification root")
            return current
        lease.close()
        if paths.config_file.exists() or paths.config_file.is_symlink():
            current = state.load_configuration(paths)
            effective_hidden = (
                current.hidden_stages
                if requested_hidden is state._OMITTED
                else requested_hidden
            )
        else:
            effective_hidden = () if requested_hidden is state._OMITTED else requested_hidden
        return state._save_configuration(
            paths, root, effective_hidden, deadline=deadline, deadline_ns=deadline_ns
        )


def _wait_ready(paths: state.StatePaths, process: subprocess.Popen[bytes], deadline: float) -> Instance:
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise _startup_failure("Nyx daemon exited before readiness", process, "readiness")
        try:
            instance = _read_instance(paths)
            response = _send_control(
                instance,
                "status",
                timeout=min(0.2, max(0.01, deadline - time.monotonic())),
                deadline=deadline,
            )
            if response.get("status") == "ready" and response.get("url") == URL:
                return instance
        except UnhealthyInstanceError:
            pass
        time.sleep(0.02)
    raise _startup_failure("Nyx startup timed out", process, "readiness-deadline")


def start() -> str:
    """Start or authenticate the current user's fixed-port instance."""
    deadline_ns = time.monotonic_ns() + int(STARTUP_TIMEOUT * 1_000_000_000)
    deadline = deadline_ns / 1_000_000_000
    try:
        paths = _paths(create=True, deadline=deadline, deadline_ns=deadline_ns)
    except TimeoutError as error:
        raise StartupError("Nyx startup timed out") from error
    with _operation_lock(paths, deadline=deadline):
        state.load_configuration(paths)
        _require_deadline(deadline)
        lease = _lease_lock(paths, timeout=0.0, deadline=deadline)
        if not lease.acquire(blocking=False):
            instance = _read_instance(paths)
            response = _send_control(instance, "status", deadline=deadline)
            if response.get("status") != "ready" or response.get("url") != URL:
                raise UnhealthyInstanceError("Nyx instance is unhealthy")
            return URL
        release_deferred = False
        try:
            _remove_stale_instance(paths, deadline=deadline)
            _require_deadline(deadline)
            ack_read, ack_write = os.pipe()
            process: subprocess.Popen[bytes] | None = None
            try:
                _require_deadline(deadline)
                process = _spawn_daemon(
                    lease.fd,
                    deadline_ns,
                    ack_fd=ack_write,
                    claim_path=paths.runtime_directory / "lease.lock",
                )
                os.close(ack_write)
                ack_write = -1
                if not _wait_for_ack(process, ack_read, deadline):
                    if not _terminate_and_reap(process, deadline):
                        _release_claim_after_reap(process, lease)
                        release_deferred = True
                    raise _startup_failure(
                        "Nyx daemon did not validate its inherited claim",
                        process,
                        getattr(process, "_nyx_ack_outcome", "acknowledgement-failed"),
                    )
                # The child now owns the same native object.  Closing this
                # descriptor is the handoff; no path reopen occurs in child.
                lease.close()
                return _wait_ready(paths, process, deadline).url
            except BaseException:
                if process is not None and not release_deferred:
                    if not _terminate_and_reap(process, deadline):
                        _release_claim_after_reap(process, lease)
                        release_deferred = True
                raise
            finally:
                os.close(ack_read)
                if ack_write >= 0:
                    os.close(ack_write)
        except BaseException:
            if not release_deferred:
                lease.close()
            raise


def stop() -> str:
    """Stop an authenticated instance, or report the already-stopped state."""
    deadline_ns = time.monotonic_ns() + int(SHUTDOWN_TIMEOUT * 1_000_000_000)
    deadline = deadline_ns / 1_000_000_000
    try:
        paths = _paths(create=True, deadline=deadline, deadline_ns=deadline_ns)
    except TimeoutError as error:
        raise ShutdownTimeoutError("Nyx shutdown timed out; ownership was retained") from error
    with _operation_lock(paths, deadline=deadline):
        _require_deadline(deadline)
        lease = _lease_lock(paths, timeout=0.0, deadline=deadline)
        if lease.acquire(blocking=False):
            lease.close()
            _remove_stale_instance(paths, deadline=deadline)
            return "stopped"
        instance = _read_instance(paths)
        _send_control(
            instance,
            "stop",
            deadline=deadline,
            deadline_ns=deadline_ns,
        )
        while time.monotonic() < deadline:
            probe = _lease_lock(paths, timeout=0.0, deadline=deadline)
            if probe.acquire(blocking=False):
                probe.close()
                _remove_stale_instance(paths, deadline=deadline)
                return "stopped"
            probe.close()
            time.sleep(0.03)
        raise ShutdownTimeoutError("Nyx shutdown timed out; ownership was retained")


def _daemon_entry(
    fd: int,
    deadline_ns: int,
    *,
    ack_fd: int | None = None,
    claim_path: Path | None = None,
) -> int:
    try:
        valid = ack_fd is None or claim_path is None or _ack_received_claim(fd, claim_path, ack_fd)
    except OSError:
        os.close(fd)
        return 21
    if not valid:
        os.close(fd)
        return 20
    try:
        daemon = _Daemon(fd, deadline_ns, ack_fd=ack_fd)
    except Exception:
        os.close(fd)
        return 22
    return daemon.run()


def _receive_daemon_handles(claim_handle: int, ack_handle: int | None) -> tuple[int, int | None]:
    claim_fd = NativeClaim.receive_handle(claim_handle)
    ack_fd = (
        NativeClaim.receive_handle(ack_handle, write_only=True)
        if ack_handle is not None
        else None
    )
    return claim_fd, ack_fd


if __name__ == "__main__":
    if "--daemon-fd" not in sys.argv and "--daemon-handle" not in sys.argv:
        raise SystemExit("internal lifecycle entry point")
    try:
        deadline = int(sys.argv[sys.argv.index("--deadline-ns") + 1])
        if "--daemon-handle" in sys.argv:
            fd, ack = _receive_daemon_handles(
                int(sys.argv[sys.argv.index("--daemon-handle") + 1]),
                int(sys.argv[sys.argv.index("--ack-handle") + 1])
                if "--ack-handle" in sys.argv
                else None,
            )
        else:
            fd = int(sys.argv[sys.argv.index("--daemon-fd") + 1])
            ack = int(sys.argv[sys.argv.index("--ack-fd") + 1]) if "--ack-fd" in sys.argv else None
        claim = Path(sys.argv[sys.argv.index("--claim-path") + 1]) if "--claim-path" in sys.argv else None
    except (ValueError, IndexError):
        raise SystemExit("invalid lifecycle arguments") from None
    raise SystemExit(_daemon_entry(fd, deadline, ack_fd=ack, claim_path=claim))


__all__ = [
    "PORT",
    "RUNTIME_CONTROL_IDENTITY_MISMATCH",
    "RUNTIME_CONTROL_TIMED_OUT",
    "RUNTIME_CONTROL_UNAVAILABLE",
    "RUNTIME_OPERATION_IN_PROGRESS",
    "RUNTIME_STATE_CHANGED",
    "RUNTIME_STATE_UNAVAILABLE",
    "RUNTIME_UNHEALTHY",
    "RUNTIME_CONTROL_TIMEOUT",
    "SHUTDOWN_TIMEOUT",
    "STARTUP_TIMEOUT",
    "URL",
    "ActiveInstanceError",
    "Instance",
    "OperationBusyError",
    "PortConflictError",
    "RuntimeErrorBase",
    "RuntimeObservation",
    "ShutdownTimeoutError",
    "StartupError",
    "UnhealthyInstanceError",
    "observe_runtime",
    "observe_runtime_paths",
    "setup",
    "start",
    "stop",
]
