"""Tests for the code-lane-gate plugin.

Covers ``plugins/code-lane-gate/``:

  * Opt-out switch — the gate is on by default (env unset blocks);
    ``CODE_LANE_GATE_E2E=0`` (also ``false``/``no``/``off``, case-
    insensitive and stripped) makes the hook return None with no scan,
    while ``1`` and unrecognized values keep it enabled.
  * Blocking — the denial is suffix-only and location-independent:
    write_file, patch mode=replace, and patch mode=patch (V4A headers,
    including the no-space ``***Update File:`` form and BOTH
    ``*** Move File:`` endpoints) each block a source-suffixed write
    wherever it lands — no git repository required. Targets outside any
    repo (a tmpdir probe, /tmp, a symlinked parent pointing at a
    repo-less tree) and the old repo-scoped cases (kept as the subset
    they now are) all block; relative paths still anchor to the task
    workspace. execute_code write-shaped snippets (every write open()
    mode — w, w+, wb, wb+, w+b, a, x, r+, rb+) block as before.
  * Allow-listing — docs/config suffixes (.md/.txt/.yaml/.json/.toml),
    extensionless paths, and paths without a dot pass anywhere; the
    Hermes memories MEMORY.md and read-only execute_code snippets
    (``open(..., "r")``, ``read_text()``, ``eval("1+1")``) pass too.
  * Path/deny-set helpers — normalization (symlink-resolving), task
    workspace anchoring, suffix extraction, V4A header parsing.
  * register() — exactly one pre_tool_call registration.
"""

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("CODE_LANE_GATE_E2E", raising=False)
    yield hermes_home


