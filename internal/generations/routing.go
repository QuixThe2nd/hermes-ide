package generations

// Session routing across overlapping gateway generations — the decision
// core the transport hub's session router executes for every inbound
// event while more than one generation is attached.
//
// The invariants (mirrored by the hub router, pinned against this package
// by tests/gateway/test_generations_contract.py through cmd/generations):
//
//   - each session is owned by exactly one generation; ownership is
//     claimed on first DELIVERY, never on attach;
//   - attaching a newer generation moves only UNOWNED sessions: a busy
//     session stays on the generation already running its turn, so a
//     draining predecessor keeps its in-flight work to the very end;
//   - the newest ATTACHED generation (attach order, not spawn order — the
//     hub cannot see PIDs that never attached) takes every unowned event;
//   - an explicit target PID (overlap handoff, returning an unowned event
//     to the generation that admitted it) wins over both rules;
//   - an owner that detached is no longer eligible: its session's next
//     event moves to the newest attached generation;
//   - when no eligible generation exists the event is HELD (retained for
//     replay on the next attach) — never dropped, never duplicated.

// AttachedGeneration is one generation currently attached to the hub.
type AttachedGeneration struct {
	// ID is the hub generation id (routing key).
	ID string `json:"id"`
	// PID is the gateway process behind the generation (0 = unknown);
	// target-PID routing matches on it.
	PID int `json:"pid"`
}

// RoutingState is the hub router's state snapshot the decision runs on.
//
// Attached MUST be in attach order, oldest first: a generation that
// re-attaches moves to the end, so the LAST entry is the newest attached
// generation — the one that takes new work.
type RoutingState struct {
	Attached []AttachedGeneration `json:"attached"`
	// Owners maps session key → owning generation id.
	Owners map[string]string `json:"owners"`
}

// Route selects the generation that must serve one inbound event.
//
// excluded lists generation ids that already refused or received this
// event (overlap handoff trail); targetPID, when > 0, pins the event to
// the generation whose gateway process owns it. The empty answer with
// ok=false means HOLD: no eligible generation is attached.
func Route(st RoutingState, session string, excluded []string, targetPID int) (string, bool) {
	skip := exclusionSet(excluded)
	if targetPID > 0 {
		// First match in attach order; the state is tiny and the
		// match set is at most one gateway per PID.
		for _, g := range st.Attached {
			if g.PID == targetPID && g.ID != "" && !skip[g.ID] {
				return g.ID, true
			}
		}
		return "", false
	}
	if owner, owned := st.Owners[session]; owned && owner != "" && !skip[owner] {
		for _, g := range st.Attached {
			if g.ID == owner {
				return owner, true
			}
		}
	}
	// Newest attached wins unowned work: last entry in attach order.
	for i := len(st.Attached) - 1; i >= 0; i-- {
		if id := st.Attached[i].ID; id != "" && !skip[id] {
			return id, true
		}
	}
	return "", false
}

// HandoffEligible answers whether the hub can return an event to the
// generation named by targetPID without revisiting any generation in
// visited (the handoff trail the event already carries).
func HandoffEligible(st RoutingState, visited []string, targetPID int) bool {
	if targetPID <= 0 {
		return false
	}
	skip := exclusionSet(visited)
	for _, g := range st.Attached {
		if g.PID == targetPID && g.ID != "" && !skip[g.ID] {
			return true
		}
	}
	return false
}

// RegisterDecision answers whether generation me may claim a
// locally-started turn on session. False means the session already has a
// live owner that is not me — the existing owner keeps the session.
func RegisterDecision(st RoutingState, session, me string) bool {
	owner, owned := st.Owners[session]
	if !owned || owner == "" || owner == me {
		return true
	}
	for _, g := range st.Attached {
		if g.ID == owner {
			return false
		}
	}
	return true
}

func exclusionSet(ids []string) map[string]bool {
	if len(ids) == 0 {
		return map[string]bool{}
	}
	set := make(map[string]bool, len(ids))
	for _, id := range ids {
		if id != "" {
			set[id] = true
		}
	}
	return set
}
