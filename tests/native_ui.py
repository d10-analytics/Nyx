"""Native window observation and OS-level automation for the delivered desktop tests.

One owner answers "which windows does process X show" (``visible_top_level_windows``)
for the readiness wait, the Windows console-window observation and the input
driver.  The driver offers a Windows and a macOS backend with the same surface:
an authority check, window bring-forward, OS mouse and keyboard input, Qt widget
lookup through the platform accessibility tree, and the native clipboard.  A
minimal page-level DevTools client may locate and read web content, but it can
never inject input: every click and keystroke is OS-level.

Only the standard library is imported at module import so Linux collection of
the tests that import this module keeps working.  Platform libraries are loaded
lazily inside the backends.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import hashlib
import http.client
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, NamedTuple

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DriverError(Exception):
    """A driver or precondition failure; never an expected failure."""


class AuthorityError(DriverError):
    """The host did not grant the driver accessibility trust or input delivery."""


class CdpError(DriverError):
    """The page-level DevTools connection failed or reported an error."""


# ---------------------------------------------------------------------------
# Window enumeration (shared owner)
# ---------------------------------------------------------------------------


class WindowInfo(NamedTuple):
    """One visible top-level window."""

    owner: int
    title: str
    window_class: str
    handle: int


_CONSOLE_WINDOW_CLASSES = frozenset({"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"})
# Hidden window a console host creates for a pseudo console; it is recorded with
# its owner relations as evidence but is never a visible console window itself.
_PSEUDO_CONSOLE_CLASS = "PseudoConsoleWindow"
LARGE_WINDOW_SIZE = (1400, 1800)
_CONSOLE_HOST_IMAGES = frozenset({"conhost.exe", "openconsole.exe"})


def is_console_window_class(window_class: str) -> bool:
    return window_class in _CONSOLE_WINDOW_CLASSES


def _windows_api() -> Any:
    """Return typed user32/kernel32 entry points for window queries."""

    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    return user32, kernel32, callback_type, wintypes


def _windows_describe(user32: Any, wintypes: Any, window: Any) -> WindowInfo:
    owner = wintypes.DWORD()
    user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
    title = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(window, title, len(title))
    window_class = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(window, window_class, len(window_class))
    return WindowInfo(owner.value, title.value, window_class.value, int(window or 0))


def _windows_top_level_windows(visible_only: bool) -> list[WindowInfo]:
    user32, _, callback_type, wintypes = _windows_api()
    found: list[WindowInfo] = []

    def visit(window, _parameter):
        if not visible_only or user32.IsWindowVisible(window):
            found.append(_windows_describe(user32, wintypes, window))
        return True

    if not user32.EnumWindows(callback_type(visit), 0):
        raise OSError(ctypes.get_last_error(), "EnumWindows failed")
    return found


def _windows_visible_top_level_windows() -> list[WindowInfo]:
    return _windows_top_level_windows(visible_only=True)


def all_top_level_windows() -> list[WindowInfo]:
    """Return every top-level window, hidden ones included (Windows only).

    A console's pseudo-console window is never visible, so the visible-window
    enumeration cannot see it.
    """

    if os.name != "nt":
        raise DriverError("hidden top-level windows are enumerated only on Windows")
    return _windows_top_level_windows(visible_only=False)


def _macos_visible_top_level_windows() -> list[WindowInfo]:
    graphics = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    foundation = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    graphics.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    graphics.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
    foundation.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    foundation.CFArrayGetCount.restype = ctypes.c_long
    foundation.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    foundation.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    foundation.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    foundation.CFDictionaryGetValue.restype = ctypes.c_void_p
    foundation.CFNumberGetValue.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int32),
    ]
    foundation.CFNumberGetValue.restype = ctypes.c_bool
    foundation.CFStringGetCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32,
    ]
    foundation.CFStringGetCString.restype = ctypes.c_bool
    foundation.CFRelease.argtypes = [ctypes.c_void_p]
    foundation.CFRelease.restype = None
    on_screen_only = 1 << 0
    exclude_desktop_elements = 1 << 4
    number_sint32 = 3
    utf8 = 0x08000100
    owner_pid_key = ctypes.c_void_p.in_dll(graphics, "kCGWindowOwnerPID").value
    owner_name_key = ctypes.c_void_p.in_dll(graphics, "kCGWindowOwnerName").value
    layer_key = ctypes.c_void_p.in_dll(graphics, "kCGWindowLayer").value

    def integer(info, key) -> int | None:
        value = foundation.CFDictionaryGetValue(info, key)
        number = ctypes.c_int32()
        if not value or not foundation.CFNumberGetValue(value, number_sint32, ctypes.byref(number)):
            return None
        return number.value

    def text(info, key) -> str:
        value = foundation.CFDictionaryGetValue(info, key)
        buffer = ctypes.create_string_buffer(512)
        if not value or not foundation.CFStringGetCString(value, buffer, len(buffer), utf8):
            return ""
        return buffer.value.decode("utf-8", errors="replace")

    windows = graphics.CGWindowListCopyWindowInfo(
        on_screen_only | exclude_desktop_elements, 0
    )
    if not windows:
        raise OSError("CGWindowListCopyWindowInfo returned no window list")
    found: list[WindowInfo] = []
    try:
        for index in range(foundation.CFArrayGetCount(windows)):
            info = foundation.CFArrayGetValueAtIndex(windows, index)
            owner = integer(info, owner_pid_key)
            # Layer 0 is the normal application window level; menu bar, dock
            # and overlay surfaces sit on other layers.
            if owner is None or integer(info, layer_key) != 0:
                continue
            # CoreGraphics carries no window class; the label is the owner name.
            found.append(WindowInfo(owner, text(info, owner_name_key), "", 0))
    finally:
        foundation.CFRelease(windows)
    return found


def visible_top_level_windows() -> list[WindowInfo]:
    """Return owner process id, label, window class and handle of each visible window."""

    if os.name == "nt":
        return _windows_visible_top_level_windows()
    if sys.platform == "darwin":
        return _macos_visible_top_level_windows()
    raise AssertionError("window readiness is observable only on Windows and macOS")


def gui_window(pid: int, title: str = "Nyx") -> WindowInfo:
    """Return the visible top-level window of ``pid`` carrying ``title``."""

    matches = [
        window
        for window in visible_top_level_windows()
        if window.owner == pid and window.title == title
    ]
    if not matches:
        raise DriverError(f"process {pid} shows no visible window titled {title!r}")
    return matches[0]


def process_image_path(pid: int) -> str:
    """Return the executable image path the operating system reports for ``pid``."""

    if os.name == "nt":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            raise DriverError(f"OpenProcess({pid}) failed: {ctypes.get_last_error()}")
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                raise DriverError(
                    f"QueryFullProcessImageNameW({pid}) failed: {ctypes.get_last_error()}"
                )
            return buffer.value
        finally:
            kernel32.CloseHandle(handle)
    if sys.platform == "darwin":
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        libproc.proc_pidpath.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(4096)
        length = libproc.proc_pidpath(pid, buffer, len(buffer))
        if length <= 0:
            raise DriverError(f"proc_pidpath({pid}) failed: {ctypes.get_errno()}")
        return buffer.value.decode("utf-8", errors="replace")
    raise DriverError("process image paths are observable only on Windows and macOS")


# ---------------------------------------------------------------------------
# Windows process snapshots, job object and window-show sampling
# ---------------------------------------------------------------------------


def windows_process_table() -> dict[int, tuple[int, str]]:
    """Return ``{pid: (parent pid, image name)}`` from a Toolhelp snapshot."""

    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = wintypes.BOOL
    th32cs_snapprocess = 0x2
    snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapprocess, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    table: dict[int, tuple[int, str]] = {}
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return table


class JobEvent(NamedTuple):
    at: float
    message: int
    pid: int


JOB_OBJECT_MSG_NEW_PROCESS = 6
JOB_OBJECT_MSG_EXIT_PROCESS = 7


class JobObserver:
    """Place a launched process in a job object and report its descendants.

    ``attach`` is called immediately after launch.  The job's completion port
    reports each descendant process ID with a timestamp from ``time.monotonic``,
    so a short-lived helper is attributed even if it exits before any poll.
    """

    def __init__(self) -> None:
        self.events: list[JobEvent] = []
        self.attached_pid: int | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._job: Any = None
        self._port: Any = None
        self._api: Any = None

    def attach(self, process: subprocess.Popen) -> None:
        if os.name != "nt":
            raise DriverError("job objects exist only on Windows")
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateIoCompletionPort.argtypes = [
            wintypes.HANDLE, wintypes.HANDLE, ctypes.c_size_t, wintypes.DWORD,
        ]
        kernel32.CreateIoCompletionPort.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.GetQueuedCompletionStatus.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.DWORD,
        ]
        kernel32.GetQueuedCompletionStatus.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        class AssociateCompletionPort(ctypes.Structure):
            _fields_ = [("CompletionKey", ctypes.c_void_p), ("CompletionPort", wintypes.HANDLE)]

        invalid_handle = ctypes.c_void_p(-1).value
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise DriverError(f"CreateJobObjectW failed: {ctypes.get_last_error()}")
        port = kernel32.CreateIoCompletionPort(invalid_handle, None, 0, 1)
        if not port:
            raise DriverError(f"CreateIoCompletionPort failed: {ctypes.get_last_error()}")
        association = AssociateCompletionPort(1, port)
        job_object_associate_completion_port_information = 7
        if not kernel32.SetInformationJobObject(
            job,
            job_object_associate_completion_port_information,
            ctypes.byref(association),
            ctypes.sizeof(association),
        ):
            raise DriverError(f"SetInformationJobObject failed: {ctypes.get_last_error()}")
        process_terminate = 0x0001
        process_set_quota = 0x0100
        handle = kernel32.OpenProcess(process_set_quota | process_terminate, False, process.pid)
        if not handle:
            raise DriverError(f"OpenProcess({process.pid}) failed: {ctypes.get_last_error()}")
        try:
            if not kernel32.AssignProcessToJobObject(job, handle):
                raise DriverError(
                    f"AssignProcessToJobObject({process.pid}) failed: {ctypes.get_last_error()}"
                )
        finally:
            kernel32.CloseHandle(handle)
        self._job = job
        self._port = port
        self._api = (kernel32, wintypes)
        self.attached_pid = process.pid
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        kernel32, wintypes = self._api
        while not self._stop.is_set():
            message = wintypes.DWORD()
            key = ctypes.c_size_t()
            overlapped = ctypes.c_void_p()
            kernel32.GetQueuedCompletionStatus(
                self._port,
                ctypes.byref(message),
                ctypes.byref(key),
                ctypes.byref(overlapped),
                100,
            )
            if overlapped.value is None:
                continue  # timeout or a port error; job messages carry the pid here
            with self._lock:
                self.events.append(JobEvent(time.monotonic(), message.value, int(overlapped.value)))

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._api is not None:
            kernel32, _ = self._api
            for handle in (self._port, self._job):
                if handle:
                    kernel32.CloseHandle(handle)
            self._api = None

    def __enter__(self) -> "JobObserver":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def descendant_pids(self) -> set[int]:
        with self._lock:
            return {e.pid for e in self.events if e.message == JOB_OBJECT_MSG_NEW_PROCESS}

    def started_between(self, begin: float, end: float) -> set[int]:
        """Descendant process IDs the job reported as started inside ``[begin, end]``."""

        with self._lock:
            return {
                e.pid
                for e in self.events
                if e.message == JOB_OBJECT_MSG_NEW_PROCESS and begin <= e.at <= end
            }


@dataclass(frozen=True)
class WindowSample:
    """A window seen being shown, with the owner relation resolved at sample time."""

    at: float
    source: str
    handle: int
    window_class: str
    title: str
    owner_pid: int
    owner_image: str
    owner_parent: int | None
    # Process table captured when the window was seen: ``{pid: (parent, image)}``.
    process_table: dict[int, tuple[int, str]] | None = None


def attribute_window(sample: WindowSample, family: set[int]) -> str | None:
    """Name the structural relation tying a sampled window to a process family.

    Console windows belong to the console host rather than to the process that
    spawned the console, so the window owner, the owner's parent and, for a
    console host owner, the host's attached client processes are compared with
    the family.
    """

    if sample.owner_pid in family:
        return "owner-in-family"
    if sample.owner_parent is not None and sample.owner_parent in family:
        return "owner-parent-in-family"
    if sample.owner_image.lower() in _CONSOLE_HOST_IMAGES and sample.process_table:
        for pid, (parent, _) in sample.process_table.items():
            if parent == sample.owner_pid and pid in family:
                return "host-has-family-client"
    return None


def is_console_evidence_class(window_class: str) -> bool:
    """Whether a window of this class is a console window or a console's pseudo window."""

    return is_console_window_class(window_class) or window_class == _PSEUDO_CONSOLE_CLASS


