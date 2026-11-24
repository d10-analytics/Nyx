"""Behavioral proofs for serving the checkout document from one serial run queue.

The serving state machine runs on a manager double and an injected monotonic
clock, so no proof spends a real interval or starts a worker: the cadence, the
change count, the failure gate and the shutdown rules are all read from that
double, and the double rejects a collection that asks for no network.  Real
workers stay where they alone prove something: a first request's document is
accepted by the parser, a run's live labels become the check record, a push
from one clone is answered against the other, a failed check keeps the saved
record, a GitHub lookup failure reaches the last check's report, no service run
writes a repository reference, and a real held check is cancelled at shutdown.
The transport is recorded by the program ``remote.origin.uploadpack`` names.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from test_checkouts import (
    clone,
    commit_file,
    doc_copy,
    find_clone,
    git,
    install_fake_gh,
    isolate_git,
    make_remote,
    point_origin_at,
    rederive,
    redigest,
    repository_fingerprint,
)
from test_server import RunningServer, StubClient, valid_catalog
from test_server import request as http_request

from nyx import checkouts, models, state, worker
from nyx.app_runtime import REFRESH_INTERVAL, ApplicationRuntime
from nyx.server import CatalogError
from nyx.worker import WorkerError

# Fictional, distinct observation instants for the documents the double returns.
_FIRST_OBSERVATION = datetime(2026, 1, 1, tzinfo=timezone.utc)
_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# The hold a run that fails with no document keeps further requests under.
_FAILURE_HOLD = min(REFRESH_INTERVAL, 60.0)
# The fake ``gh`` used by the lookup proof is a shell script.
posix_only = pytest.mark.skipif(os.name == "nt", reason="needs a POSIX shell script")


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

    @property
    def finished(self) -> bool:
        """Whether the request has answered already."""

        return not self._thread.is_alive()

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


def _threads_outside(
    threads_before: frozenset[threading.Thread], timeout: float = 5.0
) -> list[threading.Thread]:
    """Live threads that were not alive before the runtime was constructed.

    A run removes itself from ``_checkouts_threads`` just before its thread
    exits, so a run thread still in its last instructions is joined first
    instead of being counted as a stray.
    """

    deadline = time.monotonic() + timeout
    for thread in threading.enumerate():
        if thread in threads_before or thread.name != "nyx-checkouts-run":
            continue
        thread.join(max(0.0, deadline - time.monotonic()))
    return [
        thread for thread in threading.enumerate() if thread not in threads_before
    ]


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

    threads_before = frozenset(threading.enumerate())
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
        threads_before=threads_before,
        captured=models.parse_checkouts(captured_checkouts),
    )
    assert manager.max_in_flight <= 1
    assert all(call.network for call in manager.calls), "a run collected without the network"
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
    assert remote_checks["interval_seconds"] == REFRESH_INTERVAL
    last = remote_checks["last"]
    assert set(last) == {"started_at", "finished_at", "outcome", "repositories"}
    assert last["outcome"] == "completed"
    assert remote_checks["next_due_at"] == _timestamp_after(
        last["started_at"], REFRESH_INTERVAL
    )
    [repository] = last["repositories"]
    [captured] = serving.captured["repositories"]
    assert repository == {
        "remote": captured["remote"]["key"],
        "check": {"status": "off", "reason": "remote_checks_off"},
        "pr_lookup": {"status": "off", "reason": "setting_off"},
    }
    _settle(serving.runtime)


def _two_remote_document(captured: bytes) -> bytes:
    """Return two repositories whose display order and remote-key order differ."""

    document = json.loads(captured)
    first = doc_copy(document["repositories"][0])
    second = doc_copy(first)
    path = first["clones"][0]["path"]
    second = json.loads(
        json.dumps(second).replace(json.dumps(path), json.dumps(f"{path} Two"))
    )
    first["remote"]["key"] = "z.example/team/alpha"
    first["remote"]["display"] = "Alpha Remote"
    second["remote"]["key"] = "a.example/team/beta"
    second["remote"]["display"] = "Beta Remote"
    document["repositories"] = [first, second]
    redigest(rederive(document))
    return json.dumps(document, separators=(",", ":")).encode("utf-8")


def test_a_check_records_repository_remotes_in_key_order(captured_checkouts):
    document = _two_remote_document(captured_checkouts)
    listed = [repository["remote"]["key"] for repository in json.loads(document)["repositories"]]
    assert listed == ["z.example/team/alpha", "a.example/team/beta"]
    assert listed != sorted(listed)

    manager = CheckoutsManagerDouble(document)
    runtime = ApplicationRuntime(
        port=0,
        deadline=time.monotonic() + 60,
        clock=InjectedClock(),
        workers=manager,
    )
    runtime.admit_catalog()
    try:
        request = _Request(runtime.get_checkouts).start()
        manager.wait_for_calls(1)
        assert manager.calls[0].network is True
        manager.release(0)
        envelope = request.join()
        recorded = [
            repository["remote"]
            for repository in envelope["remote_checks"]["last"]["repositories"]
        ]
        assert recorded == sorted(listed)
        _settle(runtime)
    finally:
        assert runtime.shutdown(time.monotonic() + 5) is True


def test_served_requests_answer_without_a_collection(serving):
    observed = _establish_document(serving)

    for _ in range(2):
        envelope = _Request(serving.runtime.get_checkouts).start().join(timeout=5)
        assert envelope["checkouts"]["observed_at"] == observed
        assert envelope["remote_checks"]["state"] == "idle"
    assert len(serving.manager.calls) == 1
    _settle(serving.runtime)


def test_a_clock_advance_starts_one_run_behind_the_served_document(serving):
    manager, clock = serving.manager, serving.clock
    observed = _establish_document(serving)

    clock.advance(REFRESH_INTERVAL)
    refresh = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    refresh_answer = refresh.join(timeout=5)
    assert refresh_answer["checkouts"]["observed_at"] == observed
    assert [call.network for call in manager.calls] == [True, True]

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

    clock.advance(REFRESH_INTERVAL)
    trigger = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    assert _observed(trigger.join(timeout=5)) == observed

    serving.runtime.mark_checkouts_changed()
    after_mark = _Request(serving.runtime.get_checkouts).start()
    assert len(manager.calls) == 2

    manager.release(1)
    manager.wait_for_calls(3)
    assert [call.network for call in manager.calls] == [True, True, True]
    manager.release(2)

    envelope = after_mark.join()
    assert envelope["checkouts"]["observed_at"] == _document_timestamp(3)
    assert envelope["checkouts"]["observed_at"] != _document_timestamp(2)
    _settle(serving.runtime)


def test_a_held_run_that_fails_after_a_mark_opens_no_gate(serving):
    manager, clock = serving.manager, serving.clock
    observed = _establish_document(serving)

    clock.advance(REFRESH_INTERVAL)
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
    # Every read inside the hold answers the same category and starts no run.
    clock.advance(_FAILURE_HOLD - 1)
    with pytest.raises(CatalogError) as gated:
        _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert gated.value.code == "checkouts_timeout"
    assert len(manager.calls) == 1

    # One second later the hold is over, well before the interval, and the
    # read starts the next run.
    clock.advance(1)
    manager.next_outcome("rejected")
    rejected = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    assert manager.calls[1].network is True
    manager.release(1)
    with pytest.raises(CatalogError) as error:
        rejected.join()
    assert error.value.code == "checkouts_protocol_error"

    with pytest.raises(CatalogError) as gated:
        _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert gated.value.code == "checkouts_protocol_error"
    assert len(manager.calls) == 2

    clock.advance(_FAILURE_HOLD)
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

    clock.advance(_FAILURE_HOLD)
    repaired = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(4)
    manager.release(3)
    envelope = repaired.join()
    assert envelope["checkouts"]["observed_at"] == _document_timestamp(1)
    assert [call.network for call in manager.calls] == [True, True, True, True]
    _settle(serving.runtime)


def test_a_due_run_answers_behind_the_served_document_and_its_failure_changes_nothing(serving):
    manager, clock = serving.manager, serving.clock
    observed = _establish_document(serving)

    clock.advance(REFRESH_INTERVAL)
    manager.next_outcome("worker:checkouts_timeout")
    requested = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    answer = requested.join(timeout=5)
    assert answer["checkouts"]["observed_at"] == observed
    assert answer["remote_checks"]["state"] == "running"

    manager.release(1)
    _settle(serving.runtime)
    assert serving.runtime._checkouts_failure is None  # noqa: SLF001 - a current document exists

    # The failed run keeps the served document, writes its own outcome into the
    # record, and is not retried before the interval.
    follow = _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert follow["checkouts"]["observed_at"] == observed
    last = follow["remote_checks"]["last"]
    assert last["outcome"] == "checkouts_timeout"
    assert last["repositories"] == []
    assert len(manager.calls) == 2, "a failed run behind a document was retried before the interval"

    clock.advance(REFRESH_INTERVAL)
    later = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(3)
    assert manager.calls[2].network is True
    assert _observed(later.join(timeout=5)) == observed
    manager.release(2)
    _settle(serving.runtime)
    served = _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert served["checkouts"]["observed_at"] == _document_timestamp(2)
    assert [call.network for call in manager.calls] == [True, True, True]


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


def test_a_failed_run_behind_a_document_is_retried_at_most_once_per_interval(serving):
    manager, clock = serving.manager, serving.clock
    observed = _establish_document(serving)

    clock.advance(REFRESH_INTERVAL)
    manager.next_outcome("worker:checkouts_timeout")
    refresh = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    assert _observed(refresh.join(timeout=5)) == observed
    manager.release(1)
    _settle(serving.runtime)

    for _ in range(2):
        inside = _Request(serving.runtime.get_checkouts).start().join(timeout=5)
        assert inside["checkouts"]["observed_at"] == observed
        assert inside["remote_checks"]["last"]["outcome"] == "checkouts_timeout"
    assert len(manager.calls) == 2

    clock.advance(REFRESH_INTERVAL)
    after = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(3)
    assert _observed(after.join(timeout=5)) == observed
    assert [call.network for call in manager.calls] == [True, True, True]
    manager.release(2)
    _settle(serving.runtime)
    final = _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert final["checkouts"]["observed_at"] == _document_timestamp(2)


def test_concurrent_requests_share_the_one_waiting_run(serving):
    manager, clock = serving.manager, serving.clock
    _establish_document(serving)

    clock.advance(REFRESH_INTERVAL)
    trigger = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    trigger.join(timeout=5)

    # The held run is followed by the one run a change mark queues, and every
    # request shares it instead of starting a run of its own.
    serving.runtime.mark_checkouts_changed()
    first = _Request(serving.runtime.get_checkouts).start()
    second = _Request(serving.runtime.get_checkouts).start()
    _wait_for_pending(serving.runtime)
    assert len(manager.calls) == 2

    # A read past the interval still finds one run in flight and one waiting, so
    # it queues nothing further.
    clock.advance(REFRESH_INTERVAL)
    third = _Request(serving.runtime.get_checkouts).start()
    assert len(manager.calls) == 2, "a read past the interval queued another run"

    manager.release(1)
    manager.wait_for_calls(3)
    assert manager.calls[2].network is True
    manager.release(2)

    assert _observed(first.join()) == _document_timestamp(3)
    assert _observed(second.join()) == _document_timestamp(3)
    assert _observed(third.join()) == _document_timestamp(3)
    assert [call.network for call in manager.calls] == [True, True, True]
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


class RunStartFailure:
    """Make chosen checkout run threads fail to start, as thread creation can."""

    def __init__(self) -> None:
        self.attempts = 0
        self._armed = 0

    def arm(self) -> None:
        """Fail the next checkout run thread start."""

        self._armed += 1

    def start(self, original: Any, thread: threading.Thread) -> None:
        if thread.name == "nyx-checkouts-run":
            self.attempts += 1
            if self._armed:
                self._armed -= 1
                raise RuntimeError("can't start new thread")
        original(thread)


@pytest.fixture
def run_start(monkeypatch):
    failure = RunStartFailure()
    original = threading.Thread.start
    monkeypatch.setattr(
        threading.Thread, "start", lambda thread: failure.start(original, thread)
    )
    return failure


def _wait_for_pending(runtime: ApplicationRuntime, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        with runtime._checkouts_condition:  # noqa: SLF001 - the serving owner's state
            if runtime._checkouts_pending is not None:  # noqa: SLF001
                return
        assert time.monotonic() < deadline, "no run was queued"
        time.sleep(0.001)


def test_a_first_run_that_cannot_start_answers_its_category_and_leaves_the_queue_usable(
    serving, run_start
):
    manager, clock = serving.manager, serving.clock

    run_start.arm()
    with pytest.raises(CatalogError) as error:
        _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert error.value.code == "checkouts_failed"
    _settle(serving.runtime)

    with pytest.raises(CatalogError) as gated:
        _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert gated.value.code == "checkouts_failed"

    # A run that cannot start records its own failed outcome, which the next
    # envelope reports in ``last``.
    clock.advance(REFRESH_INTERVAL)
    run_start.arm()
    with pytest.raises(CatalogError) as checked:
        _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert checked.value.code == "checkouts_failed"
    assert serving.runtime._checkouts_last_check["outcome"] == "checkouts_failed"  # noqa: SLF001
    assert run_start.attempts == 2
    assert manager.calls == []
    _settle(serving.runtime)

    clock.advance(REFRESH_INTERVAL)
    repaired = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(1)
    manager.release(0)
    assert _observed(repaired.join()) == _document_timestamp(1)
    _settle(serving.runtime)


def test_a_queued_run_that_cannot_start_answers_every_waiter(serving, run_start):
    manager, clock = serving.manager, serving.clock

    first = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(1)
    serving.runtime.mark_checkouts_changed()
    check = _Request(serving.runtime.get_checkouts).start()
    _wait_for_pending(serving.runtime)
    run_start.arm()
    manager.release(0)

    assert _observed(first.join(timeout=5)) == _document_timestamp(1)
    with pytest.raises(CatalogError) as error:
        check.join(timeout=5)
    assert error.value.code == "checkouts_failed"
    _settle(serving.runtime)

    with pytest.raises(CatalogError) as gated:
        _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert gated.value.code == "checkouts_failed"
    assert len(manager.calls) == 1

    clock.advance(REFRESH_INTERVAL)
    repaired = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    assert manager.calls[1].network is True
    manager.release(1)
    assert _observed(repaired.join()) == _document_timestamp(2)
    assert run_start.attempts == 3
    _settle(serving.runtime)


def test_a_run_that_cannot_start_behind_a_served_document_changes_only_its_clock(
    serving, run_start
):
    manager, clock = serving.manager, serving.clock
    observed = _establish_document(serving)

    clock.advance(REFRESH_INTERVAL)
    run_start.arm()
    envelope = _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert _observed(envelope) == observed
    assert envelope["remote_checks"]["last"]["outcome"] == "checkouts_failed"
    assert serving.runtime._checkouts_failure is None  # noqa: SLF001 - a current document exists

    clock.advance(REFRESH_INTERVAL)
    run_start.arm()
    behind = _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert _observed(behind) == observed
    assert run_start.attempts == 3
    _settle(serving.runtime)

    inside = _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert _observed(inside) == observed
    assert run_start.attempts == 3
    assert serving.runtime._checkouts_failure is None  # noqa: SLF001
    assert len(manager.calls) == 1

    clock.advance(REFRESH_INTERVAL)
    after = _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert _observed(after) == observed
    manager.wait_for_calls(2)
    assert manager.calls[1].network is True
    manager.release(1)
    _settle(serving.runtime)
    final = _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert _observed(final) == _document_timestamp(2)


def _write_record(home, specification, folders, *, github_lookup: bool = True):
    """Write the account record whose checkout inputs name ``folders``."""

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
        assert first["remote_checks"]["last"]["outcome"] == "completed"
        assert first["remote_checks"]["last"]["repositories"] == []
        document = models.parse_checkouts(first["checkouts"])
        [repository_document] = document["repositories"]
        assert repository_document["remote"] is None
        assert _main_heads(document) == [first_commit]

        second_commit = commit_file(repository, "file.txt", "two\n", "edit")
        clock.advance(REFRESH_INTERVAL)
        served = _Request(runtime.get_checkouts).start().join(timeout=5)
        assert _main_heads(models.parse_checkouts(served["checkouts"])) == [first_commit]

        _settle(runtime, timeout=30)
        refreshed = _Request(runtime.get_checkouts).start().join()
        assert _main_heads(models.parse_checkouts(refreshed["checkouts"])) == [second_commit]
    finally:
        manager.close_admission()
        assert manager.close(time.monotonic() + 30)


# ---------------------------------------------------------------------------
# The program `remote.origin.uploadpack` names
# ---------------------------------------------------------------------------


class UploadPackRecorder:
    """The transport program a clone's ``remote.origin.uploadpack`` names.

    Git runs it in place of ``git upload-pack``.  It records its process id and
    the arguments it was given, waits for a release file when the test asked it
    to hold, and then runs the real program with the same arguments, input,
    output and exit status.
    """

    def __init__(self, directory: Path, *, hold: bool = False) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.log = directory / "upload-pack.log"
        self.release = directory / "release"
        self.script = directory / "upload_pack.py"
        waiting = (
            f"while not os.path.exists({str(self.release)!r}):\n    time.sleep(0.01)\n"
            if hold
            else ""
        )
        self.script.write_text(
            '"""Record one transport call, then run the real upload-pack."""\n'
            + "import os\n"
            + "import sys\n"
            + ("import time\n" if hold else "")
            + f"with open({str(self.log)!r}, 'a', encoding='utf-8') as handle:\n"
            + '    handle.write(str(os.getpid()) + " " + " ".join(sys.argv[1:]) + "\\n")\n'
            + waiting
            + 'os.execvp("git", ["git", "upload-pack", *sys.argv[1:]])\n',
            encoding="utf-8",
        )

    @property
    def command(self) -> str:
        """The quoted interpreter and script a clone is configured with."""

        return f'"{sys.executable}" "{self.script}"'

    def install(self, *repositories: Path) -> None:
        for repository in repositories:
            git(repository, "config", "remote.origin.uploadpack", self.command)

    def lines(self) -> list[str]:
        try:
            text = self.log.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        return text.splitlines()

    def pids(self) -> list[int]:
        return [int(line.split(" ", 1)[0]) for line in self.lines()]

    def wait_for_calls(self, count: int = 1, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while len(self.lines()) < count:
            assert time.monotonic() < deadline, "the transport program did not run"
            time.sleep(0.005)


def _real_world(
    tmp_path: Path,
    monkeypatch,
    *,
    github_lookup: bool = False,
    hold: bool = False,
    separate_folders: bool = False,
    captured: bool = False,
) -> SimpleNamespace:
    """A runtime over a real worker manager and two clones of one bare remote.

    ``separate_folders`` clones them into two sibling folders and names only
    the first in the record, so a save can add the second.  ``captured`` loads
    that record into the runtime's settings boundary, so a save reaches the real
    bytes on disk.
    """

    home = tmp_path / "home"
    home.mkdir()
    isolate_git(monkeypatch, home)
    served = tmp_path / "served"
    served.mkdir()
    remote = make_remote(served)
    folder = tmp_path / "checkouts"
    folder.mkdir()
    if separate_folders:
        first_folder = folder / "a"
        second_folder = folder / "b"
        first_folder.mkdir()
        second_folder.mkdir()
    else:
        first_folder = second_folder = folder
    first = clone(remote.as_uri(), first_folder / "Clone A")
    second = clone(remote.as_uri(), second_folder / "Clone B")
    recorder = UploadPackRecorder(tmp_path / "recorder", hold=hold)
    recorder.install(first, second)
    specification = tmp_path / "specification"
    specification.mkdir()
    paths = _write_record(
        home, specification, [first_folder], github_lookup=github_lookup
    )
    manager = worker.CatalogWorkerManager(working_directory=paths.state_directory)
    clock = InjectedClock()
    runtime = ApplicationRuntime(
        port=0,
        deadline=time.monotonic() + 120,
        clock=clock,
        workers=manager,
    )
    if captured:
        runtime.capture_configuration(state.load_configuration(paths), paths)
    runtime.admit_catalog()
    return SimpleNamespace(
        home=home,
        remote=remote,
        folder=folder,
        folders=(first_folder, second_folder),
        first=first,
        second=second,
        recorder=recorder,
        manager=manager,
        runtime=runtime,
        clock=clock,
        paths=paths,
    )


def _close_world(world: SimpleNamespace) -> None:
    world.manager.close_admission()
    assert world.manager.close(time.monotonic() + 30)


def _fingerprints(*repositories: Path) -> dict[Path, dict[str, Any]]:
    """Each repository's references, configuration and object count."""

    return {repository: repository_fingerprint(repository) for repository in repositories}


