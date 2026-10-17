"""Behavioral checks for the claim-held desktop chooser slice."""

from __future__ import annotations

import contextlib
import functools
import http.client
import inspect
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import DEFAULT, patch

import pytest
from conftest import distinct_free_port
from launch_shadow import install_proof_environment, plant_shadow, shadow_imports

from nyx import _native_claim, app_runtime, desktop, runtime, state, worker
from nyx._native_claim import NativeClaim
from nyx.models import SCHEMA_VERSION, canonical_digest, parse_catalog

pytestmark = pytest.mark.usefixtures("isolated_port")

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

    def hide(self):
        self._visible = False

    def close(self):
        # Like QWidget.close, an ignored close event keeps the window visible.
        event = SimpleNamespace(accepted=True)
        event.accept = lambda: setattr(event, "accepted", True)
        event.ignore = lambda: setattr(event, "accepted", False)
        self.closeEvent(event)
        if event.accepted:
            self._visible = False
        return event.accepted


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
    def __init__(self, text="", *_):
        super().__init__()
        self._text = text
        self.clicked = _Signal()

    def setText(self, value):
        self._text = value

    def text(self):
        return self._text

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
        self._settings = SimpleNamespace(attributes=[])
        self._settings.setAttribute = lambda attribute, enabled: self._settings.attributes.append(
            (attribute, enabled)
        )

    def profile(self):
        return self._profile

    def settings(self):
        return self._settings

    def runJavaScript(self, script, callback=None, *_):
        if callback is not None:
            callback(None)


class _WebEngineView(_Widget):
    # Optional callable whose result is recorded with every navigation call,
    # so a test can see which runtime the session held at that moment.
    observe = None

    def __init__(self, *_):
        super().__init__()
        self.loadFinished = _WebSignal()
        self._url = None
        self._page = None
        self.navigations = []

    def _record(self, kind, url):
        observed = None if self.observe is None else self.observe()
        self.navigations.append((kind, url, observed))

    def setPage(self, page):
        self._page = page

    def page(self):
        return self._page

    def setUrl(self, url):
        self._url = url
        self._record("setUrl", url)

    def reload(self):
        self._record("reload", self._url)

    def url(self):
        return self._url


class _QueuedEvents:
    """The fake GUI thread's queue of cross-thread signal deliveries.

    A delivery posted while a pool submission runs waits until the pool has
    cleared ``inside_submission``, as a queued Qt delivery reaches the GUI
    thread only after the emitting call returns.  ``deferred`` holds every
    delivery until the test calls :meth:`flush`, as a native delivery waits
    for a later event-loop turn.
    """

    def __init__(self):
        self.inside_submission = False
        self.deferred = False
        self.pending = []

    def post(self, callbacks, args):
        self.pending.extend((callback, args) for callback in callbacks)
        self.deliver()

    def deliver(self):
        if not self.inside_submission and not self.deferred:
            self.flush()

    def flush(self):
        while self.pending:
            callback, args = self.pending.pop(0)
            callback(*args)


class _QueuedSignal:
    def __init__(self, events):
        self._events = events
        self.callbacks = []

    def connect(self, callback, *_connection_type):
        self.callbacks.append(callback)

    def disconnect(self, callback=None):
        if callback is None:
            self.callbacks.clear()
        else:
            self.callbacks.remove(callback)

    def emit(self, *args):
        # Like a queued Qt delivery, an emission already posted still reaches
        # the receivers connected when it was emitted.
        self._events.post(list(self.callbacks), args)


class _SignalDescriptor:
    """A class-level ``Signal(...)`` whose instances get their own signal."""

    def __init__(self, events, *_types):
        self._events = events
        self._name = None

    def __set_name__(self, _owner, name):
        self._name = name

    def __get__(self, instance, _owner=None):
        if instance is None:
            return self
        signal = instance.__dict__.get(self._name)
        if signal is None:
            signal = instance.__dict__[self._name] = _QueuedSignal(self._events)
        return signal


class _QObject:
    def __init__(self, *_):
        pass


class _ThreadPool:
    """A single-slot pool that runs each submission inline.

    ``hold`` queues submissions until :meth:`waitForDone` runs them, so a test
    can keep an operation in flight without a thread.  ``waitForDone`` never
    delivers queued signals: the GUI thread is the one waiting.
    """

    events = None

    def __init__(self, *_):
        self.max_thread_count = None
        self.submissions = 0
        self.hold = False
        self.held = []
        self.waits = 0

    def setMaxThreadCount(self, count):
        self.max_thread_count = count

    def _run(self, runnable):
        self.events.inside_submission = True
        try:
            runnable()
        finally:
            self.events.inside_submission = False

    def start(self, runnable):
        self.submissions += 1
        if self.hold:
            self.held.append(runnable)
            return
        self._run(runnable)
        self.events.deliver()

    def waitForDone(self, *_):
        self.waits += 1
        while self.held:
            self._run(self.held.pop(0))
        return True


def _fake_qt(observe=None):
    view = _WebEngineView
    if observe is not None:
        view = type("_ObservedWebEngineView", (_WebEngineView,), {"observe": staticmethod(observe)})
    events = _QueuedEvents()
    return {
        "QtCore": SimpleNamespace(
            QUrl=lambda value: value,
            QObject=_QObject,
            QThreadPool=type("_BoundThreadPool", (_ThreadPool,), {"events": events}),
            Qt=SimpleNamespace(ConnectionType=SimpleNamespace(QueuedConnection="queued")),
            Signal=lambda *types: _SignalDescriptor(events, *types),
        ),
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
            QWebEngineSettings=SimpleNamespace(WebAttribute=SimpleNamespace(JavascriptCanAccessClipboard=3)),
        ),
        "QtWebEngineWidgets": SimpleNamespace(QWebEngineView=view),
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

desktop.runtime.desktop_host = lambda: True

phase = os.environ["NYX_TEST_CRASH_PHASE"]
marker_root = Path(os.environ["NYX_TEST_MARKER_ROOT"])
desktop.runtime._use_port(int(os.environ["NYX_TEST_PORT"]))
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
    def hide(self): pass
    def show(self):
        (marker_root / "shell-visible").write_text(str(os.getpid()), encoding="utf-8")
class LineEdit(Widget):
    def __init__(self, *args): super().__init__(*args); self.value = ""
    def setText(self, value): self.value = value
    def text(self): return self.value
class Label(LineEdit):
    def setWordWrap(self, value): pass
class Button(Widget):
    def __init__(self, text="", *args): super().__init__(); self.value = text; self.clicked = Signal()
    def setText(self, value): self.value = value
    def text(self): return self.value
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
    def settings(self): return SimpleNamespace(setAttribute=lambda *args: None)
class WebView(Widget):
    def __init__(self, *args): super().__init__(*args); self.loadFinished = Signal()
    def setPage(self, value): pass
    def setUrl(self, value): pass
class QObject:
    def __init__(self, *args): pass
class BoundSignal:
    def __init__(self): self.callbacks = []
    def connect(self, callback, *args): self.callbacks.append(callback)
    def disconnect(self, *args): self.callbacks.clear()
    def emit(self, *args):
        for callback in list(self.callbacks): callback(*args)
class SignalDescriptor:
    def __init__(self, *types): self.name = None
    def __set_name__(self, owner, name): self.name = name
    def __get__(self, instance, owner=None):
        if instance is None: return self
        return instance.__dict__.setdefault(self.name, BoundSignal())
class ThreadPool:
    def __init__(self, *args): pass
    def setMaxThreadCount(self, count): pass
    def start(self, runnable): runnable()
    def waitForDone(self, *args): return True
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
web_core = SimpleNamespace(QWebEnginePage=WebPage, QWebEngineProfile=WebProfile,
                           QWebEngineSettings=SimpleNamespace(WebAttribute=SimpleNamespace(JavascriptCanAccessClipboard=3)))
