import inspect
import json
import os
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from nyx import catalog
from nyx.models import parse_catalog

V1 = """# Example
Status: approved
Closure: approved
Sanity Recommendation: IMPLEMENT
Human Sanity Decision: AFFIRMED
Target repo: /fictional/repo
"""

PROGRAM_ID = "88888888-8888-4888-8888-888888888888"
SMALL_ORACLE_BYTES = "{\"catalog_digest\":\"e58865c903fe64fe60473a420098a7f1445a05a397ade8e8ab5e8fe070d43abb\",\"discovery_diagnostics\":[],\"entries\":[{\"board_visible\":true,\"declared\":{\"closure\":null,\"human_sanity_decision\":null,\"sanity_recommendation\":null,\"status\":null,\"target_project\":null,\"title\":\"One\"},\"diagnostics\":[],\"package_id\":null,\"package_path\":\"Fictional/Queue/one\",\"project\":\"Fictional\",\"relationship\":{\"claims\":[],\"direct_prerequisite_state\":\"relationship_unavailable\",\"participation\":\"legacy\",\"prerequisites\":[],\"program\":{\"diagnostics\":[],\"program_id\":null,\"resolution\":\"not_declared\",\"title\":null},\"superseded_by\":{\"diagnostics\":[],\"package_id\":null,\"resolution\":\"not_declared\"}},\"stage\":\"Queue\",\"state\":\"complete\",\"transitive_diagnostics\":[]}],\"identity_coverage\":{\"diagnostics\":[],\"state\":\"complete\"},\"program_coverage\":{\"diagnostics\":[],\"state\":\"complete\"},\"programs\":[],\"schema_version\":3,\"visibility\":{\"hidden_entry_count\":0,\"hidden_stages\":[\"Archive\",\"Done\",\"In_Progress\"],\"visible_entry_count\":1}}"
COMPLETE_ORACLE_BYTES = "{\"catalog_digest\":\"c1f2679d0619e9c36c1db0f4db6a4d9d35818ea89223e145d0209179da404881\",\"discovery_diagnostics\":[],\"entries\":[{\"board_visible\":true,\"declared\":{\"closure\":null,\"human_sanity_decision\":null,\"sanity_recommendation\":null,\"status\":null,\"target_project\":null,\"title\":\"Queue\"},\"diagnostics\":[],\"package_id\":\"11111111-1111-4111-8111-111111111111\",\"package_path\":\"Fictional/Queue/queue\",\"project\":\"Fictional\",\"relationship\":{\"claims\":[{\"diagnostics\":[],\"evidence_ref\":\"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"name\":\"design\",\"state\":\"satisfied\"}],\"direct_prerequisite_state\":\"no_declared_prerequisites\",\"participation\":\"available\",\"prerequisites\":[],\"program\":{\"diagnostics\":[],\"program_id\":\"88888888-8888-4888-8888-888888888888\",\"resolution\":\"resolved\",\"title\":\"Core\"},\"superseded_by\":{\"diagnostics\":[],\"package_id\":null,\"resolution\":\"not_declared\"}},\"stage\":\"Queue\",\"state\":\"complete\",\"transitive_diagnostics\":[]}],\"identity_coverage\":{\"diagnostics\":[],\"state\":\"complete\"},\"program_coverage\":{\"diagnostics\":[],\"state\":\"complete\"},\"programs\":[{\"diagnostics\":[],\"member_package_ids\":[\"11111111-1111-4111-8111-111111111111\"],\"program_id\":\"88888888-8888-4888-8888-888888888888\",\"title\":\"Core\"}],\"schema_version\":3,\"visibility\":{\"hidden_entry_count\":0,\"hidden_stages\":[\"Archive\",\"Done\",\"In_Progress\"],\"visible_entry_count\":1}}"


def package(root: Path, stage: str, name: str, content: str = V1) -> Path:
    path = root / "Fictional" / stage / name
    path.mkdir(parents=True)
    path.joinpath("spec.md").write_text(content, encoding="utf-8")
    return path


