"""Behavioral proofs for the checkouts worker kind against real repositories.

Every document comes from a real worker process over a real repository under an
isolated home, and every budget or deadline is lowered only through a manager
constructor value or a collector parameter.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
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
# How long a blocking program holds its call.  It outlives every budget here, so
# only a signal can explain its end, and it ends on its own if one never comes.
BLOCKING_PROGRAM_SECONDS = 60


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


# ---------------------------------------------------------------------------
# A checkouts worker contains the programs it starts
# ---------------------------------------------------------------------------


def _slow_command(directory: Path) -> tuple[Path, Path]:
    """An executable that starts a grandchild, records both ids and waits."""
    marker = directory / "slow-command-pids"
    script = directory / "slow-command"
    script.write_text(
        "#!/bin/sh\n"
        f"sleep {BLOCKING_PROGRAM_SECONDS} &\n"
        f"printf '%s %s\\n' \"$$\" \"$!\" > \"{marker}\"\n"
        "wait\n"
    )
    script.chmod(0o755)
    return script, marker


def _blocking_ssh(directory: Path, workspace) -> Path:
    """Point every clone at an ssh program that records its chain and blocks.

    git starts the program a repository's own ``core.sshCommand`` names, so the
    git call that waits for it, the fake ssh and the ssh's own grandchild are
    all descendants of the worker that collected the folder.
    """
    marker = directory / "ssh-pids"
    helper = directory / "blocking-ssh.py"
    helper.write_text(
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "marker = Path(sys.argv[1])\n"
        "grandchild = subprocess.Popen([\n"
        "    sys.executable, '-c', f'import time; time.sleep({%d})'\n"
        "])\n"
        "marker.write_text(f'{os.getpid()} {os.getppid()} {grandchild.pid}')\n"
        f"time.sleep({BLOCKING_PROGRAM_SECONDS})\n" % BLOCKING_PROGRAM_SECONDS
    )
    command = f'"{sys.executable}" "{helper}" "{marker}"'
    for repository in (workspace.first, workspace.second):
        git(repository, "remote", "set-url", "origin", "ssh://git@example.invalid/r.git")
        git(repository, "config", "core.sshCommand", command)
    return marker


def _recorded(marker: Path) -> list[int]:
    """The process ids a helper recorded, once it has recorded them."""
    return [int(field) for field in marker.read_text().split()]


def _alive(pid: int) -> bool:
    """Report whether one process id still names a running process."""
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _assert_gone(pids: Iterable[int], deadline: float) -> None:
    """Wait until no recorded process is alive, naming the survivors."""
    remaining = sorted(set(pids))
    while remaining and time.monotonic() < deadline:
        remaining = [pid for pid in remaining if _alive(pid)]
        if remaining:
            time.sleep(0.01)
    assert not remaining, f"processes {remaining} outlived their worker"


def _end(pids: Iterable[int]) -> None:
    """End anything a failed proof left behind."""
    for pid in sorted(set(pids)):
        if _alive(pid):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


def _process_parents() -> dict[int, int]:
    """Every live process id and its parent, from one process-table listing."""
    listing = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, check=True
    ).stdout
    parents: dict[int, int] = {}
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) == 2:
            parents[int(fields[0])] = int(fields[1])
    return parents


def _ancestors(pid: int, stop: int) -> list[int]:
    """Every process between ``pid`` and ``stop``, excluding ``stop``."""
    parents = _process_parents()
    chain: list[int] = []
    parent = parents.get(pid)
    while parent not in (None, 0, 1) and parent != stop:
        chain.append(parent)
        parent = parents.get(parent)
    assert parent == stop, f"no process chain from {pid} up to {stop}: {chain}"
    return chain


def _worker_pid(manager: worker.CatalogWorkerManager, timeout: float) -> int:
    """The process id of the worker this manager registered."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with manager._lock:
            if manager._children:
                return manager._children[0].process.pid
        time.sleep(0.01)
    raise AssertionError("the manager registered no worker")


@posix_only
def test_a_checkouts_timeout_ends_the_child_and_the_grandchild_it_started(
    workspace, tmp_path
):
    script, marker = _slow_command(tmp_path)
    manager = _worker_manager(
        workspace, command_factory=lambda: [str(script)], checkouts_timeout=0.5
    )
    errors: list[BaseException] = []
    recorded: list[int] = []

    def fetch() -> None:
        try:
            manager.fetch_checkouts(network=False)
        except BaseException as error:  # noqa: BLE001 - a failure is the assertion
            errors.append(error)

    thread = threading.Thread(target=fetch, daemon=True)
    try:
        with _managed(manager):
            thread.start()
            _wait_for(marker, 20)
            # The grandchild holds the worker's pipes and nothing else ends it,
            # so its end can only come from the group the worker leads.
            recorded = _recorded(marker)
            thread.join(timeout=20)
            assert not thread.is_alive()
            _assert_gone(recorded, time.monotonic() + 2)
    finally:
        thread.join(timeout=20)
        _end(recorded)

    assert [error.code for error in errors] == ["checkouts_timeout"]


@posix_only
def test_close_cancels_a_blocked_checkouts_collection_and_ends_its_grandchild(
    workspace, tmp_path
):
    script, marker = _slow_command(tmp_path)
    manager = _worker_manager(
        workspace, command_factory=lambda: [str(script)], checkouts_timeout=30
    )
    errors: list[BaseException] = []
    recorded: list[int] = []

    def fetch() -> None:
        try:
            manager.fetch_checkouts(network=False)
        except BaseException as error:  # noqa: BLE001 - a failure is the assertion
            errors.append(error)

    thread = threading.Thread(target=fetch, daemon=True)
    try:
        thread.start()
        _wait_for(marker, 20)
        recorded = _recorded(marker)
        assert manager.close(time.monotonic() + 5) is True
        thread.join(timeout=20)
        assert not thread.is_alive()
        assert manager.active_count == 0
        _assert_gone(recorded, time.monotonic() + 2)
    finally:
        manager.close(time.monotonic() + 5)
        thread.join(timeout=20)
        _end(recorded)

    assert [error.code for error in errors] == ["checkouts_cancelled"]


