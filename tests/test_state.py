import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from nyx import runtime, state
from nyx.catalog import scan_catalog


def isolated_home(root: Path) -> Path:
    home = root / "account-home"
    home.mkdir(mode=0o755)
    return home


def isolated_root(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir()
    return path


def configure_home(home: Path):
    return patch.object(Path, "home", return_value=home), patch.object(
        state, "_current_uid", return_value=state._current_uid()
    )


def test_account_home_comes_from_controlled_path_home_even_when_environment_differs():
    with TemporaryDirectory() as temporary:
        home = isolated_home(Path(temporary))
        with patch.object(Path, "home", return_value=home), patch.dict(
            os.environ, {"HOME": str(Path(temporary) / "wrong")}
        ):
            assert state.resolve_account_home() == home


def test_state_paths_use_exact_home_nyx_children_without_observation_creation():
    with TemporaryDirectory() as temporary:
        home = isolated_home(Path(temporary))
        with patch.object(Path, "home", return_value=home):
            paths = state.state_paths()
            assert paths.account_home == home
            assert paths.state_directory == home / ".nyx"
            assert paths.config_file == home / ".nyx" / "config" / "config.json"
            assert paths.runtime_directory == home / ".nyx" / "runtime"
            assert not (home / ".nyx").exists()


def test_setup_uses_controlled_home_and_persists_canonical_empty_symlink_root():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        actual = isolated_root(root, "empty-root")
        supplied = root / "root-link"
        supplied.symlink_to(actual, target_is_directory=True)
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch, patch.dict(
            os.environ, {"HOME": str(root / "other-home"), "XDG_CONFIG_HOME": str(root / "other-xdg")}
        ):
            result = state.setup(supplied)
            paths = state.state_paths()
            loaded = state.load_configuration(paths)

        assert result.specification_root == actual.resolve()
        assert result.hidden_stages == ()
        assert loaded == result
        payload = json.loads(paths.config_file.read_text(encoding="utf-8"))
        assert payload == {
            "schema_version": 2,
            "hidden_stages": [],
            "specification_root": str(actual.resolve()),
        }
        assert paths.config_file.is_relative_to(home)
        assert not (root / "other-home").exists()
        assert not (root / "other-xdg").exists()


def test_setup_accepts_empty_root_and_defers_malformed_children_to_catalog():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        spec_root = isolated_root(root, "spec-root")
        malformed = spec_root / "Fictional" / "Queue" / "malformed"
        malformed.mkdir(parents=True)
        malformed.joinpath("spec.md").write_text("not a valid package", encoding="utf-8")
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            result = state.setup(spec_root)
            assert result.specification_root == spec_root.resolve()
            catalog = json.loads(scan_catalog(result.specification_root))
            assert catalog["entries"][0]["relationship"]["participation"] == "legacy"
            assert catalog["entries"][0]["diagnostics"] == []


def test_valid_inactive_configuration_survives_loading_in_a_new_process():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        spec_root = isolated_root(root, "spec-root")
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            state.setup(spec_root)

        probe = (
            "import sys\n"
            "from pathlib import Path\n"
            "from nyx import state\n"
            "state.resolve_account_home = lambda: Path(sys.argv[1])\n"
            "print(state.load_configuration().specification_root)\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe, str(home)],
            cwd=Path(__file__).parents[1],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        assert completed.stdout.strip() == str(spec_root.resolve())


def test_invalid_replacement_preserves_exact_previous_configuration_bytes():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        first = isolated_root(root, "first")
        missing = root / "missing"
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            state.setup(first)
            config_file = state.state_paths().config_file
            before = config_file.read_bytes()
            with pytest.raises(state.SpecificationRootError):
                state.setup(missing)
            assert config_file.read_bytes() == before
            assert state.load_configuration().specification_root == first.resolve()


def test_expired_admission_preserves_existing_configuration_and_claims():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        first = isolated_root(root, "first")
        second = isolated_root(root, "second")
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            state.setup(first)
            paths = state.state_paths()
            claims = [paths.runtime_directory / "operation.lock", paths.runtime_directory / "lease.lock"]
            for claim in claims:
                claim.write_bytes(b"claim-bytes")
            before = {
                path: (path.lstat().st_ino, path.lstat().st_size, path.read_bytes())
                for path in claims
            }
            config_before = paths.config_file.read_bytes()
            original_lstat = state._lstat
            lookup_count = 0

            def delayed_admission_lookup(path):
                nonlocal lookup_count
                lookup_count += 1
                if lookup_count == 1:
                    time.sleep(0.03)
                return original_lstat(path)

            with patch.object(runtime, "STARTUP_TIMEOUT", 0.01), patch.object(
                state, "_lstat", side_effect=delayed_admission_lookup
            ), pytest.raises(runtime.StartupError):
                state.setup(second)

            assert paths.config_file.read_bytes() == config_before
            after = {
                path: (path.lstat().st_ino, path.lstat().st_size, path.read_bytes())
                for path in claims
            }

        assert after == before


def test_hidden_stages_are_deduplicated_and_scalar_sorted_without_normalization():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        spec_root = isolated_root(root, "spec-root")
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            result = state.setup(spec_root, ["z", "é", "z", " A ", "a"])
            payload = json.loads(state.state_paths().config_file.read_text(encoding="utf-8"))

        assert result.hidden_stages == (" A ", "a", "z", "é")
        assert payload == {
            "hidden_stages": [" A ", "a", "z", "é"],
            "schema_version": 2,
            "specification_root": str(spec_root.resolve()),
        }


@pytest.mark.parametrize("invalid", ["", ".", "..", "a/b", "a\\b", "a\x00b", "a\x7fb", "\ud800"])
def test_invalid_hidden_stage_rejected_before_state_creation(invalid):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        spec_root = isolated_root(root, "spec-root")
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch, pytest.raises(state.HiddenStageError):
            state.setup(spec_root, [invalid])
        assert not (home / ".config").exists()


def test_schema_one_loads_defaults_without_writing_and_explicit_setup_migrates():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        first = isolated_root(root, "first")
        second = isolated_root(root, "second")
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            state.setup(first)
            paths = state.state_paths()
            paths.config_file.write_text(
                json.dumps({"schema_version": 1, "specification_root": str(first.resolve())}) + "\n",
                encoding="utf-8",
            )
            before = paths.config_file.read_bytes()
            assert state.load_configuration(paths).hidden_stages == ()
            assert paths.config_file.read_bytes() == before
            migrated = state.setup(second, ["Queue"])

        assert migrated.hidden_stages == ("Queue",)
        assert json.loads(paths.config_file.read_text(encoding="utf-8")) == {
            "hidden_stages": ["Queue"],
            "schema_version": 2,
            "specification_root": str(second.resolve()),
        }


def test_omitted_setup_preserves_existing_policy_but_explicit_empty_clears_it():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        spec_root = isolated_root(root, "spec-root")
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            state.setup(spec_root, ["Queue"])
            paths = state.state_paths()
            before = paths.config_file.read_bytes()
            preserved = state.setup(spec_root)
            assert preserved.hidden_stages == ("Queue",)
            assert paths.config_file.read_bytes() == before
            cleared = state.setup(spec_root, [])

        assert cleared.hidden_stages == ()
        assert json.loads(paths.config_file.read_text(encoding="utf-8"))["hidden_stages"] == []


def test_post_replace_verification_failure_keeps_new_record_without_claiming_rollback():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        first = isolated_root(root, "first")
        second = isolated_root(root, "second")
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            state.setup(first)
            paths = state.state_paths()
            verify_record = state._verify_record

            def fail_after_replacement(path):
                verify_record(path)
                if json.loads(path.read_text(encoding="utf-8"))["specification_root"] == str(second.resolve()):
                    raise OSError("post-replace verification failed")

            with patch.object(state, "_verify_record", side_effect=fail_after_replacement), pytest.raises(
                state.ConfigurationCommitVerificationError, match="replaced but could not be verified"
            ):
                state.setup(second, ["Queue"])

        assert json.loads(paths.config_file.read_text(encoding="utf-8")) == {
            "hidden_stages": ["Queue"],
            "schema_version": 2,
            "specification_root": str(second.resolve()),
        }


def test_owned_save_classifies_pre_replacement_failure_without_creating_first_record():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        workspace = isolated_root(root, "workspace")
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            paths = state.state_paths(create=True)
            with patch.object(state.os, "replace", side_effect=OSError("injected")), pytest.raises(
                state.ConfigurationError
            ):
                state.save_configuration_owned(workspace, paths=paths)
            assert not paths.config_file.exists()


def test_owned_save_omission_defaults_empty_then_preserves_existing_policy():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        first = isolated_root(root, "first")
        second = isolated_root(root, "second")
        third = isolated_root(root, "third")
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            paths = state.state_paths(create=True)
            initial = state.save_configuration_owned(first, paths=paths)
            assert initial.hidden_stages == ()
            state.save_configuration_owned(second, ["Queue"], paths=paths)
            preserved = state.save_configuration_owned(third, paths=paths)
            assert preserved.hidden_stages == ("Queue",)
            cleared = state.save_configuration_owned(first, [], paths=paths)
        assert cleared.hidden_stages == ()


def test_candidate_validation_is_canonical_and_does_not_write_configuration():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        first = isolated_root(root, "first")
        second = isolated_root(root, "second")
        supplied = root / "second-link"
        supplied.symlink_to(second, target_is_directory=True)
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            state.setup(first, ["Queue"])
            paths = state.state_paths()
            before = paths.config_file.read_bytes()
            candidate = state.validate_configuration_candidate(supplied, paths=paths)
            assert candidate == state.Configuration(second.resolve(), ("Queue",))
            assert paths.config_file.read_bytes() == before


def test_owned_save_classifies_post_replacement_verification_and_revalidation():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        first = isolated_root(root, "first")
        second = isolated_root(root, "second")
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            state.setup(first)
            paths = state.state_paths()
            original_verify = state._verify_record
            failed = False

            def verify_once(path):
                nonlocal failed
                details = original_verify(path)
                if path == paths.config_file and json.loads(path.read_text())["specification_root"] == str(
                    second.resolve()
                ) and not failed:
                    failed = True
                    raise OSError("injected post-replacement verification failure")
                return details

            with patch.object(state, "_verify_record", side_effect=verify_once), pytest.raises(
                state.ConfigurationCommitVerificationError
            ):
                state.save_configuration_owned(second, ["Queue"], paths=paths)
            assert state.load_configuration(paths).specification_root == second.resolve()
            assert state.revalidate_configuration(paths) == state.Configuration(second.resolve(), ("Queue",))


def test_atomic_replacement_failure_preserves_previous_configuration_bytes():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        first = isolated_root(root, "first")
        second = isolated_root(root, "second")
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            state.setup(first)
            paths = state.state_paths()
            before = paths.config_file.read_bytes()
            with patch.object(state.os, "replace", side_effect=OSError("injected")), pytest.raises(
                state.ConfigurationError
            ):
                state.setup(second)
            assert paths.config_file.read_bytes() == before
            assert state.load_configuration().specification_root == first.resolve()
            assert list(paths.config_directory.glob(f".{state.CONFIG_FILENAME}.*")) == []


def test_preexisting_nyx_directory_symlink_is_rejected_without_writing_through_it():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        spec_root = isolated_root(root, "spec-root")
        external = root / "external"
        external.mkdir(mode=0o700)
        (home / ".nyx").symlink_to(external, target_is_directory=True)
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch, pytest.raises(state.AccountHomeError):
            state.setup(spec_root)
        assert list(external.iterdir()) == []


@pytest.mark.skipif(sys.platform != "win32", reason="requires a native Windows junction")
def test_preexisting_nyx_directory_junction_is_rejected_without_writing_through_it():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        external = root / "external"
        external.mkdir()
        junction = home / ".nyx"
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        assert junction.is_junction()

        with patch.object(Path, "home", return_value=home), pytest.raises(
            state.AccountHomeError, match="ordinary directory"
        ):
            state.state_paths(create=True)

        assert junction.is_junction()
        assert list(external.iterdir()) == []


@pytest.mark.parametrize("wrong_type", ["file", "symlink"])
def test_managed_root_wrong_type_is_rejected_without_following_or_repairing(wrong_type):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        external = root / "external"
        external.mkdir()
        managed = home / ".nyx"
        if wrong_type == "file":
            managed.write_bytes(b"sentinel")
        else:
            managed.symlink_to(external, target_is_directory=True)
        with patch.object(Path, "home", return_value=home), pytest.raises(state.AccountHomeError):
            state.state_paths(create=True)
        assert managed.is_symlink() if wrong_type == "symlink" else managed.read_bytes() == b"sentinel"
        assert list(external.iterdir()) == []


def test_changed_managed_target_is_rejected_after_creation_without_writing_through_replacement():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        external = root / "external"
        external.mkdir()
        original_mkdir = Path.mkdir

        def replace_root(path, *args, **kwargs):
            original_mkdir(path, *args, **kwargs)
            if path == home / ".nyx":
                path.rmdir()
                path.symlink_to(external, target_is_directory=True)

        with patch.object(Path, "home", return_value=home), patch.object(
            Path, "mkdir", replace_root
        ), pytest.raises(state.AccountHomeError, match="ordinary directory"):
            state.state_paths(create=True)
        assert list(external.iterdir()) == []


@pytest.mark.parametrize("child_name", ["config", "runtime"])
@pytest.mark.parametrize("wrong_type", ["file", "symlink"])
def test_managed_child_wrong_type_is_rejected_without_following(wrong_type, child_name):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        managed_root = home / ".nyx"
        managed_root.mkdir()
        external = root / "external"
        external.mkdir()
        child = managed_root / child_name
        if wrong_type == "file":
            child.write_bytes(b"sentinel")
        else:
            child.symlink_to(external, target_is_directory=True)
        with patch.object(Path, "home", return_value=home), pytest.raises(state.AccountHomeError):
            state.state_paths(create=True)
        assert child.is_symlink() if wrong_type == "symlink" else child.read_bytes() == b"sentinel"
        assert list(external.iterdir()) == []


def test_state_children_are_ordinary_directories_and_no_deployment_record_is_written():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        spec_root = isolated_root(root, "spec-root")
        original_home_mode = stat.S_IMODE(home.stat().st_mode)
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            state.setup(spec_root)
            paths = state.state_paths()

        assert stat.S_IMODE(home.stat().st_mode) == original_home_mode
        for directory in (paths.state_directory, paths.config_directory, paths.runtime_directory):
            assert directory.is_dir()
            assert not directory.is_symlink()
        assert paths.config_file.is_file()
        assert not paths.deployment_file.exists()


def test_existing_general_parents_are_not_repermissioned():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        nyx_root = home / ".nyx"
        config_parent = nyx_root / "config"
        runtime_parent = nyx_root / "runtime"
        nyx_root.mkdir(mode=0o755)
        config_parent.mkdir(mode=0o755)
        runtime_parent.mkdir(mode=0o755)
        config_mode = stat.S_IMODE(config_parent.stat().st_mode)
        runtime_mode = stat.S_IMODE(runtime_parent.stat().st_mode)
        spec_root = isolated_root(root, "spec-root")
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            state.setup(spec_root)
        assert stat.S_IMODE(config_parent.stat().st_mode) == config_mode
        assert stat.S_IMODE(runtime_parent.stat().st_mode) == runtime_mode


def test_root_inside_package_or_interpreter_footprint_is_rejected_before_state_creation():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        footprint = root / "footprint"
        footprint.mkdir()
        forbidden = footprint / "spec-root"
        forbidden.mkdir()
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch, patch.object(
            state, "_installation_footprints", return_value=(footprint.resolve(),)
        ), pytest.raises(state.SpecificationRootError):
            state.setup(forbidden)
        assert not (home / ".config").exists()


def test_state_admission_is_portable_across_platform_labels():
    with TemporaryDirectory() as temporary:
        home = isolated_home(Path(temporary))
        spec_root = isolated_root(Path(temporary), "spec-root")
        with patch.object(state.sys, "platform", "darwin"), patch.object(
            Path, "home", return_value=home
        ):
            paths = state.state_paths(create=True)
            state._save_configuration(paths, spec_root.resolve(), ())
        assert paths.config_file.exists()


def _managed_snapshot(home: Path) -> dict[str, tuple[str, bytes | None, int]]:
    snapshot: dict[str, tuple[str, bytes | None, int]] = {}
    if not home.exists():
        return snapshot
    for path in sorted(home.rglob("*")):
        relative = str(path.relative_to(home))
        details = path.lstat()
        if stat.S_ISREG(details.st_mode):
            content: bytes | None = path.read_bytes()
            kind = "file"
        elif stat.S_ISDIR(details.st_mode):
            content = None
            kind = "directory"
        else:
            content = None
            kind = "other"
        snapshot[relative] = (kind, content, stat.S_IMODE(details.st_mode))
    return snapshot


def test_configuration_observation_reports_confirmed_absence_without_defaults_or_writes():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        before = _managed_snapshot(home)
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            observed = state.observe_configuration()
        assert observed.status == "not_configured"
        assert observed.specification_root is None
        assert observed.hidden_stages is None
        assert observed.diagnostic is None
        assert _managed_snapshot(home) == before


@pytest.mark.parametrize("schema_version", [1, 2])
def test_configuration_observation_returns_valid_schema_data_only_when_configured(schema_version):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        spec_root = isolated_root(root, "spec-root")
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            state.state_paths(create=True)
            paths = state.state_paths()
            payload = {
                "schema_version": schema_version,
                "specification_root": str(spec_root.resolve()),
            }
            if schema_version == 2:
                payload["hidden_stages"] = ["Queue"]
            paths.config_file.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            os.chmod(paths.config_file, 0o600)
            before = _managed_snapshot(home)
            observed = state.observe_configuration()

        assert observed.status == "configured"
        assert observed.specification_root == spec_root.resolve()
        assert observed.hidden_stages == (("Queue",) if schema_version == 2 else ())
        assert observed.configuration == state.Configuration(
            spec_root.resolve(), ("Queue",) if schema_version == 2 else ()
        )
        assert _managed_snapshot(home) == before


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2, "hidden_stages": ["Queue"], "specification_root": "/tmp/root", "extra": 1},
        {"schema_version": 99, "hidden_stages": [], "specification_root": "/tmp/root"},
        {"schema_version": 2, "hidden_stages": ["bad/name"], "specification_root": "/tmp/root"},
    ],
)
def test_configuration_observation_collapses_invalid_and_unsupported_records(payload):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            paths = state.state_paths(create=True)
            paths.config_file.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            os.chmod(paths.config_file, 0o600)
            observed = state.observe_configuration()
        assert observed.status == "unavailable"
        assert observed.diagnostic == state.CONFIGURATION_UNAVAILABLE
        assert observed.specification_root is None
        assert observed.hidden_stages is None


