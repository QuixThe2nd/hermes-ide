"""Session title mixin for SessionDB: sanitizing, auto/user provenance ranking, and
lineage-aware lookups."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from agent.message_sanitization import _sanitize_surrogates
from hermes_state_common import _COMPRESSION_CHILD_SQL, escape_like as _escape_like

# caplog tests pin the "hermes_state" logger name.
logger = logging.getLogger("hermes_state")

# ASCII controls (keeping \t \n \r for the whitespace collapse), then zero-width,
# bidi override, object-replacement and interlinear-annotation code points.
_TITLE_CONTROL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_TITLE_INVISIBLE_RE = re.compile(r'[\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ufffc\ufff9-\ufffb]')
_NUMBERED_TITLE_RE = re.compile(r'^(.*?) #(\d+)$')


class SessionTitlesMixin:
    """Sanitizing, ranking auto/user titles, lineage-aware lookups."""

    @classmethod
    def _title_rank(cls, source: Optional[str]) -> int:
        """Rank a stored title_source. NULL (pre-provenance rows) is indistinguishable from a
        manual ``/title`` of that era, so it ranks as ``user``."""
        rank = cls._TITLE_SOURCE_RANK
        return rank[cls.TITLE_SOURCE_USER] if source is None else rank.get(str(source), 0)

    @staticmethod
    def sanitize_title(title: Optional[str]) -> Optional[str]:
        """Strip control/zero-width/bidi chars (and lone surrogates sqlite3 cannot bind),
        collapse whitespace, normalize empty to None. ValueError past MAX_TITLE_LENGTH."""
        from hermes_state import SessionDB
        if not title:
            return None
        cleaned = _TITLE_INVISIBLE_RE.sub('', _TITLE_CONTROL_RE.sub('', _sanitize_surrogates(title)))
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        if not cleaned:
            return None
        if len(cleaned) > SessionDB.MAX_TITLE_LENGTH:
            raise ValueError(f"Title too long ({len(cleaned)} chars, max {SessionDB.MAX_TITLE_LENGTH})")
        return cleaned

