import http.client
import json
import socket
import threading
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import UUID

import pytest

from nyx import server, state
from nyx.app_runtime import ApplicationRuntime
from nyx.models import SCHEMA_VERSION, ProtocolError, canonical_digest, parse_catalog
from nyx.server import CatalogError, _create_application_server, create_server


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
        "schema_version": SCHEMA_VERSION,
        "configuration_revision": None,
        "inventory": {
            "projects": [{"name": "Fictional", "availability": "complete"}],
            "stages": [
                {"project": "Fictional", "stage": "Queue", "availability": "complete"},
                {"project": "Fictional", "stage": "Under_Development", "availability": "complete"},
            ],
        },
        "terminal_stages": [],
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
            "declared": {"title": title, "target_project": "Fictional",
                         "status": "ready"},
            "relationship": {"participation": "available", "claims": [],
                              "prerequisites": [],
                              "direct_prerequisite_state": "no_declared_prerequisites",
                              "program": {"program_id": None, "title": None,
                                          "resolution": "not_declared"},
                              "superseded_by": {"package_id": None,
                                                "resolution": "not_declared"}},
            "reported_fields": [],
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
            }
        ],
        "prerequisites": [
            {
                "kind": "claim",
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
        },
        "superseded_by": {
            "package_id": second["package_id"],
            "resolution": "resolved",
        },
    }
    value["programs"] = [
        {
            "program_id": program_id,
            "title": "Test program",
            "member_package_ids": [first["package_id"]],
        }
    ]
    value["catalog_digest"] = canonical_digest(value)
    return value


def valid_catalog():
    return parse_catalog(raw_catalog())


def test_default_provider_uses_real_scanner_and_emits_every_stage(tmp_path, monkeypatch):
    stages = ("Archive", "Awaiting_Retrospective", "Done", "In_Progress",
              "Needs_Fixes", "Queue", "Under_Development")
    for stage in stages:
        package = tmp_path / "Fictional" / stage / "item"
        package.mkdir(parents=True)
        (package / "spec.md").write_text(f"# {stage}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = server._default_provider().as_dict()
    assert result["terminal_stages"] == []
    assert [entry["package_path"] for entry in result["entries"]] == [
        f"Fictional/{stage}/item" for stage in stages
    ]
    assert all("board_visible" not in entry for entry in result["entries"])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(schema_version=3),
        lambda value: value.update(schema_version=4),
        lambda value: value.update(schema_version=5),
        lambda value: value.update(schema_version=6),
        lambda value: value.update(schema_version=7),
        lambda value: value.pop("configuration_revision"),
        lambda value: value.update(configuration_revision=[]),
        lambda value: value.update(unknown=True),
        lambda value: value["entries"][0].update(board_visible=True),
        lambda value: value.update(visibility={}),
        lambda value: value.pop("terminal_stages"),
        lambda value: value.update(terminal_stages=["Done", "Archive"]),
        lambda value: value.update(terminal_stages=["Done", "Done"]),
        lambda value: value["entries"][0].update(stage="Done"),
        lambda value: value["entries"][0]["relationship"]["prerequisites"][0].update(kind="completion"),
        lambda value: value["entries"][0].update(
            transitive_diagnostics=[{"code": "transitive_diagnostics_truncated"}]
        ),
    ],
    ids=["schema-3", "schema-4", "schema-5", "schema-6", "schema-7", "missing-revision", "malformed-revision", "unknown-key", "old-entry-field", "old-object", "missing-terminal", "unsorted-terminal", "duplicate-terminal", "inventory-mismatch", "cross-kind-edge", "transitive-reference"],
)
def test_schema_four_parser_rejects_legacy_unknown_and_mutated_payloads(mutate):
    value = raw_catalog()
    mutate(value)
    value["catalog_digest"] = canonical_digest(value)
    with pytest.raises(ProtocolError) as error:
        parse_catalog(value)
    assert str(error.value) != "catalog digest mismatch"


def test_schema_four_parser_rejects_bad_digest_even_when_shape_is_valid():
    value = raw_catalog()
    value["catalog_digest"] = "0" * 64
    with pytest.raises(ValueError):
        parse_catalog(value)


@pytest.mark.parametrize("alter", [
    lambda catalog: replace(catalog, schema_version=3),
    lambda catalog: replace(
        catalog,
        terminal_stages=("Done", "Done"),
    ),
])
def test_catalog_object_providers_are_revalidated_as_schema_four(alter):
    value = alter(valid_catalog())
    value = replace(value, catalog_digest=canonical_digest(value.as_dict()))
    with pytest.raises(CatalogError, match="producer_protocol_error"):
        server._catalog_from_provider(lambda: value)


