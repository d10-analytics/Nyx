#!/usr/bin/env python3
"""Build the private standalone desktop application and its worker helper.

The driver resolves the committed GUI deployment configuration for the current
checkout and interpreter, builds a standalone GUI and a private
console-capable worker helper that reuses the shared catalog worker entry, and
stages both under ``dist/desktop``.  Intermediate freezer output stays under
``build/desktop`` so a build never writes outside those two trees, and the
staged manifest records the selected build inputs and artifact digests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SPEC_FILE = REPOSITORY_ROOT / "pysidedeploy.spec"
DIST_ROOT = REPOSITORY_ROOT / "dist" / "desktop"
BUILD_ROOT = REPOSITORY_ROOT / "build" / "desktop"

APPLICATION_TITLE = "Nyx"
# macOS keeps the executable beside the served package.  A bare ``Nyx`` name
# would collide with the ``nyx`` package directory on its case-insensitive
# filesystem, so the bundle executable uses a distinct name while the staged
# bundle keeps the application title.
MACOS_GUI_ENTRY_NAME = "NyxApp"
WORKER_EXECUTABLE = "NyxWorker"
WORKER_STAGE_DIRECTORY = "NyxWorker"
STATIC_MEMBER = Path("nyx") / "static" / "app.js"
ARTIFACT_MANIFEST = "artifact.json"

PINNED_PYSIDE6 = "6.11.2"
PINNED_NUITKA = "4.1.1"

_SPEC_TOKENS = (
    "@PROJECT_DIR@",
    "@INPUT_FILE@",
    "@EXEC_DIRECTORY@",
    "@PYTHON_PATH@",
    "@STATIC_SOURCE@",
    "@PLATFORM_ARGS@",
)

_GUI_ENTRY = '''\
"""Generated standalone GUI entry.  Not part of the source package."""

from nyx.desktop import main

raise SystemExit(main())
'''

_WORKER_ENTRY = '''\
"""Generated standalone worker entry.  Not part of the source package."""

from nyx.desktop_worker import main

raise SystemExit(main())
'''


class BuildError(RuntimeError):
    """The standalone artifact could not be built or staged deterministically."""


def _log(message: str) -> None:
    print(f"[build_desktop] {message}", flush=True)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BuildError(message)


def _run(command: list[str], *, cwd: Path) -> None:
    _log("run: " + " ".join(command))
    subprocess.run(command, cwd=str(cwd), check=True)


def _read_component_versions() -> dict[str, str]:
    from importlib import metadata

    def installed(name: str) -> str:
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError as error:  # pragma: no cover - host setup
            raise BuildError(f"the pinned build input {name} is not installed") from error

    versions = {
        "pyside6": installed("PySide6"),
        "shiboken6": installed("shiboken6"),
        "nuitka": installed("Nuitka"),
        "python": platform.python_version(),
    }
    if versions["pyside6"] != PINNED_PYSIDE6:
        raise BuildError(
            f"PySide6 {versions['pyside6']} does not match the pinned {PINNED_PYSIDE6}"
        )
    if versions["nuitka"] != PINNED_NUITKA:
        raise BuildError(
            f"Nuitka {versions['nuitka']} does not match the pinned {PINNED_NUITKA}"
        )
    return versions


def _platform_arguments() -> str:
    if sys.platform == "win32":
        return (
            "--windows-console-mode=disable"
            " --experimental=force-dependencies-pefile"
        )
    if sys.platform == "darwin":
        return ""
    raise BuildError("standalone desktop artifacts are built only on Windows or macOS")


def _gui_entry_name() -> str:
    return MACOS_GUI_ENTRY_NAME if sys.platform == "darwin" else APPLICATION_TITLE


def _render_spec(values: dict[str, str]) -> Path:
    text = SPEC_FILE.read_text(encoding="utf-8")
    for token in _SPEC_TOKENS:
        _require(token in text, f"deployment configuration is missing {token}")
        text = text.replace(token, values[token])
    for line in text.splitlines():
        _require("@", f"deployment configuration kept an unresolved placeholder: {line!r}")
    rendered = BUILD_ROOT / SPEC_FILE.name
    rendered.write_text(text, encoding="utf-8")
    return rendered


def _find_standalone_component(root: Path, name: str, *, executable: bool) -> Path:
    suffix = ".exe" if sys.platform == "win32" and executable else ""
    candidates = [path for path in root.glob(f"**/{name}{suffix}") if path.is_file()]
    _require(
        len(candidates) == 1,
        f"expected exactly one {name}{suffix} under {root}, found {candidates}",
    )
    return candidates[0]


def _deploy_command(spec_path: Path) -> list[str]:
    script_name = "pyside6-deploy.exe" if sys.platform == "win32" else "pyside6-deploy"
    beside_interpreter = Path(sys.executable).parent / script_name
    located = shutil.which("pyside6-deploy")
    launcher = str(beside_interpreter) if beside_interpreter.is_file() else located
    if launcher is None:
        raise BuildError("the pyside6-deploy deployment tool is not available")
    return [launcher, "-c", str(spec_path), "-f"]


def _build_gui(spec_path: Path) -> Path:
    staged = BUILD_ROOT / "staged"
    shutil.rmtree(staged, ignore_errors=True)
    _run(_deploy_command(spec_path), cwd=REPOSITORY_ROOT)
    if sys.platform == "darwin":
        produced = staged / f"{APPLICATION_TITLE}.app"
        _require(produced.is_dir(), f"the macOS app bundle was not produced at {produced}")
        destination = DIST_ROOT / produced.name
        shutil.move(str(produced), str(destination))
        return destination
    produced = staged / f"{APPLICATION_TITLE}.dist"
    _require(produced.is_dir(), f"the standalone directory was not produced at {produced}")
    destination = DIST_ROOT / APPLICATION_TITLE
    shutil.move(str(produced), str(destination))
    return destination


def _build_worker() -> Path:
    entry = BUILD_ROOT / "worker_entry.py"
    entry.write_text(_WORKER_ENTRY, encoding="utf-8")
    helper_output = BUILD_ROOT / "helper"
    command = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--quiet",
        f"--output-dir={helper_output}",
        f"--output-filename={WORKER_EXECUTABLE}",
        str(entry),
    ]
    if sys.platform == "win32":
        command.extend(
            [
                "--windows-console-mode=force",
                "--experimental=force-dependencies-pefile",
            ]
        )
    _run(command, cwd=REPOSITORY_ROOT)
    return _find_standalone_component(helper_output, WORKER_EXECUTABLE, executable=True)


def _stage_worker(application: Path, worker_executable: Path) -> Path:
    if sys.platform == "darwin":
        destination = application / "Contents" / "MacOS" / WORKER_STAGE_DIRECTORY
    else:
        destination = application / WORKER_STAGE_DIRECTORY
    _require(not destination.exists(), f"worker staging location already exists: {destination}")
    shutil.copytree(worker_executable.parent, destination)
    staged = destination / worker_executable.name
    _require(staged.is_file(), f"the staged worker helper is missing at {staged}")
    return staged


def _application_root(application: Path) -> Path:
    if sys.platform == "darwin":
        return application / "Contents" / "MacOS"
    return application


def _verify_resources(application: Path) -> Path:
    matches = [
        path
        for path in application.glob(f"**/{STATIC_MEMBER.as_posix()}")
        if path.is_file()
    ]
    _require(
        len(matches) == 1,
        f"expected exactly one board resource under {application}, found {matches}",
    )
    return matches[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    return path.relative_to(REPOSITORY_ROOT).as_posix()


def build() -> dict[str, Any]:
    try:
        import nyx  # noqa: F401 - the source package must be importable to freeze
    except ImportError as error:
        raise BuildError("the nyx package is not importable in the build interpreter") from error

    versions = _read_component_versions()
    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(DIST_ROOT, ignore_errors=True)
    DIST_ROOT.mkdir(parents=True)

    gui_entry_name = _gui_entry_name()
    gui_entry = BUILD_ROOT / f"{gui_entry_name}.py"
    gui_entry.write_text(_GUI_ENTRY, encoding="utf-8")
    spec_path = _render_spec(
        {
            "@PROJECT_DIR@": str(REPOSITORY_ROOT),
            "@INPUT_FILE@": str(gui_entry),
            "@EXEC_DIRECTORY@": str(BUILD_ROOT / "staged"),
            "@PYTHON_PATH@": sys.executable,
            # The deployment tool splits extra arguments with a POSIX shell
            # lexer, so the data source must stay free of Windows separators.
            "@STATIC_SOURCE@": "nyx/static",
            "@PLATFORM_ARGS@": _platform_arguments(),
        }
    )

    application = _build_gui(spec_path)
    worker_executable = _build_worker()
    worker = _stage_worker(application, worker_executable)
    resource = _verify_resources(application)

    executable = _find_standalone_component(
        _application_root(application), gui_entry_name, executable=True
    )
    manifest = {
        "schema": "nyx-desktop-artifact/1",
        "platform": sys.platform,
        "architecture": platform.machine(),
        "application": _relative(executable),
        "worker": _relative(worker),
        "resource": _relative(resource),
        "build_inputs": {
            key: value for key, value in sorted(versions.items())
        },
        "sha256": {
            "application": _sha256(executable),
            "worker": _sha256(worker),
            "resource": _sha256(resource),
        },
    }
    (DIST_ROOT / ARTIFACT_MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        manifest = build()
    except (BuildError, subprocess.CalledProcessError) as error:
        print(f"[build_desktop] failed: {error}", file=sys.stderr, flush=True)
        return 1
    except FileNotFoundError as error:
        print(f"[build_desktop] failed: {error}", file=sys.stderr, flush=True)
        return 1
    print("NYX_DESKTOP_BUILD=" + json.dumps(manifest, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
