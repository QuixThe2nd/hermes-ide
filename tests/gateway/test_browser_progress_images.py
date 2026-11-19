"""Automatic browser screenshot progress delivery regressions."""

import asyncio
import hashlib
import io
import json
import queue
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image

from agent.moa_loop import current_tool_stage_subscriber_token, subscribe_tool_stage_events
from gateway.browser_progress import deliver_browser_progress, discard_pending_browser_progress
from gateway.turn_context import TurnContext
from hermes_constants import get_hermes_home, hermes_home_key
from tools import browser_camofox, browser_progress, browser_tool  # noqa: F401
from tools.registry import registry
from tools.vision_ocr import LocalOCRResult


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (640, 360), "white").save(output, format="PNG")
    return output.getvalue()


def _public_page_post(tab_id):
    def post(path, *args, **kwargs):
        if path.endswith("/evaluate"):
            return {"result": {"category": None}}
        return {"tabId": tab_id, "url": "https://example.com", "title": "Example"}
    return post


class _ImageAdapter:
    def __init__(self):
        self.images = []
        self.delivered = asyncio.Event()

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, metadata=None):
        with open(image_path, "rb") as handle:
            content = handle.read()
        self.images.append({
            "chat_id": chat_id,
            "content": content,
            "caption": caption,
            "reply_to": reply_to,
            "metadata": metadata,
        })
        self.delivered.set()
        return "image-message-1"


class _Gateway:
    def __init__(self, adapter):
        self.adapter = adapter

    def _adapter_for_source(self, source):
        return self.adapter

    def _delivery_adapter_for(self, source):
        return self.adapter


@pytest.mark.asyncio
async def test_navigation_delivers_native_image_before_turn_finishes(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config",
        lambda: {"browser": {"progress_images": True}},
    )
    monkeypatch.setattr(browser_camofox, "_post", _public_page_post("tab-1"))
    monkeypatch.setattr(browser_camofox, "_get", lambda *args, **kwargs: {
        "snapshot": '- heading "Example" [e1]', "refsCount": 1,
    })
    monkeypatch.setattr(browser_camofox, "_get_raw", lambda *args, **kwargs: SimpleNamespace(
        content=_png_bytes(),
    ))
    monkeypatch.setattr(browser_camofox, "get_vnc_url", lambda: None)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/tesseract" if name == "tesseract" else None)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args=args, returncode=0, stdout="Example documentation", stderr="",
    ))

    adapter = _ImageAdapter()
    current = {"value": True}
    ctx = TurnContext(
        source=SimpleNamespace(
            platform="discord", chat_id="parent-channel", thread_id="thread-1",
            message_id="message-1",
        ),
        session_id="session-browser-progress",
        _run_still_current=lambda: current["value"],
        stage_event_queue=queue.Queue(maxsize=32),
        _browser_progress_enabled=True,
        _progress_metadata={"thread_id": "thread-1"},
        _progress_reply_to="message-1",
    )
    from gateway.run_turn_runner import TurnRunner

    runner = TurnRunner(_Gateway(adapter), ctx)  # type: ignore[arg-type]
    unsubscribe = subscribe_tool_stage_events(
        ctx.session_id, runner.tool_stage_event_callback, browser_progress=True,
    )
    drain = asyncio.create_task(runner.send_tool_stage_embeds())
    turn_finished = False
    try:
        await asyncio.sleep(0)
        entry = registry.get_entry("browser_navigate")
        assert entry is not None
        raw_result = entry.handler(
            {"url": "https://example.com"},
            task_id="task-browser-progress",
            session_id=ctx.session_id,
            tool_call_id="call-browser-progress",
        )
        result = json.loads(raw_result)
        assert result["success"] is True
        assert "screenshot_path" not in result

        await asyncio.wait_for(adapter.delivered.wait(), timeout=0.5)
        assert turn_finished is False
        assert len(adapter.images) == 1
        sent = adapter.images[0]
        assert sent["content"].startswith(b"\x89PNG\r\n\x1a\n")
        assert sent["caption"] == "Browser update"
        assert sent["reply_to"] == "message-1"
        assert sent["metadata"]["thread_id"] == "thread-1"
        turn_finished = True
    finally:
        current["value"] = False
        ctx.stage_event_queue.put({"type": "test.stop"})
        await asyncio.wait_for(drain, timeout=2)
        unsubscribe()


