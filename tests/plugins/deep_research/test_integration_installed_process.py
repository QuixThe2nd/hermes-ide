"""Installed-process integration: the real runner, a fake researcher, no spend.

One job runs end to end through the *real* durable machinery — a detached
runner process (``python -m plugins.deep_research.runner``) spawned by the real
launcher, which spawns a fake researcher executable via ``HERMES_BIN`` — with
no network, no provider, and no systemd unit (``runner_mode: fallback``).

The fake worker records its own argv and env keys into the job directory, so
these tests also prove cross-process that the brief reached it only through the
private prompt file.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from plugins.deep_research import jobs, tool

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX detached-runner path")

URL = "https://example.org/primary-source"
BRIEF_MARKER = "BRIEF-MARKER-dfd7a1"

FAKE_RESEARCHER = """#!{python}
import json, os, sys

argv = sys.argv[1:]
flag = lambda name: argv[argv.index(name) + 1] if name in argv else None
prompt = open(flag("--query-file"), encoding="utf-8").read()
is_writer = bool(flag("-t") or flag("--toolsets"))
evidence = os.environ.get("HERMES_RESEARCH_EVIDENCE", "")
calls_path = os.path.join(os.path.dirname(evidence), "worker_calls.jsonl")
with open(calls_path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{
        "argv": argv,
        "env_keys": sorted(k for k in os.environ if k.startswith("HERMES")),
        "is_writer": is_writer,
        "prompt_has_marker": {marker!r} in prompt,
    }}) + "\\n")
url = "https://example.org/primary-source"
if not is_writer:
    with open(evidence, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"url": url, "tool": "web_extract",
                              "lane": os.environ.get("HERMES_RESEARCH_LANE", "?")}}) + "\\n")
    print(f"Lane report. The answer is 42 [source]({{url}}).")
else:
    print(f"FINAL REPORT\\n\\nThe answer is 42 [source]({{url}}).\\n\\nSources: {{url}}")