class RunningServer:
    def __init__(self, client, settings=None):
        self.application = None
        if settings is None:
            self.server = create_server(client)
            self.thread = threading.Thread(target=self.server.serve_forever)
            self.thread.start()
            return

        if isinstance(settings, ApplicationRuntime):
            self.application = settings
        else:
            self.application = ApplicationRuntime(
                port=0,
                deadline=time.monotonic() + 5,
            )
            if hasattr(settings, "get_settings"):
                self.application.get_settings = settings.get_settings
            if hasattr(settings, "save_settings"):
                self.application.save_settings = settings.save_settings

        def application_server_factory(**kwargs):
            return _create_application_server(
                provider=client,
                port=kwargs["port"],
                settings_provider=kwargs["settings_provider"],
            )

        self.application._server_factory = application_server_factory
        self.application.start(static_ready=lambda: True)
        self.server = self.application.server
        self.thread = self.application.http_thread

    def __enter__(self):
        return self.server.server_port

    def __exit__(self, *exc):
        if self.application is not None:
            assert self.application.shutdown(time.monotonic() + 2)
            self.application.server = None
            return
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
        self.value = {
            "order": ["Queue", "Done"],
            "completed": ["Done"],
            "revision": "opaque",
        }
        self.calls = []

    def get_settings(self):
        return dict(self.value)

    def save_settings(self, revision, order, completed):
        if revision != self.value["revision"]:
            return {**self.value, "outcome": "conflict"}
        self.calls.append((revision, order, completed))
        self.value = {
            "order": list(order),
            "completed": list(completed),
            "revision": "new-opaque",
        }
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
                        "kind": "claim",
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
    settings = SettingsStub()
    with RunningServer(StubClient(catalog=valid_catalog())) as port:
        assert request(port, "GET", "/api/settings")[0] == 404
        status, _, _ = request(
            port,
            "PUT",
            "/api/settings",
            body=json.dumps({"revision": "opaque", "order": ["Queue"]}),
        )
        assert status == 404
    assert settings.calls == []
    with pytest.raises(TypeError, match="settings_provider"):
        create_server(StubClient(catalog=valid_catalog()), settings_provider=settings)
    with pytest.raises(TypeError, match="settings_provider"):
        server.TrackerServer(
            StubClient(catalog=valid_catalog()), settings_provider=settings
        )


def test_application_settings_route_enforces_host_methods_payload_and_opaque_result():
    settings = SettingsStub()
    with RunningServer(StubClient(catalog=valid_catalog()), settings) as port:
        status, content_type, body = request(port, "GET", "/api/settings")
        assert status == 200
        assert content_type == "application/json"
        assert json.loads(body) == {
            "order": ["Queue", "Done"],
            "completed": ["Done"],
            "revision": "opaque",
        }

        status, _, body = request(
            port,
            "PUT",
            "/api/settings",
            body=json.dumps(
                {
                    "revision": "opaque",
                    "order": ["Done", "Queue"],
                    "completed": ["Queue"],
                }
            ),
        )
        assert status == 200
        assert json.loads(body) == {
            "order": ["Done", "Queue"],
            "completed": ["Queue"],
            "revision": "new-opaque",
            "outcome": "success",
        }
        assert settings.calls == [("opaque", ["Done", "Queue"], ["Queue"])]

        assert request(port, "POST", "/api/settings")[0] == 405
        assert request(port, "GET", "/api/settings", host=f"outside.invalid:{port}")[0] == 404
        assert request(port, "PUT", "/api/settings", body=b"not-json")[0] == 400
        assert request(
            port,
            "PUT",
            "/api/settings",
            body=json.dumps(
                {"revision": "new-opaque", "order": [], "completed": []}
            ),
            host=f"127.0.0.1:{port + 1}",
        )[0] == 404


def test_application_settings_route_returns_conflict_without_replacement():
    settings = SettingsStub()
    with RunningServer(StubClient(catalog=valid_catalog()), settings) as port:
        status, content_type, body = request(
            port,
            "PUT",
            "/api/settings",
            body=json.dumps(
                {
                    "revision": "stale",
                    "order": ["Archive"],
                    "completed": ["Archive"],
                }
            ),
        )
    assert status == 409
    assert content_type == "application/json"
    assert json.loads(body) == {
        "order": ["Queue", "Done"],
        "completed": ["Done"],
        "revision": "opaque",
        "outcome": "conflict",
    }
    assert settings.calls == []


def test_application_settings_put_returns_safe_error_when_admission_closes():
    class UnavailableSettings:
        def save_settings(self, revision, order, completed):
            raise CatalogError("settings_unavailable")

    with RunningServer(
        StubClient(catalog=valid_catalog()), UnavailableSettings()
    ) as port:
        status, content_type, body = request(
            port,
            "PUT",
            "/api/settings",
            body=json.dumps(
                {"revision": "opaque", "order": ["Queue"], "completed": ["Queue"]}
            ),
        )

    assert status == 503
    assert content_type == "application/json"
    assert json.loads(body) == {"error": "settings_unavailable"}