@posix_only
def test_owner_loss_ends_the_worker_with_git_the_ssh_and_its_grandchild(
    workspace, tmp_path
):
    marker = _blocking_ssh(tmp_path, workspace)
    read_fd, write_fd = os.pipe()
    manager = _worker_manager(
        workspace, networked_checkouts_timeout=60, parent_liveness_fd=read_fd
    )
    errors: list[BaseException] = []
    recorded: list[int] = []

    def fetch() -> None:
        try:
            manager.fetch_checkouts(network=True)
        except BaseException as error:  # noqa: BLE001 - a failure is the assertion
            errors.append(error)

    thread = threading.Thread(target=fetch, daemon=True)
    try:
        thread.start()
        _wait_for(marker, 30)
        worker_pid = _worker_pid(manager, 10)
        ssh_pid, parent_pid, grandchild_pid = _recorded(marker)
        # git is the ancestor that started the ssh program, and the whole chain
        # is a descendant of the worker that is about to lose its owner.
        recorded = [ssh_pid, parent_pid, grandchild_pid]
        recorded += _ancestors(ssh_pid, worker_pid)

        os.close(write_fd)
        write_fd = -1
        closed = time.monotonic()

        thread.join(timeout=30)
        assert not thread.is_alive()
        _assert_gone(recorded, closed + 2)
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        os.close(read_fd)
        manager.close(time.monotonic() + 30)
        thread.join(timeout=30)
        _end(recorded)

    assert [error.code for error in errors] == ["checkouts_failed"]


@posix_only
def test_a_checkouts_worker_without_its_own_session_exits_seven_on_owner_loss(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    isolate_git(monkeypatch, home)
    marker = tmp_path / "collecting"
    read_fd, write_fd = os.pipe()
    script = (
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "from nyx import checkouts, state, worker\n"
        "state.resolve_account_home = lambda: Path(os.environ['NYX_TEST_HOME'])\n"
        "marker = Path(sys.argv[1])\n"
        "def collect(*_args, **_kwargs):\n"
        "    marker.write_text('collecting')\n"
        "    time.sleep(60)\n"
        "    return 'late'\n"
        "checkouts.collect_checkouts = collect\n"
        "raise SystemExit(worker.worker_entrypoint(sys.argv[2:]))\n"
    )
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(marker),
            "--kind",
            "checkouts",
            "--deadline-ms",
            "30000",
            "--parent-liveness-fd",
            str(read_fd),
        ],
        cwd=REPOSITORY_ROOT,
        env={**os.environ, "NYX_TEST_HOME": str(home)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=(read_fd,),
    )
    try:
        _wait_for(marker, 30)
        # Spawned without a session, the worker shares this test process's
        # group; it must exit on its own rather than signal that group.
        assert os.getsid(child.pid) != child.pid
        os.close(write_fd)
        write_fd = -1
        assert child.wait(timeout=30) == 7
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        if write_fd >= 0:
            os.close(write_fd)
        os.close(read_fd)


def test_a_finished_collection_leaves_no_grandchild_behind(workspace, tmp_path):
    marker = _blocking_ssh(tmp_path, workspace)
    manager = _worker_manager(workspace, networked_checkouts_timeout=NETWORKED_BUDGET_SECONDS)
    recorded: list[int] = []
    try:
        with _managed(manager):
            document = models.parse_checkouts(manager.fetch_checkouts(network=True))
        recorded = _recorded(marker)
        _assert_gone(recorded, time.monotonic() + 2)
    finally:
        _end(recorded)

    [repository] = document["repositories"]
    check = repository["remote"]["check"]
    assert check["status"] == "failed"
    assert check["reason"] in {"timed_out", "deadline"}


def _spawned_in_its_own_session(
    workspace, directory: Path, kind: str
) -> bool:
    """Report whether a spawn of ``kind`` leads the session it was started in."""
    script = directory / f"blocking-{kind}"
    # No pipe is held past the spawned process, so each kind's own termination
    # is the only thing that has to end it.
    script.write_text("#!/bin/sh\nsleep 60 >/dev/null 2>&1\n")
    script.chmod(0o755)
    manager = _worker_manager(workspace, command_factory=lambda: [str(script)])
    errors: list[BaseException] = []

    def fetch() -> None:
        try:
            if kind == worker.CHECKOUTS_KIND:
                manager.fetch_checkouts(network=False)
            else:
                manager.fetch_catalog()
        except BaseException as error:  # noqa: BLE001 - a failure is the assertion
            errors.append(error)

    thread = threading.Thread(target=fetch, daemon=True)
    try:
        thread.start()
        pid = _worker_pid(manager, 10)
        leads = os.getsid(pid) == pid
        assert manager.close(time.monotonic() + 5)
        thread.join(timeout=10)
        assert not thread.is_alive()
    finally:
        manager.close(time.monotonic() + 5)
        thread.join(timeout=10)

    assert len(errors) == 1
    return leads


@posix_only
def test_only_a_checkouts_worker_leads_its_own_session(workspace, tmp_path):
    assert _spawned_in_its_own_session(workspace, tmp_path, "checkouts") is True
    assert _spawned_in_its_own_session(workspace, tmp_path, "catalog") is False
