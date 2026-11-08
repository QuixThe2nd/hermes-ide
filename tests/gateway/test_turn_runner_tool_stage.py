"""The turn builder constructs the live runner with its tool-stage API.

#569 kept ``run_turn._run_agent_inner`` subscribing to
``turn_runner.tool_stage_event_callback`` and draining
``turn_runner.send_tool_stage_embeds()``. The constructed runner must therefore
be ``gateway.run_turn_runner.TurnRunner`` and carry both members.
"""

import asyncio

from gateway.config import Platform
from gateway.run_turn import GatewayTurnMixin
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource

USED_BY_RUN_AGENT_INNER = ("tool_stage_event_callback", "send_tool_stage_embeds")


def test_run_turn_builds_turn_runner_from_run_turn_runner():
    gateway = object.__new__(GatewayTurnMixin)
    gateway.adapters = {}
    gateway._delivery_adapter_for = lambda source: None
    gateway._run_still_current_fn = lambda session_key, run_generation: lambda: True
    display = GatewayTurnMixin._RunAgentDisplay(
        user_config={},
        platform_key="telegram",
        resolve_display_setting=lambda *args: False,
    )
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", user_id="user")

    async def build():
        return gateway._run_agent_build_turn_context(
            display,
            object,
            message="hello",
            source=source,
            session_key="agent:main:telegram:chat",
            run_generation=1,
        )

    _, turn_runner, _ = asyncio.run(build())
    assert type(turn_runner) is TurnRunner


def test_turn_runner_has_tool_stage_members_used_by_run_agent_inner():
    for name in USED_BY_RUN_AGENT_INNER:
        assert callable(getattr(TurnRunner, name, None)), f"TurnRunner is missing {name}"