def test_real_application_settings_http_rejects_stale_and_invalid_writes_and_preserves_failure():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        specification_root = root / "specifications"
        specification_root.mkdir()
        with patch.object(state, "resolve_account_home", return_value=home):
            initial = state.setup(specification_root)
            paths = state.state_paths()

        application = ApplicationRuntime(port=0, deadline=time.monotonic() + 5)
        application.capture_configuration(initial, paths)
        application.admit_catalog()
        try:
            with RunningServer(StubClient(catalog=valid_catalog()), application) as port:
                status, content_type, body = request(port, "GET", "/api/settings")
                first = json.loads(body)
                assert status == 200
                assert content_type == "application/json"
                assert first == {
                    "order": [],
                    "completed": [],
                    "revision": initial.revision,
                }
                assert str(specification_root) not in body.decode("utf-8")

                status, content_type, body = request(port, "GET", "/api/workspace")
                assert status == 200
                assert content_type == "application/json"
                assert json.loads(body) == {
                    "root": str(initial.specification_root),
                    "revision": initial.revision,
                }

                status, _, body = request(
                    port,
                    "PUT",
                    "/api/settings",
                    body=json.dumps(
                        {
                            "revision": first["revision"],
                            "order": ["Queue"],
                            "completed": ["Queue"],
                        }
                    ),
                )
                saved = json.loads(body)
                assert status == 200
                assert saved["outcome"] == "success"
                assert saved["order"] == ["Queue"]
                assert saved["revision"] != first["revision"]

                with patch.object(
                    state.os,
                    "replace",
                    side_effect=AssertionError("stale write reached replacement"),
                ) as replace_record:
                    status, _, body = request(
                        port,
                        "PUT",
                        "/api/settings",
                        body=json.dumps(
                            {
                                "revision": first["revision"],
                                "order": ["Archive"],
                                "completed": ["Archive"],
                            }
                        ),
                    )
                assert status == 409
                assert json.loads(body) == {**saved, "outcome": "conflict"}
                replace_record.assert_not_called()

                for invalid_order in (["Queue", "Queue"], ["Queue/Done"]):
                    status, _, body = request(
                        port,
                        "PUT",
                        "/api/settings",
                        body=json.dumps(
                            {
                                "revision": saved["revision"],
                                "order": invalid_order,
                                "completed": [],
                            }
                        ),
                    )
                    assert status == 400
                    assert json.loads(body) == {"error": "invalid_order"}

                with patch.object(state.os, "replace", side_effect=OSError("injected")):
                    status, _, body = request(
                        port,
                        "PUT",
                        "/api/settings",
                        body=json.dumps(
                            {
                                "revision": saved["revision"],
                                "order": ["Done"],
                                "completed": ["Done"],
                            }
                        ),
                    )
                assert status == 500
                assert json.loads(body) == {**saved, "outcome": "failure"}
                assert state.load_configuration(paths).stage_order == ("Queue",)
        finally:
            assert application.shutdown(time.monotonic() + 2)

        restarted = ApplicationRuntime(port=0, deadline=time.monotonic() + 5)
        restarted.capture_configuration(state.load_configuration(paths), paths)
        restarted.admit_catalog()
        try:
            with RunningServer(StubClient(catalog=valid_catalog()), restarted) as port:
                status, _, body = request(port, "GET", "/api/settings")
            assert status == 200
            assert json.loads(body) == {
                "order": ["Queue"],
                "completed": ["Queue"],
                "revision": state.configuration_revision(paths),
            }
        finally:
            assert restarted.shutdown(time.monotonic() + 2)


def test_real_application_settings_http_revalidates_after_commit_verification_failure():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        specification_root = root / "specifications"
        specification_root.mkdir()
        with patch.object(state, "resolve_account_home", return_value=home):
            initial = state.setup(specification_root)
            paths = state.state_paths()

        application = ApplicationRuntime(port=0, deadline=time.monotonic() + 5)
        application.capture_configuration(initial, paths)
        application.admit_catalog()
        original_verify = state._verify_record
        failed = False

        def fail_first_commit_verification(path):
            nonlocal failed
            details = original_verify(path)
            if path == paths.config_file and not failed:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if ["Queue", "Done"] in payload.get("stage_orders", {}).values():
                    failed = True
                    raise OSError("injected post-replacement verification failure")
            return details

        try:
            with RunningServer(StubClient(catalog=valid_catalog()), application) as port:
                with patch.object(
                    state, "_verify_record", side_effect=fail_first_commit_verification
                ):
                    status, _, body = request(
                        port,
                        "PUT",
                        "/api/settings",
                        body=json.dumps(
                            {
                                "revision": initial.revision,
                                "order": ["Queue", "Done"],
                                "completed": ["Done"],
                            }
                        ),
                    )
            result = json.loads(body)
            assert failed is True
            assert status == 200
            assert result["outcome"] == "success"
            assert result["order"] == ["Queue", "Done"]
            assert result["revision"] == state.configuration_revision(paths)
        finally:
            assert application.shutdown(time.monotonic() + 2)


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
        ("/static/d10-diamond.svg", "image/svg+xml"),
        ("/static/Anta-Regular.ttf", "font/ttf"),
    ):
        with RunningServer(StubClient(catalog=valid_catalog())) as port:
            status, content_type, body = request(port, "GET", path)
        assert status == 200
        assert content_type == expected_type
        assert body
        if path.startswith("/static/"):
            assert body == (Path(__file__).parents[1] / "nyx" / path.lstrip("/")).read_bytes()


