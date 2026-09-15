"""Secure per-account configuration for Nyx.

This module owns the durable setup record.  Process ownership and lifecycle
leases are deliberately kept in the runtime layer; public replacements are
delegated there while the private writer remains responsible for durable bytes.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_SCHEMA_VERSION = 2
LEGACY_CONFIG_SCHEMA_VERSION = 1
CONFIG_FILENAME = "config.json"
DEPLOYMENT_FILENAME = "deployment.json"
RUNTIME_DIRECTORY = "runtime"
_OMITTED = object()

CONFIGURATION_UNAVAILABLE = "configuration_unavailable"

CONFIGURATION_STATES = frozenset({"configured", "not_configured", "unavailable"})
RUNTIME_STATES = frozenset({"running", "not_running", "unknown"})


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


class HiddenStageError(ConfigurationError):
    """A hidden-stage name is outside the admitted Unicode component domain."""


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
    hidden_stages: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "hidden_stages": list(self.hidden_stages),
            "specification_root": str(self.specification_root),
        }


@dataclass(frozen=True)
class ConfigurationObservation:
    """A read-only, configuration-only view of the account state."""

    status: str
    specification_root: Path | None = None
    hidden_stages: tuple[str, ...] | None = None
    diagnostic: str | None = None

    @property
    def state(self) -> str:
        """Compatibility spelling for consumers that call the status a state."""

        return self.status

    @property
    def root(self) -> Path | None:
        return self.specification_root

    @property
    def configuration(self) -> Configuration | None:
        if self.status != "configured":
            return None
        assert self.specification_root is not None
        assert self.hidden_stages is not None
        return Configuration(self.specification_root, self.hidden_stages)


@dataclass(frozen=True)
class RuntimeObservation:
    """A read-only view of the fixed runtime tree, independent of configuration."""

    status: str
    paths: StatePaths | None = None
    diagnostic: str | None = None

    @property
    def state(self) -> str:
        return self.status

    @property
    def runtime_directory(self) -> Path | None:
        return None if self.paths is None else self.paths.runtime_directory


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
    if stat.S_IMODE(details.st_mode) & 0o022:
        raise AccountHomeError("Nyx state parent is writable by another UID")


def _fixed_state_paths() -> StatePaths:
    """Derive fixed per-account paths without validating their children."""

    _require_linux()
    home = resolve_account_home()
    paths = StatePaths(
        account_home=home,
        config_directory=home / ".config" / "nyx",
        config_file=home / ".config" / "nyx" / CONFIG_FILENAME,
        state_directory=home / ".local" / "state" / "nyx",
        deployment_file=home / ".local" / "state" / "nyx" / DEPLOYMENT_FILENAME,
        runtime_directory=home / ".local" / "state" / "nyx" / RUNTIME_DIRECTORY,
    )
    for path in (
        home / ".config",
        paths.config_directory,
        home / ".local",
        home / ".local" / "state",
        paths.state_directory,
        paths.runtime_directory,
    ):
        if any(_is_within(path, footprint) for footprint in _installation_footprints()):
            raise AccountHomeError("Nyx state would be inside its installation")
    return paths


def _lstat(path: Path) -> os.stat_result | None:
    """Return metadata, preserving a distinction between absent and unavailable."""

    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise AccountHomeError("Nyx state path is unavailable") from error


def _is_missing(path: Path) -> bool:
    return _lstat(path) is None


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

    paths = _fixed_state_paths()
    uid = _current_uid()
    config_base = paths.config_directory.parent
    local_base = paths.state_directory.parents[1]
    state_parent = paths.state_directory.parent
    if create:
        for path in (config_base, local_base, state_parent):
            _ensure_general_directory(path)
        for path in (paths.config_directory, paths.state_directory, paths.runtime_directory):
            _ensure_directory(path, uid)
    else:
        for path in (config_base, local_base, state_parent):
            if path.exists() or path.is_symlink():
                _verify_general_directory(path)
        for path in (paths.config_directory, paths.state_directory, paths.runtime_directory):
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


def _validate_hidden_stages(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise HiddenStageError("hidden stages must be a sequence of names")
    try:
        names = tuple(values)
    except (TypeError, ValueError) as error:
        raise HiddenStageError("hidden stages must be a sequence of names") from error
    for name in names:
        if not isinstance(name, str):
            raise HiddenStageError("hidden stage names must be text")
        if (
            not name
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in name)
            or any(0xD800 <= ord(character) <= 0xDFFF for character in name)
        ):
            raise HiddenStageError(f"invalid hidden stage name: {name!r}")
    return tuple(sorted(set(names)))


def _configuration_from_payload(payload: Any) -> Configuration:
    if not isinstance(payload, dict) or "schema_version" not in payload:
        raise ConfigurationError("Nyx configuration schema is invalid")
    schema_version = payload.get("schema_version")
    if type(schema_version) is not int:
        raise ConfigurationError("Nyx configuration schema version is unsupported")
    if schema_version == LEGACY_CONFIG_SCHEMA_VERSION:
        if set(payload) != {"schema_version", "specification_root"}:
            raise ConfigurationError("Nyx configuration schema is invalid")
        hidden_stages: tuple[str, ...] = ()
    elif schema_version == CONFIG_SCHEMA_VERSION:
        if set(payload) != {"schema_version", "hidden_stages", "specification_root"}:
            raise ConfigurationError("Nyx configuration schema is invalid")
        raw_hidden_stages = payload.get("hidden_stages")
        if not isinstance(raw_hidden_stages, list):
            raise ConfigurationError("Nyx configuration hidden stages are invalid")
        try:
            hidden_stages = _validate_hidden_stages(raw_hidden_stages)
        except HiddenStageError as error:
            raise ConfigurationError("Nyx configuration hidden stages are invalid") from error
        if raw_hidden_stages != list(hidden_stages):
            raise ConfigurationError("Nyx configuration hidden stages are not canonical")
    else:
        raise ConfigurationError("Nyx configuration schema version is unsupported")
    root = payload.get("specification_root")
    if not isinstance(root, str) or not os.path.isabs(root):
        raise ConfigurationError("Nyx configuration root is invalid")
    try:
        canonical = Path(root).resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ConfigurationError("Nyx configuration root is invalid") from error
    return Configuration(canonical, hidden_stages)


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


def _configuration_unavailable() -> ConfigurationObservation:
    return ConfigurationObservation("unavailable", diagnostic=CONFIGURATION_UNAVAILABLE)


def observe_configuration() -> ConfigurationObservation:
    """Observe only the persisted configuration without inspecting runtime paths.

    A missing, validly absent configuration is represented as ``not_configured``.
    Every other configuration read or validation failure is deliberately collapsed
    to the bounded ``configuration_unavailable`` diagnostic.
    """

    try:
        paths = _fixed_state_paths()
        config_parent = paths.config_directory.parent
        if _is_missing(config_parent):
            return ConfigurationObservation("not_configured")
        _verify_general_directory(config_parent)
        if _is_missing(paths.config_directory):
            return ConfigurationObservation("not_configured")
        _verify_directory(paths.config_directory, _current_uid())
        if _is_missing(paths.config_file):
            return ConfigurationObservation("not_configured")
        configuration = load_configuration(paths)
    except StateError:
        return _configuration_unavailable()
    return ConfigurationObservation(
        "configured",
        specification_root=configuration.specification_root,
        hidden_stages=configuration.hidden_stages,
    )


_RUNTIME_RECORDS = frozenset({"operation.lock", "lease.lock", "instance.json"})


def observe_runtime() -> RuntimeObservation:
    """Observe the fixed runtime tree without reading configuration or creating it.

    Runtime file semantics (leases, locks, and instance records) are resolved by
    the lifecycle owner.  This state-level observer validates their fixed tree
    and reports a present-but-unresolved tree as ``unknown``.
    """

    try:
        paths = _fixed_state_paths()
        local_parent = paths.state_directory.parents[1]
        state_parent = paths.state_directory.parent
        for parent in (local_parent, state_parent):
            if _is_missing(parent):
                return RuntimeObservation("not_running", paths=paths)
            _verify_general_directory(parent)
        if _is_missing(paths.state_directory):
            return RuntimeObservation("not_running", paths=paths)
        _verify_directory(paths.state_directory, _current_uid())
        if _is_missing(paths.runtime_directory):
            return RuntimeObservation("unknown", paths=paths)
        _verify_directory(paths.runtime_directory, _current_uid())

        entries = tuple(paths.runtime_directory.iterdir())
        if not entries:
            return RuntimeObservation("not_running", paths=paths)
        for entry in entries:
            details = entry.lstat()
            if entry.name not in _RUNTIME_RECORDS:
                return RuntimeObservation("unknown", paths=paths)
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                return RuntimeObservation("unknown", paths=paths)
            if details.st_uid != _current_uid() or stat.S_IMODE(details.st_mode) != 0o600:
                return RuntimeObservation("unknown", paths=paths)
    except (OSError, StateError):
        return RuntimeObservation("unknown")
    return RuntimeObservation("unknown", paths=paths)


# Explicit path-oriented aliases make the ownership boundary visible to callers
# while retaining one implementation for the read-only runtime tree.
observe_configuration_paths = observe_configuration
observe_runtime_paths = observe_runtime


def _configuration_bytes(root: Path, hidden_stages: tuple[str, ...]) -> bytes:
    return (json.dumps(
        Configuration(root, hidden_stages).as_dict(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n").encode("utf-8")


def _atomic_write_configuration(
    paths: StatePaths, root: Path, hidden_stages: tuple[str, ...]
) -> None:
    uid = _current_uid()
    temporary_name: str | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{CONFIG_FILENAME}.", dir=paths.config_directory
        )
        try:
            os.fchmod(fd, 0o600)
            data = _configuration_bytes(root, hidden_stages)
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


def _save_configuration(
    paths: StatePaths,
    root: Path,
    hidden_stages: tuple[str, ...],
) -> Configuration:
    """Atomically persist a validated configuration while runtime owns authorization."""

    _require_linux()
    if paths.config_file.exists() or paths.config_file.is_symlink():
        _verify_record(paths.config_file, _current_uid())
    _atomic_write_configuration(paths, root, hidden_stages)
    return Configuration(root, hidden_stages)


def save_configuration(
    specification_root: str | os.PathLike[str],
    hidden_stages: Iterable[str] | object = _OMITTED,
) -> Configuration:
    """Delegate configuration replacement through the runtime authorization gate."""

    from . import runtime

    if hidden_stages is _OMITTED:
        return runtime.setup(specification_root)
    return runtime.setup(specification_root, hidden_stages=hidden_stages)


def setup(
    specification_root: str | os.PathLike[str],
    hidden_stages: Iterable[str] | object = _OMITTED,
) -> Configuration:
    """Delegate setup through the runtime authorization gate."""

    from . import runtime

    if hidden_stages is _OMITTED:
        return runtime.setup(specification_root)
    return runtime.setup(specification_root, hidden_stages=hidden_stages)


__all__ = [
    "CONFIG_SCHEMA_VERSION",
    "CONFIGURATION_STATES",
    "CONFIGURATION_UNAVAILABLE",
    "AccountHomeError",
    "Configuration",
    "ConfigurationError",
    "ConfigurationObservation",
    "HiddenStageError",
    "LEGACY_CONFIG_SCHEMA_VERSION",
    "RUNTIME_STATES",
    "RuntimeObservation",
    "SpecificationRootError",
    "StateError",
    "StatePaths",
    "UnsupportedPlatformError",
    "load_configuration",
    "observe_configuration",
    "observe_configuration_paths",
    "observe_runtime",
    "observe_runtime_paths",
    "resolve_account_home",
    "resolve_specification_root",
    "save_configuration",
    "setup",
    "state_paths",
]
