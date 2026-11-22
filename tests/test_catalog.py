import dataclasses
import inspect
import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import pytest

from nyx import catalog
from nyx.models import (
    MAX_TEXT_UNITS,
    SCHEMA_VERSION,
    CatalogEntry,
    _text_units,
    canonical_digest,
    parse_catalog,
)

V1 = """# Example
Status: approved
Closure: approved
Sanity Recommendation: IMPLEMENT
Human Sanity Decision: AFFIRMED
Target repo: /fictional/repo
"""

PROGRAM_ID = "88888888-8888-4888-8888-888888888888"
SMALL_ORACLE_BYTES = "{\"catalog_digest\":\"4c97c34bdcbeb7278ca748282d013da4ccd79d8e61d164fb94805f79f553ab2b\",\"configuration_revision\":null,\"discovery_diagnostics\":[],\"entries\":[{\"declared\":{\"status\":null,\"target_project\":null,\"title\":\"One\"},\"package_id\":null,\"package_path\":\"Fictional/Queue/one\",\"project\":\"Fictional\",\"relationship\":{\"prerequisites\":[],\"program\":{\"program_id\":null,\"resolution\":\"not_declared\",\"title\":null}},\"reported_fields\":[],\"stage\":\"Queue\"}],\"identity_coverage\":{\"diagnostics\":[],\"state\":\"complete\"},\"inventory\":{\"projects\":[{\"availability\":\"complete\",\"name\":\"Fictional\"}],\"stages\":[{\"availability\":\"complete\",\"project\":\"Fictional\",\"stage\":\"Queue\"}]},\"program_coverage\":{\"diagnostics\":[],\"state\":\"complete\"},\"programs\":[],\"schema_version\":9,\"terminal_stages\":[]}"
COMPLETE_ORACLE_BYTES = "{\"catalog_digest\":\"0e80871a38999787027f5390b0305aee0c5a31d83fcabf67c1c55168818f003a\",\"configuration_revision\":null,\"discovery_diagnostics\":[],\"entries\":[{\"declared\":{\"status\":null,\"target_project\":null,\"title\":\"Archive\"},\"package_id\":\"77777777-7777-4777-8777-777777777777\",\"package_path\":\"Fictional/Archive/pkg\",\"project\":\"Fictional\",\"relationship\":{\"prerequisites\":[],\"program\":{\"program_id\":null,\"resolution\":\"not_declared\",\"title\":null}},\"reported_fields\":[],\"stage\":\"Archive\"},{\"declared\":{\"status\":null,\"target_project\":null,\"title\":\"Retro\"},\"package_id\":\"55555555-5555-4555-8555-555555555555\",\"package_path\":\"Fictional/Awaiting_Retrospective/pkg\",\"project\":\"Fictional\",\"relationship\":{\"prerequisites\":[],\"program\":{\"program_id\":null,\"resolution\":\"not_declared\",\"title\":null}},\"reported_fields\":[],\"stage\":\"Awaiting_Retrospective\"},{\"declared\":{\"status\":null,\"target_project\":null,\"title\":\"Done\"},\"package_id\":\"66666666-6666-4666-8666-666666666666\",\"package_path\":\"Fictional/Done/pkg\",\"project\":\"Fictional\",\"relationship\":{\"prerequisites\":[],\"program\":{\"program_id\":null,\"resolution\":\"not_declared\",\"title\":null}},\"reported_fields\":[],\"stage\":\"Done\"},{\"declared\":{\"status\":null,\"target_project\":null,\"title\":\"Progress\"},\"package_id\":\"33333333-3333-4333-8333-333333333333\",\"package_path\":\"Fictional/In_Progress/pkg\",\"project\":\"Fictional\",\"relationship\":{\"prerequisites\":[],\"program\":{\"program_id\":null,\"resolution\":\"not_declared\",\"title\":null}},\"reported_fields\":[],\"stage\":\"In_Progress\"},{\"declared\":{\"status\":null,\"target_project\":null,\"title\":\"Fix\"},\"package_id\":\"44444444-4444-4444-8444-444444444444\",\"package_path\":\"Fictional/Needs_Fixes/pkg\",\"project\":\"Fictional\",\"relationship\":{\"prerequisites\":[],\"program\":{\"program_id\":null,\"resolution\":\"not_declared\",\"title\":null}},\"reported_fields\":[],\"stage\":\"Needs_Fixes\"},{\"declared\":{\"status\":null,\"target_project\":null,\"title\":\"Queue\"},\"package_id\":\"22222222-2222-4222-8222-222222222222\",\"package_path\":\"Fictional/Queue/pkg\",\"project\":\"Fictional\",\"relationship\":{\"prerequisites\":[{\"reason\":\"unresolved\",\"target_package_id\":null},{\"reason\":\"resolved\",\"target_package_id\":\"33333333-3333-4333-8333-333333333333\"}],\"program\":{\"program_id\":\"88888888-8888-4888-8888-888888888888\",\"resolution\":\"resolved\",\"title\":\"Core\"}},\"reported_fields\":[],\"stage\":\"Queue\"},{\"declared\":{\"status\":null,\"target_project\":null,\"title\":\"Under\"},\"package_id\":\"11111111-1111-4111-8111-111111111111\",\"package_path\":\"Fictional/Under_Development/pkg\",\"project\":\"Fictional\",\"relationship\":{\"prerequisites\":[],\"program\":{\"program_id\":\"88888888-8888-4888-8888-888888888888\",\"resolution\":\"resolved\",\"title\":\"Core\"}},\"reported_fields\":[],\"stage\":\"Under_Development\"}],\"identity_coverage\":{\"diagnostics\":[],\"state\":\"complete\"},\"inventory\":{\"projects\":[{\"availability\":\"complete\",\"name\":\"Fictional\"}],\"stages\":[{\"availability\":\"complete\",\"project\":\"Fictional\",\"stage\":\"Archive\"},{\"availability\":\"complete\",\"project\":\"Fictional\",\"stage\":\"Awaiting_Retrospective\"},{\"availability\":\"complete\",\"project\":\"Fictional\",\"stage\":\"Done\"},{\"availability\":\"complete\",\"project\":\"Fictional\",\"stage\":\"In_Progress\"},{\"availability\":\"complete\",\"project\":\"Fictional\",\"stage\":\"Needs_Fixes\"},{\"availability\":\"complete\",\"project\":\"Fictional\",\"stage\":\"Queue\"},{\"availability\":\"complete\",\"project\":\"Fictional\",\"stage\":\"Under_Development\"}]},\"program_coverage\":{\"diagnostics\":[],\"state\":\"complete\"},\"programs\":[{\"member_package_ids\":[\"11111111-1111-4111-8111-111111111111\",\"22222222-2222-4222-8222-222222222222\"],\"program_id\":\"88888888-8888-4888-8888-888888888888\",\"title\":\"Core\"}],\"schema_version\":9,\"terminal_stages\":[]}"


def package(root: Path, stage: str, name: str, content: str = V1) -> Path:
    path = root / "Fictional" / stage / name
    path.mkdir(parents=True)
    path.joinpath("spec.md").write_text(content, encoding="utf-8")
    return path


def link_directory(link: Path, target: Path) -> None:
    """Create a real directory link, including a native Windows junction."""
    if sys.platform == "win32":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise OSError(result.stderr or result.stdout)
    else:
        link.symlink_to(target, target_is_directory=True)


HEADER_SEPARATORS = (
    b"\r", b"\n", b"\r\n", b"\x0b", b"\x0c", b"\x1c", b"\x1d", b"\x1e",
    b"\xc2\x85", b"\xe2\x80\xa8", b"\xe2\x80\xa9",
)


def package_bytes(root: Path, stage: str, name: str, content: bytes) -> Path:
    path = root / "Fictional" / stage / name
    path.mkdir(parents=True)
    path.joinpath("spec.md").write_bytes(content)
    return path


def program_descriptor(root: Path, program_id: str, content: bytes) -> Path:
    path = root / "Fictional" / "Reference" / "Programs" / program_id
    path.mkdir(parents=True)
    path.joinpath("program.md").write_bytes(content)
    return path


def header_with_cut(offset: int, package_id: str, prefix: bytes = b"") -> bytes:
    """Return item bytes whose byte-level ``## `` cut starts at ``offset``."""
    core = b"# T\nPackage ID: " + package_id.encode() + b"\nStatus: s\n"
    body_length = offset - len(prefix)
    return prefix + core + b"x" * (body_length - len(core) - 1) + b"\n## B\n\xe9"


def header_without_cut(length: int, package_id: str, prefix: bytes = b"") -> bytes:
    core = b"# T\nPackage ID: " + package_id.encode() + b"\nStatus: s\n"
    return prefix + core + b"x" * (length - len(prefix) - len(core))


def entry_by_path(value: dict, package_path: str) -> dict:
    return next(entry for entry in value["entries"] if entry["package_path"] == package_path)


class RecordingReader:
    """Wrap a real opened stream and record each read's size and end position."""

    def __init__(self, stream, reads: list) -> None:
        self._stream = stream
        self._reads = reads

    def read(self, *args):
        data = self._stream.read(*args)
        self._reads.append((args[0] if args else None, self._stream.tell()))
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._stream.close()
        return False


class AppendingReader:
    """Wrap a real opened stream and append to the file during each read."""

    def __init__(self, stream, path: Path, payload: bytes) -> None:
        self._stream = stream
        self._path = path
        self._payload = payload

    def read(self, *args):
        data = self._stream.read(*args)
        with open(self._path, "ab") as sink:
            sink.write(self._payload)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._stream.close()
        return False


class RewritingReader:
    """Wrap a real opened stream and rewrite the file once after it closes."""

    def __init__(self, stream, path: Path, content: bytes) -> None:
        self._stream = stream
        self._path = path
        self._content = content

    def read(self, *args):
        return self._stream.read(*args)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._stream.close()
        # The built-in ``open`` keeps this write out of the patched
        # ``catalog.Path.open`` call count.
        with open(self._path, "wb") as sink:
            sink.write(self._content)
        return False


