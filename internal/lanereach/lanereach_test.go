package lanereach

import (
	"encoding/json"
	"os"
	"os/exec"
	"reflect"
	"sort"
	"strings"
	"testing"
)

// The shared contract table (testdata/lane_cases.json) is the same table the
// Python contract test (tests/tools/test_delegate_lane_reach_contract.py)
// runs through the compiled cmd/lanereach binary and the Python module, so
// all three must agree on every record. The in-source tables below cover the
// host-lookup layers (resolver orders, pin plumbing) the shared table
// deliberately freezes.

// fakeHost is a hermetic Host: PATH contents, env, home and executable
// files are all declared per case.
type fakeHost struct {
	home      string
	homeErr   error
	env       map[string]string
	onPath    map[string]string // binary name → resolved path
	execFiles map[string]bool   // absolute path → executable file?
}

func (f fakeHost) LookPath(bin string) (string, error) {
	if p, ok := f.onPath[bin]; ok {
		return p, nil
	}
	return "", &exec.Error{Name: bin, Err: exec.ErrNotFound}
}

func (f fakeHost) HomeDir() (string, error) { return f.home, f.homeErr }

func (f fakeHost) Getenv(key string) string {
	if f.env == nil {
		return ""
	}
	return f.env[key]
}

func (f fakeHost) IsExecutableFile(path string) bool { return f.execFiles[path] }

func TestResolveClaudeBinaryMirrorsTheGLMOrder(t *testing.T) {
	localGlm := "/home/dev/.local/bin/claude-glm"
	cases := []struct {
		name string
		host fakeHost
		want string
	}{
		{
			name: "env override wins when executable",
			host: fakeHost{
				env:       map[string]string{"CLAUDE_GLM_BIN": "/opt/wrappers/claude-glm"},
				execFiles: map[string]bool{"/opt/wrappers/claude-glm": true},
			},
			want: "/opt/wrappers/claude-glm",
		},
		{
			name: "env override with ~ expanded",
			host: fakeHost{
				home:      "/home/dev",
				env:       map[string]string{"CLAUDE_GLM_BIN": "~/bin/claude-glm"},
				execFiles: map[string]bool{"/home/dev/bin/claude-glm": true},
			},
			want: "/home/dev/bin/claude-glm",
		},
		{
			name: "non-executable override falls through to local bin",
			host: fakeHost{
				env:       map[string]string{"CLAUDE_GLM_BIN": "/opt/wrappers/not-a-file"},
				home:      "/home/dev",
				execFiles: map[string]bool{localGlm: true},
			},
			want: localGlm,
		},
		{
			name: "local bin before PATH",
			host: fakeHost{
				home:      "/home/dev",
				execFiles: map[string]bool{localGlm: true},
				onPath:    map[string]string{"claude-glm": "/usr/bin/claude-glm"},
			},
			want: localGlm,
		},
		{
			name: "claude-glm on PATH before bare claude",
			host: fakeHost{
				onPath: map[string]string{"claude-glm": "/usr/bin/claude-glm", "claude": "/usr/bin/claude"},
			},
			want: "/usr/bin/claude-glm",
		},
		{
			name: "bare claude is the last resort",
			host: fakeHost{
				onPath: map[string]string{"claude": "/usr/bin/claude"},
			},
			want: "/usr/bin/claude",
		},
		{
			name: "kimi override is never consulted",
			host: fakeHost{
				env:    map[string]string{"CLAUDE_KIMI_BIN": "/opt/wrappers/claude-kimi"},
				onPath: map[string]string{"claude-kimi": "/usr/bin/claude-kimi"},
			},
			want: "",
		},
		{
			name: "nothing resolves",
			host: fakeHost{home: "/home/dev"},
			want: "",
		},
	}
	for _, c := range cases {
		got, ok := ResolveClaudeBinary(c.host)
		if got != c.want || ok != (c.want != "") {
			t.Errorf("%s: got (%q, %v), want (%q, %v)",
				c.name, got, ok, c.want, c.want != "")
		}
	}
}

func TestResolveOMPBinaryHonorsConfiguration(t *testing.T) {
	cases := []struct {
		name       string
		host       fakeHost
		configured string
		want       string
	}{
		{
			name:       "empty setting defaults to omp",
			host:       fakeHost{onPath: map[string]string{"omp": "/usr/bin/omp"}},
			configured: "",
			want:       "/usr/bin/omp",
		},
		{
			name:       "configured name is looked up",
			host:       fakeHost{onPath: map[string]string{"ompd": "/opt/omp/bin/ompd"}},
			configured: "ompd",
			want:       "/opt/omp/bin/ompd",
		},
		{
			name:       "configured absolute path",
			host:       fakeHost{onPath: map[string]string{"/opt/omp/bin/ompd": "/opt/omp/bin/ompd"}},
			configured: "/opt/omp/bin/ompd",
			want:       "/opt/omp/bin/ompd",
		},
		{
			name:       "missing binary is not reachable",
			host:       fakeHost{onPath: map[string]string{"other": "/usr/bin/other"}},
			configured: "",
			want:       "",
		},
	}
	for _, c := range cases {
		got, ok := ResolveOMPBinary(c.host, c.configured)
		if got != c.want || ok != (c.want != "") {
			t.Errorf("%s: got (%q, %v), want (%q, %v)",
				c.name, got, ok, c.want, c.want != "")
		}
	}
}

