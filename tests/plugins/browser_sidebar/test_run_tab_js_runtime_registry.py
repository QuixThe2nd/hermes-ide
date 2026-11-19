"""run_tab_js runtime routing: process-stable registry, profile scoping, cross-loop marshaling.

Regression suite for the incident where the gateway (attached to the browser
hub, chats flowing) still answered every ``run_tab_js`` call with
``browser sidebar is not connected``:

1. The plugin loader imports this plugin under synthetic module names and
   re-executes the package on every load, so module-global state in
   ``adapter.py`` exists once per module instance. A handler registered by
   one load read a different, empty adapter list than the one the live
   adapter appended to.
2. Registry dispatch runs async tool handlers on a worker event loop
   (``model_tools._run_async``) while the adapter's socket reader lives on
   the gateway loop — resolving adapter futures from the wrong loop.
3. ``disconnect()`` dropped the routing entry and ``connect()`` never
   restored it, so a reconnect left a healthy adapter permanently
   unroutable.
4. Routing was profile-blind: with several Hermes homes served by one
   process, a tool call could reach another profile's browser.

Every plugin load goes through the REAL loader (``PluginManager`` deferred
platform registration + ``platform_registry`` materialization) and every
tool call goes through the REAL ``tools.registry.dispatch``.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
import threading
from pathlib import Path

import pytest

pytest.importorskip("websockets")

from gateway.config import PlatformConfig
from gateway.platform_registry import platform_registry
from hermes_cli.plugins import PluginManager
from plugins.platforms.discord.hub import DiscordHubServer
from tools.registry import registry as tool_registry

pytestmark = pytest.mark.platforms("posix")

TOKEN = "regression-token_1"
PLUGIN_NAME = "browser-sidebar-platform"
PLATFORM = "browser_sidebar"
LOADER_MODULE = "hermes_plugins.browser_sidebar_platform.adapter"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sidebar_config(port: int) -> PlatformConfig:
    cfg = PlatformConfig(enabled=True)
    cfg.extra = {"token": TOKEN, "port": port}
    return cfg


def _load_plugin_real(manager: PluginManager):
    """Register + materialize the bundled platform through the real loader."""
    manifests = manager._collect_directory_manifests()
    manifest = next(m for m in manifests if (m.name or "") == PLUGIN_NAME)
    manager._register_deferred_platform(manifest)
    assert platform_registry.get(PLATFORM) is not None, "deferred platform must materialize"
    return manifest


class _GatewayLoop:
    """Dedicated event loop in its own thread — the gateway's loop for adapter
    socket I/O, kept distinct from whatever loop dispatches the tool."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="sidebar-gateway-loop", daemon=True)
        self._ready = threading.Event()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()

    def start(self):
        self._thread.start()
        assert self._ready.wait(10)

    def run(self, coro, timeout=30):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self):
        async def _shutdown():
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(_shutdown(), self.loop).result(10)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(10)
            self.loop.close()


class _SidebarRig:
    """Real hub server + registry-built adapter on a dedicated loop, plus a
    browser WebSocket that records frames and auto-answers tool requests."""

    def __init__(self, gateway_loop: _GatewayLoop, *, reply="ok-from-browser", attach_browser=True):
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="sidebar-runtime-reg-")
        self.loop = gateway_loop
        self.server = DiscordHubServer(Path(self._tmp.name) / "hub.sock")
        self.port = _free_port()
        self.adapter = platform_registry.create_adapter(PLATFORM, _sidebar_config(self.port))
        assert self.adapter is not None, "adapter must build through the platform registry"
        self.adapter._socket_path = self.server.socket_path
        self.frames = []
        self.reply = reply
        self.attach_browser = attach_browser
        self._ws = None

    async def _browser(self):
        from websockets.asyncio.client import connect

        self._ws = await connect(f"ws://127.0.0.1:{self.port}/ws", subprotocols=[TOKEN])
        try:
            while True:
                frame = json.loads(await self._ws.recv())
                self.frames.append(frame)
                if frame.get("type") == "tool" and self.reply is not None:
                    await self._ws.send(json.dumps({
                        "type": "tool_result", "request_id": frame["request_id"],
                        "ok": True, "result": self.reply,
                    }))
        except Exception:
            pass  # socket closed during teardown

    async def _wait_browser(self, timeout=5.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while not self.adapter._browser_connected:
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("browser never attached to the hub")
            await asyncio.sleep(0.02)

    def start(self):
        self.loop.run(self.server.start())
        assert self.loop.run(self.adapter.connect()), "adapter must attach to the hub"
        if self.attach_browser:
            self.loop.run(_spawn(self._browser()))
            self.loop.run(self._wait_browser())

    async def reconnect(self):
        await self.adapter.disconnect()
        assert await self.adapter.connect(), "adapter must re-attach to the hub"

    def stop(self):
        try:
            self.loop.run(self.adapter.disconnect())
        except Exception:
            pass
        try:
            self.loop.run(self.server.stop())
        except Exception:
            pass
        self._tmp.cleanup()


async def _spawn(coro):
    asyncio.get_running_loop().create_task(coro)


@pytest.fixture
def home(tmp_path, monkeypatch):
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home_dir))
    return home_dir