def _saved_heads_text(key: str) -> str:
    """The timestamp a checkout document shows for the saved check of ``key``."""

    payload = state.read_checkout_cache()
    assert payload is not None
    heads = json.loads(payload)["remote_checks"][key]["heads"]
    return datetime.fromtimestamp(heads["at"], tz=timezone.utc).strftime(_FORMAT)


def _answer(method: Any, timeout: float = 60.0) -> dict[str, Any]:
    """Run one request on its own thread and return its envelope."""

    return _Request(method).start().join(timeout=timeout)


def test_a_real_check_serves_live_labels_and_the_next_run_rechecks_them(tmp_path, monkeypatch):
    world = _real_world(tmp_path, monkeypatch, hold=True)
    try:
        before = _fingerprints(world.first, world.second, world.remote)
        waiting = _Request(world.runtime.get_checkouts).start()
        world.recorder.wait_for_calls(1)
        assert not waiting.finished, "the first request answered before its check ran"
        world.recorder.release.touch()
        first = waiting.join(timeout=60)
        assert _fingerprints(world.first, world.second, world.remote) == before
        calls = len(world.recorder.lines())
        assert calls >= 1

        document = first["checkouts"]
        assert document["remote_checks"] == "on"
        [group] = document["repositories"]
        key = group["remote"]["key"]
        assert group["remote"]["check"] == {"status": "live", "reason": None}
        assert first["remote_checks"]["state"] == "idle"
        last = first["remote_checks"]["last"]
        assert last["outcome"] == "completed"
        [repository] = last["repositories"]
        assert repository["remote"] == key
        assert repository["check"] == {"status": "live", "reason": None}
        assert repository["pr_lookup"] == {"status": "off", "reason": "setting_off"}
        assert first["remote_checks"]["next_due_at"] == _timestamp_after(
            last["started_at"], REFRESH_INTERVAL
        )

        # The run after the interval is networked too: it asks the remote again
        # and keeps the live basis.
        world.clock.advance(REFRESH_INTERVAL)
        before = _fingerprints(world.first, world.second, world.remote)
        _answer(world.runtime.get_checkouts, timeout=5)
        _settle(world.runtime, timeout=90)
        assert _fingerprints(world.first, world.second, world.remote) == before
        assert len(world.recorder.lines()) > calls, "the run after the interval asked no remote"

        recorded = _answer(world.runtime.get_checkouts)
        assert recorded["checkouts"]["remote_checks"] == "on"
        [group] = recorded["checkouts"]["repositories"]
        assert group["remote"]["check"] == {"status": "live", "reason": None}
        assert recorded["remote_checks"]["last"]["outcome"] == "completed"
    finally:
        _close_world(world)


