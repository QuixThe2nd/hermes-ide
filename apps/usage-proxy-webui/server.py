#!/usr/bin/env python3
"""Live, dark-themed dashboard for the Hermes usage-proxy SQLite ledger.

Single file, Python stdlib only (http.server + sqlite3 + json):

* the ledger is opened read-only per request (``mode=ro``): the dashboard can
  never block the proxy's writers and can never modify the ledger;
* **one shared filter state** (``Filters``) narrows everything consistently:
  time-range preset, harness, provider (the ledger's actual upstream route),
  model, chat type, chat, route and outcome.
  Exact selections AND across facets; every breakdown/facet list is computed
  over the *other* filters (cross-filtered) so it stays usable when narrowed,
  while the stat cards, time chart, donut, per-chat table and event list
  apply *all* filters.  Aggregates are computed over the full filtered
  ledger in SQL — never over the latest N events — and events are filtered
  before the LIMIT.  The state lives in the page URL (``?range=7d&harness=…``
  …), so a refresh retains it and a copied link reproduces it; the first
  paint is server-rendered under the same filters;
* chat attribution comes from the proxy's ``chat_type``/``chat_id``/
  ``chat_name`` ledger columns (written from Hermes's routed transports,
  see ``plugins/llm_usage_proxy/server.py``).  Rows without identity —
  written before the columns existed, or by harnesses that carry no Hermes
  session — are the explicit **Unknown** chat; nothing is ever inferred
  from timestamps or models.  An older ledger without the columns is read
  as-is (never migrated here): the chat facets simply report Unknown only;
* every SQL statement is assembled exclusively from this module's fixed
  fragment literals (chosen by allowlisted filter keys), and request-supplied
  values only ever travel as bound ``?`` parameters — never spliced into
  the SQL text;
* every model name — donut legend, per-model chart mode and events table —
  carries its provider's brand: an inline SVG logo (Simple Icons CC0 path
  data, Z.ai/Kimi as initial badges) and the brand colour on donut slices
  and per-model series (``PROVIDER_BRANDS``, mirrored as ``PROVIDER_HEXES``
  in the page JS); models from unknown providers keep the neutral palette;
* every harness chip, share bar, harness-mode chart segment and events-table
  caller cell carries the caller's identity colour from a stable prefix map
  (``HARNESS_BRANDS``, mirrored as ``HARNESS_HEX`` entries in the page JS and
  as ``.hb-*`` rules in the CSS), so a known caller keeps its colour even
  when its rank shifts; unknown callers keep the hashed rank palette and
  ``unattributed`` stays neutral — colour only, no logos;
* ``GET /`` serves the single-page dark dashboard.  Its JavaScript polls
  ``/api/summary``, ``/api/timeseries`` and ``/api/events`` every 5 s (each
  carrying the current filter query) and updates the stat cards, the filter
  bar, the per-harness bars, the per-chat table, the canvas charts (tokens
  per bucket, stacked by harness / model / chat / chat type / in-out / cache
  — and the model-usage donut) and the event table in place — no full page
  reloads.  The first paint is server-rendered from the same data, so the
  page is meaningful even with JavaScript disabled (the chart then shows as
  an accessible data table);
* any database failure (missing file, locked, corrupt) degrades to a soft
  error payload at HTTP 200, so the poll loop never crashes;
* nothing is written to stdout while serving (access logs are suppressed;
  the startup banner and real errors go to stderr).

Launch flags are unchanged — see ``usage-proxy-webui.service``.
"""

import argparse
from bisect import bisect_right
import html
import json
from pathlib import Path
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping, Optional
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

SYDNEY = ZoneInfo("Australia/Sydney")
UNATTRIBUTED = "unattributed"
UNKNOWN = "unknown"
# The Hermes gateway's caller values: plain `hermes` (pre-profile-split
# traffic) and `hermes:<profile>` once the gateway names its profiles.  Only
# these spellings display as "Hermes IDE"; every other caller keeps its raw
# string as label (see caller_display).
HERMES_CALLER = "hermes"
HERMES_PROFILE_PREFIX = "hermes:"
HERMES_DISPLAY = "Hermes IDE"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9136
DEFAULT_DB = str(Path.home() / ".hermes" / "usage-proxy" / "usage.sqlite")

POLL_SECONDS = 5
FETCH_TIMEOUT_MS = 4500
DASHBOARD_EVENTS = 50       # rows shown in the recent-events table
API_EVENTS_DEFAULT = 200
API_EVENTS_MAX = 1000
HOURS = 24
DAYS_7D = 7

# ── Filters ───────────────────────────────────────────────────────────────────
#
# One shared, composable filter state.  ``range`` picks the primary window and
# bucket granularity of the cards/chart; the six exact-match facets combine
# with AND.  Values live in the page URL and travel to every /api/* call, so
# the browser, a shared link and the server-rendered first paint all agree.

RANGE_KEYS = ("24h", "7d", "30d", "all")
RANGE_HOURS = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30, "all": None}
RANGE_LABELS = {
    "24h": "last 24 h",
    "7d": "last 7 d",
    "30d": "last 30 d",
    "all": "all time",
}

FILTER_KEYS = ("harness", "provider", "model", "type", "chat", "route", "outcome")
# Request-supplied filter values are matched exactly against ledger text, so
# their length is bounded purely to keep a hostile query string cheap.
MAX_FILTER_CHARS = 256

# Chat keys are "<type>:<chat_id>", a bare "<type>" (traffic of that surface
# with no chat id — cli, cronjob), or "unknown" (no identity at all).  The
# type part shares the producer's charset; the id part is free text.
_CHAT_TYPE_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")

# Ledgers written before chat attribution have no chat columns; this dashboard
# reads such a schema as-is (it never writes/migrates) and every chat facet
# collapses to Unknown.
CHAT_COLUMNS = frozenset({"chat_type", "chat_id", "chat_name"})

# Facet lists are capped so a pathological ledger cannot balloon the page;
# exceeding a cap is reported (truncated), never silent.
FACET_LIMITS = {
    "harness": 100,
    "provider": 40,
    "model": 200,
    "type": 40,
    "chat": 500,
    "route": 100,
    "outcome": 20,
}

# Muted harness colours, assigned to callers by name hash (see
# harness_color_idx) so the same harness always lands on the same hue in the
# bars, the chips and the chart — on both the server and the browser.
HARNESS_COLOR_COUNT = 6

# Harness identity colours — the harness twin of PROVIDER_BRANDS: known
# callers keep a stable brand colour no matter how their token rank shifts,
# resolved ahead of the hashed rank palette above.  Longest prefix first,
# matched case-insensitively (harness_key) on the caller string, so
# hindsight-smoke/hindsight-migrate/… fold onto hindsight's teal.  Every hex
# lives in all three views of the truth — this table, the .hb-* CSS rules
# and the browser's HARNESS_HEX mirror — because the canvases cannot read
# CSS custom properties.  Colour only, no logos; the light #BFC7D3 tints the
# chip dot and share bar on the dark surface (the chip text itself keeps
# --text-2), the same contrast situation the grok slices already handle.
HARNESS_PREFIXES = (
    ("openai-codex", "codex"),   # ahead of the bare "codex" rule
    ("codex", "codex"),
    ("claude-code", "claude-code"),
    ("hermes", "hermes"),        # this dashboard's own home turf
    ("hindsight", "hindsight"),
    ("openrouter", "openrouter"),
    ("grok", "grok"),
    ("xai", "grok"),
)
HARNESS_BRANDS = {
    "hermes": "#3987e5",       # the dashboard's own accent blue
    "claude-code": "#D97757",
    "codex": "#10A37F",
    "hindsight": "#2ea79a",    # teal — clear of Claude orange, OpenAI green, --stale
    "openrouter": "#6467F2",
    "grok": "#BFC7D3",
}

# Model-usage donut: the top MODEL_TOP_N models by 24 h tokens, the remainder
# folded into an "other" bucket.  Slice colour follows token rank (index i of
# MODEL_COLORS), so neighbours never repeat and "other" always draws the
# neutral grey.  The six hues are the harness family with the lightness
# stepped into the dark band (hue held), ordered so every neighbouring pair —
# including the wrap onto "other" — clears the CVD and normal-vision floors
# on the card surface (validator: worst adjacent OKLab dE 11.5 protan/deutan,
# 16.5 normal).
MODEL_TOP_N = 6
MODEL_COLORS = ("#bd8714", "#d46c8b", "#5b8def", "#2ea79a", "#9a7be0", "#65a46c")
MODEL_OTHER_COLOR = "#66738a"   # same neutral the page uses for unattributed
DONUT_SIZE = 180                # square canvas, CSS px (device-pixel scaled in JS)

# Provider brands for model names: a small inline SVG logo plus the brand
# colour, matched longest-prefix-first, case-insensitively (provider_key).
# Like MODEL_COLORS / MODEL_HEXES, every brand colour exists in BOTH this
# table and the browser's PROVIDER_HEXES mirror — the canvases cannot read
# CSS custom properties.  Logo path data is Simple Icons (CC0): the OpenAI
# knot, the Anthropic "A", OpenRouter, and the X glyph xAI uses white-on-dark;
# Z.ai and Kimi have no dependable path, so they get a clean initial badge (a
# brand-coloured rounded square with the letter).  Everything is inline in
# this one file — no image assets, no runtime fetches.
CARD_SURFACE = "#11151c"       # --surface: same-brand repeats shade toward it

_LOGO_OPENAI = (
    "M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9"
    "A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 "
    "0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001"
    "A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 "
    "0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 "
    "0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369"
    "l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607"
    "-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 "
    ".7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 "
    "0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 "
    "0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865"
    "A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 "
    "0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-"
    ".407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 "
    "9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 "
    "4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 "
    "7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602"
    "-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z"
)
_LOGO_ANTHROPIC = (
    "M17.3041 3.541h-3.6718l6.696 16.918H24Zm-10.6082 0L0 20.459h3.7442l1.3693-3.5527"
    "h7.0052l1.3693 3.5528h3.7442L10.5363 3.5409Zm-.3712 10.2232 2.2914-5.9456 2.2914 "
    "5.9456Z"
)
_LOGO_OPENROUTER = (
    "M16.778 1.844v1.919q-.569-.026-1.138-.032-.708-.008-1.415.037c-1.93.126-4.023.728"
    "-6.149 2.237-2.911 2.066-2.731 1.95-4.14 2.75-.396.223-1.342.574-2.185.798-.841.225"
    "-1.753.333-1.751.333v4.229s.768.108 1.61.333c.842.224 1.789.575 2.185.799 1.41.798 "
    "1.228.683 4.14 2.75 2.126 1.509 4.22 2.11 6.148 2.236.88.058 1.716.041 2.555.005"
    "v1.918l7.222-4.168-7.222-4.17v2.176c-.86.038-1.611.065-2.278.021-1.364-.09-2.417"
    "-.357-3.979-1.465-2.244-1.593-2.866-2.027-3.68-2.508.889-.518 1.449-.906 3.822"
    "-2.59 1.56-1.109 2.614-1.377 3.978-1.466.667-.044 1.418-.017 2.278.02v2.176L24 "
    "6.014Z"
)
_LOGO_X = (
    "M18.901 1.153h3.68l-8.04 9.19L24 22.846h-7.406l-5.8-7.584-6.638 7.584H.474l8.6-9.83"
    "L0 1.154h7.594l5.243 6.932ZM17.61 20.644h2.039L6.486 3.24H4.298Z"
)


def _path_logo(d: str) -> str:
    """Inline logo whose glyph follows the text colour (currentColor)."""
    return (
        '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
        f'<path fill="currentColor" d="{d}"/></svg>'
    )


def _badge_logo(letter: str, tile: str) -> str:
    """Initial badge for brands without a dependable logo path."""
    return (
        '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
        f'<rect width="24" height="24" rx="6" fill="{tile}"/>'
        '<text x="12" y="17.2" text-anchor="middle" font-family="inherit" '
        f'font-size="14.5" font-weight="700" fill="#e8edf4">{letter}</text></svg>'
    )


# Longest prefix first, so "openrouter/…" never falls through to a shorter
# rule, a bare "glm" (no dash) stays neutral, and shorter rules (Kimi's
# native "k3"/"k2", the bare "o3") sit behind their longer siblings — the
# table also covers the aggregator-normalized "vendor/model" spellings
# ("anthropic/…", "openai/…", "z-ai/…", "x-ai/…", "kimi/…") ledgers record,
# and the canonical unhyphenated catalog spellings ("xai/…", "moonshotai/…").
PROVIDER_PREFIXES = (
    ("openrouter/", "openrouter"),
    ("claude-", "claude"),
    ("anthropic/", "claude"),
    ("codex", "openai"),
    ("openai/", "openai"),
    ("gpt-", "openai"),
    ("grok-", "grok"),
    ("x-ai/", "grok"),
    ("xai/", "grok"),
    ("kimi-", "kimi"),
    ("kimi/", "kimi"),
    ("moonshot/", "kimi"),
    ("moonshotai/", "kimi"),
    ("glm-", "zai"),
    ("z-ai/", "zai"),
    ("zai", "zai"),
    ("o4-", "openai"),
    ("o3", "openai"),
)

# Kimi Coding's native bare identifiers (no dash, no vendor prefix): exact
# match only, so unrelated future "k3…"/"k2…" spellings stay neutral.
PROVIDER_EXACT = {"k3": "kimi", "k2": "kimi"}

PROVIDER_BRANDS = {
    "openai": {"name": "ChatGPT/OpenAI", "color": "#10A37F", "logo": _path_logo(_LOGO_OPENAI)},
    "zai": {"name": "Z.ai", "color": "#8A8AF0", "logo": _badge_logo("Z", "#8A8AF0")},
    "kimi": {"name": "Kimi (Moonshot AI)", "color": "#5A5AF5", "logo": _badge_logo("K", "#5A5AF5")},
    "claude": {"name": "Claude (Anthropic)", "color": "#D97757", "logo": _path_logo(_LOGO_ANTHROPIC)},
    # Grok/xAI is monochrome: white X glyph, restrained light-grey slices.
    "grok": {"name": "Grok (xAI)", "color": "#BFC7D3", "glyph": "#E8EDF4", "logo": _path_logo(_LOGO_X)},
    "openrouter": {"name": "OpenRouter", "color": "#6467F2", "logo": _path_logo(_LOGO_OPENROUTER)},
}

# Outcome badge: green when usage is final, red for auth/rate-limit
# rejections, grey for everything else.
RATE_LIMIT_CODES = (401, 429)
TONE_GOOD = "good"
TONE_CRIT = "crit"
TONE_NONE = "none"

# ---------------------------------------------------------------------------
# SQL — statements are assembled exclusively from the fixed fragment literals
# in this section: the WHERE builder picks whole fragments by allowlisted
# filter key, and request-supplied values only ever travel as bound "?"
# parameters.  No request data is ever spliced into SQL text.
# ---------------------------------------------------------------------------

# Display/grouping expressions.  The chat ones degrade to constants when the
# opened ledger predates the chat columns (read as-is: no write, no migrate).
# The provider facet groups the ledger's actual ``upstream`` route name — the
# upstream the proxy forwarded to — never a brand guessed from the model.
_EXPR_CALLER = "COALESCE(NULLIF(caller, ''), 'unattributed')"
_EXPR_UPSTREAM = "COALESCE(NULLIF(upstream, ''), 'unknown')"
_EXPR_MODEL = "COALESCE(NULLIF(model, ''), 'unknown')"
_EXPR_PATH = "COALESCE(NULLIF(path, ''), 'unknown')"
_EXPR_OUTCOME = "COALESCE(NULLIF(outcome, ''), 'unknown')"


def chat_exprs(has_chat_columns: bool) -> tuple[str, str, str]:
    """``(type_expr, id_expr, name_expr)`` for this ledger's schema.

    Every expression is one of two fixed literals, chosen by whether the
    ledger has the chat columns — never by request data.  NULL/'' ids and
    types normalize to the explicit Unknown, exactly how rows without
    identity must read.
    """
    if has_chat_columns:
        return (
            "COALESCE(NULLIF(chat_type, ''), 'unknown')",
            "NULLIF(chat_id, '')",
            "COALESCE(NULLIF(chat_name, ''), '')",
        )
    return ("'unknown'", "NULL", "''")


def chat_key_expr(type_expr: str, id_expr: str) -> str:
    """"<type>:<id>", the bare type when no id, 'unknown' when neither."""
    return (
        f"CASE WHEN {id_expr} IS NULL THEN {type_expr}"
        f" ELSE {type_expr} || ':' || {id_expr} END"
    )


class Filters:
    """The one shared filter state (parsed from the query string).

    ``range_key`` picks the window/bucket preset; the six exact-match facets
    AND together.  ``None``/"" means "not filtered".  ``chat`` keeps its
    parsed ``(type, id)`` parts alongside the wire key.
    """

    __slots__ = (
        "range_key", "harness", "provider", "model", "type", "chat", "route",
        "outcome", "chat_type_part", "chat_id_part",
    )

    def __init__(self, range_key: str = "24h") -> None:
        self.range_key = range_key if range_key in RANGE_KEYS else "24h"
        self.harness: Optional[str] = None
        self.provider: Optional[str] = None
        self.model: Optional[str] = None
        self.type: Optional[str] = None
        self.chat: Optional[str] = None
        self.route: Optional[str] = None
        self.outcome: Optional[str] = None
        self.chat_type_part: Optional[str] = None
        self.chat_id_part: Optional[str] = None

    # dict-style access keeps the WHERE builder and the JS mirrors simple
    def get(self, key: str) -> Optional[str]:
        return getattr(self, key) if key in FILTER_KEYS else None

    def items(self) -> list[tuple[str, str]]:
        return [(key, value) for key in FILTER_KEYS if (value := self.get(key))]

    def query(self) -> str:
        """Canonical query string (page URL and every /api call)."""
        pairs = [("range", self.range_key)]
        pairs.extend(self.items())
        return urlencode(pairs)

    def active_count(self) -> int:
        return len(self.items())


def _clean_filter_value(value: str) -> Optional[str]:
    text = value.strip()
    if not text or len(text) > MAX_FILTER_CHARS:
        return None
    return text


def parse_filters(query: Mapping[str, list[str]]) -> Filters:
    """Strict parser: unknown range keys, junk values and over-long values
    are dropped, never guessed at.  A chat key must be 'unknown', a bare
    type, or '<type>:<id>'; anything else cannot match a real chat."""
    filters = Filters()
    raw_range = (query.get("range") or [""])[0].strip()
    if raw_range in RANGE_KEYS:
        filters.range_key = raw_range
    for key in FILTER_KEYS:
        raw = (query.get(key) or [""])[0]
        value = _clean_filter_value(raw)
        if value is None:
            continue
        if key == "chat":
            if value == UNKNOWN:
                filters.chat = value  # no identity at all
            elif ":" in value:
                type_part, _, id_part = value.partition(":")
                if (
                    _CHAT_TYPE_RE.match(type_part)
                    and id_part
                    and len(id_part) <= MAX_FILTER_CHARS
                ):
                    filters.chat = value
                    filters.chat_type_part = type_part
                    filters.chat_id_part = id_part
            elif _CHAT_TYPE_RE.match(value):
                filters.chat = value  # a surface's id-less traffic
                filters.chat_type_part = value
                filters.chat_id_part = None
        else:
            setattr(filters, key, value)
    return filters


def where_clause(
    filters: Filters,
    has_chat_columns: bool,
    *,
    exclude: Optional[str] = None,
    with_range: bool = True,
    now: Optional[datetime] = None,
) -> tuple[str, tuple[Any, ...]]:
    """Static WHERE fragments for the active filters (+ the time range).

    ``exclude`` names the one facet dimension whose own filter is left out —
    that is how a breakdown stays usable when narrowed (cross-filtering).
    Every fragment below is a fixed literal keyed by the allowlisted filter
    name; the values are bound parameters and nothing else.  ``now`` pins the
    rolling cutoff to the snapshot's shared instant (see ``cutoff_iso``).
    """
    type_expr, id_expr, _name_expr = chat_exprs(has_chat_columns)
    parts: list[str] = []
    params: list[Any] = []

    if with_range:
        hours = RANGE_HOURS[filters.range_key]
        if hours is not None:
            parts.append("ts >= ?")
            params.append(cutoff_iso(hours, now))

    for key in FILTER_KEYS:
        if key == exclude:
            continue
        value = filters.get(key)
        if not value:
            continue
        # The "empty" facet value of every dimension matches the honest
        # empty representations alike: NULL, '' and the literal placeholder
        # itself (a row that literally recorded 'unknown' / 'unattributed'
        # is the same Unattributed as a row that recorded nothing).
        if key == "harness":
            if value == UNATTRIBUTED:
                parts.append("(caller IS NULL OR caller = '' OR caller = 'unattributed')")
            else:
                parts.append(f"({_EXPR_CALLER} = ?)")
                params.append(value)
        elif key == "provider":
            if value == UNKNOWN:
                parts.append("(upstream IS NULL OR upstream = '' OR upstream = 'unknown')")
            else:
                parts.append(f"({_EXPR_UPSTREAM} = ?)")
                params.append(value)
        elif key == "model":
            if value == UNKNOWN:
                parts.append("(model IS NULL OR model = '' OR model = 'unknown')")
            else:
                parts.append(f"({_EXPR_MODEL} = ?)")
                params.append(value)
        elif key == "route":
            if value == UNKNOWN:
                parts.append("(path IS NULL OR path = '' OR path = 'unknown')")
            else:
                parts.append(f"({_EXPR_PATH} = ?)")
                params.append(value)
        elif key == "outcome":
            if value == UNKNOWN:
                parts.append("(outcome IS NULL OR outcome = '' OR outcome = 'unknown')")
            else:
                parts.append(f"({_EXPR_OUTCOME} = ?)")
                params.append(value)
        elif key == "type":
            if not has_chat_columns:
                if value != UNKNOWN:
                    parts.append("0")  # a ledger without chat columns has no typed rows
                continue
            parts.append(f"({type_expr} = ?)")
            params.append(UNKNOWN if value == UNKNOWN else value)
        elif key == "chat":
            if not has_chat_columns:
                if value != UNKNOWN:
                    parts.append("0")
                continue
            if value == UNKNOWN:
                # no id AND no real type: NULL, '' and literal 'unknown' alike
                parts.append(f"({id_expr} IS NULL AND {type_expr} = 'unknown')")
            elif filters.chat_id_part is None:
                parts.append(f"({id_expr} IS NULL AND {type_expr} = ?)")
                params.append(filters.chat_type_part)
            else:
                parts.append(f"({id_expr} = ? AND {type_expr} = ?)")
                params.extend((filters.chat_id_part, filters.chat_type_part))
    return (" AND ".join(parts), tuple(params))


