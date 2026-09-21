"""Native desktop entry and first-launch workspace chooser.

The desktop entry is intentionally a thin shell in this milestone.  It owns
the account lifetime while the chooser is visible, and it uses the portable
state owner for all configuration validation and replacement.  Runtime and
worker admission are added by later desktop slices; this module does not
silently start the retained Linux daemon.

Qt is imported only when :func:`main` is called.  Importing this module is
therefore safe in the dependency-light Linux console and wheel paths.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

from . import runtime, state
from ._native_claim import NativeClaim
from .app_runtime import ApplicationRuntime
from .worker import CatalogWorkerManager

APPLICATION_CLAIM_FILENAME = "lease.lock"
RECOVERY_CLAIM_FILENAME = "recovery.lock"
ALREADY_OPEN_MESSAGE = "Nyx is already open"
UNAVAILABLE_MESSAGE = "Nyx is unavailable"


class DesktopError(RuntimeError):
    """Base class for bounded desktop-entry errors."""


class AlreadyOpenError(DesktopError):
    """A qualified account application claim is already held."""


class DesktopUnavailableError(DesktopError):
    """Account state or native claims cannot be safely admitted."""


class DesktopDependencyError(DesktopError):
    """The optional desktop dependency is not installed."""


class SelectionUnavailableError(DesktopError):
    """Selection cannot be changed while persisted state is unverified."""


@dataclass(frozen=True)
class DesktopSnapshot:
    """The chooser-visible state without exposing raw persisted diagnostics."""

    status: str
    configuration: state.Configuration | None = None
    diagnostic: str | None = None


class DesktopClaims:
    """Hold the account and recovery claims for one desktop application."""

    def __init__(self, paths: state.StatePaths) -> None:
        self.paths = paths
        self.application = NativeClaim(paths.runtime_directory / APPLICATION_CLAIM_FILENAME)
        self.recovery = NativeClaim(paths.runtime_directory / RECOVERY_CLAIM_FILENAME)
        self.parent_liveness_read, self.parent_liveness_write = os.pipe()
        self._recovery_blocked = False
        self._held = False

    @property
    def held(self) -> bool:
        return self._held and self.application.held and self.recovery.held

    @property
    def recovery_blocked(self) -> bool:
        return self._held and self.application.held and not self.recovery.held

    def acquire(self) -> None:
        # The operation claim serializes admission with legacy setup/start.
        # It is deliberately released only after both long-lived claims are
        # held; the application and recovery claims then span the shell.
        operation = runtime._operation_lock(self.paths, timeout=0.0)
        try:
            if not operation.acquire(blocking=False):
                raise DesktopUnavailableError(UNAVAILABLE_MESSAGE)
            try:
                if not self.application.acquire(blocking=False):
                    raise AlreadyOpenError(ALREADY_OPEN_MESSAGE)
                try:
                    if not self.recovery.acquire(blocking=False):
                        self._recovery_blocked = True
                except BaseException:
                    self.application.close()
                    raise
            except BaseException:
                raise
        except AlreadyOpenError:
            raise
        except DesktopUnavailableError:
            raise
        except (OSError, RuntimeError, state.StateError) as error:
            raise DesktopUnavailableError(UNAVAILABLE_MESSAGE) from error
        finally:
            operation.close()
        self._held = True

    def retry_recovery(self) -> bool:
        """Acquire the former worker claim after its children terminate."""

        if not self._held or not self.application.held:
            raise DesktopUnavailableError(UNAVAILABLE_MESSAGE)
        if self.recovery.held:
            self._recovery_blocked = False
            return True
        operation = runtime._operation_lock(self.paths, timeout=0.0)
        try:
            if not operation.acquire(blocking=False):
                return False
            acquired = self.recovery.acquire(blocking=False)
            if acquired:
                self._recovery_blocked = False
            return acquired
        except OSError as error:
            raise DesktopUnavailableError(UNAVAILABLE_MESSAGE) from error
        finally:
            operation.close()

    def close(self) -> None:
        try:
            os.close(self.parent_liveness_write)
        except OSError:
            pass
        try:
            os.close(self.parent_liveness_read)
        except OSError:
            pass
        self.recovery.close()
        self.application.close()
        self._held = False

    def __enter__(self) -> "DesktopClaims":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class DesktopSession:
    """A claim-held desktop session with optional shared runtime workers."""

    def __init__(self, *, paths: state.StatePaths | None = None) -> None:
        claims: DesktopClaims | None = None
        try:
            selected_paths = state.state_paths(create=True) if paths is None else paths
            claims = DesktopClaims(selected_paths)
            claims.acquire()
        except AlreadyOpenError:
            if claims is not None:
                claims.close()
            raise
        except DesktopUnavailableError:
            if claims is not None:
                claims.close()
            raise
        except (OSError, state.StateError, RuntimeError) as error:
            if claims is not None:
                claims.close()
            raise DesktopUnavailableError(UNAVAILABLE_MESSAGE) from error
        assert claims is not None
        self.claims = claims
        self.paths = selected_paths
        self._closed = False
        self._unverified = False
        self._pending_error: str | None = None
        self._application_runtime: ApplicationRuntime | None = None
        self.snapshot = self._observe()
        if self.claims.recovery_blocked:
            self.snapshot = DesktopSnapshot(
                "recovery_blocked", diagnostic="Nyx is recovering; retry when workers exit."
            )

    @property
    def unverified(self) -> bool:
        return self._unverified

    @property
    def configuration(self) -> state.Configuration | None:
        return self.snapshot.configuration

    @property
    def pending_error(self) -> str | None:
        return self._pending_error

    @property
    def runtime(self) -> ApplicationRuntime | None:
        return self._application_runtime

    @property
    def recovery_blocked(self) -> bool:
        return self.claims.recovery_blocked

    def retry_recovery(self) -> bool:
        """Retry former-worker cleanup without changing persisted state."""

        if self._closed:
            raise DesktopUnavailableError("Nyx desktop session is closed")
        acquired = self.claims.retry_recovery()
        if not acquired:
            self.snapshot = DesktopSnapshot(
                "recovery_blocked", diagnostic="Nyx is recovering; retry when workers exit."
            )
            return False
        self._pending_error = None
        self.snapshot = self._observe()
        return self.snapshot.status == "configured"

    def start_runtime(
        self,
        *,
        deadline: float | None = None,
        static_ready: Any | None = None,
    ) -> ApplicationRuntime:
        """Start the shared application runtime after desktop admission."""

        if self._closed or not self.claims.held:
            raise DesktopUnavailableError(UNAVAILABLE_MESSAGE)
        if self.snapshot.status != "configured":
            raise DesktopUnavailableError(UNAVAILABLE_MESSAGE)
        if self._application_runtime is not None:
            return self._application_runtime
        selected_deadline = deadline or (time.monotonic() + runtime.STARTUP_TIMEOUT)
        workers = CatalogWorkerManager(
            recovery_claim=self.claims.recovery,
            recovery_path=self.claims.recovery.path,
            parent_liveness_fd=self.claims.parent_liveness_read,
        )
        application = ApplicationRuntime(
            port=runtime.PORT,
            deadline=selected_deadline,
            workers=workers,
        )
        try:
            application.start(static_ready=static_ready)
            application.admit_catalog()
        except BaseException as error:
            application.cleanup_start_failure(selected_deadline)
            raise DesktopUnavailableError(UNAVAILABLE_MESSAGE) from error
        self._application_runtime = application
        return application

    def _observe(self) -> DesktopSnapshot:
        try:
            observation = state.observe_configuration()
        except state.StateError:
            return DesktopSnapshot("unavailable", diagnostic=UNAVAILABLE_MESSAGE)
        if observation.status == "configured":
            return DesktopSnapshot("configured", observation.configuration)
        if observation.status == "not_configured":
            return DesktopSnapshot("not_configured")
        return DesktopSnapshot("unavailable", diagnostic=UNAVAILABLE_MESSAGE)

    def choose_workspace(
        self,
        specification_root: str | os.PathLike[str],
        hidden_stages: tuple[str, ...] | list[str] | object = state._OMITTED,
    ) -> state.Configuration:
        """Validate and atomically save a chooser selection while claims are held."""

        if self._closed:
            raise DesktopUnavailableError("Nyx desktop session is closed")
        if not self.claims.held:
            raise SelectionUnavailableError(
                "Nyx is recovering; retry when workers exit"
            )
        if self._unverified:
            raise SelectionUnavailableError(
                "Nyx configuration is unverified; revalidate before saving"
            )
        if self.snapshot.status == "unavailable":
            raise SelectionUnavailableError(
                "Nyx configuration is unavailable; repair or revalidate it first"
            )
        try:
            configuration = state.save_configuration_owned(
                specification_root,
                hidden_stages,
                paths=self.paths,
            )
        except state.ConfigurationCommitVerificationError as error:
            # The replacement has committed.  Preserve those bytes and keep
            # the shell unavailable until state-owner revalidation succeeds.
            self._unverified = True
            self._pending_error = "Nyx configuration was saved but is unverified"
            self.snapshot = DesktopSnapshot("unavailable", diagnostic=UNAVAILABLE_MESSAGE)
            raise SelectionUnavailableError(self._pending_error) from error
        except (state.ConfigurationError, state.StateError) as error:
            # Validation or a pre-replacement write failure leaves the old
            # record (or first-launch absence) unchanged.
            self._pending_error = "Nyx workspace could not be saved"
            self.snapshot = self._observe()
            raise SelectionUnavailableError(self._pending_error) from error
        self._pending_error = None
        self.snapshot = DesktopSnapshot("configured", configuration)
        return configuration

    def revalidate(self) -> state.Configuration:
        """Retry state-owner validation after a committed verification failure."""

        if self._closed:
            raise DesktopUnavailableError("Nyx desktop session is closed")
        if not self.claims.held:
            raise SelectionUnavailableError(
                "Nyx is recovering; retry when workers exit"
            )
        try:
            configuration = state.revalidate_configuration(self.paths)
        except state.StateError as error:
            self._pending_error = "Nyx configuration remains unavailable"
            self.snapshot = DesktopSnapshot("unavailable", diagnostic=UNAVAILABLE_MESSAGE)
            raise SelectionUnavailableError(self._pending_error) from error
        self._unverified = False
        self._pending_error = None
        self.snapshot = DesktopSnapshot("configured", configuration)
        return configuration

    def cancel(self) -> None:
        """Cancel the chooser without changing persisted state."""

        self.close()

    def close(self) -> None:
        if self._closed:
            return
        if self._application_runtime is not None:
            deadline = time.monotonic() + runtime.SHUTDOWN_TIMEOUT
            if not self._application_runtime.shutdown(deadline):
                self._pending_error = "Nyx workers are still stopping; retry Close"
                self.snapshot = DesktopSnapshot(
                    "recovery_blocked", diagnostic=UNAVAILABLE_MESSAGE
                )
                return
            self._application_runtime = None
        self.claims.close()
        self._closed = True

    def __enter__(self) -> "DesktopSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _load_qt() -> dict[str, Any]:
    """Load Qt only for the GUI entry, never during ordinary package import."""

    try:
        from PySide6 import QtCore, QtWidgets
    except ImportError as error:  # pragma: no cover - host dependency selection
        raise DesktopDependencyError(
            "PySide6 is required; install the nyx[desktop] extra"
        ) from error
    return {"QtCore": QtCore, "QtWidgets": QtWidgets}


def _build_window(qt: dict[str, Any], session: DesktopSession) -> Any:
    QtCore = qt["QtCore"]
    QtWidgets = qt["QtWidgets"]

    class DesktopWindow(QtWidgets.QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("Nyx")
            self.resize(720, 460)
            self._session = session
            self._root = QtWidgets.QLineEdit(self)
            self._status = QtWidgets.QLabel(self)
            self._status.setWordWrap(True)
            self._change = QtWidgets.QPushButton("Change workspace", self)
            self._save = QtWidgets.QPushButton("Save workspace", self)
            self._cancel = QtWidgets.QPushButton("Cancel", self)
            self._retry = QtWidgets.QPushButton("Revalidate", self)
            self._quit = QtWidgets.QPushButton("Quit", self)
            self._changing = self._session.snapshot.status == "not_configured"
            self._change.clicked.connect(self._begin_change)
            self._save.clicked.connect(self._save_selection)
            self._cancel.clicked.connect(self.close)
            self._retry.clicked.connect(self._revalidate)
            self._quit.clicked.connect(self.close)
            form = QtWidgets.QFormLayout()
            form.addRow("Workspace root", self._root)
            actions = QtWidgets.QHBoxLayout()
            actions.addWidget(self._change)
            actions.addWidget(self._save)
            actions.addWidget(self._retry)
            actions.addWidget(self._cancel)
            actions.addWidget(self._quit)
            body = QtWidgets.QWidget(self)
            layout = QtWidgets.QVBoxLayout(body)
            layout.addWidget(self._status)
            layout.addLayout(form)
            layout.addLayout(actions)
            self.setCentralWidget(body)
            self._render()

        def _render(self) -> None:
            snapshot = self._session.snapshot
            if snapshot.status == "configured" and snapshot.configuration is not None:
                if not self._changing:
                    self._root.setText(str(snapshot.configuration.specification_root))
                if self._session.pending_error is not None:
                    self._status.setText(self._session.pending_error)
                elif self._changing:
                    self._status.setText("Choose a replacement workspace.")
                else:
                    self._status.setText(
                        f"Workspace: {snapshot.configuration.specification_root}"
                    )
                self._root.setEnabled(self._changing)
                self._change.setEnabled(not self._changing)
                self._save.setEnabled(self._changing)
                self._retry.setEnabled(False)
            elif snapshot.status == "not_configured":
                self._changing = True
                self._status.setText(
                    self._session.pending_error or "Choose a workspace to begin."
                )
                self._root.setEnabled(True)
                self._change.setEnabled(False)
                self._save.setEnabled(True)
                self._retry.setEnabled(False)
            else:
                self._status.setText(
                    self._session.pending_error or "Workspace configuration is unavailable."
                )
                self._root.setEnabled(False)
                self._change.setEnabled(False)
                self._save.setEnabled(False)
                self._retry.setEnabled(
                    self._session.unverified or self._session.recovery_blocked
                )

        def _begin_change(self) -> None:
            self._changing = True
            self._render()

        def _save_selection(self) -> None:
            try:
                self._session.choose_workspace(self._root.text())
            except DesktopError as error:
                self._status.setText(str(error))
            else:
                self._changing = False
            self._render()

        def _revalidate(self) -> None:
            try:
                if self._session.recovery_blocked:
                    self._session.retry_recovery()
                else:
                    self._session.revalidate()
            except DesktopError as error:
                self._status.setText(str(error))
            else:
                self._changing = False
            self._render()

        def closeEvent(self, event: Any) -> None:
            self._session.close()
            event.accept()

    # Keep the QtCore reference live for native binding implementations that
    # inspect the class module while delivering close events.
    _ = QtCore
    return DesktopWindow()


def main(argv: list[str] | None = None) -> int:
    """Run the optional GUI entry while retaining the console entry unchanged."""

    parser = argparse.ArgumentParser(prog="nyx-desktop")
    parser.parse_args(argv)
    try:
        session = DesktopSession()
    except AlreadyOpenError as error:
        print(str(error), file=sys.stderr)
        return 1
    except DesktopError as error:
        print(f"nyx-desktop: {error}", file=sys.stderr)
        return 1
    try:
        try:
            qt = _load_qt()
        except DesktopError as error:
            print(f"nyx-desktop: {error}", file=sys.stderr)
            return 1
        application = qt["QtWidgets"].QApplication.instance()
        owns_application = application is None
        if application is None:
            application = qt["QtWidgets"].QApplication(sys.argv[:1])
        window = _build_window(qt, session)
        window.show()
        if not owns_application:
            return 0
        return int(application.exec())
    finally:
        session.close()


if __name__ == "__main__":  # pragma: no cover - native host entry
    raise SystemExit(main())


__all__ = [
    "ALREADY_OPEN_MESSAGE",
    "APPLICATION_CLAIM_FILENAME",
    "DesktopClaims",
    "DesktopDependencyError",
    "DesktopError",
    "DesktopSession",
    "DesktopSnapshot",
    "DesktopUnavailableError",
    "RECOVERY_CLAIM_FILENAME",
    "SelectionUnavailableError",
    "main",
]
