"""Keep the system definitions under docs/systems exact and complete."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[1].resolve()
SYSTEMS_DIR = "docs/systems"
COVERED_ROOTS = ("nyx", "scripts")
SCHEMA_VERSION = 1
ALLOWED_FIELDS = {"schema_version", "name", "files"}
GLOB_CHARACTERS = "*?[]{}"


def git_listing(root: Path) -> set[str]:
    """Tracked, or new and not ignored, files under the covered roots."""
    completed = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            *COVERED_ROOTS,
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    paths = completed.stdout.decode("utf-8").split("\0")
    return {path for path in paths if path and (root / path).is_file()}


def _is_covered(path: str) -> bool:
    return path.split("/", 1)[0] in COVERED_ROOTS


def system_problems(root: Path, listing: set[str]) -> list[str]:
    """Return every violation of the system definition rules, sorted."""
    problems: list[str] = []
    systems = root / SYSTEMS_DIR
    if not systems.is_dir():
        return [f"{SYSTEMS_DIR}: missing"]
    owners: dict[str, list[str]] = {}
    for folder in sorted(path for path in systems.iterdir() if path.is_dir()):
        name = folder.name
        readme = folder / "README.md"
        if not readme.is_file():
            problems.append(f"{name}: README.md missing")
        elif not readme.read_text(encoding="utf-8").strip():
            problems.append(f"{name}: README.md empty")
        definition = folder / "system.toml"
        if not definition.is_file():
            problems.append(f"{name}: system.toml missing")
            continue
        try:
            data = tomllib.loads(definition.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            problems.append(f"{name}: system.toml is not valid TOML")
            continue
        for field in sorted(set(data) - ALLOWED_FIELDS):
            problems.append(f"{name}: unknown field {field}")
        if data.get("schema_version") != SCHEMA_VERSION:
            problems.append(f"{name}: schema_version must be {SCHEMA_VERSION}")
        if data.get("name") != name:
            problems.append(f"{name}: name {data.get('name')!r} differs from folder")
        files = data.get("files")
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            problems.append(f"{name}: files must be a list of strings")
            continue
        for member in files:
            if any(character in member for character in GLOB_CHARACTERS):
                problems.append(f"{name}: glob entry {member}")
                continue
            if not (root / member).is_file():
                problems.append(f"{name}: missing member {member}")
            owners.setdefault(member, []).append(name)
    for member in sorted(owners):
        if len(owners[member]) > 1:
            problems.append(f"{member}: in two systems ({', '.join(sorted(owners[member]))})")
    for path in sorted(listing):
        if _is_covered(path) and path not in owners:
            problems.append(f"{path}: in no system")
    return sorted(problems)


def write_system(root: Path, name: str, files: list[str], **overrides: object) -> None:
    folder = root / SYSTEMS_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    entries = "".join(f'    "{member}",\n' for member in files)
    lines = [
        f"schema_version = {overrides.get('schema_version', SCHEMA_VERSION)}",
        f'name = "{overrides.get("name", name)}"',
        f"files = [\n{entries}]",
    ]
    if "extra" in overrides:
        lines.append(str(overrides["extra"]))
    (folder / "system.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def layout(tmp_path: Path) -> Path:
    """Two systems covering three files plus one outside member."""
    for relative in ("nyx/a.py", "nyx/b.py", "scripts/c.py", "extra.spec"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
    write_system(tmp_path, "alpha", ["nyx/a.py", "nyx/b.py"])
    write_system(tmp_path, "beta", ["extra.spec", "scripts/c.py"])
    return tmp_path


LISTING = {"nyx/a.py", "nyx/b.py", "scripts/c.py"}


def test_repository_systems_are_exact_and_complete() -> None:
    listing = git_listing(REPOSITORY_ROOT)
    assert "nyx/catalog.py" in listing
    assert "nyx/static/app.js" in listing
    assert system_problems(REPOSITORY_ROOT, listing) == []


def test_valid_layout_has_no_problems(layout: Path) -> None:
    assert system_problems(layout, LISTING) == []


def test_missing_systems_directory(tmp_path: Path) -> None:
    assert system_problems(tmp_path, LISTING) == ["docs/systems: missing"]


def test_new_file_in_no_system(layout: Path) -> None:
    (layout / "nyx/new.py").write_text("x\n", encoding="utf-8")
    assert system_problems(layout, LISTING | {"nyx/new.py"}) == [
        "nyx/new.py: in no system"
    ]


def test_file_in_two_systems(layout: Path) -> None:
    write_system(layout, "beta", ["extra.spec", "nyx/a.py", "scripts/c.py"])
    assert system_problems(layout, LISTING) == [
        "nyx/a.py: in two systems (alpha, beta)"
    ]


def test_missing_member(layout: Path) -> None:
    (layout / "nyx/b.py").unlink()
    assert system_problems(layout, {"nyx/a.py", "scripts/c.py"}) == [
        "alpha: missing member nyx/b.py"
    ]


def test_missing_outside_member_is_reported_but_present_one_is_accepted(
    layout: Path,
) -> None:
    assert system_problems(layout, LISTING) == []
    (layout / "extra.spec").unlink()
    assert system_problems(layout, LISTING) == ["beta: missing member extra.spec"]


def test_name_differs_from_folder(layout: Path) -> None:
    (layout / SYSTEMS_DIR / "alpha").rename(layout / SYSTEMS_DIR / "gamma")
    assert system_problems(layout, LISTING) == [
        "gamma: name 'alpha' differs from folder"
    ]


def test_missing_readme(layout: Path) -> None:
    (layout / SYSTEMS_DIR / "alpha" / "README.md").unlink()
    assert system_problems(layout, LISTING) == ["alpha: README.md missing"]


def test_empty_readme(layout: Path) -> None:
    (layout / SYSTEMS_DIR / "alpha" / "README.md").write_text(" \n", encoding="utf-8")
    assert system_problems(layout, LISTING) == ["alpha: README.md empty"]


def test_folder_without_system_toml(layout: Path) -> None:
    (layout / SYSTEMS_DIR / "alpha" / "system.toml").unlink()
    assert system_problems(layout, LISTING) == [
        "alpha: system.toml missing",
        "nyx/a.py: in no system",
        "nyx/b.py: in no system",
    ]


def test_wrong_schema_version(layout: Path) -> None:
    write_system(layout, "alpha", ["nyx/a.py", "nyx/b.py"], schema_version=2)
    assert system_problems(layout, LISTING) == ["alpha: schema_version must be 1"]


def test_unknown_field(layout: Path) -> None:
    write_system(layout, "alpha", ["nyx/a.py", "nyx/b.py"], extra='notes = "x"')
    assert system_problems(layout, LISTING) == ["alpha: unknown field notes"]


def test_glob_entry(layout: Path) -> None:
    write_system(layout, "alpha", ["nyx/a.py", "nyx/*.py"])
    assert system_problems(layout, LISTING) == [
        "alpha: glob entry nyx/*.py",
        "nyx/b.py: in no system",
    ]


def test_git_listing_counts_new_files_but_not_ignored_or_deleted(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("skipped.py\n", encoding="utf-8")
    (tmp_path / "nyx").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "other.py").write_text("x\n", encoding="utf-8")
    for relative in ("nyx/kept.py", "nyx/deleted.py", "scripts/tool.py"):
        (tmp_path / relative).write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    (tmp_path / "nyx/deleted.py").unlink()
    (tmp_path / "nyx/new.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "nyx/skipped.py").write_text("x\n", encoding="utf-8")
    assert git_listing(tmp_path) == {"nyx/kept.py", "nyx/new.py", "scripts/tool.py"}
