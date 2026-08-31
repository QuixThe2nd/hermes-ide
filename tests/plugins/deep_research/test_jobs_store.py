"""Durable job store: IDs, paths, private modes, atomic transitions, recovery.

Covers TASK.md test areas 2 (job ID/path traversal and private modes) and 3
(atomic state transitions and stale-job recovery).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from plugins.deep_research import jobs


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def _make_job(home: Path, **overrides) -> tuple[str, Path]:
    kwargs = dict(
        brief="Compare the three main widget runtimes.",
        research_questions=["lane a", "lane b"],
        timeout_minutes=10,
        max_parallel=2,
        worker_profile="researcher",
        hermes_home=home,
    )
    kwargs.update(overrides)
    created = jobs.create_job(**kwargs)
    return created["job_id"], created["dir"]


# ---------------------------------------------------------------------------
# Job IDs and path safety (area 2)
# ---------------------------------------------------------------------------


class TestJobIdValidation:
    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "rj_short",
            "rj_ZZZZZZZZZZZZ",  # uppercase hex rejected
            "rj_00000000000g",  # non-hex tail
            "rj_000000000000/../../etc",  # traversal suffix
            "../../root",
            "/absolute/rj_000000000000",
            "rj_000000000000\n",
            "rj_000000000000 extra",
            None,
            1234,
            ["rj_000000000000"],
        ],
    )
    def test_non_canonical_ids_rejected(self, bad) -> None:
        assert jobs.is_canonical_job_id(bad) is False

    def test_canonical_id_accepted(self) -> None:
        assert jobs.is_canonical_job_id("rj_0123456789ab") is True

    def test_job_dir_refuses_traversal(self, home: Path) -> None:
        with pytest.raises(ValueError):
            jobs.job_dir("rj_000000000000/..", home)
        with pytest.raises(ValueError):
            jobs.job_dir("../../etc/passwd", home)

    def test_job_dir_stays_under_root(self, home: Path) -> None:
        job_id, directory = _make_job(home)
        assert directory == home / "research_jobs" / job_id
        assert directory.parent == jobs.research_jobs_root(home)

    def test_root_is_profile_aware_never_literal(self, tmp_path: Path) -> None:
        # The root follows the caller-provided home; no literal /root anywhere.
        root = jobs.research_jobs_root(tmp_path / "custom")
        assert root == tmp_path / "custom" / "research_jobs"
        assert "/root/" not in str(root) or str(tmp_path).startswith("/root")

    def test_unknown_existing_job_raises(self, home: Path) -> None:
        with pytest.raises(FileNotFoundError):
            jobs.resolve_existing_job("rj_000000000000", home)


class TestPrivateModes:
    def test_job_tree_is_private(self, home: Path) -> None:
        _job_id, directory = _make_job(home)
        assert directory.stat().st_mode & 0o777 == 0o700
        for name in ("request.json", "status.json"):
            assert (directory / name).stat().st_mode & 0o777 == 0o600
        for subdir in ("lanes", "prompts"):
            assert (directory / subdir).stat().st_mode & 0o777 == 0o700
        # Artifacts written later keep the private modes.
        report = jobs.publish_report(directory, "# report\n")
        assert report.stat().st_mode & 0o777 == 0o600
        prompt = jobs.write_prompt(directory, "lane_0", "prompt text")
        assert prompt.stat().st_mode & 0o777 == 0o600
        draft = jobs.preserve_draft(directory, "draft")
        assert draft.stat().st_mode & 0o777 == 0o600
        lane = jobs.write_lane_report(directory, 0, "lane report")
        assert lane.stat().st_mode & 0o777 == 0o600

    def test_prompt_name_validated(self, home: Path) -> None:
        _job_id, directory = _make_job(home)
        for bad in ("../escape", "lane/0", "Lane 0", "..", "lane\\0"):
            with pytest.raises(ValueError):
                jobs.write_prompt(directory, bad, "x")


# ---------------------------------------------------------------------------
# Frozen request + lane structure
# ---------------------------------------------------------------------------


class TestRequestIsFrozen:
    def test_request_snapshot_not_aliased(self, home: Path) -> None:
        questions = ["lane a", "lane b"]
        _job_id, directory = _make_job(home, research_questions=questions)
        questions.append("smuggled lane")  # caller mutation must not leak in
        request = jobs.read_request(directory)
        assert request["research_questions"] == ["lane a", "lane b"]
        assert request["brief"].startswith("Compare")

    def test_no_questions_means_one_brief_lane(self, home: Path) -> None:
        _job_id, directory = _make_job(home, research_questions=None)
        status = jobs.read_status(directory)
        assert len(status["lanes"]) == 1
        assert status["lanes"][0]["question"].startswith("Compare")
        assert jobs.read_request(directory)["research_questions"] is None

    def test_origin_identifiers_recorded(self, home: Path) -> None:
        _job_id, directory = _make_job(
            home, origin={"session_id": "sess-9", "session_key": "discord:dm:1"}
        )
        assert jobs.read_request(directory)["origin"]["session_id"] == "sess-9"


# ---------------------------------------------------------------------------
# Atomic state transitions (area 3)
# ---------------------------------------------------------------------------


class TestStateTransitions:
    def test_initial_state_is_queued(self, home: Path) -> None:
        _job_id, directory = _make_job(home)
        status = jobs.read_status(directory)
        assert status["state"] == "queued"
        assert all(lane["state"] == "pending" for lane in status["lanes"])
        assert status["notified"] is False

    def test_running_then_terminal(self, home: Path) -> None:
        _job_id, directory = _make_job(home)
        jobs.mark_running(directory, {"runner_mode": "fallback", "runner_pid": 4242})
        status = jobs.read_status(directory)
        assert status["state"] == "running"
        assert status["runner_pid"] == 4242

    def test_terminal_state_is_one_way(self, home: Path) -> None:
        _job_id, directory = _make_job(home)
        assert jobs.finish_job(directory, jobs.STATE_COMPLETED) is not None
        # Every later transition — including another finish — is refused.
        assert jobs.finish_job(directory, jobs.STATE_FAILED, error="late") is None
        assert jobs.update_lane(directory, 0, state=jobs.LANE_FAILED) is None
        assert jobs.set_phase(directory, "running lanes") is None
        assert jobs.read_status(directory)["state"] == "completed"
        assert jobs.read_status(directory)["error"] is None  # late error discarded

    def test_cancel_wins_race_against_complete(self, home: Path) -> None:
        # The exact cancel-vs-complete race: whichever finish lands first wins
        # and the loser's write is refused, not merged.
        _job_id, directory = _make_job(home)
        jobs.mark_running(directory, {})
        assert jobs.finish_job(directory, jobs.STATE_CANCELLED) is not None
        assert jobs.finish_job(directory, jobs.STATE_COMPLETED) is None
        assert jobs.read_status(directory)["state"] == "cancelled"

    def test_non_terminal_state_rejected_by_finish(self, home: Path) -> None:
        _job_id, directory = _make_job(home)
        with pytest.raises(ValueError):
            jobs.finish_job(directory, "exploded")

    def test_lane_updates_track_state(self, home: Path) -> None:
        _job_id, directory = _make_job(home)
        jobs.update_lane(directory, 1, state=jobs.LANE_SUCCEEDED, exit_code=0)
        lanes = jobs.read_status(directory)["lanes"]
        assert lanes[1]["state"] == "succeeded" and lanes[1]["exit_code"] == 0
        assert lanes[0]["state"] == "pending"

    def test_mark_notified_exactly_once(self, home: Path) -> None:
        _job_id, directory = _make_job(home)
        jobs.finish_job(directory, jobs.STATE_COMPLETED)
        assert jobs.mark_notified(directory) is True
        assert jobs.mark_notified(directory) is False  # second delivery refused
        assert jobs.read_status(directory)["notified"] is True

    def test_mark_notified_refused_while_active(self, home: Path) -> None:
        _job_id, directory = _make_job(home)
        assert jobs.mark_notified(directory) is False

    def test_status_json_is_always_valid_json(self, home: Path) -> None:
        # A torn write would fail json.load; every transition keeps it whole.
        _job_id, directory = _make_job(home)
        jobs.mark_running(directory, {})
        for index in range(2):
            jobs.update_lane(directory, index, state=jobs.LANE_SUCCEEDED, exit_code=0)
        jobs.finish_job(directory, jobs.STATE_COMPLETED)
        parsed = json.loads((directory / "status.json").read_text(encoding="utf-8"))
        assert parsed["state"] == "completed"


# ---------------------------------------------------------------------------
# Evidence reads + list bounding
# ---------------------------------------------------------------------------


class TestReads:
    def test_evidence_urls_deduped_in_first_seen_order(self, home: Path) -> None:
        _job_id, directory = _make_job(home)
        ledger = jobs.evidence_path(directory)
        records = [
            {"url": "https://a.example/x", "normalized_url": "https://a.example/x"},
            {"url": "https://b.example/y", "normalized_url": "https://b.example/y"},
            {"url": "https://a.example/x?again", "normalized_url": "https://a.example/x"},
            {"not": "a url record"},
        ]
        ledger.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        assert jobs.read_evidence_urls(directory) == [
            "https://a.example/x",
            "https://b.example/y",
        ]

    def test_missing_evidence_is_empty(self, home: Path) -> None:
        _job_id, directory = _make_job(home)
        assert jobs.read_evidence_urls(directory) == []

    def test_list_is_bounded_and_newest_first(self, home: Path) -> None:
        first_id, first_dir = _make_job(home)
        time.sleep(0.01)
        second_id, _second = _make_job(home, brief="second job")
        entries = jobs.list_recent_jobs(1, home)
        assert [entry["job_id"] for entry in entries] == [second_id]
        assert entries[0]["state"] == "queued"
        both = jobs.list_recent_jobs(10, home)
        assert [entry["job_id"] for entry in both] == [second_id, first_id]

    def test_list_skips_non_job_directories(self, home: Path) -> None:
        _job_id, _directory = _make_job(home)
        (jobs.research_jobs_root(home) / "not-a-job").mkdir()
        (jobs.research_jobs_root(home) / "rj_nothex").mkdir()
        entries = jobs.list_recent_jobs(10, home)
        assert all(jobs.is_canonical_job_id(entry["job_id"]) for entry in entries)

    def test_lane_report_roundtrip(self, home: Path) -> None:
        _job_id, directory = _make_job(home)
        jobs.write_lane_report(directory, 0, "## lane report")
        assert jobs.read_lane_report(directory, 0) == "## lane report"
        assert jobs.read_lane_report(directory, 9) == ""


# ---------------------------------------------------------------------------
# Stale-job recovery (area 3)
# ---------------------------------------------------------------------------


class TestStaleRecovery:
    def test_dead_runner_job_is_failed_with_reason(self, home: Path) -> None:
        job_id, directory = _make_job(home)
        jobs.mark_running(directory, {"runner_mode": "fallback", "runner_pid": 999999})
        # Age it past the grace window.
        status = jobs.read_status(directory)
        status["updated_at"] = time.time() - 10_000
        (directory / "status.json").write_text(json.dumps(status), encoding="utf-8")

        recovered = jobs.recover_stale_jobs(runner_alive=lambda _s: False, hermes_home=home)
        assert recovered == [job_id]
        after = jobs.read_status(directory)
        assert after["state"] == "failed"
        assert "interrupted" in (after["error"] or "")
        assert after["phase"] == "interrupted"
        assert all(
            lane["state"] == jobs.LANE_CANCELLED for lane in after["lanes"]
        )

    def test_live_runner_and_unknown_liveness_left_alone(self, home: Path) -> None:
        for verdict in (True, None):
            job_id, directory = _make_job(home)
            jobs.mark_running(directory, {"runner_mode": "fallback", "runner_pid": 999999})
            status = jobs.read_status(directory)
            status["updated_at"] = time.time() - 10_000
            (directory / "status.json").write_text(json.dumps(status), encoding="utf-8")
            recovered = jobs.recover_stale_jobs(runner_alive=lambda _s, v=verdict: v, hermes_home=home)
            assert recovered == []
            assert jobs.read_status(directory)["state"] == "running"

    def test_fresh_job_gets_grace(self, home: Path) -> None:
        _job_id, directory = _make_job(home)
        jobs.mark_running(directory, {"runner_mode": "fallback", "runner_pid": 999999})
        recovered = jobs.recover_stale_jobs(
            runner_alive=lambda _s: False, grace_seconds=3600.0, hermes_home=home
        )
        assert recovered == []
        assert jobs.read_status(directory)["state"] == "running"

    def test_terminal_jobs_never_recovered(self, home: Path) -> None:
        _job_id, directory = _make_job(home)
        jobs.finish_job(directory, jobs.STATE_COMPLETED)
        status = jobs.read_status(directory)
        status["updated_at"] = time.time() - 10_000
        (directory / "status.json").write_text(json.dumps(status), encoding="utf-8")
        assert jobs.recover_stale_jobs(runner_alive=lambda _s: False, hermes_home=home) == []

    def test_recovered_job_artifacts_stay_readable(self, home: Path) -> None:
        job_id, directory = _make_job(home)
        jobs.write_lane_report(directory, 0, "partial lane output")
        jobs.mark_running(directory, {"runner_mode": "fallback", "runner_pid": 999999})
        status = jobs.read_status(directory)
        status["updated_at"] = time.time() - 10_000
        (directory / "status.json").write_text(json.dumps(status), encoding="utf-8")
        jobs.recover_stale_jobs(runner_alive=lambda _s: False, hermes_home=home)
        assert jobs.read_lane_report(directory, 0) == "partial lane output"
        assert jobs.read_request(directory)["job_id"] == job_id