@pytest.fixture(autouse=True)
def _clean_runtime_registry():
    from plugins.platforms.browser_sidebar import runtime

    runtime.reset()
    yield
    runtime.reset()


# ─── 1. same-scope reload ────────────────────────────────────────────────────


def test_reload_same_scope_keeps_run_tab_js_routable(home):
    """A same-scope plugin reload re-executes adapter.py under a fresh module
    instance; both the stale (pre-reload) and current handlers must still reach
    the already-connected adapter."""
    manager = PluginManager()
    manifest = _load_plugin_real(manager)
    stale_entry = tool_registry.snapshot_registration(
        "run_tab_js", scope=manager.scope_key)
    assert stale_entry is not None

    gateway = _GatewayLoop()
    gateway.start()
    rig = _SidebarRig(gateway)
    try:
        rig.start()
        module_before = sys.modules.get(LOADER_MODULE)
        manager._load_plugin(manifest)  # real same-scope reload
        module_after = sys.modules.get(LOADER_MODULE)
        assert module_after is not module_before, "reload must recreate the module instance"

        payload = json.loads(tool_registry.dispatch("run_tab_js", {"code": "1"}))
        assert payload["success"] is True, payload
        assert payload["result"] == rig.reply

        # The handler captured BEFORE the reload routes too.
        stale = asyncio.run(stale_entry.handler({"code": "2"}))
        payload = json.loads(stale)
        assert payload["success"] is True, payload
    finally:
        rig.stop()
        gateway.stop()


# ─── 2. secondary-profile isolation ─────────────────────────────────────────


def test_secondary_profile_never_borrows_primary_browser(tmp_path, monkeypatch):
    """Two homes served by one process, both browsers connected: a dispatch
    under home B must reach B's browser only — never A's."""
    home_a = tmp_path / "home-a"
    home_b = tmp_path / "home-b"
    home_a.mkdir()
    home_b.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home_a))
    manager_a = PluginManager()
    _load_plugin_real(manager_a)
    monkeypatch.setenv("HERMES_HOME", str(home_b))
    manager_b = PluginManager()
    _load_plugin_real(manager_b)

    gateway = _GatewayLoop()
    gateway.start()
    rig_a = _SidebarRig(gateway, reply="from-profile-a")
    rig_b = _SidebarRig(gateway, reply="from-profile-b")
    try:
        # connect() captures the OWNING profile's key from the runtime scope it
        # runs in — start each rig under its own home, as the multiplexed
        # gateway does via _profile_runtime_scope.
        monkeypatch.setenv("HERMES_HOME", str(home_a))
        rig_a.start()
        monkeypatch.setenv("HERMES_HOME", str(home_b))
        rig_b.start()

        monkeypatch.setenv("HERMES_HOME", str(home_a))
        payload = json.loads(tool_registry.dispatch("run_tab_js", {"code": "a"}))
        assert payload["success"] is True, payload
        assert payload["result"] == "from-profile-a"

        monkeypatch.setenv("HERMES_HOME", str(home_b))
        payload = json.loads(tool_registry.dispatch("run_tab_js", {"code": "b"}))
        assert payload["success"] is True, payload
        assert payload["result"] == "from-profile-b"

        tool_frames_a = [f for f in rig_a.frames if f.get("type") == "tool"]
        tool_frames_b = [f for f in rig_b.frames if f.get("type") == "tool"]
        assert len(tool_frames_a) == 1 and tool_frames_a[0]["args"] == {"code": "a"}
        assert len(tool_frames_b) == 1 and tool_frames_b[0]["args"] == {"code": "b"}
    finally:
        rig_a.stop()
        rig_b.stop()
        gateway.stop()


# ─── 3. reconnect reuse ─────────────────────────────────────────────────────


def test_reconnect_restores_routing_after_disconnect(home):
    """disconnect() unregisters the adapter; a later connect() on the same
    instance must make it routable again."""
    manager = PluginManager()
    _load_plugin_real(manager)

    gateway = _GatewayLoop()
    gateway.start()
    rig = _SidebarRig(gateway)
    try:
        rig.start()
        payload = json.loads(tool_registry.dispatch("run_tab_js", {"code": "1"}))
        assert payload["success"] is True, payload

        rig.loop.run(rig.reconnect())

        payload = json.loads(tool_registry.dispatch("run_tab_js", {"code": "2"}))
        assert payload["success"] is True, payload
    finally:
        rig.stop()
        gateway.stop()


