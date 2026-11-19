"""Child shell identity leaking into the parent terminal via the shared snapshot.

Delegate children share the parent's LocalEnvironment on purpose:
``tools.terminal_tool._resolve_container_task_id`` collapses the child onto the
parent key and ``seed_workspace`` registers the container alias. The shared
bash snapshot (``tools/environments/base_session_env.py``) re-dumps
``export -p`` after every command, so a child's
``export HOME=... HERMES_HOME=... PYTHONPATH=...`` persists into the snapshot
and the parent's NEXT command sources the scratch home — GitHub CLI reads it
and asks for login although the real account is fine. The process
``os.environ`` is not the leak; the snapshot is.

The pin: ``ShellIdentityGuard`` must snapshot the identity names (GH_CONFIG_DIR,
HERMES_HOME, HOME, PYTHONPATH, XDG_CONFIG_HOME) at the first delegation
boundary and restore them when the LAST guard exits — refcounted on the shared
environment key, so an inner or parallel-sibling exit never restores early —
by running export/unset on the live shell so the normal per-command re-dump
stores the parent value. Other exports (PATH, an arbitrary FOO) stay.
"""

import pytest

from tools.shell_identity_guard import ShellIdentityGuard

IDENTITY_NAMES = ("GH_CONFIG_DIR", "HERMES_HOME", "HOME", "PYTHONPATH", "XDG_CONFIG_HOME")

CHILD_EXPORTS = (
    "export HOME=/tmp/pc88-child-home HERMES_HOME=/tmp/pc88-child-home/.hermes "
    "PYTHONPATH=/tmp/pc88-child-py XDG_CONFIG_HOME=/tmp/pc88-child-xdg "
    "GH_CONFIG_DIR=/tmp/pc88-child-gh"
)


def _probe_identity(env) -> dict:
    """Live-shell oracle, independent of the guard under test: exact value per
    name, ``None`` when the name is unset (not empty-but-set). NUL delimiters
    fence each answer — env values cannot contain NUL, so spaces, quotes and
    newlines round-trip unharmed."""
    probe = "; ".join(f'printf \'%s\\0%s\\0\' "${{{n}+s}}" "${{{n}-}}"' for n in IDENTITY_NAMES)
    result = env.execute(probe, timeout=30, rewrite_compound_background=False)
    assert int(result.get("returncode") or 0) == 0, f"identity probe failed: {result!r}"
    chunks = (result.get("output") or "").split("\0")
    probed = {}
    for i, name in enumerate(IDENTITY_NAMES):
        flag = chunks[2 * i] if 2 * i < len(chunks) else ""
        value = chunks[2 * i + 1] if 2 * i + 1 < len(chunks) else ""
        probed[name] = value if flag == "s" else None
    return probed


@pytest.fixture
def shared_shell(tmp_path, monkeypatch):
    """A real LocalEnvironment registered as the live shell under the shared
    container key. PYTHONPATH leaves the process env BEFORE the login snapshot
    is taken, so a Popen env can never reintroduce it behind an unset."""
    from tools import terminal_tool
    from tools.environments.local import LocalEnvironment

    monkeypatch.delenv("PYTHONPATH", raising=False)
    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    env.init_session()
    key = terminal_tool._resolve_container_task_id(None)
    with terminal_tool._env_lock:
        terminal_tool._active_environments[key] = env
    try:
        yield env
    finally:
        with terminal_tool._env_lock:
            terminal_tool._active_environments.pop(key, None)
        env.cleanup()


@pytest.mark.platforms("posix")
def test_last_guard_restores_identity_and_keeps_other_exports(shared_shell):
    env = shared_shell
    parent = env.execute("export HOME=/tmp/pc88-parent-home; unset PYTHONPATH", timeout=30)
    assert int(parent.get("returncode") or 0) == 0, f"parent export failed: {parent!r}"
    before = _probe_identity(env)
    assert before["HOME"] == "/tmp/pc88-parent-home"
    assert before["PYTHONPATH"] is None
    with ShellIdentityGuard("pc88-child"):
        child = env.execute(f"{CHILD_EXPORTS}; export FOO=from-child", timeout=30)
        assert int(child.get("returncode") or 0) == 0, f"child export failed: {child!r}"
        inside = _probe_identity(env)
        assert inside["HOME"] == "/tmp/pc88-child-home", f"child export did not land: {inside!r}"
    after = _probe_identity(env)
    assert after == before, f"child identity survived into the parent shell: {after!r} != {before!r}"
    assert after["HOME"] == "/tmp/pc88-parent-home", f"parent HOME not restored: {after!r}"
    # An unset name stays unset — restored as absent, not as empty-but-set.
    assert after["PYTHONPATH"] is None, f"PYTHONPATH reintroduced as: {after['PYTHONPATH']!r}"
    foo = env.execute('printf %s "$FOO"', timeout=30)
    assert foo["output"] == "from-child", f"unrelated export was clobbered: {foo!r}"


@pytest.mark.platforms("posix")
def test_inner_release_keeps_scratch_outer_release_restores(shared_shell):
    env = shared_shell
    parent = env.execute("export HOME=/tmp/pc88-parent-home", timeout=30)
    assert int(parent.get("returncode") or 0) == 0, f"parent export failed: {parent!r}"
    outer = ShellIdentityGuard("pc88-outer-child")
    inner = ShellIdentityGuard("pc88-inner-child")
    with outer:
        child = env.execute("export HOME=/tmp/pc88-child-home", timeout=30)
        assert int(child.get("returncode") or 0) == 0, f"child export failed: {child!r}"
        with inner:
            pass
        mid = _probe_identity(env)
        assert mid["HOME"] == "/tmp/pc88-child-home", f"inner exit restored early: {mid!r}"
    end = _probe_identity(env)
    assert end["HOME"] == "/tmp/pc88-parent-home", f"outer (last) exit did not restore: {end!r}"


@pytest.mark.platforms("posix")
def test_parent_export_persists_without_guard(shared_shell):
    env = shared_shell
    seeded = env.execute("export HOME=/tmp/pc88-parent-persisted", timeout=30)
    assert int(seeded.get("returncode") or 0) == 0, f"parent export failed: {seeded!r}"
    seen = _probe_identity(env)
    assert seen["HOME"] == "/tmp/pc88-parent-persisted", f"snapshot persistence broken: {seen!r}"
