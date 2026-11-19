"""The hub client's unix-stream reader must accept frames larger than 64 KiB.

``asyncio.open_unix_connection`` defaults its StreamReader limit to 64 KiB;
a hub frame above that (large Discord payloads: embeds, long messages,
attachment batches) made ``readline()`` raise LimitOverrunError and dropped
the frame. The adapter opens the socket with a 16 MiB limit instead.

Regression for the live-hotfix: a single >64 KiB frame over a real unix
socket must arrive whole.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.hub_client import DiscordHubClientAdapter


@pytest.mark.platforms("posix")
def test_hub_reader_accepts_frame_over_64kib():
    async def scenario(socket_dir: str) -> None:
        payload = {"type": "control", "data": "x" * 200_000}
        frame = (json.dumps(payload) + "\n").encode("utf-8")
        assert len(frame) > 65536  # larger than the 64 KiB StreamReader default

        async def fake_hub(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(frame)
            await writer.drain()

        socket_path = str(Path(socket_dir) / "hub.sock")
        server = await asyncio.start_unix_server(fake_hub, path=socket_path)
        client = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
        client._socket_path = socket_path
        try:
            await client._open_socket()
            assert client._reader is not None
            line = await asyncio.wait_for(client._reader.readline(), 5)
            assert json.loads(line) == payload
        finally:
            if client._writer is not None:
                client._writer.close()
            server.close()
            await server.wait_closed()

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="h-") as socket_dir:
        asyncio.run(scenario(socket_dir))
