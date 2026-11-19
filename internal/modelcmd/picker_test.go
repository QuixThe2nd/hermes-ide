package modelcmd

import "testing"

// TestPickerSharedTable runs every shared-table record through
// PickerOptions and compares JSON equality (unknown passthrough fields
// included).
func TestPickerSharedTable(t *testing.T) {
	for _, tc := range loadTable(t).Picker {
		got := PickerOptions(tc.Providers, tc.CurrentProvider, tc.MaxProviders, tc.MaxModels)
		if a, b := asJSON(t, got), asJSON(t, tc.Want); a != b {
			t.Errorf("%s:\n got %s\nwant %s", tc.Name, a, b)
		}
	}
}

// TestPickerDropsUnreachableRows: rows with neither models nor an api_url
// cannot be picked and must not survive.
func TestPickerDropsUnreachableRows(t *testing.T) {
	rows := []map[string]any{
		{"slug": "a", "models": []any{"m1"}},
		{"slug": "b", "models": []any{}, "api_url": "https://x/v1"},
		{"slug": "c", "models": []any{}},
		{"slug": "d"},
	}
	got := PickerOptions(rows, "a", 0, 0)
	if len(got) != 2 {
		t.Fatalf("kept %d rows, want 2 (a and b): %s", len(got), asJSON(t, got))
	}
}

// TestPickerMarksExactlyOneCurrent: is_current is enforced against the
// current-provider slug (normalized) and cleared on every other row —
// a stale row from the provider listing cannot masquerade as current.
func TestPickerMarksExactlyOneCurrent(t *testing.T) {
	rows := []map[string]any{
		{"slug": "litellm", "is_current": true, "models": []any{"a"}},
		{"slug": "anthropic", "is_current": true, "models": []any{"b"}},
	}
	got := PickerOptions(rows, " anthropic ", 0, 0)
	for _, r := range got {
		want := r["slug"] == "anthropic"
		if r["is_current"] != want {
			t.Errorf("row %v: is_current = %v, want %v", r["slug"], r["is_current"], want)
		}
	}
}

// TestPickerCapsModelsWithoutLyingAboutTotals: model lists are capped at
// max_models but total_models keeps the FULL count (the "+N more" tail is
// computed from it downstream).
func TestPickerCapsModelsWithoutLyingAboutTotals(t *testing.T) {
	models := make([]any, 6)
	for i := range models {
		models[i] = any("m")
	}
	rows := []map[string]any{
		{"slug": "big", "models": models, "total_models": 12},
	}
	got := PickerOptions(rows, "big", 0, 3)
	r := got[0]
	if len(r["models"].([]any)) != 3 {
		t.Errorf("models not capped to 3: %v", r["models"])
	}
	if n := asInt(t, r["total_models"]); n != 12 {
		t.Errorf("total_models rewritten to %d, want the original 12", n)
	}
}

// asInt coerces either a Go-native int or a JSON-decoded number.
func asInt(t *testing.T, v any) int {
	t.Helper()
	switch n := v.(type) {
	case int:
		return n
	case float64:
		return int(n)
	default:
		t.Fatalf("total_models has unexpected type %T", v)
		return 0
	}
}

// TestPickerProviderCapKeepsCurrent: with more providers than the cap the
// row count is capped AND the current provider survives even when it sat
// beyond the cut line.
func TestPickerProviderCapKeepsCurrent(t *testing.T) {
	rows := make([]map[string]any, 10)
	for i := range rows {
		rows[i] = map[string]any{"slug": "p", "models": []any{"m"}}
	}
	rows[8]["slug"] = "current"
	got := PickerOptions(rows, "current", 5, 0)
	if len(got) != 5 {
		t.Fatalf("kept %d rows, want the cap of 5", len(got))
	}
	found := false
	for _, r := range got {
		if r["slug"] == "current" {
			found = true
		}
	}
	if !found {
		t.Errorf("current provider dropped by the cap: %s", asJSON(t, got))
	}
}

// TestPickerZeroCapsFallBackToDefaults: 0/absent caps mean "default"
// (25 providers, 50 models), never "drop everything".
func TestPickerZeroCapsFallBackToDefaults(t *testing.T) {
	rows := make([]map[string]any, DefaultMaxPickerProviders+5)
	for i := range rows {
		rows[i] = map[string]any{"slug": "p", "models": []any{"m"}}
	}
	if got := PickerOptions(rows, "", 0, 0); len(got) != DefaultMaxPickerProviders {
		t.Fatalf("default cap not applied: kept %d, want %d", len(got), DefaultMaxPickerProviders)
	}
}

// TestPickerPreservesUnknownFields: rows are opaque maps — fields the Go
// side does not know about (source, is_user_defined, ...) pass through.
func TestPickerPreservesUnknownFields(t *testing.T) {
	rows := []map[string]any{
		{"slug": "a", "models": []any{"m"}, "source": "env", "is_user_defined": true},
	}
	got := PickerOptions(rows, "a", 0, 0)
	if got[0]["source"] != "env" || got[0]["is_user_defined"] != true {
		t.Errorf("unknown fields dropped: %s", asJSON(t, got))
	}
}
