"""Tests that specification validation is isolated from its environment."""
from __future__ import annotations

import ast
import hashlib
import io
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from spec_fixtures import VALID_SPEC_V3

from nyx import spec_validation

MODULES = ("spec_header", "spec_body", "spec_validation")
WRITE_MODE_CHARACTERS = set("wax+")
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
OS_MUTATIONS = (
    "mkdir",
    "makedirs",
    "unlink",
    "remove",
    "rmdir",
    "removedirs",
    "rename",
    "renames",
    "replace",
    "symlink",
    "link",
    "chmod",
    "chown",
    "truncate",
    "ftruncate",
    "write",
    "writev",
    "mknod",
    "mkfifo",
    "fchmod",
    "fchown",
    "utime",
    "lchown",
    "chflags",
    "lchmod",
    "sendfile",
    "copy_file_range",
    "pwrite",
    "pwritev",
    "posix_fallocate",
)


class Forbidden(BaseException):
    """Raised by a guard so that no validator handler can swallow it."""


def _refuse(*_args, **_kwargs):
    raise Forbidden("validation attempted a forbidden operation")


class _EnvironmentGuard:
    def __getattr__(self, name):
        raise Forbidden(f"validation read os.environ.{name}")

    def __getitem__(self, key):
        raise Forbidden("validation read os.environ")

    def __contains__(self, key):
        raise Forbidden("validation read os.environ")

    def __iter__(self):
        raise Forbidden("validation read os.environ")

    def __len__(self):
        raise Forbidden("validation read os.environ")


def _snapshot(root: Path) -> list[tuple[object, ...]]:
    entries: list[tuple[object, ...]] = []
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        entries.append(
            (path.relative_to(root).as_posix(), info.st_size, digest, info.st_mtime_ns)
        )
    return entries


def _installed_guards(*, environment: bool) -> ExitStack:
    """Reject every write path, and optionally environment and home reads."""
    stack = ExitStack()
    real_open = io.open
    real_os_open = os.open

    def checked_open(file, mode="r", *args, **kwargs):
        if isinstance(file, int) is False and WRITE_MODE_CHARACTERS & set(str(mode)):
            raise Forbidden(f"validation opened a file for writing: {mode!r}")
        return real_open(file, mode, *args, **kwargs)

    def checked_os_open(path, flags, *args, **kwargs):
        if flags & WRITE_FLAGS:
            raise Forbidden("validation opened a path with write flags")
        return real_os_open(path, flags, *args, **kwargs)

    stack.enter_context(patch("builtins.open", checked_open))
    stack.enter_context(patch("io.open", checked_open))
    stack.enter_context(patch("os.open", checked_os_open))
    for name in OS_MUTATIONS:
        if hasattr(os, name):
            stack.enter_context(patch.object(os, name, _refuse))
    if environment:
        stack.enter_context(patch.object(os, "environ", _EnvironmentGuard()))
        stack.enter_context(patch.object(Path, "home", _refuse))
    return stack


class ValidatorIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        base = Path(self.temp_dir.name)
        self.root = base / "Library"
        self.unrelated = base / "Elsewhere"
        self.unrelated.mkdir()
        self.package = self.root / "Aurora" / "Queue" / "group" / "feature"
        self.package.mkdir(parents=True)
        self.spec = self.package / "spec.md"
        # A Queue package that lacks closure approval: it is valid unless the
        # validator resolves the Queue lifecycle from the fixture's own root.
        repo = base / "repository"
        repo.mkdir()
        (repo / "AGENTS.md").write_text("rules\n", encoding="utf-8")
        self.spec.write_text(
            VALID_SPEC_V3.replace("Target repo: /tmp/repo", f"Target repo: {repo}")
            .replace("Closure: approved", "Closure: pending"),
            encoding="utf-8",
        )

    def test_environment_and_home_never_influence_the_result(self) -> None:
        for function, target in (
            (spec_validation.validate_package, self.package),
            (spec_validation.validate_spec, self.spec),
        ):
            with self.subTest(function=function.__name__):
                with _installed_guards(environment=False):
                    expected = function(target, self.unrelated)
                own_root = function(target, self.root)
                if function is spec_validation.validate_package:
                    self.assertNotEqual(expected, own_root)
                with patch.dict(os.environ, {"SPEC_ROOT": str(self.root)}):
                    with _installed_guards(environment=True):
                        actual = function(target, self.unrelated)
                self.assertEqual(expected, actual)

    def test_validation_writes_nothing_anywhere(self) -> None:
        for function, target in (
            (spec_validation.validate_package, self.package),
            (spec_validation.validate_spec, self.spec),
        ):
            with self.subTest(function=function.__name__):
                before = _snapshot(Path(self.temp_dir.name))
                with _installed_guards(environment=True):
                    function(target, self.root)
                self.assertEqual(before, _snapshot(Path(self.temp_dir.name)))

    def test_write_guards_reject_writes(self) -> None:
        probe = Path(self.temp_dir.name) / "probe.txt"
        with _installed_guards(environment=False):
            with self.assertRaises(Forbidden):
                probe.write_text("x", encoding="utf-8")
            with self.assertRaises(Forbidden):
                open(probe, "a", encoding="utf-8")
            with self.assertRaises(Forbidden):
                os.open(probe, os.O_WRONLY | os.O_CREAT)
            with self.assertRaises(Forbidden):
                os.mkdir(probe)
            self.assertTrue(self.spec.read_text(encoding="utf-8"))
        self.assertFalse(probe.exists())

    def test_modules_import_only_standard_library_and_each_other(self) -> None:
        # The guards patch attributes of os, io and builtins, so a name bound at
        # import time would escape them. The imports are therefore a closed
        # list: plain, unaliased imports of modules that neither read the
        # environment nor write, and an exact list of from-imports.
        internal = {f"nyx.{name}" for name in MODULES} | set(MODULES)
        plain = {"re", "uuid", "json"}
        from_imports = {
            ("__future__", "annotations"),
            ("dataclasses", "dataclass"),
            ("dataclasses", "field"),
            ("pathlib", "Path"),
            ("collections", "Counter"),
        }
        for name in MODULES:
            path = Path(spec_validation.__file__).with_name(f"{name}.py")
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    self.assertNotIn(node.id, {"__import__", "eval", "exec"}, name)
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertIsNone(alias.asname, (name, alias.name))
                        self.assertTrue(
                            alias.name in plain or alias.name in internal,
                            (name, alias.name),
                        )
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level or module in internal:
                        # Internal imports: relative or nyx-qualified.
                        self.assertTrue(
                            module in internal
                            or (
                                node.level
                                and not module
                                and all(a.name in internal for a in node.names)
                            ),
                            (name, module),
                        )
                        continue
                    for alias in node.names:
                        self.assertIsNone(alias.asname, (name, alias.name))
                        self.assertIn((module, alias.name), from_imports, name)
                        self.assertIn(module.split(".")[0], sys.stdlib_module_names)

    def test_non_package_folders_reject_a_package_only_when_named(self) -> None:
        for function, target in (
            (spec_validation.validate_package, self.package),
            (spec_validation.validate_spec, self.spec),
        ):
            with self.subTest(function=function.__name__):
                named = function(target, self.root, frozenset({"Aurora"}))
                self.assertTrue(any("package must be under" in e for e in named), named)
                self.assertFalse(
                    any("package must be under" in e for e in function(target, self.root))
                )


if __name__ == "__main__":
    unittest.main()
