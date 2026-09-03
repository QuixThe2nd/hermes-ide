"""Routing parity for secondary multiplexed adapters (gateway/run.py).

Shape under test: a gateway multiplexes several profiles, each with its own
bot (secondary adapters in ``_profile_adapters``), and ``gateway.
profile_routes`` maps specific guilds/channels/threads to runtimes. The
primary (shared) adapter already routes — ``_make_default_profile_message_
handler`` consults ``profile_routes`` and pins ``_authorization_profile_home``
to the transport. The secondary handlers used to stamp EVERY source with the
owning profile before routing could run and scoped the whole turn to the
owner's home, so a target guild on a secondary bot always executed with the
owner's persona, tools, memory, and session namespace.

Parity contract locked here, shared by the message, busy-session,
platform-event, and adapter-auth seams:

* Transport/auth ownership stays with the owning secondary profile
  (``_authorization_profile_home``).
* Execution (profile stamp, runtime home, session/busy keys) follows the
  canonical route resolver; no match falls back exactly to the owner.
* An explicit route to an unserved profile fails closed
  (``ProfileRouteRejected`` semantics — dropped, never owner-executed).
* Replies stay on the ingress secondary adapter.
"""

from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.profile_routing import ProfileRoute
from gateway.run import GatewayRunner
from gateway.session import SessionSource

OWNER = "research"      # the secondary profile whose Discord bot received it
ROUTED = "reviews"      # the headless runtime the target guild routes to
GUILD = "9001"          # routed guild
OTHER_GUILD = "9002"    # unrouted guild
CHANNEL = "9101"        # channel with a more specific route
THREAD = "9201"         # thread with the most specific route


# ─── fixture: one multiplexed runner with routed + served profiles ─────────


@pytest.fixture
def routed_env(monkeypatch, tmp_path):
    """Sandboxed profile homes, served set, and recording runtime scopes."""
    home = tmp_path / "home"
    homes = {
        "default": home,
        OWNER: home / "profiles" / OWNER,
        ROUTED: home / "profiles" / ROUTED,
        "chanbot": home / "profiles" / "chanbot",
        "threadbot": home / "profiles" / "threadbot",
    }
    for profile_home in homes.values():
        profile_home.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: homes.get(name, home / "profiles" / name),
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists",
        lambda name: name in homes,
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name",
        lambda: "default",
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex, profile_allowlist=None: sorted(homes.items()),
    )

    async_homes: list[Path] = []
    sync_homes: list[Path] = []

    @asynccontextmanager
    async def fake_async_scope(profile_home):
        async_homes.append(Path(profile_home))
        yield

    @contextmanager
    def fake_sync_scope(profile_home, *args, **kwargs):
        sync_homes.append(Path(profile_home))
        yield

    monkeypatch.setattr(gateway_run, "_async_profile_runtime_scope", fake_async_scope)
    monkeypatch.setattr(gateway_run, "_profile_runtime_scope", fake_sync_scope)

    return SimpleNamespace(
        homes=homes, async_homes=async_homes, sync_homes=sync_homes
    )


def _routes():
    """Guild < channel < thread specificity, exactly as config.yaml orders."""
    return [
        ProfileRoute(
            name="thread", platform="discord", chat_id=CHANNEL,
            thread_id=THREAD, profile="threadbot",
        ),
        ProfileRoute(
            name="channel", platform="discord", guild_id=GUILD,
            chat_id=CHANNEL, profile="chanbot",
        ),
        ProfileRoute(
            name="guild", platform="discord", guild_id=GUILD, profile=ROUTED,
        ),
    ]


def _runner(routed_env, routes=None):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.profile_routes = _routes() if routes is None else routes
    return runner


def _source(**overrides):
    """A secondary-adapter Discord source, unstamped (pre-``build_source``)."""
    fields = dict(
        platform=Platform.DISCORD,
        chat_id=CHANNEL,
        chat_type="group",
        user_id="u1",
        user_name="u1",
        guild_id=GUILD,
    )
    fields.update(overrides)
    return SessionSource(**fields)


def _event(source):
    return SimpleNamespace(source=source, internal=False, text="hi")


def _spy_handle(runner, capture):
    async def _fake_handle(event):
        capture["profile"] = event.source.profile
        capture["auth_home"] = getattr(
            event.source, "_authorization_profile_home", None
        )
        return "ok"

    runner._handle_message = _fake_handle


