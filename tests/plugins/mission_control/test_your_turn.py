#!/usr/bin/env python3
"""The Your turn section: the open conversations whose next move is
the human's.

The inbox sections answered "what is the agent doing" (Active, Open ·
unfinished, Open · completed, Closed) but never "what is waiting on
me". This file guards the section that answers it: Your turn, first in
the sidebar, holding exactly the open conversations whose projected
tip is neither ended nor archived and whose next move is the human's —
an explicit wait the core names in its batch waiting-status answer (an
API-run clarify, a native gateway clarify prompt, or a restart
confirmation: all one surface to this server, and the endpoint says
only THAT a session waits) or an idle chat whose newest active event
is a plain assistant answer. Closed still wins over both (in this
bundled server the projected tip's ended_at already means Closed, so
an ended row is never promoted however it ended), and the badge
counts parent conversations only.

Covered end to end at the HTTP layer over throwaway DBs (production
schema) with a stub core API standing in for the real one:

- a named wait promotes a row out of Active into Your turn — the
  lease can stay live, and neither lease nor composer job is a
  prerequisite: an expired lease or a plain user-last row promotes
  the same way when named;
- the batch contract: exactly ONE authenticated GET
  /api/sessions/waiting per inbox refresh per profile, regardless of
  how many rows render — never a per-session clarify probe;
- profile scoping: a wait named under one profile's prefixed route
  never promotes another profile's row with the same session id;
- an idle assistant-ended OPEN chat rests in Your turn with zero
  per-session HTTP; a user-last chat stays Open · unfinished,
  including when its transcript text literally mentions clarify or
  restart (waits come from the registry, never from message text);
  a live lease the core does not name stays Active;
- Closed always wins: archived and ended rows keep the Closed
  section with a wait named or an assistant answer waiting;
- fail closed: a waiting-endpoint error, a missing endpoint, or an
  unreachable core promotes nothing — rows keep their sections and
  the page still renders (bounded: the wedged-core page answers well
  inside a browser budget).

Run:  python3 tests/plugins/mission_control/test_your_turn.py
(unittest, stdlib only)
"""

import importlib.util
import itertools
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SERVER_PY = os.path.join(REPO, "plugins", "mission_control", "server.py")

_MODULE_SEQ = itertools.count()

# The production schema, imported from core: the listing is served by
# the core projection (list_sessions_rich), so fixture DBs must answer
# exactly the SQL the live ones do.
sys.path.insert(0, REPO)

from hermes_state_common import SCHEMA_SQL  # noqa: E402

SESSION_SCHEMA = SCHEMA_SQL

MAIN_KEY = "test-main-key-1"
WORK_KEY = "test-work-key-1"

# The one batch route the inbox pays for (per profile, prefixed).
WAITING_PATH = "/api/sessions/waiting"


