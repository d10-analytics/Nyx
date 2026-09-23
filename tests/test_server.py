import http.client
import json
import socket
import threading
from dataclasses import replace
from unittest.mock import patch
from uuid import UUID

import pytest

from nyx import server
from nyx.models import canonical_digest, parse_catalog
from nyx.server import CatalogError, create_server


class StubClient:
    def __init__(self, catalog=None, error=None):
        self.catalog = catalog
        self.error = error
        self.calls = 0

    def fetch_catalog(self):
        self.calls += 1
        if self.error is not None:
            raise CatalogError(self.error)
        return self.catalog


def raw_catalog():
    value = {
        "schema_version": 4,
        "inventory": {
            "projects": [{"name": "Fictional", "availability": "complete"}],
            "stages": [
                {"project": "Fictional", "stage": "Queue", "availability": "complete"},
                {"project": "Fictional", "stage": "Under_Development", "availability": "complete"},
            ],
        },
        "visibility": {"hidden_stages": ["Archive", "Done", "In_Progress"],
                        "visible_entry_count": 2, "hidden_entry_count": 0},
        "identity_coverage": {"state": "complete", "diagnostics": []},
        "program_coverage": {"state": "complete", "diagnostics": []},
        "discovery_diagnostics": [],
        "entries": [],
        "programs": [],
    }
    for path, title in (("Fictional/Queue/first", "First"),
                        ("Fictional/Under_Development/second", "Second")):
        value["entries"].append({
            "package_id": str(UUID(int=len(value["entries"]) + 1, version=4)),
            "package_path": path,
            "project": path.split("/")[0],
            "stage": path.split("/")[1],
            "board_visible": True,
            "state": "complete",
            "declared": {"title": title, "target_project": "Fictional",
                         "status": "ready", "closure": "approved",
                         "sanity_recommendation": "IMPLEMENT",
                         "human_sanity_decision": "AFFIRMED"},
            "diagnostics": [],
            "relationship": {"participation": "available", "claims": [],
                              "prerequisites": [],
                              "direct_prerequisite_state": "no_declared_prerequisites",
                              "program": {"program_id": None, "title": None,
                                          "resolution": "not_declared", "diagnostics": []},
                              "superseded_by": {"package_id": None,
                                                "resolution": "not_declared", "diagnostics": []}},
            "transitive_diagnostics": [],
        })
    first, second = value["entries"]
    program_id = str(UUID(int=3, version=4))
    first["relationship"] = {
        "participation": "available",
        "claims": [
            {
                "name": "release",
                "state": "satisfied",
                "evidence_ref": "sha256:" + "a" * 64,
                "diagnostics": [],
            }
        ],
        "prerequisites": [
            {
                "target_package_id": second["package_id"],
                "claim_name": "release",
                "observed_state": "satisfied",
                "observed_evidence_ref": "sha256:" + "a" * 64,
                "resolved_state": "satisfied",
                "reason": "claim_satisfied",
            }
        ],
        "direct_prerequisite_state": "satisfied",
        "program": {
            "program_id": program_id,
            "title": "Test program",
            "resolution": "resolved",
            "diagnostics": [],
        },
        "superseded_by": {
            "package_id": second["package_id"],
            "resolution": "resolved",
            "diagnostics": [],
        },
    }
    value["programs"] = [
        {
            "program_id": program_id,
            "title": "Test program",
            "member_package_ids": [first["package_id"]],
            "diagnostics": [],
        }
    ]
    value["catalog_digest"] = canonical_digest(value)
    return value


def valid_catalog():
    return parse_catalog(raw_catalog())


