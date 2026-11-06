"""Behavioral proofs for the checkouts worker kind against real repositories.

Every document comes from a real worker process over a real repository under an
isolated home, and every budget or deadline is lowered only through a manager
constructor value or a collector parameter.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_checkouts import clone, git, isolate_git, make_remote, posix_only, real

from nyx import models, state, worker

REPOSITORY_ROOT = Path(__file__).parents[1].resolve()
# How long the fake ssh blocks.  It is longer than any budget used below and
# shorter than a wait a person would notice, so a proof that ends early never
# leaves the process behind.
SLOW_SSH_SECONDS = 5
# The lowered networked budget of the deadline proofs.
NETWORKED_BUDGET_SECONDS = 3.0


def _write_record(
    home: Path,
    specification: Path,
    folders: list[Path],
    *,
    github_lookup: bool = True,
) -> state.StatePaths:
    """Write the schema 4 record of a workspace whose settings name ``folders``."""

    key = str(specification.resolve())
    record = {
        "schema_version": 4,
        "specification_root": key,
        "checkout_settings": {
            key: {
                "folders": sorted(str(folder) for folder in folders),
                "github_lookup": github_lookup,
            }
        },
    }
    paths = state.state_paths(create=True)
    # The proof writes into the isolated home, never the caller's account state.
    assert paths.config_file.is_relative_to(home)
    paths.config_file.write_bytes(
        (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    )
    return paths


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A configured home: one folder with two clones of one ``file://`` remote."""

    home = tmp_path / "home"
    home.mkdir()
    isolate_git(monkeypatch, home)
    served = tmp_path / "served"
    served.mkdir()
    remote = make_remote(served)
    folder = tmp_path / "checkouts"
    folder.mkdir()
    source = remote.as_uri()
    first = clone(source, folder / "Clone One")
    second = clone(source, folder / "Clone Two")

    specification = tmp_path / "specification"
    package = specification / "Fictional" / "Drafts" / "sample"
    package.mkdir(parents=True)
    package.joinpath("spec.md").write_text(
        "# Sample package\n"
        "Package ID: 11111111-1111-4111-8111-111111111111\n"
        f"Target repo: {first.resolve()}\n",
        encoding="utf-8",
    )
    paths = _write_record(home, specification, [folder])
    return SimpleNamespace(
        home=home,
        remote=remote,
        folder=folder,
        first=first,
        second=second,
        specification=specification.resolve(),
        paths=paths,
    )


@contextlib.contextmanager
def _managed(manager: worker.CatalogWorkerManager):
    """Close one manager whatever the body does."""

    try:
        yield manager
    finally:
        assert manager.close(time.monotonic() + 30)


def _worker_manager(workspace, **keywords: object) -> worker.CatalogWorkerManager:
    return worker.CatalogWorkerManager(
        working_directory=workspace.paths.state_directory, **keywords
    )


def _clones(document: dict) -> list[str]:
    return sorted(item["path"] for group in document["repositories"] for item in group["clones"])


def test_checkouts_fetch_derives_every_input_of_the_record(workspace):
    manager = _worker_manager(workspace)
    with _managed(manager):
        local = models.parse_checkouts(manager.fetch_checkouts(network=False))
        networked = models.parse_checkouts(manager.fetch_checkouts(network=True))

    assert local["folders"] == [{"path": str(workspace.folder), "status": "ok"}]
    assert local["github_lookup"] == "on"
    assert [package["package_path"] for package in local["packages"]] == [
        "Fictional/Drafts/sample"
    ]
    assert local["package_scan"]["status"] == "complete"
    assert local["remote_checks"] == "off"
    assert local["repositories"][0]["remote"]["check"] == {
        "status": "off",
        "reason": "remote_checks_off",
    }
    assert _clones(local) == sorted([real(workspace.first), real(workspace.second)])

    assert networked["remote_checks"] == "on"
    [repository] = networked["repositories"]
    assert repository["remote"]["check"] == {"status": "live", "reason": None}
    assert len(repository["clones"]) == 2


def test_catalog_fetch_on_the_same_home_is_unchanged(workspace):
    manager = _worker_manager(workspace)
    with _managed(manager):
        payload = manager.fetch_catalog()

    catalog = models.parse_catalog(payload)
    assert catalog.as_dict() == json.loads(payload)
    assert [entry["package_path"] for entry in catalog.as_dict()["entries"]] == [
        "Fictional/Drafts/sample"
    ]