class CatalogTests(TestCase):
    def test_descriptor_relative_package_boundaries_survive_replacement(self) -> None:
        for boundary in ("root", "repository", "stage", "package"):
            with self.subTest(boundary=boundary), TemporaryDirectory() as temporary:
                base = Path(temporary)
                root = base / "specs"
                package_path = package(root, "Queue", "inside", "# Inside\n")
                outside = base / "outside"
                outside_package = outside / "Queue" / "escape"
                outside_package.mkdir(parents=True)
                outside_package.joinpath("spec.md").write_text("# Escaped\n", encoding="utf-8")
                original_open = os.open
                replaced = False

                def guarded_open(path, flags, mode=0o777, *, dir_fd=None):
                    nonlocal replaced
                    descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
                    target = {
                        "root": root,
                        "repository": "Fictional",
                        "stage": "Queue",
                        "package": "inside",
                    }[boundary]
                    if not replaced and path == target:
                        if boundary == "root":
                            root.rename(base / "original-root")
                            root.symlink_to(outside, target_is_directory=True)
                        elif boundary == "repository":
                            repository = root / "Fictional"
                            repository.rename(base / "original-repository")
                            repository.symlink_to(outside, target_is_directory=True)
                        elif boundary == "stage":
                            stage = root / "Fictional" / "Queue"
                            stage.rename(base / "original-stage")
                            stage.symlink_to(outside / "Queue", target_is_directory=True)
                        else:
                            package_path.rename(base / "original-package")
                            package_path.symlink_to(outside_package, target_is_directory=True)
                        replaced = True
                    return descriptor

                with patch.object(catalog.os, "open", side_effect=guarded_open):
                    value = json.loads(catalog.scan_catalog(root))
                self.assertTrue(replaced)
                self.assertNotIn("Escaped", json.dumps(value))
                self.assertEqual("Inside", value["entries"][0]["declared"]["title"])

    def test_descriptor_relative_program_boundaries_survive_replacement(self) -> None:
        for boundary in ("reference", "programs", "uuid", "anchor"):
            with self.subTest(boundary=boundary), TemporaryDirectory() as temporary:
                base = Path(temporary)
                root = base / "specs"
                package(
                    root,
                    "Queue",
                    "one",
                    f"# Package\nProgram Membership: {PROGRAM_ID}\n",
                )
                repository = root / "Fictional"
                program_dir = repository / "Reference" / "Programs" / PROGRAM_ID
                program_dir.mkdir(parents=True)
                program_dir.joinpath("program.md").write_text(
                    f"Program ID: {PROGRAM_ID}\nProgram Title: Inside\n", encoding="utf-8"
                )
                outside = base / "outside"
                outside_dir = outside / "Reference" / "Programs" / PROGRAM_ID
                outside_dir.mkdir(parents=True)
                outside_dir.joinpath("program.md").write_text(
                    f"Program ID: {PROGRAM_ID}\nProgram Title: Escaped\n", encoding="utf-8"
                )
                outside_anchor = outside / "program.md"
                outside_anchor.write_text(
                    f"Program ID: {PROGRAM_ID}\nProgram Title: Escaped\n", encoding="utf-8"
                )
                original_open = os.open
                replaced = False

                def guarded_open(path, flags, mode=0o777, *, dir_fd=None):
                    nonlocal replaced
                    descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
                    if not replaced and path == {
                        "reference": "Reference",
                        "programs": "Programs",
                        "uuid": PROGRAM_ID,
                        "anchor": "program.md",
                    }[boundary]:
                        if boundary == "reference":
                            (repository / "Reference").rename(base / "original-reference")
                            (repository / "Reference").symlink_to(
                                outside / "Reference", target_is_directory=True
                            )
                        elif boundary == "programs":
                            (repository / "Reference" / "Programs").rename(base / "original-programs")
                            (repository / "Reference" / "Programs").symlink_to(
                                outside / "Reference" / "Programs", target_is_directory=True
                            )
                        elif boundary == "uuid":
                            program_dir.rename(base / "original-program")
                            program_dir.symlink_to(outside_dir, target_is_directory=True)
                        else:
                            program_dir.joinpath("program.md").rename(base / "original-program.md")
                            program_dir.joinpath("program.md").symlink_to(outside_anchor)
                        replaced = True
                    return descriptor

                with patch.object(catalog.os, "open", side_effect=guarded_open):
                    value = json.loads(catalog.scan_catalog(root))
                self.assertTrue(replaced)
                self.assertNotIn("Escaped", json.dumps(value))
                self.assertEqual("Inside", value["programs"][0]["title"])

    def test_group_and_spec_anchor_interceptions_use_expected_parent_descriptors(self) -> None:
        for boundary in ("group", "spec.md"):
            with self.subTest(boundary=boundary), TemporaryDirectory() as temporary:
                base = Path(temporary)
                root = base / "specs"
                package_path = package(root, "Queue", "group", "# Inside\n")
                if boundary == "group":
                    outside = base / "outside" / "escape"
                    outside.mkdir(parents=True)
                    outside.joinpath("spec.md").write_text("# Escaped\n", encoding="utf-8")
                else:
                    outside = base / "outside-spec.md"
                    outside.write_text("# Escaped\n", encoding="utf-8")
                original_open = os.open
                intercepted_parent_fds: list[int | None] = []
                replaced = False

                def guarded_open(path, flags, mode=0o777, *, dir_fd=None):
                    nonlocal replaced
                    descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
                    if not replaced and path == boundary:
                        intercepted_parent_fds.append(dir_fd)
                        self.assertIsNotNone(dir_fd)
                        if boundary == "group":
                            package_path.rename(base / "original-group")
                            package_path.symlink_to(outside, target_is_directory=True)
                        else:
                            anchor = package_path / "spec.md"
                            anchor.rename(base / "original-spec.md")
                            anchor.symlink_to(outside)
                        replaced = True
                    return descriptor

                with patch.object(catalog.os, "open", side_effect=guarded_open):
                    value = json.loads(catalog.scan_catalog(root))
                self.assertTrue(replaced)
                self.assertEqual([value for value in intercepted_parent_fds if value is not None], intercepted_parent_fds)
                self.assertNotIn("Escaped", json.dumps(value))

    def test_program_object_failures_produce_honest_incomplete_coverage(self) -> None:
        cases = ("missing", "symlink", "nonregular", "unreadable", "changed")
        expected_codes = {
            "missing": "unreadable_anchor",
            "symlink": "nonregular_anchor",
            "nonregular": "nonregular_anchor",
            "unreadable": "unreadable_anchor",
            "changed": "changed_during_read",
        }
        for case in cases:
            with self.subTest(case=case), TemporaryDirectory() as temporary:
                root = Path(temporary)
                package(
                    root,
                    "Queue",
                    "one",
                    f"# Package\nProgram Membership: {PROGRAM_ID}\n",
                )
                program_dir = root / "Fictional" / "Reference" / "Programs" / PROGRAM_ID
                program_dir.mkdir(parents=True)
                descriptor = program_dir / "program.md"
                descriptor.write_text(
                    f"Program ID: {PROGRAM_ID}\nProgram Title: Inside\n", encoding="utf-8"
                )
                if case == "missing":
                    descriptor.unlink()
                elif case == "symlink":
                    descriptor.unlink()
                    target = root / "outside-program.md"
                    target.write_text(
                        f"Program ID: {PROGRAM_ID}\nProgram Title: Escaped\n", encoding="utf-8"
                    )
                    descriptor.symlink_to(target)
                elif case == "nonregular":
                    descriptor.unlink()
                    os.mkfifo(descriptor)
                elif case == "unreadable":
                    descriptor.chmod(0)

                original_open = os.open
                original_fstat = os.fstat
                program_fds: set[int] = set()
                fstat_counts: dict[int, int] = {}

                def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
                    descriptor_fd = original_open(path, flags, mode, dir_fd=dir_fd)
                    if path == "program.md":
                        program_fds.add(descriptor_fd)
                    return descriptor_fd

                def changing_fstat(descriptor_fd):
                    info = original_fstat(descriptor_fd)
                    if case == "changed" and descriptor_fd in program_fds:
                        fstat_counts[descriptor_fd] = fstat_counts.get(descriptor_fd, 0) + 1
                        if fstat_counts[descriptor_fd] % 2 == 0:
                            values = list(info)
                            values[8] += 1
                            return os.stat_result(values)
                    return info

                with patch.object(catalog.os, "open", side_effect=tracked_open), patch.object(
                    catalog.os, "fstat", side_effect=changing_fstat
                ):
                    value = json.loads(catalog.scan_catalog(root))
                self.assertEqual("incomplete", value["program_coverage"]["state"])
                self.assertEqual([], value["programs"])
                self.assertIn(
                    expected_codes[case],
                    {item["code"] for item in value["program_coverage"]["diagnostics"]},
                )
                relationship = value["entries"][0]["relationship"]["program"]
                self.assertEqual("unknown", relationship["resolution"])
                self.assertIsNone(relationship["title"])

    def test_descriptor_capabilities_fail_without_path_fallback(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package(root, "Queue", "one")
            with patch.object(catalog.os, "O_NOFOLLOW", None), patch.object(
                Path, "read_bytes", side_effect=AssertionError("pathname fallback")
            ):
                with self.assertRaises(ValueError):
                    catalog.scan_catalog(root)
            for flag_name in ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC"):
                with patch.object(catalog.os, flag_name, 0):
                    with self.assertRaises(ValueError):
                        catalog.scan_catalog(root)

            original_open = os.open

            def unsupported_dir_fd(path, flags, mode=0o777, *, dir_fd=None):
                if dir_fd is not None:
                    raise TypeError("dir_fd unsupported")
                return original_open(path, flags, mode)

            with patch.object(catalog.os, "open", side_effect=unsupported_dir_fd):
                with self.assertRaises(ValueError):
                    catalog.scan_catalog(root)

            def not_implemented_dir_fd(path, flags, mode=0o777, *, dir_fd=None):
                if dir_fd is not None:
                    raise NotImplementedError("dir_fd unsupported")
                return original_open(path, flags, mode)

            with patch.object(catalog.os, "open", side_effect=not_implemented_dir_fd):
                with self.assertRaises(ValueError):
                    catalog.scan_catalog(root)

    def test_descriptor_session_closes_on_parser_exception_and_concurrent_scans_overlap(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package(root, "Queue", "one")
            original_open, original_close = os.open, os.close
            opened: list[int] = []
            closed: list[int] = []

            def tracked_open(*args, **kwargs):
                descriptor = original_open(*args, **kwargs)
                opened.append(descriptor)
                return descriptor

            def tracked_close(descriptor):
                closed.append(descriptor)
                return original_close(descriptor)

            with patch.object(catalog.os, "open", side_effect=tracked_open), patch.object(
                catalog.os, "close", side_effect=tracked_close
            ), patch.object(catalog, "_parse_header", side_effect=RuntimeError("parser")):
                with self.assertRaisesRegex(RuntimeError, "parser"):
                    catalog.scan_catalog(root)
            self.assertEqual(set(opened), set(closed))

            roots = []
            for title in ("First", "Second"):
                scan_root = root / title
                package(scan_root, "Queue", "one", f"# {title}\n")
                roots.append(scan_root)
            barrier = threading.Barrier(2)
            original_root_open = os.open
            admissions: list[tuple[int, int]] = []
            opened_by_thread: dict[int, list[int]] = {}
            closed_by_thread: dict[int, list[int]] = {}
            thread_errors: list[BaseException] = []

            def synchronized_open(path, flags, mode=0o777, *, dir_fd=None):
                descriptor = original_root_open(path, flags, mode, dir_fd=dir_fd)
                owner = threading.get_ident()
                opened_by_thread.setdefault(owner, []).append(descriptor)
                if dir_fd is None and path in roots:
                    admissions.append((owner, descriptor))
                    barrier.wait(timeout=5)
                return descriptor

            def synchronized_close(descriptor: int) -> None:
                closed_by_thread.setdefault(threading.get_ident(), []).append(descriptor)
                original_close(descriptor)

            results: list[dict[str, object]] = []

            def scan(scan_root: Path) -> None:
                try:
                    results.append(json.loads(catalog.scan_catalog(scan_root)))
                except BaseException as error:
                    thread_errors.append(error)

            with patch.object(catalog.os, "open", side_effect=synchronized_open), patch.object(
                catalog.os, "close", side_effect=synchronized_close
            ):
                threads = [threading.Thread(target=scan, args=(scan_root,)) for scan_root in roots]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)
            self.assertEqual([], thread_errors)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(2, len(admissions))
            self.assertEqual(2, len({descriptor for _owner, descriptor in admissions}))
            self.assertEqual({"First", "Second"}, {result["entries"][0]["declared"]["title"] for result in results})
            for owner, descriptor in admissions:
                self.assertIn(descriptor, closed_by_thread.get(owner, []))
            self.assertEqual(
                {descriptor for values in opened_by_thread.values() for descriptor in values},
                {descriptor for values in closed_by_thread.values() for descriptor in values},
            )

    def test_both_public_producers_retain_fixed_schema_three_oracle_without_writes(self) -> None:
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

    def test_both_public_producers_match_fixed_complete_graph_schema_three_oracle(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package(
                root,
                "Queue",
                "queue",
                f"# Queue\nPackage ID: 11111111-1111-4111-8111-111111111111\n"
                f"Program Membership: {PROGRAM_ID}\n"
                f"Claim: design | satisfied | sha256:{'a' * 64}\n",
            )
            descriptor = root / "Fictional" / "Reference" / "Programs" / PROGRAM_ID
            descriptor.mkdir(parents=True)
            descriptor.joinpath("program.md").write_text(
                f"Program ID: {PROGRAM_ID}\nProgram Title: Core\n", encoding="utf-8"
            )
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
            self.assertEqual(3, value["schema_version"])
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

    def test_stage_root_anchor_round_trips_through_schema_three_parser(self) -> None:
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
        self.assertEqual(
            "(spec_root: 'Path') -> 'str'",
            str(__import__("inspect").signature(catalog.build_catalog)),
        )
        self.assertEqual(
            "(spec_root: 'Path') -> 'str'",
            str(inspect.signature(catalog.scan_catalog)),
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
            first = catalog.build_catalog(root)
            second = catalog.build_catalog(root)
            self.assertEqual(first.encode("utf-8"), second.encode("utf-8"))
            value = json.loads(first)
            self.assertEqual(3, value["schema_version"])
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
            self.assertIn("Fictional/Done/done", {entry["package_path"] for entry in value["entries"]})
            queue = next(entry for entry in value["entries"] if entry["stage"] == "Queue")
            self.assertEqual("satisfied", queue["relationship"]["prerequisites"][0]["resolved_state"])

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
        assert value["schema_version"] == 3
        assert set(value) == {
            "schema_version", "catalog_digest", "visibility", "identity_coverage",
            "program_coverage", "discovery_diagnostics", "entries", "programs",
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
