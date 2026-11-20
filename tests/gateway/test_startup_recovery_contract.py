"""Shared-table parity for the startup session-recovery verdict.

``internal/startuprecovery`` (Go, canonical contract statement) and
``GatewayStartupMixin._recover_previous_run_sessions`` +
``gateway.lifecycle_ledger.previous_life_unclean`` (what the runtime runs)
must agree on every record of ``internal/startuprecovery/testdata/cases.json``.
Each row drives the real helper against a real session store and ledger file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.lifecycle_ledger import get_lifecycle_sentinel_path, previous_life_unclean
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore

_TABLE = Path(__file__).resolve().parents[2] / "internal" / "startuprecovery" / "testdata" / "cases.json"
_OWNER_PIDS = {"me": os.getpid(), "live_other": os.getppid(), "dead": 2 ** 22 + 12345}


def _cases():
    return json.loads(_TABLE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
async def test_python_decides_the_shared_table(case, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    if case["sentinel"] is not None:
        row = case["sentinel"]
        path = get_lifecycle_sentinel_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "phase": row["phase"], "pid": _OWNER_PIDS[row["owner"]],
            "prior_unclean_exit": row["prior_unclean_exit"],
        }), encoding="utf-8")
    marker = tmp_path / ".clean_shutdown"
    if case["clean_marker"]:
        marker.touch()
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    store._db = None
    source = SessionSource(platform=Platform.DISCORD, chat_id="c1", user_id="u1")
    key = store.get_or_create_session(source).session_key
    store.mark_turn_active(key)
    runner = object.__new__(GatewayRunner)
    runner.session_store = store

    assert previous_life_unclean() is case["expect_ledger_unclean"]
    await runner._recover_previous_run_sessions(marker)

    with store._lock:
        store._ensure_loaded_locked()
        entry = store._entries[key]
    assert ("recover" if entry.resume_pending else "discard") == case["expect_mode"]
    assert entry.active_turn_token is None
    assert not marker.exists()
