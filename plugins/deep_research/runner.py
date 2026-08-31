"""Durable research job runner.

Invoked host-owned (transient systemd user service, or detached fallback) as::

    python -m plugins.deep_research.runner --job <job_id> --hermes-home <dir>

It never talks to a model itself. Each lane is one ``researcher``-profile
one-shot session (``chat -Q --query-file <private prompt>``) that inherits the
job's evidence-ledger env so the plugin's ``post_tool_call`` hook records the
sources it actually fetched. Synthesis is a second one-shot with
``--toolsets file_readonly`` — the writer has no retrieval tools at all.

Fail-closed by design: a lane failure, a writer failure, a citation
validation failure after the single correction pass, or an exhausted job
budget marks the job ``failed`` with every artifact preserved and no
``report.md`` published.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from plugins.deep_research import citations as citations_mod
from plugins.deep_research import jobs, prompts
from plugins.deep_research.config import DeepResearchConfig, load_deep_research_config
from plugins.deep_research.evidence import EVIDENCE_ENV, LANE_ENV
from plugins.deep_research.launcher import resolve_worker_argv

# Secrets never appear here by construction: only ids, states, codes, seconds.
LOG_MAX_BYTES = 256 * 1024
LOG_BACKUPS = 1
_STDERR_TAIL_CHARS = 300

RESEARCH_JOB_ENV = "HERMES_RESEARCH_JOB"
WRITER_TOOLSETS = "file_readonly"

SpawnResult = Tuple[int, str, str]  # (exit_code, stdout, stderr_tail)
Spawner = Callable[[Sequence[str], Dict[str, str], float], SpawnResult]


class JobAborted(Exception):
    """The job left the active states (cancelled/interrupted) mid-run."""


class BudgetExhausted(JobAborted):
    """The job's own time budget ran out mid-run."""


