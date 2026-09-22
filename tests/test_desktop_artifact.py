"""Proof for the delivered standalone desktop artifact.

The checks launch the staged application and its bundled console helper as
black boxes: the environment removes checkout and interpreter discovery, and
observations come from the process exit, the fixed loopback board, and the
account claim files rather than from source imports inside the application.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from nyx import state
from nyx._native_claim import NativeClaim
from nyx.catalog import CATALOG_HIDDEN_STAGES, scan_catalog
from nyx.worker import CatalogWorkerManager

_ARTIFACT_REQUIRED = os.environ.get("NYX_REQUIRE_NATIVE_DESKTOP") == "1"
_ARTIFACT_ENV = os.environ.get("NYX_DESKTOP_ARTIFACT")

if _ARTIFACT_REQUIRED and not _ARTIFACT_ENV:
    raise RuntimeError(
        "NYX_DESKTOP_ARTIFACT must identify the staged application in a required native lane"
    )

pytestmark = pytest.mark.skipif(
    not _ARTIFACT_ENV,
    reason="the delivered desktop artifact is not staged on this host",
)

PORT = 8765
_HOST = f"127.0.0.1:{PORT}"
_START_TIMEOUT = 90.0
_SOURCE_ROOT = Path(__file__).parents[1]
# The delivered application must apply the persisted policy exactly, so the
# written configuration and every expected catalog use the same hidden stages.
_DELIVERED_HIDDEN_STAGES: tuple[str, ...] = tuple(CATALOG_HIDDEN_STAGES)


class ArtifactLayout:
    """Resolved delivered paths for one staged application."""

    def __init__(self, artifact: Path) -> None:
        self.artifact = artifact
        if artifact.suffix == ".app" or artifact.is_dir():
            self.application_root = artifact / "Contents" / "MacOS"
            self.executable = self.application_root / _bundle_executable(artifact)
        else:
            self.executable = artifact
            self.application_root = artifact.parent
        worker_name = "NyxWorker.exe" if os.name == "nt" else "NyxWorker"
        self.helper = self.application_root / "NyxWorker" / worker_name
        self.resource = self.application_root / "nyx" / "static" / "app.js"


def _bundle_executable(bundle: Path) -> str:
    """Read the declared macOS bundle executable so the name stays authoritative."""

    plist = bundle / "Contents" / "Info.plist"
    try:
        import plistlib

        with plist.open("rb") as handle:
            executable = plistlib.load(handle).get("CFBundleExecutable")
    except (OSError, ValueError):
        executable = None
    return executable or "NyxApp"


@pytest.fixture(scope="module")
def artifact() -> ArtifactLayout:
    layout = ArtifactLayout(Path(_ARTIFACT_ENV).resolve())
    assert layout.executable.is_file(), f"staged application executable is missing: {layout.executable}"
    assert layout.helper.is_file(), f"bundled worker helper is missing: {layout.helper}"
    assert layout.resource.is_file(), f"bundled board resource is missing: {layout.resource}"
    return layout


def _system_path() -> str:
    if os.name == "nt":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        return f"{root}\\system32;{root}"
    return "/usr/bin:/bin:/usr/sbin:/sbin"


def _inert_path(root: Path) -> Path:
    """Return an empty directory offered as PYTHONPATH to the application."""

    decoy = root / "decoy"
    decoy.mkdir()
    return decoy


def _sanitized_environment(home: Path, decoy: Path) -> dict[str, str]:
    """Remove every route from the application back to the build environment."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("PYTHON")
        and key.upper() not in {"VIRTUAL_ENV", "PIP_REQUIRE_VIRTUALENV"}
    }
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    environment["PATH"] = _system_path()
    # Nuitka ignores PYTHONPATH, but setting it to an inert directory documents
    # that no checkout path is offered to the delivered application.
    environment["PYTHONPATH"] = str(decoy)
    return environment


def _write_configuration(
    home: Path,
    workspace: Path,
    *,
    hidden_stages: tuple[str, ...] = _DELIVERED_HIDDEN_STAGES,
) -> Path:
    config_directory = home / ".nyx" / "config"
    config_directory.mkdir(parents=True)
    config_file = config_directory / "config.json"
    payload = {
        "schema_version": state.CONFIG_SCHEMA_VERSION,
        "hidden_stages": list(hidden_stages),
        "specification_root": str(workspace.resolve()),
    }
    encoded = (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    config_file.write_bytes(encoded)
    return config_file


def _request(path: str) -> tuple[int, bytes, str]:
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=2.0)
    try:
        connection.request("GET", path, headers={"Host": _HOST})
        response = connection.getresponse()
        return response.status, response.read(), response.getheader("Content-Type") or ""
    finally:
        connection.close()


