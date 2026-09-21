"""Behavioral checks for the authenticated process lifecycle."""

from __future__ import annotations

import http.client
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from nyx import _native_claim, runtime, state, worker


def _fixture(root: Path) -> tuple[state.StatePaths, Path, Path]:
    home = root / "home"
    home.mkdir()
    spec = root / "spec"
    spec.mkdir()
    with patch.object(state, "resolve_account_home", return_value=home), patch.object(
        state, "_current_uid", return_value=state._current_uid()
    ):
        state.setup(spec)
        paths = state.state_paths()
    return paths, home, spec


def _transferred_claim_fd(path: Path) -> int:
    """Acquire one native claim and transfer descriptor ownership to the test."""

    claim = runtime.NativeClaim(path)
    assert claim.acquire(blocking=False)
    assert claim.fd is not None
    fd, claim.fd = claim.fd, None
    claim.identity = None
    return fd


def _assert_claim_available(path: Path) -> None:
    claim = runtime.NativeClaim(path)
    try:
        assert claim.acquire(create=False, blocking=False)
    finally:
        claim.close()


def _wait_for_record(paths: state.StatePaths) -> runtime.Instance:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            return runtime._read_instance(paths)
        except runtime.UnhealthyInstanceError:
            time.sleep(0.02)
    raise AssertionError("daemon did not publish instance record")