def _install_real_scopes(monkeypatch):
    """Runtime scopes that install the REAL hermes-home override.

    The ``routed_env`` fixture replaces the scope context managers with
    record-only fakes; tests that assert on the ambient home a code path
    observes (auth reads vs. runtime reads) need scopes that actually enter
    the home, like production.
    """
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    @asynccontextmanager
    async def real_async_scope(profile_home):
        token = set_hermes_home_override(str(profile_home))
        try:
            yield
        finally:
            reset_hermes_home_override(token)

    @contextmanager
    def real_sync_scope(profile_home, *args, **kwargs):
        token = set_hermes_home_override(str(profile_home))
        try:
            yield
        finally:
            reset_hermes_home_override(token)

    monkeypatch.setattr(gateway_run, "_async_profile_runtime_scope", real_async_scope)
    monkeypatch.setattr(gateway_run, "_profile_runtime_scope", real_sync_scope)


# ─── message handler: route hit, fallback, and specificity ─────────────────


@pytest.mark.asyncio
async def test_target_guild_route_selects_routed_profile(routed_env):
    """A secondary bot's message from the routed guild runs the routed profile."""
    runner = _runner(routed_env)
    capture: dict = {}
    _spy_handle(runner, capture)

    result = await runner._make_profile_message_handler(OWNER)(
        _event(_source(chat_id="9199"))
    )

    assert result == "ok"
    assert capture["profile"] == ROUTED
    assert routed_env.async_homes == [routed_env.homes[ROUTED]]
    assert capture["auth_home"] == routed_env.homes[OWNER]


@pytest.mark.asyncio
async def test_other_guild_and_dm_select_owner(routed_env):
    """Unmatched guild and DM traffic fall back exactly to the owner."""
    runner = _runner(routed_env)
    capture: dict = {}
    _spy_handle(runner, capture)
    handler = runner._make_profile_message_handler(OWNER)

    await handler(_event(_source(chat_id="9199", guild_id=OTHER_GUILD)))
    assert capture["profile"] == OWNER
    assert routed_env.async_homes[-1] == routed_env.homes[OWNER]

    await handler(
        _event(_source(chat_id="u1", chat_type="dm", guild_id=None))
    )
    assert capture["profile"] == OWNER
    assert routed_env.async_homes[-1] == routed_env.homes[OWNER]
    assert capture["auth_home"] == routed_env.homes[OWNER]


@pytest.mark.asyncio
async def test_channel_and_thread_specificity_follows_existing_rules(routed_env):
    """guild < channel < thread, with parent-channel matching for threads."""
    runner = _runner(routed_env)
    capture: dict = {}
    _spy_handle(runner, capture)
    handler = runner._make_profile_message_handler(OWNER)

    # Plain message in the routed channel → channel route wins over guild.
    await handler(_event(_source()))
    assert capture["profile"] == "chanbot"

    # Message in THE routed thread → thread route wins.
    await handler(
        _event(_source(chat_id=THREAD, chat_type="thread", thread_id=THREAD,
                       parent_chat_id=CHANNEL))
    )
    assert capture["profile"] == "threadbot"

    # A different thread of the same channel inherits the channel route
    # (parent_chat_id match — forum/thread parent-chain rule).
    await handler(
        _event(_source(chat_id="9299", chat_type="thread", thread_id="9299",
                       parent_chat_id=CHANNEL))
    )
    assert capture["profile"] == "chanbot"

    # Any other channel of the guild → the guild route.
    await handler(_event(_source(chat_id="9188")))
    assert capture["profile"] == ROUTED


@pytest.mark.asyncio
async def test_build_source_stamp_wins_and_auth_home_is_owner(routed_env):
    """A source already route-stamped at ingress keeps its route (no restamp)."""
    runner = _runner(routed_env)
    capture: dict = {}
    _spy_handle(runner, capture)

    stamped = _source(chat_id="9199", profile=ROUTED)
    await runner._make_profile_message_handler(OWNER)(_event(stamped))

    assert capture["profile"] == ROUTED
    assert capture["auth_home"] == routed_env.homes[OWNER]


