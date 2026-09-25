#!/usr/bin/env python3
"""Focused tests for inline delegate dispatch cards + Claude watch links.

One spawn-shaped delegate call renders as an inline dispatch card at its
assistant carrier — for all four delegate tools, batch tasks included —
and a delegate_claude_agent card additionally becomes a whole-card link
to that exact run's live Claude viewer page through the spawn receipt
(tools.claude_run_receipts), while the run is still going and after it
completes. Covered end to end at the HTTP layer over a synthetic
state.db whose "default" home IS the fixture home (so receipts plant
exactly where production writes them):

- card basics: one card per dispatched task per tool, control calls
  render no card (and keep their generic tool row), matched children
  absorb into their card (goal-agreeing, one-to-one, fail-open — a
  weaker match leaves the card static AND the child its own row), card
  order follows the carriers, batch calls fan out, a truncated carrier
  slice still yields its tasks, every payload string is escaped, and a
  childless session renders no card scaffolding at all;
- Claude watch links: a valid receipt turns the card into the viewer
  anchor BEFORE any tool result row exists, the viewer link takes
  precedence over a matched child's transcript link, a /feed poll
  upgrades an already-rendered static card in place exactly once (same
  key, one revision change, the page's own markup), completed cards
  keep the link, and every malformed / mismatched / cross-profile /
  oversized / unsafe / logless receipt fails closed to the static card
  with no href — never a fabricated URL;
- feed reconciliation: initial render and /feed share one renderer and
  one revision key per row, children and cards ride one keyed identity
  space, and the Next sub-agent pill anchors at the first keyed row
  whether that is a child or a dispatch card.

Run:  python3 tests/plugins/mission_control/test_delegate_cards.py
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
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SERVER_PY = os.path.join(REPO, "plugins", "mission_control", "server.py")

sys.path.insert(0, REPO)

from hermes_state_common import SCHEMA_SQL  # noqa: E402
from tools import claude_run_receipts as receipts  # noqa: E402

SESSION_SCHEMA = SCHEMA_SQL

_MODULE_SEQ = itertools.count()

# Far enough in the past that relative-time buckets never flip between
# the page render and a feed poll inside one test.
BASE_TS = time.time() - 86400.0

GOAL_CLAUDE = "Harden the deployment pipeline and verify the rollout"
GOAL_AGENT = "Investigate the flaky scheduler test and report findings"
GOAL_CURSOR = "Rename the config module and update every import site"


def load_server(tmp, db_path):
    """One isolated server.py module instance per test: MAIN_DB is the
    fixture DB inside the fixture home, so profile_home("default") — the
    trusted home spawn receipts are resolved under — IS this test's own
    directory tree."""
    spec = importlib.util.spec_from_file_location(
        "mc_server_dlgcards_%d" % _MODULE_SEQ.__next__(), SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.MAIN_DB = db_path
    mod.PROFILE_GLOB = os.path.join(tmp, "profiles", "*", "state.db")
    return mod


def plant_receipt(home, sid, call, stem="20260907-120000-4242",
                  workdir="/tmp/repo"):
    """Write one REAL spawn receipt (the production writer) into the
    fixture home, run log included — exactly the bytes on_spawn leaves
    behind. Returns the receipt path."""
    log = receipts.claude_runs_dir(home) / (stem + ".jsonl")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type": "system"}\n', encoding="utf-8")
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    try:
        path = receipts.write_spawn_receipt(sid, call, str(log), workdir)
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old
    assert path is not None, "fixture receipt must write"
    return path


def mutate_receipt(path, **fields):
    """Rewrite one planted receipt with overridden fields, 0600 again."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(fields)
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(path, 0o600)
    return data


