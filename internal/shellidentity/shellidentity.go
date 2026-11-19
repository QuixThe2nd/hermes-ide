// Package shellidentity is the Go twin of the delegate-child shell identity
// guard in tools/shell_identity_guard.py: delegate children share the parent's
// terminal on purpose (one shell, one cwd, one set of installed packages),
// and the shared bash snapshot — `export -p` re-dumped after every command —
// persists a child's HOME/HERMES_HOME/PYTHONPATH into the parent's next
// command, so GitHub CLI ends up looking in a scratch home. Same rules,
// independently tested; production Python never imports or shells out to it.
//
// Contract (shared with the Python twin):
//   - Identity names, exactly: GH_CONFIG_DIR, HERMES_HOME, HOME, PYTHONPATH,
//     XDG_CONFIG_HOME.
//   - Refcount the shared environment key (what the resolver returns for a
//     raw child task id, never the child id itself): the first guard
//     captures, later guards join the capture, and only the LAST exit
//     restores. An inner or parallel-sibling exit never restores.
//   - Release is idempotent per guard instance: the worker's with-block
//     finally and the adopted-runner finally may both call it.
//   - Capture from the live environment registered under the key; otherwise
//     from the process env, recording unset names as absent (nil), never as
//     the empty string. Absent and set-to-empty are distinct states.
//   - Restore by executing ONE shell command that exports each present name
//     with POSIX-safe single-quote escaping and unsets each absent name;
//     names outside the captured mapping are never touched. With no live
//     environment at exit, restore is a no-op.
//   - An enter that arrives while a restore is in progress waits for it, then
//     joins the capture that restore re-established; it never captures
//     mid-restore, and two first-enters never both capture. No mutex is held
//     across an environment round-trip — the phase flag marks that window.
package shellidentity

import (
	"fmt"
	"os"
	"sort"
	"strings"
	"sync"
)

// IdentityNames is the exact identity set, in the Python twin's order.
var IdentityNames = [...]string{"GH_CONFIG_DIR", "HERMES_HOME", "HOME", "PYTHONPATH", "XDG_CONFIG_HOME"}

const (
	phaseCapture = "capture"
	phaseRestore = "restore"
)

// Environment is the live shared shell: capture probes presence and exact
// values for names; restore applies a captured mapping in one shell command.
// A nil value in the mapping means the name is absent (unset), not empty.
type Environment interface {
	CaptureShellIdentity(names []string) (map[string]*string, error)
	RestoreShellIdentity(captured map[string]*string) error
}

// EnvironmentFor returns the live environment registered under a shared key,
// or nil when none exists.
type EnvironmentFor func(key string) Environment

// KeyResolver maps a raw child task id onto the shared environment key.
type KeyResolver func(taskID string) string

// QuotePOSIX single-quotes value so spaces, single quotes, globs, expansions
// and newlines reach export verbatim: `it's` becomes `'it'\”s'`.
func QuotePOSIX(value string) string {
	return "'" + strings.ReplaceAll(value, "'", `'\''`) + "'"
}

// ValidName reports whether name is a legal POSIX exported variable name:
// [A-Za-z_][A-Za-z0-9_]*. Anything else is refused so a malformed entry can
// never become shell syntax.
func ValidName(name string) bool {
	if name == "" {
		return false
	}
	for i, r := range name {
		switch {
		case r == '_' || ('a' <= r && r <= 'z') || ('A' <= r && r <= 'Z'):
		case i > 0 && '0' <= r && r <= '9':
		default:
			return false
		}
	}
	return true
}

// RestoreScript builds the one-line POSIX command that exports every present
// name in captured with QuotePOSIX escaping and unsets every absent name,
// sorted by name for deterministic output. Names outside captured are never
// mentioned; malformed names are an error, not a best-effort skip.
func RestoreScript(captured map[string]*string) (string, error) {
	names := make([]string, 0, len(captured))
	for name := range captured {
		if !ValidName(name) {
			return "", fmt.Errorf("shellidentity: refusing malformed shell variable name %q", name)
		}
		names = append(names, name)
	}
	sort.Strings(names)
	statements := make([]string, 0, len(names))
	for _, name := range names {
		if value := captured[name]; value == nil {
			statements = append(statements, "unset "+name)
		} else {
			statements = append(statements, "export "+name+"="+QuotePOSIX(*value))
		}
	}
	return strings.Join(statements, "; "), nil
}

