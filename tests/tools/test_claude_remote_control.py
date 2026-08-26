"""Behavior-contract tests for the Remote Control lane of delegate_claude_agent.

Two layers:

* **Pure/state-machine tests** — URL extraction, argv construction, model and
  provider rejection, transcript parsing (partial lines, malformed lines,
  symlinks, identity mismatch, empty end_turn, ordered text blocks) driven
  with plain data.
* **Live PTY tests** (``@_REAL_PTY``) — a fake bare ``claude`` executable is
  spawned through the real ``pty.fork()`` path and really signalled, so the
  cleanup contract (TERM→KILL escalation, group reap, no orphan after the
  interactive child survives its own final report) is exercised against the
  OS rather than a mock.

The fake binary writes Claude Code's on-disk contract —
``$HOME/.claude/projects/<encoded-cwd>/<session-id>.jsonl`` — using the
canonical ``encode_claude_cwd`` helper so transcript paths match production.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import sys
import time
import uuid as uuid_module
from pathlib import Path

import pytest

# ── Shared fixtures ───────────────────────────────────────────────────────

_REAL_PTY = pytest.mark.live_system_guard_bypass

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="Remote Control lane is POSIX-only"
)

_GOOD_URL = "https://claude.ai/code/session_1a2b3c4d5e6f7a8b"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Point HOME (and therefore ~/.claude/projects) at a per-test tempdir.

    conftest redirects HERMES_HOME but deliberately not HOME; this lane
    correlates through the real ``~/.claude/projects`` tree, so it needs HOME
    isolated or tests would read and write the developer's actual sessions.
    """
    home = tmp_path / "fakehome"
    (home / ".local" / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _no_inherited_provider(monkeypatch):
    """Start every test from a first-party-clean environment.

    A developer shell (or the claude-glm wrapper) may export any of these; the
    lane must reject them, so tests that *expect* success need them gone and
    tests that expect rejection set exactly one back.
    """
    from tools.claude_remote_control import _FORBIDDEN_PROVIDER_ENV

    for name in _FORBIDDEN_PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fast_poll(monkeypatch):
    """Shrink the monitor cadence so timing tests finish in milliseconds."""
    from tools import claude_remote_control as rc

    monkeypatch.setattr(rc, "MONITOR_POLL_SECONDS", 0.01)
    monkeypatch.setattr(rc, "_REAP_POLL_SECONDS", 0.01)
    monkeypatch.setattr(rc, "_TERM_GRACE_SECONDS", 1.0)
    monkeypatch.setattr(rc, "_KILL_GRACE_SECONDS", 1.0)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    return workdir


def _write_native_claude(home: Path) -> Path:
    """Write the fake bare ``claude`` binary at ~/.local/bin/claude.

    Implements just enough of the real CLI contract for this lane: a
    machine-readable ``auth status --json``, a Remote Control URL painted into
    the PTY, the session transcript under ~/.claude/projects, and an
    interactive process that stays alive after the turn ends.
    """
    repo_root = Path(__file__).resolve().parents[2]
    script = f"""#!{sys.executable}
import json, os, select, signal, subprocess, sys, termios, time, tty
sys.path.insert(0, {str(repo_root)!r})
from tools.claude_remote_control import encode_claude_cwd

MODE = os.environ.get("FAKE_CLAUDE_MODE", "ok")
STDIN_OUT = os.environ.get("FAKE_CLAUDE_STDIN_OUT")

def _record_stdin(data: bytes) -> None:
    if STDIN_OUT and data:
        with open(STDIN_OUT, "ab") as fh:
            fh.write(data)

def _drain_stdin(timeout: float = 0.05) -> bytes:
    recorded = b""
    while True:
        ready, _, _ = select.select([0], [], [], timeout)
        if not ready:
            break
        try:
            chunk = os.read(0, 4096)
        except OSError:
            break
        if not chunk:
            break
        recorded += chunk
        _record_stdin(chunk)
    return recorded

if "auth" in sys.argv[1:3]:
    payloads = {{
        "auth_not_firstparty": {{"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "apiKey"}},
        "auth_not_logged_in": {{"loggedIn": False, "authMethod": None, "apiProvider": None}},
        "auth_nonzero": {{"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}},
        "auth_garbage": None,
        "auth_sensitive": {{
            "loggedIn": False,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "emailAddress": "leak@example.com",
            "accessToken": "sk-ant-leak",
            "organizationName": "Leaky Org",
        }},
    }}
    if MODE == "auth_nonzero":
        sys.stdout.write(json.dumps(payloads[MODE]) + "\\n"); sys.exit(7)
    if MODE == "auth_garbage":
        sys.stdout.write("not json at all\\n"); sys.exit(0)
    sys.stdout.write(json.dumps(payloads.get(MODE, {{
        "loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}})) + "\\n")
    sys.exit(0)

out = os.environ.get("FAKE_CLAUDE_ARGV_OUT")
if out:
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(sys.argv, fh)

sid_out = os.environ.get("FAKE_CLAUDE_SESSION_ID_OUT")
session_id = sys.argv[sys.argv.index("--session-id") + 1] if "--session-id" in sys.argv else None
if sid_out and session_id:
    with open(sid_out, "w", encoding="utf-8") as fh:
        fh.write(session_id)

for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, signal.SIG_DFL)
if MODE == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

# A descendant that outlives the leader proves group cleanup, not just
# proc.terminate() on the direct child.
if MODE in ("ok", "orphan", "exit_after") and session_id:
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--session-id", session_id])

url = os.environ.get("FAKE_CLAUDE_URL", {_GOOD_URL!r})
time.sleep(float(os.environ.get("FAKE_CLAUDE_URL_DELAY", "0")))

if MODE == "trust_blocker":
    sys.stdout.write("Do you trust the files in this folder?\\r\\n")
    sys.stdout.flush()
elif MODE == "bad_url":
    url = "https://evil.example.com/code/session_1a2b3c4d5e6f7a8b"

if MODE not in ("no_url", "trust_blocker"):
    # Paint it the way a TUI does: OSC title, cursor moves, and the URL split
    # across two writes with an escape sequence landing in the middle.
    sys.stdout.write("\\x1b]0;claude\\x07\\x1b[?25l\\r\\n")
    sys.stdout.flush()
    half = len(url) // 2
    sys.stdout.write(url[:half] + "\\x1b[0m")
    sys.stdout.flush()
    time.sleep(0.05)
    sys.stdout.write(url[half:] + "\\r\\n\\x1b[2K")
    sys.stdout.flush()

if MODE == "firstrun_blocker":
    sys.stdout.write("Fullscreen renderer — first-run terminal setup / theme picker\\r\\n")
    sys.stdout.flush()

if MODE in ("exit_early",):
    time.sleep(float(os.environ.get("FAKE_CLAUDE_LINGER", "60")))

def _emit_transcript(task_text: str) -> None:
    if not session_id:
        return
    encoded = encode_claude_cwd(os.getcwd())
    path = os.path.join(os.environ["HOME"], ".claude", "projects", encoded, session_id + ".jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    def emit(obj):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj) + "\\n")

    base = {{"sessionId": session_id, "cwd": os.getcwd(), "version": "2.1.245"}}
    emit({{**base, "type": "user", "isSidechain": False, "isMeta": False,
           "message": {{"role": "user", "content": task_text}}}})

    if MODE == "identity_mismatch":
        emit({{**base, "sessionId": "00000000-0000-0000-0000-000000000000",
               "type": "assistant",
               "message": {{"role": "assistant", "stop_reason": "end_turn",
                            "content": [{{"type": "text", "text": "someone else"}}]}}}})

    emit({{**base, "type": "assistant",
           "message": {{"role": "assistant", "model": "claude-opus-5",
                        "stop_reason": "tool_use",
                        "content": [{{"type": "tool_use", "name": "Edit", "input": {{}}}}]}}}})

    emit({{**base, "type": "assistant",
           "message": {{"role": "assistant", "model": "claude-opus-5",
                        "stop_reason": "end_turn",
                        "content": [{{"type": "thinking", "thinking": "hmm"}}]}}}})

    blocks = int(os.environ.get("FAKE_CLAUDE_TEXT_BLOCKS", "2"))
    emit({{**base, "type": "assistant",
           "message": {{"role": "assistant", "model": "claude-opus-5",
                        "stop_reason": "end_turn",
                        "content": [{{"type": "text", "text": "First block."}}] +
                                    [{{"type": "text", "text": f"Block {{i}}."}}
                                     for i in range(2, blocks + 1)]}}}})

    if MODE == "partial_line":
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{{"type":"assistant","message":{{"role":"ass')  # never completed

    if MODE == "malformed_line":
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{{ this is not json }}\\n')

def _read_stdin_bracketed_paste() -> str:
    old = termios.tcgetattr(0)
    try:
        tty.setraw(0)
        buf = b""
        target = b"\\x1b[201~\\r"
        deadline = time.time() + float(os.environ.get("FAKE_CLAUDE_STDIN_TIMEOUT", "30"))
        while target not in buf:
            if time.time() > deadline:
                sys.exit(1)
            ready, _, _ = select.select([0], [], [], 0.1)
            if not ready:
                continue
            chunk = os.read(0, 4096)
            if not chunk:
                break
            buf += chunk
            _record_stdin(chunk)
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, old)
    start = buf.find(b"\\x1b[200~")
    end = buf.find(b"\\x1b[201~")
    if start >= 0 and end > start:
        return buf[start + 6 : end].decode("utf-8")
    return "do the task"

if MODE == "stdin_submit" and MODE not in ("no_url", "no_transcript", "exit_early", "trust_blocker", "firstrun_blocker"):
    task_text = _read_stdin_bracketed_paste()
    time.sleep(float(os.environ.get("FAKE_CLAUDE_TRANSCRIPT_DELAY", "0")))
    _emit_transcript(task_text)
elif MODE not in ("no_url", "no_transcript", "exit_early", "trust_blocker", "firstrun_blocker", "stdin_submit"):
    time.sleep(float(os.environ.get("FAKE_CLAUDE_TRANSCRIPT_DELAY", "0")))
    argv_task = sys.argv[-1] if len(sys.argv) > 1 else "do the task"
    _emit_transcript(argv_task)

# The interactive CLI is still alive once the turn is done — that is the
# behavior the runner has to clean up after.
linger = float(os.environ.get("FAKE_CLAUDE_LINGER", "60"))
if MODE == "exit_after":
    sys.exit(0)
if MODE in ("argv_autosubmit", "stdin_submit", "trust_blocker", "firstrun_blocker"):
    end = time.time() + linger
    while time.time() < end:
        _drain_stdin(0.05)
        time.sleep(0.05)
else:
    time.sleep(linger)
"""
    binary = home / ".local" / "bin" / "claude"
    binary.write_text(script, encoding="utf-8")
    binary.chmod(0o755)
    return binary


@pytest.fixture
def native_claude(_isolated_home: Path) -> Path:
    return _write_native_claude(_isolated_home)


# ── Schema / handler threading ────────────────────────────────────────────


def test_schema_has_opt_in_remote_control_defaulting_false():
    import tools.claude_agent_tool as tool

    props = tool.DELEGATE_CLAUDE_AGENT_SCHEMA["parameters"]["properties"]
    assert props["remote_control"]["type"] == "boolean"
    assert props["remote_control"]["default"] is False
    assert set(tool.DELEGATE_CLAUDE_AGENT_SCHEMA["parameters"]["required"]) == {
        "task",
        "workdir",
    }
    # The limitation has to be visible where the model reads it.
    for phrase in ("first-party", "PTY", "Bedrock", "Vertex"):
        assert phrase in props["remote_control"]["description"]


def test_handler_threads_remote_control(monkeypatch, repo):
    """The registry handler forwards the flag into the tool function."""
    import tools.claude_agent_tool as tool

    seen = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return json.dumps({"success": True, "error": None, "final_report": "x"})

    monkeypatch.setattr(tool, "delegate_claude_agent", _capture)
    tool._handle_delegate_claude_agent({"task": "t", "workdir": str(repo)})
    assert seen["remote_control"] is False

    tool._handle_delegate_claude_agent(
        {"task": "t", "workdir": str(repo), "remote_control": True}
    )
    assert seen["remote_control"] is True

    tool._handle_delegate_claude_agent(
        {"task": "t", "workdir": str(repo), "remote_control": "false"}
    )
    assert seen["remote_control"] is False


def test_function_signature_defaults_to_false():
    """Omitting the flag is the wrapper lane, never the first-party lane."""
    import inspect

    import tools.claude_agent_tool as tool

    params = inspect.signature(tool.delegate_claude_agent).parameters
    assert params["remote_control"].default is False


# ── remote_control=False leaves the default lane alone ────────────────────


def test_false_path_still_resolves_the_wrapper(monkeypatch, repo, native_claude):
    """With the flag off, wrapper resolution runs — no first-party preflight."""
    import tools.claude_agent_tool as tool

    calls = []
    monkeypatch.setattr(
        tool,
        "resolve_claude_binary",
        lambda model=None: calls.append(model) or "/usr/bin/claude-glm",
    )
    # The wrapper lane must not touch the native-lane preflights.
    import tools.claude_remote_control as rc

    monkeypatch.setattr(
        rc, "run_auth_preflight", lambda *a, **k: pytest.fail("must not preflight")
    )
    monkeypatch.setattr(rc, "resolve_native_claude_binary", lambda: None)

    tool.delegate_claude_agent(
        task="x",
        workdir=str(repo),
        remote_control=False,
        timeout_seconds=0,
    )
    assert calls == [""]


def test_false_path_result_has_no_remote_control_keys(monkeypatch, repo, tmp_path):
    """The default lane's payload shape is unchanged by this feature."""
    import tools.claude_agent_tool as tool

    fake = tmp_path / "claude-glm"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(tool, "resolve_claude_binary", lambda model=None: str(fake))

    payload = json.loads(tool.delegate_claude_agent(task="x", workdir=str(repo)))
    assert "progress_url" not in payload
    assert "remote_control" not in payload
    assert payload["success"] is False  # no result event — the old failure mode
    assert "no result event" in payload["error"]


# ── URL extraction (startup transport) ────────────────────────────────────


def test_extract_url_accepts_strict_shape():
    from tools.claude_remote_control import extract_progress_url

    assert (
        extract_progress_url(f"Remote Control ready: {_GOOD_URL} — go")
        == _GOOD_URL
    )


def test_extract_url_strips_ansi_and_trailing_punctuation():
    from tools.ansi_strip import strip_ansi
    from tools.claude_remote_control import extract_progress_url

    text = f"\x1b[2J\x1b[H\x1b]0;t\x07watch: {_GOOD_URL}.\x1b[0m\r\n"
    assert extract_progress_url(strip_ansi(text)) == _GOOD_URL


@pytest.mark.parametrize(
    "bad",
    [
        "http://claude.ai/code/session_1a2b3c4d5e6f7a8b",  # wrong scheme
        "https://claude.ai/code/session_",  # empty id
        "https://claude.ai/code/session_abc",  # too short
        "https://claude.ai/code/session_1a2b3c4d5e6f7a8b?x=1",  # query string
        "https://claude.ai/code/session_1a2b3c4d5e6f7a8b/extra",  # extra path
        "https://claude.ai/code/other_1a2b3c4d5e6f7a8b",  # wrong path prefix
        "https://evil.example.com/code/session_1a2b3c4d5e6f7a8b",  # wrong host
    ],
)
def test_extract_url_rejects_non_conforming_urls(bad):
    from tools.claude_remote_control import extract_progress_url

    assert extract_progress_url(f"see {bad} now") is None


def test_extract_url_rejects_url_nested_in_another_host():
    """A claude.ai URL inside somebody else's query string is not progress."""
    from tools.claude_remote_control import extract_progress_url

    nested = f"https://evil.example.com/r?u={_GOOD_URL}"
    assert extract_progress_url(nested) is None


def test_extract_url_handles_url_split_across_pty_chunks():
    """The matcher runs over the accumulated tail, not per-read fragments."""
    from tools.claude_remote_control import RemoteControlRun, extract_progress_url

    run = RemoteControlRun(
        ["unused"],
        workdir="/tmp",
        env={},
        session_id=str(uuid_module.uuid4()),
        transcript_path=Path("/tmp/unused.jsonl"),
        projects_root=Path("/tmp"),
    )
    head, tail = _GOOD_URL[:20], _GOOD_URL[20:]
    run._on_pty_bytes(f"\x1b[?25l{head}".encode())
    assert extract_progress_url(run.ansi_stripped_pty_text()) is None
    run._on_pty_bytes(f"\x1b[0m{tail}\r\n".encode())
    assert run.ansi_stripped_pty_text()
    assert extract_progress_url(run.ansi_stripped_pty_text()) == _GOOD_URL


def test_pty_tail_is_bounded():
    """A chatty TUI must not grow the buffer without bound."""
    from tools.claude_remote_control import _PTY_BUFFER_BYTES, RemoteControlRun

    run = RemoteControlRun(
        ["unused"],
        workdir="/tmp",
        env={},
        session_id=str(uuid_module.uuid4()),
        transcript_path=Path("/tmp/unused.jsonl"),
        projects_root=Path("/tmp"),
    )
    for _ in range(20):
        run._on_pty_bytes(b"x" * (_PTY_BUFFER_BYTES // 4))
    assert len(run._raw_pty_tail) <= _PTY_BUFFER_BYTES


# ── Session naming, argv, model handling ──────────────────────────────────


def test_session_name_is_deterministic_and_short():
    from tools.claude_remote_control import build_session_name

    assert build_session_name("/srv/apps/billing") == "Hermes: billing"
    assert build_session_name("/srv/apps/billing/") == "Hermes: billing"
    assert build_session_name("/srv/apps/billing") == build_session_name(
        "/srv/apps/billing"
    )


def test_argv_matches_the_verified_invocation():
    from tools.claude_remote_control import build_remote_control_argv

    session_id = "9c8b7a6d-5e4f-4032-2011-feed00001234"
    argv = build_remote_control_argv(
        "/usr/local/bin/claude",
        session_id=session_id,
        session_name="Hermes: billing",
        prompt="fix the flaky test",
        permission_mode="acceptEdits",
        allowed_tools="Read,Edit,Bash",
    )
    assert argv[0] == "/usr/local/bin/claude"
    # Never print mode: it yields entrypoint=sdk-cli and no Remote Control URL.
    assert "-p" not in argv
    assert "--print" not in argv
    assert argv[argv.index("--session-id") + 1] == session_id
    assert "--remote-control=Hermes: billing" in argv
    assert "--no-chrome" in argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--allowedTools") + 1] == "Read,Edit,Bash"
    assert argv[-1] == "fix the flaky test"
    # An omitted model means the native CLI default — no selector forwarded.
    assert "--model" not in argv
    # --dangerously-skip-permissions is refused under root, as in the wrappers.
    assert "--dangerously-skip-permissions" not in argv


def test_argv_forwards_only_first_party_model_selectors():
    from tools.claude_remote_control import build_remote_control_argv, normalize_first_party_model

    common = dict(
        session_id="s" * 36,
        session_name="Hermes: x",
        prompt="p",
        permission_mode="plan",
        allowed_tools="Read",
    )
    argv = build_remote_control_argv("/bin/claude", model="claude-opus-5", **common)
    assert argv[argv.index("--model") + 1] == "claude-opus-5"

    # A selector belonging to some other provider is dropped, not forwarded:
    # the CLI would silently fall back to a different model than was named.
    assert normalize_first_party_model("glm-5.2") is None
    assert normalize_first_party_model("") is None
    assert normalize_first_party_model(None) is None
    assert normalize_first_party_model("sonnet-5") == "sonnet-5"
    dropped = build_remote_control_argv("/bin/claude", model="some-unknown-vendor", **common)
    assert "--model" not in dropped


@pytest.mark.parametrize("model", ["glm-5.2", "GLM-5.2", "kimi-k3", "Kimi-K3", "z.ai-glm"])
def test_models_requesting_wrappers_are_rejected(model):
    from tools.claude_remote_control import incompatible_model_reason

    reason = incompatible_model_reason(model)
    assert reason, f"{model} should be rejected"
    assert "remote_control" in reason


def test_first_party_and_absent_models_are_not_rejected():
    from tools.claude_remote_control import incompatible_model_reason

    assert incompatible_model_reason(None) is None
    assert incompatible_model_reason("") is None
    assert incompatible_model_reason("claude-sonnet-5") is None


# ── Native binary resolution ──────────────────────────────────────────────


def test_resolves_local_bin_claude(native_claude: Path):
    from tools.claude_remote_control import resolve_native_claude_binary

    assert resolve_native_claude_binary() == str(native_claude)


def test_missing_native_binary_returns_none(monkeypatch, _isolated_home):
    from tools.claude_remote_control import resolve_native_claude_binary

    monkeypatch.setattr("tools.claude_remote_control.shutil.which", lambda name: None)
    assert resolve_native_claude_binary() is None


def test_wrapper_named_file_is_never_accepted(monkeypatch, _isolated_home, tmp_path):
    """A file literally named claude-glm is not a bare first-party CLI."""
    from tools.claude_remote_control import resolve_native_claude_binary

    for wrapper in ("claude-glm", "claude-kimi"):
        decoy = _isolated_home / ".local" / "bin" / wrapper
        decoy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        decoy.chmod(0o755)
    monkeypatch.setattr("tools.claude_remote_control.shutil.which", lambda name: None)
    assert resolve_native_claude_binary() is None


def test_claude_symlink_pointing_at_a_wrapper_is_rejected(
    monkeypatch, _isolated_home, tmp_path
):
    """A `claude` symlink whose target is the GLM wrapper is a spoof."""
    from tools.claude_remote_control import resolve_native_claude_binary

    wrapper = tmp_path / "claude-glm"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)
    link = _isolated_home / ".local" / "bin" / "claude"
    link.symlink_to(wrapper)
    monkeypatch.setattr("tools.claude_remote_control.shutil.which", lambda name: None)
    assert resolve_native_claude_binary() is None


def test_claude_symlink_to_a_real_install_is_accepted(monkeypatch, _isolated_home, tmp_path):
    """Ordinary versioned symlinks (claude -> claude-2.1.245) still resolve."""
    from tools.claude_remote_control import resolve_native_claude_binary

    real = tmp_path / "claude-2.1.245"
    real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real.chmod(0o755)
    (_isolated_home / ".local" / "bin" / "claude").symlink_to(real)
    monkeypatch.setattr("tools.claude_remote_control.shutil.which", lambda name: None)
    resolved = resolve_native_claude_binary()
    assert resolved is not None and resolved.endswith("/claude")


def test_no_new_env_var_steers_the_lane(monkeypatch, _isolated_home):
    """The lane must not grow a path override — the wrapper vars name other providers."""
    from tools.claude_remote_control import (
        _FORBIDDEN_PROVIDER_ENV,
        resolve_native_claude_binary,
    )

    decoy = _isolated_home / "decoy-claude"
    decoy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    decoy.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BIN", str(decoy))
    monkeypatch.setattr("tools.claude_remote_control.shutil.which", lambda name: None)
    assert resolve_native_claude_binary() is None
    assert "CLAUDE_BIN" not in _FORBIDDEN_PROVIDER_ENV


# ── Provider-environment rejection ────────────────────────────────────────


@pytest.mark.parametrize(
    "env",
    [
        {"ANTHROPIC_BASE_URL": "https://relay.example"},
        {"ANTHROPIC_AUTH_TOKEN": "sk-wrapper"},
        {"ANTHROPIC_API_KEY": "sk-ant"},
        {"CLAUDE_CODE_USE_BEDROCK": "1"},
        {"ANTHROPIC_BEDROCK_BASE_URL": "https://bedrock"},
        {"CLAUDE_CODE_USE_VERTEX": "1"},
        {"CLOUD_ML_REGION": "us-east5"},
        {"CLAUDE_CODE_USE_FOUNDRY": "1"},
        {"ANTHROPIC_FOUNDRY_BASE_URL": "https://foundry"},
    ],
)
def test_custom_provider_env_is_reported(env):
    from tools.claude_remote_control import find_conflicting_provider_env

    conflicts = find_conflicting_provider_env(env)
    assert len(conflicts) == 1
    assert env and list(env)[0] in conflicts[0]


def test_clean_env_has_no_conflicts():
    from tools.claude_remote_control import find_conflicting_provider_env

    assert find_conflicting_provider_env({"HOME": "/root", "PATH": "/bin"}) == []


def test_whitespace_only_value_is_not_a_conflict():
    from tools.claude_remote_control import find_conflicting_provider_env

    assert find_conflicting_provider_env({"ANTHROPIC_BASE_URL": "   "}) == []


# ── Auth preflight ────────────────────────────────────────────────────────


def test_auth_summary_drops_everything_sensitive():
    """Only the three provider facts survive; email/org/token never do."""
    from tools.claude_remote_control import summarize_auth_status

    payload = {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "emailAddress": "someone@example.com",
        "organizationName": "Acme",
        "accessToken": "CANARY-token-not-a-real-secret",
    }
    summary = summarize_auth_status(payload)
    assert summary == {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
    }


@pytest.mark.parametrize(
    "payload,expected_problem",
    [
        ({"loggedIn": False, "authMethod": None, "apiProvider": None}, "loggedIn"),
        ({"loggedIn": True, "authMethod": "api", "apiProvider": "apiKey"}, "authMethod"),
        ({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "apiKey"}, "apiProvider"),
    ],
)
def test_auth_problems_name_the_field(payload, expected_problem):
    from tools.claude_remote_control import auth_status_problems

    problems = auth_status_problems(payload)
    assert problems and any(expected_problem in p for p in problems)


def test_auth_payload_tolerates_leading_noise():
    from tools.claude_remote_control import _parse_auth_payload

    raw = 'some banner text\n{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty"}\n'
    payload = _parse_auth_payload(raw)
    assert payload is not None
    assert payload["apiProvider"] == "firstParty"


def test_auth_payload_garbage_is_none():
    from tools.claude_remote_control import _parse_auth_payload

    assert _parse_auth_payload("") is None
    assert _parse_auth_payload("not json") is None
    assert _parse_auth_payload("[1,2,3]") is None


# ── Transcript path correlation ───────────────────────────────────────────


def test_encode_claude_cwd_matches_the_real_directory_layout():
    from tools.claude_remote_control import encode_claude_cwd

    assert encode_claude_cwd("/top/mid") == "-top-mid"
    assert encode_claude_cwd("/x.y") == "-x-y"
    assert encode_claude_cwd("/a_b") == "-a-b"
    assert encode_claude_cwd("/home/operator/.hermes/x") == "-home-operator--hermes-x"
    assert encode_claude_cwd("/srv/app") == "-srv-app"
    assert (
        encode_claude_cwd("/home/operator/.hermes/workspaces/task_demo_id/repo")
        == "-home-operator--hermes-workspaces-task-demo-id-repo"
    )


def test_expected_transcript_path_is_exact():
    from tools.claude_remote_control import expected_transcript_path

    session_id = "11111111-2222-3333-4444-555555555555"
    path = expected_transcript_path(
        session_id, "/srv/app", projects_root=Path("/home/u/.claude/projects")
    )
    assert path == Path(
        "/home/u/.claude/projects/-srv-app/11111111-2222-3333-4444-555555555555.jsonl"
    )


def test_validate_transcript_path_rejects_symlink(tmp_path):
    from tools.claude_remote_control import (
        RemoteControlRunError,
        validate_transcript_path,
    )

    projects = tmp_path / "projects"
    (projects / "-srv-app").mkdir(parents=True)
    real = projects / "-srv-app" / "real.jsonl"
    real.write_text("{}\n", encoding="utf-8")
    link = projects / "-srv-app" / "s.jsonl"
    link.symlink_to("real.jsonl")

    with pytest.raises(RemoteControlRunError, match="symlink"):
        validate_transcript_path(link, projects_root=projects)


def test_validate_transcript_path_rejects_escape_and_non_regular(tmp_path):
    from tools.claude_remote_control import (
        RemoteControlRunError,
        validate_transcript_path,
    )

    projects = tmp_path / "projects"
    projects.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    with pytest.raises(RemoteControlRunError):
        validate_transcript_path(outside, projects_root=projects)

    missing = projects / "-srv-app" / "nope.jsonl"
    with pytest.raises(RemoteControlRunError):
        validate_transcript_path(missing, projects_root=projects)


# ── Transcript completion parsing ─────────────────────────────────────────


def _assistant(session_id, blocks, stop_reason="end_turn", model="claude-opus-5"):
    return {
        "type": "assistant",
        "sessionId": session_id,
        "isSidechain": False,
        "isMeta": False,
        "message": {
            "role": "assistant",
            "model": model,
            "stop_reason": stop_reason,
            "content": blocks,
        },
    }


def _user(session_id, text="do the task"):
    return {
        "type": "user",
        "sessionId": session_id,
        "isSidechain": False,
        "isMeta": False,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


_SID = "11111111-2222-3333-4444-555555555555"


def test_report_requires_the_injected_turn_first():
    from tools.claude_remote_control import parse_transcript_report

    # An assistant answer with no preceding user prompt is not our turn.
    events = [_assistant(_SID, [{"type": "text", "text": "preexisting"}])]
    assert parse_transcript_report(events, _SID) is None


def test_report_ignores_tool_use_and_intermediate_records():
    from tools.claude_remote_control import parse_transcript_report

    events = [
        _user(_SID),
        _assistant(_SID, [{"type": "tool_use", "name": "Bash", "input": {}}], "tool_use"),
        _assistant(_SID, [{"type": "text", "text": "progress"}], "tool_use"),
        _assistant(_SID, [{"type": "tool_result", "content": "ok"}]),
    ]
    assert parse_transcript_report(events, _SID) is None


def test_report_skips_empty_end_turn_before_the_real_one():
    """Claude emits thinking-only end_turn rows; they must not finish the run."""
    from tools.claude_remote_control import parse_transcript_report

    events = [
        _user(_SID),
        _assistant(_SID, [{"type": "thinking", "thinking": "hmm"}]),
        _assistant(_SID, [{"type": "text", "text": ""}]),
        _assistant(_SID, [{"type": "text", "text": "The real answer."}]),
    ]
    report = parse_transcript_report(events, _SID)
    assert report["final_report"] == "The real answer."


def test_report_preserves_multiple_text_blocks_in_order():
    from tools.claude_remote_control import parse_transcript_report

    events = [
        _user(_SID),
        _assistant(
            _SID,
            [
                {"type": "text", "text": "First."},
                {"type": "text", "text": "Second."},
                {"type": "text", "text": "Third."},
            ],
        ),
    ]
    report = parse_transcript_report(events, _SID)
    assert report["final_text_blocks"] == ["First.", "Second.", "Third."]
    assert report["final_report"] == "First.\n\nSecond.\n\nThird."


def test_report_collects_models_actually_used():
    from tools.claude_remote_control import parse_transcript_report

    events = [
        _user(_SID),
        _assistant(_SID, [{"type": "tool_use", "name": "Bash"}], "tool_use", "claude-haiku-4-5"),
        _assistant(_SID, [{"type": "text", "text": "done"}], "end_turn", "claude-opus-5"),
    ]
    report = parse_transcript_report(events, _SID)
    assert report["models_used"] == ["claude-haiku-4-5", "claude-opus-5"]


def test_report_ignores_sidechain_and_meta_records():
    from tools.claude_remote_control import parse_transcript_report

    def _flagged(**flags):
        event = _assistant(_SID, [{"type": "text", "text": "from a subagent"}])
        event.update(flags)
        return event

    events = [
        _user(_SID),
        _flagged(isSidechain=True),
        _flagged(isMeta=True),
        _assistant(_SID, [{"type": "text", "text": "root answer"}]),
    ]
    report = parse_transcript_report(events, _SID)
    assert report["final_report"] == "root answer"


def test_report_ignores_tool_result_user_rows_as_the_injected_turn():
    """Claude echoing a tool result back is not our prompt."""
    from tools.claude_remote_control import parse_transcript_report

    echo = {
        "type": "user",
        "sessionId": _SID,
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
    }
    events = [
        echo,
        _assistant(_SID, [{"type": "text", "text": "too early"}]),
        _user(_SID),
        _assistant(_SID, [{"type": "text", "text": "right one"}]),
    ]
    report = parse_transcript_report(events, _SID)
    assert report["final_report"] == "right one"


def test_report_raises_on_session_identity_mismatch():
    from tools.claude_remote_control import RemoteControlRunError, parse_transcript_report

    events = [
        _user(_SID),
        _assistant("99999999-9999-9999-9999-999999999999", [{"type": "text", "text": "x"}]),
    ]
    with pytest.raises(RemoteControlRunError, match="identity mismatch"):
        parse_transcript_report(events, _SID)


# ── TranscriptWatcher: delayed / partial / malformed ──────────────────────


def _watcher(tmp_path, session_id=_SID):
    from tools.claude_remote_control import TranscriptWatcher

    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    path = projects / "-srv-app" / f"{session_id}.jsonl"
    return (
        TranscriptWatcher(
            path,
            session_id,
            projects_root=projects,
            appear_deadline=time.monotonic() + 5,
        ),
        path,
    )


def _write_events(path: Path, events) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")


def test_watcher_tolerates_delayed_creation(tmp_path):
    watcher, path = _watcher(tmp_path)
    assert watcher.poll() == []  # nothing there yet — not an error
    _write_events(path, [_user(_SID), _assistant(_SID, [{"type": "text", "text": "ok"}])])
    watcher.poll()
    assert watcher.report()["final_report"] == "ok"
    assert watcher.appeared


def test_watcher_holds_back_partial_trailing_line(tmp_path):
    watcher, path = _watcher(tmp_path)
    _write_events(path, [_user(_SID), _assistant(_SID, [{"type": "text", "text": "ok"}])])
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"type":"assistant","mess')  # no newline yet
    watcher.poll()
    report = watcher.report()
    assert report is not None
    assert report["final_report"] == "ok"
    assert len(watcher.events) == 2


def test_watcher_completes_a_partial_line_once_terminated(tmp_path):
    watcher, path = _watcher(tmp_path)
    _write_events(path, [_user(_SID)])
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"type":"assistant","sessionId":"' + _SID + '","message":{"role":"a')
    assert watcher.report() is None
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(
            'ssistant","stop_reason":"end_turn","content":[{"type":"text","text":"late"}]}}\n'
        )
    watcher.poll()
    assert watcher.report()["final_report"] == "late"


def test_watcher_handles_multi_byte_codepoint_split_across_polls(tmp_path):
    """A poll landing mid-UTF-8 codepoint must not corrupt the completed line."""
    watcher, path = _watcher(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "héllo"
    line = json.dumps(
        {
            "type": "user",
            "sessionId": _SID,
            "isSidechain": False,
            "isMeta": False,
            "message": {"role": "user", "content": text},
        },
        ensure_ascii=False,
    )
    raw = (line + "\n").encode("utf-8")
    split_at = raw.index(b"\xc3") + 1
    with open(path, "wb") as fh:
        fh.write(raw[:split_at])
    watcher.poll()
    assert watcher.events == []
    with open(path, "ab") as fh:
        fh.write(raw[split_at:])
    watcher.poll()
    assert len(watcher.events) == 1
    assert watcher.events[0]["message"]["content"] == text


def test_watcher_skips_malformed_complete_lines(tmp_path):
    watcher, path = _watcher(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "this is not json\n"
        + json.dumps(_user(_SID))
        + "\n"
        + "{ broken\n"
        + json.dumps(_assistant(_SID, [{"type": "text", "text": "fine"}]))
        + "\n",
        encoding="utf-8",
    )
    watcher.poll()
    assert watcher.report()["final_report"] == "fine"
    assert len(watcher.events) == 2


def test_watcher_flags_late_transcript(tmp_path):
    from tools.claude_remote_control import TranscriptWatcher

    projects = tmp_path / "projects"
    projects.mkdir()
    stale = TranscriptWatcher(
        projects / "-x" / "s.jsonl",
        _SID,
        projects_root=projects,
        appear_deadline=time.monotonic() - 1,
    )
    assert stale.late is True


def test_watcher_rejects_oversized_file(tmp_path, monkeypatch):
    from tools import claude_remote_control as rc
    from tools.claude_remote_control import RemoteControlRunError, TranscriptWatcher

    watcher, path = _watcher(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("0123456789\n", encoding="utf-8")
    monkeypatch.setattr(rc, "_TRANSCRIPT_MAX_BYTES", 4)
    with pytest.raises(RemoteControlRunError, match="exceeds"):
        watcher.poll()


def test_watcher_rejects_oversized_line(tmp_path, monkeypatch):
    from tools import claude_remote_control as rc
    from tools.claude_remote_control import RemoteControlRunError

    watcher, path = _watcher(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * 100 + "\n", encoding="utf-8")
    monkeypatch.setattr(rc, "_TRANSCRIPT_MAX_LINE_BYTES", 10)
    with pytest.raises(RemoteControlRunError, match="exceeds"):
        watcher.poll()


def test_watcher_survives_truncation(tmp_path):
    """A rotated/truncated file restarts the tail instead of reading garbage."""
    watcher, path = _watcher(tmp_path)
    _write_events(path, [_user(_SID), _assistant(_SID, [{"type": "text", "text": "old"}])])
    watcher.poll()
    path.write_text("", encoding="utf-8")
    watcher.poll()
    _write_events(path, [_user(_SID), _assistant(_SID, [{"type": "text", "text": "new"}])])
    watcher.poll()
    assert watcher.report()["final_report"] == "new"


# ── Platform gate ─────────────────────────────────────────────────────────


def test_platform_gate_reflects_the_host():
    from tools.claude_remote_control import remote_control_platform_supported

    if sys.platform == "win32":
        assert remote_control_platform_supported() is False
    else:
        assert remote_control_platform_supported() is (
            os.name == "posix" and hasattr(os, "killpg")
        )


# ── Rejection matrix through the public entry point ───────────────────────


def _delegate(monkeypatch, repo, **kwargs):
    from tools.claude_remote_control import run_remote_control_delegation

    return run_remote_control_delegation(
        task=kwargs.pop("task", "do the thing"),
        workdir=str(kwargs.pop("workdir", repo)),
        **kwargs,
    )


def test_rejects_unsupported_platform_before_spawning(monkeypatch, repo):
    from tools import claude_remote_control as rc

    monkeypatch.setattr(rc, "remote_control_platform_supported", lambda: False)
    payload = _delegate(monkeypatch, repo)
    assert payload["success"] is False
    assert "POSIX" in payload["error"]
    assert payload["remote_control"]["code"] == "unsupported_platform"
    assert payload["log_path"] is None  # nothing was ever spawned


def test_rejects_glm_model_before_spawning(monkeypatch, repo, native_claude):
    payload = _delegate(monkeypatch, repo, model="glm-5.2")
    assert payload["success"] is False
    assert payload["remote_control"]["code"] == "lane_conflict"
    assert "GLM" in payload["error"]


def test_rejects_kimi_model_before_spawning(monkeypatch, repo, native_claude):
    payload = _delegate(monkeypatch, repo, model="kimi-k3")
    assert payload["success"] is False
    assert payload["remote_control"]["code"] == "lane_conflict"
    assert "Kimi" in payload["error"]


def test_rejects_missing_native_binary(monkeypatch, repo, _isolated_home):
    from tools import claude_remote_control as rc

    monkeypatch.setattr(rc, "resolve_native_claude_binary", lambda: None)
    payload = _delegate(monkeypatch, repo)
    assert payload["success"] is False
    assert payload["remote_control"]["code"] == "binary_unavailable"
    assert "claude-glm" in payload["error"]  # names why the wrappers can't help


@pytest.mark.parametrize(
    "var",
    [
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "ANTHROPIC_FOUNDRY_BASE_URL",
    ],
)
def test_rejects_inherited_custom_provider_env(monkeypatch, repo, native_claude, var):
    monkeypatch.setenv(var, "https://some-custom-provider.example")
    payload = _delegate(monkeypatch, repo)
    assert payload["success"] is False
    assert payload["remote_control"]["code"] == "provider_conflict"
    assert var in payload["error"]


@pytest.mark.parametrize("var", ["ANTHROPIC_BASE_URL", "ANTHROPIC_FOUNDRY_BASE_URL"])
def test_provider_env_is_rejected_not_stripped(monkeypatch, repo, native_claude, var):
    """The run must not proceed on silently-removed provider config."""
    from tools import claude_remote_control as rc

    monkeypatch.setenv(var, "https://relay.example")
    spawned = []
    real_start = rc.RemoteControlRun.start

    def _spy_start(self):
        spawned.append(self.argv)
        return real_start(self)

    monkeypatch.setattr(rc.RemoteControlRun, "start", _spy_start)
    _delegate(monkeypatch, repo)
    assert spawned == []


@pytest.mark.parametrize(
    "mode",
    ["auth_not_firstparty", "auth_not_logged_in", "auth_nonzero", "auth_garbage"],
)
def test_rejects_non_first_party_auth(monkeypatch, repo, native_claude, mode, fast_poll):
    from tools import claude_remote_control as rc

    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    spawned = []
    monkeypatch.setattr(rc, "resolve_native_claude_binary", lambda: str(native_claude))

    real_start = rc.RemoteControlRun.start

    def _spy_start(self):
        spawned.append(self.argv)
        return real_start(self)

    monkeypatch.setattr(rc.RemoteControlRun, "start", _spy_start)
    payload = _delegate(monkeypatch, repo)
    assert payload["success"] is False
    assert payload["remote_control"]["code"] == "auth_not_first_party"
    assert spawned == []


def test_auth_failure_does_not_leak_account_data(monkeypatch, repo, native_claude):
    from tools.claude_remote_control import RemoteControlAuthError, run_auth_preflight

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "auth_sensitive")
    sensitive = ("leak@example.com", "sk-ant-leak", "Leaky Org")
    with pytest.raises(RemoteControlAuthError) as excinfo:
        run_auth_preflight(str(native_claude), dict(os.environ))
    msg = str(excinfo.value)
    for value in sensitive:
        assert value not in msg


def test_rejects_when_no_url_published(monkeypatch, repo, native_claude, fast_poll):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "no_url")
    payload = _delegate(monkeypatch, repo, startup_timeout_seconds=2.0)
    assert payload["success"] is False
    assert payload["remote_control"]["code"] == "no_progress_url"
    assert payload["progress_url"] is None


def test_rejects_wrong_host_url(monkeypatch, repo, native_claude, fast_poll):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "bad_url")
    payload = _delegate(monkeypatch, repo, startup_timeout_seconds=2.0)
    assert payload["success"] is False
    assert payload["remote_control"]["code"] == "no_progress_url"
    assert payload["progress_url"] is None


# ── Live PTY runs ─────────────────────────────────────────────────────────


@_REAL_PTY
@_POSIX_ONLY
def test_live_happy_path_and_reaping_after_completion(
    monkeypatch, repo, native_claude, fast_poll
):
    """End to end: URL, transcript correlation, ordered blocks, no orphan."""
    argv_out = repo / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_OUT", str(argv_out))

    payload = _delegate(monkeypatch, repo)

    assert payload["success"] is True, payload["error"]
    assert payload["error"] is None
    assert payload["progress_url"] == _GOOD_URL
    assert payload["final_report"] == "First block.\n\nBlock 2."
    assert payload["models_used"] == ["claude-opus-5"]
    # Only fields the transcript actually supports; nothing invented.
    assert payload["cost_usd"] is None
    assert payload["num_turns"] is None
    assert payload["permission_denials"] == []

    meta = payload["remote_control"]
    assert meta["enabled"] is True
    assert meta["session_name"] == "Hermes: repo"
    assert meta["auth"] == {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
    }
    assert meta["transcript_path"].endswith(payload["session_id"] + ".jsonl")
    assert Path(meta["transcript_path"]).is_file()

    argv = json.loads(argv_out.read_text(encoding="utf-8"))
    assert argv[0] == str(native_claude)
    assert "-p" not in argv
    assert "--no-chrome" in argv
    assert "--remote-control=Hermes: repo" in argv
    assert argv[-1] == "do the thing"
    # The session id is a freshly generated UUID and matches the transcript.
    assert payload["session_id"] == argv[argv.index("--session-id") + 1]
    assert uuid_module.UUID(payload["session_id"])

    # Bounded private run log exists, and carries no credential material.
    log = Path(payload["log_path"])
    assert log.is_file()
    assert "claude-runs" in str(log)
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700
    assert not list(log.parent.glob("*.pty.log"))
    logged = log.read_text(encoding="utf-8", errors="replace")
    log_lines = [json.loads(line) for line in logged.strip().split("\n") if line.strip()]
    assert any(rec.get("event") == "start" for rec in log_lines)
    assert all("argv" not in rec for rec in log_lines)
    for banned in ("sk-", "token", "emailAddress", "organizationName", "accessToken"):
        assert banned not in logged

    # The interactive child (and its descendant) were reaped.
    assert _no_survivors(payload["remote_control"]["session_uuid"])


@_REAL_PTY
@_POSIX_ONLY
def test_live_structured_log_excludes_task_prompt(monkeypatch, repo, native_claude, fast_poll):
    """The structured log must never persist the delegated task prompt."""
    canary = "PROMPT-CANARY-9f3e7d2a1b"
    task = f"inspect the repo and report {canary}"
    payload = _delegate(monkeypatch, repo, task=task)
    assert payload["success"] is True, payload["error"]
    log = Path(payload["log_path"])
    logged = log.read_text(encoding="utf-8", errors="replace")
    assert canary not in logged
    assert task not in logged
    log_lines = [json.loads(line) for line in logged.strip().split("\n") if line.strip()]
    assert any(rec.get("event") == "start" for rec in log_lines)
    assert all("argv" not in rec for rec in log_lines)
    assert not list(log.parent.glob("*.pty.log"))


@_REAL_PTY
@_POSIX_ONLY
def test_live_child_alive_after_final_report_is_stopped(
    monkeypatch, repo, native_claude, fast_poll
):
    """The CLI is still running when the report lands; the runner stops it."""
    from tools import claude_remote_control as rc

    observed = {}

    def _spy_await_report(self):
        report = real_await_report(self)
        observed["alive_at_completion"] = not self.exited
        return report

    real_await_report = rc.RemoteControlRun.await_report
    monkeypatch.setattr(rc.RemoteControlRun, "await_report", _spy_await_report)
    monkeypatch.setenv("FAKE_CLAUDE_LINGER", "60")

    payload = _delegate(monkeypatch, repo)
    assert payload["success"] is True
    # The whole point of the PTY lane: the process outlives its own answer.
    assert observed["alive_at_completion"] is True
    assert _no_survivors(payload["remote_control"]["session_uuid"])


@_REAL_PTY
@_POSIX_ONLY
def test_live_delayed_transcript_creation(monkeypatch, repo, native_claude, fast_poll):
    monkeypatch.setenv("FAKE_CLAUDE_TRANSCRIPT_DELAY", "1.0")
    payload = _delegate(monkeypatch, repo)
    assert payload["success"] is True, payload["error"]
    assert payload["final_report"].startswith("First block.")


@_REAL_PTY
@_POSIX_ONLY
def test_live_partial_then_completed_line(monkeypatch, repo, native_claude, fast_poll):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "partial_line")
    payload = _delegate(monkeypatch, repo)
    assert payload["success"] is True, payload["error"]


@_REAL_PTY
@_POSIX_ONLY
def test_live_malformed_line_is_ignored(monkeypatch, repo, native_claude, fast_poll):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "malformed_line")
    payload = _delegate(monkeypatch, repo)
    assert payload["success"] is True, payload["error"]


@_REAL_PTY
@_POSIX_ONLY
def test_live_identity_mismatch_is_a_typed_failure(
    monkeypatch, repo, native_claude, fast_poll
):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "identity_mismatch")
    payload = _delegate(monkeypatch, repo)
    assert payload["success"] is False
    assert "identity mismatch" in payload["error"]
    assert payload["remote_control"]["code"] == "run_incomplete"
    assert _no_survivors(payload["remote_control"]["session_uuid"])


