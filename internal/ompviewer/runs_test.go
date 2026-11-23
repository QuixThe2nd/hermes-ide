package ompviewer

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestEntryFileNameDeterministicShape(t *testing.T) {
	name := EntryFileName("/srv/work/proj")
	if len(name) != 64+len(".json") || !strings.HasSuffix(name, ".json") {
		t.Fatalf("unexpected entry file name shape: %q", name)
	}
	for _, r := range name[:64] {
		if !strings.ContainsRune("0123456789abcdef", r) {
			t.Fatalf("non-hex digest character in %q", name)
		}
	}
	if name != EntryFileName("/srv/work/proj") {
		t.Fatal("entry file name is not deterministic")
	}
	if name == EntryFileName("/srv/work/other") {
		t.Fatal("distinct workdirs must map to distinct entry files")
	}
}

func TestValidateWorkdir(t *testing.T) {
	t.Parallel()
	invalid := []string{
		"",
		"relative/dir",
		"/srv/work/../escape",
		"/srv/\x00work",
	}
	for _, workdir := range invalid {
		if err := ValidateWorkdir(workdir); err == nil {
			t.Errorf("ValidateWorkdir(%q) = nil, want error", workdir)
		}
	}
	valid := []string{
		"/srv/work/proj",
		"/srv/work/a..b", // "a.." is a legitimate directory name
		"/",
	}
	for _, workdir := range valid {
		if err := ValidateWorkdir(workdir); err != nil {
			t.Errorf("ValidateWorkdir(%q) = %v, want nil", workdir, err)
		}
	}
}

func TestNewRunEntryShape(t *testing.T) {
	now := time.Date(2026, 11, 23, 1, 2, 3, 456789000, time.FixedZone("X", 3600))
	entry, err := NewRunEntry("/srv/work/proj", "/srv/work/proj/.hermes-lane/m", "litellm/backend_dev", now)
	if err != nil {
		t.Fatalf("NewRunEntry: %v", err)
	}
	if entry.Version != SchemaVersion {
		t.Errorf("Version = %d, want %d", entry.Version, SchemaVersion)
	}
	// Second precision, UTC, ISO-8601 with numeric offset — same shape the
	// Python writer produces via datetime.isoformat().
	if entry.RecordedAt != "2026-11-23T00:02:03+00:00" {
		t.Errorf("RecordedAt = %q, want %q", entry.RecordedAt, "2026-11-23T00:02:03+00:00")
	}
	if _, err := NewRunEntry("relative", "", "", now); err == nil {
		t.Error("NewRunEntry with relative workdir = nil error, want error")
	}
}

func TestNewRunEntryClampsFieldsInRunes(t *testing.T) {
	long := strings.Repeat("é", MaxFieldChars+10) // multi-byte: rune clamp, not byte clamp
	entry, err := NewRunEntry("/srv/work/proj", long, long, time.Now())
	if err != nil {
		t.Fatalf("NewRunEntry: %v", err)
	}
	if got := len([]rune(entry.SessionDir)); got != MaxFieldChars {
		t.Errorf("SessionDir = %d runes, want %d", got, MaxFieldChars)
	}
	if got := len([]rune(entry.Model)); got != MaxFieldChars {
		t.Errorf("Model = %d runes, want %d", got, MaxFieldChars)
	}
}

func TestMarshalRoundTrip(t *testing.T) {
	entry, err := NewRunEntry("/srv/work/proj", "/srv/work/proj/.hermes-lane/m", "provider/model", time.Now())
	if err != nil {
		t.Fatalf("NewRunEntry: %v", err)
	}
	raw, err := Marshal(entry)
	if err != nil {
		t.Fatalf("Marshal: %v", err)
	}
	if !strings.HasSuffix(string(raw), "\n") {
		t.Error("Marshal output must end with a newline, like the Python writer")
	}
	if len(raw) > MaxEntryBytes {
		t.Errorf("entry = %d bytes, exceeds reader bound %d", len(raw), MaxEntryBytes)
	}
	var decoded map[string]any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatalf("Unmarshal: %v", err)
	}
	if decoded["version"] != float64(SchemaVersion) {
		t.Errorf("version = %v, want %d", decoded["version"], SchemaVersion)
	}
	for _, key := range []string{"workdir", "session_dir", "model", "recorded_at"} {
		if _, ok := decoded[key]; !ok {
			t.Errorf("missing wire key %q", key)
		}
	}
}
