import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nyx import state
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
    return patch.object(state, "resolve_account_home", return_value=home), patch.object(
        state, "_current_uid", return_value=os.getuid()
    )


def test_account_home_comes_from_uid_record_even_when_environment_differs():
    with TemporaryDirectory() as temporary:
        home = isolated_home(Path(temporary))
        with patch.object(state, "_current_uid", return_value=os.getuid()), patch.object(
            state.pwd,
            "getpwuid",
            return_value=SimpleNamespace(pw_dir=str(home)),
        ), patch.dict(os.environ, {"HOME": str(Path(temporary) / "wrong")}):
            assert state.resolve_account_home() == home.resolve()


def test_setup_uses_passwd_home_and_persists_canonical_empty_symlink_root():
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
            with patch.object(state.os, "fsync", side_effect=[None, OSError("directory sync failed")]), pytest.raises(
                state.ConfigurationError, match="cannot replace"
            ):
                state.setup(second, ["Queue"])

        assert json.loads(paths.config_file.read_text(encoding="utf-8")) == {
            "hidden_stages": ["Queue"],
            "schema_version": 2,
            "specification_root": str(second.resolve()),
        }


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
        (home / ".config").mkdir(mode=0o755)
        (home / ".config" / "nyx").symlink_to(external, target_is_directory=True)
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch, pytest.raises(state.AccountHomeError):
            state.setup(spec_root)
        assert list(external.iterdir()) == []


def test_state_children_are_private_uid_owned_records_and_no_deployment_record_is_written():
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
        for directory in (paths.config_directory, paths.state_directory, paths.runtime_directory):
            assert directory.is_dir()
            assert not directory.is_symlink()
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
            assert directory.stat().st_uid == os.getuid()
        for directory in (
            paths.config_file.parent.parent,
            paths.state_directory.parent.parent,
            paths.state_directory.parent,
        ):
            assert directory.is_dir()
            assert not directory.is_symlink()
        assert stat.S_IMODE(paths.config_file.stat().st_mode) == 0o600
        assert paths.config_file.stat().st_uid == os.getuid()
        assert not paths.deployment_file.exists()


def test_existing_general_parents_are_not_repermissioned():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = isolated_home(root)
        config_parent = home / ".config"
        state_parent = home / ".local" / "state"
        config_parent.mkdir(mode=0o755)
        (home / ".local").mkdir(mode=0o755)
        state_parent.mkdir(mode=0o755)
        os.chmod(home / ".local", 0o755)
        os.chmod(state_parent, 0o755)
        config_mode = stat.S_IMODE(config_parent.stat().st_mode)
        local_mode = stat.S_IMODE((home / ".local").stat().st_mode)
        state_mode = stat.S_IMODE(state_parent.stat().st_mode)
        spec_root = isolated_root(root, "spec-root")
        home_patch, uid_patch = configure_home(home)
        with home_patch, uid_patch:
            state.setup(spec_root)
        assert stat.S_IMODE(config_parent.stat().st_mode) == config_mode
        assert stat.S_IMODE((home / ".local").stat().st_mode) == local_mode
        assert stat.S_IMODE(state_parent.stat().st_mode) == state_mode


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


def test_unsupported_platform_fails_before_mutating_account_home():
    with TemporaryDirectory() as temporary:
        home = isolated_home(Path(temporary))
        with patch.object(state.sys, "platform", "darwin"), patch.object(
            state, "resolve_account_home", return_value=home
        ), pytest.raises(state.UnsupportedPlatformError):
            state.setup(home)
        assert not (home / ".config").exists()


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
            assert state.observe_runtime().status == "unknown"
            assert _managed_snapshot(home) == before
            paths.runtime_directory.mkdir(mode=0o700)
            paths.runtime_directory.joinpath("instance.json").write_text("{}\n", encoding="utf-8")
            os.chmod(paths.runtime_directory.joinpath("instance.json"), 0o644)
            before = _managed_snapshot(home)
            assert state.observe_runtime().status == "unknown"
            assert _managed_snapshot(home) == before


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
        assert not (home / ".local").exists()
