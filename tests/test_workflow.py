"""Behavioral contract checks for the hosted CI workflow.

These tests parse the real workflow document and assert the operational
contract that the native source/session lane and the desktop build/artifact
extension establish: exact commands, installer extras, artifact inputs,
required-native environment, interactive-session preflight, stage ordering,
and non-zero exit propagation.  Each supported behavior is proved to be
load-bearing by mutating the parsed document — a natural edit that removes or
weakens the behavior — and asserting that the matching check rejects it.
"""

from __future__ import annotations

import copy
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

ONE_WHEEL_MARKERS = {
    "nyx": ("find dist -maxdepth 1 -type f -name '*.whl'", 'test "$wheel_count" -eq 1'),
    "nyx-windows": (
        "Get-ChildItem -Path dist -Filter '*.whl' -File",
        "$wheels.Count -ne 1",
        'throw "Expected exactly one wheel, found $($wheels.Count)"',
    ),
    "nyx-macos": ("find dist -maxdepth 1 -type f -name '*.whl'", 'test "$wheel_count" -eq 1'),
}

LINUX_SERVICE_MARKERS = (
    "tests/test_installed_service.py",
    "sudo useradd --create-home --shell /bin/bash",
    "sudo userdel --remove",
    "trap cleanup EXIT",
    "timeout --preserve-status 90s",
    '"$nyx_verify_root/venv/bin/nyx" --stop',
)

SESSION_PREFLIGHT_MARKERS = {
    "nyx-desktop-windows": (
        "$env:ImageOS",
        "$env:ImageVersion",
        "[Environment]::UserInteractive",
        "$interactive",
        "'amd64', 'x86_64'",
        "if (-not $interactive)",
        "does not expose an interactive desktop session",
    ),
    "nyx-desktop-macos": (
        "${ImageOS:-}",
        "${ImageVersion:-}",
        "launchctl managername",
        "/dev/console",
        '"Aqua"',
        "'arm64'",
    ),
}


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


def _job_run_text(job: Any) -> str:
    return "\n".join(_run_text(step) for step in _steps(job))


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
        if command not in _job_run_text(job):
            problems.append(f"{name}: source-suite command {command!r} is missing")
    return problems


