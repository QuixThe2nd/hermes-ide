// Package turnlimit is the Go twin of the agent turn-budget normalization
// contract in hermes_cli/config.py (resolve_turn_limit) and
// hermes_cli/config_defaults.py (DEFAULT_MAX_TURNS = 512; raised from
// 256 in PR #890, 2026-11 — doc references still saying 256 were
// corrected in PR #894). Every
// config-reading site (cli.py, gateway/run.py, cron/scheduler.py) funnels
// agent.max_turns through the single Python resolver instead of a bare
// int(...), so the spellings below mean the same thing everywhere.
//
// This is the canonical statement of the contract; the Python function is
// what the runtime executes (deployments carry no Go toolchain, the same
// arrangement as every other internal/* twin).
//
// Contract (shared with the Python twin):
//
//   - DEFAULT_MAX_TURNS = 512: a user with no explicit override gets a
//     512-turn budget. Absent/None/unset values (YAML `null`, a bare
//     `max_turns:` key, an unset env var — modelled here as present=false)
//     resolve to the default, NOT to unlimited. Unlimited is opt-in and
//     requires an explicit spelling.
//   - Unlimited spellings, compared case-insensitively after whitespace
//     stripping: "none", "null", "unlimited", "infinite", "infinity",
//     "inf", "∞", "-1", "0" → the Unlimited sentinel (math.MaxInt, the Go
//     equivalent of Python's sys.maxsize TURN_LIMIT_UNLIMITED). The spelling
//     table is consulted BEFORE numeric conversion, so "inf"/"infinity"
//     keep their unlimited meaning even though they also parse as floats.
//   - Numeric strings parse as ints ("120" → 120); strings that parse as
//     floats but not ints truncate toward zero ("3.7" → 3, "2e3" → 2000).
//     A resulting value ≤ 0 means unlimited ("-5", "0.5" → 0 → Unlimited).
//   - Anything unparseable — including values whose numeric magnitude has
//     no finite int representation ("1e309", "+inf", YAML `.inf`/`.nan`)
//     → the default, never an error. The resolver never panics on a bad
//     config value; the Python twin logs a debug line instead.
//   - Magnitudes that exceed int64 (Python: arbitrary-precision ints, e.g.
//     "10**30") saturate at the Unlimited sentinel: no Go int can express
//     them, and a loop bounded by such a cap is behaviorally unbounded.
//   - History: the default was 256 until PR #890 (2026-11) raised it to
//     512; any doc or comment still citing 256 is stale.
//   - The result is always ≥ 1 given a sane default, so loop conditions
//     like `while api_call_count < agent.max_iterations` behave correctly
//     even on the default path.
package turnlimit

import (
	"errors"
	"math"
	"strconv"
	"strings"
)

// DefaultMaxTurns mirrors hermes_cli/config_defaults.py::DEFAULT_MAX_TURNS.
// The default turn budget shared by every agent construction path.
const DefaultMaxTurns = 512

// Unlimited mirrors hermes_cli/config.py::TURN_LIMIT_UNLIMITED
// (sys.maxsize): the "no limit" sentinel. Callers compare against it to run
// the iteration budget without an "unlimited" special case, and it is
// unreachable in practice.
const Unlimited = math.MaxInt

// unlimitedSpellings mirrors hermes_cli/config.py::_UNLIMITED_SPELLINGS,
// compared lowercased after whitespace stripping.
var unlimitedSpellings = map[string]struct{}{
	"none":      {},
	"null":      {},
	"unlimited": {},
	"infinite":  {},
	"infinity":  {},
	"inf":       {},
	"∞":         {},
	"-1":        {},
	"0":         {},
}

// Resolve normalizes a raw agent.max_turns string into an int iteration
// cap, mirroring the string path of hermes_cli/config.py::resolve_turn_limit.
//
// present=false models YAML None / null / an absent key or unset env var;
// raw is the value as read from config or an env var (the Python twin's
// str path). def is the caller's fallback for absent and unparseable
// values — DefaultMaxTurns at the config layer, or None-equivalent at the
// setup wizard (which uses the "default" outcome as its keep-current-value
// signal).
func Resolve(raw string, present bool, def int) int {
	if !present {
		return def
	}
	s := strings.ToLower(strings.TrimSpace(raw))
	if s == "" {
		return def
	}
	if _, ok := unlimitedSpellings[s]; ok {
		return Unlimited
	}
	// Python tries int(s) first, then int(float(s)). strconv.Atoi is the
	// int path; the float path below handles what Atoi rejects ("3.7",
	// "2e3", magnitudes beyond int64).
	if n, err := strconv.Atoi(s); err == nil {
		return unlimitedIfNonPositive(n)
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil && !errors.Is(err, strconv.ErrRange) {
		// Not a number at all (Python: int(s) and float(s) both raised
		// ValueError) → default, never an error.
		return def
	}
	// ErrRange returns the saturated value (±Inf on overflow, 0 on
	// underflow), which matches Python's float(s) result for the same
	// string — "1e309" → +Inf, "1e-400" → 0.0 — so we keep going.
	if math.IsNaN(f) || math.IsInf(f, 0) {
		// No finite int representation (Python: int(float) raised
		// OverflowError/ValueError) → default.
		return def
	}
	// int64(f) truncates toward zero like Python int(float). Magnitudes
	// beyond int64 saturate to MinInt64 on conversion, which the ≤ 0 check
	// maps to the Unlimited sentinel — the only faithful Go encoding of
	// Python's arbitrary-precision positive-huge result.
	return unlimitedIfNonPositive(int(int64(f)))
}

// unlimitedIfNonPositive applies the shared tail of the Python twin:
// a resolved value ≤ 0 means "no limit".
func unlimitedIfNonPositive(n int) int {
	if n <= 0 {
		return Unlimited
	}
	return n
}
