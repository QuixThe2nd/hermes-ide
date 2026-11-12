"""Opt-in ``restart_hub`` — the SUCCESSOR gateway bounces hermes-discord-hub.service.

Contract:
* every surface defaults to false (tool schema, ``/restart``, control-socket
  params) — default behaviour is unchanged;
* ``queue_user_restart(..., restart_hub=True)`` persists
  ``.restart_hub_pending.json`` BEFORE ``request_restart()`` (never on the
  already_in_progress / chained paths) and forwards no new
  ``request_restart`` kwargs — the file write IS the hand-off;
* the successor consumes the marker one-shot and bounces the unit via an
  injected subprocess runner (mocked systemctl here), never raising;
* the restart-result parser still accepts the exact 3/4-line shapes and also
  those shapes plus one trailing ``Hub: `` line, and the lifecycle guard does
  NOT classify the hub unit's restart as a gateway lifecycle command.
"""

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent, MessageType
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source

HUB_UNIT = "hermes-discord-hub.service"
PENDING_NAME = ".restart_hub_pending.json"
RESULT_NAME = ".restart_hub_result.json"
NOTICE_TEXT = "♻ Gateway restarted successfully. Your session continues."


@pytest.fixture(autouse=True)
def _reset_bounce_state():
    """The successor-side bounce keeps one in-process verification state."""
    import gateway.restart_hub as restart_hub

    restart_hub._bounce.clear()
    yield
    restart_hub._bounce.clear()


# ── (a) tool schema + payload parse ──────────────────────────────────────────


def test_restart_tool_schema_has_opt_in_restart_hub_property():
    from plugins.gateway_restart.tool import RESTART_SCHEMA

    parameters = RESTART_SCHEMA["parameters"]
    assert parameters["additionalProperties"] is False
    prop = parameters["properties"]["restart_hub"]
    assert prop["type"] == "boolean"
    description = prop["description"]
    assert HUB_UNIT in description
    assert "successor" in description


class _LoopThread:
    """A real event loop running in a worker thread, like the gateway loop."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5.0)
        self.loop.close()


@pytest.fixture
def loop_thread():
    lt = _LoopThread()
    yield lt
    lt.stop()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("__missing__", False),
        (False, False),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        (True, True),
        ("true", True),
        ("1", True),
        ("yes", True),
        ("on", True),
    ],
)
def test_restart_tool_parses_restart_hub_flag(loop_thread, monkeypatch, value, expected):
    runner, _adapter = make_restart_runner()
    runner._gateway_loop = loop_thread.loop
    queued = AsyncMock(
        return_value={"status": "restarting", "active_agents": 0, "via_service": False}
    )
    monkeypatch.setattr("gateway.restart.queue_user_restart", queued)
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    from plugins.gateway_restart.tool import handle_restart

    args = {} if value == "__missing__" else {"restart_hub": value}
    payload = json.loads(handle_restart(dict(args)))

    assert payload["success"] is True
    assert queued.await_args.kwargs.get("restart_hub") is expected


def test_restart_tool_control_socket_path_passes_restart_hub(monkeypatch, tmp_path):
    """Worker-process path: the flag rides the control-socket request kwargs."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: None)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    captured: dict = {}

    def _fake_request(home, **kwargs):
        captured.update(kwargs)
        return {"queued": True, "status": "restarting", "pid": 4242}

    monkeypatch.setattr(
        "gateway.control_socket.request_gateway_restart", _fake_request
    )
    from plugins.gateway_restart.tool import handle_restart

    payload = json.loads(handle_restart({"restart_hub": "true"}))

    assert payload["success"] is True
    assert captured["restart_hub"] is True
    # default (omitted) stays false on the socket path too
    captured.clear()
    json.loads(handle_restart({}))
    assert captured["restart_hub"] is False


# ── (b) slash: /restart --hub ────────────────────────────────────────────────


