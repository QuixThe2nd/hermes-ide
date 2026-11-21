"""Platform enumeration in ``hermes_cli.gateway._all_platforms()``.

The gate lives in one place — ``_all_platforms()`` — so the setup wizard, the
curses gateway-config menu, and any future picker all see the same list.
"""

import pytest


class TestPlatformEnumeration:
    def test_matrix_present(self):
        """Matrix ships everywhere: python-olm has wheels on Linux and macOS."""
        import hermes_cli.gateway as gateway_mod

        platforms = gateway_mod._all_platforms()
        keys = {p["key"] for p in platforms}
        assert "matrix" in keys, "matrix must be available"

    def test_other_platforms_unaffected(self):
        """Enumeration surfaces the full shipping set."""
        import hermes_cli.gateway as gateway_mod

        platforms = gateway_mod._all_platforms()
        keys = {p["key"] for p in platforms}
        # A representative sample of the platforms that ship; #571 removed the
        # telegram/slack adapters, so none of those are expected to appear.
        for must_have in ("discord", "whatsapp", "browser_sidebar"):
            assert must_have in keys, f"{must_have} disappeared from the picker"
