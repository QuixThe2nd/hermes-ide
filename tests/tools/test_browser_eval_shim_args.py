"""agent-browser CLI ``eval`` payloads must survive the Windows ``.cmd`` shim hop (#113838):
cmd.exe re-parses the child command line, so a newline truncates the script to its first line.
Host-specific bug — these are code-path invariants that run on every host."""

from __future__ import annotations

import base64
import importlib
import pkgutil

import tools
from tools import browser_tool_session as bt_session

_SCRIPT = "JSON.stringify(\n  [...document.images].map(i => i.src)\n)"


def test_cmd_shim_sends_eval_script_base64_and_others_raw():
    """A ``.cmd``/``.bat`` argv[0] gets ``--base64 <script>`` (lossless through cmd.exe); a
    native binary, npx sentinel-expanded or POSIX path keeps the raw script; non-eval untouched."""
    shim = bt_session._shim_safe_eval_args(r"C:\Users\u\AppData\Roaming\npm\npx.CMD", "eval", [_SCRIPT])
    assert shim[0] == "--base64"
    assert base64.b64decode(shim[1]).decode("utf-8") == _SCRIPT  # byte-identical round trip
    assert "\n" not in shim[1]

    for argv0 in (r"C:\tools\agent-browser.exe", "/home/u/.local/bin/agent-browser", "npx"):
        assert bt_session._shim_safe_eval_args(argv0, "eval", [_SCRIPT]) == [_SCRIPT]
    assert bt_session._shim_safe_eval_args("npx.cmd", "open", ["https://example.com"]) == ["https://example.com"]


def test_bundled_js_eval_payload_constants_are_single_line():
    """Every ``*_JS`` string constant in ``tools/browser_*`` is one line — the fixed
    ``_GET_IMAGES_JS`` and any future payload handed to the CLI ``eval`` as one argv element."""
    seen = []
    for mod_info in pkgutil.iter_modules(tools.__path__):
        if not mod_info.name.startswith("browser_"):
            continue
        module = importlib.import_module(f"tools.{mod_info.name}")
        for name, value in vars(module).items():
            if name.endswith("_JS") and isinstance(value, str):
                seen.append((mod_info.name, name))
                assert "\n" not in value, f"tools/{mod_info.name}.py::{name} spans multiple lines"
    assert ("browser_tool", "_GET_IMAGES_JS") in seen
