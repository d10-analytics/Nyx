"""Behavioral checks for installed worker configuration transport."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

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
            with (
                patch.object(
                    worker.NativeClaim,
                    "receive_handle",
                    side_effect=lambda fd, **_kwargs: os.dup(fd),
                ),
                patch.object(
                    worker.state,
                    "load_configuration",
                    side_effect=load_configuration,
                ),
            ):
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
            patch.object(
                worker.NativeClaim,
                "receive_handle",
                side_effect=lambda fd, **_kwargs: os.dup(fd),
            ),
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


def test_parent_observer_joins_on_normal_close() -> None:
    read_fd, write_fd = os.pipe()
    observer = worker._ParentLossObserver(read_fd)
    try:
        observer.start()
        observer.close()
        assert not observer._thread.is_alive()
        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        os.close(write_fd)


def test_inherited_worker_completes_while_parent_liveness_pipe_remains_open() -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        workspace = root / "workspace"
        workspace.mkdir()
        environment = {"HOME": str(home), "USERPROFILE": str(home)}
        with patch.dict(os.environ, environment):
            state.setup(workspace)
            paths = state.state_paths()
            claim = NativeClaim(paths.runtime_directory / "recovery.lock")
            assert claim.acquire(blocking=False)
            read_fd, write_fd = os.pipe()
            manager = worker.CatalogWorkerManager(
                timeout=2,
                recovery_claim=claim,
                recovery_path=claim.path,
                parent_liveness_fd=read_fd,
            )
            try:
                catalog = json.loads(manager.fetch_catalog())
                assert catalog["schema_version"] == 4
                assert manager.close(time.monotonic() + 2)
                assert manager.active_count == 0
                assert claim.held
            finally:
                manager.close(time.monotonic() + 2)
                os.close(write_fd)
                os.close(read_fd)
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


def test_partial_native_handle_mapping_closes_received_object_before_config_load():
    with (
        patch.object(
            worker.NativeClaim,
            "receive_handle",
            side_effect=[41, OSError("invalid liveness handle")],
        ) as receive,
        patch.object(worker.os, "close") as close,
    ):
        assert (
            worker._receive_worker_inheritance(
                101,
                Path("recovery.lock"),
                202,
            )
            is None
        )

    assert receive.call_args_list[0].args == (101,)
    assert receive.call_args_list[0].kwargs == {}
    assert receive.call_args_list[1].args == (202,)
    assert receive.call_args_list[1].kwargs == {"read_only": True}
    close.assert_called_once_with(41)


@pytest.mark.skipif(
    os.name == "nt",
    reason="direct descriptor fixture is POSIX-only; native handle proof uses the shell path",
)
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


def _packaged_layout(tmp_path: Path, *, with_helper: bool) -> tuple[Path, Path]:
    application = tmp_path / "application"
    application.mkdir()
    executable = application / ("Nyx.exe" if os.name == "nt" else "Nyx")
    executable.write_text("", encoding="utf-8")
    helper_name = "NyxWorker.exe" if os.name == "nt" else "NyxWorker"
    helper = application / "NyxWorker" / helper_name
    if with_helper:
        helper.parent.mkdir()
        helper.write_text("", encoding="utf-8")
    return executable, helper


def test_packaged_application_selects_the_bundled_console_helper(tmp_path, monkeypatch):
    executable, helper = _packaged_layout(tmp_path, with_helper=True)
    monkeypatch.setattr(worker.sys, "frozen", True, raising=False)
    monkeypatch.setattr(worker.sys, "executable", str(executable))

    assert worker.bundled_worker_command() == [str(helper)]
    assert worker.default_worker_command() == [str(helper)]


def test_packaged_application_without_helper_fails_closed_instead_of_reentering_gui(
    tmp_path, monkeypatch
):
    executable, _ = _packaged_layout(tmp_path, with_helper=False)
    monkeypatch.setattr(worker.sys, "frozen", True, raising=False)
    monkeypatch.setattr(worker.sys, "executable", str(executable))

    assert worker.bundled_worker_command() is None
    with pytest.raises(worker.WorkerError) as error:
        worker.default_worker_command()
    assert error.value.code == "producer_unavailable"


def test_source_application_keeps_the_module_worker_entry(tmp_path, monkeypatch):
    monkeypatch.delattr(worker.sys, "frozen", raising=False)
    assert worker.bundled_worker_command() is None
    assert worker.default_worker_command() == [worker.sys.executable, "-m", "nyx.worker"]


def test_console_helper_entry_emits_the_same_exact_bytes_as_the_module_entry():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        workspace = root / "workspace"
        workspace.mkdir()
        environment = {"HOME": str(home), "USERPROFILE": str(home)}
        with patch.dict(os.environ, environment):
            state.setup(workspace)
        module_entry = subprocess.run(
            [sys.executable, "-m", "nyx.worker"],
            cwd=Path(__file__).parents[1],
            env={**os.environ, **environment},
            capture_output=True,
        )
        helper_entry = subprocess.run(
            [sys.executable, "-m", "nyx.desktop_worker"],
            cwd=Path(__file__).parents[1],
            env={**os.environ, **environment},
            capture_output=True,
        )

    assert module_entry.returncode == 0, module_entry.stderr
    assert helper_entry.returncode == 0, helper_entry.stderr
    assert helper_entry.stdout == module_entry.stdout
    assert helper_entry.stdout.decode("utf-8")
