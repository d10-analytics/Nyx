"""CI-only proof that the disposable account runs the installed command."""

from __future__ import annotations

import http.client
import json
import os
import pwd
import shutil
import stat
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


STAGES = (
    "Under_Development",
    "Queue",
    "In_Progress",
    "Needs_Fixes",
    "Awaiting_Retrospective",
    "Done",
    "Archive",
)
STAGE_LABELS = {
    "Under_Development": "Under Development",
    "Queue": "Queue",
    "In_Progress": "In Progress",
    "Needs_Fixes": "Needs Fixes",
    "Awaiting_Retrospective": "Awaiting Retrospective",
    "Done": "Done",
    "Archive": "Archive",
}
PACKAGE_IDS = {
    stage: f"123e4567-e89b-42d3-a456-4266141740{index:02d}"
    for index, stage in enumerate(STAGES, start=10)
}


def _write_fictional_stages(specification_root: Path) -> None:
    for stage in STAGES:
        package_path = specification_root / "Fictional" / stage / stage.lower()
        package_path.mkdir(parents=True)
        lines = [f"# {STAGE_LABELS[stage]} package", f"Package ID: {PACKAGE_IDS[stage]}"]
        if stage == "Queue":
            lines.append(f"Prerequisite: {PACKAGE_IDS['Done']} | release")
        if stage == "Done":
            lines.append(f"Claim: release | satisfied | sha256:{'a' * 64}")
        package_path.joinpath("spec.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fetch_catalog(url: str) -> dict[str, object]:
    connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=5)
    connection.request("GET", "/api/catalog", headers={"Host": "127.0.0.1:8765"})
    response = connection.getresponse()
    body = response.read()
    connection.close()
    assert response.status == 200
    assert url == "http://127.0.0.1:8765/"
    return json.loads(body)


def _assert_catalog(value: dict[str, object], hidden_stages: list[str]) -> None:
    entries = value["entries"]
    assert value["schema_version"] == 3
    assert value["visibility"]["hidden_stages"] == hidden_stages
    assert value["visibility"]["visible_entry_count"] == 7 - len(hidden_stages)
    assert value["visibility"]["hidden_entry_count"] == len(hidden_stages)
    assert [entry["package_path"] for entry in entries] == sorted(
        f"Fictional/{stage}/{stage.lower()}" for stage in STAGES
    )
    assert {entry["stage"]: entry["board_visible"] for entry in entries} == {
        stage: stage not in hidden_stages for stage in STAGES
    }
    queue = next(entry for entry in entries if entry["stage"] == "Queue")
    assert queue["relationship"]["direct_prerequisite_state"] == "satisfied"
    prerequisite = queue["relationship"]["prerequisites"][0]
    assert prerequisite == {
        "claim_name": "release",
        "observed_evidence_ref": "sha256:" + "a" * 64,
        "observed_state": "satisfied",
        "reason": "claim_satisfied",
        "resolved_state": "satisfied",
        "target_package_id": PACKAGE_IDS["Done"],
    }
    done = next(entry for entry in entries if entry["stage"] == "Done")
    assert done["declared"]["title"] == "Done package"


def _assert_hidden_browser(url: str) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url)
            page.locator("#board .card").first.wait_for()
            assert page.locator("#board .card").count() == 6
            assert page.locator(".row-head").all_text_contents() == [
                STAGE_LABELS[stage] for stage in STAGES if stage != "Done"
            ]
            assert page.locator('.board-row[data-lifecycle="done"]').count() == 0
            assert page.locator('.card[data-package-path="Fictional/Done/done"]').count() == 0
            page.fill("#filter", "Done package")
            assert page.locator("#board .card:visible").count() == 0
            page.fill("#filter", "")

            queue = page.locator('.card[data-package-path="Fictional/Queue/queue"]')
            queue.click()
            assert queue.locator(".card-links").count() == 0
            assert page.locator('.connection[data-source="%s"]' % PACKAGE_IDS["Done"]).count() == 0
            assert page.locator("#details .prerequisite-target").inner_text() == "Done package"
            assert page.locator("#details .direct-prerequisite-state").inner_text() == (
                "Reported direct prerequisite state: satisfied."
            )
            assert page.locator("#details .reported-state").inner_text() == "Reported state: satisfied"
        finally:
            browser.close()


def _assert_show_all_browser(url: str) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url)
            page.locator("#board .card").first.wait_for()
            assert page.locator("#board .card").count() == 7
            assert page.locator(".row-head").all_text_contents() == [
                STAGE_LABELS[stage] for stage in STAGES
            ]
            assert page.locator(".board-row").evaluate_all(
                "rows => rows.map(row => [row.dataset.lifecycle, row.querySelector('.card-title')?.textContent])"
            ) == [(stage.lower(), f"{STAGE_LABELS[stage]} package") for stage in STAGES]
            assert page.locator('.card[data-package-path="Fictional/Done/done"]').count() == 1
        finally:
            browser.close()


