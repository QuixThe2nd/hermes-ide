"""Discord mention rendering: model text and auto-thread titles carry names.

Regression coverage for three reported behaviours: a mid-sentence mention of
the bot reached the model as a hole (the token was deleted), other users'
mentions arrived as raw ``<@id>`` tokens with no name, and auto-thread
placeholder titles deleted every mention. Rendering rules live in
``internal/mentionrender`` (cmd/mentionrender binary); the table in its
testdata/cases.json is shared with the Go table tests.
"""

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig

import plugins.platforms.discord.adapter as discord_platform  # noqa: E402
from plugins.platforms.discord import mention_render  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402

_MODULE_DIR = Path(__file__).resolve().parents[2] / "internal" / "mentionrender"
_TABLE = json.loads((_MODULE_DIR / "testdata" / "cases.json").read_text())["cases"]

BOT_ID = 222
BOT_NAME = "Hermes"


class FakeTextChannel:
    def __init__(self, channel_id=1):
        self.id = channel_id
        self.name = "general"
        self.guild = SimpleNamespace(name="Test Server")
        self.topic = None

    def history(self, *, limit, before, after=None, oldest_first=None):
        async def _iter():
            return
            yield
        return _iter()


def _bot_user():
    return SimpleNamespace(id=BOT_ID, display_name=BOT_NAME, name="hermes-bot", bot=True)


def make_message(*, channel, content, mentions=()):
    return SimpleNamespace(
        id=123, content=content, mentions=list(mentions), attachments=[],
        reference=None, created_at=datetime.now(timezone.utc), channel=channel,
        author=SimpleNamespace(id=42, display_name="Jezza", name="Jezza", bot=False),
        type=discord_platform.discord.MessageType.default,
        add_reaction=AsyncMock(),
    )


@pytest.fixture(scope="session")
def go_mentionrender(tmp_path_factory):
    """Build cmd/mentionrender once per session; skip when no Go toolchain."""
    if shutil.which("go") is None:
        pytest.skip("Go toolchain unavailable")
    binary = tmp_path_factory.mktemp("mentionrender") / "mentionrender"
    build = subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/mentionrender"],
        cwd=_MODULE_DIR, capture_output=True, text=True, timeout=300,
    )
    if build.returncode != 0:
        pytest.fail(f"go build cmd/mentionrender failed:\n{build.stderr}")
    return str(binary)


@pytest.fixture
def adapter(monkeypatch, go_mentionrender):
    monkeypatch.setattr(discord_platform.discord, "DMChannel", type("DMChannel", (), {}), raising=False)
    monkeypatch.setattr(discord_platform.discord, "Thread", type("Thread", (), {}), raising=False)
    monkeypatch.setattr(discord_platform.discord, "ForumChannel", type("ForumChannel", (), {}), raising=False)
    for var in (
        "DISCORD_REQUIRE_MENTION", "DISCORD_AUTO_THREAD", "DISCORD_AUTO_THREAD_REPLIES",
        "DISCORD_FREE_RESPONSE_CHANNELS", "DISCORD_NO_THREAD_CHANNELS",
        "DISCORD_ALLOWED_CHANNELS", "DISCORD_IGNORED_CHANNELS", "DISCORD_HISTORY_BACKFILL",
        "DISCORD_ALLOWED_USERS", "DISCORD_ALLOW_ALL_USERS",
        "GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS",
    ):
        monkeypatch.delenv(var, raising=False)
    # Pin the bridge to the session-built binary: each test's isolated
    # HERMES_HOME would otherwise re-resolve (and cold-build) per test.
    monkeypatch.setattr(mention_render._RESOLVER, "_binary", go_mentionrender)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._client = SimpleNamespace(user=_bot_user())
    adapter._text_batch_delay_seconds = 0  # disable batching
    adapter.handle_message = AsyncMock()
    return adapter


async def _dispatch(adapter, content, mentions):
    message = make_message(channel=FakeTextChannel(channel_id=321), content=content, mentions=mentions)
    assert await adapter._handle_message(message) is True
    adapter.handle_message.assert_awaited_once()
    return adapter.handle_message.await_args.args[0]


# ── model-facing text through the real _handle_message path ──────────────


@pytest.mark.asyncio
async def test_mid_sentence_self_and_other_mentions_render_names(adapter, monkeypatch):
    """The reported bug: the bot's mid-sentence mention left a hole; the
    other user's mention was a raw token with no name."""
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")
    event = await _dispatch(
        adapter,
        "then <@111> is my main agent, and <@222> is my public agent u talk to",
        mentions=[SimpleNamespace(id=111, display_name="Alice", name="alice", bot=False),
                  adapter._client.user],
    )
    assert event.text == (
        "then @Alice (<@111>) is my main agent, "
        "and @Hermes is my public agent u talk to"
    )


