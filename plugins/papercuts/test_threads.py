"""Per-papercut inbox threads: fold, sightings, advance, steer, close.

Discord is faked by patching the PUBLIC hermes_starts helpers
(``plugins.hermes_starts.threads``); nothing here touches the network.
These tests require the threads feature and therefore fail on the base
revision (no such modules/actions exist there).
"""
from __future__ import annotations

import contextlib
import io

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import plugins.papercuts as papercuts
from plugins.papercuts import lifecycle
from plugins.papercuts import threads as pc_threads


class FakeDiscord:
    def __init__(self):
        self.calls = []
        self.next_thread = 1000

    def open_inbox_thread(self, title, body):
        self.next_thread += 1
        thread_id = f"th{self.next_thread}"
        self.calls.append(("open", thread_id, title, body))
        return {"thread_id": thread_id, "message_id": f"m{thread_id}", "url": f"https://discord.com/channels/g/{thread_id}"}

    def post_to_thread(self, thread_id, content):
        self.calls.append(("post", thread_id, content))
        return f"msg{len(self.calls)}"

    def update_thread(self, thread_id, name=None, archived=None):
        self.calls.append(("update", thread_id, name, archived))
        return True

    def opened(self):
        return [c for c in self.calls if c[0] == "open"]

    def posted(self):
        return [c for c in self.calls if c[0] == "post"]

    def updates(self):
        return [c for c in self.calls if c[0] == "update"]


class ThreadsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = tempfile.TemporaryDirectory()
        os.environ["HERMES_PAPERCUTS_DIR"] = self.tmp.name
        os.environ["HERMES_HOME"] = self.home.name
        Path(self.home.name, "config.yaml").write_text(
            "papercuts:\n  threads:\n    enabled: true\n", encoding="utf-8"
        )
        self.discord = FakeDiscord()
        import plugins.hermes_starts.threads as hs_threads

        self.real_post_to_thread = hs_threads.post_to_thread
        patcher = mock.patch.multiple(
            hs_threads,
            open_inbox_thread=self.discord.open_inbox_thread,
            post_to_thread=self.discord.post_to_thread,
            update_inbox_thread=self.discord.update_thread,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.launches = []
        patcher = mock.patch.object(lifecycle, "_launch_run", side_effect=self._fake_launch)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Thread openings may use an auxiliary model, but the tick must stay offline-safe.
        patcher = mock.patch("agent.auxiliary_client.call_llm", side_effect=RuntimeError("offline"))
        patcher.start()
        self.addCleanup(patcher.stop)
        # The opening rewrite retries once after a 2s pause; never sleep in tests.
        patcher = mock.patch.object(pc_threads, "time")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        os.environ.pop("HERMES_PAPERCUTS_DIR", None)
        os.environ.pop("HERMES_HOME", None)
        self.tmp.cleanup()
        self.home.cleanup()

    def _fake_launch(self, item, stage, prompt_text):
        self.launches.append((str(item["id"]), stage, prompt_text))
        from plugins.papercuts.lifecycle import _pidfile

        _pidfile(str(item["id"]), stage).parent.mkdir(parents=True, exist_ok=True)
        _pidfile(str(item["id"]), stage).write_text(f"{os.getpid()}\n", encoding="utf-8")
        return os.getpid()

    def call(self, args, **kwargs):
        return json.loads(papercuts.handle_papercuts(args, **kwargs))

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
        return self.call(payload, session_id="s1", turn_id=turn, task_id="s1", tool_call_id=f"tc-{turn}")


class ThreadBodyTests(unittest.TestCase):
    def setUp(self):
        self.item = {
            "summary": "Status checks keep forcing a detour",
            "observed": "session_id=agent:main:discord:thread:123; goal_active=False",
            "component": "auto-goal",
            "severity": "minor",
            "occurrences": 6,
            # Present in the record (and the stage prompt) but never in the opening.
            "suggested_fix": "cache the status probe for a minute",
            "session_id": "agent:main:discord:thread:123",
        }
        patcher = mock.patch("agent.auxiliary_client.call_llm")
        self.call_llm = patcher.start()
        self.addCleanup(patcher.stop)
        time_patcher = mock.patch.object(pc_threads, "time")
        self.time = time_patcher.start()
        self.addCleanup(time_patcher.stop)

    @staticmethod
    def response(text):
        return {"choices": [{"message": {"content": text}}]}

    def assert_opening_shape(self, body):
        """The opening hook plus exactly ONE compact detail line, nothing else."""
        self.assertEqual(
            len(body.splitlines()), 3, "opening, blank line, detail line only"
        )
        self.assertIn(
            "Component: `auto-goal` · Severity: minor · Times seen: 6",
            body.splitlines(),
        )
        self.assertNotIn("Suggested next step", body)
        self.assertNotIn("First seen in session", body)

    def assert_fallback(self, body):
        opening = body.splitlines()[0]
        self.assertTrue(opening.startswith("Status checks keep forcing a detour."))
        self.assertIn("It has happened 6 times", opening)
        self.assert_opening_shape(body)
        self.assertNotIn("What happens:", body)
        self.assertNotIn(self.item["observed"], body)

    def test_deterministic_fallback_shape(self):
        self.call_llm.return_value = self.response("")

        self.assert_fallback(pc_threads.thread_body(self.item))

    def test_acceptable_model_output_leads_the_opening(self):
        rewritten = "Recurring status checks keep breaking your flow. Fixing them would remove a repeated detour."
        self.call_llm.return_value = self.response(rewritten)

        body = pc_threads.thread_body(self.item)

        self.assertEqual(body.splitlines()[0], rewritten)
        self.assert_opening_shape(body)
        self.assertNotIn(self.item["observed"], body)

    def test_opening_uses_the_configured_dedup_aux_task(self):
        # A papercut-specific task is unconfigured: it falls through to the
        # main provider with no fallback chain — the 2026-11-12 outage shape.
        self.call_llm.return_value = self.response("Fine.")

        pc_threads.thread_body(self.item)

        kwargs = self.call_llm.call_args.kwargs
        self.assertEqual(kwargs["task"], "hermes_starts_dedup")
        self.assertEqual(kwargs["timeout"], 45.0)

    def test_model_output_echoing_observed_data_is_rejected(self):
        self.call_llm.return_value = self.response(
            f"Status checks keep breaking your flow. {self.item['observed']}"
        )

        self.assert_fallback(pc_threads.thread_body(self.item))

    def test_overlong_model_output_is_rejected(self):
        self.call_llm.return_value = self.response("A" * 401)

        self.assert_fallback(pc_threads.thread_body(self.item))

    def test_model_error_retries_once_then_uses_rewrite(self):
        rewritten = "Recurring status checks keep breaking your flow. Fixing them would remove a repeated detour."
        self.call_llm.side_effect = [
            RuntimeError("auxiliary model unavailable"),
            self.response(rewritten),
        ]

        body = pc_threads.thread_body(self.item)

        self.assertEqual(body.splitlines()[0], rewritten)
        self.assertEqual(self.call_llm.call_count, 2)
        self.time.sleep.assert_called_once_with(2.0)

    def test_two_model_errors_use_fallback_without_raising(self):
        self.call_llm.side_effect = [
            RuntimeError("auxiliary model unavailable"),
            TimeoutError("provider timed out"),
        ]

        self.assert_fallback(pc_threads.thread_body(self.item))
        self.assertEqual(self.call_llm.call_count, 2)
        self.time.sleep.assert_called_once_with(2.0)


class FoldTests(ThreadsTestCase):
    def test_legacy_records_fold_without_migration(self):
        events = [
            {"kind": "cut", "id": "pc_aaa", "ts": "t", "ts_epoch": 1.0},
            {"kind": "cut", "id": "pc_bbb", "ts": "t", "ts_epoch": 2.0},
            {"kind": "resolve", "id": "pc_bbb", "note": "done", "ts": "t2", "ts_epoch": 3.0},
        ]
        items = papercuts._fold(events)
        self.assertEqual(items["pc_aaa"]["stage"], "queued")
        self.assertEqual(items["pc_bbb"]["stage"], "closed")

    def test_stage_events_fold(self):
        base = {"kind": "cut", "id": "pc_aaa", "ts": "t", "ts_epoch": 1.0}
        items = papercuts._fold(
            [
                base,
                {"kind": "stage", "id": "pc_aaa", "stage": "triage", "ts_epoch": 2.0, "thread_id": "th1", "attempts": 1},
                {"kind": "steer", "id": "pc_aaa", "message": "check the new parser", "ts_epoch": 2.5},
                {"kind": "stage", "id": "pc_aaa", "stage": "fixing", "ts_epoch": 3.0, "pr_url": "https://github.com/o/r/pull/1"},
            ]
        )
        item = items["pc_aaa"]
        self.assertEqual(item["stage"], "fixing")
        self.assertEqual(item["thread_id"], "th1")
        self.assertEqual(item["pr_url"], "https://github.com/o/r/pull/1")
        self.assertEqual(item["stage_since"], 3.0)
        # steer notes reset at each stage change
        self.assertEqual(item["steer_notes"], [])

    def test_resolve_maps_to_closed_stage(self):
        first = self.log()
        result = self.call({"action": "resolve", "id": first["item"]["id"], "note": "fixed"}, turn="t2")
        self.assertEqual(result["item"]["stage"], "closed")
        self.assertEqual(result["item"]["status"], "resolved")


class TickTests(ThreadsTestCase):
    def seed(self, count, **log_kwargs):
        ids = []
        for i in range(count):
            r = self.log(summary=f"friction number {chr(ord('a') + i)}", turn=f"t{i}")
            ids.append(r["item"]["id"])
        return ids

    def test_tick_fills_max_active_in_priority_order(self):
        # b is seen 3 times and major → highest priority; a/c once each.
        self.log(summary="alpha friction", turn="t0")
        for turn in ("t1", "t2", "t3"):
            self.log(summary="beta friction hard", turn=turn, severity="major")
        self.log(summary="gamma friction", turn="t4")

        rc = lifecycle.tick()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.discord.opened()), 3, "max_active default is 3")
        first_title = self.discord.opened()[0][2]
        self.assertIn("beta", first_title)
        # one thread per papercut, and one triage launch each
        thread_ids = [c[1] for c in self.discord.opened()]
        self.assertEqual(len(set(thread_ids)), 3)
        self.assertEqual([l[1] for l in self.launches], ["triage"] * 3)
        # fold now shows threads + attempts
        items = papercuts._fold(papercuts._read_events_unlocked())
        threaded = [i for i in items.values() if i.get("thread_id")]
        self.assertEqual(len(threaded), 3)
        for item in threaded:
            self.assertEqual(item["stage"], "triage")
            self.assertEqual(item["attempts"], 1)

    def test_opening_leads_with_a_plain_language_hook(self):
        self.log(summary="Config lookup keeps taking unnecessary detours", turn="t0")

        lifecycle.tick()

        body = self.discord.opened()[0][3]
        first_line = body.splitlines()[0]
        self.assertIn("Config lookup keeps taking unnecessary detours", first_line)
        self.assertIn(". ", first_line, "the hook should explain why the owner should care")
        self.assertFalse(first_line.startswith("Observed:"))
        self.assertLess(first_line.find("Config lookup"), body.find("Component:"))

    def test_tick_idempotent_while_runs_live(self):
        self.seed(5)
        lifecycle.tick()
        first = (len(self.discord.calls), len(self.launches))
        lifecycle.tick()
        self.assertEqual((len(self.discord.calls), len(self.launches)), first)

    def test_stage_timeout_relaunches_and_max_attempts_needs_human(self):
        self.seed(1)
        lifecycle.tick()  # triage, attempt 1
        item_id = self.launches[0][0]
        # Simulate the run dying and the stage going stale.
        from plugins.papercuts.lifecycle import _pidfile

        _pidfile(item_id, "triage").unlink()
        events_path = Path(self.tmp.name) / "events.jsonl"
        stale = json.dumps({"kind": "stage", "id": item_id, "stage": "triage", "ts": "x", "ts_epoch": 0.0, "attempts": 1})
        with events_path.open("a", encoding="utf-8") as fh:
            fh.write(stale + "\n")

        lifecycle.tick()  # relaunch, attempt 2
        self.assertEqual(len(self.launches), 2)
        _pidfile(item_id, "triage").unlink()
        with events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "stage", "id": item_id, "stage": "triage", "ts": "x", "ts_epoch": 0.0, "attempts": 2}) + "\n")
        lifecycle.tick()  # relaunch, attempt 3 = max_attempts
        self.assertEqual(len(self.launches), 3)
        _pidfile(item_id, "triage").unlink()

        lifecycle.tick()  # attempts exhausted → needs_human
        items = papercuts._fold(papercuts._read_events_unlocked())
        self.assertEqual(items[item_id]["stage"], "needs_human")
        renames = [c for c in self.discord.updates() if c[2] and c[2].startswith("🙋")]
        self.assertTrue(renames, "needs_human must rename the thread with the 🙋 prefix")
        self.assertTrue(any("needs a human" in c[2] for c in self.discord.posted()))

    def test_disabled_tick_is_a_noop(self):
        Path(self.home.name, "config.yaml").write_text(
            "papercuts:\n  threads:\n    enabled: false\n", encoding="utf-8"
        )
        self.seed(2)
        rc = lifecycle.tick()
        self.assertEqual(rc, 0)
        self.assertEqual(self.discord.calls, [])
        self.assertEqual(self.launches, [])
        items = papercuts._fold(papercuts._read_events_unlocked())
        self.assertTrue(all(i["stage"] == "queued" for i in items.values()))
        # log/resolve still work while disabled
        resolved = self.call({"action": "resolve", "id": next(iter(items)), "note": "x"}, turn="tz")
        self.assertTrue(resolved["success"])

    def test_dry_run_has_no_side_effects(self):
        self.seed(5)
        events_before = (Path(self.tmp.name) / "events.jsonl").read_text(encoding="utf-8")
        rc = lifecycle.tick(dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.discord.calls, [])
        self.assertEqual(self.launches, [])
        self.assertEqual(
            (Path(self.tmp.name) / "events.jsonl").read_text(encoding="utf-8"), events_before
        )