def _account_snapshot() -> dict[str, tuple[str, bytes | str | None, int, int, int]]:
    """Capture account paths and application-managed bytes without following links."""

    snapshot: dict[str, tuple[str, bytes | str | None, int, int, int]] = {}
    for path in sorted(ACCOUNT_HOME.rglob("*")):
        relative = str(path.relative_to(ACCOUNT_HOME))
        details = path.lstat()
        if stat.S_ISREG(details.st_mode):
            kind = "file"
            content: bytes | str | None = path.read_bytes()
        elif stat.S_ISDIR(details.st_mode):
            kind = "directory"
            content = None
        elif stat.S_ISLNK(details.st_mode):
            kind = "symlink"
            content = os.readlink(path)
        else:
            kind = "other"
            content = None
        snapshot[relative] = (
            kind,
            content,
            stat.S_IMODE(details.st_mode),
            details.st_uid,
            details.st_gid,
        )
    return snapshot


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
    _write_fictional_stages(specification_root)
    started = False
    try:
        configured = _run_nyx("--setup", str(specification_root), "--hide-stage", "Done")
        assert configured.returncode == 0, configured.stderr
        assert configured.stdout.strip() == f"configured {specification_root.resolve()}"
        config_file = ACCOUNT_HOME / ".config" / "nyx" / "config.json"
        assert config_file.is_relative_to(ACCOUNT_HOME)
        assert config_file.exists()
        configuration_bytes = config_file.read_bytes()

        before_configured_stopped = _account_snapshot()
        configured_stopped = _run_nyx("--status")
        assert configured_stopped.returncode == 0, configured_stopped.stderr
        assert configured_stopped.stdout.splitlines() == [
            "Configuration: configured",
            f'Specification root: {json.dumps(str(specification_root.resolve()))}',
            'Hidden stages: ["Done"]',
            "Runtime: not running",
        ]
        assert configured_stopped.stderr == ""
        assert config_file.read_bytes() == configuration_bytes
        assert _account_snapshot() == before_configured_stopped

        first = _run_nyx()
        assert first.returncode == 0, first.stderr
        assert first.stdout.strip() == "http://127.0.0.1:8765/"
        started = True
        instance_file = ACCOUNT_HOME / ".local" / "state" / "nyx" / "runtime" / "instance.json"
        first_instance = json.loads(instance_file.read_text(encoding="utf-8"))
        assert instance_file.is_relative_to(ACCOUNT_HOME)
        instance_bytes = instance_file.read_bytes()

        before_running_status = _account_snapshot()
        running_status = _run_nyx("--status")
        assert running_status.returncode == 0, running_status.stderr
        assert running_status.stdout.splitlines() == [
            "Configuration: configured",
            f'Specification root: {json.dumps(str(specification_root.resolve()))}',
            'Hidden stages: ["Done"]',
            "Runtime: running",
            'URL: "http://127.0.0.1:8765/"',
        ]
        assert running_status.stderr == ""
        assert config_file.read_bytes() == configuration_bytes
        assert instance_file.read_bytes() == instance_bytes
        assert _account_snapshot() == before_running_status

        connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=5)
        connection.request("GET", "/api/catalog", headers={"Host": "127.0.0.1:8765"})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        assert response.status == 200
        payload = json.loads(body)
        _assert_catalog(payload, ["Done"])
        _assert_hidden_browser(first.stdout.strip())

        changed_while_running = _run_nyx(
            "--setup", str(specification_root), "--show-all-stages"
        )
        assert changed_while_running.returncode == 1
        assert changed_while_running.stdout == ""
        assert "active" in changed_while_running.stderr.lower()
        assert config_file.read_bytes() == configuration_bytes
        assert instance_file.read_bytes() == instance_bytes

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

        before_post_stop = _account_snapshot()
        post_stop = _run_nyx("--status")
        assert post_stop.returncode == 0, post_stop.stderr
        assert post_stop.stdout.splitlines() == [
            "Configuration: configured",
            f'Specification root: {json.dumps(str(specification_root.resolve()))}',
            'Hidden stages: ["Done"]',
            "Runtime: not running",
        ]
        assert post_stop.stderr == ""
        assert config_file.read_bytes() == configuration_bytes
        assert _account_snapshot() == before_post_stop

        show_all = _run_nyx("--setup", str(specification_root), "--show-all-stages")
        assert show_all.returncode == 0, show_all.stderr
        assert show_all.stdout.strip() == f"configured {specification_root.resolve()}"
        show_all_config_bytes = config_file.read_bytes()
        before_show_all_stopped = _account_snapshot()
        show_all_stopped = _run_nyx("--status")
        assert show_all_stopped.returncode == 0, show_all_stopped.stderr
        assert show_all_stopped.stdout.splitlines() == [
            "Configuration: configured",
            f'Specification root: {json.dumps(str(specification_root.resolve()))}',
            "Hidden stages: []",
            "Runtime: not running",
        ]
        assert show_all_stopped.stderr == ""
        assert config_file.read_bytes() == show_all_config_bytes
        assert _account_snapshot() == before_show_all_stopped

        second = _run_nyx()
        assert second.returncode == 0, second.stderr
        assert second.stdout.strip() == "http://127.0.0.1:8765/"
        started = True
        second_instance_bytes = instance_file.read_bytes()
        before_second_running_status = _account_snapshot()
        second_running_status = _run_nyx("--status")
        assert second_running_status.returncode == 0, second_running_status.stderr
        assert second_running_status.stdout.splitlines() == [
            "Configuration: configured",
            f'Specification root: {json.dumps(str(specification_root.resolve()))}',
            "Hidden stages: []",
            "Runtime: running",
            'URL: "http://127.0.0.1:8765/"',
        ]
        assert second_running_status.stderr == ""
        assert config_file.read_bytes() == show_all_config_bytes
        assert instance_file.read_bytes() == second_instance_bytes
        assert _account_snapshot() == before_second_running_status
        second_payload = _fetch_catalog(second.stdout.strip())
        _assert_catalog(second_payload, [])
        _assert_show_all_browser(second.stdout.strip())

        final_stop = _run_nyx("--stop")
        assert final_stop.returncode == 0, final_stop.stderr
        assert final_stop.stdout.strip() == "stopped"
        started = False
    finally:
        if started:
            _run_nyx("--stop")
