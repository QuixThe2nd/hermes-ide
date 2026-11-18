package jevnotices

import "testing"

// TestNoticesEnabledTable pins display.jev_notices coercion: absent or null
// keeps the default (on), and every other value goes through the project's
// shared truthy set (utils.TRUTHY_STRINGS: 1/true/yes/on), so the switch
// reads the same way here as every Python bool-ish config key.
func TestNoticesEnabledTable(t *testing.T) {
	cases := []struct {
		name   string
		record map[string]any
		want   bool
	}{
		{"absent key keeps the default", map[string]any{"purpose": "goal-intent"}, true},
		{"null inherits the default", map[string]any{NoticesKey: nil}, true},
		{"yaml true", map[string]any{NoticesKey: true}, true},
		{"yaml false", map[string]any{NoticesKey: false}, false},
		{"string true", map[string]any{NoticesKey: "true"}, true},
		{"string on with padding", map[string]any{NoticesKey: "  On "}, true},
		{"string yes", map[string]any{NoticesKey: "YES"}, true},
		{"string one", map[string]any{NoticesKey: "1"}, true},
		{"string false", map[string]any{NoticesKey: "false"}, false},
		{"string off", map[string]any{NoticesKey: "off"}, false},
		{"string no", map[string]any{NoticesKey: "no"}, false},
		{"empty string", map[string]any{NoticesKey: ""}, false},
		{"unknown word never opts in", map[string]any{NoticesKey: "enabled"}, false},
		{"number zero", map[string]any{NoticesKey: float64(0)}, false},
		{"number one", map[string]any{NoticesKey: float64(1)}, true},
		{"go int zero", map[string]any{NoticesKey: 0}, false},
		{"go int non-zero", map[string]any{NoticesKey: 2}, true},
		{"unexpected type keeps the default", map[string]any{NoticesKey: struct{}{}}, true},
		{"empty mapping", map[string]any{NoticesKey: map[string]any{}}, false},
		{"non-empty list", map[string]any{NoticesKey: []any{"x"}}, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := NoticesEnabled(tc.record); got != tc.want {
				t.Fatalf("NoticesEnabled(%v) = %v, want %v", tc.record, got, tc.want)
			}
		})
	}
}

// TestNoticesEnabledIgnoresTheVerdictFields: the switch is the only field
// that can silence a record; outcome fields never do.
func TestNoticesEnabledIgnoresTheVerdictFields(t *testing.T) {
	for _, tc := range loadSharedCases(t) {
		if !NoticesEnabled(tc.Record) {
			t.Errorf("shared case %q is silenced without a %q field", tc.Name, NoticesKey)
		}
		silenced := map[string]any{NoticesKey: false}
		for key, value := range tc.Record {
			silenced[key] = value
		}
		if NoticesEnabled(silenced) {
			t.Errorf("shared case %q still renders with %s=false", tc.Name, NoticesKey)
		}
	}
}
