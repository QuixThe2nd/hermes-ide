from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import types

import pytest

from discord_history import cli
from discord_history.doctor import DCE_SHA256, DCE_VERSION

ROOT = Path(__file__).resolve().parents[1]
SNOW = "12345678901234567"
SYNTHETIC_CHAT_ID = "66666666666666666"
SYNTHETIC_USER_ID = "77777777777777777"


def _plugin_module():
    spec = importlib.util.spec_from_file_location("discord_history_plugin_entry", ROOT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class Context:
    def __init__(self):
        self.tool = self.command = self.skill = None

    def register_tool(self, **kw): self.tool = kw
    def register_cli_command(self, **kw): self.command = kw
    def register_skill(self, **kw): self.skill = kw


def test_exact_plugin_registration():
    module = _plugin_module()
    ctx = Context()
    module.register(ctx)
    assert ctx.tool["name"] == ctx.tool["toolset"] == "discord_history"
    assert ctx.tool["schema"] is module.DISCORD_HISTORY_SCHEMA
    assert ctx.tool["handler"] is module.handle_discord_history
    assert ctx.tool["check_fn"] is module.check_requirements
    assert ctx.tool["description"] == "Search the owner-authorized PostgreSQL Discord archive."
    assert ctx.command == {
        "name": "discord-history",
        "help": "Ingest, search, and verify the Discord history archive",
        "setup_fn": cli.setup_cli,
        "handler_fn": cli.cli_entry,
        "description": "Manage the PostgreSQL/DCE Discord history archive.",
    }
    assert ctx.skill["name"] == "discord-history"
    assert ctx.skill["description"] == "Recall owner-authorized Discord history with exact citations."
    assert ctx.skill["path"] == ROOT / "skills/discord-history/SKILL.md"
    assert ctx.skill["path"].is_file()


def test_plugin_refuses_existing_tool_name_even_same_toolset(monkeypatch):
    class Registry:
        def get_entry(self, name):
            return object() if name == "discord_history" else None
    tools = types.ModuleType("tools")
    registry_module = types.ModuleType("tools.registry")
    setattr(registry_module, "registry", Registry())
    monkeypatch.setitem(__import__("sys").modules, "tools", tools)
    monkeypatch.setitem(__import__("sys").modules, "tools.registry", registry_module)
    module = _plugin_module()
    with pytest.raises(RuntimeError, match="tool_name_collision"):
        module.register(Context())


def parser():
    p = argparse.ArgumentParser()
    cli.setup_cli(p)
    return p


@pytest.mark.parametrize("argv,command", [
    (["init"], "init"), (["doctor"], "doctor"),
    (["inventory", "--guild", SNOW], "inventory"),
    (["sync", "--guild", SNOW], "sync"),
    (["reconcile", "--guild", SNOW], "reconcile"),
    (["status"], "status"), (["verify", "--channel", SNOW], "verify"),
    (["verify-e2e", "--guild", SNOW, "--owner-audit-id", "7",
      "--expected-message-id", SNOW], "verify-e2e"),
])
def test_public_subcommands(argv, command):
    assert parser().parse_args(argv).discord_history_command == command


def test_plan_exact_verify_e2e_interface_leaves_phrase_optional():
    args = parser().parse_args(["verify-e2e", "--guild", SNOW,
                                "--owner-audit-id", "7",
                                "--expected-message-id", SNOW])
    assert args.expected_phrase is None


def test_sync_contract_and_reconcile_alias(monkeypatch, capsys):
    calls = []
    monkeypatch.setitem(cli._COMMANDS, "sync", lambda a: calls.append(vars(a)) or {"ok": True})
    ns = parser().parse_args(["sync", "--guild", SNOW, "--channel", SNOW,
                              "--mode", "backfill", "--keep-export", "--json"])
    assert cli.handle_cli(ns) == 0
    assert calls[0]["mode"] == "backfill" and calls[0]["keep_export"]
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    monkeypatch.setitem(cli._COMMANDS, "reconcile", lambda a: {"ok": a.mode == "reconcile"})
    assert cli.handle_cli(parser().parse_args(["reconcile", "--guild", SNOW])) == 0


def test_json_failure_and_exit_contract(monkeypatch, capsys):
    monkeypatch.setitem(cli._COMMANDS, "doctor", lambda a: {"ok": False, "checks": []})
    assert cli.handle_cli(parser().parse_args(["doctor", "--json"])) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"checks": [], "ok": False}
    monkeypatch.setitem(cli._COMMANDS, "status", lambda a: {"ok": False, "state": "stale"})
    assert cli.handle_cli(parser().parse_args(["status", "--json"])) == 0


