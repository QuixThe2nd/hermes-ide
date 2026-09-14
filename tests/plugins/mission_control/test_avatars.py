#!/usr/bin/env python3
"""The optional local avatar images: serving, layering and fallback.

Avatars are strictly-local, strictly-optional PNGs discovered by fixed
filename inside a home this server already trusts — the profile's own
mission-control/avatar.png and the main home's mission-control/user.png.
Everything here proves the three sides of that design:

- serving: GET /avatar/<profile> and GET /avatar-user answer exactly
  those files, PNG-typed and size-capped, and the pages layer the img
  over the letter badge (rail disc, conversation row, transcript
  bubbles, user panel, the optimistic twin's data-av-user hook);
- confinement: an unknown profile, a missing file, an oversized file
  and a symlink that resolves outside the configured home are all the
  themed 404 — never a bytes leak, never an escape;
- fallback: an install with no avatar files renders no img elements at
  all; every identity keeps its letter badge, and a broken image is
  hidden client-side (the is-broken capture-phase listener) so the
  letter always shows.

Run:  python3 tests/plugins/mission_control/test_avatars.py
(unittest, stdlib only)
"""

import importlib.util
import itertools
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

_MODULE_SEQ = itertools.count()

# Not a parsed image: the server never decodes avatar bytes, it only
# ships them — so any bytes stand in for a PNG.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"avatar-bytes" * 8

# The production schema, imported from core: the listing is now served
# by the core projection (list_sessions_rich), so fixture DBs must
# answer exactly the SQL the live ones do — the synthetic subset below
# predated that and lacks the columns the projection reads.
sys.path.insert(0, REPO)

from hermes_state_common import SCHEMA_SQL  # noqa: E402

SESSION_SCHEMA = SCHEMA_SQL


class AvatarCase(unittest.TestCase):
    """A synthetic home (main DB plus one named profile) behind a real
    HTTP server on an ephemeral port."""

    SID = "20260903_av_sess"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-avatar-")
        self.db = os.path.join(self.tmp, "state.db")
        self._make_db(self.db)
        self.profile_db = os.path.join(self.tmp, "profiles", "helper",
                                       "state.db")
        self._make_db(self.profile_db)
        for db, sid in ((self.db, self.SID),
                        (self.profile_db, self.SID)):
            con = sqlite3.connect(db)
            now = time.time()
            con.execute(
                "INSERT INTO sessions (id, source, title, started_at,"
                " last_activity_at, archived, hidden)"
                " VALUES (?,?,?,?,?,0,0)", (sid, "cli", sid, now, now))
            con.execute(
                "INSERT INTO messages (session_id, role, content,"
                " timestamp) VALUES (?,?,?,?)",
                (sid, "user", "the question", now))
            con.execute(
                "INSERT INTO messages (session_id, role, content,"
                " timestamp) VALUES (?,?,?,?)",
                (sid, "assistant", "the answer", now))
            con.commit()
            con.close()

        spec = importlib.util.spec_from_file_location(
            "mc_server_avatar_%d" % _MODULE_SEQ.__next__(), SERVER_PY)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.mod.MAIN_DB = self.db
        self.mod.PROFILE_GLOB = os.path.join(self.tmp, "profiles", "*",
                                             "state.db")
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

    def _make_db(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        con = sqlite3.connect(path)
        con.executescript(SESSION_SCHEMA)
        con.commit()
        con.close()

    def write_avatar(self, home, filename, data=PNG_BYTES):
        """One avatar file under <home>/mission-control/<filename>."""
        d = os.path.join(home, "mission-control")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, filename)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    # ---- HTTP helpers ----------------------------------------------

    def get(self, path, raw=False, headers=False):
        """(status, body) — or (status, body, headers) on request."""
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                body = resp.read()
                out = (resp.status, body if raw
                       else body.decode("utf-8"))
                hdrs = dict(resp.headers)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            out = (exc.code, body if raw else body.decode("utf-8"))
            hdrs = dict(exc.headers)
        return out + (hdrs,) if headers else out

    def img_elements(self, page):
        """The rendered avatar img elements ('' when none)."""
        return re.findall(r'<img class="av-img"[^>]*>', page)


