"""Proofs for the ``--nyx-shard`` test selector in ``tests/conftest.py``.

The parse and select functions are checked in memory; the option, its
validation and its interaction with pytest's own filters are checked through
real pytest sessions run as subprocesses from the repository root.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import parse_shard, select_shard

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_FILE = "tests/test_sample_specifications.py"
SUMMARY = re.compile(r"(?:(\d+)/)?(\d+) tests? collected(?: \((\d+) deselected\))?")


def _collect(*arguments: str, hash_seed: str | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTEST_ADDOPTS", None)
    if hash_seed is not None:
        environment["PYTHONHASHSEED"] = hash_seed
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *arguments],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _ids(completed: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in completed.stdout.splitlines() if "::" in line]


def _summary(completed: subprocess.CompletedProcess[str]) -> tuple[int, int]:
    """Return ``(collected, deselected)`` from the collection summary line.

    Pytest prints ``selected/collected tests collected (K deselected)`` when it
    deselects and ``collected tests collected`` otherwise.
    """

    match = SUMMARY.search(completed.stdout)
    assert match is not None, completed.stdout[-1500:]
    return int(match.group(2)), int(match.group(3) or 0)


@pytest.mark.parametrize("total", range(1, 7))
def test_every_shard_of_a_list_is_its_strided_slice_and_the_shards_partition_it(total) -> None:
    for count in sorted({0, 1, total - 1, total, total + 1, 2 * total + 1, 37}):
        items = [f"item-{position}" for position in range(count)]
        shards = []
        for index in range(total):
            selected, deselected = select_shard(items, index, total)
            assert selected == items[index::total]
            assert sorted(selected + deselected, key=items.index) == items
            assert not set(selected) & set(deselected)
            shards.append(selected)
        assert sorted(item for shard in shards for item in shard) == sorted(items)
        assert max(map(len, shards)) - min(map(len, shards)) <= 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0/1", (0, 1)), ("1/3", (1, 3)), ("11/12", (11, 12))],
)
def test_valid_shard_values_parse_to_index_and_total(value, expected) -> None:
    assert parse_shard(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "2/2", "1/0", "01/2", "1/02", "-1/2", " 0/2", "0/2 ", "a/b", "0//2"],
)
def test_malformed_shard_values_raise_a_usage_error_naming_option_and_value(value) -> None:
    with pytest.raises(pytest.UsageError) as raised:
        parse_shard(value)
    assert "--nyx-shard" in str(raised.value)
    assert repr(value) in str(raised.value)


@pytest.mark.parametrize("value", ["2/2", ""])
def test_a_malformed_option_on_the_command_line_stops_the_session_before_collection(
    value,
) -> None:
    completed = _collect(SAMPLE_FILE, f"--nyx-shard={value}")
    assert completed.returncode == 4, completed.stdout + completed.stderr
    report = completed.stdout + completed.stderr
    assert "--nyx-shard" in report
    assert repr(value) in report
    assert "::" not in completed.stdout


def test_the_selector_runs_after_deselect_and_reports_the_rest_as_deselected() -> None:
    everything = _collect(SAMPLE_FILE, "--collect-only", "-q")
    assert everything.returncode == 0, everything.stderr
    ids = _ids(everything)
    assert len(ids) > 2
    assert _summary(everything) == (len(ids), 0)

    sharded = _collect(
        SAMPLE_FILE, "--deselect", ids[0], "--nyx-shard=0/2", "--collect-only", "-q"
    )
    assert sharded.returncode == 0, sharded.stderr
    expected = ids[1:][0::2]
    assert _ids(sharded) == expected
    assert _summary(sharded) == (len(ids), len(ids) - len(expected))


def test_two_processes_with_different_hash_seeds_shard_the_whole_suite_identically() -> None:
    everything = _collect("tests", "--collect-only", "-q", hash_seed="1")
    assert everything.returncode == 0, everything.stderr
    universe = _ids(everything)
    assert universe
    assert _summary(everything) == (len(universe), 0)

    shard = _collect("tests", "--nyx-shard=1/2", "--collect-only", "-q", hash_seed="2")
    assert shard.returncode == 0, shard.stderr
    assert _ids(shard) == universe[1::2]
    assert _summary(shard) == (len(universe), math.ceil(len(universe) / 2))


def test_the_conftest_docstring_documents_the_option() -> None:
    import conftest

    text = " ".join((conftest.__doc__ or "").split())
    assert "--nyx-shard=I/N" in text
    assert "0-based" in text
    assert "after ``-k``, ``-m`` and ``--deselect``" in text
    assert "malformed value" in text and "stops the session" in text
    assert "Without the option nothing is deselected" in text