# ---------------------------------------------------------------------------
# Module loading + scaffolding
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_plugin_init():
    """Import the plugin __init__.py as a standalone package."""
    plugin_dir = _repo_root() / "plugins" / "code-lane-gate"
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.code_lane_gate",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.code_lane_gate"
    mod.__path__ = [str(plugin_dir)]
    sys.modules["hermes_plugins.code_lane_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def fake_repo(tmp_path) -> Path:
    """A project directory with a ``.git`` dir. The gate no longer looks
    for it — denial is suffix-only — but repo-resident targets still
    block, so these keep the old repo-scoped cases covered as a subset
    of the new everywhere contract."""
    proj = tmp_path / "x" / "proj"
    (proj / ".git").mkdir(parents=True)
    return proj


@pytest.fixture()
def task_cwd_store(monkeypatch):
    """An isolated terminal_tool cwd record store.

    The gate anchors relative paths through
    ``tools.file_tools._resolve_path_for_task``, whose first rung is the
    per-session cwd record. Emptying both backing dicts keeps a record
    written by one test from leaking into another.
    """
    import tools.terminal_tool as tt

    monkeypatch.setattr(tt, "_session_cwd", {})
    monkeypatch.setattr(tt, "_task_env_overrides", {})
    return tt


# ---------------------------------------------------------------------------
# Opt-out switch (default on)
# ---------------------------------------------------------------------------


class TestOptOut:
    def test_env_unset_blocks_by_default(self, tmp_path):
        """Env unset → the gate is ON: a would-block call blocks."""
        mod = _load_plugin_init()
        args = {"path": str(tmp_path / "probe.py"), "content": "x = 1\n"}
        out = mod._on_pre_tool_call(tool_name="write_file", args=args)
        assert isinstance(out, dict) and out["action"] == "block"

    @pytest.mark.parametrize("value", ["0", "false", "no", "off"])
    @pytest.mark.parametrize(
        "tool_name,args",
        [
            ("write_file", {"path": "/tmp/anything.py", "content": "x = 1\n"}),
            ("patch", {"mode": "patch", "patch": "*** Update File: new.ts\n"}),
            ("execute_code", {"code": "open('app.py', 'w')\n"}),
        ],
    )
    def test_opt_out_values_disable_every_gated_shape(
        self, monkeypatch, value, tool_name, args
    ):
        """0/false/no/off → the hook returns None before any scan, for
        every gated tool shape — the switch kills the whole gate."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", value)
        assert mod._on_pre_tool_call(tool_name=tool_name, args=args) is None

    def test_opt_out_is_case_insensitive_and_stripped(
        self, tmp_path, monkeypatch
    ):
        """Mixed-case, whitespace-padded opt-outs still disable."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "  OFF ")
        args = {"path": str(tmp_path / "probe.py"), "content": "x = 1\n"}
        assert mod._on_pre_tool_call(tool_name="write_file", args=args) is None

    def test_explicit_one_enables(self, tmp_path, monkeypatch):
        """The deployment host's systemd drop-in sets =1 — still enabled."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"path": str(tmp_path / "probe.py"), "content": "x = 1\n"}
        out = mod._on_pre_tool_call(tool_name="write_file", args=args)
        assert isinstance(out, dict) and out["action"] == "block"

    def test_unrecognized_value_fails_closed(self, tmp_path, monkeypatch):
        """Garbage is not an opt-out — the gate's purpose is blocking."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "maybe")
        args = {"path": str(tmp_path / "probe.py"), "content": "x = 1\n"}
        out = mod._on_pre_tool_call(tool_name="write_file", args=args)
        assert isinstance(out, dict) and out["action"] == "block"


# ---------------------------------------------------------------------------
# Blocking (E2E on)
# ---------------------------------------------------------------------------


def _assert_block(out) -> None:
    assert isinstance(out, dict)
    assert out["action"] == "block"
    assert "delegate_cursor_agent" in out["message"]
    assert "delegate_claude_agent" in out["message"]
    assert "CODE_LANE_GATE_E2E=0" in out["message"]


class TestBlocking:
    # -- no git repo anywhere: the new everywhere contract ---------------

    def test_write_file_source_outside_any_repo_blocks(
        self, tmp_path, monkeypatch
    ):
        """The contract under change: a deny-suffixed write blocks with
        no .git anywhere above the target. The message names the resolved
        path and calls it a source-file write."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        target = tmp_path / "probe.py"
        args = {"path": str(target), "content": "x = 1\n"}
        out = mod._on_pre_tool_call(tool_name="write_file", args=args)
        _assert_block(out)
        assert out["message"].startswith(
            f"code-lane-gate: {os.path.realpath(str(target))}"
        )
        assert "is a source-file write." in out["message"]

    def test_write_file_tmp_shell_script_blocks(self, monkeypatch):
        """``/tmp/anything.sh`` — deny suffix, no repo involved."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"path": "/tmp/anything.sh", "content": "echo hi\n"}
        out = mod._on_pre_tool_call(tool_name="write_file", args=args)
        _assert_block(out)
        assert "anything.sh" in out["message"]

    def test_write_file_symlinked_parent_outside_repo_blocks(
        self, tmp_path, monkeypatch
    ):
        """A .py whose parent dir is a symlink pointing outside any
        repo: resolution follows the link, and the suffix alone denies —
        a repo-less target can no longer dodge by spelling."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        real = tmp_path / "real_pkg"
        real.mkdir()
        link = tmp_path / "linked_pkg"
        link.symlink_to(real)
        args = {"path": str(link / "new.py"), "content": "x = 1\n"}
        _assert_block(mod._on_pre_tool_call(tool_name="write_file", args=args))

    def test_patch_v4a_header_source_outside_repo_blocks(
        self, tmp_path, monkeypatch
    ):
        """V4A patch naming ``new.ts`` in a non-repo dir — suffix-only
        denial, no repo needed."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {
            "mode": "patch",
            "patch": (
                "*** Begin Patch\n"
                "*** Update File: new.ts\n"
                "@@\n"
                "-old\n"
                "+new\n"
                "*** End Patch\n"
            ),
        }
        monkeypatch.chdir(tmp_path)
        _assert_block(mod._on_pre_tool_call(tool_name="patch", args=args))

    # -- git-repo cases: the old behaviour is a subset, still blocking ---

    def test_write_file_source_in_repo_blocks(self, fake_repo, monkeypatch):
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"path": str(fake_repo / "app.py"), "content": "x = 1\n"}
        _assert_block(mod._on_pre_tool_call(tool_name="write_file", args=args))

    def test_patch_replace_source_in_repo_blocks(self, fake_repo, monkeypatch):
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {
            "mode": "replace",
            "path": str(fake_repo / "app.py"),
            "old_string": "x = 1",
            "new_string": "x = 2",
        }
        _assert_block(mod._on_pre_tool_call(tool_name="patch", args=args))

    def test_patch_v4a_header_source_in_repo_blocks(
        self, fake_repo, monkeypatch
    ):
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {
            "mode": "patch",
            "patch": (
                "*** Begin Patch\n"
                "*** Update File: app.py\n"
                "@@\n"
                "-x = 1\n"
                "+x = 2\n"
                "*** End Patch\n"
            ),
        }
        # V4A paths are relative to the checkout; run the check from inside
        # the fake repo so abspath lands in gated territory.
        monkeypatch.chdir(fake_repo)
        _assert_block(mod._on_pre_tool_call(tool_name="patch", args=args))

    def test_patch_v4a_no_space_header_blocks(self, fake_repo, monkeypatch):
        """``***Update File:`` (no space) parses in the real patch parser,
        so it must gate too — a form the parser honours can't slip past."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {
            "mode": "patch",
            "patch": (
                "*** Begin Patch\n"
                "***Update File: app.py\n"
                "@@\n"
                "-x = 1\n"
                "+x = 2\n"
                "*** End Patch\n"
            ),
        }
        monkeypatch.chdir(fake_repo)
        _assert_block(mod._on_pre_tool_call(tool_name="patch", args=args))

    def test_patch_v4a_move_file_dst_blocks(self, fake_repo, monkeypatch):
        """Move into a source path: the dst endpoint gates the call."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {
            "mode": "patch",
            "patch": "*** Move File: notes.md -> app.py\n",
        }
        monkeypatch.chdir(fake_repo)
        _assert_block(mod._on_pre_tool_call(tool_name="patch", args=args))

    def test_patch_v4a_move_file_src_blocks(self, fake_repo, monkeypatch):
        """Move out of a source path: the src endpoint gates the call."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {
            "mode": "patch",
            "patch": "*** Move File: app.py -> notes.md\n",
        }
        monkeypatch.chdir(fake_repo)
        _assert_block(mod._on_pre_tool_call(tool_name="patch", args=args))

    def test_write_file_relative_path_in_task_workspace_blocks(
        self, fake_repo, tmp_path, monkeypatch, task_cwd_store
    ):
        """Relative paths anchor to the TASK workspace, not process cwd —
        the block message names the workspace-resolved path, so the gate
        and the file tools agree on where a relative ``app.py`` lands."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        monkeypatch.chdir(outside)
        task_cwd_store.record_session_cwd("task-42", str(fake_repo))
        args = {"path": "app.py", "content": "x = 1\n"}
        out = mod._on_pre_tool_call(
            tool_name="write_file", args=args, task_id="task-42"
        )
        _assert_block(out)
        assert out["message"].startswith(
            f"code-lane-gate: {fake_repo.resolve()}"
        )

    def test_repo_root_symlink_still_blocks(self, fake_repo, tmp_path, monkeypatch):
        """A symlink whose target IS the repo root stays blocked."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        link = tmp_path / "link-to-repo"
        link.symlink_to(fake_repo)
        args = {"path": str(link / "app.py"), "content": "x = 1\n"}
        _assert_block(mod._on_pre_tool_call(tool_name="write_file", args=args))

    def test_subdir_symlink_into_repo_blocks(self, fake_repo, tmp_path, monkeypatch):
        """A symlinked subdir in a repo-less tree, pointing into a repo,
        resolves to its real target before the check — a .py reached
        through the link still blocks."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        (fake_repo / "pkg").mkdir()
        outside = tmp_path / "plain-tree" / "sub"
        outside.mkdir(parents=True)
        link = outside / "link"
        link.symlink_to(fake_repo / "pkg")
        args = {"path": str(link / "new.py"), "content": "x = 1\n"}
        _assert_block(mod._on_pre_tool_call(tool_name="write_file", args=args))

    # -- execute_code heuristic (location-independent, unchanged) --------

    def test_execute_code_write_heuristic_blocks(self, monkeypatch):
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"code": "with open('app.py', 'w') as f:\n    f.write(src)\n"}
        _assert_block(mod._on_pre_tool_call(tool_name="execute_code", args=args))

    def test_execute_code_write_mode_open_blocks(self, monkeypatch):
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"code": "with open(f, 'w') as fh:\n    fh.write(src)\n"}
        _assert_block(mod._on_pre_tool_call(tool_name="execute_code", args=args))

    @pytest.mark.parametrize(
        "mode",
        ["w", "w+", "wb", "wb+", "w+b", "a", "x", "r+", "rb+"],
    )
    def test_execute_code_write_mode_matrix_blocks(self, monkeypatch, mode):
        """Every Python write mode blocks — including the wb+/w+b forms a
        single character class ([wa][+b]?) never matched. The mode is
        captured and judged by content: any w/a/x, or a + that upgrades
        even an r-mode to read-write, is a write."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"code": f'fh = open("app.py", "{mode}")\n'}
        _assert_block(mod._on_pre_tool_call(tool_name="execute_code", args=args))

    def test_execute_code_write_mode_kwarg_blocks(self, monkeypatch):
        """The mode= keyword spelling of a write open blocks too."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"code": "fh = open('app.py', mode='wb+')\n"}
        _assert_block(mod._on_pre_tool_call(tool_name="execute_code", args=args))

    def test_execute_code_later_write_open_blocks(self, monkeypatch):
        """A read open earlier in the snippet must not mask a later
        write-mode open — every open's mode is judged, not just the
        first match."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {
            "code": (
                "src = open('in.txt', 'r').read()\n"
                "out = open('app.py', 'wb+')\n"
            )
        }
        _assert_block(mod._on_pre_tool_call(tool_name="execute_code", args=args))

    def test_execute_code_shutil_copyfile_blocks(self, monkeypatch):
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"code": "shutil.copyfile(src, 'app.py')\n"}
        _assert_block(mod._on_pre_tool_call(tool_name="execute_code", args=args))

    def test_execute_code_shell_redirect_blocks(self, monkeypatch):
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"code": 'subprocess.run("cat src > app.py", shell=True)\n'}
        _assert_block(mod._on_pre_tool_call(tool_name="execute_code", args=args))

    def test_execute_code_read_only_open_passes(self, monkeypatch):
        """Read-mode opens are not writes — the heuristic must not fire."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"code": "cfg = open('app.py', 'r').read()\n"}
        assert mod._on_pre_tool_call(tool_name="execute_code", args=args) is None

    @pytest.mark.parametrize("mode", ["r", "rb", "rt"])
    def test_execute_code_read_mode_matrix_passes(self, monkeypatch, mode):
        """Modes built only from r/b/t never write — they pass."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"code": f'fh = open("app.py", "{mode}")\n'}
        assert mod._on_pre_tool_call(tool_name="execute_code", args=args) is None

    def test_execute_code_read_text_and_eval_pass(self, monkeypatch):
        """read_text()/eval() are read-only compute — they pass."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {
            "code": (
                "from pathlib import Path\n"
                "print(Path('app.py').read_text())\n"
                "print(eval('1+1'))\n"
            )
        }
        assert mod._on_pre_tool_call(tool_name="execute_code", args=args) is None

    def test_execute_code_clean_code_passes(self, monkeypatch):
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"code": "print(sum(range(10)))\n"}
        assert mod._on_pre_tool_call(tool_name="execute_code", args=args) is None


