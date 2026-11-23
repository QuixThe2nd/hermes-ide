// Package forcebg is the canonical contract statement for steer-driven
// force-backgrounding of blocking delegations.
//
// A user follow-up arriving while the top-level agent is blocked inside a
// synchronous delegate_agent call releases the blocking waits: every
// still-running child is adopted onto the async-delegation rail (never
// stopped) and the text enters the live run as a steer. The Python runtime
// implementation is tools/delegate_tool_forcebg.py; this twin is pinned
// against it by tests/tools/test_forcebg_go_contract.py, which replays the
// shared table (testdata/forcebg_cases.json) through BOTH implementations
// and requires identical observable transcripts. Production Python never
// shells out to this module — the twin exists so the fork's Go-native
// statement of the contract is executable and tested, per the Go-only
// feature policy (go-line-balance gate).
//
// Invariants mirrored from the Python implementation (each has a test in
// forcebg_test.go and a case in the shared table):
//
//   - Registration is gated: config kill switch off, delegate depth != 0, or
//     a session that cannot receive a late completion → no wait registers
//     and the plain blocking behavior survives.
//   - The steer is accepted FIRST: a refused steer adopts nothing (the
//     user's message must never be lost to an unreleasable wait).
//   - adopt is idempotent per wait: concurrent callers join the in-flight
//     adoption and observe the same delegation id; the rail is dispatched
//     exactly once.
//   - A rail refusal keeps the wait live and blocking; only the ordinary
//     timeout may end it.
//   - Every user/model-visible string (entry notice, busy receipt, injected
//     echo cap) is owned here so the two implementations cannot drift.
package forcebg

import (
	"fmt"
	"strings"
	"sync"
	"time"
)

// Config carries the delegation-section knobs the gate reads.
type Config struct {
	// SteerForceBackground is delegation.steer_force_background (default on).
	SteerForceBackground bool
	// DeliverySupported mirrors async_delegation._omitted_default_delivery_supported():
	// a session that cannot receive a late completion must never detach work.
	DeliverySupported bool
}

// DefaultConfig returns the shipped defaults: kill switch on, delivery capable.
func DefaultConfig() Config {
	return Config{SteerForceBackground: true, DeliverySupported: true}
}

// Agent is the parent-side eligibility input for one session.
type Agent struct {
	// Key identifies the agent in the registry (the Python side keys the
	// registry by the agent object itself).
	Key string
	// DelegateDepth is the agent's _delegate_depth; only the top-level
	// parent the user is talking to (depth 0) may be force-backgrounded.
	DelegateDepth int
	// HasSteer reports whether agent.steer exists and is callable.
	HasSteer bool
	// SteerRefused scripts the agent's steer() verdict for the twin; the
	// runtime side asks the live agent.
	SteerRefused bool
}

// Run is one blocking delegation wait up for release.
type Run struct {
	// Parent is the waiting parent agent.
	Parent Agent
	// AdoptID is the delegation id the rail returns for this run's
	// adoption; empty means the rail refused (capacity race).
	AdoptID string
}

// Wait is one registered blocking wait. It mirrors _BlockingWait: wake is
// closed on adoption (id or refusal), settled only on a successful adoption.
type Wait struct {
	run      *Run
	done     bool // the worker settled; the wait is no longer live
	adopted  bool
	adopting bool
	wake     chan struct{}
	mu       sync.Mutex
}

// Registry holds the live waits per agent key.
type Registry struct {
	cfg   Config
	mu    sync.Mutex
	waits map[string][]*Wait
}

// NewRegistry builds a registry under cfg.
func NewRegistry(cfg Config) *Registry {
	return &Registry{cfg: cfg, waits: map[string][]*Wait{}}
}

// eligible applies the registration gate: kill switch, top-level depth,
// delivery capability. Ordering matches the Python implementation.
func (r *Registry) eligible(parent Agent) bool {
	if !r.cfg.SteerForceBackground {
		return false
	}
	if parent.DelegateDepth != 0 {
		return false
	}
	if !r.cfg.DeliverySupported {
		return false
	}
	return true
}