def _wait_for_board(
    deadline: float, gui: "_RunningGui | None" = None
) -> tuple[int, bytes, str]:
    last: object = None
    while time.monotonic() < deadline:
        try:
            status, body, content_type = _request("/api/catalog")
        except OSError as error:
            last = error
            time.sleep(0.25)
            continue
        if status == 200:
            return status, body, content_type
        last = f"unexpected board status {status}: {body[:400]!r}"
        time.sleep(0.25)
    detail = f"the delivered board never answered: {last!r}"
    if gui is not None:
        detail += f"; application exit={gui.process.poll()!r}"
        diagnostics = gui.diagnostics()
        if diagnostics:
            detail += f"; application log:\n{diagnostics[-2000:]}"
    raise AssertionError(detail)


def _wait_port_free(deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            _request("/")
        except OSError:
            return
        time.sleep(0.25)
    raise AssertionError("the board port was never released")


class _RunningGui:
    def __init__(self, process: subprocess.Popen, handle, log: Path) -> None:
        self.process = process
        self.handle = handle
        self.log = log

    def diagnostics(self) -> str:
        try:
            return self.log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "<no application log>"

    def close(self, timeout: float = 30.0) -> int:
        """Request the ordinary native close or quit, then require an exit."""

        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(self.process.pid)],
                capture_output=True,
                check=False,
            )
        else:
            subprocess.run(
                ["osascript", "-e", 'tell application "Nyx" to quit'],
                capture_output=True,
                check=False,
            )
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            return self.process.wait(timeout=timeout)


