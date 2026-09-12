"""Cron delivery contract: a bare-platform (or origin-without-origin) deliver
value resolves no target and records an actionable delivery error — never a
silent drop, never a fallback destination."""

import logging

import pytest

from cron import scheduler_delivery as delivery
from cron.scheduler import _classify_delivery_outcome


@pytest.fixture(autouse=True)
def _isolated_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("_HERMES_CRON_EXTERNAL_WORKER", raising=False)
    monkeypatch.setattr(delivery._sched, "load_config", lambda: {})
    updates = []
    monkeypatch.setattr(
        delivery, "_record_delivery_verification",
        lambda job, targets: updates.append((job.get("id"), list(targets))),
    )
    return updates


def test_bare_platform_deliver_records_delivery_error(caplog):
    job = dict(id="digest", name="Digest", deliver="discord")

    with caplog.at_level(logging.WARNING, logger=delivery.logger.name):
        error = delivery._deliver_result(job, "Nightly report.")

    assert error is not None
    assert "hermes cron edit digest" in error
    assert "--deliver platform:chat_id[:thread_id]" in error
    assert "discord" in error
    assert any(
        "no delivery target resolved" in r.getMessage() for r in caplog.records
    )
    # The run path files this as a delivery failure, not a silent drop.
    assert _classify_delivery_outcome(
        delivery_error=error, should_deliver=True, unresolved_origin=False,
        normalized_deliver="discord", incident_acked=False, success=True,
    ) == "failed"


def test_originless_origin_deliver_records_delivery_error():
    job = dict(id="apijob", name="API job", deliver="origin")  # no captured origin

    error = delivery._deliver_result(job, "Output.")

    assert error is not None
    assert "no captured origin" in error
    assert "hermes cron edit apijob" in error
    assert _classify_delivery_outcome(
        delivery_error=error, should_deliver=True, unresolved_origin=True,
        normalized_deliver="origin", incident_acked=False, success=True,
    ) == "failed"


def test_local_deliver_stays_silent():
    job = dict(id="localjob", name="Local", deliver="local")

    assert delivery._deliver_result(job, "Output.") is None
