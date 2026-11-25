"""Per-turn terminal scope isolation under profile multiplexing (#68559 class).

One multiplexed process serves several profiles, but terminal.* used to
resolve through the process-global ``TERMINAL_*`` env vars bridged once at
startup — so every routed profile inherited the launch profile's backend,
cwd, docker mounts and shared-container key (#68559, #94200, #101132,
#95470). ``tools.terminal_scope`` installs the routed profile's COMPLETE
terminal policy as a ContextVar at each profile boundary; readers resolve
ONLY from it (omitted key → defined default, never ``os.environ``) and an
unresolvable policy fails closed.
"""

import json
import os

import pytest

from tools.terminal_scope import (
    TerminalPolicyRefusal,
    TerminalPolicyUnavailable,
    get_terminal_scope,
    install_profile_terminal_scope,
    reset_terminal_scope,
    set_terminal_scope,
    terminal_env,
)

_LAUNCH_CWD = "/home/launch-user/private"
_LAUNCH_VOLUMES = '["/host/secret:/data:rw"]'


@pytest.fixture(autouse=True)
def _polluted_launch_env(monkeypatch, tmp_path):
    """Launch profile A bridged a docker backend with sensitive policy into
    the process env; every test proves a routed profile observes none of it."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CWD", _LAUNCH_CWD)
    monkeypatch.setenv("TERMINAL_DOCKER_VOLUMES", _LAUNCH_VOLUMES)
    monkeypatch.setenv("TERMINAL_DOCKER_SHARED_CONTAINER_KEY", "alpha-shared")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "10.10.0.103")
    monkeypatch.setattr("agent.secret_scope.build_profile_secret_scope", lambda _h: {})
    monkeypatch.setattr("hermes_cli.env_loader.hydrate_profile_secret_sources", lambda _h: None)
    import tools.terminal_tool as tt

    monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", True)
    yield


def _profile(tmp_path, name, config_yaml="", dotenv=""):
    home = tmp_path / "profiles" / name
    home.mkdir(parents=True)
    if config_yaml:
        (home / "config.yaml").write_text(config_yaml, encoding="utf-8")
    if dotenv:
        (home / ".env").write_text(dotenv, encoding="utf-8")
    return home


def test_no_scope_keeps_process_env_behavior():
    """Single-process CLI/TUI (no scope bound) is byte-identical to before."""
    assert terminal_env("TERMINAL_ENV") == "docker"
    assert terminal_env("TERMINAL_SSH_HOST") == "10.10.0.103"


def test_scoped_read_never_falls_through_to_process_env():
    """Omitted key under a scope → defined default, NOT the ambient value."""
    token = set_terminal_scope({"TERMINAL_ENV": "local"})
    try:
        assert terminal_env("TERMINAL_ENV") == "local"
        assert terminal_env("TERMINAL_SSH_HOST") == ""
        assert terminal_env("TERMINAL_DOCKER_VOLUMES", "[]") == "[]"
        assert os.environ["TERMINAL_ENV"] == "docker"  # never mutated
    finally:
        reset_terminal_scope(token)


@pytest.mark.parametrize(
    "config_yaml,dotenv",
    [
        pytest.param("terminal:\n  backend: local\n  cwd: {cwd}\n", "", id="config-yaml"),
        pytest.param("", "TERMINAL_ENV=local\nTERMINAL_CWD={cwd}\n", id="dotenv-only"),
    ],
)
def test_routed_turn_reads_every_terminal_consumer_from_profile(
    tmp_path, config_yaml, dotenv
):
    """Leak matrix through the REAL gateway boundary: a routed local profile
    with its own cwd must be seen as such by every terminal.* consumer —
    terminal_tool config, container key resolution, file_tools/runtime_cwd
    cwd anchors, and env_probe backend checks — with none of launch profile
    A's backend policy showing through."""
    import gateway.run as gw
    import tools.terminal_tool as tt
    from agent import runtime_cwd
    from tools import env_probe, file_tools_paths

    b_cwd = tmp_path / "b-work"
    b_cwd.mkdir()
    home = _profile(
        tmp_path, "bee",
        config_yaml.format(cwd=b_cwd), dotenv.format(cwd=b_cwd),
    )

    with gw._profile_runtime_scope(home):
        cfg = tt._get_env_config()
        assert cfg["env_type"] == "local"
        assert cfg["cwd"] == str(b_cwd)
        assert cfg["ssh_host"] == ""
        assert tt._resolve_container_task_id(None) == "default"
        assert file_tools_paths._configured_terminal_cwd() == str(b_cwd)
        assert runtime_cwd.resolve_agent_cwd() == b_cwd
        # env_probe bails out with "" for remote backends; a local profile
        # must not be treated as remote just because the launch env isn't.
        assert env_probe._resolve_terminal_backend() == "local"
    assert get_terminal_scope() is None
    # Process env untouched — the launch profile's own turns are unchanged.
    assert os.environ["TERMINAL_SSH_HOST"] == "10.10.0.103"


