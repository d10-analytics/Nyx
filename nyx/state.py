"""Portable per-account configuration and state admission for Nyx.

This module owns the host-local ``Path.home() / ".nyx"`` tree, configuration
schema, structural admission, and atomic configuration replacement.  Lifecycle
ownership remains in :mod:`nyx.runtime`; this module never treats a locator or
runtime file as proof of ownership.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
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


def _check_deadline(deadline: float | None = None, deadline_ns: int | None = None) -> None:
    """Reject an expired operation before the next filesystem mutation."""

    if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
        raise OperationDeadlineError("Nyx operation deadline expired")
    if deadline is not None and time.monotonic() >= deadline:
        raise OperationDeadlineError("Nyx operation deadline expired")


class StateError(RuntimeError):
    """Base class for safe, user-facing state errors."""


class OperationDeadlineError(StateError, TimeoutError):
    """A public operation expired before its next state mutation."""


class UnsupportedPlatformError(StateError):
    """Retained for callers that import the historical exception type."""


class AccountHomeError(StateError):
    """The controlled home or a managed state component is unusable."""


class SpecificationRootError(StateError):
    """The requested specification root cannot be used."""


class ConfigurationError(StateError):
    """A persisted configuration is missing, unsafe, or malformed."""


class ConfigurationCommitVerificationError(ConfigurationError):
    """The replacement committed, but its immediate verification failed."""


class HiddenStageError(ConfigurationError):
    """A hidden-stage name is outside the admitted Unicode component domain."""


@dataclass(frozen=True)
class StatePaths:
    """The fixed paths belonging to the current user's home."""

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
    """A read-only view of the fixed runtime tree, independent of config."""

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
    """Compatibility no-op for callers of the former Linux-only owner."""


def _current_uid() -> int:
    """Retain the old test seam without making UID part of state admission."""

    try:
        return os.getuid()
    except AttributeError:  # pragma: no cover - only unusual Python hosts
        return -1


def resolve_account_home() -> Path:
    """Return the direct, controlled ``Path.home()`` value."""

    try:
        home = Path.home()
    except (RuntimeError, OSError) as error:
        raise AccountHomeError("current account home is unavailable") from error
    if not home.is_absolute():
        raise AccountHomeError("current account home is not absolute")
    return home


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _is_packaged_application() -> bool:
    """Report whether this module runs inside a standalone application.

    A standalone desktop build relocates the package and interpreter paths
    into an application directory or bundle, so the admission footprints must
    resolve the delivered roots instead of the build-time ones.
    """

    if getattr(sys, "frozen", False):
        return True
    compiled = globals().get("__compiled__")
    return bool(getattr(compiled, "standalone", False))


def _packaged_footprints() -> tuple[Path, ...]:
    """Resolve the packaged application, helper, and resource subtrees.

    The whole application installation is rejected so a workspace cannot be a
    subdirectory of the delivered application, and the helper and resource
    subtrees are named explicitly so admission stays correct even if the
    bundle root is not otherwise recognizable.
    """

    if not _is_packaged_application():
        return ()
    try:
        executable = Path(sys.executable).resolve(strict=True)
    except (OSError, RuntimeError):
        return ()
    roots: list[Path] = [executable.parent]
    bundle = next((parent for parent in executable.parents if parent.suffix == ".app"), None)
    if bundle is None:
        roots.append(executable.parent / "NyxWorker")
    else:
        roots.extend(
            (
                bundle,
                bundle / "Contents",
                bundle / "Contents" / "MacOS",
                bundle / "Contents" / "Frameworks",
                bundle / "Contents" / "Resources",
            )
        )
    return tuple(dict.fromkeys(roots))


def _installation_footprints() -> tuple[Path, ...]:
    """Return resolved Nyx package, interpreter, and packaged application roots."""

    candidates = [Path(__file__).resolve().parent]
    try:
        candidates.append(Path(sys.prefix).resolve(strict=True))
    except (OSError, RuntimeError):
        pass
    candidates.extend(_packaged_footprints())
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