class _CoreHandler(BaseHTTPRequestHandler):
    """The stub core API: the batch waiting-status GET per profile
    prefix, with a per-path forced error status. Every request is
    recorded for the batching, scoping and never-probed assertions."""

    def _record(self, method):
        self.server.requests.append({
            "method": method,
            "path": self.path,
            "auth": self.headers.get("Authorization") or "",
        })

    def _answer(self, status, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._record("GET")
        srv = self.server
        if srv.get_status.get(self.path, 200) != 200:
            self._answer(srv.get_status[self.path], {"error": "stub"})
            return
        m = re.match(r"^/p/([A-Za-z0-9_-]+)(/.*)$", self.path)
        if m is not None and m.group(2) == WAITING_PATH:
            if srv.raw_waiting_body is not None:
                self._answer(200, srv.raw_waiting_body)
                return
            waiting = srv.waiting.get(m.group(1), set())
            self._answer(200, {
                "object": "hermes.sessions.waiting",
                "waiting": [{"session_id": sid, "kind": "clarify"}
                            for sid in sorted(waiting)],
            })
            return
        self._answer(200, {"pending_clarify": None})

    def log_message(self, fmt, *args):
        pass  # the stub never chatters into the test log


class CoreStub(ThreadingHTTPServer):
    """The stub's mutable state: per-profile sets of session ids the
    core says wait on the human, plus forced GET statuses."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _CoreHandler)
        self.requests = []
        self.waiting = {}
        self.get_status = {}
        self.raw_waiting_body = None

    def hits(self, method=None, path=None):
        return [r for r in self.requests
                if (method is None or r["method"] == method)
                and (path is None or r["path"] == path)]


def load_server(tmp, main_db, api_base):
    """One isolated server.py module per test, pointed at the fixture
    home and the stub core. Nothing here launches children."""
    spec = importlib.util.spec_from_file_location(
        "mc_server_yourturn_%d" % _MODULE_SEQ.__next__(), SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.MAIN_DB = main_db
    mod.PROFILE_GLOB = os.path.join(tmp, "profiles", "*", "state.db")
    mod.CLARIFY_API_BASE = api_base
    return mod


class YourTurnCase(unittest.TestCase):
    """A real Mission Control server plus a stub core API, both on
    ephemeral ports, over a throwaway default-profile DB (and, for the
    scoping test, a named work profile)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-yourturn-")
        self.now = time.time()
        self.base = self.now - 3600  # fixture activity sits 1h back
        self.db = os.path.join(self.tmp, "state.db")
        con = sqlite3.connect(self.db)
        con.executescript(SESSION_SCHEMA)
        con.commit()
        con.close()
        with open(os.path.join(self.tmp, ".env"), "w",
                  encoding="utf-8") as fh:
            fh.write("API_SERVER_KEY=%s\n" % MAIN_KEY)
        self.core = CoreStub()
        self.core_port = self.core.server_address[1]
        threading.Thread(target=self.core.serve_forever,
                         daemon=True).start()
        self.mod = load_server(
            self.tmp, self.db, "http://127.0.0.1:%d" % self.core_port)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                         self.mod.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever,
                         daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.core.shutdown()
        self.core.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- fixture writers -------------------------------------------

    def add_session(self, sid, source="cli", title=None, started=None,
                    last=None, ended=None, archived=0):
        started = self.base if started is None else started
        last = started + 300 if last is None else last
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO sessions (id, source, title, started_at,"
            " last_activity_at, ended_at, end_reason, archived,"
            " hidden) VALUES (?,?,?,?,?,?,?,?,0)",
            (sid, source, title, started, last, ended,
             "cli_close" if ended else None, archived))
        con.commit()
        con.close()

    def add_message(self, sid, role, content, at=None):
        con = sqlite3.connect(self.db, timeout=10)
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp)"
            " VALUES (?,?,?,?)",
            (sid, role, content, self.base if at is None else at))
        con.commit()
        con.close()

    def add_lease(self, sid, minutes=30):
        """A live unexpired turn lease naming sid."""
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO session_turn_leases (conversation_id, holder,"
            " acquired_at, expires_at) VALUES (?,?,?,?)",
            (sid, "pid=1:worker", self.now, self.now + minutes * 60))
        con.commit()
        con.close()

    def add_expired_lease(self, sid):
        """A lease that lapsed five minutes ago — the row is open with
        no live Active signal, exactly the shape a late wait must
        still be able to promote."""
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO session_turn_leases (conversation_id, holder,"
            " acquired_at, expires_at) VALUES (?,?,?,?)",
            (sid, "pid=1:worker", self.now - 3600, self.now - 300))
        con.commit()
        con.close()

    def hold_wait(self, *sids, **kwargs):
        """The core's batch answer names these sessions under one
        profile (default unless told otherwise) as explicitly waiting
        on the human."""
        profile = kwargs.get("profile", "default")
        self.core.waiting.setdefault(profile, set()).update(sids)

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

    # ---- HTML assertions -------------------------------------------

    def section_spans(self, page):
        """(key, start, end) per rendered sidebar section, in document
        order."""
        spans = []
        for m in re.finditer(
                r'<(section|details) class="convsec"[^>]*'
                r'data-section="([a-z_]+)"', page):
            close = page.find("</%s>" % m.group(1), m.end())
            spans.append((m.group(2), m.start(), close))
        return spans

    def section_count(self, page, section):
        """The rendered count badge of one sidebar section (0 when the
        section does not render at all)."""
        m = re.search(r'data-section="%s"[^>]*>.*?data-count="(\d+)"'
                      % section, page, re.S)
        return int(m.group(1)) if m else 0

    def row_position(self, page, sid, profile="default"):
        """Position of one session's conversation row — matched on its
        data-q blob (led by the session id) AND its data-profile, so a
        session id that exists under two profiles names exactly its
        own row."""
        m = re.search(
            r'<article class="conv[^>]*data-q="%s[ "][^>]*'
            r'data-profile="%s"' % (re.escape(sid), re.escape(profile)),
            page)
        self.assertIsNotNone(
            m, "no sidebar row for %s/%s" % (profile, sid))
        return m.start()

    def section_of(self, page, sid, profile="default"):
        """data-section of the section holding one session's row — the
        honest answer to "which labeled bucket does this row render
        under"."""
        pos = self.row_position(page, sid, profile)
        for key, start, end in self.section_spans(page):
            if start < pos < end:
                return key
        self.fail("no rendered section contains %s/%s" % (profile, sid))

    def waiting_path(self, profile="default"):
        return "/p/%s%s" % (profile, WAITING_PATH)


