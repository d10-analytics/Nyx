"""Behavioral checks for the claim-held desktop chooser slice."""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nyx import _native_claim, app_runtime, desktop, runtime, state
from nyx._native_claim import NativeClaim

_NATIVE_REQUIRED = os.environ.get("NYX_REQUIRE_NATIVE_DESKTOP") == "1"
try:
    from PySide6 import (
        QtCore,
        QtTest,
        QtWebEngineCore,
        QtWebEngineWidgets,
        QtWidgets,
    )
except ImportError as _qt_error:  # Linux source collection remains dependency-light.
    QtCore = None
    QtTest = None
    QtWebEngineCore = None
    QtWebEngineWidgets = None
    QtWidgets = None
    if _NATIVE_REQUIRED:
        pytest.fail(
            f"required native desktop dependency is unavailable: {_qt_error}",
            allow_module_level=True,
        )

_NATIVE_BOARD = _NATIVE_REQUIRED and QtWebEngineWidgets is not None
_NATIVE_BOARD_SKIP = "required native QtWebEngine board lane is not enabled"


def _home(root: Path) -> Path:
    home = root / "home"
    home.mkdir()
    return home


def _workspace(root: Path, name: str) -> Path:
    workspace = root / name
    workspace.mkdir()
    return workspace


def _home_patches(home: Path):
    return patch.object(Path, "home", return_value=home), patch.object(
        state, "_current_uid", return_value=state._current_uid()
    )


class _Signal:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self):
        for callback in self._callbacks:
            callback()


class _Widget:
    def __init__(self, *_):
        self._enabled = True

    def isEnabled(self):
        return self._enabled

    def setEnabled(self, enabled):
        self._enabled = enabled


class _MainWindow(_Widget):
    def __init__(self, *_):
        super().__init__()
        self._visible = False

    def setWindowTitle(self, _):
        pass

    def resize(self, *_):
        pass

    def setCentralWidget(self, _):
        pass

    def show(self):
        self._visible = True

    def close(self):
        event = SimpleNamespace(accept=lambda: None)
        self.closeEvent(event)
        self._visible = False


class _LineEdit(_Widget):
    def __init__(self, *_):
        super().__init__()
        self._text = ""

    def setText(self, value):
        self._text = value

    def text(self):
        return self._text


class _Label(_LineEdit):
    def setWordWrap(self, _):
        pass


class _Button(_Widget):
    def __init__(self, *_):
        super().__init__()
        self.clicked = _Signal()

    def click(self):
        if self.isEnabled():
            self.clicked.emit()


class _Layout:
    def __init__(self, *_):
        pass

    def addRow(self, *_):
        pass

    def addWidget(self, *_):
        pass

    def addLayout(self, *_):
        pass


class _WebSignal:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args):
        for callback in self._callbacks:
            callback(*args)


class _WebEngineProfile:
    def __init__(self, name=None, parent=None):
        self._name = name
        self._persistent_path = None
        self._cache_path = None
        self._off_the_record = name is None

    def setPersistentStoragePath(self, path):
        self._persistent_path = path

    def persistentStoragePath(self):
        return self._persistent_path

    def setCachePath(self, path):
        self._cache_path = path

    def cachePath(self):
        return self._cache_path

    def isOffTheRecord(self):
        return self._off_the_record


class _WebEnginePage:
    def __init__(self, profile=None, parent=None):
        self._profile = profile

    def profile(self):
        return self._profile

    def runJavaScript(self, script, callback=None, *_):
        if callback is not None:
            callback(None)


class _WebEngineView(_Widget):
    def __init__(self, *_):
        super().__init__()
        self.loadFinished = _WebSignal()
        self._url = None
        self._page = None

    def setPage(self, page):
        self._page = page

    def page(self):
        return self._page

    def setUrl(self, url):
        self._url = url

    def url(self):
        return self._url


def _fake_qt():
    return {
        "QtCore": SimpleNamespace(QUrl=lambda value: value),
        "QtWidgets": SimpleNamespace(
            QFormLayout=_Layout,
            QHBoxLayout=_Layout,
            QLabel=_Label,
            QLineEdit=_LineEdit,
            QMainWindow=_MainWindow,
            QPushButton=_Button,
            QVBoxLayout=_Layout,
            QWidget=_Widget,
        ),
        "QtWebEngineCore": SimpleNamespace(
            QWebEnginePage=_WebEnginePage,
            QWebEngineProfile=_WebEngineProfile,
        ),
        "QtWebEngineWidgets": SimpleNamespace(QWebEngineView=_WebEngineView),
    }


_CRASH_WORKER_CODE = r"""
import argparse
import os
import time
from pathlib import Path

from nyx import worker

phase = os.environ["NYX_TEST_CRASH_PHASE"]
marker_root = Path(os.environ["NYX_TEST_MARKER_ROOT"])
real_exit = os._exit

def delayed_parent_loss_exit(code):
    (marker_root / "parent-lost").write_text(str(code), encoding="utf-8")
    while not (marker_root / "allow-worker-exit").exists():
        time.sleep(0.01)
    real_exit(code)

worker.os._exit = delayed_parent_loss_exit
if phase == "after_spawn_before_registration":
    real_load = worker.state.load_configuration
    def blocked_load():
        (marker_root / "before-config-load").write_text(str(os.getpid()), encoding="utf-8")
        while not (marker_root / "allow-config-load").exists():
            time.sleep(0.01)
        return real_load()
    worker.state.load_configuration = blocked_load
elif phase == "during_scan":
    def blocked_scan(*args, **kwargs):
        (marker_root / "during-scan").write_text(str(os.getpid()), encoding="utf-8")
        while not (marker_root / "allow-scan").exists():
            time.sleep(0.01)
        return "late"
    worker.scan_catalog = blocked_scan

parser = argparse.ArgumentParser()
parser.add_argument("--recovery-fd", type=int, required=True)
parser.add_argument("--recovery-path", type=Path, required=True)
parser.add_argument("--parent-liveness-fd", type=int, required=True)
options = parser.parse_args()
raise SystemExit(worker._worker_main(
    recovery_fd=options.recovery_fd,
    recovery_path=options.recovery_path,
    parent_liveness_fd=options.parent_liveness_fd,
))
"""


_CRASH_SHELL_CODE = r"""
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from nyx import desktop, worker

phase = os.environ["NYX_TEST_CRASH_PHASE"]
marker_root = Path(os.environ["NYX_TEST_MARKER_ROOT"])
desktop.runtime.PORT = int(os.environ["NYX_TEST_PORT"])
real_popen = subprocess.Popen

def scheduled_popen(*args, **kwargs):
    if phase == "before_spawn":
        (marker_root / "before-spawn").write_text("entered", encoding="utf-8")
        while not (marker_root / "allow-spawn").exists():
            time.sleep(0.01)
    process = real_popen(*args, **kwargs)
    if phase == "after_spawn_before_registration":
        (marker_root / "spawned-unregistered").write_text(
            str(process.pid), encoding="utf-8"
        )
        while not (marker_root / "allow-registration").exists():
            time.sleep(0.01)
    return process

worker.subprocess.Popen = scheduled_popen
manager_type = desktop.CatalogWorkerManager
def desktop_workers(**kwargs):
    return manager_type(
        command_factory=lambda: [sys.executable, "-c", os.environ["NYX_TEST_WORKER_CODE"]],
        **kwargs,
    )
desktop.CatalogWorkerManager = desktop_workers

class Signal:
    def __init__(self): self.callbacks = []
    def connect(self, callback): self.callbacks.append(callback)
class Widget:
    def __init__(self, *args): self.enabled = True
    def setEnabled(self, enabled): self.enabled = enabled
class MainWindow(Widget):
    def setWindowTitle(self, value): pass
    def resize(self, *args): pass
    def setCentralWidget(self, value): pass
    def close(self): pass
    def show(self):
        (marker_root / "shell-visible").write_text(str(os.getpid()), encoding="utf-8")
class LineEdit(Widget):
    def __init__(self, *args): super().__init__(*args); self.value = ""
    def setText(self, value): self.value = value
    def text(self): return self.value
class Label(LineEdit):
    def setWordWrap(self, value): pass
class Button(Widget):
    def __init__(self, *args): super().__init__(*args); self.clicked = Signal()
class Layout:
    def __init__(self, *args): pass
    def addRow(self, *args): pass
    def addWidget(self, *args): pass
    def addLayout(self, *args): pass
class WebProfile:
    def __init__(self, *args): pass
    def setPersistentStoragePath(self, value): pass
    def setCachePath(self, value): pass
class WebPage:
    def __init__(self, *args): pass
class WebView(Widget):
    def __init__(self, *args): super().__init__(*args); self.loadFinished = Signal()
    def setPage(self, value): pass
    def setUrl(self, value): pass
class Application:
    @classmethod
    def instance(cls): return None
    def __init__(self, *args): pass
    def exec(self):
        while True: time.sleep(1)

widgets = SimpleNamespace(
    QApplication=Application, QFormLayout=Layout, QHBoxLayout=Layout,
    QLabel=Label, QLineEdit=LineEdit, QMainWindow=MainWindow,
    QPushButton=Button, QVBoxLayout=Layout, QWidget=Widget,
)
web_widgets = SimpleNamespace(QWebEngineView=WebView)
web_core = SimpleNamespace(QWebEnginePage=WebPage, QWebEngineProfile=WebProfile)
desktop._load_qt = lambda: {
    "QtCore": SimpleNamespace(QUrl=lambda value: value),
    "QtWidgets": widgets,
    "QtWebEngineCore": web_core,
    "QtWebEngineWidgets": web_widgets,
}
raise SystemExit(desktop.main([]))
"""


def _wait_for_path(path: Path, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), f"timed out waiting for {path.name}"


def test_module_import_does_not_require_qt(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "PySide6", None)
    assert desktop.APPLICATION_CLAIM_FILENAME == "lease.lock"