@pytest.mark.asyncio
async def test_unserved_route_fails_closed(routed_env):
    """A route naming a profile this gateway does not serve drops the message.

    Uses the REAL ``_handle_message``: its ingress gate is the fail-closed
    boundary, and it must see the rejection marker the handler set.
    """
    runner = _runner(
        routed_env,
        routes=[ProfileRoute(name="ghost", platform="discord",
                             guild_id=GUILD, profile="ghost")],
    )
    handled = []
    original = GatewayRunner._handle_message

    async def _spy(event):
        handled.append(True)
        return await original(runner, event)

    runner._handle_message = _spy

    source = _source(chat_id="9199")
    result = await runner._make_profile_message_handler(OWNER)(_event(source))

    assert result is None
    assert handled == [True]
    assert getattr(source, "profile_route_rejected", False) is True
    assert source.profile is None
    # Never owner-executed: only the (immediately dropped) turn's wrapper
    # ran, against the owner home, and no routed runtime was entered.
    assert routed_env.async_homes == [routed_env.homes[OWNER]]


@pytest.mark.asyncio
async def test_invalid_route_target_fails_closed(routed_env):
    """A matched route whose target name cannot be a served profile rejects.

    Route matching is string-based; the served-profile gate is what contains
    an invalid/traversal target, so the source must come out rejected rather
    than owner-executed.
    """
    runner = _runner(
        routed_env,
        routes=[ProfileRoute(name="bad", platform="discord",
                             guild_id=GUILD, profile="../escape")],
    )
    capture: dict = {}
    _spy_handle(runner, capture)

    source = _source(chat_id="9199")
    await runner._make_profile_message_handler(OWNER)(_event(source))

    assert getattr(source, "profile_route_rejected", False) is True
    assert source.profile is None
    assert "profile" not in capture or capture["profile"] is None


# ─── authorization vs runtime scope split ───────────────────────────────────


@pytest.mark.asyncio
async def test_authorization_reads_owner_scope_runtime_uses_routed(
    routed_env, monkeypatch
):
    """Inside one routed secondary turn: auth home = owner, ambient = routed.

    The async runtime scope installs the routed home; ``_is_user_authorized_
    for_source`` must re-enter the owner's home for the allowlist read even
    while the turn itself is ambient in the routed runtime. That is the
    headless-profile contract: the routed runtime needs no platform token
    or allowlist of its own.
    """
    from hermes_constants import get_hermes_home

    runner = _runner(routed_env)
    auth_ambient: dict = {}

    def _fake_is_authorized(source, **kwargs):
        auth_ambient["home_at_check"] = Path(get_hermes_home())
        auth_ambient["profile"] = source.profile
        return True

    runner._is_user_authorized = _fake_is_authorized

    async def _fake_handle(event):
        # The auth gate ``_handle_message`` would run, invoked directly here
        # so the observation does not depend on the rest of the pipeline.
        assert runner._is_user_authorized_for_source(event.source) is True
        return "ok"

    runner._handle_message = _fake_handle

    # Scopes that record AND install the real home override (no secret-file
    # loading), so ambient-home observations are meaningful.
    _install_real_scopes(monkeypatch)

    result = await runner._make_profile_message_handler(OWNER)(
        _event(_source(chat_id="9199"))
    )

    assert result == "ok"
    assert auth_ambient["profile"] == ROUTED
    # Allowlist read happened under the OWNER's home, not the routed one.
    assert auth_ambient["home_at_check"] == routed_env.homes[OWNER]
    assert auth_ambient["home_at_check"] != routed_env.homes[ROUTED]


# ─── busy-session path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_busy_session_key_uses_routed_profile(routed_env):
    """A follow-up while the routed session runs keys into the routed lane."""
    runner = _runner(routed_env)
    captured: dict = {}

    async def _fake_busy(event, session_key):
        captured["key"] = session_key
        captured["profile"] = event.source.profile
        return True

    runner._handle_active_session_busy_message = _fake_busy

    handled = await runner._make_profile_busy_session_handler(OWNER)(
        _event(_source(chat_id="9199")), "adapter-ingress-key"
    )

    assert handled is True
    assert captured["profile"] == ROUTED
    assert captured["key"].startswith(f"agent:{ROUTED}:")


@pytest.mark.asyncio
async def test_busy_session_unrouted_key_uses_owner(routed_env):
    runner = _runner(routed_env)
    captured: dict = {}

    async def _fake_busy(event, session_key):
        captured["key"] = session_key
        return True

    runner._handle_active_session_busy_message = _fake_busy

    await runner._make_profile_busy_session_handler(OWNER)(
        _event(_source(chat_id="9199", guild_id=OTHER_GUILD)),
        "adapter-ingress-key",
    )

    assert captured["key"].startswith(f"agent:{OWNER}:")


