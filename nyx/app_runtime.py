"""Shared application HTTP, catalog admission, checkout serving, and worker lifecycle.

The application answers checkout requests from the document it last published
and refreshes that answer behind the request.  One serial run queue keeps a
single collection running and at most one waiting; a request without a current
document is answered by the run it shares or queues, while a request served from
the published document starts one background refresh only after the local
refresh interval has passed.  A networked check runs when its interval is due or
when the remote-check entry point asks for one.  A run that fails with no
current document answers its waiters with its category and holds further
requests for one refresh interval, and a configuration change announced through
``mark_checkouts_changed`` keeps a run that started before it from replacing the
served document.  Shutdown and start-failure cleanup close the queue first, so
no run starts once cleanup begins: a queued run's waiters are answered as
cancelled, and every started run thread is joined within the same deadline.

The active workspace's checkout settings are read and saved beside the board
settings through one compare-and-save core: the revision compare, the caller's
content check, the replacement, and the commit-verification reconciliation.  A
successful checkout settings save replaces the record, updates the captured
configuration and then marks the checkout inputs changed, so the next request
collects the new folders rather than answering from a document collected
before them.
"""

from __future__ import annotations

import errno
import http.client
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from . import state
from .models import Catalog, ProtocolError, _safe_text, parse_catalog, parse_checkouts
from .server import (
    _CHECKOUTS_ERRORS,
    CatalogError,
    InvalidCheckoutFolders,
    TrackerServer,
    _create_application_server,
)
from .worker import CatalogWorkerManager, WorkerError

# A published document is refreshed in the background at most once per local
# interval and a networked check is due once per remote interval.
LOCAL_REFRESH_INTERVAL = 60.0
REMOTE_CHECK_INTERVAL = 300.0
_CHECK = "check"
_LOCAL = "local"

Deadline = float | Callable[[], float]
ServerFactory = Callable[..., TrackerServer]
ThreadFactory = Callable[..., threading.Thread]


def _checkout_settings_request(
    current: state.Configuration, folders: object, github_lookup: object
) -> tuple[tuple[str, ...], bool]:
    """Validate one submitted checkout settings request.

    The request's shape is checked first, then every folder in submitted order:
    text the rendered document can carry, then absoluteness, then normalization.
    A normalized folder that is not already in the saved entry must answer the
    collector's one folder status with ``ok``; a folder already saved is exempt,
    so it keeps saving while its drive is unmounted and still lists as missing.
    Duplicates collapse and the stored order is canonical.  Any problem refuses
    the whole request with its submitted indexes.
    """

    if type(github_lookup) is not bool:
        raise ValueError("checkout GitHub lookup must be a boolean")
    if not isinstance(folders, list):
        raise ValueError("checkout folders must be a list")
    from .checkouts import folder_status

    saved = set(current.checkout_folders)
    stored: set[str] = set()
    problems: list[dict[str, object]] = []
    for index, folder in enumerate(folders):
        if not _safe_text(folder):
            problems.append({"index": index, "reason": "invalid_path"})
            continue
        if not os.path.isabs(folder):
            problems.append({"index": index, "reason": "not_absolute"})
            continue
        path = os.path.normpath(folder)
        if path not in saved:
            status = folder_status(path)
            if status != "ok":
                problems.append({"index": index, "reason": status})
                continue
        stored.add(path)
    if problems:
        raise InvalidCheckoutFolders(problems)
    if not stored:
        return (), False
    return tuple(sorted(stored)), github_lookup


