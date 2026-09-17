import json
import re
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from nyx.catalog import scan_catalog
from nyx.models import canonical_digest, parse_catalog
from nyx.server import CatalogError, create_server

playwright = pytest.importorskip("playwright.sync_api")

STEP_ONE = "123e4567-e89b-42d3-a456-426614174000"
STEP_TWO = "123e4567-e89b-42d3-a456-426614174001"
GATE = "123e4567-e89b-42d3-a456-426614174002"
LOOSE = "123e4567-e89b-42d3-a456-426614174003"
XSS_TITLE = '<img src=x onerror="alert(1)"> loose package'


def _declared(title, project):
    return {
        "closure": "approved",
        "human_sanity_decision": "AFFIRMED",
        "sanity_recommendation": "PROCEED_TO_DESIGN",
        "status": "ready",
        "target_project": project,
        "title": title,
    }


def _edge(target_id, name):
    return {
        "target_package_id": target_id,
        "claim_name": name,
        "observed_state": "unsatisfied",
        "observed_evidence_ref": None,
        "resolved_state": "unsatisfied",
        "reason": "claim_unsatisfied",
    }


def _entry(package_id, path, lifecycle, title, project, prerequisites=None, diagnostics=None):
    stage = path.split("/")[1]
    return {
        "package_id": package_id,
        "package_path": path,
        "project": project,
        "stage": stage,
        "board_visible": lifecycle in {"under_development", "queue", "needs_fixes", "awaiting_retrospective"},
        "state": "complete",
        "declared": _declared(title, project),
        "diagnostics": [] if diagnostics is None else diagnostics,
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
                "diagnostics": [],
            },
            "superseded_by": {"package_id": None, "resolution": "not_declared", "diagnostics": []},
        },
        "transitive_diagnostics": [],
    }


def _reseal(value):
    entries = value["entries"]
    value["visibility"]["visible_entry_count"] = sum(
        entry["board_visible"] for entry in entries
    )
    value["visibility"]["hidden_entry_count"] = sum(
        not entry["board_visible"] for entry in entries
    )
    value["catalog_digest"] = canonical_digest(value)
    return value


