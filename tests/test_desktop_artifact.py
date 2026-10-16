"""Proof for the delivered standalone desktop artifact.

The checks launch the staged application and its bundled console helper as
black boxes: the environment removes checkout and interpreter discovery, and
observations come from the process exit, the launched process's visible
window, the fixed loopback board, and the account claim files rather than from
source imports inside the application.
"""

from __future__ import annotations

import contextlib
import ctypes
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
from nyx.catalog import scan_catalog
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
# The application makes its only automatic start decision before it shows its
# window, so a shown window bounds every "nothing is listening" check.
_WINDOW_TIMEOUT = 30.0
_SOURCE_ROOT = Path(__file__).parents[1]


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
) -> tuple[Path, state.Configuration]:
    configuration = state.Configuration(workspace.resolve())
    config_directory = home / ".nyx" / "config"
    config_directory.mkdir(parents=True, exist_ok=True)
    config_file = config_directory / "config.json"
    encoded = (
        json.dumps(
            configuration.as_dict(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    config_file.write_bytes(encoded)
    return config_file, configuration


def _expected_catalog(configuration: state.Configuration) -> str:
    return scan_catalog(
        configuration.specification_root,
        completed_stage_names=configuration.completed_stage_names,
        configuration_revision=configuration.revision,
    )


def _write_raw_configuration(home: Path, payload: bytes) -> Path:
    config_directory = home / ".nyx" / "config"
    config_directory.mkdir(parents=True, exist_ok=True)
    config_file = config_directory / "config.json"
    config_file.write_bytes(payload)
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

    def close(self, timeout: float = 30.0, method: str = "quit") -> int:
        """Request the ordinary native close or quit, then require an exit.

        The window close and the explicit Quit share one visible, retryable
        shutdown.  Each platform exposes the triggers it actually offers: a
        window-manager close on Windows and either an application close or a
        quit on macOS.  Both must reach the same owned cleanup.
        """

        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(self.process.pid)],
                capture_output=True,
                check=False,
            )
        elif method == "close":
            subprocess.run(
                ["osascript", "-e", 'tell application "Nyx" to close window 1'],
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


def _windows_visible_top_level_windows() -> list[tuple[int, str]]:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    found: list[tuple[int, str]] = []

    def visit(window, _parameter):
        if user32.IsWindowVisible(window):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(window, title, len(title))
            found.append((owner.value, title.value))
        return True

    if not user32.EnumWindows(callback_type(visit), 0):
        raise OSError(ctypes.get_last_error(), "EnumWindows failed")
    return found


def _macos_visible_top_level_windows() -> list[tuple[int, str]]:
    graphics = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    foundation = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    graphics.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    graphics.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
    foundation.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    foundation.CFArrayGetCount.restype = ctypes.c_long
    foundation.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    foundation.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    foundation.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    foundation.CFDictionaryGetValue.restype = ctypes.c_void_p
    foundation.CFNumberGetValue.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int32),
    ]
    foundation.CFNumberGetValue.restype = ctypes.c_bool
    foundation.CFStringGetCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32,
    ]
    foundation.CFStringGetCString.restype = ctypes.c_bool
    foundation.CFRelease.argtypes = [ctypes.c_void_p]
    foundation.CFRelease.restype = None
    on_screen_only = 1 << 0
    exclude_desktop_elements = 1 << 4
    number_sint32 = 3
    utf8 = 0x08000100
    owner_pid_key = ctypes.c_void_p.in_dll(graphics, "kCGWindowOwnerPID").value
    owner_name_key = ctypes.c_void_p.in_dll(graphics, "kCGWindowOwnerName").value
    layer_key = ctypes.c_void_p.in_dll(graphics, "kCGWindowLayer").value

    def integer(info, key) -> int | None:
        value = foundation.CFDictionaryGetValue(info, key)
        number = ctypes.c_int32()
        if not value or not foundation.CFNumberGetValue(value, number_sint32, ctypes.byref(number)):
            return None
        return number.value

    def text(info, key) -> str:
        value = foundation.CFDictionaryGetValue(info, key)
        buffer = ctypes.create_string_buffer(512)
        if not value or not foundation.CFStringGetCString(value, buffer, len(buffer), utf8):
            return ""
        return buffer.value.decode("utf-8", errors="replace")

    windows = graphics.CGWindowListCopyWindowInfo(
        on_screen_only | exclude_desktop_elements, 0
    )
    if not windows:
        raise OSError("CGWindowListCopyWindowInfo returned no window list")
    found: list[tuple[int, str]] = []
    try:
        for index in range(foundation.CFArrayGetCount(windows)):
            info = foundation.CFArrayGetValueAtIndex(windows, index)
            owner = integer(info, owner_pid_key)
            # Layer 0 is the normal application window level; menu bar, dock
            # and overlay surfaces sit on other layers.
            if owner is None or integer(info, layer_key) != 0:
                continue
            found.append((owner, text(info, owner_name_key)))
    finally:
        foundation.CFRelease(windows)
    return found


