"""Behavioral tests for default-on, fail-closed Camofox progress capture."""

from __future__ import annotations

import io
import json
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image

from agent.moa_loop import (
    current_tool_stage_subscriber_token, publish_tool_stage, subscribe_tool_stage_events,
)
from hermes_constants import get_hermes_home, hermes_home_key
from tools import browser_camofox, browser_progress, browser_tool
from tools.vision_ocr import LocalOCRResult, run_local_ocr_checked


def _png(color: int = 0) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), (color, 0, 0)).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def capture_harness(monkeypatch):
    events = []
    home = get_hermes_home().resolve()
    session_id = "shared-session"
    unsubscribe = subscribe_tool_stage_events(session_id, events.append, browser_progress=True)
    token = current_tool_stage_subscriber_token(session_id)
    assert token is not None
    monkeypatch.setattr(browser_camofox, "_post", lambda *args, **kwargs: {
        "result": {"category": None},
    })
    monkeypatch.setattr(browser_camofox, "_snapshot_data", lambda *args, **kwargs: {
        "snapshot": '- heading "Public documentation" [e1]',
    })
    monkeypatch.setattr(browser_camofox, "_get_raw", lambda *args, **kwargs: SimpleNamespace(
        content=_png(),
    ))
    monkeypatch.setattr(
        "tools.vision_ocr.run_local_ocr_checked",
        lambda *args, **kwargs: LocalOCRResult(True, "Public documentation"),
    )
    with browser_camofox._sessions_lock:
        browser_camofox._sessions[browser_camofox._session_key("task", home_key=hermes_home_key(home))] = {
            "tab_id": "tab", "user_id": "user", "home_key": hermes_home_key(home),
        }
    browser_progress.clear_browser_progress_state(session_id)
    clock = [100.0]
    monkeypatch.setattr(browser_progress.time, "monotonic", lambda: clock[0])

    def capture(raw_result: str = json.dumps({"success": True})) -> bool:
        return browser_progress.capture_browser_progress(
            "browser_navigate",
            raw_result,
            task_id="task",
            session_id=session_id,
            tool_call_id="call",
            subscriber_token=token,
        )

    yield SimpleNamespace(
        capture=capture, clock=clock, events=events, home=home, session_id=session_id,
    )

    for event in events:
        browser_progress.discard_browser_progress_event(event, home)
    unsubscribe()
    browser_progress.clear_browser_progress_state(session_id)
    with browser_camofox._sessions_lock:
        browser_camofox._sessions.pop(browser_camofox._session_key("task", home_key=hermes_home_key(home)), None)


def _cache_files(home):
    directory = home / "browser_screenshots"
    return list(directory.glob("*.png")) if directory.exists() else []


def test_deduplicates_identical_frames_and_event_contains_no_page_content(capture_harness):
    assert capture_harness.capture() is True
    capture_harness.clock[0] += 2
    assert capture_harness.capture() is False
    assert len(capture_harness.events) == 1
    event = capture_harness.events[0]
    assert set(event) == {
        "type", "path", "profile_home", "profile_key", "session_id", "task_id",
        "tool_call_id", "sha256",
    }
    assert "Public documentation" not in repr(event)


def test_rate_limit_applies_before_another_screenshot(capture_harness, monkeypatch):
    calls = []
    monkeypatch.setattr(browser_camofox, "_get_raw", lambda *args, **kwargs: calls.append(args) or SimpleNamespace(
        content=_png(len(calls)),
    ))
    assert capture_harness.capture() is True
    capture_harness.clock[0] += 0.5
    assert capture_harness.capture() is False
    assert len(calls) == 1