def _subprocess_environment(home: Path, site_directory: Path) -> dict[str, str]:
    site_directory.mkdir()
    (site_directory / "sitecustomize.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "from nyx import state\n"
        "state.resolve_account_home = lambda: Path(os.environ['NYX_TEST_HOME'])\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["NYX_TEST_HOME"] = str(home)
    environment["PYTHONPATH"] = str(site_directory) + os.pathsep + environment.get("PYTHONPATH", "")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def test_runtime_tests_collect_without_test_owned_posix_primitives():
    """Model Windows' absent fcntl/getuid boundary during real pytest collection."""

    with TemporaryDirectory() as temporary:
        site_directory = Path(temporary)
        (site_directory / "sitecustomize.py").write_text(
            "import builtins, os\n"
            "original_import = builtins.__import__\n"
            "def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):\n"
            "    source = '' if globals is None else globals.get('__file__', '')\n"
            "    if name == 'fcntl' and source.endswith('test_runtime.py'):\n"
            "        raise ModuleNotFoundError(\"No module named 'fcntl'\")\n"
            "    return original_import(name, globals, locals, fromlist, level)\n"
            "builtins.__import__ = guarded_import\n"
            "if hasattr(os, 'getuid'):\n"
            "    del os.getuid\n",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(site_directory) + os.pathsep + environment.get(
            "PYTHONPATH", ""
        )
        collected = subprocess.run(
            [sys.executable, "-m", "pytest", __file__, "--collect-only", "-q"],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    assert collected.returncode == 0, collected.stderr
    assert "test_posix_spawn_preserves_detached_descriptor_contract" in collected.stdout
    assert (
        "test_external_launcher_death_before_ack_keeps_inherited_claim_until_child_exit"
        in collected.stdout
    )
    assert "test_launcher_loss_after_spawn_leaves_transferred_lease_until_child_exit" in collected.stdout


def test_windows_share_violation_is_a_qualified_busy_probe():
    details = os.stat(__file__)
    busy = OSError(13, "sharing violation")
    busy.winerror = 32
    with patch.object(Path, "lstat", return_value=details), patch.object(
        _native_claim, "_open_claim", side_effect=busy
    ):
        assert runtime.NativeClaim.probe(Path("claim.lock")) == "held"


def test_windows_share_violation_during_claim_acquisition_is_busy():
    busy = OSError(13, "sharing violation")
    busy.winerror = 32
    with patch.object(_native_claim, "_open_claim", side_effect=busy):
        assert runtime._FileLock(Path("claim.lock"), timeout=0.0).acquire(blocking=False) is False


def test_blocking_windows_share_violation_retries_before_deadline():
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / "claim.lock"
        path.write_bytes(b"\0")
        fd = os.open(path, os.O_RDWR)
        busy = OSError(13, "sharing violation")
        busy.winerror = 32
        claim = runtime.NativeClaim(path)
        with patch.object(_native_claim, "_open_claim", side_effect=[busy, fd]), patch.object(
            _native_claim.time, "sleep"
        ) as sleep:
            assert claim.acquire(blocking=True, deadline=time.monotonic() + 1)
        sleep.assert_called_once()
        claim.close()


def test_received_claim_validation_uses_stable_object_identity():
    received = SimpleNamespace(st_dev=7, st_ino=11, st_mode=stat.S_IFREG | 0o600)
    same_object = SimpleNamespace(st_dev=7, st_ino=11, st_mode=stat.S_IFREG | 0o666)
    replacement = SimpleNamespace(st_dev=7, st_ino=12, st_mode=stat.S_IFREG | 0o600)
    path = Path("claim.lock")
    with patch.object(_native_claim.os, "fstat", return_value=received), patch.object(
        Path, "lstat", return_value=same_object
    ):
        assert runtime.NativeClaim.validate_received(17, path)
    with patch.object(_native_claim.os, "fstat", return_value=received), patch.object(
        Path, "lstat", return_value=replacement
    ):
        assert not runtime.NativeClaim.validate_received(17, path)


@pytest.mark.parametrize(
    ("platform", "changed_field", "expected"),
    [("nt", None, "free"), ("nt", "st_ino", "changed"),
     ("nt", "st_dev", "changed"), ("nt", "st_birthtime_ns", "changed"),
     ("nt", "st_mtime_ns", "changed"), ("nt", "st_size", "changed"),
     ("nt", "st_mode", "unsafe"), ("posix", None, "changed")],
)
def test_claim_probe_compares_portable_metadata_without_losing_change_detection(platform, changed_field, expected):
    fields = dict(st_dev=7, st_ino=11, st_mode=stat.S_IFREG | 0o600,
                  st_size=1, st_mtime_ns=20, st_ctime_ns=30, st_birthtime_ns=30)
    before = SimpleNamespace(**fields)
    fields["st_ctime_ns"] = 40
    if changed_field is not None:
        fields[changed_field] = stat.S_IFDIR | 0o700 if changed_field == "st_mode" else fields[changed_field] + 1
    after = SimpleNamespace(**fields)
    path = Path("claim.lock")
    with patch.object(_native_claim.os, "name", platform), patch.object(
        Path, "lstat", return_value=before
    ), patch.object(_native_claim, "_open_claim", return_value=17), patch.object(
        _native_claim.os, "fstat", return_value=after
    ), patch.object(_native_claim, "_lock_fd", return_value=True) as lock, patch.object(
        _native_claim.os, "close"
    ) as close:
        assert runtime.NativeClaim.probe(path) == expected
    close.assert_called_once_with(17)
    if expected == "free":
        lock.assert_called_once_with(17, blocking=False, deadline=None)
    else:
        lock.assert_not_called()


def test_windows_spawn_transfers_native_handles_and_child_maps_them():
    startup = type("Startup", (), {"lpAttributeList": None})()
    process = object()
    with patch.object(runtime.os, "name", "nt"), patch.object(
        runtime.NativeClaim, "transfer_handle", side_effect=[101, 202]
    ), patch.object(runtime.os, "set_handle_inheritable", create=True) as inheritable, patch.object(
        runtime.subprocess, "STARTUPINFO", return_value=startup, create=True
    ), patch.object(runtime.subprocess, "DETACHED_PROCESS", 8, create=True), patch.object(
        runtime.subprocess, "CREATE_NEW_PROCESS_GROUP", 16, create=True
    ), patch.object(runtime.subprocess, "Popen", return_value=process) as popen:
        assert runtime._spawn_daemon(11, 1234, ack_fd=22, claim_path=Path("claim")) is process

    command, kwargs = popen.call_args.args[0], popen.call_args.kwargs
    assert command[command.index("--daemon-handle") + 1] == "101"
    assert command[command.index("--ack-handle") + 1] == "202"
    assert "--daemon-fd" not in command and "--ack-fd" not in command
    assert startup.lpAttributeList == {"handle_list": [101, 202]}
    assert kwargs["close_fds"] is True
    assert kwargs["creationflags"] == 24
    assert inheritable.call_args_list == [
        ((101, True),),
        ((202, True),),
        ((101, False),),
        ((202, False),),
    ]

    with patch.object(
        runtime.NativeClaim, "receive_handle", side_effect=[31, 42]
    ) as receive:
        assert runtime._receive_daemon_handles(101, 202) == (31, 42)
    assert receive.call_args_list[0].args == (101,)
    assert receive.call_args_list[1].args == (202,)
    assert receive.call_args_list[1].kwargs == {"write_only": True}


def test_daemon_rejects_expired_deadline_before_path_admission():
    with patch.object(runtime, "_paths") as paths, pytest.raises(runtime.StartupError):
        runtime._Daemon(7, time.monotonic_ns() - 1)
    paths.assert_not_called()


def test_daemon_rechecks_shared_deadline_after_configuration_admission():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd = os.open(paths.runtime_directory / "lease.lock", os.O_RDWR)
        original_lstat = state._lstat
        lookup_delayed = False

        def delayed_admission_lookup(path):
            nonlocal lookup_delayed
            if not lookup_delayed:
                lookup_delayed = True
                time.sleep(0.03)
            return original_lstat(path)

        try:
            with patch.object(runtime, "_paths", return_value=paths):
                daemon = runtime._Daemon(lease_fd, time.monotonic_ns() + 10_000_000)
            with patch.object(
                state, "_lstat", side_effect=delayed_admission_lookup
            ), patch.object(runtime, "create_server") as create_server, pytest.raises(
                runtime.UnhealthyInstanceError, match="deadline expired"
            ):
                daemon.start()
            create_server.assert_not_called()
            assert daemon.server is None
        finally:
            os.close(lease_fd)


def test_readiness_publication_rejects_expiry_before_creating_temp_record():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        instance = runtime.Instance("instance", runtime.URL, "capability", "127.0.0.1:1")
        with patch.object(runtime.tempfile, "mkstemp") as mkstemp, pytest.raises(
            runtime.StartupError
        ):
            runtime._write_instance(paths, instance, deadline=time.monotonic() - 1)
        mkstemp.assert_not_called()


def test_tcp_control_does_not_require_unix_socket_or_uid(monkeypatch):
    instance = runtime.Instance("instance", runtime.URL, "secret", "127.0.0.1:43210")
    response = {"instance_id": "instance", "status": "ready", "url": runtime.URL}
    connection = Mock()
    connection.recv.return_value = json.dumps(response).encode() + b"\n"
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)
    monkeypatch.delattr(os, "getuid", raising=False)
    with patch.object(socket, "socket", return_value=connection) as create:
        assert runtime._send_control(instance, "status") == response
    create.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
    connection.connect.assert_called_once_with(("127.0.0.1", 43210))
    assert json.loads(connection.sendall.call_args.args[0]) == {
        "version": 1, "capability": "secret", "command": "status"
    }
    connection.close.assert_called_once()


def test_readiness_publication_does_not_require_fchmod(monkeypatch):
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        instance = runtime.Instance("instance", runtime.URL, "capability", "127.0.0.1:43210")
        monkeypatch.delattr(os, "fchmod", raising=False)
        snapshot = runtime._write_instance(paths, instance, deadline=time.monotonic() + 2)
        assert runtime._read_instance(paths) == instance
        assert runtime._record_unchanged(runtime._record_path(paths), snapshot)
        assert not list(paths.runtime_directory.glob(".instance.json.*"))


@pytest.mark.parametrize(
    ("payload", "exit_code", "expected", "accepted"),
    [(b"1", None, "acknowledgement-accepted", True),
     (b"0", None, "acknowledgement-rejected", False),
     (b"", None, "acknowledgement-eof", False),
     (b"1", 22, "acknowledgement-accepted", False)],
)
def test_acknowledgement_diagnostics_distinguish_wire_outcomes(payload, exit_code, expected, accepted):
    read_fd, write_fd = os.pipe()
    process = SimpleNamespace(poll=lambda: exit_code)
    try:
        if payload:
            os.write(write_fd, payload)
        os.close(write_fd)
        assert runtime._wait_for_ack(process, read_fd, time.monotonic() + 1) is accepted
        assert process._nyx_ack_outcome == expected
    finally:
        os.close(read_fd)


@pytest.mark.parametrize("exited", [False, True])
def test_acknowledgement_diagnostics_distinguish_deadline_and_early_exit(exited):
    read_fd, write_fd = os.pipe()
    process = SimpleNamespace(poll=lambda: 22 if exited else None)
    try:
        deadline = time.monotonic() + (1 if exited else 0)
        assert not runtime._wait_for_ack(process, read_fd, deadline)
        assert process._nyx_ack_outcome == (
            "child-exited-before-acknowledgement" if exited else "acknowledgement-deadline"
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_daemon_entry_classifies_initialization_failure_after_valid_ack():
    with TemporaryDirectory() as temporary:
        claim = Path(temporary) / "lease.lock"
        claim_fd = _transferred_claim_fd(claim)
        read_fd, write_fd = os.pipe()
        try:
            with patch.object(runtime, "_Daemon", side_effect=ValueError("private-detail")):
                assert runtime._daemon_entry(
                    claim_fd, time.monotonic_ns() + 10**9, ack_fd=write_fd, claim_path=claim
                ) == 22
            assert os.read(read_fd, 1) == b"1"
            _assert_claim_available(claim)
        finally:
            os.close(read_fd)


def test_daemon_entry_classifies_broken_ack_pipe_and_closes_claim():
    with TemporaryDirectory() as temporary:
        claim = Path(temporary) / "lease.lock"
        claim_fd = _transferred_claim_fd(claim)
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        with patch.object(runtime, "_Daemon") as daemon:
            assert runtime._daemon_entry(
                claim_fd, time.monotonic_ns() + 10**9, ack_fd=write_fd, claim_path=claim
            ) == 21
        daemon.assert_not_called()
        _assert_claim_available(claim)


def test_startup_diagnostic_notes_preserve_public_message_and_hide_child_details():
    process = SimpleNamespace(poll=lambda: 28)
    error = runtime._startup_failure("Nyx daemon exited before readiness", process, "readiness")
    assert str(error) == "Nyx daemon exited before readiness"
    assert error.__notes__ == [
        "internal startup phase=readiness; outcome=locator-publication-failed; exit=28"
    ]


def test_public_start_reports_sanitized_detached_child_failure():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        paths, home, _ = _fixture(root)
        environment = _subprocess_environment(home, root / "site")
        script = (
            "import errno, runpy; from nyx import server\n"
            "def fail(**kwargs):\n"
            "    raise OSError(errno.EADDRINUSE, 'private-child-detail')\n"
            "server.create_server=fail\n"
            "runpy.run_module('nyx.runtime', run_name='__main__')\n"
        )
        with patch.dict(os.environ, environment), patch.object(
            state, "resolve_account_home", return_value=home
        ), patch.object(
            runtime, "_daemon_command",
            side_effect=lambda deadline: [
                sys.executable, "-c", script, "--deadline-ns", str(deadline)
            ],
        ), pytest.raises(runtime.StartupError) as caught:
            runtime.start()
        assert "outcome=fixed-port-unavailable; exit=30" in caught.value.__notes__[0]
        assert "private-child-detail" not in repr(caught.value.__notes__)
        assert "private-child-detail" not in str(caught.value)
        assert not runtime._record_path(paths).exists()
        _assert_claim_available(paths.runtime_directory / "lease.lock")


def test_daemon_entry_rejects_unvalidated_inherited_object_before_initialization():
    with TemporaryDirectory() as temporary:
        claim = Path(temporary) / "lease.lock"
        claim.write_bytes(b"\0")
        claim_fd = os.open(claim, os.O_RDWR)
        ack_read, ack_write = os.pipe()
        with patch.object(runtime.NativeClaim, "validate_received", return_value=False), patch.object(
            runtime, "_Daemon"
        ) as daemon:
            assert runtime._daemon_entry(
                claim_fd,
                time.monotonic_ns() + 1_000_000_000,
                ack_fd=ack_write,
                claim_path=claim,
            ) == 20
        assert os.read(ack_read, 1) == b"0"
        daemon.assert_not_called()
        os.close(ack_read)


def test_control_is_verified_before_locator_publication_and_catalog_admission():
    events: list[str] = []

    class Server:
        def serve_forever(self):
            return None

        def shutdown(self):
            events.append("http-shutdown")

        def close_active_connections(self):
            events.append("connections-close")

        def server_close(self):
            events.append("http-close")

    class Control:
        def getsockname(self):
            return ("127.0.0.1", 43210)

        def close(self):
            events.append("control-close")

    class Thread:
        def __init__(self, *, target, daemon=True, args=()):
            self.target = target

        def start(self):
            events.append("thread-start")
            if self.target.__name__ == "_finish_start_failure_cleanup":
                self.target()

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return False

    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd = os.open(paths.runtime_directory / "lease.lock", os.O_RDWR)
        try:
            with patch.object(runtime, "_paths", return_value=paths):
                daemon = runtime._Daemon(
                    lease_fd, time.monotonic_ns() + 5_000_000_000
                )
            with patch.object(runtime, "create_server", return_value=Server()), patch.object(
                runtime.threading, "Thread", Thread
            ), patch.object(daemon, "_static_ready", return_value=True), patch.object(
                daemon, "_bind_control", return_value=Control()
            ), patch.object(
                runtime,
                "_send_control",
                side_effect=lambda *args, **kwargs: events.append("control-probe")
                or {"status": "ready", "url": runtime.URL},
            ), patch.object(
                runtime,
                "_write_instance",
                side_effect=lambda *args, **kwargs: events.append("publish")
                or runtime._Metadata(False),
            ):
                daemon.start()
        finally:
            os.close(lease_fd)

    assert events.index("control-probe") < events.index("publish")
    assert daemon.catalog_admitted is False
    assert events[-4:] == [
        "http-shutdown",
        "connections-close",
        "http-close",
        "control-close",
    ]


def test_publication_failure_tears_down_before_releasing_lifetime_claim():
    events: list[str] = []
    paths: state.StatePaths

    def assert_claim_held(event: str) -> None:
        contender = runtime._lease_lock(paths, timeout=0.0)
        try:
            assert not contender.acquire(blocking=False)
        finally:
            contender.close()
        events.append(event)

    class Server:
        def serve_forever(self):
            return None

        def shutdown(self):
            assert_claim_held("http-shutdown")

        def close_active_connections(self):
            assert_claim_held("connections-close")

        def server_close(self):
            assert_claim_held("http-close")

    class Control:
        def getsockname(self):
            return ("127.0.0.1", 43210)

        def close(self):
            assert_claim_held("control-close")

    class Thread:
        def __init__(self, *, target, daemon=True, args=()):
            self.target = target

        def start(self):
            if self.target.__name__ == "_finish_start_failure_cleanup":
                self.target()
            return None

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return False

    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease = runtime._lease_lock(paths, timeout=0.0)
        assert lease.acquire(blocking=False)
        assert lease.fd is not None
        with patch.object(runtime, "_paths", return_value=paths):
            daemon = runtime._Daemon(
                lease.fd, time.monotonic_ns() + 5_000_000_000
            )
        with patch.object(daemon.workers, "fetch_catalog") as fetch, pytest.raises(
            runtime.CatalogError
        ):
            daemon._provider()
        fetch.assert_not_called()
        with patch.object(runtime, "create_server", return_value=Server()), patch.object(
            runtime.threading, "Thread", Thread
        ), patch.object(daemon, "_static_ready", return_value=True), patch.object(
            daemon, "_bind_control", return_value=Control()
        ), patch.object(
            runtime,
            "_send_control",
            return_value={"status": "ready", "url": runtime.URL},
        ), patch.object(
            runtime, "_write_instance", side_effect=runtime.StartupError("publication failed")
        ):
            assert daemon.run() == 28

        assert events == [
            "http-shutdown",
            "connections-close",
            "http-close",
            "control-close",
        ]
        contender = runtime._lease_lock(paths, timeout=0.0)
        assert contender.acquire(blocking=False)
        contender.close()


def test_external_catalog_request_stays_closed_during_failed_publication():
    try:
        capability_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except PermissionError:
        pytest.skip("sandbox does not permit loopback sockets")
    else:
        capability_probe.close()

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        paths, _, _ = _fixture(root)
        worker_marker = root / "worker-started"
        lease = runtime._lease_lock(paths, timeout=0.0)
        assert lease.acquire(blocking=False)
        assert lease.fd is not None
        with patch.object(runtime, "_paths", return_value=paths):
            daemon = runtime._Daemon(lease.fd, time.monotonic_ns() + 20_000_000_000)
        daemon.workers = runtime.CatalogWorkerManager(
            command_factory=lambda: [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(worker_marker)!r}).write_text('started')",
            ]
        )
        publication_entered = threading.Event()
        allow_failure = threading.Event()

        def fail_publication(*_args, **_kwargs):
            publication_entered.set()
            assert allow_failure.wait(timeout=10)
            raise runtime.StartupError("injected publication failure")

        result: list[int] = []
        with patch.object(runtime, "_write_instance", side_effect=fail_publication):
            daemon_thread = threading.Thread(target=lambda: result.append(daemon.run()))
            daemon_thread.start()
            try:
                assert publication_entered.wait(timeout=15)
                connection = http.client.HTTPConnection("127.0.0.1", runtime.PORT, timeout=2)
                try:
                    connection.request(
                        "GET",
                        "/api/catalog",
                        headers={"Host": f"127.0.0.1:{runtime.PORT}"},
                    )
                    response = connection.getresponse()
                    body = json.loads(response.read())
                finally:
                    connection.close()
                assert response.status == 502
                assert body == {"error": "producer_unavailable"}
                assert not worker_marker.exists()
                contender = runtime._lease_lock(paths, timeout=0.0)
                assert not contender.acquire(blocking=False)
                contender.close()
            finally:
                allow_failure.set()
                daemon_thread.join(timeout=15)

        assert not daemon_thread.is_alive()
        assert result == [28]
        assert not worker_marker.exists()
        assert not (paths.runtime_directory / "instance.json").exists()
        contender = runtime._lease_lock(paths, timeout=0.0)
        assert contender.acquire(blocking=False)
        contender.close()


@pytest.mark.parametrize(
    ("operation", "timeout_name"),
    [
        (lambda root: runtime.setup(root), "STARTUP_TIMEOUT"),
        (lambda _root: runtime.start(), "STARTUP_TIMEOUT"),
        (lambda _root: runtime.stop(), "SHUTDOWN_TIMEOUT"),
    ],
)
def test_expired_public_admission_leaves_fresh_state_tree_absent(operation, timeout_name):
    with TemporaryDirectory() as temporary:
        base = Path(temporary)
        home = base / "home"
        home.mkdir()
        specification = base / "spec"
        specification.mkdir()
        original_lstat = state._lstat
        lookup_count = 0

        def delayed_admission_lookup(path):
            nonlocal lookup_count
            lookup_count += 1
            if lookup_count == 1:
                time.sleep(0.03)
            return original_lstat(path)

        with patch.object(state, "resolve_account_home", return_value=home), patch.object(
            runtime, timeout_name, 0.01
        ), patch.object(state, "_lstat", side_effect=delayed_admission_lookup), pytest.raises(
            (TimeoutError, runtime.RuntimeErrorBase)
        ):
            operation(specification)
        assert not (home / ".nyx").exists()


def test_native_open_retry_rechecks_public_deadline_before_retrying():
    busy = OSError(13, "sharing violation")
    busy.winerror = 32
    original_sleep = time.sleep

    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        with patch.object(runtime, "_paths", return_value=paths), patch.object(
            runtime, "STARTUP_TIMEOUT", 0.01
        ), patch.object(_native_claim, "_open_claim", side_effect=busy) as open_claim, patch.object(
            _native_claim.time, "sleep", side_effect=lambda _interval: original_sleep(0.02)
        ), pytest.raises(runtime.RuntimeErrorBase):
            runtime.start()

    assert open_claim.call_count == 1


def test_active_start_propagates_its_public_deadline_to_status_control():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        instance = _write_runtime_record(paths)
        lease = runtime._lease_lock(paths, timeout=0.0)
        assert lease.acquire(blocking=False)
        try:
            with patch.object(runtime, "_paths", return_value=paths), patch.object(
                runtime,
                "_send_control",
                return_value={"status": "ready", "url": runtime.URL},
            ) as send:
                assert runtime.start() == runtime.URL
        finally:
            lease.close()
        assert send.call_args.args == (instance, "status")
        assert send.call_args.kwargs["deadline"] > time.monotonic()


def test_public_setup_propagates_one_deadline_into_lifetime_acquisition():
    seen: list[float | None] = []
    original = runtime._lease_lock

    def capture(paths, *, timeout=runtime.LOCK_TIMEOUT, deadline=None):
        seen.append(deadline)
        return original(paths, timeout=timeout, deadline=deadline)

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        paths, _, specification = _fixture(root)
        with patch.object(runtime, "_paths", return_value=paths), patch.object(
            runtime, "_lease_lock", side_effect=capture
        ):
            assert runtime.setup(specification).specification_root == specification.resolve()
    assert len(seen) == 1
    assert seen[0] is not None and seen[0] > time.monotonic()


def test_expired_lifetime_deadline_prevents_native_open():
    lock = runtime._FileLock(Path("lease.lock"), deadline=time.monotonic() - 1)
    with patch.object(_native_claim, "_open_claim") as open_claim, pytest.raises(
        runtime.UnhealthyInstanceError
    ):
        lock.acquire(blocking=False)
    open_claim.assert_not_called()


def test_failed_acknowledgement_keeps_parent_claim_until_child_reaped():
    class Process:
        def __init__(self):
            self.running = True
            self.reaped = False

        def poll(self):
            return None if self.running else 1

        def terminate(self):
            self.running = False

        def wait(self, timeout=None):
            self.reaped = True
            return 1

        def kill(self):
            self.running = False

    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        process = Process()

        def reject_ack(_process, _ack_fd, _deadline):
            contender = runtime._lease_lock(paths, timeout=0.0)
            try:
                assert not contender.acquire(blocking=False)
            finally:
                contender.close()
            return False

        with patch.object(runtime, "_paths", return_value=paths), patch.object(
            runtime, "_spawn_daemon", return_value=process
        ), patch.object(runtime, "_wait_for_ack", side_effect=reject_ack), pytest.raises(
            runtime.StartupError
        ):
            runtime.start()
        assert process.reaped
        contender = runtime._lease_lock(paths, timeout=0.0)
        assert contender.acquire(blocking=False)
        contender.close()


def test_resistant_failed_child_defers_claim_release_without_additive_wait():
    release = threading.Event()

    class Process:
        killed = False
        reaped = False

        def poll(self):
            return None if not self.reaped else 1

        def terminate(self):
            return None

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("child", timeout)
            assert release.wait(timeout=2)
            self.reaped = True
            return 1

    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        process = Process()
        started = time.monotonic()
        with patch.object(runtime, "_paths", return_value=paths), patch.object(
            runtime, "STARTUP_TIMEOUT", 0.05
        ), patch.object(runtime, "_spawn_daemon", return_value=process), patch.object(
            runtime, "_wait_for_ack", return_value=False
        ), pytest.raises(runtime.StartupError):
            runtime.start()
        assert time.monotonic() - started < 0.5
        assert process.killed
        contender = runtime._lease_lock(paths, timeout=0.0)
        assert not contender.acquire(blocking=False)
        contender.close()
        release.set()
        deadline = time.monotonic() + 2
        while not process.reaped and time.monotonic() < deadline:
            time.sleep(0.01)
        assert process.reaped
        contender = runtime._lease_lock(paths, timeout=0.0)
        assert contender.acquire(blocking=False)
        contender.close()


def test_resistant_startup_cleanup_retains_claim_then_finishes_asynchronously():
    cleanup_entered = threading.Event()
    allow_cleanup = threading.Event()

    class Server:
        def serve_forever(self):
            return None

        def shutdown(self):
            cleanup_entered.set()
            assert allow_cleanup.wait(timeout=2)

        def close_active_connections(self):
            return None

        def server_close(self):
            return None

    class Control:
        def getsockname(self):
            return ("127.0.0.1", 43210)

        def close(self):
            return None

    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease = runtime._lease_lock(paths, timeout=0.0)
        assert lease.acquire(blocking=False)
        assert lease.fd is not None
        with patch.object(runtime, "_paths", return_value=paths):
            daemon = runtime._Daemon(lease.fd, time.monotonic_ns() + 50_000_000)
        with patch.object(runtime, "create_server", return_value=Server()), patch.object(
            daemon, "_static_ready", return_value=True
        ), patch.object(daemon, "_bind_control", return_value=Control()), patch.object(
            daemon, "_serve_control", return_value=None
        ), patch.object(
            runtime,
            "_send_control",
            return_value={"status": "ready", "url": runtime.URL},
        ), patch.object(
            runtime, "_write_instance", side_effect=runtime.StartupError("publication failed")
        ):
            daemon_thread = threading.Thread(target=daemon.run)
            daemon_thread.start()
            assert cleanup_entered.wait(timeout=1)
            deadline = time.monotonic() + 1
            while daemon.shutdown_result != "timeout" and time.monotonic() < deadline:
                time.sleep(0.005)
            assert daemon.shutdown_result == "timeout"
            contender = runtime._lease_lock(paths, timeout=0.0)
            assert not contender.acquire(blocking=False)
            contender.close()
            allow_cleanup.set()
            daemon_thread.join(timeout=2)

        assert not daemon_thread.is_alive()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            contender = runtime._lease_lock(paths, timeout=0.0)
            if contender.acquire(blocking=False):
                contender.close()
                break
            contender.close()
            time.sleep(0.01)
        else:
            raise AssertionError("startup cleanup did not release its lifetime claim")


def test_public_start_rejects_external_child_path_reacquisition_without_handoff_gap():
    child = (
        "import os,sys\n"
        "from pathlib import Path\n"
        "from nyx._native_claim import NativeClaim\n"
        "if '--daemon-handle' in sys.argv:\n"
        "    fd=NativeClaim.receive_handle(int(sys.argv[sys.argv.index('--daemon-handle')+1]))\n"
        "    ack=NativeClaim.receive_handle(int(sys.argv[sys.argv.index('--ack-handle')+1]), write_only=True)\n"
        "else:\n"
        "    fd=int(sys.argv[sys.argv.index('--daemon-fd')+1])\n"
        "    ack=int(sys.argv[sys.argv.index('--ack-fd')+1])\n"
        "path=Path(sys.argv[sys.argv.index('--claim-path')+1])\n"
        "os.close(fd); contender=NativeClaim(path)\n"
        "acquired=contender.acquire(create=False,blocking=False)\n"
        "Path(sys.argv[1]).write_text('free' if acquired else 'busy',encoding='utf-8')\n"
        "os.write(ack,b'1' if acquired else b'0'); contender.close()"
    )
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        paths, _, _ = _fixture(root)
        marker = root / "child-result"
        with patch.object(runtime, "_paths", return_value=paths), patch.object(
            runtime,
            "_daemon_command",
            side_effect=lambda _deadline_ns: [sys.executable, "-c", child, str(marker)],
        ), pytest.raises(runtime.StartupError) as caught:
            runtime.start()
        assert marker.read_text(encoding="utf-8") == "busy"
        assert "phase=acknowledgement-rejected;" in caught.value.__notes__[0]
        assert not (paths.runtime_directory / "instance.json").exists()
        contender = runtime._lease_lock(paths, timeout=0.0)
        assert contender.acquire(blocking=False)
        contender.close()


def test_posix_spawn_preserves_detached_descriptor_contract():
    process = object()
    with patch.object(runtime.os, "name", "posix"), patch.object(
        runtime.subprocess, "Popen", return_value=process
    ) as popen:
        assert runtime._spawn_daemon(11, 1234, ack_fd=22, claim_path=Path("claim")) is process
    command, kwargs = popen.call_args.args[0], popen.call_args.kwargs
    assert command[command.index("--daemon-fd") + 1] == "11"
    assert command[command.index("--ack-fd") + 1] == "22"
    assert kwargs["pass_fds"] == (11, 22)
    assert kwargs["close_fds"] is True
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


def test_daemon_publishes_authenticated_fixed_url_and_releases_transferred_lease():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        with patch.object(runtime, "_paths", return_value=paths):
            daemon = runtime._Daemon(lease_fd, int((time.monotonic() + 5) * 1_000_000_000))
            thread = threading.Thread(target=daemon.run)
            thread.start()
            instance = _wait_for_record(paths)
            assert instance.url == "http://127.0.0.1:8765/"
            assert "pid" not in json.loads(paths.runtime_directory.joinpath("instance.json").read_text())
            assert runtime._send_control(instance, "status")["url"] == instance.url
            assert runtime.stop() == "stopped"
            thread.join(timeout=5)
            assert not thread.is_alive()
            probe = runtime._lease_lock(paths, timeout=0.0)
            assert probe.acquire(blocking=False)
            probe.close()
            assert not paths.runtime_directory.joinpath("instance.json").exists()


def test_active_daemon_setup_revalidates_same_root_and_rejects_changed_root():
    with TemporaryDirectory() as temporary:
        paths, _, first = _fixture(Path(temporary))
        second = Path(temporary) / "second"
        second.mkdir()
        before = paths.config_file.read_bytes()
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        with patch.object(runtime, "_paths", return_value=paths):
            daemon = runtime._Daemon(lease_fd, int((time.monotonic() + 5) * 1_000_000_000))
            thread = threading.Thread(target=daemon.run)
            thread.start()
            _wait_for_record(paths)
            assert runtime.setup(first).specification_root == first.resolve()
            with pytest.raises(runtime.ActiveInstanceError):
                runtime.setup(second)
            assert paths.config_file.read_bytes() == before
            assert runtime.stop() == "stopped"
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_natural_unhealthy_daemon_keeps_record_and_lease():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        with patch.object(runtime, "_paths", return_value=paths):
            daemon = runtime._Daemon(lease_fd, int((time.monotonic() + 5) * 1_000_000_000))
            thread = threading.Thread(target=daemon.run)
            thread.start()
            _wait_for_record(paths)
            assert daemon.control is not None
            daemon.control.close()
            with pytest.raises(runtime.UnhealthyInstanceError):
                runtime.stop()
            assert paths.runtime_directory.joinpath("instance.json").exists()
            probe = runtime._lease_lock(paths, timeout=0.0)
            assert not probe.acquire(blocking=False)
            probe.close()
            assert daemon.shutdown() == "stopped"
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_held_lease_excludes_changed_setup_without_mutating_configuration():
    with TemporaryDirectory() as temporary:
        paths, _, first = _fixture(Path(temporary))
        second = Path(temporary) / "second"
        second.mkdir()
        lease = runtime._lease_lock(paths, timeout=0.0)
        assert lease.acquire(blocking=False)
        try:
            with patch.object(runtime, "_paths", return_value=paths), pytest.raises(
                runtime.ActiveInstanceError
            ):
                runtime.setup(second)
        finally:
            lease.close()
        with patch.object(state, "resolve_account_home", return_value=paths.account_home):
            assert state.load_configuration(paths).specification_root == first.resolve()


def test_held_lease_allows_same_root_setup_without_mutation():
    with TemporaryDirectory() as temporary:
        paths, _, first = _fixture(Path(temporary))
        before = paths.config_file.read_bytes()
        lease = runtime._lease_lock(paths, timeout=0.0)
        assert lease.acquire(blocking=False)
        try:
            with patch.object(runtime, "_paths", return_value=paths):
                configuration = runtime.setup(first)
        finally:
            lease.close()
        assert configuration.specification_root == first.resolve()
        assert paths.config_file.read_bytes() == before


def test_held_lease_setup_rejects_expiry_after_real_configuration_admission():
    with TemporaryDirectory() as temporary:
        paths, _, first = _fixture(Path(temporary))
        lease = runtime._lease_lock(paths, timeout=0.0)
        assert lease.acquire(blocking=False)
        records = (
            paths.config_file,
            paths.runtime_directory / "operation.lock",
            paths.runtime_directory / "lease.lock",
        )

        def snapshot():
            result = {}
            for path in records:
                details = path.lstat()
                result[path] = (details.st_dev, details.st_ino, details.st_size, path.read_bytes())
            return result

        before = snapshot()
        original_lstat = state._lstat
        lookup_delayed = False

        def delayed_admission_lookup(path):
            nonlocal lookup_delayed
            if not lookup_delayed:
                lookup_delayed = True
                time.sleep(0.03)
            return original_lstat(path)

        try:
            with patch.object(runtime, "_paths", return_value=paths), patch.object(
                runtime, "STARTUP_TIMEOUT", 0.01
            ), patch.object(
                state, "_lstat", side_effect=delayed_admission_lookup
            ), patch.object(
                state, "_save_configuration", wraps=state._save_configuration
            ) as save_configuration, pytest.raises(
                runtime.UnhealthyInstanceError, match="deadline expired"
            ):
                runtime.setup(first)
            save_configuration.assert_not_called()
            assert lookup_delayed
            assert snapshot() == before
        finally:
            lease.close()


def test_held_lease_excludes_changed_hidden_stage_policy_without_mutation():
    with TemporaryDirectory() as temporary:
        paths, _, first = _fixture(Path(temporary))
        with patch.object(runtime, "_paths", return_value=paths):
            runtime.setup(first, ["Queue"])
        before = paths.config_file.read_bytes()
        lease = runtime._lease_lock(paths, timeout=0.0)
        assert lease.acquire(blocking=False)
        try:
            with patch.object(runtime, "_paths", return_value=paths), pytest.raises(
                runtime.ActiveInstanceError
            ):
                runtime.setup(first, ["Other"])
        finally:
            lease.close()
        assert paths.config_file.read_bytes() == before
        assert state.load_configuration(paths).hidden_stages == ("Queue",)


def test_held_lease_allows_identical_hidden_stage_policy_without_mutation():
    with TemporaryDirectory() as temporary:
        paths, _, first = _fixture(Path(temporary))
        with patch.object(runtime, "_paths", return_value=paths):
            runtime.setup(first, ["Queue"])
        before = paths.config_file.read_bytes()
        lease = runtime._lease_lock(paths, timeout=0.0)
        assert lease.acquire(blocking=False)
        try:
            with patch.object(runtime, "_paths", return_value=paths):
                configuration = runtime.setup(first, ["Queue", "Queue"])
        finally:
            lease.close()
        assert configuration.hidden_stages == ("Queue",)
        assert paths.config_file.read_bytes() == before


@pytest.mark.parametrize("alias", [state.setup, state.save_configuration])
@pytest.mark.parametrize("requested", ["root", "policy"])
def test_held_lease_rejects_changed_root_or_policy_through_state_aliases(alias, requested):
    with TemporaryDirectory() as temporary:
        paths, _, first = _fixture(Path(temporary))
        second = Path(temporary) / "second"
        second.mkdir()
        with patch.object(runtime, "_paths", return_value=paths):
            runtime.setup(first, ["Queue"])
        before = paths.config_file.read_bytes()
        lease = runtime._lease_lock(paths, timeout=0.0)
        assert lease.acquire(blocking=False)
        try:
            arguments = (second, ["Queue"]) if requested == "root" else (first, ["Other"])
            with patch.object(runtime, "_paths", return_value=paths), pytest.raises(
                runtime.ActiveInstanceError
            ):
                alias(*arguments)
        finally:
            lease.close()
        assert paths.config_file.read_bytes() == before
        assert state.load_configuration(paths).specification_root == first.resolve()
        assert state.load_configuration(paths).hidden_stages == ("Queue",)


def test_active_schema_one_identity_preserves_legacy_bytes():
    with TemporaryDirectory() as temporary:
        paths, _, first = _fixture(Path(temporary))
        paths.config_file.write_text(
            json.dumps({"schema_version": 1, "specification_root": str(first.resolve())}) + "\n",
            encoding="utf-8",
        )
        before = paths.config_file.read_bytes()
        lease = runtime._lease_lock(paths, timeout=0.0)
        assert lease.acquire(blocking=False)
        try:
            with patch.object(runtime, "_paths", return_value=paths):
                configuration = runtime.setup(first)
        finally:
            lease.close()
        assert configuration.hidden_stages == ()
        assert paths.config_file.read_bytes() == before


def test_active_schema_one_identity_preserves_noncanonical_legacy_bytes():
    with TemporaryDirectory() as temporary:
        paths, _, first = _fixture(Path(temporary))
        legacy = (
            '{  "specification_root" : '
            + json.dumps(str(first.resolve()))
            + ', "schema_version" : 1 }\n\n'
        ).encode("utf-8")
        paths.config_file.write_bytes(legacy)
        before = paths.config_file.read_bytes()
        lease = runtime._lease_lock(paths, timeout=0.0)
        assert lease.acquire(blocking=False)
        try:
            with patch.object(runtime, "_paths", return_value=paths):
                configuration = runtime.setup(first)
        finally:
            lease.close()
        assert configuration.hidden_stages == ()
        assert paths.config_file.read_bytes() == before


def test_free_lease_removes_stale_record_without_pid_signal():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        paths.runtime_directory.joinpath("instance.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "instance_id": "stale",
                    "url": runtime.URL,
                    "capability": "stale-capability",
                    "control": runtime._control_endpoint(1),
                }
            ),
            encoding="utf-8",
        )
        os.chmod(paths.runtime_directory / "instance.json", 0o600)
        with patch.object(runtime, "_paths", return_value=paths):
            assert runtime.stop() == "stopped"
        assert not paths.runtime_directory.joinpath("instance.json").exists()


