"""Default ``inbox-sparks`` cron job seeding for the hermes_starts plugin.

The plugin owns one scheduled wake (``start_conversation`` reviews) that every
install gets by default: on gateway start, when the plugin is enabled and its
prerequisite (a Discord bot token) holds, an ``inbox-sparks`` job is created
once per profile home — never modified, never duplicated, never recreated
after the user deletes it, and never created where config opts out.

The job spec and the create/skip decision live in the Go module
``internal/sparkscron``; this plugin only gathers per-home facts and executes
the verdict, so these tests drive the real Go binary.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PLUGIN_DIR = _REPO_ROOT / "plugins" / "hermes_starts"
_GO_MODULE_DIR = _REPO_ROOT / "internal" / "sparkscron"

_LONG_CUSTOM_PROMPT = "MAINTAINER CUSTOM PROMPT\n" + ("do not touch me\n" * 800)


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    yield hermes_home


@pytest.fixture()
def sparks_cron():
    module_name = "hermes_starts_sparks_cron_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name, _PLUGIN_DIR / "sparks_cron.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def _go_binary_path(tmp_path_factory):
    """Build the real ``cmd/sparkscron`` binary once for this file's process."""
    if shutil.which("go") is None:
        pytest.skip("no Go toolchain available for the sparkscron bridge")
    dest = tmp_path_factory.mktemp("sparkscron-bin") / "sparkscron"
    build = subprocess.run(
        ["go", "build", "-o", str(dest), "./cmd/sparkscron"],
        cwd=_GO_MODULE_DIR, capture_output=True, text=True, timeout=300,
    )
    assert build.returncode == 0, build.stderr
    return dest


@pytest.fixture()
def real_binary(sparks_cron, monkeypatch, _go_binary_path):
    monkeypatch.setattr(sparks_cron, "_go_binary", lambda: str(_go_binary_path))


def _write_token(home: Path) -> None:
    (home / ".env").write_text("DISCORD_BOT_TOKEN=test-bot-token\n", encoding="utf-8")


