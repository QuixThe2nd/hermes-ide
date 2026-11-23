// Package responsegateidentity is the Go contract twin of the Discord
// response gate's judge-facing bot identity in gateway/config.py and
// plugins/platforms/discord/adapter.py.
//
// A configured response_gate.bot_name is trimmed once and remains fixed. Nil
// represents an unset or null override: identity then comes from the Discord
// user's display name, username, and name, in that order, with "assistant" as
// the final fallback. Every candidate is trimmed, and a blank configured
// override is rejected instead of silently enabling automatic identity.
//
// Discord exposes client.user only at READY. Gate therefore distinguishes
// Connect from Ready: the first Connect uses the default, a reconnect uses the
// previous READY user until fresh information arrives, and every automatic
// READY selects afresh. Configured gates ignore connected-user changes. Each
// Snapshot carries the address examples built from the same name so question
// text and judge state cannot diverge.
//
// Production Python never calls this package; deployments need no Go
// toolchain. Both sides' tests consume testdata/identity_cases.json, following
// the internal/hubframe shared-contract pattern.
package responsegateidentity

import (
	"errors"
	"strings"
)

// DefaultName is the safe judge-facing identity when Discord has not supplied
// a usable connected-user field. It matches the Python adapter's fallback.
const DefaultName = "assistant"

// ErrInvalidBotName matches the Python configuration validation failure. A nil
// override is valid and selects automatic identity; a non-nil override must
// contain at least one non-whitespace character.
var ErrInvalidBotName = errors.New(
	"response_gate.bot_name must be a non-empty string or null",
)

// ConnectedUser contains the Discord identity fields consulted by the
// automatic fallback chain. The JSON tags let the shared contract table use
// the same field names as the Python adapter.
type ConnectedUser struct {
	DisplayName string `json:"display_name"`
	Username    string `json:"username"`
	Name        string `json:"name"`
}

// AddressExamples are the two example sets under addresses_bot.criteria in
// the Decisions question. Fixed-size arrays make the three-example contract
// explicit and keep snapshots free of mutable slice aliases.
type AddressExamples struct {
	True  [3]string `json:"true"`
	False [3]string `json:"false"`
}

// Snapshot is the identity used to build both the gate questions and the
// request's state.bot.name. Examples always correspond to Name.
type Snapshot struct {
	Name     string          `json:"name"`
	Examples AddressExamples `json:"examples"`
}

// Gate tracks identity across Discord connection lifecycle events. Its zero
// value is intentionally unusable: callers must construct it with NewGate so
// an empty override cannot be mistaken for automatic identity. Like the
// Discord adapter's event callbacks, lifecycle methods are serial, not
// concurrent.
type Gate struct {
	automatic bool
	lastReady ConnectedUser
	hasReady  bool
	current   Snapshot
}

// NewGate validates an optional configured override and returns the identity
// available before Discord's first READY. Nil selects automatic identity. A
// non-nil override is trimmed, retained verbatim otherwise, and never changes
// in response to ConnectedUser updates.
func NewGate(override *string) (Gate, error) {
	name := DefaultName
	automatic := override == nil
	if override != nil {
		name = strings.TrimSpace(*override)
		if name == "" {
			return Gate{}, ErrInvalidBotName
		}
	}
	return Gate{
		automatic: automatic,
		current:   snapshotFor(name),
	}, nil
}

// AutomaticName selects the first non-blank connected-user field in Discord
// adapter order: display name, username, name, then DefaultName. Whitespace is
// trimmed from the selected value; blank candidates do not stop the fallback.
func AutomaticName(user ConnectedUser) string {
	candidates := [3]string{user.DisplayName, user.Username, user.Name}
	for _, candidate := range candidates {
		if name := strings.TrimSpace(candidate); name != "" {
			return name
		}
	}
	return DefaultName
}

// AddressExamplesFor builds the six dynamic addresses_bot examples. Callers
// pass a resolved Snapshot.Name (or another intentionally supplied non-empty
// identity); configured-name validation belongs to NewGate, as in Python.
func AddressExamplesFor(name string) AddressExamples {
	return AddressExamples{
		True: [3]string{
			"hey " + name + " whats up",
			name + " can you check this",
			"what do you think " + name + "?",
		},
		False: [3]string{
			"i saw " + name + " in the other channel",
			"the example greeting is 'hi " + name + " whats up'",
			name + " is down again lol",
		},
	}
}

// snapshotFor keeps a name and all of its dynamic examples atomic from the
// caller's perspective.
func snapshotFor(name string) Snapshot {
	return Snapshot{Name: name, Examples: AddressExamplesFor(name)}
}

// Snapshot returns the current name and its matching address examples. The
// returned value contains no pointers or slices into Gate state, so a later
// READY cannot mutate an earlier snapshot retained by a caller.
func (g Gate) Snapshot() Snapshot {
	return g.current
}

// Connect initializes automatic identity before Discord dispatch opens. On
// the first connection no READY user exists, so this yields DefaultName. On
// reconnect it yields the previous READY user, matching the adapter's call to
// _response_gate_init while the previous client is still installed.
// Configured identity remains unchanged.
func (g *Gate) Connect() Snapshot {
	if !g.automatic {
		return g.current
	}
	user := ConnectedUser{}
	if g.hasReady {
		user = g.lastReady
	}
	return g.selectUser(user)
}

// Ready refreshes automatic identity from the user exposed by Discord READY
// and records that user for the next reconnect's pre-READY snapshot. A
// configured gate ignores the user and returns its fixed snapshot.
func (g *Gate) Ready(user ConnectedUser) Snapshot {
	if !g.automatic {
		return g.current
	}
	g.lastReady = user
	g.hasReady = true
	return g.selectUser(user)
}

// selectUser updates the snapshot only when the selected name changes. READY
// still records the complete latest user before calling this helper, so an
// unchanged display name does not discard changed lower-priority fields.
func (g *Gate) selectUser(user ConnectedUser) Snapshot {
	name := AutomaticName(user)
	if name != g.current.Name {
		g.current = snapshotFor(name)
	}
	return g.current
}
