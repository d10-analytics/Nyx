"""Exercise the published installation commands in their native shell.

Linux keeps the documented console installation and its browser/service
lifecycle.  Windows and macOS publish the private desktop artifact instead, so
the same consumer parses that guidance and, in the desktop artifact lane,
exercises the documented application actions against the staged build rather
than a detached console daemon.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

import pytest
from test_desktop_artifact import (
    _DELIVERED_HIDDEN_STAGES,
    ArtifactLayout,
    _assert_claims_released,
    _claim_paths,
    _inert_path,
    _request,
    _running_gui,
    _sanitized_environment,
    _wait_for_board,
    _wait_port_free,
    _workspace,
    _write_configuration,
)
from test_wheel_install import (
    _installed_environment,
    _native_claim_probe,
    _venv_executable,
)

from nyx._native_claim import NativeClaim
from nyx.catalog import scan_catalog

REPOSITORY_ROOT = Path(__file__).parents[1].resolve()
DOCUMENTS = ("README.md", "docs/running-nyx.md")
EXPECTED = {
    "bash": (
        "python3.12 -m venv .venv",
        ".venv/bin/python -m pip install .",
        ".venv/bin/nyx --setup examples/sample-specifications --show-all-stages",
        ".venv/bin/nyx",
        ".venv/bin/nyx --status",
        ".venv/bin/nyx --stop",
    ),
    "powershell": (
        "py -3.12 -m venv .venv",
        r".\.venv\Scripts\python.exe -m pip install '.[desktop,build]'",
        r".\.venv\Scripts\python.exe scripts\build_desktop.py",
    ),
}
# The documented private artifacts and the application actions the guide
# promises.  The desktop lane consumes these paths against the staged build.
DESKTOP_ARTIFACT_PATHS = {
    "windows": r"dist\desktop\Nyx\Nyx.exe",
    "macos": "dist/desktop/Nyx.app",
}
DESKTOP_ARTIFACT_MARKERS = (
    r"dist\desktop\Nyx\Nyx.exe",
    "dist/desktop/Nyx.app",
    "build_desktop.py",
)
DESKTOP_GUIDANCE_MARKERS = (
    "private internal feasibility build",
    "not a public release",
    "workspace chooser",
    "Change workspace",
    "Quit",
    "already open",
    "Retry",
)
STAGE_ORDER_GUIDANCE_MARKERS = {
    "README.md": (
        "Board row order",
        "account-local",
        "browser and desktop views",
        "workspace root",
        "presentation only",
        "does not rename or move",
    ),
    "docs/running-nyx.md": (
        "Board row order",
        "Move up",
        "Move down",
        "Cancel",
        "performs no write",
        "Reset",
        "canonical inventory order",
        "hidden or absent",
        "survives browser reload",
        "supported Nyx",
        "failed Save",
    ),
    "docs/workspaces.md": (
        "root-keyed order map",
        "browser and desktop views",
        "Cancel",
        "performs no write",
        "Reset",
        "Unlisted eligible stages",
        "hidden or absent",
        "If loading account settings fails, no saved order is available, "
        "so the board uses canonical inventory order",
        "reports the problem for retry",
        "If Save fails, the board keeps displaying the prior saved order "
        "and retains the unsaved editor draft",
        "never renames or moves",
    ),
}
COMPLETION_GUIDANCE_MARKERS = {
    "README.md": (
        "Completion Prerequisite: UUID",
        "literal set of stage names",
        "Board settings",
        "workspace root",
        "evidence verification",
        "Prerequisite: UUID | claim-name",
    ),
    "docs/running-nyx.md": (
        "collapsed **Board settings**",
        "Counts as finished",
        "**Save** to persist the row order and completion policy together",
        "**Cancel** drops both kinds of unsaved change",
        "**Reset** clears the current root's row order and completed-stage set",
        "hidden stages",
        "Reload board settings",
        "configuration revision",
        "moving it out reopens it",
        "recorded workflow assertion",
    ),
    "docs/workspaces.md": (
        "no implicit `Done` or `Archive` rule",
        "Completion Prerequisite: UUID",
        "missing policy leaves the requirement **unknown**",
        "saved per canonical workspace root",
        "root with no saved names reports unknown",
        "A cancelled or archived item",
        "hidden completed stage",
        "mixed dependent item",
    ),
    "docs/specification-reference.md": (
        "one exact completion grammar",
        "one-part `Prerequisite: UUID` is not",
        "never uses a magic claim name",
        "collapsed **Board settings**",
        "temporarily absent names",
        "recorded workflow assertion",
    ),
}
EVENT_COMPLETION_EXAMPLE = (
    "# Organize the neighborhood festival\n"
    "Package ID: 123e4567-e89b-42d3-a456-426614174100\n"
    "Status: planning\n"
    "Completion Prerequisite: 123e4567-e89b-42d3-a456-426614174102\n"
    "Prerequisite: 123e4567-e89b-42d3-a456-426614174102 | venue-confirmed\n"
    "Prerequisite: 123e4567-e89b-42d3-a456-426614174102 | permit-approved"
)
_START_TIMEOUT = 90.0


def _documented_commands(document: str, language: str) -> tuple[str, ...]:
    blocks = re.findall(rf"^```{language}\n(.*?)^```", document, re.MULTILINE | re.DOTALL)
    assert blocks, f"missing {language} installation block"
    commands = tuple(blocks[0].splitlines())
    assert commands == EXPECTED[language], f"changed {language} installation commands"
    return commands


@pytest.mark.parametrize("language", EXPECTED)
def test_both_documents_publish_exact_installation_commands(language):
    blocks = [
        _documented_commands((REPOSITORY_ROOT / name).read_text(encoding="utf-8"), language)
        for name in DOCUMENTS
    ]
    assert blocks[0] == blocks[1]


@pytest.mark.parametrize(
    ("language", "index"),
    [
        (language, index)
        for language, commands in EXPECTED.items()
        for index in range(len(commands))
    ],
)
def test_document_contract_rejects_changed_or_deleted_commands(language, index):
    commands = list(EXPECTED[language])
    for replacement in ("", "echo substituted command"):
        changed = commands.copy()
        changed[index] = replacement
        document = f"```{language}\n" + "\n".join(changed) + "\n```\n"
        with pytest.raises(AssertionError, match="changed"):
            _documented_commands(document, language)


def test_documents_publish_the_private_desktop_guidance():
    for name in DOCUMENTS:
        document = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
        for marker in DESKTOP_ARTIFACT_MARKERS:
            assert marker in document, (name, marker)
    guide = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    for marker in DESKTOP_GUIDANCE_MARKERS:
        assert marker in guide, marker


def test_documents_publish_the_saved_stage_order_contract():
    normalized_documents = {}
    for name, markers in STAGE_ORDER_GUIDANCE_MARKERS.items():
        document = re.sub(r"\s+", " ", (REPOSITORY_ROOT / name).read_text(encoding="utf-8"))
        normalized_documents[name] = document
        for marker in markers:
            assert marker in document, (name, marker)

    obsolete_fallback = (
        "If loading or saving account settings fails, the board falls back to "
        "canonical inventory order and keeps the last saved order unchanged."
    )
    assert obsolete_fallback not in normalized_documents["docs/workspaces.md"]


def test_documents_publish_the_explicit_completion_contract():
    for name, markers in COMPLETION_GUIDANCE_MARKERS.items():
        document = re.sub(r"\s+", " ", (REPOSITORY_ROOT / name).read_text(encoding="utf-8"))
        for marker in markers:
            assert marker in document, (name, marker)


def test_event_walkthrough_shows_whole_item_and_named_outcomes():
    document = (REPOSITORY_ROOT / "docs" / "workspaces.md").read_text(encoding="utf-8")
    assert EVENT_COMPLETION_EXAMPLE in document
    assert "event plan's whole-item requirement" in document
    assert "literal `Done` stage" in document
    assert "separate named outcomes" in document


def _run_command(command: str, *, root: Path, environment: dict[str, str]):
    if os.name == "nt":
        arguments = [
            "pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
            "$ErrorActionPreference = 'Stop'\n" + command
            + "\nif ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        ]
    else:
        arguments = ["bash", "-euo", "pipefail", "-c", command]
    result = subprocess.run(
        arguments, cwd=root, env=environment, capture_output=True, text=True,
        check=False, timeout=180,
    )
    assert result.returncode == 0, (command, result.returncode, result.stdout, result.stderr)
    return result


def test_native_shell_propagates_failed_command(tmp_path):
    if os.name == "nt":
        command = "& '" + sys.executable.replace("'", "''") + "' -c 'import sys; sys.exit(17)'"
    else:
        command = shlex.quote(sys.executable) + " -c 'import sys; sys.exit(17)'"
    with pytest.raises(AssertionError, match=r"\b17\b"):
        _run_command(command, root=tmp_path, environment=_installed_environment(tmp_path))


def _exercise_documented_linux_installation(tmp_path: Path) -> None:
    """Run the published Linux commands exactly as written."""

    language = "powershell" if os.name == "nt" else "bash"
    blocks = [
        _documented_commands((REPOSITORY_ROOT / name).read_text(encoding="utf-8"), language)
        for name in DOCUMENTS
    ]
    assert blocks[0] == blocks[1]
    commands = blocks[0]
    subprocess.run(["git", "diff", "--exit-code", "HEAD"], cwd=REPOSITORY_ROOT, check=True)
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=REPOSITORY_ROOT
    ).decode().split("\0")
    root = tmp_path / "checkout"
    root.mkdir()
    for name in filter(None, tracked):
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPOSITORY_ROOT / name, destination, follow_symlinks=False)
    home = tmp_path / "home"
    home.mkdir()
    environment = _installed_environment(home)
    assert not (root / ".venv").exists()
    assert not (home / ".nyx").exists()
    for command in commands[:2]:
        _run_command(command, root=root, environment=environment)
    venv = root / ".venv"
    interpreter = _venv_executable(venv, "python")
    console = _venv_executable(venv, "nyx")
    assert console.resolve().is_relative_to(venv.resolve())
    provenance = subprocess.run(
        [str(interpreter), "-c",
         "import importlib.metadata, json, nyx, sys; "
         "d = importlib.metadata.distribution('nyx'); "
         "print(json.dumps({'module': nyx.__file__, 'prefix': sys.prefix, "
         "'direct_url': json.loads(d.read_text('direct_url.json'))}))"],
        cwd=tmp_path, env=environment, capture_output=True, text=True, check=True, timeout=30,
    )
    installed = json.loads(provenance.stdout)
    assert Path(installed["prefix"]).resolve() == venv.resolve()
    assert Path(installed["module"]).resolve().is_relative_to(venv.resolve())
    assert not installed["direct_url"].get("dir_info", {}).get("editable", False)
    source_url = urlsplit(installed["direct_url"]["url"])
    assert source_url.scheme == "file"
    assert Path(url2pathname(source_url.path)).resolve() == root.resolve()
    sample = (root / "examples" / "sample-specifications").resolve()
    setup = _run_command(commands[2], root=root, environment=environment)
    assert setup.stdout.strip() == f"configured {sample}"
    configuration = json.loads((home / ".nyx/config/config.json").read_text(encoding="utf-8"))
    assert configuration["specification_root"] == str(sample)
    assert configuration["hidden_stages"] == []
    locator = home / ".nyx/runtime/instance.json"
    claim = home / ".nyx/runtime/lease.lock"
    try:
        started = _run_command(commands[3], root=root, environment=environment)
        assert started.stdout.strip() == "http://127.0.0.1:8765/"
        assert json.loads(locator.read_text(encoding="utf-8"))["url"] == started.stdout.strip()
        status = _run_command(commands[4], root=root, environment=environment)
        assert status.stdout.splitlines() == [
            "Configuration: configured",
            f"Workspace: {json.dumps(str(sample))}",
            "Hidden stages: []",
            "Runtime: running",
            'URL: "http://127.0.0.1:8765/"',
        ]
        assert _native_claim_probe(interpreter, claim, root=tmp_path, home=home) == "busy"
        connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=10)
        try:
            connection.request("GET", "/api/catalog", headers={"Host": "127.0.0.1:8765"})
            response = connection.getresponse()
            catalog = json.loads(response.read())
            assert response.status == 200
            assert any(
                entry["declared"]["title"] == "Build the route preview"
                for entry in catalog["entries"]
            )
        finally:
            connection.close()
        _run_command(commands[5], root=root, environment=environment)
        assert not locator.exists()
        assert _native_claim_probe(interpreter, claim, root=tmp_path, home=home) == "free"
    finally:
        # Only the isolated home can authorize cleanup of this test's instance.
        if locator.exists():
            _run_command(commands[5], root=root, environment=environment)


def _documented_desktop_artifact() -> ArtifactLayout:
    staged = os.environ.get("NYX_DESKTOP_ARTIFACT")
    if not staged:
        pytest.skip(
            "the private desktop artifact is staged only in the desktop artifact lane"
        )
    artifact = ArtifactLayout(Path(staged).resolve())
    assert artifact.executable.is_file(), f"staged application is missing: {artifact.executable}"
    documented = (
        DESKTOP_ARTIFACT_PATHS["windows"]
        if os.name == "nt"
        else DESKTOP_ARTIFACT_PATHS["macos"]
    )
    normalized = str(artifact.artifact).replace("\\", "/")
    assert documented.replace("\\", "/") in normalized, (documented, normalized)
    return artifact


def _exercise_documented_desktop_actions(tmp_path: Path) -> None:
    """Run the published desktop actions against the staged private artifact."""

    artifact = _documented_desktop_artifact()
    root = tmp_path / "documented"
    root.mkdir()
    home = root / "home"
    home.mkdir()
    decoy = _inert_path(root)
    environment = _sanitized_environment(home, decoy)

    # First launch: the chooser owns the account but serves no board.
    with _running_gui(artifact, root, environment) as gui:
        time.sleep(3)
        with pytest.raises(OSError):
            _request("/api/catalog")
        assert gui.process.poll() is None, gui.diagnostics()
        assert gui.close() == 0, gui.diagnostics()
    _assert_claims_released(home)

    with _workspace(root) as workspace:
        # Open a configured workspace, serve the board, then Quit.
        _write_configuration(home, workspace)
        expected_first = json.loads(
            scan_catalog(workspace, hidden_stages=_DELIVERED_HIDDEN_STAGES)
        )
        with _running_gui(artifact, root, environment) as gui:
            status, body, _ = _wait_for_board(time.monotonic() + _START_TIMEOUT, gui)
            assert status == 200
            assert json.loads(body) == expected_first
            assert gui.close() == 0, gui.diagnostics()
        _assert_claims_released(home)
        _wait_port_free(time.monotonic() + 15)

        # Change workspace: the replacement selection is served after restart.
        second = root / "second"
        shutil.copytree(workspace, second)
        shutil.rmtree(second / "Trail_API")
        expected_second = json.loads(
            scan_catalog(second, hidden_stages=_DELIVERED_HIDDEN_STAGES)
        )
        assert expected_first != expected_second
        _write_configuration(home, second)
        with _running_gui(artifact, root, environment) as gui:
            status, body, _ = _wait_for_board(time.monotonic() + _START_TIMEOUT, gui)
            assert status == 200
            assert json.loads(body) == expected_second
            assert gui.close() == 0, gui.diagnostics()
        _assert_claims_released(home)
        _wait_port_free(time.monotonic() + 15)

        # Recovery: a surviving former worker blocks the start until it exits.
        config_file = _write_configuration(home, workspace)
        before = config_file.read_bytes()
        _, recovery_path = _claim_paths(home)
        recovery_path.parent.mkdir(parents=True, exist_ok=True)
        former = NativeClaim(recovery_path)
        assert former.acquire(blocking=False)
        try:
            with _running_gui(artifact, root, environment) as gui:
                time.sleep(4)
                with pytest.raises(OSError):
                    _request("/api/catalog")
                assert gui.process.poll() is None, gui.diagnostics()
                assert config_file.read_bytes() == before
                assert gui.close() == 0, gui.diagnostics()
        finally:
            former.close()
        _wait_port_free(time.monotonic() + 15)


@pytest.mark.skipif(
    os.environ.get("NYX_VERIFY_DOCUMENTED_INSTALL") != "1",
    reason="documented installation runs in its dedicated native hosted lane",
)
def test_documented_native_installation_and_lifecycle(tmp_path):
    if os.name == "nt" or sys.platform == "darwin":
        _exercise_documented_desktop_actions(tmp_path)
    else:
        _exercise_documented_linux_installation(tmp_path)
