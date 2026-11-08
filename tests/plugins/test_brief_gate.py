"""Tests for the brief-gate plugin (``plugins/brief-gate/``).

The plugin registers a ``pre_tool_call`` hook that inspects
``delegate_agent`` / ``delegate_task`` calls and warns — loudly, via the
agent log — when a delegate brief is missing the dispatch-contract field
classes:

  * ``invariant``  — the observable condition that must hold
  * ``owner``      — what existing code owns that condition ("existing mechanism")
  * ``acceptance`` — the checks that prove the condition (tests, before/after)

The gate NEVER blocks a dispatch: it returns ``None`` from the hook so
the tool proceeds, and records one warning per missing-field class per
session (rate-limited).
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest
import yaml


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    yield hermes_home


# ---------------------------------------------------------------------------
# Module loading + scaffolding
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_plugin_init():
    """Import the plugin __init__.py as a standalone package."""
    plugin_dir = _repo_root() / "plugins" / "brief-gate"
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.brief_gate",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.brief_gate"
    mod.__path__ = [str(plugin_dir)]
    sys.modules["hermes_plugins.brief_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeCtx:
    """Minimal ctx surface for register(): hooks + config."""

    def __init__(self, settings=None):
        self.hooks: dict[str, object] = {}
        self._settings = settings or {}

    def register_hook(self, hook_name, callback):
        self.hooks[hook_name] = callback

    def get_config(self, key, default=None):
        return self._settings.get(key, default)


def _register(mod, settings=None):
    """register() against a fake ctx; return (ctx, hook)."""
    ctx = _FakeCtx(settings=settings)
    mod.register(ctx)
    hook = ctx.hooks.get("pre_tool_call")
    assert hook is not None, "register() must subscribe to pre_tool_call"
    return ctx, hook


_FULL_BRIEF = """\
/goal Fix the retry gate.

INVARIANT: the worker retries 429s exactly once, then surfaces the error.
EXISTING MECHANISM: tools/web_search.py owns the retry loop today.
ACCEPTANCE: tests/tools/test_web_search.py gains before/after cases; the
new tests fail on main and pass with the change.
"""

_MISSING_INVARIANT = """\
/goal Fix the retry gate.

Owner: tools/web_search.py owns the retry loop.
Tests: add before/after coverage in tests/tools/test_web_search.py.
"""

_MISSING_ACCEPTANCE = """\
/goal Fix the retry gate.