def _visible_top_level_windows() -> list[tuple[int, str]]:
    """Return the owner process id and a label for each visible top-level window."""

    if os.name == "nt":
        return _windows_visible_top_level_windows()
    if sys.platform == "darwin":
        return _macos_visible_top_level_windows()
    raise AssertionError("window readiness is observable only on Windows and macOS")


def _wait_for_window(gui: _RunningGui, timeout: float = _WINDOW_TIMEOUT) -> None:
    """Require a visible top-level window owned by the launched process.

    The application decides whether to start its board before it builds and
    shows its window, so once the window is visible an absent listener means
    the application declined to start rather than that it has not started yet.
    """

    deadline = time.monotonic() + timeout
    owners: list[tuple[int, str]] = []
    while True:
        exit_code = gui.process.poll()
        if exit_code is not None:
            raise AssertionError(
                f"the application exited with {exit_code!r} before showing a window; "
                f"application log:\n{gui.diagnostics()[-2000:]}"
            )
        owners = _visible_top_level_windows()
        if any(owner == gui.process.pid for owner, _ in owners):
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    observed = ", ".join(f"{owner}:{label!r}" for owner, label in sorted(set(owners)))
    raise AssertionError(
        f"no visible top-level window owned by pid {gui.process.pid} within {timeout:g}s; "
        f"observed window owners: [{observed}]; "
        f"application log:\n{gui.diagnostics()[-2000:]}"
    )


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


def test_delivered_application_ignores_a_decoy_checkout_and_build_interpreter(
    artifact: ArtifactLayout,
):
    """The delivered shell must resolve no checkout and no installed interpreter."""

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        # A decoy checkout would abort the import if the delivered application
        # reached it instead of its bundled package.
        decoy = root / "decoy-checkout"
        (decoy / "nyx").mkdir(parents=True)
        (decoy / "nyx" / "__init__.py").write_text(
            "raise RuntimeError('the delivered application imported a checkout')\n",
            encoding="utf-8",
        )
        # A decoy interpreter records any build-time interpreter invocation.
        decoy_bin = root / "decoy-bin"
        decoy_bin.mkdir()
        marker = root / "decoy-interpreter-ran"
        for name in ("python", "python3"):
            interpreter = decoy_bin / name
            interpreter.write_text(
                f"#!/bin/sh\ntouch {marker}\nexit 127\n", encoding="utf-8"
            )
            interpreter.chmod(0o755)
        environment = _sanitized_environment(home, decoy)
        environment["PATH"] = os.pathsep.join([str(decoy_bin), environment["PATH"]])
        with _workspace(root) as workspace:
            _, configuration = _write_configuration(home, workspace)
            expected = json.loads(_expected_catalog(configuration))
            with _running_gui(artifact, root, environment) as gui:
                status, body, _ = _wait_for_board(time.monotonic() + _START_TIMEOUT, gui)
                assert status == 200
                assert json.loads(body) == expected
                assert gui.process.poll() is None, gui.diagnostics()
                gui.close()

    assert not marker.exists(), "the delivered application ran a build-time interpreter"
    assert not (decoy / "nyx" / "__pycache__").exists(), "the checkout was imported"


