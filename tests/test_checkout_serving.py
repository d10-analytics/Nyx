"""Behavioral proofs for serving the checkout document from one serial run queue.

The serving state machine runs on a manager double and an injected monotonic
clock, so no proof spends a real interval or starts a worker.  The double
answers with real schema-8 document bytes captured once from a real collection.
One real worker proves the route wiring end to end: a served document is
accepted by the parser, and a refresh shows a commit made after the first
answer.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from test_checkouts import clone, commit_file, git, isolate_git, make_remote

from nyx import checkouts, models, state, worker
from nyx.app_runtime import (
    LOCAL_REFRESH_INTERVAL,
    REMOTE_CHECK_INTERVAL,
    ApplicationRuntime,
)
from nyx.server import CatalogError
from nyx.worker import WorkerError

# Fictional, distinct observation instants for the documents the double returns.
_FIRST_OBSERVATION = datetime(2026, 1, 1, tzinfo=timezone.utc)
_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _document_timestamp(serial: int) -> str:
    """Return the ``observed_at`` the document with ``serial`` carries."""

    return (_FIRST_OBSERVATION + timedelta(seconds=serial)).strftime(_FORMAT)


def _timestamp_after(text: str, seconds: float) -> str:
    moment = datetime.strptime(text, _FORMAT).replace(tzinfo=timezone.utc)
    return (moment + timedelta(seconds=seconds)).strftime(_FORMAT)


class InjectedClock:
    """The runtime's monotonic schedule clock, moved only by the test."""

    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class CheckoutsCall:
    """One recorded ``fetch_checkouts`` call and the event that releases it."""

    def __init__(self, order: int, network: bool) -> None:
        self.order = order
        self.network = network
        self.held = threading.Event()
        self.cancelled = False


class CheckoutsManagerDouble:
    """The one manager the runtime is handed, recording and holding each call.

    Every call blocks until the test releases it, so a request that answers at
    once is visibly different from one that waits on a run.  Each returned
    document is the captured real document with a distinct ``observed_at``,
    which its digest omits, so the parser still accepts every answer.
    """

    def __init__(self, document: bytes) -> None:
        self._document = document
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._calls: list[CheckoutsCall] = []
        self._outcomes: list[str] = []
        self._in_flight = 0
        self._closed = False
        self._served = 0
        self.max_in_flight = 0
        self.ignore_cancellation = False

    @property
    def calls(self) -> list[CheckoutsCall]:
        with self._lock:
            return list(self._calls)

    @property
    def active_count(self) -> int:
        """Report the calls a worker would still be running."""

        with self._lock:
            return self._in_flight

    def next_outcome(self, outcome: str) -> None:
        """Choose what the next call raises or returns instead of a document."""

        with self._lock:
            self._outcomes.append(outcome)

    def wait_for_calls(self, count: int, timeout: float = 10.0) -> None:
        """Wait until the runtime has started ``count`` collections."""

        deadline = time.monotonic() + timeout
        with self._changed:
            while len(self._calls) < count:
                remaining = deadline - time.monotonic()
                assert remaining > 0, f"the runtime did not start collection {count}"
                self._changed.wait(remaining)

    def release(self, order: int = -1) -> None:
        """Release one held call; the last one by default."""

        self.calls[order].held.set()

    def release_all(self) -> None:
        for call in self.calls:
            call.held.set()

    def fetch_checkouts(self, *, network: bool) -> bytes:
        with self._changed:
            if self._closed:
                raise WorkerError("checkouts_cancelled")
            call = CheckoutsCall(len(self._calls), network)
            self._calls.append(call)
            outcome = self._outcomes.pop(0) if self._outcomes else "document"
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            self._changed.notify_all()
        call.held.wait()
        with self._changed:
            self._in_flight -= 1
            self._changed.notify_all()
            cancelled = call.cancelled
        if cancelled:
            raise WorkerError("checkouts_cancelled")
        if outcome == "rejected":
            return b"not a checkout document"
        if outcome == "exception":
            raise RuntimeError("unexpected collection failure")
        if outcome.startswith("worker:"):
            raise WorkerError(outcome.split(":", 1)[1])
        return self._next_document()

    def close_admission(self) -> None:
        """Release a held call as the manager does when admission closes."""

        with self._changed:
            self._closed = True
            if self.ignore_cancellation:
                return
            for call in self._calls:
                call.cancelled = True
                call.held.set()
            self._changed.notify_all()

    def close(self, deadline: float) -> bool:
        # The double owns no child, so only the runtime's own run-thread join
        # can make a shutdown report failure.
        with self._changed:
            self._closed = True
        return True

    def _next_document(self) -> bytes:
        with self._changed:
            self._served += 1
            serial = self._served
        document = json.loads(self._document)
        document["observed_at"] = _document_timestamp(serial)
        return json.dumps(document, separators=(",", ":")).encode("utf-8")