def default_spawn(argv: Sequence[str], env: Dict[str, str], timeout: float) -> SpawnResult:
    """Run one worker session. Returns ``(exit_code, stdout, bounded stderr)``."""
    try:
        proc = subprocess.run(  # noqa: S603 — argv list built here, never a shell
            [str(part) for part in argv],
            capture_output=True,
            text=True,
            timeout=max(1.0, timeout),
            check=False,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "worker session exceeded its time budget"
    except OSError as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout or "", (proc.stderr or "")[-_STDERR_TAIL_CHARS:]


def build_worker_argv(
    worker_argv: Sequence[str],
    profile: str,
    prompt_path: Path,
    *,
    toolsets: Optional[str] = None,
) -> List[str]:
    """The one-shot worker command. The prompt travels by file path, never argv."""
    argv: List[str] = [str(part) for part in worker_argv]
    if profile:
        argv += ["-p", profile]
    argv += ["chat", "-Q", "--query-file", str(prompt_path)]
    if toolsets:
        argv += ["-t", toolsets]
    return argv


def worker_env(
    *,
    evidence: Path,
    lane: Optional[int],
    research_job: Optional[str] = None,
) -> Dict[str, str]:
    """Env for a worker session.

    ``HERMES_HOME`` is dropped so ``-p <profile>`` owns profile resolution in
    the child (same rule as the cron scheduler). The research handoff vars are
    absolute values this runner chose — never derived from untrusted content.
    """
    env = {key: value for key, value in os.environ.items() if key not in ("HERMES_HOME", "HERMES_TUI")}
    env[EVIDENCE_ENV] = str(evidence)
    if lane is not None:
        env[LANE_ENV] = str(lane)
    if research_job:
        env[RESEARCH_JOB_ENV] = research_job
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


class ResearchRunner:
    def __init__(
        self,
        job_id: str,
        hermes_home: Path,
        *,
        config: Optional[DeepResearchConfig] = None,
        worker_argv: Optional[Sequence[str]] = None,
        spawn: Spawner = default_spawn,
        logger: Optional[logging.Logger] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.job_id = job_id
        self.hermes_home = Path(hermes_home)
        self.config = config or load_deep_research_config()
        self.worker_argv = list(worker_argv) if worker_argv is not None else resolve_worker_argv()
        self.spawn = spawn
        self.log = logger or _job_logger(jobs.job_dir(job_id, self.hermes_home))
        self._clock = clock
        self._aborted = threading.Event()

    # -- public entry ------------------------------------------------------

    def run(self) -> str:
        """Drive the job to a terminal state and return that state."""
        directory = jobs.resolve_existing_job(self.job_id, self.hermes_home)
        request = jobs.read_request(directory)
        status = jobs.read_status(directory)
        if status.get("state") in jobs.TERMINAL_STATES:
            return str(status["state"])  # already decided (e.g. cancelled pre-start)

        brief = str(request.get("brief") or "")
        questions = request.get("research_questions") or []
        objectives: List[str] = [str(q) for q in questions] or [brief]
        profile = str(request.get("worker_profile") or self.config.worker_profile)
        budget_seconds = float(request.get("timeout_minutes") or self.config.default_timeout_minutes) * 60
        max_parallel = int(request.get("max_parallel") or self.config.max_parallel)
        self.deadline = self._clock() + budget_seconds

        jobs.mark_running(directory, {})
        self.log.info(
            "job %s running: %d lane(s), max_parallel=%d, budget=%ds, profile=%s",
            self.job_id, len(objectives), max_parallel, int(budget_seconds), profile,
        )

        try:
            outcomes = self._run_lanes(directory, brief, objectives, profile, max_parallel)
            failed = [o for o in outcomes if not o["ok"]]
            if failed:
                # Fail closed: no partial synthesis over a missing lane.
                self._finish(
                    directory,
                    jobs.STATE_FAILED,
                    error="lane failure: " + "; ".join(
                        f"lane {o['index']} (exit {o['exit_code']}) {o['error']}".strip()
                        for o in failed[:3]
                    ),
                )
                return jobs.STATE_FAILED
            self._run_synthesis(directory, brief, profile)
            return jobs.read_status(directory).get("state") or jobs.STATE_FAILED
        except BudgetExhausted as exc:
            # The job's own budget ran out: land a terminal state before this
            # process exits, or the job would sit in running/synthesizing
            # forever with nothing left to notify about. finish_job refuses an
            # already-terminal status, so an explicit cancellation always wins.
            self.log.warning("job %s aborted: %s", self.job_id, exc)
            if self._finish(
                directory, jobs.STATE_FAILED, error="budget exhausted", phase="budget_exhausted"
            ):
                jobs.mark_lanes_failed(directory, "budget exhausted")
            return jobs.read_status(directory).get("state") or jobs.STATE_FAILED
        except JobAborted:
            self.log.info("job %s aborted mid-run; leaving recorded terminal state", self.job_id)
            return jobs.read_status(directory).get("state") or jobs.STATE_FAILED
        except Exception as exc:  # noqa: BLE001 — the runner must always land somewhere
            self.log.exception("job %s crashed", self.job_id)
            self._finish(directory, jobs.STATE_FAILED, error=f"runner error: {exc}")
            return jobs.STATE_FAILED

    # -- lanes -------------------------------------------------------------

    def _run_lanes(
        self,
        directory: Path,
        brief: str,
        objectives: List[str],
        profile: str,
        max_parallel: int,
    ) -> List[Dict[str, Any]]:
        evidence = jobs.evidence_path(directory)
        for index, objective in enumerate(objectives):
            jobs.write_prompt(directory, f"lane_{index}", prompts.lane_prompt(brief, objective))

        def one_lane(index: int, objective: str) -> Dict[str, Any]:
            if self._aborted.is_set():
                return {"index": index, "ok": False, "exit_code": None, "error": "aborted"}
            self._check_active(directory)
            jobs.update_lane(directory, index, state=jobs.LANE_RUNNING, error=None)
            self.log.info("lane %d starting", index)
            argv = build_worker_argv(self.worker_argv, profile, directory / "prompts" / f"lane_{index}.md")
            started = time.monotonic()
            code, stdout, stderr = self.spawn(
                argv, worker_env(evidence=evidence, lane=index, research_job=self.job_id),
                self._remaining("lane"),
            )
            elapsed = int(time.monotonic() - started)
            jobs.write_lane_report(directory, index, stdout)
            if code == 0 and stdout.strip():
                jobs.update_lane(directory, index, state=jobs.LANE_SUCCEEDED, exit_code=code, error=None)
                self.log.info("lane %d succeeded in %ds", index, elapsed)
                outcome = {"index": index, "ok": True, "exit_code": code, "error": None}
            else:
                reason = stderr.strip() or ("empty worker output" if code == 0 else f"exit {code}")
                jobs.update_lane(directory, index, state=jobs.LANE_FAILED, exit_code=code, error=reason)
                self.log.warning("lane %d failed in %ds: %s", index, elapsed, reason)
                outcome = {"index": index, "ok": False, "exit_code": code, "error": reason}
            # A terminal state recorded under us (cancel) stops further lanes.
            self._check_active(directory)
            return outcome

        with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as pool:
            futures = [pool.submit(one_lane, index, objective) for index, objective in enumerate(objectives)]
            try:
                return [future.result() for future in futures]
            finally:
                self._aborted.set()  # release any queued lane still waiting
                pool.shutdown(wait=True)

    # -- synthesis ---------------------------------------------------------

    def _run_synthesis(self, directory: Path, brief: str, profile: str) -> None:
        lanes = [lane.get("index", i) for i, lane in enumerate(jobs.read_status(directory).get("lanes") or [])]
        reports = [jobs.read_lane_report(directory, index) for index in lanes]
        reports = [report for report in reports if report.strip()]
        if not reports:
            self._finish(directory, jobs.STATE_FAILED, error="no lane reports to synthesize")
            return

        jobs.set_phase(directory, "synthesizing", state=jobs.STATE_SYNTHESIZING)
        self.log.info("synthesis starting over %d lane report(s)", len(reports))
        evidence_urls = jobs.read_evidence_urls(directory)
        if not evidence_urls:
            self._finish(
                directory, jobs.STATE_FAILED,
                error="no fetched sources recorded in the evidence ledger; refusing to synthesize",
            )
            return

        prompt = prompts.synthesis_prompt(brief, reports)
        draft, code, stderr = self._write(directory, "synthesis", prompt, profile, "synthesis")
        self._record_synthesis(directory, attempts=1, correction_used=False, citation_errors=[])
        if code != 0 or not draft.strip():
            self._preserve_and_fail(directory, draft, f"synthesis writer failed: {stderr or f'exit {code}'}")
            return

        verdict = citations_mod.validate_citations(draft, evidence_urls)
        if verdict.ok:
            self._publish(directory, draft)
            return

        self.log.warning("synthesis draft failed citation validation: %s", "; ".join(verdict.errors))
        corrected, code, stderr = self._write(
            directory, "correction",
            prompts.correction_prompt(brief, draft, verdict.errors, evidence_urls),
            profile, "correction",
        )
        if code != 0 or not corrected.strip():
            self._preserve_and_fail(
                directory, corrected or draft,
                f"correction writer failed: {stderr or f'exit {code}'}",
            )
            return
        retry = citations_mod.validate_citations(corrected, evidence_urls)
        self._record_synthesis(
            directory, attempts=2, correction_used=True, citation_errors=retry.errors
        )
        if retry.ok:
            self._publish(directory, corrected)
            return
        self._preserve_and_fail(
            directory, corrected,
            "citation validation failed after correction pass: " + "; ".join(retry.errors),
        )

    def _write(self, directory: Path, name: str, prompt: str, profile: str, phase: str) -> Tuple[str, int, str]:
        """One writer session. Returns ``(text, exit_code, stderr_tail)``."""
        self._check_active(directory)
        prompt_path = jobs.write_prompt(directory, name, prompt)
        argv = build_worker_argv(
            self.worker_argv, profile, prompt_path, toolsets=WRITER_TOOLSETS
        )
        code, stdout, stderr = self.spawn(
            argv,
            # No lane: the writer does no retrieval, so it records no evidence.
            worker_env(evidence=jobs.evidence_path(directory), lane=None, research_job=self.job_id),
            self._remaining(phase),
        )
        self.log.info("%s writer exited %d", phase, code)
        return stdout, code, stderr

    def _publish(self, directory: Path, text: str) -> None:
        self._check_active(directory)
        jobs.publish_report(directory, text)
        if self._finish(directory, jobs.STATE_COMPLETED):
            self.log.info("report published")
        else:
            # Cancel won the race to the terminal state: retract the report so
            # a cancelled job never looks complete.
            try:
                (directory / "report.md").unlink()
            except OSError:
                pass
            self.log.info("job cancelled during publish; report retracted")

    def _preserve_and_fail(self, directory: Path, draft: str, error: str) -> None:
        if draft.strip():
            jobs.preserve_draft(directory, draft)
        self.log.error("job failed: %s", error)
        self._finish(directory, jobs.STATE_FAILED, error=error)

    # -- helpers -----------------------------------------------------------

    def _remaining(self, phase: str) -> float:
        remaining = self.deadline - self._clock()
        if remaining <= 0:
            raise BudgetExhausted(f"{phase} budget exhausted")
        # The true remainder — never a floor that would stretch a worker past
        # the advertised job budget. 1s is the smallest a subprocess timeout
        # accepts, so a nearly-spent budget still gets its graceful moment.
        return max(1.0, remaining)

    def _check_active(self, directory: Path) -> None:
        state = jobs.read_status(directory).get("state")
        if state not in jobs.ACTIVE_STATES:
            self._aborted.set()
            raise JobAborted(f"job state is {state}")

    def _record_synthesis(
        self, directory: Path, *, attempts: int, correction_used: bool, citation_errors: List[str]
    ) -> None:
        def mutate(status: Dict[str, Any]) -> None:
            status["synthesis"] = {
                "attempts": attempts,
                "correction_used": correction_used,
                "citation_errors": list(citation_errors),
            }

        jobs.update_status(directory, mutate=mutate)

    def _finish(
        self, directory: Path, state: str, *, error: Optional[str] = None, phase: Optional[str] = None
    ) -> bool:
        """Land the terminal state. ``False`` when a cancel/interrupt beat us."""
        self._aborted.set()
        finished = jobs.finish_job(directory, state, error=error, phase=phase)
        if finished is None:
            self.log.info("job %s already terminal; not overriding", self.job_id)
            return False
        self.log.info("job %s -> %s", self.job_id, state)
        return True


def _job_logger(directory: Path) -> logging.Logger:
    logger = logging.getLogger(f"hermes.research.{directory.name}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            directory / "runner.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        try:
            os.chmod(directory / "runner.log", jobs.FILE_MODE)
        except OSError:
            pass
    return logger


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="plugins.deep_research.runner")
    parser.add_argument("--job", required=True, help="canonical job id")
    parser.add_argument("--hermes-home", required=True, help="HERMES_HOME holding the job")
    args = parser.parse_args(argv)

    try:
        runner = ResearchRunner(args.job, Path(args.hermes_home))
    except (ValueError, FileNotFoundError) as exc:
        print(f"deep research runner: {exc}", file=sys.stderr)
        return 2
    state = runner.run()
    return 0 if state == jobs.STATE_COMPLETED else 1


if __name__ == "__main__":
    sys.exit(main())
