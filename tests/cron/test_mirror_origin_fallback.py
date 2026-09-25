"""Mirror eligibility for origin and explicit cron delivery targets.

Field report (enterprise, 2026-08-17): `cron.mirror_delivery: true` with
`deliver: origin` delivered the brief to Slack but never appended it to the
reply-facing gateway session, so a user reply hit a session with no context.

Design under test:
- Delivery targets carry `_resolved_from` provenance used to determine mirror eligibility:
  * origin match            -> eligible
  * explicit platform:chat  -> eligible ONLY with per-job attach_to_session
    (opt-in; the global flag never activates explicit targets)
- There is no per-platform default destination: ``origin`` without a captured
  origin, a bare platform token, and ``all`` each resolve NO target (the
  unresolved-outcome contract lives in test_unresolved_delivery_contract.py).
- Dedup across tokens (e.g. "origin,slack:<origin chat>" in either order)
  OR-merges eligibility so token order cannot strip it.
- The in_channel flat-session seed requires a DM-shaped target or a known
  user_id: group-channel session keys are user-isolated, and a seed without
  user_id would create an orphan session no reply ever resolves to.
"""

import pytest

from cron.scheduler import _deliver_result, _resolve_delivery_targets
from cron.scheduler_delivery import _target_mirror_eligible


class TestMirrorEligibilityResolution:
    def test_origin_target_is_eligible(self):
        job = {
            "deliver": "origin",
            "origin": {"platform": "slack", "chat_id": "D0AAA", "chat_type": "dm"},
        }
        targets = _resolve_delivery_targets(job)
        assert len(targets) == 1
        assert _target_mirror_eligible(job, targets[0], global_mirror=True)

    def test_originless_origin_resolves_no_target(self):
        """No captured origin and no default destination: nothing to mirror."""
        job = {"deliver": "origin", "origin": None}
        assert _resolve_delivery_targets(job) == []

    def test_bare_platform_resolves_no_target(self):
        job = {"deliver": "slack", "origin": None}
        assert _resolve_delivery_targets(job) == []

    def test_all_resolves_no_target(self):
        """The old broadcast expansion depended on default destinations; it is gone."""
        job = {"deliver": "all", "origin": None}
        assert _resolve_delivery_targets(job) == []

    def test_explicit_target_not_eligible_under_global_flag(self):
        """Global mirror_delivery must not write sessions into arbitrary
        explicitly-addressed chats."""
        job = {"deliver": "slack:D0EXPL", "origin": None}
        targets = _resolve_delivery_targets(job)
        assert len(targets) == 1
        assert not _target_mirror_eligible(job, targets[0], global_mirror=True)

    def test_explicit_target_eligible_with_per_job_attach(self):
        """attach_to_session=true on the job is the author declaring the
        explicit target a conversation — managed per-user DM crons."""
        job = {
            "deliver": "slack:D0EXPL",
            "origin": None,
            "attach_to_session": True,
        }
        targets = _resolve_delivery_targets(job)
        assert len(targets) == 1
        assert _target_mirror_eligible(job, targets[0], global_mirror=False)

    @pytest.mark.parametrize("deliver", ["origin,slack:D0AAA", "slack:D0AAA,origin"])
    def test_dedup_origin_and_explicit_keeps_eligibility(self, deliver):
        """The same chat addressed both ways resolves to one target whose
        origin provenance (and eligibility) survives regardless of order."""
        job = {
            "deliver": deliver,
            "origin": {"platform": "slack", "chat_id": "D0AAA", "chat_type": "dm"},
        }
        targets = _resolve_delivery_targets(job)
        assert len(targets) == 1
        assert _target_mirror_eligible(job, targets[0], global_mirror=False)

    def test_explicit_other_chat_with_origin_not_eligible(self):
        """An explicit target that is NOT the origin stays unmirrored under
        the global flag even when the job has an origin elsewhere."""
        job = {
            "deliver": "slack:D0OTHER",
            "origin": {"platform": "slack", "chat_id": "D0AAA", "chat_type": "dm"},
        }
        targets = _resolve_delivery_targets(job)
        assert len(targets) == 1
        assert not _target_mirror_eligible(job, targets[0], global_mirror=True)


