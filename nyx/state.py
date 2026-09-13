"""Secure per-account configuration for Nyx.

This module owns the durable setup record.  Process ownership and lifecycle
leases are deliberately kept in the runtime layer, so reading or replacing a
configuration never acquires or assumes a runtime lease.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_SCHEMA_VERSION = 1
CONFIG_FILENAME = "config.json"
DEPLOYMENT_FILENAME = "deployment.json"
RUNTIME_DIRECTORY = "runtime"


class StateError(RuntimeError):
    """Base class for safe, user-facing state errors."""


class UnsupportedPlatformError(StateError):
    """The fixed account-home state contract is Linux-only."""


class AccountHomeError(StateError):
    """The current UID does not have a usable account home."""


class SpecificationRootError(StateError):
    """The requested specification root cannot be used."""


class ConfigurationError(StateError):
    """A persisted configuration is missing, unsafe, or malformed."""


@dataclass(frozen=True)
class StatePaths:
    """The fixed paths belonging to the current UID's account home."""

    account_home: Path
    config_directory: Path
    config_file: Path
    state_directory: Path
    deployment_file: Path
    runtime_directory: Path


@dataclass(frozen=True)
class Configuration:
    """The validated contents of the Nyx setup record."""

    specification_root: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "specification_root": str(self.specification_root),
        }


def _require_linux() -> None:
    if sys.platform != "linux":
        raise UnsupportedPlatformError("Nyx state is supported on Linux only")


def _current_uid() -> int:
    try:
        return os.getuid()
    except AttributeError as error:  # pragma: no cover - Linux has getuid
        raise UnsupportedPlatformError("Nyx requires a Linux UID") from error


def resolve_account_home() -> Path:
    """Resolve the current UID's passwd home without consulting the environment."""

    _require_linux()
    uid = _current_uid()
    try:
        account = pwd.getpwuid(uid)
        raw_home = account.pw_dir
    except (KeyError, OSError, TypeError) as error:
        raise AccountHomeError("current UID has no account home") from error
    if not isinstance(raw_home, str) or not raw_home or not os.path.isabs(raw_home):
        raise AccountHomeError("current account home is not an absolute path")
    home = Path(raw_home)
    try:
        canonical = home.resolve(strict=True)
        mode = canonical.stat()
    except (OSError, RuntimeError) as error:
        raise AccountHomeError("current account home is unusable") from error
    if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != uid:
        raise AccountHomeError("current account home is not a UID-owned directory")
    if not os.access(canonical, os.R_OK | os.W_OK | os.X_OK):
        raise AccountHomeError("current account home is not accessible")
    return canonical


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _installation_footprints() -> tuple[Path, ...]:
    """Return resolved Nyx package and interpreter environments."""

    candidates = [Path(__file__).resolve().parent]
    try:
        candidates.append(Path(sys.prefix).resolve(strict=True))
    except (OSError, RuntimeError):
        pass
    return tuple(dict.fromkeys(candidates))


def _reject_footprint(path: Path, message: str) -> None:
    if any(_is_within(path, footprint) for footprint in _installation_footprints()):
        raise SpecificationRootError(message)


def resolve_specification_root(value: str | os.PathLike[str]) -> Path:
    """Resolve and classify a supplied root without inspecting its children."""

    if isinstance(value, bytes):
        raise SpecificationRootError("specification root must be a path")
    try:
        candidate = Path(value)
        canonical = candidate.resolve(strict=True)
        details = canonical.stat()
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise SpecificationRootError("specification root is not usable") from error
    if not stat.S_ISDIR(details.st_mode):
        raise SpecificationRootError("specification root is not a directory")
    if not os.access(canonical, os.R_OK | os.X_OK):
        raise SpecificationRootError("specification root is not readable")
    _reject_footprint(canonical, "specification root is inside the Nyx installation")
    return canonical


def _verify_directory(path: Path, uid: int) -> None:
    try:
        details = path.lstat()
    except OSError as error:
        raise AccountHomeError("Nyx state directory is unavailable") from error
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise AccountHomeError("Nyx state directory is not a real directory")
    if details.st_uid != uid or stat.S_IMODE(details.st_mode) != 0o700:
        raise AccountHomeError("Nyx state directory has unsafe ownership or mode")


def _verify_general_directory(path: Path) -> None:
    try:
        details = path.lstat()
    except OSError as error:
        raise AccountHomeError("Nyx state parent directory is unavailable") from error
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise AccountHomeError("Nyx state parent is not a real directory")


def _ensure_general_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise AccountHomeError("cannot create Nyx state parent directory") from error
    _verify_general_directory(path)


def _ensure_directory(path: Path, uid: int) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise AccountHomeError("cannot create Nyx state directory") from error
    _verify_directory(path, uid)