// sharedIdentity is the refcount and capture state for ONE shared key.
type sharedIdentity struct {
	mu       sync.Mutex
	changed  *sync.Cond
	refcount int
	captured map[string]*string
	phase    string // "", phaseCapture, phaseRestore
}

var (
	registryMu sync.Mutex
	registry   = map[string]*sharedIdentity{}
)

func sharedFor(key string) *sharedIdentity {
	registryMu.Lock()
	defer registryMu.Unlock()
	shared := registry[key]
	if shared == nil {
		shared = &sharedIdentity{}
		shared.changed = sync.NewCond(&shared.mu)
		registry[key] = shared
	}
	return shared
}

// Guard refcounts one shared environment key around a delegate child.
type Guard struct {
	key    string
	envFor EnvironmentFor
	shared *sharedIdentity
	// mu serializes the per-instance entered/released flags so the worker
	// finally and the adopted-runner finally may race Release safely.
	mu       sync.Mutex
	entered  bool
	released bool
}

// New resolves taskID onto the shared environment key and binds the guard to
// it. Resolution happens once, at construction, exactly like the Python twin.
func New(taskID string, resolve KeyResolver, envFor EnvironmentFor) *Guard {
	key := resolve(taskID)
	return &Guard{key: key, envFor: envFor, shared: sharedFor(key)}
}

// Key returns the shared environment key this guard refcounts.
func (g *Guard) Key() string { return g.key }

// captureIdentity captures from the live environment registered under key,
// falling back to the process env. Unset names are recorded as nil, never as
// the empty string.
func captureIdentity(key string, envFor EnvironmentFor) map[string]*string {
	if env := envFor(key); env != nil {
		if captured, err := env.CaptureShellIdentity(IdentityNames[:]); err == nil && captured != nil {
			return captured
		}
	}
	captured := make(map[string]*string, len(IdentityNames))
	for _, name := range IdentityNames {
		if value, ok := os.LookupEnv(name); ok {
			v := value
			captured[name] = &v
		} else {
			captured[name] = nil
		}
	}
	return captured
}

// Enter takes a reference on the shared key. The first enter captures the
// identity names; later enters join it. An enter that finds a capture or
// restore in progress waits for it — and after a restore, joins the capture
// that restore re-established instead of probing mid-transition.
func (g *Guard) Enter() {
	shared := g.shared
	waitedOutRestore := false
	captureNow := false
	shared.mu.Lock()
	for shared.phase != "" {
		if shared.phase == phaseRestore {
			waitedOutRestore = true
		}
		shared.changed.Wait()
	}
	switch {
	case shared.refcount > 0:
		shared.refcount++
	case waitedOutRestore && shared.captured != nil:
		shared.refcount = 1
	default:
		shared.phase = phaseCapture
		captureNow = true
	}
	// Set before publishing any refcount a concurrent Release of THIS
	// instance could observe; Release reads the flags under the same mu.
	g.mu.Lock()
	g.entered = true
	g.released = false
	g.mu.Unlock()
	shared.mu.Unlock()
	if captureNow {
		captured := captureIdentity(g.key, g.envFor)
		shared.mu.Lock()
		shared.captured = captured
		shared.refcount = 1
		shared.phase = ""
		shared.changed.Broadcast()
		shared.mu.Unlock()
	}
}

// Release drops this guard's reference; the LAST exit restores the captured
// identity on the live environment. Idempotent per instance — a second call
// (worker finally + adopted-runner finally) never decrements twice. With no
// live environment under the key at exit, restore is a no-op.
func (g *Guard) Release() {
	g.mu.Lock()
	if !g.entered || g.released {
		g.mu.Unlock()
		return
	}
	g.released = true
	g.mu.Unlock()
	shared := g.shared
	shared.mu.Lock()
	if shared.refcount <= 0 {
		shared.mu.Unlock()
		return
	}
	shared.refcount--
	if shared.refcount > 0 {
		shared.mu.Unlock()
		return
	}
	captured := shared.captured
	shared.phase = phaseRestore
	shared.mu.Unlock()
	if env := g.envFor(g.key); env != nil && captured != nil {
		if err := env.RestoreShellIdentity(captured); err != nil {
			_ = err // best-effort, mirrors the Python twin's warning log
		}
	}
	shared.mu.Lock()
	shared.phase = ""
	shared.changed.Broadcast()
	shared.mu.Unlock()
}
