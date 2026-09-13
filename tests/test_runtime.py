"""Behavioral checks for the authenticated process lifecycle."""

from __future__ import annotations

import fcntl
import json
import os
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
