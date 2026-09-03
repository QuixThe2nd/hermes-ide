"""Tests for gateway/profile_routing.py — profile-based routing."""

import json

import pytest
from gateway.profile_routing import (
    ProfileRoute,
    parse_profile_routes,
    match_profile_route,
)


class TestProfileRoute:
    def test_specificity_thread(self):
        r = ProfileRoute(name="t", platform="discord", profile="p",
                         guild_id="g", chat_id="c", thread_id="t")
        assert r.specificity == 14  # 2 + 4 + 8


    def test_frozen(self):
        r = ProfileRoute(name="x", platform="discord", profile="p")
        with pytest.raises(AttributeError):
            r.name = "y"


class TestProfileRouteMatching:
    def test_exact_thread_match(self):
        r = ProfileRoute(name="t", platform="discord", profile="trader",
                         guild_id="111", chat_id="222", thread_id="333")
        assert r.matches("discord", guild_id="111", chat_id="222", thread_id="333")
        assert not r.matches("discord", guild_id="111", chat_id="222", thread_id="444")


    def test_guild_and_chat_are_conjunctive(self):
        # A route declaring BOTH guild_id and chat_id requires both to match.
        # Regression guard: previously chat_id was checked first and returned
        # True before guild_id was ever consulted.
        r = ProfileRoute(name="gc", platform="discord", profile="scoped",
                         guild_id="111", chat_id="222")
        # Both match (direct channel) -> match
        assert r.matches("discord", guild_id="111", chat_id="222")
        # Both match via parent (thread inside the channel) -> match
        assert r.matches("discord", guild_id="111", chat_id="333", parent_chat_id="222")
        # chat matches but guild differs -> NO match (the bug this guards)
        assert not r.matches("discord", guild_id="999", chat_id="222")
        # guild matches but chat differs -> NO match
        assert not r.matches("discord", guild_id="111", chat_id="333")


class TestParseProfileRoutes:
    def test_empty(self):
        assert parse_profile_routes(None) == []
        assert parse_profile_routes([]) == []

    def test_coerces_yaml_native_int_ids_to_str(self):
        # PyYAML loads unquoted snowflakes / negative Telegram ids as int;
        # inbound SessionSource ids are str, so un-coerced routes never match.
        routes = parse_profile_routes([
            {"name": "server", "platform": "discord", "profile": "p",
             "guild_id": 111, "chat_id": 222, "thread_id": 333},
            {"name": "tg", "platform": "telegram", "profile": "p",
             "chat_id": -1001234567890},
            {"name": "platform-only", "platform": "discord", "profile": "p"},
        ])
        by_name = {r.name: r for r in routes}
        assert (by_name["server"].guild_id, by_name["server"].chat_id,
                by_name["server"].thread_id) == ("111", "222", "333")
        assert match_profile_route(
            routes, "discord", guild_id="111", chat_id="222", thread_id="333",
        ).name == "server"
        assert match_profile_route(
            routes, "telegram", chat_id="-1001234567890",
        ).name == "tg"
        assert (by_name["platform-only"].guild_id, by_name["platform-only"].chat_id,
                by_name["platform-only"].thread_id) == (None, None, None)

    def test_non_int_numeric_ids_warn_instead_of_silently_coercing(self, caplog):
        # #86470 nuance: float/bool stringify to values that can never match
        # an inbound id, so surface the misconfiguration at load time.
        with caplog.at_level("WARNING", logger="gateway.profile_routing"):
            routes = parse_profile_routes([
                {"name": "f", "platform": "discord", "profile": "p", "chat_id": 123.0},
                {"name": "b", "platform": "discord", "profile": "p", "guild_id": True},
            ])
        assert {r.name for r in routes} == {"f", "b"}
        assert match_profile_route(routes, "discord", chat_id="123") is None
        assert sum("can never match" in rec.message for rec in caplog.records) == 2


