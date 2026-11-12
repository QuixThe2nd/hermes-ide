import os
import sys
import tempfile
import threading
import types
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli.nous_account import NousPortalAccountInfo


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"


def _load_tool_module(module_name: str, filename: str):
    spec = spec_from_file_location(module_name, TOOLS_DIR / filename)
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module




def _reset_modules(prefixes: tuple[str, ...]):
    for name in list(sys.modules):
        if name.startswith(prefixes):
            sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def _restore_tool_and_agent_modules():
    original_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "tools"
        or name.startswith("tools.")
        or name == "agent"
        or name.startswith("agent.")
    }
    try:
        yield
    finally:
        _reset_modules(("tools", "agent"))
        sys.modules.update(original_modules)


@pytest.fixture(autouse=True)
def _enable_managed_nous_tools(monkeypatch):
    """Ensure managed_nous_tools_enabled() returns True even after module reloads.

    The _install_fake_tools_package() helper resets and reimports tool modules,
    so a simple monkeypatch on tool_backend_helpers doesn't survive.  We patch
    the *source* modules that the reimported modules will import from — both
    hermes_cli.nous_account — so the function body returns True.
    """
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
    _reset_modules(("tools", "agent"))

    tools_package = types.ModuleType("tools")
    tools_package.__path__ = [str(TOOLS_DIR)]  # type: ignore[attr-defined]
    sys.modules["tools"] = tools_package

    env_package = types.ModuleType("tools.environments")
    env_package.__path__ = [str(TOOLS_DIR / "environments")]  # type: ignore[attr-defined]
    sys.modules["tools.environments"] = env_package

    agent_package = types.ModuleType("agent")
    agent_package.__path__ = [str(REPO_ROOT / "agent")]  # type: ignore[attr-defined]
    sys.modules["agent"] = agent_package
    sys.modules["agent.auxiliary_client"] = types.SimpleNamespace(
        call_llm=lambda *args, **kwargs: "",
    )
    # hermes_cli.auth (imported transitively by nous_account /
    # tool_backend_helpers) imports sanitize_borrowed_credential_payload.
    sys.modules["agent.credential_persistence"] = types.SimpleNamespace(
        sanitize_borrowed_credential_payload=lambda entry, provider_id=None: entry,
    )


    sys.modules["tools.managed_tool_gateway"] = _load_tool_module(
        "tools.managed_tool_gateway",
        "managed_tool_gateway.py",
    )

    interrupt_event = threading.Event()
    sys.modules["tools.interrupt"] = types.SimpleNamespace(
        set_interrupt=lambda value=True: interrupt_event.set() if value else interrupt_event.clear(),
        is_interrupted=lambda: interrupt_event.is_set(),
        _interrupt_event=interrupt_event,
    )
    sys.modules["tools.approval"] = types.SimpleNamespace(
        detect_dangerous_command=lambda *args, **kwargs: None,
        check_dangerous_command=lambda *args, **kwargs: {"approved": True},
        check_all_command_guards=lambda *args, **kwargs: {"approved": True},
        load_permanent_allowlist=lambda *args, **kwargs: [],
        DANGEROUS_PATTERNS=[],
    )

    class _Registry:
        def register(self, **kwargs):
            return None

    from tools.registry import tool_error

    sys.modules["tools.registry"] = types.SimpleNamespace(
        registry=_Registry(), tool_error=tool_error,
    )

    class _DummyEnvironment:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def cleanup(self):
            return None

    class _DummyConnectionError(Exception):
        def __init__(self, message="", backend="", detail="", *args):
            super().__init__(message, *args)
            self.backend = backend
            self.detail = detail

    sys.modules["tools.environments.base"] = types.SimpleNamespace(
        BaseEnvironment=_DummyEnvironment,
        EnvironmentConnectionError=_DummyConnectionError,
    )
    sys.modules["tools.environments.local"] = types.SimpleNamespace(LocalEnvironment=_DummyEnvironment)
    sys.modules["tools.environments.singularity"] = types.SimpleNamespace(
        _get_scratch_dir=lambda: Path(tempfile.gettempdir()),
        SingularityEnvironment=_DummyEnvironment,
    )
    sys.modules["tools.environments.ssh"] = types.SimpleNamespace(SSHEnvironment=_DummyEnvironment)
    sys.modules["tools.environments.docker"] = types.SimpleNamespace(DockerEnvironment=_DummyEnvironment)
    sys.modules["tools.environments.modal"] = types.SimpleNamespace(ModalEnvironment=_DummyEnvironment)
    sys.modules["tools.environments.managed_modal"] = types.SimpleNamespace(ManagedModalEnvironment=_DummyEnvironment)




def test_terminal_tool_respects_direct_modal_mode_without_falling_back_to_managed():
    _install_fake_tools_package()
    env = os.environ.copy()
    env.pop("MODAL_TOKEN_ID", None)
    env.pop("MODAL_TOKEN_SECRET", None)

    with patch.dict(os.environ, env, clear=True):
        _load_tool_module("tools.terminal_tool", "terminal_tool.py")
        terminal_tool_backends = sys.modules["tools.terminal_tool_backends"]

        with (
            patch.object(terminal_tool_backends, "is_managed_tool_gateway_ready", return_value=True),
            patch.object(Path, "exists", return_value=False),
        ):
            with pytest.raises(ValueError, match="direct Modal credentials"):
                terminal_tool_backends._create_environment(
                    env_type="modal",
                    image="python:3.11",
                    cwd="/root",
                    timeout=60,
                    container_config={
                        "container_cpu": 1,
                        "container_memory": 2048,
                        "container_disk": 1024,
                        "container_persistent": True,
                        "modal_mode": "direct",
                    },
                    task_id="task-modal-direct-only",
                )


class TestShellEscapeBypass:
    """Regression for #36846/#36847: backslash escapes and empty-string
    literals split tokens so a denylisted command (rm) slips past detection
    while the shell still executes it."""

    def test_backslash_escape_bypass_caught(self):
        from tools.approval import detect_dangerous_command
        # literal: r-backslash-m -rf /  (shell collapses r\m -> rm)
        assert detect_dangerous_command("r\\m -rf /")[0] is True

    def test_empty_string_literal_bypass_caught(self):
        from tools.approval import detect_dangerous_command
        assert detect_dangerous_command("r''m -rf /")[0] is True
        assert detect_dangerous_command('r""m -rf /')[0] is True

    def test_plain_dangerous_still_caught(self):
        from tools.approval import detect_dangerous_command
        assert detect_dangerous_command("rm -rf /")[0] is True

    def test_benign_command_not_flagged(self):
        from tools.approval import detect_dangerous_command
        assert detect_dangerous_command("ls -la")[0] is False
