"""User-facing stalled delegation text avoids internal implementation jargon."""


def test_stalled_delegation_text_has_no_issue_numbers_or_worker_jargon():
    from tools.async_delegation import _stalled_error_text

    text = _stalled_error_text({
        "goal": "Summarise the  weekly report",
        "_stall_quiet_seconds": 610.0,
    })

    assert text.startswith('Background task "Summarise the weekly report"')
    assert "10 min" in text and "run it again" in text
    for banned in ("#60203", "completion event", "wedged", "Re-dispatch", "delegation"):
        assert banned not in text