async def _slash_restart(monkeypatch, tmp_path, text):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    queued = AsyncMock(
        return_value={"status": "restarting", "active_agents": 0, "via_service": False}
    )
    monkeypatch.setattr("gateway.restart.queue_user_restart", queued)
    runner, _adapter = make_restart_runner()
    event = MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=make_restart_source(),
        message_id="m1",
        platform_update_id=None,
    )
    await runner._handle_restart_command(event)
    return queued


@pytest.mark.asyncio
async def test_slash_restart_hub_flag_sets_restart_hub(monkeypatch, tmp_path):
    queued = await _slash_restart(monkeypatch, tmp_path, "/restart --hub")
    assert queued.await_args.kwargs.get("restart_hub") is True


@pytest.mark.asyncio
async def test_slash_restart_hub_flag_alone_among_args(monkeypatch, tmp_path):
    queued = await _slash_restart(monkeypatch, tmp_path, "/restart --hub now please")
    assert queued.await_args.kwargs.get("restart_hub") is True


@pytest.mark.asyncio
async def test_slash_restart_bare_and_unrelated_args_stay_false(monkeypatch, tmp_path):
    queued = await _slash_restart(monkeypatch, tmp_path, "/restart")
    assert queued.await_args.kwargs.get("restart_hub") is False
    queued = await _slash_restart(monkeypatch, tmp_path, "/restart check updates")
    assert queued.await_args.kwargs.get("restart_hub") is False
    # a bare word must not act as the flag
    queued = await _slash_restart(monkeypatch, tmp_path, "/restart hub")
    assert queued.await_args.kwargs.get("restart_hub") is False


# ── (c) control-socket params ────────────────────────────────────────────────


def test_request_gateway_restart_params_carry_restart_hub(monkeypatch):
    import gateway.control_socket as control_socket

    captured: dict = {}

    def _fake_query(home, verb, *, params=None, timeout=None):
        captured["verb"] = verb
        captured["params"] = params
        return {"queued": True, "status": "restarting", "pid": 1}

    monkeypatch.setattr(control_socket, "query_gateway_control", _fake_query)
    ack = control_socket.request_gateway_restart(Path("/tmp/home"), restart_hub=True)
    assert ack["queued"] is True
    assert captured["params"]["restart_hub"] is True
    # Old clients / default calls must not bounce: the key is present, false.
    control_socket.request_gateway_restart(Path("/tmp/home"))
    assert captured["params"]["restart_hub"] is False


# ── queue_user_restart: pending file is the hand-off ─────────────────────────


@pytest.fixture
def queueable_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        "gateway.restart_preflight.warn_before_user_restart", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("gateway.restart.user_restart_via_service", lambda: False)
    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)
    return runner, tmp_path


@pytest.mark.asyncio
async def test_queue_user_restart_true_writes_pending_before_request_restart(
    queueable_runner,
):
    runner, home = queueable_runner
    observed: dict = {}

    def _capture(**kwargs):
        observed["pending_at_request"] = (home / PENDING_NAME).exists()
        return True

    runner.request_restart = MagicMock(side_effect=_capture)
    status = await _queue(runner, restart_hub=True)

    assert status["status"] == "restarting"
    assert observed["pending_at_request"] is True
    payload = json.loads((home / PENDING_NAME).read_text(encoding="utf-8"))
    assert payload["restart_hub"] is True
    assert isinstance(payload["requested_at"], float)
    # restart_hub never becomes a request_restart kwarg — the file is the hand-off.
    runner.request_restart.assert_called_once_with(
        detached=True, via_service=False, restart_platforms=False
    )


@pytest.mark.asyncio
async def test_queue_user_restart_false_writes_and_deletes_nothing(queueable_runner):
    from gateway.restart_hub import write_hub_pending

    runner, home = queueable_runner
    await _queue(runner)
    assert not (home / PENDING_NAME).exists()

    # An existing pending marker survives a plain restart request untouched.
    write_hub_pending(home)
    await _queue(runner, message_id="m2")
    assert (home / PENDING_NAME).exists()


