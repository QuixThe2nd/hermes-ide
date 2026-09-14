"""Tests for ``hermes_cli/os_notify.py`` — native OS turn-finished toasts.

Covers the three public helpers: ``sanitize_notify_text`` (the AppleScript/
shell-quoting firewall), ``resolve_session_title`` (read-only state.db
lookup with fallbacks), and ``notify_session_complete`` (interrupt gate,
ssh single-remote-argument contract, command-override env vars, and the
never-raise guarantee).
"""

from __future__ import annotations

import sqlite3
import subprocess
import types

import pytest

import hermes_cli.os_notify as os_notify
from hermes_cli.os_notify import (
    notify_session_complete,
    resolve_session_title,
    sanitize_notify_text,
)


class _RecordingRun:
    """``subprocess.run`` double that records calls and can be made to fail."""

    def __init__(self, exc: Exception | None = None):
        self.calls: list[tuple[tuple, dict]] = []
        self.exc = exc

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture()
def fake_run(monkeypatch):
    """Replace the module's ``subprocess`` with a recording ``run``.

    Patching the module attribute (not ``subprocess.run`` globally) keeps
    the double scoped to ``os_notify`` for the duration of the test.
    """
    runner = _RecordingRun()
    monkeypatch.setattr(
        os_notify,
        "subprocess",
        types.SimpleNamespace(run=runner, DEVNULL=subprocess.DEVNULL),
    )
    return runner


@pytest.fixture()
def state_db(tmp_path, monkeypatch):
    """A minimal ``state.db`` under a fake Hermes home."""
    monkeypatch.setattr(os_notify, "get_hermes_home", lambda: tmp_path)
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, display_name TEXT, title TEXT)"
    )
    conn.executemany(
        "INSERT INTO sessions (id, display_name, title) VALUES (?, ?, ?)",
        [
            ("s1", "Display Name", "Fallback Title"),
            ("s2", None, "Fallback Title"),
            # Blank display_name falls through to the title, stripped.
            ("s3", "", "  Whitespace Title  "),
            ("s4", None, None),
        ],
    )
    conn.commit()
    conn.close()
    return tmp_path / "state.db"


@pytest.fixture()
def no_state_db(tmp_path, monkeypatch):
    """A fake Hermes home with no ``state.db`` at all."""
    monkeypatch.setattr(os_notify, "get_hermes_home", lambda: tmp_path)
    return tmp_path


# ── sanitize_notify_text ────────────────────────────────────────────


def test_sanitize_strips_quotes_and_metacharacters():
    raw = 'He said "hi" & `rm -rf` $HOME; (exit 1) %s'
    out = sanitize_notify_text(raw)
    for bad in '"', "'", "&", "`", "$", ";", "(", ")", "%":
        assert bad not in out
    assert out == "He said hi  rm -rf HOME exit 1 s"


def test_sanitize_keeps_allowed_characters():
    raw = "feat/os-notify_v2 #42 @main: refs + wip.d"
    assert sanitize_notify_text(raw) == raw


def test_sanitize_truncates_to_limit():
    assert sanitize_notify_text("a" * 300, 120) == "a" * 120
    assert sanitize_notify_text("abcdefghij", 5) == "abcde"
    # Truncation does not leave trailing whitespace.
    assert sanitize_notify_text("abc   ", 4) == "abc"


def test_sanitize_handles_non_strings():
    assert sanitize_notify_text(None) == ""
    assert sanitize_notify_text(12345) == "12345"
    assert sanitize_notify_text("   ") == ""


# ── resolve_session_title ───────────────────────────────────────────


def test_resolve_title_prefers_display_name(state_db):
    assert resolve_session_title("s1") == "Display Name"


def test_resolve_title_falls_back_to_title(state_db):
    assert resolve_session_title("s2") == "Fallback Title"
    assert resolve_session_title("s3") == "Whitespace Title"


def test_resolve_title_falls_back_to_hermes(state_db):
    assert resolve_session_title("s4") == "Hermes"
    # Missing row.
    assert resolve_session_title("no-such-session") == "Hermes"
    # Empty/None id never reaches the database.
    assert resolve_session_title("") == "Hermes"
    assert resolve_session_title(None) == "Hermes"


def test_resolve_title_missing_database(no_state_db):
    assert resolve_session_title("s1") == "Hermes"


# ── notify_session_complete ─────────────────────────────────────────


