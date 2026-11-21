"""``_is_interactive()`` must not trust ``isatty()`` alone: DEVNULL/detached stdin must not
count as interactive, or a background gateway would launch browser OAuth."""



def test_non_tty_stdin_is_not_interactive(monkeypatch):
    import io
    from tools import mcp_oauth
    monkeypatch.setattr(mcp_oauth.sys, "stdin", io.StringIO())
    assert mcp_oauth._stdin_is_console() is False
    assert mcp_oauth._is_interactive() is False