def _where_sql(where: str) -> str:
    return f" WHERE {where}" if where else ""


# --------------------------------------------------------------------------
# Database access (read-only, short-lived connections)
# --------------------------------------------------------------------------

def open_db_readonly(path: str) -> sqlite3.Connection:
    """Open SQLite in read-only URI mode and fail fast on lock contention."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.2)
    conn.execute("PRAGMA busy_timeout = 200")
    return conn


def ledger_columns(conn: sqlite3.Connection) -> set[str]:
    """Column names of ``usage_events`` (read-only schema probe)."""
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(usage_events)")}
    except sqlite3.Error:
        return set()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def cutoff_iso(hours: float, now: Optional[datetime] = None) -> str:
    """UTC ISO cutoff in the same format the proxy writes into ``ts``.

    ``now`` lets one snapshot share a single instant across its window
    totals, bucket skeleton and facet queries (``fetch_snapshot``), so the
    WHERE cutoff and the bucket plan can never straddle an hour boundary.
    """
    return ((now or utc_now()) - timedelta(hours=hours)).isoformat(timespec="milliseconds")


def to_sydney_datetime(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(SYDNEY)
    except (TypeError, ValueError):
        return None


def to_sydney(ts: str | None) -> str:
    local = to_sydney_datetime(ts)
    return local.strftime("%b %d %H:%M:%S") if local else "—"


def caller_label(caller: Any) -> str:
    """Traffic with no recorded caller cannot be attributed to a harness."""
    return UNATTRIBUTED if not caller else str(caller)


def is_hermes_profile(caller: Any) -> bool:
    """True for the gateway's per-profile callers (`hermes:<profile>`)."""
    return (
        isinstance(caller, str)
        and caller.startswith(HERMES_PROFILE_PREFIX)
        and len(caller) > len(HERMES_PROFILE_PREFIX)
    )


def caller_display(caller: Any) -> str:
    """Human-facing name for a raw caller value — display text only.

    Everything else (JSON payloads, element keys/values/classes, the colour
    hash) keeps the raw string; only the rendered label is prettified.
    Mirrored exactly in the browser JS (callerDisplay).
    """
    if not caller:
        return UNATTRIBUTED
    name = str(caller)
    if name == HERMES_CALLER:
        return HERMES_DISPLAY
    if is_hermes_profile(name):
        return f"{HERMES_DISPLAY} · {name[len(HERMES_PROFILE_PREFIX):]}"
    return name


def chat_label_fields(
    chat_type: Any, chat_id: Any, chat_name: Any
) -> tuple[str, str, str, str]:
    """``(key, type, display, id)`` for one chat identity.

    * key — the filter/URL identity: ``<type>:<id>``, the bare type when the
      surface has no per-chat id (cli, cronjob), ``unknown`` when the row
      carries no identity at all;
    * type — the surface ('unknown' when absent);
    * display — the name when recorded, else the id, else the type, else
      'Unknown': never an inference, only a choice among recorded fields;
    * id — the raw id ('' when absent).
    """
    type_part = str(chat_type) if chat_type else UNKNOWN
    id_part = str(chat_id) if chat_id else ""
    name_part = str(chat_name) if chat_name else ""
    key = f"{type_part}:{id_part}" if id_part else (type_part if type_part != UNKNOWN else UNKNOWN)
    display = name_part or id_part or (type_part if type_part != UNKNOWN else "Unknown")
    return key, type_part, display, id_part


def harness_color_idx(name: str) -> int:
    """djb2 — mirrored exactly in the browser JS so colours agree."""
    value = 5381
    for ch in name:
        value = (value * 33 + ord(ch)) % 2147483647
    return value % HARNESS_COLOR_COUNT


def harness_key(caller: Any) -> str | None:
    """Longest-prefix, case-insensitive identity match on the caller string —
    mirrored exactly in the browser JS (harnessKey) so colours agree."""
    if not caller or caller == UNATTRIBUTED:
        return None
    name = str(caller).lower()
    for prefix, key in HARNESS_PREFIXES:
        if name.startswith(prefix):
            return key
    return None


def harness_class_name(name: str | None) -> str:
    """Identity class first (``.hb-*``, from HARNESS_BRANDS), then the hashed
    rank palette — unknown callers keep the exact h0…h5 behaviour and
    ``unattributed`` stays neutral."""
    key = harness_key(name)
    if key:
        return "hb-" + key
    if not name or name == UNATTRIBUTED:
        return "h-unattr"
    return "h" + str(harness_color_idx(name))


def provider_key(model: Any) -> str | None:
    """Longest-prefix, case-insensitive provider match on the model string —
    mirrored exactly in the browser JS (providerKey) so brands agree."""
    if not model:
        return None
    name = str(model).lower()
    exact = PROVIDER_EXACT.get(name)
    if exact:
        return exact
    for prefix, key in PROVIDER_PREFIXES:
        if name.startswith(prefix):
            return key
    return None


BRAND_SHADE_STEPS = (
    0.62, 0.40, 0.52, 0.34, 0.58, 0.28,  # the reviewed six-repeat ring ramp
    0.24, 0.19, 0.15, 0.11, 0.08, 0.05,  # overflow: keep dimming, never repeat
)
BRAND_SHADE_TAIL_RATIO = 0.75  # geometric dim past the table: strictly decreasing


def brand_shade(color: str, step: int) -> str:
    """Brand colour dimmed toward the card surface — the second and later
    models of one provider in the same ring or column, so same-brand
    neighbours stay told apart while still reading as one brand.  Stays a
    plain hex so the canvas partial-dim pass (hexToRgba) keeps working.
    The factors never repeat and never clamp (the hourly chart can stack
    more same-provider models than the six-slot donut): past the table the
    factor keeps shrinking geometrically (×0.75 per repeat), staying
    strictly darker until 8-bit hex saturation.  Mirrored exactly in the
    browser JS (brandShade)."""
    if step <= 0:
        return color
    i = step - 1
    if i < len(BRAND_SHADE_STEPS):
        t = BRAND_SHADE_STEPS[i]
    else:
        t = BRAND_SHADE_STEPS[-1] * BRAND_SHADE_TAIL_RATIO ** (i - len(BRAND_SHADE_STEPS) + 1)
    channels = []
    for j in (1, 3, 5):
        c = int(color[j:j + 2], 16)
        s = int(CARD_SURFACE[j:j + 2], 16)
        channels.append(round(c * t + s * (1 - t)))
    return "#{:02x}{:02x}{:02x}".format(*channels)


def brand_step_map(models: list[str]) -> dict[str, int]:
    """Shade step for each model: its index among its provider's models,
    sorted by name — a function of the model's own identity alone, so the
    shade is stable wherever the model appears.  The donut builds this over
    its own slice list, the browser's hourly chart over its whole visible
    window (brandShadeSteps/chartShadeMap in the page JS, mirrored exactly):
    one rule on both sides, so the two views agree whenever they show the
    same model set.  (Counting repeats in ring order instead would not —
    ring order is tokens-desc, not name order.)"""
    by_provider: dict[str, list[str]] = {}
    for model in models:
        key = provider_key(model)
        if key:
            by_provider.setdefault(key, []).append(model)
    steps: dict[str, int] = {}
    for names in by_provider.values():
        for i, name in enumerate(sorted(names)):
            steps[name] = i
    return steps