def test_configuration_observation_collapses_path_resolution_value_error():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            paths = state.state_paths(create=True)
            paths.config_file.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "hidden_stages": [],
                        "specification_root": "/\x00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            before = paths.config_file.read_bytes()
            before_mode = stat.S_IMODE(paths.config_file.stat().st_mode)
            observed = state.observe_configuration()

        assert stat.S_IMODE(paths.config_file.stat().st_mode) == before_mode
        assert observed == state.ConfigurationObservation(
            "unavailable", diagnostic=state.CONFIGURATION_UNAVAILABLE
        )
        assert paths.config_file.read_bytes() == before


def test_configuration_observation_reports_concurrent_valid_replacement_as_unavailable():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        first = isolated_root(root, "first")
        second = isolated_root(root, "second")
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            state.setup(first)
            paths = state.state_paths()
            original_load = state.load_configuration
            replacement = paths.config_directory / "replacement.json"
            replacement.write_bytes(state._configuration_bytes(second.resolve(), ("Queue",)))

            def replace_during_read(selected):
                os.replace(replacement, paths.config_file)
                return original_load(selected)

            with patch.object(state, "load_configuration", side_effect=replace_during_read):
                observed = state.observe_configuration()

        assert observed == state.ConfigurationObservation(
            "unavailable", diagnostic=state.CONFIGURATION_UNAVAILABLE
        )
        assert state.load_configuration(paths) == state.Configuration(
            second.resolve(), ("Queue",)
        )


