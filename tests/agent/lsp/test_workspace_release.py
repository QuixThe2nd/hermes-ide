"""Workspace-scoped teardown: removed worktrees and deleted roots must not keep language servers alive.

A gateway outlives the coding sessions it runs, so a ``(server, root)`` client that survives its
worktree is an unbounded leak (multi-GiB tsserver heaps for trees that no longer exist).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from agent.lsp.manager import LSPService
from agent.lsp.servers import SERVERS, ServerContext, ServerDef, SpawnSpec
from agent.lsp.workspace import clear_cache

MOCK_SERVER = str(Path(__file__).parent / "_mock_lsp_server.py")


def _make_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    (repo / "x.py").write_text("x = 1\n", encoding="utf-8")
    return repo


@pytest.fixture
def mock_pyright(request):
    """Install the mock as ``pyright``; ``request.param`` selects the multi-root shape."""
    idx = next(i for i, s in enumerate(SERVERS) if s.server_id == "pyright")
    original = SERVERS[idx]

    def _spawn(root: str, ctx: ServerContext) -> SpawnSpec:
        return SpawnSpec(command=[sys.executable, MOCK_SERVER], workspace_root=root, cwd=root,
                         env={"MOCK_LSP_SCRIPT": "errors"}, initialization_options={})

    SERVERS[idx] = ServerDef(
        server_id="pyright", extensions=original.extensions, resolve_root=lambda fp, ws: ws,
        build_spawn=_spawn, seed_first_push=False, description="mock pyright",
        multi_root=getattr(request, "param", False),
    )
    clear_cache()
    yield
    SERVERS[idx] = original
    clear_cache()


def _service() -> LSPService:
    return LSPService(enabled=True, wait_mode="document", wait_timeout=3.0, install_strategy="manual",
                      idle_timeout=600.0)


@pytest.mark.parametrize("mock_pyright", [False, True], ids=["single-root", "multi-root"], indirect=True)
def test_release_workspace_tears_down_only_that_workspace(mock_pyright, tmp_path):
    """Releasing one root shuts down (single-root) or detaches (multi-root) exactly that root: the
    sibling keeps serving diagnostics, the released state is gone everywhere, and a repeat is a no-op."""
    gone, kept = _make_repo(tmp_path, "gone"), _make_repo(tmp_path, "kept")
    svc = _service()
    try:
        svc.snapshot_baseline(str(gone / "x.py"))
        assert svc.get_diagnostics_sync(str(gone / "x.py"), delta=False)
        assert svc.get_diagnostics_sync(str(kept / "x.py"), delta=False)
        clients = dict(svc._clients)
        procs = {key: c._proc for key, c in clients.items()}  # asyncio subprocess handles

        assert svc.release_workspace(str(gone)) == (0 if len(clients) == 1 else 1)

        assert all(str(gone) not in f for c in svc._clients.values() for f in c.workspace_folders)
        assert all(not p.startswith(str(gone)) for p in svc._delta_baseline)
        if len(clients) == 2:  # single-root: the released process is gone, the sibling's is not
            (gone_key,) = [k for k in clients if k[1] == str(gone)]
            assert gone_key not in svc._clients and gone_key not in svc._last_used
            assert procs[gone_key].returncode is not None, "released server process must have exited"
        else:  # multi-root: one shared process, only the folder was dropped
            (client,) = svc._clients.values()
            assert client.workspace_folders == [str(kept)] and client.is_running
        assert svc.release_workspace(str(gone)) == 0
        assert svc.get_diagnostics_sync(str(kept / "x.py"), delta=False)
    finally:
        svc.shutdown()


def test_reaper_shuts_down_client_whose_root_was_deleted(mock_pyright, tmp_path):
    """A root deleted outside Hermes is reaped on the next sweep even though the client is not idle."""
    repo = _make_repo(tmp_path, "repo")
    svc = _service()
    try:
        assert svc.get_diagnostics_sync(str(repo / "x.py"), delta=False)
        (key,) = svc._clients
        proc = svc._clients[key]._proc

        svc._loop.run(svc._reap_idle_once(), timeout=10.0)
        assert key in svc._clients, "an existing, recently used root must survive the sweep"

        shutil.rmtree(repo)
        svc._loop.run(svc._reap_idle_once(), timeout=10.0)
        assert key not in svc._clients and key not in svc._last_used
        assert proc.returncode is not None, "reaped server process must have exited"
    finally:
        svc.shutdown()
