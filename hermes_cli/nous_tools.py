"""Per-tool state for the Nous-managed Tool Gateway (feature availability view)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Set

from hermes_cli.config import get_env_value, load_config
from hermes_cli.nous_account import NousPortalAccountInfo
from tools.managed_tool_gateway import is_managed_tool_gateway_ready
from utils import is_truthy_value
from tools.tool_backend_helpers import fal_key_is_configured


_DEFAULT_PLATFORM_TOOLSETS = {"cli": "hermes-cli"}


@dataclass(frozen=True)
class _FeatureSpec:
    """Per-feature parameters shared by the status and defaults surfaces."""

    label: str
    included_by_default: bool
    # Tool-pool coverage category (nous_account.TOOL_COVERAGE_CATEGORIES) gating per backend:
    # the free pool funds image but NOT video.
    coverage: str
    gateway: str  # managed gateway probed for readiness (video rides image's fal-queue)
    # Config (section, selection field) written when the managed selection is stored. None = not offered.
    section_field: Optional[tuple[str, str]] = None
    offer_label: str = ""
    direct_label: str = ""
    # Direct-credential env vars that pin the category to direct credentials.
    default_direct_env: tuple[str, ...] = ()


_FEATURES: Dict[str, _FeatureSpec] = {
    "web": _FeatureSpec(
        "Web tools", True, "firecrawl", "firecrawl", ("web", "backend"),
        "Web search & extract (Firecrawl)", "Firecrawl/Exa/Parallel/Tavily/Perplexity/Keenable key or SearXNG",
        ("PARALLEL_API_KEY", "TAVILY_API_KEY", "PERPLEXITY_API_KEY", "FIRECRAWL_API_KEY", "FIRECRAWL_API_URL"),
    ),
    "image_gen": _FeatureSpec(
        "Image generation", True, "fal", "fal-queue", ("image_gen", "provider"), "Image generation (FAL)", "FAL key",
    ),
    "video_gen": _FeatureSpec(
        "Video generation", False, "fal-video", "fal-queue", ("video_gen", "provider"), "Video generation (FAL)", "FAL key",
    ),
}

_FEATURE_ORDER = tuple(_FEATURES)
# Public / test-referenced views over the table.
MANAGED_FEATURE_COVERAGE_CATEGORY: Dict[str, str] = {k: s.coverage for k, s in _FEATURES.items()}
_GATEWAY_SECTION_FIELDS = {k: s.section_field for k, s in _FEATURES.items() if s.section_field}
_ALL_GATEWAY_KEYS = tuple(_GATEWAY_SECTION_FIELDS)
_GATEWAY_TOOL_LABELS = {k: _FEATURES[k].offer_label for k in _ALL_GATEWAY_KEYS}
# Sections materialised before writing selections.
_DEFAULT_SECTIONS = ("web",)


def _uses_gateway(section: object) -> bool:
    """True when a config section explicitly opts into the gateway (legacy ``use_gateway: true``)."""
    return isinstance(section, dict) and is_truthy_value(section.get("use_gateway"), default=False)


def _selected_provider(section: object, name_key: str = "provider") -> Optional[str]:
    """Stored provider for a section (``read_selection`` semantics): ``"nous"`` for the managed
    selection (stored ``nous`` or legacy ``use_gateway: true``), a vendor name for BYOK, else None."""
    if not isinstance(section, dict):
        return None
    if _uses_gateway(section):
        return "nous"
    value = section.get(name_key)
    return None if value is None else (str(value).strip().lower() or None)


@dataclass(frozen=True)
class NousFeatureState:
    key: str
    label: str
    included_by_default: bool
    available: bool
    active: bool
    managed_by_nous: bool
    direct_override: bool
    toolset_enabled: bool
    current_provider: str = ""
    explicit_configured: bool = False


@dataclass(frozen=True)
class NousToolFeatures:
    nous_auth_present: bool
    provider_is_nous: bool
    features: Dict[str, NousFeatureState]
    account_info: Optional[NousPortalAccountInfo] = None

    def __getattr__(self, name: str) -> NousFeatureState:  # ``features.web`` -> per-key state
        if name in _FEATURE_ORDER:
            return self.features[name]
        raise AttributeError(name)

    def items(self) -> Iterable[NousFeatureState]:
        return (self.features[key] for key in _FEATURE_ORDER)


def _section(config: Dict[str, object], key: str) -> Dict[str, object]:
    """``config[key]`` when it is a dict, else ``{}`` (read-only view)."""
    value = config.get(key)
    return value if isinstance(value, dict) else {}


def _ensure_section(config: Dict[str, object], key: str) -> Dict[str, object]:
    """Return ``config[key]`` as a dict, creating/replacing it in ``config`` when missing."""
    value = config.get(key)
    if not isinstance(value, dict):
        value = config[key] = {}
    return value


def _select_nous(config: Dict[str, object], key: str) -> None:
    """Store the managed ``nous`` selection in the ``key`` section (field per _GATEWAY_SECTION_FIELDS)."""
    section_key, field = _GATEWAY_SECTION_FIELDS[key]
    section = _ensure_section(config, section_key)
    section[field] = "nous"
    section.pop("use_gateway", None)


def _norm(value: object, default: str = "") -> str:
    return str(value or default).strip().lower()


def _provider_is_nous(config: Dict[str, object]) -> bool:
    return _norm(_section(config, "model").get("provider")) == "nous"


def _toolset_enabled(config: Dict[str, object], toolset_key: str) -> bool:
    """True when some platform's configured toolsets cover every tool of ``toolset_key``."""
    from toolsets import resolve_toolset

    platform_toolsets = config.get("platform_toolsets")
    if not isinstance(platform_toolsets, dict) or not platform_toolsets:
        platform_toolsets = {"cli": [_DEFAULT_PLATFORM_TOOLSETS["cli"]]}
    target_tools = set(resolve_toolset(toolset_key))
    if not target_tools:
        return False
    for platform, raw_toolsets in platform_toolsets.items():
        toolset_names = list(raw_toolsets) if isinstance(raw_toolsets, list) else []
        if not toolset_names:
            toolset_names = [t for t in (_DEFAULT_PLATFORM_TOOLSETS.get(platform),) if t]
        available_tools: Set[str] = set()
        for toolset_name in toolset_names:
            if isinstance(toolset_name, str) and toolset_name:
                try:
                    available_tools.update(resolve_toolset(toolset_name))
                except Exception:
                    continue
        if target_tools.issubset(available_tools):
            return True
    return False


