#!/usr/bin/env python3
"""Focused tests for truthful send progress in the Mission Control UI.

Covers the send-progress contract end to end at the HTTP layer: the
/s/new launch is accepted with a fast 202 while a stubbed hermes run is
still blocked, the correlated session row is published on the status
route BEFORE the run completes, a terminal failure is safe and never
retried, a duplicate launch can never spawn a second run, the shipped
client source carries the distinct transport/waiting transitions, and
first-response detection is scoped after the newly accepted turn (a
historical assistant row never satisfies a new one), and the live
sidebar transition (a send moves the conversation row to Active and
back to Open · completed without any page reload — the server keeps
rendering the sections for the very same URL, and the shipped client
re-renders #rows from it after the 202 and on busy-state transitions
only, with last-request-wins protection against stale responses).

The hermes binary is replaced per test by a small Python stub whose
behavior is baked into the file (never into argv or env), so nothing
here talks to a real Hermes. The prompt text uses a recognizable
marker on purpose: every status/registry surface is asserted not to
contain it (the transcript itself legitimately does).
"""

import importlib.util
import itertools
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SERVER_PY = os.path.join(REPO, "plugins", "mission_control", "server.py")

# Unique module names for the per-test server instances (spec loading
# never registers them in sys.modules; the name is for tracebacks).
_MODULE_SEQ = itertools.count()

# Recognizable marker carried by every composer payload the tests send.
PROMPT = "SECRET-PROMPT-marker-4b93"