def test_notify_is_noop_when_interrupted(fake_run):
    notify_session_complete(
        session_id="s1",
        platform="cli",
        interrupted=True,
        completed=False,
        ssh_target="some-mac",
        command="echo hi",
    )
    assert fake_run.calls == []


def test_notify_ssh_uses_batchmode_and_single_remote_argument(
    fake_run, monkeypatch
):
    monkeypatch.setattr(
        os_notify, "resolve_session_title", lambda sid: 'Parsas "Mac"'
    )
    notify_session_complete(
        session_id="s1",
        platform="discord",
        interrupted=False,
        completed=True,
        ssh_target="parsas-macbook-pro",
    )
    assert len(fake_run.calls) == 1
    argv = fake_run.calls[0][0][0]
    assert argv[:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6"]
    assert argv[5] == "parsas-macbook-pro"
    # The entire osascript invocation is ONE remote argument — ssh joins
    # its arguments with spaces and hands them to the remote shell.
    assert len(argv) == 7
    remote = argv[6]
    assert remote.startswith("osascript -e ")
    # Sanitized title inside the AppleScript; no quote-breakout possible
    # (the only single quotes are the two wrapping the -e argument).
    assert 'with title "Parsas Mac"' in remote
    assert 'subtitle "Task finished"' in remote
    assert '"Session finished · discord"' in remote
    assert remote.count("'") == 2
    # Hard timeout on the whole attempt.
    assert fake_run.calls[0][1]["timeout"] == 10


def test_notify_command_override_env(fake_run):
    notify_session_complete(
        session_id="sess-42",
        platform="telegram",
        interrupted=False,
        completed=True,
        command="/opt/notify.sh",
        title='Custom "Title"',
    )
    assert len(fake_run.calls) == 1
    args, kwargs = fake_run.calls[0]
    assert args[0] == "/opt/notify.sh"
    assert kwargs["shell"] is True
    env = kwargs["env"]
    assert env["HERMES_NOTIFY_TITLE"] == "Custom Title"
    assert env["HERMES_NOTIFY_SUBTITLE"] == "Task finished"
    assert env["HERMES_NOTIFY_BODY"] == "Session finished · telegram"
    assert env["HERMES_NOTIFY_PLATFORM"] == "telegram"
    assert env["HERMES_NOTIFY_SESSION_ID"] == "sess-42"


def test_notify_command_override_wins_over_ssh(fake_run):
    notify_session_complete(
        session_id="s1",
        platform="cli",
        interrupted=False,
        completed=True,
        ssh_target="some-mac",
        command="/opt/notify.sh",
    )
    assert len(fake_run.calls) == 1
    args, kwargs = fake_run.calls[0]
    # The override runs as a shell command, NOT via ssh.
    assert args[0] == "/opt/notify.sh"
    assert kwargs["shell"] is True


def test_notify_subprocess_failures_never_raise(fake_run, monkeypatch):
    for exc in (
        subprocess.TimeoutExpired(cmd="ssh", timeout=10),
        FileNotFoundError("ssh"),
        OSError("boom"),
        RuntimeError("unexpected"),
    ):
        runner = _RecordingRun(exc=exc)
        monkeypatch.setattr(
            os_notify,
            "subprocess",
            types.SimpleNamespace(run=runner, DEVNULL=subprocess.DEVNULL),
        )
        # Command override path...
        notify_session_complete(
            session_id="s1", platform="cli", completed=True, command="true"
        )
        # ...and the built-in ssh path.
        notify_session_complete(
            session_id="s1", platform="cli", completed=True, ssh_target="mac"
        )
        assert len(runner.calls) == 2


def test_notify_local_linux_uses_notify_send(fake_run, monkeypatch):
    monkeypatch.setattr(
        os_notify, "resolve_session_title", lambda sid: "Build lane"
    )
    monkeypatch.setattr(
        os_notify.shutil, "which", lambda name: "/usr/bin/notify-send"
    )
    notify_session_complete(
        session_id="s1", platform="", interrupted=False, completed=True
    )
    argv = fake_run.calls[0][0][0]
    assert argv[0] == "notify-send"
    assert argv[1] == "Build lane"
    # Empty platform omits the "· platform" suffix.
    assert argv[2] == "Session finished"


def test_notify_local_without_dispatcher_is_silent(fake_run, monkeypatch):
    monkeypatch.setattr(os_notify.shutil, "which", lambda name: None)
    notify_session_complete(
        session_id="s1", platform="cli", completed=True
    )
    assert fake_run.calls == []
