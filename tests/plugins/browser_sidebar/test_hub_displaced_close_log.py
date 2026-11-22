"""Log treatment of the hub's displaced-connection close (1012 reconnected).

The hub closes a displaced previous extension socket with code 1012, reason
"reconnected" — intended single-active-socket behavior. The displaced
handler's read loop then raises ConnectionClosedError; that expected close
must not reach the blanket ``logger.exception`` path, while a close with any
other code still must.
"""

import logging

import pytest

pytest.importorskip("websockets")

from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from plugins.platforms.browser_sidebar.hub import BrowserSidebarHub

pytestmark = [pytest.mark.asyncio, pytest.mark.platforms("posix")]

TOKEN = "displaced-close-token"
HUB_LOGGER = "plugins.platforms.browser_sidebar.hub"


class _FakeWebSocket:
    """Minimal stand-in: authorized subprotocol, read loop raises `error`."""

    def __init__(self, error: BaseException):
        self.subprotocol = TOKEN
        self._error = error

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        raise self._error
        yield  # pragma: no cover - marks this an async generator

    async def close(self, code: int = 1000, reason: str = "") -> None:
        pass


async def test_displaced_1012_close_exits_cleanly_without_error_log(caplog):
    hub = BrowserSidebarHub()
    hub._token = TOKEN
    ws = _FakeWebSocket(ConnectionClosedError(Close(1012, "reconnected"), None))

    with caplog.at_level(logging.DEBUG, logger=HUB_LOGGER):
        await hub._handle_connection(ws)

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == []
    assert hub._socket is None


async def test_other_close_code_still_logs_exception(caplog):
    hub = BrowserSidebarHub()
    hub._token = TOKEN
    ws = _FakeWebSocket(ConnectionClosedError(None, Close(1006, "abnormal closure")))

    with caplog.at_level(logging.DEBUG, logger=HUB_LOGGER):
        await hub._handle_connection(ws)

    assert any(
        r.levelno >= logging.ERROR
        and "Browser sidebar connection handler failed" in r.getMessage()
        for r in caplog.records
    )
    assert hub._socket is None