def test_configuration_observation_isolated_from_unsafe_runtime_ancestry():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        spec_root = isolated_root(root, "spec-root")
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            state.setup(spec_root, ["Queue"])
            paths = state.state_paths()
            os.chmod(paths.runtime_directory, 0o755)
            with patch.object(state, "state_paths", side_effect=AssertionError("aggregate paths used")):
                observed = state.observe_configuration()
        assert observed.status == "configured"
        assert observed.specification_root == spec_root.resolve()
        assert observed.hidden_stages == ("Queue",)


def test_runtime_observation_isolated_from_unsafe_configuration_and_has_no_create_path():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            paths = state.state_paths(create=True)
            paths.config_file.write_text("not json\n", encoding="utf-8")
            os.chmod(paths.config_file, 0o600)
            os.chmod(paths.config_directory, 0o755)
            before = _managed_snapshot(home)
            with patch.object(state, "state_paths", side_effect=AssertionError("aggregate paths used")), patch.object(
                state, "load_configuration", side_effect=AssertionError("configuration read")
            ):
                observed = state.observe_runtime()
        assert observed.status == "not_running"
        assert observed.paths is not None
        assert _managed_snapshot(home) == before
        assert not paths.runtime_directory.joinpath("operation.lock").exists()


def test_runtime_observation_reports_incomplete_or_unsafe_layout_without_mutation():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            paths = state.state_paths(create=True)
            paths.runtime_directory.rmdir()
            before = _managed_snapshot(home)
            assert state.observe_runtime().status == "not_running"
            assert _managed_snapshot(home) == before
            paths.runtime_directory.mkdir(mode=0o700)
            paths.runtime_directory.joinpath("instance.json").write_text("{}\n", encoding="utf-8")
            os.chmod(paths.runtime_directory.joinpath("instance.json"), 0o644)
            before = _managed_snapshot(home)
            assert state.observe_runtime().status == "unknown"
            assert _managed_snapshot(home) == before


