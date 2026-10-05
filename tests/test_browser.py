import json
import os
import re
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from test_server import raw_catalog, reported_contract_cases

from nyx.app_runtime import ApplicationRuntime
from nyx.catalog import scan_catalog
from nyx.models import SCHEMA_VERSION, canonical_digest, parse_catalog
from nyx.server import CatalogError, _create_application_server, create_server

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

    def __init__(self, order=(), completed=(), revision="revision-1", root=None):
        self.order = list(order)
        self.completed = list(completed)
        self.revision = revision
        self.root = root
        self.calls = []
        self.get_calls = 0
        self.fail_next = False
        self.fail_next_load = False

    def get_settings(self):
        self.get_calls += 1
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
        if self.fail_next:
            self.fail_next = False
            return {"order": list(self.order), "completed": list(self.completed), "revision": self.revision, "outcome": "failure"}
        if revision != self.revision:
            return {"order": list(self.order), "completed": list(self.completed), "revision": self.revision, "outcome": "conflict"}
        self.order = list(order)
        self.completed = list(completed)
        self.revision = f"revision-{len(self.calls) + 1}"
        return {"order": list(self.order), "completed": list(self.completed), "revision": self.revision, "outcome": "success"}


class RootSwitchSettings(BrowserSettings):
    """Change the active workspace while a saved catalog response is delayed."""

    def switch_root(self):
        self.order = ["Queue"]
        self.completed = ["Done"]
        self.revision = "root-b-revision"


@pytest.fixture
def open_page():
    with playwright.sync_playwright() as api:
        browser = api.chromium.launch()
        created = []

        def launch(client, *, color_scheme="light", init_script=None, settings=None):
            application = None
            if settings is None:
                server = create_server(client)
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
            else:
                application = ApplicationRuntime(
                    port=0,
                    deadline=time.monotonic() + 5,
                    server_factory=lambda **kwargs: _create_application_server(
                        provider=client,
                        port=kwargs["port"],
                        settings_provider=kwargs["settings_provider"],
                    ),
                )
                application.get_settings = settings.get_settings
                application.get_workspace = settings.get_workspace
                application.save_settings = settings.save_settings
                client.settings = settings
                application.start(static_ready=lambda: True)
                server = application.server
                thread = application.http_thread
            context = browser.new_context(color_scheme=color_scheme)
            page = context.new_page()
            if init_script:
                page.add_init_script(init_script)
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.wait_for_function(
                "document.querySelector('#board .card, #board .board-empty, #board-issues')",
                timeout=15000,
            )
            page.wait_for_timeout(150)
            created.append((application, server, thread, page))
            return page

        try:
            yield launch
        finally:
            for application, server, thread, page in created:
                page.context.close()
                if application is None:
                    server.shutdown()
                    thread.join()
                    server.server_close()
                else:
                    assert application.shutdown(time.monotonic() + 2)
            browser.close()


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


def test_card_right_click_copies_full_directory_without_changing_selection(open_page, tmp_path):
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
    assert page.evaluate("navigator.clipboard.readText()") == str(root / relative)
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
    page.get_by_text("Board row order saved.", exact=True).wait_for()
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
    page_b.get_by_text("Current board row order loaded.", exact=True).wait_for()
    page_b.get_by_role("checkbox", name="Counts as finished: Under Development").uncheck()
    page_b.get_by_role("button", name="Save").click()
    # The competing save must commit, and its own catalog refresh must consume
    # the next snapshot, before the delayed refresh is released; otherwise the
    # delayed snapshot could be stamped with a revision that is still current.
    page_b.get_by_text("Board row order saved.", exact=True).wait_for()
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

    page_a.get_by_text("Current board row order loaded.", exact=True).wait_for()
    playwright.expect(gate_title).to_have_text("Fresh gate")
    assert page_a.evaluate("window.sawRejectedSnapshot") is False
    assert settings.completed == []


