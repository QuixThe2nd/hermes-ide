"""Load stored-whole system prompts from the files next to this module.

Files are UTF-8 text and are returned VERBATIM — the loader never trims or
appends whitespace, so each prompt keeps the exact bytes it had as an inline
literal (see tests/prompts/test_prompt_files.py for the byte contract).
"""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).resolve().parent
_cache: dict[str, str] = {}


def load(name: str) -> str:
    """Return ``<name>.txt`` from this directory verbatim (cached — files are immutable data)."""
    text = _cache.get(name)
    if text is None:
        text = (_DIR / f"{name}.txt").read_text(encoding="utf-8")
        _cache[name] = text
    return text
