"""The 1password optional skill is not shipped. Bitwarden is the password manager."""
from pathlib import Path

def test_1password_optional_skill_is_absent() -> None:
    repo = Path(__file__).resolve().parents[2]
    assert not (repo / "optional-skills" / "security" / "1password").exists()
