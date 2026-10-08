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

from nyx import catalog
from nyx.models import SCHEMA_VERSION, canonical_digest, parse_catalog

V1 = """# Example
Status: approved
Closure: approved
Sanity Recommendation: IMPLEMENT
Human Sanity Decision: AFFIRMED
Target repo: /fictional/repo
"""

PROGRAM_ID = "88888888-8888-4888-8888-888888888888"
SMALL_ORACLE_BYTES = "{\"catalog_digest\":\"22ad4a68204302a96b44560271803e5eb428e16e95e36fed275e312249c09c26\",\"configuration_revision\":null,\"discovery_diagnostics\":[],\"entries\":[{\"declared\":{\"closure\":null,\"human_sanity_decision\":null,\"sanity_recommendation\":null,\"status\":null,\"target_project\":null,\"title\":\"One\"},\"diagnostics\":[],\"package_id\":null,\"package_path\":\"Fictional/Queue/one\",\"project\":\"Fictional\",\"relationship\":{\"claims\":[],\"direct_prerequisite_state\":\"relationship_unavailable\",\"participation\":\"legacy\",\"prerequisites\":[],\"program\":{\"diagnostics\":[],\"program_id\":null,\"resolution\":\"not_declared\",\"title\":null},\"superseded_by\":{\"diagnostics\":[],\"package_id\":null,\"resolution\":\"not_declared\"}},\"stage\":\"Queue\",\"state\":\"complete\",\"transitive_diagnostics\":[]}],\"identity_coverage\":{\"diagnostics\":[],\"state\":\"complete\"},\"inventory\":{\"projects\":[{\"availability\":\"complete\",\"name\":\"Fictional\"}],\"stages\":[{\"availability\":\"complete\",\"project\":\"Fictional\",\"stage\":\"Queue\"}]},\"program_coverage\":{\"diagnostics\":[],\"state\":\"complete\"},\"programs\":[],\"schema_version\":7,\"terminal_stages\":[]}"
COMPLETE_ORACLE_BYTES = "{\"catalog_digest\":\"29be6f910089807a68848044c136d2980a558a8a87ee2fe265c49b32df49e9fe\",\"configuration_revision\":null,\"discovery_diagnostics\":[],\"entries\":[{\"declared\":{\"closure\":null,\"human_sanity_decision\":null,\"sanity_recommendation\":null,\"status\":null,\"target_project\":null,\"title\":\"Archive\"},\"diagnostics\":[],\"package_id\":\"77777777-7777-4777-8777-777777777777\",\"package_path\":\"Fictional/Archive/pkg\",\"project\":\"Fictional\",\"relationship\":{\"claims\":[],\"direct_prerequisite_state\":\"no_declared_prerequisites\",\"participation\":\"available\",\"prerequisites\":[],\"program\":{\"diagnostics\":[],\"program_id\":null,\"resolution\":\"not_declared\",\"title\":null},\"superseded_by\":{\"diagnostics\":[],\"package_id\":null,\"resolution\":\"not_declared\"}},\"stage\":\"Archive\",\"state\":\"complete\",\"transitive_diagnostics\":[]},{\"declared\":{\"closure\":null,\"human_sanity_decision\":null,\"sanity_recommendation\":null,\"status\":null,\"target_project\":null,\"title\":\"Retro\"},\"diagnostics\":[],\"package_id\":\"55555555-5555-4555-8555-555555555555\",\"package_path\":\"Fictional/Awaiting_Retrospective/pkg\",\"project\":\"Fictional\",\"relationship\":{\"claims\":[],\"direct_prerequisite_state\":\"no_declared_prerequisites\",\"participation\":\"available\",\"prerequisites\":[],\"program\":{\"diagnostics\":[],\"program_id\":null,\"resolution\":\"not_declared\",\"title\":null},\"superseded_by\":{\"diagnostics\":[],\"package_id\":null,\"resolution\":\"not_declared\"}},\"stage\":\"Awaiting_Retrospective\",\"state\":\"complete\",\"transitive_diagnostics\":[]},{\"declared\":{\"closure\":null,\"human_sanity_decision\":null,\"sanity_recommendation\":null,\"status\":null,\"target_project\":null,\"title\":\"Done\"},\"diagnostics\":[],\"package_id\":\"66666666-6666-4666-8666-666666666666\",\"package_path\":\"Fictional/Done/pkg\",\"project\":\"Fictional\",\"relationship\":{\"claims\":[],\"direct_prerequisite_state\":\"no_declared_prerequisites\",\"participation\":\"available\",\"prerequisites\":[],\"program\":{\"diagnostics\":[],\"program_id\":null,\"resolution\":\"not_declared\",\"title\":null},\"superseded_by\":{\"diagnostics\":[],\"package_id\":null,\"resolution\":\"not_declared\"}},\"stage\":\"Done\",\"state\":\"complete\",\"transitive_diagnostics\":[]},{\"declared\":{\"closure\":null,\"human_sanity_decision\":null,\"sanity_recommendation\":null,\"status\":null,\"target_project\":null,\"title\":\"Progress\"},\"diagnostics\":[],\"package_id\":\"33333333-3333-4333-8333-333333333333\",\"package_path\":\"Fictional/In_Progress/pkg\",\"project\":\"Fictional\",\"relationship\":{\"claims\":[{\"diagnostics\":[],\"evidence_ref\":\"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\",\"name\":\"release\",\"state\":\"satisfied\"}],\"direct_prerequisite_state\":\"no_declared_prerequisites\",\"participation\":\"available\",\"prerequisites\":[],\"program\":{\"diagnostics\":[],\"program_id\":null,\"resolution\":\"not_declared\",\"title\":null},\"superseded_by\":{\"diagnostics\":[{\"code\":\"successor_cycle\",\"message\":\"successor cycle detected: Fictional/In_Progress/pkg\"}],\"package_id\":\"22222222-2222-4222-8222-222222222222\",\"resolution\":\"resolved\"}},\"stage\":\"In_Progress\",\"state\":\"complete\",\"transitive_diagnostics\":[]},{\"declared\":{\"closure\":null,\"human_sanity_decision\":null,\"sanity_recommendation\":null,\"status\":null,\"target_project\":null,\"title\":\"Fix\"},\"diagnostics\":[],\"package_id\":\"44444444-4444-4444-8444-444444444444\",\"package_path\":\"Fictional/Needs_Fixes/pkg\",\"project\":\"Fictional\",\"relationship\":{\"claims\":[],\"direct_prerequisite_state\":\"no_declared_prerequisites\",\"participation\":\"available\",\"prerequisites\":[],\"program\":{\"diagnostics\":[],\"program_id\":null,\"resolution\":\"not_declared\",\"title\":null},\"superseded_by\":{\"diagnostics\":[],\"package_id\":null,\"resolution\":\"not_declared\"}},\"stage\":\"Needs_Fixes\",\"state\":\"complete\",\"transitive_diagnostics\":[]},{\"declared\":{\"closure\":null,\"human_sanity_decision\":null,\"sanity_recommendation\":null,\"status\":null,\"target_project\":null,\"title\":\"Queue\"},\"diagnostics\":[{\"code\":\"invalid_claim\",\"message\":\"invalid claim: Fictional/Queue/pkg\"},{\"code\":\"invalid_prerequisite\",\"message\":\"invalid prerequisite: Fictional/Queue/pkg\"}],\"package_id\":\"22222222-2222-4222-8222-222222222222\",\"package_path\":\"Fictional/Queue/pkg\",\"project\":\"Fictional\",\"relationship\":{\"claims\":[{\"diagnostics\":[],\"evidence_ref\":null,\"name\":\"release\",\"state\":\"unsatisfied\"}],\"direct_prerequisite_state\":\"unknown\",\"participation\":\"available\",\"prerequisites\":[{\"claim_name\":null,\"kind\":\"claim\",\"observed_evidence_ref\":null,\"observed_state\":null,\"reason\":\"invalid_prerequisite\",\"resolved_state\":\"unknown\",\"target_package_id\":null},{\"claim_name\":\"release\",\"kind\":\"claim\",\"observed_evidence_ref\":\"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\",\"observed_state\":\"satisfied\",\"reason\":\"claim_satisfied\",\"resolved_state\":\"satisfied\",\"target_package_id\":\"33333333-3333-4333-8333-333333333333\"}],\"program\":{\"diagnostics\":[],\"program_id\":\"88888888-8888-4888-8888-888888888888\",\"resolution\":\"resolved\",\"title\":\"Core\"},\"superseded_by\":{\"diagnostics\":[{\"code\":\"successor_cycle\",\"message\":\"successor cycle detected: Fictional/Queue/pkg\"}],\"package_id\":\"33333333-3333-4333-8333-333333333333\",\"resolution\":\"resolved\"}},\"stage\":\"Queue\",\"state\":\"partial\",\"transitive_diagnostics\":[]},{\"declared\":{\"closure\":null,\"human_sanity_decision\":null,\"sanity_recommendation\":null,\"status\":null,\"target_project\":null,\"title\":\"Under\"},\"diagnostics\":[],\"package_id\":\"11111111-1111-4111-8111-111111111111\",\"package_path\":\"Fictional/Under_Development/pkg\",\"project\":\"Fictional\",\"relationship\":{\"claims\":[{\"diagnostics\":[],\"evidence_ref\":\"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"name\":\"design\",\"state\":\"satisfied\"}],\"direct_prerequisite_state\":\"no_declared_prerequisites\",\"participation\":\"available\",\"prerequisites\":[],\"program\":{\"diagnostics\":[],\"program_id\":\"88888888-8888-4888-8888-888888888888\",\"resolution\":\"resolved\",\"title\":\"Core\"},\"superseded_by\":{\"diagnostics\":[],\"package_id\":null,\"resolution\":\"not_declared\"}},\"stage\":\"Under_Development\",\"state\":\"complete\",\"transitive_diagnostics\":[]}],\"identity_coverage\":{\"diagnostics\":[],\"state\":\"complete\"},\"inventory\":{\"projects\":[{\"availability\":\"complete\",\"name\":\"Fictional\"}],\"stages\":[{\"availability\":\"complete\",\"project\":\"Fictional\",\"stage\":\"Archive\"},{\"availability\":\"complete\",\"project\":\"Fictional\",\"stage\":\"Awaiting_Retrospective\"},{\"availability\":\"complete\",\"project\":\"Fictional\",\"stage\":\"Done\"},{\"availability\":\"complete\",\"project\":\"Fictional\",\"stage\":\"In_Progress\"},{\"availability\":\"complete\",\"project\":\"Fictional\",\"stage\":\"Needs_Fixes\"},{\"availability\":\"complete\",\"project\":\"Fictional\",\"stage\":\"Queue\"},{\"availability\":\"complete\",\"project\":\"Fictional\",\"stage\":\"Under_Development\"}]},\"program_coverage\":{\"diagnostics\":[],\"state\":\"complete\"},\"programs\":[{\"diagnostics\":[],\"member_package_ids\":[\"11111111-1111-4111-8111-111111111111\",\"22222222-2222-4222-8222-222222222222\"],\"program_id\":\"88888888-8888-4888-8888-888888888888\",\"title\":\"Core\"}],\"schema_version\":7,\"terminal_stages\":[]}"


