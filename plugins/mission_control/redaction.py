"""Secret redaction and transcript text cleanup for mission-control.

Moved byte-identical from server.py: the preview sanitize/clean/clamp
helpers, the secret-detection regexes and redaction passes, and the
defensive whole-document tool-call parser.
"""

import json
import re
from urllib.parse import unquote

# Discord envelopes wrap the real text in bookkeeping: a leading
# "[Triggering message id: …]" block (the handle for reply/react/pin)
# and a "[username]" sender prefix. Previews show the message itself.
TRIGGERING_BLOCK_RE = re.compile(r"^\s*\[Triggering message id:[^\]]*\]\s*")
SENDER_PREFIX_RE = re.compile(r"^\[[^\]\s]+\]\s+")
BLANK_RUN_RE = re.compile(r"(?:[ \t]*\n){3,}")

# Lone UTF-16 surrogates can reach Python strings only through escaped
# JSON ("\ud800") — SQLite TEXT and utf-8 decoding cannot produce one.
# They are unencodable: any response write (utf-8) would raise
# UnicodeEncodeError, so every display-bound string replaces them with
# U+FFFD. Properly paired astral characters are single code points in a
# Python str and never match.
LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def sanitize_text(text):
    """One string -> the same string with lone surrogates replaced by
    U+FFFD, so nothing downstream (HTML escape, JSON body, utf-8 write)
    can raise UnicodeEncodeError."""
    if not text:
        return text
    if not LONE_SURROGATE_RE.search(text):
        return text
    return LONE_SURROGATE_RE.sub("\N{REPLACEMENT CHARACTER}", text)


