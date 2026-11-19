from __future__ import annotations

from types import SimpleNamespace

class _FakeAgent:
    def __init__(self):
        self.calls = []
        self.model = "old/model"
        self.provider = "openrouter"

    def switch_model(self, **kwargs):
        self.calls.append(kwargs)
        self.model = kwargs["new_model"]
        self.provider = kwargs["new_provider"]


class _StubCLI:
    model = "old/model"
    provider = "openrouter"
    requested_provider = "openrouter"
    api_key = "sk-old"
    _explicit_api_key = "sk-old"
    base_url = "https://openrouter.ai/api/v1"
    _explicit_base_url = "https://openrouter.ai/api/v1"
    api_mode = "chat_completions"
    agent = None



def test_cli_restore_model_runtime_snapshot_restores_agent():
    import cli as cli_mod

    stub = _StubCLI()
    stub.agent = _FakeAgent()
    snapshot = {
        "model": "old/model",
        "provider": "openrouter",
        "requested_provider": "openrouter",
        "api_key": "sk-old",
        "explicit_api_key": "sk-old",
        "base_url": "https://openrouter.ai/api/v1",
        "explicit_base_url": "https://openrouter.ai/api/v1",
        "api_mode": "chat_completions",
    }

    cli_mod.HermesCLI._restore_model_runtime_snapshot(stub, snapshot)

    assert stub.model == "old/model"
    assert stub.provider == "openrouter"
    assert stub.agent.calls[-1]["new_model"] == "old/model"


def test_cli_restore_model_runtime_prefers_primary_runtime():
    import cli as cli_mod

    class Agent(_FakeAgent):
        _primary_runtime = None
        _rate_limited_until = 123

        def __init__(self):
            super().__init__()
            self.model = "temp/model"
            self.provider = "anthropic"

        def _restore_primary_runtime(self):
            self.model = self._primary_runtime["model"]
            self.provider = self._primary_runtime["provider"]
            return True

    stub = _StubCLI()
    stub.agent = Agent()
    snapshot = {
        "model": "old/model",
        "provider": "openrouter",
        "requested_provider": "openrouter",
        "api_key": "sk-old",
        "explicit_api_key": "sk-old",
        "base_url": "",
        "explicit_base_url": "",
        "api_mode": "chat_completions",
        "agent_primary_runtime": {
            "model": "old/model",
            "provider": "openrouter",
        },
    }

    cli_mod.HermesCLI._restore_model_runtime_snapshot(stub, snapshot)

    assert stub.agent.model == "old/model"
    assert stub.agent.provider == "openrouter"
    assert stub.agent.calls == []
