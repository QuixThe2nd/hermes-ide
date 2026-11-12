import sys
import types
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from hermes_cli.nous_account import NousPortalAccountInfo


TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"


def _load_tool_module(module_name: str, filename: str):
    spec = spec_from_file_location(module_name, TOOLS_DIR / filename)
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _restore_tool_and_agent_modules():
    original_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "tools"
        or name.startswith("tools.")
        or name == "agent"
        or name.startswith("agent.")
        or name in {"fal_client", "openai"}
    }
    try:
        yield
    finally:
        for name in list(sys.modules):
            if (
                name == "tools"
                or name.startswith("tools.")
                or name == "agent"
                or name.startswith("agent.")
                or name in {"fal_client", "openai"}
            ):
                sys.modules.pop(name, None)
        sys.modules.update(original_modules)


@pytest.fixture(autouse=True)
def _enable_managed_nous_tools(monkeypatch):
    """Patch the source modules so managed_nous_tools_enabled() returns True
    even after tool modules are dynamically reloaded."""
    monkeypatch.setattr(
        "hermes_cli.nous_account.get_nous_portal_account_info",
        lambda: NousPortalAccountInfo(
            logged_in=True,
            source="jwt",
            fresh=False,
            paid_service_access=True,
        ),
    )


def _install_fake_tools_package():
    tools_package = types.ModuleType("tools")
    tools_package.__path__ = [str(TOOLS_DIR)]  # type: ignore[attr-defined]
    sys.modules["tools"] = tools_package
    sys.modules["tools.debug_helpers"] = types.SimpleNamespace(
        DebugSession=lambda *args, **kwargs: types.SimpleNamespace(
            active=False,
            session_id="debug-session",
            log_call=lambda *a, **k: None,
            save=lambda: None,
            get_session_info=lambda: {},
        )
    )
    sys.modules["tools.managed_tool_gateway"] = _load_tool_module(
        "tools.managed_tool_gateway",
        "managed_tool_gateway.py",
    )


def _install_fake_fal_client(captured):
    def submit(model, arguments=None, headers=None):
        raise AssertionError("managed FAL gateway mode should use fal_client.SyncClient")

    class FakeResponse:
        def json(self):
            return {
                "request_id": "req-123",
                "response_url": "http://127.0.0.1:3009/requests/req-123",
                "status_url": "http://127.0.0.1:3009/requests/req-123/status",
                "cancel_url": "http://127.0.0.1:3009/requests/req-123/cancel",
            }

    def _capture_request(client, method, url, json=None, timeout=None, headers=None):
        captured["submit_via"] = "managed_client"
        captured["http_client"] = client
        captured["method"] = method
        captured["submit_url"] = url
        captured["arguments"] = json
        captured["timeout"] = timeout
        captured["headers"] = headers
        return FakeResponse()

    def _maybe_retry_request(client, method, url, json=None, timeout=None, headers=None):
        return _capture_request(client, method, url, json, timeout, headers)

    class FakeHttpClient:
        def request(self, method, url, json=None, timeout=None, headers=None):
            return _capture_request(self, method, url, json, timeout, headers)

    class SyncRequestHandle:
        def __init__(self, request_id, response_url, status_url, cancel_url, client):
            captured["request_id"] = request_id
            captured["response_url"] = response_url
            captured["status_url"] = status_url
            captured["cancel_url"] = cancel_url
            captured["handle_client"] = client

    class SyncClient:
        def __init__(self, key=None, default_timeout=120.0):
            captured["sync_client_inits"] = captured.get("sync_client_inits", 0) + 1
            captured["client_key"] = key
            captured["client_timeout"] = default_timeout
            self.default_timeout = default_timeout
            self._client = FakeHttpClient()

    fal_client_module = types.SimpleNamespace(
        submit=submit,
        SyncClient=SyncClient,
        client=types.SimpleNamespace(
            _maybe_retry_request=_maybe_retry_request,
            _raise_for_status=lambda response: None,
            SyncRequestHandle=SyncRequestHandle,
        ),
    )
    sys.modules["fal_client"] = fal_client_module
    return fal_client_module