def test_first_launch_cancel_releases_both_claims_without_creating_configuration():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        with _home_patches(home)[0]:
            session = desktop.DesktopSession()
            paths = state.state_paths()
            assert session.snapshot.status == "not_configured"
            assert session.claims.held
            assert paths.config_file is not None and not paths.config_file.exists()
            session.cancel()
            assert not paths.config_file.exists()
            assert NativeClaim.probe(paths.runtime_directory / "lease.lock") == "free"
            assert NativeClaim.probe(paths.runtime_directory / "recovery.lock") == "free"


def test_replacement_holds_application_but_blocks_save_and_start_until_recovery_retry():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")
        with _home_patches(home)[0]:
            state.setup(workspace)
            paths = state.state_paths()
            former_worker = NativeClaim(paths.runtime_directory / "recovery.lock")
            assert former_worker.acquire(blocking=False)
            session = desktop.DesktopSession()
            before = paths.config_file.read_bytes()
            assert session.recovery_blocked
            assert session.snapshot.status == "recovery_blocked"
            with pytest.raises(desktop.SelectionUnavailableError):
                session.choose_workspace(workspace)
            with pytest.raises(desktop.DesktopUnavailableError):
                session.start_runtime()
            assert paths.config_file.read_bytes() == before
            assert not session.retry_recovery()
            former_worker.close()
            assert session.retry_recovery()
            assert session.claims.held
            session.close()


def test_desktop_starts_shared_runtime_with_worker_only_inheritance():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")
        with _home_patches(home)[0]:
            state.setup(workspace)
            observed = {}

            class FakeApplicationRuntime:
                def __init__(self, **kwargs):
                    observed.update(kwargs)

                def start(self, *, static_ready=None):
                    assert static_ready is not None and static_ready()

                def admit_catalog(self):
                    observed["admitted"] = True

                def shutdown(self, _deadline):
                    return True

            with patch.object(desktop, "ApplicationRuntime", FakeApplicationRuntime):
                session = desktop.DesktopSession()
                application = session.start_runtime(static_ready=lambda: True)
                assert isinstance(application, FakeApplicationRuntime)
                workers = observed["workers"]
                assert workers._recovery_claim is session.claims.recovery
                assert workers._parent_liveness_fd == session.claims.parent_liveness_read
                assert observed["admitted"]
                session.close()


def test_first_launch_valid_selection_uses_canonical_state_owner_and_hidden_stages():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")
        supplied = root / "workspace-link"
        supplied.symlink_to(workspace, target_is_directory=True)
        with _home_patches(home)[0]:
            session = desktop.DesktopSession()
            result = session.choose_workspace(supplied, ["Done", "Queue", "Done"])
            paths = state.state_paths()
            assert result.specification_root == workspace.resolve()
            assert result.hidden_stages == ("Done", "Queue")
            assert state.load_configuration(paths) == result
            assert json.loads(paths.config_file.read_text(encoding="utf-8")) == {
                "hidden_stages": ["Done", "Queue"],
                "schema_version": 2,
                "specification_root": str(workspace.resolve()),
            }
            session.close()


def test_replacing_workspace_without_policy_input_preserves_hidden_stages():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        with _home_patches(home)[0]:
            state.setup(first, ["Queue"])
            session = desktop.DesktopSession()
            session.choose_workspace(second)
            assert state.load_configuration().hidden_stages == ("Queue",)
            session.close()


def test_competing_legacy_setup_cannot_mutate_while_desktop_claim_is_held():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        with _home_patches(home)[0]:
            session = desktop.DesktopSession()
            with pytest.raises(runtime.ActiveInstanceError):
                state.setup(first)
            assert not state.state_paths().config_file.exists()
            session.close()
            state.setup(second)
            assert state.load_configuration().specification_root == second.resolve()


def test_separate_process_reports_busy_without_writing_or_starting_runtime():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        with _home_patches(home)[0]:
            first = desktop.DesktopSession()
            paths = state.state_paths()
            lease_path = paths.runtime_directory / desktop.APPLICATION_CLAIM_FILENAME
            recovery_path = paths.runtime_directory / desktop.RECOVERY_CLAIM_FILENAME
            operation_path = paths.runtime_directory / "operation.lock"

            def snapshot_readable_runtime_state():
                details = operation_path.lstat()
                return {
                    operation_path.name: (
                        _native_claim._identity(details),
                        operation_path.read_bytes(),
                    )
                }

            try:
                lease_fd = first.claims.application.fd
                recovery_fd = first.claims.recovery.fd
                assert lease_fd is not None
                assert recovery_fd is not None
                lease_before = _native_claim._identity(os.fstat(lease_fd))
                recovery_before = _native_claim._identity(os.fstat(recovery_fd))
                runtime_names_before = {
                    path.name for path in paths.runtime_directory.iterdir()
                }
                readable_before = snapshot_readable_runtime_state()

                environment = os.environ.copy()
                environment["HOME"] = str(home)
                environment["USERPROFILE"] = str(home)
                environment.pop("PYTHONHOME", None)
                result = subprocess.run(
                    [sys.executable, "-m", "nyx.desktop"],
                    cwd=Path(__file__).parents[1],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                lease_after = _native_claim._identity(os.fstat(lease_fd))
                recovery_after = _native_claim._identity(os.fstat(recovery_fd))
                runtime_names_after = {
                    path.name for path in paths.runtime_directory.iterdir()
                }
                readable_after = snapshot_readable_runtime_state()
            finally:
                first.close()

            assert result.returncode == 1
            assert result.stdout == ""
            assert result.stderr == f"{desktop.ALREADY_OPEN_MESSAGE}\n"
            assert not paths.config_file.exists()
            assert not paths.runtime_directory.joinpath("instance.json").exists()
            assert lease_after == lease_before
            assert recovery_after == recovery_before
            assert runtime_names_after == runtime_names_before
            assert readable_after == readable_before
            assert _native_claim._identity(lease_path.lstat()) == lease_before
            assert _native_claim._identity(recovery_path.lstat()) == recovery_before
            assert lease_path.read_bytes() == b"\0"
            assert recovery_path.read_bytes() == b"\0"
            assert NativeClaim.probe(lease_path) == "free"
            assert NativeClaim.probe(recovery_path) == "free"
            second = desktop.DesktopSession()
            try:
                assert second.claims.held
            finally:
                second.close()


def test_unrelated_helper_inherits_no_desktop_claim_or_liveness_object():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")
        with _home_patches(home)[0]:
            state.setup(workspace)
            session = desktop.DesktopSession()
            descriptors = (
                session.claims.application.fd,
                session.claims.recovery.fd,
                session.claims.parent_liveness_write,
            )
            assert all(descriptor is not None for descriptor in descriptors)
            expected = [
                (
                    os.fstat(int(descriptor)).st_dev,
                    os.fstat(int(descriptor)).st_ino,
                    os.fstat(int(descriptor)).st_mode,
                )
                for descriptor in descriptors
            ]
            helper = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import json,os,sys\n"
                        "seen=[]\n"
                        "for value in sys.argv[1:]:\n"
                        " try:\n"
                        "  details=os.fstat(int(value))\n"
                        "  seen.append([details.st_dev,details.st_ino,details.st_mode])\n"
                        " except OSError:\n"
                        "  seen.append(None)\n"
                        "print(json.dumps(seen))\n"
                    ),
                    *(str(descriptor) for descriptor in descriptors),
                ],
                cwd=Path(__file__).parents[1],
                check=True,
                capture_output=True,
                text=True,
            )
            inherited = json.loads(helper.stdout)
            assert all(observed != list(identity) for observed, identity in zip(inherited, expected))
            assert session.claims.held
            session.close()