core = SimpleNamespace(
    QUrl=lambda value: value, QObject=QObject, QThreadPool=ThreadPool,
    Qt=SimpleNamespace(ConnectionType=SimpleNamespace(QueuedConnection="queued")),
    Signal=SignalDescriptor,
)
desktop._load_qt = lambda: {
    "QtCore": core,
    "QtWidgets": widgets,
    "QtWebEngineCore": web_core,
    "QtWebEngineWidgets": web_widgets,
}
raise SystemExit(desktop.main([]))
"""

_DESKTOP_ENTRY_CODE = (
    "from nyx import desktop, runtime; "
    "runtime.desktop_host = lambda: True; "
    "raise SystemExit(desktop.main([]))"
)


def _wait_for_path(path: Path, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), f"timed out waiting for {path.name}"


@pytest.mark.skipif(
    runtime.desktop_host(),
    reason="platform check only refuses on unsupported platforms",
)
def test_unsupported_platform_subprocess_exits_without_creating_home_tree():
    """Linux natural trigger — real subprocess, real filesystem."""
    with TemporaryDirectory() as temporary:
        home = Path(temporary) / "home"
        home.mkdir()
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
        assert result.returncode == 2
        assert result.stdout == ""
        assert result.stderr == f"{desktop.UNSUPPORTED_PLATFORM_MESSAGE}\n"
        assert list(home.iterdir()) == []


def test_unsupported_platform_refuses_before_session_construction(capsys):
    """Every OS — raising double proves check precedes session."""
    sentinel = RuntimeError("DesktopSession must not be called")
    with (
        patch.object(desktop.runtime, "desktop_host", return_value=False),
        patch.object(desktop, "DesktopSession", side_effect=sentinel),
    ):
        assert desktop.main([]) == 2
    assert capsys.readouterr().err == f"{desktop.UNSUPPORTED_PLATFORM_MESSAGE}\n"


def test_module_import_does_not_require_qt(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "PySide6", None)
    assert desktop.APPLICATION_CLAIM_FILENAME == "lease.lock"


def test_first_launch_close_releases_both_claims_without_creating_configuration():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        with _home_patches(home)[0]:
            session = desktop.DesktopSession()
            paths = state.state_paths()
            assert session.snapshot.status == "not_configured"
            assert session.claims.held
            assert paths.config_file is not None and not paths.config_file.exists()
            session.close()
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

                def capture_configuration(self, configuration, paths):
                    observed["configuration"] = configuration
                    observed["paths"] = paths

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
                assert workers._working_directory == session.paths.state_directory
                assert observed["admitted"]
                session.close()


def test_first_launch_valid_selection_uses_canonical_schema_three_state():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")
        supplied = root / "workspace-link"
        supplied.symlink_to(workspace, target_is_directory=True)
        class FakeApplicationRuntime:
            def __init__(self, **_kwargs):
                pass

            def capture_configuration(self, _configuration, _paths):
                pass

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
            session = desktop.DesktopSession()
            result = session.choose_workspace(supplied)
            paths = state.state_paths()
            assert result.specification_root == workspace.resolve()
            assert state.load_configuration(paths) == result
            assert json.loads(paths.config_file.read_text(encoding="utf-8")) == {
                "schema_version": 3,
                "specification_root": str(workspace.resolve()),
            }
            session.close()


def test_first_launch_save_starts_admitted_runtime_and_opens_board():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")
        created = []

        class FakeApplicationRuntime:
            def __init__(self, **_kwargs):
                self.started = False
                self.catalog_admitted = False
                self.shutdown_calls = 0
                created.append(self)

            def capture_configuration(self, configuration, paths):
                self.configuration = configuration
                self.paths = paths

            def start(self, *, static_ready=None):
                assert static_ready is None or static_ready()
                self.started = True

            def admit_catalog(self):
                assert self.started
                self.catalog_admitted = True

            def board_url(self):
                if not self.catalog_admitted:
                    raise RuntimeError("catalog has not been admitted")
                return "http://127.0.0.1:45123/"

            def shutdown(self, _deadline):
                self.shutdown_calls += 1
                return True

            def cleanup_start_failure(self, _deadline):
                return True

        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", FakeApplicationRuntime
        ):
            session = desktop.DesktopSession()
            window = desktop._build_window(_fake_qt(), session)
            assert window._page.settings().attributes == [(3, True)]

            window._root.setText(str(workspace))
            window._save.click()

            assert state.load_configuration() == state.Configuration(workspace.resolve())
            assert len(created) == 1
            assert session.runtime is created[0]
            assert created[0].started
            assert created[0].catalog_admitted
            assert [kind for kind, _url, _runtime in window._board.navigations] == ["setUrl"]
            assert window._board.url() == "http://127.0.0.1:45123/"

            window._quit.click()
            assert created[0].shutdown_calls == 1
            assert not session.claims.held


def test_first_launch_save_runtime_failure_preserves_selection_and_retries_board():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")
        created = []

        class FakeApplicationRuntime:
            def __init__(self, **_kwargs):
                self.catalog_admitted = False
                self.cleanup_calls = 0
                created.append(self)

            def capture_configuration(self, configuration, paths):
                self.configuration = configuration
                self.paths = paths

            def start(self, *, static_ready=None):
                assert static_ready is None or static_ready()
                if len(created) == 1:
                    raise RuntimeError("injected startup failure")

            def admit_catalog(self):
                self.catalog_admitted = True

            def board_url(self):
                if not self.catalog_admitted:
                    raise RuntimeError("catalog has not been admitted")
                return "http://127.0.0.1:45123/"

            def cleanup_start_failure(self, _deadline):
                self.cleanup_calls += 1
                return True

            def shutdown(self, _deadline):
                return True

        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", FakeApplicationRuntime
        ):
            session = desktop.DesktopSession()
            window = desktop._build_window(_fake_qt(), session)

            window._root.setText(str(workspace))
            window._save.click()

            assert state.load_configuration() == state.Configuration(workspace.resolve())
            assert created[0].cleanup_calls == 1
            assert session.runtime is None
            assert window._board.url() is None
            assert window._retry.isEnabled()

            window._retry.click()

            assert len(created) == 2
            assert session.runtime is created[1]
            assert created[1].catalog_admitted
            assert [kind for kind, _url, _runtime in window._board.navigations] == ["setUrl"]
            assert window._board.url() == "http://127.0.0.1:45123/"
            window._quit.click()


def test_replacing_workspace_preserves_saved_order_and_completion():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        with _home_patches(home)[0]:
            state.setup(first)
            paths = state.state_paths()
            expected = {
                "schema_version": 2,
                "hidden_stages": [],
                "specification_root": str(first.resolve()),
                "stage_orders": {str(first.resolve()): ["Done", "Queue"],
                                 str(second.resolve()): ["Archive", "Queue"]},
                "completed_stages": {str(first.resolve()): ["Done"],
                                     str(second.resolve()): ["Archive"]},
            }
            paths.config_file.write_text(json.dumps(expected) + "\n", encoding="utf-8")
            session = desktop.DesktopSession()
            session.choose_workspace(second)
            assert state.load_configuration().specification_root == second.resolve()
            expected.pop("hidden_stages")
            expected["schema_version"] = 3
            expected["specification_root"] = str(second.resolve())
            assert json.loads(paths.config_file.read_bytes()) == expected
            assert state.load_configuration().stage_order == ("Archive", "Queue")
            assert state.load_configuration().completed_stage_names == ("Archive",)
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
                    [sys.executable, "-c", _DESKTOP_ENTRY_CODE],
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


_STARTUP_FAILURE_QT_CODE = r'''
from types import SimpleNamespace


class _WindowModality:
    NonModal = "NonModal"
    ApplicationModal = "ApplicationModal"


class _FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in self.callbacks:
            callback(*args)


def build_startup_failure_qt(record, mode, existing=None):
    """Return a recording Qt double with no blocking or static message box."""

    shown = []

    class MessageBox:
        class StandardButton:
            Ok = 1 << 10
            Cancel = 1 << 22

        def __init__(self, *args):
            if mode == "box_fails":
                raise RuntimeError("message box cannot be presented")
            self.title = None
            self.text = None
            self.buttons = 0
            # A parentless QMessageBox starts application-modal.
            self.modality = _WindowModality.ApplicationModal
            self.finished = _FakeSignal()
            self.done = False

        def setWindowTitle(self, value):
            self.title = value

        def setText(self, value):
            self.text = value

        def setStandardButtons(self, value):
            self.buttons = bin(int(value)).count("1")

        def setWindowModality(self, value):
            self.modality = value

        def show(self):
            record("box", self.title, self.text, self.buttons, self.modality)
            shown.append(self)

    class Application:
        current = existing

        @classmethod
        def instance(cls):
            return cls.current

        def __init__(self, *args):
            Application.current = self
            self.quit_requested = False
            record("application")

        def quit(self, *args):
            # Only a finished message box may end the loop; a quit requested
            # before then would leave the real box waiting with no running loop.
            if not any(box.done for box in shown):
                raise RuntimeError("the application quit before its message box finished")
            self.quit_requested = True

        def exec(self):
            record("loop")
            if mode == "platform_exit":
                # What AppKit does when it answers an application quit request
                # itself: the C library's exit(0), with the box still shown and
                # without the interpreter's own shutdown.
                import ctypes

                ctypes.CDLL(None).exit(0)
            for box in shown:
                box.done = True
                box.finished.emit(1)
            if not self.quit_requested:
                raise RuntimeError("the event loop never received a quit request")
            record("loop-finished")
            return 0

    def load_qt():
        record("load_qt")
        if mode == "unavailable":
            raise DesktopDependencyError("test Qt double is unavailable")
        return {
            "QtCore": SimpleNamespace(Qt=SimpleNamespace(WindowModality=_WindowModality)),
            "QtWidgets": SimpleNamespace(QApplication=Application, QMessageBox=MessageBox),
        }

    return load_qt
'''

_STARTUP_FAILURE_ENTRY_CODE = r'''
import ctypes  # noqa: F401 - loaded under the real platform identity
import os
import sys

from nyx import desktop, runtime

runtime.desktop_host = lambda: True
if os.environ.get("NYX_TEST_PLATFORM"):
    sys.platform = os.environ["NYX_TEST_PLATFORM"]
if os.environ["NYX_TEST_PACKAGED"] == "1":
    sys.frozen = True
elif hasattr(sys, "frozen"):
    del sys.frozen


def record(*fields):
    print("\t".join(str(field) for field in fields), flush=True)


namespace = {"DesktopDependencyError": desktop.DesktopDependencyError}
exec(os.environ["NYX_TEST_QT_CODE"], namespace)
desktop._load_qt = namespace["build_startup_failure_qt"](record, os.environ["NYX_TEST_QT_MODE"])
raise SystemExit(desktop.main([]))
'''


def _startup_failure_records(*fields):
    return "\t".join(str(field) for field in fields)


def _presented(message):
    return [
        "load_qt",
        "application",
        _startup_failure_records("box", "Nyx", message, 1, "NonModal"),
        "loop",
        "loop-finished",
    ]


@pytest.mark.parametrize(
    ("trigger", "packaged", "qt_mode", "expected_records", "expected_stderr"),
    [
        pytest.param(
            "conflict", True, "ok", _presented(desktop.ALREADY_OPEN_MESSAGE), "",
            id="packaged-already-open-shows-box",
        ),
        pytest.param(
            "session_failure", True, "ok", _presented(desktop.UNAVAILABLE_MESSAGE), "",
            id="packaged-session-failure-shows-box",
        ),
        pytest.param(
            "conflict", False, "ok", [], f"{desktop.ALREADY_OPEN_MESSAGE}\n",
            id="source-already-open-writes-stderr",
        ),
        pytest.param(
            "session_failure", False, "ok", [], f"nyx-desktop: {desktop.UNAVAILABLE_MESSAGE}\n",
            id="source-session-failure-writes-stderr",
        ),
        pytest.param(
            "conflict", True, "unavailable", ["load_qt"], f"{desktop.ALREADY_OPEN_MESSAGE}\n",
            id="packaged-box-unloadable-writes-stderr",
        ),
        pytest.param(
            "conflict", True, "box_fails", ["load_qt", "application"],
            f"{desktop.ALREADY_OPEN_MESSAGE}\n",
            id="packaged-box-unpresentable-writes-stderr",
        ),
        pytest.param(
            "free", True, "unavailable", ["load_qt"],
            "nyx-desktop: test Qt double is unavailable\n",
            id="packaged-post-session-qt-failure-stays-stderr",
        ),
    ],
)
def test_desktop_entry_presents_pre_window_failures_only_when_packaged(
    trigger, packaged, qt_mode, expected_records, expected_stderr
):
    result = _run_startup_failure_entry(trigger, packaged, qt_mode)

    assert result.returncode == 1
    assert result.stdout.splitlines() == expected_records
    assert result.stderr == expected_stderr


@pytest.mark.skipif(
    os.name == "nt",
    reason="the platform-level exit under test is the POSIX C library's exit()",
)
@pytest.mark.parametrize(
    ("platform", "qt_mode", "expected_records", "expected_status"),
    [
        pytest.param(
            "darwin", "platform_exit", _presented(desktop.ALREADY_OPEN_MESSAGE)[:4], 1,
            id="macos-platform-exit-with-box-up-keeps-failure-status",
        ),
        pytest.param(
            "darwin", "ok", _presented(desktop.ALREADY_OPEN_MESSAGE), 1,
            id="macos-dismissed-box-keeps-failure-status-and-output",
        ),
        pytest.param(
            "linux", "platform_exit", _presented(desktop.ALREADY_OPEN_MESSAGE)[:4], 0,
            id="non-macos-exit-status-is-not-rewritten",
        ),
    ],
)
def test_packaged_startup_failure_keeps_its_status_when_the_platform_exits(
    platform, qt_mode, expected_records, expected_status
):
    result = _run_startup_failure_entry("conflict", True, qt_mode, platform=platform)

    assert result.stdout.splitlines() == expected_records
    assert result.stderr == ""
    assert result.returncode == expected_status


def _run_startup_failure_entry(trigger, packaged, qt_mode, platform=None):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        if trigger == "session_failure":
            home.joinpath(".nyx").write_text("", encoding="utf-8")
        environment = os.environ.copy()
        environment["HOME"] = str(home)
        environment["USERPROFILE"] = str(home)
        environment.pop("PYTHONHOME", None)
        environment["NYX_TEST_PACKAGED"] = "1" if packaged else "0"
        environment["NYX_TEST_QT_MODE"] = qt_mode
        environment["NYX_TEST_QT_CODE"] = _STARTUP_FAILURE_QT_CODE
        if platform is not None:
            environment["NYX_TEST_PLATFORM"] = platform
        else:
            environment.pop("NYX_TEST_PLATFORM", None)

        def run_entry():
            return subprocess.run(
                [sys.executable, "-c", _STARTUP_FAILURE_ENTRY_CODE],
                cwd=Path(__file__).parents[1],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )

        with _home_patches(home)[0]:
            if trigger == "conflict":
                first = desktop.DesktopSession()
                try:
                    result = run_entry()
                    assert first.claims.held
                    assert not state.state_paths().config_file.exists()
                finally:
                    first.close()
            else:
                result = run_entry()
            if trigger == "free":
                paths = state.state_paths()
                lease_path = paths.runtime_directory / desktop.APPLICATION_CLAIM_FILENAME
                recovery_path = paths.runtime_directory / desktop.RECOVERY_CLAIM_FILENAME
                assert NativeClaim.probe(lease_path) == "free"
                assert NativeClaim.probe(recovery_path) == "free"
    return result


def _in_process_startup_failure_qt(mode="ok", existing=None):
    records = []
    namespace = {"DesktopDependencyError": desktop.DesktopDependencyError}
    exec(_STARTUP_FAILURE_QT_CODE, namespace)
    load_qt = namespace["build_startup_failure_qt"](
        lambda *fields: records.append(_startup_failure_records(*fields)), mode, existing
    )
    return records, load_qt


def test_shared_packaged_check_drives_presentation_and_worker_selection(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "home"
    home.mkdir()
    home.joinpath(".nyx").write_text("", encoding="utf-8")
    application_root = tmp_path / "application"
    application_root.mkdir()
    executable = application_root / ("Nyx.exe" if os.name == "nt" else "Nyx")
    executable.write_text("", encoding="utf-8")
    monkeypatch.setattr(desktop.runtime, "desktop_host", lambda: True)

    armed = []
    monkeypatch.setattr(
        desktop, "_keep_failure_status_on_platform_exit", lambda: armed.append(True)
    )

    # Packaged by the shared check alone: no interpreter frozen marker.
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(state, "_is_packaged_application", lambda: True)
    records, load_qt = _in_process_startup_failure_qt()
    monkeypatch.setattr(desktop, "_load_qt", load_qt)
    with _home_patches(home)[0]:
        assert desktop.main([]) == 1
    assert records == _presented(desktop.UNAVAILABLE_MESSAGE)
    assert armed == [True]
    assert capsys.readouterr().err == ""
    with monkeypatch.context() as scoped:
        scoped.setattr(sys, "executable", str(executable))
        with pytest.raises(worker.WorkerError) as error:
            worker.default_worker_command()
    assert error.value.code == "producer_unavailable"

    # Not packaged by the shared check, although the interpreter is frozen.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(state, "_is_packaged_application", lambda: False)
    records, load_qt = _in_process_startup_failure_qt()
    monkeypatch.setattr(desktop, "_load_qt", load_qt)
    with _home_patches(home)[0]:
        assert desktop.main([]) == 1
    assert records == []
    assert armed == [True]
    assert capsys.readouterr().err == f"nyx-desktop: {desktop.UNAVAILABLE_MESSAGE}\n"
    assert worker.default_worker_command() == [sys.executable, "-P", "-m", "nyx.worker"]


def test_packaged_startup_failure_reuses_a_running_application_without_its_loop(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "home"
    home.mkdir()
    home.joinpath(".nyx").write_text("", encoding="utf-8")
    loop_runs = []
    existing = SimpleNamespace(
        quit=lambda *args: None,
        exec=lambda: loop_runs.append("exec"),
    )
    records, load_qt = _in_process_startup_failure_qt(existing=existing)
    armed = []
    monkeypatch.setattr(
        desktop, "_keep_failure_status_on_platform_exit", lambda: armed.append(True)
    )
    monkeypatch.setattr(desktop.runtime, "desktop_host", lambda: True)
    monkeypatch.setattr(state, "_is_packaged_application", lambda: True)
    monkeypatch.setattr(desktop, "_load_qt", load_qt)
    with _home_patches(home)[0]:
        assert desktop.main([]) == 1

    assert records == [
        "load_qt",
        _startup_failure_records("box", "Nyx", desktop.UNAVAILABLE_MESSAGE, 1, "NonModal"),
    ]
    assert loop_runs == []
    # The status of a process whose loop belongs to someone else is not touched.
    assert armed == []
    assert capsys.readouterr().err == ""


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
    port = runtime.PORT

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
                    [sys.executable, "-c", _DESKTOP_ENTRY_CODE],
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
                payload = json.loads(response.read())
                assert payload["schema_version"] == SCHEMA_VERSION
                assert payload["configuration_revision"] == state.load_configuration(paths).revision
                connection.close()
                legacy = dict(payload, schema_version=4)
                legacy["catalog_digest"] = canonical_digest(legacy)
                with patch.object(
                    application.workers,
                    "fetch_catalog",
                    return_value=json.dumps(legacy).encode("utf-8"),
                ):
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=5
                    )
                    connection.request(
                        "GET",
                        "/api/catalog",
                        headers={"Host": f"127.0.0.1:{port}"},
                    )
                    rejected = connection.getresponse()
                    assert rejected.status == 502
                    assert json.loads(rejected.read()) == {
                        "error": "producer_protocol_error"
                    }
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
                    session.choose_workspace(second)
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
            state.setup(first)
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
                second.resolve()
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
            state.setup(first)
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
            state.setup(first)
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
                assert "hidden_stages" not in json.loads(committed)
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

            def capture_configuration(self, configuration, paths):
                self.configuration = configuration
                self.paths = paths

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
            state.setup(first)
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
                second.resolve()
            )
            assert session.runtime is instances[1]
            assert session.claims.held
            session.close()


def test_active_workspace_switch_preserves_each_root_stage_order():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        instances = []

        class FakeApplicationRuntime:
            def __init__(self, **_kwargs):
                self.configuration = None
                self.paths = None
                instances.append(self)

            def capture_configuration(self, configuration, paths):
                self.configuration = configuration
                self.paths = paths

            def start(self, *, static_ready=None):
                assert static_ready is None or static_ready()

            def admit_catalog(self):
                pass

            def save_settings(self, revision, order):
                assert self.configuration is not None
                assert revision == self.configuration.revision
                self.configuration = state.save_configuration_owned(
                    self.configuration.specification_root,
                    paths=self.paths,
                    stage_order=order,
                )
                return {
                    "order": list(self.configuration.stage_order),
                    "revision": self.configuration.revision,
                    "outcome": "success",
                }

            def shutdown(self, _deadline):
                return True

            def cleanup_start_failure(self, _deadline):
                return True

        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", FakeApplicationRuntime
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            first_runtime = session.start_runtime(static_ready=lambda: True)
            first_saved = first_runtime.save_settings(
                first_runtime.configuration.revision, ["Done", "Queue"]
            )
            assert first_saved["order"] == ["Done", "Queue"]

            session.choose_workspace(second)
            second_runtime = session.runtime
            assert second_runtime is instances[1]
            assert second_runtime.configuration.specification_root == second.resolve()
            assert second_runtime.configuration.stage_order == ()
            second_saved = second_runtime.save_settings(
                second_runtime.configuration.revision, ["Archive"]
            )
            assert second_saved["order"] == ["Archive"]

            session.choose_workspace(first)
            third_runtime = session.runtime
            assert third_runtime is instances[2]
            assert third_runtime.configuration.specification_root == first.resolve()
            assert third_runtime.configuration.stage_order == ("Done", "Queue")
            persisted = state.load_configuration()
            assert persisted.stage_order == ("Done", "Queue")
            assert persisted.stage_orders == {
                str(first.resolve()): ("Done", "Queue"),
                str(second.resolve()): ("Archive",),
            }
            session.close()


def test_workspace_switch_drains_admitted_write_before_starting_new_root():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        instances = []
        write_entered = threading.Event()
        release_write = threading.Event()
        shutdown_entered = threading.Event()
        started_roots = []
        switch_result = []
        switch_errors = []
        write_result = []
        write_errors = []
        saved_orders = []

        class FakeApplicationRuntime:
            def __init__(self, **_kwargs):
                self.configuration = None
                self.paths = None
                self.accepting = False
                self.active = 0
                self.condition = threading.Condition()
                instances.append(self)

            def capture_configuration(self, configuration, paths):
                self.configuration = configuration
                self.paths = paths

            def start(self, *, static_ready=None):
                assert static_ready is None or static_ready()
                started_roots.append(self.configuration.specification_root)

            def admit_catalog(self):
                with self.condition:
                    self.accepting = True

            def get_settings(self):
                with self.condition:
                    if not self.accepting:
                        raise RuntimeError("settings_unavailable")
                    return self.configuration.stage_order

            def save_settings(self, revision, order):
                with self.condition:
                    if not self.accepting:
                        raise RuntimeError("settings_unavailable")
                    assert revision == self.configuration.revision
                    self.active += 1
                    write_entered.set()
                try:
                    assert release_write.wait(timeout=3)
                    self.configuration = state.save_configuration_owned(
                        self.configuration.specification_root,
                        paths=self.paths,
                        stage_order=order,
                    )
                    saved_orders.append(self.configuration.stage_order)
                    return {"outcome": "success"}
                finally:
                    with self.condition:
                        self.active -= 1
                        self.condition.notify_all()

            def shutdown(self, deadline):
                with self.condition:
                    self.accepting = False
                    shutdown_entered.set()
                    while self.active:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return False
                        self.condition.wait(timeout=remaining)
                return True

            def cleanup_start_failure(self, _deadline):
                return True

        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", FakeApplicationRuntime
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            first_runtime = session.start_runtime(static_ready=lambda: True)

            def write_settings():
                try:
                    write_result.append(
                        first_runtime.save_settings(
                            first_runtime.configuration.revision, ["Done"]
                        )
                    )
                except BaseException as error:  # noqa: BLE001 - surfaced below
                    write_errors.append(error)

            writer = threading.Thread(target=write_settings)
            writer.start()
            assert write_entered.wait(timeout=2)

            def switch_workspace():
                try:
                    switch_result.append(session.choose_workspace(second))
                except BaseException as error:  # noqa: BLE001 - surfaced below
                    switch_errors.append(error)

            switcher = threading.Thread(target=switch_workspace)
            switcher.start()
            assert shutdown_entered.wait(timeout=2)
            assert switcher.is_alive()
            assert state.load_configuration().specification_root == first.resolve()
            with pytest.raises(RuntimeError, match="settings_unavailable"):
                first_runtime.get_settings()

            release_write.set()
            writer.join(timeout=3)
            switcher.join(timeout=3)
            assert not writer.is_alive()
            assert not switcher.is_alive()
            assert not write_errors
            assert not switch_errors
            assert len(write_result) == 1
            assert write_result[0]["outcome"] == "success"
            assert len(switch_result) == 1
            assert switch_result[0].specification_root == second.resolve()
            assert saved_orders == [("Done",)]
            assert started_roots == [first.resolve(), second.resolve()]
            assert state.load_configuration().specification_root == second.resolve()
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

            def capture_configuration(self, configuration, paths):
                self.configuration = configuration
                self.paths = paths

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

            def capture_configuration(self, configuration, paths):
                self.configuration = configuration
                self.paths = paths

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
            assert session.configuration == state.Configuration(second.resolve())
            assert session.runtime is instances[1]
            session.close()


_SAME_BOARD_URL = "http://127.0.0.1:45125/"


def _replaceable_runtime_type(instances, *, start_failures=None, control=None):
    """A runtime double whose every instance reports the same board URL.

    ``start_failures`` maps a resolved workspace root to the errors its next
    starts raise, in order; ``control["shutdown"]`` decides whether shutdown
    completes.  A single URL for every instance means only runtime identity,
    never a URL difference, can tell the window that the board is stale.
    """

    failures = {} if start_failures is None else start_failures
    settings = {"shutdown": True} if control is None else control

    class ReplaceableRuntime:
        def __init__(self, **_kwargs):
            self.configuration = None
            self.catalog_admitted = False
            instances.append(self)

        def capture_configuration(self, configuration, _paths):
            self.configuration = configuration

        def start(self, *, static_ready=None):
            assert static_ready is None or static_ready()
            pending = failures.get(self.configuration.specification_root)
            if pending:
                raise pending.pop(0)

        def admit_catalog(self):
            self.catalog_admitted = True

        def board_url(self):
            if not self.catalog_admitted:
                raise RuntimeError("catalog has not been admitted")
            return _SAME_BOARD_URL

        def shutdown(self, _deadline):
            return settings["shutdown"]

        def cleanup_start_failure(self, _deadline):
            return True

    return ReplaceableRuntime


def _observed_window(session):
    """Build the window over a view that records ``session.runtime`` per navigation."""

    return desktop._build_window(_fake_qt(lambda: session.runtime), session)


def _fail_verification_once(paths, replacement):
    original_verify = state._verify_record
    failed = False

    def fail_once(path):
        nonlocal failed
        details = original_verify(path)
        if (
            path == paths.config_file
            and json.loads(path.read_text(encoding="utf-8"))["specification_root"]
            == str(replacement.resolve())
            and not failed
        ):
            failed = True
            raise OSError("injected post-replacement verification failure")
        return details

    return patch.object(state, "_verify_record", side_effect=fail_once)


def test_change_workspace_save_navigates_once_to_the_new_runtime():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        instances = []
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", _replaceable_runtime_type(instances)
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            session.start_runtime()
            window = _observed_window(session)
            window.show()
            assert window._board.navigations == []
            assert window._open_board()
            first_runtime = session.runtime
            assert window._board.navigations == [("setUrl", _SAME_BOARD_URL, first_runtime)]

            window._change.click()
            window._root.setText(str(root / "missing"))
            window._save.click()
            assert session.pending_error == "Nyx workspace could not be validated"
            window._cancel.click()
            assert session.runtime is first_runtime
            assert len(window._board.navigations) == 1

            window._change.click()
            window._root.setText(str(second))
            window._save.click()

            assert state.load_configuration() == state.Configuration(second.resolve())
            assert len(instances) == 2
            assert session.runtime is instances[1]
            assert session.runtime is not first_runtime
            assert len(window._board.navigations) == 2
            kind, url, observed = window._board.navigations[1]
            assert kind in {"setUrl", "reload"}
            assert url == _SAME_BOARD_URL
            assert observed is instances[1]
            assert window._visible
            assert session.claims.held
            assert window._status.text() == f"Workspace: {second.resolve()}"

            window._quit.click()
            assert not session.claims.held


def test_change_workspace_retry_after_failed_start_navigates_once_to_the_restarted_runtime():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        instances = []
        failures = {
            second.resolve(): [
                RuntimeError("injected startup failure"),
                RuntimeError("injected repeated startup failure"),
            ]
        }
        with _home_patches(home)[0], patch.object(
            desktop,
            "ApplicationRuntime",
            _replaceable_runtime_type(instances, start_failures=failures),
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            session.start_runtime()
            window = _observed_window(session)
            window.show()
            assert window._open_board()
            first_runtime = session.runtime

            window._change.click()
            window._root.setText(str(second))
            window._save.click()

            assert state.load_configuration() == state.Configuration(second.resolve())
            assert session.snapshot.status == "runtime_blocked"
            assert session.runtime is None
            assert len(window._board.navigations) == 1
            assert window._retry.text() == "Retry"
            assert window._retry.isEnabled()

            with patch.object(
                session, "retry_runtime", wraps=session.retry_runtime
            ) as retry_runtime, patch.object(
                session, "retry_workspace_switch", wraps=session.retry_workspace_switch
            ) as retry_workspace_switch:
                window._retry.click()
                assert retry_runtime.call_count == 1
                assert session.snapshot.status == "runtime_blocked"
                assert session.runtime is None
                assert len(window._board.navigations) == 1

                window._retry.click()
                assert retry_runtime.call_count == 2
                assert retry_workspace_switch.call_count == 0

            assert len(instances) == 4
            assert session.runtime is instances[3]
            assert session.runtime is not first_runtime
            assert len(window._board.navigations) == 2
            kind, url, observed = window._board.navigations[1]
            assert kind in {"setUrl", "reload"}
            assert url == _SAME_BOARD_URL
            assert observed is instances[3]
            assert window._visible
            assert session.claims.held
            window._quit.click()
            assert not session.claims.held


def test_change_workspace_unverified_commit_retry_navigates_once_to_the_restarted_runtime():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        instances = []
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", _replaceable_runtime_type(instances)
        ):
            state.setup(first)
            paths = state.state_paths()
            session = desktop.DesktopSession()
            session.start_runtime()
            window = _observed_window(session)
            window.show()
            assert window._open_board()
            first_runtime = session.runtime

            window._change.click()
            window._root.setText(str(second))
            with _fail_verification_once(paths, second):
                window._save.click()
            assert session.unverified
            assert session.runtime is None
            assert len(window._board.navigations) == 1
            assert window._retry.text() == "Retry"

            with patch.object(
                session, "retry_workspace_switch", wraps=session.retry_workspace_switch
            ) as retry_workspace_switch, patch.object(
                session, "retry_runtime", wraps=session.retry_runtime
            ) as retry_runtime:
                window._retry.click()
                assert retry_workspace_switch.call_count == 1
                assert retry_runtime.call_count == 0

            assert not session.unverified
            assert session.configuration == state.Configuration(second.resolve())
            assert session.runtime is instances[1]
            assert session.runtime is not first_runtime
            assert len(window._board.navigations) == 2
            kind, url, observed = window._board.navigations[1]
            assert kind in {"setUrl", "reload"}
            assert url == _SAME_BOARD_URL
            assert observed is instances[1]
            window._quit.click()
            assert not session.claims.held


@pytest.mark.parametrize("failed_save", [True, False], ids=["after_failed_save", "without_save"])
def test_cancel_after_change_keeps_the_current_workspace_and_clears_the_chooser_error(
    failed_save,
):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        instances = []
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", _replaceable_runtime_type(instances)
        ):
            state.setup(first)
            paths = state.state_paths()
            before = paths.config_file.read_bytes()
            session = desktop.DesktopSession()
            session.start_runtime()
            window = _observed_window(session)
            window.show()
            assert window._open_board()
            current_runtime = session.runtime
            assert not window._cancel.isEnabled()

            window._change.click()
            assert window._cancel.isEnabled()
            if failed_save:
                window._root.setText(str(root / "missing"))
                window._save.click()
                assert session.snapshot.status == "configured"
                assert session.pending_error == "Nyx workspace could not be validated"
                assert window._status.text() == "Nyx workspace could not be validated"
                assert window._cancel.isEnabled()
            else:
                window._root.setText(str(root / "not-saved"))

            with patch.object(session, "cancel", wraps=session.cancel) as cancel:
                window._cancel.click()
                assert cancel.call_count == 1

            assert window._visible
            assert session.claims.held
            assert session.runtime is current_runtime
            assert len(instances) == 1
            assert paths.config_file.read_bytes() == before
            assert window._root.text() == str(first.resolve())
            assert window._status.text() == f"Workspace: {first.resolve()}"
            assert session.pending_error is None
            assert window._change.isEnabled()
            assert not window._save.isEnabled()
            assert not window._cancel.isEnabled()
            assert not window._root.isEnabled()
            assert len(window._board.navigations) == 1

            window._quit.click()
            assert not session.claims.held


def test_first_run_cancel_is_disabled_and_quit_releases_claims_without_configuration():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        with _home_patches(home)[0]:
            session = desktop.DesktopSession()
            paths = state.state_paths()
            window = desktop._build_window(_fake_qt(), session)
            window.show()
            assert session.snapshot.status == "not_configured"
            assert window._save.isEnabled()
            assert not window._cancel.isEnabled()

            session.cancel()
            assert session.claims.held
            assert session.snapshot.status == "not_configured"
            assert not paths.config_file.exists()

            window._quit.click()
            assert not window._visible
            assert not session.claims.held
            assert not paths.config_file.exists()
            assert NativeClaim.probe(paths.runtime_directory / "lease.lock") == "free"
            assert NativeClaim.probe(paths.runtime_directory / "recovery.lock") == "free"


_RECOVERY_STATES = {
    # state: (label, enabled, session method a click runs, or None)
    "board_failed_shutdown_blocked": ("Close", True, "close"),
    "switch_blocked": ("Retry", True, "retry_workspace_switch"),
    "unverified_commit": ("Retry", True, "retry_workspace_switch"),
    "runtime_blocked": ("Retry", True, "retry_runtime"),
    "runtime_port_unavailable": ("Retry", True, "retry_runtime"),
    "recovery_blocked": ("Retry", True, "retry_runtime"),
    "shutdown_blocked": ("Close", True, "close"),
    "configured": ("Retry", False, None),
    "not_configured": ("Retry", False, None),
    "unavailable": ("Retry", False, None),
}
_SPIED_SESSION_METHODS = ("retry_workspace_switch", "retry_runtime", "revalidate", "close")


@pytest.mark.parametrize("state_name", list(_RECOVERY_STATES))
def test_recovery_button_label_enablement_and_action_agree_with_cancel_disabled(state_name):
    assert desktop.BOARD_FAILURE_MESSAGE == "Nyx board could not be displayed; choose Close"
    label, enabled, method = _RECOVERY_STATES[state_name]
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        instances = []
        failures = {}
        control = {"shutdown": True}
        former_worker = None
        runtime_type = _replaceable_runtime_type(
            instances, start_failures=failures, control=control
        )
        with _home_patches(home)[0], patch.object(desktop, "ApplicationRuntime", runtime_type):
            paths = state.state_paths(create=True)
            if state_name == "unavailable":
                paths.config_file.write_text("not json\n", encoding="utf-8")
                os.chmod(paths.config_file, 0o600)
            elif state_name != "not_configured":
                state.setup(first)
            if state_name == "recovery_blocked":
                former_worker = NativeClaim(paths.runtime_directory / "recovery.lock")
                assert former_worker.acquire(blocking=False)
            if state_name == "runtime_blocked":
                failures[first.resolve()] = [RuntimeError("injected startup failure")]
            if state_name == "runtime_port_unavailable":
                failures[first.resolve()] = [
                    app_runtime.ApplicationPortUnavailableError("injected bind failure")
                ]
            session = desktop.DesktopSession()
            if session.snapshot.status == "configured":
                with contextlib.suppress(desktop.DesktopUnavailableError):
                    session.start_runtime()
            window = desktop._build_window(_fake_qt(), session)
            window.show()
            window._open_board()

            if state_name == "board_failed_shutdown_blocked":
                control["shutdown"] = False
                admitted = session.runtime
                window._board.loadFinished.emit(False)
                assert window._board_failed
                # No cleanup ran: a refused shutdown would have blocked it.
                assert not session.shutdown_blocked
                assert session.snapshot.status == "configured"
                assert session.runtime is admitted is not None
                assert window._status.text() == desktop.BOARD_FAILURE_MESSAGE
            elif state_name == "switch_blocked":
                control["shutdown"] = False
                window._change.click()
                window._root.setText(str(second))
                window._save.click()
                assert session.snapshot.status == "switch_blocked"
            elif state_name == "unverified_commit":
                window._change.click()
                window._root.setText(str(second))
                with _fail_verification_once(paths, second):
                    window._save.click()
                assert session.unverified
                assert session.snapshot.status == "unavailable"
            elif state_name == "runtime_blocked":
                assert session.snapshot.status == "runtime_blocked"
                assert window._status.text() == "Nyx runtime could not start; retry"
            elif state_name == "runtime_port_unavailable":
                assert session.snapshot.status == "runtime_blocked"
                assert window._status.text() == runtime.PORT_UNAVAILABLE_MESSAGE
            elif state_name == "recovery_blocked":
                assert session.snapshot.status == "recovery_blocked"
            elif state_name == "shutdown_blocked":
                control["shutdown"] = False
                window._quit.click()
                assert session.snapshot.status == "shutdown_blocked"
                assert window._visible
            elif state_name == "configured":
                assert session.snapshot.status == "configured"
                assert session.runtime is not None
            elif state_name == "not_configured":
                assert session.snapshot.status == "not_configured"
            else:
                assert session.snapshot.status == "unavailable"
                assert not session.switch_retryable

            assert window._retry.text() == label
            assert window._retry.isEnabled() is enabled
            assert not window._cancel.isEnabled()
            assert window._visible
            assert session.claims.held or state_name == "recovery_blocked"

            with contextlib.ExitStack() as stack:
                spies = {
                    name: stack.enter_context(
                        patch.object(session, name, wraps=getattr(session, name))
                    )
                    for name in _SPIED_SESSION_METHODS
                }
                window._retry.click()
                for name, spy in spies.items():
                    if name == method:
                        expected_calls = 1
                    elif name == "revalidate" and method == "retry_workspace_switch":
                        # Retrying a switch re-reads the configuration itself.
                        expected_calls = 1
                    else:
                        expected_calls = 0
                    assert spy.call_count == expected_calls, (name, spy.call_count)

            if method == "close":
                # Shutdown is still blocked, so the window stays and offers Close again.
                assert window._visible
                assert session.claims.held
                assert window._retry.text() == "Close"
                assert window._retry.isEnabled()
            assert window._retry.text() != "Revalidate"

            control["shutdown"] = True
            if former_worker is not None:
                former_worker.close()
            session.close()
            assert not session.claims.held


def test_board_failure_during_change_disables_cancel_and_offers_close():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        instances = []
        control = {"shutdown": True}
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", _replaceable_runtime_type(instances, control=control)
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            session.start_runtime()
            window = desktop._build_window(_fake_qt(), session)
            window.show()
            assert window._open_board()
            window._change.click()
            assert window._cancel.isEnabled()

            admitted = session.runtime
            window._board.loadFinished.emit(False)

            assert window._board_failed
            _assert_board_failure_screen(window, session, admitted)

            control["shutdown"] = False
            window._retry.click()
            _assert_blocked_close_retry(window, session)

            control["shutdown"] = True
            window._retry.click()
            assert not window._visible
            assert not session.claims.held
            assert session.runtime is None


def _assert_board_failure_screen(window, session, admitted):
    """The window shows the board-failure screen while admitted work still runs."""

    assert window._visible
    assert window._status.text() == desktop.BOARD_FAILURE_MESSAGE
    assert not window._root.isEnabled()
    assert not window._change.isEnabled()
    assert not window._save.isEnabled()
    assert not window._cancel.isEnabled()
    assert window._retry.text() == "Close"
    assert window._retry.isEnabled()
    assert session.claims.held
    assert session.runtime is admitted is not None
    assert session.snapshot.status != "shutdown_blocked"


def _assert_blocked_close_retry(window, session):
    """A Close whose cleanup is refused keeps a visible retry with its message."""

    assert session.shutdown_blocked
    assert session.snapshot.status == "shutdown_blocked"
    assert window._visible
    assert session.claims.held
    assert session.runtime is not None
    assert window._status.text() == "Nyx workers are still stopping; retry Close"
    assert window._retry.text() == "Close"
    assert window._retry.isEnabled()


def test_board_failure_after_a_rejected_save_shows_the_failure_not_the_rejection():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        instances = []
        control = {"shutdown": True}
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", _replaceable_runtime_type(instances, control=control)
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            session.start_runtime()
            admitted = session.runtime
            window = desktop._build_window(_fake_qt(), session)
            window.show()
            assert window._open_board()
            window._change.click()
            window._root.setText(str(root / "missing"))
            window._save.click()
            assert session.pending_error == "Nyx workspace could not be validated"
            assert window._status.text() == "Nyx workspace could not be validated"

            window._board.loadFinished.emit(False)
            _assert_board_failure_screen(window, session, admitted)
            # The rejection is still pending; it just never explains the failure.
            assert session.pending_error == "Nyx workspace could not be validated"

            control["shutdown"] = False
            window._retry.click()
            _assert_blocked_close_retry(window, session)

            control["shutdown"] = True
            window._retry.click()
            assert not window._visible
            assert not session.claims.held
            assert session.runtime is None


def test_board_failure_after_a_blocked_workspace_change_shows_the_failure():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        instances = []
        control = {"shutdown": True}
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", _replaceable_runtime_type(instances, control=control)
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            session.start_runtime()
            admitted = session.runtime
            window = desktop._build_window(_fake_qt(), session)
            window.show()
            assert window._open_board()

            control["shutdown"] = False
            window._change.click()
            window._root.setText(str(second))
            window._save.click()
            assert session.snapshot.status == "switch_blocked"
            assert session.shutdown_blocked
            assert window._status.text() == (
                "Nyx workers are still stopping; retry workspace change"
            )
            assert session.runtime is admitted is not None

            window._board.loadFinished.emit(False)
            assert window._status.text() == desktop.BOARD_FAILURE_MESSAGE
            assert window._retry.text() == "Close"
            assert window._retry.isEnabled()
            assert not window._root.isEnabled()
            assert not window._change.isEnabled()
            assert not window._save.isEnabled()
            assert not window._cancel.isEnabled()
            assert window._visible
            assert session.claims.held
            assert session.runtime is admitted

            window._retry.click()
            _assert_blocked_close_retry(window, session)

            control["shutdown"] = True
            window._retry.click()
            assert not window._visible
            assert not session.claims.held
            assert session.runtime is None


@pytest.mark.parametrize("blocked", ["switch_blocked", "runtime_blocked"])
def test_session_cancel_keeps_a_blocked_state_error_and_never_closes(blocked):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        instances = []
        control = {"shutdown": True}
        failures = {}
        with _home_patches(home)[0], patch.object(
            desktop,
            "ApplicationRuntime",
            _replaceable_runtime_type(instances, start_failures=failures, control=control),
        ):
            state.setup(first)
            paths = state.state_paths()
            session = desktop.DesktopSession()
            session.start_runtime()
            if blocked == "switch_blocked":
                control["shutdown"] = False
            else:
                failures[second.resolve()] = [RuntimeError("injected startup failure")]
            with contextlib.suppress(desktop.DesktopError):
                session.choose_workspace(second)
            assert session.snapshot.status == blocked
            error = session.pending_error
            assert error is not None
            current_runtime = session.runtime
            configuration_bytes = paths.config_file.read_bytes()

            session.cancel()

            assert session.pending_error == error
            assert session.snapshot.status == blocked
            assert session.claims.held
            assert session.runtime is current_runtime
            assert paths.config_file.read_bytes() == configuration_bytes

            control["shutdown"] = True
            session.close()
            assert not session.claims.held


def test_retryable_change_with_unreadable_configuration_offers_switch_retry():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        instances = []
        control = {"shutdown": False}
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", _replaceable_runtime_type(instances, control=control)
        ):
            state.setup(first)
            paths = state.state_paths()
            session = desktop.DesktopSession()
            session.start_runtime()
            window = desktop._build_window(_fake_qt(), session)
            window.show()
            assert window._open_board()
            window._change.click()
            window._root.setText(str(second))
            window._save.click()
            assert session.snapshot.status == "switch_blocked"

            # The configuration becomes unreadable before the switch is retried.
            paths.config_file.write_text("not json\n", encoding="utf-8")
            window._retry.click()

            assert session.snapshot.status == "unavailable"
            assert session.switch_retryable
            assert not session.unverified
            assert window._retry.text() == "Retry"
            assert window._retry.isEnabled()
            assert not window._cancel.isEnabled()

            with contextlib.ExitStack() as stack:
                spies = {
                    name: stack.enter_context(
                        patch.object(session, name, wraps=getattr(session, name))
                    )
                    for name in _SPIED_SESSION_METHODS
                }
                window._retry.click()
                assert spies["retry_workspace_switch"].call_count == 1
                assert spies["retry_runtime"].call_count == 0
                assert spies["close"].call_count == 0
                # Only the switch retry's own configuration re-read.
                assert spies["revalidate"].call_count == 1

            control["shutdown"] = True
            session.close()
            assert not session.claims.held


_LIFECYCLE_METHODS = ("close", "choose_workspace", "retry_workspace_switch", "retry_runtime")


def _window_controls(window):
    """The visible status, recovery label, and every control's enablement."""

    return {
        "status": window._status.text(),
        "retry_label": window._retry.text(),
        "root": window._root.isEnabled(),
        "change": window._change.isEnabled(),
        "save": window._save.isEnabled(),
        "cancel": window._cancel.isEnabled(),
        "retry": window._retry.isEnabled(),
        "quit": window._quit.isEnabled(),
        "visible": _window_visible(window),
    }


