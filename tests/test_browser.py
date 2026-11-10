import ast
import copy
import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from test_checkouts import clone, commit_file, git, isolate_git, make_remote, redigest
from test_server import embedded_put, raw_catalog, reported_contract_cases

from nyx import models
from nyx.app_runtime import REMOTE_CHECK_INTERVAL, ApplicationRuntime
from nyx.catalog import scan_catalog
from nyx.checkouts import PackageScan, collect_checkouts
from nyx.models import SCHEMA_VERSION, canonical_digest, parse_catalog
from nyx.server import (
    CatalogError,
    InvalidCheckoutFolders,
    _create_application_server,
    create_server,
)

playwright = pytest.importorskip("playwright.sync_api")

STEP_ONE = "123e4567-e89b-42d3-a456-426614174000"
STEP_TWO = "123e4567-e89b-42d3-a456-426614174001"
GATE = "123e4567-e89b-42d3-a456-426614174002"
LOOSE = "123e4567-e89b-42d3-a456-426614174003"
REVERSE = "123e4567-e89b-42d3-a456-426614174004"
XSS_TITLE = '<img src=x onerror="alert(1)"> loose package'
# Computed card backgrounds: a card with no incoming arrow uses the brand
# color in both themes; every other card uses the theme surface.
NO_INCOMING_CARD = "rgb(1, 120, 124)"
DARK_CARD = "rgb(27, 37, 51)"
LIGHT_CARD = "rgb(255, 255, 255)"


def _declared(title, project):
    return {
        "status": "ready",
        "target_project": project,
        "title": title,
    }


def _edge(target_id, name):
    return {
        "kind": "claim",
        "target_package_id": target_id,
        "claim_name": name,
        "observed_state": "unsatisfied",
        "observed_evidence_ref": None,
        "resolved_state": "unsatisfied",
        "reason": "claim_unsatisfied",
    }


def _completion_edge(target_id, *, observed_stage=None, resolved_state="unknown",
                     reason="completion_policy_needed"):
    return {
        "kind": "completion",
        "target_package_id": target_id,
        "observed_stage": observed_stage,
        "resolved_state": resolved_state,
        "reason": reason,
    }


def _entry(package_id, path, lifecycle, title, project, prerequisites=None):
    stage = path.split("/")[1]
    return {
        "package_id": package_id,
        "package_path": path,
        "project": project,
        "stage": stage,
        "declared": _declared(title, project),
        "relationship": {
            "participation": "available",
            "claims": [],
            "prerequisites": [] if prerequisites is None else prerequisites,
            "direct_prerequisite_state": (
                "unsatisfied" if prerequisites else "no_declared_prerequisites"
            ),
            "program": {
                "program_id": None,
                "title": None,
                "resolution": "not_declared",
            },
            "superseded_by": {"package_id": None, "resolution": "not_declared"},
        },
        "reported_fields": [],
    }


def _reseal(value):
    value["schema_version"] = SCHEMA_VERSION
    value.setdefault("configuration_revision", "revision-1")
    value.setdefault("terminal_stages", [])
    value["catalog_digest"] = canonical_digest(value)
    return value


def _admit_inventory_stage(value, project, stage):
    projects = value["inventory"]["projects"]
    if not any(item["name"] == project for item in projects):
        projects.append({"name": project, "availability": "complete"})
        projects.sort(key=lambda item: item["name"])
    stages = value["inventory"]["stages"]
    if not any(item["project"] == project and item["stage"] == stage for item in stages):
        stages.append({
            "project": project,
            "stage": stage,
            "availability": "complete",
        })
        stages.sort(key=lambda item: (item["project"], item["stage"]))


def board_payload(*, titles=None):
    titles = titles or {}
    value = {
        "schema_version": SCHEMA_VERSION,
        "inventory": {
            "projects": [
                {"name": "Alpha", "availability": "complete"},
                {"name": "Beta", "availability": "complete"},
            ],
            "stages": [
                {"project": "Alpha", "stage": "Queue", "availability": "complete"},
                {"project": "Alpha", "stage": "Under_Development", "availability": "complete"},
                {"project": "Beta", "stage": "Under_Development", "availability": "complete"},
            ],
        },
        "terminal_stages": [],
        "identity_coverage": {"state": "complete", "diagnostics": []},
        "program_coverage": {"state": "complete", "diagnostics": []},
        "discovery_diagnostics": [],
        "entries": [
            _entry(
                GATE,
                "Alpha/Queue/gate",
                "queue",
                titles.get("gate", "Gate step"),
                "Alpha",
                prerequisites=[_edge(STEP_TWO, "gate")],
            ),
            _entry(
                STEP_ONE,
                "Alpha/Under_Development/step-one",
                "under_development",
                titles.get("step_one", "Foundation step"),
                "Alpha",
            ),
            _entry(
                STEP_TWO,
                "Alpha/Under_Development/step-two",
                "under_development",
                titles.get("step_two", "Dependent step"),
                "Alpha",
                prerequisites=[_edge(STEP_ONE, "build")],
            ),
            _entry(
                LOOSE,
                "Beta/Under_Development/loose",
                "under_development",
                titles.get("loose", XSS_TITLE),
                "Beta",
                prerequisites=[_edge(STEP_ONE, "shared")],
            ),
        ],
        "programs": [],
    }
    _reseal(value)
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()


def _write_package(root, project, stage, name, package_id, body):
    package_path = root / project / stage / name
    package_path.mkdir(parents=True)
    package_path.joinpath("spec.md").write_text(body, encoding="utf-8")


def mixed_producer_root(tmp_path, *, target_stage="Queue"):
    root = tmp_path / "workspace"
    target_id = "123e4567-e89b-42d3-a456-426614174010"
    source_id = "123e4567-e89b-42d3-a456-426614174011"
    _write_package(
        root, "Alpha", target_stage, "target", target_id,
        f"# Target\nPackage ID: {target_id}\n"
        "Claim: release | satisfied | sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
    )
    _write_package(
        root, "Beta", "Under_Development", "dependent", source_id,
        f"# Dependent\nPackage ID: {source_id}\n"
        f"Prerequisite: {target_id} | release\n"
        f"Completion Prerequisite: {target_id}\n",
    )
    return root, target_id, source_id


class ScanningClient:
    """Use the real catalog producer for each browser request."""

    def __init__(self, root):
        self.root = root
        self.settings = None
        self.calls = 0

    def fetch_catalog(self):
        self.calls += 1
        completed = self.settings.completed if self.settings is not None else None
        revision = self.settings.revision if self.settings is not None else None
        return parse_catalog(scan_catalog(
            self.root,
            completed_stage_names=completed,
            configuration_revision=revision,
        ))


STAGE_ROWS = (
    ("Under_Development", "under_development", "Under Development", "Under Development package"),
    ("Queue", "queue", "Queue", "Queue package"),
    ("In_Progress", "in_progress", "In Progress", "In Progress package"),
    ("Needs_Fixes", "needs_fixes", "Needs Fixes", "Needs Fixes package"),
    ("Awaiting_Retrospective", "awaiting_retrospective", "Awaiting Retrospective", "Awaiting Retrospective package"),
    ("Done", "done", "Done", "Done target"),
    ("Archive", "archive", "Archive", "Archive package"),
)
STAGE_IDS = {
    stage: f"123e4567-e89b-42d3-a456-426614174{index + 6:03d}"
    for index, (stage, *_rest) in enumerate(STAGE_ROWS)
}
UNICODE_LOW = "\ue000"
UNICODE_HIGH = "\U00010000"


def lifecycle_payload(*, completed_stages=()):
    entries = []
    for stage, lifecycle, _label, title in STAGE_ROWS:
        prerequisites = None
        if stage == "Queue":
            edge = _edge(STAGE_IDS["Done"], "release")
            evidence = "sha256:" + "b" * 64
            edge.update(
                observed_state="satisfied",
                observed_evidence_ref=evidence,
                resolved_state="satisfied",
                reason="claim_satisfied",
            )
            prerequisites = [edge]
        entry = _entry(
            STAGE_IDS[stage],
            f"Fictional/{stage}/package",
            lifecycle,
            title,
            "Fictional",
            prerequisites=prerequisites,
        )
        if stage == "Done":
            entry["relationship"]["claims"] = [{
                "name": "release",
                "state": "satisfied",
                "evidence_ref": "sha256:" + "b" * 64,
            }]
        entries.append(entry)
    value = {
        "schema_version": SCHEMA_VERSION,
        "inventory": {
            "projects": [{"name": "Fictional", "availability": "complete"}],
            "stages": [
                {"project": "Fictional", "stage": stage, "availability": "complete"}
                for stage, *_rest in sorted(STAGE_ROWS)
            ],
        },
        "terminal_stages": list(completed_stages),
        "identity_coverage": {"state": "complete", "diagnostics": []},
        "program_coverage": {"state": "complete", "diagnostics": []},
        "discovery_diagnostics": [],
        "entries": sorted(entries, key=lambda entry: entry["package_path"]),
        "programs": [],
    }
    _reseal(value)
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()


COMPACT_CARD = "123e4567-e89b-42d3-a456-426614174020"
COMPACT_DEPENDENT = "123e4567-e89b-42d3-a456-426614174021"
COMPACT_COMPLETED = "123e4567-e89b-42d3-a456-426614174022"


def compact_payload(*, extra_stage=False):
    completed_edge = _edge(COMPACT_COMPLETED, "release")
    completed_edge.update(
        observed_state="satisfied",
        observed_evidence_ref="sha256:" + "c" * 64,
        resolved_state="satisfied",
        reason="claim_satisfied",
    )
    visible_edge = _edge(COMPACT_CARD, "build")
    visible_edge.update(
        observed_state="satisfied",
        observed_evidence_ref="sha256:" + "d" * 64,
        resolved_state="satisfied",
        reason="claim_satisfied",
    )
    entries = [
        _entry(
            COMPACT_CARD, "Alpha/Testing/custom", "under_development", "Custom stage card", "Alpha"
        ),
        _entry(
            COMPACT_DEPENDENT,
            "Alpha/Queue/dependent",
            "queue",
            "Visible dependent",
            "Alpha",
            prerequisites=[visible_edge, completed_edge],
        ),
        _entry(
            COMPACT_COMPLETED,
            "Alpha/Done/completed",
            "Done",
            "Completed prerequisite",
            "Alpha",
        ),
    ]
    if extra_stage:
        entries.append(_entry(
            "123e4567-e89b-42d3-a456-426614174023",
            "Alpha/Review/new",
            "under_development",
            "Pending new stage",
            "Alpha",
        ))
    entries.sort(key=lambda entry: entry["package_path"])
    stages = [
        {"project": "Alpha", "stage": "Done", "availability": "complete"},
        {"project": "Alpha", "stage": "Partial", "availability": "incomplete"},
        {"project": "Alpha", "stage": "Queue", "availability": "complete"},
        {"project": "Alpha", "stage": "Review", "availability": "complete"},
        {"project": "Alpha", "stage": "Testing", "availability": "complete"},
        {"project": "EmptyProject", "stage": "Empty", "availability": "complete"},
    ]
    if not extra_stage:
        stages.remove({"project": "Alpha", "stage": "Review", "availability": "complete"})
    value = {
        "schema_version": SCHEMA_VERSION,
        "inventory": {
            "projects": [
                {"name": "Alpha", "availability": "complete"},
                {"name": "EmptyProject", "availability": "complete"},
                {"name": "ZeroProject", "availability": "complete"},
            ],
            "stages": stages,
        },
        "terminal_stages": ["Done"],
        "identity_coverage": {"state": "complete", "diagnostics": []},
        "program_coverage": {"state": "complete", "diagnostics": []},
        "discovery_diagnostics": [
            {"code": "discovery_unavailable", "message": "Partial stage scan incomplete"}
        ],
        "entries": entries,
        "programs": [],
    }
    _reseal(value)
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()


def unicode_producer_payload():
    """Build a producer snapshot whose ordering is Python's scalar ordering."""
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        for index, marker in enumerate((UNICODE_LOW, UNICODE_HIGH), start=1):
            project = f"Project{marker}"
            package_path = root / project / "Queue" / f"diagnostic-{marker}"
            package_path.mkdir(parents=True)
            package_path.joinpath("spec.md").write_text(
                f"# Diagnostic {marker}\nPackage ID: malformed-{index}\n",
                encoding="utf-8",
            )
            program_path = root / project / "Reference" / "Programs" / (
                f"123e4567-e89b-42d3-a456-42661417400{index}"
            )
            program_path.mkdir(parents=True)
            program_path.joinpath("program.md").write_text(
                f"# malformed program {marker}\n", encoding="utf-8"
            )
        payload = scan_catalog(root, completed_stage_names=(UNICODE_LOW, UNICODE_HIGH))
    value = json.loads(payload)
    return payload.encode("utf-8"), value


def reversed_unicode_payload(payload, kind):
    value = json.loads(payload)
    if kind == "policy":
        value["terminal_stages"].reverse()
    elif kind == "path":
        value["entries"].reverse()
    else:
        diagnostics = value["program_coverage"]["diagnostics"]
        assert len(diagnostics) == 2
        diagnostics.reverse()
    _reseal(value)
    return value


class StaticClient:
    def __init__(self, payload):
        self.payload = payload
        self.settings = None

    def fetch_catalog(self):
        payload = self.payload
        if self.settings is not None:
            payload = json.loads(payload) if isinstance(payload, (bytes, str)) else json.loads(json.dumps(payload))
            payload["configuration_revision"] = self.settings.revision
            _reseal(payload)
        return parse_catalog(payload)


class SequenceClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0
        self.settings = None

    def fetch_catalog(self):
        index = min(self.calls, len(self.payloads) - 1)
        self.calls += 1
        payload = self.payloads[index]
        if isinstance(payload, Exception):
            raise payload
        if self.settings is not None:
            payload = json.loads(payload) if isinstance(payload, (bytes, str)) else json.loads(json.dumps(payload))
            payload["configuration_revision"] = self.settings.revision
            _reseal(payload)
        return parse_catalog(payload)


class BlockingSequenceClient(SequenceClient):
    """Delay one post-save catalog so a competing settings write can win."""

    def __init__(self, payloads, blocked_call=3):
        super().__init__(payloads)
        self.blocked_call = blocked_call
        self.release = threading.Event()
        self.started = threading.Event()

    def fetch_catalog(self):
        if self.calls == self.blocked_call - 1:
            self.calls += 1
            self.started.set()
            self.release.wait(timeout=10)
            payload = self.payloads[min(self.calls - 1, len(self.payloads) - 1)]
            if isinstance(payload, Exception):
                raise payload
            if self.settings is not None:
                payload = json.loads(payload) if isinstance(payload, (bytes, str)) else json.loads(json.dumps(payload))
                payload["configuration_revision"] = self.settings.revision
                _reseal(payload)
            return parse_catalog(payload)
        return super().fetch_catalog()


class RawCatalog:
    """Keep a deliberately malformed wire snapshot reachable by browser validation."""

    def __init__(self, value):
        self.value = value

    def as_dict(self):
        return self.value


class RawSequenceClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    def fetch_catalog(self):
        index = min(self.calls, len(self.payloads) - 1)
        self.calls += 1
        payload = self.payloads[index]
        if isinstance(payload, Exception):
            raise payload
        return RawCatalog(payload)


class BlockingRawSequenceClient(RawSequenceClient):
    """Delay one deliberately malformed wire snapshot for an interim-state assertion."""

    def __init__(self, payloads, blocked_call=2):
        super().__init__(payloads)
        self.blocked_call = blocked_call
        self.release = threading.Event()
        self.started = threading.Event()

    def fetch_catalog(self):
        if self.calls == self.blocked_call - 1:
            self.started.set()
            self.release.wait(timeout=10)
        return super().fetch_catalog()


class BrowserSettings:
    """Small application-settings seam used by the browser behavior tests."""

    def __init__(self, order=(), completed=(), revision="revision-1", root=None, gate=None):
        self.order = list(order)
        self.completed = list(completed)
        self.revision = revision
        self.root = root
        self.calls = []
        self.get_calls = 0
        self.fail_next = False
        self.fail_next_load = False
        self.gate = gate
        self.entered = threading.Event()
        self.folders = []
        self.github_lookup = False
        self.folder_gets = 0
        self.fail_next_folder_load = False
        self.folder_calls = []
        # Queued answers for the next folder save: refused folders (a list of
        # ``{index, reason}``), the runtime's null-state outcome, or an error code.
        self.folder_problems = None
        self.folder_reload_needed = False
        self.folder_save_error = None
        # The page's checkouts double, when a test attaches it: a folder save
        # then switches that double's document to one listing the saved folders.
        self.checkouts = None

    def get_checkout_settings(self):
        self.folder_gets += 1
        if self.fail_next_folder_load:
            self.fail_next_folder_load = False
            raise CatalogError("settings_unavailable")
        return {
            "folders": list(self.folders),
            "github_lookup": self.github_lookup,
            "revision": self.revision,
        }

    def get_settings(self):
        self.get_calls += 1
        if self.gate is not None:
            self.entered.set()
            self.gate.wait(timeout=10)
        if self.fail_next_load:
            self.fail_next_load = False
            raise CatalogError("producer_unavailable")
        return {"order": list(self.order), "completed": list(self.completed), "revision": self.revision}

    def get_workspace(self):
        if self.root is None:
            raise CatalogError("settings_unavailable")
        return {"root": str(self.root), "revision": self.revision}

    def save_settings(self, revision, order, completed):
        self.calls.append((revision, list(order), list(completed)))
        if self.gate is not None:
            self.entered.set()
            self.gate.wait(timeout=10)
        if self.fail_next:
            self.fail_next = False
            return {"order": list(self.order), "completed": list(self.completed), "revision": self.revision, "outcome": "failure"}
        if revision != self.revision:
            return {"order": list(self.order), "completed": list(self.completed), "revision": self.revision, "outcome": "conflict"}
        self.order = list(order)
        self.completed = list(completed)
        self.revision = f"revision-{len(self.calls) + 1}"
        return {"order": list(self.order), "completed": list(self.completed), "revision": self.revision, "outcome": "success"}


    def _folder_answer(self, outcome):
        return {
            "folders": list(self.folders), "github_lookup": self.github_lookup,
            "revision": self.revision, "outcome": outcome,
        }

    def save_checkout_settings(self, revision, folders, github_lookup):
        self.folder_calls.append(
            {"revision": revision, "folders": list(folders), "github_lookup": github_lookup}
        )
        if self.folder_save_error is not None:
            code, self.folder_save_error = self.folder_save_error, None
            raise CatalogError(code)
        if self.folder_problems is not None:
            problems, self.folder_problems = self.folder_problems, None
            raise InvalidCheckoutFolders(problems)
        if self.folder_reload_needed:
            self.folder_reload_needed = False
            # The runtime's null state: no record to compare, so no revision.
            return {"folders": [], "github_lookup": False, "revision": None,
                    "outcome": "reload-needed"}
        if self.fail_next:
            self.fail_next = False
            return self._folder_answer("failure")
        if revision != self.revision:
            return self._folder_answer("conflict")
        self.folders = sorted(set(folders))
        self.github_lookup = github_lookup and bool(self.folders)
        # One revision for both kinds of save, never one an earlier save made.
        self.revision = f"{0xF0000 + len(self.folder_calls) + len(self.calls):064x}"
        if self.checkouts is not None:
            self.checkouts.document = checkouts_listing_folders(
                self.checkouts.document, self.folders, self.revision
            )
        return self._folder_answer("success")


class RootSwitchSettings(BrowserSettings):
    """Change the active workspace while a saved catalog response is delayed."""

    def switch_root(self):
        self.order = ["Queue"]
        self.completed = ["Done"]
        self.revision = "root-b-revision"


_EMPTY_CHECKOUTS_DOCUMENT = None


def empty_checkouts_document():
    """One real collector document with no folders and no package scan.

    Built once for the module: every application page answers the checkouts
    request with it, so the page runs the success path and shows nothing.
    """
    global _EMPTY_CHECKOUTS_DOCUMENT
    if _EMPTY_CHECKOUTS_DOCUMENT is None:
        with TemporaryDirectory(prefix="nyx-empty-checkouts-") as home:
            with pytest.MonkeyPatch.context() as patch:
                isolate_git(patch, Path(home))
                rendered = collect_checkouts(
                    [], deadline_ns=time.monotonic_ns() + 30_000_000_000
                )
        _EMPTY_CHECKOUTS_DOCUMENT = json.loads(rendered)
    return copy.deepcopy(_EMPTY_CHECKOUTS_DOCUMENT)


def checkouts_envelope(document):
    """The envelope the application route serves around one checkout document."""
    return {
        "checkouts": document,
        "remote_checks": {
            "state": "idle",
            "interval_seconds": REMOTE_CHECK_INTERVAL,
            "next_due_at": None,
            "last": None,
        },
    }


def checkouts_listing_folders(document, folders, revision):
    """One document re-sealed to list ``folders`` under ``revision``."""
    changed = copy.deepcopy(document)
    changed["configuration_revision"] = revision
    changed["folders"] = sorted(
        ({"path": path, "status": "ok"} for path in folders), key=models.folder_sort_key
    )
    return redigest(changed)


class BrowserCheckouts:
    """The application's checkout provider double for the browser tests.

    A request answers with the current document, or with the outcome queued for
    it: an envelope the test built, or a ``CatalogError`` the route turns into a
    502 with that code.  ``hold`` keeps the next request in flight until the
    test releases it and records when it started, so a tick can observe one
    request still running.
    """

    def __init__(self, document=None):
        self.document = empty_checkouts_document() if document is None else document
        self.calls = 0
        self.queued = []
        self.gate = None
        self.entered = threading.Event()

    def envelope(self):
        """The envelope a request answers with when nothing is queued."""
        return checkouts_envelope(self.document)

    def hold(self):
        """Hold the next request until the returned event is set."""
        self.gate = threading.Event()
        return self.gate

    def serve(self, envelope):
        """Answer the next request with ``envelope``."""
        self.queued.append(envelope)

    def fail(self, code):
        """Answer the next request with the server's 502 for ``code``."""
        self.queued.append(CatalogError(code))

    def get_checkouts(self):
        self.calls += 1
        if self.gate is not None:
            self.entered.set()
            self.gate.wait(timeout=10)
        outcome = self.queued.pop(0) if self.queued else self.envelope()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RemoteCheckCheckouts(BrowserCheckouts):
    """A provider double that also answers the remote-check route.

    ``hold_remote_check`` keeps the next ``refresh_remote_checks`` call in
    flight until the test releases it; ``serve_remote_check`` and
    ``fail_remote_check`` queue the envelope, or the category the route turns
    into a 502, that the call answers.
    """

    def __init__(self, document=None):
        super().__init__(document)
        self.remote_calls = 0
        self.remote_queued = []
        self.remote_gate = None
        self.remote_entered = threading.Event()

    def hold_remote_check(self):
        """Hold the next remote check until the returned event is set."""
        self.remote_gate = threading.Event()
        self.remote_entered = threading.Event()
        return self.remote_gate

    def serve_remote_check(self, envelope):
        """Answer the next remote check with ``envelope``."""
        self.remote_queued.append(envelope)

    def fail_remote_check(self, code):
        """Answer the next remote check with the server's 502 for ``code``."""
        self.remote_queued.append(CatalogError(code))

    def refresh_remote_checks(self):
        self.remote_calls += 1
        self.remote_entered.set()
        gate = self.remote_gate
        if gate is not None:
            gate.wait(timeout=10)
        outcome = self.remote_queued.pop(0) if self.remote_queued else self.envelope()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class CheckoutsWorkerStandIn:
    """The worker manager a real-runtime page needs, answering captured bytes.

    The application collects through its worker boundary, so a page against the
    real provider stands this in for the subprocess: a collection returns the
    captured document, so nothing starts a child, contacts git or uses the
    network.  ``hold_network`` keeps the next networked collection in flight
    until the test releases it, so a proof can hold the fetch the runtime waits
    on for a requested remote check.
    """

    def __init__(self, document):
        self.document = document
        self.fetch_calls = 0
        self.network_calls = 0
        self.network_gate = None
        self.network_entered = threading.Event()

    def hold_network(self):
        """Hold the next networked collection until the returned event is set."""
        self.network_gate = threading.Event()
        self.network_entered = threading.Event()
        return self.network_gate

    def fetch_checkouts(self, *, network):
        self.fetch_calls += 1
        if network:
            self.network_calls += 1
            self.network_entered.set()
            if self.network_gate is not None:
                self.network_gate.wait(timeout=10)
        return self.document

    def fetch_catalog(self):
        raise AssertionError("the page's catalog comes from the catalog client")

    def close_admission(self):
        """Accept the runtime's admission close; the stand-in starts no child."""

    def close(self, deadline):
        """Report the stand-in closed within the runtime's deadline."""
        return True


@pytest.fixture(scope="module")
def shared_browser():
    """Yield one Chromium for the module; each launcher closes every context but leaves the browser open."""
    with playwright.sync_playwright() as api:
        browser = api.chromium.launch()
        try:
            yield browser
        finally:
            browser.close()


@contextmanager
def page_launcher(browser, launch_timeout_ms=15000):
    """Yield a page launcher; teardown stops every server before asserting."""
    created = []

    def stop_server(application, server, thread):
        """Stop one launched server; return False if the application did not stop."""
        if application is None:
            try:
                server.shutdown()
                thread.join()
            finally:
                server.server_close()
            return True
        return application.shutdown(time.monotonic() + 2) is True

    def launch(
        client, *, color_scheme="light", init_script=None, settings=None,
        checkouts=None, workers=None, fake_clock=False,
    ):
        """Open one page over a standalone server or an application runtime.

        ``settings`` installs the board-settings seam and the checkout provider
        double, so the application path serves the checkouts route; ``checkouts``
        replaces that double.  ``workers`` instead launches the real application
        runtime over a worker stand-in, leaving the real checkout provider in
        place.
        """
        application = None
        server = None
        thread = None
        if settings is None:
            server = create_server(client)
            thread = threading.Thread(target=server.serve_forever)
            try:
                thread.start()
            except BaseException:
                server.server_close()
                raise
        else:
            application = ApplicationRuntime(
                port=0,
                deadline=time.monotonic() + 5,
                server_factory=lambda **kwargs: _create_application_server(
                    provider=client,
                    port=kwargs["port"],
                    settings_provider=kwargs["settings_provider"],
                ),
                workers=workers,
            )
            application.get_settings = settings.get_settings
            application.get_workspace = settings.get_workspace
            application.get_checkout_settings = settings.get_checkout_settings
            application.save_settings = settings.save_settings
            application.save_checkout_settings = settings.save_checkout_settings
            client.settings = settings
            if workers is None:
                double = checkouts or BrowserCheckouts()
                application.get_checkouts = double.get_checkouts
                # The routes ask the application for each provider method at
                # request time.  An instance attribute that is None shadows the
                # runtime's own method, so a double without a remote-check
                # method answers the POST with 404.
                application.refresh_remote_checks = getattr(
                    double, "refresh_remote_checks", None
                )
        context = None
        try:
            if application is not None:
                application.start(static_ready=lambda: True)
                # The checkouts route asks the application for this provider at
                # request time, so admission is open before the page loads.
                application.admit_catalog()
                server = application.server
                thread = application.http_thread
            context = browser.new_context(color_scheme=color_scheme)
            if fake_clock:
                context.clock.install()
            # Record CSP violations for every page and navigation in
            # this context and assert none at teardown.
            violations = []
            context.expose_binding(
                "__nyxCspViolation", lambda source, entry: violations.append(entry)
            )
            context.add_init_script(
                "window.__cspViolations = [];"
                "document.addEventListener('securitypolicyviolation', (event) => {"
                "  const entry = {directive: event.violatedDirective,"
                "                  blockedURI: event.blockedURI};"
                "  window.__cspViolations.push(entry);"
                "  window.__nyxCspViolation(entry);"
                "});"
            )
            page = context.new_page()
            page._nyx_csp_violations = violations
            if init_script:
                page.add_init_script(init_script)
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.wait_for_function(
                "() => document.querySelector("
                "'#board .card, #board .board-empty, #board-issues')",
                timeout=launch_timeout_ms,
            )
            if application is None:
                page.wait_for_function(
                    "() => new Promise(resolve => requestAnimationFrame("
                    "() => requestAnimationFrame(resolve)))",
                    timeout=launch_timeout_ms,
                )
            else:
                # Application pages load board settings concurrently with the
                # first render; give that load time to finish.
                page.wait_for_timeout(150)
        except BaseException as failure:
            # A launch that fails after its server started stops that
            # server (and closes its context) before the error propagates;
            # a running non-daemon serve_forever thread would otherwise
            # keep the interpreter alive at exit.
            if context is not None:
                try:
                    context.close()
                except Exception as cleanup_error:
                    failure.add_note(f"context close also failed: {cleanup_error!r}")
            try:
                if not stop_server(application, server, thread):
                    failure.add_note(f"application did not stop: {application}")
            except Exception as cleanup_error:
                failure.add_note(f"server cleanup also failed: {cleanup_error!r}")
            raise
        created.append((application, server, thread, page))
        return page

    try:
        yield launch
    finally:
        # Every context of the shared browser, and every server and
        # application this launcher started, is closed before any teardown
        # failure is raised, so a failing assertion can never leave a
        # non-daemon serve_forever thread running. The browser itself stays
        # open for the rest of the module.
        errors = []
        violations = []
        unstopped = []

        def attempt(action):
            try:
                return action()
            except Exception as error:
                errors.append(error)
                return None

        for context in list(browser.contexts):
            attempt(context.close)
        for application, server, thread, page in created:
            violations.extend(page._nyx_csp_violations)
            if attempt(lambda: stop_server(application, server, thread)) is False:
                unstopped.append(application)
        if errors:
            raise errors[0]
        assert unstopped == [], f"applications that did not stop: {unstopped}"
        assert violations == [], f"recorded CSP violations: {violations}"


# Mirrors POLL_INTERVAL in nyx/static/app.js, the browser's poll interval.
POLL_INTERVAL_MS = 10_000


def fire_poll(page):
    """Advance the fake clock one poll interval and wait for the poll's response.

    One call fires the page's interval callback once. Call it only after the
    previous request's effect is visible: a poll issued while a request is in
    flight is dropped.
    """
    with page.expect_response("**/api/catalog", timeout=5000) as response:
        page.clock.fast_forward(POLL_INTERVAL_MS)
    response.value.finished()


# Records the status of every board response the page receives, including the
# ones its first load sends before a test can attach a listener.
RESPONSE_RECORDER = """
window.checkoutsResponses = [];
window.remoteCheckResponses = [];
window.catalogResponses = [];
const nyxRecordFetch = window.fetch;
window.fetch = function (...args) {
  const route = String(args[0]);
  return nyxRecordFetch(...args).then(function (response) {
    if (route.includes("/api/checkouts/remote-check")) window.remoteCheckResponses.push(response.status);
    if (route.includes("/api/checkouts")) window.checkoutsResponses.push(response.status);
    if (route.includes("/api/catalog")) window.catalogResponses.push(response.status);
    return response;
  });
};
"""


def wait_for_responses(page, recorder, count):
    """Wait until the page has received ``count`` responses on one route."""
    page.wait_for_function(f"() => window.{recorder}.length >= {count}", timeout=5000)


def settle_frames(page):
    """Flush the page's pending response continuations before a DOM assertion."""
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame("
        "() => requestAnimationFrame(resolve)))"
    )


def fire_checkouts_poll(page):
    """Advance one tick, waiting for its catalog and checkouts responses.

    Use it only when the tick's checkouts request is answered: a tick that finds
    a checkouts request in flight sends none.
    """
    with page.expect_response("**/api/checkouts", timeout=5000) as response:
        fire_poll(page)
    response.value.finished()


def card_paths(page):
    """The package path of every rendered card, in board order."""
    return page.locator(".card[data-package-path]").evaluate_all(
        "cards => cards.map(card => card.dataset.packagePath)"
    )


@pytest.fixture
def open_page(shared_browser):
    with page_launcher(shared_browser) as launch:
        yield launch


def card_titles(page, column=1, row="Under Development"):
    selector = (
        f".board-row:has(.row-head:text-is('{row}')) .cell:nth-of-type({column}) .card-title"
    )
    return page.locator(selector).all_inner_texts()


def row_labels(page):
    return page.locator(".row-head").evaluate_all(
        "rows => rows.map(row => row.firstChild.textContent)"
    )


def stage_editor_order(page):
    return page.locator("#stage-order-list .stage-order-item").evaluate_all(
        "items => items.map(item => item.dataset.stage)"
    )


def open_stage_editor(page):
    editor = page.locator("#stage-order-editor")
    if editor.get_attribute("open") is None:
        editor.locator("summary").click()


def dependency_state_payload(state, *, titles=None):
    value = json.loads(board_payload(titles=titles))
    gate = next(entry for entry in value["entries"] if entry["package_id"] == GATE)
    edge = gate["relationship"]["prerequisites"][0]
    edge.update(observed_state=state, resolved_state=state, reason=f"claim_{state}")
    gate["relationship"]["direct_prerequisite_state"] = state
    _reseal(value)
    return value


# Literal expectations for Windows-style roots: the root is preserved and every
# separator after it is a backslash.
COPY_ROOT_CASES = [
    pytest.param(
        "C:\\work\\board",
        "C:\\work\\board\\Alpha\\Under_Development\\step-one",
        id="drive-letter",
    ),
    pytest.param(
        "\\\\server\\share\\board",
        "\\\\server\\share\\board\\Alpha\\Under_Development\\step-one",
        id="unc",
    ),
]


@pytest.mark.parametrize(("windows_root", "expected_path"), COPY_ROOT_CASES)
def test_card_right_click_copies_windows_root_in_windows_format(
    open_page, windows_root, expected_path
):
    settings = BrowserSettings(root=windows_root)
    page = open_page(StaticClient(board_payload()), settings=settings)
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.wait_for_load_state("networkidle")
    card = page.locator(f'.card[data-package-id="{STEP_ONE}"]')
    relative = card.get_attribute("data-package-path")
    assert relative
    card.click(button="right")
    playwright.expect(page.locator("#copy-status")).to_have_text("Full directory path copied.")
    playwright.expect(page.locator("#copy-status")).to_be_visible()
    card_box = card.bounding_box()
    popup_box = page.locator("#copy-status").bounding_box()
    assert card_box and popup_box
    assert 8 <= popup_box["x"] - (card_box["x"] + card_box["width"]) <= 12
    assert popup_box["x"] + popup_box["width"] <= page.viewport_size["width"]
    assert popup_box["y"] + popup_box["height"] <= page.viewport_size["height"]
    assert relative == "Alpha/Under_Development/step-one"
    copied = page.evaluate("navigator.clipboard.readText()")
    assert copied == expected_path
    assert "/" not in copied
    assert card.get_attribute("aria-pressed") == "false"


def test_card_right_click_copies_full_directory_without_changing_selection(open_page, tmp_path):
    # The pytest tmp_path root is POSIX on Linux and macOS and drive-letter on
    # Windows.
    root = tmp_path / "spec workspace"
    settings = BrowserSettings(root=root)
    page = open_page(StaticClient(board_payload()), settings=settings)
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.wait_for_load_state("networkidle")
    card = page.locator(f'.card[data-package-id="{STEP_ONE}"]')
    relative = card.get_attribute("data-package-path")
    assert relative
    card.click(button="right")
    playwright.expect(page.locator("#copy-status")).to_have_text("Full directory path copied.")
    playwright.expect(page.locator("#copy-status")).to_be_visible()
    card_box = card.bounding_box()
    popup_box = page.locator("#copy-status").bounding_box()
    assert card_box and popup_box
    assert 8 <= popup_box["x"] - (card_box["x"] + card_box["width"]) <= 12
    assert popup_box["x"] + popup_box["width"] <= page.viewport_size["width"]
    assert popup_box["y"] + popup_box["height"] <= page.viewport_size["height"]
    assert relative == "Alpha/Under_Development/step-one"
    copied = page.evaluate("navigator.clipboard.readText()")
    assert copied == str(root / relative)
    assert card.get_attribute("aria-pressed") == "false"
    playwright.expect(page.locator("#copy-status")).to_be_hidden(timeout=3500)
    page.set_viewport_size({"width": 480, "height": 320})
    card.click(button="right")
    playwright.expect(page.locator("#copy-status")).to_be_visible()
    popup_box = page.locator("#copy-status").bounding_box()
    assert popup_box
    assert popup_box["x"] >= 0 and popup_box["y"] >= 0
    assert popup_box["x"] + popup_box["width"] <= 480
    assert popup_box["y"] + popup_box["height"] <= 320
    card.click()
    assert card.get_attribute("aria-pressed") == "true"


def test_card_copy_failure_shows_error_beside_card(open_page, tmp_path):
    page = open_page(
        StaticClient(board_payload()),
        settings=BrowserSettings(root=tmp_path),
        init_script="""Object.defineProperty(navigator, 'clipboard', {
          configurable: true,
          value: {writeText: () => Promise.reject(new Error('denied'))}
        });""",
    )
    page.wait_for_load_state("networkidle")
    card = page.locator(f'.card[data-package-id="{STEP_ONE}"]')
    card.click(button="right")
    popup = page.locator("#copy-status")
    playwright.expect(popup).to_be_visible()
    playwright.expect(popup).to_have_class(re.compile(r"\berror\b"))
    playwright.expect(popup).to_have_text(
        "Clipboard access was denied; directory path was not copied."
    )


def test_board_settings_starts_collapsed_and_lists_hidden_and_absent_completion_targets(open_page):
    settings = BrowserSettings(order=["Missing", "Done"], completed=["Done", "Absent"])
    page = open_page(StaticClient(lifecycle_payload(completed_stages=("Done",))), settings=settings)
    editor = page.locator("#stage-order-editor")
    editor.wait_for()
    assert editor.get_attribute("open") is None
    editor.locator("summary").press("Enter")
    assert page.locator(
        "#stage-order-list .stage-order-item[data-stage='Under_Development'] .stage-order-name"
    ).inner_text() == "Under Development"
    assert page.get_by_role("checkbox", name="Counts as finished: Done").is_checked()
    assert page.get_by_role("checkbox", name="Counts as finished: Absent").is_checked()
    assert page.locator("#stage-order-list .stage-order-item[data-stage='Done']").count() == 1
    assert page.locator("#stage-order-list .stage-order-item[data-stage='Absent']").count() == 1
    assert "not currently available" in page.locator(
        "#stage-order-list .stage-order-item[data-stage='Absent']"
    ).inner_text()
    assert page.get_by_role("checkbox", name="Counts as finished: Done").is_enabled()
    page.get_by_role("checkbox", name="Counts as finished: Done").uncheck()
    page.get_by_role("button", name="Save").click()
    page.get_by_text("Board settings saved.", exact=True).wait_for()
    assert settings.completed == ["Absent"]


def test_empty_board_click_preference_changes_selection_immediately(open_page):
    page = open_page(StaticClient(board_payload()), settings=BrowserSettings())
    preference = page.get_by_role("checkbox", name="Click empty space to deselect")
    assert page.locator("#stage-order-editor").get_attribute("open") is None

    first = page.locator(f'.card[data-package-id="{STEP_ONE}"]')
    second = page.locator(f'.card[data-package-id="{STEP_TWO}"]')
    first.click()
    page.locator(".row-head").first.click()
    assert first.get_attribute("aria-pressed") == "true"

    open_stage_editor(page)
    assert not preference.is_checked()
    preference.check()
    assert page.get_by_role("button", name="Save").is_disabled()
    page.locator(".row-head").first.click()
    assert first.get_attribute("aria-pressed") == "false"
    assert page.locator(".card.selected").count() == 0

    first.click()
    second.click()
    assert first.get_attribute("aria-pressed") == "false"
    assert second.get_attribute("aria-pressed") == "true"
    second.click()
    assert second.get_attribute("aria-pressed") == "false"

    first.click()
    page.locator("#board .cell.vacant").first.click()
    assert first.get_attribute("aria-pressed") == "false"
    preference.uncheck()
    first.click()
    page.locator(".row-head").first.click()
    assert first.get_attribute("aria-pressed") == "true"


def test_empty_click_preference_is_available_without_saved_settings_and_persists(open_page):
    page = open_page(StaticClient(board_payload()))
    open_stage_editor(page)
    preference = page.get_by_role("checkbox", name="Click empty space to deselect")
    assert not preference.is_checked()
    assert page.locator("#stage-order-controls").is_hidden()
    preference.check()
    assert page.evaluate("localStorage.getItem('spec-tracker-deselect-on-empty-click')") == "true"
    page.reload()
    open_stage_editor(page)
    assert preference.is_checked()
    card = page.locator(f'.card[data-package-id="{STEP_ONE}"]')
    card.click()
    page.locator(".row-head").first.click()
    assert card.get_attribute("aria-pressed") == "false"

    blocked = open_page(
        StaticClient(board_payload()),
        init_script="""Object.defineProperty(window, 'localStorage', {
          get() { throw new DOMException('Blocked', 'SecurityError'); }
        });""",
    )
    open_stage_editor(blocked)
    assert not blocked.get_by_role("checkbox", name="Click empty space to deselect").is_checked()


def test_post_save_refresh_rejects_late_other_revision_and_reloads_current_board(open_page):
    first = dependency_state_payload("unsatisfied")
    stale = dependency_state_payload("unsatisfied", titles={"gate": "Stale gate"})
    fresh = dependency_state_payload("satisfied", titles={"gate": "Fresh gate"})
    client = BlockingSequenceClient([first, first, stale, stale, fresh], blocked_call=3)
    settings = BrowserSettings()
    page_a = open_page(client, settings=settings)
    page_b = open_page(client, settings=settings)
    for page in (page_a, page_b):
        page.locator("#stage-order-editor").wait_for()
        open_stage_editor(page)
        page.locator(f'.card[data-package-id="{GATE}"]').click()

    page_a.get_by_role("checkbox", name="Counts as finished: Under Development").check()
    page_a.get_by_role("button", name="Save").click()
    page_b.get_by_role("checkbox", name="Counts as finished: Under Development").check()
    page_b.get_by_role("button", name="Save").click()
    page_b.get_by_text(re.compile("Save not applied: conflict"), exact=False).wait_for()
    page_b.get_by_role("button", name="Reload board settings").click()
    page_b.get_by_text("Current board settings loaded.", exact=True).wait_for()
    page_b.get_by_role("checkbox", name="Counts as finished: Under Development").uncheck()
    page_b.get_by_role("button", name="Save").click()
    # The competing save must commit, and its own catalog refresh must consume
    # the next snapshot, before the delayed refresh is released; otherwise the
    # delayed snapshot could be stamped with a revision that is still current.
    page_b.get_by_text("Board settings saved.", exact=True).wait_for()
    playwright.expect(
        page_b.locator(f'.card[data-package-id="{GATE}"] .card-title')
    ).to_have_text("Stale gate")
    assert client.calls == 4
    assert settings.revision == "revision-4"
    gate_title = page_a.locator(f'.card[data-package-id="{GATE}"] .card-title')
    playwright.expect(gate_title).to_have_text("Gate step")
    page_a.evaluate("""() => {
      window.sawRejectedSnapshot = false;
      new MutationObserver(() => {
        if (document.querySelector('#board').textContent.includes('Stale gate')) {
          window.sawRejectedSnapshot = true;
        }
      }).observe(document.querySelector('#board'), {childList: true, subtree: true});
    }""")
    client.release.set()

    page_a.get_by_text("Current board settings loaded.", exact=True).wait_for()
    playwright.expect(gate_title).to_have_text("Fresh gate")
    assert page_a.evaluate("window.sawRejectedSnapshot") is False
    assert settings.completed == []


@pytest.mark.parametrize("case", ["stale", "null", "malformed"])
def test_post_save_refresh_rejects_invalid_revision_and_schema_payloads(open_page, case):
    valid = dependency_state_payload("unsatisfied")
    bad = dependency_state_payload("satisfied", titles={"gate": "Rejected gate"})
    if case == "stale":
        bad["configuration_revision"] = "revision-before-save"
    elif case == "null":
        bad["configuration_revision"] = None
    elif case == "malformed":
        bad["configuration_revision"] = {"revision": "bad"}
    bad["catalog_digest"] = canonical_digest(bad)
    fresh = dependency_state_payload("unknown", titles={"gate": "Fresh gate"})
    fresh["configuration_revision"] = "revision-2"
    fresh["catalog_digest"] = canonical_digest(fresh)
    client = BlockingRawSequenceClient([valid, bad, fresh])
    settings = BrowserSettings()

    from nyx import server as server_module

    def passthrough_catalog(candidate):
        return candidate if isinstance(candidate, RawCatalog) else parse_catalog(candidate)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(server_module, "parse_catalog", passthrough_catalog)
        page = open_page(client, settings=settings)
        page.locator("#stage-order-editor").wait_for()
        open_stage_editor(page)
        page.get_by_role("checkbox", name="Counts as finished: Under Development").check()
        page.get_by_role("button", name="Save").click()
        assert client.started.wait(timeout=5)
        # The save message only lasts until the blocked post-save refresh resolves.
        page.get_by_text("Board settings saved.", exact=True).wait_for(timeout=15000)
        gate_title = page.locator(f'.card[data-package-id="{GATE}"] .card-title')
        assert gate_title.inner_text() == "Gate step"
        page.evaluate("""() => {
          window.sawRejectedSnapshot = false;
          new MutationObserver(() => {
            if (document.querySelector('#board').textContent.includes('Rejected gate')) {
              window.sawRejectedSnapshot = true;
            }
          }).observe(document.querySelector('#board'), {childList: true, subtree: true});
        }""")
        client.release.set()
        playwright.expect(gate_title).to_have_text("Fresh gate", timeout=15000)

    assert page.locator("#stage-order-status").inner_text() == "Current board settings loaded."
    assert page.evaluate("window.sawRejectedSnapshot") is False


def coherent_refresh_payload():
    value = dependency_state_payload("satisfied")
    loose = next(entry for entry in value["entries"] if entry["package_id"] == LOOSE)
    edge = loose["relationship"]["prerequisites"][0]
    edge.update(target_package_id=STEP_TWO, observed_state="satisfied",
                resolved_state="satisfied", reason="claim_satisfied")
    _reseal(value)
    return value


def test_successful_save_updates_needs_blocks_selection_and_rail_pairs_together(open_page):
    client = SequenceClient([dependency_state_payload("unsatisfied"), coherent_refresh_payload()])
    settings = BrowserSettings()
    page = open_page(client, settings=settings)
    page.locator("#stage-order-editor").wait_for()
    open_stage_editor(page)
    page.locator(f'.card[data-package-id="{LOOSE}"]').click()
    page.get_by_role("checkbox", name="Counts as finished: Under Development").check()
    page.get_by_role("button", name="Save").click()
    page.get_by_text("Board settings saved.", exact=True).wait_for()

    step_two = page.locator(f'.card[data-package-id="{STEP_TWO}"]')
    assert "blocks:" in step_two.locator(".card-links").inner_text()
    assert "loose package" in step_two.locator(".card-links").inner_text()
    loose = page.locator(f'.card[data-package-id="{LOOSE}"]')
    assert "needs: Dependent step" in loose.locator(".card-links").inner_text()
    assert loose.get_attribute("aria-pressed") == "true"
    assert loose.locator(".card-filepath").inner_text() == "Filepath: Beta/Under_Development/loose"
    assert connection_pairs(page) == {(STEP_ONE, STEP_TWO), (STEP_TWO, GATE), (STEP_TWO, LOOSE)}


def test_real_producer_completion_policy_save_hides_finished_target_and_rail(open_page, tmp_path):
    root, target_id, source_id = mixed_producer_root(tmp_path)
    settings = BrowserSettings(completed=["Other"])
    client = ScanningClient(root)
    page = open_page(client, settings=settings)

    source = page.locator(f'.card[data-package-id="{source_id}"]')
    target = page.locator(f'.card[data-package-id="{target_id}"]')
    assert "needs: Target Alpha" in source.inner_text()
    assert "blocks: Dependent Beta" in target.inner_text()
    assert connection_pairs(page) == {(target_id, source_id)}
    before = next(entry for entry in client.fetch_catalog().as_dict()["entries"]
                  if entry["package_id"] == source_id)
    edge = next(edge for edge in before["relationship"]["prerequisites"] if edge["kind"] == "completion")
    assert (edge["resolved_state"], edge["reason"]) == ("unsatisfied", "completion_unsatisfied")

    source.click()

    open_stage_editor(page)
    page.get_by_role("checkbox", name="Counts as finished: Queue").check()
    page.get_by_role("button", name="Save").click()
    page.get_by_text("Board settings saved.", exact=True).wait_for()
    # Queue now counts as finished, so the refreshed board hides its row and
    # the arrow from the target while terminal rows are hidden.
    playwright.expect(target).to_have_count(0, timeout=15000)
    assert connection_pairs(page) == set()
    after = next(entry for entry in client.fetch_catalog().as_dict()["entries"]
                 if entry["package_id"] == source_id)
    edge = next(edge for edge in after["relationship"]["prerequisites"] if edge["kind"] == "completion")
    assert (edge["resolved_state"], edge["reason"]) == ("satisfied", "completion_satisfied")
    assert source.locator(".link.unresolved").count() == 0
    assert source.get_attribute("aria-pressed") == "true"

    page.get_by_label("Hide terminal rows", exact=True).uncheck()
    assert target.count() == 1
    assert connection_pairs(page) == {(target_id, source_id)}


def test_real_producer_terminal_gated_completion_target_reveals_card_and_rail(open_page, tmp_path):
    root, target_id, source_id = mixed_producer_root(tmp_path, target_stage="Done")
    settings = BrowserSettings(completed=["Done"])
    client = ScanningClient(root)
    page = open_page(client, settings=settings)

    assert page.locator(f'.card[data-package-id="{target_id}"]').count() == 0
    assert page.locator(f'.card[data-package-id="{source_id}"]').count() == 1
    assert connection_pairs(page) == set()
    page.locator(f'.card[data-package-id="{source_id}"]').click()
    source = page.locator(f'.card[data-package-id="{source_id}"]')
    assert source.locator(".link.unresolved").count() == 0
    value = client.fetch_catalog().as_dict()
    dependent = next(entry for entry in value["entries"] if entry["package_id"] == source_id)
    completion = next(edge for edge in dependent["relationship"]["prerequisites"] if edge["kind"] == "completion")
    assert completion["resolved_state"] == "satisfied"
    assert completion["reason"] == "completion_satisfied"
    page.get_by_label("Hide terminal rows", exact=True).uncheck()
    assert page.locator(f'.card[data-package-id="{target_id}"]').count() == 1
    assert connection_pairs(page) == {(target_id, source_id)}


def test_real_producer_invalid_and_missing_completion_edges_are_admitted(open_page, tmp_path):
    root = tmp_path / "workspace"
    source_id = "123e4567-e89b-42d3-a456-426614174012"
    missing_id = "123e4567-e89b-42d3-a456-426614174013"
    _write_package(
        root, "Alpha", "Queue", "invalid", source_id,
        f"# Invalid\nPackage ID: {source_id}\nCompletion Prerequisite: malformed | value\n",
    )
    _write_package(
        root, "Alpha", "Under_Development", "dependent", "123e4567-e89b-42d3-a456-426614174014",
        f"# Dependent\nPackage ID: 123e4567-e89b-42d3-a456-426614174014\n"
        f"Completion Prerequisite: {missing_id}\n",
    )
    value = json.loads(scan_catalog(root, configuration_revision="revision-1"))
    invalid_entry = next(item for item in value["entries"] if item["package_id"] == source_id)
    missing_entry = next(
        item for item in value["entries"] if item["package_id"] == "123e4567-e89b-42d3-a456-426614174014"
    )
    invalid_edge = invalid_entry["relationship"]["prerequisites"][0]
    missing_edge = missing_entry["relationship"]["prerequisites"][0]
    assert (invalid_edge["kind"], invalid_edge["reason"]) == ("completion", "invalid_prerequisite")
    assert (missing_edge["kind"], missing_edge["reason"]) == ("completion", "missing_target")
    assert parse_catalog(value).schema_version == SCHEMA_VERSION

    page = open_page(StaticClient(value))
    for package_id in (source_id, "123e4567-e89b-42d3-a456-426614174014"):
        card = page.locator(f'.card[data-package-id="{package_id}"]')
        playwright.expect(card).to_have_count(1)
        playwright.expect(card.locator(".link.unresolved")).to_have_text("unresolved target")


CLAIM_EDGE_REASONS = [
    "claim_satisfied", "claim_unsatisfied", "claim_unknown", "missing_claim",
    "invalid_claim", "missing_target", "duplicate_target", "identity_coverage_incomplete",
    "target_unreadable", "target_changed_during_read", "target_invalid_identity",
    "self_edge", "invalid_prerequisite",
]
COMPLETION_EDGE_REASONS = [
    "completion_satisfied", "completion_unsatisfied", "completion_policy_needed",
    "completion_policy_invalid", "missing_target", "duplicate_target",
    "identity_coverage_incomplete", "target_unreadable", "target_changed_during_read",
    "target_invalid_identity", "self_edge", "invalid_prerequisite",
]


def admitted_reason_payload(kind, reason):
    value = json.loads(board_payload())
    gate = next(entry for entry in value["entries"] if entry["package_id"] == GATE)
    if kind == "claim":
        edge = _edge(None if reason in {
            "missing_target", "duplicate_target", "identity_coverage_incomplete",
            "target_unreadable", "target_changed_during_read", "target_invalid_identity",
            "self_edge", "invalid_prerequisite",
        } else STEP_ONE, None if reason == "invalid_prerequisite" else "release")
        edge.update(observed_state=None, observed_evidence_ref=None,
                    resolved_state="unknown", reason=reason)
    else:
        edge = _completion_edge(
            None if reason in {
                "missing_target", "duplicate_target", "identity_coverage_incomplete",
                "target_unreadable", "target_changed_during_read", "target_invalid_identity",
                "self_edge", "invalid_prerequisite",
            } else STEP_ONE,
            observed_stage=None if reason not in {"completion_satisfied", "completion_unsatisfied"} else "Queue",
            resolved_state="unknown" if reason not in {"completion_satisfied", "completion_unsatisfied"} else (
                "satisfied" if reason == "completion_satisfied" else "unsatisfied"
            ),
            reason=reason,
        )
    gate["relationship"]["prerequisites"] = [edge]
    gate["relationship"]["direct_prerequisite_state"] = edge["resolved_state"]
    _reseal(value)
    return value


def test_browser_admits_every_python_valid_typed_edge_reason(open_page):
    reasons = [("claim", reason) for reason in CLAIM_EDGE_REASONS] + [
        ("completion", reason) for reason in COMPLETION_EDGE_REASONS
    ]
    values = []
    for kind, reason in reasons:
        value = admitted_reason_payload(kind, reason)
        gate = next(entry for entry in value["entries"] if entry["package_id"] == GATE)
        gate["declared"]["title"] = f"Gate {kind} {reason}"
        _reseal(value)
        assert parse_catalog(value).entries, f"{kind} {reason}"
        values.append(value)
    client = SequenceClient([board_payload()] + values)
    page = open_page(client, fake_clock=True)
    # Freeze the page timers so no poll can interleave with the refresh clicks.
    # pause_at takes seconds, so one second ahead of the page's current time.
    page.clock.pause_at(page.evaluate("Date.now()") / 1000 + 1)
    assert client.calls == 1, "pausing the clock must not fire a poll"
    gate_title = page.locator(f'.card[data-package-id="{GATE}"] .card-title')
    for kind, reason in reasons:
        page.locator("#refresh").click()
        playwright.expect(gate_title).to_have_text(f"Gate {kind} {reason}", timeout=5000)
        assert page.locator("#board-issues").count() == 0, f"{kind} {reason}"
    assert client.calls == len(reasons) + 1


def malformed_typed_edge_payload(case):
    value = json.loads(board_payload())
    edge = next(entry for entry in value["entries"] if entry["package_id"] == GATE)[
        "relationship"
    ]["prerequisites"][0]
    if case == "missing-kind":
        del edge["kind"]
    elif case == "invalid-kind":
        edge["kind"] = "unknown"
    elif case == "extra-key":
        edge["extra"] = "unexpected"
    elif case == "cross-kind":
        edge.clear()
        edge.update(_completion_edge(STEP_ONE, observed_stage=None,
                                     resolved_state="unknown", reason="claim_unknown"))
    elif case == "unsafe-stage":
        edge.clear()
        edge.update(_completion_edge(STEP_ONE, observed_stage="../unsafe",
                                     resolved_state="unknown", reason="completion_policy_needed"))
    elif case == "mismatched-reason":
        edge["reason"] = "completion_satisfied"
    else:  # pragma: no cover - the parameter list owns the cases
        raise AssertionError(case)
    _reseal(value)
    return value


@pytest.mark.parametrize("case", [
    "missing-kind", "invalid-kind", "extra-key", "cross-kind", "unsafe-stage", "mismatched-reason",
])
def test_browser_rejects_malformed_typed_edges_and_retains_last_board(open_page, case):
    valid = json.loads(board_payload())
    invalid = malformed_typed_edge_payload(case)
    from nyx import server as server_module

    def passthrough_catalog(candidate):
        return candidate if isinstance(candidate, RawCatalog) else parse_catalog(candidate)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(server_module, "parse_catalog", passthrough_catalog)
        client = BlockingRawSequenceClient([valid, invalid], blocked_call=2)
        page = open_page(client)
        page.get_by_role("button", name="Refresh view").click()
        assert client.started.wait(timeout=5)
        assert page.locator("#board .card").count() == 4
        assert page.locator('.card[data-package-id="%s"]' % GATE).count() == 1
        client.release.set()
        page.locator("#board").get_by_text("Latest refresh issue: producer_protocol_error", exact=True).wait_for(
            timeout=15000
        )

    assert page.locator("#board .card").count() == 4
    assert page.locator('.card[data-package-id="%s"]' % GATE).count() == 1


def test_root_switch_during_post_save_refresh_reloads_the_new_root_policy(open_page):
    client = BlockingSequenceClient(
        [dependency_state_payload("unsatisfied"), dependency_state_payload("satisfied"),
         dependency_state_payload("unknown")],
        blocked_call=2,
    )
    settings = RootSwitchSettings()
    page = open_page(client, settings=settings)
    page.locator("#stage-order-editor").wait_for()
    open_stage_editor(page)
    page.get_by_role("checkbox", name="Counts as finished: Under Development").check()
    page.get_by_role("button", name="Save").click()
    assert client.started.wait(timeout=5)
    settings.switch_root()
    assert page.locator("#stage-order-list .stage-order-item[data-stage='Under_Development']").locator(
        ".stage-completed-toggle"
    ).is_checked()
    client.release.set()
    page.wait_for_function(
        """() => {
          const done = document.querySelector("[data-stage='Done'] .stage-completed-toggle");
          const underDevelopment = document.querySelector(
            "[data-stage='Under_Development'] .stage-completed-toggle"
          );
          return done?.checked === true && underDevelopment?.checked === false;
        }""",
        timeout=15000,
    )
    assert page.locator("#stage-order-list .stage-order-item[data-stage='Queue']").count() == 1
    assert page.locator("#stage-order-list .stage-order-item[data-stage='Done']").locator(
        ".stage-completed-toggle"
    ).is_checked()
    assert not page.locator("#stage-order-list .stage-order-item[data-stage='Under_Development']").locator(
        ".stage-completed-toggle"
    ).is_checked()


def test_markup_shaped_stage_names_are_escaped_with_independent_controls(open_page):
    markup_stage = '<img src=x onerror="alert(1)">'
    value = json.loads(board_payload())
    value["inventory"]["stages"].append({
        "project": "Alpha", "stage": markup_stage, "availability": "complete",
    })
    value["inventory"]["stages"].sort(key=lambda item: (item["project"], item["stage"]))
    _reseal(value)
    settings = BrowserSettings(order=[markup_stage], completed=[markup_stage])
    page = open_page(StaticClient(value), settings=settings)
    page.locator("#stage-order-editor").wait_for()
    open_stage_editor(page)
    row = page.locator("#stage-order-list .stage-order-item").filter(has_text=markup_stage)
    assert row.count() == 1
    assert row.locator("img").count() == 0
    assert row.locator(".stage-order-name").inner_text() == markup_stage
    assert row.locator("code").count() == 0
    checkbox = row.get_by_role("checkbox")
    assert checkbox.count() == 1
    assert checkbox.get_attribute("aria-label") == f"Counts as finished: {markup_stage}"
    assert row.get_by_role("button", name=f"Move {markup_stage} up").count() == 1
    assert row.get_by_role("button", name=f"Move {markup_stage} down").count() == 1


def test_board_renders_lifecycle_rows_and_project_columns(open_page):
    page = open_page(StaticClient(board_payload()))
    assert page.get_by_label("Search", exact=True).is_visible()
    assert page.locator(".brand-name").inner_text() == "Nyx"
    assert page.locator(".brand-attribution").inner_text() == "by D10 Analytics"
    assert page.locator(".brand-logo").evaluate("image => image.complete && image.naturalWidth > 0")
    assert page.evaluate("async () => (await document.fonts.load('40px Anta')).length > 0")
    assert page.locator(".brand-name").evaluate("node => getComputedStyle(node).color") == "rgb(242, 200, 75)"
    assert page.locator(".column-head").all_inner_texts() == ["Alpha", "Beta"]
    rows = page.locator(".row-head").all_inner_texts()
    assert rows == ["Queue", "Under Development"]
    assert page.locator("#board .card").count() == 4


def test_browser_accepts_explicit_blank_declared_metadata(open_page):
    value = json.loads(board_payload())
    value["entries"][0]["declared"]["status"] = ""
    _reseal(value)
    page = open_page(StaticClient(value))
    assert page.locator("#board .card").count() == 4


def test_initial_fetch_failure_recovers_on_the_first_successful_poll(open_page):
    client = SequenceClient([CatalogError("producer_unavailable"), board_payload()])
    page = open_page(client, fake_clock=True)
    page.locator("#board").get_by_text("Latest refresh issue: producer_unavailable", exact=True).wait_for(
        timeout=15000
    )
    fire_poll(page)
    page.locator("#board .card").first.wait_for(timeout=5000)
    assert page.locator("#board .card").count() == 4
    assert page.locator("#board-issues").count() == 0
    assert client.calls == 2


def test_dependency_connectors_are_drawn_inside_a_project_column(open_page):
    page = open_page(StaticClient(board_payload()))
    assert card_titles(page) == ["Foundation step", "Dependent step"]
    rails = page.locator(".rail-layer .rail")
    assert rails.count() == 3


def test_cross_project_arrows_retain_inline_names(open_page):
    page = open_page(StaticClient(board_payload()))
    loose = page.locator('.card[data-package-path="Beta/Under_Development/loose"]')
    assert "needs:" in loose.inner_text()
    assert "Foundation step" in loose.inner_text()
    assert "Alpha" in loose.inner_text()
    step_one = page.locator('.card[data-package-path="Alpha/Under_Development/step-one"]')
    assert "blocks:" in step_one.inner_text()
    assert "Beta" in step_one.inner_text()
    assert connection_pairs(page) == {(STEP_ONE, STEP_TWO), (STEP_TWO, GATE), (STEP_ONE, LOOSE)}
    assert_readable_arrows(page)


def test_board_labels_and_toolbar_follow_both_scroll_directions(open_page):
    def aligned_geometry():
        # ResizeObserver updates the sticky offset after layout. Sample the
        # complete geometry together so a resize cannot split the observation.
        snapshot = page.wait_for_function("""() => {
            const toolbar = document.querySelector('.toolbar').getBoundingClientRect();
            const column = [...document.querySelectorAll('.column-head')].at(-1).getBoundingClientRect();
            const row = [...document.querySelectorAll('.row-head')].at(-1).getBoundingClientRect();
            const cell = [...document.querySelectorAll('.board-row .cell')].at(-1).getBoundingClientRect();
            const offset = parseFloat(getComputedStyle(document.documentElement)
                .getPropertyValue('--toolbar-height'));
            if (!Number.isFinite(offset) || window.scrollX <= 400 || window.scrollY <= 300 ||
                Math.abs(offset - toolbar.height) >= 2 ||
                Math.abs(toolbar.x) >= 2 || Math.abs(toolbar.y) >= 2 ||
                Math.abs(column.y - toolbar.height) >= 2 || Math.abs(row.x) >= 2) {
                return false;
            }
            return {toolbar: toolbar.toJSON(), column: column.toJSON(),
                    row: row.toJSON(), cell: cell.toJSON()};
        }""", timeout=5000)
        try:
            return snapshot.json_value()
        finally:
            snapshot.dispose()

    value = json.loads(board_payload())
    for index in range(8):
        project = f"Project {index:02d}"
        stage = f"Stage {index:02d}"
        value["inventory"]["projects"].append({"name": project, "availability": "complete"})
        value["inventory"]["stages"].append({
            "project": project, "stage": stage, "availability": "complete",
        })
        value["entries"].append(_entry(
            f"123e4567-e89b-42d3-a456-426614174{index + 100:03d}",
            f"{project}/{stage}/item", "under_development", f"Item {index}", project,
        ))
    value["inventory"]["stages"].sort(key=lambda item: (item["project"], item["stage"]))
    value["entries"].sort(key=lambda item: item["package_path"])
    _reseal(value)
    page = open_page(StaticClient(json.dumps(value).encode()))
    page.set_viewport_size({"width": 640, "height": 400})
    page.evaluate("() => document.fonts.ready.then(() => undefined)")
    page.locator("#board .card").first.wait_for()
    assert page.locator(".column-head").last.bounding_box()["width"] < 350
    page.evaluate("window.scrollTo(900, 600)")
    geometry = aligned_geometry()
    toolbar, column, row = (geometry[key] for key in ("toolbar", "column", "row"))
    assert abs(toolbar["x"]) < 2 and abs(toolbar["y"]) < 2
    assert abs(column["y"] - toolbar["height"]) < 2
    assert abs(row["x"]) < 2
    assert row["height"] >= geometry["cell"]["height"]
    assert page.locator(".row-head").last.evaluate("element => getComputedStyle(element).backgroundColor") != "rgba(0, 0, 0, 0)"

    page.locator('.card[data-package-path="Alpha/Under_Development/step-one"]').click()
    page.evaluate("window.scrollTo(900, 600)")
    expanded_geometry = aligned_geometry()
    expanded_toolbar = expanded_geometry["toolbar"]
    assert abs(expanded_toolbar["height"] - toolbar["height"]) < 2
    assert abs(expanded_geometry["column"]["y"] -
               expanded_toolbar["height"]) < 2


def test_reported_values_and_declared_slots_stay_searchable(open_page, tmp_path):
    _write_package(tmp_path, "Alpha", "Queue", "searchable", STEP_ONE,
                   f"# Searchable\nPackage ID: {STEP_ONE}\nOwner Note: awaiting budget\nClosure: approved\nStatus: zqstatus\nTarget repo: /work/zqtarget\n")
    _write_package(tmp_path, "Alpha", "Queue", "other", STEP_TWO,
                   f"# Other\nPackage ID: {STEP_TWO}\n")
    value = json.loads(scan_catalog(tmp_path))
    entry = next(entry for entry in value["entries"] if entry["package_id"] == STEP_ONE)
    assert entry["reported_fields"] == [{"name": "Closure", "value": "approved"}, {"name": "Owner Note", "value": "awaiting budget"}]
    assert entry["declared"]["status"] == "zqstatus"
    assert entry["declared"]["target_project"] == "zqtarget"
    page = open_page(ScanningClient(tmp_path))
    card = page.locator(f'.card[data-package-id="{STEP_ONE}"]')
    other = page.locator(f'.card[data-package-id="{STEP_TWO}"]')
    for term in ("budget", "approved", "zqstatus", "zqtarget"):
        page.fill("#filter", term)
        playwright.expect(card).to_be_visible()
        playwright.expect(other).to_be_hidden()
    assert "awaiting budget" not in card.inner_text()
    assert "approved" not in card.inner_text()
    for name in ("owner note", "closure"):
        page.fill("#filter", name)
        playwright.expect(card).to_be_hidden()
    page.fill("#filter", "")
    card.focus()
    page.keyboard.press("Enter")
    assert card.get_attribute("aria-pressed") == "true"


def test_resolved_program_title_from_descriptor_is_searchable(open_page, tmp_path):
    program_id = "123e4567-e89b-42d3-a456-426614174020"
    program_dir = tmp_path / "Alpha" / "Reference" / "Programs" / program_id
    program_dir.mkdir(parents=True)
    program_dir.joinpath("program.md").write_text(
        f"Program ID: {program_id}\nProgram Title: zqprogram\n", encoding="utf-8"
    )
    _write_package(tmp_path, "Alpha", "Queue", "member", STEP_ONE,
                   f"# Member\nPackage ID: {STEP_ONE}\nProgram Membership: {program_id}\n")
    _write_package(tmp_path, "Alpha", "Queue", "nonmember", STEP_TWO,
                   f"# Nonmember\nPackage ID: {STEP_TWO}\n")
    value = json.loads(scan_catalog(tmp_path))
    entries = {entry["package_id"]: entry for entry in value["entries"]}
    assert entries[STEP_ONE]["relationship"]["program"] == {
        "program_id": program_id, "title": "zqprogram", "resolution": "resolved",
    }
    assert entries[STEP_TWO]["relationship"]["program"]["resolution"] == "not_declared"
    for entry in entries.values():
        without_title = json.loads(json.dumps(entry))
        without_title["relationship"]["program"]["title"] = None
        assert "zqprogram" not in json.dumps(without_title)
    page = open_page(ScanningClient(tmp_path))
    member = page.locator(f'.card[data-package-id="{STEP_ONE}"]')
    nonmember = page.locator(f'.card[data-package-id="{STEP_TWO}"]')
    playwright.expect(nonmember).to_be_visible()
    page.fill("#filter", "zqprogram")
    playwright.expect(member).to_be_visible()
    playwright.expect(nonmember).to_be_hidden()


def test_long_unbroken_names_wrap_within_their_column(open_page):
    token = "x" * 300
    value = json.loads(board_payload())
    projects = ["Alpha", "Beta", token]
    value["inventory"] = {
        "projects": [{"name": project, "availability": "complete"} for project in projects],
        "stages": [
            {"project": project, "stage": "Queue", "availability": "complete"}
            for project in projects
        ],
    }
    value["entries"] = [
        _entry(STEP_ONE, f"Alpha/Queue/{token}", "queue", token, "Alpha", [_edge(STEP_TWO, "input")]),
        _entry(STEP_TWO, "Beta/Queue/source", "queue", token, "Beta"),
        _entry(GATE, f"{token}/Queue/item", "queue", "Third project item", token),
    ]
    value["entries"][0]["declared"]["target_project"] = token
    _reseal(value)
    page = open_page(StaticClient(value))
    alpha = page.locator(f'.card[data-package-id="{STEP_ONE}"]')
    beta = page.locator(f'.card[data-package-id="{STEP_TWO}"]')
    assert alpha.locator(".card-title").inner_text() == token
    assert alpha.locator(".card-project").inner_text() == f"Target Folder: {token}"
    assert alpha.locator(".card-filepath").inner_text() == f"Filepath: Alpha/Queue/{token}"
    assert alpha.locator(".card-links").inner_text() == f"needs: {token} Beta"
    assert beta.locator(".card-links").inner_text() == f"blocks: {token} Alpha"
    assert page.locator(".column-head").all_text_contents() == projects

    measurements = page.locator(
        ".card-title, .card-project, .card-filepath, .card-links, .column-head"
    ).evaluate_all("""nodes => nodes.map(node => ({
      kind: node.className, text: node.textContent,
      scrollWidth: node.scrollWidth, clientWidth: node.clientWidth,
    }))""")
    for measurement in measurements:
        assert measurement["scrollWidth"] <= measurement["clientWidth"] + 1, measurement
    bounds = page.locator(".card").evaluate_all("""cards => cards.map(card => ({
      right: card.getBoundingClientRect().right,
      cellRight: card.closest('.cell').getBoundingClientRect().right,
    }))""")
    assert len(bounds) == 3
    for bound in bounds:
        assert bound["right"] <= bound["cellRight"] + 1, bound


def test_minimal_item_omits_repeated_target_context_but_keeps_distinct_target(open_page):
    value = json.loads(board_payload())
    value["entries"][1]["declared"]["target_project"] = None
    value["entries"][3]["declared"]["target_project"] = "Declared target"
    _reseal(value)
    page = open_page(StaticClient(value))
    foundation = page.locator(f'.card[data-package-id="{STEP_ONE}"]')
    loose = page.locator(f'.card[data-package-id="{LOOSE}"]')
    assert foundation.locator(".card-project").count() == 0
    assert loose.locator(".card-project").inner_text() == "Target Folder: Declared target"
    assert foundation.locator(".card-filepath").inner_text() == (
        "Filepath: Alpha/Under_Development/step-one"
    )
    assert loose.locator(".card-filepath").inner_text() == (
        "Filepath: Beta/Under_Development/loose"
    )


def test_card_titles_are_escaped_and_never_render_markup(open_page):
    page = open_page(StaticClient(board_payload()))
    assert page.locator("#board img").count() == 0
    loose = page.locator('.card[data-package-path="Beta/Under_Development/loose"]')
    assert XSS_TITLE in loose.locator(".card-title").inner_text()


def test_filter_hides_non_matching_cards_and_their_rails(open_page):
    page = open_page(StaticClient(board_payload()))
    page.fill("#filter", "dependent")
    assert page.locator("#board .card:visible").count() == 1
    assert "Dependent step" in page.locator("#board .card:visible").inner_text()
    assert page.locator(".rail-layer .rail").count() == 0
    page.fill("#filter", "")
    assert page.locator("#board .card:visible").count() == 4
    assert page.locator(".rail-layer .rail").count() == 3


def test_poll_exposes_one_pending_update_until_applied(open_page):
    first = board_payload()
    second = board_payload(titles={"step_one": "Renamed foundation"})
    client = SequenceClient([first, second])
    # The page's only interval is the poll; record the page time at which it
    # is registered so both bounds are measured from registration, however
    # long the launch took. The init script runs after the clock is installed,
    # so the wrapped setInterval is the fake clock's.
    record_registration = (
        "(() => { const real = window.setInterval.bind(window);"
        " window.setInterval = (...args) => {"
        "   if (window.__pollRegisteredAt === undefined)"
        "     window.__pollRegisteredAt = performance.now();"
        "   return real(...args); }; })();"
    )
    page = open_page(client, fake_clock=True, init_script=record_registration)
    # Pause page time (pause_at takes seconds) so the bounds do not depend on
    # real time; frames do not run while paused, so only fast_forward advances.
    page.clock.pause_at(page.evaluate("Date.now()") / 1000 + 1)
    registered = page.evaluate("window.__pollRegisteredAt")
    assert registered is not None, "the page never registered its poll interval"
    now = page.evaluate("performance.now()")
    assert now - registered < 9_000, f"launch took {now - registered} ms past registration"
    # No poll before the documented interval has elapsed since registration.
    # Stop 100 ms short of 9 s so a poll due exactly at 9 s is not a race, and
    # give any request a poll fired in that span real time to reach the server.
    page.clock.fast_forward(registered + 8_900 - now)
    page.wait_for_timeout(250)
    assert client.calls == 1
    # A poll arrives by 10.4 s after registration.
    with page.expect_response("**/api/catalog", timeout=3000):
        page.clock.fast_forward(1_500)
    page.wait_for_selector("#refresh.pending", timeout=5000)
    assert client.calls == 2
    assert page.locator("#refresh").inner_text() == "Apply update"
    assert "Foundation step" in page.locator("#board").inner_text()
    page.click("#refresh")
    page.wait_for_selector("#board .card", timeout=15000)
    assert "Renamed foundation" in page.locator("#board").inner_text()
    assert page.locator("#refresh").inner_text() == "Refresh view"


def test_saved_stage_order_projects_rows_without_phantom_or_catalog_changes(open_page):
    settings = BrowserSettings(order=["Missing", "Under_Development", "Hidden", "Queue"])
    value = json.loads(board_payload())
    digest = value["catalog_digest"]
    page = open_page(StaticClient(value), settings=settings)
    page.locator("#stage-order-editor").wait_for()
    open_stage_editor(page)

    assert row_labels(page) == ["Under Development", "Queue"]
    assert page.locator(".row-head").all_inner_texts() == ["Under Development", "Queue"]
    assert page.locator("#stage-order-list .stage-order-item").count() == 4
    missing_editor_row = page.locator(
        "#stage-order-list .stage-order-item[data-stage='Missing']"
    )
    assert missing_editor_row.count() == 1
    assert "not currently available" in missing_editor_row.inner_text()
    assert value["catalog_digest"] == digest
    assert page.locator("[data-lifecycle='Missing']").count() == 0


def test_saved_order_survives_natural_add_hide_remove_and_refill_updates(open_page):
    first = json.loads(board_payload())
    added = json.loads(board_payload())
    _admit_inventory_stage(added, "Alpha", "Review")
    added["entries"].append(_entry(
        "123e4567-e89b-42d3-a456-426614174099",
        "Alpha/Review/new", "under_development", "New review", "Alpha",
    ))
    added["entries"].sort(key=lambda entry: entry["package_path"])
    _reseal(added)
    hidden = json.loads(json.dumps(added))
    hidden["terminal_stages"].append("Queue")
    _reseal(hidden)
    removed = json.loads(board_payload())
    recreated = json.loads(json.dumps(added))
    empty = json.loads(json.dumps(added))
    empty["entries"] = []
    _reseal(empty)
    refilled = json.loads(json.dumps(added))
    settings = BrowserSettings(order=["Queue", "Under_Development"])
    client = SequenceClient([first, added, hidden, removed, recreated, empty, refilled])
    page = open_page(client, settings=settings)
    page.locator("#stage-order-editor").wait_for()
    open_stage_editor(page)
    assert row_labels(page) == ["Queue", "Under Development"]

    page.get_by_role("button", name="Refresh view").click()
    page.wait_for_function("() => document.querySelectorAll('.row-head').length === 3")
    assert row_labels(page) == ["Queue", "Under Development", "Review"]
    assert stage_editor_order(page) == ["Queue", "Under_Development", "Review"]
    assert page.get_by_role("button", name="Save").is_disabled()
    assert settings.calls == []
    page.get_by_role("button", name="Refresh view").click()
    page.wait_for_function("() => document.querySelectorAll('.row-head').length === 2")
    assert row_labels(page) == ["Under Development", "Review"]
    assert page.locator("[data-lifecycle='Queue']").count() == 0
    page.get_by_role("button", name="Refresh view").click()
    page.wait_for_function(
        "() => document.querySelector('.row-head')?.firstChild.textContent === 'Queue'"
    )
    assert row_labels(page) == ["Queue", "Under Development"]
    assert page.locator("[data-lifecycle='Review']").count() == 0
    assert stage_editor_order(page) == ["Queue", "Under_Development"]
    assert page.get_by_role("button", name="Save").is_disabled()
    assert settings.calls == []
    page.get_by_role("button", name="Refresh view").click()
    page.wait_for_function("() => document.querySelectorAll('.row-head').length === 3")
    assert row_labels(page) == ["Queue", "Under Development", "Review"]
    page.get_by_role("button", name="Refresh view").click()
    page.locator("#board .board-empty").wait_for()
    assert row_labels(page) == []
    page.get_by_role("button", name="Refresh view").click()
    page.wait_for_function("() => document.querySelectorAll('.row-head').length === 3")
    assert row_labels(page) == ["Queue", "Under Development", "Review"]
    assert settings.order == ["Queue", "Under_Development"]


def test_genuine_stage_order_draft_survives_natural_stage_removal(open_page):
    first = json.loads(board_payload())
    added = json.loads(board_payload())
    _admit_inventory_stage(added, "Alpha", "Review")
    added["entries"].append(_entry(
        "123e4567-e89b-42d3-a456-426614174099",
        "Alpha/Review/new", "under_development", "New review", "Alpha",
    ))
    added["entries"].sort(key=lambda entry: entry["package_path"])
    _reseal(added)
    removed = json.loads(board_payload())
    settings = BrowserSettings(order=["Queue", "Under_Development"])
    page = open_page(SequenceClient([first, added, removed]), settings=settings)
    page.locator("#stage-order-editor").wait_for()
    open_stage_editor(page)

    page.get_by_role("button", name="Refresh view").click()
    page.wait_for_function("() => document.querySelectorAll('.row-head').length === 3")
    assert stage_editor_order(page) == ["Queue", "Under_Development", "Review"]
    page.get_by_role("button", name="Move Review up").press("Enter")
    assert stage_editor_order(page) == ["Queue", "Review", "Under_Development"]
    assert page.get_by_role("button", name="Save").is_enabled()
    assert settings.calls == []

    page.get_by_role("button", name="Refresh view").click()
    page.wait_for_function("() => document.querySelectorAll('.row-head').length === 2")
    assert row_labels(page) == ["Queue", "Under Development"]
    assert stage_editor_order(page) == ["Queue", "Review", "Under_Development"]
    assert "not currently available" in page.locator(
        "#stage-order-list .stage-order-item[data-stage='Review']"
    ).inner_text()
    assert page.get_by_role("button", name="Save").is_enabled()
    assert settings.calls == []


def test_keyboard_stage_editor_save_cancel_reset_and_reload(open_page):
    settings = BrowserSettings()
    page = open_page(StaticClient(board_payload()), settings=settings)
    page.locator("#stage-order-editor").wait_for()
    open_stage_editor(page)
    page.get_by_role("button", name="Move Under Development up").focus()
    page.keyboard.press("Enter")
    playwright.expect(
        page.get_by_role("button", name="Move Under Development down")
    ).to_be_focused()
    assert settings.calls == []
    page.get_by_role("button", name="Cancel").click()
    assert settings.calls == []
    assert page.locator("#stage-order-status").inner_text() == (
        "Unsaved board settings changes cancelled."
    )
    page.get_by_role("button", name="Move Under Development up").press("Enter")
    page.get_by_role("button", name="Save").click()
    page.get_by_text("Board settings saved.", exact=True).wait_for()
    assert settings.calls == [("revision-1", ["Under_Development", "Queue"], [])]
    page.wait_for_function(
        "() => JSON.stringify([...document.querySelectorAll('.row-head')].map(row => row.firstChild.textContent)) === "
        "JSON.stringify(['Under Development', 'Queue'])"
    )
    assert row_labels(page) == ["Under Development", "Queue"]

    page2 = open_page(StaticClient(board_payload()), settings=settings)
    page2.locator("#stage-order-editor").wait_for()
    open_stage_editor(page2)
    assert row_labels(page2) == ["Under Development", "Queue"]
    page2.get_by_role("button", name="Reset").click()
    page2.get_by_text("Board settings saved.", exact=True).wait_for()
    assert settings.order == []
    page2.wait_for_function(
        "() => JSON.stringify([...document.querySelectorAll('.row-head')].map(row => row.firstChild.textContent)) === "
        "JSON.stringify(['Queue', 'Under Development'])"
    )
    assert row_labels(page2) == ["Queue", "Under Development"]


def arm_focus_sampler(page):
    page.evaluate("""() => {
      window.focusViolations = [];
      const check = () => {
        if (!document.activeElement || document.activeElement === document.body) {
          window.focusViolations.push(performance.now());
        }
      };
      const frame = () => { check(); requestAnimationFrame(frame); };
      requestAnimationFrame(frame);
      for (const event of ['focusin', 'focusout']) {
        document.addEventListener(event, () => setTimeout(check, 0));
      }
    }""")


def assert_focus_sampler_clean(page):
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => setTimeout(resolve, 0)))")
    assert page.evaluate("window.focusViolations") == []


def test_stage_editor_focus_stays_on_used_control_or_successor(open_page):
    settings = BrowserSettings()
    page = open_page(StaticClient(board_payload()), settings=settings)
    page.locator("#stage-order-heading").press("Enter")
    checkbox = page.get_by_role("checkbox", name="Counts as finished: Queue", exact=True)
    checkbox.focus()
    arm_focus_sampler(page)
    kept = checkbox.element_handle()
    checkbox.press("Space")
    playwright.expect(checkbox).to_be_checked()
    playwright.expect(checkbox).to_be_focused()
    assert not kept.evaluate("node => node.isConnected")
    assert_focus_sampler_clean(page)

    page.get_by_role("button", name="Cancel", exact=True).press("Enter")
    playwright.expect(page.get_by_role("button", name="Reset", exact=True)).to_be_focused()
    page.get_by_role("button", name="Move Queue down", exact=True).press("Enter")
    playwright.expect(page.get_by_role("button", name="Move Queue up", exact=True)).to_be_focused()
    assert_focus_sampler_clean(page)
    page.get_by_role("button", name="Cancel", exact=True).press("Enter")
    assert stage_editor_order(page) == ["Queue", "Under_Development"]
    playwright.expect(page.get_by_role("button", name="Reset", exact=True)).to_be_focused()
    page.get_by_role("button", name="Move Under Development up", exact=True).press("Enter")
    playwright.expect(page.get_by_role("button", name="Move Under Development down", exact=True)).to_be_focused()
    assert_focus_sampler_clean(page)
    page.get_by_role("button", name="Cancel", exact=True).press("Enter")
    playwright.expect(page.get_by_role("button", name="Reset", exact=True)).to_be_focused()
    assert_focus_sampler_clean(page)

    page.get_by_role("button", name="Move Under Development up", exact=True).press("Enter")
    page.get_by_role("button", name="Save", exact=True).press("Enter")
    playwright.expect(page.locator(".row-head")).to_have_text(["Under Development", "Queue"])
    playwright.expect(page.get_by_role("button", name="Reset", exact=True)).to_be_focused()
    assert settings.calls == [("revision-1", ["Under_Development", "Queue"], [])]
    assert settings.get_calls == 2
    assert_focus_sampler_clean(page)

    page.get_by_role("button", name="Reset", exact=True).press("Enter")
    playwright.expect(page.locator(".row-head")).to_have_text(["Queue", "Under Development"])
    playwright.expect(page.get_by_role("button", name="Reset", exact=True)).to_be_focused()
    assert settings.calls == [
        ("revision-1", ["Under_Development", "Queue"], []), ("revision-2", [], []),
    ]
    assert settings.get_calls == 3
    assert_focus_sampler_clean(page)

    winner = open_page(StaticClient(board_payload()), settings=settings)
    winner.locator("#stage-order-heading").press("Enter")
    with winner.expect_response(lambda response: response.url.endswith("/api/settings") and response.request.method == "GET"):
        winner.get_by_role("button", name="Reset", exact=True).press("Enter")
    playwright.expect(winner.get_by_role("button", name="Reset", exact=True)).to_be_enabled()
    assert settings.calls[-1] == ("revision-3", [], [])
    assert settings.get_calls == 5
    page.get_by_role("button", name="Move Under Development up", exact=True).press("Enter")
    page.get_by_role("button", name="Save", exact=True).press("Enter")
    reload = page.get_by_role("button", name="Reload board settings", exact=True)
    playwright.expect(reload).to_be_focused()
    assert settings.calls[-1] == ("revision-3", ["Under_Development", "Queue"], [])
    assert len(settings.calls) == 4
    assert settings.get_calls == 5
    assert_focus_sampler_clean(page)
    reload.press("Enter")
    playwright.expect(page.locator("#stage-order-status")).to_have_text("Current board settings loaded.")
    playwright.expect(page.get_by_role("button", name="Reset", exact=True)).to_be_focused()
    assert settings.get_calls == 6
    assert len(settings.calls) == 4
    assert_focus_sampler_clean(page)


def test_stage_editor_focus_survives_held_settings_requests(open_page):
    settings = BrowserSettings()
    page = open_page(StaticClient(board_payload()), settings=settings)
    page.locator("#stage-order-heading").press("Enter")
    page.get_by_role("button", name="Move Under Development up", exact=True).press("Enter")
    save = page.get_by_role("button", name="Save", exact=True)
    save.focus()
    arm_focus_sampler(page)
    settings.gate = threading.Event()
    try:
        save.press("Enter")
        assert settings.entered.wait(timeout=2)
        playwright.expect(save).to_be_focused()
        assert save.get_attribute("aria-disabled") == "true"
        assert save.get_attribute("disabled") is None
        assert float(save.evaluate("node => getComputedStyle(node).opacity")) < 1
        checkbox = page.get_by_role("checkbox", name="Counts as finished: Under Development", exact=True)
        checkbox.focus()
        kept = checkbox.element_handle()
        checkbox.press("Space")
        assert not checkbox.is_checked()
        assert settings.calls == [("revision-1", ["Under_Development", "Queue"], [])]
        assert_focus_sampler_clean(page)
    finally:
        settings.gate.set()
    playwright.expect(page.locator(".row-head")).to_have_text(["Under Development", "Queue"])
    playwright.expect(checkbox).to_be_focused()
    assert not kept.evaluate("node => node.isConnected")
    assert settings.get_calls == 2
    assert len(settings.calls) == 1
    assert_focus_sampler_clean(page)

    failed = BrowserSettings()
    failed.fail_next_load = True
    retry_page = open_page(StaticClient(board_payload()), settings=failed)
    retry_page.locator("#stage-order-heading").press("Enter")
    reload = retry_page.get_by_role("button", name="Reload board settings", exact=True)
    reload.focus()
    arm_focus_sampler(retry_page)
    failed.gate = threading.Event()
    try:
        reload.press("Enter")
        assert failed.entered.wait(timeout=2)
        playwright.expect(reload).to_be_focused()
        assert reload.get_attribute("aria-disabled") == "true"
        assert reload.get_attribute("disabled") is None
        assert failed.get_calls == 2
        assert failed.calls == []
        assert_focus_sampler_clean(retry_page)
    finally:
        failed.gate.set()
    playwright.expect(retry_page.locator("#stage-order-status")).to_have_text("Current board settings loaded.")
    playwright.expect(retry_page.get_by_role("button", name="Reset", exact=True)).to_be_focused()
    assert failed.get_calls == 2
    assert failed.calls == []
    assert_focus_sampler_clean(retry_page)


def test_manual_refresh_keeps_focus_outside_stage_editor(open_page):
    settings = BrowserSettings()
    page = open_page(StaticClient(board_payload()), settings=settings)
    page.locator("#stage-order-heading").press("Enter")
    refresh = page.get_by_role("button", name="Refresh view", exact=True)
    refresh.focus()
    arm_focus_sampler(page)
    card = page.locator(".card").first.element_handle()
    with page.expect_response("**/api/catalog"):
        refresh.press("Enter")
    page.wait_for_function("node => !node.isConnected", arg=card)
    playwright.expect(refresh).to_be_focused()
    assert_focus_sampler_clean(page)


def test_stage_order_retries_failed_initial_load_without_page_reload(open_page):
    settings = BrowserSettings()
    settings.fail_next_load = True
    page = open_page(StaticClient(board_payload()), settings=settings)
    page.locator("#stage-order-editor").wait_for()
    open_stage_editor(page)

    page.get_by_text(
        "Could not load board settings: producer_unavailable", exact=True
    ).wait_for()
    assert settings.get_calls == 1
    assert page.get_by_role("button", name="Save").is_disabled()
    retry = page.get_by_role("button", name="Reload board settings")
    assert retry.is_visible()

    retry.click()
    page.get_by_text("Current board settings loaded.", exact=True).wait_for()
    assert settings.get_calls == 2
    assert stage_editor_order(page) == ["Queue", "Under_Development"]
    page.get_by_role("button", name="Move Under Development up").click()
    page.get_by_role("button", name="Save").click()
    page.get_by_text("Board settings saved.", exact=True).wait_for()
    assert settings.calls == [("revision-1", ["Under_Development", "Queue"], [])]


def test_stage_order_conflict_reloads_current_revision_and_saves_without_page_reload(open_page):
    settings = BrowserSettings()
    stale_page = open_page(StaticClient(board_payload()), settings=settings)
    winning_page = open_page(StaticClient(board_payload()), settings=settings)
    stale_page.locator("#stage-order-editor").wait_for()
    winning_page.locator("#stage-order-editor").wait_for()
    open_stage_editor(stale_page)
    open_stage_editor(winning_page)
    assert settings.get_calls == 2

    winning_page.get_by_role("button", name="Reset").click()
    winning_page.get_by_text("Board settings saved.", exact=True).wait_for()
    assert settings.calls == [("revision-1", [], [])]

    stale_page.get_by_role("button", name="Move Under Development up").click()
    stale_page.get_by_role("button", name="Save").click()
    stale_page.get_by_text(
        "Save not applied: conflict. Reload board settings to replace this unsaved draft and try again.",
        exact=True,
    ).wait_for()
    assert stage_editor_order(stale_page) == ["Under_Development", "Queue"]
    assert stale_page.get_by_role("button", name="Save").is_disabled()
    reload = stale_page.get_by_role("button", name="Reload board settings")
    assert reload.is_visible()

    reload.click()
    stale_page.get_by_text("Current board settings loaded.", exact=True).wait_for()
    assert settings.get_calls == 4
    assert stage_editor_order(stale_page) == ["Queue", "Under_Development"]
    stale_page.get_by_role("button", name="Move Under Development up").click()
    stale_page.get_by_role("button", name="Save").click()
    stale_page.get_by_text("Board settings saved.", exact=True).wait_for()
    assert settings.calls == [
        ("revision-1", [], []),
        ("revision-1", ["Under_Development", "Queue"], []),
        ("revision-2", ["Under_Development", "Queue"], []),
    ]


def test_stage_order_save_failure_keeps_editor_usable_and_stale_response_requires_reload(open_page):
    settings = BrowserSettings()
    stale_page = open_page(StaticClient(board_payload()), settings=settings)
    winning_page = open_page(StaticClient(board_payload()), settings=settings)
    stale_page.locator("#stage-order-editor").wait_for()
    winning_page.locator("#stage-order-editor").wait_for()
    open_stage_editor(stale_page)
    open_stage_editor(winning_page)

    winning_page.get_by_role("button", name="Move Under Development up").press("Enter")
    winning_page.get_by_role("button", name="Save").click()
    winning_page.get_by_text("Board settings saved.", exact=True).wait_for()
    assert settings.order == ["Under_Development", "Queue"]
    winning_page.wait_for_function(
        "() => JSON.stringify([...document.querySelectorAll('.row-head')].map(row => row.firstChild.textContent)) === "
        "JSON.stringify(['Under Development', 'Queue'])"
    )
    assert row_labels(winning_page) == ["Under Development", "Queue"]

    stale_page.get_by_role("button", name="Move Under Development up").press("Enter")
    stale_page.get_by_role("button", name="Save").click()
    stale_page.get_by_text(re.compile("Save not applied: conflict"), exact=False).wait_for()
    assert settings.order == ["Under_Development", "Queue"]
    assert stale_page.get_by_role("button", name="Save").is_disabled()
    assert stale_page.get_by_role("button", name="Reload board settings").is_visible()

    settings.fail_next = True
    winning_page.get_by_role("button", name="Move Under Development down").press("Enter")
    winning_page.get_by_role("button", name="Save").click()
    winning_page.get_by_text(
        "Save failed: the board settings were not persisted.", exact=True
    ).wait_for()
    assert settings.order == ["Under_Development", "Queue"]
    assert row_labels(winning_page) == ["Under Development", "Queue"]
    assert winning_page.locator(
        "#stage-order-list .stage-order-item"
    ).evaluate_all("items => items.map(item => item.dataset.stage)") == [
        "Queue",
        "Under_Development",
    ]
    assert winning_page.get_by_role("button", name="Save").is_enabled()


def test_stage_order_save_reports_an_error_code_then_reload_needed(open_page):
    class QueuedAnswerSettings(BrowserSettings):
        def __init__(self):
            super().__init__()
            self.answers = []

        def save_settings(self, revision, order, completed):
            if not self.answers:
                return super().save_settings(revision, order, completed)
            self.calls.append((revision, list(order), list(completed)))
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

    settings = QueuedAnswerSettings()
    settings.answers = [
        CatalogError("settings_unavailable"),
        {"order": [], "completed": [], "revision": None, "outcome": "reload-needed"},
    ]
    page = open_page(StaticClient(board_payload()), settings=settings)
    page.locator("#stage-order-editor").wait_for()
    open_stage_editor(page)
    page.get_by_role("button", name="Move Under Development up").click()
    draft = ["Under_Development", "Queue"]
    save = page.get_by_role("button", name="Save")

    save.click()
    page.get_by_text("Save failed: settings_unavailable", exact=True).wait_for()
    assert stage_editor_order(page) == draft
    assert save.is_enabled()

    save.click()
    page.get_by_text(
        "Save not applied: reload-needed. Reload board settings to replace this unsaved draft and try again.",
        exact=True,
    ).wait_for()
    assert stage_editor_order(page) == draft
    assert page.get_by_role("button", name="Reload board settings").is_visible()
    assert save.is_disabled()
    assert [call[1] for call in settings.calls] == [draft, draft]


CHANGED_ELSEWHERE = (
    "Board settings changed elsewhere. Reload board settings to replace this unsaved draft."
)
# Counts settings loads when the page starts them, so a check made right after
# a visible apply result also covers a load the server has not yet received.
SETTINGS_LOAD_COUNTER = """(() => {
  window.settingsLoads = 0;
  const original = window.fetch.bind(window);
  window.fetch = (resource, options = {}) => {
    if (String(resource).endsWith('/api/settings') && (options.method || 'GET') === 'GET') {
      window.settingsLoads += 1;
    }
    return original(resource, options);
  };
})();"""


def three_stage_payload(*, titles=None):
    value = json.loads(board_payload(titles=titles))
    _admit_inventory_stage(value, "Alpha", "Review")
    value["entries"].append(_entry(
        "123e4567-e89b-42d3-a456-426614174099",
        "Alpha/Review/new", "under_development", "New review", "Alpha",
    ))
    value["entries"].sort(key=lambda entry: entry["package_path"])
    _reseal(value)
    return value


def open_same_board(page):
    other = page.context.new_page()
    other.add_init_script(SETTINGS_LOAD_COUNTER)
    other.goto(page.url)
    other.wait_for_function(
        "() => document.querySelector('#board .card, #board .board-empty, #board-issues')",
        timeout=15000,
    )
    return other


def settings_loads(page):
    return page.evaluate("window.settingsLoads")


def save_review_first_elsewhere(page, settings):
    """Save a new order from ``page`` and wait for its post-save refresh to apply."""
    open_stage_editor(page)
    page.get_by_role("button", name="Move Review up", exact=True).click()
    page.get_by_role("button", name="Save", exact=True).click()
    playwright.expect(page.locator(".row-head")).to_have_text(["Queue", "Review", "Under Development"])
    assert settings.order == ["Queue", "Review", "Under_Development"]


def test_apply_reloads_settings_saved_on_another_page_without_a_draft(open_page):
    settings = BrowserSettings(order=["Queue", "Under_Development", "Review"])
    page = open_page(
        StaticClient(three_stage_payload()), settings=settings, init_script=SETTINGS_LOAD_COUNTER,
    )
    playwright.expect(page.locator("#stage-order-status")).to_have_text("Current board settings loaded.")
    other = open_same_board(page)
    try:
        playwright.expect(other.locator("#stage-order-status")).to_have_text("Current board settings loaded.")
        playwright.expect(other.locator(".row-head")).to_have_text(["Queue", "Under Development", "Review"])

        before_save = settings.get_calls
        page_loads = settings_loads(page)
        save_review_first_elsewhere(page, settings)
        # The saving page refreshes through its post-save fetch alone.
        assert settings_loads(page) == page_loads + 1
        assert settings.get_calls == before_save + 1
        assert page.locator("#stage-order-status").inner_text() == "Board settings saved."

        before_apply = settings.get_calls
        other_loads = settings_loads(other)
        other.locator("#refresh").click()
        playwright.expect(other.locator(".row-head")).to_have_text(["Queue", "Review", "Under Development"])
        assert stage_editor_order(other) == ["Queue", "Review", "Under_Development"]
        assert settings_loads(other) == other_loads + 1
        assert settings.get_calls == before_apply + 1
    finally:
        other.close()


@pytest.mark.parametrize("draft", ["row-move", "completed-stage"])
def test_apply_keeps_unsaved_draft_when_settings_change_on_another_page(open_page, draft):
    settings = BrowserSettings(order=["Queue", "Under_Development", "Review"])
    page = open_page(StaticClient(three_stage_payload()), settings=settings)
    playwright.expect(page.locator("#stage-order-status")).to_have_text("Current board settings loaded.")
    other = open_same_board(page)
    try:
        status = other.locator("#stage-order-status")
        playwright.expect(status).to_have_text("Current board settings loaded.")
        open_stage_editor(other)
        queue_finished = other.get_by_role("checkbox", name="Counts as finished: Queue", exact=True)
        if draft == "row-move":
            other.get_by_role("button", name="Move Under Development up", exact=True).click()
            expected_draft = ["Under_Development", "Queue", "Review"]
        else:
            queue_finished.check()
            expected_draft = ["Queue", "Under_Development", "Review"]
        assert stage_editor_order(other) == expected_draft
        assert other.get_by_role("button", name="Save", exact=True).is_enabled()

        save_review_first_elsewhere(page, settings)
        before = settings.get_calls
        other_loads = settings_loads(other)
        other.locator("#refresh").click()
        playwright.expect(status).to_have_text(CHANGED_ELSEWHERE)
        assert stage_editor_order(other) == expected_draft
        assert queue_finished.is_checked() == (draft == "completed-stage")
        assert other.get_by_role("button", name="Save", exact=True).is_disabled()
        reload = other.get_by_role("button", name="Reload board settings", exact=True)
        assert reload.is_visible()
        assert settings_loads(other) == other_loads
        assert settings.get_calls == before

        reload.click()
        playwright.expect(status).to_have_text("Current board settings loaded.")
        assert stage_editor_order(other) == ["Queue", "Review", "Under_Development"]
        assert not queue_finished.is_checked()
        assert other.get_by_role("button", name="Save", exact=True).is_disabled()
        assert settings_loads(other) == other_loads + 1
        assert settings.get_calls == before + 1
    finally:
        other.close()


def test_apply_adds_no_settings_load_while_settings_await_reload(open_page):
    settings = BrowserSettings(order=["Queue", "Under_Development", "Review"])
    client = StaticClient(three_stage_payload())
    page = open_page(client, settings=settings)
    playwright.expect(page.locator("#stage-order-status")).to_have_text("Current board settings loaded.")
    settings.fail_next_load = True
    other = open_same_board(page)
    try:
        status = other.locator("#stage-order-status")
        failure = "Could not load board settings: producer_unavailable"
        playwright.expect(status).to_have_text(failure)

        save_review_first_elsewhere(page, settings)
        client.payload = three_stage_payload(titles={"step_one": "Renamed foundation"})
        before = settings.get_calls
        other_loads = settings_loads(other)
        other.locator("#refresh").click()
        playwright.expect(
            other.locator(f'.card[data-package-id="{STEP_ONE}"] .card-title')
        ).to_have_text("Renamed foundation")
        assert settings_loads(other) == other_loads
        assert settings.get_calls == before
        assert status.text_content() == failure
        open_stage_editor(other)
        assert other.get_by_role("button", name="Reload board settings", exact=True).is_visible()
    finally:
        other.close()


def test_settings_load_held_across_the_first_apply_is_the_only_load(open_page):
    settings = BrowserSettings(order=["Queue", "Under_Development", "Review"])
    page = open_page(StaticClient(three_stage_payload()), settings=settings)
    playwright.expect(page.locator("#stage-order-status")).to_have_text("Current board settings loaded.")
    before = settings.get_calls
    settings.gate = threading.Event()
    other = page.context.new_page()
    try:
        other.add_init_script(SETTINGS_LOAD_COUNTER)
        other.goto(page.url)
        assert settings.entered.wait(timeout=5)
        playwright.expect(
            other.locator(f'.card[data-package-id="{STEP_ONE}"] .card-title')
        ).to_have_text("Foundation step", timeout=15000)
        assert settings_loads(other) == 1
        settings.gate.set()
        playwright.expect(other.locator("#stage-order-status")).to_have_text("Current board settings loaded.")
        assert settings_loads(other) == 1
        assert settings.get_calls == before + 1
    finally:
        settings.gate.set()
        other.close()


class CommitThenHoldSettings(BrowserSettings):
    """Commit a save, then hold its response so the catalog runs ahead of the page."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hold = None
        self.committed = threading.Event()

    def save_settings(self, revision, order, completed):
        result = super().save_settings(revision, order, completed)
        if self.hold is not None:
            self.committed.set()
            self.hold.wait(timeout=10)
        return result


def test_apply_during_an_in_flight_save_keeps_the_editor_usable(open_page):
    settings = CommitThenHoldSettings(order=["Queue", "Under_Development", "Review"])
    client = StaticClient(three_stage_payload())
    page = open_page(client, settings=settings, init_script=SETTINGS_LOAD_COUNTER)
    status = page.locator("#stage-order-status")
    playwright.expect(status).to_have_text("Current board settings loaded.")
    open_stage_editor(page)
    page.get_by_role("button", name="Move Review up", exact=True).click()
    loads = settings_loads(page)
    settings.hold = threading.Event()
    try:
        page.get_by_role("button", name="Save", exact=True).click()
        assert settings.committed.wait(timeout=5)
        # The catalog already carries the saved revision while the page still
        # holds the previous one and its draft.
        client.payload = three_stage_payload(titles={"step_one": "Renamed foundation"})
        page.locator("#refresh").click()
        playwright.expect(
            page.locator(f'.card[data-package-id="{STEP_ONE}"] .card-title')
        ).to_have_text("Renamed foundation")
        assert status.text_content() == "Saving board settings…"
        assert settings_loads(page) == loads
    finally:
        settings.hold.set()
    playwright.expect(page.locator(".row-head")).to_have_text(["Queue", "Review", "Under Development"])
    playwright.expect(status).to_have_text("Board settings saved.")
    assert stage_editor_order(page) == ["Queue", "Review", "Under_Development"]
    assert not page.get_by_role("button", name="Reload board settings", exact=True).is_visible()
    assert page.get_by_role("button", name="Move Queue down", exact=True).is_enabled()
    assert page.get_by_role("button", name="Reset", exact=True).is_enabled()
    assert settings_loads(page) == loads + 1


def test_stage_reorder_keeps_selection_focus_and_rail_pairs(open_page):
    first = json.loads(board_payload())
    first["entries"].append(_entry(
        REVERSE,
        "Beta/Under_Development/reverse",
        "under_development",
        "Reverse dependent",
        "Beta",
        prerequisites=[_edge(GATE, "reverse")],
    ))
    first["entries"].sort(key=lambda entry: entry["package_path"])
    _reseal(first)
    pending = json.loads(json.dumps(first))
    for entry in pending["entries"]:
        if entry["package_id"] == STEP_ONE:
            entry["declared"]["title"] = "Pending foundation"
    _reseal(pending)
    settings = BrowserSettings()
    page = open_page(SequenceClient([first, pending]), settings=settings)
    page.locator("#stage-order-editor").wait_for()
    open_stage_editor(page)
    page.fill("#filter", "dependent")
    page.locator(f'.card[data-package-id="{STEP_TWO}"]').click()
    assert page.locator(f'.card[data-package-id="{STEP_TWO}"].selected').count() == 1
    page.get_by_role("button", name="Move Under Development up").press("Enter")
    playwright.expect(
        page.get_by_role("button", name="Move Under Development down")
    ).to_be_focused()
    assert page.locator("#stage-order-status").inner_text() == "Unsaved board row order changes."
    assert page.locator("#filter").input_value() == "dependent"
    page.get_by_role("button", name="Save").click()
    page.get_by_text("Board settings saved.", exact=True).wait_for()
    assert page.locator(f'.card[data-package-id="{STEP_TWO}"].selected').count() == 1
    assert page.locator("#filter").input_value() == "dependent"
    page.wait_for_function(
        "() => document.querySelector('#board')?.textContent.includes('Pending foundation')"
    )
    assert page.locator("#refresh").inner_text() == "Refresh view"
    page.locator("#board").get_by_text(
        "Pending foundation", exact=True
    ).wait_for(state="attached")
    assert page.locator(f'.card[data-package-id="{STEP_TWO}"].selected').count() == 1
    assert page.locator("#filter").input_value() == "dependent"
    page.fill("#filter", "")
    assert connection_pairs(page) == {
        (STEP_ONE, STEP_TWO),
        (STEP_TWO, GATE),
        (STEP_ONE, LOOSE),
        (GATE, REVERSE),
    }
    reverse = page.locator(f'.connection[data-source="{GATE}"][data-dependent="{REVERSE}"]')
    assert reverse.count() == 1
    assert reverse.evaluate("""group => {
      const source = document.querySelector(`[data-package-id="${group.dataset.source}"]`);
      const dependent = document.querySelector(`[data-package-id="${group.dataset.dependent}"]`);
      return source.getBoundingClientRect().top > dependent.getBoundingClientRect().top;
    }""")
    assert_readable_arrows(page)
    assert_rail_lanes_do_not_overlap(page)


def test_poll_reconciles_a_b_a_before_a_click_fetches_the_next_manual_snapshot(open_page):
    first = board_payload(titles={"step_one": "A"})
    middle = board_payload(titles={"step_one": "B"})
    latest = board_payload(titles={"step_one": "A"})
    manual = board_payload(titles={"step_one": "C"})
    client = SequenceClient([first, middle, latest, manual])
    page = open_page(client, fake_clock=True)

    # The browser's periodic poll observes B and then the displayed A again.
    fire_poll(page)
    page.wait_for_selector("#refresh.pending", timeout=5000)
    assert page.locator("#board").get_by_text("A", exact=True).count() == 1
    fire_poll(page)
    page.wait_for_function(
        "() => !document.querySelector('#refresh').classList.contains('pending')", timeout=5000
    )
    assert page.locator("#refresh").inner_text() == "Refresh view"
    assert page.locator("#board").get_by_text("B", exact=True).count() == 0

    # A stale B must not be applied by this click: it is now a normal manual refresh.
    page.click("#refresh")
    page.locator("#board").get_by_text("C", exact=True).wait_for(timeout=15000)
    assert client.calls == 4
    assert page.locator("#board").get_by_text("B", exact=True).count() == 0


def test_later_successful_poll_replaces_pending_candidate_before_apply(open_page):
    first = board_payload(titles={"step_one": "A"})
    middle = board_payload(titles={"step_one": "B"})
    latest = board_payload(titles={"step_one": "C"})
    client = SequenceClient([first, middle, latest])
    page = open_page(client, fake_clock=True)

    fire_poll(page)
    page.wait_for_selector("#refresh.pending", timeout=5000)
    # Let the second periodic response supersede B while A remains displayed.
    # The candidate changes no visible text, so watch the button for the
    # rewrite that shows the second response was handled.
    page.evaluate("""() => {
      window.refreshChangeRecords = 0;
      new MutationObserver((records) => {
        window.refreshChangeRecords += records.length;
      }).observe(document.querySelector('#refresh'), {
        childList: true, characterData: true, subtree: true, attributes: true,
      });
    }""")
    fire_poll(page)
    page.wait_for_function("() => window.refreshChangeRecords > 0", timeout=5000)
    assert page.locator("#refresh").inner_text() == "Apply update"
    page.click("#refresh")
    page.locator("#board").get_by_text("C", exact=True).wait_for(timeout=15000)
    assert client.calls == 3
    assert page.locator("#board").get_by_text("B", exact=True).count() == 0


def observe_removed_cards(page):
    page.evaluate("""() => {
      window.removedCards = 0;
      new MutationObserver(records => {
        for (const record of records) {
          for (const node of record.removedNodes) {
            if (node.nodeType === Node.ELEMENT_NODE &&
                (node.matches('.card') || node.querySelector('.card'))) window.removedCards++;
          }
        }
      }).observe(document.querySelector('#board'), {childList: true, subtree: true});
    }""")


def observe_refresh_status(page, selector="#refresh-status"):
    """Record each text change of one status region, ``#refresh-status`` by default."""
    assert page.locator(selector).count() == 1
    page.evaluate(f"""() => {{
      window.refreshStatusRecords = 0;
      window.refreshStatusTexts = [];
      const status = document.querySelector({selector!r});
      new MutationObserver(records => {{
        window.refreshStatusRecords += records.length;
        window.refreshStatusTexts.push(status.textContent);
      }}).observe(status, {{childList: true, characterData: true, subtree: true}});
    }}""")


def test_failed_poll_updates_issues_in_place_and_keeps_card_focus(open_page):
    page = open_page(
        SequenceClient([board_payload(), CatalogError("producer_unavailable")]), fake_clock=True
    )
    board = page.locator("#board")
    assert board.get_attribute("aria-live") is None
    assert board.get_attribute("role") is None
    status = page.locator("#refresh-status")
    assert status.count() == 1
    assert board.locator("#refresh-status").count() == 0
    assert status.get_attribute("role") == "status"
    assert status.get_attribute("aria-atomic") == "true"
    assert "visually-hidden" in status.get_attribute("class").split()
    box = status.bounding_box()
    assert 0 < box["width"] <= 1 and 0 < box["height"] <= 1

    card = page.locator(f'.card[data-package-id="{STEP_TWO}"]')
    card.focus()
    card.press("Enter")
    kept = card.element_handle()
    observe_removed_cards(page)
    fire_poll(page)
    playwright.expect(status).to_have_text("Latest refresh issue: producer_unavailable", timeout=5000)
    assert kept.evaluate("node => node.isConnected && node === document.activeElement")
    assert kept.get_attribute("aria-pressed") == "true"
    assert page.evaluate("window.removedCards") == 0
    panel = page.locator("#board-issues")
    assert panel.evaluate("node => node === node.parentElement.firstElementChild")
    assert panel.get_attribute("open") is None
    assert panel.locator("summary").inner_text() == "Latest refresh issue: producer_unavailable"
    assert_readable_arrows(page)


def test_issues_panel_keeps_open_state_through_failure_and_rebuild(open_page):
    value = json.loads(board_payload())
    value["discovery_diagnostics"] = [
        {"code": "discovery_unavailable", "message": "One folder could not be read"}
    ]
    _reseal(value)
    client = SequenceClient([value, CatalogError("producer_protocol_error"), value])
    page = open_page(client)
    panel = page.locator("#board-issues").element_handle()
    summary = page.locator("#board-issues summary")
    summary.focus()
    summary.press("Enter")
    kept_summary = summary.element_handle()
    observe_removed_cards(page)
    with page.expect_response("**/api/catalog"):
        page.locator("#refresh").click()
    playwright.expect(summary).to_have_text(
        "Workspace issues: discovery_unavailable (1) · Latest refresh issue: producer_protocol_error"
    )
    assert panel.evaluate("node => node.isConnected")
    assert kept_summary.evaluate("node => node.isConnected")
    assert panel.get_attribute("open") is not None
    assert page.evaluate("window.removedCards") == 0

    card = page.locator(f'.card[data-package-id="{STEP_TWO}"]').element_handle()
    with page.expect_response("**/api/catalog"):
        page.locator("#refresh").click()
    playwright.expect(summary).to_have_text("Workspace issues: discovery_unavailable (1)")
    assert not card.evaluate("node => node.isConnected")
    assert panel.evaluate("node => node.isConnected")
    assert kept_summary.evaluate("node => node.isConnected")
    assert panel.get_attribute("open") is not None
    assert page.locator("#refresh-status").inner_text() == "Refresh issue resolved."
    assert client.calls == 3


def test_poll_recovery_removes_issues_in_place_and_announces_resolution(open_page):
    page = open_page(
        SequenceClient([board_payload(), CatalogError("producer_timeout"), board_payload()]),
        fake_clock=True,
    )
    card = page.locator(f'.card[data-package-id="{STEP_TWO}"]')
    card.focus()
    card.press("Enter")
    kept = card.element_handle()
    observe_removed_cards(page)
    fire_poll(page)
    page.locator("#board").get_by_text("Latest refresh issue: producer_timeout", exact=True).wait_for(
        timeout=5000
    )
    fire_poll(page)
    playwright.expect(page.locator("#refresh-status")).to_have_text(
        "Refresh issue resolved.", timeout=5000
    )
    assert page.locator("#board-issues").count() == 0
    assert page.locator("#refresh").inner_text() == "Refresh view"
    assert kept.evaluate("node => node.isConnected && node === document.activeElement")
    assert kept.get_attribute("aria-pressed") == "true"
    assert page.evaluate("window.removedCards") == 0


def test_refresh_failure_changes_are_announced_once(open_page):
    client = SequenceClient([
        board_payload(), CatalogError("producer_unavailable"),
        CatalogError("producer_unavailable"), CatalogError("producer_timeout"),
    ])
    page = open_page(client)
    observe_refresh_status(page)
    counts = []
    for failure in ("producer_unavailable", "producer_unavailable", "producer_timeout"):
        with page.expect_response("**/api/catalog") as response:
            page.locator("#refresh").click()
        response.value.finished()
        playwright.expect(page.locator("#refresh-status")).to_have_text(
            f"Latest refresh issue: {failure}"
        )
        # Flush mutation delivery after the fetch continuation updates the DOM.
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        counts.append(page.evaluate("window.refreshStatusRecords"))
    assert counts[0] > 0
    assert counts[1] == counts[0]
    assert counts[2] > counts[1]
    assert client.calls == 4


def test_applying_pending_update_empties_refresh_status_silently(open_page):
    client = SequenceClient([
        board_payload(titles={"step_one": "A"}),
        board_payload(titles={"step_one": "B"}),
        CatalogError("producer_protocol_error"),
    ])
    page = open_page(client, fake_clock=True)
    fire_poll(page)
    page.wait_for_selector("#refresh.pending", timeout=5000)
    fire_poll(page)
    page.locator("#board").get_by_text("Latest refresh issue: producer_protocol_error", exact=True).wait_for(
        timeout=5000
    )
    # The failed poll does not erase the pending candidate B.
    assert page.locator("#board").get_by_text("A", exact=True).count() == 1
    assert page.locator("#refresh").inner_text() == "Apply update"
    assert "producer_protocol_error" in page.locator("#board-issues summary").inner_text()
    assert page.locator("#board-issues").get_attribute("open") is None
    status = page.locator("#refresh-status")
    assert status.inner_text() == "Latest refresh issue: producer_protocol_error"
    observe_refresh_status(page)
    page.get_by_role("button", name="Apply update", exact=True).click()
    assert page.locator("#board").get_by_text("B", exact=True).count() == 1
    assert page.locator("#board-issues").count() == 0
    assert status.inner_text() == ""
    assert "Refresh issue resolved." not in page.evaluate("window.refreshStatusTexts")
    cleared_records = page.evaluate("window.refreshStatusRecords")
    fire_poll(page)
    playwright.expect(status).to_have_text("Latest refresh issue: producer_protocol_error", timeout=5000)
    assert page.evaluate("window.refreshStatusRecords") > cleared_records
    assert "Refresh issue resolved." not in page.evaluate("window.refreshStatusTexts")


def test_catalog_diagnostics_remain_visible_through_selection_filter_and_empty_refresh(open_page):
    first = json.loads(board_payload())
    first["discovery_diagnostics"] = [
        {"code": "discovery_unavailable", "message": "first catalog discovery failure"}
    ]
    _reseal(first)
    second = json.loads(board_payload())
    second["entries"] = []
    second["discovery_diagnostics"] = [
        {"code": "discovery_unavailable", "message": "refreshed catalog discovery failure"}
    ]
    _reseal(second)
    page = open_page(SequenceClient([first, second]))

    board_issues = page.locator("#board-issues")
    assert "discovery_unavailable" in board_issues.locator("summary").inner_text()
    board_issues.locator("summary").click()
    assert board_issues.locator("li").inner_text() == (
        "discovery_unavailable first catalog discovery failure"
    )

    page.locator(f'.card[data-package-id="{GATE}"]').click()
    page.fill("#filter", "not a package")
    assert page.locator("#board .card:visible").count() == 0
    assert board_issues.locator("li").inner_text() == (
        "discovery_unavailable first catalog discovery failure"
    )
    page.fill("#filter", "")
    page.click("#refresh")
    page.wait_for_function(
        "() => document.querySelectorAll('#board .card').length === 0", timeout=15000
    )
    assert page.locator("#board-issues").get_attribute("open") is not None
    assert page.locator("#board-issues li").inner_text() == (
        "discovery_unavailable refreshed catalog discovery failure"
    )
    assert page.locator(".card.selected").count() == 0


def test_catalog_diagnostics_are_visible_when_discovery_returns_no_packages(open_page):
    value = json.loads(board_payload())
    value["entries"] = []
    discovery_message = '<img src=x onerror="alert(1)"> all work item discovery failed'
    value["discovery_diagnostics"] = [
        {"code": "discovery_unavailable", "message": discovery_message}
    ]
    _reseal(value)
    page = open_page(StaticClient(value))

    assert page.locator("#board .card").count() == 0
    assert "Empty folders are hidden" in page.locator("#board .board-empty").inner_text()
    page.locator("#board-issues summary").click()
    assert discovery_message in page.locator("#board-issues").inner_text()
    assert page.locator("#board-issues li").inner_text() == (
        f"discovery_unavailable {discovery_message}"
    )
    assert page.locator("#board-issues img").count() == 0


@pytest.mark.parametrize("participation,state,edge_states", [
    ("available", "no_declared_prerequisites", []),
    ("available", "satisfied", ["satisfied", "satisfied"]),
    ("available", "unsatisfied", ["satisfied", "unsatisfied"]),
    ("available", "unknown", ["satisfied", "unknown"]),
    ("available", "unknown", []),
    ("legacy", "relationship_unavailable", []),
    ("invalid", "relationship_unavailable", []),
    ("available", "satisfied", ["unknown"]),
    ("available", "no_declared_prerequisites", ["unknown"]),
])
def test_card_color_follows_incoming_arrows_regardless_of_prerequisite_state(
    open_page, participation, state, edge_states
):
    value = json.loads(board_payload())
    relationship = value["entries"][0]["relationship"]
    edges = []
    for index, edge_state in enumerate(edge_states):
        edge = _edge(STEP_TWO, f"claim-{index}")
        edge.update(
            observed_state=edge_state, resolved_state=edge_state,
            reason=f"claim_{edge_state}",
        )
        edges.append(edge)
    relationship.update(
        participation=participation, direct_prerequisite_state=state, prerequisites=edges
    )
    _reseal(value)
    page = open_page(StaticClient(value))
    card = page.locator(f'[data-package-id="{GATE}"]')
    expected_color = DARK_CARD if edge_states else NO_INCOMING_CARD
    assert card.evaluate("el => getComputedStyle(el).backgroundColor") == expected_color
    card.click()
    assert "selected" in card.get_attribute("class").split()
    assert card.evaluate("el => getComputedStyle(el).backgroundColor") == expected_color


def test_refresh_changing_only_incoming_edge_state_keeps_card_color(open_page):
    first = json.loads(board_payload())
    relationship = first["entries"][1]["relationship"]
    satisfied = _edge(STEP_TWO, "build")
    satisfied.update(
        observed_state="satisfied", resolved_state="satisfied", reason="claim_satisfied"
    )
    relationship.update(direct_prerequisite_state="satisfied", prerequisites=[satisfied])
    _reseal(first)
    second = json.loads(json.dumps(first))
    second_relationship = second["entries"][1]["relationship"]
    second_relationship["direct_prerequisite_state"] = "unknown"
    second_relationship["prerequisites"][0].update(
        observed_state="unknown", resolved_state="unknown", reason="claim_unknown"
    )
    _reseal(second)
    page = open_page(SequenceClient([first, second]), fake_clock=True)
    card = page.locator(f'[data-package-id="{STEP_ONE}"]')
    assert card.evaluate("el => getComputedStyle(el).backgroundColor") == DARK_CARD
    card.click()
    refresh = page.locator("#refresh")
    fire_poll(page)
    playwright.expect(refresh).to_have_text("Apply update", timeout=5000)
    refresh.click()
    playwright.expect(refresh).to_have_text("Refresh view")
    assert card.evaluate("el => getComputedStyle(el).backgroundColor") == DARK_CARD


def test_incoming_arrow_colors_individual_cards_and_updates_with_filter(open_page):
    page = open_page(StaticClient(json.loads(board_payload())))
    source = page.locator(f'.card[data-package-id="{STEP_ONE}"]')
    dependent = page.locator(f'.card[data-package-id="{STEP_TWO}"]')
    assert source.locator("xpath=..").get_attribute("class") == dependent.locator(
        "xpath=.."
    ).get_attribute("class")
    assert source.evaluate("el => getComputedStyle(el).backgroundColor") == NO_INCOMING_CARD
    assert dependent.evaluate("el => getComputedStyle(el).backgroundColor") == DARK_CARD

    page.fill("#filter", "Dependent step")
    assert connection_pairs(page) == set()
    assert dependent.evaluate("el => getComputedStyle(el).backgroundColor") == NO_INCOMING_CARD

    page.fill("#filter", "")
    assert (STEP_ONE, STEP_TWO) in connection_pairs(page)
    assert dependent.evaluate("el => getComputedStyle(el).backgroundColor") == DARK_CARD

    page.get_by_label("Theme", exact=True).select_option("light")
    assert source.evaluate("el => getComputedStyle(el).backgroundColor") == NO_INCOMING_CARD
    assert dependent.evaluate("el => getComputedStyle(el).backgroundColor") == LIGHT_CARD


def test_same_project_relationship_text_is_available_to_screen_readers(open_page):
    page = open_page(StaticClient(dependency_state_payload("unsatisfied")))
    expected = {
        STEP_TWO: ["needs: Foundation step", "blocks: Gate step"],
        STEP_ONE: ["blocks: Dependent step"],
        GATE: ["needs: Dependent step"],
        LOOSE: [],
    }
    visible = {STEP_ONE: 1, LOOSE: 1, STEP_TWO: 0, GATE: 0}
    for package_id, texts in expected.items():
        card = page.locator(f'.card[data-package-id="{package_id}"]')
        spans = card.locator(".card-rail-text")
        assert spans.all_text_contents() == texts
        assert card.locator(".card-links").count() == visible[package_id]
        for span in spans.all():
            assert span.evaluate("node => node.childNodes.length === 1 && node.firstChild.nodeType === Node.TEXT_NODE")
            box = span.bounding_box()
            assert 0 < box["width"] <= 1 and 0 < box["height"] <= 1
    assert page.locator(f'.card[data-package-id="{STEP_ONE}"] .card-links').inner_text() == (
        f"blocks: {XSS_TITLE} Beta"
    )
    assert page.locator(f'.card[data-package-id="{LOOSE}"] .card-links').inner_text() == (
        "needs: Foundation step Alpha"
    )
    dependent = page.locator(f'.card[data-package-id="{STEP_TWO}"]')
    playwright.expect(dependent).to_have_accessible_name(re.compile("needs: Foundation step"))
    playwright.expect(page.locator(f'.card[data-package-id="{LOOSE}"]')).not_to_have_accessible_name(
        re.compile("blocks:")
    )
    assert page.locator("#board").get_by_text("Foundation step", exact=True).count() == 1

    page.fill("#filter", "Dependent step")
    kept = dependent.element_handle()
    page.get_by_role("button", name="Refresh view", exact=True).click()
    page.wait_for_function("node => !node.isConnected", arg=kept)
    assert page.locator(f'.card[data-package-id="{STEP_ONE}"]').is_hidden()
    assert page.locator(".rail").count() == 0
    assert dependent.locator(".card-rail-text").all_text_contents() == expected[STEP_TWO]
    playwright.expect(dependent).to_have_accessible_name(re.compile("needs: Foundation step"))

    escaped = open_page(StaticClient(dependency_state_payload("unsatisfied", titles={"step_one": XSS_TITLE})))
    assert escaped.locator(f'.card[data-package-id="{STEP_TWO}"] .card-rail-text').all_text_contents() == [
        f"needs: {XSS_TITLE}", "blocks: Gate step",
    ]
    assert escaped.locator("#board img").count() == 0


@pytest.mark.parametrize("source_project", ["Alpha", "Beta"])
def test_relationship_text_keeps_valid_target_after_malformed_prerequisite(open_page, tmp_path, source_project):
    _write_package(
        tmp_path, source_project, "Queue", "source", STEP_ONE,
        f"# Foundation step\nPackage ID: {STEP_ONE}\n"
        "Claim: release | unsatisfied\nClaim: second | unsatisfied\n",
    )
    _write_package(
        tmp_path, "Alpha", "Queue", "dependent", STEP_TWO,
        f"# Dependent step\nPackage ID: {STEP_TWO}\n"
        f"Prerequisite: {STEP_ONE} | INVALID\nPrerequisite: {STEP_ONE} | release\n"
        f"Prerequisite: {STEP_ONE} | second\n",
    )
    value = json.loads(scan_catalog(tmp_path))
    dependent_entry = next(entry for entry in value["entries"] if entry["package_id"] == STEP_TWO)
    assert [edge["reason"] for edge in dependent_entry["relationship"]["prerequisites"]] == [
        "invalid_prerequisite", "claim_unsatisfied", "claim_unsatisfied",
    ]
    page = open_page(ScanningClient(tmp_path))
    assert connection_pairs(page) == {(STEP_ONE, STEP_TWO)}
    dependent = page.locator(f'.card[data-package-id="{STEP_TWO}"]')
    source = page.locator(f'.card[data-package-id="{STEP_ONE}"]')
    if source_project == "Alpha":
        assert dependent.locator(".card-rail-text").all_text_contents() == ["needs: Foundation step"]
        assert source.locator(".card-rail-text").all_text_contents() == ["blocks: Dependent step"]
        assert dependent.locator(".card-links").count() == 0
        assert source.locator(".card-links").count() == 0
    else:
        assert dependent.locator(".card-links").all_text_contents() == ["needs: Foundation step Beta"]
        assert source.locator(".card-links").all_text_contents() == ["blocks: Dependent step Alpha"]
    playwright.expect(dependent).to_have_accessible_name(re.compile("needs: Foundation step"))
    assert dependent.text_content().count("Foundation step") == 1
    assert source.text_content().count("Dependent step") == 1


@pytest.mark.parametrize("provider_project", ["Alpha", "Beta"])
def test_shared_identity_cards_keep_valid_relationship_text(open_page, tmp_path, provider_project):
    _write_package(
        tmp_path, provider_project, "Queue", "provider", STEP_ONE,
        f"# Provider\nPackage ID: {STEP_ONE}\nClaim: release | unsatisfied\n",
    )
    for name, title in (("first", "First twin"), ("second", "Second twin")):
        _write_package(
            tmp_path, "Alpha", "Queue", name, STEP_TWO,
            f"# {title}\nPackage ID: {STEP_TWO}\nPrerequisite: {STEP_ONE} | release\n",
        )
    value = json.loads(scan_catalog(tmp_path))
    twins = [entry for entry in value["entries"] if entry["package_id"] == STEP_TWO]
    assert len(twins) == 2
    for entry in twins:
        assert [(edge["target_package_id"], edge["reason"]) for edge in entry["relationship"]["prerequisites"]] == [
            (STEP_ONE, "claim_unsatisfied"),
        ]
    page = open_page(ScanningClient(tmp_path))
    assert connection_pairs(page) == set()
    cross = provider_project != "Alpha"
    needs = "needs: Provider Beta" if cross else "needs: Provider"
    provider = page.locator(f'.card[data-package-id="{STEP_ONE}"]')
    for title in ("First twin", "Second twin"):
        card = page.locator(".card").filter(has=page.locator(".card-title", has_text=title))
        assert card.count() == 1
        assert card.locator(".card-links").all_text_contents() == [needs]
        assert card.locator(".card-links em").count() == int(cross)
        assert "no-incoming-arrow" not in card.get_attribute("class").split()
        assert card.evaluate("node => getComputedStyle(node).backgroundColor") == DARK_CARD
        assert card.locator(".card-start-text").text_content() == ""
        playwright.expect(card).not_to_have_accessible_name(re.compile("No prerequisite shown"))
    suffix = " Alpha" if cross else ""
    assert provider.locator(".card-links").all_text_contents() == [
        f"blocks: First twin{suffix} · Second twin{suffix}"
    ]
    assert provider.locator(".card-start-text").text_content() == "No prerequisite shown."

    # A relationship counts only while its prerequisite card is visible.
    page.fill("#filter", "twin")
    for title in ("First twin", "Second twin"):
        card = page.locator(".card").filter(has=page.locator(".card-title", has_text=title))
        assert card.locator(".card-links").all_text_contents() == [needs]
        assert "no-incoming-arrow" in card.get_attribute("class").split()
        assert card.locator(".card-start-text").text_content() == "No prerequisite shown."
    page.fill("#filter", "")
    for title in ("First twin", "Second twin"):
        card = page.locator(".card").filter(has=page.locator(".card-title", has_text=title))
        assert card.locator(".card-start-text").text_content() == ""


def test_no_incoming_text_follows_search_like_the_color(open_page):
    page = open_page(StaticClient(dependency_state_payload("unsatisfied")))

    def assert_text_matches_color():
        for card in page.locator(".card").all():
            start = card.locator(".card-start-text")
            assert start.count() == 1
            expected = "No prerequisite shown." if "no-incoming-arrow" in card.get_attribute("class").split() else ""
            assert start.text_content() == expected

    source = page.locator(f'.card[data-package-id="{STEP_ONE}"] .card-start-text')
    dependent = page.locator(f'.card[data-package-id="{STEP_TWO}"] .card-start-text')
    assert_text_matches_color()
    assert source.text_content() == "No prerequisite shown."
    assert dependent.text_content() == ""
    page.fill("#filter", "Dependent step")
    assert_text_matches_color()
    assert dependent.text_content() == "No prerequisite shown."
    page.fill("#filter", "")
    assert_text_matches_color()
    assert dependent.text_content() == ""


def connection_pairs(page):
    return {
        tuple(pair) for pair in page.locator(".connection").evaluate_all(
            "groups => groups.map(g => [g.dataset.source, g.dataset.dependent])"
        )
    }


def rail_failure_context(result, predicate):
    diagnostic = dict(result["diagnostic"])
    diagnostic["predicate"] = predicate
    diagnostic["runner"] = {
        "githubActions": os.environ.get("GITHUB_ACTIONS"),
        "runnerName": os.environ.get("RUNNER_NAME"),
        "runnerOS": os.environ.get("RUNNER_OS"),
        "runnerArch": os.environ.get("RUNNER_ARCH"),
        "imageOS": os.environ.get("ImageOS"),
        "imageVersion": os.environ.get("ImageVersion"),
    }
    return f"rail assertion failed: {json.dumps(diagnostic, sort_keys=True)}"


def assert_readable_arrows(page):
    results = page.evaluate("""() => {
      const board = document.querySelector('#board');
      const svg = board.querySelector(':scope > .rail-layer');
      return [...svg.querySelectorAll('.rail')].map(path => {
      const boardViewport = board.getBoundingClientRect();
      const svgViewport = svg.getBoundingClientRect();
      const svgStyle = getComputedStyle(svg);
      const viewBox = svg.viewBox.baseVal;
      const at = length => path.getPointAtLength(length);
      const point = value => ({x: value.x, y: value.y});
      const rectangle = value => ({
        left: value.left, top: value.top, right: value.right, bottom: value.bottom,
        width: value.width, height: value.height,
      });
      const boardLocalRectangle = element => {
        const value = element.getBoundingClientRect();
        return {
          left: value.left - boardViewport.left,
          top: value.top - boardViewport.top,
          right: value.right - boardViewport.left,
          bottom: value.bottom - boardViewport.top,
          width: value.width,
          height: value.height,
        };
      };
      const toViewport = value => ({
        x: svgViewport.left + (value.x - viewBox.x) * svgViewport.width / viewBox.width,
        y: svgViewport.top + (value.y - viewBox.y) * svgViewport.height / viewBox.height,
      });
      const length = path.getTotalLength();
      const start = at(0), end = at(length), beforeEnd = at(length - 1);
      const cards = [...document.querySelectorAll('.card:not([hidden])')];
      const sourceElement = cards.find(
        c => c.dataset.packageId === path.parentNode.dataset.source
      );
      const dependentElement = cards.find(
        c => c.dataset.packageId === path.parentNode.dataset.dependent
      );
      const source = boardLocalRectangle(sourceElement);
      const dependent = boardLocalRectangle(dependentElement);
      const obstacles = [...cards, ...document.querySelectorAll('.row-head, .column-head')]
        .map(boardLocalRectangle);
      let intersects = false;
      for (let distance = 0; distance <= length; distance += 2) {
        const sample = at(distance);
        if (obstacles.some(r => sample.x > r.left && sample.x < r.right &&
            sample.y > r.top && sample.y < r.bottom)) intersects = true;
      }
      const markerId = path.getAttribute('marker-end').slice(5, -1);
      const marker = svg.querySelector(`marker[id="${markerId}"]`);
      const arrowShape = marker.querySelector('path').getAttribute('d');
      const arrowTipAtEnd = marker.refX.baseVal.value === marker.viewBox.baseVal.width;
      const arrowWidth = marker.markerWidth.baseVal.value;
      const strokeWidth = parseFloat(getComputedStyle(path).strokeWidth);
      const leavesSource = Math.abs(start.x - source.left) < 2 &&
        start.y > source.top && start.y < source.bottom;
      const entersDependent = end.x < dependent.left && dependent.left - end.x <= 8 &&
        end.y > dependent.top && end.y < dependent.bottom && beforeEnd.x < end.x;
      const mappedStart = toViewport(start);
      const mappedEnd = toViewport(end);
      const sourceViewport = sourceElement.getBoundingClientRect();
      const dependentViewport = dependentElement.getBoundingClientRect();
      const placement = {
        absoluteTopLeft: svgStyle.position === 'absolute' &&
          parseFloat(svgStyle.left) === 0 && parseFloat(svgStyle.top) === 0,
        rootAtBoardOrigin: svgViewport.left === boardViewport.left &&
          svgViewport.top === boardViewport.top,
        zeroOriginViewBox: viewBox.x === 0 && viewBox.y === 0,
        viewBoxMatchesLayerDimensions: viewBox.width === svgViewport.width &&
          viewBox.height === svgViewport.height,
        endpointsMeetCards: Math.abs(mappedStart.x - sourceViewport.left) < 2 &&
          mappedStart.y > sourceViewport.top && mappedStart.y < sourceViewport.bottom &&
          mappedEnd.x < dependentViewport.left &&
          dependentViewport.left - mappedEnd.x <= 8 &&
          mappedEnd.y > dependentViewport.top && mappedEnd.y < dependentViewport.bottom,
      };
      return {
        leavesSource,
        entersDependent,
        intersects,
        arrowShape,
        arrowTipAtEnd,
        arrowWidth,
        strokeWidth,
        layerPlacement: Object.values(placement).every(Boolean),
        diagnostic: {
          edge: {
            source: path.parentNode.dataset.source,
            dependent: path.parentNode.dataset.dependent,
          },
          path: {
            d: path.getAttribute('d'), length,
            start: point(start), beforeEnd: point(beforeEnd), end: point(end),
            mappedStart: point(mappedStart), mappedEnd: point(mappedEnd),
          },
          predicates: {
            route: {
              leavesSource,
              entersDependent,
              avoidsObstacles: !intersects,
              arrowShape: arrowShape === 'M0 0L10 5L0 10Z',
              arrowTipAtEnd,
              arrowWidth: arrowWidth >= 10,
              strokeWidth: strokeWidth >= 2.5,
            },
            placement,
          },
          rectangles: {
            boardViewport: rectangle(boardViewport),
            svgViewport: rectangle(svgViewport),
            sourceBoardLocal: source,
            dependentBoardLocal: dependent,
            sourceViewport: rectangle(sourceViewport),
            dependentViewport: rectangle(dependentViewport),
          },
          layer: {
            computedPosition: svgStyle.position,
            computedLeft: svgStyle.left,
            computedTop: svgStyle.top,
            computedWidth: svgStyle.width,
            computedHeight: svgStyle.height,
            viewBox: {
              x: viewBox.x, y: viewBox.y, width: viewBox.width, height: viewBox.height,
            },
          },
          browser: {userAgent: navigator.userAgent, platform: navigator.platform},
          viewport: {
            width: window.innerWidth, height: window.innerHeight,
            devicePixelRatio: window.devicePixelRatio,
          },
          evaluation: {
            performanceNow: performance.now(), readyState: document.readyState,
            visibilityState: document.visibilityState,
            fontsStatus: document.fonts ? document.fonts.status : 'unsupported',
          },
        },
      };
      });
    }""")
    for result in results:
        assert result["leavesSource"], rail_failure_context(result, "leavesSource")
        assert result["entersDependent"], rail_failure_context(result, "entersDependent")
        assert not result["intersects"], rail_failure_context(result, "avoidsObstacles")
        assert result["arrowShape"] == "M0 0L10 5L0 10Z", rail_failure_context(
            result, "arrowShape"
        )
        assert result["arrowTipAtEnd"], rail_failure_context(result, "arrowTipAtEnd")
        assert result["arrowWidth"] >= 10, rail_failure_context(result, "arrowWidth")
        assert result["strokeWidth"] >= 2.5, rail_failure_context(result, "strokeWidth")
    for result in results:
        assert result["layerPlacement"], rail_failure_context(result, "layerPlacement")


def test_readable_arrow_failures_report_context_only_on_failure(
    open_page, capsys, monkeypatch
):
    runner_context = {
        "GITHUB_ACTIONS": "true",
        "RUNNER_NAME": "diagnostic-runner",
        "RUNNER_OS": "DiagnosticOS",
        "RUNNER_ARCH": "diagnostic-arch",
        "ImageOS": "diagnostic-image",
        "ImageVersion": "diagnostic-version",
    }
    for name, value in runner_context.items():
        monkeypatch.setenv(name, value)
    page = open_page(StaticClient(board_payload()))
    assert_readable_arrows(page)
    passing_output = capsys.readouterr()
    assert passing_output.out == ""
    assert passing_output.err == ""

    first_rail = page.locator(".rail").first
    valid_path = first_rail.get_attribute("d")
    first_rail.evaluate("path => path.setAttribute('d', 'M 0 0 L 1 1')")
    with pytest.raises(AssertionError) as failure:
        assert_readable_arrows(page)
    message = str(failure.value)
    prefix = "rail assertion failed: "
    assert message.startswith(prefix)
    diagnostic = json.loads(message[len(prefix):].splitlines()[0])

    assert diagnostic["predicate"] == "leavesSource"
    assert diagnostic["edge"] == {"source": STEP_ONE, "dependent": STEP_TWO}
    assert diagnostic["path"]["d"] == "M 0 0 L 1 1"
    assert diagnostic["path"]["length"] == pytest.approx(2 ** 0.5)
    for endpoint in ("start", "beforeEnd", "end"):
        point = diagnostic["path"][endpoint]
        assert isinstance(point["x"], (int, float))
        assert isinstance(point["y"], (int, float))
    assert diagnostic["path"]["start"] != diagnostic["path"]["end"]

    for rectangle in diagnostic["rectangles"].values():
        assert rectangle["width"] > 0
        assert rectangle["height"] > 0
        assert rectangle["right"] > rectangle["left"]
        assert rectangle["bottom"] > rectangle["top"]
    assert diagnostic["browser"]["userAgent"]
    assert diagnostic["browser"]["platform"]
    assert diagnostic["runner"] == {
        "githubActions": "true",
        "runnerName": "diagnostic-runner",
        "runnerOS": "DiagnosticOS",
        "runnerArch": "diagnostic-arch",
        "imageOS": "diagnostic-image",
        "imageVersion": "diagnostic-version",
    }
    assert diagnostic["viewport"]["width"] > 0
    assert diagnostic["viewport"]["height"] > 0
    assert diagnostic["viewport"]["devicePixelRatio"] > 0
    assert diagnostic["evaluation"]["performanceNow"] >= 0
    assert diagnostic["evaluation"]["readyState"] == "complete"
    assert diagnostic["evaluation"]["visibilityState"] == "visible"
    assert diagnostic["evaluation"]["fontsStatus"] in {"loaded", "loading"}

    first_rail.evaluate("(path, value) => path.setAttribute('d', value)", valid_path)
    assert_readable_arrows(page)
    page.locator(".rail-layer").evaluate("svg => { svg.style.left = '32px'; }")
    with pytest.raises(AssertionError) as shifted_failure:
        assert_readable_arrows(page)
    shifted_message = str(shifted_failure.value)
    assert shifted_message.startswith(prefix)
    shifted = json.loads(shifted_message[len(prefix):].splitlines()[0])

    assert shifted["predicate"] == "layerPlacement"
    assert all(shifted["predicates"]["route"].values())
    assert not shifted["predicates"]["placement"]["absoluteTopLeft"]
    assert not shifted["predicates"]["placement"]["rootAtBoardOrigin"]
    assert not shifted["predicates"]["placement"]["endpointsMeetCards"]
    assert shifted["predicates"]["placement"]["zeroOriginViewBox"]
    assert shifted["predicates"]["placement"]["viewBoxMatchesLayerDimensions"]


def test_selection_emphasizes_incoming_and_outgoing_arrows_and_restores_them(open_page):
    page = open_page(StaticClient(board_payload()))
    selected = page.locator(f'.card[data-package-id="{STEP_TWO}"]')
    selected.focus()
    page.keyboard.press("Enter")
    assert set(map(tuple, page.locator(".connection.emphasized").evaluate_all(
        "groups => groups.map(g => [g.dataset.source, g.dataset.dependent])"
    ))) == {(STEP_ONE, STEP_TWO), (STEP_TWO, GATE)}
    assert page.locator(".connection.muted").evaluate_all(
        "groups => groups.map(g => Number(getComputedStyle(g).opacity))"
    ) == [0.25]
    assert page.locator(".connection.emphasized .rail").evaluate_all(
        "paths => paths.map(p => getComputedStyle(p).stroke)"
    ) == ["rgb(118, 183, 255)"] * 2
    assert_readable_arrows(page)
    # Selecting a source emphasizes its cross-project arrow as well.
    page.locator(f'.card[data-package-id="{STEP_ONE}"]').click()
    cross = page.locator(f'.connection[data-source="{STEP_ONE}"][data-dependent="{LOOSE}"]')
    assert "emphasized" in cross.get_attribute("class").split()
    # Selection changes the border only; the source keeps its no-incoming color.
    assert page.locator(f'.card[data-package-id="{STEP_ONE}"]').evaluate(
        "c => getComputedStyle(c).backgroundColor"
    ) == NO_INCOMING_CARD
    page.locator(f'.card[data-package-id="{STEP_ONE}"]').click()
    assert page.locator(".connection.emphasized, .connection.muted").count() == 0
    assert connection_pairs(page) == {(STEP_ONE, STEP_TWO), (STEP_TWO, GATE), (STEP_ONE, LOOSE)}


def routing_payload():
    value = json.loads(board_payload())
    value["terminal_stages"] = ["Done"]
    far = "123e4567-e89b-42d3-a456-426614174004"
    unknown = "123e4567-e89b-42d3-a456-426614174005"
    value["entries"].extend([
        _entry(far, "Gamma/Done/far", "done", "Far prerequisite", "Gamma",
               prerequisites=[_edge(STEP_ONE, "forward")]),
        _entry(unknown, "Other/Queue/unknown", "queue", "Unknown project", "Other",
               prerequisites=[_edge(far, "input")]),
    ])
    _admit_inventory_stage(value, "Gamma", "Done")
    _admit_inventory_stage(value, "Other", "Queue")
    # Reverse cross-project edge and a cycle; neither implies a topological order.
    value["entries"][1]["relationship"]["prerequisites"] = [_edge(far, "reverse")]
    value["entries"][1]["relationship"]["direct_prerequisite_state"] = "unsatisfied"
    value["entries"].sort(key=lambda entry: entry["package_path"])
    _reseal(value)
    return value, far, unknown


def assert_rail_lanes_do_not_overlap(page):
    segments = page.locator(".rail").evaluate_all(r"""paths => paths.flatMap(path => {
      const tokens = path.getAttribute('d').match(/[MHV]|-?\d+(?:\.\d+)?(?:e[+-]?\d+)?/gi);
      const result = [];
      let x = 0, y = 0;
      for (let i = 0; i < tokens.length;) {
        const command = tokens[i++];
        if (command === 'M') { x = Number(tokens[i++]); y = Number(tokens[i++]); }
        else if (command === 'H') x = Number(tokens[i++]);
        else if (command === 'V') {
          const next = Number(tokens[i++]);
          result.push({source: path.parentNode.dataset.source,
            dependent: path.parentNode.dataset.dependent, x,
            start: Math.min(y, next), end: Math.max(y, next)});
          y = next;
        } else throw new Error(`Unexpected rail path command: ${command}`);
      }
      return result;
    })""")
    assert len(segments) >= page.locator(".rail").count()
    for index, first in enumerate(segments):
        for second in segments[index + 1:]:
            if (first["source"], first["dependent"]) == (second["source"], second["dependent"]):
                continue
            if abs(first["x"] - second["x"]) <= 0.5:
                assert max(first["start"], second["start"]) > min(first["end"], second["end"]), (first, second)
    return segments


def test_chained_rails_reuse_two_lanes(open_page):
    ids = [f"123e4567-e89b-42d3-a456-{index:012d}" for index in range(100, 125)]
    value = json.loads(board_payload())
    value["entries"] = [
        _entry(package_id, f"Alpha/Queue/item-{index:02d}", "queue",
               f"{'Keep' if index < 13 else 'Other'} item {index:02d}", "Alpha",
               [_edge(ids[index - 1], "input")] if index else [])
        for index, package_id in enumerate(ids)
    ]
    _reseal(value)
    page = open_page(StaticClient(value))
    assert page.locator(".cell").evaluate_all("cells => cells.map(cell => getComputedStyle(cell).paddingLeft)") == ["48px"]
    assert connection_pairs(page) == set(zip(ids, ids[1:]))
    assert_readable_arrows(page)
    before = assert_rail_lanes_do_not_overlap(page)
    lanes = {(segment["source"], segment["dependent"]): segment["x"] for segment in before}
    page.fill("#filter", "Keep")
    assert connection_pairs(page) == set(zip(ids[:12], ids[1:13]))
    assert page.locator(".cell").evaluate_all("cells => cells.map(cell => getComputedStyle(cell).paddingLeft)") == ["48px"]
    for segment in assert_rail_lanes_do_not_overlap(page):
        assert segment["x"] == pytest.approx(lanes[(segment["source"], segment["dependent"])])
    assert_readable_arrows(page)


def test_rail_lanes_are_capped_and_overflow_is_written_on_cards(open_page):
    ids = [f"123e4567-e89b-42d3-a456-{index:012d}" for index in range(100, 113)]
    value = json.loads(board_payload())
    value["entries"] = [_entry(ids[0], "Alpha/Queue/00-hub", "queue", "Hub", "Alpha")] + [
        _entry(ids[index], f"Alpha/Queue/{index:02d}-dependent", "queue", f"Dependent {index:02d}", "Alpha", [_edge(ids[0], "input")])
        for index in range(1, 13)
    ]
    _reseal(value)
    page = open_page(StaticClient(value))
    assert page.locator(".cell").evaluate_all("cells => cells.map(cell => getComputedStyle(cell).paddingLeft)") == ["132px"]
    assert connection_pairs(page) == {(ids[0], dependent) for dependent in ids[1:9]}
    for index, package_id in enumerate(ids[1:], start=1):
        card = page.locator(f'.card[data-package-id="{package_id}"]')
        if index <= 8:
            assert card.locator(".card-rail-text").all_text_contents() == ["needs: Hub"]
        else:
            assert card.locator(".card-links").all_text_contents() == ["needs: Hub"]
            assert card.locator(".card-links em").count() == 0
            assert "no-incoming-arrow" not in card.get_attribute("class").split()
            assert card.evaluate("node => getComputedStyle(node).backgroundColor") == DARK_CARD
            assert card.locator(".card-start-text").text_content() == ""
    hub = page.locator(f'.card[data-package-id="{ids[0]}"]')
    assert hub.locator(".card-links").all_text_contents() == [
        "blocks: " + " · ".join(f"Dependent {index:02d}" for index in range(9, 13))
    ]
    assert hub.locator(".card-rail-text").all_text_contents() == [
        "blocks: " + " · ".join(f"Dependent {index:02d}" for index in range(1, 9))
    ]
    assert_readable_arrows(page)
    assert_rail_lanes_do_not_overlap(page)


def test_upward_edge_rails_use_separate_lanes(open_page):
    value = json.loads(board_payload())
    value["entries"] = [
        _entry(STEP_ONE, "Alpha/Queue/q1", "queue", "Q1", "Alpha", [_edge(GATE, "input")]),
        _entry(STEP_TWO, "Alpha/Queue/q2", "queue", "Q2", "Alpha", [_edge(STEP_ONE, "input")]),
        _entry(GATE, "Alpha/Under_Development/u", "under_development", "U", "Alpha"),
    ]
    _reseal(value)
    page = open_page(StaticClient(value))
    assert row_labels(page) == ["Queue", "Under Development"]
    assert page.locator(".cell").evaluate_all("cells => cells.map(cell => getComputedStyle(cell).paddingLeft)") == ["48px", "48px"]
    assert connection_pairs(page) == {(STEP_ONE, STEP_TWO), (GATE, STEP_ONE)}
    assert_readable_arrows(page)
    assert_rail_lanes_do_not_overlap(page)


def test_cross_row_rail_uses_the_source_row_band_slot(open_page):
    value = json.loads(board_payload())
    value["entries"] = [
        _entry(STEP_ONE, "Alpha/Queue/a1", "queue", "A1", "Alpha"),
        _entry(STEP_TWO, "Alpha/Queue/a2", "queue", "A2", "Alpha", [_edge(STEP_ONE, "input")]),
        _entry(GATE, "Alpha/Under_Development/x", "under_development", "X", "Alpha", [_edge(LOOSE, "input")]),
        _entry(LOOSE, "Beta/Queue/s", "queue", "S", "Beta"),
    ]
    _admit_inventory_stage(value, "Beta", "Queue")
    _reseal(value)
    page = open_page(StaticClient(value))
    assert row_labels(page) == ["Queue", "Under Development"]
    assert page.locator('.board-row .cell:nth-of-type(1)').evaluate_all(
        "cells => cells.map(cell => getComputedStyle(cell).paddingLeft)"
    ) == ["48px", "48px"]
    assert connection_pairs(page) == {(STEP_ONE, STEP_TWO), (LOOSE, GATE)}
    assert page.locator('.board-row[data-lifecycle="Queue"] .connection-band').count() == 1
    assert_readable_arrows(page)
    assert_rail_lanes_do_not_overlap(page)


def test_cross_project_rail_needs_a_lane_in_both_columns(open_page):
    ids = [f"123e4567-e89b-42d3-a456-{index:012d}" for index in range(100, 109)]
    value = json.loads(board_payload())
    value["entries"] = [_entry(ids[0], "Alpha/Queue/hub", "queue", "Hub", "Alpha")] + [
        _entry(ids[index], f"Alpha/Under_Development/{index:02d}-dependent", "under_development",
               f"Dependent {index:02d}", "Alpha", [_edge(ids[0], "input")])
        for index in range(1, 9)
    ] + [
        _entry(STEP_ONE, "Alpha/Under_Development/x", "under_development", "X", "Alpha", [_edge(STEP_TWO, "input")]),
        _entry(STEP_TWO, "Beta/Under_Development/s", "under_development", "S", "Beta"),
    ]
    _reseal(value)
    page = open_page(StaticClient(value))
    assert page.locator('.board-row .cell:nth-of-type(1)').evaluate_all(
        "cells => cells.map(cell => getComputedStyle(cell).paddingLeft)"
    ) == ["132px", "132px"]
    assert connection_pairs(page) == {(ids[0], dependent) for dependent in ids[1:]}
    card = page.locator(f'.card[data-package-id="{STEP_ONE}"]')
    assert card.locator(".card-links").all_text_contents() == ["needs: S Beta"]
    assert card.locator(".card-links em").inner_text() == "Beta"
    assert "no-incoming-arrow" not in card.get_attribute("class").split()
    assert card.locator(".card-start-text").text_content() == ""
    assert page.locator('.board-row[data-lifecycle="Under_Development"] .connection-band').count() == 0
    assert_readable_arrows(page)
    assert_rail_lanes_do_not_overlap(page)


def test_cross_project_routes_avoid_cards_in_both_directions_after_resize_and_filter(open_page):
    value, far, unknown = routing_payload()
    page = open_page(StaticClient(value))
    expected = {
        (STEP_ONE, STEP_TWO), (STEP_TWO, GATE), (STEP_ONE, LOOSE),
    }
    assert connection_pairs(page) == expected
    assert page.locator(f'.card[data-package-id="{far}"]').count() == 0
    assert page.locator(f'.card[data-package-id="{unknown}"]').count() == 1
    assert page.locator(f'.card[data-package-id="{unknown}"] .card-links').count() == 0
    assert all(far not in pair for pair in connection_pairs(page))
    assert_readable_arrows(page)
    for width in (650, 1800):
        before = page.locator(".rail").first.get_attribute("d")
        page.set_viewport_size({"width": width, "height": 900})
        page.wait_for_function(
            "before => document.querySelector('.rail').getAttribute('d') !== before", arg=before
        )
        assert connection_pairs(page) == expected
        assert all(far not in pair for pair in connection_pairs(page))
        assert_readable_arrows(page)
    page.fill("#filter", "Alpha")
    assert connection_pairs(page) == {(STEP_ONE, STEP_TWO), (STEP_TWO, GATE)}
    assert_readable_arrows(page)
    page.fill("#filter", "")
    assert connection_pairs(page) == expected
    assert all(far not in pair for pair in connection_pairs(page))
    assert_readable_arrows(page)
    assert_rail_lanes_do_not_overlap(page)


@pytest.mark.parametrize("reason,duplicate_record", [
    ("missing_target", False), ("duplicate_target", True), ("duplicate_target", False),
])
def test_unresolved_targets_are_not_connected_to_arbitrary_cards(
    open_page, reason, duplicate_record
):
    value = json.loads(board_payload())
    edge = value["entries"][3]["relationship"]["prerequisites"][0]
    edge.update(resolved_state="unknown", reason=reason)
    if duplicate_record:
        value["entries"].append(_entry(
            STEP_ONE, "Gamma/Queue/duplicate", "queue", "Duplicate source", "Gamma"
        ))
        _admit_inventory_stage(value, "Gamma", "Queue")
    elif reason == "missing_target":
        edge["target_package_id"] = "123e4567-e89b-42d3-a456-426614174009"
    value["entries"].sort(key=lambda entry: entry["package_path"])
    _reseal(value)
    page = open_page(StaticClient(value))
    assert (STEP_ONE, LOOSE) not in connection_pairs(page)
    if duplicate_record:
        assert connection_pairs(page) == {(STEP_TWO, GATE)}
    else:
        assert connection_pairs(page) == {(STEP_ONE, STEP_TWO), (STEP_TWO, GATE)}
    assert "unresolved target" in page.locator(f'.card[data-package-id="{LOOSE}"]').inner_text()
    assert page.locator(f'.card[data-package-id="{STEP_ONE}"] .card-links').count() == 0


def test_multiple_claims_share_one_arrow_and_one_cross_project_name(open_page):
    value = json.loads(board_payload())
    value["entries"][3]["relationship"]["prerequisites"].append(_edge(STEP_ONE, "testing"))
    _reseal(value)
    page = open_page(StaticClient(value))
    assert connection_pairs(page) == {(STEP_ONE, STEP_TWO), (STEP_TWO, GATE), (STEP_ONE, LOOSE)}
    assert page.locator(".rail").count() == 3
    source = page.locator(f'.card[data-package-id="{STEP_ONE}"]')
    assert source.locator(".card-links .link.cross").count() == 1
    assert_readable_arrows(page)


def test_refresh_rebuilds_connections_and_keeps_selection_emphasis(open_page):
    first = json.loads(board_payload())
    second = json.loads(board_payload())
    second["entries"][3]["relationship"]["prerequisites"] = [_edge(STEP_TWO, "shared")]
    _reseal(second)
    page = open_page(SequenceClient([first, second]))
    page.locator(f'.card[data-package-id="{STEP_TWO}"]').click()
    page.click("#refresh")
    playwright.expect(page.locator(
        f'.connection[data-source="{STEP_TWO}"][data-dependent="{LOOSE}"]'
    )).to_have_count(1)
    assert connection_pairs(page) == {(STEP_ONE, STEP_TWO), (STEP_TWO, GATE), (STEP_TWO, LOOSE)}
    assert page.locator(".connection.emphasized").count() == 3
    assert page.locator(".connection.muted").count() == 0
    assert_readable_arrows(page)


def contrast_ratio(first, second):
    def luminance(color):
        channels = [int(value) / 255 for value in re.findall(r"\d+", color)[:3]]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


@pytest.mark.parametrize("theme,page_color,card_color", [
    ("dark", "rgb(17, 24, 33)", DARK_CARD),
    ("light", "rgb(244, 246, 248)", LIGHT_CARD),
])
def test_native_themes_preserve_readable_cards_arrows_and_selection(
    open_page, theme, page_color, card_color
):
    page = open_page(StaticClient(board_payload()))
    page.get_by_label("Theme", exact=True).select_option(theme)
    source = page.locator(f'.card[data-package-id="{STEP_ONE}"]')
    neutral = page.locator(f'.card[data-package-id="{STEP_TWO}"]')
    assert source.evaluate("c => getComputedStyle(c).backgroundColor") == NO_INCOMING_CARD
    assert neutral.evaluate("c => getComputedStyle(c).backgroundColor") == card_color
    assert page.locator("html").evaluate("el => getComputedStyle(el).backgroundColor") == page_color
    assert page.locator("html").evaluate("el => getComputedStyle(el).colorScheme") == theme
    assert page.locator(".rail-halo").evaluate_all(
        "paths => paths.map(p => getComputedStyle(p).stroke)"
    ) == [page_color] * 3
    for color in page.locator(".rail").evaluate_all(
        "paths => paths.map(p => getComputedStyle(p).stroke)"
    ):
        assert contrast_ratio(color, page_color) >= 3
    assert_readable_arrows(page)
    source.click()
    assert page.locator(".connection.emphasized").count() == 2
    assert page.locator(".connection.muted").count() == 1
    for color in page.locator(".connection.emphasized .rail").evaluate_all(
        "paths => paths.map(p => getComputedStyle(p).stroke)"
    ):
        assert contrast_ratio(color, page_color) >= 3
    text_colors = page.evaluate("""() => {
      const color = el => getComputedStyle(el).color;
      const background = el => getComputedStyle(el).backgroundColor;
      const root = background(document.documentElement);
      const pairs = [...document.querySelectorAll(
        '.row-head, .column-head, .empty')].map(el => [color(el), root]);
      document.querySelectorAll('.card').forEach(card => {
        card.querySelectorAll('.card-title, .card-project, .card-filepath, .link, .link em')
          .forEach(el => pairs.push([color(el), background(card)]));
      });
      document.querySelectorAll('input, select, #refresh').forEach(el =>
        pairs.push([color(el), background(el)]));
      const input = document.querySelector('input');
      pairs.push([getComputedStyle(input, '::placeholder').color, background(input)]);
      return pairs;
    }""")
    for foreground, background in text_colors:
        assert contrast_ratio(foreground, background) >= 4.5, (foreground, background)
    divider = page.locator(".board-row").nth(1).evaluate(
        "el => getComputedStyle(el, '::before').borderTopColor"
    )
    assert contrast_ratio(divider, page_color) >= 3


def assert_focus_indicator(control, *, expected=None, card=False):
    control.focus()
    measurement = control.evaluate("""node => {
      const style = getComputedStyle(node);
      let background = getComputedStyle(document.documentElement).backgroundColor;
      for (let ancestor = node.parentElement; ancestor; ancestor = ancestor.parentElement) {
        const color = getComputedStyle(ancestor).backgroundColor;
        const channels = color.match(/[\\d.]+/g).map(Number);
        if (channels.length === 3 || channels[3] === 1) {
          background = color;
          break;
        }
      }
      let opacity = 1;
      for (let ancestor = node; ancestor; ancestor = ancestor.parentElement) {
        opacity *= Number(getComputedStyle(ancestor).opacity);
      }
      const foreground = style.outlineColor.match(/[\\d.]+/g).map(Number);
      const surface = background.match(/[\\d.]+/g).map(Number);
      const alpha = opacity * (foreground[3] ?? 1);
      const painted = foreground.slice(0, 3).map((channel, index) =>
        Math.round(channel * alpha + surface[index] * (1 - alpha)));
      return {painted: `rgb(${painted.join(', ')})`, opacity, color: style.outlineColor, style: style.outlineStyle, background,
        focus: node.matches(':focus'), visible: node.matches(':focus-visible')};
    }""")
    assert measurement["focus"] if card else measurement["visible"]
    # An authored solid ring keeps the measured color; the browser default
    # "auto" ring varies by browser build.
    assert measurement["style"] == "solid", measurement
    assert contrast_ratio(measurement["painted"], measurement["background"]) >= 3, measurement
    if expected is not None:
        assert measurement["color"] == expected

    return measurement


@pytest.mark.parametrize("theme,card_outline,selected_ring", [
    ("dark", "rgb(72, 137, 206)", "rgb(72, 137, 206)"),
    ("light", "rgb(36, 99, 165)", "rgb(141, 185, 231)"),
])
def test_focus_indicators_contrast_with_their_surface(open_page, theme, card_outline, selected_ring):
    value = json.loads(board_payload())
    value["discovery_diagnostics"] = [
        {"code": "discovery_unavailable", "message": "One folder could not be read"}
    ]
    _reseal(value)
    page = open_page(StaticClient(value), settings=BrowserSettings())
    page.get_by_label("Theme", exact=True).select_option(theme)
    page.keyboard.press("Tab")
    card = page.locator(f'.card[data-package-id="{STEP_TWO}"]')
    card.press("Enter")
    assert selected_ring in card.evaluate("node => getComputedStyle(node).boxShadow")

    assert_focus_indicator(card, expected=card_outline, card=True)
    for selector in ("#filter", "#refresh", "#theme", "#compact-view", "#hide-terminal-rows"):
        assert_focus_indicator(page.locator(selector), expected="rgb(255, 255, 255)")
    page.locator("#stage-order-heading").press("Enter")
    page.get_by_role("button", name="Move Under Development up", exact=True).press("Enter")
    for selector in (
        "#stage-order-heading", "#deselect-on-empty-click", ".stage-completed-toggle",
        ".stage-order-move:not(:disabled)", "#stage-order-save", "#stage-order-cancel",
        "#stage-order-reset", "#board-issues summary",
    ):
        expected = card_outline if selector.endswith(("heading", "summary")) else None
        assert_focus_indicator(page.locator(selector).first, expected=expected)

    failed = BrowserSettings()
    failed.fail_next_load = True
    retry_page = open_page(StaticClient(board_payload()), settings=failed)
    retry_page.get_by_label("Theme", exact=True).select_option(theme)
    retry_page.locator("#stage-order-heading").press("Enter")
    assert_focus_indicator(retry_page.locator("#stage-order-reload"))


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("request_kind", ["save", "reload"])
def test_stage_editor_busy_focus_indicators_keep_composited_contrast(open_page, theme, request_kind):
    settings = BrowserSettings()
    settings.fail_next_load = request_kind == "reload"
    page = open_page(StaticClient(board_payload()), settings=settings)
    page.get_by_label("Theme", exact=True).select_option(theme)
    page.locator("#stage-order-heading").press("Enter")
    if request_kind == "save":
        page.get_by_role("button", name="Move Under Development up", exact=True).press("Enter")
        selectors = [
            "#stage-order-save", "#stage-order-cancel", "#stage-order-reset",
            '.stage-completed-toggle[data-stage="Under_Development"]',
            '.stage-completed-toggle[data-stage="Queue"]',
            '.stage-order-item[data-stage="Under_Development"] [data-stage-move="down"]',
            '.stage-order-item[data-stage="Queue"] [data-stage-move="up"]',
        ]
    else:
        selectors = ["#stage-order-reload"]
    used = page.locator(selectors[0])
    used.focus()
    arm_focus_sampler(page)
    settings.gate = threading.Event()
    try:
        used.press("Enter")
        assert settings.entered.wait(timeout=2)
        playwright.expect(used).to_be_focused()
        busy = page.locator('#stage-order-editor [aria-disabled="true"]:visible')
        assert busy.count() == len(selectors)
        for selector in selectors:
            control = page.locator(selector)
            assert control.get_attribute("aria-disabled") == "true"
            assert control.get_attribute("disabled") is None
            measurement = assert_focus_indicator(control)
            assert 0 < measurement["opacity"] < 1, measurement
        assert settings.calls == (
            [("revision-1", ["Under_Development", "Queue"], [])]
            if request_kind == "save" else []
        )
        assert settings.get_calls == (1 if request_kind == "save" else 2)
        used.focus()
        assert_focus_sampler_clean(page)
    finally:
        settings.gate.set()
    playwright.expect(page.locator("#stage-order-reset")).to_be_focused()
    if request_kind == "save":
        playwright.expect(page.locator(".row-head")).to_have_text(["Under Development", "Queue"])
    else:
        playwright.expect(page.locator("#stage-order-status")).to_have_text("Current board settings loaded.")
    assert_focus_sampler_clean(page)


def test_dark_is_default_and_explicit_theme_survives_reload_and_system_changes(open_page):
    page = open_page(StaticClient(board_payload()), color_scheme="light")
    selector = page.get_by_label("Theme", exact=True)
    assert selector.input_value() == "dark"
    assert page.locator("html").get_attribute("data-theme") == "dark"
    source = page.locator(f'.card[data-package-id="{STEP_ONE}"]')
    source.click()
    page.fill("#filter", "Alpha")
    selector.select_option("light")
    assert page.locator("html").get_attribute("data-theme") == "light"
    assert source.get_attribute("aria-pressed") == "true"
    assert page.locator("#filter").input_value() == "Alpha"
    assert connection_pairs(page) == {(STEP_ONE, STEP_TWO), (STEP_TWO, GATE)}
    page.emulate_media(color_scheme="dark")
    assert page.locator("html").get_attribute("data-theme") == "light"
    page.reload()
    playwright.expect(selector).to_have_value("light")
    assert page.locator("html").get_attribute("data-theme") == "light"
    selector.select_option("dark")
    page.emulate_media(color_scheme="light")
    assert page.locator("html").get_attribute("data-theme") == "dark"
    page.reload()
    playwright.expect(selector).to_have_value("dark")
    assert page.locator("html").get_attribute("data-theme") == "dark"


def test_established_theme_preference_survives_reload(open_page):
    page = open_page(
        StaticClient(board_payload()),
        color_scheme="dark",
        init_script="localStorage.setItem('spec-tracker-theme', 'light');",
    )
    selector = page.get_by_label("Theme", exact=True)
    assert selector.input_value() == "light"
    assert page.locator("html").get_attribute("data-theme") == "light"
    page.reload()
    playwright.expect(selector).to_have_value("light")
    assert page.locator("html").get_attribute("data-theme") == "light"


def test_system_theme_follows_live_changes_and_remembers_system_choice(open_page):
    page = open_page(StaticClient(board_payload()), color_scheme="light")
    selector = page.get_by_label("Theme", exact=True)
    selector.select_option("system")
    assert page.locator("html").get_attribute("data-theme") == "light"
    page.emulate_media(color_scheme="dark")
    playwright.expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.reload()
    playwright.expect(selector).to_have_value("system")
    assert page.locator("html").get_attribute("data-theme") == "dark"
    page.emulate_media(color_scheme="light")
    playwright.expect(page.locator("html")).to_have_attribute("data-theme", "light")
    assert selector.input_value() == "system"


@pytest.mark.parametrize("storage_setup", [
    "localStorage.setItem('spec-tracker-theme', 'unrecognized');",
    """Object.defineProperty(window, 'localStorage', {
      get() { throw new DOMException('Blocked', 'SecurityError'); }
    });""",
    """Storage.prototype.setItem = function() {
      throw new DOMException('Full', 'QuotaExceededError');
    };""",
])
def test_unavailable_or_invalid_saved_theme_keeps_page_and_selector_usable(
    open_page, storage_setup
):
    page = open_page(StaticClient(board_payload()), init_script=storage_setup)
    assert page.locator("html").get_attribute("data-theme") == "dark"
    page.get_by_label("Theme", exact=True).select_option("light")
    assert page.locator("html").get_attribute("data-theme") == "light"
    page.locator(f'.card[data-package-id="{STEP_TWO}"]').click()
    assert connection_pairs(page) == {(STEP_ONE, STEP_TWO), (STEP_TWO, GATE), (STEP_ONE, LOOSE)}


def test_theme_changes_sync_between_tabs(open_page):
    page = open_page(StaticClient(board_payload()))
    other = page.context.new_page()
    try:
        other.goto(page.url)
        other.get_by_label("Theme", exact=True).select_option("light")
        playwright.expect(page.get_by_label("Theme", exact=True)).to_have_value("light")
        assert page.locator("html").get_attribute("data-theme") == "light"
        other.evaluate("localStorage.removeItem('spec-tracker-theme')")
        playwright.expect(page.get_by_label("Theme", exact=True)).to_have_value("dark")
        assert page.locator("html").get_attribute("data-theme") == "dark"
    finally:
        other.close()


def test_cycle_preserves_order_of_upstream_and_downstream_packages(open_page):
    value = json.loads(board_payload())
    upstream = "123e4567-e89b-42d3-a456-426614174004"
    value["entries"] = [
        _entry(upstream, "Alpha/Queue/root", "queue", "Z upstream", "Alpha"),
        _entry(STEP_ONE, "Alpha/Queue/a", "queue", "Y cycle A", "Alpha",
               [_edge(STEP_TWO, "cycle"), _edge(upstream, "input")]),
        _entry(STEP_TWO, "Alpha/Queue/b", "queue", "X cycle B", "Alpha",
               [_edge(STEP_ONE, "cycle")]),
        _entry(GATE, "Alpha/Queue/c", "queue", "B prerequisite C", "Alpha",
               [_edge(STEP_TWO, "input")]),
        _entry(LOOSE, "Alpha/Queue/d", "queue", "A dependent D", "Alpha",
               [_edge(GATE, "input")]),
    ]
    value["entries"].sort(key=lambda entry: entry["package_path"])
    _reseal(value)
    page = open_page(StaticClient(value))
    assert page.locator(".card-title").all_text_contents() == [
        "Z upstream", "X cycle B", "Y cycle A", "B prerequisite C", "A dependent D",
    ]
    assert connection_pairs(page) == {
        (upstream, STEP_ONE), (STEP_ONE, STEP_TWO), (STEP_TWO, STEP_ONE),
        (STEP_TWO, GATE), (GATE, LOOSE),
    }
    assert_readable_arrows(page)
    assert_rail_lanes_do_not_overlap(page)


@pytest.mark.parametrize("reason", [
    "missing_target", "duplicate_target", "identity_coverage_incomplete",
    "target_unreadable", "target_changed_during_read", "target_invalid_identity",
    "invalid_prerequisite", "self_edge",
])
def test_unresolved_relationship_does_not_change_card_order(open_page, reason):
    value = json.loads(board_payload())
    edge = _edge(STEP_ONE, "input")
    edge.update(reason=reason, resolved_state="unknown")
    value["entries"] = [
        _entry(STEP_ONE, "Alpha/Queue/a", "queue", "Z candidate", "Alpha"),
        _entry(STEP_TWO, "Alpha/Queue/b", "queue", "A unresolved", "Alpha", [edge]),
    ]
    _reseal(value)
    page = open_page(StaticClient(value))
    assert page.locator(".card-title").all_text_contents() == ["A unresolved", "Z candidate"]
    assert connection_pairs(page) == set()


@pytest.mark.parametrize("duplicate_id", [STEP_ONE, STEP_TWO])
def test_identity_collision_in_another_project_does_not_change_card_order(
    open_page, duplicate_id
):
    value = json.loads(board_payload())
    value["entries"] = [
        _entry(STEP_ONE, "Alpha/Queue/a", "queue", "Z candidate", "Alpha"),
        _entry(STEP_TWO, "Alpha/Queue/b", "queue", "A dependent", "Alpha",
               [_edge(STEP_ONE, "input")]),
        _entry(duplicate_id, "Beta/Queue/c", "queue", "Duplicate", "Beta"),
    ]
    _admit_inventory_stage(value, "Beta", "Queue")
    _reseal(value)
    page = open_page(StaticClient(value))
    assert page.locator('.board-row[data-lifecycle="Queue"] .cell').first.locator(
        '.card-title'
    ).all_text_contents() == ["A dependent", "Z candidate"]
    assert connection_pairs(page) == set()


def test_successful_changed_poll_clears_previous_error_without_applying_update(open_page):
    client = SequenceClient([
        board_payload(), CatalogError("producer_timeout"),
        board_payload(titles={"step_one": "Recovered foundation"}),
    ])
    page = open_page(client, fake_clock=True)
    fire_poll(page)
    page.locator("#board").get_by_text("Latest refresh issue: producer_timeout", exact=True).wait_for(timeout=5000)
    fire_poll(page)
    page.wait_for_selector("#refresh.pending", timeout=5000)
    assert page.locator("#board-issues").count() == 0
    assert page.locator(".card-title").get_by_text("Foundation step", exact=True).count() == 1
    assert page.locator(".card-title").get_by_text("Recovered foundation", exact=True).count() == 0
    page.click("#refresh")
    assert page.locator(".card-title").get_by_text("Recovered foundation", exact=True).count() == 1
    assert client.calls == 3


def test_real_producer_renders_all_seven_rows_and_finished_done_hides_provider_links_and_rail(
    open_page, tmp_path,
):
    root = tmp_path / "seven-stages"
    for stage, _lifecycle, _label, title in STAGE_ROWS:
        body = f"# {title}\nPackage ID: {STAGE_IDS[stage]}\n"
        if stage == "Queue":
            body += f"Prerequisite: {STAGE_IDS['Done']} | release\n"
        if stage == "Done":
            body += "Claim: release | satisfied | sha256:" + "b" * 64 + "\n"
        _write_package(root, "Fictional", stage, "package", STAGE_IDS[stage], body)
    client = ScanningClient(root)
    page = open_page(client)
    assert client.fetch_catalog().as_dict()["terminal_stages"] == []
    ordered_rows = sorted(STAGE_ROWS)
    assert page.locator(".row-head").all_text_contents() == [label for _, _, label, _ in ordered_rows]
    assert page.locator("#board .card").count() == 7
    assert page.locator(".board-row").evaluate_all(
        "rows => rows.map(row => [row.dataset.lifecycle, row.querySelector('.card-title')?.textContent])"
    ) == [[stage, title] for stage, _, _, title in ordered_rows]

    hidden = json.loads(lifecycle_payload(completed_stages=("Done",)))
    queue_path = "Fictional/Queue/package"
    done_path = "Fictional/Done/package"
    hidden_page = open_page(StaticClient(hidden))
    queue = hidden_page.locator(f'.card[data-package-path="{queue_path}"]')
    queue.click()

    assert hidden_page.locator('.board-row[data-lifecycle="Done"]').count() == 0
    assert hidden_page.locator(f'.card[data-package-path="{done_path}"]').count() == 0
    hidden_page.fill("#filter", "Done target")
    assert hidden_page.locator("#board .card:visible").count() == 0
    hidden_page.fill("#filter", "")
    assert queue.locator(".card-links").count() == 0
    assert hidden_page.locator(
        f'.connection[data-source="{STAGE_IDS["Done"]}"]'
    ).count() == 0


def test_compact_view_uses_inventory_axes_and_keeps_gated_card_hidden_on_selection(open_page):
    page = open_page(StaticClient(compact_payload()))
    compact = page.get_by_label("Hide empty rows and columns", exact=True)
    assert compact.is_checked()
    assert page.locator(".column-head").all_text_contents() == ["Alpha"]
    assert row_labels(page) == ["Partial", "Queue", "Testing"]
    assert page.locator('.card[data-package-path="Alpha/Testing/custom"]').count() == 1
    assert page.locator('.card[data-package-path="Alpha/Done/completed"]').count() == 0
    assert "incomplete / unavailable" in page.locator(
        '.board-row[data-lifecycle="Partial"] .row-head'
    ).inner_text()

    dependent = page.locator('.card[data-package-path="Alpha/Queue/dependent"]')
    dependent.click()
    assert page.locator('.card[data-package-path="Alpha/Done/completed"]').count() == 0

    compact.uncheck()
    assert not compact.is_checked()
    assert page.locator(".column-head").all_text_contents() == [
        "Alpha", "EmptyProject", "ZeroProject"
    ]
    assert row_labels(page) == [
        "Partial", "Queue", "Testing", "Empty"
    ]
    assert page.locator('.board-row[data-lifecycle="Empty"] .empty').all_text_contents() == [
        "—", "—", "—"
    ]
    assert page.locator('.card[data-package-path="Alpha/Done/completed"]').count() == 0


def test_no_eligible_stage_state_keeps_projects_when_compaction_is_disabled(open_page):
    value = json.loads(compact_payload())
    value["inventory"] = {"projects": [{"name": "ZeroProject", "availability": "complete"}], "stages": []}
    value["entries"] = []
    value["terminal_stages"] = []
    _reseal(value)
    page = open_page(StaticClient(value))
    assert page.get_by_text("Empty folders are hidden", exact=False).count() == 1
    compact = page.get_by_label("Hide empty rows and columns", exact=True)
    compact.uncheck()
    assert page.locator(".column-head").all_text_contents() == ["ZeroProject"]
    assert page.get_by_text("No eligible stage directories were found.", exact=True).count() == 1


def test_compact_view_keeps_incomplete_project_when_no_stage_is_discoverable(open_page):
    value = json.loads(compact_payload())
    value["inventory"] = {
        "projects": [{"name": "UnreadableProject", "availability": "incomplete"}],
        "stages": [],
    }
    value["terminal_stages"] = []
    value["entries"] = []
    _reseal(value)

    page = open_page(StaticClient(value))

    assert page.get_by_label("Hide empty rows and columns", exact=True).is_checked()
    heading = page.locator(".column-head")
    assert heading.count() == 1
    assert heading.evaluate("node => node.firstChild.textContent") == "UnreadableProject"
    assert heading.locator(".dimension-incomplete").inner_text() == "incomplete / unavailable"
    assert page.get_by_text("No eligible stage directories were found.", exact=True).count() == 1


def test_literal_stage_names_do_not_alias_builtin_labels(open_page):
    value = json.loads(board_payload())
    value["inventory"] = {
        "projects": [{"name": "Alpha", "availability": "complete"}],
        "stages": [
            {"project": "Alpha", "stage": "Queue", "availability": "complete"},
            {"project": "Alpha", "stage": "queue", "availability": "complete"},
        ],
    }
    value["entries"] = [
        _entry(STEP_ONE, "Alpha/Queue/upper", "queue", "Upper Queue", "Alpha"),
        _entry(STEP_TWO, "Alpha/queue/lower", "queue", "Lower queue", "Alpha"),
    ]
    _reseal(value)

    page = open_page(StaticClient(value))

    assert page.locator(".row-head").all_text_contents() == ["Queue", "queue"]
    assert page.locator(".board-row").evaluate_all(
        "rows => rows.map(row => row.dataset.lifecycle)"
    ) == ["Queue", "queue"]


def test_admitted_incomplete_and_truly_empty_states_remain_distinct(open_page):
    admitted = json.loads(lifecycle_payload())
    admitted["entries"] = []
    _reseal(admitted)
    admitted_page = open_page(StaticClient(admitted))
    compact = admitted_page.get_by_label("Hide empty rows and columns", exact=True)
    compact.uncheck()
    assert admitted_page.locator(".board-empty").inner_text() == (
        "The catalog contains admitted folders but no work items."
    )

    incomplete = json.loads(compact_payload())
    incomplete["entries"] = []
    _reseal(incomplete)
    incomplete_page = open_page(StaticClient(incomplete))
    assert incomplete_page.locator(".board-empty").inner_text() == (
        "The catalog has incomplete dimensions; no work items are currently available."
    )

    empty = json.loads(lifecycle_payload())
    empty["inventory"] = {"projects": [], "stages": []}
    empty["entries"] = []
    _reseal(empty)
    empty_page = open_page(StaticClient(empty))
    assert empty_page.locator(".board-empty").inner_text() == "No work items in the catalog."


def test_malformed_poll_retains_displayed_board_and_local_preference(open_page):
    valid = json.loads(compact_payload())
    invalid = json.loads(compact_payload(extra_stage=True))
    invalid["inventory"]["stages"].reverse()
    _reseal(invalid)

    from nyx import server as server_module

    def passthrough_catalog(candidate):
        return candidate if isinstance(candidate, RawCatalog) else parse_catalog(candidate)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(server_module, "parse_catalog", passthrough_catalog)
        page = open_page(
            RawSequenceClient([valid, invalid]),
            init_script="localStorage.setItem('spec-tracker-compact-view', 'false');",
            fake_clock=True,
        )
        fire_poll(page)
        page.locator("#board").get_by_text("Latest refresh issue: producer_protocol_error", exact=True).wait_for(
            timeout=5000
        )

    # The refresh issue is addressable on its own while workspace issues share the summary.
    assert page.locator("#board-issues summary").inner_text() == (
        "Workspace issues: discovery_unavailable (1) · Latest refresh issue: producer_protocol_error"
    )
    assert not page.get_by_label("Hide empty rows and columns", exact=True).is_checked()
    assert page.locator(".column-head").all_text_contents() == [
        "Alpha", "EmptyProject", "ZeroProject"
    ]
    assert page.locator(".board-row").evaluate_all(
        "rows => rows.map(row => row.dataset.lifecycle)"
    ) == ["Partial", "Queue", "Testing", "Empty"]
    assert page.locator('.card[data-package-path="Alpha/Review/new"]').count() == 0
    assert page.locator("#refresh").inner_text() == "Refresh view"


@pytest.mark.parametrize("storage_setup", [
    "localStorage.setItem('spec-tracker-compact-view', 'false');",
    """Object.defineProperty(window, 'localStorage', {
      get() { throw new DOMException('Blocked', 'SecurityError'); }
    });""",
    """Storage.prototype.setItem = function() {
      throw new DOMException('Full', 'QuotaExceededError');
    };""",
])
def test_compact_preference_uses_storage_and_checked_fallback(open_page, storage_setup):
    page = open_page(StaticClient(compact_payload()), init_script=storage_setup)
    compact = page.get_by_label("Hide empty rows and columns", exact=True)
    if "setItem('spec-tracker-compact-view', 'false')" in storage_setup:
        assert not compact.is_checked()
        assert page.locator(".column-head").all_text_contents() == [
            "Alpha", "EmptyProject", "ZeroProject"
        ]
    else:
        assert compact.is_checked()
    compact.uncheck()
    assert not compact.is_checked()
    page.reload()
    assert not compact.is_checked() if "setItem('spec-tracker-compact-view', 'false')" in storage_setup else compact.is_checked()


def test_terminal_rows_hide_and_show_with_the_checkbox(open_page):
    value = json.loads(lifecycle_payload())
    value["terminal_stages"] = ["Archive", "Done"]
    _reseal(value)
    page = open_page(StaticClient(value))

    terminal = page.get_by_label("Hide terminal rows", exact=True)
    assert terminal.is_checked()
    # Finished rows start hidden; unfinished rows stay on the board.
    assert page.locator('.board-row[data-lifecycle="Done"]').count() == 0
    assert page.locator('.board-row[data-lifecycle="Archive"]').count() == 0
    assert page.locator('.board-row[data-lifecycle="Under_Development"]').count() == 1
    assert page.locator('.card[data-package-path="Fictional/Done/package"]').count() == 0

    terminal.uncheck()
    assert not terminal.is_checked()
    assert page.locator('.board-row[data-lifecycle="Done"]').count() == 1
    assert page.locator('.board-row[data-lifecycle="Archive"]').count() == 1
    assert page.locator('.card[data-package-path="Fictional/Done/package"]').count() == 1


def test_terminal_preference_uses_storage_and_checked_fallback(open_page):
    value = json.loads(lifecycle_payload())
    value["terminal_stages"] = ["Archive", "Done"]
    _reseal(value)

    page = open_page(StaticClient(value))
    terminal = page.get_by_label("Hide terminal rows", exact=True)
    assert terminal.is_checked()
    terminal.uncheck()
    assert page.evaluate("localStorage.getItem('spec-tracker-hide-terminal-rows')") == "false"
    assert page.locator('.board-row[data-lifecycle="Done"]').count() == 1
    # No init script on this page, so the stored preference survives the reload.
    page.reload()
    assert not terminal.is_checked()
    page.wait_for_selector('.board-row[data-lifecycle="Done"]', timeout=15000)
    assert page.locator('.board-row[data-lifecycle="Done"]').count() == 1

    restored = open_page(
        StaticClient(value),
        init_script="localStorage.setItem('spec-tracker-hide-terminal-rows', 'false');",
    )
    assert not restored.get_by_label("Hide terminal rows", exact=True).is_checked()
    assert restored.locator('.board-row[data-lifecycle="Done"]').count() == 1

    blocked = open_page(
        StaticClient(value),
        init_script="""Object.defineProperty(window, 'localStorage', {
          get() { throw new DOMException('Blocked', 'SecurityError'); }
        });""",
    )
    assert blocked.get_by_label("Hide terminal rows", exact=True).is_checked()



def test_terminal_only_board_explains_hidden_finished_rows(open_page):
    value = json.loads(lifecycle_payload())
    keep = {"Done", "Archive"}
    value["inventory"]["stages"] = [
        stage for stage in value["inventory"]["stages"] if stage["stage"] in keep
    ]
    value["entries"] = [entry for entry in value["entries"] if entry["stage"] in keep]
    value["terminal_stages"] = ["Archive", "Done"]
    _reseal(value)
    page = open_page(StaticClient(value))

    empty = page.locator(".board-empty.terminal-hidden")
    assert empty.count() == 1
    assert "Hide terminal rows" in empty.inner_text()

    page.get_by_label("Hide terminal rows", exact=True).uncheck()
    assert page.locator(".board-row").count() == 2


@pytest.mark.parametrize("compact", [True, False], ids=["compact", "expanded"])
def test_finished_rows_hidden_message_names_hide_terminal_rows(open_page, compact):
    value = json.loads(compact_payload())
    value["inventory"]["projects"] = [{"name": "Alpha", "availability": "complete"}]
    value["inventory"]["stages"] = [
        stage for stage in value["inventory"]["stages"]
        if stage["stage"] in {"Done", "Queue"}
    ]
    value["entries"] = [entry for entry in value["entries"] if entry["stage"] == "Done"]
    value["discovery_diagnostics"] = []
    _reseal(value)
    page = open_page(StaticClient(value))
    compact_control = page.get_by_label("Hide empty rows and columns", exact=True)
    assert compact_control.is_checked()
    assert page.get_by_label("Hide terminal rows", exact=True).is_checked()
    if not compact:
        compact_control.uncheck()
        assert page.locator(".column-head").all_text_contents() == ["Alpha"]
        assert row_labels(page) == ["Queue"]

    empty = page.locator("#board .board-empty")
    assert empty.count() == 1
    assert set(empty.get_attribute("class").split()) == {"empty", "board-empty", "terminal-hidden"}
    assert empty.inner_text() == (
        "Finished rows are hidden. Uncheck “Hide terminal rows” to show them."
    )
    assert page.locator("#board .compact-hidden, #board .admitted-empty").count() == 0
    assert page.locator("#board .card").count() == 0

    page.get_by_label("Hide terminal rows", exact=True).uncheck()
    assert page.locator('#board .card[data-package-path="Alpha/Done/completed"]').count() == 1
    assert page.locator("#board .board-empty").count() == 0


def test_finished_rows_hidden_precedes_incomplete_discovery(open_page):
    value = json.loads(compact_payload())
    value["inventory"]["projects"] = [{"name": "Alpha", "availability": "complete"}]
    value["inventory"]["stages"] = [
        stage for stage in value["inventory"]["stages"]
        if stage["stage"] in {"Done", "Partial", "Queue"}
    ]
    value["entries"] = [entry for entry in value["entries"] if entry["stage"] == "Done"]
    _reseal(value)
    page = open_page(StaticClient(value))
    assert page.get_by_label("Hide empty rows and columns", exact=True).is_checked()
    assert page.get_by_label("Hide terminal rows", exact=True).is_checked()

    empty = page.locator("#board .board-empty")
    assert empty.count() == 1
    assert set(empty.get_attribute("class").split()) == {"empty", "board-empty", "terminal-hidden"}
    assert empty.inner_text() == (
        "Finished rows are hidden. Uncheck “Hide terminal rows” to show them."
    )
    assert page.locator("#board .compact-hidden, #board .admitted-empty").count() == 0
    assert "incomplete / unavailable" in page.locator(
        '.board-row[data-lifecycle="Partial"] .row-head'
    ).inner_text()


def test_search_resize_and_pending_apply_keep_axes_and_rails_valid(open_page):
    first = compact_payload()
    second = compact_payload(extra_stage=True)
    page = open_page(SequenceClient([first, second]), fake_clock=True)
    assert row_labels(page) == ["Partial", "Queue", "Testing"]
    assert connection_pairs(page) == {(COMPACT_CARD, COMPACT_DEPENDENT)}
    page.fill("#filter", "custom")
    assert row_labels(page) == ["Partial", "Queue", "Testing"]
    assert page.locator("#board .card:visible").count() == 1
    assert page.locator(".rail-layer .rail").count() == 0
    page.fill("#filter", "")
    before = page.locator(".rail").first.get_attribute("d")
    page.set_viewport_size({"width": 650, "height": 900})
    page.wait_for_function(
        "before => document.querySelector('.rail').getAttribute('d') !== before", arg=before
    )
    assert connection_pairs(page) == {(COMPACT_CARD, COMPACT_DEPENDENT)}
    fire_poll(page)
    page.wait_for_selector("#refresh.pending", timeout=5000)
    assert page.locator('.card[data-package-path="Alpha/Review/new"]').count() == 0
    assert page.locator('.board-row[data-lifecycle="Review"]').count() == 0
    page.click("#refresh")
    page.locator('.card[data-package-path="Alpha/Review/new"]').wait_for(timeout=15000)
    assert page.locator('.board-row[data-lifecycle="Review"]').count() == 1


def test_pending_removed_entry_snapshot_clears_selection_only_after_apply(open_page):
    first = lifecycle_payload()
    second = json.loads(lifecycle_payload())
    second["entries"] = [entry for entry in second["entries"] if entry["stage"] != "Done"]
    _reseal(second)
    page = open_page(SequenceClient([first, second]), fake_clock=True)
    done = page.locator('.card[data-package-path="Fictional/Done/package"]')
    done.click()
    assert done.get_attribute("aria-pressed") == "true"

    fire_poll(page)
    page.wait_for_selector("#refresh.pending", timeout=5000)
    assert page.locator('.card[data-package-path="Fictional/Done/package"]').count() == 1
    assert done.get_attribute("aria-pressed") == "true"

    page.click("#refresh")
    page.wait_for_function(
        "() => !document.querySelector('#refresh').classList.contains('pending')",
        timeout=15000,
    )
    assert page.locator('.board-row[data-lifecycle="Done"]').count() == 0
    assert page.locator('.card[data-package-path="Fictional/Done/package"]').count() == 0
    assert page.locator(".card.selected").count() == 0


def test_real_producer_scalar_policy_paths_and_diagnostics_are_displayed(open_page):
    payload, value = unicode_producer_payload()
    assert value["terminal_stages"] == [UNICODE_LOW, UNICODE_HIGH]
    diagnostics = value["program_coverage"]["diagnostics"]
    assert [item["code"] for item in diagnostics] == ["invalid_package", "invalid_package"]
    assert diagnostics[0]["message"] < diagnostics[1]["message"]

    page = open_page(StaticClient(payload))
    paths = page.locator("#board .card").evaluate_all(
        "cards => cards.map(card => card.dataset.packagePath)"
    )
    expected_paths = [
        f"Project{marker}/Queue/diagnostic-{marker}"
        for marker in (UNICODE_LOW, UNICODE_HIGH)
    ]
    assert sorted(paths) == expected_paths
    for marker, path in zip((UNICODE_LOW, UNICODE_HIGH), expected_paths):
        card = page.locator(f'.card[data-package-path="{path}"]')
        card.click()
        assert card.locator(".card-title").inner_text() == f"Diagnostic {marker}"

    rendered_diagnostics = page.locator("#board-issues li").all_text_contents()
    expected_diagnostics = [
        f"{item['code']} {item['message']}"
        for item in (
            value["discovery_diagnostics"]
            + value["identity_coverage"]["diagnostics"]
            + value["program_coverage"]["diagnostics"]
        )
    ]
    assert rendered_diagnostics == expected_diagnostics
    assert len(rendered_diagnostics) == 2


@pytest.mark.parametrize("kind", ["policy", "path", "diagnostics"])
def test_digest_consistent_reversed_unicode_sequences_keep_last_valid_board(open_page, kind):
    payload, value = unicode_producer_payload()
    reversed_value = reversed_unicode_payload(payload, kind)
    expected_paths = [
        f"Project{marker}/Queue/diagnostic-{marker}"
        for marker in (UNICODE_LOW, UNICODE_HIGH)
    ]
    assert value["catalog_digest"] != reversed_value["catalog_digest"]
    if kind == "diagnostics":
        assert {item["code"] for item in reversed_value["program_coverage"]["diagnostics"]} == {
            "invalid_package"
        }

    from nyx import server as server_module

    def passthrough_catalog(candidate):
        return candidate if isinstance(candidate, RawCatalog) else parse_catalog(candidate)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(server_module, "parse_catalog", passthrough_catalog)
        page = open_page(RawSequenceClient([json.loads(payload), reversed_value]))
        page.get_by_role("button", name="Refresh view").click()
        page.locator("#board").get_by_text("Latest refresh issue: producer_protocol_error", exact=True).wait_for(
            timeout=15000
        )

    assert sorted(page.locator("#board .card").evaluate_all(
        "cards => cards.map(card => card.dataset.packagePath)"
    )) == expected_paths
    assert page.locator("#refresh").inner_text() == "Refresh view"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(terminal_stages=["Done", "Done"]),
        lambda value: value.pop("terminal_stages"),
        lambda value: value.update(schema_version=7),
        lambda value: value.update(visibility={}),
    ],
    ids=[
        "equal-terminal-stages",
        "missing-terminal-stages",
        "older-schema",
        "top-level-extra-key",
    ],
)
def test_digest_consistent_invalid_terminal_policy_keeps_last_valid_board(open_page, mutate):
    valid = json.loads(lifecycle_payload())
    invalid = json.loads(lifecycle_payload())
    for entry in invalid["entries"]:
        entry["declared"]["title"] = "Rejected refresh content"
    mutate(invalid)
    invalid["catalog_digest"] = canonical_digest(invalid)

    from nyx import server as server_module

    def passthrough_catalog(candidate):
        return candidate if isinstance(candidate, RawCatalog) else parse_catalog(candidate)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(server_module, "parse_catalog", passthrough_catalog)
        page = open_page(RawSequenceClient([valid, invalid]))
        page.get_by_role("button", name="Refresh view").click()
        page.locator("#board").get_by_text("Latest refresh issue: producer_protocol_error", exact=True).wait_for(
            timeout=15000
        )

    assert page.locator("#board .card").count() == 7
    assert page.locator(".row-head").all_text_contents() == [
        label for _stage, _lifecycle, label, _title in sorted(STAGE_ROWS)
    ]
    assert page.locator(".board-row").evaluate_all(
        "rows => rows.map(row => [row.dataset.lifecycle, row.querySelector('.card-title')?.textContent])"
    ) == [[stage, title] for stage, _lifecycle, _label, title in sorted(STAGE_ROWS)]
    assert page.locator("#refresh").inner_text() == "Refresh view"


def _malformed_browser_inventory(case):
    value = json.loads(lifecycle_payload())
    if case == "unknown-inventory-key":
        value["inventory"]["extra"] = []
    elif case == "projects-type":
        value["inventory"]["projects"] = {}
    elif case == "duplicate-project":
        value["inventory"]["projects"].append(dict(value["inventory"]["projects"][0]))
    elif case == "unordered-project":
        value["inventory"]["projects"].append(
            {"name": "Alpha", "availability": "complete"}
        )
    elif case == "duplicate-stage-pair":
        value["inventory"]["stages"].append(dict(value["inventory"]["stages"][-1]))
    elif case == "unordered-stage-pair":
        value["inventory"]["stages"].reverse()
    elif case == "unsafe-project":
        value["inventory"]["projects"][0]["name"] = "../outside"
    elif case == "missing-stage-parent":
        value["inventory"]["stages"][0]["project"] = "Missing"
    elif case == "invalid-availability":
        value["inventory"]["stages"][0]["availability"] = "unknown"
    elif case == "entry-not-admitted":
        value["entries"][0]["stage"] = "Missing"
    elif case == "digest-mismatch":
        value["catalog_digest"] = "0" * 64
        return value
    else:  # pragma: no cover - the parameter list owns the cases
        raise AssertionError(case)
    _reseal(value)
    return value


@pytest.mark.parametrize(
    "case",
    [
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
def test_browser_rejects_each_malformed_inventory_class_before_replacing_board(
    open_page, case
):
    valid = json.loads(lifecycle_payload())
    invalid = _malformed_browser_inventory(case)

    from nyx import server as server_module

    def passthrough_catalog(candidate):
        return candidate if isinstance(candidate, RawCatalog) else parse_catalog(candidate)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(server_module, "parse_catalog", passthrough_catalog)
        page = open_page(RawSequenceClient([valid, invalid]))
        page.get_by_role("button", name="Refresh view").click()
        page.locator("#board").get_by_text("Latest refresh issue: producer_protocol_error", exact=True).wait_for(
            timeout=15000
        )

    assert page.locator("#board .card").count() == 7
    assert page.locator('.card[data-package-path="Fictional/Done/package"]').count() == 1
    assert page.locator("#refresh").inner_text() == "Refresh view"


@pytest.mark.parametrize("value,accepted", reported_contract_cases())
def test_reported_contract_at_browser_acceptance(open_page, monkeypatch, value, accepted):
    from nyx import server as server_module

    monkeypatch.setattr(server_module, "parse_catalog", lambda candidate: candidate if isinstance(candidate, RawCatalog) else parse_catalog(candidate))
    assert value["catalog_digest"] == canonical_digest(value)
    if accepted:
        page = open_page(RawSequenceClient([value]))
        assert page.locator(".card-title").all_text_contents() == ["First", "Second"]
        expected_issues = [
            f"{item['code']} {item['message']}"
            for item in (
                value["discovery_diagnostics"]
                + value["identity_coverage"]["diagnostics"]
                + value["program_coverage"]["diagnostics"]
            )
        ]
        assert page.locator("#board-issues li").all_text_contents() == expected_issues
    else:
        page = open_page(RawSequenceClient([raw_catalog(), value]))
        assert page.locator(".card-title").all_text_contents() == ["First", "Second"]
        page.click("#refresh")
        page.locator("#board").get_by_text("Latest refresh issue: producer_protocol_error", exact=True).wait_for()
        assert page.locator(".card-title").all_text_contents() == ["First", "Second"]


def test_real_screened_reported_and_declared_text_is_browser_safe(open_page, tmp_path):
    long_value = "startword " + "a" * 1013 + "😀" + "b" * 66 + " tailword"
    _write_package(tmp_path, "Alpha", "Queue", "long", STEP_ONE,
                   f"# {'t' * 1100}\nPackage ID: {STEP_ONE}\nOwner Note: {long_value}\n")
    _write_package(tmp_path, "Alpha", "Queue", "tabs", STEP_TWO,
                   f"# Tab\ttitle\nPackage ID: {STEP_TWO}\nOwner Note: tab\tvalue\nStatus: tab\tstatus\n")
    value = json.loads(scan_catalog(tmp_path))
    first, second = value["entries"]
    assert first["reported_fields"] == [{"name": "Owner Note", "value": long_value[:1023]}]
    assert first["declared"]["title"] == "t" * 1024
    assert second["declared"]["title"] == "Tab title"
    assert second["declared"]["status"] == "tab status"
    assert second["reported_fields"] == [{"name": "Owner Note", "value": "tab value"}]
    assert parse_catalog(value).as_dict() == value
    page = open_page(ScanningClient(tmp_path))
    assert page.locator(".card").count() == 2
    card = page.locator(f'.card[data-package-id="{STEP_ONE}"]')
    page.fill("#filter", "startword")
    playwright.expect(card).to_be_visible()
    page.fill("#filter", "tailword")
    playwright.expect(card).to_be_hidden()


@pytest.mark.parametrize("duplicate", [False, True], ids=["missing", "duplicate"])
def test_real_unresolved_targets_do_not_draw_connections(open_page, tmp_path, duplicate):
    _write_package(tmp_path, "Alpha", "Queue", "source", STEP_ONE,
                   f"# Source\nPackage ID: {STEP_ONE}\nPrerequisite: {STEP_TWO} | release\nClaim: local | maybe\n")
    if duplicate:
        for name in ("target-a", "target-b"):
            _write_package(tmp_path, "Alpha", "Queue", name, STEP_TWO,
                           f"# Target\nPackage ID: {STEP_TWO}\nClaim: release | satisfied | sha256:{'a' * 64}\n")
    value = json.loads(scan_catalog(tmp_path))
    edge = value["entries"][0]["relationship"]["prerequisites"][0]
    assert edge["reason"] == ("duplicate_target" if duplicate else "missing_target")
    page = open_page(ScanningClient(tmp_path))
    card = page.locator(f'.card[data-package-id="{STEP_ONE}"]')
    assert "unresolved target" in card.inner_text()
    assert connection_pairs(page) == set()
    page.fill("#filter", "partial")
    playwright.expect(card).to_be_hidden()


def test_real_workspace_issues_and_default_relationship_cards_render(open_page, tmp_path):
    stage = tmp_path / "Alpha" / "Queue"
    stage.mkdir(parents=True)
    for name, data in (("empty", b""), ("invalid", b"\xff")):
        folder = stage / name
        folder.mkdir()
        (folder / "spec.md").write_bytes(data)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (stage / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")
    value = json.loads(scan_catalog(tmp_path))
    assert value["identity_coverage"]["state"] == "incomplete"
    assert parse_catalog(value).as_dict() == value
    page = open_page(ScanningClient(tmp_path))
    assert "discovery_unavailable" in page.locator("#board-issues").inner_text()
    assert sorted(page.locator(".card").evaluate_all("cards => cards.map(card => card.dataset.packagePath)")) == ["Alpha/Queue/empty", "Alpha/Queue/invalid"]


def test_real_unsafe_and_overlong_folders_keep_the_healthy_card_after_refresh(open_page, tmp_path):
    if sys.platform != "linux":
        pytest.skip("unsafe names and over-limit trees are created only on Linux")

    _write_package(tmp_path, "Alpha", "Queue", "healthy", STEP_ONE,
                   f"# Healthy\nPackage ID: {STEP_ONE}\n")
    page = open_page(ScanningClient(tmp_path), fake_clock=True)
    healthy = page.locator('[data-package-path="Alpha/Queue/healthy"]')
    assert healthy.count() == 1
    assert healthy.locator(".card-title").inner_text() == "Healthy"

    # An unsafe name below a grouping folder: the diagnostic names the parent,
    # never the rejected name itself.
    unsafe = tmp_path / "Alpha" / "Queue" / "grp" / "unsafe-marker\nname" / "item"
    unsafe.mkdir(parents=True)
    (unsafe / "spec.md").write_text("# Unsafe\n", encoding="utf-8")

    # A package whose workspace-relative path is over the producer limit: the
    # fifth 200-character component is excluded and the diagnostic names the
    # deepest admitted ancestor.
    component = "d" * 200
    deepest = tmp_path / "Alpha" / "Queue" / "deep"
    for _level in range(5):
        deepest = deepest / component
        deepest.mkdir(parents=True)
    over_limit = deepest / ("p" * 80)
    over_limit.mkdir()
    (over_limit / "spec.md").write_text(
        f"# Deep\nPackage ID: {STEP_ONE}\n", encoding="utf-8"
    )

    # The documented refresh flow: the button starts as Refresh view with no
    # update waiting, the periodic poll advertises the changed catalog as
    # Apply update, and choosing that button applies the new catalog.
    refresh = page.locator("#refresh")
    assert refresh.inner_text() == "Refresh view"
    fire_poll(page)
    page.wait_for_selector("#refresh.pending", timeout=5000)
    assert refresh.inner_text() == "Apply update"
    refresh.click()
    page.wait_for_function(
        "() => !document.querySelector('#refresh').classList.contains('pending')",
        timeout=15000,
    )

    issues = page.locator("#board-issues")
    assert issues.count() == 1
    if issues.get_attribute("open") is None:
        issues.locator("summary").click()
    # The producer mirrors each discovery diagnostic into identity coverage, so
    # the same two issues render twice; no other issue appears.
    assert set(page.locator("#board-issues li").all_inner_texts()) == {
        "discovery_unavailable discovery unavailable: Alpha/Queue/grp",
        "discovery_unavailable discovery unavailable: "
        f"Alpha/Queue/deep/{component}/{component}/{component}/{component}",
    }
    assert page.locator(".refresh-issue-summary").count() == 0
    assert page.locator('[data-package-path="Alpha/Queue/healthy"]').count() == 1
    assert page.locator('[data-package-path^="Alpha/Queue/deep/"]').count() == 0
    assert "unsafe-marker" not in page.locator("#board").inner_text()


def test_worker_reported_snapshot_round_trips_into_browser(open_page, tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace

    from nyx import state, worker

    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "workspace"
    _write_package(root, "Alpha", "Queue", "worker", STEP_ONE,
                   f"# Worker item\nPackage ID: {STEP_ONE}\nOwner Note: workersearch\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    state.setup(root)
    output = io.BytesIO()
    with monkeypatch.context() as capture:
        capture.setattr(worker.sys, "stdout", SimpleNamespace(buffer=output))
        assert worker._worker_main() == 0
    value = json.loads(output.getvalue())
    assert value["entries"][0]["reported_fields"] == [{"name": "Owner Note", "value": "workersearch"}]
    assert parse_catalog(value).as_dict() == value
    assert value["catalog_digest"] == canonical_digest(value)
    page = open_page(StaticClient(value))
    page.fill("#filter", "workersearch")
    playwright.expect(page.locator(f'.card[data-package-id="{STEP_ONE}"]')).to_be_visible()


@contextmanager
def second_origin_page_server(markup):
    """Serve attacker-controlled markup from a real second loopback origin (D-13)."""

    class AttackerPageHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(markup)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(markup)

        def log_message(self, format, *args):
            return

    page_server = ThreadingHTTPServer(("127.0.0.1", 0), AttackerPageHandler)
    thread = threading.Thread(target=page_server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{page_server.server_port}"
    finally:
        page_server.shutdown()
        thread.join()
        page_server.server_close()


def smuggling_page(target_url, payload):
    """Markup that POSTs raw smuggled bytes at the board origin (F-01)."""
    return (
        "<!doctype html><html><body><script>\n"
        f"const payload = {json.dumps(payload.decode('ascii'))};\n"
        f"const target = {json.dumps(target_url)};\n"
        "fetch(target, {method: 'POST', mode: 'no-cors', body: payload})"
        ".then(() => { document.body.dataset.smuggle = 'settled'; },"
        " (error) => { document.body.dataset.smuggle = 'failed: ' + error; });\n"
        "</script></body></html>"
    ).encode("ascii")


def test_cross_origin_no_cors_post_smuggling_never_saves_settings(open_page):
    settings = BrowserSettings()
    page = open_page(StaticClient(board_payload()), settings=settings)
    board_port = urlparse(page.url).port
    smuggled = embedded_put(board_port, settings.revision)
    with second_origin_page_server(
        smuggling_page(f"http://127.0.0.1:{board_port}/", smuggled)
    ) as attacker_url:
        attacker = page.context.new_page()
        attacker.goto(attacker_url)
        attacker.wait_for_selector("body[data-smuggle]", timeout=15000, state="attached")
        assert attacker.locator("body").get_attribute("data-smuggle") == "settled"
        attacker.wait_for_timeout(1000)
    assert settings.calls == []


def test_frame_from_second_origin_is_blocked_by_frame_ancestors(open_page):
    """A board framed from a second loopback origin loads a chrome error page (D-13)."""
    page = open_page(StaticClient(board_payload()))
    board_port = urlparse(page.url).port
    markup = (
        "<!doctype html><html><body>"
        f'<iframe src="http://127.0.0.1:{board_port}/"></iframe>'
        "</body></html>"
    ).encode("ascii")
    with second_origin_page_server(markup) as attacker_url:
        attacker_context = page.context.browser.new_context()
        attacker = attacker_context.new_page()
        try:
            attacker.goto(attacker_url)
            attacker.locator("iframe").wait_for(state="attached", timeout=15000)
            deadline = time.monotonic() + 15
            while True:
                assert len(attacker.frames) > 1, "the board frame never appeared"
                frame_url = attacker.frames[1].url
                if frame_url.startswith("chrome-error://"):
                    break
                assert time.monotonic() < deadline, (
                    f"the frame URL never became a chrome error page: {frame_url}"
                )
                attacker.wait_for_timeout(200)
            frame = attacker.frames[1]
            assert frame.locator("#board").count() == 0
        finally:
            attacker_context.close()


def test_csp_violation_recorder_records_and_clears(open_page):
    """The open_page recorder is live: an img-src violation is observed, then cleared."""
    page = open_page(StaticClient(board_payload()))
    violations = page._nyx_csp_violations
    assert violations == []
    # A data: URI image is outside img-src 'self' and needs no network.
    page.evaluate(
        "() => { const img = document.createElement('img');"
        " img.src = 'data:image/gif;base64,R0lGODlhAQABAAAAACw=';"
        " document.body.append(img); }"
    )
    page.wait_for_function(
        "() => window.__cspViolations.some((v) => v.directive === 'img-src')",
        timeout=5000,
    )
    assert violations, "the context-level recorder never received the violation"
    assert violations[-1]["directive"] == "img-src"
    violations.clear()


def test_csp_violation_teardown_fails_only_after_the_standalone_server_stops(
    monkeypatch, shared_browser
):
    """A recorded violation fails teardown without leaving serve_forever running."""
    servers = []
    original_create_server = create_server

    def recording_create_server(client):
        created = original_create_server(client)
        servers.append(created)
        return created

    monkeypatch.setitem(globals(), "create_server", recording_create_server)
    before = set(threading.enumerate())
    with pytest.raises(AssertionError, match="recorded CSP violations") as failure:
        with page_launcher(shared_browser) as launch:
            page = launch(StaticClient(board_payload()))
            page.evaluate(
                "() => { const img = document.createElement('img');"
                " img.src = 'data:image/gif;base64,R0lGODlhAQABAAAAACw=';"
                " document.body.append(img); }"
            )
            page.wait_for_function(
                "() => window.__cspViolations.some((v) => v.directive === 'img-src')",
                timeout=5000,
            )
            server_threads = [
                thread for thread in set(threading.enumerate()) - before
                if not thread.daemon
            ]
            assert server_threads, "the standalone server thread never started"
    leaked = [thread for thread in server_threads if thread.is_alive()]
    if leaked:
        # Keep a regression from hanging the interpreter at exit.
        for leaked_server in servers:
            leaked_server.shutdown()
            leaked_server.server_close()
    assert len(servers) == 1
    assert leaked == []
    assert "img-src" in str(failure.value)
    assert page.is_closed()


@pytest.mark.parametrize("with_application", [False, True], ids=["standalone", "application"])
def test_launch_failure_after_server_start_stops_the_server_before_raising(
    monkeypatch, shared_browser, with_application
):
    """A launch that fails while waiting for the board leaves no server running."""
    servers = []
    applications = []
    original_create_server = create_server

    def recording_create_server(client):
        created = original_create_server(client)
        servers.append(created)
        return created

    class RecordingApplicationRuntime(ApplicationRuntime):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            applications.append(self)

    monkeypatch.setitem(globals(), "create_server", recording_create_server)
    monkeypatch.setitem(globals(), "ApplicationRuntime", RecordingApplicationRuntime)
    settings = BrowserSettings() if with_application else None
    # Rename the board before any page script can render into it, so the
    # launch wait for a rendered board can never succeed.
    hide_board = (
        "document.addEventListener('readystatechange', () => {"
        "  const board = document.getElementById('board');"
        "  if (board) { board.id = 'renamed-board'; }"
        "}, {capture: true});"
    )
    before = set(threading.enumerate())
    started = time.monotonic()
    with pytest.raises(playwright.TimeoutError):
        with page_launcher(shared_browser, launch_timeout_ms=1000) as launch:
            launch(StaticClient(board_payload()), init_script=hide_board, settings=settings)
    elapsed = time.monotonic() - started
    leaked = [
        thread for thread in set(threading.enumerate()) - before
        if thread.is_alive() and not thread.daemon
    ] + [
        application.http_thread for application in applications
        if application.http_thread is not None and application.http_thread.is_alive()
    ]
    listening = [
        server for server in servers + [application.server for application in applications]
        if server is not None and server.socket.fileno() != -1
    ]
    if leaked or listening:
        # Keep a regression from hanging the interpreter at exit.
        for leaked_server in servers:
            leaked_server.shutdown()
            leaked_server.server_close()
        for application in applications:
            application.shutdown(time.monotonic() + 2)
    if with_application:
        assert servers == [] and len(applications) == 1
        assert applications[0].http_thread is not None
    else:
        assert len(servers) == 1 and applications == []
    assert leaked == []
    assert listening == []
    assert elapsed < 30


def test_page_launcher_closes_every_context_and_leaves_the_shared_browser_open(shared_browser):
    """Launcher exit closes a test-opened context and keeps the browser connected."""
    with page_launcher(shared_browser):
        stray = shared_browser.new_context()
        stray.new_page()
    assert shared_browser.is_connected()
    assert shared_browser.contexts == []


# ---------------------------------------------------------------------------
# Checkouts client, document validation and the Code Status failure notice
# ---------------------------------------------------------------------------

# Two distinct configuration revisions, each a 64-character lowercase digest.
CHECKOUTS_REVISION = "1" * 64
CHECKOUTS_REVISION_MOVED = "2" * 64
CHECKOUTS_PACKAGE_IDS = {
    "alpha": "123e4567-e89b-42d3-a456-426614174a01",
    "beta": "123e4567-e89b-42d3-a456-426614174a02",
    "gamma": "123e4567-e89b-42d3-a456-426614174a03",
    "delta": "123e4567-e89b-42d3-a456-426614174a04",
    "epsilon": "123e4567-e89b-42d3-a456-426614174a05",
}
# The six categories a failed checkouts request reports.
CHECKOUTS_FAILURE_CODES = [
    "checkouts_unavailable",
    "checkouts_timeout",
    "checkouts_failed",
    "checkouts_output_too_large",
    "checkouts_cancelled",
    "checkouts_protocol_error",
]


def _package_spec(root, stage, name, package_id, fields):
    """Write one fictional package document under ``<root>/Repo/<stage>/<name>``."""
    folder = root / "Repo" / stage / name
    folder.mkdir(parents=True)
    lines = [f"# {name.title()}", f"Package ID: {package_id}", "Status: approved", *fields]
    (folder / "spec.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture(scope="module")
def real_checkouts(tmp_path_factory):
    """One real workspace, its checkout documents and the catalog beside them.

    A bare ``trunk`` remote is cloned twice into one checkout folder.  A
    fictional specification root declares ``alpha`` on a pushed branch, ``beta``
    on a branch only the second clone holds, ``gamma`` in a finished stage,
    ``delta`` without a branch and ``epsilon`` on a branch nothing holds.  The
    document is captured as built, after one more commit on ``alpha``, after two
    more, and after ``alpha`` moves to the finished stage, with the catalog of
    each side of that move.  Every proof therefore reads documents the real
    collector and catalog wrote rather than a fixture imitating them, and the
    later captures are what the Code Status line proofs read.
    """
    root = tmp_path_factory.mktemp("real-checkouts")
    folder = root / "checkouts"
    folder.mkdir()
    served = root / "served"
    served.mkdir()
    (root / "home").mkdir()
    spec_root = root / "specification-root"
    with pytest.MonkeyPatch.context() as patch:
        isolate_git(patch, root / "home")
        remote = make_remote(served)
        clone_two = clone(remote.as_uri(), folder / "Repo Clone 2")
        clone_ten = clone(remote.as_uri(), folder / "Repo Clone 10")
        # topic-a and topic-c are pushed from the first clone; topic-b is
        # committed and checked out only in the second one.
        git(clone_two, "checkout", "-q", "-b", "topic-a")
        commit_file(clone_two, "alpha.txt", "alpha one\n")
        git(clone_two, "push", "-q", "-u", "origin", "topic-a")
        git(clone_ten, "checkout", "-q", "-b", "topic-b")
        commit_file(clone_ten, "beta.txt", "beta one\n")
        git(clone_two, "checkout", "-q", "-b", "topic-c", "trunk")
        commit_file(clone_two, "gamma.txt", "gamma one\n")
        git(clone_two, "push", "-q", "-u", "origin", "topic-c")
        git(clone_two, "checkout", "-q", "topic-a")

        target = [f"Target repo: {clone_two}"]
        _package_spec(spec_root, "Drafts", "alpha", CHECKOUTS_PACKAGE_IDS["alpha"],
                      ["Branch: topic-a", *target])
        _package_spec(
            spec_root, "Drafts", "beta", CHECKOUTS_PACKAGE_IDS["beta"],
            ["Branch: topic-b", *target,
             f"Completion Prerequisite: {CHECKOUTS_PACKAGE_IDS['alpha']}"],
        )
        _package_spec(spec_root, "Shipped", "gamma", CHECKOUTS_PACKAGE_IDS["gamma"],
                      ["Branch: topic-c", *target])
        _package_spec(spec_root, "Drafts", "delta", CHECKOUTS_PACKAGE_IDS["delta"], target)
        _package_spec(spec_root, "Drafts", "epsilon", CHECKOUTS_PACKAGE_IDS["epsilon"],
                      ["Branch: topic-e", *target])

        def collect():
            """One real collection of the fixture folder and package scan."""
            return collect_checkouts(
                [str(folder)],
                deadline_ns=time.monotonic_ns() + 60_000_000_000,
                package_scan=PackageScan(spec_root, ("Shipped",)),
                configuration_revision=CHECKOUTS_REVISION,
            )

        def catalog():
            """The catalog of the specification root as it stands now."""
            return scan_catalog(
                spec_root,
                completed_stage_names=["Shipped"],
                configuration_revision=CHECKOUTS_REVISION,
            )

        catalog_before = json.loads(catalog())
        rendered_zero = collect()
        commit_file(clone_two, "alpha.txt", "alpha two\n")
        rendered_one = collect()
        commit_file(clone_two, "alpha.txt", "alpha three\n")
        commit_file(clone_two, "alpha.txt", "alpha four\n")
        rendered_two = collect()
        (spec_root / "Repo" / "Drafts" / "alpha").rename(
            spec_root / "Repo" / "Shipped" / "alpha"
        )
        rendered_three = collect()
        catalog_after = json.loads(catalog())
    return SimpleNamespace(
        revision=CHECKOUTS_REVISION,
        revision_moved=CHECKOUTS_REVISION_MOVED,
        catalog=catalog_before,
        catalog_after_move=catalog_after,
        document_zero=json.loads(rendered_zero),
        document_zero_bytes=rendered_zero.encode("utf-8"),
        document_one=json.loads(rendered_one),
        document_two=json.loads(rendered_two),
        document_three=json.loads(rendered_three),
    )


def open_checkouts_page(launch, real_checkouts, double, *, client=None):
    """Open one fake-clock application page over the checkout provider double."""
    return launch(
        client or StaticClient(real_checkouts.catalog),
        settings=BrowserSettings(revision=real_checkouts.revision),
        checkouts=double,
        fake_clock=True,
        init_script=RESPONSE_RECORDER,
    )


def test_checkouts_client_requests_at_load_and_once_per_tick(open_page, real_checkouts):
    double = BrowserCheckouts(real_checkouts.document_zero)
    page = open_checkouts_page(open_page, real_checkouts, double)
    wait_for_responses(page, "checkoutsResponses", 1)
    settle_frames(page)
    assert page.evaluate("window.checkoutsResponses") == [200]
    assert double.calls == 1
    fire_checkouts_poll(page)
    assert double.calls == 2


def test_checkouts_tick_is_skipped_while_one_request_is_in_flight(open_page, real_checkouts):
    double = BrowserCheckouts(real_checkouts.document_zero)
    gate = double.hold()
    client = SequenceClient([
        real_checkouts.catalog,
        real_checkouts.catalog,
        real_checkouts.catalog,
        real_checkouts.catalog_after_move,
    ])
    page = open_checkouts_page(open_page, real_checkouts, double, client=client)
    assert double.entered.wait(timeout=10)
    assert double.calls == 1
    # Two ticks answer their own catalog request while the first checkouts
    # request is still held; neither starts another one.
    fire_poll(page)
    assert double.calls == 1
    fire_poll(page)
    assert double.calls == 1
    # The third tick answers a changed catalog, which offers an update.
    fire_poll(page)
    playwright.expect(page.locator("#refresh")).to_have_text("Apply update")
    assert double.calls == 1
    gate.set()
    wait_for_responses(page, "checkoutsResponses", 1)
    settle_frames(page)
    fire_checkouts_poll(page)
    assert double.calls == 2


def test_checkouts_client_ignores_the_catalog_buttons(open_page, real_checkouts):
    double = BrowserCheckouts(real_checkouts.document_zero)
    client = SequenceClient([
        real_checkouts.catalog,
        real_checkouts.catalog,
        real_checkouts.catalog_after_move,
    ])
    page = open_checkouts_page(open_page, real_checkouts, double, client=client)
    wait_for_responses(page, "checkoutsResponses", 1)
    settle_frames(page)
    assert "Repo/Drafts/alpha" in card_paths(page)
    assert double.calls == 1
    # Refresh view asks the catalog for another snapshot and nothing else.
    with page.expect_response("**/api/catalog") as refreshed:
        page.locator("#refresh").click()
    refreshed.value.finished()
    assert double.calls == 1
    # Applying an offered update installs that snapshot and asks for nothing.
    fire_poll(page)
    playwright.expect(page.locator("#refresh")).to_have_text("Apply update")
    wait_for_responses(page, "checkoutsResponses", 2)
    settle_frames(page)
    # The tick itself asked for checkouts; the button press adds no request.
    assert double.calls == 2
    page.locator("#refresh").click()
    playwright.expect(page.locator("#refresh")).to_have_text("Refresh view")
    settle_frames(page)
    assert "Repo/Drafts/alpha" not in card_paths(page)
    assert double.calls == 2
    assert page.evaluate("window.checkoutsResponses") == [200, 200]


def test_standalone_server_offers_no_checkouts_route(open_page, real_checkouts):
    page = open_page(
        SequenceClient([real_checkouts.catalog]),
        fake_clock=True,
        init_script=RESPONSE_RECORDER,
    )
    wait_for_responses(page, "checkoutsResponses", 1)
    settle_frames(page)
    shown = card_paths(page)
    assert shown
    fire_poll(page)
    fire_poll(page)
    wait_for_responses(page, "checkoutsResponses", 3)
    settle_frames(page)
    assert page.evaluate("window.checkoutsResponses") == [404, 404, 404]
    assert page.locator("#board-issues").count() == 0
    assert card_paths(page) == shown
    assert page.locator("#refresh").inner_text() == "Refresh view"
    assert page.locator("#refresh.pending").count() == 0
    assert page.locator("#refresh-status").inner_text() == ""


def test_checkouts_request_does_not_wait_for_the_catalog(open_page, real_checkouts):
    double = BrowserCheckouts(real_checkouts.document_zero)
    client = BlockingSequenceClient([real_checkouts.catalog] * 3, blocked_call=3)
    page = open_checkouts_page(open_page, real_checkouts, double, client=client)
    wait_for_responses(page, "checkoutsResponses", 1)
    fire_checkouts_poll(page)
    assert double.calls == 2
    # The next tick's catalog fetch is held; its checkouts request is not.
    page.clock.fast_forward(POLL_INTERVAL_MS)
    assert client.started.wait(timeout=10)
    wait_for_responses(page, "checkoutsResponses", 3)
    assert double.calls == 3
    assert not client.release.is_set()
    client.release.set()
    wait_for_responses(page, "catalogResponses", 3)


def test_checkouts_failure_notice_keeps_focus_and_the_catalog(open_page, real_checkouts):
    double = BrowserCheckouts(real_checkouts.document_zero)
    page = open_checkouts_page(open_page, real_checkouts, double)
    wait_for_responses(page, "checkoutsResponses", 1)
    settle_frames(page)
    card = page.locator(f'.card[data-package-id="{CHECKOUTS_PACKAGE_IDS["beta"]}"]')
    card.focus()
    card.press("Enter")
    kept = card.element_handle()
    observe_removed_cards(page)
    observe_refresh_status(page)
    shown = card_paths(page)
    summary = page.locator(".checkouts-issue-summary")
    for code in CHECKOUTS_FAILURE_CODES:
        double.fail(code)
        fire_checkouts_poll(page)
        playwright.expect(summary).to_have_count(1)
        playwright.expect(summary).to_have_text(f"Code Status unavailable: {code}")
        playwright.expect(page.locator("#board-issues ul li")).to_have_text(
            [f"checkouts Code Status unavailable: {code}"]
        )
        assert page.locator(".refresh-issue-summary").count() == 0, code
        assert page.locator("#refresh").inner_text() == "Refresh view", code
        assert page.locator("#refresh.pending").count() == 0, code
        assert kept.evaluate("node => node.isConnected && node === document.activeElement"), code
        assert kept.get_attribute("aria-pressed") == "true", code
        assert page.evaluate("window.removedCards") == 0, code
        assert page.evaluate("window.refreshStatusRecords") == 0, code
        assert card_paths(page) == shown, code


CHECKOUTS_ROUTED_REPLIES = [
    ("a-producer-code", {"status": 502, "body": '{"error": "producer_timeout"}'},
     "checkouts_unavailable"),
    ("an-unknown-code", {"status": 502, "body": '{"error": "surprise"}'},
     "checkouts_unavailable"),
    ("no-code", {"status": 502, "body": "{}"}, "checkouts_unavailable"),
    ("a-non-json-502", {"status": 502, "body": "not json"}, "checkouts_protocol_error"),
    ("a-non-json-200", {"status": 200, "body": "not json"}, "checkouts_protocol_error"),
    ("an-aborted-request", {"abort": True}, "checkouts_unavailable"),
]


def test_checkouts_routed_replies_map_to_their_categories(open_page, real_checkouts):
    double = BrowserCheckouts(real_checkouts.document_zero)
    page = open_checkouts_page(open_page, real_checkouts, double)
    wait_for_responses(page, "checkoutsResponses", 1)
    settle_frames(page)
    reply = {}

    def answer(route):
        if reply.get("abort"):
            route.abort()
        elif reply.get("pass"):
            route.continue_()
        else:
            route.fulfill(status=reply["status"], body=reply["body"])

    page.route("**/api/checkouts", answer)
    summary = page.locator(".checkouts-issue-summary")
    for label, body, expected in CHECKOUTS_ROUTED_REPLIES:
        reply.clear()
        reply.update(body)
        fire_poll(page)
        playwright.expect(summary).to_have_count(1)
        playwright.expect(summary).to_have_text(f"Code Status unavailable: {expected}")
        assert page.locator(".refresh-issue-summary").count() == 0, label
        # The served document clears the part again, so each case has to
        # recreate it rather than inherit the previous one.
        reply.clear()
        reply["pass"] = True
        fire_checkouts_poll(page)
        playwright.expect(summary).to_have_count(0)


def test_checkouts_categories_stay_on_their_own_surface(open_page, real_checkouts):
    double = BrowserCheckouts(real_checkouts.document_zero)
    client = SequenceClient([real_checkouts.catalog, CatalogError("checkouts_timeout")])
    page = open_checkouts_page(open_page, real_checkouts, double, client=client)
    wait_for_responses(page, "checkoutsResponses", 1)
    fire_checkouts_poll(page)
    playwright.expect(page.locator("#refresh-status")).to_have_text(
        "Latest refresh issue: producer_unavailable"
    )
    assert page.locator("#board-issues summary").inner_text() == (
        "Latest refresh issue: producer_unavailable"
    )
    assert page.locator(".checkouts-issue-summary").count() == 0


def test_checkouts_and_refresh_issues_join_and_clear_independently(open_page, real_checkouts):
    double = BrowserCheckouts(real_checkouts.document_zero)
    client = SequenceClient([
        real_checkouts.catalog,
        CatalogError("producer_unavailable"),
        real_checkouts.catalog,
    ])
    page = open_checkouts_page(open_page, real_checkouts, double, client=client)
    wait_for_responses(page, "checkoutsResponses", 1)
    double.fail("checkouts_timeout")
    double.fail("checkouts_timeout")
    fire_checkouts_poll(page)
    panel = page.locator("#board-issues")
    playwright.expect(panel.locator("summary")).to_have_text(
        "Latest refresh issue: producer_unavailable"
        " · Code Status unavailable: checkouts_timeout"
    )
    assert panel.locator(".refresh-issue-summary").inner_text() == (
        "Latest refresh issue: producer_unavailable"
    )
    playwright.expect(panel.locator("ul li")).to_have_text([
        "refresh producer_unavailable",
        "checkouts Code Status unavailable: checkouts_timeout",
    ])
    panel.locator("summary").click()
    assert panel.get_attribute("open") is not None
    kept = panel.element_handle()
    # The catalog recovers: its part goes and the checkouts part stays.
    fire_checkouts_poll(page)
    playwright.expect(panel.locator(".checkouts-issue-summary")).to_have_text(
        "Code Status unavailable: checkouts_timeout"
    )
    assert page.locator(".refresh-issue-summary").count() == 0
    assert kept.evaluate("node => node.isConnected")
    assert panel.get_attribute("open") is not None
    playwright.expect(panel.locator("ul li")).to_have_text([
        "checkouts Code Status unavailable: checkouts_timeout",
    ])
    # The checkouts recover too: with nothing left, the panel goes.
    fire_checkouts_poll(page)
    playwright.expect(page.locator("#board-issues")).to_have_count(0)


def checkouts_document_mutations():
    """Each document mutation the browser must refuse as a protocol failure.

    ``reseal`` recomputes the digest, so what the browser refuses is the
    mutation rather than a digest that mutation invalidated.
    """
    def previous_schema(document):
        document["schema_version"] = models.CHECKOUTS_SCHEMA_VERSION - 1

    def extra_top_level_key(document):
        document["unexpected"] = 1

    def missing_top_level_key(document):
        del document["diagnostics"]

    def missing_revision(document):
        del document["configuration_revision"]

    def text_revision(document):
        document["configuration_revision"] = "revision-1"

    def upper_case_digest(document):
        document["checkouts_digest"] = "A" * 64

    def changed_after_sealing(document):
        document["packages"][0]["finished"] = True

    def packages_object(document):
        document["packages"] = {}

    def extra_package_key(document):
        document["packages"][0]["unexpected"] = 1

    def missing_package_key(document):
        del document["packages"][0]["branch"]

    def absolute_package_path(document):
        document["packages"][0]["package_path"] = "/Repo/Drafts/alpha"

    def parent_package_path(document):
        document["packages"][0]["package_path"] = "Repo/Drafts/../alpha"

    def duplicate_package_path(document):
        document["packages"][1]["package_path"] = document["packages"][0]["package_path"]

    def text_code_status(document):
        document["packages"][0]["code_status"] = "in progress"

    def extra_code_status_key(document):
        document["packages"][0]["code_status"]["unexpected"] = 1

    def control_code_status_text(document):
        document["packages"][0]["code_status"]["text"] = "in progress\u0007"

    def non_string_code_status_text(document):
        document["packages"][0]["code_status"]["text"] = 7

    return [
        {"id": "previous-schema", "mutate": previous_schema, "reseal": True},
        {"id": "extra-top-level-key", "mutate": extra_top_level_key, "reseal": True},
        {"id": "missing-top-level-key", "mutate": missing_top_level_key, "reseal": True},
        {"id": "missing-revision", "mutate": missing_revision, "reseal": True},
        {"id": "text-revision", "mutate": text_revision, "reseal": True},
        {"id": "upper-case-digest", "mutate": upper_case_digest, "reseal": False},
        {"id": "changed-after-sealing", "mutate": changed_after_sealing, "reseal": False},
        {"id": "packages-object", "mutate": packages_object, "reseal": True},
        {"id": "extra-package-key", "mutate": extra_package_key, "reseal": True},
        {"id": "missing-package-key", "mutate": missing_package_key, "reseal": True},
        {"id": "absolute-package-path", "mutate": absolute_package_path, "reseal": True},
        {"id": "parent-package-path", "mutate": parent_package_path, "reseal": True},
        {"id": "duplicate-package-path", "mutate": duplicate_package_path, "reseal": True},
        {"id": "text-code-status", "mutate": text_code_status, "reseal": True},
        {"id": "extra-code-status-key", "mutate": extra_code_status_key, "reseal": True},
        {"id": "control-code-status-text", "mutate": control_code_status_text,
         "reseal": True},
        {"id": "non-string-code-status-text", "mutate": non_string_code_status_text,
         "reseal": True},
    ]


def checkouts_envelope_mutations():
    """Each envelope mutation the browser must refuse as a protocol failure.

    ``document`` marks the mutations that also leave the served document itself
    one the collector's parser refuses.
    """

    def extra_envelope_key(envelope):
        envelope["unexpected"] = None

    def no_document(envelope):
        envelope["checkouts"] = None

    return [
        {"id": "extra-envelope-key", "mutate": extra_envelope_key, "document": False},
        {"id": "no-document", "mutate": no_document, "document": True},
    ]


def test_checkouts_validator_refuses_each_mutation(open_page, real_checkouts):
    """Every malformed envelope shows the protocol failure and nothing else.

    One page alternates each case with the unchanged document, so every case
    must create the notice itself and that document must clear it again.
    """
    double = BrowserCheckouts(real_checkouts.document_zero)
    page = open_checkouts_page(open_page, real_checkouts, double)
    wait_for_responses(page, "checkoutsResponses", 1)
    settle_frames(page)
    summary = page.locator(".checkouts-issue-summary")
    assert summary.count() == 0
    card = page.locator(f'.card[data-package-id="{CHECKOUTS_PACKAGE_IDS["alpha"]}"]')
    card.click()
    kept = card.element_handle()
    shown = card_paths(page)
    for case in checkouts_document_mutations():
        envelope = checkouts_envelope(copy.deepcopy(real_checkouts.document_zero))
        case["mutate"](envelope["checkouts"])
        if case["reseal"]:
            redigest(envelope["checkouts"])
        # The collector's parser refuses every one of these documents, so the
        # browser can meet none of them from a server that served a valid one.
        with pytest.raises(models.ProtocolError):
            models.parse_checkouts(copy.deepcopy(envelope["checkouts"]))
        double.serve(envelope)
        fire_checkouts_poll(page)
        playwright.expect(summary).to_have_count(1)
        playwright.expect(summary).to_have_text(
            "Code Status unavailable: checkouts_protocol_error"
        )
        # Only the notice changes: the catalog, its selection and the button stay.
        assert summary.count() == 1, case["id"]
        assert page.locator("#refresh").inner_text() == "Refresh view", case["id"]
        assert page.locator("#refresh.pending").count() == 0, case["id"]
        assert card_paths(page) == shown, case["id"]
        assert kept.evaluate("node => node.isConnected && node === document.activeElement"), (
            case["id"]
        )
        # The unchanged document clears the part before the next case.
        fire_checkouts_poll(page)
        playwright.expect(summary).to_have_count(0)
    # A changed observation instant is outside the digest, so it is accepted.
    envelope = checkouts_envelope(copy.deepcopy(real_checkouts.document_zero))
    envelope["checkouts"]["observed_at"] = "2020-01-01T00:00:00Z"
    models.parse_checkouts(copy.deepcopy(envelope["checkouts"]))
    double.serve(envelope)
    fire_checkouts_poll(page)
    playwright.expect(summary).to_have_count(0)
    for case in checkouts_envelope_mutations():
        envelope = checkouts_envelope(copy.deepcopy(real_checkouts.document_zero))
        case["mutate"](envelope)
        if case["document"]:
            with pytest.raises(models.ProtocolError):
                models.parse_checkouts(copy.deepcopy(envelope["checkouts"]))
        double.serve(envelope)
        fire_checkouts_poll(page)
        playwright.expect(summary).to_have_text(
            "Code Status unavailable: checkouts_protocol_error"
        )
        assert summary.count() == 1, case["id"]
        fire_checkouts_poll(page)
        playwright.expect(summary).to_have_count(0)


def test_checkouts_real_runtime_serves_the_collected_document(open_page, real_checkouts):
    workers = CheckoutsWorkerStandIn(real_checkouts.document_zero_bytes)
    page = open_page(
        StaticClient(real_checkouts.catalog),
        settings=BrowserSettings(revision=real_checkouts.revision),
        workers=workers,
        fake_clock=True,
        init_script=RESPONSE_RECORDER,
    )
    wait_for_responses(page, "checkoutsResponses", 1)
    fire_checkouts_poll(page)
    settle_frames(page)
    # The real provider accepted the collected document, so the envelope keys
    # and the digest omission agree with the collector's.
    assert page.evaluate("window.checkoutsResponses") == [200, 200]
    assert workers.fetch_calls >= 1
    assert page.locator("#board-issues").count() == 0
    assert page.locator(".checkouts-issue-summary").count() == 0


# ---------------------------------------------------------------------------
# The card's Code Status line, its in-place updates and its documentation
# ---------------------------------------------------------------------------

# The Code Status text the captured D0 document holds for each package that has
# one; delta and epsilon have none.  Stating them here makes a collector change
# fail loudly rather than quietly redefining what the line must show.
CHECKOUTS_CODE_STATUS_TEXTS = {
    "Repo/Drafts/alpha": "in progress · pushed",
    "Repo/Drafts/beta": "in progress · push state unknown · in Repo Clone 10",
    "Repo/Shipped/gamma": "awaiting merge (no PR)",
}
CHECKOUTS_CARD_PATHS = {
    "Repo/Drafts/alpha", "Repo/Drafts/beta", "Repo/Drafts/delta",
    "Repo/Drafts/epsilon", "Repo/Shipped/gamma",
}


def package_text(document, path):
    """The Code Status text one package of a captured document holds."""
    package = next(item for item in document["packages"] if item["package_path"] == path)
    assert package["code_status"] is not None, path
    return package["code_status"]["text"]


def checkouts_with_text(document, path, value):
    """The envelope of one captured document with one package's text replaced.

    The replacement is re-sealed, so the browser receives a valid document and
    must show the served text rather than any text it could rebuild.
    """
    changed = copy.deepcopy(document)
    package = next(item for item in changed["packages"] if item["package_path"] == path)
    package["code_status"]["text"] = value
    return checkouts_envelope(redigest(changed))


def checkouts_with_revision(document, revision):
    """The envelope of one captured document re-sealed under another revision."""
    changed = copy.deepcopy(document)
    changed["configuration_revision"] = revision
    return checkouts_envelope(redigest(changed))


def code_status_card(page, path):
    """The card of one package path."""
    return page.locator(f'.card[data-package-path="{path}"]')


def code_status_line(page, path):
    """One card's single Code Status line, or None when it holds none."""
    lines = code_status_card(page, path).locator(".card-code-status")
    return lines.inner_text() if lines.count() else None


def card_code_status_structure(page):
    """Each card's line count and the class names of the elements around its line.

    A line a full render writes and a line an update inserts in place must agree
    on both neighbours, which is where Filepath and the start text sit.
    """
    return page.locator(".card[data-package-path]").evaluate_all("""cards => cards.map(card => ({
      path: card.dataset.packagePath,
      tag: card.tagName,
      lines: card.querySelectorAll('.card-code-status').length,
      previous: card.querySelector('.card-code-status')?.previousElementSibling?.className,
      next: card.querySelector('.card-code-status')?.nextElementSibling?.className,
    }))""")


def visible_card_paths(page, query):
    """The package paths a search leaves visible."""
    page.fill("#filter", query)
    return page.locator(".card:not([hidden])").evaluate_all(
        "cards => cards.map(card => card.dataset.packagePath)"
    )


def open_code_status_page(launch, real_checkouts, double, *, client=None):
    """Open the fixture board, settle its first scan and show every stage row."""
    page = open_checkouts_page(launch, real_checkouts, double, client=client)
    wait_for_responses(page, "checkoutsResponses", 1)
    settle_frames(page)
    # Unchecking a display preference is a full render, so every finished-stage
    # card is on the board before a proof reads its line.
    page.get_by_label("Hide terminal rows", exact=True).uncheck()
    return page


def test_code_status_line_shows_the_served_document(open_page, real_checkouts):
    """Each card shows the served text as one line after Filepath, and search reads it."""
    double = BrowserCheckouts(real_checkouts.document_zero)
    page = open_code_status_page(open_page, real_checkouts, double)
    assert set(card_paths(page)) == CHECKOUTS_CARD_PATHS
    assert {
        path: package_text(real_checkouts.document_zero, path)
        for path in CHECKOUTS_CODE_STATUS_TEXTS
    } == CHECKOUTS_CODE_STATUS_TEXTS
    for path, expected in CHECKOUTS_CODE_STATUS_TEXTS.items():
        assert code_status_line(page, path) == f"Code Status: {expected}", path
    # A package whose Code Status is null gets no line and no placeholder.
    for path in ("Repo/Drafts/delta", "Repo/Drafts/epsilon"):
        assert code_status_card(page, path).locator(".card-code-status").count() == 0, path
        assert "Code Status" not in code_status_card(page, path).inner_text(), path
    # The line follows Filepath inside the card's button and precedes its start
    # text, exactly where Filepath and Target Folder are.
    structure = card_code_status_structure(page)
    assert len(structure) == len(CHECKOUTS_CARD_PATHS)
    for card in structure:
        assert card["tag"] == "BUTTON", card
        if card["path"] in CHECKOUTS_CODE_STATUS_TEXTS:
            assert card["lines"] == 1, card
            assert card["previous"] == "card-filepath", card
            assert card["next"] == "visually-hidden card-start-text", card
        else:
            assert card["lines"] == 0, card
    # The button's accessible name carries the line, as its other lines do.
    alpha = code_status_card(page, "Repo/Drafts/alpha")
    playwright.expect(alpha).to_have_accessible_name(
        re.compile(re.escape(f"Code Status: {CHECKOUTS_CODE_STATUS_TEXTS['Repo/Drafts/alpha']}"))
    )
    # Nothing announces a Code Status: neither the line nor an ancestor is live.
    assert page.locator(".card-code-status").evaluate_all(
        "lines => lines.filter(line => line.closest("
        "'[aria-live], [role=\"status\"], [role=\"alert\"]')).length"
    ) == 0
    # Search reads the line's text, which no other card's text holds.
    page.fill("#filter", "")
    assert visible_card_paths(page, "awaiting merge") == ["Repo/Shipped/gamma"]
    assert visible_card_paths(page, "push state unknown") == ["Repo/Drafts/beta"]
    assert visible_card_paths(page, "clone 10") == ["Repo/Drafts/beta"]
    page.fill("#filter", "")


@pytest.mark.parametrize("theme,card_color", [("light", LIGHT_CARD), ("dark", DARK_CARD)])
def test_code_status_line_is_readable_on_every_card_surface(
    open_page, real_checkouts, theme, card_color
):
    """The line keeps 4.5:1 contrast on the teal and the neutral card, in both themes."""
    double = BrowserCheckouts(real_checkouts.document_zero)
    page = open_code_status_page(open_page, real_checkouts, double)
    page.get_by_label("Theme", exact=True).select_option(theme)
    alpha = code_status_card(page, "Repo/Drafts/alpha")
    beta = code_status_card(page, "Repo/Drafts/beta")
    assert alpha.evaluate("card => getComputedStyle(card).backgroundColor") == NO_INCOMING_CARD
    assert beta.evaluate("card => getComputedStyle(card).backgroundColor") == card_color
    for card in (alpha, beta):
        colors = card.evaluate("""card => ({
          text: getComputedStyle(card.querySelector('.card-code-status')).color,
          background: getComputedStyle(card).backgroundColor,
        })""")
        assert contrast_ratio(colors["text"], colors["background"]) >= 4.5, (theme, colors)


def test_code_status_line_shows_the_text_it_is_served(open_page, real_checkouts):
    """A re-sealed text is displayed verbatim, so nothing rebuilds it in the browser."""
    double = BrowserCheckouts(real_checkouts.document_zero)
    page = open_code_status_page(open_page, real_checkouts, double)
    double.serve(checkouts_with_text(real_checkouts.document_zero, "Repo/Drafts/alpha", "custom words"))
    fire_checkouts_poll(page)
    assert code_status_line(page, "Repo/Drafts/alpha") == "Code Status: custom words"
    assert visible_card_paths(page, "custom words") == ["Repo/Drafts/alpha"]
    page.fill("#filter", "")


def test_code_status_long_checkout_name_wraps_inside_the_card(open_page, real_checkouts):
    """A 300-character unbroken text breaks inside the column instead of overflowing it."""
    token = "x" * 300
    double = BrowserCheckouts(real_checkouts.document_zero)
    page = open_code_status_page(open_page, real_checkouts, double)
    double.serve(checkouts_with_text(real_checkouts.document_zero, "Repo/Drafts/alpha", token))
    fire_checkouts_poll(page)
    alpha = code_status_card(page, "Repo/Drafts/alpha")
    assert alpha.locator(".card-code-status").inner_text() == f"Code Status: {token}"
    line = alpha.locator(".card-code-status").evaluate(
        "line => ({scrollWidth: line.scrollWidth, clientWidth: line.clientWidth})"
    )
    assert line["scrollWidth"] <= line["clientWidth"] + 1, line
    bounds = alpha.evaluate("""card => ({
      right: card.getBoundingClientRect().right,
      cellRight: card.closest('.cell').getBoundingClientRect().right,
    })""")
    assert bounds["right"] <= bounds["cellRight"] + 1, bounds


def test_code_status_line_comes_from_the_real_runtime(open_page, real_checkouts):
    """The admitted runtime's own get_checkouts serves a document the card renders."""
    workers = CheckoutsWorkerStandIn(real_checkouts.document_zero_bytes)
    page = open_page(
        StaticClient(real_checkouts.catalog),
        settings=BrowserSettings(revision=real_checkouts.revision),
        workers=workers,
        fake_clock=True,
        init_script=RESPONSE_RECORDER,
    )
    wait_for_responses(page, "checkoutsResponses", 1)
    settle_frames(page)
    page.get_by_label("Hide terminal rows", exact=True).uncheck()
    assert workers.fetch_calls >= 1
    for path, expected in CHECKOUTS_CODE_STATUS_TEXTS.items():
        assert code_status_line(page, path) == f"Code Status: {expected}", path


def test_checkouts_tick_rewrites_the_line_in_place(open_page, real_checkouts):
    """One tick rewrites one card's line on the same elements and keeps the board."""
    double = BrowserCheckouts(real_checkouts.document_zero)
    page = open_code_status_page(open_page, real_checkouts, double)
    assert page.locator(".rail-layer").count() == 1
    alpha = code_status_card(page, "Repo/Drafts/alpha")
    alpha.click()
    playwright.expect(alpha).to_have_attribute("aria-pressed", "true")
    alpha.focus()
    page.set_viewport_size({"width": 900, "height": 320})
    page.evaluate("() => window.scrollTo(0, 120)")
    assert page.evaluate("window.scrollY") > 0
    # Let the resize-driven redraw settle before the tick's own redraw is
    # observed, so the layer that goes away is the one the tick replaced.
    settle_frames(page)
    assert page.locator(
        f'.connection[data-source="{CHECKOUTS_PACKAGE_IDS["alpha"]}"]'
        f'[data-dependent="{CHECKOUTS_PACKAGE_IDS["beta"]}"]'
    ).count() == 1
    rail_layer = page.locator(".rail-layer").element_handle()
    card = alpha.element_handle()
    line = alpha.locator(".card-code-status").element_handle()
    height = page.locator("#board").evaluate("board => board.getBoundingClientRect().height")
    offset = page.evaluate("window.scrollY")
    observe_removed_cards(page)
    arm_focus_sampler(page)
    observe_refresh_status(page)
    double.serve(checkouts_envelope(real_checkouts.document_one))
    fire_checkouts_poll(page)
    settle_frames(page)
    # The same card and the same line element now hold the next scan's text.
    assert line.evaluate("node => node.isConnected && node.textContent") == (
        "Code Status: in progress · 1 unpushed"
    )
    assert card.evaluate(
        "node => node.isConnected && "
        "node === document.querySelector('.card[data-package-path=\"Repo/Drafts/alpha\"]')"
    )
    assert page.evaluate("window.removedCards") == 0
    assert card.evaluate("node => node === document.activeElement")
    assert card.get_attribute("aria-pressed") == "true"
    assert_focus_sampler_clean(page)
    assert page.locator("#refresh").inner_text() == "Refresh view"
    assert page.locator("#refresh.pending").count() == 0
    assert page.evaluate("window.scrollY") == offset
    assert page.evaluate("window.refreshStatusRecords") == 0
    # The filter pass redrew the rails: the old layer is gone and a new one holds
    # the same pair, while the board's height did not change.
    assert not rail_layer.evaluate("node => node.isConnected")
    assert page.locator(".rail-layer").count() == 1
    assert page.locator(
        f'.connection[data-source="{CHECKOUTS_PACKAGE_IDS["alpha"]}"]'
        f'[data-dependent="{CHECKOUTS_PACKAGE_IDS["beta"]}"]'
    ).count() == 1
    assert page.locator("#board").evaluate(
        "board => board.getBoundingClientRect().height"
    ) == height
    # A filtered card leaves the board as soon as its new text stops matching,
    # and its empty cell says so.
    assert visible_card_paths(page, "1 unpushed") == ["Repo/Drafts/alpha"]
    double.serve(checkouts_envelope(real_checkouts.document_two))
    fire_checkouts_poll(page)
    playwright.expect(alpha).to_be_hidden()
    assert alpha.evaluate("node => node.closest('.cell').classList.contains('filtered-empty')")
    page.fill("#filter", "")
    assert code_status_line(page, "Repo/Drafts/alpha") == (
        "Code Status: in progress · 3 unpushed"
    )
    # A full render keeps the line and the same searchable text.
    page.get_by_label("Hide empty rows and columns", exact=True).uncheck()
    assert code_status_line(page, "Repo/Drafts/alpha") == (
        "Code Status: in progress · 3 unpushed"
    )
    assert visible_card_paths(page, "3 unpushed") == ["Repo/Drafts/alpha"]
    page.fill("#filter", "")


def test_checkouts_failure_hides_every_code_status_line(open_page, real_checkouts):
    """A failed read removes the lines and their text from search, and a scan restores them."""
    double = BrowserCheckouts(real_checkouts.document_zero)
    page = open_code_status_page(open_page, real_checkouts, double)
    assert page.locator(".card-code-status").count() == len(CHECKOUTS_CODE_STATUS_TEXTS)
    double.fail("checkouts_failed")
    fire_checkouts_poll(page)
    playwright.expect(page.locator(".card-code-status")).to_have_count(0)
    assert visible_card_paths(page, "in progress") == []
    page.fill("#filter", "")
    playwright.expect(page.locator(".checkouts-issue-summary")).to_have_text(
        "Code Status unavailable: checkouts_failed"
    )
    double.serve(checkouts_envelope(real_checkouts.document_zero))
    fire_checkouts_poll(page)
    for path, expected in CHECKOUTS_CODE_STATUS_TEXTS.items():
        assert code_status_line(page, path) == f"Code Status: {expected}", path
    # This recovery inserted each line into a card the board already held, so
    # the in-place insert must put it exactly where a rendered card puts it:
    # directly after Filepath and before the start text.
    for card in card_code_status_structure(page):
        if card["path"] in CHECKOUTS_CODE_STATUS_TEXTS:
            assert card["previous"] == "card-filepath", card
            assert card["next"] == "visually-hidden card-start-text", card
    assert page.locator(".checkouts-issue-summary").count() == 0


def test_apply_update_hides_the_code_status_until_a_matching_scan(open_page, real_checkouts):
    """A saved board revision keeps the displayed lines, and applying it hides them."""
    double = BrowserCheckouts(real_checkouts.document_zero)
    settings = BrowserSettings(revision=real_checkouts.revision)
    page = open_page(
        StaticClient(real_checkouts.catalog),
        settings=settings,
        checkouts=double,
        fake_clock=True,
        init_script=RESPONSE_RECORDER,
    )
    wait_for_responses(page, "checkoutsResponses", 1)
    settle_frames(page)
    page.get_by_label("Hide terminal rows", exact=True).uncheck()
    assert page.locator(".card-code-status").count() == len(CHECKOUTS_CODE_STATUS_TEXTS)
    settings.revision = real_checkouts.revision_moved
    fire_checkouts_poll(page)
    playwright.expect(page.locator("#refresh")).to_have_text("Apply update")
    # The displayed catalog is still the old revision, so its lines still match.
    for path, expected in CHECKOUTS_CODE_STATUS_TEXTS.items():
        assert code_status_line(page, path) == f"Code Status: {expected}", path
    page.locator("#refresh").click()
    playwright.expect(page.locator("#refresh")).to_have_text("Refresh view")
    settle_frames(page)
    assert page.locator(".card-code-status").count() == 0
    assert page.locator(".checkouts-issue-summary").count() == 0
    assert page.locator("#board-issues").count() == 0
    double.serve(
        checkouts_with_revision(real_checkouts.document_zero, real_checkouts.revision_moved)
    )
    fire_checkouts_poll(page)
    for path, expected in CHECKOUTS_CODE_STATUS_TEXTS.items():
        assert code_status_line(page, path) == f"Code Status: {expected}", path


def test_null_configuration_revision_never_shows_a_code_status_line(open_page, real_checkouts):
    """Null equals null is not a match, and no line or notice appears."""
    catalog = copy.deepcopy(real_checkouts.catalog)
    catalog["configuration_revision"] = None
    _reseal(catalog)
    document = copy.deepcopy(real_checkouts.document_zero)
    document["configuration_revision"] = None
    envelope = checkouts_envelope(redigest(document))
    page = open_page(StaticClient(catalog), fake_clock=True, init_script=RESPONSE_RECORDER)
    # The standalone server offers no checkouts route, so the page's own tick is
    # answered with the revision-less document through the browser's router.
    page.route(
        "**/api/checkouts",
        lambda route: route.fulfill(status=200, body=json.dumps(envelope)),
    )
    fire_poll(page)
    wait_for_responses(page, "checkoutsResponses", 2)
    settle_frames(page)
    assert page.evaluate("window.checkoutsResponses") == [404, 200]
    page.get_by_label("Hide terminal rows", exact=True).uncheck()
    assert set(card_paths(page)) == CHECKOUTS_CARD_PATHS
    assert page.locator(".card-code-status").count() == 0
    assert page.locator("#board-issues").count() == 0
    assert page.locator(".checkouts-issue-summary").count() == 0


def test_code_status_waits_for_the_card_the_scan_names(open_page, real_checkouts):
    """A moved package has no line until its card is displayed, and then gets one."""
    double = BrowserCheckouts(real_checkouts.document_zero)
    client = SequenceClient([real_checkouts.catalog, real_checkouts.catalog_after_move])
    page = open_code_status_page(open_page, real_checkouts, double, client=client)
    assert "Repo/Drafts/alpha" in card_paths(page)
    double.serve(checkouts_envelope(real_checkouts.document_three))
    fire_checkouts_poll(page)
    playwright.expect(page.locator("#refresh")).to_have_text("Apply update")
    # The displayed card's path is absent from the served scan, so it has no
    # line; the other packages keep theirs.
    assert code_status_card(page, "Repo/Drafts/alpha").locator(".card-code-status").count() == 0
    for path in ("Repo/Drafts/beta", "Repo/Shipped/gamma"):
        assert code_status_line(page, path) == (
            f"Code Status: {package_text(real_checkouts.document_three, path)}"
        ), path
    page.locator("#refresh").click()
    playwright.expect(page.locator("#refresh")).to_have_text("Refresh view")
    assert "Repo/Shipped/alpha" in card_paths(page)
    assert code_status_line(page, "Repo/Shipped/alpha") == (
        f"Code Status: {package_text(real_checkouts.document_three, 'Repo/Shipped/alpha')}"
    )


# ---------------------------------------------------------------------------
# The checkouts view: view switch, status line, remote checks and panels
# ---------------------------------------------------------------------------

CHECKOUTS_VIEW_PLACEHOLDER = "Find a checkout, branch, or work item…"
BOARD_SEARCH_PLACEHOLDER = "Find a work item, project, or program…"


def checkouts_local_time(value, reference=None, *, seconds=True):
    """The view's local label for one UTC timestamp."""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    label = moment.strftime("%H:%M:%S" if seconds else "%H:%M")
    if reference is not None:
        observed = datetime.fromisoformat(reference.replace("Z", "+00:00")).astimezone()
        if moment.date() != observed.date():
            label = f"{moment:%Y-%m-%d} {label}"
    return label


def checkouts_remote_envelope(document, *, state="idle", next_due_at=None, last=None):
    """One checkout envelope with the remote-check schedule a test states."""
    envelope = checkouts_envelope(copy.deepcopy(document))
    envelope["remote_checks"] = {
        "state": state,
        "interval_seconds": REMOTE_CHECK_INTERVAL,
        "next_due_at": next_due_at,
        "last": last,
    }
    return envelope


def checkouts_remote_run(*, finished_at, outcome="completed"):
    """One finished remote check of the shape the real runtime records."""
    return {
        "started_at": "2026-11-09T11:59:50Z",
        "finished_at": finished_at,
        "outcome": outcome,
        "repositories": [],
    }


def show_checkouts_view(page):
    """Press the Checkouts switch and wait for the two views to swap."""
    page.locator("#view-checkouts").click()
    playwright.expect(page.locator("#checkouts")).to_be_visible()
    playwright.expect(page.locator("#board")).to_be_hidden()


def checkouts_view_page(launch, real_checkouts, double, *, client=None):
    """Open one fake-clock application page and show the checkouts view."""
    page = open_checkouts_page(launch, real_checkouts, double, client=client)
    wait_for_responses(page, "checkoutsResponses", 1)
    settle_frames(page)
    show_checkouts_view(page)
    return page


def test_checkouts_view_switch_remembers_the_view_and_keeps_preferences(
    open_page, real_checkouts
):
    """One view at a time, focus on the pressed switch, and the view is remembered."""
    double = BrowserCheckouts(real_checkouts.document_zero)
    page = open_checkouts_page(open_page, real_checkouts, double)
    wait_for_responses(page, "checkoutsResponses", 1)
    settle_frames(page)
    board_switch = page.locator("#view-board")
    checkouts_switch = page.locator("#view-checkouts")
    assert board_switch.get_attribute("aria-pressed") == "true"
    assert checkouts_switch.get_attribute("aria-pressed") == "false"
    assert page.locator("#checkouts").evaluate(
        "view => getComputedStyle(view).display") == "none"
    assert page.locator("#board").is_visible()
    assert page.get_by_label("Hide terminal rows", exact=True).is_visible()
    assert page.get_by_label("Hide empty rows and columns", exact=True).is_visible()
    assert page.get_by_label("Hide quiet checkouts", exact=True).is_hidden()
    assert page.locator("#filter").get_attribute("placeholder") == BOARD_SEARCH_PLACEHOLDER
    # The existing preference keys keep their unset defaults; the view choice is
    # its own key.
    assert page.evaluate(
        "() => ['spec-tracker-compact-view', 'spec-tracker-hide-terminal-rows',"
        " 'spec-tracker-deselect-on-empty-click', 'spec-tracker-theme']"
        ".map(key => localStorage.getItem(key))"
    ) == [None, None, None, None]
    assert page.evaluate("localStorage.getItem('spec-tracker-view')") is None

    checkouts_switch.click()
    playwright.expect(page.locator("#checkouts")).to_be_visible()
    playwright.expect(page.locator("#board")).to_be_hidden()
    assert board_switch.get_attribute("aria-pressed") == "false"
    assert checkouts_switch.get_attribute("aria-pressed") == "true"
    assert page.evaluate("document.activeElement.id") == "view-checkouts"
    assert page.get_by_label("Hide terminal rows", exact=True).is_hidden()
    assert page.get_by_label("Hide empty rows and columns", exact=True).is_hidden()
    quiet = page.get_by_label("Hide quiet checkouts", exact=True)
    assert quiet.is_visible()
    assert quiet.is_checked()
    assert page.locator("#filter").get_attribute("placeholder") == CHECKOUTS_VIEW_PLACEHOLDER
    assert page.evaluate("localStorage.getItem('spec-tracker-view')") == "checkouts"
    # The checkouts preference is its own key and does not disturb the others.
    assert page.evaluate(
        "() => ['spec-tracker-compact-view', 'spec-tracker-hide-terminal-rows',"
        " 'spec-tracker-deselect-on-empty-click', 'spec-tracker-theme']"
        ".map(key => localStorage.getItem(key))"
    ) == [None, None, None, None]
    quiet.uncheck()
    assert page.evaluate("localStorage.getItem('spec-tracker-hide-quiet-checkouts')") == "false"

    # A reload starts in the remembered view.
    page.reload()
    page.wait_for_function("() => document.querySelectorAll('#board .card').length > 0")
    playwright.expect(page.locator("#checkouts")).to_be_visible()
    playwright.expect(page.locator("#board")).to_be_hidden()
    assert page.locator("#view-checkouts").get_attribute("aria-pressed") == "true"
    assert not page.get_by_label("Hide quiet checkouts", exact=True).is_checked()

    page.locator("#view-board").click()
    playwright.expect(page.locator("#board")).to_be_visible()
    playwright.expect(page.locator("#checkouts")).to_be_hidden()
    assert page.locator("#view-board").get_attribute("aria-pressed") == "true"
    assert page.evaluate("localStorage.getItem('spec-tracker-view')") == "board"
    assert page.get_by_label("Hide terminal rows", exact=True).is_visible()
    assert page.get_by_label("Hide quiet checkouts", exact=True).is_hidden()
    assert page.locator("#filter").get_attribute("placeholder") == BOARD_SEARCH_PLACEHOLDER


@pytest.mark.parametrize(
    "init_script,expected",
    [
        ("localStorage.setItem('spec-tracker-view', 'checkouts');", "checkouts"),
        ("localStorage.setItem('spec-tracker-view', 'somewhere-else');", "board"),
        (
            "Object.defineProperty(window, 'localStorage', {"
            "get() { throw new DOMException('Blocked', 'SecurityError'); }});",
            "board",
        ),
    ],
    ids=["stored-checkouts", "unknown-value", "blocked-storage"],
)
def test_checkouts_view_starts_in_board_without_a_known_value(
    open_page, real_checkouts, init_script, expected
):
    """A fresh browser, an unknown value and blocked storage fall back to Board."""
    page = open_page(
        StaticClient(real_checkouts.catalog),
        fake_clock=True,
        init_script=init_script + RESPONSE_RECORDER,
    )
    wait_for_responses(page, "checkoutsResponses", 1)
    settle_frames(page)
    if expected == "checkouts":
        playwright.expect(page.locator("#checkouts")).to_be_visible()
        assert page.locator("#view-checkouts").get_attribute("aria-pressed") == "true"
    else:
        playwright.expect(page.locator("#board")).to_be_visible()
        assert page.locator("#view-board").get_attribute("aria-pressed") == "true"
        assert page.locator("#checkouts").evaluate(
            "view => getComputedStyle(view).display") == "none"
    # Switching works in every case, including with storage unavailable.
    page.locator("#view-checkouts").click()
    playwright.expect(page.locator("#checkouts")).to_be_visible()
    playwright.expect(page.locator("#board")).to_be_hidden()
    assert page.evaluate("document.activeElement.id") == "view-checkouts"
    page.locator("#view-board").click()
    playwright.expect(page.locator("#board")).to_be_visible()
    playwright.expect(page.locator("#checkouts")).to_be_hidden()


def test_checkouts_view_keeps_a_waiting_catalog_update_for_the_board(
    open_page, real_checkouts
):
    """A tick while Checkouts is shown offers the change to the board, not here."""
    changed = copy.deepcopy(real_checkouts.catalog)
    title = next(
        entry for entry in changed["entries"] if entry["package_path"] == "Repo/Drafts/alpha"
    )
    title["declared"]["title"] = "Alpha renamed"
    _reseal(changed)
    double = BrowserCheckouts(real_checkouts.document_zero)
    client = SequenceClient([real_checkouts.catalog, changed])
    page = checkouts_view_page(open_page, real_checkouts, double, client=client)
    fire_checkouts_poll(page)
    # The board's update is offered on the board, never applied from here.
    playwright.expect(page.locator("#refresh")).to_have_text("Refresh view")
    playwright.expect(page.locator("#view-board")).to_have_text("Board (update waiting)")
    catalogs = page.evaluate("window.catalogResponses.length")
    checkouts = page.evaluate("window.checkoutsResponses.length")
    with page.expect_response("**/api/checkouts") as response:
        page.locator("#refresh").click()
    response.value.finished()
    assert page.evaluate("window.checkoutsResponses.length") == checkouts + 1
    assert page.evaluate("window.catalogResponses.length") == catalogs
    assert "Alpha renamed" not in page.locator(".card-title").all_inner_texts()
    # The waiting update is still waiting when the board is shown again.
    page.locator("#view-board").click()
    playwright.expect(page.locator("#refresh")).to_have_text("Apply update")
    assert page.locator("#refresh.pending").count() == 1
    assert page.locator("#view-board").inner_text() == "Board"
    page.locator("#refresh").click()
    playwright.expect(page.locator("#refresh")).to_have_text("Refresh view")
    assert "Alpha renamed" in page.locator(".card-title").all_inner_texts()


def test_checkouts_view_search_carries_back_to_the_board(open_page, real_checkouts):
    """The one search box keeps its text across the switch and its rails redraw."""
    double = BrowserCheckouts(real_checkouts.document_zero)
    page = checkouts_view_page(open_page, real_checkouts, double)
    page.fill("#filter", "alpha")
    assert page.locator("#filter").input_value() == "alpha"
    page.locator("#view-board").click()
    playwright.expect(page.locator("#board")).to_be_visible()
    assert page.locator(".card:not([hidden])").evaluate_all(
        "cards => cards.map(card => card.dataset.packagePath)") == ["Repo/Drafts/alpha"]
    # With an empty query the board's dependent pair is drawn again after the
    # switch back, and each rail meets its two cards.
    page.locator("#view-checkouts").click()
    page.fill("#filter", "")
    page.locator("#view-board").click()
    playwright.expect(page.locator("#board")).to_be_visible()
    settle_frames(page)
    assert page.locator(
        f'.connection[data-source="{CHECKOUTS_PACKAGE_IDS["alpha"]}"]'
        f'[data-dependent="{CHECKOUTS_PACKAGE_IDS["beta"]}"]'
    ).count() == 1
    assert_readable_arrows(page)


def test_checkouts_view_uses_the_theme_colors(open_page, real_checkouts):
    """The view's own text takes the theme's tokens in both themes."""
    double = BrowserCheckouts(real_checkouts.document_zero)
    page = checkouts_view_page(open_page, real_checkouts, double)
    status_line = page.locator("#checkouts-status-line")
    dark = status_line.evaluate("line => getComputedStyle(line).color")
    assert dark == "rgb(230, 237, 243)"
    page.get_by_label("Theme", exact=True).select_option("light")
    light = status_line.evaluate("line => getComputedStyle(line).color")
    assert light == "rgb(32, 36, 42)"
    assert light != dark
    assert page.locator("#checkouts").evaluate(
        "view => getComputedStyle(view).color") == light


def test_checkouts_status_line_shows_local_remote_and_lookup_state(
    open_page, real_checkouts
):
    """The line dates the local state and names the check schedule and lookup."""
    document = real_checkouts.document_zero
    observed = document["observed_at"]
    local = checkouts_local_time(observed)
    double = BrowserCheckouts(document)
    page = checkouts_view_page(open_page, real_checkouts, double)
    status_line = page.locator("#checkouts-status-line")
    assert status_line.inner_text() == (
        f"Local state {local} · Remotes not checked yet"
        " · Next check at the next refresh · Pull request lookup off"
    )
    assert status_line.locator("time").first.get_attribute("datetime") == observed
    finished = "2026-11-09T12:00:00Z"
    due = "2026-11-09T12:05:00Z"
    double.serve(
        checkouts_remote_envelope(
            document, next_due_at=due,
            last=checkouts_remote_run(finished_at=finished),
        )
    )
    fire_checkouts_poll(page)
    assert status_line.inner_text() == (
        f"Local state {local} · Remotes checked {checkouts_local_time(finished, observed)}"
        f" · Next check about {checkouts_local_time(due, observed)}"
        " · Pull request lookup off"
    )
    # A check in flight while the envelope is served says so.
    double.serve(
        checkouts_remote_envelope(
            document, state="running", last=checkouts_remote_run(finished_at=finished)
        )
    )
    fire_checkouts_poll(page)
    assert status_line.inner_text() == (
        f"Local state {local} · Checking remotes… · Next check at the next refresh"
        " · Pull request lookup off"
    )
    # A failed check keeps the document and states its category and time.
    double.serve(
        checkouts_remote_envelope(
            document, next_due_at=None,
            last=checkouts_remote_run(
                finished_at=finished, outcome="checkouts_timeout"
            ),
        )
    )
    fire_checkouts_poll(page)
    assert status_line.inner_text() == (
        f"Local state {local} · Last remote check failed (checkouts_timeout) at "
        f"{checkouts_local_time(finished, observed)}"
        " · Next check at the next refresh · Pull request lookup off"
    )


def test_checkouts_check_remotes_now_sends_one_held_post_and_announces_once(
    open_page, real_checkouts
):
    """One press while idle sends one POST, blocks repeats, and announces its result."""
    document = real_checkouts.document_zero
    observed = document["observed_at"]
    finished = "2026-11-09T12:00:00Z"
    double = RemoteCheckCheckouts(document)
    double.serve_remote_check(
        checkouts_remote_envelope(
            document, next_due_at="2026-11-09T12:05:00Z",
            last=checkouts_remote_run(finished_at=finished),
        )
    )
    page = checkouts_view_page(open_page, real_checkouts, double)
    # The view's own network surface is the GET and the POST: no checkout
    # settings request is made from here.
    settings_requests = []
    page.route(
        "**/api/checkout-settings*",
        lambda route: (
            settings_requests.append(route.request.url),
            route.fulfill(status=404, body="{}"),
        ),
    )
    requests = []

    def record_checkouts_request(request):
        if "/api/checkouts" in request.url:
            requests.append((request.method, request.url.rsplit("/api/", 1)[1]))

    page.on("request", record_checkouts_request)
    gate = double.hold_remote_check()
    button = page.locator("#check-remotes")
    button.focus()
    button.click()
    assert double.remote_entered.wait(timeout=10)
    assert button.get_attribute("aria-disabled") == "true"
    assert button.inner_text() == "Checking remotes…"
    # aria-disabled leaves the button focusable and clickable; only the
    # handler refuses the repeat.
    assert button.get_attribute("disabled") is None
    assert page.evaluate("document.activeElement.id") == "check-remotes"
    assert "Checking remotes…" in page.locator("#checkouts-status-line").inner_text()
    # Exactly one POST reached the provider; the read count did not grow.
    assert double.remote_calls == 1
    assert double.calls == 1
    assert page.evaluate("window.remoteCheckResponses") == []
    # Two more presses and two ticks send nothing while it is held.
    button.dispatch_event("click")
    button.dispatch_event("click")
    fire_poll(page)
    fire_poll(page)
    assert double.remote_calls == 1
    assert double.calls == 1
    assert page.evaluate("window.remoteCheckResponses") == []
    observe_refresh_status(page, "#checkouts-action-status")
    gate.set()
    wait_for_responses(page, "remoteCheckResponses", 1)
    settle_frames(page)
    assert page.evaluate("window.remoteCheckResponses") == [200]
    # One message, once, for the one check.
    assert page.evaluate("window.refreshStatusRecords") == 1
    assert page.evaluate("window.refreshStatusTexts") == ["Remote check finished."]
    assert page.locator("#checkouts-action-status").inner_text() == "Remote check finished."
    assert button.inner_text() == "Check remotes now"
    assert button.get_attribute("aria-disabled") is None
    # The one POST was the only checkouts request while it was held, and no
    # checkout-settings request was made.
    assert requests == [("POST", "checkouts/remote-check")]
    assert settings_requests == []
    assert page.locator("#checkouts-status-line").inner_text() == (
        f"Local state {checkouts_local_time(observed)}"
        f" · Remotes checked {checkouts_local_time(finished, observed)}"
        f" · Next check about {checkouts_local_time('2026-11-09T12:05:00Z', observed)}"
        " · Pull request lookup off"
    )


def test_checkouts_check_remotes_now_waits_for_a_held_read(open_page, real_checkouts):
    """A press during a held read is handed to a held POST and announced once."""
    document = real_checkouts.document_zero
    observed = document["observed_at"]
    finished = "2026-11-09T12:00:00Z"
    double = RemoteCheckCheckouts(document)
    double.serve_remote_check(
        checkouts_remote_envelope(document, last=checkouts_remote_run(finished_at=finished))
    )
    read_gate = double.hold()
    page = open_checkouts_page(open_page, real_checkouts, double)
    assert double.entered.wait(timeout=10)
    show_checkouts_view(page)
    button = page.locator("#check-remotes")
    button.click()
    # The press is remembered, not sent during the read.
    assert button.get_attribute("aria-disabled") == "true"
    assert button.inner_text() == "Checking remotes…"
    assert double.remote_calls == 0
    assert page.evaluate("window.remoteCheckResponses") == []
    # Hold the queued POST too, so the hand-off itself is observed in flight.
    post_gate = double.hold_remote_check()
    read_gate.set()
    wait_for_responses(page, "checkoutsResponses", 1)
    assert double.remote_entered.wait(timeout=10)
    # The read's settle sent the POST immediately and kept the pressed state:
    # the button and the status line both still say a check is running.
    assert double.remote_calls == 1
    assert double.calls == 1
    assert button.get_attribute("aria-disabled") == "true"
    assert button.inner_text() == "Checking remotes…"
    assert "Checking remotes…" in page.locator("#checkouts-status-line").inner_text()
    assert page.evaluate("window.remoteCheckResponses") == []
    # No catalog tick fired between the read's settle and the POST.
    assert page.evaluate("window.catalogResponses.length") == 1
    # Two further presses and two ticks send nothing while the POST is held.
    button.dispatch_event("click")
    button.dispatch_event("click")
    fire_poll(page)
    fire_poll(page)
    assert double.remote_calls == 1
    assert double.calls == 1
    assert page.evaluate("window.remoteCheckResponses") == []
    observe_refresh_status(page, "#checkouts-action-status")
    post_gate.set()
    wait_for_responses(page, "remoteCheckResponses", 1)
    settle_frames(page)
    assert page.evaluate("window.remoteCheckResponses") == [200]
    # One POST, one announcement, and the pressed state ends on the POST settle.
    assert double.remote_calls == 1
    assert page.evaluate("window.refreshStatusRecords") == 1
    assert page.evaluate("window.refreshStatusTexts") == ["Remote check finished."]
    assert page.locator("#checkouts-action-status").inner_text() == "Remote check finished."
    assert f"Remotes checked {checkouts_local_time(finished, observed)}" in (
        page.locator("#checkouts-status-line").inner_text()
    )
    assert button.inner_text() == "Check remotes now"
    assert button.get_attribute("aria-disabled") is None


def test_checkouts_action_status_announces_each_completed_check(
    open_page, real_checkouts
):
    """Each completed check is announced even when its words repeat."""
    document = real_checkouts.document_zero
    finished = "2026-11-09T12:00:00Z"
    double = RemoteCheckCheckouts(document)
    for _ in range(2):
        double.serve_remote_check(
            checkouts_remote_envelope(
                document, last=checkouts_remote_run(finished_at=finished)
            )
        )
    page = checkouts_view_page(open_page, real_checkouts, double)
    observe_refresh_status(page, "#checkouts-action-status")
    for expected in (1, 2):
        page.locator("#check-remotes").click()
        wait_for_responses(page, "remoteCheckResponses", expected)
        settle_frames(page)
        assert page.evaluate("window.refreshStatusRecords") == expected
        assert page.evaluate("window.refreshStatusTexts") == (
            ["Remote check finished."] * expected
        )
    assert page.locator("#checkouts-action-status").inner_text() == "Remote check finished."


def test_checkouts_remote_check_failed_outcome_writes_its_category(
    open_page, real_checkouts
):
    """A 200 envelope with a failed check keeps the view and names the failure."""
    document = real_checkouts.document_zero
    observed = document["observed_at"]
    finished = "2026-11-09T12:00:00Z"
    double = RemoteCheckCheckouts(document)
    double.serve_remote_check(
        checkouts_remote_envelope(
            document,
            last=checkouts_remote_run(finished_at=finished, outcome="checkouts_timeout"),
        )
    )
    page = checkouts_view_page(open_page, real_checkouts, double)
    observe_refresh_status(page, "#checkouts-action-status")
    page.locator("#check-remotes").click()
    wait_for_responses(page, "remoteCheckResponses", 1)
    settle_frames(page)
    assert page.evaluate("window.remoteCheckResponses") == [200]
    assert page.locator("#checkouts-action-status").inner_text() == (
        "Remote check failed: checkouts_timeout."
    )
    assert page.evaluate("window.refreshStatusRecords") == 1
    assert page.locator("#checkouts-status-line").inner_text() == (
        f"Local state {checkouts_local_time(observed)}"
        f" · Last remote check failed (checkouts_timeout) at "
        f"{checkouts_local_time(finished, observed)}"
        " · Next check at the next refresh · Pull request lookup off"
    )
    # The document is still shown: a failed check does not blank the view.
    assert page.locator(".checkouts-panel").count() == 0


def test_checkouts_remote_check_502_installs_the_failed_panel(
    open_page, real_checkouts
):
    """A 502 from the remote-check route installs its category as the failed panel."""
    document = real_checkouts.document_zero
    double = RemoteCheckCheckouts(document)
    double.fail_remote_check("checkouts_timeout")
    page = checkouts_view_page(open_page, real_checkouts, double)
    page.locator("#check-remotes").click()
    wait_for_responses(page, "remoteCheckResponses", 1)
    settle_frames(page)
    assert page.evaluate("window.remoteCheckResponses") == [502]
    assert page.locator("#checkouts-action-status").inner_text() == (
        "Remote check failed: checkouts_timeout."
    )
    assert page.locator(".checkouts-panel").inner_text() == (
        "Checkout state is unavailable: checkouts_timeout."
        " It clears when a later collection finishes in time."
    )
    assert page.locator("#board-issues .checkouts-issue-summary").inner_text() == (
        "Code Status unavailable: checkouts_timeout"
    )


def test_checkouts_check_remotes_now_404_keeps_the_shown_outcome(
    open_page, real_checkouts
):
    """A provider without the method answers 404, and the view is left as it was."""
    document = real_checkouts.document_zero
    double = BrowserCheckouts(document)
    page = checkouts_view_page(open_page, real_checkouts, double)
    before = page.locator("#checkouts-status-line").inner_text()
    page.locator("#check-remotes").click()
    wait_for_responses(page, "remoteCheckResponses", 1)
    settle_frames(page)
    assert page.evaluate("window.remoteCheckResponses") == [404]
    assert page.locator("#checkouts-action-status").inner_text() == (
        "This server does not offer remote checks."
    )
    assert page.locator("#checkouts-status-line").inner_text() == before
    assert page.locator(".checkouts-panel").count() == 0
    assert page.locator("#board-issues").count() == 0
    assert page.locator("#check-remotes").inner_text() == "Check remotes now"


def test_checkouts_check_remotes_404_says_what_the_words_block_says(
    open_page, real_checkouts
):
    """The 404 sentence comes from the words block: editing one without the page fails."""
    edited = "Remote checks are not part of this server."
    source = checkouts_view_source().read_text(encoding="utf-8")
    entry = f'"doesNotOfferRemotes": {json.dumps(checkout_words()["text"]["doesNotOfferRemotes"])}'
    assert source.count(entry) == 1
    # Only the words entry is edited; any other copy of the sentence stays.
    page_source = source.replace(entry, f'"doesNotOfferRemotes": {json.dumps(edited)}')
    double = BrowserCheckouts(real_checkouts.document_zero)
    page = open_checkouts_page(open_page, real_checkouts, double)
    page.route("**/static/app.js", lambda route: route.fulfill(
        status=200, content_type="text/javascript", body=page_source))
    page.reload()
    wait_for_responses(page, "checkoutsResponses", 1)
    settle_frames(page)
    show_checkouts_view(page)
    page.locator("#check-remotes").click()
    wait_for_responses(page, "remoteCheckResponses", 1)
    settle_frames(page)
    assert page.evaluate("window.remoteCheckResponses") == [404]
    assert page.locator("#checkouts-action-status").inner_text() == edited


def test_checkouts_settles_leave_the_board_alone_until_the_view_is_shown(
    open_page, real_checkouts
):
    """Ticks while the board is shown touch no board node; switching shows the latest outcome."""
    document = real_checkouts.document_zero
    double = BrowserCheckouts(document)
    page = open_code_status_page(open_page, real_checkouts, double)
    settle_frames(page)
    rail_layer = page.locator(".rail-layer").element_handle()
    page.evaluate("""() => {
      window.boardRecords = [];
      new MutationObserver((records) => window.boardRecords.push(...records.map(
        (record) => record.type + ':' + (record.attributeName || ''))))
        .observe(document.querySelector('#board'),
          {subtree: true, childList: true, attributes: true, characterData: true});
    }""")

    # An unchanged tick, then one whose outcome changes only the scan's time:
    # neither re-filters the board or redraws its rails.
    double.serve(checkouts_envelope(document))
    fire_checkouts_poll(page)
    settle_frames(page)
    later = "2030-01-01T00:00:00Z"
    double.serve(checkouts_envelope(with_observed_at(document, later)))
    fire_checkouts_poll(page)
    settle_frames(page)
    assert page.evaluate("window.boardRecords") == []
    assert rail_layer.evaluate("node => node.isConnected")

    # A failure while the board is shown is still recorded for the view.
    double.fail("checkouts_timeout")
    fire_checkouts_poll(page)
    settle_frames(page)
    double.serve(checkouts_envelope(with_observed_at(document, later)))
    fire_checkouts_poll(page)
    settle_frames(page)

    page.locator("#view-checkouts").click()
    playwright.expect(page.locator("#checkouts")).to_be_visible()
    assert page.locator("#checkouts-status-line").inner_text().startswith(
        f"Local state {checkouts_local_time(later, later)}")
    assert page.locator(".checkouts-panel").count() == 0
    assert page.locator(".checkouts-group").count() > 0


def checkouts_remote_check_mutations():
    """Each remote-check mutation the browser must refuse as a protocol failure."""
    run = checkouts_remote_run(finished_at="2026-11-09T12:00:00Z")

    def missing_state(remote):
        del remote["state"]

    def extra_key(remote):
        remote["unexpected"] = 1

    def state_outside_the_set(remote):
        remote["state"] = "paused"

    def zero_interval(remote):
        remote["interval_seconds"] = 0

    def text_interval(remote):
        remote["interval_seconds"] = "300"

    def malformed_due(remote):
        remote["next_due_at"] = "tomorrow"

    def due_without_zone(remote):
        remote["next_due_at"] = "2026-11-09T12:05:00"

    def last_missing_key(remote):
        last = dict(run)
        del last["repositories"]
        remote["last"] = last

    def last_extra_key(remote):
        last = dict(run)
        last["unexpected"] = 1
        remote["last"] = last

    def last_bad_started(remote):
        last = dict(run)
        last["started_at"] = "yesterday"
        remote["last"] = last

    def last_bad_finished(remote):
        last = dict(run)
        last["finished_at"] = 7
        remote["last"] = last

    def outcome_outside_the_categories(remote):
        last = dict(run)
        last["outcome"] = "producer_timeout"
        remote["last"] = last

    def unknown_checkouts_category(remote):
        last = dict(run)
        last["outcome"] = "checkouts_surprise"
        remote["last"] = last

    def repositories_object(remote):
        last = dict(run)
        last["repositories"] = {}
        remote["last"] = last

    def last_text(remote):
        remote["last"] = "completed"

    return [
        {"id": "missing-state", "mutate": missing_state},
        {"id": "extra-key", "mutate": extra_key},
        {"id": "state-outside-the-set", "mutate": state_outside_the_set},
        {"id": "zero-interval", "mutate": zero_interval},
        {"id": "text-interval", "mutate": text_interval},
        {"id": "malformed-due", "mutate": malformed_due},
        {"id": "due-without-zone", "mutate": due_without_zone},
        {"id": "last-missing-key", "mutate": last_missing_key},
        {"id": "last-extra-key", "mutate": last_extra_key},
        {"id": "last-bad-started", "mutate": last_bad_started},
        {"id": "last-bad-finished", "mutate": last_bad_finished},
        {"id": "outcome-outside-the-categories", "mutate": outcome_outside_the_categories},
        {"id": "unknown-checkouts-category", "mutate": unknown_checkouts_category},
        {"id": "repositories-object", "mutate": repositories_object},
        {"id": "last-text", "mutate": last_text},
    ]


def test_checkouts_view_refuses_each_malformed_remote_check_envelope(
    open_page, real_checkouts
):
    """Every malformed remote-check shape shows the protocol panel, then clears."""
    double = BrowserCheckouts(real_checkouts.document_zero)
    page = checkouts_view_page(open_page, real_checkouts, double)
    panel = page.locator(".checkouts-panel")
    assert panel.count() == 0
    for case in checkouts_remote_check_mutations():
        envelope = checkouts_envelope(copy.deepcopy(real_checkouts.document_zero))
        case["mutate"](envelope["remote_checks"])
        double.serve(envelope)
        fire_checkouts_poll(page)
        playwright.expect(panel).to_have_count(1)
        assert panel.inner_text() == (
            "Checkout state is unavailable: checkouts_protocol_error."
            " It clears when a later collection returns a valid document."
        ), case["id"]
        assert page.locator("#checkouts-status-line").inner_text() == "", case["id"]
        # The next ready document clears it again.
        fire_checkouts_poll(page)
        playwright.expect(panel).to_have_count(0)


def test_checkouts_check_remotes_now_uses_the_real_route(open_page, real_checkouts):
    """The real runtime accepts the POST once its held network fetch is released."""
    workers = CheckoutsWorkerStandIn(real_checkouts.document_zero_bytes)
    page = open_page(
        StaticClient(real_checkouts.catalog),
        settings=BrowserSettings(revision=real_checkouts.revision),
        workers=workers,
        fake_clock=True,
        init_script=RESPONSE_RECORDER,
    )
    wait_for_responses(page, "checkoutsResponses", 1)
    settle_frames(page)
    show_checkouts_view(page)
    assert page.locator("#checkouts-status-line").inner_text().count("Remotes checked ") == 1
    posted = []
    page.on(
        "request",
        lambda request: posted.append(request)
        if request.url.endswith("/api/checkouts/remote-check") else None,
    )
    gate = workers.hold_network()
    page.locator("#check-remotes").click()
    assert workers.network_entered.wait(timeout=10)
    assert page.locator("#check-remotes").get_attribute("aria-disabled") == "true"
    assert workers.network_calls == 2
    assert page.evaluate("window.remoteCheckResponses") == []
    assert [(request.method, request.headers.get("content-type"), request.post_data)
            for request in posted] == [("POST", "application/json", "{}")]
    gate.set()
    wait_for_responses(page, "remoteCheckResponses", 1)
    settle_frames(page)
    # The real route answered 200, so its envelope passed the extended validator.
    assert page.evaluate("window.remoteCheckResponses") == [200]
    assert page.locator(".checkouts-panel").count() == 0
    status = page.locator("#checkouts-status-line").inner_text()
    assert "Remotes checked " in status
    assert "Last remote check failed" not in status
    assert page.locator("#checkouts-action-status").inner_text() == "Remote check finished."
    assert page.locator("#check-remotes").inner_text() == "Check remotes now"


def test_checkouts_view_standalone_server_shows_only_the_unavailable_panel(
    open_page, real_checkouts
):
    """A server without the checkouts route shows one panel and a usable switch."""
    page = open_page(
        StaticClient(real_checkouts.catalog),
        fake_clock=True,
        init_script=RESPONSE_RECORDER,
    )
    wait_for_responses(page, "checkoutsResponses", 1)
    settle_frames(page)
    show_checkouts_view(page)
    panel = page.locator(".checkouts-panel")
    playwright.expect(panel).to_have_count(1)
    assert panel.inner_text() == "This Nyx server does not serve checkout state."
    assert page.locator("#checkouts-status-line").inner_text() == ""
    assert page.locator("#checkouts .checkouts-group").count() == 0
    assert page.locator("#checkouts .checkouts-attention").count() == 0
    assert page.locator("#checkouts .checkouts-setup").count() == 0
    # The switch stays usable while the panel is shown.
    page.locator("#view-board").click()
    playwright.expect(page.locator("#board")).to_be_visible()
    page.locator("#view-checkouts").click()
    playwright.expect(page.locator("#checkouts")).to_be_visible()
    # The next tick's ready document removes the panel.
    page.route(
        "**/api/checkouts",
        lambda route: route.fulfill(
            status=200,
            body=json.dumps(checkouts_envelope(real_checkouts.document_zero)),
        ),
    )
    fire_checkouts_poll(page)
    playwright.expect(panel).to_have_count(0)
    assert page.locator("#checkouts-status-line").inner_text().startswith("Local state ")


def test_checkouts_view_failed_request_shows_the_clearing_panel(
    open_page, real_checkouts
):
    """A 502 shows its category and clearing words, and leaves the board's part alone."""
    double = BrowserCheckouts(real_checkouts.document_zero)
    double.fail("checkouts_timeout")
    page = checkouts_view_page(open_page, real_checkouts, double)
    panel = page.locator(".checkouts-panel")
    playwright.expect(panel).to_have_count(1)
    assert panel.inner_text() == (
        "Checkout state is unavailable: checkouts_timeout."
        " It clears when a later collection finishes in time."
    )
    assert page.locator("#checkouts-status-line").inner_text() == ""
    assert page.locator("#checkouts .checkouts-group").count() == 0
    assert page.locator("#checkouts .checkouts-attention").count() == 0
    assert page.locator("#checkouts .checkouts-setup").count() == 0
    # The board's issues part keeps the existing failure part, unchanged.
    page.locator("#view-board").click()
    playwright.expect(page.locator("#board")).to_be_visible()
    assert page.locator(".checkouts-issue-summary").inner_text() == (
        "Code Status unavailable: checkouts_timeout"
    )
    page.locator("#view-checkouts").click()
    playwright.expect(page.locator("#checkouts")).to_be_visible()
    # The next tick's ready document removes the panel.
    double.serve(checkouts_envelope(real_checkouts.document_zero))
    fire_checkouts_poll(page)
    playwright.expect(panel).to_have_count(0)
    assert page.locator("#checkouts-status-line").inner_text().startswith("Local state ")


def test_checkouts_view_malformed_body_shows_the_protocol_panel(
    open_page, real_checkouts
):
    """A 200 body the client cannot validate shows the protocol panel until a valid tick."""
    double = BrowserCheckouts(real_checkouts.document_zero)
    page = checkouts_view_page(open_page, real_checkouts, double)
    panel = page.locator(".checkouts-panel")
    assert panel.count() == 0
    page.route("**/api/checkouts", lambda route: route.fulfill(status=200, body="not json"))
    fire_checkouts_poll(page)
    playwright.expect(panel).to_have_count(1)
    assert panel.inner_text() == (
        "Checkout state is unavailable: checkouts_protocol_error."
        " It clears when a later collection returns a valid document."
    )
    assert page.locator("#checkouts-status-line").inner_text() == ""
    assert page.locator("#checkouts .checkouts-group").count() == 0
    assert page.locator("#checkouts .checkouts-setup").count() == 0
    # The switch stays usable while the panel is shown.
    page.locator("#view-board").click()
    playwright.expect(page.locator("#board")).to_be_visible()
    page.locator("#view-checkouts").click()
    playwright.expect(page.locator("#checkouts")).to_be_visible()
    page.unroute("**/api/checkouts")
    double.serve(checkouts_envelope(real_checkouts.document_zero))
    fire_checkouts_poll(page)
    playwright.expect(panel).to_have_count(0)
    assert page.locator("#checkouts-status-line").inner_text().startswith("Local state ")


# ---------------------------------------------------------------------------
# The checkouts view: words, groups, rows and the Checkout setup panel
# ---------------------------------------------------------------------------

CHECKOUTS_WORDS_BEGIN = "// checkout words: begin"
CHECKOUTS_WORDS_END = "// checkout words: end"


def checkouts_view_source():
    """The one browser file the view's words and rendering live in."""
    return Path(__file__).resolve().parents[1] / "nyx" / "static" / "app.js"


def checkout_words():
    """The view's one words block, read between its markers as the JSON it must stay."""
    source = checkouts_view_source().read_text(encoding="utf-8")
    assert source.count(CHECKOUTS_WORDS_BEGIN) == 1
    assert source.count(CHECKOUTS_WORDS_END) == 1
    block = source.split(CHECKOUTS_WORDS_BEGIN, 1)[1].split(CHECKOUTS_WORDS_END, 1)[0]
    match = re.fullmatch(r"\s*const WORDS = (\{.*\});\s*", block, re.S)
    assert match is not None, "the words block is not one `const WORDS = {…};` literal"
    return json.loads(match.group(1))


def _checkout_literal_strings(node):
    """Every string a word-carrying expression names, however it nests."""
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else set()
    if isinstance(node, ast.IfExp):
        return _checkout_literal_strings(node.body) | _checkout_literal_strings(node.orelse)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        found = set()
        for element in node.elts:
            found |= _checkout_literal_strings(element)
        return found
    return set()


def checkout_function_literals(name):
    """The string literals one collector function appends, returns or keys state/basis with."""
    source = (Path(__file__).resolve().parents[1] / "nyx" / "models.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    [function] = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    found = {"appended": set(), "returned": set(), "named": set()}
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                node.func.attr in ("append", "extend") and node.args:
            found["appended"] |= _checkout_literal_strings(node.args[0])
        if isinstance(node, ast.Return) and node.value is not None:
            found["returned"] |= _checkout_literal_strings(node.value)
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in ("state", "basis"):
                    found["named"] |= _checkout_literal_strings(value)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in ("state", "basis"):
                    found["named"] |= _checkout_literal_strings(node.value)
    return found


def checkout_word_requirements():
    """Every value the view renders, paired with the tables that must name it."""
    availability = checkout_function_literals("_clone_availability")
    blockers = checkout_function_literals("derive_removable")["appended"] | \
        checkout_function_literals("_verdict_blockers")["returned"]
    attention_basis = checkout_function_literals("derive_attention")["named"]
    return [
        (("attentionClass",), {name for name, _ in models._ATTENTION_CODES}),
        (("attentionCode",), {code for _, code in models._ATTENTION_CODES}),
        (("availabilityState",), availability["named"]),
        (("availabilityReason",), availability["appended"]),
        (("assignmentBasis",), attention_basis),
        (("status",), set(models._CLONE_STATUSES)),
        (("comparisonRelation",), set(models._COMPARISON_RELATIONS)),
        (("comparisonBasis",), set(models._COMPARISON_BASES)),
        (("verdictState",), set(models._VERDICT_STATES)),
        (("verdictRung",), set(models._VERDICT_MERGED_RUNGS) | {"none"}),
        (("verdictReason",), set(models._REMOTE_VERDICT_UNKNOWN_REASONS)),
        (("branchBlocker", "worktreeBlocker"), blockers),
        (("operation",), set(models.OPERATIONS)),
        (("changeKind",), set(models._CHANGE_KEYS)),
        (("folderStatus",), set(models._FOLDER_STATUSES)),
        # Every reason a save can refuse a folder for: the collector's problem
        # statuses, which the save reuses, and the two it checks before reading.
        (("folderReason",), (set(models._FOLDER_STATUSES) - {"ok"}) | {"invalid_path", "not_absolute"}),
        (("gitStatus",), set(models._GIT_STATUSES)),
        (("checkStatus",), set(models._CHECK_STATUSES)),
        (("checkReason",), set(models._CHECK_FAILURE_REASONS)),
        (("lookupStatus",), set(models._PR_LOOKUP_STATUSES)),
        (("lookupReason",), set(models._PR_LOOKUP_FAILURE_REASONS)),
        (("remoteCopy",), set(models._REMOTE_COPY_STATES)),
        (("pullRequestState",), set(models._PULL_REQUEST_STATES)),
        (("branchPackageBasis",), set(models._BRANCH_PACKAGE_BASES)),
        (("containment",), set(models._CONTAINMENT_OUTCOMES)),
        (("diagnostic",), set(models.CHECKOUTS_DIAGNOSTIC_CODES)),
        (("lookupReason",), {"setting_off", "not_github", "remote_checks_off"}),
        (("checkReason",), {"remote_checks_off"}),
        (("containment",), {"no_base", "incomplete", "git_too_old"}),
        (("verdictRung",), {"none"}),
    ]


def missing_checkout_words(words):
    """Every value the words block does not name, as ``(table, value)`` pairs."""
    missing = []
    for tables, values in checkout_word_requirements():
        for value in sorted(values):
            if not any(value in (words.get(table) or {}) for table in tables):
                missing.append((tables[0], value))
    return missing


def test_checkouts_words_cover_every_served_value():
    """A collector value without browser words fails; one removed value fails too."""
    words = checkout_words()
    assert missing_checkout_words(words) == []

    # Removing one value from a test-local copy fails the same comparison, so
    # the test above cannot pass on an empty or stale word table.
    broken = copy.deepcopy(words)
    assert broken["comparisonRelation"].pop("diverged") == "{a} ahead, {b} behind"
    assert missing_checkout_words(broken) == [("comparisonRelation", "diverged")]
    # A save reason without words fails the same way.
    reasons = copy.deepcopy(words)
    reasons["folderReason"].pop("not_absolute")
    assert missing_checkout_words(reasons) == [("folderReason", "not_absolute")]


def test_checkouts_words_block_names_the_view_markers_once():
    """The block is one JSON literal between the exact markers the test reads."""
    source = checkouts_view_source().read_text(encoding="utf-8")
    assert source.count(CHECKOUTS_WORDS_BEGIN) == 1
    assert source.count(CHECKOUTS_WORDS_END) == 1
    words = checkout_words()
    # The rows, headers and setup sentences the view shows are in the block too.
    assert words["text"]["noFolders"] == (
        "No checkout folders are configured. Choose Checkout folders above to add one."
    )
    assert words["column"]["default"] == "Default branch vs remote"
    assert words["text"]["workItemBranches"] == "Work item branches"


# ---------------------------------------------------------------------------
# The view's real git world and the documents it captures once
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def view_checkouts(tmp_path_factory):
    """One real checkout world and the documents every view proof reads.

    A bare ``trunk`` remote is cloned three times into one folder: the second
    clone carries a linked worktree outside the folder, the third is level with
    the remote, and the tenth never fetched.  A second remote holds one clean
    clone, and a clone outside the folder pushes a package branch no listed
    clone holds.  The documents are captured as built, after a remote check
    that follows a push from another clone, on the next local collection, with
    the bare remote renamed, after one more commit on ``topic-a``, after a
    second one, after that branch is deleted, after a stash in the quiet clone, with no finished
    stage, and with no ``git`` on ``PATH``.  This world is separate from
    ``real_checkouts`` so that fixture's pinned Code Status texts stay as they
    are.
    """
    root = tmp_path_factory.mktemp("view-checkouts")
    folder = root / "checkouts"
    folder.mkdir()
    served = root / "served"
    served.mkdir()
    (root / "home").mkdir()
    # The saved remote check lives in ~/.nyx, so a later local collection can
    # read the answer the check recorded.
    (root / "home" / ".nyx").mkdir()
    spec_root = root / "specification-root"
    missing_folder = root / "missing-folder"
    with pytest.MonkeyPatch.context() as patch:
        isolate_git(patch, root / "home")
        remote = make_remote(served)
        quiet_remote = make_remote(served, "quiet.git")
        clone_two = clone(remote.as_uri(), folder / "Repo Clone 2")
        clone_three = clone(remote.as_uri(), folder / "Repo Clone 3")
        clone(remote.as_uri(), folder / "Repo Clone 10")
        quiet = clone(quiet_remote.as_uri(), folder / "Quiet Clone")
        # The quiet clone and two others have fetched; the tenth has not, which
        # is a freshness state of its own.
        git(quiet, "fetch", "-q")
        git(clone_two, "fetch", "-q")
        git(clone_three, "fetch", "-q")
        outside = root / "outside"
        git(clone_two, "worktree", "add", "-q", str(outside), "-b", "outside")
        # A clone outside the folder pushes a package branch no listed clone
        # holds, so the group serves a remote-only verdict.
        away = clone(remote.as_uri(), root / "away" / "Away Clone")
        git(away, "checkout", "-q", "-b", "topic-d")
        commit_file(away, "delta.txt", "delta one\n")
        git(away, "push", "-q", "-u", "origin", "topic-d")
        git(clone_two, "checkout", "-q", "-b", "topic-a")
        commit_file(clone_two, "alpha.txt", "alpha one\n")
        git(clone_two, "push", "-q", "-u", "origin", "topic-a")
        git(clone_two, "checkout", "-q", "trunk")

        target = [f"Target repo: {clone_two}"]
        _package_spec(spec_root, "Drafts", "alpha", CHECKOUTS_PACKAGE_IDS["alpha"],
                      ["Branch: topic-a", *target])
        _package_spec(spec_root, "Drafts", "delta", CHECKOUTS_PACKAGE_IDS["delta"],
                      ["Branch: topic-d", *target])
        _package_spec(spec_root, "Shipped", "gamma", CHECKOUTS_PACKAGE_IDS["gamma"],
                      ["Branch: topic-c", *target])

        def collect(stages=("Shipped",), **options):
            """One real collection of the fixture folder and package scan."""
            return json.loads(collect_checkouts(
                [str(folder)],
                deadline_ns=time.monotonic_ns() + 60_000_000_000,
                package_scan=PackageScan(spec_root, stages),
                configuration_revision=CHECKOUTS_REVISION,
                **options,
            ))

        catalog = json.loads(scan_catalog(
            spec_root,
            completed_stage_names=["Shipped"],
            configuration_revision=CHECKOUTS_REVISION,
        ))
        document_built = collect()
        # Another clone pushes a commit, so the first clone is behind and the
        # third is level when the remote is checked.
        commit_file(clone_three, "moved.txt", "moved\n")
        git(clone_three, "push", "-q", "origin", "trunk")
        document_checked = collect(remote_checks=True)
        document_recorded = collect()
        away_remote = remote.rename(served / "remote.away")
        document_renamed = collect(remote_checks=True)
        away_remote.rename(remote)
        git(clone_two, "checkout", "-q", "topic-a")
        commit_file(clone_two, "alpha.txt", "alpha two\n")
        document_topic_committed = collect()
        commit_file(clone_two, "alpha.txt", "alpha three\n")
        document_topic_recommitted = collect()
        git(clone_two, "checkout", "-q", "trunk")
        git(clone_two, "branch", "-D", "topic-a")
        document_topic_deleted = collect()
        (quiet / "file.txt").write_bytes(b"stashed\n")
        git(quiet, "stash", "push", "-q")
        document_quiet_stashed = collect()
        document_no_finished = collect(stages=())
        empty_bin = root / "empty-bin"
        empty_bin.mkdir()
        patch.setenv("PATH", str(empty_bin))
        document_no_git = collect()
    return SimpleNamespace(
        folder=folder,
        spec_root=spec_root,
        missing_folder=missing_folder,
        outside=outside,
        document_built=document_built,
        document_checked=document_checked,
        document_recorded=document_recorded,
        document_renamed=document_renamed,
        document_topic_committed=document_topic_committed,
        document_topic_recommitted=document_topic_recommitted,
        document_topic_deleted=document_topic_deleted,
        document_quiet_stashed=document_quiet_stashed,
        document_no_finished=document_no_finished,
        document_no_git=document_no_git,
        catalog=catalog,
    )


def test_checkouts_view_fixture_captures_the_states_it_names(view_checkouts):
    """Every capture parses, and each one carries the state its name claims."""
    captures = {
        "built": view_checkouts.document_built,
        "checked": view_checkouts.document_checked,
        "recorded": view_checkouts.document_recorded,
        "renamed": view_checkouts.document_renamed,
        "topic committed": view_checkouts.document_topic_committed,
        "topic recommitted": view_checkouts.document_topic_recommitted,
        "topic deleted": view_checkouts.document_topic_deleted,
        "quiet stashed": view_checkouts.document_quiet_stashed,
        "no finished stage": view_checkouts.document_no_finished,
        "no git": view_checkouts.document_no_git,
    }
    for label, document in captures.items():
        models.parse_checkouts(json.dumps(document))

    def group(document, suffix):
        return checkout_group(document, suffix)

    def branch_names(document):
        _, clone = checkout_clone(document, "Repo Clone 2")
        return [branch["name"] for branch in clone["branches"]]

    assert group(view_checkouts.document_built, "remote")["remote"]["check"]["status"] == "off"
    assert group(view_checkouts.document_checked, "remote")["remote"]["check"]["status"] == "live"
    assert group(view_checkouts.document_recorded, "remote")["remote"]["check"]["status"] == "recorded"
    assert group(view_checkouts.document_renamed, "remote")["remote"]["check"] == {
        "status": "recorded", "reason": "not_found",
        "checked_at": group(view_checkouts.document_renamed, "remote")
        ["remote"]["check"]["checked_at"],
    }
    assert "topic-a" not in branch_names(view_checkouts.document_topic_deleted)
    assert "topic-a" in branch_names(view_checkouts.document_topic_committed)
    assert checkout_clone(view_checkouts.document_quiet_stashed, "Quiet Clone")[1][
        "stash_count"] == 1
    assert view_checkouts.document_no_finished["package_scan"]["finished_policy"] == \
        "not_configured"
    assert view_checkouts.document_no_git["git"]["status"] == "missing"
    assert view_checkouts.document_no_git["repositories"] == []


# ---------------------------------------------------------------------------
# Re-digested shape variants the parser must still accept
# ---------------------------------------------------------------------------


def checkout_group(document, display_suffix):
    """The group whose served display name ends with ``display_suffix``."""
    for group in document["repositories"]:
        remote = group["remote"]
        if remote is not None and str(remote["display"]).endswith(display_suffix):
            return group
    raise AssertionError(
        f"no group display ends with {display_suffix}: "
        f"{[group['remote'] for group in document['repositories']]}"
    )


def checkout_clone(document, name):
    """The group and clone whose path is named ``name``."""
    for group in document["repositories"]:
        for item in group["clones"]:
            if Path(item["path"]).name == name:
                return group, item
    raise AssertionError(f"no clone named {name}")


def checkout_worktree(clone, name):
    """The linked worktree of ``clone`` named ``name``."""
    return next(
        worktree for worktree in clone["worktrees"]
        if not worktree["main"] and Path(worktree["path"]).name == name
    )


def set_clone_status(clone, status):
    """Make one clone unreadable in one named way, leaving the facts it cannot hold."""
    clone["status"] = status
    for field in (
        "bare", "partial_clone", "shallow", "cached_default", "local_default",
        "cached_vs_remote", "stash_count", "worktrees", "branches",
        "verdict_base", "containment", "removable_count",
    ):
        clone[field] = None


CLONE_FACTS = (
    "bare", "partial_clone", "shallow", "cached_default", "local_default",
    "cached_vs_remote", "stash_count", "worktrees", "branches",
    "verdict_base", "containment", "removable_count",
)


def reseal_checkouts(document):
    """Recompute every derived value the parser checks, then re-digest."""
    # Removable comes first: the attention list counts the items it decides.
    for group in document["repositories"]:
        for item in group["clones"]:
            if item["status"] != "ok":
                continue
            removable = models.derive_removable(item)
            for branch, value in zip(item["branches"], removable["branches"]):
                branch["removable"] = value
            for worktree, value in zip(item["worktrees"], removable["worktrees"]):
                worktree["removable"] = value
            item["removable_count"] = removable["removable_count"]
    derived = models.derive_attention(document)
    for group, free in zip(document["repositories"], derived["free_clones"]):
        group["free_clones"] = free
        for item in group["clones"]:
            item["availability"] = derived["availability"][item["path"]]
    document["attention"] = derived["attention"]
    return redigest(document)


def checkout_raw_variant(document, mutate):
    """One re-digested capture with ``mutate`` applied, without the parser check."""
    variant = copy.deepcopy(document)
    mutate(variant)
    return reseal_checkouts(variant)


def checkout_variant(document, mutate):
    """One re-digested capture with ``mutate`` applied; the parser must accept it."""
    variant = checkout_raw_variant(document, mutate)
    models.parse_checkouts(json.dumps(variant))
    return variant


def open_view_checkouts(launch, view_checkouts, document):
    """Open the checkouts view over this fixture's catalog and ``document``."""
    double = BrowserCheckouts(document)
    page = checkouts_view_page(
        launch,
        SimpleNamespace(catalog=view_checkouts.catalog, revision=CHECKOUTS_REVISION),
        double,
    )
    return page, double


def serve_checkouts(page, double, document):
    """Answer the next tick with ``document`` and let the view render it."""
    double.serve(checkouts_envelope(document))
    fire_checkouts_poll(page)
    settle_frames(page)


def group_index(document, display_suffix):
    """Where a group sits in the served repository order."""
    for index, group in enumerate(document["repositories"]):
        remote = group["remote"]
        if remote is not None and str(remote["display"]).endswith(display_suffix):
            return index
    raise AssertionError(f"no group display ends with {display_suffix}")


def relation_words(comparison):
    """The relation words a served comparison renders, from the view's own block."""
    template = checkout_words()["comparisonRelation"][comparison["relation"]]
    return template.format(
        a="" if comparison["ahead"] is None else comparison["ahead"],
        b="" if comparison["behind"] is None else comparison["behind"],
    )


# ---------------------------------------------------------------------------
# The one Checkout setup panel
# ---------------------------------------------------------------------------


def add_setup_problems(view_checkouts):
    """A missing folder, a group whose lookup lost ``gh``, and one diagnostic."""
    missing = str(view_checkouts.missing_folder)
    diagnostic_path = str(view_checkouts.folder / "Not A Repo")

    def mutate(document):
        document["folders"] = [
            {"path": str(view_checkouts.folder), "status": "ok"},
            {"path": missing, "status": "missing"},
        ]
        document["github_lookup"] = "on"
        group = checkout_group(document, "remote")
        group["remote"]["key"] = "github.com/example/example"
        group["remote"]["display"] = "example/example"
        group["pr_lookup"] = {
            "status": "unavailable", "reason": "gh_missing", "checked_at": None,
        }
        for other in document["repositories"]:
            if other is not group:
                other["pr_lookup"] = {
                    "status": "off", "reason": "not_github", "checked_at": None,
                }
        document["diagnostics"] = [{"code": "not_a_repository", "path": diagnostic_path}]
    return mutate


def git_problem_variant(view_checkouts, version):
    """A capture whose git is too old, with a missing folder and one diagnostic."""
    diagnostic_path = str(view_checkouts.folder / "Not A Repo")

    def mutate(document):
        document["git"] = {"status": "too_old", "version": version}
        document["folders"] = [
            {"path": str(view_checkouts.folder), "status": "ok"},
            {"path": str(view_checkouts.missing_folder), "status": "missing"},
        ]
        document["diagnostics"] = [{"code": "not_a_repository", "path": diagnostic_path}]
    return checkout_variant(view_checkouts.document_no_git, mutate)


def test_checkouts_setup_panel_names_the_setup_story_in_order(
    open_page, view_checkouts
):
    """One panel names each setup problem with its words, in the one order."""
    page, double = open_view_checkouts(
        open_page, view_checkouts, view_checkouts.document_built
    )
    body = page.locator("#checkouts-body")
    # Lookup off is status-line words with no panel item at all.
    assert "Pull request lookup off" in page.locator("#checkouts-status-line").inner_text()
    assert body.locator(".checkouts-setup").count() == 0

    document = checkout_variant(
        view_checkouts.document_checked, add_setup_problems(view_checkouts)
    )
    serve_checkouts(page, double, document)
    panel = body.locator(".checkouts-setup")
    playwright.expect(panel).to_have_count(1)
    assert panel.locator("h2").inner_text() == "Checkout setup"
    items = panel.locator(".checkouts-setup-item")
    assert items.evaluate_all(
        "items => items.map(item => item.dataset.setup)") == ["folder", "gh", "diagnostic"]
    texts = items.all_inner_texts()
    # A folder's words follow its path.
    assert texts[0] == f"{view_checkouts.missing_folder} — does not exist"
    assert texts[1] == (
        "The GitHub CLI (gh) was not found; pull request states are not shown.")
    assert texts[2] == (
        f"{view_checkouts.folder / 'Not A Repo'} has a .git entry but is not the top "
        "of its repository")
    # A folder problem never suppresses the groups: the folder that is still
    # readable keeps rendering its clones.
    shown = body.locator(".checkouts-group")
    assert shown.count() == 1
    assert shown.locator(".checkouts-group-name").inner_text() == "example/example"
    assert shown.locator("tr.checkouts-clone").count() == len(
        checkout_group(document, "example/example")["clones"])
    # The panel is not a live region, has no control, and never takes focus.
    assert panel.locator(
        "[aria-live], [role='status'], [role='alert'], input, button, select, "
        "textarea, [tabindex]").count() == 0
    page.locator("#view-board").focus()
    serve_checkouts(page, double, document)
    assert page.evaluate(
        "() => !document.querySelector('.checkouts-setup')"
        ".contains(document.activeElement)") is True

    # A git problem comes first, before the missing folder and the diagnostic.
    older = git_problem_variant(view_checkouts, "2.30.0")
    serve_checkouts(page, double, older)
    items = body.locator(".checkouts-setup .checkouts-setup-item")
    assert items.evaluate_all(
        "items => items.map(item => item.dataset.setup)") == ["git", "folder", "diagnostic"]
    assert items.first.inner_text() == (
        "Git 2.30.0 is older than Nyx supports (2.36)")

    unreadable = git_problem_variant(view_checkouts, None)
    serve_checkouts(page, double, unreadable)
    assert body.locator(".checkouts-setup .checkouts-setup-item").first.inner_text() == (
        "Git is older than Nyx supports (2.36), or its version could not be read")

    # A capture taken with no git on PATH: the sentence, no groups, no failure panel.
    serve_checkouts(page, double, view_checkouts.document_no_git)
    setup = body.locator(".checkouts-setup")
    assert "Git was not found, so Nyx cannot read checkouts." in setup.inner_text()
    assert "on the PATH Nyx runs with" in setup.inner_text()
    assert body.locator(".checkouts-group").count() == 0
    assert body.locator(".checkouts-panel:not(.checkouts-setup)").count() == 0

    # No configured folder: the guide section is named, and no control appears.
    serve_checkouts(page, double, empty_checkouts_document())
    setup = body.locator(".checkouts-setup")
    sentence = setup.locator(".checkouts-setup-item").inner_text()
    assert sentence == (
        "No checkout folders are configured. Choose Checkout folders above to add one."
    )
    assert setup.locator("input, button, select, textarea, [contenteditable]").count() == 0
    assert body.locator(".checkouts-group").count() == 0


# ---------------------------------------------------------------------------
# Groups, rows, Work item branches and freshness
# ---------------------------------------------------------------------------


def test_checkouts_groups_rows_and_branches_follow_the_document(
    open_page, view_checkouts
):
    """Groups, clone rows and Work item branches render the served order."""
    document = view_checkouts.document_built
    page, double = open_view_checkouts(open_page, view_checkouts, document)
    names = page.locator(".checkouts-group-name")
    quiet = [
        group for group in document["repositories"]
        if str(group["remote"]["display"]).endswith("quiet")
    ]
    assert len(quiet) == 1
    # The quiet repository has no group of its own while the box is checked.
    assert names.all_inner_texts() == [
        group["remote"]["display"] for group in document["repositories"] if group not in quiet]

    page.get_by_label("Hide quiet checkouts", exact=True).uncheck()
    assert names.all_inner_texts() == [
        group["remote"]["display"] for group in document["repositories"]]
    remote_index = group_index(document, "remote")
    group = document["repositories"][remote_index]
    remote_view = page.locator(".checkouts-group").nth(remote_index)

    # The eight column headers, once per group table.
    assert remote_view.locator(
        "table.checkouts-clones > thead th").all_inner_texts() == [
        "Checkout", "Availability", "HEAD", "Default branch vs remote",
        "Tree", "Branches", "Cleanup", "Package",
    ]
    # Clone rows follow the group's clones, and a linked worktree sits directly
    # under the clone that owns it.
    assert remote_view.locator(
        "table.checkouts-clones > tbody > tr:not(.checkouts-detail)").evaluate_all(
        "rows => rows.map(row => row.classList.contains('checkouts-worktree')"
        " ? row.dataset.worktree : row.dataset.clone)") == [
        "Repo Clone 2", "outside", "Repo Clone 3", "Repo Clone 10",
    ]
    assert [Path(clone["path"]).name for clone in group["clones"]] == [
        "Repo Clone 2", "Repo Clone 3", "Repo Clone 10"]

    # Work item branches follow the group's package_branches with their words.
    entries = group["package_branches"]
    branches = remote_view.locator("table.checkouts-package-branches tbody tr")
    assert remote_view.locator(
        "table.checkouts-package-branches caption").inner_text() == "Work item branches"
    assert branches.evaluate_all(
        "rows => rows.map(row => row.dataset.branch)") == [
        entry["name"] for entry in entries]
    by_name = {entry["name"]: entry for entry in entries}
    topic_a = remote_view.locator(
        'table.checkouts-package-branches tr[data-branch="topic-a"]')
    assert topic_a.locator(".checkouts-remote-copy").inner_text() == "on the remote (cached)"
    assert topic_a.locator(".checkouts-verdict-words").inner_text() == "not merged"
    # A merge verdict carries the basis of the clone's verdict base.
    assert topic_a.locator(".checkouts-verdict .checkouts-basis").inner_text() == "cached"
    assert topic_a.locator(".checkouts-work-items .checkouts-package-path").inner_text() == (
        "Repo/Drafts/alpha")
    assert topic_a.locator(".checkouts-work-items .checkouts-basis").inner_text() == (
        "(Target repo)")
    assert topic_a.locator("[data-show-on-board]").inner_text() == "Show on board"
    assert by_name["topic-a"]["remote_verdict"] is None
    topic_c = remote_view.locator(
        'table.checkouts-package-branches tr[data-branch="topic-c"]')
    assert topic_c.locator(".checkouts-remote-copy").inner_text() == "unknown"

    # Free clones come from the served list, in its order, with each distance.
    free = group["free_clones"]
    assert len(free) >= 2
    expected = []
    for path in free:
        clone = next(item for item in group["clones"] if item["path"] == path)
        local = clone["local_default"]
        distance = local["vs_remote"] or local["vs_cached"]
        expected.append(f"{Path(path).name} {relation_words(distance)}")
    assert remote_view.locator(".checkouts-group-free").inner_text() == (
        "Free clones: " + " · ".join(expected))

    # A never-fetched clone reads cached on its value and names the age once.
    clone_ten = remote_view.locator('tr.checkouts-clone[data-clone="Repo Clone 10"]')
    assert clone_ten.locator(".checkouts-default .checkouts-relation").inner_text() == "up to date"
    assert clone_ten.locator(".checkouts-default .checkouts-basis").inner_text() == "cached"
    assert "cached, never fetched" in clone_ten.inner_text()
    clone_two = remote_view.locator('tr.checkouts-clone[data-clone="Repo Clone 2"]')
    assert clone_two.locator(".checkouts-head").inner_text() == "trunk"
    assert clone_two.locator(".checkouts-tree").inner_text() == "clean"
    assert clone_two.locator(".checkouts-branches").inner_text() == "3 branches"
    assert clone_two.locator(".checkouts-cleanup").inner_text().startswith(
        "1 branch, 1 worktree removable")
    assert clone_two.locator(".checkouts-cleanup").inner_text().endswith(
        "topic-a (not merged)")
    linked = remote_view.locator('tr.checkouts-worktree[data-worktree="outside"]')
    assert linked.locator(".checkouts-head").inner_text() == "outside"
    assert linked.locator(".checkouts-cleanup").inner_text() == "Removable"

    # One remote check later, the same rows read the live answers: the branch
    # no clone holds states why, and the branch nothing pushed is absent.
    checked = view_checkouts.document_checked
    serve_checkouts(page, double, checked)
    remote_index = group_index(checked, "remote")
    remote_view = page.locator(".checkouts-group").nth(remote_index)
    topic_d = remote_view.locator(
        'table.checkouts-package-branches tr[data-branch="topic-d"]')
    assert topic_d.locator(".checkouts-remote-copy").inner_text() == "on the remote (live)"
    assert topic_d.locator(".checkouts-verdict-words").inner_text() == (
        "merge state unknown — no clone holds its commits")
    topic_c = remote_view.locator(
        'table.checkouts-package-branches tr[data-branch="topic-c"]')
    assert topic_c.locator(".checkouts-remote-copy").inner_text() == "not on the remote"
    assert topic_a.locator(".checkouts-remote-copy").inner_text() == "on the remote (live)"



def test_checkouts_freshness_reads_cached_live_and_recorded(
    open_page, view_checkouts
):
    """One value names its own basis; a failed latest check says so."""
    page, double = open_view_checkouts(
        open_page, view_checkouts, view_checkouts.document_built
    )
    clone_ten = page.locator('tr.checkouts-clone[data-clone="Repo Clone 10"]')
    assert clone_ten.locator(".checkouts-default .checkouts-basis").inner_text() == "cached"

    checked = view_checkouts.document_checked
    serve_checkouts(page, double, checked)
    assert checkout_group(checked, "remote")["remote"]["check"]["status"] == "live"
    assert page.locator(".checkouts-group-check").first.inner_text() == "checked live"
    clone_ten = page.locator('tr.checkouts-clone[data-clone="Repo Clone 10"]')
    assert clone_ten.locator(".checkouts-default .checkouts-basis").inner_text() == "live"
    assert "cached, never fetched" not in clone_ten.inner_text()

    recorded = view_checkouts.document_recorded
    serve_checkouts(page, double, recorded)
    check = checkout_group(recorded, "remote")["remote"]["check"]
    assert check["status"] == "recorded"
    when = checkouts_local_time(check["checked_at"], recorded["observed_at"], seconds=False)
    clone_ten = page.locator('tr.checkouts-clone[data-clone="Repo Clone 10"]')
    assert clone_ten.locator(".checkouts-default .checkouts-basis").inner_text() == (
        f"recorded {when}")
    assert page.locator(".checkouts-group-check").first.inner_text() == f"recorded {when}"

    renamed = view_checkouts.document_renamed
    serve_checkouts(page, double, renamed)
    check = checkout_group(renamed, "remote")["remote"]["check"]
    when = checkouts_local_time(check["checked_at"], renamed["observed_at"], seconds=False)
    clone_ten = page.locator('tr.checkouts-clone[data-clone="Repo Clone 10"]')
    note = clone_ten.locator(".checkouts-default .checkouts-note").inner_text()
    assert note == "(latest check failed: remote repository not found)"
    assert page.locator(".checkouts-group-check").first.inner_text() == (
        f"recorded {when} (remote repository not found)")


def test_checkouts_quiet_repositories_collapse_into_one_row(
    open_page, view_checkouts
):
    """A quiet repository is one row while the box is checked, a group when not."""
    document = view_checkouts.document_built
    quiet = checkout_group(document, "quiet")
    quiet_display = quiet["remote"]["display"]
    other_display = checkout_group(document, "remote")["remote"]["display"]
    page, double = open_view_checkouts(open_page, view_checkouts, document)

    def groups():
        return page.locator(".checkouts-group-name").all_inner_texts()

    assert groups() == [other_display]
    row = page.locator("#checkouts-body .checkouts-quiet")
    playwright.expect(row).to_have_count(1)
    assert row.locator("summary").inner_text() == "Quiet repositories (1)"
    row.locator("summary").click()
    assert row.locator("li").inner_text() == f"{quiet_display} Quiet Clone"

    page.get_by_label("Hide quiet checkouts", exact=True).uncheck()
    assert groups() == [quiet_display, other_display]
    assert page.locator("#checkouts-body .checkouts-quiet").count() == 0

    # The unchecked state survives a reload.  The reload's own catalog and
    # checkout requests must settle before a tick can answer, or the tick is
    # dropped for a request still in flight.
    page.reload()
    page.wait_for_function(
        "() => window.catalogResponses.length >= 1 && window.checkoutsResponses.length >= 1")
    settle_frames(page)
    page.wait_for_function(
        "() => document.querySelectorAll('#checkouts-body .checkouts-group').length === 2")
    assert not page.get_by_label("Hide quiet checkouts", exact=True).is_checked()
    assert groups() == [quiet_display, other_display]

    # Checked again, a stash makes the quiet clone ordinary: its own group, no row.
    page.get_by_label("Hide quiet checkouts", exact=True).check()
    assert groups() == [other_display]
    serve_checkouts(page, double, view_checkouts.document_quiet_stashed)
    assert groups() == [quiet_display, other_display]
    assert page.locator("#checkouts-body .checkouts-quiet").count() == 0

    # Two null comparisons prove nothing about equality, so it is not quiet.
    def both_null(value):
        _, clone = checkout_clone(value, "Quiet Clone")
        clone["local_default"] = None

    serve_checkouts(
        page, double, checkout_variant(view_checkouts.document_built, both_null))
    assert groups() == [quiet_display, other_display]
    assert page.locator("#checkouts-body .checkouts-quiet").count() == 0


def test_checkouts_search_shows_only_the_matching_group(open_page, view_checkouts):
    """The shared search hides every group whose served names do not match."""
    document = view_checkouts.document_built
    page, double = open_view_checkouts(open_page, view_checkouts, document)
    page.get_by_label("Hide quiet checkouts", exact=True).uncheck()
    assert page.locator(".checkouts-group").count() == len(document["repositories"])
    page.fill("#filter", "topic-a")
    visible = page.locator(".checkouts-group:not([hidden]) .checkouts-group-name")
    assert visible.all_inner_texts() == [
        checkout_group(document, "remote")["remote"]["display"]]
    page.fill("#filter", "")
    assert page.locator(".checkouts-group:not([hidden])").count() == len(
        document["repositories"])


def test_checkouts_search_matches_the_repositories_in_the_quiet_row(
    open_page, view_checkouts
):
    """The quiet row stays for a held repository that matches and goes when none do."""
    document = view_checkouts.document_built
    quiet = checkout_group(document, "quiet")
    quiet_display = quiet["remote"]["display"]
    other_display = checkout_group(document, "remote")["remote"]["display"]
    page, _ = open_view_checkouts(open_page, view_checkouts, document)
    row = page.locator("#checkouts-body .checkouts-quiet")
    summary = row.locator("summary")
    groups = page.locator(".checkouts-group:not([hidden]) .checkouts-group-name")
    playwright.expect(row).to_be_visible()
    assert summary.inner_text() == "Quiet repositories (1)"

    # A quiet repository's name keeps the row, counts its match and hides the
    # group that does not match.
    page.fill("#filter", quiet_display)
    playwright.expect(row).to_be_visible()
    assert summary.inner_text() == "Quiet repositories (1, 1 matching)"
    assert groups.all_inner_texts() == []

    # The clone's folder name is searched as it is for a group.
    page.fill("#filter", "quiet clone")
    playwright.expect(row).to_be_visible()

    # A name no quiet repository has hides the row; the group that has it stays.
    page.fill("#filter", other_display)
    playwright.expect(row).to_be_hidden()
    assert groups.all_inner_texts() == [other_display]
    page.fill("#filter", "no such words anywhere")
    playwright.expect(row).to_be_hidden()
    assert groups.all_inner_texts() == []

    # Clearing the search restores the earlier state.
    page.fill("#filter", "")
    playwright.expect(row).to_be_visible()
    assert summary.inner_text() == "Quiet repositories (1)"
    assert groups.all_inner_texts() == [other_display]


def test_checkouts_search_can_match_a_group_and_the_quiet_row_together(
    open_page, view_checkouts
):
    """A query both kinds hold keeps the group and the row; one kind alone keeps one."""
    document = view_checkouts.document_built
    quiet_group = checkout_group(document, "quiet")
    other = checkout_group(document, "remote")
    page, _ = open_view_checkouts(open_page, view_checkouts, document)
    row = page.locator("#checkouts-body .checkouts-quiet")
    groups = page.locator(".checkouts-group:not([hidden]) .checkouts-group-name")
    branch = quiet_group["default_branch"]["name"]
    assert other["default_branch"]["name"] == branch

    page.fill("#filter", branch)
    playwright.expect(row).to_be_visible()
    assert groups.all_inner_texts() == [other["remote"]["display"]]

    # Words only the group holds hide the row; words only the row holds hide the group.
    page.fill("#filter", "topic-a")
    playwright.expect(row).to_be_hidden()
    assert groups.all_inner_texts() == [other["remote"]["display"]]
    page.fill("#filter", quiet_group["remote"]["display"])
    playwright.expect(row).to_be_visible()
    assert groups.all_inner_texts() == []

    # The row, opened and filtered, keeps its listed repository.
    row.locator("summary").click()
    playwright.expect(row.locator("li:not([hidden])")).to_have_count(1)


def mutate_searchable_words(document):
    """A quiet clone with a detached, edited tree and a detached linked worktree."""
    verdict = {
        "state": "merged", "rung": "ancestor", "reason": None, "pull_request": None,
    }
    _, quiet = checkout_clone(document, "Quiet Clone")
    main = next(item for item in quiet["worktrees"] if item["main"])
    main["head"] = {"branch": None, "sha": "abc1234" + "0" * 33}
    main["verdict"] = dict(verdict)
    main["changes"]["unstaged"] = 3
    _, clone_two = checkout_clone(document, "Repo Clone 2")
    linked = checkout_worktree(clone_two, "outside")
    linked["head"] = {"branch": None, "sha": "fedcba9" + "0" * 33}
    linked["verdict"] = dict(verdict)


def test_checkouts_search_matches_the_words_each_row_shows(open_page, view_checkouts):
    """A Code Status phrase, availability, HEAD and tree word or a worktree's HEAD finds its group."""
    document = checkout_variant(view_checkouts.document_built, mutate_searchable_words)
    page, _ = open_view_checkouts(open_page, view_checkouts, document)
    page.get_by_label("Hide quiet checkouts", exact=True).uncheck()
    remote = checkout_group(document, "remote")["remote"]["display"]
    quiet = checkout_group(document, "quiet")["remote"]["display"]

    def shown(query):
        page.fill("#filter", query)
        return page.locator(
            ".checkouts-group:not([hidden]) .checkouts-group-name").all_inner_texts()

    assert sorted(shown("")) == sorted([remote, quiet])
    # The served Code Status text of a work item the group serves.
    assert shown("in progress · pushed") == [remote]
    # The availability words of a clone row.
    assert shown("has linked worktrees") == [remote]
    # The HEAD words of the quiet clone's main worktree, and its tree words.
    assert shown("detached at abc1234") == [quiet]
    assert shown("3 unstaged") == [quiet]
    # A linked worktree's HEAD words.
    assert shown("detached at fedcba9") == [remote]
    assert shown("no such words anywhere") == []


# ---------------------------------------------------------------------------
# The browser: one assertion per word table
# ---------------------------------------------------------------------------


def mutate_check_failed(document):
    """Every group's remote check failed with nothing saved to fall back on."""
    document["remote_checks"] = "on"
    for group in document["repositories"]:
        group["remote"]["check"] = {"status": "failed", "reason": "not_found"}


def mutate_clone_statuses(document):
    """Two clones that could not be read, in the two ways the words name."""
    _, clone_three = checkout_clone(document, "Repo Clone 3")
    set_clone_status(clone_three, "unsafe_ownership")
    _, clone_ten = checkout_clone(document, "Repo Clone 10")
    set_clone_status(clone_ten, "too_many_branches")


def mutate_worktree_facts(document):
    """A detached dirty main worktree in a rebase beside one locked worktree."""
    _, clone_two = checkout_clone(document, "Repo Clone 2")
    main = next(item for item in clone_two["worktrees"] if item["main"])
    linked = checkout_worktree(clone_two, "outside")
    linked["locked"] = True
    main["operations"] = ["rebase"]
    main["changes"]["staged"] = 2
    main["head"] = {"branch": None, "sha": main["head"]["sha"]}
    main["verdict"] = {
        "state": "merged", "rung": "ancestor", "reason": None, "pull_request": None,
    }


def mutate_prunable_worktree(document):
    """A linked worktree whose directory is gone."""
    _, clone_two = checkout_clone(document, "Repo Clone 2")
    linked = checkout_worktree(clone_two, "outside")
    linked["prunable"] = True
    linked["status"] = "failed"
    for field in ("head", "changes", "operations", "index_lock", "verdict"):
        linked[field] = None


def mutate_checkout_flags(document):
    """One bare clone, one shallow clone and one partial clone."""
    _, clone_three = checkout_clone(document, "Repo Clone 3")
    clone_three["bare"] = True
    clone_three["worktrees"] = []
    for branch in clone_three["branches"]:
        branch["checked_out_in"] = []
    _, clone_ten = checkout_clone(document, "Repo Clone 10")
    clone_ten["shallow"] = True
    clone_ten["containment"] = "incomplete"
    _, clone_two = checkout_clone(document, "Repo Clone 2")
    clone_two["partial_clone"] = True
    clone_two["containment"] = "incomplete"


def mutate_containment(document):
    """One clone whose branch folding ran out of time."""
    _, clone_three = checkout_clone(document, "Repo Clone 3")
    clone_three["containment"] = "deadline"


def mutate_assignment(document):
    """A package assigned to another clone and one to a linked worktree."""
    _, clone_two = checkout_clone(document, "Repo Clone 2")
    _, clone_three = checkout_clone(document, "Repo Clone 3")
    outside = checkout_worktree(clone_two, "outside")["path"]
    packages = {item["package_path"]: item for item in document["packages"]}
    alpha = packages["Repo/Drafts/alpha"]
    alpha["target"] = {
        "clone": clone_three["path"], "status": "clone", "worktree": None,
        "within_folders": alpha["target"]["within_folders"],
    }
    topic_a = next(branch for branch in clone_two["branches"] if branch["name"] == "topic-a")
    topic_a["packages"] = [{"package_path": "Repo/Drafts/alpha", "basis": "sibling_clone"}]
    delta = packages["Repo/Drafts/delta"]
    delta["target"] = {
        "clone": clone_two["path"], "status": "worktree", "worktree": outside,
        "within_folders": delta["target"]["within_folders"],
    }


def mutate_lookup_and_pull_requests(document):
    """A live lookup with one open and one merged pull request on the branch."""
    document["github_lookup"] = "on"
    group = checkout_group(document, "remote")
    group["remote"]["key"] = "github.com/example/example"
    group["remote"]["display"] = "example/example"
    group["pr_lookup"] = {"status": "live", "reason": None, "checked_at": None}
    for other in document["repositories"]:
        if other is not group:
            other["pr_lookup"] = {
                "status": "off", "reason": "not_github", "checked_at": None,
            }
    _, clone_two = checkout_clone(document, "Repo Clone 2")
    topic_a = next(branch for branch in clone_two["branches"] if branch["name"] == "topic-a")
    topic_a["verdict"] = {
        "state": "merged", "rung": "pull_request", "reason": None, "pull_request": 7,
    }
    topic_a["pull_requests"] = [
        {"number": 3, "state": "open", "head_matches": False},
        {"number": 7, "state": "merged", "head_matches": True},
    ]


def mutate_merged_branch(document):
    """The package branch proved merged because its tip is in the default branch."""
    _, clone_two = checkout_clone(document, "Repo Clone 2")
    topic_a = next(branch for branch in clone_two["branches"] if branch["name"] == "topic-a")
    topic_a["verdict"] = {
        "state": "merged", "rung": "ancestor", "reason": None, "pull_request": None,
    }


def test_checkouts_word_tables_show_their_served_values(
    open_page, view_checkouts
):
    """Every word table renders a served value's words, from captures and variants."""
    document = view_checkouts.document_built
    page, double = open_view_checkouts(open_page, view_checkouts, document)
    body = page.locator("#checkouts-body")

    def clone_row(name):
        return body.locator(f'tr.checkouts-clone[data-clone="{name}"]')

    def shows(text):
        assert text in body.inner_text(), text

    # The built capture: comparison relation and basis, availability state and
    # reason, the assignment basis, status, remote copy, verdict state, the
    # branch blocker, the branch package basis and the check/lookup words.
    shows("up to date")
    shows("cached")
    shows("Free")
    clone_two = clone_row("Repo Clone 2")
    availability = clone_two.locator(".checkouts-availability").inner_text()
    assert availability.startswith("In use")
    assert "has linked worktrees" in availability
    assert "Assigned: Repo/Drafts/alpha (Target repo)" in availability
    shows("unknown")
    shows("not merged")
    shows("Repo/Drafts/alpha (Target repo)")
    assert "topic-a (not merged)" in clone_two.locator(".checkouts-cleanup").inner_text()
    assert page.locator(".checkouts-group-check").first.inner_text() == "remote checks off"
    assert page.locator(".checkouts-group-lookup").first.inner_text() == (
        "pull request lookup off")
    # The default branch is named in the header as it is served.
    assert body.locator(".checkouts-group-default").last.inner_text() == "trunk"

    # One remote check later: the live basis and the present and absent copies.
    checked = view_checkouts.document_checked
    serve_checkouts(page, double, checked)
    assert page.locator(".checkouts-group-check").first.inner_text() == "checked live"
    shows("on the remote (live)")
    shows("not on the remote")
    shows("live")

    # A saved check from a later local collection, and one that then failed.
    recorded = view_checkouts.document_recorded
    serve_checkouts(page, double, recorded)
    check = checkout_group(recorded, "remote")["remote"]["check"]
    shows(f"recorded {checkouts_local_time(check['checked_at'], recorded['observed_at'], seconds=False)}")
    serve_checkouts(page, double, view_checkouts.document_renamed)
    shows("(latest check failed: remote repository not found)")

    # A check that failed with nothing saved falls back to nothing.
    serve_checkouts(
        page, double, checkout_variant(document, mutate_check_failed))
    assert page.locator(".checkouts-group-check").first.inner_text() == (
        "check failed (remote repository not found)")

    # Clone status words, and the Availability a clone without facts shows.
    serve_checkouts(
        page, double, checkout_variant(document, mutate_clone_statuses))
    shows("git refused it: ownership")
    shows("more than 100 local branches; not examined")
    assert clone_row("Repo Clone 3").locator(
        ".checkouts-availability").inner_text() == "Unknown"

    # Worktree facts: a detached, dirty main worktree in a rebase, and a locked
    # linked worktree with its branch and worktree blockers.
    serve_checkouts(
        page, double, checkout_variant(document, mutate_worktree_facts))
    clone_two = clone_row("Repo Clone 2")
    assert clone_two.locator(".checkouts-head").inner_text().startswith("detached at ")
    shows("detached HEAD")
    shows("2 staged")
    shows("rebase in progress")
    shows("git operation in progress")
    assert "outside (locked)" in clone_two.locator(".checkouts-cleanup").inner_text()
    linked = body.locator('tr.checkouts-worktree[data-worktree="outside"]')
    assert linked.locator(".checkouts-cleanup").inner_text() == "locked"

    # A prunable linked worktree: the status words and the flag word.
    serve_checkouts(
        page, double, checkout_variant(document, mutate_prunable_worktree))
    shows("could not be read")
    shows("directory is gone")

    # Bare, shallow and partial clones name their facts once in the Checkout cell.
    serve_checkouts(
        page, double, checkout_variant(document, mutate_checkout_flags))
    bare = clone_row("Repo Clone 3")
    assert bare.locator("td.checkouts-head").inner_text() == "bare repository"
    assert bare.locator("td.checkouts-tree").inner_text() == "no working tree"
    assert "shallow" in clone_row("Repo Clone 10").locator(
        "th.checkouts-checkout").inner_text()
    assert "partial" in clone_row("Repo Clone 2").locator(
        "th.checkouts-checkout").inner_text()

    # A clone whose branch folding did not finish says why, once.
    serve_checkouts(
        page, double, checkout_variant(document, mutate_containment))
    shows("Branches were not checked for folding: ran out of time")

    # Another clone's assigned package and a linked worktree's, with both bases.
    serve_checkouts(
        page, double, checkout_variant(document, mutate_assignment))
    assigned = clone_row("Repo Clone 3").locator(".checkouts-availability").inner_text()
    assert assigned.startswith("Assigned")
    assert "Repo/Drafts/alpha (Target repo)" in assigned
    assert "Repo/Drafts/delta (Target repo worktree)" in clone_row(
        "Repo Clone 2").locator(".checkouts-availability").inner_text()
    shows("same branch in another clone")

    # A live lookup with served pull requests and a pull-request rung.
    serve_checkouts(
        page, double, checkout_variant(checked, mutate_lookup_and_pull_requests))
    shows("pull requests live")
    assert body.locator('table.checkouts-package-branches tr[data-branch="topic-a"]'
                        " .checkouts-verdict-words").inner_text() == (
        "merged — pull request #7 merged")
    pulls = body.locator('table.checkouts-package-branches tr[data-branch="topic-a"]'
                         " .checkouts-pulls").inner_text()
    assert pulls == "#3 open (different head) · #7 merged"
    assert "Pull requests live" in page.locator("#checkouts-status-line").inner_text()

    # A branch proved merged by its tip being in the default branch.
    serve_checkouts(
        page, double, checkout_variant(document, mutate_merged_branch))
    assert body.locator('table.checkouts-package-branches tr[data-branch="topic-a"]'
                        " .checkouts-verdict-words").inner_text() == "merged — tip is in trunk"


def test_checkouts_missing_word_renders_the_raw_code(open_page, view_checkouts):
    """A value newer than this page's words renders as itself and never throws."""
    document = checkout_raw_variant(
        view_checkouts.document_built,
        lambda value: set_clone_status(
            checkout_clone(value, "Repo Clone 10")[1], "future_status"),
    )
    # The value is one a newer collector would serve before this page learns its
    # words, so the strict parser refuses it and only the provider double can
    # answer with it; the page must still show it rather than break.
    with pytest.raises(models.ProtocolError):
        models.parse_checkouts(json.dumps(document))
    page, double = open_view_checkouts(open_page, view_checkouts, document)
    body = page.locator("#checkouts-body")
    assert "future_status" in body.inner_text()
    # The rest of the view still renders.
    assert body.locator("tr.checkouts-clone").count() == 3
    assert "Repo Clone 2" in body.inner_text()


def test_checkouts_show_on_board_reveals_one_work_item(open_page, view_checkouts):
    """Each work item's table action shows its card, or says why it cannot."""
    document = view_checkouts.document_built
    page, double = open_view_checkouts(open_page, view_checkouts, document)
    page.get_by_label("Hide quiet checkouts", exact=True).uncheck()
    remote_view = page.locator(".checkouts-group").nth(group_index(document, "remote"))
    topic_a = remote_view.locator(
        'table.checkouts-package-branches tr[data-branch="topic-a"]')
    topic_a.locator('[data-show-on-board="Repo/Drafts/alpha"]').click()
    playwright.expect(page.locator("#board")).to_be_visible()
    playwright.expect(page.locator("#checkouts")).to_be_hidden()
    card = page.locator('.card[data-package-path="Repo/Drafts/alpha"]')
    playwright.expect(card).to_have_class(re.compile(r"\bselected\b"))
    assert page.evaluate(
        "() => document.activeElement.dataset.packagePath") == "Repo/Drafts/alpha"

    # A work item in a finished row the board is hiding says so and stays put.
    show_checkouts_view(page)
    remote_view = page.locator(".checkouts-group").nth(group_index(document, "remote"))
    topic_c = remote_view.locator(
        'table.checkouts-package-branches tr[data-branch="topic-c"]')
    topic_c.locator('[data-show-on-board="Repo/Shipped/gamma"]').click()
    playwright.expect(page.locator("#checkouts")).to_be_visible()
    playwright.expect(page.locator("#board")).to_be_hidden()
    assert page.locator("#checkouts-action-status").inner_text() == (
        "This work item is in a finished row; uncheck Hide terminal rows to show it.")


def test_checkouts_status_line_names_a_scan_from_other_settings(
    open_page, view_checkouts
):
    """A scan collected under other settings than the board shown says so once."""
    document = view_checkouts.document_built
    page, double = open_view_checkouts(open_page, view_checkouts, document)
    sentence = "Collected under different settings from the board shown."
    line = page.locator("#checkouts-status-line")
    assert sentence not in line.inner_text()

    def other_settings(value):
        value["configuration_revision"] = "3" * 64

    serve_checkouts(page, double, checkout_variant(document, other_settings))
    assert line.inner_text().endswith(sentence)
    # The values are still rendered: the scan is its own dated answer.
    assert page.locator("#checkouts-body .checkouts-group").count() >= 1


def test_checkouts_timed_out_worktrees_and_unborn_heads_show_their_words(
    open_page, view_checkouts
):
    """A main worktree git did not answer for fills HEAD and Tree and is never nested."""

    def unanswered(worktree):
        worktree["status"] = "timed_out"
        for field in ("head", "changes", "operations", "index_lock", "verdict"):
            worktree[field] = None

    def mutate(document):
        _, clone_two = checkout_clone(document, "Repo Clone 2")
        unanswered(next(item for item in clone_two["worktrees"] if item["main"]))
        unanswered(checkout_worktree(clone_two, "outside"))

    document = checkout_variant(view_checkouts.document_built, mutate)
    page, _ = open_view_checkouts(open_page, view_checkouts, document)
    clone_two = page.locator('tr.checkouts-clone[data-clone="Repo Clone 2"]')
    assert clone_two.locator("td.checkouts-head").inner_text() == "git did not answer in time"
    assert clone_two.locator("td.checkouts-tree").inner_text() == "git did not answer in time"
    # The main worktree is never a nested row; the linked one shows the same words.
    assert page.locator("tr.checkouts-worktree").evaluate_all(
        "rows => rows.map(row => row.dataset.worktree)") == ["outside"]
    linked = page.locator('tr.checkouts-worktree[data-worktree="outside"]')
    assert linked.locator("td.checkouts-head").inner_text() == "git did not answer in time"
    assert linked.locator("td.checkouts-tree").inner_text() == "git did not answer in time"

    # A linked worktree on a branch with no commit reads "unborn <branch>".
    def unborn(value):
        _, clone = checkout_clone(value, "Repo Clone 2")
        checkout_worktree(clone, "outside")["head"] = {"branch": "outside", "sha": None}

    served = checkout_raw_variant(view_checkouts.document_built, unborn)
    page, _ = open_view_checkouts(open_page, view_checkouts, served)
    assert page.locator(
        'tr.checkouts-worktree[data-worktree="outside"] td.checkouts-head'
    ).inner_text() == "unborn outside"


def test_checkouts_cached_fetch_age_rounds_down_against_the_observation(
    open_page, view_checkouts
):
    """A cached value names the clone's fetch age once, in whole units."""
    document = view_checkouts.document_built
    observed = datetime.fromisoformat(document["observed_at"].replace("Z", "+00:00"))

    def fetched(minutes):
        def mutate(value):
            _, clone = checkout_clone(value, "Repo Clone 2")
            moment = observed - timedelta(minutes=minutes, seconds=30)
            clone["cached_default"]["last_fetched_at"] = moment.strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            clone["cached_default"]["fetched_since_clone"] = True
        return checkout_variant(document, mutate)

    page, double = open_view_checkouts(open_page, view_checkouts, fetched(0))
    row = page.locator('tr.checkouts-clone[data-clone="Repo Clone 2"] .checkouts-freshness')
    assert row.inner_text() == "cached, fetched <1 min ago"
    for minutes, words in ((7, "7 min"), (125, "2 h"), (60 * 49, "2 d")):
        serve_checkouts(page, double, fetched(minutes))
        assert row.inner_text() == f"cached, fetched {words} ago"


def test_checkouts_branches_cell_counts_stashes_in_words(open_page, view_checkouts):
    """The Branches cell pluralizes the stash count it shows ("&lt;K&gt; stashes")."""

    def stashed(count):
        def mutate(document):
            _, clone = checkout_clone(document, "Repo Clone 2")
            clone["stash_count"] = count
        return checkout_variant(view_checkouts.document_built, mutate)

    page, double = open_view_checkouts(open_page, view_checkouts, stashed(1))
    cell = page.locator('tr.checkouts-clone[data-clone="Repo Clone 2"] .checkouts-branches')
    assert cell.inner_text() == "3 branches · 1 stash"
    assert "1 stashs" not in page.locator("#checkouts-body").inner_text()
    serve_checkouts(page, double, stashed(2))
    assert cell.inner_text() == "3 branches · 2 stashes"
    assert "stashs" not in page.locator("#checkouts-body").inner_text()
    # The singular of the cleanup counts reads the same way in the same table.
    cleanup = page.locator('tr.checkouts-clone[data-clone="Repo Clone 2"] .checkouts-cleanup')
    assert cleanup.inner_text().startswith("1 branch, 1 worktree removable")


def test_checkouts_show_on_board_keeps_a_matching_search(open_page, view_checkouts):
    """A search survives the jump unless it is what hides the card."""
    document = view_checkouts.document_built
    page, double = open_view_checkouts(open_page, view_checkouts, document)
    page.get_by_label("Hide quiet checkouts", exact=True).uncheck()

    def show_button():
        remote_view = page.locator(".checkouts-group").nth(group_index(document, "remote"))
        return remote_view.locator(
            'table.checkouts-package-branches tr[data-branch="topic-a"] '
            '[data-show-on-board="Repo/Drafts/alpha"]')

    # A query that matches the card is kept across the jump.
    page.fill("#filter", "alpha")
    show_button().click()
    playwright.expect(page.locator("#board")).to_be_visible()
    assert page.locator("#filter").input_value() == "alpha"
    assert page.locator('.card[data-package-path="Repo/Drafts/alpha"]').is_hidden() is False

    # A query that hides the card is cleared before the jump.  The group stays
    # listed by its other clone's name while this work item's card does not
    # match, so the table row is reachable with the hiding query in place.
    show_checkouts_view(page)
    page.fill("#filter", "Repo Clone 10")
    show_button().click()
    playwright.expect(page.locator("#board")).to_be_visible()
    assert page.locator("#filter").input_value() == ""
    assert page.evaluate(
        "() => document.activeElement.dataset.packagePath") == "Repo/Drafts/alpha"


def test_checkouts_git_problem_escapes_the_version_once(open_page, view_checkouts):
    """A git version carrying markup is escaped exactly once in the setup panel."""
    document = git_problem_variant(view_checkouts, "2.36&1")
    page, _ = open_view_checkouts(open_page, view_checkouts, document)
    item = page.locator('#checkouts-body [data-setup="git"]')
    assert item.inner_text() == "Git 2.36&1 is older than Nyx supports (2.36)"
    assert "&amp;" in item.inner_html()
    assert "&amp;amp;" not in item.inner_html()
    # The rest of the panel keeps its order and words.
    kinds = page.locator("#checkouts-body .checkouts-setup-item").evaluate_all(
        "items => items.map(item => item.dataset.setup)")
    assert kinds == ["git", "folder", "diagnostic"]


# ---------------------------------------------------------------------------
# Needs attention, expanded rows, Show on board and the settings sentence
# ---------------------------------------------------------------------------

ATTENTION_PACKAGE_IDS = {
    "alpha": "123e4567-e89b-42d3-a456-426614174b01",
    "gamma": "123e4567-e89b-42d3-a456-426614174b03",
    "zeta": "123e4567-e89b-42d3-a456-426614174b06",
}


@pytest.fixture(scope="module")
def attention_checkouts(tmp_path_factory):
    """One real checkout world that holds every attention code the view navigates.

    A bare ``trunk`` remote is cloned three times into one folder.  ``Repo Clone 2``
    holds ``topic-a`` (pushed, then one more local commit), ``topic-a-part`` (a
    branch whose tip ``topic-a`` holds), ``topic-merged`` (merged with
    ``--no-ff``), ``topic-squash`` (``merge --squash`` plus a commit), a pushed
    finished-stage branch, and a linked worktree with a change.  ``Repo Clone 3``
    holds a branch never pushed and a stash; ``Repo Clone 10`` never fetched.  One
    work item has a Baseline trunk has moved past and one names a Target repo that
    does not exist; ``catalog`` is built before that one is written, so a board
    shown from it does not hold it, and ``catalog_all`` holds every work item.  The captures are the world as built and the same world
    with no finished stage.
    """
    root = tmp_path_factory.mktemp("attention-checkouts")
    folder = root / "checkouts"
    folder.mkdir()
    served = root / "served"
    served.mkdir()
    (root / "home").mkdir()
    (root / "home" / ".nyx").mkdir()
    spec_root = root / "specification-root"
    with pytest.MonkeyPatch.context() as patch:
        isolate_git(patch, root / "home")
        remote = make_remote(served)
        clone_two = clone(remote.as_uri(), folder / "Repo Clone 2")
        clone_three = clone(remote.as_uri(), folder / "Repo Clone 3")
        clone(remote.as_uri(), folder / "Repo Clone 10")
        git(clone_two, "fetch", "-q")
        git(clone_three, "fetch", "-q")
        seed = git(clone_two, "rev-parse", "trunk").stdout.strip()

        git(clone_two, "checkout", "-q", "-b", "topic-a")
        commit_file(clone_two, "alpha.txt", "alpha one\n")
        git(clone_two, "push", "-q", "-u", "origin", "topic-a")
        git(clone_two, "branch", "topic-a-part")
        commit_file(clone_two, "alpha.txt", "alpha two\n")
        git(clone_two, "checkout", "-q", "trunk")
        git(clone_two, "checkout", "-q", "-b", "topic-merged")
        commit_file(clone_two, "merged.txt", "merged\n")
        git(clone_two, "checkout", "-q", "trunk")
        git(clone_two, "merge", "-q", "--no-ff", "-m", "merge topic-merged", "topic-merged")
        git(clone_two, "checkout", "-q", "-b", "topic-squash")
        commit_file(clone_two, "squash.txt", "squash\n")
        git(clone_two, "checkout", "-q", "trunk")
        git(clone_two, "merge", "-q", "--squash", "topic-squash")
        git(clone_two, "commit", "-q", "-m", "squash topic-squash")
        git(clone_two, "checkout", "-q", "-b", "topic-g", "trunk")
        commit_file(clone_two, "gamma.txt", "gamma\n")
        git(clone_two, "push", "-q", "-u", "origin", "topic-g")
        git(clone_two, "checkout", "-q", "trunk")
        git(clone_two, "push", "-q", "origin", "trunk")
        git(clone_two, "worktree", "add", "-q", str(root / "linked"), "-b", "wt-branch")
        (root / "linked" / "dirty.txt").write_bytes(b"dirty\n")

        git(clone_three, "pull", "-q")
        git(clone_three, "checkout", "-q", "-b", "local-only")
        commit_file(clone_three, "local.txt", "local\n")
        git(clone_three, "checkout", "-q", "trunk")
        commit_file(clone_three, "stash.txt", "stash one\n")
        git(clone_three, "reset", "-q", "--hard", "origin/trunk")
        (clone_three / "file.txt").write_bytes(b"stashed\n")
        git(clone_three, "stash", "push", "-q")

        target = [f"Target repo: {clone_two}"]
        _package_spec(spec_root, "Drafts", "alpha", ATTENTION_PACKAGE_IDS["alpha"],
                      ["Branch: topic-a", f"Baseline: {seed}", *target])
        _package_spec(spec_root, "Shipped", "gamma", ATTENTION_PACKAGE_IDS["gamma"],
                      ["Branch: topic-g", *target])
        catalog = json.loads(scan_catalog(
            spec_root,
            completed_stage_names=["Shipped"],
            configuration_revision=CHECKOUTS_REVISION,
        ))
        _package_spec(spec_root, "Drafts", "zeta", ATTENTION_PACKAGE_IDS["zeta"],
                      ["Branch: topic-z", f"Target repo: {folder / 'Missing Clone'}"])
        catalog_all = json.loads(scan_catalog(
            spec_root,
            completed_stage_names=["Shipped"],
            configuration_revision=CHECKOUTS_REVISION,
        ))

        def collect(stages):
            return json.loads(collect_checkouts(
                [str(folder)],
                deadline_ns=time.monotonic_ns() + 60_000_000_000,
                package_scan=PackageScan(spec_root, stages),
                configuration_revision=CHECKOUTS_REVISION,
            ))

        document = collect(("Shipped",))
        document_no_finished = collect(())
    return SimpleNamespace(
        folder=folder, spec_root=spec_root, linked=root / "linked", catalog=catalog,
        catalog_all=catalog_all,
        document=document, document_no_finished=document_no_finished,
    )


def test_checkouts_attention_fixture_holds_every_code_it_names(attention_checkouts):
    """Both captures parse, and the world holds the codes the proofs navigate."""
    for document in (attention_checkouts.document, attention_checkouts.document_no_finished):
        models.parse_checkouts(json.dumps(document))
    assert [item["code"] for item in attention_checkouts.document["attention"]] == [
        "branch_unpushed", "branch_not_pushed", "stash", "uncommitted_changes",
        "awaiting_merge", "removable", "base_moved", "target_repo_missing", "never_fetched",
    ]
    assert attention_checkouts.document_no_finished["attention"][-1]["code"] == \
        "finished_stages_not_configured"
    _, clone_two = checkout_clone(attention_checkouts.document, "Repo Clone 2")
    branches = {branch["name"]: branch for branch in clone_two["branches"]}
    assert branches["topic-a-part"]["contained_in"] == "topic-a"
    assert branches["topic-merged"]["verdict"]["rung"] == "ancestor"
    assert branches["topic-a"]["verdict"]["state"] == "not_merged"
    assert branches["wt-branch"]["removable"]["blockers"] == ["worktree_not_clean"]


def open_attention_checkouts(launch, attention_checkouts, document, *, catalog=None, client=None):
    """Open the checkouts view over the attention world's board and ``document``."""
    double = BrowserCheckouts(document)
    page = checkouts_view_page(
        launch,
        SimpleNamespace(
            catalog=attention_checkouts.catalog_all if catalog is None else catalog,
            revision=CHECKOUTS_REVISION,
        ),
        double,
        client=client,
    )
    return page, double


def attention_words(item):
    """The words one attention item must read, built from the view's own block."""
    words = checkout_words()
    values = {
        "branch": item["branch"] or "",
        "clone": Path(item["clone"]).name if item["clone"] else "",
        "worktree": Path(item["worktree"]).name if item["worktree"] else "",
        "count": "" if item["count"] is None else item["count"],
        "workItem": item["packages"][0] if item["packages"] else "",
    }
    if item["code"] == "uncommitted_changes" and item["count"] == 0:
        template = words["text"]["uncommittedOperation"]
    elif item["count"] == 1 and item["code"] in words["attentionCodeOne"]:
        template = words["attentionCodeOne"][item["code"]]
    else:
        template = words["attentionCode"][item["code"]]
    sentence = template.format(**values)
    if item["code"] == "awaiting_merge" and item["pull_request"] is not None:
        sentence += " " + words["text"]["awaitingMergePullRequest"].format(n=item["pull_request"])
    return sentence


def focused_checkouts_target(page):
    """What holds focus, described by the fields the view's rows carry."""
    return page.evaluate("""() => {
      const el = document.activeElement;
      const row = el.closest('tr.checkouts-clone');
      return {
        tag: el.tagName,
        cls: el.className,
        branch: el.dataset.branch || null,
        worktree: el.dataset.worktree || null,
        clone: row === null ? null : row.dataset.clone,
        packagePath: el.dataset.packagePath || null,
        expanded: el.getAttribute('aria-expanded'),
      };
    }""")


def focused_inside_viewport(page):
    """Whether the focused element lies below the sticky toolbar and above the fold."""
    box = page.evaluate("""() => {
      const rect = document.activeElement.getBoundingClientRect();
      return {
        top: rect.top, bottom: rect.bottom, height: window.innerHeight,
        bar: document.querySelector('.toolbar').getBoundingClientRect().bottom,
      };
    }""")
    return box["top"] >= box["bar"] - 1 and box["bottom"] <= box["height"] + 1


def test_checkouts_attention_list_follows_the_document(open_page, attention_checkouts):
    """Items equal the served array in order, with class headings and served counts."""
    document = attention_checkouts.document
    page, double = open_attention_checkouts(open_page, attention_checkouts, document)
    items = page.locator("#checkouts-body .checkouts-attention li[data-code]")
    assert items.evaluate_all("rows => rows.map(row => row.dataset.code)") == [
        item["code"] for item in document["attention"]]
    # A class heading appears exactly where the class changes in document order.
    classes = []
    for item in document["attention"]:
        if not classes or classes[-1] != item["class"]:
            classes.append(item["class"])
    headings = page.locator("#checkouts-body .checkouts-attention .checkouts-attention-class")
    assert headings.evaluate_all("nodes => nodes.map(node => node.dataset.class)") == classes
    assert headings.all_inner_texts() == [
        checkout_words()["attentionClass"][name] for name in classes]
    assert page.locator(".checkouts-attention h2").inner_text() == "Needs attention"
    # Each item reads its words with the served count.
    texts = items.evaluate_all(
        "rows => rows.map(row => row.querySelector("
        "'button.checkouts-attention-go, .checkouts-attention-words').innerText)")
    assert texts == [attention_words(item) for item in document["attention"]]
    assert texts[0] == "topic-a in Repo Clone 2 has 1 commit not on the remote"
    assert texts[4] == "topic-g is finished but not merged"
    # Items that name a clone or repository are controls; one that names only a
    # work item is text with that work item's Show on board.
    kinds = items.evaluate_all("rows => rows.map(row => row.dataset.attentionKind)")
    assert kinds == ["branch", "branch", "clone", "worktree", "package-branch", "clone",
                     "clone", "work-items", "clone"]
    missing = page.locator('li[data-code="target_repo_missing"]')
    assert missing.locator("button.checkouts-attention-go").count() == 0
    assert missing.locator("[data-show-on-board]").get_attribute("data-show-on-board") == \
        "Repo/Drafts/zeta"


def test_checkouts_attention_counts_read_singular_at_one_and_plural_otherwise(
    open_page, attention_checkouts
):
    """Each counted code says "1 commit" at one and "2 commits" at two."""
    document = copy.deepcopy(attention_checkouts.document)
    by_code = {item["code"]: item for item in document["attention"]}
    # The default-branch item has no row in this world; it reuses an item's destination.
    by_code["default_branch_ahead"] = copy.deepcopy(by_code["branch_unpushed"])
    by_code["default_branch_ahead"]["code"] = "default_branch_ahead"
    expected = {
        "branch_unpushed": ("topic-a in Repo Clone 2 has {} not on the remote",
                            "commit"),
        "default_branch_ahead": ("default branch topic-a in Repo Clone 2 has {} not on the remote",
                                 "commit"),
        "stash": ("Repo Clone 3 has {}", "stash"),
        "uncommitted_changes": ("linked has {}", "uncommitted change"),
        "removable": ("Repo Clone 2 has {} ready to remove", "branch or worktree"),
        "base_moved": ("the base of Repo/Drafts/alpha moved {} past its Baseline", "commit"),
    }
    plurals = {"commit": "commits", "stash": "stashes",
               "uncommitted change": "uncommitted changes",
               "branch or worktree": "branches or worktrees"}
    page, double = open_attention_checkouts(open_page, attention_checkouts, document)
    for count in (1, 2, 5):
        variant = copy.deepcopy(document)
        variant["attention"] = []
        for code in expected:
            item = copy.deepcopy(by_code[code])
            item["count"] = count
            variant["attention"].append(item)
        serve_checkouts(page, double, redigest(variant))
        for code, (template, noun) in expected.items():
            phrase = f"1 {noun}" if count == 1 else f"{count} {plurals[noun]}"
            sentence = page.locator(
                f'li[data-code="{code}"]').first.inner_text()
            assert sentence == template.format(phrase), (code, count)


def test_checkouts_attention_pull_request_joins_the_awaiting_merge_words(
    open_page, attention_checkouts
):
    """A finished item with an open pull request names it."""
    def open_pull_request(document):
        package = next(item for item in document["packages"]
                       if item["package_path"] == "Repo/Shipped/gamma")
        status = package["code_status"]
        status["state"] = "pr_open"
        status["pull_request"] = 7
        status["push"] = "pushed"
        status["text"] = models.code_status_text(status)

    document = checkout_variant(attention_checkouts.document, open_pull_request)
    page, _ = open_attention_checkouts(open_page, attention_checkouts, document)
    sentence = page.locator('li[data-code="awaiting_merge"] button').inner_text()
    assert sentence == "topic-g is finished but not merged (pull request #7)"
    assert sentence == attention_words(
        next(item for item in document["attention"] if item["code"] == "awaiting_merge"))


def test_checkouts_attention_items_move_by_keyboard_to_their_destination(
    open_page, attention_checkouts
):
    """Enter on an item opens, unfolds and focuses what it names, in view."""
    document = attention_checkouts.document
    page, double = open_attention_checkouts(open_page, attention_checkouts, document)

    def press(code):
        page.locator(f'li[data-code="{code}"] button').press("Enter")

    press("branch_unpushed")
    clone_two = page.locator('tr.checkouts-clone[data-clone="Repo Clone 2"]')
    assert clone_two.locator(".checkouts-toggle").get_attribute("aria-expanded") == "true"
    assert page.locator('tr.checkouts-detail[data-detail-for$="Repo Clone 2"]').is_visible()
    assert focused_checkouts_target(page)["branch"] == "topic-a"
    assert "checkouts-clone-branch" in focused_checkouts_target(page)["cls"]
    assert focused_inside_viewport(page)

    press("uncommitted_changes")
    target = focused_checkouts_target(page)
    assert (target["worktree"], target["cls"]) == ("linked", "checkouts-worktree")
    assert focused_inside_viewport(page)

    press("never_fetched")
    target = focused_checkouts_target(page)
    assert (target["clone"], target["cls"], target["expanded"]) == (
        "Repo Clone 10", "checkouts-toggle", "false")
    assert focused_inside_viewport(page)

    press("awaiting_merge")
    target = focused_checkouts_target(page)
    assert (target["branch"], target["cls"]) == ("topic-g", "checkouts-branch")
    assert page.evaluate("document.activeElement.closest('table').className") == \
        "checkouts-package-branches"
    assert focused_inside_viewport(page)

    # A clone-only item focuses that clone's toggle without opening it.
    for code, name in (("stash", "Repo Clone 3"), ("removable", "Repo Clone 2"),
                       ("base_moved", "Repo Clone 2")):
        press(code)
        target = focused_checkouts_target(page)
        assert (target["clone"], target["cls"]) == (name, "checkouts-toggle"), code

    # An item that names only a work item moves nothing.
    page.locator('li[data-code="target_repo_missing"] .checkouts-attention-words').click()
    assert focused_checkouts_target(page)["cls"] != "checkouts-attention-go"
    before = focused_checkouts_target(page)
    page.locator('li[data-code="target_repo_missing"] .checkouts-attention-words').click()
    assert focused_checkouts_target(page) == before
    playwright.expect(page.locator("#checkouts")).to_be_visible()

    # Without a finished stage, the policy item is words and no control.
    serve_checkouts(page, double, attention_checkouts.document_no_finished)
    policy = page.locator('li[data-code="finished_stages_not_configured"]')
    assert policy.inner_text() == (
        "No stage counts as finished; choose one under Counts as finished in Board settings")
    assert policy.locator("button, a, [tabindex]").count() == 0


def test_checkouts_attention_reaches_unreadable_clones_and_main_worktrees(
    open_page, attention_checkouts
):
    """A clone that cannot be read, or a worktree at the clone's own path, focuses the clone row."""
    page, double = open_attention_checkouts(
        open_page, attention_checkouts, attention_checkouts.document)

    def unreadable(document):
        _, clone = checkout_clone(document, "Repo Clone 10")
        set_clone_status(clone, "failed")

    serve_checkouts(page, double, checkout_variant(attention_checkouts.document, unreadable))
    page.locator('li[data-code="checkout_unavailable"] button').press("Enter")
    target = focused_checkouts_target(page)
    assert (target["clone"], target["cls"]) == ("Repo Clone 10", "checkouts-clone")
    assert page.locator(
        'tr.checkouts-clone[data-clone="Repo Clone 10"] .checkouts-toggle').count() == 0

    def own_worktree(document):
        _, clone = checkout_clone(document, "Repo Clone 3")
        main = next(worktree for worktree in clone["worktrees"] if worktree["main"])
        main["changes"]["untracked"] = 2

    variant = checkout_variant(attention_checkouts.document, own_worktree)
    serve_checkouts(page, double, variant)
    page.locator('li[data-code="uncommitted_changes"]').filter(
        has_text="Repo Clone 3 has 2 uncommitted changes").locator("button").press("Enter")
    target = focused_checkouts_target(page)
    assert (target["clone"], target["cls"]) == ("Repo Clone 3", "checkouts-clone")


def test_checkouts_attention_unfolds_a_folded_branch_before_focusing_it(
    open_page, attention_checkouts
):
    """A branch behind a fold is shown, with its fold open, when an item names it."""
    def mismatch(document):
        _, clone = checkout_clone(document, "Repo Clone 2")
        next(branch for branch in clone["branches"]
             if branch["name"] == "topic-a-part")["upstream_mismatch"] = True

    document = checkout_variant(attention_checkouts.document, mismatch)
    page, _ = open_attention_checkouts(open_page, attention_checkouts, document)
    page.locator('li[data-code="upstream_mismatch"] button').press("Enter")
    target = focused_checkouts_target(page)
    assert target["branch"] == "topic-a-part"
    assert page.locator('tr.checkouts-clone-branch[data-branch="topic-a"] '
                        '.checkouts-fold-toggle').get_attribute("aria-expanded") == "true"
    assert focused_inside_viewport(page)


def test_checkouts_attention_ignores_search_and_a_hidden_destination_clears_it(
    open_page, attention_checkouts
):
    """Every item stays listed under a search; moving to a hidden destination clears it."""
    document = attention_checkouts.document
    page, _ = open_attention_checkouts(open_page, attention_checkouts, document)
    page.fill("#filter", "zzz")
    items = page.locator("#checkouts-body li[data-code]")
    assert items.count() == len(document["attention"])
    assert page.locator(".checkouts-group").evaluate_all(
        "groups => groups.map(group => group.hidden)") == [True]
    page.locator('li[data-code="branch_unpushed"] button').press("Enter")
    assert page.locator("#filter").input_value() == ""
    assert page.locator(".checkouts-group").evaluate_all(
        "groups => groups.map(group => group.hidden)") == [False]
    assert focused_checkouts_target(page)["branch"] == "topic-a"

    # The package-branch destination clears it the same way.
    page.fill("#filter", "zzz")
    page.locator('li[data-code="awaiting_merge"] button').press("Enter")
    assert page.locator("#filter").input_value() == ""
    assert focused_checkouts_target(page)["branch"] == "topic-g"
    assert items.count() == len(document["attention"])


def share_a_branch_between_two_checkouts(document):
    """``topic-merged`` held by the main worktree and the linked one."""
    _, clone_two = checkout_clone(document, "Repo Clone 2")
    branch = next(item for item in clone_two["branches"] if item["name"] == "topic-merged")
    branch["checked_out_in"] = [worktree["path"] for worktree in clone_two["worktrees"]]


def open_clone_detail(page, name):
    """Open one clone row from its toggle and return its detail row."""
    row = page.locator(f'tr.checkouts-clone[data-clone="{name}"]')
    row.locator(".checkouts-toggle").click()
    detail = row.locator("xpath=following-sibling::tr[1]")
    playwright.expect(detail).to_be_visible()
    return detail


def served_verdict_words(group, verdict):
    """The words a served merge verdict must read, from the view's own block."""
    words = checkout_words()
    state = words["verdictState"][verdict["state"]]
    if verdict["state"] == "merged" and verdict["rung"] not in (None, "none"):
        rung = words["verdictRung"][verdict["rung"]].format(
            default=group["default_branch"]["name"], n=verdict["pull_request"] or "")
        return f"{state} — {rung}"
    return state


def test_checkouts_expanded_row_shows_its_branches_folds_blockers_and_work_items(
    open_page, attention_checkouts
):
    """An opened clone shows its path, branch table, folds, blockers and work items."""
    document = attention_checkouts.document
    group, clone = checkout_clone(document, "Repo Clone 2")
    page, double = open_attention_checkouts(open_page, attention_checkouts, document)
    detail = page.locator('tr.checkouts-detail[data-detail-for$="Repo Clone 2"]')
    assert not detail.is_visible()
    detail = open_clone_detail(page, "Repo Clone 2")
    assert detail.locator(".checkouts-clone-path-value").inner_text() == clone["path"]

    # The branch table follows the document's order; a branch another branch's
    # tip holds is folded under it until the fold is opened.
    rows = detail.locator("tr.checkouts-clone-branch")
    assert rows.evaluate_all("rows => rows.map(row => row.dataset.branch)") == [
        branch["name"] for branch in clone["branches"]]
    assert detail.locator("th[scope=col]").all_inner_texts()[:6] == [
        "Branch", "Checked out in", "Tip", "Remote", "Verdict", "Cleanup"]
    part = detail.locator('tr[data-branch="topic-a-part"]')
    assert not part.is_visible()
    fold = detail.locator('tr[data-branch="topic-a"] .checkouts-fold-toggle')
    assert fold.inner_text() == "Folded branches (1)"
    fold.click()
    assert part.is_visible()
    assert fold.get_attribute("aria-expanded") == "true"
    fold.click()
    assert not part.is_visible()
    assert fold.get_attribute("aria-expanded") == "false"

    # "Checked out in" names every checkout that holds a branch, and a dash for none.
    def holders(name):
        return detail.locator(f'tr[data-branch="{name}"] td').first.evaluate(
            "cell => [...cell.childNodes].filter(node => node.nodeType === 3 || "
            "!node.classList.contains('cell-label')).map(node => node.textContent).join('')")

    assert [holders(name) for name in ("topic-a", "trunk", "wt-branch")] == [
        "—", "Repo Clone 2", "linked"]
    assert all(holders(branch["name"]) == "—" for branch in clone["branches"]
               if not branch["checked_out_in"])
    shared = checkout_variant(document, share_a_branch_between_two_checkouts)
    serve_checkouts(page, double, shared)
    detail = page.locator('tr.checkouts-detail[data-detail-for$="Repo Clone 2"]')
    assert holders("topic-merged") == "Repo Clone 2 · linked"
    serve_checkouts(page, double, document)
    detail = page.locator('tr.checkouts-detail[data-detail-for$="Repo Clone 2"]')

    # Each verdict reads the words of its served verdict, with its base's basis.
    for branch in clone["branches"]:
        cell = detail.locator(f'tr[data-branch="{branch["name"]}"] td.checkouts-verdict')
        if branch["verdict"] is None:
            assert cell.inner_text() == "—", branch["name"]
            continue
        assert cell.locator(".checkouts-verdict-words").inner_text() == \
            served_verdict_words(group, branch["verdict"]), branch["name"]
        assert cell.locator(".checkouts-basis").inner_text() == checkout_words()[
            "comparisonBasis"][clone["verdict_base"]["basis"]], branch["name"]
    words_of = {
        name: detail.locator(f'tr[data-branch="{name}"] .checkouts-verdict-words').inner_text()
        for name in ("topic-a", "topic-merged", "topic-squash")
    }
    assert words_of == {
        "topic-a": "not merged",
        "topic-merged": "merged — tip is in trunk",
        "topic-squash": "merged — every change is in trunk",
    }

    # Cleanup names what blocks a branch or linked worktree, and Removable otherwise.
    cleanup = {
        name: detail.locator(f'tr[data-branch="{name}"] .checkouts-branch-cleanup').inner_text()
        for name in ("topic-a", "topic-merged", "wt-branch")
    }
    assert cleanup == {
        "topic-a": "not merged", "topic-merged": "Removable",
        "wt-branch": "its worktree has changes",
    }
    assert detail.locator(
        'tr.checkouts-detail-worktree[data-worktree="linked"] .checkouts-worktree-cleanup'
    ).inner_text() == "has changes"

    # The work items the clone serves: how each matched, its served Code Status text.
    served = {item["package_path"]: item for item in document["packages"]}
    items = detail.locator("li.checkouts-clone-work-item")
    assert items.evaluate_all("rows => rows.map(row => row.dataset.package)") == [
        "Repo/Drafts/alpha", "Repo/Shipped/gamma"]
    alpha = detail.locator('li[data-package="Repo/Drafts/alpha"]')
    assert alpha.locator(".checkouts-basis").inner_text() == \
        "(Target repo, folded into this branch)"
    assert alpha.locator(".checkouts-code-status").inner_text() == \
        served["Repo/Drafts/alpha"]["code_status"]["text"]
    gamma = detail.locator('li[data-package="Repo/Shipped/gamma"]')
    assert gamma.locator(".checkouts-basis").inner_text() == "(Target repo)"
    assert gamma.locator(".checkouts-code-status").inner_text() == "awaiting merge (no PR)"


def test_checkouts_show_on_board_selects_without_toggling_and_a_card_click_still_toggles(
    open_page, attention_checkouts
):
    """The expanded row's action selects, focuses and shows the card; repeating keeps it."""
    page, _ = open_attention_checkouts(
        open_page, attention_checkouts, attention_checkouts.document)
    detail = open_clone_detail(page, "Repo Clone 2")
    detail.locator('li[data-package="Repo/Drafts/alpha"] [data-show-on-board]').click()
    playwright.expect(page.locator("#board")).to_be_visible()
    playwright.expect(page.locator("#checkouts")).to_be_hidden()
    card = page.locator('.card[data-package-path="Repo/Drafts/alpha"]')
    playwright.expect(card).to_have_class(re.compile(r"\bselected\b"))
    assert focused_checkouts_target(page)["packagePath"] == "Repo/Drafts/alpha"
    assert focused_inside_viewport(page)

    # Doing it again leaves the card selected.
    show_checkouts_view(page)
    page.locator('tr.checkouts-detail li[data-package="Repo/Drafts/alpha"] '
                 '[data-show-on-board]').click()
    playwright.expect(card).to_have_class(re.compile(r"\bselected\b"))
    assert card.get_attribute("aria-pressed") == "true"

    # The card's own click keeps its toggle.
    card.click()
    assert card.get_attribute("aria-pressed") == "false"
    assert "selected" not in (card.get_attribute("class") or "").split()
    card.click()
    assert card.get_attribute("aria-pressed") == "true"


def test_checkouts_show_on_board_clears_a_search_that_hides_the_card(
    open_page, attention_checkouts
):
    """A search that hides the work item's card is cleared before the board shows."""
    page, _ = open_attention_checkouts(
        open_page, attention_checkouts, attention_checkouts.document)
    page.fill("#filter", "zzz")
    page.locator('li[data-code="target_repo_missing"] [data-show-on-board]').click()
    playwright.expect(page.locator("#board")).to_be_visible()
    assert page.locator("#filter").input_value() == ""
    card = page.locator('.card[data-package-path="Repo/Drafts/zeta"]')
    playwright.expect(card).to_have_class(re.compile(r"\bselected\b"))
    assert card.is_visible()
    assert focused_checkouts_target(page)["packagePath"] == "Repo/Drafts/zeta"


def test_checkouts_show_on_board_refuses_a_finished_row_and_an_absent_work_item(
    open_page, attention_checkouts
):
    """Where the card cannot be shown the view stays and says why; the preference is left alone."""
    document = attention_checkouts.document
    page, _ = open_attention_checkouts(open_page, attention_checkouts, document)
    key = "spec-tracker-hide-terminal-rows"
    stored = page.evaluate("key => window.localStorage.getItem(key)", key)
    detail = open_clone_detail(page, "Repo Clone 2")
    detail.locator('li[data-package="Repo/Shipped/gamma"] [data-show-on-board]').click()
    playwright.expect(page.locator("#checkouts")).to_be_visible()
    playwright.expect(page.locator("#board")).to_be_hidden()
    assert page.locator("#checkouts-action-status").inner_text() == (
        "This work item is in a finished row; uncheck Hide terminal rows to show it.")
    assert page.get_by_label("Hide terminal rows", exact=True).is_checked()
    assert page.evaluate("key => window.localStorage.getItem(key)", key) == stored

    # A work item the displayed catalog does not hold.
    other, _ = open_attention_checkouts(
        open_page, attention_checkouts, document, catalog=attention_checkouts.catalog)
    stored = other.evaluate("key => window.localStorage.getItem(key)", key)
    other.locator('li[data-code="target_repo_missing"] [data-show-on-board]').click()
    playwright.expect(other.locator("#checkouts")).to_be_visible()
    playwright.expect(other.locator("#board")).to_be_hidden()
    assert other.locator("#checkouts-action-status").inner_text() == (
        "This work item is not on the displayed board; choose Refresh view on the board.")
    assert other.evaluate("key => window.localStorage.getItem(key)", key) == stored


class HexRevisionSettings(BrowserSettings):
    """Board settings whose saved revision is one a checkout document can carry."""

    def save_settings(self, revision, order, completed):
        result = super().save_settings(revision, order, completed)
        if result["outcome"] == "success":
            self.revision = result["revision"] = "2" * 64
        return result


SETTINGS_SENTENCE = "Collected under different settings from the board shown."


def open_settings_checkouts(launch, attention_checkouts, document, *, client=None, settings=None):
    """Open the checkouts view over a board whose settings the page can save."""
    double = BrowserCheckouts(document)
    settings = settings or HexRevisionSettings(completed=["Shipped"], revision=CHECKOUTS_REVISION)
    page = launch(
        client or StaticClient(attention_checkouts.catalog_all),
        settings=settings, checkouts=double, fake_clock=True, init_script=RESPONSE_RECORDER,
    )
    wait_for_responses(page, "checkoutsResponses", 1)
    settle_frames(page)
    show_checkouts_view(page)
    return page, double, settings


def test_checkouts_settings_sentence_follows_a_board_settings_save(
    open_page, attention_checkouts
):
    """A save changes the board's revision, not the scan: the scan stays and says so."""
    document = attention_checkouts.document
    page, double, settings = open_settings_checkouts(open_page, attention_checkouts, document)
    line = page.locator("#checkouts-status-line")
    items = page.locator("#checkouts-body li[data-code]")
    groups = page.locator("#checkouts-body .checkouts-group")
    assert SETTINGS_SENTENCE not in line.inner_text()
    assert items.count() == len(document["attention"])
    assert groups.count() == 1
    local_state = line.locator("time").first.inner_text()

    page.locator("#stage-order-editor summary").click()
    page.get_by_role("checkbox", name="Counts as finished: Shipped").uncheck()
    page.get_by_role("button", name="Save").click()
    page.get_by_text("Board settings saved.", exact=True).wait_for()
    assert settings.revision == "2" * 64
    playwright.expect(line).to_contain_text(SETTINGS_SENTENCE)
    assert line.inner_text().count(SETTINGS_SENTENCE) == 1
    assert items.count() == len(document["attention"])
    assert groups.count() == 1
    assert line.locator("time").first.inner_text() == local_state

    # The next tick still serves the old document, so the sentence stays.
    fire_checkouts_poll(page)
    settle_frames(page)
    assert line.inner_text().count(SETTINGS_SENTENCE) == 1
    assert items.count() == len(document["attention"])
    assert line.locator("time").first.inner_text() == local_state

    # A capture collected under the new revision clears it.
    double.serve(checkouts_with_revision(document, "2" * 64))
    fire_checkouts_poll(page)
    settle_frames(page)
    assert SETTINGS_SENTENCE not in line.inner_text()
    assert items.count() == len(document["attention"])


def test_checkouts_settings_sentence_shows_when_the_catalog_request_failed(
    open_page, attention_checkouts
):
    """With no catalog displayed there is no revision to match, and the scan still renders."""
    document = attention_checkouts.document
    client = SequenceClient([CatalogError("producer_unavailable")])
    page, _, _ = open_settings_checkouts(
        open_page, attention_checkouts, document, client=client)
    line = page.locator("#checkouts-status-line")
    playwright.expect(line).to_contain_text(SETTINGS_SENTENCE)
    assert page.locator("#checkouts-body li[data-code]").count() == len(document["attention"])
    assert page.locator("#checkouts-body .checkouts-group").count() == 1


def covered_card_clearance(page):
    """How far the focused card's edges lie outside the area the sticky heads leave."""
    return page.evaluate("""() => {
      const rect = document.activeElement.getBoundingClientRect();
      let covered = 0;
      document.querySelectorAll('.toolbar, .board-corner, .column-head').forEach((head) => {
        covered = Math.max(covered, head.getBoundingClientRect().bottom);
      });
      return {top: rect.top, bottom: rect.bottom, covered, height: window.innerHeight,
              scrolled: window.scrollY, path: document.activeElement.dataset.packagePath};
    }""")


def test_checkouts_show_on_board_clears_the_sticky_heads_on_a_tall_board(
    open_page, attention_checkouts, tmp_path
):
    """Whether the card lies above or below the window, it ends fully below the heads."""
    spec_root = tmp_path / "tall-root"
    _package_spec(spec_root, "Drafts", "alpha", ATTENTION_PACKAGE_IDS["alpha"], [])
    _package_spec(spec_root, "Drafts", "zeta", ATTENTION_PACKAGE_IDS["zeta"], [])
    for number in range(60):
        _package_spec(spec_root, "Drafts", f"filler-{number:02d}",
                      f"123e4567-e89b-42d3-a456-4266141750{number:02d}", [])
    catalog = json.loads(scan_catalog(
        spec_root, completed_stage_names=["Shipped"],
        configuration_revision=CHECKOUTS_REVISION,
    ))
    page, _ = open_attention_checkouts(
        open_page, attention_checkouts, attention_checkouts.document, catalog=catalog)
    page.locator("#view-board").click()
    assert page.evaluate("document.documentElement.scrollHeight") > 3 * 720

    for target, scroll_to in (("Repo/Drafts/alpha", "bottom"), ("Repo/Drafts/zeta", "top")):
        page.locator("#view-board").click()
        page.evaluate(
            "where => window.scrollTo(0, where === 'bottom' ? document.documentElement.scrollHeight : 0)",
            scroll_to)
        show_checkouts_view(page)
        if target == "Repo/Drafts/zeta":
            page.locator('li[data-code="target_repo_missing"] [data-show-on-board]').click()
        else:
            detail = open_clone_detail(page, "Repo Clone 2")
            detail.locator(f'li[data-package="{target}"] [data-show-on-board]').click()
        playwright.expect(page.locator("#board")).to_be_visible()
        # The checkouts toolbar is shorter than the board's, so the heads settle
        # lower once the switch is painted; the card must clear where they end.
        settle_frames(page)
        box = covered_card_clearance(page)
        assert box["path"] == target
        assert box["top"] >= box["covered"], (target, box)
        assert box["bottom"] <= box["height"], (target, box)


# ---------------------------------------------------------------------------
# In-place refresh, the narrow layout and accessibility
# ---------------------------------------------------------------------------


def folded_variant(document):
    """The capture with the ``outside`` branch of Repo Clone 2 folded under ``topic-a``."""
    def fold(value):
        _, clone = checkout_clone(value, "Repo Clone 2")
        next(branch for branch in clone["branches"]
             if branch["name"] == "outside")["contained_in"] = "topic-a"

    return checkout_variant(document, fold)


def with_observed_at(document, observed_at):
    """The same capture with one observation time; the digest does not cover it."""
    changed = copy.deepcopy(document)
    changed["observed_at"] = observed_at
    return changed


def observe_checkouts_mutations(page):
    """Record every DOM change inside ``#checkouts`` with the keyed blocks it touches.

    A change to a node's own text or attributes belongs to the block it sits in;
    a node added or removed is itself the block.  The status line and the
    action status are told apart, because only the status line may change.
    """
    page.evaluate("""() => {
      window.checkoutsRecords = [];
      const status = document.querySelector('#checkouts-status-line');
      const action = document.querySelector('#checkouts-action-status');
      const own = (node) => {
        const element = node.nodeType === 1 ? node : node.parentElement;
        if (element === null) return null;
        const block = element.closest('[data-key]');
        return block === null ? null : block.getAttribute('data-key');
      };
      new MutationObserver((records) => {
        for (const record of records) {
          const moved = [...record.addedNodes, ...record.removedNodes];
          window.checkoutsRecords.push({
            status: status.contains(record.target),
            action: action.contains(record.target),
            attribute: record.type === 'attributes' ? record.attributeName : null,
            keys: record.type === 'childList' ? moved.map(own) : [own(record.target)],
          });
        }
      }).observe(document.querySelector('#checkouts'),
        {subtree: true, childList: true, attributes: true, characterData: true});
    }""")


def checkouts_records(page, *, with_attribute=False):
    """The records seen since the last call, as ``(status, action, keys)`` tuples.

    ``with_attribute`` appends the changed attribute's name (None for a text or
    node change) as a fourth item.
    """
    records = page.evaluate(
        "() => window.checkoutsRecords.splice(0, window.checkoutsRecords.length)")
    if with_attribute:
        return [(item["status"], item["action"], item["keys"], item["attribute"])
                for item in records]
    return [(item["status"], item["action"], item["keys"]) for item in records]


def checkouts_focus(page):
    """The focused element's key and control, or None outside a keyed block."""
    return page.evaluate("""() => {
      const el = document.activeElement;
      return {key: el.getAttribute('data-key'), control: el.getAttribute('data-control'),
              tag: el.tagName, cls: el.className, id: el.id};
    }""")


def open_clone_two_state(page):
    """Open the quiet row, Repo Clone 2 and its fold; focus the row toggle; scroll 200 px.

    Returns the toggle's key and control.
    """
    page.set_viewport_size({"width": 1280, "height": 600})
    page.locator("details.checkouts-quiet > summary").click()
    detail = open_clone_detail(page, "Repo Clone 2")
    fold = detail.locator(".checkouts-fold-toggle")
    assert fold.count() == 1
    fold.click()
    assert fold.get_attribute("aria-expanded") == "true"
    toggle = page.locator('tr.checkouts-clone[data-clone="Repo Clone 2"] .checkouts-toggle')
    toggle.focus()
    page.evaluate("window.scrollTo(0, 200)")
    assert page.evaluate("window.scrollY") == 200
    return checkouts_focus(page)


def assert_clone_two_state_kept(page, focus, scroll=200):
    """The quiet row, the row, its fold, the focus and the scroll are as they were."""
    assert page.locator("details.checkouts-quiet").evaluate("el => el.open") is True
    row = page.locator('tr.checkouts-clone[data-clone="Repo Clone 2"]')
    assert row.locator(".checkouts-toggle").get_attribute("aria-expanded") == "true"
    detail = row.locator("xpath=following-sibling::tr[1]")
    assert detail.evaluate("el => el.hidden") is False
    fold = detail.locator(".checkouts-fold-toggle")
    assert fold.get_attribute("aria-expanded") == "true"
    assert detail.locator('tr[data-branch="outside"]').evaluate("el => el.hidden") is False
    assert checkouts_focus(page) == focus
    assert page.evaluate("window.scrollY") == scroll


def test_checkouts_refresh_keeps_open_state_focus_and_scroll_in_keyed_blocks(
    open_page, view_checkouts
):
    """A tick replaces only the blocks that changed and keeps what the reader did."""
    before = folded_variant(view_checkouts.document_topic_committed)
    after = with_observed_at(
        folded_variant(view_checkouts.document_topic_recommitted), before["observed_at"])
    clone_path = checkout_clone(before, "Repo Clone 2")[1]["path"]
    page, double = open_view_checkouts(open_page, view_checkouts, before)
    focus = open_clone_two_state(page)
    assert focus["control"] == "toggle" and json.loads(focus["key"]) == ["clone", clone_path]
    held = page.evaluate_handle("document.activeElement")
    arm_focus_sampler(page)
    observe_checkouts_mutations(page)
    pushed = page.locator("#checkouts-action-status").inner_text()

    # Two ticks over an unchanged document change nothing but the status line.
    for _ in range(2):
        serve_checkouts(page, double, before)
        records = checkouts_records(page)
        assert records and all(status for status, _, _ in records), records
        assert held.evaluate("node => node === document.activeElement")
        assert_clone_two_state_kept(page, focus)

    # One more commit on topic-a updates Repo Clone 2's blocks alone.
    row = page.locator('tr.checkouts-clone-branch[data-branch="topic-a"]')
    assert "1 ahead" in row.locator(".checkouts-remote-state").inner_text()
    old_tip = row.locator(".checkouts-tip").inner_text()
    serve_checkouts(page, double, after)
    records = checkouts_records(page, with_attribute=True)
    outside = [(keys, attribute) for status, _, keys, attribute in records if not status]
    assert outside, "the new commit changed no block"
    # The group's search words follow the served Code Status text and HEAD
    # words, so its own search attribute is the one write a group block takes;
    # every other change sits in a block of Repo Clone 2.
    assert all(
        keys and all(
            key is not None and (clone_path in json.loads(key) or (
                json.loads(key)[0] == "group" and attribute == "data-search"))
            for key in keys)
        for keys, attribute in outside), outside
    assert "2 ahead" in row.locator(".checkouts-remote-state").inner_text()
    assert row.locator(".checkouts-tip").inner_text() != old_tip
    assert_clone_two_state_kept(page, focus)
    assert page.locator("#checkouts-action-status").inner_text() == pushed
    assert all(not action for _, action, _, _ in records)
    assert_focus_sampler_clean(page)


def test_checkouts_refresh_moves_focus_to_the_next_row_when_its_row_goes(
    open_page, view_checkouts
):
    """A removed row hands focus to the next row toggle, else the group heading."""
    before = view_checkouts.document_topic_committed
    page, double = open_view_checkouts(open_page, view_checkouts, before)
    detail = open_clone_detail(page, "Repo Clone 2")
    detail.locator('tr[data-branch="topic-a"]').focus()
    arm_focus_sampler(page)

    # The tick that deletes the focused branch row moves focus to the next row
    # toggle in the group, Repo Clone 3's.
    serve_checkouts(page, double, view_checkouts.document_topic_deleted)
    assert page.locator('tr[data-branch="topic-a"].checkouts-clone-branch').count() == 0
    focus = checkouts_focus(page)
    assert focus["control"] == "toggle"
    assert json.loads(focus["key"]) == ["clone", checkout_clone(before, "Repo Clone 3")[1]["path"]]
    assert_focus_sampler_clean(page)

    # The tick that removes the group's last row moves it to the group heading.
    page.locator(
        'tr.checkouts-clone[data-clone="Repo Clone 10"] .checkouts-toggle').focus()

    def without_last_clone(document):
        group = checkout_group(document, "remote")
        group["clones"] = [item for item in group["clones"]
                           if not item["path"].endswith("Repo Clone 10")]

    serve_checkouts(page, double, checkout_variant(before, without_last_clone))
    assert page.locator('tr.checkouts-clone[data-clone="Repo Clone 10"]').count() == 0
    focus = page.evaluate("""() => ({
      head: document.activeElement.classList.contains('checkouts-group-head'),
      group: document.activeElement.closest('.checkouts-group').dataset.checkoutsGroup,
    })""")
    assert focus["head"] is True
    assert focus["group"] == checkout_group(before, "remote")["remote"]["key"]
    assert_focus_sampler_clean(page)

    # With no group left the view's own heading takes it.
    serve_checkouts(page, double, view_checkouts.document_no_git)
    assert page.evaluate("document.activeElement.id") == "checkouts-heading"
    assert_focus_sampler_clean(page)


def test_checkouts_refresh_keeps_state_across_the_settings_sentence_and_a_failure(
    open_page, view_checkouts
):
    """The mismatch sentence comes and goes, and a failed outcome returns, with state kept."""
    before = folded_variant(view_checkouts.document_topic_committed)
    page, double = open_view_checkouts(open_page, view_checkouts, before)
    focus = open_clone_two_state(page)
    sentence = "Collected under different settings from the board shown."
    line = page.locator("#checkouts-status-line")
    # The status row straddles the window's top edge, so its line is the first
    # thing fully in view; the sentence wraps the row and moves that line up.
    scroll = page.evaluate("""() => {
      const row = document.querySelector('.checkouts-status').getBoundingClientRect();
      window.scrollTo(0, Math.ceil(row.top + window.scrollY) + 4);
      return window.scrollY;
    }""")
    line_top = line.evaluate("el => el.getBoundingClientRect().top")
    assert 0 <= line_top < 20
    arm_focus_sampler(page)
    observe_checkouts_mutations(page)

    # A revision that differs from the board's adds the sentence; the same
    # revision clears it.  Neither touches a block.
    double.serve(checkouts_with_revision(before, "2" * 64))
    fire_checkouts_poll(page)
    settle_frames(page)
    assert sentence in line.inner_text()
    assert_clone_two_state_kept(page, focus, scroll)
    assert line.evaluate("el => el.getBoundingClientRect().top") < line_top
    serve_checkouts(page, double, before)
    assert sentence not in line.inner_text()
    assert_clone_two_state_kept(page, focus, scroll)
    assert all(status for status, _, _ in checkouts_records(page))

    # A failed outcome shows the failed panel in place of the view; the next
    # ready one restores the open row, its fold and the quiet row.
    double.fail("checkouts_timeout")
    fire_checkouts_poll(page)
    playwright.expect(page.locator(".checkouts-panel")).to_have_count(1)
    assert page.locator(".checkouts-group").count() == 0
    assert page.locator("details.checkouts-quiet").count() == 0
    assert page.evaluate("document.activeElement.id") == "checkouts-heading"
    serve_checkouts(page, double, before)
    assert page.locator(".checkouts-panel").count() == 0
    row = page.locator('tr.checkouts-clone[data-clone="Repo Clone 2"]')
    assert row.locator(".checkouts-toggle").get_attribute("aria-expanded") == "true"
    detail = row.locator("xpath=following-sibling::tr[1]")
    assert detail.evaluate("el => el.hidden") is False
    assert detail.locator(".checkouts-fold-toggle").get_attribute("aria-expanded") == "true"
    assert detail.locator('tr[data-branch="outside"]').evaluate("el => el.hidden") is False
    assert page.locator("details.checkouts-quiet").evaluate("el => el.open") is True
    assert_focus_sampler_clean(page)


def test_checkouts_refresh_forgets_what_the_document_no_longer_has(
    open_page, view_checkouts
):
    """A row the document drops is not reopened if it returns later."""
    before = view_checkouts.document_topic_committed
    page, double = open_view_checkouts(open_page, view_checkouts, before)
    open_clone_detail(page, "Repo Clone 3")

    def without_clone_three(document):
        group = checkout_group(document, "remote")
        group["clones"] = [item for item in group["clones"]
                           if not item["path"].endswith("Repo Clone 3")]

    serve_checkouts(page, double, checkout_variant(before, without_clone_three))
    assert page.locator('tr.checkouts-clone[data-clone="Repo Clone 3"]').count() == 0
    serve_checkouts(page, double, before)
    row = page.locator('tr.checkouts-clone[data-clone="Repo Clone 3"]')
    assert row.locator(".checkouts-toggle").get_attribute("aria-expanded") == "false"
    assert row.locator("xpath=following-sibling::tr[1]").evaluate("el => el.hidden") is True


def long_name_variant(view_checkouts):
    """A capture whose Repo Clone 10 path and ``topic-a`` branch are 300 unbroken characters."""
    document = view_checkouts.document_topic_committed
    clone_ten = checkout_clone(document, "Repo Clone 10")[1]["path"]
    long_path = str(Path(clone_ten).with_name("RepoClone" + "x" * 291))
    rendered = json.dumps(document).replace(json.dumps(clone_ten), json.dumps(long_path))
    rendered = rendered.replace('"topic-a"', json.dumps("p" * 300))
    variant = reseal_checkouts(json.loads(rendered))
    models.parse_checkouts(json.dumps(variant))
    return variant


def open_every_clone_row(page):
    """Open each clone row's detail from its toggle."""
    for toggle in page.locator("#checkouts-body .checkouts-toggle").all():
        toggle.click()


def test_checkouts_narrow_layout_stacks_values_under_their_column_names(
    open_page, view_checkouts
):
    """Stacked at 360 px, tabular from 701 px, with table roles throughout."""
    document = long_name_variant(view_checkouts)
    page, double = open_view_checkouts(open_page, view_checkouts, document)
    page.get_by_label("Hide quiet checkouts", exact=True).uncheck()
    toggle_folders_editor(page)
    playwright.expect(page.locator("#checkout-folders-status")).to_have_text(
        "Current checkout folders loaded.")
    add_folder(page, "/" + "q" * 299)
    columns = ["Checkout", "Availability", "HEAD", "Default branch vs remote",
               "Tree", "Branches", "Cleanup", "Package"]
    for theme in ("dark", "light"):
        page.get_by_label("Theme", exact=True).select_option(theme)

        page.set_viewport_size({"width": 360, "height": 800})
        open_every_clone_row(page)
        settle_frames(page)
        assert page.evaluate("""() => [document.documentElement.scrollWidth,
          document.documentElement.clientWidth]""") == [360, 360]
        editor_rights = page.evaluate("""() => [...document.querySelectorAll(
          '#checkout-folders-editor summary, #checkout-folders-editor input, ' +
          '#checkout-folders-editor button, #checkout-folders-editor .checkout-folders-path')]
          .filter(node => node.getClientRects().length)
          .map(node => [node.id || node.className, node.getBoundingClientRect().right])""")
        assert len(editor_rights) >= 7, editor_rights
        assert all(right <= 360.5 for _, right in editor_rights), editor_rights
        row = page.locator('tr.checkouts-clone[data-clone="Repo Clone 2"]')
        labels = row.evaluate("""el => [...el.children].map(cell => {
          const label = cell.firstElementChild;
          const value = document.createRange();
          value.setStartAfter(label);
          value.setEnd(cell, cell.childNodes.length);
          const shown = getComputedStyle(label);
          return {
            text: label.textContent, label: label.className, display: shown.display,
            visibility: shown.visibility, before: label.getBoundingClientRect().bottom,
            value: value.getBoundingClientRect().top,
            hasValue: cell.textContent.trim().length > label.textContent.trim().length,
          };
        })""")
        assert [item["text"] for item in labels] == columns
        for item in labels:
            assert item["label"] == "cell-label" and item["display"] == "block"
            assert item["visibility"] == "visible"
            assert item["hasValue"] and item["before"] <= item["value"] + 1, item
        for role, count in (("table", 1), ("row", 1), ("columnheader", 8), ("cell", 1)):
            assert page.get_by_role(role).count() >= count, role
        assert page.get_by_role("columnheader", name="Default branch vs remote").count() >= 1
        assert page.get_by_role("cell").filter(has_text="3 branches").count() >= 1

        # The long names wrap inside their cells instead of widening the view.
        inside = page.evaluate("""() => [...document.querySelectorAll('#checkouts td, #checkouts th')]
          .filter(cell => /p{300}|x{291}/.test(cell.textContent))
          .map(cell => ({tag: cell.tagName, scroll: cell.scrollWidth, client: cell.clientWidth,
                         right: cell.getBoundingClientRect().right}))""")
        assert len(inside) >= 4, inside
        assert all(item["scroll"] <= item["client"] + 1 and item["right"] <= 360.5
                   for item in inside), inside

        # At 701 px the names are visually hidden and the rows are tabular.
        page.set_viewport_size({"width": 701, "height": 800})
        settle_frames(page)
        assert page.evaluate("""() => [...document.querySelectorAll('#checkouts .cell-label')]
          .every(label => getComputedStyle(label).display === 'none')""") is True
        assert page.evaluate("""() => document.querySelectorAll('#checkouts .cell-label').length""") > 0
        assert row.evaluate("el => getComputedStyle(el).display") == "table-row"
        assert row.evaluate("el => getComputedStyle(el.closest('table')).display") == "table"
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth") is True

        # At 1600 px the same roles resolve and nothing scrolls sideways.
        page.set_viewport_size({"width": 1600, "height": 900})
        settle_frames(page)
        for role in ("table", "row", "columnheader", "cell"):
            assert page.get_by_role(role).count() >= 1, role
        assert page.get_by_role("columnheader", name="Cleanup").count() >= 1
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth") is True


CHECKOUTS_TEXT_SAMPLES = {
    "status line": "#checkouts-status-line",
    "column headers": "#checkouts th[scope=col]",
    "cell values": (
        "#checkouts-body td, #checkouts-body th[scope=row], .checkouts-basis, "
        ".checkouts-empty, .checkouts-caption, .checkouts-note, .checkouts-blocker, "
        ".checkouts-code-status, .checkouts-package-path, .checkouts-flag, "
        ".checkouts-quiet summary, .checkouts-quiet li"),
    "attention items": (
        ".checkouts-attention h2, .checkouts-attention-class, "
        ".checkouts-attention-item, .checkouts-attention-go"),
    "freshness words": ".checkouts-freshness",
    "panel text": ".checkouts-panel, .checkouts-panel h2, .checkouts-panel li",
    "folders editor": (
        "#checkout-folders-editor summary, #checkout-folders-editor label, "
        ".checkout-folders-path, .checkout-folders-status, "
        "#checkout-folders-editor button"),
}


def checkouts_text_colors(page):
    """Each sample's text color over the first opaque background behind it."""
    return page.evaluate("""(samples) => {
      const backdrop = (node) => {
        for (let el = node; el; el = el.parentElement) {
          const color = getComputedStyle(el).backgroundColor;
          const channels = color.match(/[\\d.]+/g).map(Number);
          if (channels.length === 3 || channels[3] === 1) return color;
        }
        return getComputedStyle(document.documentElement).backgroundColor;
      };
      const found = {};
      for (const [name, selector] of Object.entries(samples)) {
        const pairs = new Map();
        for (const el of document.querySelectorAll(selector)) {
          if (!el.getClientRects().length || !el.textContent.trim()) continue;
          const color = getComputedStyle(el).color;
          pairs.set(color + '|' + backdrop(el), el.textContent.trim().slice(0, 30));
        }
        found[name] = [...pairs].map(([pair, text]) => [...pair.split('|'), text]);
      }
      return found;
    }""", CHECKOUTS_TEXT_SAMPLES)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_checkouts_text_and_focus_rings_have_readable_contrast(
    open_page, view_checkouts, theme
):
    """Text keeps 4.5:1 and every control's focus ring 3:1 in both themes."""
    cached = folded_variant(view_checkouts.document_built)
    setup = checkout_variant(
        folded_variant(view_checkouts.document_checked), add_setup_problems(view_checkouts))
    page, double = open_view_checkouts(open_page, view_checkouts, cached)
    page.set_viewport_size({"width": 1600, "height": 1200})
    page.get_by_label("Theme", exact=True).select_option(theme)
    # An open editor with one draft folder shows every control it has.
    toggle_folders_editor(page)
    playwright.expect(page.locator("#checkout-folders-status")).to_have_text(
        "Current checkout folders loaded.")
    add_folder(page, "/work/draft")
    open_every_clone_row(page)
    detail = page.locator('tr.checkouts-clone[data-clone="Repo Clone 2"]').locator(
        "xpath=following-sibling::tr[1]")
    detail.locator(".checkouts-fold-toggle").click()
    # The cached capture shows the freshness words; the setup capture its panel.
    shown = {}
    for document in (cached, setup):
        if document is setup:
            serve_checkouts(page, double, setup)
        settle_frames(page)
        for name, pairs in checkouts_text_colors(page).items():
            shown.setdefault(name, []).extend(pairs)
    for name, pairs in shown.items():
        assert pairs, f"nothing is shown for {name}"
        for color, background, sample in pairs:
            ratio = contrast_ratio(color, background)
            assert ratio >= 4.5, (name, sample, color, background, ratio)

    rings = {
        "view switch": "#view-checkouts",
        "row toggle": 'tr.checkouts-clone[data-clone="Repo Clone 3"] .checkouts-toggle',
        "fold": ".checkouts-fold-toggle",
        "attention item": ".checkouts-attention-go",
        "Check remotes now": "#check-remotes",
        "Show on board": ".checkouts-show-on-board",
        "folders summary": "#checkout-folders-editor summary",
        "folders path": "#checkout-folders-path",
        "folders Add": "#checkout-folders-add",
        "folders Remove": ".checkout-folders-remove",
        "folders checkbox": "#checkout-folders-github",
        "folders Save": "#checkout-folders-save",
        "folders Cancel": "#checkout-folders-cancel",
    }
    for name, selector in rings.items():
        # A key press makes the next script focus show as keyboard focus.
        page.keyboard.press("Shift")
        assert_focus_indicator(page.locator(selector).first), name


def tab_until(page, probe):
    """Press Tab, then Shift+Tab, until ``probe`` holds for the focused element."""
    for key, limit in (("Tab", 80), ("Shift+Tab", 160)):
        for _ in range(limit):
            if page.evaluate(probe):
                return
            page.keyboard.press(key)
    raise AssertionError(f"keyboard never reached {probe}")


def test_checkouts_every_control_works_by_keyboard_alone(open_page, view_checkouts):
    """Tab, Shift+Tab, Enter and Space reach and operate each control."""
    document = folded_variant(view_checkouts.document_topic_committed)
    page, double = open_view_checkouts(open_page, view_checkouts, document)
    clone_two = checkout_clone(document, "Repo Clone 2")[1]["path"]
    clone_three = checkout_clone(document, "Repo Clone 3")[1]["path"]
    page.evaluate("document.activeElement.blur()")

    # The view switch: Enter returns to the board and Space shows the view again,
    # with focus staying on the pressed switch.
    tab_until(page, "document.activeElement.id === 'view-board'")
    page.keyboard.press("Enter")
    playwright.expect(page.locator("#board")).to_be_visible()
    tab_until(page, "document.activeElement.id === 'view-checkouts'")
    page.keyboard.press("Space")
    playwright.expect(page.locator("#checkouts")).to_be_visible()
    assert page.evaluate("document.activeElement.id") == "view-checkouts"

    # Check remotes now: this provider has no remote check, and says so.
    tab_until(page, "document.activeElement.id === 'check-remotes'")
    page.keyboard.press("Enter")
    playwright.expect(page.locator("#checkouts-action-status")).to_have_text(
        "This server does not offer remote checks.")

    # The folders editor: Enter on its summary opens it, a path is typed and
    # added with Enter, and Space on a Remove drops that folder.  Focus goes to
    # the next folder's Remove, else the previous one, else the path input.
    tab_until(page, "document.activeElement.matches('#checkout-folders-editor summary')")
    page.keyboard.press("Enter")
    playwright.expect(page.locator("#checkout-folders-status")).to_have_text(
        "Current checkout folders loaded.")
    tab_until(page, "document.activeElement.id === 'checkout-folders-path'")
    for name in ("code", "docs", "site"):
        page.keyboard.type(name)
        page.keyboard.press("Enter")
    assert folder_paths(page) == ["code", "docs", "site"]
    focus_label = "document.activeElement.getAttribute('aria-label')"
    tab_until(page, f"{focus_label} === 'Remove checkout folder docs'")
    page.keyboard.press("Space")
    assert folder_paths(page) == ["code", "site"]
    assert page.evaluate(focus_label) == "Remove checkout folder site"
    page.keyboard.press("Space")
    assert folder_paths(page) == ["code"]
    assert page.evaluate(focus_label) == "Remove checkout folder code"
    page.keyboard.press("Space")
    assert folder_paths(page) == []
    assert page.evaluate("document.activeElement.id") == "checkout-folders-path"
    # The checkbox takes Space, and Cancel takes Enter, then focus stays in the panel.
    tab_until(page, "document.activeElement.id === 'checkout-folders-github'")
    page.keyboard.press("Space")
    assert page.locator("#checkout-folders-github").is_checked()
    tab_until(page, "document.activeElement.id === 'checkout-folders-save'")
    tab_until(page, "document.activeElement.id === 'checkout-folders-cancel'")
    page.keyboard.press("Enter")
    assert not page.locator("#checkout-folders-github").is_checked()
    assert page.evaluate(
        "document.activeElement.closest('#checkout-folders-editor') !== null")
    tab_until(page, "document.activeElement.matches('#checkout-folders-editor summary')")
    page.keyboard.press("Enter")
    assert page.evaluate("document.querySelector('#checkout-folders-editor').open") is False

    # A row toggle opens with Enter and closes with Space.
    toggle_probe = f"""(() => {{
      const el = document.activeElement;
      return el.getAttribute('data-control') === 'toggle' &&
        JSON.parse(el.getAttribute('data-key'))[1] === {json.dumps(clone_two)};
    }})()"""
    tab_until(page, toggle_probe)
    row = page.locator('tr.checkouts-clone[data-clone="Repo Clone 2"]')
    toggle = row.locator(".checkouts-toggle")
    page.keyboard.press("Enter")
    assert toggle.get_attribute("aria-expanded") == "true"
    page.keyboard.press("Space")
    assert toggle.get_attribute("aria-expanded") == "false"
    page.keyboard.press("Enter")
    assert toggle.get_attribute("aria-expanded") == "true"

    # The fold in its detail opens with Space and closes with Enter.
    tab_until(page, "document.activeElement.getAttribute('data-control') === 'fold'")
    fold = page.locator(".checkouts-fold-toggle")
    outside = page.locator('tr.checkouts-clone-branch[data-branch="outside"]')
    page.keyboard.press("Space")
    assert fold.get_attribute("aria-expanded") == "true"
    assert outside.evaluate("el => el.hidden") is False
    page.keyboard.press("Enter")
    assert fold.get_attribute("aria-expanded") == "false"
    assert outside.evaluate("el => el.hidden") is True

    # Another row's toggle is reached after the open detail's controls.
    tab_until(page, f"""(() => {{
      const el = document.activeElement;
      return el.getAttribute('data-control') === 'toggle' &&
        JSON.parse(el.getAttribute('data-key'))[1] === {json.dumps(clone_three)};
    }})()""")

    # An attention item moves focus to the row it names.
    tab_until(page, """(() => {
      const el = document.activeElement;
      return el.getAttribute('data-control') === 'go' &&
        el.closest('li').dataset.code === 'branch_unpushed';
    })()""")
    page.keyboard.press("Enter")
    assert page.evaluate("""() => document.activeElement.dataset.branch + ' ' +
      document.activeElement.className""") == "topic-a checkouts-clone-branch"

    # Show on board shows the board with the work item's card selected and focused.
    tab_until(page, """(() => {
      const el = document.activeElement;
      return el.getAttribute('data-control') === 'show:Repo/Drafts/alpha' &&
        el.closest('tr').classList.contains('checkouts-branch');
    })()""")
    page.keyboard.press("Enter")
    playwright.expect(page.locator("#board")).to_be_visible()
    assert page.evaluate("document.activeElement.dataset.packagePath") == "Repo/Drafts/alpha"


def test_checkouts_only_the_action_status_is_a_live_region(open_page, view_checkouts):
    """Refreshed content is never announced; the action status is the one region."""
    ready = view_checkouts.document_topic_committed
    setup = checkout_variant(view_checkouts.document_checked, add_setup_problems(view_checkouts))
    page, double = open_view_checkouts(open_page, view_checkouts, ready)
    page.get_by_label("Hide quiet checkouts", exact=True).uncheck()
    observe_checkouts_mutations(page)
    live = """() => [...document.querySelectorAll(
      '#checkouts [aria-live], #checkouts [role=status], #checkouts [role=alert], #checkouts output')]
      .map(el => el.id)"""
    for step in (ready, setup, view_checkouts.document_topic_recommitted, "failed"):
        if step == "failed":
            double.fail("checkouts_unavailable")
            fire_checkouts_poll(page)
            playwright.expect(page.locator(".checkouts-panel")).to_have_count(1)
        else:
            serve_checkouts(page, double, step)
        assert page.evaluate(live) == ["checkouts-action-status", "checkout-folders-status"]
        # A tick never writes either region.
        assert all(not action for _, action, _ in checkouts_records(page))
        assert page.locator("#checkouts-action-status").inner_text() == ""
        assert page.locator("#checkout-folders-status").inner_text() == ""


def test_checkouts_quiet_row_lists_and_counts_only_the_matching_repositories(
    open_page, view_checkouts
):
    """With two quiet repositories a query lists and counts only the one it matches."""
    document = copy.deepcopy(view_checkouts.document_built)
    quiet = checkout_group(document, "quiet")
    twin = copy.deepcopy(quiet)
    twin["remote"]["display"] = "example.test/twin-repository"
    twin["remote"]["key"] = "example.test/twin-repository"
    for item in twin["clones"]:
        item["path"] = item["path"].replace("quiet", "twinclone")
        for tree in item["worktrees"]:
            tree["path"] = tree["path"].replace("quiet", "twinclone")
    document["repositories"].append(twin)
    page, _ = open_view_checkouts(open_page, view_checkouts, redigest(document))
    row = page.locator("#checkouts-body .checkouts-quiet")
    summary = row.locator("summary")
    assert summary.inner_text() == "Quiet repositories (2)"

    page.fill("#filter", "twin-repository")
    playwright.expect(row).to_be_visible()
    assert summary.inner_text() == "Quiet repositories (2, 1 matching)"
    row.locator("summary").click()
    playwright.expect(row.locator("li:not([hidden])")).to_have_count(1)
    assert "twin-repository" in row.locator("li:not([hidden])").inner_text()

    page.fill("#filter", "")
    assert summary.inner_text() == "Quiet repositories (2)"
    playwright.expect(row.locator("li:not([hidden])")).to_have_count(2)


# ---------------------------------------------------------------------------
# The checkout folders editor: host, load and the one auto-open
# ---------------------------------------------------------------------------

FOLDERS_EDITOR = "#checkout-folders-editor"
FOLDERS_LIST_ITEMS = "#checkout-folders-list .checkout-folders-path"


def folder_paths(page):
    """The folder paths the editor lists, in shown order."""
    return page.locator(FOLDERS_LIST_ITEMS).evaluate_all(
        "items => items.map(item => item.textContent)"
    )


def toggle_folders_editor(page):
    """Press the editor's summary and let its toggle event run."""
    page.locator(f"{FOLDERS_EDITOR} summary").click()
    settle_frames(page)


def open_folders_page(launch, attention_checkouts, settings, double=None):
    """Open the checkouts view over a document that collects one folder."""
    double = double or BrowserCheckouts(attention_checkouts.document)
    page = launch(
        StaticClient(attention_checkouts.catalog_all),
        settings=settings, checkouts=double, fake_clock=True, init_script=RESPONSE_RECORDER,
    )
    wait_for_responses(page, "checkoutsResponses", 1)
    settle_frames(page)
    show_checkouts_view(page)
    return page, double


def test_checkout_folders_editor_sits_in_the_status_row_for_ready_and_failed_outcomes(
    open_page, attention_checkouts
):
    """Only a settled ready or failed outcome shows the editor, beside the check button."""
    double = BrowserCheckouts(attention_checkouts.document)
    gate = double.hold()
    page = open_page(
        StaticClient(attention_checkouts.catalog_all),
        settings=BrowserSettings(revision=CHECKOUTS_REVISION), checkouts=double,
        fake_clock=True, init_script=RESPONSE_RECORDER,
    )
    assert double.entered.wait(timeout=10)
    show_checkouts_view(page)
    editor = page.locator(FOLDERS_EDITOR)
    # Nothing has settled, so the held first request leaves the editor hidden.
    playwright.expect(editor).to_be_hidden()
    gate.set()
    wait_for_responses(page, "checkoutsResponses", 1)
    playwright.expect(editor).to_be_visible()
    assert page.evaluate("""() => {
      const editor = document.querySelector('#checkout-folders-editor');
      const check = document.querySelector('#check-remotes');
      return editor.tagName === 'DETAILS' &&
        editor.parentElement.classList.contains('checkouts-status') &&
        editor.parentElement === check.parentElement &&
        Boolean(check.compareDocumentPosition(editor) & Node.DOCUMENT_POSITION_FOLLOWING) &&
        editor.closest('#checkouts-status-line, #checkouts-body') === null &&
        editor.querySelector('summary').textContent === 'Checkout folders';
    }""")
    page.evaluate("document.querySelector('#checkout-folders-editor').dataset.mark = 'same'")

    # A failed outcome keeps it, and the node a tick leaves is the one marked.
    double.fail("checkouts_unavailable")
    fire_checkouts_poll(page)
    playwright.expect(page.locator(".checkouts-panel")).to_have_count(1)
    assert page.locator("#checkouts-status-line").inner_text() == ""
    playwright.expect(editor).to_be_visible()
    assert editor.get_attribute("data-mark") == "same"
    fire_checkouts_poll(page)
    settle_frames(page)
    playwright.expect(editor).to_be_visible()
    assert editor.get_attribute("data-mark") == "same"

    # The standalone server has no checkouts route, so the editor stays hidden.
    plain = open_page(
        StaticClient(attention_checkouts.catalog_all), fake_clock=True,
        init_script=RESPONSE_RECORDER,
    )
    wait_for_responses(plain, "checkoutsResponses", 1)
    assert plain.evaluate("window.checkoutsResponses") == [404]
    settle_frames(plain)
    show_checkouts_view(plain)
    playwright.expect(plain.locator(FOLDERS_EDITOR)).to_be_hidden()


def test_checkout_folders_editor_loads_the_saved_folders_on_every_open_without_a_draft(
    open_page, attention_checkouts
):
    """Each open asks again, so a record changed between two opens shows its new folders."""
    settings = HexRevisionSettings(revision=CHECKOUTS_REVISION)
    settings.folders = ["/work/alpha", "/work/beta"]
    page, _ = open_folders_page(open_page, attention_checkouts, settings)
    assert settings.folder_gets == 0
    toggle_folders_editor(page)
    playwright.expect(page.locator(FOLDERS_LIST_ITEMS)).to_have_count(2)
    assert folder_paths(page) == ["/work/alpha", "/work/beta"]
    assert not page.locator("#checkout-folders-github").is_checked()
    assert settings.folder_gets == 1

    toggle_folders_editor(page)
    settings.folders = ["/work/gamma", "/work/alpha"]
    settings.github_lookup = True
    toggle_folders_editor(page)
    playwright.expect(page.locator("#checkout-folders-github")).to_be_checked()
    assert folder_paths(page) == ["/work/gamma", "/work/alpha"]
    assert settings.folder_gets == 2


def test_checkout_folders_editor_discards_an_earlier_load_answer(
    open_page, attention_checkouts
):
    """An answer to the first open that arrives after the second open's answer is dropped."""

    class FirstLoadHeld(HexRevisionSettings):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.first_gate = threading.Event()
            self.first_entered = threading.Event()

        def get_checkout_settings(self):
            answer = super().get_checkout_settings()
            if self.folder_gets == 1:
                self.first_entered.set()
                self.first_gate.wait(timeout=10)
            return answer

    settings = FirstLoadHeld(revision=CHECKOUTS_REVISION)
    settings.folders = ["/work/old"]
    page, _ = open_folders_page(open_page, attention_checkouts, settings)
    toggle_folders_editor(page)
    assert settings.first_entered.wait(timeout=10)
    toggle_folders_editor(page)
    settings.folders = ["/work/new"]
    toggle_folders_editor(page)
    playwright.expect(page.locator(FOLDERS_LIST_ITEMS)).to_have_text(["/work/new"])
    with page.expect_response("**/api/checkout-settings") as late:
        settings.first_gate.set()
    late.value.finished()
    settle_frames(page)
    assert settings.folder_gets == 2
    assert folder_paths(page) == ["/work/new"]


def test_checkout_folders_editor_load_failure_offers_reload_and_keeps_the_view(
    open_page, attention_checkouts
):
    """A failed load names its code, leaves the groups, and Reload loads."""
    settings = HexRevisionSettings(revision=CHECKOUTS_REVISION)
    settings.folders = ["/work/alpha"]
    settings.fail_next_folder_load = True
    page, _ = open_folders_page(open_page, attention_checkouts, settings)
    reload_button = page.get_by_role("button", name="Reload checkout folders", exact=True)
    playwright.expect(reload_button).to_be_hidden()
    toggle_folders_editor(page)
    status = page.locator("#checkout-folders-status")
    playwright.expect(status).to_have_text(
        "Could not load checkout folders: settings_unavailable"
    )
    playwright.expect(reload_button).to_be_visible()
    assert page.locator("#checkouts-body .checkouts-group").count() == 1
    assert folder_paths(page) == []

    reload_button.click()
    playwright.expect(page.locator(FOLDERS_LIST_ITEMS)).to_have_text(["/work/alpha"])
    playwright.expect(reload_button).to_be_hidden()
    assert settings.folder_gets == 2
    assert "Could not load" not in status.inner_text()


def test_checkout_folders_editor_opens_itself_once_with_no_saved_folders(
    open_page, attention_checkouts
):
    """A ready document with no folders opens the editor once; the reader's close sticks."""
    settings = BrowserSettings(revision=CHECKOUTS_REVISION)
    settings.folders = ["/work/alpha"]
    double = BrowserCheckouts()
    page = open_page(
        StaticClient(attention_checkouts.catalog_all), settings=settings, checkouts=double,
        fake_clock=True, init_script=RESPONSE_RECORDER,
    )
    wait_for_responses(page, "checkoutsResponses", 1)
    editor = page.locator(FOLDERS_EDITOR)
    # The view is still the board; the panel opens itself and loads once.
    playwright.expect(editor).to_have_attribute("open", "")
    playwright.expect(page.locator(FOLDERS_LIST_ITEMS)).to_have_text(["/work/alpha"])
    assert settings.folder_gets == 1
    show_checkouts_view(page)
    toggle_folders_editor(page)
    assert page.evaluate("document.querySelector('#checkout-folders-editor').open") is False

    fire_checkouts_poll(page)
    settle_frames(page)
    page.locator("#view-board").click()
    page.locator("#view-checkouts").click()
    playwright.expect(page.locator("#checkouts")).to_be_visible()
    fire_checkouts_poll(page)
    settle_frames(page)
    assert page.evaluate("document.querySelector('#checkout-folders-editor').open") is False
    assert settings.folder_gets == 1

    # A document with a folder never opens it.
    other = BrowserSettings(revision=CHECKOUTS_REVISION)
    full, _ = open_folders_page(open_page, attention_checkouts, other)
    fire_checkouts_poll(full)
    settle_frames(full)
    assert full.evaluate("document.querySelector('#checkout-folders-editor').open") is False
    assert other.folder_gets == 0


def folder_buttons(page, role):
    """The visible text of each of the editor's folder buttons of one role."""
    return page.locator(f'{FOLDERS_EDITOR} button[data-role="{role}"]').evaluate_all(
        "buttons => buttons.map(button => button.textContent)"
    )


def add_folder(page, path):
    """Type ``path`` in the editor's input and press its Add button."""
    page.locator("#checkout-folders-path").fill(path)
    page.get_by_role("button", name="Add", exact=True).click()


def test_checkout_folders_draft_adds_removes_and_cancels(open_page, attention_checkouts):
    """The draft takes exactly what is typed; only empty and repeated entries are refused."""
    settings = BrowserSettings(revision=CHECKOUTS_REVISION)
    settings.folders = ["/work/alpha"]
    page, _ = open_folders_page(open_page, attention_checkouts, settings)
    toggle_folders_editor(page)
    playwright.expect(page.locator(FOLDERS_LIST_ITEMS)).to_have_text(["/work/alpha"])
    save = page.get_by_role("button", name="Save checkout folders", exact=True)
    cancel = page.get_by_role("button", name="Cancel checkout folder changes", exact=True)
    path_input = page.locator("#checkout-folders-path")
    lookup = page.get_by_label("Look up pull requests with the GitHub CLI (gh)")
    playwright.expect(save).to_be_disabled()
    playwright.expect(cancel).to_be_disabled()

    # Add keeps a relative path as typed and clears the input.
    add_folder(page, "code")
    assert folder_paths(page) == ["/work/alpha", "code"]
    assert path_input.input_value() == ""
    playwright.expect(save).to_be_enabled()
    playwright.expect(cancel).to_be_enabled()
    # Enter in the input adds too.
    path_input.fill("/work/beta")
    path_input.press("Enter")
    assert folder_paths(page) == ["/work/alpha", "code", "/work/beta"]
    # An empty entry and an exact repeat are ignored.
    page.get_by_role("button", name="Add", exact=True).click()
    add_folder(page, "code")
    path_input.press("Enter")
    assert folder_paths(page) == ["/work/alpha", "code", "/work/beta"]

    # Remove drops its own folder.
    page.get_by_role("button", name="Remove checkout folder /work/alpha").click()
    assert folder_paths(page) == ["code", "/work/beta"]
    lookup.check()
    assert settings.folder_gets == 1

    # Cancel restores the saved list and the checkbox, and both buttons go quiet.
    cancel.click()
    assert folder_paths(page) == ["/work/alpha"]
    assert not lookup.is_checked()
    playwright.expect(save).to_be_disabled()
    playwright.expect(cancel).to_be_disabled()

    # The checkbox alone is a draft, and undoing it by hand is no draft.
    lookup.check()
    playwright.expect(save).to_be_enabled()
    lookup.uncheck()
    playwright.expect(save).to_be_disabled()
    # Removing and re-adding the same folder restores the saved state.
    page.get_by_role("button", name="Remove checkout folder /work/alpha").click()
    playwright.expect(save).to_be_enabled()
    add_folder(page, "/work/alpha")
    playwright.expect(save).to_be_disabled()
    assert settings.folder_gets == 1


def suggestion_document(attention_checkouts, suggestions):
    """The attention capture with ``suggestions`` as its parent-folder suggestions."""
    return checkout_variant(
        attention_checkouts.document,
        lambda document: document.__setitem__("folder_suggestions", suggestions),
    )


def test_checkout_folders_suggestions_offer_unconfigured_parents_with_their_counts(
    open_page, attention_checkouts
):
    """A ready document's suggestions are Add buttons; a draft folder or a failure hides them."""
    document = suggestion_document(attention_checkouts, [
        {"path": "/work/aaa", "package_count": 1},
        {"path": "/work/zzz", "package_count": 3},
    ])
    settings = BrowserSettings(revision=CHECKOUTS_REVISION)
    page, double = open_folders_page(
        open_page, attention_checkouts, settings, BrowserCheckouts(document))
    toggle_folders_editor(page)
    playwright.expect(page.locator("#checkout-folders-status")).to_have_text(
        "Current checkout folders loaded.")
    assert folder_buttons(page, "suggest") == [
        "Add /work/aaa (1 work item)", "Add /work/zzz (3 work items)"]

    # A path already in the draft is not offered.
    add_folder(page, "/work/zzz")
    assert folder_buttons(page, "suggest") == ["Add /work/aaa (1 work item)"]
    # Adding a suggestion appends its path and takes its button away.
    page.get_by_role("button", name="Add /work/aaa (1 work item)").click()
    assert folder_paths(page) == ["/work/zzz", "/work/aaa"]
    assert folder_buttons(page, "suggest") == []
    # Removing a folder offers its suggestion again.
    page.get_by_role("button", name="Remove checkout folder /work/zzz").click()
    assert folder_buttons(page, "suggest") == ["Add /work/zzz (3 work items)"]

    # A failed collection shows none, and the draft stays.
    double.fail("checkouts_unavailable")
    fire_checkouts_poll(page)
    playwright.expect(page.locator(".checkouts-panel")).to_have_count(1)
    assert folder_buttons(page, "suggest") == []
    assert folder_paths(page) == ["/work/aaa"]
    # The next ready document brings them back.
    serve_checkouts(page, double, document)
    assert folder_buttons(page, "suggest") == ["Add /work/zzz (3 work items)"]


def test_checkout_folders_draft_and_focus_survive_ticks_installs_and_view_switches(
    open_page, attention_checkouts
):
    """Nothing the view redraws touches an open editor, its draft or the focus in it."""
    settings = HexRevisionSettings(revision=CHECKOUTS_REVISION)
    settings.folders = ["/work/alpha"]
    page, _ = open_folders_page(open_page, attention_checkouts, settings)
    toggle_folders_editor(page)
    playwright.expect(page.locator(FOLDERS_LIST_ITEMS)).to_have_text(["/work/alpha"])
    add_folder(page, "/work/draft")
    path_input = page.locator("#checkout-folders-path")
    path_input.fill("typed")
    path_input.focus()
    page.evaluate("""() => {
      window.editorRecords = [];
      new MutationObserver((records) => window.editorRecords.push(...records.map(
        (record) => record.type + ':' + record.target.nodeName)))
        .observe(document.querySelector('#checkout-folders-editor'),
          {subtree: true, childList: true, attributes: true, characterData: true});
    }""")

    def intact():
        assert page.evaluate("document.querySelector('#checkout-folders-editor').open") is True
        assert folder_paths(page) == ["/work/alpha", "/work/draft"]
        assert path_input.input_value() == "typed"
        playwright.expect(path_input).to_be_focused()
        assert page.locator("#checkouts-body .checkouts-group").count() == 1

    fire_checkouts_poll(page)
    fire_checkouts_poll(page)
    settle_frames(page)
    assert page.evaluate("window.editorRecords") == []
    intact()

    # Installing a catalog from the board's Refresh view leaves the editor alone.
    page.locator("#view-board").click()
    with page.expect_response("**/api/catalog"):
        page.get_by_role("button", name="Refresh view", exact=True).click()
    settle_frames(page)
    page.locator("#view-checkouts").click()
    playwright.expect(page.locator("#checkouts")).to_be_visible()
    settle_frames(page)
    assert page.evaluate("window.editorRecords") == []
    assert page.evaluate("document.querySelector('#checkout-folders-editor').open") is True
    assert folder_paths(page) == ["/work/alpha", "/work/draft"]
    assert path_input.input_value() == "typed"
    assert settings.folder_gets == 1
    path_input.focus()
    fire_checkouts_poll(page)
    settle_frames(page)
    intact()


# ---------------------------------------------------------------------------
# The checkout folders editor: Save, its outcomes and the folder problems
# ---------------------------------------------------------------------------

FOLDERS_SAVE_BUTTON = "Save checkout folders"
FOLDERS_STATUS = "#checkout-folders-status"


def folders_save_button(page):
    return page.get_by_role("button", name=FOLDERS_SAVE_BUTTON, exact=True)


def folder_marks(page):
    """Each listed folder's ``(aria-invalid, words of its description)`` pair."""
    return page.locator("#checkout-folders-list > li").evaluate_all("""items => items.map((item) => {
      const name = item.querySelector('.checkout-folders-path');
      const id = name.getAttribute('aria-describedby');
      const described = id ? document.getElementById(id) : null;
      return [name.getAttribute('aria-invalid'), described ? described.textContent : null,
        id !== null && item.contains(described)];
    })""")


def test_checkout_folders_save_sends_the_draft_and_shows_the_servers_answer(
    open_page, attention_checkouts
):
    """The editor shows the response, in its order and with its checkbox, not the draft."""
    settings = BrowserSettings(revision=CHECKOUTS_REVISION)
    settings.folders = ["/work/zeta"]
    page, _ = open_folders_page(open_page, attention_checkouts, settings)
    toggle_folders_editor(page)
    playwright.expect(page.locator(FOLDERS_LIST_ITEMS)).to_have_text(["/work/zeta"])
    lookup = page.get_by_label("Look up pull requests with the GitHub CLI (gh)")
    status = page.locator(FOLDERS_STATUS)

    add_folder(page, "/work/alpha")
    add_folder(page, "/work/mid")
    lookup.check()
    folders_save_button(page).click()
    # The double sorts, so a page that kept its draft would show another order.
    playwright.expect(page.locator(FOLDERS_LIST_ITEMS)).to_have_text(
        ["/work/alpha", "/work/mid", "/work/zeta"]
    )
    assert settings.folder_calls == [{
        "revision": CHECKOUTS_REVISION,
        "folders": ["/work/zeta", "/work/alpha", "/work/mid"],
        "github_lookup": True,
    }]
    playwright.expect(status).to_have_text("Checkout folders saved.")
    assert lookup.is_checked()
    playwright.expect(folders_save_button(page)).to_be_disabled()
    first = settings.revision
    assert first != CHECKOUTS_REVISION

    # Clearing every folder with the box checked: the answer unchecks it, and
    # the message says why; the page does not predict that itself.
    for path in ("/work/alpha", "/work/mid", "/work/zeta"):
        page.get_by_role("button", name=f"Remove checkout folder {path}").click()
    assert lookup.is_checked()
    folders_save_button(page).click()
    playwright.expect(status).to_have_text(
        "Checkout folders saved. With no folders the pull-request lookup is off."
    )
    assert folder_paths(page) == []
    assert not lookup.is_checked()
    assert settings.folder_calls[1] == {
        "revision": first, "folders": [], "github_lookup": True,
    }
    assert settings.revision not in (CHECKOUTS_REVISION, first)


def test_checkout_folders_save_marks_each_refused_folder_with_its_problem(
    open_page, attention_checkouts
):
    """Each problem's submitted index marks its own row, and an edit clears the marks."""
    settings = BrowserSettings(revision=CHECKOUTS_REVISION)
    page, _ = open_folders_page(open_page, attention_checkouts, settings)
    toggle_folders_editor(page)
    playwright.expect(page.locator(FOLDERS_STATUS)).to_have_text("Current checkout folders loaded.")
    add_folder(page, "/gone")
    add_folder(page, "/work/fine")
    add_folder(page, "relative")
    settings.folder_problems = [
        {"index": 0, "reason": "missing"}, {"index": 2, "reason": "not_absolute"},
    ]
    folders_save_button(page).click()
    reasons = checkout_words()["folderReason"]
    playwright.expect(page.locator(FOLDERS_STATUS)).to_have_text(
        "The checkout folders were not saved. Fix the marked folders and save again."
    )
    assert folder_marks(page) == [
        ["true", reasons["missing"], True],
        [None, None, False],
        ["true", reasons["not_absolute"], True],
    ]
    assert reasons["missing"] != reasons["not_absolute"]
    assert folder_paths(page) == ["/gone", "/work/fine", "relative"]
    assert settings.folder_calls[0]["folders"] == ["/gone", "/work/fine", "relative"]

    # An edit moves the positions the marks name, so it clears them.
    page.get_by_role("button", name="Remove checkout folder /gone").click()
    assert folder_marks(page) == [[None, None, False], [None, None, False]]


def test_checkout_folders_save_keeps_the_draft_through_each_refusal(
    open_page, attention_checkouts
):
    """Failure, an error code, reload-needed and conflict each keep the draft."""
    settings = BrowserSettings(revision=CHECKOUTS_REVISION)
    settings.folders = ["/work/alpha"]
    page, _ = open_folders_page(open_page, attention_checkouts, settings)
    toggle_folders_editor(page)
    playwright.expect(page.locator(FOLDERS_LIST_ITEMS)).to_have_text(["/work/alpha"])
    status = page.locator(FOLDERS_STATUS)
    save = folders_save_button(page)
    cancel = page.get_by_role("button", name="Cancel checkout folder changes", exact=True)
    reload_button = page.get_by_role("button", name="Reload checkout folders", exact=True)
    draft = ["/work/alpha", "/work/beta"]
    add_folder(page, "/work/beta")

    settings.fail_next = True
    save.click()
    playwright.expect(status).to_have_text("Save failed: the checkout folders were not saved.")
    assert folder_paths(page) == draft
    playwright.expect(reload_button).to_be_hidden()
    assert settings.folders == ["/work/alpha"]

    settings.folder_save_error = "settings_unavailable"
    save.click()
    playwright.expect(status).to_have_text("Save failed: settings_unavailable")
    assert folder_paths(page) == draft

    # The runtime's null state is neither the saved list nor a protocol error.
    settings.folder_reload_needed = True
    save.click()
    playwright.expect(status).to_have_text(
        "Save not applied: the saved checkout folders are not available to compare. "
        "Reload checkout folders to replace this unsaved draft and try again."
    )
    assert folder_paths(page) == draft
    playwright.expect(reload_button).to_be_visible()
    playwright.expect(cancel).to_be_enabled()
    cancel.click()
    assert folder_paths(page) == ["/work/alpha"]
    playwright.expect(reload_button).to_be_visible()
    settings.folders = ["/work/alpha", "/work/served"]
    reload_button.click()
    playwright.expect(page.locator(FOLDERS_LIST_ITEMS)).to_have_text(
        ["/work/alpha", "/work/served"]
    )
    playwright.expect(reload_button).to_be_hidden()

    # A save by someone else moves the revision this draft was made under.
    add_folder(page, "/work/gamma")
    outside = settings.save_checkout_settings(settings.revision, ["/work/outside"], False)
    assert outside["outcome"] == "success"
    save.click()
    playwright.expect(status).to_have_text(
        "Save not applied: the checkout folders changed elsewhere. "
        "Reload checkout folders to replace this unsaved draft and try again."
    )
    assert folder_paths(page) == ["/work/alpha", "/work/served", "/work/gamma"]
    reload_button.click()
    playwright.expect(page.locator(FOLDERS_LIST_ITEMS)).to_have_text(["/work/outside"])
    assert [call["folders"] for call in settings.folder_calls] == [
        draft, draft, draft, ["/work/outside"], ["/work/alpha", "/work/served", "/work/gamma"],
    ]


def test_checkout_folders_save_in_flight_keeps_focus_on_save(open_page, attention_checkouts):
    """While the save is held every control is aria-disabled and focus stays on Save."""

    class HeldFolderSave(BrowserSettings):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.save_gate = threading.Event()
            self.save_entered = threading.Event()

        def save_checkout_settings(self, revision, folders, github_lookup):
            self.save_entered.set()
            self.save_gate.wait(timeout=10)
            return super().save_checkout_settings(revision, folders, github_lookup)

    settings = HeldFolderSave(revision=CHECKOUTS_REVISION)
    settings.folders = ["/work/alpha"]
    page, _ = open_folders_page(open_page, attention_checkouts, settings)
    toggle_folders_editor(page)
    playwright.expect(page.locator(FOLDERS_LIST_ITEMS)).to_have_text(["/work/alpha"])
    add_folder(page, "/work/beta")
    save = folders_save_button(page)
    save.focus()
    arm_focus_sampler(page)
    try:
        save.press("Enter")
        assert settings.save_entered.wait(timeout=5)
        playwright.expect(save).to_be_focused()
        controls = page.locator(
            f"{FOLDERS_EDITOR} button:visible, {FOLDERS_EDITOR} input:visible"
        )
        assert controls.count() >= 6
        for index in range(controls.count()):
            control = controls.nth(index)
            assert control.get_attribute("aria-disabled") == "true"
            assert control.get_attribute("disabled") is None
        measurement = assert_focus_indicator(save)
        assert 0 < measurement["opacity"] < 1, measurement
        # A dimmed control does nothing: the draft is untouched by its click.
        # Playwright treats aria-disabled as not actionable, so the click is forced.
        page.get_by_role("button", name="Remove checkout folder /work/beta").click(force=True)
        assert folder_paths(page) == ["/work/alpha", "/work/beta"]
        assert len(settings.folder_calls) == 0
        save.focus()
        assert_focus_sampler_clean(page)
    finally:
        settings.save_gate.set()
    playwright.expect(page.locator(FOLDERS_STATUS)).to_have_text("Checkout folders saved.")
    playwright.expect(save).to_be_disabled()
    # Save has nothing left to save, so focus moved to the nearest operable control.
    playwright.expect(page.locator("#checkout-folders-github")).to_be_focused()
    assert page.locator(f"{FOLDERS_EDITOR} [aria-disabled]").count() == 0
    assert_focus_sampler_clean(page)


def test_checkout_folders_save_refusals_keep_focus_in_the_editor(
    open_page, attention_checkouts
):
    """After each refusal focus is on Save, or on another operable editor control when
    Save is not operable, never the body."""
    settings = BrowserSettings(revision=CHECKOUTS_REVISION)
    settings.folders = ["/work/alpha"]
    page, _ = open_folders_page(open_page, attention_checkouts, settings)
    toggle_folders_editor(page)
    playwright.expect(page.locator(FOLDERS_LIST_ITEMS)).to_have_text(["/work/alpha"])
    save = folders_save_button(page)
    status = page.locator(FOLDERS_STATUS)

    def focus_is_in_editor_and_operable():
        return page.evaluate(
            """([selector]) => {
              const active = document.activeElement;
              const editor = document.querySelector(selector);
              return !!editor && editor.contains(active) && active !== editor &&
                !active.disabled;
            }""",
            [FOLDERS_EDITOR],
        )

    add_folder(page, "/work/beta")
    save.focus()
    settings.fail_next = True
    save.press("Enter")
    playwright.expect(status).to_have_text("Save failed: the checkout folders were not saved.")
    playwright.expect(save).to_be_focused()

    settings.folder_problems = [{"index": 0, "reason": "missing"}]
    save.press("Enter")
    playwright.expect(status).to_contain_text("were not saved. Fix the marked folders")
    playwright.expect(save).to_be_focused()
    assert folder_marks(page)[0][0] == "true"

    # A conflict disables Save until a reload; focus must not fall to the body.
    settings.revision = "e" * 64
    save.press("Enter")
    playwright.expect(status).to_contain_text("changed elsewhere")
    assert focus_is_in_editor_and_operable()


def test_checkout_folders_reload_after_a_refusal_leaves_no_stale_marks(
    open_page, attention_checkouts
):
    """A failed reload shows the saved list unmarked, not the refused draft's marks."""
    settings = BrowserSettings(revision=CHECKOUTS_REVISION)
    settings.folders = ["/work/alpha"]
    page, _ = open_folders_page(open_page, attention_checkouts, settings)
    toggle_folders_editor(page)
    playwright.expect(page.locator(FOLDERS_LIST_ITEMS)).to_have_text(["/work/alpha"])
    add_folder(page, "/gone")
    settings.folder_problems = [{"index": 0, "reason": "missing"}]
    folders_save_button(page).click()
    playwright.expect(page.locator(f"{FOLDERS_LIST_ITEMS}[aria-invalid]")).to_have_count(1)
    # The same draft now meets a changed revision: the marks stay with the
    # draft and Reload is offered.
    settings.save_checkout_settings(settings.revision, ["/work/alpha"], False)
    folders_save_button(page).click()
    reload_button = page.get_by_role("button", name="Reload checkout folders", exact=True)
    playwright.expect(reload_button).to_be_visible()
    assert folder_marks(page)[0][0] == "true"

    settings.fail_next_folder_load = True
    reload_button.click()
    playwright.expect(page.locator(FOLDERS_STATUS)).to_have_text(
        "Could not load checkout folders: settings_unavailable"
    )
    assert folder_paths(page) == ["/work/alpha"]
    assert folder_marks(page) == [[None, None, False]]


# ---------------------------------------------------------------------------
# The checkout folders editor: one revision for both editors, the refresh after a save
# ---------------------------------------------------------------------------

BOARD_SAVE_BUTTON = "Save"
FOLDERS_CHANGED_ELSEWHERE = (
    "The checkout folders changed elsewhere. Reload checkout folders to replace this "
    "unsaved draft."
)
NO_FOLDERS_WORDS = "No checkout folders are configured."


def board_save_button(page):
    return page.get_by_role("button", name=BOARD_SAVE_BUTTON, exact=True)


def move_second_stage_up(page):
    """Draft a Board settings change: the second stage row moves up."""
    open_stage_editor(page)
    page.locator("#stage-order-list .stage-order-item").nth(1).get_by_role(
        "button", name=re.compile(r"^Move .* up$")
    ).click()
    playwright.expect(board_save_button(page)).to_be_enabled()


def open_two_editors(open_page, attention_checkouts, settings, double=None):
    """Open the checkouts view with the folder editor open beside the Board settings."""
    double = double or BrowserCheckouts(attention_checkouts.document)
    settings.checkouts = double
    page, double = open_folders_page(open_page, attention_checkouts, settings, double)
    playwright.expect(page.locator("#stage-order-status")).to_contain_text("board settings loaded")
    toggle_folders_editor(page)
    return page, double


def wait_for_catalog_installs(page, count):
    """Wait for the page's ``count``-th catalog response and let it install."""
    wait_for_responses(page, "catalogResponses", count)
    settle_frames(page)


def test_checkout_folders_save_keeps_a_board_settings_draft_current(
    open_page, attention_checkouts
):
    """The reader's own folder save is not a change made elsewhere to the Board settings."""
    settings = HexRevisionSettings(revision=CHECKOUTS_REVISION)
    settings.folders = ["/work/alpha"]
    page, _ = open_two_editors(open_page, attention_checkouts, settings)
    move_second_stage_up(page)
    draft = stage_editor_order(page)
    add_folder(page, "/work/beta")
    folders_save_button(page).click()
    playwright.expect(page.locator(FOLDERS_STATUS)).to_have_text("Checkout folders saved.")
    wait_for_catalog_installs(page, 2)
    saved_revision = settings.revision
    assert saved_revision != CHECKOUTS_REVISION

    status = page.locator("#stage-order-status")
    assert status.inner_text() != CHANGED_ELSEWHERE
    assert stage_editor_order(page) == draft
    playwright.expect(board_save_button(page)).to_be_enabled()
    board_save_button(page).click()
    playwright.expect(status).to_have_text("Board settings saved.")
    assert [call[0] for call in settings.calls] == [saved_revision]


def test_board_settings_save_keeps_a_checkout_folders_draft_current(
    open_page, attention_checkouts
):
    """The reader's own Board settings save is not a change made elsewhere to the folders."""
    settings = HexRevisionSettings(revision=CHECKOUTS_REVISION)
    settings.folders = ["/work/alpha"]
    page, _ = open_two_editors(open_page, attention_checkouts, settings)
    playwright.expect(page.locator(FOLDERS_LIST_ITEMS)).to_have_text(["/work/alpha"])
    add_folder(page, "/work/beta")
    status = page.locator(FOLDERS_STATUS)
    playwright.expect(status).to_have_text("Unsaved checkout folder changes.")
    move_second_stage_up(page)
    board_save_button(page).click()
    playwright.expect(page.locator("#stage-order-status")).to_have_text("Board settings saved.")
    wait_for_catalog_installs(page, 2)
    saved_revision = settings.revision
    assert saved_revision != CHECKOUTS_REVISION

    assert folder_paths(page) == ["/work/alpha", "/work/beta"]
    playwright.expect(status).to_have_text("Unsaved checkout folder changes.")
    assert settings.folder_gets == 1
    assert page.get_by_role("button", name="Reload checkout folders", exact=True).is_hidden()
    folders_save_button(page).click()
    playwright.expect(status).to_have_text("Checkout folders saved.")
    assert [call["revision"] for call in settings.folder_calls] == [saved_revision]


def test_checkout_folders_failed_save_advances_no_revision(open_page, attention_checkouts):
    """A failed folder save leaves the Board settings on the revision it holds."""

    class FailureNamesOtherRevision(HexRevisionSettings):
        def save_checkout_settings(self, revision, folders, github_lookup):
            if not self.fail_next:
                return super().save_checkout_settings(revision, folders, github_lookup)
            self.fail_next = False
            self.folder_calls.append(
                {"revision": revision, "folders": list(folders), "github_lookup": github_lookup}
            )
            return {"folders": [], "github_lookup": False, "revision": "9" * 64,
                    "outcome": "failure"}

    settings = FailureNamesOtherRevision(revision=CHECKOUTS_REVISION)
    settings.folders = ["/work/alpha"]
    page, _ = open_two_editors(open_page, attention_checkouts, settings)
    add_folder(page, "/work/beta")
    settings.fail_next = True
    folders_save_button(page).click()
    playwright.expect(page.locator(FOLDERS_STATUS)).to_have_text(
        "Save failed: the checkout folders were not saved."
    )
    assert len(settings.folder_calls) == 1
    move_second_stage_up(page)
    board_save_button(page).click()
    playwright.expect(page.locator("#stage-order-status")).to_have_text("Board settings saved.")
    assert [call[0] for call in settings.calls] == [CHECKOUTS_REVISION]
    # The folder draft is untouched, and its next save still sends the revision.
    assert folder_paths(page) == ["/work/alpha", "/work/beta"]


@pytest.mark.parametrize("board_loads", [True, False], ids=["board-loaded", "board-load-failed"])
def test_checkout_folders_editor_follows_a_record_changed_elsewhere(
    open_page, attention_checkouts, board_loads
):
    """An installed catalog reloads an idle folder editor and flags a drafted one."""
    settings = HexRevisionSettings(revision=CHECKOUTS_REVISION)
    settings.folders = ["/work/alpha"]
    settings.fail_next_load = not board_loads
    double = BrowserCheckouts(attention_checkouts.document)
    settings.checkouts = double
    page, _ = open_folders_page(open_page, attention_checkouts, settings, double)
    toggle_folders_editor(page)
    playwright.expect(page.locator(FOLDERS_LIST_ITEMS)).to_have_text(["/work/alpha"])
    status = page.locator(FOLDERS_STATUS)
    installs = [1]

    def refresh_board_view():
        installs[0] += 1
        page.locator("#view-board").click()
        page.get_by_role("button", name="Refresh view", exact=True).click()
        wait_for_catalog_installs(page, installs[0])
        page.locator("#view-checkouts").click()
        playwright.expect(page.locator("#checkouts")).to_be_visible()

    # No draft: the editor shows the other page's folders, with no message.
    settings.save_checkout_settings(settings.revision, ["/work/alpha", "/work/other"], False)
    refresh_board_view()
    playwright.expect(page.locator(FOLDERS_LIST_ITEMS)).to_have_text(
        ["/work/alpha", "/work/other"]
    )
    playwright.expect(status).to_have_text("Current checkout folders loaded.")
    assert page.get_by_role("button", name="Reload checkout folders", exact=True).is_hidden()

    # A draft: kept, named as changed elsewhere, and replaced by Reload.
    add_folder(page, "/work/draft")
    settings.save_checkout_settings(settings.revision, ["/work/alpha", "/work/third"], False)
    refresh_board_view()
    playwright.expect(status).to_have_text(FOLDERS_CHANGED_ELSEWHERE)
    assert folder_paths(page) == ["/work/alpha", "/work/other", "/work/draft"]
    playwright.expect(folders_save_button(page)).to_be_disabled()
    page.get_by_role("button", name="Reload checkout folders", exact=True).click()
    playwright.expect(page.locator(FOLDERS_LIST_ITEMS)).to_have_text(
        ["/work/alpha", "/work/third"]
    )
    playwright.expect(status).to_have_text("Current checkout folders loaded.")


def test_checkout_folders_save_refreshes_the_catalog_and_reads_the_new_folders(
    open_page, attention_checkouts
):
    """With no tick, a save installs a catalog and one read shows the new folders."""
    settings = HexRevisionSettings(revision=CHECKOUTS_REVISION)
    double = BrowserCheckouts()
    settings.checkouts = double
    page, _ = open_folders_page(open_page, attention_checkouts, settings, double)
    body = page.locator("#checkouts-body")
    playwright.expect(body).to_contain_text(NO_FOLDERS_WORDS)
    playwright.expect(page.locator("#checkout-folders-path")).to_be_visible()  # opens itself
    catalogs = page.evaluate("window.catalogResponses.length")
    reads = page.evaluate("window.checkoutsResponses.length")
    calls = double.calls
    order, completed = list(settings.order), list(settings.completed)
    add_folder(page, "/work/alpha")
    folders_save_button(page).click()
    playwright.expect(page.locator(FOLDERS_STATUS)).to_have_text("Checkout folders saved.")
    wait_for_responses(page, "catalogResponses", catalogs + 1)
    wait_for_responses(page, "checkoutsResponses", reads + 1)
    settle_frames(page)

    assert page.evaluate("window.catalogResponses.length") == catalogs + 1
    assert page.evaluate("window.checkoutsResponses.length") == reads + 1
    assert double.calls == calls + 1
    assert page.locator("#refresh").get_attribute("data-pending") == "false"
    playwright.expect(body).not_to_contain_text(NO_FOLDERS_WORDS)
    assert SETTINGS_SENTENCE not in page.locator("#checkouts-status-line").inner_text()
    assert (settings.order, settings.completed) == (order, completed)
    assert settings.calls == []


def test_checkout_folders_save_refresh_clears_an_update_marked_by_a_poll(
    open_page, attention_checkouts
):
    """A tick between the save's commit and its refresh leaves no waiting update."""

    class HeldCommit(HexRevisionSettings):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.committed = threading.Event()
            self.release = threading.Event()

        def save_checkout_settings(self, revision, folders, github_lookup):
            answer = super().save_checkout_settings(revision, folders, github_lookup)
            self.committed.set()
            self.release.wait(timeout=10)
            return answer

    settings = HeldCommit(revision=CHECKOUTS_REVISION)
    double = BrowserCheckouts()
    settings.checkouts = double
    page, _ = open_folders_page(open_page, attention_checkouts, settings, double)
    playwright.expect(page.locator("#checkout-folders-path")).to_be_visible()  # opens itself
    add_folder(page, "/work/alpha")
    refresh = page.locator("#refresh")
    try:
        folders_save_button(page).click()
        assert settings.committed.wait(timeout=5)
        fire_poll(page)
        playwright.expect(refresh).to_have_attribute("data-pending", "true")
    finally:
        settings.release.set()
    playwright.expect(page.locator(FOLDERS_STATUS)).to_have_text("Checkout folders saved.")
    playwright.expect(refresh).to_have_attribute("data-pending", "false")


def test_checkout_folders_save_sends_no_second_checkouts_request_while_one_is_in_flight(
    open_page, attention_checkouts
):
    """The save's read honors the one in-flight slot; the next tick shows the folders."""
    settings = HexRevisionSettings(revision=CHECKOUTS_REVISION)
    double = BrowserCheckouts()
    settings.checkouts = double
    page, _ = open_folders_page(open_page, attention_checkouts, settings, double)
    playwright.expect(page.locator("#checkout-folders-path")).to_be_visible()  # opens itself
    add_folder(page, "/work/alpha")
    calls = double.calls
    gate = double.hold()
    fire_poll(page)
    assert double.entered.wait(timeout=5)
    assert double.calls == calls + 1
    catalogs = page.evaluate("window.catalogResponses.length")
    folders_save_button(page).click()
    playwright.expect(page.locator(FOLDERS_STATUS)).to_have_text("Checkout folders saved.")
    wait_for_catalog_installs(page, catalogs + 1)
    # The refresh's catalog is in, and still only the tick's read has reached the double.
    assert double.calls == calls + 1
    reads = page.evaluate("window.checkoutsResponses.length")
    gate.set()
    wait_for_responses(page, "checkoutsResponses", reads + 1)
    settle_frames(page)
    assert double.calls == calls + 1
    double.gate = None
    fire_checkouts_poll(page)
    settle_frames(page)
    playwright.expect(page.locator("#checkouts-body")).not_to_contain_text(NO_FOLDERS_WORDS)


def test_board_settings_save_leaves_a_flagged_checkout_folders_draft_unsaveable(
    open_page, attention_checkouts
):
    """A folder draft already named as changed elsewhere holds no revision to advance."""
    settings = HexRevisionSettings(revision=CHECKOUTS_REVISION)
    settings.folders = ["/work/alpha"]
    page, _ = open_two_editors(open_page, attention_checkouts, settings)
    playwright.expect(page.locator(FOLDERS_LIST_ITEMS)).to_have_text(["/work/alpha"])
    add_folder(page, "/work/draft")
    settings.save_checkout_settings(settings.revision, ["/work/alpha", "/work/third"], False)
    outside_saves = len(settings.folder_calls)
    page.locator("#view-board").click()
    page.get_by_role("button", name="Refresh view", exact=True).click()
    wait_for_catalog_installs(page, 2)
    page.locator("#view-checkouts").click()
    status = page.locator(FOLDERS_STATUS)
    playwright.expect(status).to_have_text(FOLDERS_CHANGED_ELSEWHERE)
    playwright.expect(page.locator("#stage-order-status")).to_have_text(
        "Current board settings loaded."
    )

    move_second_stage_up(page)
    board_save_button(page).click()
    playwright.expect(page.locator("#stage-order-status")).to_have_text("Board settings saved.")
    wait_for_catalog_installs(page, 3)

    # A further edit redraws the editor; the save must still be refused for lack of a revision.
    add_folder(page, "/work/more")
    playwright.expect(folders_save_button(page)).to_be_disabled()
    playwright.expect(
        page.get_by_role("button", name="Reload checkout folders", exact=True)
    ).to_be_visible()
    assert len(settings.folder_calls) == outside_saves
