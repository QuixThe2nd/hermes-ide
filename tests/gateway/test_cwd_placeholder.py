"""Unit tests for gateway.cwd_placeholder.resolve_placeholder_terminal_cwd."""

from gateway.cwd_placeholder import resolve_placeholder_terminal_cwd


class TestResolvePlaceholderTerminalCwd:
    def test_local_placeholder_uses_messaging_cwd(self):
        assert resolve_placeholder_terminal_cwd(
            configured_cwd=".",
            terminal_backend="local",
            messaging_cwd="/home/user/project",
            home_fallback="/home/user",
        ) == "/home/user/project"

    def test_local_placeholder_falls_back_to_home(self):
        assert resolve_placeholder_terminal_cwd(
            configured_cwd="",
            terminal_backend="local",
            messaging_cwd=None,
            home_fallback="/home/user",
        ) == "/home/user"

    def test_ssh_placeholder_unset(self):
        assert resolve_placeholder_terminal_cwd(
            configured_cwd=".",
            terminal_backend="ssh",
            messaging_cwd="/home/user",
            home_fallback="/home/user",
        ) is None

    def test_explicit_cwd_wins_over_placeholder(self):
        assert resolve_placeholder_terminal_cwd(
            configured_cwd="/srv/project",
            terminal_backend="local",
            messaging_cwd="/home/user/project",
            home_fallback="/home/user",
        ) == "/srv/project"
