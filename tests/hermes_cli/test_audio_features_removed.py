"""Audio features are removed. Legacy config keys must still load.

Absence assertions fail while the STT pipeline remains and pass after deletion.
"""
import importlib
import importlib.util
from pathlib import Path

import pytest

from hermes_cli.config_defaults import DEFAULT_CONFIG

_REPO_ROOT = str(Path(__file__).resolve().parents[2]) + "/"

_REMOVED_MODULES = (
    "tools.tts_tool",
    "tools.voice_mode",
    "tools.transcription_tools",
    "tools.transcription_command",
    "gateway.run_voice",
)

_REMOVED_CALLABLES = (
    ("gateway.run", "_event_media_is_stt_input"),
    ("gateway.run", "_probe_audio_duration"),
    ("gateway.run", "GatewayRunner", "_transcribe_and_echo_pending_voice"),
    ("gateway.run", "GatewayRunner", "_enrich_message_with_transcription"),
    ("gateway.run", "GatewayRunner", "_pending_event_audio_paths"),
)


def _find_spec_local(mod):
    spec = importlib.util.find_spec(mod)
    if spec is None or not spec.origin:
        return None
    return spec if str(spec.origin).startswith(_REPO_ROOT) else None


@pytest.mark.parametrize("mod", _REMOVED_MODULES)
def test_audio_module_is_gone(mod):
    assert _find_spec_local(mod) is None, mod


@pytest.mark.parametrize("parts", _REMOVED_CALLABLES)
def test_audio_callable_is_gone(parts):
    mod_name, *attrs = parts
    obj = importlib.import_module(mod_name)
    for attr in attrs[:-1]:
        obj = getattr(obj, attr)
    assert not hasattr(obj, attrs[-1]), f"{mod_name}.{'.'.join(attrs)} still present"


def test_voice_fx_and_audio_tags_absent():
    repo = Path(_REPO_ROOT)
    assert not (repo / "plugins/platforms/discord/voice_fx.py").exists()
    needles = ("voice_fx", "tts_audio_tags")
    for rel in (
        "hermes_cli/config_defaults.py",
        "agent/auxiliary_client.py",
    ):
        text = (repo / rel).read_text(encoding="utf-8")
        for needle in needles:
            assert needle not in text, f"{needle} still in {rel}"
    discord = repo / "plugins/platforms/discord"
    for path in discord.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "voice_fx" not in text, path


def test_defaults_have_no_audio_feature_keys():
    assert "tts" not in DEFAULT_CONFIG
    assert "stt" not in DEFAULT_CONFIG
    assert "voice" not in DEFAULT_CONFIG
    auxiliary = DEFAULT_CONFIG.get("auxiliary") or {}
    assert "tts_audio_tags" not in auxiliary


def test_legacy_audio_config_keys_still_load(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "model:\n"
        "  default: test/custom\n"
        "tts:\n"
        "  provider: edge\n"
        "stt:\n"
        "  provider: local\n"
        "  echo_transcripts: true\n"
        "voice:\n"
        "  record_key: ctrl+b\n"
        "  auto_tts: false\n"
        "discord:\n"
        "  voice_fx:\n"
        "    enabled: true\n"
        "auxiliary:\n"
        "  tts_audio_tags:\n"
        "    provider: auto\n"
        "    model: \"\"\n"
    )
    from hermes_cli.config import load_config
    from gateway.config import GatewayConfig

    config = load_config()
    assert config["model"]["default"] == "test/custom"
    assert config["tts"]["provider"] == "edge"
    assert config["stt"]["provider"] == "local"
    GatewayConfig.from_dict({
        "stt": {"echo_transcripts": True},
        "stt_echo_transcripts": False,
        "discord": {"voice_fx": {"enabled": True}},
    })
