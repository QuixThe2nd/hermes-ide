#!/usr/bin/env python3
"""Focused tests for truthful send progress in the Mission Control UI.

Covers the send-progress contract end to end at the HTTP layer, with
the composer transport the repair installed: every turn is a run on
the core API server (never a oneshot CLI child), so the core is faked
in-process at the module's core_api_request seam. The /s/new launch is
accepted with a fast 202 while the faked run is still executing, the
deterministic session id from the admission is published on the status
route BEFORE the run completes (and, for a run that fails fast, still
published once its row exists), a terminal failure is safe and never
retried, a duplicate launch can never admit a second run, a rejected
reply (409 busy, core unavailable) is an explicit failed send rather
than a silently delivered one, and first-response detection is scoped
after the newly accepted turn (a historical assistant row never
satisfies a new one), plus the live sidebar transition (a send moves
the conversation row to Active and back to Open · completed without
any page reload).

The fake core records method and path (and, for admissions, the exact
payload it was given — asserted, never logged) and simulates the run
lifecycle the real /v1/runs serves, including writing the
session/message rows the pages read, so nothing here talks to a real
Hermes or spawns any child. The prompt text uses a recognizable marker
on purpose: every status/registry surface is asserted not to contain
it (the transcript itself legitimately does).
"""

import importlib.util
import itertools
import json
import html
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
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

# The production schema, imported from core: the listing is now served
# by the core projection (list_sessions_rich), so fixture DBs must
# answer exactly the SQL the live ones do — the synthetic subset below
# predated that and lacks the columns the projection reads.
sys.path.insert(0, REPO)

from hermes_state_common import SCHEMA_SQL  # noqa: E402

SESSION_SCHEMA = SCHEMA_SQL


def load_server(tmp, db_path):
    """One isolated server.py module instance per test: its MAIN_DB and
    profile glob point at the test fixture, its core API client at the
    in-process fake, and its in-memory job registries start empty."""
    spec = importlib.util.spec_from_file_location(
        "mc_server_under_test_%d" % _MODULE_SEQ.__next__(), SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.MAIN_DB = db_path
    mod.PROFILE_GLOB = os.path.join(tmp, "no-such-profile", "*", "state.db")
    return mod


class FakeCore:
    """In-process stand-in for the core API server the composer talks to.

    Installed over the module's core_api_request seam. Behaviors bake
    the run lifecycle in (never argv or env): admissions answer the
    202 contract — run_id plus the canonical session_id — and, like the
    real admitted agent, write the session/message rows the pages read,
    so navigation and feed states exercise the real SQL. The calls list
    records method and path; admissions also remember the exact payload
    they were handed (asserted by the tests, never logged).
    """

    def __init__(self, db, tmp):
        self.db = db
        self.tmp = tmp
        self.lock = threading.Lock()
        self.calls = []
        self.admissions = []       # payloads of every POST /v1/runs
        self.behavior = None       # None: every call errors (no core)
        self.runs = {}             # run_id -> {"sid":…, "kind":…}

    # -- lifecycle control -------------------------------------------

    def set_behavior(self, behavior):
        self.behavior = behavior

    def session_id(self):
        try:
            with open(os.path.join(self.tmp, "session-id")) as fh:
                return fh.read().strip()
        except OSError:
            return "20260902_stub_default"

    def released(self):
        return os.path.exists(os.path.join(self.tmp, "release"))

    # -- the seam ------------------------------------------------------

    def __call__(self, method, path, profile, dbs, payload=None,
                 timeout=None):
        with self.lock:
            self.calls.append((method, path))
        if self.behavior is None:
            return 0, None, "unreachable"
        if method == "POST" and path == "/v1/runs":
            return self._admit(payload)
        if method == "GET" and path.startswith("/v1/runs/"):
            return self._status(path.rsplit("/", 1)[-1])
        return 200, {"pending_clarify": None}, None

    def _admit(self, payload):
        with self.lock:
            self.admissions.append(dict(payload or {}))
        if self.behavior == "new-fail":
            # the core itself unreachable: transport error, no body
            return 0, None, "unavailable"
        run_id = "run_fake_%d" % (len(self.admissions))
        if self.behavior in ("new-block", "new-fail-after-create"):
            sid = self.session_id()
            self._create_session_row(sid)
            self._add_message(sid, "user", PROMPT)
            kind = self.behavior
        else:  # reply behaviors run the session the composer named
            sid = (payload or {}).get("session_id") or ""
            kind = self.behavior
        with self.lock:
            self.runs[run_id] = {"sid": sid, "kind": kind}
        return 202, {"run_id": run_id, "status": "started",
                     "session_id": sid, "replayed": False}, None

    def _status(self, run_id):
        with self.lock:
            run = self.runs.get(run_id)
        if run is None:
            return 404, {"error": "not found"}, None
        if run["kind"] in ("new-block", "reply-block"):
            if not self.released():
                return 200, {"run_id": run_id, "status": "running",
                             "session_id": run["sid"]}, None
            if run["kind"] == "reply-block":
                self._add_message(run["sid"], "assistant",
                                  "stub answer: done")
            return 200, {"run_id": run_id, "status": "completed",
                         "session_id": run["sid"]}, None
        if run["kind"] == "new-fail-after-create":
            return 200, {"run_id": run_id, "status": "failed",
                         "session_id": run["sid"]}, None
        return 200, {"run_id": run_id, "status": "completed",
                     "session_id": run["sid"]}, None

    # -- the rows the real agent would persist ------------------------

    def _create_session_row(self, sid):
        con = sqlite3.connect(self.db, timeout=10)
        now = time.time()
        con.execute(
            "INSERT OR REPLACE INTO sessions (id, source, title, started_at,"
            " last_activity_at, archived, hidden) VALUES (?,?,?,?,?,0,0)",
            (sid, "mission-control", "stub session", now, now))
        con.commit()
        con.close()

    def _add_message(self, sid, role, content):
        con = sqlite3.connect(self.db, timeout=10)
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp)"
            " VALUES (?,?,?,?)", (sid, role, content, time.time()))
        con.commit()
        con.close()

    # -- assertions ---------------------------------------------------

    def admission_count(self):
        with self.lock:
            return len(self.admissions)


