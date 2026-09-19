"""Keep source, test, and build artifacts free of private-provenance material."""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
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


def _is_own_fixture_bytecode(path: Path) -> bool:
    bytecode_directory = Path(__file__).resolve().parent / "__pycache__"
    prefix = f"{Path(__file__).stem}."
    return path.parent == bytecode_directory and path.name.startswith(prefix) and path.suffix == ".pyc"


def _contains_sensitive_marker(path: Path) -> bool:
    if _is_own_fixture_bytecode(path):
        return False
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                if any(marker in member.filename for marker in _FICTIONAL_SENTINELS):
                    return True
                content = archive.read(member).decode("utf-8", errors="replace")
                if any(marker in content for marker in _FICTIONAL_SENTINELS):
                    return True
        return False
    content = path.read_bytes().decode("utf-8", errors="replace")
    return any(marker in content for marker in _FICTIONAL_SENTINELS)


def test_source_tests_static_wheels_and_ci_artifacts_contain_no_fictional_sensitive_markers():
    inspected = _inspection_paths()
    assert inspected
    for path in inspected:
        if path == Path(__file__).resolve():
            continue
        assert not _contains_sensitive_marker(path), path


def test_compressed_wheel_members_are_inspected_for_sensitive_markers():
    with TemporaryDirectory() as temporary:
        wheel = Path(temporary) / "synthetic.whl"
        with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("nyx/private.txt", _FICTIONAL_SENTINELS[0])
        assert _contains_sensitive_marker(wheel)


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
        repository_before = _snapshot(REPOSITORY_ROOT)
        before = _snapshot(root)

        rendered = catalog.scan_catalog(root)
        assert '"entries"' in rendered
        assert _snapshot(root) == before
        assert _snapshot(REPOSITORY_ROOT) == repository_before

        with pytest.raises(server.CatalogError) as error:
            server._catalog_from_provider(  # noqa: SLF001 - inspect the safe boundary
                lambda: (_ for _ in ()).throw(RuntimeError("private capture details"))
            )
        assert error.value.code == "producer_failed"
        assert _snapshot(root) == before
        assert _snapshot(REPOSITORY_ROOT) == repository_before
        assert not any(path.name.endswith((".json", ".log", ".png")) for path in root.rglob("*"))


def test_bytecode_enabled_import_creates_own_fixture_bytecode_without_failing_scan():
    environment = os.environ.copy()
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    subprocess.run(
        [sys.executable, "-c", "import tests.test_confidentiality"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
    )
    bytecode = sorted(
        (REPOSITORY_ROOT / "tests" / "__pycache__").glob("test_confidentiality*.pyc")
    )
    assert bytecode
    assert any(
        marker in path.read_bytes().decode("utf-8", errors="replace")
        for path in bytecode
        for marker in _FICTIONAL_SENTINELS
    )
    assert all(not _contains_sensitive_marker(path) for path in bytecode)


def test_no_environment_redirects_catalog_state_into_the_checkout():
    assert not (REPOSITORY_ROOT / ".config").exists()
    assert not (REPOSITORY_ROOT / ".local").exists()
    assert not (REPOSITORY_ROOT / ".nyx").exists()
    assert os.environ.get("XDG_CONFIG_HOME") != str(REPOSITORY_ROOT / ".config")
    assert os.environ.get("XDG_STATE_HOME") != str(REPOSITORY_ROOT / ".local" / "state")