@pytest.mark.parametrize(
    ("phase", "stage_marker", "worker_expected"),
    [
        ("before_spawn", "before-spawn", False),
        (
            "after_spawn_before_registration",
            "before-config-load",
            True,
        ),
        ("during_scan", "during-scan", True),
    ],
)
def test_actual_desktop_http_parent_loss_blocks_replacement_until_worker_terminal(
    phase,
    stage_marker,
    worker_expected,
):
    probe = None
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    except PermissionError:
        pytest.skip("sandbox does not permit loopback sockets")
    finally:
        if probe is not None:
            probe.close()

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")
        marker_root = root / "markers"
        marker_root.mkdir()
        with _home_patches(home)[0]:
            state.setup(workspace)
            paths = state.state_paths()
            configuration_bytes = paths.config_file.read_bytes()

        environment = os.environ.copy()
        environment.update(
            HOME=str(home),
            USERPROFILE=str(home),
            NYX_TEST_CRASH_PHASE=phase,
            NYX_TEST_MARKER_ROOT=str(marker_root),
            NYX_TEST_PORT=str(port),
            NYX_TEST_WORKER_CODE=_CRASH_WORKER_CODE,
        )
        environment.pop("PYTHONHOME", None)
        shell = subprocess.Popen(
            [sys.executable, "-c", _CRASH_SHELL_CODE],
            cwd=Path(__file__).parents[1],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        replacement = None
        demand_errors = []

        def demand_catalog():
            connection = http.client.HTTPConnection(
                "127.0.0.1", port, timeout=10
            )
            try:
                connection.request(
                    "GET",
                    "/api/catalog",
                    headers={"Host": f"127.0.0.1:{port}"},
                )
                response = connection.getresponse()
                response.read()
            except (OSError, http.client.HTTPException) as error:
                demand_errors.append(type(error).__name__)
            finally:
                connection.close()

        try:
            try:
                _wait_for_path(marker_root / "shell-visible")
            except AssertionError:
                if shell.poll() is not None:
                    stdout, stderr = shell.communicate(timeout=1)
                    pytest.fail(
                        f"desktop shell exited {shell.returncode}: stdout={stdout!r} "
                        f"stderr={stderr!r}"
                    )
                raise
            demand = threading.Thread(target=demand_catalog, daemon=True)
            demand.start()
            _wait_for_path(marker_root / stage_marker)

            shell.kill()
            assert shell.wait(timeout=5) != 0
            if worker_expected:
                _wait_for_path(marker_root / "parent-lost")

            with (
                _home_patches(home)[0],
                patch.object(runtime, "PORT", port),
                patch.dict(
                    os.environ,
                    {"HOME": str(home), "USERPROFILE": str(home)},
                ),
            ):
                replacement = desktop.DesktopSession()
                assert replacement.claims.application.held
                assert replacement.recovery_blocked is worker_expected
                assert replacement.runtime is None
                assert paths.config_file.read_bytes() == configuration_bytes
                if worker_expected:
                    with pytest.raises(desktop.DesktopUnavailableError):
                        replacement.start_runtime()

                third = subprocess.run(
                    [sys.executable, "-m", "nyx.desktop"],
                    cwd=Path(__file__).parents[1],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                assert third.returncode == 1
                assert third.stdout == ""
                assert third.stderr == f"{desktop.ALREADY_OPEN_MESSAGE}\n"
                assert paths.config_file.read_bytes() == configuration_bytes

                if worker_expected:
                    (marker_root / "allow-worker-exit").touch()
                    deadline = time.monotonic() + 5
                    while not replacement.retry_recovery() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert replacement.claims.held

                application = replacement.start_runtime()
                connection = http.client.HTTPConnection(
                    "127.0.0.1", port, timeout=5
                )
                connection.request(
                    "GET",
                    "/api/catalog",
                    headers={"Host": f"127.0.0.1:{port}"},
                )
                response = connection.getresponse()
                assert response.status == 200
                assert json.loads(response.read())["schema_version"] == 4
                connection.close()
                assert application is replacement.runtime
                replacement.close()
                assert not replacement.claims.held
            demand.join(timeout=5)
            assert not demand.is_alive()
            if worker_expected:
                assert demand_errors
        finally:
            (marker_root / "allow-worker-exit").touch()
            (marker_root / "allow-spawn").touch()
            (marker_root / "allow-registration").touch()
            (marker_root / "allow-config-load").touch()
            (marker_root / "allow-scan").touch()
            if replacement is not None:
                replacement.close()
            if shell.poll() is None:
                shell.kill()
                shell.wait(timeout=5)


def test_unsafe_runtime_ancestry_is_unavailable_not_already_open():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        outside = root / "outside"
        outside.mkdir()
        managed = home / ".nyx"
        managed.mkdir()
        (managed / "config").mkdir()
        (managed / "runtime").symlink_to(outside, target_is_directory=True)
        with _home_patches(home)[0], pytest.raises(desktop.DesktopUnavailableError):
            desktop.DesktopSession()
        assert not (outside / "lease.lock").exists()
        assert not (outside / "recovery.lock").exists()


def test_first_launch_pre_replacement_failure_preserves_absence():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")
        with _home_patches(home)[0]:
            session = desktop.DesktopSession()
            with patch.object(state.os, "replace", side_effect=OSError("injected")), pytest.raises(
                desktop.SelectionUnavailableError
            ):
                session.choose_workspace(workspace)
            assert session.snapshot.status == "not_configured"
            assert not state.state_paths().config_file.exists()
            session.close()


def test_post_replacement_failure_keeps_committed_bytes_unverified_and_revalidation_unblocks():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        with _home_patches(home)[0]:
            state.setup(first)
            paths = state.state_paths()
            original_verify = state._verify_record
            failures = 0

            def fail_once(path):
                nonlocal failures
                details = original_verify(path)
                if path == paths.config_file and json.loads(path.read_text())["specification_root"] == str(
                    second.resolve()
                ) and failures == 0:
                    failures += 1
                    raise OSError("post-replacement verification failed")
                return details

            with patch.object(state, "_verify_record", side_effect=fail_once):
                session = desktop.DesktopSession()
                with pytest.raises(desktop.SelectionUnavailableError):
                    session.choose_workspace(second, ["Queue"])
                assert session.unverified
                assert json.loads(paths.config_file.read_text(encoding="utf-8"))["specification_root"] == str(
                    second.resolve()
                )
                with pytest.raises(desktop.SelectionUnavailableError):
                    session.choose_workspace(first)
            assert session.revalidate().specification_root == second.resolve()
            assert not session.unverified
            session.close()


def test_malformed_configuration_is_unavailable_and_cannot_be_overwritten_by_chooser():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")
        with _home_patches(home)[0]:
            paths = state.state_paths(create=True)
            paths.config_file.write_text("not json\n", encoding="utf-8")
            os.chmod(paths.config_file, 0o600)
            before = paths.config_file.read_bytes()
            session = desktop.DesktopSession()
            assert session.snapshot.status == "unavailable"
            with pytest.raises(desktop.SelectionUnavailableError):
                session.choose_workspace(workspace)
            assert paths.config_file.read_bytes() == before
            session.close()


def test_configured_shell_change_action_saves_replacement_and_preserves_policy():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        with _home_patches(home)[0]:
            state.setup(first, ["Queue"])
            session = desktop.DesktopSession()
            window = desktop._build_window(_fake_qt(), session)
            assert window._change.isEnabled()
            assert not window._save.isEnabled()
            assert not window._root.isEnabled()

            window._change.click()
            assert not window._change.isEnabled()
            assert window._save.isEnabled()
            assert window._root.isEnabled()
            window._root.setText(str(second))
            window._save.click()

            assert state.load_configuration() == state.Configuration(
                second.resolve(), ("Queue",)
            )
            assert window._change.isEnabled()
            assert not window._save.isEnabled()
            assert not window._root.isEnabled()
            window._quit.click()
            assert not session.claims.held


def test_shell_pre_replacement_failure_preserves_prior_bytes_and_retry_state():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        with _home_patches(home)[0]:
            state.setup(first, ["Queue"])
            paths = state.state_paths()
            before = paths.config_file.read_bytes()
            session = desktop.DesktopSession()
            window = desktop._build_window(_fake_qt(), session)
            window._change.click()
            window._root.setText(str(second))

            with patch.object(state.os, "replace", side_effect=OSError("injected")):
                window._save.click()

            assert paths.config_file.read_bytes() == before
            assert window._status.text() == "Nyx workspace could not be saved"
            assert window._save.isEnabled()
            assert not window._change.isEnabled()
            assert not paths.runtime_directory.joinpath("instance.json").exists()
            window._quit.click()
            assert paths.config_file.read_bytes() == before


def test_shell_post_replacement_failure_blocks_change_until_revalidation():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        with _home_patches(home)[0]:
            state.setup(first, ["Queue"])
            paths = state.state_paths()
            original_verify = state._verify_record
            replacement_failed = False
            allow_revalidation = False

            def fail_until_revalidation_allowed(path):
                nonlocal replacement_failed
                details = original_verify(path)
                payload = json.loads(path.read_text(encoding="utf-8"))
                if (
                    path == paths.config_file
                    and payload["specification_root"] == str(second.resolve())
                    and (not replacement_failed or not allow_revalidation)
                ):
                    replacement_failed = True
                    raise OSError("injected post-replacement verification failure")
                return details

            session = desktop.DesktopSession()
            window = desktop._build_window(_fake_qt(), session)
            window._change.click()
            window._root.setText(str(second))
            with patch.object(
                state, "_verify_record", side_effect=fail_until_revalidation_allowed
            ):
                window._save.click()
                committed = paths.config_file.read_bytes()
                assert json.loads(committed)["specification_root"] == str(second.resolve())
                assert json.loads(committed)["hidden_stages"] == ["Queue"]
                assert window._status.text() == "Nyx configuration was saved but is unverified"
                assert not window._change.isEnabled()
                assert not window._save.isEnabled()
                assert not window._root.isEnabled()
                assert window._retry.isEnabled()
                window._change.click()
                assert paths.config_file.read_bytes() == committed
                window._retry.click()
                assert session.snapshot.status == "unavailable"
                assert session.unverified
                assert window._status.text() == "Nyx configuration remains unavailable"
                assert not window._save.isEnabled()
                assert window._retry.isEnabled()
                allow_revalidation = True
                window._retry.click()

            assert session.snapshot.status == "configured"
            assert not session.unverified
            assert window._change.isEnabled()
            assert not window._save.isEnabled()
            assert not window._retry.isEnabled()
            assert window._root.text() == str(second.resolve())
            assert not paths.runtime_directory.joinpath("instance.json").exists()
            window._quit.click()


def test_active_workspace_switch_keeps_claims_and_retries_incomplete_stop():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        shutdown_results = iter((False, True, True))
        instances = []

        class FakeApplicationRuntime:
            def __init__(self, **_kwargs):
                self.shutdown_calls = 0
                instances.append(self)

            def start(self, *, static_ready=None):
                assert static_ready is None or static_ready()

            def admit_catalog(self):
                pass

            def shutdown(self, _deadline):
                self.shutdown_calls += 1
                return next(shutdown_results)

            def cleanup_start_failure(self, _deadline):
                return True

        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", FakeApplicationRuntime
        ):
            state.setup(first, ["Queue"])
            session = desktop.DesktopSession()
            session.start_runtime(static_ready=lambda: True)
            before = state.state_paths().config_file.read_bytes()

            with pytest.raises(desktop.SelectionUnavailableError):
                session.choose_workspace(second)
            assert state.state_paths().config_file.read_bytes() == before
            assert session.snapshot.status == "switch_blocked"
            assert session.switch_blocked
            assert session.claims.held
            assert session.retry_workspace_switch()
            assert state.load_configuration() == state.Configuration(
                second.resolve(), ("Queue",)
            )
            assert session.runtime is instances[1]
            assert session.claims.held
            session.close()


def test_repeated_incomplete_switch_retry_keeps_retry_visible():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")

        class FakeApplicationRuntime:
            def __init__(self, **_kwargs):
                self.cleanup_allowed = False

            def start(self, *, static_ready=None):
                assert static_ready is None or static_ready()

            def admit_catalog(self):
                pass

            def shutdown(self, _deadline):
                return self.cleanup_allowed

            def cleanup_start_failure(self, _deadline):
                return False

        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", FakeApplicationRuntime
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            application = session.start_runtime(static_ready=lambda: True)
            window = desktop._build_window(_fake_qt(), session)
            window._change.click()
            window._root.setText(str(second))
            window._save.click()
            assert session.switch_blocked
            assert window._retry.isEnabled()

            window._retry.click()

            assert session.switch_blocked
            assert session.snapshot.status == "switch_blocked"
            assert window._retry.isEnabled()
            application.cleanup_allowed = True
            session.close()
            assert not session.claims.held


def test_postcommit_switch_retry_revalidates_then_restarts_saved_workspace():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        instances = []

        class FakeApplicationRuntime:
            def __init__(self, **_kwargs):
                instances.append(self)

            def start(self, *, static_ready=None):
                assert static_ready is None or static_ready()

            def admit_catalog(self):
                pass

            def shutdown(self, _deadline):
                return True

            def cleanup_start_failure(self, _deadline):
                return True

        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", FakeApplicationRuntime
        ):
            state.setup(first)
            paths = state.state_paths()
            session = desktop.DesktopSession()
            session.start_runtime(static_ready=lambda: True)
            window = desktop._build_window(_fake_qt(), session)
            window._change.click()
            window._root.setText(str(second))
            original_verify = state._verify_record
            failed = False

            def fail_once(path):
                nonlocal failed
                details = original_verify(path)
                if (
                    path == paths.config_file
                    and json.loads(path.read_text(encoding="utf-8"))["specification_root"]
                    == str(second.resolve())
                    and not failed
                ):
                    failed = True
                    raise OSError("injected post-replacement verification failure")
                return details

            with patch.object(state, "_verify_record", side_effect=fail_once):
                window._save.click()
                assert session.unverified
                assert session.runtime is None
                assert window._retry.isEnabled()

            window._retry.click()

            assert not session.unverified
            assert session.configuration == state.Configuration(second.resolve(), ())
            assert session.runtime is instances[1]
            session.close()