def test_an_unreadable_record_reports_each_kind_its_own_category(workspace):
    workspace.paths.config_file.write_text("{not a record", encoding="utf-8")
    manager = _worker_manager(workspace)
    with _managed(manager):
        with pytest.raises(worker.WorkerError) as checkouts_error:
            manager.fetch_checkouts(network=False)
        with pytest.raises(worker.WorkerError) as catalog_error:
            manager.fetch_catalog()

    assert checkouts_error.value.code == "checkouts_unavailable"
    assert catalog_error.value.code == "producer_unavailable"


def test_a_missing_working_directory_is_unavailable_for_checkouts(workspace, tmp_path):
    missing = tmp_path / "missing-state-directory"
    manager = worker.CatalogWorkerManager(working_directory=missing)
    try:
        with pytest.raises(worker.WorkerError) as error:
            manager.fetch_checkouts(network=False)
        assert error.value.code == "checkouts_unavailable"
        assert manager.active_count == 0
    finally:
        assert manager.close(time.monotonic() + 5)
    assert not missing.exists()


def test_a_closed_manager_reports_the_checkouts_cancellation():
    manager = worker.CatalogWorkerManager(command_factory=_argument_command)
    manager.close_admission()
    try:
        with pytest.raises(worker.WorkerError) as error:
            manager.fetch_checkouts(network=False)
        assert error.value.code == "checkouts_cancelled"
        assert manager.active_count == 0
    finally:
        assert manager.close(time.monotonic() + 5)


