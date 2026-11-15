"""Shared fork coding conventions for delegated coding agents."""

from pathlib import Path

from hermes_constants import get_bundled_skills_dir


_CONVENTIONS_PATH = (
    get_bundled_skills_dir(Path(__file__).resolve().parent.parent / "skills")
    / "dev-lanes/SOUL.md"
)


def _read_conventions(path: Path) -> tuple[str, str]:
    conventions = path.read_text(encoding="utf-8").strip()
    _prefix, ownership_heading, remainder = conventions.partition("\n## Ownership\n")
    guidance, following_heading, _suffix = remainder.partition("\n## ")
    if not ownership_heading or not following_heading or not guidance.strip():
        raise ValueError(
            f"Malformed fork coding conventions in {path}: expected a non-empty "
            "'## Ownership' section followed by another level-two heading"
        )
    return conventions, guidance.strip()


FORK_CODING_CONVENTIONS, CODEBASE_OWNERSHIP_GUIDANCE = _read_conventions(_CONVENTIONS_PATH)
CODEBASE_OWNERSHIP_SENTENCE = CODEBASE_OWNERSHIP_GUIDANCE.split("\n\n", 1)[0]

# Pre-SOUL callers used one paragraph and lacked the publishing-agent clause.
_LEGACY_CODEBASE_OWNERSHIP_GUIDANCE = " ".join(
    CODEBASE_OWNERSHIP_GUIDANCE.split("\n\n")
).replace(", so the publishing agent can see what went beyond the brief", "")


def ensure_codebase_ownership(text: str) -> str:
    """Add the complete conventions once, expanding legacy ownership material."""
    if FORK_CODING_CONVENTIONS in text:
        return text
    if _LEGACY_CODEBASE_OWNERSHIP_GUIDANCE in text:
        return text.replace(
            _LEGACY_CODEBASE_OWNERSHIP_GUIDANCE, FORK_CODING_CONVENTIONS, 1
        )
    if CODEBASE_OWNERSHIP_GUIDANCE in text:
        return text.replace(CODEBASE_OWNERSHIP_GUIDANCE, FORK_CODING_CONVENTIONS, 1)
    if CODEBASE_OWNERSHIP_SENTENCE in text:
        return text.replace(CODEBASE_OWNERSHIP_SENTENCE, FORK_CODING_CONVENTIONS, 1)
    return f"{FORK_CODING_CONVENTIONS}\n\n{text}" if text else FORK_CODING_CONVENTIONS
