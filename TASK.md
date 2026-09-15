# Task: attribution gate + UA probe attribution in llm_usage_proxy

Branch: `feat/llm-usage-proxy` (current HEAD 52189558d2). Work in THIS clone. Do not touch /root/hermes-agent (live tree). Do not commit TASK.md, FINISH.md, or RESUME.md.

## Invariant

After this change, in manage-keys mode with ≥1 caller token configured, the ledger can no longer receive rows with an empty caller label from clients that identify via the two known mechanisms (caller token OR X-Usage-Caller header OR Claude Code User-Agent). Unlabeled traffic is refused before the upstream call.

## Context (verified on the host, do not re-derive)

- `plugins/llm_usage_proxy/server.py` (uncommitted-in-live-tree feature, committed here on the branch):
  - `CALLER_LABEL_HEADER = "X-Usage-Caller"` (line ~115), `sanitize_caller_label()` (line ~874) accepts `[A-Za-z0-9._:-]{1,64}`.
  - `_authenticate_caller()` (line ~1420): when `key_store.caller_count() == 0` returns `(None, None)` = accept-unattributed. This is the hole.
  - `_proxy()` (line ~1473): caller token auth happens under `if key_store is not None:` (line ~1506); `label = self._presented_caller_label()` at line ~1503; `if caller is None: caller = label` at line ~1541 — a `None` caller reaching `store.insert` is the empty ledger column.
  - `_forward_headers()` (line ~1380): strips CALLER_TOKEN_HEADER and CALLER_LABEL_HEADER before forwarding; the User-Agent header currently passes through untouched — keep it that way.
- Claude Code (all delegate runs via /root/.local/bin/claude-glm and claude-kimi) sends `User-Agent: claude-cli/<version> (external, cli)` on its connectivity probes (e.g. GET /api/hello, HEAD variants). These probes bypass the Anthropic SDK client so the wrapper's `ANTHROPIC_CUSTOM_HEADERS` never applies to them. Verified live: they are the only unattributed traffic hitting the proxy today.
- Hermes in-process gateway traffic always carries `X-Usage-Caller: hermes` (injected in `hermes_cli/llm_usage_routes.py::_proxied_request`).

## Required changes (only in `plugins/llm_usage_proxy/server.py` + tests + README.md)

1. **UA-based label fallback** — new module-level helper, e.g.:

```python
UA_LABEL_RE = re.compile(r"claude-cli/")  # prefix match on the UA token

def caller_label_from_user_agent(user_agent: Optional[str]) -> Optional[str]:
    """Attribute the well-known CLI probe UA to its harness, else None."""
    if not user_agent:
        return None
    return "claude-code" if user_agent.strip().startswith("claude-cli/") else None
```

2. **In `_proxy()`**: resolve the row's caller as:
   `token caller` (existing auth) → else presented `X-Usage-Caller` label → else UA fallback → else None. Order matters: token caller wins over header label; header label wins over UA.

3. **The gate** — inside `_proxy()`, when `self.manage_keys` AND the key store has ≥1 caller token AND the resolved caller is None:
   - respond `401` with JSON body `{"error": "unattributed request: send a caller token, an 'X-Usage-Caller: <label>' header, or a known harness User-Agent"}`,
   - record the rejection row WITH a caller — use the sentinel label `unattributed` (it passes `sanitize_caller_label`) — via the existing `_record_rejection(..., caller="unattributed")`, outcome `rejected`, status 401. The row must never carry an empty caller.
   - Do NOT forward anything upstream. Do not close the connection abruptly; normal JSON response path is fine.
   - The gate must sit AFTER body read (so the ledger row keeps the model) and BEFORE `_open_and_send`. Reuse the existing `caller`/`label` locals; do not re-parse headers.

4. **CORRECTION (applies to item 3's gate condition):** gate on `self.manage_keys` ALONE — do NOT require `key_store.caller_count() >= 1`. Reason: minting any caller token makes the existing `_authenticate_caller` refuse the gateway's own tokenless label-only traffic (401 'missing caller token'), so a token-count condition would leave the gate permanently inert on the live deployment. Managed mode ⇒ attribution required. Zero-token mode (not manage-keys) unchanged: accepted as today.

5. **`_record_rejection` gate rows must pass the caller through** — the existing implementation already accepts `caller=`; just ensure the gate uses it.

6. **README.md**: find the section describing the usage proxy (search "llm_usage_proxy" or "usage-proxy"; if none exists, it goes in the Plugins/observability area next to where other in-tree plugins are described — keep it one paragraph, do not create a new top-level section). Add one sentence: caller attribution is enforced when caller tokens exist — requests presenting neither a token, an X-Usage-Caller label, nor a recognized harness User-Agent are refused with 401 and never reach the upstream provider.

## Tests (tests/plugins/llm_usage_proxy/ — add to the existing caller test module `test_caller_label.py`)

Follow the existing test style in that file (they start a real server thread on a loopback port with a fake upstream; reuse the existing fixtures/helpers). Cover:

1. manage-keys + ≥1 caller token + request with only `User-Agent: claude-cli/2.1.226 (external, cli)` and no X-Usage-Caller → row recorded with caller `claude-code`, upstream receives the request, UA header still forwarded upstream.
2. manage-keys + ≥1 caller token + request with no identifying header at all → 401 JSON, row `outcome=rejected status=401 caller=unattributed`, NO upstream call (assert fake upstream hit-count unchanged).
3. manage-keys + zero caller tokens + no identifying header → accepted as today (compat), row caller None.
4. Token caller beats header label: request with valid caller token AND X-Usage-Caller: something-else → row caller = token's name.
5. Header label beats UA: request with X-Usage-Caller: claude-code AND User-Agent: totally-other/1.0 → accepted, row caller = claude-code (from header), not gated.
6. Label with invalid characters (`X-Usage-Caller: bad label!`) + no UA + tokens exist → 401 gate (sanitize returns None).
7. `caller_label_from_user_agent` unit cases: None, empty, `claude-cli/2.1.226 (external, cli)` → claude-code, `Mozilla/5.0` → None.

## Definition of done

- Focused suite green: `HERMES_PYTHON=/root/hermes-agent/.venv/bin/python bash scripts/run_tests.sh tests/plugins/llm_usage_proxy -j 4` (report exact counts; the runner is the receipt, not bare pytest).
- One commit on `feat/llm-usage-proxy`, conventional message `feat(llm_usage_proxy): refuse unattributed traffic when caller tokens exist`, identity `git -c user.name="Hermes Agent" -c user.email="hermes@yazdani.au" commit`.
- `git diff --stat <old-HEAD>..HEAD` touches ONLY server.py, its tests, README.md.
- Do NOT push; leave the branch local. Report: new HEAD SHA, diffstat, test counts, any deviation from this brief.
