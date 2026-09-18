import inspect
import json
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from nyx import catalog
from nyx.models import canonical_digest, parse_catalog

V1 = """# Example
Status: approved
Closure: approved
Sanity Recommendation: IMPLEMENT
Human Sanity Decision: AFFIRMED
Target repo: /fictional/repo
"""

PROGRAM_ID = "88888888-8888-4888-8888-888888888888"
SMALL_ORACLE_BYTES = "{\"catalog_digest\":\"e58865c903fe64fe60473a420098a7f1445a05a397ade8e8ab5e8fe070d43abb\",\"discovery_diagnostics\":[],\"entries\":[{\"board_visible\":true,\"declared\":{\"closure\":null,\"human_sanity_decision\":null,\"sanity_recommendation\":null,\"status\":null,\"target_project\":null,\"title\":\"One\"},\"diagnostics\":[],\"package_id\":null,\"package_path\":\"Fictional/Queue/one\",\"project\":\"Fictional\",\"relationship\":{\"claims\":[],\"direct_prerequisite_state\":\"relationship_unavailable\",\"participation\":\"legacy\",\"prerequisites\":[],\"program\":{\"diagnostics\":[],\"program_id\":null,\"resolution\":\"not_declared\",\"title\":null},\"superseded_by\":{\"diagnostics\":[],\"package_id\":null,\"resolution\":\"not_declared\"}},\"stage\":\"Queue\",\"state\":\"complete\",\"transitive_diagnostics\":[]}],\"identity_coverage\":{\"diagnostics\":[],\"state\":\"complete\"},\"program_coverage\":{\"diagnostics\":[],\"state\":\"complete\"},\"programs\":[],\"schema_version\":3,\"visibility\":{\"hidden_entry_count\":0,\"hidden_stages\":[\"Archive\",\"Done\",\"In_Progress\"],\"visible_entry_count\":1}}"
COMPLETE_ORACLE_BYTES = '{"catalog_digest":"f1040b4367b54ea507ff91e667a8ca6237809f4ecf67aed952650b352d2cb575","discovery_diagnostics":[],"entries":[{"board_visible":true,"declared":{"closure":null,"human_sanity_decision":null,"sanity_recommendation":null,"status":null,"target_project":null,"title":"Retro"},"diagnostics":[],"package_id":"55555555-5555-4555-8555-555555555555","package_path":"Fictional/Awaiting_Retrospective/pkg","project":"Fictional","relationship":{"claims":[],"direct_prerequisite_state":"no_declared_prerequisites","participation":"available","prerequisites":[],"program":{"diagnostics":[],"program_id":null,"resolution":"not_declared","title":null},"superseded_by":{"diagnostics":[],"package_id":null,"resolution":"not_declared"}},"stage":"Awaiting_Retrospective","state":"complete","transitive_diagnostics":[]},{"board_visible":false,"declared":{"closure":null,"human_sanity_decision":null,"sanity_recommendation":null,"status":null,"target_project":null,"title":"Progress"},"diagnostics":[],"package_id":"33333333-3333-4333-8333-333333333333","package_path":"Fictional/In_Progress/pkg","project":"Fictional","relationship":{"claims":[{"diagnostics":[],"evidence_ref":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","name":"release","state":"satisfied"}],"direct_prerequisite_state":"no_declared_prerequisites","participation":"available","prerequisites":[],"program":{"diagnostics":[],"program_id":null,"resolution":"not_declared","title":null},"superseded_by":{"diagnostics":[{"code":"successor_cycle","message":"successor cycle detected: Fictional/In_Progress/pkg"}],"package_id":"22222222-2222-4222-8222-222222222222","resolution":"resolved"}},"stage":"In_Progress","state":"complete","transitive_diagnostics":[]},{"board_visible":true,"declared":{"closure":null,"human_sanity_decision":null,"sanity_recommendation":null,"status":null,"target_project":null,"title":"Fix"},"diagnostics":[],"package_id":"44444444-4444-4444-8444-444444444444","package_path":"Fictional/Needs_Fixes/pkg","project":"Fictional","relationship":{"claims":[],"direct_prerequisite_state":"no_declared_prerequisites","participation":"available","prerequisites":[],"program":{"diagnostics":[],"program_id":null,"resolution":"not_declared","title":null},"superseded_by":{"diagnostics":[],"package_id":null,"resolution":"not_declared"}},"stage":"Needs_Fixes","state":"complete","transitive_diagnostics":[]},{"board_visible":true,"declared":{"closure":null,"human_sanity_decision":null,"sanity_recommendation":null,"status":null,"target_project":null,"title":"Queue"},"diagnostics":[{"code":"invalid_claim","message":"invalid claim: Fictional/Queue/pkg"},{"code":"invalid_prerequisite","message":"invalid prerequisite: Fictional/Queue/pkg"}],"package_id":"22222222-2222-4222-8222-222222222222","package_path":"Fictional/Queue/pkg","project":"Fictional","relationship":{"claims":[{"diagnostics":[],"evidence_ref":null,"name":"release","state":"unsatisfied"}],"direct_prerequisite_state":"unknown","participation":"available","prerequisites":[{"claim_name":null,"observed_evidence_ref":null,"observed_state":null,"reason":"invalid_prerequisite","resolved_state":"unknown","target_package_id":null},{"claim_name":"release","observed_evidence_ref":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","observed_state":"satisfied","reason":"claim_satisfied","resolved_state":"satisfied","target_package_id":"33333333-3333-4333-8333-333333333333"}],"program":{"diagnostics":[],"program_id":"88888888-8888-4888-8888-888888888888","resolution":"resolved","title":"Core"},"superseded_by":{"diagnostics":[{"code":"successor_cycle","message":"successor cycle detected: Fictional/Queue/pkg"}],"package_id":"33333333-3333-4333-8333-333333333333","resolution":"resolved"}},"stage":"Queue","state":"partial","transitive_diagnostics":[]},{"board_visible":true,"declared":{"closure":null,"human_sanity_decision":null,"sanity_recommendation":null,"status":null,"target_project":null,"title":"Under"},"diagnostics":[],"package_id":"11111111-1111-4111-8111-111111111111","package_path":"Fictional/Under_Development/pkg","project":"Fictional","relationship":{"claims":[{"diagnostics":[],"evidence_ref":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","name":"design","state":"satisfied"}],"direct_prerequisite_state":"no_declared_prerequisites","participation":"available","prerequisites":[],"program":{"diagnostics":[],"program_id":"88888888-8888-4888-8888-888888888888","resolution":"resolved","title":"Core"},"superseded_by":{"diagnostics":[],"package_id":null,"resolution":"not_declared"}},"stage":"Under_Development","state":"complete","transitive_diagnostics":[]}],"identity_coverage":{"diagnostics":[],"state":"complete"},"program_coverage":{"diagnostics":[],"state":"complete"},"programs":[{"diagnostics":[],"member_package_ids":["11111111-1111-4111-8111-111111111111","22222222-2222-4222-8222-222222222222"],"program_id":"88888888-8888-4888-8888-888888888888","title":"Core"}],"schema_version":3,"visibility":{"hidden_entry_count":3,"hidden_stages":["Archive","Done","In_Progress"],"visible_entry_count":4}}'


