"""Cron jobs are never model-specific: per-job pins are ignored (fleet default always wins).

Background: a job carried a stale pin (`model: gpt-5.6-sol, provider: openai-codex`) in the jobs
store and failed with HTTP 400 after that provider stopped existing on the host. Per-job model
pinning (pins + creation snapshots) was therefore removed.

Current contract:
  - run_job() resolves the model as cron.model (fleet default) > global default; any per-job
    ``model``/``provider``/``model_snapshot``/``provider_snapshot`` fields are tolerated on load
    and have ZERO effect.
  - create_job() never computes snapshots; the fields persist as None for record-shape stability.
  - resnapshot_job()/resnapshot_all_unpinned() (still called by the cronjob tool's resnap action)
    clear leftover pin fields from older stores.

These tests exercise the full run_job path (real imports, mocked AIAgent +
resolve_runtime_provider against a temp HERMES_HOME).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root is importable.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron.scheduler import run_job


def _base_job(**overrides):
    job = {
        "id": "pin-test",
        "name": "pin test",
        "prompt": "hello",
        "model": None,
        "provider": None,
        "provider_snapshot": None,
        "model_snapshot": None,
        "base_url": None,
    }
    job.update(overrides)
    return job


def _run(job, tmp_path, *, current_provider="openrouter", current_model=None, cron_model=None,
         cron_model_provider=None):
    """Drive run_job against a temp config.yaml whose ``model.default`` / ``model.provider`` are
    the CURRENT global defaults. Returns ``(success, error, agent_kwargs, resolve_kwargs)`` where
    the last two are the kwargs AIAgent / resolve_runtime_provider were called with (None when
    never called)."""
    config_yaml = ""
    if current_model or current_provider:
        config_yaml += "model:\n"
        if current_model:
            config_yaml += f"  default: {current_model}\n"
        if current_provider:
            config_yaml += f"  provider: {current_provider}\n"
    cron_lines = []
    if cron_model is not None:
        cron_lines.append(f"  model: {cron_model}")
    if cron_model_provider is not None:
        cron_lines.append(f"  model_provider: {cron_model_provider}")
    if cron_lines:
        config_yaml += "cron:\n" + "\n".join(cron_lines) + "\n"
    (tmp_path / "config.yaml").write_text(config_yaml)

    resolve_kwargs = {}

    def _resolve(**kwargs):
        resolve_kwargs.update(kwargs)
        return {
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
            "provider": kwargs.get("requested") or current_provider,
            "api_mode": "chat_completions",
        }

    fake_db = MagicMock()
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._get_hermes_home", return_value=tmp_path), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state_registry.acquire", return_value=fake_db), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=_resolve), \
         patch("run_agent.AIAgent") as mock_agent_cls:
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "ok"}
        mock_agent_cls.return_value = mock_agent
        success, _output, _final, error = run_job(job)
        agent_kwargs = mock_agent_cls.call_args.kwargs if mock_agent_cls.called else None
    return success, error, agent_kwargs, (resolve_kwargs or None)


class TestPerJobPinsAreIgnored:
    def test_stale_pin_and_snapshot_run_on_global_default(self, tmp_path):
        """A job naming a provider that no longer exists must still run: the stale model/provider
        pins and creation snapshots are ignored and the fleet (global) default is used."""
        job = _base_job(
            model="gpt-5.6-sol", provider="openai-codex",
            model_snapshot="old-model", provider_snapshot="old-provider")
        success, error, agent_kwargs, resolve_kwargs = _run(
            job, tmp_path, current_provider="new-provider", current_model="new-model")

        assert success is True, error
        assert agent_kwargs["model"] == "new-model"
        assert resolve_kwargs["requested"] is None
        assert resolve_kwargs["target_model"] == "new-model"

    def test_cron_fleet_default_beats_job_pin(self, tmp_path):
        """cron.model / cron.model_provider route the whole fleet, pins included."""
        job = _base_job(model="stale-model", provider="stale-provider")
        success, error, agent_kwargs, resolve_kwargs = _run(
            job, tmp_path, current_provider="new-provider", current_model="new-model",
            cron_model="fleet-model", cron_model_provider="fleet-provider")

        assert success is True, error
        assert agent_kwargs["model"] == "fleet-model"
        assert resolve_kwargs["requested"] == "fleet-provider"
        assert resolve_kwargs["target_model"] == "fleet-model"

    def test_job_without_pins_follows_global_default(self, tmp_path):
        job = _base_job()
        success, error, agent_kwargs, resolve_kwargs = _run(
            job, tmp_path, current_provider="new-provider", current_model="new-model")

        assert success is True, error
        assert agent_kwargs["model"] == "new-model"
        assert resolve_kwargs["requested"] is None

    def test_missing_model_guides_to_fleet_default(self, tmp_path, monkeypatch):
        """A missing-model failure must point at the fleet default, not per-job pinning."""
        monkeypatch.delenv("HERMES_MODEL", raising=False)
        success, error, agent_kwargs, _ = _run(
            _base_job(), tmp_path, current_provider="openrouter", current_model=None)

        assert success is False
        assert agent_kwargs is None
        assert "cron.model" in error
        assert "hermes cron edit" not in error


class TestCreateJobNoSnapshot:
    """create_job stores the (inert) model/provider fields verbatim and never resolves a snapshot."""

    @staticmethod
    def _isolate_storage(monkeypatch):
        """Patch cron.jobs storage so create_job never touches the real store."""
        import contextlib
        import cron.jobs as jobs

        @contextlib.contextmanager
        def _noop_lock():
            yield

        monkeypatch.setattr(jobs, "_jobs_lock", _noop_lock, raising=True)
        monkeypatch.setattr(jobs, "load_jobs", lambda: [], raising=True)
        monkeypatch.setattr(jobs, "save_jobs", lambda j: None, raising=True)
        return jobs

    def test_create_stores_none_snapshots_without_resolving(self, monkeypatch):
        jobs = self._isolate_storage(monkeypatch)

        resolver = MagicMock(return_value={"provider": "openrouter"})
        with patch("hermes_cli.runtime_provider.resolve_runtime_provider", resolver):
            job = jobs.create_job(prompt="do a thing", schedule="every 1 hour")

        assert job["provider_snapshot"] is None
        assert job["model_snapshot"] is None
        resolver.assert_not_called()

    def test_create_tolerates_passed_pins_inertly(self, monkeypatch):
        """The CLI/tool still accept --model/--provider; they are stored but have no effect."""
        jobs = self._isolate_storage(monkeypatch)

        job = jobs.create_job(
            prompt="do a thing", schedule="every 1 hour", model="m", provider="p")

        assert job["model"] == "m"
        assert job["provider"] == "p"
        assert job["model_snapshot"] is None
        assert job["provider_snapshot"] is None


class TestResnapshot:
    """resnapshot_job / resnapshot_all_unpinned clear leftover pin fields (still called by the
    cronjob tool's resnap action)."""

    @staticmethod
    def _install_store(monkeypatch, initial_jobs):
        """Install an in-memory cron job store backed by a real list so
        resnapshot functions can load/save against it."""
        import contextlib
        import cron.jobs as jobs

        store = [dict(j) for j in initial_jobs]  # deep-ish copy per job

        @contextlib.contextmanager
        def _lock():
            yield

        monkeypatch.setattr(jobs, "_jobs_lock", _lock, raising=True)
        monkeypatch.setattr(jobs, "load_jobs", lambda: [dict(j) for j in store], raising=True)

        def _save(job_list):
            store[:] = [dict(j) for j in job_list]

        monkeypatch.setattr(jobs, "save_jobs", _save, raising=True)
        return jobs, store

    def _make_job(self, job_id, **overrides):
        job = {
            "id": job_id,
            "name": f"job {job_id}",
            "prompt": "do a thing",
            "model": None,
            "provider": None,
            "model_snapshot": "old-model",
            "provider_snapshot": "old-provider",
            "base_url": None,
            "no_agent": False,
        }
        job.update(overrides)
        return job

    def test_resnapshot_clears_pin_fields(self, monkeypatch):
        jobs_mod, store = self._install_store(
            monkeypatch,
            [self._make_job("j1", model="stale-model", provider="stale-provider")],
        )

        updated = jobs_mod.resnapshot_job("j1")

        assert updated is not None
        assert updated["model"] is None
        assert updated["provider"] is None
        assert updated["model_snapshot"] is None
        assert updated["provider_snapshot"] is None
        # Persisted too.
        assert store[0]["model"] is None
        assert store[0]["provider_snapshot"] is None

    def test_resnapshot_missing_job_returns_none(self, monkeypatch):
        jobs_mod, _store = self._install_store(monkeypatch, [])
        assert jobs_mod.resnapshot_job("nope") is None

    def test_resnapshot_all_clears_every_job(self, monkeypatch):
        jobs_mod, store = self._install_store(
            monkeypatch,
            [
                self._make_job("j1", model="m1", provider="p1"),
                self._make_job("j2", no_agent=True, model_snapshot="old"),
                self._make_job("j3", model=None, provider=None, model_snapshot=None,
                               provider_snapshot=None),
            ],
        )

        updated = jobs_mod.resnapshot_all_unpinned()

        assert [j["id"] for j in updated] == ["j1", "j2"]
        for stored in store:
            assert stored["model"] is None
            assert stored["provider"] is None
            assert stored["model_snapshot"] is None
            assert stored["provider_snapshot"] is None
