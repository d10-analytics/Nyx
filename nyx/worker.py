"""Private catalog and checkouts worker processes and direct-child cleanup owner.

One worker process runs one kind: the catalog scan, or one collection of the
configured checkout folders.  Both kinds take the same inherited objects, the
same spawn protocol, stdout and stderr caps, the same timeout, cancellation and
reaping mechanism, and the same exit-status categories; only the kind's
arguments, its budget and the names its failures are published under differ.

A checkouts worker contains the programs it starts.  The manager gives it a
session of its own on POSIX, so the manager stops the whole group it leads at
either terminate site, the worker sweeps the same group once its document is
out, and a worker that loses its owner force-kills that group because no
manager remains to stop anything.  On Windows the worker owns a kill-on-close
job object, so the kernel ends every program it started however it ends.  A
catalog worker starts no program and keeps its unchanged spawn and its
process-only termination.
"""

from __future__ import annotations

import os
import select
import signal
import site
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import state
from ._native_claim import NativeClaim
from .catalog import MAX_OUTPUT_BYTES, CatalogOutputTooLargeError, scan_catalog

if os.name == "nt":  # pragma: no cover - exercised by the native Windows lane
    import ctypes
    from ctypes import wintypes

MAX_STDOUT_BYTES = MAX_OUTPUT_BYTES
MAX_STDERR_BYTES = 8 * 1024
WORKER_TIMEOUT = 5.0
CHECKOUTS_TIMEOUT = 30.0
NETWORKED_CHECKOUTS_TIMEOUT = 60.0
# The share of a checkouts budget kept back for interpreter start, rendering and
# exit, so a collection that reaches its own deadline still returns a document.
START_RESERVE_SECONDS = 1.5
MAX_DEADLINE_MS = 600_000
CATALOG_KIND = "catalog"
CHECKOUTS_KIND = "checkouts"
WORKER_KINDS = (CATALOG_KIND, CHECKOUTS_KIND)
_READ_CHUNK_BYTES = 64 * 1024
WORKER_EXECUTABLE_NAME = "NyxWorker"
WORKER_HELPER_DIRECTORY = "NyxWorker"
# Categories for a worker's nonzero exit status; any other status is a failure.
_EXIT_STATUS_CATEGORIES = {
    3: "producer_output_too_large",
    4: "producer_unavailable",
}
# The manager's terminal reasons, translated for the checkouts kind only.  It is
# a closed set: a checkouts caller must never see a catalog kind's category, so
# an unlisted reason falls back to the kind's general failure.
_CHECKOUTS_CATEGORIES = {
    "producer_cancelled": "checkouts_cancelled",
    "producer_unavailable": "checkouts_unavailable",
    "producer_timeout": "checkouts_timeout",
    "producer_output_too_large": "checkouts_output_too_large",
    "producer_failed": "checkouts_failed",
}


def _deadline_ms(timeout: float) -> int:
    """Return the collector deadline a checkouts worker gets for one budget.

    The value is relative, because a monotonic instant means nothing in another
    process.  The reserve keeps a collection that cannot finish inside its
    calls' cap, so the worker still renders ``unknown`` and ``deadline`` values
    rather than the manager losing the answer to its own budget.
    """

    return max(0, int((timeout - START_RESERVE_SECONDS) * 1000))


def _arguments_admitted(kind: str, network: bool, deadline_ms: int | None) -> bool:
    """Report whether one worker argument set is admitted for its kind.

    The catalog kind accepts neither of the checkouts kind's arguments, which
    keeps the catalog command line unchanged.  The checkouts kind requires a
    deadline inside the admitted range, and the networked mode is optional.
    """

    if kind == CATALOG_KIND:
        return not network and deadline_ms is None
    if kind != CHECKOUTS_KIND or deadline_ms is None:
        return False
    return 0 <= deadline_ms <= MAX_DEADLINE_MS


def _is_packaged_application() -> bool:
    """Report whether this module runs inside a standalone application.

    A frozen interpreter no longer exposes a Python launcher, so the worker
    command must not be reconstructed from ``sys.executable`` there.  The
    shared state check is consulted at call time so every packaged-build
    decision follows one predicate.
    """

    return state._is_packaged_application()


