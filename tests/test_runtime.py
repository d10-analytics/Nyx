"""Behavioral checks for the authenticated process lifecycle."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from nyx import runtime, state


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