Invariant: the worker retries 429s exactly once, then surfaces the error.
Existing mechanism: tools/web_search.py owns the retry loop.
"""


# ---------------------------------------------------------------------------
# Hook behaviour — single-goal shape
# ---------------------------------------------------------------------------


class TestSingleGoalShape:
    def test_full_contract_brief_passes_silently(self, caplog):
        mod = _load_plugin_init()
        _, hook = _register(mod)
        with caplog.at_level(logging.WARNING):
            out = hook(tool_name="delegate_agent", args={"goal": _FULL_BRIEF}, session_id="s1")
        assert out is None, "the gate must never block a dispatch"
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []

    def test_missing_invariant_warns_once(self, caplog):
        mod = _load_plugin_init()
        _, hook = _register(mod)
        with caplog.at_level(logging.WARNING):
            out1 = hook(tool_name="delegate_agent", args={"goal": _MISSING_INVARIANT},
                        session_id="s1")
            out2 = hook(tool_name="delegate_agent", args={"goal": _MISSING_INVARIANT},
                        session_id="s1")
        assert out1 is None and out2 is None
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING
                    and "invariant" in r.getMessage().lower()]
        assert len(warnings) == 1, "rate-limit: one warning per missing class per session"

    def test_missing_acceptance_warns(self, caplog):
        mod = _load_plugin_init()
        _, hook = _register(mod)
        with caplog.at_level(logging.WARNING):
            hook(tool_name="delegate_agent", args={"goal": _MISSING_ACCEPTANCE}, session_id="s1")
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING
                    and "acceptance" in r.getMessage().lower()]
        assert len(warnings) == 1

    def test_delegate_task_alias_is_checked(self, caplog):
        mod = _load_plugin_init()
        _, hook = _register(mod)
        with caplog.at_level(logging.WARNING):
            hook(tool_name="delegate_task", args={"goal": _MISSING_INVARIANT}, session_id="s1")
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING
                    and "invariant" in r.getMessage().lower()]
        assert len(warnings) == 1

    def test_context_field_is_part_of_the_brief(self, caplog):
        """goal + context are concatenated: a contract field mentioned in
        ``context`` counts."""
        mod = _load_plugin_init()
        _, hook = _register(mod)
        args = {
            "goal": "/goal Fix the retry gate.\n\nOwner: tools/web_search.py. Tests: yes.",
            "context": "INVARIANT: 429s are retried exactly once.",
        }
        with caplog.at_level(logging.WARNING):
            hook(tool_name="delegate_agent", args=args, session_id="s1")
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING
                    and "invariant" in r.getMessage().lower()]
        assert warnings == []

    def test_rate_limit_is_scoped_per_session(self, caplog):
        """A different session gets its own warning budget."""
        mod = _load_plugin_init()
        _, hook = _register(mod)
        with caplog.at_level(logging.WARNING):
            hook(tool_name="delegate_agent", args={"goal": _MISSING_INVARIANT}, session_id="s1")
            hook(tool_name="delegate_agent", args={"goal": _MISSING_INVARIANT}, session_id="s2")
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING
                    and "invariant" in r.getMessage().lower()]
        assert len(warnings) == 2


# ---------------------------------------------------------------------------
# tasks=[...] batch shape
# ---------------------------------------------------------------------------


class TestTasksShape:
    def test_each_task_is_checked(self, caplog):
        mod = _load_plugin_init()
        _, hook = _register(mod)
        args = {
            "tasks": [
                {"goal": _FULL_BRIEF},
                {"goal": _MISSING_ACCEPTANCE},
                {"goal": _MISSING_INVARIANT},
            ]
        }
        with caplog.at_level(logging.WARNING):
            out = hook(tool_name="delegate_agent", args=args, session_id="s1")
        assert out is None
        texts = [r.getMessage().lower() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("acceptance" in t for t in texts)
        assert any("invariant" in t for t in texts)

    def test_task_context_counts(self, caplog):
        mod = _load_plugin_init()
        _, hook = _register(mod)
        args = {
            "tasks": [
                {
                    "goal": "Fix the retry gate. Owner: web_search. Tests: added.",
                    "context": "Invariant: 429 retried once.",
                }
            ]
        }
        with caplog.at_level(logging.WARNING):
            hook(tool_name="delegate_agent", args=args, session_id="s1")
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING
                    and "invariant" in r.getMessage().lower()]
        assert warnings == []

    def test_all_tasks_clean_is_silent(self, caplog):
        mod = _load_plugin_init()
        _, hook = _register(mod)
        args = {"tasks": [{"goal": _FULL_BRIEF}, {"goal": _FULL_BRIEF}]}
        with caplog.at_level(logging.WARNING):
            hook(tool_name="delegate_agent", args=args, session_id="s1")
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []


# ---------------------------------------------------------------------------
# Ignore non-delegate calls / malformed args
# ---------------------------------------------------------------------------


class TestIgnore:
    def test_non_delegate_tool_is_ignored(self, caplog):
        mod = _load_plugin_init()
        _, hook = _register(mod)
        with caplog.at_level(logging.WARNING):
            out = hook(tool_name="write_file", args={"path": "/tmp/x.py"}, session_id="s1")
        assert out is None
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []

    def test_missing_goal_and_tasks_is_ignored(self, caplog):
        mod = _load_plugin_init()
        _, hook = _register(mod)
        with caplog.at_level(logging.WARNING):
            out = hook(tool_name="delegate_agent", args={}, session_id="s1")
        assert out is None
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []

    def test_non_dict_args_is_ignored(self, caplog):
        mod = _load_plugin_init()
        _, hook = _register(mod)
        with caplog.at_level(logging.WARNING):
            out = hook(tool_name="delegate_agent", args="not-a-dict", session_id="s1")
        assert out is None
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []


# ---------------------------------------------------------------------------
# Config gates
# ---------------------------------------------------------------------------


class TestConfig:
    def test_disabled_via_plugin_settings_noops(self, caplog):
        """plugins.entries.brief-gate.settings.enabled: false → no warning."""
        mod = _load_plugin_init()
        _, hook = _register(mod, settings={"enabled": False})
        with caplog.at_level(logging.WARNING):
            out = hook(tool_name="delegate_agent", args={"goal": _MISSING_INVARIANT},
                       session_id="s1")
        assert out is None
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []

    def test_required_fields_can_narrow(self, caplog):
        """required_fields: [invariant] → missing acceptance no longer warns."""
        mod = _load_plugin_init()
        _, hook = _register(mod, settings={"required_fields": ["invariant"]})
        with caplog.at_level(logging.WARNING):
            hook(tool_name="delegate_agent", args={"goal": _MISSING_ACCEPTANCE},
                 session_id="s1")
            hook(tool_name="delegate_agent", args={"goal": _MISSING_INVARIANT},
                 session_id="s1")
        texts = [r.getMessage().lower() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("invariant" in t for t in texts)
        assert not any("acceptance" in t for t in texts)

    def test_flat_config_section_disables(self, tmp_path, monkeypatch, caplog):
        """plugins.brief_gate.enabled: false in config.yaml no-ops the hook
        even when the per-entry settings say nothing."""
        home = tmp_path / ".hermes"
        home.mkdir(exist_ok=True)
        (home / "config.yaml").write_text(
            yaml.safe_dump({"plugins": {"brief_gate": {"enabled": False}}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        mod = _load_plugin_init()
        _, hook = _register(mod)  # fake ctx has no settings → falls to file config
        with caplog.at_level(logging.WARNING):
            out = hook(tool_name="delegate_agent", args={"goal": _MISSING_INVARIANT},
                       session_id="s1")
        assert out is None
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []


# ---------------------------------------------------------------------------
# Real discovery through PluginManager (bundled, default-enabled)
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_bundled_default_enabled_loads_and_registers_hook(self, tmp_path, monkeypatch):
        from hermes_cli.plugins import PluginManager

        home = tmp_path / ".hermes"
        home.mkdir(exist_ok=True)
        (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": []}}),
                                          encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(_repo_root() / "plugins"))

        mgr = PluginManager()
        mgr.discover_and_load()
        loaded = mgr._plugins.get("brief-gate")
        assert loaded is not None, "brief-gate must be discovered under plugins/"
        assert loaded.enabled is True, "default_enabled: true must load without plugins.enabled"
        assert loaded.error is None
        assert mgr.has_hook("pre_tool_call")

    def test_explicit_disable_wins(self, tmp_path, monkeypatch):
        from hermes_cli.plugins import PluginManager

        home = tmp_path / ".hermes"
        home.mkdir(exist_ok=True)
        (home / "config.yaml").write_text(
            yaml.safe_dump({"plugins": {"enabled": [], "disabled": ["brief-gate"]}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(_repo_root() / "plugins"))

        mgr = PluginManager()
        mgr.discover_and_load()
        loaded = mgr._plugins["brief-gate"]
        assert loaded.enabled is False

    def test_hook_fires_through_real_dispatch(self, tmp_path, monkeypatch, caplog):
        """End-to-end: discovery, then invoke_hook('pre_tool_call', ...) warns
        on a brief missing the contract."""
        from hermes_cli import plugins as plugins_mod

        home = tmp_path / ".hermes"
        home.mkdir(exist_ok=True)
        (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": []}}),
                                          encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(_repo_root() / "plugins"))

        mgr = plugins_mod.PluginManager()
        monkeypatch.setattr(plugins_mod, "_delivery_manager", lambda: mgr)
        try:
            from hermes_cli import lifecycle as lifecycle_mod
            monkeypatch.setattr(lifecycle_mod, "_delivery_manager", lambda: mgr, raising=False)
        except Exception:
            pass
        mgr.discover_and_load()

        with caplog.at_level(logging.WARNING):
            results = mgr.invoke_hook(
                "pre_tool_call",
                tool_name="delegate_agent",
                args={"goal": _MISSING_INVARIANT},
                session_id="sess-e2e",
            )
        # The hook observes and returns None (never blocks).
        assert all(r is None for r in results)
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING
                    and "invariant" in r.getMessage().lower()]
        assert len(warnings) == 1
