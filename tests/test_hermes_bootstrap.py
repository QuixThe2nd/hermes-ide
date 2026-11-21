"""Tests for hermes_bootstrap — process-level bootstrap helpers.

The bootstrap module is imported at the top of every Hermes entry point
(hermes, hermes-agent, gateway, batch_runner, cli.py).

Key invariants covered by these tests:

  1. Load order: every Hermes entry point imports hermes_bootstrap as its
     first non-docstring import (before anything that might do file I/O
     or print to stdout)
  2. harden_import_path keeps the source root ahead of a stale cwd
  3. The happy-eyeballs socket racer applies to http.client and urllib3 connects
"""

from __future__ import annotations

import errno
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


# Import the module under test via an import-time side-effect check path.
# We need to be able to reset its state between tests, so we import it
# fresh in each test.
def _fresh_import():
    """Return a freshly-imported hermes_bootstrap module.

    Drops any cached copy from sys.modules first so module-level code
    runs again and the platform check re-evaluates.
    """
    sys.modules.pop("hermes_bootstrap", None)
    import hermes_bootstrap  # noqa: WPS433
    return hermes_bootstrap




class TestEntryPointsImportBootstrap:
    """Every Hermes entry point must import hermes_bootstrap as its
    first non-docstring import.  We check this by scanning source files
    rather than invoking the entry points (which would require a full
    agent context)."""

    # Entry points that invoke Hermes as a process.  Each one must
    # import hermes_bootstrap before doing any file I/O or stdout writes.
    ENTRY_POINTS = [
        "hermes_cli/main.py",   # hermes CLI (console_script)
        "run_agent.py",          # hermes-agent (console_script)
        "gateway/run.py",        # gateway
        "batch_runner.py",       # batch mode
        "cli.py",                # legacy direct-launch CLI
    ]

    @pytest.mark.parametrize("path", ENTRY_POINTS)
    def test_entry_point_imports_bootstrap(self, path):
        """The file must contain 'import hermes_bootstrap' and that
        line must appear before the first 'import' of anything else.

        We're lenient about the docstring (can be arbitrarily long) and
        about comment lines — just need to verify the first import
        statement is the bootstrap.

        Also lenient about a try/except wrapper around the import: entry
        points may guard the import against ``ModuleNotFoundError`` so a
        half-finished ``hermes update`` (git-reset landed new code but
        ``uv pip install -e .`` didn't finish re-registering
        ``hermes_bootstrap`` as a top-level module) leaves hermes
        recoverable instead of crashing on every invocation.  When the
        first top-level node is such a guarded-import block, we peek
        inside it to verify bootstrap is the imported module.
        """
        # Resolve relative to the hermes-agent repo root.  Tests live
        # at tests/test_hermes_bootstrap.py, so go up one dir.
        import pathlib
        here = pathlib.Path(__file__).resolve()
        repo_root = here.parent.parent  # tests/ -> repo root
        full_path = repo_root / path
        assert full_path.exists(), f"entry point missing: {full_path}"

        source = full_path.read_text(encoding="utf-8")

        # Find the first non-comment, non-blank line that starts with
        # 'import ' or 'from ', or a Try block whose body is the import.
        import ast
        tree = ast.parse(source)

        first_import_node = None
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                first_import_node = node
                break
            # Accept a guarded-import Try block where the body is a lone
            # Import node — this is the recovery-friendly form that lets
            # hermes start even when hermes_bootstrap hasn't been
            # re-registered in the venv yet.
            if isinstance(node, ast.Try) and len(node.body) == 1 and isinstance(
                node.body[0], (ast.Import, ast.ImportFrom)
            ):
                first_import_node = node.body[0]
                break

        assert first_import_node is not None, (
            f"{path}: no top-level imports found at all"
        )

        if isinstance(first_import_node, ast.Import):
            first_import_name = first_import_node.names[0].name
        else:  # ImportFrom
            first_import_name = first_import_node.module or ""

        assert first_import_name == "hermes_bootstrap", (
            f"{path}: first top-level import is {first_import_name!r}, "
            f"but it must be 'hermes_bootstrap' so UTF-8 stdio is "
            f"configured before anything else initializes.  Move the "
            f"'import hermes_bootstrap' line to be the first import."
        )


class TestHardenImportPath:
    """harden_import_path() must keep a same-named package in the launch
    directory from shadowing Hermes's own top-level modules — covering both
    the relative ('' / '.') and absolute-path forms the cwd can take on
    sys.path (issue #51286)."""

    def _run(self, hb, path_seed, env=None):
        original = sys.path[:]
        original_env = os.environ.get("HERMES_PYTHON_SRC_ROOT")
        try:
            sys.path[:] = path_seed
            if env is not None:
                os.environ["HERMES_PYTHON_SRC_ROOT"] = env
            elif "HERMES_PYTHON_SRC_ROOT" in os.environ:
                del os.environ["HERMES_PYTHON_SRC_ROOT"]
            hb.harden_import_path(src_root="/opt/hermes")
            return sys.path[:]
        finally:
            sys.path[:] = original
            if original_env is None:
                os.environ.pop("HERMES_PYTHON_SRC_ROOT", None)
            else:
                os.environ["HERMES_PYTHON_SRC_ROOT"] = original_env

    def test_relative_cwd_forms_removed(self):
        hb = _fresh_import()
        result = self._run(hb, ["", ".", "/opt/hermes", "/usr/lib/python"])
        assert "" not in result
        assert "." not in result

    def test_src_root_forced_to_front(self):
        hb = _fresh_import()
        result = self._run(hb, ["", "/opt/hermes", "/usr/lib/python"])
        assert result[0] == "/opt/hermes"

    def test_absolute_cwd_path_loses_to_src_root(self):
        # The real #51286 bug: the launch dir is present as its own absolute
        # path (venv activation / a project on PYTHONPATH), ahead of the
        # Hermes root.  The guard must relocate Hermes to the front.
        hb = _fresh_import()
        result = self._run(hb, ["/home/user/tg-ws-proxy", "/opt/hermes"])
        assert result[0] == "/opt/hermes"
        # The cwd absolute path may still appear (it can hold legit deps),
        # but only AFTER the Hermes root.
        assert result.index("/opt/hermes") < result.index("/home/user/tg-ws-proxy")


    def test_env_var_used_when_no_arg(self):
        hb = _fresh_import()
        original = sys.path[:]
        original_env = os.environ.get("HERMES_PYTHON_SRC_ROOT")
        try:
            sys.path[:] = ["", "/cwd/proj", "/usr/lib"]
            os.environ["HERMES_PYTHON_SRC_ROOT"] = "/env/hermes"
            hb.harden_import_path()
            assert sys.path[0] == "/env/hermes"
        finally:
            sys.path[:] = original
            if original_env is None:
                os.environ.pop("HERMES_PYTHON_SRC_ROOT", None)
            else:
                os.environ["HERMES_PYTHON_SRC_ROOT"] = original_env