class SightingTests(ThreadsTestCase):
    def test_repeat_log_posts_seen_again_to_thread(self):
        first = self.log(turn="t0")
        lifecycle.tick()  # threads it into triage
        again = self.log(turn="t1")
        self.assertTrue(again["deduplicated"])
        sightings = [c for c in self.discord.posted() if "seen again" in c[2]]
        self.assertEqual(len(sightings), 1)
        self.assertIn("the cli lacked a direct key lookup", sightings[0][2])

    def test_repeat_log_on_closed_reopens_same_thread(self):
        first = self.log(turn="t0")
        item_id = first["item"]["id"]
        lifecycle.tick()
        thread_id = [c[1] for c in self.discord.opened()][0]
        self.call({"action": "resolve", "id": item_id, "note": "fixed upstream"}, turn="t2")
        unarchives = [c for c in self.discord.updates() if c[3] is True]
        self.assertTrue(unarchives, "closing must archive the thread")

        again = self.log(turn="t3")
        self.assertTrue(again["reopened"])
        self.assertEqual(len(self.discord.opened()), 1, "never a second thread")
        self.assertTrue(any("reopened" in c[2] for c in self.discord.posted()))
        reopens = [c for c in self.discord.updates() if c[3] is False]
        self.assertTrue(reopens, "reopening must unarchive the same thread")
        items = papercuts._fold(papercuts._read_events_unlocked())
        self.assertTrue(any("seen again ×2" in c[2] for c in self.discord.posted()))
        self.assertEqual(again["item"]["stage"], "triage")
        self.assertEqual(items[item_id]["stage"], "triage")