def attribute_console_windows(
    samples: list[WindowSample],
    family: set[int],
    allowed: set[str] | None = None,
) -> list[tuple[WindowSample, str]]:
    """Return console windows tied to ``family`` by a concrete process relation.

    Only ownership relations count: the window's owner is in the family, the
    owner's parent is, or the owner is a console host whose attached client is.
    A console a process creates for itself exhibits a pseudo-console window
    owned by that process.  Appearing at about the same time as a family
    process never attributes a window: a hosted terminal such as the runner's
    own can open windows while the family runs.  ``allowed`` restricts the
    relations, so the application is judged only by relations a positive
    control has shown the sampler can observe.
    """

    attributed: list[tuple[WindowSample, str]] = []
    for sample in samples:
        if not is_console_evidence_class(sample.window_class):
            continue
        relation = attribute_window(sample, family)
        if relation is not None and (allowed is None or relation in allowed):
            attributed.append((sample, relation))
    return attributed


_ATTACH_CONSOLE_SCRIPT = (
    "import ctypes, json, sys\n"
    "k = ctypes.WinDLL('kernel32', use_last_error=True)\n"
    "u = ctypes.WinDLL('user32', use_last_error=True)\n"
    "k.GetConsoleWindow.restype = ctypes.c_void_p\n"
    "u.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]\n"
    "u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]\n"
    "k.FreeConsole()\n"
    "attached = bool(k.AttachConsole(int(sys.argv[1])))\n"
    "error = ctypes.get_last_error()\n"
    "handle = (k.GetConsoleWindow() or 0) if attached else 0\n"
    "info = {'attached': attached, 'error': error, 'hwnd': handle}\n"
    "if handle:\n"
    "    name = ctypes.create_unicode_buffer(256)\n"
    "    u.GetClassNameW(handle, name, 256)\n"
    "    owner = ctypes.c_ulong()\n"
    "    u.GetWindowThreadProcessId(handle, ctypes.byref(owner))\n"
    "    info['class'] = name.value\n"
    "    info['owner'] = owner.value\n"
    "k.FreeConsole()\n"
    "print(json.dumps(info))\n"
)


