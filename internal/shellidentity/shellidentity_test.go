package shellidentity

import (
	"fmt"
	"os"
	"os/exec"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// Fakes and helpers.
// ---------------------------------------------------------------------------

// fakeEnv records captures and restores; each capture returns distinct values
// (capture#N:name) so a re-capture can never be mistaken for the original.
type fakeEnv struct {
	mu        sync.Mutex
	captures  []map[string]*string
	restores  []map[string]*string
	restoreFn func(captured map[string]*string) error
}

func (f *fakeEnv) CaptureShellIdentity(names []string) (map[string]*string, error) {
	f.mu.Lock()
	n := len(f.captures)
	f.mu.Unlock()
	captured := make(map[string]*string, len(names))
	for _, name := range names {
		v := fmt.Sprintf("capture#%d:%s", n, name)
		captured[name] = &v
	}
	f.mu.Lock()
	f.captures = append(f.captures, captured)
	f.mu.Unlock()
	return captured, nil
}

func (f *fakeEnv) RestoreShellIdentity(captured map[string]*string) error {
	if f.restoreFn != nil {
		return f.restoreFn(captured)
	}
	f.recordRestore(captured)
	return nil
}

func (f *fakeEnv) recordRestore(captured map[string]*string) {
	cp := make(map[string]*string, len(captured))
	for name, value := range captured {
		if value == nil {
			cp[name] = nil
		} else {
			v := *value
			cp[name] = &v
		}
	}
	f.mu.Lock()
	f.restores = append(f.restores, cp)
	f.mu.Unlock()
}

func (f *fakeEnv) captureCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.captures)
}

func (f *fakeEnv) restoreCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.restores)
}

func (f *fakeEnv) restoreAt(i int) map[string]*string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.restores[i]
}

func staticEnvFor(env Environment) EnvironmentFor {
	return func(string) Environment { return env }
}

// runUnderBash executes prelude, then script, then a NUL-fenced probe of
// names — the same fencing the Python twin's capture uses — and returns the
// probed mapping. Env values cannot contain NUL, so values with spaces,
// quotes and newlines round-trip exactly.
func runUnderBash(t *testing.T, prelude, script string, names []string) map[string]*string {
	t.Helper()
	var probe strings.Builder
	for _, name := range names {
		fmt.Fprintf(&probe, `printf '%%s\0%%s\0' "${%s+s}" "${%s-}"; `, name, name)
	}
	cmd := exec.Command("bash", "-c", prelude+"; "+script+"; "+probe.String())
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("bash round-trip failed: %v\nscript:\n%s\noutput: %q", err, cmd.Args[1], out)
	}
	chunks := strings.Split(string(out), "\x00")
	if len(chunks) < 2*len(names) {
		t.Fatalf("probe output truncated: %q (chunks=%d names=%d)", out, len(chunks), len(names))
	}
	got := make(map[string]*string, len(names))
	for i, name := range names {
		if chunks[2*i] == "s" {
			v := chunks[2*i+1]
			got[name] = &v
		} else {
			got[name] = nil
		}
	}
	return got
}

func requireBash(t *testing.T) {
	t.Helper()
	if _, err := exec.LookPath("bash"); err != nil {
		t.Skip("bash not available for the shell round-trip")
	}
}

func strPtr(s string) *string { return &s }

// ---------------------------------------------------------------------------
// Restore script under real bash: quoting matrix + unset semantics.
// ---------------------------------------------------------------------------

