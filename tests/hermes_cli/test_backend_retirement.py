"""Cooperative retirement is a reservation, never a sampled idle verdict."""

def test_only_uncommitted_permits_expire_and_unreadable_work_never_grants(monkeypatch):
    from hermes_cli.backend_retirement import RetirementFence, idle_proof as _idle_proof  # noqa: F401

    fence = RetirementFence()
    now = [100.0]
    monkeypatch.setattr(fence, "_now", lambda: now[0], raising=False)
    first = fence.prepare()["token"]
    now[0] += 30.0
    assert fence.commit(first) == {"ok": False}
    assert fence.cancel(first) == {"ok": False}
    with fence.work() as admitted:
        assert admitted
        assert fence.prepare() == {"ok": False, "idle": False}
    second = fence.prepare()["token"]
    assert first != second
    assert RetirementFence().commit(second) == {"ok": False}
    now[0] += 29.0
    assert fence.commit(second) == {"ok": True}
    now[0] += 3600.0
    assert fence.commit(second) == {"ok": True}
    assert fence.prepare() == {"ok": True, "idle": True, "token": second}
    assert fence.cancel(second) == {"ok": False}
    with fence.work() as admitted:
        assert not admitted
    import hermes_cli.backend_retirement as br
    monkeypatch.setattr(br, "idle_proof", lambda: {"idle": None})
    assert RetirementFence().prepare() == {"ok": False, "idle": None}
