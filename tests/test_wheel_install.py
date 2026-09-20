"""Verify behavior from one built wheel when the source checkout is unavailable."""

from __future__ import annotations

import http.client
import io
import json
import os
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

REPOSITORY_ROOT = Path(__file__).parents[1].resolve()
EXPECTED_MODULES = {
    "nyx/__init__.py",
    "nyx/catalog.py",
    "nyx/cli.py",
    "nyx/models.py",
    "nyx/_native_claim.py",
    "nyx/runtime.py",
    "nyx/server.py",
    "nyx/state.py",
    "nyx/worker.py",
}
EXPECTED_ASSETS = {
    "nyx/static/index.html",
    "nyx/static/app.js",
    "nyx/static/style.css",
    "nyx/static/theme.js",
}


def _prohibited_sequences() -> tuple[bytes, ...]:
    return (
        b"".join((b"v", bytes((50,)))),
        b" ".join((b"survives", b"migration")),
        b"_".join((b"viewer", b"migration")),
    )


def _assert_packaged_wording_clean(archive: zipfile.ZipFile) -> None:
    for name in archive.namelist():
        if not (
            name.startswith("nyx/")
            and (name.endswith(".py") or name.startswith("nyx/static/"))
        ):
            continue
        payload = archive.read(name).lower()
        for sequence in _prohibited_sequences():
            assert sequence not in payload, f"{name} contains forbidden bytes {sequence!r}"


def _wheel_path() -> Path:
    wheels = sorted((REPOSITORY_ROOT / "dist").glob("*.whl"))
    if not wheels:
        pytest.skip("the installed-wheel lane runs after the wheel-build step")
    assert len(wheels) == 1
    return wheels[0]