func TestRestoreScriptUnderBashQuotingMatrix(t *testing.T) {
	requireBash(t)
	values := []struct {
		label string
		value string
	}{
		{"plain", "/tmp/pc88-parent-home"},
		{"space", "/tmp/pc 88 parent home"},
		{"single-quote", "it's /o'clock"},
		{"double-quote-and-expansion", `say "hi" to $HOME and ` + "`backtick`"},
		{"injection-shaped", `'; unset PATH; echo pwned; '`},
		{"newline", "line1\nline2"},
		{"empty", ""},
	}
	names := []string{"GH_CONFIG_DIR", "HERMES_HOME", "HOME", "PYTHONPATH", "XDG_CONFIG_HOME"}
	scratch := "export GH_CONFIG_DIR=scratch-gh HERMES_HOME=scratch-hermes HOME=scratch-home " +
		"PYTHONPATH=scratch-py XDG_CONFIG_HOME=scratch-xdg FOO=bystander"
	for _, tc := range values {
		t.Run(tc.label, func(t *testing.T) {
			captured := map[string]*string{
				"GH_CONFIG_DIR":   nil, // absent: must end UNSET, not empty-but-set
				"HERMES_HOME":     strPtr("/tmp/pc88-hermes"),
				"HOME":            strPtr(tc.value),
				"PYTHONPATH":      strPtr("/tmp/pc88 py:/tmp/other"),
				"XDG_CONFIG_HOME": strPtr("/tmp/pc88-xdg"),
			}
			script, err := RestoreScript(captured)
			if err != nil {
				t.Fatalf("RestoreScript: %v", err)
			}
			got := runUnderBash(t, scratch, script, append(names, "FOO"))
			if gh := got["GH_CONFIG_DIR"]; gh != nil {
				t.Fatalf("absent name reintroduced as %q, want unset", *gh)
			}
			if home := got["HOME"]; home == nil || *home != tc.value {
				t.Fatalf("HOME = %v, want %q", got["HOME"], tc.value)
			}
			if py := got["PYTHONPATH"]; py == nil || *py != "/tmp/pc88 py:/tmp/other" {
				t.Fatalf("PYTHONPATH = %v, want spaced value verbatim", got["PYTHONPATH"])
			}
			if hh := got["HERMES_HOME"]; hh == nil || *hh != "/tmp/pc88-hermes" {
				t.Fatalf("HERMES_HOME = %v", got["HERMES_HOME"])
			}
			if xdg := got["XDG_CONFIG_HOME"]; xdg == nil || *xdg != "/tmp/pc88-xdg" {
				t.Fatalf("XDG_CONFIG_HOME = %v", got["XDG_CONFIG_HOME"])
			}
			if foo := got["FOO"]; foo == nil || *foo != "bystander" {
				t.Fatalf("bystander FOO clobbered: %v", got["FOO"])
			}
		})
	}
}

func TestRestoreScriptAbsentVsEmptyUnderBash(t *testing.T) {
	requireBash(t)
	captured := map[string]*string{
		"GH_CONFIG_DIR":   nil,
		"HOME":            strPtr(""),
		"XDG_CONFIG_HOME": strPtr("x y"),
	}
	script, err := RestoreScript(captured)
	if err != nil {
		t.Fatalf("RestoreScript: %v", err)
	}
	pre := "export GH_CONFIG_DIR=scratch HOME=scratch XDG_CONFIG_HOME=scratch"
	got := runUnderBash(t, pre, script, []string{"GH_CONFIG_DIR", "HOME", "XDG_CONFIG_HOME"})
	if got["GH_CONFIG_DIR"] != nil {
		t.Fatalf("absent name must stay unset, got %q", *got["GH_CONFIG_DIR"])
	}
	if home := got["HOME"]; home == nil || *home != "" {
		t.Fatalf("set-to-empty must stay empty-but-set, got %v", got["HOME"])
	}
	if xdg := got["XDG_CONFIG_HOME"]; xdg == nil || *xdg != "x y" {
		t.Fatalf("XDG_CONFIG_HOME = %v, want %q", got["XDG_CONFIG_HOME"], "x y")
	}
}

func TestRestoreScriptRefusesMalformedNames(t *testing.T) {
	for _, name := range []string{"", "EVIL; rm -rf /", "has space", "1STARTS-digit", "a=b"} {
		if ValidName(name) {
			t.Fatalf("ValidName(%q) = true, want false", name)
		}
		if _, err := RestoreScript(map[string]*string{name: strPtr("v")}); err == nil {
			t.Fatalf("RestoreScript accepted malformed name %q", name)
		}
	}
	for _, name := range []string{"HOME", "_UNDER", "a1", "Z_9"} {
		if !ValidName(name) {
			t.Fatalf("ValidName(%q) = false, want true", name)
		}
	}
}

