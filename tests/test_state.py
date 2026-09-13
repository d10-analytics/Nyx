import json
import os
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nyx import state


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
        assert loaded == result
        payload = json.loads(paths.config_file.read_text(encoding="utf-8"))
        assert payload == {
            "schema_version": 1,
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
            assert state.setup(spec_root).specification_root == spec_root.resolve()


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
        for directory in (
            paths.config_file.parent.parent,
            paths.config_directory,
            paths.state_directory.parent.parent,
            paths.state_directory.parent,
            paths.state_directory,
            paths.runtime_directory,
        ):
            assert directory.is_dir()
            assert not directory.is_symlink()
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
            assert directory.stat().st_uid == os.getuid()
        assert stat.S_IMODE(paths.config_file.stat().st_mode) == 0o600
        assert paths.config_file.stat().st_uid == os.getuid()
        assert not paths.deployment_file.exists()


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