class DispatchCase(unittest.TestCase):
    """A real ThreadingHTTPServer on an ephemeral port over a synthetic
    state.db that lives inside its own fixture Hermes home."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dlgcards-test-")
        # the "default" profile's home: the DB's own directory
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.db = os.path.join(self.home, "state.db")
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
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- fixture helpers -------------------------------------------

    def add_session(self, sid, title="fixture", ts=None):
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT OR REPLACE INTO sessions (id, source, title,"
            " started_at, last_activity_at, archived, hidden)"
            " VALUES (?,?,?,?,?,0,0)",
            (sid, "cli", title, ts if ts is not None else BASE_TS,
             ts if ts is not None else BASE_TS))
        con.commit()
        con.close()

    def add_message(self, sid, role, content, ts=None, **cols):
        con = sqlite3.connect(self.db, timeout=10)
        cols_sql = "".join(", %s" % key for key in cols)
        vals = list(cols.values())
        marks = "".join(",?" for _ in cols)
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp%s)"
            " VALUES (?,?,?,?%s)" % (cols_sql, marks),
            [sid, role, content, ts if ts is not None else BASE_TS] + vals)
        con.commit()
        con.close()

    def add_carrier(self, sid, call_id, tool, task, ts, args=None,
                    truncate_at=None):
        """One assistant tool_calls carrier holding a single delegate
        call (the chronological authority a card hangs off)."""
        merged = {"task": task, "workdir": "/tmp/repo"}
        if args:
            merged.update(args)
        tool_calls = json.dumps([{
            "id": call_id, "type": "function",
            "function": {"name": tool,
                         "arguments": json.dumps(merged)},
        }])
        if truncate_at is not None:
            tool_calls = tool_calls[:truncate_at]
        self.add_message(sid, "assistant", "", ts=ts,
                         tool_calls=tool_calls)

    def add_tool_result(self, sid, call_id, tool_name, ts, content="ok"):
        self.add_message(sid, "tool", content, ts=ts,
                         tool_name=tool_name, tool_call_id=call_id)

    def add_child(self, parent_sid, child_sid, goal, ts,
                  ended=None, end_reason=None):
        """A source='subagent' child of the parent session whose first
        user message is the goal it was dispatched for."""
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT OR REPLACE INTO sessions (id, source, title,"
            " started_at, last_activity_at, ended_at, end_reason,"
            " archived, hidden, parent_session_id)"
            " VALUES (?,?,?,?,?,?,?,0,0,?)",
            (child_sid, "subagent", None, ts, ts + 60, ended, end_reason,
             parent_sid))
        con.commit()
        con.close()
        self.add_message(child_sid, "user", goal, ts=ts)

    def add_delegation(self, sid, delegation_id, goal, ts, state="completed",
                       completed=None, model="glm-4.6"):
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO async_delegations (delegation_id, origin_session,"
            " parent_session_id, state, dispatched_at, completed_at,"
            " updated_at, task_json) VALUES (?,?,?,?,?,?,?,?)",
            (delegation_id, sid, sid, state, ts, completed, ts,
             json.dumps({"goal": goal, "model": model})))
        con.commit()
        con.close()

    # ---- HTTP helpers ----------------------------------------------

    def request(self, method, path, obj=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(obj).encode("utf-8") if obj is not None else None
        headers = {"Content-Type": "application/json"} if obj else {}
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def page(self, sid="20260907_cards"):
        status, body = self.request("GET", "/s/default/" + sid)
        self.assertEqual(status, 200)
        return body

    def feed(self, sid="20260907_cards"):
        status, body = self.request(
            "GET", "/s/default/%s/feed?after=0" % sid)
        self.assertEqual(status, 200)
        return json.loads(body)

    # ---- assertion helpers -----------------------------------------

    def card_in_page(self, body, key):
        """The <li> for one keyed row, exactly as the page renders it."""
        m = re.search(
            r'<li class="msg (?:delegate-item|subagent-item)"'
            r' id="sa-%s".*?</li>' % re.escape(key), body, re.S)
        self.assertIsNotNone(m, "no inline row for key %s" % key)
        return m.group(0)

    def delegate_keys_in_order(self, body):
        return re.findall(r'<li class="msg delegate-item" id="sa-([^"]+)"',
                          body)

    def card_key(self, call_id, idx=0):
        return "delegate-%s-%d" % (call_id, idx)


class TestDispatchCardBasics(DispatchCase):
    """One truthful card per dispatched task, nothing for control calls,
    and matching that only ever links what provably belongs together."""

    SID = "20260907_cards"

    def setUp(self):
        super().setUp()
        self.add_session(self.SID)

    def test_all_four_delegate_tools_render_one_card_each(self):
        self.add_carrier(self.SID, "call-a", "delegate_agent",
                         GOAL_AGENT, BASE_TS)
        self.add_carrier(self.SID, "call-t", "delegate_task",
                         "Summarize the incident and open a tracking bug",
                         BASE_TS + 10)
        self.add_carrier(self.SID, "call-c", "delegate_claude_agent",
                         GOAL_CLAUDE, BASE_TS + 20)
        self.add_carrier(self.SID, "call-x", "delegate_cursor_agent",
                         GOAL_CURSOR, BASE_TS + 30)
        body = self.page(self.SID)
        self.assertEqual(self.delegate_keys_in_order(body), [
            self.card_key("call-a"), self.card_key("call-t"),
            self.card_key("call-c"), self.card_key("call-x")])
        # the exact tool name rides along as the mono chip
        for tool in ("delegate_agent", "delegate_task",
                     "delegate_claude_agent", "delegate_cursor_agent"):
            self.assertIn('<span class="dlg-tool">%s</span>' % tool, body)

    def test_control_calls_render_no_card_and_keep_their_tool_row(self):
        self.add_carrier(self.SID, "call-l", "delegate_agent",
                         "unused", BASE_TS, args={"action": "list"})
        self.add_tool_result(self.SID, "call-l", "delegate_agent",
                             BASE_TS + 5, content="CONTROL-RESULT")
        body = self.page(self.SID)
        self.assertEqual(self.delegate_keys_in_order(body), [])
        # the generic delegate tool row stays: nothing suppressed it
        self.assertIn("CONTROL-RESULT", body)

    def test_matched_child_becomes_the_cards_link(self):
        self.add_carrier(self.SID, "call-m", "delegate_agent",
                         GOAL_AGENT, BASE_TS)
        self.add_child(self.SID, "child-m", GOAL_AGENT, BASE_TS + 5)
        body = self.page(self.SID)
        card = self.card_in_page(body, self.card_key("call-m"))
        self.assertIn('href="/s/default/child-m"', card)
        self.assertIn(">Open</span>", card)
        # the absorbed child renders as its card, never a second row
        self.assertNotIn('data-child="default-child-m"', body)
        self.assertNotIn('<li class="msg subagent-item"', body)

    def test_child_without_goal_agreement_stays_its_own_row(self):
        self.add_carrier(self.SID, "call-u", "delegate_agent",
                         GOAL_AGENT, BASE_TS)
        # same window, DIFFERENT task: time alone never fabricates a link
        self.add_child(self.SID, "child-u", GOAL_CURSOR, BASE_TS + 5)
        body = self.page(self.SID)
        card = self.card_in_page(body, self.card_key("call-u"))
        self.assertIn("dlg-static", card)
        self.assertNotIn("href=", card)
        # ...and the child keeps its ordinary standalone row + link
        child = self.card_in_page(body, "default-child-u")
        self.assertIn('href="/s/default/child-u"', child)

    def test_ambiguous_children_fail_open(self):
        self.add_carrier(self.SID, "call-1", "delegate_agent",
                         GOAL_AGENT, BASE_TS)
        # TWO goal-agreeing children in the window: not one-to-one
        self.add_child(self.SID, "child-1a", GOAL_AGENT, BASE_TS + 5)
        self.add_child(self.SID, "child-1b", GOAL_AGENT, BASE_TS + 6)
        body = self.page(self.SID)
        card = self.card_in_page(body, self.card_key("call-1"))
        self.assertIn("dlg-static", card)
        self.assertNotIn("href=", card)
        self.assertIsNotNone(re.search(
            r'<li class="msg subagent-item"[^>]*data-child="default-child-1a"',
            body))
        self.assertIsNotNone(re.search(
            r'<li class="msg subagent-item"[^>]*data-child="default-child-1b"',
            body))

    def test_delegation_record_drives_card_state(self):
        self.add_carrier(self.SID, "call-d", "delegate_agent",
                         GOAL_AGENT, BASE_TS)
        self.add_delegation(self.SID, "del-1", GOAL_AGENT, BASE_TS + 3,
                            state="completed", completed=BASE_TS + 99)
        body = self.page(self.SID)
        card = self.card_in_page(body, self.card_key("call-d"))
        self.assertIn('data-state="done"', card)
        self.assertIn("Done", card)

    def test_result_row_of_a_carded_call_is_suppressed(self):
        self.add_carrier(self.SID, "call-s", "delegate_claude_agent",
                         GOAL_CLAUDE, BASE_TS)
        self.add_tool_result(self.SID, "call-s", "delegate_claude_agent",
                             BASE_TS + 90, content="DELEGATE-RESULT")
        self.add_tool_result(self.SID, "call-o", "read_file", BASE_TS + 91,
                             content="OTHER-RESULT")
        body = self.page(self.SID)
        # the card replaces the generic delegate result row...
        self.assertNotIn("DELEGATE-RESULT", body)
        self.assertIn(self.card_key("call-s"), body)
        # ...while unrelated tool results are untouched
        self.assertIn("OTHER-RESULT", body)

    def test_batch_call_yields_one_card_per_task(self):
        args = {"tasks": [
            {"goal": GOAL_AGENT},
            {"goal": GOAL_CLAUDE},
        ]}
        self.add_carrier(self.SID, "call-b", "delegate_task",
                         "unused", BASE_TS, args=args)
        body = self.page(self.SID)
        self.assertEqual(self.delegate_keys_in_order(body), [
            self.card_key("call-b", 0), self.card_key("call-b", 1)])

    def test_truncated_carrier_slice_degrades_to_a_partial_card(self):
        """A carrier slice cut deep inside the arguments string (the SQL
        cap on a long batch takes the whole outer array down with it):
        whole-document parsing dies, and the anchored recovery still
        yields exactly one truthful card — label lost is label
        substituted ("Sub-agent dispatch"), never a crash and never a
        fabricated call id."""
        full = json.dumps([{
            "id": "call-tr", "call_id": "call-tr", "type": "function",
            "function": {"name": "delegate_claude_agent",
                         "arguments": json.dumps(
                             {"task": GOAL_CLAUDE,
                              "workdir": "/tmp/repo"})},
        }])
        self.add_message(self.SID, "assistant", "", ts=BASE_TS,
                         tool_calls=full[:len(full) - 60])
        body = self.page(self.SID)
        keys = self.delegate_keys_in_order(body)
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0].startswith("delegate-"))
        card = self.card_in_page(body, keys[0])
        self.assertIn("dlg-tool", card)
        self.assertIn("Sub-agent dispatch", card)

    def test_whole_batch_under_the_cap_keeps_call_id_and_suppresses(self):
        """The complete-document path (the normal case): a batch large
        enough to stress the slice bound but still whole recovers every
        task with its call id — the card keys by the real call id and
        the result row is suppressed."""
        goal = "A very long dispatch whose arguments overflow the cap"
        args = json.dumps({"background": True,
                           "tasks": [{"context": "x" * 4000,
                                      "goal": goal}]})
        raw = json.dumps([{
            "id": "call-bwc", "call_id": "call-bwc", "type": "function",
            "function": {"name": "delegate_agent", "arguments": args},
        }])
        self.assertLess(len(raw), self.mod.DELEGATE_CALLS_CHARS)
        self.add_message(self.SID, "assistant", "", ts=BASE_TS,
                         tool_calls=raw)
        self.add_tool_result(self.SID, "call-bwc", "delegate_agent",
                             BASE_TS + 5, content="BATCH-RESULT")
        body = self.page(self.SID)
        self.assertEqual(self.delegate_keys_in_order(body),
                         [self.card_key("call-bwc")])
        self.assertIn(goal, body)
        self.assertNotIn("BATCH-RESULT", body)

    def test_payload_labels_are_escaped(self):
        self.add_carrier(self.SID, "call-e", "delegate_claude_agent",
                         'Fix <script>alert("x")</script> & pipes',
                         BASE_TS)
        body = self.page(self.SID)
        card = self.card_in_page(body, self.card_key("call-e"))
        self.assertNotIn("<script>", card)
        self.assertIn("&lt;script&gt;", card)

    def test_childless_session_renders_no_card_scaffolding(self):
        self.add_message(self.SID, "user", "plain question", ts=BASE_TS)
        self.add_message(self.SID, "assistant", "plain answer",
                         ts=BASE_TS + 1)
        body = self.page(self.SID)
        # rendered rows, not the CSS/JS that styles them
        self.assertNotIn('<li class="msg delegate-item"', body)
        self.assertNotIn('<li class="msg subagent-item"', body)
        self.assertNotIn('<a class="next-subagent"', body)
        # and the feed carries an empty keyed payload, not an error
        payload = self.feed(self.SID)
        self.assertEqual(payload["subagents"]["count"], 0)


class TestClaudeWatchLinks(DispatchCase):
    """The Claude card ↔ exact live run correlation: receipt-gated, fail
    closed, live before the result row and durable after it."""

    SID = "20260907_watch"
    CALL = "call-watch-1"

    def setUp(self):
        super().setUp()
        self.add_session(self.SID)
        self.add_carrier(self.SID, self.CALL, "delegate_claude_agent",
                         GOAL_CLAUDE, BASE_TS)

    def key(self):
        return self.card_key(self.CALL)

    def test_card_is_viewer_anchor_before_any_result_row(self):
        path = plant_receipt(self.home, self.SID, self.CALL)
        url = json.loads(path.read_text(encoding="utf-8"))["viewer_url"]
        self.assertTrue(url.startswith(("http://", "https://")))
        body = self.page(self.SID)
        card = self.card_in_page(body, self.key())
        self.assertIn('href="%s"' % url, card)
        self.assertIn('target="_blank"', card)
        self.assertIn('rel="noopener"', card)
        self.assertIn("Watch this run in the Claude live viewer", card)
        self.assertIn(">Watch</span>", card)
        self.assertNotIn("dlg-static", card)
        # a run in flight: no result row, the card reads Running
        self.assertIn('data-state="running"', card)

    def test_watch_link_takes_precedence_over_child_link(self):
        plant_receipt(self.home, self.SID, self.CALL)
        self.add_child(self.SID, "child-w", GOAL_CLAUDE, BASE_TS + 5)
        body = self.page(self.SID)
        card = self.card_in_page(body, self.key())
        self.assertNotIn('href="/s/', card)
        self.assertRegex(card, r'href="https?://')
        self.assertIn(">Watch</span>", card)

    def test_completed_card_keeps_the_viewer_link(self):
        plant_receipt(self.home, self.SID, self.CALL)
        self.add_tool_result(self.SID, self.CALL,
                             "delegate_claude_agent", BASE_TS + 240,
                             content="final report: pipeline hardened")
        body = self.page(self.SID)
        card = self.card_in_page(body, self.key())
        self.assertIn('data-state="done"', card)
        self.assertRegex(card, r'href="https?://')
        self.assertIn(">Watch</span>", card)

    def test_missing_receipt_never_fabricates_a_url(self):
        body = self.page(self.SID)
        card = self.card_in_page(body, self.key())
        self.assertIn("dlg-static", card)
        self.assertNotIn("href=", card)
        self.assertNotIn("Watch", card)

    def test_cursor_card_is_never_a_viewer_anchor(self):
        # a receipt keyed to a delegate_cursor_agent call exists, but the
        # viewer link is a Claude-card behavior only
        plant_receipt(self.home, self.SID, "call-cursor-1")
        self.add_carrier(self.SID, "call-cursor-1", "delegate_cursor_agent",
                         GOAL_CURSOR, BASE_TS + 10)
        body = self.page(self.SID)
        card = self.card_in_page(body, self.card_key("call-cursor-1"))
        self.assertNotIn("href=", card)
        self.assertIn("dlg-static", card)

    def test_malformed_or_unsafe_receipts_fail_closed(self):
        """Every receipt that is not exactly this dispatch's own valid
        receipt reads as no receipt: the card keeps its static form and
        never grows an href."""
        url_cases = (
            ("embedded id mismatch",
             lambda p: mutate_receipt(p, session_id="someone-else")),
            ("unknown key", lambda p: mutate_receipt(p, extra="nope")),
            ("wrong schema version",
             lambda p: mutate_receipt(p, schema_version=99)),
            ("javascript url", lambda p: mutate_receipt(
                p, viewer_url="javascript:alert(1)//#20260907-120000-4242")),
            ("userinfo url", lambda p: mutate_receipt(
                p, viewer_url="http://u:p@h:8787/#20260907-120000-4242")),
            ("wrong fragment", lambda p: mutate_receipt(
                p, viewer_url="http://h:8787/#20260908-999999-1")),
            ("oversized receipt", lambda p: mutate_receipt(
                p, workdir="x" * (receipts.MAX_RECEIPT_BYTES + 10))),
        )
        for label, mutate in url_cases:
            path = plant_receipt(self.home, self.SID, self.CALL)
            mutate(path)
            card = self.card_in_page(self.page(self.SID), self.key())
            self.assertNotIn("href=", card, label)
            self.assertIn("dlg-static", card, label)

        # loose permissions
        path = plant_receipt(self.home, self.SID, self.CALL)
        os.chmod(path, 0o644)
        try:
            card = self.card_in_page(self.page(self.SID), self.key())
            self.assertNotIn("href=", card, "loose permissions")
            self.assertIn("dlg-static", card, "loose permissions")
        finally:
            os.chmod(path, 0o600)

        # a symlink standing in for the receipt
        path = plant_receipt(self.home, self.SID, self.CALL)
        outside = os.path.join(self.tmp, "outside.json")
        shutil.copyfile(path, outside)
        os.unlink(path)
        os.symlink(outside, path)
        card = self.card_in_page(self.page(self.SID), self.key())
        self.assertNotIn("href=", card, "symlink receipt")
        self.assertIn("dlg-static", card, "symlink receipt")

        # the receipt is intact but names a run log that never existed
        path = plant_receipt(self.home, self.SID, self.CALL,
                             stem="20260907-130000-777")
        os.unlink(os.path.join(
            self.home, "claude-runs", "20260907-130000-777.jsonl"))
        card = self.card_in_page(self.page(self.SID), self.key())
        self.assertNotIn("href=", card, "missing run log")
        self.assertIn("dlg-static", card, "missing run log")

    def test_cross_profile_receipt_fails_closed(self):
        """A receipt living in ANOTHER profile's home never links this
        profile's card: resolution is exact on (home, session, call)."""
        other_home = os.path.join(self.tmp, "profiles", "alpha")
        os.makedirs(other_home)
        con = sqlite3.connect(os.path.join(other_home, "state.db"))
        con.executescript(SESSION_SCHEMA)
        con.commit()
        con.close()
        plant_receipt(other_home, self.SID, self.CALL)
        body = self.page(self.SID)
        card = self.card_in_page(body, self.key())
        self.assertNotIn("href=", card)
        self.assertIn("dlg-static", card)

    def test_unavailable_receipt_reader_degrades_to_static_cards(self):
        """The receipt validator is an optional capability of this
        stdlib-only server module: with the tools layer unavailable the
        page still renders every card, just always static — the viewer
        link degrades, never the page."""
        plant_receipt(self.home, self.SID, self.CALL)
        self.mod._RECEIPTS_LOADED = True
        self.mod._RECEIPTS_MODULE = None
        body = self.page(self.SID)
        card = self.card_in_page(body, self.key())
        self.assertNotIn("href=", card)
        self.assertIn("dlg-static", card)
        # and it recovers the moment the capability comes back
        self.mod._RECEIPTS_LOADED = False
        self.mod._RECEIPTS_MODULE = None
        card = self.card_in_page(self.page(self.SID), self.key())
        self.assertRegex(card, r'href="https?://')


