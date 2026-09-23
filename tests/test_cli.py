"""Behavioral checks for the user-facing command dispatcher."""

from __future__ import annotations

import os
import subprocess
import sys
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


def test_setup_replaces_hidden_stage_policy_from_repeated_options(capsys):
    class Configuration:
        specification_root = "/private/spec-root"

    with patch.object(runtime, "setup", return_value=Configuration()) as setup:
        assert cli.main(["--setup", "/input/link", "--hide-stage", "Queue", "--hide-stage", "Done"]) == 0

    setup.assert_called_once_with("/input/link", hidden_stages=["Queue", "Done"])
    assert "configured /private/spec-root" in capsys.readouterr().out


def test_setup_can_explicitly_clear_hidden_stage_policy(capsys):
    class Configuration:
        specification_root = "/private/spec-root"

    with patch.object(runtime, "setup", return_value=Configuration()) as setup:
        assert cli.main(["--setup", "/input/link", "--show-all-stages"]) == 0

    setup.assert_called_once_with("/input/link", hidden_stages=())
    assert "configured /private/spec-root" in capsys.readouterr().out


def test_hidden_stage_options_are_setup_only():
    with pytest.raises(SystemExit):
        cli.main(["--hide-stage", "Queue"])


def test_help_names_the_configured_location_workspace_and_preserves_spec_root(
    capsys,
):
    with pytest.raises(SystemExit) as error:
        cli.main(["--help"])

    assert error.value.code == 0
    captured = capsys.readouterr()
    assert "--setup SPEC_ROOT" in captured.out
    assert "configure the Workspace at SPEC_ROOT" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("requested", ["root", "policy"])
def test_active_setup_rejects_changed_root_or_policy_without_cli_mutation(capsys, requested):
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
            state.setup(first, ["Queue"])
            paths = state.state_paths()
        lease = runtime._lease_lock(paths, timeout=0.0)
        assert lease.acquire(blocking=False)
        before = paths.config_file.read_bytes()
        try:
            arguments = ["--setup", str(second)] if requested == "root" else [
                "--setup", str(first), "--hide-stage", "Other"
            ]
            with patch.object(runtime, "_paths", return_value=paths):
                assert cli.main(arguments) == 1
        finally:
            lease.close()
        assert paths.config_file.read_bytes() == before
        assert "stop Nyx" in capsys.readouterr().err


def test_status_renders_configured_stopped_snapshot_with_ascii_json_and_no_lifecycle(
    capsys,
):
    configuration = state.ConfigurationObservation(
        "configured",
        specification_root=Path("/private/spec\n-root\x1b[31m\u0085"),
        hidden_stages=("Done", "Queue\n\u0085"),
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
        'Hidden stages: ["Done", "Queue\\n\\u0085"]',
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
        "Hidden stages: not configured",
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
        "Hidden stages: unavailable",
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
            "Hidden stages: unavailable",
            "Runtime: not running",
            "Diagnostic: configuration unavailable",
        ]
        assert captured.err == ""
        assert paths.config_file.read_bytes() == before


def test_status_keeps_configuration_result_when_runtime_is_unknown_and_bounds_diagnostic(
    capsys,
):
    configuration = state.ConfigurationObservation(
        "configured", specification_root=Path("/private/spec"), hidden_stages=()
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
        "Hidden stages: []",
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
        (["--status", "--hide-stage", "Queue"], "--hide-stage and --show-all-stages require --setup"),
        (["--status", "--show-all-stages"], "--hide-stage and --show-all-stages require --setup"),
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
        "Hidden stages: unavailable",
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
        ["--setup", "/tmp/spec", "--hide-stage", "Queue"],
        ["--setup", "/tmp/spec", "--show-all-stages"],
        ["--hide-stage", "Queue"],
        ["--show-all-stages"],
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
        assert cli.main(["--setup", str(specification), "--show-all-stages"]) == 2
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