@pytest.mark.asyncio
async def test_busy_session_unserved_route_fails_closed(routed_env):
    """A rejected route drops the busy follow-up instead of borrowing a lane."""
    runner = _runner(
        routed_env,
        routes=[ProfileRoute(name="ghost", platform="discord",
                             guild_id=GUILD, profile="ghost")],
    )
    called = []

    async def _fake_busy(event, session_key):
        called.append(session_key)
        return True

    runner._handle_active_session_busy_message = _fake_busy

    handled = await runner._make_profile_busy_session_handler(OWNER)(
        _event(_source(chat_id="9199")), "adapter-ingress-key"
    )

    assert handled is True  # "handled" = consumed, i.e. dropped
    assert called == []


@pytest.mark.asyncio
async def test_busy_turn_auth_reads_owner_home_policy_reads_routed_home(
    routed_env, monkeypatch
):
    """The REAL busy handler: auth gate under owner home, runtime under routed.

    Exercises the real ``_handle_active_session_busy_message`` far enough to
    observe both sides of the split: its authorization call must run under
    the OWNER's home (via ``_is_user_authorized_for_source`` and the stamped
    transport home) while the next profile-sensitive read — the busy-policy
    resolution — runs under the ROUTED home the handler wrapper entered.

    Mutation sensitivity:
    * Reverting the busy method to ``_is_user_authorized`` makes the auth
      read observe the routed (ambient) home → ``auth_home`` assertion fails.
    * Dropping the wrapper's runtime scope makes the policy read observe the
      launch home → ``busy_mode_home`` assertion fails.
    """
    from hermes_constants import get_hermes_home

    runner = _runner(routed_env)
    observed: dict = {}

    def _fake_is_authorized(source, **kwargs):
        observed["auth_home"] = Path(get_hermes_home())
        return True

    runner._is_user_authorized = _fake_is_authorized

    def _spy_busy_mode(source):
        # First profile-sensitive read after the auth gate inside the real
        # busy method (``_effective_busy_input_mode``).
        observed["busy_mode_home"] = Path(get_hermes_home())
        return "interrupt"

    runner._effective_busy_input_mode = _spy_busy_mode
    # Draining with no resolvable adapter returns right after the policy
    # read, keeping the probe inside the real method's first lines.
    runner._draining = True
    runner.adapters = {}
    runner._profile_adapters = {}

    _install_real_scopes(monkeypatch)

    handled = await runner._make_profile_busy_session_handler(OWNER)(
        _event(_source(chat_id="9199")), "adapter-ingress-key"
    )

    assert handled is True
    assert observed["auth_home"] == routed_env.homes[OWNER]
    assert observed["auth_home"] != routed_env.homes[ROUTED]
    assert observed["busy_mode_home"] == routed_env.homes[ROUTED]


@pytest.mark.asyncio
async def test_busy_turn_unrouted_runs_under_owner_home(routed_env, monkeypatch):
    """Unmatched secondary busy traffic executes under the OWNER home."""
    from hermes_constants import get_hermes_home

    runner = _runner(routed_env)
    observed: dict = {}

    def _fake_is_authorized(source, **kwargs):
        observed["auth_home"] = Path(get_hermes_home())
        return True

    runner._is_user_authorized = _fake_is_authorized

    def _spy_busy_mode(source):
        observed["busy_mode_home"] = Path(get_hermes_home())
        return "interrupt"

    runner._effective_busy_input_mode = _spy_busy_mode
    runner._draining = True
    runner.adapters = {}
    runner._profile_adapters = {}

    _install_real_scopes(monkeypatch)

    handled = await runner._make_profile_busy_session_handler(OWNER)(
        _event(_source(chat_id="9199", guild_id=OTHER_GUILD)),
        "adapter-ingress-key",
    )

    assert handled is True
    assert observed["auth_home"] == routed_env.homes[OWNER]
    assert observed["busy_mode_home"] == routed_env.homes[OWNER]


# ─── platform-event path ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_platform_event_routes_execution_and_keeps_owner_auth(routed_env):
    runner = _runner(routed_env)
    captured: dict = {}

    async def _fake_platform_event(event, source):
        captured["profile"] = source.profile
        captured["auth_home"] = getattr(
            source, "_authorization_profile_home", None
        )

    runner._handle_gateway_platform_event = _fake_platform_event

    await runner._make_profile_platform_event_handler(OWNER)(
        {"event_type": "message_edited"}, _source(chat_id="9199")
    )

    assert captured["profile"] == ROUTED
    assert captured["auth_home"] == routed_env.homes[OWNER]
    assert routed_env.sync_homes == [routed_env.homes[ROUTED]]