def test_delivered_board_serves_exact_catalog_and_bundled_resources(artifact: ArtifactLayout):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        with _workspace(root) as workspace:
            _, configuration = _write_configuration(home, workspace)
            expected = json.loads(_expected_catalog(configuration))
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


def test_delivered_application_rejects_a_workspace_inside_its_installation(
    artifact: ArtifactLayout,
):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        inside = artifact.resource.parent
        assert inside.is_dir(), f"a resource subtree is required inside {artifact.application_root}"
        _write_configuration(home, inside)
        environment = _sanitized_environment(home, decoy)
        with _running_gui(artifact, root, environment) as gui:
            _wait_for_window(gui)
            with pytest.raises(OSError):
                _request("/api/catalog")
            assert gui.process.poll() is None
            gui.close()
        _assert_claims_released(home)


def _request_close_by_pid(pid: int) -> None:
    """Ask exactly one process to close gracefully, addressed by its id.

    Two delivered instances share one application name, so a request by name
    could reach the wrong one.  Windows receives the window-close request
    ``taskkill /PID`` sends without ``/F``; macOS receives an application
    terminate request for the running application with that process id.
    """

    if os.name == "nt":
        command = ["taskkill", "/PID", str(pid)]
    else:
        script = (
            "ObjC.import('AppKit');"
            f"var target = $.NSRunningApplication.runningApplicationWithProcessIdentifier({pid});"
            f"if (!target || target.processIdentifier !== {pid}) "
            f"{{ throw new Error('no running application has pid {pid}'); }}"
            "if (!target.terminate) { throw new Error('the terminate request was refused'); }"
        )
        command = ["osascript", "-l", "JavaScript", "-e", script]
    request = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
    assert request.returncode == 0, (
        f"the close request for pid {pid} failed with {request.returncode}: "
        f"stdout={request.stdout!r} stderr={request.stderr!r}"
    )


def test_delivered_second_process_reports_already_open_without_competing_writes(
    artifact: ArtifactLayout,
):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        # The second launch keeps its own log so it cannot truncate the first
        # application's diagnostics.
        second_root = root / "second-launch"
        second_root.mkdir()
        with _workspace(root) as workspace:
            config_file, _ = _write_configuration(home, workspace)
            environment = _sanitized_environment(home, decoy)
            lease_path, recovery_path = _claim_paths(home)

            def assert_first_application_state_unchanged() -> None:
                # Only the held qualified application claim reports already
                # open, and the refused launch changes no state and starts no
                # competing work while the first application keeps serving.
                assert NativeClaim.probe(lease_path) == "held"
                assert NativeClaim.probe(recovery_path) == "held"
                assert config_file.read_bytes() == before
                assert (
                    sorted(path.name for path in lease_path.parent.iterdir())
                    == runtime_before
                )

            with _running_gui(artifact, root, environment) as gui:
                _wait_for_board(time.monotonic() + _START_TIMEOUT, gui)
                before = config_file.read_bytes()
                runtime_before = sorted(
                    path.name for path in lease_path.parent.iterdir()
                )
                with _running_gui(artifact, second_root, environment) as second:
                    try:
                        # The refusal is visible: the second process owns a
                        # top-level window before it exits.
                        _wait_for_window(second)
                        if os.name == "nt":
                            titles = [
                                title
                                for owner, title in _visible_top_level_windows()
                                if owner == second.process.pid
                            ]
                            assert "Nyx" in titles, (
                                f"second launch windows {titles!r}; "
                                f"application log:\n{second.diagnostics()[-2000:]}"
                            )
                        assert_first_application_state_unchanged()
                        _request_close_by_pid(second.process.pid)
                        try:
                            second_code = second.process.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            pytest.fail(
                                "the second launch did not exit after its close request; "
                                f"application log:\n{second.diagnostics()[-2000:]}"
                            )
                    finally:
                        # Cleanup after a failed proof only; a by-name close
                        # here could reach the first application.
                        if second.process.poll() is None:
                            second.process.kill()
                            second.process.wait(timeout=30)
                assert second_code == 1, second.diagnostics()
                assert_first_application_state_unchanged()
                assert gui.process.poll() is None, gui.diagnostics()
                status, _, _ = _wait_for_board(time.monotonic() + 30, gui)
                assert status == 200
                assert gui.close() == 0, gui.diagnostics()
            _assert_claims_released(home)