class _Request:
    """One request running on its own thread so a wait cannot hang the suite."""

    def __init__(self, function: Any) -> None:
        self.value: Any = None
        self.error: BaseException | None = None
        self._function = function
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self.value = self._function()
        except BaseException as error:  # noqa: BLE001 - re-raised by the test thread
            self.error = error

    def start(self) -> _Request:
        self._thread.start()
        return self

    def join(self, timeout: float = 10.0) -> Any:
        self._thread.join(timeout)
        assert not self._thread.is_alive(), "the request did not answer"
        if self.error is not None:
            raise self.error
        return self.value


def _settle(runtime: ApplicationRuntime, timeout: float = 10.0) -> None:
    """Wait until no run is queued or running, then check no run thread lives."""

    deadline = time.monotonic() + timeout
    while True:
        with runtime._checkouts_condition:  # noqa: SLF001 - the serving owner's state
            idle = (
                runtime._checkouts_running is None  # noqa: SLF001
                and runtime._checkouts_pending is None  # noqa: SLF001
            )
            threads = not runtime._checkouts_threads  # noqa: SLF001
        assert idle or time.monotonic() < deadline, "a checkout run did not finish"
        if idle:
            assert threads, "an idle runtime still holds a live run thread"
            return
        time.sleep(0.001)


@pytest.fixture(scope="module")
def captured_checkouts(tmp_path_factory):
    """One real collection of a real clone, captured once for the double."""

    root = tmp_path_factory.mktemp("checkouts-capture")
    home = root / "home"
    home.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        isolate_git(patch, home)
        served = root / "served"
        served.mkdir()
        remote = make_remote(served)
        folder = root / "checkouts"
        folder.mkdir()
        clone(remote.as_uri(), folder / "Clone One")
        rendered = checkouts.collect_checkouts(
            [str(folder)],
            deadline_ns=time.monotonic_ns() + 30_000_000_000,
        )
    return rendered.encode("utf-8")


@pytest.fixture
def serving(captured_checkouts):
    """A runtime over the manager double and an injected clock."""

    manager = CheckoutsManagerDouble(captured_checkouts)
    clock = InjectedClock()
    runtime = ApplicationRuntime(
        port=0,
        deadline=time.monotonic() + 60,
        clock=clock,
        workers=manager,
    )
    runtime.admit_catalog()
    yield SimpleNamespace(
        runtime=runtime,
        manager=manager,
        clock=clock,
        captured=models.parse_checkouts(captured_checkouts),
    )
    assert manager.max_in_flight <= 1
    manager.close_admission()
    assert manager.close(time.monotonic() + 5)


def _establish_document(serving) -> str:
    """Answer one first request, leaving a current document and a fresh clock."""

    request = _Request(serving.runtime.get_checkouts).start()
    serving.manager.wait_for_calls(1)
    serving.manager.release(0)
    envelope = request.join()
    assert envelope["checkouts"]["observed_at"] == _document_timestamp(1)
    return envelope["checkouts"]["observed_at"]