def _any_env(*names: str) -> bool:
    """True when any of the named env vars (via get_env_value) is set."""
    return any(get_env_value(name) for name in names)


def _account_info_or_none(**kwargs) -> Optional[NousPortalAccountInfo]:
    """``get_nous_portal_account_info(**kwargs)``, failing closed to ``None`` on any error."""
    try:
        from hermes_cli.nous_account import get_nous_portal_account_info
        return get_nous_portal_account_info(**kwargs)
    except Exception:
        return None


def _state(key: str, **fields) -> NousFeatureState:
    spec = _FEATURES[key]
    fields.setdefault("direct_override", fields["active"] and not fields["managed_by_nous"])
    return NousFeatureState(key, spec.label, spec.included_by_default, **fields)


def _web_feature(web_cfg: Dict[str, object], tool_enabled: bool, managed: bool, web_gw: bool, direct_firecrawl: bool) -> NousFeatureState:
    # Per-capability overrides decide the active search/extract backend independently of web.backend.
    backend, search_backend, extract_backend = (_norm(web_cfg.get(k)) for k in ("backend", "search_backend", "extract_backend"))
    # The "nous" selection is serviced by Firecrawl — normalize so downstream vendor checks hold.
    if backend == "nous" or web_gw:
        backend = "firecrawl"
    # Direct readiness per vendor; a stored managed selection suppresses direct credentials.
    # Keyless Tavily is opt-in: selecting it writes web.backend (or a per-capability override).
    direct = {
        "exa": _any_env("EXA_API_KEY") and not web_gw,
        "firecrawl": direct_firecrawl,
        "parallel": _any_env("PARALLEL_API_KEY") and not web_gw,
        "tavily": (_any_env("TAVILY_API_KEY") or "tavily" in {backend, search_backend, extract_backend}) and not web_gw,
        "perplexity": _any_env("PERPLEXITY_API_KEY") and not web_gw,
        "searxng": _any_env("SEARXNG_URL"),
    }
    web_managed = backend == "firecrawl" and managed and not direct_firecrawl
    active = web_managed or direct.get(backend) or direct.get(search_backend) or (extract_backend in ("tavily", "perplexity") and direct[extract_backend])
    return _state(
        "web", available=bool(managed or any(direct.values())), active=bool(tool_enabled and active),
        managed_by_nous=web_managed, toolset_enabled=tool_enabled,
        current_provider=backend or search_backend or extract_backend or "",
        explicit_configured=bool(backend or search_backend or extract_backend),
    )


