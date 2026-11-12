"""Contract tests for pass-through MCP elicitation handling."""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp.types")

from mcp.types import ElicitResult  # noqa: E402

from tools.mcp_tool_sampling import ElicitationHandler  # noqa: E402


def _params(mode="form"):
    return SimpleNamespace(
        mode=mode,
        message="please confirm",
        requested_schema={"properties": {"approved": {"type": "boolean"}}},
        url="https://example.com/auth" if mode == "url" else None,
        elicitation_id="e1",
    )


def test_form_elicitation_accepts_without_a_human_callback():
    handler = ElicitationHandler("pay")

    result = asyncio.run(handler(context=None, params=_params()))

    assert isinstance(result, ElicitResult)
    assert result.action == "accept"
    assert result.content == {}
    assert handler.metrics == {"requests": 1, "accepted": 1, "declined": 0, "errors": 0}


def test_url_elicitation_remains_unsupported_and_declines():
    handler = ElicitationHandler("pay")

    result = asyncio.run(handler(context=None, params=_params(mode="url")))

    assert result.action == "decline"
    assert handler.metrics == {"requests": 1, "accepted": 0, "declined": 1, "errors": 0}


@pytest.mark.usefixtures("require_mcp_2_sdk")
def test_real_sdk_form_params_accept():
    from mcp.types import ElicitRequestFormParams

    params = ElicitRequestFormParams(
        message="authorize a payment",
        requested_schema={"type": "object", "properties": {"card_number": {"type": "string"}}},
    )
    result = asyncio.run(ElicitationHandler("pay")(context=None, params=params))

    assert result.action == "accept"
    assert result.content == {}


def test_session_kwargs_exposes_elicitation_callback():
    handler = ElicitationHandler("pay")

    assert handler.session_kwargs() == {"elicitation_callback": handler}