def test_turn_cap_and_explicit_turn_reset(capture_harness, monkeypatch):
    content = [_png(0)]
    monkeypatch.setattr(browser_camofox, "_get_raw", lambda *args, **kwargs: SimpleNamespace(
        content=content[0],
    ))
    for index in range(browser_progress._MAX_IMAGES_PER_TURN):
        content[0] = _png(index + 1)
        capture_harness.clock[0] += 2
        assert capture_harness.capture() is True
    content[0] = _png(50)
    capture_harness.clock[0] += 2
    assert capture_harness.capture() is False
    assert len(capture_harness.events) == browser_progress._MAX_IMAGES_PER_TURN

    browser_progress.clear_browser_progress_state(capture_harness.session_id)
    capture_harness.clock[0] += 2
    assert capture_harness.capture() is True


@pytest.mark.parametrize(("snapshot", "ocr_text"), [
    ('- textbox "Password" [e1]', "Public documentation"),
    ('- heading "Account" [e1]', "Enter your 6-digit verification code"),
    ('- heading "Account" [e1]', "Unlock your vault with its master password"),
    ('- heading "Checkout" [e1]', "Credit card CVV and payment details"),
])
def test_sensitive_dom_or_successful_ocr_suppresses_and_cleans(
    capture_harness, monkeypatch, snapshot, ocr_text,
):
    monkeypatch.setattr(browser_camofox, "_snapshot_data", lambda *args, **kwargs: {
        "snapshot": snapshot,
    })
    monkeypatch.setattr(
        "tools.vision_ocr.run_local_ocr_checked",
        lambda *args, **kwargs: LocalOCRResult(True, ocr_text),
    )
    assert capture_harness.capture() is False
    assert capture_harness.events == []
    assert _cache_files(capture_harness.home) == []


def test_failed_local_privacy_inspection_fails_closed(capture_harness, monkeypatch):
    monkeypatch.setattr(
        "tools.vision_ocr.run_local_ocr_checked",
        lambda *args, **kwargs: LocalOCRResult(False, ""),
    )
    assert capture_harness.capture() is False
    assert capture_harness.events == []
    assert _cache_files(capture_harness.home) == []

def test_unlabeled_password_field_suppresses_before_screenshot(capture_harness, monkeypatch):
    requests = []

    def evaluate(path, body, timeout=None):
        requests.append((path, body, timeout))
        return {"result": {"category": "authentication"}}

    monkeypatch.setattr(browser_camofox, "_post", evaluate)
    monkeypatch.setattr(
        browser_camofox, "_get_raw",
        lambda *args, **kwargs: pytest.fail("sensitive field reached screenshot transport"),
    )
    assert capture_harness.capture() is False
    assert capture_harness.events == []
    assert requests[0][0] == "/tabs/tab/evaluate"
    assert requests[0][1]["userId"] == "user"
    assert 'input[type="password"]' in requests[0][1]["expression"]
    assert _cache_files(capture_harness.home) == []


@pytest.mark.parametrize("failure", ["exception", "malformed"])
def test_dom_privacy_probe_failure_fails_closed(capture_harness, monkeypatch, failure):
    if failure == "exception":
        def inspect(*args, **kwargs):
            raise RuntimeError("DOM inspection failed")
    else:
        def inspect(*args, **kwargs):
            return {"result": {"category": None, "unexpected": "page content"}}
    monkeypatch.setattr(browser_camofox, "_post", inspect)
    assert capture_harness.capture() is False
    assert capture_harness.events == []
    assert _cache_files(capture_harness.home) == []


def test_default_on_with_explicit_opt_out():
    config_path = get_hermes_home() / "config.yaml"
    config_path.write_text("browser: {}\n", encoding="utf-8")
    assert browser_progress.browser_progress_enabled() is True
    config_path.write_text("browser:\n  progress_images: false\n", encoding="utf-8")
    assert browser_progress.browser_progress_enabled() is False
    config_path.write_text("browser:\n  progress_images: true\n", encoding="utf-8")
    assert browser_progress.browser_progress_enabled() is True


def test_wrong_profile_tab_binding_never_publishes(capture_harness):
    with browser_camofox._sessions_lock:
        key = browser_camofox._session_key("task", home_key=hermes_home_key(capture_harness.home))
        browser_camofox._sessions[key]["home_key"] = "another-profile"
    assert capture_harness.capture() is False
    assert capture_harness.events == []