class TestFallbackMirrorEndToEnd:
    """Drive _deliver_result with a stubbed sender + mirror recorder."""

    @pytest.fixture()
    def slack_env(self, monkeypatch, tmp_path):
        home = tmp_path / "hermes-home"
        home.mkdir()
        (home / "config.yaml").write_text(
            "cron:\n  mirror_delivery: true\n"
            "platforms:\n  slack:\n    enabled: true\n    token: xoxb-test\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))

        send_calls = []

        async def fake_sender(pconfig, chat_id, message, *, thread_id=None,
                              media_files=None, force_document=False, caption=None):
            send_calls.append({"chat_id": chat_id, "thread_id": thread_id})
            return {"success": True, "chat_id": chat_id, "message_id": "1.2"}

        import gateway.platform_registry as reg
        import hermes_cli.plugins as hp

        entry = reg.platform_registry.get("slack")
        if entry is None:
            hp.discover_plugins()
            entry = reg.platform_registry.get("slack")
        if entry is None:
            pytest.skip("slack platform entry not registered")
        monkeypatch.setattr(entry, "standalone_sender_fn", fake_sender)
        monkeypatch.setattr(hp, "discover_plugins", lambda *a, **k: None)

        mirror_calls = []

        import cron.scheduler as sched

        def fake_mirror(platform, chat_id, text, source_label="cli",
                        thread_id=None, user_id=None, role="assistant"):
            mirror_calls.append({
                "platform": platform, "chat_id": chat_id,
                "thread_id": thread_id, "user_id": user_id, "role": role,
            })
            return True

        import gateway.mirror as mirror_mod

        real_mirror = mirror_mod.mirror_to_session
        monkeypatch.setattr(mirror_mod, "mirror_to_session", fake_mirror)
        return {"send": send_calls, "mirror": mirror_calls, "real_mirror": real_mirror, "home": home}

    def test_explicit_target_with_attach_mirrors(self, slack_env):
        job = {
            "id": "j3", "name": "managed-dm", "deliver": "slack:D0USER7",
            "origin": None, "attach_to_session": True,
        }
        err = _deliver_result(job, "managed brief", adapters=None, loop=None)
        assert err is None
        assert len(slack_env["send"]) == 1
        assert len(slack_env["mirror"]) == 1
        assert slack_env["mirror"][0]["chat_id"] == "D0USER7"

    def test_explicit_target_without_attach_does_not_mirror(self, slack_env):
        job = {
            "id": "j4", "name": "plain-explicit", "deliver": "slack:D0USER8",
            "origin": None,
        }
        err = _deliver_result(job, "plain text", adapters=None, loop=None)
        assert err is None
        assert len(slack_env["mirror"]) == 0

    def test_origin_job_still_mirrors_unchanged(self, slack_env):
        """Regression control: the June origin-scoped behavior is untouched."""
        job = {
            "id": "j5", "name": "origin-job", "deliver": "origin",
            "origin": {"platform": "slack", "chat_id": "D0AAA", "chat_type": "dm"},
        }
        err = _deliver_result(job, "origin brief", adapters=None, loop=None)
        assert err is None
        assert len(slack_env["mirror"]) == 1
        assert slack_env["mirror"][0]["chat_id"] == "D0AAA"


class TestInChannelSeedUserIdGuard:
    """Group-channel seeds are user-keyed; a seed with no user_id would create
    an orphan session. DM targets are safe (key has no user_id)."""

    def test_seed_requires_dm_or_user_id(self):
        from cron.scheduler_delivery import _inchannel_seed_allowed

        # DM-shaped chat, no user_id: allowed (DM keys don't embed user).
        assert _inchannel_seed_allowed(is_dm=True, user_id=None)
        # Group chat with known user: allowed.
        assert _inchannel_seed_allowed(is_dm=False, user_id="U123")
        # Group chat, no user: refused — would orphan the session.
        assert not _inchannel_seed_allowed(is_dm=False, user_id=None)