def test_runtime_observation_preserves_empty_claims_and_metadata():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            paths = state.state_paths(create=True)
            claims = [paths.runtime_directory / "operation.lock", paths.runtime_directory / "lease.lock"]
            for claim in claims:
                claim.write_bytes(b"")
            before = {
                claim: (
                    claim.lstat().st_dev,
                    claim.lstat().st_ino,
                    claim.lstat().st_mode,
                    claim.lstat().st_size,
                    claim.lstat().st_mtime_ns,
                    claim.lstat().st_ctime_ns,
                    claim.read_bytes(),
                )
                for claim in claims
            }

            observed = state.observe_runtime()

        after = {
            claim: (
                claim.lstat().st_dev,
                claim.lstat().st_ino,
                claim.lstat().st_mode,
                claim.lstat().st_size,
                claim.lstat().st_mtime_ns,
                claim.lstat().st_ctime_ns,
                claim.read_bytes(),
            )
            for claim in claims
        }
        assert observed.status == "unknown"
        assert after == before


def test_runtime_observation_rejects_runtime_symlink_without_following_it():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        external = root / "external"
        external.mkdir()
        outside_claim = external / "operation.lock"
        outside_claim.write_bytes(b"outside")
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            paths = state.state_paths(create=True)
            paths.runtime_directory.rmdir()
            paths.runtime_directory.symlink_to(external, target_is_directory=True)
            observed = state.observe_runtime()

        assert observed.status == "unknown"
        assert outside_claim.read_bytes() == b"outside"