def clean_preview(text):
    """Raw message text -> display preview: envelope stripped, extra
    blank lines collapsed, ends trimmed, lone surrogates replaced.
    Newlines survive (the preview cell renders pre-wrap)."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = TRIGGERING_BLOCK_RE.sub("", text, count=1)
    text = SENDER_PREFIX_RE.sub("", text, count=1)
    text = BLANK_RUN_RE.sub("\n\n", text)
    return sanitize_text(text).strip()


def clamp_label(text):
    """Raw text -> one-line sub-agent row label: cleaned with the same
    helpers as previews, all whitespace collapsed, capped hard so a
    whole prompt can never dump into the section (CSS clamps the visual
    to two lines on top of this)."""
    text = " ".join(clean_preview(text).split())
    if len(text) > SUBAGENT_LABEL_CHARS:
        text = text[:SUBAGENT_LABEL_CHARS - 1].rstrip() \
            + "\N{HORIZONTAL ELLIPSIS}"
    return text
# ---- live tool activity (/s/<profile>/<id> + /feed) ------------------
# A tool call must be visible after its assistant tool_calls carrier is
# persisted and before its matching tool result row exists. Everything
# below is defensive by design: carriers are legacy JSON of several
# Hermes generations, so nothing here may ever raise into a page or a
# feed poll, and nothing unbounded may ever reach the response.

# Keys whose values are redacted at any depth of a parsed argument
# object (matched as substrings of the lowercased key, so
# "github_token" or "PASSWORD" both redact), plus the same words in
# textual "key=value" / "key: value" forms inside string values. Each
# keyword's own underscore is [_-]? in the patterns, so the hyphenated
# spelling ("x-api-key", "access-key") is the same keyword.
SECRET_KEY_WORDS = ("password", "passwd", "passphrase", "secret", "token",
                    "api_key", "apikey", "access_key", "auth", "authorization",
                    "cookie", "credential")
SECRET_KEY_RE = re.compile(
    "(?i)(?:%s)" % "|".join(w.replace("_", "[_-]?")
                            for w in SECRET_KEY_WORDS))
# The same words as a name's required SUFFIX — exactly the judgment the
# assignment pass below applies to a raw name, factored out so a
# percent-decoded parameter name is judged by the same rule.
SECRET_KEY_SUFFIX_RE = re.compile(
    "(?i)(?:%s)$" % "|".join(w.replace("_", "[_-]?")
                             for w in SECRET_KEY_WORDS))
REDACTED = "[REDACTED]"

# The one bounded redaction boundary every UI-exposed tool argument and
# tool-result detail passes through, in order (each pattern's output is
# opaque to the ones after it):
#
# 1. Authorization header values — the keyword plus its whole
#    credential, scheme word included in the match so "Authorization:
#    Bearer <token>" can never leave "<token>" (or mask only "Bearer")
#    behind. Handles quoting and the common delimiter characters.
# 2. Standalone bearer tokens with no keyword in front of them.
# 3. Credential-bearing URLs and DB URIs — scheme://user:pass@… keeps
#    the scheme and host shape, replaces the whole userinfo.
# 4. Key/value secret assignments, quoted or bare — the FULL value is
#    replaced (a quoted phrase like password: "hunter two" is one
#    match, not a masked first word plus a leaked remainder).
# 5. Percent-encoded parameter names — "Api%5FKey=…",
#    "access%2Dtoken=…", "%41pi%5fkey=…" — spell the keyword with
#    escapes the raw-name pass cannot see, so a name is judged again
#    after exactly one safe percent-decoding pass of the NAME ONLY;
#    the value is replaced whole, still exactly as written.
AUTH_VALUE_RE = re.compile(
    r"(?i)\b(authorization|proxy[-_]authorization)\b(\s*[=:]\s*)"
    r"(?:bearer|basic|digest|token|oauth|negotiate|hmac|mutual)[ \t]+"
    r"[^\s,;\"'<>)]+"
    r"|\b(authorization|proxy[-_]authorization)\b(\s*[=:]\s*)"
    r"[^\s,;\"'<>)]+")
BEARER_TOKEN_RE = re.compile(
    r"(?i)\b(bearer)[ \t]+[A-Za-z0-9._~+/\-=]{8,}")
URL_USERINFO_RE = re.compile(
    r"(?i)\b(?:[a-z][a-z0-9+.\-]*)://(?:[^\s/@:\[\"']+)?(?::[^\s/@\[\"']*)?@")
# Compound names count too ("github_token: …", "x-api-key: …",
# "SESSION_SECRET=…"), so the keyword may carry a run of identifier
# characters — underscores AND hyphens. The value may not be the
# redaction marker itself (keeps the pass idempotent and a following
# quote alive).
SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_-]*(?:%s))[\"']?([ \t]*[:=][ \t]*)"
    r"(?!\[REDACTED\])(\"[^\"]*\"|'[^']*'|[^\s,;&>]+)"
    % "|".join(w.replace("_", "[_-]?") for w in SECRET_KEY_WORDS))

# A parameter NAME that carries at least one percent escape, its "="
# and the value forms the assignment pass would have consumed — quoted
# phrase or bare run, exactly the same value charset, so pass 5's
# replacements match pass 4's shape. Only percent-bearing names ever
# match, so ordinary text ("status=open", "a=b", prose) is not even a
# candidate here, and a clean name like "redirect_uri" never blankets
# the nested parameters its value may hold.
ENCODED_PARAM_RE = re.compile(
    r"(?<![A-Za-z0-9_.~%-])([A-Za-z0-9_.~%-]*%[A-Za-z0-9_.~%-]*)="
    r"(\"[^\"]*\"|'[^']*'|[^\s,;&>]+)")


def _param_name_is_sensitive(name):
    """True when a URL/query/form parameter name is secret-bearing.

    The raw name counts exactly when the assignment pass above would
    have taken it (a secret keyword as the name's suffix). Otherwise
    the name gets ONE safe percent-decoding pass — the name only,
    never any value — and is judged again by the same suffix rule, so
    "Api%5FKey", "access%2Dtoken" and "%41pi%5fkey" all count while
    "redirect%5Furi" and "token%5Fcount" do not. Malformed escapes
    fail closed: whatever still holds an escape (or an undecodable
    byte) after that one pass is judged by the broader substring test
    AND by the most lenient further reading — bad escapes simply
    dropped, "%%5Fapi%%5Fkey" reading as "__api_key" — so a name that
    MIGHT decode to a keyword under any decoder redacts. Even a
    decoder error counts as sensitive. Never raises.
    """
    if SECRET_KEY_SUFFIX_RE.search(name):
        return True
    try:
        decoded = unquote(name)
    except Exception:
        return True
    if SECRET_KEY_SUFFIX_RE.search(decoded):
        return True
    if "%" in decoded or "\N{REPLACEMENT CHARACTER}" in decoded:
        if SECRET_KEY_RE.search(decoded):
            return True
        lenient = decoded.replace("%", "").replace(
            "\N{REPLACEMENT CHARACTER}", "")
        return bool(SECRET_KEY_SUFFIX_RE.search(lenient))
    return False


def _redact_encoded_params(text, depth=0):
    """Mask the values of percent-encoded sensitive parameter names.

    Pass 5 of the redaction boundary: each candidate name=value pair is
    judged by _param_name_is_sensitive(); a sensitive name keeps its
    encoded spelling and loses its whole value — quoted forms included
    — to REDACTED, with nothing decoded, normalized or reordered in the
    original text. A benign encoded name's value may itself carry
    nested parameters (a redirect target URL), so that value is
    re-scanned on its own, strictly shorter each level and capped at a
    few levels, instead of being blanket-consumed and hiding them.
    """
    if depth >= 4 or "%" not in text:
        return text
    out, last = [], 0
    for m in ENCODED_PARAM_RE.finditer(text):
        value = m.group(2)
        if _param_name_is_sensitive(m.group(1)):
            fixed = REDACTED
        else:
            fixed = _redact_encoded_params(value, depth + 1)
        if fixed == value:
            continue
        out.append(text[last:m.start(2)])
        out.append(fixed)
        last = m.end()
    if not out:
        return text
    out.append(text[last:])
    return "".join(out)


def redact_secret_text(text):
    """One string -> the same string with credential-shaped content
    masked, value-complete rather than first-token-only.

    Runs on every string that can reach a tool argument summary, a tool
    detail block, or a Discord error line: Authorization headers
    (keyword + scheme + full credential), bare bearer tokens,
    user:password URL/DB-URI userinfo, password/token/api-key style
    assignments with their full quoted or bare value, and the same
    assignments hiding behind a percent-encoded parameter name. Useful
    non-secret text (paths, commands, ordinary words) passes through.
    """
    if not text:
        return text

    def _auth_sub(m):
        # group layout alternates (keyword, sep) between the two arms
        keyword = m.group(1) or m.group(3)
        sep = m.group(2) or m.group(4)
        # The scheme word and the credential go together — keeping the
        # scheme would only hand a later pass a bare "Bearer <masked>"
        # fragment. One replacement, whole value.
        return keyword + sep + REDACTED

    text = AUTH_VALUE_RE.sub(_auth_sub, text)
    text = BEARER_TOKEN_RE.sub(
        lambda m: m.group(1) + " " + REDACTED, text)
    text = URL_USERINFO_RE.sub(
        lambda m: m.group(0).split("://", 1)[0] + "://" + REDACTED + "@",
        text)
    text = SECRET_ASSIGN_RE.sub(
        lambda m: m.group(1) + m.group(2) + REDACTED, text)
    text = _redact_encoded_params(text)
    return text


def redact_secrets(value):
    """Recursively redact secret-bearing keys of a parsed JSON value;
    lists and nested dicts are walked, everything else passes through."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = str(k)
            if SECRET_KEY_RE.search(key):
                out[key] = REDACTED
            else:
                out[key] = redact_secrets(v)
        return out
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    if isinstance(value, str):
        return redact_secret_text(value)
    return value


def parse_tool_calls(raw):
    """Bounded tool_calls JSON -> [{name, args, ids}], never raising.

    Accepts both the OpenAI-ish shape ({"function": {"name",
    "arguments"}}) and flatter legacy shapes ({"name", "arguments"}).
    arguments may be a JSON string or an already-parsed object; ids
    collects every non-empty id-ish field a tool result could echo back
    — canonical first (call_id, the Responses pairing key the executor
    and the spawn receipts use, matching coalesce_tool_call_id), then
    the legacy transport aliases (id, response_item_id) — so a caller
    taking the first id correlates the way the runtime does. Malformed
    input yields [] — the page and the feed simply see no pending calls
    from it.
    """
    if not raw or not isinstance(raw, str):
        return []
    try:
        arr = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(arr, list):
        return []
    calls = []
    for c in arr[:TOOL_CALLS_MAX]:
        if not isinstance(c, dict):
            continue
        fn = c.get("function")
        name = ""
        args_raw = ""
        if isinstance(fn, dict):
            name = fn.get("name") or c.get("name") or ""
            a = fn.get("arguments", c.get("arguments"))
        else:
            name = c.get("name") or ""
            a = c.get("arguments")
        if isinstance(a, str):
            args_raw = a
        elif a is None:
            args_raw = ""
        else:
            try:
                args_raw = json.dumps(a)
            except (TypeError, ValueError):
                args_raw = ""
        if not isinstance(name, str):
            name = ""
        ids = []
        for key in ("call_id", "id", "response_item_id"):
            v = c.get(key)
            if isinstance(v, str) and v:
                ids.append(v)
        calls.append({"name": name, "args": args_raw, "ids": ids})
    return calls


# ---- inline delegate dispatch cards (/s/<profile>/<id>) --------------
# The real spawn-shaped delegate tool call is the chronological
# authority for a dispatch: one card renders at the carrier's position
# (replacing the generic delegate_* tool row), links into a child
# transcript only when a child session or async delegation proves the
# route, and — for delegate_claude_agent — opens that exact run's live
# viewer page through a validated spawn receipt. Control calls
# (delegate_agent with action list/steer/stop) are not dispatches and
# never produce cards.


# Defensive parsing bounds: one carrier rarely holds more than a
# handful of calls, and summaries are display-only.
TOOL_CALLS_MAX = 12
ARGS_SUMMARY_CHARS = 160
ARGS_VALUE_CHARS = 48


# Belt-and-braces over the SQL substr: the Python clamp keeps any one
# row's markup small no matter where the label came from.
SUBAGENT_LABEL_CHARS = 200


# The delegate tools whose spawn-shaped calls become dispatch cards.
DELEGATE_SPAWN_NAMES = ("delegate_agent", "delegate_task",
                        "delegate_claude_agent", "delegate_cursor_agent")
