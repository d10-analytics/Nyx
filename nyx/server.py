"""Loopback-only HTTP surface for the read-only catalog viewer."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Any, Protocol

from .catalog import scan_catalog
from .models import Catalog, ProtocolError, parse_catalog
from .state import ConfigurationError, StageOrderError, StateError

_STATIC_ROOT = Path(__file__).with_name("static")
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}
_API_ROUTES = frozenset({"/api/catalog", "/api/settings"})
_SAFE_ERRORS = frozenset(
    {
        "producer_unavailable",
        "producer_timeout",
        "producer_failed",
        "producer_output_too_large",
        "producer_protocol_error",
        "producer_cancelled",
        "settings_unavailable",
    }
)


class CatalogError(RuntimeError):
    """A safe, user-facing catalog provider failure category."""

    def __init__(self, code: str) -> None:
        if code not in _SAFE_ERRORS:
            raise ValueError("invalid catalog error code")
        self.code = code
        super().__init__(code)


class CatalogProvider(Protocol):
    """The narrow provider seam used by the static server."""

    def fetch_catalog(self) -> Catalog: ...


Provider = CatalogProvider | Callable[[], Catalog | bytes | str | dict[str, Any]]


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _catalog_from_provider(provider: Provider) -> Catalog:
    try:
        value = provider.fetch_catalog() if hasattr(provider, "fetch_catalog") else provider()
        if isinstance(value, Catalog):
            return parse_catalog(value.as_dict())
        return parse_catalog(value)
    except CatalogError:
        raise
    except (ProtocolError, ValueError, TypeError, RecursionError):
        raise CatalogError("producer_protocol_error") from None
    except Exception:  # noqa: BLE001 - provider failures must stay category-safe
        raise CatalogError("producer_failed") from None


def _default_provider() -> Catalog:
    try:
        return parse_catalog(scan_catalog(Path.cwd()))
    except (OSError, ValueError, ProtocolError, RecursionError):
        raise CatalogError("producer_unavailable") from None


def _handler_for(
    provider: Provider, settings_provider: Any | None = None
) -> type[BaseHTTPRequestHandler]:
    class TrackerHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            # Catalog values and filesystem paths never enter request logs.
            return

        def send_error(
            self, code: int, message: str | None = None, explain: str | None = None
        ) -> None:
            if code == HTTPStatus.NOT_IMPLEMENTED:
                self._send(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    b"Method not allowed\n",
                    "text/plain; charset=utf-8",
                )
                return
            super().send_error(code, message, explain)

        def _host_allowed(self) -> bool:
            expected_port = self.server.server_port
            return self.headers.get("Host") in {
                f"127.0.0.1:{expected_port}",
                f"localhost:{expected_port}",
            }

        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _error(self, code: str) -> None:
            self._send(HTTPStatus.BAD_GATEWAY, _json_bytes({"error": code}), "application/json")

        def _settings_provider(self) -> Any | None:
            return settings_provider

        def _settings_get(self) -> None:
            settings = self._settings_provider()
            if settings is None or not hasattr(settings, "get_settings"):
                self._send(HTTPStatus.NOT_FOUND, b"Not found\n", "text/plain; charset=utf-8")
                return
            try:
                body = settings.get_settings()
            except CatalogError as error:
                self._send(HTTPStatus.SERVICE_UNAVAILABLE, _json_bytes({"error": error.code}), "application/json")
                return
            self._send(HTTPStatus.OK, _json_bytes(body), "application/json")

        def _settings_put(self) -> None:
            settings = self._settings_provider()
            if settings is None or not hasattr(settings, "save_settings"):
                self._send(HTTPStatus.NOT_FOUND, b"Not found\n", "text/plain; charset=utf-8")
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if length < 0 or length > 1024 * 1024:
                self._send(HTTPStatus.BAD_REQUEST, _json_bytes({"error": "invalid_payload"}), "application/json")
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
                self._send(HTTPStatus.BAD_REQUEST, _json_bytes({"error": "invalid_payload"}), "application/json")
                return
            if (
                not isinstance(payload, dict)
                or set(payload) != {"revision", "order"}
                or not isinstance(payload.get("revision"), str)
                or not isinstance(payload.get("order"), list)
            ):
                self._send(HTTPStatus.BAD_REQUEST, _json_bytes({"error": "invalid_payload"}), "application/json")
                return
            try:
                result = settings.save_settings(payload["revision"], payload["order"])
            except CatalogError as error:
                self._send(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    _json_bytes({"error": error.code}),
                    "application/json",
                )
                return
            except (ValueError, ProtocolError, TypeError):
                self._send(HTTPStatus.BAD_REQUEST, _json_bytes({"error": "invalid_payload"}), "application/json")
                return
            except StageOrderError:
                self._send(HTTPStatus.BAD_REQUEST, _json_bytes({"error": "invalid_order"}), "application/json")
                return
            except (ConfigurationError, StateError):
                self._send(HTTPStatus.SERVICE_UNAVAILABLE, _json_bytes({"error": "settings_unavailable"}), "application/json")
                return
            outcome = result.get("outcome")
            status = HTTPStatus.CONFLICT if outcome in {"conflict", "reload-needed"} else HTTPStatus.OK
            if outcome == "failure":
                status = HTTPStatus.INTERNAL_SERVER_ERROR
            self._send(status, _json_bytes(result), "application/json")

        def _dispatch(self) -> None:
            if not self._host_allowed():
                self._send(HTTPStatus.NOT_FOUND, b"Not found\n", "text/plain; charset=utf-8")
                return
            if self.path == "/api/settings":
                if self.command in {"GET", "HEAD"}:
                    self._settings_get()
                elif self.command == "PUT":
                    self._settings_put()
                else:
                    self._send(
                        HTTPStatus.METHOD_NOT_ALLOWED,
                        b"Method not allowed\n",
                        "text/plain; charset=utf-8",
                    )
                return
            if self.command not in {"GET", "HEAD"}:
                self._send(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    b"Method not allowed\n",
                    "text/plain; charset=utf-8",
                )
                return
            if self.path in _API_ROUTES:
                try:
                    catalog = _catalog_from_provider(provider)
                except CatalogError as error:
                    self._error(error.code)
                    return
                self._send(
                    HTTPStatus.OK,
                    _json_bytes(catalog.as_dict()),
                    "application/json",
                )
                return
            static = _STATIC.get(self.path)
            if static is None:
                self._send(HTTPStatus.NOT_FOUND, b"Not found\n", "text/plain; charset=utf-8")
                return
            filename, content_type = static
            try:
                body = (_STATIC_ROOT / filename).read_bytes()
            except OSError:
                self._send(HTTPStatus.NOT_FOUND, b"Not found\n", "text/plain; charset=utf-8")
                return
            self._send(HTTPStatus.OK, body, content_type)

        def do_GET(self) -> None:
            self._dispatch()

        def do_POST(self) -> None:
            self._dispatch()

        def do_PUT(self) -> None:
            self._dispatch()

        def do_DELETE(self) -> None:
            self._dispatch()

        def do_PATCH(self) -> None:
            self._dispatch()

        def do_OPTIONS(self) -> None:
            self._dispatch()

        def do_TRACE(self) -> None:
            self._dispatch()

        def do_CONNECT(self) -> None:
            self._dispatch()

        def do_HEAD(self) -> None:
            self._dispatch()

    return TrackerHandler


class TrackerServer(ThreadingHTTPServer):
    """A server constrained to loopback and the fixed tracker routes."""

    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self) -> None:
        # The listener is already numeric loopback; reverse DNS adds no useful
        # identity and can block construction before the server starts listening.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def get_request(self) -> tuple[Any, Any]:
        connection, address = super().get_request()
        with self._connection_lock:
            self._active_connections.add(connection)
        return connection, address

    def close_request(self, request: Any) -> None:
        with self._connection_lock:
            self._active_connections.discard(request)
        super().close_request(request)

    def close_active_connections(self) -> None:
        """Close accepted idle/active sockets during an owned shutdown."""
        with self._connection_lock:
            connections = tuple(self._active_connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

    def __init__(
        self,
        provider: Provider | None = None,
        port: int = 0,
    ) -> None:
        self._initialize(provider, port, None)

    def _initialize(
        self,
        provider: Provider | None,
        port: int,
        settings_provider: Any | None,
    ) -> None:
        self._active_connections: set[Any] = set()
        self._connection_lock = threading.Lock()
        selected_provider = provider if provider is not None else _default_provider
        super().__init__(
            ("127.0.0.1", port),
            _handler_for(selected_provider, settings_provider),
        )

def create_server(
    provider: Provider | None = None,
    port: int = 0,
) -> TrackerServer:
    """Create a standalone read-only catalog server."""

    return TrackerServer(provider=provider, port=port)


class _ApplicationTrackerServer(TrackerServer):
    """Settings-capable server constructed only by the application owner."""

    def __init__(
        self,
        provider: Provider,
        port: int,
        settings_provider: Any,
    ) -> None:
        self._initialize(provider, port, settings_provider)


def _create_application_server(
    provider: Provider,
    port: int,
    settings_provider: Any,
) -> TrackerServer:
    """Create the settings-capable server used by ApplicationRuntime."""

    return _ApplicationTrackerServer(
        provider=provider,
        port=port,
        settings_provider=settings_provider,
    )


def run_server(provider: Provider | None = None, port: int = 0) -> None:
    """Run the local service until interrupted."""
    server = create_server(provider=provider, port=port)
    print(f"http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