// ---------------------------------------------------------------------------
// Refcount: first captures, inner/parallel exit does not restore, last restores.
// ---------------------------------------------------------------------------

func TestRefcountInnerExitDoesNotRestoreOuterLastRestores(t *testing.T) {
	env := &fakeEnv{}
	resolve := func(string) string { return "k-refcount" }
	outer := New("pc88-outer-child", resolve, staticEnvFor(env))
	inner := New("pc88-inner-child", resolve, staticEnvFor(env))
	if outer.Key() != "k-refcount" || inner.Key() != "k-refcount" {
		t.Fatalf("both child ids must resolve onto the shared key, got %q / %q", outer.Key(), inner.Key())
	}
	outer.Enter()
	if env.captureCount() != 1 {
		t.Fatalf("first enter must capture once, got %d", env.captureCount())
	}
	inner.Enter()
	if env.captureCount() != 1 {
		t.Fatalf("second enter must join, not recapture; captures=%d", env.captureCount())
	}
	inner.Release()
	if env.restoreCount() != 0 {
		t.Fatalf("inner exit must not restore, restores=%d", env.restoreCount())
	}
	if env.captureCount() != 1 {
		t.Fatalf("inner exit must not trigger a recapture, captures=%d", env.captureCount())
	}
	outer.Release()
	if env.restoreCount() != 1 {
		t.Fatalf("last exit must restore exactly once, restores=%d", env.restoreCount())
	}
	want := env.capturesSnapshot()[0]
	if got := env.restoreAt(0); !reflect.DeepEqual(got, want) {
		t.Fatalf("restore used %v, want the original capture %v", got, want)
	}
}

func (f *fakeEnv) capturesSnapshot() []map[string]*string {
	f.mu.Lock()
	defer f.mu.Unlock()
	cp := make([]map[string]*string, len(f.captures))
	for i, captured := range f.captures {
		m := make(map[string]*string, len(captured))
		for name, value := range captured {
			if value == nil {
				m[name] = nil
			} else {
				v := *value
				m[name] = &v
			}
		}
		cp[i] = m
	}
	return cp
}

func TestReleaseIdempotentPerInstance(t *testing.T) {
	env := &fakeEnv{}
	resolve := func(string) string { return "k-idempotent" }
	g := New("child", resolve, staticEnvFor(env))
	g.Enter()
	g.Release()
	g.Release() // the adopted-runner finally fires after the worker finally
	g.Release()
	if env.restoreCount() != 1 {
		t.Fatalf("idempotent release must restore exactly once, got %d", env.restoreCount())
	}
}

func TestEnterDuringRestoreJoinsOriginalCapture(t *testing.T) {
	env := &fakeEnv{}
	resolve := func(string) string { return "k-join" }
	started := make(chan struct{})
	unblock := make(chan struct{})
	var once sync.Once
	env.restoreFn = func(captured map[string]*string) error {
		once.Do(func() {
			close(started)
			<-unblock
		})
		env.recordRestore(captured)
		return nil
	}
	a := New("child-a", resolve, staticEnvFor(env))
	a.Enter()
	released := make(chan struct{})
	go func() { defer close(released); a.Release() }()
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("release never reached the restore phase")
	}
	// While the restore is blocked, a fresh enter must neither capture nor
	// proceed on half-restored state; it waits, then joins the capture.
	entered := make(chan *Guard)
	go func() {
		b := New("child-b", resolve, staticEnvFor(env))
		b.Enter()
		entered <- b
	}()
	select {
	case b := <-entered:
		_ = b
		t.Fatal("enter completed mid-restore; it must wait for the restore")
	case <-time.After(200 * time.Millisecond):
	}
	close(unblock)
	select {
	case <-released:
	case <-time.After(5 * time.Second):
		t.Fatal("release never finished after unblock")
	}
	var b *Guard
	select {
	case b = <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("enter never completed after the restore finished")
	}
	if env.captureCount() != 1 {
		t.Fatalf("enter after restore must join the original capture, captures=%d", env.captureCount())
	}
	b.Release()
	if env.restoreCount() != 2 {
		t.Fatalf("second guard's exit must restore, restores=%d", env.restoreCount())
	}
	if got, want := env.restoreAt(1), env.capturesSnapshot()[0]; !reflect.DeepEqual(got, want) {
		t.Fatalf("joined guard restored %v, want the ORIGINAL capture %v", got, want)
	}
}