def test_active_workspace_switch_restarts_real_catalog_worker_over_http():
    try:
        capability_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except PermissionError:
        pytest.skip("sandbox does not permit loopback sockets")
    try:
        capability_probe.bind(("127.0.0.1", 0))
        port = capability_probe.getsockname()[1]
    except PermissionError:
        pytest.skip("sandbox does not permit loopback sockets")
    finally:
        capability_probe.close()

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")

        def request_catalog():
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("GET", "/api/catalog", headers={"Host": f"127.0.0.1:{port}"})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        with (
            _home_patches(home)[0],
            patch.object(runtime, "PORT", port),
            patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            try:
                session.start_runtime()
                first_status, first_catalog = request_catalog()
                assert first_status == 200
                assert isinstance(first_catalog, dict)
                session.choose_workspace(second)
                second_status, second_catalog = request_catalog()
                assert second_status == 200
                assert isinstance(second_catalog, dict)
                assert state.load_configuration().specification_root == second.resolve()
                assert session.claims.held
            finally:
                session.close()


def test_active_workspace_switch_http_response_comes_from_new_workspace():
    try:
        capability_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except PermissionError:
        pytest.skip("sandbox does not permit loopback sockets")
    try:
        capability_probe.bind(("127.0.0.1", 0))
        port = capability_probe.getsockname()[1]
    except PermissionError:
        pytest.skip("sandbox does not permit loopback sockets")
    finally:
        capability_probe.close()

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        for workspace, title in ((first, "FIRST"), (second, "SECOND")):
            package = workspace / "Fictional" / "Queue" / "sample"
            package.mkdir(parents=True)
            package.joinpath("spec.md").write_text(
                f"# {title}\nStatus: approved\nClosure: approved\n",
                encoding="utf-8",
            )

        def request_catalog():
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request(
                    "GET",
                    "/api/catalog",
                    headers={"Host": f"127.0.0.1:{port}"},
                )
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        with (
            _home_patches(home)[0],
            patch.object(runtime, "PORT", port),
            patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            try:
                session.start_runtime()
                first_status, first_catalog = request_catalog()
                assert first_status == 200
                assert [entry["declared"]["title"] for entry in first_catalog["entries"]] == [
                    "FIRST"
                ]
                session.choose_workspace(second)
                second_status, second_catalog = request_catalog()
                assert second_status == 200
                assert [entry["declared"]["title"] for entry in second_catalog["entries"]] == [
                    "SECOND"
                ]
            finally:
                session.close()


def test_active_switch_postcommit_verification_failure_preserves_b_on_quit():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        with _home_patches(home)[0]:
            state.setup(first, ["Queue"])
            paths = state.state_paths()
            session = desktop.DesktopSession()

            class FakeApplicationRuntime:
                def __init__(self, **_kwargs):
                    pass

                def start(self, *, static_ready=None):
                    assert static_ready is None or static_ready()

                def admit_catalog(self):
                    pass

                def shutdown(self, _deadline):
                    return True

                def cleanup_start_failure(self, _deadline):
                    return True

            session_start = patch.object(
                desktop, "ApplicationRuntime", FakeApplicationRuntime
            )
            original_verify = state._verify_record
            failed = False

            def fail_after_replacement(path):
                nonlocal failed
                details = original_verify(path)
                if (
                    path == paths.config_file
                    and json.loads(path.read_text(encoding="utf-8"))["specification_root"]
                    == str(second.resolve())
                    and not failed
                ):
                    failed = True
                    raise OSError("injected post-replacement verification failure")
                return details

            with session_start:
                session.start_runtime(static_ready=lambda: True)
                with patch.object(state, "_verify_record", side_effect=fail_after_replacement):
                    with pytest.raises(desktop.SelectionUnavailableError):
                        session.choose_workspace(second)
                committed = paths.config_file.read_bytes()
                assert json.loads(committed)["specification_root"] == str(second.resolve())
                assert session.unverified
                assert session.runtime is None
                session.close()
                assert paths.config_file.read_bytes() == committed
                assert not session.claims.held


def test_verified_switch_can_leave_saved_workspace_without_running_runtime():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")

        class FakeApplicationRuntime:
            created = 0

            def __init__(self, **_kwargs):
                self.index = FakeApplicationRuntime.created
                FakeApplicationRuntime.created += 1

            def start(self, *, static_ready=None):
                if self.index == 1:
                    raise RuntimeError("injected startup failure")
                assert static_ready is None or static_ready()

            def admit_catalog(self):
                pass

            def shutdown(self, _deadline):
                return True

            def cleanup_start_failure(self, _deadline):
                return True

        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", FakeApplicationRuntime
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            session.start_runtime(static_ready=lambda: True)
            with pytest.raises(desktop.DesktopUnavailableError):
                session.choose_workspace(second)
            assert state.load_configuration() == state.Configuration(second.resolve(), ())
            assert session.runtime is None
            assert session.runtime_retryable
            assert session.claims.held
            session.close()
            assert state.load_configuration() == state.Configuration(second.resolve(), ())


def test_unavailable_shell_disables_change_save_and_revalidation():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        with _home_patches(home)[0]:
            paths = state.state_paths(create=True)
            paths.config_file.write_text("not json\n", encoding="utf-8")
            os.chmod(paths.config_file, 0o600)
            session = desktop.DesktopSession()
            window = desktop._build_window(_fake_qt(), session)
            assert not window._change.isEnabled()
            assert not window._save.isEnabled()
            assert not window._root.isEnabled()
            assert not window._retry.isEnabled()
            window._quit.click()


def test_close_failure_keeps_desktop_visible_with_retryable_cleanup():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")
        with _home_patches(home)[0]:
            state.setup(workspace)
            session = desktop.DesktopSession()

            class RuntimeThatCannotClose:
                def shutdown(self, _deadline):
                    return False

            session._application_runtime = RuntimeThatCannotClose()
            window = desktop._build_window(_fake_qt(), session)
            event = SimpleNamespace(accepted=False, ignored=False)
            event.accept = lambda: setattr(event, "accepted", True)
            event.ignore = lambda: setattr(event, "ignored", True)

            window.closeEvent(event)

            assert not event.accepted
            assert event.ignored
            assert session.claims.held
            assert window._retry.isEnabled()
            assert "retry" in window._status.text().lower()
            session._application_runtime = None
            session.close()


def test_configured_desktop_entry_admits_shared_runtime_before_running_shell():
    calls = []

    class Session:
        snapshot = SimpleNamespace(status="configured")

        def start_runtime(self):
            calls.append("start_runtime")

        def close(self):
            calls.append("close")

    class Application:
        @classmethod
        def instance(cls):
            return None

        def __init__(self, *_):
            pass

        def exec(self):
            return 0

    class Widgets:
        QApplication = Application

    window = SimpleNamespace(
        show=lambda: calls.append("show"),
        _open_board=lambda: calls.append("open_board"),
    )
    with (
        patch.object(desktop, "DesktopSession", Session),
        patch.object(desktop, "_load_qt", return_value={"QtWidgets": Widgets}),
        patch.object(desktop, "_build_window", return_value=window),
    ):
        assert desktop.main([]) == 0

    assert calls[:2] == ["start_runtime", "show"]
    assert calls[-1] == "close"
    assert calls[2:-1] == ["open_board"]


def test_failed_runtime_start_retains_cleanup_owner_when_workers_remain():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")
        with _home_patches(home)[0]:
            state.setup(workspace)
            created = []

            class RuntimeWithUnfinishedCleanup:
                def __init__(self, **_kwargs):
                    self.cleanup_allowed = False
                    created.append(self)

                def start(self, **_kwargs):
                    raise RuntimeError("injected startup failure")

                def cleanup_start_failure(self, _deadline):
                    return self.cleanup_allowed

                def shutdown(self, _deadline):
                    return True

            with patch.object(desktop, "ApplicationRuntime", RuntimeWithUnfinishedCleanup):
                session = desktop.DesktopSession()
                with pytest.raises(desktop.DesktopUnavailableError):
                    session.start_runtime(static_ready=lambda: True)

                assert created
                assert session.runtime is created[0]
                assert session.claims.held
                created[0].cleanup_allowed = True
                session.close()
                assert not session.claims.held


@pytest.mark.skipif(os.name == "nt", reason="source-only Qt session is validated on hosted native lanes")
def test_qt_is_optional_for_linux_source_collection():
    with patch.object(desktop, "_load_qt", side_effect=desktop.DesktopDependencyError("missing")):
        with pytest.raises(desktop.DesktopDependencyError):
            desktop._load_qt()


@pytest.mark.skipif(
    QtWidgets is None or not _NATIVE_REQUIRED,
    reason="required native session lane is not enabled",
)
def test_required_native_session_activates_existing_configured_shell_and_closes():
    if _NATIVE_REQUIRED and os.environ.get("QT_QPA_PLATFORM", "").lower() in {
        "offscreen",
        "minimal",
        "minimalegl",
    }:
        pytest.fail("required native desktop lane cannot use an offscreen Qt platform")
    expected_host = os.environ.get("NYX_EXPECTED_NATIVE_HOST")
    if expected_host == "windows-2025":
        assert sys.platform == "win32"
    elif expected_host == "macos-15":
        assert sys.platform == "darwin"
    else:
        pytest.fail("required native desktop lane did not identify its hosted runner")
    with _native_temp_home() as root:
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        with _home_patches(home)[0]:
            state.setup(first, ["Queue"])
            session = desktop.DesktopSession()
            application = QtWidgets.QApplication.instance()
            owns_application = application is None
            if application is None:
                application = QtWidgets.QApplication([])
            window = desktop._build_window(_native_qt(), session)
            claims_identity = id(session.claims)
            window.show()
            application.processEvents()
            window._change.click()
            window._root.setText(str(second))
            window._save.click()
            application.processEvents()
            assert state.load_configuration() == state.Configuration(
                second.resolve(), ("Queue",)
            )

            handle = window.windowHandle()
            assert handle is not None
            window.raise_()
            window.activateWindow()
            handle.requestActivate()
            for _ in range(20):
                application.processEvents()
                QtTest.QTest.qWait(25)
                if window.isActiveWindow():
                    break
            capabilities = {
                "active_window": window.isActiveWindow(),
                "application_state": int(application.applicationState().value),
                "host": expected_host,
                "image_os": os.environ.get("ImageOS"),
                "image_version": os.environ.get("ImageVersion"),
                "platform": application.platformName(),
                "pyside_version": QtCore.__version__,
                "qt_version": QtCore.qVersion(),
                "screens": len(application.screens()),
                "source_sha": os.environ.get("GITHUB_SHA"),
                "visible": window.isVisible(),
            }
            assert window.isVisible()
            assert window.isActiveWindow()
            assert (
                application.applicationState()
                == QtCore.Qt.ApplicationState.ApplicationActive
            )
            assert id(session.claims) == claims_identity
            assert session.claims.held
            window.close()
            application.processEvents()
            capabilities["close_delivered"] = not session.claims.held and not window.isVisible()
            print(f"NYX_NATIVE_DESKTOP_CAPABILITIES={json.dumps(capabilities, sort_keys=True)}")
            assert not session.claims.held
            assert not window.isVisible()
            assert state.load_configuration() == state.Configuration(
                second.resolve(), ("Queue",)
            )
            released = _destroy_native_window(
                application,
                window,
                store_path=session.paths.state_directory
                / desktop.PRESENTATION_DIRECTORY,
            )
            assert released
            if owns_application:
                application.quit()


def _free_loopback_port() -> int:
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except PermissionError:
        pytest.skip("sandbox does not permit loopback sockets")
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    except PermissionError:
        pytest.skip("sandbox does not permit loopback sockets")
    finally:
        probe.close()


def _request_catalog(port: int):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(
            "GET", "/api/catalog", headers={"Host": f"127.0.0.1:{port}"}
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def _inert_application_runtime() -> desktop.ApplicationRuntime:
    """A shared runtime with an idle listener for navigation-order checks."""

    class _IdleServer:
        def serve_forever(self):
            return None

        def shutdown(self):
            return None

        def close_active_connections(self):
            return None

        def server_close(self):
            return None

    class _IdleThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            return None

    return desktop.ApplicationRuntime(
        port=45123,
        deadline=time.monotonic() + 60,
        server_factory=lambda **_kwargs: _IdleServer(),
        thread_factory=_IdleThread,
    )


def _board_workspace(root: Path) -> Path:
    """A sample workspace with visible content, a hidden stage, and an edge."""

    workspace = _workspace(root, "board")
    packages = {
        ("Alpha", "Queue", "delivery"): (
            "# Alpha delivery\n"
            "Target repo: Alpha\n"
            "Package ID: 11111111-1111-4111-8111-111111111111\n"
            "Prerequisite: 22222222-2222-4222-8222-222222222222 | design-ready\n"
        ),
        ("Beta", "Under_Development", "design"): (
            "# Beta design\n"
            "Target repo: Beta\n"
            "Package ID: 22222222-2222-4222-8222-222222222222\n"
            "Claim: design-ready | satisfied | sha256:" + "f" * 64 + "\n"
        ),
        ("Gamma", "Done", "finished"): (
            "# Hidden completed work\n"
            "Target repo: Gamma\n"
            "Package ID: 33333333-3333-4333-8333-333333333333\n"
        ),
    }
    for (project, stage, package), body in packages.items():
        directory = workspace / project / stage / package
        directory.mkdir(parents=True)
        (directory / "spec.md").write_text(body, encoding="utf-8")
    return workspace


def test_application_runtime_board_url_requires_catalog_admission():
    application = _inert_application_runtime()
    with pytest.raises(RuntimeError):
        application.board_url()
    application.start(static_ready=lambda: True)
    with pytest.raises(RuntimeError):
        application.board_url()
    application.admit_catalog()
    assert application.board_url() == f"http://127.0.0.1:{application.port}/"
    assert application.shutdown(time.monotonic() + 5)
    with pytest.raises(RuntimeError):
        application.board_url()


def test_shell_navigation_waits_for_owned_runtime_admission():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")
        with _home_patches(home)[0]:
            state.setup(workspace)
            session = desktop.DesktopSession()
            application = _inert_application_runtime()
            application.start(static_ready=lambda: True)
            session._application_runtime = application
            try:
                window = desktop._build_window(_fake_qt(), session)
                assert window._board.url() is None
                assert not window._open_board()
                assert not application.catalog_admitted
                assert window._board.url() is None
                application.admit_catalog()
                assert window._open_board()
                assert window._board.url() == application.board_url()
            finally:
                session._application_runtime = None
                session.close()
            assert application.shutdown(time.monotonic() + 5)


def test_failed_runtime_start_leaves_the_board_unloaded():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")

        class RuntimeThatCannotStart:
            def __init__(self, **_kwargs):
                pass

            def start(self, **_kwargs):
                raise RuntimeError("injected startup failure")

            def cleanup_start_failure(self, _deadline):
                return True

            def shutdown(self, _deadline):
                return True

        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", RuntimeThatCannotStart
        ):
            state.setup(workspace)
            session = desktop.DesktopSession()
            with pytest.raises(desktop.DesktopUnavailableError):
                session.start_runtime(static_ready=lambda: True)
            assert session.runtime is None
            window = desktop._build_window(_fake_qt(), session)
            assert not window._open_board()
            assert window._board.url() is None
            session.close()


def test_presentation_release_is_idempotent_and_detaches_engine_objects():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")
        with _home_patches(home)[0]:
            state.setup(workspace)
            session = desktop.DesktopSession()
            window = desktop._build_window(_fake_qt(), session)
            released = (window._page, window._board, window._profile)
            assert all(item is not None for item in released)

            desktop._release_presentation(None, window)
            assert window._page is None
            assert window._board is None
            assert window._profile is None
            # Releasing twice, or after the objects are already gone, must not
            # raise and must not reach back into the destroyed Qt objects.
            desktop._release_presentation(None, window)
            session.close()
            assert not session.claims.held


def test_board_load_failure_without_an_opened_board_keeps_the_chooser():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")
        with _home_patches(home)[0]:
            state.setup(workspace)
            session = desktop.DesktopSession()
            assert session.runtime is None
            window = desktop._build_window(_fake_qt(), session)
            window.show()
            # No admitted work exists and no board navigation was issued, so a
            # stray failed load (for example an internal blank navigation) must
            # not be mistaken for an admitted-work failure and reap the chooser.
            window._board.loadFinished.emit(False)
            assert not window._board_failed
            assert session.claims.held
            assert window._visible
            assert not session.shutdown_blocked
            session.close()
            assert not session.claims.held


def test_post_admission_board_load_failure_reaps_shared_runtime():
    port = _free_loopback_port()
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")
        with (
            _home_patches(home)[0],
            patch.object(runtime, "PORT", port),
            patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),
        ):
            state.setup(workspace)
            session = desktop.DesktopSession()
            session.start_runtime()
            application = session.runtime
            assert application is not None and application.catalog_admitted
            window = desktop._build_window(_fake_qt(), session)
            window.show()
            try:
                assert window._open_board()
                assert window._board.url() == application.board_url()
                window._board.loadFinished.emit(False)
                assert not session.claims.held
                assert session.runtime is None
                assert not window._visible
                with pytest.raises((OSError, http.client.HTTPException)):
                    _request_catalog(port)
            finally:
                session.close()


def test_incomplete_post_admission_view_cleanup_keeps_a_visible_retry():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")

        class RuntimeThatCannotStop:
            catalog_admitted = True

            def __init__(self):
                self.allow_close = False

            def board_url(self):
                return "http://127.0.0.1:45124/"

            def shutdown(self, _deadline):
                return self.allow_close

            def cleanup_start_failure(self, _deadline):
                return self.allow_close

        with _home_patches(home)[0]:
            state.setup(workspace)
            session = desktop.DesktopSession()
            application = RuntimeThatCannotStop()
            session._application_runtime = application
            window = desktop._build_window(_fake_qt(), session)
            window.show()
            assert window._open_board()
            window._board.loadFinished.emit(False)
            assert session.shutdown_blocked
            assert session.claims.held
            assert window._visible
            assert window._retry.isEnabled()
            assert "retry" in window._status.text().lower()
            application.allow_close = True
            window._retry.click()
            assert not session.claims.held
            assert not window._visible
            session.close()


def test_board_diagnostic_names_url_load_and_renderer_observations():
    class _FakeUrl:
        def toString(self):
            return "chrome-error://chromewebdata/"

    class _FakePage:
        def __init__(self):
            self.loadStarted = _WebSignal()
            self.loadProgress = _WebSignal()
            self.loadFinished = _WebSignal()
            self.renderProcessTerminated = _WebSignal()

        def url(self):
            return _FakeUrl()

    class _TerminationStatus:
        value = 1

    page = _FakePage()
    probe = _NavigationProbe(page, requested_url="http://127.0.0.1:1234/")
    page.loadStarted.emit()
    page.loadProgress.emit(100)
    page.loadFinished.emit(False)
    page.renderProcessTerminated.emit(_TerminationStatus(), 139)

    message = _board_diagnostic("", probe)
    assert "board did not load" in message
    assert "http://127.0.0.1:1234/" in message
    assert "chrome-error://chromewebdata/" in message
    assert "load_finished" in message
    assert "render_process_terminated" in message
    assert "(1, 139)" in message


def test_desktop_first_catalog_request_uses_the_canonical_shared_runtime():
    assert desktop.ApplicationRuntime is app_runtime.ApplicationRuntime
    assert runtime.ApplicationRuntime is app_runtime.ApplicationRuntime
    port = _free_loopback_port()
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _board_workspace(root)
        with (
            _home_patches(home)[0],
            patch.object(runtime, "PORT", port),
            patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),
        ):
            state.setup(workspace, ["Done"])
            session = desktop.DesktopSession()
            try:
                application = session.start_runtime()
                assert isinstance(application, app_runtime.ApplicationRuntime)
                assert application is session.runtime
                assert application.catalog_admitted
                status, payload = _request_catalog(port)
                assert status == 200
                titles = [
                    entry["declared"]["title"] for entry in payload["entries"]
                ]
                assert "Alpha delivery" in titles
                assert "Beta design" in titles
                assert "Hidden completed work" not in titles
                assert payload["visibility"]["hidden_stages"] == ["Done"]
                assert payload["visibility"]["hidden_entry_count"] == 1
                assert all(entry["stage"] != "Done" for entry in payload["entries"])
                alpha = next(
                    entry
                    for entry in payload["entries"]
                    if entry["declared"]["title"] == "Alpha delivery"
                )
                edge = alpha["relationship"]["prerequisites"][0]
                assert edge["target_package_id"] == "22222222-2222-4222-8222-222222222222"
                assert edge["resolved_state"] == "satisfied"
            finally:
                session.close()


