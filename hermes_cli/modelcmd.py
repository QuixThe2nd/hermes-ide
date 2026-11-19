"""Python twin of ``internal/modelcmd`` (Go canonical).

The Go module under ``internal/modelcmd`` owns the pure logic of the
``/model`` slash command: argument/flag parsing, the no-argument listing
text, and the interactive picker's provider rows. This module is the twin
the runtime imports — repo deployments (pip, docker) carry no Go toolchain,
the same arrangement as ``internal/servingidentity`` and
``internal/generations``. ``tests/hermes_cli/test_modelcmd_contract.py``
pipes every shared-table record (``internal/modelcmd/testdata/cases.json``)
through the running Go binary when a toolchain exists and asserts this
twin answers identically, so the implementations cannot drift.

Parsing lives in ``hermes_cli.model_switch.parse_model_switch_args`` and is
NOT re-implemented here — the twin owns only the rendering/trimming halves.
"""

from __future__ import annotations

from typing import Any, Dict, List

DEFAULT_MAX_PICKER_PROVIDERS = 25  # Discord select-menu option cap
DEFAULT_MAX_PICKER_MODELS = 50

_EN_LABELS = {
    "current_label": "Current: `{model}` on {provider}",
    "current_tag": " (current)",
    "more_models_suffix": " (+{count} more)",
    "usage_switch_model": "`/model <name>` — switch model",
    "usage_switch_provider": "`/model <name> --provider <slug>` — switch provider",
    "usage_persist": "`/model <name> --global` — persist",
}


def listing_record(
    current_model: str,
    current_provider_label: str,
    providers: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Assemble the wire record RenderListing consumes.

    Labels are pre-localized here (the Go side renders only); providers
    rows are ``list_authenticated_providers`` output verbatim.
    """
    from agent.i18n import t

    return {
        "current_model": current_model or "unknown",
        "current_provider_label": current_provider_label or "unknown",
        "labels": {
            "current_label": t("gateway.model.current_label"),
            "current_tag": t("gateway.model.current_tag"),
            "more_models_suffix": t("gateway.model.more_models_suffix"),
            "usage_switch_model": t("gateway.model.usage_switch_model"),
            "usage_switch_provider": t("gateway.model.usage_switch_provider"),
            "usage_persist": t("gateway.model.usage_persist"),
        },
        "providers": providers,
    }


def _label(labels: Dict[str, str], key: str) -> str:
    v = (labels or {}).get(key)
    return v if v else _EN_LABELS[key]


def _format(template: str, **vars: str) -> str:
    try:
        return template.format(**vars)
    except (KeyError, IndexError):
        return template


def render_model_listing(record: Dict[str, Any]) -> str:
    """Lay out the no-arg ``/model`` reply: current line, one block per
    provider, then the three usage lines. Mirrors Go RenderListing."""
    labels = record.get("labels") or {}
    lines = [
        _format(
            _label(labels, "current_label"),
            model=record.get("current_model") or "unknown",
            provider=record.get("current_provider_label") or "unknown",
        ),
        "",
    ]
    for p in record.get("providers") or []:
        tag = _label(labels, "current_tag") if p.get("is_current") else ""
        lines.append(f"**{p['name']}** `--provider {p['slug']}`{tag}:")
        models = p.get("models") or []
        if models:
            model_strs = ", ".join(f"`{m}`" for m in models)
            hidden = int(p.get("total_models") or 0) - len(models)
            extra = (
                _format(_label(labels, "more_models_suffix"), count=str(hidden))
                if hidden > 0
                else ""
            )
            lines.append(f"  {model_strs}{extra}")
        elif p.get("api_url"):
            lines.append(f"  `{p['api_url']}`")
        lines.append("")
    lines.extend(
        [
            _label(labels, "usage_switch_model"),
            _label(labels, "usage_switch_provider"),
            _label(labels, "usage_persist"),
        ]
    )
    return "\n".join(lines)


def build_picker_rows(
    providers: List[Dict[str, Any]],
    current_provider: str = "",
    max_providers: int = DEFAULT_MAX_PICKER_PROVIDERS,
    max_models: int = DEFAULT_MAX_PICKER_MODELS,
) -> List[Dict[str, Any]]:
    """Trim raw provider rows to what the interactive picker can render.

    Mirrors Go PickerOptions: drop rows without models AND without an
    ``api_url``; cap each row's model list (``total_models`` keeps the full
    count so "+N more" stays truthful); mark ``is_current`` exactly on the
    row matching *current_provider*; cap the row count, always keeping the
    current provider.
    """
    max_providers = max_providers or DEFAULT_MAX_PICKER_PROVIDERS
    max_models = max_models or DEFAULT_MAX_PICKER_MODELS
    current = (current_provider or "").strip().lower()
    rows: List[Dict[str, Any]] = []
    for p in providers or []:
        models = [m for m in (p.get("models") or []) if isinstance(m, str)]
        if not models and not str(p.get("api_url") or "").strip():
            continue
        p = dict(p)
        if len(models) > max_models:
            p["models"] = models[:max_models]
        p["is_current"] = str(p.get("slug") or "").lower() == current
        rows.append(p)
    if len(rows) > max_providers:
        current_idx = next(
            (i for i, r in enumerate(rows) if r.get("is_current")), -1
        )
        keep = rows[:max_providers]
        if current_idx >= max_providers:
            keep[-1], rows[current_idx] = rows[current_idx], keep[-1]
        rows = keep
    return rows
