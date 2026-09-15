from __future__ import annotations

import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path


PLUGIN = Path(__file__).with_name("__init__.py")
spec = importlib.util.spec_from_file_location("papercuts_plugin_under_test", PLUGIN)
assert spec and spec.loader
papercuts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(papercuts)


class PapercutsPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["HERMES_PAPERCUTS_DIR"] = self.tmp.name

    def tearDown(self) -> None:
        os.environ.pop("HERMES_PAPERCUTS_DIR", None)
        self.tmp.cleanup()

    def call(self, args, *, session="s1", turn="t1"):
        return json.loads(
            papercuts.handle_papercuts(
                args,
                session_id=session,
                turn_id=turn,
                task_id=session,
                tool_call_id=f"tc-{turn}",
            )
        )

    def log(self, summary="config lookup is awkward", *, turn="t1", **overrides):
        payload = {
            "action": "log",
            "summary": summary,
            "observed": "the cli lacked a direct key lookup",
            "workaround": "loaded config through python",
            "suggested_fix": "add a config get subcommand",
            "severity": "minor",
            "category": "tool",
            "component": "hermes config",
        }
        payload.update(overrides)
        return self.call(payload, turn=turn)

    def test_log_deduplicate_resolve_reopen_and_stats(self):
        first = self.log()
        self.assertTrue(first["success"])
        self.assertFalse(first["deduplicated"])
        item_id = first["item"]["id"]

        duplicate = self.log(turn="t2")
        self.assertTrue(duplicate["deduplicated"])
        self.assertEqual(duplicate["item"]["occurrences"], 2)

        resolved = self.call(
            {"action": "resolve", "id": item_id[:7], "note": "implemented"},
            turn="t3",
        )
        self.assertEqual(resolved["item"]["status"], "resolved")

        reopened = self.log(turn="t4")
        self.assertTrue(reopened["reopened"])
        self.assertEqual(reopened["item"]["status"], "open")
        self.assertEqual(reopened["item"]["occurrences"], 3)

        stats = self.call({"action": "stats"}, turn="t5")
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["by_status"]["open"], 1)
        self.assertEqual(stats["occurrences"], 3)

    def test_one_new_cut_per_turn(self):
        self.assertTrue(self.log()["success"])
        second = self.log(summary="another unrelated failure", turn="t1")
        self.assertFalse(second["success"])
        self.assertEqual(second["error"]["code"], "turn_limit")

    def test_secret_redaction_and_file_permissions(self):
        result = self.log(
            observed="request failed with api_key=super-secret-value and Bearer abc.def.ghi",
        )
        rendered = json.dumps(result)
        self.assertNotIn("super-secret-value", rendered)
        self.assertNotIn("abc.def.ghi", rendered)
        mode = stat.S_IMODE((Path(self.tmp.name) / "events.jsonl").stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_list_ignore_and_torn_line_tolerance(self):
        first = self.log()
        item_id = first["item"]["id"]
        events = Path(self.tmp.name) / "events.jsonl"
        with events.open("a", encoding="utf-8") as fh:
            fh.write("{torn\n")

        ignored = self.call(
            {"action": "ignore", "id": item_id, "note": "expected behavior"},
            turn="t2",
        )
        self.assertEqual(ignored["item"]["status"], "ignored")
        listed = self.call({"action": "list", "status": "ignored"}, turn="t3")
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["items"][0]["id"], item_id)

    def test_validation(self):
        missing = self.call({"action": "log"})
        self.assertEqual(missing["error"]["code"], "invalid_input")
        bad_action = self.call({"action": "explode"})
        self.assertEqual(bad_action["error"]["code"], "invalid_input")


if __name__ == "__main__":
    unittest.main(verbosity=2)
