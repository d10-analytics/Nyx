import inspect
import json
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

    def test_stable_body_anchor_is_read_once_and_hook_gets_header_only(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            anchor = package(
                root,
                "Queue",
                "callback",
                "# Header\nStatus: approved\n## Details\nprivate body\n",
            ) / "spec.md"
            calls: list[tuple[Path, list[str]]] = []

            def full_validity(package_path: Path, lines: list[str]) -> bool:
                calls.append((package_path, lines))
                return False

            original_read = Path.read_bytes
            with patch.object(
                Path,
                "read_bytes",
                autospec=True,
                side_effect=lambda path: original_read(path),
            ) as read_bytes:
                rendered = catalog.scan_catalog(root, _full_validity=full_validity)
            value = json.loads(rendered)
            self.assertEqual(1, read_bytes.call_count)
            self.assertEqual([(anchor.parent, ["# Header", "Status: approved"])], calls)
            self.assertNotIn("private body", rendered)
            self.assertEqual("complete", value["entries"][0]["state"])

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
        self.assertEqual(
            "(spec_root: 'Path', *, _full_validity: 'Callable[[Path, list[str]], bool] | None' = None) -> 'str'",
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

    def test_hook_exception_classes_mark_stable_capture_invalid(self) -> None:
        for error_type in (OSError, UnicodeError, ValueError):
            with self.subTest(error_type=error_type), TemporaryDirectory() as temporary:
                root = Path(temporary)
                package(root, "Queue", "one", "# Heading\n## Body\nsecret")

                def hook(_path: Path, _lines: list[str], error_type=error_type) -> bool:
                    raise error_type("hook failure")

                value = json.loads(catalog.scan_catalog(root, _full_validity=hook))
                self.assertEqual("partial", value["entries"][0]["state"])
                self.assertEqual("invalid_package", value["entries"][0]["diagnostics"][0]["code"])

    def test_unexpected_hook_exception_propagates(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package(root, "Queue", "one", "# Heading\n## Body\nsecret")

            def hook(_path: Path, _lines: list[str]) -> bool:
                raise RuntimeError("unexpected hook failure")

            with self.assertRaisesRegex(RuntimeError, "unexpected hook failure"):
                catalog.scan_catalog(root, _full_validity=hook)

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
ORACLE_BYTES = "{\"catalog_digest\":\"0d5fa0a5af42e342354e5064d429ec232213e7097b589ba22c2c086a34f13c03\",\"discovery_diagnostics\":[],\"entries\":[{\"declared\":{\"closure\":null,\"human_sanity_decision\":null,\"sanity_recommendation\":null,\"status\":null,\"target_project\":null,\"title\":\"Retro\"},\"diagnostics\":[],\"lifecycle\":\"awaiting_retrospective\",\"package_id\":\"55555555-5555-4555-8555-555555555555\",\"package_path\":\"Fictional/Awaiting_Retrospective/pkg\",\"relationship\":{\"claims\":[],\"direct_prerequisite_state\":\"no_declared_prerequisites\",\"participation\":\"available\",\"prerequisites\":[],\"program\":{\"diagnostics\":[],\"program_id\":null,\"resolution\":\"not_declared\",\"title\":null},\"superseded_by\":{\"diagnostics\":[],\"package_id\":null,\"resolution\":\"not_declared\"}},\"state\":\"complete\",\"transitive_diagnostics\":[]},{\"declared\":{\"closure\":null,\"human_sanity_decision\":null,\"sanity_recommendation\":null,\"status\":null,\"target_project\":null,\"title\":\"Progress\"},\"diagnostics\":[],\"lifecycle\":\"in_progress\",\"package_id\":\"33333333-3333-4333-8333-333333333333\",\"package_path\":\"Fictional/In_Progress/pkg\",\"relationship\":{\"claims\":[{\"diagnostics\":[],\"evidence_ref\":\"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\",\"name\":\"release\",\"state\":\"satisfied\"}],\"direct_prerequisite_state\":\"no_declared_prerequisites\",\"participation\":\"available\",\"prerequisites\":[],\"program\":{\"diagnostics\":[],\"program_id\":null,\"resolution\":\"not_declared\",\"title\":null},\"superseded_by\":{\"diagnostics\":[{\"code\":\"successor_cycle\",\"message\":\"successor cycle detected: Fictional/In_Progress/pkg\"}],\"package_id\":\"22222222-2222-4222-8222-222222222222\",\"resolution\":\"resolved\"}},\"state\":\"complete\",\"transitive_diagnostics\":[]},{\"declared\":{\"closure\":null,\"human_sanity_decision\":null,\"sanity_recommendation\":null,\"status\":null,\"target_project\":null,\"title\":\"Fix\"},\"diagnostics\":[],\"lifecycle\":\"needs_fixes\",\"package_id\":\"44444444-4444-4444-8444-444444444444\",\"package_path\":\"Fictional/Needs_Fixes/pkg\",\"relationship\":{\"claims\":[],\"direct_prerequisite_state\":\"no_declared_prerequisites\",\"participation\":\"available\",\"prerequisites\":[],\"program\":{\"diagnostics\":[],\"program_id\":null,\"resolution\":\"not_declared\",\"title\":null},\"superseded_by\":{\"diagnostics\":[],\"package_id\":null,\"resolution\":\"not_declared\"}},\"state\":\"complete\",\"transitive_diagnostics\":[]},{\"declared\":{\"closure\":null,\"human_sanity_decision\":null,\"sanity_recommendation\":null,\"status\":null,\"target_project\":null,\"title\":\"Queue\"},\"diagnostics\":[{\"code\":\"invalid_claim\",\"message\":\"invalid claim: Fictional/Queue/pkg\"},{\"code\":\"invalid_prerequisite\",\"message\":\"invalid prerequisite: Fictional/Queue/pkg\"}],\"lifecycle\":\"queue\",\"package_id\":\"22222222-2222-4222-8222-222222222222\",\"package_path\":\"Fictional/Queue/pkg\",\"relationship\":{\"claims\":[{\"diagnostics\":[],\"evidence_ref\":null,\"name\":\"release\",\"state\":\"unsatisfied\"}],\"direct_prerequisite_state\":\"unknown\",\"participation\":\"available\",\"prerequisites\":[{\"claim_name\":null,\"observed_evidence_ref\":null,\"observed_state\":null,\"reason\":\"invalid_prerequisite\",\"resolved_state\":\"unknown\",\"target_package_id\":null},{\"claim_name\":\"release\",\"observed_evidence_ref\":\"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\",\"observed_state\":\"satisfied\",\"reason\":\"claim_satisfied\",\"resolved_state\":\"satisfied\",\"target_package_id\":\"33333333-3333-4333-8333-333333333333\"}],\"program\":{\"diagnostics\":[],\"program_id\":\"88888888-8888-4888-8888-888888888888\",\"resolution\":\"resolved\",\"title\":\"Core\"},\"superseded_by\":{\"diagnostics\":[{\"code\":\"successor_cycle\",\"message\":\"successor cycle detected: Fictional/Queue/pkg\"}],\"package_id\":\"33333333-3333-4333-8333-333333333333\",\"resolution\":\"resolved\"}},\"state\":\"partial\",\"transitive_diagnostics\":[{\"code\":\"invalid_prerequisite\",\"origin_package_id\":\"22222222-2222-4222-8222-222222222222\",\"path_package_ids\":[\"22222222-2222-4222-8222-222222222222\"]}]},{\"declared\":{\"closure\":null,\"human_sanity_decision\":null,\"sanity_recommendation\":null,\"status\":null,\"target_project\":null,\"title\":\"Under\"},\"diagnostics\":[],\"lifecycle\":\"under_development\",\"package_id\":\"11111111-1111-4111-8111-111111111111\",\"package_path\":\"Fictional/Under_Development/pkg\",\"relationship\":{\"claims\":[{\"diagnostics\":[],\"evidence_ref\":\"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"name\":\"design\",\"state\":\"satisfied\"}],\"direct_prerequisite_state\":\"no_declared_prerequisites\",\"participation\":\"available\",\"prerequisites\":[],\"program\":{\"diagnostics\":[],\"program_id\":\"88888888-8888-4888-8888-888888888888\",\"resolution\":\"resolved\",\"title\":\"Core\"},\"superseded_by\":{\"diagnostics\":[],\"package_id\":null,\"resolution\":\"not_declared\"}},\"state\":\"complete\",\"transitive_diagnostics\":[]}],\"identity_coverage\":{\"diagnostics\":[],\"state\":\"complete\"},\"program_coverage\":{\"diagnostics\":[],\"state\":\"complete\"},\"programs\":[{\"diagnostics\":[],\"member_package_ids\":[\"11111111-1111-4111-8111-111111111111\",\"22222222-2222-4222-8222-222222222222\"],\"program_id\":\"88888888-8888-4888-8888-888888888888\",\"title\":\"Core\"}],\"schema_version\":2}"

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