def test_a_real_check_after_a_push_shows_the_other_clone_behind_on_the_live_basis(
    tmp_path, monkeypatch
):
    world = _real_world(tmp_path, monkeypatch)
    try:
        commit_file(world.first, "file.txt", "second\n", "second")
        git(world.first, "push", "-q", "origin", "trunk")

        before = _fingerprints(world.first, world.second, world.remote)
        envelope = _answer(world.runtime.get_checkouts)
        assert _fingerprints(world.first, world.second, world.remote) == before

        document = envelope["checkouts"]
        assert document["remote_checks"] == "on"
        behind = find_clone(document, world.second)["local_default"]["vs_remote"]
        assert {
            field: behind[field] for field in ("basis", "relation", "ahead", "behind")
        } == {"basis": "live", "relation": "behind", "ahead": 0, "behind": 1}
        assert envelope["remote_checks"]["last"]["outcome"] == "completed"
        assert world.recorder.lines(), "the check did not contact the remote"
    finally:
        _close_world(world)


def test_a_real_failed_check_keeps_the_saved_record_and_its_reason_in_last(tmp_path, monkeypatch):
    world = _real_world(tmp_path, monkeypatch)
    try:
        first = _answer(world.runtime.get_checkouts)
        [group] = first["checkouts"]["repositories"]
        key = group["remote"]["key"]
        assert group["remote"]["check"] == {"status": "live", "reason": None}
        saved_at = _saved_heads_text(key)

        world.remote.rename(world.remote.with_name("remote.git.away"))
        # The due check that finds the remote gone keeps the saved record, and
        # the request after it reads the document it published.
        world.clock.advance(REFRESH_INTERVAL)
        before = _fingerprints(world.first, world.second)
        _answer(world.runtime.get_checkouts, timeout=5)
        _settle(world.runtime, timeout=90)
        assert _fingerprints(world.first, world.second) == before

        failed = _answer(world.runtime.get_checkouts)
        [group] = failed["checkouts"]["repositories"]
        check = group["remote"]["check"]
        assert check["status"] == "recorded"
        assert check["checked_at"] == saved_at
        reason = check["reason"]
        assert reason is not None and reason != "remote_checks_off"
        last = failed["remote_checks"]["last"]
        assert last["outcome"] == "completed"
        [repository] = last["repositories"]
        assert repository["check"] == {"status": "recorded", "reason": reason}

        world.clock.advance(REFRESH_INTERVAL)
        before = _fingerprints(world.first, world.second)
        _answer(world.runtime.get_checkouts, timeout=5)
        _settle(world.runtime, timeout=90)
        assert _fingerprints(world.first, world.second) == before
        again = _answer(world.runtime.get_checkouts)
        [group] = again["checkouts"]["repositories"]
        assert group["remote"]["check"] == {
            "status": "recorded",
            "reason": reason,
            "checked_at": saved_at,
        }
        [repository] = again["remote_checks"]["last"]["repositories"]
        assert repository["check"] == {"status": "recorded", "reason": reason}
    finally:
        _close_world(world)