def reported_contract_cases():
    """Shared protocol examples exercised at Python and browser acceptance points."""
    cases = []

    def add(name, mutate, accepted=False):
        value = raw_catalog()
        mutate(value, value["entries"][0])
        value["catalog_digest"] = canonical_digest(value)
        cases.append(pytest.param(value, accepted, id=name))

    add("extra-declared", lambda v, e: e["declared"].update(closure="approved"))
    add("missing-declared", lambda v, e: e["declared"].pop("status"))
    records = [
        ("not-list", {}),
        ("extra-record-key", [{"name": "Note", "value": "ok", "extra": 1}]),
        ("missing-value", [{"name": "Note"}]),
        ("sixty-five", [{"name": f"N{i:02}", "value": "ok"} for i in range(65)]),
        ("unsorted", [{"name": "Z", "value": "ok"}, {"name": "A", "value": "ok"}]),
        ("duplicate", [{"name": "Note", "value": "ok"}] * 2),
        ("ascii-case-duplicate", [{"name": "CLOSURE", "value": "a"}, {"name": "Closure", "value": "b"}]),
        ("ascii-overlong", [{"name": "Note", "value": "a" * 1025}]),
        ("astral-overlong", [{"name": "Note", "value": "😀" * 513}]),
        ("empty-value", [{"name": "Note", "value": ""}]),
        ("tab-value", [{"name": "Note", "value": "a\tb"}]),
        ("control-value", [{"name": "Note", "value": "a\x7fb"}]),
        ("surrogate-value", [{"name": "Note", "value": "\ud800"}]),
        ("empty-name", [{"name": "", "value": "ok"}]),
        ("control-name", [{"name": "N\t", "value": "ok"}]),
        ("surrogate-name", [{"name": "N\ud800", "value": "ok"}]),
        ("utf16-order", [{"name": "X😀", "value": "a"}, {"name": "X�", "value": "b"}]),
    ]
    for name, fields in records:
        add(name, lambda v, e, fields=fields: e.update(reported_fields=fields))
    add("tab-title", lambda v, e: e["declared"].update(title="a\tb"))
    add("overlong-title", lambda v, e: e["declared"].update(title="a" * 1025))
    add("entry-state", lambda v, e: e.update(state="complete"))
    add("entry-diagnostics", lambda v, e: e.update(diagnostics=[]))
    add("transitive", lambda v, e: e.update(transitive_diagnostics=[]))
    add("claim-diagnostics", lambda v, e: e["relationship"]["claims"][0].update(diagnostics=[]))
    add("program-diagnostics", lambda v, e: e["relationship"]["program"].update(diagnostics=[]))
    add("successor-diagnostics", lambda v, e: e["relationship"]["superseded_by"].update(diagnostics=[]))
    add("catalog-program-diagnostics", lambda v, e: v["programs"][0].update(diagnostics=[]))
    for name, fields in [
        ("sixty-four", [{"name": f"N{i:02}", "value": "ok"} for i in range(64)]),
        ("astral-limit", [{"name": "Note", "value": "😀" * 512}]),
        ("unicode-distinct", [{"name": "XÄ", "value": "a"}, {"name": "Xä", "value": "b"}]),
        ("scalar-order", [{"name": "X�", "value": "a"}, {"name": "X😀", "value": "b"}]),
    ]:
        add(name, lambda v, e, fields=fields: e.update(reported_fields=fields), True)
    return cases


@pytest.mark.parametrize("value,accepted", reported_contract_cases())
def test_reported_contract_at_python_acceptance(value, accepted):
    assert value["catalog_digest"] == canonical_digest(value)
    if accepted:
        assert parse_catalog(value).as_dict() == value
    else:
        with pytest.raises(ProtocolError) as error:
            parse_catalog(value)
        assert str(error.value) != "catalog digest mismatch"


# --- Request framing, close rule and settings-PUT guards (AC-01, AC-05) ---

UNCHANGED_SETTINGS = {"order": ["Queue", "Done"], "completed": ["Done"], "revision": "opaque"}


def settings_body(revision="opaque"):
    return json.dumps(
        {"revision": revision, "order": [], "completed": []},
        separators=(",", ":"),
    ).encode()


def embedded_put(port, revision):
    """A complete savable PUT request; the declared body of every AC-01 vector."""
    body = settings_body(revision)
    head = (
        "PUT /api/settings HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "\r\n"
    ).encode("ascii")
    return head + body


def raw_request(port, first_line, *, host=None, body=b"", content_length=None, header_lines=()):
    """Assemble one raw HTTP request; header_lines are emitted verbatim."""
    lines = [
        first_line,
        "Host: " + (host if host is not None else f"127.0.0.1:{port}"),
        "Content-Type: application/json",
    ]
    if content_length is not None:
        lines.append(f"Content-Length: {content_length}")
    lines.extend(header_lines)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1") + body


def send_and_drain(port, payload):
    """Send raw bytes and collect every response byte until close or reset."""
    chunks = []
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.settimeout(5)
        try:
            connection.sendall(payload)
        except OSError:
            # The server may close early on an over-bound body before the
            # client finishes sending; the caller asserts what arrived.
            return b"".join(chunks)
        try:
            while True:
                piece = connection.recv(65536)
                if not piece:
                    break
                chunks.append(piece)
        except ConnectionResetError:
            pass  # a reset after the complete response is accepted
    return b"".join(chunks)


def one_response_with_close(data):
    """Assert exactly one response with Connection: close; return head and body."""
    assert data.count(b"HTTP/1.1") == 1
    head, separator, body = data.partition(b"\r\n\r\n")
    assert separator
    assert b"connection: close" in head.lower()
    return head, body


def response_status(head):
    return int(head.split(b" ", 2)[1])


def assert_unchanged_settings(port):
    status, _, body = request(port, "GET", "/api/settings")
    assert status == 200
    assert json.loads(body) == UNCHANGED_SETTINGS


DECLARED_BODY_CASES = (
    "post-root",
    "post-catalog",
    "delete-settings",
    "options-settings",
    "patch-workspace",
    "unknown-method",
    "unknown-method-chunked",
    "missing-route",
    "head-index",
    "get-index",
    "settings-host-mismatch",
    "settings-over-bound",
)


