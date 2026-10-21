"""Exercise the published installation commands in their native shell.

Linux keeps the documented console installation and its browser/service
lifecycle.  Windows and macOS publish the private desktop artifact instead, so
the same consumer parses that guidance and, in the desktop artifact lane,
exercises the documented application actions against the staged build rather
than a detached console daemon.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

import pytest
from test_desktop_artifact import (
    ArtifactLayout,
    _assert_claims_released,
    _claim_paths,
    _expected_catalog,
    _inert_path,
    _request,
    _running_gui,
    _sanitized_environment,
    _wait_for_board,
    _wait_for_window,
    _wait_port_free,
    _workspace,
    _write_configuration,
)
from test_wheel_install import (
    _installed_environment,
    _native_claim_probe,
    _venv_executable,
)

from nyx import catalog
from nyx._native_claim import NativeClaim

REPOSITORY_ROOT = Path(__file__).parents[1].resolve()
DOCUMENTS = ("README.md", "docs/running-nyx.md")
EXPECTED = {
    "bash": (
        "python3.12 -m venv .venv",
        ".venv/bin/python -m pip install .",
        ".venv/bin/nyx --setup examples/sample-specifications",
        ".venv/bin/nyx",
        ".venv/bin/nyx --status",
        ".venv/bin/nyx --stop",
    ),
}
BUILD_HEADING = "### Build the private artifact"
# The desktop build is bounded for a native compile, not for one command.  Each
# bound stays below its desktop job's time limit so a hung build fails here with
# its captured output instead of being cut off by the job limit.
_BUILD_TIMEOUT_MINUTES = {"powershell": 20, "bash": 10}
# The documented private artifacts and the application actions the guide
# promises.  The desktop lane consumes these paths against the staged build.
DESKTOP_ARTIFACT_PATHS = {
    "windows": r"dist\desktop\Nyx\Nyx.exe",
    "macos": "dist/desktop/Nyx.app",
}
DESKTOP_ARTIFACT_MARKERS = (
    r"dist\desktop\Nyx\Nyx.exe",
    "dist/desktop/Nyx.app",
    "build_desktop.py",
)
DESKTOP_GUIDANCE_MARKERS = (
    "private internal feasibility build",
    "not a public release",
    "workspace chooser",
    "Change workspace",
    "Quit",
    "already open",
    "Retry",
    "**Cancel** is available only while changing the workspace",
    "returns to the current workspace without quitting",
    "**Close** finishes a blocked close or a board-display failure",
)
STAGE_ORDER_GUIDANCE_MARKERS = {
    "README.md": (
        "The **Board settings** panel saves the board row order in the same "
        "account-local configuration",
        "account-local",
        "browser and desktop views",
        "workspace root",
        "presentation only",
        "does not rename or move",
    ),
    "docs/running-nyx.md": (
        "the toolbar includes a collapsed **Board settings** panel with a "
        "row-order editor",
        "Move up",
        "Move down",
        "Cancel",
        "performs no write",
        "Reset",
        "canonical inventory order",
        "absent",
        "survives browser reload",
        "supported Nyx",
        "failed Save",
    ),
    "docs/workspaces.md": (
        "root-keyed order map",
        "browser and desktop views",
        "Cancel",
        "performs no write",
        "Reset",
        "Unlisted eligible stages",
        "absent",
        "If loading account settings fails, no saved order is available, "
        "so the board uses canonical inventory order",
        "reports the problem for retry",
        "If Save fails, the board keeps displaying the prior saved order "
        "and retains the unsaved editor draft",
        "never renames or moves",
    ),
}
COMPLETION_GUIDANCE_MARKERS = {
    "README.md": (
        "Completion Prerequisite: UUID",
        "literal set of stage names",
        "Board settings",
        "workspace root",
        "evidence verification",
        "Prerequisite: UUID | claim-name",
    ),
    "docs/running-nyx.md": (
        "collapsed **Board settings**",
        "Counts as finished",
        "**Save** to persist the row order and completion policy together",
        "**Cancel** drops both kinds of unsaved change",
        "**Reset** clears the current root's row order and completed-stage set",
        "Hide terminal rows",
        "Reload board settings",
        "configuration revision",
        "moving it out reopens it",
        "recorded workflow assertion",
        "A theme change follows other open tabs",
        "The compact view (**Hide empty rows and columns**), **Hide terminal rows**, and "
        "**Click empty space to deselect** apply per tab until reload",
        '"Board settings changed elsewhere. Reload board settings to replace this unsaved draft."',
        "Choose **Reload board settings** to replace the draft",
    ),
    "docs/workspaces.md": (
        "no implicit `Done` or `Archive` rule",
        "Completion Prerequisite: UUID",
        "missing policy leaves the requirement **unknown**",
        "saved per canonical workspace root",
        "stay keyed to the workspace's original path",
        "does not carry them to the new path and does not delete them",
        "Moving the folder back to its original path restores its order and completion policy",
        "root with no saved names reports unknown",
        "A cancelled or archived item",
        "finished row",
        "mixed dependent item",
    ),
    "docs/specification-reference.md": (
        "one exact completion grammar",
        "one-part `Prerequisite: UUID` is not",
        "never uses a magic claim name",
        "collapsed **Board settings**",
        "temporarily absent names",
        "recorded workflow assertion",
    ),
}
CHECKOUT_RETIRED_WORDING = (
    "cached-only labels",
    "makes no network connection:",
    "never fetches or contacts a remote",
    "`default <name> <relation> cached <time>`",
    "`cached <time>`",
    "every verdict in it is `unknown` with the reason `no_base`. Every",
    "an unreadable clone, or one with no default branch name",
    "so the document is the same warm or cold",
    "it checks every branch it can before it looks anything up",
)
CHECKOUT_GUIDANCE_MARKERS = {
    "docs/running-nyx.md": (
        "`nyx checkouts`",
        "`--json`",
        "one line per clone and one indented line per linked worktree",
        "nyx checkouts --no-network",
        "`--no-network` skips every remote check",
        "`--no-network` is accepted only with `checkouts`",
        "`remote live`, `remote off` or `remote failed <reason>`",
        "`unreachable`, `not_found`, `authentication_failed`, `timed_out` or `failed`",
        "`branches <basis> local-only <n>`",
        "`default <name> live <relation> cached <relation> tracking live <relation> "
        "fetched <time>`",
        "`default <name> cached <relation> fetched <time>`",
        "`never fetched`",
        "`no live default`",
        "`no cached default`",
        "`no origin remote`",
        "`local-only <n>`",
        "`unknown <m>`",
        "`cached` only ever labels a basis",
        "By default `nyx checkouts` is networked",
        "it asks each distinct `origin` remote once",
        "the count may be borrowed read-only from a sibling clone",
        "Nyx's own SSH runs in batch mode",
        "unless you configured an SSH command yourself",
        "`--no-network` makes no network connection at all",
        "The command is read-only",
        "writes no alternates file and never fetches",
        "Checkout folders are set per workspace",
        "A settings editor arrives with the Checkouts view",
        "cannot be combined with `--setup`, `--stop` or `--status`",
        "exits 1 with one line only when the configuration is unavailable "
        "or the document exceeds 2 MiB",
        "`no checkout folders are configured`",
        "`checkout_cache_unreadable` and `checkout_cache_write_failed`",
        "appears only in the `--json` document: the plain lines above are unchanged",
        "`verdict_base` names the default branch the verdicts are judged against",
        '"base stale"',
        "which can be older than the remote until you fetch",
        "`ancestor`: the branch's commit is already in the base's history",
        "`patch_equivalent`: the branch has no merge commits",
        "`squash_equivalent`: the branch's whole change",
        "`tree_equal`: the branch's file contents are identical",
        "`not_merged` has the rung `none`",
        "`no_base`",
        "A clone with no `origin` remote or no default branch name has no base",
        "neither the remote's current commit for it (in the clone or borrowed from a sibling) "
        "nor its own remote-tracking copy of that branch",
        "Every verdict in a clone with no base is `unknown` with the reason `no_base`",
        "has no branches, no worktrees and no base, so it has no verdicts at all",
        "It never replaces a verdict the run decides itself",
        "a warm run differs from a cold run only where the cold run says `unknown`",
        "it works the branches with no remembered answer first, then those with the oldest "
        "remembered answer, then the newest, finishing every check of one branch (including "
        "the `tree_equal` rung and the `not_merged` decision) before it starts the next, and "
        "a remembered answer stands in only where that re-check does not finish",
        "A run that re-checks nothing leaves the file untouched",
        "a remembered answer never replaces a `tree_equal` verdict that the cold run decides",
        "which can only make matching stricter",
        "so once the attributes are gone a run that decides the branch reports its own verdict",
        "`incomplete_clone` means the clone is shallow or partial",
        "`git_too_old` means git is older than 2.39",
        "the two patch rungs, `patch_equivalent` and `squash_equivalent`, need 2.39",
        "`history_limit` means the branch or the comparison was too large",
        "`deadline` means the 60 seconds ran out first",
        "`failed` means a git call failed",
        "`~/.nyx/cache/checkouts.json`",
        "The cache is used only when `~/.nyx` already exists",
        "Deleting it only costs time",
        "Its only write is the verdict cache file, which changes no repository",
    ),
    "docs/workspaces.md": (
        '"schema_version": 4',
        '"checkout_settings"',
        '"github_lookup": false',
        "The key under `checkout_settings` is the record's own `specification_root` value",
        "Each entry in `folders` must be an absolute path, and the list must be sorted",
        "write each backslash as `\\\\` in JSON",
        "Stop Nyx first",
        "`nyx --stop` on Linux, or quit the desktop application",
        "rebuild the desktop application at this version",
        "an older application refuses a schema 4 record",
        "the configuration becomes unavailable",
        "Undoing the edit restores it",
    ),
}
PORT_CONFLICT_GUIDANCE_MARKERS = (
    "**Nyx says port 8765 is already in use:**",
    "Linux", "`ss -ltnp 'sport = :8765'`",
    "macOS", "`lsof -nP -iTCP:8765 -sTCP:LISTEN`",
    "Windows PowerShell", "`Get-NetTCPConnection -LocalPort 8765 -State Listen`",
    "`OwningProcess` is the process ID",
    "Close that program", "run `nyx` again", "or choose **Retry**",
    "Nyx never switches ports",
    "Nyx runtime could not start; retry",
    "excluded or reserved port range", "no owning program",
    "`netsh interface ipv4 show excludedportrange protocol=tcp`",
)
RECOVERY_GUIDANCE_HEADING = "### Recover an unreadable runtime record"
RECOVERY_GUIDANCE_MARKERS = (
    "unreadable or malformed and has no owning instance",
    "`Runtime: unknown`",
    "`runtime state unavailable`",
    "`nyx --stop` removes that record exactly as it removes a valid stale record",
    "prints the ordinary `stopped` result",
    "no separate recovery message",
    "With no owning instance, starting with `nyx` still refuses while the record "
    "cannot be read, and its message names the recovery step and ends "
    "`; run 'nyx --stop' to recover`",
    "`; run 'nyx --stop' to recover`",
    "Recovery requires that no instance owns the runtime",
    "a live owner still refuses both start and stop",
)
EVENT_COMPLETION_EXAMPLE = (
    "# Organize the neighborhood festival\n"
    "Package ID: 123e4567-e89b-42d3-a456-426614174100\n"
    "Status: planning\n"
    "Completion Prerequisite: 123e4567-e89b-42d3-a456-426614174102\n"
    "Prerequisite: 123e4567-e89b-42d3-a456-426614174102 | venue-confirmed\n"
    "Prerequisite: 123e4567-e89b-42d3-a456-426614174102 | permit-approved"
)
_START_TIMEOUT = 90.0


def _fenced_blocks(text: str, language: str) -> list[str]:
    return re.findall(rf"^```{language}\n(.*?)^```", text, re.MULTILINE | re.DOTALL)


def _documented_commands(document: str, language: str) -> tuple[str, ...]:
    blocks = _fenced_blocks(document, language)
    assert blocks, f"missing {language} installation block"
    commands = tuple(blocks[0].splitlines())
    assert commands == EXPECTED[language], f"changed {language} installation commands"
    return commands


def _published_build_block(document: str, language: str) -> str:
    """Return the one ``language`` block published under the build heading."""

    assert document.count(BUILD_HEADING + "\n") == 1, "missing build heading"
    kept: list[str] = []
    fenced = False
    for line in document.split(BUILD_HEADING + "\n", 1)[1].splitlines(keepends=True):
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(r"#{1,3} ", line):
            break
        kept.append(line)
    section = "".join(kept)
    blocks = _fenced_blocks(section, language)
    assert len(blocks) == 1, f"expected one {language} build block, found {len(blocks)}"
    assert blocks[0].strip(), f"empty {language} build block"
    return blocks[0]


def _lane_build_language() -> str | None:
    if os.name == "nt":
        return "powershell"
    if sys.platform == "darwin":
        return "bash"
    return None


@pytest.mark.parametrize("language", EXPECTED)
def test_both_documents_publish_exact_installation_commands(language):
    blocks = [
        _documented_commands((REPOSITORY_ROOT / name).read_text(encoding="utf-8"), language)
        for name in DOCUMENTS
    ]
    assert blocks[0] == blocks[1]


@pytest.mark.parametrize(
    ("language", "index"),
    [
        (language, index)
        for language, commands in EXPECTED.items()
        for index in range(len(commands))
    ],
)
def test_document_contract_rejects_changed_or_deleted_commands(language, index):
    commands = list(EXPECTED[language])
    for replacement in ("", "echo substituted command"):
        changed = commands.copy()
        changed[index] = replacement
        document = f"```{language}\n" + "\n".join(changed) + "\n```\n"
        with pytest.raises(AssertionError, match="changed"):
            _documented_commands(document, language)


def test_readme_and_guide_publish_the_same_powershell_build_block():
    guide = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    readme_blocks = _fenced_blocks(readme, "powershell")
    assert len(readme_blocks) == 1, "README must publish exactly one PowerShell block"
    selected = _published_build_block(guide, "powershell")
    assert readme_blocks[0] == selected
    assert "scripts" in selected and "build_desktop.py" in selected


SYNTHETIC_GUIDE = (
    "# Running Nyx\n\n"
    "## Linux\n\n```bash\nlinux-first\n```\n\n"
    "{heading}\n\n"
    "```powershell\n{powershell}\n```\n\n"
    "```bash\n{macos}\n```\n\n"
    "### Open\n\n```bash\nafter-section\n```\n"
)


def _synthetic_guide(powershell="ps-build", macos="mac-build", heading=BUILD_HEADING):
    return SYNTHETIC_GUIDE.format(heading=heading, powershell=powershell, macos=macos)


def test_build_block_is_selected_by_heading_and_language_not_position():
    guide = _synthetic_guide()
    assert _published_build_block(guide, "bash") == "mac-build\n"
    assert _published_build_block(guide, "powershell") == "ps-build\n"
    # The first and last bash blocks of the document are never the build block.
    assert _published_build_block(guide, "bash") not in ("linux-first\n", "after-section\n")


def test_changing_a_command_under_the_heading_changes_the_selected_block():
    before = _published_build_block(_synthetic_guide(), "bash")
    after = _published_build_block(_synthetic_guide(macos="mac-build\nextra"), "bash")
    assert after != before
    assert after == "mac-build\nextra\n"
    # A change outside the heading leaves the selection alone.
    other = _synthetic_guide().replace("linux-first", "linux-changed")
    assert _published_build_block(other, "bash") == before


def test_build_selection_rejects_missing_heading_or_missing_language_block():
    with pytest.raises(AssertionError, match="missing build heading"):
        _published_build_block(_synthetic_guide(heading="### Something else"), "bash")
    with pytest.raises(AssertionError, match="expected one"):
        _published_build_block(_synthetic_guide().replace("```powershell", "```text"), "powershell")


def test_build_selection_keeps_comment_lines_inside_the_selected_block():
    # A shell comment inside the block looks like a heading; it must neither end
    # the section nor drop the commands after it.
    macos = "mac-venv\n# then build\nmac-build"
    guide = _synthetic_guide(macos=macos)
    assert _published_build_block(guide, "bash") == macos + "\n"


def test_real_guide_selects_the_macos_build_block_not_the_linux_one():
    guide = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    selected = _published_build_block(guide, "bash")
    linux = "\n".join(EXPECTED["bash"]) + "\n"
    assert selected != linux
    assert "build_desktop.py" in selected
    assert "venv" in selected


def test_documents_publish_the_private_desktop_guidance():
    for name in DOCUMENTS:
        document = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
        for marker in DESKTOP_ARTIFACT_MARKERS:
            assert marker in document, (name, marker)
    guide = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    for marker in DESKTOP_GUIDANCE_MARKERS:
        assert marker in guide, marker
    for name in DOCUMENTS:
        assert "**Revalidate**" not in (REPOSITORY_ROOT / name).read_text(encoding="utf-8"), name


@pytest.mark.parametrize("name", [
    "README.md", "docs/running-nyx.md", "docs/workspaces.md",
    "docs/specification-reference.md", "examples/sample-specifications/README.md",
])
def test_documents_remove_stage_hiding_and_retain_finished_rows(name):
    document = re.sub(r"\s+", " ", (REPOSITORY_ROOT / name).read_text(encoding="utf-8"))
    assert not re.search(
        r"hide-stage|show-all-stages|visible stages|stage visibility|configured-hidden|"
        r"hidden stages|hidden completed stage|hidden prerequisite|hidden or absent",
        document, re.IGNORECASE,
    )
    assert "Hide terminal rows" in document
    assert "Board settings" in document


def test_documents_publish_the_saved_stage_order_contract():
    normalized_documents = {}
    for name, markers in STAGE_ORDER_GUIDANCE_MARKERS.items():
        document = re.sub(r"\s+", " ", (REPOSITORY_ROOT / name).read_text(encoding="utf-8"))
        normalized_documents[name] = document
        for marker in markers:
            assert marker in document, (name, marker)

    obsolete_fallback = (
        "If loading or saving account settings fails, the board falls back to "
        "canonical inventory order and keeps the last saved order unchanged."
    )
    assert obsolete_fallback not in normalized_documents["docs/workspaces.md"]


def test_documents_publish_the_explicit_completion_contract():
    for name, markers in COMPLETION_GUIDANCE_MARKERS.items():
        document = re.sub(r"\s+", " ", (REPOSITORY_ROOT / name).read_text(encoding="utf-8"))
        for marker in markers:
            assert marker in document, (name, marker)


def test_documents_publish_the_checkout_contract():
    for name, markers in CHECKOUT_GUIDANCE_MARKERS.items():
        document = re.sub(r"\s+", " ", (REPOSITORY_ROOT / name).read_text(encoding="utf-8"))
        for marker in markers:
            assert marker in document, (name, marker)


def test_checkout_documentation_drops_the_cached_only_wording():
    document = re.sub(
        r"\s+", " ", (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    )
    for wording in CHECKOUT_RETIRED_WORDING:
        assert wording not in document, wording


def test_documented_header_limits_match_the_catalog():
    document = (REPOSITORY_ROOT / "docs" / "specification-reference.md").read_text(encoding="utf-8")
    normalized = re.sub(r"\s+", " ", document)
    limit = f"{catalog._HEADER_BYTE_LIMIT:,}"
    assert "A single leading UTF-8 byte order mark (BOM) is ignored" in normalized
    assert f"The header limit is {limit} bytes" in normalized
    assert f"A whole `program.md` descriptor obeys the same {limit}-byte limit" in normalized


def test_documented_output_limit_matches_the_catalog():
    document = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    heading = "## If something looks wrong"
    assert heading in document
    section = document.split(heading, 1)[1].split("\n## ", 1)[0]
    normalized = re.sub(r"\s+", " ", section)
    mebibytes = catalog.MAX_OUTPUT_BYTES / (1024 * 1024)
    limit = f"{mebibytes:g} MiB"
    assert "`producer_output_too_large`" in normalized
    assert f"larger than the {limit} catalog limit" in normalized


def test_event_walkthrough_shows_whole_item_and_named_outcomes():
    document = (REPOSITORY_ROOT / "docs" / "workspaces.md").read_text(encoding="utf-8")
    assert EVENT_COMPLETION_EXAMPLE in document
    assert "event plan's whole-item requirement" in document
    assert "literal `Done` stage" in document
    assert "separate named outcomes" in document


def test_port_conflict_guidance_matches_the_public_message():
    from nyx import runtime

    document = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    assert document.splitlines()[0] == "# Running Nyx"
    section = document.split("## If something looks wrong\n", 1)[1].split("\n## ", 1)[0]
    normalized = re.sub(r"\s+", " ", section)
    for marker in PORT_CONFLICT_GUIDANCE_MARKERS:
        assert marker in normalized, marker
    assert "```" not in section
    assert "If something looks wrong" in runtime.PORT_UNAVAILABLE_MESSAGE
    assert "Running Nyx" in runtime.PORT_UNAVAILABLE_MESSAGE


def test_documents_publish_the_invalid_record_recovery_guidance():
    document = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    # The recovery guidance must not displace the installation block: the first
    # fenced block in each published language stays exactly as documented.
    for language in EXPECTED:
        _documented_commands(document, language)
    assert RECOVERY_GUIDANCE_HEADING in document, "missing invalid-record recovery guidance"
    section = document.split(RECOVERY_GUIDANCE_HEADING, 1)[1].split("\n#", 1)[0]
    normalized = re.sub(r"\s+", " ", section)
    for marker in RECOVERY_GUIDANCE_MARKERS:
        assert marker in normalized, marker
    assert "```" not in section


_ALL_DOCUMENTS = (
    "README.md", "docs/running-nyx.md", "docs/workspaces.md",
    "docs/specification-reference.md",
)
UUID_COMMANDS = (
    "python3 -c 'import uuid; print(uuid.uuid4())'",
    "uuidgen | tr '[:upper:]' '[:lower:]'",
    "[guid]::NewGuid().ToString()",
)
UUID_RULE_MARKERS = (
    "Every UUID field (`Package ID`, `Program ID`, `Program Membership`, "
    "`Prerequisite`, `Completion Prerequisite`, and `Superseded By`) takes a "
    "lowercase version 4 UUID only",
    "An invalid `Package ID` makes the item lose its identity, so ",
    "An invalid `Prerequisite` or `Completion Prerequisite` shows "
    "**unresolved target**",
    "An invalid `Program Membership` or `Superseded By` leaves the item's "
    "identity intact",
)
PROGRAM_GRAMMAR_MARKERS = (
    "holds exactly one `Program ID` line and one `Program Title` line and no "
    "other non-empty content",
    "The ID must equal the program directory name",
    "the title must be 1–120 characters",
    "inside a canonical UUID program directory makes every program membership "
    "in the workspace **unknown**",
    "program-title search finds nothing, while **Workspace issues** names that "
    "descriptor",
    "A program directory whose name is not a lowercase version 4 UUID is "
    "skipped without a diagnostic",
)
UNRESOLVED_CHECK_FIRST = "check **Workspace issues** first for an unreadable directory or specification file that made identity coverage incomplete"
UNRESOLVED_MARKERS = (
    UNRESOLVED_CHECK_FIRST,
    "Descriptor problems affect programs only and do not make identity "
    "coverage incomplete",
    "conservative all-or-nothing resolution",
)


def _normalized(name: str) -> str:
    return re.sub(r"\s+", " ", (REPOSITORY_ROOT / name).read_text(encoding="utf-8"))


def _section(name: str, heading: str) -> str:
    text = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
    assert heading in text, (name, heading)
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]
    return re.sub(r"\s+", " ", section)


def test_dependency_and_completion_policy_text_matches_the_board():
    readme = _normalized("README.md")
    assert "satisfied, waiting, unknown, or unavailable" not in readme
    assert "shows links to its resolved prerequisite and dependent items" in readme
    assert "**unresolved target** only when a relationship cannot be resolved" in readme
    assert "asks you to configure completed stages" not in _normalized("docs/workspaces.md")
    assert "actionable configuration explanation" not in _normalized(
        "docs/specification-reference.md")
    assert "The policy is set in **Board settings**; a missing policy" in _normalized(
        "docs/workspaces.md")
    assert ("The completion policy is configured in the collapsed **Board settings** "
            "panel") in _normalized("docs/specification-reference.md")
    assert ("a missing policy leaves the requirement **unknown**, with no prompt "
            "or explanation") in _normalized("docs/workspaces.md")
    assert ("a completion dependency is **unknown**; the board shows no prompt "
            "or explanation") in _normalized("docs/specification-reference.md")


def test_configuration_unavailable_recovery_is_one_fence_free_bullet():
    document = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    heading = "## If something looks wrong\n"
    assert heading in document
    raw = document.split(heading, 1)[1].split("\n## ", 1)[0]
    bullets = [
        re.sub(r"\s+", " ", bullet)
        for bullet in re.split(r"\n(?=- )", raw)
        if "Configuration: unavailable" in bullet
    ]
    assert len(bullets) == 1
    bullet = bullets[0]
    for marker in (
        "`nyx --status` prints `Configuration: unavailable`",
        "`nyx --setup` does not repair an existing unreadable record",
        "Run `nyx --stop`",
        "restore `~/.nyx/config/config.json` from a backup",
        "move it aside",
        "`nyx --setup <workspace>`",
        "discards the saved board settings and checkout folders",
    ):
        assert marker in bullet, marker
    assert "```" not in raw
    for forbidden in ("pkill", "killall", "taskkill", "kill -9"):
        assert forbidden not in bullet, forbidden


@pytest.mark.parametrize("name", ["docs/specification-reference.md", "docs/workspaces.md"])
def test_documents_state_the_uuid_rule_effects_and_commands(name):
    document = _normalized(name)
    for marker in UUID_RULE_MARKERS + UUID_COMMANDS:
        assert marker in document, (name, marker)
    assert "**Workspace issues** lists nothing" in document, name
    assert "invalid `Program ID`" in document, name


def test_reference_states_program_id_invalidates_the_descriptor():
    document = _normalized("docs/specification-reference.md")
    assert ("An invalid `Program ID` makes its program descriptor invalid, as "
            "described under Programs, without affecting any item's identity") in document


def test_readme_publishes_no_uuid_generation_command():
    readme = _normalized("README.md")
    for command in UUID_COMMANDS:
        assert command not in readme, command


def test_reference_programs_section_documents_the_descriptor_grammar():
    section = _section("docs/specification-reference.md", "## Programs")
    for marker in PROGRAM_GRAMMAR_MARKERS:
        assert marker in section, marker


def test_reference_scopes_bold_names_to_reported_text_and_core_labels_are_plain():
    reference = _normalized("docs/specification-reference.md")
    assert ("Bold names are documented for reported-text fields only. The core "
            "field labels in the table above are written plain") in reference
    core_bold = re.compile(
        r"\*\*(Package ID|Status|Target repo|Program ID|Program Title|"
        r"Program Membership|Claim|Prerequisite|Completion Prerequisite|"
        r"Superseded By)\*{0,2}:"
    )
    for name in _ALL_DOCUMENTS:
        assert not core_bold.search(_normalized(name)), name


@pytest.mark.parametrize("name,section_heading,old_phrase", [
    ("docs/running-nyx.md", "## If something looks wrong\n",
     "inspect the involved `spec.md` headers"),
    ("docs/specification-reference.md", "## Diagnostics",
     "inspect the affected specification headers"),
])
def test_unresolved_target_guidance_checks_workspace_issues_first(
        name, section_heading, old_phrase):
    section = _section(name, section_heading)
    for marker in UNRESOLVED_MARKERS:
        assert marker in section, (name, marker)
    if old_phrase in section:
        assert section.index(old_phrase) > section.index(UNRESOLVED_CHECK_FIRST), name


def test_documents_name_board_settings_and_reset_clears_both_settings():
    for name in _ALL_DOCUMENTS:
        document = _normalized(name)
        assert "**Board row order**" not in document, name
        assert "Reload board row order" not in document, name
    assert "used by the bundled examples" not in _normalized("docs/workspaces.md")
    for name, phrase in (
        ("docs/workspaces.md", "clears only the current root's saved order"),
        ("docs/running-nyx.md", "clears the saved order for the current workspace only"),
        ("docs/running-nyx.md", "discards the editor's unsaved moves"),
    ):
        assert phrase not in _normalized(name), (name, phrase)
    assert ("**Reset** clears the current root's saved row order and "
            "completed-stage set") in _normalized("docs/workspaces.md")
    guide = _normalized("docs/running-nyx.md")
    assert ("**Cancel** discards the unsaved moves and unsaved completion "
            "choices") in guide
    assert ("**Reset** clears the current root's row order and completed-stage "
            "set; the board then follows the canonical inventory order") in guide
    assert "use the row-order editor to move **Queue**" in guide
    assert "**Reload board settings**" in guide
    assert "**Reload board settings**" in _normalized("docs/workspaces.md")


def test_documents_state_the_loopback_exposure_without_caller_authentication():
    for name in ("README.md", "docs/running-nyx.md"):
        document = _normalized(name)
        assert "serves one local account" not in document, name
        assert "intended for one local account" not in document, name
    readme = _normalized("README.md")
    assert "The browser is read-only;" not in readme
    assert "without caller authentication, so other local accounts on the same host" in readme
    assert "The browser never edits specification files" in readme
    for heading in ("### Desktop limits", "## If something looks wrong\n"):
        section = _section("docs/running-nyx.md", heading)
        assert "without caller authentication, so other local accounts on the same host" in section, heading
        assert "change board settings" in section, heading


def _run_command(command: str, *, root: Path, environment: dict[str, str]):
    """Run one published Linux installation command in its own bash."""

    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", command], cwd=root, env=environment,
        capture_output=True, text=True, check=False, timeout=180,
    )
    assert result.returncode == 0, (command, result.returncode, result.stdout, result.stderr)
    return result


@pytest.mark.skipif(os.name == "nt", reason="per-line commands run only on the Linux installation path")
def test_native_shell_propagates_failed_command(tmp_path):
    with pytest.raises(AssertionError, match=r"\b17\b"):
        _run_command(_native_exit(17), root=tmp_path, environment=_installed_environment(tmp_path))


_PWSH_SESSION_PREFIX = (
    "$ErrorActionPreference = 'Stop'\n"
    "$PSNativeCommandUseErrorActionPreference = $true\n"
    "trap {\n"
    "  [Console]::Error.WriteLine(($_ | Out-String))\n"
    "  if ($_.Exception -is [System.Management.Automation.NativeCommandExitException] "
    "-and $LASTEXITCODE -is [int] -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
    "  exit 1\n"
    "}\n"
)


def _session_arguments(block: str) -> list[str]:
    """Return the argument vector running ``block`` as one shell session."""

    if os.name == "nt":
        return [
            "pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
            _PWSH_SESSION_PREFIX + block,
        ]
    return ["bash", "-euo", "pipefail", "-c", block]


_TIMEOUT_REPORT_GRACE_SECONDS = 10.0


def _kill_session_tree(process: subprocess.Popen) -> None:
    """Kill the session shell and every descendant that holds its output pipes."""

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True, check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        process.kill()


def _run_session(
    block: str, *, root: Path, environment: dict[str, str] | None = None,
    timeout: float,
) -> subprocess.CompletedProcess:
    """Run ``block`` as one shell session, killing its whole process tree on timeout.

    ``subprocess.run`` kills only the shell, and the build children that keep the
    output pipes open then keep the read blocked.  The session therefore leads
    its own process group (or is killed with ``taskkill /T`` on Windows), and the
    partial output is collected with a bounded read before the timeout is raised.
    """

    arguments = _session_arguments(block)
    if shutil.which(arguments[0]) is None:
        pytest.fail(f"the lane's shell {arguments[0]!r} is not available")
    options: dict[str, object] = {}
    if os.name != "nt":
        options["start_new_session"] = True
    with subprocess.Popen(
        arguments, cwd=root, env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, **options,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as expired:
            _kill_session_tree(process)
            try:
                stdout, stderr = process.communicate(timeout=_TIMEOUT_REPORT_GRACE_SECONDS)
            except subprocess.TimeoutExpired as lingering:
                stdout, stderr = lingering.stdout, lingering.stderr
            raise subprocess.TimeoutExpired(
                arguments, timeout, output=_as_text(stdout), stderr=_as_text(stderr)
            ) from expired
        except BaseException:
            _kill_session_tree(process)
            raise
    return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)


def _as_text(output: str | bytes | None) -> str:
    if output is None:
        return ""
    return output.decode(errors="replace") if isinstance(output, bytes) else output


def test_session_fails_hard_when_the_lane_shell_is_missing(tmp_path, monkeypatch):
    shell = _session_arguments("unused")[0]
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(pytest.fail.Exception, match=rf"the lane's shell '{shell}' is not available"):
        _run_session("unused", root=tmp_path, timeout=120)


def test_session_timeout_kills_the_tree_and_reports_partial_output(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    child = (
        "import time\n"
        "print('session-started', flush=True)\n"
        f"path = {str(heartbeat)!r}\n"
        "end = time.monotonic() + 120\n"
        "while time.monotonic() < end:\n"
        "    open(path, 'a').write('x')\n"
        "    time.sleep(0.1)\n"
    )
    script = tmp_path / "outlives.py"
    script.write_text(child, encoding="utf-8")
    if os.name == "nt":
        block = f"& '{sys.executable}' '{script}'\necho after-child"
    else:
        block = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}\necho after-child"
    timeout = 3.0
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        _run_session(block, root=tmp_path, timeout=timeout)
    elapsed = time.monotonic() - started
    assert elapsed < timeout + _TIMEOUT_REPORT_GRACE_SECONDS, elapsed
    assert "session-started" in _as_text(raised.value.stdout)
    size = heartbeat.stat().st_size
    assert size > 0
    time.sleep(1.0)
    assert heartbeat.stat().st_size == size, "the foreground child survived the timeout"


def _native_exit(code: int) -> str:
    interpreter = sys.executable
    if os.name == "nt":
        return f"& '{interpreter.replace(chr(39), chr(39) * 2)}' -c 'import sys; sys.exit({code})'"
    return f"{shlex.quote(interpreter)} -c 'import sys; sys.exit({code})'"


def test_session_keeps_state_between_lines(tmp_path):
    if os.name == "nt":
        block = "$env:NYX_SESSION_PROBE = 'kept'\nWrite-Output \"seen=$env:NYX_SESSION_PROBE\""
    else:
        block = 'export NYX_SESSION_PROBE=kept\necho "seen=$NYX_SESSION_PROBE"'
    result = _run_session(block, root=tmp_path, timeout=120)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.strip() == "seen=kept"


def test_session_runs_from_the_given_root(tmp_path):
    block = "Write-Output (Get-Location).Path" if os.name == "nt" else "pwd"
    result = _run_session(block, root=tmp_path, timeout=120)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


def test_session_failing_native_command_stops_the_block_with_its_own_exit_code(tmp_path):
    block = _native_exit(17) + "\necho after-failure"
    result = _run_session(block, root=tmp_path, timeout=120)
    assert result.returncode == 17, (result.returncode, result.stdout, result.stderr)
    assert "after-failure" not in result.stdout


def test_session_without_errors_exits_zero(tmp_path):
    block = _native_exit(0) + "\necho finished"
    result = _run_session(block, root=tmp_path, timeout=120)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "finished" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="the PowerShell trap exists only on Windows lanes")
def test_session_non_native_terminating_error_exits_non_zero_with_record(tmp_path):
    # A native command exiting 0 leaves LASTEXITCODE 0, so a ``throw`` must
    # still end the session non-zero.
    block = _native_exit(0) + "\nthrow 'nyx-session-boom'\nWrite-Output after-throw"
    result = _run_session(block, root=tmp_path, timeout=120)
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert "nyx-session-boom" in result.stderr
    assert "after-throw" not in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="the PowerShell trap exists only on Windows lanes")
def test_session_native_failure_record_reaches_stderr(tmp_path):
    result = _run_session(_native_exit(23), root=tmp_path, timeout=120)
    assert result.returncode == 23, (result.returncode, result.stdout, result.stderr)
    assert result.stderr.strip()


@pytest.mark.skipif(
    os.environ.get("NYX_VERIFY_DOCUMENTED_BUILD") != "1",
    reason="the documented build runs only in the desktop documented-build step",
)
def test_documented_desktop_build_blocks_run_literally():
    language = _lane_build_language()
    if language is None:
        pytest.skip("the documented build runs only on the Windows and macOS desktop lanes")
    guide = (REPOSITORY_ROOT / "docs" / "running-nyx.md").read_text(encoding="utf-8")
    block = _published_build_block(guide, language)
    bound = _BUILD_TIMEOUT_MINUTES[language] * 60
    try:
        result = _run_session(
            block, root=REPOSITORY_ROOT, environment=dict(os.environ), timeout=bound
        )
    except subprocess.TimeoutExpired as expired:
        pytest.fail(f"the documented {language} build exceeded {bound} s: {expired.stdout!r}")
    print(result.stdout)
    print(result.stderr, file=sys.stderr)
    assert result.returncode == 0, (
        f"documented {language} build exited {result.returncode}",
        result.stdout[-4000:], result.stderr[-4000:],
    )


def _exercise_documented_linux_installation(tmp_path: Path) -> None:
    """Run the published Linux commands exactly as written."""

    blocks = [
        _documented_commands((REPOSITORY_ROOT / name).read_text(encoding="utf-8"), "bash")
        for name in DOCUMENTS
    ]
    assert blocks[0] == blocks[1]
    commands = blocks[0]
    subprocess.run(["git", "diff", "--exit-code", "HEAD"], cwd=REPOSITORY_ROOT, check=True)
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=REPOSITORY_ROOT
    ).decode().split("\0")
    root = tmp_path / "checkout"
    root.mkdir()
    for name in filter(None, tracked):
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPOSITORY_ROOT / name, destination, follow_symlinks=False)
    home = tmp_path / "home"
    home.mkdir()
    environment = _installed_environment(home)
    assert not (root / ".venv").exists()
    assert not (home / ".nyx").exists()
    for command in commands[:2]:
        _run_command(command, root=root, environment=environment)
    venv = root / ".venv"
    interpreter = _venv_executable(venv, "python")
    console = _venv_executable(venv, "nyx")
    assert console.resolve().is_relative_to(venv.resolve())
    provenance = subprocess.run(
        [str(interpreter), "-c",
         "import importlib.metadata, json, nyx, sys; "
         "d = importlib.metadata.distribution('nyx'); "
         "print(json.dumps({'module': nyx.__file__, 'prefix': sys.prefix, "
         "'direct_url': json.loads(d.read_text('direct_url.json'))}))"],
        cwd=tmp_path, env=environment, capture_output=True, text=True, check=True, timeout=30,
    )
    installed = json.loads(provenance.stdout)
    assert Path(installed["prefix"]).resolve() == venv.resolve()
    assert Path(installed["module"]).resolve().is_relative_to(venv.resolve())
    assert not installed["direct_url"].get("dir_info", {}).get("editable", False)
    source_url = urlsplit(installed["direct_url"]["url"])
    assert source_url.scheme == "file"
    assert Path(url2pathname(source_url.path)).resolve() == root.resolve()
    sample = (root / "examples" / "sample-specifications").resolve()
    setup = _run_command(commands[2], root=root, environment=environment)
    assert setup.stdout.strip() == f"configured {sample}"
    configuration = json.loads((home / ".nyx/config/config.json").read_text(encoding="utf-8"))
    assert configuration["specification_root"] == str(sample)
    assert configuration == {"schema_version": 4, "specification_root": str(sample)}
    locator = home / ".nyx/runtime/instance.json"
    claim = home / ".nyx/runtime/lease.lock"
    try:
        started = _run_command(commands[3], root=root, environment=environment)
        assert started.stdout.strip() == "http://127.0.0.1:8765/"
        assert json.loads(locator.read_text(encoding="utf-8"))["url"] == started.stdout.strip()
        status = _run_command(commands[4], root=root, environment=environment)
        assert status.stdout.splitlines() == [
            "Configuration: configured",
            f"Workspace: {json.dumps(str(sample))}",
            "Runtime: running",
            'URL: "http://127.0.0.1:8765/"',
        ]
        assert _native_claim_probe(interpreter, claim, root=tmp_path, home=home) == "busy"
        connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=10)
        try:
            connection.request("GET", "/api/catalog", headers={"Host": "127.0.0.1:8765"})
            response = connection.getresponse()
            catalog = json.loads(response.read())
            assert response.status == 200
            assert any(
                entry["declared"]["title"] == "Build the route preview"
                for entry in catalog["entries"]
            )
        finally:
            connection.close()
        _run_command(commands[5], root=root, environment=environment)
        assert not locator.exists()
        assert _native_claim_probe(interpreter, claim, root=tmp_path, home=home) == "free"
    finally:
        # Only the isolated home can authorize cleanup of this test's instance.
        if locator.exists():
            _run_command(commands[5], root=root, environment=environment)


def _documented_desktop_artifact() -> ArtifactLayout:
    staged = os.environ.get("NYX_DESKTOP_ARTIFACT")
    if not staged:
        pytest.skip(
            "the private desktop artifact is staged only in the desktop artifact lane"
        )
    artifact = ArtifactLayout(Path(staged).resolve())
    assert artifact.executable.is_file(), f"staged application is missing: {artifact.executable}"
    documented = (
        DESKTOP_ARTIFACT_PATHS["windows"]
        if os.name == "nt"
        else DESKTOP_ARTIFACT_PATHS["macos"]
    )
    normalized = str(artifact.artifact).replace("\\", "/")
    assert documented.replace("\\", "/") in normalized, (documented, normalized)
    return artifact


def _exercise_documented_desktop_actions(tmp_path: Path) -> None:
    """Run the published desktop actions against the staged private artifact."""

    artifact = _documented_desktop_artifact()
    root = tmp_path / "documented"
    root.mkdir()
    home = root / "home"
    home.mkdir()
    decoy = _inert_path(root)
    environment = _sanitized_environment(home, decoy)

    # First launch: the chooser owns the account but serves no board.
    with _running_gui(artifact, root, environment) as gui:
        _wait_for_window(gui)
        with pytest.raises(OSError):
            _request("/api/catalog")
        assert gui.process.poll() is None, gui.diagnostics()
        assert gui.close() == 0, gui.diagnostics()
    _assert_claims_released(home)

    with _workspace(root) as workspace:
        # Open a configured workspace, serve the board, then Quit.
        _, first_configuration = _write_configuration(home, workspace)
        expected_first = json.loads(_expected_catalog(first_configuration))
        with _running_gui(artifact, root, environment) as gui:
            status, body, _ = _wait_for_board(time.monotonic() + _START_TIMEOUT, gui)
            assert status == 200
            assert json.loads(body) == expected_first
            assert gui.close() == 0, gui.diagnostics()
        _assert_claims_released(home)
        _wait_port_free(time.monotonic() + 15)

        # Change workspace: the replacement selection is served after restart.
        second = root / "second"
        shutil.copytree(workspace, second)
        shutil.rmtree(second / "Trail_API")
        _, second_configuration = _write_configuration(home, second)
        expected_second = json.loads(_expected_catalog(second_configuration))
        assert expected_first != expected_second
        with _running_gui(artifact, root, environment) as gui:
            status, body, _ = _wait_for_board(time.monotonic() + _START_TIMEOUT, gui)
            assert status == 200
            assert json.loads(body) == expected_second
            assert gui.close() == 0, gui.diagnostics()
        _assert_claims_released(home)
        _wait_port_free(time.monotonic() + 15)

        # Recovery: a surviving former worker blocks the start until it exits.
        config_file, _ = _write_configuration(home, workspace)
        before = config_file.read_bytes()
        _, recovery_path = _claim_paths(home)
        recovery_path.parent.mkdir(parents=True, exist_ok=True)
        former = NativeClaim(recovery_path)
        assert former.acquire(blocking=False)
        try:
            with _running_gui(artifact, root, environment) as gui:
                _wait_for_window(gui)
                with pytest.raises(OSError):
                    _request("/api/catalog")
                assert gui.process.poll() is None, gui.diagnostics()
                assert config_file.read_bytes() == before
                assert gui.close() == 0, gui.diagnostics()
        finally:
            former.close()
        _wait_port_free(time.monotonic() + 15)


@pytest.mark.skipif(
    os.environ.get("NYX_VERIFY_DOCUMENTED_INSTALL") != "1",
    reason="documented installation runs in its dedicated native hosted lane",
)
def test_documented_native_installation_and_lifecycle(tmp_path):
    if os.name == "nt" or sys.platform == "darwin":
        _exercise_documented_desktop_actions(tmp_path)
    else:
        _exercise_documented_linux_installation(tmp_path)
