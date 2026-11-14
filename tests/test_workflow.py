"""Behavioral contract checks for the hosted CI workflow.

These tests parse the real workflow document and assert the operational
contract that the native source/session lane and the desktop build/artifact
extension establish: exact commands, installer extras, artifact inputs,
required-native environment, interactive-session preflight, stage ordering,
job time limits and non-zero exit propagation.  The trigger and the lane
routing are pinned exactly too: the workflow triggers only on an unconfigured
push and workflow_dispatch, the reuse gate is the one job outside the lane map,
every lane depends on only the gate and triggers under exactly its group's
reuse-aware condition, and no step carries a condition.  A guard-property check
evaluates those parsed conditions to prove that a successful branch push run or
dispatch run ran every lane of its group, which is what lets a push to main
reuse it, and lane text may not read the ref or event.  Checks read each step's
effective lines (the stripped lines left after dropping blank lines and
full-line comments) and match gating commands exactly, so a commented-out or
failure-masked command does not satisfy them.  Each supported behavior is
proved to be load-bearing by mutating the parsed document — a natural edit
that removes or weakens the behavior — and asserting that the matching check
rejects it.
"""

from __future__ import annotations

import ast
import copy
import re
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

REPO_ROOT = Path(__file__).parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "tests.yml"

# The established lane matrix.  Baseline jobs keep their latest labels; the
# desktop lanes pin the explicit feasibility hosts.
BASELINE_JOBS = {
    "nyx": {"runs_on": "ubuntu-latest", "python": '"$runner_python"'},
    "nyx-windows": {"runs_on": "windows-latest", "python": "python"},
    "nyx-macos": {"runs_on": "macos-latest", "python": '"$runner_python"'},
}
# The source suite runs in sharded jobs, one per package lane: each must carry
# the runner label and interpreter of its ``package_job`` entry in
# ``BASELINE_JOBS`` (``suite_pairing_violations`` rejects a difference) and
# splits the suite into ``shards`` matrix entries.  A shard-count change is an
# edit of this table and of the job's matrix list.
SUITE_JOBS = {
    "nyx-suite": {
        "runs_on": "ubuntu-latest",
        "python": '"$runner_python"',
        "package_job": "nyx",
        "shards": 2,
    },
    "nyx-windows-suite": {
        "runs_on": "windows-latest",
        "python": "python",
        "package_job": "nyx-windows",
        "shards": 3,
    },
    "nyx-macos-suite": {
        "runs_on": "macos-latest",
        "python": '"$runner_python"',
        "package_job": "nyx-macos",
        "shards": 2,
    },
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

POWERSHELL_JOBS = ("nyx-windows", "nyx-windows-suite", "nyx-desktop-windows")
POSIX_JOBS = ("nyx", "nyx-suite", "nyx-macos", "nyx-macos-suite", "nyx-desktop-macos")

REQUIRED_NATIVE_ENV = "NYX_REQUIRE_NATIVE_DESKTOP"
EXPECTED_NATIVE_HOST_ENV = "NYX_EXPECTED_NATIVE_HOST"
DESKTOP_ARTIFACT_ENV = "NYX_DESKTOP_ARTIFACT"
DOCUMENTED_INSTALL_ENV = "NYX_VERIFY_DOCUMENTED_INSTALL"
DOCUMENTED_BUILD_ENV = "NYX_VERIFY_DOCUMENTED_BUILD"

# The trigger mapping is exact: both events carry no configuration, so there is
# no branch, path or tag filter and no dispatch input.  Pushes build the
# package and suite lanes; the native desktop lanes wait for dispatched runs
# and main pushes.  A push to main consults the reuse gate and skips a group
# whose identical-tree results already passed; every lane's condition starts
# with a status check so a skipped or failed gate still lets it run.
TRIGGER_EVENTS = {"push": None, "workflow_dispatch": None}
GATE_JOB = "reuse-gate"
PACKAGE_LANE_CONDITION = (
    "!cancelled() && github.event_name == 'push' && "
    "(github.ref != 'refs/heads/main' || "
    "needs.reuse-gate.outputs.reuse_source != 'true')"
)
DESKTOP_LANE_CONDITION = (
    "!cancelled() && (github.event_name == 'workflow_dispatch' || "
    "(github.ref == 'refs/heads/main' && "
    "needs.reuse-gate.outputs.reuse_desktop != 'true'))"
)
LANE_ROUTING_CONDITIONS = {
    **{name: PACKAGE_LANE_CONDITION for name in BASELINE_JOBS},
    **{name: PACKAGE_LANE_CONDITION for name in SUITE_JOBS},
    **{name: DESKTOP_LANE_CONDITION for name in DESKTOP_JOBS},
}

GATE_CONDITION = "github.event_name == 'push' && github.ref == 'refs/heads/main'"
GATE_PERMISSIONS = {"contents": "read", "actions": "read"}
GATE_SCRIPT = ".github/scripts/reuse_gate.py"
GATE_COMMAND = f"python3 {GATE_SCRIPT}"
GATE_OUTPUT_NAMES = ("reuse_source", "reuse_desktop")
GATE_CHECKOUT = {
    "uses": "actions/checkout@v4",
    "with": {"sparse-checkout": ".github/scripts", "persist-credentials": False},
}
GATE_TOKEN_ENV = {"GITHUB_TOKEN": "${{ github.token }}"}

LINUX_SERVICE_STEP = "Verify package and installed service"
BASELINE_LIFECYCLE_STEP = "Verify package and installed lifecycle"
DOCUMENTED_INSTALL_STEP = "Verify documented installation commands"
SOURCE_SESSION_STEP = "Verify native desktop source and session"
DOCUMENTED_BUILD_STEP = "Build the documented desktop artifact"
BUILD_ARTIFACT_STEP = "Build and verify the native desktop artifact"
PREFLIGHT_STEP = "Record native host and session preflight"

SOURCE_SUITE_COMMAND = "{python} -m pytest tests/ -v"
SHARD_OPTION = "--nyx-shard"
SHARD_SUFFIX = " --nyx-shard=${{ strategy.job-index }}/${{ strategy.job-total }}"
SHARDED_SUITE_COMMAND = SOURCE_SUITE_COMMAND + SHARD_SUFFIX
SUITE_INSTALL_COMMAND = "{python} -m pip install -e '.[test]' build"
SUITE_STEP = "Run the source suite shard"
RUFF_COMMAND = "ruff check ."
WHEEL_BUILD_COMMAND = "{python} -m build --wheel"
INSTALLED_WHEEL_COMMAND = "{python} -m pytest tests/test_wheel_install.py -v"
DOCUMENTED_INSTALL_COMMAND = (
    "python -m pytest tests/test_documented_install.py::test_documented_native_installation_and_lifecycle -v"
)
DOCUMENTED_ARTIFACT_INSTALL_COMMAND = (
    "python -m pytest tests/test_documented_install.py::test_documented_native_installation_and_lifecycle -v -s"
)
DESKTOP_SOURCE_COMMAND = "python -m pytest tests/test_desktop.py -v -s"
DESKTOP_ARTIFACT_COMMAND = "python -m pytest tests/test_desktop_artifact.py -v -s"
DOCUMENTED_BUILD_COMMAND = (
    "python -m pytest tests/test_documented_install.py::test_documented_desktop_build_blocks_run_literally -v -s"
)
# The published build commands live only in the guide.  The workflow holds no
# copy and no other build command, so any of these tokens in a desktop job is a
# copied or direct build (the guide's blocks all create or use ``.venv``).
FORBIDDEN_BUILD_MARKERS = (
    "build_desktop",
    ".venv",
    "-m venv",
    "vswhere",
    "vcvars",
)
DESKTOP_EXTRA_INSTALL = "python -m pip install -e '.[test,desktop,build]'"
PWSH_FAILURE_GUARD = "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }"
POSIX_FAILURE_GUARD = "set -euo pipefail"

BROWSER_INSTALL_COMMANDS = {
    "nyx": '"$runner_python" -m playwright install --with-deps chromium',
    "nyx-suite": '"$runner_python" -m playwright install --with-deps chromium',
    "nyx-windows-suite": "python -m playwright install chromium",
    "nyx-macos-suite": '"$runner_python" -m playwright install chromium',
}

# The one run of the gated documented-install proof in each job that has it:
# the Linux package lane, and the artifact step of each desktop lane.
DOCUMENTED_INSTALL_STEPS = {
    "nyx": (DOCUMENTED_INSTALL_STEP, DOCUMENTED_INSTALL_COMMAND),
    "nyx-desktop-windows": (BUILD_ARTIFACT_STEP, DOCUMENTED_ARTIFACT_INSTALL_COMMAND),
    "nyx-desktop-macos": (BUILD_ARTIFACT_STEP, DOCUMENTED_ARTIFACT_INSTALL_COMMAND),
}

# Any effective line naming one of these files runs a once-only proof.  Each
# job's designated (step, command) pairs are the only places such a line may
# appear, each exactly once.
DESKTOP_SOURCE_TARGET = "tests/test_desktop.py"
DOCUMENTED_INSTALL_TARGET = "tests/test_documented_install.py"

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
# The continuation line that installs pytest, with the wheel, for the
# installed-service proof.
LINUX_SERVICE_PIP_CONTINUATION = '"$nyx_verify_wheel" pytest playwright'

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

# Every job caps its wall-clock duration so a hung run fails instead of holding
# a hosted runner for hours, and every job has a pin below.  A package or suite
# cap is twice the job's projected duration, rounded up to the next five
# minutes, with a floor of 10 minutes.  Projected durations: nyx-suite about
# 500 s, nyx-windows-suite about 765 s, nyx-macos-suite about 897 s (each the
# largest shard of its split plus the shared-world rebuild per extra shard),
# nyx about 116 s, nyx-windows about 66 s and nyx-macos about 52 s (the job
# minus its suite session).  A cap is re-derived when its projection changes.
TIMEOUT_KEY = "timeout-minutes"
TIMEOUT_MINIMUM_MINUTES = 1
TIMEOUT_MAXIMUM_MINUTES = 60
JOB_TIMEOUT_MINUTES = {
    GATE_JOB: 5,
    "nyx": 10,
    "nyx-windows": 10,
    "nyx-macos": 10,
    "nyx-suite": 20,
    "nyx-windows-suite": 30,
    "nyx-macos-suite": 30,
    "nyx-desktop-windows": 35,
    "nyx-desktop-macos": 15,
}
# A cancelled run is not evidence for the commit it ran on, so the workflow
# never cancels in-progress runs through a concurrency group.
CONCURRENCY_KEY = "concurrency"

# Any effective line naming this file runs the installed-wheel proof.
INSTALLED_WHEEL_TARGET = "tests/test_wheel_install.py"

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


_TRIGGER_KEYS = (True, "on", "true", "True")


def _events(workflow: dict[str, Any]) -> list[str]:
    if not isinstance(workflow, dict):
        return []
    for key in _TRIGGER_KEYS:
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


def _trigger_value(workflow: dict[str, Any]) -> Any:
    """Read the trigger value through the key spellings ``on`` parses to."""

    if not isinstance(workflow, dict):
        return None
    for key in _TRIGGER_KEYS:
        if key in workflow:
            return workflow[key]
    return None


def _set_trigger(workflow: dict[str, Any], value: Any) -> None:
    for key in _TRIGGER_KEYS:
        if key in workflow:
            workflow[key] = value
            return
    raise WorkflowContractViolation("workflow declares no trigger events")


# --------------------------------------------------------------------------
# Checks.  Each returns a list of human-readable violations and never raises
# for a well-formed (possibly mutated) document.
# --------------------------------------------------------------------------


def check_workflow_trigger(workflow: dict[str, Any]) -> list[str]:
    """Require the trigger mapping to be exactly both events, unconfigured."""

    if _trigger_value(workflow) != TRIGGER_EVENTS:
        return [
            "workflow does not declare exactly a plain push and workflow_dispatch trigger"
        ]
    return []


def check_lane_routing(workflow: dict[str, Any]) -> list[str]:
    """Require every lane to depend on the gate and carry its group's condition.

    A job outside the routing map has no lane unless it is the reuse gate,
    which ``check_reuse_gate`` owns.  A mapped job must carry its condition
    exactly (an absent ``if`` included in the rejection) and ``needs`` exactly
    the gate.  Routing stays a job-level decision, so no step declares a
    condition.  Missing jobs stay owned by ``check_job_matrix``.  Lanes stay
    independent of each other: a lane that needs another lane is skipped
    precisely when that lane's own ``if`` would have fired.
    """

    problems: list[str] = []
    for name, job in _jobs(workflow).items():
        if name == GATE_JOB:
            continue
        condition = LANE_ROUTING_CONDITIONS.get(name)
        if condition is None:
            problems.append(f"{name}: job is outside the lane routing map")
            continue
        if_value = job.get("if") if isinstance(job, dict) else None
        if if_value != condition:
            problems.append(
                f"{name}: job condition is {if_value!r}, expected {condition!r}"
            )
        for step in _steps(job):
            if "if" in step:
                problems.append(
                    f"{name}: step {step.get('name') or step.get('uses')!r} "
                    f"declares if={step['if']!r}"
                )
        needs = job.get("needs") if isinstance(job, dict) else None
        if needs != [GATE_JOB]:
            problems.append(
                f"{name}: job needs is {needs!r}, expected {[GATE_JOB]!r}; "
                "lanes depend on the gate only, never on each other"
            )
    return problems


def check_reuse_gate(workflow: dict[str, Any]) -> list[str]:
    """Pin the reuse gate's shape and keep it the only privileged job.

    The gate is a main-push-only job on the plain hosted Ubuntu label with
    read-only token scopes, no dependency, exactly two outputs read from its
    run step, and exactly two steps: a credential-free sparse checkout of the
    script directory and one run of that script.  No other job and no workflow
    level key declares ``permissions``.
    """

    problems: list[str] = []
    if "permissions" in workflow:
        problems.append(f"workflow declares permissions={workflow['permissions']!r}")
    for name, job in _jobs(workflow).items():
        if name != GATE_JOB and isinstance(job, dict) and "permissions" in job:
            problems.append(f"{name}: job declares permissions={job['permissions']!r}")
    gate = _job(workflow, GATE_JOB)
    if not isinstance(gate, dict):
        return [*problems, f"{GATE_JOB}: job is missing"]
    expected = {
        "if": GATE_CONDITION,
        "runs-on": "ubuntu-latest",
        "permissions": GATE_PERMISSIONS,
    }
    for key, value in expected.items():
        if gate.get(key) != value:
            problems.append(f"{GATE_JOB}: {key} is {gate.get(key)!r}, expected {value!r}")
    if "needs" in gate:
        problems.append(f"{GATE_JOB}: job declares needs={gate['needs']!r}")
    steps = gate.get("steps")
    if not isinstance(steps, list) or len(steps) != 2 or not all(
        isinstance(step, dict) for step in steps
    ):
        return [*problems, f"{GATE_JOB}: steps are not exactly a checkout and one run"]
    checkout, run = steps
    if checkout != GATE_CHECKOUT:
        problems.append(f"{GATE_JOB}: checkout step is {checkout!r}, expected {GATE_CHECKOUT!r}")
    if set(run) != {"id", "env", "run"} or not isinstance(run.get("id"), str) or not run["id"]:
        problems.append(f"{GATE_JOB}: run step keys are {sorted(run)!r}, expected id, env, run")
        return problems
    if run["env"] != GATE_TOKEN_ENV:
        problems.append(f"{GATE_JOB}: run step env is {run['env']!r}, expected {GATE_TOKEN_ENV!r}")
    if _effective_lines(run) != [GATE_COMMAND]:
        problems.append(
            f"{GATE_JOB}: run step lines are {_effective_lines(run)!r}, expected {[GATE_COMMAND]!r}"
        )
    if not (REPO_ROOT / GATE_SCRIPT).is_file():
        problems.append(f"{GATE_JOB}: {GATE_SCRIPT} does not exist")
    outputs = {
        name: f"${{{{ steps.{run['id']}.outputs.{name} }}}}" for name in GATE_OUTPUT_NAMES
    }
    if gate.get("outputs") != outputs:
        problems.append(f"{GATE_JOB}: outputs are {gate.get('outputs')!r}, expected {outputs!r}")
    return problems


# --------------------------------------------------------------------------
# Guard property: the reuse inference holds for the workflow's actual routing
# --------------------------------------------------------------------------

MAIN_REF = "refs/heads/main"
GUARD_EVENTS = ("push", "workflow_dispatch")
GUARD_REFS = (MAIN_REF, "refs/heads/feature", "refs/tags/v1")
GATE_OUTPUT_PAIRS = tuple(
    {"reuse_source": source, "reuse_desktop": desktop}
    for source in ("true", "false")
    for desktop in ("true", "false")
)
GATE_STATES = (
    ("skipped", {}),
    ("failure", {}),
    *(("success", pair) for pair in GATE_OUTPUT_PAIRS),
    ("success", {}),
)
LANE_GROUPS = {
    **{name: "source" for name in (*BASELINE_JOBS, *SUITE_JOBS)},
    **{name: "desktop" for name in DESKTOP_JOBS},
}
REUSE_EVIDENCE_RULE = (
    "reuse evidence rule: a successful branch push run is taken as proof that "
    "every package and suite lane ran, and a successful dispatch run as proof "
    "that both desktop lanes ran, so a lane condition change must keep that true"
)


class GuardEvaluationError(ValueError):
    """Raised for an expression element the guard evaluator does not model."""


_GUARD_TOKEN = re.compile(
    r"\s*(?:(&&|\|\||==|!=|!|\(|\))|'((?:[^']|'')*)'|([A-Za-z_][\w.\-]*))"
)


def _guard_tokens(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while text[position:].strip():
        match = _GUARD_TOKEN.match(text, position)
        if match is None:
            raise GuardEvaluationError(f"unmodelled syntax at {text[position:]!r}")
        operator, string, name = match.groups()
        if operator is not None:
            tokens.append(("op", operator))
        elif name is not None:
            tokens.append(("name", name))
        else:
            tokens.append(("str", string.replace("''", "'")))
        position = match.end()
    return tokens


class _GuardParser:
    """Evaluate one job condition over a modelled event, ref and gate state."""

    def __init__(self, text: str, event: str, ref: str, outputs: dict[str, str]):
        self.tokens = _guard_tokens(text)
        self.index = 0
        self.event = event
        self.ref = ref
        self.outputs = outputs
        self.status_check = False

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _take(self) -> tuple[str, str]:
        token = self._peek()
        if token is None:
            raise GuardEvaluationError("expression ends early")
        self.index += 1
        return token

    def _accept(self, operator: str) -> bool:
        if self._peek() == ("op", operator):
            self.index += 1
            return True
        return False

    def evaluate(self) -> bool:
        value = self._or()
        if self._peek() is not None:
            raise GuardEvaluationError(f"unmodelled token {self._peek()!r}")
        return self._boolean(value)

    @staticmethod
    def _boolean(value: bool | str) -> bool:
        if not isinstance(value, bool):
            raise GuardEvaluationError(f"unmodelled truthiness of {value!r}")
        return value

    def _or(self) -> bool | str:
        value = self._and()
        while self._accept("||"):
            right = self._and()
            value = self._boolean(value) or self._boolean(right)
        return value

    def _and(self) -> bool | str:
        value = self._not()
        while self._accept("&&"):
            right = self._not()
            value = self._boolean(value) and self._boolean(right)
        return value

    def _not(self) -> bool | str:
        if self._accept("!"):
            return not self._boolean(self._not())
        return self._comparison()

    def _comparison(self) -> bool | str:
        left = self._primary()
        for operator in ("==", "!="):
            if self._accept(operator):
                right = self._primary()
                if not isinstance(left, str) or not isinstance(right, str):
                    raise GuardEvaluationError("unmodelled comparison of non-strings")
                equal = left.lower() == right.lower()
                return equal if operator == "==" else not equal
        return left

    def _primary(self) -> bool | str:
        kind, value = self._take()
        if kind == "str":
            return value
        if kind == "op":
            if value != "(":
                raise GuardEvaluationError(f"unmodelled operator {value!r}")
            inner = self._or()
            if not self._accept(")"):
                raise GuardEvaluationError("unbalanced parenthesis")
            return inner
        if self._accept("("):
            if value != "cancelled" or not self._accept(")"):
                raise GuardEvaluationError(f"unmodelled function {value}()")
            self.status_check = True
            return False
        return self._context(value)

    def _context(self, name: str) -> str:
        if name == "github.event_name":
            return self.event
        if name == "github.ref":
            return self.ref
        prefix = f"needs.{GATE_JOB}.outputs."
        if name.startswith(prefix) and name != prefix:
            return self.outputs.get(name[len(prefix) :], "")
        raise GuardEvaluationError(f"unmodelled context {name!r}")


def guard_runs(
    text: str, event: str, ref: str, gate_result: str, outputs: dict[str, str]
) -> bool:
    """Return whether a lane with this condition starts under the scenario.

    A condition without a status-check function is also gated by the implicit
    ``success()`` of its dependency, so a skipped or failed gate skips it.
    """

    parser = _GuardParser(text, event, ref, outputs)
    value = parser.evaluate()
    return value if parser.status_check else value and gate_result == "success"


def _expected_guard(group: str, event: str, ref: str, outputs: dict[str, str]) -> bool:
    if event == "workflow_dispatch":
        return group == "desktop"
    if ref != MAIN_REF:
        return group == "source"
    return outputs.get(f"reuse_{group}", "") != "true"


def check_guard_property(workflow: dict[str, Any]) -> list[str]:
    """Evaluate each lane's parsed condition over every event, ref and gate state.

    Package and suite lanes always run on a push to another ref and never on a
    dispatch; desktop lanes always run on a dispatch and never on a push to
    another ref; on a push to main each group runs exactly when its own gate
    output is not ``'true'``, so a skipped, failed or empty gate runs both.
    """

    problems: list[str] = []
    for name, group in LANE_GROUPS.items():
        job = _job(workflow, name)
        text = job.get("if") if isinstance(job, dict) else None
        if not isinstance(text, str):
            problems.append(f"{name}: job condition {text!r} is not a string; {REUSE_EVIDENCE_RULE}")
            continue
        problems.extend(_guard_problems(name, group, text))
    return problems


def _guard_problems(name: str, group: str, text: str) -> list[str]:
    problems: list[str] = []
    for event in GUARD_EVENTS:
        for ref in GUARD_REFS:
            for result, outputs in GATE_STATES:
                want = _expected_guard(group, event, ref, outputs)
                try:
                    got = guard_runs(text, event, ref, result, outputs)
                except GuardEvaluationError as error:
                    return [f"{name}: {error}; {REUSE_EVIDENCE_RULE}"]
                if got != want:
                    problems.append(
                        f"{name}: {'runs' if got else 'skips'} on {event} of {ref} "
                        f"with gate {result} {outputs}, expected "
                        f"{'to run' if want else 'to skip'}; {REUSE_EVIDENCE_RULE}"
                    )
    return problems


_LANE_REF_READ = re.compile(
    r"github\.(?:ref|event_name)|github\[\s*['\"](?:ref|event_name)|"
    r"GITHUB_(?:REF|EVENT_NAME)",
    re.IGNORECASE,
)


def check_lane_ref_reads(workflow: dict[str, Any]) -> list[str]:
    """Reject lane text that reads the ref or event, so lanes behave alike everywhere.

    Cross-ref evidence is valid only while a lane does the same work on every
    ref and event.  Job ``env`` and each step's ``run``, ``env`` and ``with``
    may not read either value; the job-level ``if`` and the gate are exempt.
    """

    problems: list[str] = []
    for name, job in _jobs(workflow).items():
        if name == GATE_JOB or not isinstance(job, dict):
            continue
        places = [("job env", job.get("env"))]
        for step in _steps(job):
            label = step.get("name") or step.get("uses") or step.get("id")
            places.extend(
                (f"step {label!r} {key}", step.get(key)) for key in ("run", "env", "with")
            )
        for place, value in places:
            if value is not None and _LANE_REF_READ.search(str(value)):
                problems.append(f"{name}: {place} reads the ref or event: {value!r}")
    return problems


def suite_pairing_violations(
    baseline_jobs: dict[str, dict[str, Any]], suite_jobs: dict[str, dict[str, Any]]
) -> list[str]:
    problems: list[str] = []
    for name, spec in suite_jobs.items():
        package = baseline_jobs.get(spec["package_job"])
        if package is None:
            problems.append(f"{name}: package job {spec['package_job']!r} is not a baseline job")
            continue
        for key in ("runs_on", "python"):
            if spec[key] != package[key]:
                problems.append(
                    f"{name}: {key} is {spec[key]!r}, but package job "
                    f"{spec['package_job']} has {package[key]!r}"
                )
    return problems


def check_job_matrix(workflow: dict[str, Any]) -> list[str]:
    problems = suite_pairing_violations(BASELINE_JOBS, SUITE_JOBS)
    for name, spec in {**BASELINE_JOBS, **SUITE_JOBS, **DESKTOP_JOBS}.items():
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


def _suite_command(name: str) -> str:
    return SHARDED_SUITE_COMMAND.replace("{python}", SUITE_JOBS[name]["python"])


# A run line names pytest when, with quotes, backticks and backslashes removed,
# it contains ``pytest`` or ``py.test`` in any letter case.
_PYTEST_NAME = re.compile(r"py\.?test", re.IGNORECASE)
_PYTEST_NAME_MASK = str.maketrans("", "", "\"'`\\")


def _names_pytest(line: str) -> bool:
    return bool(_PYTEST_NAME.search(line.translate(_PYTEST_NAME_MASK)))


# The only run lines naming pytest that each package job may hold, compared
# exactly: the installed-wheel proof in every package job and, in the Linux
# job, the documented-install proof, the installed-service lines that run
# pytest and the continuation that installs it for that service.
_LINUX_PACKAGE_PYTEST_LINES = frozenset(
    {
        DOCUMENTED_INSTALL_COMMAND,
        LINUX_SERVICE_PIP_CONTINUATION,
        *(line for gate in LINUX_SERVICE_GATES for line in gate if _names_pytest(line)),
    }
)
PACKAGE_JOB_PYTEST_LINES = {
    name: frozenset({INSTALLED_WHEEL_COMMAND.format(python=spec["python"])})
    | (_LINUX_PACKAGE_PYTEST_LINES if name == "nyx" else frozenset())
    for name, spec in BASELINE_JOBS.items()
}


def check_baseline_source_suite(workflow: dict[str, Any]) -> list[str]:
    """Require the sharded source suite in each suite job and nowhere else.

    Each suite job holds the exact sharded command and the exact install line
    its suite needs.  A package job runs pytest only through its designated
    lines: every effective line of every step that names pytest (case
    insensitively, ignoring quotes, backticks and backslashes) must equal one
    of that job's lines in ``PACKAGE_JOB_PYTEST_LINES``.  Any other such line
    is reported, whatever it selects and whichever command wraps it, so a new
    pytest run in a package job needs a matching edit of that table.  A name
    built from variables or split across lines names pytest on no single line
    and is left to review.
    """

    problems: list[str] = []
    for name, spec in SUITE_JOBS.items():
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        segments = _job_effective_lines(job)
        problems.extend(
            _gate_violations(
                f"{name}: sharded source-suite command", segments, (_suite_command(name),)
            )
        )
        install = SUITE_INSTALL_COMMAND.replace("{python}", spec["python"])
        problems.extend(
            _gate_violations(f"{name}: source-suite install", segments, (install,))
        )
    for name in BASELINE_JOBS:
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        designated = PACKAGE_JOB_PYTEST_LINES[name]
        for step in _steps(job):
            problems.extend(
                f"{name}: step {step.get('name')!r} runs an undesignated pytest line: {line!r}"
                for line in _effective_lines(step)
                if _names_pytest(line) and line not in designated
            )
    return problems


def _mentions_shard_option(value: Any) -> bool:
    return SHARD_OPTION in str(value)


def check_source_suite_shards(workflow: dict[str, Any]) -> list[str]:
    """Require each suite job's matrix and keep the shard option in one place.

    The strategy of a suite job is exactly ``fail-fast: false`` and one
    ``shard`` list of its table's length.  The shard option appears only in each
    suite job's sharded command line, never in the workflow-level, a job-level
    or a step ``env``, nor on any other line of any job.
    """

    problems: list[str] = []
    if _mentions_shard_option(workflow.get("env")):
        problems.append(f"workflow env carries {SHARD_OPTION}: {workflow.get('env')!r}")
    for name, spec in SUITE_JOBS.items():
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        expected = {"fail-fast": False, "matrix": {"shard": list(range(spec["shards"]))}}
        if job.get("strategy") != expected:
            problems.append(
                f"{name}: strategy is {job.get('strategy')!r}, expected {expected!r}"
            )
    for name, job in _jobs(workflow).items():
        if not isinstance(job, dict):
            continue
        if _mentions_shard_option(job.get("env")):
            problems.append(f"{name}: job env carries {SHARD_OPTION}: {job.get('env')!r}")
        allowed = _suite_command(name) if name in SUITE_JOBS else None
        for step in _steps(job):
            if _mentions_shard_option(step.get("env")):
                problems.append(
                    f"{name}: step {step.get('name')!r} env carries {SHARD_OPTION}: "
                    f"{step.get('env')!r}"
                )
            problems.extend(
                f"{name}: step {step.get('name')!r} carries {SHARD_OPTION} on {line!r}"
                for line in _effective_lines(step)
                if SHARD_OPTION in line and line != allowed
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
    """Require one exact installed-wheel run per baseline job, after the build.

    Positions are ``(step, line)`` pairs over the job's effective lines, so the
    order holds across steps as well as within one.
    """

    problems: list[str] = []
    for name, spec in BASELINE_JOBS.items():
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        segments = _job_effective_lines(job)
        command = INSTALLED_WHEEL_COMMAND.format(python=spec["python"])
        build = WHEEL_BUILD_COMMAND.format(python=spec["python"])
        problems.extend(
            _gate_violations(f"{name}: installed-wheel command", segments, (command,))
        )
        invocations = [
            (step, index)
            for step, lines in enumerate(segments)
            for index, line in enumerate(lines)
            if INSTALLED_WHEEL_TARGET in line
        ]
        if len(invocations) > 1:
            problems.append(
                f"{name}: {INSTALLED_WHEEL_TARGET} runs {len(invocations)} times; "
                "expected exactly once"
            )
        builds = [
            (step, index)
            for step, lines in enumerate(segments)
            for index, line in enumerate(lines)
            if line == build
        ]
        for position in invocations:
            if not any(built < position for built in builds):
                problems.append(
                    f"{name}: {INSTALLED_WHEEL_TARGET} runs before the wheel build "
                    f"{build!r}"
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
    for name, (step_name, command) in DOCUMENTED_INSTALL_STEPS.items():
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        step = _step_named(job, step_name)
        if step is None:
            problems.append(f"{name}: {step_name!r} step is missing")
            continue
        env = _step_env(step)
        if env.get(DOCUMENTED_INSTALL_ENV) != "1":
            problems.append(
                f"{name}: {step_name!r} does not set {DOCUMENTED_INSTALL_ENV}=1"
            )
        problems.extend(
            _gate_violations(
                f"{name}: documented installation command",
                [_effective_lines(step)],
                (command,),
            )
        )
    return problems


def _once_proof_designations(job_name: str) -> dict[str, list[tuple[str, str]]]:
    """Return the (step, command) pairs designated to run each once-only proof."""

    desktop: list[tuple[str, str]] = []
    documented: list[tuple[str, str]] = []
    if job_name in DESKTOP_JOBS:
        desktop.append((SOURCE_SESSION_STEP, DESKTOP_SOURCE_COMMAND))
        documented.append((DOCUMENTED_BUILD_STEP, DOCUMENTED_BUILD_COMMAND))
    if job_name in DOCUMENTED_INSTALL_STEPS:
        documented.append(DOCUMENTED_INSTALL_STEPS[job_name])
    return {DESKTOP_SOURCE_TARGET: desktop, DOCUMENTED_INSTALL_TARGET: documented}


def check_once_only_proofs(workflow: dict[str, Any]) -> list[str]:
    """Require each desktop and documented-install proof to run once, in place.

    In every job, an effective line naming either test file must be exactly a
    designated command sitting in its designated step, and each designated
    command must appear once there.
    """

    problems: list[str] = []
    for name, job in _jobs(workflow).items():
        for target, designated in _once_proof_designations(name).items():
            counts = {pair: 0 for pair in designated}
            for step in _steps(job):
                step_name = step.get("name")
                for line in _effective_lines(step):
                    if target not in line:
                        continue
                    if (step_name, line) in counts:
                        counts[(step_name, line)] += 1
                    else:
                        problems.append(
                            f"{name}: {step_name!r} runs {line!r}, which is not a "
                            f"designated {target} command for that step"
                        )
            problems.extend(
                f"{name}: {step_name!r} runs {command!r} {count} times; expected once"
                for (step_name, command), count in counts.items()
                if count > 1
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


def check_job_time_limits(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for job_name, job in _jobs(workflow).items():
        if not isinstance(job, dict) or TIMEOUT_KEY not in job:
            problems.append(f"{job_name}: job does not declare {TIMEOUT_KEY}")
            continue
        minutes = job[TIMEOUT_KEY]
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            problems.append(f"{job_name}: {TIMEOUT_KEY} {minutes!r} is not an integer")
            continue
        if not TIMEOUT_MINIMUM_MINUTES <= minutes <= TIMEOUT_MAXIMUM_MINUTES:
            problems.append(
                f"{job_name}: {TIMEOUT_KEY} {minutes} is outside "
                f"{TIMEOUT_MINIMUM_MINUTES}-{TIMEOUT_MAXIMUM_MINUTES}"
            )
            continue
        expected = JOB_TIMEOUT_MINUTES.get(job_name)
        if expected is None:
            problems.append(f"{job_name}: job has no pinned {TIMEOUT_KEY}")
        elif minutes != expected:
            problems.append(
                f"{job_name}: {TIMEOUT_KEY} is {minutes}, expected {expected}"
            )
    return problems


def check_no_concurrency_cancellation(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if CONCURRENCY_KEY in workflow:
        problems.append(f"workflow declares {CONCURRENCY_KEY}={workflow[CONCURRENCY_KEY]!r}")
    for job_name, job in _jobs(workflow).items():
        if isinstance(job, dict) and CONCURRENCY_KEY in job:
            problems.append(
                f"{job_name}: job declares {CONCURRENCY_KEY}={job[CONCURRENCY_KEY]!r}"
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
        build = _step_named(job, DOCUMENTED_BUILD_STEP)
        if build is not None and REQUIRED_NATIVE_ENV in _step_env(build):
            problems.append(
                f"{name}: {DOCUMENTED_BUILD_STEP!r} sets {REQUIRED_NATIVE_ENV}, "
                "which arms the pre-artifact import guard"
            )
    return problems


def check_documented_build_step(workflow: dict[str, Any]) -> list[str]:
    """Require the pre-artifact documented build step in both desktop jobs.

    The step runs the gated build test only: its env is exactly the build gate
    (so the import guard stays armed for the artifact step), it sits between
    the source step and the artifact step, and no other desktop step carries
    the gate, a copied published command, or a direct build.
    """

    problems: list[str] = []
    for name in DESKTOP_JOBS:
        job = _job(workflow, name)
        if job is None:
            problems.append(f"{name}: job is missing")
            continue
        steps = _steps(job)
        names = [step.get("name") for step in steps]
        step = _step_named(job, DOCUMENTED_BUILD_STEP)
        if step is None:
            problems.append(f"{name}: {DOCUMENTED_BUILD_STEP!r} step is missing")
        else:
            if _step_env(step) != {DOCUMENTED_BUILD_ENV: "1"}:
                problems.append(
                    f"{name}: {DOCUMENTED_BUILD_STEP!r} env is {_step_env(step)!r}, "
                    f"expected only {DOCUMENTED_BUILD_ENV}='1'"
                )
            problems.extend(
                _gate_violations(
                    f"{name}: documented build command",
                    [_effective_lines(step)],
                    (DOCUMENTED_BUILD_COMMAND,),
                )
            )
            if names.count(DOCUMENTED_BUILD_STEP) != 1:
                problems.append(f"{name}: {DOCUMENTED_BUILD_STEP!r} step is duplicated")
            if SOURCE_SESSION_STEP in names and BUILD_ARTIFACT_STEP in names:
                if not (
                    names.index(SOURCE_SESSION_STEP)
                    < names.index(DOCUMENTED_BUILD_STEP)
                    < names.index(BUILD_ARTIFACT_STEP)
                ):
                    problems.append(
                        f"{name}: {DOCUMENTED_BUILD_STEP!r} must run after the "
                        "source step and before the artifact step"
                    )
        for other in steps:
            if other.get("name") == DOCUMENTED_BUILD_STEP:
                continue
            if DOCUMENTED_BUILD_ENV in _step_env(other):
                problems.append(
                    f"{name}: step {other.get('name')!r} sets {DOCUMENTED_BUILD_ENV}"
                )
        for other in steps:
            for line in _effective_lines(other):
                for marker in FORBIDDEN_BUILD_MARKERS:
                    if marker in line:
                        problems.append(
                            f"{name}: step {other.get('name')!r} holds build command "
                            f"text {marker!r}: {line!r}"
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
        if not _contains_marker(segments, spec["artifact"]):
            problems.append(f"{name}: artifact path {spec['artifact']!r} is missing")
        problems.extend(
            _gate_violations(
                f"{name}: delivered artifact tests", segments, (DESKTOP_ARTIFACT_COMMAND,)
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
        if DOCUMENTED_BUILD_STEP in names and not (
            source_index < names.index(DOCUMENTED_BUILD_STEP) < build_index
        ):
            problems.append(
                f"{name}: documented build stage must run between the native "
                "source/session stage and the artifact stage"
            )
        lines = _effective_lines(steps[build_index])
        stages = (
            lambda line: spec["artifact_export"] in line,
            lambda line: line == DESKTOP_ARTIFACT_COMMAND,
            lambda line: line == DOCUMENTED_ARTIFACT_INSTALL_COMMAND,
        )
        positions = [
            next((index for index, line in enumerate(lines) if matches(line)), -1)
            for matches in stages
        ]
        if any(position < 0 for position in positions):
            continue
        if positions != sorted(positions) or len(set(positions)) != len(positions):
            problems.append(
                f"{name}: artifact stage order must be artifact input, "
                "artifact tests, then the documented installation"
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
    "lane_routing": check_lane_routing,
    "reuse_gate": check_reuse_gate,
    "guard_property": check_guard_property,
    "lane_ref_reads": check_lane_ref_reads,
    "job_matrix": check_job_matrix,
    "shell_matrix": check_shell_matrix,
    "baseline_source_suite": check_baseline_source_suite,
    "source_suite_shards": check_source_suite_shards,
    "baseline_ruff": check_baseline_ruff,
    "baseline_wheel": check_baseline_wheel,
    "baseline_installed_wheel": check_baseline_installed_wheel,
    "baseline_browser_prerequisites": check_baseline_browser_prerequisites,
    "linux_service_lane": check_linux_service_lane,
    "documented_install_lane": check_documented_install_lane,
    "once_only_proofs": check_once_only_proofs,
    "powershell_failure_propagation": check_powershell_failure_propagation,
    "posix_failure_propagation": check_posix_failure_propagation,
    "no_continue_on_error": check_no_continue_on_error,
    "job_time_limits": check_job_time_limits,
    "no_concurrency_cancellation": check_no_concurrency_cancellation,
    "native_environment": check_native_environment,
    "documented_build_step": check_documented_build_step,
    "native_dependencies": check_native_dependencies,
    "native_source_lane": check_native_source_lane,
    "native_session_preflight": check_native_session_preflight,
    "native_artifact_lane": check_native_artifact_lane,
    "native_artifact_input": check_native_artifact_input,
    "native_stage_ordering": check_native_stage_ordering,
    "no_offscreen_substitute": check_no_offscreen_substitute,
}


# --------------------------------------------------------------------------
# The real workflow satisfies every established check
# --------------------------------------------------------------------------


@pytest.mark.parametrize("check_name", sorted(CHECKS))
def test_established_workflow_check_has_no_violations(check_name: str) -> None:
    assert CHECKS[check_name](load_workflow()) == []


# --------------------------------------------------------------------------
# Natural mutations are rejected by their matching check
# --------------------------------------------------------------------------


def _mutate_remove_baseline_source_suite(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-suite", SUITE_STEP)
    command = f"{_suite_command('nyx-suite')}\n"
    assert command in _run_text(step)
    step["run"] = _run_text(step).replace(command, "", 1)


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
    step = _step(workflow, "nyx", DOCUMENTED_INSTALL_STEP)
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


def _mutate_move_documented_build_after_artifact_step(workflow: dict[str, Any]) -> None:
    job = _job(workflow, "nyx-desktop-windows")
    steps = _steps(job)
    documented = next(step for step in steps if step.get("name") == DOCUMENTED_BUILD_STEP)
    steps.remove(documented)
    steps.append(documented)
    job["steps"] = steps


def _mutate_drop_documented_build_step(workflow: dict[str, Any]) -> None:
    job = _job(workflow, "nyx-desktop-macos")
    job["steps"] = [step for step in _steps(job) if step.get("name") != DOCUMENTED_BUILD_STEP]


def _mutate_require_native_on_documented_build(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-desktop-windows", DOCUMENTED_BUILD_STEP)
    step["env"] = {**_step_env(step), REQUIRED_NATIVE_ENV: "1"}


def _mutate_drop_documented_build_gate(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-desktop-macos", DOCUMENTED_BUILD_STEP)
    step["env"] = {}


def _mutate_gate_the_artifact_step_as_documented_build(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-desktop-windows", BUILD_ARTIFACT_STEP)
    step["env"] = {**_step_env(step), DOCUMENTED_BUILD_ENV: "1"}


def _mutate_re_add_direct_build(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx-desktop-windows",
        BUILD_ARTIFACT_STEP,
        "$env:NYX_DESKTOP_ARTIFACT = $artifact",
        _insert_before("python scripts/build_desktop.py"),
    )


def _mutate_copy_published_command_into_documented_step(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx-desktop-windows",
        DOCUMENTED_BUILD_STEP,
        DOCUMENTED_BUILD_COMMAND,
        _insert_before(r".\.venv\Scripts\python.exe -m pip install '.[desktop,build]'"),
    )


def _mutate_reload_visual_studio_loader(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx-desktop-windows",
        BUILD_ARTIFACT_STEP,
        "$env:NYX_DESKTOP_ARTIFACT = $artifact",
        _insert_before(
            '$vswhere = "${env:ProgramFiles(x86)}\\Microsoft Visual Studio\\Installer\\vswhere.exe"'
        ),
    )


def _mutate_move_build_step_before_source_step(workflow: dict[str, Any]) -> None:
    job = _job(workflow, "nyx-desktop-macos")
    steps = _steps(job)
    names = [step.get("name") for step in steps]
    source_index = names.index(SOURCE_SESSION_STEP)
    build_index = names.index(BUILD_ARTIFACT_STEP)
    assert source_index < build_index
    steps[source_index], steps[build_index] = steps[build_index], steps[source_index]
    job["steps"] = steps


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


def _mutate_filter_push_trigger_to_main(workflow: dict[str, Any]) -> None:
    _set_trigger(
        workflow,
        {"push": {"branches": ["main"]}, "workflow_dispatch": None},
    )


def _mutate_remove_workflow_dispatch_trigger(workflow: dict[str, Any]) -> None:
    _set_trigger(workflow, {"push": None})


def _mutate_add_scheduled_trigger(workflow: dict[str, Any]) -> None:
    _set_trigger(
        workflow,
        {
            "push": None,
            "workflow_dispatch": None,
            "schedule": [{"cron": "0 6 * * *"}],
        },
    )


def _mutate_drop_package_job_condition(workflow: dict[str, Any]) -> None:
    _job(workflow, "nyx-windows").pop("if")


def _mutate_narrow_desktop_routing_to_main(workflow: dict[str, Any]) -> None:
    _job(workflow, "nyx-desktop-macos")["if"] = "github.ref == 'refs/heads/main'"


def _mutate_lane_needs_another_lane(workflow: dict[str, Any]) -> None:
    _job(workflow, "nyx-desktop-windows")["needs"] = [GATE_JOB, "nyx-windows"]


def _mutate_condition_the_documented_build_step(workflow: dict[str, Any]) -> None:
    _step(workflow, "nyx-desktop-windows", DOCUMENTED_BUILD_STEP)["if"] = (
        "github.ref == 'refs/heads/main'"
    )


def _mutate_drop_lane_needs(workflow: dict[str, Any]) -> None:
    _job(workflow, "nyx-macos").pop("needs")


def _mutate_gate_actions_write(workflow: dict[str, Any]) -> None:
    _job(workflow, GATE_JOB)["permissions"]["actions"] = "write"


def _mutate_gate_keeps_credentials(workflow: dict[str, Any]) -> None:
    del _job(workflow, GATE_JOB)["steps"][0]["with"]["persist-credentials"]


def _mutate_gate_adds_setup_python(workflow: dict[str, Any]) -> None:
    _job(workflow, GATE_JOB)["steps"].insert(
        1, {"uses": "actions/setup-python@v5", "with": {"python-version": "3.12"}}
    )


def _mutate_gate_runs_on_every_push(workflow: dict[str, Any]) -> None:
    _job(workflow, GATE_JOB)["if"] = "github.event_name == 'push'"


def _mutate_unpin_gate_timeout(workflow: dict[str, Any]) -> None:
    _job(workflow, GATE_JOB)[TIMEOUT_KEY] = 10


def _mutate_lane_declares_permissions(workflow: dict[str, Any]) -> None:
    _job(workflow, "nyx")["permissions"] = {"contents": "read"}


def _mutate_workflow_declares_permissions(workflow: dict[str, Any]) -> None:
    workflow["permissions"] = {"contents": "read"}


def _mutate_gate_output_from_another_step(workflow: dict[str, Any]) -> None:
    _job(workflow, GATE_JOB)["outputs"]["reuse_desktop"] = (
        "${{ steps.other.outputs.reuse_desktop }}"
    )


def _set_condition(job: str, condition: str) -> Callable[[dict[str, Any]], None]:
    def mutate(workflow: dict[str, Any]) -> None:
        _job(workflow, job)["if"] = condition

    return mutate


def _mutate_lane_echoes_ref_name(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-suite", SUITE_STEP)
    step["run"] = 'echo "${{ github.ref_name }}"\n' + _run_text(step)


def _mutate_lane_step_env_reads_event(workflow: dict[str, Any]) -> None:
    _step(workflow, "nyx-desktop-macos", PREFLIGHT_STEP).setdefault("env", {})[
        "NYX_EVENT"
    ] = "${{ github.event_name }}"


def _mutate_add_unrouted_job(workflow: dict[str, Any]) -> None:
    _jobs(workflow)["nyx-extra"] = {
        "runs-on": "ubuntu-latest",
        TIMEOUT_KEY: 30,
        "steps": [{"uses": "actions/checkout@v4"}],
    }


def _mutate_change_baseline_runner(workflow: dict[str, Any]) -> None:
    _job(workflow, "nyx")["runs-on"] = "ubuntu-22.04"


def _mutate_remove_pwsh_shell_declaration(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-windows", "Verify package and installed lifecycle")
    assert step.get("shell") == "pwsh"
    step.pop("shell")


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


def _mutate_comment_out_windows_source_suite(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx-windows-suite",
        SUITE_STEP,
        _suite_command("nyx-windows-suite"),
        _comment_out,
    )


def _mutate_disable_errexit_before_source_suite(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx-suite",
        SUITE_STEP,
        _suite_command("nyx-suite"),
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


def _mutate_ignore_baseline_ruff_failure(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow, "nyx-macos", BASELINE_LIFECYCLE_STEP, RUFF_COMMAND, _append(" || true")
    )


def _mutate_exit_zero_after_installed_wheel(workflow: dict[str, Any]) -> None:
    # This is the nyx job's only installed-wheel line, so the missing exact line
    # and the altered copy are both reported.
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


def _mutate_comment_out_documented_build(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx-desktop-windows",
        DOCUMENTED_BUILD_STEP,
        DOCUMENTED_BUILD_COMMAND,
        _comment_out,
    )


def _mutate_comment_out_desktop_extras_install(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow, "nyx-desktop-macos", SOURCE_SESSION_STEP, DESKTOP_EXTRA_INSTALL, _comment_out
    )


def _mutate_comment_out_documented_install(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow, "nyx", DOCUMENTED_INSTALL_STEP, DOCUMENTED_INSTALL_COMMAND, _comment_out
    )


def _mutate_remove_artifact_step_documented_install(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx-desktop-windows",
        BUILD_ARTIFACT_STEP,
        DOCUMENTED_ARTIFACT_INSTALL_COMMAND,
        _comment_out,
    )


def _mutate_drop_artifact_step_documented_install_gate(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-desktop-macos", BUILD_ARTIFACT_STEP)
    env = dict(_step_env(step))
    assert env.pop(DOCUMENTED_INSTALL_ENV, None) == "1"
    step["env"] = env


def _mutate_move_documented_install_above_artifact_export(
    workflow: dict[str, Any],
) -> None:
    step = _step(workflow, "nyx-desktop-macos", BUILD_ARTIFACT_STEP)
    export = DESKTOP_JOBS["nyx-desktop-macos"]["artifact_export"]
    physical = _run_text(step).splitlines()
    install = [
        index
        for index, line in enumerate(physical)
        if line.strip() == DOCUMENTED_ARTIFACT_INSTALL_COMMAND
    ]
    exported = [index for index, line in enumerate(physical) if export in line]
    assert install and exported and install[0] > exported[0]
    line = physical.pop(install[0])
    physical.insert(exported[0], line)
    step["run"] = "\n".join(physical) + "\n"


def _mutate_ignore_installed_service_failure(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx",
        LINUX_SERVICE_STEP,
        'nyx-service-test "$nyx_verify_root/exercise" "$nyx_verify_root/venv/bin/python"',
        _append(" || true"),
    )


def _mutate_ignore_one_wheel_count(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow, "nyx", LINUX_SERVICE_STEP, 'test "$wheel_count" -eq 1', _append(" || true")
    )


def _mutate_late_powershell_error_preference(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-desktop-windows", DOCUMENTED_BUILD_STEP)
    lines = _run_text(step).splitlines()
    assert lines[0] == PWSH_ERROR_PREFERENCE
    step["run"] = "\n".join([*lines[1:], lines[0]]) + "\n"


def _mutate_drop_powershell_error_preference(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-desktop-windows", DOCUMENTED_BUILD_STEP)
    lines = _run_text(step).splitlines()
    assert lines[0] == PWSH_ERROR_PREFERENCE
    step["run"] = "\n".join(lines[1:]) + "\n"


def _mutate_reassign_powershell_error_preference(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx-windows-suite",
        SUITE_STEP,
        _suite_command("nyx-windows-suite"),
        _insert_before("$ErrorActionPreference = 'Continue'"),
    )


def _mutate_comment_out_powershell_exit_guard(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-desktop-windows", BUILD_ARTIFACT_STEP)
    physical = _run_text(step).splitlines()
    tests = physical.index(DESKTOP_ARTIFACT_COMMAND)
    assert physical[tests + 1] == PWSH_FAILURE_GUARD
    physical[tests + 1] = _comment_out(physical[tests + 1])
    step["run"] = "\n".join(physical) + "\n"


def _mutate_duplicate_installed_wheel_after_service_proof(workflow: dict[str, Any]) -> None:
    # A second installed-wheel run after the separate-account
    # installed-service proof.
    command = INSTALLED_WHEEL_COMMAND.format(python=BASELINE_JOBS["nyx"]["python"])
    _rewrite_run_line(
        workflow,
        "nyx",
        LINUX_SERVICE_STEP,
        'nyx-service-test "$nyx_verify_root/exercise" "$nyx_verify_root/venv/bin/python"',
        _append(f"\n{command}"),
    )


def _mutate_re_add_desktop_pass_to_artifact_step(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx-desktop-windows",
        BUILD_ARTIFACT_STEP,
        DESKTOP_ARTIFACT_COMMAND,
        _insert_before(DESKTOP_SOURCE_COMMAND),
    )


def _mutate_add_whole_file_documented_install_run(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx-desktop-macos",
        BUILD_ARTIFACT_STEP,
        DOCUMENTED_ARTIFACT_INSTALL_COMMAND,
        _insert_before("python -m pytest tests/test_documented_install.py -v -s"),
    )


def _mutate_duplicate_desktop_source_command(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx-desktop-macos",
        SOURCE_SESSION_STEP,
        DESKTOP_SOURCE_COMMAND,
        _append(f"\n{DESKTOP_SOURCE_COMMAND}"),
    )


def _mutate_move_installed_wheel_before_build(workflow: dict[str, Any]) -> None:
    command = INSTALLED_WHEEL_COMMAND.format(python=BASELINE_JOBS["nyx-macos"]["python"])
    build = WHEEL_BUILD_COMMAND.format(python=BASELINE_JOBS["nyx-macos"]["python"])
    step = _step(workflow, "nyx-macos", BASELINE_LIFECYCLE_STEP)
    text = _run_text(step)
    assert f"{command}\n" in text
    step["run"] = text.replace(f"{command}\n", "", 1)
    _rewrite_run_line(
        workflow, "nyx-macos", BASELINE_LIFECYCLE_STEP, build, _insert_before(command)
    )


def _mutate_remove_job_time_limit(workflow: dict[str, Any]) -> None:
    job = _job(workflow, "nyx-windows")
    assert TIMEOUT_KEY in job
    del job[TIMEOUT_KEY]


def _mutate_raise_job_time_limit_to_six_hours(workflow: dict[str, Any]) -> None:
    _job(workflow, "nyx")[TIMEOUT_KEY] = 360


def _mutate_quote_job_time_limit(workflow: dict[str, Any]) -> None:
    _job(workflow, "nyx-macos")[TIMEOUT_KEY] = "30"


def _mutate_change_job_time_limit_within_range(workflow: dict[str, Any]) -> None:
    _job(workflow, "nyx-desktop-macos")[TIMEOUT_KEY] = 30


def _mutate_change_desktop_windows_time_limit_within_range(workflow: dict[str, Any]) -> None:
    _job(workflow, "nyx-desktop-windows")[TIMEOUT_KEY] = 40


def _mutate_add_job_without_time_limit(workflow: dict[str, Any]) -> None:
    _jobs(workflow)["nyx-extra"] = {
        "runs-on": "ubuntu-latest",
        "steps": [{"uses": "actions/checkout@v4"}],
    }


def _mutate_add_job_with_out_of_range_time_limit(workflow: dict[str, Any]) -> None:
    _jobs(workflow)["nyx-extra"] = {
        "runs-on": "ubuntu-latest",
        TIMEOUT_KEY: 360,
        "steps": [{"uses": "actions/checkout@v4"}],
    }


def _mutate_add_workflow_concurrency(workflow: dict[str, Any]) -> None:
    workflow[CONCURRENCY_KEY] = {
        "group": "${{ github.workflow }}-${{ github.ref }}",
        "cancel-in-progress": True,
    }


def _mutate_add_job_concurrency(workflow: dict[str, Any]) -> None:
    _job(workflow, "nyx-desktop-windows")[CONCURRENCY_KEY] = {
        "group": "${{ github.ref }}-desktop-windows",
        "cancel-in-progress": True,
    }


def _mutate_continue_job_on_error(workflow: dict[str, Any]) -> None:
    _job(workflow, "nyx-macos")[CONTINUE_ON_ERROR_KEY] = True


def _mutate_continue_step_on_error(workflow: dict[str, Any]) -> None:
    _step(workflow, "nyx-desktop-windows", SOURCE_SESSION_STEP)[CONTINUE_ON_ERROR_KEY] = True


def _mutate_drop_windows_suite_shards(workflow: dict[str, Any]) -> None:
    _job(workflow, "nyx-windows-suite")["strategy"]["matrix"]["shard"] = [0]


def _mutate_duplicate_macos_suite_shard(workflow: dict[str, Any]) -> None:
    _job(workflow, "nyx-macos-suite")["strategy"]["matrix"]["shard"] = [0, 0]


def _mutate_enable_suite_fail_fast(workflow: dict[str, Any]) -> None:
    del _job(workflow, "nyx-suite")["strategy"]["fail-fast"]


def _mutate_limit_suite_parallelism(workflow: dict[str, Any]) -> None:
    _job(workflow, "nyx-windows-suite")["strategy"]["max-parallel"] = 1


def _mutate_add_second_matrix_key(workflow: dict[str, Any]) -> None:
    _job(workflow, "nyx-macos-suite")["strategy"]["matrix"]["python"] = ["3.12"]


def _mutate_shard_option_in_job_env(workflow: dict[str, Any]) -> None:
    _job(workflow, "nyx")["env"] = {"PYTEST_ADDOPTS": "--nyx-shard=0/2"}


def _mutate_shard_option_in_suite_step_env(workflow: dict[str, Any]) -> None:
    _step(workflow, "nyx-suite", SUITE_STEP)["env"] = {
        "PYTEST_ADDOPTS": "--nyx-shard=0/2"
    }


def _mutate_shard_option_in_workflow_env(workflow: dict[str, Any]) -> None:
    workflow["env"] = {"PYTEST_ADDOPTS": "--nyx-shard=0/2"}


def _mutate_shard_option_on_package_pytest_line(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx-macos",
        BASELINE_LIFECYCLE_STEP,
        WHEEL_BUILD_COMMAND.format(python=BASELINE_JOBS["nyx-macos"]["python"]),
        _insert_before(
            '"$runner_python" -m pytest tests/test_catalog.py -v --nyx-shard=0/2'
        ),
    )


def _mutate_drop_shard_option(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx-macos-suite",
        SUITE_STEP,
        _suite_command("nyx-macos-suite"),
        lambda line: line.replace(SHARD_SUFFIX, ""),
    )


def _mutate_rerun_whole_suite_in_package_job(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx-windows",
        BASELINE_LIFECYCLE_STEP,
        WHEEL_BUILD_COMMAND.format(python=BASELINE_JOBS["nyx-windows"]["python"]),
        _insert_before("python -m pytest tests/ -v"),
    )


def _rerun_package_job_suite_as(
    command: str, job: str = "nyx-windows"
) -> Callable[[dict[str, Any]], None]:
    def mutate(workflow: dict[str, Any]) -> None:
        _rewrite_run_line(
            workflow,
            job,
            BASELINE_LIFECYCLE_STEP,
            WHEEL_BUILD_COMMAND.format(python=BASELINE_JOBS[job]["python"]),
            _insert_before(command),
        )

    return mutate


# The one installed-service line that runs pytest, designated to the Linux
# package job alone.
(_LINUX_SERVICE_PYTEST_LINE,) = (
    line for gate in LINUX_SERVICE_GATES for line in gate if _names_pytest(line)
)


def _mutate_add_undesignated_file_level_run(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx",
        LINUX_SERVICE_STEP,
        WHEEL_BUILD_COMMAND.format(python=BASELINE_JOBS["nyx"]["python"]),
        _insert_before('"$runner_python" -m pytest tests/test_catalog.py -v'),
    )


def _mutate_drop_suite_test_extras(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-suite", SUITE_STEP)
    install = SUITE_INSTALL_COMMAND.replace("{python}", SUITE_JOBS["nyx-suite"]["python"])
    assert install in _run_text(step)
    step["run"] = _run_text(step).replace(
        install, install.replace("'.[test]'", "'.'"), 1
    )


def _mutate_change_suite_runner(workflow: dict[str, Any]) -> None:
    _job(workflow, "nyx-windows-suite")["runs-on"] = "windows-2025"


def _mutate_suite_runs_on_dispatch(workflow: dict[str, Any]) -> None:
    del _job(workflow, "nyx-macos-suite")["if"]


def _mutate_remove_suite_pwsh_shell(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-windows-suite", SUITE_STEP)
    assert step.get("shell") == "pwsh"
    step.pop("shell")


def _mutate_comment_out_suite_exit_guard(workflow: dict[str, Any]) -> None:
    step = _step(workflow, "nyx-windows-suite", SUITE_STEP)
    physical = _run_text(step).splitlines()
    sharded = physical.index(_suite_command("nyx-windows-suite"))
    assert physical[sharded + 1] == PWSH_FAILURE_GUARD
    physical[sharded + 1] = _comment_out(physical[sharded + 1])
    step["run"] = "\n".join(physical) + "\n"


def _mutate_disable_errexit_before_macos_suite(workflow: dict[str, Any]) -> None:
    _rewrite_run_line(
        workflow,
        "nyx-macos-suite",
        SUITE_STEP,
        _suite_command("nyx-macos-suite"),
        _insert_before("set +e"),
    )


def _remove_suite_browser_prerequisite(workflow: dict[str, Any], name: str) -> None:
    step = _step(workflow, name, SUITE_STEP)
    command = BROWSER_INSTALL_COMMANDS[name]
    assert command in _run_text(step)
    step["run"] = "\n".join(
        line for line in _run_text(step).splitlines() if line.strip() != command
    ) + "\n"


def _mutate_remove_suite_browser_prerequisite(workflow: dict[str, Any]) -> None:
    _remove_suite_browser_prerequisite(workflow, "nyx-macos-suite")


def _mutate_remove_linux_suite_browser_prerequisite(workflow: dict[str, Any]) -> None:
    _remove_suite_browser_prerequisite(workflow, "nyx-suite")


def _mutate_remove_windows_suite_browser_prerequisite(workflow: dict[str, Any]) -> None:
    _remove_suite_browser_prerequisite(workflow, "nyx-windows-suite")


def _mutate_change_suite_time_limit_within_range(workflow: dict[str, Any]) -> None:
    job = _job(workflow, "nyx-windows-suite")
    job[TIMEOUT_KEY] = JOB_TIMEOUT_MINUTES["nyx-windows-suite"] + 5


MUTATIONS: dict[str, tuple[Callable[[dict[str, Any]], list[str]], Callable[[dict[str, Any]], None]]] = {
    "remove baseline source suite": (
        check_baseline_source_suite,
        _mutate_remove_baseline_source_suite,
    ),
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
    "remove artifact environment": (
        check_native_artifact_input,
        _mutate_remove_artifact_environment,
    ),
    "override artifact input": (
        check_native_artifact_input,
        _mutate_override_artifact_input,
    ),
    "move documented build step after artifact step": (
        check_documented_build_step,
        _mutate_move_documented_build_after_artifact_step,
    ),
    "move documented build step after artifact step (ordering)": (
        check_native_stage_ordering,
        _mutate_move_documented_build_after_artifact_step,
    ),
    "drop documented build step": (
        check_documented_build_step,
        _mutate_drop_documented_build_step,
    ),
    "require native desktop on documented build step": (
        check_documented_build_step,
        _mutate_require_native_on_documented_build,
    ),
    "require native desktop on documented build step (environment)": (
        check_native_environment,
        _mutate_require_native_on_documented_build,
    ),
    "drop documented build gate": (
        check_documented_build_step,
        _mutate_drop_documented_build_gate,
    ),
    "set documented build gate on artifact step": (
        check_documented_build_step,
        _mutate_gate_the_artifact_step_as_documented_build,
    ),
    "re-add direct desktop build": (
        check_documented_build_step,
        _mutate_re_add_direct_build,
    ),
    "copy published command into documented build step": (
        check_documented_build_step,
        _mutate_copy_published_command_into_documented_step,
    ),
    "reload Visual Studio loader": (
        check_documented_build_step,
        _mutate_reload_visual_studio_loader,
    ),
    "move build step before source step": (
        check_native_stage_ordering,
        _mutate_move_build_step_before_source_step,
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
    "filter push trigger to main": (
        check_workflow_trigger,
        _mutate_filter_push_trigger_to_main,
    ),
    "remove workflow dispatch trigger": (
        check_workflow_trigger,
        _mutate_remove_workflow_dispatch_trigger,
    ),
    "add a scheduled trigger": (check_workflow_trigger, _mutate_add_scheduled_trigger),
    "drop the package-job condition": (
        check_lane_routing,
        _mutate_drop_package_job_condition,
    ),
    "narrow the desktop routing condition to main": (
        check_lane_routing,
        _mutate_narrow_desktop_routing_to_main,
    ),
    "run the documented build step only on main": (
        check_lane_routing,
        _mutate_condition_the_documented_build_step,
    ),
    "add an unrouted job": (check_lane_routing, _mutate_add_unrouted_job),
    "drop a lane's needs": (check_lane_routing, _mutate_drop_lane_needs),
    "gate gains actions write": (check_reuse_gate, _mutate_gate_actions_write),
    "gate keeps checkout credentials": (
        check_reuse_gate,
        _mutate_gate_keeps_credentials,
    ),
    "gate gains a setup-python step": (check_reuse_gate, _mutate_gate_adds_setup_python),
    "gate runs on every push": (check_reuse_gate, _mutate_gate_runs_on_every_push),
    "unpin the gate timeout": (check_job_time_limits, _mutate_unpin_gate_timeout),
    "a lane declares permissions": (check_reuse_gate, _mutate_lane_declares_permissions),
    "the workflow declares permissions": (
        check_reuse_gate,
        _mutate_workflow_declares_permissions,
    ),
    "gate output read from another step": (
        check_reuse_gate,
        _mutate_gate_output_from_another_step,
    ),
    "a suite lane skips non-main pushes": (
        check_guard_property,
        _set_condition(
            "nyx-windows-suite",
            "!cancelled() && github.event_name == 'push' && "
            "github.ref == 'refs/heads/main' && "
            "needs.reuse-gate.outputs.reuse_source != 'true'",
        ),
    ),
    "a desktop lane lacks the dispatch disjunct": (
        check_guard_property,
        _set_condition(
            "nyx-desktop-windows",
            "!cancelled() && (github.ref == 'refs/heads/main' && "
            "needs.reuse-gate.outputs.reuse_desktop != 'true')",
        ),
    ),
    "a desktop lane reads the source output": (
        check_guard_property,
        _set_condition(
            "nyx-desktop-macos",
            DESKTOP_LANE_CONDITION.replace("reuse_desktop", "reuse_source"),
        ),
    ),
    "a lane tests for false instead of not true": (
        check_guard_property,
        _set_condition(
            "nyx-macos",
            PACKAGE_LANE_CONDITION.replace("!= 'true'", "== 'false'"),
        ),
    ),
    "a lane condition uses an unmodelled function": (
        check_guard_property,
        _set_condition(
            "nyx",
            PACKAGE_LANE_CONDITION.replace("!cancelled()", "!failure()"),
        ),
    ),
    "a lane drops the status check": (
        check_guard_property,
        _set_condition("nyx-suite", PACKAGE_LANE_CONDITION.removeprefix("!cancelled() && ")),
    ),
    "a lane echoes the ref name": (check_lane_ref_reads, _mutate_lane_echoes_ref_name),
    "a lane step env reads the event": (
        check_lane_ref_reads,
        _mutate_lane_step_env_reads_event,
    ),
    "a lane needs another lane": (
        check_lane_routing,
        _mutate_lane_needs_another_lane,
    ),
    "change baseline runner": (check_job_matrix, _mutate_change_baseline_runner),
    "remove pwsh shell declaration": (
        check_shell_matrix,
        _mutate_remove_pwsh_shell_declaration,
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
    "comment out documented build command": (
        check_documented_build_step,
        _mutate_comment_out_documented_build,
    ),
    "comment out desktop extras install": (
        check_native_dependencies,
        _mutate_comment_out_desktop_extras_install,
    ),
    "comment out documented install command": (
        check_documented_install_lane,
        _mutate_comment_out_documented_install,
    ),
    "remove artifact-step documented install command": (
        check_documented_install_lane,
        _mutate_remove_artifact_step_documented_install,
    ),
    "drop artifact-step documented install gate": (
        check_documented_install_lane,
        _mutate_drop_artifact_step_documented_install_gate,
    ),
    "move documented install above artifact export": (
        check_native_stage_ordering,
        _mutate_move_documented_install_above_artifact_export,
    ),
    "ignore installed-service failure": (
        check_linux_service_lane,
        _mutate_ignore_installed_service_failure,
    ),
    "ignore one-wheel count failure": (
        check_baseline_wheel,
        _mutate_ignore_one_wheel_count,
    ),
    "late powershell error preference": (
        check_powershell_failure_propagation,
        _mutate_late_powershell_error_preference,
    ),
    "drop powershell error preference": (
        check_powershell_failure_propagation,
        _mutate_drop_powershell_error_preference,
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
    "duplicate installed-wheel test after service proof": (
        check_baseline_installed_wheel,
        _mutate_duplicate_installed_wheel_after_service_proof,
    ),
    "re-add desktop pass to artifact step": (
        check_once_only_proofs,
        _mutate_re_add_desktop_pass_to_artifact_step,
    ),
    "add whole-file documented-install run": (
        check_once_only_proofs,
        _mutate_add_whole_file_documented_install_run,
    ),
    "duplicate desktop source command": (
        check_once_only_proofs,
        _mutate_duplicate_desktop_source_command,
    ),
    "move installed-wheel test before wheel build": (
        check_baseline_installed_wheel,
        _mutate_move_installed_wheel_before_build,
    ),
    "remove job time limit": (check_job_time_limits, _mutate_remove_job_time_limit),
    "raise job time limit to 360": (
        check_job_time_limits,
        _mutate_raise_job_time_limit_to_six_hours,
    ),
    "quote job time limit": (check_job_time_limits, _mutate_quote_job_time_limit),
    "change job time limit within range": (
        check_job_time_limits,
        _mutate_change_job_time_limit_within_range,
    ),
    "change windows desktop job time limit within range": (
        check_job_time_limits,
        _mutate_change_desktop_windows_time_limit_within_range,
    ),
    "add job without time limit": (
        check_job_time_limits,
        _mutate_add_job_without_time_limit,
    ),
    "add job with out-of-range time limit": (
        check_job_time_limits,
        _mutate_add_job_with_out_of_range_time_limit,
    ),
    "add workflow concurrency cancellation": (
        check_no_concurrency_cancellation,
        _mutate_add_workflow_concurrency,
    ),
    "add job concurrency cancellation": (
        check_no_concurrency_cancellation,
        _mutate_add_job_concurrency,
    ),
    "drop windows suite shards": (
        check_source_suite_shards,
        _mutate_drop_windows_suite_shards,
    ),
    "duplicate a macOS suite shard": (
        check_source_suite_shards,
        _mutate_duplicate_macos_suite_shard,
    ),
    "enable fail-fast on a suite job": (
        check_source_suite_shards,
        _mutate_enable_suite_fail_fast,
    ),
    "limit suite parallelism": (
        check_source_suite_shards,
        _mutate_limit_suite_parallelism,
    ),
    "add a second matrix key": (
        check_source_suite_shards,
        _mutate_add_second_matrix_key,
    ),
    "shard option in job env": (
        check_source_suite_shards,
        _mutate_shard_option_in_job_env,
    ),
    "shard option in a suite step env": (
        check_source_suite_shards,
        _mutate_shard_option_in_suite_step_env,
    ),
    "shard option in workflow env": (
        check_source_suite_shards,
        _mutate_shard_option_in_workflow_env,
    ),
    "shard option on a package-job pytest line": (
        check_source_suite_shards,
        _mutate_shard_option_on_package_pytest_line,
    ),
    "drop the shard option": (
        check_baseline_source_suite,
        _mutate_drop_shard_option,
    ),
    "rerun the whole suite in a package job": (
        check_baseline_source_suite,
        _mutate_rerun_whole_suite_in_package_job,
    ),
    "rerun the suite in a package job without a path": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as("python -m pytest -v"),
    ),
    "rerun the suite in a package job as ./tests/": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as("python -m pytest ./tests/ -v"),
    ),
    "rerun the suite in a package job with a quoted directory": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as('python -m pytest "tests/" -v'),
    ),
    "rerun the suite in a package job from the current directory": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as("python -m pytest . -v"),
    ),
    "rerun the suite in a package job through a glob": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as("python -m pytest tests/*.py -v"),
    ),
    "rerun the suite in a package job under timeout": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as("timeout 600 pytest tests/ -v", "nyx-macos"),
    ),
    "rerun the suite in a package job through env": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as("env FOO=1 pytest tests/ -v", "nyx-macos"),
    ),
    "rerun the suite in a package job after a command separator": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as("cd . && pytest tests/ -v", "nyx-macos"),
    ),
    "rerun the suite in a package job after installing pytest": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as("python -m pip install pytest; python -m pytest -v"),
    ),
    "rerun the suite in a package job with a plugin option": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as("python -m pytest -v -p no:cacheprovider"),
    ),
    "rerun the suite in a package job ignoring one file": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as("python -m pytest -v --ignore tests/test_desktop.py"),
    ),
    "rerun the suite in a package job with an unknown option value": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as('"$runner_python" -m pytest -v --timeout 600', "nyx-macos"),
    ),
    "rerun the suite in a package job with redirected output": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as(
            '"$runner_python" -m pytest -v > build/pytest.log', "nyx-macos"
        ),
    ),
    "rerun the suite in a package job across a continuation line": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as('"$runner_python" -m pytest \\\ntests/ -v', "nyx-macos"),
    ),
    "rerun the suite in a package job across a pwsh continuation line": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as("python -m pytest tests/test_catalog.py `\ntests/ -v"),
    ),
    "rerun the suite in a package job with a multi-line keyword": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as(
            '"$runner_python" -m pytest tests/ -v -k "not desktop\nand not native"', "nyx-macos"
        ),
    ),
    "rerun the suite in a package job through bash -c": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as("bash -c 'python -m pytest tests/ -v'", "nyx-macos"),
    ),
    "rerun the suite in a package job through a pytest executable": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as(r".venv\Scripts\pytest.exe .\tests\ -v"),
    ),
    "rerun the suite in a package job through py.test": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as("py.test -v", "nyx-macos"),
    ),
    "rerun the suite in a package job from the workspace path": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as(
            '"$runner_python" -m pytest "$GITHUB_WORKSPACE/tests" -v', "nyx-macos"
        ),
    ),
    "rerun a suite shard in a package job without a path": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as(
            "python -m pytest -v --nyx-shard=${{ strategy.job-index }}/${{ strategy.job-total }}"
        ),
    ),
    "rerun the whole suite through a quoted command": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as('eval "python -m pytest tests/ -v"', "nyx-macos"),
    ),
    "rerun the whole suite through a quote-split name": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as('python -m py"test" tests/ -v', "nyx-macos"),
    ),
    "rerun the whole suite through a single-quote-split name": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as("python -m py'test' tests/ -v", "nyx-macos"),
    ),
    "rerun the whole suite through a backtick-split name": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as(".venv/Scripts/p`ytest.exe tests/ -v"),
    ),
    "rerun the whole suite through a backslash-split name": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as(r"python -m py\test tests/ -v", "nyx-macos"),
    ),
    "rerun the whole suite through an upper-case executable name": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as(r".venv\Scripts\PyTest.exe tests\ -v"),
    ),
    "copy the Linux service pytest line into another package job": (
        check_baseline_source_suite,
        _rerun_package_job_suite_as(_LINUX_SERVICE_PYTEST_LINE, "nyx-macos"),
    ),
    "add an undesignated file-level run": (
        check_baseline_source_suite,
        _mutate_add_undesignated_file_level_run,
    ),
    "drop test extras from a suite install": (
        check_baseline_source_suite,
        _mutate_drop_suite_test_extras,
    ),
    "change a suite job runner": (check_job_matrix, _mutate_change_suite_runner),
    "suite job runs on dispatch": (check_lane_routing, _mutate_suite_runs_on_dispatch),
    "remove pwsh from the windows suite step": (
        check_shell_matrix,
        _mutate_remove_suite_pwsh_shell,
    ),
    "comment out the exit guard after the windows suite": (
        check_powershell_failure_propagation,
        _mutate_comment_out_suite_exit_guard,
    ),
    "set +e before the macOS suite": (
        check_posix_failure_propagation,
        _mutate_disable_errexit_before_macos_suite,
    ),
    "remove browser prerequisite from a suite job": (
        check_baseline_browser_prerequisites,
        _mutate_remove_suite_browser_prerequisite,
    ),
    "remove browser prerequisite from the Linux suite job": (
        check_baseline_browser_prerequisites,
        _mutate_remove_linux_suite_browser_prerequisite,
    ),
    "remove browser prerequisite from the windows suite job": (
        check_baseline_browser_prerequisites,
        _mutate_remove_windows_suite_browser_prerequisite,
    ),
    "add job with an unpinned time limit": (
        check_job_time_limits,
        _mutate_add_unrouted_job,
    ),
    "change a suite job time limit within range": (
        check_job_time_limits,
        _mutate_change_suite_time_limit_within_range,
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
        "nyx-suite",
        SUITE_STEP,
        _suite_command("nyx-suite"),
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


# --------------------------------------------------------------------------
# Job time limits and the single installed-wheel proof
# --------------------------------------------------------------------------


DOCUMENTED_INSTALL_PATH = Path(__file__).with_name("test_documented_install.py")
BUILD_BOUND_JOBS = {"powershell": "nyx-desktop-windows", "bash": "nyx-desktop-macos"}


def _documented_build_bounds() -> dict[str, int]:
    """Read the build bounds from the test module without importing it."""

    tree = ast.parse(DOCUMENTED_INSTALL_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_BUILD_TIMEOUT_MINUTES"
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise WorkflowContractViolation("_BUILD_TIMEOUT_MINUTES is not defined")


def build_bound_violations(
    bounds: dict[str, int], workflow: dict[str, Any]
) -> list[str]:
    problems: list[str] = []
    for language, job_name in BUILD_BOUND_JOBS.items():
        bound = bounds.get(language)
        cap = _job(workflow, job_name).get(TIMEOUT_KEY)
        if not isinstance(bound, int) or isinstance(bound, bool) or bound <= 0:
            problems.append(f"{language}: build bound {bound!r} is not a positive integer")
        elif bound >= cap:
            problems.append(
                f"{language}: build bound {bound} min is not below the {job_name} "
                f"cap of {cap} min"
            )
    return problems


def test_documented_build_bound_is_below_each_desktop_job_cap() -> None:
    assert build_bound_violations(_documented_build_bounds(), load_workflow()) == []


@pytest.mark.parametrize(
    ("bounds", "expected"),
    [
        (
            {"powershell": 45, "bash": 10},
            ["powershell: build bound 45 min is not below the nyx-desktop-windows cap of 35 min"],
        ),
        (
            {"powershell": 20, "bash": 15},
            ["bash: build bound 15 min is not below the nyx-desktop-macos cap of 15 min"],
        ),
        ({"powershell": 20}, ["bash: build bound None is not a positive integer"]),
    ],
)
def test_build_bound_at_or_above_the_job_cap_is_rejected(
    bounds: dict[str, int], expected: list[str]
) -> None:
    assert build_bound_violations(bounds, load_workflow()) == expected


def test_suite_jobs_match_their_package_jobs() -> None:
    assert suite_pairing_violations(BASELINE_JOBS, SUITE_JOBS) == []


@pytest.mark.parametrize("key", ["runs_on", "python"])
@pytest.mark.parametrize("package_job", sorted(BASELINE_JOBS))
def test_package_job_change_without_its_suite_job_is_rejected(
    monkeypatch: pytest.MonkeyPatch, package_job: str, key: str
) -> None:
    drifted = {**BASELINE_JOBS[package_job], key: "drifted"}
    monkeypatch.setitem(BASELINE_JOBS, package_job, drifted)
    suite = next(n for n, s in SUITE_JOBS.items() if s["package_job"] == package_job)
    assert (
        f"{suite}: {key} is {SUITE_JOBS[suite][key]!r}, but package job "
        f"{package_job} has 'drifted'"
    ) in check_job_matrix(load_workflow())


def test_suite_job_change_without_its_package_job_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        SUITE_JOBS, "nyx-macos-suite", {**SUITE_JOBS["nyx-macos-suite"], "runs_on": "macos-15"}
    )
    assert (
        "nyx-macos-suite: runs_on is 'macos-15', but package job nyx-macos has 'macos-latest'"
    ) in check_job_matrix(load_workflow())


def test_altered_copy_beside_an_intact_gate_is_rejected() -> None:
    # An intact exact line remains, so only the altered-copy rule can catch a
    # failure-masked duplicate of a one-line gate.
    mutated = copy.deepcopy(load_workflow())
    _rewrite_run_line(
        mutated,
        "nyx-macos",
        BASELINE_LIFECYCLE_STEP,
        RUFF_COMMAND,
        _insert_before(f"{RUFF_COMMAND} || true"),
    )
    assert check_baseline_ruff(mutated) == [
        "nyx-macos: Ruff command 'ruff check .' is altered as 'ruff check . || true'"
    ]


# --------------------------------------------------------------------------
# The guard evaluator and the routing property read the parsed conditions
# --------------------------------------------------------------------------


def test_guard_property_reads_the_parsed_condition_not_the_constants() -> None:
    # An equivalent rewrite satisfies the property but not the exact-text rule,
    # so each check answers from its own input.
    mutated = copy.deepcopy(load_workflow())
    _job(mutated, "nyx")["if"] = (
        "!cancelled() && github.event_name == 'push' && "
        "(needs.reuse-gate.outputs.reuse_source != 'true' || github.ref != 'refs/heads/main')"
    )
    assert check_guard_property(mutated) == []
    assert any(p.startswith("nyx: job condition") for p in check_lane_routing(mutated))


def test_guard_property_failure_names_the_reuse_evidence_rule() -> None:
    mutated = copy.deepcopy(load_workflow())
    _job(mutated, "nyx-suite")["if"] = "github.event_name == 'push'"
    problems = check_guard_property(mutated)
    assert problems
    assert all("successful branch push run" in p and "dispatch run" in p for p in problems)


@pytest.mark.parametrize(
    "condition",
    [
        "github.event_name == 'push' && github.actor == 'x'",
        "github.event_name == 'push' && github.ref < 'x'",
        "!cancelled() && always()",
        "github.event_name",
        "github.event_name == 'push' ||",
        "(github.event_name == 'push'",
    ],
)
def test_guard_evaluator_raises_on_anything_unmodelled(condition: str) -> None:
    with pytest.raises(GuardEvaluationError):
        guard_runs(condition, "push", MAIN_REF, "success", {})


def test_guard_evaluator_models_implicit_success_and_absent_outputs() -> None:
    plain = "github.event_name == 'push'"
    assert guard_runs(plain, "push", MAIN_REF, "success", {})
    assert not guard_runs(plain, "push", MAIN_REF, "skipped", {})
    assert guard_runs(
        "!cancelled() && needs.reuse-gate.outputs.reuse_source != 'true'",
        "push",
        MAIN_REF,
        "skipped",
        {},
    )