def _observed(envelope: dict[str, Any]) -> str:
    return envelope["checkouts"]["observed_at"]


def test_the_first_request_waits_for_one_check_and_a_concurrent_request_shares_it(serving):
    manager = serving.manager

    first = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(1)
    second = _Request(serving.runtime.get_checkouts).start()
    manager.release(0)

    first_envelope = first.join()
    second_envelope = second.join()

    assert [call.network for call in manager.calls] == [True]
    assert _observed(first_envelope) == _document_timestamp(1)
    assert _observed(second_envelope) == _document_timestamp(1)
    remote_checks = first_envelope["remote_checks"]
    assert set(remote_checks) == {"state", "interval_seconds", "next_due_at", "last"}
    assert remote_checks["state"] == "idle"
    assert remote_checks["interval_seconds"] == REMOTE_CHECK_INTERVAL
    last = remote_checks["last"]
    assert set(last) == {"started_at", "finished_at", "outcome", "repositories"}
    assert last["outcome"] == "completed"
    assert remote_checks["next_due_at"] == _timestamp_after(
        last["started_at"], REMOTE_CHECK_INTERVAL
    )
    [repository] = last["repositories"]
    [captured] = serving.captured["repositories"]
    assert repository == {
        "remote": captured["remote"]["key"],
        "check": {"status": "off", "reason": "remote_checks_off"},
        "pr_lookup": {"status": "off", "reason": "setting_off"},
    }
    _settle(serving.runtime)


def test_served_requests_answer_without_a_collection(serving):
    observed = _establish_document(serving)

    for _ in range(2):
        envelope = _Request(serving.runtime.get_checkouts).start().join(timeout=5)
        assert envelope["checkouts"]["observed_at"] == observed
        assert envelope["remote_checks"]["state"] == "idle"
    assert len(serving.manager.calls) == 1
    _settle(serving.runtime)


def test_a_clock_advance_starts_one_local_refresh_behind_the_served_document(serving):
    manager, clock = serving.manager, serving.clock
    observed = _establish_document(serving)

    clock.advance(LOCAL_REFRESH_INTERVAL)
    refresh = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    refresh_answer = refresh.join(timeout=5)
    assert refresh_answer["checkouts"]["observed_at"] == observed
    assert [call.network for call in manager.calls] == [True, False]

    while_held = _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert while_held["checkouts"]["observed_at"] == observed
    assert len(manager.calls) == 2

    manager.release(1)
    _settle(serving.runtime)
    later = _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert later["checkouts"]["observed_at"] == _document_timestamp(2)


def test_a_mark_starts_nothing_and_the_next_request_waits_for_post_mark_work(serving):
    manager = serving.manager
    _establish_document(serving)

    serving.runtime.mark_checkouts_changed()
    assert len(manager.calls) == 1

    after_mark = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    manager.release(1)
    envelope = after_mark.join()
    assert envelope["checkouts"]["observed_at"] == _document_timestamp(2)
    assert [call.network for call in manager.calls] == [True, True]
    _settle(serving.runtime)


def test_a_held_run_from_before_a_mark_never_serves_its_document(serving):
    manager, clock = serving.manager, serving.clock
    observed = _establish_document(serving)

    clock.advance(LOCAL_REFRESH_INTERVAL)
    trigger = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    assert _observed(trigger.join(timeout=5)) == observed

    serving.runtime.mark_checkouts_changed()
    after_mark = _Request(serving.runtime.get_checkouts).start()
    assert len(manager.calls) == 2

    manager.release(1)
    manager.wait_for_calls(3)
    assert [call.network for call in manager.calls] == [True, False, True]
    manager.release(2)

    envelope = after_mark.join()
    assert envelope["checkouts"]["observed_at"] == _document_timestamp(3)
    assert envelope["checkouts"]["observed_at"] != _document_timestamp(2)
    _settle(serving.runtime)