@posix_only
def test_a_real_github_lookup_failure_reaches_the_last_check_report(tmp_path, monkeypatch):
    world = _real_world(tmp_path, monkeypatch, github_lookup=True)
    try:
        install_fake_gh(tmp_path, monkeypatch, stderr="not authenticated\n", code=4)
        point_origin_at(world.first, world.remote, "https://github.com/example/sample.git")
        point_origin_at(world.second, world.remote, "https://github.com/example/sample.git")

        envelope = _answer(world.runtime.get_checkouts)

        document = envelope["checkouts"]
        assert document["github_lookup"] == "on"
        assert document["remote_checks"] == "on"
        [group] = document["repositories"]
        assert group["remote"]["key"] == "github.com/example/sample"
        lookup = group["pr_lookup"]
        assert (lookup["status"], lookup["reason"]) == ("unavailable", "not_authenticated")
        [repository] = envelope["remote_checks"]["last"]["repositories"]
        assert repository["remote"] == "github.com/example/sample"
        assert repository["pr_lookup"] == {
            "status": "unavailable",
            "reason": "not_authenticated",
        }
        assert world.recorder.lines(), "the GitHub-style origin did not reach the bare remote"
    finally:
        _close_world(world)


# ---------------------------------------------------------------------------
# Saving the checkout inputs
# ---------------------------------------------------------------------------