// Register adds one wait for run; it returns nil when ineligible — the
// caller then keeps today's plain blocking wait.
func (r *Registry) Register(run *Run) *Wait {
	if run == nil || !r.eligible(run.Parent) {
		return nil
	}
	w := &Wait{run: run, wake: make(chan struct{})}
	r.mu.Lock()
	r.waits[run.Parent.Key] = append(r.waits[run.Parent.Key], w)
	r.mu.Unlock()
	return w
}

// Unregister removes one wait from the registry (the wait has ended).
func (r *Registry) Unregister(w *Wait) {
	if w == nil {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	list := r.waits[w.run.Parent.Key]
	for i, cand := range list {
		if cand == w {
			r.waits[w.run.Parent.Key] = append(list[:i], list[i+1:]...)
			return
		}
	}
}

// LiveCount reports how many still-running, unadopted waits are registered.
func (r *Registry) LiveCount(key string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	n := 0
	for _, w := range r.waits[key] {
		if w.live() {
			n++
		}
	}
	return n
}

// HasEligible reports whether at least one live wait is registered — the
// gateway's cheap gate before committing to the release path.
func (r *Registry) HasEligible(key string) bool {
	return r.LiveCount(key) > 0
}

// live reports whether the wait is still running and unadopted. It takes the
// wait's own mutex: done and adopted are guarded by w.mu (written by
// Complete and Adopt), NOT by the registry lock. Callers may hold r.mu —
// r.mu → w.mu is the only lock order in the package (Complete, Adopt and
// ReleasedBySteer never take r.mu), so this cannot deadlock.
func (w *Wait) live() bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	return !w.done && !w.adopted
}

// Complete marks the wait's worker settled — a normally-finished child
// leaves no phantom behind.
func (w *Wait) Complete() {
	w.mu.Lock()
	w.done = true
	w.mu.Unlock()
}

// ReleasedBySteer reports whether a steer released this wait with a
// successful adoption (the blocking wait must return a force-background
// entry naming the delegation id).
func (w *Wait) ReleasedBySteer() bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	select {
	case <-w.wake:
		return w.adopted && w.run.AdoptID != ""
	default:
		return false
	}
}

// Adopt dispatches this wait's still-running worker onto the async-delegation
// rail, exactly once per wait. Concurrent callers join the in-flight
// adoption (bounded by the wake close) and observe the same verdict. The
// returned id is empty when the rail refused — the wait then stays live and
// blocking. The worker is never stopped here.
func (w *Wait) Adopt() string {
	w.mu.Lock()
	if w.adopted {
		id := w.run.AdoptID
		w.mu.Unlock()
		return id
	}
	if w.adopting {
		w.mu.Unlock()
		// Joiner: the dispatcher always closes wake when it finishes (id or
		// refusal); a dispatch somehow longer than this window reads as a
		// refusal — conservative, and never a second dispatch.
		select {
		case <-w.wake:
		}
		w.mu.Lock()
		id := w.run.AdoptID
		w.mu.Unlock()
		return id
	}
	w.adopting = true
	w.mu.Unlock()

	id := w.dispatch()
	w.mu.Lock()
	w.adopted = true
	w.mu.Unlock()
	close(w.wake)
	return id
}

// dispatch is the rail handoff; the twin scripts the verdict via AdoptID
// while the runtime calls _adopt_running_worker(future).
func (w *Wait) dispatch() string {
	return w.run.AdoptID
}

// AwaitRelease mirrors the runtime's wait_until_released loop: block until
// the worker settles (settled closes), the configured cap elapses, or a
// steer releases the wait with a successful adoption (wake closes with an
// id). A refusal closes wake WITHOUT an id — the loop drops the wake case
// and waits the wait out, which is what keeps today's blocking semantics
// alive on a rail refusal (a refused wait is not retried by later steers).
func AwaitRelease(w *Wait, settled <-chan struct{}, timeout time.Duration) string {
	var deadline <-chan time.Time
	if timeout >= 0 {
		deadline = time.After(timeout)
	} else {
		deadline = make(chan time.Time) // no cap: only settle or release end it
	}
	wake := w.wake
	for {
		var wakeCase <-chan struct{}
		if wake != nil {
			wakeCase = wake
		}
		select {
		case <-wakeCase:
			if w.ReleasedBySteer() {
				return "released"
			}
			wake = nil // refusal: no longer releasable; wait it out
		case <-settled:
			return "settled"
		case <-deadline:
			return "timeout"
		}
	}
}

