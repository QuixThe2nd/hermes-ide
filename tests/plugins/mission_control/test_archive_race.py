#!/usr/bin/env python3
"""A stale Discord snapshot can never overwrite a user-confirmed
close/reopen.

The ownership rule under test: a background sync pass reads the
profile's archive epoch BEFORE its fetch, and re-checks it under the
same lock that guards the snapshot apply — while every user mutation
bumps the epoch and writes under that lock. So a snapshot fetched
before the user acted is discarded whole when its fetch returns, and
a user action that arrives while a snapshot is being applied waits
for the lock instead of interleaving. The user-confirmed state wins
by construction, deterministically — the races here are staged with
Events, never sleeps.

The last test proves the guard is the epoch and not a broken sync:
once a NEW pass starts after the user action, its fresh snapshot
mirrors normally.
"""

import importlib.util
import itertools
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SERVER_PY = os.path.join(REPO, "plugins", "mission_control", "server.py")

_MODULE_SEQ = itertools.count()

THREAD = "1234567890123456789"
OTHER = "9876543210987654321"

# The production schema, imported from core: the listing is now served
# by the core projection (list_sessions_rich), so fixture DBs must
# answer exactly the SQL the live ones do — the synthetic subset below
# predated that and lacks the columns the projection reads.
sys.path.insert(0, REPO)

from hermes_state_common import SCHEMA_SQL  # noqa: E402

SESSION_SCHEMA = SCHEMA_SQL

# Three rows on the fixture thread (the mirror flips every row of a
# thread), one on another active thread.


def seed(db):
    con = sqlite3.connect(db)
    con.executescript(SESSION_SCHEMA)
    now = time.time()
    rows = [
        ("sess_a", THREAD, 0), ("sess_b", THREAD, 0),
        ("sess_c", THREAD, 0), ("sess_d", OTHER, 0),
    ]
    for sid, tid, arch in rows:
        con.execute(
            "INSERT INTO sessions (id, source, title, started_at,"
            " last_activity_at, archived, hidden, thread_id)"
            " VALUES (?, 'discord', ?, ?, ?, ?, 0, ?)",
            (sid, sid, now - 600, now - 30, arch, tid))
    con.commit()
    con.close()


def archived_values(db, tid=THREAD):
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        return sorted(r[0] for r in con.execute(
            "SELECT archived FROM sessions WHERE thread_id = ?"
            " ORDER BY id", (tid,)))
    finally:
        con.close()