def test_profile_omitting_keys_gets_defaults_not_launch_values(tmp_path):
    """#101132/#95470: a profile that omits terminal keys gets the config
    defaults, never the launch profile's bridged env values."""
    import gateway.run as gw
    import tools.terminal_tool as tt

    home = _profile(tmp_path, "bee", "terminal:\n  backend: ssh\n")
    with gw._profile_runtime_scope(home):
        cfg = tt._get_env_config()
        assert cfg["env_type"] == "ssh"
        assert cfg["ssh_host"] == ""
        assert cfg["cwd"] != _LAUNCH_CWD
    assert os.environ["TERMINAL_SSH_HOST"] == "10.10.0.103"  # A unchanged


def test_malformed_profile_config_refuses_execution(tmp_path):
    """Unresolvable policy → refusal scope; terminal_tool refuses instead of
    running under the launch process's ambient policy (fail closed)."""
    from tools.terminal_tool import terminal_tool

    home = _profile(tmp_path, "broken", "terminal: [unclosed\n")
    token = install_profile_terminal_scope(home)
    try:
        assert isinstance(get_terminal_scope(), TerminalPolicyRefusal)
        with pytest.raises(TerminalPolicyUnavailable):
            terminal_env("TERMINAL_ENV")
        result = terminal_tool(command="whoami")
        assert "terminal policy unavailable" in result
    finally:
        reset_terminal_scope(token)


def test_gateway_runtime_scope_resets_on_error(tmp_path):
    import gateway.run as gw

    home = _profile(tmp_path, "qa", "terminal:\n  backend: local\n")
    with pytest.raises(RuntimeError):
        with gw._profile_runtime_scope(home):
            assert terminal_env("TERMINAL_ENV") == "local"
            raise RuntimeError("turn blew up")
    assert get_terminal_scope() is None


def test_list_and_dict_terminal_values_serialize_as_json_not_repr():
    """Non-scalar terminal values must serialize as JSON text — Python repr
    (str() of a list/dict) would break every json.loads consumer. The bridge
    and the profile scope builder share this helper."""
    from hermes_cli.config import _terminal_env_value

    assert _terminal_env_value(["/tmp/a:/data"]) == '["/tmp/a:/data"]'
    assert _terminal_env_value({"FOO": "bar"}) == '{"FOO": "bar"}'
    assert _terminal_env_value("host.example.test") == "host.example.test"


def test_retired_docker_list_keys_do_not_bridge_from_config(tmp_path):
    """Legacy docker list/dict keys left in a profile's config.yaml bridge
    nothing into the terminal scope after the backend retirement
    (c7442dd1745, #708); the backend string still passes through."""
    from tools.terminal_scope import build_profile_terminal_scope

    home = _profile(
        tmp_path,
        "docker-lists",
        "terminal:\n"
        "  backend: docker\n"
        "  docker_forward_env:\n"
        "    - EMAIL_HOME_ADDRESS\n"
        "  docker_volumes:\n"
        "    - /tmp/a:/data\n"
        "  docker_env:\n"
        "    FOO: bar\n",
    )
    scope = build_profile_terminal_scope(home)
    assert scope["TERMINAL_ENV"] == "docker"
    assert "TERMINAL_DOCKER_FORWARD_ENV" not in scope
    assert "TERMINAL_DOCKER_VOLUMES" not in scope
    assert "TERMINAL_DOCKER_ENV" not in scope


def test_dotenv_json_strings_stay_json_strings(tmp_path):
    """The .env path already stores JSON text; str() must keep that payload."""
    from tools.terminal_scope import build_profile_terminal_scope

    home = _profile(
        tmp_path,
        "dotenv-json",
        "terminal:\n  backend: docker\n",
        'TERMINAL_DOCKER_FORWARD_ENV=["EMAIL_HOME_ADDRESS"]\n'
        'TERMINAL_DOCKER_VOLUMES=["/tmp/a:/data"]\n',
    )
    scope = build_profile_terminal_scope(home)
    assert json.loads(scope["TERMINAL_DOCKER_FORWARD_ENV"]) == ["EMAIL_HOME_ADDRESS"]
    assert json.loads(scope["TERMINAL_DOCKER_VOLUMES"]) == ["/tmp/a:/data"]


def test_launch_turn_binds_terminal_scope_once_multiplexing_is_active(
    tmp_path, monkeypatch
):
    """#107422: after multiplexing starts, launch turns bind the launch home's
    own terminal policy (mirrors ``prompt_turn._prepare_turn_input``'s
    ``elif _served_profile_homes`` branch) so poisoned ambient os.environ is
    never the authority."""
    from tools.terminal_scope import (
        get_terminal_scope,
        install_profile_terminal_scope,
        reset_terminal_scope,
    )

    launch_home = tmp_path / ".hermes"
    launch_home.mkdir()
    (launch_home / "config.yaml").write_text(
        "terminal:\n  backend: local\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    # Poison ambient the way the pre-fix latch did — launch scope must win.
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_DOCKER_IMAGE", "bee/img:1")

    token = install_profile_terminal_scope(launch_home)
    try:
        assert get_terminal_scope() is not None
        assert terminal_env("TERMINAL_ENV") == "local"
        # DEFAULT_CONFIG may backfill docker_image; the poisoned bee image must not win.
        assert terminal_env("TERMINAL_DOCKER_IMAGE", "") != "bee/img:1"
        assert os.environ["TERMINAL_ENV"] == "docker"
        assert os.environ["TERMINAL_DOCKER_IMAGE"] == "bee/img:1"
    finally:
        reset_terminal_scope(token)
    assert get_terminal_scope() is None