def test_verify_e2e_pass_fail(monkeypatch, capsys):
    monkeypatch.setitem(cli._COMMANDS, "verify-e2e",
                        lambda a: {"verdict": "FAIL", "failed_checks": ["freshness"]})
    ns = parser().parse_args(["verify-e2e", "--guild", SNOW, "--owner-audit-id", "1",
                              "--expected-message-id", SNOW, "--expected-phrase", "needle", "--json"])
    assert cli.handle_cli(ns) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "FAIL"
    plain = parser().parse_args(["verify-e2e", "--guild", SNOW, "--owner-audit-id", "1",
                                 "--expected-message-id", SNOW, "--expected-phrase", "needle"])
    assert cli.handle_cli(plain) == 1
    assert capsys.readouterr().out == "FAIL\n"
    with pytest.raises(SystemExit) as exc:
        plain.func(plain)
    assert exc.value.code == 1


def test_invalid_ids_and_channel_cap_are_usage_errors():
    with pytest.raises(SystemExit) as exc:
        parser().parse_args(["inventory", "--guild", "12"])
    assert exc.value.code == 2
    argv = ["sync", "--guild", SNOW] + sum((["--channel", SNOW] for _ in range(101)), [])
    with pytest.raises(SystemExit) as exc:
        parser().parse_args(argv)
    assert exc.value.code == 2


def test_schema_hard_bounds_and_pins():
    module = _plugin_module()
    props = module.DISCORD_HISTORY_SCHEMA["parameters"]["properties"]
    assert props["query"]["maxLength"] == 500
    assert (props["limit"]["default"], props["limit"]["maximum"]) == (10, 50)
    assert props["context_before"]["maximum"] == props["context_after"]["maximum"] == 20
    assert DCE_VERSION == "2.47.3"
    assert DCE_SHA256 == "8f86bd3a2c2f4412ffbbb2dcb9348642f8f929ad94a4f290ff0f78068c44fc86"


def test_handler_authorizes_on_every_invocation(monkeypatch):
    module = _plugin_module()
    calls = []
    fake = SimpleNamespace(handle_discord_history=lambda **kw: calls.append(kw) or "ok")
    monkeypatch.setitem(__import__("sys").modules, "discord_history.service", fake)
    assert module.handle_discord_history(action="status") == "ok"
    assert module.handle_discord_history(action="status") == "ok"
    assert len(calls) == 2