def _migrate_oracle(legacy: str) -> str:
    """Canonicalize the inspected schema-seven producer oracles."""
    value = json.loads(legacy)
    value["schema_version"] = SCHEMA_VERSION
    value["configuration_revision"] = None
    # The historical fixtures predate the completion policy, so they carry no
    # terminal-stage grouping.  The digest is recomputed below.
    value["terminal_stages"] = []
    for program in value["programs"]:
        del program["diagnostics"]
    for entry in value["entries"]:
        for key in ("state", "diagnostics", "transitive_diagnostics"):
            del entry[key]
        for key in ("closure", "sanity_recommendation", "human_sanity_decision"):
            del entry["declared"][key]
        entry["reported_fields"] = []
        for claim in entry["relationship"]["claims"]:
            del claim["diagnostics"]
        for key in ("program", "superseded_by"):
            del entry["relationship"][key]["diagnostics"]
        for edge in entry["relationship"]["prerequisites"]:
            edge["kind"] = "claim"
    projects = sorted({entry["project"] for entry in value["entries"]})
    stages = sorted({(entry["project"], entry["stage"]) for entry in value["entries"]})
    if len(value["entries"]) >= 5 and projects == ["Fictional"]:
        stages = sorted(("Fictional", stage) for stage in ("Archive", "Awaiting_Retrospective", "Done", "In_Progress", "Needs_Fixes", "Queue", "Under_Development"))
    value["inventory"] = {
        "projects": [{"name": project, "availability": "complete"} for project in projects],
        "stages": [
            {"project": project, "stage": stage, "availability": "complete"}
            for project, stage in stages
        ],
    }
    value["catalog_digest"] = canonical_digest(value)
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


