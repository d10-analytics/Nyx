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
    ArtifactLayout,
    _assert_claims_released,
    _claim_paths,
    _expected_catalog,
    _inert_path,
    _request,
    _running_gui,
    _sanitized_environment,
    _wait_for_board,
    _wait_for_window,
    _wait_port_free,
    _workspace,
    _write_configuration,
)
from test_wheel_install import (
    _installed_environment,
    _native_claim_probe,
    _venv_executable,
)

from nyx import catalog
from nyx._native_claim import NativeClaim

REPOSITORY_ROOT = Path(__file__).parents[1].resolve()
DOCUMENTS = ("README.md", "docs/running-nyx.md")
EXPECTED = {
    "bash": (
        "python3.12 -m venv .venv",
        ".venv/bin/python -m pip install .",
        ".venv/bin/nyx --setup examples/sample-specifications",
        ".venv/bin/nyx",
        ".venv/bin/nyx --status",
        ".venv/bin/nyx --stop",
    ),
}
BUILD_HEADING = "### Build the private artifact"
# The desktop build is bounded for a native compile, not for one command.
_BUILD_TIMEOUT = 45 * 60
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
    "**Cancel** is available only while changing the workspace",
    "returns to the current workspace without quitting",
    "**Close** finishes a blocked close or a board-display failure",
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
        "absent",
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
        "absent",
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
        "Hide terminal rows",
        "Reload board settings",
        "configuration revision",
        "moving it out reopens it",
        "recorded workflow assertion",
        "A theme change follows other open tabs",
        "The compact view (**Hide empty rows and columns**), **Hide terminal rows**, and "
        "**Click empty space to deselect** apply per tab until reload",
        '"Board settings changed elsewhere. Reload board settings to replace this unsaved draft."',
        "Choose **Reload board settings** to replace the draft",
    ),
    "docs/workspaces.md": (
        "no implicit `Done` or `Archive` rule",
        "Completion Prerequisite: UUID",
        "missing policy leaves the requirement **unknown**",
        "saved per canonical workspace root",
        "root with no saved names reports unknown",
        "A cancelled or archived item",
        "finished row",
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
PORT_CONFLICT_GUIDANCE_MARKERS = (
    "**Nyx says port 8765 is already in use:**",
    "Linux", "`ss -ltnp 'sport = :8765'`",
    "macOS", "`lsof -nP -iTCP:8765 -sTCP:LISTEN`",
    "Windows PowerShell", "`Get-NetTCPConnection -LocalPort 8765 -State Listen`",
    "`OwningProcess` is the process ID",
    "Close that program", "run `nyx` again", "or choose **Retry**",
    "Nyx never switches ports",
    "Nyx runtime could not start; retry",
    "excluded or reserved port range", "no owning program",
    "`netsh interface ipv4 show excludedportrange protocol=tcp`",
)
RECOVERY_GUIDANCE_HEADING = "### Recover an unreadable runtime record"
RECOVERY_GUIDANCE_MARKERS = (
    "unreadable or malformed and has no owning instance",
    "`Runtime: unknown`",
    "`runtime state unavailable`",
    "`nyx --stop` removes that record exactly as it removes a valid stale record",
    "prints the ordinary `stopped` result",
    "no separate recovery message",
    "With no owning instance, starting with `nyx` still refuses while the record "
    "cannot be read, and its message names the recovery step and ends "
    "`; run 'nyx --stop' to recover`",
    "`; run 'nyx --stop' to recover`",
    "Recovery requires that no instance owns the runtime",
    "a live owner still refuses both start and stop",
)
EVENT_COMPLETION_EXAMPLE = (
    "# Organize the neighborhood festival\n"
    "Package ID: 123e4567-e89b-42d3-a456-426614174100\n"
    "Status: planning\n"
    "Completion Prerequisite: 123e4567-e89b-42d3-a456-426614174102\n"
    "Prerequisite: 123e4567-e89b-42d3-a456-426614174102 | venue-confirmed\n"
    "Prerequisite: 123e4567-e89b-42d3-a456-426614174102 | permit-approved"
)
_START_TIMEOUT = 90.0


def _fenced_blocks(text: str, language: str) -> list[str]:
    return re.findall(rf"^```{language}\n(.*?)^```", text, re.MULTILINE | re.DOTALL)


def _documented_commands(document: str, language: str) -> tuple[str, ...]:
    blocks = _fenced_blocks(document, language)
    assert blocks, f"missing {language} installation block"
    commands = tuple(blocks[0].splitlines())
    assert commands == EXPECTED[language], f"changed {language} installation commands"
    return commands


def _published_build_block(document: str, language: str) -> str:
    """Return the one ``language`` block published under the build heading."""

    assert document.count(BUILD_HEADING + "\n") == 1, "missing build heading"
    kept: list[str] = []
    fenced = False
    for line in document.split(BUILD_HEADING + "\n", 1)[1].splitlines(keepends=True):
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(r"#{1,3} ", line):
            break
        kept.append(line)
    section = "".join(kept)
    blocks = _fenced_blocks(section, language)
    assert len(blocks) == 1, f"expected one {language} build block, found {len(blocks)}"
    assert blocks[0].strip(), f"empty {language} build block"
    return blocks[0]


def _lane_build_language() -> str | None:
    if os.name == "nt":
        return "powershell"
    if sys.platform == "darwin":
        return "bash"
    return None


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


def test_readme_and_guide_publish_the_same_powershell_build_block():
    guide = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    readme_blocks = _fenced_blocks(readme, "powershell")
    assert len(readme_blocks) == 1, "README must publish exactly one PowerShell block"
    selected = _published_build_block(guide, "powershell")
    assert readme_blocks[0] == selected
    assert "scripts" in selected and "build_desktop.py" in selected


SYNTHETIC_GUIDE = (
    "# Running Nyx\n\n"
    "## Linux\n\n```bash\nlinux-first\n```\n\n"
    "{heading}\n\n"
    "```powershell\n{powershell}\n```\n\n"
    "```bash\n{macos}\n```\n\n"
    "### Open\n\n```bash\nafter-section\n```\n"
)


def _synthetic_guide(powershell="ps-build", macos="mac-build", heading=BUILD_HEADING):
    return SYNTHETIC_GUIDE.format(heading=heading, powershell=powershell, macos=macos)


def test_build_block_is_selected_by_heading_and_language_not_position():
    guide = _synthetic_guide()
    assert _published_build_block(guide, "bash") == "mac-build\n"
    assert _published_build_block(guide, "powershell") == "ps-build\n"
    # The first and last bash blocks of the document are never the build block.
    assert _published_build_block(guide, "bash") not in ("linux-first\n", "after-section\n")


def test_changing_a_command_under_the_heading_changes_the_selected_block():
    before = _published_build_block(_synthetic_guide(), "bash")
    after = _published_build_block(_synthetic_guide(macos="mac-build\nextra"), "bash")
    assert after != before
    assert after == "mac-build\nextra\n"
    # A change outside the heading leaves the selection alone.
    other = _synthetic_guide().replace("linux-first", "linux-changed")
    assert _published_build_block(other, "bash") == before


def test_build_selection_rejects_missing_heading_or_missing_language_block():
    with pytest.raises(AssertionError, match="missing build heading"):
        _published_build_block(_synthetic_guide(heading="### Something else"), "bash")
    with pytest.raises(AssertionError, match="expected one"):
        _published_build_block(_synthetic_guide().replace("```powershell", "```text"), "powershell")


def test_real_guide_selects_the_macos_build_block_not_the_linux_one():
    guide = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    selected = _published_build_block(guide, "bash")
    linux = "\n".join(EXPECTED["bash"]) + "\n"
    assert selected != linux
    assert "build_desktop.py" in selected
    assert "venv" in selected


def test_documents_publish_the_private_desktop_guidance():
    for name in DOCUMENTS:
        document = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
        for marker in DESKTOP_ARTIFACT_MARKERS:
            assert marker in document, (name, marker)
    guide = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    for marker in DESKTOP_GUIDANCE_MARKERS:
        assert marker in guide, marker
    for name in DOCUMENTS:
        assert "**Revalidate**" not in (REPOSITORY_ROOT / name).read_text(encoding="utf-8"), name


@pytest.mark.parametrize("name", [
    "README.md", "docs/running-nyx.md", "docs/workspaces.md",
    "docs/specification-reference.md", "examples/sample-specifications/README.md",
])
def test_documents_remove_stage_hiding_and_retain_finished_rows(name):
    document = re.sub(r"\s+", " ", (REPOSITORY_ROOT / name).read_text(encoding="utf-8"))
    assert not re.search(
        r"hide-stage|show-all-stages|visible stages|stage visibility|configured-hidden|"
        r"hidden stages|hidden completed stage|hidden prerequisite|hidden or absent",
        document, re.IGNORECASE,
    )
    assert "Hide terminal rows" in document
    assert "Board settings" in document


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


def test_documented_header_limits_match_the_catalog():
    document = (REPOSITORY_ROOT / "docs" / "specification-reference.md").read_text(encoding="utf-8")
    normalized = re.sub(r"\s+", " ", document)
    limit = f"{catalog._HEADER_BYTE_LIMIT:,}"
    assert "A single leading UTF-8 byte order mark (BOM) is ignored" in normalized
    assert f"The header limit is {limit} bytes" in normalized
    assert f"A whole `program.md` descriptor obeys the same {limit}-byte limit" in normalized


def test_documented_output_limit_matches_the_catalog():
    document = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    heading = "## If something looks wrong"
    assert heading in document
    section = document.split(heading, 1)[1].split("\n## ", 1)[0]
    normalized = re.sub(r"\s+", " ", section)
    mebibytes = catalog.MAX_OUTPUT_BYTES / (1024 * 1024)
    limit = f"{mebibytes:g} MiB"
    assert "`producer_output_too_large`" in normalized
    assert f"larger than the {limit} catalog limit" in normalized


def test_event_walkthrough_shows_whole_item_and_named_outcomes():
    document = (REPOSITORY_ROOT / "docs" / "workspaces.md").read_text(encoding="utf-8")
    assert EVENT_COMPLETION_EXAMPLE in document
    assert "event plan's whole-item requirement" in document
    assert "literal `Done` stage" in document
    assert "separate named outcomes" in document


def test_port_conflict_guidance_matches_the_public_message():
    from nyx import runtime

    document = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    assert document.splitlines()[0] == "# Running Nyx"
    section = document.split("## If something looks wrong\n", 1)[1].split("\n## ", 1)[0]
    normalized = re.sub(r"\s+", " ", section)
    for marker in PORT_CONFLICT_GUIDANCE_MARKERS:
        assert marker in normalized, marker
    assert "```" not in section
    assert "If something looks wrong" in runtime.PORT_UNAVAILABLE_MESSAGE
    assert "Running Nyx" in runtime.PORT_UNAVAILABLE_MESSAGE


def test_documents_publish_the_invalid_record_recovery_guidance():
    document = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    # The recovery guidance must not displace the installation block: the first
    # fenced block in each published language stays exactly as documented.
    for language in EXPECTED:
        _documented_commands(document, language)
    assert RECOVERY_GUIDANCE_HEADING in document, "missing invalid-record recovery guidance"
    section = document.split(RECOVERY_GUIDANCE_HEADING, 1)[1].split("\n#", 1)[0]
    normalized = re.sub(r"\s+", " ", section)
    for marker in RECOVERY_GUIDANCE_MARKERS:
        assert marker in normalized, marker
    assert "```" not in section


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


_PWSH_SESSION_PREFIX = (
    "$ErrorActionPreference = 'Stop'\n"
    "$PSNativeCommandUseErrorActionPreference = $true\n"
    "trap {\n"
    "  [Console]::Error.WriteLine(($_ | Out-String))\n"
    "  if ($_.Exception -is [System.Management.Automation.NativeCommandExitException] "
    "-and $LASTEXITCODE -is [int] -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
    "  exit 1\n"
    "}\n"
)


def _session_arguments(block: str) -> list[str]:
    """Return the argument vector running ``block`` as one shell session."""

    if os.name == "nt":
        return [
            "pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
            _PWSH_SESSION_PREFIX + block,
        ]
    return ["bash", "-euo", "pipefail", "-c", block]


def _run_session(
    block: str, *, root: Path, environment: dict[str, str] | None = None,
    timeout: float = _BUILD_TIMEOUT,
) -> subprocess.CompletedProcess:
    arguments = _session_arguments(block)
    if shutil.which(arguments[0]) is None:
        pytest.fail(f"the lane's shell {arguments[0]!r} is not available")
    return subprocess.run(
        arguments, cwd=root, env=environment, capture_output=True, text=True,
        check=False, timeout=timeout,
    )


def _native_exit(code: int) -> str:
    interpreter = sys.executable
    if os.name == "nt":
        return f"& '{interpreter.replace(chr(39), chr(39) * 2)}' -c 'import sys; sys.exit({code})'"
    return f"{shlex.quote(interpreter)} -c 'import sys; sys.exit({code})'"


def test_session_keeps_state_between_lines(tmp_path):
    if os.name == "nt":
        block = "$env:NYX_SESSION_PROBE = 'kept'\nWrite-Output \"seen=$env:NYX_SESSION_PROBE\""
    else:
        block = 'export NYX_SESSION_PROBE=kept\necho "seen=$NYX_SESSION_PROBE"'
    result = _run_session(block, root=tmp_path, timeout=120)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.strip() == "seen=kept"


def test_session_runs_from_the_given_root(tmp_path):
    block = "Write-Output (Get-Location).Path" if os.name == "nt" else "pwd"
    result = _run_session(block, root=tmp_path, timeout=120)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


def test_session_failing_native_command_stops_the_block_with_its_own_exit_code(tmp_path):
    block = _native_exit(17) + "\necho after-failure"
    result = _run_session(block, root=tmp_path, timeout=120)
    assert result.returncode == 17, (result.returncode, result.stdout, result.stderr)
    assert "after-failure" not in result.stdout


def test_session_without_errors_exits_zero(tmp_path):
    block = _native_exit(0) + "\necho finished"
    result = _run_session(block, root=tmp_path, timeout=120)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "finished" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="the PowerShell trap exists only on Windows lanes")