def test_command_wrappers_forward_arguments_merge_probes_and_close(monkeypatch):
    class Connection:
        def __init__(self): self.closed = False
        def execute(self, _sql, _params): return SimpleNamespace(fetchone=lambda: (SNOW,))
        def close(self): self.closed = True

    db = __import__("discord_history.db", fromlist=["apply_migrations"])
    doctor = __import__("discord_history.doctor", fromlist=["run_doctor"])
    service = __import__("discord_history.service", fromlist=["run_sync"])
    config_module = __import__("discord_history.config", fromlist=["load_secrets"])
    verify = __import__("discord_history.verify", fromlist=["verify_e2e"])
    connections = []
    monkeypatch.setattr(cli, "_connection", lambda: connections.append(Connection()) or connections[-1])
    migration_results = [["003.sql"], [], []]
    monkeypatch.setattr(db, "apply_migrations", lambda conn: migration_results.pop(0))
    monkeypatch.setattr(doctor, "run_doctor", lambda: {"ok": True, "checks": []})
    monkeypatch.setattr(service, "run_inventory", lambda **kw: {"ok": True, **kw})
    monkeypatch.setattr(service, "run_sync", lambda **kw: {"ok": True, **kw})
    monkeypatch.setattr(service, "archive_status", lambda **kw: [kw])
    monkeypatch.setattr(service, "run_denial_and_retrieval_probes", lambda: {"denial": True})
    monkeypatch.setattr(service, "run_live_acceptance_probes", lambda **kw: {"live": bool(kw)})
    monkeypatch.setattr(service, "run_channel_verification",
                        lambda channel: {"ok": True, "channel_id": channel,
                                         "checks": {"dce_live_set_equality": True}})
    monkeypatch.setattr(service, "load_plugin_config", lambda: SimpleNamespace(owner_user_ids={SNOW}))
    monkeypatch.setattr(config_module, "load_secrets", lambda: SimpleNamespace(audit_hmac_key=b"k" * 32))

    seen = {}
    monkeypatch.setattr(verify, "verify_e2e", lambda conn, **kw: seen.update(kw) or {"verdict": "PASS"})

    assert cli.cmd_init(argparse.Namespace())["applied"] == ["003.sql"]
    assert cli.cmd_doctor(argparse.Namespace())["ok"] is True
    assert cli.cmd_inventory(argparse.Namespace(guild=SNOW))["guild_id"] == SNOW
    synced = cli.cmd_sync(argparse.Namespace(guild=SNOW, channel=[SNOW], mode="reconcile", keep_export=True))
    assert synced["channel_ids"] == [SNOW] and synced["keep_export"] is True
    assert cli.cmd_status(argparse.Namespace(guild=SNOW, channel=SNOW))["status"][0]["channel_id"] == SNOW
    assert cli.cmd_verify(argparse.Namespace(channel=SNOW))["ok"] is True
    e2e = cli.cmd_verify_e2e(argparse.Namespace(
        guild=SNOW, owner_audit_id="7", expected_message_id=SNOW, expected_phrase=None
    ))
    assert e2e["ok"] is True
    assert seen["expected_phrase"] is None
    assert seen["probes"] == {
        "denial": True,
        "live": True,
        "schema_migration_reapply_noop": True,
        "full_channel_verification": True,
        "channel_dce_live_set_equality": True,
    }
    assert len(seen["owner_principal_hmacs"]) == 1
    assert len(seen["owner_principal_hmacs"][0]) == 64
    assert all(conn.closed for conn in connections)


def test_cli_failures_are_redacted_and_ascii_validated(monkeypatch, capsys):
    assert cli.handle_cli(argparse.Namespace(discord_history_command=None)) == 2
    assert "usage:" in capsys.readouterr().err

    class Failure(RuntimeError):
        code = "stable_code"

    monkeypatch.setitem(cli._COMMANDS, "doctor", lambda _args: (_ for _ in ()).throw(Failure("secret")))
    args = argparse.Namespace(discord_history_command="doctor", json_output=True)
    assert cli.handle_cli(args) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "stable_code"
    monkeypatch.setitem(cli._COMMANDS, "doctor", lambda _args: "invalid")
    assert cli.handle_cli(args) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "operation_failed"

    for value in ("١٢٣٤٥٦٧٨٩٠١٢٣٤٥٦٧", "123"):
        with pytest.raises(argparse.ArgumentTypeError):
            cli.snowflake(value)
    with pytest.raises(argparse.ArgumentTypeError):
        cli.audit_id("٠١")
    with pytest.raises(argparse.ArgumentTypeError):
        cli.expected_phrase("")
    with pytest.raises(argparse.ArgumentTypeError):
        cli.expected_phrase("x\x00y")


def test_cli_entry_returns_zero_and_raises_nonzero(monkeypatch):
    monkeypatch.setattr(cli, "handle_cli", lambda _args: 0)
    assert cli.cli_entry(argparse.Namespace()) == 0
    monkeypatch.setattr(cli, "handle_cli", lambda _args: 2)
    with pytest.raises(SystemExit) as exc:
        cli.cli_entry(argparse.Namespace())
    assert exc.value.code == 2


