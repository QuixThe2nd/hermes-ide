"""Tests for the code-lane-gate plugin.

Covers ``plugins/code-lane-gate/``:

  * Kill-switch — the hook is dark (returns None, no scan) unless
    ``CODE_LANE_GATE_E2E`` is set.
  * Blocking — write_file, patch mode=replace, patch mode=patch (V4A
    headers, including the no-space ``***Update File:`` form and BOTH
    ``*** Move File:`` endpoints), relative paths anchored to a task
    workspace, symlinked paths into a repo, and the execute_code
    write heuristic (every write open() mode — w, w+, wb, wb+, w+b,
    a, x, r+, rb+) each block a source-file edit with the
    delegate-lane steering message.
  * Allow-listing — .md inside a repo, .py outside any repo, the
    Hermes memories MEMORY.md, and read-only execute_code snippets
    (``open(..., "r")``, ``read_text()``, ``eval("1+1")``) all pass.
  * Path/deny-set helpers — normalization (symlink-resolving), task
    workspace anchoring, suffix extraction, the .git upward walk, V4A
    header parsing.
  * register() — exactly one pre_tool_call registration.
"""

import importlib.util
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
# Module loading + fake repo scaffolding
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
    """A project directory with a ``.git`` dir — the repo signal the gate
    walks upward for. Source files inside it are gated territory."""
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
# Kill-switch
# ---------------------------------------------------------------------------


class TestKillSwitch:
    def test_default_off_no_scan(self, fake_repo):
        """Env unset → the hook returns None even for a would-block call."""
        mod = _load_plugin_init()
        args = {"path": str(fake_repo / "app.py"), "content": "x = 1\n"}
        assert mod._on_pre_tool_call(tool_name="write_file", args=args) is None

    def test_explicit_zero_disables(self, fake_repo, monkeypatch):
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "0")
        args = {"path": str(fake_repo / "app.py"), "content": "x = 1\n"}
        assert mod._on_pre_tool_call(tool_name="write_file", args=args) is None

    def test_e2e_on_enables(self, fake_repo, monkeypatch):
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"path": str(fake_repo / "app.py"), "content": "x = 1\n"}
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
        """Relative paths anchor to the TASK workspace, not process cwd.

        The process cwd here sits outside any repo, so process-cwd
        resolution would wave the write through; only resolving against
        the task's recorded workspace (as write_file itself does) catches
        it.
        """
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
        """A symlink whose target IS the repo root stays gated."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        link = tmp_path / "link-to-repo"
        link.symlink_to(fake_repo)
        args = {"path": str(link / "app.py"), "content": "x = 1\n"}
        _assert_block(mod._on_pre_tool_call(tool_name="write_file", args=args))

    def test_subdir_symlink_into_repo_blocks(self, fake_repo, tmp_path, monkeypatch):
        """A symlinked subdir inside a repo-less tree, pointing into a
        repo, is resolved to its real target before the .git walk —
        abspath-only normalization would climb the link's fake ancestors
        and miss the repo entirely."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        (fake_repo / "pkg").mkdir()
        outside = tmp_path / "plain-tree" / "sub"
        outside.mkdir(parents=True)
        link = outside / "link"
        link.symlink_to(fake_repo / "pkg")
        args = {"path": str(link / "new.py"), "content": "x = 1\n"}
        _assert_block(mod._on_pre_tool_call(tool_name="write_file", args=args))

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
    def test_markdown_inside_repo_allowed(self, fake_repo, monkeypatch):
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        args = {"path": str(fake_repo / "README.md"), "content": "# hi\n"}
        assert mod._on_pre_tool_call(tool_name="write_file", args=args) is None

    def test_source_outside_any_repo_allowed(self, tmp_path, monkeypatch):
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        # A .py that is not under any .git — the repo condition fails.
        outside = tmp_path / "loose" / "app.py"
        assert not mod._is_inside_git_repo(mod._normalize_path(str(outside)))
        args = {"path": str(outside), "content": "x = 1\n"}
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

    def test_relative_path_with_no_task_anchor_outside_repo_allowed(
        self, tmp_path, monkeypatch, task_cwd_store
    ):
        """Companion to the task-workspace block test: with no recorded
        workspace and the process cwd outside any repo, the same relative
        path resolves to repo-less territory and passes."""
        mod = _load_plugin_init()
        monkeypatch.setenv("CODE_LANE_GATE_E2E", "1")
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        monkeypatch.chdir(outside)
        args = {"path": "app.py", "content": "x = 1\n"}
        assert mod._on_pre_tool_call(
            tool_name="write_file", args=args, task_id="task-none"
        ) is None

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

    def test_is_inside_git_repo_walks_upward(self, tmp_path):
        mod = _load_plugin_init()
        repo = tmp_path / "monorepo"
        (repo / ".git").mkdir(parents=True)
        deep = repo / "packages" / "web" / "src" / "app.py"
        assert mod._is_inside_git_repo(str(deep)) is True
        assert mod._is_inside_git_repo(str(tmp_path / "elsewhere" / "a.py")) is False

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

    def test_first_denied_path_requires_both_conditions(self, tmp_path):
        mod = _load_plugin_init()
        repo = tmp_path / "r"
        (repo / ".git").mkdir(parents=True)
        # Source suffix but no repo → allowed.
        assert mod._first_denied_path([str(tmp_path / "free" / "a.py")]) is None
        # Repo but docs suffix → allowed.
        assert mod._first_denied_path([str(repo / "README.md")]) is None
        # Both → the normalized path comes back.
        assert mod._first_denied_path([str(repo / "a.py")]) == str(repo / "a.py")
        # First match wins across a mixed list.
        assert mod._first_denied_path(
            [str(repo / "notes.md"), str(repo / "b.ts")]
        ) == str(repo / "b.ts")


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