class TestExplicitWaits(YourTurnCase):
    """A named wait is the strongest Your turn signal: it pulls a row
    out of Active even while the agent's lease stays live, and it
    needs neither lease nor job — the wait itself is the evidence."""

    def seed_active(self, sid):
        self.add_session(sid, title="held on a question")
        self.add_message(sid, "user", "go do the thing")
        self.add_lease(sid)

    def test_wait_moves_active_row_to_your_turn(self):
        sid = "20260909_yt_clarified"
        self.seed_active(sid)
        self.hold_wait(sid)
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(self.section_of(page, sid), "your_turn")
        self.assertEqual(self.section_count(page, "your_turn"), 1)
        self.assertEqual(self.section_count(page, "active"), 0)
        # the lease is still live: the promote happened despite it, via
        # exactly ONE batch GET for the one profile — never a
        # per-session clarify probe
        self.assertEqual(
            len(self.core.hits("GET", self.waiting_path())), 1)
        self.assertEqual(len(self.core.hits("GET")), 1)

    def test_native_restart_wait_lands_in_your_turn(self):
        """A restart confirmation registers through the same core
        surface the batch endpoint reads — same promotion, same one
        request."""
        sid = "20260909_yt_restart"
        self.seed_active(sid)
        self.hold_wait(sid)
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(self.section_of(page, sid), "your_turn")
        self.assertEqual(self.section_count(page, "your_turn"), 1)
        self.assertEqual(self.section_count(page, "active"), 0)

    def test_wait_promotes_row_with_expired_lease(self):
        """Lease/job is NOT a prerequisite: the wait names the session,
        the row is open, so it is Your turn even though no live Active
        signal exists."""
        sid = "20260909_yt_nolease"
        self.add_session(sid, title="lease lapsed mid-question")
        self.add_message(sid, "user", "go", self.base)
        self.add_expired_lease(sid)
        self.hold_wait(sid)
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(self.section_of(page, sid), "your_turn")
        self.assertEqual(self.section_count(page, "your_turn"), 1)

    def test_wait_promotes_user_last_row_without_any_lease(self):
        """A row the DB calls Open · unfinished (user-last) still moves
        when the core names it — the wait outranks the transcript
        shape."""
        sid = "20260909_yt_userwait"
        self.add_session(sid, title="question left hanging")
        self.add_message(sid, "user", "please confirm", self.base)
        self.hold_wait(sid)
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(self.section_of(page, sid), "your_turn")
        self.assertEqual(self.section_count(page, "incomplete"), 0)

    def test_one_batch_request_per_refresh_regardless_of_rows(self):
        """The request count scales with profiles, never rows: five
        waiting rows still cost one GET per page load."""
        for i in range(5):
            sid = "20260909_yt_row%d" % i
            self.seed_active(sid)
            self.hold_wait(sid)
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(self.section_count(page, "your_turn"), 5)
        self.assertEqual(len(self.core.hits("GET", self.waiting_path())), 1)
        # a second refresh is a second batch call — still row-count proof
        _s, _p = self.request("GET", "/")
        self.assertEqual(len(self.core.hits("GET", self.waiting_path())), 2)

    def test_dropped_wait_moves_row_out_of_your_turn(self):
        """The core answered or cancelled: the next page load reflects
        it — the row returns to its prior section (Active, lease live)."""
        sid = "20260909_yt_dropped"
        self.seed_active(sid)
        self.hold_wait(sid)
        _s, page = self.request("GET", "/")
        self.assertEqual(self.section_of(page, sid), "your_turn")
        self.core.waiting["default"].discard(sid)
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(self.section_of(page, sid), "active")
        self.assertEqual(self.section_count(page, "your_turn"), 0)
        self.assertEqual(self.section_count(page, "active"), 1)

    def test_wait_never_leaks_across_profiles(self):
        """Profile+session is a composite key: the same session id
        exists under two profiles, only the named one's wait promotes
        its own row — and each profile pays exactly one batch GET with
        its own key."""
        sid = "20260909_yt_shared"
        work_db = os.path.join(self.tmp, "profiles", "work", "state.db")
        os.makedirs(os.path.dirname(work_db), exist_ok=True)
        con = sqlite3.connect(work_db)
        con.executescript(SESSION_SCHEMA)
        con.execute(
            "INSERT INTO sessions (id, source, title, started_at,"
            " last_activity_at, archived, hidden)"
            " VALUES (?,?,?,?,?,0,0)",
            (sid, "cli", "the work copy", self.base, self.base + 60))
        con.commit()
        con.close()
        with open(os.path.join(self.tmp, "profiles", "work", ".env"),
                  "w", encoding="utf-8") as fh:
            fh.write("API_SERVER_KEY=%s\n" % WORK_KEY)
        self.add_session(sid, title="the default copy")
        self.add_message(sid, "user", "go", self.base)
        self.add_lease(sid)
        # ONLY the work profile's wait names the shared id
        self.hold_wait(sid, profile="work")

        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        # the default-profile row stays Active: its own batch answer
        # named nothing — and the work-profile row with the SAME id is
        # the one that moved
        self.assertEqual(self.section_of(page, sid, "default"), "active")
        self.assertEqual(self.section_of(page, sid, "work"), "your_turn")
        # both profiles were asked exactly once, with their own keys
        self.assertEqual(
            len(self.core.hits("GET", self.waiting_path("default"))), 1)
        self.assertEqual(
            len(self.core.hits("GET", self.waiting_path("work"))), 1)
        for hit in self.core.hits("GET", self.waiting_path("default")):
            self.assertEqual(hit["auth"], "Bearer " + MAIN_KEY)
        for hit in self.core.hits("GET", self.waiting_path("work")):
            self.assertEqual(hit["auth"], "Bearer " + WORK_KEY)


