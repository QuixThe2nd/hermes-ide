package rootmodules

import (
	"os"
	"path/filepath"
	"testing"
)

// repoRoot resolves the checkout root from this package's directory
// (internal/rootmodules → ../..) and requires setup.py to be there.
func repoRoot(t *testing.T) string {
	t.Helper()
	root, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, "setup.py")); err != nil {
		t.Fatalf("checkout root not found at %s: %v", root, err)
	}
	return root
}

func TestWrapperNameHasNoPySuffix(t *testing.T) {
	// The real stdout names: agent_cli_runner's ".hermes-agent-cli.out" and
	// delegate_tool_omp's ".hermes-harness.stdout".
	for _, stdout := range []string{".hermes-agent-cli.out", ".hermes-harness.stdout", "out"} {
		if name := WrapperName(stdout); filepath.Ext(name) == ".py" {
			t.Errorf("WrapperName(%q) = %q: a .py wrapper in a checkout root is scanned as a root module", stdout, name)
		}
	}
	if got, want := WrapperName(".hermes-agent-cli.out"), ".hermes-agent-cli.out.run"; got != want {
		t.Errorf("WrapperName = %q, want %q", got, want)
	}
}

func TestIsIdentifier(t *testing.T) {
	valid := []string{"run_agent", "hermes_state", "toolsets", "_private", "a1"}
	invalid := []string{"", ".hermes-agent-cli.out.run", "not-a-module", "1abc", "a b", "setup.py"}
	for _, s := range valid {
		if !IsIdentifier(s) {
			t.Errorf("IsIdentifier(%q) = false, want true", s)
		}
	}
	for _, s := range invalid {
		if IsIdentifier(s) {
			t.Errorf("IsIdentifier(%q) = true, want false", s)
		}
	}
}

func TestModuleNamesSkipsNonIdentifierStems(t *testing.T) {
	dir := t.TempDir()
	files := []string{
		"real_module.py",
		".hermes-agent-cli.out.run.py", // the stray wrapper that broke the build
		"not-a-module.py",
		"setup.py",  // the builder itself, never a module
		"notes.txt", // not Python at all
	}
	for _, f := range files {
		if err := os.WriteFile(filepath.Join(dir, f), nil, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	names, err := ModuleNames(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(names) != 1 || names[0] != "real_module" {
		t.Errorf("ModuleNames = %v, want [real_module]", names)
	}
}

func TestCheckoutRootHasNoUnimportablePyFile(t *testing.T) {
	// The live-tree guard: a job scratch file at the checkout root must never
	// reach the wheel build. A failure here means something dropped a "*.py"
	// file with an unimportable stem (the detached-wrapper bug class) — delete
	// the file and fix whatever wrote it.
	if name, found := HasUnimportablePyFile(repoRoot(t)); found {
		t.Errorf("checkout root holds unimportable module file %q", name)
	}
}

func TestCheckoutRootModuleNamesAreValid(t *testing.T) {
	names, err := ModuleNames(repoRoot(t))
	if err != nil {
		t.Fatal(err)
	}
	if len(names) == 0 {
		t.Fatal("checkout root has no importable modules — wrong directory?")
	}
	for _, name := range names {
		if !IsIdentifier(name) {
			t.Errorf("derived module %q is not a valid Python identifier", name)
		}
	}
}
