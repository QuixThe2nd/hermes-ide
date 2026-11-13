"""The model must be told when the backend replaced its container/sandbox
mid-command (ported from lobehub/lobehub#19329): silent recovery leaves the
agent assuming background processes and unsynced files survived."""

from tools.environments.local import LocalEnvironment


def test_execute_folds_recreation_flag_once():
    """A pending recreation mark surfaces as ``environment_recreated: True``
    on the next execute() result and is consumed — the following command
    reports a clean result. Exercises the real BaseEnvironment.execute path."""
    env = LocalEnvironment(cwd=".", timeout=30)
    try:
        env._mark_recreated()
        result = env.execute("echo hi")
        assert result["returncode"] == 0
        assert result.get("environment_recreated") is True

        result2 = env.execute("echo again")
        assert "environment_recreated" not in result2
    finally:
        env.cleanup()
