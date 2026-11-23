"""Shared-contract parity for persistence-error classification.

``internal/stateerrors`` (Go, canonical contract statement) and
``hermes_state_errors.py`` (the module the runtime executes) must classify
identically: the typed-error ladder, FTS-scoping provenance, the ordered
phrase table, the disk bucket, and every type gate on the diagnostic
predicates. The shared table
(``internal/stateerrors/testdata/classify_cases.json``) is replayed through
the real Python predicates with real exception instances, and — when a Go
toolchain is available — through the built ``cmd/stateerrors`` binary, so
Python == table == Go. Production Python never shells out to the binary.
"""

from __future__ import annotations

import errno
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

import hermes_state_errors as stateerrors

_REPO = Path(__file__).resolve().parents[2]
_TABLE = _REPO / "internal" / "stateerrors" / "testdata" / "classify_cases.json"
_GO_DIR = _REPO / "internal" / "stateerrors"


def _shared_cases():
    return json.loads(_TABLE.read_text(encoding="utf-8"))["cases"]


# kind tag -> the real Python exception class the table entry stands for.
# Building the actual classes (not mocks) is what makes the isinstance
# ladder and the DatabaseError-family gates mean anything.
_TYPED = {
    "turn_lease": stateerrors.SessionTurnLeaseLostError,
    "compression_closed": stateerrors.CompressionSessionClosedError,
    "compression_busy": stateerrors.CompressionSessionBusyError,
    "compression_in_progress": stateerrors.SessionCompressionInProgressError,
    "replaced": stateerrors.StateDbReplacedError,
    "deleted_wal": stateerrors.DeletedWalGenerationError,
    "corrupt": stateerrors.StateDbCorruptError,
}


def _build(spec: dict):
    """Construct the real error object one shared-table case describes."""
    kind, text = spec["kind"], spec.get("text", "")
    if kind == "none":
        return None
    if kind == "string":
        exc = text
    elif kind == "operational_error":
        exc = sqlite3.OperationalError(text)
    elif kind == "database_error":
        exc = sqlite3.DatabaseError(text)
    elif kind == "os_error":
        # errno first: str() gains an "[Errno N] " prefix, which the marker
        # checks tolerate; the ENOSPC shortcut does not depend on wording.
        exc = OSError(errno.ENOSPC, text) if spec.get("enospc") else OSError(text)
    elif kind == "exception":
        exc = Exception(text)
    else:
        exc = _TYPED[kind](text)
    if "sqlite_errorcode" in spec and not isinstance(exc, str):
        # SQLite attaches this attribute when it raises (3.11+); setting it
        # directly reproduces that shape for a table-driven case.
        exc.sqlite_errorcode = spec["sqlite_errorcode"]  # type: ignore[attr-defined]
    return exc


def _python_outputs(exc) -> dict:
    return {
        "classify": stateerrors.classify_persistence_error(exc),
        "transient": stateerrors.is_transient_sqlite_error(exc),
        "malformed_db": stateerrors.is_malformed_db_error(exc),
        "malformed_schema": stateerrors.is_malformed_schema_error(exc),
        "disk_full": stateerrors.is_disk_full_error(exc),
        "fts_scoped": stateerrors.is_fts_scoped_corruption_error(exc),
    }


@pytest.fixture(scope="session")
def go_stateerrors(tmp_path_factory):
    """Build cmd/stateerrors once per session; None when no Go toolchain."""
    if shutil.which("go") is None:
        return None
    binary = tmp_path_factory.mktemp("stateerrors", numbered=True) / "stateerrors"
    build = subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/stateerrors"],
        cwd=_GO_DIR,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if build.returncode != 0:
        pytest.fail(f"go build cmd/stateerrors failed:\n{build.stderr}")
    return str(binary)


@pytest.mark.parametrize("case", _shared_cases(), ids=lambda c: c["name"])
def test_python_classification_matches_the_shared_table(case):
    assert _python_outputs(_build(case["error"])) == case["want"]


def test_go_binary_classification_matches_python_and_the_table(go_stateerrors):
    if go_stateerrors is None:
        pytest.skip("no Go toolchain available")
    run = subprocess.run(
        [go_stateerrors],
        input=_TABLE.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr
    results = {entry["name"]: entry for entry in json.loads(run.stdout)["results"]}
    cases = _shared_cases()
    assert set(results) == {case["name"] for case in cases}
    for case in cases:
        got = results[case["name"]]
        python = _python_outputs(_build(case["error"]))
        for key, want in case["want"].items():
            assert got[key] == want, f"{case['name']}.{key}: go={got[key]} want={want}"
            assert python[key] == want, f"{case['name']}.{key}: python={python[key]} want={want}"


def test_go_binary_rejects_unknown_kind(go_stateerrors):
    if go_stateerrors is None:
        pytest.skip("no Go toolchain available")
    bad = {"cases": [{"name": "bad", "error": {"kind": "explode", "text": "x"}}]}
    run = subprocess.run(
        [go_stateerrors],
        input=json.dumps(bad),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 2
    assert "unknown error kind" in run.stderr


def test_shared_table_produces_every_cause():
    """Every bucket in PERSISTENCE_ERROR_CAUSES must be reachable through the
    shared table, and the table must not invent buckets outside the tuple."""
    produced = {case["want"]["classify"] for case in _shared_cases()}
    assert produced == set(stateerrors.PERSISTENCE_ERROR_CAUSES)


def test_shared_table_covers_the_precedence_rules():
    """The ordering rules the contract stands on each have a named case."""
    names = {case["name"] for case in _shared_cases()}
    for required in (
        "corruption_phrase_beats_disk_fallback",
        "fts_vtab_code_outranks_generic_prose",
        "known_code_contradicts_fts_prose",
        "fts_object_name_alone_is_not_corruption",
        "typed_deleted_wal_beats_replaced_parent",
        "typed_compression_in_progress_collapses_to_busy",
        "typed_corrupt_beats_fts_code",
        "phrase_turn_lease_beats_locked",
        "phrase_compression_closed_beats_busy",
        "string_busy_is_not_transient",
        "string_malformed_image_not_diagnosed",
        "os_error_enospc_shortcut",
    ):
        assert required in names, f"shared table lost the {required} contract case"
