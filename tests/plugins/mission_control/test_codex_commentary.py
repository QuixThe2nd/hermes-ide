"""Regression tests for the Codex commentary fallback in the
mission_control server.

A Codex tool-call assistant row keeps content='' and stores its
narration in codex_message_items in the exact shape the responses
adapter persists:
[{"type": "message", "role": "assistant", "status": "completed",
   "phase": "commentary",
   "content": [{"type": "output_text", "text": ...}]}]. The historical
bug this file guards against: CHAT_PAGE_SQL and FEED_AFTER_SQL read
only content, so chat_messages() dropped every one of those rows and
the page showed collapsed tool groups with none of the narration
proven present in SQLite.

The invariant under test, in one sentence: an assistant row whose
content is empty recovers exactly its visible output_text/text
commentary — from a message item that is COMPLETE (status exactly
"completed") and exactly in the "commentary" phase — rendered as a
normal agent message that splits tool groups, on the page and in
every /feed poll alike; a row with content stays the sole authority;
and malformed, truncated, oversize, too-deep, legacy, wrong-role,
wrong-phase, in-progress, reasoning or function-call JSON never
surfaces anything and never breaks the render.

Structural bounds (item count, block count, nesting depth, aggregate
text, raw input size) are exercised at their exact limit and one past
it. Chronology is exercised with non-monotonic timestamps, and the
collapsed-tool-group seam is exercised across split feed polls.

Unit cases feed chat_messages() rows in the exact SQL column order;
end-to-end cases drive a throwaway state.db (production-shaped schema)
through load_chat() and load_feed().
"""

import html
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import plugins.mission_control.server as server


