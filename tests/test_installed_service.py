"""CI-only proof that the disposable account runs the installed command."""

from __future__ import annotations

import http.client
import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path

import pytest

if Path(__file__).resolve().parent != Path("/opt/nyx-verify/exercise"):
    pytest.skip(
        "installed service proof runs only from the disposable CI exercise directory",
        allow_module_level=True,
    )


ACCOUNT_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
VERIFY_ROOT = Path("/opt/nyx-verify")
SYMLINK = ACCOUNT_HOME / ".local" / "bin" / "nyx"
VENV_COMMAND = VERIFY_ROOT / "venv" / "bin" / "nyx"
EXERCISE = VERIFY_ROOT / "exercise"


def _run_nyx(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    return subprocess.run(
        ["nyx", *arguments],
        cwd=EXERCISE,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _account_snapshot() -> tuple[tuple[str, str, bytes | str | None], ...]:
    """Capture account paths and application-managed bytes without following links."""

    snapshot: list[tuple[str, str, bytes | str | None]] = []
    for path in sorted(ACCOUNT_HOME.rglob("*")):
        relative = str(path.relative_to(ACCOUNT_HOME))
        if path.is_symlink():
            snapshot.append((relative, "symlink", os.readlink(path)))
        elif path.is_dir():
            snapshot.append((relative, "directory", None))
        else:
            snapshot.append((relative, "file", path.read_bytes()))
    return tuple(snapshot)


def test_bare_installed_command_owns_setup_start_reuse_and_stop():
    assert os.environ.get("HOME") == str(ACCOUNT_HOME)
    command_path = subprocess.check_output(
        ["bash", "-c", "command -v nyx"], cwd=EXERCISE, text=True
    ).strip()
    assert command_path == str(SYMLINK)
    assert shutil.which("nyx") == command_path
    assert (
        subprocess.check_output(["readlink", str(SYMLINK)], text=True).strip()
        == str(VENV_COMMAND)
    )
    assert SYMLINK.readlink() == VENV_COMMAND

    before_status = _account_snapshot()
    unconfigured = _run_nyx("--status")
    assert unconfigured.returncode == 0, unconfigured.stderr
    assert unconfigured.stdout.splitlines() == [
        "Configuration: not configured",
        "Specification root: not configured",
        "Hidden stages: not configured",
        "Runtime: not running",
    ]
    assert unconfigured.stderr == ""
    assert _account_snapshot() == before_status

    specification_root = ACCOUNT_HOME / "fictional-specifications"
    anchor = specification_root / "Fictional" / "Queue" / "sample" / "spec.md"
    anchor.parent.mkdir(parents=True)
    anchor.write_text(
        "# Fictional sample\nPackage ID: 123e4567-e89b-42d3-a456-426614174000\n",
        encoding="utf-8",
    )
    started = False
    try:
        configured = _run_nyx("--setup", str(specification_root))
        assert configured.returncode == 0, configured.stderr
        assert configured.stdout.strip() == f"configured {specification_root.resolve()}"
        config_file = ACCOUNT_HOME / ".config" / "nyx" / "config.json"
        assert config_file.is_relative_to(ACCOUNT_HOME)
        assert config_file.exists()
        configuration_bytes = config_file.read_bytes()

        configured_stopped = _run_nyx("--status")
        assert configured_stopped.returncode == 0, configured_stopped.stderr
        assert configured_stopped.stdout.splitlines() == [
            "Configuration: configured",
            f'Specification root: {json.dumps(str(specification_root.resolve()))}',
            "Hidden stages: []",
            "Runtime: not running",
        ]
        assert configured_stopped.stderr == ""
        assert config_file.read_bytes() == configuration_bytes

        first = _run_nyx()
        assert first.returncode == 0, first.stderr
        assert first.stdout.strip() == "http://127.0.0.1:8765/"
        started = True
        instance_file = ACCOUNT_HOME / ".local" / "state" / "nyx" / "runtime" / "instance.json"
        first_instance = json.loads(instance_file.read_text(encoding="utf-8"))
        assert instance_file.is_relative_to(ACCOUNT_HOME)
        instance_bytes = instance_file.read_bytes()

        running_status = _run_nyx("--status")
        assert running_status.returncode == 0, running_status.stderr
        assert running_status.stdout.splitlines() == [
            "Configuration: configured",
            f'Specification root: {json.dumps(str(specification_root.resolve()))}',
            "Hidden stages: []",
            "Runtime: running",
            'URL: "http://127.0.0.1:8765/"',
        ]
        assert running_status.stderr == ""
        assert config_file.read_bytes() == configuration_bytes
        assert instance_file.read_bytes() == instance_bytes

        connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=5)
        connection.request("GET", "/api/catalog", headers={"Host": "127.0.0.1:8765"})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        assert response.status == 200
        payload = json.loads(body)
        assert payload["schema_version"] == 3
        assert payload["entries"][0]["package_path"] == "Fictional/Queue/sample"

        reused = _run_nyx()
        assert reused.returncode == 0, reused.stderr
        assert reused.stdout.strip() == "http://127.0.0.1:8765/"
        second_instance = json.loads(instance_file.read_text(encoding="utf-8"))
        assert second_instance["instance_id"] == first_instance["instance_id"]

        stopped = _run_nyx("--stop")
        assert stopped.returncode == 0, stopped.stderr
        assert stopped.stdout.strip() == "stopped"
        started = False
        assert not instance_file.exists()

        post_stop = _run_nyx("--status")
        assert post_stop.returncode == 0, post_stop.stderr
        assert post_stop.stdout.splitlines() == [
            "Configuration: configured",
            f'Specification root: {json.dumps(str(specification_root.resolve()))}',
            "Hidden stages: []",
            "Runtime: not running",
        ]
        assert post_stop.stderr == ""
        assert config_file.read_bytes() == configuration_bytes
    finally:
        if started:
            _run_nyx("--stop")
