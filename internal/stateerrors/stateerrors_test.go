package stateerrors

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// contractCase is one row of the shared table; cmd/stateerrors parses the
// same shape, so keep the two struct definitions field-identical.
type contractCase struct {
	Name  string `json:"name"`
	Error struct {
		Kind   string `json:"kind"`
		Text   string `json:"text"`
		Code   *int   `json:"sqlite_errorcode,omitempty"`
		ENOSPC bool   `json:"enospc,omitempty"`
	} `json:"error"`
	Want struct {
		Classify        string `json:"classify"`
		Transient       bool   `json:"transient"`
		MalformedDB     bool   `json:"malformed_db"`
		MalformedSchema bool   `json:"malformed_schema"`
		DiskFull        bool   `json:"disk_full"`
		FTSScoped       bool   `json:"fts_scoped"`
	} `json:"want"`
}

type contractTable struct {
	Cases []contractCase `json:"cases"`
}

func loadContractTable(t *testing.T) contractTable {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", "classify_cases.json"))
	if err != nil {
		t.Fatalf("read shared table: %v", err)
	}
	var table contractTable
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parse shared table: %v", err)
	}
	if len(table.Cases) == 0 {
		t.Fatal("shared table holds no cases")
	}
	return table
}

func (c contractCase) view(t *testing.T) ErrorView {
	t.Helper()
	return ErrorView{
		Kind:   Kind(c.Error.Kind),
		Text:   c.Error.Text,
		Code:   c.Error.Code,
		ENOSPC: c.Error.ENOSPC,
	}
}

// TestSharedContractTable replays every shared-table case through the Go
// package. The Python contract test
// (tests/hermes_state/test_error_classification_go_contract.py) replays the
// same cases through hermes_state_errors.py with real exceptions, so
// Python == table == Go.
func TestSharedContractTable(t *testing.T) {
	for _, c := range loadContractTable(t).Cases {
		t.Run(c.Name, func(t *testing.T) {
			v := c.view(t)
			if got := Classify(v); got != Cause(c.Want.Classify) {
				t.Errorf("Classify(%q) = %q, want %q", c.Error.Text, got, c.Want.Classify)
			}
			if got := IsTransient(v); got != c.Want.Transient {
				t.Errorf("IsTransient(%q) = %v, want %v", c.Error.Text, got, c.Want.Transient)
			}
			if got := IsMalformedDB(v); got != c.Want.MalformedDB {
				t.Errorf("IsMalformedDB(%q) = %v, want %v", c.Error.Text, got, c.Want.MalformedDB)
			}
			if got := IsMalformedSchema(v); got != c.Want.MalformedSchema {
				t.Errorf("IsMalformedSchema(%q) = %v, want %v", c.Error.Text, got, c.Want.MalformedSchema)
			}
			if got := IsDiskFull(v); got != c.Want.DiskFull {
				t.Errorf("IsDiskFull(%q) = %v, want %v", c.Error.Text, got, c.Want.DiskFull)
			}
			if got := IsFTSScopedCorruption(v); got != c.Want.FTSScoped {
				t.Errorf("IsFTSScopedCorruption(%q) = %v, want %v", c.Error.Text, got, c.Want.FTSScoped)
			}
		})
	}
}

// TestSharedTableProducesEveryCause pins the relationship between the table
// and the exported bucket set: every Cause a consumer can enumerate must be
// reachable through at least one shared case, and no case may name a bucket
// outside Causes.
func TestSharedTableProducesEveryCause(t *testing.T) {
	produced := map[Cause]bool{}
	for _, c := range loadContractTable(t).Cases {
		produced[Cause(c.Want.Classify)] = true
	}
	for _, cause := range Causes {
		if !produced[cause] {
			t.Errorf("shared table never produces cause %q", cause)
		}
	}
	known := map[Cause]bool{}
	for _, cause := range Causes {
		known[cause] = true
	}
	for cause := range produced {
		if !known[cause] {
			t.Errorf("shared table produces unlisted cause %q", cause)
		}
	}
}

// TestClassifyPrecedence pins the ordering rules directly, independent of
// the shared table: a regression here means the precedence contract broke,
// not that a case file drifted.
func TestClassifyPrecedence(t *testing.T) {
	code := func(n int) *int { return &n }
	tests := []struct {
		name string
		view ErrorView
		want Cause
	}{
		{
			// The typed ladder runs before every text check: a typed lease
			// refusal whose message happens to say "locked" is fencing, not
			// contention.
			name: "typed_kind_beats_prose",
			view: ErrorView{Kind: KindTurnLease, Text: "database is locked"},
			want: CauseTurnLease,
		},
		{
			// A typed error outranks even an FTS-scoped result code riding
			// on it.
			name: "typed_kind_beats_fts_code",
			view: ErrorView{Kind: KindCorrupt, Text: "x", Code: code(sqliteCorruptVtab)},
			want: CauseCorrupt,
		},
		{
			// FTS provenance beats the corruption phrase table: index damage
			// is never whole-file corruption.
			name: "fts_scoping_beats_corruption_phrase",
			view: ErrorView{Kind: KindDatabaseError, Text: "database disk image is malformed", Code: code(sqliteCorruptVtab)},
			want: CauseFTSIndex,
		},
		{
			// #77386: the corruption phrase beats the disk bucket because
			// "database disk image is malformed" contains "disk".
			name: "corruption_phrase_beats_disk_bucket",
			view: ErrorView{Kind: KindString, Text: "database disk image is malformed"},
			want: CauseCorrupt,
		},
		{
			// Phrase rows are ordered: turn lease before locked.
			name: "turn_lease_phrase_beats_locked",
			view: ErrorView{Kind: KindString, Text: "turn lease lost while database is locked"},
			want: CauseTurnLease,
		},
		{
			// Phrase rows are ordered: closed-by-compression before busy.
			name: "compression_closed_phrase_beats_busy",
			view: ErrorView{Kind: KindString, Text: "closed by compression and still busy"},
			want: CauseCompressionClosed,
		},
		{
			// Phrase rows are ordered: the retired-sidecar split before the
			// main-file replacement phrase and before locked.
			name: "deleted_wal_phrase_beats_replaced",
			view: ErrorView{Kind: KindString, Text: "deleted state.db-wal; state.db was replaced underneath"},
			want: CauseDeletedWal,
		},
		{
			// The disk fallback catches wording the disk-full markers do
			// not: readonly spellings still mean "cannot write".
			name: "readonly_falls_into_disk_bucket",
			view: ErrorView{Kind: KindString, Text: "attempt to write a readonly database"},
			want: CauseDisk,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := Classify(tt.view); got != tt.want {
				t.Errorf("Classify(%+v) = %q, want %q", tt.view, got, tt.want)
			}
		})
	}
}