class AdvanceSteerTests(ThreadsTestCase):
    def threaded_item(self):
        first = self.log(turn="t0")
        lifecycle.tick()
        return first["item"]["id"], [c[1] for c in self.discord.opened()][0]

    def test_advance_records_posts_and_archives_on_close(self):
        item_id, thread_id = self.threaded_item()
        result = self.call(
            {
                "action": "advance",
                "id": item_id,
                "to_stage": "fixing",
                "note": "fix brief: touch X",
            },
            turn="t2",
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["item"]["stage"], "fixing")
        self.assertTrue(any("fix brief" in c[2] for c in self.discord.posted()))

        closed = self.call(
            {"action": "advance", "id": item_id, "to_stage": "closed", "note": "merged as abc123"},
            turn="t3",
        )
        self.assertEqual(closed["item"]["stage"], "closed")
        self.assertTrue(any(c[3] is True for c in self.discord.updates()), "closed must archive")

    def test_close_posts_exactly_one_transition_message(self):
        item_id, thread_id = self.threaded_item()
        before = len(self.discord.posted())

        closed = self.call(
            {"action": "advance", "id": item_id, "to_stage": "closed", "note": "Users can sign in again."},
            turn="t3",
        )

        self.assertTrue(closed["success"])
        new_posts = [c for c in self.discord.posted()[before:] if c[1] == thread_id]
        self.assertEqual(len(new_posts), 1)
        self.assertIn("Users can sign in again.", new_posts[0][2])

    def test_long_advance_note_is_split_with_full_content_preserved(self):
        item_id, thread_id = self.threaded_item()
        note = ("The sign-in flow works again after the small routing fix. " * 60).strip()
        before = len(self.discord.posted())
        import plugins.hermes_starts.threads as hs_threads

        with mock.patch.object(hs_threads, "post_to_thread", self.real_post_to_thread), \
                mock.patch("plugins.hermes_starts._read_discord_token", return_value="token"), \
                mock.patch("plugins.hermes_starts._post_channel_message", side_effect=lambda _token, target, content: self.discord.post_to_thread(target, content)):
            result = self.call(
                {"action": "advance", "id": item_id, "to_stage": "fixing", "note": note},
                turn="t3",
            )

        self.assertTrue(result["success"])
        self.assertEqual(papercuts._read_events_unlocked()[-1]["note"], note)
        new_posts = [c[2] for c in self.discord.posted()[before:] if c[1] == thread_id]
        self.assertGreater(len(new_posts), 1)
        self.assertTrue(all(len(part) <= 2000 for part in new_posts))
        self.assertEqual("".join(new_posts), f"→ fixing: {note}")
        self.assertNotIn("…", "".join(new_posts))

    def test_advance_to_verifying_carries_pr_url(self):
        item_id, _ = self.threaded_item()
        result = self.call(
            {
                "action": "advance",
                "id": item_id,
                "to_stage": "verifying",
                "note": "PR open",
                "pr_url": "https://github.com/o/r/pull/9",
            },
            turn="t2",
        )
        self.assertEqual(result["item"]["pr_url"], "https://github.com/o/r/pull/9")
        self.assertTrue(any("pull/9" in c[2] for c in self.discord.posted()))

    def test_advance_rejects_bad_stage(self):
        item_id, _ = self.threaded_item()
        bad = self.call({"action": "advance", "id": item_id, "to_stage": "warp"}, turn="t2")
        self.assertEqual(bad["error"]["code"], "invalid_input")

    def test_steer_records_note_and_unparks_needs_human(self):
        item_id, thread_id = self.threaded_item()
        self.call({"action": "advance", "id": item_id, "to_stage": "needs_human", "note": "stuck"}, turn="t2")
        steered = self.call({"action": "steer", "id": item_id, "message": "use the new parser"}, turn="t3")
        self.assertTrue(steered["success"])
        self.assertEqual(steered["item"]["stage"], "triage")
        self.assertEqual(steered["item"]["steer_notes"][0]["message"], "use the new parser")
        # steer notes reach the next work order's prompt
        lifecycle._pidfile(item_id, "triage").unlink(missing_ok=True)
        prompt = lifecycle.render_stage_prompt(steered["item"], "triage")
        self.assertIn("use the new parser", prompt)