class TestFeedReconciliation(DispatchCase):
    """Initial render and /feed polling share one renderer and one key
    per row, so a static card upgrades in place — exactly once."""

    SID = "20260907_feed"
    CALL = "call-feed-1"

    def setUp(self):
        super().setUp()
        self.add_session(self.SID)
        self.add_carrier(self.SID, self.CALL, "delegate_claude_agent",
                         GOAL_CLAUDE, BASE_TS)
        self.add_carrier(self.SID, "call-feed-2", "delegate_agent",
                         GOAL_AGENT, BASE_TS + 10)
        self.add_child(self.SID, "child-f", GOAL_AGENT, BASE_TS + 15)

    def key(self):
        return self.card_key(self.CALL)

    def test_page_and_feed_share_renderer_and_revision_key(self):
        body = self.page(self.SID)
        payload = self.feed(self.SID)
        items = {i["key"]: i for i in payload["subagents"]["items"]}
        # one keyed identity space: the matched child's card (from the
        # delegate_agent carrier) and the standalone claude card
        self.assertIn(self.key(), items)
        self.assertIn(self.card_key("call-feed-2"), items)
        # the matched child rides only as its card — no duplicate row
        self.assertNotIn("default-child-f", items)
        for key, item in items.items():
            li = self.card_in_page(body, key)
            # identical markup, identical revision, one identity
            self.assertIn(item["html"], body.replace("</li>\n", "</li>\n"),
                          key)
            self.assertEqual(
                re.search(r'data-rev="([^"]*)"', li).group(1),
                item["rev"], key)
            self.assertEqual(
                re.search(r'data-state="([^"]*)"', li).group(1),
                item["state"], key)
            for field in ("id", "profile", "started", "state", "rev",
                          "html"):
                self.assertIn(field, item)

    def test_static_card_upgrades_in_place_exactly_once(self):
        """The /feed lifecycle of the watch link: no receipt yet (static,
        running), the receipt lands (same key, one revision change, an
        anchor), further polls change nothing."""
        # before: static card, its rev captured from the page itself
        body = self.page(self.SID)
        static = self.card_in_page(body, self.key())
        self.assertIn("dlg-static", static)
        rev0 = re.search(r'data-rev="([^"]*)"', static).group(1)

        first = {i["key"]: i for i in self.feed(self.SID)["subagents"]
                 ["items"]}
        self.assertIn(self.key(), first)
        self.assertEqual(first[self.key()]["rev"], rev0)
        self.assertNotIn("href=", first[self.key()]["html"])

        # the receipt appears (exactly when on_spawn fires for a late
        # spawn): the same key upgrades, rev moves once
        path = plant_receipt(self.home, self.SID, self.CALL)
        url = json.loads(path.read_text(encoding="utf-8"))["viewer_url"]
        second = {i["key"]: i for i in self.feed(self.SID)["subagents"]
                  ["items"]}
        self.assertIn(self.key(), second)
        up = second[self.key()]
        self.assertNotEqual(up["rev"], rev0)
        self.assertIn('href="%s"' % url, up["html"])
        self.assertEqual(up["state"], "running")
        # one identity, not a second row beside the static one
        keys = [i["key"] for i in self.feed(self.SID)["subagents"]["items"]]
        self.assertEqual(keys.count(self.key()), 1)

        # steady state: the next poll changes nothing at all
        third = {i["key"]: i for i in self.feed(self.SID)["subagents"]
                 ["items"]}
        self.assertEqual(third[self.key()]["rev"], up["rev"])
        self.assertEqual(third[self.key()]["html"], up["html"])

        # and the fresh page render agrees with the upgraded feed row
        fresh = self.card_in_page(self.page(self.SID), self.key())
        self.assertIn('href="%s"' % url, fresh)
        self.assertEqual(
            re.search(r'data-rev="([^"]*)"', fresh).group(1), up["rev"])

    def test_next_subagent_pill_anchors_the_first_keyed_row(self):
        # first keyed row here is the claude card (carrier at BASE_TS)
        body = self.page(self.SID)
        m = re.search(r'<a class="next-subagent" id="next-subagent"'
                      r' href="#sa-([^"]+)"', body)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), self.key())

    def test_next_subagent_pill_covers_child_rows_too(self):
        # a session whose first keyed row is a standalone child
        sid = "20260907_pillchild"
        self.add_session(sid)
        self.add_child(sid, "child-p", GOAL_AGENT, BASE_TS + 1)
        body = self.page(sid)
        m = re.search(r'<a class="next-subagent" id="next-subagent"'
                      r' href="#sa-([^"]+)"', body)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "default-child-p")


if __name__ == "__main__":
    unittest.main(verbosity=2)