def _document_clone_paths(document: dict[str, Any]) -> list[str]:
    """Every clone path the served document lists, in listing order."""

    return [
        clone_document["path"]
        for repository in document["repositories"]
        for clone_document in repository["clones"]
    ]


def _put_checkout_settings(port, revision, folders, github_lookup=True):
    return http_request(
        port,
        "PUT",
        "/api/checkout-settings",
        body=json.dumps(
            {
                "revision": revision,
                "folders": [str(folder) for folder in folders],
                "github_lookup": github_lookup,
            }
        ),
    )


def _put_settings(port, revision, order, completed):
    return http_request(
        port,
        "PUT",
        "/api/settings",
        body=json.dumps(
            {"revision": revision, "order": list(order), "completed": list(completed)}
        ),
    )


def test_a_saved_checkout_folder_marks_the_inputs_so_the_next_request_checks_it(
    tmp_path, monkeypatch
):
    """The mark, not the clock, makes the newly saved folder's clone appear."""

    world = _real_world(tmp_path, monkeypatch, separate_folders=True, captured=True)
    first_folder, second_folder = world.folders
    with RunningServer(StubClient(catalog=valid_catalog()), world.runtime) as port:
        status, _, body = http_request(port, "GET", "/api/checkouts")
        assert status == 200
        first = json.loads(body)
        assert first["remote_checks"]["last"]["outcome"] == "completed"
        document = models.parse_checkouts(first["checkouts"])
        assert document["folders"] == [{"path": str(first_folder), "status": "ok"}]
        assert _document_clone_paths(document) == [str(world.first)]
        recorded = len(world.recorder.lines())
        assert recorded >= 1
        revision = world.runtime.configuration.revision
        assert world.clock.value == 1000.0

        status, content_type, body = _put_checkout_settings(
            port, revision, [first_folder, second_folder], True
        )
        assert status == 200
        assert content_type == "application/json"
        saved = json.loads(body)
        assert saved["outcome"] == "success"
        assert saved["folders"] == [str(first_folder), str(second_folder)]
        assert saved["github_lookup"] is True
        assert saved["revision"] != revision

        # No interval has elapsed, so only the save's mark can make this check.
        status, _, body = http_request(port, "GET", "/api/checkouts")
        assert status == 200
        assert world.clock.value == 1000.0
        after = json.loads(body)
        document = models.parse_checkouts(after["checkouts"])
        assert document["folders"] == [
            {"path": str(first_folder), "status": "ok"},
            {"path": str(second_folder), "status": "ok"},
        ]
        assert _document_clone_paths(document) == sorted(
            [str(world.first), str(world.second)]
        )
        assert document["configuration_revision"] == saved["revision"]
        assert document["github_lookup"] == "on"
        assert len(world.recorder.lines()) == recorded + 1
        stored = json.loads(world.paths.config_file.read_bytes())
        key = str(world.runtime.configuration.specification_root)
        assert stored["checkout_settings"][key] == {
            "folders": [str(first_folder), str(second_folder)],
            "github_lookup": True,
        }

        # A board settings save marks the inputs too, so the read after it waits
        # for a run under the new settings with no interval elapsed.
        recorded = len(world.recorder.lines())
        status, _, body = _put_settings(port, saved["revision"], ["Shipped"], ["Shipped"])
        assert status == 200
        stage_saved = json.loads(body)
        assert stage_saved["outcome"] == "success"
        assert stage_saved["revision"] != saved["revision"]

        status, _, body = http_request(port, "GET", "/api/checkouts")
        assert status == 200
        assert world.clock.value == 1000.0
        after_stage = json.loads(body)
        document = models.parse_checkouts(after_stage["checkouts"])
        assert document["configuration_revision"] == stage_saved["revision"]
        assert document["folders"] == [
            {"path": str(first_folder), "status": "ok"},
            {"path": str(second_folder), "status": "ok"},
        ]
        assert len(world.recorder.lines()) == recorded + 1