def declared_body_vector(name, port, revision):
    """Build each AC-01 vector; the baseline double-responds and saves the PUT."""
    put = embedded_put(port, revision)
    simple = {
        "post-root": ("POST / HTTP/1.1", 405, b"Method not allowed\n"),
        "post-catalog": ("POST /api/catalog HTTP/1.1", 405, b"Method not allowed\n"),
        "delete-settings": ("DELETE /api/settings HTTP/1.1", 405, b"Method not allowed\n"),
        "options-settings": ("OPTIONS /api/settings HTTP/1.1", 405, b"Method not allowed\n"),
        "patch-workspace": ("PATCH /api/workspace HTTP/1.1", 405, b"Method not allowed\n"),
        "unknown-method": ("FOO / HTTP/1.1", 405, b"Method not allowed\n"),
        "missing-route": ("GET /nope HTTP/1.1", 404, b"Not found\n"),
        "head-index": ("HEAD / HTTP/1.1", 200, b""),
        "settings-host-mismatch": ("POST /api/settings HTTP/1.1", 404, b"Not found\n"),
    }
    if name == "unknown-method-chunked":
        return (
            raw_request(
                port, "FOO / HTTP/1.1", body=put,
                header_lines=["Transfer-Encoding: chunked"],
            ),
            405,
            b"Method not allowed\n",
        )
    if name == "settings-over-bound":
        return (
            raw_request(
                port, "PUT /api/settings HTTP/1.1", body=put,
                content_length=1024 * 1024 + 1,
            ),
            400,
            b'{"error":"invalid_payload"}',
        )
    if name == "get-index":
        return (
            raw_request(port, "GET / HTTP/1.1", body=put, content_length=len(put)),
            200,
            server._STATIC_ROOT.joinpath("index.html").read_bytes(),
        )
    first_line, status, body = simple[name]
    host = f"outside.invalid:{port}" if name == "settings-host-mismatch" else None
    return (
        raw_request(port, first_line, host=host, body=put, content_length=len(put)),
        status,
        body,
    )


@pytest.mark.parametrize("case", DECLARED_BODY_CASES)
def test_declared_body_requests_are_answered_once_then_closed_without_saving(case):
    settings = SettingsStub()
    with RunningServer(StubClient(catalog=valid_catalog()), settings) as port:
        payload, expected_status, expected_body = declared_body_vector(case, port, "opaque")
        head, body = one_response_with_close(send_and_drain(port, payload))
        assert response_status(head) == expected_status
        assert body == expected_body
        assert settings.calls == []
        assert_unchanged_settings(port)


def test_standalone_settings_put_with_declared_body_is_404_then_close():
    with RunningServer(StubClient(catalog=valid_catalog())) as port:
        put = embedded_put(port, "opaque")
        payload = raw_request(port, "PUT /api/settings HTTP/1.1", body=put, content_length=len(put))
        head, body = one_response_with_close(send_and_drain(port, payload))
    assert response_status(head) == 404
    assert body == b"Not found\n"


INVALID_FRAMING_CASES = (
    "chunked-without-length",
    "chunked-and-length",
    "duplicate-length",
    "plus-prefixed-length",
    "underscored-length",
    "non-ascii-length",
    "zero-padded-length",
)


def invalid_framing_vector(name, port, revision):
    """Build each AC-01 invalid-framing vector; a lenient parse would save."""
    body = settings_body(revision)
    if name == "chunked-without-length":
        return raw_request(
            port, "PUT /api/settings HTTP/1.1", body=body,
            header_lines=["Transfer-Encoding: chunked"],
        )
    if name == "chunked-and-length":
        return raw_request(
            port, "PUT /api/settings HTTP/1.1", body=body,
            content_length=len(body),
            header_lines=["Transfer-Encoding: chunked"],
        )
    if name == "duplicate-length":
        return raw_request(
            port, "PUT /api/settings HTTP/1.1", body=body,
            header_lines=[f"Content-Length: {len(body)}", "Content-Length: 0"],
        )
    if name == "plus-prefixed-length":
        return raw_request(
            port, "PUT /api/settings HTTP/1.1", body=body,
            header_lines=[f"Content-Length: +{len(body)}"],
        )
    if name == "underscored-length":
        padded = body.ljust(50)  # int("5_0") == 50
        assert len(padded) == 50
        return raw_request(
            port, "PUT /api/settings HTTP/1.1", body=padded,
            header_lines=["Content-Length: 5_0"],
        )
    if name == "non-ascii-length":
        # The UTF-8 bytes of an Arabic-Indic digit arrive decoded as
        # ISO-8859-1, so no lenient parser can read them as a number (F-05);
        # this vector is close-rule coverage only.
        return raw_request(
            port, "PUT /api/settings HTTP/1.1", body=body,
            header_lines=["Content-Length: \xd9\xa5"],
        )
    assert name == "zero-padded-length"
    return raw_request(
        port, "PUT /api/settings HTTP/1.1", body=body,
        header_lines=[f"Content-Length: {len(body):011d}"],
    )


@pytest.mark.parametrize("case", INVALID_FRAMING_CASES)
def test_invalid_framing_is_answered_400_then_closed_without_saving(case):
    settings = SettingsStub()
    with RunningServer(StubClient(catalog=valid_catalog()), settings) as port:
        payload = invalid_framing_vector(case, port, "opaque")
        head, body = one_response_with_close(send_and_drain(port, payload))
        assert response_status(head) == 400
        assert json.loads(body) == {"error": "invalid_payload"}
        assert settings.calls == []
        assert_unchanged_settings(port)


def test_invalid_framing_with_mismatched_host_stays_404_and_closes():
    settings = SettingsStub()
    with RunningServer(StubClient(catalog=valid_catalog()), settings) as port:
        payload = raw_request(
            port, "PUT /api/settings HTTP/1.1", host=f"outside.invalid:{port}",
            body=settings_body("opaque"),
            header_lines=["Transfer-Encoding: chunked"],
        )
        head, body = one_response_with_close(send_and_drain(port, payload))
        assert response_status(head) == 404
        assert body == b"Not found\n"
        assert b"text/plain; charset=utf-8" in head
        assert settings.calls == []


