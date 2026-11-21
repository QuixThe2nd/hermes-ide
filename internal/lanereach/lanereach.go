// Package lanereach is the Go twin of the delegation lane harness
// reachability contract in tools/delegate_lane_specs.py: which harness
// backends can actually run on a host (harness_reachable), the fallback
// ladder the bundled omp default steps down through when the omp binary is
// missing (resolve_runnable_spec: omp → claude → subagent), and lane
// visibility after that ladder (lane_harness_reachable).
//
// This is the canonical statement of the contract; the Python module is what
// the runtime gates lane tools with (executed by tests, not imported at
// runtime — deployments (pip, docker) carry no Go toolchain, the same
// arrangement as every other internal/* twin). cmd/lanereach is the
// executable form: the Python contract test
// (tests/tools/test_delegate_lane_reach_contract.py) builds it and asserts
// identical answers for every shared-table record in
// testdata/lane_cases.json, and the Go tests run the same table as a second
// cross-check.
//
// Contract (shared with the Python twin):
//   - "subagent" is always reachable: it is an in-process child agent with
//     no external binary.
//   - "omp" is reachable when the configured binary (config omp.binary,
//     default "omp") resolves on PATH; an empty or missing setting means
//     "omp".
//   - "claude" is reachable when the default Claude Code wrapper resolves,
//     in the GLM order of tools.claude_agent_tool.resolve_claude_binary
//     called with no model: the CLAUDE_GLM_BIN override (an executable
//     file, "~" expanded), then ~/.local/bin/claude-glm, then claude-glm on
//     PATH, then bare claude on PATH. The kimi wrapper is never consulted.
//   - Any other harness name (empty, unknown, wrong case) is unreachable;
//     probes never raise.
//   - A lane whose configured harness is "omp" and whose omp binary is
//     missing runs on the first reachable fallback, in order: claude, then
//     the in-process subagent — so the ladder always ends runnable. A lane
//     pinned to claude never falls back: it is simply hidden while that
//     wrapper is unavailable.
//   - Lane visibility is the reachability of the ladder-resolved harness.
//
// exec.LookPath is the Go-native PATH lookup; on Windows it resolves bare
// names differently from Python's shutil.which (PATHEXT), so the shared
// table pins every probe and both twins stay host-independent.
package lanereach

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// Harness names of the lane contract; mirrors HARNESSES in the Python twin.
const (
	HarnessOMP      = "omp"
	HarnessClaude   = "claude"
	HarnessSubagent = "subagent"
)

// DefaultOMpBinary is the omp binary name when the profile configures none;
// mirrors the Python probe's `config.get("binary") or "omp"` (the caller
// passes "" for an unset or empty setting).
const DefaultOMpBinary = "omp"

// The ~/.local/bin wrapper name the claude resolver looks for.
const claudeLocalBin = "claude-glm"

// HarnessFallbacks is the ladder the bundled omp default steps down through
// when the omp binary is missing, in order; mirrors _HARNESS_FALLBACKS.
var HarnessFallbacks = []string{HarnessClaude, HarnessSubagent}

// Host is every external read the reachability contract makes, injected so
// tests are hermetic. The production wiring is OSHost.
type Host interface {
	// LookPath resolves a binary on PATH (exec.LookPath semantics).
	LookPath(bin string) (string, error)
	// HomeDir is the user's home directory (os.UserHomeDir semantics).
	HomeDir() (string, error)
	// Getenv reads an environment variable ("" when unset).
	Getenv(key string) string
	// IsExecutableFile reports path being a non-directory file with an
	// execute bit (Python's is_file() + os.access(X_OK)).
	IsExecutableFile(path string) bool
}

// OSHost wires Host to the real process environment.
type OSHost struct{}

func (OSHost) LookPath(bin string) (string, error) { return exec.LookPath(bin) }
func (OSHost) HomeDir() (string, error)            { return os.UserHomeDir() }
func (OSHost) Getenv(key string) string            { return os.Getenv(key) }
func (OSHost) IsExecutableFile(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir() && info.Mode().Perm()&0o111 != 0
}

// ResolveOMPBinary resolves the configured omp binary on PATH, defaulting
// the name to "omp"; mirrors shutil.which(str(config.get("binary") or "omp")).
func ResolveOMPBinary(host Host, configured string) (string, bool) {
	name := configured
	if name == "" {
		name = DefaultOMpBinary
	}
	found, err := host.LookPath(name)
	return found, err == nil
}

// ResolveClaudeBinary mirrors the GLM order of
// tools.claude_agent_tool.resolve_claude_binary with no model (the form the
// reachability probe calls): the CLAUDE_GLM_BIN override (executable file,
// "~" expanded), ~/.local/bin/claude-glm, claude-glm on PATH, bare claude on
// PATH. Never falls back to the kimi wrapper or past a missing result.
func ResolveClaudeBinary(host Host) (string, bool) {
	if override := host.Getenv("CLAUDE_GLM_BIN"); override != "" {
		if p := expandHome(host, override); host.IsExecutableFile(p) {
			return p, true
		}
	}
	if home, err := host.HomeDir(); err == nil {
		if p := filepath.Join(home, ".local", "bin", claudeLocalBin); host.IsExecutableFile(p) {
			return p, true
		}
	}
	for _, name := range []string{claudeLocalBin, "claude"} {
		if found, err := host.LookPath(name); err == nil {
			return found, true
		}
	}
	return "", false
}