SESSION_SCHEMA = """
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

# The stub hermes: behavior, the main DB path and its control dir are
# baked into the file at write time (nothing rides argv or the
# environment), so a stub never sees another test's control files. The
# calls log records ONLY the first argv token — never the prompt.
STUB_TEMPLATE = '''#!/usr/bin/env python3
import os, sqlite3, sys, time

DB = %(db)r
DIR = %(dir)r
BEHAVIOR = %(behavior)r

argv = sys.argv[1:]
with open(os.path.join(DIR, "calls"), "a") as fh:
    fh.write("CALL " + (argv[0] if argv else "?") + "\\n")


def prompt():
    for i, a in enumerate(argv):
        if a == "-q" and i + 1 < len(argv):
            return argv[i + 1]
    return ""


def session_id():
    try:
        with open(os.path.join(DIR, "session-id")) as fh:
            return fh.read().strip()
    except OSError:
        return "20260902_stub_default"


def add_message(sid, role, content, tool_name=None):
    con = sqlite3.connect(DB, timeout=10)
    con.execute(
        "INSERT INTO messages (session_id, role, content, tool_name,"
        " timestamp) VALUES (?,?,?,?,?)",
        (sid, role, content, tool_name, time.time()))
    con.commit()
    con.close()


if BEHAVIOR in ("new-block", "new-fail-after-create"):
    sid = session_id()
    con = sqlite3.connect(DB, timeout=10)
    now = time.time()
    con.execute(
        "INSERT OR REPLACE INTO sessions (id, source, title, started_at,"
        " last_activity_at, archived, hidden) VALUES (?,?,?,?,?,0,0)",
        (sid, "mission-control", "stub session", now, now))
    con.commit()
    con.close()
    add_message(sid, "user", prompt())
    sys.stderr.write("session_id: " + sid + "\\n")
    sys.stderr.flush()
    if BEHAVIOR == "new-block":
        rel = os.path.join(DIR, "release")
        for _ in range(2400):
            if os.path.exists(rel):
                break
            time.sleep(0.05)
    sys.exit(3 if BEHAVIOR == "new-fail-after-create" else 0)

if BEHAVIOR == "new-fail":
    sys.exit(3)

if BEHAVIOR == "reply-block":
    sid = argv[1] if len(argv) > 1 and argv[0] == "--resume" else None
    rel = os.path.join(DIR, "release")
    for _ in range(2400):
        if os.path.exists(rel):
            break
        time.sleep(0.05)
    if sid:
        add_message(sid, "assistant", "stub answer: done")
    sys.exit(0)

sys.exit(0)
'''


def load_server(tmp, db_path):
    """One isolated server.py module instance per test: its MAIN_DB and
    profile glob point at the test fixture, its hermes binary at a
    stub, and its in-memory job registries start empty."""
    spec = importlib.util.spec_from_file_location(
        "mc_server_under_test_%d" % _MODULE_SEQ.__next__(), SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.MAIN_DB = db_path
    mod.PROFILE_GLOB = os.path.join(tmp, "no-such-profile", "*", "state.db")
    return mod


class ServerCase(unittest.TestCase):
    """A real ThreadingHTTPServer on an ephemeral port over a synthetic
    state.db, plus the stub-hermes writer."""

    # seconds a poll helper will wait for a condition before failing
    POLL_TIMEOUT = 15.0

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sendprog-test-")
        self.db = os.path.join(self.tmp, "state.db")
        con = sqlite3.connect(self.db)
        con.executescript(SESSION_SCHEMA)
        con.commit()
        con.close()
        self.mod = load_server(self.tmp, self.db)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                         self.mod.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever,
                         daemon=True).start()

    def tearDown(self):
        # Unblock every stub, then wait for this module's jobs to drain
        # so nothing writes into a torn-down tmp dir.
        try:
            with open(os.path.join(self.tmp, "release"), "w") as fh:
                fh.write("go")
        except OSError:
            pass
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            with self.mod._new_jobs_lock:
                live_new = [j for j in self.mod._new_jobs.values()
                            if j["state"] in self.mod.NEW_JOB_LIVE_STATES]
            with self.mod._jobs_lock:
                live_reply = len(self.mod._jobs)
            if not live_new and not live_reply:
                break
            time.sleep(0.05)
        self.mod.terminate_children()
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- fixture helpers -------------------------------------------

    def write_stub(self, behavior):
        """Install the behavior's stub as the module's hermes binary."""
        path = os.path.join(self.tmp, "hermes-stub")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(STUB_TEMPLATE % {
                "db": self.db, "dir": self.tmp, "behavior": behavior})
        os.chmod(path, 0o755)
        self.mod.HERMES_BIN = path

    def set_stub_session(self, sid):
        with open(os.path.join(self.tmp, "session-id"), "w") as fh:
            fh.write(sid)

    def release(self):
        with open(os.path.join(self.tmp, "release"), "w") as fh:
            fh.write("go")

    def unrelease(self):
        try:
            os.remove(os.path.join(self.tmp, "release"))
        except OSError:
            pass

    def call_count(self):
        """How many stub invocations happened (never counts text)."""
        try:
            with open(os.path.join(self.tmp, "calls")) as fh:
                return len([ln for ln in fh if ln.startswith("CALL")])
        except OSError:
            return 0

    def wait_calls(self, n):
        """Block until the stub has demonstrably started n runs — the
        202 can beat the child's first write by a few milliseconds,
        which is the very behavior under test."""
        deadline = time.monotonic() + self.POLL_TIMEOUT
        while time.monotonic() < deadline:
            if self.call_count() >= n:
                return
            time.sleep(0.05)
        self.fail("stub was invoked %d times, expected at least %d"
                  % (self.call_count(), n))

    def add_session(self, sid, source="mission-control", title="fixture"):
        con = sqlite3.connect(self.db)
        now = time.time()
        con.execute(
            "INSERT OR REPLACE INTO sessions (id, source, title,"
            " started_at, last_activity_at, archived, hidden)"
            " VALUES (?,?,?,?,?,0,0)", (sid, source, title, now, now))
        con.commit()
        con.close()

    def add_message(self, sid, role, content, tool_name=None):
        con = sqlite3.connect(self.db, timeout=10)
        con.execute(
            "INSERT INTO messages (session_id, role, content, tool_name,"
            " timestamp) VALUES (?,?,?,?,?)",
            (sid, role, content, tool_name, time.time()))
        con.commit()
        con.close()

    # ---- HTTP helpers ----------------------------------------------

    def request(self, method, path, obj=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = None
        headers = {}
        if obj is not None:
            data = json.dumps(obj).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def request_json(self, method, path, obj=None):
        status, body = self.request(method, path, obj)
        return status, json.loads(body)

    def poll_status(self, path, want):
        """GET <path> until its JSON satisfies want(payload); returns the
        last payload (never raises on 404 — returns the parsed body)."""
        deadline = time.monotonic() + self.POLL_TIMEOUT
        last = {}
        while time.monotonic() < deadline:
            try:
                status, payload = self.request_json("GET", path)
                last = payload
                if status == 200 and want(payload):
                    return payload
            except (ValueError, urllib.error.URLError):
                pass
            time.sleep(0.1)
        self.fail("status %s never satisfied the condition; last: %r"
                  % (path, last))


class TestNewSessionAsync(ServerCase):
    """Contract 2: fast acceptance, mid-run session discovery, honest
    completion — and nothing about the prompt anywhere near the status
    API."""

    def test_accepted_fast_and_session_published_before_completion(self):
        self.write_stub("new-block")
        self.set_stub_session("20260902_newjob_a1")
        t0 = time.monotonic()
        status, body = self.request_json("POST", "/s/new",
                                         {"text": PROMPT})
        elapsed = time.monotonic() - t0
        self.assertEqual(status, 202)
        # acceptance is prompt while the stubbed run stays blocked
        self.assertLess(elapsed, 2.0)
        job = body["job"]
        self.assertRegex(job, r"^[A-Za-z0-9_-]{8,}$")
        self.assertEqual(body["status_url"], "/s/new/" + job)
        self.wait_calls(1)  # the one background run really started

        # the correlated session row is published WHILE the run is live
        st = self.poll_status(body["status_url"],
                              lambda p: bool(p.get("session_id")))
        self.assertIn(st["status"], ("starting", "running"))
        self.assertEqual(st["session_id"], "20260902_newjob_a1")
        self.assertEqual(st["url"], "/s/default/20260902_newjob_a1")
        self.assertNotIn(PROMPT, json.dumps(st))
        self.assertEqual(self.call_count(), 1)  # still the same single run

        # the fresh session page is already live: busy strip in the
        # pre-first-output state, exactly the waiting wording
        code, page = self.request("GET", st["url"])
        self.assertEqual(code, 200)
        self.assertIn("Waiting for first response", page)
        self.assertIn('id="live-activity"', page)

        # releasing the run settles the job as done, same session
        self.release()
        done = self.poll_status(body["status_url"],
                                lambda p: p.get("status") == "done")
        self.assertEqual(done["session_id"], "20260902_newjob_a1")
        self.assertEqual(self.call_count(), 1)

    def test_status_route_is_safe_and_terminal_on_failure(self):
        self.write_stub("new-fail-after-create")
        self.set_stub_session("20260902_newjob_f1")
        status, body = self.request_json("POST", "/s/new",
                                         {"text": PROMPT})
        self.assertEqual(status, 202)
        failed = self.poll_status(body["status_url"],
                                  lambda p: p.get("status") == "failed")
        # safe canned reason, no prompt, no stub output
        self.assertIn("exit code 3", failed["error"])
        self.assertNotIn(PROMPT, json.dumps(failed))
        # terminal: repeated polls keep the same verdict
        for _ in range(3):
            status2, again = self.request_json("GET", body["status_url"])
            self.assertEqual(status2, 200)
            self.assertEqual(again["status"], "failed")
        # no retry ever happened
        self.assertEqual(self.call_count(), 1)
        # a client that had navigated to the created session gets the
        # failure note from the feed exactly once
        first = self.poll_status(
            "/s/default/20260902_newjob_f1/feed?after=0",
            lambda p: p.get("note"))
        self.assertIn("new-session run failed", first["note"])
        self.assertNotIn(PROMPT, first["note"])
        _status, second = self.request_json(
            "GET", "/s/default/20260902_newjob_f1/feed?after=0")
        self.assertNotIn("note", second)
        self.assertFalse(second["busy"])

    def test_failed_launch_without_session_is_terminal(self):
        self.write_stub("new-fail")
        status, body = self.request_json("POST", "/s/new",
                                         {"text": PROMPT})
        self.assertEqual(status, 202)
        failed = self.poll_status(body["status_url"],
                                  lambda p: p.get("status") == "failed")
        self.assertFalse(failed["session_id"])
        self.assertEqual(failed["error"], "the launch failed (exit code 3)")
        self.assertNotIn(PROMPT, json.dumps(failed))
        self.assertEqual(self.call_count(), 1)

    def test_unknown_job_id_is_404(self):
        status, body = self.request_json("GET", "/s/new/no-such-job")
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])


class TestDuplicateLaunch(ServerCase):
    """Contract 2: exactly one live launch — a concurrent POST can never
    spawn a second run, and the gate reopens once the run settles."""

    def test_second_post_while_live_is_refused(self):
        self.write_stub("new-block")
        self.set_stub_session("20260902_dup_1")
        status1, first = self.request_json("POST", "/s/new",
                                           {"text": PROMPT})
        self.assertEqual(status1, 202)
        status2, second = self.request_json("POST", "/s/new",
                                            {"text": PROMPT})
        self.assertEqual(status2, 409)
        self.assertFalse(second["ok"])
        self.wait_calls(1)

        self.release()
        self.poll_status(first["status_url"],
                         lambda p: p.get("status") == "done")
        self.assertEqual(self.call_count(), 1)

        # settled: the next launch is accepted again (still serialized)
        self.unrelease()
        self.set_stub_session("20260902_dup_2")
        status3, third = self.request_json("POST", "/s/new",
                                           {"text": PROMPT})
        self.assertEqual(status3, 202)
        # The third run starts genuinely blocked (unrelease above), so
        # it lives its full lifecycle: releasing lets the stub exit, and
        # only that genuine exit may settle the job done.
        self.release()
        self.poll_status(third["status_url"],
                         lambda p: p.get("status") == "done")
        self.assertEqual(self.call_count(), 2)


class TestReplyProgress(ServerCase):
    """Contract 1/4, server side: /reply keeps its fast 202, the feed
    reports the turn busy with the waiting state until this turn's
    first output lands, then the specific activity takes over."""

    def setUp(self):
        super().setUp()
        self.add_session("s_reply_1", source="cli", title="reply fixture")
        self.add_message("s_reply_1", "user", "earlier turn")
        self.add_message("s_reply_1", "assistant",
                         "earlier answer (historical)")

    def test_202_is_fast_and_feed_waiting_then_answered(self):
        self.write_stub("reply-block")
        t0 = time.monotonic()
        status, body = self.request_json(
            "POST", "/s/default/s_reply_1/reply", {"text": PROMPT})
        elapsed = time.monotonic() - t0
        self.assertEqual(status, 202)
        self.assertLess(elapsed, 2.0)
        self.wait_calls(1)

        # one in-flight reply per session: a second send is refused
        # while the first is still running (no duplicate run)
        status_again, _body = self.request_json(
            "POST", "/s/default/s_reply_1/reply", {"text": PROMPT})
        self.assertEqual(status_again, 409)
        self.assertEqual(self.call_count(), 1)

        # busy + the pre-first-output waiting state (the historical
        # assistant row above must NOT satisfy this new turn)
        feed = self.poll_status("/s/default/s_reply_1/feed?after=0",
                                lambda p: p.get("busy"))
        self.assertTrue(feed["busy"])
        self.assertEqual(feed["activity"]["state"],
                         self.mod.WAITING_FIRST_RESPONSE)
        self.assertIn("Waiting for first response",
                      feed["activity"]["html"])

        # the answer lands: busy clears, the strip goes away. The row
        # commit and the job settle are distinct events milliseconds
        # apart (the child writes its answer, then exits, then the job
        # releases the busy key), so the poll waits for the settled
        # end state — answer present AND busy gone — not merely the
        # first row.
        self.release()
        answered = self.poll_status(
            "/s/default/s_reply_1/feed?after=0",
            lambda p: any(m.get("role") == "assistant"
                          for m in p.get("messages", []))
            and not p.get("busy"))
        self.assertFalse(answered["busy"])
        self.assertEqual(answered["activity"]["html"], "")
        self.assertEqual(self.call_count(), 1)


class TestClientSourceTransitions(ServerCase):
    """Contract 1/3, shipped source: both pages carry the distinct
    transport ticks and the separate waiting indicator, /new carries
    the job-poll flow with its bound, and neither page mixes them."""

    def test_new_page_source(self):
        status, page = self.request("GET", "/new")
        self.assertEqual(status, 200)
        # transport ticks: Sending exists as a tick state only…
        self.assertIn('word = "Sending…"', page)
        self.assertIn('"sent"', page)          # the acceptance transition
        self.assertIn('"failed"', page)        # the failure transition
        # …and the separate waiting indicator is its own row
        self.assertIn('id="waiting-row"', page)
        self.assertIn("Waiting for first response", page)
        self.assertIn("waiting-text", page)    # its CSS shipped too
        # the job-poll flow with its hard bound
        self.assertIn("status_url", page)
        self.assertIn("pollJob", page)
        self.assertIn("JOB_MAX_MS", page)
        # no session exists yet: never a typing row or a live strip
        self.assertNotIn('id="typing-row"', page)
        self.assertNotIn('id="live-activity"', page)

    def test_chat_page_source(self):
        self.add_session("s_src_1")
        self.add_message("s_src_1", "user", "hello")
        status, page = self.request("GET", "/s/default/s_src_1")
        self.assertEqual(status, 200)
        self.assertIn('id="waiting-row"', page)
        self.assertIn("Waiting for first response", page)
        self.assertIn('id="typing-row"', page)
        # first-response detection is scoped: ids are compared against
        # the turn's floor, which adoption raises to the turn's own
        # user row — historical rows cannot pass it
        self.assertIn("turnFloor", page)
        self.assertIn("turnOutputs[t] > turnFloor", page)
        self.assertIn("beginTurn", page)
        # the waiting row yields to the live strip once output exists
        self.assertIn("setWaiting", page)


class TestLiveSidebarRefresh(ServerCase):
    """The conversation sidebar follows a send without a page reload.

    The server keeps classifying the very same chat URL (Active while
    a reply job this server runs owns the session, Open · completed
    once the turn settles), and the shipped client re-renders #rows
    from that URL: immediately after /reply's 202, and again on this
    session's busy-state transitions only — never per feed poll —
    behind last-request-wins guards on both the page fetch and the
    feed polls."""

    def selected_section(self, page):
        """data-section of the sidebar section holding the selected
        conversation row (fails the test when nothing is selected)."""
        for m in re.finditer(
                r'<(section|details) class="convsec"[^>]*'
                r'data-section="([a-z]+)"', page):
            close = page.find("</%s>" % m.group(1), m.end())
            block = page[m.end():close]
            if 'class="conv is-selected' in block:
                return m.group(2)
        self.fail("no selected conversation row found in the sidebar")

    def test_completed_answer_renders_under_open_completed(self):
        self.add_session("s_sb_done", source="cli", title="settled turn")
        self.add_message("s_sb_done", "user", "the question")
        self.add_message("s_sb_done", "assistant", "the answer")
        status, page = self.request("GET", "/s/default/s_sb_done")
        self.assertEqual(status, 200)
        # a completed assistant answer classifies the row Open ·
        # completed, and the chat page's own URL renders it selected
        self.assertEqual(self.selected_section(page), "completed")
        self.assertIn("Open · completed", page)

    def test_row_moves_to_active_while_reply_runs_then_back(self):
        self.write_stub("reply-block")
        self.add_session("s_sb_move", source="cli", title="moving row")
        self.add_message("s_sb_move", "user", "the question")
        self.add_message("s_sb_move", "assistant", "the answer")

        status, page = self.request("GET", "/s/default/s_sb_move")
        self.assertEqual(status, 200)
        self.assertEqual(self.selected_section(page), "completed")

        # the send is accepted fast while the stubbed reply is blocked
        status, _body = self.request_json(
            "POST", "/s/default/s_sb_move/reply", {"text": PROMPT})
        self.assertEqual(status, 202)
        self.wait_calls(1)

        # _jobs now owns the session, so the SAME chat URL — the one
        # the client's no-reload refresh fetches — server-renders the
        # selected row under Active
        status, active_page = self.request("GET", "/s/default/s_sb_move")
        self.assertEqual(status, 200)
        self.assertEqual(self.selected_section(active_page), "active")

        # settle the turn: the answer lands and the busy key clears,
        # after which the same URL renders the row completed again
        self.release()
        self.poll_status(
            "/s/default/s_sb_move/feed?after=0",
            lambda p: any(m.get("role") == "assistant"
                          for m in p.get("messages", []))
            and not p.get("busy"))
        status, done_page = self.request("GET", "/s/default/s_sb_move")
        self.assertEqual(status, 200)
        self.assertEqual(self.selected_section(done_page), "completed")
        self.assertEqual(self.call_count(), 1)

    def test_chat_client_carries_the_no_reload_refresh_path(self):
        self.add_session("s_sb_src")
        self.add_message("s_sb_src", "user", "hello")
        status, page = self.request("GET", "/s/default/s_sb_src")
        self.assertEqual(status, 200)
        # the shared helper ships in the sidebar script: it re-fetches
        # the current page URL without the cache, swaps in ONLY the
        # fresh #rows, and rebinds the existing MC behavior over them
        self.assertIn("function refreshRows()", page)
        self.assertIn(
            'window.fetch(window.location.href, { cache: "no-store" })',
            page)
        self.assertIn('doc.getElementById("rows")', page)
        self.assertIn("current.parentNode.replaceChild(fresh, current)",
                      page)
        self.assertIn("bindClosed();", page)
        self.assertIn("applyFilter();", page)
        self.assertIn("tickRelatives();", page)
        self.assertIn("refreshRows: refreshRows", page)
        # stale-response safety: only the newest page fetch may swap
        self.assertIn("gen !== rowsGen", page)
        # invoked immediately after the chat-mode /reply 202 (the
        # second 202 branch in the source; /new's comes first)
        first202 = page.index("resp.status === 202")
        chat202 = page.index("resp.status === 202", first202 + 1)
        next409 = page.index("resp.status === 409", chat202)
        branch = page[chat202:next409]
        self.assertIn("beginTurn()", branch)
        self.assertIn("refreshSidebar()", branch)
        self.assertLess(branch.index("beginTurn()"),
                        branch.index("refreshSidebar()"))
        # invoked again ONLY on a busy-state transition, never per poll
        self.assertIn("var wasBusy = busy;", page)
        self.assertIn("if (wasBusy !== busy) refreshSidebar();", page)
        # a feed response started before a newer one (or before the
        # accepted send) never applies its stale busy verdict
        self.assertIn("seq === feedSeq", page)
        bt = page.index("function beginTurn()")
        bset = page.index("setWaiting();", bt)
        self.assertLess(page.index("feedSeq++;", bt), bset)

    def test_inbox_auto_refresh_still_ships(self):
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("data-refresh=", page)
        self.assertIn(
            "window.setInterval(refresh, refreshSeconds * 1000)", page)
        self.assertIn("window.MC.relist()", page)


class TestFirstResponseScoping(unittest.TestCase):
    """Contract 1/4, detection itself: pre-first-output is the waiting
    state only when no assistant/tool row follows the turn's own user
    row; a historical answer never satisfies a new turn."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sendprog-scope-")
        self.db = os.path.join(self.tmp, "state.db")
        con = sqlite3.connect(self.db)
        con.executescript(SESSION_SCHEMA)
        con.commit()
        con.close()
        self.mod = load_server(self.tmp, self.db)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def rows(self, *specs):
        """specs are (role, content[, tool_name]) appended to one
        session; returns an open read-only connection for
        compute_activity."""
        sid = "s_scope"
        con = sqlite3.connect(self.db)
        con.execute("INSERT OR REPLACE INTO sessions (id, source, title,"
                    " started_at, last_activity_at, archived, hidden)"
                    " VALUES (?,?,?,?,?,0,0)",
                    (sid, "cli", "scope", time.time(), time.time()))
        for i, spec in enumerate(specs):
            role = spec[0]
            con.execute(
                "INSERT INTO messages (session_id, role, content,"
                " tool_name, timestamp) VALUES (?,?,?,?,?)",
                (sid, role, spec[1], spec[2] if len(spec) > 2 else None,
                 1000.0 + i))
        con.commit()
        con.close()
        return sqlite3.connect("file:%s?mode=ro" % self.db, uri=True)

    def test_historical_answer_never_satisfies_new_turn(self):
        con = self.rows(
            ("user", "old question"),
            ("assistant", "old answer"),          # historical, before…
            ("user", "the newly accepted turn"),   # …this turn's floor
        )
        try:
            act = self.mod.compute_activity(con, "s_scope", 2000.0,
                                            busy_job=True)
            self.assertTrue(act["active"])
            self.assertEqual(act["state"], self.mod.WAITING_FIRST_RESPONSE)
        finally:
            con.close()

    def test_first_output_replaces_waiting_state(self):
        con = self.rows(
            ("user", "old question"),
            ("assistant", "old answer"),
            ("user", "the newly accepted turn"),
            ("tool", "…", "read_file"),   # this turn's first output
        )
        try:
            act = self.mod.compute_activity(con, "s_scope", 2000.0,
                                            busy_job=True)
            self.assertEqual(act["state"], "Thinking after read_file")
            self.assertNotEqual(act["state"], self.mod.WAITING_FIRST_RESPONSE)
        finally:
            con.close()

    def test_waiting_state_for_brand_new_and_quiet_turns(self):
        for specs in ([("user", "first ever message")], []):
            con = self.rows(*specs)
            try:
                act = self.mod.compute_activity(con, "s_scope", 2000.0,
                                                busy_job=True)
                self.assertEqual(act["state"],
                                 self.mod.WAITING_FIRST_RESPONSE)
            finally:
                con.close()

    def test_idle_turn_shows_nothing(self):
        con = self.rows(("user", "question"), ("assistant", "answer"))
        try:
            act = self.mod.compute_activity(con, "s_scope", 2000.0,
                                            busy_job=False)
            self.assertFalse(act["active"])
            self.assertEqual(self.mod.render_activity(act), "")
        finally:
            con.close()


