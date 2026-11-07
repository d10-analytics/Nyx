"""Behavioral proofs for serving the checkout document from one serial run queue.

The serving state machine runs on a manager double and an injected monotonic
clock, so no proof spends a real interval or starts a worker: the cadence, the
change count, the failure gate, the remote-check entry point and the shutdown
rules are all read from that double.  Real workers stay where they alone prove
something: a first request's document is accepted by the parser, a check's live
labels become a recorded label after the next local refresh, a push from one
clone is answered against the other, a failed check keeps the saved record, a
GitHub lookup failure reaches the last check's report, no service run writes a
repository reference, and a real held check is cancelled at shutdown.  The
transport is recorded by the program ``remote.origin.uploadpack`` names.
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

import pytest
from test_checkouts import (
    clone,
    commit_file,
    find_clone,
    git,
    install_fake_gh,
    isolate_git,
    make_remote,
    point_origin_at,
    repository_fingerprint,
)
from test_server import RunningServer, StubClient, valid_catalog
from test_server import request as http_request

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

    run_start.arm()
    with pytest.raises(CatalogError) as checked:
        _Request(serving.runtime.refresh_remote_checks).start().join(timeout=5)
    assert checked.value.code == "checkouts_failed"
    assert serving.runtime._checkouts_last_check["outcome"] == "checkouts_failed"  # noqa: SLF001
    assert run_start.attempts == 2
    assert manager.calls == []
    _settle(serving.runtime)

    clock.advance(LOCAL_REFRESH_INTERVAL)
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
    check = _Request(serving.runtime.refresh_remote_checks).start()
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

    clock.advance(LOCAL_REFRESH_INTERVAL)
    repaired = _Request(serving.runtime.get_checkouts).start()
    manager.wait_for_calls(2)
    assert manager.calls[1].network is False
    manager.release(1)
    assert _observed(repaired.join()) == _document_timestamp(2)
    assert run_start.attempts == 3
    _settle(serving.runtime)


def test_a_run_that_cannot_start_behind_a_served_document_changes_only_its_clocks(
    serving, run_start
):
    manager, clock = serving.manager, serving.clock
    observed = _establish_document(serving)

    run_start.arm()
    envelope = _Request(serving.runtime.refresh_remote_checks).start().join(timeout=5)
    assert _observed(envelope) == observed
    assert envelope["remote_checks"]["last"]["outcome"] == "checkouts_failed"
    assert serving.runtime._checkouts_failure is None  # noqa: SLF001 - a current document exists

    clock.advance(LOCAL_REFRESH_INTERVAL)
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

    clock.advance(LOCAL_REFRESH_INTERVAL)
    after = _Request(serving.runtime.get_checkouts).start().join(timeout=5)
    assert _observed(after) == observed
    manager.wait_for_calls(2)
    assert manager.calls[1].network is False
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

    def let_through(self) -> None:
        """Release the transport the recorder is holding."""

        self.release.write_bytes(b"")


def _real_world(
    tmp_path: Path,
    monkeypatch,
    *,
    github_lookup: bool = False,
    hold: bool = False,
) -> SimpleNamespace:
    """A runtime over a real worker manager and two clones of one bare remote."""

    home = tmp_path / "home"
    home.mkdir()
    isolate_git(monkeypatch, home)
    served = tmp_path / "served"
    served.mkdir()
    remote = make_remote(served)
    folder = tmp_path / "checkouts"
    folder.mkdir()
    first = clone(remote.as_uri(), folder / "Clone A")
    second = clone(remote.as_uri(), folder / "Clone B")
    recorder = UploadPackRecorder(tmp_path / "recorder", hold=hold)
    recorder.install(first, second)
    specification = tmp_path / "specification"
    specification.mkdir()
    paths = _write_record(home, specification, [folder], github_lookup=github_lookup)
    manager = worker.CatalogWorkerManager(working_directory=paths.state_directory)
    clock = InjectedClock()
    runtime = ApplicationRuntime(
        port=0,
        deadline=time.monotonic() + 120,
        clock=clock,
        workers=manager,
    )
    runtime.admit_catalog()
    return SimpleNamespace(
        home=home,
        remote=remote,
        folder=folder,
        first=first,
        second=second,
        recorder=recorder,
        manager=manager,
        runtime=runtime,
        clock=clock,
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


def test_a_real_check_serves_live_labels_and_the_local_refresh_records_them(tmp_path, monkeypatch):
    world = _real_world(tmp_path, monkeypatch)
    try:
        before = _fingerprints(world.first, world.second, world.remote)
        waiting = _Request(world.runtime.get_checkouts).start()
        world.recorder.wait_for_calls(1)
        assert not waiting.finished, "the first request answered before its check ran"
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
            last["started_at"], REMOTE_CHECK_INTERVAL
        )
        saved_at = _saved_heads_text(key)

        world.clock.advance(LOCAL_REFRESH_INTERVAL)
        before = _fingerprints(world.first, world.second, world.remote)
        _answer(world.runtime.get_checkouts, timeout=5)
        _settle(world.runtime, timeout=90)
        assert _fingerprints(world.first, world.second, world.remote) == before
        assert len(world.recorder.lines()) == calls, "a local run contacted the remote"

        recorded = _answer(world.runtime.get_checkouts)
        assert recorded["checkouts"]["remote_checks"] == "off"
        [group] = recorded["checkouts"]["repositories"]
        assert group["remote"]["check"] == {
            "status": "recorded",
            "reason": "remote_checks_off",
            "checked_at": saved_at,
        }
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
        envelope = _answer(world.runtime.refresh_remote_checks)
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
        before = _fingerprints(world.first, world.second)
        failed = _answer(world.runtime.refresh_remote_checks)
        assert _fingerprints(world.first, world.second) == before

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

        world.clock.advance(LOCAL_REFRESH_INTERVAL)
        _answer(world.runtime.get_checkouts, timeout=5)
        _settle(world.runtime, timeout=90)
        local = _answer(world.runtime.get_checkouts)
        [group] = local["checkouts"]["repositories"]
        assert group["remote"]["check"] == {
            "status": "recorded",
            "reason": "remote_checks_off",
            "checked_at": saved_at,
        }
        [repository] = local["remote_checks"]["last"]["repositories"]
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

        envelope = _answer(world.runtime.refresh_remote_checks)

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
# Cadence, the change count and the remote-check entry point on the double
# ---------------------------------------------------------------------------


def test_local_refreshes_serve_the_interval_until_the_check_becomes_the_refresh(serving):
    runtime, manager, clock = serving.runtime, serving.manager, serving.clock
    _establish_document(serving)

    for count in range(2, 6):
        clock.advance(LOCAL_REFRESH_INTERVAL)
        _Request(runtime.get_checkouts).start().join(timeout=5)
        manager.wait_for_calls(count)
        assert manager.calls[count - 1].network is False
        manager.release(count - 1)
        _settle(runtime)
    assert [call.network for call in manager.calls] == [True, False, False, False, False]

    clock.advance(LOCAL_REFRESH_INTERVAL)
    _Request(runtime.get_checkouts).start().join(timeout=5)
    manager.wait_for_calls(6)
    assert manager.calls[5].network is True
    manager.release(5)
    _settle(runtime)

    served = _Request(runtime.get_checkouts).start().join(timeout=5)
    assert len(manager.calls) == 6, "a local run followed the due check"
    assert served["remote_checks"]["state"] == "idle"
    assert served["remote_checks"]["last"]["outcome"] == "completed"


def test_an_idle_runtime_starts_nothing_and_its_next_request_checks(serving):
    runtime, manager, clock = serving.runtime, serving.manager, serving.clock
    _establish_document(serving)
    calls = len(manager.calls)

    clock.advance(REMOTE_CHECK_INTERVAL * 3)

    assert len(manager.calls) == calls
    with runtime._checkouts_condition:  # noqa: SLF001 - the serving owner's state
        assert not runtime._checkouts_threads  # noqa: SLF001 - no run is alive
    served = _Request(runtime.get_checkouts).start().join(timeout=5)
    manager.wait_for_calls(calls + 1)
    assert manager.calls[calls].network is True
    assert served["remote_checks"]["state"] == "running"
    manager.release(calls)
    _settle(runtime)


def test_the_envelope_reports_a_pending_check_and_an_overdue_in_flight_one(serving):
    runtime, manager, clock = serving.runtime, serving.manager, serving.clock
    _establish_document(serving)

    settled = _Request(runtime.get_checkouts).start().join(timeout=5)
    assert settled["remote_checks"]["state"] == "idle"
    last = settled["remote_checks"]["last"]
    due_at = _timestamp_after(last["started_at"], REMOTE_CHECK_INTERVAL)
    assert settled["remote_checks"]["next_due_at"] == due_at

    clock.advance(LOCAL_REFRESH_INTERVAL)
    _Request(runtime.get_checkouts).start().join(timeout=5)
    manager.wait_for_calls(2)
    assert manager.calls[1].network is False
    queued = _Request(runtime.refresh_remote_checks).start()
    _wait_for_pending(runtime)
    pending = _Request(runtime.get_checkouts).start().join(timeout=5)
    assert pending["remote_checks"]["state"] == "running"
    assert pending["remote_checks"]["next_due_at"] == due_at

    manager.release(1)
    manager.wait_for_calls(3)
    assert manager.calls[2].network is True
    clock.advance(REMOTE_CHECK_INTERVAL)
    overdue = _Request(runtime.get_checkouts).start().join(timeout=5)
    assert overdue["remote_checks"]["state"] == "running"
    assert overdue["remote_checks"]["next_due_at"] is None

    manager.release(2)
    _settle(runtime)
    assert queued.join(timeout=5)["remote_checks"]["last"]["outcome"] == "completed"


def test_three_remote_check_requests_share_the_one_check_queued_behind_a_held_run(serving):
    runtime, manager, clock = serving.runtime, serving.manager, serving.clock
    _establish_document(serving)

    clock.advance(LOCAL_REFRESH_INTERVAL)
    _Request(runtime.get_checkouts).start().join(timeout=5)
    manager.wait_for_calls(2)
    assert manager.calls[1].network is False

    arrived = threading.Barrier(4)

    def remote_check():
        arrived.wait()
        return runtime.refresh_remote_checks()

    posts = [_Request(remote_check).start() for _ in range(3)]
    arrived.wait()
    _wait_for_pending(runtime)
    assert len(manager.calls) == 2

    manager.release(1)
    manager.wait_for_calls(3)
    assert manager.calls[2].network is True
    manager.release(2)

    answers = [post.join(timeout=10) for post in posts]
    assert {_observed(answer) for answer in answers} == {_document_timestamp(3)}
    assert len(manager.calls) == 3, "a second check served one of the requests"

    served = _Request(runtime.get_checkouts).start().join(timeout=5)
    assert _observed(served) == _document_timestamp(3)
    assert len(manager.calls) == 3, "a local run followed the check"
    _settle(runtime)


def test_a_mark_during_a_remote_check_keeps_its_answer_and_supersedes_the_document(serving):
    runtime, manager, clock = serving.runtime, serving.manager, serving.clock
    _establish_document(serving)

    clock.advance(LOCAL_REFRESH_INTERVAL)
    _Request(runtime.get_checkouts).start().join(timeout=5)
    manager.wait_for_calls(2)
    assert manager.calls[1].network is False
    post = _Request(runtime.refresh_remote_checks).start()
    _wait_for_pending(runtime)
    manager.release(1)
    manager.wait_for_calls(3)
    assert manager.calls[2].network is True

    runtime.mark_checkouts_changed()
    manager.release(2)

    answer = post.join(timeout=10)
    assert _observed(answer) == _document_timestamp(3)
    assert answer["remote_checks"]["last"]["outcome"] == "completed"

    after_mark = _Request(runtime.get_checkouts).start()
    manager.wait_for_calls(4)
    assert manager.calls[3].network is True
    assert not after_mark.finished
    manager.release(3)
    assert _observed(after_mark.join(timeout=10)) == _document_timestamp(4)
    assert len(manager.calls) == 4
    _settle(runtime)


def test_a_failing_check_after_a_mark_gates_get_requests_but_a_post_still_checks(serving):
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

    post = _Request(runtime.refresh_remote_checks).start()
    manager.wait_for_calls(3)
    assert manager.calls[2].network is True
    shared = _Request(runtime.get_checkouts).start()
    assert len(manager.calls) == 3, "the gated request started its own run"

    manager.release(2)
    posted = post.join(timeout=10)
    shared_answer = shared.join(timeout=10)
    assert _observed(posted) == _document_timestamp(2)
    assert _observed(shared_answer) == _document_timestamp(2)
    _settle(runtime)


def test_the_remote_check_route_rejects_a_wrong_payload_without_queuing_a_run(serving):
    manager = serving.manager

    with RunningServer(StubClient(catalog=valid_catalog()), serving.runtime) as port:
        status, content_type, body = http_request(
            port,
            "POST",
            "/api/checkouts/remote-check",
            body="{}",
            content_type="text/plain",
        )
        assert (status, content_type) == (400, "application/json")
        assert json.loads(body) == {"error": "invalid_payload"}
        assert http_request(port, "POST", "/api/checkouts/remote-check", body="[]")[0] == 400
        assert http_request(port, "POST", "/api/checkouts/remote-check", body="")[0] == 400

    assert manager.calls == []


# ---------------------------------------------------------------------------
# Shutdown and start-failure cleanup
# ---------------------------------------------------------------------------


def test_shutdown_cancels_the_held_and_queued_runs_their_waiters_share(serving):
    runtime, manager = serving.runtime, serving.manager

    first = _Request(runtime.get_checkouts).start()
    manager.wait_for_calls(1)
    queued = _Request(runtime.refresh_remote_checks).start()
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
    queued = _Request(runtime.refresh_remote_checks).start()
    _wait_for_pending(runtime)

    assert runtime.cleanup_start_failure(time.monotonic() + 5) is True

    for waiter in (first, queued):
        with pytest.raises(CatalogError) as error:
            waiter.join(timeout=5)
        assert error.value.code == "checkouts_cancelled"
    assert len(manager.calls) == 1


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
    for method in (runtime.get_checkouts, runtime.refresh_remote_checks):
        with pytest.raises(CatalogError) as error:
            method()
        assert error.value.code == "checkouts_cancelled"
    assert len(manager.calls) == calls


def test_shutdown_cancels_a_held_local_refresh_without_publishing_it(serving, captured_checkouts):
    runtime, manager, clock = serving.runtime, serving.manager, serving.clock
    observed = _establish_document(serving)

    clock.advance(LOCAL_REFRESH_INTERVAL)
    _Request(runtime.get_checkouts).start().join(timeout=5)
    manager.wait_for_calls(2)
    assert manager.calls[1].network is False

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


def test_the_checkout_routes_answer_a_cancelled_provider_with_502():
    def cancelled():
        raise CatalogError("checkouts_cancelled")

    application = ApplicationRuntime(port=0, deadline=time.monotonic() + 5)
    application.get_checkouts = cancelled
    application.refresh_remote_checks = cancelled

    with RunningServer(StubClient(catalog=valid_catalog()), application) as port:
        status, content_type, body = http_request(port, "GET", "/api/checkouts")
        assert (status, content_type) == (502, "application/json")
        assert json.loads(body) == {"error": "checkouts_cancelled"}

        status, content_type, body = http_request(
            port, "POST", "/api/checkouts/remote-check", body="{}"
        )
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
        waiting = _Request(world.runtime.refresh_remote_checks).start()
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


