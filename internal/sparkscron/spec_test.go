package sparkscron

import (
	"strings"
	"testing"
)

// TestDefaultSpecIsValid pins the shipped spec against its own invariants:
// the Python bridge refuses to create a job from a failing spec, so a broken
// build fails loud (no seeding) rather than seeding a malformed record.
func TestDefaultSpecIsValid(t *testing.T) {
	if err := DefaultSpec().Validate(); err != nil {
		t.Fatalf("DefaultSpec().Validate() = %v, want nil", err)
	}
}

// TestSpecValidate rejects every shape the create path must never store.
func TestSpecValidate(t *testing.T) {
	valid := DefaultSpec()
	tests := []struct {
		name string
		mut  func(*Spec)
	}{
		{"wrong name", func(s *Spec) { s.Name = "inbox-sparks-2" }},
		{"empty schedule", func(s *Spec) { s.Schedule = "" }},
		{"non-local deliver leaks runs to a channel", func(s *Spec) { s.Deliver = "origin" }},
		{"no skill reference", func(s *Spec) { s.Skills = nil }},
		{"wrong skill first", func(s *Spec) { s.Skills = []string{"other", Skill} }},
		{"no toolsets", func(s *Spec) { s.EnabledToolsets = nil }},
		{"empty toolset entry", func(s *Spec) { s.EnabledToolsets = []string{"hermes_starts", ""} }},
		{"duplicate toolset entry", func(s *Spec) { s.EnabledToolsets = []string{"hermes_starts", "file", "file"} }},
		{"hermes_starts toolset missing", func(s *Spec) { s.EnabledToolsets = []string{"file", "terminal"} }},
		{"empty prompt", func(s *Spec) { s.Prompt = "" }},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			spec := valid
			tt.mut(&spec)
			if err := spec.Validate(); err == nil {
				t.Fatalf("Validate() = nil, want error for %s", tt.name)
			}
		})
	}
}

// TestDefaultSpecShape pins the relationships the seeded job relies on:
// generic toolsets only (tabctl is host-specific and must stay out), the
// skill the wake expects, and the prompt's own contract markers.
func TestDefaultSpecShape(t *testing.T) {
	spec := DefaultSpec()
	if spec.Name != "inbox-sparks" {
		t.Errorf("Name = %q, want inbox-sparks", spec.Name)
	}
	if spec.Schedule != "every 120m" {
		t.Errorf("Schedule = %q, want every 120m", spec.Schedule)
	}
	if spec.Deliver != "local" {
		t.Errorf("Deliver = %q, want local", spec.Deliver)
	}
	if len(spec.Skills) != 1 || spec.Skills[0] != "hermes-starts" {
		t.Errorf("Skills = %v, want [hermes-starts]", spec.Skills)
	}
	want := map[string]bool{
		"hermes_starts": false, "file": false, "terminal": false,
		"session_search": false, "skills": false, "web": false,
	}
	for _, ts := range spec.EnabledToolsets {
		if _, ok := want[ts]; !ok {
			t.Errorf("unexpected toolset %q in %v", ts, spec.EnabledToolsets)
			continue
		}
		want[ts] = true
	}
	for ts, seen := range want {
		if !seen {
			t.Errorf("toolset %q missing from %v", ts, spec.EnabledToolsets)
		}
	}
	if len(spec.EnabledToolsets) != len(want) {
		t.Errorf("EnabledToolsets = %v, want exactly the generic set", spec.EnabledToolsets)
	}
}

// TestPromptContract: the prompt is self-contained (names its own tool and
// silence marker) and generic (no host-specific tool names, no maintainer
// handles, no channel ids).
func TestPromptContract(t *testing.T) {
	p := Prompt
	for _, needle := range []string{"start_conversation", "[SILENT]", "second person"} {
		if !strings.Contains(p, needle) {
			t.Errorf("Prompt must contain %q", needle)
		}
	}
	for _, banned := range []string{"tabctl", "Parsa", "Quix", "#inbox", "1540279725673418834", "/root/"} {
		if strings.Contains(p, banned) {
			t.Errorf("Prompt must stay generic; found %q", banned)
		}
	}
	if !strings.HasSuffix(strings.TrimSpace(p), "[SILENT] and nothing else.") {
		t.Errorf("Prompt must end with the exact silence instruction")
	}
}

// TestDefaultSpecIsCopy ensures the accessor hands out a deep copy: callers
// (and JSON round-trips) must not be able to mutate the shared Toolsets slice.
func TestDefaultSpecIsCopy(t *testing.T) {
	first := DefaultSpec()
	first.EnabledToolsets[0] = "mutated"
	second := DefaultSpec()
	if second.EnabledToolsets[0] != "hermes_starts" {
		t.Fatalf("DefaultSpec() shares state: got %v", second.EnabledToolsets)
	}
}