# ---------------------------------------------------------------------------
# Allow-listing (E2E on)
# ---------------------------------------------------------------------------


class TestAllowed:
    @pytest.mark.parametrize(
        "name",
        ["README.md", "notes.txt", "conf.yaml", "data.json", "pyproject.toml"],
    )
    def test_docs_and_config_suffixes_allowed_anywhere(
        self, tmp_path, monkeypatch, name
    ):
        """Docs/config are never policed — the allow is as
        location-independent as the block."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"path": str(tmp_path / name), "content": "x\n"}
        assert mod._on_pre_tool_call(tool_name="write_file", args=args) is None

    def test_patch_replace_markdown_outside_repo_allowed(
        self, tmp_path, monkeypatch
    ):
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {
            "mode": "replace",
            "path": str(tmp_path / "notes.md"),
            "old_string": "a",
            "new_string": "b",
        }
        assert mod._on_pre_tool_call(tool_name="patch", args=args) is None

    def test_patch_v4a_docs_only_outside_repo_allowed(
        self, tmp_path, monkeypatch
    ):
        """A V4A patch naming only docs/config files passes."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {
            "mode": "patch",
            "patch": (
                "*** Begin Patch\n"
                "*** Update File: docs/notes.md\n"
                "@@\n"
                "-old\n"
                "+new\n"
                "*** End Patch\n"
            ),
        }
        monkeypatch.chdir(tmp_path)
        assert mod._on_pre_tool_call(tool_name="patch", args=args) is None

    def test_markdown_inside_repo_allowed(self, fake_repo, monkeypatch):
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"path": str(fake_repo / "README.md"), "content": "# hi\n"}
        assert mod._on_pre_tool_call(tool_name="write_file", args=args) is None

    def test_hermes_memory_markdown_allowed(self, monkeypatch):
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"path": "/root/.hermes/memories/MEMORY.md", "content": "- x\n"}
        assert mod._on_pre_tool_call(tool_name="write_file", args=args) is None

    def test_suffixless_path_inside_repo_allowed(self, fake_repo, monkeypatch):
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"path": str(fake_repo / "Makefile"), "content": "all:\n"}
        assert mod._on_pre_tool_call(tool_name="write_file", args=args) is None

    def test_suffixless_path_outside_repo_allowed(self, tmp_path, monkeypatch):
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"path": str(tmp_path / "Makefile"), "content": "all:\n"}
        assert mod._on_pre_tool_call(tool_name="write_file", args=args) is None

    def test_path_without_any_dot_allowed(self, tmp_path, monkeypatch):
        """No dot anywhere in the path → no suffix → allowed."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"path": str(tmp_path / "bin" / "tool"), "content": "#!/bin/sh\n"}
        assert mod._on_pre_tool_call(tool_name="write_file", args=args) is None

    def test_untargeted_tool_skipped(self, fake_repo, monkeypatch):
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"pattern": "def main", "path": str(fake_repo)}
        assert mod._on_pre_tool_call(tool_name="search_files", args=args) is None


# ---------------------------------------------------------------------------
# Path / deny-set helpers (no env involved)
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_normalize_path_expands_user_and_absolutizes(self):
        mod = _load_plugin_init()
        rel = mod._normalize_path("rel/app.py")
        assert Path(rel).is_absolute() and rel.endswith("rel/app.py")
        assert mod._normalize_path("~/x.py").startswith("/")

    def test_normalize_path_resolves_symlinks(self, tmp_path):
        mod = _load_plugin_init()
        real = tmp_path / "real" / "proj"
        real.mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(real)
        assert mod._normalize_path(str(link / "app.py")) == str(
            real / "app.py"
        )

    def test_resolve_for_task_anchors_relative_to_task_workspace(
        self, tmp_path, monkeypatch, task_cwd_store
    ):
        mod = _load_plugin_init()
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.chdir(outside)
        task_cwd_store.record_session_cwd("task-7", str(workspace))
        assert mod._resolve_for_task("app.py", "task-7") == str(
            (workspace / "app.py").resolve()
        )
        # No recorded workspace → process cwd, same as the tool layer's
        # last-resort rung.
        assert mod._resolve_for_task("app.py", "task-unknown") == str(
            (outside / "app.py").resolve()
        )

    def test_deny_suffix(self):
        mod = _load_plugin_init()
        assert mod._deny_suffix("/a/b/app.PY") == "py"
        assert mod._deny_suffix("/a/b/component.TSX") == "tsx"
        assert mod._deny_suffix("/a/b/app.tar.gz") == "gz"
        assert mod._deny_suffix("/a/b/Makefile") == ""
        assert mod._deny_suffix("/a/b/.py") == ""

    def test_deny_set_contents(self):
        mod = _load_plugin_init()
        for suffix in ("py", "ts", "tsx", "js", "jsx", "go", "rs", "sh", "sql"):
            assert suffix in mod._DENY_SUFFIXES
        for suffix in ("md", "yaml", "yml", "json", "toml", "txt", ""):
            assert suffix not in mod._DENY_SUFFIXES

    def test_paths_from_v4a_patch(self):
        mod = _load_plugin_init()
        text = (
            "*** Begin Patch\n"
            "*** Update File: src/app.py\n"
            "@@ context @@\n"
            "-old\n"
            "+new\n"
            "*** Add File: `docs/notes.md`\n"
            "+hello\n"
            "*** Delete File:   old/mod.rs \n"
            "*** End Patch\n"
        )
        assert mod._paths_from_v4a_patch(text) == [
            "src/app.py",
            "docs/notes.md",
            "old/mod.rs",
        ]

    def test_paths_from_v4a_patch_no_space_and_move_forms(self):
        """The real parser (tools/patch_parser.py) accepts ``***Update
        File:`` with no space and ``*** Move File: src -> dst`` — the gate
        must extract both Move endpoints, src first."""
        mod = _load_plugin_init()
        text = (
            "*** Begin Patch\n"
            "***Update File: app.py\n"
            "***Add File: new.ts\n"
            "***Delete File: gone.go\n"
            "*** Move File: `a.py` -> b.rs\n"
            "*** End Patch\n"
        )
        assert mod._paths_from_v4a_patch(text) == [
            "app.py",
            "new.ts",
            "gone.go",
            "a.py",
            "b.rs",
        ]

    def test_first_denied_path_is_suffix_only(self, tmp_path):
        """Denial needs ONLY a deny suffix — no repo condition exists."""
        mod = _load_plugin_init()
        # Source suffix with no repo anywhere → denied.
        assert mod._first_denied_path([str(tmp_path / "free" / "a.py")]) == str(
            (tmp_path / "free" / "a.py").resolve()
        )
        # Docs suffix → allowed.
        assert mod._first_denied_path([str(tmp_path / "README.md")]) is None
        # No suffix → allowed.
        assert mod._first_denied_path([str(tmp_path / "Makefile")]) is None
        # First match wins across a mixed list.
        assert mod._first_denied_path(
            [str(tmp_path / "notes.md"), str(tmp_path / "b.ts")]
        ) == str((tmp_path / "b.ts").resolve())


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------


class TestRegister:
    def test_registers_exactly_one_pre_tool_call(self):
        mod = _load_plugin_init()

        class StubCtx:
            def __init__(self):
                self.registrations = []

            def register_hook(self, name, callback):
                self.registrations.append((name, callback))

        ctx = StubCtx()
        mod.register(ctx)
        assert [name for name, _ in ctx.registrations] == ["pre_tool_call"]
        assert ctx.registrations[0][1] is mod._on_pre_tool_call