@pytest.mark.asyncio
async def test_queue_user_restart_already_in_progress_writes_no_pending(
    queueable_runner,
):
    runner, home = queueable_runner
    runner._restart_requested = True
    status = await _queue(runner, restart_hub=True)
    assert status["status"] == "already_in_progress"
    assert not (home / PENDING_NAME).exists()
    runner.request_restart.assert_not_called()


@pytest.mark.asyncio
async def test_queue_user_restart_chained_records_flag_on_chain_marker(
    queueable_runner,
):
    """Chained: the dying process stores the flag on the chain payload, never a
    hub marker of its own — the already-up successor re-hands it to the NEXT one."""
    runner, home = queueable_runner
    runner._draining = True
    runner._restart_requested = True
    runner._restart_task_started = True
    runner._overlap_restart_successor_started = True
    runner._overlap_restart_successor_pid = 202

    status = await _queue(runner, restart_hub=True)

    assert status["status"] == "chained"
    chain = json.loads((home / ".restart_chain.json").read_text(encoding="utf-8"))
    assert chain["restart_hub"] is True
    assert not (home / PENDING_NAME).exists()
    runner.request_restart.assert_not_called()


async def _queue(runner, *, restart_hub=False, message_id="m1"):
    from gateway.restart import queue_user_restart

    return await queue_user_restart(
        runner, make_restart_source(), message_id,
        restart_platforms=False, restart_hub=restart_hub,
    )


# ── chained restart: the already-up successor re-queues the bounce ───────────


def _draining_runner():
    runner, _adapter = make_restart_runner()
    runner._draining = True
    runner._restart_requested = True
    runner._restart_task_started = True
    runner._overlap_restart_successor_started = True
    runner._overlap_restart_successor_pid = 202
    return runner


def test_record_chained_restart_coalesce_keeps_hub_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    from gateway.restart_chain import record_chained_restart

    runner = _draining_runner()
    assert record_chained_restart(runner) is True
    marker = tmp_path / ".restart_chain.json"
    assert "restart_hub" not in json.loads(marker.read_text(encoding="utf-8"))

    # A later --hub request joining the chain must not be lost to coalescing.
    assert record_chained_restart(runner, restart_hub=True) is True
    assert json.loads(marker.read_text(encoding="utf-8"))["restart_hub"] is True


