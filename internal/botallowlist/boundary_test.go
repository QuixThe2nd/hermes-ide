package botallowlist

// Boundary and failure-shape coverage beyond the shared admission table
// (testdata/admission_cases.json): numeric Go types, float64 rendering and
// precision, malformed entries inside a JSON-array STRING, and cross-shape
// canonicalization. The table only marks malformed raw containers; the
// string forms below reach parseEntries through parseString.

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

// Parse accepts every numeric Go type the bridge's JSON decoder can
// produce; each renders digit-exact except float64 (see the precision
// test below).
func TestParseNumericGoTypes(t *testing.T) {
	cases := []struct {
		name string
		raw  any
		want []string
	}{
		{"float64 integral renders without exponent", float64(111), []string{"111"}},
		{"float64 round million renders in full", float64(1e6), []string{"1000000"}},
		// A fractional number can never name a user ID, but the coercion is
		// total: it parses to an entry that simply never matches.
		{"float64 fractional renders verbatim", float64(1.5), []string{"1.5"}},
		{"int64 keeps every digit", int64(987654321098765432), []string{"987654321098765432"}},
		{"int zero is the string 0", 0, []string{"0"}},
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

// float64 cannot hold a snowflake: a decoder without UseNumber silently
// rounds the ID, and the corrupted entry would deny the real bot (and
// admit nobody else — the rounded digits match no account). This pins the
// corruption so the UseNumber contract in cmd/botallowlist stays obvious.
func TestParseFloat64SnowflakeLosesDigits(t *testing.T) {
	const snowflake = "123456789012345678901"
	got, err := Parse(float64(123456789012345678901))
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("Parse = %v, want one entry", got)
	}
	if got[0] == snowflake {
		t.Fatalf("float64 held a snowflake exactly; the UseNumber warning is stale")
	}
	if Member(got, snowflake) {
		t.Fatalf("Member(%v, %s) = true: a rounded ID must not admit the real bot", got, snowflake)
	}
}

// A JSON-array string is decoded and its entries face the same shape
// rules as a YAML list: any non-scalar entry makes the whole value
// malformed — never silently keep the valid-looking entries.
func TestParseJSONArrayStringMalformedEntries(t *testing.T) {
	cases := []struct {
		name string
		raw  string
	}{
		{"bool entry", `["111", true]`},
		{"leading bool entry", `[true, "111"]`},
		{"null entry", `["111", null]`},
		{"nested list entry", `["111", ["x"]]`},
		{"only a nested list", `[["111"]]`},
		{"map entry", `["111", {"id": "222"}]`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := Parse(tc.raw)
			if !errors.Is(err, ErrMalformed) {
				t.Fatalf("Parse(%q) error = %v, want ErrMalformed", tc.raw, err)
			}
		})
	}
}

// String-form edges where the JSON attempt and the CSV fallback diverge:
// valid JSON with trailing whitespace still takes the JSON path; trailing
// DATA makes the whole value CSV-shaped, mirroring Python's json.loads.
func TestParseJSONArrayStringBoundaries(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		want []string
	}{
		{"array of blanks narrows nothing", `[ " " , "" ]`, nil},
		{"empty array with inner space", `[ ]`, nil},
		{"trailing space keeps the JSON path", `["111"] `, []string{"111"}},
		{"trailing newline keeps the JSON path", "[\"111\"]\n", []string{"111"}},
		{"numbers with inner spaces", `[ 111 , 222 ]`, []string{"111", "222"}},
		// Trailing data after the array: the whole value is CSV text.
		{"trailing data falls back to CSV", `["111"] , 222`, []string{"222", `["111"]`}},
		// Invalid JSON inside the brackets: CSV splits on commas, so the
		// brackets stay glued to their neighbours — entries that never
		// match a real user ID, same as the Python coercion.
		{"dangling comma invalidates the JSON path", `[111,]`, []string{"[111", "]"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := Parse(tc.raw)
			if err != nil {
				t.Fatalf("Parse(%q): %v", tc.raw, err)
			}
			if strings.Join(got, ",") != strings.Join(tc.want, ",") {
				t.Fatalf("Parse(%q) = %v, want %v", tc.raw, got, tc.want)
			}
		})
	}
}