@pytest.mark.skipif(sys.platform != "win32", reason="requires a native Windows junction")
def test_runtime_observation_rejects_runtime_junction_without_following_it():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        external = root / "external"
        external.mkdir()
        outside_claim = external / "operation.lock"
        outside_claim.write_bytes(b"outside")
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            paths = state.state_paths(create=True)
            paths.runtime_directory.rmdir()
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(paths.runtime_directory), str(external)],
                check=False,
                capture_output=True,
                text=True,
            )
            assert completed.returncode == 0, completed.stderr
            observed = state.observe_runtime()

        assert observed.status == "unknown"
        assert outside_claim.read_bytes() == b"outside"


def test_fresh_runtime_observation_does_not_create_runtime_directory_or_lock():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            before = _managed_snapshot(home)
            observed = state.observe_runtime()
        assert observed.status == "not_running"
        assert _managed_snapshot(home) == before
        assert not (home / ".nyx").exists()


def test_runtime_observation_bounds_unavailable_controlled_home_as_unknown():
    with patch.object(Path, "home", side_effect=OSError("home unavailable")):
        observed = state.observe_runtime()

    assert observed == state.RuntimeObservation("unknown")


def _packaged_application(tmp_path, monkeypatch, *, with_helper=True):
    """Simulate a standalone application whose executable is relocated."""

    import sys

    application = tmp_path / "Nyx"
    application.mkdir()
    executable = application / ("Nyx.exe" if os.name == "nt" else "Nyx")
    executable.write_text("", encoding="utf-8")
    if with_helper:
        helper = application / "NyxWorker"
        helper.mkdir()
        (helper / ("NyxWorker.exe" if os.name == "nt" else "NyxWorker")).write_text(
            "", encoding="utf-8"
        )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    return application, executable