@pytest.mark.asyncio
async def test_platform_event_unrouted_uses_owner(routed_env):
    runner = _runner(routed_env)
    captured: dict = {}

    async def _fake_platform_event(event, source):
        captured["profile"] = source.profile

    runner._handle_gateway_platform_event = _fake_platform_event

    await runner._make_profile_platform_event_handler(OWNER)(
        {"event_type": "message_edited"},
        _source(chat_id="9199", guild_id=OTHER_GUILD),
    )

    assert captured["profile"] == OWNER
    assert routed_env.sync_homes[-1] == routed_env.homes[OWNER]


@pytest.mark.asyncio
async def test_platform_event_unserved_route_fails_closed(routed_env):
    """A route to an unserved profile drops the event before hook dispatch.

    Fail-closed parity with the message/busy/auth seams: no authorization
    read, no ``gateway_platform_event`` hook, and no owner fallback.
    """
    runner = _runner(
        routed_env,
        routes=[ProfileRoute(name="ghost", platform="discord",
                             guild_id=GUILD, profile="ghost")],
    )
    called = []

    async def _fake_platform_event(event, source):
        called.append(event)

    runner._handle_gateway_platform_event = _fake_platform_event

    source = _source(chat_id="9199")
    await runner._make_profile_platform_event_handler(OWNER)(
        {"event_type": "message_edited"}, source
    )

    assert called == []
    assert getattr(source, "profile_route_rejected", False) is True
    assert source.profile is None
    # Never owner-executed: no runtime scope was entered at all.
    assert routed_env.sync_homes == []


@pytest.mark.asyncio
async def test_platform_event_invalid_route_target_fails_closed(routed_env):
    """An invalid route target rejects the event instead of owner-dispatching."""
    runner = _runner(
        routed_env,
        routes=[ProfileRoute(name="bad", platform="discord",
                             guild_id=GUILD, profile="../escape")],
    )
    called = []

    async def _fake_platform_event(event, source):
        called.append(event)

    runner._handle_gateway_platform_event = _fake_platform_event

    source = _source(chat_id="9199")
    await runner._make_profile_platform_event_handler(OWNER)(
        {"event_type": "message_edited"}, source
    )

    assert called == []
    assert getattr(source, "profile_route_rejected", False) is True
    assert source.profile is None
    assert routed_env.sync_homes == []


@pytest.mark.asyncio
async def test_platform_event_hook_runs_routed_home_auth_reads_owner(
    routed_env, monkeypatch
):
    """The REAL platform-event handler: hook under routed home, auth under owner.

    Exercises the real ``_handle_gateway_platform_event`` with a registered
    hook: the ``_is_user_authorized_for_source`` gate must observe the OWNER
    home while the ``gateway_platform_event`` hook dispatch observes the
    ROUTED home the wrapper entered.

    Mutation sensitivity: reverting the method to ``_is_user_authorized``
    makes the auth read observe the routed (ambient) home → the
    ``auth_home`` assertion fails.
    """
    import hermes_cli.lifecycle as lifecycle
    from hermes_constants import get_hermes_home

    runner = _runner(routed_env)
    observed: dict = {}

    monkeypatch.setattr(
        lifecycle, "has_hook", lambda name: name == "gateway_platform_event"
    )

    def _fake_invoke_hook(hook_name, **kwargs):
        observed["hook"] = hook_name
        observed["hook_home"] = Path(get_hermes_home())
        return []

    monkeypatch.setattr(lifecycle, "invoke_hook", _fake_invoke_hook)

    def _fake_is_authorized(source, **kwargs):
        observed["auth_home"] = Path(get_hermes_home())
        return True

    runner._is_user_authorized = _fake_is_authorized

    _install_real_scopes(monkeypatch)

    await runner._make_profile_platform_event_handler(OWNER)(
        {"event_type": "message_edited"}, _source(chat_id="9199")
    )

    assert observed["hook"] == "gateway_platform_event"
    assert observed["auth_home"] == routed_env.homes[OWNER]
    assert observed["auth_home"] != routed_env.homes[ROUTED]
    assert observed["hook_home"] == routed_env.homes[ROUTED]