def console_window_of(pid: int) -> dict[str, Any]:
    """Attach to the console of ``pid`` and report its console window, or why not.

    A short-lived helper process does the attaching, so this process keeps its
    own console.  The window handle returned by ``GetConsoleWindow`` is the
    strongest evidence of which window belongs to the process.
    """

    if os.name != "nt":
        raise DriverError("console attachment exists only on Windows")
    completed = subprocess.run(
        [sys.executable, "-c", _ATTACH_CONSOLE_SCRIPT, str(pid)],
        capture_output=True, text=True, timeout=30, check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if completed.returncode != 0:
        return {"attached": False, "error": f"helper exit {completed.returncode}: "
                f"{completed.stderr[-300:]!r}", "hwnd": 0}
    return _json_or_raise(completed.stdout.strip(), "the console attach helper")


def _sample_from_info(
    window: WindowInfo, table: dict[int, tuple[int, str]], source: str
) -> WindowSample:
    parent, image = table.get(window.owner, (None, ""))
    return WindowSample(
        time.monotonic(), source, window.handle, window.window_class, window.title,
        window.owner, image, parent, table,
    )


def observe_console_relations(
    pid: int,
    *,
    enumerate_windows: Callable[[], list[WindowInfo]],
    process_table: Callable[[], dict[int, tuple[int, str]]],
    attach: Callable[[int], dict[str, Any]],
    duration: float = 5.0,
    interval: float = 0.1,
    attach_interval: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Look for a console window of ``pid`` by polling, independent of show events.

    Each pass enumerates every top-level window, hidden ones included, and ties
    console and pseudo-console windows to ``pid`` through concrete process
    relations.  Until a window is found, the console of ``pid`` is also
    attached to at intervals and its window handle is looked up the same way;
    one attach is always made for the evidence record.  The relations found are
    the ones an application's windows may then be judged by.
    """

    found: dict[int, tuple[WindowSample, str, str]] = {}
    unattributed: list[WindowSample] = []
    attempts: list[dict[str, Any]] = []
    deadline = clock() + duration
    next_attach = clock()
    while True:
        table = process_table()
        windows = enumerate_windows()
        for window in windows:
            if is_console_evidence_class(window.window_class):
                sample = _sample_from_info(window, table, "poll-all")
                relation = attribute_window(sample, {pid})
                if relation is not None:
                    found[window.handle] = (sample, relation, "poll-all")
        if not found and clock() >= next_attach:
            info = attach(pid)
            attempts.append(info)
            next_attach = clock() + attach_interval
            if info.get("hwnd"):
                window = WindowInfo(
                    int(info.get("owner", 0)), "", str(info.get("class", "")), int(info["hwnd"])
                )
                sample = _sample_from_info(window, table, "attach")
                relation = attribute_window(sample, {pid})
                if relation is not None and is_console_evidence_class(window.window_class):
                    found[window.handle] = (sample, relation, "attach")
                else:
                    unattributed.append(sample)
        if found or clock() >= deadline:
            break
        sleep(interval)
    if not attempts:
        attempts.append(attach(pid))  # evidence only; the windows were already found
    return {
        "windows": list(found.values()),
        "relations": {relation for _, relation, _ in found.values()},
        "attach_attempts": attempts,
        "unattributed_attached": unattributed,
    }


class WindowShowSampler:
    """Sample every top-level window shown for the whole run (Windows only).

    A WinEvent hook records create and show events as they happen, and a fast
    enumeration pass backs it up, because a console window can live for only a
    fraction of the readiness poll's interval.
    """

    _POLL_INTERVAL = 0.015

    def __init__(self) -> None:
        self.samples: list[WindowSample] = []
        self.errors: list[str] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._hook_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._hook_thread_id: int | None = None
        self._ready = threading.Event()
        self._seen: set[tuple[int, int]] = set()
        self._hook_failure: str | None = None
        # First time each handle was seen shown, and the handles that existed
        # when ``mark_baseline`` ran.
        self.shown_at: dict[int, float] = {}
        self.baseline: frozenset[int] = frozenset()

    def start(self) -> None:
        if os.name != "nt":
            raise DriverError("window-show sampling exists only on Windows")
        self._hook_thread = threading.Thread(target=self._run_hook, daemon=True)
        self._hook_thread.start()
        if not self._ready.wait(timeout=10):
            raise DriverError("the window-show hook thread never became ready")
        if self._hook_failure is not None:
            raise DriverError(self._hook_failure)
        self._poll_thread = threading.Thread(target=self._run_poll, daemon=True)
        self._poll_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._hook_thread_id is not None:
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.PostThreadMessageW.argtypes = [
                wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
            ]
            user32.PostThreadMessageW.restype = wintypes.BOOL
            wm_quit = 0x0012
            user32.PostThreadMessageW(self._hook_thread_id, wm_quit, 0, 0)
        for thread in (self._hook_thread, self._poll_thread):
            if thread is not None:
                thread.join(timeout=5)

    def __enter__(self) -> "WindowShowSampler":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def mark_baseline(self) -> None:
        """Record every window seen so far as pre-existing, not caused by the run."""

        with self._lock:
            self.baseline = frozenset(sample.handle for sample in self.samples)

    def snapshot(self) -> list[WindowSample]:
        with self._lock:
            return list(self.samples)

    def _record(
        self, user32: Any, wintypes: Any, window: Any, source: str, shown: bool
    ) -> None:
        if not window or user32.GetAncestor(window, 2) != window:  # GA_ROOT
            return
        info = _windows_describe(user32, wintypes, window)
        key = (info.handle, info.owner)
        now = time.monotonic()
        with self._lock:
            if shown:
                self.shown_at.setdefault(info.handle, now)
            if key in self._seen:
                return
            self._seen.add(key)
        image = ""
        parent: int | None = None
        table: dict[int, tuple[int, str]] | None = None
        if is_console_window_class(info.window_class) or info.window_class == _PSEUDO_CONSOLE_CLASS:
            # Resolve the relations now: a console host can be gone before the
            # run ends.
            table = windows_process_table()
            if info.owner in table:
                parent, image = table[info.owner]
        sample = WindowSample(
            now, source, info.handle, info.window_class, info.title,
            info.owner, image, parent, table,
        )
        with self._lock:
            self.samples.append(sample)

    def _run_hook(self) -> None:
        try:
            user32, kernel32, _, wintypes = _windows_api()
            kernel32.GetCurrentThreadId.argtypes = []
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            callback_type = ctypes.WINFUNCTYPE(
                None,
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.HWND,
                wintypes.LONG,
                wintypes.LONG,
                wintypes.DWORD,
                wintypes.DWORD,
            )
            user32.SetWinEventHook.argtypes = [
                wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, callback_type,
                wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            ]
            user32.SetWinEventHook.restype = wintypes.HANDLE
            user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
            user32.UnhookWinEvent.restype = wintypes.BOOL
            user32.GetMessageW.argtypes = [
                ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
            ]
            user32.GetMessageW.restype = wintypes.BOOL
            user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user32.DispatchMessageW.restype = ctypes.c_ssize_t
            event_object_create = 0x8000
            event_object_show = 0x8002
            objid_window = 0
            childid_self = 0
            winevent_outofcontext = 0

            def on_event(_hook, event, window, id_object, id_child, _thread, _time):
                if id_object != objid_window or id_child != childid_self:
                    return
                if event not in (event_object_create, event_object_show):
                    return
                try:
                    self._record(
                        user32, wintypes, window, "hook", event == event_object_show
                    )
                except Exception as error:  # pragma: no cover - hook diagnostics
                    self.errors.append(repr(error))

            procedure = callback_type(on_event)
            self._hook_thread_id = kernel32.GetCurrentThreadId()
            # Pump once so the thread owns a message queue before WM_QUIT arrives.
            message = wintypes.MSG()
            hook = user32.SetWinEventHook(
                event_object_create, event_object_show, None, procedure, 0, 0,
                winevent_outofcontext,
            )
            if not hook:
                self._hook_failure = f"SetWinEventHook failed: {ctypes.get_last_error()}"
                self._ready.set()
                return
            self._ready.set()
            try:
                while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
            finally:
                user32.UnhookWinEvent(hook)
        except Exception as error:  # pragma: no cover - platform failure
            self._hook_failure = f"window-show hook thread failed: {error!r}"
            self._ready.set()

    def _run_poll(self) -> None:
        user32, _, callback_type, wintypes = _windows_api()
        while not self._stop.is_set():

            def visit(window, _parameter):
                if user32.IsWindowVisible(window):
                    try:
                        self._record(user32, wintypes, window, "poll", True)
                    except Exception as error:  # pragma: no cover - diagnostics
                        self.errors.append(repr(error))
                return True

            user32.EnumWindows(callback_type(visit), 0)
            time.sleep(self._POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Geometry and shared driver surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rect:
    left: float
    top: float
    width: float
    height: float

    @property
    def center(self) -> tuple[float, float]:
        return self.left + self.width / 2, self.top + self.height / 2


@dataclass(frozen=True)
class Widget:
    """A located native widget: screen rectangle, enabled state and text value."""

    name: str
    rect: Rect
    enabled: bool
    value: str | None
    element: Any = None  # platform handle (macOS AX element) for focus queries


def _json_or_raise(text: str, what: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as error:
        raise DriverError(f"{what} returned unparsable output: {text[:500]!r}") from error


# ---------------------------------------------------------------------------
# Windows backend
# ---------------------------------------------------------------------------

_UIA_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition 'using System.Runtime.InteropServices; public static class NyxDpi { [DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); }'
[void][NyxDpi]::SetProcessDPIAware()
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$request = ConvertFrom-Json $env:NYX_UIA_REQUEST
$automation = [System.Windows.Automation.AutomationElement]
$scope = [System.Windows.Automation.TreeScope]
$root = $automation::RootElement
$owner = New-Object System.Windows.Automation.PropertyCondition($automation::ProcessIdProperty, [int]$request.pid)
$windows = $root.FindAll($scope::Children, $owner)
if ($windows.Count -eq 0) { throw "no accessible top-level window for pid $($request.pid)" }
if ($request.mode -eq 'probe') {
    $names = @(); foreach ($w in $windows) { $names += $w.Current.Name }
    ConvertTo-Json -InputObject @($names) -Compress
    exit 0
}
$types = @{
    button = [System.Windows.Automation.ControlType]::Button
    edit = [System.Windows.Automation.ControlType]::Edit
}
$typed = New-Object System.Windows.Automation.PropertyCondition($automation::ControlTypeProperty, $types[$request.role])
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$exact = @(); $native = @()
foreach ($window in $windows) {
    foreach ($element in $window.FindAll($scope::Descendants, $typed)) {
        $current = $element.Current
        $rect = $current.BoundingRectangle
        if ($current.IsOffscreen -or $rect.IsEmpty -or [double]::IsInfinity($rect.Left)) { continue }
        $value = $null
        try {
            $pattern = $element.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
            $value = $pattern.Current.Value
        } catch { }
        $entry = [pscustomobject]@{
            name = $current.Name; left = $rect.Left; top = $rect.Top
            width = $rect.Width; height = $rect.Height
            enabled = [bool]$current.IsEnabled; value = $value
        }
        if ($current.Name -eq $request.name) { $exact += $entry; continue }
        if ($request.role -eq 'edit') {
            $inside = $false
            $parent = $walker.GetParent($element)
            while ($parent -ne $null) {
                if ($parent.Current.ControlType -eq [System.Windows.Automation.ControlType]::Document) { $inside = $true; break }
                $parent = $walker.GetParent($parent)
            }
            if (-not $inside) { $native += $entry }
        }
    }
}
$result = $exact
if ($result.Count -eq 0 -and $native.Count -eq 1) { $result = $native }
ConvertTo-Json -InputObject @($result) -Compress -Depth 4
"""


class _WindowsBackend:
    """Windows UI Automation lookup with ``SendInput`` and the native clipboard."""

    name = "windows"

    def __init__(self) -> None:
        from ctypes import wintypes

        self._wintypes = wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._declare()
        self._make_dpi_aware()

    # -- setup -------------------------------------------------------------

    def _declare(self) -> None:
        wintypes = self._wintypes
        user32, kernel32 = self.user32, self.kernel32

        class MouseInput(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class KeyboardInput(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t),
            ]

        class InputUnion(ctypes.Union):
            _fields_ = [("mi", MouseInput), ("ki", KeyboardInput)]

        class Input(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", InputUnion)]

        self._Input = Input
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        user32.SendInput.restype = wintypes.UINT
        user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        user32.SetCursorPos.restype = wintypes.BOOL
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        user32.CloseDesktop.restype = wintypes.BOOL
        user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
        user32.keybd_event.restype = None
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.argtypes = []
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL

    def _make_dpi_aware(self) -> None:
        """Use physical pixels everywhere so window, accessibility and input agree."""

        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 is the handle value -4.
        try:
            self.user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            self.user32.SetProcessDpiAwarenessContext.restype = self._wintypes.BOOL
            if self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
        except AttributeError:
            pass
        self.user32.SetProcessDPIAware()

    # -- authority ---------------------------------------------------------

    def check_authority(self, pid: int | None = None) -> str:
        """Verify an interactive desktop, delivered input and an accessibility tree."""

        wintypes = self._wintypes
        generic_read = 0x80000000
        desktop = self.user32.OpenInputDesktop(0, False, generic_read)
        if not desktop:
            raise AuthorityError(
                f"the input desktop is not accessible (error {ctypes.get_last_error()})"
            )
        self.user32.CloseDesktop(desktop)
        target = (60, 60)
        self.move(*target)
        point = wintypes.POINT()
        if not self.user32.GetCursorPos(ctypes.byref(point)):
            raise AuthorityError("GetCursorPos failed")
        if abs(point.x - target[0]) > 2 or abs(point.y - target[1]) > 2:
            raise AuthorityError(
                f"input was not delivered: cursor at ({point.x},{point.y}), expected {target}"
            )
        if pid is not None:
            try:
                self._run_uia({"mode": "probe", "pid": pid})
            except DriverError as error:
                raise AuthorityError(f"UI Automation is unavailable: {error}") from error
        return "interactive desktop, SendInput delivery and UI Automation"

    # -- windows -----------------------------------------------------------

    def window_frame(self, pid: int) -> Rect:
        window = gui_window(pid)
        rect = self._wintypes.RECT()
        if not self.user32.GetWindowRect(window.handle, ctypes.byref(rect)):
            raise DriverError(f"GetWindowRect failed: {ctypes.get_last_error()}")
        return Rect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)

    def enlarge_window(self, pid: int) -> None:
        """Place the window at the origin with a fixed generous size, past the screen if need be."""

        handle = gui_window(pid).handle
        sw_restore, swp_nozorder, swp_showwindow = 9, 0x0004, 0x0040
        self.user32.ShowWindow(handle, sw_restore)  # a maximized window ignores a resize
        width, height = LARGE_WINDOW_SIZE
        if not self.user32.SetWindowPos(
            handle, None, 0, 0, width, height, swp_nozorder | swp_showwindow
        ):
            raise DriverError(f"SetWindowPos failed: {ctypes.get_last_error()}")

    def move_window(self, pid: int, left: float, top: float) -> None:
        """Move the window's top-left corner to ``(left, top)`` without resizing it."""

        swp_nosize, swp_nozorder = 0x0001, 0x0004
        if not self.user32.SetWindowPos(
            gui_window(pid).handle, None, int(round(left)), int(round(top)), 0, 0,
            swp_nosize | swp_nozorder,
        ):
            raise DriverError(f"SetWindowPos failed: {ctypes.get_last_error()}")

    def screen_bounds(self) -> Rect:
        """Return the virtual desktop rectangle in physical pixels."""

        sm_xvirtualscreen, sm_yvirtualscreen, sm_cxvirtualscreen, sm_cyvirtualscreen = 76, 77, 78, 79
        metric = self.user32.GetSystemMetrics
        return Rect(
            metric(sm_xvirtualscreen), metric(sm_yvirtualscreen),
            metric(sm_cxvirtualscreen), metric(sm_cyvirtualscreen),
        )

    def zoom_out_shortcut(self) -> None:
        """Press Ctrl+minus."""

        vk_control, vk_oem_minus, keyup = 0x11, 0xBD, 0x2
        self._send(
            self._key(vk_control, 0, 0),
            self._key(vk_oem_minus, 0, 0),
            self._key(vk_oem_minus, 0, keyup),
            self._key(vk_control, 0, keyup),
        )
        time.sleep(0.2)

    def zoom_out_wheel(self, x: float, y: float) -> None:
        """Turn the wheel down one notch with Ctrl held, over the point ``(x, y)``."""

        vk_control, keyup, wheel = 0x11, 0x2, 0x0800
        self.move(x, y)
        notch = self._mouse(wheel)
        notch.mi.mouseData = (-120) & 0xFFFFFFFF
        self._send(self._key(vk_control, 0, 0), notch, self._key(vk_control, 0, keyup))
        time.sleep(0.2)

    def bring_forward(self, pid: int) -> None:
        window = gui_window(pid)
        handle = window.handle
        sw_restore = 9
        if self.user32.IsIconic(handle):
            self.user32.ShowWindow(handle, sw_restore)
        # A synthetic Alt tap lifts the foreground lock that otherwise makes
        # SetForegroundWindow fail for a background process.
        vk_menu = 0x12
        keyeventf_keyup = 0x2
        self.user32.keybd_event(vk_menu, 0, 0, 0)
        self.user32.keybd_event(vk_menu, 0, keyeventf_keyup, 0)
        self.user32.SetForegroundWindow(handle)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.user32.GetForegroundWindow() == handle:
                return
            time.sleep(0.1)
            self.user32.SetForegroundWindow(handle)
        raise DriverError(f"window {handle:#x} of pid {pid} did not become the foreground window")

    # -- input -------------------------------------------------------------

    def _send(self, *inputs: Any) -> None:
        array = (self._Input * len(inputs))(*inputs)
        sent = self.user32.SendInput(len(inputs), array, ctypes.sizeof(self._Input))
        if sent != len(inputs):
            raise DriverError(
                f"SendInput delivered {sent} of {len(inputs)} events: {ctypes.get_last_error()}"
            )

    def _mouse(self, flags: int) -> Any:
        event = self._Input()
        event.type = 0  # INPUT_MOUSE
        event.mi.dwFlags = flags
        return event

    def _key(self, virtual_key: int, scan: int, flags: int) -> Any:
        event = self._Input()
        event.type = 1  # INPUT_KEYBOARD
        event.ki.wVk = virtual_key
        event.ki.wScan = scan
        event.ki.dwFlags = flags
        return event

    def move(self, x: float, y: float) -> None:
        if not self.user32.SetCursorPos(int(round(x)), int(round(y))):
            raise DriverError(f"SetCursorPos failed: {ctypes.get_last_error()}")
        time.sleep(0.1)

    def click(self, x: float, y: float, button: str = "left") -> None:
        self.move(x, y)
        down, up = (0x0002, 0x0004) if button == "left" else (0x0008, 0x0010)
        self._send(self._mouse(down))
        time.sleep(0.05)
        self._send(self._mouse(up))
        time.sleep(0.15)

    def select_all(self) -> None:
        vk_control, vk_a, keyup = 0x11, 0x41, 0x2
        self._send(
            self._key(vk_control, 0, 0),
            self._key(vk_a, 0, 0),
            self._key(vk_a, 0, keyup),
            self._key(vk_control, 0, keyup),
        )
        time.sleep(0.1)

    def type_text(self, text: str) -> None:
        keyeventf_unicode, keyup = 0x4, 0x2
        encoded = text.encode("utf-16-le")
        for index in range(0, len(encoded), 2):
            unit = int.from_bytes(encoded[index:index + 2], "little")
            self._send(
                self._key(0, unit, keyeventf_unicode),
                self._key(0, unit, keyeventf_unicode | keyup),
            )
            time.sleep(0.01)
        time.sleep(0.1)

    def replace_text(self, pid: int, name: str, text: str) -> None:
        """Click the named edit field, select its content and type ``text`` over it."""

        field = self.find_widget(pid, "edit", name)
        self.click(*field.rect.center)
        self.select_all()
        self.type_text(text)

    # -- accessibility -----------------------------------------------------

    def _run_uia(self, request: dict[str, Any]) -> Any:
        shell = shutil.which("powershell.exe") or "powershell.exe"
        encoded = base64.b64encode(_UIA_SCRIPT.encode("utf-16-le")).decode("ascii")
        environment = dict(os.environ)
        environment["NYX_UIA_REQUEST"] = json.dumps(request)
        completed = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", encoded],
            capture_output=True, text=True, env=environment, timeout=120, check=False,
        )
        if completed.returncode != 0:
            raise DriverError(
                f"UI Automation query exited {completed.returncode}: "
                f"stdout={completed.stdout[-500:]!r} stderr={completed.stderr[-1500:]!r}"
            )
        return _json_or_raise(completed.stdout.strip(), "UI Automation query")

    def find_widget(self, pid: int, role: str, name: str) -> Widget:
        matches = self._run_uia({"mode": "find", "pid": pid, "role": role, "name": name})
        if not matches:
            raise DriverError(f"no visible {role} named {name!r} in the window of pid {pid}")
        if len(matches) != 1:
            raise DriverError(
                f"{len(matches)} visible {role} widgets match {name!r}: "
                f"{[m['name'] for m in matches]!r}"
            )
        match = matches[0]
        return Widget(
            match["name"],
            Rect(match["left"], match["top"], match["width"], match["height"]),
            bool(match["enabled"]),
            match.get("value"),
        )

    # -- clipboard ---------------------------------------------------------

    @contextlib.contextmanager
    def _clipboard(self):
        deadline = time.monotonic() + 5
        while not self.user32.OpenClipboard(None):
            if time.monotonic() >= deadline:
                raise DriverError(f"OpenClipboard failed: {ctypes.get_last_error()}")
            time.sleep(0.05)
        try:
            yield
        finally:
            self.user32.CloseClipboard()

    def set_clipboard(self, text: str) -> None:
        cf_unicodetext, gmem_moveable = 13, 0x0002
        data = (text + "\0").encode("utf-16-le")
        with self._clipboard():
            if not self.user32.EmptyClipboard():
                raise DriverError(f"EmptyClipboard failed: {ctypes.get_last_error()}")
            memory = self.kernel32.GlobalAlloc(gmem_moveable, len(data))
            if not memory:
                raise DriverError(f"GlobalAlloc failed: {ctypes.get_last_error()}")
            address = self.kernel32.GlobalLock(memory)
            if not address:
                self.kernel32.GlobalFree(memory)
                raise DriverError(f"GlobalLock failed: {ctypes.get_last_error()}")
            ctypes.memmove(address, data, len(data))
            self.kernel32.GlobalUnlock(memory)
            if not self.user32.SetClipboardData(cf_unicodetext, memory):
                self.kernel32.GlobalFree(memory)
                raise DriverError(f"SetClipboardData failed: {ctypes.get_last_error()}")

    def get_clipboard(self) -> str | None:
        cf_unicodetext = 13
        with self._clipboard():
            memory = self.user32.GetClipboardData(cf_unicodetext)
            if not memory:
                return None
            address = self.kernel32.GlobalLock(memory)
            if not address:
                raise DriverError(f"GlobalLock failed: {ctypes.get_last_error()}")
            try:
                return ctypes.wstring_at(address)
            finally:
                self.kernel32.GlobalUnlock(memory)


# ---------------------------------------------------------------------------
# macOS backend
# ---------------------------------------------------------------------------


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _CGRect(ctypes.Structure):
    _fields_ = [("origin", _CGPoint), ("size", _CGSize)]


class _MacBackend:
    """Accessibility (AX) lookup, CGEvent input and the native pasteboard."""

    name = "macos"
    _UTF8 = 0x08000100

    def __init__(self) -> None:
        self.services = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        self.foundation = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        self._declare()

    def _declare(self) -> None:
        sv, cf = self.services, self.foundation
        void_p = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = void_p
        cf.CFStringGetCString.argtypes = [void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetTypeID.argtypes = []
        cf.CFStringGetTypeID.restype = ctypes.c_ulong
        cf.CFArrayGetTypeID.argtypes = []
        cf.CFArrayGetTypeID.restype = ctypes.c_ulong
        cf.CFGetTypeID.argtypes = [void_p]
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFArrayGetCount.argtypes = [void_p]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetValueAtIndex.argtypes = [void_p, ctypes.c_long]
        cf.CFArrayGetValueAtIndex.restype = void_p
        cf.CFRelease.argtypes = [void_p]
        cf.CFRelease.restype = None
        self._true = void_p.in_dll(cf, "kCFBooleanTrue")
        cf.CFBooleanGetValue.argtypes = [void_p]
        cf.CFBooleanGetValue.restype = ctypes.c_ubyte
        sv.AXIsProcessTrusted.argtypes = []
        sv.AXIsProcessTrusted.restype = ctypes.c_ubyte
        sv.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
        sv.AXUIElementCreateApplication.restype = void_p
        sv.AXUIElementCopyAttributeValue.argtypes = [void_p, void_p, ctypes.POINTER(void_p)]
        sv.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
        sv.AXValueGetValue.argtypes = [void_p, ctypes.c_int, void_p]
        sv.AXValueGetValue.restype = ctypes.c_ubyte
        sv.CGEventCreate.argtypes = [void_p]
        sv.CGEventCreate.restype = void_p
        sv.CGEventGetLocation.argtypes = [void_p]
        sv.CGEventGetLocation.restype = _CGPoint
        sv.CGEventCreateMouseEvent.argtypes = [void_p, ctypes.c_uint32, _CGPoint, ctypes.c_uint32]
        sv.CGEventCreateMouseEvent.restype = void_p
        sv.CGEventCreateKeyboardEvent.argtypes = [void_p, ctypes.c_uint16, ctypes.c_ubyte]
        sv.CGEventCreateKeyboardEvent.restype = void_p
        sv.CGEventKeyboardSetUnicodeString.argtypes = [
            void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_uint16),
        ]
        sv.CGEventKeyboardSetUnicodeString.restype = None
        sv.CGEventSetFlags.argtypes = [void_p, ctypes.c_uint64]
        sv.CGEventSetFlags.restype = None
        sv.CGEventSetIntegerValueField.argtypes = [void_p, ctypes.c_uint32, ctypes.c_int64]
        sv.CGEventSetIntegerValueField.restype = None
        sv.CGEventPost.argtypes = [ctypes.c_uint32, void_p]
        sv.CGEventPost.restype = None
        sv.CGMainDisplayID.argtypes = []
        sv.CGMainDisplayID.restype = ctypes.c_uint32
        sv.CGDisplayBounds.argtypes = [ctypes.c_uint32]
        sv.CGDisplayBounds.restype = _CGRect
        sv.CGEventSetType.argtypes = [void_p, ctypes.c_uint32]
        sv.CGEventSetType.restype = None
        sv.CGEventSetLocation.argtypes = [void_p, _CGPoint]
        sv.CGEventSetLocation.restype = None
        sv.AXValueCreate.argtypes = [ctypes.c_int, void_p]
        sv.AXValueCreate.restype = void_p
        sv.AXUIElementSetAttributeValue.argtypes = [void_p, void_p, void_p]
        sv.AXUIElementSetAttributeValue.restype = ctypes.c_int32
        self._post_to_pid = getattr(sv, "CGEventPostToPid", None)
        if self._post_to_pid is not None:
            self._post_to_pid.argtypes = [ctypes.c_int, void_p]
            self._post_to_pid.restype = None

    # -- CoreFoundation helpers -------------------------------------------

    def _cfstring(self, text: str) -> Any:
        return self.foundation.CFStringCreateWithCString(None, text.encode("utf-8"), self._UTF8)

    def _read_string(self, value: Any) -> str | None:
        if not value or self.foundation.CFGetTypeID(value) != self.foundation.CFStringGetTypeID():
            return None
        buffer = ctypes.create_string_buffer(2048)
        if not self.foundation.CFStringGetCString(value, buffer, len(buffer), self._UTF8):
            return None
        return buffer.value.decode("utf-8", errors="replace")

    def _attribute(self, element: Any, attribute: str) -> Any:
        key = self._cfstring(attribute)
        out = ctypes.c_void_p()
        try:
            error = self.services.AXUIElementCopyAttributeValue(element, key, ctypes.byref(out))
        finally:
            self.foundation.CFRelease(key)
        return out.value if error == 0 else None

    def _frame(self, element: Any) -> Rect | None:
        position = self._attribute(element, "AXPosition")
        size = self._attribute(element, "AXSize")
        if not position or not size:
            return None
        origin, extent = _CGPoint(), _CGSize()
        k_ax_value_cgpoint, k_ax_value_cgsize = 1, 2
        if not self.services.AXValueGetValue(position, k_ax_value_cgpoint, ctypes.byref(origin)):
            return None
        if not self.services.AXValueGetValue(size, k_ax_value_cgsize, ctypes.byref(extent)):
            return None
        return Rect(origin.x, origin.y, extent.width, extent.height)

    def _children(self, element: Any) -> list[Any]:
        array = self._attribute(element, "AXChildren")
        if not array or self.foundation.CFGetTypeID(array) != self.foundation.CFArrayGetTypeID():
            return []
        return [
            self.foundation.CFArrayGetValueAtIndex(array, index)
            for index in range(self.foundation.CFArrayGetCount(array))
        ]

    def _windows(self, pid: int) -> list[Any]:
        application = self.services.AXUIElementCreateApplication(pid)
        if not application:
            raise DriverError(f"AXUIElementCreateApplication({pid}) returned nothing")
        array = self._attribute(application, "AXWindows")
        if not array:
            raise DriverError(f"the accessibility tree of pid {pid} lists no windows")
        return [
            self.foundation.CFArrayGetValueAtIndex(array, index)
            for index in range(self.foundation.CFArrayGetCount(array))
        ]

    # -- authority ---------------------------------------------------------

    def check_authority(self, pid: int | None = None) -> str:
        if not self.services.AXIsProcessTrusted():
            raise AuthorityError("AXIsProcessTrusted() is false: no accessibility authority")
        preflight = getattr(self.services, "CGPreflightPostEventAccess", None)
        if preflight is not None:
            preflight.argtypes = []
            preflight.restype = ctypes.c_ubyte
            if not preflight():
                raise AuthorityError("CGPreflightPostEventAccess() is false: events cannot be posted")
        target = (60.0, 60.0)
        self.move(*target)
        point = self._cursor()
        if abs(point[0] - target[0]) > 2 or abs(point[1] - target[1]) > 2:
            raise AuthorityError(f"input was not delivered: cursor at {point}, expected {target}")
        if pid is not None:
            try:
                self._windows(pid)
            except DriverError as error:
                raise AuthorityError(f"the accessibility tree is unavailable: {error}") from error
        return "accessibility trust, CGEvent delivery and an AX window tree"

    def _cursor(self) -> tuple[float, float]:
        event = self.services.CGEventCreate(None)
        point = self.services.CGEventGetLocation(event)
        self.foundation.CFRelease(event)
        return point.x, point.y

    # -- windows -----------------------------------------------------------

    def window_frame(self, pid: int) -> Rect:
        for window in self._windows(pid):
            frame = self._frame(window)
            if frame is not None:
                return frame
        raise DriverError(f"no accessible window of pid {pid} reports a frame")

    def enlarge_window(self, pid: int) -> None:
        """Place the window at the origin with a fixed generous size, past the screen if need be."""

        width, height = LARGE_WINDOW_SIZE
        top_left = _CGPoint(0.0, 0.0)
        size = _CGSize(float(width), float(height))
        k_ax_value_cgpoint, k_ax_value_cgsize = 1, 2
        windows = self._windows(pid)
        window = next((w for w in windows if self._frame(w) is not None), None)
        if window is None:
            raise DriverError(f"no accessible window of pid {pid} reports a frame")
        # Position first so the size is not clamped against the old origin, then
        # size, then position again in case the window manager moved it.
        for attribute, kind, value in (
            ("AXPosition", k_ax_value_cgpoint, top_left),
            ("AXSize", k_ax_value_cgsize, size),
            ("AXPosition", k_ax_value_cgpoint, top_left),
        ):
            packed = self.services.AXValueCreate(kind, ctypes.byref(value))
            key = self._cfstring(attribute)
            try:
                status = self.services.AXUIElementSetAttributeValue(window, key, packed)
            finally:
                self.foundation.CFRelease(key)
                if packed:
                    self.foundation.CFRelease(packed)
            if status != 0:
                raise DriverError(f"setting {attribute} failed with AXError {status}")

    def move_window(self, pid: int, left: float, top: float) -> None:
        """Move the window's top-left corner to ``(left, top)`` without resizing it."""

        windows = self._windows(pid)
        window = next((w for w in windows if self._frame(w) is not None), None)
        if window is None:
            raise DriverError(f"no accessible window of pid {pid} reports a frame")
        packed = self.services.AXValueCreate(1, ctypes.byref(_CGPoint(left, top)))
        key = self._cfstring("AXPosition")
        try:
            status = self.services.AXUIElementSetAttributeValue(window, key, packed)
        finally:
            self.foundation.CFRelease(key)
            if packed:
                self.foundation.CFRelease(packed)
        if status != 0:
            raise DriverError(f"setting AXPosition failed with AXError {status}")

    def screen_bounds(self) -> Rect:
        """Return the main display rectangle in points."""

        bounds = self.services.CGDisplayBounds(self.services.CGMainDisplayID())
        return Rect(bounds.origin.x, bounds.origin.y, bounds.size.width, bounds.size.height)

    def zoom_out_shortcut(self) -> None:
        """Press Command+minus."""

        command_flag, key_minus = 0x100000, 27
        self._post_key(key_minus, True, command_flag)
        self._post_key(key_minus, False, command_flag)
        time.sleep(0.2)

    def zoom_out_wheel(self, x: float, y: float) -> None:
        """Scroll down one notch with Command held over ``(x, y)``.

        The event is built from a blank event and typed as a scroll wheel event,
        because ``CGEventCreateScrollWheelEvent`` is variadic and cannot be
        called portably through ``ctypes``.
        """

        self.move(x, y)
        event = self.services.CGEventCreate(None)
        if not event:
            raise DriverError("CGEventCreate returned nothing")
        k_cg_event_scroll_wheel = 22
        k_scroll_delta_axis_1, k_scroll_point_delta_axis_1 = 11, 96
        self.services.CGEventSetType(event, k_cg_event_scroll_wheel)
        self.services.CGEventSetLocation(event, _CGPoint(x, y))
        self.services.CGEventSetFlags(event, 0x100000)
        self.services.CGEventSetIntegerValueField(event, k_scroll_delta_axis_1, -2)
        self.services.CGEventSetIntegerValueField(event, k_scroll_point_delta_axis_1, -20)
        self.services.CGEventPost(0, event)
        self.foundation.CFRelease(event)
        time.sleep(0.2)

    def bring_forward(self, pid: int) -> None:
        script = (
            "ObjC.import('AppKit');"
            f"var target = $.NSRunningApplication.runningApplicationWithProcessIdentifier({pid});"
            f"if (!target) {{ throw new Error('no running application has pid {pid}'); }}"
            "target.activateWithOptions($.NSApplicationActivateIgnoringOtherApps);"
        )
        check = (
            "ObjC.import('AppKit');"
            "$.NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier;"
        )
        activate = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if activate.returncode != 0:
            raise DriverError(f"activation of pid {pid} failed: {activate.stderr!r}")
        deadline = time.monotonic() + 5
        frontmost = ""
        while time.monotonic() < deadline:
            frontmost = subprocess.run(
                ["osascript", "-l", "JavaScript", "-e", check],
                capture_output=True, text=True, timeout=30, check=False,
            ).stdout.strip()
            if frontmost == str(pid):
                return
            time.sleep(0.2)
        raise DriverError(f"pid {pid} did not become frontmost (frontmost pid {frontmost!r})")

    # -- input -------------------------------------------------------------

    def _post_mouse(self, kind: int, x: float, y: float, button: int, state: int = 0) -> None:
        event = self.services.CGEventCreateMouseEvent(None, kind, _CGPoint(x, y), button)
        if not event:
            raise DriverError("CGEventCreateMouseEvent returned nothing")
        k_cg_mouse_event_click_state = 1
        if state:
            self.services.CGEventSetIntegerValueField(event, k_cg_mouse_event_click_state, state)
        self.services.CGEventPost(0, event)  # kCGHIDEventTap
        self.foundation.CFRelease(event)

    def move(self, x: float, y: float) -> None:
        self._post_mouse(5, x, y, 0)  # kCGEventMouseMoved
        time.sleep(0.1)

    def click(self, x: float, y: float, button: str = "left") -> None:
        self.move(x, y)
        if button == "left":
            down, up, code = 1, 2, 0
        else:
            down, up, code = 3, 4, 1
        self._post_mouse(down, x, y, code, state=1)
        time.sleep(0.05)
        self._post_mouse(up, x, y, code, state=1)
        time.sleep(0.15)

    # US-layout virtual key codes, so the application sees a real key together
    # with the Unicode text rather than key code 0 ("A") for every character.
    _KEY_CODES = {
        "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
        "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
        "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28,
        "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38,
        "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47,
        " ": 49, "`": 50,
    }
    _SHIFTED = {"_": "-", "+": "=", ":": ";", "~": "`", "?": "/", "<": ",", ">": ".",
                "{": "[", "}": "]", "|": "\\", '"': "'", "!": "1", "@": "2", "#": "3",
                "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0"}

    def _key_for(self, character: str) -> tuple[int, int]:
        """Return ``(virtual key, flags)`` for a character, falling back to code 0."""

        shift = 0x20000
        if character in self._KEY_CODES:
            return self._KEY_CODES[character], 0
        if character.isupper() and character.lower() in self._KEY_CODES:
            return self._KEY_CODES[character.lower()], shift
        if character in self._SHIFTED:
            return self._KEY_CODES[self._SHIFTED[character]], shift
        return 0, 0

    def _post_key(
        self, key_code: int, down: bool, flags: int = 0, text: str | None = None,
        pid: int | None = None,
    ) -> None:
        event = self.services.CGEventCreateKeyboardEvent(None, key_code, 1 if down else 0)
        if not event:
            raise DriverError("CGEventCreateKeyboardEvent returned nothing")
        if flags:
            self.services.CGEventSetFlags(event, flags)
        if text is not None:
            raw = text.encode("utf-16-le")
            units = (ctypes.c_uint16 * (len(raw) // 2)).from_buffer_copy(raw)
            self.services.CGEventKeyboardSetUnicodeString(event, len(units), units)
        if pid is not None and self._post_to_pid is not None:
            self._post_to_pid(pid, event)
        else:
            self.services.CGEventPost(0, event)
        self.foundation.CFRelease(event)

    def select_all(self, pid: int | None = None) -> None:
        command_flag, key_a = 0x100000, 0
        self._post_key(key_a, True, command_flag, pid=pid)
        self._post_key(key_a, False, command_flag, pid=pid)
        time.sleep(0.2)

    def type_text(self, text: str, pid: int | None = None) -> None:
        for character in text:
            key_code, flags = self._key_for(character)
            self._post_key(key_code, True, flags, text=character, pid=pid)
            self._post_key(key_code, False, flags, text=character, pid=pid)
            time.sleep(0.015)
        time.sleep(0.2)

    def _focus_state(self, widget: Widget) -> str:
        focused = self._attribute(widget.element, "AXFocused")
        return "unknown" if focused is None else str(bool(self.foundation.CFBooleanGetValue(focused)))

    def _frontmost_pid(self) -> str:
        script = (
            "ObjC.import('AppKit');"
            "$.NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier;"
        )
        return subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout.strip()

    def replace_text(self, pid: int, name: str, text: str) -> None:
        """Replace the named text field's content with real keyboard events.

        Each bounded attempt activates the application, clicks the field,
        requires it to hold keyboard focus (setting the accessibility focus
        attribute when the click did not give it), selects all and types.  The
        first attempt posts through the HID tap; later ones post directly to
        the process.  A probe line records the focus state and the read-back
        value of every attempt.
        """

        diagnostics: list[str] = []
        for attempt, direct in enumerate((False, True, True), start=1):
            self.bring_forward(pid)
            field = self.find_widget(pid, "edit", name)
            self.click(*field.rect.center)
            time.sleep(0.3)
            focus = self._focus_state(field)
            if focus != "True":
                key = self._cfstring("AXFocused")
                try:
                    status = self.services.AXUIElementSetAttributeValue(
                        field.element, key, self._true
                    )
                finally:
                    self.foundation.CFRelease(key)
                time.sleep(0.2)
                focus = f"{focus}->{self._focus_state(field)} (AXFocused set status {status})"
            target = pid if direct else None
            self.select_all(target)
            self.type_text(text, target)
            time.sleep(0.3)
            value = self.find_widget(pid, "edit", name).value
            line = (
                f"attempt={attempt} post={'pid' if direct else 'hid'} focus={focus} "
                f"frontmost={self._frontmost_pid()} read_back={value!r}"
            )
            diagnostics.append(line)
            print(f"NYX-PROBE macos-input {line}")
            if value == text:
                return
        raise DriverError(
            f"the {name!r} field never took the typed text {text!r}: " + "; ".join(diagnostics)
        )

    # -- accessibility -----------------------------------------------------

    def find_widget(self, pid: int, role: str, name: str) -> Widget:
        want = {"button": "AXButton", "edit": "AXTextField"}[role]
        exact: list[Widget] = []
        native: list[Widget] = []

        def visit(element: Any) -> None:
            element_role = self._read_string(self._attribute(element, "AXRole"))
            if element_role == "AXWebArea":
                return  # web content is located through the page, not here
            if element_role == want:
                frame = self._frame(element)
                if frame is not None and frame.width > 0 and frame.height > 0:
                    label = ""
                    for attribute in ("AXTitle", "AXDescription", "AXLabel"):
                        label = self._read_string(self._attribute(element, attribute)) or ""
                        if label:
                            break
                    enabled_value = self._attribute(element, "AXEnabled")
                    enabled = (
                        True if enabled_value is None
                        else bool(self.foundation.CFBooleanGetValue(enabled_value))
                    )
                    value = self._read_string(self._attribute(element, "AXValue"))
                    widget = Widget(label, frame, enabled, value, element)
                    (exact if label == name else native).append(widget)
            for child in self._children(element):
                visit(child)

        for window in self._windows(pid):
            visit(window)
        matches = exact or (native if role == "edit" and len(native) == 1 else [])
        if not matches:
            raise DriverError(f"no visible {role} named {name!r} in the window of pid {pid}")
        if len(matches) != 1:
            raise DriverError(
                f"{len(matches)} visible {role} widgets match {name!r}: "
                f"{[m.name for m in matches]!r}"
            )
        return matches[0]

    # -- clipboard ---------------------------------------------------------

    @staticmethod
    def _pasteboard_environment() -> dict[str, str]:
        environment = dict(os.environ)
        environment["LANG"] = "en_US.UTF-8"
        environment["LC_ALL"] = "en_US.UTF-8"
        return environment

    def set_clipboard(self, text: str) -> None:
        completed = subprocess.run(
            ["pbcopy"], input=text.encode("utf-8"), capture_output=True,
            env=self._pasteboard_environment(), timeout=30, check=False,
        )
        if completed.returncode != 0:
            raise DriverError(f"pbcopy exited {completed.returncode}: {completed.stderr!r}")

    def get_clipboard(self) -> str | None:
        completed = subprocess.run(
            ["pbpaste"], capture_output=True, env=self._pasteboard_environment(),
            timeout=30, check=False,
        )
        if completed.returncode != 0:
            raise DriverError(f"pbpaste exited {completed.returncode}: {completed.stderr!r}")
        return completed.stdout.decode("utf-8", errors="replace") or None


def create_driver() -> Any:
    """Return the backend for this host."""

    if os.name == "nt":
        return _WindowsBackend()
    if sys.platform == "darwin":
        return _MacBackend()
    raise DriverError("the native driver exists only for Windows and macOS")


# ---------------------------------------------------------------------------
# Page-level DevTools client (locate and read only)
# ---------------------------------------------------------------------------

_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_FORBIDDEN_DOMAINS = frozenset({"Input", "Emulation"})


def encode_frame(opcode: int, payload: bytes, mask_key: bytes) -> bytes:
    """Encode one masked, final client WebSocket frame (RFC 6455)."""

    if len(mask_key) != 4:
        raise ValueError("the mask key is four bytes")
    length = len(payload)
    header = bytes([0x80 | opcode])
    if length < 126:
        header += bytes([0x80 | length])
    elif length < 1 << 16:
        header += bytes([0x80 | 126]) + struct.pack(">H", length)
    else:
        header += bytes([0x80 | 127]) + struct.pack(">Q", length)
    masked = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))
    return header + mask_key + masked


def read_frame(read_exact: Callable[[int], bytes]) -> tuple[bool, int, bytes]:
    """Read one frame through ``read_exact`` and return ``(fin, opcode, payload)``."""

    first, second = read_exact(2)
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        (length,) = struct.unpack(">H", read_exact(2))
    elif length == 127:
        (length,) = struct.unpack(">Q", read_exact(8))
    mask_key = read_exact(4) if masked else b""
    payload = read_exact(length) if length else b""
    if masked:
        payload = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))
    return fin, opcode, payload