def _venv_executable(venv: Path, name: str) -> Path:
    directory = venv / ("Scripts" if os.name == "nt" else "bin")
    candidates = [directory / name]
    if os.name == "nt" and not name.endswith(".exe"):
        candidates.insert(0, directory / f"{name}.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise AssertionError(f"virtual environment executable is missing: {candidates}")


def _installed_environment(home: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    return environment


def _run_installed_console(
    console: Path, arguments: list[str], *, root: Path, home: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(console), *arguments],
        cwd=root,
        env=_installed_environment(home),
        check=False,
        capture_output=True,
        text=True,
    )


def _native_claim_probe(interpreter: Path, claim: Path, *, root: Path, home: Path) -> str:
    probe = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from nyx._native_claim import NativeClaim

        claim = NativeClaim(Path(sys.argv[1]))
        print("busy" if not claim.acquire(create=False, blocking=False) else "free")
        claim.close()
        """
    )
    result = subprocess.run(
        [str(interpreter), "-c", probe, str(claim)],
        cwd=root,
        env=_installed_environment(home),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_wheel_contains_every_module_and_frontend_asset():
    wheel = _wheel_path()
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert EXPECTED_MODULES <= names
    assert EXPECTED_ASSETS <= names


@pytest.mark.parametrize(
    "sequence", _prohibited_sequences(), ids=lambda sequence: sequence.decode("ascii")
)
def test_wheel_audit_rejects_each_prohibited_sequence(sequence: bytes):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("nyx/models.py", b"# " + sequence.upper())
    stream.seek(0)
    with zipfile.ZipFile(stream) as archive:
        with pytest.raises(AssertionError, match="forbidden bytes"):
            _assert_packaged_wording_clean(archive)


def test_built_wheel_python_and_static_members_have_current_terms():
    wheel = _wheel_path()
    with zipfile.ZipFile(wheel) as archive:
        _assert_packaged_wording_clean(archive)


def test_installed_wheel_serves_api_and_real_browser_behavior_without_checkout_imports():
    wheel = _wheel_path()
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        venv = root / "venv"
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            check=True,
        )
        interpreter = _venv_executable(venv, "python")
        subprocess.run(
            [str(interpreter), "-m", "pip", "install", "--no-deps", str(wheel)],
            check=True,
            capture_output=True,
            text=True,
        )
        console = _venv_executable(venv, "nyx")
        home = root / "home"
        home.mkdir()
        specification_root = root / "specifications"
        package = specification_root / "Fictional" / "Queue" / "installed-demo"
        package.mkdir(parents=True)
        package.joinpath("spec.md").write_text(
            "# Installed catalog entry\nStatus: approved\nClosure: approved\n",
            encoding="utf-8",
        )
        provenance = subprocess.run(
            [str(interpreter), "-c", "import nyx; print(nyx.__file__)"],
            cwd=root,
            env=_installed_environment(home),
            check=False,
            capture_output=True,
            text=True,
        )
        assert provenance.returncode == 0, provenance.stderr
        installed_module = Path(provenance.stdout.strip()).resolve()
        assert installed_module.is_relative_to(venv.resolve())
        assert not installed_module.is_relative_to(REPOSITORY_ROOT)

        setup = _run_installed_console(
            console, ["--setup", str(specification_root)], root=root, home=home
        )
        assert setup.returncode == 0, setup.stderr
        assert setup.stdout.strip() == f"configured {specification_root.resolve()}"
        instance_file = home / ".nyx" / "runtime" / "instance.json"
        claim_file = home / ".nyx" / "runtime" / "lease.lock"
        launcher_code = textwrap.dedent(
            """
            import json
            import subprocess
            import sys

            result = subprocess.run([sys.argv[1]], capture_output=True, text=True, check=False)
            print(
                json.dumps({
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }),
                flush=True,
            )
            sys.stdin.buffer.read()
            """
        )
        launcher_kwargs: dict[str, object] = {
            "cwd": root,
            "env": _installed_environment(home),
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
        }
        if os.name == "nt":
            launcher_kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        else:
            launcher_kwargs["start_new_session"] = True
        launcher = subprocess.Popen(
            [str(interpreter), "-c", launcher_code, str(console)], **launcher_kwargs
        )
        try:
            assert launcher.stdout is not None
            line = launcher.stdout.readline()
            assert line, launcher.stderr.read() if launcher.stderr is not None else ""
            launcher_result = json.loads(line)
            assert launcher_result == {
                "returncode": 0,
                "stdout": "http://127.0.0.1:8765/\n",
                "stderr": "",
            }
        finally:
            launcher.terminate()
            launcher.wait(timeout=10)

        first_instance = json.loads(instance_file.read_text(encoding="utf-8"))
        assert first_instance["url"] == "http://127.0.0.1:8765/"
        running = _run_installed_console(console, ["--status"], root=root, home=home)
        assert running.returncode == 0, running.stderr
        assert running.stdout.splitlines() == [
            "Configuration: configured",
            f'Specification root: {json.dumps(str(specification_root.resolve()))}',
            "Hidden stages: []",
            "Runtime: running",
            'URL: "http://127.0.0.1:8765/"',
        ]
        assert _native_claim_probe(interpreter, claim_file, root=root, home=home) == "busy"
        connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=5)
        connection.request("GET", "/api/catalog", headers={"Host": "127.0.0.1:8765"})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        assert response.status == 200
        payload = json.loads(body)
        assert [entry["package_path"] for entry in payload["entries"]] == [
            "Fictional/Queue/installed-demo"
        ]

        reused = _run_installed_console(console, [], root=root, home=home)
        assert reused.returncode == 0, reused.stderr
        assert reused.stdout.strip() == "http://127.0.0.1:8765/"
        assert json.loads(instance_file.read_text(encoding="utf-8"))["instance_id"] == (
            first_instance["instance_id"]
        )
        stopped = _run_installed_console(console, ["--stop"], root=root, home=home)
        assert stopped.returncode == 0, stopped.stderr
        assert stopped.stdout.strip() == "stopped"
        assert not instance_file.exists()
        assert _native_claim_probe(interpreter, claim_file, root=root, home=home) == "free"
        after_stop = _run_installed_console(console, ["--status"], root=root, home=home)
        assert after_stop.returncode == 0, after_stop.stderr
        assert after_stop.stdout.splitlines() == [
            "Configuration: configured",
            f'Specification root: {json.dumps(str(specification_root.resolve()))}',
            "Hidden stages: []",
            "Runtime: not running",
        ]
        rejection = subprocess.run(
            [
                str(interpreter),
                "-c",
                textwrap.dedent(
                    """
                    import http.client
                    import json
                    import threading
                    from nyx.models import canonical_digest
                    from nyx.server import create_server

                    baseline = {
                        "schema_version": 4,
                        "inventory": {
                            "projects": [{"name": "Fictional", "availability": "complete"}],
                            "stages": [],
                        },
                        "visibility": {
                            "hidden_stages": [],
                            "visible_entry_count": 0,
                            "hidden_entry_count": 0,
                        },
                        "identity_coverage": {"state": "complete", "diagnostics": []},
                        "program_coverage": {"state": "complete", "diagnostics": []},
                        "discovery_diagnostics": [],
                        "entries": [],
                        "programs": [],
                    }
                    malformed = []
                    legacy = json.loads(json.dumps(baseline))
                    legacy["schema_version"] = 3
                    legacy["catalog_digest"] = canonical_digest(legacy)
                    malformed.append(legacy)
                    duplicate = json.loads(json.dumps(baseline))
                    duplicate["inventory"]["projects"].append(
                        dict(duplicate["inventory"]["projects"][0])
                    )
                    duplicate["catalog_digest"] = canonical_digest(duplicate)
                    malformed.append(duplicate)

                    for value in malformed:
                        service = create_server(lambda value=value: value, port=0)
                        thread = threading.Thread(target=service.serve_forever)
                        thread.start()
                        try:
                            connection = http.client.HTTPConnection(
                                "127.0.0.1", service.server_port, timeout=5
                            )
                            connection.request(
                                "GET",
                                "/api/catalog",
                                headers={"Host": f"127.0.0.1:{service.server_port}"},
                            )
                            response = connection.getresponse()
                            body = response.read()
                            connection.close()
                            assert response.status == 502
                            assert json.loads(body) == {"error": "producer_protocol_error"}
                        finally:
                            service.shutdown()
                            thread.join(timeout=5)
                            service.server_close()
                    """
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=root,
            env=_installed_environment(home),
        )
        assert rejection.returncode == 0, rejection.stderr
        probe = root / "probe.py"
        probe.write_text(
            textwrap.dedent(
                """
                import http.client
                import json
                import threading
                import sys
                from nyx.models import canonical_digest
                from nyx.server import create_server

                entry = {
                    "package_id": "123e4567-e89b-42d3-a456-426614174000",
                    "package_path": "Fictional/Queue/installed-demo",
                    "project": "Fictional",
                    "stage": "Queue",
                    "board_visible": True,
                    "state": "complete",
                    "declared": {
                        "title": "Installed catalog entry",
                        "target_project": "Fictional",
                        "status": "ready",
                        "closure": "approved",
                        "sanity_recommendation": "PROCEED_TO_DESIGN",
                        "human_sanity_decision": "AFFIRMED",
                    },
                    "diagnostics": [],
                    "relationship": {
                        "participation": "available",
                        "claims": [],
                        "prerequisites": [],
                        "direct_prerequisite_state": "no_declared_prerequisites",
                        "program": {
                            "program_id": None,
                            "title": None,
                            "resolution": "not_declared",
                            "diagnostics": [],
                        },
                        "superseded_by": {
                            "package_id": None,
                            "resolution": "not_declared",
                            "diagnostics": [],
                        },
                    },
                    "transitive_diagnostics": [],
                }
                catalog = {
                    "schema_version": 4,
                    "inventory": {
                        "projects": [{"name": "Fictional", "availability": "complete"}],
                        "stages": [
                            {"project": "Fictional", "stage": "Queue", "availability": "complete"},
                            {
                                "project": "Fictional",
                                "stage": "Under_Development",
                                "availability": "complete",
                            },
                        ],
                    },
                    "visibility": {"hidden_stages": ["Archive", "Done", "In_Progress"],
                                    "visible_entry_count": 1, "hidden_entry_count": 0},
                    "identity_coverage": {"state": "complete", "diagnostics": []},
                    "program_coverage": {"state": "complete", "diagnostics": []},
                    "discovery_diagnostics": [],
                    "entries": [entry],
                    "programs": [],
                }
                catalog["catalog_digest"] = canonical_digest(catalog)
                service = create_server(lambda: catalog, port=0)
                thread = threading.Thread(target=service.serve_forever)
                thread.start()
                print(json.dumps({"module": __import__("nyx").__file__, "port": service.server_port}), flush=True)
                try:
                    sys.stdin.readline()
                finally:
                    service.shutdown()
                    thread.join(timeout=5)
                    service.server_close()
                """
            ),
            encoding="utf-8",
        )
        environment = _installed_environment(home)
        process = subprocess.Popen(
            [str(interpreter), str(probe)],
            cwd=root,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout is not None
            line = process.stdout.readline()
            assert line, process.stderr.read() if process.stderr is not None else ""
            details = json.loads(line)
            installed_module = Path(details["module"])
            port = int(details["port"])
            assert installed_module.is_relative_to(venv)
            assert not installed_module.is_relative_to(REPOSITORY_ROOT)

            connection = http.client.HTTPConnection("127.0.0.1", port)
            connection.request("GET", "/api/catalog", headers={"Host": f"127.0.0.1:{port}"})
            response = connection.getresponse()
            body = response.read()
            connection.close()
            assert response.status == 200
            assert json.loads(body)["entries"][0]["package_path"] == "Fictional/Queue/installed-demo"

            pytest.importorskip("playwright.sync_api")
            from playwright.sync_api import sync_playwright

            with sync_playwright() as api:
                browser = api.chromium.launch()
                page = browser.new_page()
                try:
                    page.goto(f"http://127.0.0.1:{port}/")
                    page.locator(".card-title").wait_for(timeout=15000)
                    assert page.locator(".column-head").all_inner_texts() == ["Fictional"]
                    compact = page.get_by_label("Hide empty rows and columns", exact=True)
                    assert compact.is_checked()
                    assert page.locator(".row-head").all_inner_texts() == ["Queue"]
                    assert page.locator(".card-title").all_inner_texts() == [
                        "Installed catalog entry"
                    ]
                    assert page.locator(
                        '.card[data-package-path="Fictional/Queue/installed-demo"]'
                    ).count() == 1
                    assert page.locator("#board").inner_text().count(
                        "Installed catalog entry"
                    ) == 1
                    compact.uncheck()
                    assert page.locator(".row-head").all_inner_texts() == [
                        "Queue",
                        "Under Development",
                    ]
                    assert page.locator(
                        '.board-row[data-lifecycle="Under_Development"] .card'
                    ).count() == 0
                finally:
                    page.close()
                    browser.close()
        finally:
            if process.poll() is None:
                assert process.stdin is not None
                process.stdin.write("\n")
                process.stdin.close()
            process.wait(timeout=10)