@pytest.mark.asyncio
async def test_leading_self_mention_command_still_detected(adapter, monkeypatch):
    """``<@bot> /restart`` is an address prefix: stripped, still a command."""
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")
    event = await _dispatch(adapter, "<@222> /restart", mentions=[adapter._client.user])
    assert event.text == "/restart"
    assert event.message_type == discord_platform.MessageType.COMMAND


@pytest.mark.asyncio
async def test_mention_only_ping_still_dropped(adapter, monkeypatch):
    """A bare ``<@bot>`` ping reaches no turn, exactly as before."""
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")
    message = make_message(
        channel=FakeTextChannel(channel_id=321), content="<@222>",
        mentions=[adapter._client.user],
    )
    assert await adapter._handle_message(message) is False
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_unresolvable_other_mention_stays_raw(adapter, monkeypatch):
    """No name from Discord → the raw token stays so the id is still visible."""
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")
    # 999 unresolved: no mention object, no guild member.
    event = await _dispatch(adapter, "<@222> ask <@999> about it", mentions=[adapter._client.user])
    assert event.text == "ask <@999> about it"


@pytest.mark.asyncio
async def test_other_mention_name_from_guild_member_cache(adapter, monkeypatch):
    """A token whose user is not in message.mentions resolves via the guild
    member cache — no REST call."""
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")
    guild = SimpleNamespace(
        id=777, name="Test Server",
        get_member=lambda uid: (
            SimpleNamespace(display_name="Cached Carol", name="carol") if uid == 333 else None
        ),
    )
    message = make_message(
        channel=FakeTextChannel(channel_id=321), content="<@222> ask <@333> about it",
        mentions=[adapter._client.user],
    )
    message.guild = guild
    assert await adapter._handle_message(message) is True
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "ask @Cached Carol (<@333>) about it"


# ── auto-thread placeholder titles ────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_thread_title_renders_mention_names(adapter):
    """The reported title bug: every mention was deleted, leaving holes."""
    created_names = []

    async def _create_thread(**kwargs):
        created_names.append(kwargs["name"])
        return SimpleNamespace(id=55555, name=kwargs["name"])

    message = make_message(
        channel=FakeTextChannel(channel_id=321),
        content="<@222> please introduce urselves to <@111>. he just setup hermes",
        mentions=[adapter._client.user,
                  SimpleNamespace(id=111, display_name="Alice", name="alice", bot=False)],
    )
    message.create_thread = _create_thread
    assert await adapter._handle_message(message) is True
    assert created_names == ["please introduce urselves to @Alice. he just setup hermes"]


def test_inbox_thread_legacy_title_still_works_without_names(adapter):
    """``_derive_auto_thread_name`` (content-only, no name data) keeps its
    historical delete-all-mentions rendering for the inbox path."""
    assert adapter._derive_auto_thread_name(
        "<@222> please introduce urselves to <@111>. he just setup hermes"
    ) == "please introduce urselves to . he just setup hermes"


# ── fail-open contract ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fail_open_without_helper_dispatches_historical_text(adapter, monkeypatch):
    """Helper unavailable → the message is still dispatched with today's text:
    the bot's own mention deleted, other mentions raw."""
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")
    monkeypatch.setattr(mention_render, "_RESOLVER", SimpleNamespace(get=lambda: None))
    event = await _dispatch(
        adapter,
        "then <@111> is my main agent, and <@222> is my public agent u talk to",
        mentions=[adapter._client.user],
    )
    assert event.text == "then <@111> is my main agent, and  is my public agent u talk to"


@pytest.mark.asyncio
@pytest.mark.parametrize("binary", [None, "/bin/echo"])  # missing; garbage output
async def test_bridge_returns_none_when_helper_fails(monkeypatch, binary):
    monkeypatch.setattr(mention_render, "_RESOLVER", SimpleNamespace(get=lambda: binary))
    assert await mention_render.render_body_async(
        "ask <@111> about it", bot_id="222", bot_name="Hermes", names={"111": "Alice"},
    ) is None


@pytest.mark.asyncio
async def test_helper_never_invoked_for_mention_free_text(monkeypatch):
    """Token-free content must not spawn the helper (or its first-use build)."""
    def _explode():
        raise AssertionError("resolver consulted for mention-free text")

    monkeypatch.setattr(mention_render, "_RESOLVER", SimpleNamespace(get=_explode))
    for render in (mention_render.render_body_async, mention_render.render_title_async):
        assert await render("plain text, no tokens", bot_id="222", bot_name="Hermes", names={}) is None


# ── shared rendering table through the real binary ───────────────────────


def test_shared_rendering_table(go_mentionrender):
    """Every row of the Go-shared table, through the real cmd/mentionrender."""
    for case in _TABLE:
        run = subprocess.run(
            [go_mentionrender],
            input=json.dumps({
                "content": case["content"], "bot_id": case["bot_id"],
                "bot_name": case["bot_name"], "names": case["names"], "mode": case["mode"],
            }),
            capture_output=True, text=True, timeout=10,
        )
        assert run.returncode == 0, (case["name"], run.stderr)
        assert json.loads(run.stdout)["text"] == case["want"], case["name"]