_BOARD_JSON_SNAPSHOT_SCRIPT = """
(() => {
  const result = {
    href: null,
    readyState: null,
    origin: null,
    hasStatus: false,
    error: null,
    status: '',
    titles: [],
    rows: [],
    links: [],
    compact: null,
    stored: null,
  };
  try {
    result.href = String(location.href);
    result.readyState = String(document.readyState);
    result.origin = String(location.origin);
    result.hasStatus = !!document.querySelector('#status');
    result.status = document.querySelector('#status') ? String(document.querySelector('#status').textContent) : '';
    result.titles = [...document.querySelectorAll('.card-title')].map((node) => String(node.textContent));
    result.rows = [...document.querySelectorAll('.board-row')].map((node) => String(node.dataset.lifecycle));
    result.links = [...document.querySelectorAll('.card-links')].map((node) => String(node.textContent));
    result.compact = document.querySelector('#compact-view') ? !!document.querySelector('#compact-view').checked : null;
    result.stored = window.localStorage.getItem('spec-tracker-compact-view');
  } catch (error) {
    result.error = error.name + ': ' + error.message;
  }
  return JSON.stringify(result);
})()
"""


def _native_qt() -> dict[str, object]:
    return {
        "QtCore": QtCore,
        "QtWidgets": QtWidgets,
        "QtWebEngineCore": QtWebEngineCore,
        "QtWebEngineWidgets": QtWebEngineWidgets,
    }