def test_a_held_run_that_fails_after_a_mark_opens_no_gate(serving):
    manager, clock = serving.manager, serving.clock
    observed = _establish_document(serving)

    clock.advance(LOCAL_REFRESH_INTERVAL)
    manager.next_outcome("worker:checkouts_timeout")
    trigger = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    assert _observed(trigger.join(timeout=5)) == observed

    serving.runtime.mark_checkouts_changed()
    after_mark = _Request(serving.runtime.get_checkouts).start()
    assert len(manager.calls) == 2

    manager.release(1)
    manager.wait_for_calls(3)
    manager.release(2)

    envelope = after_mark.join()
    assert envelope["checkouts"]["observed_at"] == _document_timestamp(2)
    _settle(serving.runtime)


def test_a_failed_first_check_gates_requests_and_its_retries_reopen_it(serving):
    manager, clock = serving.manager, serving.clock
    manager.next_outcome("worker:checkouts_timeout")

    first = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(1)
    second = _Request(serving.runtime.get_checkouts).start()
    manager.release(0)

    for request in (first, second):
        with pytest.raises(CatalogError) as error:
            request.join()
        assert error.value.code == "checkouts_timeout"

    assert [call.network for call in manager.calls] == [True]
    for _ in range(2):
        with pytest.raises(CatalogError) as gated:
            _Request(serving.runtime.get_checkouts).start().join(timeout=5)
        assert gated.value.code == "checkouts_timeout"
    assert len(manager.calls) == 1

    clock.advance(LOCAL_REFRESH_INTERVAL)
    manager.next_outcome("rejected")
    rejected = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    assert manager.calls[1].network is False
    manager.release(1)
    with pytest.raises(CatalogError) as error:
        rejected.join()
    assert error.value.code == "checkouts_protocol_error"

    with pytest.raises(CatalogError) as gated:
        _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert gated.value.code == "checkouts_protocol_error"
    assert len(manager.calls) == 2

    clock.advance(LOCAL_REFRESH_INTERVAL)
    manager.next_outcome("exception")
    crashed = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(3)
    manager.release(2)
    with pytest.raises(CatalogError) as error:
        crashed.join()
    assert error.value.code == "checkouts_failed"

    with pytest.raises(CatalogError) as gated:
        _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert gated.value.code == "checkouts_failed"
    assert len(manager.calls) == 3

    clock.advance(LOCAL_REFRESH_INTERVAL)
    repaired = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(4)
    manager.release(3)
    envelope = repaired.join()
    assert envelope["checkouts"]["observed_at"] == _document_timestamp(1)
    assert [call.network for call in manager.calls] == [True, False, False, False]
    _settle(serving.runtime)


def test_a_due_check_answers_behind_the_served_document_and_its_failure_changes_nothing(serving):
    manager, clock = serving.manager, serving.clock
    observed = _establish_document(serving)

    clock.advance(REMOTE_CHECK_INTERVAL)
    manager.next_outcome("worker:checkouts_timeout")
    requested = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    answer = requested.join(timeout=5)
    assert answer["checkouts"]["observed_at"] == observed
    assert answer["remote_checks"]["state"] == "running"

    manager.release(1)
    _settle(serving.runtime)
    assert serving.runtime._checkouts_failure is None  # noqa: SLF001 - a current document exists

    follow = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(3)
    follow_answer = follow.join(timeout=5)
    assert follow_answer["checkouts"]["observed_at"] == observed
    last = follow_answer["remote_checks"]["last"]
    assert last["outcome"] == "checkouts_timeout"
    assert last["repositories"] == []
    assert [call.network for call in manager.calls] == [True, True, False]
    manager.release(2)
    _settle(serving.runtime)