# ─── adapter auth-check callback ────────────────────────────────────────────
#
# The callback carries chat/channel/thread ids but no guild id (the adapter
# seam it serves — external-context sender verification — never had one), so
# channel routes are the ones that can match here. That is the same
# specificity contract the primary callback has always had.


def _auth_check_runner(routed_env):
    runner = _runner(routed_env)
    seen: dict = {}

    def _fake_is_authorized(source, **kwargs):
        seen["profile"] = source.profile
        seen["auth_home"] = getattr(
            source, "_authorization_profile_home", None
        )
        return True

    runner._is_user_authorized = _fake_is_authorized
    return runner, seen


def test_auth_check_routed_chat_consults_routed_profile(routed_env):
    """External-context senders verify like inbound messages (#86296 parity).

    The callback carries no guild id, so only a chat-scoped route (no guild
    constraint) can match — this one selects chanbot for the sender check
    while the allowlist read stays under the owner's home.
    """
    runner, seen = _auth_check_runner(routed_env)
    runner.config.profile_routes = [
        ProfileRoute(name="channel", platform="discord", chat_id=CHANNEL,
                     profile="chanbot")
    ]
    check = runner._make_adapter_auth_check(Platform.DISCORD, profile_name=OWNER)

    assert check("u1", "group", CHANNEL) is True
    assert seen["profile"] == "chanbot"
    assert seen["auth_home"] == routed_env.homes[OWNER]


def test_auth_check_guild_constrained_route_needs_trusted_guild(routed_env):
    """A guild+chat route must not match a guild-less callback source.

    ``ProfileRoute.matches`` requires every declared discriminator to hold
    against trusted source metadata; the auth-check seam carries no guild
    id, so a guild-constrained route (like ``_routes()``'s chanbot route)
    falls through and the check runs under the owner — never silently
    inheriting a guild-scoped route off an unverified chat id alone.
    """
    runner, seen = _auth_check_runner(routed_env)
    check = runner._make_adapter_auth_check(Platform.DISCORD, profile_name=OWNER)

    assert check("u1", "group", CHANNEL) is True
    assert seen["profile"] == OWNER
    assert seen["auth_home"] == routed_env.homes[OWNER]


def test_auth_check_unrouted_chat_uses_owner(routed_env):
    runner, seen = _auth_check_runner(routed_env)
    check = runner._make_adapter_auth_check(Platform.DISCORD, profile_name=OWNER)

    # DM (no guild context on the callback): no route matches → owner.
    assert check("u1", "dm", "u1") is True
    assert seen["profile"] == OWNER
    assert seen["auth_home"] == routed_env.homes[OWNER]


def test_auth_check_unserved_route_fails_closed(routed_env):
    runner, seen = _auth_check_runner(routed_env)
    runner.config.profile_routes = [
        ProfileRoute(name="ghost", platform="discord", chat_id=CHANNEL,
                     profile="ghost")
    ]
    check = runner._make_adapter_auth_check(Platform.DISCORD, profile_name=OWNER)

    assert check("u1", "group", CHANNEL) is False
    assert "profile" not in seen  # denied before any policy read