// TestPredicateTypeGates pins which kinds open which predicates: the
// malformed diagnostics require the DatabaseError family, the transient
// predicate requires OperationalError, and only disk-full has an errno
// shortcut. A bare string must fail every type gate even when its text
// matches a marker.
func TestPredicateTypeGates(t *testing.T) {
	text := "database disk image is malformed"
	if IsMalformedDB(ErrorView{Kind: KindString, Text: text}) {
		t.Error("malformed_db fired for a bare string; the isinstance gate is broken")
	}
	// OperationalError subclasses DatabaseError in the sqlite3 hierarchy:
	// the family gate must include it.
	if !IsMalformedDB(ErrorView{Kind: KindOperationalError, Text: text}) {
		t.Error("malformed_db did not fire for OperationalError, a DatabaseError subclass")
	}
	if !IsMalformedDB(ErrorView{Kind: KindDatabaseError, Text: text}) {
		t.Error("malformed_db did not fire for DatabaseError with corrupt-image text")
	}
	// StateDbCorruptError subclasses sqlite3.DatabaseError: the family gate
	// must include the typed quarantine error.
	if !IsMalformedDB(ErrorView{Kind: KindCorrupt, Text: text}) {
		t.Error("malformed_db did not fire for the typed corrupt kind, a DatabaseError subclass")
	}
	if IsMalformedSchema(ErrorView{Kind: KindDatabaseError, Text: text}) {
		t.Error("malformed_schema fired on generic corrupt-image text; runtime repair must fail closed on it")
	}
	if !IsMalformedSchema(ErrorView{Kind: KindDatabaseError, Text: "malformed database schema"}) {
		t.Error("malformed_schema did not fire on explicit schema text")
	}
	if IsTransient(ErrorView{Kind: KindDatabaseError, Text: "database is locked"}) {
		t.Error("transient fired for DatabaseError; OperationalError only")
	}
	if !IsTransient(ErrorView{Kind: KindOperationalError, Text: "database is locked"}) {
		t.Error("transient did not fire for a locked OperationalError")
	}
	// None is false everywhere; ENOSPC is the only message-independent path.
	none := ErrorView{Kind: KindNone}
	if IsMalformedDB(none) || IsMalformedSchema(none) || IsTransient(none) ||
		IsDiskFull(none) || IsFTSScopedCorruption(none) {
		t.Error("a predicate fired for the none kind")
	}
	if !IsDiskFull(ErrorView{Kind: KindOSError, Text: "write failed", ENOSPC: true}) {
		t.Error("disk_full did not fire on the ENOSPC errno shortcut")
	}
	if IsDiskFull(ErrorView{Kind: KindOSError, Text: "write failed"}) {
		t.Error("disk_full fired for an OSError with neither ENOSPC nor marker wording")
	}
}

// TestFTSProvenanceRules pins the result-code precedence of
// IsFTSScopedCorruption: a known code decides alone, VTAB outranks prose,
// any other known code contradicts FTS-looking prose, and only the codeless
// path consults the text.
func TestFTSProvenanceRules(t *testing.T) {
	code := func(n int) *int { return &n }
	tests := []struct {
		name string
		view ErrorView
		want bool
	}{
		{
			name: "vtab_code_alone",
			view: ErrorView{Kind: KindDatabaseError, Text: "node handler failed", Code: code(sqliteCorruptVtab)},
			want: true,
		},
		{
			name: "vtab_code_outranks_generic_prose",
			view: ErrorView{Kind: KindDatabaseError, Text: "database disk image is malformed", Code: code(sqliteCorruptVtab)},
			want: true,
		},
		{
			// Bare SQLITE_CORRUPT carries no object scope.
			name: "bare_corrupt_code_carries_no_scope",
			view: ErrorView{Kind: KindDatabaseError, Text: "malformed messages_fts", Code: code(11)},
			want: false,
		},
		{
			name: "codeless_fts5_corruption_report",
			view: ErrorView{Kind: KindString, Text: "fts5: database corruption in messages_fts_data"},
			want: true,
		},
		{
			name: "codeless_corruption_marker_naming_fts_object",
			view: ErrorView{Kind: KindString, Text: "error: database disk image is malformed inside messages_fts_idx"},
			want: true,
		},
		{
			name: "fts_object_name_without_corruption",
			view: ErrorView{Kind: KindString, Text: "messages_fts_docsize has a missing row"},
			want: false,
		},
		{
			name: "corruption_marker_without_fts_object",
			view: ErrorView{Kind: KindString, Text: "database disk image is malformed"},
			want: false,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := IsFTSScopedCorruption(tt.view); got != tt.want {
				t.Errorf("IsFTSScopedCorruption(%+v) = %v, want %v", tt.view, got, tt.want)
			}
		})
	}
}