def test_default_provider_uses_unselected_scanner():
    payload = json.dumps(raw_catalog(), separators=(",", ":"))
    with patch.object(server, "scan_catalog", return_value=payload) as scanner:
        result = server._default_provider()
    assert result == valid_catalog()
    scanner.assert_called_once_with(server.Path.cwd())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(schema_version=3),
        lambda value: value.update(unknown=True),
        lambda value: value["entries"][0].update(board_visible="true"),
        lambda value: value["entries"][0].update(stage="Done"),
        lambda value: value["entries"][0].update(
            transitive_diagnostics=[{"code": "transitive_diagnostics_truncated"}]
        ),
    ],
    ids=["schema-2", "unknown-key", "nonboolean-visibility", "policy-mismatch", "transitive-reference"],
)
def test_schema_four_parser_rejects_legacy_unknown_and_mutated_payloads(mutate):
    value = raw_catalog()
    mutate(value)
    value["catalog_digest"] = canonical_digest(value)
    with pytest.raises(ValueError):
        parse_catalog(value)


def test_schema_four_parser_rejects_bad_digest_even_when_shape_is_valid():
    value = raw_catalog()
    value["catalog_digest"] = "0" * 64
    with pytest.raises(ValueError):
        parse_catalog(value)


@pytest.mark.parametrize("alter", [
    lambda catalog: replace(catalog, schema_version=3),
    lambda catalog: replace(
        catalog,
        entries=(replace(catalog.entries[0], board_visible="yes"), *catalog.entries[1:]),
    ),
])
def test_catalog_object_providers_are_revalidated_as_schema_four(alter):
    with pytest.raises(CatalogError, match="producer_protocol_error"):
        server._catalog_from_provider(lambda: alter(valid_catalog()))


class RunningServer:
    def __init__(self, client, settings=None):
        self.server = create_server(client, settings_provider=settings)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def __enter__(self):
        return self.server.server_port

    def __exit__(self, *exc):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