def test_managed_fal_submit_uses_gateway_origin_and_nous_token(monkeypatch):
    captured = {}
    _install_fake_tools_package()
    _install_fake_fal_client(captured)
    # The fake fal_client above answers every SDK touch; the real lazy-dep gate
    # (a version-pinned metadata check) must not refuse first on an install
    # without the fal extra. Replace the sys.modules entry itself so the
    # loader's ``from tools.lazy_deps import ensure`` resolves to the stub no
    # matter which tools package object a prior test left behind.
    monkeypatch.setitem(
        sys.modules,
        "tools.lazy_deps",
        types.SimpleNamespace(ensure=lambda *args, **kwargs: None),
    )
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.setenv("FAL_QUEUE_GATEWAY_URL", "http://127.0.0.1:3009")
    monkeypatch.setenv("TOOL_GATEWAY_USER_TOKEN", "nous-token")

    image_generation_tool = _load_tool_module(
        "tools.image_generation_tool",
        "image_generation_tool.py",
    )
    monkeypatch.setattr(image_generation_tool.uuid, "uuid4", lambda: "fal-submit-123")
    
    image_generation_tool._submit_fal_request(
        "fal-ai/flux-2-pro",
        {"prompt": "test prompt", "num_images": 1},
    )

    assert captured["submit_via"] == "managed_client"
    assert captured["client_key"] == "nous-token"
    assert captured["submit_url"] == "http://127.0.0.1:3009/fal-ai/flux-2-pro"
    assert captured["method"] == "POST"
    assert captured["arguments"] == {"prompt": "test prompt", "num_images": 1}
    assert captured["headers"] == {"x-idempotency-key": "fal-submit-123"}
    assert captured["sync_client_inits"] == 1
PLUGINS_DIR = Path(__file__).resolve().parents[2] / "plugins"


def _load_video_gen_plugin(monkeypatch):
    """Load the FAL video gen plugin in isolation."""
    _install_fake_tools_package()

    # Also need the agent.video_gen_provider ABC
    agent_dir = Path(__file__).resolve().parents[2] / "agent"
    spec = spec_from_file_location(
        "agent.video_gen_provider",
        agent_dir / "video_gen_provider.py",
    )
    assert spec and spec.loader
    mod = module_from_spec(spec)
    sys.modules["agent.video_gen_provider"] = mod
    spec.loader.exec_module(mod)

    # Load the plugin
    plugin_init = PLUGINS_DIR / "video_gen" / "fal" / "__init__.py"
    spec = spec_from_file_location("plugins.video_gen.fal", plugin_init)
    assert spec and spec.loader
    plugin_mod = module_from_spec(spec)
    sys.modules["plugins.video_gen.fal"] = plugin_mod
    spec.loader.exec_module(plugin_mod)
    return plugin_mod


def test_video_gen_happy_horse_uses_alibaba_namespace():
    """Verify the happy-horse family uses alibaba/ not fal-ai/ endpoints."""
    _install_fake_tools_package()

    # Load just the plugin module to check the catalog
    plugin_init = PLUGINS_DIR / "video_gen" / "fal" / "__init__.py"

    agent_dir = Path(__file__).resolve().parents[2] / "agent"
    spec = spec_from_file_location(
        "agent.video_gen_provider",
        agent_dir / "video_gen_provider.py",
    )
    mod = module_from_spec(spec)
    sys.modules["agent.video_gen_provider"] = mod
    spec.loader.exec_module(mod)

    spec = spec_from_file_location("plugins.video_gen.fal", plugin_init)
    plugin_mod = module_from_spec(spec)
    sys.modules["plugins.video_gen.fal"] = plugin_mod
    spec.loader.exec_module(plugin_mod)

    hh = plugin_mod.FAL_FAMILIES["happy-horse"]
    assert hh["text_endpoint"].startswith("alibaba/happy-horse/") and hh["text_endpoint"].endswith("/text-to-video")
    assert hh["image_endpoint"].startswith("alibaba/happy-horse/") and hh["image_endpoint"].endswith("/image-to-video")
