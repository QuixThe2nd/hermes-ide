"""Managed image generation: one picker row, one model catalog.

Two gateways sit behind the managed selection; the stored ``image_gen.model`` decides which
one serves a request:

* ``fal``  — the FAL managed gateway, every id in the in-tree ``FAL_MODELS`` catalog;
* ``krea`` — the Krea managed gateway, the ``plugins/image_gen/krea`` model ids.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

FAL, KREA = "fal", "krea"

_FAL_KREA_PREFIX = "fal-ai/krea/"


def managed_backend_for_model(model_id: Optional[str]) -> str:
    """Gateway that serves ``model_id`` under the managed selection (unset → FAL default)."""
    from plugins.image_gen.krea import KREA_MODEL_IDS
    from tools.image_generation_catalog import FAL_MODELS

    candidate = model_id.strip() if isinstance(model_id, str) else ""
    if not candidate or candidate in FAL_MODELS:
        return FAL
    if candidate in KREA_MODEL_IDS:
        return KREA
    return FAL


def _plugin_rows(name: str) -> list:
    """``list_models()`` of a registered image gen plugin; ``[]`` when unavailable."""
    from tools.image_generation_tool import _get_plugin_provider

    try:
        provider = _get_plugin_provider(name)
        return list(provider.list_models() or []) if provider is not None else []
    except Exception:  # noqa: BLE001 - a broken plugin must not empty the whole picker
        return []


def managed_image_catalog(*, include_krea: bool = True) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """``({model_id: metadata}, default_model)`` for the managed row's model picker.

    FAL catalog first (minus the Krea-on-FAL entries), then native Krea. Every row carries
    ``backend`` so callers can hide the gateways an account cannot use.
    """
    from tools.image_generation_catalog import DEFAULT_MODEL, FAL_MODELS

    catalog: Dict[str, Dict[str, Any]] = {
        mid: {**meta, "backend": FAL} for mid, meta in FAL_MODELS.items()
        if not mid.startswith(_FAL_KREA_PREFIX)}
    if include_krea:
        for row in _plugin_rows("krea"):
            catalog[row["id"]] = {**row, "backend": KREA}
    return catalog, DEFAULT_MODEL