def free_loopback_port(*, avoid: int) -> int:
    """Return an unused loopback TCP port different from ``avoid``."""

    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if port != avoid:
            return port
    raise DriverError("no free loopback port was available")


class CdpPage:
    """A minimal page-level DevTools WebSocket client.

    It evaluates expressions to locate and read web content.  Methods in the
    ``Input`` and ``Emulation`` domains are refused so that no input is ever
    injected through the page: every click and keystroke is OS-level.
    """

    def __init__(self, port: int, page_url_prefix: str, timeout: float = 15.0) -> None:
        self.port = port
        self.timeout = timeout
        self._next_id = 0
        self._buffer = b""
        self._socket: socket.socket | None = None
        self.target_url = ""
        websocket_path = self._discover(page_url_prefix)
        self._connect(websocket_path)

    def _discover(self, page_url_prefix: str) -> str:
        deadline = time.monotonic() + self.timeout
        last: object = None
        while time.monotonic() < deadline:
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            try:
                connection.request("GET", "/json/list")
                response = connection.getresponse()
                body = response.read()
                if response.status == 200:
                    targets = json.loads(body)
                    pages = [
                        target for target in targets
                        if target.get("type") == "page"
                        and str(target.get("url", "")).startswith(page_url_prefix)
                    ]
                    if pages:
                        self.target_url = pages[0]["url"]
                        url = pages[0]["webSocketDebuggerUrl"]
                        return "/" + url.split("/", 3)[3]
                    last = f"no page target under {page_url_prefix!r}: {targets!r}"
                else:
                    last = f"status {response.status}"
            except (OSError, ValueError, KeyError) as error:
                last = error
            finally:
                connection.close()
            time.sleep(0.25)
        raise CdpError(f"no DevTools page target on port {self.port}: {last!r}")

    def _connect(self, path: str) -> None:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=self.timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                raise CdpError(f"the DevTools handshake closed early: {data!r}")
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            sock.close()
            raise CdpError(f"the DevTools handshake was refused: {lines[0]!r}")
        accept = base64.b64encode(
            hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest()
        ).decode("ascii")
        headers = {
            line.split(":", 1)[0].strip().lower(): line.split(":", 1)[1].strip()
            for line in lines[1:] if ":" in line
        }
        if headers.get("sec-websocket-accept") != accept:
            sock.close()
            raise CdpError("the DevTools handshake returned a wrong accept key")
        self._buffer = rest
        self._socket = sock

    def _read_exact(self, count: int) -> bytes:
        assert self._socket is not None
        while len(self._buffer) < count:
            chunk = self._socket.recv(65536)
            if not chunk:
                raise CdpError("the DevTools connection closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        assert self._socket is not None
        self._socket.sendall(encode_frame(opcode, payload, os.urandom(4)))

    def _receive_message(self) -> str:
        fragments = b""
        while True:
            fin, opcode, payload = read_frame(self._read_exact)
            if opcode == 0x9:  # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:
                raise CdpError("the DevTools endpoint closed the connection")
            fragments += payload
            if fin:
                return fragments.decode("utf-8")

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if method.split(".", 1)[0] in _FORBIDDEN_DOMAINS:
            raise CdpError(f"{method} would inject input; all input must be OS-level")
        self._next_id += 1
        identifier = self._next_id
        message = {"id": identifier, "method": method, "params": params or {}}
        self._send_frame(0x1, json.dumps(message).encode("utf-8"))
        assert self._socket is not None
        self._socket.settimeout(self.timeout)
        while True:
            reply = json.loads(self._receive_message())
            if reply.get("id") != identifier:
                continue  # an event or a stale reply
            if "error" in reply:
                raise CdpError(f"{method} failed: {reply['error']!r}")
            return reply.get("result", {})

    def evaluate(self, expression: str) -> Any:
        """Evaluate a side-effect-free expression and return its JSON value."""

        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": False},
        )
        if "exceptionDetails" in result:
            raise CdpError(f"the page rejected the expression: {result['exceptionDetails']!r}")
        return result.get("result", {}).get("value")

    def close(self) -> None:
        if self._socket is not None:
            with contextlib.suppress(OSError):
                self._send_frame(0x8, b"")
            self._socket.close()
            self._socket = None

    def __enter__(self) -> "CdpPage":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def settle_measurement(measure: Callable[[], Any], limit: float = 3.0, pause: float = 0.25) -> Any:
    """Read ``measure`` until two consecutive reads agree or ``limit`` seconds pass."""

    previous = None
    latest = measure()
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        time.sleep(pause)
        latest = measure()
        if latest == previous:
            break
        previous = latest
    return latest