def _native_application():
    application = QtWidgets.QApplication.instance()
    if application is None:
        application = QtWidgets.QApplication([])
    return application


def _pump_native_events(application, milliseconds: int = 20) -> None:
    """Process Qt events and explicitly yield the GIL to Python threads."""

    application.processEvents()
    QtTest.QTest.qWait(milliseconds)
    time.sleep(0.01)


class _NavigationProbe:
    """Record QtWebEngine navigation and renderer signals for diagnosis.

    The board proofs must report what the engine actually did rather than only
    an empty snapshot.  ``renderProcessTerminated`` separates a crashed
    renderer from a server or navigation problem, and the page URL shows
    whether the main frame ever left the initial document.
    """

    def __init__(self, page, requested_url=None):
        self.page = page
        self.requested_url = requested_url
        self.load_started = 0
        self.load_progress = []
        self.load_finished = []
        self.renderer_terminations = []
        self.hooks = []
        for name, handler in (
            ("loadStarted", self._on_load_started),
            ("loadProgress", self._on_load_progress),
            ("loadFinished", self._on_load_finished),
            ("renderProcessTerminated", self._on_render_process_terminated),
        ):
            signal = getattr(page, name, None)
            connect = getattr(signal, "connect", None)
            if not callable(connect):
                continue
            try:
                connect(handler)
            except (TypeError, RuntimeError):
                continue
            self.hooks.append(name)

    def _on_load_started(self, *_):
        self.load_started += 1

    def _on_load_progress(self, value):
        try:
            self.load_progress.append(int(value))
        except (TypeError, ValueError):
            self.load_progress.append(value)

    def _on_load_finished(self, ok):
        self.load_finished.append(bool(ok))

    def _on_render_process_terminated(self, status, exit_code):
        try:
            self.renderer_terminations.append(
                (int(getattr(status, "value", status)), int(exit_code))
            )
        except (TypeError, ValueError):
            self.renderer_terminations.append((repr(status), repr(exit_code)))

    def current_url(self):
        try:
            return self.page.url().toString()
        except RuntimeError:
            return None

    def describe(self):
        return {
            "requested_url": self.requested_url,
            "page_url": self.current_url(),
            "load_started": self.load_started,
            "last_load_progress": self.load_progress[-1] if self.load_progress else None,
            "load_finished": list(self.load_finished),
            "render_process_terminated": list(self.renderer_terminations),
            "signal_hooks": list(self.hooks),
        }


def _eval_js(application, page, script, timeout: float = 30.0):
    outcome = {}
    done = threading.Event()

    def finish(value):
        outcome["value"] = value
        done.set()

    try:
        page.runJavaScript(script, finish)
    except RuntimeError as error:
        raise AssertionError(f"board JavaScript could not start: {error}") from error
    deadline = time.monotonic() + timeout
    while not done.is_set() and time.monotonic() < deadline:
        _pump_native_events(application)
    assert done.is_set(), "board JavaScript did not complete"
    return outcome["value"]


def _board_diagnostic(snapshot, probe=None, raw=None) -> str:
    parts = []
    if raw is not None:
        parts.append(f"raw={raw!r}")
    parts.append(f"snapshot={snapshot!r}")
    if probe is not None:
        parts.append(f"navigation={probe.describe()!r}")
    return "board did not load: " + " ".join(parts)


def _parse_board_observation(raw, probe=None):
    """Parse the string result of the board observation script.

    ``runJavaScript`` reaches Python through a binding that carries only
    boolean, numeric, and string results; a JavaScript object is converted to a
    value whose string form is empty.  The board therefore observes itself by
    returning ``JSON.stringify`` output.  A result that is not a non-empty JSON
    object string is a hard, explicitly reported contract failure rather than a
    silently empty snapshot.
    """

    if not isinstance(raw, str):
        raise AssertionError(
            "board observation was not delivered as a string result "
            f"(binding returned {type(raw).__name__}): "
            + _board_diagnostic(None, probe, raw)
        )
    if raw == "":
        raise AssertionError(
            "board observation was an empty string, so no JSON board state can "
            "be read through this binding: " + _board_diagnostic(None, probe, raw)
        )
    try:
        snapshot = json.loads(raw)
    except ValueError as error:
        raise AssertionError(
            f"board observation was not valid JSON ({error}): "
            + _board_diagnostic(None, probe, raw)
        ) from error
    if not isinstance(snapshot, dict):
        raise AssertionError(
            "board observation was not a JSON object: "
            + _board_diagnostic(snapshot, probe, raw)
        )
    return snapshot


def _wait_for_json_board(application, page, timeout: float = 60.0, probe=None):
    """Read the navigated board through a string result.

    ``_BOARD_JSON_SNAPSHOT_SCRIPT`` returns ``JSON.stringify`` output because
    QtWebEngine's ``runJavaScript`` result conversion delivers only boolean,
    numeric, and string values; an object-returning snapshot arrives as an
    empty string and cannot tell a rendered board apart from a blank document.
    A missing, empty, or malformed string result is a hard failure, and the
    board is accepted only once its status reports a loaded catalog.
    """

    deadline = time.monotonic() + timeout
    raw = None
    snapshot = None
    while time.monotonic() < deadline:
        try:
            raw = _eval_js(
                application, page, _BOARD_JSON_SNAPSHOT_SCRIPT, timeout=15.0
            )
        except AssertionError as error:
            raise AssertionError(_board_diagnostic(snapshot, probe, raw)) from error
        snapshot = _parse_board_observation(raw, probe)
        if str(snapshot.get("status", "")).startswith("Loaded"):
            return snapshot
        _pump_native_events(application, 50)
    raise AssertionError(_board_diagnostic(snapshot, probe, raw))