func TestConcurrentEntersCaptureOnceLastReleaseRestores(t *testing.T) {
	env := &fakeEnv{}
	resolve := func(string) string { return "k-concurrent" }
	const n = 8
	ready := make(chan *Guard, n)
	var wg sync.WaitGroup
	for i := range n {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			g := New(fmt.Sprintf("child-%d", i), resolve, staticEnvFor(env))
			g.Enter()
			ready <- g
		}(i)
	}
	wg.Wait()
	guards := make([]*Guard, 0, n)
	for range n {
		guards = append(guards, <-ready)
	}
	if env.captureCount() != 1 {
		t.Fatalf("concurrent first-enters must capture exactly once, got %d", env.captureCount())
	}
	for _, g := range guards {
		g.Release()
	}
	if env.restoreCount() != 1 {
		t.Fatalf("only the last release must restore, got %d", env.restoreCount())
	}
}

func TestNoLiveEnvironmentAtExitIsNoop(t *testing.T) {
	env := &fakeEnv{}
	resolve := func(string) string { return "k-noenv" }
	// Live at enter, gone at exit: envFor returns nil once the guard holds.
	live := &atomicEnvHolder{env: env}
	g := New("child", resolve, live.envFor)
	g.Enter()
	live.Set(nil)
	g.Release() // must not panic and must not restore through a nil env
	if env.restoreCount() != 0 {
		t.Fatalf("restore with no live environment must be a no-op, restores=%d", env.restoreCount())
	}
}

type atomicEnvHolder struct {
	mu  sync.Mutex
	env Environment
}

func (h *atomicEnvHolder) envFor(string) Environment {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.env
}

func (h *atomicEnvHolder) Set(env Environment) {
	h.mu.Lock()
	h.env = env
	h.mu.Unlock()
}

func TestCaptureFallsBackToProcessEnvAbsentVsEmpty(t *testing.T) {
	resolve := func(string) string { return "k-procenv" }
	t.Setenv("XDG_CONFIG_HOME", "/tmp/pc88-xdg")
	prev, had := os.LookupEnv("GH_CONFIG_DIR")
	os.Unsetenv("GH_CONFIG_DIR")
	defer func() {
		if had {
			os.Setenv("GH_CONFIG_DIR", prev)
		} else {
			os.Unsetenv("GH_CONFIG_DIR")
		}
	}()
	var nilEnv Environment // envFor yields a nil Environment: process-env path
	g := New("child", resolve, func(string) Environment { return nilEnv })
	g.Enter()
	script, err := RestoreScript(captureIdentity("k-procenv", func(string) Environment { return nil }))
	if err != nil {
		t.Fatalf("RestoreScript: %v", err)
	}
	got := runUnderBash(t, "export GH_CONFIG_DIR=scratch XDG_CONFIG_HOME=scratch", script,
		[]string{"GH_CONFIG_DIR", "XDG_CONFIG_HOME"})
	if got["GH_CONFIG_DIR"] != nil {
		t.Fatalf("process-env-absent name must restore as unset, got %q", *got["GH_CONFIG_DIR"])
	}
	if xdg := got["XDG_CONFIG_HOME"]; xdg == nil || *xdg != "/tmp/pc88-xdg" {
		t.Fatalf("process-env value not restored: %v", got["XDG_CONFIG_HOME"])
	}
	g.Release()
}