# Single grouped-totals shape shared by every facet: one fixed statement per
# expression, only bound parameters varying.
def query_facet(
    conn: sqlite3.Connection,
    expr: str,
    where: str,
    params: tuple[Any, ...],
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """``[{value, requests, tokens}]`` for one allowlisted expression."""
    sql = (
        f"SELECT {expr} AS value, COUNT(*) AS requests,"
        f" COALESCE(SUM(total_tokens), 0) AS tokens"
        f" FROM usage_events{_where_sql(where)}"
        f" GROUP BY {expr} ORDER BY tokens DESC, requests DESC, value ASC LIMIT ?"
    )
    rows = conn.execute(sql, (*params, limit + 1)).fetchall()
    truncated = len(rows) > limit
    values = [
        {"value": r[0], "requests": r[1] or 0, "tokens": r[2] or 0}
        for r in rows[:limit]
    ]
    return values, truncated


def query_window(
    conn: sqlite3.Connection, where: str, params: tuple[Any, ...]
) -> dict[str, Any]:
    """Request/token totals (input, output, cached) over a filtered window."""
    sql = (
        "SELECT COUNT(*) AS requests,"
        " COALESCE(SUM(total_tokens), 0) AS tokens,"
        " COALESCE(SUM(prompt_tokens), 0) AS input_tokens,"
        " COALESCE(SUM(completion_tokens), 0) AS output_tokens,"
        " COALESCE(SUM(cached_tokens), 0) AS cached_tokens,"
        " MIN(ts) AS first_ts"
        f" FROM usage_events{_where_sql(where)}"
    )
    requests, tokens, input_t, output_t, cached_t, first_ts = conn.execute(
        sql, params
    ).fetchone()
    return {
        "requests": requests or 0,
        "tokens": tokens or 0,
        "input_tokens": input_t or 0,
        "output_tokens": output_t or 0,
        "cached_tokens": cached_t or 0,
        "first_ts": first_ts,
    }


def query_chat_breakdown(
    conn: sqlite3.Connection,
    type_expr: str,
    id_expr: str,
    name_expr: str,
    where: str,
    params: tuple[Any, ...],
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Per-chat rows over the (already cross-filtered) window.

    Groups by the chat identity alone — never by model or hour — so the
    table's numbers are the full filtered ledger's, and requests/models can
    be drilled into afterwards by setting the chat filter.
    """
    key_expr = chat_key_expr(type_expr, id_expr)
    sql = (
        f"SELECT {key_expr} AS chat_key, {type_expr} AS chat_type,"
        f" {id_expr} AS chat_id,"
        f" COALESCE(MAX(NULLIF({name_expr}, '')), '') AS chat_name,"
        " COUNT(*) AS requests,"
        " COALESCE(SUM(prompt_tokens), 0) AS input_tokens,"
        " COALESCE(SUM(completion_tokens), 0) AS output_tokens,"
        " COALESCE(SUM(cached_tokens), 0) AS cached_tokens,"
        " COALESCE(SUM(total_tokens), 0) AS total_tokens"
        f" FROM usage_events{_where_sql(where)}"
        f" GROUP BY {type_expr}, {id_expr}"
        " ORDER BY total_tokens DESC, requests DESC, chat_key ASC LIMIT ?"
    )
    rows = conn.execute(sql, (*params, limit + 1)).fetchall()
    truncated = len(rows) > limit
    chats = []
    for key, chat_type, chat_id, chat_name, requests, in_t, out_t, cached_t, total_t in rows[:limit]:
        _key, type_part, display, _id = chat_label_fields(chat_type, chat_id, chat_name)
        chats.append(
            {
                "key": key,
                "type": type_part,
                "display": display,
                "id": chat_id or "",
                "requests": requests or 0,
                "input_tokens": in_t or 0,
                "output_tokens": out_t or 0,
                "cached_tokens": cached_t or 0,
                "total_tokens": total_t or 0,
            }
        )
    return chats, truncated


def by_model_rows(facet_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Donut rows from the model facet's ``{value, requests, tokens}`` rows.

    Events with no recorded model cannot be attributed, so they collapse into
    one ``unknown`` slice rather than vanishing from the total.
    """
    return [
        {
            "model": "unknown" if not r["value"] or r["value"] == "(null)" else r["value"],
            "tokens": int(r["tokens"] or 0),
            "requests": int(r["requests"] or 0),
        }
        for r in facet_rows
    ]


def caller_rows_from(facet_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-harness bar rows from the harness facet's rows."""
    return [
        {
            "caller": r["value"] or UNATTRIBUTED,
            "requests": int(r["requests"] or 0),
            "total_tokens": int(r["tokens"] or 0),
            "unattributed": not r["value"] or r["value"] == UNATTRIBUTED,
        }
        for r in facet_rows
    ]


# ── time buckets ─────────────────────────────────────────────────────────────
#
# The chart window follows the range filter: hourly buckets for 24 h, daily
# buckets for 7 d / 30 d, and for "all" a span from the first filtered event
# to now folded into at most BUCKET_MAX columns (widening the bucket width
# instead of dropping columns).  Bucket instants are UTC; labels are Sydney.

BUCKET_MAX = 60
CHAT_SERIES_TOP = 12  # per-bucket chat segments kept before the "other" fold


def _bucket(start: datetime, label: str, day: str | None, partial: bool) -> dict[str, Any]:
    return {
        "start": start,
        "hour_bucket": start.isoformat(),  # wire shape kept for the page JS
        "label_sydney": label,
        "day_sydney": day,
        "requests": 0,
        "tokens": 0,
        # input/output/cached bucket totals behind the chart's in/out and
        # cache breakdowns; the series entries carry the same splits per
        # (harness, model) group (query_timeseries)
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        # per-(harness, model) token groups behind the bucket total — the
        # stacked segments of the chart column (query_timeseries)
        "series": [],
        # per-chat token groups behind the chat/type chart modes
        "chat_series": [],
        # The first bucket is truncated by the rolling cutoff and the last
        # is still in progress — both drawn at half strength.
        "partial": partial,
    }


def bucket_plan(
    range_key: str, first_ts: str | None, now: Optional[datetime] = None
) -> list[dict[str, Any]]:
    """Empty bucket skeletons covering the full filtered window, oldest first.

    Rolling ranges start at the bucket *containing* the WHERE cutoff — the
    first column is the partial hour/day the cutoff falls inside — and end
    with the current, still-in-progress bucket, so the chart accounts for
    exactly the same events as the stat cards and breakdowns (which filter
    ``ts >= now - <range>``).  ``now`` is the snapshot's shared instant when
    called from ``fetch_snapshot`` (see ``cutoff_iso``).
    """
    now = now or utc_now()
    if range_key == "24h":
        current = now.replace(minute=0, second=0, microsecond=0)
        buckets = []
        for i in range(HOURS, -1, -1):  # the cutoff's partial hour … now
            start = current - timedelta(hours=i)
            local = start.astimezone(SYDNEY)
            buckets.append(
                _bucket(
                    start,
                    local.strftime("%H:%M"),
                    local.strftime("%a") if local.hour == 0 else None,
                    i in (0, HOURS),
                )
            )
        return buckets
    if range_key in ("7d", "30d"):
        days = DAYS_7D if range_key == "7d" else 30
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return [
            _bucket(
                today - timedelta(days=i),
                (today - timedelta(days=i)).astimezone(SYDNEY).strftime("%b %d"),
                None,
                i in (0, days),  # the cutoff's partial day … today
            )
            for i in range(days, -1, -1)
        ]
    # "all": from the first filtered event to now, ≤ BUCKET_MAX columns
    first = to_sydney_datetime(first_ts) if first_ts else None
    first_day = (
        first.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        if first
        else now.replace(hour=0, minute=0, second=0, microsecond=0)
    )
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    span_days = max(1, (today - first_day).days + 1)
    width = max(1, -(-span_days // BUCKET_MAX))  # ceil
    buckets = []
    start = first_day
    while start <= today:
        local = start.astimezone(SYDNEY)
        label = local.strftime("%b %d") if width == 1 else local.strftime("%b %d") + "+"
        buckets.append(_bucket(start, label, None, start + timedelta(days=width) > today))
        start += timedelta(days=width)
    return buckets


def hour_buckets() -> list[dict[str, Any]]:
    """24 empty hourly buckets (UTC) ending with the current, just-started hour."""
    return bucket_plan("24h", None)


def query_timeseries(
    conn: sqlite3.Connection,
    where: str,
    params: tuple[Any, ...],
    buckets: list[dict[str, Any]],
    has_chat_columns: bool,
) -> list[dict[str, Any]]:
    """Fill the bucket skeleton from the filtered ledger (this is
    /api/timeseries).

    Two fixed statements, both filtered by the shared WHERE: the main series
    grouped by bucket × caller × model (carrying the input/output/cached
    splits so the chart can re-stack by those dimensions), and the chat
    series grouped by bucket × chat identity (tokens only) behind the
    chat/type breakdown modes.  Rows arrive pre-aggregated in SQL over the
    full filtered window — never capped by an event LIMIT.
    """
    if not buckets:
        return buckets
    starts = [b["start"] for b in buckets]
    # Hourly grouping only when the plan itself is hourly (the 24 h range);
    # everything else groups by UTC day.  Placement below is arithmetic over
    # the skeleton's own starts, so a one-bucket "all" window or a widened
    # all-time span can never be misread as hourly by a span guess.
    width = starts[1] - starts[0] if len(starts) > 1 else None
    hourly = width is not None and width < timedelta(days=1)
    key_len = 13 if hourly else 10
    type_expr, id_expr, name_expr = chat_exprs(has_chat_columns)

    rows = conn.execute(
        f"SELECT substr(ts, 1, {key_len}) AS b, {_EXPR_CALLER} AS caller,"
        f" {_EXPR_MODEL} AS model,"
        " COUNT(*) AS requests,"
        " COALESCE(SUM(total_tokens), 0) AS tokens,"
        " COALESCE(SUM(prompt_tokens), 0) AS input_tokens,"
        " COALESCE(SUM(completion_tokens), 0) AS output_tokens,"
        " COALESCE(SUM(cached_tokens), 0) AS cached_tokens"
        f" FROM usage_events{_where_sql(where)}"
        f" GROUP BY b, {_EXPR_CALLER}, {_EXPR_MODEL}",
        params,
    ).fetchall()
    chat_rows = conn.execute(
        f"SELECT substr(ts, 1, {key_len}) AS b, {type_expr} AS chat_type,"
        f" {id_expr} AS chat_id,"
        f" COALESCE(MAX(NULLIF({name_expr}, '')), '') AS chat_name,"
        " COALESCE(SUM(total_tokens), 0) AS tokens"
        f" FROM usage_events{_where_sql(where)}"
        f" GROUP BY b, {type_expr}, {id_expr}",
        params,
    ).fetchall()

    # bucket lookup: the raw substr key of a row -> its bucket.  A row's key
    # is its hour/day truncated to text, i.e. the *start* of its hour/day, so
    # placement is one bisect over the skeleton's starts — uniform for hourly,
    # daily and widened all-time buckets alike, and correct for the shared
    # first partial bucket (a row at the cutoff keys exactly to its start).
    def bucket_for(raw_key: str) -> dict[str, Any] | None:
        try:
            ts = datetime.fromisoformat(raw_key).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        i = bisect_right(starts, ts) - 1
        return buckets[i] if i >= 0 else None

    groups: dict[int, dict[tuple[str, str], dict[str, int]]] = {}
    for raw_key, caller, model, requests, tokens, input_t, output_t, cached_t in rows:
        bucket = bucket_for(raw_key)
        if bucket is None:
            continue
        pair = (caller or UNATTRIBUTED, model or "unknown")
        agg = groups.setdefault(id(bucket), {}).setdefault(
            pair, {"tokens": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
        )
        agg["tokens"] += tokens or 0
        agg["input_tokens"] += input_t or 0
        agg["output_tokens"] += output_t or 0
        agg["cached_tokens"] += cached_t or 0
        bucket["requests"] += requests or 0

    chat_groups: dict[int, dict[str, dict[str, Any]]] = {}
    for raw_key, chat_type, chat_id, chat_name, tokens in chat_rows:
        bucket = bucket_for(raw_key)
        if bucket is None:
            continue
        key, type_part, display, _id = chat_label_fields(chat_type, chat_id, chat_name)
        agg = chat_groups.setdefault(id(bucket), {}).setdefault(
            key, {"key": key, "type": type_part, "display": display, "tokens": 0}
        )
        if not agg["display"] or agg["display"] == type_part:
            agg["display"] = display  # a later row may carry the name
        agg["tokens"] += tokens or 0

    for bucket in buckets:
        series = [
            {"caller": caller, "model": model, **agg}
            for (caller, model), agg in groups.get(id(bucket), {}).items()
            if agg["tokens"] > 0
        ]
        series.sort(key=lambda s: (-s["tokens"], s["caller"], s["model"]))
        bucket["tokens"] = sum(s["tokens"] for s in series)
        bucket["input_tokens"] = sum(s["input_tokens"] for s in series)
        bucket["output_tokens"] = sum(s["output_tokens"] for s in series)
        bucket["cached_tokens"] = sum(s["cached_tokens"] for s in series)
        bucket["series"] = series

        chats = sorted(
            chat_groups.get(id(bucket), {}).values(),
            key=lambda c: (-c["tokens"], c["key"]),
        )
        chats = [c for c in chats if c["tokens"] > 0]
        if len(chats) > CHAT_SERIES_TOP:
            rest = chats[CHAT_SERIES_TOP:]
            chats = chats[:CHAT_SERIES_TOP]
            chats.append(
                {
                    "key": "other",
                    "type": "other",
                    "display": "other",
                    "tokens": sum(c["tokens"] for c in rest),
                }
            )
        bucket["chat_series"] = chats
    # strip the datetime helper before JSON serialization
    for bucket in buckets:
        bucket.pop("start", None)
    return buckets


def _event_row(r: tuple[Any, ...]) -> dict[str, Any]:
    key, type_part, display, id_part = chat_label_fields(r[16], r[17], r[18])
    return {
        "id": r[0],
        "ts": r[1],
        "ts_sydney": to_sydney(r[1]),
        "upstream": r[2],
        "model": r[3],
        "path": r[4],
        "route": r[4],
        "status_code": r[5],
        "latency_ms": r[6],
        "prompt_tokens": r[7],
        "completion_tokens": r[8],
        "cached_tokens": r[9],
        "reasoning_tokens": r[10],
        "cache_creation_tokens": r[11],
        "total_tokens": r[12],
        "outcome": r[13],
        "usage_complete": r[14],
        "caller": caller_label(r[15]),
        "unattributed": not r[15],
        "chat_key": key,
        "chat_type": type_part,
        "chat_display": display,
        "chat_id": id_part,
    }


def query_events(
    conn: sqlite3.Connection,
    where: str,
    params: tuple[Any, ...],
    has_chat_columns: bool,
    limit: int = API_EVENTS_DEFAULT,
) -> list[dict[str, Any]]:
    """Newest events of the *filtered* ledger — the WHERE is applied before
    the LIMIT, so a narrowed view shows the newest matching rows, not a
    filtered slice of the newest N overall."""
    type_expr, id_expr, name_expr = chat_exprs(has_chat_columns)
    sql = (
        "SELECT id, ts, upstream, model, path, status_code, latency_ms,"
        " prompt_tokens, completion_tokens, cached_tokens, reasoning_tokens,"
        " cache_creation_tokens, total_tokens, outcome, usage_complete, caller,"
        f" {type_expr}, {id_expr}, {name_expr}"
        f" FROM usage_events{_where_sql(where)}"
        " ORDER BY ts DESC, id DESC LIMIT ?"
    )
    rows = conn.execute(sql, (*params, limit)).fetchall()
    return [_event_row(r) for r in rows]


def badge_parts(e: dict[str, Any]) -> tuple[str, str]:
    """Outcome badge: final = green, 401/429 = red, everything else grey."""
    status = e.get("status_code")
    if status in RATE_LIMIT_CODES:
        return TONE_CRIT, str(status)
    if e.get("usage_complete") == "final":
        return TONE_GOOD, "final"
    label = e.get("outcome")
    if not label:
        label = str(status) if status is not None else "—"
    return TONE_NONE, label


def empty_window() -> dict[str, Any]:
    return {"requests": 0, "tokens": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}


def _filters_payload(filters: Filters) -> dict[str, Any]:
    return {
        "values": dict(filters.items()),
        "query": filters.query(),
        "active_count": filters.active_count(),
    }


def error_snapshot(message: str, filters: Optional[Filters] = None) -> dict[str, Any]:
    """A payload shaped like a real snapshot, but flagged as failed."""
    filters = filters or Filters()
    buckets = bucket_plan(filters.range_key, None)
    for bucket in buckets:
        bucket.pop("start", None)  # not JSON-serializable; query_timeseries strips it too
    return {
        "error": message,
        "generated_at_utc": utc_now().isoformat(timespec="seconds"),
        "generated_at_sydney": to_sydney(utc_now().isoformat(timespec="seconds")),
        "range": {"key": filters.range_key, "label": RANGE_LABELS[filters.range_key]},
        "filters": _filters_payload(filters),
        "has_chat": False,
        "window": empty_window(),
        "facets": {key: {"options": [], "truncated": False} for key in FILTER_KEYS},
        "by_model": [],
        "per_caller": [],
        "chats": {"rows": [], "truncated": False},
        "per_hour": buckets,
        "events": [],
    }


def fetch_snapshot(
    db_path: str,
    filters: Optional[Filters] = None,
    event_limit: int = API_EVENTS_DEFAULT,
) -> dict[str, Any]:
    """Everything one dashboard refresh needs under the shared filter state,
    or a soft-error snapshot.

    Every number is an aggregate over the full *filtered* ledger computed in
    SQL: the stat cards and breakdowns apply all filters (range included),
    each facet's option list is cross-filtered (all filters except its own)
    so it stays usable when narrowed, and events are filtered before their
    LIMIT.  ``event_limit=0`` skips the events query entirely (used by
    /api/summary).

    All queries run inside one read transaction over one shared ``now``, so
    the cards, the chart buckets and the breakdowns always describe the same
    instant and the same ledger contents even while the proxy keeps writing
    (a deferred read transaction is snapshot-consistent under WAL and never
    blocks the writer longer than its own commit).
    """
    filters = filters or Filters()
    try:
        conn = open_db_readonly(db_path)
    except (sqlite3.Error, OSError) as exc:
        return error_snapshot(f"cannot open ledger read-only: {exc}", filters)

    try:
        now = utc_now()
        conn.execute("BEGIN")  # one consistent read snapshot for every query below
        has_chat = CHAT_COLUMNS <= ledger_columns(conn)
        type_expr, id_expr, name_expr = chat_exprs(has_chat)
        where, params = where_clause(filters, has_chat, now=now)

        window = query_window(conn, where, params)
        buckets = bucket_plan(filters.range_key, window["first_ts"], now=now)
        per_bucket = query_timeseries(conn, where, params, buckets, has_chat)
        events = (
            query_events(conn, where, params, has_chat, event_limit)
            if event_limit > 0
            else []
        )

        # Breakdowns apply ALL filters (the filtered ledger's own shape).
        caller_rows = caller_rows_from(
            query_facet(conn, _EXPR_CALLER, where, params, FACET_LIMITS["harness"])[0]
        )
        by_model = by_model_rows(
            query_facet(conn, _EXPR_MODEL, where, params, FACET_LIMITS["model"])[0]
        )
        chat_rows, chats_truncated = query_chat_breakdown(
            conn, type_expr, id_expr, name_expr, where, params, FACET_LIMITS["chat"]
        )

        # Facet option lists are cross-filtered: every filter except the
        # facet's own, so a narrowed view still offers meaningful choices.
        facet_exprs = {
            "harness": _EXPR_CALLER,
            "provider": _EXPR_UPSTREAM,
            "model": _EXPR_MODEL,
            "route": _EXPR_PATH,
            "outcome": _EXPR_OUTCOME,
        }
        facets: dict[str, Any] = {}
        for key in FILTER_KEYS:
            fwhere, fparams = where_clause(filters, has_chat, exclude=key, now=now)
            if key == "chat":
                options, truncated = query_chat_breakdown(
                    conn, type_expr, id_expr, name_expr,
                    fwhere, fparams, FACET_LIMITS["chat"],
                )
            elif key == "type":
                options, truncated = query_facet(
                    conn, type_expr, fwhere, fparams, FACET_LIMITS["type"]
                )
            else:
                options, truncated = query_facet(
                    conn, facet_exprs[key], fwhere, fparams, FACET_LIMITS[key]
                )
            facets[key] = {"options": options, "truncated": truncated}
        conn.commit()  # read-only: ends the snapshot; nothing was written
    except (sqlite3.Error, OSError) as exc:
        return error_snapshot(f"ledger query failed: {exc}", filters)
    finally:
        conn.close()

    return {
        "error": None,
        "generated_at_utc": utc_now().isoformat(timespec="seconds"),
        "generated_at_sydney": to_sydney(utc_now().isoformat(timespec="seconds")),
        "range": {"key": filters.range_key, "label": RANGE_LABELS[filters.range_key]},
        "filters": _filters_payload(filters),
        "has_chat": has_chat,
        "window": {
            "requests": window["requests"],
            "tokens": window["tokens"],
            "input_tokens": window["input_tokens"],
            "output_tokens": window["output_tokens"],
            "cached_tokens": window["cached_tokens"],
        },
        "facets": facets,
        "by_model": by_model,
        "per_caller": caller_rows,
        "chats": {"rows": chat_rows, "truncated": chats_truncated},
        "per_hour": per_bucket,
        "events": events,
    }


# --------------------------------------------------------------------------
# Formatting helpers (server-side mirrors of the client JS)
# --------------------------------------------------------------------------

def fmt_int(value: Any) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}"


def fmt_compact(value: Any) -> str:
    n = float(value or 0)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if abs(n) >= divisor:
            return f"{n / divisor:.1f}".rstrip("0").rstrip(".") + suffix
    return str(int(n))


def fmt_stat(value: Any) -> str:
    n = int(value or 0)
    return f"{n:,}" if abs(n) < 10_000 else fmt_compact(n)


def fmt_opt(value: Any) -> str:
    """Table numeral; unknown totals (usage missing) read as a dash, not 0."""
    return "—" if value is None else fmt_stat(value)


def fmt_avg(tokens: Any, requests: Any) -> str:
    if not requests:
        return "no requests"
    return f"avg {fmt_compact(float(tokens) / requests)} / request"


def fmt_cached_hint(cached: Any) -> str:
    return f"incl. {fmt_compact(cached)} cached" if cached else "prompt tokens"


def bucket_label(bucket: dict[str, Any]) -> str:
    day = bucket.get("day_sydney")
    return (day + " " if day else "") + (bucket.get("label_sydney") or "")


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else "—"))


# --------------------------------------------------------------------------
# HTML fragments
# --------------------------------------------------------------------------

def stat_card(
    value_id: str, hint_id: str, label: str, value: Any, hint: str, label_id: str = ""
) -> str:
    lid = f' id="{label_id}"' if label_id else ""
    return (
        '<div class="card stat">'
        f'<div class="label"{lid}>{esc(label)}</div>'
        f'<div class="value" id="{value_id}">{esc(fmt_stat(value))}</div>'
        f'<div class="hint" id="{hint_id}">{esc(hint)}</div>'
        "</div>"
    )


def render_cards(snapshot: dict[str, Any]) -> str:
    window = snapshot.get("window") or {}
    req = window.get("requests") or 0
    range_label = (snapshot.get("range") or {}).get("label") or "last 24 h"
    return "".join(
        [
            stat_card("c-req-24h", "h-req-24h", f"Requests · {range_label}", window.get("requests"), "filtered window", "l-req-24h"),
            stat_card("c-tok-24h", "h-tok-24h", f"Total tokens · {range_label}", window.get("tokens"), fmt_avg(window.get("tokens"), req), "l-tok-24h"),
            stat_card("c-in-24h", "h-in-24h", f"Input tokens · {range_label}", window.get("input_tokens"), fmt_cached_hint(window.get("cached_tokens")), "l-in-24h"),
            stat_card("c-out-24h", "h-out-24h", f"Output tokens · {range_label}", window.get("output_tokens"), fmt_avg(window.get("output_tokens"), req), "l-out-24h"),
        ]
    )


# ── filter bar ───────────────────────────────────────────────────────────────

# (filter key, select id, all-option label) in filter-bar order; range and
# chat are rendered specially (preset list, searchable picker).
_FACET_SELECTS = (
    ("harness", "f-harness", "All harnesses"),
    ("provider", "f-provider", "All providers"),
    ("model", "f-model", "All models"),
    ("type", "f-type", "All chat types"),
    ("route", "f-route", "All routes"),
    ("outcome", "f-outcome", "All outcomes"),
)


def _facet_option(value: str, requests: Any, tokens: Any, selected: bool, label: str = "") -> str:
    sel = " selected" if selected else ""
    count = f"{fmt_compact(tokens or 0)} tok · {fmt_int(requests or 0)} req"
    return f'<option value="{esc(value)}"{sel}>{esc(label or value)} · {esc(count)}</option>'


def render_filter_bar(snapshot: dict[str, Any]) -> str:
    """The shared filter state as controls: range preset, one select per
    exact-match facet (options carry their cross-filtered counts), a
    searchable chat picker, and one removable chip per active filter."""
    filters = (snapshot.get("filters") or {}).get("values") or {}
    facets = snapshot.get("facets") or {}
    range_key = (snapshot.get("range") or {}).get("key") or "24h"

    range_options = "".join(
        f'<option value="{key}"{" selected" if key == range_key else ""}>'
        f"{esc(label)}</option>"
        for key, label in RANGE_LABELS.items()
    )

    selects = []
    for key, select_id, all_label in _FACET_SELECTS:
        selected_value = filters.get(key) or ""
        options = [f'<option value="">{esc(all_label)}</option>']
        seen = set()
        for opt in (facets.get(key) or {}).get("options") or []:
            value = str(opt.get("value") or "")
            seen.add(value)
            options.append(
                _facet_option(
                    value, opt.get("requests"), opt.get("tokens"),
                    value == selected_value,
                    # harness options label the Hermes family "Hermes IDE";
                    # the option VALUE stays the raw caller (the filter key)
                    caller_display(value) if key == "harness" else "",
                )
            )
        if selected_value and selected_value not in seen:
            # the active filter narrowed itself out of the cross-filtered
            # list — keep it selectable anyway (never silently dropped)
            sel_label = caller_display(selected_value) if key == "harness" else selected_value
            options.append(
                f'<option value="{esc(selected_value)}" selected>{esc(sel_label)}</option>'
            )
        selects.append(
            f'<label class="f"><span>{esc(all_label[4:])}</span>'
            f'<select id="{select_id}" data-key="{key}">{"".join(options)}</select></label>'
        )

    # searchable chat picker: the select lists "<display> (<key>)"; the
    # search input filters the options client-side
    selected_chat = filters.get("chat") or ""
    chat_options = ['<option value="">All chats</option>']
    seen_chat = set()
    for opt in (facets.get("chat") or {}).get("options") or []:
        key = str(opt.get("key") or "")
        seen_chat.add(key)
        display = str(opt.get("display") or key)
        count = f"{fmt_compact(opt.get('total_tokens') or 0)} tok · {fmt_int(opt.get('requests') or 0)} req"
        sel = " selected" if key == selected_chat else ""
        chat_options.append(
            f'<option value="{esc(key)}"{sel}>{esc(display)} · {esc(count)}</option>'
        )
    if selected_chat and selected_chat not in seen_chat:
        chat_options.insert(
            1, f'<option value="{esc(selected_chat)}" selected>{esc(selected_chat)}</option>'
        )
    chat_picker = (
        '<label class="f chatpick"><span>Chats</span>'
        '<input type="search" id="f-chat-search" placeholder="search chats&hellip;"'
        ' autocomplete="off" spellcheck="false" aria-label="Search chats">'
        f'<select id="f-chat" data-key="chat">{"".join(chat_options)}</select></label>'
    )

    chips = []
    for key, value in filters.items():
        chips.append(
            f'<button type="button" class="fchip" data-key="{esc(key)}"'
            f' title="Clear the {esc(key)} filter">'
            f'<span class="fk">{esc(key)}</span> {esc(value)}'
            '<span class="fx" aria-hidden="true">✕</span></button>'
        )
    active = len(chips)
    return (
        '<section class="card filters" aria-label="Filters" id="filters">'
        '<div class="card-head"><h2>Filters</h2><div class="card-meta">'
        f'<span class="win" id="filter-count">{"" if active else "no filters active"}'
        f'{active if active else ""}{" active" if active else ""}</span>'
        f'<button type="button" class="fbtn" id="filter-clear"{" hidden" if not active else ""}>clear all</button>'
        '</div></div>'
        '<div class="filter-grid">'
        f'<label class="f"><span>Range</span><select id="f-range">{range_options}</select></label>'
        + "".join(selects[:2])
        + "".join(selects[2:4])
        + chat_picker
        + "".join(selects[4:])
        + '</div><div class="chips" id="filter-chips">'
        + "".join(chips)
        + '</div></section>'
    )


def chat_table_body(chats: dict[str, Any]) -> str:
    rows = (chats or {}).get("rows") or []
    if not rows:
        return '<tbody id="chat-body"><tr><td colspan="6" class="muted">No requests in the filtered window</td></tr></tbody>'
    out = []
    for c in rows:
        key = c.get("key") or UNKNOWN
        display = c.get("display") or "Unknown"
        sub = c.get("id") or c.get("type") or ""
        sub_html = f'<div class="csub">{esc(sub)}</div>' if sub and sub != display else ""
        unknown_cls = " c-unknown" if key == UNKNOWN else ""
        out.append(
            f'<tr class="crow{unknown_cls}" data-chat="{esc(key)}" tabindex="0"'
            f' title="Filter to {esc(display)}">'
            f'<td><div class="cname">{esc(display)}</div>{sub_html}</td>'
            f'<td class="num">{fmt_int(c.get("requests"))}</td>'
            f'<td class="num">{esc(fmt_stat(c.get("input_tokens")))}</td>'
            f'<td class="num">{esc(fmt_stat(c.get("output_tokens")))}</td>'
            f'<td class="num">{esc(fmt_stat(c.get("cached_tokens")))}</td>'
            f'<td class="num total">{esc(fmt_stat(c.get("total_tokens")))}</td>'
            "</tr>"
        )
    truncated = (chats or {}).get("truncated")
    if truncated:
        out.append(
            f'<tr><td colspan="6" class="muted">showing the top {len(rows)} chats by tokens'
            ' — narrow with filters to see the rest</td></tr>'
        )
    return '<tbody id="chat-body">' + "".join(out) + "</tbody>"


def _harness_rank(r: dict[str, Any]) -> tuple[int, int, str]:
    """The per-harness facet's ORDER BY (tokens desc, requests desc, caller
    asc), reused to place the Hermes IDE subtotal among the other callers."""
    return (-(r.get("total_tokens") or 0), -(r.get("requests") or 0), str(r.get("caller")))


def harness_display_rows(rows: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    """Per-harness rows for the table: ``hermes:<profile>`` callers become
    indented subrows under a ``Hermes IDE`` parent whose totals also fold in
    any plain ``hermes`` traffic (the pre-profile-split caller value).

    Each entry is ``(row, kind)`` with kind "" / "subtotal" / "subrow".
    Without profile callers the input rows come back untouched, so a ledger
    that never recorded them renders exactly as before.  Mirrored exactly in
    the browser JS (harnessDisplayRows) — display grouping only: the raw
    rows (and every total elsewhere) are untouched, so the subtotal can
    never double-count.
    """
    profile_rows = [r for r in rows if is_hermes_profile(r.get("caller"))]
    if not profile_rows:
        return [(r, "") for r in rows]
    hermes_rows: list[dict[str, Any]] = []
    other_rows: list[dict[str, Any]] = []
    for r in rows:
        if r.get("caller") == HERMES_CALLER or is_hermes_profile(r.get("caller")):
            hermes_rows.append(r)
        else:
            other_rows.append(r)
    parent = {
        "caller": HERMES_CALLER,
        "unattributed": False,
        "requests": sum(r.get("requests") or 0 for r in hermes_rows),
        "total_tokens": sum(r.get("total_tokens") or 0 for r in hermes_rows),
    }
    out: list[tuple[dict[str, Any], str]] = []
    for r in sorted(other_rows + [parent], key=_harness_rank):
        if r is parent:
            out.append((r, "subtotal"))
            out.extend((p, "subrow") for p in sorted(profile_rows, key=_harness_rank))
        else:
            out.append((r, ""))
    return out


def harness_table_body(rows: list[dict[str, Any]]) -> str:
    """One row per harness: chip, requests, tokens and a share-of-max bar.
    Rows drill down — a click sets the harness filter.

    Hermes profile callers render as indented subrows under a Hermes IDE
    parent subtotal row (see harness_display_rows).  The subtotal is NOT
    clickable: the harness filter matches one exact caller, so no single
    selection would honestly represent the whole Hermes family — its title
    says the scope instead.  Subrows drill to their exact profile caller.
    """
    if not rows:
        return '<tbody id="harness-body"><tr><td colspan="4" class="muted">No requests in the filtered window</td></tr></tbody>'
    display = harness_display_rows(rows)
    peak = max((float(r.get("total_tokens") or 0) for r, _ in display), default=0.0)
    out = []
    for r, kind in display:
        tokens = float(r.get("total_tokens") or 0)
        tr_class = "h-unattr" if r.get("unattributed") else harness_class_name(r.get("caller"))
        if kind:
            tr_class += f" {kind}"
        caller = r.get("caller") or UNATTRIBUTED
        fill = ""
        if peak > 0 and tokens > 0:
            width = max(1.5, tokens / peak * 100)
            fill = f'<div class="bar-fill" style="width:{width:.1f}%"></div>'
        if kind == "subtotal":
            attrs = (
                ' title="Hermes IDE total across every hermes caller (plain'
                " 'hermes' plus all profiles) — the harness filter matches one"
                ' exact caller, so pick a profile below to filter"'
            )
        else:
            attrs = (
                f' data-harness="{esc(caller)}" tabindex="0"'
                f' title="Filter to {esc(caller_display(caller))}"'
            )
            tr_class += " hrow"
        out.append(
            f'<tr class="{tr_class}"{attrs}>'
            f'<td><span class="chip">{esc(caller_display(caller))}</span></td>'
            f'<td class="num">{fmt_int(r.get("requests"))}</td>'
            f'<td class="num">{esc(fmt_stat(tokens))}</td>'
            f'<td class="bar-cell"><div class="bar-track" aria-hidden="true">{fill}</div></td>'
            "</tr>"
        )
    return '<tbody id="harness-body">' + "".join(out) + "</tbody>"


def model_name_html(model: Any) -> str:
    """Provider logo beside the escaped model name — the model twin of the
    harness chip, used by every model-name cell (donut legend, chart table,
    events table).  Unknown providers and missing models stay plain text."""
    key = provider_key(model)
    if not key:
        return esc(model)
    brand = PROVIDER_BRANDS[key]
    glyph = brand.get("glyph", brand["color"])
    return (
        f'<span class="mbrand" title="{esc(brand["name"])}">'
        f'<span class="plogo" style="color:{glyph}">{brand["logo"]}</span>'
        f"{esc(model)}</span>"
    )


def events_table_body(events: list[dict[str, Any]]) -> str:
    if not events:
        return '<tr><td colspan="9" class="muted">No events match the filters</td></tr>'
    out = []
    for e in events:
        tone, label = badge_parts(e)
        row_cls = ' class="row-crit"' if tone == TONE_CRIT else ""
        chip_cls = "h-unattr" if e.get("unattributed") else harness_class_name(e.get("caller"))
        chat_display = e.get("chat_display") or "Unknown"
        chat_cls = "" if e.get("chat_key") and e.get("chat_key") != UNKNOWN else "muted"
        out.append(
            f"<tr{row_cls}>"
            f'<td class="num" title="{esc(e.get("ts"))}">{esc(e.get("ts_sydney"))}</td>'
            f'<td><span class="chip {chip_cls}">{esc(caller_display(e.get("caller")))}</span></td>'
            f'<td class="{chat_cls}" title="{esc(e.get("chat_key"))}">{esc(chat_display)}</td>'
            f"<td>{model_name_html(e.get('model'))}</td>"
            f"<td>{esc(e.get('route'))}</td>"
            f'<td class="num">{esc(fmt_opt(e.get("prompt_tokens")))}</td>'
            f'<td class="num">{esc(fmt_opt(e.get("completion_tokens")))}</td>'
            f'<td class="num total">{esc(fmt_opt(e.get("total_tokens")))}</td>'
            f'<td><span class="badge"><span class="dot-s tone-{tone}"></span><span>{esc(label)}</span></span></td>'
            "</tr>"
        )
    return "".join(out)


def bucket_series_groups(bucket: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per harness: ``{caller, tokens, models: [(model, tokens), …]}``
    with harnesses and models both sorted tokens-desc — the structured text
    twin of the canvas stacking (and of the browser-side ``seriesLines``) for
    the sr-only chart table, where each model name also carries its brand.
    """
    by_caller: dict[str, dict[str, int]] = {}
    for s in bucket.get("series") or []:
        tokens = int(s.get("tokens") or 0)
        if tokens <= 0:
            continue
        models = by_caller.setdefault(s.get("caller") or UNATTRIBUTED, {})
        name = s.get("model") or "unknown"
        models[name] = models.get(name, 0) + tokens
    groups = [
        {
            "caller": caller,
            "tokens": sum(models.values()),
            "models": sorted(models.items(), key=lambda kv: (-kv[1], kv[0])),
        }
        for caller, models in by_caller.items()
    ]
    groups.sort(key=lambda g: (-g["tokens"], g["caller"]))
    return groups


def series_line_html(group: dict[str, Any]) -> str:
    """``claude 12.3k (modelA 8.1k · modelB 4.2k)`` — one branded line.
    The harness name is the display label (Hermes IDE · <profile>); model
    names stay raw beside their brand logos."""
    detail = " · ".join(
        f"{model_name_html(m)} {esc(fmt_compact(t))}" for m, t in group["models"]
    )
    return f"{esc(caller_display(group['caller']))} {esc(fmt_compact(group['tokens']))} ({detail})"


def chart_data_table(buckets: list[dict[str, Any]]) -> str:
    """Screen-reader table twin of the stacked canvas chart."""
    rows = []
    for b in buckets:
        cell = "".join(f"<div>{series_line_html(g)}</div>" for g in bucket_series_groups(b)) or "—"
        rows.append(
            "<tr>"
            f"<td>{esc(bucket_label(b))}</td>"
            f'<td class="num">{fmt_int(b.get("requests"))}</td>'
            f'<td class="num">{fmt_int(b.get("tokens"))}</td>'
            f"<td>{cell}</td>"
            "</tr>"
        )
    return (
        '<table class="sr-only"><caption>Tokens per time bucket (Australia/Sydney)</caption>'
        '<thead><tr><th scope="col">Hour</th><th scope="col">Requests</th><th scope="col">Tokens</th>'
        '<th scope="col" id="chart-table-series-head">Per-harness tokens (per model)</th></tr></thead>'
        f'<tbody id="chart-table-body">{"".join(rows)}</tbody></table>'
    )


def donut_slices(by_model: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Donut slices: top MODEL_TOP_N models by tokens, remainder as "other".

    Rows without token accounting (total_tokens NULL) draw no slice, so an
    all-zero window degrades to the muted empty state instead of a dead ring.
    """
    rows = [r for r in (by_model or []) if (r.get("tokens") or 0) > 0]
    rows.sort(key=lambda r: (-r["tokens"], r["model"]))
    slices = [{"model": r["model"], "tokens": r["tokens"], "requests": r["requests"]} for r in rows[:MODEL_TOP_N]]
    if len(rows) > MODEL_TOP_N:
        rest = rows[MODEL_TOP_N:]
        slices.append(
            {
                "model": "other",
                "tokens": sum(r["tokens"] for r in rest),
                "requests": sum(r["requests"] for r in rest),
            }
        )
    return slices


def slice_color(index: int) -> str:
    return MODEL_COLORS[index] if index < len(MODEL_COLORS) else MODEL_OTHER_COLOR


def slice_fill(slices: list[dict[str, Any]], index: int, steps: dict[str, int]) -> str:
    """Slice colour: the model's provider brand when known, the rank palette
    when not, and the neutral grey for the folded "other" bucket.  A brand
    repeated in the ring shades toward the surface (brand_shade) at the
    model's own name-sorted step — ``steps`` is brand_step_map over this
    ring's models, the same rule the browser chart's window-wide map uses,
    so both views agree when the model sets match.  Mirrored in the browser
    JS (sliceFill) for the canvas and the re-rendered legend."""
    model = slices[index]["model"]
    if model == "other":
        return MODEL_OTHER_COLOR
    key = provider_key(model)
    if not key:
        return slice_color(index)
    return brand_shade(PROVIDER_BRANDS[key]["color"], steps.get(model, 0))


def fmt_pct(part: int, total: int) -> str:
    if total <= 0 or part <= 0:
        return "0%"
    share = part / total * 100
    return ("<1" if share < 1 else str(round(share))) + "%"


def model_legend_html(slices: list[dict[str, Any]]) -> str:
    """Legend body: swatch, brand logo, model, tokens, share — colour never
    carries it alone."""
    total = sum(s["tokens"] for s in slices)
    steps = brand_step_map([s["model"] for s in slices if s["model"] != "other"])
    items = "".join(
        f'<li class="lrow" data-model="{esc(s["model"])}" tabindex="0"'
        f' title="Filter to {esc(s["model"])}">'
        f'<span class="swatch" style="background:{slice_fill(slices, i, steps)}"></span>'
        f'<span class="name">{model_name_html(s["model"])}</span>'
        f'<span class="num">{esc(fmt_stat(s["tokens"]))}</span>'
        f'<span class="pct">{esc(fmt_pct(s["tokens"], total))}</span>'
        "</li>"
        for i, s in enumerate(slices)
    )
    return f'<ul class="legend" id="model-legend">{items}</ul>'


def donut_aria(slices: list[dict[str, Any]], range_label: str) -> str:
    total = sum(s["tokens"] for s in slices)
    breakdown = ", ".join(f"{s['model']} {fmt_pct(s['tokens'], total)}" for s in slices)
    return f"Donut chart of token share by model over {range_label}. {breakdown}"


def model_panel_html(by_model: list[dict[str, Any]] | None, range_label: str = "last 24 h") -> str:
    """First-paint twin of the browser-rendered donut panel."""
    slices = donut_slices(by_model)
    aria = donut_aria(slices, range_label) if slices else f"Donut chart of token share by model over {range_label}. No usage."
    return (
        '<section class="card" aria-label="Model usage">'
        '<div class="card-head"><h2>Model usage</h2>'
        '<span class="win" id="donut-win">'
        + esc(range_label)
        + " &middot; by total tokens &middot; top "
        + str(MODEL_TOP_N)
        + " + other</span></div>"
        '<div class="donut-row" id="donut-row"'
        + ("" if slices else " hidden")
        + "><div class=\"donut-wrap\" id=\"donut-wrap\" tabindex=\"0\" role=\"group\" aria-label=\""
        + esc(aria)
        + '">'
        f'<canvas id="donut" width="{DONUT_SIZE}" height="{DONUT_SIZE}"></canvas>'
        '<div class="tooltip" id="donut-tip" hidden></div>'
        "</div>"
        + model_legend_html(slices)
        + '</div><p class="muted donut-empty" id="donut-empty"'
        + (" hidden" if slices else "")
        + ">no usage in the filtered window</p>"
        "</section>"
    )


# --------------------------------------------------------------------------
# Styling — dark, near-black, one accent hue, system font stack, no external
# assets of any kind (no CDN links, no webfonts).
# --------------------------------------------------------------------------

CSS = """
:root {
  color-scheme: dark;
  --bg: #0a0d12;
  --surface: #11151c;
  --surface-2: #171c25;
  --border: rgba(255, 255, 255, 0.07);
  --border-strong: rgba(255, 255, 255, 0.15);
  --grid: #1f2531;
  --text: #e8edf4;
  --text-2: #a7b2c3;
  --muted: #6d7889;
  --accent: #3987e5;
  --accent-bright: #6aa6ee;
  --good: #2ea043;
  --bad: #e5534b;
  --stale: #d29922;
  --unattr: #8b95a6;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, "Liberation Mono", monospace;
}
* { box-sizing: border-box; }
/* author display values must not defeat the hidden attribute (donut-row) */
[hidden] { display: none !important; }
html, body { margin: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--sans);
  font-size: 15px;
  line-height: 1.45;
  -webkit-font-smoothing: antialiased;
}

.wrap { max-width: 1400px; margin: 0 auto; padding: 26px 28px 56px; }

.topbar { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 18px; }
h1 { margin: 0; font-size: 1.32rem; font-weight: 650; letter-spacing: -0.015em; }
h1 .accent { color: var(--accent-bright); }
.subtitle { margin: 3px 0 0; color: var(--text-2); font-size: 0.83rem; }

.live {
  display: inline-flex; align-items: center; gap: 9px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 999px;
  padding: 6px 14px; font-size: 0.78rem; color: var(--text-2); white-space: nowrap;
  font-family: var(--mono); font-variant-numeric: tabular-nums;
}
.live .dot { flex: none; width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
.live.ok .dot { background: var(--good); box-shadow: 0 0 0 3px rgba(46, 160, 67, 0.18); }
.live.error .dot { background: var(--bad); box-shadow: 0 0 0 3px rgba(229, 83, 75, 0.2); }
.live.stale .dot { background: var(--stale); box-shadow: 0 0 0 3px rgba(210, 153, 34, 0.18); }
.live .sep { color: var(--muted); }
.live .updated { color: var(--text); }
@media (prefers-reduced-motion: no-preference) {
  .live.ok .dot, .live.stale .dot { animation: pulse 2.4s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: 0.4; } }
}

.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 12px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 15px 17px; min-width: 0; }
.card h2 { margin: 0; font-size: 0.92rem; font-weight: 600; }
.card-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
.card-meta { display: flex; gap: 14px; flex-wrap: wrap; }
.win { color: var(--muted); font-size: 0.73rem; }

/* harness/model segmented toggle in the hourly chart card head */
.chart-mode { display: inline-flex; gap: 2px; padding: 2px; background: rgba(255, 255, 255, 0.04); border: 1px solid var(--border); border-radius: 7px; }
.chart-mode .mode-btn {
  appearance: none; border: 0; padding: 3px 10px; border-radius: 5px;
  background: transparent; color: var(--muted); cursor: pointer;
  font-family: var(--mono); font-size: 0.66rem; font-weight: 600;
  letter-spacing: 0.05em; line-height: 1.5;
}
.chart-mode .mode-btn:hover { color: var(--text-2); }
.chart-mode .mode-btn:focus-visible { box-shadow: 0 0 0 2px var(--accent); outline: none; }
.chart-mode .mode-btn.active { background: rgba(57, 135, 229, 0.28); color: var(--text); }
.stat .label { color: var(--muted); font-size: 0.71rem; font-weight: 600; letter-spacing: 0.07em; text-transform: uppercase; }
.stat .value { font-family: var(--mono); font-size: 1.78rem; font-weight: 600; letter-spacing: -0.02em; line-height: 1.15; margin-top: 9px; font-variant-numeric: tabular-nums; }
.stat .hint { color: var(--muted); font-size: 0.74rem; margin-top: 7px; font-variant-numeric: tabular-nums; }

.mid { display: grid; grid-template-columns: minmax(0, 1fr); gap: 12px; margin-bottom: 12px; }
@media (min-width: 1080px) { .mid { grid-template-columns: minmax(0, 1.9fr) minmax(330px, 1fr); align-items: start; } }
.side { display: grid; gap: 12px; min-width: 0; }

.error-card { display: none; margin-bottom: 12px; border-color: rgba(229, 83, 75, 0.45); background: rgba(229, 83, 75, 0.07); }
.error-card.show { display: block; }
.error-card h2 { color: #f1a1a1; }
.error-card p { margin: 6px 0 0; color: var(--text-2); font-size: 0.82rem; overflow-wrap: anywhere; }

.scroll-x { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 0.84rem; }
th {
  text-align: left; color: var(--muted); font-size: 0.68rem; font-weight: 600;
  letter-spacing: 0.06em; text-transform: uppercase; padding: 6px 10px;
  border-bottom: 1px solid var(--border-strong); white-space: nowrap;
}
td { padding: 7px 10px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover td { background: var(--surface-2); }
th.num, td.num { text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; }
td.total { font-weight: 600; }
td.bar-cell { width: 34%; min-width: 96px; }
th:first-child, td:first-child { padding-left: 2px; }
th:last-child, td:last-child { padding-right: 2px; }

.events th { position: sticky; top: 0; background: var(--surface); border-bottom: none; box-shadow: inset 0 -1px 0 var(--border-strong); z-index: 2; }
tr.row-crit td { background: rgba(229, 83, 75, 0.10); }
tr.row-crit:hover td { background: rgba(229, 83, 75, 0.16); }
tr.row-crit td:first-child { box-shadow: inset 2px 0 0 var(--bad); }

.muted { color: var(--muted); }

/* harness palette — muted, one hue per caller, applied via these classes.
   .hb-* are the stable identity colours (HARNESS_BRANDS): a known caller
   keeps its brand ahead of the hashed rank slots; the browser's HARNESS_HEX
   mirror carries the same hexes for the canvas paths */
.h0 { --hc: #5b8def; }
.h1 { --hc: #3fb0a3; }
.h2 { --hc: #9a7be0; }
.h3 { --hc: #d9a13b; }
.h4 { --hc: #d9708f; }
.h5 { --hc: #7fae83; }
.h-unattr { --hc: #66738a; }
.hb-hermes { --hc: #3987e5; }
.hb-claude-code { --hc: #D97757; }
.hb-codex { --hc: #10A37F; }
.hb-hindsight { --hc: #2ea79a; }
.hb-openrouter { --hc: #6467F2; }
.hb-grok { --hc: #BFC7D3; }
.chip { display: inline-flex; align-items: center; gap: 7px; color: var(--text-2); }
.chip::before { content: ""; flex: none; width: 8px; height: 8px; border-radius: 50%; background: var(--hc, var(--muted)); }
.h-unattr .chip { color: var(--unattr); font-style: italic; }

/* provider brand mark beside model names — the model twin of the harness
   chip.  Glyph colour is an inline style; the canvas twins are the
   PROVIDER_HEXES mirror in the page JS (a canvas cannot read CSS values) */
.mbrand { display: inline-flex; align-items: center; gap: 6px; }
.plogo { display: inline-flex; flex: none; }
.plogo svg { width: 12px; height: 12px; display: block; }
.legend .plogo svg { width: 11px; height: 11px; }
.tooltip .tl .plogo svg { width: 10px; height: 10px; }

.bar-track { height: 6px; background: rgba(255, 255, 255, 0.05); border-radius: 3px; overflow: hidden; }
.bar-fill { height: 100%; min-width: 3px; background: var(--hc, var(--accent)); border-radius: 0 3px 3px 0; }

.chart-wrap { position: relative; outline: none; border-radius: 8px; }
.chart-wrap:focus-visible { box-shadow: 0 0 0 2px var(--accent); }
.chart-wrap canvas { display: block; width: 100%; }

/* model-usage donut: ring on the left, legend beside it, wraps below on narrow cards */
.donut-row { display: flex; align-items: center; gap: 18px; flex-wrap: wrap; }
.donut-wrap { position: relative; flex: none; outline: none; border-radius: 8px; }
.donut-wrap:focus-visible { box-shadow: 0 0 0 2px var(--accent); }
.donut-wrap canvas { display: block; }
.donut-empty { margin: 4px 0 2px; font-size: 0.82rem; }
.legend { list-style: none; margin: 0; padding: 0; flex: 1 1 160px; min-width: 160px; }
.legend li { display: flex; align-items: center; gap: 8px; padding: 4px 0; font-size: 0.8rem; border-bottom: 1px solid var(--grid); }
.legend li:last-child { border-bottom: none; }
.legend .swatch { flex: none; width: 9px; height: 9px; border-radius: 3px; }
.legend .name { color: var(--text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.legend .num { margin-left: auto; font-family: var(--mono); font-variant-numeric: tabular-nums; }
.legend .pct { color: var(--muted); min-width: 3.2em; text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; }

.tooltip {
  position: absolute; z-index: 5; transform: translate(-50%, 0);
  background: rgba(9, 12, 17, 0.96); border: 1px solid var(--border-strong); border-radius: 8px;
  padding: 7px 10px; font-size: 0.78rem; pointer-events: none;
  box-shadow: 0 10px 26px rgba(0, 0, 0, 0.45); white-space: nowrap;
}
.tooltip .tv { font-weight: 650; font-family: var(--mono); font-variant-numeric: tabular-nums; }
.tooltip .tl { color: var(--text-2); margin-top: 2px; }
/* harness swatch inside a chart-tooltip line — colour set from JS */
.tooltip .tl-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; background: var(--muted); }

.badge { display: inline-flex; align-items: center; gap: 7px; color: var(--text-2); }
.dot-s { flex: none; width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
.tone-good { background: var(--good); }
.tone-crit { background: var(--bad); }
.tone-none { background: #66738a; }

/* filter bar — one shared state, every control reuses the page tokens */
.filters { margin-bottom: 12px; }
.filter-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(168px, 1fr)); gap: 10px 12px; }
.f { display: grid; gap: 4px; min-width: 0; }
.f > span { color: var(--muted); font-size: 0.68rem; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; }
.f select, .f input[type="search"] {
  width: 100%; background: var(--surface-2); color: var(--text);
  border: 1px solid var(--border); border-radius: 7px; padding: 6px 8px;
  font: inherit; font-size: 0.8rem;
}
.f select:focus-visible, .f input[type="search"]:focus-visible { outline: none; box-shadow: 0 0 0 2px var(--accent); }
.chatpick { grid-row: span 2; }
.chatpick select { margin-top: 2px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.chips:empty { margin-top: 0; }
.fchip {
  display: inline-flex; align-items: center; gap: 7px; cursor: pointer;
  background: rgba(57, 135, 229, 0.14); color: var(--text-2);
  border: 1px solid rgba(57, 135, 229, 0.4); border-radius: 999px;
  padding: 3px 10px; font: inherit; font-size: 0.76rem; max-width: 100%;
}
.fchip:hover { color: var(--text); border-color: var(--accent); }
.fchip:focus-visible { outline: none; box-shadow: 0 0 0 2px var(--accent); }
.fchip .fk { color: var(--accent-bright); font-weight: 600; }
.fchip .fx { color: var(--muted); font-size: 0.68rem; }
.fbtn {
  appearance: none; cursor: pointer; background: transparent;
  border: 1px solid var(--border); border-radius: 7px; color: var(--muted);
  padding: 3px 10px; font: inherit; font-size: 0.72rem;
}
.fbtn:hover { color: var(--text-2); border-color: var(--border-strong); }
.fbtn:focus-visible { outline: none; box-shadow: 0 0 0 2px var(--accent); }

/* per-chat table: drill-down rows + sortable numeric columns */
.crow { cursor: pointer; }
.crow:focus-visible { outline: none; box-shadow: inset 0 0 0 2px var(--accent); }
.cname { color: var(--text-2); max-width: 340px; overflow: hidden; text-overflow: ellipsis; }
.crow:hover .cname, .hrow:hover .chip, .lrow:hover .name { color: var(--text); }
.csub { color: var(--muted); font-size: 0.72rem; font-family: var(--mono); max-width: 340px; overflow: hidden; text-overflow: ellipsis; }
.c-unknown .cname { color: var(--unattr); font-style: italic; }
.hrow, .lrow { cursor: pointer; }
.hrow:focus-visible, .lrow:focus-visible { outline: none; box-shadow: inset 0 0 0 2px var(--accent); }
/* Hermes IDE profile subcategories: `hermes:<profile>` rows indent under a
   parent row whose totals also fold in pre-split plain `hermes` traffic;
   the subtotal is display-only (not clickable) — the harness filter matches
   one exact caller, so the family row has no honest single selection */
tr.subrow td:first-child { padding-left: 26px; }
tr.subtotal td { background: var(--surface-2); border-bottom-color: var(--border-strong); }
tr.subtotal .chip { color: var(--text); }
tr.subtotal td.num { font-weight: 600; }
th.sortable { cursor: pointer; user-select: none; }
th.sortable:hover, th.sortable:focus-visible { color: var(--text-2); outline: none; }
th.sortable.sorted-asc::after { content: " ▲"; font-size: 0.6rem; }
th.sortable.sorted-desc::after { content: " ▼"; font-size: 0.6rem; }

footer { margin-top: 20px; color: var(--muted); font-size: 0.75rem; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }

@media (max-width: 600px) {
  body { font-size: 14px; }
  .wrap { padding: 18px 12px 44px; }
  h1 { font-size: 1.12rem; }
  .stat .value { font-size: 1.5rem; }
  .card { padding: 13px 14px; }
  th, td { padding: 6px 8px; }
}
"""


# --------------------------------------------------------------------------
# Client layer — polls /api/summary, /api/timeseries and /api/events every
# POLL_SECONDS and re-renders cards / harness bars / canvas charts (hourly
# columns, model donut) / event table in place.  All DB-derived strings are
# inserted with textContent, never innerHTML.
# --------------------------------------------------------------------------

JS = r"""
(function () {
  'use strict';

  var POLL_MS = __POLL_MS__;
  var FETCH_TIMEOUT_MS = __FETCH_TIMEOUT_MS__;
  var DASHBOARD_EVENTS = __DASHBOARD_EVENTS__;
  var N_COLORS = __HARNESS_COLOR_COUNT__;
  var UNATTR = 'unattributed';
  var HERMES = 'hermes';
  var HERMES_PREFIX = 'hermes:';
  var HERMES_DISPLAY = 'Hermes IDE';
  var RATE_LIMIT_CODES = [401, 429];
  var CH = { H: 260, padL: 50, padR: 12, padT: 24, padB: 26, barMax: 30 };
  var C = {
    bar: '#3987e5', barHot: '#6aa6ee', barPartial: 'rgba(57, 135, 229, 0.45)',
    grid: '#1f2531', axis: '#2b3342', text: '#6d7889', textStrong: '#a7b2c3',
    peak: '#e8edf4'
  };

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function setText(id, text) { var node = $(id); if (node) node.textContent = text; }

  /* Prototype-safe own-property test: every plain-object lookup keyed by a
     DB-derived string (a model name) goes through this, so a model literally
     named "constructor" or "toString" can never resolve through
     Object.prototype.  The brand/harness tables (PROVIDER_EXACT, PROVIDER_HEXES,
     PROVIDER_LOGOS, PROVIDER_GLYPHS, HARNESS_HEX, …) are safe without it
     because their keys reach them only via providerKey()/harnessKey(), whose
     results are the author-controlled key strings. */
  function hasOwn(obj, key) {
    return Object.prototype.hasOwnProperty.call(obj, key);
  }

  function fmtCompact(value) {
    var n = Number(value) || 0;
    var steps = [[1e9, 'B'], [1e6, 'M'], [1e3, 'k']];
    for (var i = 0; i < steps.length; i++) {
      if (Math.abs(n) >= steps[i][0]) {
        return (n / steps[i][0]).toFixed(1).replace(/\.0$/, '') + steps[i][1];
      }
    }
    return String(Math.round(n));
  }

  function fmtInt(value) {
    if (value === null || value === undefined) return '—';
    return Number(value).toLocaleString('en-US');
  }

  function fmtStat(value) {
    var n = Number(value) || 0;
    return Math.abs(n) < 10000 ? fmtInt(n) : fmtCompact(n);
  }

  function fmtOpt(value) { return (value === null || value === undefined) ? '—' : fmtStat(value); }

  function fmtAvg(tokens, requests) {
    if (!requests) return 'no requests';
    return 'avg ' + fmtCompact(tokens / requests) + ' / request';
  }

  function fmtCachedHint(cached) {
    return (Number(cached) || 0) > 0 ? 'incl. ' + fmtCompact(cached) + ' cached' : 'prompt tokens';
  }

  /* ---- shared filter state ----
     One state, narrowed everywhere at once: the page URL is the source of
     truth (refresh keeps it, a copied link reproduces it), the controls
     render it, every /api poll carries it, and the server applies the same
     parsing to the first paint. */
  var FILTER_KEYS = ['harness', 'provider', 'model', 'type', 'chat', 'route', 'outcome'];
  var RANGE_KEYS = ['24h', '7d', '30d', 'all'];
  var RANGE_LABELS = { '24h': 'last 24 h', '7d': 'last 7 d', '30d': 'last 30 d', 'all': 'all time' };
  var BUCKET_WORDS = { '24h': '1 h buckets', '7d': '1 d buckets', '30d': '1 d buckets', 'all': 'auto-width buckets' };
  var rangeKey = '24h';

  function readStateFromUrl() {
    var params = new URLSearchParams(window.location.search);
    var r = params.get('range');
    rangeKey = RANGE_KEYS.indexOf(r) >= 0 ? r : '24h';
    var state = {};
    FILTER_KEYS.forEach(function (k) {
      var v = params.get(k);
      if (v) state[k] = v;
    });
    return state;
  }

  function currentState() {
    var state = {};
    FILTER_KEYS.forEach(function (k) {
      var sel = $('f-' + k);
      if (sel && sel.value) state[k] = sel.value;
    });
    return state;
  }

  function stateQuery() {
    var params = new URLSearchParams();
    params.set('range', rangeKey);
    var state = currentState();
    FILTER_KEYS.forEach(function (k) { if (state[k]) params.set(k, state[k]); });
    return params.toString();
  }

  function writeStateToUrl() {
    var qs = stateQuery();
    history.replaceState(null, '', window.location.pathname + (qs ? '?' + qs : ''));
  }

  function optionExists(sel, value) {
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === value) return true;
    }
    return false;
  }

  function applyStateToControls(state) {
    var r = $('f-range');
    if (r) r.value = rangeKey;
    FILTER_KEYS.forEach(function (k) {
      var sel = $('f-' + k);
      if (!sel) return;
      var v = state[k] || '';
      /* an active filter can be absent from the cross-filtered option list —
         keep it selectable anyway, never silently dropped */
      if (v && !optionExists(sel, v)) sel.appendChild(new Option(v, v));
      sel.value = v;
    });
    applyChatSearch();
  }

  function onFilterChange() {
    var r = $('f-range');
    if (r && RANGE_KEYS.indexOf(r.value) >= 0) rangeKey = r.value;
    writeStateToUrl();
    renderChips();
    tick();  /* 'updating…' until the refetch for this state lands */
    poll();
  }

  function setFilter(key, value) {
    var sel = $('f-' + key);
    if (!sel) return;
    if (value && !optionExists(sel, value)) sel.appendChild(new Option(value, value));
    sel.value = value;
    onFilterChange();
  }

  function renderChips() {
    var box = $('filter-chips');
    if (!box) return;
    var state = currentState();
    box.textContent = '';
    var active = 0;
    FILTER_KEYS.forEach(function (k) {
      if (!state[k]) return;
      active++;
      var chip = el('button', 'fchip');
      chip.type = 'button';
      chip.title = 'Clear the ' + k + ' filter';
      chip.appendChild(el('span', 'fk', k));
      chip.appendChild(document.createTextNode(' ' + state[k] + ' '));
      chip.appendChild(el('span', 'fx', '✕'));
      chip.addEventListener('click', function () { setFilter(k, ''); });
      box.appendChild(chip);
    });
    setText('filter-count', active ? String(active) + ' active' : 'no filters active');
    var clear = $('filter-clear');
    if (clear) clear.hidden = !active;
  }

  /* facet option lists are cross-filtered on the server; here they are
     re-rendered around the current selection (which is never lost) */
  function renderFacetOptions(key, facet) {
    var sel = $('f-' + key);
    if (!sel) return;
    var current = sel.value;
    var allLabel = sel.options.length ? sel.options[0].text : 'All';
    sel.textContent = '';
    sel.appendChild(new Option(allLabel, ''));
    var seen = {};
    (facet && facet.options ? facet.options : []).forEach(function (o) {
      var value, label;
      if (key === 'chat') {
        value = String(o.key || '');
        label = (o.display || value) + ' · ' + fmtCompact(o.total_tokens) + ' tok · ' + fmtInt(o.requests) + ' req';
      } else if (key === 'harness') {
        /* label shows the Hermes family as 'Hermes IDE [· profile]'; the
           option VALUE stays the raw caller — it is the filter key */
        value = String(o.value || '');
        label = callerDisplay(value) + ' · ' + fmtCompact(o.tokens) + ' tok · ' + fmtInt(o.requests) + ' req';
      } else {
        value = String(o.value || '');
        label = value + ' · ' + fmtCompact(o.tokens) + ' tok · ' + fmtInt(o.requests) + ' req';
      }
      if (!value || hasOwn(seen, value)) return;
      seen[value] = true;
      sel.appendChild(new Option(label, value));
    });
    if (current && !hasOwn(seen, current)) sel.appendChild(new Option(current, current));
    sel.value = current;
    if (key === 'chat') applyChatSearch();
  }

  function applyChatSearch() {
    var input = $('f-chat-search'), sel = $('f-chat');
    if (!input || !sel) return;
    var q = input.value.trim().toLowerCase();
    for (var i = 0; i < sel.options.length; i++) {
      var opt = sel.options[i];
      opt.hidden = !!(q && opt.value &&
        opt.text.toLowerCase().indexOf(q) < 0 && opt.value.toLowerCase().indexOf(q) < 0);
    }
  }

  function wireFilters() {
    var r = $('f-range');
    if (r) r.addEventListener('change', onFilterChange);
    FILTER_KEYS.forEach(function (k) {
      var sel = $('f-' + k);
      if (sel) sel.addEventListener('change', onFilterChange);
    });
    var search = $('f-chat-search');
    if (search) search.addEventListener('input', applyChatSearch);
    var clear = $('filter-clear');
    if (clear) clear.addEventListener('click', function () {
      FILTER_KEYS.forEach(function (k) {
        var sel = $('f-' + k);
        if (sel) sel.value = '';
      });
      onFilterChange();
    });
    window.addEventListener('popstate', function () {
      applyStateToControls(readStateFromUrl());
      renderChips();
      tick();
      poll();
    });
    /* drill-down: chat rows, harness rows and donut legend entries set their
       filter (delegated — every poll re-renders the rows) */
    $('chat-body').addEventListener('click', drillHandler('chat', 'data-chat'));
    $('harness-body').addEventListener('click', drillHandler('harness', 'data-harness'));
    $('model-legend').addEventListener('click', drillHandler('model', 'data-model'));
  }

  function drillHandler(key, attr) {
    return function (ev) {
      var row = ev.target.closest('tr[' + attr + '],li[' + attr + ']');
      if (!row) return;
      var value = row.getAttribute(attr);
      if (value) setFilter(key, value);
    };
  }

  /* same djb2 as the server's harness_color_idx, so chip colours agree */
  function djb2(name) {
    var h = 5381;
    for (var i = 0; i < name.length; i++) h = (h * 33 + name.charCodeAt(i)) % 2147483647;
    return h;
  }

  /* harness identity prefixes — the twin of the server's HARNESS_PREFIXES
     (same longest-prefix-first order), consulted before the hashed rank
     palette so a known caller keeps its colour as ranks shift */
  var HARNESS_PREFIXES = [
    ['openai-codex', 'codex'], ['codex', 'codex'], ['claude-code', 'claude-code'],
    ['hermes', 'hermes'], ['hindsight', 'hindsight'], ['openrouter', 'openrouter'],
    ['grok', 'grok'], ['xai', 'grok']
  ];

  /* same longest-prefix, case-insensitive match as the server's harness_key */
  function harnessKey(caller) {
    if (!caller || caller === UNATTR) return null;
    var name = String(caller).toLowerCase();
    for (var i = 0; i < HARNESS_PREFIXES.length; i++) {
      if (name.indexOf(HARNESS_PREFIXES[i][0]) === 0) return HARNESS_PREFIXES[i][1];
    }
    return null;
  }

  function harnessClass(caller) {
    var key = harnessKey(caller);
    if (key) return 'hb-' + key;
    if (!caller || caller === UNATTR) return 'h-unattr';
    return 'h' + (djb2(caller) % N_COLORS);
  }

  /* display name for a raw caller value — labels only.  Raw caller strings
     stay the keys, option values, filter values and colour-hash inputs
     everywhere (same contract as the server's caller_display) */
  function isHermesProfile(c) {
    return typeof c === 'string' && c.indexOf(HERMES_PREFIX) === 0 && c.length > HERMES_PREFIX.length;
  }

  function callerDisplay(c) {
    if (!c) return UNATTR;
    if (c === HERMES) return HERMES_DISPLAY;
    if (isHermesProfile(c)) return HERMES_DISPLAY + ' · ' + c.slice(HERMES_PREFIX.length);
    return c;
  }

  /* hex mirror of the .h0…h5/.h-unattr/.hb-* CSS palette — a canvas cannot
     read CSS custom properties, so the chart resolves harnessClass() to hex
     here; the hb-* entries are the server's HARNESS_BRANDS twin */
  var HARNESS_HEX = {
    h0: '#5b8def', h1: '#3fb0a3', h2: '#9a7be0',
    h3: '#d9a13b', h4: '#d9708f', h5: '#7fae83', 'h-unattr': '#66738a',
    'hb-hermes': '#3987e5', 'hb-claude-code': '#D97757', 'hb-codex': '#10A37F',
    'hb-hindsight': '#2ea79a', 'hb-openrouter': '#6467F2', 'hb-grok': '#BFC7D3'
  };

  function harnessHex(caller) {
    return HARNESS_HEX[harnessClass(caller)] || HARNESS_HEX['h-unattr'];
  }

  /* hex mirror of the server's MODEL_COLORS + MODEL_OTHER_COLOR — the chart's
     per-model mode hashes into these instead of the harness palette when a
     model has no provider brand */
  var MODEL_HEXES = ['#bd8714', '#d46c8b', '#5b8def', '#2ea79a', '#9a7be0', '#65a46c', '#66738a'];

  /* provider brand mirror of the server's PROVIDER_PREFIXES / PROVIDER_BRANDS
     (same longest-prefix-first order): the re-rendered DOM and the canvases
     need the same prefixes, colours, glyph tints and inline logos the first
     paint got from Python.  Logos are Simple Icons (CC0) path data; Z.ai and
     Kimi are clean initial badges (brand-coloured rounded square + letter) */
  var PROVIDER_PREFIXES = [
    ['openrouter/', 'openrouter'], ['claude-', 'claude'], ['anthropic/', 'claude'],
    ['codex', 'openai'], ['openai/', 'openai'], ['gpt-', 'openai'],
    ['grok-', 'grok'], ['x-ai/', 'grok'], ['xai/', 'grok'],
    ['kimi-', 'kimi'], ['kimi/', 'kimi'], ['moonshot/', 'kimi'],
    ['moonshotai/', 'kimi'], ['glm-', 'zai'], ['z-ai/', 'zai'],
    ['zai', 'zai'], ['o4-', 'openai'], ['o3', 'openai']
  ];
  var PROVIDER_EXACT = { k3: 'kimi', k2: 'kimi' };  /* native bare IDs */
  var PROVIDER_NAMES = {
    openai: 'ChatGPT/OpenAI', zai: 'Z.ai', kimi: 'Kimi (Moonshot AI)',
    claude: 'Claude (Anthropic)', grok: 'Grok (xAI)', openrouter: 'OpenRouter'
  };
  var PROVIDER_HEXES = {
    openai: '#10A37F', zai: '#8A8AF0', kimi: '#5A5AF5',
    claude: '#D97757', grok: '#BFC7D3', openrouter: '#6467F2'
  };
  var PROVIDER_GLYPHS = { grok: '#E8EDF4' };  /* monochrome white-on-dark X */
  var PROVIDER_LOGOS = {
    openai: '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z"/></svg>',
    zai: '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><rect width="24" height="24" rx="6" fill="#8A8AF0"/><text x="12" y="17.2" text-anchor="middle" font-family="inherit" font-size="14.5" font-weight="700" fill="#e8edf4">Z</text></svg>',
    kimi: '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><rect width="24" height="24" rx="6" fill="#5A5AF5"/><text x="12" y="17.2" text-anchor="middle" font-family="inherit" font-size="14.5" font-weight="700" fill="#e8edf4">K</text></svg>',
    claude: '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M17.3041 3.541h-3.6718l6.696 16.918H24Zm-10.6082 0L0 20.459h3.7442l1.3693-3.5527h7.0052l1.3693 3.5528h3.7442L10.5363 3.5409Zm-.3712 10.2232 2.2914-5.9456 2.2914 5.9456Z"/></svg>',
    grok: '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M18.901 1.153h3.68l-8.04 9.19L24 22.846h-7.406l-5.8-7.584-6.638 7.584H.474l8.6-9.83L0 1.154h7.594l5.243 6.932ZM17.61 20.644h2.039L6.486 3.24H4.298Z"/></svg>',
    openrouter: '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M16.778 1.844v1.919q-.569-.026-1.138-.032-.708-.008-1.415.037c-1.93.126-4.023.728-6.149 2.237-2.911 2.066-2.731 1.95-4.14 2.75-.396.223-1.342.574-2.185.798-.841.225-1.753.333-1.751.333v4.229s.768.108 1.61.333c.842.224 1.789.575 2.185.799 1.41.798 1.228.683 4.14 2.75 2.126 1.509 4.22 2.11 6.148 2.236.88.058 1.716.041 2.555.005v1.918l7.222-4.168-7.222-4.17v2.176c-.86.038-1.611.065-2.278.021-1.364-.09-2.417-.357-3.979-1.465-2.244-1.593-2.866-2.027-3.68-2.508.889-.518 1.449-.906 3.822-2.59 1.56-1.109 2.614-1.377 3.978-1.466.667-.044 1.418-.017 2.278.02v2.176L24 6.014Z"/></svg>'
  };

  /* same longest-prefix, case-insensitive match as the server's provider_key;
     the exact table is consulted own-property-only, so prototype names
     ("constructor", "toString", …) fall through to the prefixes — and then
     to no brand at all — instead of resolving through Object.prototype */
  function providerKey(model) {
    if (!model) return null;
    var name = String(model).toLowerCase();
    if (hasOwn(PROVIDER_EXACT, name)) return PROVIDER_EXACT[name];
    for (var i = 0; i < PROVIDER_PREFIXES.length; i++) {
      if (name.indexOf(PROVIDER_PREFIXES[i][0]) === 0) return PROVIDER_PREFIXES[i][1];
    }
    return null;
  }

  /* server twin: brand_shade — same-brand repeats dim toward the card surface
     and stay plain hex, so the partial-bar dim (hexToRgba) keeps working */
  var CARD_SURFACE = '#11151c';
  function mixHex(a, b, t) {
    var pa = parseInt(a.slice(1), 16), pb = parseInt(b.slice(1), 16);
    var out = '#';
    for (var shift = 16; shift >= 0; shift -= 8) {
      var v = Math.round(((pa >> shift) & 255) * t + ((pb >> shift) & 255) * (1 - t)).toString(16);
      out += v.length < 2 ? '0' + v : v;
    }
    return out;
  }
  function brandShade(hex, step) {
    if (step <= 0) return hex;
    /* server twin BRAND_SHADE_STEPS: never repeats and never clamps — past
       the table the factor keeps shrinking ×0.75 per repeat, strictly
       darker until 8-bit hex saturation */
    var STEPS = [0.62, 0.40, 0.52, 0.34, 0.58, 0.28,
                 0.24, 0.19, 0.15, 0.11, 0.08, 0.05];
    var i = step - 1;
    var t = i < STEPS.length ? STEPS[i]
      : STEPS[STEPS.length - 1] * Math.pow(0.75, i - STEPS.length + 1);
    return mixHex(hex, CARD_SURFACE, t);
  }

  /* deterministic name order — plain code-point comparison, the twin of the
     server's sorted(), so both sides build identical step maps */
  function nameOrder(a, b) { return a < b ? -1 : a > b ? 1 : 0; }

  /* A model's brand-shade step: its index among its provider's models,
     sorted by name — a function of the model's own identity alone, so the
     shade is stable wherever the model appears.  The hourly chart builds
     this over every model visible in the window (chartShadeMap, cached in
     modelShadeSteps), the donut over its own slice list (donutShadeSteps):
     one rule, so whenever the two views show the same model set they
     resolve the same steps — and therefore the same shades.  (Counting
     repeats in ring order instead would not agree: ring order is
     tokens-desc, not name order.)  Server twin: brand_step_map. */
  function brandShadeSteps(models) {
    var byProvider = {};
    var steps = {};
    models.forEach(function (m) {
      var key = providerKey(m);
      if (!key) return;
      if (!hasOwn(byProvider, key)) byProvider[key] = [];
      byProvider[key].push(m);
    });
    Object.keys(byProvider).forEach(function (key) {
      var names = byProvider[key].sort(nameOrder);
      for (var i = 0; i < names.length; i++) steps[names[i]] = i;
    });
    return steps;
  }

  /* coloured logo span for a provider key — the markup is the static,
     author-controlled PROVIDER_LOGOS twin of the server's model_name_html,
     so innerHTML is safe here; model names themselves always stay textContent */
  function brandLogoEl(key) {
    if (!key) return null;
    var span = el('span', 'plogo');
    span.style.color = PROVIDER_GLYPHS[key] || PROVIDER_HEXES[key];
    span.innerHTML = PROVIDER_LOGOS[key];
    return span;
  }

  /* provider logo + model name — the events-table cell twin of the server's
     model_name_html */
  function brandBadge(model) {
    var key = providerKey(model);
    var wrap = el('span', 'mbrand');
    if (key) {
      wrap.title = PROVIDER_NAMES[key];
      wrap.appendChild(brandLogoEl(key));
    }
    wrap.appendChild(document.createTextNode(model || '—'));
    return wrap;
  }

  /* fixed two-slot palettes for the in/out and cache breakdowns — the
     accent/input blue and the --stale amber are var() values the canvas
     cannot read, mirrored here as hex like the palettes above */
  var INOUT_HEXES = { input: '#3987e5', output: '#2ea79a' };
  var CACHE_HEXES = { cached: '#d29922', uncached: '#6d7889' };

  function hexToRgba(hex, alpha) {
    var n = parseInt(hex.slice(1), 16);
    return 'rgba(' + (n >> 16 & 255) + ',' + (n >> 8 & 255) + ',' + (n & 255) + ',' + alpha + ')';
  }

  function bucketLabel(b) {
    if (!b) return '';
    return (b.day_sydney ? b.day_sydney + ' ' : '') + (b.label_sydney || '');
  }

  var sydFmt = null, sydTime = null;
  try {
    sydFmt = new Intl.DateTimeFormat('en-US', {
      timeZone: 'Australia/Sydney', month: 'short', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
    });
    sydTime = new Intl.DateTimeFormat('en-AU', {
      timeZone: 'Australia/Sydney', hour: '2-digit', minute: '2-digit',
      second: '2-digit', hourCycle: 'h23'
    });
  } catch (e) { /* no ICU tz data: server-provided strings are used instead */ }

  function fmtSydneyTime(ms) {
    if (!sydTime) return new Date(ms).toISOString().slice(11, 19) + 'Z';
    return sydTime.format(new Date(ms));
  }

  /* ---- stat cards ---- */

  function renderCards(s) {
    var w = s.window || {};
    var rl = (s.range && s.range.label) || RANGE_LABELS[rangeKey];
    var req = Number(w.requests) || 0;
    setText('l-req-24h', 'Requests · ' + rl);
    setText('l-tok-24h', 'Total tokens · ' + rl);
    setText('l-in-24h', 'Input tokens · ' + rl);
    setText('l-out-24h', 'Output tokens · ' + rl);
    setText('c-req-24h', fmtStat(w.requests));
    setText('h-req-24h', 'filtered window');
    setText('c-tok-24h', fmtStat(w.tokens));
    setText('h-tok-24h', fmtAvg(Number(w.tokens) || 0, req));
    setText('c-in-24h', fmtStat(w.input_tokens));
    setText('h-in-24h', fmtCachedHint(w.cached_tokens));
    setText('c-out-24h', fmtStat(w.output_tokens));
    setText('h-out-24h', fmtAvg(Number(w.output_tokens) || 0, req));
  }

  /* ---- per-harness share bars ---- */

  /* the per-harness facet's ORDER BY (tokens desc, requests desc, caller
     asc) — reused to place the Hermes IDE subtotal among the other callers */
  function harnessRank(a, b) {
    return (Number(b.total_tokens) || 0) - (Number(a.total_tokens) || 0)
      || (Number(b.requests) || 0) - (Number(a.requests) || 0)
      || (String(a.caller) < String(b.caller) ? -1 : String(a.caller) > String(b.caller) ? 1 : 0);
  }

  /* hermes:<profile> rows become indented subrows under a Hermes IDE parent
     whose totals also fold in plain `hermes` traffic (pre-profile-split
     rows); without profile rows the input order passes through unchanged —
     same contract as the server's harness_display_rows.  Display grouping
     only: raw rows are untouched, so the subtotal never double-counts. */
  function harnessDisplayRows(rows) {
    var profileRows = rows.filter(function (r) { return isHermesProfile(r.caller); });
    if (!profileRows.length) return rows.map(function (r) { return [r, '']; });
    var hermesRows = [], otherRows = [];
    rows.forEach(function (r) {
      (r.caller === HERMES || isHermesProfile(r.caller) ? hermesRows : otherRows).push(r);
    });
    var parent = {
      caller: HERMES,
      unattributed: false,
      requests: hermesRows.reduce(function (a, r) { return a + (Number(r.requests) || 0); }, 0),
      total_tokens: hermesRows.reduce(function (a, r) { return a + (Number(r.total_tokens) || 0); }, 0)
    };
    var out = [];
    otherRows.concat([parent]).sort(harnessRank).forEach(function (r) {
      if (r === parent) {
        out.push([r, 'subtotal']);
        profileRows.slice().sort(harnessRank).forEach(function (p) { out.push([p, 'subrow']); });
      } else {
        out.push([r, '']);
      }
    });
    return out;
  }

  function renderHarness(rows) {
    var tbody = $('harness-body');
    if (!tbody) return;
    tbody.textContent = '';
    if (!rows || !rows.length) {
      var emptyRow = el('tr');
      var cell = el('td', 'muted', 'No requests in the filtered window');
      cell.colSpan = 4;
      emptyRow.appendChild(cell);
      tbody.appendChild(emptyRow);
      return;
    }
    var display = harnessDisplayRows(rows);
    var max = 0;
    display.forEach(function (d) { max = Math.max(max, Number(d[0].total_tokens) || 0); });
    display.forEach(function (d) {
      var r = d[0], kind = d[1];
      var tokens = Number(r.total_tokens) || 0;
      var caller = r.caller || UNATTR;
      var cls = (r.unattributed ? 'h-unattr' : harnessClass(r.caller)) + (kind ? ' ' + kind : '');
      var tr = el('tr', cls);
      if (kind === 'subtotal') {
        /* no honest single selection represents the whole Hermes family —
           the harness filter matches one exact caller, so the subtotal
           states its scope instead of being clickable */
        tr.title = 'Hermes IDE total across every hermes caller (plain \'hermes\' plus all' +
          ' profiles) — pick a profile below to filter';
      } else {
        tr.classList.add('hrow');
        tr.setAttribute('data-harness', caller);  /* raw caller = filter value */
        tr.setAttribute('tabindex', '0');
        tr.title = 'Filter to ' + callerDisplay(caller);
      }
      var tdLabel = el('td');
      tdLabel.appendChild(el('span', 'chip', callerDisplay(caller)));
      tr.appendChild(tdLabel);
      tr.appendChild(el('td', 'num', fmtInt(r.requests)));
      tr.appendChild(el('td', 'num', fmtStat(tokens)));
      var tdBar = el('td', 'bar-cell');
      var track = el('div', 'bar-track');
      track.setAttribute('aria-hidden', 'true');
      if (max > 0 && tokens > 0) {
        var fill = el('div', 'bar-fill');
        fill.style.width = Math.max(1.5, (tokens / max) * 100) + '%';
        track.appendChild(fill);
      }
      tdBar.appendChild(track);
      tr.appendChild(tdBar);
      tbody.appendChild(tr);
    });
  }

  /* ---- per-chat table (sortable, rows drill into the chat filter) ---- */

  var chatSort = { key: 'total_tokens', dir: -1 };
  var lastChats = { rows: [], truncated: false };

  function renderChats(chats) {
    lastChats = chats && chats.rows ? chats : { rows: [], truncated: false };
    var tbody = $('chat-body');
    if (!tbody) return;
    tbody.textContent = '';
    var rows = lastChats.rows.slice();
    rows.sort(function (a, b) {
      var d = (Number(b[chatSort.key]) || 0) - (Number(a[chatSort.key]) || 0);
      if (!d) d = String(a.key) < String(b.key) ? -1 : 1;
      return chatSort.dir < 0 ? d : -d;
    });
    if (!rows.length) {
      var emptyRow = el('tr');
      var cell = el('td', 'muted', 'No requests in the filtered window');
      cell.colSpan = 6;
      emptyRow.appendChild(cell);
      tbody.appendChild(emptyRow);
      return;
    }
    rows.forEach(function (c) {
      var key = c.key || 'unknown';
      var display = c.display || 'Unknown';
      var tr = el('tr', 'crow' + (key === 'unknown' ? ' c-unknown' : ''));
      tr.setAttribute('data-chat', key);
      tr.title = 'Filter to ' + display;
      var tdChat = el('td');
      tdChat.appendChild(el('div', 'cname', display));
      var sub = c.id || c.type || '';
      if (sub && sub !== display) tdChat.appendChild(el('div', 'csub', sub));
      tr.appendChild(tdChat);
      tr.appendChild(el('td', 'num', fmtInt(c.requests)));
      tr.appendChild(el('td', 'num', fmtStat(c.input_tokens)));
      tr.appendChild(el('td', 'num', fmtStat(c.output_tokens)));
      tr.appendChild(el('td', 'num', fmtStat(c.cached_tokens)));
      tr.appendChild(el('td', 'num total', fmtStat(c.total_tokens)));
      tbody.appendChild(tr);
    });
    if (lastChats.truncated) {
      var noteRow = el('tr');
      var note = el('td', 'muted',
        'showing the top ' + rows.length + ' chats by tokens — narrow with filters to see the rest');
      note.colSpan = 6;
      noteRow.appendChild(note);
      tbody.appendChild(noteRow);
    }
    markChatSort();
  }

  function markChatSort() {
    var heads = document.querySelectorAll('#chat-table th.sortable');
    heads.forEach(function (th) {
      var active = th.getAttribute('data-sort') === chatSort.key;
      th.classList.toggle('sorted-desc', active && chatSort.dir < 0);
      th.classList.toggle('sorted-asc', active && chatSort.dir > 0);
    });
  }

  function wireChatSort() {
    document.querySelectorAll('#chat-table th.sortable').forEach(function (th) {
      th.addEventListener('click', function () {
        var key = th.getAttribute('data-sort');
        if (chatSort.key === key) chatSort.dir = -chatSort.dir;
        else { chatSort.key = key; chatSort.dir = -1; }
        renderChats(lastChats);
      });
    });
  }

  /* ---- model-usage donut (24 h, top 6 + other) ---- */

  var DN = {
    size: __DONUT_SIZE__, ring: 26,
    /* rank-order slice colours — server twin: MODEL_COLORS / MODEL_OTHER_COLOR.
     * Provider-branded models override these with PROVIDER_HEXES (sliceFill);
     * these stay the neutral palette for unknown providers */
    colors: ['#bd8714', '#d46c8b', '#5b8def', '#2ea79a', '#9a7be0', '#65a46c'],
    other: '#66738a', surface: '#11151c',
    text: '#a7b2c3', muted: '#6d7889', bright: '#e8edf4'
  };
  var donutGeom = null;      /* {slices, total, cx, cy, rIn, rOut, start} for hit tests */
  var donutHover = -1;
  var lastByModel = [];
  var lastRangeLabel = 'last 24 h';

  function donutSlices(byModel) {
    var rows = (byModel || []).filter(function (r) { return (Number(r.tokens) || 0) > 0; });
    rows.sort(function (a, b) { return (Number(b.tokens) || 0) - (Number(a.tokens) || 0) || String(a.model).localeCompare(String(b.model)); });
    var slices = rows.slice(0, DN.colors.length).map(function (r) {
      return { model: String(r.model), tokens: Number(r.tokens) || 0, requests: Number(r.requests) || 0 };
    });
    if (rows.length > DN.colors.length) {
      var rest = rows.slice(DN.colors.length);
      slices.push({
        model: 'other',
        tokens: rest.reduce(function (a, r) { return a + (Number(r.tokens) || 0); }, 0),
        requests: rest.reduce(function (a, r) { return a + (Number(r.requests) || 0); }, 0)
      });
    }
    return slices;
  }

  function sliceColor(i) { return i < DN.colors.length ? DN.colors[i] : DN.other; }

  /* the donut's step map: brandShadeSteps over this ring's own models.  The
     slice list is the stable set here, and the name-sorted rule is the same
     one the chart's window-wide map uses, so the two views agree whenever
     they show the same models */
  function donutShadeSteps(slices) {
    return brandShadeSteps(slices
      .filter(function (s) { return s.model !== 'other'; })
      .map(function (s) { return s.model; }));
  }

  /* provider brand colour, rank palette when the provider is unknown, neutral
     grey for the folded "other" bucket — and same-brand repeats shade toward
     the surface at each model's own name-sorted step (steps: donutShadeSteps
     over this ring, built once per render).  Server twin: slice_fill */
  function sliceFill(slices, i, steps) {
    var model = slices[i].model;
    if (model === 'other') return DN.other;
    var key = providerKey(model);
    if (!key) return sliceColor(i);
    var step = hasOwn(steps, model) ? steps[model] : 0;
    return brandShade(PROVIDER_HEXES[key], step);
  }

  function pctLabel(part, total) {
    if (!total || part <= 0) return '0%';
    var share = (part / total) * 100;
    return (share < 1 ? '<1' : String(Math.round(share))) + '%';
  }

  function renderDonut(byModel) {
    lastByModel = byModel || [];
    var canvas = $('donut'), wrap = $('donut-wrap');
    if (!canvas || !wrap) return;
    var slices = donutSlices(lastByModel);
    var row = $('donut-row'), empty = $('donut-empty');
    if (row) row.hidden = !slices.length;
    if (empty) empty.hidden = !!slices.length;
    if (!slices.length) { donutGeom = null; return; }

    var total = slices.reduce(function (a, s) { return a + s.tokens; }, 0);
    var dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(DN.size * dpr);
    canvas.height = Math.round(DN.size * dpr);
    canvas.style.width = DN.size + 'px';
    canvas.style.height = DN.size + 'px';
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, DN.size, DN.size);

    var cx = DN.size / 2, cy = DN.size / 2;
    var rOut = DN.size / 2 - 4, rIn = rOut - DN.ring;
    var mono = 'ui-monospace, Menlo, Consolas, monospace';
    var a0 = -Math.PI / 2;
    var shadeSteps = donutShadeSteps(slices);

    slices.forEach(function (s, i) {
      var ang = total > 0 ? (s.tokens / total) * Math.PI * 2 : 0;
      ctx.beginPath();
      ctx.moveTo(cx + Math.cos(a0) * rIn, cy + Math.sin(a0) * rIn);
      ctx.arc(cx, cy, rOut + (i === donutHover ? 3 : 0), a0, a0 + ang);
      ctx.arc(cx, cy, rIn, a0 + ang, a0, true);
      ctx.closePath();
      ctx.fillStyle = sliceFill(slices, i, shadeSteps);
      ctx.fill();
      /* 2 px surface ring = the gap between neighbouring slices */
      ctx.strokeStyle = DN.surface;
      ctx.lineWidth = 2;
      ctx.stroke();
      a0 += ang;
    });

    /* the hole carries the window total */
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = DN.bright;
    ctx.font = '600 15px ' + mono;
    ctx.fillText(fmtCompact(total), cx, cy - 8);
    ctx.fillStyle = DN.muted;
    ctx.font = '10px ' + mono;
    ctx.fillText('tokens · ' + lastRangeLabel, cx, cy + 10);

    donutGeom = { slices: slices, total: total, cx: cx, cy: cy, rIn: rIn, rOut: rOut, start: -Math.PI / 2 };
    canvas.setAttribute('aria-label', 'Token share by model, ' + lastRangeLabel + ': ' +
      slices.map(function (s) { return s.model + ' ' + pctLabel(s.tokens, total); }).join(', '));

    var legend = $('model-legend');
    if (legend) {
      legend.textContent = '';
      slices.forEach(function (s, i) {
        var li = el('li', 'lrow');
        li.setAttribute('data-model', s.model);
        li.title = 'Filter to ' + s.model;
        var sw = el('span', 'swatch');
        sw.style.background = sliceFill(slices, i, shadeSteps);
        li.appendChild(sw);
        var logo = brandLogoEl(providerKey(s.model));
        if (logo) li.appendChild(logo);
        li.appendChild(el('span', 'name', s.model));
        li.appendChild(el('span', 'num', fmtStat(s.tokens)));
        li.appendChild(el('span', 'pct', pctLabel(s.tokens, total)));
        legend.appendChild(li);
      });
    }
  }

  function donutSliceAt(x, y) {
    if (!donutGeom || !donutGeom.total) return -1;
    var g = donutGeom, dx = x - g.cx, dy = y - g.cy;
    var r = Math.hypot(dx, dy);
    if (r < g.rIn - 2 || r > g.rOut + 5) return -1;
    var rel = Math.atan2(dy, dx) - g.start;
    while (rel < 0) rel += Math.PI * 2;
    var acc = 0;
    for (var i = 0; i < g.slices.length; i++) {
      acc += (g.slices[i].tokens / g.total) * Math.PI * 2;
      if (rel <= acc) return i;
    }
    return -1;
  }

  function showDonutHover(i, px, py) {
    var g = donutGeom, tip = $('donut-tip'), wrap = $('donut-wrap');
    if (!g || !tip || !wrap || !g.slices[i]) return;
    donutHover = i;
    renderDonut(lastByModel);
    var s = g.slices[i];
    tip.textContent = '';
    tip.appendChild(el('div', 'tv', fmtCompact(s.tokens) + ' tokens'));
    tip.appendChild(el('div', 'tl',
      s.model + ' · ' + pctLabel(s.tokens, g.total) + ' · ' + fmtInt(s.requests) + ' req'));
    tip.hidden = false;
    tip.style.left = Math.max(tip.offsetWidth / 2 + 2,
      Math.min(wrap.clientWidth - tip.offsetWidth / 2 - 2, px)) + 'px';
    tip.style.top = Math.max(2, py - tip.offsetHeight - 10) + 'px';
  }

  function hideDonutHover() {
    if (donutHover < 0) { var tip = $('donut-tip'); if (tip) tip.hidden = true; return; }
    donutHover = -1;
    var tip2 = $('donut-tip');
    if (tip2) tip2.hidden = true;
    renderDonut(lastByModel);
  }

  function wireDonut() {
    var canvas = $('donut'), wrap = $('donut-wrap');
    if (!canvas || !wrap) return;
    canvas.addEventListener('pointermove', function (ev) {
      var rect = canvas.getBoundingClientRect();
      var i = donutSliceAt(ev.clientX - rect.left, ev.clientY - rect.top);
      if (i !== donutHover) showDonutHover(i, ev.clientX - rect.left, ev.clientY - rect.top);
    });
    canvas.addEventListener('pointerleave', hideDonutHover);
    /* slice click drills into the model filter ("other" is a fold, not a model) */
    canvas.addEventListener('click', function (ev) {
      var rect = canvas.getBoundingClientRect();
      var i = donutSliceAt(ev.clientX - rect.left, ev.clientY - rect.top);
      if (i < 0 || !donutGeom) return;
      var model = donutGeom.slices[i].model;
      if (model && model !== 'other') setFilter('model', model);
    });
    wrap.addEventListener('keydown', function (ev) {
      var n = donutGeom ? donutGeom.slices.length : 0;
      if (!n) return;
      if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
        var next = donutHover < 0 ? 0 : (donutHover + (ev.key === 'ArrowRight' ? 1 : -1) + n) % n;
        var g = donutGeom, acc = -Math.PI / 2;
        for (var k = 0; k < next; k++) acc += (g.slices[k].tokens / g.total) * Math.PI * 2;
        var mid = acc + (g.slices[next].tokens / g.total) * Math.PI;
        showDonutHover(next, g.cx + Math.cos(mid) * (g.rOut + 3), g.cy + Math.sin(mid) * (g.rOut + 3));
        ev.preventDefault();
      } else if (ev.key === 'Escape') { hideDonutHover(); }
    });
  }

  /* ---- 24 h column chart on a canvas ---- */

  function niceStep(rough) {
    if (!(rough > 0) || !isFinite(rough)) return 1;
    var mag = Math.pow(10, Math.floor(Math.log10(rough)));
    var factors = [1, 2, 2.5, 5, 10];
    for (var i = 0; i < factors.length; i++) {
      if (rough <= factors[i] * mag) return factors[i] * mag;
    }
    return 10 * mag;
  }

  var chartGeom = null;
  var hoverIdx = -1;
  var chartMode = 'harness';   /* breakdown dimension: 'harness' | 'model' | 'inout' | 'cache' */
  var modelShadeSteps = {};    /* window-wide brand-shade steps for model mode — chartShadeMap */

  function barTopPath(ctx, x, y, w, h) {
    var r = Math.min(3, w / 2, h);
    var x2 = x + w, yb = y + h;
    ctx.beginPath();
    ctx.moveTo(x, yb);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.lineTo(x2 - r, y);
    ctx.quadraticCurveTo(x2, y, x2, y + r);
    ctx.lineTo(x2, yb);
    ctx.closePath();
    ctx.fill();
  }

  /* one stacked-segment entry per member of the active breakdown dimension
     present in the bucket, name ascending — the fixed bottom-to-top stack
     order, independent of segment size.  The in/out and cache dimensions
     aggregate the per-series input/output/cached splits instead of the
     (harness, model) names; cache's uncached = prompt tokens not served
     from the cache (clamped at 0 against odd rows).  The chat and type
     dimensions read the bucket's chat_series instead (tokens per recorded
     chat identity, per-bucket top chats + an "other" fold). */
  function bucketSegments(b) {
    var byKey = {};
    var series = b && b.series ? b.series : [];
    if (chartMode === 'inout' || chartMode === 'cache') {
      var input = 0, output = 0, cached = 0;
      series.forEach(function (s) {
        input += Number(s.input_tokens) || 0;
        output += Number(s.output_tokens) || 0;
        cached += Number(s.cached_tokens) || 0;
      });
      if (chartMode === 'inout') {
        if (input > 0) byKey.input = input;
        if (output > 0) byKey.output = output;
      } else {
        if (cached > 0) byKey.cached = cached;
        var uncached = Math.max(input - cached, 0);
        if (uncached > 0) byKey.uncached = uncached;
      }
    } else if (chartMode === 'chat' || chartMode === 'type') {
      (b && b.chat_series ? b.chat_series : []).forEach(function (s) {
        var t = Number(s.tokens) || 0;
        if (t <= 0) return;
        var k = chartMode === 'chat' ? (s.display || s.key || 'Unknown') : (s.type || 'unknown');
        byKey[k] = (byKey[k] || 0) + t;
      });
    } else {
      series.forEach(function (s) {
        var t = Number(s.tokens) || 0;
        if (t <= 0) return;
        var k = chartMode === 'model' ? (s.model || 'unknown') : (s.caller || UNATTR);
        byKey[k] = (byKey[k] || 0) + t;
      });
    }
    return Object.keys(byKey)
      .map(function (k) { return { name: k, tokens: byKey[k] }; })
      .sort(function (a, k) { return a.name < k.name ? -1 : a.name > k.name ? 1 : 0; });
  }

  /* segment colours for one bar: the harness palette in harness mode; in
     model mode each model's provider brand (PROVIDER_HEXES), shaded toward
     the surface (brandShade — never repeating, so uncapped hourly stacks
     stay told apart) at the model's OWN step from the window-wide step map
     (modelShadeSteps, built by chartShadeMap before the render), so a
     model's shade is the same in every hour column no matter which
     same-provider siblings share the bucket; a model with no provider still
     hashes into MODEL_HEXES, a slot already claimed by an earlier
     (alphabetical) model or a brand's first slice advanced +1 so stacked
     neighbours stay distinguishable; the in/out and cache modes have fixed
     two-slot palettes */
  function segmentHexes(segs) {
    var out = {};
    if (chartMode === 'inout' || chartMode === 'cache') {
      var pal = chartMode === 'inout' ? INOUT_HEXES : CACHE_HEXES;
      segs.forEach(function (s) { if (pal[s.name]) out[s.name] = pal[s.name]; });
      return out;
    }
    if (chartMode === 'harness') {
      segs.forEach(function (s) { out[s.name] = harnessHex(s.name); });
      return out;
    }
    /* model mode brands known providers; chat/type segments and unknown
       models share the hashed neutral palette (chat names are not models —
       a brand there would be a guess) */
    var taken = [];
    var brandTaken = [];  /* brandShade(step 0) is the pure brand hex — keep
                             the neutral palette off those slots too */
    segs.forEach(function (s) {
      var key = chartMode === 'model' ? providerKey(s.name) : null;
      if (key) {
        var step = hasOwn(modelShadeSteps, s.name) ? modelShadeSteps[s.name] : 0;
        out[s.name] = brandShade(PROVIDER_HEXES[key], step);
        if (step === 0) {
          for (var b = 0; b < MODEL_HEXES.length; b++) {
            if (MODEL_HEXES[b].toLowerCase() === PROVIDER_HEXES[key].toLowerCase()) {
              brandTaken[b] = true;
            }
          }
        }
        return;
      }
      var idx = djb2(s.name) % MODEL_HEXES.length;
      for (var bump = 0;
           bump < MODEL_HEXES.length && (taken[idx] || brandTaken[idx]);
           bump++) {
        idx = (idx + 1) % MODEL_HEXES.length;
      }
      taken[idx] = true;
      brandTaken[idx] = true;
      out[s.name] = MODEL_HEXES[idx];
    });
    return out;
  }

  /* Window-wide step map for model mode: brandShadeSteps over every model
     name visible anywhere in the current chart window, cached in
     modelShadeSteps for the render (and the tooltip's segmentHexes call).
     A model's shade must depend only on its own identity, so an hour where
     gpt-4 is absent must not promote gpt-5 from its shaded step to the pure
     brand hex.  The donut builds the same map over its own slice list
     (donutShadeSteps), so the two views agree when the sets match */
  function chartShadeMap(buckets) {
    var seen = {};
    (buckets || []).forEach(function (b) {
      (b && b.series ? b.series : []).forEach(function (s) {
        if ((Number(s.tokens) || 0) > 0) seen[s.model || 'unknown'] = true;
      });
    });
    return brandShadeSteps(Object.keys(seen));
  }

  /* "caller 12.3k (modelA 8.1k · modelB 4.2k)" per harness — textContent
     twin of the server's bucket_series_groups for the sr-only chart table,
     as structured entries ({caller, total, models}) so renderChartTable can
     hang each model's brand logo beside its name; in the in/out and cache
     modes the row collapses to the same two segments the columns stack, as a
     plain {text} line, e.g. "output 12.3k · input 45.6k" */
  function seriesLines(b) {
    if (chartMode === 'inout' || chartMode === 'cache') {
      var segs = bucketSegments(b);
      if (!segs.length) return [];
      var order = chartMode === 'inout' ? ['output', 'input'] : ['cached', 'uncached'];
      var byName = {};
      segs.forEach(function (s) { byName[s.name] = s.tokens; });
      return [{ text: order
        .filter(function (k) { return byName[k] !== undefined; })
        .map(function (k) { return k + ' ' + fmtCompact(byName[k]); })
        .join(' · ') }];
    }
    if (chartMode === 'chat' || chartMode === 'type') {
      var csegs = bucketSegments(b)
        .sort(function (a, c) { return c.tokens - a.tokens || (a.name < c.name ? -1 : 1); });
      if (!csegs.length) return [];
      return [{ text: csegs.slice(0, 8)
        .map(function (s) { return s.name + ' ' + fmtCompact(s.tokens); })
        .join(' · ') }];
    }
    var byCaller = {};
    (b && b.series ? b.series : []).forEach(function (s) {
      var t = Number(s.tokens) || 0;
      if (t <= 0) return;
      var c = s.caller || UNATTR;
      var models = byCaller[c] || (byCaller[c] = {});
      var m = s.model || 'unknown';
      models[m] = (models[m] || 0) + t;
    });
    return Object.keys(byCaller)
      .sort(function (a, c) {
        return modelsTotal(byCaller[c]) - modelsTotal(byCaller[a]) || (a < c ? -1 : a > c ? 1 : 0);
      })
      .map(function (c) {
        var models = byCaller[c];
        return {
          caller: c,
          total: modelsTotal(models),
          models: Object.keys(models)
            .sort(function (a, m) { return models[m] - models[a] || (a < m ? -1 : a > m ? 1 : 0); })
            .map(function (m) { return { name: m, tokens: models[m] }; })
        };
      });
  }

  function modelsTotal(models) {
    var t = 0;
    for (var m in models) t += models[m];
    return t;
  }

  function renderChart(buckets) {
    var canvas = $('chart'), wrap = $('chart-wrap');
    if (!canvas || !wrap || !buckets) return;
    var n = buckets.length;
    var cssW = Math.max(320, wrap.clientWidth || 800);
    var dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(CH.H * dpr);
    canvas.style.width = cssW + 'px';
    canvas.style.height = CH.H + 'px';
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, CH.H);

    var padL = CH.padL, padT = CH.padT;
    var plotW = cssW - padL - CH.padR;
    var plotH = CH.H - padT - CH.padB;
    var baseY = padT + plotH;
    var band = n ? plotW / n : plotW;
    var barW = Math.max(2, Math.min(CH.barMax, band - 6));

    var tokens = buckets.map(function (b) { return Number(b.tokens) || 0; });
    var peak = 0;
    tokens.forEach(function (t) { if (t > peak) peak = t; });

    /* model mode: refresh the window-wide step map before any column reads
       it, so every hour resolves the same per-model shades */
    if (chartMode === 'model') modelShadeSteps = chartShadeMap(buckets);

    /* y gridlines + tick labels */
    ctx.font = '11px ' + 'ui-monospace, Menlo, Consolas, monospace';
    ctx.textBaseline = 'middle';
    if (peak > 0) {
      ctx.strokeStyle = C.grid;
      ctx.fillStyle = C.text;
      ctx.lineWidth = 1;
      var step = niceStep(peak / 4);
      for (var tick = step; tick <= peak; tick += step) {
        var gy = Math.round(baseY - (tick / peak) * plotH) + 0.5;
        ctx.beginPath();
        ctx.moveTo(padL, gy);
        ctx.lineTo(cssW - CH.padR, gy);
        ctx.stroke();
        ctx.textAlign = 'right';
        ctx.fillText(fmtCompact(tick), padL - 8, gy);
      }
    }

    /* baseline */
    ctx.strokeStyle = C.axis;
    ctx.beginPath();
    ctx.moveTo(padL, baseY + 0.5);
    ctx.lineTo(cssW - CH.padR, baseY + 0.5);
    ctx.stroke();

    /* columns + x axis (Sydney time: day name at midnight, time every 4 h;
       day buckets thin their labels to about a dozen) */
    var hourly = !!(buckets[0] && String(buckets[0].label_sydney).indexOf(':') >= 0);
    var labelEvery = Math.max(1, Math.ceil(n / 12));
    var peakIdx = tokens.indexOf(peak);
    buckets.forEach(function (b, i) {
      var v = tokens[i];
      if (v > 0) {
        var h = (v / peak) * plotH;
        var x = padL + i * band + (band - barW) / 2;
        var segs = bucketSegments(b);
        if (segs.length) {
          /* stacked segments of the active dimension, alphabetical from the
             base up; only the topmost keeps the rounded top, lower ones butt
             squarely.  Heights are the segments' own tokens against the
             shared peak, so a dimension whose segments sum below the hour
             total (cache = prompt tokens only) draws a proportionally
             shorter column instead of a rescaled one */
          var hexes = segmentHexes(segs);
          var y = baseY;
          var topY = baseY;
          segs.forEach(function (s, si) {
            var sh = (s.tokens / peak) * plotH;
            var color = hexes[s.name];
            ctx.fillStyle = b.partial ? hexToRgba(color, 0.45) : color;
            if (si === segs.length - 1) {
              barTopPath(ctx, x, y - sh, barW, sh);
            } else {
              /* +0.5 px overlap hides the hairline seam under the segment above */
              ctx.fillRect(x, y - sh, barW, sh + 0.5);
            }
            y -= sh;
            topY = y;
          });
          if (i === hoverIdx) {
            /* barHot treatment for a stacked bar: one translucent lift pass
               over the whole column lightens every segment at once */
            ctx.fillStyle = 'rgba(255, 255, 255, 0.22)';
            barTopPath(ctx, x, topY, barW, baseY - topY);
          }
        } else if (!b.series || !b.series.length) {
          /* legacy shape (no series): one solid column exactly as before */
          ctx.fillStyle = i === hoverIdx ? C.barHot : (b.partial ? C.barPartial : C.bar);
          barTopPath(ctx, x, baseY - h, barW, h);
        }
      }
      var centre = padL + i * band + band / 2;
      var hour = parseInt(b.label_sydney, 10) || 0;
      var showLabel = hourly ? (b.day_sydney || hour % 4 === 0) : (i % labelEvery === 0);
      if (showLabel) {
        ctx.strokeStyle = C.axis;
        ctx.beginPath();
        ctx.moveTo(Math.round(centre) + 0.5, baseY);
        ctx.lineTo(Math.round(centre) + 0.5, baseY + 4);
        ctx.stroke();
        ctx.textAlign = 'center';
        ctx.fillStyle = b.day_sydney ? C.textStrong : C.text;
        ctx.font = (b.day_sydney ? '600 ' : '') + '11px ui-monospace, Menlo, Consolas, monospace';
        ctx.fillText(b.day_sydney || b.label_sydney, centre, baseY + 16);
        ctx.font = '11px ui-monospace, Menlo, Consolas, monospace';
      }
    });

    /* one selective direct label — the peak */
    if (peak > 0 && peakIdx >= 0) {
      ctx.fillStyle = C.peak;
      ctx.textAlign = 'center';
      ctx.font = '600 11px ui-monospace, Menlo, Consolas, monospace';
      ctx.fillText(fmtCompact(peak), padL + peakIdx * band + band / 2, Math.max(8, padT - 10));
    }

    chartGeom = { cssW: cssW, n: n, band: band, padL: padL, peak: peak, plotH: plotH, baseY: baseY, buckets: buckets, tokens: tokens };
    if (hoverIdx >= n) hoverIdx = -1;
    setText('chart-peak', peak > 0
      ? 'peak ' + fmtCompact(peak) + ' · ' + bucketLabel(buckets[peakIdx])
      : 'no traffic in the filtered window');
    renderChartTable(buckets);
  }

  function renderChartTable(buckets) {
    var tbody = $('chart-table-body');
    if (!tbody) return;
    setText('chart-table-series-head', chartMode === 'inout'
      ? 'Input / output tokens'
      : chartMode === 'cache' ? 'Cached / uncached tokens'
      : chartMode === 'chat' ? 'Per-chat tokens'
      : chartMode === 'type' ? 'Per-chat-type tokens'
      : 'Per-harness tokens (per model)');
    tbody.textContent = '';
    buckets.forEach(function (b) {
      var tr = el('tr');
      tr.appendChild(el('td', '', bucketLabel(b)));
      tr.appendChild(el('td', 'num', fmtInt(b.requests)));
      tr.appendChild(el('td', 'num', fmtInt(b.tokens)));
      var td = el('td');
      var lines = seriesLines(b);
      if (lines.length) {
        lines.forEach(function (l) {
          var div = el('div');
          if (l.text) {
            div.textContent = l.text;
          } else {
            /* "caller 12.3k (modelA 8.1k · modelB 4.2k)" with each model's
               provider logo beside its name; the harness name is the display
               label (Hermes IDE · <profile>), models stay raw */
            div.appendChild(document.createTextNode(callerDisplay(l.caller) + ' ' + fmtCompact(l.total) + ' ('));
            l.models.forEach(function (m, mi) {
              if (mi) div.appendChild(document.createTextNode(' · '));
              var logo = brandLogoEl(providerKey(m.name));
              if (logo) div.appendChild(logo);
              div.appendChild(document.createTextNode(m.name + ' ' + fmtCompact(m.tokens)));
            });
            div.appendChild(document.createTextNode(')'));
          }
          td.appendChild(div);
        });
      } else {
        td.textContent = '—';
      }
      tr.appendChild(td);
      tbody.appendChild(tr);
    });
  }

  function showChartHover(i) {
    if (!chartGeom || !chartGeom.n) return;
    var b = chartGeom.buckets[i];
    if (!b) return;
    hoverIdx = i;
    renderChart(buckets());
    var tip = $('chart-tip');
    tip.textContent = '';
    tip.appendChild(el('div', 'tv', fmtCompact(chartGeom.tokens[i]) + ' tokens'));
    tip.appendChild(el('div', 'tl',
      bucketLabel(b) + ' · ' + fmtInt(b.requests) + ' req' + (b.partial ? ' · partial' : '')));
    /* one line per member of the active dimension present in that hour,
       dot coloured like its segment — and in model mode the provider logo
       beside the name (harness names are client apps, not model providers,
       so they stay unbranded) */
    var segs = bucketSegments(b);
    var hexes = segmentHexes(segs);
    segs.forEach(function (seg) {
      var line = el('div', 'tl');
      var dot = el('span', 'tl-dot');
      dot.style.background = hexes[seg.name];
      line.appendChild(dot);
      var logo = chartMode === 'model' ? brandLogoEl(providerKey(seg.name)) : null;
      if (logo) line.appendChild(logo);
      var segName = chartMode === 'harness' ? callerDisplay(seg.name) : seg.name;
      line.appendChild(document.createTextNode(segName + ' · ' + fmtCompact(seg.tokens)));
      tip.appendChild(line);
    });
    tip.hidden = false;
    var centre = chartGeom.padL + i * chartGeom.band + chartGeom.band / 2;
    var h = chartGeom.tokens[i] > 0 ? (chartGeom.tokens[i] / chartGeom.peak) * chartGeom.plotH : 0;
    tip.style.left = Math.max(tip.offsetWidth / 2 + 4,
      Math.min(chartGeom.cssW - tip.offsetWidth / 2 - 4, centre)) + 'px';
    tip.style.top = Math.max(2, chartGeom.baseY - h - tip.offsetHeight - 8) + 'px';
  }

  function hideChartHover() {
    hoverIdx = -1;
    var tip = $('chart-tip');
    if (tip) tip.hidden = true;
    if (chartGeom) renderChart(buckets());
  }

  /* switch the hourly chart's breakdown dimension; the poll re-render reads
     chartMode on every tick, so the choice survives without a reload */
  var MODE_LABELS = {
    harness: 'broken down by harness',
    model: 'broken down by model',
    chat: 'broken down by chat',
    type: 'broken down by chat type',
    inout: 'broken down by input and output tokens',
    cache: 'broken down by cached and uncached prompt tokens'
  };
  var CHART_MODES = ['harness', 'model', 'chat', 'type', 'inout', 'cache'];

  function setChartMode(mode) {
    if (!MODE_LABELS[mode]) return;
    chartMode = mode;
    CHART_MODES.forEach(function (m) {
      var btn = $('mode-' + m);
      if (!btn) return;
      btn.classList.toggle('active', mode === m);
      btn.setAttribute('aria-pressed', mode === m ? 'true' : 'false');
    });
    var wrap = $('chart-wrap');
    if (wrap) wrap.setAttribute('aria-label',
      'Column chart of tokens per bucket over ' + lastRangeLabel + ', ' + MODE_LABELS[mode] +
      '. Use the left and right arrow keys to read values.');
    hoverIdx = -1;
    var tip = $('chart-tip');
    if (tip) tip.hidden = true;
    renderChart(buckets());
  }

  var lastSeries = null;
  function buckets() { return lastSeries || bootBuckets; }

  function wireChart() {
    var canvas = $('chart'), wrap = $('chart-wrap');
    if (!canvas || !wrap) return;
    CHART_MODES.forEach(function (m) {
      var btn = $('mode-' + m);
      if (btn) btn.addEventListener('click', function () { setChartMode(m); });
    });
    canvas.addEventListener('pointermove', function (ev) {
      if (!chartGeom || !chartGeom.n) return;
      var rect = canvas.getBoundingClientRect();
      var i = Math.floor((ev.clientX - rect.left - chartGeom.padL) / chartGeom.band);
      i = Math.max(0, Math.min(chartGeom.n - 1, i));
      if (i !== hoverIdx) showChartHover(i);
    });
    canvas.addEventListener('pointerleave', hideChartHover);
    wrap.addEventListener('keydown', function (ev) {
      var n = chartGeom ? chartGeom.n : 0;
      if (!n) return;
      if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
        var next = hoverIdx < 0 ? n - 1 : Math.max(0, Math.min(n - 1, hoverIdx + (ev.key === 'ArrowRight' ? 1 : -1)));
        showChartHover(next);
        ev.preventDefault();
      } else if (ev.key === 'Escape') { hideChartHover(); }
    });
  }

  /* ---- recent events ---- */

  function badgeTone(e) {
    var status = (e.status_code === null || e.status_code === undefined) ? null : Number(e.status_code);
    if (status !== null && RATE_LIMIT_CODES.indexOf(status) !== -1) return ['crit', String(status)];
    if (e.usage_complete === 'final') return ['good', 'final'];
    return ['none', e.outcome || (status === null ? '—' : String(status))];
  }

  function renderEvents(events) {
    var tbody = $('events-body');
    if (!tbody) return;
    tbody.textContent = '';
    if (!events || !events.length) {
      var tr = el('tr');
      var cell = el('td', 'muted', 'No events match the filters');
      cell.colSpan = 9;
      tr.appendChild(cell);
      tbody.appendChild(tr);
      return;
    }
    events.forEach(function (e) {
      var toneLabel = badgeTone(e);
      var tr = el('tr', toneLabel[0] === 'crit' ? 'row-crit' : '');

      var tdTime = el('td', 'num', e.ts_sydney || '—');
      tdTime.title = e.ts || '';
      tr.appendChild(tdTime);

      var tdCaller = el('td');
      tdCaller.appendChild(el('span', 'chip ' + (e.unattributed ? 'h-unattr' : harnessClass(e.caller)), callerDisplay(e.caller)));
      tr.appendChild(tdCaller);

      var tdChat = el('td', e.chat_key && e.chat_key !== 'unknown' ? '' : 'muted', e.chat_display || 'Unknown');
      tdChat.title = e.chat_key || '';
      tr.appendChild(tdChat);

      var tdModel = el('td');
      tdModel.appendChild(brandBadge(e.model));
      tr.appendChild(tdModel);
      tr.appendChild(el('td', '', e.route || e.path || '—'));
      tr.appendChild(el('td', 'num', fmtOpt(e.prompt_tokens)));
      tr.appendChild(el('td', 'num', fmtOpt(e.completion_tokens)));
      tr.appendChild(el('td', 'num total', fmtOpt(e.total_tokens)));

      var tdBadge = el('td');
      var badge = el('span', 'badge');
      badge.appendChild(el('span', 'dot-s tone-' + toneLabel[0]));
      badge.appendChild(el('span', '', toneLabel[1]));
      tdBadge.appendChild(badge);
      tr.appendChild(tdBadge);
      tbody.appendChild(tr);
    });
  }

  /* ---- error card, live pill, polling ---- */

  function showError(message) {
    $('error-card').classList.add('show');
    $('error-msg').textContent = message;
  }

  function hideError() {
    $('error-card').classList.remove('show');
  }

  var lastOkAt = null;

  function setLive(state) {
    var live = $('live');
    live.classList.remove('ok', 'stale', 'error');
    if (state !== 'connecting') live.classList.add(state);
  }

  function tick() {
    var live = $('live');
    if (live.classList.contains('error')) { setText('tick', 'retrying…'); return; }
    if (lastOkAt === null) { setText('tick', 'connecting…'); return; }
    /* the controls moved ahead of the data on screen: say so, never pass
       the old render off as the new filter state's results */
    if (renderedQuery !== null && renderedQuery !== stateQuery()) {
      setText('tick', 'updating…');
      return;
    }
    var age = Math.max(0, Math.round((Date.now() - lastOkAt) / 1000));
    setText('tick', 'live');
    if (age >= POLL_MS / 1000 + 4) setLive('stale');
  }

  async function fetchJson(url, ctrl) {
    var timer = setTimeout(function () { ctrl.abort(); }, FETCH_TIMEOUT_MS);
    try {
      var res = await fetch(url, { cache: 'no-store', signal: ctrl.signal });
      return await res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  /* Latest-state-wins polling.  Every poll() supersedes the one still in
     flight: its requests are aborted and its response — success OR failure —
     is dropped, so an older filter state's data or error can never render
     under newer chips/URL.  A filter change therefore converges immediately
     (no waiting for the next interval tick), and while the network catches
     up tick() reads 'updating…' until the rendered data matches the
     controls again. */
  var pollGen = 0;
  var inFlightCtrl = null;
  var renderedQuery = null;  /* the stateQuery() the screen currently shows */

  async function poll() {
    var gen = ++pollGen;
    var q = stateQuery();
    if (inFlightCtrl) inFlightCtrl.abort();  // supersede the older request
    var ctrl = new AbortController();
    inFlightCtrl = ctrl;
    try {
      var results = await Promise.all([
        fetchJson('/api/summary?' + q, ctrl),
        fetchJson('/api/timeseries?' + q, ctrl),
        fetchJson('/api/events?limit=' + DASHBOARD_EVENTS + '&' + q, ctrl),
      ]);
      if (gen !== pollGen) return;  // superseded while awaiting — never render stale
      var summary = results[0], series = results[1], events = results[2];

      var errors = [];
      if (summary && summary.error) errors.push(summary.error);
      if (series && series.error) errors.push(series.error);
      if (events && events.error) errors.push(events.error);
      if (errors.length) showError(errors.join(' · '));
      else hideError();

      /* Refetch keeps the frame: previous renders hold until new data lands. */
      if (summary && !summary.error) renderSummary(summary);
      if (series && Array.isArray(series.buckets)) {
        lastSeries = series.buckets;
        if (series.range && series.range.label) {
          lastRangeLabel = series.range.label;
          setText('chart-win', lastRangeLabel + ' · ' +
            (BUCKET_WORDS[series.range.key] || 'buckets') + ' · axis in Sydney time');
        }
        renderChart(lastSeries);
      }
      if (Array.isArray(events)) renderEvents(events);
      renderedQuery = q;

      if (!errors.length) {
        lastOkAt = Date.now();
        setLive('ok');
        setText('updated', fmtSydneyTime(lastOkAt));
      } else {
        setLive('error');
      }
    } catch (err) {
      if (gen !== pollGen) return;  // aborted by a newer state — obsolete failure
      showError('fetch failed: ' + err);
      setLive('error');
    } finally {
      if (gen === pollGen) {
        inFlightCtrl = null;
        tick();
      }
    }
  }

  function renderSummary(summary) {
    if (summary.range && summary.range.label) lastRangeLabel = summary.range.label;
    renderCards(summary);
    renderHarness(summary.per_caller);
    renderDonut(summary.by_model);
    renderChats(summary.chats);
    var facets = summary.facets || {};
    FILTER_KEYS.forEach(function (k) { renderFacetOptions(k, facets[k]); });
    var rl = summary.range && summary.range.label ? summary.range.label : RANGE_LABELS[rangeKey];
    setText('harness-win', rl + ' · bar = share of top harness · click to filter');
    setText('donut-win', rl + ' · by total tokens · top 6 + other');
    setText('chat-win', rl + ' · actual recorded identity · click a row to filter · click a column to sort');
  }

  var bootBuckets = [];
  var boot = {};
  try { boot = JSON.parse($('bootstrap').textContent); } catch (e) { boot = {}; }

  if (boot.error) showError(boot.error);
  applyStateToControls(readStateFromUrl());
  renderChips();
  renderSummary(boot);
  bootBuckets = boot.per_hour || [];
  lastSeries = bootBuckets;
  renderChart(bootBuckets);
  renderEvents(boot.events || []);
  renderedQuery = stateQuery();  /* the first paint already matches the URL */
  wireFilters();
  wireChart();
  wireDonut();
  wireChatSort();
  tick();

  var resizeTimer = null;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      var tip = $('chart-tip');
      if (tip) tip.hidden = true;
      hoverIdx = -1;
      renderChart(buckets());
    }, 150);
  });

  /* refresh immediately when the tab becomes visible again */
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) poll();
  });

  setInterval(poll, POLL_MS);
  setInterval(tick, 1000);
})();
"""


# --------------------------------------------------------------------------
# HTML rendering (initial paint; the browser re-renders from JSON afterwards)
# --------------------------------------------------------------------------

FAVICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E"
    "%3Crect width='16' height='16' rx='3' fill='%230a0d12'/%3E"
    "%3Cpath d='M3.5 11.5v-4M7 11.5v-7M10.5 11.5v-3M13.5 11.5v-5' stroke='%233987e5' "
    "stroke-width='1.8' stroke-linecap='round'/%3E%3C/svg%3E"
)


def render_page(snapshot: dict[str, Any]) -> bytes:
    per_hour = snapshot.get("per_hour") or hour_buckets()
    events = snapshot.get("events") or []
    harness_rows = snapshot.get("per_caller") or []
    range_label = (snapshot.get("range") or {}).get("label") or "last 24 h"
    range_key = (snapshot.get("range") or {}).get("key") or "24h"
    bucket_word = {"24h": "1 h buckets", "7d": "1 d buckets", "30d": "1 d buckets"}.get(
        range_key, "auto-width buckets"
    )

    if snapshot.get("error"):
        error_class = " error-card show"
        error_msg = esc(snapshot["error"])
    else:
        error_class = ""
        error_msg = "The usage ledger is temporarily unavailable; the page will keep retrying."

    tokens = [float(b.get("tokens") or 0) for b in per_hour]
    peak = max(tokens) if tokens else 0.0
    peak_note = (
        "peak " + fmt_compact(peak) + " · " + bucket_label(per_hour[tokens.index(peak)])
        if peak > 0
        else "no traffic in the filtered window"
    )

    bootstrap = json.dumps(snapshot, separators=(",", ":")).replace("</", "<\\/")

    js = (
        JS
        .replace("__POLL_MS__", str(POLL_SECONDS * 1000))
        .replace("__FETCH_TIMEOUT_MS__", str(FETCH_TIMEOUT_MS))
        .replace("__DASHBOARD_EVENTS__", str(DASHBOARD_EVENTS))
        .replace("__HARNESS_COLOR_COUNT__", str(HARNESS_COLOR_COUNT))
        .replace("__DONUT_SIZE__", str(DONUT_SIZE))
    )

    page = (
        """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>AI Usage — Live</title>
<link rel="icon" href=\""""
        + FAVICON
        + """\">
<style>"""
        + CSS
        + """</style>
</head>
<body>
<div class="wrap">

<header class="topbar">
  <div>
    <h1>AI <span class="accent">Usage</span></h1>
    <p class="subtitle">Live LLM token usage &middot; read-only view of the SQLite ledger &middot; times in Australia/Sydney</p>
  </div>
  <div class="live" id="live" role="status">
    <span class="dot" aria-hidden="true"></span>
    <span id="tick">connecting&hellip;</span>
    <span class="sep" aria-hidden="true">&middot;</span>
    <span class="updated" id="updated">&mdash;</span>
  </div>
</header>

<div class="card error-card"""
        + error_class
        + """\" id="error-card">
  <h2>Ledger unavailable</h2>
  <p id="error-msg">"""
        + error_msg
        + """</p>
</div>

"""
        + render_filter_bar(snapshot)
        + """

<section class="cards" aria-label="Filtered totals">"""
        + render_cards(snapshot)
        + """</section>

<div class="mid">
<section class="card chart-card" aria-label="Tokens over time">
  <div class="card-head">
    <h2>Tokens over time</h2>
    <div class="chart-mode" role="group" aria-label="Chart breakdown dimension">
      <button type="button" class="mode-btn active" id="mode-harness" aria-pressed="true">harness</button>
      <button type="button" class="mode-btn" id="mode-model" aria-pressed="false">model</button>
      <button type="button" class="mode-btn" id="mode-chat" aria-pressed="false">chat</button>
      <button type="button" class="mode-btn" id="mode-type" aria-pressed="false">type</button>
      <button type="button" class="mode-btn" id="mode-inout" aria-pressed="false">in/out</button>
      <button type="button" class="mode-btn" id="mode-cache" aria-pressed="false">cache</button>
    </div>
    <div class="card-meta">
      <span class="win" id="chart-peak">"""
        + esc(peak_note)
        + """</span>
      <span class="win" id="chart-win">"""
        + esc(f"{range_label} · {bucket_word} · axis in Sydney time")
        + """</span>
    </div>
  </div>
  <div class="chart-wrap" id="chart-wrap" tabindex="0" role="group" aria-label="Column chart of tokens per bucket over the filtered window, broken down by harness. Use the left and right arrow keys to read values.">
    <canvas id="chart" width="800" height="260"></canvas>
    <div class="tooltip" id="chart-tip" hidden></div>
  </div>
  """
        + chart_data_table(per_hour)
        + """
</section>

<div class="side">
<section class="card" aria-label="Per-harness usage">
  <div class="card-head"><h2>Per-harness usage</h2><span class="win" id="harness-win">"""
        + esc(f"{range_label} · bar = share of top harness · click to filter")
        + """</span></div>
  <div class="scroll-x">
  <table>
    <thead><tr><th scope="col">Harness</th><th scope="col" class="num">Requests</th><th scope="col" class="num">Tokens</th><th scope="col"><span class="sr-only">Share of tokens</span></th></tr></thead>
    """
        + harness_table_body(harness_rows)
        + """
  </table>
  </div>
</section>

"""
        + model_panel_html(snapshot.get("by_model"), range_label)
        + """
</div>
</div>

<section class="card" aria-label="Per-chat usage">
  <div class="card-head">
    <h2>Per-chat usage</h2>
    <span class="win" id="chat-win">"""
        + esc(f"{range_label} · actual recorded identity · click a row to filter · click a column to sort")
        + """</span>
  </div>
  <div class="scroll-x">
  <table id="chat-table">
    <thead><tr>
      <th scope="col">Chat</th>
      <th scope="col" class="num sortable" data-sort="requests" tabindex="0">Requests</th>
      <th scope="col" class="num sortable" data-sort="input_tokens" tabindex="0">In</th>
      <th scope="col" class="num sortable" data-sort="output_tokens" tabindex="0">Out</th>
      <th scope="col" class="num sortable" data-sort="cached_tokens" tabindex="0">Cached</th>
      <th scope="col" class="num sortable sorted-desc" data-sort="total_tokens" tabindex="0">Total</th>
    </tr></thead>
    """
        + chat_table_body(snapshot.get("chats"))
        + """
  </table>
  </div>
</section>

<section class="card" aria-label="Recent events">
  <div class="card-head">
    <h2>Recent events</h2>
    <span class="win">last """
        + str(len(events))
        + """ matching &middot; newest first &middot; <span class="dot-s tone-good"></span> final &middot; <span class="dot-s tone-crit"></span> 401/429 &middot; <span class="dot-s tone-none"></span> other</span>
  </div>
  <div class="scroll-x">
  <table class="events">
    <thead>
      <tr>
        <th scope="col">Time</th><th scope="col">Harness</th><th scope="col">Chat</th><th scope="col">Model</th><th scope="col">Route</th>
        <th scope="col" class="num">In</th><th scope="col" class="num">Out</th><th scope="col" class="num">Total</th><th scope="col">Outcome</th>
      </tr>
    </thead>
    <tbody id="events-body">"""
        + events_table_body(events)
        + """</tbody>
  </table>
  </div>
</section>

<footer>
  Polls /api/summary, /api/timeseries and /api/events every """
        + str(POLL_SECONDS)
        + """ s with the filters in the page URL &middot; SQLite opened read-only (mode=ro) &middot; LAN only, no authentication.
  <noscript>Live updates need JavaScript &mdash; showing the snapshot from page load.</noscript>
</footer>
</div>

<script type="application/json" id="bootstrap">"""
        + bootstrap
        + """</script>
<script>"""
        + js
        + """</script>
</body>
</html>"""
    )
    return page.encode("utf-8")


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

class UsageProxyHandler(BaseHTTPRequestHandler):
    db_path: str = DEFAULT_DB
    server_version = "usage-proxy-webui/2"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        """Access logs are suppressed — a LAN dashboard must not spam stdout."""

    def log_error(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _events_body(self, parsed) -> tuple[int, str, bytes]:
        qs = parse_qs(parsed.query)
        filters = parse_filters(qs)
        limit = API_EVENTS_DEFAULT
        if "limit" in qs:
            try:
                limit = min(max(1, int(qs["limit"][0])), API_EVENTS_MAX)
            except (ValueError, IndexError):
                return 400, "text/plain; charset=utf-8", b"invalid limit\n"

        conn: sqlite3.Connection | None = None
        try:
            conn = open_db_readonly(self.db_path)
            has_chat = CHAT_COLUMNS <= ledger_columns(conn)
            where, params = where_clause(filters, has_chat)
            events = query_events(conn, where, params, has_chat, limit)
        except (sqlite3.Error, OSError) as exc:
            # Soft failure: HTTP 200 with an error payload so pollers keep polling.
            body: Any = {"error": f"ledger unavailable: {exc}"}
            return 200, "application/json; charset=utf-8", json.dumps(body, indent=2).encode("utf-8")
        finally:
            if conn is not None:
                conn.close()

        if "envelope" in qs and qs["envelope"][0] == "object":
            payload: Any = {"events": events, "limit": limit}  # pre-redesign shape
        else:
            payload = events
        return 200, "application/json; charset=utf-8", json.dumps(payload, indent=2).encode("utf-8")

    def _timeseries_body(self, parsed) -> tuple[int, str, bytes]:
        filters = parse_filters(parse_qs(parsed.query))
        conn: sqlite3.Connection | None = None
        try:
            conn = open_db_readonly(self.db_path)
            now = utc_now()  # one instant for the cutoff and the bucket plan
            conn.execute("BEGIN")  # consistent read while the proxy writes
            has_chat = CHAT_COLUMNS <= ledger_columns(conn)
            where, params = where_clause(filters, has_chat, now=now)
            first_ts = query_window(conn, where, params)["first_ts"]
            buckets = query_timeseries(
                conn, where, params, bucket_plan(filters.range_key, first_ts, now=now), has_chat
            )
            conn.commit()  # read-only: ends the snapshot; nothing was written
            payload: Any = {
                "buckets": buckets,
                "range": {"key": filters.range_key, "label": RANGE_LABELS[filters.range_key]},
            }
        except (sqlite3.Error, OSError) as exc:
            body: Any = {"error": f"ledger unavailable: {exc}"}
            return 200, "application/json; charset=utf-8", json.dumps(body, indent=2).encode("utf-8")
        finally:
            if conn is not None:
                conn.close()
        return 200, "application/json; charset=utf-8", json.dumps(payload, indent=2).encode("utf-8")

    def _body_for(self, parsed) -> tuple[int, str, bytes]:
        route = parsed.path
        filters = parse_filters(parse_qs(parsed.query))
        if route == "/":
            # Always render the shell at HTTP 200 — even when the ledger is
            # unreachable — so the browser keeps a page that can keep polling.
            snapshot = fetch_snapshot(self.db_path, filters, DASHBOARD_EVENTS)
            return 200, "text/html; charset=utf-8", render_page(snapshot)
        if route == "/api/summary":
            snapshot = fetch_snapshot(self.db_path, filters, event_limit=0)
            payload = {k: v for k, v in snapshot.items() if k != "events"}
            return 200, "application/json; charset=utf-8", json.dumps(payload, indent=2).encode("utf-8")
        if route == "/api/timeseries":
            return self._timeseries_body(parsed)
        if route == "/api/events":
            return self._events_body(parsed)
        return 404, "text/plain; charset=utf-8", b"not found\n"

    def do_GET(self) -> None:
        try:
            status, content_type, body = self._body_for(urlparse(self.path))
        except Exception as exc:  # a handler must never kill the keep-alive connection
            self.log_error("request failed: %r", exc)
            try:
                self._send(500, "application/json; charset=utf-8", b'{"error": "internal error"}\n')
            except OSError:
                pass
            return
        try:
            self._send(status, content_type, body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client hung up mid-response; nothing to do

    def do_HEAD(self) -> None:
        try:
            status, content_type, body = self._body_for(urlparse(self.path))
        except Exception as exc:
            self.log_error("request failed: %r", exc)
            try:
                self.send_error(500)
            except OSError:
                pass
            return
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()


def main() -> None:
    parser = argparse.ArgumentParser(description="Live read-only usage-proxy ledger dashboard")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Bind host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Bind port (default: {DEFAULT_PORT})")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite ledger path, opened read-only (default: {DEFAULT_DB})")
    args = parser.parse_args()

    UsageProxyHandler.db_path = args.db
    server = ThreadingHTTPServer((args.host, args.port), UsageProxyHandler)
    server.daemon_threads = True
    sys.stderr.write(f"usage-proxy-webui serving on http://{args.host}:{args.port}  db={args.db}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nShutting down.\n")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