def test_free_lease_retains_unsafe_instance_record():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        sentinel = Path(temporary) / "sentinel"
        sentinel.write_text("keep", encoding="utf-8")
        paths.runtime_directory.joinpath("instance.json").symlink_to(sentinel)
        with patch.object(runtime, "_paths", return_value=paths), pytest.raises(
            runtime.UnhealthyInstanceError
        ):
            runtime.stop()
        assert sentinel.read_text(encoding="utf-8") == "keep"
        assert paths.runtime_directory.joinpath("instance.json").is_symlink()


def test_shutdown_does_not_remove_replaced_instance_record():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        record = paths.runtime_directory / "instance.json"
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        try:
            with patch.object(runtime, "_paths", return_value=paths):
                daemon = runtime._Daemon(lease_fd, int((time.monotonic() + 5) * 1_000_000_000))
                daemon.instance = runtime.Instance(
                    "published-instance",
                    runtime.URL,
                    "published-capability",
                    runtime._control_endpoint(1),
                )
                daemon.published_record = runtime._write_instance(
                    paths,
                    daemon.instance,
                    deadline=time.monotonic() + 1,
                )
                assert runtime._read_instance(paths) == daemon.instance
                record.write_bytes(b"replacement-record")
                assert daemon.shutdown(deadline=time.monotonic() + 1) == "stopped"
            assert record.read_bytes() == b"replacement-record"
        finally:
            os.close(lease_fd)