def _window_visible(window):
    visible = getattr(window, "_visible", None)
    return window.isVisible() if visible is None else visible


_CLOSING_PROGRESS = "Closing Nyx; waiting for its work to stop"
_SAVE_PROGRESS = "Changing workspace; restarting Nyx work"
_RETRY_PROGRESS = "Retrying; waiting for Nyx work"


def _progress_controls(status, retry_label, *, visible=True):
    """The in-flight screen: progress text, racing actions off, Quit on."""

    return {
        "status": status,
        "retry_label": retry_label,
        "root": False,
        "change": False,
        "save": False,
        "cancel": False,
        "retry": False,
        "quit": True,
        "visible": visible,
    }


@contextlib.contextmanager
def _lifecycle_spies(session, events, names=_LIFECYCLE_METHODS):
    """Record each session lifecycle call and whether a pool submission ran it."""

    calls = []
    with contextlib.ExitStack() as stack:
        for name in names:

            def record(*_args, _name=name, **_kwargs):
                calls.append((_name, events.inside_submission))
                return DEFAULT

            stack.enter_context(
                patch.object(session, name, wraps=getattr(session, name), side_effect=record)
            )
        yield calls


_CLOSING_POOL_TRIGGERS = ("quit_button", "window_close", "shutdown_blocked_close", "board_failure_close")


