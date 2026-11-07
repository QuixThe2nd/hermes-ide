"""Present-progressive normalisation for tool-progress labels.

Models author the ``description`` argument of a tool call imperatively
("Inspect dedup tests"), but the label is shown while the call is running, so
the user should read the present progressive ("Inspecting dedup tests").
``progressive_phrase`` rewrites ONLY the first word, and only when it is a
curated common imperative verb — everything else (non-verb openers, acronyms,
already-progressive words, digits/symbols) passes through untouched.

Pure stdlib, table-driven; consumed by ``agent.display``.
"""

from __future__ import annotations

import re

# Verbs whose imperative → participle spelling the regular rules get wrong.
# Consonant doubling (English doubles only stressed final syllables, so it is
# unsuited to a general rule — "running"/"logging" but "visiting"/"editing"),
# drop/keep 'e', and ie→y all live here.
_PROGRESSIVE_IRREGULAR: dict[str, str] = {
    "be": "being",
    "cut": "cutting",
    "debug": "debugging",
    "embed": "embedding",
    "flag": "flagging",
    "get": "getting",
    "grep": "grepping",
    "lie": "lying",
    "log": "logging",
    "map": "mapping",
    "pin": "pinning",
    "plan": "planning",
    "quit": "quitting",
    "refer": "referring",
    "remap": "remapping",
    "rescan": "rescanning",
    "reset": "resetting",
    "run": "running",
    "scan": "scanning",
    "see": "seeing",
    "set": "setting",
    "skip": "skipping",
    "spot": "spotting",
    "split": "splitting",
    "stop": "stopping",
    "strip": "stripping",
    "submit": "submitting",
    "swap": "swapping",
    "tag": "tagging",
    "tie": "tying",
    "transfer": "transferring",
    "trim": "trimming",
    "unpin": "unpinning",
    "unset": "unsetting",
}

# Curated imperative verbs we are willing to rewrite. Conservative by design:
# a false positive mangles the user-facing label, a miss only leaves an
# imperative verb readable.
_PROGRESSIVE_VERBS: frozenset[str] = frozenset({
    "accept", "access", "add", "adjust", "aggregate", "allow", "analyse", "analyze",
    "be", "debug", "get", "identify", "lie", "make", "refer", "see", "tie", "try",
    "apply", "archive", "assess", "assign", "attach", "audit", "await", "backfill",
    "backport", "backup", "benchmark", "bind", "branch", "break", "browse", "build",
    "bump", "cache", "calculate", "cancel", "capture", "cast", "catch", "change",
    "check", "clean", "clear", "clone", "close", "collect",
    "combine", "compare", "compile", "complete", "compress", "compute", "confirm",
    "connect", "construct", "consult", "contain", "convert", "copy", "count",
    "create", "curate", "cut", "decompress", "deduplicate", "delete", "deploy",
    "deprecate", "describe", "detect", "determine", "diff", "disable", "discard",
    "discover", "dispatch", "display", "divide", "document", "download", "draft",
    "dump", "duplicate", "echo", "edit", "embed", "enable", "encode", "encrypt",
    "enforce", "ensure", "enter", "enumerate", "evaluate", "examine", "exclude",
    "execute", "expand", "expect", "explain", "explore", "export", "extend",
    "extract", "fetch", "fill", "filter", "find", "finish", "fix", "flag", "flatten",
    "flush", "follow", "format", "forward", "gather", "generate", "give",
    "grant", "grep", "group", "handle", "hash", "inspect", "install", "instantiate",
    "instrument", "interpret", "introduce", "invalidate", "iterate", "join", "kill",
    "label", "launch", "learn", "list", "load", "localize", "localise", "locate",
    "lock", "log", "look", "loop", "maintain", "map", "mark", "match", "measure",
    "merge", "migrate", "mirror", "mock", "monitor", "mount", "move", "name",
    "navigate", "normalise", "normalize", "note", "observe", "obtain", "open",
    "optimise", "optimize", "organise", "organize", "override", "package", "parse",
    "patch", "pause", "perform", "pin", "pipe", "pivot", "plan", "populate",
    "post", "prepare", "present", "preserve", "print", "process", "produce",
    "profile", "promote", "prompt", "prove", "provide", "publish", "pull", "purge",
    "push", "query", "queue", "quit", "raise", "read", "reap", "rebase", "rebuild",
    "recalculate", "receive", "recheck", "record", "recover", "recreate", "redo",
    "reduce", "refactor", "refresh", "regenerate", "register", "reinstall",
    "reinstate", "reload", "remap", "remove", "rename", "render",
    "reorder", "reorganise", "reorganize", "repair", "repeat", "replace", "reply",
    "report", "reproduce", "request", "require", "rescan", "reset", "resize",
    "resolve", "respond", "restart", "restore", "restrict", "resume", "retrieve",
    "retry", "reuse", "review", "revise", "revoke", "rotate", "route", "run",
    "sample", "sanitise", "sanitize", "save", "scan", "schedule", "scope", "scrape",
    "search", "seed", "select", "send", "separate", "serve", "set", "share",
    "show", "shrink", "shuffle", "sign", "simplify", "skip", "sleep", "sort",
    "spawn", "split", "spot", "stage", "start", "stash", "stop", "store",
    "stream", "strip", "study", "submit", "subscribe", "summarise", "summarize",
    "swap", "sync", "synchronise", "synchronize", "tag", "tail", "test", "thread",
    "time", "toggle", "trace", "track", "train", "transfer", "transform", "translate",
    "trim", "truncate", "tune", "unbind", "uncomment", "undo", "uninstall",
    "unlock", "unpack", "unpin", "unset", "update", "upgrade", "upload", "use",
    "validate", "verify", "view", "visit", "wait", "walk", "watch",
    "wire", "write", "yield",
})