// The same logical list must canonicalize identically regardless of the
// shape it arrived in: the Python bridge caches verdicts per canonical
// list, so two spellings of one set must be interchangeable.
func TestParseCanonicalizesAcrossShapes(t *testing.T) {
	fromCSV, err := Parse("111, 222")
	if err != nil {
		t.Fatalf("Parse CSV: %v", err)
	}
	fromList, err := Parse([]any{"222", "111"})
	if err != nil {
		t.Fatalf("Parse list: %v", err)
	}
	fromJSONString, err := Parse(`["222","111"]`)
	if err != nil {
		t.Fatalf("Parse JSON string: %v", err)
	}
	if strings.Join(fromCSV, ",") != strings.Join(fromList, ",") {
		t.Fatalf("CSV %v and list %v canonicalize differently", fromCSV, fromList)
	}
	if strings.Join(fromCSV, ",") != strings.Join(fromJSONString, ",") {
		t.Fatalf("CSV %v and JSON string %v canonicalize differently", fromCSV, fromJSONString)
	}
	// Duplicates collapse before sorting, whatever the shape.
	fromDupes, err := Parse([]any{"111", "111", "222"})
	if err != nil {
		t.Fatalf("Parse dupes: %v", err)
	}
	if strings.Join(fromDupes, ",") != strings.Join(fromCSV, ",") {
		t.Fatalf("dupes %v did not collapse to %v", fromDupes, fromCSV)
	}
}

// Membership is an exact, case-sensitive string comparison against the
// parsed set — no trimming of stored IDs at Member time (Parse output is
// already clean), no numeric normalization.
func TestMemberComparesExactly(t *testing.T) {
	ids, err := Parse([]any{"ABC", "111"})
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	cases := []struct {
		authorID string
		want     bool
	}{
		{"ABC", true},
		{"abc", false}, // case-sensitive: IDs are opaque strings
		{"Abc", false},
		{"111", true},
		{" 111 ", true}, // the author ID itself is trimmed
		{"\t111\n", true},
	}
	for _, tc := range cases {
		if got := Member(ids, tc.authorID); got != tc.want {
			t.Fatalf("Member(%v, %q) = %v, want %v", ids, tc.authorID, got, tc.want)
		}
	}
}

// entryText is the one renderer every numeric entry passes through. It
// never trims (parseEntries owns trimming) and never exponents, so a
// JSON-decoded integral number always names the same ID string.
func TestEntryTextRendersWithoutNormalization(t *testing.T) {
	cases := []struct {
		name  string
		entry any
		want  string
	}{
		{"string passes through untrimmed", " 111 ", " 111 "},
		{"json.Number keeps digit-exact text", json.Number("123456789012345678901"), "123456789012345678901"},
		// A json.Number built outside a decoder is rendered as written;
		// only decoder-produced values are guaranteed numeric.
		{"json.Number passes non-numeric text through", json.Number("abc"), "abc"},
		{"float64 never renders an exponent", float64(1e21), "1000000000000000000000"},
		{"negative float64 keeps its sign", float64(-42), "-42"},
		{"negative int keeps its sign", -7, "-7"},
		{"int64 zero", int64(0), "0"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := entryText(tc.entry)
			if err != nil {
				t.Fatalf("entryText(%#v): %v", tc.entry, err)
			}
			if got != tc.want {
				t.Fatalf("entryText(%#v) = %q, want %q", tc.entry, got, tc.want)
			}
		})
	}
}

// Every non-scalar entry shape is malformed with the SAME sentinel, so
// callers can fail closed with one errors.Is check.
func TestEntryTextRejectsNonScalars(t *testing.T) {
	for _, entry := range []any{true, false, nil, []any{1}, map[string]any{"id": "1"}} {
		if _, err := entryText(entry); !errors.Is(err, ErrMalformed) {
			t.Fatalf("entryText(%#v) error = %v, want ErrMalformed", entry, err)
		}
	}
}

// Lone brackets are not JSON at all: they take the CSV path as opaque
// text, while JSON with interior/edge whitespace still decodes.
func TestParseBracketAndWhitespaceEdges(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		want []string
	}{
		{"lone open bracket is CSV text", "[", []string{"["}},
		{"lone close bracket is CSV text", "]", []string{"]"}},
		{"interior newlines stay on the JSON path", "[\n111\n]", []string{"111"}},
		{"leading tab still reaches the JSON path", "\t[\"111\"]", []string{"111"}},
		{"mixed trailing whitespace stays JSON", "[\"111\"]\t\n ", []string{"111"}},
		{"CSV dedupes after sorting", "1,2,3,2,1", []string{"1", "2", "3"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := Parse(tc.raw)
			if err != nil {
				t.Fatalf("Parse(%q): %v", tc.raw, err)
			}
			if strings.Join(got, ",") != strings.Join(tc.want, ",") {
				t.Fatalf("Parse(%q) = %v, want %v", tc.raw, got, tc.want)
			}
		})
	}
}
