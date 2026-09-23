"""Shared application HTTP, catalog admission, and worker lifecycle."""

from __future__ import annotations

import http.client
import threading
import time
from collections.abc import Callable
from typing import Any

from . import state
from .models import Catalog, ProtocolError, parse_catalog
from .server import CatalogError, TrackerServer, create_server
from .worker import CatalogWorkerManager, WorkerError

Deadline = float | Callable[[], float]
ServerFactory = Callable[..., TrackerServer]
ThreadFactory = Callable[..., threading.Thread]


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
        server_factory: ServerFactory = create_server,
        thread_factory: ThreadFactory = threading.Thread,
        workers: CatalogWorkerManager | None = None,
    ) -> None:
        self.port = port
        self._deadline_source = deadline
        self._server_factory = server_factory
        self._thread_factory = thread_factory
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
    ) -> dict[str, object]:
        if order is None:
            order = () if configuration is None else configuration.stage_order
        return {
            "order": list(order),
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
                    "revision": configuration.revision,
                }
        finally:
            self._settings_release()

    def save_settings(
        self, revision: str, order: object
    ) -> dict[str, object]:
        """Compare and atomically save the captured root's stage order.

        The account configuration remains the state owner's source of truth.
        A commit verification failure is immediately revalidated so callers can
        distinguish a successful requested write, a competing valid write, and
        an unreadable record without a rollback or recovery record.
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
                    return self._settings_result(current, "conflict")
                try:
                    committed = state.save_configuration_owned(
                        current.specification_root,
                        paths=paths,
                        stage_order=order,
                    )
                except state.StageOrderError:
                    raise
                except state.ConfigurationCommitVerificationError:
                    return self._reconcile_settings_write(current, order)
                except state.ConfigurationError:
                    # A pre-replacement error leaves the old record in place.
                    try:
                        reloaded = state.revalidate_configuration(paths)
                    except state.StateError:
                        return self._settings_result(None, "reload-needed", order=())
                    self._configuration = reloaded
                    return self._settings_result(reloaded, "failure")
                self._configuration = committed
                return self._settings_result(committed, "success")
        finally:
            self._settings_release()

    def _reconcile_settings_write(
        self, previous: state.Configuration, requested_order: object
    ) -> dict[str, object]:
        paths = self._settings_paths
        if paths is None:
            return self._settings_result(None, "reload-needed", order=())
        try:
            reloaded = state.revalidate_configuration(paths)
        except state.StateError:
            return self._settings_result(None, "reload-needed", order=())
        self._configuration = reloaded
        try:
            requested = tuple(requested_order)  # validation already happened before replacement
        except TypeError:
            requested = ()
        if reloaded.specification_root == previous.specification_root and reloaded.stage_order == requested:
            return self._settings_result(reloaded, "success")
        return self._settings_result(reloaded, "conflict")

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
        self.server = self._server_factory(provider=self._provider, port=self.port)
        set_settings_provider = getattr(self.server, "set_settings_provider", None)
        if set_settings_provider is not None:
            set_settings_provider(self)
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

    def cleanup_start_failure(self, deadline: float) -> bool:
        """Close failed-start resources, retaining ownership if work remains."""

        self.catalog_admitted = False
        settings_closed = self._close_settings_admission(deadline)
        listener_closed = True
        workers_closed = False
        try:
            try:
                self.workers.close_admission()
            except Exception:
                listener_closed = False
            if self.server is not None:
                for cleanup in (
                    self.server.shutdown,
                    self.server.close_active_connections,
                    self.server.server_close,
                ):
                    try:
                        cleanup()
                    except Exception:
                        listener_closed = False
            try:
                workers_closed = self.workers.close(deadline)
            except Exception:
                workers_closed = False
        finally:
            self.catalog_admitted = False
        return settings_closed and listener_closed and workers_closed

    def shutdown(self, deadline: float) -> bool:
        """Close listener and workers before lifecycle ownership is released."""

        self.catalog_admitted = False
        if not self._close_settings_admission(deadline):
            return False
        self.workers.close_admission()
        if self.server is not None:
            if time.monotonic() >= deadline:
                return False
            self.server.shutdown()
            if time.monotonic() >= deadline:
                return False
            self.server.close_active_connections()
            if time.monotonic() >= deadline:
                return False
            self.server.server_close()
        if time.monotonic() >= deadline:
            return False
        return self.workers.close(deadline) and time.monotonic() < deadline


__all__ = ["ApplicationReadinessError", "ApplicationRuntime"]
