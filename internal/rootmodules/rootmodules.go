// Package rootmodules holds the packaging invariant for root-level Python
// modules: setup.py::_root_py_modules() derives the wheel's py_modules list
// from the checkout tree, so a root "*.py" file whose stem is not a valid
// Python identifier (a job's detached-service wrapper, a scratch script)
// must never reach that list — setuptools parses a dot-containing stem as a
// dotted package path and fails the build. It also owns the detached-service
// wrapper naming rule (tools/harness_persist.py): the wrapper rides the
// stdout path as "<stdout name>.run" and must never end in ".py", or it
// lands in a checkout root as an importable-looking file and re-breaks the
// wheel build.
//
// It is the Go twin of that Python behavior — same rules, independently
// tested. Production Python never calls it; deployments (pip, docker) carry
// no Go toolchain. See internal/footerfmt for the pattern.
package rootmodules

import (
	"os"
	"sort"
	"strings"
	"unicode"
)

// WrapperName returns the detached-service wrapper filename for a stdout
// file: the stdout name plus ".run". The wrapper is executed as
// `sys.executable <path>`, so it needs no ".py" suffix — and must not have
// one: in a checkout root a "*.py" wrapper is scanned as a root module by
// setup.py and its unimportable stem fails the wheel build.
func WrapperName(stdoutName string) string {
	return stdoutName + ".run"
}

// ModuleNames lists the valid importable top-level module names among the
// "*.py" files directly inside dir, excluding setup.py — the Go twin of
// setup.py::_root_py_modules(). Files whose stem is not a valid Python
// identifier (dotfiles, dashed names, job scratch files) are skipped: they
// cannot be imported, and setuptools would parse their stems as dotted
// package paths.
func ModuleNames(dir string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	names := make([]string, 0, len(entries))
	for _, entry := range entries {
		name := entry.Name()
		if !strings.HasSuffix(name, ".py") || name == "setup.py" {
			continue
		}
		stem := strings.TrimSuffix(name, ".py")
		if IsIdentifier(stem) {
			names = append(names, stem)
		}
	}
	sort.Strings(names)
	return names, nil
}

// IsIdentifier reports whether s is a valid Python identifier: a leading
// letter or underscore followed by letters, digits, or underscores —
// str.isidentifier() semantics over Unicode letter and digit classes.
func IsIdentifier(s string) bool {
	for i, r := range s {
		switch {
		case r == '_' || unicode.IsLetter(r):
		case i > 0 && unicode.IsDigit(r):
		default:
			return false
		}
	}
	return len(s) > 0
}

// HasUnimportablePyFile reports whether dir holds a "*.py" file whose stem
// is not a valid Python identifier — the exact condition that broke the
// wheel build when a detached-service wrapper landed in a checkout root.
func HasUnimportablePyFile(dir string) (string, bool) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return "", false
	}
	for _, entry := range entries {
		name := entry.Name()
		if strings.HasSuffix(name, ".py") && !IsIdentifier(strings.TrimSuffix(name, ".py")) {
			return name, true
		}
	}
	return "", false
}
