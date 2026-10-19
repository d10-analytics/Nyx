"""Behavioral checks for the authenticated process lifecycle."""

from __future__ import annotations

import errno
import http.client
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from conftest import (
    LOOPBACK_DENIED_REASON,
    SERVICE_PORT_HELD_REASON,
    _free_loopback_port,
    managed_daemon_run,
    skip_if_service_port_is_held,
)
from launch_shadow import plant_shadow, proof_environment, shadow_imports

from nyx import _native_claim, catalog, runtime, state, worker
from nyx.models import SCHEMA_VERSION, canonical_digest

pytestmark = pytest.mark.usefixtures("isolated_port")

# Children launched by these tests adopt the parent's port through the runtime's
# own formula.  A direct ``nyx.runtime`` launch is left alone: the daemon takes
# its port from ``--port``, and importing the module here would load it twice.
_PORT_SITECUSTOMIZE = (
    "import os as _nyx_os, sys as _nyx_sys\n"
    "if 'nyx.runtime' not in _nyx_sys.orig_argv:\n"
    "    from nyx import runtime as _nyx_runtime\n"
    "    _nyx_runtime._use_port(int(_nyx_os.environ['NYX_TEST_PORT']))\n"
)


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


def test_application_runtime_provider_admits_schema_six_and_rejects_schema_four():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        package = root / "Fictional" / "Queue" / "entry"
        package.mkdir(parents=True)
        package.joinpath("spec.md").write_text("# Entry\n", encoding="utf-8")
        payload = catalog.scan_catalog(root)
        application = runtime.ApplicationRuntime(
            port=runtime.PORT, deadline=time.monotonic() + 5
        )
        application.catalog_admitted = True

        class Workers:
            def __init__(self, value: bytes):
                self.value = value

            def fetch_catalog(self) -> bytes:
                return self.value

        application.workers = Workers(payload.encode("utf-8"))
        admitted = application._provider()
        assert admitted.schema_version == SCHEMA_VERSION
        assert admitted.configuration_revision is None

        legacy = json.loads(payload)
        legacy["schema_version"] = 4
        legacy["catalog_digest"] = canonical_digest(legacy)
        application.workers = Workers(json.dumps(legacy).encode("utf-8"))
        with pytest.raises(runtime.CatalogError, match="producer_protocol_error"):
            application._provider()


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


def _subprocess_environment(
    home: Path, site_directory: Path, *, extra_sitecustomize: str = ""
) -> dict[str, str]:
    site_directory.mkdir()
    (site_directory / "sitecustomize.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "from nyx import state\n"
        "state.resolve_account_home = lambda: Path(os.environ['NYX_TEST_HOME'])\n"
        + _PORT_SITECUSTOMIZE
        + extra_sitecustomize,
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["NYX_TEST_HOME"] = str(home)
    environment["NYX_TEST_PORT"] = str(runtime.PORT)
    environment["PYTHONPATH"] = str(site_directory) + os.pathsep + environment.get("PYTHONPATH", "")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _wait_for_process_exit(pid: int, deadline: float) -> None:
    """Wait until a foreign process (not our child) has fully exited.

    A detached daemon holds the temp state directory as its working
    directory, and Windows refuses to remove that tree while it lives.
    """
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:  # ERROR_INVALID_PARAMETER: already exited
            return
        try:
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            kernel32.WaitForSingleObject(handle, remaining)
        finally:
            kernel32.CloseHandle(handle)
    else:
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.01)


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
    ), patch.object(
        runtime.subprocess, "Popen", return_value=process
    ) as popen, TemporaryDirectory() as temporary:
        directory = Path(temporary)
        assert (
            runtime._spawn_daemon(
                11, 1234, ack_fd=22, claim_path=Path("claim"), working_directory=directory
            )
            is process
        )

    command, kwargs = popen.call_args.args[0], popen.call_args.kwargs
    assert command[:4] == [sys.executable, "-P", "-m", "nyx.runtime"]
    assert kwargs["cwd"] == directory
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
                # The delay must exceed the shared deadline so the expiry is
                # observed after configuration admission, not at the
                # pre-admission check.
                time.sleep(0.4)
            return original_lstat(path)

        try:
            with patch.object(runtime, "_paths", return_value=paths):
                # The deadline must survive daemon construction on a loaded
                # runner so the pre-admission check cannot win the race.
                daemon = runtime._Daemon(lease_fd, time.monotonic_ns() + 300_000_000)
            with patch.object(
                state, "_lstat", side_effect=delayed_admission_lookup
            ), patch.object(
                daemon.application, "_server_factory"
            ) as create_server, pytest.raises(
                runtime.UnhealthyInstanceError, match="deadline expired"
            ):
                daemon.start()
            create_server.assert_not_called()
            assert daemon.server is None
        finally:
            os.close(lease_fd)


def test_static_readiness_expiry_retains_its_distinct_daemon_failure_phase():
    # Only readiness polling advances time; configuration cannot consume the
    # deadline because of host load before the phase under test is reached.
    clock = [1.0]

    def advance_clock(seconds):
        clock[0] += seconds

    class Server:
        def serve_forever(self):
            return None

        def shutdown(self):
            return None

        def close_active_connections(self):
            return None

        def server_close(self):
            return None

    class Thread:
        def __init__(self, *, target, daemon=True, args=()):
            self.target = target

        def start(self):
            if self.target.__name__ == "_finish_start_failure_cleanup":
                self.target()

        def join(self, timeout=None):
            return None

    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd = os.open(paths.runtime_directory / "lease.lock", os.O_RDWR)
        try:
            with patch.object(
                runtime.time, "monotonic", side_effect=lambda: clock[0]
            ), patch.object(
                runtime.time, "monotonic_ns", side_effect=lambda: int(clock[0] * 1e9)
            ), patch.object(
                runtime.time, "sleep", side_effect=advance_clock
            ):
                with patch.object(runtime, "_paths", return_value=paths):
                    daemon = runtime._Daemon(lease_fd, 1_500_000_000)
                with patch.object(
                    daemon.application, "_server_factory", return_value=Server()
                ) as create_server, patch.object(
                    runtime.threading, "Thread", Thread
                ), patch.object(daemon, "_static_ready", return_value=False) as ready:
                    assert daemon.run() == 25
                assert ready.call_count > 1
                assert clock[0] >= daemon._deadline()
            create_server.assert_called_once()
        finally:
            try:
                os.close(lease_fd)
            except OSError:
                pass

    assert runtime._DAEMON_FAILURES[25] == "static-readiness-failed"


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


@pytest.mark.skipif(
    runtime.desktop_host(),
    reason="the detached Linux service is not dispatched on desktop hosts",
)
@pytest.mark.parametrize("error_name", ["EADDRINUSE", "ENOMEM"])
@pytest.mark.parametrize("through_cli", [False, True])
def test_public_start_reports_sanitized_detached_child_failure(error_name, through_cli, capsys):
    from nyx import cli
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        paths, home, _ = _fixture(root)
        environment = _subprocess_environment(home, root / "site")
        script = (
            "import errno, runpy; from nyx import app_runtime\n"
            "def fail(**kwargs):\n"
            f"    raise OSError(errno.{error_name}, 'private-child-detail')\n"
            "class FailingApplicationRuntime(app_runtime.ApplicationRuntime):\n"
            "    def __init__(self, **kwargs):\n"
            "        super().__init__(**kwargs)\n"
            "        self._server_factory=fail\n"
            "app_runtime.ApplicationRuntime=FailingApplicationRuntime\n"
            "runpy.run_module('nyx.runtime', run_name='__main__')\n"
        )
        with patch.dict(os.environ, environment), patch.object(
            state, "resolve_account_home", return_value=home
        ), patch.object(
            runtime, "_daemon_command",
            side_effect=lambda deadline: [
                sys.executable, "-c", script, "--deadline-ns", str(deadline),
                "--port", str(runtime.PORT),
            ],
        ):
            if through_cli:
                assert cli.main([]) == 1
                captured = capsys.readouterr()
                expected = (
                    runtime.PORT_UNAVAILABLE_MESSAGE if error_name == "EADDRINUSE"
                    else "Nyx daemon exited before readiness"
                )
                assert captured.out == ""
                assert captured.err == f"nyx: {expected}\n"
            else:
                with pytest.raises(runtime.StartupError) as caught:
                    runtime.start()
                if error_name == "EADDRINUSE":
                    assert isinstance(caught.value, runtime.PortConflictError)
                    assert str(caught.value) == runtime.PORT_UNAVAILABLE_MESSAGE
                    outcome = "fixed-port-unavailable; exit=30"
                else:
                    assert not isinstance(caught.value, runtime.PortConflictError)
                    assert str(caught.value) == "Nyx daemon exited before readiness"
                    outcome = "http-start-failed; exit=24"
                assert f"outcome={outcome}" in caught.value.__notes__[0]
                assert "private-child-detail" not in repr(caught.value.__notes__)
                assert "private-child-detail" not in str(caught.value)
        assert not runtime._record_path(paths).exists()
        _assert_claim_available(paths.runtime_directory / "lease.lock")


@pytest.mark.fixed_port
def test_public_port_constants_and_daemon_command_keep_the_fixed_port():
    assert runtime.PORT == 8765
    assert runtime.URL == "http://127.0.0.1:8765/"
    assert runtime.PORT_UNAVAILABLE_MESSAGE == (
        "Nyx could not start: port 8765 is already in use by another program. "
        'Close that program and try again; "If something looks wrong" in the '
        "Running Nyx guide shows how to find it."
    )
    command = runtime._daemon_command(1234)
    assert command[command.index("--port") + 1] == "8765"
    assert command[-4:] == ["--deadline-ns", "1234", "--port", "8765"]


@pytest.mark.fixed_port
def test_startup_port_conflict_uses_actionable_public_message():
    error = runtime._startup_failure(
        "Nyx daemon exited before readiness", SimpleNamespace(poll=lambda: 30), "readiness"
    )
    assert isinstance(error, runtime.PortConflictError)
    assert str(error) == runtime.PORT_UNAVAILABLE_MESSAGE
    assert runtime.PORT_UNAVAILABLE_MESSAGE == (
        "Nyx could not start: port 8765 is already in use by another program. "
        'Close that program and try again; "If something looks wrong" in the '
        "Running Nyx guide shows how to find it."
    )
    assert error.__notes__ == [
        "internal startup phase=readiness; outcome=fixed-port-unavailable; exit=30"
    ]


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


def test_module_entry_refuses_desktop_daemon_arguments_before_handle_conversion(
    monkeypatch, capsys
):
    monkeypatch.setattr(runtime, "desktop_host", lambda: True)
    with patch.object(runtime, "_receive_daemon_handles") as receive, patch.object(
        runtime, "_daemon_entry"
    ) as entry:
        for arguments in (
            ["nyx/runtime.py", "--daemon-fd", "7", "--deadline-ns", "1"],
            [
                "nyx/runtime.py",
                "--daemon-handle",
                "9",
                "--ack-handle",
                "8",
                "--deadline-ns",
                "1",
            ],
            ["nyx/runtime.py", "--claim-path", "/tmp/lease.lock", "--deadline-ns", "1"],
            ["nyx/runtime.py", "--port", "8765"],
        ):
            assert runtime._module_entry(arguments) == 2
    receive.assert_not_called()
    entry.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{runtime._DESKTOP_DAEMON_REFUSAL}\n" * 4


def test_module_entry_applies_the_launcher_port_before_building_the_daemon(monkeypatch):
    monkeypatch.setattr(runtime, "desktop_host", lambda: False)
    observed: list[tuple[int, str, str]] = []

    class RecordingDaemon:
        def __init__(self, fd, deadline_ns, *, ack_fd=None):
            observed.append((runtime.PORT, runtime.URL, runtime.PORT_UNAVAILABLE_MESSAGE))
            assert (fd, deadline_ns, ack_fd) == (7, 1234, None)

        def run(self):
            return 25

    with patch.object(runtime, "_Daemon", RecordingDaemon):
        assert (
            runtime._module_entry(
                ["nyx/runtime.py", "--daemon-fd", "7", "--deadline-ns", "1234", "--port", "43117"]
            )
            == 25
        )
    assert observed == [
        (
            43117,
            "http://127.0.0.1:43117/",
            "Nyx could not start: port 43117 is already in use by another program. "
            'Close that program and try again; "If something looks wrong" in the '
            "Running Nyx guide shows how to find it.",
        )
    ]


@pytest.mark.parametrize(
    "port_arguments",
    [[], ["--port"], ["--port", "eighty"], ["--port", "-1"], ["--port", "0"], ["--port", "65536"]],
    ids=["missing", "no-value", "not-a-number", "negative", "zero", "out-of-range"],
)
def test_module_entry_rejects_a_missing_or_malformed_port_before_building_the_daemon(
    monkeypatch, port_arguments
):
    monkeypatch.setattr(runtime, "desktop_host", lambda: False)
    before = (runtime.PORT, runtime.URL, runtime.PORT_UNAVAILABLE_MESSAGE)
    with patch.object(runtime, "_Daemon") as daemon, patch.object(
        runtime, "_daemon_entry"
    ) as entry, pytest.raises(SystemExit) as error:
        runtime._module_entry(
            ["nyx/runtime.py", "--daemon-fd", "7", "--deadline-ns", "1234", *port_arguments]
        )
    assert error.value.code == "invalid lifecycle arguments"
    daemon.assert_not_called()
    entry.assert_not_called()
    assert (runtime.PORT, runtime.URL, runtime.PORT_UNAVAILABLE_MESSAGE) == before


def test_module_entry_still_rejects_a_launch_without_daemon_arguments(monkeypatch):
    monkeypatch.setattr(runtime, "desktop_host", lambda: False)
    with pytest.raises(SystemExit) as error:
        runtime._module_entry(["nyx/runtime.py"])
    assert error.value.code == "internal lifecycle entry point"


