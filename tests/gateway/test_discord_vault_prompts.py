"""Discord save-login prompts through the real gateway worker and vault tool path."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext
from tests.gateway._plugin_adapter_loader import load_plugin_adapter


_ORIGIN = "https://example.test"
_PASSWORD = "synthetic-discord-vault-password"
_IDENTIFIER = "synthetic-user@example.test"
_REAL_TURN_TIMEOUT = 30


class _RecordingStore:
    def __init__(self):
        self.items = []

    def add_item(self, kind, label, payload, *, origin):
        self.items.append({
            "kind": kind,
            "label": label,
            "payload": dict(payload),
            "origin": origin,
        })
        return SimpleNamespace(id="vault_synthetic_login")


class _SentMessage:
    def __init__(self, channel, payload):
        self.id = 9000 + len(channel.sent)
        self.channel = channel
        self.payload = payload
        self.edits = []

    async def edit(self, **kwargs):
        self.payload.update(kwargs)
        self.edits.append(kwargs)
        return self


class _Channel:
    def __init__(self, channel_id=4242):
        self.id = channel_id
        self.sent = []
        self.prompt_available = asyncio.Event()
        self.interactions = []

    async def send(self, **kwargs):
        message = _SentMessage(self, kwargs)
        self.sent.append(message)
        self.prompt_available.set()
        return message


class _InteractionResponse:
    def __init__(self):
        self.messages = []
        self.modal = None
        self.modal_opened = asyncio.Event()

    async def send_modal(self, modal):
        self.modal = modal
        self.modal_opened.set()

    async def send_message(self, content, *, ephemeral=False, **kwargs):
        self.messages.append({"content": content, "ephemeral": ephemeral, **kwargs})


class _Interaction:
    def __init__(self, channel, user_id, *, message=None, custom_id="", client=None):
        self.user = SimpleNamespace(id=int(user_id), roles=[])
        self.channel = channel
        self.channel_id = channel.id
        self.message = message
        self.client = client if client is not None else getattr(channel, "client", None)
        self.data = {"custom_id": custom_id} if custom_id else {}
        self.response = _InteractionResponse()
        channel.interactions.append(self)


class _SaveLoginAgent:
    """One model turn whose tool call is dispatched by the production registry."""

    def __init__(self, observations=None):
        self.observations = observations if observations is not None else []

    def run_conversation(self, api_message, **kwargs):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from agent.vault_backends.unlock import get_save_login_prompt_callback
        from model_tools import handle_function_call
        from tools.thread_context import propagate_context_to_thread

        parent_thread = threading.get_ident()
        parent_callback = get_save_login_prompt_callback()

        def call_tool():
            child_thread = threading.get_ident()
            child_callback = get_save_login_prompt_callback()
            raw = handle_function_call(
                "browser_vault_save_login",
                {"label": "Example"},
                task_id=kwargs["task_id"],
            )
            return raw, child_thread, child_callback

        with ThreadPoolExecutor(max_workers=1) as executor:
            raw, child_thread, child_callback = executor.submit(
                propagate_context_to_thread(call_tool)
            ).result()
        self.observations.append({
            "parent_thread": parent_thread,
            "child_thread": child_thread,
            "parent_callback": parent_callback,
            "child_callback": child_callback,
        })
        return {
            "final_response": raw,
            "messages": [
                {"role": "user", "content": api_message},
                {"role": "assistant", "content": raw},
            ],
            "api_calls": 1,
        }


async def _close_client():
    return None


@pytest.fixture
def discord_vault_rig(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    discord = load_plugin_adapter("discord")
    adapter = discord.DiscordAdapter(PlatformConfig(enabled=True, extra={}))
    adapter._allowed_user_ids = {"777"}
    authorization_homes = []
    original_component_auth = discord._component_check_auth

    def record_component_auth(*args):
        from hermes_constants import get_hermes_home

        authorization_homes.append(str(get_hermes_home()))
        return original_component_auth(*args)

    monkeypatch.setattr(discord, "_component_check_auth", record_component_auth)
    runner = GatewayRunner(GatewayConfig())
    runner.adapters = {Platform.DISCORD: adapter}
    adapter.gateway_runner = runner
    monkeypatch.setattr(runner, "_is_session_incognito", lambda _source: False)

    source = adapter.build_source(
        chat_id="4242", chat_type="dm", user_id="777", user_name="Requesting User"
    )
    assert adapter._canonicalize(source) is not None
    session_key = adapter._source_session_key(source)
    channel = _Channel()

    async def resolve_channel(_chat_id):
        return channel

    monkeypatch.setattr(adapter, "_resolve_channel", resolve_channel)
    adapter._client = SimpleNamespace(
        add_view=lambda *_args, **_kwargs: None, close=_close_client
    )
    channel.client = adapter._client
    adapter.handle_message = _RejectIngestion()

    from tools import browser_vault_tool

    store = _RecordingStore()
    store.homes = []
    fills = []
    observations = []
    monkeypatch.setattr(
        browser_vault_tool,
        "_eval_js",
        lambda _task_id, _expression: {"success": True, "result": f"{_ORIGIN}/login"},
    )

    def get_store():
        from hermes_constants import get_hermes_home

        store.homes.append(str(get_hermes_home()))
        return store

    monkeypatch.setattr("agent.vault_store.get_vault_store", get_store)

    def record_fill(handle, task_id=None):
        fills.append({"handle": handle, "task_id": task_id})
        return json.dumps({"success": True, "filled_fields": 1, "origin": _ORIGIN})

    monkeypatch.setattr(browser_vault_tool, "browser_vault_fill", record_fill)
    ctx = TurnContext(
        source=source,
        session_key=session_key,
        session_id="discord-vault-session",
        message="Save this login",
        user_config={},
        _run_still_current=lambda: True,
    )
    yield SimpleNamespace(
        adapter=adapter,
        channel=channel,
        ctx=ctx,
        runner=runner,
        store=store,
        fills=fills,
        observations=observations,
        authorization_homes=authorization_homes,
        discord_module=discord,
        home=tmp_path / "home",
    )

    from agent.vault_backends.unlock import (
        set_code_prompt_callback,
        set_save_login_prompt_callback,
        set_unlock_prompt_callback,
    )

    set_save_login_prompt_callback(None)
    set_unlock_prompt_callback(None)
    set_code_prompt_callback(None)


class _RejectIngestion:
    def __init__(self):
        self.called = False

    async def __call__(self, _event):
        self.called = True
        raise AssertionError("a vault modal submission reached conversation ingestion")


async def _begin_prompt(
    rig, *, agent=None, ctx=None, profile_home=None, before_turn=None, after_turn=None
):
    ctx = ctx or rig.ctx
    ctx._loop_for_step = asyncio.get_running_loop()
    agent = agent or _SaveLoginAgent(rig.observations)

    def invoke():
        if before_turn is not None:
            before_turn()
        try:
            return TurnRunner(rig.runner, ctx)._run_conversation_with_session(
                agent, [], None, None, None
            )
        finally:
            if after_turn is not None:
                after_turn()

    def run_turn():
        if profile_home is None:
            return invoke()
        from gateway.run import _profile_runtime_scope

        with _profile_runtime_scope(profile_home):
            return invoke()

    rig.channel.prompt_available.clear()
    worker = asyncio.create_task(rig.runner._run_in_executor_with_context(run_turn))
    prompt_wait = asyncio.create_task(rig.channel.prompt_available.wait())
    done, _pending = await asyncio.wait(
        {worker, prompt_wait},
        timeout=_REAL_TURN_TIMEOUT,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if prompt_wait not in done:
        prompt_wait.cancel()
        if worker not in done:
            from plugins.platforms.discord.vault_prompts import close_all_vault_prompts

            close_all_vault_prompts(rig.adapter)
            await asyncio.wait_for(worker, timeout=_REAL_TURN_TIMEOUT)
        tool_result = worker.result()["final_response"]
        pytest.fail(
            f"Discord did not open a save-login prompt; real tool result: {tool_result}"
        )
    return worker, rig.channel.sent[-1]


async def _run_without_prompt(rig, *, agent=None, before_turn=None, after_turn=None):
    rig.ctx._loop_for_step = asyncio.get_running_loop()
    agent = agent or _SaveLoginAgent(rig.observations)

    def run_turn():
        if before_turn is not None:
            before_turn()
        try:
            return TurnRunner(rig.runner, rig.ctx)._run_conversation_with_session(
                agent, [], None, None, None
            )
        finally:
            if after_turn is not None:
                after_turn()

    worker = asyncio.create_task(rig.runner._run_in_executor_with_context(run_turn))
    return await asyncio.wait_for(worker, timeout=_REAL_TURN_TIMEOUT)


async def _open_modal(
    rig, prompt_message, *, user_id="777", channel=None, client=None, custom_id=None
):
    view = prompt_message.payload["view"]
    button = next(item for item in view.children if "open" in item.label.lower())
    interaction = _Interaction(
        channel or rig.channel,
        user_id,
        message=prompt_message,
        custom_id=button.custom_id if custom_id is None else custom_id,
        client=client,
    )
    await button.callback(interaction)
    return interaction, interaction.response.modal


def _populate_modal(
    modal, identifier=_IDENTIFIER, password=_PASSWORD, interaction=None
):
    modal.identifier._refresh_state(interaction, {"value": identifier})
    modal.password._refresh_state(interaction, {"value": password})


async def _submit_modal(
    rig,
    modal,
    prompt_message,
    *,
    identifier=_IDENTIFIER,
    password=_PASSWORD,
    user_id="777",
    channel=None,
    client=None,
    custom_id=None,
):
    interaction = _Interaction(
        channel or rig.channel,
        user_id,
        message=prompt_message,
        custom_id=modal.custom_id if custom_id is None else custom_id,
        client=client,
    )
    _populate_modal(modal, identifier, password, interaction)
    await modal.on_submit(interaction)
    return interaction


def _surface_text(rig, result, caplog):
    parts = [json.dumps(result, default=str), caplog.text]
    for message in rig.channel.sent:
        payload = message.payload
        parts.append(str(payload.get("content") or ""))
        embed = payload.get("embed")
        if embed is not None:
            parts.extend((str(embed.title), str(embed.description)))
        for edit in message.edits:
            parts.append(str(edit.get("content") or ""))
        view = payload.get("view")
        modal = getattr(getattr(view, "_request", None), "modal", None)
        if modal is not None:
            parts.extend((modal.identifier.value, modal.password.value))
    for interaction in rig.channel.interactions:
        parts.append(json.dumps(interaction.response.messages, default=str))
    return "\n".join(parts)


def _assert_prompt_registries_empty(rig):
    assert rig.adapter._vault_prompt_bindings == {}
    assert rig.adapter._vault_prompt_requests == {}
    assert rig.adapter._vault_prompt_sessions == {}


@pytest.mark.asyncio
async def test_discord_worker_save_login_modal_stores_and_fills_without_secret_leak(
    discord_vault_rig, caplog
):
    rig = discord_vault_rig
    worker, prompt_message = await _begin_prompt(rig)
    disclosure = str(prompt_message.payload.get("content", "")) + str(
        prompt_message.payload.get("embed").description
    )
    assert "unmasked" in disclosure.lower()
    assert "Discord processes" in disclosure
    assert "hermes vault add" in disclosure
    assert "Desktop → Settings → Passwords & Logins" in disclosure

    _open_interaction, modal = await _open_modal(rig, prompt_message)
    assert modal is not None
    modal_disclosure = (
        modal.title + " " + " ".join(item.label for item in modal.children)
    )
    assert "unmasked" in modal_disclosure.lower()
    assert "Discord" in modal_disclosure

    await _submit_modal(rig, modal, prompt_message)
    result = await asyncio.wait_for(worker, timeout=_REAL_TURN_TIMEOUT)
    payload = json.loads(result["final_response"])

    assert payload["success"] is True
    assert payload["origin"] == _ORIGIN
    assert rig.store.items[0]["origin"] == _ORIGIN
    assert rig.store.items[0]["payload"]["password"] == _PASSWORD
    assert rig.store.items[0]["payload"]["identifier"] == _IDENTIFIER
    assert rig.fills == [
        {"handle": "vault_synthetic_login", "task_id": "discord-vault-session"}
    ]
    assert rig.adapter.handle_message.called is False
    assert modal.password.value == ""
    assert modal.identifier.value == ""
    assert rig.observations[0]["parent_thread"] != rig.observations[0]["child_thread"]
    assert rig.observations[0]["parent_callback"] is not None
    assert rig.observations[0]["child_callback"] is not None
    _assert_prompt_registries_empty(rig)
    assert _PASSWORD not in _surface_text(rig, result, caplog)


@pytest.mark.asyncio
async def test_discord_vault_prompt_denies_wrong_owners_transport_and_replay(
    discord_vault_rig, caplog
):
    rig = discord_vault_rig
    rig.adapter._allowed_user_ids.add(
        "888"
    )  # admitted to Discord, but not this request's owner
    worker, prompt_message = await _begin_prompt(rig)
    wrong_channel = _Channel(9999)
    wrong_channel.client = rig.adapter._client

    for kwargs in (
        {"user_id": "888"},
        {"channel": wrong_channel},
        {"client": SimpleNamespace()},
        {"custom_id": "vault-save-open:forged"},
    ):
        interaction, modal = await _open_modal(rig, prompt_message, **kwargs)
        assert modal is None
        assert interaction.response.messages
        assert not worker.done()

    _opened, modal = await _open_modal(rig, prompt_message)
    assert modal is not None
    repeated_open, repeated_modal = await _open_modal(rig, prompt_message)
    assert repeated_modal is None
    assert "already opened" in repeated_open.response.messages[-1]["content"]
    attacker_password = _PASSWORD + "-attacker"
    attacker_submit = await _submit_modal(
        rig, modal, prompt_message, user_id="888", password=attacker_password
    )
    assert "not owned" in attacker_submit.response.messages[-1]["content"]
    assert not worker.done()
    assert rig.store.items == []
    await _submit_modal(rig, modal, prompt_message)
    result = await asyncio.wait_for(worker, timeout=_REAL_TURN_TIMEOUT)
    assert json.loads(result["final_response"])["success"] is True
    assert len(rig.store.items) == 1

    replay_password = _PASSWORD + "-replay"
    replay = await _submit_modal(rig, modal, prompt_message, password=replay_password)
    reopened, replay_modal = await _open_modal(rig, prompt_message)
    assert replay_modal is None
    assert "closed" in reopened.response.messages[-1]["content"]
    assert "already closed" in replay.response.messages[-1]["content"]
    assert len(rig.store.items) == 1
    assert modal.identifier.value == modal.password.value == ""
    assert rig.adapter.handle_message.called is False
    _assert_prompt_registries_empty(rig)
    exposed = _surface_text(rig, result, caplog)
    assert _PASSWORD not in exposed
    assert replay_password not in exposed
    assert attacker_password not in exposed


@pytest.mark.asyncio
async def test_discord_vault_cancel_wakes_worker_and_clears_modal(
    discord_vault_rig, caplog
):
    rig = discord_vault_rig
    worker, prompt_message = await _begin_prompt(rig)
    _opened, modal = await _open_modal(rig, prompt_message)
    _populate_modal(modal)
    view = prompt_message.payload["view"]
    cancel = next(item for item in view.children if item.label == "Cancel")
    interaction = _Interaction(
        rig.channel, "777", message=prompt_message, custom_id=cancel.custom_id
    )
    await cancel.callback(interaction)
    result = await asyncio.wait_for(worker, timeout=_REAL_TURN_TIMEOUT)

    payload = json.loads(result["final_response"])
    assert payload["success"] is False
    assert payload["error_type"] == "save_declined"
    assert rig.store.items == []
    assert rig.fills == []
    assert modal.identifier.value == modal.password.value == ""
    assert modal._request.state.value == "cancelled"
    _assert_prompt_registries_empty(rig)
    assert _PASSWORD not in _surface_text(rig, result, caplog)


@pytest.mark.asyncio
async def test_discord_vault_scope_restores_previous_surface_callbacks(
    discord_vault_rig,
):
    from agent.vault_backends.unlock import (
        get_code_prompt_callback,
        get_save_login_prompt_callback,
        get_unlock_prompt_callback,
        set_code_prompt_callback,
        set_save_login_prompt_callback,
        set_unlock_prompt_callback,
    )

    def previous_save(_origin, _host):
        raise AssertionError(
            "the previous surface callback must not handle this Discord turn"
        )

    def previous_unlock(_backend, _display_name):
        raise AssertionError("unlock prompting must stay suppressed")

    def previous_code(_site, _hint):
        raise AssertionError("OTP prompting must stay suppressed")

    restored = {}

    def install_previous():
        set_save_login_prompt_callback(previous_save)
        set_unlock_prompt_callback(previous_unlock)
        set_code_prompt_callback(previous_code)

    def capture_restored():
        restored.update(
            save=get_save_login_prompt_callback(),
            unlock=get_unlock_prompt_callback(),
            code=get_code_prompt_callback(),
        )
        set_save_login_prompt_callback(None)
        set_unlock_prompt_callback(None)
        set_code_prompt_callback(None)

    rig = discord_vault_rig
    worker, prompt_message = await _begin_prompt(
        rig, before_turn=install_previous, after_turn=capture_restored
    )
    _opened, modal = await _open_modal(rig, prompt_message)
    view = prompt_message.payload["view"]
    cancel = next(item for item in view.children if item.label == "Cancel")
    interaction = _Interaction(
        rig.channel, "777", message=prompt_message, custom_id=cancel.custom_id
    )
    await cancel.callback(interaction)
    result = await asyncio.wait_for(worker, timeout=_REAL_TURN_TIMEOUT)

    assert json.loads(result["final_response"])["error_type"] == "save_declined"
    assert restored == {
        "save": previous_save,
        "unlock": previous_unlock,
        "code": previous_code,
    }
    assert modal.identifier.value == modal.password.value == ""
    _assert_prompt_registries_empty(rig)


@pytest.mark.asyncio
async def test_discord_vault_timeout_expires_abandoned_modal(
    discord_vault_rig, monkeypatch, caplog
):
    rig = discord_vault_rig
    monkeypatch.setattr(
        "plugins.platforms.discord.vault_prompts._VAULT_PROMPT_TIMEOUT_SECONDS", 0.05
    )
    worker, prompt_message = await _begin_prompt(rig)
    _opened, modal = await _open_modal(rig, prompt_message)
    _populate_modal(modal)
    result = await asyncio.wait_for(worker, timeout=_REAL_TURN_TIMEOUT)

    payload = json.loads(result["final_response"])
    assert payload["error_type"] == "save_declined"
    assert modal._request.state.value == "expired"
    assert modal.identifier.value == modal.password.value == ""
    assert rig.store.items == []
    _assert_prompt_registries_empty(rig)
    assert _PASSWORD not in _surface_text(rig, result, caplog)


@pytest.mark.asyncio
async def test_discord_vault_disconnect_wakes_worker_before_client_close(
    discord_vault_rig, caplog
):
    rig = discord_vault_rig
    worker, prompt_message = await _begin_prompt(rig)
    _opened, modal = await _open_modal(rig, prompt_message)
    _populate_modal(modal)
    await rig.adapter.disconnect()
    result = await asyncio.wait_for(worker, timeout=_REAL_TURN_TIMEOUT)

    assert json.loads(result["final_response"])["error_type"] == "save_declined"
    assert modal._request.state.value == "disconnected"
    assert modal.identifier.value == modal.password.value == ""
    assert rig.store.items == []
    _assert_prompt_registries_empty(rig)
    assert _PASSWORD not in _surface_text(rig, result, caplog)


@pytest.mark.asyncio
async def test_discord_vault_storage_error_is_generic_and_does_not_fill(
    discord_vault_rig, monkeypatch, caplog
):
    rig = discord_vault_rig

    def fail_store(*_args, **_kwargs):
        raise RuntimeError(_PASSWORD)

    monkeypatch.setattr(rig.store, "add_item", fail_store)
    worker, prompt_message = await _begin_prompt(rig)
    _opened, modal = await _open_modal(rig, prompt_message)
    await _submit_modal(rig, modal, prompt_message)
    result = await asyncio.wait_for(worker, timeout=_REAL_TURN_TIMEOUT)

    payload = json.loads(result["final_response"])
    assert payload["success"] is False
    assert payload["error_type"] == "save_failed"
    assert rig.fills == []
    assert modal.identifier.value == modal.password.value == ""
    _assert_prompt_registries_empty(rig)
    assert _PASSWORD not in _surface_text(rig, result, caplog)


@pytest.mark.asyncio
async def test_discord_vault_send_failure_closes_binding(
    discord_vault_rig, monkeypatch
):
    rig = discord_vault_rig

    async def fail_send(**_kwargs):
        raise RuntimeError("synthetic Discord send failure")

    monkeypatch.setattr(rig.channel, "send", fail_send)
    result = await _run_without_prompt(rig)
    payload = json.loads(result["final_response"])
    assert payload["success"] is False
    assert payload["error_type"] == "save_declined"
    assert rig.store.items == []
    assert rig.channel.sent == []
    _assert_prompt_registries_empty(rig)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "cron_session"), [("discord", True), ("api_server", False)]
)
async def test_discord_vault_save_never_prompts_unattended_sessions(
    discord_vault_rig, platform, cron_session
):
    from gateway.session_context import clear_session_vars, set_session_vars

    rig = discord_vault_rig
    tokens = set_session_vars(platform=platform, cron_session=cron_session)
    try:
        result = await _run_without_prompt(rig)
    finally:
        clear_session_vars(tokens)
    payload = json.loads(result["final_response"])
    assert payload["success"] is False
    assert payload["error_type"] == "prompt_unavailable"
    assert rig.channel.sent == []
    assert rig.store.items == []
    _assert_prompt_registries_empty(rig)


@pytest.mark.asyncio
async def test_discord_turn_leaves_unlock_and_otp_unavailable(
    discord_vault_rig, monkeypatch
):
    rig = discord_vault_rig
    locked_backend = SimpleNamespace(
        name="onepassword",
        display_name="Locked test backend",
        needs_unlock=True,
        is_unlocked=lambda: False,
    )
    monkeypatch.setattr(
        "agent.vault_backends.enabled_backends", lambda: [locked_backend]
    )
    monkeypatch.setattr(
        "agent.vault_login_classifier.classify_otp_controls",
        lambda _controls: [SimpleNamespace()],
    )

    class SiblingProbeAgent:
        def run_conversation(self, api_message, **kwargs):
            from agent.vault_backends.unlock import (
                get_code_prompt_callback,
                get_save_login_prompt_callback,
                get_unlock_prompt_callback,
            )
            from model_tools import handle_function_call

            callbacks = {
                "save": get_save_login_prompt_callback() is not None,
                "unlock": get_unlock_prompt_callback() is not None,
                "code": get_code_prompt_callback() is not None,
            }
            unlock = json.loads(
                handle_function_call(
                    "browser_vault_unlock",
                    {"backend": "onepassword"},
                    task_id=kwargs["task_id"],
                )
            )
            code = json.loads(
                handle_function_call(
                    "browser_vault_enter_code",
                    {"handle": ""},
                    task_id=kwargs["task_id"],
                )
            )
            raw = json.dumps({"callbacks": callbacks, "unlock": unlock, "code": code})
            return {
                "final_response": raw,
                "messages": [
                    {"role": "user", "content": api_message},
                    {"role": "assistant", "content": raw},
                ],
                "api_calls": 1,
            }

    result = await _run_without_prompt(rig, agent=SiblingProbeAgent())
    payload = json.loads(result["final_response"])
    assert payload["callbacks"] == {"save": True, "unlock": False, "code": False}
    assert payload["unlock"].get("error_type") == "unlock_unavailable", payload
    assert "unsupported transport" in payload["unlock"]["error"]
    assert payload["code"]["error_type"] == "prompt_unavailable"
    assert "unsupported transport" in payload["code"]["error"]
    assert rig.channel.sent == []
    _assert_prompt_registries_empty(rig)


@pytest.mark.asyncio
async def test_discord_vault_source_session_mismatch_fails_closed(discord_vault_rig):
    from agent.vault_backends.unlock import set_save_login_prompt_callback

    def foreign_save(_origin, _host):
        raise AssertionError(
            "a mismatched Discord source reached another surface's callback"
        )

    rig = discord_vault_rig
    rig.ctx.session_key = "agent:main:discord:dm:not-the-source"
    result = await _run_without_prompt(
        rig,
        before_turn=lambda: set_save_login_prompt_callback(foreign_save),
        after_turn=lambda: set_save_login_prompt_callback(None),
    )
    assert json.loads(result["final_response"])["error_type"] == "prompt_unavailable"
    assert rig.channel.sent == []
    _assert_prompt_registries_empty(rig)


@pytest.mark.asyncio
async def test_discord_vault_modal_error_clears_answer_and_wakes_worker(
    discord_vault_rig, monkeypatch, caplog
):
    rig = discord_vault_rig
    worker, prompt_message = await _begin_prompt(rig)
    binding = next(iter(rig.adapter._vault_prompt_bindings.values()))
    original_finish = binding.finish

    def fail_submission(request, state, **kwargs):
        if state.value == "submitted":
            raise RuntimeError(_PASSWORD)
        return original_finish(request, state, **kwargs)

    monkeypatch.setattr(binding, "finish", fail_submission)
    _opened, modal = await _open_modal(rig, prompt_message)
    await _submit_modal(rig, modal, prompt_message)
    result = await asyncio.wait_for(worker, timeout=_REAL_TURN_TIMEOUT)

    assert json.loads(result["final_response"])["error_type"] == "save_declined"
    assert modal._request.state.value == "failed"
    assert modal.identifier.value == modal.password.value == ""
    assert rig.store.items == []
    _assert_prompt_registries_empty(rig)
    assert _PASSWORD not in _surface_text(rig, result, caplog)


@pytest.mark.asyncio
async def test_discord_vault_shared_transport_profile_isolation_a_b_a(
    discord_vault_rig, monkeypatch, caplog
):
    from unittest.mock import patch

    from gateway.profile_routing import parse_profile_routes
    from gateway.session_identity import identity_of

    rig = discord_vault_rig
    ops_home = rig.home / "profiles" / "ops"
    ops_home.mkdir(parents=True)
    rig.adapter._allowed_user_ids.add("888")
    rig.runner.config.multiplex_profiles = True
    rig.runner.config.profile_routes = parse_profile_routes([
        {
            "name": "ops-user",
            "platform": "discord",
            "profile": "ops",
            "user_id": "888",
        }
    ])
    rig.runner._primary_profile_name = "default"
    rig.runner._profile_adapters = {"ops": {}}
    served = [("default", rig.home), ("ops", ops_home)]
    passwords = [_PASSWORD + "-a1", _PASSWORD + "-b", _PASSWORD + "-a2"]
    expected_homes = [str(rig.home), str(ops_home), str(rig.home)]
    expected_profiles = ["default", "ops", "default"]
    expected_users = ["777", "888", "777"]
    owners = []
    session_keys = []

    with (
        patch("hermes_cli.profiles.profiles_to_serve", return_value=served),
        patch(
            "hermes_cli.profiles.get_profile_dir",
            side_effect=lambda name: rig.home if name == "default" else ops_home,
        ),
        patch("hermes_cli.profiles.profile_exists", return_value=True),
    ):
        for index, (user_id, profile) in enumerate(
            zip(expected_users, expected_profiles)
        ):
            source = rig.adapter.build_source(
                chat_id="4242",
                chat_type="dm",
                user_id=user_id,
                user_name="Requesting User",
            )
            session_key = rig.adapter._source_session_key(source)
            identity = identity_of(source)
            assert identity is not None
            assert identity.runtime_profile == profile
            assert identity.transport_profile == "default"
            assert rig.runner._delivery_adapter_for(source) is rig.adapter
            ctx = TurnContext(
                source=source,
                session_key=session_key,
                session_id=f"discord-vault-{index}",
                message="Save this login",
                user_config={},
                _run_still_current=lambda: True,
            )
            worker, prompt_message = await _begin_prompt(
                rig, ctx=ctx, profile_home=identity.runtime_home
            )
            _opened, modal = await _open_modal(rig, prompt_message, user_id=user_id)
            owners.append(modal._request.owner)
            session_keys.append(session_key)
            await _submit_modal(
                rig,
                modal,
                prompt_message,
                user_id=user_id,
                identifier=f"user-{index}@example.test",
                password=passwords[index],
            )
            result = await asyncio.wait_for(worker, timeout=_REAL_TURN_TIMEOUT)
            assert json.loads(result["final_response"])["success"] is True
            _assert_prompt_registries_empty(rig)

    assert [owner.runtime_profile for owner in owners] == expected_profiles
    assert [str(owner.runtime_home) for owner in owners] == expected_homes
    assert [owner.transport_profile for owner in owners] == ["default"] * 3
    assert [str(owner.authorization_home) for owner in owners] == [str(rig.home)] * 3
    assert [owner.user_id for owner in owners] == expected_users
    assert [owner.channel_id for owner in owners] == ["4242"] * 3
    assert [owner.session_key for owner in owners] == session_keys
    assert session_keys[0] == session_keys[2]
    assert session_keys[0] != session_keys[1]
    assert rig.store.homes == expected_homes
    assert rig.authorization_homes == [str(rig.home)] * 6
    assert len(rig.store.items) == 3
    assert rig.adapter.handle_message.called is False
    exposed = _surface_text(rig, {"turns": len(owners)}, caplog)
    for password in passwords:
        assert password not in exposed


@pytest.mark.asyncio
async def test_discord_vault_authorization_error_clears_pending_modal(
    discord_vault_rig, monkeypatch, caplog
):
    rig = discord_vault_rig
    worker, prompt_message = await _begin_prompt(rig)
    _opened, modal = await _open_modal(rig, prompt_message)
    _populate_modal(modal)
    binding = modal._request.binding

    def fail_authorization(_interaction):
        raise RuntimeError(_PASSWORD)

    monkeypatch.setattr(binding, "_authorize", fail_authorization)
    view = prompt_message.payload["view"]
    cancel = next(item for item in view.children if item.label == "Cancel")
    interaction = _Interaction(
        rig.channel, "777", message=prompt_message, custom_id=cancel.custom_id
    )
    await cancel.callback(interaction)
    result = await asyncio.wait_for(worker, timeout=5)

    assert json.loads(result["final_response"])["error_type"] == "save_declined"
    assert modal._request.state.value == "failed"
    assert modal.identifier.value == modal.password.value == ""
    assert rig.store.items == []
    _assert_prompt_registries_empty(rig)
    assert _PASSWORD not in _surface_text(rig, result, caplog)


@pytest.mark.asyncio
async def test_discord_vault_prompt_uses_status_thread_not_parent_channel(
    discord_vault_rig, monkeypatch, caplog
):
    rig = discord_vault_rig
    rig.ctx._status_chat_id = "4242"
    rig.ctx._status_thread_metadata = {"thread_id": "5252"}
    thread = _Channel(5252)
    thread.client = rig.adapter._client
    rig.channel = thread

    async def resolve_thread(chat_id):
        assert str(chat_id) == "5252"
        return thread

    monkeypatch.setattr(rig.adapter, "_resolve_channel", resolve_thread)
    worker, prompt_message = await _begin_prompt(rig)
    parent = _Channel(4242)
    parent.client = rig.adapter._client
    denied, parent_modal = await _open_modal(rig, prompt_message, channel=parent)
    assert parent_modal is None
    assert denied.response.messages

    _opened, modal = await _open_modal(rig, prompt_message)
    assert modal._request.owner.channel_id == "5252"
    await _submit_modal(rig, modal, prompt_message)
    result = await asyncio.wait_for(worker, timeout=5)
    assert json.loads(result["final_response"])["success"] is True
    assert prompt_message.channel is thread
    _assert_prompt_registries_empty(rig)
    assert _PASSWORD not in _surface_text(rig, result, caplog)