class LifecycleRegressionTests(ThreadsTestCase):
    def test_dead_run_waits_for_timeout_before_relaunch(self):
        item_id = self.seed(1)[0]
        lifecycle.tick()
        lifecycle._pidfile(item_id, "triage").unlink()
        first = papercuts._fold(papercuts._read_events_unlocked())[item_id]
        launched_at = first["stage_since"]

        lifecycle.tick(now=launched_at + 1)
        self.assertEqual(len(self.launches), 1)
        lifecycle.tick(now=launched_at + 120 * 60 + 1)
        self.assertEqual(len(self.launches), 2)

    def test_new_stage_resets_attempt_budget(self):
        item_id, _thread_id = self.threaded_item()
        advanced = self.call(
            {"action": "advance", "id": item_id, "to_stage": "fixing", "note": "brief"}
        )
        self.assertEqual(advanced["item"]["attempts"], 0)

    def test_close_maps_resolved_and_repeat_advance_is_idempotent(self):
        item_id, _thread_id = self.threaded_item()
        args = {"action": "advance", "id": item_id, "to_stage": "closed", "note": "fixed"}
        closed = self.call(args)
        self.assertEqual(closed["item"]["status"], "resolved")
        before = Path(self.tmp.name, "events.jsonl").read_text(encoding="utf-8")
        duplicate = self.call(args)
        self.assertFalse(duplicate["changed"])
        self.assertEqual(Path(self.tmp.name, "events.jsonl").read_text(encoding="utf-8"), before)

    def test_verifying_requires_pr_url(self):
        item_id, _thread_id = self.threaded_item()
        result = self.call(
            {"action": "advance", "id": item_id, "to_stage": "verifying", "note": "PR opened"}
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "invalid_input")

    def test_steer_resumes_stage_that_needed_human(self):
        item_id, _thread_id = self.threaded_item()
        self.call({"action": "advance", "id": item_id, "to_stage": "fixing", "note": "brief"})
        self.call({"action": "advance", "id": item_id, "to_stage": "needs_human", "note": "owner?"})
        result = self.call({"action": "steer", "id": item_id, "message": "backend owns it"})
        self.assertEqual(result["item"]["stage"], "fixing")
        prompt = lifecycle.render_stage_prompt(result["item"])
        self.assertIn("backend owns it", prompt)

    def test_dry_run_without_store_creates_nothing(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(lifecycle.tick(dry_run=True), 0)
        self.assertIn("nothing to do", output.getvalue())
        # setUp's TemporaryDirectory already created the dir itself; the dry
        # run's contract is that it stays completely empty (no store created).
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_live_pid_helper_suppresses_due_relaunch(self):
        item_id = self.seed(1)[0]
        lifecycle.tick()
        lifecycle._pidfile(item_id, "triage").unlink()
        lifecycle._pidfile(item_id, "triage").write_text("4242\n", encoding="utf-8")
        with mock.patch.object(lifecycle, "_pid_exists", return_value=True):
            lifecycle.tick(now=10**10)
        self.assertEqual(len(self.launches), 1)


# Reuse the setup helpers without running their test classes a second time.
LifecycleRegressionTests.threaded_item = AdvanceSteerTests.threaded_item
LifecycleRegressionTests.seed = TickTests.seed


if __name__ == "__main__":
    unittest.main(verbosity=2)