@_REAL_PTY
@_POSIX_ONLY
def test_live_leader_exit_after_transcript(monkeypatch, repo, native_claude, fast_poll):
    """Leader exits on its own; the report still lands and descendants die."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "exit_after")
    payload = _delegate(monkeypatch, repo)
    assert payload["success"] is True, payload["error"]
    assert _no_survivors(payload["remote_control"]["session_uuid"])


@_REAL_PTY
@_POSIX_ONLY
def test_live_timeout_kills_the_group(monkeypatch, repo, native_claude, fast_poll):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "no_transcript")
    payload = _delegate(monkeypatch, repo, transcript_appear_grace_seconds=1.0)
    assert payload["success"] is False
    assert "transcript" in payload["error"]
    assert _no_survivors(payload["remote_control"]["session_uuid"])


@_REAL_PTY
@_POSIX_ONLY
def test_live_wall_clock_timeout(monkeypatch, repo, native_claude, fast_poll):
    """A hard wall-clock bound trips even while the TUI keeps repainting."""
    from tools.claude_remote_control import (
        RemoteControlRun,
        RemoteControlRunError,
        build_remote_control_argv,
        build_remote_control_env,
        claude_projects_root,
        expected_transcript_path,
    )

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "no_transcript")
    session_id = str(uuid_module.uuid4())
    resolved_workdir = str(repo.resolve())
    env, conflicts = build_remote_control_env()
    assert conflicts == []
    argv = build_remote_control_argv(
        str(native_claude),
        session_id=session_id,
        session_name="Hermes: repo",
        prompt="do the thing",
        permission_mode="acceptEdits",
        allowed_tools="Read,Write,Edit,Glob,Grep,Bash",
    )
    transcript_path = expected_transcript_path(session_id, resolved_workdir)
    run = RemoteControlRun(
        argv,
        workdir=resolved_workdir,
        env=env,
        session_id=session_id,
        transcript_path=transcript_path,
        projects_root=claude_projects_root(),
        timeout_seconds=2,
        stall_watchdog_seconds=60,
        transcript_appear_grace_seconds=60,
    )
    run.start()
    try:
        run.await_progress_url()
        with pytest.raises(RemoteControlRunError, match="timeout"):
            run.await_report()
    finally:
        run.stop()
    assert _no_survivors(session_id)


@_REAL_PTY
@_POSIX_ONLY
def test_live_stall_watchdog(monkeypatch, repo, native_claude, fast_poll):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "no_transcript")
    payload = _delegate(
        monkeypatch, repo, timeout_seconds=0, stall_watchdog_seconds=1.2
    )
    assert payload["success"] is False
    assert "stalled" in payload["error"]
    assert _no_survivors(payload["remote_control"]["session_uuid"])


@_REAL_PTY
@_POSIX_ONLY
def test_live_interrupt_is_honored(monkeypatch, repo, native_claude, fast_poll):
    from tools import claude_remote_control as rc
    from tools.interrupt import set_interrupt

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "no_transcript")
    original = rc._check_interrupted
    calls = {"n": 0}

    def _interrupt_after_a_while():
        calls["n"] += 1
        if calls["n"] > 3:
            return True
        return original()

    monkeypatch.setattr(rc, "_check_interrupted", _interrupt_after_a_while)
    try:
        payload = _delegate(monkeypatch, repo)
        assert payload["success"] is False
        assert payload["error"] == "interrupted"
        assert _no_survivors(payload["remote_control"]["session_uuid"])
    finally:
        set_interrupt(False)


@_REAL_PTY
@_POSIX_ONLY
def test_live_interrupt_during_await_progress_url(
    monkeypatch, repo, native_claude, fast_poll
):
    """Interrupt is honored while waiting for the Remote Control URL."""
    from tools import claude_remote_control as rc
    from tools.interrupt import set_interrupt

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "no_url")
    original = rc._check_interrupted
    calls = {"n": 0}

    def _interrupt_after_a_while():
        calls["n"] += 1
        if calls["n"] > 3:
            return True
        return original()

    monkeypatch.setattr(rc, "_check_interrupted", _interrupt_after_a_while)
    try:
        payload = _delegate(monkeypatch, repo, startup_timeout_seconds=30)
        assert payload["success"] is False
        assert payload["error"] == "interrupted"
        assert _no_survivors(payload["remote_control"]["session_uuid"])
    finally:
        set_interrupt(False)


@_REAL_PTY
@_POSIX_ONLY
def test_live_term_ignoring_child_is_killed(monkeypatch, repo, native_claude, fast_poll):
    """SIGTERM-ignoring child is stopped only after the TERM grace window + KILL."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stubborn")
    payload = _delegate(monkeypatch, repo)
    assert payload["success"] is True, payload["error"]
    assert _no_survivors(payload["remote_control"]["session_uuid"])
    assert payload["duration_seconds"] >= 0.9


