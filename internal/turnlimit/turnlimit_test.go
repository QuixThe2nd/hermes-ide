package turnlimit

import (
	"math"
	"testing"
)

// Table mirrors tests/hermes_cli/test_resolve_turn_limit.py: same inputs
// must produce the same outcomes as the Python twin.
var resolveCases = []struct {
	name    string
	raw     string
	present bool
	def     int
	want    int
}{
	// Default path: absent/None/unset and empty/whitespace-only strings
	// resolve to the default, NOT to unlimited — unlimited is opt-in via
	// an explicit spelling.
	{"absent_uses_default", "", false, DefaultMaxTurns, DefaultMaxTurns},
	{"absent_custom_default", "", false, 500, 500},
	{"empty_string", "", true, DefaultMaxTurns, DefaultMaxTurns},
	{"whitespace_only", "   ", true, DefaultMaxTurns, DefaultMaxTurns},

	// Every unlimited spelling, as documented.
	{"none", "none", true, DefaultMaxTurns, Unlimited},
	{"null", "null", true, DefaultMaxTurns, Unlimited},
	{"unlimited", "unlimited", true, DefaultMaxTurns, Unlimited},
	{"infinite", "infinite", true, DefaultMaxTurns, Unlimited},
	{"infinity", "infinity", true, DefaultMaxTurns, Unlimited},
	{"inf", "inf", true, DefaultMaxTurns, Unlimited},
	{"infinity_symbol", "∞", true, DefaultMaxTurns, Unlimited},
	{"minus_one", "-1", true, DefaultMaxTurns, Unlimited},
	{"zero_string", "0", true, DefaultMaxTurns, Unlimited},

	// Case-insensitive and whitespace-tolerant spellings.
	{"None_capitalized", "None", true, DefaultMaxTurns, Unlimited},
	{"NONE_upper", "NONE", true, DefaultMaxTurns, Unlimited},
	{"nOnE_mixed", "nOnE", true, DefaultMaxTurns, Unlimited},
	{"UNLIMITED_upper", "UNLIMITED", true, DefaultMaxTurns, Unlimited},
	{"INF_upper", "INF", true, DefaultMaxTurns, Unlimited},
	{"none_padded", " none ", true, DefaultMaxTurns, Unlimited},
	{"unlimited_padded", "  unlimited  ", true, DefaultMaxTurns, Unlimited},
	{"infinite_tabbed", "\tinfinite\t", true, DefaultMaxTurns, Unlimited},

	// Positive numbers pass through exactly.
	{"int_120", "120", true, DefaultMaxTurns, 120},
	{"int_1", "1", true, DefaultMaxTurns, 1},
	{"int_3", "3", true, DefaultMaxTurns, 3},
	{"explicit_plus", "+42", true, DefaultMaxTurns, 42},
	{"large_finite", "1000000000000000000", true, DefaultMaxTurns, 1000000000000000000},

	// Floats truncate toward zero.
	{"float_truncates", "3.7", true, DefaultMaxTurns, 3},
	{"float_integral", "3.0", true, DefaultMaxTurns, 3},
	{"float_100_9", "100.9", true, DefaultMaxTurns, 100},
	{"scientific_notation", "2e3", true, DefaultMaxTurns, 2000},

	// Zero and negative values mean unlimited.
	{"negative_int", "-5", true, DefaultMaxTurns, Unlimited},
	{"negative_42", "-42", true, DefaultMaxTurns, Unlimited},
	{"negative_float", "-3.7", true, DefaultMaxTurns, Unlimited},
	{"zero_float", "0.0", true, DefaultMaxTurns, Unlimited},
	{"fraction_truncates_to_zero", "0.5", true, DefaultMaxTurns, Unlimited},

	// Unparseable strings → default, never an error.
	{"garbage", "garbage", true, DefaultMaxTurns, DefaultMaxTurns},
	{"not_a_number", "not_a_number", true, DefaultMaxTurns, DefaultMaxTurns},
	{"garbage_custom_default", "garbage", true, 500, 500},

	// Overflow-like: magnitude with no finite int representation → default.
	{"overflow_1e309", "1e309", true, DefaultMaxTurns, DefaultMaxTurns},
	{"overflow_negative", "-1e309", true, DefaultMaxTurns, DefaultMaxTurns},
	{"overflow_1e400", "1e400", true, DefaultMaxTurns, DefaultMaxTurns},
	{"overflow_1e1000000", "1e1000000", true, DefaultMaxTurns, DefaultMaxTurns},
	{"plus_inf_not_spelling", "+inf", true, DefaultMaxTurns, DefaultMaxTurns},
	{"minus_inf_not_spelling", "-inf", true, DefaultMaxTurns, DefaultMaxTurns},
	{"yaml_dot_inf", ".inf", true, DefaultMaxTurns, DefaultMaxTurns},
	{"nan", "nan", true, DefaultMaxTurns, DefaultMaxTurns},
	{"overflow_custom_default", "1e309", true, 500, 500},

	// Underflow to 0.0 → ≤ 0 → unlimited (Python float("1e-400") = 0.0 →
	// int = 0 → TURN_LIMIT_UNLIMITED); ErrRange must not be treated as
	// unparseable.
	{"underflow_is_unlimited", "1e-400", true, DefaultMaxTurns, Unlimited},
}

