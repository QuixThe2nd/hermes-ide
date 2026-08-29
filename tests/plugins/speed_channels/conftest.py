"""Shared fixtures: an in-memory transport for downloader APIs and Discord,
plus a scripted stand-in for the 1.1.1.1 ping.

Follows the ``http_fn`` DI style of tests/plugins/quota_channels — no respx, no
real network. The transport returns ``(status, body, headers)`` because the
qBittorrent session cookie has to ride on the login response's Set-Cookie.

Every credential here is short, fake, and unroutable; no fixture contains a
real token, API key, or webhook URL.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

from plugins.speed_channels.core import DISCORD_TOKEN_ENV_KEY

# Short, obviously-fake values — well under any real credential length.
FAKE_TOKEN_VALUE = "tok" + "en-fake"
QBIT = "http://qbit"
SAB = "http://sab"
SLSKD = "http://slskd"

# A fixed wall clock so "due" arithmetic and label timestamps are deterministic.
FIXED_NOW = 1_700_000_000.0


class FakeTransport:
    """Callable transport implementing the six endpoints run_tick touches."""

    def __init__(
        self,
        *,
        qbit_speed=2_500_000.0,
        qbit_downloading=5,
        sab_kbps=1100.0,
        sab_slots=12,
        slsk_dl=340 * 1024.0,
        slsk_up=96 * 1024.0,
        sab_status=200,
    ):
        self.qbit_speed = qbit_speed
        self.qbit_downloading = qbit_downloading
        self.sab_kbps = sab_kbps
        self.sab_slots = sab_slots
        self.sab_status = sab_status
        self.slsk_dl = slsk_dl
        self.slsk_up = slsk_up

        # Discord side: three voice channels + the category.
        self.channels = {
            "cq": {"id": "cq", "name": "qBittorrent"},
            "cs": {"id": "cs", "name": "SABnzbd"},
            "cl": {"id": "cl", "name": "slskd"},
            "cat": {"id": "cat", "name": "Speeds"},
        }
        self.patch_status = 200  # override to simulate 429/403 on PATCH

        self.requests = []  # (method, url, headers-lower, body)
        self.patched = []  # (channel_id, new_name)

    # -- helpers -------------------------------------------------------------

    @property
    def patch_calls(self):
        return len(self.patched)

    def _record(self, req):
        headers = {k.lower(): v for k, v in req.headers.items()}
        body = req.data.decode() if req.data else ""
        self.requests.append((req.get_method().upper(), req.full_url, headers, body))
        return headers

    # -- transport -----------------------------------------------------------

    def __call__(self, req, timeout=20.0):
        headers = self._record(req)
        parsed = urllib.parse.urlsplit(req.full_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path

        if base == QBIT:
            return self._qbit(req, path)
        if base == SAB:
            return self._sab(parsed)
        if base == SLSKD:
            return self._slskd(path)
        return self._discord(req, path)

    def _json(self, payload, status=200, headers=None):
        return status, json.dumps(payload).encode(), dict(headers or {})

    def _qbit(self, req, path):
        if path == "/api/v2/auth/login":
            # Bare text, exactly as the real server replies.
            return 200, b"Ok.", {"set-cookie": "SID=s1; path=/"}
        if path == "/api/v2/transfer/info":
            if "SID=s1" not in req.headers.get("Cookie", ""):
                return self._json({"message": "forbidden"}, status=403)
            return self._json({"dl_info_speed": self.qbit_speed, "up_info_speed": 100})
        if path == "/api/v2/torrents/info":
            return self._json([{"hash": f"t{i}"} for i in range(self.qbit_downloading)])
        return self._json({"message": "unrouted"}, status=404)

    def _sab(self, parsed):
        if parsed.query.startswith("mode=queue"):
            if self.sab_status != 200:
                return self.sab_status, b"server error", {}
            return self._json(
                {"queue": {"kbpersec": self.sab_kbps, "noofslots": self.sab_slots}}
            )
        return self._json({"message": "unrouted"}, status=404)

    def _slskd(self, path):
        if path.endswith("/downloads"):
            files = [
                {"state": "Succeeded", "averageSpeed": 9_999_999},  # excluded
                {"state": "InProgress", "averageSpeed": self.slsk_dl - 100},
                {"state": "Queued,Waiting", "averageSpeed": 100},
                {"state": "Cancelled", "averageSpeed": 9_999_999},  # excluded
            ]
        else:
            files = [{"state": "InProgress", "averageSpeed": self.slsk_up}]
        payload = [{"username": "peer", "directories": [{"files": files}]}]
        return self._json(payload)

    def _discord(self, req, path):
        if path.startswith("/api/v10"):
            path = path[len("/api/v10"):]
        parts = path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "channels":
            return self._json({"message": "unrouted"}, status=404)
        chan = self.channels.get(parts[1])
        if chan is None:
            return self._json({"message": "Unknown Channel"}, status=404)
        if req.get_method().upper() == "PATCH":
            if self.patch_status != 200:
                return self.patch_status, b'{"message": "rate limited"}', {}
            chan["name"] = json.loads(req.data)["name"]
            self.patched.append((chan["id"], chan["name"]))
        return self._json(chan)


@pytest.fixture
def hermes(tmp_path, monkeypatch):
    """A temp HERMES_HOME with the four short, fake secret env files."""
    home = tmp_path / "hermes"
    secrets = home / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "discord.env").write_text(
        f"{DISCORD_TOKEN_ENV_KEY}={FAKE_TOKEN_VALUE}\n", encoding="utf-8"
    )
    (secrets / "qbittorrent.env").write_text(
        f"QBIT_BASE_URL={QBIT}\nQBIT_USER=u\nQBIT_PASS=p\n", encoding="utf-8"
    )
    (secrets / "sabnzbd.env").write_text(
        f"SABNZBD_URL={SAB}\nSABNZBD_API_KEY=sabkey\n", encoding="utf-8"
    )
    (secrets / "slskd.env").write_text(
        f"SLSKD_URL={SLSKD}\nSLSKD_API_KEY=slskey\n", encoding="utf-8"
    )
    (home / "config.yaml").write_text(
        "speed_channels:\n"
        "  guild_id: \"900000000000000001\"\n"
        "  category_id: cat\n"
        "  channel_ids: {qbittorrent: cq, sabnzbd: cs, slskd: cl}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def config(hermes):
    from plugins.speed_channels.core import load_speed_config

    return load_speed_config()  # reads HERMES_HOME/config.yaml from `hermes`


class FakePing:
    """Stand-in for ``default_ping``: scripted RTT in ms, counts calls, and
    never touches the network or a real ping binary."""

    def __init__(self, value=33.3):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def ping():
    return FakePing()


@pytest.fixture
def now():
    return lambda: FIXED_NOW
