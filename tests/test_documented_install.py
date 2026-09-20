"""Exercise the published installation commands in their native shell."""

from __future__ import annotations

import http.client
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

import pytest
from test_wheel_install import (
    _installed_environment,
    _native_claim_probe,
    _venv_executable,
)

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
        r".\.venv\Scripts\python.exe -m pip install .",
        r".\.venv\Scripts\nyx.exe --setup .\examples\sample-specifications --show-all-stages",
        r".\.venv\Scripts\nyx.exe",
        r".\.venv\Scripts\nyx.exe --status",
        r".\.venv\Scripts\nyx.exe --stop",
    ),
}


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


@pytest.mark.parametrize("language", EXPECTED)
@pytest.mark.parametrize("index", range(6))
def test_document_contract_rejects_changed_or_deleted_commands(language, index):
    commands = list(EXPECTED[language])
    for replacement in ("", "echo substituted command"):
        changed = commands.copy()
        changed[index] = replacement
        document = f"```{language}\n" + "\n".join(changed) + "\n```\n"
        with pytest.raises(AssertionError, match="changed"):
            _documented_commands(document, language)


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


@pytest.mark.skipif(
    os.environ.get("NYX_VERIFY_DOCUMENTED_INSTALL") != "1",
    reason="documented installation runs in its dedicated native hosted lane",
)
def test_documented_native_installation_and_lifecycle(tmp_path):
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
            f"Specification root: {json.dumps(str(sample))}",
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