def _utc_timestamp(instant: float) -> str:
    """Return ``instant`` as the UTC ISO 8601 text the served documents use."""

    return datetime.fromtimestamp(instant, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _CheckoutsRun:
    """One checkout collection the application has started or queued.

    A queued run decides its kind, change count and start instant only when it
    starts, so a check requested while it waits is still honoured.
    """

    def __init__(self) -> None:
        self.kind: str | None = None
        self.change = 0
        self.started_at = ""
        self.started_wall = 0.0
        self.started_monotonic = 0.0
        self.document: dict[str, Any] | None = None
        self.error: str | None = None
        self.done = False


class ApplicationPortUnavailableError(RuntimeError):
    """The application listener could not bind its configured port."""


class ApplicationReadinessError(RuntimeError):
    """The application listener started but did not become ready in time."""


class ApplicationRuntime:
    """Own the shared HTTP/provider/worker lifecycle for one application.

    The lifecycle adapter retains ownership of authenticated locator and
    control state.  This object owns the application listener, catalog
    admission, direct catalog workers, and their startup/terminal cleanup so
    another entry mode can use the same behavior without duplicating it.
    """

    def __init__(
        self,
        *,
        port: int,
        deadline: Deadline,
        server_factory: ServerFactory = _create_application_server,
        thread_factory: ThreadFactory = threading.Thread,
        workers: CatalogWorkerManager | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.port = port
        self._deadline_source = deadline
        self._server_factory = server_factory
        self._thread_factory = thread_factory
        self._clock = clock
        self.server: TrackerServer | Any | None = None
        self.http_thread: threading.Thread | Any | None = None
        self.workers = workers or CatalogWorkerManager()
        self.catalog_admitted = False
        self._settings_paths: state.StatePaths | None = None
        self._configuration: state.Configuration | None = None
        self._settings_accepting = False
        self._settings_active = 0
        self._settings_condition = threading.Condition()
        self._settings_operation_lock = threading.Lock()
        self._checkouts_condition = threading.Condition()
        self._checkouts_latest: dict[str, Any] | None = None
        self._checkouts_latest_change = 0
        self._checkouts_failure: tuple[str, int, float] | None = None
        self._checkouts_change = 0
        self._checkouts_local_refresh: float | None = None
        self._checkouts_last_check: dict[str, Any] | None = None
        self._checkouts_last_check_started_at = ""
        self._checkouts_last_check_started_wall = 0.0
        self._checkouts_last_check_started_monotonic: float | None = None
        self._checkouts_last_check_change = 0
        self._checkouts_check_requested = False
        self._checkouts_running: _CheckoutsRun | None = None
        self._checkouts_pending: _CheckoutsRun | None = None
        self._checkouts_threads: set[threading.Thread] = set()
        self._checkouts_queue_closed = False

    @property
    def configuration(self) -> state.Configuration | None:
        """Return the configuration captured by this application owner."""

        return self._configuration

    def capture_configuration(
        self, configuration: state.Configuration, paths: state.StatePaths
    ) -> None:
        """Capture the validated root used by the service settings boundary."""

        if not isinstance(configuration, state.Configuration):
            raise TypeError("application configuration must be validated")
        with self._settings_condition:
            if self._settings_accepting or self._settings_active:
                raise RuntimeError("application settings are already active")
            self._configuration = configuration
            self._settings_paths = paths

    def _settings_admit(self) -> None:
        with self._settings_condition:
            if not self._settings_accepting or self._configuration is None:
                raise CatalogError("settings_unavailable")
            self._settings_active += 1

    def _settings_release(self) -> None:
        with self._settings_condition:
            self._settings_active -= 1
            self._settings_condition.notify_all()

    def _settings_result(
        self,
        configuration: state.Configuration | None,
        outcome: str,
        *,
        order: tuple[str, ...] | None = None,
        completed: tuple[str, ...] | None = None,
    ) -> dict[str, object]:
        if order is None:
            order = () if configuration is None else configuration.stage_order
        if completed is None:
            completed = () if configuration is None else configuration.completed_stage_names
        return {
            "order": list(order),
            "completed": list(completed),
            "revision": None if configuration is None else configuration.revision,
            "outcome": outcome,
        }

    def get_settings(self) -> dict[str, object]:
        """Return only the active workspace order and its opaque revision."""

        self._settings_admit()
        try:
            with self._settings_operation_lock:
                configuration = self._configuration
                if configuration is None:
                    raise CatalogError("settings_unavailable")
                return {
                    "order": list(configuration.stage_order),
                    "completed": list(configuration.completed_stage_names),
                    "revision": configuration.revision,
                }
        finally:
            self._settings_release()

    def get_workspace(self) -> dict[str, str]:
        """Return the active literal workspace path with its configuration revision."""

        self._settings_admit()
        try:
            with self._settings_operation_lock:
                configuration = self._configuration
                if configuration is None:
                    raise CatalogError("settings_unavailable")
                return {
                    "root": str(configuration.specification_root),
                    "revision": configuration.revision,
                }
        finally:
            self._settings_release()

    def get_checkout_settings(self) -> dict[str, object]:
        """Return the active workspace's checkout folders, switch and revision."""

        self._settings_admit()
        try:
            with self._settings_operation_lock:
                configuration = self._configuration
                if configuration is None:
                    raise CatalogError("settings_unavailable")
                return self._checkout_settings_body(configuration)
        finally:
            self._settings_release()

    def _checkout_settings_body(
        self, configuration: state.Configuration | None
    ) -> dict[str, object]:
        if configuration is None:
            return {"folders": [], "github_lookup": False, "revision": None}
        return {
            "folders": list(configuration.checkout_folders),
            "github_lookup": configuration.checkout_github_lookup,
            "revision": configuration.revision,
        }

    def _checkout_settings_result(
        self, configuration: state.Configuration | None, outcome: str
    ) -> dict[str, object]:
        return {**self._checkout_settings_body(configuration), "outcome": outcome}

    def save_checkout_settings(
        self, revision: object, folders: object, github_lookup: object
    ) -> dict[str, object]:
        """Compare and save the active workspace's checkout folders and switch.

        A successful save replaces the record, updates the captured
        configuration and then marks the checkout inputs changed, so the next
        checkout request collects the new folders instead of answering from a
        document collected before them.
        """

        def prepare(current: state.Configuration) -> tuple[tuple[str, ...], bool]:
            return _checkout_settings_request(current, folders, github_lookup)

        def write(
            current: state.Configuration,
            paths: state.StatePaths,
            requested: tuple[tuple[str, ...], bool],
        ) -> state.Configuration:
            requested_folders, requested_lookup = requested
            return state.save_configuration_owned(
                current.specification_root,
                paths=paths,
                checkout_folders=requested_folders,
                github_lookup=requested_lookup,
            )

        def holds(
            reloaded: state.Configuration, requested: tuple[tuple[str, ...], bool]
        ) -> bool:
            requested_folders, requested_lookup = requested
            entry = reloaded.checkout_settings.get(str(reloaded.specification_root))
            if not requested_folders:
                return entry is None
            return (
                entry is not None
                and entry.folders == requested_folders
                and entry.github_lookup is requested_lookup
            )

        return self._save_settings_change(
            revision,
            prepare=prepare,
            write=write,
            holds=holds,
            shape=self._checkout_settings_result,
            announce=self.mark_checkouts_changed,
        )

    def save_settings(
        self, revision: str, order: object, completed: object = state._OMITTED
    ) -> dict[str, object]:
        """Compare and atomically save the captured root's settings.

        The account configuration remains the state owner's source of truth.
        A commit verification failure is immediately revalidated so callers can
        distinguish a successful requested write, a competing valid write, and
        an unreadable record without a rollback or recovery record.
        """

        def prepare(current: state.Configuration) -> object:
            return (
                current.completed_stage_names
                if completed is state._OMITTED
                else completed
            )

        def write(
            current: state.Configuration, paths: state.StatePaths, requested_completed: object
        ) -> state.Configuration:
            return state.save_configuration_owned(
                current.specification_root,
                paths=paths,
                stage_order=order,
                completed_stage_names=requested_completed,
            )

        def holds(reloaded: state.Configuration, requested_completed: object) -> bool:
            try:
                requested = tuple(order)  # validation already happened before replacement
            except TypeError:
                requested = ()
            try:
                requested_completed_tuple = tuple(requested_completed)
            except TypeError:
                requested_completed_tuple = ()
            return reloaded.stage_order == requested and reloaded.completed_stage_names == tuple(
                sorted(requested_completed_tuple)
            )

        return self._save_settings_change(
            revision,
            prepare=prepare,
            write=write,
            holds=holds,
            shape=self._settings_result,
        )

    def _save_settings_change(
        self,
        revision: object,
        *,
        prepare: Callable[[state.Configuration], Any],
        write: Callable[[state.Configuration, state.StatePaths, Any], state.Configuration],
        holds: Callable[[state.Configuration, Any], bool],
        shape: Callable[[state.Configuration | None, str], dict[str, object]],
        announce: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        """Run one compare-and-save: revision, content check, write, reconcile.

        The order is load-bearing.  A stale revision answers before the content
        check, so a stale editor never receives a folder refusal the board
        editor would not show; the check runs before the replacement, so a
        refused request leaves the record byte-identical.  Both settings saves
        share this mechanism, including the commit-verification reconciliation,
        which asks the caller whether the reloaded record holds the request.
        """

        self._settings_admit()
        try:
            with self._settings_operation_lock:
                current = self._configuration
                paths = self._settings_paths
                if current is None or paths is None:
                    raise CatalogError("settings_unavailable")
                if not isinstance(revision, str):
                    raise ValueError("settings revision must be text")
                if revision != current.revision:
                    return shape(current, "conflict")
                requested = prepare(current)
                try:
                    committed = write(current, paths, requested)
                except (state.StageOrderError, state.CompletedStageError):
                    raise
                except state.ConfigurationCommitVerificationError:
                    return self._reconcile_settings_write(
                        current, requested, holds, shape, announce
                    )
                except state.ConfigurationError:
                    # A pre-replacement error leaves the old record in place.
                    try:
                        reloaded = state.revalidate_configuration(paths)
                    except state.StateError:
                        return shape(None, "reload-needed")
                    self._configuration = reloaded
                    return shape(reloaded, "failure")
                self._configuration = committed
                if announce is not None:
                    announce()
                return shape(committed, "success")
        finally:
            self._settings_release()

    def _reconcile_settings_write(
        self,
        previous: state.Configuration,
        requested: Any,
        holds: Callable[[state.Configuration, Any], bool],
        shape: Callable[[state.Configuration | None, str], dict[str, object]],
        announce: Callable[[], None] | None,
    ) -> dict[str, object]:
        paths = self._settings_paths
        if paths is None:
            return shape(None, "reload-needed")
        try:
            reloaded = state.revalidate_configuration(paths)
        except state.StateError:
            return shape(None, "reload-needed")
        self._configuration = reloaded
        if (
            reloaded.specification_root == previous.specification_root
            and holds(reloaded, requested)
        ):
            if announce is not None:
                announce()
            return shape(reloaded, "success")
        return shape(reloaded, "conflict")

    def mark_checkouts_changed(self) -> None:
        """Announce that the checkout inputs changed.

        The change count is part of a run's identity: a run that started before
        the mark cannot replace the served document, so the next request waits
        for a run that reads the new inputs.  Nothing starts here.
        """

        with self._checkouts_condition:
            self._checkouts_change += 1

    def get_checkouts(self) -> dict[str, Any]:
        """Return the served checkout envelope, collecting a document if needed.

        The answer is the current document, or the document of the run this
        request shares or queues.  A shared or queued run answers even when a
        configuration change arrives before it ends, and a failed run answers
        its waiters with its own category.
        """

        if not self.catalog_admitted:
            raise CatalogError("checkouts_unavailable")
        with self._checkouts_condition:
            while True:
                if self._checkouts_queue_closed:
                    raise CatalogError("checkouts_cancelled")
                if self._checkouts_document_is_current():
                    self._queue_background_run()
                    return self._checkouts_envelope(self._checkouts_latest)
                run = self._checkouts_document_source()
                if run is None:
                    failure = self._checkouts_open_failure()
                    if failure is not None:
                        raise CatalogError(failure[0])
                    run = self._queue_checkouts_run()
                while not run.done:
                    self._checkouts_condition.wait()
                if run.document is not None:
                    return self._checkouts_envelope(run.document)
                raise CatalogError(run.error)

    def refresh_remote_checks(self) -> dict[str, Any]:
        """Queue one networked check for the remote-check entry point.

        The answer is the document of the first run that starts after the
        request arrives.  A check that fails answers with the served document
        and its recorded outcome while one is current, and with its category
        when none is.
        """

        if not self.catalog_admitted:
            raise CatalogError("checkouts_unavailable")
        with self._checkouts_condition:
            if self._checkouts_queue_closed:
                raise CatalogError("checkouts_cancelled")
            self._checkouts_check_requested = True
            run = self._checkouts_pending
            if run is None:
                run = self._queue_checkouts_run()
            while not run.done:
                self._checkouts_condition.wait()
            if run.document is not None:
                return self._checkouts_envelope(run.document)
            if run.error == "checkouts_cancelled":
                raise CatalogError("checkouts_cancelled")
            if self._checkouts_document_is_current():
                return self._checkouts_envelope(self._checkouts_latest)
            raise CatalogError(run.error)

    def _checkouts_document_is_current(self) -> bool:
        return (
            self._checkouts_latest is not None
            and self._checkouts_latest_change == self._checkouts_change
        )

    def _checkouts_check_is_due(self) -> bool:
        if self._checkouts_last_check_started_monotonic is None:
            return True
        if self._checkouts_last_check_change != self._checkouts_change:
            return True
        elapsed = self._clock() - self._checkouts_last_check_started_monotonic
        return elapsed >= REMOTE_CHECK_INTERVAL

    def _checkouts_open_failure(self) -> tuple[str, int, float] | None:
        failure = self._checkouts_failure
        if failure is None:
            return None
        _code, change, started = failure
        if change != self._checkouts_change:
            return None
        if self._clock() - started >= LOCAL_REFRESH_INTERVAL:
            return None
        return failure

    def _checkouts_document_source(self) -> _CheckoutsRun | None:
        running = self._checkouts_running
        if running is not None and running.change == self._checkouts_change:
            return running
        return self._checkouts_pending

    def _queue_background_run(self) -> None:
        if self._checkouts_running is not None or self._checkouts_pending is not None:
            return
        if self._checkouts_check_is_due():
            self._queue_checkouts_run()
            return
        clock = self._checkouts_local_refresh
        if clock is not None and self._clock() - clock < LOCAL_REFRESH_INTERVAL:
            return
        self._queue_checkouts_run()

    def _queue_checkouts_run(self) -> _CheckoutsRun:
        run = self._checkouts_pending
        if run is not None:
            return run
        run = _CheckoutsRun()
        if self._checkouts_running is None:
            self._start_checkouts_run(run)
        else:
            self._checkouts_pending = run
        return run

    def _start_checkouts_run(self, run: _CheckoutsRun) -> None:
        """Make ``run`` the in-flight run and start its thread.

        A run that cannot start ends at once as an unexpected failure, with the
        accounting of any failed run, so its waiters are answered and the queue
        keeps no slot that cannot make progress.
        """

        self._checkouts_running = run
        try:
            self._begin_checkouts_run(run)
        except Exception:  # noqa: BLE001 - a run failure must stay category-safe
            self._checkouts_running = None
            self._settle_checkouts_run(run, None, "checkouts_failed")
            self._checkouts_condition.notify_all()

    def _begin_checkouts_run(self, run: _CheckoutsRun) -> None:
        run.change = self._checkouts_change
        run.started_wall = time.time()
        run.started_at = _utc_timestamp(run.started_wall)
        run.started_monotonic = self._clock()
        if self._checkouts_check_requested or self._checkouts_check_is_due():
            run.kind = _CHECK
            self._checkouts_check_requested = False
            self._checkouts_last_check_started_at = run.started_at
            self._checkouts_last_check_started_wall = run.started_wall
            self._checkouts_last_check_started_monotonic = run.started_monotonic
            self._checkouts_last_check_change = run.change
        else:
            run.kind = _LOCAL
        thread = threading.Thread(
            target=self._collect_checkouts,
            args=(run,),
            name="nyx-checkouts-run",
            daemon=True,
        )
        self._checkouts_threads.add(thread)
        try:
            thread.start()
        except BaseException:
            self._checkouts_threads.discard(thread)
            raise

    def _collect_checkouts(self, run: _CheckoutsRun) -> None:
        document: dict[str, Any] | None = None
        failure: str | None = None
        try:
            payload = self.workers.fetch_checkouts(network=run.kind == _CHECK)
        except WorkerError as error:
            failure = error.code if error.code in _CHECKOUTS_ERRORS else "checkouts_failed"
        except Exception:  # noqa: BLE001 - a run failure must stay category-safe
            failure = "checkouts_failed"
        else:
            try:
                document = parse_checkouts(payload)
            except (ProtocolError, ValueError, TypeError, RecursionError):
                failure = "checkouts_protocol_error"
            except Exception:  # noqa: BLE001 - a run failure must stay category-safe
                failure = "checkouts_failed"
        with self._checkouts_condition:
            try:
                self._finish_checkouts_run(run, document, failure)
            finally:
                self._checkouts_condition.notify_all()

    def _finish_checkouts_run(
        self,
        run: _CheckoutsRun,
        document: dict[str, Any] | None,
        failure: str | None,
    ) -> None:
        self._checkouts_threads.discard(threading.current_thread())
        try:
            self._settle_checkouts_run(run, document, failure)
        finally:
            pending = self._checkouts_pending
            self._checkouts_pending = None
            self._checkouts_running = None
            if pending is not None:
                self._start_checkouts_run(pending)

    def _settle_checkouts_run(
        self,
        run: _CheckoutsRun,
        document: dict[str, Any] | None,
        failure: str | None,
    ) -> None:
        """Record how ``run`` ended and apply it to the served state."""

        run.done = True
        run.document = document
        run.error = failure
        current = run.change == self._checkouts_change
        if document is not None:
            if current:
                self._checkouts_latest = document
                self._checkouts_latest_change = run.change
                self._checkouts_failure = None
                self._checkouts_local_refresh = run.started_monotonic
            if run.kind == _CHECK:
                self._record_checkouts_check(run, "completed", document)
        elif failure != "checkouts_cancelled":
            if current and not self._checkouts_document_is_current():
                self._checkouts_failure = (failure, run.change, run.started_monotonic)
            if run.kind == _LOCAL and current:
                self._checkouts_local_refresh = run.started_monotonic
            if run.kind == _CHECK:
                self._record_checkouts_check(run, failure, None)

    def _record_checkouts_check(
        self,
        run: _CheckoutsRun,
        outcome: str,
        document: dict[str, Any] | None,
    ) -> None:
        repositories: list[dict[str, Any]] = []
        if document is not None:
            for repository in document["repositories"]:
                remote = repository["remote"]
                if remote is None:
                    continue
                check = remote["check"]
                lookup = repository["pr_lookup"]
                repositories.append(
                    {
                        "remote": remote["key"],
                        "check": {"status": check["status"], "reason": check["reason"]},
                        "pr_lookup": {
                            "status": lookup["status"],
                            "reason": lookup["reason"],
                        },
                    }
                )
            repositories.sort(key=lambda item: item["remote"])
        self._checkouts_last_check = {
            "started_at": run.started_at,
            "finished_at": _utc_timestamp(time.time()),
            "outcome": outcome,
            "repositories": repositories,
        }

    def _checkouts_envelope(self, document: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "checkouts": document,
            "remote_checks": {
                "state": "running" if self._checkouts_check_is_running() else "idle",
                "interval_seconds": REMOTE_CHECK_INTERVAL,
                "next_due_at": self._checkouts_next_due_at(),
                "last": self._checkouts_last_check,
            },
        }

    def _checkouts_check_is_running(self) -> bool:
        running = self._checkouts_running
        if running is not None and running.kind == _CHECK:
            return True
        if self._checkouts_pending is None:
            return False
        return self._checkouts_check_requested or self._checkouts_check_is_due()

    def _checkouts_next_due_at(self) -> str | None:
        if self._checkouts_check_is_due():
            return None
        return _utc_timestamp(
            self._checkouts_last_check_started_wall + REMOTE_CHECK_INTERVAL
        )

    def _close_checkouts_queue(self) -> None:
        """Refuse new runs and answer the queued run's waiters as cancelled.

        Nothing starts once this returns: the in-flight run is cancelled by the
        worker admission close that follows, and its waiters are answered when
        its run settles.
        """

        with self._checkouts_condition:
            self._checkouts_queue_closed = True
            pending = self._checkouts_pending
            self._checkouts_pending = None
            if pending is not None:
                self._settle_checkouts_run(pending, None, "checkouts_cancelled")
            self._checkouts_condition.notify_all()

    def _join_checkouts_runs(self, deadline: float) -> bool:
        """Join every started run thread within the deadline.

        A thread still alive at the deadline reports the cleanup as not closed;
        a retried call repeats the join on the threads it still finds.
        """

        with self._checkouts_condition:
            threads = tuple(self._checkouts_threads)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                thread.join(remaining)
            if thread.is_alive():
                return False
        return True

    def _close_settings_admission(self, deadline: float) -> bool:
        with self._settings_condition:
            self._settings_accepting = False
            while self._settings_active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._settings_condition.wait(timeout=remaining)
            return True

    def _deadline(self) -> float:
        source = self._deadline_source
        return source() if callable(source) else source

    def _require_deadline(self) -> None:
        if time.monotonic() >= self._deadline():
            raise RuntimeError("application runtime deadline expired")

    def _provider(self) -> Catalog:
        if not self.catalog_admitted:
            raise CatalogError("producer_unavailable")
        try:
            return parse_catalog(self.workers.fetch_catalog())
        except WorkerError as error:
            raise CatalogError(error.code) from None
        except (ProtocolError, ValueError, TypeError) as error:
            raise CatalogError("producer_protocol_error") from error

    def _static_ready(self) -> bool:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=0.2)
        try:
            connection.request(
                "GET", "/", headers={"Host": f"127.0.0.1:{self.port}"}
            )
            response = connection.getresponse()
            response.read(64 * 1024)
            return (
                response.status == 200
                and response.getheader("Content-Type") == "text/html; charset=utf-8"
            )
        except (OSError, http.client.HTTPException):
            return False
        finally:
            connection.close()

    def start(self, *, static_ready: Callable[[], bool] | None = None) -> None:
        """Start the listener, leaving catalog admission closed."""

        self._require_deadline()
        try:
            self.server = self._server_factory(
                provider=self._provider,
                port=self.port,
                settings_provider=self,
            )
        except OSError as error:
            if error.errno in (errno.EADDRINUSE, errno.EACCES):
                raise ApplicationPortUnavailableError("application port is unavailable") from error
            raise
        self.http_thread = self._thread_factory(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.http_thread.start()
        ready = static_ready or self._static_ready
        while time.monotonic() < self._deadline() and not ready():
            time.sleep(0.01)
        if time.monotonic() >= self._deadline():
            raise ApplicationReadinessError(
                "application HTTP listener did not become ready"
            )

    def admit_catalog(self) -> None:
        """Open provider admission after control verification and publication."""

        self._require_deadline()
        with self._settings_condition:
            self.catalog_admitted = True
            self._settings_accepting = self._configuration is not None

    def board_url(self) -> str:
        """Return the loopback board URL, available only after admission.

        A caller must not navigate a presentation surface before the owned
        runtime has opened catalog admission, so the accessor refuses to
        produce a URL while admission is closed (including after cleanup).
        """

        if not self.catalog_admitted:
            raise RuntimeError("catalog is not admitted")
        return f"http://127.0.0.1:{self.port}/"

    def wait(self) -> None:
        """Wait for the application listener to finish serving."""

        if self.http_thread is not None:
            self.http_thread.join()

    def _join_server_handlers(self, deadline: float) -> bool:
        """Wait for in-flight handler work, tolerating test server doubles."""
        join = getattr(self.server, "join_handlers", None)
        if join is None:
            return True
        try:
            return bool(join(deadline))
        except Exception:
            return False

    def cleanup_start_failure(self, deadline: float) -> bool:
        """Close failed-start resources, retaining ownership if work remains."""

        self.catalog_admitted = False
        self._close_checkouts_queue()
        settings_closed = self._close_settings_admission(deadline)
        listener_closed = True
        workers_closed = False
        runs_joined = False
        try:
            try:
                self.workers.close_admission()
            except Exception:
                listener_closed = False
            if self.server is not None:
                for cleanup in (
                    self.server.shutdown,
                    self.server.close_active_connections,
                ):
                    try:
                        cleanup()
                    except Exception:
                        listener_closed = False
                # join_handlers drains parsed request work within the deadline
                # and returns immediately when handlers are parked on kept-alive
                # reads.  Startup-failure cleanup runs after the shared deadline
                # has necessarily expired (e.g. static-readiness expiry), so its
                # result must not retain the failure: run() waits for the
                # terminal state this completes, and the accepted sockets are
                # already closed above.
                self._join_server_handlers(deadline)
                try:
                    self.server.server_close()
                except Exception:
                    listener_closed = False
            try:
                workers_closed = self.workers.close(deadline)
            except Exception:
                workers_closed = False
            runs_joined = self._join_checkouts_runs(deadline)
        finally:
            self.catalog_admitted = False
        return settings_closed and listener_closed and workers_closed and runs_joined

    def shutdown(self, deadline: float) -> bool:
        """Close listener and workers before lifecycle ownership is released.

        The checkout queue closes before the settings wait, so no run can start
        while cleanup is under way and the waiter of a run that never starts is
        told it was cancelled.  The accepted sockets close before the listener
        so a kept-alive handler stops waiting, and the listener is released even
        when parsed request work does not drain inside the deadline: the
        caller's precondition is the released port, so an undrained request is
        reported through the result instead of by skipping the listener close.
        A checkout run thread still alive at the deadline reports the same way
        and a retried call repeats the join.  An expired deadline still stops
        before the next cleanup phase begins.
        """

        self.catalog_admitted = False
        self._close_checkouts_queue()
        if not self._close_settings_admission(deadline):
            return False
        self.workers.close_admission()
        drained = True
        if self.server is not None:
            if time.monotonic() >= deadline:
                return False
            self.server.shutdown()
            if time.monotonic() >= deadline:
                return False
            self.server.close_active_connections()
            if time.monotonic() >= deadline:
                return False
            drained = self._join_server_handlers(deadline)
            try:
                self.server.server_close()
            except Exception:
                return False
        if time.monotonic() >= deadline:
            return False
        workers_closed = self.workers.close(deadline)
        joined = self._join_checkouts_runs(deadline)
        return workers_closed and drained and joined and time.monotonic() < deadline


__all__ = [
    "ApplicationPortUnavailableError",
    "ApplicationReadinessError",
    "ApplicationRuntime",
    "LOCAL_REFRESH_INTERVAL",
    "REMOTE_CHECK_INTERVAL",
]
