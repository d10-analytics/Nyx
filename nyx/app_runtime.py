"""Shared application HTTP, catalog admission, and worker lifecycle."""

from __future__ import annotations

import http.client
import threading
import time
from collections.abc import Callable
from typing import Any

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
        self.catalog_admitted = True

    def wait(self) -> None:
        """Wait for the application listener to finish serving."""

        if self.http_thread is not None:
            self.http_thread.join()

    def cleanup_start_failure(self, deadline: float) -> bool:
        """Close failed-start resources, retaining ownership if work remains."""

        self.catalog_admitted = False
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
        return listener_closed and workers_closed

    def shutdown(self, deadline: float) -> bool:
        """Close listener and workers before lifecycle ownership is released."""

        self.catalog_admitted = False
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