def test_a_mark_during_the_first_run_answers_its_waiter_and_supersedes_its_document(serving):
    manager = serving.manager

    first = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(1)
    serving.runtime.mark_checkouts_changed()
    manager.release(0)

    first_answer = first.join()
    assert first_answer["checkouts"]["observed_at"] == _document_timestamp(1)
    assert first_answer["remote_checks"]["last"]["outcome"] == "completed"
    assert len(manager.calls) == 1
    _settle(serving.runtime)

    later = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    assert manager.calls[1].network is True
    manager.release(1)
    later_answer = later.join()
    assert later_answer["checkouts"]["observed_at"] == _document_timestamp(2)
    _settle(serving.runtime)


def test_a_mark_closes_the_failure_gate(serving):
    manager = serving.manager
    manager.next_outcome("worker:checkouts_timeout")

    first = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(1)
    manager.release(0)
    with pytest.raises(CatalogError) as error:
        first.join()
    assert error.value.code == "checkouts_timeout"

    with pytest.raises(CatalogError) as gated:
        _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert gated.value.code == "checkouts_timeout"

    serving.runtime.mark_checkouts_changed()
    retry = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    manager.release(1)
    envelope = retry.join()
    assert envelope["checkouts"]["observed_at"] == _document_timestamp(1)
    assert [call.network for call in manager.calls] == [True, True]
    _settle(serving.runtime)


def test_a_failed_local_refresh_is_retried_at_most_once_per_interval(serving):
    manager, clock = serving.manager, serving.clock
    observed = _establish_document(serving)

    clock.advance(LOCAL_REFRESH_INTERVAL)
    manager.next_outcome("worker:checkouts_timeout")
    refresh = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    assert _observed(refresh.join(timeout=5)) == observed
    manager.release(1)
    _settle(serving.runtime)

    for _ in range(2):
        inside = _Request(serving.runtime.get_checkouts).start().join(timeout=5)
        assert inside["checkouts"]["observed_at"] == observed
        assert inside["remote_checks"]["last"]["outcome"] == "completed"
    assert len(manager.calls) == 2

    clock.advance(LOCAL_REFRESH_INTERVAL)
    after = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(3)
    assert _observed(after.join(timeout=5)) == observed
    assert [call.network for call in manager.calls] == [True, False, False]
    manager.release(2)
    _settle(serving.runtime)
    final = _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert final["checkouts"]["observed_at"] == _document_timestamp(2)


def test_a_remote_check_answers_with_the_document_of_its_own_run(serving):
    manager = serving.manager
    observed = _establish_document(serving)

    request = _Request(serving.runtime.refresh_remote_checks).start()
    manager.wait_for_calls(2)
    assert manager.calls[1].network is True
    manager.release(1)

    envelope = request.join()
    assert envelope["checkouts"]["observed_at"] == _document_timestamp(2)
    assert envelope["checkouts"]["observed_at"] != observed
    assert envelope["remote_checks"]["last"]["outcome"] == "completed"
    _settle(serving.runtime)


def test_a_failed_remote_check_answers_with_the_served_document_and_its_outcome(serving):
    manager = serving.manager
    observed = _establish_document(serving)

    manager.next_outcome("worker:checkouts_failed")
    request = _Request(serving.runtime.refresh_remote_checks).start()
    manager.wait_for_calls(2)
    manager.release(1)

    envelope = request.join()
    assert envelope["checkouts"]["observed_at"] == observed
    assert envelope["remote_checks"]["last"]["outcome"] == "checkouts_failed"
    assert serving.runtime._checkouts_failure is None  # noqa: SLF001 - a current document exists
    _settle(serving.runtime)


def test_a_failed_remote_check_without_a_served_document_answers_its_category(serving):
    manager = serving.manager
    manager.next_outcome("worker:checkouts_timeout")

    request = _Request(serving.runtime.refresh_remote_checks).start()
    manager.wait_for_calls(1)
    manager.release(0)

    with pytest.raises(CatalogError) as error:
        request.join()
    assert error.value.code == "checkouts_timeout"
    _settle(serving.runtime)