def _install_fake_session_context(monkeypatch, *, captures):
    """Install a gateway.session_context module capturing set_session_vars/clear_session_vars.

    The plugin running in the deployed hermes venv imports the real one; the
    tests install a stand-in so cmd_recall can be exercised without Hermes core.
    """
    import types
    sentinel = object()

    class _Tokens:
        def __init__(self, payload):
            self.payload = payload

    module = types.ModuleType("gateway.session_context")

    def _set(platform="", chat_id="", thread_id="", user_id="", **_):
        if platform != "discord" or not user_id or not chat_id:
            raise AssertionError(f"unexpected session binding: {platform!r} {chat_id!r} {thread_id!r} {user_id!r}")
        captures["set"].append({"platform": platform, "chat_id": chat_id,
                                "thread_id": thread_id, "user_id": user_id})
        return _Tokens(captures["set"][-1])

    def _clear(tokens):
        captures["cleared"].append(tokens.payload if isinstance(tokens, _Tokens) else None)

    def _set_unset(name):
        return sentinel

    module.set_session_vars = _set
    module.clear_session_vars = _clear
    module._UNSET = sentinel
    module.get_session_env = lambda name, default="": ""
    module._SESSION_PLATFORM = _set_unset("p")
    module._SESSION_USER_ID = _set_unset("u")
    module._SESSION_CHAT_ID = _set_unset("c")
    module._SESSION_THREAD_ID = _set_unset("t")
    gateway = types.ModuleType("gateway")
    gateway.session_context = module
    monkeypatch.setitem(__import__("sys").modules, "gateway", gateway)
    monkeypatch.setitem(__import__("sys").modules, "gateway.session_context", module)
    return module


def test_recall_parser_enforces_snowflake_format_and_required_args():
    p = parser()
    recall = p.parse_args(["recall", "--guild", SNOW, "--user", SNOW, "--thread", SNOW])
    assert recall.discord_history_command == "recall"
    assert recall.action == "status"
    assert recall.thread_id == SNOW
    with pytest.raises(SystemExit):
        p.parse_args(["recall", "--guild", "12", "--user", SNOW])
    with pytest.raises(SystemExit):
        p.parse_args(["recall", "--guild", SNOW])
    with pytest.raises(SystemExit):
        p.parse_args(["recall", "--guild", SNOW, "--user", "12345"])


def test_recall_binds_session_calls_service_and_clears(monkeypatch):
    captures = {"set": [], "cleared": []}
    _install_fake_session_context(monkeypatch, captures=captures)

    service_module = types.SimpleNamespace()
    service_calls = []

    def _fake_handle(arguments):
        service_calls.append(arguments)
        return json.dumps({"action": arguments["action"],
                           "guild_id": arguments["guild_id"],
                           "channels": [{"channel_id": SNOW, "channel_name": "general"}]})

    service_module.handle_discord_history = _fake_handle
    monkeypatch.setitem(__import__("sys").modules,
                        "discord_history.service", service_module)

    args = argparse.Namespace(
        discord_history_command="recall",
        action="status", guild=SNOW, thread_id=SNOW, chat_id=None,
        user_id="11111111111111111", query=None, message_id=None,
        channel_ids=None, limit=None, context_before=None, context_after=None,
        after=None, before=None,
    )
    result = cli.cmd_recall(args)

    assert result["ok"] is True
    assert result["action"] == "status"
    assert captures["set"], "session must be bound"
    binding = captures["set"][-1]
    assert binding == {"platform": "discord", "chat_id": SNOW,
                       "thread_id": SNOW, "user_id": "11111111111111111"}
    assert service_calls == [{"action": "status", "guild_id": SNOW}]
    assert captures["cleared"] == [binding]
    assert result["response"]["channels"][0]["channel_id"] == SNOW