def test_disconnect_of_replaced_adapter_keeps_replacement_routable(home):
    """A same-profile replacement adapter re-registers over the old one; the
    old adapter's late disconnect must not evict the replacement."""
    manager = PluginManager()
    _load_plugin_real(manager)

    gateway = _GatewayLoop()
    gateway.start()
    old_rig = _SidebarRig(gateway, reply="old-adapter")
    new_rig = _SidebarRig(gateway, reply="new-adapter")
    try:
        old_rig.start()
        new_rig.start()
        # Same home: the newest connected adapter owns the profile's route.
        payload = json.loads(tool_registry.dispatch("run_tab_js", {"code": "1"}))
        assert payload["success"] is True, payload
        assert payload["result"] == "new-adapter"

        old_rig.loop.run(old_rig.adapter.disconnect())

        payload = json.loads(tool_registry.dispatch("run_tab_js", {"code": "2"}))
        assert payload["success"] is True, payload
        assert payload["result"] == "new-adapter"
    finally:
        old_rig.stop()
        new_rig.stop()
        gateway.stop()


# ─── 4. cross-thread/cross-loop execution ───────────────────────────────────


def test_run_tab_js_from_foreign_loop_marshals_onto_adapter_loop(home):
    """registry dispatch runs the handler on a worker loop while the adapter's
    socket lives on the gateway loop; the whole request must execute on the
    adapter's owning loop (no cross-thread future resolution)."""
    from plugins.platforms.browser_sidebar import adapter as canonical

    gateway = _GatewayLoop()
    gateway.start()
    # Build the adapter from the canonical module instance — NOT the loader's —
    # then drive the entry point from an unrelated loop (asyncio.run).
    cfg = _sidebar_config(_free_port())
    adapter = canonical.BrowserSidebarAdapter(cfg)
    tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="sidebar-xthread-")
    server = DiscordHubServer(Path(tmp.name) / "hub.sock")
    adapter._socket_path = server.socket_path
    frames = []

    async def _browser():
        from websockets.asyncio.client import connect

        ws = await connect(f"ws://127.0.0.1:{adapter.port}/ws", subprotocols=[TOKEN])
        try:
            while True:
                frame = json.loads(await ws.recv())
                frames.append(frame)
                if frame.get("type") == "tool":
                    await ws.send(json.dumps({
                        "type": "tool_result", "request_id": frame["request_id"],
                        "ok": True, "result": "cross-loop-ok"}))
        except Exception:
            pass

    try:
        gateway.run(server.start())
        assert gateway.run(adapter.connect())
        gateway.run(_spawn(_browser()))

        async def _wait():
            deadline = asyncio.get_running_loop().time() + 5
            while not adapter._browser_connected:
                await asyncio.sleep(0.02)
                assert asyncio.get_running_loop().time() < deadline

        gateway.run(_wait())

        # Fresh loop in the main thread — a different loop than the adapter's.
        payload = json.loads(asyncio.run(canonical._run_tab_js_entry({"code": "1"})))
        assert payload["success"] is True, payload
        assert payload["result"] == "cross-loop-ok"
        assert frames and frames[0]["type"] == "tool"
    finally:
        gateway.run(adapter.disconnect())
        gateway.run(server.stop())
        tmp.cleanup()
        gateway.stop()


# ─── 5. diagnostics ─────────────────────────────────────────────────────────


_DIAGNOSTICS_KEYS = {
    "requested_profile", "registered_profiles", "registered_count",
    "entries", "runtime_module", "handler_module",
}
_ENTRY_KEYS = {
    "profile", "browser_connected", "adapter_module", "loop_alive",
    "generation_id", "gateway_pid", "hub_pid",
}


def test_no_adapter_diagnostics_are_actionable_and_secret_free(home):
    manager = PluginManager()
    _load_plugin_real(manager)

    payload = json.loads(tool_registry.dispatch("run_tab_js", {"code": "1"}))
    assert payload["success"] is False
    assert payload["error"] == "browser sidebar is not connected"
    diag = payload["diagnostics"]
    assert set(diag) <= _DIAGNOSTICS_KEYS
    assert diag["requested_profile"] == str(home)
    assert diag["registered_count"] == 0
    assert diag["registered_profiles"] == []
    assert diag["runtime_module"] == "plugins.platforms.browser_sidebar.runtime"
    assert diag["handler_module"]  # which adapter.py instance answered
    assert TOKEN not in json.dumps(payload)


def test_browser_offline_diagnostics_report_attached_adapter(home):
    """Hub attached but no browser WebSocket: the error must show the adapter
    IS registered for this profile with browser_connected false."""
    manager = PluginManager()
    _load_plugin_real(manager)

    gateway = _GatewayLoop()
    gateway.start()
    rig = _SidebarRig(gateway, attach_browser=False)
    try:
        rig.start()
        payload = json.loads(tool_registry.dispatch("run_tab_js", {"code": "1"}))
        assert payload["success"] is False
        assert payload["error"] == "browser sidebar is not connected"
        diag = payload["diagnostics"]
        assert diag["registered_count"] == 1
        [entry] = diag["entries"]
        assert set(entry) <= _ENTRY_KEYS
        assert entry["profile"] == str(home)
        assert entry["browser_connected"] is False
    finally:
        rig.stop()
        gateway.stop()