class ServerCase(unittest.TestCase):
    """A real ThreadingHTTPServer on an ephemeral port over a synthetic
    state.db, with the core API faked in-process — and a tripwire that
    fails the test if anything ever tries to spawn a CLI child."""

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
        self.core = FakeCore(self.db, self.tmp)
        self._core_patch = unittest.mock.patch.object(
            self.mod, "core_api_request", side_effect=self.core)
        self._core_patch.start()
        self.addCleanup(self._core_patch.stop)
        # The transport contract under test: a composer turn may talk
        # to the core API, but it may never fall back to a child.
        self._spawns = []
        for name in ("Popen", "run"):
            patcher = unittest.mock.patch.object(
                subprocess, name,
                side_effect=lambda *a, **k: self._spawns.append((name, a)))
            patcher.start()
            self.addCleanup(patcher.stop)
        self._csrf = None
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                         self.mod.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever,
                         daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.drain_jobs)
        self.addCleanup(self.assert_no_children)

    def drain_jobs(self):
        # Unblock every faked run, then wait for this module's jobs to
        # drain so nothing writes into a torn-down tmp dir.
        self.release()
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            with self.mod._new_jobs_lock:
                live_new = [j for j in self.mod._new_jobs.values()
                            if j["state"] in self.mod.NEW_JOB_LIVE_STATES]
            with self.mod._jobs_lock:
                live_reply = len(self.mod._jobs)
            if not live_new and not live_reply:
                return
            time.sleep(0.05)

    def assert_no_children(self):
        self.assertEqual(self._spawns, [],
                         "a composer turn spawned a CLI child")

    # ---- fixture helpers -------------------------------------------

    def write_stub(self, behavior):
        """Point the faked core at one run behavior (name kept from the
        oneshot era so the scenarios read the same)."""
        self.core.set_behavior(behavior)

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
        """How many runs were admitted (never counts text)."""
        return self.core.admission_count()

    def wait_calls(self, n):
        """Block until n admissions demonstrably happened — the 202 can
        beat the worker's first poll by a few milliseconds, which is
        the very behavior under test."""
        deadline = time.monotonic() + self.POLL_TIMEOUT
        while time.monotonic() < deadline:
            if self.call_count() >= n:
                return
            time.sleep(0.05)
        self.fail("core admitted %d runs, expected at least %d"
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

    def csrf_token(self):
        """The token a real client mines from a served page's meta tag.

        The server emits its per-process CSRF token only to pages it
        serves, so this is exactly how the shipped client obtains it."""
        if self._csrf is None:
            status, page = self.request("GET", "/new")
            self.assertEqual(status, 200)
            m = re.search(
                r'<meta name="mission-control-csrf" content="([^"]*)"',
                page)
            self.assertIsNotNone(m, "served page carries the CSRF meta")
            self._csrf = html.unescape(m.group(1))
        return self._csrf

    def request(self, method, path, obj=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = None
        headers = {}
        if obj is not None:
            data = json.dumps(obj).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if method == "POST":
            # The real UI's send shape: same-origin, JSON body, the
            # token in a non-simple header.
            headers["Origin"] = "http://127.0.0.1:%d" % self.port
            headers["X-CSRF-Token"] = self.csrf_token()
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")
        except urllib.error.URLError as exc:
            # the server deliberately closes refused connections
            return (exc.reason.errno if hasattr(exc.reason, "errno")
                    else 0), ""

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
        # acceptance is prompt while the faked run stays live
        self.assertLess(elapsed, 2.0)
        job = body["job"]
        self.assertRegex(job, r"^[A-Za-z0-9_-]{8,}$")
        self.assertEqual(body["status_url"], "/s/new/" + job)
        self.wait_calls(1)  # the one background run really was admitted

        # the deterministic session id is published WHILE the run is live
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

        # settling the run settles the job as done, same session
        self.release()
        done = self.poll_status(body["status_url"],
                                lambda p: p.get("status") == "done")
        self.assertEqual(done["session_id"], "20260902_newjob_a1")
        self.assertEqual(self.call_count(), 1)

    def test_status_route_is_safe_and_terminal_on_failure(self):
        # The run wrote its session row, then failed fast — before any
        # status poll could observe it — and the job still publishes
        # the row it owns, then settles failed.
        self.write_stub("new-fail-after-create")
        self.set_stub_session("20260902_newjob_f1")
        status, body = self.request_json("POST", "/s/new",
                                         {"text": PROMPT})
        self.assertEqual(status, 202)
        failed = self.poll_status(body["status_url"],
                                  lambda p: p.get("status") == "failed")
        self.assertEqual(failed["session_id"], "20260902_newjob_f1")
        # safe canned reason, no prompt, no core output
        self.assertEqual(failed["error"], "the launch failed")
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
        # No core reachable: admission fails, nothing ran, no session.
        self.write_stub("new-fail")
        status, body = self.request_json("POST", "/s/new",
                                         {"text": PROMPT})
        self.assertEqual(status, 202)
        failed = self.poll_status(body["status_url"],
                                  lambda p: p.get("status") == "failed")
        self.assertFalse(failed["session_id"])
        self.assertEqual(failed["error"],
                         "the agent gateway could not be reached")
        self.assertNotIn(PROMPT, json.dumps(failed))
        self.assertEqual(self.call_count(), 1)

    def test_unknown_job_id_is_404(self):
        status, body = self.request_json("GET", "/s/new/no-such-job")
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])


