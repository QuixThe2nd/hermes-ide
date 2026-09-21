"""Desktop/TUI sessions must adopt a ``fallback_providers`` chain added after the chat was opened.

Regression for #95066: ``_make_agent`` read the chain once, so a session born before ``hermes fallback
add`` kept an empty ``_fallback_chain`` forever and a Codex ``usage_limit_reached`` 429 ended in a
provider error instead of switching to the configured fallback.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from tui_gateway import server


def _session(chain=None):
    agent = SimpleNamespace(
        _fallback_chain=list(chain or []), _fallback_model=None, _fallback_index=0,
        _fallback_activated=False, _rate_limited_until=0, _unavailable_fallback_keys=set(),
    )
    return {"agent": agent, "session_key": "session-95066"}, agent


def test_chain_added_after_open_reaches_the_live_agent(monkeypatch):
    session, agent = _session()
    fallback = [{"provider": "xai-oauth", "model": "grok-4.6"}]
    monkeypatch.setattr(server, "_load_cfg", lambda: {"fallback_providers": fallback})

    server._sync_agent_fallback_with_config("sid", session)

    assert agent._fallback_chain == fallback
    assert agent._fallback_model == fallback[0]
    assert agent._fallback_index == 0


def test_turn_admission_syncs_fallback_chain_before_running():
    # Wiring guard: the sync must run on the turn thread right beside the model/compression syncs.
    from tui_gateway import prompt_turn

    source = inspect.getsource(prompt_turn)
    sync_idx = source.find("_sync_agent_fallback_with_config(sid, session)")
    assert sync_idx > 0
    assert source.find("_sync_agent_compression_with_config(sid, session)") < sync_idx < source.find("st.agent = agent = session[\"agent\"]")