class TestIdleChats(YourTurnCase):
    """The quiet half of Your turn: open chats that settled on a plain
    assistant answer — no lease, no job, no per-session HTTP at all."""

    def test_assistant_last_rests_in_your_turn_without_probing(self):
        open_ended = "20260909_yt_openans"  # assistant last, open tip
        self.add_session(open_ended, title="still open")
        self.add_message(open_ended, "user", "another question",
                         self.base + 60)
        self.add_message(open_ended, "assistant", "another answer",
                         self.base + 120)

        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(self.section_of(page, open_ended), "your_turn")
        self.assertEqual(self.section_count(page, "your_turn"), 1)
        self.assertEqual(self.section_count(page, "completed"), 0)
        # the perf contract: only the one batch GET, nothing per row
        self.assertEqual(len(self.core.hits("GET")), 1)
        self.assertEqual(len(self.core.hits("GET", self.waiting_path())), 1)

    def test_user_last_stays_unfinished(self):
        sid = "20260909_yt_userlast"
        self.add_session(sid, title="waiting on the agent")
        self.add_message(sid, "user", "please go", self.base)
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(self.section_of(page, sid), "incomplete")
        self.assertEqual(self.section_count(page, "incomplete"), 1)
        self.assertEqual(self.section_count(page, "your_turn"), 0)

    def test_synthetic_mentions_of_clarify_or_restart_never_wait(self):
        """Waits come from the registry through the batch endpoint,
        never from transcript text: a user-last chat whose messages
        literally discuss a clarify prompt or the restart word stays
        exactly where the transcript shape puts it."""
        sid = "20260909_yt_mention"
        self.add_session(sid, title="talks about clarify a lot")
        self.add_message(sid, "assistant",
                         "shall I run the clarify prompt?", self.base)
        self.add_message(sid, "user",
                         "yes please clarify, then restart", self.base + 30)
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(self.section_of(page, sid), "incomplete")
        self.assertEqual(self.section_count(page, "your_turn"), 0)

    def test_live_lease_without_any_wait_stays_active(self):
        """The agent is genuinely working and asked nothing: Active —
        the batch answer simply does not name the session."""
        sid = "20260909_yt_working"
        self.add_session(sid, title="hard at work")
        self.add_message(sid, "user", "go", self.base)
        self.add_lease(sid)
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(self.section_of(page, sid), "active")
        self.assertEqual(self.section_count(page, "your_turn"), 0)
        self.assertEqual(len(self.core.hits("GET", self.waiting_path())), 1)