def test_valid_settings_put_still_saves_and_closes():
    settings = SettingsStub()
    with RunningServer(StubClient(catalog=valid_catalog()), settings) as port:
        body = settings_body("opaque")
        payload = raw_request(
            port, "PUT /api/settings HTTP/1.1", body=body, content_length=len(body)
        )
        head, body = one_response_with_close(send_and_drain(port, payload))
        assert response_status(head) == 200
        assert json.loads(body)["outcome"] == "success"
        assert settings.calls == [("opaque", [], [])]


def test_pipelined_bodyless_gets_both_answer_on_one_connection():
    settings = SettingsStub()
    with RunningServer(StubClient(catalog=valid_catalog()), settings) as port:
        pipelined = raw_request(port, "GET /api/settings HTTP/1.1") * 2
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            connection.settimeout(5)
            connection.sendall(pipelined)
            data = b""
            while data.count(b"HTTP/1.1") < 2:
                piece = connection.recv(65536)
                if not piece:
                    break
                data += piece
            try:
                connection.settimeout(0.5)
                data += connection.recv(65536)
            except TimeoutError:
                pass  # the connection stays open after both responses
        assert data.count(b"HTTP/1.1") == 2
        parts = data.split(b"HTTP/1.1")
        assert len(parts) == 3
        first_head, _, first_body = parts[1].partition(b"\r\n\r\n")
        second_head, _, second_body = parts[2].partition(b"\r\n\r\n")
        assert first_head.startswith(b" 200") and second_head.startswith(b" 200")
        assert b"connection: close" not in (first_head + second_head).lower()
        assert json.loads(first_body) == UNCHANGED_SETTINGS
        assert json.loads(second_body) == UNCHANGED_SETTINGS
        assert settings.calls == []


def test_settings_put_rejects_extra_key_without_saving():
    settings = SettingsStub()
    with RunningServer(StubClient(catalog=valid_catalog()), settings) as port:
        body = json.dumps(
            {"revision": "opaque", "order": [], "completed": [], "extra": True}
        ).encode()
        status, _, response = request(port, "PUT", "/api/settings", body=body)
        assert status == 400
        assert json.loads(response) == {"error": "invalid_payload"}
        assert settings.calls == []
        assert_unchanged_settings(port)


def test_settings_put_rejects_missing_completed_key_without_saving():
    settings = SettingsStub()
    with RunningServer(StubClient(catalog=valid_catalog()), settings) as port:
        body = json.dumps({"revision": "opaque", "order": []}).encode()
        status, _, response = request(port, "PUT", "/api/settings", body=body)
        assert status == 400
        assert json.loads(response) == {"error": "invalid_payload"}
        assert settings.calls == []
        assert_unchanged_settings(port)


def test_settings_put_accepts_a_valid_body_of_exactly_one_mebibyte():
    settings = SettingsStub()
    with RunningServer(StubClient(catalog=valid_catalog()), settings) as port:
        body = settings_body("opaque").ljust(1024 * 1024)
        assert len(body) == 1024 * 1024
        status, _, response = request(port, "PUT", "/api/settings", body=body)
        assert status == 200
        assert json.loads(response)["outcome"] == "success"
        assert settings.calls == [("opaque", [], [])]


def test_settings_put_rejects_one_byte_over_the_bound_and_keeps_state():
    settings = SettingsStub()
    with RunningServer(StubClient(catalog=valid_catalog()), settings) as port:
        body = settings_body("opaque").ljust(1024 * 1024 + 1)
        assert len(body) == 1024 * 1024 + 1
        payload = raw_request(
            port, "PUT /api/settings HTTP/1.1", body=body, content_length=len(body)
        )
        data = send_and_drain(port, payload)
        if data:
            head, separator, rest = data.partition(b"\r\n\r\n")
            assert data.startswith(b"HTTP/1.1 400")
            if separator:
                assert json.loads(rest) == {"error": "invalid_payload"}
        assert settings.calls == []
        assert_unchanged_settings(port)


# --- Connection timeout and handler cap (AC-03, D-03, D-11) ---

CONNECTION_TIMEOUT = 2  # patched per-connection read timeout (t)
CONNECTION_CAP = 3  # patched handler-thread cap (c)
HANDLER_THREAD_SUFFIX = "(process_request_thread)"


def handler_thread_count():
    return sum(
        1 for thread in threading.enumerate() if thread.name.endswith(HANDLER_THREAD_SUFFIX)
    )