def test_a_reconciled_checkout_settings_save_still_marks_the_inputs(tmp_path, monkeypatch):
    """A commit-verification failure the core reconciled to success still marks."""

    world = _real_world(tmp_path, monkeypatch, separate_folders=True, captured=True)
    first_folder, second_folder = world.folders
    with RunningServer(StubClient(catalog=valid_catalog()), world.runtime) as port:
        status, _, body = http_request(port, "GET", "/api/checkouts")
        assert status == 200
        assert json.loads(body)["remote_checks"]["last"]["outcome"] == "completed"
        recorded = len(world.recorder.lines())
        assert recorded >= 1
        revision = world.runtime.configuration.revision
        assert world.clock.value == 1000.0
        seen = state._verify_record
        fires = []

        def fail_once_after_replace(path, *args, **kwargs):
            details = seen(path, *args, **kwargs)
            if fires or path != world.paths.config_file:
                return details
            payload = json.loads(path.read_text(encoding="utf-8"))
            key = str(world.runtime.configuration.specification_root)
            entry = payload.get("checkout_settings", {}).get(key)
            if entry is None or entry.get("github_lookup") is not True:
                return details
            fires.append(path)
            raise OSError("injected post-replacement verification failure")

        with patch.object(state, "_verify_record", side_effect=fail_once_after_replace):
            # The submitted spelling normalizes to the stored folder, so the
            # reconciliation compares the normalized entry and reports success.
            status, _, body = _put_checkout_settings(
                port, revision, [str(second_folder) + os.sep + "."], True
            )
        assert fires == [world.paths.config_file]
        assert status == 200
        saved = json.loads(body)
        assert saved["outcome"] == "success"
        assert saved["folders"] == [str(second_folder)]
        assert saved["revision"] != revision

        status, _, body = http_request(port, "GET", "/api/checkouts")
        assert status == 200
        assert world.clock.value == 1000.0
        document = models.parse_checkouts(json.loads(body)["checkouts"])
        assert document["folders"] == [{"path": str(second_folder), "status": "ok"}]
        assert document["configuration_revision"] == saved["revision"]
        assert len(world.recorder.lines()) == recorded + 1


def test_a_saved_folder_removed_from_disk_still_saves_and_lists_as_missing(
    tmp_path, monkeypatch
):
    world = _real_world(tmp_path, monkeypatch, captured=True)
    kept = tmp_path / "kept"
    kept.mkdir()
    gone = tmp_path / "gone"
    gone.mkdir()
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    with RunningServer(StubClient(catalog=valid_catalog()), world.runtime) as port:
        status, _, body = http_request(port, "GET", "/api/checkouts")
        assert status == 200
        assert json.loads(body)["remote_checks"]["last"]["outcome"] == "completed"
        revision = world.runtime.configuration.revision

        status, _, body = _put_checkout_settings(port, revision, [kept, gone], True)
        assert status == 200
        saved = json.loads(body)
        assert saved["outcome"] == "success"
        gone.rmdir()
        assert not gone.exists()

        # The saved folder is exempt from the write-time existence check, so it
        # keeps saving while it is gone.
        status, _, body = _put_checkout_settings(
            port, saved["revision"], [kept, gone, fresh], True
        )
        assert status == 200
        again = json.loads(body)
        assert again["outcome"] == "success"
        assert again["folders"] == sorted([str(kept), str(gone), str(fresh)])

        status, _, body = http_request(port, "GET", "/api/checkouts")
        assert status == 200
        document = models.parse_checkouts(json.loads(body)["checkouts"])
        assert {folder["path"]: folder["status"] for folder in document["folders"]} == {
            str(kept): "ok",
            str(gone): "missing",
            str(fresh): "ok",
        }
        stored = json.loads(world.paths.config_file.read_bytes())
        key = str(world.runtime.configuration.specification_root)
        assert str(gone) in stored["checkout_settings"][key]["folders"]


# ---------------------------------------------------------------------------
# Cadence, the change count and the envelope on the double
# ---------------------------------------------------------------------------


