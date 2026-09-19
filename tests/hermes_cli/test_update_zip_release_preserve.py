"""#70337/#87331/#90495: the ZIP swap must preserve the gitignored build outputs.

The GitHub source ZIP carries only source; the BUILT desktop app
(release/win-unpacked/Hermes.exe), its renderer bundle (dist/), its own
node_modules and the dashboard assets (hermes_cli/web_dist/) exist only in
the live tree. Swapping `apps` / `hermes_cli` without grafting them deletes
them — and the dirty-tree guard must admit their ``!!`` status lines, or the
fallback refuses every install that has them.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def test_staged_apps_swap_preserves_live_release_dir(tmp_path, monkeypatch):
    from hermes_cli import main as hermes_main
    from hermes_cli.update_cmd import (
        _commit_staged_replacements,
        _stage_replacement,
    )

    # live tree: apps/desktop/release/win-unpacked/Hermes.exe + old source
    root = tmp_path / "install"
    live_apps = root / "apps" / "desktop"
    (live_apps / "release" / "win-unpacked").mkdir(parents=True)
    (live_apps / "release" / "win-unpacked" / "Hermes.exe").write_bytes(b"MZbuilt")
    (live_apps / "electron").mkdir()
    (live_apps / "electron" / "main.ts").write_text("old source")

    # extracted ZIP: new source, NO release dir (GitHub source archive shape)
    extracted = tmp_path / "extracted"
    zip_apps = extracted / "apps" / "desktop"
    (zip_apps / "electron").mkdir(parents=True)
    (zip_apps / "electron" / "main.ts").write_text("new source")

    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", root)

    # Reproduce the _update_via_zip staging loop for the `apps` entry,
    # including the release-dir graft.
    src = str(extracted / "apps")
    dst = str(root / "apps")
    staged_path = _stage_replacement(src, dst)
    live_release = os.path.join(dst, "desktop", "release")
    staged_release = os.path.join(staged_path, "desktop", "release")
    if os.path.isdir(live_release) and not os.path.exists(staged_release):
        os.makedirs(os.path.dirname(staged_release), exist_ok=True)
        shutil.copytree(live_release, staged_release)

    _commit_staged_replacements([(staged_path, dst)])

    # New source landed AND the built desktop app survived.
    assert (root / "apps" / "desktop" / "electron" / "main.ts").read_text() == (
        "new source"
    )
    exe = root / "apps" / "desktop" / "release" / "win-unpacked" / "Hermes.exe"
    assert exe.exists() and exe.read_bytes() == b"MZbuilt"


def test_zip_swap_keeps_every_nested_build_output_and_the_guard_admits_them(tmp_path):
    from hermes_cli.update_cmd_zip import (
        _commit_staged_replacements,
        _is_zip_preserved_entry_status_line,
        _stage_entries,
    )

    root = tmp_path / "install"
    outputs = {
        "apps/desktop/release/win-unpacked/Hermes.exe": b"MZbuilt",
        "apps/desktop/node_modules/electron/index.js": b"electron",
        "apps/desktop/dist/index.html": b"live renderer",
        "hermes_cli/web_dist/index.html": b"<dashboard>",
    }
    for rel, data in outputs.items():
        (root / rel).parent.mkdir(parents=True)
        (root / rel).write_bytes(data)
    (root / "hermes_cli" / "__pycache__").mkdir()
    (root / "hermes_cli" / "x.py").write_text("old")

    # Extracted ZIP: new source, none of the outputs — except dist/, which a future archive may ship.
    extracted = tmp_path / "extracted"
    (extracted / "apps" / "desktop" / "dist").mkdir(parents=True)
    (extracted / "apps" / "desktop" / "dist" / "index.html").write_bytes(b"shipped renderer")
    (extracted / "hermes_cli").mkdir()
    (extracted / "hermes_cli" / "x.py").write_text("new")

    _commit_staged_replacements(_stage_entries(str(extracted), ["apps", "hermes_cli"], str(root)))

    assert (root / "hermes_cli" / "x.py").read_text() == "new"
    for rel, data in outputs.items():
        if rel.startswith("apps/desktop/dist/"):
            continue
        assert (root / rel).read_bytes() == data, rel
    # What the ZIP ships wins over the live copy; the graft never clobbers it.
    assert (root / "apps" / "desktop" / "dist" / "index.html").read_bytes() == b"shipped renderer"
    assert not (root / "hermes_cli" / "__pycache__").exists()  # regenerable, dropped with the old tree

    # The dirty-tree guard sees these outputs as ``!!`` lines; they must not refuse the swap.
    for line in ("!! apps/desktop/release/", "!! apps/desktop/dist/", "!! apps/desktop/node_modules/",
                 "!! hermes_cli/web_dist/", "!! hermes_cli/__pycache__/", "!! __pycache__/"):
        assert _is_zip_preserved_entry_status_line(line), line
    # ...while other gitignored data, untracked files and renames into those dirs still block.
    for line in ("!! apps/desktop/notes.local", "?? apps/desktop/release/", "!! hermes_cli/web_dist_backup/",
                 "R  src/x -> apps/desktop/release/x"):
        assert not _is_zip_preserved_entry_status_line(line), line
