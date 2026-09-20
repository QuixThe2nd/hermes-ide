from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class PapercutsPluginManagerTests(unittest.TestCase):
    def test_plugin_manager_registers_papercuts_tool(self):
        yaml = __import__("yaml")
        del yaml  # ensure Hermes runtime dependency is present

        from hermes_cli.plugins import PluginManager
        from tools.registry import registry

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes-home"
            plugins_root = home / "plugins"
            plugins_root.mkdir(parents=True)
            shutil.copytree(
                PLUGIN_ROOT,
                plugins_root / "papercuts",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            import os

            os.environ["HERMES_HOME"] = str(home)
            os.environ["HERMES_ENABLE_PROJECT_PLUGINS"] = "0"

            entries_before = {entry.name: entry for entry in registry._snapshot_entries()}
            manager = PluginManager()
            manifests = manager._scan_directory(plugins_root, source="user")
            self.assertEqual(len(manifests), 1)
            manifest = manifests[0]
            self.assertEqual(manifest.name, "papercuts")

            manager._load_plugin(manifest)
            loaded = manager._plugins["papercuts"]
            self.assertTrue(loaded.enabled)
            self.assertFalse(loaded.error)
            self.assertIn("papercuts", loaded.tools_registered)

            entry = registry.get_entry("papercuts")
            self.assertIsNotNone(entry)
            self.assertEqual(entry.toolset, "papercuts")

            entries_after = {entry.name: entry for entry in registry._snapshot_entries()}
            for name in set(entries_before) | set(entries_after):
                previous = entries_before.get(name)
                current = entries_after.get(name)
                if current is not previous:
                    if previous is None:
                        registry._tools.pop(name, None)
                    else:
                        registry._tools[name] = previous


if __name__ == "__main__":
    unittest.main(verbosity=2)