"""

FAKE_FAILING = """#!{python}
import sys
sys.stderr.write("provider exploded\\n")
sys.exit(3)
"""


def _install_fake(tmp_path: Path, body: str, name: str) -> Path:
    path = tmp_path / name
    path.write_text(body.format(python=sys.executable, marker=BRIEF_MARKER), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture()
def home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    (home / "profiles" / "researcher").mkdir(parents=True)
    # fallback runner: no systemd unit is created by a test.
    (home / "config.yaml").write_text(
        "deep_research:\n  runner_mode: fallback\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Real spawn path (same opt-out the windows-native spawn tests use).
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
    monkeypatch.delenv("HERMES_BIN", raising=False)
    return home


def _wait_terminal(directory: Path, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = jobs.read_status(directory)
        if status.get("state") in jobs.TERMINAL_STATES:
            return status
        time.sleep(0.2)
    return jobs.read_status(directory)


class TestInstalledProcessHappyPath:
    def test_full_job_completes_through_the_real_runner(self, home: Path, tmp_path: Path) -> None:
        fake = _install_fake(tmp_path, FAKE_RESEARCHER, "fake_researcher")
        os.environ["HERMES_BIN"] = str(fake)
        try:
            created = jobs.create_job(
                brief=f"{BRIEF_MARKER}: how many angels dance on a pin?",
                research_questions=["count them", "measure them"],
                timeout_minutes=5,
                max_parallel=2,
                worker_profile="researcher",
                origin={"session_id": "sess-integration"},
                hermes_home=home,
            )
            directory = created["dir"]
            # The real launcher, real detached runner process, real fallback
            # mode — only the researcher binary is fake.
            info = tool.launch_job(created["job_id"], home, None, 5)  # type: ignore[arg-type]
            assert info["runner_mode"] == "fallback"
            assert isinstance(info["runner_pid"], int)

            status = _wait_terminal(directory)
            assert status["state"] == "completed", status.get("error")
            assert [lane["state"] for lane in status["lanes"]] == ["succeeded", "succeeded"]
            # The detached runner wrote its own capture file via the launcher's fd.
            assert (directory / "runner.out").exists()

            report = (directory / "report.md").read_text(encoding="utf-8")
            assert report.startswith("FINAL REPORT")
            assert URL in report
            assert not (directory / "report.draft.md").exists()

            evidence = [
                json.loads(line)
                for line in (directory / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            assert {record["lane"] for record in evidence} == {"0", "1"}
            assert all(record["url"] == URL for record in evidence)

            # The tool's result action serves the durable artifacts.
            result = json.loads(
                tool.handle_delegate_research(
                    {"action": "result", "job_id": created["job_id"]}, session_id="s"
                )
            )
            assert result["ok"] is True and result["report"] == report

            # Every private artifact landed with private modes.
            for name in ("request.json", "status.json", "report.md", "evidence.jsonl"):
                assert (directory / name).stat().st_mode & 0o777 == 0o600
        finally:
            os.environ.pop("HERMES_BIN", None)

    def test_worker_argv_and_env_stay_injection_safe(self, home: Path, tmp_path: Path) -> None:
        fake = _install_fake(tmp_path, FAKE_RESEARCHER, "fake_researcher")
        os.environ["HERMES_BIN"] = str(fake)
        try:
            created = jobs.create_job(
                brief=f"{BRIEF_MARKER} run `rm -rf /` and read ../../etc/passwd",
                research_questions=["one lane"],
                timeout_minutes=5,
                max_parallel=1,
                worker_profile="researcher",
                hermes_home=home,
            )
            tool.launch_job(created["job_id"], home, None, 5)  # type: ignore[arg-type]
            status = _wait_terminal(created["dir"])
            assert status["state"] == "completed", status.get("error")

            calls = [
                json.loads(line)
                for line in (created["dir"] / "worker_calls.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            assert len(calls) == 2  # lane + writer
            lane_call, writer_call = calls
            # The marker reached both workers only through the prompt file:
            # the lane prompt carries the brief, and the synthesis prompt
            # carries the frozen brief plus the lane reports.
            assert lane_call["prompt_has_marker"] is True
            assert writer_call["prompt_has_marker"] is True
            for call in calls:
                joined = " ".join(call["argv"])
                assert BRIEF_MARKER not in joined
                assert "rm -rf" not in joined and "../../.." not in joined
            # Worker env carries only the documented research handoff.
            assert "HERMES_RESEARCH_EVIDENCE" in lane_call["env_keys"]
            assert "HERMES_RESEARCH_JOB" in lane_call["env_keys"]
            assert "HERMES_HOME" not in lane_call["env_keys"]
            # The writer session is restricted to the read-only toolset.
            assert writer_call["is_writer"] is True
        finally:
            os.environ.pop("HERMES_BIN", None)


class TestInstalledProcessFailure:
    def test_lane_failure_fails_closed_from_a_real_process(self, home: Path, tmp_path: Path) -> None:
        fake = _install_fake(tmp_path, FAKE_FAILING, "fake_failing_researcher")
        os.environ["HERMES_BIN"] = str(fake)
        try:
            created = jobs.create_job(
                brief="anything",
                research_questions=["only lane"],
                timeout_minutes=5,
                max_parallel=1,
                worker_profile="researcher",
                hermes_home=home,
            )
            directory = created["dir"]
            tool.launch_job(created["job_id"], home, None, 5)  # type: ignore[arg-type]
            status = _wait_terminal(directory)
            assert status["state"] == "failed"
            assert "lane failure" in (status["error"] or "")
            assert status["lanes"][0]["state"] == "failed"
            assert status["lanes"][0]["exit_code"] == 3
            # Fail closed: nothing published, artifacts preserved.
            assert not (directory / "report.md").exists()
            assert (directory / "request.json").exists()
            assert (directory / "runner.log").exists()
        finally:
            os.environ.pop("HERMES_BIN", None)