@pytest.mark.parametrize(
    "expiring_phase", ["http-shutdown", "connections-close", "http-close", "workers-close"]
)
def test_shutdown_deadline_stops_before_next_cleanup_mutation(expiring_phase):
    events: list[str] = []
    clock = [1.0]

    def phase(name: str) -> None:
        events.append(name)
        if name == expiring_phase:
            clock[0] = 2.0

    class Server:
        def shutdown(self):
            phase("http-shutdown")

        def close_active_connections(self):
            phase("connections-close")

        def server_close(self):
            phase("http-close")

    class Control:
        def close(self):
            events.append("control-close")

    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        record = paths.runtime_directory / "instance.json"
        record.write_bytes(b"owned-record")
        lease_fd = os.open(paths.runtime_directory / "lease.lock", os.O_RDWR)
        try:
            with patch.object(runtime, "_paths", return_value=paths):
                daemon = runtime._Daemon(lease_fd, time.monotonic_ns() + 5_000_000_000)
            daemon.server = Server()
            daemon.control = Control()
            daemon.published_record = runtime._record_snapshot(record)
            with patch.object(daemon.workers, "close_admission"), patch.object(
                daemon.workers,
                "close",
                side_effect=lambda deadline: phase("workers-close") or True,
            ), patch.object(runtime.time, "monotonic", side_effect=lambda: clock[0]):
                assert daemon.shutdown(deadline=2.0) == "timeout"
            assert record.read_bytes() == b"owned-record"
            assert "control-close" not in events
            assert events[-1] == expiring_phase
        finally:
            os.close(lease_fd)


def test_terminal_shutdown_cannot_timeout_after_removing_owned_record():
    events: list[str] = []

    class Control:
        def close(self):
            events.append("control-close")

    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        record = paths.runtime_directory / "instance.json"
        record.write_bytes(b"owned-record")
        lease_fd = os.open(paths.runtime_directory / "lease.lock", os.O_RDWR)
        try:
            with patch.object(runtime, "_paths", return_value=paths):
                daemon = runtime._Daemon(lease_fd, time.monotonic_ns() + 5_000_000_000)
            daemon.control = Control()
            daemon.published_record = runtime._record_snapshot(record)
            with patch.object(daemon.workers, "close_admission"), patch.object(
                daemon.workers, "close", return_value=True
            ), patch.object(runtime.time, "monotonic", side_effect=[1, 1, 1, 1, 2]):
                assert daemon.shutdown(deadline=2.0) == "stopped"
            assert not record.exists()
            assert events == ["control-close"]
        finally:
            os.close(lease_fd)


def test_wrong_capability_cannot_control_a_ready_instance():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        with patch.object(runtime, "_paths", return_value=paths):
            daemon = runtime._Daemon(lease_fd, int((time.monotonic() + 5) * 1_000_000_000))
            thread = threading.Thread(target=daemon.run)
            thread.start()
            instance = _wait_for_record(paths)
            forged = runtime.Instance(
                instance_id=instance.instance_id,
                url=instance.url,
                capability="wrong-capability",
                control=instance.control,
            )
            with pytest.raises(runtime.UnhealthyInstanceError):
                runtime._send_control(forged, "stop")
            assert runtime._send_control(instance, "status")["status"] == "ready"
            assert runtime.stop() == "stopped"
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_stop_ack_failure_and_duplicate_requests_retain_published_cleanup():
    published_at_send: list[bool] = []
    shutdown_deadlines: list[float] = []
    finished_deadlines: list[float] = []
    shutdown_lock = threading.Lock()
    both_shutdowns_started = threading.Event()
    both_shutdowns_finished = threading.Event()
    release_shutdowns = threading.Event()

    class Connection:
        def __init__(self, deadline_ns: int, *, fail_response: bool = False):
            self.deadline_ns = deadline_ns
            self.fail_response = fail_response

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, _timeout):
            return None

        def recv(self, _limit):
            return (
                json.dumps(
                    {
                        "version": 1,
                        "capability": "capability",
                        "command": "stop",
                        "deadline_ns": self.deadline_ns,
                    }
                )
                + "\n"
            ).encode()

        def sendall(self, data):
            response = json.loads(data.splitlines()[0])
            assert response["status"] == "stopping"
            published_at_send.append(daemon.stop_requested.is_set())
            if self.fail_response:
                raise OSError("caller disconnected during acknowledgement")

    class Control:
        def __init__(self):
            self.connections = iter(
                [
                    Connection(2_000_000_000, fail_response=True),
                    Connection(3_000_000_000),
                ]
            )

        def accept(self):
            try:
                return next(self.connections), None
            except StopIteration:
                raise OSError("test control complete") from None

    def shutdown(deadline):
        with shutdown_lock:
            shutdown_deadlines.append(deadline)
            if len(shutdown_deadlines) == 2:
                both_shutdowns_started.set()
        assert release_shutdowns.wait(timeout=2)
        with shutdown_lock:
            finished_deadlines.append(deadline)
            if len(finished_deadlines) == 2:
                both_shutdowns_finished.set()
        return "timeout"

    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd = os.open(paths.runtime_directory / "lease.lock", os.O_RDWR)
        try:
            with patch.object(runtime, "_paths", return_value=paths):
                daemon = runtime._Daemon(lease_fd, time.monotonic_ns() + 5_000_000_000)
            daemon.instance = runtime.Instance(
                instance_id="instance",
                url=runtime.URL,
                capability="capability",
                control=runtime._control_endpoint(1),
            )
            daemon.control = Control()
            with patch.object(daemon, "shutdown", side_effect=shutdown):
                control_thread = threading.Thread(target=daemon._serve_control)
                control_thread.start()
                control_thread.join(timeout=2)
                assert not control_thread.is_alive()
                assert both_shutdowns_started.wait(timeout=2)
                release_shutdowns.set()
                assert both_shutdowns_finished.wait(timeout=2)
            assert published_at_send == [True, True]
            assert sorted(shutdown_deadlines) == [2.0, 3.0]
            assert sorted(finished_deadlines) == [2.0, 3.0]
        finally:
            release_shutdowns.set()
            os.close(lease_fd)


def test_held_lease_retains_valid_stale_record_without_control_or_signal():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        record = paths.runtime_directory / "instance.json"
        record.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "instance_id": "stale",
                    "url": runtime.URL,
                    "capability": "stale-capability",
                    "control": runtime._control_endpoint(1),
                }
            ),
            encoding="utf-8",
        )
        os.chmod(record, 0o600)
        lease = runtime._lease_lock(paths, timeout=0.0)
        assert lease.acquire(blocking=False)
        try:
            with patch.object(runtime, "_paths", return_value=paths), pytest.raises(
                runtime.UnhealthyInstanceError
            ):
                runtime.stop()
        finally:
            lease.close()
        assert record.exists()


def test_fixed_port_occupant_causes_startup_failure_without_fallback():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        occupant = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        occupant.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupant.bind(("127.0.0.1", runtime.PORT))
        occupant.listen(1)
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        result: list[int] = []
        try:
            with patch.object(runtime, "_paths", return_value=paths):
                daemon = runtime._Daemon(lease_fd, int((time.monotonic() + 5) * 1_000_000_000))
                thread = threading.Thread(target=lambda: result.append(daemon.run()))
                thread.start()
                thread.join(timeout=8)
                assert not thread.is_alive()
            assert result in ([25], [30])
            if daemon.server is not None:
                assert daemon.server.server_address == ("127.0.0.1", runtime.PORT)
            assert not paths.runtime_directory.joinpath("instance.json").exists()
            assert not list(paths.runtime_directory.glob(".instance.json.*"))
        finally:
            occupant.close()


def _write_runtime_record(
    paths: state.StatePaths,
    *,
    instance_id: str = "instance",
    capability: str = "capability",
    control: str | None = None,
) -> runtime.Instance:
    instance = runtime.Instance(
        instance_id,
        runtime.URL,
        capability,
        runtime._control_endpoint(1) if control is None else control,
    )
    record = paths.runtime_directory / "instance.json"
    record.write_text(json.dumps(instance.as_dict()) + "\n", encoding="utf-8")
    os.chmod(record, 0o600)
    return instance


def _tcp_control_record(
    paths: state.StatePaths,
) -> tuple[runtime.Instance, socket.socket]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
    except BaseException:
        listener.close()
        raise
    instance = _write_runtime_record(
        paths,
        control=runtime._control_endpoint(listener.getsockname()[1]),
    )
    return instance, listener


def _held_lease(paths: state.StatePaths) -> tuple[int, runtime._FileLock]:
    lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
    lease = runtime._lease_lock(paths, timeout=0.0)
    return lease_fd, lease


def _observe(paths: state.StatePaths) -> runtime.RuntimeObservation:
    with patch.object(state, "resolve_account_home", return_value=paths.account_home), patch.object(
        state, "_current_uid", return_value=state._current_uid()
    ):
        return runtime.observe_runtime()


@pytest.mark.parametrize("damage", ["absent", "malformed", "replaced"])
def test_live_locator_damage_never_authorizes_setup_replacement(damage):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        paths, _, first = _fixture(root)
        second = root / "second-specification"
        second.mkdir()
        original = _write_runtime_record(paths)
        record = paths.runtime_directory / "instance.json"
        if damage == "absent":
            record.unlink()
        elif damage == "malformed":
            record.write_bytes(b"not-json\n")
        else:
            _write_runtime_record(paths, instance_id="replacement", capability="foreign")
        before = paths.config_file.read_bytes()
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        try:
            with patch.object(runtime, "_paths", return_value=paths), pytest.raises(
                runtime.ActiveInstanceError
            ):
                runtime.setup(second)
        finally:
            os.close(lease_fd)
        assert paths.config_file.read_bytes() == before
        assert state.load_configuration(paths).specification_root == first.resolve()
        if damage == "absent":
            assert not record.exists()
        elif damage == "malformed":
            assert record.read_bytes() == b"not-json\n"
        else:
            assert runtime._read_instance(paths).instance_id == "replacement"
        assert original.instance_id == "instance"


