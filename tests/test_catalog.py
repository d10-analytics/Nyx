import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from nyx import catalog

V1 = """# Example
Status: approved
Closure: approved
Sanity Recommendation: IMPLEMENT
Human Sanity Decision: AFFIRMED
Target repo: /fictional/repo
"""


def package(root: Path, stage: str, name: str, content: str = V1) -> Path:
    path = root / "Fictional" / stage / name
    path.mkdir(parents=True)
    path.joinpath("spec.md").write_text(content, encoding="utf-8")
    return path


class CatalogTests(TestCase):
    def test_v1_is_deterministic_and_declared_only(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package(root, "Queue", "zeta", V1.replace("# Example", "# Ω"))
            package(root, "Under_Development", "alpha", "# Alpha\nbody secret")
            first = catalog.scan_catalog(root, version=1)
            second = catalog.scan_catalog(root, version=1)
            self.assertEqual(first, second)
            value = json.loads(first)
            self.assertEqual(["Fictional/Queue/zeta", "Fictional/Under_Development/alpha"],
                             [entry["package_path"] for entry in value["entries"]])
            self.assertEqual("Ω", value["entries"][0]["declared"]["title"])
            self.assertNotIn("body secret", first)
            digest_input = dict(value)
            digest_input.pop("catalog_digest")
            expected = catalog.sha256(
                json.dumps(digest_input, ensure_ascii=True, sort_keys=True,
                          separators=(",", ":")).encode()
            ).hexdigest()
            self.assertEqual(expected, value["catalog_digest"])

    def test_v2_relationships_are_ordered_and_digest_tracks_declared_changes(self) -> None:
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
            first = json.loads(catalog.scan_catalog(root, version=2))
            source = next(entry for entry in first["entries"]
                          if entry["package_path"].endswith("/source"))
            edge = source["relationship"]["prerequisites"][0]
            self.assertEqual("satisfied", edge["resolved_state"])
            self.assertEqual("claim_satisfied", edge["reason"])
            original_digest = first["catalog_digest"]
            anchor = root / "Fictional" / "Queue" / "source" / "spec.md"
            anchor.write_text(anchor.read_text(encoding="utf-8").replace("# Source", "# Changed"),
                              encoding="utf-8")
            changed = json.loads(catalog.scan_catalog(root, version=2))
            self.assertNotEqual(original_digest, changed["catalog_digest"])
            changed_source = next(entry for entry in changed["entries"]
                                  if entry["package_path"].endswith("/source"))
            self.assertEqual("Changed", changed_source["declared"]["title"])

    def test_v2_missing_and_ambiguous_targets_are_safe(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            target_id = "22222222-2222-4222-8222-222222222222"
            source_id = "11111111-1111-4111-8111-111111111111"
            package(root, "Queue", "source",
                    f"# Source\nPackage ID: {source_id}\n"
                    f"Prerequisite: {target_id} | handoff\n")
            package(root, "Done", "target-a", f"# A\nPackage ID: {target_id}\n")
            package(root, "Archive", "target-b", f"# B\nPackage ID: {target_id}\n")
            value = json.loads(catalog.scan_catalog(root, version=2))
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
            value = json.loads(catalog.scan_catalog(root, version=2))
            unreadable.joinpath("spec.md").chmod(0o600)
            entries = {entry["package_path"]: entry for entry in value["entries"]}
            self.assertEqual("invalid_package_id",
                             entries["Fictional/Queue/malformed"]["diagnostics"][0]["code"])
            self.assertEqual("unreadable_anchor",
                             entries["Fictional/Queue/unreadable"]["diagnostics"][0]["code"])
            self.assertNotIn(str(malformed), json.dumps(value))

    def test_private_callback_receives_captured_lines_once_and_no_shared_import(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            anchor = package(root, "Queue", "callback", V1)
            calls: list[tuple[Path, list[str]]] = []

            def full_validity(package_path: Path, lines: list[str]) -> bool:
                calls.append((package_path, lines))
                return False

            original_read = Path.read_bytes
            with patch.object(
                Path, "read_bytes", autospec=True,
                side_effect=lambda path: original_read(path),
            ) as read_bytes:
                result = catalog.scan_catalog(root, version=1, _full_validity=full_validity)
            self.assertEqual(1, read_bytes.call_count)
            self.assertEqual(anchor, calls[0][0])
            self.assertEqual(V1.splitlines(), calls[0][1])
            self.assertEqual("complete", json.loads(result)["entries"][0]["state"])

    def test_private_callback_receives_nonempty_malformed_v1_capture(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            anchor = package(root, "Queue", "malformed", "Status: approved\nbody\n")
            calls: list[tuple[Path, list[str]]] = []

            def full_validity(package_path: Path, lines: list[str]) -> bool:
                calls.append((package_path, lines))
                return False

            result = catalog.scan_catalog(root, version=1, _full_validity=full_validity)

            self.assertEqual([(anchor, ["Status: approved", "body"])], calls)
            self.assertEqual(
                "partial",
                json.loads(result)["entries"][0]["state"],
            )

    def test_public_builders_do_not_accept_full_validity_hooks(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package(root, "Queue", "one", V1)
            for builder in (catalog.build_catalog, catalog.build_catalog_v2):
                with self.subTest(builder=builder.__name__), self.assertRaises(TypeError):
                    builder(root, lambda _path, _lines: False)

    def test_malformed_v1_callback_and_catalog_diagnostic_are_single_pass(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            anchor = package(root, "Queue", "malformed", "Status: approved\nbody\n")
            calls: list[list[str]] = []

            def full_validity(_package_path: Path, lines: list[str]) -> bool:
                calls.append(lines)
                return True

            value = json.loads(
                catalog.scan_catalog(root, version=1, _full_validity=full_validity)
            )
            self.assertEqual([["Status: approved", "body"]], calls)
            entry = value["entries"][0]
            self.assertEqual(anchor.as_posix(), (root / entry["package_path"]).as_posix())
            self.assertEqual(
                ["invalid_package"],
                [item["code"] for item in entry["diagnostics"]],
            )

    def test_output_bound_is_enforced(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package(root, "Queue", "one", V1)
            with patch.object(catalog, "MAX_OUTPUT_BYTES", 10), self.assertRaisesRegex(
                ValueError, "exceeds 2 MiB"
            ):
                catalog.scan_catalog(root, version=1)

    def test_output_bound_is_enforced_for_both_wire_versions(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package(root, "Queue", "one", V1)
            for version in (1, 2):
                with self.subTest(version=version), patch.object(
                    catalog, "MAX_OUTPUT_BYTES", 10
                ), self.assertRaisesRegex(ValueError, "exceeds 2 MiB"):
                    catalog.scan_catalog(root, version=version)

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
                value = json.loads(catalog.scan_catalog(root, version=1))
            self.assertEqual("product-checkout", value["entries"][0]["declared"]["target_project"])

    def test_v2_missing_prerequisite_is_unknown_without_target_payload(self) -> None:
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

            value = json.loads(catalog.scan_catalog(root, version=2))
            edge = value["entries"][0]["relationship"]["prerequisites"][0]
            self.assertEqual("missing_target", edge["reason"])
            self.assertEqual("unknown", edge["resolved_state"])
            self.assertIsNone(edge["observed_state"])
            self.assertIsNone(edge["observed_evidence_ref"])
