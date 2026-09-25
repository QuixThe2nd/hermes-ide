"""In-memory fake Cursor Cloud Agents API for recovery tests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Dict, Optional, Tuple


def _noop_popen(*args, **kwargs):
    class _Proc:
        pid = 9999
        returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    return _Proc()


class FakeCursorCloud:
    def __init__(self) -> None:
        self.agents: Dict[str, Dict[str, Any]] = {}
        self.runs: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.create_calls = 0
        self.poll_calls = 0
        self.get_run_calls = 0
        self.get_agent_calls = 0
        self.list_calls = 0
        self.fail_create = False
        self.fail_receipt_write = False
        self.block_after_create = False
        self.mismatch_create_agent_id = False
        self.terminal_status = "FINISHED"
        self.result_text = "cloud done"
        self.on_create: Optional[Callable[[Dict[str, Any]], None]] = None

    def reset_counters(self) -> None:
        self.create_calls = 0
        self.poll_calls = 0
        self.get_run_calls = 0
        self.get_agent_calls = 0
        self.list_calls = 0

    def seed_running(
        self,
        *,
        agent_id: str,
        run_id: str,
        status: str = "CREATING",
    ) -> None:
        self.agents[agent_id] = {
            "id": agent_id,
            "name": "hermes-seeded",
            "url": f"https://cursor.com/agents/{agent_id}",
            "latestRunId": run_id,
            "status": status,
            "env": {"type": "machine", "name": "hermes-seeded"},
            "repos": [{"url": "https://github.com/acme/demo"}],
        }
        self.runs[(agent_id, run_id)] = {
            "id": run_id,
            "agentId": agent_id,
            "status": status,
            "result": "",
        }

    def seed_terminal(
        self,
        *,
        agent_id: str,
        run_id: str,
        status: str = "FINISHED",
        result_text: str | None = None,
    ) -> None:
        self.seed_running(agent_id=agent_id, run_id=run_id, status=status)
        run = self.runs[(agent_id, run_id)]
        run["status"] = status
        run["result"] = result_text if result_text is not None else self.result_text

    def _create(self, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        self.create_calls += 1
        if self.fail_create:
            raise RuntimeError("create blocked by test")
        if self.on_create:
            self.on_create(payload)
        agent_id = str(payload.get("agentId") or "")
        if self.mismatch_create_agent_id and agent_id:
            agent_id = f"{agent_id}-mismatch"
        run_id = f"run-{len(self.runs) + 1}"
        agent = {
            "id": agent_id,
            "name": str(payload.get("name") or "hermes-test"),
            "url": f"https://cursor.com/agents/{agent_id}",
            "latestRunId": run_id,
            "status": "CREATING",
            "env": payload.get("env") or {},
            "repos": payload.get("repos") or [],
        }
        run = {
            "id": run_id,
            "agentId": agent_id,
            "status": "CREATING" if not self.block_after_create else self.terminal_status,
            "result": self.result_text if self.block_after_create else "",
        }
        self.agents[agent_id] = agent
        self.runs[(agent_id, run_id)] = run
        return deepcopy(agent), deepcopy(run)

    def _get_agent(self, agent_id: str) -> Dict[str, Any]:
        self.get_agent_calls += 1
        agent = self.agents.get(agent_id)
        if agent is None:
            from tools.cursor_agent_tool import CursorCloudError

            raise CursorCloudError(f"agent {agent_id} not found")
        return deepcopy(agent)

    def _get_run(self, agent_id: str, run_id: str) -> Dict[str, Any]:
        self.get_run_calls += 1
        run = self.runs.get((agent_id, run_id))
        if run is None:
            from tools.cursor_agent_tool import CursorCloudError

            raise CursorCloudError(f"run {agent_id}/{run_id} not found")
        return deepcopy(run)

    def _list_agents(self) -> Dict[str, Any]:
        self.list_calls += 1
        return {"items": [deepcopy(item) for item in self.agents.values()]}

    def install(self, monkeypatch, cursor_agent_tool, *, tmp_path, worker_popen=_noop_popen) -> None:
        secret = tmp_path / "cursor-cloud.env"
        secret.write_text("CURSOR_API_KEY=test-secret-key\n", encoding="utf-8")
        monkeypatch.setattr(cursor_agent_tool, "CURSOR_CLOUD_ENV_PATH", secret)
        monkeypatch.setattr(cursor_agent_tool, "resolve_cursor_agent_binary", lambda: "/usr/bin/agent")
        monkeypatch.setattr(
            cursor_agent_tool,
            "resolve_workdir_origin",
            lambda workdir: "https://github.com/acme/demo",
        )
        monkeypatch.setattr(cursor_agent_tool, "resolve_workdir_starting_ref", lambda workdir: "main")
        monkeypatch.setattr(cursor_agent_tool, "WORKER_READY_ATTEMPTS", 1)
        monkeypatch.setattr(cursor_agent_tool, "WORKER_READY_DELAY_SECONDS", 0)
        monkeypatch.setattr(cursor_agent_tool, "POLL_INTERVAL_SECONDS", 0)
        monkeypatch.setattr(cursor_agent_tool.subprocess, "Popen", worker_popen)
        monkeypatch.setattr(cursor_agent_tool, "preflight_worker_auth", lambda *a, **k: None)

        cloud = self

        def _create_agent_with_timeout_dedupe(payload, api_key):
            return cloud._create(payload)

        def _poll_cloud_run(**kwargs):
            cloud.poll_calls += 1
            agent_id = kwargs["agent_id"]
            run_id = kwargs["run_id"]
            run = cloud.runs[(agent_id, run_id)]
            run["status"] = cloud.terminal_status
            run["result"] = cloud.result_text
            return deepcopy(run)

        def _http_request(method, path, *, api_key, json_body=None, timeout=30.0, params=None):
            del api_key, timeout, params
            if method == "GET" and path.startswith("/v1/agents/") and "/runs/" in path:
                _, _, tail = path.partition("/v1/agents/")
                agent_id, _, run_id = tail.partition("/runs/")
                return cloud._get_run(agent_id, run_id)
            if method == "GET" and path.startswith("/v1/agents/"):
                agent_id = path.rsplit("/", 1)[-1]
                return cloud._get_agent(agent_id)
            if method == "GET" and path == "/v1/agents":
                return cloud._list_agents()
            if method == "POST" and path == "/v1/agents":
                agent, run = cloud._create(json_body or {})
                return {"agent": agent, "run": run}
            raise AssertionError(f"unexpected fake cloud request: {method} {path}")

        monkeypatch.setattr(cursor_agent_tool, "create_agent_with_timeout_dedupe", _create_agent_with_timeout_dedupe)
        monkeypatch.setattr(cursor_agent_tool, "poll_cloud_run", _poll_cloud_run)
        monkeypatch.setattr(cursor_agent_tool, "_http_request", _http_request)
