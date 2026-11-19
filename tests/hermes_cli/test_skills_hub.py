from io import StringIO
from unittest.mock import patch

import pytest
from rich.console import Console

from cli import ChatConsole
from hermes_cli.skills_hub import do_check, do_install, do_list, do_update, handle_skills_slash


class _DummyLockFile:
    def __init__(self, installed):
        self._installed = installed

    def list_installed(self):
        return self._installed


@pytest.fixture()
def hub_env(monkeypatch, tmp_path):
    """Set up isolated hub directory paths and return (monkeypatch, tmp_path)."""
    import tools.skills_hub as hub

    hub_dir = tmp_path / "skills" / ".hub"
    monkeypatch.setattr(hub, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(hub, "HUB_DIR", hub_dir)
    monkeypatch.setattr(hub, "LOCK_FILE", hub_dir / "lock.json")
    monkeypatch.setattr(hub, "QUARANTINE_DIR", hub_dir / "quarantine")
    monkeypatch.setattr(hub, "AUDIT_LOG", hub_dir / "audit.log")
    monkeypatch.setattr(hub, "TAPS_FILE", hub_dir / "taps.json")
    monkeypatch.setattr(hub, "INDEX_CACHE_DIR", hub_dir / "index-cache")

    return hub_dir


# ---------------------------------------------------------------------------
# Fixtures for common skill setups
# ---------------------------------------------------------------------------

_HUB_ENTRY = {"name": "hub-skill", "source": "github", "trust_level": "community"}

_ALL_THREE_SKILLS = [
    {"name": "hub-skill", "category": "x", "description": "hub"},
    {"name": "builtin-skill", "category": "x", "description": "builtin"},
    {"name": "local-skill", "category": "x", "description": "local"},
]

_BUILTIN_MANIFEST = {"builtin-skill": "abc123"}


@pytest.fixture()
def three_source_env(monkeypatch, hub_env):
    """Populate hub/builtin/local skills for source-classification tests."""
    import tools.skills_hub as hub
    import tools.skills_sync as skills_sync
    import tools.skills_tool as skills_tool

    monkeypatch.setattr(hub, "HubLockFile", lambda: _DummyLockFile([_HUB_ENTRY]))
    monkeypatch.setattr(skills_tool, "_find_all_skills", lambda **_kwargs: list(_ALL_THREE_SKILLS))
    monkeypatch.setattr(skills_sync, "_read_manifest", lambda: dict(_BUILTIN_MANIFEST))

    return hub_env


def _capture(source_filter: str = "all") -> str:
    """Run do_list into a string buffer and return the output."""
    sink = StringIO()
    console = Console(file=sink, force_terminal=False, color_system=None)
    do_list(source_filter=source_filter, console=console)
    return sink.getvalue()


def _capture_check(monkeypatch, results, name=None) -> str:
    import tools.skills_hub_install as hub_install

    sink = StringIO()
    console = Console(file=sink, force_terminal=False, color_system=None)
    monkeypatch.setattr(hub_install, "check_for_skill_updates", lambda **_kwargs: results)
    do_check(name=name, console=console)
    return sink.getvalue()


def _capture_update(monkeypatch, results) -> tuple[str, list[tuple[str, str, bool]]]:
    import tools.skills_hub as hub
    import tools.skills_hub_install as hub_install
    import hermes_cli.skills_hub as cli_hub

    sink = StringIO()
    console = Console(file=sink, force_terminal=False, color_system=None)
    installs = []

    monkeypatch.setattr(hub_install, "check_for_skill_updates", lambda **_kwargs: results)
    monkeypatch.setattr(hub, "HubLockFile", lambda: type("L", (), {
        "get_installed": lambda self, name: {"install_path": "category/" + name}
    })())
    monkeypatch.setattr(cli_hub, "do_install", lambda identifier, category="", force=False, console=None, source_id=None: installs.append((identifier, category, force)))

    do_update(console=console)
    return sink.getvalue(), installs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------




def test_do_list_platform_env_is_ignored(three_source_env, monkeypatch):
    """`hermes skills list` reads the active profile's config via
    HERMES_HOME (swapped by -p), so it must NOT pass a platform arg to
    ``get_disabled_skill_names`` — otherwise per-platform overrides
    would silently leak in from HERMES_PLATFORM env."""
    from agent import skill_utils

    seen = {}

    def _fake(platform=None):
        seen["platform"] = platform
        return set()

    monkeypatch.setattr(skill_utils, "get_disabled_skill_names", _fake)
    _capture()

    assert seen["platform"] is None


# ---------------------------------------------------------------------------
# Cross-registry hijack regression tests
#
# An update must never change a skill's source registry. Skill names are not
# namespaced across registries, so an unconstrained name resolve can install a
# different author's same-named skill over the user's files.
# ---------------------------------------------------------------------------




def test_check_for_skill_updates_does_not_fall_back_across_registries(tmp_path, monkeypatch):
    """An entry whose source has no adapter reports `unavailable`.

    Previously `candidate_sources ... or sources` fell back to every source, so
    a same-named skill in another registry could satisfy the fetch and be
    reported as this entry's update -- the step that preceded the overwrite.
    The foreign source here returns a *valid* bundle with a different hash, so
    the old code reports `update_available` (sourced from the wrong registry)
    while the fixed code reports `unavailable`.
    """
    from tools.skills_hub_install import check_for_skill_updates

    class _ForeignBundle:
        name = "reddit"
        files = {"SKILL.md": "# a different author's reddit skill"}
        source = "skills.sh"
        identifier = "skills-sh/someone-else/reddit"
        trust_level = "community"
        metadata: dict = {}

    class _ForeignSource:
        """skills-sh adapter; must NOT be consulted for a clawhub-locked entry."""

        def source_id(self):
            return "skills-sh"

        def fetch(self, identifier):
            return _ForeignBundle()

        def inspect(self, identifier):
            return _ForeignBundle()

    from tools import skills_hub as hub
    monkeypatch.setattr(hub, "SKILLS_DIR", tmp_path)
    (tmp_path / "reddit").mkdir()
    lock = _DummyLockFile([
        {"name": "reddit", "identifier": "reddit", "source": "clawhub",
         "install_path": "reddit", "content_hash": "hash-of-the-clawhub-copy"},
    ])

    results = check_for_skill_updates(
        lock=lock,  # type: ignore[arg-type]  # duck-typed double, matches _DummyLockFile usage above
        sources=[_ForeignSource()],  # type: ignore[list-item]
    )

    assert len(results) == 1
    assert results[0]["source"] == "clawhub", "provenance must be preserved"
    assert results[0]["status"] == "unavailable", (
        "a clawhub-locked skill must not be matched against a skills-sh bundle; "
        "reporting update_available here is the cross-registry hijack"
    )
    assert "bundle" not in results[0], "must not carry a foreign registry's bundle"


def test_resolve_does_not_pair_catalog_meta_with_foreign_same_name_bundle():
    """Inspect metadata from one registry must not ship with another registry's files.

    skills.sh can inspect ``owner/repo/skills/skillopt`` while ClawHub used to
    fetch by last path segment ``skillopt`` and return a different author's
    SKILL.md. The header then showed the requested identifier and the preview
    showed the wrong skill.
    """
    from hermes_cli.skills_hub import _resolve_source_meta_and_bundle
    from tools.skills_hub_models import SkillBundle, SkillMeta

    class CatalogSource:
        def inspect(self, identifier):
            return SkillMeta(
                name="skillopt",
                description="project-based pipelines",
                source="skills.sh",
                identifier="skills-sh/latipun7/agent-skill-collections/skills/skillopt",
                trust_level="community",
            )

        def fetch(self, identifier):
            return None

    class ForeignSlugSource:
        def inspect(self, identifier):
            return SkillMeta(
                name="skillopt",
                description="Train, evaluate, and improve Agent skill files",
                source="clawhub",
                identifier="skillopt",
                trust_level="community",
            )

        def fetch(self, identifier):
            return SkillBundle(
                name="skillopt",
                files={"SKILL.md": "# Train, evaluate, and improve Agent skill files\n"},
                source="clawhub",
                identifier="skillopt",
                trust_level="community",
            )

    meta, bundle, matched = _resolve_source_meta_and_bundle(
        "latipun7/agent-skill-collections/skills/skillopt",
        [CatalogSource(), ForeignSlugSource()],
    )

    assert bundle is not None
    assert bundle.source == "clawhub"
    assert meta is not None
    assert meta.source == "clawhub"
    assert meta.identifier == "skillopt"
    assert "project-based" not in (meta.description or "")
    assert matched is not None
    assert matched.__class__ is ForeignSlugSource


def test_resolve_keeps_catalog_meta_when_later_sources_do_not_fetch():
    from hermes_cli.skills_hub import _resolve_source_meta_and_bundle
    from tools.skills_hub_models import SkillMeta

    class CatalogSource:
        def inspect(self, identifier):
            return SkillMeta(
                name="skillopt",
                description="project-based pipelines",
                source="skills.sh",
                identifier="skills-sh/latipun7/agent-skill-collections/skills/skillopt",
                trust_level="community",
            )

        def fetch(self, identifier):
            return None

    class QuietSource:
        def inspect(self, identifier):
            return None

        def fetch(self, identifier):
            return None

    meta, bundle, matched = _resolve_source_meta_and_bundle(
        "latipun7/agent-skill-collections/skills/skillopt",
        [CatalogSource(), QuietSource()],
    )

    assert bundle is None
    assert meta is not None
    assert meta.source == "skills.sh"
    assert meta.identifier.endswith("latipun7/agent-skill-collections/skills/skillopt")
    assert matched is not None
    assert matched.__class__ is CatalogSource




# ---------------------------------------------------------------------------
# UrlSource-specific install paths: --name override, interactive prompts,
# non-interactive error, existing-category scan.
# ---------------------------------------------------------------------------


def _make_url_bundle_fetcher(name="", awaiting_name=True, url="https://example.com/SKILL.md"):
    """Return a fake source that simulates ``UrlSource.fetch`` for a
    URL-sourced skill whose name hasn't been auto-resolved."""

    class _UrlSource:
        def inspect(self, identifier):
            return type("Meta", (), {
                "extra": {"url": url, "awaiting_name": awaiting_name},
                "identifier": url,
                "name": name,
                "path": name,
            })()

        def fetch(self, identifier):
            return type("Bundle", (), {
                "name": name,
                "files": {"SKILL.md": "---\ndescription: ok\n---\n# body\n"},
                "source": "url",
                "identifier": url,
                "trust_level": "community",
                "metadata": {"url": url, "awaiting_name": awaiting_name},
            })()

    return _UrlSource


def _install_mocks(monkeypatch, tmp_path, source_factory, category_hint=""):
    """Wire the minimum set of monkeypatches for a do_install dry run."""
    import tools.skills_hub as hub
    import tools.skills_hub_install as hub_install
    import tools.skills_hub_search as hub_search
    import tools.skills_guard as guard

    q_path = tmp_path / "skills" / ".hub" / "quarantine" / "pending"
    q_path.mkdir(parents=True)

    install_calls: list = []

    def _install_from_quarantine(q, name, category, bundle, result):
        install_calls.append({"name": name, "category": category})
        install_dir = tmp_path / "skills" / (f"{category}/" if category else "") / name
        install_dir.mkdir(parents=True, exist_ok=True)
        return install_dir

    monkeypatch.setattr(hub, "ensure_hub_dirs", lambda: None)
    monkeypatch.setattr(hub_search, "create_source_router", lambda auth: [source_factory()])
    monkeypatch.setattr(hub_install, "quarantine_bundle", lambda bundle: q_path)
    monkeypatch.setattr(hub_install, "install_from_quarantine", _install_from_quarantine)
    monkeypatch.setattr(
        hub, "HubLockFile",
        lambda: type("Lock", (), {"get_installed": lambda self, n: None})(),
    )
    monkeypatch.setattr(
        guard, "scan_skill",
        lambda skill_path, source="community": guard.ScanResult(
            skill_name="pending", source=source, trust_level="community", verdict="safe",
        ),
    )
    monkeypatch.setattr(guard, "format_scan_report", lambda result: "scan ok")
    monkeypatch.setattr(guard, "should_allow_install", lambda result, force=False: (True, "ok"))
    return install_calls






# ── _existing_categories ────────────────────────────────────────────────────






# ---------------------------------------------------------------------------
# browse_skills — dedup by identifier, not name
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Regression: full identifier must be recoverable from `hermes skills search`
# even when the slug is too long to fit the terminal width (issue #33674).
# ---------------------------------------------------------------------------

# A real browse-sh-style slug whose trailing -XXXXXX hash matters for install
_LONG_SLUG = "browse-sh/weather.gov/get-forecast-1uezib"

_LONG_RESULT = type("R", (), {
    "name": "get-forecast",
    "description": "Fetch the forecast",
    "source": "browse-sh",
    "trust_level": "community",
    "identifier": _LONG_SLUG,
})()


def test_do_search_json_flag_emits_full_identifiers(capsys):
    """`--json` must print a parseable array with full identifiers and skip the table."""
    from hermes_cli.skills_hub import do_search

    sink = StringIO()
    console = Console(file=sink, force_terminal=False, color_system=None, width=40)

    with patch("tools.skills_hub_search.unified_search", return_value=[_LONG_RESULT]), \
         patch("tools.skills_hub_search.create_source_router", return_value={}), \
         patch("tools.skills_hub_github.GitHubAuth"):
        do_search("weather", console=console, as_json=True)

    # JSON goes to stdout via print(), not the Rich console sink.
    captured = capsys.readouterr().out
    import json as _json
    payload = _json.loads(captured)
    assert isinstance(payload, list) and len(payload) == 1
    assert payload[0]["identifier"] == _LONG_SLUG
    assert payload[0]["name"] == "get-forecast"
    assert payload[0]["source"] == "browse-sh"
    # Table render must be suppressed — sink should be empty (no "Searching for:" header).
    assert "Searching for:" not in sink.getvalue()



# ---------------------------------------------------------------------------
# Local-edit protection in do_update (ported from paperclipai/paperclip#10978)
# ---------------------------------------------------------------------------


def _update_env(monkeypatch, tmp_path, *, edit_after_install: bool):
    """Install a fake hub skill on disk, optionally edit it, and wire mocks.

    Returns (console_sink, installs_list).
    """
    import hermes_cli.skills_hub as cli_hub
    import tools.skills_hub as hub
    import tools.skills_hub_install as hub_install
    from tools.skills_guard import content_hash

    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "category" / "hub-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# hub-skill\noriginal\n")

    recorded = content_hash(skill_dir)
    if edit_after_install:
        (skill_dir / "SKILL.md").write_text("# hub-skill\nuser edited\n")

    monkeypatch.setattr(hub, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(hub_install, "check_for_skill_updates", lambda **_kwargs: [{
        "name": "hub-skill",
        "identifier": "someone/hub-skill",
        "source": "github",
        "status": "update_available",
    }])
    monkeypatch.setattr(hub, "HubLockFile", lambda: type("L", (), {
        "get_installed": lambda self, name: {
            "install_path": "category/hub-skill",
            "content_hash": recorded,
        }
    })())

    installs = []
    monkeypatch.setattr(
        cli_hub, "do_install",
        lambda identifier, category="", force=False, console=None, source_id=None:
            installs.append(identifier),
    )

    sink = StringIO()
    console = Console(file=sink, force_terminal=False, color_system=None)
    return console, sink, installs


def test_do_update_skips_locally_edited_skill(monkeypatch, tmp_path):
    """A hub skill whose on-disk hash drifted from the lockfile is skipped."""
    console, sink, installs = _update_env(monkeypatch, tmp_path, edit_after_install=True)

    do_update(console=console)

    assert installs == []
    out = sink.getvalue()
    assert "local edits" in out
    assert "--force" in out


def test_do_update_force_overwrites_local_edits(monkeypatch, tmp_path):
    """--force restores the destructive replace for edited skills."""
    console, sink, installs = _update_env(monkeypatch, tmp_path, edit_after_install=True)

    do_update(console=console, force=True)

    assert installs == ["someone/hub-skill"]
    assert "local edits" not in sink.getvalue()


def test_do_update_unmodified_skill_updates_normally(monkeypatch, tmp_path):
    """No local drift -> the update proceeds without --force."""
    console, sink, installs = _update_env(monkeypatch, tmp_path, edit_after_install=False)

    do_update(console=console)

    assert installs == ["someone/hub-skill"]
    assert "Updated 1 skill(s)" in sink.getvalue()


# ---------------------------------------------------------------------------
# Stale index entry messages (#3259)
# ---------------------------------------------------------------------------


def _stale_env(monkeypatch):
    """do_install where the index has metadata but the files are gone (404)."""
    import hermes_cli.skills_hub as cli_hub
    import tools.skills_hub as hub

    class StaleSource:
        def source_id(self):
            return "skills-sh"

    meta = type("Meta", (), {"identifier": "skills-sh/org/gone-skill"})()
    monkeypatch.setattr(hub, "ensure_hub_dirs", lambda: None)
    monkeypatch.setattr(cli_hub, "_sources", lambda: [StaleSource()])
    monkeypatch.setattr(
        cli_hub, "_resolve_source_meta_and_bundle",
        lambda identifier, sources: (meta, None, sources[0]))
    sink = StringIO()
    console = Console(file=sink, force_terminal=False, color_system=None)
    return console, sink


def test_do_install_stale_index_names_the_problem(monkeypatch):
    """Index hit + missing files reads as a stale entry, not a typo (#3259)."""
    from hermes_cli.skills_hub import do_install

    console, sink = _stale_env(monkeypatch)
    do_install("skills-sh/org/gone-skill", console=console, skip_confirm=True)

    out = sink.getvalue()
    assert "Stale index entry" in out
    assert "skills-sh" in out
    assert "Could not fetch" not in out


@pytest.mark.parametrize("meta_hit", [False, True])
def test_do_install_generic_when_no_index_hit_or_rate_limited(monkeypatch, meta_hit):
    """No index hit — or a throttled fetch that only *looks* like a stale entry — keeps the
    generic message (plus the rate-limit hint), never the stale-entry verdict."""
    import hermes_cli.skills_hub as cli_hub
    import tools.skills_hub as hub
    from hermes_cli.skills_hub import do_install

    class ThrottledSource:
        is_rate_limited = meta_hit

        def source_id(self):
            return "skills-sh"

    meta = type("Meta", (), {"identifier": "skills-sh/org/gone-skill"})() if meta_hit else None
    src = ThrottledSource()
    monkeypatch.setattr(hub, "ensure_hub_dirs", lambda: None)
    monkeypatch.setattr(cli_hub, "_sources", lambda: [src])
    monkeypatch.setattr(
        cli_hub, "_resolve_source_meta_and_bundle",
        lambda identifier, sources: (meta, None, src if meta_hit else None))
    sink = StringIO()
    console = Console(file=sink, force_terminal=False, color_system=None)
    do_install("skills-sh/org/gone-skill", console=console, skip_confirm=True)

    out = sink.getvalue()
    assert "Could not download" in out
    assert "Stale index entry" not in out
    assert ("rate limit" in out) is meta_hit

# ---------------------------------------------------------------------------
# Default catalog is fork-owned (fork policy: no automatic Nous fetch)
# ---------------------------------------------------------------------------


class TestDefaultCatalogIsForkOwned:
    """With no ``--source`` flag the hub consults only this fork's official
    catalog. The upstream Nous docs skills-index integration is GONE: no
    code path fetches it, an old (even poisoned) ``hermes-index.json`` cache
    has no reader left, and explicit ``--source all`` queries the community
    registries directly instead of any proxy catalog."""

    @staticmethod
    def _poison_entry():
        return {
            "name": "neuroskill-bci",
            "description": "Upstream copy of a skill this fork deleted.",
            "source": "github",
            "identifier": "NousResearch/hermes-agent/optional-skills/health/neuroskill-bci",
            "repo": "NousResearch/hermes-agent",
            "path": "optional-skills/health/neuroskill-bci",
            "trust_level": "community",
            "tags": ["bci"],
        }

    @pytest.fixture()
    def fork_router(self, monkeypatch, hub_env):
        """Real router + poisoned leftover index cache + network recorder."""
        import json as _json
        from unittest.mock import MagicMock
        import httpx
        import tools.skills_hub as hub
        import tools.skills_hub_github as hub_github

        cache = hub.INDEX_CACHE_DIR / "hermes-index.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(_json.dumps({"skills": [self._poison_entry()]}), encoding="utf-8")

        # Recorder: any hub HTTP call is recorded; only the removed index URL
        # is a contract violation (community registries may phone home when
        # explicitly selected).
        fetched = []

        def _recording_get(url, *args, **kwargs):
            fetched.append(str(url))
            raise httpx.ConnectError("network disabled in test")

        monkeypatch.setattr(httpx, "get", _recording_get)

        # Client-based fetches (SSRF-guarded get/stream) get the same recorder.
        class _RecordingClient:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, url, **kwargs):
                return _recording_get(url, **kwargs)

            def stream(self, method, url, **kwargs):
                return _recording_get(url, **kwargs)

        monkeypatch.setattr(
            "tools.url_safety.create_ssrf_safe_client", lambda **kwargs: _RecordingClient())

        tree_repos = []
        monkeypatch.setattr(
            hub_github.GitHubSource, "_get_repo_tree",
            lambda self, repo: (tree_repos.append(repo), None)[1])
        monkeypatch.setattr(hub_github, "GitHubAuth", MagicMock())
        return {"fetched": fetched, "tree_repos": tree_repos}

    def _assert_no_index_fetch(self, fork_router):
        fetched = fork_router["fetched"]
        assert not any(
            "skills-index" in url or "nousresearch.com/docs/api" in url for url in fetched
        ), f"upstream docs index fetched: {fetched}"

    def _do_search_json(self, capsys, query, **kwargs):
        from hermes_cli.skills_hub import do_search
        import json as _json

        sink = StringIO()
        console = Console(file=sink, force_terminal=False, color_system=None, width=120)
        do_search(query, console=console, as_json=True, **kwargs)
        return _json.loads(capsys.readouterr().out)

    def test_default_search_is_official_only_and_ignores_poisoned_index(self, fork_router, capsys):
        rows = self._do_search_json(capsys, "neuroskill")

        assert rows == [], f"default catalog must not surface upstream copies: {rows}"
        # Default search is fork-catalog-only: no HTTP at all, poisoned cache unread.
        assert fork_router["fetched"] == [], f"default search phoned home: {fork_router['fetched']}"
        assert "NousResearch/hermes-agent" not in fork_router["tree_repos"]

    def test_default_browse_lists_only_official_source(self, fork_router):
        from hermes_cli.skills_hub import browse_skills

        page = browse_skills(page_size=100)  # one page: every catalog entry visible

        assert page["total"] > 0  # the fork catalog itself is reachable
        assert {item["source"] for item in page["items"]} == {"official"}
        assert all("neuroskill" not in item["identifier"] for item in page["items"])
        self._assert_no_index_fetch(fork_router)

    def test_short_name_resolution_defaults_to_official(self, fork_router):
        from hermes_cli.skills_hub import _resolve_short_name
        from tools.skills_hub_search import create_source_router

        sink = StringIO()
        console = Console(file=sink, force_terminal=False, color_system=None, width=120)
        sources = create_source_router()

        assert _resolve_short_name("neuroskill-bci", sources, console) == ""

        from tools.skills_hub_official import OptionalSkillSource
        local_name = OptionalSkillSource().list_local()[0].name
        assert _resolve_short_name(local_name, sources, console).startswith("official/")

    def test_explicit_all_queries_registries_directly_never_the_index(self, fork_router, capsys):
        import tools.skills_hub_skillssh as skillssh

        searched = []
        original = skillssh.SkillsShSource.search

        def _record(self, query, limit=10):
            searched.append(query)
            return original(self, query, limit=limit)

        skillssh.SkillsShSource.search = _record
        try:
            # Explicit "all" queries the community registries directly — the
            # results may be empty here (network disabled), but the registries
            # must have been ASKED and the removed index never fetched.
            self._do_search_json(capsys, "neuroskill", source="all")
            self._assert_no_index_fetch(fork_router)
            assert "neuroskill" in searched, (
                "explicit --source all must query community registries directly")

            self._do_search_json(capsys, "neuroskill", source="skills-sh")
            assert searched.count("neuroskill") == 2, (
                "explicit --source skills-sh must still query the registry")
        finally:
            skillssh.SkillsShSource.search = original

    def test_default_router_retains_community_adapters_for_explicit_installs(self, fork_router):
        from tools.skills_hub_clawhub import ClawHubSource
        from tools.skills_hub_github import GitHubSource
        from tools.skills_hub_official import OptionalSkillSource
        from tools.skills_hub_search import create_source_router
        from tools.skills_hub_skillssh import SkillsShSource
        from tools.skills_hub_sources import BrowseShSource, LobeHubSource, UrlSource, WellKnownSkillSource

        sources = create_source_router()

        for adapter in (OptionalSkillSource, SkillsShSource, WellKnownSkillSource,
                        UrlSource, GitHubSource, ClawHubSource, LobeHubSource, BrowseShSource):
            assert any(isinstance(src, adapter) for src in sources), (
                f"{adapter.__name__} must stay constructible for explicit installs")