def check_baseline_ruff(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name in BASELINE_JOBS:
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        if RUFF_COMMAND not in _job_run_text(job):
            problems.append(f"{name}: Ruff command {RUFF_COMMAND!r} is missing")
    return problems


def check_baseline_wheel(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, spec in BASELINE_JOBS.items():
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        text = _job_run_text(job)
        build = WHEEL_BUILD_COMMAND.format(python=spec["python"])
        if build not in text:
            problems.append(f"{name}: wheel build command {build!r} is missing")
        for marker in ONE_WHEEL_MARKERS[name]:
            if marker not in text:
                problems.append(f"{name}: one-wheel assertion {marker!r} is missing")
    return problems


def check_baseline_installed_wheel(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, spec in BASELINE_JOBS.items():
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        command = INSTALLED_WHEEL_COMMAND.format(python=spec["python"])
        if command not in _job_run_text(job):
            problems.append(f"{name}: installed-wheel command {command!r} is missing")
    return problems


def check_baseline_browser_prerequisites(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, command in BROWSER_INSTALL_COMMANDS.items():
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        if command not in _job_run_text(job):
            problems.append(f"{name}: browser prerequisite {command!r} is missing")
    return problems


def check_linux_service_lane(workflow: dict[str, Any]) -> list[str]:
    job = _job(workflow, "nyx")
    if job is None:
        return ["nyx: job is missing"]
    text = _job_run_text(job)
    return [
        f"nyx: Linux installed-service marker {marker!r} is missing"
        for marker in LINUX_SERVICE_MARKERS
        if marker not in text
    ]


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
        if DOCUMENTED_INSTALL_COMMAND not in _run_text(step):
            problems.append(
                f"{name}: documented installation command {DOCUMENTED_INSTALL_COMMAND!r} is missing"
            )
    return problems


def _unguarded_pwsh_commands(name: str, step: dict[str, Any]) -> list[str]:
    lines = [line.strip() for line in _run_text(step).splitlines()]
    problems: list[str] = []
    for index, line in enumerate(lines):
        if not (line.startswith("python ") or line.startswith("ruff ")):
            continue
        guard = ""
        for follower in lines[index + 1 :]:
            if not follower or follower.startswith("#"):
                continue
            guard = follower
            break
        if "$LASTEXITCODE" not in guard or "exit $LASTEXITCODE" not in guard:
            problems.append(
                f"{name}: step {step.get('name')!r} command {line!r} lacks a "
                "$LASTEXITCODE exit guard"
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
            if step.get("shell") != "pwsh" or not _run_text(step):
                continue
            if "$ErrorActionPreference = 'Stop'" not in _run_text(step):
                problems.append(
                    f"{name}: step {step.get('name')!r} does not set ErrorActionPreference=Stop"
                )
            problems.extend(_unguarded_pwsh_commands(name, step))
    return problems


def check_posix_failure_propagation(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name in POSIX_JOBS:
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        for step in _steps(job):
            if not _run_text(step) or step.get("shell") == "pwsh":
                continue
            if POSIX_FAILURE_GUARD not in _run_text(step):
                problems.append(
                    f"{name}: step {step.get('name')!r} does not enable "
                    f"{POSIX_FAILURE_GUARD!r} shell failure propagation"
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
        if DESKTOP_EXTRA_INSTALL not in _run_text(step):
            problems.append(
                f"{name}: native source/session lane does not install {DESKTOP_EXTRA_INSTALL!r}"
            )
    return problems


def check_native_source_lane(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name in DESKTOP_JOBS:
        step = _step_named(_job(workflow, name), SOURCE_SESSION_STEP)
        if step is None:
            problems.append(f"{name}: {SOURCE_SESSION_STEP!r} step is missing")
            continue
        if DESKTOP_SOURCE_COMMAND not in _run_text(step):
            problems.append(
                f"{name}: native source/session lane does not run {DESKTOP_SOURCE_COMMAND!r}"
            )
    return problems


def check_native_session_preflight(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, markers in SESSION_PREFLIGHT_MARKERS.items():
        step = _step_named(_job(workflow, name), PREFLIGHT_STEP)
        if step is None:
            problems.append(f"{name}: {PREFLIGHT_STEP!r} step is missing")
            continue
        text = _run_text(step)
        for marker in markers:
            if marker not in text:
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
        text = _run_text(step)
        if DESKTOP_BUILD_COMMAND not in text:
            problems.append(f"{name}: artifact build command {DESKTOP_BUILD_COMMAND!r} is missing")
        if spec["artifact"] not in text:
            problems.append(f"{name}: artifact path {spec['artifact']!r} is missing")
        if DESKTOP_ARTIFACT_COMMAND not in text:
            problems.append(
                f"{name}: delivered artifact tests {DESKTOP_ARTIFACT_COMMAND!r} are missing"
            )
        if DESKTOP_SOURCE_COMMAND not in text:
            problems.append(
                f"{name}: artifact-mode desktop scenarios {DESKTOP_SOURCE_COMMAND!r} are missing"
            )
    return problems


def check_native_artifact_input(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, spec in DESKTOP_JOBS.items():
        step = _step_named(_job(workflow, name), BUILD_ARTIFACT_STEP)
        if step is None:
            problems.append(f"{name}: {BUILD_ARTIFACT_STEP!r} step is missing")
            continue
        text = _run_text(step)
        if spec["artifact_setup"] not in text:
            problems.append(
                f"{name}: artifact input setup {spec['artifact_setup']!r} is missing"
            )
        if spec["artifact_export"] not in text:
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
        text = _run_text(steps[build_index])
        markers = (
            DESKTOP_BUILD_COMMAND,
            spec["artifact_export"],
            DESKTOP_ARTIFACT_COMMAND,
            DESKTOP_SOURCE_COMMAND,
        )
        positions = [text.find(marker) for marker in markers]
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
            if "offscreen" in _run_text(step).lower():
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