@pytest.mark.parametrize(
    "trigger",
    [*_CLOSING_POOL_TRIGGERS, "save", "switch_retry", "runtime_retry"],
)
def test_window_runs_every_lifecycle_call_inside_its_pool_submission(trigger):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        instances = []
        failures = {}
        control = {"shutdown": True}
        runtime_type = _replaceable_runtime_type(
            instances, start_failures=failures, control=control
        )
        with _home_patches(home)[0], patch.object(desktop, "ApplicationRuntime", runtime_type):
            state.setup(first)
            if trigger == "runtime_retry":
                failures[first.resolve()] = [RuntimeError("injected startup failure")]
            session = desktop.DesktopSession()
            with contextlib.suppress(desktop.DesktopUnavailableError):
                session.start_runtime()
            window = desktop._build_window(_fake_qt(), session)
            window.show()
            window._open_board()
            pool = window._pool
            assert pool.max_thread_count == 1
            try:
                with _lifecycle_spies(session, pool.events) as calls:
                    if trigger == "quit_button":
                        window._quit.click()
                        expected = ["close"]
                    elif trigger == "window_close":
                        window.close()
                        expected = ["close"]
                    elif trigger == "shutdown_blocked_close":
                        control["shutdown"] = False
                        window._quit.click()
                        assert session.snapshot.status == "shutdown_blocked"
                        assert window._visible
                        assert window._retry.text() == "Close"
                        control["shutdown"] = True
                        window._retry.click()
                        expected = ["close", "close"]
                    elif trigger == "board_failure_close":
                        window._board.loadFinished.emit(False)
                        assert window._board_failed
                        assert window._retry.text() == "Close"
                        window._retry.click()
                        expected = ["close"]
                    elif trigger == "save":
                        window._change.click()
                        window._root.setText(str(second))
                        window._save.click()
                        expected = ["choose_workspace"]
                    elif trigger == "switch_retry":
                        control["shutdown"] = False
                        window._change.click()
                        window._root.setText(str(second))
                        window._save.click()
                        assert session.snapshot.status == "switch_blocked"
                        assert window._retry.text() == "Retry"
                        control["shutdown"] = True
                        window._retry.click()
                        expected = ["choose_workspace", "retry_workspace_switch"]
                    else:
                        assert session.snapshot.status == "runtime_blocked"
                        window._retry.click()
                        expected = ["retry_runtime"]

                # Each call ran inside a pool submission, never in the click
                # handler or in the completion delivered after it.
                assert calls == [(name, True) for name in expected]
                assert pool.submissions == len(expected)
                assert not pool.events.inside_submission
                assert pool.events.pending == []
                if trigger in _CLOSING_POOL_TRIGGERS:
                    assert not window._visible
                    assert not session.claims.held
                    assert session.runtime is None
                else:
                    current = first if trigger == "runtime_retry" else second
                    assert state.load_configuration() == state.Configuration(current.resolve())
                    assert session.snapshot.status == "configured"
                    assert session.runtime is instances[-1]
                    assert window._status.text() == f"Workspace: {current.resolve()}"
                    assert window._change.isEnabled()
                    assert not window._save.isEnabled()
                    assert window._visible
            finally:
                control["shutdown"] = True
                session.close()
            assert not session.claims.held


