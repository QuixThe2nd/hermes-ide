package footerfmt

import "testing"

func TestShortModel(t *testing.T) {
	cases := []struct {
		model, want string
	}{
		{"openai/gpt-5.4", "gpt-5.4"},
		{"anthropic/claude-sonnet-4.6", "sonnet-4.6"},
		{"claude-opus-5-5", "opus-5-5"},
		{"gpt-5.4", "gpt-5.4"},
		{"gemini-3.1-pro", "gemini-3.1-pro"},
		{"claude", "claude"},
		{"", ""},
	}
	for _, tc := range cases {
		if got := ShortModel(tc.model); got != tc.want {
			t.Errorf("ShortModel(%q) = %q, want %q", tc.model, got, tc.want)
		}
	}
}

func TestEffortShorthand(t *testing.T) {
	cases := []struct {
		effort, want string
	}{
		{"medium", "med."},
		{"minimal", "min."},
		{"low", "low"},
		{"high", "high"},
		{"xhigh", "xhigh"},
		{"off", "off"},
		{"", ""},
	}
	for _, tc := range cases {
		if got := EffortShorthand(tc.effort); got != tc.want {
			t.Errorf("EffortShorthand(%q) = %q, want %q", tc.effort, got, tc.want)
		}
	}
}

func TestModelField(t *testing.T) {
	cases := []struct {
		model, effort, want string
	}{
		{"claude-opus-5-5", "medium", "opus-5-5 (med.)"},
		{"anthropic/claude-opus-5-5", "medium", "opus-5-5 (med.)"},
		{"opus-5-5", "minimal", "opus-5-5 (min.)"},
		{"opus-5-5", "xhigh", "opus-5-5 (xhigh)"},
		{"gpt-5.4", "", "gpt-5.4"},
		{"", "medium", ""}, // no model carries no effort
	}
	for _, tc := range cases {
		if got := ModelField(tc.model, tc.effort); got != tc.want {
			t.Errorf("ModelField(%q, %q) = %q, want %q", tc.model, tc.effort, got, tc.want)
		}
	}
}

func TestFormatDuration(t *testing.T) {
	cases := []struct {
		seconds float64
		want    string
	}{
		{-1.0, ""},
		{0.4, "0.4s"},
		{9.99, "10.0s"},
		{10, "10s"},
		{42.7, "43s"},
		{60, "1m00s"},
		{125, "2m05s"},
		{3599, "59m59s"},
		{3600, "1h00m"},
		{3725, "1h02m"},
	}
	for _, tc := range cases {
		if got := FormatDuration(tc.seconds); got != tc.want {
			t.Errorf("FormatDuration(%v) = %q, want %q", tc.seconds, got, tc.want)
		}
	}
}

func fp(v float64) *float64 { return &v }
func ip(v int) *int         { return &v }

func TestToolsField(t *testing.T) {
	cases := []struct {
		name      string
		toolTime  *float64
		toolCalls *int
		want      string
	}{
		{"zero time, zero calls", fp(0.0), ip(0), "tools 0"},
		{"zero time, some calls", fp(0.0), ip(5), "tools 0"},
		{"measured time, zero calls", fp(3.1), ip(0), "tools 0"},
		{"measured time and calls", fp(3.1), ip(5), "tools 3.1s"},
		{"whole-second time", fp(15.0), ip(3), "tools 15s"},
		{"unmeasured time, zero calls", nil, ip(0), ""},
		{"unmeasured time and calls", nil, nil, ""},
		{"negative time", fp(-1.0), ip(3), ""},
		{"measured time, unmeasured calls", fp(2.5), nil, "tools 2.5s"},
	}
	for _, tc := range cases {
		if got := ToolsField(tc.toolTime, tc.toolCalls); got != tc.want {
			t.Errorf("%s: ToolsField = %q, want %q", tc.name, got, tc.want)
		}
	}
}
