"""Shared fixtures: an in-memory Discord REST guild for home_server tests.

Follows the ``http_fn`` DI style of tests/plugins/quota_channels — no respx, no
real network. Every mutating call is counted so idempotency can be asserted by
call counts rather than snapshots.

Dummy credentials are deliberately far shorter than a real token and never a
valid one; no fixture contains a real token or webhook URL.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

from plugins.home_server.core import DISCORD_TOKEN_ENV_KEY

GUILD = "900000000000000001"

# Short, obviously-fake values — well under any real credential length.
BOT_TOKEN = "tok" + "en-fake"

# Discord channel types used by the template.
TYPE_TEXT = 0
TYPE_VOICE = 2
TYPE_CATEGORY = 4


class FakeDiscord:
    """Callable transport implementing the Discord routes home_server uses."""

    def __init__(self, existing=None):
        self.channels = {}
        self.messages = {}
        self.webhooks = {}
        self.next_id = 5000
        self.mutations = []  # (verb, path) for every non-GET request
        for chan in existing or []:
            self.add_channel(**chan)

    def add_channel(self, *, id, name, type, parent_id=None, position=0):
        self.channels[str(id)] = {
            "id": str(id),
            "name": name,
            "type": type,
            "parent_id": parent_id,
            "position": position,
        }
        return self.channels[str(id)]

    def count(self, verb, prefix=""):
        return sum(1 for v, p in self.mutations if v == verb and p.startswith(prefix))

    # -- transport ----------------------------------------------------------

    def __call__(self, req, timeout=25.0):
        method = (req.method or "GET").upper()
        parsed = urllib.parse.urlsplit(req.full_url)
        path = parsed.path
        if path.startswith("/api/v10"):
            path = path[len("/api/v10"):]
        body = json.loads(req.data.decode()) if req.data else {}

        if method == "GET" and path == f"/guilds/{GUILD}/channels":
            return 200, json.dumps(list(self.channels.values())).encode()

        if method == "POST" and path == f"/guilds/{GUILD}/channels":
            self.mutations.append((method, path))
            self.next_id += 1
            chan = self.add_channel(
                id=self.next_id,
                name=body["name"],
                type=body["type"],
                parent_id=body.get("parent_id"),
            )
            return 201, json.dumps(chan).encode()

        parts = path.strip("/").split("/")

        if method == "GET" and len(parts) == 2 and parts[0] == "channels":
            chan = self.channels.get(parts[1])
            if chan is None:
                return 404, b'{"message": "Unknown Channel"}'
            return 200, json.dumps(chan).encode()

        if method == "POST" and len(parts) == 3 and parts[2] == "messages":
            self.mutations.append((method, path))
            if parts[1] not in self.channels:
                return 404, b'{"message": "Unknown Channel"}'
            self.next_id += 1
            self.messages[self.next_id] = {
                "id": self.next_id,
                "channel_id": parts[1],
                "embeds": body.get("embeds", []),
            }
            return 200, json.dumps({"id": self.next_id}).encode()

        if method == "POST" and len(parts) == 3 and parts[2] == "webhooks":
            self.mutations.append((method, path))
            if parts[1] not in self.channels:
                return 404, b'{"message": "Unknown Channel"}'
            self.next_id += 1
            # Fake, non-routable URL shape.
            self.webhooks[self.next_id] = {
                "id": self.next_id,
                "channel_id": parts[1],
                "url": f"wh-{self.next_id}",
            }
            return 201, json.dumps(self.webhooks[self.next_id]).encode()

        if method == "PATCH" and len(parts) == 2 and parts[0] == "channels":
            self.mutations.append((method, path))
            chan = self.channels.get(parts[1])
            if chan is None:
                return 404, b'{"message": "Unknown Channel"}'
            chan["name"] = body.get("name", chan["name"])
            return 200, json.dumps(chan).encode()

        return 404, json.dumps({"message": f"unrouted {method} {path}"}).encode()


@pytest.fixture
def hermes(tmp_path, monkeypatch):
    """A temp HERMES_HOME, feature enabled, with a discord secret."""
    home = tmp_path / "hermes"
    (home / "secrets").mkdir(parents=True)
    (home / "secrets" / "discord.env").write_text(
        f"{DISCORD_TOKEN_ENV_KEY}={BOT_TOKEN}\n", encoding="utf-8"
    )
    (home / "config.yaml").write_text(
        "model:\n"
        "  default: keep-me\n"
        "discord_home_server:\n"
        f"  guild_id: \"{GUILD}\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def guild():
    return GUILD


@pytest.fixture
def make_discord():
    """Factory so tests can pre-seed an existing guild layout."""
    return FakeDiscord


@pytest.fixture
def write_config(hermes):
    def _write(section: dict, *, platforms: dict | None = None) -> None:
        import yaml

        path = hermes / "config.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw.setdefault("discord_home_server", {}).update(section)
        if platforms is not None:
            raw["platforms"] = platforms
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    return _write


@pytest.fixture
def read_config(hermes):
    def _read() -> dict:
        import yaml

        return yaml.safe_load((hermes / "config.yaml").read_text(encoding="utf-8")) or {}

    return _read


@pytest.fixture
def state(hermes):
    def _state() -> dict:
        import json

        return json.loads(
            (hermes / "home_server" / "state.json").read_text(encoding="utf-8")
        )

    return _state