def board_payload(*, titles=None):
    titles = titles or {}
    value = {
        "schema_version": 4,
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
        "visibility": {"hidden_stages": ["Archive", "Done", "In_Progress"],
                        "visible_entry_count": 4, "hidden_entry_count": 0},
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
                diagnostics=[{"code": "invalid_package", "message": "example diagnostic"}],
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


def lifecycle_payload(*, hidden_stages=()):
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
        entry["board_visible"] = stage not in hidden_stages
        if stage == "Done":
            entry["relationship"]["claims"] = [{
                "name": "release",
                "state": "satisfied",
                "evidence_ref": "sha256:" + "b" * 64,
                "diagnostics": [],
            }]
        entries.append(entry)
    value = {
        "schema_version": 4,
        "inventory": {
            "projects": [{"name": "Fictional", "availability": "complete"}],
            "stages": [
                {"project": "Fictional", "stage": stage, "availability": "complete"}
                for stage, *_rest in sorted(STAGE_ROWS)
            ],
        },
        "visibility": {
            "hidden_stages": list(hidden_stages),
            "visible_entry_count": 0,
            "hidden_entry_count": 0,
        },
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
COMPACT_HIDDEN = "123e4567-e89b-42d3-a456-426614174022"


def compact_payload(*, extra_stage=False):
    hidden_edge = _edge(COMPACT_HIDDEN, "release")
    hidden_edge.update(
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
            "Queue",
            "Visible dependent",
            "Alpha",
            prerequisites=[hidden_edge, visible_edge],
        ),
        _entry(
            COMPACT_HIDDEN,
            "HiddenOnly/Done/hidden",
            "Done",
            "Hidden prerequisite",
            "HiddenOnly",
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
        {"project": "Alpha", "stage": "Partial", "availability": "incomplete"},
        {"project": "Alpha", "stage": "Queue", "availability": "complete"},
        {"project": "Alpha", "stage": "Review", "availability": "complete"},
        {"project": "Alpha", "stage": "Testing", "availability": "complete"},
        {"project": "EmptyProject", "stage": "Empty", "availability": "complete"},
        {"project": "HiddenOnly", "stage": "Done", "availability": "complete"},
    ]
    if not extra_stage:
        stages.remove({"project": "Alpha", "stage": "Review", "availability": "complete"})
    value = {
        "schema_version": 4,
        "inventory": {
            "projects": [
                {"name": "Alpha", "availability": "complete"},
                {"name": "EmptyProject", "availability": "complete"},
                {"name": "HiddenOnly", "availability": "complete"},
            ],
            "stages": stages,
        },
        "visibility": {
            "hidden_stages": ["Done"],
            "visible_entry_count": 0,
            "hidden_entry_count": 0,
        },
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
        payload = scan_catalog(root, hidden_stages=(UNICODE_LOW, UNICODE_HIGH))
    value = json.loads(payload)
    return payload.encode("utf-8"), value


def reversed_unicode_payload(payload, kind):
    value = json.loads(payload)
    if kind == "policy":
        value["visibility"]["hidden_stages"].reverse()
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

    def fetch_catalog(self):
        return parse_catalog(self.payload)


class SequenceClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    def fetch_catalog(self):
        index = min(self.calls, len(self.payloads) - 1)
        self.calls += 1
        payload = self.payloads[index]
        if isinstance(payload, Exception):
            raise payload
        return parse_catalog(payload)


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


@pytest.fixture
def open_page():
    with playwright.sync_playwright() as api:
        browser = api.chromium.launch()
        created = []

        def launch(client, *, color_scheme="light", init_script=None):
            server = create_server(client)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            context = browser.new_context(color_scheme=color_scheme)
            page = context.new_page()
            if init_script:
                page.add_init_script(init_script)
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.wait_for_function(
                "document.querySelector('#board .card, #board .board-empty') || "
                "document.querySelector('#status').textContent.includes('failed')",
                timeout=15000,
            )
            page.wait_for_timeout(150)
            created.append((server, thread, page))
            return page

        try:
            yield launch
        finally:
            for server, thread, page in created:
                page.context.close()
                server.shutdown()
                thread.join()
                server.server_close()
            browser.close()


def card_titles(page, column=1, row="Under Development"):
    selector = (
        f".board-row:has(.row-head:text-is('{row}')) .cell:nth-of-type({column}) .card-title"
    )
    return page.locator(selector).all_inner_texts()


def test_board_renders_lifecycle_rows_and_project_columns(open_page):
    page = open_page(StaticClient(board_payload()))
    assert page.locator(".column-head").all_inner_texts() == ["Alpha", "Beta"]
    rows = page.locator(".row-head").all_inner_texts()
    assert rows == ["Under Development", "Queue"]
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
    page.get_by_text("Refresh failed: producer_unavailable", exact=True).wait_for(
        timeout=15000
    )
    page.locator("#board .card").first.wait_for(timeout=15000)
    assert page.locator("#board .card").count() == 4
    assert page.locator("#status").inner_text().startswith("Loaded 4 packages")
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


def test_selecting_a_card_shows_declared_values_and_diagnostics(open_page):
    page = open_page(StaticClient(board_payload()))
    page.locator('.card[data-package-path="Alpha/Under_Development/step-two"]').click()
    details = page.locator("#details")
    assert details.locator("h2").inner_text() == "Dependent step"
    assert details.get_by_text("Direct prerequisites", exact=True).count() == 1
    assert "Foundation step" in details.inner_text()
    assert details.get_by_text("example diagnostic", exact=False).count() == 1


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
    page.get_by_text("Update check failed: producer_protocol_error", exact=True).wait_for(
        timeout=15000
    )
    assert page.locator("#board").get_by_text("A", exact=True).count() == 1
    assert page.locator("#refresh").inner_text() == "Apply update"

    # The failed response did not erase B, the most recent successful candidate.
    page.click("#refresh")
    page.locator("#board").get_by_text("B", exact=True).wait_for(timeout=15000)
    assert client.calls == 3


def test_details_list_every_claim_while_the_graph_deduplicates_the_package_pair(open_page):
    value = json.loads(board_payload())
    dependent = value["entries"][3]
    build = _edge(STEP_ONE, "build")
    build.update(
        observed_state="unknown",
        observed_evidence_ref="sha256:" + "a" * 64,
        resolved_state="satisfied",
        reason="claim_satisfied",
    )
    testing = _edge(STEP_ONE, "testing")
    dependent["relationship"].update(
        prerequisites=[build, testing], direct_prerequisite_state="unsatisfied"
    )
    _reseal(value)
    page = open_page(StaticClient(value))

    page.locator(f'.card[data-package-id="{LOOSE}"]').click()
    details = page.locator("#details")
    claims = details.locator(".prerequisite-claim")
    assert claims.count() == 2
    assert claims.nth(0).locator(".prerequisite-target").inner_text() == "Foundation step"
    assert claims.nth(0).locator(".claim-name").inner_text() == "Claim: build"
    assert claims.nth(0).locator(".reported-state").inner_text() == "Reported state: satisfied"
    assert claims.nth(0).locator(".claim-reason").inner_text() == "Reason: claim_satisfied"
    assert claims.nth(1).locator(".claim-name").inner_text() == "Claim: testing"
    assert claims.nth(1).locator(".reported-state").inner_text() == "Reported state: unsatisfied"
    assert claims.nth(1).locator(".claim-reason").inner_text() == "Reason: claim_unsatisfied"
    assert connection_pairs(page) == {(STEP_ONE, STEP_TWO), (STEP_TWO, GATE), (STEP_ONE, LOOSE)}
    assert page.locator(
        f'.connection[data-source="{STEP_ONE}"][data-dependent="{LOOSE}"]'
    ).count() == 1


@pytest.mark.parametrize(
    ("participation", "state", "message", "unblocked"),
    [
        ("available", "no_declared_prerequisites", "No direct prerequisites.", True),
        ("available", "unknown", "Direct prerequisite information is unknown.", False),
        (
            "legacy",
            "relationship_unavailable",
            "Direct prerequisite information unavailable.",
            False,
        ),
    ],
)
def test_details_distinguish_confirmed_empty_from_unknown_or_unavailable_prerequisites(
    open_page, participation, state, message, unblocked
):
    value = json.loads(board_payload())
    relationship = value["entries"][0]["relationship"]
    relationship.update(
        participation=participation,
        direct_prerequisite_state=state,
        prerequisites=[],
    )
    _reseal(value)
    page = open_page(StaticClient(value))
    card = page.locator(f'.card[data-package-id="{GATE}"]')
    card.click()

    details_text = page.locator("#details").inner_text()
    if not unblocked:
        assert "No direct prerequisites." not in details_text
    state_message = page.locator("#details .direct-prerequisite-state")
    assert state_message.get_attribute("data-direct-prerequisite-state") == (
        "relationship_unavailable" if participation == "legacy" else state
    )
    assert state_message.inner_text() == message
    assert card.locator(".card-status").count() == int(unblocked)


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

    assert "first catalog discovery failure" in page.locator("#details").inner_text()
    catalog_diagnostics = page.locator("#details .catalog-diagnostics")
    assert catalog_diagnostics.locator("li").inner_text() == (
        "discovery_unavailable first catalog discovery failure"
    )
    assert "catalog diagnostics available" in page.locator("#status").inner_text().lower()
    assert "select a package" not in page.locator("#status").inner_text().lower()

    page.locator(f'.card[data-package-id="{GATE}"]').click()
    page.fill("#filter", "not a package")
    assert page.locator("#board .card:visible").count() == 0
    assert catalog_diagnostics.locator("li").inner_text() == (
        "discovery_unavailable first catalog discovery failure"
    )
    page.fill("#filter", "")
    page.click("#refresh")
    page.locator("#details h2").get_by_text("No packages available", exact=True).wait_for(
        timeout=15000
    )
    assert page.locator("#details .catalog-diagnostics li").inner_text() == (
        "discovery_unavailable refreshed catalog discovery failure"
    )
    assert page.locator(".card.selected").count() == 0
    assert "select a package" not in page.locator("#status").inner_text().lower()


def test_catalog_diagnostics_are_visible_when_discovery_returns_no_packages(open_page):
    value = json.loads(board_payload())
    value["entries"] = []
    discovery_message = '<img src=x onerror="alert(1)"> all package discovery failed'
    value["discovery_diagnostics"] = [
        {"code": "discovery_unavailable", "message": discovery_message}
    ]
    _reseal(value)
    page = open_page(StaticClient(value))

    assert page.locator("#board .card").count() == 0
    assert "Empty folders are hidden" in page.locator("#board .board-empty").inner_text()
    assert discovery_message in page.locator("#details").inner_text()
    assert page.locator("#details h2").inner_text() == "No packages available"
    assert page.locator("#details .catalog-diagnostics li").inner_text() == (
        f"discovery_unavailable {discovery_message}"
    )
    assert page.locator("#details .catalog-diagnostics img").count() == 0
    assert "catalog diagnostics available" in page.locator("#status").inner_text().lower()
    assert "select a package" not in page.locator("#status").inner_text().lower()


@pytest.mark.parametrize("participation,state,edge_states,unblocked", [
    ("available", "no_declared_prerequisites", [], True),
    ("available", "satisfied", ["satisfied", "satisfied"], True),
    ("available", "unsatisfied", ["satisfied", "unsatisfied"], False),
    ("available", "unknown", ["satisfied", "unknown"], False),
    ("available", "unknown", [], False),
    ("legacy", "relationship_unavailable", [], False),
    ("invalid", "relationship_unavailable", [], False),
    ("available", "satisfied", ["unknown"], False),
    ("available", "no_declared_prerequisites", ["unknown"], False),
])
def test_unblocked_cards_require_confirmed_clear_prerequisites(
    open_page, participation, state, edge_states, unblocked
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
    assert card.locator(".card-status").all_text_contents() == (["Unblocked"] if unblocked else [])
    assert card.evaluate("el => getComputedStyle(el).backgroundColor") == (
        "rgb(20, 61, 43)" if unblocked else "rgb(27, 37, 51)"
    )
    card.click()
    assert "selected" in card.get_attribute("class").split()
    assert card.evaluate("el => getComputedStyle(el).backgroundColor") == (
        "rgb(20, 61, 43)" if unblocked else "rgb(27, 37, 51)"
    )
    direct_state = page.locator("#details .direct-prerequisite-state").inner_text()
    if state == "no_declared_prerequisites" and not edge_states:
        assert direct_state == "No direct prerequisites."
    else:
        assert direct_state != "No direct prerequisites."
    if "unknown" in edge_states:
        assert direct_state != "All reported direct prerequisite claims are satisfied."


def test_refresh_removes_unblocked_indicator_when_a_prerequisite_becomes_unknown(open_page):
    first = json.loads(board_payload())
    second = json.loads(board_payload())
    second["entries"][1]["relationship"]["direct_prerequisite_state"] = "unknown"
    _reseal(second)
    page = open_page(SequenceClient([first, second]))
    card = page.locator(f'[data-package-id="{STEP_ONE}"]')
    assert card.locator(".card-status").inner_text() == "Unblocked"
    page.click("#refresh")
    playwright.expect(card.locator(".card-status")).to_have_count(0)
    assert card.evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(27, 37, 51)"


def connection_pairs(page):
    return {
        tuple(pair) for pair in page.locator(".connection").evaluate_all(
            "groups => groups.map(g => [g.dataset.source, g.dataset.dependent])"
        )
    }


def assert_readable_arrows(page):
    results = page.locator(".rail").evaluate_all("""paths => paths.map(path => {
      const matrix = path.getScreenCTM();
      const at = length => path.getPointAtLength(length).matrixTransform(matrix);
      const length = path.getTotalLength();
      const start = at(0), end = at(length), beforeEnd = at(length - 1);
      const cards = [...document.querySelectorAll('.card:not([hidden])')];
      const source = cards.find(c => c.dataset.packageId === path.parentNode.dataset.source)
        .getBoundingClientRect();
      const dependent = cards.find(c => c.dataset.packageId === path.parentNode.dataset.dependent)
        .getBoundingClientRect();
      const obstacles = [...cards, ...document.querySelectorAll('.row-head, .column-head')]
        .map(c => c.getBoundingClientRect());
      let intersects = false;
      for (let distance = 0; distance <= length; distance += 2) {
        const point = at(distance);
        if (obstacles.some(r => point.x > r.left && point.x < r.right &&
            point.y > r.top && point.y < r.bottom)) intersects = true;
      }
      const markerId = path.getAttribute('marker-end').slice(5, -1);
      const marker = document.getElementById(markerId);
      return {
        leavesSource: Math.abs(start.x - source.left) < 2 &&
          start.y > source.top && start.y < source.bottom,
        entersDependent: end.x < dependent.left && dependent.left - end.x <= 8 &&
          end.y > dependent.top && end.y < dependent.bottom && beforeEnd.x < end.x,
        intersects,
        arrowShape: marker.querySelector('path').getAttribute('d'),
        arrowTipAtEnd: marker.refX.baseVal.value === marker.viewBox.baseVal.width,
        arrowWidth: marker.markerWidth.baseVal.value,
        strokeWidth: parseFloat(getComputedStyle(path).strokeWidth),
      };
    })""")
    for result in results:
        assert result["leavesSource"]
        assert result["entersDependent"]
        assert not result["intersects"]
        assert result["arrowShape"] == "M0 0L10 5L0 10Z"
        assert result["arrowTipAtEnd"]
        assert result["arrowWidth"] >= 10
        assert result["strokeWidth"] >= 2.5


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
    assert page.locator(f'.card[data-package-id="{STEP_ONE}"]').evaluate(
        "c => getComputedStyle(c).backgroundColor"
    ) == "rgb(20, 61, 43)"
    page.locator(f'.card[data-package-id="{STEP_ONE}"]').click()
    assert page.locator(".connection.emphasized, .connection.muted").count() == 0
    assert connection_pairs(page) == {(STEP_ONE, STEP_TWO), (STEP_TWO, GATE), (STEP_ONE, LOOSE)}


def routing_payload():
    value = json.loads(board_payload())
    far = "123e4567-e89b-42d3-a456-426614174004"
    unknown = "123e4567-e89b-42d3-a456-426614174005"
    value["entries"].extend([
        _entry(far, "Gamma/Done/far", "done", "Far prerequisite", "Gamma",
               prerequisites=[_edge(STEP_ONE, "forward")]),
        _entry(unknown, "Other/Queue/unknown", "queue", "Unknown project", "Other",
               prerequisites=[_edge(far, "input")]),
    ])
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


@pytest.mark.parametrize("theme,page_color,card_color,green_color", [
    ("dark", "rgb(17, 24, 33)", "rgb(27, 37, 51)", "rgb(20, 61, 43)"),
    ("light", "rgb(244, 246, 248)", "rgb(255, 255, 255)", "rgb(220, 252, 231)"),
])
def test_native_themes_preserve_readable_cards_arrows_and_selection(
    open_page, theme, page_color, card_color, green_color
):
    page = open_page(StaticClient(board_payload()))
    page.get_by_label("Theme", exact=True).select_option(theme)
    source = page.locator(f'.card[data-package-id="{STEP_ONE}"]')
    neutral = page.locator(f'.card[data-package-id="{STEP_TWO}"]')
    assert source.evaluate("c => getComputedStyle(c).backgroundColor") == green_color
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
        card.querySelectorAll('.card-title, .card-project, .card-status, .link, .link em')
          .forEach(el => pairs.push([color(el), background(card)]));
      });
      document.querySelectorAll('input, select, #refresh, .details').forEach(el =>
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
    assert page.locator("#details h2").inner_text() == "Dependent step"
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
    _reseal(value)
    page = open_page(StaticClient(value))
    assert page.locator('.board-row[data-lifecycle="queue"] .cell').first.locator(
        '.card-title'
    ).all_text_contents() == ["A dependent", "Z candidate"]
    assert connection_pairs(page) == set()


def test_successful_changed_poll_clears_previous_error_without_applying_update(open_page):
    client = SequenceClient([
        board_payload(), CatalogError("producer_timeout"),
        board_payload(titles={"step_one": "Recovered foundation"}),
    ])
    page = open_page(client)
    page.get_by_text("Update check failed: producer_timeout", exact=True).wait_for(timeout=15000)
    page.wait_for_selector("#refresh.pending", timeout=15000)
    assert page.locator("#status").inner_text() == (
        "Loaded 4 packages · select a package to view diagnostics"
    )
    assert page.locator(".card-title").get_by_text("Foundation step", exact=True).count() == 1
    assert page.locator(".card-title").get_by_text("Recovered foundation", exact=True).count() == 0
    page.click("#refresh")
    assert page.locator(".card-title").get_by_text("Recovered foundation", exact=True).count() == 1
    assert client.calls == 3


def test_show_all_snapshot_renders_all_seven_rows_and_hidden_done_keeps_direct_context(
    open_page,
):
    page = open_page(StaticClient(lifecycle_payload()))
    ordered_rows = sorted(STAGE_ROWS)
    assert page.locator(".row-head").all_text_contents() == [label for _, _, label, _ in ordered_rows]
    assert page.locator("#board .card").count() == 7
    assert page.locator(".board-row").evaluate_all(
        "rows => rows.map(row => [row.dataset.lifecycle, row.querySelector('.card-title')?.textContent])"
    ) == [[lifecycle, title] for _, lifecycle, _, title in ordered_rows]

    hidden = json.loads(lifecycle_payload(hidden_stages=("Done",)))
    queue_path = "Fictional/Queue/package"
    done_path = "Fictional/Done/package"
    hidden_page = open_page(StaticClient(hidden))
    queue = hidden_page.locator(f'.card[data-package-path="{queue_path}"]')
    queue.click()

    assert hidden_page.locator('.board-row[data-lifecycle="done"]').count() == 0
    assert hidden_page.locator(f'.card[data-package-path="{done_path}"]').count() == 0
    hidden_page.fill("#filter", "Done target")
    assert hidden_page.locator("#board .card:visible").count() == 0
    hidden_page.fill("#filter", "")
    assert queue.locator(".card-links").count() == 0
    assert hidden_page.locator(
        f'.connection[data-source="{STAGE_IDS["Done"]}"]'
    ).count() == 0
    assert hidden_page.locator("#details .prerequisite-target").inner_text() == "Done target"


def test_compact_view_uses_inventory_axes_and_preserves_hidden_context(open_page):
    page = open_page(StaticClient(compact_payload()))
    compact = page.get_by_label("Hide empty rows and columns", exact=True)
    assert compact.is_checked()
    assert page.locator(".column-head").all_text_contents() == ["Alpha"]
    assert page.locator(".row-head").all_text_contents() == ["Partial", "Queue", "Testing"]
    assert page.locator('.card[data-package-path="Alpha/Testing/custom"]').count() == 1
    assert page.locator('.card[data-package-path="HiddenOnly/Done/hidden"]').count() == 0
    assert "incomplete / unavailable" in page.locator(".row-head").first.inner_text()

    dependent = page.locator('.card[data-package-path="Alpha/Queue/dependent"]')
    dependent.click()
    details = page.locator("#details")
    assert details.locator(".prerequisite-target").all_text_contents() == [
        "Hidden prerequisite", "Custom stage card"
    ]
    assert "Partial stage scan incomplete" in details.inner_text()
    assert page.locator('.card[data-package-path="HiddenOnly/Done/hidden"]').count() == 0

    compact.uncheck()
    assert not compact.is_checked()
    assert page.locator(".column-head").all_text_contents() == ["Alpha", "EmptyProject"]
    assert page.locator(".row-head").all_text_contents() == [
        "Partial", "Queue", "Testing", "Empty"
    ]
    assert page.locator('.board-row[data-lifecycle="Empty"] .empty').inner_text() == "—"
    assert page.locator('.card[data-package-path="HiddenOnly/Done/hidden"]').count() == 0


def test_no_eligible_stage_state_keeps_projects_when_compaction_is_disabled(open_page):
    value = json.loads(compact_payload())
    hidden = sorted(stage["stage"] for stage in value["inventory"]["stages"])
    value["visibility"]["hidden_stages"] = hidden
    for entry in value["entries"]:
        entry["board_visible"] = False
    _reseal(value)
    page = open_page(StaticClient(value))
    assert page.get_by_text("Empty folders are hidden", exact=False).count() == 1
    compact = page.get_by_label("Hide empty rows and columns", exact=True)
    compact.uncheck()
    assert page.locator(".column-head").all_text_contents() == [
        "Alpha", "EmptyProject", "HiddenOnly"
    ]
    assert page.get_by_text("No eligible stage directories were found.", exact=True).count() == 1


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
        assert page.locator(".column-head").all_text_contents() == ["Alpha", "EmptyProject"]
    else:
        assert compact.is_checked()
    compact.uncheck()
    assert not compact.is_checked()
    page.reload()
    assert not compact.is_checked() if "Blocked" not in storage_setup else compact.is_checked()


def test_search_resize_and_pending_apply_keep_axes_and_rails_valid(open_page):
    first = compact_payload()
    second = compact_payload(extra_stage=True)
    page = open_page(SequenceClient([first, second]))
    assert page.locator(".row-head").all_text_contents() == ["Partial", "Queue", "Testing"]
    assert connection_pairs(page) == {(COMPACT_CARD, COMPACT_DEPENDENT)}
    page.fill("#filter", "custom")
    assert page.locator(".row-head").all_text_contents() == ["Partial", "Queue", "Testing"]
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


def test_pending_done_hidden_snapshot_clears_selection_only_after_apply(open_page):
    first = lifecycle_payload()
    second = lifecycle_payload(hidden_stages=("Done",))
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
    assert page.locator('.board-row[data-lifecycle="done"]').count() == 0
    assert page.locator('.card[data-package-path="Fictional/Done/package"]').count() == 0
    assert page.locator("#details h2").inner_text() == "Select a package"


def test_real_producer_scalar_policy_paths_and_diagnostics_are_displayed(open_page):
    payload, value = unicode_producer_payload()
    assert value["visibility"]["hidden_stages"] == [UNICODE_LOW, UNICODE_HIGH]
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
        assert f"invalid package identity: {path}" in page.locator("#details").inner_text()
        assert f"Diagnostic {marker}" in page.locator("#details h2").inner_text()


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
        page.get_by_text("Update check failed: producer_protocol_error", exact=True).wait_for(
            timeout=15000
        )

    assert sorted(page.locator("#board .card").evaluate_all(
        "cards => cards.map(card => card.dataset.packagePath)"
    )) == expected_paths
    assert page.locator("#refresh").inner_text() == "Refresh view"


def test_digest_consistent_duplicate_policy_keeps_last_valid_board(open_page):
    valid = json.loads(lifecycle_payload())
    duplicate = json.loads(lifecycle_payload(hidden_stages=("Done",)))
    duplicate["visibility"]["hidden_stages"] = ["Done", "Done"]
    _reseal(duplicate)
    assert duplicate["catalog_digest"] == canonical_digest(duplicate)

    from nyx import server as server_module

    def passthrough_catalog(candidate):
        return candidate if isinstance(candidate, RawCatalog) else parse_catalog(candidate)

    client = RawSequenceClient([valid, duplicate])
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(server_module, "parse_catalog", passthrough_catalog)
        page = open_page(client)
        page.get_by_text("Update check failed: producer_protocol_error", exact=True).wait_for(
            timeout=15000
        )

    assert page.locator("#board .card").count() == 7
    assert page.locator('.card[data-package-path="Fictional/Done/package"]').count() == 1
    assert page.locator("#refresh").inner_text() == "Refresh view"
    assert client.calls >= 2


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
        page.get_by_text("Update check failed: producer_protocol_error", exact=True).wait_for(
            timeout=15000
        )

    assert page.locator("#board .card").count() == 7
    assert page.locator("#refresh").inner_text() == "Refresh view"


def _malformed_browser_inventory(case):
    value = json.loads(lifecycle_payload())
    if case == "schema-3":
        value["schema_version"] = 3
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
        page.get_by_text("Update check failed: producer_protocol_error", exact=True).wait_for(
            timeout=15000
        )

    assert page.locator("#board .card").count() == 7
    assert page.locator('.card[data-package-path="Fictional/Done/package"]').count() == 1
    assert page.locator("#refresh").inner_text() == "Refresh view"