def test_recall_forwards_search_arguments_and_thread_metadata(monkeypatch):
    captures = {"set": [], "cleared": []}
    _install_fake_session_context(monkeypatch, captures=captures)

    service_module = types.SimpleNamespace()
    service_calls = []

    def _fake_handle(arguments):
        service_calls.append(arguments)
        return json.dumps({"action": "search", "guild_id": arguments["guild_id"],
                           "results": [], "query": arguments.get("query")})

    service_module.handle_discord_history = _fake_handle
    monkeypatch.setitem(__import__("sys").modules,
                        "discord_history.service", service_module)

    args = argparse.Namespace(
        discord_history_command="recall",
        action="search", guild=SNOW,
        thread_id="55555555555555555",
        chat_id=SYNTHETIC_CHAT_ID,
        user_id=SYNTHETIC_USER_ID,
        query="verify recall works",
        message_id=None,
        channel_ids=[SNOW, "99999999999999999"],
        limit=5, context_before=None, context_after=None,
        after="2026-01-01T00:00:00Z", before=None,
    )
    result = cli.cmd_recall(args)
    assert result["ok"] is True
    binding = captures["set"][-1]
    assert binding["chat_id"] == SYNTHETIC_CHAT_ID
    assert binding["thread_id"] == "55555555555555555"
    assert binding["user_id"] == SYNTHETIC_USER_ID
    sent = service_calls[-1]
    assert sent == {
        "action": "search", "guild_id": SNOW,
        "query": "verify recall works",
        "channel_ids": [SNOW, "99999999999999999"],
        "limit": 5,
        "after": "2026-01-01T00:00:00Z",
    }
    assert captures["cleared"] == [binding]


def test_recall_surfaces_authorization_failure_with_session_undone(monkeypatch):
    captures = {"set": [], "cleared": []}
    _install_fake_session_context(monkeypatch, captures=captures)

    service_module = types.SimpleNamespace()
    service_module.handle_discord_history = lambda _a: json.dumps({"error": "authorization_failed"})
    monkeypatch.setitem(__import__("sys").modules,
                        "discord_history.service", service_module)

    args = argparse.Namespace(
        discord_history_command="recall",
        action="status", guild=SNOW,
        thread_id=SNOW, chat_id=None,
        user_id="11111111111111111",
        query=None, message_id=None,
        channel_ids=None, limit=None, context_before=None,
        context_after=None, after=None, before=None,
    )
    result = cli.cmd_recall(args)
    assert result["ok"] is False
    assert result["error"] == "authorization_failed"
    assert captures["set"] and captures["cleared"], "session must be cleared even on failure"


def test_recall_disables_bytecode_writes_for_session(monkeypatch):
    captures = {"set": [], "cleared": []}
    _install_fake_session_context(monkeypatch, captures=captures)
    sys_module = __import__("sys")

    class FakeService:
        @staticmethod
        def handle_discord_history(_a):
            return json.dumps({"action": "status", "guild_id": SNOW, "channels": []})

    monkeypatch.setitem(sys_module.modules, "discord_history.service", FakeService)
    pre = sys_module.dont_write_bytecode
    args = argparse.Namespace(
        discord_history_command="recall",
        action="status", guild=SNOW, thread_id=SNOW, chat_id=None,
        user_id="11111111111111111", query=None, message_id=None,
        channel_ids=None, limit=None, context_before=None, context_after=None,
        after=None, before=None,
    )
    try:
        cli.cmd_recall(args)
        assert sys_module.dont_write_bytecode is True
    finally:
        sys_module.dont_write_bytecode = pre


