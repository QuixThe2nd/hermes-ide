package modelcmd

import "testing"

// TestRenderListingSharedTable runs every shared-table record through
// RenderListing and compares the exact reply text.
func TestRenderListingSharedTable(t *testing.T) {
	for _, tc := range loadTable(t).Listing {
		if got := RenderListing(tc.Record); got != tc.Want {
			t.Errorf("%s:\n got:\n%s\nwant:\n%s", tc.Name, got, tc.Want)
		}
	}
}

// TestRenderListingHiddenModelsTail pins the "+N more" arithmetic: the tail
// appears exactly when total_models exceeds the shown list.
func TestRenderListingHiddenModelsTail(t *testing.T) {
	rec := ListingRecord{
		CurrentModel:         "m",
		CurrentProviderLabel: "P",
		Providers: []ListingProvider{
			{Name: "P", Slug: "p", IsCurrent: true, Models: []string{"a"}, TotalModels: 3},
			{Name: "Q", Slug: "q", Models: []string{"a"}, TotalModels: 1},
		},
	}
	out := RenderListing(rec)
	if !contains(out, "`a` (+2 more)") {
		t.Errorf("hidden-models tail missing/miscalculated:\n%s", out)
	}
	// Exactly-shown provider must NOT carry the tail.
	if contains(out, "(+0 more)") {
		t.Errorf("zero-count tail rendered:\n%s", out)
	}
}

// TestRenderListingEndpointFallback: a provider with no listed models
// shows its api_url instead of an empty model line.
func TestRenderListingEndpointFallback(t *testing.T) {
	rec := ListingRecord{
		CurrentModel:         "m",
		CurrentProviderLabel: "P",
		Providers: []ListingProvider{
			{Name: "Custom", Slug: "custom", APIURL: "https://api.example.com/v1"},
		},
	}
	out := RenderListing(rec)
	if !contains(out, "  `https://api.example.com/v1`") {
		t.Errorf("api_url fallback line missing:\n%s", out)
	}
}

// TestRenderListingMissingLabelsFallBackToEnglish: a record without a
// labels map (or with blank strings) renders with the English defaults
// instead of literal {model} placeholders.
func TestRenderListingMissingLabelsFallBackToEnglish(t *testing.T) {
	rec := ListingRecord{CurrentModel: "m", CurrentProviderLabel: "P"}
	out := RenderListing(rec)
	if !contains(out, "Current: `m` on P") {
		t.Errorf("default current label not applied:\n%s", out)
	}
	for _, want := range []string{"`/model <name>` — switch model"} {
		if !contains(out, want) {
			t.Errorf("default usage line missing (%q):\n%s", want, out)
		}
	}
}

// TestRenderListingPlaceholderSubstitution: localized templates substitute
// their own placeholders ({count} included), and provider/model ids are
// never rewritten by label formatting.
func TestRenderListingPlaceholderSubstitution(t *testing.T) {
	rec := ListingRecord{
		CurrentModel:         "m",
		CurrentProviderLabel: "P",
		Labels: map[string]string{
			"current_label":      "Actuel : `{model}` sur {provider}",
			"more_models_suffix": " (+{count} autres)",
		},
		Providers: []ListingProvider{
			{Name: "P", Slug: "p", Models: []string{"a"}, TotalModels: 4},
		},
	}
	out := RenderListing(rec)
	if !contains(out, "Actuel : `m` sur P") {
		t.Errorf("localized current label not substituted:\n%s", out)
	}
	if !contains(out, "`a` (+3 autres)") {
		t.Errorf("localized count not substituted:\n%s", out)
	}
}

func contains(haystack, needle string) bool {
	return len(needle) == 0 || indexOf(haystack, needle) >= 0
}

func indexOf(h, n string) int {
	for i := 0; i+len(n) <= len(h); i++ {
		if h[i:i+len(n)] == n {
			return i
		}
	}
	return -1
}