@pytest.mark.asyncio
async def test_run_chained_restart_writes_hub_pending_before_request(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr("gateway.restart_chain.POST_CLAIM_STARTUP_SETTLE_SECONDS", 0.0)
    marker = tmp_path / ".restart_chain.json"
    marker.write_text(
        json.dumps({
            "requested_at": time.time(),
            "source": {"kind": "user"},
            "via_service": False,
            "restart_hub": True,
        }),
        encoding="utf-8",
    )
    observed: dict = {}

    def _capture(**kwargs):
        observed["pending_at_request"] = (tmp_path / PENDING_NAME).exists()
        return True

    runner = SimpleNamespace(request_restart=MagicMock(side_effect=_capture))
    from gateway.restart_chain import run_chained_restart

    assert await run_chained_restart(runner) is True

    assert observed["pending_at_request"] is True
    assert not marker.exists()
    runner.request_restart.assert_called_once_with(overlap=True, via_service=False)


@pytest.mark.asyncio
async def test_run_chained_restart_without_flag_writes_no_pending(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr("gateway.restart_chain.POST_CLAIM_STARTUP_SETTLE_SECONDS", 0.0)
    marker = tmp_path / ".restart_chain.json"
    marker.write_text(
        json.dumps({
            "requested_at": time.time(), "source": {"kind": "user"}, "via_service": False,
        }),
        encoding="utf-8",
    )
    runner = SimpleNamespace(request_restart=MagicMock(return_value=True))
    from gateway.restart_chain import run_chained_restart

    assert await run_chained_restart(runner) is True
    assert not (tmp_path / PENDING_NAME).exists()


# ── successor-side bounce (mocked systemctl) ─────────────────────────────────


def _fake_systemctl(shows, *, restart_rc=0, restart_stderr=""):
    """Injected subprocess runner: ``shows`` yields one result per ``show`` call."""
    calls: list[list[str]] = []
    show_results = iter(shows)

    def run(argv, **_kwargs):
        calls.append(list(argv))
        if argv[1] == "show":
            result = next(show_results)
            if isinstance(result, BaseException):
                raise result
            if isinstance(result, SimpleNamespace):
                return result
            return SimpleNamespace(returncode=0, stdout=str(result), stderr="")
        return SimpleNamespace(returncode=restart_rc, stdout="", stderr=restart_stderr)

    return run, calls


def _write_log(home: Path, text: str) -> Path:
    log = home / "logs" / "gateway.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(text)
    return log


def _result(home: Path) -> dict:
    return json.loads((home / RESULT_NAME).read_text(encoding="utf-8"))


def _no_sleep(_seconds):
    return None


def test_consume_bounces_hub_unit_success(tmp_path):
    from gateway.restart_hub import consume_restart_hub_bounce, write_hub_pending

    home = tmp_path
    _write_log(home, "pre-bounce bytes\n")
    write_hub_pending(home)
    run, calls = _fake_systemctl(shows=[111, 222])

    assert consume_restart_hub_bounce(home, run=run, sleep=_no_sleep) is True

    assert calls[0] == ["systemctl", "show", HUB_UNIT, "-p", "MainPID", "--value"]
    assert calls[1] == ["systemctl", "restart", HUB_UNIT]
    # calls[2] is the post-restart MainPID poll that confirmed pid 222.
    assert calls[2] == ["systemctl", "show", HUB_UNIT, "-p", "MainPID", "--value"]
    assert not (home / PENDING_NAME).exists()
    report = _result(home)
    assert report["success"] is True
    assert report["old_pid"] == 111
    assert report["new_pid"] == 222
    assert report["attached_generation"] is False  # verified after adapters attach

    # One-shot: a second call is a no-op because the marker was consumed.
    assert consume_restart_hub_bounce(home, run=run, sleep=_no_sleep) is False
    assert len(calls) == 3


def test_consume_without_pending_marker_is_a_noop(tmp_path):
    from gateway.restart_hub import consume_restart_hub_bounce

    run, calls = _fake_systemctl(shows=[111])
    assert consume_restart_hub_bounce(tmp_path, run=run, sleep=_no_sleep) is False
    assert calls == []
    assert not (tmp_path / RESULT_NAME).exists()


def test_complete_report_success_line_with_attach_marker(tmp_path):
    from gateway.restart_hub import (
        complete_restart_hub_report,
        consume_restart_hub_bounce,
        load_hub_report_line,
        write_hub_pending,
    )

    home = tmp_path
    _write_log(home, "pre-bounce bytes\n")
    write_hub_pending(home)
    run, _calls = _fake_systemctl(shows=[111, 222])
    consume_restart_hub_bounce(home, run=run, sleep=_no_sleep)
    _write_log(home, "[discord] Attached generation g2 to Discord hub pid 222\n")

    complete_restart_hub_report(home)

    report = _result(home)
    assert report["attached_generation"] is True
    assert load_hub_report_line(home) == (
        f"restarted {HUB_UNIT} (pid 111 -> 222); Attached generation seen"
    )
    # Idempotent: a second completion pass changes nothing.
    complete_restart_hub_report(home)
    assert _result(home)["attached_generation"] is True


def test_complete_report_missing_marker_is_warning_not_failure(tmp_path):
    from gateway.restart_hub import (
        complete_restart_hub_report,
        consume_restart_hub_bounce,
        hub_report_line,
        load_hub_report_line,
        write_hub_pending,
    )

    home = tmp_path
    _write_log(home, "pre-bounce bytes\n")
    write_hub_pending(home)
    run, _calls = _fake_systemctl(shows=[111, 222])
    consume_restart_hub_bounce(home, run=run, sleep=_no_sleep)

    complete_restart_hub_report(home)  # log gained no "Attached generation" bytes

    line = load_hub_report_line(home)
    assert line is not None
    assert line.startswith("warning — ")
    assert "Attached generation not seen" in line
    assert line.endswith("; gateway restart continued")
    assert hub_report_line(_result(home)) == line


def test_bounce_pid_unchanged_is_warning(tmp_path):
    from gateway.restart_hub import (
        consume_restart_hub_bounce,
        load_hub_report_line,
        write_hub_pending,
    )

    home = tmp_path
    write_hub_pending(home)
    run, _calls = _fake_systemctl(shows=[111, 111, 111, 111, 111])

    assert consume_restart_hub_bounce(home, run=run, sleep=_no_sleep) is True

    report = _result(home)
    assert report["success"] is False
    assert report["new_pid"] is None
    line = load_hub_report_line(home)
    assert line is not None and "MainPID unchanged" in line


def test_bounce_systemctl_missing_is_warning(tmp_path):
    from gateway.restart_hub import consume_restart_hub_bounce, write_hub_pending

    home = tmp_path
    write_hub_pending(home)

    def _missing(argv, **_kwargs):
        raise FileNotFoundError("systemctl")

    assert consume_restart_hub_bounce(home, run=_missing, sleep=_no_sleep) is True

    report = _result(home)
    assert report["success"] is False
    assert "systemctl not found" in report["warning"]


def test_bounce_unit_missing_is_warning(tmp_path):
    from gateway.restart_hub import consume_restart_hub_bounce, write_hub_pending

    home = tmp_path
    write_hub_pending(home)
    run, calls = _fake_systemctl(
        shows=[SimpleNamespace(returncode=5, stdout="", stderr="Unit hermes-discord-hub.service not found.")],
    )

    assert consume_restart_hub_bounce(home, run=run, sleep=_no_sleep) is True

    assert calls == [["systemctl", "show", HUB_UNIT, "-p", "MainPID", "--value"]]
    report = _result(home)
    assert report["success"] is False
    assert "not found" in report["warning"]
    assert load_hub_warning(home)


def load_hub_warning(home: Path) -> str:
    from gateway.restart_hub import load_hub_report_line

    return load_hub_report_line(home) or ""


def test_stale_hub_result_is_ignored(tmp_path):
    from gateway.restart_hub import load_hub_report_line

    (tmp_path / RESULT_NAME).write_text(
        json.dumps({
            "success": True, "warning": None, "old_pid": 1, "new_pid": 2,
            "attached_generation": True, "at": 0,
        }),
        encoding="utf-8",
    )
    assert load_hub_report_line(tmp_path) is None


# ── restart-result composer stays byte-identical by default ─────────────────


def test_restart_success_content_default_shapes_unchanged():
    from gateway.run_turn_recovery import (
        RESTART_NOTICE_FIRST_LINE,
        _parse_restart_success_content,
        _restart_success_content,
    )

    for old in ("pid 1", None):
        text = _restart_success_content(old, "pid 9")
        assert text.startswith(RESTART_NOTICE_FIRST_LINE + "\n")
        assert "\nHub:" not in text
        assert _parse_restart_success_content(text) == (old, "pid 9")


@pytest.mark.parametrize("old", ["pid 1", None])
def test_parser_accepts_shapes_with_optional_hub_line(old):
    from gateway.run_turn_recovery import _parse_restart_success_content, _restart_success_content

    hub_line = f"restarted {HUB_UNIT} (pid 1 -> 2); Attached generation seen"
    with_hub = _restart_success_content(old, "pid 9", hub_line=hub_line)
    assert with_hub.endswith(f"\nHub: {hub_line}")
    assert _parse_restart_success_content(with_hub) == (old, "pid 9")

    without_hub = _restart_success_content(old, "pid 9")
    assert "\nHub: " not in without_hub
    assert _parse_restart_success_content(without_hub) == (old, "pid 9")


def test_parser_rejects_empty_hub_line():
    from gateway.run_turn_recovery import _parse_restart_success_content

    content = (
        "Restarted successfully\n"
        "New process: pid 9\n"
        "You are now running in pid 9. Continue the original task.\n"
        "Hub: "
    )
    assert _parse_restart_success_content(content) is None


def _restart_tail_history() -> list[dict]:
    return [
        {"role": "user", "content": "restart, then finish my task"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_restart",
                "type": "function",
                "function": {"name": "restart", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_restart",
            "name": "restart",
            "content": "Restarted successfully",
        },
    ]


def test_repair_restart_tool_tail_includes_fresh_hub_report(monkeypatch, tmp_path):
    from gateway.run_turn_recovery import _repair_restart_tool_tail

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / RESULT_NAME).write_text(
        json.dumps({
            "success": True, "warning": None, "old_pid": 111, "new_pid": 222,
            "attached_generation": True, "at": time.time(),
        }),
        encoding="utf-8",
    )

    repair = _repair_restart_tool_tail(_restart_tail_history())

    assert repair is not None
    rewritten, _changed = repair
    assert rewritten[-1]["content"].endswith(
        f"\nHub: restarted {HUB_UNIT} (pid 111 -> 222); Attached generation seen"
    )


def test_repair_restart_tool_tail_unchanged_without_hub_report(monkeypatch, tmp_path):
    from gateway.run_turn_recovery import _repair_restart_tool_tail

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / RESULT_NAME).write_text(
        json.dumps({
            "success": True, "warning": None, "old_pid": 1, "new_pid": 2,
            "attached_generation": True, "at": 0,  # stale: ignored
        }),
        encoding="utf-8",
    )

    repair = _repair_restart_tool_tail(_restart_tail_history())

    assert repair is not None
    rewritten, _changed = repair
    assert "\nHub: " not in rewritten[-1]["content"]


# ── comeback notice ──────────────────────────────────────────────────────────


async def _send_restart_notification(monkeypatch, tmp_path, *, hub_result):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / ".restart_notify.json").write_text(
        json.dumps({
            "platform": "discord", "chat_id": "42",
            "chat_type": "dm", "message_id": "m1",
        }),
        encoding="utf-8",
    )
    if hub_result is not None:
        (tmp_path / RESULT_NAME).write_text(json.dumps(hub_result), encoding="utf-8")
    runner, adapter = make_restart_runner()
    runner._mark_and_schedule_restart_requester_resume = MagicMock(return_value=True)
    transport = SimpleNamespace(
        adapter=adapter,
        send=AsyncMock(return_value=SendResult(success=True, message_id="2")),
    )
    monkeypatch.setattr(
        "gateway.delivery.resolve_delivery_transport",
        lambda platform, config, adapters: transport,
    )
    await runner._send_restart_notification()
    return transport


