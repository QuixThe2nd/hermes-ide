"""
Regression tests for the shared-container task_id mapping.

The top-level agent and all delegate_agent subagents share a single
terminal sandbox keyed by ``"default"``.  ``_resolve_container_task_id``
is the sole gatekeeper for which tool-call task_ids go to the shared
container vs. get their own isolated sandbox.  RL / benchmark
environments opt in to isolation by calling
``register_task_env_overrides(task_id, {...})`` before the agent loop;
every other task_id collapses back to ``"default"``.

If you change the collapse logic, update both the helper and these
tests -- see `hermes-agent-dev` skill, "Why do subagents get their own
containers?" section.
"""

import pytest

from agent.terminal_env_provider import TerminalEnvironmentProvider
from tools import terminal_tool


@pytest.fixture(autouse=True)
def _clean_overrides():
    """Ensure no stray overrides from other tests leak in."""
    before = dict(terminal_tool._task_env_overrides)
    terminal_tool._task_env_overrides.clear()
    yield
    terminal_tool._task_env_overrides.clear()
    terminal_tool._task_env_overrides.update(before)


def test_none_task_id_maps_to_default():
    assert terminal_tool._resolve_container_task_id(None) == "default"


def test_empty_task_id_maps_to_default():
    assert terminal_tool._resolve_container_task_id("") == "default"


def test_cwd_only_override_collapses_to_default():
    """CWD-only overrides (ACP adapter workspace tracking) must NOT trigger
    container isolation — they should collapse to the shared 'default'
    container so all surfaces (TUI, gateway, dashboard) share one sandbox.
    Regression for #37361."""
    terminal_tool.register_task_env_overrides(
        "acp-session-abc", {"cwd": "/home/user/project"}
    )
    try:
        assert (
            terminal_tool._resolve_container_task_id("acp-session-abc")
            == "default"
        )
    finally:
        terminal_tool.clear_task_env_overrides("acp-session-abc")


def test_env_type_override_keeps_own_id():
    """env_type is an isolation key — must trigger per-task container."""
    terminal_tool.register_task_env_overrides(
        "bench-env", {"env_type": "sandbox", "cwd": "/work"}
    )
    try:
        assert (
            terminal_tool._resolve_container_task_id("bench-env")
            == "bench-env"
        )
    finally:
        terminal_tool.clear_task_env_overrides("bench-env")


# --- Cross-profile SSH-leak isolation (commit e00f940a9, re-applied) ---------
#
# When a session key is present (WebUI/gateway), each session must own its own
# slot in _active_environments so switching from profile A (ssh_host=10.0.0.1)
# to profile B (ssh_host=10.0.0.2) cannot reuse A's SSHEnvironment. Without this
# the shared "default" slot silently runs commands on the wrong remote host.


