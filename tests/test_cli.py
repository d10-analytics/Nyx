"""Behavioral checks for the user-facing command dispatcher."""

from __future__ import annotations

from unittest.mock import patch

from nyx import cli, runtime


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
