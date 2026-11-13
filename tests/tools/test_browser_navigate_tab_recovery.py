"""Camofox tab re-registration after a lost control response.

``POST /tabs`` (or the per-tab navigate POST) can die after the server already
created the page — a read timeout or an HTTP 500. The session must recover the
``tabId`` by listing the user's tabs and matching the posted URL, so the rest of
the task does not strand on ``"No browser session"``. Connection errors, 404s and
other 4xx mean the action never happened and keep today's failure text.
"""

import json
from unittest.mock import MagicMock

import requests

from tools import browser_camofox as bc
from tools.browser_camofox import camofox_navigate, camofox_snapshot

TARGET_URL = "http://192.168.1.9/photo.jpg"


def _mock_response(status: int = 200, json_data: "dict | None" = None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data or {}
    resp.raise_for_status = MagicMock()
    return resp


def _http_error(status: int) -> requests.HTTPError:
    return requests.HTTPError(f"{status} error", response=_mock_response(status=status))


def _install(monkeypatch, *, post_side_effect, tabs_response):
    """Patch the Camofox seam functions; return the ``_get`` mock (for call assertions)."""
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    monkeypatch.setattr(bc, "get_vnc_url", lambda: None)
    monkeypatch.setattr(bc, "_post", MagicMock(side_effect=post_side_effect))

    def fake_get(path, params=None, timeout=None):
        if path == "/tabs":
            return tabs_response
        return {"snapshot": "page snapshot", "refsCount": 2}

    get_mock = MagicMock(side_effect=fake_get)
    monkeypatch.setattr(bc, "_get", get_mock)
    return get_mock


def _tabs_requested(get_mock) -> bool:
    return any(call.args and call.args[0] == "/tabs" for call in get_mock.call_args_list)


def test_timeout_after_tab_created_adopts_tab(monkeypatch):
    task_id = "rec-timeout"
    _install(monkeypatch,
             post_side_effect=requests.Timeout("read timed out"),
             tabs_response={"tabs": [{"tabId": "tab-9", "url": TARGET_URL}]})
    try:
        nav = json.loads(camofox_navigate(TARGET_URL, task_id=task_id))
        assert nav["success"] is True
        assert bc._sessions[task_id]["tab_id"] == "tab-9"

        snap = json.loads(camofox_snapshot(task_id=task_id))
        assert "No browser session" not in json.dumps(snap)
    finally:
        bc._drop_session(task_id)


def test_read_timeout_after_tab_created_adopts_tab(monkeypatch):
    task_id = "rec-read-timeout"
    _install(monkeypatch,
             post_side_effect=requests.ReadTimeout("read timed out"),
             tabs_response={"tabs": [{"tabId": "tab-r", "url": TARGET_URL}]})
    try:
        nav = json.loads(camofox_navigate(TARGET_URL, task_id=task_id))
        assert nav["success"] is True
        assert bc._sessions[task_id]["tab_id"] == "tab-r"
    finally:
        bc._drop_session(task_id)


def test_timeout_with_no_tabs_preserves_failure(monkeypatch):
    task_id = "rec-empty"
    get_mock = _install(monkeypatch,
                        post_side_effect=requests.Timeout("read timed out"),
                        tabs_response={"tabs": []})
    try:
        nav = json.loads(camofox_navigate(TARGET_URL, task_id=task_id))
        assert nav["success"] is False
        assert bc._sessions[task_id]["tab_id"] is None

        snap = json.loads(camofox_snapshot(task_id=task_id))
        assert "No browser session" in json.dumps(snap)
        assert _tabs_requested(get_mock)
    finally:
        bc._drop_session(task_id)


def test_timeout_with_non_matching_tab_url_not_adopted(monkeypatch):
    task_id = "rec-other"
    _install(monkeypatch,
             post_side_effect=requests.Timeout("read timed out"),
             tabs_response={"tabs": [{"tabId": "tab-o", "url": "http://192.168.1.9/other.jpg"}]})
    try:
        nav = json.loads(camofox_navigate(TARGET_URL, task_id=task_id))
        assert nav["success"] is False
        assert bc._sessions[task_id]["tab_id"] is None

        snap = json.loads(camofox_snapshot(task_id=task_id))
        assert "No browser session" in json.dumps(snap)
    finally:
        bc._drop_session(task_id)


def test_http_500_after_tab_created_adopts_tab(monkeypatch):
    task_id = "rec-500"
    _install(monkeypatch,
             post_side_effect=_http_error(500),
             tabs_response={"tabs": [{"tabId": "tab-5", "url": TARGET_URL}]})
    try:
        nav = json.loads(camofox_navigate(TARGET_URL, task_id=task_id))
        assert nav["success"] is True
        assert bc._sessions[task_id]["tab_id"] == "tab-5"

        snap = json.loads(camofox_snapshot(task_id=task_id))
        assert "No browser session" not in json.dumps(snap)
    finally:
        bc._drop_session(task_id)


def test_http_404_not_recovered_by_tab_listing(monkeypatch):
    task_id = "rec-404"
    get_mock = _install(monkeypatch,
                        post_side_effect=_http_error(404),
                        tabs_response={"tabs": [{"tabId": "tab-4", "url": TARGET_URL}]})
    try:
        nav = json.loads(camofox_navigate(TARGET_URL, task_id=task_id))
        assert nav["success"] is False
        assert bc._sessions[task_id]["tab_id"] is None
        assert not _tabs_requested(get_mock)
    finally:
        bc._drop_session(task_id)


def test_connection_error_keeps_cannot_connect_message(monkeypatch):
    task_id = "rec-conn"
    get_mock = _install(monkeypatch,
                        post_side_effect=requests.ConnectionError("refused"),
                        tabs_response={"tabs": [{"tabId": "tab-c", "url": TARGET_URL}]})
    try:
        nav = json.loads(camofox_navigate(TARGET_URL, task_id=task_id))
        assert nav["success"] is False
        assert "Cannot connect to Camofox" in json.dumps(nav)
        assert bc._sessions[task_id]["tab_id"] is None
        assert not _tabs_requested(get_mock)
    finally:
        bc._drop_session(task_id)


def test_navigate_timeout_on_existing_tab_adopts_matching_tab(monkeypatch):
    task_id = "rec-nav-timeout"
    _install(monkeypatch,
             post_side_effect=[{"tabId": "tab-nav", "url": TARGET_URL},
                               requests.Timeout("read timed out")],
             tabs_response={"tabs": [{"tabId": "tab-nav", "currentUrl": TARGET_URL}]})
    try:
        first = json.loads(camofox_navigate(TARGET_URL, task_id=task_id))
        assert first["success"] is True

        second = json.loads(camofox_navigate(TARGET_URL, task_id=task_id))
        assert second["success"] is True
        assert bc._sessions[task_id]["tab_id"] == "tab-nav"

        snap = json.loads(camofox_snapshot(task_id=task_id))
        assert "No browser session" not in json.dumps(snap)
    finally:
        bc._drop_session(task_id)
