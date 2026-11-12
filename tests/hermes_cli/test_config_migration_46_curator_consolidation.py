"""Migration contract for removing the retired curator consolidation toggle."""

from __future__ import annotations

import os
from unittest.mock import patch

import yaml


def test_consolidation_toggle_is_dropped_and_version_advances(tmp_path):
    from hermes_cli.config import migrate_config
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    previous_version = int(DEFAULT_CONFIG["_config_version"]) - 1
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({
            "_config_version": previous_version,
            "curator": {
                "enabled": True,
                "interval_hours": 48,
                "consolidate": False,
            },
        }),
        encoding="utf-8",
    )

    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        migrate_config(interactive=False, quiet=True)

    raw = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert raw["_config_version"] == DEFAULT_CONFIG["_config_version"]
    assert "consolidate" not in raw["curator"]
    assert raw["curator"]["enabled"] is True
    assert raw["curator"]["interval_hours"] == 48