def _write_config(home: Path, config: Dict[str, Any]) -> None:
    (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")


def _jobs_in_store(home: Path) -> List[Dict[str, Any]]:
    jobs_file = home / "cron" / "jobs.json"
    if not jobs_file.is_file():
        return []
    return json.loads(jobs_file.read_text(encoding="utf-8")).get("jobs", [])


def _sparks_jobs(home: Path) -> List[Dict[str, Any]]:
    return [j for j in _jobs_in_store(home) if j.get("name") == "inbox-sparks"]


def _ensure(mod, prereq=None):
    return mod.ensure_sparks_cron_jobs(prereq or (lambda: True))


def test_fresh_home_seeds_job_once(_isolate_env, sparks_cron, real_binary):
    home = _isolate_env
    _write_token(home)
    _ensure(sparks_cron)
    _ensure(sparks_cron)  # idempotent: no duplicate on a second pass

    jobs = _sparks_jobs(home)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["schedule"]["kind"] == "interval"
    assert job["schedule"]["minutes"] == 120
    assert job["deliver"] == "local"
    assert "hermes_starts" in job["enabled_toolsets"]
    for toolset in ("file", "terminal", "session_search", "skills", "web"):
        assert toolset in job["enabled_toolsets"]
    assert "tabctl" not in job["enabled_toolsets"]
    assert job["skills"] == []
    assert job["skill"] is None
    assert job["model"] is None and job["provider"] is None
    assert "[SILENT]" in job["prompt"]
    assert "start_conversation" in job["prompt"]
    # Seeding marker recorded so a later deletion is respected.
    assert (home / "hermes_starts" / "sparks_cron_seeded").is_file()


def test_plugin_disabled_launch_home_not_seeded(_isolate_env, sparks_cron, real_binary):
    home = _isolate_env
    _write_token(home)
    _write_config(home, {"plugins": {"disabled": ["hermes_starts"]}})

    _ensure(sparks_cron)
    _ensure(sparks_cron)

    assert _sparks_jobs(home) == []
    assert not (home / "hermes_starts" / "sparks_cron_seeded").exists()


@pytest.mark.parametrize("deny", [
    ["hermes_starts"],
    ["inbox_sparks"],
    ["Inbox-Sparks"],  # case + hyphen spelling must count as the same plugin
    ["unrelated_plugin", "hermes-starts"],
])
def test_multiplex_plugin_disabled_home_not_seeded(
    tmp_path, monkeypatch, sparks_cron, real_binary, deny
):
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    beta = home / "profiles" / "beta"
    beta.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_token(home)
    _write_token(beta)
    _write_config(home, {"gateway": {"multiplex_profiles": True}})
    _write_config(beta, {"plugins": {"disabled": deny}})

    _ensure(sparks_cron)

    assert len(_sparks_jobs(home)) == 1
    assert (home / "hermes_starts" / "sparks_cron_seeded").is_file()
    assert _sparks_jobs(beta) == []
    assert not (beta / "hermes_starts" / "sparks_cron_seeded").exists()


def test_existing_job_untouched_byte_identical(_isolate_env, sparks_cron, real_binary):
    from cron.jobs import create_job

    home = _isolate_env
    _write_token(home)
    create_job(
        prompt=_LONG_CUSTOM_PROMPT, schedule="every 5m", name="inbox-sparks",
        deliver="discord:1:2", enabled_toolsets=["hermes_starts", "tabctl"],
    )
    before = _sparks_jobs(home)[0]
    _ensure(sparks_cron)
    after = _sparks_jobs(home)[0]
    assert len(_sparks_jobs(home)) == 1
    assert after["id"] == before["id"]
    assert after["prompt"] == before["prompt"] == _LONG_CUSTOM_PROMPT.strip()
    assert after["schedule"] == before["schedule"]
    assert after["deliver"] == before["deliver"]
    assert after["enabled_toolsets"] == before["enabled_toolsets"]
    # We did not seed, so no marker: a later delete may reseed the default.
    assert not (home / "hermes_starts" / "sparks_cron_seeded").exists()


def test_paused_job_stays_as_is(_isolate_env, sparks_cron, real_binary):
    from cron.jobs import create_job

    home = _isolate_env
    _write_token(home)
    create_job(prompt="hold", schedule="every 3h", name="inbox-sparks", paused=True)
    _ensure(sparks_cron)
    job = _sparks_jobs(home)[0]
    assert len(_sparks_jobs(home)) == 1
    assert job["enabled"] is False
    assert job["state"] == "paused"


def test_deleted_after_seeding_not_recreated(_isolate_env, sparks_cron, real_binary):
    from cron.jobs import remove_job

    home = _isolate_env
    _write_token(home)
    _ensure(sparks_cron)
    job_id = _sparks_jobs(home)[0]["id"]
    assert remove_job(job_id)
    _ensure(sparks_cron)
    assert _sparks_jobs(home) == []


def test_sparks_cron_false_never_seeds(_isolate_env, sparks_cron, real_binary):
    home = _isolate_env
    _write_token(home)
    _write_config(home, {
        "plugins": {"entries": {"hermes_starts": {
            "enabled": True, "settings": {"sparks_cron": False},
        }}},
    })
    _ensure(sparks_cron)
    _ensure(sparks_cron)
    assert _sparks_jobs(home) == []
    assert not (home / "hermes_starts" / "sparks_cron_seeded").exists()


def test_missing_prerequisite_never_seeds(_isolate_env, sparks_cron, real_binary):
    home = _isolate_env  # no DISCORD_BOT_TOKEN in .env
    _ensure(sparks_cron, prereq=lambda: False)
    assert _sparks_jobs(home) == []
    assert not (home / "hermes_starts" / "sparks_cron_seeded").exists()


def test_ambiguous_duplicate_jobs_left_alone(_isolate_env, sparks_cron, real_binary):
    from cron.jobs import create_job

    home = _isolate_env
    _write_token(home)
    create_job(prompt="one", schedule="every 2h", name="inbox-sparks")
    create_job(prompt="two", schedule="every 3h", name="inbox-sparks")
    _ensure(sparks_cron)
    jobs = _sparks_jobs(home)
    assert sorted(j["prompt"] for j in jobs) == ["one", "two"]


def test_no_go_binary_degrades_to_noop(_isolate_env, sparks_cron, monkeypatch):
    home = _isolate_env
    _write_token(home)
    monkeypatch.setattr(sparks_cron, "_go_binary", lambda: None)
    _ensure(sparks_cron)  # must not raise
    assert _sparks_jobs(home) == []
    assert not (home / "hermes_starts" / "sparks_cron_seeded").exists()


def test_multiplex_seeds_each_profile_store(tmp_path, monkeypatch, sparks_cron, real_binary):
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    beta = home / "profiles" / "beta"
    beta.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_token(home)
    _write_token(beta)
    _write_config(home, {"gateway": {"multiplex_profiles": True}})
    # beta opts out in its own config: per-profile flag, not process-global.
    _write_config(beta, {
        "plugins": {"entries": {"hermes_starts": {
            "settings": {"sparks_cron": False},
        }}},
    })

    _ensure(sparks_cron)

    default_jobs = _sparks_jobs(home)
    assert len(default_jobs) == 1
    assert _sparks_jobs(beta) == []
    assert (home / "hermes_starts" / "sparks_cron_seeded").is_file()
    assert not (beta / "hermes_starts" / "sparks_cron_seeded").exists()


def test_multiplex_second_profile_gets_own_job(tmp_path, monkeypatch, sparks_cron, real_binary):
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    beta = home / "profiles" / "beta"
    beta.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_token(home)
    _write_token(beta)
    _write_config(home, {"gateway": {"multiplex_profiles": True}})

    _ensure(sparks_cron)

    first = _sparks_jobs(home)
    second = _sparks_jobs(beta)
    assert len(first) == 1 and len(second) == 1
    assert first[0]["id"] != second[0]["id"]
    assert (beta / "hermes_starts" / "sparks_cron_seeded").is_file()
