"""Per-user Nyx lifecycle ownership and authenticated local control."""

from __future__ import annotations

import base64
import errno
import fcntl
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from . import state
from .models import Catalog, ProtocolError, parse_catalog
from .server import CatalogError, TrackerServer, create_server
from .worker import CatalogWorkerManager, WorkerError

PORT = 8765
URL = f"http://127.0.0.1:{PORT}/"
SCHEMA_VERSION = 1
LOCK_TIMEOUT = 5.0
STARTUP_TIMEOUT = 5.0
SHUTDOWN_TIMEOUT = 5.0


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
    def __init__(self, path: Path, *, timeout: float = LOCK_TIMEOUT) -> None:
        self.path = path
        self.timeout = timeout
        self.fd: int | None = None

    def acquire(self, *, blocking: bool = True) -> bool:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
            details = os.fstat(fd)
            uid = os.getuid()
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != uid
                or stat.S_IMODE(details.st_mode) != 0o600
            ):
                os.close(fd)
                raise RuntimeErrorBase("Nyx lock has unsafe ownership or mode")
        except OSError as error:
            raise RuntimeErrorBase("Nyx lock is unavailable") from error
        operation = fcntl.LOCK_EX | (fcntl.LOCK_NB if not blocking else 0)
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, operation)
                    self.fd = fd
                    return True
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN):
                        raise RuntimeErrorBase("Nyx lock cannot be acquired") from error
                    if not blocking or time.monotonic() >= deadline:
                        os.close(fd)
                        return False
                    time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        except BaseException:
            os.close(fd)
            raise

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> Self:
        if not self.acquire():
            raise OperationBusyError("another Nyx operation is in progress")
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _paths(*, create: bool = True) -> state.StatePaths:
    return state.state_paths(create=create)


def _operation_lock(paths: state.StatePaths, *, timeout: float = LOCK_TIMEOUT) -> _FileLock:
    return _FileLock(paths.runtime_directory / "operation.lock", timeout=timeout)


def _lease_lock(paths: state.StatePaths, *, timeout: float = LOCK_TIMEOUT) -> _FileLock:
    return _FileLock(paths.runtime_directory / "lease.lock", timeout=timeout)


def _control_name() -> str:
    return f"\x00nyx-control-{os.getuid()}"


def _record_path(paths: state.StatePaths) -> Path:
    return paths.runtime_directory / "instance.json"


def _read_instance(paths: state.StatePaths) -> Instance:
    path = _record_path(paths)
    try:
        details = path.lstat()
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise UnhealthyInstanceError("Nyx instance record is unsafe")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != SCHEMA_VERSION
            or set(payload) != {"schema_version", "instance_id", "url", "capability", "control"}
            or payload.get("url") != URL
            or payload.get("control") != _control_name()
            or not all(isinstance(payload.get(key), str) and payload[key] for key in ("instance_id", "capability"))
        ):
            raise UnhealthyInstanceError("Nyx instance record is invalid")
        return Instance(
            instance_id=payload["instance_id"],
            url=payload["url"],
            capability=payload["capability"],
            control=payload["control"],
        )
    except FileNotFoundError as error:
        raise UnhealthyInstanceError("Nyx instance is not ready") from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UnhealthyInstanceError("Nyx instance record is unavailable") from error


