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


def test_installed_wheel_serves_api_and_real_browser_behavior_without_checkout_imports():
    wheel = _wheel_path()
    pytest.importorskip("playwright.sync_api")
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        installed = root / "installed"
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(installed), str(wheel)],
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
                import threading
                from nyx.models import canonical_digest
                from nyx.server import create_server

                entry = {
                    "package_id": "123e4567-e89b-42d3-a456-426614174000",
                    "package_path": "Fictional/Queue/installed-demo",
                    "lifecycle": "queue",
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
                    "schema_version": 2,
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
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", service.server_port)
                    connection.request(
                        "GET", "/api/catalog", headers={"Host": f"127.0.0.1:{service.server_port}"}
                    )
                    response = connection.getresponse()
                    body = response.read()
                    connection.close()
                    assert response.status == 200
                    assert json.loads(body)["entries"][0]["package_path"] == entry["package_path"]

                    from playwright.sync_api import sync_playwright

                    with sync_playwright() as api:
                        browser = api.chromium.launch()
                        page = browser.new_page()
                        try:
                            page.goto(f"http://127.0.0.1:{service.server_port}/")
                            page.locator(".card-title").wait_for(timeout=15000)
                            assert page.locator(".column-head").all_inner_texts() == ["Fictional"]
                            assert page.locator(".row-head").all_inner_texts()[:2] == [
                                "Under Development",
                                "Queue",
                            ]
                            assert page.locator(".card-title").all_inner_texts() == [
                                "Installed catalog entry"
                            ]
                            assert page.locator(
                                '.card[data-package-path="Fictional/Queue/installed-demo"]'
                            ).count() == 1
                            assert page.locator("#board").inner_text().count(
                                "Installed catalog entry"
                            ) == 1
                        finally:
                            page.close()
                            browser.close()
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
        environment["PYTHONPATH"] = str(installed)
        completed = subprocess.run(
            [sys.executable, str(probe)],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        installed_module = Path(completed.stdout.strip())
        assert installed_module.is_relative_to(installed)
        assert not installed_module.is_relative_to(REPOSITORY_ROOT)