def _migrate_oracle(legacy: str) -> str:
    """Keep the historical graph assertions exact while adding the v4 inventory."""
    value = json.loads(legacy)
    value["schema_version"] = 4
    projects = sorted({entry["project"] for entry in value["entries"]})
    stages = sorted({(entry["project"], entry["stage"]) for entry in value["entries"]})
    if len(value["entries"]) >= 5 and projects == ["Fictional"]:
        stages = sorted(("Fictional", stage) for stage in catalog.CATALOG_LIFECYCLE_DIRECTORIES)
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


class CatalogTests(TestCase):
    def test_public_producers_use_one_portable_scanner_and_reject_root_links(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            outside = base / "outside"
            outside.mkdir()
            (outside / "outside-marker").write_text("outside", encoding="utf-8")
            root = base / "specs"
            root.symlink_to(outside, target_is_directory=True)
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
                    self.assertEqual("nonregular_anchor", anchor_entry["diagnostics"][0]["code"])
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
            os.mkfifo(nonregular / "spec.md")
            unreadable.joinpath("spec.md").chmod(0)

            identity_count = 0
            def changed_identity(info):
                nonlocal identity_count
                identity_count += 1
                return (identity_count, 1, 1, 1, 1, 1, 1)

            with patch.object(catalog, "_catalog_identity", side_effect=changed_identity):
                values = [
                    json.loads(producer(root))
                    for producer in (catalog.build_catalog, catalog.scan_catalog)
                ]
            for value in values:
                entries = {entry["package_path"]: entry for entry in value["entries"]}
                self.assertEqual("nonregular_anchor", entries["Fictional/Queue/fifo"]["diagnostics"][0]["code"])
                self.assertEqual("unreadable_anchor", entries["Fictional/Queue/unreadable"]["diagnostics"][0]["code"])
                self.assertEqual("changed_during_read", entries["Fictional/Queue/changed"]["diagnostics"][0]["code"])
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
                catalog.os, "mknod", side_effect=reject_write_operation
            ), patch.object(catalog.os, "link", side_effect=reject_write_operation), patch.object(
                catalog.os, "rmdir", side_effect=reject_write_operation
            ), patch.object(catalog.os, "remove", side_effect=reject_write_operation), patch.object(
                catalog.os, "fchmod", side_effect=reject_write_operation
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
            self.assertEqual(4, value["schema_version"])
            self.assertEqual(
                {"projects": [{"name": "Fictional", "availability": "complete"}],
                 "stages": [
                     {"project": "Fictional", "stage": stage, "availability": "complete"}
                     for stage in ("Queue", "Under_Development")
                 ]},
                value["inventory"],
            )
            self.assertEqual({"hidden_stages": ["Archive", "Done", "In_Progress"],
                              "visible_entry_count": 2, "hidden_entry_count": 0},
                             value["visibility"])
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

    def test_schema_four_inventory_mutations_are_rejected_before_use(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package(root, "Testing", "custom", "# Custom\n")
            (root / "Fictional" / "Empty").mkdir()
            baseline = json.loads(catalog.scan_catalog(root))

        mutations = (
            lambda value: value.update(schema_version=3),
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
            unreadable.joinpath("spec.md").chmod(0)
            value = json.loads(catalog.scan_catalog(root))
            unreadable.joinpath("spec.md").chmod(0o600)
            entries = {entry["package_path"]: entry for entry in value["entries"]}
            self.assertEqual("invalid_package_id",
                             entries["Fictional/Queue/malformed"]["diagnostics"][0]["code"])
            self.assertEqual("unreadable_anchor",
                             entries["Fictional/Queue/unreadable"]["diagnostics"][0]["code"])
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
            rendered = str(signature).replace(str(catalog._OMITTED), "<omitted>")
            self.assertEqual(
                "(spec_root: 'Path', *, hidden_stages: 'Iterable[str] | object' = <omitted>) -> 'str'",
                rendered,
            )
            self.assertEqual(
                ["spec_root", "hidden_stages"], list(signature.parameters)
            )
            self.assertEqual(
                inspect.Parameter.KEYWORD_ONLY,
                signature.parameters["hidden_stages"].kind,
            )
            self.assertIs(
                catalog._OMITTED, signature.parameters["hidden_stages"].default
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
                            stack.enter_context(
                                patch.object(catalog.os, name, side_effect=reject_write_operation)
                            )
                        rendered = producer(root)

                    self.assertEqual(16, scans.call_count)
                    renders.append(rendered)
                    value = json.loads(rendered)
                    self.assertEqual(4, value["schema_version"])
                    self.assertEqual(4, value["visibility"]["visible_entry_count"])
                    self.assertEqual(3, value["visibility"]["hidden_entry_count"])
                    digest_input = dict(value)
                    digest_input.pop("catalog_digest")
                    self.assertEqual(
                        catalog.sha256(json.dumps(digest_input, sort_keys=True,
                                                   separators=(",", ":")).encode()).hexdigest(),
                        value["catalog_digest"],
                    )
                    self.assertEqual(
                        {"Under_Development", "Queue", "Needs_Fixes", "Awaiting_Retrospective", "Done"},
                        {entry["stage"] for entry in value["entries"]},
                    )
                    self.assertEqual(
                        [
                            "Fictional/Awaiting_Retrospective/awaiting_retrospective",
                            "Fictional/Done/done",
                            "Fictional/Needs_Fixes/needs_fixes",
                            "Fictional/Queue/queue",
                            "Fictional/Under_Development/under_development",
                        ],
                        [entry["package_path"] for entry in value["entries"]],
                    )
                    self.assertEqual(
                        {
                            "Awaiting_Retrospective": True,
                            "Done": False,
                            "Needs_Fixes": True,
                            "Queue": True,
                            "Under_Development": True,
                        },
                        {entry["stage"]: entry["board_visible"] for entry in value["entries"]},
                    )
                    self.assertIn("Fictional/Done/done", {entry["package_path"] for entry in value["entries"]})
                    queue = next(entry for entry in value["entries"] if entry["stage"] == "Queue")
                    self.assertEqual("satisfied", queue["relationship"]["prerequisites"][0]["resolved_state"])
                    self.assertEqual(before, inventory())

            self.assertEqual(renders[0].encode("utf-8"), renders[1].encode("utf-8"))

    def test_each_explicit_policy_controls_rows_counts_booleans_and_digest(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stages = (
                "Under_Development", "Queue", "In_Progress", "Needs_Fixes",
                "Awaiting_Retrospective", "Done", "Archive",
            )
            for stage in stages:
                package(root, stage, stage.lower(), f"# {stage} title\n")

            policies = (
                ("omitted", None, ["Archive", "Done", "In_Progress"], 4, 3),
                ("empty", [], [], 7, 0),
                ("known", ["Done"], ["Done"], 6, 1),
                ("unknown", ["Custom"], ["Custom"], 7, 0),
                ("case-distinct", ["done"], ["done"], 7, 0),
                ("deduplicated", ["Done", "Archive", "Done"], ["Archive", "Done"], 5, 2),
            )
            expected_paths = sorted([
                f"Fictional/{stage}/{stage.lower()}"
                for stage in stages
            ])
            for producer in (catalog.build_catalog, catalog.scan_catalog):
                for label, policy, hidden, visible_count, hidden_count in policies:
                    with self.subTest(producer=producer.__name__, policy=label):
                        kwargs = {} if policy is None else {"hidden_stages": policy}
                        value = json.loads(producer(root, **kwargs))
                        self.assertEqual(hidden, value["visibility"]["hidden_stages"])
                        self.assertEqual(visible_count, value["visibility"]["visible_entry_count"])
                        self.assertEqual(hidden_count, value["visibility"]["hidden_entry_count"])
                        entries = value["entries"]
                        self.assertEqual(
                            [path for path in expected_paths if path.split("/")[1] not in hidden],
                            [entry["package_path"] for entry in entries],
                        )
                        self.assertEqual(
                            {stage: stage not in hidden for stage in stages if stage not in hidden},
                            {entry["stage"]: entry["board_visible"] for entry in entries},
                        )
                        self.assertEqual(
                            {f"{stage} title" for stage in stages if stage not in hidden},
                            {entry["declared"]["title"] for entry in entries},
                        )
                        digest_input = dict(value)
                        digest_input.pop("catalog_digest")
                        self.assertEqual(
                            catalog.sha256(
                                json.dumps(
                                    digest_input,
                                    ensure_ascii=True,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                            ).hexdigest(),
                            value["catalog_digest"],
                        )

    def test_hidden_prerequisite_is_direct_context_and_disconnected_hidden_is_omitted(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_id = "11111111-1111-4111-8111-111111111111"
            target_id = "22222222-2222-4222-8222-222222222222"
            package(
                root,
                "Queue",
                "source",
                f"# Source title\nPackage ID: {source_id}\n"
                f"Prerequisite: {target_id} | release\n",
            )
            package(
                root,
                "Done",
                "target",
                f"# Target title\nPackage ID: {target_id}\n"
                f"Claim: release | satisfied | sha256:{'a' * 64}\n",
            )
            package(root, "Archive", "disconnected", "# Disconnected title\n")
            value = json.loads(catalog.build_catalog(root))
            self.assertEqual(
                ["Fictional/Done/target", "Fictional/Queue/source"],
                [entry["package_path"] for entry in value["entries"]],
            )
            self.assertEqual(
                {"hidden_stages": ["Archive", "Done", "In_Progress"],
                 "visible_entry_count": 1, "hidden_entry_count": 2},
                value["visibility"],
            )
            target = value["entries"][0]
            source = value["entries"][1]
            self.assertEqual("Target title", target["declared"]["title"])
            self.assertFalse(target["board_visible"])
            self.assertEqual("complete", target["state"])
            self.assertEqual("satisfied", source["relationship"]["direct_prerequisite_state"])
            self.assertEqual("satisfied", source["relationship"]["prerequisites"][0]["resolved_state"])
            self.assertEqual("claim_satisfied", source["relationship"]["prerequisites"][0]["reason"])
            digest_input = dict(value)
            digest_input.pop("catalog_digest")
            self.assertEqual(
                catalog.sha256(
                    json.dumps(digest_input, ensure_ascii=True, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                value["catalog_digest"],
            )

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
        assert value["schema_version"] == 4
        assert set(value) == {
            "schema_version", "catalog_digest", "visibility", "identity_coverage",
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
            "Fictional/Awaiting_Retrospective/pkg", "Fictional/In_Progress/pkg",
            "Fictional/Needs_Fixes/pkg", "Fictional/Queue/pkg",
            "Fictional/Under_Development/pkg",
        ]
        assert "Fictional/Archive/pkg" not in {entry["package_path"] for entry in value["entries"]}
        queue = next(entry for entry in value["entries"] if entry["stage"] == "Queue")
        assert {item["code"] for item in queue["diagnostics"]} == {"invalid_claim", "invalid_prerequisite"}
        assert {item["reason"] for item in queue["relationship"]["prerequisites"]} == {"claim_satisfied", "invalid_prerequisite"}
        assert queue["relationship"]["program"]["resolution"] == "resolved"
        assert queue["relationship"]["superseded_by"]["resolution"] == "resolved"
        assert queue["relationship"]["superseded_by"]["diagnostics"][0]["code"] == "successor_cycle"
        assert value["programs"][0]["member_package_ids"] == [ORACLE_IDS[0], ORACLE_IDS[1]]