def test_concurrent_remote_checks_share_the_pending_check(serving):
    manager, clock = serving.manager, serving.clock
    _establish_document(serving)

    clock.advance(LOCAL_REFRESH_INTERVAL)
    trigger = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    trigger.join(timeout=5)

    first = _Request(serving.runtime.refresh_remote_checks).start()
    second = _Request(serving.runtime.refresh_remote_checks).start()
    assert len(manager.calls) == 2

    manager.release(1)
    manager.wait_for_calls(3)
    assert manager.calls[2].network is True
    manager.release(2)

    assert _observed(first.join()) == _document_timestamp(3)
    assert _observed(second.join()) == _document_timestamp(3)
    assert [call.network for call in manager.calls] == [True, False, True]
    _settle(serving.runtime)


def test_a_worker_cancellation_answers_its_waiters_without_opening_a_gate(serving):
    manager = serving.manager

    request = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(1)
    manager.close_admission()
    with pytest.raises(CatalogError) as error:
        request.join()
    assert error.value.code == "checkouts_cancelled"
    assert serving.runtime._checkouts_last_check is None  # noqa: SLF001 - nothing completed
    assert serving.runtime._checkouts_failure is None  # noqa: SLF001 - no gate opened

    with pytest.raises(CatalogError) as again:
        _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert again.value.code == "checkouts_cancelled"


def _write_record(home, specification, folders):
    """Write the account record whose checkout inputs name ``folders``."""

    key = str(specification.resolve())
    record = {
        "schema_version": 4,
        "specification_root": key,
        "checkout_settings": {
            key: {
                "folders": sorted(str(folder) for folder in folders),
                "github_lookup": True,
            }
        },
    }
    paths = state.state_paths(create=True)
    assert paths.config_file.is_relative_to(home)
    paths.config_file.write_bytes(
        (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    )
    return paths


def _main_heads(document: dict[str, Any]) -> list[str]:
    return [
        worktree["head"]["sha"]
        for repository in document["repositories"]
        for clone_document in repository["clones"]
        for worktree in clone_document["worktrees"]
        if worktree["main"]
    ]


def test_a_real_worker_serves_an_accepted_document_and_a_refresh_shows_a_new_commit(
    tmp_path, monkeypatch
):
    """The published document answers while the refresh collects, so the new head
    appears on the request that follows the refresh."""

    home = tmp_path / "home"
    home.mkdir()
    isolate_git(monkeypatch, home)
    folder = tmp_path / "checkouts"
    folder.mkdir()
    repository = folder / "Sample"
    git(tmp_path, "init", "-q", "-b", "trunk", str(repository))
    first_commit = commit_file(repository, "file.txt", "one\n", "seed")
    specification = tmp_path / "specification"
    specification.mkdir()
    paths = _write_record(home, specification, [folder])

    manager = worker.CatalogWorkerManager(working_directory=paths.state_directory)
    clock = InjectedClock()
    runtime = ApplicationRuntime(
        port=0,
        deadline=time.monotonic() + 60,
        clock=clock,
        workers=manager,
    )
    runtime.admit_catalog()
    try:
        first = _Request(runtime.get_checkouts).start().join()
        document = models.parse_checkouts(first["checkouts"])
        [repository_document] = document["repositories"]
        assert repository_document["remote"] is None
        assert _main_heads(document) == [first_commit]

        second_commit = commit_file(repository, "file.txt", "two\n", "edit")
        clock.advance(LOCAL_REFRESH_INTERVAL)
        served = _Request(runtime.get_checkouts).start().join(timeout=5)
        assert _main_heads(models.parse_checkouts(served["checkouts"])) == [first_commit]

        _settle(runtime, timeout=30)
        refreshed = _Request(runtime.get_checkouts).start().join()
        assert _main_heads(models.parse_checkouts(refreshed["checkouts"])) == [second_commit]
    finally:
        manager.close_admission()
        assert manager.close(time.monotonic() + 30)
