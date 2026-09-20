"""Behavioral checks for the authenticated process lifecycle."""

from __future__ import annotations

import fcntl
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
from unittest.mock import patch

import pytest

from nyx import _native_claim, runtime, state, worker


def _fixture(root: Path) -> tuple[state.StatePaths, Path, Path]:
    home = root / "home"
    home.mkdir()
    spec = root / "spec"
    spec.mkdir()
    with patch.object(state, "resolve_account_home", return_value=home), patch.object(
        state, "_current_uid", return_value=os.getuid()
    ):
        state.setup(spec)
        paths = state.state_paths()
    return paths, home, spec


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


def test_windows_share_violation_is_a_qualified_busy_probe():
    details = os.stat(__file__)
    busy = OSError(13, "sharing violation")
    busy.winerror = 32
    with patch.object(Path, "lstat", return_value=details), patch.object(
        _native_claim, "_open_claim", side_effect=busy
    ):
        assert runtime.NativeClaim.probe(Path("claim.lock")) == "held"


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


def test_readiness_publication_rejects_expiry_before_creating_temp_record():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        instance = runtime.Instance("instance", runtime.URL, "capability", "127.0.0.1:1")
        with patch.object(runtime.tempfile, "mkstemp") as mkstemp, pytest.raises(
            runtime.StartupError
        ):
            runtime._write_instance(paths, instance, deadline=time.monotonic() - 1)
        mkstemp.assert_not_called()


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
            ) == 1
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

        def join(self, timeout=None):
            return None

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
            return None

        def join(self, timeout=None):
            return None

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
            assert daemon.run() == 1

        assert events == [
            "http-shutdown",
            "connections-close",
            "http-close",
            "control-close",
        ]
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
        with patch.object(state, "resolve_account_home", return_value=home), patch.object(
            runtime, timeout_name, 0.0
        ), pytest.raises((TimeoutError, runtime.RuntimeErrorBase)):
            operation(specification)
        assert not (home / ".nyx").exists()


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


