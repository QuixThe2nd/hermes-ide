package botallowlist

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

// jsonNumber decodes text as a single JSON value with UseNumber, the same
// decoding the cmd binary applies to a request — list entries that are
// JSON numbers must keep every digit (user IDs exceed float64 precision).
func jsonNumber(t *testing.T, text string) any {
	t.Helper()
	decoder := json.NewDecoder(strings.NewReader(text))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		t.Fatalf("decode %s: %v", text, err)
	}
	return value
}

func TestParseShapes(t *testing.T) {
	cases := []struct {
		name string
		raw  any
		want []string
	}{
		{"nil narrows nothing", nil, nil},
		{"empty string", "", nil},
		{"whitespace string", "   ", nil},
		{"empty list", []any{}, nil},
		{"empty JSON array string", "[]", nil},
		{"CSV commas only", ",,", nil},
		{"list of strings", []any{"222", "111"}, []string{"111", "222"}},
		{"list trims and drops blanks", []any{" 111 ", ""}, []string{"111"}},
		{"CSV string", "111, 222 ,333", []string{"111", "222", "333"}},
		{"CSV with empty tail", "111,", []string{"111"}},
		{"JSON array string", `["111","222"]`, []string{"111", "222"}},
		{"JSON array string with whitespace", `[ "111" , "222" ]`, []string{"111", "222"}},
		{"invalid JSON array string falls back to CSV", "[111, abc", []string{"[111", "abc"}},
		// json.loads rejects trailing data; so do we — the value is CSV.
		{"JSON array with trailing garbage is CSV", `["111"] extra`, []string{`["111"] extra`}},
		{"list of json.Number", []any{jsonNumber(t, "111")}, []string{"111"}},
		{"list of ints", []any{111, 222}, []string{"111", "222"}},
		{"top-level json.Number", jsonNumber(t, "111"), []string{"111"}},
		{"top-level int", 111, []string{"111"}},
		// A JSON object given as a string is CSV-shaped text whose entries
		// simply never match a real user ID.
		{"JSON object string is CSV-shaped", `{"id":"111"}, x`, []string{"x", `{"id":"111"}`}},
		{"dedupes", []any{"111", "111"}, []string{"111"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := Parse(tc.raw)
			if err != nil {
				t.Fatalf("Parse(%#v): %v", tc.raw, err)
			}
			if strings.Join(got, ",") != strings.Join(tc.want, ",") {
				t.Fatalf("Parse(%#v) = %v, want %v", tc.raw, got, tc.want)
			}
		})
	}
}

func TestParseMalformed(t *testing.T) {
	cases := []struct {
		name string
		raw  any
	}{
		{"map", map[string]any{"id": "111"}},
		{"bool", true},
		{"list with nested list", []any{[]any{"111"}}},
		{"list with map", []any{map[string]any{"id": "111"}}},
		{"list with bool", []any{true}},
		{"list with null", []any{nil}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := Parse(tc.raw)
			if err == nil {
				t.Fatalf("Parse(%#v) succeeded, want ErrMalformed", tc.raw)
			}
			if !errors.Is(err, ErrMalformed) {
				t.Fatalf("Parse(%#v) error = %v, want ErrMalformed", tc.raw, err)
			}
		})
	}
}

// User IDs are snowflakes with more digits than float64 holds exactly; a
// precision-losing decode would silently admit or deny the wrong bot.
func TestParseSnowflakePrecision(t *testing.T) {
	const snowflake = "123456789012345678901"
	raw := jsonNumber(t, `[`+snowflake+`]`)
	ids, err := Parse(raw)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if len(ids) != 1 || ids[0] != snowflake {
		t.Fatalf("Parse = %v, want [%s]", ids, snowflake)
	}
	if !Member(ids, snowflake) {
		t.Fatalf("Member(%v, %s) = false", ids, snowflake)
	}
}

func TestMemberExactness(t *testing.T) {
	ids := []string{"111", "222"}
	cases := []struct {
		authorID string
		want     bool
	}{
		{"111", true},
		{"222", true},
		{" 111 ", true}, // author IDs arrive clean; trim is belt-and-braces
		{"", false},
		{"   ", false},
		{"1111", false}, // no prefix matching
		{"11", false},   // no substring matching
		{"0111", false}, // no numeric normalization
		{"333", false},
	}
	for _, tc := range cases {
		if got := Member(ids, tc.authorID); got != tc.want {
			t.Fatalf("Member(%v, %q) = %v, want %v", ids, tc.authorID, got, tc.want)
		}
	}
	if Member(nil, "111") {
		t.Fatal("Member(nil, ...) = true: an empty set admits nothing")
	}
}
