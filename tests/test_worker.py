"""Behavioral checks for installed worker configuration transport."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from nyx import state, worker


class _Buffer:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.flush_count = 0

    def write(self, value: bytes) -> int:
        self.writes.append(value)
        return len(value)

    def flush(self) -> None:
        self.flush_count += 1


class _Stdout:
    def __init__(self) -> None:
        self.buffer = _Buffer()


def test_worker_main_loads_once_and_transports_configuration_by_identity() -> None:
    root = Path("/sentinel/specification-root")
    hidden_stages = tuple(["Done", "In_Progress"])
    configuration = state.Configuration(root, hidden_stages)
    load_count = 0
    scanner_arguments: list[tuple[object, object]] = []
    stdout = _Stdout()

    def load_configuration() -> state.Configuration:
        nonlocal load_count
        load_count += 1
        return configuration

    def scan_catalog(received_root: object, *, hidden_stages: object) -> str:
        scanner_arguments.append((received_root, hidden_stages))
        return "catalog ✓"

    with (
        patch.object(worker.state, "load_configuration", side_effect=load_configuration),
        patch.object(worker, "scan_catalog", side_effect=scan_catalog),
        patch.object(worker.sys, "stdout", stdout),
    ):
        assert worker._worker_main() == 0

    assert load_count == 1
    assert len(scanner_arguments) == 1
    received_root, received_hidden_stages = scanner_arguments[0]
    assert received_root is root
    assert received_hidden_stages is hidden_stages
    assert stdout.buffer.writes == ["catalog ✓".encode("utf-8")]
    assert stdout.buffer.flush_count == 1