def test_live_replaced_locator_is_rejected_before_start_authorization():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        original = _write_runtime_record(paths)
        replacement = runtime.Instance(
            "replacement", runtime.URL, "foreign", runtime._control_endpoint(2)
        )
        record = paths.runtime_directory / "instance.json"
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")

        def replace_before_response(instance, command, **_kwargs):
            assert instance == original
            assert command == "status"
            runtime._write_instance(paths, replacement, deadline=time.monotonic() + 1)
            return {"status": "ready", "instance_id": original.instance_id, "url": runtime.URL}

        try:
            with patch.object(runtime, "_paths", return_value=paths), patch.object(
                runtime, "_send_control", side_effect=replace_before_response
            ), pytest.raises(runtime.UnhealthyInstanceError, match="record changed"):
                runtime.start()
        finally:
            os.close(lease_fd)
        assert record.read_bytes() == (
            json.dumps(replacement.as_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()


def test_live_replaced_locator_is_rejected_before_stop_cleanup():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        original = _write_runtime_record(paths)
        replacement = runtime.Instance(
            "replacement", runtime.URL, "foreign", runtime._control_endpoint(2)
        )
        record = paths.runtime_directory / "instance.json"
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")

        def replace_before_response(instance, command, **_kwargs):
            assert instance == original
            assert command == "stop"
            runtime._write_instance(paths, replacement, deadline=time.monotonic() + 1)
            return {"status": "stopping", "instance_id": original.instance_id, "url": runtime.URL}

        try:
            with patch.object(runtime, "_paths", return_value=paths), patch.object(
                runtime, "_send_control", side_effect=replace_before_response
            ), patch.object(runtime, "SHUTDOWN_TIMEOUT", 0.1), pytest.raises(
                runtime.UnhealthyInstanceError, match="record changed"
            ):
                runtime.stop()
        finally:
            os.close(lease_fd)
        assert record.read_bytes() == (
            json.dumps(replacement.as_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()


@pytest.mark.parametrize(
    ("control", "label"),
    [
        ("", "empty"),
        ("127.0.0.1:-1", "negative"),
        ("127.0.0.1:0", "zero"),
        ("127.0.0.1:65536", "too-large"),
        ("127.0.0.1:+1", "signed-positive"),
        ("127.0.0.1:01", "leading-zero"),
        ("127.0.0.1:1 ", "trailing-whitespace"),
        (" 127.0.0.1:1", "leading-whitespace"),
        ("127.0.0.1:١", "non-ascii-digit"),
        ("localhost:1", "alternate-host"),
        ("http://127.0.0.1:1", "url-scheme"),
        ("127.0.0.1:1/suffix", "suffix"),
        ("\x00nyx-control-1000", "legacy-abstract-socket"),
    ],
)
def test_public_lifecycle_rejects_invalid_control_records_without_use(
    control: str, label: str
):
    del label
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd, lease = _held_lease(paths)
        record = paths.runtime_directory / "instance.json"
        _write_runtime_record(paths, control=control)
        before = record.read_bytes()
        try:
            with patch.object(runtime, "_send_control") as send_control, patch.object(
                runtime.socket, "socket", side_effect=AssertionError("socket use")
            ) as create_socket:
                observed = _observe(paths)
            assert observed.status == "unknown"
            assert observed.diagnostic == runtime.RUNTIME_STATE_UNAVAILABLE
            send_control.assert_not_called()
            create_socket.assert_not_called()

            with patch.object(runtime, "_paths", return_value=paths), patch.object(
                runtime, "_send_control"
            ) as send_control, patch.object(
                runtime.socket, "socket", side_effect=AssertionError("socket use")
            ) as create_socket, pytest.raises(
                runtime.UnhealthyInstanceError, match="record is unavailable"
            ):
                runtime.start()
            send_control.assert_not_called()
            create_socket.assert_not_called()

            with patch.object(runtime, "_paths", return_value=paths), patch.object(
                runtime, "_send_control"
            ) as send_control, patch.object(
                runtime.socket, "socket", side_effect=AssertionError("socket use")
            ) as create_socket, pytest.raises(
                runtime.UnhealthyInstanceError, match="record is unavailable"
            ):
                runtime.stop()
            send_control.assert_not_called()
            create_socket.assert_not_called()
        finally:
            lease.close()
            os.close(lease_fd)
        assert record.read_bytes() == before


@pytest.mark.parametrize(
    ("control", "port"),
    [("127.0.0.1:1", 1), ("127.0.0.1:65535", 65535)],
)
def test_record_consumption_accepts_control_port_boundaries(control: str, port: int):
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        _write_runtime_record(paths, control=control)
        assert runtime._read_instance(paths).control == control
        assert runtime._parse_control_endpoint(control) == (
            socket.AF_INET,
            ("127.0.0.1", port),
        )


def test_free_lease_start_and_stop_retain_invalid_stale_control_record():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        record = paths.runtime_directory / "instance.json"
        _write_runtime_record(paths, control="127.0.0.1:0")
        before = record.read_bytes()

        for operation in (runtime.start, runtime.stop):
            with patch.object(runtime, "_paths", return_value=paths), patch.object(
                runtime, "_send_control"
            ) as send_control, patch.object(
                runtime.socket, "socket", side_effect=AssertionError("socket use")
            ) as create_socket, pytest.raises(
                runtime.UnhealthyInstanceError, match="record is unavailable"
            ):
                operation()
            send_control.assert_not_called()
            create_socket.assert_not_called()
            assert record.read_bytes() == before


def _claim_snapshot(path: Path) -> tuple[int, int, int, int, int, int]:
    details = path.lstat()
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def test_observe_runtime_preserves_empty_existing_claims_and_metadata():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        claims = [paths.runtime_directory / "operation.lock", paths.runtime_directory / "lease.lock"]
        for claim in claims:
            claim.write_bytes(b"")
        before = {claim: (_claim_snapshot(claim), claim.read_bytes()) for claim in claims}

        observed = _observe(paths)

        assert observed.status == "not_running", observed.diagnostic
        assert observed.diagnostic is None
        assert {claim: (_claim_snapshot(claim), claim.read_bytes()) for claim in claims} == before


@pytest.mark.parametrize("replacement_phase", ["before_hold", "after_hold"])
@pytest.mark.parametrize("replaced_directory", ["state", "runtime"])
def test_observe_runtime_revalidates_ancestry_replaced_after_admission(
    replaced_directory,
    replacement_phase,
):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        paths, _, _ = _fixture(root)
        external_state = root / "external-state"
        external_runtime = external_state / "runtime"
        external_runtime.mkdir(parents=True)
        outside_claim = external_runtime / "operation.lock"
        outside_claim.write_bytes(b"outside")
        outside_before = (_claim_snapshot(outside_claim), outside_claim.read_bytes())
        replaced_path = (
            paths.state_directory
            if replaced_directory == "state"
            else paths.runtime_directory
        )
        replacement = external_state if replaced_directory == "state" else external_runtime
        admitted_path = root / f"admitted-{replaced_directory}"
        parent_path = (
            paths.account_home
            if replaced_directory == "state"
            else paths.state_directory
        )
        replaced = False
        replacement_prevented = False

        def replace_directory() -> None:
            nonlocal replaced
            nonlocal replacement_prevented
            try:
                replaced_path.rename(admitted_path)
                if sys.platform == "win32":
                    completed = subprocess.run(
                        ["cmd", "/c", "mklink", "/J", str(replaced_path), str(replacement)],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    if completed.returncode:
                        raise OSError(completed.stderr or completed.stdout)
                else:
                    replaced_path.symlink_to(replacement, target_is_directory=True)
            except OSError:
                replacement_prevented = True
            else:
                replaced = True

        original_stat_child = runtime.NativeDirectory.stat_child
        original_open_child = runtime.NativeDirectory.open_child

        def replace_before_hold(directory, name):
            details = original_stat_child(directory, name)
            if (
                replacement_phase == "before_hold"
                and not replaced
                and directory.path == parent_path
                and name == replaced_path.name
            ):
                replace_directory()
            return details

        def replace_after_hold(directory, name, path):
            held = original_open_child(directory, name, path)
            if (
                replacement_phase == "after_hold"
                and not replaced
                and path == replaced_path
            ):
                replace_directory()
            return held

        external_runtime_resolved = external_runtime.resolve()
        outside_accesses: list[tuple[str, Path]] = []
        original_iterdir = Path.iterdir
        original_read_bytes = Path.read_bytes
        original_read_text = Path.read_text
        original_exists = Path.exists
        original_probe = runtime.NativeClaim.probe

        def record_iteration(path: Path):
            if path.resolve() == external_runtime_resolved:
                outside_accesses.append(("iteration", path))
            return original_iterdir(path)

        def record_bytes_read(path: Path):
            if path.resolve().is_relative_to(external_runtime_resolved):
                outside_accesses.append(("bytes", path))
            return original_read_bytes(path)

        def record_text_read(path: Path, *args, **kwargs):
            if path.resolve().is_relative_to(external_runtime_resolved):
                outside_accesses.append(("text", path))
            return original_read_text(path, *args, **kwargs)

        def record_exists(path: Path):
            if path.resolve().is_relative_to(external_runtime_resolved):
                outside_accesses.append(("exists", path))
            return original_exists(path)

        def record_claim_probe(path: Path, *, dir_fd=None):
            if dir_fd is None and path.resolve().is_relative_to(external_runtime_resolved):
                outside_accesses.append(("claim", path))
            return original_probe(path, dir_fd=dir_fd)

        with patch.object(state, "resolve_account_home", return_value=paths.account_home), patch.object(
            state, "_current_uid", return_value=state._current_uid()
        ), patch.object(
            runtime.NativeDirectory, "stat_child", replace_before_hold
        ), patch.object(
            runtime.NativeDirectory, "open_child", replace_after_hold
        ), patch.object(
            Path, "iterdir", record_iteration
        ), patch.object(
            Path, "read_bytes", record_bytes_read
        ), patch.object(
            Path, "read_text", record_text_read
        ), patch.object(
            Path, "exists", record_exists
        ), patch.object(
            runtime.NativeClaim, "probe", side_effect=record_claim_probe
        ):
            observed = runtime.observe_runtime()

        assert replaced or replacement_prevented
        if replaced:
            assert observed.status == "unknown"
            assert observed.diagnostic in {
                runtime.RUNTIME_STATE_CHANGED,
                runtime.RUNTIME_STATE_UNAVAILABLE,
            }
        else:
            assert os.name == "nt" and replacement_phase == "after_hold"
            assert observed.status == "not_running"
        assert outside_accesses == []
        assert (_claim_snapshot(outside_claim), outside_claim.read_bytes()) == outside_before


@pytest.mark.skipif(sys.platform != "win32", reason="requires a native Windows junction")
def test_observe_runtime_rejects_native_runtime_junction_before_outside_iteration():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        paths, _, _ = _fixture(root)
        paths.runtime_directory.rmdir()
        external = root / "external"
        external.mkdir()
        outside_claim = external / "operation.lock"
        outside_claim.write_bytes(b"outside")
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(paths.runtime_directory), str(external)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        iterated: list[Path] = []
        original_iterdir = Path.iterdir

        def record_iteration(path: Path):
            iterated.append(path)
            return original_iterdir(path)

        with patch.object(state, "resolve_account_home", return_value=paths.account_home), patch.object(
            state, "_current_uid", return_value=state._current_uid()
        ), patch.object(Path, "iterdir", record_iteration):
            observed = runtime.observe_runtime()

        assert observed.status == "unknown"
        assert observed.diagnostic == runtime.RUNTIME_STATE_UNAVAILABLE
        assert paths.runtime_directory not in iterated
        assert outside_claim.read_bytes() == b"outside"


def test_observe_runtime_reports_operation_contention_without_mutation():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        operation = runtime._operation_lock(paths, timeout=0.0)
        assert operation.acquire(blocking=False)
        def snapshot() -> dict[str, tuple[tuple[int, int, int, int], bytes | None]]:
            result = {}
            for path in paths.runtime_directory.iterdir():
                details = path.lstat()
                identity = (
                    details.st_mode,
                    details.st_size,
                    details.st_mtime_ns,
                    details.st_ctime_ns,
                )
                payload = None if path.name == "operation.lock" else path.read_bytes()
                result[path.name] = identity, payload
            return result

        before = snapshot()
        try:
            observed = _observe(paths)
        finally:
            operation.close()
        assert observed.status == "unknown"
        assert observed.diagnostic == runtime.RUNTIME_OPERATION_IN_PROGRESS
        after = snapshot()
        assert after == before


def test_observe_runtime_reports_free_lease_stale_record_and_retains_bytes():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        record = paths.runtime_directory / "instance.json"
        instance = _write_runtime_record(paths)
        before = record.read_bytes()
        before_mode = stat.S_IMODE(record.stat().st_mode)
        observed = _observe(paths)
        assert observed.status == "not_running", observed.diagnostic
        assert observed.diagnostic is None
        assert record.read_bytes() == before
        assert stat.S_IMODE(record.stat().st_mode) == before_mode
        assert instance.instance_id == "instance"


def test_observe_runtime_rechecks_absent_runtime_before_reporting_stopped():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        paths.runtime_directory.joinpath("operation.lock").unlink()
        paths.runtime_directory.joinpath("lease.lock").unlink()
        paths.runtime_directory.rmdir()
        operation_holder: runtime._FileLock | None = None
        original_stat_child = runtime.NativeDirectory.stat_child
        runtime_checks = 0

        def check_then_start(directory, name):
            nonlocal operation_holder
            nonlocal runtime_checks
            if directory.path == paths.state_directory and name == paths.runtime_directory.name:
                runtime_checks += 1
                if runtime_checks == 2:
                    paths.runtime_directory.mkdir(mode=0o700, parents=True)
                    operation_holder = runtime._operation_lock(paths, timeout=0.0)
                    assert operation_holder.acquire(blocking=False)
            return original_stat_child(directory, name)

        try:
            with patch.object(state, "resolve_account_home", return_value=paths.account_home), patch.object(
                state, "_current_uid", return_value=state._current_uid()
            ), patch.object(runtime.NativeDirectory, "stat_child", check_then_start):
                observed = runtime.observe_runtime()
        finally:
            if operation_holder is not None:
                operation_holder.close()
        assert observed.status == "unknown"
        assert observed.diagnostic == runtime.RUNTIME_STATE_CHANGED
        assert paths.runtime_directory.joinpath("operation.lock").exists()


def test_observe_runtime_requires_same_uid_before_sending_capability():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        if os.name == "nt":
            lease_fd, lease = _held_lease(paths)
            instance, listener = _tcp_control_record(paths)
            received: list[bytes] = []

            def serve_tcp() -> None:
                connection, _ = listener.accept()
                with connection:
                    received.append(connection.recv(4096))
                    connection.sendall(
                        (
                            json.dumps(
                                {
                                    "status": "ready",
                                    "instance_id": instance.instance_id,
                                    "url": runtime.URL,
                                }
                            )
                            + "\n"
                        ).encode()
                    )

            server = threading.Thread(target=serve_tcp)
            server.start()
            try:
                with patch.object(
                    runtime, "_peer_uid", side_effect=AssertionError("TCP has no UID probe")
                ):
                    observed = _observe(paths)
            finally:
                listener.close()
                server.join(timeout=3)
                lease.close()
                os.close(lease_fd)
            assert observed.status == "running"
            assert instance.capability.encode("utf-8") in b"".join(received)
            return

        control_path = paths.account_home.parent / "peer-control.sock"
        instance = _write_runtime_record(paths, control="local-peer-control")
        lease_fd, lease = _held_lease(paths)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(control_path))
        except PermissionError:
            listener.close()
            lease.close()
            os.close(lease_fd)
            pytest.skip("sandbox does not permit local control sockets")
        listener.listen(1)
        listener.settimeout(3)
        received: list[bytes] = []

        def serve() -> None:
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                return
            with connection:
                connection.settimeout(2)
                try:
                    received.append(connection.recv(1024))
                except TimeoutError:
                    received.append(b"")

        server = threading.Thread(target=serve)
        server.start()
        try:
            with patch.object(
                runtime, "_parse_control_endpoint", return_value=(socket.AF_UNIX, str(control_path))
            ), patch.object(
                runtime, "_peer_uid", return_value=state._current_uid() + 1
            ), patch.object(runtime.os, "getuid", return_value=state._current_uid(), create=True):
                observed = _observe(paths)
        finally:
            server.join(timeout=3)
            listener.close()
            lease.close()
            os.close(lease_fd)
        assert observed.status == "unknown"
        assert not server.is_alive()
        assert observed.diagnostic == runtime.RUNTIME_CONTROL_IDENTITY_MISMATCH
        assert received == [b""]
        assert instance.capability.encode("utf-8") not in b"".join(received)


def test_observe_runtime_accepts_authenticated_ready_control_and_rechecks_state():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd, lease = _held_lease(paths)
        try:
            instance, listener = _tcp_control_record(paths)
        except PermissionError:
            lease.close()
            os.close(lease_fd)
            pytest.skip("sandbox does not permit local control sockets")

        listener.settimeout(1)
        served: list[str] = []
        requests: list[dict[str, object]] = []
        def serve() -> None:
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(1)
                    requests.append(json.loads(connection.recv(4096).splitlines()[0]))
                    connection.sendall(
                        (json.dumps({"status": "ready", "instance_id": instance.instance_id, "url": runtime.URL}) + "\n").encode()
                    )
                served.append("response-sent")
            except TimeoutError:
                served.append("no-request")

        server = threading.Thread(target=serve)
        server.start()
        try:
            observed = _observe(paths)
        finally:
            server.join(timeout=3)
            listener.close()
            lease.close()
            os.close(lease_fd)
        assert observed.status == "running", (observed.diagnostic, served)
        assert served == ["response-sent"]
        assert not server.is_alive()
        assert observed.url == runtime.URL
        assert observed.diagnostic is None
        assert requests == [
            {"version": 1, "capability": instance.capability, "command": "status"}
        ]


def test_observe_runtime_rejects_lease_release_before_ready_response():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd, lease = _held_lease(paths)
        try:
            instance, listener = _tcp_control_record(paths)
        except PermissionError:
            lease.close()
            os.close(lease_fd)
            pytest.skip("sandbox does not permit local control sockets")
        released = threading.Event()
        listener.settimeout(1)

        def serve() -> None:
            nonlocal lease_fd
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                return
            with connection:
                connection.recv(4096)
                os.close(lease_fd)
                lease_fd = -1
                released.set()
                connection.sendall(
                    (json.dumps({"status": "ready", "instance_id": instance.instance_id, "url": runtime.URL}) + "\n").encode()
                )

        server = threading.Thread(target=serve)
        server.start()
        try:
            observed = _observe(paths)
        finally:
            server.join(timeout=3)
            listener.close()
            lease.close()
            if lease_fd >= 0:
                os.close(lease_fd)
        assert released.is_set(), (observed.status, observed.diagnostic)
        assert not server.is_alive()
        assert observed.status == "unknown"
        assert observed.diagnostic == runtime.RUNTIME_STATE_CHANGED


def test_observe_runtime_control_timeout_uses_one_total_second_without_retry():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd, lease = _held_lease(paths)
        try:
            _, listener = _tcp_control_record(paths)
        except PermissionError:
            lease.close()
            os.close(lease_fd)
            pytest.skip("sandbox does not permit local control sockets")
        accepted = threading.Event()

        def serve() -> None:
            connection, _ = listener.accept()
            accepted.set()
            with connection:
                time.sleep(2)

        server = threading.Thread(target=serve)
        server.start()
        started = time.monotonic()
        try:
            observed = _observe(paths)
            elapsed = time.monotonic() - started
        finally:
            listener.close()
            server.join(timeout=3)
            lease.close()
            os.close(lease_fd)
        assert accepted.is_set()
        assert observed.status == "unknown"
        assert observed.diagnostic == runtime.RUNTIME_CONTROL_TIMED_OUT
        assert elapsed < 1.5


def test_observe_runtime_rejects_record_replacement_after_ready_response():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd, lease = _held_lease(paths)
        try:
            instance, listener = _tcp_control_record(paths)
        except PermissionError:
            lease.close()
            os.close(lease_fd)
            pytest.skip("sandbox does not permit local control sockets")

        def serve() -> None:
            connection, _ = listener.accept()
            with connection:
                connection.recv(4096)
                record = paths.runtime_directory / "instance.json"
                before = record.stat()
                record.write_bytes(record.read_bytes().replace(b"instance", b"changed_"))
                os.utime(record, ns=(before.st_atime_ns, before.st_mtime_ns))
                connection.sendall(
                    (json.dumps({"status": "ready", "instance_id": instance.instance_id, "url": runtime.URL}) + "\n").encode()
                )

        server = threading.Thread(target=serve)
        server.start()
        try:
            observed = _observe(paths)
        finally:
            listener.close()
            server.join(timeout=3)
            lease.close()
            os.close(lease_fd)
        assert observed.status == "unknown"
        assert observed.diagnostic == runtime.RUNTIME_STATE_CHANGED


def test_foreign_control_holder_causes_startup_failure_without_record():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        try:
            occupant = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except PermissionError:
            pytest.skip("sandbox does not permit loopback sockets")
        occupant.bind(("127.0.0.1", 0))
        occupant.listen(1)
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        result: list[int] = []

        def bind_occupied_control() -> socket.socket:
            control = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                control.bind(occupant.getsockname())
            except BaseException:
                control.close()
                raise
            raise AssertionError("occupied endpoint unexpectedly accepted a second bind")

        try:
            with patch.object(runtime, "_paths", return_value=paths):
                daemon = runtime._Daemon(lease_fd, int((time.monotonic() + 5) * 1_000_000_000))
                with patch.object(daemon, "_bind_control", side_effect=bind_occupied_control):
                    thread = threading.Thread(target=lambda: result.append(daemon.run()))
                    thread.start()
                    thread.join(timeout=5)
                    assert not thread.is_alive()
            assert result == [26]
            assert not paths.runtime_directory.joinpath("instance.json").exists()
        finally:
            occupant.close()


def test_idle_http_connection_does_not_block_authenticated_stop():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        idle = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with patch.object(runtime, "_paths", return_value=paths):
            daemon = runtime._Daemon(lease_fd, int((time.monotonic() + 5) * 1_000_000_000))
            thread = threading.Thread(target=daemon.run)
            thread.start()
            _wait_for_record(paths)
            idle.connect(("127.0.0.1", runtime.PORT))
            assert runtime.stop() == "stopped"
            thread.join(timeout=5)
            assert not thread.is_alive()
        idle.close()


def test_close_admission_reaps_registered_direct_child():
    manager = runtime.CatalogWorkerManager(
        command_factory=lambda: [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    errors: list[runtime.WorkerError] = []

    def fetch() -> None:
        try:
            manager.fetch_catalog()
        except runtime.WorkerError as error:
            errors.append(error)

    thread = threading.Thread(target=fetch)
    thread.start()
    deadline = time.monotonic() + 2
    while manager.active_count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.active_count == 1
    assert manager.close(time.monotonic() + 2)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert [error.code for error in errors] == ["producer_cancelled"]
    assert manager.active_count == 0


def test_closed_worker_admission_spawns_no_child():
    commands: list[list[str]] = []

    def factory() -> list[str]:
        command = [sys.executable, "-c", "raise SystemExit(0)"]
        commands.append(command)
        return command

    manager = runtime.CatalogWorkerManager(command_factory=factory)
    manager.close_admission()
    with pytest.raises(runtime.WorkerError, match="producer_cancelled"):
        manager.fetch_catalog()
    assert commands == []


def test_worker_output_limit_is_enforced_incrementally():
    manager = runtime.CatalogWorkerManager(
        command_factory=lambda: [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'x' * (2**21 + 1)); sys.stdout.flush()",
        ],
        timeout=2,
    )
    started = time.monotonic()
    with pytest.raises(runtime.WorkerError, match="producer_output_too_large"):
        manager.fetch_catalog()
    assert time.monotonic() - started < 2
    assert manager.close(time.monotonic() + 2)
    assert manager.active_count == 0


def test_worker_stderr_limit_is_enforced_incrementally():
    manager = runtime.CatalogWorkerManager(
        command_factory=lambda: [
            sys.executable,
            "-c",
            "import sys; sys.stderr.buffer.write(b'x' * (2**13 + 1)); sys.stderr.flush()",
        ],
        timeout=2,
    )
    with pytest.raises(runtime.WorkerError, match="producer_output_too_large"):
        manager.fetch_catalog()
    assert manager.close(time.monotonic() + 2)
    assert manager.active_count == 0


def test_worker_timeout_has_no_additive_reap_window():
    manager = runtime.CatalogWorkerManager(
        command_factory=lambda: [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout=0.2,
    )
    started = time.monotonic()
    with pytest.raises(runtime.WorkerError, match="producer_timeout"):
        manager.fetch_catalog()
    elapsed = time.monotonic() - started
    assert elapsed < 0.8
    assert manager.close(time.monotonic() + 2)


def test_worker_collects_interleaved_flushed_streams_with_real_pipes():
    manager = runtime.CatalogWorkerManager(
        command_factory=lambda: [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stdout.buffer.write(b'one'); sys.stdout.flush(); "
                "sys.stderr.buffer.write(b'noise'); sys.stderr.flush(); "
                "sys.stdout.buffer.write(b'-two'); sys.stdout.flush(); "
                "sys.stderr.buffer.write(b'other'); sys.stderr.flush()"
            ),
        ],
        timeout=2,
    )

    assert manager.fetch_catalog() == b"one-two"
    assert manager.close(time.monotonic() + 2)


def test_worker_timeout_after_prefix_stall_finishes_autonomously():
    manager = runtime.CatalogWorkerManager(
        command_factory=lambda: [
            sys.executable,
            "-c",
            "import sys,time; sys.stdout.write('prefix'); sys.stdout.flush(); time.sleep(30)",
        ],
        timeout=0.2,
    )
    started = time.monotonic()
    with pytest.raises(runtime.WorkerError, match="producer_timeout"):
        manager.fetch_catalog()
    assert time.monotonic() - started < 0.8
    assert manager.close(time.monotonic() + 2)
    assert manager.active_count == 0


def test_worker_timeout_finalizer_reaps_without_manager_close():
    manager = runtime.CatalogWorkerManager(
        command_factory=lambda: [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout=0.2,
    )
    with pytest.raises(runtime.WorkerError, match="producer_timeout"):
        manager.fetch_catalog()

    deadline = time.monotonic() + 2
    while manager.active_count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.active_count == 0


def test_worker_retains_no_bytes_beyond_stream_caps_and_completes_readers():
    manager = runtime.CatalogWorkerManager(
        command_factory=lambda: [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stdout.buffer.write(b'x' * (2**21 + 1)); sys.stdout.flush(); "
                "sys.stderr.buffer.write(b'y' * (2**13 + 1)); sys.stderr.flush()"
            ),
        ],
        timeout=2,
    )
    finalized: list[object] = []
    original_start = manager._start_finalizer_locked

    def observe_finalizer(child: object) -> None:
        finalized.append(child)
        original_start(child)  # type: ignore[arg-type]

    with patch.object(manager, "_start_finalizer_locked", side_effect=observe_finalizer):
        with pytest.raises(runtime.WorkerError, match="producer_output_too_large"):
            manager.fetch_catalog()
    deadline = time.monotonic() + 2
    while manager.active_count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.active_count == 0
    child = finalized[0]
    assert child.retained_bytes["stdout"] <= worker.MAX_STDOUT_BYTES  # type: ignore[attr-defined]
    assert child.retained_bytes["stderr"] <= worker.MAX_STDERR_BYTES  # type: ignore[attr-defined]
    assert child.readers_complete.is_set()  # type: ignore[attr-defined]


def test_worker_maps_eof_before_nonzero_exit_and_reaps_child():
    for exit_code, expected in ((4, "producer_unavailable"), (7, "producer_failed")):
        manager = runtime.CatalogWorkerManager(
            command_factory=lambda exit_code=exit_code: [
                sys.executable,
                "-c",
                f"import sys; sys.stdout.close(); sys.stderr.close(); sys.exit({exit_code})",
            ],
            timeout=2,
        )
        with pytest.raises(runtime.WorkerError, match=expected):
            manager.fetch_catalog()
        assert manager.close(time.monotonic() + 2)
        assert manager.active_count == 0


def test_worker_cancellation_after_eof_wins_before_success_publication():
    with TemporaryDirectory() as temporary:
        gate = Path(temporary) / "release"
        manager = runtime.CatalogWorkerManager(
            command_factory=lambda: [
                sys.executable,
                "-c",
                (
                    "import os,time,sys\n"
                    "os.close(1); os.close(2)\n"
                    f"gate={str(gate)!r}\n"
                    "while not os.path.exists(gate):\n"
                    "    time.sleep(.01)\n"
                ),
            ],
            timeout=2,
        )
        errors: list[runtime.WorkerError] = []
        thread = threading.Thread(target=lambda: _capture_worker_error(manager, errors), daemon=True)
        thread.start()
        deadline = time.monotonic() + 2
        child = None
        while child is None and time.monotonic() < deadline:
            with manager._lock:
                if manager._children:
                    child = manager._children[0]
            time.sleep(0.01)
        assert child is not None
        while not child.readers_complete.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child.readers_complete.is_set()
        assert manager.close(time.monotonic() + 2)
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert [error.code for error in errors] == ["producer_cancelled"]


class _PortableProcessControl:
    def __init__(self, process: subprocess.Popen[bytes], *, resistant: bool) -> None:
        self._process = process
        self._resistant = resistant

    def terminate(self) -> None:
        if not self._resistant:
            self._process.terminate()

    def __getattr__(self, name: str) -> object:
        return getattr(self._process, name)


def test_worker_resistant_child_keeps_manager_ownership_after_shared_deadline():
    real_popen = subprocess.Popen

    def portable_popen(*args: object, **kwargs: object) -> _PortableProcessControl:
        return _PortableProcessControl(real_popen(*args, **kwargs), resistant=True)

    manager = runtime.CatalogWorkerManager(
        command_factory=lambda: [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ],
        timeout=30,
    )
    errors: list[runtime.WorkerError] = []
    thread = threading.Thread(
        target=lambda: _capture_worker_error(manager, errors),
        daemon=True,
    )
    with patch.object(worker.subprocess, "Popen", side_effect=portable_popen):
        thread.start()
        deadline = time.monotonic() + 2
        child = None
        while time.monotonic() < deadline:
            with manager._lock:
                if manager._children:
                    child = manager._children[0]
                    break
            time.sleep(0.01)
        assert child is not None
        assert manager.active_count == 1
        assert manager.close(time.monotonic() + 0.2) is False
        assert manager.active_count == 1
        child.process.kill()
        assert manager.close(time.monotonic() + 2)
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert [error.code for error in errors] == ["producer_cancelled"]


def test_worker_shared_shutdown_reaps_cooperative_child_with_portable_resistant_facade():
    real_popen = subprocess.Popen
    process_calls = 0
    process_lock = threading.Lock()
    command_calls = 0

    def portable_popen(*args: object, **kwargs: object) -> _PortableProcessControl:
        nonlocal process_calls
        with process_lock:
            resistant = process_calls == 0
            process_calls += 1
        return _PortableProcessControl(
            real_popen(*args, **kwargs),
            resistant=resistant,
        )

    def command_factory() -> list[str]:
        nonlocal command_calls
        with process_lock:
            resistant = command_calls == 0
            command_calls += 1
        if resistant:
            return [sys.executable, "-c", "import time; time.sleep(30)"]
        return [
            sys.executable,
            "-c",
            "import sys,time; sys.stdout.write('cooperative'); sys.stdout.flush(); time.sleep(.05)",
        ]

    manager = runtime.CatalogWorkerManager(command_factory=command_factory, timeout=30)
    errors: list[runtime.WorkerError] = []
    results: list[bytes] = []

    def fetch() -> None:
        try:
            results.append(manager.fetch_catalog())
        except runtime.WorkerError as error:
            errors.append(error)

    with patch.object(worker.subprocess, "Popen", side_effect=portable_popen):
        resistant_thread = threading.Thread(target=fetch, daemon=True)
        resistant_thread.start()
        deadline = time.monotonic() + 2
        while not manager._children and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(manager._children) == 1

        cooperative_thread = threading.Thread(target=fetch, daemon=True)
        cooperative_thread.start()
        cooperative_thread.join(timeout=2)
        assert not cooperative_thread.is_alive()
        assert results == [b"cooperative"]

        assert manager.close(time.monotonic() + 0.2) is False
        assert manager.active_count == 1

        resistant_child = manager._children[0]
        resistant_child.process.kill()
        assert manager.close(time.monotonic() + 2)
        resistant_thread.join(timeout=2)

    assert not resistant_thread.is_alive()
    assert [error.code for error in errors] == ["producer_cancelled"]
    assert manager.active_count == 0


def _capture_worker_error(
    manager: runtime.CatalogWorkerManager, errors: list[runtime.WorkerError]
) -> None:
    try:
        manager.fetch_catalog()
    except runtime.WorkerError as error:
        errors.append(error)


def test_worker_source_has_no_selector_or_nonblocking_pipe_path():
    source = Path(worker.__file__).read_text(encoding="utf-8")
    assert "selectors" not in source
    assert "os.set_blocking" not in source
    assert "os.read" not in source
    assert "communicate" not in source


def test_stop_during_spawn_reaps_child_registered_after_admission_closes():
    with TemporaryDirectory():
        entered = threading.Event()
        release = threading.Event()

        def factory() -> list[str]:
            entered.set()
            assert release.wait(timeout=3)
            return [sys.executable, "-c", "import time; time.sleep(30)"]

        manager = runtime.CatalogWorkerManager(command_factory=factory)
        errors: list[runtime.WorkerError] = []

        def fetch() -> None:
            try:
                manager.fetch_catalog()
            except runtime.WorkerError as error:
                errors.append(error)

        fetch_thread = threading.Thread(target=fetch)
        fetch_thread.start()
        assert entered.wait(timeout=2)
        close_result: list[bool] = []
        close_thread = threading.Thread(
            target=lambda: close_result.append(manager.close(time.monotonic() + 3))
        )
        close_thread.start()
        time.sleep(0.05)
        release.set()
        fetch_thread.join(timeout=4)
        close_thread.join(timeout=4)
        assert not fetch_thread.is_alive()
        assert not close_thread.is_alive()
        assert close_result == [True]
        assert [error.code for error in errors] == ["producer_cancelled"]
        assert manager.active_count == 0


def test_simultaneous_start_processes_share_one_authenticated_instance():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        specification_root = root / "spec"
        specification_root.mkdir()
        site_directory = root / "site"
        environment = _subprocess_environment(home, site_directory)
        with patch.object(state, "resolve_account_home", return_value=home):
            state.setup(specification_root)
        first = second = None
        try:
            command = (
                "from nyx import runtime; runtime.STARTUP_TIMEOUT=15; "
                "print(runtime.start(), flush=True); "
                "print(runtime._read_instance(runtime._paths()).instance_id, flush=True)"
            )
            first = subprocess.Popen(
                [sys.executable, "-c", command],
                cwd=Path(__file__).parents[1],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            second = subprocess.Popen(
                [sys.executable, "-c", command],
                cwd=Path(__file__).parents[1],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            first_stdout, first_stderr = first.communicate(timeout=25)
            second_stdout, second_stderr = second.communicate(timeout=25)
            assert first.returncode == 0, first_stderr
            assert second.returncode == 0, second_stderr
            first_lines = first_stdout.splitlines()
            second_lines = second_stdout.splitlines()
            assert first_lines[0] == runtime.URL
            assert second_lines[0] == runtime.URL
            assert first_lines[1] == second_lines[1]
        finally:
            subprocess.run(
                [sys.executable, "-c", "from nyx import runtime; runtime.stop()"],
                cwd=Path(__file__).parents[1],
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            for process in (first, second):
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)


def test_actual_daemon_spawn_retains_lease_after_launcher_death():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        specification_root = root / "spec"
        specification_root.mkdir()
        site_directory = root / "site"
        environment = _subprocess_environment(home, site_directory)
        with patch.object(state, "resolve_account_home", return_value=home):
            state.setup(specification_root)
            paths = state.state_paths()
        script = (
            "import os,time; from nyx import runtime; paths=runtime._paths(create=True); "
            "lease=runtime._lease_lock(paths,timeout=0); assert lease.acquire(blocking=False); "
            "runtime._spawn_daemon(lease.fd,int((time.monotonic()+5)*10**9)); os._exit(0)"
        )
        launcher = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert launcher.returncode == 0, launcher.stderr
        _wait_for_record(paths)
        probe = runtime._lease_lock(paths, timeout=0.0)
        assert not probe.acquire(blocking=False)
        probe.close()
        with patch.object(runtime, "_paths", return_value=paths):
            assert runtime.stop() == "stopped"


def test_public_start_daemon_survives_launcher_exit_after_acknowledgement():
    try:
        capability_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except PermissionError:
        pytest.skip("sandbox does not permit loopback sockets")
    else:
        capability_probe.close()

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        specification_root = root / "spec"
        specification_root.mkdir()
        site_directory = root / "site"
        environment = _subprocess_environment(home, site_directory)
        with patch.object(state, "resolve_account_home", return_value=home):
            state.setup(specification_root)
            paths = state.state_paths()
        launcher = subprocess.run(
            [sys.executable, "-c", "from nyx import runtime; print(runtime.start())"],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        try:
            assert launcher.returncode == 0, launcher.stderr
            assert launcher.stdout.strip() == runtime.URL
            _wait_for_record(paths)
            contender = runtime._lease_lock(paths, timeout=0.0)
            assert not contender.acquire(blocking=False)
            contender.close()
        finally:
            with patch.object(runtime, "_paths", return_value=paths):
                runtime.stop()


def test_external_launcher_death_before_ack_keeps_inherited_claim_until_child_exit():
    child = (
        "import os,sys,time; from pathlib import Path; from nyx._native_claim import NativeClaim; "
        "fd=(NativeClaim.receive_handle(int(sys.argv[sys.argv.index('--daemon-handle')+1])) "
        "if '--daemon-handle' in sys.argv else int(sys.argv[sys.argv.index('--daemon-fd')+1])); "
        "ack=(NativeClaim.receive_handle(int(sys.argv[sys.argv.index('--ack-handle')+1]),write_only=True) "
        "if '--ack-handle' in sys.argv else int(sys.argv[sys.argv.index('--ack-fd')+1])); "
        "path=Path(sys.argv[sys.argv.index('--claim-path')+1]); "
        "time.sleep(.4); valid=NativeClaim.validate_received(fd,path); "
        "exec(\"try:\\n os.write(ack,b'1' if valid else b'0')\\nexcept OSError:\\n pass\"); "
        "os.close(ack); os.close(fd)"
    )
    launcher_code = (
        "import os,sys,time; from pathlib import Path; from nyx import runtime; "
        "paths=runtime._paths(create=True); lease=runtime._lease_lock(paths,timeout=0); "
        "assert lease.acquire(blocking=False); read_fd,write_fd=os.pipe(); "
        f"runtime._daemon_command=lambda deadline:[sys.executable,'-c',{child!r}]; "
        "process=runtime._spawn_daemon(lease.fd,time.monotonic_ns()+5*10**9,"
        "ack_fd=write_fd,claim_path=paths.runtime_directory/'lease.lock'); "
        "Path(sys.argv[1]).write_text(str(process.pid),encoding='utf-8'); time.sleep(30)"
    )
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        specification = root / "spec"
        specification.mkdir()
        site_directory = root / "site"
        environment = _subprocess_environment(home, site_directory)
        with patch.object(state, "resolve_account_home", return_value=home):
            state.setup(specification)
            paths = state.state_paths()
        marker = root / "spawned"
        launcher = subprocess.Popen(
            [sys.executable, "-c", launcher_code, str(marker)],
            cwd=Path(__file__).parents[1],
            env=environment,
        )
        try:
            deadline = time.monotonic() + 3
            while not marker.exists() and launcher.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.exists()
            launcher.kill()
            launcher.wait(timeout=2)
            contender = runtime._lease_lock(paths, timeout=0.0)
            assert not contender.acquire(blocking=False)
            contender.close()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                contender = runtime._lease_lock(paths, timeout=0.0)
                if contender.acquire(blocking=False):
                    contender.close()
                    break
                contender.close()
                time.sleep(0.02)
            else:
                raise AssertionError("inherited claim did not release after failed acknowledgement")
        finally:
            if launcher.poll() is None:
                launcher.kill()
                launcher.wait(timeout=2)


def test_actual_launcher_death_before_spawn_releases_lease():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        specification_root = root / "spec"
        specification_root.mkdir()
        site_directory = root / "site"
        environment = _subprocess_environment(home, site_directory)
        with patch.object(state, "resolve_account_home", return_value=home):
            state.setup(specification_root)
            paths = state.state_paths()
        script = (
            "from nyx import runtime; import os; paths=runtime._paths(create=True); "
            "lease=runtime._lease_lock(paths,timeout=0); assert lease.acquire(blocking=False); os._exit(0)"
        )
        launcher = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert launcher.returncode == 0, launcher.stderr
        probe = runtime._lease_lock(paths, timeout=0.0)
        assert probe.acquire(blocking=False)
        probe.close()


def test_separate_processes_serialize_on_the_persistent_operation_lock():
    with TemporaryDirectory() as temporary:
        lock_path = Path(temporary) / "operation.lock"
        script = (
            "import sys,time; from nyx.runtime import _FileLock; "
            "lock=_FileLock(__import__('pathlib').Path(sys.argv[1]), timeout=2); "
            "deadline=time.monotonic()+2; acquired=lock.acquire(blocking=False); "
            "exec(\"while not acquired and time.monotonic()<deadline:\\n time.sleep(.01); acquired=lock.acquire(blocking=False)\"); "
            "assert acquired; open(sys.argv[2], 'a').write('acquired\\n'); "
            "time.sleep(.35); lock.close()"
        )
        events = Path(temporary) / "events"
        started = time.monotonic()
        first = subprocess.Popen([sys.executable, "-c", script, str(lock_path), str(events)])
        second = subprocess.Popen([sys.executable, "-c", script, str(lock_path), str(events)])
        assert first.wait(timeout=3) == 0
        assert second.wait(timeout=3) == 0
        assert time.monotonic() - started >= 0.6
        assert events.read_text(encoding="utf-8").splitlines() == ["acquired", "acquired"]


def test_launcher_loss_after_spawn_leaves_transferred_lease_until_child_exit():
    with TemporaryDirectory() as temporary:
        lock_path = Path(temporary) / "lease.lock"
        child = (
            "import os,sys,time; from nyx._native_claim import NativeClaim; "
            "fd=(NativeClaim.receive_handle(int(sys.argv[sys.argv.index('--daemon-handle')+1])) "
            "if '--daemon-handle' in sys.argv else int(sys.argv[sys.argv.index('--daemon-fd')+1])); "
            "time.sleep(.8); os.close(fd)"
        )
        script = (
            "import os,sys,time; from pathlib import Path; from nyx import runtime; "
            "lease=runtime._FileLock(Path(sys.argv[1]),timeout=0); assert lease.acquire(blocking=False); "
            f"runtime._daemon_command=lambda deadline:[sys.executable,'-c',{child!r}]; "
            "runtime._spawn_daemon(lease.fd,time.monotonic_ns()+5*10**9); lease.close(); os._exit(0)"
        )
        launcher = subprocess.Popen([sys.executable, "-c", script, str(lock_path)])
        assert launcher.wait(timeout=2) == 0
        probe = runtime._FileLock(lock_path, timeout=0.0)
        assert not probe.acquire(blocking=False)
        time.sleep(1)
        assert probe.acquire(blocking=False)
        probe.close()


def test_launcher_loss_before_spawn_releases_untransferred_lease():
    with TemporaryDirectory() as temporary:
        lock_path = Path(temporary) / "lease.lock"
        script = (
            "import os,sys; from pathlib import Path; from nyx import runtime; "
            "lease=runtime._FileLock(Path(sys.argv[1]),timeout=0); "
            "assert lease.acquire(blocking=False); os._exit(0)"
        )
        launcher = subprocess.Popen([sys.executable, "-c", script, str(lock_path)])
        assert launcher.wait(timeout=2) == 0
        probe = runtime._FileLock(lock_path, timeout=0.0)
        assert probe.acquire(blocking=False)
        probe.close()


@pytest.mark.parametrize("phase", ["before_spawn", "after_spawn_before_ack", "after_ack"])
def test_continuous_external_contender_covers_launcher_loss_phase(phase):
    child = (
        "import os,sys,time\n"
        "from pathlib import Path\n"
        "from nyx._native_claim import NativeClaim\n"
        "phase,marker,release=sys.argv[1:4]\n"
        "if '--daemon-handle' in sys.argv:\n"
        " fd=NativeClaim.receive_handle(int(sys.argv[sys.argv.index('--daemon-handle')+1]))\n"
        "else:\n"
        " fd=int(sys.argv[sys.argv.index('--daemon-fd')+1])\n"
        "if '--ack-handle' in sys.argv:\n"
        " ack=NativeClaim.receive_handle(int(sys.argv[sys.argv.index('--ack-handle')+1]),write_only=True)\n"
        "elif '--ack-fd' in sys.argv:\n"
        " ack=int(sys.argv[sys.argv.index('--ack-fd')+1])\n"
        "else:\n"
        " ack=None\n"
        "Path(marker).write_text('spawned',encoding='utf-8')\n"
        "if phase == 'after_spawn_before_ack':\n"
        " time.sleep(.5)\n"
        "if ack is not None:\n"
        " os.write(ack,b'1')\n"
        " os.close(ack)\n"
        " Path(marker).write_text('ack',encoding='utf-8')\n"
        "while not Path(release).exists():\n"
        " time.sleep(.01)\n"
        "os.close(fd)\n"
    )
    launcher = (
        "import os,sys,time\n"
        "from pathlib import Path\n"
        "from nyx import runtime\n"
        "lock_path,phase,marker,release=sys.argv[1:5]\n"
        "lease=runtime._FileLock(Path(lock_path),timeout=0)\n"
        "assert lease.acquire(blocking=False)\n"
        "if phase == 'before_spawn':\n"
        " Path(marker).write_text('held',encoding='utf-8')\n"
        " time.sleep(30)\n"
        "else:\n"
        " read_fd,write_fd=os.pipe()\n"
        f" runtime._daemon_command=lambda deadline:[sys.executable,'-c',{child!r},phase,marker,release]\n"
        " runtime._spawn_daemon(lease.fd,time.monotonic_ns()+10**10,ack_fd=write_fd,claim_path=Path(lock_path))\n"
        " os.close(write_fd)\n"
        " if phase == 'after_ack':\n"
        "  os.read(read_fd,1)\n"
        "  Path(marker).write_text('ack',encoding='utf-8')\n"
        " os.close(read_fd)\n"
        " time.sleep(30)\n"
    )
    contender = (
        "import sys,time\n"
        "from pathlib import Path\n"
        "from nyx.runtime import _FileLock\n"
        "lock_path,acquired,stop=sys.argv[1:4]\n"
        "while not Path(stop).exists():\n"
        " lock=_FileLock(Path(lock_path),timeout=0)\n"
        " if lock.acquire(blocking=False):\n"
        "  Path(acquired).write_text('acquired',encoding='utf-8')\n"
        "  while not Path(stop).exists():\n"
        "   time.sleep(.01)\n"
        "  lock.close()\n"
        "  break\n"
        " lock.close()\n"
        " time.sleep(.005)\n"
    )
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        lock_path = root / "lease.lock"
        marker = root / "phase"
        release = root / "release"
        acquired = root / "acquired"
        stop = root / "stop"
        launcher_process = subprocess.Popen(
            [sys.executable, "-c", launcher, str(lock_path), phase, str(marker), str(release)],
            cwd=Path(__file__).parents[1],
        )
        contender_process = None
        try:
            deadline = time.monotonic() + 3
            while not marker.exists() and launcher_process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.exists(), phase
            contender_process = subprocess.Popen(
                [sys.executable, "-c", contender, str(lock_path), str(acquired), str(stop)],
                cwd=Path(__file__).parents[1],
            )
            time.sleep(0.1)
            launcher_process.kill()
            assert launcher_process.wait(timeout=2) != 0
            if phase == "before_spawn":
                deadline = time.monotonic() + 2
                while not acquired.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert acquired.exists()
            else:
                time.sleep(0.2)
                assert not acquired.exists(), phase
                release.touch()
                deadline = time.monotonic() + 2
                while not acquired.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert acquired.exists(), phase
        finally:
            release.touch()
            stop.touch()
            if contender_process is not None:
                contender_process.wait(timeout=3)
            if launcher_process.poll() is None:
                launcher_process.kill()
                launcher_process.wait(timeout=2)


@pytest.mark.skipif(os.name == "nt", reason="POSIX path replacement is required")
def test_parent_received_claim_validation_rejects_substituted_path_reopen():
    child = (
        "import os,sys,time\n"
        "from pathlib import Path\n"
        "from nyx._native_claim import NativeClaim\n"
        "marker,release=sys.argv[1:3]\n"
        "fd=int(sys.argv[sys.argv.index('--daemon-fd')+1])\n"
        "ack=int(sys.argv[sys.argv.index('--ack-fd')+1])\n"
        "path=Path(sys.argv[sys.argv.index('--claim-path')+1])\n"
        "Path(marker).write_text('ready',encoding='utf-8')\n"
        "while not Path(release).exists():\n"
        " time.sleep(.01)\n"
        "valid=NativeClaim.validate_received(fd,path)\n"
        "os.write(ack,b'1' if valid else b'0')\n"
        "os.close(ack)\n"
        "os.close(fd)\n"
    )
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        paths, _, _ = _fixture(root)
        marker = root / "child-ready"
        release = root / "release"
        errors: list[BaseException] = []

        def start() -> None:
            try:
                with patch.object(runtime, "_paths", return_value=paths), patch.object(
                    runtime,
                    "_daemon_command",
                    return_value=[sys.executable, "-c", child, str(marker), str(release)],
                ):
                    runtime.start()
            except BaseException as error:  # noqa: BLE001 - asserted below
                errors.append(error)

        thread = threading.Thread(target=start)
        thread.start()
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        claim_path = paths.runtime_directory / "lease.lock"
        replacement = root / "replacement-lease.lock"
        replacement.write_bytes(b"\0")
        os.replace(replacement, claim_path)
        release.touch()
        thread.join(timeout=4)
        assert not thread.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], runtime.StartupError)
        _assert_claim_available(claim_path)


def test_natural_crashed_daemon_leaves_record_for_free_lease_cleanup():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        script = (
            "import os,sys,time; from pathlib import Path; from nyx import runtime,state; "
            "home=Path(sys.argv[1]); root=home/'.nyx'; paths=state.StatePaths(home,root/'config',root/'config/config.json',"
            "root,root/'runtime/deployment.json',root/'runtime'); "
            "runtime._paths=lambda **kwargs: paths; lease=runtime._lease_lock(paths,timeout=0); "
            "assert lease.acquire(blocking=False); runtime._Daemon(lease.fd,time.monotonic_ns()+5*10**9).start()"
        )
        crashed = subprocess.Popen([sys.executable, "-c", script, str(paths.account_home)])
        try:
            _wait_for_record(paths)
            crashed.kill()
            assert crashed.wait(timeout=2) != 0
            with patch.object(runtime, "_paths", return_value=paths):
                assert runtime.stop() == "stopped"
            assert not paths.runtime_directory.joinpath("instance.json").exists()
        finally:
            if crashed.poll() is None:
                crashed.kill()
                crashed.wait()


def test_public_stop_timeout_retains_authenticated_cleanup_until_retry():
    try:
        capability_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        capability_probe.bind(("127.0.0.1", 0))
    except PermissionError:
        pytest.skip("sandbox does not permit loopback sockets")
    else:
        capability_probe.close()
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        second_root = Path(temporary) / "second-specification"
        second_root.mkdir()
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        daemon = None
        daemon_thread = None
        worker_child = None
        try:
            with patch.object(
                state, "resolve_account_home", return_value=paths.account_home
            ), patch.object(runtime, "_paths", return_value=paths), patch.object(
                runtime, "SHUTDOWN_TIMEOUT", 0.25
            ):
                daemon = runtime._Daemon(
                    lease_fd, int((time.monotonic() + 5) * 1_000_000_000)
                )
                daemon.workers = runtime.CatalogWorkerManager(
                    command_factory=lambda: [
                        sys.executable,
                        "-c",
                        "import signal,time; "
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        "time.sleep(30)",
                    ],
                    timeout=30,
                )
                daemon_thread = threading.Thread(target=daemon.run)
                daemon_thread.start()
                instance = _wait_for_record(paths)
                record_before = paths.runtime_directory.joinpath("instance.json").read_bytes()
                record_metadata_before = runtime._record_snapshot(
                    paths.runtime_directory.joinpath("instance.json")
                )

                request_done: list[object] = []

                def request_catalog() -> None:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", runtime.PORT, timeout=10
                    )
                    try:
                        connection.request(
                            "GET", "/api/catalog", headers={"Host": f"127.0.0.1:{runtime.PORT}"}
                        )
                        request_done.append(connection.getresponse().status)
                    except OSError:
                        request_done.append(None)
                    finally:
                        connection.close()

                request_thread = threading.Thread(target=request_catalog)
                request_thread.start()
                deadline = time.monotonic() + 2
                while daemon.workers.active_count == 0 and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert daemon.workers.active_count == 1
                with daemon.workers._lock:
                    worker_child = daemon.workers._children[0]

                stop_errors: list[BaseException] = []

                def request_stop() -> None:
                    try:
                        runtime.stop()
                    except BaseException as error:  # noqa: BLE001 - assert public error below
                        stop_errors.append(error)

                stop_thread = threading.Thread(target=request_stop)
                stop_thread.start()
                assert daemon.stop_requested.wait(timeout=1)
                status_deadline = time.monotonic() + 1
                while time.monotonic() < status_deadline:
                    cleanup_status = runtime.observe_runtime()
                    if cleanup_status.diagnostic == runtime.RUNTIME_UNHEALTHY:
                        break
                    time.sleep(0.01)
                else:
                    raise AssertionError("public status did not authenticate cleanup in progress")
                assert cleanup_status.status == "unknown"
                stop_thread.join(timeout=2)
                assert not stop_thread.is_alive()
                assert len(stop_errors) == 1
                assert isinstance(stop_errors[0], runtime.ShutdownTimeoutError)
                assert paths.runtime_directory.joinpath("instance.json").read_bytes() == record_before
                assert runtime._record_snapshot(
                    paths.runtime_directory.joinpath("instance.json")
                ) == record_metadata_before

                timed_out_status = runtime.observe_runtime()
                assert timed_out_status.status == "unknown"
                assert timed_out_status.diagnostic == runtime.RUNTIME_UNHEALTHY

                configuration_before = paths.config_file.read_bytes()
                with pytest.raises(runtime.UnhealthyInstanceError):
                    runtime.start()
                with pytest.raises(runtime.ActiveInstanceError):
                    runtime.setup(second_root)
                assert paths.config_file.read_bytes() == configuration_before
                assert paths.runtime_directory.joinpath("instance.json").read_bytes() == record_before
                probe = runtime._lease_lock(paths, timeout=0.0)
                assert not probe.acquire(blocking=False)
                probe.close()

                # Release only the real worker obstacle; the public stop below
                # must perform the retry and terminal cleanup.
                worker_child.process.kill()
                request_thread.join(timeout=3)
                assert not request_thread.is_alive()
                assert runtime.stop() == "stopped"
                assert not paths.runtime_directory.joinpath("instance.json").exists()
                _assert_claim_available(paths.runtime_directory / "lease.lock")
                daemon_thread.join(timeout=3)
                assert not daemon_thread.is_alive()
                with pytest.raises(runtime.UnhealthyInstanceError):
                    runtime._send_control(instance, "status")
                assert runtime.observe_runtime().status == "not_running"
        finally:
            if worker_child is not None and worker_child.process.poll() is None:
                worker_child.process.kill()
            if daemon is not None and daemon_thread is not None and daemon_thread.is_alive():
                try:
                    with patch.object(runtime, "_paths", return_value=paths), patch.object(
                        runtime, "SHUTDOWN_TIMEOUT", 1.0
                    ):
                        runtime.stop()
                except runtime.RuntimeErrorBase:
                    pass
                daemon_thread.join(timeout=3)
            try:
                os.close(lease_fd)
            except OSError:
                pass


def test_public_stop_publishes_unhealthy_before_competing_start_handoff():
    stop_command = threading.Event()
    acknowledgement_sent = threading.Event()
    allow_acknowledgement_return = threading.Event()
    control_closed = threading.Event()
    shutdown_started = threading.Event()
    operation_gate = threading.Lock()

    class Operation:
        def __init__(self):
            self.acquired = False

        def acquire(self, *, blocking=True):
            self.acquired = operation_gate.acquire(blocking=blocking)
            return self.acquired

        def close(self):
            if self.acquired:
                self.acquired = False
                operation_gate.release()

        def __enter__(self):
            assert self.acquire()
            return self

        def __exit__(self, *_args):
            self.close()

    class HeldLease:
        def acquire(self, *, blocking=True):
            return False

        def close(self):
            return None

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, _timeout):
            return None

        def recv(self, _limit):
            return (
                json.dumps(
                    {
                        "version": 1,
                        "capability": "capability",
                        "command": "stop",
                        "deadline_ns": time.monotonic_ns() + 1_000_000_000,
                    }
                )
                + "\n"
            ).encode()

        def sendall(self, data):
            response = json.loads(data.splitlines()[0])
            assert response["status"] == "stopping"
            acknowledgement_sent.set()
            assert allow_acknowledgement_return.wait(timeout=2)

    class Control:
        def __init__(self):
            self.accepted = False

        def accept(self):
            if not self.accepted:
                assert stop_command.wait(timeout=2)
                self.accepted = True
                return Connection(), None
            if control_closed.wait(timeout=0.05):
                raise OSError("test control closed")
            raise TimeoutError

        def close(self):
            control_closed.set()

    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        record = paths.runtime_directory / "instance.json"
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        daemon = None
        control_thread = None
        stop_thread = None
        try:
            with patch.object(runtime, "_paths", return_value=paths):
                daemon = runtime._Daemon(lease_fd, time.monotonic_ns() + 5_000_000_000)
            daemon.instance = runtime.Instance(
                instance_id="instance",
                url=runtime.URL,
                capability="capability",
                control=runtime._control_endpoint(1),
            )
            daemon.published_record = runtime._write_instance(
                paths, daemon.instance, deadline=time.monotonic() + 1
            )
            daemon.control = Control()

            def send_control(instance, command, **_kwargs):
                assert instance == daemon.instance
                if command == "stop":
                    stop_command.set()
                    assert acknowledgement_sent.wait(timeout=2)
                    return {
                        "status": "stopping",
                        "instance_id": instance.instance_id,
                        "url": runtime.URL,
                    }
                assert command == "status"
                return {
                    "status": "unhealthy" if daemon.stop_requested.is_set() else "ready",
                    "instance_id": instance.instance_id,
                    "url": runtime.URL,
                }

            stop_errors: list[BaseException] = []

            def public_stop():
                try:
                    runtime.stop()
                except BaseException as error:  # noqa: BLE001 - asserted below
                    stop_errors.append(error)

            def retained_shutdown(_deadline):
                shutdown_started.set()
                return "timeout"

            # Exercise the public lifecycle functions and real persisted record
            # while an in-memory claim/transport fixture exposes the exact
            # acknowledgement handoff without requiring a loopback socket.
            with patch.object(runtime, "_paths", return_value=paths), patch.object(
                runtime, "_send_control", side_effect=send_control
            ), patch.object(
                runtime, "_operation_lock", side_effect=lambda *_args, **_kwargs: Operation()
            ), patch.object(
                runtime, "_lease_lock", side_effect=lambda *_args, **_kwargs: HeldLease()
            ), patch.object(runtime, "SHUTDOWN_TIMEOUT", 0.2), patch.object(
                daemon, "shutdown", side_effect=retained_shutdown
            ):
                control_thread = threading.Thread(target=daemon._serve_control)
                control_thread.start()
                stop_thread = threading.Thread(target=public_stop)
                stop_thread.start()
                assert acknowledgement_sent.wait(timeout=2), (
                    stop_thread.is_alive(),
                    stop_errors,
                    record.exists(),
                )
                assert not shutdown_started.is_set()
                with pytest.raises(runtime.UnhealthyInstanceError):
                    runtime.start()
                assert daemon.stop_requested.is_set()
                assert record.exists()
                allow_acknowledgement_return.set()
                assert shutdown_started.wait(timeout=2)
                stop_thread.join(timeout=2)
                assert not stop_thread.is_alive()
                assert len(stop_errors) == 1
                assert isinstance(stop_errors[0], runtime.ShutdownTimeoutError)
        finally:
            allow_acknowledgement_return.set()
            control_closed.set()
            if stop_thread is not None:
                stop_thread.join(timeout=2)
            if control_thread is not None:
                control_thread.join(timeout=2)
            try:
                os.close(lease_fd)
            except OSError:
                pass