def test_auth_check_allowlist_read_runs_under_owner_home(routed_env, monkeypatch):
    """The callback's policy read executes under the OWNER's home, not ambient.

    ``_apply_route_or_owner_profile`` stamps ``_authorization_profile_home``
    before EITHER call variant, so asserting on the stamped attribute cannot
    catch a revert from ``_is_user_authorized_for_source`` to a bare
    ``_is_user_authorized``. Observing the ambient home during the real
    ``_is_user_authorized`` call does: with the launch home ambient, only the
    for-source wrapper re-enters the owner's scope for the read.
    """
    from hermes_constants import (
        get_hermes_home,
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    runner = _runner(routed_env)
    runner.config.profile_routes = [
        ProfileRoute(name="channel", platform="discord", chat_id=CHANNEL,
                     profile="chanbot")
    ]
    observed: dict = {}

    def _fake_is_authorized(source, **kwargs):
        observed["home"] = Path(get_hermes_home())
        observed["profile"] = source.profile
        return True

    runner._is_user_authorized = _fake_is_authorized
    _install_real_scopes(monkeypatch)

    adapter = _FakeAdapter("owner-bot")
    runner.adapters = {}
    runner._profile_adapters = {OWNER: {Platform.DISCORD: adapter}}

    # Ambient home = the launch home, deliberately NOT the owner's.
    token = set_hermes_home_override(str(routed_env.homes["default"]))
    try:
        check = runner._make_adapter_auth_check(Platform.DISCORD, profile_name=OWNER)
        assert check("u1", "group", CHANNEL) is True
    finally:
        reset_hermes_home_override(token)

    assert observed["profile"] == "chanbot"  # routed pairing store consulted
    assert observed["home"] == routed_env.homes[OWNER]
    assert observed["home"] != routed_env.homes["default"]


# ─── egress stays on the ingress adapter ────────────────────────────────────


class _FakeAdapter:
    """Weak-referenceable stand-in for a secondary adapter.

    ``build_source``/the auth-check seam store the ingress adapter as a
    weakref on the source; ``SimpleNamespace`` does not support weak
    references, so the fixture needs a real (slotted) object.
    """

    __slots__ = ("name", "__weakref__")

    def __init__(self, name):
        self.name = name


class _IngressAdapter(BasePlatformAdapter):
    """Minimal deterministic secondary adapter: records sends, no network.

    A real ``BasePlatformAdapter`` subclass so the source under test is built
    by the REAL ``build_source`` path — the same code that stamps the routed
    profile and records the transport provenance weakref at production
    ingress.
    """

    def __init__(self, platform):
        super().__init__(PlatformConfig(), platform)
        self.sent: list = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id="m1")

    async def get_chat_info(self, chat_id):
        return {"name": "fake", "type": "group"}


@pytest.mark.asyncio
async def test_outbound_send_uses_ingress_adapter_for_routed_headless_profile(
    routed_env,
):
    """Replies leave through the secondary bot that admitted the message.

    The source is built through the REAL ingress ``build_source`` path, which
    stamps both the routed profile and the transport-provenance weakref.
    After the turn routes to a HEADLESS profile (no adapter of its own), an
    actual send through the runner's delivery helper must still reach the
    owning secondary adapter.

    Mutation sensitivity: removing the provenance assignment from
    ``build_source`` makes ``_adapter_for_source`` fall back to the routed
    profile's (empty) adapter registry — fail-closed to None — and the send
    assertion below fails.
    """
    runner = _runner(routed_env)
    adapter = _IngressAdapter(Platform.DISCORD)
    adapter.gateway_runner = runner
    runner.adapters = {}
    # ROUTED is deliberately headless: only the owner has an adapter.
    runner._profile_adapters = {OWNER: {Platform.DISCORD: adapter}}

    source = adapter.build_source(
        chat_id="9199",
        chat_type="group",
        user_id="u1",
        user_name="u1",
        guild_id=GUILD,
    )
    # Real ingress routing + provenance, no manual injection.
    assert source.profile == ROUTED
    assert source._transport_adapter_ref() is adapter

    capture: dict = {}
    _spy_handle(runner, capture)
    await runner._make_profile_message_handler(OWNER)(_event(source))
    assert capture["profile"] == ROUTED

    # An actual outbound send through the runner's real delivery path.
    await runner._deliver_platform_notice(source, "progress: still working")

    assert [s["chat_id"] for s in adapter.sent] == ["9199"]
    assert adapter.sent[0]["content"] == "progress: still working"


# ─── invalid route target loaded through GatewayConfig.from_dict ────────────
#
# The parser retains an explicitly configured invalid target as a matchable
# route (fail-closed); these tests prove that a config.yaml-shaped dict
# loaded through the REAL ``GatewayConfig.from_dict`` → ``parse_profile_routes``
# path is rejected at every secondary dispatch seam — message, busy-session,
# platform-event, and adapter-auth — instead of becoming owner fallback.

BAD_TARGET = "../escape"


def _runner_from_config_dict(routed_env, raw_routes):
    """A multiplex runner whose routes came through ``GatewayConfig.from_dict``."""
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig.from_dict({
        "multiplex_profiles": True,
        "profile_routes": raw_routes,
    })
    # Parity fixture invariant: the served set never contains the target.
    assert runner.config.profile_routes, "invalid target must be retained"
    return runner


def _invalid_guild_route():
    return [
        {"name": "bad", "platform": "discord", "guild_id": GUILD,
         "profile": BAD_TARGET}
    ]


