"""Behavioral contract checks for the hosted CI workflow.

These tests parse the real workflow document and assert the operational
contract that the native source/session lane and the desktop build/artifact
extension establish: exact commands, installer extras, artifact inputs,
required-native environment, interactive-session preflight, stage ordering,
and non-zero exit propagation.  Checks read each step's effective lines (the
stripped lines left after dropping blank lines and full-line comments) and
match gating commands exactly, so a commented-out or failure-masked command
does not satisfy them.  Each supported behavior is proved to be
load-bearing by mutating the parsed document — a natural edit that removes or
weakens the behavior — and asserting that the matching check rejects it.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

WORKFLOW_PATH = (
    Path(__file__).parents[1] / ".github" / "workflows" / "tests.yml"
)

# The established lane matrix.  Baseline jobs keep their latest labels; the
# desktop lanes pin the explicit feasibility hosts.
BASELINE_JOBS = {
    "nyx": {"runs_on": "ubuntu-latest", "python": '"$runner_python"'},
    "nyx-windows": {"runs_on": "windows-latest", "python": "python"},
    "nyx-macos": {"runs_on": "macos-latest", "python": '"$runner_python"'},
}
DESKTOP_JOBS = {
    "nyx-desktop-windows": {
        "runs_on": "windows-2025",
        "host": "windows-2025",
        "artifact": "dist/desktop/Nyx/Nyx.exe",
        "artifact_setup": "$artifact = (Resolve-Path 'dist/desktop/Nyx/Nyx.exe').Path",
        "artifact_export": "$env:NYX_DESKTOP_ARTIFACT = $artifact",
        "shell": "pwsh",
    },
    "nyx-desktop-macos": {
        "runs_on": "macos-15",
        "host": "macos-15",
        "artifact": "dist/desktop/Nyx.app",
        "artifact_setup": 'artifact="$PWD/dist/desktop/Nyx.app"',
        "artifact_export": 'export NYX_DESKTOP_ARTIFACT="$artifact"',
        "shell": "bash",
    },
}

POWERSHELL_JOBS = ("nyx-windows", "nyx-desktop-windows")
POSIX_JOBS = ("nyx", "nyx-macos", "nyx-desktop-macos")

REQUIRED_NATIVE_ENV = "NYX_REQUIRE_NATIVE_DESKTOP"
EXPECTED_NATIVE_HOST_ENV = "NYX_EXPECTED_NATIVE_HOST"
DESKTOP_ARTIFACT_ENV = "NYX_DESKTOP_ARTIFACT"
DOCUMENTED_INSTALL_ENV = "NYX_VERIFY_DOCUMENTED_INSTALL"

SOURCE_SESSION_STEP = "Verify native desktop source and session"
BUILD_ARTIFACT_STEP = "Build and verify the native desktop artifact"
PREFLIGHT_STEP = "Record native host and session preflight"

SOURCE_SUITE_COMMAND = "{python} -m pytest tests/ -v"
RUFF_COMMAND = "ruff check ."
WHEEL_BUILD_COMMAND = "{python} -m build --wheel"
INSTALLED_WHEEL_COMMAND = "{python} -m pytest tests/test_wheel_install.py -v"
DOCUMENTED_INSTALL_COMMAND = "python -m pytest tests/test_documented_install.py -v"
DESKTOP_SOURCE_COMMAND = "python -m pytest tests/test_desktop.py -v -s"
DESKTOP_ARTIFACT_COMMAND = "python -m pytest tests/test_desktop_artifact.py -v -s"
DESKTOP_BUILD_COMMAND = "python scripts/build_desktop.py"
DESKTOP_EXTRA_INSTALL = "python -m pip install -e '.[test,desktop,build]'"
PWSH_FAILURE_GUARD = "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }"
POSIX_FAILURE_GUARD = "set -euo pipefail"

BROWSER_INSTALL_COMMANDS = {
    "nyx": '"$runner_python" -m playwright install --with-deps chromium',
    "nyx-windows": "python -m playwright install chromium",
    "nyx-macos": '"$runner_python" -m playwright install chromium',
}

# Gating commands are matched by exact equality against effective lines.  A
# gate is a tuple of effective lines: a one-line gate is a single command, and
# a command that continues over several physical lines is matched as a
# contiguous block, so commenting out, ``|| true``, ``; exit 0`` or
# backgrounding on any of its lines is detected.
POSIX_ONE_WHEEL_GATES = (
    ("wheel_count=\"$(find dist -maxdepth 1 -type f -name '*.whl' | wc -l)\"",),
    ('test "$wheel_count" -eq 1',),
)
ONE_WHEEL_GATES = {
    "nyx": POSIX_ONE_WHEEL_GATES,
    "nyx-windows": (
        (
            "$wheels = @(Get-ChildItem -Path dist -Filter '*.whl' -File)",
            "if ($wheels.Count -ne 1) {",
            'throw "Expected exactly one wheel, found $($wheels.Count)"',
            "}",
        ),
    ),
    "nyx-macos": POSIX_ONE_WHEEL_GATES,
}

LINUX_SERVICE_GATES = (
    ('sudo userdel --remove "$nyx_verify_user"',),
    ("trap cleanup EXIT",),
    ('sudo useradd --create-home --shell /bin/bash "$nyx_verify_user"',),
    (
        'sudo install --owner="$nyx_verify_user" --group="$nyx_verify_user" --mode=0600 \\',
        'tests/test_installed_service.py "$nyx_verify_root/exercise/test_installed_service.py"',
    ),
    (
        'timeout --preserve-status 90s sudo -u "$nyx_verify_user" -H env -u PYTHONPATH -u PYTHONHOME \\',
        'PLAYWRIGHT_BROWSERS_PATH="$nyx_verify_root/playwright-browsers" \\',
        'PATH="$nyx_verify_home/.local/bin:/usr/bin:/bin" \\',
        "/bin/bash -c 'cd \"$1\" && exec \"$2\" -m pytest test_installed_service.py -v' \\",
        'nyx-service-test "$nyx_verify_root/exercise" "$nyx_verify_root/venv/bin/python"',
    ),
)
# The cleanup stop is deliberately best-effort (it ends in ``|| true``), so it
# stays a substring marker rather than an exact gate.
LINUX_SERVICE_MARKERS = ('"$nyx_verify_root/venv/bin/nyx" --stop',)

SESSION_PREFLIGHT_GATES = {
    "nyx-desktop-windows": (
        (
            "if ([string]::IsNullOrWhiteSpace($env:ImageOS) -or",
            "[string]::IsNullOrWhiteSpace($env:ImageVersion)) {",
            "throw 'GitHub runner image identity is unavailable'",
            "}",
        ),
        ("$interactive = [Environment]::UserInteractive",),
        (
            'python -c "import platform, sys; print(platform.platform()); '
            "assert sys.version_info[:2] == (3, 12); "
            "assert platform.machine().lower() in {'amd64', 'x86_64'}\"",
        ),
        (
            "if (-not $interactive) {",
            "throw 'windows-2025 does not expose an interactive desktop session'",
            "}",
        ),
    ),
    "nyx-desktop-macos": (
        ('test -n "${ImageOS:-}"',),
        ('test -n "${ImageVersion:-}"',),
        ('session_manager="$(launchctl managername)"',),
        (
            'python -c "import platform, sys; print(platform.platform()); '
            "assert sys.version_info[:2] == (3, 12); "
            "assert platform.machine() == 'arm64'\"",
        ),
        ('test "$session_manager" = "Aqua"',),
    ),
}
# The console user is an informational value that is echoed, not a gate.
SESSION_PREFLIGHT_MARKERS = {
    "nyx-desktop-windows": (),
    "nyx-desktop-macos": ("/dev/console",),
}

PWSH_ERROR_PREFERENCE = "$ErrorActionPreference = 'Stop'"
CONTINUE_ON_ERROR_KEY = "continue-on-error"

# ``set`` invocations inside one effective line; the captured arguments stop at
# the next command separator.
_SET_INVOCATION = re.compile(r"(?:^|[\s;&|(`])set\s+([^;&|)`]*)")
_FAILURE_PROPAGATION_OPTIONS = frozenset({"errexit", "pipefail"})


class WorkflowContractViolation(AssertionError):
    """Raised when a workflow no longer satisfies the established contract."""


# --------------------------------------------------------------------------
# Parsing and structural accessors
# --------------------------------------------------------------------------


def load_workflow() -> dict[str, Any]:
    """Parse the real workflow document as the contract authority."""

    with WORKFLOW_PATH.open(encoding="utf-8") as stream:
        document = yaml.safe_load(stream)
    if not isinstance(document, dict):
        raise WorkflowContractViolation("workflow document is not a mapping")
    return document


def _jobs(workflow: dict[str, Any]) -> dict[str, Any]:
    jobs = workflow.get("jobs")
    return jobs if isinstance(jobs, dict) else {}


def _job(workflow: dict[str, Any], name: str) -> Any:
    return _jobs(workflow).get(name)


def _steps(job: Any) -> list[dict[str, Any]]:
    if not isinstance(job, dict):
        return []
    steps = job.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


def _step_named(job: Any, name: str) -> Any:
    for step in _steps(job):
        if step.get("name") == name:
            return step
    return None


def _run_text(step: Any) -> str:
    if not isinstance(step, dict):
        return ""
    run = step.get("run")
    return run if isinstance(run, str) else ""


def _effective_lines(step: Any) -> list[str]:
    """Return the lines a step's shell actually executes, for bash and pwsh.

    Each physical line is stripped; blank lines and full-line ``#`` comments
    are dropped.  Continuation lines are not joined, so a multi-line command
    is compared as a block of these lines.  PowerShell block comments
    (``<# ... #>``) are not recognised.
    """

    lines: list[str] = []
    for physical in _run_text(step).splitlines():
        line = physical.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def _job_effective_lines(job: Any) -> list[list[str]]:
    """Return the effective lines of every step in a job, one list per step."""

    return [_effective_lines(step) for step in _steps(job)]


def _contains_gate(segments: list[list[str]], gate: tuple[str, ...]) -> bool:
    width = len(gate)
    for lines in segments:
        for start in range(len(lines) - width + 1):
            if tuple(lines[start : start + width]) == gate:
                return True
    return False


def _contains_marker(segments: list[list[str]], marker: str) -> bool:
    return any(marker in line for lines in segments for line in lines)


def _gate_violations(
    label: str, segments: list[list[str]], gate: tuple[str, ...]
) -> list[str]:
    """Report a gate that is absent or that appears in an altered form.

    A gate must appear as exact contiguous effective lines.  For a one-line
    gate, every effective line that contains the command but differs from it
    is also reported, so a weakened copy cannot hide behind an intact
    duplicate elsewhere in the same scope.
    """

    problems: list[str] = []
    if not _contains_gate(segments, gate):
        if len(gate) == 1:
            problems.append(f"{label} {gate[0]!r} is not an exact effective line")
        else:
            problems.append(
                f"{label} block starting {gate[0]!r} is not present as "
                f"{len(gate)} contiguous exact effective lines"
            )
    if len(gate) == 1:
        command = gate[0]
        for lines in segments:
            for line in lines:
                if command in line and line != command:
                    problems.append(f"{label} {command!r} is altered as {line!r}")
    return problems


def _step_env(step: Any) -> dict[str, Any]:
    if not isinstance(step, dict):
        return {}
    env = step.get("env")
    return env if isinstance(env, dict) else {}


def _step(workflow: dict[str, Any], job_name: str, step_name: str) -> dict[str, Any]:
    found = _step_named(_job(workflow, job_name), step_name)
    if found is None:
        raise WorkflowContractViolation(f"{job_name}: step {step_name!r} is missing")
    return found


def _events(workflow: dict[str, Any]) -> list[str]:
    if not isinstance(workflow, dict):
        return []
    for key in (True, "on", "true", "True"):
        if key not in workflow:
            continue
        value = workflow[key]
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(entry) for entry in value]
        if isinstance(value, dict):
            return [str(entry) for entry in value]
    return []


# --------------------------------------------------------------------------
# Checks.  Each returns a list of human-readable violations and never raises
# for a well-formed (possibly mutated) document.
# --------------------------------------------------------------------------


def check_workflow_trigger(workflow: dict[str, Any]) -> list[str]:
    if "push" not in _events(workflow):
        return ["workflow does not trigger on push"]
    return []


def check_job_matrix(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, spec in {**BASELINE_JOBS, **DESKTOP_JOBS}.items():
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        if job.get("runs-on") != spec["runs_on"]:
            problems.append(
                f"{name}: runs-on is {job.get('runs-on')!r}, expected {spec['runs_on']!r}"
            )
    return problems


def check_shell_matrix(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name in POWERSHELL_JOBS:
        for step in _steps(_job(workflow, name)):
            if not _run_text(step):
                continue
            if step.get("shell") != "pwsh":
                problems.append(
                    f"{name}: step {step.get('name')!r} does not declare the pwsh shell"
                )
    for name in POSIX_JOBS:
        for step in _steps(_job(workflow, name)):
            if not _run_text(step):
                continue
            if step.get("shell") == "pwsh":
                problems.append(
                    f"{name}: step {step.get('name')!r} unexpectedly uses the pwsh shell"
                )
    return problems


def check_baseline_source_suite(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, spec in BASELINE_JOBS.items():
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        command = SOURCE_SUITE_COMMAND.format(python=spec["python"])
        problems.extend(
            _gate_violations(
                f"{name}: source-suite command", _job_effective_lines(job), (command,)
            )
        )
    return problems


def check_baseline_ruff(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name in BASELINE_JOBS:
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        problems.extend(
            _gate_violations(
                f"{name}: Ruff command", _job_effective_lines(job), (RUFF_COMMAND,)
            )
        )
    return problems


def check_baseline_wheel(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, spec in BASELINE_JOBS.items():
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        segments = _job_effective_lines(job)
        build = WHEEL_BUILD_COMMAND.format(python=spec["python"])
        problems.extend(
            _gate_violations(f"{name}: wheel build command", segments, (build,))
        )
        for gate in ONE_WHEEL_GATES[name]:
            problems.extend(
                _gate_violations(f"{name}: one-wheel assertion", segments, gate)
            )
    return problems


def check_baseline_installed_wheel(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, spec in BASELINE_JOBS.items():
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        command = INSTALLED_WHEEL_COMMAND.format(python=spec["python"])
        problems.extend(
            _gate_violations(
                f"{name}: installed-wheel command", _job_effective_lines(job), (command,)
            )
        )
    return problems


def check_baseline_browser_prerequisites(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, command in BROWSER_INSTALL_COMMANDS.items():
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        if not _contains_marker(_job_effective_lines(job), command):
            problems.append(f"{name}: browser prerequisite {command!r} is missing")
    return problems


def check_linux_service_lane(workflow: dict[str, Any]) -> list[str]:
    job = _job(workflow, "nyx")
    if job is None:
        return ["nyx: job is missing"]
    segments = _job_effective_lines(job)
    problems: list[str] = []
    for gate in LINUX_SERVICE_GATES:
        problems.extend(
            _gate_violations("nyx: Linux installed-service command", segments, gate)
        )
    problems.extend(
        f"nyx: Linux installed-service marker {marker!r} is missing"
        for marker in LINUX_SERVICE_MARKERS
        if not _contains_marker(segments, marker)
    )
    return problems


def check_documented_install_lane(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name in BASELINE_JOBS:
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        step = _step_named(job, "Verify documented installation commands")
        if step is None:
            problems.append(f"{name}: documented installation step is missing")
            continue
        env = _step_env(step)
        if env.get(DOCUMENTED_INSTALL_ENV) != "1":
            problems.append(
                f"{name}: documented installation step does not set {DOCUMENTED_INSTALL_ENV}=1"
            )
        problems.extend(
            _gate_violations(
                f"{name}: documented installation command",
                [_effective_lines(step)],
                (DOCUMENTED_INSTALL_COMMAND,),
            )
        )
    return problems


def _unguarded_pwsh_commands(name: str, step: dict[str, Any]) -> list[str]:
    lines = _effective_lines(step)
    problems: list[str] = []
    for index, line in enumerate(lines):
        if not (line.startswith("python ") or line.startswith("ruff ")):
            continue
        guard = lines[index + 1] if index + 1 < len(lines) else ""
        if guard != PWSH_FAILURE_GUARD:
            problems.append(
                f"{name}: step {step.get('name')!r} command {line!r} is not followed "
                f"by the exit guard {PWSH_FAILURE_GUARD!r}"
            )
    return problems


def check_powershell_failure_propagation(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name in POWERSHELL_JOBS:
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        for step in _steps(job):
            lines = _effective_lines(step)
            if step.get("shell") != "pwsh" or not lines:
                continue
            if lines[0] != PWSH_ERROR_PREFERENCE:
                problems.append(
                    f"{name}: step {step.get('name')!r} does not start with "
                    f"{PWSH_ERROR_PREFERENCE!r}"
                )
            for line in lines[1:]:
                if "erroractionpreference" in line.lower():
                    problems.append(
                        f"{name}: step {step.get('name')!r} changes the error "
                        f"preference after the guard: {line!r}"
                    )
            problems.extend(_unguarded_pwsh_commands(name, step))
    return problems


def _disables_failure_propagation(line: str) -> bool:
    """Return whether a line runs ``set`` to clear errexit or pipefail."""

    for invocation in _SET_INVOCATION.finditer(line):
        tokens = invocation.group(1).split()
        index = 0
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if len(token) < 2 or token[0] not in "+-" or token.startswith("--"):
                continue
            flags = token[1:]
            if token[0] == "+" and "e" in flags:
                return True
            if "o" in flags and index < len(tokens):
                option = tokens[index]
                index += 1
                if token[0] == "+" and option in _FAILURE_PROPAGATION_OPTIONS:
                    return True
    return False


def check_posix_failure_propagation(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name in POSIX_JOBS:
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        for step in _steps(job):
            lines = _effective_lines(step)
            if not lines or step.get("shell") == "pwsh":
                continue
            if lines[0] != POSIX_FAILURE_GUARD:
                problems.append(
                    f"{name}: step {step.get('name')!r} does not start with "
                    f"{POSIX_FAILURE_GUARD!r} shell failure propagation"
                )
            for line in lines:
                if _disables_failure_propagation(line):
                    problems.append(
                        f"{name}: step {step.get('name')!r} disables shell failure "
                        f"propagation: {line!r}"
                    )
    return problems


def check_no_continue_on_error(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for job_name, job in _jobs(workflow).items():
        if isinstance(job, dict) and CONTINUE_ON_ERROR_KEY in job:
            problems.append(
                f"{job_name}: job sets {CONTINUE_ON_ERROR_KEY}="
                f"{job[CONTINUE_ON_ERROR_KEY]!r}"
            )
        for step in _steps(job):
            if CONTINUE_ON_ERROR_KEY in step:
                problems.append(
                    f"{job_name}: step {step.get('name') or step.get('uses')!r} sets "
                    f"{CONTINUE_ON_ERROR_KEY}={step[CONTINUE_ON_ERROR_KEY]!r}"
                )
    return problems


def check_native_environment(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, spec in DESKTOP_JOBS.items():
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        for step_name in (SOURCE_SESSION_STEP, BUILD_ARTIFACT_STEP):
            step = _step_named(job, step_name)
            if step is None:
                problems.append(f"{name}: {step_name!r} step is missing")
                continue
            env = _step_env(step)
            if env.get(REQUIRED_NATIVE_ENV) != "1":
                problems.append(
                    f"{name}: {step_name!r} does not set {REQUIRED_NATIVE_ENV}=1"
                )
            if env.get(EXPECTED_NATIVE_HOST_ENV) != spec["host"]:
                problems.append(
                    f"{name}: {step_name!r} expected native host is "
                    f"{env.get(EXPECTED_NATIVE_HOST_ENV)!r}, expected {spec['host']!r}"
                )
    return problems


def check_native_dependencies(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name in DESKTOP_JOBS:
        step = _step_named(_job(workflow, name), SOURCE_SESSION_STEP)
        if step is None:
            problems.append(f"{name}: {SOURCE_SESSION_STEP!r} step is missing")
            continue
        problems.extend(
            _gate_violations(
                f"{name}: native source/session install",
                [_effective_lines(step)],
                (DESKTOP_EXTRA_INSTALL,),
            )
        )
    return problems


def check_native_source_lane(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name in DESKTOP_JOBS:
        step = _step_named(_job(workflow, name), SOURCE_SESSION_STEP)
        if step is None:
            problems.append(f"{name}: {SOURCE_SESSION_STEP!r} step is missing")
            continue
        problems.extend(
            _gate_violations(
                f"{name}: native source/session command",
                [_effective_lines(step)],
                (DESKTOP_SOURCE_COMMAND,),
            )
        )
    return problems


def check_native_session_preflight(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, gates in SESSION_PREFLIGHT_GATES.items():
        step = _step_named(_job(workflow, name), PREFLIGHT_STEP)
        if step is None:
            problems.append(f"{name}: {PREFLIGHT_STEP!r} step is missing")
            continue
        segments = [_effective_lines(step)]
        for gate in gates:
            problems.extend(
                _gate_violations(f"{name}: session preflight gate", segments, gate)
            )
        for marker in SESSION_PREFLIGHT_MARKERS[name]:
            if not _contains_marker(segments, marker):
                problems.append(
                    f"{name}: session preflight marker {marker!r} is missing"
                )
    return problems


def check_native_artifact_lane(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, spec in DESKTOP_JOBS.items():
        step = _step_named(_job(workflow, name), BUILD_ARTIFACT_STEP)
        if step is None:
            problems.append(f"{name}: {BUILD_ARTIFACT_STEP!r} step is missing")
            continue
        segments = [_effective_lines(step)]
        problems.extend(
            _gate_violations(
                f"{name}: artifact build command", segments, (DESKTOP_BUILD_COMMAND,)
            )
        )
        if not _contains_marker(segments, spec["artifact"]):
            problems.append(f"{name}: artifact path {spec['artifact']!r} is missing")
        problems.extend(
            _gate_violations(
                f"{name}: delivered artifact tests", segments, (DESKTOP_ARTIFACT_COMMAND,)
            )
        )
        problems.extend(
            _gate_violations(
                f"{name}: artifact-mode desktop scenarios",
                segments,
                (DESKTOP_SOURCE_COMMAND,),
            )
        )
    return problems


def check_native_artifact_input(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, spec in DESKTOP_JOBS.items():
        step = _step_named(_job(workflow, name), BUILD_ARTIFACT_STEP)
        if step is None:
            problems.append(f"{name}: {BUILD_ARTIFACT_STEP!r} step is missing")
            continue
        segments = [_effective_lines(step)]
        if not _contains_marker(segments, spec["artifact_setup"]):
            problems.append(
                f"{name}: artifact input setup {spec['artifact_setup']!r} is missing"
            )
        if not _contains_marker(segments, spec["artifact_export"]):
            problems.append(
                f"{name}: {DESKTOP_ARTIFACT_ENV} export {spec['artifact_export']!r} is missing"
            )
    return problems


def check_native_stage_ordering(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, spec in DESKTOP_JOBS.items():
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        steps = _steps(job)
        names = [step.get("name") for step in steps]
        if SOURCE_SESSION_STEP not in names or BUILD_ARTIFACT_STEP not in names:
            problems.append(f"{name}: native source/session or artifact stage is missing")
            continue
        source_index = names.index(SOURCE_SESSION_STEP)
        build_index = names.index(BUILD_ARTIFACT_STEP)
        if source_index > build_index:
            problems.append(
                f"{name}: artifact build stage runs before the native source/session stage"
            )
        lines = _effective_lines(steps[build_index])
        stages = (
            lambda line: line == DESKTOP_BUILD_COMMAND,
            lambda line: spec["artifact_export"] in line,
            lambda line: line == DESKTOP_ARTIFACT_COMMAND,
            lambda line: line == DESKTOP_SOURCE_COMMAND,
        )
        positions = [
            next((index for index, line in enumerate(lines) if matches(line)), -1)
            for matches in stages
        ]
        if any(position < 0 for position in positions):
            continue
        if positions != sorted(positions) or len(set(positions)) != len(positions):
            problems.append(
                f"{name}: artifact stage order must be build, artifact input, "
                "artifact tests, then artifact-mode desktop scenarios"
            )
    return problems


def check_no_offscreen_substitute(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for job_name, job in _jobs(workflow).items():
        for step in _steps(job):
            env = _step_env(step)
            for key, value in env.items():
                if "QT_QPA_PLATFORM" in str(key) and "offscreen" in str(value).lower():
                    problems.append(
                        f"{job_name}: step {step.get('name')!r} sets QT_QPA_PLATFORM={value!r}"
                    )
            if any("offscreen" in line.lower() for line in _effective_lines(step)):
                problems.append(
                    f"{job_name}: step {step.get('name')!r} references an offscreen platform"
                )
    return problems


CHECKS: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "workflow_trigger": check_workflow_trigger,
    "job_matrix": check_job_matrix,
    "shell_matrix": check_shell_matrix,
    "baseline_source_suite": check_baseline_source_suite,
    "baseline_ruff": check_baseline_ruff,
    "baseline_wheel": check_baseline_wheel,
    "baseline_installed_wheel": check_baseline_installed_wheel,
    "baseline_browser_prerequisites": check_baseline_browser_prerequisites,
    "linux_service_lane": check_linux_service_lane,
    "documented_install_lane": check_documented_install_lane,
    "powershell_failure_propagation": check_powershell_failure_propagation,
    "posix_failure_propagation": check_posix_failure_propagation,
    "no_continue_on_error": check_no_continue_on_error,
    "native_environment": check_native_environment,
    "native_dependencies": check_native_dependencies,
    "native_source_lane": check_native_source_lane,
    "native_session_preflight": check_native_session_preflight,
    "native_artifact_lane": check_native_artifact_lane,
    "native_artifact_input": check_native_artifact_input,
    "native_stage_ordering": check_native_stage_ordering,
    "no_offscreen_substitute": check_no_offscreen_substitute,
}


def contract_violations(workflow: dict[str, Any]) -> dict[str, list[str]]:
    """Return every failing check, keyed by check name."""

    return {name: check(workflow) for name, check in CHECKS.items() if check(workflow)}


# --------------------------------------------------------------------------
# The real workflow satisfies every established check
# --------------------------------------------------------------------------


@pytest.mark.parametrize("check_name", sorted(CHECKS))
def test_established_workflow_check_has_no_violations(check_name: str) -> None:
    assert CHECKS[check_name](load_workflow()) == []


def test_real_workflow_satisfies_the_established_contract() -> None:
    violations = contract_violations(load_workflow())
    assert violations == {}


# --------------------------------------------------------------------------
# Natural mutations are rejected by their matching check
# --------------------------------------------------------------------------


def _mutate_remove_baseline_source_suite(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx", "Verify package and installed service")
    step["run"] = _run_text(step).replace(
        '"$runner_python" -m pytest tests/ -v\n', "", 1
    )


def _mutate_remove_baseline_ruff(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-macos", "Verify package and installed lifecycle")
    step["run"] = _run_text(step).replace("ruff check .\n", "", 1)


def _mutate_remove_baseline_wheel_build(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx", "Verify package and installed service")
    step["run"] = _run_text(step).replace(
        '"$runner_python" -m build --wheel\n', "", 1
    )


def _mutate_weaken_one_wheel_assertion(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-macos", "Verify package and installed lifecycle")
    step["run"] = _run_text(step).replace(
        'test "$wheel_count" -eq 1', 'test "$wheel_count" -ge 0', 1
    )


def _mutate_weaken_windows_wheel_enforcement(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-windows", "Verify package and installed lifecycle")
    text = _run_text(step)
    throw = 'throw "Expected exactly one wheel, found $($wheels.Count)"'
    assert throw in text
    step["run"] = text.replace(throw, 'Write-Host "unexpected wheel count"', 1)


def _mutate_remove_installed_wheel_test(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-macos", "Verify package and installed lifecycle")
    step["run"] = _run_text(step).replace(
        '"$runner_python" -m pytest tests/test_wheel_install.py -v\n', "", 1
    )


def _mutate_remove_browser_prerequisites(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx", "Verify package and installed service")
    step["run"] = _run_text(step).replace(
        '"$runner_python" -m playwright install --with-deps chromium\n', "", 1
    )


def _mutate_remove_linux_service_lane(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx", "Verify package and installed service")
    text = _run_text(step)
    head, separator, _tail = text.partition("nyx_verify_user=nyx-verify")
    assert separator, "expected the disposable-account service lane in the nyx job"
    step["run"] = head


def _mutate_omit_linux_service_cleanup(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx", "Verify package and installed service")
    text = _run_text(step)
    assert "trap cleanup EXIT\n" in text
    text = text.replace("trap cleanup EXIT\n", "", 1)
    text = text.replace('sudo userdel --remove "$nyx_verify_user"', "true", 1)
    step["run"] = text


def _mutate_remove_documented_install_gate(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-windows", "Verify documented installation commands")
    env = dict(_step_env(step))
    env.pop(DOCUMENTED_INSTALL_ENV, None)
    step["env"] = env


def _mutate_remove_desktop_build_dependencies(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-desktop-windows", SOURCE_SESSION_STEP)
    step["run"] = _run_text(step).replace(
        DESKTOP_EXTRA_INSTALL, "python -m pip install -e '.[test]'", 1
    )


def _mutate_remove_required_native_environment(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-desktop-macos", SOURCE_SESSION_STEP)
    env = dict(_step_env(step))
    env.pop(REQUIRED_NATIVE_ENV, None)
    step["env"] = env


def _mutate_override_required_native_host(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-desktop-windows", SOURCE_SESSION_STEP)
    env = dict(_step_env(step))
    env[EXPECTED_NATIVE_HOST_ENV] = "windows-latest"
    step["env"] = env


def _mutate_remove_native_source_session_command(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-desktop-macos", SOURCE_SESSION_STEP)
    step["run"] = _run_text(step).replace(f"{DESKTOP_SOURCE_COMMAND}\n", "", 1)


def _mutate_remove_session_preflight(workflow: dict[str, Any]) -> None:
    job = _job(workflow, "nyx-desktop-windows")
    job["steps"] = [
        step for step in _steps(job) if step.get("name") != PREFLIGHT_STEP
    ]


def _mutate_weaken_session_preflight(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-desktop-macos", PREFLIGHT_STEP)
    step["run"] = _run_text(step).replace('test "$session_manager" = "Aqua"', "true", 1)


def _mutate_omit_windows_session_gate(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-desktop-windows", PREFLIGHT_STEP)
    text = _run_text(step)
    gate = (
        "if (-not $interactive) {\n"
        "  throw 'windows-2025 does not expose an interactive desktop session'\n"
        "}\n"
    )
    assert gate in text
    step["run"] = text.replace(gate, "", 1)


def _mutate_remove_artifact_tests(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-desktop-windows", BUILD_ARTIFACT_STEP)
    step["run"] = _run_text(step).replace(f"{DESKTOP_ARTIFACT_COMMAND}\n", "", 1)


def _mutate_remove_artifact_mode_desktop_run(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-desktop-macos", BUILD_ARTIFACT_STEP)
    step["run"] = _run_text(step).replace(f"{DESKTOP_SOURCE_COMMAND}\n", "", 1)


def _mutate_remove_artifact_environment(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-desktop-windows", BUILD_ARTIFACT_STEP)
    step["run"] = _run_text(step).replace(
        "$env:NYX_DESKTOP_ARTIFACT = $artifact\n", "", 1
    )


def _mutate_override_artifact_input(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-desktop-macos", BUILD_ARTIFACT_STEP)
    step["run"] = _run_text(step).replace(
        'artifact="$PWD/dist/desktop/Nyx.app"', 'artifact="$PWD/dist"', 1
    )


def _mutate_move_build_after_artifact_tests(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-desktop-windows", BUILD_ARTIFACT_STEP)
    text = _run_text(step)
    build_line = f"{DESKTOP_BUILD_COMMAND}\n"
    assert build_line in text
    step["run"] = text.replace(build_line, "", 1) + build_line


def _mutate_move_build_step_before_source_step(workflow: dict[str, Any]) -> None:
    job = _job(workflow, "nyx-desktop-macos")
    steps = _steps(job)
    names = [step.get("name") for step in steps]
    source_index = names.index(SOURCE_SESSION_STEP)
    build_index = names.index(BUILD_ARTIFACT_STEP)
    assert source_index < build_index
    steps[source_index], steps[build_index] = steps[build_index], steps[source_index]
    job["steps"] = steps


def _mutate_omit_powershell_failure_propagation(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-windows", "Verify package and installed lifecycle")
    lines = _run_text(step).splitlines()
    kept: list[str] = []
    drop_next_guard = False
    for line in lines:
        if drop_next_guard and "$LASTEXITCODE" in line:
            drop_next_guard = False
            continue
        kept.append(line)
        if line.strip() == "python -m pytest tests/ -v":
            drop_next_guard = True
    step["run"] = "\n".join(kept) + "\n"


def _mutate_omit_posix_failure_propagation(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx", "Verify package and installed service")
    text = _run_text(step)
    assert f"{POSIX_FAILURE_GUARD}\n" in text
    step["run"] = text.replace(f"{POSIX_FAILURE_GUARD}\n", "", 1)


def _mutate_substitute_offscreen_platform(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-desktop-windows", SOURCE_SESSION_STEP)
    env = dict(_step_env(step))
    env["QT_QPA_PLATFORM"] = "offscreen"
    step["env"] = env


def _mutate_remove_push_trigger(workflow: dict[str, Any]) -> None:
    for key in list(workflow):
        if key is True or str(key).lower() in ("on", "true"):
            assert "push" in _events(workflow)
            del workflow[key]


def _mutate_change_baseline_runner(workflow: dict[str, Any]) -> None:
    _job(workflow, "nyx")["runs-on"] = "ubuntu-22.04"


def _mutate_remove_pwsh_shell_declaration(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-windows", "Verify package and installed lifecycle")
    assert step.get("shell") == "pwsh"
    step.pop("shell")


LINUX_SERVICE_STEP = "Verify package and installed service"
BASELINE_LIFECYCLE_STEP = "Verify package and installed lifecycle"
DOCUMENTED_INSTALL_STEP = "Verify documented installation commands"


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _comment_out(line: str) -> str:
    return f"{_indent(line)}# {line.lstrip()}"


def _append(suffix: str) -> Callable[[str], str]:
    return lambda line: f"{line}{suffix}"


def _insert_before(new_line: str) -> Callable[[str], str]:
    return lambda line: f"{_indent(line)}{new_line}\n{line}"


def _rewrite_run_line(
    workflow: dict[str, Any],
    job_name: str,
    step_name: str,
    target: str,
    transform: Callable[[str], str],
) -> None:
    """Rewrite the first physical run line whose stripped text is ``target``."""

    step = _step(workflow, job_name, step_name)
    physical = _run_text(step).splitlines()
    matches = [index for index, line in enumerate(physical) if line.strip() == target]
    assert matches, f"{job_name}: step {step_name!r} has no line {target!r}"
    index = matches[0]
    physical[index : index + 1] = transform(physical[index]).split("\n")
    step["run"] = "\n".join(physical) + "\n"


def _mutate_comment_out_baseline_source_suite(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow, "nyx", LINUX_SERVICE_STEP, '"$runner_python" -m pytest tests/ -v', _comment_out
    )


def _mutate_comment_out_windows_source_suite(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow, "nyx-windows", BASELINE_LIFECYCLE_STEP, "python -m pytest tests/ -v", _comment_out
    )


def _mutate_disable_errexit_before_source_suite(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx",
        LINUX_SERVICE_STEP,
        '"$runner_python" -m pytest tests/ -v',
        _insert_before("set +e"),
    )


def _mutate_disable_errexit_by_name(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx-macos",
        BASELINE_LIFECYCLE_STEP,
        RUFF_COMMAND,
        _insert_before("set +o errexit"),
    )


def _mutate_disable_pipefail(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx-desktop-macos",
        BUILD_ARTIFACT_STEP,
        DESKTOP_ARTIFACT_COMMAND,
        _insert_before("set +o pipefail"),
    )


def _mutate_move_posix_guard(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-macos", BASELINE_LIFECYCLE_STEP)
    lines = _run_text(step).splitlines()
    assert lines[0] == POSIX_FAILURE_GUARD
    step["run"] = "\n".join([lines[1], lines[0], *lines[2:]]) + "\n"


def _mutate_ignore_baseline_ruff_failure(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow, "nyx-macos", BASELINE_LIFECYCLE_STEP, RUFF_COMMAND, _append(" || true")
    )


def _mutate_exit_zero_after_installed_wheel(workflow: dict[str, Any]) -> None:
    # The nyx job carries an intact copy of this line later in the same step,
    # so only the altered-copy rule can catch this edit.
    _rewrite_run_line(
        workflow,
        "nyx",
        LINUX_SERVICE_STEP,
        '"$runner_python" -m pytest tests/test_wheel_install.py -v',
        _append("; exit 0"),
    )


def _mutate_background_desktop_source_command(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow, "nyx-desktop-macos", SOURCE_SESSION_STEP, DESKTOP_SOURCE_COMMAND, _append(" &")
    )


def _mutate_ignore_artifact_test_failure(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx-desktop-macos",
        BUILD_ARTIFACT_STEP,
        DESKTOP_ARTIFACT_COMMAND,
        _append(" || true"),
    )


def _mutate_comment_out_desktop_build(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow, "nyx-desktop-windows", BUILD_ARTIFACT_STEP, DESKTOP_BUILD_COMMAND, _comment_out
    )


def _mutate_comment_out_desktop_extras_install(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow, "nyx-desktop-macos", SOURCE_SESSION_STEP, DESKTOP_EXTRA_INSTALL, _comment_out
    )


def _mutate_comment_out_documented_install(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow, "nyx-macos", DOCUMENTED_INSTALL_STEP, DOCUMENTED_INSTALL_COMMAND, _comment_out
    )


def _mutate_ignore_installed_service_failure(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx",
        LINUX_SERVICE_STEP,
        'nyx-service-test "$nyx_verify_root/exercise" "$nyx_verify_root/venv/bin/python"',
        _append(" || true"),
    )


def _mutate_comment_out_installed_service_line(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx",
        LINUX_SERVICE_STEP,
        'PATH="$nyx_verify_home/.local/bin:/usr/bin:/bin" \\',
        _comment_out,
    )


def _mutate_ignore_one_wheel_count(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow, "nyx", LINUX_SERVICE_STEP, 'test "$wheel_count" -eq 1', _append(" || true")
    )


def _mutate_comment_out_aqua_gate(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx-desktop-macos",
        PREFLIGHT_STEP,
        'test "$session_manager" = "Aqua"',
        _comment_out,
    )


def _mutate_late_powershell_error_preference(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-windows", DOCUMENTED_INSTALL_STEP)
    lines = _run_text(step).splitlines()
    assert lines[0] == PWSH_ERROR_PREFERENCE
    step["run"] = "\n".join([*lines[1:], lines[0]]) + "\n"


def _mutate_reassign_powershell_error_preference(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx-windows",
        BASELINE_LIFECYCLE_STEP,
        "python -m pytest tests/ -v",
        _insert_before("$ErrorActionPreference = 'Continue'"),
    )


def _mutate_comment_out_powershell_exit_guard(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-desktop-windows", BUILD_ARTIFACT_STEP)
    physical = _run_text(step).splitlines()
    build = physical.index(DESKTOP_BUILD_COMMAND)
    assert physical[build + 1] == PWSH_FAILURE_GUARD
    physical[build + 1] = _comment_out(physical[build + 1])
    step["run"] = "\n".join(physical) + "\n"


def _mutate_continue_job_on_error(workflow: dict[str, Any]) -> None:
    _job(workflow, "nyx-macos")[CONTINUE_ON_ERROR_KEY] = True


def _mutate_continue_step_on_error(workflow: dict[str, Any]) -> None:
    _step(workflow, "nyx-desktop-windows", SOURCE_SESSION_STEP)[CONTINUE_ON_ERROR_KEY] = True


MUTATIONS: dict[str, tuple[Callable[[dict[str, Any]], list[str]], Callable[[dict[str, Any]], None]]] = {
    "remove baseline source suite": (
        check_baseline_source_suite,
        _mutate_remove_baseline_source_suite,
    ),
    "remove baseline ruff": (check_baseline_ruff, _mutate_remove_baseline_ruff),
    "remove baseline wheel build": (
        check_baseline_wheel,
        _mutate_remove_baseline_wheel_build,
    ),
    "weaken one-wheel assertion": (
        check_baseline_wheel,
        _mutate_weaken_one_wheel_assertion,
    ),
    "weaken windows one-wheel enforcement": (
        check_baseline_wheel,
        _mutate_weaken_windows_wheel_enforcement,
    ),
    "remove installed-wheel test": (
        check_baseline_installed_wheel,
        _mutate_remove_installed_wheel_test,
    ),
    "remove browser prerequisites": (
        check_baseline_browser_prerequisites,
        _mutate_remove_browser_prerequisites,
    ),
    "remove linux service lane": (
        check_linux_service_lane,
        _mutate_remove_linux_service_lane,
    ),
    "omit linux service cleanup": (
        check_linux_service_lane,
        _mutate_omit_linux_service_cleanup,
    ),
    "remove documented install gate": (
        check_documented_install_lane,
        _mutate_remove_documented_install_gate,
    ),
    "remove desktop/build dependencies": (
        check_native_dependencies,
        _mutate_remove_desktop_build_dependencies,
    ),
    "remove required-native environment": (
        check_native_environment,
        _mutate_remove_required_native_environment,
    ),
    "override required-native host": (
        check_native_environment,
        _mutate_override_required_native_host,
    ),
    "remove native source/session command": (
        check_native_source_lane,
        _mutate_remove_native_source_session_command,
    ),
    "remove session preflight": (
        check_native_session_preflight,
        _mutate_remove_session_preflight,
    ),
    "weaken session preflight": (
        check_native_session_preflight,
        _mutate_weaken_session_preflight,
    ),
    "omit windows session gate": (
        check_native_session_preflight,
        _mutate_omit_windows_session_gate,
    ),
    "remove artifact tests": (
        check_native_artifact_lane,
        _mutate_remove_artifact_tests,
    ),
    "remove artifact-mode desktop run": (
        check_native_artifact_lane,
        _mutate_remove_artifact_mode_desktop_run,
    ),
    "remove artifact environment": (
        check_native_artifact_input,
        _mutate_remove_artifact_environment,
    ),
    "override artifact input": (
        check_native_artifact_input,
        _mutate_override_artifact_input,
    ),
    "move build after artifact tests": (
        check_native_stage_ordering,
        _mutate_move_build_after_artifact_tests,
    ),
    "move build step before source step": (
        check_native_stage_ordering,
        _mutate_move_build_step_before_source_step,
    ),
    "omit powershell failure propagation": (
        check_powershell_failure_propagation,
        _mutate_omit_powershell_failure_propagation,
    ),
    "omit POSIX failure propagation": (
        check_posix_failure_propagation,
        _mutate_omit_posix_failure_propagation,
    ),
    "substitute offscreen platform": (
        check_no_offscreen_substitute,
        _mutate_substitute_offscreen_platform,
    ),
    "remove push trigger": (check_workflow_trigger, _mutate_remove_push_trigger),
    "change baseline runner": (check_job_matrix, _mutate_change_baseline_runner),
    "remove pwsh shell declaration": (
        check_shell_matrix,
        _mutate_remove_pwsh_shell_declaration,
    ),
    "comment out baseline source suite": (
        check_baseline_source_suite,
        _mutate_comment_out_baseline_source_suite,
    ),
    "comment out windows source suite": (
        check_baseline_source_suite,
        _mutate_comment_out_windows_source_suite,
    ),
    "set +e before baseline source suite": (
        check_posix_failure_propagation,
        _mutate_disable_errexit_before_source_suite,
    ),
    "set +o errexit": (check_posix_failure_propagation, _mutate_disable_errexit_by_name),
    "set +o pipefail": (check_posix_failure_propagation, _mutate_disable_pipefail),
    "move POSIX guard below first effective line": (
        check_posix_failure_propagation,
        _mutate_move_posix_guard,
    ),
    "ignore baseline ruff failure": (
        check_baseline_ruff,
        _mutate_ignore_baseline_ruff_failure,
    ),
    "exit zero after installed-wheel test": (
        check_baseline_installed_wheel,
        _mutate_exit_zero_after_installed_wheel,
    ),
    "background native source/session command": (
        check_native_source_lane,
        _mutate_background_desktop_source_command,
    ),
    "ignore delivered artifact test failure": (
        check_native_artifact_lane,
        _mutate_ignore_artifact_test_failure,
    ),
    "comment out desktop build command": (
        check_native_artifact_lane,
        _mutate_comment_out_desktop_build,
    ),
    "comment out desktop extras install": (
        check_native_dependencies,
        _mutate_comment_out_desktop_extras_install,
    ),
    "comment out documented install command": (
        check_documented_install_lane,
        _mutate_comment_out_documented_install,
    ),
    "ignore installed-service failure": (
        check_linux_service_lane,
        _mutate_ignore_installed_service_failure,
    ),
    "comment out installed-service command line": (
        check_linux_service_lane,
        _mutate_comment_out_installed_service_line,
    ),
    "ignore one-wheel count failure": (
        check_baseline_wheel,
        _mutate_ignore_one_wheel_count,
    ),
    "comment out Aqua session gate": (
        check_native_session_preflight,
        _mutate_comment_out_aqua_gate,
    ),
    "late powershell error preference": (
        check_powershell_failure_propagation,
        _mutate_late_powershell_error_preference,
    ),
    "reassign powershell error preference": (
        check_powershell_failure_propagation,
        _mutate_reassign_powershell_error_preference,
    ),
    "comment out powershell exit guard": (
        check_powershell_failure_propagation,
        _mutate_comment_out_powershell_exit_guard,
    ),
    "continue job on error": (check_no_continue_on_error, _mutate_continue_job_on_error),
    "continue step on error": (check_no_continue_on_error, _mutate_continue_step_on_error),
}


@pytest.mark.parametrize("label", sorted(MUTATIONS))
def test_natural_workflow_mutation_is_rejected(label: str) -> None:
    check, mutate = MUTATIONS[label]
    original = load_workflow()
    assert check(original) == [], (
        f"{check.__name__} already reports violations on the real workflow"
    )
    mutated = copy.deepcopy(original)
    mutate(mutated)
    problems = check(mutated)
    assert problems, f"{check.__name__} did not reject the mutation: {label}"


# --------------------------------------------------------------------------
# The effective-line reader and the rules it deliberately leaves alone
# --------------------------------------------------------------------------


def test_effective_lines_strip_and_drop_blank_and_comment_lines() -> None:
    step = {
        "run": (
            "set -euo pipefail\n"
            "\n"
            "  # python -m pytest tests/ -v\n"
            "    python -m pytest tests/ -v  \n"
            "echo done # trailing comment stays\n"
        )
    }
    assert _effective_lines(step) == [
        "set -euo pipefail",
        "python -m pytest tests/ -v",
        "echo done # trailing comment stays",
    ]


def test_tracing_toggle_after_posix_guard_is_not_a_failure_propagation_violation() -> None:
    mutated = copy.deepcopy(load_workflow())
    _rewrite_run_line(
        mutated,
        "nyx",
        LINUX_SERVICE_STEP,
        '"$runner_python" -m pytest tests/ -v',
        _insert_before("set +x"),
    )
    assert check_posix_failure_propagation(mutated) == []


def test_best_effort_cleanup_stop_remains_a_substring_marker() -> None:
    workflow = load_workflow()
    cleanup_stop = next(
        line
        for line in _effective_lines(_step(workflow, "nyx", LINUX_SERVICE_STEP))
        if LINUX_SERVICE_MARKERS[0] in line
    )
    assert cleanup_stop.endswith("|| true")
    assert check_linux_service_lane(workflow) == []
