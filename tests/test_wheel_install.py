"""Verify behavior from one built wheel when the source checkout is unavailable."""

from __future__ import annotations

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


def _wheel_path() -> Path:
    wheels = sorted((REPOSITORY_ROOT / "dist").glob("*.whl"))
    if not wheels:
        pytest.skip("the installed-wheel lane runs after the wheel-build step")
    assert len(wheels) == 1
    return wheels[0]


def test_wheel_contains_every_module_and_frontend_asset():
    wheel = _wheel_path()
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert EXPECTED_MODULES <= names
    assert EXPECTED_ASSETS <= names


def test_installed_wheel_serves_static_and_api_behavior_without_checkout_imports():
    wheel = _wheel_path()
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        venv = root / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        interpreter = venv / "bin" / "python"
        subprocess.run(
            [str(interpreter), "-m", "pip", "install", "--no-deps", str(wheel)],
            check=True,
            capture_output=True,
            text=True,
        )
        probe = root / "probe.py"
        probe.write_text(
            textwrap.dedent(
                """
                import http.client
                import json
                from nyx.models import canonical_digest
                from nyx.server import create_server

                catalog = {
                    "schema_version": 2,
                    "identity_coverage": {"state": "complete", "diagnostics": []},
                    "program_coverage": {"state": "complete", "diagnostics": []},
                    "discovery_diagnostics": [],
                    "entries": [],
                    "programs": [],
                }
                catalog["catalog_digest"] = canonical_digest(catalog)
                service = create_server(lambda: catalog, port=0)
                import threading
                thread = threading.Thread(target=service.serve_forever)
                thread.start()
                try:
                    for path, content_type in (
                        ("/", "text/html; charset=utf-8"),
                        ("/static/app.js", "text/javascript; charset=utf-8"),
                        ("/static/theme.js", "text/javascript; charset=utf-8"),
                        ("/static/style.css", "text/css; charset=utf-8"),
                    ):
                        connection = http.client.HTTPConnection("127.0.0.1", service.server_port)
                        connection.request(
                            "GET", path, headers={"Host": f"127.0.0.1:{service.server_port}"}
                        )
                        response = connection.getresponse()
                        body = response.read()
                        connection.close()
                        assert response.status == 200
                        assert response.getheader("Content-Type") == content_type
                        assert body

                    connection = http.client.HTTPConnection("127.0.0.1", service.server_port)
                    connection.request(
                        "GET", "/api/catalog", headers={"Host": f"127.0.0.1:{service.server_port}"}
                    )
                    response = connection.getresponse()
                    body = response.read()
                    connection.close()
                    assert response.status == 200
                    assert json.loads(body)["entries"] == []
                    import nyx
                    print(nyx.__file__)
                finally:
                    service.shutdown()
                    thread.join(timeout=5)
                    service.server_close()
                """
            ),
            encoding="utf-8",
        )
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "PYTHONHOME"}
        }
        completed = subprocess.run(
            [str(interpreter), str(probe)],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        installed_module = Path(completed.stdout.strip())
        assert installed_module.is_relative_to(venv)
        assert not installed_module.is_relative_to(REPOSITORY_ROOT)