def request(port, method, path, host=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {} if host is None else {"Host": host}
    if body is not None:
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, response.getheader("Content-Type"), body


class SettingsStub:
    def __init__(self):
        self.value = {"order": ["Queue", "Done"], "revision": "opaque"}
        self.calls = []

    def get_settings(self):
        return dict(self.value)

    def save_settings(self, revision, order):
        self.calls.append((revision, order))
        if revision != self.value["revision"]:
            return {**self.value, "outcome": "conflict"}
        self.value = {"order": list(order), "revision": "new-opaque"}
        return {**self.value, "outcome": "success"}


def test_numeric_loopback_bind_serves_and_closes_without_reverse_dns():
    client = StubClient(catalog=valid_catalog())
    with patch.object(socket, "getfqdn", side_effect=AssertionError("reverse DNS forbidden")) as fqdn, patch.object(
        socket, "gethostbyaddr", side_effect=AssertionError("reverse DNS forbidden")
    ) as reverse:
        running = RunningServer(client)
        with running as port:
            assert port > 0
            assert running.server.server_address == ("127.0.0.1", port)
            assert running.server.server_name == "127.0.0.1"
            status, content_type, body = request(port, "GET", "/")
            assert status == 200 and content_type == "text/html; charset=utf-8"
            assert body == server._STATIC_ROOT.joinpath("index.html").read_bytes()
            assert request(port, "GET", "/", host=f"localhost:{port}")[0] == 200
            assert request(port, "GET", "/api/catalog", host=f"outside.invalid:{port}")[0] == 404
            assert request(port, "GET", "/api/catalog", host=f"127.0.0.1:{port + 1}")[0] == 404
            assert client.calls == 0
            status, _, body = request(port, "GET", "/api/catalog")
            assert status == 200
            assert json.loads(body) == client.catalog.as_dict()
            assert client.calls == 1
        assert not running.thread.is_alive()
        assert running.server.socket.fileno() == -1
        replacement = create_server(client, port=port)
        try:
            assert replacement.server_address == ("127.0.0.1", port)
            assert replacement.server_port == port
        finally:
            replacement.server_close()
        fqdn.assert_not_called()
        reverse.assert_not_called()


def test_occupied_numeric_bind_closes_failed_socket_without_reverse_dns():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupant:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            occupant.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        occupant.bind(("127.0.0.1", 0))
        occupant.listen(1)
        port = occupant.getsockname()[1]
        created = []
        original_socket = socket.socket

        def capture_socket(*args, **kwargs):
            result = original_socket(*args, **kwargs)
            created.append(result)
            return result

        with patch.object(socket, "socket", side_effect=capture_socket), patch.object(
            socket, "getfqdn", side_effect=AssertionError("reverse DNS forbidden")
        ), patch.object(
            socket, "gethostbyaddr", side_effect=AssertionError("reverse DNS forbidden")
        ), pytest.raises(OSError):
            create_server(port=port)
        assert len(created) == 1
        assert created[0].fileno() == -1
        assert occupant.getsockname() == ("127.0.0.1", port)


def test_catalog_route_returns_the_projection_and_uses_one_fetch():
    client = StubClient(catalog=valid_catalog())
    with RunningServer(client) as port:
        status, content_type, body = request(port, "GET", "/api/catalog")
    assert status == 200
    assert content_type == "application/json"
    payload = json.loads(body)
    assert [entry["package_path"] for entry in payload["entries"]] == [
        "Fictional/Queue/first",
        "Fictional/Under_Development/second",
    ]
    assert client.calls == 1


def test_catalog_route_accepts_a_callable_provider():
    calls = []

    def provider():
        calls.append(True)
        return raw_catalog()

    with RunningServer(provider) as port:
        status, _, body = request(port, "GET", "/api/catalog")
    assert status == 200
    assert json.loads(body)["entries"][0]["package_path"] == "Fictional/Queue/first"
    assert calls == [True]


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("schema-3", lambda value: value.update(schema_version=3)),
        ("unknown-inventory-key", lambda value: value["inventory"].update(extra=[])),
        ("projects-type", lambda value: value["inventory"].update(projects={})),
        (
            "duplicate-project",
            lambda value: value["inventory"]["projects"].append(
                dict(value["inventory"]["projects"][0])
            ),
        ),
        (
            "unordered-project",
            lambda value: value["inventory"]["projects"].append(
                {"name": "Alpha", "availability": "complete"}
            ),
        ),
        (
            "duplicate-stage-pair",
            lambda value: value["inventory"]["stages"].append(
                dict(value["inventory"]["stages"][-1])
            ),
        ),
        ("unordered-stage-pair", lambda value: value["inventory"]["stages"].reverse()),
        (
            "unsafe-project",
            lambda value: value["inventory"]["projects"][0].update(name="../outside"),
        ),
        (
            "missing-stage-parent",
            lambda value: value["inventory"]["stages"][0].update(project="Missing"),
        ),
        (
            "invalid-availability",
            lambda value: value["inventory"]["stages"][0].update(availability="unknown"),
        ),
        ("entry-not-admitted", lambda value: value["entries"][0].update(stage="Missing")),
        ("digest-mismatch", lambda value: value.update(catalog_digest="0" * 64)),
    ],
    ids=[
        "schema-3",
        "unknown-inventory-key",
        "projects-type",
        "duplicate-project",
        "unordered-project",
        "duplicate-stage-pair",
        "unordered-stage-pair",
        "unsafe-project",
        "missing-stage-parent",
        "invalid-availability",
        "entry-not-admitted",
        "digest-mismatch",
    ],
)
def test_catalog_route_rejects_malformed_inventory_before_serving(case, mutate):
    value = raw_catalog()
    mutate(value)
    if case != "digest-mismatch":
        value["catalog_digest"] = canonical_digest(value)

    with RunningServer(lambda: value) as port:
        status, content_type, body = request(port, "GET", "/api/catalog")

    assert status == 502
    assert content_type == "application/json"
    assert json.loads(body) == {"error": "producer_protocol_error"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["identity_coverage"].update({"diagnostics": {}}),
        lambda value: value["entries"][0]["relationship"].update(
            {
                "claims": [
                    {
                        "name": "release",
                        "state": "satisfied",
                        "evidence_ref": None,
                        "diagnostics": "not-a-list",
                    }
                ]
            }
        ),
        lambda value: value["entries"][0]["relationship"].update(
            {
                "prerequisites": [
                    {
                        "target_package_id": None,
                        "claim_name": None,
                        "observed_state": "invalid",
                        "observed_evidence_ref": None,
                        "resolved_state": "unknown",
                        "reason": "invalid_prerequisite",
                    }
                ]
            }
        ),
        lambda value: value["entries"][0]["relationship"]["prerequisites"][0].update(
            {"observed_evidence_ref": "unsafe"}
        ),
        lambda value: value["entries"][0]["relationship"].update(
            {
                "superseded_by": {
                    "package_id": None,
                    "resolution": "not_declared",
                    "diagnostics": [{"code": "invalid_package", "message": 1}],
                }
            }
        ),
        lambda value: value["entries"][0]["relationship"]["program"].update(
            {"diagnostics": [{"code": "invalid_package", "message": 1}]}
        ),
        lambda value: value.update(
            {
                "programs": [
                    {
                        "program_id": str(UUID(int=3, version=4)),
                        "title": "Program",
                        "member_package_ids": ["not-a-uuid"],
                        "diagnostics": [],
                    }
                ]
            }
        ),
    ],
    ids=[
        "coverage",
        "claims",
        "observed_state",
        "observed_evidence",
        "successor_diagnostics",
        "program_diagnostics",
        "program_members",
    ],
)
def test_digest_valid_malformed_nested_protocol_fields_return_safe_error(mutate):
    value = raw_catalog()
    mutate(value)
    value["catalog_digest"] = canonical_digest(value)

    with RunningServer(lambda: value) as port:
        status, content_type, body = request(port, "GET", "/api/catalog")

    assert status == 502
    assert content_type == "application/json"
    assert json.loads(body) == {"error": "producer_protocol_error"}