def wait_for(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def open_connection(port):
    connection = socket.create_connection(("127.0.0.1", port), timeout=5)
    connection.settimeout(5)
    return connection


def hold_connection(port):
    """Open a connection whose handler thread blocks on a partial request."""
    connection = open_connection(port)
    connection.sendall(b"GET / HTTP/1.1\r\n")
    return connection


def read_until_eof(connection, timeout):
    """Read until end of stream; return (data, elapsed) with elapsed None on stall."""
    connection.settimeout(timeout)
    chunks = []
    started = time.monotonic()
    while True:
        try:
            piece = connection.recv(65536)
        except TimeoutError:
            return b"".join(chunks), None
        if not piece:
            return b"".join(chunks), time.monotonic() - started
        chunks.append(piece)


def read_until_refused(connection, timeout):
    """Read until end of stream or a connection reset; both count as closed.

    On Windows a refusal of a socket with unread data can surface as a reset
    rather than end of stream. Either outcome counts as closed without a
    response, provided no response bytes arrived.
    """
    connection.settimeout(timeout)
    chunks = []
    while True:
        try:
            piece = connection.recv(65536)
        except (ConnectionResetError, ConnectionAbortedError):
            break
        except TimeoutError:
            break
        if not piece:
            break
        chunks.append(piece)
    return b"".join(chunks)


def flood_once(port):
    """One cap-excess connection sending no bytes; True when refused within 0.5 s.

    No bytes are sent: on Windows a refusal of a socket with unread data can
    surface as a reset. End of stream or a reset both count as refused
    provided no response bytes arrived.
    """
    connection = open_connection(port)
    try:
        connection.settimeout(0.5)
        try:
            data = connection.recv(65536)
        except (ConnectionResetError, ConnectionAbortedError):
            return True
        except TimeoutError:
            return False
        return data == b""
    finally:
        connection.close()


def test_idle_keepalive_connection_reaches_end_of_stream_within_timeout():
    client = StubClient(catalog=valid_catalog())
    with patch.object(server, "_CONNECTION_TIMEOUT_SECONDS", CONNECTION_TIMEOUT), patch.object(
        server, "_MAX_ACTIVE_CONNECTIONS", 8
    ):
        with RunningServer(client) as port:
            connection = open_connection(port)
            connection.sendall(raw_request(port, "GET / HTTP/1.1"))
            data, elapsed = read_until_eof(connection, CONNECTION_TIMEOUT + 1)
            connection.close()
    assert data.startswith(b"HTTP/1.1 200")
    assert elapsed is not None
    assert elapsed <= CONNECTION_TIMEOUT + 1


def test_stalled_body_put_is_answered_400_closed_without_saving(capsys):
    settings = SettingsStub()
    with patch.object(server, "_CONNECTION_TIMEOUT_SECONDS", CONNECTION_TIMEOUT), patch.object(
        server, "_MAX_ACTIVE_CONNECTIONS", 8
    ):
        with RunningServer(StubClient(catalog=valid_catalog()), settings) as port:
            connection = open_connection(port)
            connection.sendall(
                raw_request(port, "PUT /api/settings HTTP/1.1", content_length=100)
            )
            data, elapsed = read_until_eof(connection, CONNECTION_TIMEOUT + 1)
            connection.close()
    head, _, body = data.partition(b"\r\n\r\n")
    assert data.count(b"HTTP/1.1") == 1
    assert head.startswith(b"HTTP/1.1 400")
    assert b"connection: close" in head.lower()
    assert json.loads(body) == {"error": "invalid_payload"}
    assert elapsed is not None
    assert elapsed <= CONNECTION_TIMEOUT + 1
    assert settings.calls == []
    assert "Traceback" not in capsys.readouterr().err


def test_flood_beyond_cap_is_refused_and_slots_are_released():
    cap = CONNECTION_CAP
    client = StubClient(catalog=valid_catalog())
    with patch.object(server, "_CONNECTION_TIMEOUT_SECONDS", CONNECTION_TIMEOUT), patch.object(
        server, "_MAX_ACTIVE_CONNECTIONS", cap
    ):
        running = RunningServer(client)
        with running as port:
            baseline = handler_thread_count()
            holders = [hold_connection(port) for _ in range(cap)]
            assert wait_for(lambda: handler_thread_count() - baseline >= cap, 5)
            peak = handler_thread_count() - baseline
            for _ in range(8):
                assert flood_once(port), "an excess connection was not refused"
                peak = max(peak, handler_thread_count() - baseline)
            assert peak <= cap
            assert wait_for(lambda: handler_thread_count() - baseline <= cap, 1)
            # Close the flood holders client-side; the server's connection
            # set drains within the timeout and every slot is released.
            for connection in holders:
                connection.close()
            assert wait_for(
                lambda: not running.server._active_connections, CONNECTION_TIMEOUT + 1
            )
            for _ in range(cap + 2):
                assert request(port, "GET", "/")[0] == 200
                assert wait_for(lambda: handler_thread_count() <= baseline, 5)


def test_owned_shutdown_completes_while_the_cap_is_full():
    cap = CONNECTION_CAP
    client = StubClient(catalog=valid_catalog())
    with patch.object(server, "_CONNECTION_TIMEOUT_SECONDS", 30), patch.object(
        server, "_MAX_ACTIVE_CONNECTIONS", cap
    ):
        running = RunningServer(client, SettingsStub())
        holders = []
        outcome = []
        try:
            port = running.server.server_port
            baseline = handler_thread_count()
            holders = [hold_connection(port) for _ in range(cap)]
            assert wait_for(lambda: handler_thread_count() - baseline >= cap, 10)
            for _ in range(8):
                assert flood_once(port), "an excess connection was not refused"
            assert wait_for(lambda: handler_thread_count() - baseline >= cap, 5)

            def shut():
                outcome.append(running.application.shutdown(time.monotonic() + 2))

            helper = threading.Thread(target=shut)
            helper.start()
            helper.join(5)
            assert not helper.is_alive(), "shutdown hung while the cap was full"
            assert outcome == [True]
            running.application.server = None
        finally:
            for connection in holders:
                connection.close()
            if not outcome:
                assert running.application.shutdown(time.monotonic() + 2)
                running.application.server = None


def test_injected_handler_thread_start_failure_releases_the_slot():
    cap = CONNECTION_CAP
    # The patched timeout only has to outlast the proof; AC-03 floors it at 2 s.
    hold_timeout = 10
    client = StubClient(catalog=valid_catalog())

    class FailingThread(threading.Thread):
        def start(self):
            raise RuntimeError("injected thread-start failure")

    with patch.object(server, "_CONNECTION_TIMEOUT_SECONDS", hold_timeout), patch.object(
        server, "_MAX_ACTIVE_CONNECTIONS", cap
    ):
        with RunningServer(client) as port:
            with patch.object(threading, "Thread", FailingThread):
                connection = open_connection(port)
                connection.sendall(raw_request(port, "GET / HTTP/1.1"))
                data = read_until_refused(connection, hold_timeout + 1)
                connection.close()
            assert b"HTTP/1.1" not in data  # closed without a response

            # The released slot leaves room for c concurrent partial
            # connections, all accepted afterwards. Acceptance is proved by
            # behavior, not by counting threads: every holder is open at once
            # and then completes its request. A connection refused for lack
            # of a slot is closed without a response, so a slot that was not
            # released on the failure path leaves the c-th holder unanswered.
            holders = []
            try:
                for _ in range(cap):
                    holders.append(hold_connection(port))
                finish = f"Host: 127.0.0.1:{port}\r\n\r\n".encode("ascii")
                for holder in holders:
                    try:
                        holder.sendall(finish)
                    except OSError:
                        pass  # a refused holder; the response check reports it
                statuses = []
                for holder in holders:
                    reply = b""
                    holder.settimeout(5)
                    try:
                        while b"\r\n" not in reply:
                            piece = holder.recv(65536)
                            if not piece:
                                break
                            reply += piece
                    except (ConnectionResetError, ConnectionAbortedError, TimeoutError):
                        pass
                    statuses.append(reply.split(b"\r\n", 1)[0])
                assert statuses == [b"HTTP/1.1 200 OK"] * cap, statuses
            finally:
                for holder in holders:
                    holder.close()


def test_default_timeout_and_cap_constants_are_pinned():
    assert server._CONNECTION_TIMEOUT_SECONDS == 15.0
    assert server._MAX_ACTIVE_CONNECTIONS == 32
    created = create_server(StubClient(catalog=valid_catalog()))
    try:
        assert created.RequestHandlerClass.timeout == server._CONNECTION_TIMEOUT_SECONDS
    finally:
        created.server_close()


EXPECTED_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "style-src-attr 'unsafe-inline'; img-src 'self'; font-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)
EXPECTED_SECURITY_HEADERS = (
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Content-Security-Policy", EXPECTED_CSP),
)


def assert_security_headers_exactly_once(head: bytes) -> None:
    """Each D-12 header appears exactly once with its exact value in the block."""
    lines = head.split(b"\r\n")[1:]
    for name, value in EXPECTED_SECURITY_HEADERS:
        prefix = f"{name.lower()}:".encode()
        wanted_value = value.lower().encode()
        matches = [
            line
            for line in lines
            if line.lower().startswith(prefix)
            and line[len(prefix) :].strip().lower() == wanted_value
        ]
        assert matches == [f"{name}: {value}".encode()], (name, matches)


def http_response_with_security_headers(port, method, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path)
    response = connection.getresponse()
    body = response.read()
    seen = {
        name: response.msg.get_all(name) for name, _ in EXPECTED_SECURITY_HEADERS
    }
    connection.close()
    return response.status, body, seen


@pytest.mark.parametrize(
    "method, path, expected_status",
    [
        ("GET", "/", 200),
        ("GET", "/api/settings", 200),
        ("GET", "/nope", 404),
        ("POST", "/", 405),
        ("HEAD", "/api/catalog", 200),
    ],
)
def test_framing_headers_appear_exactly_once_on_dispatched_responses(
    method, path, expected_status
):
    settings = SettingsStub()
    with RunningServer(StubClient(catalog=valid_catalog()), settings) as port:
        status, body, seen = http_response_with_security_headers(port, method, path)
    assert status == expected_status
    for name, value in EXPECTED_SECURITY_HEADERS:
        assert seen[name] == [value]
    if method == "HEAD":
        assert body == b""
    if path == "/api/settings":
        assert json.loads(body) == UNCHANGED_SETTINGS
    if path == "/" and method == "GET":
        assert body.startswith(b"<!doctype html")
    if path == "/" and method == "POST":
        assert body == b"Method not allowed\n"


def test_framing_headers_appear_exactly_once_on_invalid_framing_400():
    settings = SettingsStub()
    with RunningServer(StubClient(catalog=valid_catalog()), settings) as port:
        payload = invalid_framing_vector("chunked-without-length", port, "opaque")
        head, body = one_response_with_close(send_and_drain(port, payload))
    assert response_status(head) == 400
    assert json.loads(body) == {"error": "invalid_payload"}
    assert_security_headers_exactly_once(head)


def test_framing_headers_on_the_standard_library_414():
    with RunningServer(StubClient(catalog=valid_catalog())) as port:
        # Exactly 65,537 request-line bytes with no line terminator: the
        # stdlib readline limit produces its own 414 response.
        request_line = b"GET /" + b"a" * 65523 + b" HTTP/1.1"
        assert len(request_line) == 65537
        data = send_and_drain(port, request_line)
    assert data.count(b"HTTP/1.1") == 1
    head, _, _ = data.partition(b"\r\n\r\n")
    assert response_status(head) == 414
    assert_security_headers_exactly_once(head)