def _write_instance(paths: state.StatePaths, instance: Instance) -> None:
    data = (json.dumps(instance.as_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode()
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".instance.json.", dir=paths.runtime_directory)
        os.fchmod(fd, 0o600)
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        os.fsync(fd)
        os.close(fd)
        os.replace(temporary, _record_path(paths))
        temporary = None
    except OSError as error:
        raise StartupError("Nyx could not publish readiness") from error
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _remove_stale_instance(paths: state.StatePaths) -> None:
    try:
        record = _record_path(paths)
        details = record.lstat()
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise UnhealthyInstanceError("Nyx instance record is unsafe")
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


def _send_control(instance: Instance, command: str, *, timeout: float = 1.0) -> dict[str, Any]:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(instance.control)
        payload = {"version": 1, "capability": instance.capability, "command": command}
        connection.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        data = connection.recv(8192)
        if not data:
            raise UnhealthyInstanceError("Nyx control did not respond")
        response = json.loads(data.splitlines()[0])
        if not isinstance(response, dict) or response.get("instance_id") != instance.instance_id:
            raise UnhealthyInstanceError("Nyx control identity did not match")
        return response
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise UnhealthyInstanceError("Nyx instance is unhealthy") from error
    finally:
        connection.close()


class _Daemon:
    def __init__(self, lease_fd: int, deadline_ns: int) -> None:
        self.lease_fd = lease_fd
        self.deadline_ns = deadline_ns
        self.paths = _paths(create=True)
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

    def _deadline(self) -> float:
        return self.deadline_ns / 1_000_000_000

    def _provider(self) -> Catalog:
        try:
            return parse_catalog(self.workers.fetch_catalog())
        except WorkerError as error:
            raise CatalogError(error.code) from None
        except (ProtocolError, ValueError, TypeError) as error:
            raise CatalogError("producer_protocol_error") from error

    def _bind_control(self) -> socket.socket:
        control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        control.settimeout(0.2)
        try:
            control.bind(_control_name())
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
        while not self.stop_requested.is_set() or self.shutdown_result == "timeout":
            try:
                connection, _ = self.control.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                if _peer_uid(connection) != os.getuid():
                    continue
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
                        status = "unhealthy" if self.shutdown_result == "timeout" else "ready"
                        response = {"status": status, "instance_id": self.instance.instance_id, "url": URL}
                    elif command == "stop":
                        response = {"status": "stopping", "instance_id": self.instance.instance_id, "url": URL}
                        connection.sendall((json.dumps(response) + "\n").encode())
                        threading.Thread(target=self.shutdown, daemon=True).start()
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
            state.load_configuration(self.paths)
            self.server = create_server(provider=self._provider, port=PORT)
            self.http_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.http_thread.start()
            while time.monotonic() < self._deadline() and not self._static_ready():
                time.sleep(0.01)
            if time.monotonic() >= self._deadline():
                raise StartupError("Nyx static server did not become ready")
            self.control = self._bind_control()
            self.instance = Instance(
                instance_id=secrets.token_hex(16),
                url=URL,
                capability=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("="),
                control=_control_name(),
            )
            if time.monotonic() >= self._deadline():
                raise StartupError("Nyx startup timed out")
            _write_instance(self.paths, self.instance)
            self.control_thread = threading.Thread(target=self._serve_control, daemon=True)
            self.control_thread.start()
            self.http_thread.join()
        except OSError as error:
            if error.errno in (errno.EADDRINUSE, errno.EACCES):
                raise PortConflictError("Nyx fixed port 8765 is unavailable") from None
            raise StartupError("Nyx server could not start") from error
        finally:
            if not self.stop_requested.is_set():
                self._cleanup_start_failure()

    def _cleanup_start_failure(self) -> None:
        if self.control is not None:
            try:
                self.control.close()
            except OSError:
                pass
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        try:
            if self.instance is not None:
                _record_path(self.paths).unlink()
        except OSError:
            pass

    def shutdown(self) -> str:
        try:
            with self.shutdown_lock:
                if self.shutdown_result not in {"running", "timeout"}:
                    return self.shutdown_result
                deadline = time.monotonic() + SHUTDOWN_TIMEOUT
                self.stop_requested.set()
                self.workers.close_admission()
                if self.server is not None:
                    self.server.shutdown()
                    self.server.close_active_connections()
                    self.server.server_close()
                workers_ok = self.workers.close(deadline)
                if self.control_thread is not None:
                    self.control_thread.join(timeout=max(0.0, deadline - time.monotonic()))
                if not workers_ok or time.monotonic() >= deadline:
                    self.shutdown_result = "timeout"
                    return self.shutdown_result
                try:
                    if self.control is not None:
                        self.control.close()
                    _record_path(self.paths).unlink()
                except OSError:
                    self.shutdown_result = "timeout"
                    return self.shutdown_result
                self.shutdown_result = "stopped"
                return self.shutdown_result
        finally:
            self.shutdown_done.set()

    def run(self) -> int:
        completed = False
        try:
            self.start()
            completed = True
        except StartupError:
            self._cleanup_start_failure()
            return 1
        finally:
            if completed and self.stop_requested.is_set():
                self.shutdown_done.wait()
            if completed and self.shutdown_result == "timeout":
                # Ownership stays with this daemon while direct-child cleanup
                # or connection shutdown is unresolved.
                while self.shutdown_result == "timeout":
                    time.sleep(1.0)
            else:
                try:
                    os.close(self.lease_fd)
                except OSError:
                    pass
        return 0


def _spawn_daemon(lease_fd: int, deadline_ns: int) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "nyx.runtime", "--daemon-fd", str(lease_fd), "--deadline-ns", str(deadline_ns)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        pass_fds=(lease_fd,),
        start_new_session=True,
    )


