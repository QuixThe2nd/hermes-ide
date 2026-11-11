"""Shared ownership guidance for delegated coding agents."""

CODEBASE_OWNERSHIP_SENTENCE = (
    "This codebase is yours: what it's for and how it's built are your call, "
    "and you'll keep building on it."
)

CODEBASE_OWNERSHIP_GUIDANCE = (
    CODEBASE_OWNERSHIP_SENTENCE
    + " In the area this task touches, act as an owner, not a guest: merge "
    "duplicate code into one implementation instead of adding a parallel one, "
    "and fix related bugs you find rather than only mentioning them. Put each "
    "cleanup or adjacent fix in its own commit with its own test, and list it "
    "in your final report. Unrelated areas stay out of the diff; report "
    "observations instead. For static analysis, ownership applies to the "
    "working repository, never to analyzed target artifacts."
)


def ensure_codebase_ownership(text: str) -> str:
    """Add the guidance once, expanding a pre-existing standalone sentence."""
    if CODEBASE_OWNERSHIP_GUIDANCE in text:
        return text
    if CODEBASE_OWNERSHIP_SENTENCE in text:
        return text.replace(CODEBASE_OWNERSHIP_SENTENCE, CODEBASE_OWNERSHIP_GUIDANCE, 1)
    if not text:
        return CODEBASE_OWNERSHIP_GUIDANCE
    return f"{CODEBASE_OWNERSHIP_GUIDANCE}\n\n{text}"
