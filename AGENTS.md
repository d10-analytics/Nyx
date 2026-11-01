# Nyx repository instructions

## Environment and verification

- Use Python 3.12 in a virtual environment (for example the ignored `.venv/`)
  and run every command below with it activated. Install the way CI does:
  `python -m pip install -e '.[test]' build`, then
  `python -m playwright install chromium` for the browser tests. The native
  desktop lanes install `'.[test,desktop,build]'` instead.
- Lint: `ruff check .` (settings in `pyproject.toml`). No type checker is
  configured.
- Use focused pytest selections for development feedback, for example
  `python -m pytest tests/test_catalog.py -v`.
- Full verification is the GitHub Actions workflow
  `.github/workflows/tests.yml`, run on every push and on manual dispatch.
  Every push, to a branch or to a tag, runs the three package lanes; a push
  to `main` also runs the two desktop lanes, and a manual dispatch runs
  those two desktop lanes only, from that branch:
  `gh workflow run tests.yml --ref <branch>`. Hand-run the desktop lanes on
  a branch before merging it whenever the branch changes `pyproject.toml`
  other than its `[tool.ruff]` settings, a file listed in
  `docs/systems/desktop/system.toml`, `nyx/desktop_worker.py`, the desktop
  build section of `docs/running-nyx.md`, `tests/test_desktop.py`,
  `tests/test_desktop_artifact.py`, `tests/native_ui.py`,
  `tests/test_documented_install.py`, or `.github/workflows/tests.yml`. A
  manual run is CI evidence only when both desktop lanes ran and passed; a
  run whose desktop lanes show as skipped proves nothing. Do not hand-run
  `main`: its push already runs every lane.
  Local runs are feedback, not CI evidence.
- Some proofs run only on CI hosts or when CI sets
  `NYX_VERIFY_DOCUMENTED_INSTALL`, `NYX_VERIFY_DOCUMENTED_BUILD`, or
  `NYX_REQUIRE_NATIVE_DESKTOP`. Locally they skip; a skip is not a pass.

## Required project references

- Before design, implementation or review, follow Reading path in
  `docs/systems/README.md`.

## Core conventions

- Nyx reads specifications and never edits or moves them; the browser never
  edits specification files (`README.md`).
- Every file under `nyx/` and `scripts/` belongs to exactly one system in
  `docs/systems/`. Update its `system.toml` and README in the same change
  (`tests/test_system_definitions.py`).
- Follow the repository-wide rules in `docs/systems/README.md` and the
  cross-system invariants of every system you change.

## Testing evidence

- Tests that start the service use the `isolated_port` fixture; tests that run
  a daemon on a thread use `managed_daemon_run` (`tests/conftest.py`).
- Prove each new workflow check load-bearing with a mutation it rejects, as
  `tests/test_workflow.py` does.

## Confidentiality

This repository is public. Commit no private paths, workspace content,
credentials, or captured catalog data. Examples, screenshots, and test fixtures
use fictional data (`docs/running-nyx.md`, `tests/test_confidentiality.py`).
