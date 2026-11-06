"""Shadow-module and marked-copy fixtures for launch-directory import proofs.

A shadow is a regular ``nyx`` package plus a top-level ``json`` module planted
in one directory.  Every shadow module appends its own name to a marker file
that lives outside that directory, so any child process that imports a shadow
instead of the installed package leaves an observable trace.  ``json`` is
shadowed because ``nyx`` imports it while loading, which exposes a child whose
import path reaches the directory even when the package itself is resolved
elsewhere.

A marked copy is the package this process loaded, copied into a directory that
no site folder contains; its ``__init__`` records the importing process's own
command line and the copy's root, so a marker shows which processes loaded
that copy and from which import root.
"""

from __future__ import annotations

import ast
import os
import shutil
from pathlib import Path

SHADOW_MODULES = ("nyx/__init__.py", "nyx/runtime.py", "nyx/worker.py", "json.py")


def plant_shadow(directory: Path, marker: Path) -> None:
    """Write the shadow package and ``json`` module into ``directory``.

    The marker must be outside ``directory`` so that planting and reading it
    never changes what the directory offers to an import path.
    """

    directory = Path(directory)
    marker = Path(marker)
    if marker.parent.resolve() == directory.resolve():
        raise ValueError("the shadow marker must live outside the shadow directory")
    (directory / "nyx").mkdir(parents=True, exist_ok=True)
    for relative in SHADOW_MODULES:
        name = relative.removesuffix(".py").removesuffix("/__init__").replace("/", ".")
        # Only builtins are used so the shadow cannot recurse through another
        # shadowed module before it records the import.
        source = (
            f"with open({str(marker)!r}, 'a', encoding='utf-8') as _stream:\n"
            f"    _stream.write({name!r} + '\\n')\n"
        )
        (directory / relative).write_text(source, encoding="utf-8")


def shadow_imports(marker: Path) -> list[str]:
    """Return the shadow modules that were imported, in import order."""

    try:
        return Path(marker).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def plant_marked_copy(directory: Path, marker: Path) -> Path:
    """Copy the executing ``nyx`` package under ``directory`` and mark imports.

    The copy sits outside every site folder, so only an import path naming the
    returned root reaches it.  Its ``__init__`` starts with a builtins-only
    append of the importing process's command line and that root to ``marker``,
    which records every process that loaded the copy.  The marker must be
    outside ``directory`` so writing it never changes what the copy offers to
    an import path.
    """

    import nyx as executing_package

    directory = Path(directory).resolve()
    marker = Path(marker)
    if marker.parent.resolve() == directory:
        raise ValueError("the marked-copy marker must live outside the copy's root")
    target = directory / "nyx"
    shutil.copytree(
        Path(executing_package.__file__).resolve().parent,
        target,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    init = target / "__init__.py"
    record = (
        f"with open({str(marker)!r}, 'a', encoding='utf-8') as _stream:\n"
        f"    _stream.write(repr(sys.orig_argv) + '\\t' + {str(directory)!r} + '\\n')\n"
    )
    # ``sys`` is a builtin module, so the record is written before any other
    # module of the copy can resolve through a separator-bearing import path.
    init.write_text(f"import sys\n{record}{init.read_text(encoding='utf-8')}", encoding="utf-8")
    return directory


def marked_copy_imports(marker: Path) -> list[tuple[tuple[str, ...], Path]]:
    """Return the recorded command line and root of each marked-copy import."""

    try:
        lines = Path(marker).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    records: list[tuple[tuple[str, ...], Path]] = []
    for line in lines:
        invocation, _, root = line.partition("\t")
        records.append((tuple(ast.literal_eval(invocation)), Path(root)))
    return records


def proof_environment(home: Path, *, pythonpath: str | None = None) -> dict[str, str]:
    """Build a child environment that resolves Nyx state under ``home``.

    The environment is the current one without ``PYTHONPATH`` or
    ``PYTHONHOME``, with ``HOME`` and ``USERPROFILE`` replaced.  No
    ``sitecustomize`` is installed: preloading ``nyx`` there would resolve the
    package before the launch directory joins the import path.  ``pythonpath``
    sets an explicit ``PYTHONPATH`` for proofs that need one.
    """

    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    if pythonpath is not None:
        environment["PYTHONPATH"] = pythonpath
    return environment


def install_proof_environment(monkeypatch, home: Path, *, pythonpath: str | None = None) -> None:
    """Apply ``proof_environment`` to this process through ``monkeypatch``."""

    environment = proof_environment(home, pythonpath=pythonpath)
    for key in ("PYTHONPATH", "PYTHONHOME"):
        if key not in environment:
            monkeypatch.delenv(key, raising=False)
    for key in ("HOME", "USERPROFILE", "PYTHONPATH"):
        if key in environment:
            monkeypatch.setenv(key, environment[key])