@pytest.mark.parametrize("case", ["stale", "null", "malformed", "schema-4"])
def test_post_save_refresh_rejects_invalid_revision_and_schema_payloads(open_page, case):
    valid = dependency_state_payload("unsatisfied")
    bad = dependency_state_payload("satisfied", titles={"gate": "Rejected gate"})
    if case == "stale":
        bad["configuration_revision"] = "revision-before-save"
    elif case == "null":
        bad["configuration_revision"] = None
    elif case == "malformed":
        bad["configuration_revision"] = {"revision": "bad"}
    else:
        bad["schema_version"] = 4
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
        page.get_by_text("Board row order saved.", exact=True).wait_for(timeout=15000)
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

    assert page.locator("#stage-order-status").inner_text() == "Current board row order loaded."
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
    page.get_by_text("Board row order saved.", exact=True).wait_for()

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
    page.get_by_text("Board row order saved.", exact=True).wait_for()
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


@pytest.mark.parametrize(("kind", "reason"),
                         [("claim", reason) for reason in CLAIM_EDGE_REASONS] +
                         [("completion", reason) for reason in COMPLETION_EDGE_REASONS])
def test_browser_admits_every_python_valid_typed_edge_reason(open_page, kind, reason):
    value = admitted_reason_payload(kind, reason)
    assert parse_catalog(value).entries
    page = open_page(StaticClient(value))
    assert page.locator(f'.card[data-package-id="{GATE}"]').count() == 1


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
    page = open_page(client)
    page.locator("#board").get_by_text("Latest refresh issue: producer_unavailable", exact=True).wait_for(
        timeout=15000
    )
    page.locator("#board .card").first.wait_for(timeout=15000)
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
    page = open_page(SequenceClient([first, second]))
    page.wait_for_selector("#refresh.pending", timeout=20000)
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
        "Unsaved board row order changes cancelled."
    )
    page.get_by_role("button", name="Move Under Development up").press("Enter")
    page.get_by_role("button", name="Save").click()
    page.get_by_text("Board row order saved.", exact=True).wait_for()
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
    page2.get_by_text("Board row order saved.", exact=True).wait_for()
    assert settings.order == []
    page2.wait_for_function(
        "() => JSON.stringify([...document.querySelectorAll('.row-head')].map(row => row.firstChild.textContent)) === "
        "JSON.stringify(['Queue', 'Under Development'])"
    )
    assert row_labels(page2) == ["Queue", "Under Development"]


def test_stage_order_retries_failed_initial_load_without_page_reload(open_page):
    settings = BrowserSettings()
    settings.fail_next_load = True
    page = open_page(StaticClient(board_payload()), settings=settings)
    page.locator("#stage-order-editor").wait_for()
    open_stage_editor(page)

    page.get_by_text(
        "Could not load board row order: producer_unavailable", exact=True
    ).wait_for()
    assert settings.get_calls == 1
    assert page.get_by_role("button", name="Save").is_disabled()
    retry = page.get_by_role("button", name="Reload board settings")
    assert retry.is_visible()

    retry.click()
    page.get_by_text("Current board row order loaded.", exact=True).wait_for()
    assert settings.get_calls == 2
    assert stage_editor_order(page) == ["Queue", "Under_Development"]
    page.get_by_role("button", name="Move Under Development up").click()
    page.get_by_role("button", name="Save").click()
    page.get_by_text("Board row order saved.", exact=True).wait_for()
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
    winning_page.get_by_text("Board row order saved.", exact=True).wait_for()
    assert settings.calls == [("revision-1", [], [])]

    stale_page.get_by_role("button", name="Move Under Development up").click()
    stale_page.get_by_role("button", name="Save").click()
    stale_page.get_by_text(re.compile("Save not applied: conflict"), exact=False).wait_for()
    assert stage_editor_order(stale_page) == ["Under_Development", "Queue"]
    assert stale_page.get_by_role("button", name="Save").is_disabled()
    reload = stale_page.get_by_role("button", name="Reload board settings")
    assert reload.is_visible()

    reload.click()
    stale_page.get_by_text("Current board row order loaded.", exact=True).wait_for()
    assert settings.get_calls == 4
    assert stage_editor_order(stale_page) == ["Queue", "Under_Development"]
    stale_page.get_by_role("button", name="Move Under Development up").click()
    stale_page.get_by_role("button", name="Save").click()
    stale_page.get_by_text("Board row order saved.", exact=True).wait_for()
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
    winning_page.get_by_text("Board row order saved.", exact=True).wait_for()
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
        "Save failed: the board row order was not persisted.", exact=True
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
    page.get_by_text("Board row order saved.", exact=True).wait_for()
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


