"""Cross-layer contracts for the Camofox-only browser surface."""

import json
from unittest.mock import MagicMock, patch

import model_tools  # noqa: F401 — discover the real registry entries
from toolsets import resolve_toolset
from tools import browser_camofox, browser_tool
from tools.registry import registry


CORE_BROWSER_TOOLS = {
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_scroll",
    "browser_back",
    "browser_press",
    "browser_get_images",
    "browser_vision",
    "browser_console",
}


def test_exact_browser_registry_and_toolset_exposure():
    assert {schema["name"] for schema in browser_tool.BROWSER_TOOL_SCHEMAS} == CORE_BROWSER_TOOLS
    assert CORE_BROWSER_TOOLS <= set(resolve_toolset("browser"))
    registered = {entry.name for entry in registry.get_all_entries()}
    assert CORE_BROWSER_TOOLS <= registered
    assert not {"browser_exec", "browser_cdp", "browser_dialog"} & registered


def test_legacy_browser_yaml_is_ignored_without_rewriting_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """browser:
  engine: lightpanda
  backend: browser-use
  cloud_provider: browserbase
  cdp_url: http://legacy-cdp:9222
  use_real_profile: true
  camofox:
    managed_persistence: true
""",
        encoding="utf-8",
    )
    original = config_path.read_bytes()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BROWSER_CDP_URL", "http://legacy-cdp:9222")

    from hermes_cli.config import load_config

    config = load_config()
    assert config["browser"]["camofox"]["managed_persistence"] is True
    assert browser_camofox.is_camofox_mode() is True
    assert browser_tool.check_browser_requirements() is True
    assert config_path.read_bytes() == original


def test_private_navigation_is_blocked_before_camofox():
    with (
        patch("tools.browser_tool._allow_private_urls", return_value=False),
        patch("tools.browser_tool._camofox.camofox_navigate") as navigate,
    ):
        result = json.loads(browser_tool.browser_navigate("http://127.0.0.1:8080/private"))

    assert result["success"] is False
    assert "private or internal" in result["error"]
    navigate.assert_not_called()


def test_metadata_navigation_stays_blocked_when_private_urls_are_allowed():
    with (
        patch("tools.browser_tool._allow_private_urls", return_value=True),
        patch("tools.browser_tool._camofox.camofox_navigate") as navigate,
    ):
        result = json.loads(browser_tool.browser_navigate("http://169.254.169.254/latest/meta-data/"))

    assert result["success"] is False
    assert "metadata" in result["error"]
    navigate.assert_not_called()


def test_registered_navigation_dispatches_authenticated_camofox_http(monkeypatch):
    monkeypatch.setenv("CAMOFOX_URL", "http://camofox.lan:9377")
    monkeypatch.setenv("CAMOFOX_API_KEY", "camofox-secret")
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "tabId": "tab-registered",
        "url": "https://example.com/",
        "title": "Example",
    }
    response.content = b"{}"
    response.raise_for_status = MagicMock()

    entry = registry.get_entry("browser_navigate")
    assert entry is not None
    with (
        patch("tools.browser_tool._track_activity", return_value="registered-nav"),
        patch("tools.browser_camofox.requests.post", return_value=response) as post,
        patch("tools.browser_camofox._get", return_value={"snapshot": "- heading \"Example\" [e1]", "refsCount": 1}),
        patch("tools.browser_camofox.get_vnc_url", return_value=None),
    ):
        result = json.loads(entry.handler({"url": "https://example.com"}, task_id="registered-nav"))

    assert result["success"] is True
    assert result["url"] == "https://example.com"
    assert post.call_args.args[0] == "http://camofox.lan:9377/tabs"
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer camofox-secret"}
    assert post.call_args.kwargs["json"]["url"] == "https://example.com"
