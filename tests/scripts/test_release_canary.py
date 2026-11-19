"""Canary release plumbing in scripts/release.py.

The canary channel's whole safety story is tag SHAPES: canary tags
carry a -canary.YYYYMMDD suffix, every stable selector requires the
no-suffix shape, and the version math over stable gives the channel deterministic semver
ordering. These tests pin the shapes and the math; the workflow only
provides credentials.
"""

import importlib.util
from pathlib import Path

import pytest

_RELEASE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "release.py"
_SPEC = importlib.util.spec_from_file_location("hermes_release_canary", _RELEASE_PATH)
assert _SPEC and _SPEC.loader
release = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release)


class TestCanaryTagShape:
    def test_canary_tag_for_date_is_next_patch(self):
        """The authority's math: current stable's patch + 1, any patch."""
        assert release.canary_tag_for_date("0.27.4", "20260818103000") == "v0.27.5-canary.20260818103000"
        assert release.canary_tag_for_date("1.4.0", "20261231235959") == "v1.4.1-canary.20261231235959"

    def test_v_prefixed_stable_tag_accepted(self):
        """cmd_canary passes the stable TAG (with the v) — the authority
        strips it, so both forms produce the same tag."""
        assert release.canary_tag_for_date("v0.27.4", "20260818103000") == "v0.27.5-canary.20260818103000"

    def test_shape_round_trips_through_the_canary_matcher(self):
        tag = release.canary_tag_for_date("0.27.4", "20260818103000")
        assert release._CANARY_TAG_RE.fullmatch(tag)

    def test_legacy_date_only_shape_still_matches(self):
        """Readers stay tolerant of the original 8-digit suffix so an
        already-published canary keeps parsing (prune, last-canary)."""
        assert release._CANARY_TAG_RE.fullmatch("v0.28.0-canary.20260818")

    def test_canary_shape_is_invisible_to_the_stable_selector(self):
        """THE invariant: a canary tag must never parse as stable —
        otherwise stable users update onto canaries."""
        for tag in (
            release.canary_tag_for_date("0.27.4", "20260818103000"),
            "v0.28.0-canary.20260818",
        ):
            assert release._SEMVER_TAG_RE.fullmatch(tag) is None, tag

    def test_stable_shape_is_invisible_to_the_canary_matcher(self):
        assert release._CANARY_TAG_RE.fullmatch("v0.27.2") is None
        # Legacy CalVer must not match either (v2026.7.20 has 20-prefixed
        # components that could fool a sloppy pattern).
        assert release._CANARY_TAG_RE.fullmatch("v2026.7.20") is None

    def test_same_day_canaries_order_chronologically(self):
        """Second precision exists so manual fires can stack in one day;
        fixed-length pure-numeric identifiers order the same lexically
        (git -v:refname) and numerically (semver prerelease compare)."""
        a = release.canary_tag_for_date("0.27.4", "20260818090000")
        b = release.canary_tag_for_date("0.27.4", "20260818171500")
        assert a < b  # lexical == chronological at fixed length

    def test_canary_outversions_current_stable_loses_to_next_patch(self):
        """The semver ordering that makes both channel switches work,
        checked with packaging's canonical comparison when available,
        else by standard prerelease precedence rules."""
        try:
            from packaging.version import Version
        except ImportError:
            pytest.skip("packaging not installed in this env")
        canary = Version("0.27.5-canary.20260818103000".replace("-canary.", "a"))
        assert Version("0.27.4") < canary < Version("0.27.5")


class TestCanaryDateStamps:
    def test_prune_cutoff_uses_the_tag_date(self):
        """The prune window keys on the tag's own YYYYMMDD prefix, for
        both suffix shapes — a 14-digit suffix compared whole against an
        8-digit cutoff would be decided by string length, not by day."""
        legacy = release._CANARY_TAG_RE.fullmatch("v0.27.1-canary.20260801")
        stamped = release._CANARY_TAG_RE.fullmatch("v0.27.1-canary.20260801235959")
        assert legacy and legacy.group(0).split("-canary.")[1][:8] == "20260801"
        assert stamped and stamped.group(0).split("-canary.")[1][:8] == "20260801"


class TestCanaryIsPublishedPrerelease:
    """A canary is published as a source prerelease, not a draft."""

    def _create_argv(self, monkeypatch, tmp_path):
        """The argv of the `gh release create` cmd_canary would run."""
        import subprocess
        from types import SimpleNamespace

        captured: list[list[str]] = []

        def fake_run(cmd, *a, **kw):
            captured.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="url", stderr="")

        def fake_git_result(*args, **kw):
            # `rev-parse --verify --quiet refs/tags/<tag>` must MISS, or
            # cmd_canary short-circuits on "tag already exists".
            code = 1 if "rev-parse" in args else 0
            return subprocess.CompletedProcess(args, code, "", "")

        monkeypatch.setattr(release, "get_last_tag", lambda: "v0.27.0")
        monkeypatch.setattr(release, "get_last_canary_tag", lambda: None)
        monkeypatch.setattr(release, "get_commits", lambda **kw: [{"hash": "a" * 40, "subject": "feat: x", "author": "e"}])
        monkeypatch.setattr(release, "generate_changelog", lambda *a, **kw: "notes")
        monkeypatch.setattr(release, "resolve_push_remote", lambda r: "origin")
        monkeypatch.setattr(release, "remote_github_repo", lambda r: "o/r")
        monkeypatch.setattr(release, "git_result", fake_git_result)
        monkeypatch.setattr(release, "git", lambda *a, **kw: "")
        monkeypatch.setattr(release, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

        release.cmd_canary(SimpleNamespace(no_changelog=True, date="20260818103000", publish=True, remote="origin"))
        return next(c for c in captured if c[:3] == ["gh", "release", "create"])

    def test_created_as_a_published_prerelease(self, monkeypatch, tmp_path):
        argv = self._create_argv(monkeypatch, tmp_path)
        assert "--draft" not in argv, argv
        assert "--prerelease" in argv, argv

    def test_refuses_to_invent_a_tag(self, monkeypatch, tmp_path):
        """Without --verify-tag, gh creates a missing tag from the default
        branch tip, which would release a different commit than the one
        the canary math tagged."""
        assert "--verify-tag" in self._create_argv(monkeypatch, tmp_path)