func TestResolve(t *testing.T) {
	for _, tc := range resolveCases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Resolve(tc.raw, tc.present, tc.def); got != tc.want {
				t.Errorf("Resolve(%q, %v, %d) = %d, want %d",
					tc.raw, tc.present, tc.def, got, tc.want)
			}
		})
	}
}

func TestDefaultMaxTurnsIs512(t *testing.T) {
	// Pin the product decision: a user with no explicit override gets 512
	// turns. Must track hermes_cli/config_defaults.py::DEFAULT_MAX_TURNS.
	if DefaultMaxTurns != 512 {
		t.Errorf("DefaultMaxTurns = %d, want 512", DefaultMaxTurns)
	}
}

func TestUnlimitedSentinel(t *testing.T) {
	// The sentinel is the Go equivalent of Python's sys.maxsize and must
	// dominate any realistic turn count.
	if Unlimited != math.MaxInt {
		t.Errorf("Unlimited = %d, want math.MaxInt (%d)", Unlimited, math.MaxInt)
	}
	if Unlimited <= 1_000_000_000 {
		t.Errorf("Unlimited = %d, want > 1e9", Unlimited)
	}
}

func TestResolveResultAlwaysAtLeastOne(t *testing.T) {
	// Given a sane default, every spelling class resolves to >= 1 so loop
	// conditions like `api_call_count < max_iterations` behave correctly.
	for _, tc := range resolveCases {
		if tc.def < 1 {
			continue
		}
		if got := Resolve(tc.raw, tc.present, tc.def); got < 1 {
			t.Errorf("Resolve(%q, %v, %d) = %d, want >= 1",
				tc.raw, tc.present, tc.def, got)
		}
	}
}

func TestSpellingTableConsultedBeforeNumeric(t *testing.T) {
	// "inf"/"infinity" parse as floats in both languages; the spelling
	// table must win so they keep their documented unlimited meaning
	// instead of hitting the overflow-to-default path like "+inf" does.
	for _, spelling := range []string{"inf", "INF", "infinity", "infinite", "unlimited", "none"} {
		if got := Resolve(spelling, true, DefaultMaxTurns); got != Unlimited {
			t.Errorf("Resolve(%q) = %d, want Unlimited", spelling, got)
		}
	}
	if got := Resolve("+inf", true, DefaultMaxTurns); got != DefaultMaxTurns {
		t.Errorf("Resolve(%q) = %d, want default (not a spelling)", "+inf", got)
	}
}