def test_window_submits_nothing_while_a_lifecycle_operation_is_in_flight():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        instances = []
        control = {"shutdown": False}
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", _replaceable_runtime_type(instances, control=control)
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            session.start_runtime()
            with contextlib.suppress(desktop.DesktopError):
                session.choose_workspace(second)
            assert session.snapshot.status == "switch_blocked"
            window = desktop._build_window(_fake_qt(), session)
            window.show()
            pool = window._pool
            control["shutdown"] = True
            # The pool keeps the retry in flight without running it, so the
            # session is still blocked and every refused click would act.
            pool.hold = True
            spied = (*_LIFECYCLE_METHODS, "cancel")
            with _lifecycle_spies(session, pool.events, spied) as calls:
                window._retry.click()
                assert pool.submissions == 1
                assert calls == []
                in_flight = _window_controls(window)
                assert in_flight["status"] == _RETRY_PROGRESS

                for button in (
                    window._change,
                    window._save,
                    window._retry,
                    window._cancel,
                ):
                    button.clicked.emit()

                assert pool.submissions == 1
                assert calls == []
                assert _window_controls(window) == in_flight

                # Quit and the window close only record a Quit for the
                # running retry; neither starts an operation of its own.
                window._quit.clicked.emit()
                assert not window.close()

                assert pool.submissions == 1
                assert calls == []
                assert _window_controls(window) == {**in_flight, "status": _CLOSING_PROGRESS}
                assert session.snapshot.status == "switch_blocked"
                assert window._visible

                pool.hold = False
                pool.waitForDone()
                pool.events.flush()

            # The Quit was recorded before the retry reached its save, so the
            # retry abandoned the change and closed the session itself.
            assert calls == [("retry_workspace_switch", True), ("close", True)]
            assert pool.submissions == 1
            assert state.load_configuration() == state.Configuration(first.resolve())
            assert session.runtime is None
            assert not window._visible
            assert not session.claims.held


def test_change_during_an_in_flight_close_leaves_the_chooser_closed():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        instances = []
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", _replaceable_runtime_type(instances)
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            session.start_runtime()
            window = desktop._build_window(_fake_qt(), session)
            window.show()
            assert not window._changing
            pool = window._pool
            # Keep the Quit's close in flight from the ordinary configured
            # screen, where Change would otherwise open the chooser.
            pool.hold = True
            spied = (*_LIFECYCLE_METHODS, "cancel")
            with _lifecycle_spies(session, pool.events, spied) as calls:
                window._quit.click()
                assert pool.submissions == 1
                in_flight = _window_controls(window)

                for button in (window._change, window._cancel, window._save, window._retry):
                    button.clicked.emit()

                assert not window._changing
                assert _window_controls(window) == in_flight
                assert pool.submissions == 1
                assert calls == []

                pool.hold = False
                pool.waitForDone()
                pool.events.flush()

            assert calls == [("close", True)]
            assert not window._visible
            assert not session.claims.held


def test_save_submits_the_workspace_shown_when_it_was_clicked():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        third = _workspace(root, "third")
        instances = []
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", _replaceable_runtime_type(instances)
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            session.start_runtime()
            window = desktop._build_window(_fake_qt(), session)
            window.show()
            pool = window._pool
            window._change.click()
            window._root.setText(str(second))
            # The field is a GUI-thread widget: Save reads it in the click
            # handler, never on the pool thread that runs the save.
            reads = []
            real_text = window._root.text

            def recorded_text():
                reads.append(pool.events.inside_submission)
                return real_text()

            pool.hold = True
            with patch.object(window._root, "text", side_effect=recorded_text):
                window._save.click()
                assert reads == [False]
                window._root.setText(str(third))
                pool.hold = False
                pool.waitForDone()
                pool.events.flush()
                assert reads == [False]

            assert pool.submissions == 1
            assert state.load_configuration() == state.Configuration(second.resolve())
            assert session.runtime is instances[1]
            assert window._status.text() == f"Workspace: {second.resolve()}"
            window._quit.click()
            assert not session.claims.held


def test_unexpected_lifecycle_failure_renders_the_current_state_and_reopens_actions():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        instances = []
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", _replaceable_runtime_type(instances)
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            session.start_runtime()
            window = desktop._build_window(_fake_qt(), session)
            window.show()
            assert window._open_board()
            pool = window._pool
            events = pool.events
            window._change.click()
            window._root.setText(str(second))
            before = _window_controls(window)
            real_choose = session.choose_workspace

            def broken_choose(_specification_root):
                # Leave a visible session change behind, then fail unexpectedly.
                with contextlib.suppress(desktop.DesktopError):
                    real_choose(str(root / "missing"))
                raise RuntimeError("injected unexpected failure")

            # Deliver completions only when flushed, after the click handler
            # has returned, as a queued native delivery does.
            events.deferred = True
            with patch.object(session, "choose_workspace", side_effect=broken_choose):
                window._save.click()
                assert pool.submissions == 1
                assert session.pending_error == "Nyx workspace could not be validated"
                in_flight = _window_controls(window)
                assert in_flight == _progress_controls(_SAVE_PROGRESS, before["retry_label"])
                window._save.clicked.emit()
                assert pool.submissions == 1
                assert _window_controls(window) == in_flight
                with pytest.raises(RuntimeError, match="injected unexpected failure"):
                    events.flush()

            after = _window_controls(window)
            assert after != before
            assert after["status"] == "Nyx workspace could not be validated"
            assert after["status"] != _SAVE_PROGRESS
            assert after["save"] and after["root"]
            window._render()
            assert _window_controls(window) == after

            events.deferred = False
            window._save.click()
            assert pool.submissions == 2
            assert state.load_configuration() == state.Configuration(second.resolve())
            assert session.runtime is instances[1]
            assert window._status.text() == f"Workspace: {second.resolve()}"
            window._quit.click()
            assert not session.claims.held


def test_presentation_release_waits_for_the_operation_then_hides_without_submitting():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        instances = []
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", _replaceable_runtime_type(instances)
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            session.start_runtime()
            window = desktop._build_window(_fake_qt(), session)
            window.show()
            assert window._open_board()
            pool = window._pool
            window._change.click()
            window._root.setText(str(second))
            order = []
            real_choose = session.choose_workspace
            real_hide = window.hide

            def recorded_choose(*args):
                try:
                    return real_choose(*args)
                finally:
                    order.append("operation_completed")

            def recorded_hide():
                order.append("hide")
                real_hide()

            pool.hold = True
            with (
                patch.object(session, "choose_workspace", side_effect=recorded_choose),
                patch.object(window, "hide", side_effect=recorded_hide),
                patch.object(window, "close", wraps=window.close) as close,
            ):
                window._save.click()
                assert pool.submissions == 1
                assert order == []
                assert window._visible

                desktop._release_presentation(None, window)

                assert order == ["operation_completed", "hide"]
                assert pool.waits >= 1
                assert close.call_count == 0
                assert not window._visible
                assert pool.submissions == 1
                assert window._relay.completed.callbacks == []
                # A completion posted before the disconnect is still delivered
                # by a later event-loop turn; it starts no operation.
                pool.events.flush()
                assert pool.submissions == 1
                assert close.call_count == 0
                assert not window._visible

            assert state.load_configuration() == state.Configuration(second.resolve())
            session.close()
            assert not session.claims.held


class _SessionAccessRecorder:
    """Record each session method or property reached, with its calling thread.

    Installed before the window is built.  Only accesses made from outside
    the session are recorded: once a session method or property runs, the
    session's own reads of itself are not, so a pool thread running a
    lifecycle call records nothing while the window's reads record the GUI
    thread or whichever thread made them.
    """

    def __init__(self, session):
        self.accesses = []
        self._lock = threading.Lock()
        self._local = threading.local()
        recorder = self
        base = type(session)

        def inside():
            return getattr(recorder._local, "depth", 0)

        def enter():
            recorder._local.depth = inside() + 1

        def leave():
            recorder._local.depth = inside() - 1

        def __getattribute__(obj, name):
            if name.startswith("__") or inside():
                return object.__getattribute__(obj, name)
            with recorder._lock:
                recorder.accesses.append((name, threading.current_thread()))
            enter()
            try:
                value = object.__getattribute__(obj, name)
            finally:
                leave()
            if not inspect.ismethod(value):
                return value

            @functools.wraps(value)
            def call(*args, **kwargs):
                enter()
                try:
                    return value(*args, **kwargs)
                finally:
                    leave()

            return call

        session.__class__ = type(
            f"Recorded{base.__name__}", (base,), {"__getattribute__": __getattribute__}
        )

    def mark(self):
        with self._lock:
            return len(self.accesses)

    def since(self, mark):
        with self._lock:
            return list(self.accesses[mark:])

    def names_from(self, mark, thread):
        return [name for name, caller in self.since(mark) if caller is thread]