def _packaged_executable_roots() -> tuple[Path, ...]:
    """Resolve the directories that can hold the staged console helper.

    A standalone build may report its launcher through ``sys.executable`` or
    ``sys.argv[0]``, and a windowed Windows build additionally exposes the
    real image path through ``GetModuleFileNameW``.  Every candidate is
    resolved so the helper is found beside whichever path describes the
    running application.
    """

    candidates: list[str] = []
    if sys.executable:
        candidates.append(sys.executable)
    if getattr(sys, "argv", None):
        candidates.append(sys.argv[0])
    if os.name == "nt":  # pragma: no cover - exercised by the native Windows lane
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            buffer = ctypes.create_unicode_buffer(32768)
            if kernel32.GetModuleFileNameW(None, buffer, len(buffer)):
                candidates.append(buffer.value)
        except (OSError, AttributeError):
            pass
    roots: list[Path] = []
    for candidate in candidates:
        if not candidate:
            continue
        try:
            roots.append(Path(candidate).resolve(strict=True).parent)
        except (OSError, RuntimeError):
            continue
    return tuple(dict.fromkeys(roots))


def bundled_worker_command() -> list[str] | None:
    """Return the packaged console helper command when one is staged.

    The helper lives in a dedicated directory beside the application
    executable (Windows) or beside the bundle executable (macOS).  Returning
    ``None`` rather than a Python launcher keeps a missing helper from
    re-entering the GUI the way a stray interpreter path would.
    """

    if not _is_packaged_application():
        return None
    name = (
        f"{WORKER_EXECUTABLE_NAME}.exe"
        if os.name == "nt"
        else WORKER_EXECUTABLE_NAME
    )
    for root in _packaged_executable_roots():
        for candidate in (
            root / WORKER_HELPER_DIRECTORY / name,
            root / name,
        ):
            if candidate.is_file():
                return [str(candidate)]
    return None


def default_worker_command() -> list[str]:
    """Select the catalog worker entry for the current runtime.

    A standalone application must spawn its bundled console helper.  A source
    or virtual-environment run uses the module entry with ``-P`` so the
    worker's working directory never joins its import path; that worker
    receives its import root from ``source_child_environment``.
    """

    if _is_packaged_application():
        bundled = bundled_worker_command()
        if bundled is None:
            raise WorkerError("producer_unavailable")
        return bundled
    return [sys.executable, "-P", "-m", "nyx.worker"]


def _package_directory() -> str:
    """Return the directory holding the ``nyx`` package this process imported.

    The value comes from this module's own location at call time, so it names
    the code the starting process already loaded rather than the working
    directory, the launch arguments, or the environment.  Only ``os.path`` is
    used, so the derivation also works under a patched ``os.name``.
    """

    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def _canonical_directory(path: str) -> str:
    """Return the form one directory path is compared in."""

    return os.path.normcase(os.path.realpath(path))


def source_child_environment() -> dict[str, str] | None:
    """Return the environment a source-run child needs to import this package.

    ``None`` means the child inherits this process's environment unchanged: a
    packaged application starts its bundled helper, and a run loaded from one
    of this interpreter's site folders already resolves the same package while
    no import path is set and user site packages are enabled.  Every other
    source run receives a new mapping whose ``PYTHONPATH`` starts with the
    package directory, followed by any inherited entries verbatim.  A package
    directory that cannot be one import-path entry is refused before any child
    is started.
    """

    if _is_packaged_application():
        return None
    directory = _package_directory()
    inherited = os.environ.get("PYTHONPATH") or ""
    if not inherited and not sys.flags.no_user_site:
        installed = site.getsitepackages()
        if site.ENABLE_USER_SITE:
            installed = [*installed, site.getusersitepackages()]
        resolved = _canonical_directory(directory)
        if any(_canonical_directory(entry) == resolved for entry in installed):
            return None
    if os.pathsep in directory:
        raise WorkerError("producer_unavailable")
    return {
        **os.environ,
        "PYTHONPATH": directory if not inherited else directory + os.pathsep + inherited,
    }


def _windows_parent_pipe_closed(fd: int) -> bool:
    """Observe Windows anonymous-pipe closure without a blocking read."""

    if os.name != "nt":
        raise RuntimeError("Windows pipe observation is unavailable")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.PeekNamedPipe.restype = wintypes.BOOL
    handle = wintypes.HANDLE(NativeClaim.transfer_handle(fd))
    if kernel32.PeekNamedPipe(handle, None, 0, None, None, None):
        return False
    error = ctypes.get_last_error()
    if error in {109, 233}:
        return True
    raise OSError(error, "PeekNamedPipe failed")


def _leads_own_session() -> bool:
    """Report whether this process leads the session its programs inherit.

    Only such a worker may signal its own process group: any other worker
    shares its starter's group, so a group signal there would stop the starter
    instead of only the programs this worker started.
    """

    try:
        return os.getsid(0) == os.getpid()
    except (AttributeError, OSError):  # pragma: no cover - no sessions on Windows
        return False