# The messages shape the chat queries read, including the
# codex_message_items column the fallback projects from (kept in the
# production column set so the fixture answers the same SQL the live
# DBs do).
SCHEMA = """
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


def commentary_item(text, role="assistant", itype="message",
                    block_type="output_text", phase="commentary",
                    status="completed"):
    """One codex_message_items entry in the stable live shape.

    status/phase match what agent/codex_responses_adapter.py persists:
    every message item carries a normalized status, and the phase is
    written exactly when the run produced one. Pass phase=None or
    status=None to build the degenerate shapes the parser must
    reject."""
    item = {"type": itype, "role": role}
    if status is not None:
        item["status"] = status
    if phase is not None:
        item["phase"] = phase
    item["content"] = [{"type": block_type, "text": text}]
    return item


def commentary_json(*items, **kwargs):
    return json.dumps(list(items), **kwargs)


def row(role, content, row_id, ts, tool_name=None, codex="",
        tool_calls="", tool_call_id=None, finish_reason=None):
    """A transcript row in the exact CHAT_PAGE_SQL/FEED_AFTER_SQL
    column order: role, tool_name, timestamp, id, content, tool_calls,
    tool_call_id, finish_reason, then the bounded Codex fallback."""
    return (role, tool_name, ts, row_id, content, tool_calls,
            tool_call_id, finish_reason, codex)


NARRATION = "Stepping into the repo to check the failing tests."


class TestValidRecovery(unittest.TestCase):
    """An empty-content assistant carrier recovers its narration."""

    def test_live_shape_recovers_text_with_role_and_id(self):
        rows = [row("assistant", "", 7, 1000.0,
                    codex=commentary_json(commentary_item(NARRATION)))]
        msgs = server.chat_messages(rows)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0], {"kind": "text", "role": "assistant",
                                   "ts": 1000.0, "id": 7,
                                   "text": NARRATION})

    def test_bare_item_dict_recovers_too(self):
        rows = [row("assistant", "", 7, 1000.0,
                    codex=json.dumps(commentary_item(NARRATION)))]
        self.assertEqual([m["text"] for m in server.chat_messages(rows)],
                         [NARRATION])

    def test_plain_text_block_type_recovers(self):
        rows = [row("assistant", "", 7, 1000.0,
                    codex=commentary_json(
                        commentary_item(NARRATION, block_type="text")))]
        self.assertEqual([m["text"] for m in server.chat_messages(rows)],
                         [NARRATION])

    def test_blocks_and_items_join_with_breaks(self):
        codex = commentary_json(
            {"type": "message", "role": "assistant",
             "status": "completed", "phase": "commentary",
             "content": [{"type": "output_text", "text": "one"},
                         {"type": "text", "text": "two"}]},
            commentary_item("three"),
        )
        self.assertEqual(server.codex_commentary_text(codex),
                         "one\ntwo\n\nthree")

    def test_null_content_carrier_recovers(self):
        rows = [row("assistant", None, 7, 1000.0,
                    codex=commentary_json(commentary_item(NARRATION)))]
        self.assertEqual([m["text"] for m in server.chat_messages(rows)],
                         [NARRATION])

    def test_oversize_source_is_rejected_whole(self):
        # Bigger than the parser's input cap: rejected, never sliced
        # into an accepted truncated prefix.
        blob = commentary_json(commentary_item("x" * 9000))
        self.assertEqual(server.codex_commentary_text(blob), "")


class TestNormalContentWins(unittest.TestCase):
    """Content stays the sole authority; codex never duplicates it."""

    def test_content_row_renders_only_content(self):
        rows = [row("assistant", "plain answer", 7, 1000.0,
                    codex=commentary_json(
                        commentary_item("fallback narration")))]
        msgs = server.chat_messages(rows)
        self.assertEqual([m["text"] for m in msgs], ["plain answer"])
        self.assertNotIn("fallback narration",
                         json.dumps(msgs, default=str))

    def test_sql_never_projects_codex_when_content_present(self):
        # At the source: the fallback column selects '' for a row with
        # content, so the blob cannot even leave the DB for it.
        con = sqlite3.connect(":memory:")
        con.executescript(SCHEMA)
        con.execute("INSERT INTO sessions (id, source, started_at,"
                    " last_activity_at) VALUES ('s', 'cli', 1, 1)")
        con.execute("INSERT INTO messages (session_id, role, content,"
                    " codex_message_items, timestamp)"
                    " VALUES ('s', 'assistant', 'real', ?, 1)",
                    (commentary_json(commentary_item("hidden")),))
        proj = con.execute(server.CHAT_PAGE_SQL, ("s",)).fetchall()[0][8]
        self.assertEqual(proj, "")
        con.close()


class TestUnrecoverableDataStaysHidden(unittest.TestCase):
    """Everything that is not a strict assistant message item yields
    no text — and never raises."""

    HIDDEN = {
        "malformed": "{not json at all",
        "truncated": commentary_json(
            commentary_item("cut off mid"))[:25],
        "wrong role user": commentary_json(
            commentary_item("user says", role="user")),
        "wrong role tool": commentary_json(
            commentary_item("tool payload", role="tool")),
        "reasoning item": json.dumps(
            [{"type": "reasoning", "summary":
              [{"type": "summary_text", "text": "inner monologue"}]}]),
        "function call item": json.dumps(
            [{"type": "function_call", "name": "shell",
              "arguments": json.dumps(
                  {"command": "rm -rf / --no-preserve-root"})}]),
        "tool result item": json.dumps(
            [{"type": "tool_result", "call_id": "c1",
              "output": [{"type": "output_text", "text": "leak"}]}]),
        "legacy string content": json.dumps(
            [{"type": "message", "role": "assistant",
              "content": "legacy bare string"}]),
        "unknown block type": commentary_json(
            {"type": "message", "role": "assistant",
             "content": [{"type": "input_text", "text": "tool input"}]}),
        "non-string block text": commentary_json(
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text",
                          "text": {"nested": "dict"}}]}),
        "content not a list": commentary_json(
            {"type": "message", "role": "assistant",
             "content": {"type": "output_text", "text": "wrapped"}}),
        "top level string": json.dumps("just a string"),
        "top level number": json.dumps(42),
        "non-dict list items": json.dumps(["one", 2, None]),
        "phase-less message": json.dumps(
            [{"type": "message", "role": "assistant",
              "status": "completed",
              "content": [{"type": "output_text",
                           "text": "no phase written"}]}]),
        "analysis phase": commentary_json(
            commentary_item("planning", phase="analysis")),
        "reasoning phase": commentary_json(
            commentary_item("thinking", phase="reasoning")),
        "final phase": commentary_json(
            commentary_item("the answer", phase="final")),
        "final_answer phase": commentary_json(
            commentary_item("the answer", phase="final_answer")),
        "in-progress status": commentary_json(
            commentary_item("still streaming", status="in_progress")),
        "incomplete status": commentary_json(
            commentary_item("cut off", status="incomplete")),
        "failed status": commentary_json(
            commentary_item("errored", status="failed")),
        "cancelled status": commentary_json(
            commentary_item("aborted", status="cancelled")),
    }

    def test_parser_yields_nothing_for_each(self):
        for label, blob in sorted(self.HIDDEN.items()):
            self.assertEqual(server.codex_commentary_text(blob), "",
                             label)
            rows = [row("assistant", "", 7, 1000.0, codex=blob)]
            self.assertEqual(server.chat_messages(rows), [], label)

    def test_non_string_raw_values(self):
        for raw in (None, 123, [], {}, b"bytes"):
            self.assertEqual(server.codex_commentary_text(raw), "")

    def test_deep_nesting_never_raises(self):
        self.assertEqual(server.codex_commentary_text("[" * 4000), "")


class TestStructuralLimits(unittest.TestCase):
    """Every parser bound, at its exact limit and one past it."""

    def test_item_count_limit(self):
        # Compact encoding: the input-size cap (4000 chars) sits below
        # what 32 verbose items would occupy, so the exact limit and
        # its +1 case are built small enough that the size cap cannot
        # be the thing that rejects them.
        n = server.CODEX_ITEMS_MAX_ITEMS
        at = json.dumps(
            [commentary_item("x", block_type="text") for _ in range(n)],
            separators=(",", ":"))
        self.assertLessEqual(len(at), server.CODEX_ITEMS_MAX_CHARS)
        self.assertNotEqual(server.codex_commentary_text(at), "")
        over = json.dumps(
            [commentary_item("x", block_type="text")
             for _ in range(n + 1)],
            separators=(",", ":"))
        self.assertLessEqual(len(over), server.CODEX_ITEMS_MAX_CHARS)
        self.assertEqual(server.codex_commentary_text(over), "")

    def test_block_count_limit(self):
        n = server.CODEX_ITEM_MAX_BLOCKS
        block = {"type": "output_text", "text": "b"}
        at = commentary_json(commentary_item("lead"))
        at = json.loads(at)
        at[0]["content"] = [dict(block)] * n
        self.assertNotEqual(server.codex_commentary_text(
            json.dumps(at)), "")
        at[0]["content"] = [dict(block)] * (n + 1)
        self.assertEqual(server.codex_commentary_text(
            json.dumps(at)), "")

    def test_nesting_depth_limit(self):
        # The junk branch's deepest node sits at depth exactly the cap
        # (top list 0, item dict 1, junk list 2, +1 per wrap): the
        # value survives and its real commentary still comes back.
        base = {"type": "message", "role": "assistant",
                "status": "completed", "phase": "commentary",
                "content": [{"type": "output_text", "text": "ok"}]}
        wraps = server.CODEX_ITEMS_MAX_DEPTH - 2
        deep_enough = json.dumps(
            [dict(base, junk=self._nested(wraps, "x"))])
        self.assertEqual(server.codex_commentary_text(deep_enough), "ok")
        too_deep = json.dumps(
            [dict(base, junk=self._nested(wraps + 1, "x"))])
        self.assertEqual(server.codex_commentary_text(too_deep), "")

    @staticmethod
    def _nested(depth, leaf):
        value = leaf
        for _ in range(depth):
            value = [value]
        return value

    def test_aggregate_text_cap(self):
        cap = server.CHAT_TEXT_CHARS
        # Through the normal door the input-size cap (equal to the
        # text cap) already bounds aggregate text below this point;
        # lift it here to prove the parser's own aggregate clamp at
        # its exact boundary and one past it.
        with mock.patch.object(server, "CODEX_ITEMS_MAX_CHARS", cap * 4):
            at = commentary_json(commentary_item("a" * cap))
            self.assertLess(len(at), cap * 4)
            self.assertEqual(server.codex_commentary_text(at), "a" * cap)
            # limit+1: the second item's marker must never land
            over = commentary_json(commentary_item("a" * (cap - 2)),
                                   commentary_item("ZZZ"))
            out = server.codex_commentary_text(over)
            self.assertEqual(len(out), cap)
            self.assertNotIn("ZZZ", out)

    def test_input_size_cap_rejects_json_plus_padding(self):
        # A value that parses as valid JSON but carries trailing
        # padding past the cap must be rejected whole — never accepted
        # as its truncated prefix.
        ok = commentary_json(
            commentary_item("a" * (server.CODEX_ITEMS_MAX_CHARS - 300)))
        self.assertLessEqual(len(ok), server.CODEX_ITEMS_MAX_CHARS)
        self.assertNotEqual(server.codex_commentary_text(ok), "")
        padded = ok + " " * (
            server.CODEX_ITEMS_MAX_CHARS - len(ok) + 1)
        self.assertGreater(len(padded), server.CODEX_ITEMS_MAX_CHARS)
        self.assertEqual(server.codex_commentary_text(padded), "")

    def test_lone_surrogate_is_sanitized_not_fatal(self):
        blob = commentary_json(
            commentary_item("bad \ud800 pair \udfff here"))
        out = server.codex_commentary_text(blob)
        self.assertNotIn("\ud800", out)
        self.assertNotIn("\udfff", out)
        self.assertIn("\N{REPLACEMENT CHARACTER}", out)
        # and the recovered text still encodes cleanly for HTTP/HTML
        out.encode("utf-8")

    def test_mixed_list_surfaces_only_the_message(self):
        blob = commentary_json(
            {"type": "reasoning", "summary": []},
            {"type": "function_call", "name": "shell",
             "arguments": "{\"command\": \"rm -rf /\"}"},
            commentary_item(NARRATION),
        )
        rows = [row("assistant", "", 7, 1000.0, codex=blob)]
        msgs = server.chat_messages(rows)
        self.assertEqual([m["text"] for m in msgs], [NARRATION])

    def test_silent_marker_row_still_skipped(self):
        rows = [row("assistant", "[SILENT]", 7, 1000.0,
                    codex=commentary_json(
                        commentary_item(NARRATION)))]
        self.assertEqual(server.chat_messages(rows), [])

    def test_empty_user_row_never_uses_fallback(self):
        rows = [row("user", "", 7, 1000.0,
                    codex=commentary_json(commentary_item(NARRATION)))]
        self.assertEqual(server.chat_messages(rows), [])

    def test_whitespace_content_row_is_not_the_commentary_shape(self):
        # Only a strictly empty content column selects the fallback
        # projection, so a whitespace-only row keeps today's behavior.
        rows = [row("assistant", "   ", 7, 1000.0,
                    codex=commentary_json(commentary_item(NARRATION)))]
        self.assertEqual(server.chat_messages(rows), [])


class TestToolGrouping(unittest.TestCase):
    """Recovered commentary splits tool runs; empty carriers do not."""

    def test_commentary_between_tools_splits_groups(self):
        rows = [
            row("tool", "out", 1, 1000.0, tool_name="read_file"),
            row("assistant", "", 2, 1001.0,
                codex=commentary_json(commentary_item(NARRATION))),
            row("tool", "out2", 3, 1002.0, tool_name="run_py"),
        ]
        items = server.chat_items(server.chat_messages(rows))
        self.assertEqual([it["kind"] for it in items],
                         ["tools", "text", "tools"])
        self.assertEqual(len(items[0]["items"]), 1)
        self.assertEqual(items[1]["id"], 2)
        self.assertEqual(len(items[2]["items"]), 1)

    def test_empty_carrier_keeps_one_group(self):
        rows = [
            row("tool", "out", 1, 1000.0, tool_name="read_file"),
            row("assistant", "", 2, 1001.0,
                codex=commentary_json(
                    commentary_item("x"))[:25]),  # truncated -> hidden
            row("tool", "out2", 3, 1002.0, tool_name="run_py"),
        ]
        items = server.chat_items(server.chat_messages(rows))
        self.assertEqual([it["kind"] for it in items], ["tools"])
        self.assertEqual(len(items[0]["items"]), 2)
        self.assertEqual(items[0]["id"], 3)  # group id = newest row

    def test_plain_empty_carrier_keeps_one_group(self):
        rows = [
            row("tool", "out", 1, 1000.0, tool_name="read_file"),
            row("assistant", "", 2, 1001.0, tool_calls='[{"id":"c1"}]'),
            row("tool", "out2", 3, 1002.0, tool_name="run_py"),
        ]
        items = server.chat_items(server.chat_messages(rows))
        self.assertEqual([it["kind"] for it in items], ["tools"])
        self.assertEqual(len(items[0]["items"]), 2)


class CommentaryDB(unittest.TestCase):
    """A throwaway state.db driven through load_chat()/load_feed()."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-commentary-")
        self.db = os.path.join(self.tmp, "state.db")
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA)
        con.commit()
        con.close()
        self.dbs = {"default": self.db}
        # The connection boundary validates every opened path against the
        # served home, so the shared module must be pointed at this
        # fixture's home for the DB-backed tests below (the pure-function
        # classes above never open a DB either way).
        self._patchers = [
            mock.patch.object(server, "MAIN_DB", self.db),
            mock.patch.object(
                server, "PROFILE_GLOB",
                os.path.join(self.tmp, "no-such-profile", "*", "state.db")),
        ]
        for p in self._patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patchers])
        self.sid = "20260902_190023_924136cd"
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO sessions (id, source, title, started_at,"
                    " last_activity_at) VALUES (?, 'cli', ?, 1, 1)",
                    (self.sid, "fixture"))
        con.commit()
        con.close()
        self._seq = 0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_message(self, role, content=None, tool_name=None, codex=None,
                    ts=None):
        self._seq += 1
        con = sqlite3.connect(self.db)
        try:
            con.execute(
                "INSERT INTO messages (session_id, role, content,"
                " tool_name, codex_message_items, timestamp)"
                " VALUES (?,?,?,?,?,?)",
                (self.sid, role, content, tool_name, codex,
                 (1000.0 + self._seq) if ts is None else ts))
            con.commit()
            row_id = con.execute(
                "SELECT id FROM messages WHERE session_id = ?"
                " ORDER BY id DESC LIMIT 1", (self.sid,)).fetchone()[0]
        finally:
            con.close()
        return row_id

    def page_items(self):
        chat = server.load_chat("default", self.sid, self.dbs)
        return server.chat_items(server.chat_messages(chat["rows"]))

    def feed(self, after=0):
        return server.load_feed("default", self.sid, self.dbs, after)