def _fal_feature(key: str, tool_enabled: bool, direct: bool, managed: bool, selected: Optional[str]) -> NousFeatureState:
    # image_gen / video_gen: same FAL_KEY, independently gated managed availability.
    fal_managed = tool_enabled and managed and not direct
    if selected not in (None, "nous") or (selected is None and direct):
        label = "FAL"
    else:
        label = "Nous Tool Gateway" if (fal_managed or selected == "nous") else ""
    return _state(
        key, available=bool(managed or direct), active=bool(tool_enabled and (fal_managed or direct)),
        managed_by_nous=fal_managed, toolset_enabled=tool_enabled, current_provider=label,
        explicit_configured=selected is not None or direct,
    )


def get_nous_tool_features(config: Optional[Dict[str, object]] = None, *, force_fresh: bool = False) -> NousToolFeatures:
    if config is None:
        config = load_config() or {}
    provider_is_nous = _provider_is_nous(config)
    account_info = _account_info_or_none(**({"force_fresh": True} if force_fresh else {}))
    # Coarse "entitled to any managed tool" gate: paid access OR a live free tool pool. Per-backend
    # availability is then narrowed by coverage (the pool funds image but not video, etc.).
    nous_auth_present = bool(account_info and account_info.logged_in)
    managed_tools_flag = nous_auth_present and account_info.tool_gateway_entitled
    enabled = {key: _toolset_enabled(config, key) for key in ("web", "image_gen", "video_gen")}
    # Stored selections (strict model): "nous" = managed gateway; vendor name = that vendor direct;
    # None = never configured (autodetect). Lockstep with tool_backend_helpers.read_selection.
    selected = {
        key: _selected_provider(_section(config, section_key), field)
        for key, (section_key, field) in _GATEWAY_SECTION_FIELDS.items()
    }
    use_gateway = {key: value == "nous" for key, value in selected.items()}
    # Managed availability per feature. A stored VENDOR selection pins the category to direct
    # credentials — managed availability must not light it up (the runtime errors, not reroutes).
    # Every remaining feature has a config selection field that pins vendor vs managed.
    managed = {
        key: (
            managed_tools_flag and is_managed_tool_gateway_ready(spec.gateway)
            and account_info.tool_gateway_entitled_for(spec.coverage)
            and (selected.get(key) is None or use_gateway.get(key, False))
        )
        for key, spec in _FEATURES.items()
    }
    direct_firecrawl = _any_env("FIRECRAWL_API_KEY", "FIRECRAWL_API_URL") and not use_gateway["web"]
    fal_configured = fal_key_is_configured()

    def _fal(key: str) -> NousFeatureState:
        return _fal_feature(key, enabled[key], fal_configured and not use_gateway[key], managed[key], selected[key])

    features = {  # insertion order == _FEATURE_ORDER
        "web": _web_feature(_section(config, "web"), enabled["web"], managed["web"], use_gateway["web"], direct_firecrawl),
        "image_gen": _fal("image_gen"),
        "video_gen": _fal("video_gen"),
    }
    return NousToolFeatures(
        nous_auth_present=nous_auth_present,
        provider_is_nous=provider_is_nous, features=features, account_info=account_info,
    )