def _make_event(label="event", session_id="session"):
    home = get_hermes_home().resolve()
    directory = home / "browser_screenshots"
    directory.mkdir(parents=True, exist_ok=True)
    content = _png_bytes()
    path = directory / f"browser_screenshot_{label}.png"
    path.write_bytes(content)
    return {
        "type": "browser.screenshot",
        "path": str(path),
        "profile_home": str(home),
        "profile_key": hermes_home_key(home),
        "session_id": session_id,
        "task_id": "task",
        "tool_call_id": "call",
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _direct_runner(adapter, *, platform="discord", session_id="session", thread_id="thread"):
    from gateway.run_turn_runner import TurnRunner

    current = {"value": True}
    ctx = TurnContext(
        source=SimpleNamespace(
            platform=platform, chat_id="source-chat", thread_id=thread_id,
            message_id="source-message",
        ),
        session_id=session_id,
        _run_still_current=lambda: current["value"],
        _browser_progress_enabled=True,
        _progress_metadata={"thread_id": thread_id},
        _progress_reply_to="source-message",
    )
    return TurnRunner(_Gateway(adapter), ctx), ctx, current  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_real_discord_attachment_uses_progress_thread(monkeypatch):
    from plugins.platforms.discord.adapter import DiscordAdapter
    from gateway.platforms.base import Platform

    event = _make_event("discord-native")
    adapter = object.__new__(DiscordAdapter)
    adapter.platform = Platform.DISCORD
    adapter._client = object()
    resolved = []
    sent = []

    class Channel:
        async def send(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(id="discord-image", attachments=[object()])

    channel = Channel()
    async def resolve(channel_id):
        resolved.append(channel_id)
        return channel

    async def allow_upload(*args, **kwargs):
        return None

    monkeypatch.setattr(adapter, "_resolve_channel", resolve)
    monkeypatch.setattr(adapter, "_reject_oversized_upload", allow_upload)
    monkeypatch.setattr(adapter, "_is_forum_parent", lambda value: False)
    runner, _, _ = _direct_runner(adapter, thread_id="discord-thread")
    await deliver_browser_progress(runner, event)
    assert resolved == ["discord-thread"]
    assert sent[0]["content"] == "Browser update"
    assert len(sent[0]["files"]) == 1


@pytest.mark.asyncio
async def test_real_matrix_image_upload_contains_thread_relation():
    from plugins.platforms.matrix.adapter import MatrixAdapter

    event = _make_event("matrix-native")
    sent = []

    class Client:
        crypto = None

        async def upload_media(self, data, **kwargs):
            return "mxc://local/browser-image"

        async def send_message_event(self, room_id, event_type, content):
            sent.append((str(room_id), content))
            return "$browser-image"

    adapter = object.__new__(MatrixAdapter)
    adapter._client = Client()
    adapter._encryption = False
    adapter._max_media_bytes = 10 * 1024 * 1024
    adapter._inbox_router = SimpleNamespace(child_reply_anchor=lambda room, reply: reply)
    runner, _, _ = _direct_runner(
        adapter, platform="matrix", thread_id="$thread-root",
    )
    await deliver_browser_progress(runner, event)
    room, content = sent[0]
    assert room == "source-chat"
    assert content["msgtype"] == "m.image"
    assert content["body"] == "Browser update"
    assert content["m.relates_to"] == {
        "m.in_reply_to": {"event_id": "source-message"},
        "rel_type": "m.thread",
        "event_id": "$thread-root",
        "is_falling_back": True,
    }


@pytest.mark.asyncio
async def test_discord_backed_matrix_relay_preserves_media_route(monkeypatch):
    from gateway.relay.adapter import RelayAdapter

    event = _make_event("matrix-relay")
    actions = []

    class MediaClient:
        async def upload(self, path, filename=None):
            return "https://relay.local/media/browser-image"

    async def gated(chat_id, action, surface_declines=False):
        actions.append(action)
        return {"success": True, "message_id": "relay-image"}

    adapter = object.__new__(RelayAdapter)
    adapter._transport = object()
    adapter.descriptor = SimpleNamespace(supports_op=lambda op: op == "send_media")
    adapter._media_client = MediaClient()
    monkeypatch.setattr(adapter, "_prepare_slack_egress", lambda chat, reply, metadata: reply)
    monkeypatch.setattr(adapter, "_with_scope", lambda chat, metadata: metadata)
    monkeypatch.setattr(adapter, "_gated_op", gated)
    runner, _, _ = _direct_runner(
        adapter, platform="matrix", thread_id="discord-thread-via-relay",
    )
    await deliver_browser_progress(runner, event)
    assert actions[0]["op"] == "send_media"
    assert actions[0]["chat_id"] == "source-chat"
    assert actions[0]["content"] == "Browser update"
    assert actions[0]["metadata"]["thread_id"] == "discord-thread-via-relay"
    assert actions[0]["reply_to"] == "source-message"


@pytest.mark.asyncio
async def test_same_session_profiles_do_not_cross_route_a_b_a(monkeypatch, tmp_path):
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    event = _make_event("profile-owned", session_id="identical-session")
    path = event["path"]
    adapter_a = _ImageAdapter()
    adapter_b = _ImageAdapter()
    runner_a, _, _ = _direct_runner(
        adapter_a, session_id="identical-session", thread_id="thread-a",
    )
    runner_b, _, _ = _direct_runner(
        adapter_b, session_id="identical-session", thread_id="thread-b",
    )

    monkeypatch.setenv("HERMES_HOME", str(home_b))
    await deliver_browser_progress(runner_b, event)
    assert adapter_b.images == []
    assert adapter_a.images == []
    assert get_hermes_home() != home_a
    from pathlib import Path
    assert Path(path).exists(), "wrong-profile consumer must not delete the artifact"

    monkeypatch.setenv("HERMES_HOME", str(home_a))
    await deliver_browser_progress(runner_a, event)
    assert adapter_b.images == []
    assert adapter_a.images[0]["metadata"]["thread_id"] == "thread-a"
    assert not Path(path).exists()


@pytest.mark.asyncio
async def test_wrong_turn_is_not_sent_and_stale_owned_artifact_is_cleaned():
    event = _make_event("wrong-turn", session_id="old-turn")
    adapter = _ImageAdapter()
    runner, ctx, current = _direct_runner(adapter, session_id="new-turn")
    from pathlib import Path
    path = Path(event["path"])

    await deliver_browser_progress(runner, event)
    assert adapter.images == []
    assert path.exists(), "another live turn may still own this artifact"

    ctx.session_id = "old-turn"
    current["value"] = False
    await deliver_browser_progress(runner, event)
    assert adapter.images == []
    assert not path.exists()


@pytest.mark.asyncio
async def test_adapter_failure_is_fail_soft_and_cleans_image():
    class BrokenAdapter(_ImageAdapter):
        async def send_image_file(self, *args, **kwargs):
            raise RuntimeError("transport failed")

    event = _make_event("send-failure")
    runner, _, _ = _direct_runner(BrokenAdapter())
    from pathlib import Path
    path = Path(event["path"])
    await deliver_browser_progress(runner, event)
    assert not path.exists()


def test_full_stage_queue_rejects_capture_and_deletes_artifact(monkeypatch):
    from gateway.run_turn_runner import TurnRunner

    browser_progress.clear_browser_progress_state(None)
    ctx = TurnContext(
        source=SimpleNamespace(platform="discord", chat_id="chat"),
        session_id="full-queue",
        _run_still_current=lambda: True,
        stage_event_queue=queue.Queue(maxsize=1),
        _browser_progress_enabled=True,
    )
    assert ctx.session_id is not None
    ctx.stage_event_queue.put({"type": "tool.stage", "invocation_id": "occupied"})
    runner = TurnRunner(_Gateway(_ImageAdapter()), ctx)  # type: ignore[arg-type]
    unsubscribe = subscribe_tool_stage_events(
        ctx.session_id, runner.tool_stage_event_callback, browser_progress=True,
    )
    token = current_tool_stage_subscriber_token(ctx.session_id)
    assert token is not None
    home = get_hermes_home().resolve()
    session_key = browser_camofox._session_key("full-queue-task", home_key=hermes_home_key(home))
    with browser_camofox._sessions_lock:
        browser_camofox._sessions[session_key] = {
            "tab_id": "tab", "user_id": "user", "home_key": hermes_home_key(home),
        }
    monkeypatch.setattr(browser_camofox, "_post", _public_page_post("tab"))
    monkeypatch.setattr(browser_camofox, "_snapshot_data", lambda *args, **kwargs: {
        "snapshot": "Public documentation",
    })
    monkeypatch.setattr(browser_camofox, "_get_raw", lambda *args, **kwargs: SimpleNamespace(
        content=_png_bytes(),
    ))
    monkeypatch.setattr(
        "tools.vision_ocr.run_local_ocr_checked",
        lambda *args, **kwargs: LocalOCRResult(True, "Public documentation"),
    )
    try:
        assert browser_progress.capture_browser_progress(
            "browser_navigate", '{"success":true}', task_id="full-queue-task",
            session_id=ctx.session_id, tool_call_id="call", subscriber_token=token,
        ) is False
        assert list((home / "browser_screenshots").glob("*.png")) == []
    finally:
        unsubscribe()
        browser_progress.clear_browser_progress_state(None)
        with browser_camofox._sessions_lock:
            browser_camofox._sessions.pop(session_key, None)


def test_cancelled_turn_discards_pending_owned_artifact():
    event = _make_event("cancelled-turn")
    pending = queue.Queue()
    pending.put(event)
    from pathlib import Path
    path = Path(event["path"])
    discard_pending_browser_progress(pending)
    assert pending.empty()
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("browser_config, expected_delivery", [({}, True), ({"progress_images": False}, False)])
async def test_production_turn_config_enables_immediate_native_delivery(monkeypatch, browser_config, expected_delivery):
    import importlib
    import sys
    import types

    from gateway.config import PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter, Platform, SendResult
    from gateway.session import SessionSource

    class TurnAdapter(BasePlatformAdapter):
        def __init__(self):
            super().__init__(PlatformConfig(enabled=True, token="***"), Platform.DISCORD)
            self.images = []

        async def connect(self, *, is_reconnect=False):
            return True

        async def disconnect(self):
            return None

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            return SendResult(success=True, message_id="text-message")

        async def send_tool_stage_embed(self, *args, **kwargs):
            return SendResult(success=True, message_id="stage-message")

        async def edit_tool_stage_embed(self, *args, **kwargs):
            return SendResult(success=True, message_id="stage-message")

        async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, metadata=None, **kwargs):
            with open(image_path, "rb") as handle:
                content = handle.read()
            self.images.append({
                "content": content, "caption": caption,
                "reply_to": reply_to, "metadata": metadata,
            })
            return SendResult(success=True, message_id="image-message")

        async def get_chat_info(self, chat_id):
            return {"id": chat_id}

    adapter = TurnAdapter()
    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.hooks = types.SimpleNamespace(loaded_hooks=False)
    runner.config = types.SimpleNamespace(
        thread_sessions_per_user=False, group_sessions_per_user=False, stt_enabled=False,
    )
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    config = {"browser": browser_config}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: config)
    monkeypatch.setattr(browser_camofox, "_post", _public_page_post("tab-full-turn"))
    monkeypatch.setattr(browser_camofox, "_get", lambda *args, **kwargs: {
        "snapshot": "Public documentation", "refsCount": 1,
    })
    monkeypatch.setattr(browser_camofox, "_get_raw", lambda *args, **kwargs: SimpleNamespace(
        content=_png_bytes(),
    ))
    monkeypatch.setattr(browser_camofox, "get_vnc_url", lambda: None)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args=args, returncode=0, stdout="Public documentation", stderr="",
    ))

    capture_calls = []
    real_capture = browser_progress.capture_browser_progress
    def capture_probe(*args, **kwargs):
        capture_calls.append(None)
        return real_capture(*args, **kwargs)
    monkeypatch.setattr(browser_progress, "capture_browser_progress", capture_probe)
    raw_results = []
    subscriber_tokens = []
    stage_subscriber_tokens = []

    class BrowserAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id")
            self.tools = []

        def run_conversation(self, message, conversation_history=None, task_id=None):
            stage_subscriber_tokens.append(current_tool_stage_subscriber_token(self.session_id))
            subscriber_tokens.append(browser_progress.browser_progress_subscriber_token(self.session_id))
            entry = registry.get_entry("browser_navigate")
            assert entry is not None
            raw_results.append(entry.handler(
                {"url": "https://example.com"}, task_id=task_id,
                session_id=self.session_id, tool_call_id="full-turn-call",
            ))
            # Return immediately after the final tool: no polling grace for the stage queue.
            return {"final_response": "done", "messages": [], "api_calls": 1}

    fake_run_agent = types.ModuleType("run_agent")
    setattr(fake_run_agent, "AIAgent", BrowserAgent)
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    source = SessionSource(
        platform=Platform.DISCORD, chat_id="parent-channel", chat_type="dm",
        thread_id="full-turn-thread", message_id="full-turn-message",
    )
    result = await runner._run_agent(
        message="browse", context_prompt="", history=[], source=source,
        session_id="full-turn-session", session_key="agent:main:discord:dm:parent-channel",
    )
    assert result["final_response"] == "done"
    assert json.loads(raw_results[0])["success"] is True
    assert stage_subscriber_tokens[0] is not None
    assert (subscriber_tokens[0] is not None) is expected_delivery
    assert len(capture_calls) == int(expected_delivery)
    assert len(adapter.images) == int(expected_delivery)
    if expected_delivery:
        assert adapter.images[0]["caption"] == "Browser update"
        assert adapter.images[0]["metadata"]["thread_id"] == "full-turn-thread"