def test_reads_inside_the_interval_serve_the_document_and_the_one_at_it_starts_a_run(serving):
    """A read one second before the interval starts nothing; the next one run."""

    runtime, manager, clock = serving.runtime, serving.manager, serving.clock
    observed = _establish_document(serving)

    elapsed = 0.0
    while elapsed + 10 <= REFRESH_INTERVAL - 1:
        clock.advance(10)
        elapsed += 10
        inside = _Request(runtime.get_checkouts).start().join(timeout=5)
        assert _observed(inside) == observed
        assert inside["remote_checks"]["state"] == "idle"
        assert inside["remote_checks"]["next_due_at"] is not None
        assert len(manager.calls) == 1, "a read inside the interval started a run"

    clock.advance(REFRESH_INTERVAL - 1 - elapsed)
    assert _observed(_Request(runtime.get_checkouts).start().join(timeout=5)) == observed
    assert len(manager.calls) == 1, "the read before the interval started a run"

    clock.advance(1)
    at_interval = _Request(runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    assert manager.calls[1].network is True
    assert _observed(at_interval.join(timeout=5)) == observed
    manager.release(1)
    _settle(runtime)

    served = _Request(runtime.get_checkouts).start().join(timeout=5)
    assert _observed(served) == _document_timestamp(2)
    assert len(manager.calls) == 2, "a second run followed the one at the interval"


def test_an_idle_runtime_starts_nothing_and_its_next_request_checks(serving):
    """Ten intervals with no request start no run and leave no thread."""

    runtime, manager, clock = serving.runtime, serving.manager, serving.clock
    _establish_document(serving)
    _settle(runtime)
    calls = len(manager.calls)

    for _ in range(10):
        clock.advance(REFRESH_INTERVAL)
        assert len(manager.calls) == calls, "an unrequested runtime collected"
        assert _threads_outside(serving.threads_before) == [], (
            "a thread outside the ones alive before the runtime appeared"
        )

    served = _Request(runtime.get_checkouts).start().join(timeout=5)
    manager.wait_for_calls(calls + 1)
    assert manager.calls[calls].network is True
    assert served["remote_checks"]["state"] == "running"
    manager.release(calls)
    _settle(runtime)


def test_the_envelope_reports_a_due_check_in_flight_and_an_overdue_one(serving):
    runtime, manager, clock = serving.runtime, serving.manager, serving.clock
    _establish_document(serving)

    settled = _Request(runtime.get_checkouts).start().join(timeout=5)
    assert settled["remote_checks"]["state"] == "idle"
    assert settled["remote_checks"]["interval_seconds"] == REFRESH_INTERVAL
    last = settled["remote_checks"]["last"]
    due_at = _timestamp_after(last["started_at"], REFRESH_INTERVAL)
    assert settled["remote_checks"]["next_due_at"] == due_at

    # The request that finds the check due starts it and answers behind it, so
    # its envelope reports the check while it is in flight.
    clock.advance(REFRESH_INTERVAL)
    in_flight = _Request(runtime.get_checkouts).start().join(timeout=5)
    manager.wait_for_calls(2)
    assert manager.calls[1].network is True
    assert in_flight["remote_checks"]["state"] == "running"
    assert in_flight["remote_checks"]["next_due_at"] is not None

    # Once its interval has passed again while it still runs, no next due time
    # is stated.
    clock.advance(REFRESH_INTERVAL)
    overdue = _Request(runtime.get_checkouts).start().join(timeout=5)
    assert overdue["remote_checks"]["state"] == "running"
    assert overdue["remote_checks"]["next_due_at"] is None

    manager.release(1)
    _settle(runtime)
    assert runtime._checkouts_last_check["outcome"] == "completed"  # noqa: SLF001


def test_three_concurrent_requests_share_the_one_run_queued_behind_a_held_run(serving):
    runtime, manager, clock = serving.runtime, serving.manager, serving.clock
    _establish_document(serving)

    clock.advance(REFRESH_INTERVAL)
    _Request(runtime.get_checkouts).start().join(timeout=5)
    manager.wait_for_calls(2)
    assert manager.calls[1].network is True

    # The change mark during the held check queues the one run the three
    # requests that arrive together share.
    runtime.mark_checkouts_changed()
    arrived = threading.Barrier(4)

    def shared():
        arrived.wait()
        return runtime.get_checkouts()

    requests = [_Request(shared).start() for _ in range(3)]
    arrived.wait()
    _wait_for_pending(runtime)
    assert len(manager.calls) == 2, "a queued request started its own check"

    manager.release(1)
    manager.wait_for_calls(3)
    assert manager.calls[2].network is True
    manager.release(2)

    answers = [request.join(timeout=10) for request in requests]
    assert {_observed(answer) for answer in answers} == {_document_timestamp(3)}
    assert len(manager.calls) == 3, "a second check served one of the requests"

    served = _Request(runtime.get_checkouts).start().join(timeout=5)
    assert _observed(served) == _document_timestamp(3)
    assert len(manager.calls) == 3, "a further run followed the shared one"
    _settle(runtime)


def test_a_mark_during_a_remote_check_keeps_its_answer_and_supersedes_the_document(serving):
    runtime, manager = serving.runtime, serving.manager
    _establish_document(serving)

    # The first mark makes the next request wait for a networked run, and the
    # second arrives while that run is in flight.
    runtime.mark_checkouts_changed()
    check = _Request(runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    assert manager.calls[1].network is True

    runtime.mark_checkouts_changed()
    manager.release(1)

    answer = check.join(timeout=10)
    assert _observed(answer) == _document_timestamp(2)
    assert answer["remote_checks"]["last"]["outcome"] == "completed"

    after_mark = _Request(runtime.get_checkouts).start()
    manager.wait_for_calls(3)
    assert manager.calls[2].network is True
    assert not after_mark.finished
    manager.release(2)
    assert _observed(after_mark.join(timeout=10)) == _document_timestamp(3)
    assert len(manager.calls) == 3
    _settle(runtime)


def test_a_failing_check_after_a_mark_gates_get_requests(serving):
    runtime, manager = serving.runtime, serving.manager
    _establish_document(serving)

    runtime.mark_checkouts_changed()
    manager.next_outcome("worker:checkouts_timeout")
    waiting = _Request(runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    assert manager.calls[1].network is True
    manager.release(1)

    with pytest.raises(CatalogError) as error:
        waiting.join(timeout=10)
    assert error.value.code == "checkouts_timeout"
    assert runtime._checkouts_last_check["outcome"] == "checkouts_timeout"  # noqa: SLF001
    assert runtime._checkouts_last_check["repositories"] == []  # noqa: SLF001

    for _ in range(2):
        with pytest.raises(CatalogError) as gated:
            _Request(runtime.get_checkouts).start().join(timeout=5)
        assert gated.value.code == "checkouts_timeout"
    assert len(manager.calls) == 2
    _settle(runtime)


# ---------------------------------------------------------------------------
# Shutdown and start-failure cleanup
# ---------------------------------------------------------------------------


def test_shutdown_cancels_the_held_and_queued_runs_their_waiters_share(serving):
    runtime, manager = serving.runtime, serving.manager

    first = _Request(runtime.get_checkouts).start()
    manager.wait_for_calls(1)
    runtime.mark_checkouts_changed()
    queued = _Request(runtime.get_checkouts).start()
    _wait_for_pending(runtime)

    assert runtime.shutdown(time.monotonic() + 5) is True

    for waiter in (first, queued):
        with pytest.raises(CatalogError) as error:
            waiter.join(timeout=5)
        assert error.value.code == "checkouts_cancelled"
    assert len(manager.calls) == 1, "the queued check started"
    with runtime._checkouts_condition:  # noqa: SLF001 - the serving owner's state
        assert not runtime._checkouts_threads  # noqa: SLF001 - the run thread is joined


def test_start_failure_cleanup_cancels_the_same_waiters(serving):
    runtime, manager = serving.runtime, serving.manager

    first = _Request(runtime.get_checkouts).start()
    manager.wait_for_calls(1)
    runtime.mark_checkouts_changed()
    queued = _Request(runtime.get_checkouts).start()
    _wait_for_pending(runtime)

    assert runtime.cleanup_start_failure(time.monotonic() + 5) is True

    for waiter in (first, queued):
        with pytest.raises(CatalogError) as error:
            waiter.join(timeout=5)
        assert error.value.code == "checkouts_cancelled"
    assert len(manager.calls) == 1


@pytest.mark.parametrize("cleanup", ["shutdown", "cleanup_start_failure"])
def test_cleanup_drops_a_queued_run_whose_held_one_ignores_cancellation(serving, cleanup):
    runtime, manager = serving.runtime, serving.manager
    manager.ignore_cancellation = True

    first = _Request(runtime.get_checkouts).start()
    manager.wait_for_calls(1)
    runtime.mark_checkouts_changed()
    queued = _Request(runtime.get_checkouts).start()
    _wait_for_pending(runtime)

    assert getattr(runtime, cleanup)(time.monotonic() + 0.3) is False

    with pytest.raises(CatalogError) as error:
        queued.join(timeout=5)
    assert error.value.code == "checkouts_cancelled"
    assert len(manager.calls) == 1, "the queued check started"

    manager.release(0)
    first.join(timeout=5)


def test_a_request_after_shutdown_begins_starts_nothing(serving):
    runtime, manager = serving.runtime, serving.manager
    _establish_document(serving)
    calls = len(manager.calls)

    assert runtime.shutdown(time.monotonic() + 5) is True
    with pytest.raises(CatalogError):
        _Request(runtime.get_checkouts).start().join(timeout=5)
    assert len(manager.calls) == calls

    # A request that read admission just before it closed still finds the queue
    # closed: it starts nothing and is told the work was cancelled.
    runtime.catalog_admitted = True
    with pytest.raises(CatalogError) as error:
        runtime.get_checkouts()
    assert error.value.code == "checkouts_cancelled"
    assert len(manager.calls) == calls


def test_shutdown_cancels_a_held_refresh_without_publishing_it(serving, captured_checkouts):
    runtime, manager, clock = serving.runtime, serving.manager, serving.clock
    observed = _establish_document(serving)

    clock.advance(REFRESH_INTERVAL)
    _Request(runtime.get_checkouts).start().join(timeout=5)
    manager.wait_for_calls(2)
    assert manager.calls[1].network is True

    assert runtime.shutdown(time.monotonic() + 5) is True
    with runtime._checkouts_condition:  # noqa: SLF001 - the serving owner's state
        assert not runtime._checkouts_threads  # noqa: SLF001 - the run thread is joined
    assert runtime._checkouts_latest["observed_at"] == observed  # noqa: SLF001

    fresh_manager = CheckoutsManagerDouble(captured_checkouts)
    fresh = ApplicationRuntime(
        port=0,
        deadline=time.monotonic() + 60,
        clock=InjectedClock(),
        workers=fresh_manager,
    )
    fresh.admit_catalog()
    try:
        waiting = _Request(fresh.get_checkouts).start()
        fresh_manager.wait_for_calls(1)
        assert not waiting.finished, "a new runtime served a document it never collected"
        fresh_manager.release(0)
        assert _observed(waiting.join(timeout=5)) == _document_timestamp(1)
    finally:
        assert fresh.shutdown(time.monotonic() + 5) is True


def test_shutdown_reports_an_unjoined_run_and_a_retry_joins_it(serving):
    runtime, manager = serving.runtime, serving.manager
    manager.ignore_cancellation = True

    waiting = _Request(runtime.get_checkouts).start()
    manager.wait_for_calls(1)
    with runtime._checkouts_condition:  # noqa: SLF001 - the serving owner's state
        threads = tuple(runtime._checkouts_threads)  # noqa: SLF001
    assert threads

    deadline = time.monotonic() + 0.5
    assert runtime.shutdown(deadline) is False
    assert time.monotonic() >= deadline
    assert all(thread.is_alive() for thread in threads)

    manager.release(0)
    assert runtime.shutdown(time.monotonic() + 5) is True
    waiting.join(timeout=5)


def test_the_checkouts_route_answers_a_cancelled_provider_with_502():
    def cancelled():
        raise CatalogError("checkouts_cancelled")

    application = ApplicationRuntime(port=0, deadline=time.monotonic() + 5)
    application.get_checkouts = cancelled

    with RunningServer(StubClient(catalog=valid_catalog()), application) as port:
        status, content_type, body = http_request(port, "GET", "/api/checkouts")
        assert (status, content_type) == (502, "application/json")
        assert json.loads(body) == {"error": "checkouts_cancelled"}


def _wait_for_process_exit(pid: int, timeout: float = 5.0) -> None:
    """Wait for ``pid`` to stop existing, allowing a moment for reaping."""

    deadline = time.monotonic() + timeout
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        assert time.monotonic() < deadline, f"the held transport program {pid} still exists"
        time.sleep(0.01)


def test_shutdown_cancels_a_real_check_held_in_the_transport(tmp_path, monkeypatch):
    world = _real_world(tmp_path, monkeypatch, hold=True)
    try:
        waiting = _Request(world.runtime.get_checkouts).start()
        world.recorder.wait_for_calls(1)
        assert not waiting.finished
        held = world.recorder.pids()

        started = time.monotonic()
        assert world.runtime.shutdown(started + 5) is True
        assert time.monotonic() - started < 5

        with pytest.raises(CatalogError) as error:
            waiting.join(timeout=5)
        assert error.value.code == "checkouts_cancelled"
        assert world.runtime.workers.active_count == 0
        assert world.runtime._checkouts_last_check is None  # noqa: SLF001 - nothing completed
        assert world.runtime._checkouts_latest is None  # noqa: SLF001 - nothing published
        if os.name != "nt":
            for pid in held:
                _wait_for_process_exit(pid)
    finally:
        _close_world(world)


def test_a_request_that_read_admission_before_the_close_starts_no_run(serving, monkeypatch):
    runtime, manager = serving.runtime, serving.manager
    _establish_document(serving)
    assert runtime.shutdown(time.monotonic() + 5) is True

    def refuse(run: object) -> None:
        raise AssertionError("a run was started after the queue closed")

    monkeypatch.setattr(runtime, "_start_checkouts_run", refuse)
    runtime.catalog_admitted = True
    with pytest.raises(CatalogError) as error:
        runtime.get_checkouts()
    assert error.value.code == "checkouts_cancelled"
    assert len(manager.calls) == 1


def test_the_queue_closes_before_an_unfinished_settings_wait_returns(serving):
    runtime, manager = serving.runtime, serving.manager
    first = _Request(runtime.get_checkouts).start()
    manager.wait_for_calls(1)
    runtime.mark_checkouts_changed()
    queued = _Request(runtime.get_checkouts).start()
    _wait_for_pending(runtime)

    with runtime._settings_condition:  # noqa: SLF001 - a settings operation still active
        runtime._settings_active += 1  # noqa: SLF001
    try:
        assert runtime.shutdown(time.monotonic() + 0.2) is False
        with pytest.raises(CatalogError) as error:
            queued.join(timeout=5)
        assert error.value.code == "checkouts_cancelled"
        assert len(manager.calls) == 1, "the queued check started"
    finally:
        with runtime._settings_condition:  # noqa: SLF001
            runtime._settings_active -= 1  # noqa: SLF001
    # The settings wait returned first, so the worker admission was never closed
    # and the held run still answers its own waiter.
    manager.release(0)
    first.join(timeout=5)
    assert runtime.shutdown(time.monotonic() + 5) is True