def point_is_on_screen(screen: Rect, x: float, y: float, edge: float = 0.0) -> bool:
    return (
        screen.left + edge <= x < screen.left + screen.width - edge
        and screen.top + edge <= y < screen.top + screen.height - edge
    )


def place_click_point_on_screen(
    driver: Any,
    pid: int,
    screen: Rect,
    calibrate: Callable[[], "PageCalibration"],
    choose: Callable[[Callable[[float, float], bool] | None], dict[str, Any] | None],
    settle: Callable[[], Any],
    *,
    max_moves: int = 2,
    edge: float = 20.0,
) -> dict[str, Any]:
    """Choose a card point whose screen position is safely on the screen.

    The window is large and may extend past the screen.  When the chosen point
    maps outside the screen's usable area (or within ``edge`` pixels of its
    border), the window is moved by OS window management by the delta that
    brings the point to the screen centre, the page is allowed to settle, the
    calibration is redone and the point recomputed.  At most ``max_moves``
    moves are made.  After the last, only points that remain off screen are
    dropped: a candidate that is on screen is used, otherwise this raises with
    every measurement.  ``choose(reachable)`` returns the geometry (with the
    chosen CSS ``point``) or ``None`` when no candidate qualifies.
    """

    centre = (screen.left + screen.width / 2, screen.top + screen.height / 2)
    moves: list[dict[str, Any]] = []
    for attempt in range(max_moves + 1):
        calibration = calibrate()
        geometry = choose(None)
        if geometry is None:
            raise DriverError(f"no clickable point on the card; moves so far {moves}")
        screen_point = calibration.to_screen(*geometry["point"])
        if point_is_on_screen(screen, *screen_point, edge):
            return {"geometry": geometry, "screen_point": screen_point,
                    "calibration": calibration, "moves": moves}
        if attempt == max_moves:
            break
        frame = driver.window_frame(pid)
        delta = (centre[0] - screen_point[0], centre[1] - screen_point[1])
        target = (frame.left + delta[0], frame.top + delta[1])
        driver.move_window(pid, *target)
        settle()
        moves.append({
            "from_point": list(screen_point), "delta": list(delta),
            "frame_before": [frame.left, frame.top, frame.width, frame.height],
            "window_to": list(target),
        })
        print(f"NYX-PROBE window-move {json.dumps(moves[-1])}")
    def reachable(x: float, y: float) -> bool:
        return point_is_on_screen(screen, *calibration.to_screen(x, y), edge)

    geometry = choose(reachable)
    if geometry is not None:
        screen_point = calibration.to_screen(*geometry["point"])
        if point_is_on_screen(screen, *screen_point, edge):
            return {"geometry": geometry, "screen_point": screen_point,
                    "calibration": calibration, "moves": moves}
    raise DriverError(
        f"the card point stays off screen after {len(moves)} window moves: screen {screen}, "
        f"last point {screen_point}, calibration origin ({calibration.origin_x:.1f},"
        f"{calibration.origin_y:.1f}) scale {calibration.scale:.3f}, moves {json.dumps(moves)}"
    )