def test_unsubscribed_facade_does_not_capture_or_change_tool_result(monkeypatch):
    monkeypatch.setattr(
        browser_progress, "capture_browser_progress",
        lambda *args, **kwargs: pytest.fail("unsubscribed capture attempted"),
    )
    sentinel = '{"success":true,"value":1}'
    handler = browser_tool._handler(
        lambda **kwargs: sentinel, {"url": ""}, "browser_navigate",
    )
    assert handler(
        {"url": "https://example.com"}, task_id="task", session_id="session",
    ) is sentinel


def test_capture_exception_never_changes_browser_tool_result(monkeypatch):
    monkeypatch.setattr(browser_progress, "browser_progress_subscriber_token", lambda session_id: "token")
    def explode(*args, **kwargs):
        raise RuntimeError("capture failed")
    monkeypatch.setattr(browser_progress, "capture_browser_progress", explode)
    sentinel = '{"success":true,"value":2}'
    handler = browser_tool._handler(
        lambda **kwargs: sentinel, {"url": ""}, "browser_navigate",
    )
    assert handler(
        {"url": "https://example.com"}, task_id="task", session_id="session",
    ) is sentinel


def test_stale_turn_token_cannot_publish_to_replacement_subscriber():
    session_id = "replacement-turn"
    first_events = []
    second_events = []
    unsubscribe_first = subscribe_tool_stage_events(session_id, first_events.append)
    first_token = current_tool_stage_subscriber_token(session_id)
    unsubscribe_second = subscribe_tool_stage_events(session_id, second_events.append)
    second_token = current_tool_stage_subscriber_token(session_id)
    try:
        assert publish_tool_stage(session_id, {"event": "late"}, subscriber_token=first_token) is False
        assert publish_tool_stage(session_id, {"event": "current"}, subscriber_token=second_token) is True
        assert first_events == []
        assert second_events == [{"event": "current"}]
        unsubscribe_second()
        assert publish_tool_stage(session_id, {"event": "parent"}, subscriber_token=first_token) is True
        assert first_events == [{"event": "parent"}]
    finally:
        unsubscribe_second()
        unsubscribe_first()


def test_same_session_stage_bus_is_profile_scoped_a_b_a(monkeypatch, tmp_path):
    session_id = "shared-across-profiles"
    events_a = []
    events_b = []
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "a"))
    unsubscribe_a = subscribe_tool_stage_events(session_id, events_a.append)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "b"))
    unsubscribe_b = subscribe_tool_stage_events(session_id, events_b.append)
    try:
        assert publish_tool_stage(session_id, {"owner": "b"}) is True
        assert events_a == []
        assert events_b == [{"owner": "b"}]
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "a"))
        assert publish_tool_stage(session_id, {"owner": "a"}) is True
        assert events_a == [{"owner": "a"}]
        assert events_b == [{"owner": "b"}]
    finally:
        unsubscribe_b()
        unsubscribe_a()


def test_checked_ocr_accepts_successful_empty_text(monkeypatch):
    monkeypatch.setattr("tools.vision_ocr.shutil.which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args=args, returncode=0, stdout="  \n", stderr="",
    ))
    assert run_local_ocr_checked("image.png") == LocalOCRResult(True, "")


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "missing"])
def test_checked_ocr_marks_every_inspection_failure(monkeypatch, failure):
    if failure == "missing":
        monkeypatch.setattr("tools.vision_ocr.shutil.which", lambda name: None)
    else:
        monkeypatch.setattr("tools.vision_ocr.shutil.which", lambda name: "/usr/bin/tesseract")
        if failure == "nonzero":
            monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
                args=args, returncode=2, stdout="partial text", stderr="failed",
            ))
        else:
            def timeout(*args, **kwargs):
                raise subprocess.TimeoutExpired("tesseract", 5)
            monkeypatch.setattr(subprocess, "run", timeout)
    result = run_local_ocr_checked("image.png")
    assert result.success is False
    assert result.text == ""