class _Gate:
    """Hold one runtime call until released; report whether it is held."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.holding = False

    def wait(self, deadline=None):
        timeout = 30.0 if deadline is None else max(0.0, deadline - time.monotonic())
        self.holding = True
        self.entered.set()
        try:
            return self.release.wait(timeout)
        finally:
            self.holding = False


class _ScriptedLifecycle:
    """Script a runtime double's starts and shutdowns, holding any of them.

    ``start_script`` maps a resolved root to the outcomes of its next starts
    in order: an exception is raised, a :class:`_Gate` holds the started
    runtime until released.  ``shutdown_script`` lists ``(gate, completes)``
    for the next shutdowns in order; a gated shutdown that is not released
    returns ``False`` at its deadline.  Unscripted calls succeed at once.
    """

    def __init__(self):
        self.started_roots = []
        self.shutdown_roots = []
        self.start_script = {}
        self.shutdown_script = []

    def runtime_type(self):
        control = self

        class ScriptedRuntime:
            def __init__(self, **_kwargs):
                self.configuration = None

            def capture_configuration(self, configuration, _paths):
                self.configuration = configuration

            def start(self, *, static_ready=None):
                assert static_ready is None or static_ready()
                root = self.configuration.specification_root
                pending = control.start_script.get(root)
                outcome = pending.pop(0) if pending else None
                if isinstance(outcome, BaseException):
                    raise outcome
                control.started_roots.append(root)
                if outcome is not None:
                    assert outcome.wait(), "a held start was never released"

            def admit_catalog(self):
                pass

            def shutdown(self, deadline):
                gate, completes = (
                    control.shutdown_script.pop(0) if control.shutdown_script else (None, True)
                )
                if gate is not None and not gate.wait(deadline):
                    return False
                if completes:
                    control.shutdown_roots.append(self.configuration.specification_root)
                return completes

            def cleanup_start_failure(self, _deadline):
                return True

        return ScriptedRuntime


_HELD_PROGRESS_CASES = ("save", "board_failure_close", "close_then_quit", "runtime_retry_then_quit")


@pytest.mark.parametrize("case", [*_HELD_PROGRESS_CASES, "save_then_quit"])
def test_held_operation_shows_its_progress_and_reaches_only_request_quit(case):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        instances = []
        failures = {}
        runtime_type = _replaceable_runtime_type(instances, start_failures=failures)
        with _home_patches(home)[0], patch.object(desktop, "ApplicationRuntime", runtime_type):
            state.setup(first)
            first_bytes = state.state_paths().config_file.read_bytes()
            if case == "runtime_retry_then_quit":
                failures[first.resolve()] = [RuntimeError("injected startup failure")]
            session = desktop.DesktopSession()
            with contextlib.suppress(desktop.DesktopUnavailableError):
                session.start_runtime()
            recorder = _SessionAccessRecorder(session)
            window = desktop._build_window(_fake_qt(), session)
            window.show()
            window._open_board()
            pool = window._pool
            gui = threading.current_thread()
            try:
                if case in {"save", "save_then_quit"}:
                    window._change.click()
                    window._root.setText(str(second))
                    trigger, progress = window._save, _SAVE_PROGRESS
                elif case == "board_failure_close":
                    window._board.loadFinished.emit(False)
                    assert window._status.text() == desktop.BOARD_FAILURE_MESSAGE
                    trigger, progress = window._retry, _CLOSING_PROGRESS
                elif case == "close_then_quit":
                    trigger, progress = window._quit, _CLOSING_PROGRESS
                else:
                    assert session.snapshot.status == "runtime_blocked"
                    trigger, progress = window._retry, _RETRY_PROGRESS
                label = window._retry.text()

                pool.hold = True
                trigger.click()
                held = recorder.mark()
                assert pool.submissions == 1
                assert _window_controls(window) == _progress_controls(progress, label)
                if case.endswith("_then_quit"):
                    window._quit.click()
                    assert pool.submissions == 1
                    assert _window_controls(window) == _progress_controls(
                        _CLOSING_PROGRESS, label
                    )
                    expected_names = ["request_quit"]
                else:
                    expected_names = []
                # While the operation is held the window reached the session
                # only to record a Quit.
                assert recorder.names_from(held, gui) == expected_names

                pool.hold = False
                pool.waitForDone()
                pool.events.flush()

                if case == "save":
                    assert pool.submissions == 1
                    assert state.load_configuration() == state.Configuration(second.resolve())
                    assert window._status.text() == f"Workspace: {second.resolve()}"
                    assert window._change.isEnabled()
                    assert window._visible
                else:
                    if case == "runtime_retry_then_quit":
                        # The retry started the runtime; one chained close
                        # carried out the Quit.
                        assert len(instances) == 2
                        assert pool.submissions == 2
                    else:
                        assert pool.submissions == 1
                    if case == "save_then_quit":
                        assert state.state_paths().config_file.read_bytes() == first_bytes
                    assert session.runtime is None
                    assert not session.claims.held
                    assert not window._visible
            finally:
                session.close()
            assert not session.claims.held


def test_quit_during_a_held_close_that_stays_blocked_chains_exactly_one_close():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        instances = []
        control = {"shutdown": False}
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", _replaceable_runtime_type(instances, control=control)
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            session.start_runtime()
            window = desktop._build_window(_fake_qt(), session)
            window.show()
            pool = window._pool
            pool.hold = True
            with _lifecycle_spies(session, pool.events) as calls:
                window._quit.click()
                window._quit.click()
                window.close()
                assert pool.submissions == 1
                assert calls == []

                pool.hold = False
                pool.waitForDone()
                pool.events.flush()

            # The held close stayed blocked, so the recorded Quit made exactly
            # one more full close; its blocked outcome is final.
            assert calls == [("close", True), ("close", True)]
            assert pool.submissions == 2
            assert pool.events.pending == []
            _assert_blocked_close_retry(window, session)
            assert window._status.text() == "Nyx workers are still stopping; retry Close"

            control["shutdown"] = True
            window._retry.click()
            assert pool.submissions == 3
            assert not window._visible
            assert not session.claims.held
            assert session.runtime is None


def test_unexpected_failure_with_a_recorded_quit_still_closes_the_window():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        instances = []
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", _replaceable_runtime_type(instances)
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            session.start_runtime()
            window = desktop._build_window(_fake_qt(), session)
            window.show()
            pool = window._pool
            window._change.click()
            window._root.setText(str(second))

            def broken_choose(_specification_root):
                raise RuntimeError("injected unexpected failure")

            pool.hold = True
            with patch.object(session, "choose_workspace", side_effect=broken_choose):
                window._save.click()
                window._quit.click()
                assert window._status.text() == _CLOSING_PROGRESS
                pool.hold = False
                pool.waitForDone()
                # The failure still surfaces, after the recorded Quit has been
                # carried out by one more close.
                with pytest.raises(RuntimeError, match="injected unexpected failure"):
                    pool.events.flush()

            assert pool.submissions == 2
            assert not window._visible
            assert not session.claims.held
            assert session.runtime is None
            assert state.load_configuration() == state.Configuration(first.resolve())


def test_quit_recorded_during_an_operation_starts_no_close_once_teardown_begins():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        instances = []
        control = {"shutdown": False}
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", _replaceable_runtime_type(instances, control=control)
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            session.start_runtime()
            window = desktop._build_window(_fake_qt(), session)
            window.show()
            pool = window._pool
            pool.hold = True
            with patch.object(window, "close", wraps=window.close) as close:
                window._quit.click()
                close.reset_mock()
                window._quit.click()
                assert window._status.text() == _CLOSING_PROGRESS
                assert pool.submissions == 1

                desktop._release_presentation(None, window)

                assert pool.submissions == 1
                assert not window._visible
                # The held close stayed blocked; its completion, posted before
                # the disconnect, is still delivered by a later event-loop
                # turn and must not carry out the recorded Quit.
                assert len(pool.events.pending) == 1
                pool.events.flush()
                assert pool.submissions == 1
                assert close.call_count == 0
                assert not window._visible

            assert session.snapshot.status == "shutdown_blocked"
            assert session.claims.held
            control["shutdown"] = True
            session.close()
            assert not session.claims.held


def _switch_on_thread(session, specification_root):
    outcome = {}

    def run():
        try:
            outcome["result"] = session.choose_workspace(specification_root)
        except BaseException as error:  # noqa: BLE001 - asserted by the caller
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


@pytest.mark.parametrize("requester", ["request_quit", "close"])
def test_quit_requested_while_the_switch_stop_is_held_keeps_the_saved_workspace(requester):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        control = _ScriptedLifecycle()
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", control.runtime_type()
        ):
            state.setup(first)
            paths = state.state_paths()
            first_bytes = paths.config_file.read_bytes()
            session = desktop.DesktopSession()
            session.start_runtime()
            gate = _Gate()
            control.shutdown_script.append((gate, True))
            thread, outcome = _switch_on_thread(session, second)
            try:
                assert gate.entered.wait(timeout=10)
                getattr(session, requester)()
                # A Quit only records the request; the switch still owns the
                # session and its claims.
                assert session.claims.held
                assert gate.holding
            finally:
                gate.release.set()
                thread.join(timeout=10)
            assert not thread.is_alive()
            assert isinstance(outcome.get("error"), desktop.SelectionUnavailableError)
            assert str(outcome["error"]) == "Nyx workspace change canceled"
            assert paths.config_file.read_bytes() == first_bytes
            assert control.started_roots == [first.resolve()]
            assert control.shutdown_roots == [first.resolve()]
            assert session.runtime is None
            assert not session.claims.held


def test_quit_requested_right_after_the_switch_commit_keeps_b_and_never_starts_it():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        control = _ScriptedLifecycle()
        committed = threading.Event()
        release = threading.Event()
        real_save = state.save_configuration_owned

        def held_after_commit(*args, **kwargs):
            result = real_save(*args, **kwargs)
            committed.set()
            assert release.wait(timeout=10)
            return result

        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", control.runtime_type()
        ):
            state.setup(first)
            paths = state.state_paths()
            session = desktop.DesktopSession()
            session.start_runtime()
            with patch.object(state, "save_configuration_owned", side_effect=held_after_commit):
                thread, outcome = _switch_on_thread(session, second)
                try:
                    assert committed.wait(timeout=10)
                    session.request_quit()
                    assert session.claims.held
                finally:
                    release.set()
                    thread.join(timeout=10)
            assert not thread.is_alive()
            assert "error" not in outcome
            assert outcome["result"] == state.Configuration(second.resolve())
            saved = json.loads(paths.config_file.read_text(encoding="utf-8"))
            assert saved["specification_root"] == str(second.resolve())
            assert control.started_roots == [first.resolve()]
            assert control.shutdown_roots == [first.resolve()]
            assert session.runtime is None
            assert not session.claims.held


def test_quit_requested_while_b_starts_keeps_b_and_shuts_it_down():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        control = _ScriptedLifecycle()
        gate = _Gate()
        control.start_script[second.resolve()] = [gate]
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", control.runtime_type()
        ):
            state.setup(first)
            paths = state.state_paths()
            session = desktop.DesktopSession()
            session.start_runtime()
            thread, outcome = _switch_on_thread(session, second)
            try:
                assert gate.entered.wait(timeout=10)
                session.request_quit()
                assert session.claims.held
            finally:
                gate.release.set()
                thread.join(timeout=10)
            assert not thread.is_alive()
            assert "error" not in outcome
            assert outcome["result"] == state.Configuration(second.resolve())
            saved = json.loads(paths.config_file.read_text(encoding="utf-8"))
            assert saved["specification_root"] == str(second.resolve())
            assert control.started_roots == [first.resolve(), second.resolve()]
            assert control.shutdown_roots == [first.resolve(), second.resolve()]
            assert session.runtime is None
            assert not session.claims.held


def test_quit_requested_while_b_starts_and_then_fails_closes_the_session():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        control = _ScriptedLifecycle()

        class FailingStart(_Gate):
            """Hold B's start, then fail it once released."""

            def wait(self, deadline=None):
                super().wait(deadline)
                raise RuntimeError("injected startup failure")

        gate = FailingStart()
        control.start_script[second.resolve()] = [gate]
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", control.runtime_type()
        ):
            state.setup(first)
            paths = state.state_paths()
            session = desktop.DesktopSession()
            session.start_runtime()
            thread, outcome = _switch_on_thread(session, second)
            try:
                assert gate.entered.wait(timeout=10)
                session.request_quit()
                assert session.claims.held
            finally:
                gate.release.set()
                thread.join(timeout=10)
            assert not thread.is_alive()
            # B's start failed after the commit, so the change reports the
            # failure, but the recorded Quit still closes the session.
            assert isinstance(outcome.get("error"), desktop.DesktopUnavailableError)
            saved = json.loads(paths.config_file.read_text(encoding="utf-8"))
            assert saved["specification_root"] == str(second.resolve())
            assert control.started_roots == [first.resolve(), second.resolve()]
            assert control.shutdown_roots == [first.resolve()]
            assert session.runtime is None
            assert not session.claims.held


def test_a_close_that_proceeds_consumes_the_requested_quit():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        control = _ScriptedLifecycle()
        # The first stop stays blocked; later stops complete.
        control.shutdown_script.append((None, False))
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", control.runtime_type()
        ):
            state.setup(first)
            paths = state.state_paths()
            session = desktop.DesktopSession()
            session.start_runtime()
            # A Quit recorded while no workspace change runs, as the window
            # records one during a running close or retry, is carried out by
            # the next close that proceeds, even when its stop stays blocked.
            session.request_quit()
            session.close()
            assert session.snapshot.status == "shutdown_blocked"
            assert session.claims.held

            # A later workspace change is therefore not cancelled by a Quit
            # that a close has already acted on.
            assert session.choose_workspace(second) == state.Configuration(second.resolve())
            saved = json.loads(paths.config_file.read_text(encoding="utf-8"))
            assert saved["specification_root"] == str(second.resolve())
            assert control.started_roots == [first.resolve(), second.resolve()]
            assert control.shutdown_roots == [first.resolve()]
            assert session.runtime is not None
            assert session.claims.held

            session.close()
            assert control.shutdown_roots == [first.resolve(), second.resolve()]
            assert not session.claims.held


def test_active_workspace_switch_restarts_real_catalog_worker_over_http():
    port = runtime.PORT

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
    port = runtime.PORT

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


def test_desktop_catalog_worker_runs_in_state_directory_not_launch_directory(
    tmp_path, monkeypatch
):
    port = runtime.PORT

    home = _home(tmp_path)
    workspace = _workspace(tmp_path, "workspace")
    package = workspace / "Fictional" / "Queue" / "sample"
    package.mkdir(parents=True)
    package.joinpath("spec.md").write_text("# SAMPLE\n", encoding="utf-8")
    launch = tmp_path / "launch"
    launch.mkdir()
    marker = tmp_path / "shadow-imports.log"
    plant_shadow(launch, marker)
    install_proof_environment(monkeypatch, home, pythonpath=".")
    # The desktop process itself runs in the shadow directory, so a worker
    # that inherited this directory would resolve ``PYTHONPATH=.`` to it.
    monkeypatch.chdir(launch)
    assert Path.cwd() == launch.resolve()
    assert os.environ["PYTHONPATH"] == "."

    with _home_patches(home)[0]:
        state.setup(workspace)
        session = desktop.DesktopSession()
        try:
            session.start_runtime()
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            try:
                connection.request(
                    "GET",
                    "/api/catalog",
                    headers={"Host": f"127.0.0.1:{port}"},
                )
                response = connection.getresponse()
                status, catalog = response.status, json.loads(response.read())
            finally:
                connection.close()
        finally:
            # Release the session's handles before the temporary tree is
            # removed; Windows refuses to delete open files.
            session.close()

    assert status == 200
    assert catalog["schema_version"] == SCHEMA_VERSION
    assert parse_catalog(catalog).as_dict() == catalog
    assert [entry["declared"]["title"] for entry in catalog["entries"]] == ["SAMPLE"]
    assert shadow_imports(marker) == []


