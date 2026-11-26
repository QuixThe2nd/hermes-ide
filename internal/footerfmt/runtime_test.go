package footerfmt

import "testing"

func TestRuntimeFooterEnabledOrder(t *testing.T) {
	cases := []struct {
		name     string
		override *bool
		display  *bool
		want     bool
	}{
		{"nothing configured keeps the footer", nil, nil, true},
		{"display off suppresses", nil, BoolPtr(false), false},
		{"display on keeps", nil, BoolPtr(true), true},
		{"override off suppresses", BoolPtr(false), nil, false},
		{"override on keeps", BoolPtr(true), nil, true},
		{"override off beats display on", BoolPtr(false), BoolPtr(true), false},
		{"override on beats display off", BoolPtr(true), BoolPtr(false), true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := RuntimeFooterEnabled(tc.override, tc.display); got != tc.want {
				t.Fatalf("RuntimeFooterEnabled(%v, %v) = %v, want %v",
					tc.override, tc.display, got, tc.want)
			}
		})
	}
}

func TestResolveRuntimeFooterChain(t *testing.T) {
	cases := []struct {
		name     string
		override *bool
		platform *bool
		global   *bool
		want     bool
	}{
		{"nothing configured keeps the footer", nil, nil, nil, true},
		{"global off suppresses", nil, nil, BoolPtr(false), false},
		{"platform off suppresses", nil, BoolPtr(false), nil, false},
		{"platform on beats global off", nil, BoolPtr(true), BoolPtr(false), true},
		{"platform off beats global on", nil, BoolPtr(false), BoolPtr(true), false},
		{"override off beats platform on", BoolPtr(false), BoolPtr(true), BoolPtr(true), false},
		{"override on beats platform off", BoolPtr(true), BoolPtr(false), BoolPtr(false), true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := ResolveRuntimeFooter(tc.override, tc.platform, tc.global)
			if got != tc.want {
				t.Fatalf("ResolveRuntimeFooter(%v, %v, %v) = %v, want %v",
					tc.override, tc.platform, tc.global, got, tc.want)
			}
		})
	}
}

func TestRuntimeFooterSuppressedIsTheNegation(t *testing.T) {
	bools := []*bool{nil, BoolPtr(false), BoolPtr(true)}
	for _, override := range bools {
		for _, display := range bools {
			enabled := RuntimeFooterEnabled(override, display)
			if got := RuntimeFooterSuppressed(override, display); got == enabled {
				t.Fatalf("suppressed(%v, %v) = %v, not the negation of enabled = %v",
					override, display, got, enabled)
			}
		}
	}
}