class CatalogTests(TestCase):
    def test_public_producers_use_one_portable_scanner_and_reject_root_links(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            outside = base / "outside"
            outside.mkdir()
            (outside / "outside-marker").write_text("outside", encoding="utf-8")
            root = base / "specs"
            link_directory(root, outside)
            for producer in (catalog.build_catalog, catalog.scan_catalog):
                with self.subTest(producer=producer.__name__):
                    with self.assertRaisesRegex(ValueError, "specification root cannot be read"):
                        producer(root)

    def test_discovered_links_are_excluded_before_every_structural_admission(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "specs"
            package(root, "Queue", "literal", "# Literal\n")
            outside = base / "outside"
            outside.mkdir()
            (outside / "outside-marker").write_text("outside-marker", encoding="utf-8")
            repository = root / "Fictional"

            link_directory(root / "LinkedProject", outside)
            link_directory(repository / "LinkedStage", outside)
            link_directory(repository / "Queue" / "linked-group", outside)
            link_directory(repository / "Queue" / "linked-package", outside)
            reference = repository / "Reference"
            reference.mkdir()
            link_directory(reference / "Programs", outside)

            ref_link_repo = root / "ReferenceLink"
            ref_link_repo.mkdir()
            link_directory(ref_link_repo / "Reference", outside)
            uuid_link_repo = root / "UuidLink"
            uuid_programs = uuid_link_repo / "Reference" / "Programs"
            uuid_programs.mkdir(parents=True)
            link_directory(uuid_programs / PROGRAM_ID, outside)
            program_anchor_repo = root / "ProgramAnchor"
            (program_anchor_repo / "Queue" / "member").mkdir(parents=True)
            (program_anchor_repo / "Queue" / "member" / "spec.md").write_text(
                f"# Member\nProgram Membership: {PROGRAM_ID}\n", encoding="utf-8"
            )
            program_anchor_dir = program_anchor_repo / "Reference" / "Programs" / PROGRAM_ID
            program_anchor_dir.mkdir(parents=True)
            program_anchor_dir.joinpath("program.md").symlink_to(outside / "outside-marker")

            anchor_link = package(root, "Queue", "anchor-link", "# Anchor\n")
            anchor_link.joinpath("spec.md").unlink()
            anchor_link.joinpath("spec.md").symlink_to(outside / "outside-marker")

            for producer in (catalog.build_catalog, catalog.scan_catalog):
                with self.subTest(producer=producer.__name__):
                    value = json.loads(producer(root))
                    rendered = json.dumps(value)
                    self.assertNotIn("outside-marker", rendered)
                    paths = [entry["package_path"] for entry in value["entries"]]
                    self.assertIn("Fictional/Queue/literal", paths)
                    self.assertNotIn("Fictional/Queue/linked-package", paths)
                    anchor_entry = next(
                        entry for entry in value["entries"]
                        if entry["package_path"] == "Fictional/Queue/anchor-link"
                    )
                    self.assertIn({"code": "nonregular_anchor", "message": "nonregular anchor: " + anchor_entry["package_path"]}, value["identity_coverage"]["diagnostics"])
                    self.assertTrue(value["discovery_diagnostics"])

    def test_anchor_links_nonregular_unreadable_and_changed_reads_are_bounded(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "specs"
            link_package = package(root, "Queue", "linked", "# Linked\n")
            nonregular = package(root, "Queue", "fifo", "# FIFO\n")
            unreadable = package(root, "Queue", "unreadable", "# Unreadable\n")
            package(root, "Queue", "changed", "# Changed\n")
            outside = base / "outside.md"
            outside.write_text("# Outside\noutside-marker\n", encoding="utf-8")
            link_package.joinpath("spec.md").unlink()
            link_package.joinpath("spec.md").symlink_to(outside)
            nonregular.joinpath("spec.md").unlink()

            if sys.platform == "win32":
                nonregular.joinpath("spec.md").mkdir()
                original_open = catalog.Path.open

                def deny_unreadable(path, *args, **kwargs):
                    if path == unreadable / "spec.md":
                        raise PermissionError("injected unreadable anchor")
                    return original_open(path, *args, **kwargs)

                open_patch = patch.object(
                    catalog.Path,
                    "open",
                    autospec=True,
                    side_effect=deny_unreadable,
                )
            else:
                os.mkfifo(nonregular / "spec.md")
                unreadable.joinpath("spec.md").chmod(0)
                open_patch = nullcontext()

            changed_info = (root / "Fictional" / "Queue" / "changed" / "spec.md").lstat()
            changed_key = catalog._catalog_identity(changed_info)
            original_identity = catalog._catalog_identity
            identity_count = 0

            def changed_identity(info):
                nonlocal identity_count
                identity = original_identity(info)
                if identity != changed_key:
                    return identity
                identity_count += 1
                return (identity_count, 1, 1, 1, 1, 1, 1)

            with open_patch, patch.object(catalog, "_catalog_identity", side_effect=changed_identity):
                values = [
                    json.loads(producer(root))
                    for producer in (catalog.build_catalog, catalog.scan_catalog)
                ]
            for value in values:
                self.assertIn({"code": "nonregular_anchor", "message": "nonregular anchor: Fictional/Queue/fifo"}, value["identity_coverage"]["diagnostics"])
                self.assertIn({"code": "unreadable_anchor", "message": "unreadable anchor: Fictional/Queue/unreadable"}, value["identity_coverage"]["diagnostics"])
                self.assertIn({"code": "changed_during_read", "message": "changed during read: Fictional/Queue/changed"}, value["identity_coverage"]["diagnostics"])
                self.assertNotIn("outside-marker", json.dumps(value))
            unreadable.joinpath("spec.md").chmod(0o600)

    def test_both_public_producers_retain_fixed_schema_four_oracle_without_writes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package(root, "Queue", "one", "# One\n")
            ignored = root / "Fictional" / "Queue" / "one" / ".pipeline"
            ignored.mkdir()
            outside = root / "outside-marker"
            outside.write_text("outside", encoding="utf-8")
            ignored.joinpath("link").symlink_to(outside)

            def snapshot() -> list[tuple[object, ...]]:
                result = []
                for path in sorted(root.rglob("*")):
                    info = path.lstat()
                    if path.is_symlink():
                        kind = "symlink"
                        payload: object = os.readlink(path)
                    elif path.is_dir():
                        kind = "directory"
                        payload = None
                    elif path.is_file():
                        kind = "file"
                        payload = path.read_bytes()
                    else:
                        kind = "other"
                        payload = None
                    result.append(
                        (
                            path.relative_to(root).as_posix(),
                            kind,
                            info.st_mode,
                            info.st_uid,
                            info.st_gid,
                            info.st_size,
                            info.st_mtime_ns,
                            info.st_ctime_ns,
                            payload,
                        )
                    )
                return result

            before = snapshot()
            original_open = os.open

            def reject_writes(path, flags, mode=0o777, *, dir_fd=None):
                self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
                return original_open(path, flags, mode, dir_fd=dir_fd)

            def reject_write_operation(*_args, **_kwargs):
                raise AssertionError("catalog attempted a write operation")

            with patch.object(catalog.os, "open", side_effect=reject_writes), patch.object(
                catalog.os, "mkdir", side_effect=reject_write_operation
            ), patch.object(catalog.os, "unlink", side_effect=reject_write_operation), patch.object(
                catalog.os, "rename", side_effect=reject_write_operation
            ), patch.object(catalog.os, "replace", side_effect=reject_write_operation), patch.object(
                catalog.os, "symlink", side_effect=reject_write_operation
            ), patch.object(catalog.os, "chmod", side_effect=reject_write_operation), patch.object(
                catalog.os, "truncate", side_effect=reject_write_operation
            ), patch.object(catalog.os, "write", side_effect=reject_write_operation), patch.object(
                catalog.os, "mknod", side_effect=reject_write_operation, create=True
            ), patch.object(catalog.os, "link", side_effect=reject_write_operation), patch.object(
                catalog.os, "rmdir", side_effect=reject_write_operation
            ), patch.object(catalog.os, "remove", side_effect=reject_write_operation), patch.object(
                catalog.os, "fchmod", side_effect=reject_write_operation, create=True
            ), patch.object(catalog.os, "ftruncate", side_effect=reject_write_operation), patch.object(
                catalog.os, "utime", side_effect=reject_write_operation
            ):
                self.assertEqual(SMALL_ORACLE_BYTES, catalog.build_catalog(root))
                self.assertEqual(SMALL_ORACLE_BYTES, catalog.scan_catalog(root))
            after = snapshot()
            self.assertEqual(before, after)

    def test_both_public_producers_match_fixed_complete_graph_schema_four_oracle(self) -> None:
        with TemporaryDirectory() as temporary:
            root = make_baseline_graph(Path(temporary))
            self.assertEqual(COMPLETE_ORACLE_BYTES, catalog.build_catalog(root))
            self.assertEqual(COMPLETE_ORACLE_BYTES, catalog.scan_catalog(root))

    def test_catalog_is_deterministic_and_declared_only(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package(root, "Queue", "zeta", V1.replace("# Example", "# Ω"))
            package(root, "Under_Development", "alpha", "# Alpha\nbody secret")
            first = catalog.scan_catalog(root)
            second = catalog.scan_catalog(root)
            self.assertEqual(first, second)
            value = json.loads(first)
            self.assertEqual(["Fictional/Queue/zeta", "Fictional/Under_Development/alpha"],
                             [entry["package_path"] for entry in value["entries"]])
            self.assertEqual(SCHEMA_VERSION, value["schema_version"])
            self.assertEqual(
                {"projects": [{"name": "Fictional", "availability": "complete"}],
                 "stages": [
                     {"project": "Fictional", "stage": stage, "availability": "complete"}
                     for stage in ("Queue", "Under_Development")
                 ]},
                value["inventory"],
            )
            self.assertEqual([], value["terminal_stages"])
            self.assertEqual("Ω", value["entries"][0]["declared"]["title"])
            self.assertNotIn("body secret", first)
            digest_input = dict(value)
            digest_input.pop("catalog_digest")
            expected = catalog.sha256(
                json.dumps(digest_input, ensure_ascii=True, sort_keys=True,
                          separators=(",", ":")).encode()
            ).hexdigest()
            self.assertEqual(expected, value["catalog_digest"])

    def test_discovered_inventory_admits_custom_and_empty_dimensions_without_following_controls(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "specs"
            root.mkdir()
            package(root, "Testing", "custom", "# Custom\n")
            (root / "Fictional" / "Empty").mkdir()
            (root / "Fictional" / "Empty" / ".gitkeep").write_text("", encoding="utf-8")
            (root / "EmptyProject").mkdir()
            (root / "EmptyProject" / ".gitkeep").write_text("", encoding="utf-8")
            (root / "Fictional" / "Reference").mkdir()
            (root / "Fictional" / ".pipeline").mkdir()
            (root / "Fictional" / "direct-file").write_text("ignored", encoding="utf-8")
            outside = Path(temporary) / "outside"
            outside.mkdir()
            (root / "Fictional" / "Escaped").symlink_to(outside, target_is_directory=True)

            rendered = catalog.scan_catalog(root)
            value = json.loads(rendered)
            assert value["inventory"] == {
                "projects": [
                    {"name": "EmptyProject", "availability": "complete"},
                    {"name": "Fictional", "availability": "complete"},
                ],
                "stages": [
                    {"project": "Fictional", "stage": "Empty", "availability": "complete"},
                    {"project": "Fictional", "stage": "Testing", "availability": "complete"},
                ],
            }
            assert [entry["package_path"] for entry in value["entries"]] == [
                "Fictional/Testing/custom"
            ]
            parsed = parse_catalog(rendered)
            assert parsed.inventory == value["inventory"]
            assert "outside" not in rendered

    def test_symlinked_project_and_stage_candidates_emit_bounded_diagnostics(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "specs"
            (root / "Fictional" / "Queue").mkdir(parents=True)
            outside = base / "outside"
            outside.mkdir()
            (root / "Fictional" / "Link").symlink_to(outside, target_is_directory=True)
            (root / "LinkProject").symlink_to(outside, target_is_directory=True)

            value = json.loads(catalog.scan_catalog(root))

            assert value["inventory"] == {
                "projects": [{"name": "Fictional", "availability": "complete"}],
                "stages": [
                    {"project": "Fictional", "stage": "Queue", "availability": "complete"}
                ],
            }
            assert [item["code"] for item in value["discovery_diagnostics"]] == [
                "discovery_unavailable",
                "discovery_unavailable",
            ]
            assert {
                item["message"] for item in value["discovery_diagnostics"]
            } <= {
                "discovery unavailable: .",
                "discovery unavailable: Fictional",
            }
            assert "outside" not in json.dumps(value)

    def test_markdown_links_at_every_level_preserve_complete_relationships(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "specs"
            target_id = "22222222-2222-4222-8222-222222222222"
            source_id = "11111111-1111-4111-8111-111111111111"
            package(
                root,
                "Queue",
                "source",
                f"# Source\nPackage ID: {source_id}\n"
                f"Completion Prerequisite: {target_id}\n",
            )
            package(
                root,
                "Done",
                "target",
                f"# Target\nPackage ID: {target_id}\n",
            )

            group = root / "Fictional" / "Done" / "group"
            group.mkdir()
            links = (
                root / "AGENTS.md",
                root / "Fictional" / "notes.md",
                root / "Fictional" / "Queue" / "guide.md",
                group / "notes.md",
            )
            for index, link in enumerate(links):
                outside = base / f"outside-{index}"
                (outside / "escaped").mkdir(parents=True)
                (outside / "escaped" / "spec.md").write_text(
                    f"# Escaped link {index}\n", encoding="utf-8"
                )
                link_directory(link, outside)

            value = json.loads(catalog.scan_catalog(root))
            entries = {entry["package_path"]: entry for entry in value["entries"]}
            source = entries["Fictional/Queue/source"]
            target = entries["Fictional/Done/target"]
            edge = source["relationship"]["prerequisites"][0]

            self.assertEqual(
                {"state": "complete", "diagnostics": []}, value["identity_coverage"]
            )
            self.assertEqual(
                {
                    "projects": [{"name": "Fictional", "availability": "complete"}],
                    "stages": [
                        {"project": "Fictional", "stage": "Done", "availability": "complete"},
                        {"project": "Fictional", "stage": "Queue", "availability": "complete"},
                    ],
                },
                value["inventory"],
            )
            self.assertEqual("complete", value["program_coverage"]["state"])
            self.assertEqual([], value["discovery_diagnostics"])
            self.assertEqual(
                {"Fictional/Done/target", "Fictional/Queue/source"},
                set(entries),
            )
            self.assertEqual("Source", source["declared"]["title"])
            self.assertEqual("Target", target["declared"]["title"])
            self.assertEqual(set(source["relationship"]), {"prerequisites", "program"})
            self.assertEqual(set(target["relationship"]), {"prerequisites", "program"})
            self.assertEqual(
                {"target_package_id": target_id, "reason": "resolved"}, edge
            )
            self.assertNotIn("Escaped", json.dumps(value))

    def test_document_link_names_are_literal_at_every_discovery_level(self) -> None:
        for name in ("notes.md", "CLAUDE.md", ".md", "notes.MD", "notes.md.txt", "notes"):
            for level in ("root", "project", "stage", "grouping"):
                for target_kind in ("directory", "file"):
                    with self.subTest(name=name, level=level, target=target_kind):
                        with TemporaryDirectory() as temporary:
                            base = Path(temporary)
                            root = base / "specs"
                            package(root, "Queue", "item", "# Item\n")
                            group = root / "Fictional" / "Queue" / "group"
                            group.mkdir()
                            parents = {
                                "root": root,
                                "project": root / "Fictional",
                                "stage": root / "Fictional" / "Queue",
                                "grouping": group,
                            }
                            link = parents[level] / name
                            outside = base / "outside"
                            if target_kind == "directory":
                                (outside / "escaped").mkdir(parents=True)
                                (outside / "escaped" / "spec.md").write_text(
                                    "# Escaped directory\n", encoding="utf-8"
                                )
                                link_directory(link, outside)
                            else:
                                outside.write_text("# Escaped file\n", encoding="utf-8")
                                link.symlink_to(outside)

                            value = json.loads(catalog.scan_catalog(root))
                            quiet = name in {"notes.md", "CLAUDE.md", ".md"}
                            diagnostic_path = {
                                "root": ".",
                                "project": "Fictional",
                                "stage": f"Fictional/Queue/{name}",
                                "grouping": f"Fictional/Queue/group/{name}",
                            }[level]
                            diagnostics = [] if quiet else [{
                                "code": "discovery_unavailable",
                                "message": f"discovery unavailable: {diagnostic_path}",
                            }]
                            availability = (
                                "incomplete"
                                if not quiet and level in {"stage", "grouping"}
                                else "complete"
                            )
                            self.assertEqual(diagnostics, value["discovery_diagnostics"])
                            self.assertEqual(
                                {
                                    "state": "complete" if quiet else "incomplete",
                                    "diagnostics": diagnostics,
                                },
                                value["identity_coverage"],
                            )
                            self.assertEqual(
                                {
                                    "projects": [{"name": "Fictional", "availability": availability}],
                                    "stages": [{
                                        "project": "Fictional", "stage": "Queue",
                                        "availability": availability,
                                    }],
                                },
                                value["inventory"],
                            )
                            self.assertEqual(
                                {"Fictional/Queue/item"},
                                {entry["package_path"] for entry in value["entries"]},
                            )
                            self.assertNotIn("Escaped", json.dumps(value))

    def test_unsafe_document_link_names_keep_their_discovery_diagnostic(self) -> None:
        if sys.platform == "win32":
            self.skipTest("control characters cannot appear in Windows file names")
        name = "bad\x01.md"
        for level in ("root", "project"):
            for target_kind in ("directory", "file"):
                with self.subTest(name=name, level=level, target=target_kind):
                    with TemporaryDirectory() as temporary:
                        base = Path(temporary)
                        root = base / "specs"
                        package(root, "Queue", "item", "# Item\n")
                        parent = {"root": root, "project": root / "Fictional"}[level]
                        link = parent / name
                        outside = base / "outside"
                        if target_kind == "directory":
                            (outside / "escaped").mkdir(parents=True)
                            (outside / "escaped" / "spec.md").write_text(
                                "# Escaped directory\n", encoding="utf-8"
                            )
                            link_directory(link, outside)
                        else:
                            outside.write_text("# Escaped file\n", encoding="utf-8")
                            link.symlink_to(outside)
                        self.assertTrue(link.is_symlink())

                        value = json.loads(catalog.scan_catalog(root))
                        diagnostic_path = {"root": ".", "project": "Fictional"}[level]
                        diagnostics = [{
                            "code": "discovery_unavailable",
                            "message": f"discovery unavailable: {diagnostic_path}",
                        }]
                        self.assertEqual(diagnostics, value["discovery_diagnostics"])
                        self.assertEqual(
                            {"state": "incomplete", "diagnostics": diagnostics},
                            value["identity_coverage"],
                        )
                        self.assertEqual(
                            {
                                "projects": [{"name": "Fictional", "availability": "complete"}],
                                "stages": [{
                                    "project": "Fictional", "stage": "Queue",
                                    "availability": "complete",
                                }],
                            },
                            value["inventory"],
                        )
                        self.assertEqual(
                            {"Fictional/Queue/item"},
                            {entry["package_path"] for entry in value["entries"]},
                        )
                        self.assertNotIn("Escaped", json.dumps(value))

    def test_spec_links_at_root_and_project_are_documents_not_anchors(self) -> None:
        for level in ("root", "project"):
            for target_kind in ("directory", "file"):
                with self.subTest(name="spec.md", level=level, target=target_kind):
                    with TemporaryDirectory() as temporary:
                        base = Path(temporary)
                        root = base / "specs"
                        package(root, "Queue", "item", "# Item\n")
                        (root / "Fictional" / "Queue" / "group").mkdir()
                        parent = root if level == "root" else root / "Fictional"
                        link = parent / "spec.md"
                        outside = base / "outside"
                        if target_kind == "directory":
                            (outside / "escaped").mkdir(parents=True)
                            (outside / "escaped" / "spec.md").write_text(
                                "# Escaped directory\n", encoding="utf-8"
                            )
                            link_directory(link, outside)
                        else:
                            outside.write_text("# Escaped file\n", encoding="utf-8")
                            link.symlink_to(outside)

                        value = json.loads(catalog.scan_catalog(root))
                        self.assertEqual([], value["discovery_diagnostics"])
                        self.assertEqual(
                            {"state": "complete", "diagnostics": []},
                            value["identity_coverage"],
                        )
                        self.assertEqual(
                            {
                                "projects": [{"name": "Fictional", "availability": "complete"}],
                                "stages": [{
                                    "project": "Fictional", "stage": "Queue",
                                    "availability": "complete",
                                }],
                            },
                            value["inventory"],
                        )
                        self.assertEqual(
                            {"Fictional/Queue/item"},
                            {entry["package_path"] for entry in value["entries"]},
                        )
                        self.assertNotIn("Escaped", json.dumps(value))

    def test_regular_workspace_link_names_remain_ordinary_candidates(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "specs"
            fixtures = (
                ("CLAUDE.md", "Queue", "claude"),
                ("CODEX.md", "Queue", "codex"),
                ("Fictional", "template_spec.md", "template"),
                ("Fictional", "Queue", "guide.md/pkg"),
            )
            for project, stage, name in fixtures:
                package_path = root / project / stage / name
                package_path.mkdir(parents=True)
                package_path.joinpath("spec.md").write_text(
                    f"# {name}\n", encoding="utf-8"
                )

            value = json.loads(catalog.scan_catalog(root))

            self.assertEqual(
                [
                    {"name": "CLAUDE.md", "availability": "complete"},
                    {"name": "CODEX.md", "availability": "complete"},
                    {"name": "Fictional", "availability": "complete"},
                ],
                value["inventory"]["projects"],
            )
            self.assertEqual(
                {
                    "CLAUDE.md/Queue/claude",
                    "CODEX.md/Queue/codex",
                    "Fictional/template_spec.md/template",
                    "Fictional/Queue/guide.md/pkg",
                },
                {entry["package_path"] for entry in value["entries"]},
            )
            self.assertEqual([], value["discovery_diagnostics"])

    def test_unrecognized_catalog_symlinks_remain_untraversed_and_incomplete(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "specs"
            package(root, "Queue", "valid", "# Valid\n")
            member_id = "33333333-3333-4333-8333-333333333333"
            package(
                root,
                "Queue",
                "member",
                f"# Member\nProgram Membership: {member_id}\n",
            )

            outside_project = base / "outside-project"
            (outside_project / "Queue" / "escaped").mkdir(parents=True)
            (outside_project / "Queue" / "escaped" / "spec.md").write_text(
                "# Escaped project\n", encoding="utf-8"
            )
            (root / "UnexpectedProject").symlink_to(
                outside_project, target_is_directory=True
            )

            outside_stage = base / "outside-stage"
            (outside_stage / "escaped").mkdir(parents=True)
            (outside_stage / "escaped" / "spec.md").write_text(
                "# Escaped stage\n", encoding="utf-8"
            )
            (root / "Fictional" / "UnexpectedStage").symlink_to(
                outside_stage, target_is_directory=True
            )

            outside_group = base / "outside-group"
            (outside_group / "escaped").mkdir(parents=True)
            (outside_group / "escaped" / "spec.md").write_text(
                "# Escaped group\n", encoding="utf-8"
            )
            (root / "Fictional" / "Queue" / "group").symlink_to(
                outside_group, target_is_directory=True
            )

            programs = root / "Fictional" / "Reference" / "Programs"
            programs.mkdir(parents=True)
            outside_program = base / "outside-program"
            outside_program.mkdir()
            (outside_program / "program.md").write_text(
                f"Program ID: {member_id}\nProgram Title: Escaped\n",
                encoding="utf-8",
            )
            (programs / member_id).symlink_to(outside_program, target_is_directory=True)

            anchor_id = "44444444-4444-4444-8444-444444444444"
            anchor_program = programs / anchor_id
            anchor_program.mkdir(parents=True)
            outside_anchor = base / "outside-program.md"
            outside_anchor.write_text(
                f"Program ID: {anchor_id}\nProgram Title: Escaped anchor\n",
                encoding="utf-8",
            )
            anchor_program.joinpath("program.md").symlink_to(outside_anchor)

            linked_package = root / "Fictional" / "Queue" / "linked-package"
            linked_package.mkdir()
            outside_spec = base / "outside-spec.md"
            outside_spec.write_text("# Escaped spec\n", encoding="utf-8")
            linked_package.joinpath("spec.md").symlink_to(outside_spec)

            value = json.loads(catalog.scan_catalog(root))
            diagnostics = {
                item["message"] for item in value["discovery_diagnostics"]
            }
            self.assertEqual(
                {
                    "discovery unavailable: .",
                    "discovery unavailable: Fictional",
                    "discovery unavailable: Fictional/Queue/group",
                },
                diagnostics,
            )
            self.assertEqual("incomplete", value["identity_coverage"]["state"])
            self.assertIn(
                "discovery unavailable: Fictional/Queue/group",
                {item["message"] for item in value["identity_coverage"]["diagnostics"]},
            )
            self.assertEqual("incomplete", value["program_coverage"]["state"])
            program_diagnostics = {
                item["message"] for item in value["program_coverage"]["diagnostics"]
            }
            self.assertIn(
                f"invalid package: Fictional/Reference/Programs/{member_id}",
                program_diagnostics,
            )
            self.assertIn(
                f"nonregular anchor: Fictional/Reference/Programs/{anchor_id}",
                program_diagnostics,
            )
            entries = {entry["package_path"]: entry for entry in value["entries"]}
            self.assertNotIn("UnexpectedProject", " ".join(entries))
            self.assertNotIn("UnexpectedStage", " ".join(entries))
            self.assertNotIn("/group", " ".join(entries))
            self.assertEqual([], value["programs"])
            self.assertIn("Fictional/Queue/linked-package", entries)
            self.assertEqual(
                [{"code": "nonregular_anchor", "message":
                  "nonregular anchor: Fictional/Queue/linked-package"}],
                [item for item in value["identity_coverage"]["diagnostics"] if item["message"].endswith(": Fictional/Queue/linked-package")],
            )
            self.assertNotIn("Escaped", json.dumps(value))

    def test_schema_four_inventory_mutations_are_rejected_before_use(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package(root, "Testing", "custom", "# Custom\n")
            (root / "Fictional" / "Empty").mkdir()
            baseline = json.loads(catalog.scan_catalog(root))

        mutations = (
            lambda value: value.update(schema_version=3),
            lambda value: value.update(schema_version=4),
            lambda value: value.update(schema_version=5),
            lambda value: value["inventory"].update(extra=[]),
            lambda value: value["inventory"].update(projects={}),
            lambda value: value["inventory"]["projects"].append(value["inventory"]["projects"][0]),
            lambda value: value["inventory"]["stages"].reverse(),
            lambda value: value["inventory"]["stages"][0].update(availability="unknown"),
            lambda value: value["inventory"]["stages"][0].update(project="Missing"),
            lambda value: value["entries"][0].update(stage="Missing"),
            lambda value: value.update(catalog_digest="0" * 64),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value = json.loads(json.dumps(baseline))
                mutate(value)
                if value.get("catalog_digest") != "0" * 64:
                    value["catalog_digest"] = canonical_digest(value)
                with self.assertRaises(ValueError):
                    parse_catalog(value)

    def test_stage_root_anchor_round_trips_through_schema_four_parser(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "Fictional" / "Queue"
            stage.mkdir(parents=True)
            stage.joinpath("spec.md").write_text(V1, encoding="utf-8")
            rendered = catalog.scan_catalog(root)
            value = json.loads(rendered)
            assert [entry["package_path"] for entry in value["entries"]] == ["Fictional/Queue"]
            assert parse_catalog(rendered).entries[0].package_path == "Fictional/Queue"

    def test_relationships_are_ordered_and_digest_tracks_declared_changes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            target_id = "22222222-2222-4222-8222-222222222222"
            source_id = "11111111-1111-4111-8111-111111111111"
            package(root, "Queue", "source",
                    f"# Source\nPackage ID: {source_id}\n"
                    f"Completion Prerequisite: {target_id}\n")
            package(root, "Done", "target", f"# Target\nPackage ID: {target_id}\n")
            first = json.loads(catalog.scan_catalog(root))
            source = next(entry for entry in first["entries"]
                          if entry["package_path"].endswith("/source"))
            edge = source["relationship"]["prerequisites"][0]
            self.assertEqual(
                {"target_package_id": target_id, "reason": "resolved"}, edge
            )
            original_digest = first["catalog_digest"]
            anchor = root / "Fictional" / "Queue" / "source" / "spec.md"
            anchor.write_text(anchor.read_text(encoding="utf-8").replace("# Source", "# Changed"),
                              encoding="utf-8")
            changed = json.loads(catalog.scan_catalog(root))
            self.assertNotEqual(original_digest, changed["catalog_digest"])
            changed_source = next(entry for entry in changed["entries"]
                                  if entry["package_path"].endswith("/source"))
            self.assertEqual("Changed", changed_source["declared"]["title"])

    def test_missing_and_ambiguous_targets_are_safe(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            target_id = "22222222-2222-4222-8222-222222222222"
            source_id = "11111111-1111-4111-8111-111111111111"
            package(root, "Queue", "source",
                    f"# Source\nPackage ID: {source_id}\n"
                    f"Completion Prerequisite: {target_id}\n")
            package(root, "Done", "target-a", f"# A\nPackage ID: {target_id}\n")
            package(root, "Archive", "target-b", f"# B\nPackage ID: {target_id}\n")
            value = json.loads(catalog.scan_catalog(root))
            edge = next(entry for entry in value["entries"]
                        if entry["package_path"].endswith("/source"))["relationship"]["prerequisites"][0]
            self.assertEqual(
                {"target_package_id": target_id, "reason": "unresolved"}, edge
            )

    def test_malformed_and_unreadable_anchors_are_partial_without_payload(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            malformed = package(root, "Queue", "malformed", "# Malformed\nPackage ID: nope\n")
            unreadable = package(root, "Queue", "unreadable", V1)
            unreadable_anchor = unreadable / "spec.md"
            if sys.platform == "win32":
                original_open = catalog.Path.open

                def deny_unreadable(path, *args, **kwargs):
                    if path == unreadable_anchor:
                        raise PermissionError("injected unreadable anchor")
                    return original_open(path, *args, **kwargs)

                open_patch = patch.object(
                    catalog.Path,
                    "open",
                    autospec=True,
                    side_effect=deny_unreadable,
                )
            else:
                unreadable_anchor.chmod(0)
                open_patch = nullcontext()
            with open_patch:
                value = json.loads(catalog.scan_catalog(root))
            if sys.platform != "win32":
                unreadable_anchor.chmod(0o600)
            entries = {entry["package_path"]: entry for entry in value["entries"]}
            malformed_entry = entries["Fictional/Queue/malformed"]
            self.assertIsNone(malformed_entry["package_id"])
            self.assertEqual(
                {"prerequisites": [], "program": {
                    "program_id": None, "title": None, "resolution": "not_declared"}},
                malformed_entry["relationship"],
            )
            self.assertIn({"code": "unreadable_anchor", "message": "unreadable anchor: Fictional/Queue/unreadable"}, value["identity_coverage"]["diagnostics"])
            self.assertNotIn(str(malformed), json.dumps(value))

    def test_output_bound_is_enforced(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package(root, "Queue", "one", V1)
            with patch.object(catalog, "MAX_OUTPUT_BYTES", 10), self.assertRaisesRegex(
                catalog.CatalogOutputTooLargeError, "exceeds 2 MiB"
            ) as raised:
                catalog.scan_catalog(root)
            self.assertIsInstance(raised.exception, ValueError)

    def test_catalog_only_does_not_lookup_declared_target_checkout(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = Path(temporary) / "product-checkout"
            content = V1.replace("/fictional/repo", str(target))
            package(root, "Queue", "one", content)
            original_exists = Path.exists

            def guarded_exists(path: Path) -> bool:
                if path == target:
                    raise AssertionError("catalog looked up target checkout")
                return original_exists(path)

            with patch.object(Path, "exists", guarded_exists):
                value = json.loads(catalog.scan_catalog(root))
            self.assertEqual("product-checkout", value["entries"][0]["declared"]["target_project"])

    def test_missing_prerequisite_is_unresolved_without_target_payload(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_id = "11111111-1111-4111-8111-111111111111"
            target_id = "22222222-2222-4222-8222-222222222222"
            package(
                root,
                "Queue",
                "source",
                f"# Source\nPackage ID: {source_id}\n"
                f"Completion Prerequisite: {target_id}\n",
            )

            value = json.loads(catalog.scan_catalog(root))
            edge = value["entries"][0]["relationship"]["prerequisites"][0]
            self.assertEqual(
                {"target_package_id": target_id, "reason": "unresolved"}, edge
            )

    def test_public_api_has_one_builder_and_one_scanner(self) -> None:
        from nyx import __all__

        self.assertEqual(["build_catalog", "scan_catalog"], __all__)
        for producer in (catalog.build_catalog, catalog.scan_catalog):
            signature = inspect.signature(producer)
            rendered = str(signature)
            self.assertEqual(
                "(spec_root: 'Path', *, completed_stage_names: 'Iterable[str] | None' = None, configuration_revision: 'str | None' = None) -> 'str'",
                rendered,
            )
            self.assertEqual(
                ["spec_root", "completed_stage_names", "configuration_revision"], list(signature.parameters)
            )
            self.assertEqual(
                inspect.Parameter.KEYWORD_ONLY,
                signature.parameters["completed_stage_names"].kind,
            )
            self.assertIs(
                None, signature.parameters["completed_stage_names"].default
            )

    def test_public_entry_points_share_the_canonical_builder(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package(root, "Queue", "one", V1)
            with patch.object(catalog, "_build_catalog", wraps=catalog._build_catalog) as builder:
                catalog.build_catalog(root)
                catalog.scan_catalog(root)
            self.assertEqual(2, builder.call_count)
            self.assertEqual([root, root], [call.args[0] for call in builder.call_args_list])

    def test_fixed_lifecycle_projection_has_retained_digest_and_wire_bytes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ids = [f"{value:08d}-0000-4000-8000-{value:012d}" for value in range(1, 8)]
            stages = (
                "Under_Development", "Queue", "In_Progress", "Needs_Fixes",
                "Awaiting_Retrospective", "Done", "Archive",
            )
            for stage, package_id in zip(stages, ids, strict=True):
                package(root, stage, stage.lower(), f"# {stage}\nPackage ID: {package_id}\n")
            queue_anchor = root / "Fictional" / "Queue" / "queue" / "spec.md"
            queue_anchor.write_text(
                f"# Queue\nPackage ID: {ids[1]}\nCompletion Prerequisite: {ids[5]}\n",
                encoding="utf-8",
            )
            target_anchor = root / "Fictional" / "Done" / "done" / "spec.md"
            target_anchor.write_text(
                f"# Done\nPackage ID: {ids[5]}\n",
                encoding="utf-8",
            )
            def inventory() -> list[tuple[object, ...]]:
                result = []
                for path in [root, *sorted(root.rglob("*"))]:
                    info = path.lstat()
                    if path.is_symlink():
                        kind = "symlink"
                        payload: object = os.readlink(path)
                    elif path.is_dir():
                        kind = "directory"
                        payload = None
                    elif path.is_file():
                        kind = "file"
                        payload = path.read_bytes()
                    else:
                        kind = "other"
                        payload = None
                    result.append(
                        (
                            "." if path == root else path.relative_to(root).as_posix(),
                            kind,
                            info.st_mode,
                            info.st_uid,
                            info.st_gid,
                            info.st_size,
                            info.st_mtime_ns,
                            info.st_ctime_ns,
                            payload,
                        )
                    )
                return result

            original_open = os.open
            renders: list[str] = []
            for producer in (catalog.build_catalog, catalog.scan_catalog):
                with self.subTest(producer=producer.__name__):
                    before = inventory()

                    def reject_writes(path, flags, mode=0o777, *, dir_fd=None):
                        self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
                        return original_open(path, flags, mode, dir_fd=dir_fd)

                    def reject_write_operation(*_args, **_kwargs):
                        raise AssertionError("catalog attempted a write operation")

                    with ExitStack() as stack:
                        scans = stack.enter_context(
                            patch.object(
                                catalog,
                                "_catalog_scandir",
                                wraps=catalog._catalog_scandir,
                            )
                        )
                        stack.enter_context(patch.object(catalog.os, "open", side_effect=reject_writes))
                        for name in (
                            "mkdir", "unlink", "rename", "replace", "symlink", "chmod",
                            "truncate", "write", "mknod", "link", "rmdir", "remove",
                            "fchmod", "ftruncate", "utime",
                        ):
                            if not hasattr(catalog.os, name):
                                continue
                            stack.enter_context(
                                patch.object(catalog.os, name, side_effect=reject_write_operation)
                            )
                        rendered = producer(root)

                    self.assertEqual(16, scans.call_count)
                    renders.append(rendered)
                    value = json.loads(rendered)
                    self.assertEqual(SCHEMA_VERSION, value["schema_version"])
                    digest_input = dict(value)
                    digest_input.pop("catalog_digest")
                    self.assertEqual(
                        catalog.sha256(json.dumps(digest_input, sort_keys=True,
                                                   separators=(",", ":")).encode()).hexdigest(),
                        value["catalog_digest"],
                    )
                    self.assertEqual(
                        set(stages),
                        {entry["stage"] for entry in value["entries"]},
                    )
                    self.assertEqual(
                        [
                            "Fictional/Archive/archive",
                            "Fictional/Awaiting_Retrospective/awaiting_retrospective",
                            "Fictional/Done/done",
                            "Fictional/In_Progress/in_progress",
                            "Fictional/Needs_Fixes/needs_fixes",
                            "Fictional/Queue/queue",
                            "Fictional/Under_Development/under_development",
                        ],
                        [entry["package_path"] for entry in value["entries"]],
                    )
                    self.assertTrue(all("board_visible" not in entry for entry in value["entries"]))
                    self.assertIn("Fictional/Done/done", {entry["package_path"] for entry in value["entries"]})
                    queue = next(entry for entry in value["entries"] if entry["stage"] == "Queue")
                    self.assertEqual(
                        {"target_package_id": ids[5], "reason": "resolved"},
                        queue["relationship"]["prerequisites"][0],
                    )
                    self.assertEqual(before, inventory())

            self.assertEqual(renders[0].encode("utf-8"), renders[1].encode("utf-8"))

    def test_all_stages_and_member_programs_are_emitted_without_policy(self) -> None:
        with TemporaryDirectory() as temporary:
            root = make_baseline_graph(Path(temporary))
            orphan_id = "99999999-9999-4999-8999-999999999999"
            orphan = root / "Fictional" / "Reference" / "Programs" / orphan_id
            orphan.mkdir()
            (orphan / "program.md").write_text(
                f"Program ID: {orphan_id}\nProgram Title: Orphan\n", encoding="utf-8"
            )
            rendered = [producer(root) for producer in (catalog.build_catalog, catalog.scan_catalog)]
            self.assertEqual(rendered[0], rendered[1])
            value = json.loads(rendered[0])
            stages = ("Archive", "Awaiting_Retrospective", "Done", "In_Progress",
                      "Needs_Fixes", "Queue", "Under_Development")
            self.assertEqual(
                [f"Fictional/{stage}/pkg" for stage in stages],
                [entry["package_path"] for entry in value["entries"]],
            )
            self.assertEqual([ORACLE_PROGRAM_ID], [program["program_id"] for program in value["programs"]])
            self.assertEqual(value, parse_catalog(value).as_dict())

def test_terminal_stages_track_completion_without_removing_entries():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        package(root, "Done", "target", "# Done item\n")
        package(root, "Queue", "source", "# Queue item\n")
        no_policy = json.loads(catalog.scan_catalog(root))
        assert no_policy["terminal_stages"] == []
        for policy, expected in ((["Done", "Archive"], ["Archive", "Done"]),
                                 (["Done", "Done"], []), (["bad/name"], [])):
            finished = json.loads(catalog.scan_catalog(root, completed_stage_names=policy))
            assert finished["terminal_stages"] == expected
            assert finished["entries"] == no_policy["entries"]
            assert [entry["stage"] for entry in finished["entries"]] == ["Done", "Queue"]


ORACLE_IDS = [
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
    "44444444-4444-4444-8444-444444444444",
    "55555555-5555-4555-8555-555555555555",
    "66666666-6666-4666-8666-666666666666",
    "77777777-7777-4777-8777-777777777777",
]
ORACLE_PROGRAM_ID = "88888888-8888-4888-8888-888888888888"

def make_baseline_graph(root: Path) -> Path:
    repository = root / "Fictional"
    stages = (
        "Under_Development", "Queue", "In_Progress", "Needs_Fixes",
        "Awaiting_Retrospective", "Done", "Archive",
    )
    contents = (
        f"# Under\nPackage ID: {ORACLE_IDS[0]}\nProgram Membership: {ORACLE_PROGRAM_ID}\n",
        f"# Queue\nPackage ID: {ORACLE_IDS[1]}\nProgram Membership: {ORACLE_PROGRAM_ID}\n"
        f"Completion Prerequisite: {ORACLE_IDS[2]}\n"
        "Completion Prerequisite: malformed\n"
        f"Superseded By: {ORACLE_IDS[2]}\n",
        f"# Progress\nPackage ID: {ORACLE_IDS[2]}\n"
        f"Superseded By: {ORACLE_IDS[1]}\n",
        f"# Fix\nPackage ID: {ORACLE_IDS[3]}\n",
        f"# Retro\nPackage ID: {ORACLE_IDS[4]}\n",
        f"# Done\nPackage ID: {ORACLE_IDS[5]}\n",
        f"# Archive\nPackage ID: {ORACLE_IDS[6]}\n",
    )
    for stage, content in zip(stages, contents, strict=True):
        package_path = repository / stage / "pkg"
        package_path.mkdir(parents=True)
        package_path.joinpath("spec.md").write_text(content, encoding="utf-8")
    descriptor = repository / "Reference" / "Programs" / ORACLE_PROGRAM_ID
    descriptor.mkdir(parents=True)
    descriptor.joinpath("program.md").write_text(
        f"Program ID: {ORACLE_PROGRAM_ID}\nProgram Title: Core\n", encoding="utf-8"
    )
    return root

def test_baseline_graph_retains_exact_serialized_bytes_and_relationship_proof():
    with TemporaryDirectory() as temporary:
        root = make_baseline_graph(Path(temporary))
        rendered = catalog.build_catalog(root)
        value = json.loads(rendered)
        assert value["schema_version"] == SCHEMA_VERSION
        assert set(value) == {
            "schema_version", "catalog_digest", "configuration_revision", "terminal_stages", "identity_coverage",
            "program_coverage", "discovery_diagnostics", "entries", "programs", "inventory",
        }
        assert value["inventory"] == {
            "projects": [{"name": "Fictional", "availability": "complete"}],
            "stages": [
                {"project": "Fictional", "stage": stage, "availability": "complete"}
                for stage in ("Archive", "Awaiting_Retrospective", "Done", "In_Progress",
                              "Needs_Fixes", "Queue", "Under_Development")
            ],
        }
        digest_input = dict(value)
        digest_input.pop("catalog_digest")
        assert value["catalog_digest"] == catalog.sha256(
            json.dumps(digest_input, ensure_ascii=True, sort_keys=True,
                       separators=(",", ":")).encode()
        ).hexdigest()
        assert [entry["package_path"] for entry in value["entries"]] == [
            "Fictional/Archive/pkg", "Fictional/Awaiting_Retrospective/pkg",
            "Fictional/Done/pkg", "Fictional/In_Progress/pkg",
            "Fictional/Needs_Fixes/pkg", "Fictional/Queue/pkg",
            "Fictional/Under_Development/pkg",
        ]
        assert "Fictional/Archive/pkg" in {entry["package_path"] for entry in value["entries"]}
        queue = next(entry for entry in value["entries"] if entry["stage"] == "Queue")
        assert set(queue["relationship"]) == {"prerequisites", "program"}
        assert queue["relationship"]["prerequisites"] == [
            {"target_package_id": None, "reason": "unresolved"},
            {"target_package_id": ORACLE_IDS[2], "reason": "resolved"},
        ]
        assert queue["relationship"]["program"]["resolution"] == "resolved"
        assert queue["relationship"]["program"]["title"] == "Core"
        assert value["programs"][0]["member_package_ids"] == [ORACLE_IDS[0], ORACLE_IDS[1]]


def test_reported_header_grammar_order_deduplication_and_screening(tmp_path):
    cases = [
        ("Closure: a\nCLOSURE: b\nclosure: c", [{"name": "Closure", "value": "a"}]),
        ("Package id: x\nStatus: s\n**Target repo:** /work/repo\nProgram membership: y", []),
        ("**Closure:** v\n> Note: v\n<!-- X: v -->\n- Item: v\n| Col: v |\n`Code: v`\nhttps://x\n# Heading: v", [{"name": "Closure", "value": "v"}]),
        ("Empty:\nBlank: \t\t\nOwner Note:\nOwner Note: later", [{"name": "Owner Note", "value": "later"}]),
        ("**First**: one\n Second: two\nThird:** three\n## Body\nIgnored: body", [{"name": "First", "value": "one"}, {"name": "Second", "value": "two"}, {"name": "Third", "value": "three"}]),
        ("A" * 64 + ": yes\n" + "B" * 65 + ": no\nBad\tName: no\nXÄ: upper\nXä: lower", [{"name": "A" * 64, "value": "yes"}, {"name": "XÄ", "value": "upper"}, {"name": "Xä", "value": "lower"}]),
    ]
    for index, (header, expected) in enumerate(cases):
        root = tmp_path / str(index)
        package(root, "Queue", "item", "# Example\n" + header)
        value = json.loads(catalog.scan_catalog(root))
        assert value["entries"][0]["reported_fields"] == expected
        assert parse_catalog(value).as_dict() == value
        if index == 1:
            assert value["entries"][0]["declared"]["status"] == "s"
    root = tmp_path / "cap"
    names = [f"Z{i:02}" for i in reversed(range(64))] + ["Aaa Last"]
    package(root, "Queue", "item", "# Cap\n" + "\n".join(f"{name}: {name}" for name in names))
    value = json.loads(catalog.scan_catalog(root))
    assert value["entries"][0]["reported_fields"] == [
        {"name": name, "value": name} for name in sorted(names[:64])
    ]


def test_real_text_screening_truncates_on_code_point_boundary(tmp_path):
    long_value = "startword " + "a" * 1013 + "😀" + "b" * 66 + " tailword"
    assert len(long_value.encode("utf-16-le")) // 2 == 1100
    package(tmp_path, "Queue", "item", "# " + "t" * 1100 + "\nOwner Note: " + long_value + "\nStatus: a\tb\nTarget repo: /work/x\ty\nOther: a\x00b\x7fc\n")
    value = json.loads(catalog.scan_catalog(tmp_path))
    entry = value["entries"][0]
    assert entry["declared"] == {"title": "t" * 1024, "status": "a b", "target_project": "x y"}
    assert entry["reported_fields"] == [{"name": "Other", "value": "a b c"}, {"name": "Owner Note", "value": long_value[:1023]}]
    assert parse_catalog(value).as_dict() == value


def test_core_headers_and_exact_wire_shapes_include_default_relationships(tmp_path):
    from nyx.models import DIAGNOSTIC_CODES

    target_id, source_id, program_id = ORACLE_IDS[:3]
    descriptor = tmp_path / "Fictional" / "Reference" / "Programs" / program_id
    descriptor.mkdir(parents=True)
    (descriptor / "program.md").write_text(f"Program ID: {program_id}\nProgram Title: Core\n", encoding="utf-8")
    package(tmp_path, "Done", "target", f"# Target\nPackage ID: {target_id}\n")
    package(tmp_path, "Queue", "source", f"# Source\nPackage ID: {source_id}\nTarget repo: /work/core\nStatus: ready\nCompletion Prerequisite: {target_id}\nProgram Membership: {program_id}\nSuperseded By: {target_id}\n")
    value = json.loads(catalog.scan_catalog(tmp_path, completed_stage_names=["Done"]))
    source = value["entries"][1]
    assert source["declared"] == {"title": "Source", "target_project": "core", "status": "ready"}
    assert source["package_id"] == source_id
    assert source["relationship"]["prerequisites"] == [
        {"target_package_id": target_id, "reason": "resolved"}
    ]
    assert source["relationship"]["program"] == {"program_id": program_id, "title": "Core", "resolution": "resolved"}
    assert value["entries"][0]["relationship"] == {
        "prerequisites": [],
        "program": {"program_id": None, "title": None, "resolution": "not_declared"},
    }
    package(tmp_path, "Queue", "empty", "")
    invalid = package(tmp_path, "Queue", "bytes", "") / "spec.md"
    invalid.write_bytes(b"\xff")
    unreadable = package(tmp_path, "Queue", "unreadable", "# Hidden") / "spec.md"
    original = catalog._catalog_read_anchor
    with patch.object(catalog, "_catalog_read_anchor", side_effect=lambda path: (None, "unreadable_anchor") if path == unreadable else original(path)):
        value = json.loads(catalog.scan_catalog(tmp_path, completed_stage_names=["Done"]))
    assert len(value["entries"]) == 5
    for entry in value["entries"]:
        assert set(entry) == {"package_id", "package_path", "project", "stage", "declared", "reported_fields", "relationship"}
        assert set(entry["declared"]) == {"title", "target_project", "status"}
        assert entry["reported_fields"] == []
        relationship = entry["relationship"]
        assert set(relationship) == {"prerequisites", "program"}
        for edge in relationship["prerequisites"]:
            assert set(edge) == {"target_package_id", "reason"}
        assert set(relationship["program"]) == {"program_id", "title", "resolution"}
    assert all(set(program) == {"program_id", "title", "member_package_ids"} for program in value["programs"])
    assert DIAGNOSTIC_CODES == set(catalog.CATALOG_DIAGNOSTIC_MESSAGES) == {
        "discovery_unavailable", "invalid_package", "unreadable_anchor", "nonregular_anchor", "changed_during_read", "duplicate_program_id"
    }
    assert parse_catalog(value).as_dict() == value


def test_header_body_byte_and_separator_table_through_both_producers(tmp_path):
    package_id = "11111111-1111-4111-8111-111111111111"
    root = tmp_path / "valid"
    valid = {
        "body-byte": b"# T\nPackage ID: " + package_id.encode() + b"\n## B\n\xe9",
        "cr-only": b"# T\rPackage ID: " + package_id.encode() + b"\r## B\r\xe9",
        "non-separator": (
            b"# T\nStatus: x\xc5\x85## y\nPackage ID: "
            + package_id.encode()
            + b"\n## B\n\xe9"
        ),
    }
    for index, separator in enumerate(HEADER_SEPARATORS):
        valid[f"sep-{index}"] = b"# T\nStatus: s" + separator + b"## B\n\xe9"
    for name, content in valid.items():
        package_bytes(root, "Queue", name, content)

    for producer in (catalog.build_catalog, catalog.scan_catalog):
        value = json.loads(producer(root))
        assert value["identity_coverage"] == {"state": "complete", "diagnostics": []}
        body = entry_by_path(value, "Fictional/Queue/body-byte")
        assert body["declared"]["title"] == "T"
        assert body["package_id"] == package_id
        cr_only = entry_by_path(value, "Fictional/Queue/cr-only")
        assert cr_only["declared"]["title"] == "T"
        assert cr_only["package_id"] == package_id
        non_separator = entry_by_path(value, "Fictional/Queue/non-separator")
        assert non_separator["declared"]["title"] == "T"
        assert non_separator["declared"]["status"] == "x\u0145## y"
        assert non_separator["package_id"] == package_id
        for index in range(len(HEADER_SEPARATORS)):
            entry = entry_by_path(value, f"Fictional/Queue/sep-{index}")
            assert entry["declared"]["title"] == "T"
            assert entry["declared"]["status"] == "s"

    header_root = tmp_path / "header-byte"
    package_bytes(
        header_root,
        "Queue",
        "header-byte",
        b"# T\nPackage ID: " + package_id.encode() + b"\nStatus: \xe9\n## B\n",
    )
    for producer in (catalog.build_catalog, catalog.scan_catalog):
        value = json.loads(producer(header_root))
        assert value["identity_coverage"]["state"] == "incomplete"
        assert {
            "code": "invalid_package",
            "message": "invalid package: Fictional/Queue/header-byte",
        } in value["identity_coverage"]["diagnostics"]


def test_bom_header_and_program_descriptor_through_both_producers(tmp_path):
    package_id = "22222222-2222-4222-8222-222222222222"
    program_id = "99999999-9999-4999-8999-999999999999"
    bom = b"\xef\xbb\xbf"
    root = tmp_path / "specs"
    package_bytes(root, "Queue", "bom-item",
                  bom + b"# T\nPackage ID: " + package_id.encode() + b"\n## B\n")
    package_bytes(root, "Queue", "double-bom",
                  bom + bom + b"# T\nPackage ID: " + package_id.encode() + b"\n## B\n")
    package_bytes(root, "Queue", "bom-cut-first",
                  bom + b"## B\n# T\nPackage ID: " + package_id.encode() + b"\n")
    package_bytes(root, "Queue", "bom-member",
                  b"# Member\nProgram Membership: " + program_id.encode() + b"\n")
    program_descriptor(
        root,
        program_id,
        bom + b"Program ID: " + program_id.encode() + b"\nProgram Title: Core\n",
    )

    for producer in (catalog.build_catalog, catalog.scan_catalog):
        value = json.loads(producer(root))
        bom_item = entry_by_path(value, "Fictional/Queue/bom-item")
        assert bom_item["declared"]["title"] == "T"
        assert bom_item["package_id"] == package_id
        assert entry_by_path(value, "Fictional/Queue/double-bom")["declared"]["title"] is None
        cut_first = entry_by_path(value, "Fictional/Queue/bom-cut-first")
        assert cut_first["declared"]["title"] is None
        assert cut_first["package_id"] is None
        member = entry_by_path(value, "Fictional/Queue/bom-member")
        assert member["relationship"]["program"]["title"] == "Core"
        assert member["relationship"]["program"]["resolution"] == "resolved"
        assert value["program_coverage"] == {"state": "complete", "diagnostics": []}


def test_anchor_header_byte_bound_through_both_producers(tmp_path):
    package_id = "33333333-3333-4333-8333-333333333333"
    bom = b"\xef\xbb\xbf"
    limit = catalog._HEADER_BYTE_LIMIT
    root = tmp_path / "at-limit"
    package_bytes(root, "Queue", "cut-at-limit", header_with_cut(limit, package_id))
    package_bytes(root, "Queue", "no-cut-at-limit", header_without_cut(limit, package_id))
    for producer in (catalog.build_catalog, catalog.scan_catalog):
        value = json.loads(producer(root))
        assert value["identity_coverage"] == {"state": "complete", "diagnostics": []}
        for name in ("cut-at-limit", "no-cut-at-limit"):
            entry = entry_by_path(value, f"Fictional/Queue/{name}")
            assert entry["declared"]["title"] == "T"
            assert entry["package_id"] == package_id

    over_root = tmp_path / "over-limit"
    package_bytes(over_root, "Queue", "cut-over-limit",
                  header_with_cut(limit + 1, package_id))
    package_bytes(over_root, "Queue", "no-cut-over-limit",
                  header_without_cut(limit + 1, package_id))
    package_bytes(over_root, "Queue", "bom-cut-over-limit",
                  header_with_cut(limit + 1, package_id, bom))
    for producer in (catalog.build_catalog, catalog.scan_catalog):
        value = json.loads(producer(over_root))
        assert value["identity_coverage"]["state"] == "incomplete"
        for name in ("cut-over-limit", "no-cut-over-limit", "bom-cut-over-limit"):
            assert {
                "code": "invalid_package",
                "message": f"invalid package: Fictional/Queue/{name}",
            } in value["identity_coverage"]["diagnostics"]


def test_anchor_program_descriptor_bound_through_both_producers(tmp_path):
    program_id = "99999999-9999-4999-8999-999999999999"
    limit = catalog._HEADER_BYTE_LIMIT
    core = b"Program ID: " + program_id.encode() + b"\nProgram Title: Core\n"

    at_limit = tmp_path / "at-limit"
    package_bytes(at_limit, "Queue", "member",
                  b"# Member\nProgram Membership: " + program_id.encode() + b"\n")
    program_descriptor(at_limit, program_id, core + b"\n" * (limit - len(core)))
    for producer in (catalog.build_catalog, catalog.scan_catalog):
        value = json.loads(producer(at_limit))
        assert value["program_coverage"] == {"state": "complete", "diagnostics": []}
        member = entry_by_path(value, "Fictional/Queue/member")
        assert member["relationship"]["program"]["title"] == "Core"
        assert member["relationship"]["program"]["resolution"] == "resolved"

    over_limit = tmp_path / "over-limit"
    package_bytes(over_limit, "Queue", "member",
                  b"# Member\nProgram Membership: " + program_id.encode() + b"\n")
    program_descriptor(over_limit, program_id, core + b"\n" * (limit + 1 - len(core)))
    for producer in (catalog.build_catalog, catalog.scan_catalog):
        value = json.loads(producer(over_limit))
        assert value["program_coverage"]["state"] == "incomplete"
        assert {
            "code": "invalid_package",
            "message": f"invalid package: Fictional/Reference/Programs/{program_id}",
        } in value["program_coverage"]["diagnostics"]
        member = entry_by_path(value, "Fictional/Queue/member")
        assert member["relationship"]["program"]["title"] is None
        assert member["relationship"]["program"]["resolution"] == "unknown"


def test_anchor_changed_during_read_through_stream_wrapper(tmp_path):
    package_id = "44444444-4444-4444-8444-444444444444"
    root = tmp_path / "specs"
    package = package_bytes(
        root,
        "Queue",
        "changing",
        b"# T\nPackage ID: " + package_id.encode() + b"\n## B\n" + b"x" * (32 * 1024 * 1024),
    )
    spec = package / "spec.md"
    original_open = catalog.Path.open

    def appending_open(target, *args, **kwargs):
        stream = original_open(target, *args, **kwargs)
        if Path(target) == spec:
            return AppendingReader(stream, spec, b"\nappended")
        return stream

    with patch.object(catalog.Path, "open", autospec=True, side_effect=appending_open):
        value = json.loads(catalog.build_catalog(root))
    assert {
        "code": "changed_during_read",
        "message": "changed during read: Fictional/Queue/changing",
    } in value["identity_coverage"]["diagnostics"]
    assert value["identity_coverage"]["state"] == "incomplete"


def test_anchor_bounded_read_bytes_through_stream_wrapper(tmp_path):
    package_id = "55555555-5555-4555-8555-555555555555"
    limit = catalog._HEADER_BYTE_LIMIT
    root = tmp_path / "specs"
    package = package_bytes(
        root,
        "Queue",
        "large",
        b"# T\nPackage ID: " + package_id.encode() + b"\n## B\n" + b"x" * (32 * 1024 * 1024),
    )
    spec = package / "spec.md"
    reads: list = []
    original_open = catalog.Path.open

    def recording_open(target, *args, **kwargs):
        stream = original_open(target, *args, **kwargs)
        if Path(target) == spec:
            return RecordingReader(stream, reads)
        return stream

    with patch.object(catalog.Path, "open", autospec=True, side_effect=recording_open):
        data, diagnostic = catalog._catalog_read_anchor(spec)
        value = json.loads(catalog.build_catalog(root))

    assert data is not None
    assert diagnostic is None
    assert reads
    for size, position in reads:
        assert size is not None
        assert size <= limit + 3
        assert position <= limit + 3
    entry = entry_by_path(value, "Fictional/Queue/large")
    assert entry["declared"]["title"] == "T"
    assert entry["package_id"] == package_id


def test_pattern_scan_is_linear_over_long_header_fields(tmp_path):
    spaces = 60000
    program_id = "99999999-9999-4999-8999-999999999999"
    long_value = b"a" + b" " * spaces + b"b"
    root = tmp_path / "specs"
    rows = {
        "completion": b"Completion Prerequisite: " + long_value + b"\n",
        "status": b"Status: " + long_value + b"\n",
        "target": b"Target repo: " + long_value + b"\n",
        "title": b"# " + long_value + b"\n",
    }
    for name, content in rows.items():
        package_bytes(root, "Queue", name, content)
    program_descriptor(
        root,
        program_id,
        b"Program ID: " + program_id.encode() + b"\nProgram Title: " + long_value + b"\n",
    )

    fixtures = [root / "Fictional" / "Queue" / name / "spec.md" for name in rows]
    fixtures.append(
        root / "Fictional" / "Reference" / "Programs" / program_id / "program.md"
    )
    for fixture in fixtures:
        assert fixture.stat().st_size <= catalog._HEADER_BYTE_LIMIT, fixture

    started = time.perf_counter()
    value = json.loads(catalog.build_catalog(root))
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, elapsed

    assert value["identity_coverage"]["state"] == "complete"
    for name in rows:
        assert {
            "code": "invalid_package",
            "message": f"invalid package: Fictional/Queue/{name}",
        } not in value["identity_coverage"]["diagnostics"]


def test_pattern_trailing_whitespace_stripping_table(tmp_path):
    program_id = "99999999-9999-4999-8999-999999999999"
    for index, whitespace in enumerate(("\xa0", "\u3000", "\t", "\x1f")):
        encoded = whitespace.encode("utf-8")
        root = tmp_path / f"ws-{index}"
        package_bytes(
            root,
            "Queue",
            "member",
            b"# Member\nProgram Membership: " + program_id.encode()
            + b"\nTarget repo: /work/x/ " + encoded + b"\n",
        )
        program_descriptor(
            root,
            program_id,
            b"Program ID: " + program_id.encode()
            + b"\nProgram Title: Core" + encoded + b"\n",
        )
        for producer in (catalog.build_catalog, catalog.scan_catalog):
            value = json.loads(producer(root))
            context = (whitespace, producer.__name__)
            assert value["program_coverage"] == {"state": "complete", "diagnostics": []}, context
            member = entry_by_path(value, "Fictional/Queue/member")
            assert member["declared"]["target_project"] == "x", context
            assert member["relationship"]["program"]["resolution"] == "resolved", context
            assert member["relationship"]["program"]["title"] == "Core", context
            assert value["programs"] == [
                {"program_id": program_id, "title": "Core", "member_package_ids": []}
            ], context


def _leaf_package(directory: Path) -> Path:
    """Place one healthy ``item/spec.md`` package inside ``directory``."""
    item = directory / "item"
    item.mkdir()
    item.joinpath("spec.md").write_text("# Item\n", encoding="utf-8")
    return item


def _make_unsafe_directory(parent: Path, name) -> Path:
    """Create one unsafe-named directory under ``parent``.

    ``name`` is text for the forms a ``str`` path carries (backslash, control
    character, unpaired surrogate) and bytes for a name the platform's
    filesystem decoder cannot represent.
    """
    if isinstance(name, bytes):
        raw = os.fsencode(parent) + b"/" + name
        os.mkdir(raw)
        return Path(os.fsdecode(raw))
    path = parent / name
    path.mkdir()
    return path


def _unsafe_name_forms() -> list[tuple[str, object, bool]]:
    """Return ``(label, name, skippable)`` for the unsafe forms to attempt.

    Windows rejects backslash and control characters in names, so only an
    unpaired surrogate is attempted there (PEP 529 ``surrogatepass``).  macOS
    accepts control characters; its backslash and undecodable forms are
    skipped only if creation raises ``OSError``.  Linux creates every form.
    """
    if sys.platform == "win32":
        return [("unpaired-surrogate", "unsafe-marker\ud800name", True)]
    return [
        ("backslash", "unsafe-marker\\name", sys.platform == "darwin"),
        ("newline", "unsafe-marker\nname", False),
        ("undecodable", b"unsafe-marker\xffname", sys.platform == "darwin"),
    ]


def _nested_directories(root: Path, components: list[str]) -> Path:
    """Create a grouping-directory chain under one stage and return the leaf."""
    directory = root / "Fictional" / "Queue"
    for component in components:
        directory = directory / component
        directory.mkdir()
    return directory


def test_nested_admission_unsafe_names_name_the_containing_directory() -> None:
    attempted = 0
    for label, name, skippable in _unsafe_name_forms():
        for depth, diagnostic in ((1, "Fictional/Queue"), (2, "Fictional/Queue/grp")):
            with TemporaryDirectory() as temporary:
                root = Path(temporary)
                package(root, "Queue", "ok", "# Ok\n")
                parent = root / "Fictional" / "Queue"
                if depth == 2:
                    parent = parent / "grp"
                    parent.mkdir()
                try:
                    unsafe = _make_unsafe_directory(parent, name)
                except OSError:
                    if not skippable:
                        raise
                    continue
                attempted += 1
                _leaf_package(unsafe)
                value = json.loads(catalog.scan_catalog(root))
                diagnostics = [{
                    "code": "discovery_unavailable",
                    "message": f"discovery unavailable: {diagnostic}",
                }]
                context = (label, depth)
                assert value["discovery_diagnostics"] == diagnostics, context
                assert {
                    entry["package_path"] for entry in value["entries"]
                } == {"Fictional/Queue/ok"}, context
                assert value["identity_coverage"] == {
                    "state": "incomplete",
                    "diagnostics": diagnostics,
                }, context
                assert value["inventory"] == {
                    "projects": [{"name": "Fictional", "availability": "incomplete"}],
                    "stages": [{
                        "project": "Fictional", "stage": "Queue",
                        "availability": "incomplete",
                    }],
                }, context
                assert "unsafe-marker" not in json.dumps(value), context
                assert parse_catalog(value).as_dict() == value, context
    if attempted == 0:
        pytest.skip("no unsafe name form is creatable on this platform")


def test_nested_admission_unsafe_link_and_file_in_grouping_folder(tmp_path) -> None:
    if sys.platform == "win32":
        pytest.skip("Windows rejects backslash and control characters in names")
    diagnostics = [{
        "code": "discovery_unavailable",
        "message": "discovery unavailable: Fictional/Queue/grp",
    }]
    for kind in ("link", "file"):
        root = tmp_path / kind
        package(root, "Queue", "ok", "# Ok\n")
        package(root, "Queue", "grp/fine", "# Fine\n")
        group = root / "Fictional" / "Queue" / "grp"
        if kind == "link":
            outside = tmp_path / "outside"
            outside.mkdir()
            outside.joinpath("spec.md").write_text(
                "# Escaped\nescaped-marker\n", encoding="utf-8"
            )
            link_directory(group / "unsafe-marker\x01.md", outside)
        else:
            group.joinpath("unsafe-marker\x01.txt").write_text(
                "escaped-marker\n", encoding="utf-8"
            )
        value = json.loads(catalog.scan_catalog(root))
        assert value["discovery_diagnostics"] == diagnostics, kind
        assert {
            entry["package_path"] for entry in value["entries"]
        } == {"Fictional/Queue/ok", "Fictional/Queue/grp/fine"}, kind
        assert value["identity_coverage"] == {
            "state": "incomplete",
            "diagnostics": diagnostics,
        }, kind
        assert value["inventory"]["stages"] == [{
            "project": "Fictional", "stage": "Queue", "availability": "incomplete",
        }], kind
        assert "escaped-marker" not in json.dumps(value), kind
        assert parse_catalog(value).as_dict() == value, kind


def test_nested_admission_path_bound_is_derived_from_the_message_table() -> None:
    longest = max(
        _text_units(text) for text in catalog.CATALOG_DIAGNOSTIC_MESSAGES.values()
    )
    assert MAX_TEXT_UNITS == 1024
    assert catalog._MAX_ADMITTED_PATH_UNITS == 996
    assert catalog._MAX_ADMITTED_PATH_UNITS == MAX_TEXT_UNITS - (longest + 2)


def _bound_diagnostics(root: Path, directory: Path):
    (directory / "spec.md").write_text("# Bounded\n", encoding="utf-8")
    value = json.loads(catalog.scan_catalog(root))
    parent = directory.parent.relative_to(root).as_posix()
    return value, [{
        "code": "discovery_unavailable",
        "message": f"discovery unavailable: {parent}",
    }]


def test_nested_admission_path_bound_admits_p_and_excludes_p_plus_one(tmp_path) -> None:
    if sys.platform != "linux":
        pytest.skip("over-limit trees are created only on Linux")

    # Exactly P units is admitted and listed, with no diagnostic.
    root = tmp_path / "at-limit"
    package(root, "Queue", "ok", "# Ok\n")
    target = _nested_directories(root, ["a" * 200, "b" * 200, "c" * 200, "d" * 200, "e" * 176])
    at_limit = target.relative_to(root).as_posix()
    assert _text_units(at_limit) == 996
    target.joinpath("spec.md").write_text("# At limit\n", encoding="utf-8")
    value = json.loads(catalog.scan_catalog(root))
    assert value["discovery_diagnostics"] == []
    assert {
        entry["package_path"] for entry in value["entries"]
    } == {"Fictional/Queue/ok", at_limit}
    assert value["inventory"]["stages"] == [{
        "project": "Fictional", "stage": "Queue", "availability": "complete",
    }]
    assert parse_catalog(value).as_dict() == value

    # One unit over P is excluded and names its admitted parent.
    root = tmp_path / "over-limit"
    package(root, "Queue", "ok", "# Ok\n")
    target = _nested_directories(root, ["a" * 200, "b" * 200, "c" * 200, "d" * 200, "e" * 177])
    assert _text_units(target.relative_to(root).as_posix()) == 997
    value, diagnostics = _bound_diagnostics(root, target)
    assert value["discovery_diagnostics"] == diagnostics
    assert {entry["package_path"] for entry in value["entries"]} == {"Fictional/Queue/ok"}
    assert value["inventory"]["stages"] == [{
        "project": "Fictional", "stage": "Queue", "availability": "incomplete",
    }]
    assert parse_catalog(value).as_dict() == value

    # At most P code points but more than P UTF-16 units is excluded too.
    root = tmp_path / "astral"
    package(root, "Queue", "ok", "# Ok\n")
    target = _nested_directories(
        root, ["a" * 200, "b" * 200, "c" * 200, "d" * 200, "e" * 175 + "\U0001f600"]
    )
    astral = target.relative_to(root).as_posix()
    assert len(astral) <= 996
    assert _text_units(astral) == 997
    value, diagnostics = _bound_diagnostics(root, target)
    assert value["discovery_diagnostics"] == diagnostics
    assert {entry["package_path"] for entry in value["entries"]} == {"Fictional/Queue/ok"}
    assert parse_catalog(value).as_dict() == value

    # A safe-named regular file one unit over P yields the parent diagnostic.
    root = tmp_path / "regular-file"
    package(root, "Queue", "ok", "# Ok\n")
    target = _nested_directories(root, ["a" * 200, "b" * 200, "c" * 200, "d" * 200, "e" * 167])
    notes = target / "notes.txt"
    notes.write_text("notes\n", encoding="utf-8")
    assert _text_units(notes.relative_to(root).as_posix()) == 997
    value = json.loads(catalog.scan_catalog(root))
    assert value["discovery_diagnostics"] == [{
        "code": "discovery_unavailable",
        "message": f"discovery unavailable: {target.relative_to(root).as_posix()}",
    }]
    assert {entry["package_path"] for entry in value["entries"]} == {"Fictional/Queue/ok"}
    assert value["inventory"]["stages"] == [{
        "project": "Fictional", "stage": "Queue", "availability": "incomplete",
    }]
    assert parse_catalog(value).as_dict() == value


def test_nested_admission_deep_unreadable_folders_keep_messages_bounded(tmp_path) -> None:
    if sys.platform != "linux":
        pytest.skip("over-limit trees are created only on Linux")

    def messages(value: dict) -> list[str]:
        found = [diagnostic["message"] for diagnostic in value["discovery_diagnostics"]]
        found += [item["message"] for item in value["identity_coverage"]["diagnostics"]]
        found += [item["message"] for item in value["program_coverage"]["diagnostics"]]
        assert found
        return found

    # The F-04 reproduction: five 200-character components, deepest unreadable.
    root = tmp_path / "deep"
    package(root, "Queue", "ok", "# Ok\n")
    deepest = _nested_directories(root, ["a" * 200] * 5)
    fourth = deepest.parent
    fourth_relative = fourth.relative_to(root).as_posix()
    assert _text_units(fourth_relative) == 819
    deepest.chmod(0)
    try:
        value = json.loads(catalog.scan_catalog(root))
    finally:
        deepest.chmod(0o700)
    assert value["discovery_diagnostics"] == [{
        "code": "discovery_unavailable",
        "message": f"discovery unavailable: {fourth_relative}",
    }]
    assert {entry["package_path"] for entry in value["entries"]} == {"Fictional/Queue/ok"}
    assert parse_catalog(value).as_dict() == value
    assert all(_text_units(message) <= MAX_TEXT_UNITS for message in messages(value))

    # An unreadable directory exactly at P units yields its own diagnostic.
    root = tmp_path / "at-bound"
    package(root, "Queue", "ok", "# Ok\n")
    unreadable = _nested_directories(
        root, ["a" * 200, "b" * 200, "c" * 200, "d" * 200, "e" * 176]
    )
    unreadable_relative = unreadable.relative_to(root).as_posix()
    assert _text_units(unreadable_relative) == 996
    unreadable.chmod(0)
    try:
        value = json.loads(catalog.scan_catalog(root))
    finally:
        unreadable.chmod(0o700)
    assert value["discovery_diagnostics"] == [{
        "code": "discovery_unavailable",
        "message": f"discovery unavailable: {unreadable_relative}",
    }]
    assert _text_units(value["discovery_diagnostics"][0]["message"]) == 1019
    assert {entry["package_path"] for entry in value["entries"]} == {"Fictional/Queue/ok"}
    assert parse_catalog(value).as_dict() == value
    assert all(_text_units(message) <= MAX_TEXT_UNITS for message in messages(value))


def test_duplicate_package_id_rows_leave_target_identity_invalid(tmp_path):
    source_id = "11111111-1111-4111-8111-111111111111"
    target_id = "22222222-2222-4222-8222-222222222222"
    root = tmp_path / "specs"
    package(
        root,
        "Queue",
        "source",
        f"# Source\nPackage ID: {source_id}\nCompletion Prerequisite: {target_id}\n",
    )
    package(
        root,
        "Queue",
        "target",
        f"# Target\nPackage ID: {target_id}\nPackage ID: {target_id}\n",
    )
    value = json.loads(catalog.build_catalog(root))
    target = entry_by_path(value, "Fictional/Queue/target")
    assert target["package_id"] is None
    assert set(target["relationship"]) == {"prerequisites", "program"}
    source = entry_by_path(value, "Fictional/Queue/source")
    assert source["relationship"]["prerequisites"] == [
        {"target_package_id": target_id, "reason": "unresolved"}
    ]


@pytest.mark.parametrize("reserved", ["Reference", ".pipeline"])
def test_reserved_names_inside_a_stage_are_skipped_at_every_level(tmp_path, reserved):
    root = tmp_path / "specs"
    package(root, "Queue", f"{reserved}/hidden-a", "# Hidden A\n")
    package(root, "Queue", f"group/{reserved}/hidden-b", "# Hidden B\n")
    package(root, "Queue", "group/visible", "# Visible\n")
    value = json.loads(catalog.scan_catalog(root))
    assert {entry["package_path"] for entry in value["entries"]} == {
        "Fictional/Queue/group/visible"
    }
    assert value["discovery_diagnostics"] == []
    assert value["inventory"]["stages"] == [
        {"project": "Fictional", "stage": "Queue", "availability": "complete"}
    ]


def test_workspace_root_reference_folder_is_not_a_project(tmp_path):
    root = tmp_path / "specs"
    hidden = root / "Reference" / "Queue" / "hidden"
    hidden.mkdir(parents=True)
    hidden.joinpath("spec.md").write_text("# Hidden\n", encoding="utf-8")
    package(root, "Queue", "visible", "# Visible\n")
    value = json.loads(catalog.build_catalog(root))
    assert value["inventory"]["projects"] == [
        {"name": "Fictional", "availability": "complete"}
    ]
    assert [entry["package_path"] for entry in value["entries"]] == [
        "Fictional/Queue/visible"
    ]


def test_anchor_changed_once_after_read_is_retried_and_read_again(tmp_path):
    root = tmp_path / "specs"
    spec = package(root, "Queue", "changing", "# Before\n") / "spec.md"
    rewritten = b"# After the rewrite\n"
    assert len(rewritten) != spec.stat().st_size
    original_open = catalog.Path.open
    opens: list[Path] = []

    def rewriting_open(target, *args, **kwargs):
        stream = original_open(target, *args, **kwargs)
        if Path(target) == spec:
            opens.append(Path(target))
            if len(opens) == 1:
                return RewritingReader(stream, spec, rewritten)
        return stream

    with patch.object(catalog.Path, "open", autospec=True, side_effect=rewriting_open):
        value = json.loads(catalog.scan_catalog(root))
    assert len(opens) == 2
    assert entry_by_path(value, "Fictional/Queue/changing")["declared"]["title"] == (
        "After the rewrite"
    )
    assert value["identity_coverage"] == {"state": "complete", "diagnostics": []}
    assert value["discovery_diagnostics"] == []


def test_anchor_changed_after_every_read_is_retried_exactly_once(tmp_path):
    root = tmp_path / "specs"
    spec = package(root, "Queue", "changing", "# Before\n") / "spec.md"
    original_open = catalog.Path.open
    opens: list[Path] = []

    def rewriting_open(target, *args, **kwargs):
        stream = original_open(target, *args, **kwargs)
        if Path(target) == spec:
            opens.append(Path(target))
            return RewritingReader(stream, spec, b"# Rewrite\n" + b"x" * len(opens))
        return stream

    with patch.object(catalog.Path, "open", autospec=True, side_effect=rewriting_open):
        value = json.loads(catalog.scan_catalog(root))
    assert len(opens) == 2
    assert value["identity_coverage"] == {
        "state": "incomplete",
        "diagnostics": [
            {
                "code": "changed_during_read",
                "message": "changed during read: Fictional/Queue/changing",
            }
        ],
    }


ID_SHIPPED = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ID_SUCCESSOR = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ABSOLUTE_MARKER = "/fictional/absolute-marker"
TARGET = f"{ABSOLUTE_MARKER}/repo"


def _head_root(base: Path) -> Path:
    """A real specification root whose packages exercise every header form."""
    root = base / "specs"
    package(root, "Drafts", "bold", f"# Bold\n**Branch:** topic\nTarget repo: `{TARGET}`\n")
    package(root, "Drafts", "ticked", f"# Ticked\nBranch: `topic`\nTarget repo: {TARGET}\n")
    package(
        root, "Drafts", "none",
        "# None\nBranch: not applicable — no repository\nTarget repo: relative/repo\n",
    )
    package(root, "Drafts", "proposed", "# Proposed\nBranch: spec/x (proposed)\n")
    package(root, "Drafts", "long", f"# Long\nBranch: {'b' * 1025}\n")
    package(root, "Drafts", "legacy", "# Legacy\nProposed branch: legacy\n")
    package(
        root, "Shipped", "done",
        f"# Done\nPackage ID: {ID_SHIPPED}\nSuperseded By: {ID_SUCCESSOR}\nBranch: spec/done\n",
    )
    package(root, "Shelf", "idle", "# Idle\nTarget repo: nowhere\n")
    return root


def _heads_by_name(heads: catalog.PackageHeads) -> dict[str, catalog.PackageHead]:
    return {head.package_path.rsplit("/", 1)[1]: head for head in heads.heads}


def test_package_heads_report_header_values_from_a_real_root(tmp_path) -> None:
    root = _head_root(tmp_path)
    heads = catalog.scan_package_heads(root, completed_stage_names=["Shipped"])
    assert heads.status == "complete"
    by_name = _heads_by_name(heads)
    assert [head.package_path for head in heads.heads] == sorted(
        head.package_path for head in heads.heads
    )
    assert by_name["bold"].branch == catalog.BranchHead("declared", "topic")
    assert by_name["ticked"].branch == catalog.BranchHead("declared", "topic")
    assert by_name["none"].branch == catalog.BranchHead("not_applicable", None)
    assert by_name["proposed"].branch == catalog.BranchHead("invalid", None)
    assert by_name["long"].branch == catalog.BranchHead("invalid", None)
    assert by_name["legacy"].branch == catalog.BranchHead("absent", None)
    assert by_name["bold"].target_repo == catalog.TargetRepoHead("usable", TARGET)
    assert by_name["ticked"].target_repo == catalog.TargetRepoHead("usable", TARGET)
    assert by_name["none"].target_repo == catalog.TargetRepoHead("invalid", None)
    assert by_name["legacy"].target_repo == catalog.TargetRepoHead("absent", None)
    assert by_name["done"].package_id == ID_SHIPPED
    assert by_name["done"].superseded is True
    assert by_name["done"].branch == catalog.BranchHead("declared", "spec/done")
    assert all(not head.superseded for name, head in by_name.items() if name != "done")
    assert by_name["bold"].package_id is None


def test_package_head_branch_limit_is_utf16_units(tmp_path) -> None:
    root = tmp_path / "specs"
    astral = "\U0001F600"
    package(root, "Drafts", "at", f"# At\nBranch: {'b' * 1022}{astral}\n")
    package(root, "Drafts", "over", f"# Over\nBranch: {'b' * 1023}{astral}\n")
    by_name = _heads_by_name(catalog.scan_package_heads(root))
    assert _text_units(by_name["at"].branch.name) == MAX_TEXT_UNITS
    assert by_name["at"].branch.status == "declared"
    assert by_name["over"].branch == catalog.BranchHead("invalid", None)


def test_package_heads_finish_only_the_configured_stages(tmp_path) -> None:
    root = _head_root(tmp_path)
    configured = catalog.scan_package_heads(root, completed_stage_names=["Shipped"])
    assert catalog.finished_policy(["Shipped"]) == "configured"
    assert {
        head.package_path for head in configured.heads if head.finished
    } == {"Fictional/Shipped/done"}
    for policy in (None, [], "Shipped", ["Shipped", "Shipped"], ["bad/stage"]):
        heads = catalog.scan_package_heads(root, completed_stage_names=policy)
        assert catalog.finished_policy(policy) == "not_configured", policy
        assert not any(head.finished for head in heads.heads), policy


def test_package_heads_are_incomplete_when_a_spec_is_unreadable(tmp_path) -> None:
    if sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0):
        pytest.skip("permission bits do not deny reads here")
    root = _head_root(tmp_path)
    blocked = root / "Fictional" / "Drafts" / "bold" / "spec.md"
    blocked.chmod(0)
    try:
        heads = catalog.scan_package_heads(root, completed_stage_names=["Shipped"])
    finally:
        blocked.chmod(0o600)
    assert heads.status == "incomplete"
    unreadable = _heads_by_name(heads)["bold"]
    assert unreadable.branch.status == "absent"
    assert unreadable.target_repo.status == "absent"


def test_package_heads_refuse_a_missing_root(tmp_path) -> None:
    with pytest.raises(ValueError):
        catalog.scan_package_heads(tmp_path / "missing")


def test_scan_catalog_wire_is_unchanged_by_the_package_heads(tmp_path) -> None:
    root = _head_root(tmp_path)
    rendered = catalog.scan_catalog(root, completed_stage_names=["Shipped"])
    assert ABSOLUTE_MARKER not in rendered
    assert "Target repo" not in rendered
    value = json.loads(rendered)
    assert "join" not in rendered
    entries = {entry["package_path"]: entry for entry in value["entries"]}
    bold = entries["Fictional/Drafts/bold"]
    # The displayed basename keeps the baseline rule: a backticked value is not
    # normalized here, only in the package heads.
    assert bold["declared"]["target_project"] == "repo`"
    assert entries["Fictional/Drafts/ticked"]["declared"]["target_project"] == "repo"
    assert bold["reported_fields"] == [{"name": "Branch", "value": "topic"}]
    assert entries["Fictional/Drafts/ticked"]["reported_fields"] == [
        {"name": "Branch", "value": "`topic`"}
    ]
    assert entries["Fictional/Drafts/proposed"]["reported_fields"] == [
        {"name": "Branch", "value": "spec/x (proposed)"},
    ]
    assert entries["Fictional/Drafts/legacy"]["reported_fields"] == [
        {"name": "Proposed branch", "value": "legacy"},
    ]
    assert value["terminal_stages"] == ["Shipped"]
    # The same bytes whether or not the heads were read from the same root.
    catalog.scan_package_heads(root, completed_stage_names=["Shipped"])
    assert catalog.scan_catalog(root, completed_stage_names=["Shipped"]) == rendered
    assert catalog.build_catalog(root, completed_stage_names=["Shipped"]) == rendered


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("topic", "topic"),
        ("** topic", "topic"),
        ("**topic", "topic"),
        ("  `topic`  ", "topic"),
        ("** `topic`", "topic"),
        ("`a`", "a"),
        ("``", "``"),
        ("`", "`"),
        ("``x``", "`x`"),
        ("`topic", "`topic"),
        ("", ""),
    ],
)
def test_normalize_header_value_strips_bold_residue_and_one_backtick_pair(raw, expected) -> None:
    assert catalog.normalize_header_value(raw) == expected


@pytest.mark.parametrize(
    "name",
    [
        "a b", "a~b", "a^b", "a:b", "a?b", "a*b", "a[b", "a\\b", "a`b", "a<b", "a>b",
        "a..b", "a@{b", "a//b", "@", "-a", "/a", "a/", "a.", "a/.b", "a.lock",
        "a/b.lock/c", "a\x07b", "a\x7fb",
    ],
)
def test_branch_head_rejects_names_git_would_refuse(name) -> None:
    assert catalog._branch_head(name) == catalog.BranchHead("invalid", None)


@pytest.mark.parametrize("name", ["topic", "spec/x", "a@b", "v1.2", "feature/é", "a-b_c"])
def test_branch_head_accepts_ordinary_names(name) -> None:
    assert catalog._branch_head(name) == catalog.BranchHead("declared", name)


def test_branch_head_not_applicable_is_ascii_case_insensitive() -> None:
    assert catalog._branch_head("Not Applicable — x").status == "not_applicable"
    assert catalog._branch_head("not applicable").status == "not_applicable"
    assert catalog._branch_head("not applic").status == "invalid"


def test_target_repo_head_requires_an_absolute_path() -> None:
    assert catalog._target_repo_head(f"** `{TARGET}`") == catalog.TargetRepoHead("usable", TARGET)
    assert catalog._target_repo_head("TBD — later").status == "invalid"
    assert catalog._target_repo_head("Minotaur").status == "invalid"
    assert catalog._target_repo_head("  ").status == "absent"
    assert catalog._target_repo_head(None).status == "absent"


def test_package_heads_judge_target_repo_by_the_host_absolute_form(tmp_path) -> None:
    root = tmp_path / "specs"
    native = tmp_path / "checkouts" / "repo"
    package(root, "Drafts", "native", f"# Native\nTarget repo: `{native}`\n")
    package(root, "Drafts", "drive", "# Drive\nTarget repo: C:\\work\\repo\n")
    by_name = _heads_by_name(catalog.scan_package_heads(root))
    assert by_name["native"].target_repo == catalog.TargetRepoHead("usable", str(native))
    if sys.platform != "win32":
        assert by_name["drive"].target_repo == catalog.TargetRepoHead("invalid", None)
    else:
        assert by_name["drive"].target_repo == catalog.TargetRepoHead("usable", "C:\\work\\repo")


HEX40 = "0123456789abcdef0123456789abcdef01234567"
HEX64 = "0123456789abcdef" * 4
BASELINE_FORMS = {
    "plain": (f"Baseline: {HEX40}", catalog.BaselineHead("declared", HEX40)),
    "zero": (f"Baseline: {'0' * 40}", catalog.BaselineHead("declared", "0" * 40)),
    "bold": (f"**Baseline:** `{HEX40}` (note)", catalog.BaselineHead("declared", HEX40)),
    "dated": (f"Baseline: {HEX40} (2026-09-01)", catalog.BaselineHead("declared", HEX40)),
    "wide": (f"Baseline: {HEX64}", catalog.BaselineHead("declared", HEX64)),
    "upper": (f"Baseline: {HEX40.upper()}", catalog.BaselineHead("invalid", None)),
    "short": ("Baseline: c3e197a", catalog.BaselineHead("invalid", None)),
    "branch": ("Baseline: main @ abc1234", catalog.BaselineHead("invalid", None)),
    "long": (f"Baseline: {HEX40}0", catalog.BaselineHead("invalid", None)),
    "prefixed": (f"Baseline: main {HEX40}", catalog.BaselineHead("invalid", None)),
    "open_tick": (f"Baseline: `{HEX40}", catalog.BaselineHead("invalid", None)),
    "double_tick": (f"Baseline: ``{HEX40}`` (note)", catalog.BaselineHead("invalid", None)),
    "empty": ("Baseline:", catalog.BaselineHead("absent", None)),
    "missing": (None, catalog.BaselineHead("absent", None)),
    "sanity": (f"Sanity Baseline: {HEX40}", catalog.BaselineHead("absent", None)),
}


def _baseline_root(base: Path, *, with_baseline: bool) -> Path:
    root = base / ("with" if with_baseline else "without") / "specs"
    for name, (line, _) in BASELINE_FORMS.items():
        lines = ["# Package", "Branch: topic"]
        if line is not None and (with_baseline or not line.lstrip("*").startswith("Baseline")):
            lines.append(line)
        package(root, "Drafts", name, "\n".join(lines) + "\n")
    return root


def test_package_heads_read_the_baseline_header_forms(tmp_path) -> None:
    with_lines = _heads_by_name(catalog.scan_package_heads(_baseline_root(tmp_path, with_baseline=True)))
    without = _heads_by_name(catalog.scan_package_heads(_baseline_root(tmp_path, with_baseline=False)))
    assert set(with_lines) == set(BASELINE_FORMS)
    for name, (_, expected) in BASELINE_FORMS.items():
        assert with_lines[name].baseline == expected, name
        assert without[name].baseline == catalog.BaselineHead("absent", None), name
        assert dataclasses.replace(with_lines[name], baseline=expected, package_path="") == (
            dataclasses.replace(without[name], baseline=expected, package_path="")
        ), name


def test_baseline_header_does_not_reach_the_catalog_wire(tmp_path) -> None:
    root = _baseline_root(tmp_path, with_baseline=True)
    rendered = catalog.scan_catalog(root)
    with patch.object(
        catalog, "_JOIN_LABELS", {"target_repo": "Target repo", "branch": "Branch"}
    ):
        assert catalog.scan_catalog(root) == rendered
    entries = {entry["package_path"]: entry for entry in json.loads(rendered)["entries"]}
    assert {"name": "Baseline", "value": HEX40} in entries["Fictional/Drafts/plain"]["reported_fields"]


SCAN_ID_A = "20000000-0000-4000-8000-00000000000a"
SCAN_ID_B = "20000000-0000-4000-8000-00000000000b"
SCAN_ID_C = "20000000-0000-4000-8000-00000000000c"


def _scan_root(base: Path) -> Path:
    """A root with a nested package, a linked spec and completion rows."""
    root = base / "specs"
    outer = package(root, "Shelf", "outer", f"# Outer\nPackage ID: {SCAN_ID_A}\n")
    inner = outer / "inner"
    inner.mkdir()
    inner.joinpath("spec.md").write_text(f"# Inner\nPackage ID: {SCAN_ID_C}\n", encoding="utf-8")
    package(
        root, "Drafts", "rows",
        f"# Rows\nPackage ID: {SCAN_ID_B}\n"
        f"Completion Prerequisite: {SCAN_ID_A}\n"
        "Completion Prerequisite: not-an-id\n",
    )
    real = base / "real-spec.md"
    real.write_text(f"# Linked\nPackage ID: {SCAN_ID_C}\n", encoding="utf-8")
    linked = root / "Fictional" / "Drafts" / "linked"
    linked.mkdir()
    linked.joinpath("spec.md").symlink_to(real)
    return root


def test_scan_packages_records_match_the_catalog_listing(tmp_path) -> None:
    root = _scan_root(tmp_path)
    listed = [
        entry["package_path"] for entry in json.loads(catalog.build_catalog(root))["entries"]
    ]
    scan = catalog.scan_packages(root)

    assert sorted(listed) == [record.package_path for record in scan.records]
    assert "Fictional/Shelf/outer/inner" not in listed
    by_name = {record.package_path.rsplit("/", 1)[1]: record for record in scan.records}
    assert set(by_name) == {"outer", "rows", "linked"}
    rows = by_name["rows"]
    assert rows.stage == "Drafts"
    assert rows.package_id == SCAN_ID_B
    assert rows.candidate_ids == frozenset({SCAN_ID_B})
    assert rows.read_diagnostic is None
    prerequisites = rows.relationship["prerequisites"]
    assert all(set(row) == {"target_package_id"} for row in prerequisites)
    assert [row["target_package_id"] for row in prerequisites] == [SCAN_ID_A, None]
    linked = by_name["linked"]
    assert linked.read_diagnostic == "nonregular_anchor"
    assert linked.package_id is None
    assert linked.relationship is None


def test_scan_packages_status_follows_identity_coverage(tmp_path) -> None:
    root = _scan_root(tmp_path)
    coverage = json.loads(catalog.build_catalog(root))["identity_coverage"]["state"]
    assert coverage == "incomplete"
    assert catalog.scan_packages(root).status == coverage

    clean = tmp_path / "clean"
    package(clean, "Drafts", "one", f"# One\nPackage ID: {SCAN_ID_A}\n")
    assert json.loads(catalog.build_catalog(clean))["identity_coverage"]["state"] == "complete"
    assert catalog.scan_packages(clean).status == "complete"


def test_scan_package_record_fields_are_exactly_the_header_facts() -> None:
    fields = {field.name for field in dataclasses.fields(catalog.PackageScanRecord)}
    assert fields == {
        "package_path", "stage", "read_diagnostic", "relationship", "candidate_ids", "package_id",
    }
    assert {field.name for field in dataclasses.fields(catalog.PackageScan)} == {"status", "records"}


def test_scan_package_records_are_frozen(tmp_path) -> None:
    scan = catalog.scan_packages(_scan_root(tmp_path))
    with pytest.raises(dataclasses.FrozenInstanceError):
        scan.records[0].package_path = "other"
    with pytest.raises(dataclasses.FrozenInstanceError):
        scan.status = "incomplete"


def test_scan_package_record_path_reads_back_a_section_past_the_header_limit(tmp_path) -> None:
    root = tmp_path / "specs"
    filler = "x" * (catalog._HEADER_BYTE_LIMIT + 100)
    package(
        root, "Queue", "long",
        f"# Long\nPackage ID: {SCAN_ID_A}\nStatus: ready\n\n## Notes\n{filler}\n\n"
        "## File boundaries\n\n| Task | File |\n| --- | --- |\n| T1 | `a.py` |\n",
    )
    (record,) = catalog.scan_packages(root).records
    text = (root / record.package_path / "spec.md").read_text(encoding="utf-8")
    assert text.index("## File boundaries") > catalog._HEADER_BYTE_LIMIT
    assert text.endswith("| T1 | `a.py` |\n")
    assert record.package_id == SCAN_ID_A


def _scandir_calls(root: Path, function) -> int:
    with patch.object(catalog, "_catalog_scandir", wraps=catalog._catalog_scandir) as scans:
        function(root)
    return scans.call_count


def test_scan_packages_walks_the_tree_exactly_as_often_as_one_catalog_build(tmp_path) -> None:
    root = _scan_root(tmp_path)
    expected = _scandir_calls(root, catalog.build_catalog)
    assert expected > 0
    assert _scandir_calls(root, catalog.scan_packages) == expected


def test_scan_packages_does_not_change_the_catalog_bytes(tmp_path) -> None:
    root = _scan_root(tmp_path)
    before = catalog.build_catalog(root)
    catalog.scan_packages(root)
    assert catalog.build_catalog(root) == before


def test_scan_and_catalog_serve_the_loki_prerequisite_interface(tmp_path):
    """One root proves both interfaces an ordering consumer reads."""
    source_id = "11111111-1111-4111-8111-111111111111"
    target_id = "22222222-2222-4222-9222-222222222222"
    missing_id = "33333333-3333-4333-8333-333333333333"
    duplicate_id = "44444444-4444-4444-8444-444444444444"
    blocked_id = "55555555-5555-4555-8555-555555555555"
    root = tmp_path / "specs"
    package(
        root,
        "Queue",
        "source",
        f"# Source\nPackage ID: {source_id}\n"
        f"Completion Prerequisite: {target_id}\n"
        "Completion Prerequisite: not-a-uuid\n"
        f"Completion Prerequisite: {missing_id}\n"
        f"Completion Prerequisite: {source_id}\n"
        f"Completion Prerequisite: {duplicate_id}\n"
        f"Completion Prerequisite: {target_id} | claim\n",
    )
    package(root, "Queue", "target", f"# Target\nPackage ID: {target_id}\n")
    package(root, "Queue", "duplicate-a", f"# A\nPackage ID: {duplicate_id}\n")
    package(root, "Queue", "duplicate-b", f"# B\nPackage ID: {duplicate_id}\n")

    scan = catalog.scan_packages(root)
    assert scan.status == "complete"
    source = next(
        record for record in scan.records
        if record.package_path == "Fictional/Queue/source"
    )
    assert source.stage == "Queue"
    assert source.package_id == source_id
    assert source.candidate_ids == frozenset({source_id})
    rows = source.relationship["prerequisites"]
    assert all(set(row) == {"target_package_id"} for row in rows)
    assert [row["target_package_id"] for row in rows] == [
        target_id, None, missing_id, source_id, duplicate_id, None,
    ]

    entry = entry_by_path(json.loads(catalog.build_catalog(root)), "Fictional/Queue/source")
    edges = entry["relationship"]["prerequisites"]
    assert all(set(edge) == {"target_package_id", "reason"} for edge in edges)
    assert [edge["target_package_id"] for edge in edges] == sorted(
        [target_id, None, missing_id, source_id, duplicate_id, None],
        key=lambda value: value or "",
    )
    # Only the readable, singly identified target resolves; the self row does
    # not, and neither does a malformed, absent, duplicated or pipe-qualified one.
    assert [edge["reason"] for edge in edges] == [
        "unresolved", "unresolved", "unresolved", "resolved", "unresolved",
        "unresolved",
    ]

    blocked = package(root, "Queue", "blocked", f"# Blocked\nPackage ID: {blocked_id}\n")
    blocked_anchor = blocked / "spec.md"
    permission_bits_deny = sys.platform != "win32" and not (
        hasattr(os, "geteuid") and os.geteuid() == 0
    )
    blocked_anchor.chmod(0)
    if permission_bits_deny:
        denial = nullcontext()
    else:
        original_open = catalog.Path.open

        def deny(target, *args, **kwargs):
            if Path(target) == blocked_anchor:
                raise PermissionError("injected unreadable anchor")
            return original_open(target, *args, **kwargs)

        denial = patch.object(catalog.Path, "open", autospec=True, side_effect=deny)
    try:
        with denial:
            incomplete = catalog.scan_packages(root)
            incomplete_value = json.loads(catalog.build_catalog(root))
    finally:
        blocked_anchor.chmod(0o600)
    assert incomplete.status == "incomplete"
    incomplete_edges = entry_by_path(
        incomplete_value, "Fictional/Queue/source"
    )["relationship"]["prerequisites"]
    assert [edge["reason"] for edge in incomplete_edges] == ["unresolved"] * 6


def test_served_edges_ignore_the_completion_policy(tmp_path):
    """The policy still groups terminal stages but never evaluates an edge."""
    source_id = "11111111-1111-4111-8111-111111111111"
    target_id = "22222222-2222-4222-8222-222222222222"
    root = tmp_path / "specs"
    package(
        root, "Queue", "source",
        f"# Source\nPackage ID: {source_id}\nCompletion Prerequisite: {target_id}\n",
    )
    target = package(root, "Done", "target", f"# Target\nPackage ID: {target_id}\n")
    expected_edge = [{"target_package_id": target_id, "reason": "resolved"}]

    def served_under_every_policy():
        values = [
            json.loads(catalog.build_catalog(root, completed_stage_names=policy))
            for policy in (None, ["Done"], ["Done", "Done"])
        ]
        assert values[0]["entries"] == values[1]["entries"] == values[2]["entries"]
        assert [value["terminal_stages"] for value in values] == [[], ["Done"], []]
        for value in values:
            assert entry_by_path(
                value, "Fictional/Queue/source"
            )["relationship"]["prerequisites"] == expected_edge
        return values

    served_under_every_policy()
    (root / "Fictional" / "Queue").mkdir(exist_ok=True)
    target.rename(root / "Fictional" / "Queue" / "target")
    served_under_every_policy()
    assert list(inspect.signature(catalog._edge).parameters) == [
        "source", "row", "index", "identity_complete",
    ]
    assert list(inspect.signature(catalog._render_entry).parameters) == [
        "record", "index", "identity_complete", "program_index",
        "program_coverage_complete",
    ]


def test_relationship_shape_identity_and_legacy_edges(tmp_path):
    """Every served relationship is the two-key shape, edges included."""
    target_id = "22222222-2222-4222-8222-222222222222"
    duplicate_id = "11111111-1111-4111-8111-111111111111"
    root = tmp_path / "specs"
    package(root, "Archive", "legacy", f"# Legacy\nCompletion Prerequisite: {target_id}\n")
    package(root, "Queue", "target", f"# Target\nPackage ID: {target_id}\n")
    package(
        root, "Queue", "twice",
        f"# Twice\nPackage ID: {duplicate_id}\nPackage ID: {duplicate_id}\n",
    )
    value = json.loads(catalog.build_catalog(root))
    for entry in value["entries"]:
        assert set(entry["relationship"]) == {"prerequisites", "program"}
        assert set(entry["relationship"]["program"]) == {
            "program_id", "title", "resolution",
        }
    legacy = entry_by_path(value, "Fictional/Archive/legacy")
    assert legacy["package_id"] is None
    assert legacy["relationship"]["prerequisites"] == [
        {"target_package_id": target_id, "reason": "resolved"},
    ]
    assert entry_by_path(value, "Fictional/Queue/twice")["package_id"] is None
    assert {field.name for field in dataclasses.fields(CatalogEntry)} == {
        "package_path", "project", "stage", "declared", "reported_fields",
        "relationship_data", "package_id", "program_id", "program_title",
    }
    # The legacy package's edge survives the protocol, so its parsed entry keeps it.
    assert parse_catalog(value).as_dict() == value


def test_retired_labels_stay_out_of_reported_fields(tmp_path):
    """A finished and a live card show only their live header fields."""
    target_id = "22222222-2222-4222-8222-222222222222"
    rows = (
        f"Prerequisite: {target_id} | release\n"
        "Claim: release | satisfied | sha256:" + "a" * 64 + "\n"
        "Owner: Alice\n"
    )
    for index, (stage, package_id) in enumerate((
        ("Done", "11111111-1111-4111-8111-111111111111"),
        ("Under_Development", "22222222-2222-4222-9222-222222222222"),
    )):
        root = tmp_path / str(index) / "specs"
        package(root, stage, "item", f"# Item\nPackage ID: {package_id}\n" + rows)
        value = json.loads(catalog.build_catalog(root))
        entry = entry_by_path(value, f"Fictional/{stage}/item")
        assert entry["reported_fields"] == [{"name": "Owner", "value": "Alice"}]
        assert entry["relationship"]["prerequisites"] == []
        assert value["discovery_diagnostics"] == []
        assert value["identity_coverage"] == {"state": "complete", "diagnostics": []}
        assert parse_catalog(value).as_dict() == value
    # Dropping the retired labels from the exclusion shows what it hides.
    retired = {label.lower() for label in catalog.RETIRED_HEADER_LABELS}
    root = tmp_path / "0" / "specs"
    with patch.object(
        catalog, "_REPORTED_EXCLUSIONS", catalog._REPORTED_EXCLUSIONS - retired
    ):
        exposed = json.loads(catalog.build_catalog(root))
    assert [
        field["name"]
        for field in entry_by_path(exposed, "Fictional/Done/item")["reported_fields"]
    ] == ["Claim", "Owner", "Prerequisite"]
