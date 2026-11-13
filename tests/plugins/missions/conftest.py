"""Hermetic env for the missions test suite.

On hosts where the gateway exports ``HERMES_MISSION_PROFILE`` (and
``HERMES_SESSION_*`` routing ids) into the shell, the plugin honours those
values and tests that assume the ``assistant`` default profile fail. Strip
them before every test; tests that exercise the env explicitly (see
``test_mission_profile_handoff.py``) set it via ``monkeypatch.setenv`` in the
test body, which runs after this fixture.
"""

import pytest

# Everything plugins/missions/__init__.py reads from the environment:
# HERMES_MISSION_PROFILE directly, HERMES_SESSION_KEY via os.environ, and
# HERMES_SESSION_ID via gateway.session_context.get_session_env (ContextVar
# with an os.environ fallback).
_MISSION_ENV_VARS = (
    "HERMES_MISSION_PROFILE",
    "HERMES_SESSION_KEY",
    "HERMES_SESSION_ID",
)


@pytest.fixture(autouse=True)
def _hermetic_mission_env(monkeypatch):
    for var in _MISSION_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
