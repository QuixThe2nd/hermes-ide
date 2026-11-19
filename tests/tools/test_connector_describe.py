"""Connector schema description preserves composed names and candidate precedence."""

from tools.connectors.gateway.bridge import connector_describe


def test_connector_describe_maps_slugs_back_to_composed_names():
    class FakeClient:
        def schemas(self, slugs):
            assert slugs == ["GMAIL_SEND_EMAIL", "SEND_EMAIL"]
            return {
                "schemas": {
                    "GMAIL_SEND_EMAIL": {
                        "connector": "gmail",
                        "tool": "GMAIL_SEND_EMAIL",
                        "description": "Send an email",
                        "input_schema": {"type": "object"},
                    }
                },
                "not_found": [],
            }

    out = connector_describe(
        ["connectors__gmail__SEND_EMAIL", "connectors__broken"],
        availability=lambda: True,
        client_factory=lambda: FakeClient(),
    )
    assert out["tools"]["connectors__gmail__SEND_EMAIL"]["parameters"] == {"type": "object"}


def test_connector_describe_colliding_candidate_slugs_resolve_per_name():
    # A shared candidate slug must not be claimed globally by whichever name came first.
    class FakeClient:
        def schemas(self, slugs):
            assert slugs == ["FIRST_SECOND_X", "SECOND_X", "X"]
            return {
                "schemas": {
                    "SECOND_X": {
                        "connector": "second",
                        "tool": "SECOND_X",
                        "description": "Shared slug",
                        "input_schema": {"type": "object"},
                    }
                },
                "not_found": ["FIRST_SECOND_X", "X"],
            }

    out = connector_describe(
        ["connectors__first__SECOND_X", "connectors__second__X"],
        availability=lambda: True,
        client_factory=lambda: FakeClient(),
    )
    assert set(out["tools"]) == {
        "connectors__first__SECOND_X",
        "connectors__second__X",
    }


def test_connector_describe_prefers_each_names_prefixed_candidate():
    class FakeClient:
        def schemas(self, slugs):
            assert slugs == ["GMAIL_X", "X"]
            return {
                "schemas": {
                    "GMAIL_X": {
                        "connector": "gmail",
                        "tool": "GMAIL_X",
                        "description": "prefixed",
                        "input_schema": {"type": "object", "title": "prefixed"},
                    },
                    "X": {
                        "connector": "gmail",
                        "tool": "X",
                        "description": "literal",
                        "input_schema": {"type": "object", "title": "literal"},
                    },
                },
                "not_found": [],
            }

    out = connector_describe(
        ["connectors__gmail__X"],
        availability=lambda: True,
        client_factory=lambda: FakeClient(),
    )
    assert out["tools"]["connectors__gmail__X"]["description"] == "prefixed"


def test_connector_describe_is_empty_on_unavailable_and_exploding_client():
    assert connector_describe(["connectors__g__T"], availability=lambda: False) == {}

    def boom():
        raise RuntimeError("boom")

    assert connector_describe(
        ["connectors__g__T"], availability=lambda: True, client_factory=boom
    ) == {}