def test_packaged_application_footprints_reject_application_and_helper_subtrees(
    tmp_path, monkeypatch
):
    application, executable = _packaged_application(tmp_path, monkeypatch)
    workspaces = [application / "workspace", application / "NyxWorker" / "workspace"]
    for workspace in workspaces:
        workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()

    footprints = state._packaged_footprints()
    assert executable.parent in footprints
    assert executable.parent / "NyxWorker" in footprints

    for workspace in workspaces:
        with pytest.raises(state.SpecificationRootError):
            state.resolve_specification_root(workspace)
    assert state.resolve_specification_root(external) == external.resolve()


def test_packaged_macos_bundle_footprints_reject_bundle_subtrees(tmp_path, monkeypatch):
    import sys

    executable = tmp_path / "Nyx.app" / "Contents" / "MacOS" / "Nyx"
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")
    resources = tmp_path / "Nyx.app" / "Contents" / "Resources"
    frameworks = tmp_path / "Nyx.app" / "Contents" / "Frameworks"
    resources.mkdir()
    frameworks.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    footprints = state._packaged_footprints()
    assert (tmp_path / "Nyx.app") in footprints
    assert resources in footprints
    assert frameworks in footprints
    with pytest.raises(state.SpecificationRootError):
        state.resolve_specification_root(resources)
    external = tmp_path / "external"
    external.mkdir()
    assert state.resolve_specification_root(external) == external.resolve()


