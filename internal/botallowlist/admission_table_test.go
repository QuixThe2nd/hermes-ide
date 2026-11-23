package botallowlist

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// admissionCase is one row of testdata/admission_cases.json — the contract
// the Python bridge's callers rely on. The Python test derives its verdict
// from the same fields, so both sides move together or both fail.
type admissionCase struct {
	Name      string `json:"name"`
	Raw       any    `json:"raw"`
	AuthorID  string `json:"author_id"`
	Listed    bool   `json:"listed"`
	Count     int    `json:"count"`
	Malformed bool   `json:"malformed"`
}

func loadAdmissionCases(t *testing.T) []admissionCase {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("testdata", "admission_cases.json"))
	if err != nil {
		t.Fatalf("read table: %v", err)
	}
	var table struct {
		Cases []admissionCase `json:"cases"`
	}
	// UseNumber mirrors the cmd binary's request decoding: raw numbers must
	// keep every digit (user IDs exceed float64 precision).
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	if err := decoder.Decode(&table); err != nil {
		t.Fatalf("parse table: %v", err)
	}
	if len(table.Cases) == 0 {
		t.Fatal("table is empty")
	}
	return table.Cases
}

// TestAdmissionTable pins Parse and Member against every shared-table row:
// malformed rows must raise ErrMalformed, the rest must parse to the stated
// count and answer membership exactly.
func TestAdmissionTable(t *testing.T) {
	for _, tc := range loadAdmissionCases(t) {
		t.Run(tc.Name, func(t *testing.T) {
			ids, err := Parse(tc.Raw)
			if tc.Malformed {
				if !errors.Is(err, ErrMalformed) {
					t.Fatalf("Parse(%#v) error = %v, want ErrMalformed", tc.Raw, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("Parse(%#v): %v", tc.Raw, err)
			}
			if len(ids) != tc.Count {
				t.Fatalf("Parse(%#v) parsed %d IDs, want %d (%v)", tc.Raw, len(ids), tc.Count, ids)
			}
			if got := Member(ids, tc.AuthorID); got != tc.Listed {
				t.Fatalf("Member(%v, %q) = %v, want %v", ids, tc.AuthorID, got, tc.Listed)
			}
		})
	}
}

// FuzzParseNeverPanics fuzzes the string forms: any byte sequence must
// either parse or fail with ErrMalformed, and a parsed set re-parsed as a
// list must yield the same set (parse idempotence).
func FuzzParseNeverPanics(f *testing.F) {
	for _, seed := range []string{"", "[]", ",,", `["1","2"]`, "[1, x", "1,2"} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, seed string) {
		ids, err := Parse(seed)
		if err != nil {
			if !errors.Is(err, ErrMalformed) {
				t.Fatalf("Parse(%q) error %v is not ErrMalformed", seed, err)
			}
			return
		}
		asList := make([]any, 0, len(ids))
		for _, id := range ids {
			asList = append(asList, id)
		}
		again, err := Parse(asList)
		if err != nil {
			t.Fatalf("re-parse of %v failed: %v", ids, err)
		}
		if strings.Join(ids, ",") != strings.Join(again, ",") {
			t.Fatalf("Parse(%q) = %v, re-parse = %v", seed, ids, again)
		}
	})
}