@_REAL_PTY
@_POSIX_ONLY
def test_live_descendant_outliving_leader_is_reaped(
    monkeypatch, repo, native_claude, fast_poll
):
    """The spawned grandchild must not outlive the tool call."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "orphan")
    payload = _delegate(monkeypatch, repo)
    assert payload["success"] is True, payload["error"]
    assert _no_survivors(payload["remote_control"]["session_uuid"])


# ── Prompt submission, setup blockers, unsafe prompts ─────────────────────


def test_sanitize_prompt_for_pty_normalizes_line_endings():
    from tools.claude_remote_control import RemoteControlUnsafePrompt, sanitize_prompt_for_pty

    assert sanitize_prompt_for_pty("a\r\nb\rc") == "a\nb\nc"
    assert sanitize_prompt_for_pty("tab\there\nand\nnewline") == "tab\there\nand\nnewline"


@pytest.mark.parametrize(
    "bad_char",
    ["\x00", "\x07", "\x1b", "\x7f"],
)
def test_sanitize_prompt_for_pty_rejects_unsafe_c0(bad_char):
    from tools.claude_remote_control import RemoteControlUnsafePrompt, sanitize_prompt_for_pty

    canary = f"secret-task-{bad_char}-text"
    with pytest.raises(RemoteControlUnsafePrompt) as excinfo:
        sanitize_prompt_for_pty(canary)
    msg = str(excinfo.value)
    assert excinfo.value.code == "unsafe_prompt"
    assert "U+" in msg
    assert canary not in msg


def test_detect_setup_blocker_labels():
    from tools.claude_remote_control import detect_setup_blocker

    assert detect_setup_blocker("Do you trust the files in this folder?") == "workspace trust"
    assert (
        detect_setup_blocker("Fullscreen renderer — first-run terminal setup")
        == "first-run setup"
    )
    assert detect_setup_blocker("Remote Control ready") is None


@_REAL_PTY
@_POSIX_ONLY
def test_live_pty_prompt_submission(monkeypatch, repo, native_claude, fast_poll):
    """Bracketed-paste injection when Claude does not auto-submit argv."""
    stdin_out = repo / "stdin.bin"
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stdin_submit")
    monkeypatch.setenv("FAKE_CLAUDE_STDIN_OUT", str(stdin_out))
    payload = _delegate(
        monkeypatch,
        repo,
        prompt_submit_grace_seconds=0.2,
        prompt_ready_quiet_seconds=0.05,
        prompt_ready_timeout_seconds=2.0,
    )
    assert payload["success"] is True, payload["error"]
    assert stdin_out.read_bytes() == b"\x1b[200~do the thing\x1b[201~\r"
    transcript = Path(payload["remote_control"]["transcript_path"]).read_text(encoding="utf-8")
    user_turns = [
        json.loads(line)
        for line in transcript.splitlines()
        if line.strip() and json.loads(line).get("type") == "user"
    ]
    assert len(user_turns) == 1
    assert payload["final_report"] == "First block.\n\nBlock 2."
    assert payload["remote_control"]["prompt_source"] == "pty"
    assert _no_survivors(payload["remote_control"]["session_uuid"])


@_REAL_PTY
@_POSIX_ONLY
def test_live_argv_autosubmit_dedupes_pty_injection(
    monkeypatch, repo, native_claude, fast_poll
):
    stdin_out = repo / "stdin.bin"
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "argv_autosubmit")
    monkeypatch.setenv("FAKE_CLAUDE_STDIN_OUT", str(stdin_out))
    payload = _delegate(
        monkeypatch,
        repo,
        prompt_submit_grace_seconds=2.0,
        prompt_ready_quiet_seconds=0.05,
        prompt_ready_timeout_seconds=2.0,
    )
    assert payload["success"] is True, payload["error"]
    assert not stdin_out.exists() or stdin_out.read_bytes() == b""
    transcript = Path(payload["remote_control"]["transcript_path"]).read_text(encoding="utf-8")
    user_turns = [
        json.loads(line)
        for line in transcript.splitlines()
        if line.strip() and json.loads(line).get("type") == "user"
    ]
    assert len(user_turns) == 1
    assert payload["remote_control"]["prompt_source"] == "argv"


@_REAL_PTY
@_POSIX_ONLY
def test_live_argv_autosubmit_dedupes_after_grace_during_readiness(
    monkeypatch, repo, native_claude, fast_poll
):
    """Argv auto-submit that lands during readiness must not trigger PTY injection."""
    stdin_out = repo / "stdin.bin"
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "argv_autosubmit")
    monkeypatch.setenv("FAKE_CLAUDE_STDIN_OUT", str(stdin_out))
    monkeypatch.setenv("FAKE_CLAUDE_TRANSCRIPT_DELAY", "1.5")
    payload = _delegate(
        monkeypatch,
        repo,
        prompt_submit_grace_seconds=0.5,
        prompt_ready_quiet_seconds=2.0,
        prompt_ready_timeout_seconds=10.0,
    )
    assert payload["success"] is True, payload["error"]
    assert payload["remote_control"]["prompt_source"] == "argv"
    assert not stdin_out.exists() or stdin_out.read_bytes() == b""
    transcript = Path(payload["remote_control"]["transcript_path"]).read_text(encoding="utf-8")
    user_turns = [
        json.loads(line)
        for line in transcript.splitlines()
        if line.strip() and json.loads(line).get("type") == "user"
    ]
    assert len(user_turns) == 1
    assert _no_survivors(payload["remote_control"]["session_uuid"])


@_REAL_PTY
@_POSIX_ONLY
def test_live_trust_blocker_fails_fast_before_url(
    monkeypatch, repo, native_claude, fast_poll
):
    stdin_out = repo / "stdin.bin"
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "trust_blocker")
    monkeypatch.setenv("FAKE_CLAUDE_STDIN_OUT", str(stdin_out))
    started = time.monotonic()
    payload = _delegate(
        monkeypatch,
        repo,
        startup_timeout_seconds=30.0,
        prompt_submit_grace_seconds=0.2,
        prompt_ready_quiet_seconds=0.05,
        prompt_ready_timeout_seconds=2.0,
    )
    elapsed = time.monotonic() - started
    assert payload["success"] is False
    assert payload["remote_control"]["code"] == "interactive_setup_blocked"
    assert str(repo.resolve()) in payload["error"]
    assert "claude" in payload["error"].lower()
    assert not stdin_out.exists() or stdin_out.read_bytes() == b""
    assert elapsed < 10.0
    assert _no_survivors(payload["remote_control"]["session_uuid"])


@_REAL_PTY
@_POSIX_ONLY
def test_live_firstrun_blocker_after_url(monkeypatch, repo, native_claude, fast_poll):
    stdin_out = repo / "stdin.bin"
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "firstrun_blocker")
    monkeypatch.setenv("FAKE_CLAUDE_STDIN_OUT", str(stdin_out))
    payload = _delegate(
        monkeypatch,
        repo,
        prompt_submit_grace_seconds=0.2,
        prompt_ready_quiet_seconds=0.05,
        prompt_ready_timeout_seconds=2.0,
    )
    assert payload["success"] is False
    assert payload["remote_control"]["code"] == "interactive_setup_blocked"
    assert str(repo.resolve()) in payload["error"]
    assert "claude" in payload["error"].lower()
    assert not stdin_out.exists() or stdin_out.read_bytes() == b""
    assert _no_survivors(payload["remote_control"]["session_uuid"])


def test_unsafe_task_fails_closed_before_spawn(monkeypatch, repo, native_claude):
    from tools import claude_remote_control as rc

    canary = "CANARY-UNSAFE-TASK-7c4a"
    spawned = []
    real_start = rc.RemoteControlRun.start

    def _spy_start(self):
        spawned.append(self.argv)
        return real_start(self)

    monkeypatch.setattr(rc.RemoteControlRun, "start", _spy_start)
    for bad in (f"{canary}\x07", f"{canary}\x1b"):
        payload = _delegate(monkeypatch, repo, task=bad)
        assert payload["success"] is False
        assert payload["remote_control"]["code"] == "unsafe_prompt"
        assert canary not in (payload["error"] or "")
        log_path = payload.get("log_path")
        if log_path and Path(log_path).exists():
            logged = Path(log_path).read_text(encoding="utf-8", errors="replace")
            assert canary not in logged
    assert spawned == []


@_REAL_PTY
@_POSIX_ONLY
def test_live_log_privacy_includes_prompt_submitted_metadata_only(
    monkeypatch, repo, native_claude, fast_poll
):
    canary = "PROMPT-CANARY-9f3e7d2a1b"
    task = f"inspect the repo and report {canary}"
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stdin_submit")
    payload = _delegate(
        monkeypatch,
        repo,
        task=task,
        prompt_submit_grace_seconds=0.2,
        prompt_ready_quiet_seconds=0.05,
        prompt_ready_timeout_seconds=2.0,
    )
    assert payload["success"] is True, payload["error"]
    log = Path(payload["log_path"])
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700
    logged = log.read_text(encoding="utf-8", errors="replace")
    assert canary not in logged
    assert task not in logged
    log_lines = [json.loads(line) for line in logged.strip().split("\n") if line.strip()]
    submitted = [rec for rec in log_lines if rec.get("event") == "prompt_submitted"]
    assert len(submitted) == 1
    rec = submitted[0]
    assert set(rec.keys()) <= {"ts", "event", "source", "chars", "bytes"}
    assert rec["source"] == "pty"
    assert rec["chars"] == len(task)
    assert rec["bytes"] == len(task.encode("utf-8"))


def _no_survivors(session_uuid: str) -> bool:
    """True when nothing from this run's process group is still alive."""
    import psutil

    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline = proc.info["cmdline"] or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if any(session_uuid in str(part) for part in cmdline):
            return False
    return True


# End of Remote Control lane tests.