def test_source_installation_footprints_do_not_add_application_roots(tmp_path, monkeypatch):
    import sys

    monkeypatch.delattr(sys, "frozen", raising=False)
    external = tmp_path / "external"
    external.mkdir()
    assert state._packaged_footprints() == ()
    assert external.resolve() in state._installation_footprints() or state.resolve_specification_root(
        external
    ) == external.resolve()


def test_footprint_rejection_preserves_prior_configuration_bytes(tmp_path, monkeypatch):
    application, _ = _packaged_application(tmp_path, monkeypatch)
    home = isolated_home(tmp_path)
    external = isolated_root(tmp_path, "external")
    inside = application / "workspace"
    inside.mkdir()
    home_patch, uid_patch = configure_home(home)
    with home_patch, uid_patch:
        paths = state.state_paths(create=True)
        state.save_configuration_owned(external, paths=paths)
        before = paths.config_file.read_bytes()
        with pytest.raises(state.SpecificationRootError):
            state.save_configuration_owned(inside, paths=paths)
        assert paths.config_file.read_bytes() == before


def test_persisted_configuration_inside_a_packaged_footprint_stays_unavailable(
    tmp_path, monkeypatch
):
    application, _ = _packaged_application(tmp_path, monkeypatch)
    inside = application / "workspace"
    inside.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    home = isolated_home(tmp_path)
    home_patch, uid_patch = configure_home(home)
    with home_patch, uid_patch:
        paths = state.state_paths(create=True)
        state._save_configuration(paths, external.resolve(), ())
        assert state.load_configuration(paths).specification_root == external.resolve()
        state._save_configuration(paths, inside.resolve(), ())
        with pytest.raises(state.ConfigurationError):
            state.load_configuration(paths)
        with pytest.raises(state.ConfigurationError):
            state.revalidate_configuration(paths)
        observed = state.observe_configuration()
    assert observed.status == "unavailable"
    assert observed.specification_root is None