@contextlib.contextmanager
def _running_gui(artifact: ArtifactLayout, root: Path, environment: dict[str, str]):
    log = root / "application.log"
    handle = log.open("wb")
    process = subprocess.Popen(
        [str(artifact.executable)],
        cwd=str(root),
        env=environment,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    running = _RunningGui(process, handle, log)
    try:
        yield running
    finally:
        if process.poll() is None:
            running.close()
        handle.close()


@contextlib.contextmanager
def _workspace(root: Path):
    workspace = root / "workspace"
    shutil.copytree(_SOURCE_ROOT / "examples" / "sample-specifications", workspace)
    yield workspace


def test_delivered_artifact_carries_application_worker_and_resources(artifact: ArtifactLayout):
    assert artifact.application_root.is_dir()
    assert artifact.helper.parent.name == "NyxWorker"
    assert artifact.resource.read_bytes() == (_SOURCE_ROOT / "nyx" / "static" / "app.js").read_bytes()


def test_delivered_environment_exposes_no_build_interpreter_or_checkout_path():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        environment = _sanitized_environment(home, decoy)

    path_entries = environment["PATH"].split(os.pathsep)
    assert str(Path(sys.executable).resolve().parent) not in path_entries
    assert str(_SOURCE_ROOT) not in environment["PATH"]
    assert "PYTHONHOME" not in environment
    assert environment.get("VIRTUAL_ENV") is None
    assert str(_SOURCE_ROOT) not in environment["PYTHONPATH"]


def test_delivered_board_serves_exact_catalog_and_bundled_resources(artifact: ArtifactLayout):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        with _workspace(root) as workspace:
            _write_configuration(home, workspace)
            expected = json.loads(
                scan_catalog(workspace, hidden_stages=_DELIVERED_HIDDEN_STAGES)
            )
            environment = _sanitized_environment(home, decoy)
            with _running_gui(artifact, root, environment) as gui:
                status, body, content_type = _wait_for_board(
                    time.monotonic() + _START_TIMEOUT, gui
                )
                assert (status, content_type) == (200, "application/json")
                assert json.loads(body) == expected
                resource_status, resource_body, _ = _request("/static/app.js")
                assert resource_status == 200
                assert resource_body == (_SOURCE_ROOT / "nyx" / "static" / "app.js").read_bytes()
                index_status, _, index_type = _request("/")
                assert index_status == 200
                assert index_type == "text/html; charset=utf-8"
                assert gui.process.poll() is None
                code = gui.close()
                assert code == 0, gui.diagnostics()
            _wait_port_free(time.monotonic() + 15)
            _assert_claims_released(home)


def test_delivered_second_process_reports_already_open_without_competing_writes(
    artifact: ArtifactLayout,
):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        with _workspace(root) as workspace:
            config_file = _write_configuration(home, workspace)
            environment = _sanitized_environment(home, decoy)
            with _running_gui(artifact, root, environment) as gui:
                _wait_for_board(time.monotonic() + _START_TIMEOUT, gui)
                before = config_file.read_bytes()
                second = subprocess.run(
                    [str(artifact.executable)],
                    cwd=str(root),
                    env=environment,
                    capture_output=True,
                    timeout=60,
                )
                assert second.returncode == 1
                assert config_file.read_bytes() == before
                status, _, _ = _wait_for_board(time.monotonic() + 30, gui)
                assert status == 200
                gui.close()


def test_delivered_chooser_state_starts_no_runtime_or_workers(artifact: ArtifactLayout):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        environment = _sanitized_environment(home, decoy)
        lease_path, _ = _claim_paths(home)
        with _running_gui(artifact, root, environment) as gui:
            deadline = time.monotonic() + 10
            while not lease_path.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            time.sleep(3)
            with pytest.raises(OSError):
                _request("/api/catalog")
            assert gui.process.poll() is None
            gui.close()
        _assert_claims_released(home)


def test_delivered_worker_helper_emits_exact_utf8_catalog_bytes(artifact: ArtifactLayout):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        with _workspace(root) as workspace:
            _write_configuration(home, workspace)
            expected_text = scan_catalog(workspace, hidden_stages=_DELIVERED_HIDDEN_STAGES)
            completed = subprocess.run(
                [str(artifact.helper)],
                cwd=str(root),
                env=_sanitized_environment(home, decoy),
                capture_output=True,
            )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == expected_text.encode("utf-8")
    assert completed.stdout.decode("utf-8") == expected_text


def test_delivered_worker_helper_rejects_missing_or_substituted_inherited_objects(
    artifact: ArtifactLayout,
):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        environment = _sanitized_environment(home, decoy)
        partial = subprocess.run(
            [str(artifact.helper), "--recovery-fd", "0"],
            cwd=str(root),
            env=environment,
            capture_output=True,
        )
        substituted = subprocess.run(
            [
                str(artifact.helper),
                "--recovery-fd",
                "0",
                "--recovery-path",
                str(root / "substituted.lock"),
                "--parent-liveness-fd",
                "0",
            ],
            cwd=str(root),
            env=environment,
            capture_output=True,
        )
    assert partial.returncode == 4
    assert substituted.returncode == 4


def test_delivered_worker_helper_completes_with_inherited_claims(artifact: ArtifactLayout):
    """Drive the delivered helper the way the delivered shell does."""

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        with _workspace(root) as workspace:
            _write_configuration(home, workspace)
            expected_text = scan_catalog(workspace, hidden_stages=_DELIVERED_HIDDEN_STAGES)
            environment = _sanitized_environment(home, decoy)
            _, recovery_path = _claim_paths(home)
            recovery_path.parent.mkdir(parents=True, exist_ok=True)
            claim = NativeClaim(recovery_path)
            assert claim.acquire(blocking=False)
            read_fd, write_fd = os.pipe()
            manager = CatalogWorkerManager(
                command_factory=lambda: [str(artifact.helper)],
                timeout=30,
                recovery_claim=claim,
                recovery_path=claim.path,
                parent_liveness_fd=read_fd,
            )
            try:
                with patch.dict(os.environ, environment, clear=True):
                    catalog = manager.fetch_catalog()
                assert manager.close(time.monotonic() + 30)
                assert claim.held
            finally:
                manager.close(time.monotonic() + 30)
                os.close(write_fd)
                os.close(read_fd)
                claim.close()

    assert catalog == expected_text.encode("utf-8")
    assert manager.active_count == 0
    assert not claim.held


def test_delivered_gui_never_answers_worker_inheritance_with_a_window(artifact: ArtifactLayout):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        completed = subprocess.run(
            [
                str(artifact.executable),
                "--recovery-fd",
                "0",
                "--recovery-path",
                str(root / "recovery.lock"),
                "--parent-liveness-fd",
                "0",
            ],
            cwd=str(root),
            env=_sanitized_environment(home, decoy),
            capture_output=True,
            timeout=60,
        )
    assert completed.returncode == 2
    # A windowed build may own no standard stream; the bounded exit code is the
    # primary signal and the diagnostic is asserted only when it is emitted.
    if completed.stderr:
        assert b"private worker entry" in completed.stderr


def _claim_paths(home: Path) -> tuple[Path, Path]:
    with patch.object(state.Path, "home", return_value=home):
        paths = state.state_paths()
    return paths.runtime_directory / "lease.lock", paths.runtime_directory / "recovery.lock"


def _assert_claims_released(home: Path) -> None:
    lease_path, recovery_path = _claim_paths(home)
    for path in (lease_path, recovery_path):
        claim = NativeClaim(path)
        assert claim.acquire(blocking=False), f"claim still held after exit: {path}"
        claim.close()