@pytest.mark.parametrize(
    "path",
    ["/api/catalog/check", "/api/other", "/../secrets", "/static/../app.js", "/missing"],
)
def test_unknown_routes_are_not_found(path):
    with RunningServer(StubClient(catalog=valid_catalog())) as port:
        status, _, _ = request(port, "GET", path)
    assert status == 404


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_mutating_methods_are_rejected(method):
    client = StubClient(catalog=valid_catalog())
    with RunningServer(client) as port:
        status, _, _ = request(port, method, "/api/catalog")
    assert status == 405
    assert client.calls == 0


def test_head_returns_headers_without_a_body():
    with RunningServer(StubClient(catalog=valid_catalog())) as port:
        status, _, body = request(port, "HEAD", "/api/catalog")
    assert status == 200
    assert body == b""


@pytest.mark.parametrize(
    "host",
    ["evil.example", "127.0.0.1:1", "localhost", "127.0.0.1"],
)
def test_non_loopback_and_mismatched_host_headers_are_hidden(host):
    with RunningServer(StubClient(catalog=valid_catalog())) as port:
        status, _, _ = request(port, "GET", "/api/catalog", host=host)
    assert status == 404


def test_loopback_host_aliases_are_accepted():
    for host_template in ("127.0.0.1:{port}", "localhost:{port}"):
        with RunningServer(StubClient(catalog=valid_catalog())) as port:
            host = host_template.format(port=port)
            status, _, _ = request(port, "GET", "/api/catalog", host=host)
        assert status == 200


def test_standalone_server_keeps_settings_route_read_only_and_hidden():
    with RunningServer(StubClient(catalog=valid_catalog())) as port:
        assert request(port, "GET", "/api/settings")[0] == 404
        status, _, _ = request(
            port,
            "PUT",
            "/api/settings",
            body=json.dumps({"revision": "opaque", "order": ["Queue"]}),
        )
        assert status == 404