func TestExpandHome(t *testing.T) {
	host := fakeHost{home: "/home/dev"}
	cases := []struct{ in, want string }{
		{"~", "/home/dev"},
		{"~/wrapper", "/home/dev/wrapper"},
		{"~other/wrapper", "~other/wrapper"},
		{"/abs/wrapper", "/abs/wrapper"},
		{"relative/wrapper", "relative/wrapper"},
	}
	for _, c := range cases {
		if got := expandHome(host, c.in); got != c.want {
			t.Errorf("expandHome(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestHarnessReachableMatrix(t *testing.T) {
	ompHost := fakeHost{onPath: map[string]string{"omp": "/usr/bin/omp"}}
	claudeHost := fakeHost{
		home:      "/home/dev",
		execFiles: map[string]bool{"/home/dev/.local/bin/claude-glm": true},
	}
	cases := []struct {
		name    string
		host    fakeHost
		harness string
		want    bool
	}{
		{"subagent needs no binary", fakeHost{}, HarnessSubagent, true},
		{"omp found", ompHost, HarnessOMP, true},
		{"omp missing", fakeHost{}, HarnessOMP, false},
		{"claude wrapper found", claudeHost, HarnessClaude, true},
		{"claude missing", fakeHost{}, HarnessClaude, false},
		{"unknown harness", ompHost, "codex", false},
		{"empty harness", ompHost, "", false},
		{"wrong case never matches", ompHost, "OMP", false},
		{"wrong case claude", ompHost, "Claude", false},
	}
	for _, c := range cases {
		if got := HarnessReachable(c.host, c.harness, ""); got != c.want {
			t.Errorf("%s: got %v, want %v", c.name, got, c.want)
		}
	}
}

func TestResolveRunnableLadder(t *testing.T) {
	// hosts is a matrix over which of omp/claude are resolvable; subagent
	// is always resolvable.
	hosts := map[string]fakeHost{
		"omp+claude": {onPath: map[string]string{"omp": "/usr/bin/omp"},
			execFiles: map[string]bool{"/home/dev/.local/bin/claude-glm": true}, home: "/home/dev"},
		"omp only":    {onPath: map[string]string{"omp": "/usr/bin/omp"}},
		"claude only": {home: "/home/dev", execFiles: map[string]bool{"/home/dev/.local/bin/claude-glm": true}},
		"neither":     {},
	}
	cases := []struct {
		name    string
		hostKey string
		harness string
		want    string
	}{
		{"reachable omp stays omp", "omp+claude", HarnessOMP, HarnessOMP},
		{"omp missing prefers claude", "claude only", HarnessOMP, HarnessClaude},
		{"omp and claude missing land subagent", "neither", HarnessOMP, HarnessSubagent},
		{"pinned claude never falls back up to omp", "omp only", HarnessClaude, HarnessClaude},
		{"subagent stays subagent", "neither", HarnessSubagent, HarnessSubagent},
	}
	for _, c := range cases {
		if got := ResolveRunnable(hosts[c.hostKey], c.harness, ""); got != c.want {
			t.Errorf("%s: got %q, want %q", c.name, got, c.want)
		}
	}
}

func TestLaneReachableIsTheResolvedHarness(t *testing.T) {
	empty := fakeHost{}
	claude := fakeHost{home: "/home/dev",
		execFiles: map[string]bool{"/home/dev/.local/bin/claude-glm": true}}
	if !LaneReachable(empty, HarnessOMP, "") {
		t.Error("omp ladder ends at the always-reachable subagent; lane must be visible")
	}
	if LaneReachable(claude, HarnessClaude, "") != true {
		t.Error("pinned claude with wrapper present must be visible")
	}
	if got := LaneReachable(empty, "codex", ""); got {
		t.Error("unknown harness is never reachable")
	}
}

// ── shared contract table ────────────────────────────────────────────────

type sharedCase struct {
	Name      string             `json:"name"`
	Op        string             `json:"op"`
	Harness   string             `json:"harness"`
	OmpBinary string             `json:"omp_binary"`
	Pin       map[string]*string `json:"pin"`
	Expect    Response           `json:"expect"`
}

func loadSharedCases(t *testing.T) []sharedCase {
	t.Helper()
	raw, err := os.ReadFile("testdata/lane_cases.json")
	if err != nil {
		t.Fatalf("read shared table: %v", err)
	}
	var table struct {
		Cases []sharedCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parse shared table: %v", err)
	}
	if len(table.Cases) == 0 {
		t.Fatal("shared table is empty")
	}
	return table.Cases
}

func TestApplyMatchesTheSharedTable(t *testing.T) {
	for _, c := range loadSharedCases(t) {
		got, err := Apply(OSHost{}, Request{
			Op: c.Op, Harness: c.Harness, OmpBinary: c.OmpBinary, Pin: c.Pin,
		})
		if err != nil {
			t.Errorf("%s: %v", c.Name, err)
			continue
		}
		if got != c.Expect {
			t.Errorf("%s: got %+v, want %+v", c.Name, got, c.Expect)
		}
	}
}

func TestApplyRejectsUnknownOps(t *testing.T) {
	if _, err := Apply(OSHost{}, Request{Op: "probe", Harness: "omp"}); err == nil ||
		!strings.Contains(err.Error(), "unknown op") {
		t.Errorf("unknown op: got err %v, want unknown-op error", err)
	}
}

func TestApplyWithoutPinUsesTheHostProbes(t *testing.T) {
	// subagent consults no probe, so the unpinned host path is answerable
	// deterministically on any machine.
	got, err := Apply(OSHost{}, Request{Op: OpResolve, Harness: HarnessSubagent})
	if err != nil || got != (Response{Reachable: true, Harness: HarnessSubagent}) {
		t.Errorf("unpinned subagent resolve: got (%+v, %v)", got, err)
	}
	got, err = Apply(OSHost{}, Request{Harness: HarnessSubagent}) // default op
	if err != nil || got != (Response{Reachable: true, Harness: HarnessSubagent}) {
		t.Errorf("default op: got (%+v, %v)", got, err)
	}
}

func TestApplyPinOverridesTheHostLookup(t *testing.T) {
	// Pin every probe to not-found even though the host has a claude
	// wrapper: the pin must win, and the ladder must still land subagent.
	host := fakeHost{home: "/home/dev",
		execFiles: map[string]bool{
			"/home/dev/.local/bin/claude-glm": true,
		}}
	got, err := Apply(host, Request{
		Op: OpResolve, Harness: HarnessOMP,
		Pin: map[string]*string{"omp": nil, "claude": nil},
	})
	if err != nil || got != (Response{Reachable: true, Harness: HarnessSubagent}) {
		t.Errorf("pinned-missing ladder: got (%+v, %v)", got, err)
	}
	// An empty-string pin also pins not-found.
	emptyPin := ""
	got, err = Apply(host, Request{
		Op: OpReachable, Harness: HarnessClaude,
		Pin: map[string]*string{"claude": &emptyPin},
	})
	if err != nil || got != (Response{Reachable: false, Harness: HarnessClaude}) {
		t.Errorf("empty-string pin: got (%+v, %v)", got, err)
	}
	// A partial pin leaves the other probe on the host: claude unpinned
	// resolves through the fake host.
	got, err = Apply(host, Request{
		Op: OpReachable, Harness: HarnessClaude,
		Pin: map[string]*string{"omp": nil},
	})
	if err != nil || got != (Response{Reachable: true, Harness: HarnessClaude}) {
		t.Errorf("partial pin: got (%+v, %v)", got, err)
	}
}

func TestSharedTableCoversTheLadderContract(t *testing.T) {
	// Every branch of the reachability + fallback contract appears in the
	// table, so neither twin can drift on an untested branch.
	names := ""
	for _, c := range loadSharedCases(t) {
		names += c.Name + " "
	}
	required := []string{
		"subagent_needs_no_binary",
		"omp_stays_when_reachable",
		"steps_to_claude",
		"land_subagent",
		"pinned_claude_missing",
		"removed_cursor",
		"unknown_harness",
	}
	for _, needle := range required {
		if !strings.Contains(names, needle) {
			t.Errorf("shared table lost the %q case", needle)
		}
	}
}

func TestSharedTablePinsEveryProbe(t *testing.T) {
	// The table must freeze both probes per case, or the Go run would
	// consult the real host and stop being reproducible.
	for _, c := range loadSharedCases(t) {
		keys := make([]string, 0, len(c.Pin))
		for k := range c.Pin {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		if !reflect.DeepEqual(keys, []string{HarnessClaude, HarnessOMP}) {
			t.Errorf("%s: pin keys = %v, want both omp/claude", c.Name, keys)
		}
	}
}
