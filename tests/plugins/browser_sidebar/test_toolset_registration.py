"""browser_sidebar toolset wiring: default-off + platform restriction exact sets.

The restriction map is asserted by EXACT equality so a new key can never be
added silently (and the pre-existing discord keys stay asserted here).
"""

from hermes_cli.toolset_scope import _TOOLSET_PLATFORM_RESTRICTIONS, toolset_allowed_for_platform
from hermes_cli.tools_config import _DEFAULT_OFF_TOOLSETS


def test_restriction_map_exact_equality():
    assert _TOOLSET_PLATFORM_RESTRICTIONS == {
        "discord": {"discord"},
        "discord_admin": {"discord"},
        "browser_sidebar": {"browser_sidebar"},
    }


def test_browser_sidebar_toolset_only_on_its_platform():
    assert toolset_allowed_for_platform("browser_sidebar", "browser_sidebar")
    for platform in ("discord", "cli", "telegram", "matrix"):
        assert not toolset_allowed_for_platform("browser_sidebar", platform), platform


def test_default_off_exact_equality():
    assert _DEFAULT_OFF_TOOLSETS == {
        "moa", "video", "video_gen", "x_search", "quota_channels", "assistant_handoff",
        "file_readonly", "skills_list", "browser_sidebar",
    }


def test_discord_keys_still_asserted():
    """Old keys keep their old meaning (guard against accidental removal)."""
    assert toolset_allowed_for_platform("discord", "discord")
    assert not toolset_allowed_for_platform("discord", "matrix")
    assert toolset_allowed_for_platform("discord_admin", "discord")
    assert not toolset_allowed_for_platform("discord_admin", "cli")


def test_browser_sidebar_not_leaked_to_other_platforms():
    from hermes_cli.tools_config import _get_platform_tools
    config = {"platform_toolsets": {"matrix": ["hermes-matrix", "browser_sidebar"]}}
    enabled = _get_platform_tools(config, "matrix", include_default_mcp_servers=False)
    assert "browser_sidebar" not in enabled
