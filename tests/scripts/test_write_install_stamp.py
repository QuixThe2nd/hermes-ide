from scripts.write_install_stamp import build_stamp


def test_build_stamp_keeps_provenance_separate_from_distribution():
    stamp = build_stamp(commit="a" * 40, source="ci", distribution="docker", update_mechanism="external")

    assert stamp["source"] == "ci"
    assert stamp["distribution"] == "docker"


def test_distribution_defaults_to_null():
    stamp = build_stamp(commit="c" * 40, update_mechanism="self")

    assert stamp["distribution"] is None


def test_cli_writes_external_docker_stamp(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    out = tmp_path / "stamp.json"
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "write_install_stamp.py"),
            "--output", str(out),
            "--commit", "d" * 40,
            "--distribution", "docker",
            "--update-mechanism", "external",
        ],
        capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    stamp = json.loads(out.read_text(encoding="utf-8-sig"))
    assert stamp["distribution"] == "docker"
    assert stamp["updateMechanism"] == "external"
