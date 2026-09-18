"""Auto-routed auxiliary calls on xAI OAuth refresh the grant after a 403 bad-credentials (#84845).

A plugin/hook ``call_llm`` with no provider override inherits the main ``xai-oauth`` route as
``resolved_provider == "auto"``; the refresh rung must still resolve the concrete backend from the
client's host so the expired bearer is refreshed and retried instead of benching the only grant.
"""

from agent.auxiliary_client import _auth_refresh_provider_for_route


def test_auto_route_on_xai_host_refreshes_xai_oauth():
    assert _auth_refresh_provider_for_route("auto", "https://api.x.ai/v1") == "xai-oauth"


def test_unknown_host_on_auto_route_stays_auto():
    assert _auth_refresh_provider_for_route("auto", "https://unknown.example.com/v1") == "auto"
