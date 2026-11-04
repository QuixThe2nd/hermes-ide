"""The source-ZIP swap preserves retained nested build outputs.

The GitHub source ZIP carries only source; dashboard assets, TUI builds and
Node dependency trees exist only in the live tree. The swap must graft those
outputs into staging, and the dirty-tree guard must admit their ``!!`` lines.
"""

from __future__ import annotations


def test_staged_hermes_cli_swap_preserves_live_web_dist(tmp_path):
    from hermes_cli.update_cmd_zip import _commit_staged_replacements, _stage_entries

    root = tmp_path / "install"
    (root / "hermes_cli" / "web_dist").mkdir(parents=True)
    (root / "hermes_cli" / "web_dist" / "index.html").write_bytes(b"<live dashboard>")
    (root / "hermes_cli" / "main.py").write_text("old source", encoding="utf-8")

    extracted = tmp_path / "extracted"
    (extracted / "hermes_cli").mkdir(parents=True)
    (extracted / "hermes_cli" / "main.py").write_text("new source", encoding="utf-8")

    _commit_staged_replacements(_stage_entries(str(extracted), ["hermes_cli"], str(root)))

    assert (root / "hermes_cli" / "main.py").read_text(encoding="utf-8") == "new source"
    assert (root / "hermes_cli" / "web_dist" / "index.html").read_bytes() == b"<live dashboard>"


def test_zip_swap_keeps_every_nested_build_output_and_the_guard_admits_them(tmp_path):
    from hermes_cli.update_cmd_zip import (
        _commit_staged_replacements,
        _is_zip_preserved_entry_status_line,
        _stage_entries,
    )

    root = tmp_path / "install"
    outputs = {
        "hermes_cli/web_dist/index.html": b"<dashboard>",
        "scripts/whatsapp-bridge/node_modules/package/index.js": b"bridge",
        "ui-tui/dist/entry.js": b"tui",
        "ui-tui/node_modules/package/index.js": b"dependency",
        "ui-tui/packages/hermes-ink/dist/index.js": b"ink",
        "web/node_modules/package/index.js": b"web dependency",
    }
    for rel, data in outputs.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    (root / "hermes_cli" / "__pycache__").mkdir()
    (root / "hermes_cli" / "x.py").write_text("old", encoding="utf-8")

    extracted = tmp_path / "extracted"
    (extracted / "hermes_cli").mkdir(parents=True)
    (extracted / "hermes_cli" / "x.py").write_text("new", encoding="utf-8")
    (extracted / "ui-tui").mkdir()
    (extracted / "ui-tui" / "source.ts").write_text("new", encoding="utf-8")
    (extracted / "scripts").mkdir()
    (extracted / "web").mkdir()

    _commit_staged_replacements(
        _stage_entries(str(extracted), ["hermes_cli", "scripts", "ui-tui", "web"], str(root))
    )

    assert (root / "hermes_cli" / "x.py").read_text(encoding="utf-8") == "new"
    assert (root / "ui-tui" / "source.ts").read_text(encoding="utf-8") == "new"
    for rel, data in outputs.items():
        assert (root / rel).read_bytes() == data, rel
    assert not (root / "hermes_cli" / "__pycache__").exists()

    for line in (
        "!! hermes_cli/web_dist/", "!! hermes_cli/__pycache__/", "!! __pycache__/",
        "!! scripts/whatsapp-bridge/node_modules/", "!! ui-tui/dist/",
        "!! ui-tui/node_modules/", "!! ui-tui/packages/hermes-ink/dist/",
        "!! web/node_modules/", "!! tests-js/node_modules/",
    ):
        assert _is_zip_preserved_entry_status_line(line), line
    for line in (
        "!! hermes_cli/notes.local", "?? hermes_cli/web_dist/",
        "!! hermes_cli/web_dist_backup/", "R  src/x -> hermes_cli/web_dist/x",
    ):
        assert not _is_zip_preserved_entry_status_line(line), line


def test_zip_ships_a_nested_output_instead_of_clobbering_it_with_the_live_copy(tmp_path):
    from hermes_cli.update_cmd_zip import _commit_staged_replacements, _stage_entries

    root = tmp_path / "install"
    (root / "hermes_cli" / "web_dist").mkdir(parents=True)
    (root / "hermes_cli" / "web_dist" / "index.html").write_bytes(b"live")
    extracted = tmp_path / "extracted"
    (extracted / "hermes_cli" / "web_dist").mkdir(parents=True)
    (extracted / "hermes_cli" / "web_dist" / "index.html").write_bytes(b"shipped")

    _commit_staged_replacements(_stage_entries(str(extracted), ["hermes_cli"], str(root)))

    assert (root / "hermes_cli" / "web_dist" / "index.html").read_bytes() == b"shipped"


def test_guard_admits_a_real_installs_ignored_set_and_blocks_only_what_the_swap_destroys(tmp_path):
    """Real git plus the repo's ignore rules exercises the actual dirty-tree guard."""
    import subprocess

    from hermes_cli.update_cmd_zip import _zip_overlay_block_reason

    root = tmp_path / "install"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "ui-tui").mkdir()
    (root / ".gitignore").write_text(
        ".bytecode-fingerprint\n.hermes-bootstrap-complete\n.install_method\n"
        "hermes_agent.egg-info/\n__pycache__/\nnode_modules/\nvenv/\n.env\n"
        "hermes_cli/web_dist/\n",
        encoding="utf-8",
    )
    (root / "ui-tui" / ".gitignore").write_text("dist/\n", encoding="utf-8")
    for tracked in (
        "hermes_cli/main.py", "scripts/whatsapp-bridge/index.js", "ui-tui/package.json",
        "web/package.json", "tests-js/a.test.ts", "run_agent.py",
    ):
        (root / tracked).parent.mkdir(parents=True, exist_ok=True)
        (root / tracked).write_text("src", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        check=True,
    )
    for ignored in (
        ".bytecode-fingerprint", ".hermes-bootstrap-complete", ".install_method",
        "hermes_agent.egg-info/PKG-INFO", "hermes_cli/__pycache__/main.pyc", "__pycache__/x.pyc",
        "hermes_cli/web_dist/index.html", "ui-tui/dist/entry.js", "ui-tui/node_modules/x/index.js",
        "ui-tui/packages/hermes-ink/dist/index.js", "web/node_modules/x/index.js",
        "tests-js/node_modules/x/index.js", "scripts/whatsapp-bridge/node_modules/x/index.js",
        "venv/lib.py", "node_modules/x/index.js", ".env",
    ):
        (root / ignored).parent.mkdir(parents=True, exist_ok=True)
        (root / ignored).write_text("artifact", encoding="utf-8")
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "-uall", "--ignored=matching"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert status.count("!!") >= 15 and "??" not in status, status

    assert _zip_overlay_block_reason(root) is None
    shipped = {"hermes_cli", "scripts", "ui-tui", "web", "tests-js", "run_agent.py"}
    assert _zip_overlay_block_reason(root, shipped=shipped) is None
    assert _zip_overlay_block_reason(root, shipped=shipped | {"hermes_agent.egg-info"}) is not None
    (root / "hermes_cli" / ".env").write_text("mine", encoding="utf-8")
    assert _zip_overlay_block_reason(root) is not None
