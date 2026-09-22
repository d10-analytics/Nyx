"""Native desktop entry and first-launch workspace chooser.

The desktop entry owns the account lifetime while the shell is visible.  It
uses the portable state owner for configuration and the shared application
runtime for HTTP/catalog work; it never starts the retained Linux daemon.

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


@dataclass(frozen=True)
class _PendingSwitch:
    configuration: state.Configuration
    runtime_expected: bool


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
        self._runtime_start_failed = False
        self._shutdown_blocked = False
        self._switch_blocked = False
        self._switch_in_progress = False
        self._quit_requested = False
        self._pending_switch: _PendingSwitch | None = None
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

    @property
    def runtime_retryable(self) -> bool:
        return self.snapshot.status == "runtime_blocked"

    @property
    def shutdown_blocked(self) -> bool:
        return self._shutdown_blocked

    @property
    def switch_blocked(self) -> bool:
        return self._switch_blocked

    @property
    def switch_in_progress(self) -> bool:
        return self._switch_in_progress

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

    def retry_runtime(self, *, deadline: float | None = None) -> bool:
        """Acquire crash recovery and retry an incomplete runtime start."""

        if self._closed:
            raise DesktopUnavailableError("Nyx desktop session is closed")
        if self.claims.recovery_blocked and not self.retry_recovery():
            return False
        selected_deadline = (
            time.monotonic() + runtime.STARTUP_TIMEOUT
            if deadline is None
            else deadline
        )
        if self._application_runtime is not None:
            if not self._runtime_start_failed:
                return True
            try:
                cleaned = self._application_runtime.cleanup_start_failure(
                    selected_deadline
                )
            except Exception:
                cleaned = False
            if not cleaned:
                self._pending_error = "Nyx runtime cleanup is incomplete; retry"
                configuration = self.snapshot.configuration or self._observe().configuration
                self.snapshot = DesktopSnapshot(
                    "runtime_blocked",
                    configuration,
                    diagnostic=UNAVAILABLE_MESSAGE,
                )
                return False
            self._application_runtime = None
            self._runtime_start_failed = False
        try:
            self.start_runtime(deadline=selected_deadline)
        except DesktopUnavailableError:
            return False
        return True

    def retry_workspace_switch(self) -> bool:
        """Retry a stopped or blocked workspace switch with retained claims."""

        if self._closed:
            raise DesktopUnavailableError("Nyx desktop session is closed")
        pending = self._pending_switch
        if pending is None:
            return False
        if self._unverified:
            self.revalidate()
        self._switch_blocked = False
        try:
            self._switch_to(pending.configuration, pending.runtime_expected)
        except DesktopError:
            return False
        return not self._switch_blocked and not self._unverified

    def start_runtime(
        self,
        *,
        deadline: float | None = None,
        static_ready: Any | None = None,
    ) -> ApplicationRuntime:
        """Start the shared application runtime after desktop admission."""

        if self._closed or not self.claims.held:
            raise DesktopUnavailableError(UNAVAILABLE_MESSAGE)
        if self.snapshot.status not in {"configured", "runtime_blocked"}:
            raise DesktopUnavailableError(UNAVAILABLE_MESSAGE)
        if self._application_runtime is not None:
            if self._runtime_start_failed:
                raise DesktopUnavailableError(UNAVAILABLE_MESSAGE)
            return self._application_runtime
        selected_deadline = (
            time.monotonic() + runtime.STARTUP_TIMEOUT
            if deadline is None
            else deadline
        )
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
        self._application_runtime = application
        try:
            application.start(static_ready=static_ready)
            application.admit_catalog()
        except BaseException as error:
            self._runtime_start_failed = True
            try:
                cleaned = application.cleanup_start_failure(selected_deadline)
            except Exception:
                cleaned = False
            if cleaned:
                self._application_runtime = None
                self._runtime_start_failed = False
            self._pending_error = "Nyx runtime could not start; retry"
            configuration = self.snapshot.configuration or self._observe().configuration
            self.snapshot = DesktopSnapshot(
                "runtime_blocked",
                configuration,
                diagnostic=UNAVAILABLE_MESSAGE,
            )
            raise DesktopUnavailableError(UNAVAILABLE_MESSAGE) from error
        self._runtime_start_failed = False
        self._shutdown_blocked = False
        self._pending_error = None
        self.snapshot = self._observe()
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

    def _stop_runtime_for_switch(self) -> bool:
        application = self._application_runtime
        if application is None:
            return not self._runtime_start_failed
        deadline = time.monotonic() + runtime.SHUTDOWN_TIMEOUT
        try:
            if self._runtime_start_failed:
                stopped = application.cleanup_start_failure(deadline)
            else:
                stopped = application.shutdown(deadline)
        except Exception:
            stopped = False
        if not stopped:
            self._shutdown_blocked = True
            return False
        self._application_runtime = None
        self._runtime_start_failed = False
        self._shutdown_blocked = False
        return True

    def _finish_switch_failure(self, message: str) -> None:
        self._switch_blocked = True
        self._pending_error = message
        configuration = self.snapshot.configuration or self._observe().configuration
        self.snapshot = DesktopSnapshot("switch_blocked", configuration)

    def _finish_switch_close(self) -> None:
        self._switch_in_progress = False
        self._pending_switch = None
        self._switch_blocked = False
        self._quit_requested = False
        self.close()

    def _switch_to(
        self,
        configuration: state.Configuration,
        runtime_expected: bool,
    ) -> state.Configuration:
        self._switch_in_progress = True
        self._switch_blocked = False
        self._pending_switch = _PendingSwitch(configuration, runtime_expected)
        try:
            if not self._stop_runtime_for_switch():
                self._switch_in_progress = False
                self._finish_switch_failure(
                    "Nyx workers are still stopping; retry workspace change"
                )
                raise SelectionUnavailableError(self._pending_error or UNAVAILABLE_MESSAGE)
            if self._quit_requested:
                self._finish_switch_close()
                raise SelectionUnavailableError("Nyx workspace change canceled")
            try:
                committed = state.save_configuration_owned(
                    configuration.specification_root,
                    configuration.hidden_stages,
                    paths=self.paths,
                )
            except state.ConfigurationCommitVerificationError as error:
                self._unverified = True
                self._switch_in_progress = False
                self._pending_error = "Nyx configuration was saved but is unverified"
                self.snapshot = DesktopSnapshot(
                    "unavailable", diagnostic=UNAVAILABLE_MESSAGE
                )
                raise SelectionUnavailableError(self._pending_error) from error
            except (state.ConfigurationError, state.StateError) as error:
                self._switch_in_progress = False
                if runtime_expected:
                    self._finish_switch_failure("Nyx workspace could not be saved")
                else:
                    self._pending_switch = None
                    self._switch_blocked = False
                    self._pending_error = "Nyx workspace could not be saved"
                    self.snapshot = self._observe()
                raise SelectionUnavailableError(self._pending_error or UNAVAILABLE_MESSAGE) from error

            self._pending_switch = None
            self._switch_blocked = False
            self._unverified = False
            self._pending_error = None
            self.snapshot = DesktopSnapshot("configured", committed)
            if self._quit_requested:
                self._finish_switch_close()
                return committed
            if runtime_expected:
                try:
                    self.start_runtime()
                except DesktopError:
                    if self._quit_requested:
                        self._finish_switch_close()
                    raise
            self._switch_in_progress = False
            if self._quit_requested:
                self._finish_switch_close()
            return committed
        finally:
            if self._switch_in_progress and self._pending_switch is None:
                self._switch_in_progress = False

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
        if self._switch_in_progress or self._switch_blocked:
            raise SelectionUnavailableError(
                "Nyx workspace change is awaiting cleanup; retry it first"
            )
        try:
            current = (
                None
                if self.snapshot.status == "not_configured"
                else state.revalidate_configuration(self.paths)
            )
            configuration = state.validate_configuration_candidate(
                specification_root,
                hidden_stages,
                paths=self.paths,
            )
        except state.StateError as error:
            self._pending_error = "Nyx workspace could not be validated"
            raise SelectionUnavailableError(self._pending_error) from error
        runtime_expected = self._application_runtime is not None
        if self._runtime_start_failed:
            raise SelectionUnavailableError(
                "Nyx runtime cleanup is incomplete; retry before changing workspace"
            )
        if current is not None and current != self.snapshot.configuration:
            self.snapshot = DesktopSnapshot("configured", current)
        return self._switch_to(configuration, runtime_expected)

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
        if self._switch_in_progress:
            self._quit_requested = True
            return
        self._pending_switch = None
        self._switch_blocked = False
        if self._application_runtime is not None:
            deadline = time.monotonic() + runtime.SHUTDOWN_TIMEOUT
            try:
                if self._runtime_start_failed:
                    closed = self._application_runtime.cleanup_start_failure(deadline)
                else:
                    closed = self._application_runtime.shutdown(deadline)
            except Exception:
                closed = False
            if not closed:
                self._shutdown_blocked = True
                self._pending_error = "Nyx workers are still stopping; retry Close"
                self.snapshot = DesktopSnapshot(
                    "shutdown_blocked",
                    self.snapshot.configuration,
                    diagnostic=UNAVAILABLE_MESSAGE,
                )
                return
            self._application_runtime = None
            self._runtime_start_failed = False
        self._shutdown_blocked = False
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
            elif snapshot.status == "switch_blocked":
                self._status.setText(
                    self._session.pending_error
                    or "Workspace change is waiting for runtime cleanup."
                )
                self._root.setEnabled(False)
                self._change.setEnabled(False)
                self._save.setEnabled(False)
                self._retry.setEnabled(True)
            else:
                self._status.setText(
                    self._session.pending_error
                    or snapshot.diagnostic
                    or "Workspace configuration is unavailable."
                )
                self._root.setEnabled(False)
                self._change.setEnabled(False)
                self._save.setEnabled(False)
                self._retry.setEnabled(
                    self._session.unverified
                    or self._session.recovery_blocked
                    or self._session.runtime_retryable
                    or self._session.shutdown_blocked
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
                if self._session.switch_blocked:
                    self._session.retry_workspace_switch()
                elif self._session.shutdown_blocked:
                    self.close()
                    return
                if (
                    self._session.recovery_blocked
                    or self._session.runtime_retryable
                ):
                    self._session.retry_runtime()
                else:
                    self._session.revalidate()
            except DesktopError as error:
                self._status.setText(str(error))
            else:
                self._changing = False
            self._render()

        def closeEvent(self, event: Any) -> None:
            self._session.close()
            if self._session.shutdown_blocked or self._session.switch_in_progress:
                self._render()
                event.ignore()
            else:
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
        if session.snapshot.status == "configured":
            try:
                session.start_runtime()
            except DesktopError:
                # The same window owns the visible retry path for an incomplete
                # startup or cleanup; do not drop its claims in an error exit.
                pass
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
