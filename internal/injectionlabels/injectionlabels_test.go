package injectionlabels

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

// The shared contract table (testdata/labels.json) is asserted from BOTH
// sides: these tests and the Python contract test
// (tests/gateway/test_context_injection_labels.py) read the same bytes, so
// the Go twin and gateway/context_injection_labels.py cannot drift apart.

// sharedTable mirrors testdata/labels.json.
type sharedTable struct {
	GenericLabel string            `json:"generic_label"`
	KindLabels   map[string]string `json:"kind_labels"`
	Memory       struct {
		Plain string `json:"plain"`
		One   string `json:"one"`
		Many  string `json:"many"`
	} `json:"memory"`
	Cases []sharedCase `json:"cases"`
}

// sharedCase is one kinds→heading-lines expectation. An absent kinds array
// is the legacy payload shape; an absent recalled_count means no usable
// count (plain memory heading).
type sharedCase struct {
	Name          string   `json:"name"`
	Kinds         []string `json:"kinds"`
	MemorySourced bool     `json:"memory_sourced"`
	RecalledCount int      `json:"recalled_count"`
	ExpectLines   []string `json:"expect_lines"`
}

func loadTable(t *testing.T) sharedTable {
	t.Helper()
	raw, err := os.ReadFile("testdata/labels.json")
	if err != nil {
		t.Fatalf("read shared table: %v", err)
	}
	var table sharedTable
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parse shared table: %v", err)
	}
	return table
}

// TestLabelsMatchSharedTable pins the label table itself: the generic label,
// every per-kind label, and the three memory heading forms must equal the
// shared table the Python contract test also reads.
func TestLabelsMatchSharedTable(t *testing.T) {
	table := loadTable(t)
	if GenericLabel != table.GenericLabel {
		t.Errorf("GenericLabel = %q, want %q", GenericLabel, table.GenericLabel)
	}
	if !reflect.DeepEqual(KindLabels, table.KindLabels) {
		t.Errorf("KindLabels = %q, want %q", KindLabels, table.KindLabels)
	}
	if got := MemoryHeading(0); got != table.Memory.Plain {
		t.Errorf("MemoryHeading(0) = %q, want %q", got, table.Memory.Plain)
	}
	if got := MemoryHeading(1); got != table.Memory.One {
		t.Errorf("MemoryHeading(1) = %q, want %q", got, table.Memory.One)
	}
	if got := MemoryHeading(3); got != table.Memory.Many {
		t.Errorf("MemoryHeading(3) = %q, want %q", got, table.Memory.Many)
	}
}

// TestHeadingLinesMatchSharedTable drives HeadingLines through every case in
// the shared table: legacy payloads, unknown/blank kinds, the memory
// special case with and without counts, payload ordering, and output-line
// deduplication.
func TestHeadingLinesMatchSharedTable(t *testing.T) {
	for _, tc := range loadTable(t).Cases {
		got := HeadingLines(tc.Kinds, tc.MemorySourced, tc.RecalledCount)
		if !reflect.DeepEqual(got, tc.ExpectLines) {
			t.Errorf("%s: HeadingLines(%q, memorySourced=%v, recalledCount=%d) = %q, want %q",
				tc.Name, tc.Kinds, tc.MemorySourced, tc.RecalledCount, got, tc.ExpectLines)
		}
	}
}

// TestEveryKnownKindAppearsInSharedTable guards the fixture against drift:
// a kind added to KindLabels without a table entry (or vice versa) fails
// here and in the Python twin's same-shaped check.
func TestEveryKnownKindAppearsInSharedTable(t *testing.T) {
	table := loadTable(t)
	if len(KindLabels) != len(table.KindLabels) {
		t.Errorf("KindLabels has %d entries, shared table has %d", len(KindLabels), len(table.KindLabels))
	}
	for kind := range KindLabels {
		if _, ok := table.KindLabels[kind]; !ok {
			t.Errorf("kind %q missing from shared table", kind)
		}
	}
}

// TestNegativeCountRendersPlain pins the guard behind the counted form: only
// a positive discrete count is ever spoken.
func TestNegativeCountRendersPlain(t *testing.T) {
	if got := MemoryHeading(-2); got != "🧠 Injecting memory" {
		t.Errorf("MemoryHeading(-2) = %q, want the plain heading", got)
	}
}