class TestAvatarServing(AvatarCase):

    def test_profile_avatar_served_and_layered(self):
        self.write_avatar(self.tmp, "avatar.png")
        status, body, headers = self.get("/avatar/default", raw=True,
                                         headers=True)
        self.assertEqual(status, 200)
        self.assertEqual(body, PNG_BYTES)
        self.assertEqual(headers.get("Content-Type"), "image/png")
        self.assertIn("max-age=3600", headers.get("Cache-Control", ""))

        # the identity's surfaces all layer the img over the letter
        # (the fixture has rows in two profiles; only the default
        # profile has an avatar, so every img names its URL)
        status, page = self.get("/")
        self.assertEqual(status, 200)
        imgs = self.img_elements(page)
        self.assertTrue(imgs)
        for img in imgs:
            self.assertIn('src="/avatar/default"', img)
        # the letter badge is still there underneath
        self.assertIn('title="profile: Hermes"', page)

        status, page = self.get("/s/default/" + self.SID)
        self.assertEqual(status, 200)
        imgs = self.img_elements(page)
        self.assertTrue(imgs)
        for img in imgs:
            self.assertIn('src="/avatar/default"', img)
        # the 48px rail disc and the 40px transcript bubbles both
        # carry the layered image
        self.assertIn('width="48"', page)
        self.assertIn('width="40"', page)

    def test_named_profile_avatar_uses_its_own_home(self):
        home = os.path.join(self.tmp, "profiles", "helper")
        self.write_avatar(home, "avatar.png")
        status, body = self.get("/avatar/helper", raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(body, PNG_BYTES)
        # the row and rail for that profile carry its URL, the default
        # profile's row stays letter-only
        status, page = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn('src="/avatar/helper"', page)
        self.assertNotIn('src="/avatar/default"', page)

    def test_user_avatar_served_and_hooked_into_the_page(self):
        self.write_avatar(self.tmp, "user.png")
        status, body = self.get("/avatar-user", raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(body, PNG_BYTES)
        status, page = self.get("/s/default/" + self.SID)
        self.assertEqual(status, 200)
        # the client-side optimistic twin builds its img from this hook
        self.assertIn('data-av-user="/avatar-user"', page)
        # the sidebar's user panel layers the same image
        self.assertIn('src="/avatar-user"', page)
        status, page = self.get("/new")
        self.assertEqual(status, 200)
        self.assertIn('data-av-user="/avatar-user"', page)

    def test_avatar_img_escapes_its_attributes(self):
        esc = self.mod.avatar_img
        # empty URL is no element at all
        self.assertEqual(esc("", "Hermes", 40), "")
        # a hostile label can never break out of the attribute
        hostile = esc("/avatar/default", 'H"><script>', 40)
        self.assertIn("&quot;&gt;&lt;script&gt;", hostile)
        self.assertNotIn("<script>", hostile)


class TestAvatarConfinement(AvatarCase):

    def test_missing_avatar_is_404(self):
        for path in ("/avatar/default", "/avatar-user",
                     "/avatar/helper"):
            status, _body = self.get(path)
            self.assertEqual(status, 404, path)

    def test_unknown_profile_avatar_is_404(self):
        self.write_avatar(self.tmp, "avatar.png")
        status, _body = self.get("/avatar/stranger")
        self.assertEqual(status, 404)

    def test_oversized_avatar_is_refused(self):
        self.write_avatar(self.tmp, "avatar.png",
                          b"x" * (self.mod.AVATAR_MAX_BYTES + 1))
        status, _body = self.get("/avatar/default")
        self.assertEqual(status, 404)
        # and the URL helper never advertises it either
        self.assertEqual(self.mod.user_avatar_url(), "")
        self.assertEqual(self.mod.profile_avatar_url("default"), "")

    def test_avatar_symlink_escape_is_refused(self):
        outside = tempfile.mkdtemp(prefix="mc-avatar-out-")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        outside_file = os.path.join(outside, "secret.png")
        with open(outside_file, "wb") as fh:
            fh.write(PNG_BYTES)
        d = os.path.join(self.tmp, "mission-control")
        os.makedirs(d, exist_ok=True)
        os.symlink(outside_file, os.path.join(d, "avatar.png"))
        status, _body = self.get("/avatar/default")
        self.assertEqual(status, 404)
        status, page = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(self.img_elements(page), [])


class TestAvatarFallback(AvatarCase):

    def test_no_avatar_files_means_no_img_elements(self):
        status, page = self.get("/s/default/" + self.SID)
        self.assertEqual(status, 200)
        self.assertEqual(self.img_elements(page), [],
                         "no avatar files, yet an img rendered")
        # the client-side hook renders empty (no default asset)
        self.assertIn('data-av-user=""', page)
        status, page = self.get("/new")
        self.assertEqual(status, 200)
        self.assertEqual(self.img_elements(page), [])
        self.assertIn('data-av-user=""', page)
        status, page = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(self.img_elements(page), [])

    def test_broken_image_falls_back_to_the_letter_client_side(self):
        # The shipped client hides a failed avatar img on the capture
        # phase (img errors do not bubble) so the covered letter badge
        # reappears — the letter is markup, never a network dependency.
        status, page = self.get("/s/default/" + self.SID)
        self.assertEqual(status, 200)
        self.assertIn('t.classList.add("is-broken");', page)
        self.assertIn('.avatar .av-img.is-broken { display: none; }',
                      page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