@pytest.mark.asyncio
async def test_config_dict_invalid_target_message_fails_closed(routed_env):
    """Invalid target via from_dict: the real ingress gate drops the message."""
    runner = _runner_from_config_dict(routed_env, _invalid_guild_route())
    handled = []
    original = GatewayRunner._handle_message

    async def _spy(event):
        handled.append(True)
        return await original(runner, event)

    runner._handle_message = _spy

    source = _source(chat_id="9199")
    result = await runner._make_profile_message_handler(OWNER)(_event(source))

    assert result is None
    assert handled == [True]  # reached the gate, then dropped — never ran
    assert getattr(source, "profile_route_rejected", False) is True
    assert source.profile is None


@pytest.mark.asyncio
async def test_config_dict_invalid_target_busy_never_dispatches(routed_env):
    """Invalid target via from_dict: busy follow-up is consumed, not dispatched."""
    runner = _runner_from_config_dict(routed_env, _invalid_guild_route())
    called = []

    async def _fake_busy(event, session_key):
        called.append(session_key)
        return True

    runner._handle_active_session_busy_message = _fake_busy

    handled = await runner._make_profile_busy_session_handler(OWNER)(
        _event(_source(chat_id="9199")), "adapter-ingress-key"
    )

    assert handled is True  # consumed = dropped
    assert called == []


@pytest.mark.asyncio
async def test_config_dict_invalid_target_platform_event_never_dispatches(routed_env):
    """Invalid target via from_dict: no auth read, no hook dispatch, no owner."""
    runner = _runner_from_config_dict(routed_env, _invalid_guild_route())
    called = []

    async def _fake_platform_event(event, source):
        called.append(event)

    runner._handle_gateway_platform_event = _fake_platform_event

    source = _source(chat_id="9199")
    await runner._make_profile_platform_event_handler(OWNER)(
        {"event_type": "message_edited"}, source
    )

    assert called == []
    assert getattr(source, "profile_route_rejected", False) is True
    assert source.profile is None
    assert routed_env.sync_homes == []


def test_config_dict_invalid_target_auth_check_denies(routed_env):
    """Invalid target via from_dict: the adapter auth callback denies closed."""
    runner = _runner_from_config_dict(
        routed_env,
        # The auth-check seam carries no guild id — use a chat-scoped route.
        [{"name": "bad", "platform": "discord", "chat_id": CHANNEL,
          "profile": BAD_TARGET}],
    )
    seen = []

    def _fake_is_authorized(source, **kwargs):
        seen.append(source)
        return True

    runner._is_user_authorized = _fake_is_authorized
    check = runner._make_adapter_auth_check(Platform.DISCORD, profile_name=OWNER)

    assert check("u1", "group", CHANNEL) is False
    assert seen == []  # denied before any policy read


# ─── primary behavior unchanged ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_primary_handler_routing_unchanged(routed_env):
    """The default-profile handler keeps its #86296/#84079 contract exactly."""
    runner = _runner(routed_env)
    capture: dict = {}
    _spy_handle(runner, capture)
    handler = runner._make_default_profile_message_handler()

    launch_home = Path(gateway_run.get_hermes_home())

    # Routed chat → routed profile + launch transport home.
    await handler(_event(_source(chat_id="9199")))
    assert capture["profile"] == ROUTED
    assert capture["auth_home"] == launch_home
    assert routed_env.async_homes[-1] == routed_env.homes[ROUTED]

    # Unrouted chat → no profile stamp, launch home, unchanged.
    await handler(_event(_source(chat_id="9199", guild_id=OTHER_GUILD)))
    assert capture["profile"] is None
    assert capture["auth_home"] == launch_home
    assert routed_env.async_homes[-1] == launch_home


@pytest.mark.asyncio
async def test_primary_busy_path_unchanged_without_transport_home(routed_env):
    """Primary busy sources carry no auth-home marker → plain scope check.

    ``_handle_active_session_busy_message`` authorizes via
    ``_is_user_authorized_for_source``; with no marker stamped the check must
    be a passthrough of the exact source object, exactly as before.
    """
    runner = _runner(routed_env)
    authorized = []

    def _fake_is_authorized(source, **kwargs):
        authorized.append(source)
        return True

    runner._is_user_authorized = _fake_is_authorized
    runner._draining = True
    # Draining + no resolvable adapter returns right after the auth gate.

    event = _event(_source(chat_id="9199", profile=ROUTED))
    assert await runner._handle_active_session_busy_message(event, "k") is True
    assert authorized == [event.source]