def _stop_descendants() -> None:
    """Stop the programs this worker started, once its document is out.

    The worker ignores the signal for itself first, so the stop reaches what it
    started and nothing more, and it signals nothing when it does not lead its
    own session.  A program that deliberately ignores that signal survives, and
    a worker the operating system ends outright runs no sweep at all.
    """

    if not _leads_own_session():
        return
    try:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    except (AttributeError, OSError, ValueError):  # pragma: no cover - signal setup
        return
    try:
        os.killpg(os.getpgid(0), signal.SIGTERM)
    except OSError:
        pass


def _end_on_owner_loss(kind: str) -> None:
    """End a worker whose owner is gone.

    A checkouts worker that leads its own session force-kills the group it owns,
    because no manager remains to stop those programs afterwards; every other
    worker keeps the plain exit status.
    """

    if kind == CHECKOUTS_KIND and _leads_own_session():
        try:
            os.killpg(os.getpgid(0), signal.SIGKILL)
        except OSError:
            pass
    os._exit(7)


if os.name == "nt":  # pragma: no cover - exercised by the native Windows lane

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    # The worker holds the only, non-inheritable job handle; the kernel closes
    # it when the process ends, however it ends, and the job with it.
    _containment_job: int | None = None

    class _JobBasicLimits(ctypes.Structure):
        """The fixed-shape lead of a job object's extended limits."""

        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JobIOCounters(ctypes.Structure):
        """The input and output counters every extended-limits value carries."""

        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _JobExtendedLimits(ctypes.Structure):
        """The extended limits of one job object."""

        _fields_ = [
            ("BasicLimitInformation", _JobBasicLimits),
            ("IoInfo", _JobIOCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    def _contain_descendants() -> bool:
        """Give this worker a job that ends its programs with it.

        The job is created kill-on-close and the worker joins it before its
        first child, so a normal end, a terminated process and ``_exit`` all
        end every descendant.  ``False`` means no such guarantee exists, which
        the caller reports as an unavailable worker.
        """

        global _containment_job
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = (
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            )
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = (
                wintypes.HANDLE,
                wintypes.HANDLE,
            )
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.GetCurrentProcess.argtypes = ()
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return False
            limits = _JobExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = (
                _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            if not kernel32.SetInformationJobObject(
                job,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                return False
            if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
                return False
        except (AttributeError, OSError):
            return False
        _containment_job = job
        return True

else:

    def _contain_descendants() -> bool:
        """Report that this worker needs no job: its session is the containment."""

        return True


class WorkerError(RuntimeError):
    """A safe category returned by a catalog worker."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _ParentLossObserver:
    """Terminate a worker once its liveness pipe's write end is closed by its holder."""

    def __init__(self, fd: int, kind: str = CATALOG_KIND) -> None:
        self.fd = fd
        self.kind = kind
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._observe,
            daemon=True,
            name="nyx-worker-parent-observer",
        )

    def start(self) -> None:
        self._thread.start()

    def _end(self) -> None:
        """End this worker, taking a contained kind's programs with it."""

        _end_on_owner_loss(self.kind)

    def _observe(self) -> None:
        try:
            while not self._stop.is_set():
                if os.name == "nt":
                    try:
                        parent_closed = _windows_parent_pipe_closed(self.fd)
                    except OSError:
                        return
                    if parent_closed and not self._stop.is_set():
                        self._end()
                    self._stop.wait(0.05)
                    continue
                try:
                    readable, _, _ = select.select([self.fd], [], [], 0.1)
                except (OSError, ValueError):
                    return
                if not readable:
                    continue
                try:
                    data = getattr(os, "read")(self.fd, 1)
                except OSError:
                    return
                if not data and not self._stop.is_set():
                    self._end()
        finally:
            try:
                os.close(self.fd)
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self._thread.join()


def _receive_worker_inheritance(
    recovery_fd: int | None,
    recovery_path: Path | None,
    parent_liveness_fd: int | None,
    *,
    kind: str = CATALOG_KIND,
) -> tuple[NativeClaim | None, _ParentLossObserver | None] | None:
    """Validate a worker's inherited objects before configuration can be loaded.

    The accepted set is chosen only by which arguments are present: none,
    parent liveness alone, or a recovery claim with its path and parent
    liveness.  Any other set is refused, and a claim that fails reception or
    adoption is refused rather than accepted as parent liveness alone.
    """

    if parent_liveness_fd is None:
        if recovery_fd is None and recovery_path is None:
            return (None, None)
        return None
    if (recovery_fd is None) != (recovery_path is None):
        return None
    received_fd: int | None = None
    liveness_fd: int | None = None
    claim: NativeClaim | None = None
    observer: _ParentLossObserver | None = None
    succeeded = False
    try:
        if recovery_fd is not None and recovery_path is not None:
            received_fd = NativeClaim.receive_handle(recovery_fd)
            liveness_fd = NativeClaim.receive_handle(parent_liveness_fd, read_only=True)
            claim = NativeClaim(recovery_path)
            if not claim.adopt_received(received_fd):
                return None
            received_fd = None
        else:
            liveness_fd = NativeClaim.receive_handle(parent_liveness_fd, read_only=True)
        observer = _ParentLossObserver(liveness_fd, kind)
        observer.start()
        liveness_fd = None
        succeeded = True
        return claim, observer
    except (OSError, RuntimeError, ValueError):
        return None
    finally:
        if not succeeded and claim is not None:
            claim.close()
        for fd in (received_fd, liveness_fd):
            if fd is None:
                continue
            try:
                os.close(fd)
            except OSError:
                pass


def _worker_main(
    *,
    recovery_fd: int | None = None,
    recovery_path: Path | None = None,
    parent_liveness_fd: int | None = None,
    kind: str = CATALOG_KIND,
    network: bool = False,
    deadline_ms: int | None = None,
) -> int:
    """Run one worker kind using the persisted private configuration.

    The catalog kind scans the configured specification root and the checkouts
    kind collects the configured checkout folders once.  An argument set the
    kind does not admit is refused before anything is inherited or loaded, and
    every other policy is shared by both kinds.
    """

    if not _arguments_admitted(kind, network, deadline_ms):
        return 4
    inherited = _receive_worker_inheritance(
        recovery_fd, recovery_path, parent_liveness_fd, kind=kind
    )
    if inherited is None:
        if recovery_fd is not None or recovery_path is not None or parent_liveness_fd is not None:
            return 4
        claim = None
        observer = None
    else:
        claim, observer = inherited

    try:
        if kind == CHECKOUTS_KIND:
            assert deadline_ms is not None
            return _run_checkouts(network=network, deadline_ms=deadline_ms)
        return _run_catalog()
    finally:
        if observer is not None:
            observer.close()
        if claim is not None:
            claim.close()


def _run_catalog() -> int:
    """Scan the configured specification root as one catalog document."""

    try:
        configuration = state.load_configuration()
        rendered = scan_catalog(
            configuration.specification_root,
            completed_stage_names=configuration.completed_stage_names,
            configuration_revision=configuration.revision,
        )
        encoded = rendered.encode("utf-8")
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
        return 0
    except (state.ConfigurationError, state.AccountHomeError, state.UnsupportedPlatformError):
        return 4
    except CatalogOutputTooLargeError:
        return 3
    except (OSError, ValueError, UnicodeError):
        return 5
    except Exception:  # noqa: BLE001 - worker boundary has a category-safe envelope
        return 6


def _run_checkouts(*, network: bool, deadline_ms: int) -> int:
    """Collect the configured checkout folders as one checkout document.

    The containment job exists before the first program this worker starts.
    ``nyx.checkouts`` is imported here, so a catalog worker's start time does
    not include the collector's import.  An unavailable configuration is
    reported as the kind's unavailable status, and a not-configured account
    still emits a document whose folders are empty.
    """

    if not _contain_descendants():
        return 4
    deadline_ns = time.monotonic_ns() + deadline_ms * 1_000_000
    from . import checkouts

    try:
        observation = state.observe_configuration()
        inputs = checkouts.collection_inputs(observation)
    except state.StateError:
        # observe_configuration itself reports an unreadable record as an
        # unavailable observation; this catch mirrors the command line's
        # handling so both entries keep the same contract.
        return 4
    if inputs is None:
        return 4
    try:
        rendered = checkouts.collect_checkouts(
            inputs.folders,
            deadline_ns=deadline_ns,
            remote_checks=network,
            github_lookup=inputs.github_lookup,
            package_scan=inputs.package_scan,
            configuration_revision=inputs.configuration_revision,
        )
        encoded = rendered.encode("utf-8")
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
        return 0
    except checkouts.CheckoutsOutputTooLargeError:
        return 3
    except (OSError, ValueError, UnicodeError):
        return 5
    except Exception:  # noqa: BLE001 - worker boundary has a category-safe envelope
        return 6
    finally:
        # After the document is out, because a call that timed out can leave the
        # program it started behind and only this worker's group knows of it.
        _stop_descendants()


@dataclass
class _Child:
    process: subprocess.Popen[bytes]
    kind: str = CATALOG_KIND
    cancelled: bool = False
    terminal_reason: str | None = None
    finalizer_started: bool = False
    finalizer_done: threading.Event = field(default_factory=threading.Event)
    progress: threading.Event = field(default_factory=threading.Event)
    readers_complete: threading.Event = field(default_factory=threading.Event)
    reader_threads: list[threading.Thread] = field(default_factory=list)
    reader_count: int = 0
    reader_done_count: int = 0
    retained_bytes: dict[str, int] = field(default_factory=lambda: {"stdout": 0, "stderr": 0})
    captured: dict[str, bytearray] = field(
        default_factory=lambda: {"stdout": bytearray(), "stderr": bytearray()}
    )


class _Reservation:
    """Admission reservation held while a direct child is being spawned.

    Each reservation is compared by identity, so a request only ever removes
    its own reservation however overlapping spawns complete.
    """


@dataclass(frozen=True)
class _InheritedObjects:
    """The objects one catalog worker inherits from its manager.

    One derived value supplies both the descriptors the platform passes to
    the child and the arguments that name them, so the two cannot disagree.
    """

    recovery_fd: int | None = None
    recovery_path: Path | None = None
    parent_liveness_fd: int | None = None

    @property
    def descriptors(self) -> tuple[int, ...]:
        return tuple(
            fd for fd in (self.recovery_fd, self.parent_liveness_fd) if fd is not None
        )

    def arguments(self) -> list[str]:
        arguments: list[str] = []
        if self.recovery_fd is not None:
            arguments += ["--recovery-fd", str(NativeClaim.transfer_handle(self.recovery_fd))]
        if self.recovery_path is not None:
            arguments += ["--recovery-path", str(self.recovery_path)]
        if self.parent_liveness_fd is not None:
            arguments += [
                "--parent-liveness-fd",
                str(NativeClaim.transfer_handle(self.parent_liveness_fd)),
            ]
        return arguments


class CatalogWorkerManager:
    """Admit, register, cancel and reap worker children of either kind.

    When no ``command_factory`` is given, a source-run worker also receives
    its import environment from ``source_child_environment`` so it loads the
    package this process loaded.  An injected factory keeps the inherited
    environment unchanged.
    """

    def __init__(
        self,
        *,
        command_factory: Callable[[], list[str]] | None = None,
        timeout: float = WORKER_TIMEOUT,
        checkouts_timeout: float = CHECKOUTS_TIMEOUT,
        networked_checkouts_timeout: float = NETWORKED_CHECKOUTS_TIMEOUT,
        recovery_claim: NativeClaim | None = None,
        recovery_path: Path | None = None,
        parent_liveness_fd: int | None = None,
        working_directory: Path | None = None,
    ) -> None:
        """Configure worker admission.

        ``working_directory`` is the directory every worker spawns in.  When
        it is ``None`` a worker inherits this process's directory.  A given
        directory that is missing fails the fetch; there is no fallback to the
        inherited directory.  ``timeout`` is the catalog kind's budget, and
        ``checkouts_timeout`` and ``networked_checkouts_timeout`` are the
        checkouts kind's local and networked budgets.  A fetch names its kind,
        and no kind is stored here.
        """

        self._command_factory = command_factory or default_worker_command
        self._default_command = command_factory is None
        self._timeout = timeout
        self._checkouts_timeout = checkouts_timeout
        self._networked_checkouts_timeout = networked_checkouts_timeout
        self._working_directory = working_directory
        self._recovery_claim = recovery_claim
        self._recovery_path = recovery_path or (
            None if recovery_claim is None else recovery_claim.path
        )
        self._parent_liveness_fd = parent_liveness_fd
        self._lock = threading.RLock()
        self._closing = False
        self._children: list[_Child] = []
        self._reservations: list[_Reservation] = []

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._children) + len(self._reservations)

    def fetch_catalog(self) -> bytes:
        """Fetch one catalog, ensuring every spawned child is registered."""

        return self._fetch(CATALOG_KIND)

    def fetch_checkouts(self, *, network: bool) -> bytes:
        """Fetch one checkout document, local or networked.

        The kind belongs to this call alone: both kinds run over the same
        reservation, spawn, reader, collect and finalizer path, and every
        failure is published under this kind's own categories.
        """

        return self._fetch(CHECKOUTS_KIND, network=network)

    def _fetch(self, kind: str, *, network: bool = False) -> bytes:
        """Run one child of ``kind`` and publish its document or its category."""

        try:
            return self._spawn_and_collect(kind, network=network)
        except WorkerError as error:
            if kind == CATALOG_KIND:
                raise
            raise WorkerError(
                _CHECKOUTS_CATEGORIES.get(error.code, "checkouts_failed")
            ) from error
        except OSError as error:
            # A failed spawn escapes raw for the catalog kind; a checkouts fetch
            # reports its own unavailable status for a missing directory or a
            # refused process creation.
            if kind == CATALOG_KIND:
                raise
            raise WorkerError(_CHECKOUTS_CATEGORIES["producer_unavailable"]) from error

    def _spawn_and_collect(self, kind: str, *, network: bool) -> bytes:
        """Reserve, spawn, register and collect one child of ``kind``."""

        timeout = self._timeout_for(kind, network)
        reservation = _Reservation()
        with self._lock:
            if self._closing:
                raise WorkerError("producer_cancelled")
            self._reservations.append(reservation)
        try:
            spawn_kwargs = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "close_fds": True,
            }
            if self._working_directory is not None:
                spawn_kwargs["cwd"] = self._working_directory
            # A checkouts worker leads its own session, so the one group the
            # manager signals holds exactly the programs that worker starts and
            # their children.  A catalog worker starts none and keeps its
            # unchanged spawn.
            if kind == CHECKOUTS_KIND and os.name != "nt":
                spawn_kwargs["start_new_session"] = True
            # Bound before any call that can raise, because the platform cleanup
            # below iterates it whatever failed.
            inherited_handles: list[int] = []
            if self._default_command:
                child_environment = source_child_environment()
                if child_environment is not None:
                    spawn_kwargs["env"] = child_environment
            inherited = self._inherited_objects()
            if inherited.descriptors:
                if os.name != "nt":
                    spawn_kwargs["pass_fds"] = inherited.descriptors
                else:  # pragma: no cover - exercised by the native Windows lane
                    inherited_handles = [
                        NativeClaim.transfer_handle(fd) for fd in inherited.descriptors
                    ]
                    for handle in inherited_handles:
                        os.set_handle_inheritable(handle, True)
                    startup = subprocess.STARTUPINFO()
                    startup.lpAttributeList = {"handle_list": inherited_handles}
                    spawn_kwargs["startupinfo"] = startup
            process = subprocess.Popen(
                self._worker_command(kind, network, inherited),
                **spawn_kwargs,
            )
        except BaseException:
            with self._lock:
                self._reservations.remove(reservation)
            raise
        finally:
            if os.name == "nt":  # pragma: no cover - native Windows lane
                for handle in inherited_handles:
                    os.set_handle_inheritable(handle, False)

        child = _Child(process, kind)
        with self._lock:
            self._reservations.remove(reservation)
            self._children.append(child)
            self._start_readers_locked(child)
            if self._closing:
                child.cancelled = True
                self._select_terminal_locked(child, "producer_cancelled")

        try:
            return self._collect(child, timeout)
        finally:
            # Cleanup is manager-owned and deliberately continues after the
            # request returns.  In particular, do not remove a child merely
            # because poll() reports exit while a reader still owns a pipe.
            with self._lock:
                if child.terminal_reason is None:
                    self._select_terminal_locked(child, "producer_failed")
                else:
                    self._start_finalizer_locked(child)

    def _inherited_objects(self) -> _InheritedObjects:
        """Derive what a worker inherits: nothing, liveness, or claim and liveness.

        A configured recovery claim requires its descriptor, its path and the
        parent liveness descriptor; without all three no worker may start.
        """

        if self._recovery_claim is None:
            return _InheritedObjects(parent_liveness_fd=self._parent_liveness_fd)
        if (
            self._recovery_claim.fd is None
            or self._recovery_path is None
            or self._parent_liveness_fd is None
        ):
            raise WorkerError("producer_unavailable")
        return _InheritedObjects(
            recovery_fd=self._recovery_claim.fd,
            recovery_path=self._recovery_path,
            parent_liveness_fd=self._parent_liveness_fd,
        )

    def _timeout_for(self, kind: str, network: bool) -> float:
        """Return the budget of one kind and mode."""

        if kind == CHECKOUTS_KIND:
            return (
                self._networked_checkouts_timeout
                if network
                else self._checkouts_timeout
            )
        return self._timeout

    def _worker_command(
        self, kind: str, network: bool, inherited: _InheritedObjects
    ) -> list[str]:
        """Add the kind's own arguments and those naming inherited objects.

        A catalog worker's command line gains nothing, so its spawn is
        unchanged.  A checkouts worker names its kind, the collector deadline
        derived from its budget, and the networked mode when it is on.
        """

        command = list(self._command_factory())
        if kind == CHECKOUTS_KIND:
            command += [
                "--kind",
                CHECKOUTS_KIND,
                "--deadline-ms",
                str(_deadline_ms(self._timeout_for(kind, network))),
            ]
            if network:
                command.append("--network")
        return [*command, *inherited.arguments()]

    def _start_readers_locked(self, child: _Child) -> None:
        """Start one blocking reader per pipe while the child is registered."""

        if child.reader_threads:
            return
        streams = (
            ("stdout", child.process.stdout, MAX_STDOUT_BYTES),
            ("stderr", child.process.stderr, MAX_STDERR_BYTES),
        )
        for name, stream, limit in streams:
            if stream is None:
                continue
            reader = threading.Thread(
                target=self._read_stream,
                args=(child, name, stream, limit),
                daemon=True,
                name=f"nyx-worker-{name}",
            )
            child.reader_threads.append(reader)
            child.reader_count += 1
            reader.start()

    def _read_stream(self, child: _Child, name: str, stream: object, limit: int) -> None:
        """Drain one ordinary blocking pipe, retaining only its bounded prefix."""

        try:
            while True:
                chunk = stream.read(_READ_CHUNK_BYTES)  # type: ignore[attr-defined]
                if not chunk:
                    break
                with self._lock:
                    if child.terminal_reason is not None:
                        # Once another terminal reason wins, continue draining
                        # so the sibling reader and child can reach EOF without
                        # retaining bytes that cannot be published.
                        continue
                    remaining = limit - len(child.captured[name])
                    if len(chunk) > remaining:
                        if remaining:
                            child.captured[name].extend(chunk[:remaining])
                        child.retained_bytes[name] = len(child.captured[name])
                        self._select_terminal_locked(child, "producer_output_too_large")
                        continue
                    child.captured[name].extend(chunk)
                    child.retained_bytes[name] = len(child.captured[name])
        except (OSError, ValueError):
            with self._lock:
                if child.terminal_reason is None:
                    self._select_terminal_locked(child, "producer_failed")
        finally:
            with self._lock:
                child.reader_done_count += 1
                if child.reader_done_count >= child.reader_count:
                    child.readers_complete.set()
                child.progress.set()

    def _terminate(self, child: _Child) -> None:
        """Stop one child: a checkouts worker's group, a catalog worker's process.

        A checkouts worker leads the group the manager started it in, so its own
        programs and their children stop with it.  The group is signalled only
        while its leader is unreaped, because a reaped leader's group id could
        name an unrelated process.  A catalog worker starts no program and keeps
        the process-only termination its resistant-child proofs require.
        """

        if child.process.poll() is not None:
            return
        if child.kind == CHECKOUTS_KIND and os.name != "nt":
            try:
                os.killpg(child.process.pid, signal.SIGTERM)
            except OSError:
                pass
            return
        try:
            child.process.terminate()
        except (OSError, ProcessLookupError):
            pass

    def _select_terminal_locked(self, child: _Child, reason: str) -> None:
        """Select the first terminal request reason and start its finalizer."""

        if child.terminal_reason is not None:
            return
        child.terminal_reason = reason
        if reason != "producer_failed":
            child.cancelled = reason in {
                "producer_cancelled",
                "producer_timeout",
                "producer_output_too_large",
            }
        if reason != "producer_failed":
            self._terminate(child)
        child.progress.set()
        self._start_finalizer_locked(child)

    def _start_finalizer_locked(self, child: _Child) -> None:
        """Start exactly one autonomous manager-owned cleanup operation."""

        if child.finalizer_started:
            return
        child.finalizer_started = True
        threading.Thread(
            target=self._finalize_child,
            args=(child,),
            daemon=True,
            name="nyx-worker-finalizer",
        ).start()

    def _finalize_child(self, child: _Child) -> None:
        """Terminate, reap, drain and close a child without request coupling."""

        try:
            self._terminate(child)
            # No request deadline is used here.  close() supplies the separate
            # manager shutdown deadline while this operation retains ownership.
            child.process.wait()
            for reader in child.reader_threads:
                reader.join()
            for stream in (child.process.stdout, child.process.stderr):
                if stream is not None:
                    stream.close()
        finally:
            with self._lock:
                if child.process.poll() is not None and child.readers_complete.is_set():
                    if child in self._children:
                        self._children.remove(child)
                    child.finalizer_done.set()
                child.progress.set()

    def _terminal_error(self, child: _Child) -> WorkerError | None:
        with self._lock:
            reason = child.terminal_reason
        if reason in {None, "success"}:
            return None
        return WorkerError(reason)

    def _collect(self, child: _Child, timeout: float) -> bytes:
        """Collect both pipes under one absolute request deadline."""

        deadline = time.monotonic() + timeout
        while True:
            error = self._terminal_error(child)
            if error is not None:
                raise error
            wait_for_process = False
            with self._lock:
                if child.readers_complete.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._select_terminal_locked(child, "producer_timeout")
                        raise WorkerError("producer_timeout")
                    wait_for_process = True

                if not wait_for_process:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._select_terminal_locked(child, "producer_timeout")
                        raise WorkerError("producer_timeout")
            if wait_for_process:
                try:
                    child.process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    with self._lock:
                        if child.terminal_reason is None:
                            self._select_terminal_locked(child, "producer_timeout")
                        reason = child.terminal_reason
                    if reason not in {None, "success"}:
                        raise WorkerError(reason)
                    continue
                with self._lock:
                    if child.terminal_reason is not None:
                        reason = child.terminal_reason
                        if reason != "success":
                            raise WorkerError(reason)
                        continue
                    if self._closing or child.cancelled:
                        self._select_terminal_locked(child, "producer_cancelled")
                        raise WorkerError("producer_cancelled")
                    if child.process.returncode == 0:
                        # Keep the final cancellation check and result capture
                        # under the manager lock so close cannot publish bytes
                        # after it has won the ordering race.
                        child.terminal_reason = "success"
                        self._start_finalizer_locked(child)
                        return bytes(child.captured["stdout"])
                    code = _EXIT_STATUS_CATEGORIES.get(
                        child.process.returncode, "producer_failed"
                    )
                    self._select_terminal_locked(child, code)
                    raise WorkerError(code)
            child.progress.wait(timeout=remaining)
            child.progress.clear()

    def close(self, deadline: float) -> bool:
        """Close admission and reap direct children before one shared deadline."""

        self.close_admission()
        while True:
            remaining_time = deadline - time.monotonic()
            with self._lock:
                if not self._children and not self._reservations:
                    return True
                children = tuple(self._children)
                for child in children:
                    child.cancelled = True
                    self._select_terminal_locked(child, "producer_cancelled")
            if remaining_time <= 0:
                return False
            for child in children:
                child.finalizer_done.wait(timeout=max(0.0, deadline - time.monotonic()))
                if time.monotonic() >= deadline:
                    break

    def close_admission(self) -> None:
        """Prevent new requests and cancel currently registered children."""

        with self._lock:
            self._closing = True
            for child in tuple(self._children):
                child.cancelled = True
                self._select_terminal_locked(child, "producer_cancelled")


def add_worker_arguments(parser: Any) -> None:
    """Register the arguments shared by both worker entries and both kinds.

    An absent ``--kind`` means the catalog, which also accepts neither of the
    checkouts kind's arguments; the semantic combination is checked before any
    inherited object is received or any configuration is loaded.
    """

    parser.add_argument("--recovery-fd", type=int)
    parser.add_argument("--recovery-path", type=Path)
    parser.add_argument("--parent-liveness-fd", type=int)
    parser.add_argument("--kind", choices=WORKER_KINDS, default=CATALOG_KIND)
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--deadline-ms", type=int)


def worker_entrypoint(argv: list[str] | None = None) -> int:
    """Run one worker kind from either the module or packaged entry."""

    import argparse

    parser = argparse.ArgumentParser(prog="nyx.worker")
    add_worker_arguments(parser)
    options = parser.parse_args(argv)
    return _worker_main(
        recovery_fd=options.recovery_fd,
        recovery_path=options.recovery_path,
        parent_liveness_fd=options.parent_liveness_fd,
        kind=options.kind,
        network=options.network,
        deadline_ms=options.deadline_ms,
    )


if __name__ == "__main__":
    raise SystemExit(worker_entrypoint())


__all__ = [
    "CatalogWorkerManager",
    "WorkerError",
    "add_worker_arguments",
    "bundled_worker_command",
    "default_worker_command",
    "source_child_environment",
    "worker_entrypoint",
]
