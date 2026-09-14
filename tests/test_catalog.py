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
            self.assertEqual(2, value["schema_version"])
            self.assertEqual("Ω", value["entries"][0]["declared"]["title"])
            self.assertNotIn("body secret", first)
            digest_input = dict(value)
            digest_input.pop("catalog_digest")
            expected = catalog.sha256(
                json.dumps(digest_input, ensure_ascii=True, sort_keys=True,
                          separators=(",", ":")).encode()
            ).hexdigest()
            self.assertEqual(expected, value["catalog_digest"])

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
                result = catalog.scan_catalog(root, _full_validity=full_validity)
            self.assertEqual(1, read_bytes.call_count)
            self.assertEqual(anchor, calls[0][0])
            self.assertEqual(V1.splitlines(), calls[0][1])
            self.assertEqual("complete", json.loads(result)["entries"][0]["state"])

    def test_private_callback_receives_nonempty_malformed_capture(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            anchor = package(root, "Queue", "malformed", "Status: approved\nbody\n")
            calls: list[tuple[Path, list[str]]] = []

            def full_validity(package_path: Path, lines: list[str]) -> bool:
                calls.append((package_path, lines))
                return False

            result = catalog.scan_catalog(root, _full_validity=full_validity)

            self.assertEqual([(anchor, ["Status: approved", "body"])], calls)
            self.assertEqual(
                "complete",
                json.loads(result)["entries"][0]["state"],
            )

    def test_public_builders_do_not_accept_full_validity_hooks(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package(root, "Queue", "one", V1)
            with self.assertRaises(TypeError):
                catalog.build_catalog(root, lambda _path, _lines: False)

    def test_malformed_callback_and_catalog_diagnostic_are_single_pass(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            anchor = package(root, "Queue", "malformed", "Status: approved\nbody\n")
            calls: list[list[str]] = []

            def full_validity(_package_path: Path, lines: list[str]) -> bool:
                calls.append(lines)
                return True

            value = json.loads(
                catalog.scan_catalog(root, _full_validity=full_validity)
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
        self.assertIn("_full_validity", str(__import__("inspect").signature(catalog.scan_catalog)))
        self.assertNotIn("version", str(__import__("inspect").signature(catalog.scan_catalog)))

    def test_public_entry_points_share_the_canonical_builder(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package(root, "Queue", "one", V1)
            with patch.object(catalog, "_build_catalog", wraps=catalog._build_catalog) as builder:
                catalog.build_catalog(root)
                catalog.scan_catalog(root)
            self.assertEqual(2, builder.call_count)
            self.assertEqual([root, root], [call.args[0] for call in builder.call_args_list])

    def test_empty_and_first_level_two_headers_do_not_call_hook(self) -> None:
        for content in (b"", b"## Body\nsecret"):
            with self.subTest(content=content), TemporaryDirectory() as temporary:
                root = Path(temporary)
                anchor = package(root, "Queue", "one", "# placeholder\n") / "spec.md"
                anchor.write_bytes(content)
                calls: list[object] = []
                catalog.scan_catalog(root, _full_validity=lambda *_: calls.append(True))
                self.assertEqual([], calls)

    def test_changed_fingerprint_retries_before_hook_capture(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            anchor = package(root, "Queue", "one", "# Heading\n## Body\nsecret") / "spec.md"
            calls: list[tuple[Path, list[str]]] = []
            original = catalog._catalog_fingerprint
            fingerprints = 0

            def fingerprint(path: Path) -> tuple[int, int, int, int, int, int]:
                nonlocal fingerprints
                fingerprints += 1
                result = original(path)
                if fingerprints == 2:
                    return (*result[:5], result[5] + 1)
                return result

            with patch.object(catalog, "_catalog_fingerprint", side_effect=fingerprint):
                catalog.scan_catalog(root, _full_validity=lambda path, lines: calls.append((path, lines)) or False)
            self.assertGreaterEqual(fingerprints, 4)
            self.assertEqual([(anchor.parent, ["# Heading"])], calls)

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
            self.assertEqual(2, value["schema_version"])
            self.assertEqual(
                "48656e04b2c6d6748ed288d382d4aaeffddd8bca600a9b7e85c35d048eb96543",
                value["catalog_digest"],
            )
            self.assertEqual(
                {"under_development", "queue", "needs_fixes", "awaiting_retrospective", "done"},
                {entry["lifecycle"] for entry in value["entries"]},
            )
            self.assertIn("Fictional/Done/done", {entry["package_path"] for entry in value["entries"]})
            queue = next(entry for entry in value["entries"] if entry["lifecycle"] == "queue")
            self.assertEqual("satisfied", queue["relationship"]["prerequisites"][0]["resolved_state"])
