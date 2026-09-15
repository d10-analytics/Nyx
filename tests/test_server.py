import http.client
import json
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
        "schema_version": 3,
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
        lambda value: value.update(schema_version=2),
        lambda value: value.update(unknown=True),
        lambda value: value["entries"][0].update(board_visible="true"),
        lambda value: value["entries"][0].update(stage="Done"),
        lambda value: value["entries"][0].update(
            transitive_diagnostics=[{"code": "transitive_diagnostics_truncated"}]
        ),
    ],
    ids=["schema-2", "unknown-key", "nonboolean-visibility", "policy-mismatch", "transitive-reference"],
)
def test_schema_three_parser_rejects_legacy_unknown_and_mutated_payloads(mutate):
    value = raw_catalog()
    mutate(value)
    value["catalog_digest"] = canonical_digest(value)
    with pytest.raises(ValueError):
        parse_catalog(value)


def test_schema_three_parser_rejects_bad_digest_even_when_shape_is_valid():
    value = raw_catalog()
    value["catalog_digest"] = "0" * 64
    with pytest.raises(ValueError):
        parse_catalog(value)


@pytest.mark.parametrize("alter", [
    lambda catalog: replace(catalog, schema_version=2),
    lambda catalog: replace(
        catalog,
        entries=(replace(catalog.entries[0], board_visible="yes"), *catalog.entries[1:]),
    ),
])
def test_catalog_object_providers_are_revalidated_as_schema_three(alter):
    with pytest.raises(CatalogError, match="producer_protocol_error"):
        server._catalog_from_provider(lambda: alter(valid_catalog()))


class RunningServer:
    def __init__(self, client):
        self.server = create_server(client)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def __enter__(self):
        return self.server.server_port

    def __exit__(self, *exc):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


def request(port, method, path, host=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {} if host is None else {"Host": host}
    connection.request(method, path, headers=headers)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, response.getheader("Content-Type"), body


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
