"""Behavioral proof of the reuse gate's decisions, outputs, summary and failure model.

Decision cases call the gate's entry function in process with a fake client that
answers only the current run and the current workflow's runs listing.  HTTP
failure cases use the real client against a loopback server, and one case runs
the script itself as a subprocess.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import socketserver
import subprocess
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "reuse_gate.py"
_spec = importlib.util.spec_from_file_location("reuse_gate", SCRIPT)
reuse_gate = importlib.util.module_from_spec(_spec)
sys.modules["reuse_gate"] = reuse_gate
_spec.loader.exec_module(reuse_gate)

REPOSITORY = "example-org/example-repo"
WORKFLOW_ID = 4242
CURRENT_ID = 9000
PUSHED_SHA = "f" * 40
TREE = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
OTHER_TREE = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345679"
RUN_PATH = f"/repos/{REPOSITORY}/actions/runs/{CURRENT_ID}"


def listing_path(page: int) -> str:
    return (
        f"/repos/{REPOSITORY}/actions/workflows/{WORKFLOW_ID}/runs"
        f"?status=success&per_page=100&page={page}"
    )


def current_record(**overrides):
    record = {
        "id": CURRENT_ID,
        "workflow_id": WORKFLOW_ID,
        "event": "push",
        "head_branch": "main",
        "head_sha": PUSHED_SHA,
        "head_commit": {"tree_id": TREE},
        "status": "in_progress",
        "conclusion": None,
        "run_attempt": 1,
    }
    record.update(overrides)
    return record


def run(run_id, event="push", branch="feature/x", tree=TREE, **overrides):
    record = {
        "id": run_id,
        "event": event,
        "head_branch": branch,
        "head_sha": f"{run_id:040d}"[-40:],
        "head_commit": {"tree_id": tree},
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
    }
    record.update(overrides)
    return record


def filler(count, start=100):
    return [run(start + index, tree=OTHER_TREE) for index in range(count)]


class FakeClient:
    """Answers only the two permitted paths and fails the test on any other request."""

    def __init__(self, current, pages):
        self.answers = {RUN_PATH: current}
        for number, page in enumerate(pages, start=1):
            self.answers[listing_path(number)] = {"workflow_runs": page}
        self.requests = []

    def __call__(self, path):
        self.requests.append(path)
        if path not in self.answers:
            pytest.fail(f"unexpected request {path}")
        return self.answers[path]


def environment(tmp_path, api_url="http://unused.invalid"):
    summary = tmp_path / "summary.md"
    outputs = tmp_path / "outputs.txt"
    return {
        "GITHUB_API_URL": api_url,
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_RUN_ID": str(CURRENT_ID),
        "GITHUB_SHA": PUSHED_SHA,
        "GITHUB_OUTPUT": str(outputs),
        "GITHUB_STEP_SUMMARY": str(summary),
        "GITHUB_TOKEN": "fictional-token",
    }


class Result:
    def __init__(self, env, code, stdout, requests=()):
        self.code = code
        self.stdout = stdout
        self.requests = list(requests)
        outputs = Path(env["GITHUB_OUTPUT"])
        summary = Path(env["GITHUB_STEP_SUMMARY"])
        self.output_lines = outputs.read_text().splitlines() if outputs.is_file() else []
        self.summary = summary.read_text() if summary.is_file() else ""

    @property
    def values(self):
        return dict(line.split("=") for line in self.output_lines)


def gate(tmp_path, pages, current=None):
    env = environment(tmp_path)
    client = FakeClient(current if current is not None else current_record(), pages)
    out = io.StringIO()
    code = reuse_gate.main(env, client, out)
    return Result(env, code, out.getvalue(), client.requests)


def outcome(result):
    return result.values["reuse_source"], result.values["reuse_desktop"]


# AC-01
def test_same_tree_branch_push_reuses_package_group_and_one_character_tree_change_does_not(tmp_path):
    matched = gate(_sub(tmp_path, "a"), [[run(1)]])
    assert outcome(matched) == ("true", "false")
    changed = gate(_sub(tmp_path, "b"), [[run(1, tree=OTHER_TREE)]])
    assert outcome(changed) == ("false", "false")
    assert TREE != OTHER_TREE and sum(a != b for a, b in zip(TREE, OTHER_TREE)) == 1


def _sub(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    return path


def test_tag_push_qualifies_like_a_branch_push(tmp_path):
    assert outcome(gate(tmp_path, [[run(1, branch="v1.2.3")]])) == ("true", "false")


# AC-02 and R-03
@pytest.mark.parametrize(
    ("candidates", "expected"),
    [
        ([run(1, event="workflow_dispatch", branch="main")], ("false", "true")),
        ([run(1, event="workflow_dispatch", branch="feature/x")], ("false", "true")),
        ([run(1, event="push", branch="feature/x")], ("true", "false")),
        ([run(1, event="push", branch="v1.0")], ("true", "false")),
        ([run(1, event="push", branch="main")], ("false", "false")),
        (
            [run(2, event="push", branch="feature/x"), run(1, event="workflow_dispatch")],
            ("true", "true"),
        ),
        ([run(1, event="schedule", branch="feature/x")], ("false", "false")),
    ],
)
def test_groups_are_decided_independently_by_event_and_ref(tmp_path, candidates, expected):
    assert outcome(gate(tmp_path, [candidates])) == expected


# AC-03
@pytest.mark.parametrize(
    "overrides",
    [
        {"conclusion": "skipped"},
        {"conclusion": "failure"},
        {"conclusion": "cancelled"},
        {"conclusion": "neutral"},
        {"status": "in_progress", "conclusion": None},
        {"status": "queued", "conclusion": None},
        {"id": CURRENT_ID},
        {"run_attempt": 2, "conclusion": "failure"},
    ],
)
@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_disqualified_same_tree_candidate_is_refused_for_both_groups(tmp_path, overrides, event):
    result = gate(tmp_path, [[run(1, event=event, **overrides)]])
    assert outcome(result) == ("false", "false")


def test_rerun_attempt_with_success_is_accepted(tmp_path):
    assert outcome(gate(tmp_path, [[run(1, run_attempt=2)]])) == ("true", "false")


@pytest.mark.parametrize("branch", ["main", None, "", "missing"])
def test_push_without_a_non_main_ref_is_not_package_evidence(tmp_path, branch):
    candidate = run(1, branch=branch)
    if branch == "missing":
        del candidate["head_branch"]
    assert outcome(gate(tmp_path, [[candidate]])) == ("false", "false")


def test_only_the_current_workflows_runs_are_requested(tmp_path):
    result = gate(tmp_path, [[run(1)]])
    assert result.requests == [RUN_PATH, listing_path(1)]


# AC-04
def test_scan_continues_past_same_tree_runs_that_do_not_qualify(tmp_path):
    newer = [
        run(5, event="push", branch="main"),
        run(4, event="workflow_dispatch", branch="main"),
    ]
    older = [run(3, event="push", branch="feature/x")]
    result = gate(tmp_path, [newer + older])
    assert outcome(result) == ("true", "true")
    assert "push run 3," in result.summary and "workflow_dispatch run 4," in result.summary


def test_newer_push_at_the_tree_does_not_end_the_desktop_search(tmp_path):
    candidates = [run(3, event="push"), run(2, event="push"), run(1, event="workflow_dispatch")]
    result = gate(tmp_path, [candidates])
    assert outcome(result) == ("true", "true")
    assert "push run 3," in result.summary and "workflow_dispatch run 1," in result.summary


def test_qualifying_run_on_second_page_is_found_and_no_third_page_is_requested(tmp_path):
    result = gate(tmp_path, [filler(100), [run(1)]])
    assert outcome(result) == ("true", "false")
    assert result.requests == [RUN_PATH, listing_path(1), listing_path(2)]


def test_two_full_pages_without_a_match_never_request_a_third_page(tmp_path):
    result = gate(tmp_path, [filler(100), filler(100, start=300)])
    assert outcome(result) == ("false", "false")
    assert result.requests == [RUN_PATH, listing_path(1), listing_path(2)]


def test_short_page_ends_the_listing(tmp_path):
    result = gate(tmp_path, [filler(99)])
    assert outcome(result) == ("false", "false")
    assert result.requests == [RUN_PATH, listing_path(1)]


def test_no_further_page_is_requested_once_both_groups_matched(tmp_path):
    page = [run(2, event="push")] + [run(1, event="workflow_dispatch")] + filler(98)
    result = gate(tmp_path, [page])
    assert outcome(result) == ("true", "true")
    assert result.requests == [RUN_PATH, listing_path(1)]


def test_newest_qualifying_run_per_group_is_recorded(tmp_path):
    result = gate(tmp_path, [[run(7), run(6), run(5, event="workflow_dispatch"), run(4, event="workflow_dispatch")]])
    assert "push run 7," in result.summary and "push run 6," not in result.summary
    assert "workflow_dispatch run 5," in result.summary
    assert "workflow_dispatch run 4," not in result.summary


# AC-05 (in-process shapes)
def test_current_run_without_a_tree_fails_open(tmp_path):
    record = current_record()
    del record["head_commit"]
    result = gate(tmp_path, [[run(1)]], current=record)
    assert outcome(result) == ("false", "false")
    assert result.code == 0
    assert result.stdout.startswith("::warning::") and "tree_id" in result.summary


@pytest.mark.parametrize("listing", [{"message": "oops"}, {"workflow_runs": "x"}, []])
def test_listing_without_a_runs_list_fails_open(tmp_path, listing):
    env = environment(tmp_path)
    client = FakeClient(current_record(), [])
    client.answers[listing_path(1)] = listing
    out = io.StringIO()
    assert reuse_gate.main(env, client, out) == 0
    result = Result(env, 0, out.getvalue())
    assert outcome(result) == ("false", "false")
    assert "::warning::" in result.stdout and "workflow_runs" in result.summary


def test_candidate_with_unreadable_tree_fails_open_even_after_a_match(tmp_path):
    broken = run(1)
    del broken["head_commit"]
    result = gate(tmp_path, [[run(3), broken]])
    assert outcome(result) == ("false", "false")
    assert result.code == 0 and "::warning::" in result.stdout


def test_exhausting_pages_keeps_a_group_that_matched(tmp_path):
    page_one = [run(2, event="push")] + filler(99)
    result = gate(tmp_path, [page_one, filler(100, start=300)])
    assert outcome(result) == ("true", "false")
    assert "::warning::" not in result.stdout
    assert "desktop lanes run" in result.summary


# AC-05 (real client against a loopback server)
class Loopback:
    def __init__(self, routes):
        self.routes = routes
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.requests.append(self.path)
                status, body = outer.routes.get(self.path, (404, b"{}"))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def payload(value):
    return 200, json.dumps(value).encode()


def good_routes(first_page=None):
    return {
        RUN_PATH: payload(current_record()),
        listing_path(1): payload({"workflow_runs": first_page if first_page is not None else [run(1)]}),
    }


def http_gate(tmp_path, routes):
    out = io.StringIO()
    with Loopback(routes) as server:
        env = environment(tmp_path, server.url)
        code = reuse_gate.main(env, None, out)
    return Result(env, code, out.getvalue())


@pytest.mark.parametrize(
    ("override_path", "response", "reason"),
    [
        (RUN_PATH, (500, b"{}"), "HTTP 500"),
        (RUN_PATH, (404, b"{}"), "HTTP 404"),
        (listing_path(1), (403, b'{"message":"rate limit"}'), "HTTP 403"),
        (listing_path(1), (200, b"<html>not json</html>"), "JSONDecodeError"),
    ],
)
def test_http_failures_fail_open_with_a_warning(tmp_path, override_path, response, reason):
    routes = good_routes()
    routes[override_path] = response
    result = http_gate(tmp_path, routes)
    assert result.code == 0
    assert outcome(result) == ("false", "false")
    assert result.stdout.startswith("::warning::")
    assert reason in result.stdout and reason in result.summary


class _RawHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.recv(65536)
        self.request.sendall(self.server.raw_response)


@pytest.mark.parametrize(
    ("raw_response", "reason"),
    [
        (b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n12345", "IncompleteRead"),
        (b"not an http status line\r\n\r\n", "BadStatusLine"),
    ],
)
def test_truncated_body_and_malformed_status_line_fail_open(tmp_path, raw_response, reason):
    server = socketserver.TCPServer(("127.0.0.1", 0), _RawHandler)
    server.raw_response = raw_response
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        env = environment(tmp_path, f"http://127.0.0.1:{server.server_address[1]}")
        out = io.StringIO()
        code = reuse_gate.main(env, None, out)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    result = Result(env, code, out.getvalue())
    assert code == 0
    assert outcome(result) == ("false", "false")
    assert result.stdout.startswith("::warning::")
    assert reason in result.stdout and reason in result.summary


def test_http_failure_after_a_match_still_writes_false_for_both_groups(tmp_path):
    routes = good_routes(first_page=[run(2, event="push")] + filler(99))
    routes[listing_path(2)] = (503, b"{}")
    result = http_gate(tmp_path, routes)
    assert result.code == 0 and outcome(result) == ("false", "false")
    assert "::warning::" in result.stdout


def test_unreachable_host_fails_open(tmp_path):
    with Loopback({}) as server:
        url = server.url
    env = environment(tmp_path, url)
    out = io.StringIO()
    assert reuse_gate.main(env, None, out) == 0
    result = Result(env, 0, out.getvalue())
    assert outcome(result) == ("false", "false") and "::warning::" in result.stdout


def test_request_timeout_fails_open(tmp_path, monkeypatch):
    import socket

    def timeout(*args, **kwargs):
        raise socket.timeout("timed out")

    monkeypatch.setattr(reuse_gate.urllib.request, "urlopen", timeout)
    env = environment(tmp_path)
    out = io.StringIO()
    assert reuse_gate.main(env, None, out) == 0
    result = Result(env, 0, out.getvalue())
    assert outcome(result) == ("false", "false") and "timed out" in result.summary


def test_real_client_applies_the_request_timeout(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["timeout"] = timeout
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(reuse_gate.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(reuse_gate.ExpectedFailure):
        reuse_gate.make_client("http://127.0.0.1:1", None)("/x")
    assert seen["timeout"] == 15


# AC-06
def test_summary_records_reused_evidence_and_the_group_that_runs(tmp_path):
    result = gate(tmp_path, [[run(77, head_sha="c" * 40)]])
    assert "package and suite lanes reused" in result.summary
    for fact in ("push run 77,", "SHA " + "c" * 40, f"tree {TREE}", f"pushed SHA {PUSHED_SHA}"):
        assert fact in result.summary
    assert "did not run them" in result.summary
    assert "desktop lanes run: no completed successful workflow_dispatch run" in result.summary


def test_summary_records_both_reused_groups_and_none(tmp_path):
    both = gate(
        _sub(tmp_path, "both"),
        [[run(2, head_sha="1" * 40), run(1, event="workflow_dispatch", head_sha="2" * 40)]],
    )
    assert "package and suite lanes reused" in both.summary
    assert "desktop lanes reused" in both.summary and "2" * 40 in both.summary
    none = gate(_sub(tmp_path, "none"), [[]])
    assert "reused" not in none.summary
    assert "package and suite lanes run:" in none.summary and "desktop lanes run:" in none.summary


# AC-07
def test_outputs_file_gets_exactly_the_two_lines_once(tmp_path):
    result = gate(tmp_path, [[run(1)]])
    assert result.output_lines == ["reuse_source=true", "reuse_desktop=false"]


def test_summary_write_failure_exits_non_zero_and_leaves_outputs_unchanged(tmp_path):
    env = environment(tmp_path)
    Path(env["GITHUB_OUTPUT"]).write_text("existing=1\n")
    Path(env["GITHUB_STEP_SUMMARY"]).mkdir()
    with pytest.raises(OSError):
        reuse_gate.main(env, FakeClient(current_record(), [[run(1)]]), io.StringIO())
    assert Path(env["GITHUB_OUTPUT"]).read_text() == "existing=1\n"


def test_unexpected_exception_exits_non_zero_without_outputs(tmp_path):
    def broken(path):
        raise RuntimeError("bug")

    env = environment(tmp_path)
    with pytest.raises(RuntimeError):
        reuse_gate.main(env, broken, io.StringIO())
    assert not Path(env["GITHUB_OUTPUT"]).exists()


# Entry wiring
def test_script_subprocess_positive_match_writes_outputs_and_summary(tmp_path):
    routes = good_routes(
        first_page=[run(2, event="push", head_sha="d" * 40), run(1, event="workflow_dispatch")]
    )
    with Loopback(routes) as server:
        env = environment(tmp_path, server.url)
        completed = subprocess.run(
            [sys.executable, str(SCRIPT)],
            env={**env, "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
            capture_output=True,
            text=True,
            timeout=60,
        )
        requests = list(server.requests)
    assert completed.returncode == 0, completed.stderr
    result = Result(env, completed.returncode, completed.stdout)
    assert result.output_lines == ["reuse_source=true", "reuse_desktop=true"]
    assert "push run 2," in result.summary and "d" * 40 in result.summary
    assert requests == [RUN_PATH, listing_path(1)]
