"""Regression tests for the Codex commentary fallback in the
mission_control server.

A Codex tool-call assistant row keeps content='' and stores its
narration in codex_message_items as
[{"type": "message", "role": "assistant", "phase": "commentary",
   "content": [{"type": "output_text", "text": ...}]}]. The historical
bug this file guards against: CHAT_PAGE_SQL and FEED_AFTER_SQL read
only content, so chat_messages() dropped every one of those rows and
the page showed collapsed tool groups with none of the narration
proven present in SQLite.

The invariant under test, in one sentence: an assistant row whose
content is empty recovers exactly its visible output_text/text
commentary from a substr-bounded codex_message_items projection —
rendered as a normal agent message that splits tool groups, on the
page and in every /feed poll alike — while a row with content stays
the sole authority, and malformed, truncated, legacy, wrong-role,
reasoning or function-call JSON never surfaces anything and never
breaks the render.

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
                    block_type="output_text"):
    """One codex_message_items entry in the stable live shape."""
    return {"type": itype, "role": role, "phase": "commentary",
            "content": [{"type": block_type, "text": text}]}


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

    def test_recovered_text_clamped_to_transcript_limit(self):
        blob = commentary_json(commentary_item("x" * 9000))
        out = server.codex_commentary_text(blob)
        self.assertEqual(len(out), server.CHAT_TEXT_CHARS)
        self.assertTrue(out.startswith("x"))
        self.assertEqual(server.CHAT_TEXT_CHARS, 4000)


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

    def add_message(self, role, content=None, tool_name=None, codex=None):
        self._seq += 1
        con = sqlite3.connect(self.db)
        try:
            con.execute(
                "INSERT INTO messages (session_id, role, content,"
                " tool_name, codex_message_items, timestamp)"
                " VALUES (?,?,?,?,?,?)",
                (self.sid, role, content, tool_name, codex,
                 1000.0 + self._seq))
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

    def test_blob_past_sql_bound_is_truncated_and_hidden(self):
        # The projection leaves SQLite substr-capped at 4000 chars, so
        # an oversize blob arrives cut mid-JSON, fails the defensive
        # parse and yields no text — never the raw JSON, never a crash.
        self.add_message("user", "go")
        self.add_message("assistant", "", codex=commentary_json(
            commentary_item("y" * 9000)))
        chat = server.load_chat("default", self.sid, self.dbs)
        carrier = [r for r in chat["rows"] if r[0] == "assistant"][0]
        self.assertEqual(len(carrier[8]), 4000)
        items = self.chat_items(chat)
        self.assertEqual([it["kind"] for it in items], ["text"])

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


if __name__ == "__main__":
    unittest.main()