def test_session_key_scopes_to_its_own_slot(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_KEY", "sess-A")
    assert terminal_tool._resolve_container_task_id(None) == "session:sess-A"


def test_distinct_session_keys_get_distinct_slots(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_KEY", "sess-A")
    a = terminal_tool._resolve_container_task_id(None)
    monkeypatch.setenv("HERMES_SESSION_KEY", "sess-B")
    b = terminal_tool._resolve_container_task_id(None)
    assert a == "session:sess-A"
    assert b == "session:sess-B"
    assert a != b


def test_subagent_collapses_onto_parent_session(monkeypatch):
    # Subagents inherit the parent's session key, so they share the parent's
    # container (the #16177 intent) rather than a global "default".
    monkeypatch.setenv("HERMES_SESSION_KEY", "sess-A")
    assert (
        terminal_tool._resolve_container_task_id("subagent-3-cafef00d")
        == "session:sess-A"
    )


def test_rl_override_wins_over_session_key(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_KEY", "sess-A")
    terminal_tool.register_task_env_overrides("tb2-z", {"image": "z:1"})
    try:
        assert terminal_tool._resolve_container_task_id("tb2-z") == "tb2-z"
    finally:
        terminal_tool.clear_task_env_overrides("tb2-z")


def test_retired_backend_image_override_key_does_not_isolate(monkeypatch):
    # The backend-specific override keys (docker_image and siblings) were
    # retired with their backends (c7442dd1745, #708); only the generic
    # ``image``/``env_type`` keys pin a per-task sandbox. Leftover RL-harness
    # registrations of the old keys must not resurrect isolation.
    monkeypatch.setenv("HERMES_SESSION_KEY", "sess-A")
    terminal_tool.register_task_env_overrides("tb2-legacy", {"docker_image": "z:1"})
    try:
        assert terminal_tool._resolve_container_task_id("tb2-legacy") == "session:sess-A"
    finally:
        terminal_tool.clear_task_env_overrides("tb2-legacy")


def test_no_session_key_still_defaults(monkeypatch):
    # CLI mode: no session key -> unchanged "default" behaviour.
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    assert terminal_tool._resolve_container_task_id(None) == "default"


# --- Production gateway path: session key bound via ContextVars ---------------
#
# The tests above set HERMES_SESSION_KEY through os.environ, which only
# exercises the os.getenv() *fallback* branch of the scoping logic. Real
# gateway turns never write this process-global env var — they bind the
# identity through gateway.session_context.set_session_vars(), which stores it
# in a ContextVar, and _resolve_container_task_id reads it back via
# get_session_env(). These companion tests cover that production path with
# HERMES_SESSION_KEY absent from os.environ.


def test_session_key_from_contextvar_without_environ(monkeypatch):
    # Prove the fix works on the gateway path: HERMES_SESSION_KEY is NOT in
    # os.environ; the key lives only in the ContextVar bound by the gateway.
    from gateway.session_context import clear_session_vars, set_session_vars

    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    tokens = set_session_vars(session_key="sess-ctx")
    try:
        assert (
            terminal_tool._resolve_container_task_id(None) == "session:sess-ctx"
        )
        # Subagents inherit the same ContextVar and collapse onto the parent.
        assert (
            terminal_tool._resolve_container_task_id("subagent-1-cafe")
            == "session:sess-ctx"
        )
    finally:
        clear_session_vars(tokens)


def test_contextvar_session_key_wins_over_environ(monkeypatch):
    # Two concurrent gateway sessions in one process must not cross-contaminate:
    # the ContextVar is authoritative even when a *different* value lingers in
    # os.environ (e.g. a CLI-set or previously-leaked global). The container
    # slot must follow the ContextVar-bound session, not the process global.
    from gateway.session_context import clear_session_vars, set_session_vars

    monkeypatch.setenv("HERMES_SESSION_KEY", "sess-ENV")
    tokens = set_session_vars(session_key="sess-CTX")
    try:
        assert (
            terminal_tool._resolve_container_task_id(None) == "session:sess-CTX"
        )
    finally:
        clear_session_vars(tokens)


# --- Retired built-in docker knobs no longer reshape identity -----------------
#
# The built-in docker backend — and with it the profile-scoped persistent
# container and the trusted-profiles shared-container key — was removed
# (c7442dd1745, #708). Leftover TERMINAL_ENV=docker / TERMINAL_CONTAINER_
# PERSISTENT / TERMINAL_DOCKER_SHARED_CONTAINER_KEY values in a user's env or
# config must not reintroduce cross-session sharing: identity stays
# session-scoped (the SSH-leak isolation) or "default" (CLI), and no
# "profile:" / "shared:" keys are minted.


def _docker_residue(monkeypatch, persistent="true", shared_key=None):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", persistent)
    if shared_key is not None:
        monkeypatch.setenv("TERMINAL_DOCKER_SHARED_CONTAINER_KEY", shared_key)


def test_docker_residue_keeps_session_scoping(monkeypatch):
    # A gateway session stays in its own slot even with persistent-docker and
    # shared-key residue: no collapse to "default", no "profile:"/"shared:" key.
    from gateway.session_context import clear_session_vars, set_session_vars

    _docker_residue(monkeypatch, shared_key="team/workspace")
    tokens = set_session_vars(session_key="agent:main:discord:dm:123", profile="work")
    try:
        key = terminal_tool._resolve_container_task_id(None)
    finally:
        clear_session_vars(tokens)
    assert key == "session:agent:main:discord:dm:123"


def test_docker_residue_cli_lands_on_default(monkeypatch):
    # CLI (no session key) lands on "default"; the shared-container opt-in is
    # gone with the backend, so no "shared:" unification happens.
    _docker_residue(monkeypatch, shared_key="team/workspace")
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    assert terminal_tool._resolve_container_task_id(None) == "default"


def test_docker_residue_never_keys_by_profile(monkeypatch):
    # Two sessions of one profile and one of another each keep their own
    # session slot — nothing keys by profile under docker residue.
    from gateway.session_context import clear_session_vars, set_session_vars

    _docker_residue(monkeypatch)
    keys = []
    for session_key, profile in (("s1", "work"), ("s2", "work"), ("s3", "research")):
        tokens = set_session_vars(session_key=session_key, profile=profile)
        try:
            keys.append(terminal_tool._resolve_container_task_id(None))
        finally:
            clear_session_vars(tokens)
    assert keys == ["session:s1", "session:s2", "session:s3"]


def test_nonpersistent_docker_residue_keeps_session_scoping(monkeypatch):
    # Same inertness with the persistence knob off: no plugin backend declares
    # per-session isolation for the retired name, so session scoping holds.
    from gateway.session_context import clear_session_vars, set_session_vars

    _docker_residue(monkeypatch, persistent="false")
    tokens = set_session_vars(session_key="sess-A", profile="work")
    try:
        assert terminal_tool._resolve_container_task_id(None) == "session:sess-A"
    finally:
        clear_session_vars(tokens)


def test_ssh_backend_keeps_session_scoping(monkeypatch):
    # The original a270c4ade leak: SSH environments must stay session-scoped
    # regardless of profile, or profile A's SSHEnvironment leaks into B.
    from gateway.session_context import clear_session_vars, set_session_vars

    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    tokens = set_session_vars(session_key="sess-A", profile="work")
    try:
        assert terminal_tool._resolve_container_task_id(None) == "session:sess-A"
    finally:
        clear_session_vars(tokens)


# --- Per-session isolation for plugin backends ---------------------------------
#
# A plugin backend declaring ``session_isolated_when_nonpersistent`` gets
# per-task identities when the persistence knob is off — the surviving home of
# the #82731 "non-persistent means not shared" contract. Persistent mode (the
# default) keeps the shared slot so subagents and sessions share one sandbox.


class _DummySandbox(TerminalEnvironmentProvider):
    """Minimal registered provider used to drive the classification flags."""

    name = "dummysbx"
    is_container = True
    session_isolated_when_nonpersistent = True

    def is_available(self):
        return True

    def create_environment(self, *, cwd, timeout, task_id="default", **kwargs):
        raise AssertionError("classification-only provider; never instantiated")


class _PlainSandbox(_DummySandbox):
    name = "dummyplain"
    session_isolated_when_nonpersistent = False


@pytest.fixture(autouse=True)
def _clean_plugin_registry():
    from agent import terminal_env_registry as reg

    reg._reset_for_tests()
    yield
    reg._reset_for_tests()


def _register_provider(monkeypatch, name, *, session_isolated, persistent):
    from agent import terminal_env_registry as reg

    reg.register_provider(_DummySandbox() if session_isolated else _PlainSandbox())
    monkeypatch.setenv("TERMINAL_ENV", name)
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", persistent)


def test_plugin_nonpersistent_mode_gives_each_task_its_own_key(monkeypatch):
    _register_provider(
        monkeypatch, "dummysbx", session_isolated=True, persistent="false"
    )
    assert terminal_tool._resolve_container_task_id("rollout-7") == "rollout-7"


def test_plugin_isolation_alias_collapses_subagent_onto_parent(monkeypatch):
    # delegate_agent children register an alias so they still share the parent
    # rollout's sandbox under per-session isolation.
    _register_provider(
        monkeypatch, "dummysbx", session_isolated=True, persistent="false"
    )
    terminal_tool.register_container_alias("subagent-1", "rollout-7")
    try:
        assert terminal_tool._resolve_container_task_id("subagent-1") == "rollout-7"
    finally:
        with terminal_tool._container_alias_lock:
            terminal_tool._container_aliases.pop("subagent-1", None)


def test_plugin_persistent_mode_collapses_to_default(monkeypatch):
    _register_provider(
        monkeypatch, "dummysbx", session_isolated=True, persistent="true"
    )
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    assert terminal_tool._resolve_container_task_id("subagent-9") == "default"


def test_plugin_without_isolation_flag_never_splits_tasks(monkeypatch):
    # Only backends that declare the flag opt into per-session identities.
    _register_provider(
        monkeypatch, "dummyplain", session_isolated=False, persistent="false"
    )
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    assert terminal_tool._resolve_container_task_id("subagent-9") == "default"


def test_shared_key_residue_keeps_ssh_session_scoping(monkeypatch):
    # Shared-key residue under a surviving backend: SSH keeps session scoping
    # — the retired opt-in has no effect anywhere anymore.
    from gateway.session_context import clear_session_vars, set_session_vars

    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_DOCKER_SHARED_CONTAINER_KEY", "team/workspace")
    tokens = set_session_vars(session_key="sess-A", profile="work")
    try:
        assert terminal_tool._resolve_container_task_id(None) == "session:sess-A"
    finally:
        clear_session_vars(tokens)