def prepare_board_viewport(
    driver: Any,
    pid: int,
    measure: Callable[[], tuple[float, float, float]],
    board_point: Callable[[], tuple[float, float]],
    *,
    minimum_height: float = 300.0,
    minimum_band: float = 80.0,
    timeout: float = 10.0,
    pause: float = 0.25,
) -> dict[str, Any]:
    """Give the board a viewport in which a card can be clicked below the page toolbar.

    ``measure`` returns ``(viewport width, viewport height, toolbar bottom)`` in
    CSS pixels.  The window is first placed at a fixed generous size.  When the
    viewport is still unusable, OS input zooms the page out: the zoom-out
    shortcut up to three times and, only if the viewport did not change at all,
    Ctrl/Command+wheel notches over the board up to three times.  The result is
    usable when the viewport is at least ``minimum_height`` tall and the toolbar
    bottom leaves ``minimum_band`` CSS pixels above the viewport bottom; any
    other outcome is a ``DriverError`` carrying every measured step.
    """

    steps: list[dict[str, Any]] = []

    def usable(state: tuple[float, float, float]) -> bool:
        return state[1] >= minimum_height and state[1] - state[2] >= minimum_band

    def settle(limit: float) -> tuple[float, float, float]:
        return settle_measurement(measure, limit, pause)

    def record(name: str, state: tuple[float, float, float], **extra: Any) -> None:
        step = {"step": name, "viewport": [state[0], state[1]], "toolbar_bottom": state[2], **extra}
        steps.append(step)
        print(f"NYX-PROBE board-preparation {json.dumps(step)}")

    def frame_list() -> list[float]:
        frame = driver.window_frame(pid)
        return [frame.left, frame.top, frame.width, frame.height]

    before_frame = frame_list()
    before = measure()
    record("before", before, frame=before_frame)
    driver.enlarge_window(pid)
    state = settle(timeout)
    enlarged = state
    record("enlarged", state, frame=frame_list(), requested=list(LARGE_WINDOW_SIZE))
    if not usable(state):
        for attempt in range(1, 4):
            driver.zoom_out_shortcut()
            state = settle(3.0)
            record(f"shortcut-{attempt}", state)
            if usable(state):
                break
        if not usable(state) and state[:2] == enlarged[:2]:
            x, y = board_point()
            for attempt in range(1, 4):
                driver.zoom_out_wheel(x, y)
                state = settle(3.0)
                record(f"wheel-{attempt}", state, pointer=[x, y])
                if usable(state):
                    break
    if not usable(state):
        raise DriverError(
            "the board has no usable viewport: need a height of at least "
            f"{minimum_height:g} CSS px with the toolbar bottom {minimum_band:g} px above the "
            f"viewport bottom; measured steps {json.dumps(steps)}"
        )
    return {
        "steps": steps,
        "frame_before": before_frame,
        "frame_after": frame_list(),
        "viewport_before": [before[0], before[1]],
        "viewport_after": [state[0], state[1]],
        "toolbar_bottom": state[2],
    }


