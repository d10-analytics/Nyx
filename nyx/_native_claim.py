"""Private native ownership primitive used by the Nyx lifecycle.

The file is deliberately small and private.  A claim is represented by a
persistent regular file and ownership is represented only by the open native
object; releasing a claim therefore means closing that object, never unlinking
the file or issuing an explicit unlock on POSIX.
"""

from __future__ import annotations

import errno
import os
import stat
import time
from pathlib import Path
from typing import Self

if os.name == "nt":  # pragma: no cover - exercised by the native Windows lane
    import ctypes
    import msvcrt
    from ctypes import wintypes
else:  # pragma: no cover - import selection is platform-defined
    import fcntl


_BUSY_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EDEADLK, 13, 32, 33})
_MISSING = "absent"


def _identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _regular(details: os.stat_result) -> bool:
    return stat.S_ISREG(details.st_mode) and not stat.S_ISLNK(details.st_mode)


def _busy(error: OSError) -> bool:
    return error.errno in _BUSY_ERRNOS or getattr(error, "winerror", None) in {
        32,
        33,
    }


def _sharing_busy(error: OSError) -> bool:
    """Return whether an open failed because Windows share-zero is held."""

    winerror = getattr(error, "winerror", None)
    return winerror in {32, 33} or (os.name == "nt" and error.errno in {32, 33})


def _remaining(deadline: float | None) -> float:
    if deadline is None:
        return 0.02
    return max(0.0, deadline - time.monotonic())


def _lock_fd(fd: int, *, blocking: bool, deadline: float | None) -> bool:
    if os.name == "nt":  # pragma: no cover - exercised by the native Windows lane
        os.lseek(fd, 0, os.SEEK_SET)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        while True:
            try:
                msvcrt.locking(fd, mode, 1)
                return True
            except OSError as error:
                if not _busy(error):
                    raise
                if not blocking or (deadline is not None and _remaining(deadline) <= 0):
                    return False
                time.sleep(min(0.02, _remaining(deadline)))
    operation = fcntl.LOCK_EX | fcntl.LOCK_NB
    while True:
        try:
            fcntl.flock(fd, operation)
            return True
        except OSError as error:
            if not _busy(error):
                raise
            if not blocking or (deadline is not None and _remaining(deadline) <= 0):
                return False
            time.sleep(min(0.02, _remaining(deadline)))


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised by the native Windows lane
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    # POSIX deliberately has no explicit unlock.  Closing the descriptor is
    # the release operation and remains safe when the descriptor is inherited.


def _open_claim(path: Path, *, create: bool) -> int:
    if os.name != "nt":
        flags = os.O_RDWR | (os.O_CREAT if create else 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        return os.open(path, flags, 0o600)
    # Share-zero CreateFileW is the Windows ownership primitive.  The CRT
    # descriptor exists only to make the handle transferable and closeable.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(
        wintypes.LPCWSTR(str(path)),
        0x80000000 | 0x40000000,
        0,
        None,
        4 if create else 3,
        0x80,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.get_last_error()
        raise OSError(error, "CreateFileW failed", str(path))
    return msvcrt.open_osfhandle(handle, os.O_RDWR)


class NativeClaim:
    """One close-released native exclusive claim on a persistent file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.fd: int | None = None
        self.identity: tuple[int, int, int, int, int, int] | None = None

    @property
    def held(self) -> bool:
        return self.fd is not None

    def acquire(
        self,
        *,
        create: bool = True,
        blocking: bool = True,
        deadline: float | None = None,
    ) -> bool:
        if deadline is not None and _remaining(deadline) <= 0:
            raise TimeoutError("native claim deadline expired")
        try:
            fd = _open_claim(self.path, create=create)
            details = os.fstat(fd)
            if not _regular(details):
                os.close(fd)
                raise OSError(errno.ELOOP, "claim is not a regular file")
            if details.st_size == 0:
                if deadline is not None and _remaining(deadline) <= 0:
                    os.close(fd)
                    raise TimeoutError("native claim deadline expired")
                os.write(fd, b"\0")
        except OSError as error:
            if _sharing_busy(error):
                return False
            raise
        try:
            if deadline is not None and _remaining(deadline) <= 0:
                raise TimeoutError("native claim deadline expired")
            if not _lock_fd(fd, blocking=blocking, deadline=deadline):
                os.close(fd)
                return False
            self.fd = fd
            self.identity = _identity(os.fstat(fd))
            return True
        except BaseException:
            os.close(fd)
            raise

    def close(self) -> None:
        fd, self.fd = self.fd, None
        self.identity = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self) -> Self:
        if not self.acquire():
            raise TimeoutError("native claim is busy")
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @classmethod
    def probe(cls, path: Path) -> str:
        """Probe an existing path without creating or repairing it.

        Results are ``absent``, ``free``, ``held``, ``changed`` or ``unsafe``.
        Only a qualified native busy result is reported as ``held``.
        """

        try:
            before = path.lstat()
        except FileNotFoundError:
            return _MISSING
        except OSError as error:
            # A share-zero Windows owner prevents even an existing-only open.
            # That qualified sharing violation is the ownership observation;
            # unrelated open failures remain unsafe.
            return "held" if _sharing_busy(error) else "unsafe"
        if not _regular(before):
            return "unsafe"
        fd: int | None = None
        try:
            fd = _open_claim(path, create=False)
            after = os.fstat(fd)
            if not _regular(after):
                return "unsafe"
            if _identity(before) != _identity(after):
                return "changed"
            try:
                if _lock_fd(fd, blocking=False, deadline=None):
                    return "free"
                return "held"
            except OSError as error:
                return "held" if _busy(error) else "unsafe"
        except FileNotFoundError:
            return "changed"
        except OSError as error:
            return "held" if _sharing_busy(error) else "unsafe"
        finally:
            if fd is not None:
                os.close(fd)

    @classmethod
    def validate_received(cls, fd: int, path: Path) -> bool:
        """Validate the actual received object, without opening ``path``."""

        try:
            received = os.fstat(fd)
            current = path.lstat()
        except OSError:
            return False
        return _regular(received) and _regular(current) and _identity(received) == _identity(current)

    @staticmethod
    def transfer_handle(fd: int) -> int:
        """Return the native object identifier supplied to a child process."""

        if os.name == "nt":  # pragma: no cover - exercised by the native Windows lane
            return int(msvcrt.get_osfhandle(fd))
        return fd

    @staticmethod
    def receive_handle(handle: int, *, write_only: bool = False) -> int:
        """Map an inherited native object into this process's descriptor table."""

        if os.name == "nt":  # pragma: no cover - exercised by the native Windows lane
            flags = os.O_WRONLY if write_only else os.O_RDWR
            return msvcrt.open_osfhandle(handle, flags)
        return handle


__all__ = ["NativeClaim"]
