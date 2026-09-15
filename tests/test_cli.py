"""Behavioral checks for the user-facing command dispatcher."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from nyx import cli, runtime, state


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
            state, "_current_uid", return_value=os.getuid()
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