class TestInvalidProfileTargets:
    """An explicitly configured invalid target must fail CLOSED, not open.

    Dropping the route at parse time would turn matching traffic into owner
    fallback (fail-open). The parser therefore retains the route so it can
    still match, and the served-profile check in
    ``GatewayRunner._profile_name_for_source`` rejects it with
    ``ProfileRouteRejected``. The invalid name is an opaque string on the
    route — never normalized into validity, never resolved as a path.
    """

    def test_invalid_target_retained_as_matching_route(self, caplog):
        with caplog.at_level("WARNING", logger="gateway.profile_routing"):
            routes = parse_profile_routes([
                {"name": "bad", "platform": "discord", "guild_id": "9001",
                 "profile": "../escape"},
            ])
        assert len(routes) == 1
        assert routes[0].profile == "../escape"
        # The route still matches its configured scope...
        assert routes[0].matches("discord", guild_id="9001")
        assert match_profile_route(routes, "discord", guild_id="9001") is routes[0]
        # ...and the misconfiguration is logged safely (repr-quoted).
        assert sum(
            "invalid profile name" in rec.message and "'../escape'" in rec.message
            for rec in caplog.records
        ) == 1

    def test_valid_names_still_normalized_and_validated(self):
        routes = parse_profile_routes([
            {"name": "ok", "platform": "discord", "guild_id": "1",
             "profile": "  Reviews  "},
        ])
        assert [r.profile for r in routes] == ["reviews"]

    def test_missing_platform_or_profile_still_skipped(self):
        assert parse_profile_routes([
            {"name": "no-platform", "profile": "x"},
            {"name": "no-profile", "platform": "discord"},
        ]) == []

    def test_disabled_invalid_route_stays_inactive(self):
        routes = parse_profile_routes([
            {"name": "bad", "platform": "discord", "guild_id": "9001",
             "profile": "../escape", "enabled": False},
        ])
        assert len(routes) == 1
        assert not routes[0].matches("discord", guild_id="9001")
        assert match_profile_route(routes, "discord", guild_id="9001") is None


class TestGatewayConfigInvalidTargetIntegration:
    """End-to-end: an invalid target loaded through ``GatewayConfig.from_dict``
    is retained by the parser and then rejected by the canonical resolver —
    without the invalid name ever reaching a filesystem-path resolution."""

    def test_from_dict_invalid_target_rejected_fail_closed(
        self, monkeypatch, tmp_path
    ):
        import hermes_cli.profiles as profiles_mod
        from gateway.config import GatewayConfig, Platform
        from gateway.profile_routing import ProfileRouteRejected
        from gateway.run import GatewayRunner
        from gateway.session import SessionSource

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        served = [("default", tmp_path), ("research", tmp_path / "profiles" / "research")]
        monkeypatch.setattr(
            "hermes_cli.profiles.profiles_to_serve",
            lambda multiplex, profile_allowlist=None: served,
        )
        path_resolutions = []
        real_get_profile_dir = profiles_mod.get_profile_dir

        def _recording_get_profile_dir(name):
            path_resolutions.append(name)
            return real_get_profile_dir(name)

        monkeypatch.setattr(
            profiles_mod, "get_profile_dir", _recording_get_profile_dir
        )

        config = GatewayConfig.from_dict({
            "multiplex_profiles": True,
            "profile_routes": [
                {"name": "bad", "platform": "discord", "guild_id": "9001",
                 "profile": "../escape"},
            ],
        })
        # Retained by the parser (fail-closed), not silently dropped.
        assert len(config.profile_routes) == 1
        assert config.profile_routes[0].profile == "../escape"

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = config
        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id="9101",
            chat_type="group",
            user_id="u1",
            guild_id="9001",
        )
        with pytest.raises(ProfileRouteRejected):
            runner._profile_name_for_source(source)
        # The canonical resolver never resolved the invalid name as a path.
        assert "../escape" not in path_resolutions

        # Unmatched traffic is unaffected by the retained invalid route.
        other = SessionSource(
            platform=Platform.DISCORD,
            chat_id="9199",
            chat_type="group",
            user_id="u1",
            guild_id="9002",
        )
        assert runner._profile_name_for_source(other) is None


class TestMatchProfileRoute:


    def test_no_match_returns_none(self):
        routes = [
            ProfileRoute(name="r", platform="telegram", profile="p"),
        ]
        assert match_profile_route(routes, "discord") is None


class TestSessionKeyIntegration:
    def test_default_profile_key(self):
        from gateway.session import build_session_key, SessionSource, Platform
        src = SessionSource(platform=Platform.DISCORD, chat_id="123",
                            chat_type="channel", user_id="456")
        key = build_session_key(src)
        assert key.startswith("agent:main:")