@pytest.mark.parametrize("kind", ["malformed", "unsupported", "unverified"])
def test_delivered_chooser_blocks_a_bad_persisted_state_without_changing_bytes(
    artifact: ArtifactLayout, kind: str
):
    """Every unusable persisted state stays visibly blocked and untouched."""

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        if kind == "malformed":
            preserved = _write_raw_configuration(home, b"{ this is not json\n")
        elif kind == "unsupported":
            preserved = _write_raw_configuration(
                home,
                json.dumps(
                    {
                        "schema_version": 99,
                        "specification_root": str(root),
                    }
                ).encode("utf-8"),
            )
        else:
            committed = root / "committed.json"
            committed.write_bytes(
                json.dumps(
                    {
                        "schema_version": state.CONFIG_SCHEMA_VERSION,
                        "specification_root": str(root),
                    }
                ).encode("utf-8")
            )
            config_directory = home / ".nyx" / "config"
            config_directory.mkdir(parents=True)
            (config_directory / "config.json").symlink_to(committed)
            preserved = committed
        before = preserved.read_bytes()
        environment = _sanitized_environment(home, decoy)
        with _running_gui(artifact, root, environment) as gui:
            _wait_for_window(gui)
            with pytest.raises(OSError):
                _request("/api/catalog")
            assert gui.process.poll() is None, gui.diagnostics()
            assert preserved.read_bytes() == before
            # The platform's ordinary close or quit reaches the same owned
            # cleanup, and the refused state is left byte-identical.
            assert gui.close() == 0, gui.diagnostics()
        _assert_claims_released(home)
        assert preserved.read_bytes() == before


def test_delivered_recovery_blocks_start_until_former_workers_exit(
    artifact: ArtifactLayout,
):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        with _workspace(root) as workspace:
            config_file, configuration = _write_configuration(home, workspace)
            before = config_file.read_bytes()
            expected = json.loads(_expected_catalog(configuration))
            environment = _sanitized_environment(home, decoy)
            lease_path, recovery_path = _claim_paths(home)
            recovery_path.parent.mkdir(parents=True, exist_ok=True)
            # A surviving former worker holds the recovery claim, so the
            # replacement may not start or change anything.
            former = NativeClaim(recovery_path)
            assert former.acquire(blocking=False)
            try:
                with _running_gui(artifact, root, environment) as gui:
                    _wait_for_window(gui)
                    with pytest.raises(OSError):
                        _request("/api/catalog")
                    assert gui.process.poll() is None, gui.diagnostics()
                    assert config_file.read_bytes() == before
                    assert gui.close() == 0, gui.diagnostics()
                released = NativeClaim(lease_path)
                assert released.acquire(blocking=False), "the application claim was retained"
                released.close()
            finally:
                former.close()
            _wait_port_free(time.monotonic() + 15)
            # Once the former workers are terminal, a later launch acquires
            # recovery and serves the same persisted selection.
            with _running_gui(artifact, root, environment) as gui:
                status, body, _ = _wait_for_board(time.monotonic() + _START_TIMEOUT, gui)
                assert status == 200
                assert json.loads(body) == expected
                assert config_file.read_bytes() == before
                assert gui.close() == 0, gui.diagnostics()
        _assert_claims_released(home)