def setup(specification_root: str | os.PathLike[str]) -> state.Configuration:
    """Persist setup while excluding mutation during any held lifecycle lease."""

    paths = _paths(create=True)
    with _operation_lock(paths):
        root = state.resolve_specification_root(specification_root)
        lease = _lease_lock(paths, timeout=0.0)
        if not lease.acquire(blocking=False):
            try:
                current = state.load_configuration(paths)
            except state.StateError as error:
                raise ActiveInstanceError("active Nyx instance has no usable configuration") from error
            if current.specification_root != root:
                raise ActiveInstanceError("stop Nyx before changing its specification root")
            return current
        lease.close()
        return state.save_configuration(root)


def _wait_ready(paths: state.StatePaths, process: subprocess.Popen[bytes], deadline: float) -> Instance:
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise StartupError("Nyx daemon exited before readiness")
        try:
            instance = _read_instance(paths)
            response = _send_control(instance, "status", timeout=min(0.2, max(0.01, deadline - time.monotonic())))
            if response.get("status") == "ready" and response.get("url") == URL:
                return instance
        except UnhealthyInstanceError:
            pass
        time.sleep(0.02)
    raise StartupError("Nyx startup timed out")


def start() -> str:
    """Start or authenticate the current user's fixed-port instance."""

    paths = _paths(create=True)
    with _operation_lock(paths):
        state.load_configuration(paths)
        lease = _lease_lock(paths, timeout=0.0)
        if not lease.acquire(blocking=False):
            instance = _read_instance(paths)
            response = _send_control(instance, "status")
            if response.get("status") != "ready" or response.get("url") != URL:
                raise UnhealthyInstanceError("Nyx instance is unhealthy")
            return URL
        try:
            _remove_stale_instance(paths)
            deadline = time.monotonic() + STARTUP_TIMEOUT
            process = _spawn_daemon(lease.fd, int(deadline * 1_000_000_000))
            lease.close()
            instance = _wait_ready(paths, process, deadline)
            return instance.url
        except BaseException:
            lease.close()
            raise


def stop() -> str:
    """Stop an authenticated instance, or report the already-stopped state."""

    paths = _paths(create=True)
    with _operation_lock(paths):
        lease = _lease_lock(paths, timeout=0.0)
        if lease.acquire(blocking=False):
            lease.close()
            _remove_stale_instance(paths)
            return "stopped"
        instance = _read_instance(paths)
        _send_control(instance, "stop")
        deadline = time.monotonic() + SHUTDOWN_TIMEOUT
        while time.monotonic() < deadline:
            probe = _lease_lock(paths, timeout=0.0)
            if probe.acquire(blocking=False):
                probe.close()
                _remove_stale_instance(paths)
                return "stopped"
            probe.close()
            time.sleep(0.03)
        raise ShutdownTimeoutError("Nyx shutdown timed out; ownership was retained")


def _daemon_entry(fd: int, deadline_ns: int) -> int:
    return _Daemon(fd, deadline_ns).run()


if __name__ == "__main__":
    if "--daemon-fd" not in sys.argv:
        raise SystemExit("internal lifecycle entry point")
    try:
        fd = int(sys.argv[sys.argv.index("--daemon-fd") + 1])
        deadline = int(sys.argv[sys.argv.index("--deadline-ns") + 1])
    except (ValueError, IndexError):
        raise SystemExit("invalid lifecycle arguments") from None
    raise SystemExit(_daemon_entry(fd, deadline))


__all__ = [
    "PORT",
    "SHUTDOWN_TIMEOUT",
    "STARTUP_TIMEOUT",
    "URL",
    "ActiveInstanceError",
    "Instance",
    "OperationBusyError",
    "PortConflictError",
    "RuntimeErrorBase",
    "ShutdownTimeoutError",
    "StartupError",
    "UnhealthyInstanceError",
    "setup",
    "start",
    "stop",
]
