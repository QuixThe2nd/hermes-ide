"""ThreadParticipationTracker default capacity must survive busy guilds.

At 500 tracked threads the tracker forgot threads the bot participates in
on busy guilds, so it stopped answering un-pinged replies in older threads
(they no longer counted as "participated"). The default capacity is 20000.

Regression for the live-hotfix.
"""

from __future__ import annotations

from gateway.platforms.helpers import ThreadParticipationTracker


def test_default_tracker_retains_more_than_500_threads():
    tracker = ThreadParticipationTracker("discord")
    thread_ids = [f"thread-{i:04d}" for i in range(600)]
    for thread_id in thread_ids:
        tracker.mark(thread_id)

    assert all(thread_id in tracker for thread_id in thread_ids)
    assert "thread-0000" in tracker  # oldest survives, not just the tail

    # Persistence keeps the same capacity: a fresh instance still knows the
    # oldest threads instead of having truncated them away on save.
    assert "thread-0000" in ThreadParticipationTracker("discord")
