"""Regression tests for the research-job lineage pass in the
mission_control server.

A delegate_research job dispatches its lanes/synthesis/correction as
worker sessions in the researcher profile DB. Those runs belong under
their dispatching parent's Sub-agents section, never in the top-level
inbox — but only when the durable job artifacts (request.json,
status.json, prompts/*.md) prove the link. The historical bug this file
guards against: the prompt read cap was once 8192 characters, so every
synthesis/correction prompt (tens of thousands of characters) was
skipped instead of matched, and those worker sessions leaked into the
inbox as ordinary top-level chats.

The invariant under test, in one sentence: a worker session is hidden
from the top level exactly when one job's artifacts uniquely prove it
(prompt byte-equality inside the job's time window, origin session
existing, no competing parent, and the run's own source explicitly
marking a non-human worker), and every weaker case — no match, an
oversize or wrapped prompt, a missing origin, an out-of-window start,
an ambiguous collision, a human-facing source like 'cli', or simply an
unrelated chat — keeps its top-level row.

Each test builds a throwaway profile tree (default + researcher state
DBs plus research_jobs/) with the production schema, points the
server's DB discovery at it, and asserts on the consumers of the
lineage result: load_sessions() (top-level listing and its totals),
subagents_for() (the parent page's child rendering), and — for the
bounds and identity rules — the rendered page HTML and the feed's
serialized children, the two public surfaces a browser actually sees.
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import unittest.mock

import plugins.mission_control.server as server


# The production schema, from core: the listing is served by the core
# projection (list_sessions_rich), which also reads system_prompts and
# the shared session_turn_leases shape — the verbatim table copies
# below predated that and made every fixture DB unlistable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from hermes_state_common import SCHEMA_SQL  # noqa: E402


class LineageFixture(unittest.TestCase):
    """One throwaway home: default + researcher DBs and research_jobs/.

    Timestamps sit near the real current time so the same fixture
    satisfies both build_lineage(<test now>) and the subagents_for()
    path (which internally uses the real clock).
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="mc-lineage-")
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        self.now = time.time() - 1800  # fixture "now", inside the window
        self.base = self.now - 600     # job created_at, 10 min before now

        self.default_db = os.path.join(self.root, "state.db")
        self.researcher_db = os.path.join(self.root, "profiles",
                                          "researcher", "state.db")
        for path in (self.default_db, self.researcher_db):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            con = sqlite3.connect(path)
            try:
                con.executescript(SCHEMA_SQL)
                con.commit()
            finally:
                con.close()

        self._patchers = [
            unittest.mock.patch.object(server, "MAIN_DB", self.default_db),
            unittest.mock.patch.object(
                server, "PROFILE_GLOB",
                os.path.join(self.root, "profiles", "*", "state.db")),
        ]
        for p in self._patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patchers])
        self.addCleanup(self._reset_lineage)
        self._reset_lineage()

    def _reset_lineage(self):
        """Forget the shared snapshot so each scenario rebuilds it."""
        server._lineage_cache["index"] = None
        server._lineage_cache["at"] = 0.0

    # ---- fixture writers -------------------------------------------

    def add_session(self, db, sid, source="cli", title=None,
                    started=None, last=None, ended=None,
                    parent_session_id=None):
        con = sqlite3.connect(db)
        try:
            con.execute(
                "INSERT INTO sessions (id, source, title, started_at,"
                " last_activity_at, ended_at, end_reason,"
                " parent_session_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, source, title, started, last, ended,
                 "cli_close" if ended is not None else None,
                 parent_session_id))
            con.commit()
        finally:
            con.close()

    def add_message(self, db, sid, role, content, at):
        con = sqlite3.connect(db)
        try:
            con.execute(
                "INSERT INTO messages (session_id, role, content,"
                " timestamp) VALUES (?, ?, ?, ?)",
                (sid, role, content, at))
            con.commit()
        finally:
            con.close()

    def add_job(self, job_id, origin_sid, prompts, created=None,
                completed=None, worker="researcher"):
        """One research_jobs/<job_id>/ tree whose origin is this home."""
        created = self.base if created is None else created
        completed = (self.base + 300) if completed is None else completed
        jdir = os.path.join(self.root, "research_jobs", job_id)
        os.makedirs(os.path.join(jdir, "prompts"), exist_ok=True)
        request = {
            "job_id": job_id,
            "brief": "test brief",
            "research_questions": ["q"],
            "worker_profile": worker,
            "created_at": created,
            "origin": {
                "session_id": origin_sid,
                "task_id": origin_sid,
                "hermes_home": self.root,
            },
        }
        status = {
            "job_id": job_id,
            "state": "completed",
            "created_at": created,
            "updated_at": completed,
            "completed_at": completed,
        }
        with open(os.path.join(jdir, "request.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(request, fh)
        with open(os.path.join(jdir, "status.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(status, fh)
        for name, text in prompts.items():
            with open(os.path.join(jdir, "prompts", name), "w",
                      encoding="utf-8") as fh:
                fh.write(text)

    def add_profile_db(self, name):
        """One more worker-profile DB under the fixture home."""
        path = os.path.join(self.root, "profiles", name, "state.db")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        con = sqlite3.connect(path)
        try:
            con.executescript(SCHEMA_SQL)
            con.commit()
        finally:
            con.close()
        return path

    # ---- workers and parents ---------------------------------------

    def add_worker(self, sid, title, prompt, started=None, db=None,
                   source="research-worker"):
        """A worker-profile run dispatched with exactly `prompt`.

        source defaults to the deep_research runner's worker tag: the
        explicit non-human marking a job match requires before it may
        claim a session. Tests pass source="cli" (or "") to build the
        human-facing false match that must keep its inbox row."""
        started = (self.base + 5) if started is None else started
        self.add_session(db or self.researcher_db, sid, source=source,
                         title=title, started=started,
                         last=started + 60, ended=started + 60)
        self.add_message(db or self.researcher_db, sid, "user", prompt,
                         started)
        self.add_message(db or self.researcher_db, sid, "assistant",
                         "done", started + 60)

    def add_parent(self, sid, title="Research dispatch", started=None):
        started = (self.base - 120) if started is None else started
        self.add_session(self.default_db, sid, source="discord",
                         title=title, started=started,
                         last=started + 400, ended=started + 400)

    # ---- assertions ------------------------------------------------

    def top_level(self):
        """(rows, keys) of the inbox listing — the totals consumer."""
        self._reset_lineage()
        rows, _notes = server.load_sessions(self.now)
        return rows, {(r["profile"], r["id"]) for r in rows}

    def children_of(self, profile, sid):
        """The parent page's Sub-agents rendering source."""
        self._reset_lineage()
        con = sqlite3.connect("file:%s?mode=ro" % self.default_db,
                              uri=True)
        try:
            return server.subagents_for(con, profile, sid)
        finally:
            con.close()

    def _dbs(self):
        return {name: db_path for db_path, name in server.discover_dbs()}

    def parent_page_html(self, profile, sid):
        """The public /s/<profile>/<sid> page HTML."""
        self._reset_lineage()
        chat = server.load_chat(profile, sid, self._dbs())
        return server.render_chat(chat)

    def feed_children(self, profile, sid):
        """The feed poll's serialized children, in rendered order."""
        self._reset_lineage()
        feed = server.load_feed(profile, sid, self._dbs(), 0)
        return feed["subagents"]


class TestPromptShapes(LineageFixture):
    """The matching itself: what links and what must never link."""

    def test_oversize_synthesis_prompt_links_under_origin(self):
        """The bug shape: a >8192-char synthesis prompt must link.

        With the old 8192-char cap the prompt file was skipped outright,
        so the equality test could never even run and the worker leaked
        into the top level. 30000 chars sits well past the old cap and
        under the current one, exactly like the live synthesis and
        correction prompts (14.7k-54.7k chars).
        """
        prompt = "Synthesis of findings. " + ("evidence " * 3750)
        self.assertGreater(len(prompt), 8192)
        self.add_parent("20260902_100000_origin01")
        self.add_job("rj_test0001", "20260902_100000_origin01",
                     {"synthesis.md": prompt,
                      "lane_0.md": "Lane zero question?"})
        self.add_worker("20260902_100100_worker01",
                        "Write the synthesis", prompt,
                        started=self.base + 10)

        _rows, keys = self.top_level()
        self.assertNotIn(("researcher", "20260902_100100_worker01"),
                         keys)
        kids = self.children_of("default", "20260902_100000_origin01")
        self.assertIn("20260902_100100_worker01",
                      [k["id"] for k in kids])
        kid = next(k for k in kids if k["id"] == "20260902_100100_worker01")
        self.assertEqual(kid["profile"], "researcher")

    def test_ordinary_lane_prompt_links_under_origin(self):
        """The everyday case since the feature exists: a small lane
        prompt byte-equal to prompts/lane_0.md inside the window."""
        prompt = ("Rebuild the cultivar rows and cite every claim "
                  "from a readable full page.")
        self.add_parent("20260902_100000_origin02")
        self.add_job("rj_test0002", "20260902_100000_origin02",
                     {"lane_0.md": prompt})
        self.add_worker("20260902_100100_worker02",
                        "Rebuild rows", prompt)

        _rows, keys = self.top_level()
        self.assertNotIn(("researcher", "20260902_100100_worker02"),
                         keys)
        kids = self.children_of("default", "20260902_100000_origin02")
        self.assertIn("20260902_100100_worker02", [k["id"] for k in kids])

    def test_prompt_past_cap_is_skipped_not_prefix_matched(self):
        """Fail closed at the cap: a file past LINEAGE_PROMPT_MAX_CHARS
        is skipped, and the equally-capped DB read must never be able
        to satisfy it as a prefix match. The worker stays top-level."""
        prompt = "x" * (server.LINEAGE_PROMPT_MAX_CHARS + 5000)
        self.add_parent("20260902_100000_origin03")
        self.add_job("rj_test0003", "20260902_100000_origin03",
                     {"synthesis.md": prompt})
        self.add_worker("20260902_100100_worker03",
                        "Huge synthesis", prompt)

        _rows, keys = self.top_level()
        self.assertIn(("researcher", "20260902_100100_worker03"), keys)
        kids = self.children_of("default", "20260902_100000_origin03")
        self.assertEqual(kids, [])

    def test_exact_cap_prompt_not_prefix_matched_longer_worker(self):
        """The 65,536-character prefix collision: a job prompt sitting
        exactly at LINEAGE_PROMPT_MAX_CHARS must not equal the capped
        DB read of a longer worker first message that shares its
        prefix. The first row's original length comes back beside its
        text, an over-cap message is rejected before comparison, and
        the run stays top-level under no parent."""
        cap = server.LINEAGE_PROMPT_MAX_CHARS
        body = "cap-boundary lane body. "
        prompt = (body * (cap // len(body) + 1))[:cap]
        self.assertEqual(len(prompt), cap)
        message = prompt + " tail running past the cap, same prefix."
        self.assertGreater(len(message), cap)
        self.assertTrue(message.startswith(prompt))
        self.add_parent("20260902_100000_origin12")
        self.add_job("rj_test0012", "20260902_100000_origin12",
                     {"lane_0.md": prompt})
        self.add_worker("20260902_100100_worker12",
                        "Longer same-prefix run", message)

        _rows, keys = self.top_level()
        self.assertIn(("researcher", "20260902_100100_worker12"), keys)
        kids = self.children_of("default", "20260902_100000_origin12")
        self.assertEqual(kids, [])

    def test_exact_cap_prompt_and_message_pair_links(self):
        """The boundary from the other side: a job prompt exactly at
        the cap and a first user message of the very same bytes and
        length is a true match, not a prefix artifact — only messages
        longer than the cap are rejected."""
        cap = server.LINEAGE_PROMPT_MAX_CHARS
        body = "cap-boundary lane body. "
        prompt = (body * (cap // len(body) + 1))[:cap]
        self.assertEqual(len(prompt), cap)
        self.add_parent("20260902_100000_origin13")
        self.add_job("rj_test0013", "20260902_100000_origin13",
                     {"lane_0.md": prompt})
        self.add_worker("20260902_100100_worker13",
                        "Exactly capped run", prompt)

        _rows, keys = self.top_level()
        self.assertNotIn(("researcher", "20260902_100100_worker13"),
                         keys)
        kids = self.children_of("default", "20260902_100000_origin13")
        self.assertIn("20260902_100100_worker13", [k["id"] for k in kids])

    def test_wrapped_prompt_does_not_link(self):
        """No fuzzy matching: a job prompt that merely wraps the
        worker's first message (added header and footer) is different
        bytes, so it proves nothing and the run stays top-level."""
        inner = "Answer the research question with citations."
        wrapped = ("You are a research worker.\n\n%s\n\nReport back."
                   % inner)
        self.add_parent("20260902_100000_origin04")
        self.add_job("rj_test0004", "20260902_100000_origin04",
                     {"lane_0.md": wrapped})
        self.add_worker("20260902_100100_worker04",
                        "Wrapped lane", inner)

        _rows, keys = self.top_level()
        self.assertIn(("researcher", "20260902_100100_worker04"), keys)


class TestEvidenceRequirements(LineageFixture):
    """Every weaker evidence case keeps its top-level row."""

    def test_ambiguous_collision_stays_top_level(self):
        """Two jobs with the same prompt text and overlapping windows
        both claim the worker; collision rejection drops the link (it
        must not be guessed onto either parent) and the run stays a
        top-level chat."""
        prompt = "Shared retry prompt after a runner restart."
        self.add_parent("20260902_100000_origin05")
        self.add_parent("20260902_100000_origin06")
        self.add_job("rj_test0005", "20260902_100000_origin05",
                     {"lane_0.md": prompt}, created=self.base,
                     completed=self.base + 300)
        self.add_job("rj_test0006", "20260902_100000_origin06",
                     {"lane_0.md": prompt}, created=self.base + 30,
                     completed=self.base + 330)
        self.add_worker("20260902_100100_worker05",
                        "Ambiguous lane", prompt,
                        started=self.base + 60)  # inside both windows

        _rows, keys = self.top_level()
        self.assertIn(("researcher", "20260902_100100_worker05"), keys)
        for origin in ("20260902_100000_origin05",
                       "20260902_100000_origin06"):
            kids = self.children_of("default", origin)
            self.assertNotIn("20260902_100100_worker05",
                             [k["id"] for k in kids])

    def test_missing_origin_session_stays_top_level(self):
        """A job whose origin_session_id matches no session row in the
        owner DB proves nothing: the exact id is part of the evidence,
        so the worker keeps its top-level row."""
        prompt = "Lane for a parent that is not in the DB."
        self.add_job("rj_test0007", "20260902_100000_gone007",
                     {"lane_0.md": prompt})
        self.add_worker("20260902_100100_worker07",
                        "Orphan lane", prompt)

        _rows, keys = self.top_level()
        self.assertIn(("researcher", "20260902_100100_worker07"), keys)

    def test_out_of_window_start_stays_top_level(self):
        """The job's time bounds are evidence too: an exact prompt that
        started long after the job finished is a different run (prompt
        bytes do recur across jobs) and stays top-level."""
        prompt = "Recurring lane prompt that jobs reuse."
        self.add_parent("20260902_100000_origin08")
        self.add_job("rj_test0008", "20260902_100000_origin08",
                     {"lane_0.md": prompt},
                     created=self.base,
                     completed=self.base + 300)
        # started_at past hi = completed + LINEAGE_SKEW_SECONDS
        self.add_worker("20260902_100100_worker08",
                        "Late rerun", prompt,
                        started=self.base + 300
                        + server.LINEAGE_SKEW_SECONDS + 120)

        _rows, keys = self.top_level()
        self.assertIn(("researcher", "20260902_100100_worker08"), keys)
        kids = self.children_of("default", "20260902_100000_origin08")
        self.assertEqual(kids, [])

    def test_unrelated_researcher_cli_session_stays_top_level(self):
        """Profile alone is never evidence: a researcher-profile chat
        whose first message matches no job prompt keeps its inbox row —
        hiding it would be the very profile-only cut this app rejects."""
        self.add_parent("20260902_100000_origin09")
        self.add_job("rj_test0009", "20260902_100000_origin09",
                     {"lane_0.md": "The dispatched lane prompt."})
        self.add_worker("20260902_100100_worker09a",
                        "Dispatched lane", "The dispatched lane prompt.")
        self.add_worker("20260902_100100_worker09b",
                        "Someone's own chat",
                        "Just researching on my own, no job involved.")

        rows, keys = self.top_level()
        self.assertNotIn(("researcher", "20260902_100100_worker09a"),
                         keys)
        self.assertIn(("researcher", "20260902_100100_worker09b"), keys)

    def test_unrelated_default_cli_session_stays_top_level(self):
        """Nor is source='cli' evidence: an ordinary default-profile
        CLI chat is untouched by the whole lineage pass."""
        prompt = "Lane prompt also present as a job prompt."
        self.add_parent("20260902_100000_origin10")
        self.add_job("rj_test0010", "20260902_100000_origin10",
                     {"lane_0.md": prompt})
        self.add_session(self.default_db, "20260902_100100_cli10",
                         source="cli", title="Plain CLI chat",
                         started=self.base + 20, last=self.base + 90,
                         ended=self.base + 90)
        self.add_message(self.default_db, "20260902_100100_cli10",
                         "user", prompt, self.base + 20)
        self.add_message(self.default_db, "20260902_100100_cli10",
                         "assistant", "here you go", self.base + 90)

        _rows, keys = self.top_level()
        self.assertIn(("default", "20260902_100100_cli10"), keys)


class TestConsumers(LineageFixture):
    """Listing, totals and the parent page share one lineage result."""

    def test_linked_worker_absent_from_listing_and_totals(self):
        """The three consumers of one snapshot agree: the linked worker
        is gone from the listing rows, therefore out of the row count
        the sidebar total renders from, and present under its origin."""
        prompt = "The one and only dispatched prompt."
        self.add_parent("20260902_100000_origin11",
                        title="The dispatching chat")
        # An unrelated researcher chat that must stay counted.
        self.add_worker("20260902_100100_worker11a",
                        "Unrelated chat", "Unrelated first message.")
        self.add_job("rj_test0011", "20260902_100000_origin11",
                     {"lane_0.md": prompt})
        self.add_worker("20260902_100100_worker11b",
                        "Dispatched run", prompt)

        rows, keys = self.top_level()
        self.assertNotIn(("researcher", "20260902_100100_worker11b"),
                         keys)
        self.assertIn(("researcher", "20260902_100100_worker11a"), keys)
        # Totals render len(rows); the linked worker is not in it once.
        researcher_rows = [r for r in rows
                           if r["profile"] == "researcher"]
        self.assertEqual(
            [r["id"] for r in researcher_rows],
            ["20260902_100100_worker11a"])
        self.assertEqual(len(rows), len(keys))  # ids unique per row

        kids = self.children_of("default", "20260902_100000_origin11")
        self.assertEqual([k["id"] for k in kids],
                         ["20260902_100100_worker11b"])
        self.assertEqual(kids[0]["profile"], "researcher")
        self.assertEqual(kids[0]["label"], "Dispatched run")


class TestWorkerSourceGate(LineageFixture):
    """Prompt bytes plus a time window are inference, never proof."""

    def test_human_facing_cli_false_match_keeps_inbox_row(self):
        """The false positive the gate exists for: a researcher-profile
        chat a person typed at their own CLI whose first prompt and
        start time exactly match a job. It must stay an inbox row and
        never become a child, while its worker-tagged twin (same
        prompt, same minute) links — the source is the only differing
        evidence. Asserted on the public surfaces, not just the
        helper's sets."""
        prompt = "Prompt a human could coincidentally repeat verbatim."
        self.add_parent("20260902_100000_origin20")
        self.add_job("rj_test0020", "20260902_100000_origin20",
                     {"lane_0.md": prompt})
        self.add_worker("20260902_100100_worker20a",
                        "Human's own chat", prompt, source="cli")
        self.add_worker("20260902_100100_worker20b",
                        "Dispatched run", prompt)

        rows, keys = self.top_level()
        self.assertIn(("researcher", "20260902_100100_worker20a"), keys)
        self.assertNotIn(("researcher", "20260902_100100_worker20b"),
                         keys)
        kids = self.children_of("default", "20260902_100000_origin20")
        self.assertEqual([k["id"] for k in kids],
                         ["20260902_100100_worker20b"])

        # The page and the feed — what a browser sees — agree.
        page = self.parent_page_html("default",
                                     "20260902_100000_origin20")
        self.assertIn('href="/s/researcher/20260902_100100_worker20b"',
                      page)
        self.assertNotIn("20260902_100100_worker20a", page)
        feed = self.feed_children("default", "20260902_100000_origin20")
        self.assertEqual([c["id"] for c in feed],
                         ["20260902_100100_worker20b"])

    def test_blank_source_false_match_keeps_inbox_row(self):
        """A blank source is unknown, not worker: it fails open to the
        inbox rather than being claimed by inference."""
        prompt = "Prompt repeated by an untagged legacy run."
        self.add_parent("20260902_100000_origin21")
        self.add_job("rj_test0021", "20260902_100000_origin21",
                     {"lane_0.md": prompt})
        self.add_worker("20260902_100100_worker21",
                        "Legacy untagged run", prompt, source="")

        _rows, keys = self.top_level()
        self.assertIn(("researcher", "20260902_100100_worker21"), keys)
        kids = self.children_of("default", "20260902_100000_origin21")
        self.assertEqual(kids, [])


class TestMalformedMetadata(LineageFixture):
    """One bad artifact skips only itself; nothing ever raises."""

    def mutate_job_request(self, job_id, **fields):
        """Rewrite fields into one written job's request.json."""
        path = os.path.join(self.root, "research_jobs", job_id,
                            "request.json")
        with open(path, encoding="utf-8") as fh:
            req = json.load(fh)
        req.update(fields)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(req, fh)

    def test_worker_profile_list_skips_only_that_job(self):
        """worker_profile must be a non-empty string: a JSON list there
        must be skipped before any set-membership use, discarding only
        its own job's link while the sibling job's link stands."""
        good = "The valid sibling job's dispatched prompt."
        bad = "The malformed job's dispatched prompt."
        self.add_parent("20260902_100000_origin30")
        self.add_parent("20260902_100000_origin31")
        self.add_job("rj_test0030", "20260902_100000_origin30",
                     {"lane_0.md": bad})
        self.add_job("rj_test0031", "20260902_100000_origin31",
                     {"lane_0.md": good})
        self.mutate_job_request("rj_test0030",
                                worker_profile=["researcher", 7])
        self.add_worker("20260902_100100_worker30a",
                        "Malformed job's run", bad)
        self.add_worker("20260902_100100_worker30b",
                        "Valid job's run", good)

        _rows, keys = self.top_level()  # must not raise
        self.assertIn(("researcher", "20260902_100100_worker30a"), keys)
        self.assertNotIn(("researcher", "20260902_100100_worker30b"),
                         keys)
        self.assertEqual(
            self.children_of("default", "20260902_100000_origin30"), [])
        self.assertEqual(
            [k["id"] for k in
             self.children_of("default", "20260902_100000_origin31")],
            ["20260902_100100_worker30b"])

    def test_other_malformed_shapes_skip_only_their_job(self):
        """A string created_at, a non-dict origin, and an unparseable
        request.json each skip exactly their own job; the fourth,
        well-formed job beside them still links."""
        prompt = "Prompt only the valid job dispatched."
        self.add_parent("20260902_100000_origin32")
        self.add_parent("20260902_100000_origin33")
        self.add_parent("20260902_100000_origin34")
        self.add_parent("20260902_100000_origin35")
        self.add_job("rj_test0032", "20260902_100000_origin32",
                     {"lane_0.md": prompt}, created="yesterday")
        self.add_job("rj_test0033", "20260902_100000_origin33",
                     {"lane_0.md": prompt})
        self.mutate_job_request("rj_test0033", origin=None)
        self.add_job("rj_test0034", "20260902_100000_origin34",
                     {"lane_0.md": prompt})
        with open(os.path.join(self.root, "research_jobs", "rj_test0034",
                               "request.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{not json at all")
        self.add_job("rj_test0035", "20260902_100000_origin35",
                     {"lane_0.md": prompt})
        self.add_worker("20260902_100100_worker35",
                        "The valid job's run", prompt,
                        started=self.base + 10)

        _rows, keys = self.top_level()  # must not raise
        self.assertNotIn(("researcher", "20260902_100100_worker35"),
                         keys)
        self.assertEqual(
            [k["id"] for k in
             self.children_of("default", "20260902_100000_origin35")],
            ["20260902_100100_worker35"])
        for origin in ("20260902_100000_origin32",
                       "20260902_100000_origin33",
                       "20260902_100000_origin34"):
            self.assertEqual(self.children_of("default", origin), [])

    def test_overlapping_jobs_each_link_their_own_worker(self):
        """Two jobs whose windows overlap but whose prompts differ each
        claim exactly their own worker — overlap alone is never
        ambiguity (that needs one worker claimed by two parents)."""
        pa = "Prompt dispatched by the earlier overlapping job."
        pb = "Prompt dispatched by the later overlapping job."
        self.add_parent("20260902_100000_origin36")
        self.add_parent("20260902_100000_origin37")
        self.add_job("rj_test0036", "20260902_100000_origin36",
                     {"lane_0.md": pa}, created=self.base,
                     completed=self.base + 300)
        self.add_job("rj_test0037", "20260902_100000_origin37",
                     {"lane_0.md": pb}, created=self.base + 30,
                     completed=self.base + 330)
        self.add_worker("20260902_100100_worker36a",
                        "Earlier job's run", pa, started=self.base + 60)
        self.add_worker("20260902_100100_worker37b",
                        "Later job's run", pb, started=self.base + 90)

        _rows, keys = self.top_level()
        self.assertNotIn(("researcher", "20260902_100100_worker36a"),
                         keys)
        self.assertNotIn(("researcher", "20260902_100100_worker37b"),
                         keys)
        self.assertEqual(
            [k["id"] for k in
             self.children_of("default", "20260902_100000_origin36")],
            ["20260902_100100_worker36a"])
        self.assertEqual(
            [k["id"] for k in
             self.children_of("default", "20260902_100000_origin37")],
            ["20260902_100100_worker37b"])


class TestChildIdentityAndBounds(LineageFixture):
    """(profile, session id) identity and the merged 50-child bound."""

    def test_duplicate_ids_across_profiles_are_distinct_children(self):
        """The same session id in two worker profiles is two children:
        each links to its own parent, each leaves the inbox by its own
        (profile, id) key, and the page/feed links are
        profile-qualified."""
        shared = "20260902_100100_dup60"
        p_researcher = "Question dispatched to the researcher profile."
        p_analyst = "Question dispatched to the analyst profile."
        analyst_db = self.add_profile_db("analyst")
        self.add_parent("20260902_100000_origin60a")
        self.add_parent("20260902_100000_origin60b")
        self.add_job("rj_test0060a", "20260902_100000_origin60a",
                     {"lane_0.md": p_researcher}, worker="researcher")
        self.add_job("rj_test0060b", "20260902_100000_origin60b",
                     {"lane_0.md": p_analyst}, worker="analyst")
        self.add_worker(shared, "Researcher run", p_researcher)
        self.add_worker(shared, "Analyst run", p_analyst, db=analyst_db)

        _rows, keys = self.top_level()
        self.assertNotIn(("researcher", shared), keys)
        self.assertNotIn(("analyst", shared), keys)
        kids_a = self.children_of("default", "20260902_100000_origin60a")
        self.assertEqual([(k["profile"], k["id"]) for k in kids_a],
                         [("researcher", shared)])
        kids_b = self.children_of("default", "20260902_100000_origin60b")
        self.assertEqual([(k["profile"], k["id"]) for k in kids_b],
                         [("analyst", shared)])

        page = self.parent_page_html("default",
                                     "20260902_100000_origin60a")
        self.assertIn('href="/s/researcher/%s"' % shared, page)
        self.assertNotIn('href="/s/analyst/%s"' % shared, page)
        feed = self.feed_children("default",
                                  "20260902_100000_origin60a")
        self.assertEqual([c["profile"] for c in feed], ["researcher"])

    def test_fifty_bound_applies_after_merging_profiles(self):
        """30 same-profile subagents plus 25 linked researcher workers
        is 55 children of one parent: the 50-child bound is applied
        after the merge — over the combined list, ordered oldest-first
        — on both the page and the feed, keeping the 50 earliest
        starts and cutting the 5 latest."""
        parent = "20260902_100000_origin61"
        self.add_parent(parent)
        prompts = {"lane_%02d.md" % i: "Fifty-bound lane %02d prompt." % i
                   for i in range(25)}
        self.add_job("rj_test0061", parent, prompts)
        sids = []
        for i in range(55):
            sid = "20260902_100100_kid%02d" % i
            started = self.base + i  # one second apart: strict order
            if i < 30:
                self.add_session(self.default_db, sid, source="subagent",
                                 title="Subagent %02d" % i,
                                 started=started, last=started + 30,
                                 ended=started + 30,
                                 parent_session_id=parent)
            else:
                self.add_worker(sid, "Worker %02d" % (i - 30),
                                prompts["lane_%02d.md" % (i - 30)],
                                started=started)
            sids.append(sid)

        rows, keys = self.top_level()
        # Every linked worker leaves the inbox — including the five the
        # display bound later cuts; the parent and nothing else moves.
        for i in range(30, 55):
            self.assertNotIn(("researcher", sids[i]), keys)
        self.assertIn(("default", parent), keys)

        kids = self.children_of("default", parent)
        self.assertEqual(len(kids), server.SUBAGENT_MAX_CHILDREN)
        self.assertEqual([k["id"] for k in kids], sids[:50])
        self.assertEqual([k["id"] for k in kids][-1], sids[49])

        # The public page: a bounded section with the count badge and
        # profile-qualified links, oldest first, newest five absent.
        page = self.parent_page_html("default", parent)
        self.assertIn(
            '<span class="sa-count">%d</span>'
            % server.SUBAGENT_MAX_CHILDREN, page)
        self.assertEqual(page.count('class="sa-item"'),
                         server.SUBAGENT_MAX_CHILDREN)
        self.assertIn('href="/s/default/%s"' % sids[0], page)
        self.assertIn('href="/s/researcher/%s"' % sids[49], page)
        for i in range(50, 55):
            self.assertNotIn(sids[i], page)

        # The feed ships the same bounded ordered list.
        feed = self.feed_children("default", parent)
        self.assertEqual([c["id"] for c in feed], sids[:50])

    def test_equal_starts_order_by_id_then_profile(self):
        """The explicit tie-breaker: children sharing a started_at sort
        by session id, then profile. One parent's four same-start
        children — researcher workers "a..." and "c...", plus the same
        id "b..." in both worker profiles — render in exactly that
        order on the helper, the feed and the page, on every rebuild."""
        parent = "20260902_100000_origin62"
        self.add_parent(parent)
        tie = self.base + 5
        analyst_db = self.add_profile_db("analyst")
        self.add_job("rj_test0062a", parent,
                     {"lane_0.md": "Tie-breaker researcher prompt."},
                     worker="researcher")
        self.add_job("rj_test0062b", parent,
                     {"lane_0.md": "Tie-breaker analyst prompt."},
                     worker="analyst")
        self.add_worker("20260902_100100_aworker", "Researcher a",
                        "Tie-breaker researcher prompt.", started=tie)
        self.add_worker("20260902_100100_cworker", "Researcher c",
                        "Tie-breaker researcher prompt.", started=tie)
        self.add_worker("20260902_100100_bdup", "Analyst b",
                        "Tie-breaker analyst prompt.", started=tie,
                        db=analyst_db)
        self.add_worker("20260902_100100_bdup", "Researcher b",
                        "Tie-breaker researcher prompt.", started=tie)
        order = [("researcher", "20260902_100100_aworker"),
                 ("analyst", "20260902_100100_bdup"),
                 ("researcher", "20260902_100100_bdup"),
                 ("researcher", "20260902_100100_cworker")]

        for _ in range(2):  # stable across rebuilds
            self.assertEqual(
                [(k["profile"], k["id"])
                 for k in self.children_of("default", parent)], order)
        self.assertEqual(
            [(c["profile"], c["id"])
             for c in self.feed_children("default", parent)], order)
        page = self.parent_page_html("default", parent)
        positions = [page.index('href="/s/%s/%s"' % key)
                     for key in order]
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