def test_active_workspace_switch_real_settings_endpoint_restores_each_root_order():
    port = runtime.PORT

    def request_settings(method="GET", payload=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            body = None if payload is None else json.dumps(payload).encode()
            headers = {"Host": f"127.0.0.1:{port}"}
            if body is not None:
                headers["Content-Type"] = "application/json"
                headers["Content-Length"] = str(len(body))
            connection.request(method, "/api/settings", body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        with (
            _home_patches(home)[0],
            patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            try:
                session.start_runtime()
                status, first_settings = request_settings()
                assert status == 200
                assert first_settings["order"] == []
                assert first_settings["completed"] == []
                first_saved_status, first_saved = request_settings(
                    "PUT",
                    {
                        "revision": first_settings["revision"],
                        "order": ["Done", "Queue"],
                        "completed": ["Done"],
                    },
                )
                assert first_saved_status == 200
                assert first_saved["outcome"] == "success"

                session.choose_workspace(second)
                status, second_settings = request_settings()
                assert status == 200
                assert second_settings["order"] == []
                assert second_settings["completed"] == []
                second_saved_status, second_saved = request_settings(
                    "PUT",
                    {
                        "revision": second_settings["revision"],
                        "order": ["Archive"],
                        "completed": ["Archive"],
                    },
                )
                assert second_saved_status == 200
                assert second_saved["outcome"] == "success"

                session.choose_workspace(first)
                status, restored = request_settings()
                assert status == 200
                assert restored["order"] == ["Done", "Queue"]
                assert restored["completed"] == ["Done"]
            finally:
                session.close()


def test_active_switch_postcommit_verification_failure_preserves_b_on_quit():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        with _home_patches(home)[0]:
            state.setup(first)
            paths = state.state_paths()
            session = desktop.DesktopSession()

            class FakeApplicationRuntime:
                def __init__(self, **_kwargs):
                    pass

                def capture_configuration(self, _configuration, _paths):
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

            def capture_configuration(self, configuration, paths):
                self.configuration = configuration
                self.paths = paths

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
            assert state.load_configuration() == state.Configuration(second.resolve())
            assert session.runtime is None
            assert session.runtime_retryable
            assert session.claims.held
            session.close()
            assert state.load_configuration() == state.Configuration(second.resolve())


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
        patch.object(desktop.runtime, "desktop_host", return_value=True),
        patch.object(desktop, "DesktopSession", Session),
        patch.object(desktop, "_load_qt", return_value={"QtWidgets": Widgets}),
        patch.object(desktop, "_build_window", return_value=window),
    ):
        assert desktop.main([]) == 0

    assert calls[:2] == ["start_runtime", "show"]
    assert calls[-1] == "close"
    assert calls[2:-1] == ["open_board"]


@pytest.mark.parametrize("error_name", ["EADDRINUSE", "EACCES", "ENOMEM"])
def test_failed_runtime_bind_shows_sanitized_retry_message(error_name):
    import errno

    injected = OSError(getattr(errno, error_name), "private-bind-detail")

    def fail(**_kwargs):
        raise injected

    class FailingApplicationRuntime(app_runtime.ApplicationRuntime):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._server_factory = fail

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", FailingApplicationRuntime
        ):
            state.setup(workspace)
            session = desktop.DesktopSession()
            try:
                with pytest.raises(desktop.DesktopUnavailableError) as caught:
                    session.start_runtime(static_ready=lambda: True)
                if error_name == "ENOMEM":
                    assert caught.value.__cause__ is injected
                    expected = "Nyx runtime could not start; retry"
                else:
                    assert isinstance(
                        caught.value.__cause__, app_runtime.ApplicationPortUnavailableError
                    )
                    assert caught.value.__cause__.__cause__ is injected
                    expected = runtime.PORT_UNAVAILABLE_MESSAGE
                assert session.pending_error == expected
                assert session.snapshot.status == "runtime_blocked"
                assert session.runtime_retryable
                window = desktop._build_window(_fake_qt(), session)
                assert window._status.text() == expected
                assert "private-bind-detail" not in window._status.text()
                assert window._retry.isEnabled()
            finally:
                session.close()
            assert not session.claims.held


@pytest.mark.parametrize("error_name", ["EADDRINUSE", "EACCES", "ENOMEM"])
def test_application_runtime_classifies_only_port_unavailable_errors(error_name):
    import errno

    injected = OSError(getattr(errno, error_name), "private-bind-detail")

    def fail(**_kwargs):
        raise injected

    application = app_runtime.ApplicationRuntime(
        port=runtime.PORT, deadline=time.monotonic() + 5, server_factory=fail
    )
    if error_name == "ENOMEM":
        with pytest.raises(OSError) as caught:
            application.start()
        assert caught.value is injected
    else:
        with pytest.raises(app_runtime.ApplicationPortUnavailableError) as caught:
            application.start()
        assert caught.value.__cause__ is injected


@pytest.mark.parametrize("error_name", ["EADDRINUSE", "EACCES"])
@pytest.mark.parametrize("failure_phase", ["thread_start", "readiness"])
def test_application_runtime_does_not_classify_errors_after_bind(error_name, failure_phase):
    import errno

    injected = OSError(getattr(errno, error_name), "private-post-bind-detail")

    def fail():
        raise injected

    server = SimpleNamespace(serve_forever=lambda: None)
    thread = SimpleNamespace(start=fail if failure_phase == "thread_start" else lambda: None)
    application = app_runtime.ApplicationRuntime(
        port=runtime.PORT,
        deadline=time.monotonic() + 5,
        server_factory=lambda **_kwargs: server,
        thread_factory=lambda **_kwargs: thread,
    )
    with pytest.raises(OSError) as caught:
        application.start(static_ready=fail)
    assert caught.value is injected
    assert application.server is server
    assert application.http_thread is thread


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


def test_qt_is_optional_for_linux_source_collection():
    with patch.dict(sys.modules, {"PySide6": None}):
        with pytest.raises(desktop.DesktopDependencyError, match=r"nyx\[desktop\]") as raised:
            desktop._load_qt()
    assert isinstance(raised.value.__cause__, ImportError)


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
            state.setup(first)
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
            _wait_for_window_operation(application, window)
            assert state.load_configuration() == state.Configuration(
                second.resolve()
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
            _wait_for_window_operation(application, window)
            capabilities["close_delivered"] = not session.claims.held and not window.isVisible()
            print(f"NYX_NATIVE_DESKTOP_CAPABILITIES={json.dumps(capabilities, sort_keys=True)}")
            assert not session.claims.held
            assert not window.isVisible()
            assert state.load_configuration() == state.Configuration(
                second.resolve()
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
    """A sample workspace with content, a finished stage, and an edge."""

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
            "# Completed work\n"
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


_BOARD_FAILURE_TRIGGERS = {
    "close_button": lambda window: window._retry.click(),
    "quit_button": lambda window: window._quit.click(),
    "window_close": lambda window: window.close(),
}


@pytest.mark.parametrize("trigger", list(_BOARD_FAILURE_TRIGGERS))
def test_post_admission_board_load_failure_reaps_shared_runtime(trigger):
    port = runtime.PORT
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _workspace(root, "workspace")
        with (
            _home_patches(home)[0],
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
                for _ in range(2):
                    # A repeated failed load changes nothing.
                    window._board.loadFinished.emit(False)
                    assert window._board_failed
                    _assert_board_failure_screen(window, session, application)
                    assert session.runtime is application
                    assert _request_catalog(port)[0] == 200

                _BOARD_FAILURE_TRIGGERS[trigger](window)
                assert not session.claims.held
                assert session.runtime is None
                assert not window._visible
                with pytest.raises((OSError, http.client.HTTPException)):
                    _request_catalog(port)
            finally:
                session.close()


@pytest.mark.parametrize("trigger", list(_BOARD_FAILURE_TRIGGERS))
def test_blocked_board_failure_cleanup_keeps_retry_close_for_every_trigger(trigger):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        first = _workspace(root, "first")
        instances = []
        control = {"shutdown": True}
        with _home_patches(home)[0], patch.object(
            desktop, "ApplicationRuntime", _replaceable_runtime_type(instances, control=control)
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            session.start_runtime()
            admitted = session.runtime
            window = desktop._build_window(_fake_qt(), session)
            window.show()
            assert window._open_board()
            window._board.loadFinished.emit(False)
            _assert_board_failure_screen(window, session, admitted)

            # Quit and closing the window block exactly as Close does: the
            # failure screen stays, now explaining the refused cleanup.
            control["shutdown"] = False
            _BOARD_FAILURE_TRIGGERS[trigger](window)
            _assert_blocked_close_retry(window, session)
            assert session.runtime is admitted
            assert not window._root.isEnabled()
            assert not window._change.isEnabled()
            assert not window._save.isEnabled()
            assert not window._cancel.isEnabled()

            # Another failed load while cleanup is blocked changes nothing.
            window._board.loadFinished.emit(False)
            _assert_blocked_close_retry(window, session)

            control["shutdown"] = True
            window._retry.click()
            assert not window._visible
            assert not session.claims.held
            assert session.runtime is None


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
            # No cleanup has run before Close.
            assert not session.shutdown_blocked
            _assert_board_failure_screen(window, session, application)

            window._retry.click()
            _assert_blocked_close_retry(window, session)
            assert session.runtime is application
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
    port = runtime.PORT
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = _home(root)
        workspace = _board_workspace(root)
        with (
            _home_patches(home)[0],
            patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),
        ):
            state.setup(workspace)
            state.save_configuration_owned(workspace, completed_stage_names=["Done"])
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
                assert "Completed work" in titles
                assert payload["terminal_stages"] == ["Done"]
                assert sum(entry["stage"] == "Done" for entry in payload["entries"]) == 1
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
    boardReady: false,
    error: null,
    refreshIssue: '',
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
    result.boardReady = !!document.querySelector('#board .card, #board .board-empty');
    result.refreshIssue = document.querySelector('#board-issues summary')?.textContent || '';
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


def _wait_for_window_operation(application, window, timeout: float = 30.0) -> None:
    """Pump events until the window's lifecycle operation has completed.

    A window that runs its lifecycle calls synchronously never has one in
    flight, so the wait returns at once there.
    """

    deadline = time.monotonic() + timeout
    while getattr(window, "_operation", None) is not None:
        assert time.monotonic() < deadline, "the window's lifecycle operation never completed"
        _pump_native_events(application, 20)


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
    board is accepted once cards or an empty result have rendered without a refresh failure.
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
        if snapshot.get("boardReady") and "Latest refresh issue:" not in snapshot.get("refreshIssue", ""):
            return snapshot
        _pump_native_events(application, 50)
    raise AssertionError(_board_diagnostic(snapshot, probe, raw))


def _wait_for_refresh_issue(application, page, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = _eval_js(
            application,
            page,
            "document.querySelector('#board-issues summary')?.textContent || ''",
            timeout=15.0,
        )
        if isinstance(value, str) and "Latest refresh issue:" in value:
            return value
        _pump_native_events(application, 50)
    raise AssertionError(f"board refresh issue never appeared: {value!r}")


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
    port = runtime.PORT
    with _native_temp_home() as root:
        home = _home(root)
        workspace = _board_workspace(root)
        application = _native_application()
        with (
            _home_patches(home)[0],
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
            "boardReady": True,
            "refreshIssue": "",
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
    port = runtime.PORT
    with _native_temp_home() as root:
        home = _home(root)
        workspace = _board_workspace(root)
        application = _native_application()
        with (
            _home_patches(home)[0],
            patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),
        ):
            state.setup(workspace)
            state.save_configuration_owned(workspace, completed_stage_names=["Done"])
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
                assert snapshot["boardReady"] is True
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
def test_required_native_embedded_board_renders_content_gated_finished_stage_and_dependency():
    _assert_native_host_has_no_offscreen_platform()
    port = runtime.PORT
    with _native_temp_home() as root:
        home = _home(root)
        workspace = _board_workspace(root)
        application = _native_application()
        with (
            _home_patches(home)[0],
            patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),
        ):
            state.setup(workspace)
            state.save_configuration_owned(workspace, completed_stage_names=["Done"])
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
                assert "Completed work" not in snapshot["titles"]
                assert "done" not in snapshot["rows"]
                assert any(
                    "needs:" in link and "Beta design" in link
                    for link in snapshot["links"]
                )
                shared.catalog_admitted = False
                _eval_js(application, page, "document.querySelector('#refresh').click()")
                assert "Latest refresh issue:" in _wait_for_refresh_issue(application, page)
                shared.catalog_admitted = True
                _eval_js(application, page, "document.querySelector('#refresh').click()")
                recovered = _wait_for_json_board(application, page, probe=probe)
                assert "Alpha delivery" in recovered["titles"]
                assert "Latest refresh issue:" not in recovered["refreshIssue"]
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
    with _native_temp_home() as root:
        home = _home(root)
        workspace = _board_workspace(root)
        application = _native_application()
        with (
            _home_patches(home)[0],
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
    dead_port = distinct_free_port()
    with _native_temp_home() as root:
        home = _home(root)
        workspace = _board_workspace(root)
        application = _native_application()
        with (
            _home_patches(home)[0],
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
                admitted = session.runtime
                window._board.setUrl(QtCore.QUrl(f"http://127.0.0.1:{dead_port}/"))
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and not window._board_failed:
                    _pump_native_events(application, 50)
                assert window._board_failed, "the dead-port board load never failed"
                # The failure screen stays up and the admitted work keeps running
                # until the user chooses Close.
                assert window.isVisible()
                assert window._status.text() == desktop.BOARD_FAILURE_MESSAGE
                assert window._retry.text() == "Close"
                assert window._retry.isEnabled()
                assert session.claims.held
                assert session.runtime is admitted is not None
                window._retry.click()
                _wait_for_window_operation(application, window)
                if session.shutdown_blocked:
                    assert session.claims.held
                    assert window.isVisible()
                    assert window._retry.isEnabled()
                    assert window._status.text() == (
                        "Nyx workers are still stopping; retry Close"
                    )
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


def _titled_workspace(root: Path, name: str, titles) -> Path:
    """A workspace whose Queue packages carry exactly the given card titles."""

    workspace = _workspace(root, name)
    for index, title in enumerate(titles):
        directory = workspace / "Project" / "Queue" / f"package-{index}"
        directory.mkdir(parents=True)
        (directory / "spec.md").write_text(
            f"# {title}\nTarget repo: Project\n", encoding="utf-8"
        )
    return workspace


def _wait_for_board_titles(application, page, titles, timeout: float = 60.0, probe=None):
    """Poll the rendered board until every given title is shown.

    The view keeps the previous document while a replacement navigation is
    in flight, so an observation can briefly be missing or come from either
    document; only the deadline turns that into a failure.
    """

    deadline = time.monotonic() + timeout
    raw = None
    snapshot = None
    last_error = None
    while time.monotonic() < deadline:
        try:
            raw = _eval_js(application, page, _BOARD_JSON_SNAPSHOT_SCRIPT, timeout=15.0)
            snapshot = _parse_board_observation(raw, probe)
        except AssertionError as error:
            last_error = error
        else:
            if snapshot.get("boardReady") and set(titles) <= set(snapshot.get("titles", [])):
                return snapshot
        _pump_native_events(application, 50)
    raise AssertionError(
        f"titles {sorted(titles)!r} never rendered (last observation error: "
        f"{last_error}): " + _board_diagnostic(snapshot, probe, raw)
    )


@pytest.mark.skipif(not _NATIVE_BOARD, reason=_NATIVE_BOARD_SKIP)
def test_required_native_change_workspace_save_replaces_the_rendered_board():
    _assert_native_host_has_no_offscreen_platform()
    first_titles = ("Amber harbor", "Amber lantern")
    second_titles = ("Cobalt meadow",)
    with _native_temp_home() as root:
        home = _home(root)
        first = _titled_workspace(root, "first", first_titles)
        second = _titled_workspace(root, "second", second_titles)
        application = _native_application()
        with (
            _home_patches(home)[0],
            patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),
        ):
            state.setup(first)
            session = desktop.DesktopSession()
            session.start_runtime()
            first_runtime = session.runtime
            assert first_runtime is not None and first_runtime.catalog_admitted
            window = desktop._build_window(_native_qt(), session)
            window.show()
            released = None
            try:
                page = window._board.page()
                probe = _NavigationProbe(page, requested_url=first_runtime.board_url())
                # Opened exactly as the desktop entry does, with one call.
                assert window._open_board()
                initial = _wait_for_json_board(application, page, probe=probe)
                assert set(first_titles) <= set(initial["titles"])
                assert not set(second_titles) & set(initial["titles"])

                window._change.click()
                window._root.setText(str(second))
                window._save.click()
                _wait_for_window_operation(application, window)

                saved = state.load_configuration()
                assert saved == state.Configuration(second.resolve()), (
                    "the workspace save did not commit the replacement: "
                    f"status={session.snapshot.status} "
                    f"pending_error={session.pending_error!r} "
                    f"switch_blocked={session.switch_blocked} "
                    f"shutdown_blocked={session.shutdown_blocked} "
                    f"saved_root={saved.specification_root}"
                )
                replaced = _wait_for_board_titles(
                    application, page, second_titles, probe=probe
                )
                assert not set(first_titles) & set(replaced["titles"]), replaced["titles"]
                assert window.isVisible()
                assert session.claims.held
                assert session.runtime is not None
                assert session.runtime is not first_runtime
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


class _HeldCleanup:
    """Control a runtime double whose shutdown can be held on a release event."""

    def __init__(self):
        self.hold = False
        self.complete = False
        self.holding = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.started_roots = []

    def arm(self, *, complete):
        self.entered.clear()
        self.release.clear()
        self.complete = complete
        self.hold = True

    def disarm(self):
        self.hold = False
        self.complete = True
        self.release.set()


def _held_cleanup_runtime_type(control):
    class HeldCleanupRuntime:
        def __init__(self, **_kwargs):
            self.configuration = None

        def capture_configuration(self, configuration, _paths):
            self.configuration = configuration

        def start(self, *, static_ready=None):
            assert static_ready is None or static_ready()
            control.started_roots.append(self.configuration.specification_root)

        def admit_catalog(self):
            pass

        def shutdown(self, deadline):
            if not control.hold:
                return control.complete
            control.holding = True
            control.entered.set()
            try:
                released = control.release.wait(
                    timeout=max(0.0, deadline - time.monotonic())
                )
            finally:
                control.holding = False
            return released and control.complete

        def cleanup_start_failure(self, _deadline):
            return True

    return HeldCleanupRuntime


def _assert_responsive_while_cleanup_held(application, window, control, action):
    """Run ``action`` and prove the GUI thread kept serving events meanwhile."""

    started = time.monotonic()
    action()
    returned = time.monotonic() - started
    assert control.entered.wait(timeout=10), "the held cleanup was never entered"
    observed = {}
    QtCore.QTimer.singleShot(
        0, lambda: observed.setdefault("fired_while_cleanup_held", control.holding)
    )
    deadline = time.monotonic() + 5
    while "fired_while_cleanup_held" not in observed and time.monotonic() < deadline:
        _pump_native_events(application, 10)
    assert observed.get("fired_while_cleanup_held") is True, observed
    assert returned < 0.25, f"the click handler blocked for {returned:.3f}s"
    assert window.isVisible()


@pytest.mark.skipif(not _NATIVE_BOARD, reason=_NATIVE_BOARD_SKIP)
@pytest.mark.parametrize("action", ["save", "retry", "close"])
def test_required_native_lifecycle_action_keeps_the_window_responsive_while_cleanup_is_held(
    action,
):
    _assert_native_host_has_no_offscreen_platform()
    control = _HeldCleanup()
    with _native_temp_home() as root:
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        application = _native_application()
        with (
            _home_patches(home)[0],
            patch.object(desktop, "ApplicationRuntime", _held_cleanup_runtime_type(control)),
        ):
            state.setup(first)
            paths = state.state_paths()
            first_bytes = paths.config_file.read_bytes()
            session = desktop.DesktopSession()
            session.start_runtime()
            window = desktop._build_window(_native_qt(), session)
            window.show()
            _pump_native_events(application)
            released = None
            try:
                if action == "retry":
                    # Reach the blocked workspace change without holding cleanup.
                    window._change.click()
                    window._root.setText(str(second))
                    window._save.click()
                    _wait_for_window_operation(application, window)
                    assert session.snapshot.status == "switch_blocked"
                    assert window._retry.text() == "Retry"
                    assert window._retry.isEnabled()
                    trigger = window._retry.click
                elif action == "save":
                    window._change.click()
                    window._root.setText(str(second))
                    trigger = window._save.click
                else:
                    trigger = window.close

                control.arm(complete=False)
                _assert_responsive_while_cleanup_held(application, window, control, trigger)
                control.release.set()
                _wait_for_window_operation(application, window)

                assert window.isVisible()
                assert session.claims.held
                assert control.started_roots == [first.resolve()]
                assert paths.config_file.read_bytes() == first_bytes
                if action == "close":
                    assert session.snapshot.status == "shutdown_blocked"
                    assert window._status.text() == (
                        "Nyx workers are still stopping; retry Close"
                    )
                    assert window._retry.text() == "Close"
                    assert window._retry.isEnabled()

                    control.arm(complete=True)
                    _assert_responsive_while_cleanup_held(
                        application, window, control, window.close
                    )
                    control.release.set()
                    _wait_for_window_operation(application, window)
                    assert not window.isVisible()
                    assert not session.claims.held
                    assert session.runtime is None
                else:
                    assert session.snapshot.status == "switch_blocked"
                    assert window._retry.text() == "Retry"
                    assert window._retry.isEnabled()
            finally:
                control.disarm()
                released = _destroy_native_window(
                    application,
                    window,
                    store_path=session.paths.state_directory
                    / desktop.PRESENTATION_DIRECTORY,
                )
                session.close()
            assert not session.claims.held
            assert released


@contextlib.contextmanager
def _native_scripted_session(control, *, setup=None, gates=()):
    """A native window over a scripted runtime for workspace A, torn down after.

    Every gate is released before teardown, which waits for the window's
    operation, so a failed check never waits out a held call.
    """

    with _native_temp_home() as root:
        home = _home(root)
        first = _workspace(root, "first")
        second = _workspace(root, "second")
        application = _native_application()
        with (
            _home_patches(home)[0],
            patch.object(desktop, "ApplicationRuntime", control.runtime_type()),
        ):
            state.setup(first)
            paths = state.state_paths()
            if setup is not None:
                setup(first.resolve(), second.resolve())
            session = desktop.DesktopSession()
            with contextlib.suppress(desktop.DesktopUnavailableError):
                session.start_runtime()
            context = SimpleNamespace(
                application=application,
                session=session,
                first=first.resolve(),
                second=second.resolve(),
                paths=paths,
                first_bytes=paths.config_file.read_bytes(),
                window=None,
                recorder=None,
            )
            released = None
            try:
                yield context
            finally:
                for gate in gates:
                    gate.release.set()
                if context.window is not None:
                    released = _destroy_native_window(
                        application,
                        context.window,
                        store_path=session.paths.state_directory
                        / desktop.PRESENTATION_DIRECTORY,
                    )
                session.close()
            assert not session.claims.held
            assert released in (None, True)


def _show_native_window(context, *, record=False):
    if record:
        context.recorder = _SessionAccessRecorder(context.session)
    context.window = desktop._build_window(_native_qt(), context.session)
    context.window.show()
    _pump_native_events(context.application)
    return context.window


def _pump_until(application, condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        _pump_native_events(application, 10)


def _assert_window_closed_with_claims_released(context):
    assert not context.window.isVisible()
    assert not context.session.claims.held
    assert context.session.runtime is None


@pytest.mark.skipif(not _NATIVE_BOARD, reason=_NATIVE_BOARD_SKIP)
@pytest.mark.parametrize("case", _HELD_PROGRESS_CASES)
def test_required_native_held_operation_shows_its_progress_and_reaches_only_request_quit(case):
    _assert_native_host_has_no_offscreen_platform()
    control = _ScriptedLifecycle()
    gate = _Gate()

    def setup(first, _second):
        if case == "runtime_retry_then_quit":
            control.start_script[first] = [RuntimeError("injected startup failure"), gate]
        else:
            control.shutdown_script.append((gate, True))

    with _native_scripted_session(control, setup=setup, gates=(gate,)) as context:
        session = context.session
        window = _show_native_window(context, record=True)
        recorder = context.recorder
        gui = threading.current_thread()
        if case == "save":
            window._change.click()
            window._root.setText(str(context.second))
            trigger, progress = window._save.click, _SAVE_PROGRESS
        elif case == "board_failure_close":
            window._board_load_finished(False)
            assert window._status.text() == desktop.BOARD_FAILURE_MESSAGE
            assert window._retry.text() == "Close"
            trigger, progress = window._retry.click, _CLOSING_PROGRESS
        elif case == "close_then_quit":
            trigger, progress = window.close, _CLOSING_PROGRESS
        else:
            assert session.snapshot.status == "runtime_blocked"
            assert window._retry.text() == "Retry"
            trigger, progress = window._retry.click, _RETRY_PROGRESS
        label = window._retry.text()

        trigger()
        held = recorder.mark()
        assert gate.entered.wait(timeout=10), "the held operation was never entered"
        _pump_native_events(context.application)
        assert gate.holding
        assert _window_controls(window) == _progress_controls(progress, label)
        if case.endswith("_then_quit"):
            window._quit.click()
            _pump_native_events(context.application)
            assert gate.holding
            assert _window_controls(window) == _progress_controls(_CLOSING_PROGRESS, label)
            expected_names = ["request_quit"]
        else:
            expected_names = []
        # While the operation is held the GUI thread reached the session
        # only to record a Quit.
        assert recorder.names_from(held, gui) == expected_names

        completion = recorder.mark()
        gate.release.set()
        _wait_for_window_operation(context.application, window)
        # Every session access made by completion handling, including a
        # chained close's submission, came from the GUI thread.
        handled = recorder.since(completion)
        assert handled
        assert all(caller is gui for _, caller in handled), handled

        if case == "save":
            assert state.load_configuration() == state.Configuration(context.second)
            assert control.started_roots == [context.first, context.second]
            assert window._status.text() == f"Workspace: {context.second}"
            assert window.isVisible()
        else:
            _assert_window_closed_with_claims_released(context)
            if case == "runtime_retry_then_quit":
                assert control.started_roots == [context.first]
                assert control.shutdown_roots == [context.first]


@pytest.mark.skipif(not _NATIVE_BOARD, reason=_NATIVE_BOARD_SKIP)
@pytest.mark.parametrize("point", ["before_commit", "after_commit"])
def test_required_native_quit_during_a_held_workspace_change_follows_the_commit(point):
    _assert_native_host_has_no_offscreen_platform()
    control = _ScriptedLifecycle()
    gate = _Gate()

    def setup(_first, second):
        if point == "before_commit":
            control.shutdown_script.append((gate, True))
        else:
            control.start_script[second] = [gate]

    with _native_scripted_session(control, setup=setup, gates=(gate,)) as context:
        window = _show_native_window(context)
        window._change.click()
        window._root.setText(str(context.second))
        window._save.click()
        assert gate.entered.wait(timeout=10), "the held operation was never entered"
        _pump_native_events(context.application)
        assert window._status.text() == _SAVE_PROGRESS

        window._quit.click()
        _pump_native_events(context.application)
        assert gate.holding
        assert window._status.text() == _CLOSING_PROGRESS
        assert window.isVisible()

        gate.release.set()
        _wait_for_window_operation(context.application, window)

        _assert_window_closed_with_claims_released(context)
        if point == "before_commit":
            assert context.paths.config_file.read_bytes() == context.first_bytes
            assert control.started_roots == [context.first]
            assert control.shutdown_roots == [context.first]
        else:
            saved = json.loads(context.paths.config_file.read_text(encoding="utf-8"))
            assert saved["specification_root"] == str(context.second)
            assert control.started_roots == [context.first, context.second]
            assert control.shutdown_roots == [context.first, context.second]


@pytest.mark.skipif(not _NATIVE_BOARD, reason=_NATIVE_BOARD_SKIP)
def test_required_native_quit_during_a_stop_that_times_out_chains_one_responsive_close():
    _assert_native_host_has_no_offscreen_platform()
    control = _ScriptedLifecycle()
    stop_gate = _Gate()
    chained_gate = _Gate()
    control.shutdown_script.extend([(stop_gate, False), (chained_gate, True)])
    with _native_scripted_session(control, gates=(stop_gate, chained_gate)) as context:
        application = context.application
        window = _show_native_window(context)
        window._change.click()
        window._root.setText(str(context.second))
        window._save.click()
        assert stop_gate.entered.wait(timeout=10), "the held stop was never entered"
        window._quit.click()
        _pump_native_events(application)
        assert window._status.text() == _CLOSING_PROGRESS

        submitted = []
        handler_returned = []
        real_submit = window._submit

        def recorded_submit(kind, call):
            started = time.monotonic()
            submitted.append(kind)
            real_submit(kind, call)
            # Runs once the submitting completion handler has returned.
            QtCore.QTimer.singleShot(
                0, lambda: handler_returned.append(time.monotonic() - started)
            )

        with patch.object(window, "_submit", side_effect=recorded_submit):
            # The stop reports that it timed out, so the change could not
            # close the session; the recorded Quit chains one close.
            stop_gate.release.set()
            _pump_until(
                application, lambda: chained_gate.entered.is_set() and handler_returned
            )
            assert submitted == ["close"]
            assert chained_gate.entered.is_set(), "the chained close never started"
            assert handler_returned, "the submitting completion handler never returned"
            assert handler_returned[0] < 0.25, (
                f"the completion handler blocked for {handler_returned[0]:.3f}s"
            )
            observed = {}
            QtCore.QTimer.singleShot(
                0,
                lambda: observed.setdefault(
                    "fired_while_cleanup_held", chained_gate.holding
                ),
            )
            _pump_until(application, lambda: "fired_while_cleanup_held" in observed)
            assert observed.get("fired_while_cleanup_held") is True, observed
            assert window.isVisible()
            assert window._status.text() == _CLOSING_PROGRESS
            assert context.paths.config_file.read_bytes() == context.first_bytes

            chained_gate.release.set()
            _wait_for_window_operation(application, window)

        assert submitted == ["close"]
        _assert_window_closed_with_claims_released(context)
        assert context.paths.config_file.read_bytes() == context.first_bytes
        assert control.started_roots == [context.first]
        assert control.shutdown_roots == [context.first]


@pytest.mark.skipif(not _NATIVE_BOARD, reason=_NATIVE_BOARD_SKIP)
def test_required_native_teardown_during_a_held_operation_submits_no_recorded_quit_close():
    _assert_native_host_has_no_offscreen_platform()
    control = _ScriptedLifecycle()
    gate = _Gate()
    control.shutdown_script.append((gate, False))
    with _native_scripted_session(control, gates=(gate,)) as context:
        application = context.application
        session = context.session
        window = _show_native_window(context)
        window._quit.click()
        assert gate.entered.wait(timeout=10), "the held close was never entered"
        window._quit.click()
        _pump_native_events(application)
        assert window._status.text() == _CLOSING_PROGRESS

        after_teardown = []
        teardown = threading.Event()
        real_submit = window._submit

        def recorded_submit(kind, call):
            if teardown.is_set():
                after_teardown.append(kind)
            real_submit(kind, call)

        with patch.object(window, "_submit", side_effect=recorded_submit):
            # Teardown waits on the held close, which then stays blocked;
            # its completion was posted before the relay was disconnected.
            releaser = threading.Timer(0.3, gate.release.set)
            releaser.start()
            teardown.set()
            desktop._release_presentation(application, window)
            releaser.join(timeout=10)
            for _ in range(10):
                _pump_native_events(application, 20)
            assert after_teardown == []

        assert not window.isVisible()
        assert session.snapshot.status == "shutdown_blocked"
        assert session.claims.held
        assert session.runtime is not None


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
            configuration = state.setup(workspace)
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
        assert catalog["schema_version"] == SCHEMA_VERSION
        assert catalog["configuration_revision"] == configuration.revision
        assert manager.active_count == 0
        legacy = dict(catalog, schema_version=4)
        legacy["catalog_digest"] = canonical_digest(legacy)
        with pytest.raises(ValueError):
            parse_catalog(legacy)


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