def test_posix_spawn_preserves_detached_descriptor_contract():
    process = object()
    with patch.object(runtime.subprocess, "Popen", return_value=process) as popen:
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
        lease_fd = os.open(paths.runtime_directory / "lease.lock", os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(lease_fd, 0o600)
        fcntl.flock(lease_fd, fcntl.LOCK_EX)
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
        lease_fd = os.open(paths.runtime_directory / "lease.lock", os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(lease_fd, 0o600)
        fcntl.flock(lease_fd, fcntl.LOCK_EX)
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
        lease_fd = os.open(paths.runtime_directory / "lease.lock", os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(lease_fd, 0o600)
        fcntl.flock(lease_fd, fcntl.LOCK_EX)
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
            '{  "specification_root" : "'
            + str(first.resolve())
            + '", "schema_version" : 1 }\n\n'
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
                    "control": runtime._control_name(),
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
        record.write_bytes(b"daemon-record")
        lease_fd = os.open(paths.runtime_directory / "lease.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lease_fd, fcntl.LOCK_EX)
        try:
            with patch.object(runtime, "_paths", return_value=paths):
                daemon = runtime._Daemon(lease_fd, int((time.monotonic() + 5) * 1_000_000_000))
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


def test_wrong_capability_cannot_control_a_ready_instance():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd = os.open(paths.runtime_directory / "lease.lock", os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(lease_fd, 0o600)
        fcntl.flock(lease_fd, fcntl.LOCK_EX)
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
                    "control": runtime._control_name(),
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
        lease_fd = os.open(paths.runtime_directory / "lease.lock", os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(lease_fd, 0o600)
        fcntl.flock(lease_fd, fcntl.LOCK_EX)
        result: list[int] = []
        try:
            with patch.object(runtime, "_paths", return_value=paths):
                daemon = runtime._Daemon(lease_fd, int((time.monotonic() + 5) * 1_000_000_000))
                thread = threading.Thread(target=lambda: result.append(daemon.run()))
                thread.start()
                thread.join(timeout=5)
                assert not thread.is_alive()
            assert result == [1]
            assert not paths.runtime_directory.joinpath("instance.json").exists()
            assert not list(paths.runtime_directory.glob(".instance.json.*"))
        finally:
            occupant.close()


def _write_runtime_record(paths: state.StatePaths, *, instance_id: str = "instance", capability: str = "capability") -> runtime.Instance:
    instance = runtime.Instance(instance_id, runtime.URL, capability, runtime._control_name())
    record = paths.runtime_directory / "instance.json"
    record.write_text(json.dumps(instance.as_dict()) + "\n", encoding="utf-8")
    os.chmod(record, 0o600)
    return instance


def _held_lease(paths: state.StatePaths) -> tuple[int, runtime._FileLock]:
    lease_fd = os.open(paths.runtime_directory / "lease.lock", os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(lease_fd, 0o600)
    fcntl.flock(lease_fd, fcntl.LOCK_EX)
    lease = runtime._lease_lock(paths, timeout=0.0)
    return lease_fd, lease


def _observe(paths: state.StatePaths) -> runtime.RuntimeObservation:
    with patch.object(state, "resolve_account_home", return_value=paths.account_home), patch.object(
        state, "_current_uid", return_value=os.getuid()
    ):
        return runtime.observe_runtime()


def test_observe_runtime_reports_operation_contention_without_mutation():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        operation = runtime._operation_lock(paths, timeout=0.0)
        assert operation.acquire(blocking=False)
        before = {path.name: (path.stat().st_mode, path.read_bytes()) for path in paths.runtime_directory.iterdir()}
        try:
            observed = _observe(paths)
        finally:
            operation.close()
        assert observed.status == "unknown"
        assert observed.diagnostic == runtime.RUNTIME_OPERATION_IN_PROGRESS
        after = {path.name: (path.stat().st_mode, path.read_bytes()) for path in paths.runtime_directory.iterdir()}
        assert after == before


def test_observe_runtime_reports_free_lease_stale_record_and_retains_bytes():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        record = paths.runtime_directory / "instance.json"
        instance = _write_runtime_record(paths)
        before = record.read_bytes()
        before_mode = stat.S_IMODE(record.stat().st_mode)
        observed = _observe(paths)
        assert observed.status == "not_running"
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
        original_observer = state.observe_runtime
        observation_count = 0

        def observe_then_start() -> state.RuntimeObservation:
            result = original_observer()
            nonlocal operation_holder
            nonlocal observation_count
            observation_count += 1
            if observation_count == 2:
                paths.runtime_directory.mkdir(mode=0o700, parents=True)
                operation_holder = runtime._operation_lock(paths, timeout=0.0)
                assert operation_holder.acquire(blocking=False)
            return result

        try:
            with patch.object(state, "resolve_account_home", return_value=paths.account_home), patch.object(
                state, "_current_uid", return_value=os.getuid()
            ), patch.object(state, "observe_runtime", side_effect=observe_then_start):
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
        instance = _write_runtime_record(paths)
        lease_fd, lease = _held_lease(paths)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(runtime._control_name())
        except PermissionError:
            listener.close()
            lease.close()
            os.close(lease_fd)
            pytest.skip("sandbox does not permit local control sockets")
        listener.listen(1)
        received: list[bytes] = []

        def serve() -> None:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(2)
                try:
                    received.append(connection.recv(1024))
                except TimeoutError:
                    received.append(b"")

        server = threading.Thread(target=serve)
        server.start()
        try:
            with patch.object(runtime, "_peer_uid", return_value=os.getuid() + 1):
                observed = _observe(paths)
        finally:
            listener.close()
            server.join(timeout=3)
            lease.close()
            os.close(lease_fd)
        assert observed.status == "unknown"
        assert observed.diagnostic == runtime.RUNTIME_CONTROL_IDENTITY_MISMATCH
        assert received == [b""]
        assert instance.capability.encode("utf-8") not in b"".join(received)


def test_observe_runtime_accepts_authenticated_ready_control_and_rechecks_state():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        instance = _write_runtime_record(paths)
        lease_fd, lease = _held_lease(paths)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(runtime._control_name())
        except PermissionError:
            listener.close()
            lease.close()
            os.close(lease_fd)
            pytest.skip("sandbox does not permit local control sockets")
        listener.listen(1)

        def serve() -> None:
            connection, _ = listener.accept()
            with connection:
                connection.recv(4096)
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
        assert observed.status == "running"
        assert observed.url == runtime.URL
        assert observed.diagnostic is None


def test_observe_runtime_rejects_lease_release_before_ready_response():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        instance = _write_runtime_record(paths)
        lease_fd, lease = _held_lease(paths)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(runtime._control_name())
        except PermissionError:
            listener.close()
            lease.close()
            os.close(lease_fd)
            pytest.skip("sandbox does not permit local control sockets")
        listener.listen(1)
        released = threading.Event()

        def serve() -> None:
            connection, _ = listener.accept()
            with connection:
                connection.recv(4096)
                fcntl.flock(lease_fd, fcntl.LOCK_UN)
                released.set()
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
        assert released.is_set()
        assert observed.status == "unknown"
        assert observed.diagnostic == runtime.RUNTIME_STATE_CHANGED


def test_observe_runtime_control_timeout_uses_one_total_second_without_retry():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        _write_runtime_record(paths)
        lease_fd, lease = _held_lease(paths)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(runtime._control_name())
        except PermissionError:
            listener.close()
            lease.close()
            os.close(lease_fd)
            pytest.skip("sandbox does not permit local control sockets")
        listener.listen(1)
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
        instance = _write_runtime_record(paths)
        lease_fd, lease = _held_lease(paths)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(runtime._control_name())
        except PermissionError:
            listener.close()
            lease.close()
            os.close(lease_fd)
            pytest.skip("sandbox does not permit local control sockets")
        listener.listen(1)

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
        occupant = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        occupant.bind(runtime._control_name())
        occupant.listen(1)
        lease_fd = os.open(paths.runtime_directory / "lease.lock", os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(lease_fd, 0o600)
        fcntl.flock(lease_fd, fcntl.LOCK_EX)
        result: list[int] = []
        try:
            with patch.object(runtime, "_paths", return_value=paths):
                daemon = runtime._Daemon(lease_fd, int((time.monotonic() + 5) * 1_000_000_000))
                thread = threading.Thread(target=lambda: result.append(daemon.run()))
                thread.start()
                thread.join(timeout=5)
                assert not thread.is_alive()
            assert result == [1]
            assert not paths.runtime_directory.joinpath("instance.json").exists()
        finally:
            occupant.close()


def test_idle_http_connection_does_not_block_authenticated_stop():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd = os.open(paths.runtime_directory / "lease.lock", os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(lease_fd, 0o600)
        fcntl.flock(lease_fd, fcntl.LOCK_EX)
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
    assert time.monotonic() - started < 0.35
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
        while manager.active_count == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert manager.active_count == 1
        assert manager.close(time.monotonic() + 0.2) is False
        assert manager.active_count == 1
        child = manager._children[0]
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
                "from nyx import runtime; print(runtime.start(), flush=True); "
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
            first_stdout, first_stderr = first.communicate(timeout=12)
            second_stdout, second_stderr = second.communicate(timeout=12)
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
                timeout=12,
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
            "assert lock.acquire(); open(sys.argv[2], 'a').write('acquired\\n'); "
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
        script = (
            "import fcntl,os,subprocess,sys,time; "
            "fd=os.open(sys.argv[1], os.O_RDWR|os.O_CREAT, 0o600); os.fchmod(fd, 0o600); "
            "fcntl.flock(fd, fcntl.LOCK_EX); "
            "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(.8)'], pass_fds=(fd,)); "
            "os.close(fd); os._exit(0)"
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
            "import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600); "
            "os.fchmod(fd,0o600); fcntl.flock(fd,fcntl.LOCK_EX); os._exit(0)"
        )
        launcher = subprocess.Popen([sys.executable, "-c", script, str(lock_path)])
        assert launcher.wait(timeout=2) == 0
        probe = runtime._FileLock(lock_path, timeout=0.0)
        assert probe.acquire(blocking=False)
        probe.close()


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
            assert crashed.wait(timeout=2) == -9
            with patch.object(runtime, "_paths", return_value=paths):
                assert runtime.stop() == "stopped"
            assert not paths.runtime_directory.joinpath("instance.json").exists()
        finally:
            if crashed.poll() is None:
                crashed.kill()
                crashed.wait()


def test_controlled_stop_timeout_retains_daemon_lease_until_child_cleanup():
    with TemporaryDirectory() as temporary:
        paths, _, _ = _fixture(Path(temporary))
        lease_fd = os.open(paths.runtime_directory / "lease.lock", os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(lease_fd, 0o600)
        fcntl.flock(lease_fd, fcntl.LOCK_EX)
        with patch.object(runtime, "_paths", return_value=paths):
            daemon = runtime._Daemon(lease_fd, int((time.monotonic() + 5) * 1_000_000_000))
            daemon.workers = runtime.CatalogWorkerManager(
                command_factory=lambda: [
                    sys.executable,
                    "-c",
                    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(.5); time.sleep(30)",
                ],
                timeout=30,
            )
            daemon_thread = threading.Thread(target=daemon.run)
            daemon_thread.start()
            instance = _wait_for_record(paths)
            request_done: list[object] = []

            def request_catalog() -> None:
                connection = http.client.HTTPConnection("127.0.0.1", runtime.PORT, timeout=10)
                try:
                    connection.request("GET", "/api/catalog", headers={"Host": f"127.0.0.1:{runtime.PORT}"})
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
            time.sleep(0.7)
            response = runtime._send_control(instance, "stop")
            assert response["status"] == "stopping"
            assert daemon.shutdown_done.wait(timeout=7)
            assert daemon.shutdown_result == "timeout"
            probe = runtime._lease_lock(paths, timeout=0.0)
            assert not probe.acquire(blocking=False)
            probe.close()
            # The child and request are test-owned; release them before allowing
            # the daemon thread to complete its retained-ownership loop.
            for child in tuple(daemon.workers._children):
                child.process.kill()
            request_thread.join(timeout=3)
            daemon.shutdown_result = "stopped"
            if daemon.control is not None:
                daemon.control.close()
            daemon_thread.join(timeout=3)
            assert not daemon_thread.is_alive()
