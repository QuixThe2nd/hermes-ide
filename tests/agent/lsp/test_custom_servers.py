"""Config-declared LSP servers (``lsp.servers.<id>`` with ``extensions``) route files and spawn like built-ins."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from agent.lsp.manager import LSPService
from agent.lsp.servers import SERVERS, ServerContext, custom_servers, find_server_for_file, language_id_for

_MOCK = os.path.join(os.path.dirname(__file__), "_mock_lsp_server.py")


def test_custom_server_precedes_builtins_and_carries_language_id():
    cfg = {
        "pyright": {"disabled": True},  # override of a built-in id is NOT a custom server
        "blade": {"command": [sys.executable, _MOCK], "extensions": [".PHP"], "language_id": "blade"},
        "broken": {"command": "not-a-list", "extensions": [".zzz"]},  # malformed → skipped, not fatal
    }
    custom = custom_servers(cfg)
    assert [s.server_id for s in custom] == ["blade"]
    registry = [*custom, *SERVERS]
    assert find_server_for_file("/w/index.php", registry).server_id == "blade"  # custom wins over intelephense
    assert find_server_for_file("/w/index.php").server_id == "intelephense"  # built-in registry untouched
    assert language_id_for("/w/index.php", custom[0]) == "blade"
    assert language_id_for("/w/index.php") == "php"
    spec = custom[0].build_spawn("/w", ServerContext("/w", install_strategy="manual"))
    assert spec is not None and spec.command == [sys.executable, _MOCK]


@pytest.mark.timeout(60)
def test_service_gets_diagnostics_from_config_declared_server(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    target = tmp_path / "doc.pnch"
    target.write_text("hello\n")
    (tmp_path / "x.rs").write_text("fn main() {}\n")
    svc = LSPService(
        enabled=True, wait_mode="document", wait_timeout=5.0, install_strategy="manual",
        env_overrides={"panache": {"MOCK_LSP_SCRIPT": "errors"}}, idle_timeout=0,
        extra_servers=custom_servers({"panache": {"command": [sys.executable, _MOCK], "extensions": [".pnch"]}}),
    )
    try:
        assert svc.enabled_for(str(target))
        diags = svc.get_diagnostics_sync(str(target))
        assert diags and diags[0]["source"] == "mock-lsp"
        # Control: a built-in extension still routes to the registry entry, not the custom server.
        assert svc._server_for(str(tmp_path / "x.rs")).server_id == "rust-analyzer"
    finally:
        svc.shutdown()
