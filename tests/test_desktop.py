"""Behavioral checks for the claim-held desktop chooser slice."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nyx import _native_claim, desktop, runtime, state
from nyx._native_claim import NativeClaim

_NATIVE_REQUIRED = os.environ.get("NYX_REQUIRE_NATIVE_DESKTOP") == "1"
try:
    from PySide6 import QtCore, QtTest, QtWidgets
except ImportError as _qt_error:  # Linux source collection remains dependency-light.
    QtCore = None
    QtTest = None
    QtWidgets = None
    if _NATIVE_REQUIRED:
        pytest.fail(
            f"required native desktop dependency is unavailable: {_qt_error}",
            allow_module_level=True,
        )


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


def _fake_qt():
    return {
        "QtCore": SimpleNamespace(),
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
    }


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
            assert paths.config_file.read_bytes() == committed


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

    window = SimpleNamespace(show=lambda: calls.append("show"))
    with (
        patch.object(desktop, "DesktopSession", Session),
        patch.object(desktop, "_load_qt", return_value={"QtWidgets": Widgets}),
        patch.object(desktop, "_build_window", return_value=window),
    ):
        assert desktop.main([]) == 0

    assert calls == ["start_runtime", "show", "close"]


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
                    created.append(self)

                def start(self, **_kwargs):
                    raise RuntimeError("injected startup failure")

                def cleanup_start_failure(self, _deadline):
                    return False

                def shutdown(self, _deadline):
                    return True

            with patch.object(desktop, "ApplicationRuntime", RuntimeWithUnfinishedCleanup):
                session = desktop.DesktopSession()
                with pytest.raises(desktop.DesktopUnavailableError):
                    session.start_runtime(static_ready=lambda: True)

                assert created
                assert session.runtime is created[0]
                assert session.claims.held
                session.close()


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
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
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
            window = desktop._build_window({"QtCore": QtCore, "QtWidgets": QtWidgets}, session)
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
            if owns_application:
                application.quit()
