# live-progress

Reference renderer for the **`plugin`** tool-progress mode. It exists so
`display.tool_progress: plugin` has something to render out of the box, and so plugin authors
have a working example of the two `ctx.progress()` shapes.

```yaml
display:
  tool_progress: plugin          # global default
  platforms:
    discord:
      tool_progress: plugin      # per platform
```

## What it renders

One bubble per turn, edited in place by the gateway:

```
🤖 Working — refactor the export path
▸ read_file gateway/run_turn.py
✎ patch gateway/run_turn.py
$ pytest tests/gateway -q
8s · 3 step(s)
```

| Hook | Effect |
|---|---|
| `pre_llm_call` | Opens the body: header plus the user's ask as subtitle (truncated to 60 chars). |
| `post_tool_call` | Appends one line per finished tool call: glyph, tool name, a short hint (`file_path` / `command` / `query`). |
| `post_llm_call` | Marks the run done — the footer gains `✅`. The answer itself is the gateway's own message. |
| `on_session_end` | Drops the per-session state. |

The body is pushed as `("__body__", text)` on every change, which replaces the whole bubble
instead of appending a line — that is what makes the live footer possible.

## Settings

```yaml
plugins:
  entries:
    live-progress:
      settings:
        title: "🤖 Working"   # header text
        max_steps: 8          # rolling window; older steps collapse into "… N earlier step(s)"
```

## What the mode does not change

The gateway still owns the message: `progress_edit_interval` throttling, overflow splitting at
the platform text limit, `cleanup_progress`, cross-platform suppression (Signal and other
non-editing adapters) and the mid-run restart recovery path are core behaviour. In `plugin` mode
the core only stops queueing its *own* tool lines so the two renderers cannot interleave;
`thinking_progress` lines and the final answer are unaffected.

`ctx.progress()` is fail-open: it returns `False` when no turn is running (CLI session, platform
without progress support, plugin work outside a turn) and never raises. Writing progress is
therefore always optional for a plugin — branch on the return value only if you want to skip the
rendering work itself.

## Install

```bash
cp -r plugins/live-progress ~/.hermes/plugins/
hermes plugins list          # confirm it is discovered
```

Then set `display.tool_progress: plugin` and start a turn that calls a few tools. Without the
mode nothing is sent to you: this plugin has no other output path.
