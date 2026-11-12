"""Direct Camofox console/evaluate behavior and safety contracts."""

import json
from unittest.mock import call, patch

from tools.browser_tool import _browser_eval, browser_console
from tools.registry import registry


def test_console_without_expression_delegates_to_camofox():
    with (
        patch("tools.browser_tool._track_activity", return_value="task"),
        patch("tools.browser_tool._camofox.camofox_console", return_value='{"success": true}') as console,
    ):
        assert json.loads(browser_console(clear=True, task_id="task"))["success"] is True
    console.assert_called_once_with(True, "task")


def test_registered_console_handler_evaluates_and_redacts_result():
    secret = "sk-proj-ABCD1234567890EFGH"
    entry = registry.get_entry("browser_console")
    assert entry is not None

    with (
        patch("tools.browser_tool._track_activity", return_value="registered"),
        patch("tools.browser_tool._allow_private_urls", return_value=False),
        patch("tools.browser_tool._camofox._ensure_tab", return_value={"tab_id": "tab-1", "user_id": "u1"}),
        patch(
            "tools.browser_tool._camofox._post",
            side_effect=[{"result": secret}, {"result": "https://example.com/"}],
        ) as post,
    ):
        result = json.loads(entry.handler({"expression": "document.body.innerText"}, task_id="registered"))

    assert result["success"] is True
    assert secret not in json.dumps(result)
    assert post.call_args_list == [
        call("/tabs/tab-1/evaluate", body={"expression": "document.body.innerText", "userId": "u1"}),
        call("/tabs/tab-1/evaluate", body={"expression": "window.location.href", "userId": "u1"}),
    ]


def test_private_url_literal_is_blocked_before_evaluate():
    with (
        patch("tools.browser_tool._track_activity", return_value="default"),
        patch("tools.browser_tool._allow_private_urls", return_value=False),
        patch("tools.browser_tool._camofox._post") as post,
    ):
        result = json.loads(_browser_eval('fetch("http://127.0.0.1:8080/private")'))

    assert result["success"] is False
    assert "private or internal" in result["error"]
    post.assert_not_called()


def test_post_evaluate_private_page_is_not_returned():
    with (
        patch("tools.browser_tool._track_activity", return_value="default"),
        patch("tools.browser_tool._allow_private_urls", return_value=False),
        patch("tools.browser_tool._camofox._ensure_tab", return_value={"tab_id": "tab-1", "user_id": "u1"}),
        patch(
            "tools.browser_tool._camofox._post",
            side_effect=[{"result": "private page contents"}, {"result": "http://127.0.0.1:8080/"}],
        ),
    ):
        result = json.loads(_browser_eval("location.href = 'https://example.com'; document.body.innerText"))

    assert result["success"] is False
    assert "JavaScript navigation" in result["error"]
    assert "private page contents" not in json.dumps(result)


def test_restrict_evaluate_blocks_sensitive_primitive():
    with patch("tools.browser_tool._browser_cfg", side_effect=lambda key, default, parse, label: key == "restrict_evaluate"):
        result = json.loads(browser_console(expression="document.cookie"))

    assert result["success"] is False
    assert "document.cookie" in result["error"]
    assert "restrict_evaluate" in result["error"]


def test_evaluate_requires_an_open_tab():
    with (
        patch("tools.browser_tool._track_activity", return_value="default"),
        patch("tools.browser_tool._camofox._ensure_tab", return_value={"tab_id": None, "user_id": "u1"}),
        patch("tools.browser_tool._camofox._post") as post,
    ):
        result = json.loads(_browser_eval("document.title"))

    assert result["success"] is False
    assert "browser_navigate" in result["error"]
    post.assert_not_called()
