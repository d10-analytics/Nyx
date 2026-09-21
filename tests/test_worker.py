"""Behavioral checks for installed worker configuration transport."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nyx import state, worker
from nyx._native_claim import NativeClaim


class _Buffer:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.flush_count = 0

    def write(self, value: bytes) -> int:
        self.writes.append(value)
        return len(value)

    def flush(self) -> None:
        self.flush_count += 1


class _Stdout:
    def __init__(self) -> None:
        self.buffer = _Buffer()


def test_worker_main_loads_once_and_transports_configuration_by_identity() -> None:
    root = Path("/sentinel/specification-root")
    hidden_stages = tuple(["Done", "In_Progress"])
    configuration = state.Configuration(root, hidden_stages)
    load_count = 0
    scanner_arguments: list[tuple[object, object]] = []
    stdout = _Stdout()

    def load_configuration() -> state.Configuration:
        nonlocal load_count
        load_count += 1
        return configuration

    def scan_catalog(received_root: object, *, hidden_stages: object) -> str:
        scanner_arguments.append((received_root, hidden_stages))
        return "catalog ✓"

    with (
        patch.object(worker.state, "load_configuration", side_effect=load_configuration),
        patch.object(worker, "scan_catalog", side_effect=scan_catalog),
        patch.object(worker.sys, "stdout", stdout),
    ):
        assert worker._worker_main() == 0

    assert load_count == 1
    assert len(scanner_arguments) == 1
    received_root, received_hidden_stages = scanner_arguments[0]
    assert received_root is root
    assert received_hidden_stages is hidden_stages
    assert stdout.buffer.writes == ["catalog ✓".encode("utf-8")]
    assert stdout.buffer.flush_count == 1


def test_inherited_worker_rejects_substituted_claim_before_configuration_load() -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        actual_path = root / "recovery.lock"
        substituted_path = root / "substituted.lock"
        claim = NativeClaim(actual_path)
        assert claim.acquire(blocking=False)
        read_fd, write_fd = os.pipe()
        loads = 0

        def load_configuration() -> state.Configuration:
            nonlocal loads
            loads += 1
            raise AssertionError("configuration must not load")

        try:
            with patch.object(worker.state, "load_configuration", side_effect=load_configuration):
                assert (
                    worker._worker_main(
                        recovery_fd=claim.fd,
                        recovery_path=substituted_path,
                        parent_liveness_fd=read_fd,
                    )
                    == 4
                )
            assert loads == 0
        finally:
            os.close(write_fd)
            claim.close()


def test_inherited_worker_validates_claim_and_parent_observation_before_one_load() -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        claim = NativeClaim(root / "recovery.lock")
        assert claim.acquire(blocking=False)
        read_fd, write_fd = os.pipe()
        configuration = state.Configuration(root, ("Queue",))
        stdout = _Stdout()
        loaded = 0

        def load_configuration() -> state.Configuration:
            nonlocal loaded
            loaded += 1
            return configuration

        with (
            patch.object(worker.state, "load_configuration", side_effect=load_configuration),
            patch.object(worker, "scan_catalog", return_value="exact bytes"),
            patch.object(worker.sys, "stdout", stdout),
        ):
            try:
                assert (
                    worker._worker_main(
                        recovery_fd=claim.fd,
                        recovery_path=claim.path,
                        parent_liveness_fd=read_fd,
                    )
                    == 0
                )
            finally:
                os.close(write_fd)

        assert loaded == 1
        assert stdout.buffer.writes == [b"exact bytes"]
        assert claim.held
        claim.close()


def test_partial_inherited_worker_objects_are_rejected_without_loading_config() -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        claim = NativeClaim(root / "recovery.lock")
        assert claim.acquire(blocking=False)
        loads = 0

        def load_configuration() -> state.Configuration:
            nonlocal loads
            loads += 1
            raise AssertionError("configuration must not load")

        try:
            with patch.object(worker.state, "load_configuration", side_effect=load_configuration):
                assert (
                    worker._worker_main(
                        recovery_fd=claim.fd,
                        recovery_path=claim.path,
                    )
                    == 4
                )
            assert loads == 0
        finally:
            claim.close()


def test_parent_loss_terminates_a_scanning_desktop_worker_and_releases_recovery_claim():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        workspace = root / "workspace"
        workspace.mkdir()
        marker = root / "scan-started"
        with patch.object(state, "resolve_account_home", return_value=home):
            state.setup(workspace)
            paths = state.state_paths()
        claim = NativeClaim(paths.runtime_directory / "recovery.lock")
        assert claim.acquire(blocking=False)
        read_fd, write_fd = os.pipe()
        script = (
            "import os,sys,time\n"
            "from pathlib import Path\n"
            "from nyx import state,worker\n"
            "state.resolve_account_home=lambda: Path(os.environ['NYX_TEST_HOME'])\n"
            "marker=Path(sys.argv[4])\n"
            "def scan(*args,**kwargs):\n"
            "    marker.write_text('started')\n"
            "    time.sleep(30)\n"
            "    return 'late'\n"
            "worker.scan_catalog=scan\n"
            "raise SystemExit(worker._worker_main(recovery_fd=int(sys.argv[1]),\n"
            "    recovery_path=Path(sys.argv[2]), parent_liveness_fd=int(sys.argv[3])))\n"
        )
        environment = os.environ.copy()
        environment["NYX_TEST_HOME"] = str(home)
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(claim.fd), str(claim.path), str(read_fd), str(marker)],
            cwd=Path(__file__).parents[1],
            env=environment,
            pass_fds=(claim.fd, read_fd),
        )
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.exists()
            os.close(write_fd)
            write_fd = -1
            assert child.wait(timeout=5) == 7
            claim.close()
            assert NativeClaim.probe(claim.path) == "free"
        finally:
            if write_fd >= 0:
                os.close(write_fd)
            os.close(read_fd)
            claim.close()