def state_paths(*, create: bool = False) -> StatePaths:
    """Return fixed per-account paths, optionally creating Nyx directories."""

    _require_linux()
    home = resolve_account_home()
    uid = _current_uid()
    config_base = home / ".config"
    config_directory = config_base / "nyx"
    state_directory = home / ".local" / "state" / "nyx"
    runtime_directory = state_directory / RUNTIME_DIRECTORY
    paths = StatePaths(
        account_home=home,
        config_directory=config_directory,
        config_file=config_directory / CONFIG_FILENAME,
        state_directory=state_directory,
        deployment_file=state_directory / DEPLOYMENT_FILENAME,
        runtime_directory=runtime_directory,
    )
    for path in (config_base, config_directory, home / ".local", home / ".local" / "state",
                 state_directory, runtime_directory):
        if any(_is_within(path, footprint) for footprint in _installation_footprints()):
            raise AccountHomeError("Nyx state would be inside its installation")
    if create:
        for path in (config_base, home / ".local", home / ".local" / "state"):
            _ensure_general_directory(path)
        for path in (config_directory, state_directory, runtime_directory):
            _ensure_directory(path, uid)
    else:
        for path in (config_base, home / ".local", home / ".local" / "state"):
            if path.exists() or path.is_symlink():
                _verify_general_directory(path)
        for path in (config_directory, state_directory, runtime_directory):
            if path.exists() or path.is_symlink():
                _verify_directory(path, uid)
    return paths


def _verify_record(path: Path, uid: int) -> os.stat_result:
    try:
        details = path.lstat()
    except FileNotFoundError as error:
        raise ConfigurationError("Nyx configuration is missing") from error
    except OSError as error:
        raise ConfigurationError("Nyx configuration is unavailable") from error
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise ConfigurationError("Nyx configuration is not a regular file")
    if details.st_uid != uid or stat.S_IMODE(details.st_mode) != 0o600:
        raise ConfigurationError("Nyx configuration has unsafe ownership or mode")
    return details


def _configuration_from_payload(payload: Any) -> Configuration:
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "specification_root"}:
        raise ConfigurationError("Nyx configuration schema is invalid")
    if payload.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ConfigurationError("Nyx configuration schema version is unsupported")
    root = payload.get("specification_root")
    if not isinstance(root, str) or not os.path.isabs(root):
        raise ConfigurationError("Nyx configuration root is invalid")
    try:
        canonical = Path(root).resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ConfigurationError("Nyx configuration root is invalid") from error
    return Configuration(canonical)


def load_configuration(paths: StatePaths | None = None) -> Configuration:
    """Read and validate the secure setup record without checking root contents."""

    _require_linux()
    selected = state_paths() if paths is None else paths
    _verify_record(selected.config_file, _current_uid())
    try:
        payload = json.loads(selected.config_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("Nyx configuration cannot be read") from error
    return _configuration_from_payload(payload)


def _configuration_bytes(root: Path) -> bytes:
    return (json.dumps(
        Configuration(root).as_dict(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n").encode("utf-8")


def _atomic_write_configuration(paths: StatePaths, root: Path) -> None:
    uid = _current_uid()
    temporary_name: str | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{CONFIG_FILENAME}.", dir=paths.config_directory
        )
        try:
            os.fchmod(fd, 0o600)
            data = _configuration_bytes(root)
            written = 0
            while written < len(data):
                written += os.write(fd, data[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary_name, paths.config_file)
        temporary_name = None
        _verify_record(paths.config_file, uid)
        directory_fd = os.open(paths.config_directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, ValueError) as error:
        raise ConfigurationError("cannot replace Nyx configuration") from error
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def save_configuration(specification_root: str | os.PathLike[str]) -> Configuration:
    """Validate and atomically persist one canonical inactive setup root."""

    _require_linux()
    root = resolve_specification_root(specification_root)
    paths = state_paths(create=True)
    if paths.config_file.exists() or paths.config_file.is_symlink():
        _verify_record(paths.config_file, _current_uid())
    _atomic_write_configuration(paths, root)
    return Configuration(root)


def setup(specification_root: str | os.PathLike[str]) -> Configuration:
    """Public setup operation; it has no runtime-lease dependency."""

    return save_configuration(specification_root)


__all__ = [
    "CONFIG_SCHEMA_VERSION",
    "AccountHomeError",
    "Configuration",
    "ConfigurationError",
    "SpecificationRootError",
    "StateError",
    "StatePaths",
    "UnsupportedPlatformError",
    "load_configuration",
    "resolve_account_home",
    "resolve_specification_root",
    "save_configuration",
    "setup",
    "state_paths",
]
