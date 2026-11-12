---
name: har-derived-api-client
description: Derive an HTTP client from an existing browser HAR.
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Browser, HAR, API, Reverse-Engineering]
    category: web-development
---

# HAR-Derived API Client

Distill an existing HTTP Archive (HAR) into a site's private JSON API so you can
call it directly with plain HTTP — far cheaper and faster than controlling the
page on every request. Credit: trick by Jared Longster, popularized by Dax
(thdxr). This replays captured traffic; it does NOT bypass auth, solve CAPTCHAs,
or defeat bot-detection. If the site needs a logged-in session, carry its
headers/cookies forward with the subject's authorization; never forge them.

Derivation is pure stdlib. Replay needs only `requests`/`httpx` (or `curl`).

## When to Use

- "Build a CLI/client for <website>" — derive its API instead of scripting clicks.
- "This site has no public API but the page clearly fetches JSON."
- You're about to loop `browser_navigate` for the same query repeatedly — stop and derive the endpoint once.
- Reverse-engineering an autocomplete, search, feed, or checkout XHR.
- You already have a HAR from an authorized browser or network-recording session.

## Prerequisites

- An existing HAR file with response bodies embedded.
- `requests` or `httpx` for replay (stdlib `urllib` also works).
- No API keys for derivation. Any keys/tokens the client needs are the ones the HAR captured.
- Camofox's REST API does not expose HAR capture. Supply a HAR from browser
  DevTools or another authorized recorder rather than expecting `browser_*`
  tools to create one.

## How to Run

Invoke `scripts/har_to_client.py` through the `terminal` tool. It filters the
HAR to XHR/fetch/JSON, groups by endpoint, and prints params, headers, bodies,
and replay hints (User-Agent / cookie / auth).

```bash
python3 scripts/har_to_client.py out.har --host SITE --max-body 400
```

Resolve script paths against this skill's directory and HAR paths against the
current workspace.

## Quick Reference

```
har_to_client.py <in.har> [--host SUBSTR] [--include-static] [--max-body N]
  default: keeps only XHR/fetch/JSON; --host narrows to one domain
  prints per endpoint: query params, non-boring req headers, req body sample,
                       response status/content-type + body sample
  prints "### Replay hints": the browser User-Agent, cookie/auth presence
```

## Procedure

1. **Find the interaction.** Open the site with Camofox `browser_navigate` and
   confirm a JSON XHR fires. Capture an authorized HAR with browser DevTools or
   an external network recorder, including response bodies.
2. **Derive** with `har_to_client.py --host <domain>`. Read off the method,
   URL/path template (numeric/UUID segments collapse to `{id}`), query params,
   request-body JSON, and `### Replay hints` block.
3. **Write the client.** Recreate the request exactly — same method, path,
   query params, and body. Send the headers the site actually needs: at minimum
   copy the **User-Agent** from the replay hints. If hints report cookies or an
   auth/token header, resend those too.
4. **Test browserless.** Run the client with the `terminal` tool and confirm it
   returns the same data the browser saw.
5. **(Optional) Wrap as a CLI** — a small `argparse` script over the derived
   call, for example `search.py "frank herbert"`.

Worked example (Wikipedia search-title):

```python
import requests
r = requests.get(
    "https://en.wikipedia.org/w/rest.php/v1/search/title",
    params={"q": "frank herbert", "limit": 5},
    headers={"accept": "application/json",
             "User-Agent": "Mozilla/5.0 ..."},  # from HAR
    timeout=15,
)
for p in r.json()["pages"]:
    print(p["title"], "-", p.get("description"))
```

## Pitfalls

- **Default library User-Agent gets 403.** Many sites reject
  `python-requests/x.y`. Send the browser UA from the replay hints before
  assuming the endpoint changed.
- **Server-rendered pages have no XHR** to derive — `har_to_client.py` prints
  "No API-looking entries". Scrape the HTML or find the interaction that fetches JSON.
- **Debounced/typeahead XHRs need a real pause** during capture. A HAR recorded
  before the request fires cannot contain the endpoint.
- **Missing response bodies limit derivation.** Re-capture with response content
  enabled when the HAR contains URLs and headers but no JSON payloads.
- **Auth/session endpoints** need the captured `Cookie`/`Authorization` header,
  and those expire. Re-capture when replay returns 401. HARs contain live
  secrets: store them with restrictive permissions and delete them after deriving.
- **Endpoints shift.** Re-run the capture→derive loop when a client breaks
  rather than patching URLs by hand.

## Verification

Run the derivation against an existing HAR and confirm the printed endpoint,
parameters, and response body match a direct replay:

```bash
python3 scripts/har_to_client.py /path/to/authorized-capture.har --host SITE --max-body 200
```

The derived client must reproduce the captured JSON response over plain HTTP
without a browser in the loop.