"""Contract checks for the self-hosted-application-operations skill's
static-site publication reference (absorbs the retired publish-site skill).

Structural + relational checks only (stdlib + pytest, no network).
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO / "skills" / "devops" / "self-hosted-application-operations"
SKILL_MD = SKILL_DIR / "SKILL.md"
REFERENCE = SKILL_DIR / "references" / "static-site-publication.md"

# The retired provider ladder must not come back inside the reference.
PROVIDER_LADDER_MARKERS = (
    "wrangler",
    "netlify",
    "gh-pages",
    "github.io",
    "cloudflared",
    "trycloudflare.com",
)


@pytest.fixture(scope="module")
def skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def reference_text() -> str:
    return REFERENCE.read_text(encoding="utf-8")


def test_skill_and_reference_exist():
    assert SKILL_MD.is_file(), f"missing {SKILL_MD}"
    assert REFERENCE.is_file(), f"missing {REFERENCE}"


def test_retired_optional_skill_removed():
    old = REPO / "optional-skills" / "web-development" / "publish-site"
    assert not old.exists(), f"retired optional skill still present: {old}"


def test_skill_frontmatter(skill_text: str):
    m = re.match(r"^---\n(.*?)\n---\n", skill_text, re.DOTALL)
    assert m, "SKILL.md must open with frontmatter"
    fm = dict(
        line.split(":", 1)
        for line in m.group(1).splitlines()
        if line and not line.startswith((" ", "\t"))
    )
    assert fm["name"].strip() == "self-hosted-application-operations"
    desc = fm["description"].strip().strip('"')
    assert len(desc) <= 60, f"description is {len(desc)} chars (>60)"
    assert desc.endswith(".")


def test_skill_routes_to_reference(skill_text: str):
    """Small SKILL.md whose procedure hands off to the reference file."""
    assert "references/static-site-publication.md" in skill_text
    lines = skill_text.count("\n") + 1
    assert lines <= 100, f"router SKILL.md should stay small; is {lines} lines"


def test_existing_infrastructure_first(skill_text: str, reference_text: str):
    """Homelab/existing infrastructure first; no cloud-provider ladder."""
    for text in (skill_text, reference_text):
        assert "existing" in text.lower()
        lowered = text.lower()
        for marker in PROVIDER_LADDER_MARKERS:
            assert marker not in lowered, f"cloud ladder marker present: {marker}"


def test_no_auto_public_repos_or_tunnels(reference_text: str):
    assert "explicit user request" in reference_text
    assert "tunnels" in reference_text


def test_immutable_release_and_rollback(reference_text: str):
    assert "versioned release" in reference_text
    assert "previous" in reference_text and "rollback" in reference_text.lower()
    # Rollback must switch artifacts, never clobber a dirty tree or live output.
    assert "immutable" in reference_text or "switching to the previous" in reference_text
    assert "Never overwrite a dirty tree" in reference_text
    assert "hand-edit live output" in reference_text


def test_preview_signoff_and_secrets(reference_text: str):
    assert "Preview" in reference_text
    assert "sign-off" in reference_text
    assert "credentials" in reference_text
    assert ".env" in reference_text
    assert "source maps" in reference_text


def test_build_output_not_source(reference_text: str):
    assert "output directory" in reference_text
    assert "not source" in reference_text
    assert "index.html" in reference_text


def test_acceptance_checks(reference_text: str):
    """Origin+edge, assets, SPA deep route, real user flow."""
    assert "origin" in reference_text and "edge" in reference_text
    assert "content type" in reference_text
    assert "deep route" in reference_text
    assert "user flow" in reference_text
    # HTTP 200 alone is insufficient — content must be asserted.
    assert "HTTP 200 alone is insufficient" in reference_text


def test_reporting_discipline(reference_text: str):
    """Report must disclose unexercised checks; upload != deployment."""
    assert "Disclose any checks not exercised" in reference_text
    assert "a successful upload is not a successful deployment" in reference_text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