class TestPageAndFeed(CommentaryDB):
    """The fallback behaves identically on the initial render and the
    /feed delta polls, with cursor ids and ordering preserved."""

    def test_page_and_full_snapshot_render_identical_commentary(self):
        self.add_message("user", "please fix the tests")
        self.add_message("tool", '{"ok": true}', tool_name="run_py")
        cid = self.add_message(
            "assistant", "",
            codex=commentary_json(commentary_item(NARRATION)))
        self.assertEqual(self.page_items(), self.feed(after=0)["items"])
        entry = self.feed(after=0)["items"][-1]
        self.assertEqual((entry["kind"], entry["id"], entry["text"]),
                         ("text", cid, NARRATION))

    def test_delta_poll_returns_only_new_rows_in_order(self):
        uid = self.add_message("user", "go")
        tid = self.add_message("tool", "out", tool_name="read_file")
        cid = self.add_message(
            "assistant", "",
            codex=commentary_json(commentary_item(NARRATION)))
        feed = self.feed(after=uid)
        self.assertEqual([it["id"] for it in feed["items"]], [tid, cid])
        self.assertEqual([it["kind"] for it in feed["items"]],
                         ["tools", "text"])
        self.assertEqual(feed["items"][1]["text"], NARRATION)
        self.assertEqual(feed["last_id"], cid)

    def test_rendered_item_carries_escaped_text_and_row_id(self):
        spicy = 'Reading <b>notes</b> & "logs" now'
        cid = self.add_message(
            "assistant", "", codex=commentary_json(
                commentary_item(spicy)))
        entry = self.feed(after=0)["items"][0]
        self.assertEqual(entry["id"], cid)
        self.assertEqual(entry["role"], "assistant")
        self.assertEqual(entry["text"], spicy)
        markup = server.render_chat_item(entry, "default")
        self.assertIn(html.escape(spicy), markup)
        self.assertNotIn("<b>", markup)

    def test_page_markup_carries_escaped_commentary(self):
        spicy = 'Reading <b>notes</b> & "logs" now'
        self.add_message("assistant", "",
                         codex=commentary_json(commentary_item(spicy)))
        page = server.render_chat(
            server.load_chat("default", self.sid, self.dbs))
        self.assertIn(html.escape(spicy), page)
        self.assertNotIn("<b>notes", page)

    def test_blob_past_sql_bound_is_rejected_whole(self):
        # The projection refuses to select an oversized value at all
        # (length() gate in SQL): the fallback column arrives empty, so
        # an oversize blob can never be accepted as a truncated prefix
        # — never the raw JSON, never a crash.
        self.add_message("user", "go")
        self.add_message("assistant", "", codex=commentary_json(
            commentary_item("y" * 9000)))
        chat = server.load_chat("default", self.sid, self.dbs)
        carrier = [r for r in chat["rows"] if r[0] == "assistant"][0]
        self.assertEqual(carrier[8], "")
        items = self.chat_items(chat)
        self.assertEqual([it["kind"] for it in items], ["text"])

    def test_valid_json_with_padding_is_rejected_in_sql(self):
        # Valid JSON followed by padding past the bound: SQL must hand
        # the parser nothing, not a parseable prefix of the value.
        ok = commentary_json(commentary_item("short narration"))
        padded = ok + " " * (server.CODEX_ITEMS_MAX_CHARS
                             - len(ok) + 1)
        self.add_message("user", "go")
        self.add_message("assistant", "", codex=padded)
        chat = server.load_chat("default", self.sid, self.dbs)
        carrier = [r for r in chat["rows"] if r[0] == "assistant"][0]
        self.assertEqual(carrier[8], "")
        self.assertEqual([it["kind"] for it in self.chat_items(chat)],
                         ["text"])

    def chat_items(self, chat):
        return server.chat_items(server.chat_messages(chat["rows"]))

    def test_normal_content_page_shows_no_fallback_text(self):
        self.add_message("assistant", "real answer", codex=commentary_json(
            commentary_item("fallback narration")))
        items = self.page_items()
        self.assertEqual([it["text"] for it in items], ["real answer"])

    def test_carrier_between_tools_splits_groups_end_to_end(self):
        self.add_message("tool", "out", tool_name="read_file")
        self.add_message("assistant", "",
                         codex=commentary_json(commentary_item(NARRATION)))
        self.add_message("tool", "out2", tool_name="run_py")
        kinds = [it["kind"] for it in self.feed(after=0)["items"]]
        self.assertEqual(kinds, ["tools", "text", "tools"])