class TestDuplicateLaunch(ServerCase):
    """Contract 2: exactly one live launch — a concurrent POST can never
    admit a second run, and the gate reopens once the run settles."""

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
        # The third run starts genuinely live (unrelease above), so it
        # lives its full lifecycle: only its genuine completion may
        # settle the job done.
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
        # the admission named the exact session and carried the prompt
        # only inside its JSON body — never argv, never a URL
        self.assertEqual(self.core.admissions, [
            {"input": PROMPT, "session_id": "s_reply_1"}])

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
        # apart (the core writes its answer, then the poller observes
        # the terminal state and releases the busy key), so the poll
        # waits for the settled end state — answer present AND busy
        # gone — not merely the first row.
        self.release()
        answered = self.poll_status(
            "/s/default/s_reply_1/feed?after=0",
            lambda p: any(m.get("role") == "assistant"
                          for m in p.get("messages", []))
            and not p.get("busy"))
        self.assertFalse(answered["busy"])
        self.assertEqual(answered["activity"]["html"], "")
        self.assertEqual(self.call_count(), 1)

    def test_unavailable_core_is_an_explicit_failed_send(self):
        # No core reachable: the turn is refused 503 synchronously —
        # never a silent fallback, never a duplicate run — and the
        # session's lease is released so a retry is possible at once.
        self.write_stub("new-fail")
        status, body = self.request_json(
            "POST", "/s/default/s_reply_1/reply", {"text": PROMPT})
        self.assertEqual(status, 503)
        self.assertFalse(body["ok"])
        self.assertEqual(self.call_count(), 1)
        with self.mod._jobs_lock:
            self.assertNotIn(("default", "s_reply_1"), self.mod._jobs)
        # the retry (core back) is accepted immediately
        self.write_stub("reply-block")
        status2, _body = self.request_json(
            "POST", "/s/default/s_reply_1/reply", {"text": PROMPT})
        self.assertEqual(status2, 202)
        self.release()

    def test_session_id_mismatch_fails_closed(self):
        # The core must run the exact session the composer addressed;
        # an echo that names any other session is a refusal, not a
        # silent conversation fork.
        self.write_stub("reply-block")
        original = self.core._admit

        def _mismatched(payload):
            _status, obj, err = original(payload)
            if isinstance(obj, dict) and obj.get("run_id"):
                obj = dict(obj, session_id="s_somebody_else")
            return _status, obj, err

        with unittest.mock.patch.object(self.core, "_admit",
                                        side_effect=_mismatched):
            status, body = self.request_json(
                "POST", "/s/default/s_reply_1/reply", {"text": PROMPT})
        self.assertEqual(status, 503)
        with self.mod._jobs_lock:
            self.assertNotIn(("default", "s_reply_1"), self.mod._jobs)