def test_delivered_unverified_committed_state_revalidates_then_starts(
    artifact: ArtifactLayout,
):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        with _workspace(root) as workspace:
            valid, configuration = _write_configuration(home, workspace)
            committed = root / "committed.json"
            committed.write_bytes(valid.read_bytes())
            valid.unlink()
            valid.symlink_to(committed)
            before = committed.read_bytes()
            environment = _sanitized_environment(home, decoy)
            with _running_gui(artifact, root, environment) as gui:
                _wait_for_window(gui)
                with pytest.raises(OSError):
                    _request("/api/catalog")
                assert gui.process.poll() is None, gui.diagnostics()
                # The committed selection bytes stay untouched and are never
                # rolled back.
                assert committed.read_bytes() == before
                assert gui.close() == 0, gui.diagnostics()
            _wait_port_free(time.monotonic() + 15)
            # Only a safely revalidated record admits a start.
            valid.unlink()
            valid.write_bytes(before)
            expected = json.loads(_expected_catalog(configuration))
            with _running_gui(artifact, root, environment) as gui:
                status, body, _ = _wait_for_board(time.monotonic() + _START_TIMEOUT, gui)
                assert status == 200
                assert json.loads(body) == expected
                assert gui.close() == 0, gui.diagnostics()
        _assert_claims_released(home)


def test_delivered_restart_serves_the_newly_saved_workspace(artifact: ArtifactLayout):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        first = root / "first"
        second = root / "second"
        shutil.copytree(_SOURCE_ROOT / "examples" / "sample-specifications", first)
        shutil.copytree(_SOURCE_ROOT / "examples" / "sample-specifications", second)
        shutil.rmtree(second / "Trail_API")
        environment = _sanitized_environment(home, decoy)
        _, first_configuration = _write_configuration(home, first)
        expected_first = json.loads(_expected_catalog(first_configuration))
        with _running_gui(artifact, root, environment) as gui:
            _, body, _ = _wait_for_board(time.monotonic() + _START_TIMEOUT, gui)
            assert json.loads(body) == expected_first
            assert gui.close() == 0, gui.diagnostics()
        _assert_claims_released(home)
        _wait_port_free(time.monotonic() + 15)
        # The replacement selection is saved only through the canonical owner
        # and the next start serves that workspace's real catalog.
        _, second_configuration = _write_configuration(home, second)
        expected_second = json.loads(_expected_catalog(second_configuration))
        assert expected_first != expected_second
        with _running_gui(artifact, root, environment) as gui:
            _, body, _ = _wait_for_board(time.monotonic() + _START_TIMEOUT, gui)
            assert json.loads(body) == expected_second
            assert gui.close() == 0, gui.diagnostics()
        _assert_claims_released(home)


def test_delivered_chooser_state_starts_no_runtime_or_workers(artifact: ArtifactLayout):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        environment = _sanitized_environment(home, decoy)
        with _running_gui(artifact, root, environment) as gui:
            _wait_for_window(gui)
            with pytest.raises(OSError):
                _request("/api/catalog")
            assert gui.process.poll() is None
            gui.close()
        _assert_claims_released(home)


def test_window_readiness_fails_at_its_bound_for_a_process_without_a_window():
    """A launched process that never shows a window fails the readiness wait."""

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        log = root / "application.log"
        handle = log.open("wb")
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; print('headless child ready', flush=True); time.sleep(120)",
            ],
            cwd=str(root),
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        gui = _RunningGui(process, handle, log)
        try:
            deadline = time.monotonic() + 30
            while b"headless child ready" not in log.read_bytes():
                assert time.monotonic() < deadline, gui.diagnostics()
                assert process.poll() is None, gui.diagnostics()
                time.sleep(0.1)
            bound = 2.0
            started = time.monotonic()
            with pytest.raises(AssertionError) as failure:
                _wait_for_window(gui, timeout=bound)
            elapsed = time.monotonic() - started
            assert bound <= elapsed < bound + 15, elapsed
            assert process.poll() is None, "the child must still be running at the bound"
            message = str(failure.value)
            assert f"no visible top-level window owned by pid {process.pid}" in message
            assert "observed window owners: [" in message
            assert "headless child ready" in message
        finally:
            process.kill()
            process.wait(timeout=30)
            handle.close()


def test_delivered_worker_helper_emits_exact_utf8_catalog_bytes(artifact: ArtifactLayout):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        with _workspace(root) as workspace:
            _, configuration = _write_configuration(home, workspace)
            expected_text = _expected_catalog(configuration)
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
            _, configuration = _write_configuration(home, workspace)
            expected_text = _expected_catalog(configuration)
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
