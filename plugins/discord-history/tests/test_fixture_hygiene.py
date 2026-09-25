"""Prevent production-looking Discord snowflakes from entering fixtures."""
from __future__ import annotations

import re
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SNOWFLAKE = re.compile(r"(?<![0-9])[0-9]{17,20}(?![0-9])")
SEQUENTIAL_FIXTURE = re.compile(r"[1-4]2345678901234567")
ZERO_PADDED_FIXTURE = re.compile(r"[1-9]0{15,18}[1-9]")


def _is_obviously_synthetic(value: str) -> bool:
    """Accept repeated/patterned fixture IDs, reject organic-looking values."""
    return (
        len(set(value)) <= 2
        or SEQUENTIAL_FIXTURE.fullmatch(value) is not None
        or ZERO_PADDED_FIXTURE.fullmatch(value) is not None
    )


def test_synthetic_shape_classifier_rejects_organic_snowflakes():
    assert _is_obviously_synthetic("11111111111111111")
    assert _is_obviously_synthetic("12345678901234567")
    assert _is_obviously_synthetic("20000000000000001")
    # Digits of pi/e are deterministic synthetic data with an organic shape.
    synthetic_pi = "3141592653" + "589793238"
    synthetic_e = "271828182" + "845904523"
    assert not _is_obviously_synthetic(synthetic_pi)
    assert not _is_obviously_synthetic(synthetic_e)


def test_discord_ids_in_source_and_fixtures_are_obviously_synthetic():
    candidates = []
    for path in sorted(PLUGIN_ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".json"}:
            continue
        if any(part in {"__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        for match in SNOWFLAKE.finditer(text):
            value = match.group(0)
            if not _is_obviously_synthetic(value):
                candidates.append(f"{path.relative_to(PLUGIN_ROOT)}:{value}")

    assert candidates == [], "production-looking Discord IDs found: " + ", ".join(candidates)