def _wait_for_status(application, page, prefix: str, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = _eval_js(
            application,
            page,
            "document.querySelector('#status').textContent",
            timeout=15.0,
        )
        if isinstance(value, str) and value.startswith(prefix):
            return value
        _pump_native_events(application, 50)
    raise AssertionError(f"board status never reached {prefix!r}: {value!r}")


def _wait_for_engine_sanity(application, page, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = _eval_js(
            application,
            page,
            "(() => { const node = document.querySelector('#engine-sanity');"
            " return node ? node.textContent : null; })()",
            timeout=10.0,
        )
        if isinstance(value, str) and value == "ready":
            return value
        _pump_native_events(application, 50)
    raise AssertionError(
        "self-contained engine sanity string control never returned 'ready': "
        f"{value!r}"
    )


def _presentation_store_is_releasable(path: Path) -> bool:
    """Report whether the persistent presentation store can be removed.

    On Windows a directory rename fails while any process still holds a file
    inside it, so a successful round-trip rename proves the profile released
    the store without destroying the persisted preference.
    """

    probe = path.with_name(f"{path.name}.release-probe")
    if not path.exists():
        return not probe.exists()
    if probe.exists():
        # A previous probe was interrupted; restore the store before retrying.
        try:
            os.rename(probe, path)
        except OSError:
            return False
    try:
        os.rename(path, probe)
    except OSError:
        return False
    try:
        os.rename(probe, path)
    except OSError:
        return False
    return True


def _destroy_native_window(application, window, *, store_path=None, timeout: float = 10.0):
    """Release the presentation profile and report whether its store freed."""

    desktop._release_presentation(application, window)
    try:
        window.deleteLater()
    except RuntimeError:
        pass
    desktop._flush_deferred_deletes(application)
    QtTest.QTest.qWait(50)
    if store_path is None:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _presentation_store_is_releasable(store_path):
            return True
        _pump_native_events(application, 50)
    return False


@contextlib.contextmanager
def _native_temp_home():
    """Temporary home whose cleanup failure never masks a native failure."""

    temporary = TemporaryDirectory()
    try:
        yield Path(temporary.name)
    finally:
        try:
            temporary.cleanup()
        except OSError as cleanup_error:
            if sys.exc_info()[0] is None:
                raise
            note = f"native temp home cleanup also failed: {cleanup_error!r}"
            try:
                sys.exception().add_note(note)
            except Exception:
                pass
            print(note, file=sys.stderr)


def _assert_native_host_has_no_offscreen_platform() -> None:
    if os.environ.get("QT_QPA_PLATFORM", "").lower() in {
        "offscreen",
        "minimal",
        "minimalegl",
    }:
        pytest.fail("required native desktop lane cannot use an offscreen Qt platform")


_ENGINE_SANITY_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'></head>"
    "<body><div id='engine-sanity'>ready</div></body></html>"
)


@pytest.mark.skipif(not _NATIVE_BOARD, reason=_NATIVE_BOARD_SKIP)
def test_required_native_engine_sanity_separates_renderer_from_loopback_serving():
    """Contrast a self-contained string probe with the navigated board.

    The self-contained document reports its marker from ``textContent``, a
    JavaScript string, so it is a valid control for the string JSON observation
    contract the board proofs use.  When the control returns its marker and
    Python serving returns the catalog but the board string observation still
    does not report a loaded catalog, the failure message reports both raw
    observations and the binding fact instead of blaming the renderer or its
    network access.
    """

    _assert_native_host_has_no_offscreen_platform()
    port = _free_loopback_port()
    with _native_temp_home() as root:
        home = _home(root)
        workspace = _board_workspace(root)
        application = _native_application()
        with (
            _home_patches(home)[0],
            patch.object(runtime, "PORT", port),
            patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),
        ):
            state.setup(workspace)
            session = desktop.DesktopSession()
            session.start_runtime()
            shared = session.runtime
            assert shared is not None and shared.catalog_admitted
            window = desktop._build_window(_native_qt(), session)
            window.show()
            released = None
            try:
                page = window._board.page()
                sanity_probe = _NavigationProbe(page, requested_url="self-contained")
                page.setHtml(_ENGINE_SANITY_HTML, QtCore.QUrl("http://127.0.0.1/"))
                sanity = _wait_for_engine_sanity(application, page)
                assert isinstance(sanity, str), (
                    "the self-contained engine sanity control did not return a "
                    f"string result: {sanity!r} {sanity_probe.describe()!r}"
                )
                assert sanity == "ready", (
                    "hosted QtWebEngine renderer could not evaluate a "
                    f"self-contained document: {sanity!r} "
                    f"{sanity_probe.describe()!r}"
                )
                status, payload = _request_catalog(port)
                assert status == 200
                assert any(
                    entry["declared"]["title"] == "Alpha delivery"
                    for entry in payload["entries"]
                )
                assert window._open_board()
                board_probe = _NavigationProbe(page, requested_url=shared.board_url())
                try:
                    snapshot = _wait_for_json_board(application, page, probe=board_probe)
                except AssertionError as error:
                    raise AssertionError(
                        f"the self-contained string control returned {sanity!r} "
                        "and Python serving returned catalog status "
                        f"{status}, but the board string observation did not "
                        "report a loaded catalog; this contrast is a raw "
                        "observation about the runJavaScript result binding, "
                        "which carries only boolean/numeric/string results, not "
                        "proof that the renderer or its network access is the "
                        f"limitation: {error}"
                    ) from error
                assert "Alpha delivery" in snapshot["titles"]
            finally:
                released = _destroy_native_window(
                    application,
                    window,
                    store_path=session.paths.state_directory
                    / desktop.PRESENTATION_DIRECTORY,
                )
                session.close()
            assert not session.claims.held
            assert released


def test_board_json_observation_parses_strings_and_rejects_missing_results():
    """Pin the string JSON observation contract without a native engine."""

    class _FakePage:
        def __init__(self, results):
            self._results = list(results)
            self.scripts = []

        def runJavaScript(self, script, callback=None, *_):
            self.scripts.append(script)
            callback(self._results.pop(0))

    payload = json.dumps(
        {
            "status": "Loaded 3 packages",
            "titles": ["Alpha delivery", "Beta design"],
            "rows": ["in-progress", "planned"],
            "links": ["needs: Beta design"],
            "compact": False,
            "stored": "false",
        }
    )
    page = _FakePage([payload])
    snapshot = _wait_for_json_board(object(), page, timeout=1.0)
    assert snapshot["titles"] == ["Alpha delivery", "Beta design"]
    assert snapshot["rows"] == ["in-progress", "planned"]
    assert snapshot["links"] == ["needs: Beta design"]
    assert snapshot["compact"] is False
    assert snapshot["stored"] == "false"
    assert "JSON.stringify" in page.scripts[0], (
        "the board observation must return a string result, not an object"
    )

    empty = _FakePage([""])
    with pytest.raises(AssertionError) as empty_error:
        _wait_for_json_board(object(), empty, timeout=1.0)
    assert "empty string" in str(empty_error.value)

    missing = _FakePage([None])
    with pytest.raises(AssertionError) as missing_error:
        _wait_for_json_board(object(), missing, timeout=1.0)
    assert "string result" in str(missing_error.value)

    malformed = _FakePage(["not json"])
    with pytest.raises(AssertionError) as malformed_error:
        _wait_for_json_board(object(), malformed, timeout=1.0)
    assert "valid JSON" in str(malformed_error.value)


@pytest.mark.skipif(not _NATIVE_BOARD, reason=_NATIVE_BOARD_SKIP)
def test_required_native_board_json_snapshot_separates_rendering_from_observation():
    """Prove the board renders using a string result the binding can deliver.

    The self-contained engine-sanity document already shows that a string
    result works on this engine instance, so an empty object snapshot cannot be
    attributed to the shared runtime.  Reading the navigated DOM through
    ``JSON.stringify`` fails only when the board document itself did not render
    its catalog content, which is the distinction the native proof needs.
    """

    _assert_native_host_has_no_offscreen_platform()
    port = _free_loopback_port()
    with _native_temp_home() as root:
        home = _home(root)
        workspace = _board_workspace(root)
        application = _native_application()
        with (
            _home_patches(home)[0],
            patch.object(runtime, "PORT", port),
            patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),
        ):
            state.setup(workspace, ["Done"])
            session = desktop.DesktopSession()
            session.start_runtime()
            shared = session.runtime
            assert shared is not None and shared.catalog_admitted
            window = desktop._build_window(_native_qt(), session)
            window.show()
            released = None
            try:
                page = window._board.page()
                probe = _NavigationProbe(page, requested_url=shared.board_url())
                assert window._open_board()
                snapshot = _wait_for_json_board(application, page, probe=probe)
                assert snapshot["origin"] == f"http://127.0.0.1:{port}"
                assert snapshot["hasStatus"] is True
                assert "Alpha delivery" in snapshot["titles"]
            finally:
                released = _destroy_native_window(
                    application,
                    window,
                    store_path=session.paths.state_directory
                    / desktop.PRESENTATION_DIRECTORY,
                )
                session.close()
            assert not session.claims.held
            assert released