def test_a_not_configured_home_still_yields_an_empty_document(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    isolate_git(monkeypatch, home)
    manager = worker.CatalogWorkerManager()
    with _managed(manager):
        document = models.parse_checkouts(manager.fetch_checkouts(network=False))

    assert document["folders"] == []
    assert document["packages"] == []
    assert document["repositories"] == []
    assert document["package_scan"] == {
        "status": "unavailable",
        "finished_policy": "not_configured",
    }


def test_worker_entrypoint_refuses_kind_arguments_before_loading(monkeypatch):
    loaded: list[str] = []

    def refuse(name: str):
        def load(*_args: object, **_kwargs: object) -> object:
            loaded.append(name)
            raise AssertionError(f"{name} must not run for a rejected argument set")

        return load

    monkeypatch.setattr(worker.state, "load_configuration", refuse("load_configuration"))
    monkeypatch.setattr(worker.state, "observe_configuration", refuse("observe_configuration"))

    rejected = [
        ["--network"],
        ["--deadline-ms", "5000"],
        ["--kind", "checkouts"],
        ["--kind", "checkouts", "--deadline-ms", "700000"],
    ]
    for argv in rejected:
        assert worker.worker_entrypoint(argv) == 4, argv
    assert loaded == []


def _argument_command() -> list[str]:
    return [
        sys.executable,
        "-c",
        "import json, sys; print(json.dumps(sys.argv[1:]))",
    ]


def test_checkouts_fetch_names_its_kind_deadline_and_network_mode():
    manager = worker.CatalogWorkerManager(command_factory=_argument_command)
    with _managed(manager):
        local = json.loads(manager.fetch_checkouts(network=False))
        networked = json.loads(manager.fetch_checkouts(network=True))
        catalog = json.loads(manager.fetch_catalog())

    assert local == ["--kind", "checkouts", "--deadline-ms", "28500"]
    assert networked == ["--kind", "checkouts", "--deadline-ms", "58500", "--network"]
    assert catalog == []


def _install_slow_ssh(tmp_path: Path, monkeypatch, marker: Path) -> None:
    """Put a fake ``ssh`` first on ``PATH`` that blocks for a bounded time."""

    directory = tmp_path / "slow-ssh"
    directory.mkdir()
    script = directory / "ssh"
    script.write_text(
        "#!/bin/sh\n"
        f'printf started > "{marker}"\n'
        f"sleep {SLOW_SSH_SECONDS}\n"
        "exit 255\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")


def _blocked_remote(workspace, tmp_path, monkeypatch) -> Path:
    """Point every clone at a remote reached through the fake ssh."""

    marker = tmp_path / "ssh-started"
    _install_slow_ssh(tmp_path, monkeypatch, marker)
    for repository in (workspace.first, workspace.second):
        git(repository, "remote", "set-url", "origin", "ssh://git@example.invalid/r.git")
    return marker


def _wait_for(path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists()


@posix_only
def test_a_blocked_networked_fetch_returns_inside_its_budget_and_leaves_the_catalog_free(
    workspace, tmp_path, monkeypatch
):
    marker = _blocked_remote(workspace, tmp_path, monkeypatch)
    manager = _worker_manager(
        workspace,
        timeout=10,
        networked_checkouts_timeout=NETWORKED_BUDGET_SECONDS,
    )
    documents: list[bytes] = []
    errors: list[BaseException] = []

    def fetch() -> None:
        try:
            documents.append(manager.fetch_checkouts(network=True))
        except BaseException as error:  # noqa: BLE001 - a failure is the assertion
            errors.append(error)

    started = time.monotonic()
    thread = threading.Thread(target=fetch, daemon=True)
    with _managed(manager):
        thread.start()
        _wait_for(marker, 10)
        catalog = models.parse_catalog(manager.fetch_catalog())
        assert thread.is_alive()
        thread.join(timeout=60)

    assert not thread.is_alive()
    assert errors == []
    [payload] = documents
    document = models.parse_checkouts(payload)
    assert time.monotonic() - started < NETWORKED_BUDGET_SECONDS
    assert document["remote_checks"] == "on"
    assert [package["package_path"] for package in document["packages"]] == [
        "Fictional/Drafts/sample"
    ]
    [repository] = document["repositories"]
    check = repository["remote"]["check"]
    assert check["status"] == "failed"
    assert check["reason"] in {"timed_out", "deadline"}
    assert [entry["package_path"] for entry in catalog.as_dict()["entries"]] == [
        "Fictional/Drafts/sample"
    ]


@posix_only
def test_without_the_deadline_pass_through_the_fetch_reports_a_timeout(
    workspace, tmp_path, monkeypatch
):
    _blocked_remote(workspace, tmp_path, monkeypatch)
    # The mutation a reviewer applies: the manager still spawns a checkouts
    # worker, but it no longer hands the collector a deadline below its budget.
    monkeypatch.setattr(worker, "_deadline_ms", lambda timeout: worker.MAX_DEADLINE_MS)
    manager = _worker_manager(
        workspace,
        timeout=10,
        networked_checkouts_timeout=NETWORKED_BUDGET_SECONDS,
    )
    with _managed(manager):
        with pytest.raises(worker.WorkerError) as error:
            manager.fetch_checkouts(network=True)

    assert error.value.code == "checkouts_timeout"


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (
            "raise checkouts.CheckoutsOutputTooLargeError("
            "'checkouts output exceeds 2 MiB')",
            "checkouts_output_too_large",
        ),
        ("raise OSError('no scratch directory')", "checkouts_failed"),
    ],
    ids=["oversize", "oserror"],
)
def test_checkouts_categories_follow_the_worker_exit_status(
    workspace, monkeypatch, failure, category
):
    monkeypatch.setenv("PYTHONPATH", str(REPOSITORY_ROOT))
    script = (
        "from nyx import checkouts, worker\n"
        "def fail(*_args, **_kwargs):\n"
        f"    {failure}\n"
        "checkouts.collect_checkouts = fail\n"
        "raise SystemExit(worker.worker_entrypoint())\n"
    )
    manager = _worker_manager(
        workspace, command_factory=lambda: [sys.executable, "-c", script]
    )
    with _managed(manager):
        with pytest.raises(worker.WorkerError) as error:
            manager.fetch_checkouts(network=False)

    assert error.value.code == category


def test_a_state_error_while_observing_configuration_is_unavailable(monkeypatch):
    def observe() -> object:
        raise state.StateError("account home is unreadable")

    def collect(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("nothing may be collected without an observation")

    monkeypatch.setattr(worker.state, "observe_configuration", observe)
    from nyx import checkouts

    monkeypatch.setattr(checkouts, "collect_checkouts", collect)

    assert worker._worker_main(kind="checkouts", deadline_ms=1000) == 4