_FIRST_WORD_RE = re.compile(r"^(\S+)(\s+)(.*)$", re.DOTALL)


def _participle(word: str) -> str:
    lowered = word.lower()
    if lowered in _PROGRESSIVE_IRREGULAR:
        stem = _PROGRESSIVE_IRREGULAR[lowered]
    elif lowered.endswith("ie"):
        stem = lowered[:-2] + "ying"
    elif lowered.endswith("e") and not lowered.endswith("ee"):
        stem = lowered[:-1] + "ing"
    else:
        stem = lowered + "ing"
    # Match the opener's capitalisation: "Check" → "Checking", "check" → "checking"
    # (the describe cap layer capitalises the final label anyway).
    return stem[0].upper() + stem[1:] if word[0].isupper() else stem


# Inverse generated by the forward spelling rules, including every irregular.
_BASE_VERBS = {_participle(verb).lower(): verb for verb in _PROGRESSIVE_VERBS}


def base_verb_phrase(text: str) -> str:
    """Rewrite a curated leading present participle to its base verb for an infinitive.

    Unknown words, acronyms, non-words, and the remainder of the phrase pass through
    unchanged. A converted opener is lowercase because it normally follows ``to``.
    """
    if not text:
        return text
    match = _FIRST_WORD_RE.match(text)
    if match:
        word, sep, rest = match.group(1), match.group(2), match.group(3)
    else:
        word, sep, rest = text, "", ""
    if not word[0].isalpha() or word.isupper():
        return text
    base = _BASE_VERBS.get(word.lower())
    return base + sep + rest if base is not None else text


def progressive_phrase(text: str) -> str:
    """Return *text* with a bare imperative first verb rewritten to present progressive.

    Only the first word is considered, and only when (case-insensitively) it is a
    curated imperative verb that is not already a participle. Acronyms
    (all-caps), words starting with a non-letter, unknown words, and everything
    after the first word pass through byte-for-byte.
    """
    if not text:
        return text
    match = _FIRST_WORD_RE.match(text)
    if match:
        word, sep, rest = match.group(1), match.group(2), match.group(3)
    else:
        word, sep, rest = text, "", ""
    lowered = word.lower()
    if (
        not word[0].isalpha()
        or word.isupper()  # acronyms: "API", "PR"
        or lowered not in _PROGRESSIVE_VERBS
    ):
        return text
    return _participle(word) + sep + rest