@pytest.mark.asyncio
async def test_restart_notification_appends_hub_line(monkeypatch, tmp_path):
    transport = await _send_restart_notification(
        monkeypatch, tmp_path,
        hub_result={
            "success": True, "warning": None, "old_pid": 111, "new_pid": 222,
            "attached_generation": True, "at": time.time(),
        },
    )
    sent_text = transport.send.await_args.args[2]
    assert sent_text == (
        f"{NOTICE_TEXT}\n"
        f"Hub: restarted {HUB_UNIT} (pid 111 -> 222); Attached generation seen"
    )


@pytest.mark.asyncio
async def test_restart_notification_unchanged_without_hub_result(monkeypatch, tmp_path):
    transport = await _send_restart_notification(monkeypatch, tmp_path, hub_result=None)
    assert transport.send.await_args.args[2] == NOTICE_TEXT


# ── lifecycle guard ──────────────────────────────────────────────────────────


def test_lifecycle_guard_hub_unit_restart_is_not_gateway_lifecycle():
    from cron.lifecycle_guard import scan_gateway_lifecycle

    unsafe, refusal = scan_gateway_lifecycle(
        f"systemctl restart {HUB_UNIT}"
    )
    assert unsafe is False
    assert refusal is None

    unsafe, _refusal = scan_gateway_lifecycle("systemctl restart hermes-gateway")
    assert unsafe is True