// SteerOutcome is what one steer produced: acceptance plus the adopted ids.
type SteerOutcome struct {
	Accepted bool
	IDs      []string
}

// SteerForcingBackground delivers text as a steer and force-backgrounds
// every live blocking wait of the agent. The steer verdict comes FIRST: a
// refused steer adopts nothing — without an accepted steer there is nothing
// for the release to carry, so the user's message keeps today's queue
// semantics. On acceptance each live wait is adopted; a rail refusal
// degrades to the plain-steer path (no ids, wait keeps blocking).
func (r *Registry) SteerForcingBackground(agent Agent, text string) SteerOutcome {
	out := SteerOutcome{}
	if strings.TrimSpace(text) == "" {
		return out
	}
	if !agent.HasSteer || agent.SteerRefused {
		return out
	}
	out.Accepted = true
	r.mu.Lock()
	live := make([]*Wait, 0, len(r.waits[agent.Key]))
	for _, w := range r.waits[agent.Key] {
		if w.live() {
			live = append(live, w)
		}
	}
	r.mu.Unlock()
	for _, w := range live {
		if id := w.Adopt(); id != "" {
			out.IDs = append(out.IDs, id)
		}
	}
	return out
}

// InjectedCap bounds the injected-text echo in the busy receipt.
const InjectedCap = 1500

// EntryNotice is the parent-facing tool-entry text for one steer-released
// wait. It must tell the model the child was NOT stopped and where its
// result will re-enter, and that the user's message follows as a steer.
func EntryNotice(delegationID string) string {
	return "The user sent a new message while this subagent was still running, so it was moved to the " +
		"background as delegation " + delegationID + " - it was NOT stopped and its result will re-enter " +
		"this conversation as a completion message when it finishes. The user's message follows as a " +
		"steer; respond to it now rather than waiting for the background result."
}

// BusyReceipt is the user-facing chat receipt naming the adopted delegations
// and echoing the exact injected text (capped, fenced).
func BusyReceipt(delegationIDs []string, injectedText string) string {
	ids := strings.Join(delegationIDs, ", ")
	shown := capInjected(injectedText)
	plural := ""
	if len(delegationIDs) != 1 {
		plural = "s"
	}
	return "🌀 Your message moved the running agent" + plural + " to the background (delegation" +
		plural + " " + ids + ").\nInjected into the agent's context:\n```\n" + shown + "\n```"
}

// capInjected trims the echo to InjectedCap runes with an ellipsis marker.
func capInjected(text string) string {
	shown := strings.TrimSpace(text)
	runes := []rune(shown)
	if len(runes) <= InjectedCap {
		return shown
	}
	trimmed := strings.TrimRight(string(runes[:InjectedCap]), " \t\n\r")
	return trimmed + "..."
}

// SteeredQueueReceipt is the queue-mode plain-steer receipt (the rail
// refused the adoption race): the text landed inside the run, nothing is
// queued to replay it.
func SteeredQueueReceipt(statusDetail string) string {
	return "⏩ Steered into current run" + statusDetail + ". Your message will be injected at a safe point in this run."
}

// Summary renders a compact one-line verdict for logs and transcripts.
func Summary(outcome SteerOutcome) string {
	if !outcome.Accepted {
		return "steer refused; nothing adopted"
	}
	if len(outcome.IDs) == 0 {
		return "steer accepted; adoption refused (children keep blocking)"
	}
	return fmt.Sprintf("steer accepted; force-backgrounded %d delegation(s): %s",
		len(outcome.IDs), strings.Join(outcome.IDs, ", "))
}
