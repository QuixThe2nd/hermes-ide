"""Interactive model picker used after OAuth login.

Re-exported from ``hermes_cli/auth.py`` (patch targets unchanged); origin helpers are imported
lazily per function so ``hermes_cli.auth.<helper>`` patches still intercept and no cycle forms.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Dict, List, Optional

logger = logging.getLogger("hermes_cli.auth")

_CUSTOM_LABEL = "Enter custom model name"
_SKIP_LABEL = "Skip (keep current)"
_CURRENT_SUFFIX = "  ← currently in use"


def _confirm_selection_guards(
    model_id: str, *, provider: str = "", base_url: str = "", api_key: str = "",
    include_kinds: Optional[List[str]] = None,
) -> bool:
    """Prompt before saving a model that trips any selection guard (cost, data-policy, ...).

    Shows one [y/N] confirm listing every warning that fired. Returns True to proceed.
    """
    try:
        from hermes_cli.model_selection_guards import combined_message, selection_warnings
        warnings = selection_warnings(
            model_id, provider=provider, base_url=base_url, api_key=api_key, include_kinds=include_kinds,
        )
    except Exception:
        warnings = []
    if not warnings:
        return True

    print()
    print("=" * 72)
    print(combined_message(warnings))
    print("=" * 72)
    try:
        response = input("Switch anyway? [y/N]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        return False
    return response in {"y", "yes"}


class _ModelPickerRows:
    """Column-aligned picker rows (name + $/Mtok).

    Styled segments, not ANSI baked into one string — curses addnstr would render escape
    bytes literally.
    """

    def __init__(
        self, all_models: List[str], pricing: Optional[Dict[str, Dict[str, str]]], *,
        current_model: str,
    ) -> None:
        from hermes_cli.models_pricing import _format_price_per_mtok
        self.current_model = current_model
        self.has_pricing = bool(pricing and any(pricing.get(m) for m in all_models))
        self.name_col = max((len(m) for m in all_models), default=0) + 2 if self.has_pricing else 0
        # (inp, out, cache)
        self._price_cache: dict[str, tuple[str, str, str]] = {}
        self.price_col = 3  # minimum width
        self.cache_col = 0  # only set if any model has cache pricing
        self.has_cache = False
        if not self.has_pricing:
            return

        for mid in all_models:
            p = pricing.get(mid)  # type: ignore[union-attr]
            inp, out, cache = "", "", ""
            if p:
                inp = _format_price_per_mtok(p.get("prompt", ""))
                out = _format_price_per_mtok(p.get("completion", ""))
                cache_read = p.get("input_cache_read", "")
                cache = _format_price_per_mtok(cache_read) if cache_read else ""
                if cache:
                    self.has_cache = True
            self._price_cache[mid] = (inp, out, cache)
            self.price_col = max(self.price_col, len(inp), len(out))
            self.cache_col = max(self.cache_col, len(cache))
        if self.has_cache:
            self.cache_col = max(self.cache_col, 5)  # minimum: "Cache" header

    def segments(self, mid: str) -> list[tuple[str, str | None]]:
        """Build a rich radiolist row: plain name + prices."""
        current = [(_CURRENT_SUFFIX, None)] if mid == self.current_model else []
        if not self.has_pricing:
            return [(mid, None), *current]

        inp, out, cache = self._price_cache.get(mid, ("", "", ""))
        segs: list[tuple[str, str | None]] = [(f"{mid:<{self.name_col}}", None)]
        price_part = f" {inp:>{self.price_col}}  {out:>{self.price_col}}"
        if self.has_cache:
            price_part += f"  {cache:>{self.cache_col}}"
        segs.append((price_part, None))
        return segs + current

    def label(self, mid: str) -> str:
        return "".join(text for text, _style in self.segments(mid))

    def menu_title(self) -> str:
        """``Select default model:`` plus an aligned pricing header hint when priced."""
        title = "Select default model:"
        if self.has_pricing:
            # Each choice is "  {label}" (2 spaces) plus a 3-char cursor region ("-> " or "   "),
            # so content starts at col 5.
            pad = " " * 5
            header = f"\n{pad}{'':>{self.name_col}} {'In':>{self.price_col}}  {'Out':>{self.price_col}}"
            if self.has_cache:
                header += f"  {'Cache':>{self.cache_col}}"
            # Legend lives on the column-header line so it reads as a key, not a fake menu row.
            title += header + "  $/Mtok"
        return title


def _prompt_model_selection(
    model_ids: List[str], current_model: str = "",
    pricing: Optional[Dict[str, Dict[str, str]]] = None,
    confirm_provider: str = "", confirm_base_url: str = "",
    confirm_api_key: str = "",
) -> Optional[str]:
    """Interactive model picker; current_model listed first. Returns the chosen model ID or None.

    With *pricing* (``{model_id: {prompt, completion}}``) a compact price column is shown.
    """
    from hermes_cli.cli_output import line_input

    def _confirmed_selection(mid: str) -> Optional[str]:
        if not mid:
            return None
        # Cost guard needs a known provider; id-keyed guards (data policy) always run.
        ok = _confirm_selection_guards(
            mid, provider=confirm_provider, base_url=confirm_base_url, api_key=confirm_api_key,
            include_kinds=None if confirm_provider else ["data_policy"],
        )
        return mid if ok else None

    def _custom_selection() -> Optional[str]:
        try:
            custom = line_input("Enter model name: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        return _confirmed_selection(custom) if custom else None

    # Reorder: current model first, then the rest (deduplicated)
    ordered = list(dict.fromkeys(
        ([current_model] if current_model and current_model in model_ids else []) + list(model_ids)
    ))

    # All models for column-width computation
    rows = _ModelPickerRows(ordered, pricing, current_model=current_model)

    menu_title = rows.menu_title()
    n = len(ordered)

    # Try arrow-key menu first, fall back to number input.
    try:
        from hermes_cli.curses_ui import curses_radiolist
        choices = [rows.segments(mid) for mid in ordered] + [_CUSTOM_LABEL, _SKIP_LABEL]

        # Header/legend go in the description so they survive the curses clear.
        desc_lines: list[str] = menu_title.split("\n", 1)[1].splitlines() if rows.has_pricing else []

        # Search haystack = label + aliases for brand-less wire ids (Kimi `k3` ↔ "kimi"); skip when
        # model_search_text adds nothing beyond the bare id.
        from hermes_cli.model_search import model_search_text
        model_search_labels = []
        for mid in ordered:
            label, haystack = rows.label(mid), model_search_text(mid)
            model_search_labels.append(label if haystack == mid else f"{label} {haystack}")
        model_search_labels += [_CUSTOM_LABEL, _SKIP_LABEL]

        idx = curses_radiolist(
            "Select default model:",
            choices,
            selected=0,  # cursor on the current model (index 0 if it was reordered to top)
            cancel_returns=-1,
            description="\n".join(desc_lines) if desc_lines else None,
            searchable=True,
            search_labels=model_search_labels,
        )
        if idx < 0:
            return None
        print()
        if idx < n:
            return _confirmed_selection(ordered[idx])
        if idx == n:
            return _custom_selection()
        return None
    except (ImportError, NotImplementedError, OSError, subprocess.SubprocessError):
        pass

    # Fallback: numbered list
    from hermes_cli.curses_ui import format_radio_item_ansi
    for line in menu_title.splitlines():
        print(line)
    num_width = len(str(n + 2))
    for i, mid in enumerate(ordered, 1):
        print(f"  {i:>{num_width}}. {format_radio_item_ansi(rows.segments(mid))}")
    print(f"  {n + 1:>{num_width}}. {_CUSTOM_LABEL}")
    print(f"  {n + 2:>{num_width}}. {_SKIP_LABEL}")
    print()

    while True:
        try:
            choice = input(f"Choice [1-{n + 2}] (default: skip): ").strip()
            if not choice:
                return None
            idx = int(choice)
            if 1 <= idx <= n:
                return _confirmed_selection(ordered[idx - 1])
            if idx == n + 1:
                return _custom_selection()
            if idx == n + 2:
                return None
            print(f"Please enter 1-{n + 2}")
        except ValueError:
            print("Please enter a number")
        except (KeyboardInterrupt, EOFError):
            return None


def _save_model_choice(model_id: str) -> None:
    """Save the selected model to config.yaml only — NOT .env, which would stomp in multi-agent setups."""
    from hermes_cli.config import save_config, load_config
    config = load_config()
    # Always use dict format so provider/base_url can be stored alongside
    if isinstance(config.get("model"), dict):
        config["model"]["default"] = model_id
    else:
        config["model"] = {"default": model_id}
    save_config(config)