class TestChronologyAndGroupSeam(CommentaryDB):
    """Row id is the one authoritative chronology, and one maximal
    tool run stays one group however its rows split across feed
    polls."""

    def test_non_monotonic_timestamps_keep_id_order(self):
        # Timestamps written out of order (a tool result stamped
        # before the user row that precedes it by insertion): the
        # page and the full feed still render in id order.
        uid = self.add_message("user", "A", ts=1005.0)
        tid = self.add_message("tool", "out", tool_name="read_file",
                               ts=1001.0)
        cid = self.add_message("assistant", "C final", ts=1003.0)
        page_ids = [it["id"] for it in self.page_items()]
        self.assertEqual(page_ids, [uid, tid, cid])
        full = self.feed(after=0)
        self.assertEqual([it["id"] for it in full["items"]],
                         [uid, tid, cid])
        # A delta poll started before the tool row landed agrees.
        delta = self.feed(after=uid)
        self.assertEqual([it["id"] for it in delta["items"]],
                         [tid, cid])

    def test_story_shape_on_all_three_surfaces(self):
        # A -> collapsed tools[1,2] -> commentary B -> collapsed
        # tool[3] -> final C, identical on the initial page, the full
        # feed and the (whole-conversation) delta feed.
        uid = self.add_message("user", "A")
        t1 = self.add_message("tool", "o1", tool_name="read_file")
        t2 = self.add_message("tool", "o2", tool_name="run_py")
        bid = self.add_message(
            "assistant", "", codex=commentary_json(
                commentary_item("B between")))
        t3 = self.add_message("tool", "o3", tool_name="grep")
        cid = self.add_message("assistant", "C final")

        surfaces = (("page", self.page_items()),
                    ("full feed", self.feed(after=0)["items"]))
        for label, items in surfaces:
            self.assertEqual([it["kind"] for it in items],
                             ["text", "tools", "text", "tools", "text"],
                             label)
            self.assertEqual([t["id"] for t in items[1]["items"]],
                             [t1, t2], label)
            self.assertEqual(items[1]["first_id"], t1, label)
            self.assertEqual(items[1]["id"], t2, label)
            self.assertEqual([t["id"] for t in items[3]["items"]], [t3],
                             label)
            self.assertEqual(items[3]["first_id"], t3, label)
            self.assertEqual([it["id"] for it in items],
                             [uid, t2, bid, t3, cid], label)
        # The delta poll (cursor after A) leads with the collapsed
        # pair — complete, same first_id — then B, tool[3], C.
        delta = self.feed(after=uid)["items"]
        self.assertEqual([it["kind"] for it in delta],
                         ["tools", "text", "tools", "text"])
        self.assertEqual([t["id"] for t in delta[0]["items"]], [t1, t2])
        self.assertEqual(delta[0]["first_id"], t1)
        self.assertEqual([t["id"] for t in delta[2]["items"]], [t3])
        self.assertEqual([it["id"] for it in delta],
                         [t2, bid, t3, cid])

    def test_group_html_carries_data_first_id(self):
        self.add_message("tool", "o1", tool_name="read_file")
        self.add_message("tool", "o2", tool_name="run_py")
        entry = self.feed(after=0)["items"][0]
        markup = server.render_chat_item(entry, "default")
        self.assertIn('data-first-id="%d"' % entry["first_id"], markup)
        self.assertIn("2 tool calls", markup)

    def test_delta_poll_merges_split_tool_run_into_one_group(self):
        # Poll 1 draws tools[1]; by poll 2 the run has grown to
        # [1,2,3] and a final text landed. The delta must re-deliver
        # the COMPLETE group (backfilled before the cursor) with the
        # same first_id the client already drew, so its seam merge
        # replaces the shorter element instead of appending a second
        # adjacent group.
        self.add_message("user", "A")
        t1 = self.add_message("tool", "o1", tool_name="read_file")
        first = self.feed(after=0)
        self.assertEqual([it["kind"] for it in first["items"]],
                         ["text", "tools"])
        self.assertEqual(first["items"][1]["first_id"], t1)
        cursor = first["last_id"]
        t2 = self.add_message("tool", "o2", tool_name="run_py")
        t3 = self.add_message("tool", "o3", tool_name="grep")
        cid = self.add_message("assistant", "C final")
        delta = self.feed(after=cursor)
        self.assertEqual([it["kind"] for it in delta["items"]],
                         ["tools", "text"])
        group = delta["items"][0]
        self.assertEqual(group["first_id"], t1)
        self.assertEqual(group["id"], t3)
        self.assertEqual([t["id"] for t in group["items"]], [t1, t2, t3])
        self.assertEqual(delta["items"][1]["id"], cid)
        self.assertEqual(delta["last_id"], cid)

    def test_delta_poll_never_merges_across_intervening_text(self):
        # Text landed between the drawn group and the new tool row:
        # the delta leads with that text (no backfill is triggered),
        # and the new group holds only its own row.
        self.add_message("user", "A")
        self.add_message("tool", "o1", tool_name="read_file")
        first = self.feed(after=0)
        cursor = first["last_id"]
        bid = self.add_message("assistant", "B between")
        t2 = self.add_message("tool", "o2", tool_name="grep")
        delta = self.feed(after=cursor)
        self.assertEqual([it["kind"] for it in delta["items"]],
                         ["text", "tools"])
        self.assertEqual(delta["items"][0]["id"], bid)
        self.assertEqual([t["id"] for t in delta["items"][1]["items"]],
                         [t2])
        self.assertEqual(delta["items"][1]["first_id"], t2)

    def test_backfill_stops_at_text_preceding_the_run(self):
        # The delta's first row is a tool whose immediately preceding
        # displayable row is TEXT: the walk backwards stops there, so
        # the group starts fresh and never swallows earlier tools.
        self.add_message("user", "A")
        self.add_message("tool", "o1", tool_name="read_file")
        bid = self.add_message("assistant", "B between")
        cursor = self.feed(after=0)["last_id"]
        t2 = self.add_message("tool", "o2", tool_name="grep")
        delta = self.feed(after=cursor)
        self.assertEqual([it["kind"] for it in delta["items"]], ["tools"])
        self.assertEqual([t["id"] for t in delta["items"][0]["items"]],
                         [t2])

    def test_backfill_steps_over_dropped_carriers(self):
        # An empty assistant carrier (nothing recoverable) between two
        # tool rows is transparent: the backfill walks over it and the
        # run stays one group.
        self.add_message("user", "A")
        t1 = self.add_message("tool", "o1", tool_name="read_file")
        self.add_message("assistant", "", codex="{not json")
        cursor = self.feed(after=0)["last_id"]
        t2 = self.add_message("tool", "o2", tool_name="grep")
        delta = self.feed(after=cursor)
        self.assertEqual([it["kind"] for it in delta["items"]], ["tools"])
        self.assertEqual([t["id"] for t in delta["items"][0]["items"]],
                         [t1, t2])


if __name__ == "__main__":
    unittest.main()
