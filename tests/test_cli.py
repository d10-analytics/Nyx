"""Behavioral checks for the user-facing command dispatcher."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from nyx import cli, runtime, state

REPOSITORY_ROOT = Path(__file__).parents[1].resolve()
DESKTOP_REFUSAL = cli._DESKTOP_LIFECYCLE_REFUSAL
DESKTOP_ONLY = pytest.mark.skipif(
    not runtime.desktop_host(),
    reason="the desktop console dispatch is selected on Windows and macOS",
)


@pytest.fixture(autouse=True)
def _linux_dispatch(monkeypatch):
    """Keep the established lifecycle checks on the Linux service dispatch.

    The desktop branch is exercised explicitly below; without this selector the
    same checks would refuse the lifecycle options on Windows and macOS.
    """

    monkeypatch.setattr(runtime, "desktop_host", lambda: False)


def _subprocess_environment(home: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    return environment



def test_setup_dispatches_to_runtime_and_reports_canonical_root(capsys):
    class Configuration:
        specification_root = "/private/spec-root"

    with patch.object(runtime, "setup", return_value=Configuration()) as setup:
        assert cli.main(["--setup", "/input/link"]) == 0
    setup.assert_called_once_with("/input/link")
    assert "configured /private/spec-root" in capsys.readouterr().out


def test_start_and_stop_are_terminal_commands(capsys):
    with patch.object(runtime, "start", return_value=runtime.URL) as start, patch.object(
        runtime, "stop", return_value="stopped"
    ) as stop:
        assert cli.main([]) == 0
        assert cli.main(["--stop"]) == 0
    start.assert_called_once_with()
    stop.assert_called_once_with()
    assert capsys.readouterr().out.splitlines() == [runtime.URL, "stopped"]


@pytest.mark.parametrize(
    ("payload", "mode"),
    [
        pytest.param(
            b'{"schema_version": 1, "instance_id": "trunc',
            None,
            id="truncated-json",
        ),
        pytest.param(
            json.dumps({"schema_version": 99, "unexpected": True}).encode("utf-8"),
            None,
            id="wrong-schema",
        ),
        pytest.param(b"\xff\xfe\x00\x01", None, id="non-utf8"),
        pytest.param(
            None,
            0o000,
            id="mode-000",
            marks=pytest.mark.skipif(
                os.name == "nt" or os.geteuid() == 0,
                reason="requires POSIX permissions enforced for a non-root user",
            ),
        ),
    ],
)
def test_stop_recovery_prints_stopped_and_removes_invalid_record(
    tmp_path, capsys, payload, mode
):
    """--stop recovers an invalid record with the unchanged output contract."""
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with patch.object(state, "resolve_account_home", return_value=home), patch.object(
        state, "_current_uid", return_value=state._current_uid()
    ):
        state.setup(workspace)
        paths = state.state_paths()
    record = paths.runtime_directory / "instance.json"
    leftover = paths.runtime_directory / ".instance.json.leftover"
    leftover.write_bytes(b"leftover")
    if payload is None:
        record.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "instance_id": "stale",
                    "url": runtime.URL,
                    "capability": "stale-capability",
                    "control": runtime._control_endpoint(1),
                }
            ),
            encoding="utf-8",
        )
        record.chmod(mode)
    else:
        record.write_bytes(payload)
    try:
        with patch.object(runtime, "_paths", return_value=paths):
            assert cli.main(["--stop"]) == 0
        captured = capsys.readouterr()
        assert captured.out == "stopped\n"
        assert captured.err == ""
        assert not record.exists()
        assert not leftover.exists()
    finally:
        if record.exists():
            record.chmod(0o600)


def test_real_setup_cli_uses_portable_state_layout(capsys):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        specification = root / "spec"
        specification.mkdir()
        with patch.object(state, "resolve_account_home", return_value=home):
            assert cli.main(["--setup", str(specification)]) == 0
        paths = state.StatePaths(
            account_home=home,
            config_directory=home / ".nyx" / "config",
            config_file=home / ".nyx" / "config" / "config.json",
            state_directory=home / ".nyx",
            deployment_file=home / ".nyx" / "runtime" / "deployment.json",
            runtime_directory=home / ".nyx" / "runtime",
        )
        assert paths.config_file.is_file()
        assert json.loads(paths.config_file.read_bytes()) == {
            "schema_version": 4,
            "specification_root": str(specification.resolve()),
        }
        assert paths.runtime_directory.is_dir()
        assert not (home / ".config" / "nyx").exists()
        assert not (home / ".local" / "state" / "nyx").exists()
        assert capsys.readouterr().out == f"configured {specification.resolve()}\n"


def test_expired_start_cli_reports_bounded_error_without_creating_state(capsys):
    with TemporaryDirectory() as temporary:
        home = Path(temporary) / "home"
        home.mkdir()
        with patch.object(state, "resolve_account_home", return_value=home), patch.object(
            runtime, "STARTUP_TIMEOUT", 0.0
        ):
            assert cli.main([]) == 1
        assert not (home / ".nyx").exists()
        assert capsys.readouterr().err == "nyx: Nyx startup timed out\n"


def test_missing_configuration_cli_explains_first_setup(tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    with patch.object(state, "resolve_account_home", return_value=home):
        assert cli.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "nyx: Nyx configuration is missing; run 'nyx --setup <workspace>' first\n"
    )


def test_malformed_configuration_cli_has_no_missing_setup_hint(tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with patch.object(state, "resolve_account_home", return_value=home):
        assert cli.main(["--setup", str(workspace)]) == 0
        capsys.readouterr()
        paths = state.state_paths()
        paths.config_file.write_bytes(b"{")
        assert cli.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "nyx: Nyx configuration cannot be read\n"


@pytest.mark.parametrize("flag", [["--hide-stage", "Done"], ["--show-all-stages"]])
@pytest.mark.parametrize("with_setup", [False, True])
def test_removed_stage_options_are_unrecognized_without_mutating_state(tmp_path, capsys, flag, with_setup):
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with patch.object(state, "resolve_account_home", return_value=home):
        state.setup(workspace)
        paths = state.state_paths()
        before = paths.config_file.read_bytes()
        arguments = (["--setup", str(workspace)] if with_setup else []) + flag
        with pytest.raises(SystemExit) as error:
            cli.main(arguments)
        assert error.value.code == 2
        assert "unrecognized arguments:" in capsys.readouterr().err
        assert paths.config_file.read_bytes() == before


def test_help_names_the_configured_location_workspace_and_preserves_spec_root(
    capsys,
):
    with pytest.raises(SystemExit) as error:
        cli.main(["--help"])

    assert error.value.code == 0
    captured = capsys.readouterr()
    assert "--hide-stage" not in captured.out
    assert "--show-all-stages" not in captured.out
    assert "--setup SPEC_ROOT" in captured.out
    assert "configure the Workspace at SPEC_ROOT" in captured.out
    normalized = " ".join(captured.out.split())
    assert "stop the running Nyx board" in normalized
    assert "report configuration and runtime state without changing either" in normalized
    assert (
        f"Start the Nyx board at {runtime.URL}, or reuse the one already running."
    ) in normalized
    assert (
        "--setup, --stop and --status are available on Linux only; on Windows "
        "and macOS, open the Nyx application and use its workspace controls. "
        "Run 'nyx --setup <workspace>' once before the first start."
    ) in normalized
    assert captured.err == ""


def test_active_setup_rejects_changed_root_without_cli_mutation(capsys):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        first = root / "first"
        first.mkdir()
        second = root / "second"
        second.mkdir()
        with patch.object(state, "resolve_account_home", return_value=home), patch.object(
            state, "_current_uid", return_value=state._current_uid()
        ):
            state.setup(first)
            paths = state.state_paths()
        lease = runtime._lease_lock(paths, timeout=0.0)
        assert lease.acquire(blocking=False)
        before = paths.config_file.read_bytes()
        try:
            arguments = ["--setup", str(second)]
            with patch.object(runtime, "_paths", return_value=paths):
                assert cli.main(arguments) == 1
        finally:
            lease.close()
        assert paths.config_file.read_bytes() == before
        assert "stop Nyx" in capsys.readouterr().err


def test_active_setup_with_same_root_leaves_checkout_record_unchanged(capsys):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        first = root / "first"
        first.mkdir()
        with patch.object(state, "resolve_account_home", return_value=home), patch.object(
            state, "_current_uid", return_value=state._current_uid()
        ):
            state.setup(first)
            paths = state.state_paths()
        settings = {str(first.resolve()): {"folders": [str(root / "one")], "github_lookup": True}}
        paths.config_file.write_bytes(
            (
                json.dumps(
                    {
                        "schema_version": 4,
                        "specification_root": str(first.resolve()),
                        "checkout_settings": settings,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        )
        lease = runtime._lease_lock(paths, timeout=0.0)
        assert lease.acquire(blocking=False)
        before = paths.config_file.read_bytes()
        try:
            with patch.object(runtime, "_paths", return_value=paths):
                assert cli.main(["--setup", str(first)]) == 0
        finally:
            lease.close()
        assert paths.config_file.read_bytes() == before


def test_status_renders_configured_stopped_snapshot_with_ascii_json_and_no_lifecycle(
    capsys,
):
    configuration = state.ConfigurationObservation(
        "configured",
        specification_root=Path("/private/spec\n-root\x1b[31m\u0085"),
    )
    runtime_observation = runtime.RuntimeObservation("not_running")
    with patch.object(state, "observe_configuration", return_value=configuration) as observe_config, patch.object(
        runtime, "observe_runtime", return_value=runtime_observation
    ) as observe_runtime, patch.object(runtime, "start") as start, patch.object(
        runtime, "stop"
    ) as stop, patch.object(runtime, "setup") as setup:
        assert cli.main(["--status"]) == 0

    observe_config.assert_called_once_with()
    observe_runtime.assert_called_once_with()
    start.assert_not_called()
    stop.assert_not_called()
    setup.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "Configuration: configured",
        f"Workspace: {cli._json_literal(str(configuration.specification_root))}",
        "Runtime: not running",
    ]
    assert captured.err == ""


def test_status_running_prints_verified_url_after_runtime(capsys):
    configuration = state.ConfigurationObservation("not_configured")
    runtime_observation = runtime.RuntimeObservation(
        "running", url="http://127.0.0.1:8765/\n"
    )
    with patch.object(state, "observe_configuration", return_value=configuration) as observe_config, patch.object(
        runtime, "observe_runtime", return_value=runtime_observation
    ) as observe_runtime:
        assert cli.main(["--status"]) == 0

    observe_config.assert_called_once_with()
    observe_runtime.assert_called_once_with()
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "Configuration: not configured",
        "Workspace: not configured",
        "Runtime: running",
        'URL: "http://127.0.0.1:8765/\\n"',
    ]
    assert captured.err == ""


def test_status_keeps_runtime_result_when_configuration_is_unavailable(capsys):
    configuration = state.ConfigurationObservation(
        "unavailable", diagnostic="persisted secret must not be printed"
    )
    runtime_observation = runtime.RuntimeObservation(
        "running", url=runtime.URL
    )
    with patch.object(state, "observe_configuration", return_value=configuration), patch.object(
        runtime, "observe_runtime", return_value=runtime_observation
    ):
        assert cli.main(["--status"]) == 1

    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "Configuration: unavailable",
        "Workspace: unavailable",
        "Runtime: running",
        f'URL: "{runtime.URL}"',
        "Diagnostic: configuration unavailable",
    ]
    assert "persisted secret" not in captured.out
    assert captured.err == ""


def test_status_collapses_malformed_persisted_root_and_preserves_runtime_sibling(capsys):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        home_patch = patch.object(state, "resolve_account_home", return_value=home)
        uid_patch = patch.object(state, "_current_uid", return_value=state._current_uid())
        with home_patch, uid_patch:
            paths = state.state_paths(create=True)
            paths.config_file.write_text(
                '{"schema_version":2,"hidden_stages":[],"specification_root":"/\\u0000"}\n',
                encoding="utf-8",
            )
            os.chmod(paths.config_file, 0o600)
            before = paths.config_file.read_bytes()

            assert cli.main(["--status"]) == 1

        captured = capsys.readouterr()
        assert captured.out.splitlines() == [
            "Configuration: unavailable",
            "Workspace: unavailable",
            "Runtime: not running",
            "Diagnostic: configuration unavailable",
        ]
        assert captured.err == ""
        assert paths.config_file.read_bytes() == before


def test_status_keeps_configuration_result_when_runtime_is_unknown_and_bounds_diagnostic(
    capsys,
):
    configuration = state.ConfigurationObservation(
        "configured", specification_root=Path("/private/spec")
    )
    runtime_observation = runtime.RuntimeObservation(
        "unknown", diagnostic="raw runtime exception"
    )
    with patch.object(state, "observe_configuration", return_value=configuration), patch.object(
        runtime, "observe_runtime", return_value=runtime_observation
    ):
        assert cli.main(["--status"]) == 1

    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "Configuration: configured",
        f"Workspace: {cli._json_literal(str(configuration.specification_root))}",
        "Runtime: unknown",
        "Diagnostic: runtime state unavailable",
    ]
    assert "raw runtime exception" not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    ("arguments", "error_fragment"),
    [
        (["--status", "--setup", "/tmp/spec"], "argument --setup: not allowed with argument --status"),
        (["--status", "--stop"], "argument --stop: not allowed with argument --status"),
        (["--status", "--hide-stage", "Queue"], "unrecognized arguments:"),
        (["--status", "--show-all-stages"], "unrecognized arguments:"),
    ],
)
def test_status_rejects_other_commands_before_observing(arguments, error_fragment, capsys):
    with patch.object(state, "observe_configuration") as observe_config, patch.object(
        runtime, "observe_runtime"
    ) as observe_runtime, patch.object(runtime, "setup") as setup, patch.object(
        runtime, "start"
    ) as start, patch.object(runtime, "stop") as stop:
        with pytest.raises(SystemExit) as error:
            cli.main(arguments)

    assert error.value.code == 2
    observe_config.assert_not_called()
    observe_runtime.assert_not_called()
    setup.assert_not_called()
    start.assert_not_called()
    stop.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("usage: nyx ")
    assert error_fragment in captured.err


def test_status_orders_configuration_and_runtime_diagnostics_after_both_observations(
    capsys,
):
    configuration = state.ConfigurationObservation("unavailable")
    runtime_observation = runtime.RuntimeObservation(
        "unknown", diagnostic=runtime.RUNTIME_CONTROL_TIMED_OUT
    )
    with patch.object(state, "observe_configuration", return_value=configuration) as observe_config, patch.object(
        runtime, "observe_runtime", return_value=runtime_observation
    ) as observe_runtime:
        assert cli.main(["--status"]) == 1

    observe_config.assert_called_once_with()
    observe_runtime.assert_called_once_with()
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "Configuration: unavailable",
        "Workspace: unavailable",
        "Runtime: unknown",
        "Diagnostic: configuration unavailable",
        "Diagnostic: runtime control timed out",
    ]
    assert captured.err == ""


def test_desktop_console_launches_the_owned_application(monkeypatch):
    monkeypatch.setattr(runtime, "desktop_host", lambda: True)
    launched: list[str] = []
    monkeypatch.setattr(cli, "_launch_desktop", lambda: launched.append("app") or 0)
    with patch.object(runtime, "setup") as setup, patch.object(
        runtime, "start"
    ) as start, patch.object(runtime, "stop") as stop:
        assert cli.main([]) == 0
    assert launched == ["app"]
    setup.assert_not_called()
    start.assert_not_called()
    stop.assert_not_called()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--setup", "/tmp/spec"],
        ["--status"],
        ["--stop"],
    ],
)
def test_desktop_console_refuses_retired_commands_before_any_lifecycle(
    monkeypatch, capsys, arguments
):
    monkeypatch.setattr(runtime, "desktop_host", lambda: True)
    with patch.object(runtime, "setup") as setup, patch.object(
        runtime, "start"
    ) as start, patch.object(runtime, "stop") as stop, patch.object(
        state, "observe_configuration"
    ) as observe_config, patch.object(
        runtime, "observe_runtime"
    ) as observe_runtime, patch.object(cli, "_launch_desktop") as launch:
        assert cli.main(arguments) == 2
    for operation in (setup, start, stop, observe_config, observe_runtime, launch):
        operation.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"nyx: {DESKTOP_REFUSAL}\n"


def test_desktop_console_refusal_leaves_configuration_and_state_untouched(
    monkeypatch, capsys
):
    monkeypatch.setattr(runtime, "desktop_host", lambda: True)
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        specification = root / "spec"
        specification.mkdir()
        with patch.object(state, "resolve_account_home", return_value=home), patch.object(
            state, "_current_uid", return_value=state._current_uid()
        ):
            state.setup(specification)
            paths = state.state_paths()
        before = paths.config_file.read_bytes()
        assert cli.main(["--setup", str(specification)]) == 2
        assert cli.main(["--status"]) == 2
        assert cli.main(["--stop"]) == 2
        assert paths.config_file.read_bytes() == before
        assert not paths.runtime_directory.joinpath("instance.json").exists()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"nyx: {DESKTOP_REFUSAL}\n" * 3


@DESKTOP_ONLY
def test_module_launch_refuses_retired_options_on_a_desktop_host(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    completed = subprocess.run(
        [sys.executable, "-m", "nyx.cli", "--status"],
        cwd=REPOSITORY_ROOT,
        env=_subprocess_environment(home),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == f"nyx: {DESKTOP_REFUSAL}\n"
    assert not (home / ".nyx").exists()


def test_status_is_configured_after_a_stored_workspace_is_moved_and_relinked(capsys):
    from test_state import stale_key_scenario

    with TemporaryDirectory() as temporary:
        scenario = stale_key_scenario(Path(temporary))
        home_patch, uid_patch = scenario["home_patches"]
        with home_patch, uid_patch:
            assert cli.main(["--status"]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == "Configuration: configured"
        assert lines[1] == f"Workspace: {cli._json_literal(str(scenario['b']))}"


@pytest.mark.parametrize("kind", ["deep", "not_json"])
def test_status_reports_unavailable_without_traceback_for_unparseable_record(kind, capsys):
    from test_state import DEEP_LEVELS, deep_array

    with TemporaryDirectory() as temporary:
        home = Path(temporary) / "home"
        home.mkdir()
        home_patch = patch.object(state, "resolve_account_home", return_value=home)
        uid_patch = patch.object(state, "_current_uid", return_value=state._current_uid())
        with home_patch, uid_patch:
            paths = state.state_paths(create=True)
            text = deep_array(DEEP_LEVELS) if kind == "deep" else "not json at all"
            if kind == "deep":
                with pytest.raises(RecursionError):
                    json.loads(text)
            paths.config_file.write_text(text, encoding="utf-8")
            os.chmod(paths.config_file, 0o600)
            assert cli.main(["--status"]) == 1
        captured = capsys.readouterr()
        assert "Configuration: unavailable" in captured.out.splitlines()
        assert "Traceback" not in captured.out + captured.err


# ---------------------------------------------------------------------------
# nyx checkouts
# ---------------------------------------------------------------------------

import test_checkouts  # noqa: E402
from test_checkouts import (  # noqa: E402
    GLOBAL_CONFIG,
    add_packed_refs,
    allow_filters,
    clone,
    closed_port,
    commit_file,
    git,
    make_remote,
    real,
    trust_only,
    web_clone,
)

from nyx import checkouts, models  # noqa: E402

recorders = test_checkouts.recorders

NO_FOLDERS = "no checkout folders are configured"
UNAVAILABLE = "nyx: Nyx configuration is unavailable\n"


@pytest.fixture
def account(tmp_path, monkeypatch):
    """An isolated account home that is also the only git configuration."""

    home = tmp_path / "account"
    home.mkdir(mode=0o755)
    (home / ".gitconfig").write_text(GLOBAL_CONFIG)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in list(os.environ):
        if name.startswith("GIT_") and name != "GIT_CONFIG_NOSYSTEM":
            monkeypatch.delenv(name)
    return home


def _write_record(tmp_path: Path, payload: dict | bytes) -> Path:
    paths = state.state_paths(create=True)
    if isinstance(payload, bytes):
        paths.config_file.write_bytes(payload)
    else:
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        paths.config_file.write_bytes(data.encode("utf-8"))
    return paths.config_file


def _configure(tmp_path: Path, folders: list[Path | str]) -> Path:
    """Write a schema 4 record for a workspace whose settings name ``folders``."""

    specification = tmp_path / "spec"
    specification.mkdir(exist_ok=True)
    key = str(specification.resolve())
    entry = {"folders": sorted(str(folder) for folder in folders), "github_lookup": False}
    return _write_record(
        tmp_path,
        {
            "schema_version": 4,
            "specification_root": key,
            "checkout_settings": {key: entry},
        },
    )


def _run_checkouts(capsys, *arguments: str) -> tuple[int, str, str]:
    code = cli.main(["checkouts", *arguments])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _plain_lines(capsys, *arguments: str) -> list[str]:
    code, out, err = _run_checkouts(capsys, *arguments)
    assert (code, err) == (0, "")
    return out.splitlines()


def _line_for(lines: list[str], path: Path | str, indent: str = "") -> str:
    literal = indent + cli._json_literal(real(path))
    [line] = [
        candidate
        for candidate in lines
        if candidate == literal or candidate.startswith(literal + "  ")
    ]
    return line


def _file_listing(root: Path) -> list[str]:
    """Every path under ``root``, relative and with ``/`` separators on every platform."""
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def test_module_launch_prints_a_valid_document_and_adds_only_the_cache_file(
    tmp_path, account, monkeypatch
):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "listed")
    git(repository, "checkout", "-q", "-b", "picked", "trunk")
    commit_file(repository, "picked.txt", "picked\n")
    git(repository, "checkout", "-q", "trunk")
    commit_file(repository, "diverged.txt", "diverged\n")
    git(repository, "cherry-pick", "picked")
    git(repository, "push", "-q", "origin", "trunk")
    tip = git(repository, "rev-parse", "picked").stdout.strip()
    base = git(repository, "rev-parse", "origin/trunk").stdout.strip()
    record = _configure(tmp_path, [folder])
    before = record.read_bytes()
    listing = _file_listing(account / ".nyx")
    runtime_before = state.observe_runtime()

    completed = subprocess.run(
        [sys.executable, "-m", "nyx.cli", "checkouts", "--json"],
        cwd=REPOSITORY_ROOT,
        env=_subprocess_environment(account),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    document = models.parse_checkouts(completed.stdout)
    [item] = [c for r in document["repositories"] for c in r["clones"]]
    assert item["path"] == real(repository)
    assert document["diagnostics"] == []
    [picked] = [b for b in item["branches"] if b["name"] == "picked"]
    assert picked["verdict"] == {
        "state": "merged", "rung": "patch_equivalent", "reason": None,
    }
    assert record.read_bytes() == before
    after = _file_listing(account / ".nyx")
    assert sorted(set(after) - set(listing)) == ["cache", "cache/checkouts.json"]
    assert set(listing) <= set(after)
    stored = json.loads((account / ".nyx" / "cache" / "checkouts.json").read_text())
    assert set(stored) == {"schema_version", "verdicts"}
    assert stored["schema_version"] == 1
    assert {key: entry["rung"] for key, entry in stored["verdicts"].items()} == {
        f"{tip}.{base}": "patch_equivalent"
    }
    assert not any(name.endswith(("instance.json", ".lock")) for name in after)
    assert "runtime" not in {name.split("/")[0] for name in set(after) - set(listing)}
    assert state.observe_runtime() == runtime_before


def test_module_launch_with_an_empty_home_exits_zero_and_creates_no_state(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    completed = subprocess.run(
        [sys.executable, "-m", "nyx.cli", "checkouts", "--json"],
        cwd=REPOSITORY_ROOT,
        env=_subprocess_environment(home),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0
    assert NO_FOLDERS in completed.stdout
    assert not (home / ".nyx").exists()


def test_checkouts_runs_on_a_desktop_host_without_launching_the_application(
    tmp_path, account, monkeypatch, capsys
):
    monkeypatch.setattr(runtime, "desktop_host", lambda: True)
    with patch.object(cli, "_launch_desktop") as launch, patch.object(
        runtime, "observe_runtime"
    ) as observe_runtime:
        code, out, _ = _run_checkouts(capsys)
    assert code == 0
    assert NO_FOLDERS in out
    launch.assert_not_called()
    observe_runtime.assert_not_called()


def test_without_configuration_one_line_is_printed_and_nothing_is_created(
    account, capsys
):
    code, out, err = _run_checkouts(capsys)
    assert (code, err) == (0, "")
    assert len(out.splitlines()) == 1
    assert NO_FOLDERS in out
    assert not (account / ".nyx").exists()


def test_configuration_without_folders_for_the_active_workspace_is_said_so(
    tmp_path, account, capsys
):
    specification = tmp_path / "spec"
    specification.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    _write_record(
        tmp_path,
        {
            "schema_version": 4,
            "specification_root": str(specification.resolve()),
            "checkout_settings": {
                str(other.resolve()): {"folders": [str(tmp_path)], "github_lookup": False}
            },
        },
    )
    code, out, err = _run_checkouts(capsys)
    assert (code, err) == (0, "")
    assert len(out.splitlines()) == 1
    assert NO_FOLDERS in out


def test_unreadable_record_prints_only_the_unavailable_line_and_exits_one(
    tmp_path, account, capsys
):
    record = _configure(tmp_path, [tmp_path])
    record.write_bytes(b"{ not json")
    code, out, err = _run_checkouts(capsys, "--json")
    assert (code, out, err) == (1, "", UNAVAILABLE)


def test_a_document_over_two_mebibytes_is_refused_with_one_error_line(
    tmp_path, account, capsys
):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    # Each clone holds the most branches one clone may (the default and 99
    # more), all at the default's tip, and long names fill the document.
    for index in range(48):
        repository = clone(remote, folder / f"many-{index:03d}")
        head = git(repository, "rev-parse", "HEAD").stdout.strip()
        add_packed_refs(
            repository,
            {f"refs/heads/{index:03d}-{n:03d}-" + "x" * 230: head for n in range(99)},
        )
    assert len(git(repository, "for-each-ref", "refs/heads").stdout.splitlines()) == 100
    _configure(tmp_path, [folder])

    for arguments in ((), ("--json",), ("--no-network",), ("--json", "--no-network")):
        code, out, err = _run_checkouts(capsys, *arguments)
        assert code == 1
        assert out == ""
        assert err.startswith("nyx: ")
        assert "checkouts_output_too_large" in err
        assert len(err.splitlines()) == 1


def test_missing_git_exits_zero_in_both_modes(tmp_path, account, monkeypatch, capsys):
    folder = tmp_path / "folder"
    folder.mkdir()
    _configure(tmp_path, [folder])
    empty = tmp_path / "no-git"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    code, out, err = _run_checkouts(capsys, "--json")
    assert (code, err) == (0, "")
    assert models.parse_checkouts(out)["git"] == {"status": "missing", "version": None}

    code, out, err = _run_checkouts(capsys)
    assert (code, err) == (0, "")
    assert out.splitlines()[0] == "git missing"


@pytest.mark.parametrize(
    "arguments",
    [
        ["checkouts", "--status"],
        ["checkouts", "--stop"],
        ["--setup", "/some/root", "checkouts"],
        ["checkouts", "--setup", "/some/root"],
        ["--json"],
        ["--no-network"],
        ["--json", "--no-network"],
        ["--setup", "/some/root", "--no-network"],
        ["checkouts", "--status", "--no-network"],
        ["checkouts", "extra"],
    ],
)
def test_checkouts_combinations_are_usage_errors_that_never_read_state(
    arguments, capsys
):
    with patch.object(state, "observe_configuration") as observe_config, patch.object(
        runtime, "observe_runtime"
    ) as observe_runtime, patch.object(runtime, "setup") as setup, patch.object(
        runtime, "start"
    ) as start, patch.object(runtime, "stop") as stop:
        with pytest.raises(SystemExit) as error:
            cli.main(arguments)
    assert error.value.code == 2
    for operation in (observe_config, observe_runtime, setup, start, stop):
        operation.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("usage: nyx ")


def test_folder_and_diagnostic_lines_precede_the_clones(tmp_path, account, capsys):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "real")
    (folder / "fake" / ".git" / "info").mkdir(parents=True)
    absent = tmp_path / "absent"
    _configure(tmp_path, [folder, absent])

    lines = _plain_lines(capsys)

    assert lines[0] == f"folder {cli._json_literal(str(absent))} missing"
    assert lines[1] == f"diagnostic not_a_repository {cli._json_literal(str(folder / 'fake'))}"
    assert lines[2].startswith(cli._json_literal(real(repository)) + "  ")
    assert len(lines) == 3


def test_clone_line_carries_head_real_counts_and_default_facts(tmp_path, account, capsys):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "work")
    commit_file(repository, "tracked.txt", "one\n")
    commit_file(repository, "other.txt", "one\n")
    git(repository, "checkout", "-q", "-b", "feature")
    (repository / "tracked.txt").write_text("two\n")
    git(repository, "add", "tracked.txt")
    (repository / "other.txt").write_text("two\n")
    (repository / "new-a.txt").write_text("a\n")
    (repository / "new-b.txt").write_text("b\n")
    _configure(tmp_path, [folder])

    line = _line_for(_plain_lines(capsys, "--no-network"), repository)

    elements = line.split("  ")[1:]
    assert elements[0] == "branch feature"
    assert elements[1] == "staged 1 unstaged 1 untracked 2 conflicted 0"
    assert elements[2] == "remote off"
    assert elements[3] == "branches cached local-only 1"
    assert elements[4] == "default trunk cached ahead never fetched"
    assert not any(element.startswith("operations") for element in elements)


def test_cached_default_shows_the_fetch_time_once_the_clone_has_fetched(
    tmp_path, account, capsys
):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "work")
    git(repository, "fetch", "-q", "origin")
    _configure(tmp_path, [folder])

    line = _line_for(_plain_lines(capsys, "--no-network"), repository)

    assert re.fullmatch(
        r"default trunk cached equal fetched [0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z",
        line.split("  ")[-1],
    )
    assert re.search(r"cached [0-9]{4}-", line) is None


def test_conflicted_merge_prints_its_operation_and_conflicted_count(
    tmp_path, account, capsys
):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "work")
    git(repository, "checkout", "-q", "-b", "other")
    commit_file(repository, "file.txt", "other\n")
    git(repository, "checkout", "-q", "trunk")
    commit_file(repository, "file.txt", "mine\n")
    git(repository, "merge", "other", check=False)
    _configure(tmp_path, [folder])

    elements = _line_for(_plain_lines(capsys), repository).split("  ")[1:]

    assert elements[:3] == [
        "branch trunk",
        "staged 0 unstaged 0 untracked 0 conflicted 1",
        "operations merge",
    ]


def test_linked_worktrees_are_indented_lines_under_their_clone(tmp_path, account, capsys):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "work")
    outside = tmp_path / "outside" / "linked"
    git(repository, "worktree", "add", "-q", "-b", "side", str(outside))
    detached = tmp_path / "outside" / "detached"
    git(repository, "worktree", "add", "-q", "--detach", str(detached))
    sha = git(repository, "rev-parse", "HEAD").stdout.strip()
    _configure(tmp_path, [folder])

    lines = _plain_lines(capsys)

    clone_index = lines.index(_line_for(lines, repository))
    linked = _line_for(lines, outside, "  ")
    assert linked == (
        f"  {cli._json_literal(real(outside))}  branch side  "
        "staged 0 unstaged 0 untracked 0 conflicted 0"
    )
    assert _line_for(lines, detached, "  ").split("  ")[2] == f"detached {sha}"
    assert lines.index(linked) > clone_index


def test_bare_repository_clone_line_has_no_head_or_counts_but_its_worktree_has(
    tmp_path, account, capsys
):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    linked = folder / "bare-linked"
    git(remote, "worktree", "add", "-q", str(linked), "trunk")
    _configure(tmp_path, [folder])

    lines = _plain_lines(capsys)

    assert _line_for(lines, remote) == f"{cli._json_literal(real(remote))}  no origin remote"
    assert _line_for(lines, linked, "  ").split("  ")[2] == "branch trunk"


def test_refused_clone_prints_its_status_instead_of_the_facts(
    tmp_path, account, monkeypatch, capsys
):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    trusted = clone(remote, folder / "trusted")
    refused = clone(remote, folder / "refused")
    trust_only(account, [trusted])
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    _configure(tmp_path, [folder])

    lines = _plain_lines(capsys)

    assert _line_for(lines, refused) == f"{cli._json_literal(real(refused))}  unsafe_ownership"
    assert "no origin remote" not in _line_for(lines, refused)
    assert "branch trunk" in _line_for(lines, trusted)


def test_tree_filtered_clone_prints_main_failed_and_keeps_remote_and_default(
    tmp_path, account, capsys
):
    remote = make_remote(tmp_path)
    allow_filters(remote)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote.as_uri(), folder / "trees", "--filter=tree:0", "--no-checkout")
    _configure(tmp_path, [folder])

    elements = _line_for(_plain_lines(capsys, "--no-network"), repository).split("  ")[1:]

    assert elements[0] == "main failed"
    assert elements[1] == "remote off"
    assert elements[2] == "branches cached local-only 0"
    assert elements[3].startswith("default trunk cached ")
    assert not any(element.startswith(("branch ", "staged ")) for element in elements)


def test_clone_without_origin_prints_no_origin_remote_and_no_remote_counts(
    tmp_path, account, capsys
):
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = folder / "local"
    git(folder, "init", "-q", "-b", "main", str(repository))
    commit_file(repository, "file.txt", "one\n")
    git(repository, "branch", "wip")
    _configure(tmp_path, [folder])

    line = _line_for(_plain_lines(capsys), repository)

    assert line.split("  ")[2:] == [
        "staged 0 unstaged 0 untracked 0 conflicted 0",
        "no origin remote",
    ]
    assert "local-only" not in line
    assert "default" not in line


def test_clone_of_an_empty_remote_under_no_network_prints_no_cached_default(
    tmp_path, account, capsys
):
    empty = tmp_path / "empty.git"
    git(tmp_path, "init", "-q", "--bare", str(empty))
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(empty, folder / "fresh")
    commit_file(repository, "file.txt", "one\n")
    _configure(tmp_path, [folder])

    elements = _line_for(_plain_lines(capsys, "--no-network"), repository).split("  ")[1:]

    assert elements[-3:] == ["remote off", "branches cached local-only 1", "no cached default"]


def test_cut_comparison_prints_unknown_after_local_only(tmp_path, account):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "slow")
    git(repository, "checkout", "-q", "-b", "feature")
    git(repository, "push", "-q", "origin", "feature")
    make_slow_clone_in_place(repository)

    text = checkouts.collect_checkouts(
        [str(folder)], deadline_ns=time.monotonic_ns() + 1_000_000_000
    )
    lines = cli.format_checkouts(models.parse_checkouts(text))

    elements = _line_for(lines, repository).split("  ")[1:]
    assert elements[0] == "main timed_out"
    assert elements[1] == "remote off"
    assert elements[2] == "branches cached local-only 0 unknown 1"
    assert elements[3].startswith("default trunk cached equal ")


def test_a_clone_over_the_branch_cap_prints_only_its_status(tmp_path, account, capsys):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "crowded")
    head = git(repository, "rev-parse", "HEAD").stdout.strip()
    add_packed_refs(
        repository, {f"refs/heads/extra-{n:03d}": head for n in range(models.CHECKOUT_BRANCH_CAP)}
    )
    assert len(git(repository, "for-each-ref", "refs/heads").stdout.splitlines()) == 101
    sibling = clone(remote, folder / "ordinary")
    _configure(tmp_path, [folder])

    lines = _plain_lines(capsys, "--no-network")

    assert _line_for(lines, repository) == f"{cli._json_literal(real(repository))}  too_many_branches"
    # The sibling clone of the same remote keeps its ordinary line.
    assert "too_many_branches" not in _line_for(lines, sibling)


def make_slow_clone_in_place(repository: Path) -> None:
    """One more commit on ``feature`` and a stat-dirty file under a slow clean filter."""

    commit_file(repository, "slow.txt", "slow\n", "one unpushed commit")
    (repository / ".git" / "info" / "attributes").write_text("slow.txt filter=slow\n")
    git(repository, "config", "filter.slow.clean", "sleep 5; cat")
    later = time.time_ns() + 5_000_000_000
    os.utime(repository / "slow.txt", ns=(later, later))


@pytest.mark.parametrize(
    "arguments", [(), ("--json",), ("--no-network",), ("--json", "--no-network")]
)
def test_origin_credentials_never_reach_either_output(tmp_path, account, capsys, arguments):
    remote = make_remote(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    repository = clone(remote, folder / "work")
    git(
        repository,
        "remote",
        "set-url",
        "origin",
        "https://someuser:s3cr3tpass@example.invalid/owner/repo.git",
    )
    _configure(tmp_path, [folder])

    code, out, err = _run_checkouts(capsys, *arguments)

    assert (code, err) == (0, "")
    assert cli._json_literal(real(repository)) in out
    assert "s3cr3tpass" not in out
    assert "someuser" not in out


# ---------------------------------------------------------------------------
# nyx checkouts: networked default, --no-network and the plain-output grammar
# ---------------------------------------------------------------------------

REMOTE_ELEMENT = re.compile(
    r"remote (?P<state>live|off|failed (?P<reason>unreachable|not_found|"
    r"authentication_failed|timed_out|failed))"
)
BRANCHES_ELEMENT = re.compile(
    r"branches (?P<basis>live|cached) local-only (?P<local_only>[0-9]+)"
    r"(?: unknown (?P<unknown>[0-9]+))?"
)
FRESHNESS = r"(?:never fetched|fetched (?P<fetched>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z))"
LIVE_DEFAULT = re.compile(
    r"default (?P<name>\S+) live (?P<live>[a-z]+) cached (?P<cached>[a-z]+) "
    r"tracking live (?P<tracking>[a-z]+) " + FRESHNESS
)
CACHED_DEFAULT = re.compile(
    r"default (?P<name>\S+) cached (?P<cached>[a-z]+) " + FRESHNESS
)
NO_DEFAULT = re.compile(r"no (?P<basis>live|cached) default")


def _module_run(account: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "nyx.cli", *arguments],
        cwd=REPOSITORY_ROOT,
        env=_subprocess_environment(account),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _relation(comparison: dict | None) -> str:
    return "unknown" if comparison is None else comparison["relation"]


def _assert_line_matches_document(line: str, group: dict, item: dict) -> dict:
    """Parse the three trailing elements and compare each captured field to the document."""

    remote, branches, default = line.split("  ")[-3:]
    check = group["remote"]["check"]
    parsed = REMOTE_ELEMENT.fullmatch(remote)
    assert parsed, remote
    assert parsed["state"].split()[0] == {"live": "live", "off": "off", "failed": "failed"}[
        check["status"]
    ]
    assert parsed["reason"] == (check["reason"] if check["status"] == "failed" else None)

    basis = "live" if check["status"] == "live" else "cached"
    counted = BRANCHES_ELEMENT.fullmatch(branches)
    assert counted, branches
    relations = [_relation(branch["vs_remote"]) for branch in item["branches"]]
    assert counted["basis"] == basis
    assert int(counted["local_only"]) == relations.count("absent")
    assert counted["unknown"] == (
        str(relations.count("unknown")) if "unknown" in relations else None
    )
    assert {b["vs_remote"]["basis"] for b in item["branches"]} <= {basis}

    local = item["local_default"]
    fetched = item["cached_default"]["last_fetched_at"]
    if local is None:
        found = NO_DEFAULT.fullmatch(default)
        assert found and found["basis"] == basis
        return {"default": None}
    if basis == "live":
        found = LIVE_DEFAULT.fullmatch(default)
        assert found, default
        assert found["live"] == _relation(local["vs_live"])
        assert found["tracking"] == _relation(item["cached_vs_live"])
    else:
        found = CACHED_DEFAULT.fullmatch(default)
        assert found, default
    assert found["name"] == local["name"]
    assert found["cached"] == _relation(local["vs_cached"])
    assert found["fetched"] == fetched
    return {"default": default}


def _origin_lines(lines: list[str], document: dict) -> dict[str, tuple[str, dict, dict]]:
    found = {}
    for group in document["repositories"]:
        if group["remote"] is None:
            continue
        for item in group["clones"]:
            if item["status"] == "ok":
                found[item["path"]] = (_line_for(lines, item["path"]), group, item)
    return found


@pytest.fixture
def grammar_scenario(tmp_path, account, recorders):
    recorder = recorders()
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()

    alpha = make_remote(served, "alpha.git")
    fetched = web_clone(recorder, "alpha", alpha, folder / "a-fetched", "a-fetched")
    git(fetched, "fetch", "-q", "origin")
    never = web_clone(recorder, "alpha", alpha, folder / "b-never", "b-never")
    git(never, "checkout", "-q", "-b", "feature")
    bare_branches = web_clone(recorder, "alpha", alpha, folder / "c-no-branches", "c-none")
    git(bare_branches, "checkout", "-q", "--detach")
    git(bare_branches, "update-ref", "-d", "refs/heads/trunk")
    git(bare_branches, "update-ref", "-d", "refs/heads/feature", check=False)

    detached_remote = make_remote(served, "detached.git")
    detached = web_clone(recorder, "detached", detached_remote, folder / "d-detached", "d")
    sha = git(detached_remote, "rev-parse", "trunk").stdout.strip()
    git(detached_remote, "update-ref", "--no-deref", "HEAD", sha)

    refused = clone(make_remote(served, "refused.git"), folder / "e-refused")
    git(
        refused,
        "remote",
        "set-url",
        "origin",
        f"http://someuser:s3cr3tpass@127.0.0.1:{closed_port()}/refused.git",
    )

    empty_remote = served / "empty.git"
    git(served, "init", "-q", "--bare", str(empty_remote))
    empty = clone(empty_remote, folder / "f-empty")

    _configure(tmp_path, [folder])
    return {
        "recorder": recorder,
        "paths": {
            "fetched": fetched,
            "never": never,
            "no_branches": bare_branches,
            "detached": detached,
            "refused": refused,
            "empty": empty,
        },
    }


def _both_outputs(account: Path, *flags: str) -> tuple[list[str], dict, str]:
    plain = _module_run(account, "checkouts", *flags)
    document_run = _module_run(account, "checkouts", "--json", *flags)
    assert (plain.returncode, plain.stderr) == (0, "")
    assert (document_run.returncode, document_run.stderr) == (0, "")
    return (
        plain.stdout.splitlines(),
        models.parse_checkouts(document_run.stdout),
        plain.stdout + document_run.stdout,
    )


def test_live_plain_lines_follow_the_grammar_and_agree_with_the_document(
    grammar_scenario, account
):
    paths = grammar_scenario["paths"]
    lines, document, text = _both_outputs(account)

    assert document["remote_checks"] == "on"
    by_path = _origin_lines(lines, document)
    parsed = {
        name: _assert_line_matches_document(*by_path[real(path)])
        for name, path in paths.items()
    }

    def tail(name: str) -> list[str]:
        return by_path[real(paths[name])][0].split("  ")[-3:]

    assert tail("fetched")[:2] == ["remote live", "branches live local-only 0"]
    assert re.fullmatch(
        r"default trunk live equal cached equal tracking live equal fetched "
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z",
        tail("fetched")[2],
    )
    assert tail("never")[:2] == ["remote live", "branches live local-only 1"]
    assert tail("never")[2].endswith(" never fetched")
    assert tail("no_branches")[:2] == ["remote live", "branches live local-only 0"]
    assert tail("detached") == ["remote live", "branches live local-only 0", "no live default"]
    assert tail("refused") == [
        "remote failed unreachable",
        "branches cached local-only 0",
        tail("refused")[2],
    ]
    assert CACHED_DEFAULT.fullmatch(tail("refused")[2])
    assert tail("empty") == ["remote live", "branches live local-only 0", "no live default"]
    assert parsed["detached"] == {"default": None}
    assert re.search(r"cached [0-9]{4}-", text) is None
    assert "s3cr3tpass" not in text and "someuser" not in text
    assert "http://" not in text
    assert "127.0.0.1" not in "\n".join(lines)


def test_no_network_plain_lines_follow_the_cached_grammar(grammar_scenario, account):
    paths = grammar_scenario["paths"]
    recorder = grammar_scenario["recorder"]
    before = len(recorder.discoveries())
    lines, document, text = _both_outputs(account, "--no-network")

    assert len(recorder.discoveries()) == before
    assert document["remote_checks"] == "off"
    by_path = _origin_lines(lines, document)
    for path in paths.values():
        line, group, item = by_path[real(path)]
        assert group["remote"]["check"] == {"status": "off", "reason": "remote_checks_off"}
        _assert_line_matches_document(line, group, item)
        remote, branches, default = line.split("  ")[-3:]
        assert remote == "remote off"
        assert branches.startswith("branches cached local-only ")
        assert default.startswith(("default ", "no cached default"))
        assert " live " not in default
    assert by_path[real(paths["empty"])][0].split("  ")[-1] == "no cached default"
    assert by_path[real(paths["never"])][0].endswith(" never fetched")
    assert re.search(r"cached [0-9]{4}-", text) is None
    assert "s3cr3tpass" not in text and "someuser" not in text


def test_a_failed_group_line_matches_a_document_that_fell_back_to_cached(
    grammar_scenario, account
):
    lines, document, _ = _both_outputs(account)
    group = next(
        g
        for g in document["repositories"]
        if g["remote"] is not None and g["remote"]["check"]["status"] == "failed"
    )
    [item] = group["clones"]
    assert group["remote"]["check"] == {"status": "failed", "reason": "unreachable"}
    assert {b["vs_remote"]["basis"] for b in item["branches"]} == {"cached"}
    assert "remote failed unreachable" in _line_for(lines, item["path"]).split("  ")


def _single_clone(tmp_path, recorders):
    recorder = recorders()
    served = tmp_path / "served"
    served.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    web_clone(recorder, "solo", make_remote(served, "solo.git"), folder / "solo", "solo")
    _configure(tmp_path, [folder])
    return recorder


def test_no_network_makes_no_request_and_the_default_makes_exactly_one(
    tmp_path, account, recorders
):
    recorder = _single_clone(tmp_path, recorders)

    off = _module_run(account, "checkouts", "--no-network", "--json")
    assert (off.returncode, off.stderr) == (0, "")
    assert recorder.requests == []
    document = models.parse_checkouts(off.stdout)
    assert document["remote_checks"] == "off"
    [group] = document["repositories"]
    assert group["remote"]["check"] == {"status": "off", "reason": "remote_checks_off"}
    assert {c["basis"] for item in group["clones"] for c in _every_comparison(item)} == {
        "cached"
    }

    on = _module_run(account, "checkouts", "--json")
    assert (on.returncode, on.stderr) == (0, "")
    assert len(recorder.discoveries()) == 1
    live = models.parse_checkouts(on.stdout)
    assert live["remote_checks"] == "on"
    assert live["repositories"][0]["remote"]["check"] == {"status": "live", "reason": None}


def _every_comparison(item: dict) -> list[dict]:
    found = [branch["vs_remote"] for branch in item["branches"]]
    found.append(item["cached_vs_live"])
    if item["local_default"] is not None:
        found += [item["local_default"]["vs_live"], item["local_default"]["vs_cached"]]
    return [comparison for comparison in found if comparison is not None]


def test_help_lists_no_network_and_no_longer_claims_cached_only_output():
    completed = subprocess.run(
        [sys.executable, "-m", "nyx.cli", "checkouts", "--help"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0
    flattened = " ".join(completed.stdout.split())
    assert "--no-network" in flattened
    assert "skip every remote check" in flattened
    assert "prints the cached facts" not in flattened


def test_no_network_without_checkouts_is_a_usage_error(account):
    for arguments in (["--no-network"], ["--no-network", "--json"]):
        completed = _module_run(account, *arguments)
        assert completed.returncode == 2
        assert completed.stdout == ""
        assert completed.stderr.startswith("usage: nyx ")
    assert not (account / ".nyx").exists()


@pytest.mark.parametrize("flags", [(), ("--no-network",)])
def test_exit_codes_do_not_depend_on_the_switch(tmp_path, account, monkeypatch, capsys, flags):
    code, out, err = _run_checkouts(capsys, *flags)
    assert (code, err) == (0, "") and NO_FOLDERS in out

    folder = tmp_path / "folder"
    folder.mkdir()
    record = _configure(tmp_path, [folder])
    empty = tmp_path / "no-git"
    empty.mkdir()
    with monkeypatch.context() as scoped:
        scoped.setenv("PATH", str(empty))
        code, out, err = _run_checkouts(capsys, "--json", *flags)
    assert (code, err) == (0, "")
    assert models.parse_checkouts(out)["git"]["status"] == "missing"

    record.write_bytes(b"{ not json")
    assert _run_checkouts(capsys, "--json", *flags) == (1, "", UNAVAILABLE)