class TestJobRegistryBound(unittest.TestCase):
    """Contract 2/3: the launch registry is bounded and old terminal
    jobs are cleaned; live jobs are never dropped."""

    def test_cap_and_ttl_pruning(self):
        tmp = tempfile.mkdtemp(prefix="sendprog-reg-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        mod = load_server(tmp, os.path.join(tmp, "state.db"))
        now = 5000.0
        with mod._new_jobs_lock:
            for i in range(mod.NEW_JOBS_MAX + 10):
                mod._new_jobs["j%d" % i] = {
                    "state": mod.NEW_JOB_DONE, "session_id": "s%d" % i,
                    "error": "", "created": now - 10 * mod.NEW_JOB_TTL_SECONDS,
                    "finished": now - 10 * mod.NEW_JOB_TTL_SECONDS + i,
                }
            mod._new_jobs["live"] = {"state": mod.NEW_JOB_RUNNING,
                                     "session_id": None, "error": "",
                                     "created": now, "finished": None}
            mod._prune_new_jobs_locked(now)
            # everything terminal is far past the TTL: all gone, the
            # live job survives
            self.assertEqual(len(mod._new_jobs), 1)
            self.assertIn("live", mod._new_jobs)

        # fresh terminal jobs (inside the TTL) survive until the cap
        # forces the oldest out
        with mod._new_jobs_lock:
            mod._new_jobs.clear()
            for i in range(mod.NEW_JOBS_MAX + 5):
                mod._new_jobs["k%d" % i] = {
                    "state": mod.NEW_JOB_FAILED, "session_id": None,
                    "error": "x", "created": now + i, "finished": now + i,
                }
            mod._prune_new_jobs_locked(now + 100)
            self.assertLessEqual(len(mod._new_jobs), mod.NEW_JOBS_MAX)
            # oldest-first: k0..k4 are the ones that had to go
            for i in range(5):
                self.assertNotIn("k%d" % i, mod._new_jobs)
            self.assertIn("k%d" % (mod.NEW_JOBS_MAX + 4), mod._new_jobs)

    def test_payload_shape_holds_no_secrets(self):
        tmp = tempfile.mkdtemp(prefix="sendprog-pay-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        mod = load_server(tmp, os.path.join(tmp, "state.db"))
        with mod._new_jobs_lock:
            mod._new_jobs["abc123"] = {
                "state": mod.NEW_JOB_RUNNING, "session_id": "s_1",
                "error": "", "created": 1.0, "finished": None}
        payload = mod.new_job_payload("abc123")
        self.assertEqual(
            sorted(payload),
            ["error", "job", "ok", "session_id", "status", "url"])
        self.assertIsNone(mod.new_job_payload("missing"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