def _is_reparse_or_link(path: Path, details: os.stat_result) -> bool:
    """Reject links and native Windows reparse points without following them."""

    if stat.S_ISLNK(details.st_mode):
        return True
    attributes = getattr(details, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if attributes & reparse_flag:
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        try:
            return bool(is_junction())
        except OSError:
            return True
    return False


def _lstat(path: Path) -> os.stat_result | None:
    """Return metadata, preserving a distinction between absent and unusable."""

    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise AccountHomeError("Nyx state path is unavailable") from error


def _identity(details: os.stat_result) -> tuple[int, int, int, int]:
    return (details.st_dev, details.st_ino, details.st_mode, details.st_size)


def _record_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Return the fields that expose record replacement or in-place mutation."""

    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _admit_directory(
    path: Path,
    *,
    create: bool,
    deadline: float | None = None,
    deadline_ns: int | None = None,
) -> bool:
    """Admit one managed directory, creating it only when requested."""

    _check_deadline(deadline, deadline_ns)
    before = _lstat(path)
    if before is None:
        if not create:
            return False
        # Metadata admission can block.  Do not let an expired public
        # operation create the next managed directory after that lookup.
        _check_deadline(deadline, deadline_ns)
        try:
            path.mkdir()
        except FileExistsError:
            pass
        except OSError as error:
            raise AccountHomeError("cannot create Nyx state directory") from error
        after = _lstat(path)
        if after is None:
            raise AccountHomeError("Nyx state directory disappeared during admission")
    else:
        after = _lstat(path)
        if after is None:
            raise AccountHomeError("Nyx state directory disappeared during admission")

    if _is_reparse_or_link(path, after) or not stat.S_ISDIR(after.st_mode):
        raise AccountHomeError("Nyx state directory is not an ordinary directory")
    if before is not None and _identity(before) != _identity(after):
        raise AccountHomeError("Nyx state directory changed during admission")
    return True


def _managed_paths() -> StatePaths:
    home = resolve_account_home()
    root = home / ".nyx"
    config = root / "config"
    runtime = root / RUNTIME_DIRECTORY
    return StatePaths(
        account_home=home,
        config_directory=config,
        config_file=config / CONFIG_FILENAME,
        state_directory=root,
        deployment_file=runtime / DEPLOYMENT_FILENAME,
        runtime_directory=runtime,
    )


def _fixed_state_paths() -> StatePaths:
    """Derive managed paths without validating or creating any child."""

    return _managed_paths()


def state_paths(
    *,
    create: bool = False,
    deadline: float | None = None,
    deadline_ns: int | None = None,
) -> StatePaths:
    """Return fixed paths and optionally admit the managed directories."""

    paths = _fixed_state_paths()
    if create:
        _admit_directory(paths.state_directory, create=True, deadline=deadline, deadline_ns=deadline_ns)
        _admit_directory(paths.config_directory, create=True, deadline=deadline, deadline_ns=deadline_ns)
        _admit_directory(paths.runtime_directory, create=True, deadline=deadline, deadline_ns=deadline_ns)
    else:
        for path in (paths.state_directory, paths.config_directory, paths.runtime_directory):
            if _lstat(path) is not None:
                _admit_directory(path, create=False)
    return paths


def _verify_record(path: Path, _uid: int | None = None) -> os.stat_result:
    try:
        details = path.lstat()
    except FileNotFoundError as error:
        raise ConfigurationError("Nyx configuration is missing") from error
    except OSError as error:
        raise ConfigurationError("Nyx configuration is unavailable") from error
    if _is_reparse_or_link(path, details) or not stat.S_ISREG(details.st_mode):
        raise ConfigurationError("Nyx configuration is not a regular file")
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
    except (OSError, RuntimeError, ValueError) as error:
        raise ConfigurationError("Nyx configuration root is invalid") from error
    return Configuration(canonical, hidden_stages)


def load_configuration(paths: StatePaths | None = None) -> Configuration:
    """Read and validate the setup record without checking root contents."""

    selected = state_paths() if paths is None else paths
    if not _admit_directory(selected.state_directory, create=False):
        raise ConfigurationError("Nyx configuration is missing")
    if not _admit_directory(selected.config_directory, create=False):
        raise ConfigurationError("Nyx configuration is missing")
    _verify_record(selected.config_file)
    try:
        payload = json.loads(selected.config_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("Nyx configuration cannot be read") from error
    return _configuration_from_payload(payload)


def _configuration_unavailable() -> ConfigurationObservation:
    return ConfigurationObservation("unavailable", diagnostic=CONFIGURATION_UNAVAILABLE)


def observe_configuration() -> ConfigurationObservation:
    """Observe configuration without creating, changing, or reading runtime."""

    try:
        paths = _fixed_state_paths()
        if not _admit_directory(paths.state_directory, create=False):
            return ConfigurationObservation("not_configured")
        if not _admit_directory(paths.config_directory, create=False):
            return ConfigurationObservation("not_configured")
        if _lstat(paths.config_file) is None:
            return ConfigurationObservation("not_configured")
        before = _verify_record(paths.config_file)
        configuration = load_configuration(paths)
        after = _verify_record(paths.config_file)
        if _record_identity(before) != _record_identity(after):
            return _configuration_unavailable()
    except (StateError, OSError, ValueError):
        return _configuration_unavailable()
    return ConfigurationObservation(
        "configured",
        specification_root=configuration.specification_root,
        hidden_stages=configuration.hidden_stages,
    )


_RUNTIME_RECORDS = frozenset({"operation.lock", "lease.lock", "instance.json"})


def observe_runtime() -> RuntimeObservation:
    """Observe runtime structure without reading configuration or creating it."""

    paths: StatePaths | None = None
    try:
        paths = _fixed_state_paths()
        if not _admit_directory(paths.state_directory, create=False):
            return RuntimeObservation("not_running", paths=paths)
        if not _admit_directory(paths.runtime_directory, create=False):
            return RuntimeObservation("not_running", paths=paths)
        entries = tuple(paths.runtime_directory.iterdir())
        if not entries:
            return RuntimeObservation("not_running", paths=paths)
        for entry in entries:
            details = entry.lstat()
            if entry.name not in _RUNTIME_RECORDS:
                return RuntimeObservation("unknown", paths=paths)
            if _is_reparse_or_link(entry, details) or not stat.S_ISREG(details.st_mode):
                return RuntimeObservation("unknown", paths=paths)
    except (OSError, StateError):
        return RuntimeObservation("unknown", paths=paths)
    return RuntimeObservation("unknown", paths=paths)


observe_configuration_paths = observe_configuration
observe_runtime_paths = observe_runtime


def _configuration_bytes(root: Path, hidden_stages: tuple[str, ...]) -> bytes:
    return (
        json.dumps(
            Configuration(root, hidden_stages).as_dict(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sync_directory(path: Path) -> None:
    """Best-effort directory flush on hosts that expose directory handles."""

    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(path, flags)
    except (AttributeError, OSError):
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_write_configuration(
    paths: StatePaths,
    root: Path,
    hidden_stages: tuple[str, ...],
    *,
    deadline: float | None = None,
    deadline_ns: int | None = None,
) -> None:
    temporary_name: str | None = None
    replaced = False
    try:
        if not _admit_directory(paths.state_directory, create=False) or not _admit_directory(
            paths.config_directory, create=False
        ):
            raise ConfigurationError("Nyx configuration directory is unavailable")
        _check_deadline(deadline, deadline_ns)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{CONFIG_FILENAME}.", dir=paths.config_directory
        )
        try:
            data = _configuration_bytes(root, hidden_stages)
            written = 0
            while written < len(data):
                _check_deadline(deadline, deadline_ns)
                written += os.write(fd, data[written:])
            _check_deadline(deadline, deadline_ns)
            os.fsync(fd)
        finally:
            os.close(fd)
        _check_deadline(deadline, deadline_ns)
        os.replace(temporary_name, paths.config_file)
        temporary_name = None
        replaced = True
        _verify_record(paths.config_file)
        _sync_directory(paths.config_directory)
    except (OSError, ValueError, ConfigurationError) as error:
        if isinstance(error, ConfigurationError):
            if replaced and not isinstance(error, ConfigurationCommitVerificationError):
                raise ConfigurationCommitVerificationError(
                    "Nyx configuration was replaced but could not be verified"
                ) from error
            raise
        if replaced:
            raise ConfigurationCommitVerificationError(
                "Nyx configuration was replaced but could not be verified"
            ) from error
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
    *,
    deadline: float | None = None,
    deadline_ns: int | None = None,
) -> Configuration:
    """Atomically persist validated configuration under runtime authorization."""

    if _lstat(paths.config_file) is not None:
        _verify_record(paths.config_file)
    _atomic_write_configuration(
        paths, root, hidden_stages, deadline=deadline, deadline_ns=deadline_ns
    )
    return Configuration(root, hidden_stages)


def validate_configuration_candidate(
    specification_root: str | os.PathLike[str],
    hidden_stages: Iterable[str] | object = _OMITTED,
    *,
    paths: StatePaths | None = None,
) -> Configuration:
    """Validate a prospective configuration without changing persisted state."""

    selected_paths = state_paths() if paths is None else paths
    root = resolve_specification_root(specification_root)
    if hidden_stages is _OMITTED:
        if _lstat(selected_paths.config_file) is None:
            validated_hidden_stages = ()
        else:
            validated_hidden_stages = load_configuration(selected_paths).hidden_stages
    else:
        validated_hidden_stages = _validate_hidden_stages(hidden_stages)
    return Configuration(root, validated_hidden_stages)


def save_configuration_owned(
    specification_root: str | os.PathLike[str],
    hidden_stages: Iterable[str] | object = _OMITTED,
    *,
    paths: StatePaths | None = None,
    deadline: float | None = None,
    deadline_ns: int | None = None,
) -> Configuration:
    """Persist validated configuration for a caller holding lifecycle claims.

    Desktop ownership already holds the account and recovery claims.  This
    seam deliberately bypasses the public setup aliases, which would try to
    acquire the account lease a second time, while retaining this module's
    canonical root, hidden-stage, and atomic-write validation.
    """

    selected_paths = (
        state_paths(create=True, deadline=deadline, deadline_ns=deadline_ns)
        if paths is None
        else paths
    )
    candidate = validate_configuration_candidate(
        specification_root,
        hidden_stages,
        paths=selected_paths,
    )
    return _save_configuration(
        selected_paths,
        candidate.specification_root,
        candidate.hidden_stages,
        deadline=deadline,
        deadline_ns=deadline_ns,
    )


def revalidate_configuration(paths: StatePaths | None = None) -> Configuration:
    """Reload a committed record through the state owner's validation path."""

    selected = state_paths() if paths is None else paths
    try:
        before = _verify_record(selected.config_file)
        configuration = load_configuration(selected)
        after = _verify_record(selected.config_file)
    except StateError:
        raise
    except (OSError, ValueError) as error:
        raise ConfigurationError("Nyx configuration could not be revalidated") from error
    if _record_identity(before) != _record_identity(after):
        raise ConfigurationError("Nyx configuration changed during validation")
    return configuration


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
    "ConfigurationCommitVerificationError",
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
    "save_configuration_owned",
    "validate_configuration_candidate",
    "revalidate_configuration",
    "setup",
    "state_paths",
]
