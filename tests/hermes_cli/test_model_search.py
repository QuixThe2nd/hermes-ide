"""Picker search aliases for brand-less wire model ids."""

from hermes_cli.curses_ui import _filter_indices
from hermes_cli.model_search import model_search_text


def test_model_search_text_keeps_ordinary_ids():
    assert model_search_text("kimi-k2.6") == "kimi-k2.6"
    assert model_search_text("glm-5.2") == "glm-5.2"


def test_filter_indices_surfaces_k3_for_kimi_query():
    models = ["kimi-k2.6", "kimi-k2.5", "k3", "kimi-for-coding"]
    haystacks = [model_search_text(m) for m in models]
    ranked = [models[i] for i in _filter_indices(haystacks, "kimi")]
    assert "k3" in ranked


def test_model_search_text_does_not_brand_the_retired_ox_alpha_preview():
    # the retired preview's codename is no longer a search alias: the opaque
    # wire id stays searchable by its own id only
    assert model_search_text("x-preview-f-free") == "x-preview-f-free"
    assert model_search_text("X-Preview-F-Free") == "X-Preview-F-Free"


def test_filter_indices_does_not_surface_retired_codename_queries():
    models = ["x-preview-f-free", "gpt-5.6-sol", "kimi-k3"]
    haystacks = [model_search_text(m) for m in models]
    for query in ("ox", "ox-alpha"):
        ranked = [models[i] for i in _filter_indices(haystacks, query)]
        assert "x-preview-f-free" not in ranked, query
    # the id itself still finds the model — manually entered ids stay usable
    ranked = [models[i] for i in _filter_indices(haystacks, "x-preview")]
    assert "x-preview-f-free" in ranked