@pytest.mark.skipif(
    not runtime.desktop_host(),
    reason="the internal daemon module is refused on Windows and macOS",
)
def test_desktop_module_launch_refuses_legacy_daemon_arguments(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    environment = _subprocess_environment(home, tmp_path / "site")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "nyx.runtime",
            "--daemon-fd",
            "0",
            "--deadline-ns",
            "1",
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == f"{runtime._DESKTOP_DAEMON_REFUSAL}\n"
    assert not (home / ".nyx").exists()


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
            with patch.object(
                daemon.application, "_server_factory", return_value=Server()
            ), patch.object(runtime.threading, "Thread", Thread), patch.object(
                daemon, "_static_ready", return_value=True
            ), patch.object(
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


def test_linux_daemon_wires_http_provider_and_workers_to_shared_application_owner():
    created: list[tuple[object, int, object]] = []

    class Server:
        def serve_forever(self):
            return None

        def shutdown(self):
            return None

        def close_active_connections(self):
            return None

        def server_close(self):
            return None

    class Control:
        def getsockname(self):
            return ("127.0.0.1", 43210)

        def close(self):
            return None

    class Thread:
        def __init__(self, *, target, daemon=True, args=()):
            self.target = target

        def start(self):
            if self.target.__name__ == "_finish_start_failure_cleanup":
                self.target()

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return False

    def make_server(*, provider, port, settings_provider):
        created.append((provider, port, settings_provider))
        return Server()

    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd = os.open(paths.runtime_directory / "lease.lock", os.O_RDWR)
        try:
            with patch.object(runtime, "_paths", return_value=paths):
                daemon = runtime._Daemon(
                    lease_fd, time.monotonic_ns() + 5_000_000_000
                )
            with patch.object(
                daemon.application, "_server_factory", side_effect=make_server
            ), patch.object(runtime.threading, "Thread", Thread), patch.object(
                daemon, "_static_ready", return_value=True
            ), patch.object(
                daemon, "_bind_control", return_value=Control()
            ), patch.object(
                runtime,
                "_send_control",
                return_value={"status": "ready", "url": runtime.URL},
            ), patch.object(
                runtime, "_write_instance", return_value=runtime._Metadata(False)
            ):
                daemon.start()
        finally:
            os.close(lease_fd)

    assert len(created) == 1
    provider, port, settings_provider = created[0]
    assert port == runtime.PORT
    assert getattr(provider, "__self__", None) is daemon.application
    assert settings_provider is daemon.application
    assert daemon.application.server is daemon.server
    assert daemon.application.workers is daemon.workers
    assert daemon.application.catalog_admitted is False


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
        with patch.object(
            daemon.application, "_server_factory", return_value=Server()
        ), patch.object(runtime.threading, "Thread", Thread), patch.object(
            daemon, "_static_ready", return_value=True
        ), patch.object(
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

        with patch.object(runtime, "_paths", return_value=paths), patch.object(
            runtime, "_write_instance", side_effect=fail_publication
        ), managed_daemon_run(daemon) as run:
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
            assert run.join(timeout=15) == 28

        assert not worker_marker.exists()
        assert daemon.workers.active_count == 0
        assert daemon.http_thread is not None
        assert not daemon.http_thread.is_alive()
        listener_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            assert listener_probe.connect_ex(("127.0.0.1", runtime.PORT)) != 0
        finally:
            listener_probe.close()
        assert not (paths.runtime_directory / "instance.json").exists()
        contender = runtime._lease_lock(paths, timeout=0.0)
        assert contender.acquire(blocking=False)
        contender.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM-resistant worker proof")
def test_failed_start_cleanup_retains_resistant_worker_until_later_completion():
    with TemporaryDirectory() as temporary:
        marker = Path(temporary) / "worker-ready"
        application = runtime.ApplicationRuntime(
            port=runtime.PORT,
            deadline=time.monotonic() + 5,
            workers=runtime.CatalogWorkerManager(
                command_factory=lambda: [
                    sys.executable,
                    "-c",
                    (
                        "import signal,time; from pathlib import Path; "
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        f"Path({str(marker)!r}).write_text('ready', encoding='utf-8'); "
                        "time.sleep(30)"
                    ),
                ],
                timeout=30,
            ),
        )
        application.catalog_admitted = True
        request_errors: list[BaseException] = []

        def request_catalog() -> None:
            try:
                application._provider()
            except BaseException as error:  # noqa: BLE001 - asserted below
                request_errors.append(error)

        request = threading.Thread(target=request_catalog)
        request.start()
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        assert application.workers.active_count == 1

        assert application.cleanup_start_failure(time.monotonic() + 0.05) is False
        assert application.catalog_admitted is False
        assert application.workers.active_count == 1
        with application.workers._lock:
            child = application.workers._children[0]
        assert child.process.poll() is None

        child.process.kill()
        request.join(timeout=3)
        assert not request.is_alive()
        assert len(request_errors) == 1
        assert isinstance(request_errors[0], runtime.CatalogError)
        assert application.cleanup_start_failure(time.monotonic() + 2) is True
        assert application.workers.active_count == 0


def _owner_schema_two(paths, specification_root):
    root = str(specification_root.resolve())
    payload = {
        "schema_version": 2,
        "specification_root": root,
        "hidden_stages": [],
        "stage_orders": {root: ["Under_Development", "Queue", "In_Progress",
                                "Needs_Fixes", "Awaiting_Retrospective", "Done", "Archive"]},
        "completed_stages": {root: ["Archive", "Done"]},
    }
    paths.config_file.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return payload


def test_schema_two_owner_settings_rewrite_only_on_setup_with_equal_revision():
    with TemporaryDirectory() as temporary:
        paths, home, specification_root = _fixture(Path(temporary))
        expected = _owner_schema_two(paths, specification_root)
        before = paths.config_file.read_bytes()
        loaded = state.load_configuration(paths)
        with patch.object(state, "resolve_account_home", return_value=home):
            observed = state.observe_configuration()
        assert observed.configuration == loaded
        assert observed.status == "configured"
        assert paths.config_file.read_bytes() == before
        with patch.object(runtime, "_paths", return_value=paths):
            rewritten = runtime.setup(specification_root)
        expected.pop("hidden_stages")
        expected["schema_version"] = 4
        assert json.loads(paths.config_file.read_bytes()) == expected
        assert rewritten.stage_orders == loaded.stage_orders
        assert rewritten.completed_stages == loaded.completed_stages
        assert rewritten.revision == loaded.revision == state.configuration_revision(paths)


def test_application_settings_save_conflict_and_restart_persistence():
    with TemporaryDirectory() as temporary:
        paths, _, specification_root = _fixture(Path(temporary))
        expected = _owner_schema_two(paths, specification_root)
        initial = state.load_configuration(paths)
        application = runtime.ApplicationRuntime(
            port=runtime.PORT, deadline=time.monotonic() + 5
        )
        application.capture_configuration(initial, paths)
        application.admit_catalog()
        saved = application.save_settings(
            initial.revision, ["Done", "Queue"], ["Done"]
        )
        assert saved["outcome"] == "success"
        assert saved["order"] == ["Done", "Queue"]
        assert saved["completed"] == ["Done"]
        expected.pop("hidden_stages")
        expected["schema_version"] = 4
        expected["stage_orders"][str(specification_root.resolve())] = ["Done", "Queue"]
        expected["completed_stages"][str(specification_root.resolve())] = ["Done"]
        assert json.loads(paths.config_file.read_bytes()) == expected
        assert application.save_settings(initial.revision, ["Archive"])["outcome"] == "conflict"
        assert state.load_configuration(paths).stage_order == ("Done", "Queue")
        assert state.load_configuration(paths).completed_stage_names == ("Done",)
        assert application.shutdown(time.monotonic() + 2)

        restarted = runtime.ApplicationRuntime(
            port=runtime.PORT, deadline=time.monotonic() + 5
        )
        restarted.capture_configuration(state.load_configuration(paths), paths)
        restarted.admit_catalog()
        current = restarted.get_settings()
        assert current["order"] == ["Done", "Queue"]
        assert current["completed"] == ["Done"]
        assert current["revision"] == state.configuration_revision(paths)
        assert str(specification_root) not in current
        assert restarted.shutdown(time.monotonic() + 2)


def test_application_settings_save_reports_pre_replace_failure_and_preserves_order():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        initial = state.load_configuration(paths)
        application = runtime.ApplicationRuntime(
            port=runtime.PORT, deadline=time.monotonic() + 5
        )
        application.capture_configuration(initial, paths)
        application.admit_catalog()
        with patch.object(state.os, "replace", side_effect=OSError("injected")):
            result = application.save_settings(initial.revision, ["Queue"])
        assert result["outcome"] == "failure"
        assert result["order"] == []
        assert state.load_configuration(paths).stage_order == ()
        assert application.shutdown(time.monotonic() + 2)


def test_application_settings_save_revalidates_after_post_replace_failure():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        initial = state.load_configuration(paths)
        application = runtime.ApplicationRuntime(
            port=runtime.PORT, deadline=time.monotonic() + 5
        )
        application.capture_configuration(initial, paths)
        application.admit_catalog()
        original_verify = state._verify_record
        failed = False

        def fail_once(path):
            nonlocal failed
            details = original_verify(path)
            if (
                path == paths.config_file
                and not failed
                and json.loads(path.read_text(encoding="utf-8")).get("stage_orders", {}).values()
                and ["Queue", "Done"]
                in json.loads(path.read_text(encoding="utf-8")).get("stage_orders", {}).values()
            ):
                failed = True
                raise OSError("injected post-replacement verification failure")
            return details

        with patch.object(state, "_verify_record", side_effect=fail_once):
            result = application.save_settings(initial.revision, ["Queue", "Done"])
        assert result["outcome"] == "success"
        assert result["order"] == ["Queue", "Done"]
        assert state.load_configuration(paths).stage_order == ("Queue", "Done")
        assert application.shutdown(time.monotonic() + 2)


def _settings_replaced_record_hook(paths, requested_order, replacement):
    """Overwrite the just-replaced record, then fail its commit verification once.

    The fired flag is tested before the record is read, so every later
    verification (including revalidation of an unreadable record) sees the
    real result.
    """

    original_verify = state._verify_record
    fires = []

    def verify(path, *args, **kwargs):
        details = original_verify(path, *args, **kwargs)
        if fires or path != paths.config_file:
            return details
        payload = json.loads(path.read_text(encoding="utf-8"))
        if list(requested_order) not in payload.get("stage_orders", {}).values():
            return details
        fires.append(path)
        path.write_bytes(replacement)
        raise OSError("injected post-replacement verification failure")

    return verify, fires


def _settings_application(paths):
    initial = state.load_configuration(paths)
    application = runtime.ApplicationRuntime(
        port=runtime.PORT, deadline=time.monotonic() + 5
    )
    application.capture_configuration(initial, paths)
    application.admit_catalog()
    return initial, application


def test_application_settings_save_reports_conflict_for_competing_record_after_replace():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        initial, application = _settings_application(paths)
        root = str(initial.specification_root)
        competing = state._configuration_bytes(
            initial.specification_root,
            {root: ["Archive", "Review"]},
            {root: ["Archive"]},
        )
        verify, fires = _settings_replaced_record_hook(paths, ["Queue", "Done"], competing)
        with patch.object(state, "_verify_record", side_effect=verify):
            result = application.save_settings(initial.revision, ["Queue", "Done"], ["Done"])
        assert fires == [paths.config_file]
        assert result == {
            "order": ["Archive", "Review"],
            "completed": ["Archive"],
            "revision": state.configuration_revision(paths),
            "outcome": "conflict",
        }
        assert paths.config_file.read_bytes() == competing
        reloaded = state.load_configuration(paths)
        assert reloaded.stage_order == ("Archive", "Review")
        assert reloaded.completed_stage_names == ("Archive",)
        # The runtime adopts the competing record, so its reported revision is current.
        assert application.get_settings() == {
            "order": ["Archive", "Review"],
            "completed": ["Archive"],
            "revision": result["revision"],
        }
        retried = application.save_settings(result["revision"], ["Queue", "Done"], ["Done"])
        assert retried == {
            "order": ["Queue", "Done"],
            "completed": ["Done"],
            "revision": state.configuration_revision(paths),
            "outcome": "success",
        }
        assert application.shutdown(time.monotonic() + 2)


def test_application_settings_save_reports_reload_needed_for_unreadable_record_after_replace():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        initial, application = _settings_application(paths)
        verify, fires = _settings_replaced_record_hook(paths, ["Queue", "Done"], b"{")
        with patch.object(state, "_verify_record", side_effect=verify):
            result = application.save_settings(initial.revision, ["Queue", "Done"], ["Done"])
        assert fires == [paths.config_file]
        assert result == {
            "order": [],
            "completed": [],
            "revision": None,
            "outcome": "reload-needed",
        }
        assert paths.config_file.read_bytes() == b"{"
        assert application.shutdown(time.monotonic() + 2)


def test_application_settings_save_reports_reload_needed_for_unreadable_record_before_replace():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        initial, application = _settings_application(paths)
        original_replace = state.os.replace
        fires = []

        def replace(source, destination, *args, **kwargs):
            if fires or Path(destination) != paths.config_file:
                return original_replace(source, destination, *args, **kwargs)
            fires.append(Path(destination))
            paths.config_file.write_bytes(b"{")
            raise OSError("injected replacement failure")

        with patch.object(state.os, "replace", side_effect=replace):
            result = application.save_settings(initial.revision, ["Queue", "Done"], ["Done"])
        assert fires == [paths.config_file]
        assert result == {
            "order": [],
            "completed": [],
            "revision": None,
            "outcome": "reload-needed",
        }
        assert paths.config_file.read_bytes() == b"{"
        assert application.shutdown(time.monotonic() + 2)


def test_application_shutdown_drains_admitted_settings_write_and_rejects_later_admission():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        initial = state.load_configuration(paths)
        application = runtime.ApplicationRuntime(
            port=runtime.PORT, deadline=time.monotonic() + 5
        )
        application.capture_configuration(initial, paths)
        application.admit_catalog()
        entered = threading.Event()
        release = threading.Event()
        saved: list[dict[str, object]] = []
        shutdown_result: list[bool] = []
        original_save = state.save_configuration_owned

        def blocked_save(*args, **kwargs):
            entered.set()
            assert release.wait(timeout=3)
            return original_save(*args, **kwargs)

        with patch.object(state, "save_configuration_owned", side_effect=blocked_save):
            save_thread = threading.Thread(
                target=lambda: saved.append(application.save_settings(initial.revision, ["Queue"]))
            )
            save_thread.start()
            assert entered.wait(timeout=2)
            shutdown_thread = threading.Thread(
                target=lambda: shutdown_result.append(application.shutdown(time.monotonic() + 3))
            )
            shutdown_thread.start()
            time.sleep(0.05)
            assert shutdown_thread.is_alive()
            release.set()
            save_thread.join(timeout=3)
            shutdown_thread.join(timeout=3)
        assert saved == [{
            "order": ["Queue"],
            "completed": [],
            "revision": saved[0]["revision"],
            "outcome": "success",
        }]
        assert shutdown_result == [True]
        with pytest.raises(runtime.CatalogError, match="settings_unavailable"):
            application.get_settings()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux background-service proof")
def test_normal_service_settings_survive_terminal_stop_and_restart():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        specification_root = root / "specifications"
        specification_root.mkdir()

        with patch.object(state, "resolve_account_home", return_value=home):
            runtime.setup(specification_root)
            paths = state.state_paths()

            def settings_request(
                method: str, body: dict[str, object] | None = None
            ) -> tuple[int, dict[str, object]]:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", runtime.PORT, timeout=5
                )
                try:
                    headers = {"Host": f"127.0.0.1:{runtime.PORT}"}
                    encoded = None if body is None else json.dumps(body)
                    if encoded is not None:
                        headers["Content-Type"] = "application/json"
                    connection.request(
                        method, "/api/settings", body=encoded, headers=headers
                    )
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    connection.close()

            def new_daemon() -> runtime._Daemon:
                lease_fd = _transferred_claim_fd(
                    paths.runtime_directory / "lease.lock"
                )
                return runtime._Daemon(
                    lease_fd, time.monotonic_ns() + 20_000_000_000
                )

            def wait_for_settings() -> None:
                _wait_for_record(paths)
                deadline = time.monotonic() + 5
                while True:
                    status, current = settings_request("GET")
                    if status == 200:
                        return
                    assert status == 503
                    if time.monotonic() >= deadline:
                        raise AssertionError("service settings did not become ready")
                    time.sleep(0.01)

            with managed_daemon_run(new_daemon()) as run:
                wait_for_settings()
                first_status, first = settings_request("GET")
                assert first_status == 200
                assert first == {
                    "order": [],
                    "completed": [],
                    "revision": state.configuration_revision(paths),
                }

                saved_status, saved = settings_request(
                    "PUT",
                    {
                        "revision": first["revision"],
                        "order": ["Done", "Queue"],
                        "completed": ["Done"],
                    },
                )
                assert saved_status == 200
                assert saved["outcome"] == "success"
                assert saved["order"] == ["Done", "Queue"]
                assert saved["completed"] == ["Done"]
                assert saved["revision"] == state.configuration_revision(paths)

                assert runtime.stop() == "stopped"
                assert run.join(timeout=5) == 0

            with managed_daemon_run(new_daemon()) as run:
                wait_for_settings()
                restarted_status, restarted = settings_request("GET")
                assert restarted_status == 200
                assert restarted == {
                    "order": ["Done", "Queue"],
                    "completed": ["Done"],
                    "revision": state.configuration_revision(paths),
                }

                assert runtime.stop() == "stopped"
                assert run.join(timeout=5) == 0


@pytest.mark.skipif(sys.platform != "linux", reason="Linux background-service proof")
def test_linux_cli_uses_one_application_owner_through_failed_active_work_cleanup():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        specifications = root / "specifications"
        package = specifications / "Fictional" / "Queue" / "sample"
        package.mkdir(parents=True)
        package.joinpath("spec.md").write_text(
            "# Sample catalog entry\nStatus: approved\nClosure: approved\n",
            encoding="utf-8",
        )
        probe_file = root / "application-owner.jsonl"
        worker_pid_file = root / "worker.pid"
        site_directory = root / "site"
        site_directory.mkdir()
        site_directory.joinpath("sitecustomize.py").write_text(
            textwrap.dedent(
                """
                import json
                import os
                import signal
                import sys
                import threading
                import time
                from pathlib import Path

                from nyx import app_runtime, state

                state.resolve_account_home = lambda: Path(os.environ["NYX_TEST_HOME"])
                probe_file = Path(os.environ["NYX_OWNER_PROBE"])
                worker_pid_file = Path(os.environ["NYX_WORKER_PID"])
                original_application_runtime = app_runtime.ApplicationRuntime

                def record(**event):
                    with probe_file.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(event, sort_keys=True) + "\\n")

                worker_code = (
                    "import os,signal,sys,time; from pathlib import Path; "
                    "from nyx.worker import _worker_main; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
                    "result=_worker_main(); time.sleep(30); raise SystemExit(result)"
                )

                class ObservedApplicationRuntime(original_application_runtime):
                    instance_count = 0

                    def __init__(self, **kwargs):
                        kwargs["workers"] = app_runtime.CatalogWorkerManager(
                            command_factory=lambda: [
                                sys.executable,
                                "-c",
                                worker_code,
                                str(worker_pid_file),
                            ],
                            timeout=30,
                        )
                        super().__init__(**kwargs)
                        type(self).instance_count += 1
                        self.observed_provider = None
                        self.observed_server = None
                        original_factory = self._server_factory

                        def observing_factory(**factory_kwargs):
                            self.observed_provider = factory_kwargs["provider"]
                            self.observed_server = original_factory(**factory_kwargs)
                            return self.observed_server

                        self._server_factory = observing_factory

                    def identities(self):
                        provider_owner = getattr(self.observed_provider, "__self__", None)
                        return {
                            "application_id": id(self),
                            "provider_owner_id": id(provider_owner),
                            "manager_id": id(self.workers),
                            "server_id": id(self.server),
                            "factory_server_id": id(self.observed_server),
                        }

                    def start(self, **kwargs):
                        super().start(**kwargs)
                        record(
                            event="started",
                            daemon_pid=os.getpid(),
                            instance_count=type(self).instance_count,
                            listener_port=self.server.server_port,
                            **self.identities(),
                        )
                        threading.Thread(
                            target=self.finish_listener_during_active_work,
                            daemon=True,
                        ).start()

                    def finish_listener_during_active_work(self):
                        deadline = time.monotonic() + 10
                        while self.workers.active_count == 0 and time.monotonic() < deadline:
                            time.sleep(0.01)
                        record(
                            event="active",
                            active_count=self.workers.active_count,
                            listener_fileno=self.server.fileno(),
                            **self.identities(),
                        )
                        if self.workers.active_count:
                            self.server.shutdown()

                    def cleanup_start_failure(self, deadline):
                        complete = super().cleanup_start_failure(deadline)
                        record(
                            event="failed_cleanup",
                            complete=complete,
                            active_count=self.workers.active_count,
                            listener_fileno=self.server.fileno(),
                            **self.identities(),
                        )
                        return complete

                    def shutdown(self, deadline):
                        complete = super().shutdown(deadline)
                        record(
                            event="shutdown",
                            complete=complete,
                            active_count=self.workers.active_count,
                            listener_fileno=self.server.fileno(),
                            **self.identities(),
                        )
                        return complete

                app_runtime.ApplicationRuntime = ObservedApplicationRuntime
                """
            )
            + _PORT_SITECUSTOMIZE,
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["HOME"] = str(home)
        environment["USERPROFILE"] = str(home)
        environment["NYX_TEST_HOME"] = str(home)
        environment["NYX_TEST_PORT"] = str(runtime.PORT)
        environment["NYX_OWNER_PROBE"] = str(probe_file)
        environment["NYX_WORKER_PID"] = str(worker_pid_file)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = str(site_directory) + os.pathsep + environment.get(
            "PYTHONPATH", ""
        )
        repository_root = Path(__file__).parents[1]

        def run_cli(*arguments: str, timeout: float = 15) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-m", "nyx.cli", *arguments],
                cwd=repository_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )

        daemon_pid: int | None = None
        worker_pid: int | None = None
        request_result: list[object] = []
        request_thread: threading.Thread | None = None
        try:
            setup = run_cli("--setup", str(specifications))
            assert setup.returncode == 0, setup.stderr
            started = run_cli()
            assert started.returncode == 0, started.stderr
            assert started.stdout == f"{runtime.URL}\n"

            def request_catalog() -> None:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", runtime.PORT, timeout=15
                )
                try:
                    connection.request(
                        "GET",
                        "/api/catalog",
                        headers={"Host": f"127.0.0.1:{runtime.PORT}"},
                    )
                    response = connection.getresponse()
                    request_result.append((response.status, response.read()))
                except OSError as error:
                    request_result.append(error)
                finally:
                    connection.close()

            request_thread = threading.Thread(target=request_catalog)
            request_thread.start()
            deadline = time.monotonic() + 12
            events: list[dict[str, object]] = []
            while time.monotonic() < deadline:
                if probe_file.exists():
                    events = [
                        json.loads(line)
                        for line in probe_file.read_text(encoding="utf-8").splitlines()
                    ]
                if worker_pid_file.exists() and any(
                    event["event"] == "failed_cleanup" for event in events
                ):
                    break
                time.sleep(0.02)
            assert worker_pid_file.exists()
            worker_pid = int(worker_pid_file.read_text(encoding="utf-8"))
            started_event = next(event for event in events if event["event"] == "started")
            active_event = next(event for event in events if event["event"] == "active")
            cleanup_event = next(
                event for event in events if event["event"] == "failed_cleanup"
            )
            daemon_pid = int(started_event["daemon_pid"])

            assert started_event["instance_count"] == 1
            assert started_event["listener_port"] == runtime.PORT
            assert started_event["application_id"] == started_event["provider_owner_id"]
            assert started_event["server_id"] == started_event["factory_server_id"]
            assert active_event["active_count"] == 1
            assert active_event["application_id"] == started_event["application_id"]
            assert active_event["manager_id"] == started_event["manager_id"]
            assert active_event["server_id"] == started_event["server_id"]
            assert cleanup_event["complete"] is False
            assert cleanup_event["active_count"] == 1
            assert cleanup_event["listener_fileno"] == -1

            instance_file = home / ".nyx" / "runtime" / "instance.json"
            claim_file = home / ".nyx" / "runtime" / "lease.lock"
            assert instance_file.exists()
            contender = runtime.NativeClaim(claim_file)
            assert not contender.acquire(create=False, blocking=False)
            contender.close()

            first_stop = run_cli("--stop")
            assert first_stop.returncode == 1
            assert "ownership was retained" in first_stop.stderr
            assert instance_file.exists()

            os.kill(worker_pid, signal.SIGKILL)
            request_thread.join(timeout=3)
            assert not request_thread.is_alive()
            assert request_result

            second_stop = run_cli("--stop")
            assert second_stop.returncode == 0, second_stop.stderr
            assert second_stop.stdout == "stopped\n"
            assert not instance_file.exists()
            _assert_claim_available(claim_file)

            events = [
                json.loads(line)
                for line in probe_file.read_text(encoding="utf-8").splitlines()
            ]
            shutdown_events = [event for event in events if event["event"] == "shutdown"]
            assert [event["complete"] for event in shutdown_events] == [False, True]
            assert shutdown_events[0]["active_count"] == 1
            assert shutdown_events[1]["active_count"] == 0
            assert all(
                event["application_id"] == started_event["application_id"]
                and event["manager_id"] == started_event["manager_id"]
                and event["server_id"] == started_event["server_id"]
                for event in shutdown_events
            )
        finally:
            if worker_pid is None and worker_pid_file.exists():
                worker_pid = int(worker_pid_file.read_text(encoding="utf-8"))
            if worker_pid is not None:
                try:
                    os.kill(worker_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if request_thread is not None:
                request_thread.join(timeout=1)
            for _ in range(2):
                try:
                    stopped = run_cli("--stop", timeout=7)
                except subprocess.TimeoutExpired:
                    continue
                if stopped.returncode == 0:
                    break
            if daemon_pid is None and probe_file.exists():
                for event in probe_file.read_text(encoding="utf-8").splitlines():
                    payload = json.loads(event)
                    if payload["event"] == "started":
                        daemon_pid = int(payload["daemon_pid"])
                        break
            if daemon_pid is not None:
                try:
                    os.kill(daemon_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


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
        with patch.object(runtime, "_paths", return_value=paths), patch.object(
            daemon.application, "_server_factory", return_value=Server()
        ), patch.object(daemon, "_static_ready", return_value=True), patch.object(
            daemon, "_bind_control", return_value=Control()
        ), patch.object(daemon, "_serve_control", return_value=None), patch.object(
            runtime,
            "_send_control",
            return_value={"status": "ready", "url": runtime.URL},
        ), patch.object(
            runtime, "_write_instance", side_effect=runtime.StartupError("publication failed")
        ), managed_daemon_run(daemon) as run:
            try:
                assert cleanup_entered.wait(timeout=1)
                deadline = time.monotonic() + 1
                while daemon.shutdown_result != "timeout" and time.monotonic() < deadline:
                    time.sleep(0.005)
                assert daemon.shutdown_result == "timeout"
                contender = runtime._lease_lock(paths, timeout=0.0)
                assert not contender.acquire(blocking=False)
                contender.close()
            finally:
                allow_cleanup.set()
            run.join(timeout=2)
            assert not run.is_alive()

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
    ) as popen, TemporaryDirectory() as temporary:
        directory = Path(temporary)
        assert (
            runtime._spawn_daemon(
                11, 1234, ack_fd=22, claim_path=Path("claim"), working_directory=directory
            )
            is process
        )
    command, kwargs = popen.call_args.args[0], popen.call_args.kwargs
    assert command[:4] == [sys.executable, "-P", "-m", "nyx.runtime"]
    assert kwargs["cwd"] == directory
    assert command[command.index("--daemon-fd") + 1] == "11"
    assert command[command.index("--ack-fd") + 1] == "22"
    assert kwargs["pass_fds"] == (11, 22)
    assert kwargs["close_fds"] is True
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


def test_public_start_spawns_the_daemon_in_its_admitted_state_directory():
    class SpawnRecorded(Exception):
        pass

    spawn = Mock(side_effect=SpawnRecorded)
    with TemporaryDirectory() as temporary:
        paths, home, _ = _fixture(Path(temporary))
        assert paths.state_directory == home / ".nyx"
        assert paths.state_directory.is_dir()
        with patch.object(runtime, "_paths", return_value=paths), patch.object(
            runtime, "_spawn_daemon", spawn
        ), pytest.raises(SpawnRecorded):
            runtime.start()
        assert spawn.call_count == 1
        assert spawn.call_args.kwargs["working_directory"] == paths.state_directory
        contender = runtime._lease_lock(paths, timeout=0.0)
        assert contender.acquire(blocking=False)
        contender.close()


def _sample_workspace(root: Path) -> Path:
    workspace = root / "specifications"
    package = workspace / "Fictional" / "Queue" / "sample"
    package.mkdir(parents=True)
    package.joinpath("spec.md").write_text("# Sample catalog entry\n", encoding="utf-8")
    return workspace


def _prove_isolated_cli_service(
    *,
    cli: list[str],
    launch: Path,
    environment: dict[str, str],
    workspace: Path,
    shadow_directory: Path,
    marker: Path,
) -> None:
    """Start the real service, request a worker-built catalog, then stop it."""

    def run_cli(*arguments: str, timeout: float = 15) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*cli, *arguments],
            cwd=launch,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    try:
        configured = run_cli("--setup", str(workspace))
        assert configured.returncode == 0, configured.stderr
        plant_shadow(shadow_directory, marker)
        started = run_cli()
        assert started.returncode == 0, (started.stderr, shadow_imports(marker))
        assert started.stdout == f"{runtime.URL}\n"
        connection = http.client.HTTPConnection("127.0.0.1", runtime.PORT, timeout=15)
        try:
            connection.request(
                "GET", "/api/catalog", headers={"Host": f"127.0.0.1:{runtime.PORT}"}
            )
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()
        assert response.status == 200, (body, shadow_imports(marker))
        payload = json.loads(body)
        assert payload["schema_version"] == SCHEMA_VERSION
        assert [entry["package_path"] for entry in payload["entries"]] == [
            "Fictional/Queue/sample"
        ]
        assert shadow_imports(marker) == []
    finally:
        for _ in range(2):
            try:
                stopped = run_cli("--stop", timeout=10)
            except subprocess.TimeoutExpired:
                continue
            if stopped.returncode == 0:
                break


@pytest.mark.fixed_port
@pytest.mark.skipif(
    runtime.desktop_host(),
    reason="the detached Linux service is not dispatched on desktop hosts",
)
def test_cli_service_ignores_shadow_modules_planted_in_its_state_directory():
    skip_if_service_port_is_held()
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        launch = root / "launch"
        launch.mkdir()
        environment = proof_environment(home)
        assert "PYTHONPATH" not in environment
        _prove_isolated_cli_service(
            cli=[sys.executable, "-m", "nyx.cli"],
            launch=launch,
            environment=environment,
            workspace=_sample_workspace(root),
            shadow_directory=home / ".nyx",
            marker=root / "shadow-imports.log",
        )


@pytest.mark.fixed_port
@pytest.mark.skipif(
    runtime.desktop_host(),
    reason="the detached Linux service is not dispatched on desktop hosts",
)
def test_cli_service_children_leave_the_launch_directory_for_the_state_directory():
    skip_if_service_port_is_held()
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        launch = root / "launch"
        launch.mkdir()
        # The launcher ignores PYTHONPATH and its own directory, so only the
        # spawned daemon and worker can resolve the relative entry, which
        # names their working directory.
        _prove_isolated_cli_service(
            cli=[sys.executable, "-E", "-P", "-m", "nyx.cli"],
            launch=launch,
            environment=proof_environment(home, pythonpath="."),
            workspace=_sample_workspace(root),
            shadow_directory=launch,
            marker=root / "shadow-imports.log",
        )


@pytest.mark.skipif(
    runtime.desktop_host(),
    reason="the detached Linux service is not dispatched on desktop hosts",
)
def test_stop_recovery_then_start_reaches_readiness(tmp_path, monkeypatch):
    """A recovered invalid record lets the next start reach readiness."""
    home = tmp_path / "home"
    home.mkdir()
    specification = tmp_path / "spec"
    specification.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    state.setup(specification)
    paths = state.state_paths()
    record = paths.runtime_directory / "instance.json"
    record.write_bytes(b"not json")
    leftover = paths.runtime_directory / ".instance.json.leftover"
    leftover.write_bytes(b"leftover")
    try:
        assert runtime.stop() == "stopped"
        assert not record.exists()
        assert not leftover.exists()
        assert runtime.start() == runtime.URL
        published = json.loads(record.read_text(encoding="utf-8"))
        # The detached daemon publishes the port it was told to serve, derived
        # through the same formula as this process's URL.
        assert published["url"] == runtime.URL != "http://127.0.0.1:8765/"
    finally:
        try:
            runtime.stop()
        except runtime.RuntimeErrorBase:
            pass


@pytest.mark.parametrize("denied", ["create", "bind"])
@pytest.mark.parametrize(
    "probe",
    [_free_loopback_port, skip_if_service_port_is_held],
    ids=["port-choice", "fixed-port-probe"],
)
def test_loopback_denial_skips_with_the_sandbox_reason_not_the_port_held_reason(
    monkeypatch, denied, probe
):
    real_socket = socket.socket

    class DeniedSocket(real_socket):
        def __init__(self, *args, **kwargs):
            if denied == "create":
                raise PermissionError("loopback sockets are denied")
            super().__init__(*args, **kwargs)

        def bind(self, address):
            raise PermissionError("loopback sockets are denied")

    monkeypatch.setattr(socket, "socket", DeniedSocket)
    with pytest.raises(pytest.skip.Exception) as skipped:
        probe()
    assert skipped.value.msg == LOOPBACK_DENIED_REASON


@pytest.mark.fixed_port
def test_service_tests_pass_while_another_listener_holds_the_fixed_port():
    """Port isolation keeps service tests independent of a running Nyx.

    This test holds the public port with an ordinary listener, then runs an
    in-process daemon test, a desktop-session test and, where the detached
    service exists, a test whose daemon child is spawned.  Each must pass.
    """

    try:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except PermissionError:
        pytest.skip(LOOPBACK_DENIED_REASON)
    try:
        try:
            holder.bind(("127.0.0.1", runtime.PORT))
        except PermissionError:
            pytest.skip(LOOPBACK_DENIED_REASON)
        except OSError:
            pytest.skip(SERVICE_PORT_HELD_REASON)
        holder.listen(1)
        nodes = [
            "tests/test_runtime.py::"
            "test_daemon_publishes_authenticated_fixed_url_and_releases_transferred_lease",
            "tests/test_desktop.py::"
            "test_active_workspace_switch_restarts_real_catalog_worker_over_http",
        ]
        if not runtime.desktop_host():
            nodes.append("tests/test_runtime.py::test_stop_recovery_then_start_reaches_readiness")
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rA", *nodes],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    finally:
        holder.close()
    reported = completed.stdout.splitlines()
    for node in nodes:
        assert f"PASSED {node}" in reported, completed.stdout[-6000:] + completed.stderr[-2000:]
    assert completed.returncode == 0, completed.stdout[-6000:]


class _ScriptedDaemon:
    """A daemon stand-in whose shutdown results are scripted in order.

    ``run()`` blocks until ``released`` is set.  A shutdown that reports
    ``"stopped"`` releases it unless the run is scripted to ignore shutdown.
    """

    def __init__(self, results: list[str], *, run_honors_shutdown: bool = True) -> None:
        self.results = list(results)
        self.run_honors_shutdown = run_honors_shutdown
        self.shutdown_deadlines: list[float] = []
        self.released = threading.Event()

    def run(self) -> int:
        self.released.wait(timeout=30)
        return 0

    def shutdown(self, deadline: float) -> str:
        self.shutdown_deadlines.append(deadline)
        result = self.results.pop(0)
        if result == "stopped" and self.run_honors_shutdown:
            self.released.set()
        return result


def test_managed_daemon_run_retries_timed_out_shutdown_until_stopped():
    daemon = _ScriptedDaemon(["timeout", "stopped"])
    with managed_daemon_run(daemon, shutdown_timeout=5, join_timeout=5) as run:
        assert run.is_alive()
        assert daemon.shutdown_deadlines == []

    # A third call would exhaust the script and raise inside the cleanup.
    assert len(daemon.shutdown_deadlines) == 2
    assert daemon.results == []
    assert not run.is_alive()
    assert run.returncode == 0


def test_managed_daemon_run_stops_retrying_at_its_shutdown_deadline():
    daemon = _ScriptedDaemon(["timeout"] * 1000, run_honors_shutdown=False)
    runs = []
    started = time.monotonic()
    try:
        with pytest.raises(pytest.fail.Exception, match="last shutdown result: 'timeout'"):
            with managed_daemon_run(
                daemon, shutdown_timeout=0.3, join_timeout=0.1, retry_interval=0.05
            ) as run:
                runs.append(run)
        elapsed = time.monotonic() - started
        assert 2 <= len(daemon.shutdown_deadlines) < 1000
        assert len(set(daemon.shutdown_deadlines)) == 1
        assert elapsed < 5
    finally:
        daemon.released.set()
        for run in runs:
            run.join(timeout=5)
    assert runs and not runs[0].is_alive()


def test_managed_daemon_run_fails_the_test_when_the_run_thread_outlives_shutdown():
    daemon = _ScriptedDaemon(["stopped"], run_honors_shutdown=False)
    runs = []
    try:
        with pytest.raises(pytest.fail.Exception, match="still alive") as failure:
            with managed_daemon_run(daemon, shutdown_timeout=1, join_timeout=0.2) as run:
                runs.append(run)
        assert "last shutdown result: 'stopped'" in str(failure.value)
        assert len(daemon.shutdown_deadlines) == 1
        assert runs[0].is_alive()
        assert not runs[0].thread.daemon
    finally:
        daemon.released.set()
        for run in runs:
            run.join(timeout=5)
    assert not runs[0].is_alive()
    assert runs[0].returncode == 0


_FAILING_AFTER_READINESS = """
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import conftest
import pytest
from conftest import SERVICE_PORT, managed_daemon_run
from test_runtime import _fixture, _transferred_claim_fd, _wait_for_record

from nyx import runtime

pytestmark = pytest.mark.usefixtures("isolated_port")


def test_assertion_fails_after_daemon_readiness():
    assert Path(conftest.__file__).resolve() == Path({conftest_path!r})
    assert runtime.PORT != SERVICE_PORT
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        with patch.object(runtime, "_paths", return_value=paths):
            daemon = runtime._Daemon(lease_fd, int((time.monotonic() + 5) * 1_000_000_000))
            with managed_daemon_run(daemon):
                instance = _wait_for_record(paths)
                assert instance.url != runtime.URL, "deliberate failure after readiness"
"""


def test_failing_assertion_after_daemon_readiness_ends_pytest_with_the_failure(tmp_path):
    tests_directory = Path(__file__).resolve().parent
    generated = tmp_path / "test_failing_after_readiness.py"
    generated.write_text(
        _FAILING_AFTER_READINESS.format(
            conftest_path=str(tests_directory / "conftest.py")
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(tests_directory.parent), str(tests_directory), environment.get("PYTHONPATH", "")]
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "conftest",
                "-p",
                "no:cacheprovider",
                "-q",
                "--rootdir",
                str(tmp_path),
                str(generated),
            ],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except subprocess.TimeoutExpired as expired:
        pytest.fail(
            f"pytest was still running {expired.timeout} s after the failing assertion",
            pytrace=False,
        )
    report = completed.stdout[-6000:] + completed.stderr[-2000:]
    assert completed.returncode == 1, report
    assert "AssertionError: deliberate failure after readiness" in completed.stdout, report
    assert "1 failed" in completed.stdout, report


def test_daemon_publishes_authenticated_fixed_url_and_releases_transferred_lease():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        with patch.object(runtime, "_paths", return_value=paths):
            daemon = runtime._Daemon(lease_fd, int((time.monotonic() + 5) * 1_000_000_000))
            with managed_daemon_run(daemon) as run:
                instance = _wait_for_record(paths)
                assert instance.url == runtime.URL
                assert "pid" not in json.loads(
                    paths.runtime_directory.joinpath("instance.json").read_text()
                )
                assert runtime._send_control(instance, "status")["url"] == instance.url
                assert runtime.stop() == "stopped"
                assert run.join(timeout=5) == 0
                probe = runtime._lease_lock(paths, timeout=0.0)
                assert probe.acquire(blocking=False)
                probe.close()
                assert not paths.runtime_directory.joinpath("instance.json").exists()


def _descriptors_referring_to(identity: tuple[int, int]) -> list[int]:
    """List this process's descriptors that refer to one ``(st_dev, st_ino)``."""

    found: list[int] = []
    for name in os.listdir("/proc/self/fd"):
        try:
            details = os.stat(f"/proc/self/fd/{name}")
        except OSError:
            continue
        if (details.st_dev, details.st_ino) == identity:
            found.append(int(name))
    return found


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="the detached service and the /proc descriptor table are Linux-only",
)
@pytest.mark.parametrize("ending", ["normal_stop", "start_failure"])
def test_daemon_owns_one_liveness_pipe_and_releases_it_when_run_returns(ending):
    import fcntl

    original_manager = runtime.CatalogWorkerManager
    built: list[tuple[dict[str, object], object]] = []

    def build_manager(**kwargs):
        manager = original_manager(**kwargs)
        built.append((kwargs, manager))
        return manager

    def fail_server(**_kwargs):
        raise OSError(errno.ENOMEM, "injected listener failure")

    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        with patch.object(runtime, "_paths", return_value=paths):
            with patch.object(runtime, "CatalogWorkerManager", side_effect=build_manager):
                daemon = runtime._Daemon(
                    lease_fd, int((time.monotonic() + 5) * 1_000_000_000)
                )
            assert len(built) == 1
            kwargs, manager = built[0]
            assert daemon.workers is manager
            liveness_fd = kwargs.get("parent_liveness_fd")
            assert isinstance(liveness_fd, int)
            assert fcntl.fcntl(liveness_fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
            details = os.fstat(liveness_fd)
            assert stat.S_ISFIFO(details.st_mode)
            identity = (details.st_dev, details.st_ino)
            assert len(_descriptors_referring_to(identity)) == 2

            if ending == "normal_stop":
                with managed_daemon_run(daemon) as run:
                    _wait_for_record(paths)
                    assert runtime.stop() == "stopped"
                    assert run.join(timeout=5) == 0
            else:
                daemon.application._server_factory = fail_server
                assert daemon.run() == 24
                assert daemon.server is None

        assert _descriptors_referring_to(identity) == []


def test_public_stop_waits_for_terminal_record_removal_and_original_lease_release():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        record = paths.runtime_directory / "instance.json"
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        record_removed = threading.Event()
        allow_terminal_release = threading.Event()
        original_unlink = Path.unlink
        result: list[str] = []
        errors: list[BaseException] = []

        def gate_terminal_removal(path, *args, **kwargs):
            removed = original_unlink(path, *args, **kwargs)
            if path == record:
                record_removed.set()
                assert allow_terminal_release.wait(timeout=3)
            return removed

        with patch.object(runtime, "_paths", return_value=paths), patch.object(
            Path, "unlink", gate_terminal_removal
        ):
            daemon = runtime._Daemon(
                lease_fd, int((time.monotonic() + 10) * 1_000_000_000)
            )
            with managed_daemon_run(daemon) as run:
                _wait_for_record(paths)

                def public_stop() -> None:
                    try:
                        result.append(runtime.stop())
                    except BaseException as error:  # noqa: BLE001 - asserted below
                        errors.append(error)

                stop_thread = threading.Thread(target=public_stop)
                stop_thread.start()
                try:
                    assert record_removed.wait(timeout=3)
                    assert not record.exists()
                    contender = runtime._lease_lock(paths, timeout=0.0)
                    assert not contender.acquire(blocking=False)
                    contender.close()
                    assert stop_thread.is_alive()
                    assert result == []
                    assert errors == []
                finally:
                    allow_terminal_release.set()
                stop_thread.join(timeout=5)
                assert run.join(timeout=5) == 0

        assert not stop_thread.is_alive()
        assert errors == []
        assert result == ["stopped"]
        _assert_claim_available(paths.runtime_directory / "lease.lock")


def test_active_daemon_setup_revalidates_same_root_and_rejects_changed_root():
    with TemporaryDirectory() as temporary:
        paths, _, first = _fixture(Path(temporary))
        second = Path(temporary) / "second"
        second.mkdir()
        before = paths.config_file.read_bytes()
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        with patch.object(runtime, "_paths", return_value=paths):
            daemon = runtime._Daemon(lease_fd, int((time.monotonic() + 5) * 1_000_000_000))
            with managed_daemon_run(daemon) as run:
                _wait_for_record(paths)
                assert runtime.setup(first).specification_root == first.resolve()
                with pytest.raises(runtime.ActiveInstanceError):
                    runtime.setup(second)
                assert paths.config_file.read_bytes() == before
                assert runtime.stop() == "stopped"
                assert run.join(timeout=5) == 0


def test_natural_unhealthy_daemon_keeps_record_and_lease():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        with patch.object(runtime, "_paths", return_value=paths):
            daemon = runtime._Daemon(lease_fd, int((time.monotonic() + 5) * 1_000_000_000))
            with managed_daemon_run(daemon) as run:
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
                assert run.join(timeout=5) == 0


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
            assert configuration == state.Configuration(first.resolve())
        finally:
            lease.close()
        assert configuration.specification_root == first.resolve()
        assert paths.config_file.read_bytes() == before


def test_held_lease_setup_rejects_expiry_after_real_configuration_admission():
    with TemporaryDirectory() as temporary:
        paths, _, first = _fixture(Path(temporary))
        lease_path = paths.runtime_directory / "lease.lock"
        lease_bytes_before = lease_path.read_bytes()
        lease = runtime._lease_lock(paths, timeout=0.0)
        assert lease.acquire(blocking=False)
        assert lease.fd is not None
        records = (
            paths.config_file,
            paths.runtime_directory / "operation.lock",
        )

        def snapshot():
            result = {}
            for path in records:
                details = path.lstat()
                result[path] = (_native_claim._identity(details), path.read_bytes())
            return result

        try:
            before = snapshot()
            lease_before = _native_claim._identity(os.fstat(lease.fd))
            original_admit_directory = state._admit_directory
            startup_timeout = 0.01
            clock_ns = 1_000_000_000_000
            configuration_admitted = False
            deadline_checked_after_admission = False

            def admitting_configuration(
                path, *, create, deadline=None, deadline_ns=None
            ):
                nonlocal configuration_admitted
                result = original_admit_directory(
                    path,
                    create=create,
                    deadline=deadline,
                    deadline_ns=deadline_ns,
                )
                if path == paths.config_directory and not create:
                    configuration_admitted = True
                return result

            def controlled_clock_ns():
                if configuration_admitted:
                    return clock_ns + int(startup_timeout * 1_000_000_000) + 1
                return clock_ns

            def controlled_monotonic_ns():
                return controlled_clock_ns()

            def controlled_monotonic():
                nonlocal deadline_checked_after_admission
                value = controlled_clock_ns()
                if configuration_admitted:
                    deadline_checked_after_admission = True
                return value / 1_000_000_000

            with patch.object(runtime, "_paths", return_value=paths), patch.object(
                runtime, "STARTUP_TIMEOUT", startup_timeout
            ), patch.object(
                state,
                "_admit_directory",
                side_effect=admitting_configuration,
            ), patch.object(
                runtime.time,
                "monotonic_ns",
                side_effect=controlled_monotonic_ns,
            ), patch.object(
                runtime.time,
                "monotonic",
                side_effect=controlled_monotonic,
            ), patch.object(
                state, "_save_configuration", wraps=state._save_configuration
            ) as save_configuration, pytest.raises(
                runtime.UnhealthyInstanceError, match="deadline expired"
            ):
                runtime.setup(first)
            save_configuration.assert_not_called()
            assert configuration_admitted
            assert deadline_checked_after_admission
            assert snapshot() == before
            assert _native_claim._identity(os.fstat(lease.fd)) == lease_before
        finally:
            lease.close()
        assert _native_claim._identity(lease_path.lstat()) == lease_before
        assert lease_path.read_bytes() == lease_bytes_before
        _assert_claim_available(lease_path)


@pytest.mark.parametrize("alias", [state.setup, state.save_configuration])
def test_held_lease_rejects_changed_root_through_state_aliases(alias):
    with TemporaryDirectory() as temporary:
        paths, _, first = _fixture(Path(temporary))
        second = Path(temporary) / "second"
        second.mkdir()
        with patch.object(runtime, "_paths", return_value=paths):
            runtime.setup(first)
        before = paths.config_file.read_bytes()
        lease = runtime._lease_lock(paths, timeout=0.0)
        assert lease.acquire(blocking=False)
        try:
            with patch.object(runtime, "_paths", return_value=paths), pytest.raises(
                runtime.ActiveInstanceError
            ):
                alias(second)
        finally:
            lease.close()
        assert paths.config_file.read_bytes() == before
        assert state.load_configuration(paths).specification_root == first.resolve()


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
            assert configuration == state.Configuration(first.resolve())
        finally:
            lease.close()
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
            assert configuration == state.Configuration(first.resolve())
        finally:
            lease.close()
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
            with managed_daemon_run(daemon) as run:
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
                assert run.join(timeout=5) == 0


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
        try:
            with patch.object(runtime, "_paths", return_value=paths):
                daemon = runtime._Daemon(lease_fd, int((time.monotonic() + 5) * 1_000_000_000))
                with managed_daemon_run(daemon) as run:
                    assert run.join(timeout=8) in (25, 30)
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


def _runtime_directory_snapshot(
    paths: state.StatePaths,
) -> dict[str, tuple[tuple[int, int, int, int], bytes | None]]:
    """Capture each entry's identity and its bytes when they are readable.

    A lock held with a mandatory native claim cannot be opened for reading on
    Windows, so an unreadable entry records no payload.  Its identity is still
    compared, and every readable entry keeps its byte comparison.
    """

    snapshot = {}
    for path in paths.runtime_directory.iterdir():
        details = path.lstat()
        identity = (
            details.st_mode,
            details.st_size,
            details.st_mtime_ns,
            details.st_ctime_ns,
        )
        try:
            payload = path.read_bytes()
        except OSError:
            payload = None
        snapshot[path.name] = identity, payload
    return snapshot


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="SIGKILL not available on this platform",
)
def test_temporary_record_cleaned_up_after_stop(tmp_path):
    """SIGKILL crash leaves .instance.json.*; stop() sweeps it."""
    paths, _, _ = _fixture(tmp_path)
    instance = runtime.Instance(
        "iid", runtime.URL, "cap", "127.0.0.1:43210"
    )
    record_bytes = (
        json.dumps(instance.as_dict(), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()

    script = textwrap.dedent(f"""\
        import os, signal, sys
        sys.path.insert(0, {str(Path(__file__).parents[1])!r})
        from nyx import runtime, state
        from unittest.mock import patch

        runtime._use_port(int(os.environ["NYX_TEST_PORT"]))
        paths = state.state_paths()
        instance = runtime.Instance(
            "iid", runtime.URL, "cap", "127.0.0.1:43210"
        )

        def kill_replace(src, dst):
            os.kill(os.getpid(), signal.SIGKILL)

        with patch.object(os, "replace", kill_replace):
            try:
                runtime._write_instance(paths, instance, deadline=float("inf"))
            except BaseException:
                pass
    """)
    environment = os.environ.copy()
    environment["HOME"] = str(paths.account_home)
    environment["NYX_TEST_PORT"] = str(runtime.PORT)
    environment.pop("PYTHONHOME", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == -signal.SIGKILL
    leftovers = list(paths.runtime_directory.glob(".instance.json.*"))
    assert len(leftovers) == 1
    assert leftovers[0].read_bytes() == record_bytes
    assert not paths.runtime_directory.joinpath("instance.json").exists()

    assert _observe(paths).status == "unknown"
    assert leftovers[0].exists()

    with (
        patch.object(state, "resolve_account_home", return_value=paths.account_home),
        patch.object(state, "_current_uid", return_value=state._current_uid()),
    ):
        assert runtime.stop() == "stopped"

    assert not list(paths.runtime_directory.glob(".instance.json.*"))
    assert _observe(paths).status == "not_running"


def test_temporary_record_swept_before_spawn(tmp_path):
    """Sweep precedes daemon spawn."""
    paths, _, _ = _fixture(tmp_path)
    temp = paths.runtime_directory / ".instance.json.abc123"
    temp.write_bytes(b"leftover")
    captured: list[list[str]] = []

    def fail_spawn(*_args, **_kwargs):
        captured.append(list(
            e.name for e in paths.runtime_directory.iterdir()
            if e.name.startswith(".instance.json.")
        ))
        raise RuntimeError("sentinel")

    with (
        patch.object(runtime, "_paths", return_value=paths),
        patch.object(runtime, "_spawn_daemon", fail_spawn),
        pytest.raises(RuntimeError, match="sentinel"),
    ):
        runtime.start()
    assert captured == [[]]
    assert not temp.exists()


_LIFECYCLE_OPERATIONS = pytest.mark.parametrize(
    "operation", ["start", "stop"], ids=["start", "stop"]
)


def _refusing_spawn(*_args, **_kwargs):
    raise AssertionError("daemon spawn must not be reached")


def _run_lifecycle(paths: state.StatePaths, operation: str) -> str:
    with (
        patch.object(state, "resolve_account_home", return_value=paths.account_home),
        patch.object(state, "_current_uid", return_value=state._current_uid()),
        patch.object(runtime, "_spawn_daemon", _refusing_spawn),
    ):
        return getattr(runtime, operation)()


@_LIFECYCLE_OPERATIONS
def test_temporary_record_not_swept_while_lease_held(tmp_path, operation):
    """Held lease means a live writer; sweep must not run."""
    paths, _, _ = _fixture(tmp_path)
    lease_fd, lease = _held_lease(paths)
    temp = paths.runtime_directory / ".instance.json.abc123"
    temp.write_bytes(b"leftover")
    try:
        with pytest.raises(
            runtime.UnhealthyInstanceError, match=r"^Nyx instance is not ready$"
        ):
            _run_lifecycle(paths, operation)
        assert temp.read_bytes() == b"leftover"
        assert not paths.runtime_directory.joinpath("instance.json").exists()
    finally:
        lease.close()
        os.close(lease_fd)


def test_invalid_record_refused_by_start_retains_record_and_leftover(tmp_path):
    """Start refuses a present invalid record and blocks the sweep."""
    paths, _, _ = _fixture(tmp_path)
    record = paths.runtime_directory / "instance.json"
    leftover = paths.runtime_directory / ".instance.json.leftover"
    leftover.write_bytes(b"leftover")
    record.write_bytes(b"not json")

    with pytest.raises(
        runtime.UnhealthyInstanceError,
        match=r"^Nyx instance record is unavailable; run 'nyx --stop' to recover$",
    ):
        _run_lifecycle(paths, "start")

    assert record.read_bytes() == b"not json"
    assert leftover.read_bytes() == b"leftover"


def test_invalid_record_removed_by_stop_and_leftover_swept(tmp_path):
    """Stop removes an invalid regular-file record and sweeps the leftover."""
    paths, _, _ = _fixture(tmp_path)
    record = paths.runtime_directory / "instance.json"
    leftover = paths.runtime_directory / ".instance.json.leftover"
    leftover.write_bytes(b"leftover")
    record.write_bytes(b"not json")

    assert _run_lifecycle(paths, "stop") == "stopped"

    assert not record.exists()
    assert not leftover.exists()


@_LIFECYCLE_OPERATIONS
@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_unsafe_record_refused_by_both_lifecycles(tmp_path, operation, kind):
    """A non-regular record stays unsafe; record and leftover remain."""
    paths, _, _ = _fixture(tmp_path)
    record = paths.runtime_directory / "instance.json"
    leftover = paths.runtime_directory / ".instance.json.leftover"
    leftover.write_bytes(b"leftover")
    target = tmp_path / "foreign-record.json"
    if kind == "directory":
        record.mkdir()
    else:
        _write_runtime_record(paths)
        target.write_bytes(record.read_bytes())
        record.unlink()
        record.symlink_to(target)
    target_bytes = target.read_bytes() if kind == "symlink" else None

    with pytest.raises(
        runtime.UnhealthyInstanceError, match=r"^Nyx instance record is unsafe$"
    ):
        _run_lifecycle(paths, operation)

    assert leftover.read_bytes() == b"leftover"
    if kind == "directory":
        assert record.is_dir()
        assert list(record.iterdir()) == []
    else:
        assert record.is_symlink()
        assert os.path.samefile(record, target)
        assert target.read_bytes() == target_bytes


@pytest.mark.skipif(sys.platform != "win32", reason="requires a native Windows junction")
def test_free_lease_stop_refuses_native_record_junction(tmp_path):
    """A junction record is non-regular and stays refused as unsafe."""
    paths, _, _ = _fixture(tmp_path)
    record = paths.runtime_directory / "instance.json"
    target = tmp_path / "foreign-record"
    target.mkdir()
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(record), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert record.is_junction()

    with patch.object(runtime, "_paths", return_value=paths), pytest.raises(
        runtime.UnhealthyInstanceError, match=r"^Nyx instance record is unsafe$"
    ):
        runtime.stop()

    assert record.is_junction()
    assert list(target.iterdir()) == []


@pytest.mark.parametrize(
    ("payload", "mode"),
    [
        pytest.param(
            b'{"schema_version": 1, "instance_id": "trunc',
            None,
            id="truncated-json",
        ),
        pytest.param(
            json.dumps({"schema_version": 99, "unexpected": True}).encode("utf-8"),
            None,
            id="wrong-schema",
        ),
        pytest.param(b"\xff\xfe\x00\x01", None, id="non-utf8"),
        pytest.param(
            None,
            0o000,
            id="mode-000",
            marks=pytest.mark.skipif(
                os.name == "nt" or os.geteuid() == 0,
                reason="requires POSIX permissions enforced for a non-root user",
            ),
        ),
    ],
)
def test_free_lease_stop_removes_each_invalid_record_form(tmp_path, payload, mode):
    """Stop removes any regular-file record without reading or validating it."""
    paths, _, _ = _fixture(tmp_path)
    record = paths.runtime_directory / "instance.json"
    leftover = paths.runtime_directory / ".instance.json.leftover"
    leftover.write_bytes(b"leftover")
    if payload is None:
        _write_runtime_record(paths)
        record.chmod(mode)
    else:
        record.write_bytes(payload)
    try:
        with patch.object(runtime, "_paths", return_value=paths), patch.object(
            runtime, "_send_control"
        ) as send_control, patch.object(
            runtime.socket, "socket", side_effect=AssertionError("socket use")
        ) as create_socket:
            assert runtime.stop() == "stopped"
        send_control.assert_not_called()
        create_socket.assert_not_called()
        assert not record.exists()
        assert not leftover.exists()
    finally:
        if record.exists():
            record.chmod(0o600)


def test_held_lease_retains_invalid_record_and_refuses_stop(tmp_path):
    """A held lifetime lease leaves an invalid record untouched."""
    paths, _, _ = _fixture(tmp_path)
    lease_fd, lease = _held_lease(paths)
    record = paths.runtime_directory / "instance.json"
    record.write_bytes(b"not json")
    before = record.read_bytes()
    try:
        with patch.object(runtime, "_paths", return_value=paths), pytest.raises(
            runtime.UnhealthyInstanceError,
            match=r"^Nyx instance record is unavailable$",
        ):
            runtime.stop()
        assert record.read_bytes() == before
    finally:
        lease.close()
        os.close(lease_fd)


@_LIFECYCLE_OPERATIONS
def test_record_swap_before_unlink_aborts_with_changed(tmp_path, operation):
    """The identity recheck is the last step before the unlink."""
    paths, _, _ = _fixture(tmp_path)
    record = paths.runtime_directory / "instance.json"
    _write_runtime_record(paths)
    leftover = paths.runtime_directory / ".instance.json.leftover"
    leftover.write_bytes(b"leftover")
    real_require_deadline = runtime._require_deadline
    checks: list[float] = []

    def swap_at_removal(deadline):
        checks.append(deadline)
        if len(checks) == 2:
            record.write_bytes(b"replacement-record")
        real_require_deadline(deadline)

    with patch.object(runtime, "_paths", return_value=paths), patch.object(
        runtime, "_require_deadline", swap_at_removal
    ), pytest.raises(
        runtime.UnhealthyInstanceError, match=r"^Nyx instance record changed$"
    ):
        _run_lifecycle(paths, operation)

    assert len(checks) >= 2
    assert record.read_bytes() == b"replacement-record"
    assert leftover.read_bytes() == b"leftover"


@_LIFECYCLE_OPERATIONS
@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_record_type_swap_before_unlink_aborts_with_changed(tmp_path, operation, kind):
    """A record that became non-regular after the snapshot is changed, not removed."""
    paths, _, _ = _fixture(tmp_path)
    record = paths.runtime_directory / "instance.json"
    _write_runtime_record(paths)
    leftover = paths.runtime_directory / ".instance.json.leftover"
    leftover.write_bytes(b"leftover")
    foreign = tmp_path / "foreign-record"
    if kind == "symlink":
        foreign.write_bytes(b"foreign-bytes")
    else:
        foreign.mkdir()
    real_require_deadline = runtime._require_deadline
    checks: list[float] = []

    def swap_type_at_removal(deadline):
        checks.append(deadline)
        if len(checks) == 2:
            record.unlink()
            if kind == "symlink":
                record.symlink_to(foreign)
            else:
                record.mkdir()
        real_require_deadline(deadline)

    with patch.object(runtime, "_paths", return_value=paths), patch.object(
        runtime, "_require_deadline", swap_type_at_removal
    ), pytest.raises(
        runtime.UnhealthyInstanceError, match=r"^Nyx instance record changed$"
    ):
        _run_lifecycle(paths, operation)

    assert len(checks) >= 2
    assert leftover.read_bytes() == b"leftover"
    if kind == "symlink":
        assert record.is_symlink()
        assert foreign.read_bytes() == b"foreign-bytes"
    else:
        assert record.is_dir()
        assert list(foreign.iterdir()) == []


@_LIFECYCLE_OPERATIONS
@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_record_type_change_before_snapshot_aborts_with_changed(
    tmp_path, operation, kind
):
    """A type change between the type check and the snapshot is a replacement."""
    paths, _, _ = _fixture(tmp_path)
    record = paths.runtime_directory / "instance.json"
    _write_runtime_record(paths)
    leftover = paths.runtime_directory / ".instance.json.leftover"
    leftover.write_bytes(b"leftover")
    foreign = tmp_path / "foreign-record"
    if kind == "symlink":
        foreign.write_bytes(b"foreign-bytes")
    else:
        foreign.mkdir()
    real_lstat = Path.lstat
    swaps: list[bool] = []

    def swap_after_type_check(path, *args, **kwargs):
        details = real_lstat(path, *args, **kwargs)
        if path == record and not swaps:
            record.unlink()
            if kind == "symlink":
                record.symlink_to(foreign)
            else:
                record.mkdir()
            swaps.append(True)
        return details

    with patch.object(runtime, "_paths", return_value=paths), patch.object(
        Path, "lstat", swap_after_type_check
    ), pytest.raises(
        runtime.UnhealthyInstanceError, match=r"^Nyx instance record changed$"
    ):
        _run_lifecycle(paths, operation)

    assert swaps == [True]
    assert leftover.read_bytes() == b"leftover"
    if kind == "symlink":
        assert record.is_symlink()
        assert foreign.read_bytes() == b"foreign-bytes"
    else:
        assert record.is_dir()
        assert list(foreign.iterdir()) == []


class _ReparseStat:
    """A stat result carrying the reparse attribute on any host."""

    def __init__(self, details):
        self._details = details
        self.st_file_attributes = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    def __getattr__(self, name):
        return getattr(self._details, name)


@_LIFECYCLE_OPERATIONS
def test_regular_file_reparse_record_refused_as_unsafe(tmp_path, operation):
    """A regular-file reparse point is unsafe, not a raw runtime error."""
    paths, _, _ = _fixture(tmp_path)
    record = paths.runtime_directory / "instance.json"
    _write_runtime_record(paths)
    before = record.read_bytes()
    leftover = paths.runtime_directory / ".instance.json.leftover"
    leftover.write_bytes(b"leftover")
    real_lstat = Path.lstat

    def reparse_lstat(path, *args, **kwargs):
        details = real_lstat(path, *args, **kwargs)
        if path == record:
            return _ReparseStat(details)
        return details

    with patch.object(runtime, "_paths", return_value=paths), patch.object(
        Path, "lstat", reparse_lstat
    ), pytest.raises(
        runtime.UnhealthyInstanceError, match=r"^Nyx instance record is unsafe$"
    ):
        _run_lifecycle(paths, operation)

    assert record.read_bytes() == before
    assert leftover.read_bytes() == b"leftover"


def test_record_vanish_before_unlink_is_absent_for_stop(tmp_path):
    """A record that vanished before the unlink is the absent case."""
    paths, _, _ = _fixture(tmp_path)
    record = paths.runtime_directory / "instance.json"
    _write_runtime_record(paths)
    leftover = paths.runtime_directory / ".instance.json.leftover"
    leftover.write_bytes(b"leftover")
    real_require_deadline = runtime._require_deadline
    checks: list[float] = []

    def vanish_at_removal(deadline):
        checks.append(deadline)
        if len(checks) == 2:
            record.unlink()
        real_require_deadline(deadline)

    with patch.object(runtime, "_paths", return_value=paths), patch.object(
        runtime, "_require_deadline", vanish_at_removal
    ):
        assert runtime.stop() == "stopped"

    assert len(checks) >= 2
    assert not record.exists()
    assert not leftover.exists()


def test_record_vanish_before_unlink_proceeds_to_spawn(tmp_path):
    """A record that vanished before the unlink lets start reach the spawn."""
    paths, _, _ = _fixture(tmp_path)
    record = paths.runtime_directory / "instance.json"
    _write_runtime_record(paths)
    leftover = paths.runtime_directory / ".instance.json.leftover"
    leftover.write_bytes(b"leftover")
    real_require_deadline = runtime._require_deadline
    checks: list[float] = []
    captured: list[list[str]] = []

    def vanish_at_removal(deadline):
        checks.append(deadline)
        if len(checks) == 2:
            record.unlink()
        real_require_deadline(deadline)

    def record_spawn(*_args, **_kwargs):
        captured.append(sorted(entry.name for entry in paths.runtime_directory.iterdir()))
        raise RuntimeError("sentinel")

    with patch.object(runtime, "_paths", return_value=paths), patch.object(
        runtime, "_require_deadline", vanish_at_removal
    ), patch.object(runtime, "_spawn_daemon", record_spawn), pytest.raises(
        RuntimeError, match="sentinel"
    ):
        runtime.start()

    assert len(checks) >= 2
    assert captured == [["lease.lock", "operation.lock"]]
    assert not record.exists()


def test_record_vanish_during_start_validation_is_absent(tmp_path):
    """A record gone when start validates is absent, not unready."""
    paths, _, _ = _fixture(tmp_path)
    record = paths.runtime_directory / "instance.json"
    _write_runtime_record(paths)
    leftover = paths.runtime_directory / ".instance.json.leftover"
    leftover.write_bytes(b"leftover")
    real_read_instance = runtime._read_instance
    reads: list[object] = []
    captured: list[list[str]] = []

    def vanish_on_validation(state_paths, *, directory=None):
        reads.append(state_paths)
        if len(reads) == 1:
            record.unlink()
            raise runtime.UnhealthyInstanceError(
                "Nyx instance is not ready"
            ) from FileNotFoundError(record)
        return real_read_instance(state_paths, directory=directory)

    def record_spawn(*_args, **_kwargs):
        captured.append(sorted(entry.name for entry in paths.runtime_directory.iterdir()))
        raise RuntimeError("sentinel")

    with patch.object(runtime, "_paths", return_value=paths), patch.object(
        runtime, "_read_instance", vanish_on_validation
    ), patch.object(runtime, "_spawn_daemon", record_spawn), pytest.raises(
        RuntimeError, match="sentinel"
    ):
        runtime.start()

    assert len(reads) == 1
    assert captured == [["lease.lock", "operation.lock"]]
    assert not record.exists()


@pytest.mark.parametrize(
    ("error_type", "error_number", "error_message"),
    [
        pytest.param(PermissionError, 13, "EACCES", id="eacces"),
        pytest.param(OSError, 30, "EROFS", id="erofs"),
        pytest.param(IsADirectoryError, 21, "EISDIR", id="eisdir"),
    ],
)
@_LIFECYCLE_OPERATIONS
def test_failed_removal_retains_record_and_leftover(
    tmp_path, operation, error_type, error_number, error_message
):
    """A refused unlink keeps the record and the leftover temporary."""
    paths, _, _ = _fixture(tmp_path)
    record = paths.runtime_directory / "instance.json"
    _write_runtime_record(paths)
    leftover = paths.runtime_directory / ".instance.json.leftover"
    leftover.write_bytes(b"leftover")
    real_unlink = Path.unlink

    def refuse_record_unlink(path, *args, **kwargs):
        if path == record:
            raise error_type(error_number, error_message)
        return real_unlink(path, *args, **kwargs)

    with patch.object(runtime, "_paths", return_value=paths), patch.object(
        Path, "unlink", refuse_record_unlink
    ), pytest.raises(
        runtime.RuntimeErrorBase,
        match=r"^Nyx stale instance record cannot be removed$",
    ) as raised:
        _run_lifecycle(paths, operation)

    assert type(raised.value) is runtime.RuntimeErrorBase
    assert record.exists()
    assert leftover.read_bytes() == b"leftover"


@_LIFECYCLE_OPERATIONS
def test_removal_deadline_expiry_retains_record_and_leftover(tmp_path, operation):
    """An expired deadline is not reported as an invalid record."""
    paths, _, _ = _fixture(tmp_path)
    record = paths.runtime_directory / "instance.json"
    _write_runtime_record(paths)
    leftover = paths.runtime_directory / ".instance.json.leftover"
    leftover.write_bytes(b"leftover")
    real_require_deadline = runtime._require_deadline
    checks: list[float] = []

    def expire_at_removal(deadline):
        checks.append(deadline)
        if len(checks) == 2:
            raise runtime._ControlTimeoutError("control deadline expired")
        real_require_deadline(deadline)

    with patch.object(runtime, "_paths", return_value=paths), patch.object(
        runtime, "_require_deadline", expire_at_removal
    ), pytest.raises(
        runtime._ControlTimeoutError, match=r"^control deadline expired$"
    ):
        _run_lifecycle(paths, operation)

    assert len(checks) >= 2
    assert record.exists()
    assert leftover.read_bytes() == b"leftover"


@_LIFECYCLE_OPERATIONS
def test_timeout_is_never_reported_as_an_invalid_record(tmp_path, operation):
    """A deadline failure keeps its own outcome, not the refusal message."""
    paths, _, _ = _fixture(tmp_path)
    record = paths.runtime_directory / "instance.json"
    _write_runtime_record(paths)
    real_require_deadline = runtime._require_deadline
    checks: list[float] = []

    def expire_at_removal(deadline):
        checks.append(deadline)
        if len(checks) == 2:
            raise runtime._ControlTimeoutError(runtime._RECORD_UNAVAILABLE)
        real_require_deadline(deadline)

    with patch.object(runtime, "_paths", return_value=paths), patch.object(
        runtime, "_require_deadline", expire_at_removal
    ), pytest.raises(runtime._ControlTimeoutError) as raised:
        _run_lifecycle(paths, operation)

    assert type(raised.value) is runtime._ControlTimeoutError
    assert str(raised.value) == runtime._RECORD_UNAVAILABLE
    assert record.exists()


@_LIFECYCLE_OPERATIONS
def test_temporary_record_directory_with_prefix_raises(tmp_path, operation):
    """A directory named like an atomic-write temporary is a bounded error."""
    paths, _, _ = _fixture(tmp_path)
    bad = paths.runtime_directory / ".instance.json.x"
    bad.mkdir()
    with pytest.raises(
        runtime.RuntimeErrorBase,
        match=r"^Nyx stale instance record cannot be removed$",
    ) as raised:
        _run_lifecycle(paths, operation)
    assert type(raised.value) is runtime.RuntimeErrorBase
    assert bad.is_dir()
    assert list(bad.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory permissions")
@pytest.mark.parametrize("argv", [[], ["--stop"]], ids=["start", "stop"])
def test_unlistable_runtime_directory_reports_bounded_cli_error(
    tmp_path, monkeypatch, capsys, argv
):
    """An unreadable runtime directory refuses the sweep with a CLI error."""
    from nyx import cli

    paths, _, _ = _fixture(tmp_path)
    monkeypatch.setattr(runtime, "desktop_host", lambda: False)
    spawned: list[object] = []

    def refuse_spawn(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("daemon spawn must not be reached")

    directory = paths.runtime_directory
    original_mode = stat.S_IMODE(directory.stat().st_mode)
    directory.chmod(0o300)
    try:
        try:
            os.listdir(directory)
        except PermissionError:
            pass
        else:
            pytest.skip("directory read permission is not enforced (root?)")
        with (
            patch.object(state, "resolve_account_home", return_value=paths.account_home),
            patch.object(state, "_current_uid", return_value=state._current_uid()),
            patch.object(runtime, "_spawn_daemon", refuse_spawn),
        ):
            result = cli.main(argv)
    finally:
        directory.chmod(original_mode)

    captured = capsys.readouterr()
    assert result == 1
    assert captured.err == "nyx: Nyx stale instance record cannot be removed\n"
    assert captured.out == ""
    assert spawned == []


@pytest.mark.parametrize("damage", ["absent", "malformed", "replaced"])
def test_live_locator_damage_never_authorizes_public_lifecycle_replacement(damage):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        paths, home, first = _fixture(root)
        record = paths.runtime_directory / "instance.json"
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        try:
            with patch.object(runtime, "_paths", return_value=paths):
                daemon = runtime._Daemon(
                    lease_fd,
                    int((time.monotonic() + 10) * 1_000_000_000),
                )
        except BaseException:
            # The running daemon owns the transferred descriptor and closes it
            # in _Daemon.run(); the test owns it only if construction failed.
            os.close(lease_fd)
            raise
        with patch.object(runtime, "_paths", return_value=paths), managed_daemon_run(
            daemon
        ) as run:
            instance = _wait_for_record(paths)
            original_bytes = record.read_bytes()
            if damage == "absent":
                record.unlink()
                damaged_bytes = None
            elif damage == "malformed":
                damaged_bytes = b"not-json\n"
                record.write_bytes(damaged_bytes)
            else:
                foreign_port_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                foreign_port_socket.bind(("127.0.0.1", 0))
                foreign_port = foreign_port_socket.getsockname()[1]
                foreign_port_socket.close()
                damaged = runtime.Instance(
                    "replacement",
                    runtime.URL,
                    "foreign-capability",
                    runtime._control_endpoint(foreign_port),
                )
                damaged_bytes = (
                    json.dumps(damaged.as_dict(), sort_keys=True, separators=(",", ":")) + "\n"
                ).encode()
                record.write_bytes(damaged_bytes)
            configuration_before = paths.config_file.read_bytes()
            with patch.object(state, "resolve_account_home", return_value=home):
                assert runtime.setup(first).specification_root == first.resolve()
                assert paths.config_file.read_bytes() == configuration_before
                with pytest.raises(runtime.UnhealthyInstanceError):
                    runtime.start()
                with pytest.raises(runtime.UnhealthyInstanceError):
                    runtime.stop()
            if damage == "absent":
                assert not record.exists()
            else:
                assert record.read_bytes() == damaged_bytes
            assert paths.config_file.read_bytes() == configuration_before
            assert instance == daemon.instance

            # Restore only the originally published locator so the real
            # authenticated control path can terminate the still-live daemon.
            record.write_bytes(original_bytes)
            with patch.object(state, "resolve_account_home", return_value=home):
                assert runtime.stop() == "stopped"
            assert not record.exists()
            assert run.join(timeout=5) == 0
            assert daemon.control_thread is not None
            assert not daemon.control_thread.is_alive()
            assert daemon.http_thread is not None
            assert not daemon.http_thread.is_alive()
            assert daemon.control is not None
            assert daemon.control.fileno() == -1
            assert daemon.server is not None
            assert daemon.server.fileno() == -1
            assert daemon.workers.active_count == 0
            _assert_claim_available(paths.runtime_directory / "lease.lock")


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
            ), patch.object(runtime, "SHUTDOWN_TIMEOUT", 1.0), pytest.raises(
                runtime.UnhealthyInstanceError, match="record changed"
            ):
                runtime.stop()
            assert record.read_bytes() == (
                json.dumps(replacement.as_dict(), sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            contender = runtime._lease_lock(paths, timeout=0.0)
            assert not contender.acquire(blocking=False)
            contender.close()
        finally:
            os.close(lease_fd)
        assert record.read_bytes() == (
            json.dumps(replacement.as_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()


def test_record_snapshot_reads_without_exclusive_path_open():
    with TemporaryDirectory() as temporary:
        record = Path(temporary) / "instance.json"
        record.write_bytes(b'{"schema_version": 1}\n')
        with patch.object(
            Path, "read_bytes", side_effect=AssertionError("exclusive path read")
        ):
            snapshot = runtime._record_snapshot(record)
        assert snapshot.data == b'{"schema_version": 1}\n'


@pytest.mark.skipif(os.name != "nt", reason="Windows share-delete read proof")
def test_shared_record_read_permits_terminal_unlink():
    with TemporaryDirectory() as temporary:
        record = Path(temporary) / "instance.json"
        record.write_bytes(b'{"schema_version": 1}\n')
        descriptor = _native_claim.open_existing_read(record)
        try:
            assert os.read(descriptor, 4096) == b'{"schema_version": 1}\n'
            os.unlink(record)
            assert not record.exists()
        finally:
            os.close(descriptor)


def _wait_for_clock_advance(limit: float = 5.0) -> None:
    """Return only once time.monotonic() has moved past its value on entry.

    Bounded so a coarse clock (about 15.6 ms on Windows) still advances.
    """
    entry = time.monotonic()
    give_up = entry + limit
    while time.monotonic() <= entry:
        assert time.monotonic() < give_up, "monotonic clock did not advance"
        time.sleep(0.001)


def test_public_start_hands_one_deadline_to_every_phase():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        seen: dict[str, float] = {}
        real_operation_lock = runtime._operation_lock
        real_lease_lock = runtime._lease_lock
        real_remove_stale = runtime._remove_stale_instance

        def recording_paths(**kwargs):
            seen["paths"] = kwargs["deadline"]
            assert kwargs["deadline_ns"] / 1_000_000_000 == kwargs["deadline"]
            return paths

        def recording_operation_lock(lock_paths, **kwargs):
            seen["operation_lock"] = kwargs["deadline"]
            return real_operation_lock(lock_paths, **kwargs)

        def recording_lease_lock(lock_paths, **kwargs):
            seen["lease_lock"] = kwargs["deadline"]
            return real_lease_lock(lock_paths, **kwargs)

        def recording_remove_stale(stale_paths, **kwargs):
            seen["remove_stale"] = kwargs["deadline"]
            return real_remove_stale(stale_paths, **kwargs)

        def spawn(lease_fd, deadline_ns, **_kwargs):
            seen["spawn"] = deadline_ns / 1_000_000_000
            return SimpleNamespace()

        def acknowledge(_process, _ack_fd, deadline):
            seen["ack"] = deadline
            _wait_for_clock_advance()
            return True

        def ready(_paths, _process, deadline):
            seen["ready"] = deadline
            return SimpleNamespace(url=runtime.URL)

        with patch.object(
            runtime, "_paths", side_effect=recording_paths
        ), patch.object(
            runtime, "_operation_lock", side_effect=recording_operation_lock
        ), patch.object(
            runtime, "_lease_lock", side_effect=recording_lease_lock
        ), patch.object(
            runtime, "_remove_stale_instance", side_effect=recording_remove_stale
        ), patch.object(
            runtime, "_spawn_daemon", side_effect=spawn
        ), patch.object(
            runtime, "_wait_for_ack", side_effect=acknowledge
        ), patch.object(runtime, "_wait_ready", side_effect=ready):
            assert runtime.start() == runtime.URL
        assert set(seen) == {
            "paths",
            "operation_lock",
            "lease_lock",
            "remove_stale",
            "spawn",
            "ack",
            "ready",
        }
        assert {seen[name] for name in seen} == {seen["spawn"]}


def test_public_stop_accepts_fast_owned_terminal_absence_after_lease_release():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        original = _write_runtime_record(paths)
        record = paths.runtime_directory / "instance.json"
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")

        control_deadlines: list[float] = []
        lock_calls: list[tuple[float | None, float]] = []
        real_operation_lock = runtime._operation_lock

        def recording_operation_lock(lock_paths, **kwargs):
            lock_calls.append(
                (kwargs.get("deadline"), kwargs.get("timeout", runtime.LOCK_TIMEOUT))
            )
            return real_operation_lock(lock_paths, **kwargs)

        def complete_terminal_cleanup(instance, command, **_kwargs):
            nonlocal lease_fd
            assert instance == original
            assert command == "stop"
            control_deadlines.append(_kwargs["deadline"])
            _wait_for_clock_advance()
            record.unlink()
            os.close(lease_fd)
            lease_fd = -1
            return {
                "status": "stopping",
                "instance_id": original.instance_id,
                "url": runtime.URL,
            }

        try:
            with patch.object(runtime, "_paths", return_value=paths), patch.object(
                runtime, "_send_control", side_effect=complete_terminal_cleanup
            ), patch.object(
                runtime, "_operation_lock", side_effect=recording_operation_lock
            ):
                assert runtime.stop() == "stopped"
        finally:
            if lease_fd >= 0:
                os.close(lease_fd)
        assert not record.exists()
        _assert_claim_available(paths.runtime_directory / "lease.lock")
        assert len(control_deadlines) == 1
        assert lock_calls
        assert {deadline for deadline, _ in lock_calls} == set(control_deadlines)
        assert any(timeout == 0.0 for _, timeout in lock_calls)


def test_public_stop_accepts_record_disappearance_during_terminal_snapshot():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        original = _write_runtime_record(paths)
        record = paths.runtime_directory / "instance.json"
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        original_read_descriptor = runtime._read_descriptor
        terminal_read = False

        def acknowledge_stop(instance, command, **_kwargs):
            nonlocal terminal_read
            assert instance == original
            assert command == "stop"
            terminal_read = True
            return {
                "status": "stopping",
                "instance_id": original.instance_id,
                "url": runtime.URL,
            }

        def remove_during_read(fd):
            nonlocal lease_fd, terminal_read
            if terminal_read:
                terminal_read = False
                record.unlink()
                os.close(lease_fd)
                lease_fd = -1
            return original_read_descriptor(fd)

        try:
            with patch.object(runtime, "_paths", return_value=paths), patch.object(
                runtime, "_send_control", side_effect=acknowledge_stop
            ), patch.object(runtime, "_read_descriptor", remove_during_read):
                assert runtime.stop() == "stopped"
        finally:
            if lease_fd >= 0:
                os.close(lease_fd)
        assert not record.exists()
        _assert_claim_available(paths.runtime_directory / "lease.lock")


def test_public_stop_waits_directly_for_original_lease_after_terminal_absence():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        original = _write_runtime_record(paths)
        record = paths.runtime_directory / "instance.json"
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        original_existing_lock = runtime._ExistingLock
        blocking_wait_started = threading.Event()
        release_thread = None

        class ObservedExistingLock(original_existing_lock):
            def acquire(self, *, blocking=False, deadline=None):
                if blocking:
                    blocking_wait_started.set()
                return super().acquire(blocking=blocking, deadline=deadline)

        def complete_terminal_record_removal(instance, command, **_kwargs):
            nonlocal lease_fd, release_thread
            assert instance == original
            assert command == "stop"
            record.unlink()

            def release_original_lease():
                nonlocal lease_fd
                if blocking_wait_started.wait(timeout=1):
                    os.close(lease_fd)
                    lease_fd = -1

            release_thread = threading.Thread(target=release_original_lease)
            release_thread.start()
            return {
                "status": "stopping",
                "instance_id": original.instance_id,
                "url": runtime.URL,
            }

        try:
            with patch.object(runtime, "_paths", return_value=paths), patch.object(
                runtime, "_send_control", side_effect=complete_terminal_record_removal
            ), patch.object(runtime, "_ExistingLock", ObservedExistingLock), patch.object(
                runtime, "SHUTDOWN_TIMEOUT", 0.2
            ):
                assert runtime.stop() == "stopped"
        finally:
            if release_thread is not None:
                release_thread.join(timeout=2)
            if lease_fd >= 0:
                os.close(lease_fd)
        assert blocking_wait_started.is_set()
        assert release_thread is not None and not release_thread.is_alive()
        assert not record.exists()
        _assert_claim_available(paths.runtime_directory / "lease.lock")


def test_public_stop_does_not_accept_absence_while_original_lease_remains_held():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        original = _write_runtime_record(paths)
        record = paths.runtime_directory / "instance.json"
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")

        def remove_without_releasing(instance, command, **_kwargs):
            assert instance == original
            assert command == "stop"
            record.unlink()
            return {
                "status": "stopping",
                "instance_id": original.instance_id,
                "url": runtime.URL,
            }

        try:
            with patch.object(runtime, "_paths", return_value=paths), patch.object(
                runtime, "_send_control", side_effect=remove_without_releasing
            ), patch.object(runtime, "SHUTDOWN_TIMEOUT", 0.05), pytest.raises(
                runtime.UnhealthyInstanceError, match="record changed"
            ):
                runtime.stop()
            contender = runtime._lease_lock(paths, timeout=0.0)
            assert not contender.acquire(blocking=False)
            contender.close()
        finally:
            os.close(lease_fd)
        assert not record.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permits held-path replacement")
def test_public_stop_rejects_replacement_of_original_terminal_lease_object():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        paths, _, _ = _fixture(root)
        original = _write_runtime_record(paths)
        record = paths.runtime_directory / "instance.json"
        lease_path = paths.runtime_directory / "lease.lock"
        lease_fd = _transferred_claim_fd(lease_path)
        replacement = root / "replacement-lease.lock"
        replacement.write_bytes(b"replacement")

        def substitute_terminal_lease(instance, command, **_kwargs):
            nonlocal lease_fd
            assert instance == original
            assert command == "stop"
            record.unlink()
            os.replace(replacement, lease_path)
            os.close(lease_fd)
            lease_fd = -1
            return {
                "status": "stopping",
                "instance_id": original.instance_id,
                "url": runtime.URL,
            }

        try:
            with patch.object(runtime, "_paths", return_value=paths), patch.object(
                runtime, "_send_control", side_effect=substitute_terminal_lease
            ), pytest.raises(runtime.UnhealthyInstanceError, match="lifetime lease changed"):
                runtime.stop()
        finally:
            if lease_fd >= 0:
                os.close(lease_fd)
        assert not record.exists()
        assert lease_path.read_bytes() == b"replacement"
        _assert_claim_available(lease_path)


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0,
    reason="requires POSIX permissions enforced for a non-root user",
)
@pytest.mark.parametrize("command", ["start", "stop", "cli-start", "cli-stop"])
def test_unreadable_live_record_reports_unavailable_without_mutation(command, capsys):
    from nyx import cli

    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd, lease = _held_lease(paths)
        _write_runtime_record(paths)
        record = paths.runtime_directory / "instance.json"
        before = record.read_bytes()
        try:
            record.chmod(0o000)
            with patch.object(runtime, "_paths", return_value=paths), patch.object(
                runtime, "desktop_host", return_value=False
            ), patch.object(runtime, "_send_control") as send_control:
                if command.startswith("cli-"):
                    arguments = [] if command == "cli-start" else ["--stop"]
                    assert cli.main(arguments) == 1
                    captured = capsys.readouterr()
                    assert captured.out == ""
                    assert captured.err == "nyx: Nyx instance record is unavailable\n"
                else:
                    with pytest.raises(runtime.UnhealthyInstanceError) as error:
                        getattr(runtime, command)()
                    assert str(error.value) == "Nyx instance record is unavailable"
                    assert isinstance(error.value.__cause__, PermissionError)
                send_control.assert_not_called()
            record.chmod(0o600)
            assert record.read_bytes() == before
            fresh_lease = runtime._lease_lock(paths, timeout=0.0)
            try:
                assert fresh_lease.acquire(blocking=False) is False
            finally:
                fresh_lease.close()
        finally:
            record.chmod(0o600)
            lease.close()
            os.close(lease_fd)


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0,
    reason="requires POSIX symlinks and a non-root user",
)
@pytest.mark.parametrize("command", ["start", "stop"])
def test_unsafe_live_record_preserves_runtime_error_and_symlink(command):
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd, lease = _held_lease(paths)
        _write_runtime_record(paths)
        record = paths.runtime_directory / "instance.json"
        target = paths.runtime_directory / "valid-record.json"
        record.rename(target)
        record.symlink_to(target)
        try:
            with patch.object(runtime, "_paths", return_value=paths), patch.object(
                runtime, "_send_control"
            ) as send_control, pytest.raises(runtime.RuntimeErrorBase) as error:
                getattr(runtime, command)()
            assert type(error.value) is runtime.RuntimeErrorBase
            assert str(error.value) == "unsafe Nyx runtime state"
            assert record.is_symlink()
            send_control.assert_not_called()
        finally:
            lease.close()
            os.close(lease_fd)


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


def test_free_lease_start_refuses_invalid_stale_control_record():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        record = paths.runtime_directory / "instance.json"
        _write_runtime_record(paths, control="127.0.0.1:0")
        before = record.read_bytes()

        with patch.object(runtime, "_paths", return_value=paths), patch.object(
            runtime, "_send_control"
        ) as send_control, patch.object(
            runtime.socket, "socket", side_effect=AssertionError("socket use")
        ) as create_socket, pytest.raises(
            runtime.UnhealthyInstanceError,
            match=r"^Nyx instance record is unavailable; run 'nyx --stop' to recover$",
        ):
            runtime.start()
        send_control.assert_not_called()
        create_socket.assert_not_called()
        assert record.read_bytes() == before


def test_free_lease_stop_removes_invalid_stale_control_record():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        record = paths.runtime_directory / "instance.json"
        _write_runtime_record(paths, control="127.0.0.1:0")

        with patch.object(runtime, "_paths", return_value=paths), patch.object(
            runtime, "_send_control"
        ) as send_control, patch.object(
            runtime.socket, "socket", side_effect=AssertionError("socket use")
        ) as create_socket:
            assert runtime.stop() == "stopped"
        send_control.assert_not_called()
        create_socket.assert_not_called()
        assert not record.exists()


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
        original_open_read = runtime.open_existing_read

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

        def record_open_read(path: Path):
            if path.resolve().is_relative_to(external_runtime_resolved):
                outside_accesses.append(("read_open", path))
            return original_open_read(path)

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
        ), patch.object(
            runtime, "open_existing_read", record_open_read
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
        paths.runtime_directory.joinpath("operation.lock").unlink()
        paths.runtime_directory.joinpath("lease.lock").unlink()
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
        assert paths.runtime_directory.is_junction()
        outside_before = outside_claim.read_bytes()
        outside_accesses: list[tuple[str, Path]] = []
        original_scandir = runtime.NativeDirectory.scandir
        original_open_file = runtime.NativeDirectory.open_file
        original_probe = runtime.NativeClaim.probe

        def record_iteration(directory):
            if directory.path == paths.runtime_directory:
                outside_accesses.append(("iteration", directory.path))
            return original_scandir(directory)

        def record_read(directory, name):
            if directory.path == paths.runtime_directory:
                outside_accesses.append(("read", directory.path / name))
            return original_open_file(directory, name)

        def record_claim_probe(path: Path, *, dir_fd=None):
            if path.parent == paths.runtime_directory:
                outside_accesses.append(("claim", path))
            return original_probe(path, dir_fd=dir_fd)

        try:
            with patch.object(
                state, "resolve_account_home", return_value=paths.account_home
            ), patch.object(
                state, "_current_uid", return_value=state._current_uid()
            ), patch.object(
                runtime.NativeDirectory, "scandir", record_iteration
            ), patch.object(
                runtime.NativeDirectory, "open_file", record_read
            ), patch.object(
                runtime.NativeClaim, "probe", side_effect=record_claim_probe
            ):
                observed = runtime.observe_runtime()
        finally:
            paths.runtime_directory.rmdir()

        assert observed.status == "unknown"
        assert observed.diagnostic == runtime.RUNTIME_STATE_UNAVAILABLE
        assert outside_accesses == []
        assert outside_claim.read_bytes() == outside_before


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


def test_observe_runtime_reports_unavailable_for_free_lease_invalid_record(tmp_path):
    """Status reads only: an invalid record is unknown, not not-running."""
    paths, _, _ = _fixture(tmp_path)
    record = paths.runtime_directory / "instance.json"
    record.write_bytes(b"not json")
    before = _runtime_directory_snapshot(paths)
    assert before["instance.json"][1] == b"not json"

    observed = _observe(paths)

    assert observed.status == "unknown"
    assert observed.diagnostic == runtime.RUNTIME_STATE_UNAVAILABLE
    assert _runtime_directory_snapshot(paths) == before
    assert record.read_bytes() == b"not json"


def test_observe_runtime_reports_changed_for_held_lease_without_record(tmp_path):
    """A held lease without a locator is a changed runtime, not unavailable."""
    paths, _, _ = _fixture(tmp_path)
    lease_fd, lease = _held_lease(paths)
    try:
        before = _runtime_directory_snapshot(paths)
        assert "lease.lock" in before

        observed = _observe(paths)

        assert observed.status == "unknown"
        assert observed.diagnostic == runtime.RUNTIME_STATE_CHANGED
        assert _runtime_directory_snapshot(paths) == before
    finally:
        lease.close()
        os.close(lease_fd)


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
                with patch.object(
                    daemon, "_bind_control", side_effect=bind_occupied_control
                ), managed_daemon_run(daemon) as run:
                    assert run.join(timeout=5) == 26
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
            try:
                with managed_daemon_run(daemon) as run:
                    _wait_for_record(paths)
                    idle.connect(("127.0.0.1", runtime.PORT))
                    assert runtime.stop() == "stopped"
                    assert run.join(timeout=5) == 0
            finally:
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
    cases = (
        (3, "producer_output_too_large"),
        (4, "producer_unavailable"),
        (5, "producer_failed"),
        (6, "producer_failed"),
        (7, "producer_failed"),
    )
    for exit_code, expected in cases:
        manager = runtime.CatalogWorkerManager(
            command_factory=lambda exit_code=exit_code: [
                sys.executable,
                "-c",
                f"import sys; sys.stdout.close(); sys.stderr.close(); sys.exit({exit_code})",
            ],
            timeout=2,
        )
        with pytest.raises(runtime.WorkerError) as raised:
            manager.fetch_catalog()
        assert raised.value.code == expected, exit_code
        assert manager.close(time.monotonic() + 2) is True, exit_code
        assert manager.active_count == 0, exit_code


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


def test_spawn_failure_releases_reservation_before_shared_close_completes():
    manager = runtime.CatalogWorkerManager(
        command_factory=lambda: [sys.executable, "-c", "raise SystemExit(0)"]
    )
    spawned = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def fail_spawn(*_args: object, **_kwargs: object) -> None:
        spawned.set()
        assert release.wait(timeout=3)
        raise OSError("injected spawn failure")

    def fetch() -> None:
        try:
            manager.fetch_catalog()
        except BaseException as error:
            errors.append(error)

    with patch.object(worker.subprocess, "Popen", side_effect=fail_spawn):
        fetch_thread = threading.Thread(target=fetch)
        fetch_thread.start()
        assert spawned.wait(timeout=2)
        assert manager.active_count == 1
        close_result: list[bool] = []
        close_thread = threading.Thread(
            target=lambda: close_result.append(manager.close(time.monotonic() + 3))
        )
        close_thread.start()
        release.set()
        fetch_thread.join(timeout=4)
        close_thread.join(timeout=4)

    assert not fetch_thread.is_alive()
    assert not close_thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], OSError)
    assert close_result == [True]
    assert manager.active_count == 0


def test_close_between_real_spawn_and_registration_reaps_the_child():
    entered = threading.Event()
    release = threading.Event()
    real_popen = subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []

    def pause_after_spawn(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)
        processes.append(process)
        entered.set()
        assert release.wait(timeout=3)
        return process

    manager = runtime.CatalogWorkerManager(
        command_factory=lambda: [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    errors: list[runtime.WorkerError] = []
    fetch_thread = threading.Thread(
        target=lambda: _capture_worker_error(manager, errors)
    )
    with patch.object(worker.subprocess, "Popen", side_effect=pause_after_spawn):
        fetch_thread.start()
        assert entered.wait(timeout=2)
        assert manager.active_count == 1
        close_result: list[bool] = []
        close_thread = threading.Thread(
            target=lambda: close_result.append(manager.close(time.monotonic() + 3))
        )
        close_thread.start()
        release.set()
        fetch_thread.join(timeout=4)
        close_thread.join(timeout=4)

    assert not fetch_thread.is_alive()
    assert not close_thread.is_alive()
    assert close_result == [True]
    assert [error.code for error in errors] == ["producer_cancelled"]
    assert len(processes) == 1 and processes[0].poll() is not None
    assert manager.active_count == 0


def _run_reversed_spawns(*, held_call_fails: bool) -> SimpleNamespace:
    """Complete a second fetch entirely while the first is held inside ``Popen``.

    The first request enters the patched ``Popen`` (so its reservation is the
    first one appended) and stays there until the second ``fetch_catalog`` has
    returned.  The held call then either spawns its real child or raises
    ``OSError``.  Every real child is reaped before this helper returns.
    """

    real_popen = subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []
    calls_lock = threading.Lock()
    calls = 0
    entered = threading.Event()
    release = threading.Event()

    def hold_first_call(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        nonlocal calls
        with calls_lock:
            index = calls
            calls += 1
        if index == 0:
            entered.set()
            assert release.wait(timeout=5)
            if held_call_fails:
                raise OSError("injected held spawn failure")
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    manager = runtime.CatalogWorkerManager(
        command_factory=lambda: [sys.executable, "-c", "import sys; sys.stdout.write('x')"]
    )
    first_results: list[bytes] = []
    first_errors: list[BaseException] = []

    def first_fetch() -> None:
        try:
            first_results.append(manager.fetch_catalog())
        except BaseException as error:
            first_errors.append(error)

    first_thread = threading.Thread(target=first_fetch, daemon=True)
    try:
        with patch.object(worker.subprocess, "Popen", side_effect=hold_first_call):
            first_thread.start()
            assert entered.wait(timeout=2)
            second_result = manager.fetch_catalog()
            release.set()
            first_thread.join(timeout=5)
        assert not first_thread.is_alive()
        settle_deadline = time.monotonic() + 2
        while manager.active_count and time.monotonic() < settle_deadline:
            time.sleep(0.01)
        settled_count = manager.active_count
        close_result = manager.close(time.monotonic() + 3)
        final_count = manager.active_count
        children_exited = [process.poll() is not None for process in processes]
    finally:
        release.set()
        first_thread.join(timeout=5)
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
    return SimpleNamespace(
        first_results=first_results,
        first_errors=first_errors,
        second_result=second_result,
        settled_count=settled_count,
        close_result=close_result,
        final_count=final_count,
        children_exited=children_exited,
    )


def test_reversed_spawn_registration_removes_only_each_requests_reservation():
    outcome = _run_reversed_spawns(held_call_fails=False)

    assert outcome.first_errors == []
    assert outcome.first_results == [b"x"]
    assert outcome.second_result == b"x"
    assert outcome.settled_count == 0
    assert outcome.close_result is True
    assert outcome.final_count == 0
    assert outcome.children_exited == [True, True]


def test_reversed_spawn_failure_raises_its_own_error_without_leaking():
    outcome = _run_reversed_spawns(held_call_fails=True)

    assert outcome.first_results == []
    assert len(outcome.first_errors) == 1
    error = outcome.first_errors[0]
    assert isinstance(error, OSError)
    assert not isinstance(error, ValueError)
    assert str(error) == "injected held spawn failure"
    assert outcome.second_result == b"x"
    assert outcome.settled_count == 0
    assert outcome.close_result is True
    assert outcome.final_count == 0
    assert outcome.children_exited == [True]


@pytest.mark.skipif(
    runtime.desktop_host(),
    reason="the detached Linux service is not dispatched on desktop hosts",
)
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


@pytest.mark.skipif(
    runtime.desktop_host(),
    reason="the detached Linux service is not dispatched on desktop hosts",
)
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
            "runtime._spawn_daemon(lease.fd,int((time.monotonic()+5)*10**9),"
            "working_directory=paths.state_directory); os._exit(0)"
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


@pytest.mark.skipif(
    runtime.desktop_host(),
    reason="the detached Linux service is not dispatched on desktop hosts",
)
def test_public_start_daemon_survives_launcher_exit_after_acknowledgement():
    try:
        capability_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except PermissionError:
        pytest.skip("sandbox does not permit loopback sockets")
    else:
        capability_probe.close()

    # A timed-out public stop leaves the detached daemon owning the share-zero
    # lifetime lease, and Windows then refuses to remove the state tree while
    # that handle is open.  Capture the daemon process id so teardown can always
    # reclaim it, and keep cleanup from replacing the real stop error with a
    # Windows file-lock error.
    with TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        specification_root = root / "spec"
        specification_root.mkdir()
        site_directory = root / "site"
        owner_probe = root / "owner.json"
        environment = _subprocess_environment(
            home,
            site_directory,
            extra_sitecustomize=(
                "import json\n"
                "from nyx import app_runtime\n"
                "probe = Path(os.environ['NYX_OWNER_PROBE'])\n"
                "original_start = app_runtime.ApplicationRuntime.start\n"
                "def start(self, **kwargs):\n"
                "    result = original_start(self, **kwargs)\n"
                "    probe.write_text(json.dumps({'daemon_pid': os.getpid()}), encoding='utf-8')\n"
                "    return result\n"
                "app_runtime.ApplicationRuntime.start = start\n"
            ),
        )
        environment["NYX_OWNER_PROBE"] = str(owner_probe)
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
        daemon_pid: int | None = None
        try:
            assert launcher.returncode == 0, launcher.stderr
            assert launcher.stdout.strip() == runtime.URL
            _wait_for_record(paths)
            daemon_deadline = time.monotonic() + 5
            while daemon_pid is None and time.monotonic() < daemon_deadline:
                try:
                    daemon_pid = int(
                        json.loads(owner_probe.read_text(encoding="utf-8"))["daemon_pid"]
                    )
                except (OSError, ValueError, KeyError):
                    time.sleep(0.01)
            assert daemon_pid is not None
            contender = runtime._lease_lock(paths, timeout=0.0)
            assert not contender.acquire(blocking=False)
            contender.close()
        finally:
            try:
                # Public stop is retryable: a timed-out attempt retains
                # ownership until a later authenticated stop converges.
                with patch.object(runtime, "_paths", return_value=paths):
                    for _ in range(2):
                        try:
                            if runtime.stop() == "stopped":
                                break
                        except runtime.RuntimeErrorBase:
                            continue
            finally:
                if daemon_pid is not None:
                    try:
                        os.kill(daemon_pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                    except OSError:
                        pass
        assert not paths.runtime_directory.joinpath("instance.json").exists(), (
            "public stop did not converge after retry; the daemon was force-terminated"
        )
        _assert_claim_available(paths.runtime_directory / "lease.lock")


# Worker processes replace the catalog scan with a probe that records the
# inherited liveness descriptor and, once a block file exists, keeps scanning.
# ``-P -m nyx.worker`` binds ``scan_catalog`` from ``nyx.catalog`` in a fresh
# ``__main__`` after this file runs, so the module attribute is replaced rather
# than ``nyx.worker``'s.  The daemon process records its own pid once started.
_CONTAINMENT_SITECUSTOMIZE = (
    "import json as _probe_json, sys as _probe_sys\n"
    "_probe_directory = Path(os.environ['NYX_CONTAINMENT_PROBE'])\n"
    "def _probe_write(name, value):\n"
    "    pending = _probe_directory / (name + '.pending')\n"
    "    pending.write_text(_probe_json.dumps(value), encoding='utf-8')\n"
    "    os.replace(pending, _probe_directory / name)\n"
    "if 'nyx.worker' in _probe_sys.orig_argv:\n"
    "    import fcntl as _probe_fcntl, time as _probe_time\n"
    "    from nyx import catalog as _probe_catalog\n"
    "    _probe_real_scan = _probe_catalog.scan_catalog\n"
    "    def _probe_liveness():\n"
    "        argv = _probe_sys.argv\n"
    "        if '--parent-liveness-fd' not in argv:\n"
    "            return None\n"
    "        fd = int(argv[argv.index('--parent-liveness-fd') + 1])\n"
    "        details = os.fstat(fd)\n"
    "        identity = (details.st_dev, details.st_ino)\n"
    "        count = 0\n"
    "        for name in os.listdir('/proc/self/fd'):\n"
    "            try:\n"
    "                entry = os.stat('/proc/self/fd/' + name)\n"
    "            except OSError:\n"
    "                continue\n"
    "            if (entry.st_dev, entry.st_ino) == identity:\n"
    "                count += 1\n"
    "        access = _probe_fcntl.fcntl(fd, _probe_fcntl.F_GETFL) & os.O_ACCMODE\n"
    "        return {'device': details.st_dev, 'inode': details.st_ino,\n"
    "                'access': access, 'count': count}\n"
    "    def _probe_scan(*args, **kwargs):\n"
    "        _probe_write('worker-%d.json' % os.getpid(), {\n"
    "            'pid': os.getpid(), 'ppid': os.getppid(), 'liveness': _probe_liveness()})\n"
    "        if (_probe_directory / 'block').exists():\n"
    "            _probe_time.sleep(60)\n"
    "        return _probe_real_scan(*args, **kwargs)\n"
    "    _probe_catalog.scan_catalog = _probe_scan\n"
    "elif 'nyx.runtime' in _probe_sys.orig_argv:\n"
    "    from nyx import app_runtime as _probe_app_runtime\n"
    "    _probe_original_start = _probe_app_runtime.ApplicationRuntime.start\n"
    "    def _probe_start(self, **kwargs):\n"
    "        result = _probe_original_start(self, **kwargs)\n"
    "        _probe_write('daemon.json', {'pid': os.getpid()})\n"
    "        return result\n"
    "    _probe_app_runtime.ApplicationRuntime.start = _probe_start\n"
)

# A separate subreaper adopts the daemon after the launcher exits and the
# daemon's workers after the daemon dies, so their raw wait statuses stay
# observable.  The pytest process itself never becomes a subreaper.
_SUBREAPER_HARNESS = (
    "import ctypes, json, os, sys, time\n"
    "PR_SET_CHILD_SUBREAPER = 36\n"
    "libc = ctypes.CDLL(None, use_errno=True)\n"
    "if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:\n"
    "    raise SystemExit('prctl failed: %d' % ctypes.get_errno())\n"
    "report = open(sys.argv[1], 'a', encoding='utf-8')\n"
    "def emit(value):\n"
    "    report.write(json.dumps(value) + '\\n')\n"
    "    report.flush()\n"
    "output = os.O_WRONLY | os.O_CREAT | os.O_TRUNC\n"
    "launcher = os.posix_spawn(\n"
    "    sys.executable,\n"
    "    [sys.executable, '-c', 'from nyx import runtime; print(runtime.start())'],\n"
    "    os.environ,\n"
    "    file_actions=[\n"
    "        (os.POSIX_SPAWN_OPEN, 1, sys.argv[2], output, 0o600),\n"
    "        (os.POSIX_SPAWN_OPEN, 2, sys.argv[3], output, 0o600),\n"
    "    ],\n"
    ")\n"
    "emit({'launcher': launcher})\n"
    "while True:\n"
    "    try:\n"
    "        pid, status = os.waitpid(-1, 0)\n"
    "    except ChildProcessError:\n"
    "        time.sleep(0.01)\n"
    "        continue\n"
    "    emit({'pid': pid, 'status': status, 'time': time.monotonic()})\n"
)


def _harness_report(report: Path) -> tuple[int | None, dict[int, tuple[int, float]]]:
    """Return the harness's launcher pid and every reaped pid's status and time."""

    launcher: int | None = None
    reaped: dict[int, tuple[int, float]] = {}
    try:
        lines = report.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return launcher, reaped
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "launcher" in event:
            launcher = event["launcher"]
        else:
            reaped[event["pid"]] = (event["status"], event["time"])
    return launcher, reaped


def _worker_markers(probe: Path) -> dict[int, dict[str, object]]:
    markers: dict[int, dict[str, object]] = {}
    for path in probe.glob("worker-*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        markers[record["pid"]] = record
    return markers


def _request_catalog() -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", runtime.PORT, timeout=15)
    try:
        connection.request(
            "GET", "/api/catalog", headers={"Host": f"127.0.0.1:{runtime.PORT}"}
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="the detached service, child subreaping and /proc are Linux-only",
)
@pytest.mark.parametrize("daemon_signal", ["SIGKILL", "SIGTERM"])
def test_daemon_loss_terminates_its_scanning_worker_through_one_shared_liveness_pipe(
    daemon_signal,
):
    try:
        capability_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except PermissionError:
        pytest.skip("sandbox does not permit loopback sockets")
    else:
        capability_probe.close()

    signal_number = getattr(signal, daemon_signal)
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        workspace = _sample_workspace(root)
        probe = root / "probe"
        probe.mkdir()
        report = root / "reaped.jsonl"
        launcher_stdout = root / "launcher.out"
        launcher_stderr = root / "launcher.err"
        environment = _subprocess_environment(
            home, root / "site", extra_sitecustomize=_CONTAINMENT_SITECUSTOMIZE
        )
        environment["NYX_CONTAINMENT_PROBE"] = str(probe)
        with patch.object(state, "resolve_account_home", return_value=home):
            state.setup(workspace)
        harness = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _SUBREAPER_HARNESS,
                str(report),
                str(launcher_stdout),
                str(launcher_stderr),
            ],
            cwd=Path(__file__).parents[1],
            env=environment,
            stdin=subprocess.DEVNULL,
        )
        recorded: set[int] = set()
        blocked_outcome: list[object] = []
        request_thread: threading.Thread | None = None
        try:
            launcher_pid: int | None = None
            reaped: dict[int, tuple[int, float]] = {}
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and harness.poll() is None:
                launcher_pid, reaped = _harness_report(report)
                if launcher_pid is not None and launcher_pid in reaped:
                    break
                time.sleep(0.02)
            assert harness.poll() is None, harness.returncode
            assert launcher_pid is not None and launcher_pid in reaped
            launcher_status = reaped[launcher_pid][0]
            assert os.waitstatus_to_exitcode(launcher_status) == 0, (
                launcher_stderr.read_text(encoding="utf-8")
            )
            assert launcher_stdout.read_text(encoding="utf-8").strip() == runtime.URL

            daemon_pid: int | None = None
            deadline = time.monotonic() + 5
            while daemon_pid is None and time.monotonic() < deadline:
                try:
                    daemon_pid = int(
                        json.loads((probe / "daemon.json").read_text(encoding="utf-8"))["pid"]
                    )
                except (OSError, ValueError, KeyError):
                    time.sleep(0.01)
            assert daemon_pid is not None
            recorded.add(daemon_pid)

            status, body = _request_catalog()
            assert status == 200, body
            payload = json.loads(body)
            assert [entry["package_path"] for entry in payload["entries"]] == [
                "Fictional/Queue/sample"
            ]
            first_markers = _worker_markers(probe)
            assert len(first_markers) == 1
            (first,) = first_markers.values()
            recorded.add(first["pid"])

            (probe / "block").write_text("", encoding="utf-8")

            def request_blocked_catalog() -> None:
                try:
                    blocked_outcome.append(_request_catalog())
                except OSError as error:
                    blocked_outcome.append(error)

            request_thread = threading.Thread(
                target=request_blocked_catalog, name="nyx-test-blocked-catalog", daemon=True
            )
            request_thread.start()
            second: dict[str, object] | None = None
            deadline = time.monotonic() + 10
            while second is None and time.monotonic() < deadline:
                markers = _worker_markers(probe)
                for pid, record in markers.items():
                    if pid not in first_markers:
                        second = record
                if second is None:
                    time.sleep(0.005)
            assert second is not None
            worker_pid = second["pid"]
            recorded.add(worker_pid)
            assert second["ppid"] == daemon_pid
            os.kill(daemon_pid, signal_number)
            signalled = time.monotonic()

            deadline = signalled + 3
            while time.monotonic() < deadline:
                _, reaped = _harness_report(report)
                if worker_pid in reaped and daemon_pid in reaped:
                    break
                time.sleep(0.01)
            assert worker_pid in reaped, "the scanning worker outlived its daemon"
            worker_status, worker_reaped_at = reaped[worker_pid]
            assert os.WIFEXITED(worker_status), worker_status
            assert os.WEXITSTATUS(worker_status) == 7
            assert worker_reaped_at - signalled <= 2.0
            assert daemon_pid in reaped
            daemon_status, daemon_reaped_at = reaped[daemon_pid]
            assert os.WIFSIGNALED(daemon_status), daemon_status
            assert os.WTERMSIG(daemon_status) == signal_number
            assert daemon_reaped_at - signalled <= 2.0
            for pid in (first["pid"], worker_pid):
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)

            first_liveness = first["liveness"]
            second_liveness = second["liveness"]
            assert first_liveness is not None and second_liveness is not None
            assert (first_liveness["device"], first_liveness["inode"]) == (
                second_liveness["device"],
                second_liveness["inode"],
            )
            for liveness in (first_liveness, second_liveness):
                assert liveness["access"] == os.O_RDONLY
                assert liveness["count"] == 1
        finally:
            # Test-only cleanup of any recorded process the proof left running.
            _, reaped = _harness_report(report)
            killed: list[int] = []
            for pid in recorded:
                if pid in reaped:
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    continue
                killed.append(pid)
            deadline = time.monotonic() + 5
            while killed and time.monotonic() < deadline and harness.poll() is None:
                _, reaped = _harness_report(report)
                if all(pid in reaped for pid in killed):
                    break
                time.sleep(0.02)
            harness.kill()
            harness.wait(timeout=5)
            if request_thread is not None:
                request_thread.join(timeout=20)


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
        "ack_fd=write_fd,claim_path=paths.runtime_directory/'lease.lock',"
        "working_directory=paths.state_directory); "
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
            # The child exits just after releasing its inherited claim fd;
            # teardown must wait for the process itself or Windows refuses
            # to remove the state directory it uses as its cwd.
            try:
                child_pid = int(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                child_pid = None
            if child_pid is not None:
                _wait_for_process_exit(child_pid, time.monotonic() + 10)


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
            "runtime._spawn_daemon(lease.fd,time.monotonic_ns()+5*10**9,"
            "working_directory=Path(sys.argv[1]).parent); lease.close(); os._exit(0)"
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
        " try:\n"
        "  os.write(ack,b'1')\n"
        " except OSError:\n"
        "  pass\n"
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
        " runtime._spawn_daemon(lease.fd,time.monotonic_ns()+10**10,ack_fd=write_fd,"
        "claim_path=Path(lock_path),working_directory=Path(lock_path).parent)\n"
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
            "runtime._use_port(int(os.environ['NYX_TEST_PORT'])); "
            "home=Path(sys.argv[1]); root=home/'.nyx'; paths=state.StatePaths(home,root/'config',root/'config/config.json',"
            "root,root/'runtime/deployment.json',root/'runtime'); "
            "runtime._paths=lambda **kwargs: paths; lease=runtime._lease_lock(paths,timeout=0); "
            "assert lease.acquire(blocking=False); runtime._Daemon(lease.fd,time.monotonic_ns()+5*10**9).start()"
        )
        crashed = subprocess.Popen(
            [sys.executable, "-c", script, str(paths.account_home)],
            env={**os.environ, "NYX_TEST_PORT": str(runtime.PORT)},
        )
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


def test_terminal_control_close_wakes_blocked_accept_before_completion():
    accept_entered = threading.Event()
    release_accept = threading.Event()
    wakeup_connected = threading.Event()
    shutdown_called = threading.Event()
    close_called = threading.Event()

    class Wakeup:
        def settimeout(self, timeout):
            assert timeout == 0.05

        def connect(self, address):
            assert address == ("127.0.0.1", 43210)
            wakeup_connected.set()
            release_accept.set()

        def close(self):
            return None

    class Control:
        family = socket.AF_INET
        type = socket.SOCK_STREAM
        proto = 0

        def getsockname(self):
            return ("127.0.0.1", 43210)

        def accept(self):
            accept_entered.set()
            assert release_accept.wait(timeout=2)
            raise OSError("terminal control listener closed")

        def shutdown(self, how):
            assert how == socket.SHUT_RDWR
            shutdown_called.set()
            raise OSError("listener shutdown is not portable")

        def close(self):
            close_called.set()

    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        control_thread = None
        with patch.object(runtime, "_paths", return_value=paths):
            daemon = runtime._Daemon(
                lease_fd, int((time.monotonic() + 5) * 1_000_000_000)
            )
        daemon.control = Control()
        try:
            control_thread = threading.Thread(target=daemon._serve_control)
            daemon.control_thread = control_thread
            control_thread.start()
            assert accept_entered.wait(timeout=1)
            with patch.object(daemon.application, "shutdown", return_value=True), patch.object(
                runtime.socket, "socket", return_value=Wakeup()
            ):
                assert daemon.shutdown(deadline=time.monotonic() + 0.5) == "stopped"
            assert wakeup_connected.is_set()
            assert shutdown_called.is_set()
            assert close_called.is_set()
            assert not control_thread.is_alive()
        finally:
            release_accept.set()
            if control_thread is not None:
                control_thread.join(timeout=2)
            os.close(lease_fd)


def test_authenticated_retry_renews_inflight_cleanup_without_serialized_gap():
    first_cleanup_entered = threading.Event()
    allow_first_cleanup_return = threading.Event()
    retry_deadline_published = threading.Event()
    cleanup_calls: list[tuple[int | None, float]] = []
    results: dict[str, str] = {}

    def cleanup(deadline):
        cleanup_calls.append((threading.current_thread().ident, deadline))
        if len(cleanup_calls) == 1:
            first_cleanup_entered.set()
            assert allow_first_cleanup_return.wait(timeout=2)
            return False
        return True

    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        with patch.object(runtime, "_paths", return_value=paths):
            daemon = runtime._Daemon(
                lease_fd, int((time.monotonic() + 5) * 1_000_000_000)
            )

        def request(name, deadline):
            results[name] = daemon.shutdown(deadline=deadline)

        first_deadline = time.monotonic() + 0.1
        retry_deadline = time.monotonic() + 1

        original_extend_shutdown_deadline = daemon._extend_shutdown_deadline

        def observe_deadline(deadline):
            extended = original_extend_shutdown_deadline(deadline)
            if deadline == retry_deadline:
                retry_deadline_published.set()
            return extended

        first = threading.Thread(target=request, args=("first", first_deadline))
        retry = threading.Thread(target=request, args=("retry", retry_deadline))
        try:
            with patch.object(
                daemon, "_extend_shutdown_deadline", side_effect=observe_deadline
            ), patch.object(daemon.application, "shutdown", side_effect=cleanup):
                first.start()
                assert first_cleanup_entered.wait(timeout=1)
                retry.start()
                assert retry_deadline_published.wait(timeout=1)
                assert daemon._current_shutdown_deadline() >= retry_deadline
                allow_first_cleanup_return.set()
                first.join(timeout=2)
                retry.join(timeout=2)
            assert not first.is_alive()
            assert not retry.is_alive()
            assert results == {"first": "stopped", "retry": "stopped"}
            assert len(cleanup_calls) == 2
            assert cleanup_calls[0][0] == cleanup_calls[1][0]
            assert cleanup_calls[1][1] >= retry_deadline
        finally:
            allow_first_cleanup_return.set()
            if first.ident is not None:
                first.join(timeout=2)
            if retry.ident is not None:
                retry.join(timeout=2)
            os.close(lease_fd)


def test_retained_shutdown_retry_wakes_owner_and_releases_original_claim():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_path = paths.runtime_directory / "lease.lock"
        lease_fd = _transferred_claim_fd(lease_path)
        with patch.object(runtime, "_paths", return_value=paths):
            daemon = runtime._Daemon(
                lease_fd, int((time.monotonic() + 5) * 1_000_000_000)
            )
        with patch.object(runtime, "_paths", return_value=paths), patch.object(
            daemon, "start"
        ), patch.object(daemon.application, "shutdown", side_effect=[False, True]):
            try:
                assert daemon.shutdown(deadline=time.monotonic() + 1) == "timeout"
            except BaseException:
                # The descriptor passes to the daemon only when run() starts.
                os.close(lease_fd)
                raise
            with managed_daemon_run(daemon) as run:
                # The retained owner consumes the first attempt's notification
                # and waits for retry completion while continuing to hold the
                # exact transferred lifetime object.
                deadline = time.monotonic() + 1
                while daemon.shutdown_done.is_set() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert not daemon.shutdown_done.is_set()
                contender = runtime._lease_lock(paths, timeout=0.0)
                assert not contender.acquire(blocking=False)
                contender.close()

                assert daemon.shutdown(deadline=time.monotonic() + 1) == "stopped"
                assert run.join(timeout=0.5) == 0
        _assert_claim_available(lease_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM-resistant worker proof")
def test_public_stop_timeout_retains_authenticated_cleanup_until_retry():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        second_root = Path(temporary) / "second-specification"
        second_root.mkdir()
        worker_ready = Path(temporary) / "resistant-worker-ready"
        lease_fd = _transferred_claim_fd(paths.runtime_directory / "lease.lock")
        worker_child = None
        with patch.object(
            state, "resolve_account_home", return_value=paths.account_home
        ), patch.object(runtime, "_paths", return_value=paths), patch.object(
            # Budget comfortably above the HTTP listener's 0.5 s
            # serve_forever poll interval so the first stop times out
            # because the SIGTERM-resistant worker refuses to die, and the
            # retry never inherits a leftover poll tail that would make its
            # convergence depend on scheduler jitter.
            runtime,
            "SHUTDOWN_TIMEOUT",
            1.0,
        ):
            daemon = runtime._Daemon(
                lease_fd, int((time.monotonic() + 5) * 1_000_000_000)
            )
            daemon.workers = runtime.CatalogWorkerManager(
                command_factory=lambda: [
                    sys.executable,
                    "-c",
                    "import signal,time,pathlib; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    f"pathlib.Path({str(worker_ready)!r}).write_text('ready'); "
                    "time.sleep(30)",
                ],
                timeout=30,
            )
            with managed_daemon_run(daemon) as run:
                try:
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
                    # SIGTERM must be ignored before the stop tears the worker
                    # down; otherwise the interpreter's default disposition kills a
                    # child that has not yet installed its handler, and the premise
                    # of a resistant worker is never actually established.
                    ready_deadline = time.monotonic() + 5
                    while not worker_ready.exists() and time.monotonic() < ready_deadline:
                        time.sleep(0.01)
                    assert worker_ready.exists(), "resistant worker did not install its handler"

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
                    assert run.join(timeout=3) == 0
                    with pytest.raises(runtime.UnhealthyInstanceError):
                        runtime._send_control(instance, "status")
                    assert runtime.observe_runtime().status == "not_running"
                finally:
                    # The resistant worker is the obstacle this test owns; release it
                    # before the managed exit retries the shutdown.
                    if worker_child is not None and worker_child.process.poll() is None:
                        worker_child.process.kill()


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


def test_setup_of_active_and_moved_workspace_succeeds_after_stored_key_stops_resolving():
    from test_state import stale_key_scenario

    with TemporaryDirectory() as temporary:
        scenario = stale_key_scenario(Path(temporary))
        home_patch, uid_patch = scenario["home_patches"]
        with home_patch, uid_patch:
            assert runtime.setup(scenario["b"]).specification_root == scenario["b"]
            moved = runtime.setup(scenario["moved"])
            assert moved.specification_root == scenario["moved"]
            assert moved.stage_order == ()
            stored = json.loads(scenario["paths"].config_file.read_text(encoding="utf-8"))
            key = str(scenario["a"])
            assert (stored["stage_orders"][key], stored["completed_stages"][key]) == scenario[
                "entries_a"
            ]


def _checkout_folders(base):
    # Native, normalized, sorted absolute folders that need not exist.
    return [str(base / "one"), str(base / "three"), str(base / "two")]


def _write_checkout_record(paths, specification_root, other_root):
    folders = _checkout_folders(other_root.parent)
    settings = {
        str(specification_root.resolve()): {"folders": folders[:2], "github_lookup": True},
        str(other_root.resolve()): {"folders": folders[2:], "github_lookup": False},
    }
    payload = {
        "schema_version": 4,
        "specification_root": str(specification_root.resolve()),
        "checkout_settings": settings,
    }
    paths.config_file.write_bytes(
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    )
    return payload


def test_setup_preserves_checkout_settings_and_revision_tracks_them():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        paths, home, specification_root = _fixture(root)
        other = root / "other"
        other.mkdir()
        payload = _write_checkout_record(paths, specification_root, other)
        before = paths.config_file.read_bytes()
        revision = state.configuration_revision(paths)
        with patch.object(runtime, "_paths", return_value=paths):
            rewritten = runtime.setup(other)
        stored = json.loads(paths.config_file.read_bytes())
        assert stored["schema_version"] == 4
        assert stored["specification_root"] == str(other.resolve())
        assert stored["checkout_settings"] == payload["checkout_settings"]
        assert rewritten.checkout_folders == (_checkout_folders(root)[2],)
        assert state.configuration_revision(paths) != revision

        # Records that differ only in checkout_settings have different revisions.
        paths.config_file.write_bytes(before)
        changed = json.loads(before)
        changed["checkout_settings"][str(other.resolve())]["github_lookup"] = True
        paths.config_file.write_text(json.dumps(changed) + "\n", encoding="utf-8")
        assert state.configuration_revision(paths) != revision

        # Rewriting an unchanged schema 3 record as schema 4 keeps its revision.
        paths.config_file.write_text(
            json.dumps({"schema_version": 3, "specification_root": str(other.resolve())}) + "\n",
            encoding="utf-8",
        )
        legacy_revision = state.configuration_revision(paths)
        with patch.object(runtime, "_paths", return_value=paths):
            runtime.setup(other)
        assert json.loads(paths.config_file.read_bytes())["schema_version"] == 4
        assert state.configuration_revision(paths) == legacy_revision


def test_setup_with_held_lease_and_same_root_leaves_checkout_record_bytes_unchanged():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        paths, home, specification_root = _fixture(root)
        other = root / "other"
        other.mkdir()
        _write_checkout_record(paths, specification_root, other)
        before = paths.config_file.read_bytes()
        lease = runtime._lease_lock(paths, timeout=0.0)
        assert lease.acquire(blocking=False)
        try:
            with patch.object(runtime, "_paths", return_value=paths):
                assert runtime.setup(specification_root).checkout_folders == tuple(_checkout_folders(root)[:2])
        finally:
            lease.close()
        assert paths.config_file.read_bytes() == before
