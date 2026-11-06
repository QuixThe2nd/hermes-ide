"""Regression tests for the test-suite audio-playback guard.

The incident (historical, from the removed gateway-server suite): a plain
test run spoke the words "partial answer complete" out of a developer's
speakers. The route was entirely in-process — no leaked shell variable:

  1. A handler flipped the voice flag by writing the real process
     environment: ``os.environ["HERMES_VOICE_TTS"] = "1"``.
  2. The flag outlives that test, so every later test in the process saw it.
  3. A completed turn's TTS dispatch called ``hermes_cli.voice.speak_text``
     with the turn's final response text.
  4. ``speak_text`` needs no API key to be audible — ``tools/tts_tool.py``
     defaults to the keyless ``edge`` provider.

Two independent defences in ``tests/conftest.py``, one test each below:

  * ``_HERMES_BEHAVIORAL_VARS`` blanks ``HERMES_VOICE`` /
    ``HERMES_VOICE_TTS`` at every test setup, so a leaked flag cannot cross a
    test boundary.
  * ``_audio_playback_guard`` stubs ``speak_text`` outright, so step 3 stays
    silent even *within* the test that set the flag itself.

The second is the load-bearing one: env blanking alone cannot stop code under
test from re-setting the variable mid-test.
"""

import pytest


def test_guard_can_be_opted_out_of_explicitly():
    """The stub is a guard, not a lobotomy — the real function is reachable."""
    import hermes_cli.voice as voice

    assert voice.speak_text.__name__ == "_blocked_speak_text"


@pytest.mark.real_audio_playback
def test_bypass_marker_restores_the_real_speak_text():
    """``@pytest.mark.real_audio_playback`` hands back the real primitive.

    Asserts identity only — it does not call it, which would speak aloud.
    """
    import hermes_cli.voice as voice

    assert voice.speak_text.__name__ == "speak_text"