def test_poll_reconciles_a_b_a_before_a_click_fetches_the_next_manual_snapshot(open_page):
    first = board_payload(titles={"step_one": "A"})
    middle = board_payload(titles={"step_one": "B"})
    latest = board_payload(titles={"step_one": "A"})
    manual = board_payload(titles={"step_one": "C"})
    client = SequenceClient([first, middle, latest, manual])
    page = open_page(client)

    # The browser's periodic poll observes B and then the displayed A again.
    page.wait_for_selector("#refresh.pending", timeout=15000)
    assert page.locator("#board").get_by_text("A", exact=True).count() == 1
    page.wait_for_function(
        "() => !document.querySelector('#refresh').classList.contains('pending')", timeout=15000
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
    page = open_page(client)

    page.wait_for_selector("#refresh.pending", timeout=15000)
    # Let the second periodic response supersede B while A remains displayed.
    page.wait_for_timeout(10500)
    assert page.locator("#refresh").inner_text() == "Apply update"
    page.click("#refresh")
    page.locator("#board").get_by_text("C", exact=True).wait_for(timeout=15000)
    assert client.calls == 3
    assert page.locator("#board").get_by_text("B", exact=True).count() == 0


def test_failed_poll_keeps_the_latest_successful_pending_snapshot(open_page):
    first = board_payload(titles={"step_one": "A"})
    pending = board_payload(titles={"step_one": "B"})
    client = SequenceClient([first, pending, CatalogError("producer_protocol_error")])
    page = open_page(client)

    page.wait_for_selector("#refresh.pending", timeout=15000)
    page.locator("#board").get_by_text("Latest refresh issue: producer_protocol_error", exact=True).wait_for(
        timeout=15000
    )
    assert page.locator("#board").get_by_text("A", exact=True).count() == 1
    assert page.locator("#refresh").inner_text() == "Apply update"
    assert "producer_protocol_error" in page.locator("#board-issues summary").inner_text()
    assert page.locator("#board-issues").get_attribute("open") is None

    # The failed response did not erase B, the most recent successful candidate.
    page.click("#refresh")
    page.locator("#board").get_by_text("B", exact=True).wait_for(timeout=15000)
    assert client.calls == 3
    assert page.locator("#board-issues").count() == 0


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


def observe_refresh_status(page):
    assert page.locator("#refresh-status").count() == 1
    page.evaluate("""() => {
      window.refreshStatusRecords = 0;
      window.refreshStatusTexts = [];
      const status = document.querySelector('#refresh-status');
      new MutationObserver(records => {
        window.refreshStatusRecords += records.length;
        window.refreshStatusTexts.push(status.textContent);
      }).observe(status, {childList: true, characterData: true, subtree: true});
    }""")


def test_failed_poll_updates_issues_in_place_and_keeps_card_focus(open_page):
    page = open_page(SequenceClient([board_payload(), CatalogError("producer_unavailable")]))
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
    playwright.expect(status).to_have_text("Latest refresh issue: producer_unavailable", timeout=15000)
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
    page = open_page(SequenceClient([
        board_payload(), CatalogError("producer_timeout"), board_payload(),
    ]))
    card = page.locator(f'.card[data-package-id="{STEP_TWO}"]')
    card.focus()
    card.press("Enter")
    kept = card.element_handle()
    observe_removed_cards(page)
    page.locator("#board").get_by_text("Latest refresh issue: producer_timeout", exact=True).wait_for(
        timeout=15000
    )
    playwright.expect(page.locator("#refresh-status")).to_have_text(
        "Refresh issue resolved.", timeout=15000
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
    page = open_page(client)
    page.wait_for_selector("#refresh.pending", timeout=15000)
    page.locator("#board").get_by_text("Latest refresh issue: producer_protocol_error", exact=True).wait_for(
        timeout=15000
    )
    status = page.locator("#refresh-status")
    assert status.inner_text() == "Latest refresh issue: producer_protocol_error"
    observe_refresh_status(page)
    page.get_by_role("button", name="Apply update", exact=True).click()
    assert page.locator("#board").get_by_text("B", exact=True).count() == 1
    assert page.locator("#board-issues").count() == 0
    assert status.inner_text() == ""
    assert "Refresh issue resolved." not in page.evaluate("window.refreshStatusTexts")
    cleared_records = page.evaluate("window.refreshStatusRecords")
    playwright.expect(status).to_have_text("Latest refresh issue: producer_protocol_error", timeout=15000)
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
    page.wait_for_function("document.querySelectorAll('#board .card').length === 0", timeout=15000)
    page.locator("#board-issues summary").click()
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
    page = open_page(SequenceClient([first, second]))
    card = page.locator(f'[data-package-id="{STEP_ONE}"]')
    assert card.evaluate("el => getComputedStyle(el).backgroundColor") == DARK_CARD
    card.click()
    refresh = page.locator("#refresh")
    playwright.expect(refresh).to_have_text("Apply update", timeout=15000)
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
    page = open_page(client)
    page.locator("#board").get_by_text("Latest refresh issue: producer_timeout", exact=True).wait_for(timeout=15000)
    page.wait_for_selector("#refresh.pending", timeout=15000)
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
        )
        page.locator("#board").get_by_text("Latest refresh issue: producer_protocol_error", exact=True).wait_for(
            timeout=15000
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
    page = open_page(SequenceClient([first, second]))
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
    page.wait_for_selector("#refresh.pending", timeout=15000)
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
    page = open_page(SequenceClient([first, second]))
    done = page.locator('.card[data-package-path="Fictional/Done/package"]')
    done.click()
    assert done.get_attribute("aria-pressed") == "true"

    page.wait_for_selector("#refresh.pending", timeout=15000)
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
        page.locator("#board").get_by_text("Latest refresh issue: producer_protocol_error", exact=True).wait_for(
            timeout=15000
        )

    assert sorted(page.locator("#board .card").evaluate_all(
        "cards => cards.map(card => card.dataset.packagePath)"
    )) == expected_paths
    assert page.locator("#refresh").inner_text() == "Refresh view"


def test_digest_consistent_duplicate_policy_keeps_last_valid_board(open_page):
    valid = json.loads(lifecycle_payload())
    duplicate = json.loads(lifecycle_payload(completed_stages=("Done",)))
    duplicate["terminal_stages"] = ["Done", "Done"]
    _reseal(duplicate)
    assert duplicate["catalog_digest"] == canonical_digest(duplicate)

    from nyx import server as server_module

    def passthrough_catalog(candidate):
        return candidate if isinstance(candidate, RawCatalog) else parse_catalog(candidate)

    client = RawSequenceClient([valid, duplicate])
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(server_module, "parse_catalog", passthrough_catalog)
        page = open_page(client)
        page.locator("#board").get_by_text("Latest refresh issue: producer_protocol_error", exact=True).wait_for(
            timeout=15000
        )

    assert page.locator("#board .card").count() == 7
    assert page.locator('.card[data-package-path="Fictional/Done/package"]').count() == 1
    assert page.locator("#refresh").inner_text() == "Refresh view"
    assert client.calls >= 2


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(terminal_stages=["Done", "Done"]),
    lambda value: value.update(terminal_stages=["Done", "Archive"]),
    lambda value: value.pop("terminal_stages"),
    lambda value: value.update(schema_version=7),
    lambda value: value.update(visibility={}),
    lambda value: value["entries"][0].update(board_visible=True),
])
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


def test_digest_consistent_reversed_inventory_keeps_last_valid_board(open_page):
    valid = json.loads(lifecycle_payload())
    invalid = json.loads(lifecycle_payload())
    invalid["inventory"]["stages"].reverse()
    _reseal(invalid)

    from nyx import server as server_module

    def passthrough_catalog(candidate):
        return candidate if isinstance(candidate, RawCatalog) else parse_catalog(candidate)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(server_module, "parse_catalog", passthrough_catalog)
        page = open_page(RawSequenceClient([valid, invalid]))
        page.locator("#board").get_by_text("Latest refresh issue: producer_protocol_error", exact=True).wait_for(
            timeout=15000
        )

    assert page.locator("#board .card").count() == 7
    assert page.locator("#refresh").inner_text() == "Refresh view"


def _malformed_browser_inventory(case):
    value = json.loads(lifecycle_payload())
    if case == "schema-3":
        value["schema_version"] = 3
        value["catalog_digest"] = canonical_digest(value)
        return value
    elif case == "unknown-inventory-key":
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
        assert page.locator("#board-issues").count() == 0
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