@pytest.mark.skipif(not _NATIVE_BOARD, reason=_NATIVE_BOARD_SKIP)
def test_required_native_embedded_board_renders_content_hidden_stage_and_dependency():
    _assert_native_host_has_no_offscreen_platform()
    port = _free_loopback_port()
    with _native_temp_home() as root:
        home = _home(root)
        workspace = _board_workspace(root)
        application = _native_application()
        with (
            _home_patches(home)[0],
            patch.object(runtime, "PORT", port),
            patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),
        ):
            state.setup(workspace, ["Done"])
            session = desktop.DesktopSession()
            session.start_runtime()
            shared = session.runtime
            assert shared is not None and shared.catalog_admitted
            window = desktop._build_window(_native_qt(), session)
            window.show()
            released = None
            try:
                page = window._board.page()
                probe = _NavigationProbe(page, requested_url=shared.board_url())
                assert window._open_board()
                assert window._board.url().toString() == shared.board_url()
                status, payload = _request_catalog(port)
                assert status == 200
                assert "Alpha delivery" in [
                    entry["declared"]["title"] for entry in payload["entries"]
                ]
                snapshot = _wait_for_json_board(application, page, probe=probe)
                assert "Alpha delivery" in snapshot["titles"]
                assert "Beta design" in snapshot["titles"]
                assert "Hidden completed work" not in snapshot["titles"]
                assert "done" not in snapshot["rows"]
                assert any(
                    "needs:" in link and "Beta design" in link
                    for link in snapshot["links"]
                )
                shared.catalog_admitted = False
                _eval_js(application, page, "document.querySelector('#refresh').click()")
                assert _wait_for_status(application, page, "Refresh failed").startswith(
                    "Refresh failed"
                )
                shared.catalog_admitted = True
                _eval_js(application, page, "document.querySelector('#refresh').click()")
                assert _wait_for_status(application, page, "Loaded").startswith("Loaded")
            finally:
                released = _destroy_native_window(
                    application,
                    window,
                    store_path=session.paths.state_directory
                    / desktop.PRESENTATION_DIRECTORY,
                )
                session.close()
            assert not session.claims.held
            assert released


@pytest.mark.skipif(not _NATIVE_BOARD, reason=_NATIVE_BOARD_SKIP)
def test_required_native_compact_preference_survives_a_normal_reopen():
    _assert_native_host_has_no_offscreen_platform()
    port = _free_loopback_port()
    with _native_temp_home() as root:
        home = _home(root)
        workspace = _board_workspace(root)
        application = _native_application()
        with (
            _home_patches(home)[0],
            patch.object(runtime, "PORT", port),
            patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),
        ):
            state.setup(workspace)
            session = desktop.DesktopSession()
            session.start_runtime()
            shared = session.runtime
            assert shared is not None
            store_path = session.paths.state_directory / desktop.PRESENTATION_DIRECTORY
            window = desktop._build_window(_native_qt(), session)
            window.show()
            first_released = None
            try:
                page = window._board.page()
                probe = _NavigationProbe(page, requested_url=shared.board_url())
                assert window._open_board()
                _wait_for_json_board(application, page, probe=probe)
                assert not window._profile.isOffTheRecord()
                expected_storage = session.paths.state_directory / "presentation"
                assert Path(window._profile.persistentStoragePath()) == expected_storage
                stored = _eval_js(
                    application,
                    page,
                    "(() => { const control = document.querySelector('#compact-view');"
                    " control.checked = false; control.dispatchEvent(new Event('change'));"
                    " return window.localStorage.getItem('spec-tracker-compact-view'); })()",
                )
                assert stored == "false"
                QtTest.QTest.qWait(500)
            finally:
                first_released = _destroy_native_window(
                    application, window, store_path=store_path
                )
                session.close()
            assert not session.claims.held
            assert first_released

            reopened = desktop.DesktopSession()
            reopened.start_runtime()
            reopened_board = reopened.runtime
            assert reopened_board is not None
            window = desktop._build_window(_native_qt(), reopened)
            window.show()
            second_released = None
            try:
                page = window._board.page()
                probe = _NavigationProbe(page, requested_url=reopened_board.board_url())
                assert window._open_board()
                snapshot = _wait_for_json_board(application, page, probe=probe)
                assert snapshot["compact"] is False
                assert snapshot["stored"] == "false"
            finally:
                second_released = _destroy_native_window(
                    application, window, store_path=store_path
                )
                reopened.close()
            assert not reopened.claims.held
            assert second_released


@pytest.mark.skipif(not _NATIVE_BOARD, reason=_NATIVE_BOARD_SKIP)
def test_required_native_post_admission_view_failure_reaps_or_retains_retry():
    _assert_native_host_has_no_offscreen_platform()
    dead_port = _free_loopback_port()
    port = _free_loopback_port()
    with _native_temp_home() as root:
        home = _home(root)
        workspace = _board_workspace(root)
        application = _native_application()
        with (
            _home_patches(home)[0],
            patch.object(runtime, "PORT", port),
            patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),
        ):
            state.setup(workspace)
            session = desktop.DesktopSession()
            session.start_runtime()
            window = desktop._build_window(_native_qt(), session)
            window.show()
            released = None
            try:
                assert window._open_board()
                # Trigger the later view failure by sending the admitted board to
                # a dead loopback port; a first successful board load is not
                # required to exercise the post-admission cleanup obligation.
                window._board.setUrl(QtCore.QUrl(f"http://127.0.0.1:{dead_port}/"))
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and session.runtime is not None:
                    _pump_native_events(application, 50)
                if session.shutdown_blocked:
                    assert session.claims.held
                    assert window.isVisible()
                    assert window._retry.isEnabled()
                else:
                    assert not session.claims.held
                    assert session.runtime is None
                    assert not window.isVisible()
            finally:
                released = _destroy_native_window(
                    application,
                    window,
                    store_path=session.paths.state_directory
                    / desktop.PRESENTATION_DIRECTORY,
                )
                session.close()
            assert released


_ARTIFACT_ENTRY = os.environ.get("NYX_DESKTOP_ARTIFACT")


def _delivered_entry_paths() -> tuple[Path, Path] | None:
    """Resolve the staged application and helper when an artifact is supplied."""

    if not _ARTIFACT_ENTRY:
        return None
    artifact = Path(_ARTIFACT_ENTRY).resolve()
    if artifact.suffix == ".app" or artifact.is_dir():
        application_root = artifact / "Contents" / "MacOS"
        executable = application_root / _delivered_bundle_executable(artifact)
    else:
        executable = artifact
        application_root = artifact.parent
    worker_name = "NyxWorker.exe" if os.name == "nt" else "NyxWorker"
    return executable, application_root / "NyxWorker" / worker_name


def _delivered_bundle_executable(bundle: Path) -> str:
    """Read the macOS bundle executable so the delivered name stays authoritative."""

    try:
        import plistlib

        with (bundle / "Contents" / "Info.plist").open("rb") as handle:
            executable = plistlib.load(handle).get("CFBundleExecutable")
    except (OSError, ValueError):
        executable = None
    return executable or "NyxApp"


@pytest.fixture
def delivered_worker_helper() -> list[str]:
    """Entry fixture that points the established process scenarios at the artifact."""

    paths = _delivered_entry_paths()
    if paths is None:
        pytest.skip("the delivered desktop artifact is not staged on this host")
    _, helper = paths
    assert helper.is_file(), f"the bundled worker helper is missing: {helper}"
    return [str(helper)]


def test_delivered_worker_entry_reuses_manager_protocol_and_reaps(delivered_worker_helper):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")
        home_patch, uid_patch = _home_patches(home)
        with home_patch, uid_patch:
            state.setup(workspace)
            manager = desktop.CatalogWorkerManager(
                command_factory=lambda: list(delivered_worker_helper),
                timeout=30,
            )
            try:
                # The delivered helper resolves the account home in its own
                # process, so the patched home must reach its environment.
                with patch.dict(
                    os.environ, {"HOME": str(home), "USERPROFILE": str(home)}
                ):
                    catalog = json.loads(manager.fetch_catalog())
            finally:
                assert manager.close(time.monotonic() + 30)
        assert catalog["schema_version"] == 4
        assert manager.active_count == 0


def _build_desktop_driver():
    """Load the staging driver so its macOS linking stays covered off macOS."""

    import importlib.util

    path = Path(__file__).parents[1] / "scripts" / "build_desktop.py"
    spec = importlib.util.spec_from_file_location("nyx_build_desktop_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_macos_staging_exposes_frameworks_at_the_engine_relative_roots(tmp_path):
    driver = _build_desktop_driver()
    macos_root = tmp_path / "Nyx.app" / "Contents" / "MacOS"
    library_root = macos_root / "PySide6" / "Qt" / "lib"
    for name in ("QtCore", "QtWebEngineCore"):
        binary = library_root / f"{name}.framework" / "Versions" / "A" / name
        binary.parent.mkdir(parents=True)
        binary.write_text("", encoding="utf-8")

    driver._link_macos_qt_frameworks(macos_root)

    flat = macos_root / "PySide6" / "QtCore.framework"
    assert flat.is_symlink()
    assert (flat / "Versions" / "A" / "QtCore").is_file()
    nested = library_root / "QtWebEngineCore.framework" / "lib"
    assert nested.is_symlink()
    assert (
        nested / "QtWebEngineCore.framework" / "Versions" / "A" / "QtWebEngineCore"
    ).is_file()

    driver._link_macos_qt_frameworks(macos_root)
    assert flat.is_symlink()
    assert nested.is_symlink()


def test_build_driver_rejects_a_leftover_deployment_placeholder(tmp_path, monkeypatch):
    driver = _build_desktop_driver()
    spec_path = tmp_path / "pysidedeploy.spec"
    spec_path.write_text(
        "\n".join(driver._SPEC_TOKENS) + "\n@UNRESOLVED_PLACEHOLDER@\n",
        encoding="utf-8",
    )
    build_root = tmp_path / "build"
    build_root.mkdir()
    monkeypatch.setattr(driver, "SPEC_FILE", spec_path)
    monkeypatch.setattr(driver, "BUILD_ROOT", build_root)

    with pytest.raises(driver.BuildError):
        driver._render_spec({token: "resolved" for token in driver._SPEC_TOKENS})