class TestClosedStillWins(YourTurnCase):
    """Closed outranks every Your turn signal — in this bundled server
    the projected tip's ended_at already means Closed, so BOTH the
    archived and the ended-but-unarchived row stay Closed with a wait
    named or an assistant answer waiting."""

    def test_archived_and_ended_rows_stay_closed(self):
        waited = "20260909_yt_archwait"
        answered = "20260909_yt_endedans"
        self.add_session(waited, title="closed with a wait", archived=1)
        self.add_message(waited, "user", "go", self.base)
        self.add_lease(waited)  # live lease too: closed still wins
        self.hold_wait(waited)
        self.add_session(answered, title="ended after answering",
                         ended=self.base + 300)
        self.add_message(answered, "user", "q", self.base)
        self.add_message(answered, "assistant", "a", self.base + 300)

        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(self.section_of(page, waited), "closed")
        self.assertEqual(self.section_of(page, answered), "closed")
        self.assertEqual(self.section_count(page, "closed"), 2)
        self.assertEqual(self.section_count(page, "your_turn"), 0)
        self.assertEqual(self.section_count(page, "active"), 0)


class TestFailClosed(YourTurnCase):
    """The batch endpoint is an enhancement, never a dependency: an
    erroring, missing or unreachable endpoint leaves the prior
    classification intact and the page renders."""

    def seed_active(self, sid):
        self.add_session(sid, title="unreachable wait")
        self.add_message(sid, "user", "go do the thing")
        self.add_lease(sid)

    def test_waiting_endpoint_error_fails_closed(self):
        sid = "20260909_yt_errclosed"
        self.seed_active(sid)
        self.core.get_status[self.waiting_path()] = 500
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(self.section_of(page, sid), "active")
        self.assertEqual(self.section_count(page, "active"), 1)
        self.assertEqual(self.section_count(page, "your_turn"), 0)

    def test_missing_endpoint_fails_closed_idle_rule_survives(self):
        """A core without the route yet (404): no invented waits, but
        the idle plain-answer rule still works — the feature degrades
        to its pre-batch behaviour, never to a broken page."""
        sid = "20260909_yt_noendp"
        idle = "20260909_yt_idle_noendp"
        self.seed_active(sid)
        self.core.get_status[self.waiting_path()] = 404
        self.add_session(idle, title="answered, core route missing")
        self.add_message(idle, "user", "q", self.base)
        self.add_message(idle, "assistant", "a", self.base + 30)
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(self.section_of(page, sid), "active")
        self.assertEqual(self.section_of(page, idle), "your_turn")
        self.assertEqual(self.section_count(page, "your_turn"), 1)

    def test_unreachable_core_never_hangs_the_page(self):
        """A wedged core costs at most the bounded deadline: the page
        still answers within a generous client budget and classifies
        from the DB alone."""
        sid = "20260909_yt_wedged"
        self.seed_active(sid)
        # Point the module's API base at a port with no listener; the
        # connection is refused, which the client must treat like any
        # other upstream error.
        self.mod.CLARIFY_API_BASE = "http://127.0.0.1:1"
        started = time.time()
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertLess(time.time() - started, 10)
        self.assertEqual(self.section_of(page, sid), "active")

    def test_non_list_waiting_value_fails_closed(self):
        """A 200 whose waiting field is not a list ({"waiting": 1} or
        true) is malformed, not an empty wait list: iterating it would
        crash the inbox, so the profile fails closed — rows keep their
        sections while the idle plain-answer rule still applies."""
        sid = "20260909_yt_watint"
        idle = "20260909_yt_idle_watint"
        self.seed_active(sid)
        self.add_session(idle, title="answered anyway")
        self.add_message(idle, "user", "q", self.base)
        self.add_message(idle, "assistant", "a", self.base + 30)
        for malformed in ({"waiting": 1}, {"waiting": True}):
            self.core.raw_waiting_body = malformed
            status, page = self.request("GET", "/")
            self.assertEqual(status, 200)
            self.assertEqual(self.section_of(page, sid), "active")
            self.assertEqual(self.section_of(page, idle), "your_turn")
            self.assertEqual(self.section_count(page, "your_turn"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