def test_package_bytecode_scrubber_removes_cache_in_deploy_root(tmp_path, monkeypatch):
    """Scrubber must remove __pycache__/ and *.pyc from a configured deploy root
    and leave any other directory alone."""
    from discord_history import _scrub_deployed_tree_bytecode
    deploy = tmp_path / "deploy"
    (deploy / "discord_history").mkdir(parents=True)
    (deploy / "discord_history" / "__pycache__").mkdir()
    (deploy / "discord_history" / "__pycache__" / "tool.cpython-311.pyc").write_bytes(b"x")
    (deploy / "__pycache__").mkdir()
    (deploy / "__pycache__" / "plugin.cpython-311.pyc").write_bytes(b"x")
    # An unrelated tree must be left alone.
    other = tmp_path / "elsewhere" / "__pycache__"
    other.mkdir(parents=True)
    (other / "marker.pyc").write_bytes(b"keep")

    monkeypatch.setenv("HERMES_DISCORD_HISTORY_DEPLOY_ROOT", str(deploy))
    _scrub_deployed_tree_bytecode()

    assert not (deploy / "discord_history" / "__pycache__").exists()
    assert not list(deploy.rglob("*.pyc"))
    assert (other / "marker.pyc").read_bytes() == b"keep"


def test_package_bytecode_scrubber_is_safe_when_deploy_root_missing(tmp_path, monkeypatch):
    from discord_history import _scrub_deployed_tree_bytecode
    monkeypatch.setenv("HERMES_DISCORD_HISTORY_DEPLOY_ROOT", str(tmp_path / "no-such-dir"))
    _scrub_deployed_tree_bytecode()  # must not raise


def test_package_bytecode_scrubber_fails_closed_when_shared_path_helper_fails(
    tmp_path, monkeypatch
):
    """A helper failure must not trigger an independent HERMES_HOME fallback."""
    from discord_history import _scrub_deployed_tree_bytecode
    from discord_history import paths

    home = tmp_path / "profile-home"
    cache = home / "plugins" / "discord-history" / "__pycache__"
    cache.mkdir(parents=True)
    marker = cache / "marker.pyc"
    marker.write_bytes(b"keep")
    monkeypatch.delenv("HERMES_DISCORD_HISTORY_DEPLOY_ROOT", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        paths, "deployed_plugin_root", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    _scrub_deployed_tree_bytecode()

    assert marker.read_bytes() == b"keep"


def test_package_bytecode_guard_arms_for_deploy_path(monkeypatch, tmp_path):
    """Module-level guard must arm when globals().__file__ points inside the deploy dir."""
    sys_module = __import__("sys")
    from discord_history import _disable_bytecode_for_deployed_tree as guard

    deploy_home = tmp_path / "profile-home"
    deploy_root = deploy_home / "plugins" / "discord-history"
    deploy_root.mkdir(parents=True)
    source_root = Path(__file__).resolve().parents[1]

    pre = sys_module.dont_write_bytecode
    saved = guard.__globals__["__file__"]
    try:
        monkeypatch.setenv("HERMES_HOME", str(deploy_home))
        sys_module.dont_write_bytecode = False
        guard.__globals__["__file__"] = str(
            deploy_root / "discord_history" / "__init__.py"
        )
        guard()
        assert sys_module.dont_write_bytecode is True

        sys_module.dont_write_bytecode = False
        guard.__globals__["__file__"] = str(source_root / "discord_history" / "__init__.py")
        guard()
        assert sys_module.dont_write_bytecode is False
    finally:
        guard.__globals__["__file__"] = saved
        sys_module.dont_write_bytecode = pre


def test_recall_rejects_when_chat_supplied_without_thread(monkeypatch):
    args = argparse.Namespace(
        discord_history_command="recall",
        action="status", guild=SNOW,
        thread_id=None, chat_id=SYNTHETIC_CHAT_ID,
        user_id="11111111111111111",
        query=None, message_id=None,
        channel_ids=None, limit=None, context_before=None,
        context_after=None, after=None, before=None,
    )
    result = cli.cmd_recall(args)
    assert result["ok"] is False
    assert result["error"] == "missing_session_thread"


def test_recall_handle_cli_dispatch_emits_json(monkeypatch, capsys):
    def _fake_cmd(_args):
        return {"ok": True, "command": "recall", "action": "status",
                "session": {}, "response": {"channels": []}}
    monkeypatch.setitem(cli._COMMANDS, "recall", _fake_cmd)
    args = argparse.Namespace(discord_history_command="recall", json_output=True)
    assert cli.handle_cli(args) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["ok"] is True
