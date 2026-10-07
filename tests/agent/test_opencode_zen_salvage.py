"""Fork regression guard: retired slugs stay out of curated setup inventories.

Salvaged from tests/agent/test_opencode_free_provider.py when upstream
removed the keyless opencode-free tier (998f614c7f); the rest of that
file tested only the deleted provider. The Ox Alpha preview slug
x-preview-f-free was retired on the fork (PR #192); the setup sample
inventory must not re-list it, and manual wire-ID classification via
is_opencode_zen_free_model stays intact.

The models-catalog assertion from the original test is intentionally
dropped: upstream's curated opencode-zen list is a live-discovery floor
and re-lists x-preview-f-free by design.
"""

from hermes_cli.models import is_opencode_zen_free_model
from hermes_cli.setup import _DEFAULT_PROVIDER_MODELS


def test_retired_ox_alpha_not_in_setup_inventory():
    """The retired Ox Alpha preview slug stays out of the setup sample
    inventory for opencode-zen, while generic manual wire-ID handling
    keeps classifying it as a keyless Zen free slug."""
    assert "x-preview-f-free" not in _DEFAULT_PROVIDER_MODELS["opencode-zen"]
    assert is_opencode_zen_free_model("x-preview-f-free")