def test_application_settings_route_enforces_host_methods_payload_and_opaque_result():
    settings = SettingsStub()
    with RunningServer(StubClient(catalog=valid_catalog()), settings) as port:
        status, content_type, body = request(port, "GET", "/api/settings")
        assert status == 200
        assert content_type == "application/json"
        assert json.loads(body) == {"order": ["Queue", "Done"], "revision": "opaque"}

        status, _, body = request(
            port,
            "PUT",
            "/api/settings",
            body=json.dumps({"revision": "opaque", "order": ["Done", "Queue"]}),
        )
        assert status == 200
        assert json.loads(body) == {
            "order": ["Done", "Queue"],
            "revision": "new-opaque",
            "outcome": "success",
        }
        assert settings.calls == [("opaque", ["Done", "Queue"])]

        assert request(port, "POST", "/api/settings")[0] == 405
        assert request(port, "GET", "/api/settings", host=f"outside.invalid:{port}")[0] == 404
        assert request(port, "PUT", "/api/settings", body=b"not-json")[0] == 400
        assert request(port, "PUT", "/api/settings", body=json.dumps({"revision": "new-opaque", "order": []}), host=f"127.0.0.1:{port + 1}")[0] == 404


def test_application_settings_route_returns_conflict_without_replacement():
    settings = SettingsStub()
    with RunningServer(StubClient(catalog=valid_catalog()), settings) as port:
        status, content_type, body = request(
            port,
            "PUT",
            "/api/settings",
            body=json.dumps({"revision": "stale", "order": ["Archive"]}),
        )
    assert status == 409
    assert content_type == "application/json"
    assert json.loads(body) == {
        "order": ["Queue", "Done"],
        "revision": "opaque",
        "outcome": "conflict",
    }
    assert settings.calls == []


def test_application_settings_put_returns_safe_error_when_admission_closes():
    class UnavailableSettings:
        def save_settings(self, revision, order):
            raise CatalogError("settings_unavailable")

    with RunningServer(
        StubClient(catalog=valid_catalog()), UnavailableSettings()
    ) as port:
        status, content_type, body = request(
            port,
            "PUT",
            "/api/settings",
            body=json.dumps({"revision": "opaque", "order": ["Queue"]}),
        )

    assert status == 503
    assert content_type == "application/json"
    assert json.loads(body) == {"error": "settings_unavailable"}


@pytest.mark.parametrize(
    "category",
    [
        "producer_unavailable",
        "producer_timeout",
        "producer_failed",
        "producer_output_too_large",
        "producer_protocol_error",
    ],
)
def test_producer_failures_surface_only_a_safe_category(category):
    with RunningServer(StubClient(error=category)) as port:
        status, content_type, body = request(port, "GET", "/api/catalog")
    assert status == 502
    assert content_type == "application/json"
    assert json.loads(body) == {"error": category}


def malformed_provider():
    return b"not a catalog"


def failed_provider():
    raise RuntimeError("private provider details")


@pytest.mark.parametrize(
    ("provider", "category"),
    [
        (malformed_provider, "producer_protocol_error"),
        (failed_provider, "producer_failed"),
    ],
)
def test_provider_failures_are_normalized_without_leaking_details(provider, category):
    with RunningServer(provider) as port:
        status, content_type, body = request(port, "GET", "/api/catalog")
    assert status == 502
    assert content_type == "application/json"
    assert json.loads(body) == {"error": category}
    assert b"private provider details" not in body


def test_static_assets_are_served_from_the_fixed_allowlist():
    for path, expected_type in (
        ("/", "text/html; charset=utf-8"),
        ("/static/app.js", "text/javascript; charset=utf-8"),
        ("/static/theme.js", "text/javascript; charset=utf-8"),
        ("/static/style.css", "text/css; charset=utf-8"),
    ):
        with RunningServer(StubClient(catalog=valid_catalog())) as port:
            status, content_type, body = request(port, "GET", path)
        assert status == 200
        assert content_type == expected_type
        assert body