class TestRejectedSendClientContract(ServerCase):
    """The shipped client's busy-recovery contract (review finding 3):
    a reply POST that is not accepted — 409 busy above all — marks the
    optimistic row failed (never Sent/Read), restores the exact
    submitted text without clobbering a newer edit, and no later feed
    echo or busy poll can promote a rejected row back up the ladder."""

    def chat_page(self):
        self.add_session("s_reject_1", source="cli", title="reject")
        self.add_message("s_reject_1", "user", "hello")
        status, page = self.request("GET", "/s/default/s_reject_1")
        self.assertEqual(status, 200)
        return page

    def test_409_branch_fails_the_send_and_restores_text(self):
        page = self.chat_page()
        first202 = page.index("resp.status === 202")
        chat202 = page.index("resp.status === 202", first202 + 1)
        next409 = page.index("resp.status === 409", chat202)
        next404 = page.index("resp.status === 404", next409)
        branch = page[next409:next404]
        # the reply-mode 409 routes to failSend — the row fails and
        # the text returns — and never marks the row sent
        self.assertIn("failSend(rec, text,", branch)
        self.assertNotIn('setTickState(rec, "sent")', branch)
        self.assertIn("was not sent and is back in the composer", branch)

    def test_other_rejections_share_the_failure_path(self):
        page = self.chat_page()
        # 404 and 400/413 are failures too, and anything else (503,
        # network) lands in the catch — all through failSend
        for needle in ('failSend(rec, text, "This session can no longer',
                       'failSend(rec, text, "That message was refused'):
            self.assertIn(needle, page)
        chat404 = page.index('failSend(rec, text, "This session can no')
        catch_at = page.index("}).catch(function () {", chat404)
        catch_block = page[catch_at:catch_at + 400]
        self.assertIn("failSend(rec, text,", catch_block)

    def test_failed_state_is_terminal_for_every_promoter(self):
        page = self.chat_page()
        # setTickState: a failed row never moves again
        guard = page.index("function setTickState(rec, state)")
        block = page[guard:page.index("function ", guard + 10)]
        self.assertIn('if (rec.state === "failed") return;', block)
        # findOutgoing: a failed row is never the twin of a server echo
        finder = page.index("function findOutgoing(text)")
        fblock = page[finder:page.index("function ", finder + 10)]
        self.assertIn('outgoing[i].state !== "failed"', fblock)

    def test_restore_keeps_both_inputs_in_a_deterministic_order(self):
        page = self.chat_page()
        failer = page.index("function failSend(rec, text, msg)")
        block = page[failer:page.index("showFlash(msg", failer)]
        self.assertIn("var current = box.value;", block)
        self.assertIn('box.value = text + "\\n" + current;', block)


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

        # the send is accepted fast while the faked reply is still live
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
        """specs are (role, content[, tool_name[, tool_calls[,
        tool_call_id]]]) appended to one session; returns an open
        read-only connection for compute_activity."""
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
                " tool_name, tool_calls, tool_call_id, timestamp)"
                " VALUES (?,?,?,?,?,?,?)",
                (sid, role, spec[1],
                 spec[2] if len(spec) > 2 else None,
                 spec[3] if len(spec) > 3 else None,
                 spec[4] if len(spec) > 4 else None,
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

    def test_pending_tool_calls_until_their_results_land(self):
        """The pending lifecycle: a carrier's calls are pending (name,
        state, bounded args summary) until a tool row echoes the id
        back; each result resolves exactly its own call, and once the
        last one lands the strip's state derives from the result."""
        calls = json.dumps([
            {"id": "call_a", "function": {
                "name": "read_file",
                "arguments": "{\"path\": \"/etc/hosts\"}"}},
            {"id": "call_b", "function": {
                "name": "http",
                "arguments": "{\"url\": \"https://example.com\"}"}},
        ])
        con = self.rows(
            ("user", "the turn"),
            ("assistant", "", None, calls),
        )
        try:
            act = self.mod.compute_activity(con, "s_scope", 2000.0,
                                            busy_job=True)
            self.assertEqual([p["name"] for p in act["pending"]],
                             ["read_file", "http"])
            self.assertEqual(act["pending_count"], 2)
            self.assertEqual(act["names"], ["read_file", "http"])
            self.assertEqual(act["state"], act["pending"][-1]["state"])
            self.assertIn("path=/etc/hosts", act["pending"][0]["args"])
            strip = self.mod.render_activity(act)
            self.assertIn("read_file", strip)
            self.assertIn("http", strip)
        finally:
            con.close()

        # one result resolves only its own call
        con = self.rows(
            ("user", "the turn"),
            ("assistant", "", None, calls),
            ("tool", "done", "read_file", None, "call_a"),
        )
        try:
            act = self.mod.compute_activity(con, "s_scope", 2000.5,
                                            busy_job=True)
            self.assertEqual([p["name"] for p in act["pending"]],
                             ["http"])
            self.assertEqual(act["state"], act["pending"][0]["state"])
        finally:
            con.close()

        # the last result resolves the turn: no pending work left, and
        # the state names the result it is now thinking after
        con = self.rows(
            ("user", "the turn"),
            ("assistant", "", None, calls),
            ("tool", "done", "read_file", None, "call_a"),
            ("tool", "done", "http", None, "call_b"),
        )
        try:
            act = self.mod.compute_activity(con, "s_scope", 2001.0,
                                            busy_job=True)
            self.assertEqual(act["pending"], [])
            self.assertEqual(act["pending_count"], 0)
            self.assertTrue(act["active"])
            self.assertEqual(act["state"], "Thinking after http")
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


class TestLiveSubagents(ServerCase):
    """The Sub-agents section is live: a child dispatched after the
    page loaded appears on the next feed poll — the poll payload
    carries the whole replacement section — and the client source
    swaps it in place."""

    def test_feed_poll_replaces_the_subagents_section(self):
        self.add_session("s_parent", source="discord",
                         title="dispatching chat")
        self.add_message("s_parent", "user", "go research that")
        _status, page = self.request("GET", "/s/default/s_parent")
        self.assertNotIn('id="subagents"', page)
        self.assertIn("applySubagents", page)  # the client swap path

        # a same-profile subagent lands while the page is open
        con = sqlite3.connect(self.db)
        now = time.time()
        con.execute(
            "INSERT INTO sessions (id, source, title, started_at,"
            " last_activity_at, archived, hidden, parent_session_id)"
            " VALUES ('s_child','subagent','child goal text',?,?,0,0,"
            " 's_parent')", (now - 30, now - 5))
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp)"
            " VALUES ('s_child','user','the dispatched goal',?)",
            (now - 30,))
        con.commit()
        con.close()

        feed = self.poll_status(
            "/s/default/s_parent/feed?after=0",
            lambda p: p["subagents"]["count"] == 1)
        self.assertEqual(feed["subagents"]["ids"], ["s_child"])
        self.assertIn("child goal text", feed["subagents"]["html"])
        self.assertIn('href="/s/default/s_child"',
                      feed["subagents"]["html"])

        # a reload renders the same section server-side
        _status, page2 = self.request("GET", "/s/default/s_parent")
        self.assertIn('id="subagents"', page2)
        self.assertIn("child goal text", page2)

        # and the section leaves again when the child is hidden — the
        # replacement is a full swap, not an append-only list
        con = sqlite3.connect(self.db)
        con.execute("UPDATE sessions SET hidden = 1 WHERE id = 's_child'")
        con.commit()
        con.close()
        gone = self.poll_status(
            "/s/default/s_parent/feed?after=0",
            lambda p: p["subagents"]["count"] == 0)
        self.assertEqual(gone["subagents"]["html"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
