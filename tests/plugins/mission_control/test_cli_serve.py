"""The real ``hermes mission_control serve`` path, end to end.

Config precedence for the serve flags, then a real CLI subprocess
against a temporary HERMES_HOME on a free loopback port: the inbox, a
session page, inline (image-free) assets, the full feed, a delta feed
poll that observes a row inserted after serving started, and a clean
SIGTERM shutdown. Nothing here touches a real Hermes home or spawns a
real hermes turn.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

SESSIONS_SCHEMA = """
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  title TEXT,
  display_name TEXT,
  started_at REAL NOT NULL,
  ended_at REAL,
  end_reason TEXT,
  last_activity_at REAL,
  archived INTEGER NOT NULL DEFAULT 0,
  hidden INTEGER NOT NULL DEFAULT 0,
  cwd TEXT,
  thread_id TEXT,
  parent_session_id TEXT
);
"""

MESSAGES_SCHEMA = """
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT,
  tool_name TEXT,
  tool_call_id TEXT,
  tool_calls TEXT,
  codex_message_items TEXT,
  timestamp REAL NOT NULL,
  finish_reason TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  display_kind TEXT
);
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_home(root: Path) -> Path:
    """A throwaway Hermes home with one default-profile session."""
    home = root / ".hermes"
    db = home / "state.db"
    db.parent.mkdir(parents=True)
    now = time.time()
    con = sqlite3.connect(str(db))
    con.executescript(SESSIONS_SCHEMA + MESSAGES_SCHEMA)
    con.execute(
        "INSERT INTO sessions (id, source, title, started_at,"
        " last_activity_at) VALUES ('sess-live', 'discord',"
        " 'Serve smoke session', ?, ?)", (now - 120, now - 5))
    rows = [
        ("user", "please check the failing tests", now - 120,
         None, None, None),
        ("tool", "", now - 100, "read_file", None, None),
        ("assistant", "", now - 90, None, None,
         '[{"type": "message", "role": "assistant", "phase":'
         ' "commentary", "content": [{"type": "output_text",'
         ' "text": "Stepping into the repo to check the failing '
         'tests."}]}]'),
        ("assistant", "all green now", now - 5, None, None, None),
    ]
    for role, content, ts, tname, tc, codex in rows:
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp,"
            " tool_name, tool_calls, codex_message_items)"
            " VALUES (?,?,?,?,?,?,?)",
            ("sess-live", role, content, ts, tname, tc, codex))
    con.commit()
    con.close()
    return home


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_text(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


class _ServeProcess:
    """One real ``mission_control serve`` subprocess under test."""

    def __init__(self, home: Path, *extra_args: str):
        self.tmp = tempfile.mkdtemp(prefix="mc-serve-test-")
        self._all_output = ""
        env = dict(os.environ)
        env["HERMES_HOME"] = str(home)
        env.setdefault("HERMES_BUNDLED_PLUGINS", str(REPO_ROOT / "plugins"))
        self.port = _free_port()
        argv = [sys.executable, "-m", "hermes_cli.main",
                "mission_control", "serve",
                "--port", str(self.port)] + list(extra_args)
        self.proc = subprocess.Popen(
            argv, cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")

    def wait_ready(self, timeout: float = 60.0) -> str:
        """Block until the startup line lands; return output so far."""
        deadline = time.time() + timeout
        lines: list[str] = []
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if line:
                lines.append(line)
                if "serving on http://" in line:
                    return "".join(lines)
                if "Traceback" in line:
                    raise AssertionError(
                        "serve crashed at startup:\n" + "".join(lines))
                continue
            if self.proc.poll() is not None:
                raise AssertionError(
                    "serve exited before listening:\n" + "".join(lines))
            time.sleep(0.05)
        raise AssertionError("serve never listened:\n" + "".join(lines))

    def output(self) -> str:
        return self._all_output

    def stop(self, timeout: float = 20.0) -> int:
        self.proc.send_signal(signal.SIGTERM)
        try:
            self._all_output, _ = self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self._all_output, _ = self.proc.communicate()
            raise AssertionError("serve ignored SIGTERM")
        return self.proc.returncode

    def url(self, path: str) -> str:
        return "http://127.0.0.1:%d%s" % (self.port, path)


@pytest.fixture
def serve_home(tmp_path):
    return _make_home(tmp_path)


def test_config_defaults_when_section_absent(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    from plugins.mission_control.cli import load_serve_defaults

    assert load_serve_defaults() == {"host": "127.0.0.1", "port": 9136,
                                     "discord_sync": True}


def test_config_section_overrides_defaults(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "mission_control:\n  host: 127.0.0.2\n  port: 9200\n"
        "  discord_sync: false\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    from plugins.mission_control.cli import load_serve_defaults

    assert load_serve_defaults() == {"host": "127.0.0.2", "port": 9200,
                                     "discord_sync": False}


def test_config_invalid_values_fall_back(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "mission_control:\n  host: '   '\n  port: not-a-port\n",
        encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    from plugins.mission_control.cli import load_serve_defaults

    assert load_serve_defaults() == {"host": "127.0.0.1", "port": 9136,
                                     "discord_sync": True}


def test_cmd_serve_builds_argv_from_config_and_flags(
        tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "mission_control:\n  port: 9201\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    import argparse

    from plugins.mission_control import cli, server

    captured: list[list[str]] = []
    monkeypatch.setattr(
        server, "main", lambda argv: captured.append(list(argv)))

    def run_cli(*flags):
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers(dest="cmd")
        plugin_parser = subs.add_parser("mission_control")
        cli.register_cli(plugin_parser)
        args = parser.parse_args(["mission_control", "serve"] +
                                 list(flags))
        assert cli.mission_control_command(args) == 0

    # Config port applies, loopback host is the default, sync stays on.
    run_cli()
    assert captured[-1] == ["--host", "127.0.0.1", "--port", "9201"]

    # CLI flags win over the config section.
    run_cli("--host", "127.0.0.5", "--port", "9202",
            "--no-discord-sync")
    assert captured[-1] == ["--host", "127.0.0.5", "--port", "9202",
                            "--no-discord-sync"]

    # Config discord_sync: false forces the flag even without --no-…
    (home / "config.yaml").write_text(
        "mission_control:\n  discord_sync: false\n", encoding="utf-8")
    run_cli()
    assert captured[-1][-1] == "--no-discord-sync"


def test_real_serve_path_root_session_feeds_and_clean_shutdown(
        serve_home):
    proc = _ServeProcess(serve_home, "--no-discord-sync")
    try:
        startup = proc.wait_ready()
        assert "serving on http://127.0.0.1:%d/" % proc.port in startup
        assert str(serve_home) in startup

        # Inbox: the session row, generic identities, no image assets.
        status, root = _get_text(proc.url("/"))
        assert status == 200
        assert "Serve smoke session" in root
        assert "Hermes" in root
        assert "<img" not in root
        assert 'src="/static' not in root

        # Session page: transcript with recovered commentary and a
        # collapsed tool group; assets are inline style/script only.
        status, page = _get_text(proc.url("/s/default/sess-live"))
        assert status == 200
        assert "please check the failing tests" in page
        assert "Stepping into the repo to check the failing tests." in page
        assert "all green now" in page
        assert "tool-group" in page
        assert "<style>" in page and "<script>" in page
        assert "<img" not in page
        assert 'src="/static' not in page

        # Full feed: page/feed parity over the item kinds. The stored
        # chronology is user text, one tool call, the empty-content
        # assistant carrier (recovered commentary), final text — so the
        # display order is text / tools / commentary / text.
        feed = _get_json(proc.url("/s/default/sess-live/feed?after=0"))
        kinds = [(m["kind"], m.get("role")) for m in feed["messages"]]
        assert kinds == [("text", "user"), ("tools", ""),
                         ("text", "assistant"), ("text", "assistant")]
        assert feed["messages"][2]["text"].startswith("Stepping into")
        assert "tool-group" in feed["messages"][1]["html"]
        last_id = feed["last_id"]

        # Delta feed: a row inserted after serving started is observed
        # exactly once, then the cursor goes quiet.
        con = sqlite3.connect(str(serve_home / "state.db"))
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp)"
            " VALUES ('sess-live', 'assistant', 'delta answer', ?)",
            (time.time(),))
        con.commit()
        con.close()
        deadline = time.time() + 10
        delta = {"messages": []}
        while time.time() < deadline:
            delta = _get_json(
                proc.url("/s/default/sess-live/feed?after=%d" % last_id))
            if delta["messages"]:
                break
            time.sleep(0.1)
        assert [(m["kind"], m.get("role")) for m in delta["messages"]] == \
            [("text", "assistant")]
        assert delta["messages"][0]["text"] == "delta answer"
        quiet = _get_json(
            proc.url("/s/default/sess-live/feed?after=%d"
                     % delta["last_id"]))
        assert quiet["messages"] == []

        # Unknown paths stay 404; /new serves the blank composer.
        assert _get_text(proc.url("/nope"))[0] == 404
        assert _get_text(proc.url("/static/user.png"))[0] == 404
        assert _get_text(proc.url("/new"))[0] == 200
    finally:
        code = proc.stop()
    assert code == 0, proc.output()


def test_real_serve_non_loopback_warns(serve_home):
    proc = _ServeProcess(serve_home, "--host", "127.0.0.1",
                         "--no-discord-sync")
    try:
        startup = proc.wait_ready()
        assert "WARNING" not in startup  # loopback needs no warning
    finally:
        assert proc.stop() == 0

    proc = _ServeProcess(serve_home, "--host", "0.0.0.0",
                         "--no-discord-sync")
    try:
        startup = proc.wait_ready()
        assert "WARNING: binding 0.0.0.0" in startup
        assert "no built-in authentication" in startup
    finally:
        assert proc.stop() == 0
