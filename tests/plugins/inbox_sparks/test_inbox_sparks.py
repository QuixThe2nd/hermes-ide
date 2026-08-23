"""Tests for the bundled inbox_sparks plugin (pre_turn_end gate).

Covers the handler's cooldown rules, the ``pre_turn_end`` directive
aggregation in ``hermes_cli.plugins`` (malformed results ignored), and the
``_pre_turn_end_synthetic`` marker registrations that keep the nudge out of
durable transcripts — mirroring the pre_verify marker tests in
``tests/agent/test_verification_stop_caching.py``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    yield hermes_home


@pytest.fixture
def inbox_sparks_module():
    repo_root = Path(__file__).resolve().parents[3]
    plugin_dir = repo_root / "plugins" / "inbox_sparks"
    module_name = "inbox_sparks_plugin_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, plugin_dir / "__init__.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _state_path(hermes_home: Path) -> Path:
    return hermes_home / "inbox_sparks" / "state.json"


def _seed_state(hermes_home: Path, timestamp: float) -> None:
    path = _state_path(hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_directive_at": timestamp}), encoding="utf-8")


class TestDirectiveWithinCooldownRules:
    def test_first_directive_issued_and_state_persisted(self, inbox_sparks_module, _isolate_env):
        before = time.time()
        result = inbox_sparks_module._handle_pre_turn_end(cooldown_minutes=240)

        assert isinstance(result, dict)
        assert result["action"] == "continue"
        assert result["message"] == inbox_sparks_module.DIRECTIVE
        state = json.loads(_state_path(_isolate_env).read_text(encoding="utf-8"))
        assert before <= state["last_directive_at"] <= time.time()

    def test_state_file_mode_0600(self, inbox_sparks_module, _isolate_env):
        inbox_sparks_module._handle_pre_turn_end(cooldown_minutes=240)
        assert _state_path(_isolate_env).exists()
        mode = _state_path(_isolate_env).stat().st_mode & 0o777
        assert mode == 0o600

    def test_silent_inside_cooldown_window(self, inbox_sparks_module, _isolate_env):
        _seed_state(_isolate_env, time.time() - 60)  # one minute ago
        assert inbox_sparks_module._handle_pre_turn_end(cooldown_minutes=240) is None
        # Silence must not re-stamp the window.
        state = json.loads(_state_path(_isolate_env).read_text(encoding="utf-8"))
        assert state["last_directive_at"] <= time.time() - 59

    def test_directive_again_after_window_expires(self, inbox_sparks_module, _isolate_env):
        _seed_state(_isolate_env, time.time() - 241 * 60)
        result = inbox_sparks_module._handle_pre_turn_end(cooldown_minutes=240)
        assert result is not None and result["action"] == "continue"

    def test_zero_cooldown_disables_rate_limit(self, inbox_sparks_module, _isolate_env):
        _seed_state(_isolate_env, time.time())
        result = inbox_sparks_module._handle_pre_turn_end(cooldown_minutes=0)
        assert result is not None and result["action"] == "continue"

    def test_corrupt_state_treated_as_never_issued(self, inbox_sparks_module, _isolate_env):
        _state_path(_isolate_env).parent.mkdir(parents=True, exist_ok=True)
        _state_path(_isolate_env).write_text("not json", encoding="utf-8")
        result = inbox_sparks_module._handle_pre_turn_end(cooldown_minutes=240)
        assert result is not None and result["action"] == "continue"

    def test_directive_message_contract(self, inbox_sparks_module):
        message = inbox_sparks_module.DIRECTIVE
        assert "start_conversation" in message
        assert "zero or more times" in message
        assert "mildly interesting" in message
        assert "do nothing" in message

    def test_handler_never_raises_on_state_write_failure(self, inbox_sparks_module, monkeypatch):
        def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(inbox_sparks_module, "_write_last_directive_at", _boom)
        # A failed stamp must NOT yield an unpersisted directive.
        assert inbox_sparks_module._handle_pre_turn_end(cooldown_minutes=240) is None

    def test_handler_never_raises_on_cooldown_check_failure(self, inbox_sparks_module, monkeypatch):
        def _boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(inbox_sparks_module, "_cooldown_active", _boom)
        assert inbox_sparks_module._handle_pre_turn_end(cooldown_minutes=240) is None

    def test_handler_is_fast_on_silent_path(self, inbox_sparks_module, _isolate_env):
        _seed_state(_isolate_env, time.time())
        start = time.perf_counter()
        for _ in range(100):
            assert inbox_sparks_module._handle_pre_turn_end(cooldown_minutes=240) is None
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0  # 100 cold-path probes; each must stay trivial


class TestCooldownResolution:
    def test_default(self, inbox_sparks_module):
        assert inbox_sparks_module._resolve_cooldown_minutes(None) == 240

    def test_plugin_config_override(self, inbox_sparks_module):
        class _Ctx:
            def get_config(self, key, default=None):
                assert key == "cooldown_minutes"
                return 15

        assert inbox_sparks_module._resolve_cooldown_minutes(_Ctx()) == 15

    def test_plugin_config_accepts_string_values(self, inbox_sparks_module):
        class _Ctx:
            def get_config(self, key, default=None):
                return "30"

        assert inbox_sparks_module._resolve_cooldown_minutes(_Ctx()) == 30

    def test_ctx_without_get_config_uses_default(self, inbox_sparks_module):
        class _Ctx:
            pass

        assert inbox_sparks_module._resolve_cooldown_minutes(_Ctx()) == 240

    def test_bad_values_fall_back_to_default(self, inbox_sparks_module):
        class _BadValueCtx:
            def get_config(self, key, default=None):
                return "soon"

        assert inbox_sparks_module._resolve_cooldown_minutes(_BadValueCtx()) == 240

        class _BadCtx:
            def get_config(self, key, default=None):
                raise RuntimeError("config unavailable")

        assert inbox_sparks_module._resolve_cooldown_minutes(_BadCtx()) == 240


class TestRegister:
    def _capture_ctx(self):
        captured: Dict[str, Any] = {}

        class _Ctx:
            def register_hook(self, hook_name, callback):
                captured["hook"] = hook_name
                captured["callback"] = callback

            def get_config(self, key, default=None):
                return default

        return _Ctx(), captured

    def test_subscribes_pre_turn_end(self, inbox_sparks_module):
        ctx, captured = self._capture_ctx()
        inbox_sparks_module.register(ctx)
        assert captured["hook"] == "pre_turn_end"
        result = captured["callback"]()
        assert result is not None and result["action"] == "continue"

    def test_cooldown_read_at_register_time(self, inbox_sparks_module, _isolate_env):
        settings = {"cooldown_minutes": 240}
        captured: Dict[str, Any] = {}

        class _Ctx:
            def register_hook(self, hook_name, callback):
                captured["callback"] = callback

            def get_config(self, key, default=None):
                return settings.get(key, default)

        inbox_sparks_module.register(_Ctx())
        settings["cooldown_minutes"] = 1  # later config changes must not apply

        _seed_state(_isolate_env, time.time() - 120)  # outside a 1-minute window
        assert captured["callback"]() is None  # ...but inside the register-time 240

    def test_discover_via_plugin_manager(self, _isolate_env):
        for key in list(sys.modules):
            if key.startswith(("plugins.inbox_sparks", "hermes_cli.plugins")):
                del sys.modules[key]

        from hermes_cli.plugins import PluginManager

        mgr = PluginManager()
        mgr.discover_and_load(force=True)

        assert "inbox_sparks" in mgr._plugins
        loaded = mgr._plugins["inbox_sparks"]
        assert loaded.enabled is True
        assert loaded.error is None
        # default_enabled: true — bundled and on with no plugins.enabled entry.
        assert mgr.has_hook("pre_turn_end")


class TestPreTurnEndDispatcher:
    """``hermes_cli.plugins.get_pre_turn_end_continue_message`` — mirrors the
    pre_verify aggregation tests in tests/hermes_cli/test_plugins.py."""

    def _get(self):
        from hermes_cli.plugins import get_pre_turn_end_continue_message

        return get_pre_turn_end_continue_message

    def test_none_when_no_hooks(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda hook_name, **kwargs: [])
        assert self._get()() is None

    def test_malformed_results_ignored(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.plugins.invoke_hook",
            lambda hook_name, **kwargs: [
                "garbage",
                42,
                {"action": "bogus", "message": "x"},
                {"action": "continue", "message": "   "},
                {"decision": "unblock", "reason": "x"},
            ],
        )
        assert self._get()() is None

    def test_first_valid_directive_wins(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.plugins.invoke_hook",
            lambda hook_name, **kwargs: [
                {"action": "continue", "message": "first"},
                {"decision": "block", "reason": "second"},
            ],
        )
        assert self._get()() == "first"

    def test_claude_stop_shape_accepted(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.plugins.invoke_hook",
            lambda hook_name, **kwargs: [{"decision": "block", "reason": "keep going"}],
        )
        assert self._get()() == "keep going"

    def test_forwards_scope_signals_to_hooks(self, monkeypatch):
        seen: Dict[str, Any] = {}

        def capture(hook_name, **kwargs):
            seen["hook"] = hook_name
            seen.update(kwargs)
            return []

        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", capture)
        self._get()(
            session_id="s1",
            platform="discord",
            model="m1",
            attempt=1,
            final_response="done",
            last_user_text="hello there",
        )
        assert seen["hook"] == "pre_turn_end"
        assert seen["session_id"] == "s1"
        assert seen["platform"] == "discord"
        assert seen["attempt"] == 1
        assert seen["final_response"] == "done"
        assert seen["last_user_text"] == "hello there"


class TestSyntheticMarkerStripping:
    """The ``_pre_turn_end_synthetic`` nudge must never reach durable
    transcripts — mirrors tests/agent/test_verification_stop_caching.py."""

    def test_registered_in_ephemeral_scaffolding_flags(self):
        import run_agent as ra

        assert "_pre_turn_end_synthetic" in ra._EPHEMERAL_SCAFFOLDING_FLAGS
        assert ra._is_ephemeral_scaffolding(
            {"role": "user", "content": "consider start_conversation",
             "_pre_turn_end_synthetic": True}
        )
        assert not ra._is_ephemeral_scaffolding({"role": "assistant", "content": "answer"})

    def test_registered_in_finalizer_continuation_flags(self):
        from agent.turn_finalizer import (
            _VERIFICATION_CONTINUATION_FLAGS,
            _drop_verification_continuation_scaffolding,
        )

        assert "_pre_turn_end_synthetic" in _VERIFICATION_CONTINUATION_FLAGS
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "attempted answer"},
            {"role": "user", "content": "consider start_conversation",
             "_pre_turn_end_synthetic": True},
            {"role": "assistant", "content": "final answer"},
        ]
        _drop_verification_continuation_scaffolding(messages)
        assert [m["content"] for m in messages] == [
            "hi", "attempted answer", "final answer",
        ]

    def test_registered_in_compression_synthetic_user_flags(self):
        from agent.conversation_compression import _SYNTHETIC_USER_FLAGS, _is_real_user_message

        assert "_pre_turn_end_synthetic" in _SYNTHETIC_USER_FLAGS
        assert not _is_real_user_message(
            {"role": "user", "content": "consider start_conversation",
             "_pre_turn_end_synthetic": True}
        )
        assert _is_real_user_message({"role": "user", "content": "real question"})

    def test_shell_hook_parser_accepts_pre_turn_end(self):
        from agent.shell_hooks import _parse_response

        parsed = _parse_response("pre_turn_end", '{"decision": "block", "reason": "weigh in"}')
        assert parsed == {"action": "continue", "message": "weigh in"}
        assert _parse_response("pre_turn_end", '{"action": "continue"}') is None
        assert _parse_response("pre_turn_end", "not json") is None