SMALL_ORACLE_BYTES = _migrate_oracle(SMALL_ORACLE_BYTES)
COMPLETE_ORACLE_BYTES = _migrate_oracle(COMPLETE_ORACLE_BYTES)


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
                f"Prerequisite: {target_id} | handoff\n",
            )
            package(
                root,
                "Done",
                "target",
                f"# Target\nPackage ID: {target_id}\n"
                f"Claim: handoff | satisfied | sha256:{'a' * 64}\n",
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
            self.assertEqual(
                [{
                    "evidence_ref": f"sha256:{'a' * 64}",
                    "name": "handoff",
                    "state": "satisfied",
                }],
                target["relationship"]["claims"],
            )
            self.assertEqual(target_id, edge["target_package_id"])
            self.assertEqual("handoff", edge["claim_name"])
            self.assertEqual("satisfied", edge["observed_state"])
            self.assertEqual(f"sha256:{'a' * 64}", edge["observed_evidence_ref"])
            self.assertEqual("satisfied", edge["resolved_state"])
            self.assertEqual("claim_satisfied", edge["reason"])
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
                    f"Prerequisite: {target_id} | handoff\n")
            package(root, "Done", "target",
                    f"# Target\nPackage ID: {target_id}\n"
                    f"Claim: handoff | satisfied | sha256:{'a' * 64}\n")
            first = json.loads(catalog.scan_catalog(root))
            source = next(entry for entry in first["entries"]
                          if entry["package_path"].endswith("/source"))
            edge = source["relationship"]["prerequisites"][0]
            self.assertEqual("satisfied", edge["resolved_state"])
            self.assertEqual("claim_satisfied", edge["reason"])
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
                    f"Prerequisite: {target_id} | handoff\n")
            package(root, "Done", "target-a", f"# A\nPackage ID: {target_id}\n")
            package(root, "Archive", "target-b", f"# B\nPackage ID: {target_id}\n")
            value = json.loads(catalog.scan_catalog(root))
            edge = next(entry for entry in value["entries"]
                        if entry["package_path"].endswith("/source"))["relationship"]["prerequisites"][0]
            self.assertEqual("duplicate_target", edge["reason"])
            self.assertEqual("unknown", edge["resolved_state"])

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
            self.assertEqual("invalid",
                             entries["Fictional/Queue/malformed"]["relationship"]["participation"])
            self.assertIn({"code": "unreadable_anchor", "message": "unreadable anchor: Fictional/Queue/unreadable"}, value["identity_coverage"]["diagnostics"])
            self.assertNotIn(str(malformed), json.dumps(value))

    def test_output_bound_is_enforced(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package(root, "Queue", "one", V1)
            with patch.object(catalog, "MAX_OUTPUT_BYTES", 10), self.assertRaisesRegex(
                ValueError, "exceeds 2 MiB"
            ):
                catalog.scan_catalog(root)

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

    def test_missing_prerequisite_is_unknown_without_target_payload(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_id = "11111111-1111-4111-8111-111111111111"
            target_id = "22222222-2222-4222-8222-222222222222"
            package(
                root,
                "Queue",
                "source",
                f"# Source\nPackage ID: {source_id}\n"
                f"Prerequisite: {target_id} | handoff\n",
            )

            value = json.loads(catalog.scan_catalog(root))
            edge = value["entries"][0]["relationship"]["prerequisites"][0]
            self.assertEqual("missing_target", edge["reason"])
            self.assertEqual("unknown", edge["resolved_state"])
            self.assertIsNone(edge["observed_state"])
            self.assertIsNone(edge["observed_evidence_ref"])

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
                f"# Queue\nPackage ID: {ids[1]}\nPrerequisite: {ids[5]} | release\n",
                encoding="utf-8",
            )
            target_anchor = root / "Fictional" / "Done" / "done" / "spec.md"
            target_anchor.write_text(
                f"# Done\nPackage ID: {ids[5]}\nClaim: release | satisfied | sha256:{'a' * 64}\n",
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
                    self.assertEqual("satisfied", queue["relationship"]["prerequisites"][0]["resolved_state"])
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

    def test_completion_edges_follow_literal_policy_and_folder_moves(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_id = "11111111-1111-4111-8111-111111111111"
            target_id = "22222222-2222-4222-8222-222222222222"
            package(
                root,
                "Queue",
                "source",
                f"# Source\nPackage ID: {source_id}\n"
                f"Completion Prerequisite: {target_id}\n"
                f"Claim: release | unsatisfied\n",
            )
            target = package(
                root,
                "Ready",
                "target",
                f"# Target\nPackage ID: {target_id}\n"
                f"Claim: release | satisfied | sha256:{'a' * 64}\n",
            )

            def source_entry():
                value = json.loads(
                    catalog.scan_catalog(
                        root,
                        completed_stage_names=["Done"],
                        configuration_revision="revision-1",
                    )
                )
                return value, next(
                    entry for entry in value["entries"] if entry["package_id"] == source_id
                )

            value, source_entry_value = source_entry()
            edge = source_entry_value["relationship"]["prerequisites"][0]
            self.assertEqual(SCHEMA_VERSION, value["schema_version"])
            self.assertEqual("revision-1", value["configuration_revision"])
            self.assertEqual(
                {
                    "kind": "completion",
                    "target_package_id": target_id,
                    "observed_stage": "Ready",
                    "resolved_state": "unsatisfied",
                    "reason": "completion_unsatisfied",
                },
                edge,
            )
            self.assertEqual("unsatisfied", source_entry_value["relationship"]["direct_prerequisite_state"])

            (root / "Fictional" / "Done").mkdir()
            target.rename(root / "Fictional" / "Done" / "target")
            value, source_entry_value = source_entry()
            edge = source_entry_value["relationship"]["prerequisites"][0]
            self.assertEqual("Done", edge["observed_stage"])
            self.assertEqual("satisfied", edge["resolved_state"])
            self.assertEqual("completion_satisfied", edge["reason"])
            self.assertEqual("satisfied", source_entry_value["relationship"]["direct_prerequisite_state"])
            self.assertEqual("release", next(iter(source_entry_value["relationship"]["claims"]))["name"])

            (root / "Fictional" / "Done" / "target").rename(target)
            _, source_entry_value = source_entry()
            edge = source_entry_value["relationship"]["prerequisites"][0]
            self.assertEqual("Ready", edge["observed_stage"])
            self.assertEqual("unsatisfied", edge["resolved_state"])

    def test_completion_policy_and_typed_schema_are_strict_and_safe(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            target_id = "22222222-2222-4222-8222-222222222222"
            source_id = "11111111-1111-4111-8111-111111111111"
            package(
                root,
                "Queue",
                "source",
                f"# Source\nPackage ID: {source_id}\nCompletion Prerequisite: {target_id}\n",
            )
            package(root, "Done", "target", f"# Target\nPackage ID: {target_id}\n")
            missing_policy = json.loads(catalog.scan_catalog(root))
            missing_edge = missing_policy["entries"][-1]["relationship"]["prerequisites"][0]
            self.assertEqual("completion_policy_needed", missing_edge["reason"])
            self.assertEqual("unknown", missing_edge["resolved_state"])
            invalid_policy = json.loads(
                catalog.scan_catalog(root, completed_stage_names=["Done", "Done"])
            )
            invalid_edge = invalid_policy["entries"][-1]["relationship"]["prerequisites"][0]
            self.assertEqual("completion_policy_invalid", invalid_edge["reason"])
            self.assertEqual("unknown", invalid_edge["resolved_state"])

            parsed = parse_catalog(missing_policy)
            self.assertIsNone(parsed.configuration_revision)
            schema_four = dict(missing_policy, schema_version=4)
            schema_four["catalog_digest"] = canonical_digest(schema_four)
            with self.assertRaises(ValueError):
                parse_catalog(schema_four)
            malformed = json.loads(catalog.scan_catalog(root, completed_stage_names=["Done"]))
            typed_edge = malformed["entries"][-1]["relationship"]["prerequisites"][0]
            typed_edge["kind"] = "claim"
            malformed["catalog_digest"] = canonical_digest(malformed)
            with self.assertRaises(ValueError):
                parse_catalog(malformed)

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
        f"# Under\nPackage ID: {ORACLE_IDS[0]}\nProgram Membership: {ORACLE_PROGRAM_ID}\n"
        f"Claim: design | satisfied | sha256:{'a' * 64}\n",
        f"# Queue\nPackage ID: {ORACLE_IDS[1]}\nProgram Membership: {ORACLE_PROGRAM_ID}\n"
        f"Prerequisite: {ORACLE_IDS[2]} | release\nPrerequisite: malformed\n"
        f"Superseded By: {ORACLE_IDS[2]}\nClaim: release | unsatisfied\n"
        "Claim: Bad Name | satisfied\n",
        f"# Progress\nPackage ID: {ORACLE_IDS[2]}\n"
        f"Claim: release | satisfied | sha256:{'b' * 64}\n"
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
        assert queue["relationship"]["claims"] == [{"name": "release", "state": "unsatisfied", "evidence_ref": None}]
        assert {item["reason"] for item in queue["relationship"]["prerequisites"]} == {"claim_satisfied", "invalid_prerequisite"}
        assert queue["relationship"]["program"]["resolution"] == "resolved"
        assert queue["relationship"]["superseded_by"]["resolution"] == "resolved"
        assert set(queue["relationship"]["superseded_by"]) == {"package_id", "resolution"}
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


def test_invalid_claim_forms_keep_resolution_marker_private(tmp_path):
    target_id, source_id = ORACLE_IDS[:2]
    for index, rows in enumerate([
        "Claim: release | satisfied | sha256:bad\n",
        "Claim: release | maybe\n",
        "Claim: release | satisfied | sha256:" + "a" * 64 + "\nClaim: release | unknown\n",
    ]):
        root = tmp_path / str(index)
        package(root, "Queue", "target", f"# Target\nPackage ID: {target_id}\n" + rows)
        package(root, "Queue", "source", f"# Source\nPackage ID: {source_id}\nPrerequisite: {target_id} | release\n")
        value = json.loads(catalog.scan_catalog(root))
        source, target = value["entries"]
        assert target["relationship"]["claims"] == [{"name": "release", "state": "unknown", "evidence_ref": None}]
        edge = source["relationship"]["prerequisites"][0]
        assert (edge["reason"], edge["resolved_state"]) == ("invalid_claim", "unknown")
        assert parse_catalog(value).as_dict() == value


def test_core_headers_and_exact_wire_shapes_include_default_relationships(tmp_path):
    from nyx.models import DIAGNOSTIC_CODES

    target_id, source_id, program_id = ORACLE_IDS[:3]
    descriptor = tmp_path / "Fictional" / "Reference" / "Programs" / program_id
    descriptor.mkdir(parents=True)
    (descriptor / "program.md").write_text(f"Program ID: {program_id}\nProgram Title: Core\n", encoding="utf-8")
    package(tmp_path, "Done", "target", f"# Target\nPackage ID: {target_id}\nClaim: release | satisfied | sha256:" + "a" * 64 + "\n")
    package(tmp_path, "Queue", "source", f"# Source\nPackage ID: {source_id}\nTarget repo: /work/core\nStatus: ready\nPrerequisite: {target_id} | release\nCompletion Prerequisite: {target_id}\nProgram Membership: {program_id}\nSuperseded By: {target_id}\n")
    value = json.loads(catalog.scan_catalog(tmp_path, completed_stage_names=["Done"]))
    source = value["entries"][1]
    assert source["declared"] == {"title": "Source", "target_project": "core", "status": "ready"}
    assert source["package_id"] == source_id
    assert {edge["reason"] for edge in source["relationship"]["prerequisites"]} == {"claim_satisfied", "completion_satisfied"}
    assert source["relationship"]["program"] == {"program_id": program_id, "title": "Core", "resolution": "resolved"}
    assert source["relationship"]["superseded_by"] == {"package_id": target_id, "resolution": "resolved"}
    assert value["entries"][0]["relationship"]["claims"] == [{"name": "release", "state": "satisfied", "evidence_ref": "sha256:" + "a" * 64}]
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
        assert set(relationship) == {"participation", "claims", "prerequisites", "direct_prerequisite_state", "program", "superseded_by"}
        for claim in relationship["claims"]:
            assert set(claim) == {"name", "state", "evidence_ref"}
        assert set(relationship["program"]) == {"program_id", "title", "resolution"}
        assert set(relationship["superseded_by"]) == {"package_id", "resolution"}
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
        "claim": b"Claim: " + long_value + b"\n",
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
