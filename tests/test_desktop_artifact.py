"""Proof for the delivered standalone desktop artifact.

The checks launch the staged application and its bundled console helper as
black boxes: the environment removes checkout and interpreter discovery, and
observations come from the process exit, the launched process's visible
window, the fixed loopback board, and the account claim files rather than from
source imports inside the application.
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
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import native_ui
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


# The window enumerators and their dispatcher live in the shared native driver
# module so one owner answers "which windows does process X show".
_visible_top_level_windows = native_ui.visible_top_level_windows


def _wait_for_window(gui: _RunningGui, timeout: float = _WINDOW_TIMEOUT) -> None:
    """Require a visible top-level window owned by the launched process.

    The application decides whether to start its board before it builds and
    shows its window, so once the window is visible an absent listener means
    the application declined to start rather than that it has not started yet.
    """

    deadline = time.monotonic() + timeout
    owners: list[native_ui.WindowInfo] = []
    while True:
        exit_code = gui.process.poll()
        if exit_code is not None:
            raise AssertionError(
                f"the application exited with {exit_code!r} before showing a window; "
                f"application log:\n{gui.diagnostics()[-2000:]}"
            )
        owners = _visible_top_level_windows()
        if any(window.owner == gui.process.pid for window in owners):
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    observed = ", ".join(
        f"{owner}:{label!r}"
        for owner, label in sorted({(window.owner, window.title) for window in owners})
    )
    raise AssertionError(
        f"no visible top-level window owned by pid {gui.process.pid} within {timeout:g}s; "
        f"observed window owners: [{observed}]; "
        f"application log:\n{gui.diagnostics()[-2000:]}"
    )


@contextlib.contextmanager
def _running_gui(
    artifact: ArtifactLayout,
    root: Path,
    environment: dict[str, str],
    extra_environment: dict[str, str] | None = None,
    job_observer: native_ui.JobObserver | None = None,
):
    """Launch the staged application and yield it.

    ``extra_environment`` adds variables to this launch only.  A
    ``job_observer`` places the process in its job object immediately after
    launch, before the caller can issue a first request.
    """

    log = root / "application.log"
    handle = log.open("wb")
    launch_environment = {**environment, **(extra_environment or {})}
    process = subprocess.Popen(
        [str(artifact.executable)],
        cwd=str(root),
        env=launch_environment,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    running = _RunningGui(process, handle, log)
    try:
        if job_observer is not None:
            try:
                job_observer.attach(process)
            except native_ui.DriverError as error:
                pytest.fail(f"the application could not be placed in a job object: {error}")
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


def _find_dotnet_compiler() -> Path:
    """Locate the C# compiler shipped with Windows, so no download is needed."""

    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for framework in ("Framework64", "Framework"):
        candidate = windows / "Microsoft.NET" / framework / "v4.0.30319" / "csc.exe"
        if candidate.is_file():
            return candidate
    pytest.fail("the .NET Framework C# compiler that ships with Windows was not found")