def _intersect(first: Rect, second: Rect) -> Rect | None:
    left, top = max(first.left, second.left), max(first.top, second.top)
    right = min(first.left + first.width, second.left + second.width)
    bottom = min(first.top + first.height, second.top + second.height)
    if right - left < 1 or bottom - top < 1:
        return None
    return Rect(left, top, right - left, bottom - top)


def card_click_candidates(
    geometry: dict[str, Any], *, margin: float = 4.0, inset: float = 4.0, minimum: float = 6.0
) -> list[tuple[float, float]]:
    """Return click points inside the card, below the sticky toolbar and above any bottom cover.

    The card rectangle is intersected with the viewport band that no fixed or
    sticky element covers.  A card taller than the band still yields points
    from the band, because the band, not the card, bounds the search.  An
    empty or too small intersection returns no points.
    """

    left, top, width, height = geometry["rect"]
    viewport_width, _ = geometry["client"]
    band_top = geometry["toolbarBottom"] + margin
    band_bottom = geometry["bottomLimit"] - margin
    y_low = max(top + inset, band_top)
    y_high = min(top + height - inset, band_bottom)
    x_low = max(left + inset, 0.0)
    x_high = min(left + width - inset, viewport_width)
    if y_high - y_low < minimum or x_high - x_low < minimum:
        return []
    return [
        (x_low + (x_high - x_low) * fx, y_low + (y_high - y_low) * fy)
        for fy in (0.5, 0.25, 0.75, 0.1, 0.9)
        for fx in (0.5, 0.25, 0.75)
    ]