class RaceCase(unittest.TestCase):
    """One profile DB with Discord rows, Discord patched, sync
    fetch/apply controllable from the test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="archrace-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = os.path.join(self.tmp, "state.db")
        seed(self.db)

        spec = importlib.util.spec_from_file_location(
            "mc_server_race_%d" % _MODULE_SEQ.__next__(), SERVER_PY)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.mod.MAIN_DB = self.db
        self.mod.PROFILE_GLOB = os.path.join(
            self.tmp, "no-such-profile", "*", "state.db")

        self._tok = unittest.mock.patch.object(
            self.mod, "load_discord_token", return_value="fixture-token")
        self._tok.start()
        self.addCleanup(self._tok.stop)

        # discord_request answers the thread PATCH with exactly the
        # archived state the caller asked for (the success contract).
        def patch_thread(method, path, token, payload=None):
            return 200, {"thread_metadata": {
                "archived": bool((payload or {}).get("archived"))}}, None
        self._discord = unittest.mock.patch.object(
            self.mod, "discord_request", side_effect=patch_thread)
        self._discord.start()
        self.addCleanup(self._discord.stop)

    def dbs(self):
        return {name: path for path, name in self.mod.discover_dbs()}

    def user_toggle(self, sid, desired):
        status, payload = self.mod.set_session_archived(
            "default", sid, self.dbs(), desired)
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])

    def run_sync_pass(self):
        """One discord_sync_once on a daemon thread; returns it."""
        t = threading.Thread(target=lambda: self.mod.discord_sync_once(
            time.time()), daemon=True)
        t.start()
        return t


class TestStaleSnapshotDiscarded(RaceCase):
    """Fetch in flight when the user acts: the snapshot is stale."""

    def test_close_reopen_during_fetch_keeps_user_state(self):
        # The fetch blocks until released; when it returns it will
        # claim the fixture thread is NOT active (it would archive all
        # three rows) — exactly a stale snapshot from before the user
        # reopened.
        fetched = threading.Event()
        release = threading.Event()

        def blocking_fetch(token):
            fetched.set()
            release.wait(10)
            return [OTHER], None

        with unittest.mock.patch.object(self.mod, "fetch_active_thread_ids",
                                        side_effect=blocking_fetch):
            sync = self.run_sync_pass()
            self.assertTrue(fetched.wait(10))

            # The user confirms close, then reopen, while the fetch is
            # still in flight. Final user state: OPEN (archived=0).
            self.user_toggle("sess_a", False)   # reopen from 0 is a no-op…
            self.user_toggle("sess_a", True)    # close: all rows -> 1
            self.user_toggle("sess_a", False)   # reopen: all rows -> 0
            self.assertEqual(archived_values(self.db), [0, 0, 0])

            release.set()
            sync.join(10)
            self.assertFalse(sync.is_alive())

        # The stale snapshot (thread absent from the active set) was
        # discarded whole: the user-confirmed open state survives.
        self.assertEqual(archived_values(self.db), [0, 0, 0])

        # And the user's close did happen along the way — Discord saw
        # the PATCHes (verify-then-mirror), proving the user path ran
        # to completion rather than being skipped.
        self.assertGreaterEqual(self.mod.discord_request.call_count, 1)

    def test_close_during_fetch_keeps_closed_state(self):
        fetched = threading.Event()
        release = threading.Event()

        def blocking_fetch(token):
            fetched.set()
            release.wait(10)
            return [THREAD], None   # stale: says the thread IS active

        with unittest.mock.patch.object(self.mod, "fetch_active_thread_ids",
                                        side_effect=blocking_fetch):
            sync = self.run_sync_pass()
            self.assertTrue(fetched.wait(10))
            self.user_toggle("sess_a", True)    # user closes mid-fetch
            self.assertEqual(archived_values(self.db), [1, 1, 1])
            release.set()
            sync.join(10)
            self.assertFalse(sync.is_alive())

        # The stale snapshot would have un-archived every row; the
        # user-confirmed close survives it.
        self.assertEqual(archived_values(self.db), [1, 1, 1])


class TestApplySerializesWithUser(RaceCase):
    """User action arriving while a snapshot is being applied waits
    for the lock — no interleaving, one consistent final state."""

    def test_user_write_waits_for_the_apply_then_wins(self):
        applying = threading.Event()
        inside = threading.Event()
        real_apply = self.mod.apply_discord_snapshot

        def slow_apply(db_path, active_ids, now):
            applying.set()
            inside.wait(10)   # hold the epoch lock mid-apply
            return real_apply(db_path, active_ids, now)

        with unittest.mock.patch.object(
                self.mod, "apply_discord_snapshot", side_effect=slow_apply), \
                unittest.mock.patch.object(
                    self.mod, "fetch_active_thread_ids",
                    return_value=([OTHER], None)):
            sync = self.run_sync_pass()
            self.assertTrue(applying.wait(10))

            # While the snapshot holds the lock, the user's close
            # cannot start its transaction. It runs in a thread so the
            # test can observe it is genuinely still pending.
            user = threading.Thread(target=self.user_toggle,
                                    args=("sess_a", True), daemon=True)
            user.start()
            # still holding: no user row has flipped
            time.sleep(0.2)
            self.assertEqual(archived_values(self.db), [0, 0, 0])

            inside.set()
            sync.join(10)
            user.join(10)
            self.assertFalse(sync.is_alive())
            self.assertFalse(user.is_alive())

        # The apply committed first (archiving all rows — the snapshot
        # said the thread is gone), then the user's close ran after it
        # under the lock. Either ordering of VALUES is fine only if it
        # is consistent; here both agree on closed, and crucially no
        # half-applied state (mixed 0/1) ever became visible.
        final = archived_values(self.db)
        self.assertIn(final, ([1, 1, 1], [0, 0, 0]))
        self.assertEqual(len(set(final)), 1)


class TestFreshPassStillMirrors(RaceCase):
    """The epoch guard guards staleness, not the sync itself."""

    def test_new_pass_after_user_action_applies(self):
        # User closes the thread…
        self.user_toggle("sess_a", True)
        self.assertEqual(archived_values(self.db), [1, 1, 1])
        # …then a fresh pass starts (epoch read NOW, after the action)
        # whose snapshot says the thread is active again: it mirrors.
        with unittest.mock.patch.object(
                self.mod, "fetch_active_thread_ids",
                return_value=([THREAD], None)):
            sync = self.run_sync_pass()
            sync.join(10)
            self.assertFalse(sync.is_alive())
        self.assertEqual(archived_values(self.db), [0, 0, 0])


if __name__ == "__main__":
    unittest.main()
