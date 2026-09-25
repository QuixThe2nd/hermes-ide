"""Evidence ledger hook: discovery is not evidence, only fetched pages record.

Covers TASK.md test area 8 (search snippet rejected, successful fetch/open
recorded) plus the canonical-path and no-body/no-secret invariants.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from plugins.deep_research import evidence, jobs

FETCHED = "https://example.org/deep-dive"
SECOND = "https://example.net/appendix"


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture()
def job(home: Path) -> tuple[str, Path]:
    created = jobs.create_job(
        brief="brief",
        research_questions=None,
        timeout_minutes=10,
        max_parallel=1,
        worker_profile="researcher",
        hermes_home=home,
    )
    return created["job_id"], created["dir"]


@pytest.fixture()
def ledger_env(job: tuple[str, Path], monkeypatch) -> Path:
    _job_id, directory = job
    path = jobs.evidence_path(directory)
    monkeypatch.setenv(evidence.EVIDENCE_ENV, str(path))
    monkeypatch.setenv(evidence.LANE_ENV, "3")
    return path


def _lines(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _assert_empty_private_ledger(path: Path) -> None:
    """A no-op leaves the pre-created ledger present, empty, and private.

    ``create_job`` deliberately creates ``evidence.jsonl`` up front so the first
    concurrent writer never decides its mode; no-op cases must therefore assert
    emptiness, not absence.
    """
    assert path.exists()
    assert _lines(path) == []
    assert path.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# What counts as evidence (area 8)
# ---------------------------------------------------------------------------


class TestHookRecordsFetchesOnly:
    def test_successful_web_extract_records_url_and_title(self, ledger_env: Path) -> None:
        result = {
            "success": True,
            "results": [
                {"url": FETCHED, "title": "The deep dive", "content": "x" * 500},
                {"url": SECOND, "title": "Appendix"},
            ],
        }
        evidence.handle_post_tool_call(
            tool_name="web_extract", args={"urls": [FETCHED, SECOND]}, result=result
        )
        records = _lines(ledger_env)
        assert [r["url"] for r in records] == [FETCHED, SECOND]
        assert records[0]["title"] == "The deep dive"
        assert records[0]["tool"] == "web_extract"
        assert records[0]["lane"] == 3
        assert records[0]["status"] == "fetched"
        assert records[0]["fetched_at"]
        # Normalized form is stored too, for provenance matching.
        assert records[0]["normalized_url"] == FETCHED

    def test_blocked_web_extract_records_nothing(self, ledger_env: Path) -> None:
        evidence.handle_post_tool_call(
            tool_name="web_extract",
            args={"urls": [FETCHED]},
            result={"success": False, "error": "blocked by robots policy"},
        )
        _assert_empty_private_ledger(ledger_env)

    def test_per_url_failure_is_not_evidence(self, ledger_env: Path) -> None:
        result = {
            "success": True,
            "results": [
                {"url": FETCHED, "title": "ok"},
                {"url": "https://example.org/broken", "error": "404 Not Found"},
            ],
        }
        evidence.handle_post_tool_call("web_extract", {"urls": ["x"]}, result)
        assert [r["url"] for r in _lines(ledger_env)] == [FETCHED]

    def test_successful_browser_navigation_records_the_url(self, ledger_env: Path) -> None:
        evidence.handle_post_tool_call(
            tool_name="browser_navigate",
            args={"url": FETCHED},
            result={"success": True, "url": FETCHED, "title": "Deep dive"},
        )
        records = _lines(ledger_env)
        assert [r["url"] for r in records] == [FETCHED]
        assert records[0]["tool"] == "browser_navigate"

    def test_browser_navigation_records_the_final_url_after_redirects(
        self, ledger_env: Path
    ) -> None:
        # The lane cites the page it actually read; recording the argument
        # would make citation validation reject the redirected URL.
        evidence.handle_post_tool_call(
            tool_name="browser_navigate",
            args={"url": "http://example.org/a"},
            result={"success": True, "url": "https://example.org/a", "title": "A"},
        )
        assert [r["url"] for r in _lines(ledger_env)] == ["https://example.org/a"]

    def test_browser_navigation_falls_back_to_the_argument_url(self, ledger_env: Path) -> None:
        for result in ({"success": True}, {"success": True, "url": ""}, {"success": True, "url": None}):
            evidence.handle_post_tool_call("browser_navigate", {"url": FETCHED}, result)
        assert [r["url"] for r in _lines(ledger_env)] == [FETCHED] * 3

    def test_search_snippets_are_discovery_not_evidence(self, ledger_env: Path) -> None:
        snippet_result = {
            "success": True,
            "results": [
                {"url": FETCHED, "title": "Snippet title", "snippet": "…answer…"},
            ],
        }
        evidence.handle_post_tool_call("web_search", {"query": "widgets"}, snippet_result)
        _assert_empty_private_ledger(ledger_env)

    def test_unrelated_tools_record_nothing(self, ledger_env: Path) -> None:
        for tool in ("file_read", "shell", "memory_search", "web_search"):
            evidence.handle_post_tool_call(tool, {"url": FETCHED}, {"success": True})
        _assert_empty_private_ledger(ledger_env)

    def test_string_result_payload_is_parsed(self, ledger_env: Path) -> None:
        payload = json.dumps({"success": True, "results": [{"url": FETCHED, "title": "t"}]})
        evidence.handle_post_tool_call("web_extract", {"urls": [FETCHED]}, payload)
        assert [r["url"] for r in _lines(ledger_env)] == [FETCHED]

    def test_unparseable_result_records_nothing(self, ledger_env: Path) -> None:
        evidence.handle_post_tool_call("web_extract", {"urls": [FETCHED]}, "<not json>")
        _assert_empty_private_ledger(ledger_env)

    def test_hook_never_raises(self, ledger_env: Path) -> None:
        class Boom:
            def __getattr__(self, name):
                raise RuntimeError("bad payload")

        evidence.handle_post_tool_call("web_extract", {"urls": [FETCHED]}, Boom())
        evidence.handle_post_tool_call("web_extract", None, {"success": True})
        evidence.handle_post_tool_call("", {}, None)
        _assert_empty_private_ledger(ledger_env)


class TestHookIsNoOpOutsideJobs:
    def test_no_ledger_env_means_no_write(self, job: tuple[str, Path], monkeypatch, tmp_path) -> None:
        _job_id, directory = job
        monkeypatch.delenv(evidence.EVIDENCE_ENV, raising=False)
        evidence.handle_post_tool_call(
            "web_extract",
            {"urls": [FETCHED]},
            {"success": True, "results": [{"url": FETCHED}]},
        )
        _assert_empty_private_ledger(directory / "evidence.jsonl")

    @pytest.mark.parametrize(
        "bad",
        [
            "evidence.jsonl",  # relative
            "/tmp/research_jobs/evidence.jsonl",  # missing job id
            "/tmp/research_jobs/not-a-job/evidence.jsonl",
            "/tmp/elsewhere/rj_0123456789ab/evidence.jsonl",  # wrong root name
            "/tmp/research_jobs/rj_0123456789ab/other.jsonl",
            "/tmp/research_jobs/rj_0123456789ab/../rj_0123456789ab/evidence.jsonl",
            "",
        ],
    )
    def test_non_canonical_ledger_paths_rejected(self, bad: str) -> None:
        assert evidence.canonical_ledger_path(bad) is None

    def test_canonical_ledger_path_accepted(self, job: tuple[str, Path]) -> None:
        job_id, directory = job
        assert evidence.canonical_ledger_path(str(directory / "evidence.jsonl")) == (
            directory / "evidence.jsonl"
        )

    def test_hook_ignores_redirected_ledger(self, job: tuple[str, Path], monkeypatch, tmp_path) -> None:
        _job_id, directory = job
        decoy = tmp_path / "decoy.jsonl"
        monkeypatch.setenv(evidence.EVIDENCE_ENV, str(decoy))
        evidence.handle_post_tool_call(
            "web_extract", {"urls": [FETCHED]}, {"success": True, "results": [{"url": FETCHED}]}
        )
        assert not decoy.exists()


class TestLedgerContents:
    def test_no_page_bodies_or_secrets_in_ledger(self, ledger_env: Path) -> None:
        body = "SUPERCALIFRAGILISTIC page body " + "x" * 5_000
        evidence.handle_post_tool_call(
            "web_extract",
            {"urls": [FETCHED], "api_key": "sk-live-secret"},
            {
                "success": True,
                "results": [{"url": FETCHED, "title": "t", "content": body}],
                "api_key": "sk-live-secret",
            },
        )
        raw = ledger_env.read_text(encoding="utf-8")
        assert "SUPERCALIFRAGILISTIC" not in raw
        assert "sk-live-secret" not in raw
        assert "api_key" not in raw

    def test_titles_are_bounded(self, ledger_env: Path) -> None:
        evidence.handle_post_tool_call(
            "web_extract",
            {"urls": [FETCHED]},
            {"success": True, "results": [{"url": FETCHED, "title": "T" * 5_000}]},
        )
        assert len(_lines(ledger_env)[0]["title"]) <= 200

    def test_ledger_file_is_private(self, ledger_env: Path) -> None:
        evidence.handle_post_tool_call(
            "web_extract",
            {"urls": [FETCHED]},
            {"success": True, "results": [{"url": FETCHED}]},
        )
        assert ledger_env.stat().st_mode & 0o777 == 0o600

    def test_concurrent_writers_do_not_corrupt_lines(self, ledger_env: Path) -> None:
        import threading

        def write(index: int) -> None:
            for _ in range(20):
                evidence.record_source(
                    ledger_env,
                    url=f"https://example.org/p{index}",
                    tool="web_extract",
                    lane=index,
                )

        threads = [threading.Thread(target=write, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        lines = _lines(ledger_env)
        assert len(lines) == 4 * 20
        assert len({r["url"] for r in lines}) == 4

    def test_urls_are_normalized_on_record(self, ledger_env: Path) -> None:
        evidence.handle_post_tool_call(
            "web_extract",
            {"urls": ["https://Example.org:443/deep-dive#section"]},
            {"success": True, "results": [{"url": "https://Example.org:443/deep-dive#section"}]},
        )
        (record,) = _lines(ledger_env)
        assert record["normalized_url"] == FETCHED

    def test_lane_defaults_to_zero_without_env(self, ledger_env: Path, monkeypatch) -> None:
        monkeypatch.delenv(evidence.LANE_ENV, raising=False)
        assert evidence.current_lane() == 0