def test_session_non_native_terminating_error_exits_non_zero_with_record(tmp_path):
    # A native command exiting 0 leaves LASTEXITCODE 0, so a ``throw`` must
    # still end the session non-zero.
    block = _native_exit(0) + "\nthrow 'nyx-session-boom'\nWrite-Output after-throw"
    result = _run_session(block, root=tmp_path, timeout=120)
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert "nyx-session-boom" in result.stderr
    assert "after-throw" not in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="the PowerShell trap exists only on Windows lanes")
def test_session_native_failure_record_reaches_stderr(tmp_path):
    result = _run_session(_native_exit(23), root=tmp_path, timeout=120)
    assert result.returncode == 23, (result.returncode, result.stdout, result.stderr)
    assert result.stderr.strip()


@pytest.mark.skipif(
    os.environ.get("NYX_VERIFY_DOCUMENTED_BUILD") != "1",
    reason="the documented build runs only in the desktop documented-build step",
)
def test_documented_desktop_build_blocks_run_literally():
    language = _lane_build_language()
    if language is None:
        pytest.skip("the documented build runs only on the Windows and macOS desktop lanes")
    guide = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    block = _published_build_block(guide, language)
    try:
        result = _run_session(block, root=REPOSITORY_ROOT, environment=dict(os.environ))
    except subprocess.TimeoutExpired as expired:
        pytest.fail(f"the documented {language} build exceeded {_BUILD_TIMEOUT} s: {expired.stdout!r}")
    print(result.stdout)
    print(result.stderr, file=sys.stderr)
    assert result.returncode == 0, (
        f"documented {language} build exited {result.returncode}",
        result.stdout[-4000:], result.stderr[-4000:],
    )


