"""Baileys creds.json presence is not the same as a finished pairing."""

from gateway.platforms.whatsapp_common import whatsapp_session_is_paired


def test_missing_creds_are_unpaired(tmp_path):
    assert whatsapp_session_is_paired(tmp_path) is False


def test_empty_object_still_counts_as_legacy_paired(tmp_path):
    (tmp_path / "creds.json").write_text("{}", encoding="utf-8")
    assert whatsapp_session_is_paired(tmp_path) is True


def test_registered_false_is_unpaired(tmp_path):
    (tmp_path / "creds.json").write_text(
        '{"registered": false, "pairingCode": "ABCD-1234"}',
        encoding="utf-8",
    )
    assert whatsapp_session_is_paired(tmp_path) is False


def test_pairing_code_without_registered_true_is_unpaired(tmp_path):
    (tmp_path / "creds.json").write_text(
        '{"pairingCode": "ABCD-1234"}',
        encoding="utf-8",
    )
    assert whatsapp_session_is_paired(tmp_path) is False


def test_registered_true_is_paired(tmp_path):
    (tmp_path / "creds.json").write_text(
        '{"registered": true, "me": {"id": "15551234567:1@s.whatsapp.net"}}',
        encoding="utf-8",
    )
    assert whatsapp_session_is_paired(tmp_path) is True


def test_malformed_creds_are_unpaired(tmp_path):
    (tmp_path / "creds.json").write_text("{not-json", encoding="utf-8")
    assert whatsapp_session_is_paired(tmp_path) is False