class TestParentChatIdMatching:
    """Thread messages carry thread_id as chat_id; parent_chat_id is the channel."""

    def test_channel_route_matches_via_parent_chat_id(self):
        r = ProfileRoute(name="ch", platform="discord", profile="trader",
                         chat_id="222")
        assert r.matches("discord", chat_id="333", parent_chat_id="222")


    def test_match_profile_route_with_parent_chat_id(self):
        routes = [
            ProfileRoute(name="ch", platform="discord", profile="trader",
                         chat_id="222"),
        ]
        m = match_profile_route(routes, "discord", chat_id="333", parent_chat_id="222")
        assert m is not None
        assert m.profile == "trader"


class TestForumPostMatching:
    """Test that forum posts match via parent_chat_id (direct parent)."""


    def test_forum_post_comment_matches_channel_not_thread_id(self):
        """Verify that thread_id matching is distinct from parent_chat_id matching."""
        routes = [
            ProfileRoute(name="forum", platform="discord", profile="forum_profile",
                         chat_id="forum_channel_123"),
            ProfileRoute(name="post", platform="discord", profile="post_profile",
                         thread_id="post_thread_456"),
        ]
        # A comment on the forum post should match the forum channel route, not the thread route
        m = match_profile_route(routes, "discord", chat_id="post_thread_456", 
                                 parent_chat_id="forum_channel_123")
        assert m is not None
        assert m.profile == "forum_profile"


class TestWhatsAppChatIdIdentityMatching:
    """WhatsApp ``chat_id`` routes match across number / JID / LID forms (the
    same alias canonicalization allowlists and session keys already use);
    every other platform, and WhatsApp groups, stay exact-compare."""

    PHONE = "15551234567"
    LID = "999999999999999"

    def _write_lid_mapping(self, tmp_path, monkeypatch):
        mapping_dir = tmp_path / "platforms" / "whatsapp" / "session"
        mapping_dir.mkdir(parents=True)
        (mapping_dir / f"lid-mapping-{self.PHONE}.json").write_text(json.dumps(f"{self.LID}@lid"))
        (mapping_dir / f"lid-mapping-{self.LID}_reverse.json").write_text(
            json.dumps(f"{self.PHONE}@s.whatsapp.net")
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def test_number_route_matches_jid_and_mapped_lid_forms(self, tmp_path, monkeypatch):
        self._write_lid_mapping(tmp_path, monkeypatch)
        for platform in ("whatsapp", "whatsapp_cloud"):
            r = ProfileRoute(name="owner", platform=platform, profile="owner", chat_id=self.PHONE)
            assert r.matches(platform, chat_id=f"{self.PHONE}@s.whatsapp.net")
            assert r.matches(platform, chat_id=f"{self.PHONE}:47@s.whatsapp.net")
            assert r.matches(platform, chat_id=f"{self.LID}@lid")
            # Alias fallback also applies to the thread-parent slot.
            assert r.matches(platform, chat_id="thread-1", parent_chat_id=f"{self.LID}@lid")
            assert not r.matches(platform, chat_id="15550001111@s.whatsapp.net")

    def test_groups_and_other_platforms_stay_exact(self, tmp_path, monkeypatch):
        self._write_lid_mapping(tmp_path, monkeypatch)
        group = "120363012345678901@g.us"
        owner = ProfileRoute(name="owner", platform="whatsapp", profile="owner", chat_id=self.PHONE)
        assert not owner.matches("whatsapp", chat_id=group)
        grp = ProfileRoute(name="grp", platform="whatsapp", profile="grp", chat_id=group)
        assert grp.matches("whatsapp", chat_id=group)
        assert not grp.matches("whatsapp", chat_id=f"{self.PHONE}@s.whatsapp.net")
        # Stripping @g.us must never turn a group into a phone-identity match.
        assert not ProfileRoute(
            name="oops", platform="whatsapp", profile="owner", chat_id=group.split("@", 1)[0]
        ).matches("whatsapp", chat_id=group)
        tg = ProfileRoute(name="tg", platform="telegram", profile="owner", chat_id="640466638")
        assert tg.matches("telegram", chat_id="640466638")
        assert not tg.matches("telegram", chat_id="640466638@s.whatsapp.net")