class TestHappyEyeballsSocketConnect:
    """Importing the bootstrap races IPv6/IPv4 for every sync connect in the process (#114265)."""

    def test_import_routes_http_client_and_urllib3_connects_through_the_racer(self):
        import http.client

        import urllib3
        import urllib3.util.connection as urllib3_connection

        hb = _fresh_import()
        assert socket.create_connection.__module__ == hb.__name__
        # urllib3 keeps its own serial connect walker; it is patched once imported (lazily).
        assert getattr(urllib3_connection.create_connection, "_hermes_happy_eyeballs", False)
        # Re-importing the bootstrap (or importing it after urllib3) never wraps the racer twice.
        racer = socket.create_connection
        _fresh_import()
        assert socket.create_connection is racer
        # The bootstrap must not pay urllib3's import (~50 ms) on every process start: a fresh
        # interpreter gets the patch the moment urllib3 loads, not before.
        subprocess.run([sys.executable, "-c", textwrap.dedent("""
            import sys, hermes_bootstrap
            assert "urllib3" not in sys.modules, "bootstrap imported urllib3 eagerly"
            import urllib3.util.connection as c
            assert c.create_connection._hermes_happy_eyeballs
        """)], check=True, cwd=str(Path(hb.__file__).parent), timeout=60)

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        http_conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        urllib3_conn = urllib3.connection.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            http_conn.connect()
            urllib3_conn.connect()  # exercises the socket_options kwarg of the urllib3 racer
            assert http_conn.sock.getpeername()[1] == port
            assert urllib3_conn.sock.getpeername()[1] == port
        finally:
            http_conn.close()
            urllib3_conn.close()
            listener.close()

    def test_installed_racer_wins_ipv4_while_ipv6_hangs(self, monkeypatch):
        hb = _fresh_import()
        clock = [0.0]
        sockets = []

        class FakeSocket:
            def __init__(self, family, socktype, proto):
                self.family = family
                self.closed = False
                self.timeout = None
                sockets.append(self)

            def setsockopt(self, *_args):
                pass

            def setblocking(self, _blocking):
                pass

            def settimeout(self, timeout):
                self.timeout = timeout

            def connect_ex(self, _address):
                return errno.EINPROGRESS if self.family == socket.AF_INET6 else 0

            def close(self):
                self.closed = True

        class FakeSelector:
            def register(self, *_args):
                pass

            def unregister(self, *_args):
                pass

            def select(self, timeout):
                clock[0] += timeout or 0.0  # the v6 attempt never completes
                return []

            def close(self):
                pass

        monkeypatch.setattr(hb.socket, "getaddrinfo", lambda *_a, **_k: [
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2001:db8::1", 443, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.0.2.1", 443)),
        ])
        monkeypatch.setattr(hb.socket, "socket", FakeSocket)
        monkeypatch.setattr(hb.selectors, "DefaultSelector", FakeSelector)
        monkeypatch.setattr(hb.time, "monotonic", lambda: clock[0])

        # http.client passes the module timeout sentinel through positionally.
        winner = socket.create_connection(("example.com", 443), socket._GLOBAL_DEFAULT_TIMEOUT, None)

        assert winner.family == socket.AF_INET
        assert winner.timeout is None  # sentinel resolves to the process default, like stock
        assert clock[0] == hb._HAPPY_EYEBALLS_DELAY_SECONDS
        assert sockets[0].closed is True and sockets[1] is winner

    def test_racer_bug_raises_instead_of_falling_back_to_the_serial_walk(self, monkeypatch):
        """A non-OSError from the racer is a bug in the racer, not a network outcome: it must
        surface, never silently reroute the connect through the serial stock walker (which
        would reintroduce the exact stall the racer exists to remove). OSError still means
        "every candidate failed" and propagates unchanged."""
        import urllib3.util.connection as urllib3_connection

        _fresh_import()

        def boom(*_args, **_kwargs):
            raise RuntimeError("racer bug")

        for racer in (socket.create_connection, urllib3_connection.create_connection):
            assert getattr(racer, "_hermes_happy_eyeballs", False)
            monkeypatch.setitem(racer.__globals__, "_happy_eyeballs_create_connection", boom)
            with pytest.raises(RuntimeError, match="racer bug"):
                racer(("127.0.0.1", 1), 1.0)