def _exercise_documented_linux_installation(tmp_path: Path) -> None:
    """Run the published Linux commands exactly as written."""

    blocks = [
        _documented_commands((REPOSITORY_ROOT / name).read_text(encoding="utf-8"), "bash")
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
    assert configuration == {"schema_version": 3, "specification_root": str(sample)}
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
        _wait_for_window(gui)
        with pytest.raises(OSError):
            _request("/api/catalog")
        assert gui.process.poll() is None, gui.diagnostics()
        assert gui.close() == 0, gui.diagnostics()
    _assert_claims_released(home)

    with _workspace(root) as workspace:
        # Open a configured workspace, serve the board, then Quit.
        _, first_configuration = _write_configuration(home, workspace)
        expected_first = json.loads(_expected_catalog(first_configuration))
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
        _, second_configuration = _write_configuration(home, second)
        expected_second = json.loads(_expected_catalog(second_configuration))
        assert expected_first != expected_second
        with _running_gui(artifact, root, environment) as gui:
            status, body, _ = _wait_for_board(time.monotonic() + _START_TIMEOUT, gui)
            assert status == 200
            assert json.loads(body) == expected_second
            assert gui.close() == 0, gui.diagnostics()
        _assert_claims_released(home)
        _wait_port_free(time.monotonic() + 15)

        # Recovery: a surviving former worker blocks the start until it exits.
        config_file, _ = _write_configuration(home, workspace)
        before = config_file.read_bytes()
        _, recovery_path = _claim_paths(home)
        recovery_path.parent.mkdir(parents=True, exist_ok=True)
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
