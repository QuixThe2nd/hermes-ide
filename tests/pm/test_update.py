"""Pure unit tests for pm.update (no network, no store, no lockfile writes).

The upstream index helpers (github_release_tags, npm_dist_tags, ...) are
network I/O — covered by design, never in tests. The resolution engine is
pure: candidate lists in, Resolved decision out.
"""

from __future__ import annotations

from pm.update import (
    Resolved,
    best_in_minor,
    minor_of,
    resolve_best,
    resolve_package,
    version_key,
)


def _pkg(pkg_name="node", version_style="semver", **latest):
    """A stub package whose latest_versions returns canned per-target lists."""

    class _P:
        def __init__(self):
            self.name = pkg_name
            self.version_style = version_style
            self._latest = dict(latest)

        def latest_versions(self, target, locked=None):
            return list(self._latest.get(target, []))

    return _P()


# ── version parsing / comparison ───────────────────────────────────────────


def test_version_key_sorts_numeric_and_suffixes():
    assert version_key("2.53.0+5") > version_key("2.53.0+3")
    assert version_key("3.11.16+20260814") > version_key("3.11.16+20260801")
    assert version_key("26.8.1") > version_key("26.7.0")
    assert version_key("10362") > version_key("10361")
    # prerelease-ish segments sort after numerics
    assert version_key("9.0.1") < version_key("9.0.1-rc1")


def test_minor_of():
    assert minor_of("26.7.0") == (26, 7)
    assert minor_of("3.11.16+20260814") == (3, 11)
    assert minor_of("10362") is None  # single component


def test_best_in_minor():
    versions = ["9.0.3", "9.0.1", "9.1.0", "8.4.9"]
    assert best_in_minor(versions, (9, 0)) == "9.0.3"
    assert best_in_minor(versions, (9, 1)) == "9.1.0"
    assert best_in_minor(versions, (10, 0)) is None


# ── semver resolution ──────────────────────────────────────────────────────


def test_semver_picks_highest_shared_version():
    r = resolve_best(
        "node",
        ["win32-x64", "linux-x64"],
        {"win32-x64": ["26.8.1", "26.7.0"], "linux-x64": ["26.8.1", "26.7.0"]},
        locked="26.7.0",
        style="semver",
    )
    assert r.changed
    assert r.version == "26.8.1"
    assert r.per_target == {"win32-x64": "26.8.1", "linux-x64": "26.8.1"}


def test_semver_no_update_when_up_to_date():
    r = resolve_best(
        "node",
        ["win32-x64"],
        {"win32-x64": ["26.7.0"]},
        locked="26.7.0",
        style="semver",
    )
    assert not r.changed
    assert r.version == "26.7.0"


def test_semver_divergent_targets_no_shared_version():
    r = resolve_best(
        "gh",
        ["win32-x64", "linux-x64"],
        {"win32-x64": ["2.97.0"], "linux-x64": ["2.96.0"]},
        locked="2.95.0",
        style="semver",
    )
    assert r.version is None
    assert r.reason == "no shared version"
    assert not r.changed


# ── minor-style (ffmpeg) resolution ───────────────────────────────────────


def test_minor_style_shared_minor_with_per_target_patches():
    """The cadence-mismatch case: posix and win32 drift in PATCH. The update
    moves to the highest shared major.minor; each target pins its own patch."""
    r = resolve_best(
        "ffmpeg",
        ["linux-x64", "win32-x64"],
        {"linux-x64": ["9.1.2", "9.0.1"], "win32-x64": ["9.1.0", "9.0.1"]},
        locked="9.0.1",
        style="minor",
    )
    assert r.changed
    assert r.version == "9.1"
    assert r.per_target == {"linux-x64": "9.1.2", "win32-x64": "9.1.0"}


def test_minor_style_no_shared_minor_blocks_update():
    """posix on 9.1, win32 still on 9.0 — no minor every target serves."""
    r = resolve_best(
        "ffmpeg",
        ["linux-x64", "win32-x64"],
        {"linux-x64": ["9.1.2"], "win32-x64": ["9.0.3"]},
        locked="9.0.1",
        style="minor",
    )
    assert r.version is None
    assert r.reason == "no shared minor"
    assert not r.changed


def test_minor_style_patch_drift_within_shared_minor_is_up_to_date():
    """Same minor, patch drift only — the lockfile version label doesn't
    move (patches live in per-target urls)."""
    r = resolve_best(
        "ffmpeg",
        ["linux-x64", "win32-x64"],
        {"linux-x64": ["9.1.2"], "win32-x64": ["9.1.0"]},
        locked="9.1",
        style="minor",
    )
    assert not r.changed
    assert r.version == "9.1"


# ── source availability ────────────────────────────────────────────────────


def test_no_source_anywhere():
    r = resolve_best("chromium", ["win32-x64"], {"win32-x64": []}, locked="1208+145", style="semver")
    assert r.version is None
    assert r.reason == "no source"
    assert not r.changed


def test_missing_source_for_one_target_skipped():
    """A target whose index is unreachable must not block the others."""
    r = resolve_best(
        "node",
        ["win32-x64", "linux-x64"],
        {"win32-x64": ["26.8.1"], "linux-x64": []},
        locked="26.7.0",
        style="semver",
    )
    assert r.changed
    assert r.version == "26.8.1"


# ── resolve_package wiring ─────────────────────────────────────────────────


def test_resolve_package_calls_latest_versions_per_target():
    pkg = _pkg(
        "node",
        **{
            "win32-x64": ["26.8.1", "26.7.0"],
            "linux-x64": ["26.8.1", "26.7.0"],
        },
    )
    r = resolve_package(pkg, ["win32-x64", "linux-x64"], locked="26.7.0")
    assert isinstance(r, Resolved)
    assert r.changed
    assert r.version == "26.8.1"