def _install_interpreter_decoys(decoy_bin: Path, marker: Path, scratch: Path) -> None:
    """Install decoy interpreters that record an invocation and exit nonzero.

    On Windows each decoy is a native executable image, because
    ``CreateProcess`` does not run a shebang script as ``python.exe``.
    """

    if os.name == "nt":
        source = scratch / "interpreter-decoy.cs"
        literal = str(marker).replace('"', '""')
        source.write_text(
            "using System.IO;\n"
            "public static class NyxInterpreterDecoy {\n"
            "    public static int Main(string[] arguments) {\n"
            f'        File.AppendAllText(@"{literal}", "invoked\\n");\n'
            "        return 9;\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
        python = decoy_bin / "python.exe"
        compiled = subprocess.run(
            [str(_find_dotnet_compiler()), "/nologo", "/target:exe", f"/out:{python}", str(source)],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if compiled.returncode != 0 or not python.is_file():
            pytest.fail(
                f"the native interpreter decoy did not compile ({compiled.returncode}): "
                f"{compiled.stdout[-1500:]!r} {compiled.stderr[-1500:]!r}"
            )
        shutil.copyfile(python, decoy_bin / "python3.exe")
        return
    for name in ("python", "python3"):
        interpreter = decoy_bin / name
        interpreter.write_text(f"#!/bin/sh\ntouch {marker}\nexit 127\n", encoding="utf-8")
        interpreter.chmod(0o755)


def _prove_decoys_fire(environment: dict[str, str], marker: Path, root: Path) -> None:
    """Run each interpreter name through the sanitized PATH and require the marker.

    The control uses the platform shell, which resolves the name through the
    environment's own ``PATH`` the way the application's lookup would.
    """

    shell = (
        [os.environ.get("COMSPEC", "cmd.exe"), "/c"]
        if os.name == "nt"
        else ["/bin/sh", "-c"]
    )
    for name in ("python", "python3"):
        command = shell + ([name, "-c", "pass"] if os.name == "nt" else [f"{name} -c pass"])
        control = subprocess.run(
            command, env=environment, cwd=str(root), capture_output=True, timeout=60, check=False
        )
        if not marker.exists():
            pytest.fail(
                f"the interpreter decoy never fired for {name!r} through the sanitized PATH "
                f"(exit {control.returncode}): {control.stdout!r} {control.stderr!r}"
            )
        if control.returncode == 0:
            pytest.fail(f"the interpreter decoy for {name!r} did not exit nonzero")
        marker.unlink()


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
        _install_interpreter_decoys(decoy_bin, marker, root)
        environment = _sanitized_environment(home, decoy)
        environment["PATH"] = os.pathsep.join([str(decoy_bin), environment["PATH"]])
        _prove_decoys_fire(environment, marker, root)
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
                                window.title
                                for window in _visible_top_level_windows()
                                if window.owner == second.process.pid
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


# ---------------------------------------------------------------------------
# Windows console-window observation (U1)
# ---------------------------------------------------------------------------


class ConsoleWindowDefect(Exception):
    """A console-class window attributed to the application or a descendant (U1)."""


_CONSOLE_CONTROL_SOURCE = (
    "import ctypes, sys, time\n"
    "kernel32 = ctypes.windll.kernel32\n"
    "kernel32.GetConsoleWindow.restype = ctypes.c_void_p\n"
    "open(sys.argv[1], 'w').write(str(kernel32.GetConsoleWindow() or 0))\n"
    "time.sleep(3.0)\n"
)


def _describe_sample(sample: native_ui.WindowSample) -> str:
    return (
        f"class={sample.window_class!r} hwnd={sample.handle:#x} owner_pid={sample.owner_pid} "
        f"owner_image={sample.owner_image!r} owner_parent={sample.owner_parent} "
        f"title={sample.title!r} source={sample.source}"
    )


def _run_console_control(
    sampler: native_ui.WindowShowSampler, root: Path
) -> tuple[set[str], set[int]]:
    """Start a console-subsystem child in a new console and require the sampler to see it.

    A hosted terminal owns the console window in a process tree unrelated to the
    child, so attribution combines structural relations (window owner, owner's
    parent, a console host's attached clients) with first appearance during the
    child's lifetime of a window that did not exist before the run.  Return the
    relations the control exhibited, so the application is judged by the same
    relations, and the handles the control caused.
    """

    hwnd_file = root / "control-console-window"
    started = time.monotonic()
    control = subprocess.Popen(
        [sys.executable, "-c", _CONSOLE_CONTROL_SOURCE, str(hwnd_file)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    try:
        control.wait(timeout=60)
    except subprocess.TimeoutExpired:
        control.kill()
        control.wait(timeout=30)
        pytest.fail("the console control child did not finish")
    ended = time.monotonic()
    time.sleep(1.0)
    try:
        control_hwnd = int(hwnd_file.read_text(encoding="utf-8") or "0")
    except (OSError, ValueError):
        control_hwnd = 0
    samples = sampler.snapshot()
    shown_at = dict(sampler.shown_at)
    attributed = native_ui.attribute_console_windows(
        samples, shown_at, sampler.baseline, {control.pid}, [(started, ended)]
    )
    relations = {relation for _, relation in attributed}
    control_handles = {sample.handle for sample, _ in attributed}
    during = [
        sample for sample in samples
        if sample.handle not in sampler.baseline and started - 0.25 <= sample.at <= ended + 1.5
    ]
    print(
        f"NYX-PROBE console-control pid={control.pid} console_hwnd={control_hwnd:#x} "
        f"relations={sorted(relations)} "
        f"attributed={[_describe_sample(sample) + ' via ' + rel for sample, rel in attributed]!r} "
        f"observed_by_handle={[_describe_sample(sample) for sample in samples if sample.handle == control_hwnd]!r} "
        f"new_windows_during_control={[_describe_sample(sample) for sample in during]!r} "
        f"baseline_count={len(sampler.baseline)} "
        f"shown_console_class={[_describe_sample(sample) for sample in samples if native_ui.is_console_window_class(sample.window_class) and sample.handle in shown_at]!r}"
    )
    if sampler.errors:
        pytest.fail(f"the window-show sampler reported errors: {sampler.errors!r}")
    if not relations:
        pytest.fail(
            "the positive control was not seen: no console-class window was attributed to the "
            f"control child (pid {control.pid}) by ownership, console-host clients or first "
            "appearance during its lifetime; the sampler cannot judge the application"
        )
    return relations, control_handles


@pytest.mark.skipif(
    os.name != "nt",
    reason="console-subsystem helper windows are a Windows-only observation",
)
def test_delivered_gui_and_helper_show_no_console_window(artifact: ArtifactLayout):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        environment = _sanitized_environment(home, decoy)
        with _workspace(root) as workspace:
            _, configuration = _write_configuration(home, workspace)
            expected = json.loads(_expected_catalog(configuration))
            observer = native_ui.JobObserver()
            try:
                with native_ui.WindowShowSampler() as sampler:
                    time.sleep(1.0)
                    sampler.mark_baseline()
                    relations, control_handles = _run_console_control(sampler, root)
                    with _running_gui(artifact, root, environment, job_observer=observer) as gui:
                        _, body, _ = _wait_for_board(time.monotonic() + _START_TIMEOUT, gui)
                        if json.loads(body) != expected:
                            pytest.fail("the delivered board did not serve the exact catalog")
                        statuses: list[int] = []
                        begin = time.monotonic()
                        for _ in range(6):
                            status, _, _ = _request("/api/catalog")
                            statuses.append(status)
                            time.sleep(0.3)
                        end = time.monotonic()
                        helpers = observer.started_between(begin, end + 0.5)
                        pid = gui.process.pid
                        exit_code = gui.close()
                        time.sleep(0.5)
                    samples = sampler.snapshot()
            finally:
                observer.close()
            family = {pid} | observer.descendant_pids()
            if sampler.errors:
                pytest.fail(f"the window-show sampler reported errors: {sampler.errors!r}")
            if statuses.count(200) < 3:
                pytest.fail(f"fewer than three catalog requests succeeded: {statuses!r}")
            if not helpers:
                pytest.fail(
                    "the job object reported no helper process during the catalog requests; "
                    f"job events: {observer.events!r}"
                )
            if exit_code != 0:
                pytest.fail(f"the application exited {exit_code!r} after the observation")
            console_samples = [
                sample for sample in samples
                if native_ui.is_console_window_class(sample.window_class)
            ]
            judged = [
                (sample, relation)
                for sample, relation in native_ui.attribute_console_windows(
                    samples,
                    dict(sampler.shown_at),
                    sampler.baseline,
                    family,
                    list(observer.lifetimes().values()),
                    allowed=relations,
                )
                if sample.handle not in control_handles
            ]
            attributed = [sample for sample, _ in judged]
            print(
                f"NYX-PROBE console-observation gui_pid={pid} helper_pids={sorted(helpers)} "
                f"control_relations={sorted(relations)} requests={statuses} "
                f"attributed={[_describe_sample(sample) + ' via ' + rel for sample, rel in judged]!r} "
                f"all_console_class_samples={[_describe_sample(s) for s in console_samples]!r}"
            )
            if attributed:
                raise ConsoleWindowDefect(
                    "console-class windows were attributed to the application or a descendant: "
                    + "; ".join(_describe_sample(sample) for sample in attributed)
                )
        _assert_claims_released(home)


# ---------------------------------------------------------------------------
# Shared OS-level driver consumers
# ---------------------------------------------------------------------------

_CDP_PAGE_PREFIX = f"http://{_HOST}/"
_HOST_LABEL = os.environ.get("NYX_EXPECTED_NATIVE_HOST", "unrecorded-host")

_CARD_PATHS_EXPRESSION = (
    "Array.from(document.querySelectorAll('.card[data-package-path]'))"
    ".map((card) => card.dataset.packagePath)"
)
_CARD_TITLES_EXPRESSION = (
    "Array.from(document.querySelectorAll('.card .card-title'))"
    ".map((title) => title.textContent)"
)


@contextlib.contextmanager
def _driver_step(what: str):
    """Report a driver or precondition failure as a hard failure."""

    try:
        yield
    except native_ui.DriverError as error:
        pytest.fail(f"{what}: {error}", pytrace=False)


def _authorized_driver(gui: _RunningGui):
    """Return the lane's driver after its authority check, failing hard otherwise."""

    with _driver_step("the native driver could not start"):
        driver = native_ui.create_driver()
    try:
        detail = driver.check_authority(gui.process.pid)
    except native_ui.DriverError as error:
        print(f"NYX-PROBE driver-authority host={_HOST_LABEL} backend={driver.name} "
              f"result=FAIL detail={error}")
        pytest.fail(
            f"the native driver was not granted its authority on {_HOST_LABEL}: {error}",
            pytrace=False,
        )
    print(f"NYX-PROBE driver-authority host={_HOST_LABEL} backend={driver.name} "
          f"result=PASS detail={detail}")
    return driver


def _cdp_environment() -> tuple[int, dict[str, str]]:
    port = native_ui.free_loopback_port(avoid=PORT)
    return port, {"QTWEBENGINE_REMOTE_DEBUGGING": str(port)}


def _saved_root(config_file: Path) -> str:
    return json.loads(config_file.read_text(encoding="utf-8"))["specification_root"]


def _catalog_pairs(catalog: dict) -> list[tuple[str, str]]:
    """Return sorted ``(package path, card title)`` pairs for a catalog payload."""

    return sorted(
        (entry["package_path"], entry["declared"]["title"] or entry["package_path"])
        for entry in catalog["entries"]
    )


def _wait_for_cards(page: native_ui.CdpPage, expected: int, timeout: float = 30.0) -> list[str]:
    deadline = time.monotonic() + timeout
    paths: list[str] = []
    while time.monotonic() < deadline:
        paths = page.evaluate(_CARD_PATHS_EXPRESSION) or []
        if len(paths) >= expected:
            return paths
        time.sleep(0.25)
    pytest.fail(f"the board rendered {len(paths)} cards, expected {expected}: {paths!r}")


def _locate_card(page: native_ui.CdpPage, target: str) -> dict:
    """Scroll ``target`` into view and pick a point that really hits that card.

    Rectangle, viewport, scroll offsets and a grid of hit tests are read in one
    evaluation after an instant scroll, so a smooth-scroll animation or a board
    re-render cannot move the card between measuring and testing it.  The probe
    line records the geometry; a card that no sampled point hits is a hard
    failure.
    """

    expression = (
        "(function (target) {"
        " const card = Array.from(document.querySelectorAll('.card[data-package-path]'))"
        "   .find((c) => c.dataset.packagePath === target);"
        " if (!card) return {error: 'the target card is not rendered'};"
        " card.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});"
        " const r = card.getBoundingClientRect();"
        " const root = document.documentElement;"
        " const describe = (e) => e ? e.tagName + (e.id ? '#' + e.id : '') +"
        "   (typeof e.className === 'string' && e.className.trim()"
        "     ? '.' + e.className.trim().split(/\\s+/).join('.') : '') : null;"
        " const fractions = [[.5, .5], [.5, .25], [.5, .75], [.25, .5], [.75, .5],"
        "   [.25, .25], [.75, .25], [.25, .75], [.75, .75]];"
        " const points = fractions.map(([fx, fy]) => {"
        "   const x = r.left + r.width * fx, y = r.top + r.height * fy;"
        "   const e = document.elementFromPoint(x, y);"
        "   const c = e && e.closest('.card[data-package-path]');"
        "   return {x: x, y: y, hit: c ? c.dataset.packagePath : null, top: describe(e)}; });"
        " const vv = window.visualViewport;"
        " const style = getComputedStyle(card);"
        " return {rect: [r.left, r.top, r.width, r.height],"
        "   inner: [window.innerWidth, window.innerHeight],"
        "   client: [root.clientWidth, root.clientHeight],"
        "   visual: vv ? [vv.offsetLeft, vv.offsetTop, vv.pageLeft, vv.pageTop, vv.scale] : null,"
        "   scroll: [window.scrollX, window.scrollY], ratio: window.devicePixelRatio,"
        "   style: [style.display, style.visibility, style.pointerEvents],"
        "   stack: document.elementsFromPoint(r.left + r.width / 2, r.top + r.height / 2)"
        "     .slice(0, 6).map(describe),"
        "   points: points}; })(" + json.dumps(target) + ")"
    )
    geometry = page.evaluate(expression)
    print(f"NYX-PROBE card-geometry target={target!r} geometry={json.dumps(geometry)}")
    if not isinstance(geometry, dict) or "error" in geometry:
        pytest.fail(f"the target card could not be measured: {geometry!r}")
    width, height = geometry["client"]
    for point in geometry["points"]:
        if point["hit"] == target and 0 <= point["x"] < width and 0 <= point["y"] < height:
            geometry["point"] = (point["x"], point["y"])
            return geometry
    pytest.fail(
        f"no sampled point of the card hits it: card {target!r}, rectangle {geometry['rect']}, "
        f"viewport {geometry['client']}, scroll {geometry['scroll']}, "
        f"stack at the centre {geometry['stack']}, points {geometry['points']}"
    )


def test_native_driver_authority_probe_records_the_lane_result(artifact: ArtifactLayout):
    """First-push probe: record whether this lane grants the driver its authority."""

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        with _workspace(root) as workspace:
            _write_configuration(home, workspace)
            with _running_gui(
                artifact, root, _sanitized_environment(home, decoy)
            ) as gui:
                _wait_for_board(time.monotonic() + _START_TIMEOUT, gui)
                _wait_for_window(gui)
                _authorized_driver(gui)
                assert gui.close() == 0, gui.diagnostics()
        _assert_claims_released(home)


class NativeClipboardWriteDefect(Exception):
    """A correctly delivered right-click whose write never reached the native clipboard (S2-N2)."""


def test_delivered_card_right_click_copies_path_to_native_clipboard(artifact: ArtifactLayout):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        cdp_port, cdp_environment = _cdp_environment()
        with _workspace(root) as workspace:
            config_file, configuration = _write_configuration(home, workspace)
            expected_catalog = json.loads(_expected_catalog(configuration))
            catalog_paths = {entry["package_path"] for entry in expected_catalog["entries"]}
            environment = _sanitized_environment(home, decoy)
            with _running_gui(
                artifact, root, environment, extra_environment=cdp_environment
            ) as gui:
                _, body, _ = _wait_for_board(time.monotonic() + _START_TIMEOUT, gui)
                if json.loads(body) != expected_catalog:
                    pytest.fail("the delivered board did not serve the exact catalog")
                _wait_for_window(gui)
                driver = _authorized_driver(gui)
                pid = gui.process.pid
                with _driver_step("the page could not be located or read"):
                    page = native_ui.CdpPage(cdp_port, _CDP_PAGE_PREFIX)
                try:
                    with _driver_step("the window or page could not be prepared"):
                        _wait_for_cards(page, len(catalog_paths))
                        driver.bring_forward(pid)
                        calibration = native_ui.calibrate_page(
                            driver, page, driver.window_frame(pid)
                        )
                        shown = [p for p in page.evaluate(_CARD_PATHS_EXPRESSION)
                                 if p in catalog_paths]
                        if not shown:
                            pytest.fail("no rendered card matches a catalog package path")
                        target = sorted(shown)[0]
                        page.evaluate(
                            "(() => { window.__nyxContext = [];"
                            " document.addEventListener('contextmenu', (event) => {"
                            "   const card = event.target.closest('.card[data-package-path]');"
                            "   window.__nyxContext.push(card ? card.dataset.packagePath : null);"
                            " }, true); return true; })()"
                        )
                        sentinel = f"NYX-SENTINEL-{uuid.uuid4().hex}"
                        driver.set_clipboard(sentinel)
                        if driver.get_clipboard() != sentinel:
                            pytest.fail("the native clipboard did not accept the sentinel")
                        driver.bring_forward(pid)
                        # Measure last, immediately before the click, so nothing
                        # that happened while the window came forward can move the card.
                        geometry = _locate_card(page, target)
                        css_x, css_y = geometry["point"]
                        screen_x, screen_y = calibration.to_screen(css_x, css_y)
                        print(
                            f"NYX-PROBE click-point css=({css_x:.1f},{css_y:.1f}) "
                            f"screen=({screen_x:.1f},{screen_y:.1f}) "
                            f"scale={calibration.scale:.3f} origin=({calibration.origin_x:.1f},"
                            f"{calibration.origin_y:.1f}) moves={calibration.moves!r}"
                        )
                        driver.click(screen_x, screen_y, "right")
                    with _driver_step("the click could not be confirmed"):
                        deadline = time.monotonic() + 10
                        reached: list = []
                        while time.monotonic() < deadline:
                            reached = page.evaluate("window.__nyxContext") or []
                            if reached:
                                break
                            time.sleep(0.2)
                        if reached != [target]:
                            pytest.fail(
                                f"the right-click did not reach the target card {target!r}; "
                                f"context-menu events reached {reached!r}"
                            )
                    expected_path = os.path.join(_saved_root(config_file), *target.split("/"))
                    with _driver_step("the native clipboard could not be read"):
                        deadline = time.monotonic() + 10
                        copied = sentinel
                        while time.monotonic() < deadline:
                            copied = driver.get_clipboard()
                            if copied != sentinel:
                                break
                            time.sleep(0.25)
                        status_text = page.evaluate(
                            "(() => { const s = document.querySelector('.copy-status');"
                            " return s ? s.textContent : null; })()"
                        )
                    if copied == sentinel:
                        raise NativeClipboardWriteDefect(
                            f"the context-menu event reached card {target!r} but the native "
                            f"clipboard still holds the sentinel; page copy status: {status_text!r}"
                        )
                    assert copied == expected_path
                finally:
                    page.close()
                assert gui.close() == 0, gui.diagnostics()
        _assert_claims_released(home)


def test_delivered_change_workspace_save_switches_to_second_workspace(artifact: ArtifactLayout):
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
        cdp_port, cdp_environment = _cdp_environment()
        config_file, first_configuration = _write_configuration(home, first)
        second_root = str(second.resolve())
        expected_first = json.loads(_expected_catalog(first_configuration))
        expected_second = json.loads(
            _expected_catalog(state.Configuration(second.resolve()))
        )
        pairs_first, pairs_second = _catalog_pairs(expected_first), _catalog_pairs(expected_second)
        if pairs_first == pairs_second:
            pytest.fail("the two workspaces must present different cards")
        titles_first = sorted(title for _, title in pairs_first)
        titles_second = sorted(title for _, title in pairs_second)
        environment = _sanitized_environment(home, decoy)

        def page_titles() -> list[str] | None:
            try:
                with native_ui.CdpPage(cdp_port, _CDP_PAGE_PREFIX, timeout=5) as page:
                    return sorted(page.evaluate(_CARD_TITLES_EXPRESSION) or [])
            except (native_ui.CdpError, OSError):
                return None

        with _running_gui(
            artifact, root, environment, extra_environment=cdp_environment
        ) as gui:
            _, body, _ = _wait_for_board(time.monotonic() + _START_TIMEOUT, gui)
            if _catalog_pairs(json.loads(body)) != pairs_first:
                pytest.fail("the delivered board did not start on the first workspace")
            _wait_for_window(gui)
            driver = _authorized_driver(gui)
            pid = gui.process.pid
            with _driver_step("the first workspace's cards could not be read"):
                with native_ui.CdpPage(cdp_port, _CDP_PAGE_PREFIX) as page:
                    _wait_for_cards(page, len(pairs_first))
                    if sorted(page.evaluate(_CARD_TITLES_EXPRESSION)) != titles_first:
                        pytest.fail("the page's card titles do not match the first catalog")
            with _driver_step("the Change workspace flow could not be driven"):
                driver.bring_forward(pid)
                change = driver.find_widget(pid, "button", "Change workspace")
                if not change.enabled:
                    pytest.fail("Change workspace is disabled")
                driver.click(*change.rect.center)
                deadline = time.monotonic() + 15
                field = None
                while time.monotonic() < deadline:
                    try:
                        candidate = driver.find_widget(pid, "edit", "Workspace root")
                    except native_ui.DriverError:
                        candidate = None
                    if candidate is not None and candidate.enabled:
                        field = candidate
                        break
                    time.sleep(0.25)
                if field is None:
                    pytest.fail("the Workspace root field never became available")
                driver.replace_text(pid, "Workspace root", second_root)
                deadline = time.monotonic() + 10
                read_back = None
                while time.monotonic() < deadline:
                    read_back = driver.find_widget(pid, "edit", "Workspace root").value
                    if read_back == second_root:
                        break
                    time.sleep(0.25)
                if read_back != second_root:
                    pytest.fail(
                        f"the Workspace root field reads {read_back!r}, expected {second_root!r}"
                    )
                save = driver.find_widget(pid, "button", "Save workspace")
                if not save.enabled:
                    pytest.fail("Save workspace is disabled")
                driver.click(*save.rect.center)
            deadline = time.monotonic() + 60
            catalog_pairs: object = None
            titles: object = None
            saved: object = None
            while time.monotonic() < deadline:
                try:
                    status, served, _ = _request("/api/catalog")
                    catalog_pairs = _catalog_pairs(json.loads(served)) if status == 200 else None
                except OSError:
                    catalog_pairs = None
                titles = page_titles()
                saved = _saved_root(config_file)
                if (
                    catalog_pairs == pairs_second
                    and titles == titles_second
                    and saved == second_root
                ):
                    break
                time.sleep(0.5)
            assert saved == second_root, f"saved configuration root is {saved!r}"
            assert catalog_pairs == pairs_second, f"HTTP catalog cards are {catalog_pairs!r}"
            assert titles == titles_second, f"page card titles are {titles!r}"
            assert gui.process.poll() is None, gui.diagnostics()
            assert gui.process.pid == pid
            assert gui.close() == 0, gui.diagnostics()
        _assert_claims_released(home)


# ---------------------------------------------------------------------------
# Relocated delivered copy
# ---------------------------------------------------------------------------


def _symlinks(root: Path) -> set[str]:
    found: set[str] = set()
    for directory, names, files in os.walk(root, followlinks=False):
        for name in names + files:
            path = Path(directory) / name
            if path.is_symlink():
                found.add(str(path.relative_to(root)))
    return found


@contextlib.contextmanager
def _moved_aside(paths: list[Path]):
    """Move each existing tree aside by rename and restore them all afterwards."""

    moved: list[tuple[Path, Path]] = []
    try:
        for path in paths:
            if not (path.exists() or path.is_symlink()):
                continue
            aside = path.with_name(f"{path.name}.nyx-aside-{uuid.uuid4().hex[:8]}")
            path.rename(aside)
            moved.append((path, aside))
        yield [path for path, _ in moved]
    finally:
        failures: list[str] = []
        for path, aside in reversed(moved):
            try:
                aside.rename(path)
            except OSError as error:
                failures.append(f"{aside} -> {path}: {error}")
        if failures:
            raise RuntimeError("a moved-aside tree could not be restored: " + "; ".join(failures))


_LSREGISTER = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)


def test_delivered_relocated_copy_runs_without_the_build_tree(artifact: ArtifactLayout):
    build_trees = [
        _SOURCE_ROOT / "dist" / "desktop",
        _SOURCE_ROOT / "build" / "desktop",
        _SOURCE_ROOT / ".venv",
    ]
    if not build_trees[2].exists() and _ARTIFACT_REQUIRED:
        pytest.fail(
            f"the documented build environment {build_trees[2]} is absent in the artifact lane"
        )
    bundle = artifact.artifact.suffix == ".app" or artifact.artifact.is_dir()
    staged = artifact.artifact if bundle else artifact.application_root
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        decoy = _inert_path(root)
        copy_root = root / "relocated"
        copy_root.mkdir()
        copied = copy_root / staged.name
        shutil.copytree(staged, copied, symlinks=True)
        if _symlinks(copied) != _symlinks(staged):
            pytest.fail("the copy did not preserve the staged bundle's symlinks")
        relocated = ArtifactLayout(copied if bundle else copied / artifact.executable.name)
        for required in (relocated.executable, relocated.helper, relocated.resource):
            if not required.is_file():
                pytest.fail(f"the copy lacks {required}")
        process_id: int | None = None
        try:
            with _moved_aside(build_trees):
                with _workspace(root) as workspace:
                    _, configuration = _write_configuration(home, workspace)
                    expected = json.loads(_expected_catalog(configuration))
                    environment = _sanitized_environment(home, decoy)
                    with _running_gui(relocated, root, environment) as gui:
                        process_id = gui.process.pid
                        try:
                            _, body, _ = _wait_for_board(time.monotonic() + _START_TIMEOUT, gui)
                            with _driver_step("the running image path could not be read"):
                                image = native_ui.process_image_path(gui.process.pid)
                            if os.path.normcase(os.path.realpath(image)) != os.path.normcase(
                                os.path.realpath(relocated.executable)
                            ):
                                pytest.fail(
                                    f"the application runs from {image!r}, "
                                    f"not from the copy {relocated.executable}"
                                )
                            assert json.loads(body) == expected
                            assert gui.process.poll() is None, gui.diagnostics()
                        finally:
                            if gui.process.poll() is None:
                                _request_close_by_pid(gui.process.pid)
                                try:
                                    code = gui.process.wait(timeout=30)
                                except subprocess.TimeoutExpired:
                                    gui.process.kill()
                                    code = gui.process.wait(timeout=30)
                            else:
                                code = gui.process.returncode
                        assert code == 0, gui.diagnostics()
                    _wait_port_free(time.monotonic() + 15)
                    _assert_claims_released(home)
        finally:
            if sys.platform == "darwin":
                unregister = subprocess.run(
                    [_LSREGISTER, "-u", str(copied)],
                    capture_output=True, text=True, timeout=60, check=False,
                )
                if unregister.returncode != 0:
                    raise RuntimeError(
                        f"lsregister -u {copied} failed ({unregister.returncode}): "
                        f"{unregister.stderr!r}; relocated pid {process_id}"
                    )