// expandHome expands a leading "~" or "~/" like Python's Path.expanduser for
// the override paths this contract reads; "~user" prefixes are left alone.
func expandHome(host Host, path string) string {
	if path == "~" {
		if home, err := host.HomeDir(); err == nil {
			return home
		}
		return path
	}
	if strings.HasPrefix(path, "~/") {
		if home, err := host.HomeDir(); err == nil {
			return filepath.Join(home, strings.TrimPrefix(path, "~/"))
		}
	}
	return path
}

// probeSet is the two external reachability probes; pinning one replaces
// its host lookup, which is how the shared table runs hermetically.
type probeSet struct {
	omp    func(configured string) (string, bool)
	claude func() (string, bool)
}

func hostProbes(host Host) probeSet {
	return probeSet{
		omp:    func(configured string) (string, bool) { return ResolveOMPBinary(host, configured) },
		claude: func() (string, bool) { return ResolveClaudeBinary(host) },
	}
}

// pinnedProbes freezes any probe whose key appears in pin: a non-empty
// string pins reachable at that path, nil or "" pins not-found, an absent
// key keeps the real host lookup for that probe.
func pinnedProbes(host Host, pin map[string]*string) probeSet {
	probe := hostProbes(host)
	freeze := func(v *string) func() (string, bool) {
		return func() (string, bool) {
			if v == nil || *v == "" {
				return "", false
			}
			return *v, true
		}
	}
	if v, ok := pin[HarnessOMP]; ok {
		probe.omp = func(string) (string, bool) { return freeze(v)() }
	}
	if v, ok := pin[HarnessClaude]; ok {
		probe.claude = freeze(v)
	}
	return probe
}

// reachable mirrors harness_reachable: true when a harness's binary/backend
// can actually run on this host.
func (p probeSet) reachable(harness, ompConfigured string) bool {
	switch harness {
	case HarnessSubagent:
		return true // in-process child agent: no external binary
	case HarnessOMP:
		_, ok := p.omp(ompConfigured)
		return ok
	case HarnessClaude:
		_, ok := p.claude()
		return ok
	default:
		return false
	}
}

// HarnessReachable mirrors tools.delegate_lane_specs.harness_reachable.
func HarnessReachable(host Host, harness, ompConfigured string) bool {
	return hostProbes(host).reachable(harness, ompConfigured)
}

// resolveRunnable mirrors resolve_runnable_spec's harness ladder (the spec
// merge around it stays in Python): a lane configured for omp whose binary
// is missing steps down to the first reachable fallback — claude, then the
// in-process subagent — and every other harness stays as configured.
func (p probeSet) resolveRunnable(harness, ompConfigured string) string {
	if harness == HarnessOMP && !p.reachable(HarnessOMP, ompConfigured) {
		for _, fallback := range HarnessFallbacks {
			if p.reachable(fallback, ompConfigured) {
				return fallback
			}
		}
	}
	return harness
}

// ResolveRunnable mirrors tools.delegate_lane_specs.resolve_runnable_spec's
// harness resolution; ompConfigured is the profile's omp.binary ("" = "omp").
func ResolveRunnable(host Host, harness, ompConfigured string) string {
	return hostProbes(host).resolveRunnable(harness, ompConfigured)
}

// LaneReachable mirrors lane_harness_reachable: lane visibility is the
// reachability of the ladder-resolved harness.
func LaneReachable(host Host, harness, ompConfigured string) bool {
	probe := hostProbes(host)
	return probe.reachable(probe.resolveRunnable(harness, ompConfigured), ompConfigured)
}

// Request is one cmd/lanereach invocation (JSON on stdin). OmpBinary is the
// profile's omp.binary ("" = unset → "omp"). Pin, when non-nil, freezes the
// two external probes for hermetic parity runs; an absent key keeps the
// real host lookup for that probe.
type Request struct {
	Op        string             `json:"op"`
	Harness   string             `json:"harness"`
	OmpBinary string             `json:"omp_binary"`
	Pin       map[string]*string `json:"pin"`
}

// Response is the single JSON object cmd/lanereach prints.
type Response struct {
	Reachable bool   `json:"reachable"`
	Harness   string `json:"harness"`
}

// Op values for Request.
const (
	// OpReachable answers harness_reachable: the configured harness's own
	// reachability, Harness echoed unchanged.
	OpReachable = "reachable"
	// OpResolve (also the default) answers resolve_runnable_spec +
	// lane_harness_reachable: Harness is the ladder-resolved runnable
	// harness, Reachable is lane visibility.
	OpResolve = "resolve"
)

// Apply answers one Request against host, honoring a probe pin table.
func Apply(host Host, req Request) (Response, error) {
	probe := pinnedProbes(host, req.Pin)
	switch req.Op {
	case OpResolve, "":
		runnable := probe.resolveRunnable(req.Harness, req.OmpBinary)
		return Response{Reachable: probe.reachable(runnable, req.OmpBinary), Harness: runnable}, nil
	case OpReachable:
		return Response{Reachable: probe.reachable(req.Harness, req.OmpBinary), Harness: req.Harness}, nil
	default:
		return Response{}, errors.New("unknown op: " + req.Op)
	}
}
