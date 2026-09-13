"""Keep source, test, and build artifacts free of private-provenance material."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from nyx import catalog, server

REPOSITORY_ROOT = Path(__file__).parents[1].resolve()
_FICTIONAL_SENTINELS = (
    "real-spec-root.invalid",
    "source-checkout.invalid",
    "provenance-map.invalid",
    "payload-capture.invalid",
    "diagnostic-screenshot.invalid",
)


def _inspection_paths() -> list[Path]:
    paths: list[Path] = []
    for directory in (
        REPOSITORY_ROOT / "nyx",
        REPOSITORY_ROOT / "tests",
        REPOSITORY_ROOT / ".github",
    ):
        if directory.is_dir():
            paths.extend(path for path in directory.rglob("*") if path.is_file())
    for directory_name in ("dist", "build"):
        directory = REPOSITORY_ROOT / directory_name
        if directory.is_dir():
            paths.extend(path for path in directory.rglob("*") if path.is_file())
    return sorted(set(paths))


def test_source_tests_static_wheels_and_ci_artifacts_contain_no_fictional_sensitive_markers():
    inspected = _inspection_paths()
    assert inspected
    for path in inspected:
        if path == Path(__file__).resolve():
            continue
        content = path.read_bytes().decode("utf-8", errors="replace")
        assert not any(marker in content for marker in _FICTIONAL_SENTINELS), path


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_catalog_and_safe_provider_errors_do_not_capture_data_in_nyx():
    with TemporaryDirectory() as temporary:
        root = Path(temporary) / "fictional-root"
        root.mkdir()
        anchor = root / "Fictional" / "Queue" / "sample" / "spec.md"
        anchor.parent.mkdir(parents=True)
        anchor.write_text("# Fictional sample\n", encoding="utf-8")
        before = _snapshot(root)

        rendered = catalog.scan_catalog(root, version=2)
        assert '"entries"' in rendered
        assert _snapshot(root) == before

        with pytest.raises(server.CatalogError) as error:
            server._catalog_from_provider(  # noqa: SLF001 - inspect the safe boundary
                lambda: (_ for _ in ()).throw(RuntimeError("private capture details"))
            )
        assert error.value.code == "producer_failed"
        assert _snapshot(root) == before
        assert not any(path.name.endswith((".json", ".log", ".png")) for path in root.rglob("*"))


def test_no_environment_redirects_catalog_state_into_the_checkout():
    assert not (REPOSITORY_ROOT / ".config").exists()
    assert not (REPOSITORY_ROOT / ".local").exists()
    assert os.environ.get("XDG_CONFIG_HOME") != str(REPOSITORY_ROOT / ".config")
    assert os.environ.get("XDG_STATE_HOME") != str(REPOSITORY_ROOT / ".local" / "state")
