"""The delegate_research tool: schema, action validation, gating, registration.

Covers TASK.md test areas 1 (registration/check_fn/schema/action validation),
4 (start returns before runner completion), 10 (result never returns partial as
completed), and 12 (real plugin discovery registers the tool in ``web``).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from plugins.deep_research import jobs, tool

BRIEF = "Compare the durability guarantees of the three queue candidates."


@pytest.fixture()
def profile_home(tmp_path: Path, monkeypatch) -> Path:
    """A temp HERMES_HOME whose configured worker profile exists."""
    home = tmp_path / "home"
    (home / "profiles" / "researcher").mkdir(parents=True)
    (home / "profiles" / "researcher" / "config.yaml").touch()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture()
def launcher_stub(monkeypatch, profile_home: Path):
    calls: list[dict] = []

    def _launch(job_id, hermes_home, config, timeout_minutes):
        calls.append(
            {
                "job_id": job_id,
                "home": str(hermes_home),
                "timeout": timeout_minutes,
                "state_at_spawn": jobs.read_status(
                    jobs.job_dir(job_id, Path(hermes_home))
                )["state"],
            }
        )
        return {
            "runner_mode": "fallback",
            "runner_unit": None,
            "runner_pid": None,
            "runner_pid_start": None,
            "runner_scope": "fallback",
            "runner_reason": "no usable systemd service manager (user manager unreachable); using detached fallback",
        }

    monkeypatch.setattr(tool, "launch_job", _launch)
    return calls


def _call(args: dict, **kwargs) -> dict:
    return json.loads(tool.handle_delegate_research(args, **kwargs))


def _start(launcher_stub, **overrides) -> dict:
    args = {"action": "start", "brief": BRIEF}
    args.update(overrides)
    return _call(args, session_id="sess-1", task_id="task-1")


# ---------------------------------------------------------------------------
# Area 1: schema + action validation
# ---------------------------------------------------------------------------


class TestSchema:
    def test_single_tool_single_schema_in_web_toolset(self) -> None:
        schema = tool.DELEGATE_RESEARCH_SCHEMA
        assert schema["name"] == "delegate_research"
        props = schema["parameters"]["properties"]
        assert props["action"]["enum"] == ["start", "status", "cancel", "result", "list"]
        assert schema["parameters"]["required"] == ["action"]
        # No command or path parameter is ever accepted from the model.
        assert set(props) == {
            "action",
            "brief",
            "research_questions",
            "timeout_minutes",
            "max_parallel",
            "job_id",
        }
        assert props["job_id"]["pattern"] == "^rj_[0-9a-f]{12}$"
        assert props["research_questions"]["minItems"] == 1
        assert props["research_questions"]["maxItems"] == 8
        assert props["timeout_minutes"]["minimum"] == 5
        assert props["timeout_minutes"]["maximum"] == 60
        assert props["max_parallel"]["minimum"] == 1
        assert props["max_parallel"]["maximum"] == 4

    def test_description_states_the_contract(self) -> None:
        description = tool.DELEGATE_RESEARCH_SCHEMA["description"]
        assert "clarified" in description and "brief" in description
        assert "not" in description and "in parallel" in description
        assert "trivia" in description

    @pytest.mark.parametrize(
        "args,code",
        [
            ({"action": "nope"}, "invalid_action"),
            ({}, "invalid_action"),
            ({"action": "start"}, "invalid_brief"),
            ({"action": "start", "brief": "   "}, "invalid_brief"),
            ({"action": "start", "brief": "x", "research_questions": []}, "invalid_research_questions"),
            ({"action": "start", "brief": "x", "research_questions": ["a"] * 9}, "invalid_research_questions"),
            ({"action": "start", "brief": "x", "research_questions": ["a", ""]}, "invalid_research_questions"),
            ({"action": "start", "brief": "x", "research_questions": [1, 2]}, "invalid_research_questions"),
            ({"action": "start", "brief": "x", "timeout_minutes": "soon"}, "invalid_timeout_minutes"),
            ({"action": "start", "brief": "x", "max_parallel": True}, "invalid_max_parallel"),
            ({"action": "status"}, "invalid_job_id"),
            ({"action": "status", "job_id": "guess"}, "invalid_job_id"),
            ({"action": "status", "job_id": "rj_000000000000"}, "unknown_job"),
            ({"action": "cancel", "job_id": "../../../root"}, "invalid_job_id"),
            ({"action": "result", "job_id": "rj_short"}, "invalid_job_id"),
            ({"action": "list", "limit": 0}, "invalid_limit"),
            ({"action": "list", "limit": "many"}, "invalid_limit"),
        ],
    )
    def test_action_validation(self, profile_home: Path, args: dict, code: str) -> None:
        result = _call(args)
        assert result["ok"] is False and result["error"] == code, result

    def test_brief_length_is_bounded(self, profile_home: Path, launcher_stub) -> None:
        assert _start(launcher_stub, brief="x" * 20_001)["error"] == "invalid_brief"


class TestStartKnobs:
    def test_defaults_come_from_config(self, profile_home: Path, launcher_stub) -> None:
        result = _start(launcher_stub)
        assert result["ok"] is True
        assert result["timeout_minutes"] == 30
        assert result["max_parallel"] == 2
        assert result["worker_profile"] == "researcher"
        assert result["lanes"] == {"total": 1}  # no questions → one lane

    def test_values_are_clamped_to_the_schema_window(self, profile_home: Path, launcher_stub) -> None:
        result = _start(launcher_stub, timeout_minutes=999, max_parallel=99)
        assert result["timeout_minutes"] == 60
        assert result["max_parallel"] == 4
        result = _start(launcher_stub, timeout_minutes=1, max_parallel=0)
        assert result["timeout_minutes"] == 5
        assert result["max_parallel"] == 1

    def test_config_profile_is_honoured(self, profile_home: Path, launcher_stub, monkeypatch) -> None:
        (profile_home / "profiles" / "fieldagent").mkdir()
        (profile_home / "config.yaml").write_text(
            "deep_research:\n  worker_profile: fieldagent\n  default_timeout_minutes: 45\n",
            encoding="utf-8",
        )
        result = _start(launcher_stub)
        assert result["worker_profile"] == "fieldagent"
        assert result["timeout_minutes"] == 45

    def test_fallback_durability_is_stated(self, profile_home: Path, launcher_stub) -> None:
        result = _start(launcher_stub)
        assert result["runner_mode"] == "fallback"
        assert result["runner_scope"] == "fallback"
        assert "fallback" in result["durability"]
        # The downgrade reason is honest, not a generic shrug.
        assert "user manager unreachable" in result["durability"]

    def test_schema_documents_the_real_timeout_default(self) -> None:
        description = tool.DELEGATE_RESEARCH_SCHEMA["parameters"]["properties"]["timeout_minutes"]["description"]
        assert "default 30" in description

    def test_start_and_status_surface_the_manager_scope(self, profile_home: Path, monkeypatch) -> None:
        def _system_launch(job_id, hermes_home, config, timeout_minutes):
            return {
                "runner_mode": "systemd",
                "runner_unit": f"hermes-research-{job_id}",
                "runner_pid": None,
                "runner_pid_start": None,
                "runner_scope": "system",
            }

        monkeypatch.setattr(tool, "launch_job", _system_launch)
        result = _start(None)  # type: ignore[arg-type]
        assert result["runner_scope"] == "system"
        assert "durability" not in result  # a real transient service needs no caveat
        status = _call({"action": "status", "job_id": result["job_id"]})
        assert status["runner_scope"] == "system"
        assert status["runner_mode"] == "systemd"

    def test_start_freezes_origin_routing_data(self, profile_home: Path, launcher_stub) -> None:
        # session_key resolution happens once, at start, against the ACTIVE
        # home; request.json then carries enough to route the completion even
        # after a gateway restart (no re-resolution, no live db needed).
        result = _start(launcher_stub)
        origin = jobs.read_request(Path(result["job_dir"]))["origin"]
        assert origin["session_id"] == "sess-1"
        assert origin["task_id"] == "task-1"
        assert origin["hermes_home"] == str(profile_home)
        assert "session_key" in origin  # best-effort; frozen either way

    def test_forced_systemd_failure_fails_the_start(self, profile_home: Path, monkeypatch) -> None:
        # runner_mode=systemd requires a transient service: no silent fallback.
        (profile_home / "config.yaml").write_text(
            "deep_research:\n  runner_mode: systemd\n", encoding="utf-8"
        )
        monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)

        def refusing_launch(**kwargs):
            from plugins.deep_research import launcher

            raise launcher.RunnerLaunchError(
                "forced runner_mode=systemd could not launch a transient service: "
                "user manager unreachable; system manager unreachable"
            )

        monkeypatch.setattr("plugins.deep_research.launcher.launch", refusing_launch)
        result = _start(None)  # type: ignore[arg-type]
        assert result["ok"] is False and result["error"] == "launch_failed"
        assert "forced runner_mode=systemd" in result["message"]
        directory = jobs.research_jobs_root(profile_home) / result["job_id"]
        status = jobs.read_status(directory)
        assert status["state"] == "failed"
        assert status["phase"] == "launch_failed"
        assert "forced runner_mode=systemd" in status["error"]
        # No runner was ever recorded — nothing is pretending to run.
        assert status["runner_mode"] is None and status["runner_scope"] is None


# ---------------------------------------------------------------------------
# Area 4: start returns before runner completion
# ---------------------------------------------------------------------------


class TestStartReturnsImmediately:
    def test_job_is_queued_when_the_runner_spawns(self, profile_home: Path, launcher_stub) -> None:
        started = time.monotonic()
        result = _start(launcher_stub)
        assert time.monotonic() - started < 5  # no research happened inline
        assert launcher_stub[0]["state_at_spawn"] == "queued"
        assert result["state"] == "queued"
        assert result["job_id"] == launcher_stub[0]["job_id"]
        # The response is a handle plus routing, never a report.
        assert "report" not in result
        assert "Do not run web searches in parallel" in result["next"]

    def test_launch_failure_marks_job_failed_not_stuck(self, profile_home: Path, monkeypatch) -> None:
        def boom(*_a, **_k):
            raise RuntimeError("systemd-run: not available")

        monkeypatch.setattr(tool, "launch_job", boom)
        result = _start(None)  # type: ignore[arg-type] # (stub unused: launch raises)
        assert result["ok"] is False and result["error"] == "launch_failed"
        # The half-created job is closed out honestly, not left queued forever.
        directory = jobs.research_jobs_root(profile_home) / result["job_id"]
        status = jobs.read_status(directory)
        assert status["state"] == "failed"
        assert status["phase"] == "launch_failed"
        assert "systemd-run" in status["error"]

    def test_spawn_is_fenced_under_test_isolation(self, profile_home: Path, monkeypatch) -> None:
        monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
        monkeypatch.setenv("HERMES_TEST_ISOLATION", "1")
        with pytest.raises(RuntimeError):
            tool.launch_job("rj_0123456789ab", Path(profile_home), None, 10)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Area 10: result never returns a partial report as completed
# ---------------------------------------------------------------------------


class TestResultGating:
    def _job(self, profile_home: Path, launcher_stub) -> tuple[str, Path]:
        result = _start(launcher_stub, research_questions=["a", "b"])
        directory = Path(result["job_dir"])
        return result["job_id"], directory

    @pytest.mark.parametrize("state", ["queued", "running", "synthesizing", "failed", "cancelled"])
    def test_non_completed_states_never_expose_a_report(
        self, profile_home: Path, launcher_stub, state: str
    ) -> None:
        job_id, directory = self._job(profile_home, launcher_stub)
        if state != "queued":
            jobs.mark_running(directory, {})
        if state == "synthesizing":
            jobs.set_phase(directory, "synthesizing", state=jobs.STATE_SYNTHESIZING)
        elif state in ("failed", "cancelled"):
            jobs.finish_job(directory, state, error="boom" if state == "failed" else None)
        # A stray draft must not leak out as a result either.
        jobs.preserve_draft(directory, "DRAFT BODY THAT MUST NOT BE RETURNED")
        result = _call({"action": "result", "job_id": job_id})
        assert result["ok"] is False and result["error"] == "not_completed"
        assert result["state"] == state
        assert "DRAFT BODY" not in json.dumps(result)
        assert "report_path" not in result
        assert "no report yet" in result["note"]

    def test_completed_returns_report_paths_and_provenance_caveat(
        self, profile_home: Path, launcher_stub
    ) -> None:
        job_id, directory = self._job(profile_home, launcher_stub)
        jobs.publish_report(directory, f"# Answer\n\nSee [s](https://example.org/x).\n")
        jobs.finish_job(directory, jobs.STATE_COMPLETED)
        result = _call({"action": "result", "job_id": job_id})
        assert result["ok"] is True
        assert result["report"].startswith("# Answer")
        assert result["report_path"] == str(directory / "report.md")
        assert result["evidence_path"] == str(directory / "evidence.jsonl")
        caveat = result["citation_check"]
        assert caveat["validated"] == "url-provenance"
        assert "does not prove" in caveat["limitation"]

    def test_status_is_bounded_and_honest(self, profile_home: Path, launcher_stub) -> None:
        job_id, directory = self._job(profile_home, launcher_stub)
        status = _call({"action": "status", "job_id": job_id})
        assert status["state"] == "queued"
        assert status["blocker"].startswith("runner has not picked")
        assert status["lanes"] == {"total": 2, "succeeded": 0, "failed": 0, "running": 0, "pending": 2}
        assert len(json.dumps(status)) < 2_000
        # A long error is truncated, never dumped raw into model context.
        jobs.finish_job(directory, jobs.STATE_FAILED, error="E" * 5_000)
        failed = _call({"action": "status", "job_id": job_id})
        assert len(failed["error"]) <= 400

    def test_list_is_bounded(self, profile_home: Path, launcher_stub) -> None:
        for index in range(3):
            _start(launcher_stub, brief=f"brief {index}")
        listed = _call({"action": "list"})
        assert listed["count"] == 3
        assert len(json.dumps(listed)) < 4_000
        capped = _call({"action": "list", "limit": 1})
        assert capped["count"] == 1

    def test_cancel_then_result_is_cancelled_not_completed(
        self, profile_home: Path, launcher_stub
    ) -> None:
        job_id, directory = self._job(profile_home, launcher_stub)
        jobs.mark_running(directory, {"runner_mode": "fallback", "runner_pid": None})
        assert _call({"action": "cancel", "job_id": job_id})["state"] == "cancelled"
        result = _call({"action": "result", "job_id": job_id})
        assert result["error"] == "not_completed" and result["state"] == "cancelled"

    def test_cancel_never_claims_success_when_the_status_lock_is_refused(
        self, profile_home: Path, launcher_stub, monkeypatch
    ) -> None:
        # Fail-closed lock: cancel must report the refusal as an error, not
        # answer ok with a state it could not record.
        fcntl = pytest.importorskip("fcntl")
        result = _start(launcher_stub)
        job_id, directory = result["job_id"], Path(result["job_dir"])
        monkeypatch.setattr(jobs, "_JOB_LOCK_TIMEOUT_SECONDS", 0.2)
        holder = open(directory / ".status.lock", "a+", encoding="utf-8")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            outcome = _call({"action": "cancel", "job_id": job_id})
            assert outcome["ok"] is False
            assert outcome["error"] == "status_lock_refused"
            assert "could not" in outcome["note"]
            # Nothing landed: the on-disk state is untouched.
            assert jobs.read_status(directory)["state"] == jobs.STATE_QUEUED
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()
        # Lock released: the same cancel records the terminal state.
        assert _call({"action": "cancel", "job_id": job_id})["state"] == "cancelled"


# ---------------------------------------------------------------------------
# Areas 1/12: check_fn + real registration
# ---------------------------------------------------------------------------


class TestCheckFn:
    def test_available_with_profile(self, profile_home: Path) -> None:
        assert tool.availability_error() is None
        assert tool.check_requirements() is True

    def test_hidden_when_profile_missing(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "bare-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        error = tool.availability_error()
        assert error is not None and "researcher" in error
        assert tool.check_requirements() is False

    def test_hidden_when_disabled_in_config(self, profile_home: Path) -> None:
        (profile_home / "config.yaml").write_text(
            "deep_research:\n  enabled: false\n", encoding="utf-8"
        )
        assert tool.check_requirements() is False

    def test_hidden_inside_a_research_worker(self, profile_home: Path, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_RESEARCH_JOB", "rj_0123456789ab")
        assert tool.check_requirements() is False
        assert _call({"action": "start", "brief": "x"})["error"] == "unavailable"

    def test_hidden_when_hermes_bin_is_broken(self, profile_home: Path, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_BIN", "/nonexistent/hermes-wrapper")
        assert tool.check_requirements() is False


class TestRealRegistration:
    """Real plugin discovery registers the tool into the ``web`` toolset."""

    def test_delegate_research_is_in_the_web_toolset(self) -> None:
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
        from toolsets import get_toolset

        web = get_toolset("web")
        assert "delegate_research" in web["tools"]
        assert "web_search" in web["tools"]  # the toolset is extended, not replaced

    def test_registry_entry_dispatches_the_plugin_handler(self) -> None:
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
        from tools.registry import registry

        entry = registry.get_entry("delegate_research")
        assert entry is not None
        assert entry.handler is tool.handle_delegate_research
        result = json.loads(registry.dispatch("delegate_research", {"action": "list"}, session_id="s"))
        assert result["ok"] is True and "jobs" in result

    def test_manifest_declares_default_enabled_and_underscore_dir(self) -> None:
        import yaml

        manifest = yaml.safe_load(
            (Path(tool.__file__).parent / "plugin.yaml").read_text(encoding="utf-8")
        )
        assert manifest["name"] == "deep_research"
        assert manifest["default_enabled"] is True
        assert "delegate_research" in manifest["provides_tools"]
        assert "post_tool_call" in manifest["provides_hooks"]
