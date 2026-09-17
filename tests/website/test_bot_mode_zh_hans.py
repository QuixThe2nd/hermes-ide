"""Regression for issue #94366.

The zh-Hans docs tree had no translation of the Bot Mode user guide, so
zh-Hans readers silently fell back to the English page. This keeps the
translation structurally aligned with the English source: same headings
in the same order, and machine-readable values in code blocks (commands,
config keys, RPC payloads) preserved byte-for-byte.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EN_DOC = REPO_ROOT / "website" / "docs" / "user-guide" / "bot-mode.md"
ZH_DOC = (
    REPO_ROOT
    / "website"
    / "i18n"
    / "zh-Hans"
    / "docusaurus-plugin-content-docs"
    / "current"
    / "user-guide"
    / "bot-mode.md"
)

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
CODE_BLOCK_RE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)


def _heading_levels(text: str) -> list[int]:
    return [len(level) for level, _ in HEADING_RE.findall(text)]


def test_heading_structure_matches_english():
    en_levels = _heading_levels(EN_DOC.read_text(encoding="utf-8"))
    zh_levels = _heading_levels(ZH_DOC.read_text(encoding="utf-8"))
    assert zh_levels == en_levels, (
        "zh-Hans heading levels/order must mirror the English source "
        f"(english={en_levels}, zh-Hans={zh_levels})"
    )


def test_code_blocks_preserve_machine_readable_values():
    en_blocks = [b.strip() for b in CODE_BLOCK_RE.findall(EN_DOC.read_text(encoding="utf-8"))]
    zh_blocks = [b.strip() for b in CODE_BLOCK_RE.findall(ZH_DOC.read_text(encoding="utf-8"))]
    assert len(en_blocks) == len(zh_blocks)
    for en_block, zh_block in zip(en_blocks, zh_blocks):
        # Compare non-comment lines verbatim: commands, paths, and config
        # keys must stay byte-accurate even though comments are translated.
        en_code_lines = [line.split("#", 1)[0].rstrip() for line in en_block.splitlines()]
        zh_code_lines = [line.split("#", 1)[0].rstrip() for line in zh_block.splitlines()]
        assert en_code_lines == zh_code_lines