@dataclass
class PageCalibration:
    """Map page CSS pixels to screen pixels from two observed pointer moves."""

    origin_x: float
    origin_y: float
    scale: float = 1.0
    moves: list[tuple[float, float, float, float]] = field(default_factory=list)

    def to_screen(self, css_x: float, css_y: float) -> tuple[float, float]:
        return self.origin_x + self.scale * css_x, self.origin_y + self.scale * css_y


_INSTALL_POINTER_RECORDER = (
    "(() => { window.__nyxPointer = null;"
    " document.addEventListener('mousemove', (event) => {"
    "   window.__nyxPointer = [event.clientX, event.clientY]; }, true);"
    " return true; })()"
)


def find_pointer_base(
    driver: Any, page: CdpPage, frame: Rect, screen: Rect | None = None
) -> tuple[float, float, float, float]:
    """Find a visible on-screen point over the page and return it with its page coordinates.

    A window larger than the screen leaves part of the page unreachable, so the
    search covers the part of the window on the screen, trying several heights
    until the page reports receiving the OS pointer.
    """

    page.evaluate(_INSTALL_POINTER_RECORDER)
    visible = _intersect(frame, screen) if screen is not None else frame
    if visible is None:
        raise DriverError(f"the window {frame} lies outside the screen {screen}")
    tried: list[tuple[float, float]] = []
    for fraction in (0.85, 0.7, 0.55, 0.4, 0.25):
        screen_x = visible.left + visible.width * 0.75
        screen_y = visible.top + visible.height * fraction
        tried.append((round(screen_x), round(screen_y)))
        driver.move(screen_x + 5, screen_y + 5)  # enter the page before the measured move
        driver.move(screen_x, screen_y)
        client = None
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            client = page.evaluate("window.__nyxPointer")
            if client:
                break
            time.sleep(0.1)
        page.evaluate("window.__nyxPointer = null")
        if client:
            return screen_x, screen_y, float(client[0]), float(client[1])
    raise DriverError(
        f"the page received no pointer move at any of {tried} (window {frame}, screen {screen})"
    )


def calibrate_page(
    driver: Any, page: CdpPage, frame: Rect, screen: Rect | None = None
) -> PageCalibration:
    """Learn where the page sits on screen by moving the OS pointer over it.

    The page only records pointer positions it receives; the moves themselves
    are OS input, so a mapping error shows up as a hard failure here rather
    than as a missed click later.
    """

    first = find_pointer_base(driver, page, frame, screen)
    observed = [first]
    sx1, sy1 = first[0], first[1]
    second = None
    for offset_x, offset_y in ((-60.0, -40.0), (60.0, 40.0), (-60.0, 40.0)):
        screen_x, screen_y = sx1 + offset_x, sy1 + offset_y
        driver.move(screen_x + 5, screen_y + 5)
        driver.move(screen_x, screen_y)
        client = None
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            client = page.evaluate("window.__nyxPointer")
            if client:
                break
            time.sleep(0.1)
        page.evaluate("window.__nyxPointer = null")
        if client:
            second = (screen_x, screen_y, float(client[0]), float(client[1]))
            break
    if second is None:
        raise DriverError(f"the page received no second pointer move near {first[:2]}")
    observed.append(second)
    (sx1, sy1, cx1, cy1), (sx2, sy2, cx2, cy2) = observed
    if abs(cx1 - cx2) < 1 or abs(cy1 - cy2) < 1:
        raise DriverError(f"the page pointer did not follow the OS pointer: {observed!r}")
    scale_x = (sx1 - sx2) / (cx1 - cx2)
    scale_y = (sy1 - sy2) / (cy1 - cy2)
    if abs(scale_x - scale_y) > 0.05 * max(scale_x, scale_y):
        raise DriverError(f"inconsistent page scale {scale_x:.3f} vs {scale_y:.3f}: {observed!r}")
    scale = (scale_x + scale_y) / 2
    return PageCalibration(sx1 - scale * cx1, sy1 - scale * cy1, scale, observed)
