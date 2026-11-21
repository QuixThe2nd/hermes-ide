package footerfmt

import (
	"math"
	"testing"
)

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

func TestPhaseTail(t *testing.T) {
	cases := []struct {
		name     string
		overhead *float64
		phases   map[string]float64
		want     string
	}{
		{"unmeasured overhead omitted", nil, map[string]float64{"mem": 1.0}, ""},
		{"no phases keeps legacy single other", fp(13.0), nil, "other 13s"},
		{"empty phases keeps legacy single other", fp(13.0), map[string]float64{}, "other 13s"},
		{
			"fixed order after tools",
			fp(8.8),
			map[string]float64{"save": 0.2, "compress": 5.0, "setup": 0.4, "mem": 3.2},
			"mem 3.2s · setup 0.4s · compress 5.0s · save 0.2s",
		},
		{
			"residual other renders when above threshold",
			fp(30.0),
			map[string]float64{"mem": 5.0, "setup": 4.9},
			"mem 5.0s · setup 4.9s · other 20s",
		},
		{
			"sub-threshold phase hidden but still attributed",
			fp(30.0),
			map[string]float64{"mem": 0.05, "setup": 4.9},
			"setup 4.9s · other 25s",
		},
		{
			"residual below threshold omitted",
			fp(29.05),
			map[string]float64{"mem": 5.0, "setup": 24.0},
			"mem 5.0s · setup 24s",
		},
		{
			"unknown keys ignored",
			fp(20.0),
			map[string]float64{"bogus": 5.0, "mem": 1.0},
			"mem 1.0s · other 19s",
		},
		{
			"all phases hidden still shrink the residual",
			fp(1.0),
			map[string]float64{"mem": 0.02, "setup": 0.03},
			"other 0.9s",
		},
		{"negative overhead renders nothing", fp(-2.0), nil, ""},
	}
	for _, tc := range cases {
		if got := PhaseTail(tc.overhead, tc.phases); got != tc.want {
			t.Errorf("%s: PhaseTail = %q, want %q", tc.name, got, tc.want)
		}
	}
}

func TestPhaseTailThresholdBoundary(t *testing.T) {
	at := PhaseTail(fp(1.0), map[string]float64{"mem": 0.1})
	if at != "mem 0.1s · other 0.9s" {
		t.Errorf("0.1s renders: PhaseTail = %q, want %q", at, "mem 0.1s · other 0.9s")
	}
	below := PhaseTail(fp(1.0), map[string]float64{"mem": 0.099})
	if below != "other 0.9s" {
		t.Errorf("0.099s hides: PhaseTail = %q, want %q", below, "other 0.9s")
	}
}

func TestContextPct(t *testing.T) {
	cases := []struct {
		name         string
		tokens, wind int
		want         string
	}{
		{"nominal", 230_000, 1_000_000, "23%"},
		{"rounds up", 255_000, 1_000_000, "26%"},
		{"clamps above 100", 120_000, 100_000, "100%"},
		{"clamps below 0", -5, 100, ""},
		{"no window", 10, 0, ""},
		{"zero tokens", 0, 100, "0%"},
	}
	for _, tc := range cases {
		if got := ContextPct(tc.tokens, tc.wind); got != tc.want {
			t.Errorf("%s: ContextPct = %q, want %q", tc.name, got, tc.want)
		}
	}
}

func TestAPICallsField(t *testing.T) {
	cases := []struct {
		calls int
		want  string
	}{
		{1, "1 call"},
		{3, "3 calls"},
		{0, ""},
		{-1, ""},
	}
	for _, tc := range cases {
		if got := APICallsField(tc.calls); got != tc.want {
			t.Errorf("%d: APICallsField = %q, want %q", tc.calls, got, tc.want)
		}
	}
}

func TestFooterLine(t *testing.T) {
	cases := []struct {
		name string
		in   LineInput
		want string
	}{
		{
			"categorized remainder",
			LineInput{
				Model: "claude-opus-5-5", Effort: "medium",
				ContextTokens: 230_000, ContextLength: 1_000_000,
				TurnTime: fp(15.0), APITime: fp(11.0), ToolTime: fp(0.0), ToolCalls: ip(0),
				APICalls: 1,
				Phases:   map[string]float64{"mem": 3.2, "setup": 0.4, "save": 0.2},
			},
			"opus-5-5 (med.) · 23% · 15s · api 11s · tools 0 · mem 3.2s · setup 0.4s · save 0.2s · other 0.2s · 1 call",
		},
		{
			"legacy single other without phases",
			LineInput{
				Model: "claude-opus-5-5", Effort: "medium",
				ContextTokens: 230_000, ContextLength: 1_000_000,
				TurnTime: fp(15.0), APITime: fp(11.0), ToolTime: fp(0.0), ToolCalls: ip(0),
				APICalls: 1,
			},
			"opus-5-5 (med.) · 23% · 15s · api 11s · tools 0 · other 4.0s · 1 call",
		},
		{
			"unmeasured timing fields skipped",
			LineInput{Model: "m", ContextTokens: 5, ContextLength: 100, APICalls: 0},
			"m · 5%",
		},
		{
			"empty when nothing has data",
			LineInput{},
			"",
		},
		{
			"multi call tool turn",
			LineInput{
				Model:         "openai/gpt-5.4",
				ContextTokens: 50_000, ContextLength: 400_000,
				TurnTime: fp(70.0), APITime: fp(42.0), ToolTime: fp(15.0), ToolCalls: ip(6),
				APICalls: 3,
			},
			"gpt-5.4 · 13% · 1m10s · api 42s · tools 15s · other 13s · 3 calls · 6 tools",
		},
	}
	for _, tc := range cases {
		if got := FooterLine(tc.in); got != tc.want {
			t.Errorf("%s: FooterLine = %q, want %q", tc.name, got, tc.want)
		}
	}
}

func TestParsePhases(t *testing.T) {
	got := ParsePhases(map[string]float64{
		"mem_time": 3.2, "save_time": 0.2, "api_time": 1.0,
	})
	want := map[string]float64{"mem": 3.2, "save": 0.2}
	if len(got) != len(want) {
		t.Fatalf("ParsePhases = %v, want %v", got, want)
	}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("ParsePhases[%q] = %v, want %v", k, got[k], v)
		}
	}
	if ParsePhases(nil) != nil {
		t.Error("ParsePhases(nil) must be nil")
	}
	if ParsePhases(map[string]float64{"api_time": 1.0}) != nil {
		t.Error("ParsePhases without phase keys must be nil")
	}
}

func TestToolCallsField(t *testing.T) {
	cases := []struct {
		calls *int
		want  string
	}{
		{ip(1), "1 tool"},
		{ip(9), "9 tools"},
		{ip(0), ""},
		{nil, ""},
	}
	for _, tc := range cases {
		if got := ToolCallsField(tc.calls); got != tc.want {
			t.Errorf("ToolCallsField = %q, want %q", got, tc.want)
		}
	}
}

func TestFooterLinePartialTimingOmitsTail(t *testing.T) {
	// The overhead needs the full turn/api/tool triple; phases alone must
	// never conjure a tail when any timing field is unmeasured.
	in := LineInput{
		Model: "m", ContextTokens: 5, ContextLength: 100,
		TurnTime: fp(9.0),
		Phases:   map[string]float64{"mem": 3.2},
		APICalls: 1,
	}
	if got := FooterLine(in); got != "m · 5% · 9.0s · 1 call" {
		t.Errorf("FooterLine = %q, want partial timing without tail", got)
	}
}

func TestPhaseTailResidualAtExactThreshold(t *testing.T) {
	// Residual exactly 0.1s renders: the threshold is inclusive. (0.2-0.1
	// is exactly the double 0.1; 1.0-0.9 is not — float subtraction decides.)
	got := PhaseTail(fp(0.2), map[string]float64{"mem": 0.1})
	if got != "mem 0.1s · other 0.1s" {
		t.Errorf("PhaseTail = %q, want %q", got, "mem 0.1s · other 0.1s")
	}
}

func TestMergePhases(t *testing.T) {
	cases := []struct {
		name           string
		gateway, agent map[string]float64
		want           map[string]float64
	}{
		{"sums shared keys", map[string]float64{"setup": 0.85, "save": 1.4},
			map[string]float64{"mem": 1.9, "setup": 0.05, "save": 0.02},
			map[string]float64{"mem": 1.9, "setup": 0.9, "save": 1.42}},
		{"agent only", nil, map[string]float64{"mem": 0.4}, map[string]float64{"mem": 0.4}},
		{"gateway only", map[string]float64{"save": 2.0}, nil, map[string]float64{"save": 2.0}},
		{"negatives ignored", map[string]float64{"save": -0.3},
			map[string]float64{"save": 0.5, "mem": -1}, map[string]float64{"save": 0.5}},
		{"zero kept as measured", map[string]float64{"setup": 0}, nil, map[string]float64{"setup": 0}},
		{"nothing measured", nil, map[string]float64{"mem": -0.1}, nil},
		{"both nil", nil, nil, nil},
	}
	for _, tc := range cases {
		assertPhases(t, "MergePhases "+tc.name, MergePhases(tc.gateway, tc.agent), tc.want)
	}
}

// assertPhases compares phase maps within float tolerance; nil and empty
// differ (nil keeps the legacy single-"other" render).
func assertPhases(t *testing.T, label string, got, want map[string]float64) {
	t.Helper()
	if (got == nil) != (want == nil) || len(got) != len(want) {
		t.Errorf("%s = %v, want %v", label, got, want)
		return
	}
	for key, w := range want {
		if math.Abs(got[key]-w) > 1e-9 {
			t.Errorf("%s[%q] = %v, want %v", label, key, got[key], w)
		}
	}
}

func TestFooterPhases(t *testing.T) {
	live := map[string]float64{"mem_time": 1.9, "setup_time": 0.05, "save_time": 0.02,
		"gateway_setup_time": 0.85, "conversation_ended_at": 100}
	cases := []struct {
		name        string
		result      map[string]float64
		turnEndedAt *float64
		want        map[string]float64
	}{
		{"gateway spans fold into setup and save", live, fp(102),
			map[string]float64{"mem": 1.9, "setup": 0.9, "save": 2.02}},
		{"no turn-end stamp measures no save span", live, nil,
			map[string]float64{"mem": 1.9, "setup": 0.9, "save": 0.02}},
		{"agent only", map[string]float64{"mem_time": 0.4}, fp(5), map[string]float64{"mem": 0.4}},
		{"stamp after turn end is dropped",
			map[string]float64{"conversation_ended_at": 6}, fp(5), nil},
		{"nothing measured", nil, fp(5), nil},
	}
	for _, tc := range cases {
		assertPhases(t, "FooterPhases "+tc.name, FooterPhases(tc.result, tc.turnEndedAt), tc.want)
	}
}

func TestFooterLineWithGatewayPhases(t *testing.T) {
	// The live footer's bare "other 4.4s" splits into mem/setup/save; every
	// other segment is identical to the legacy render.
	result := map[string]float64{"mem_time": 1.9, "setup_time": 0.05, "compress_time": 0,
		"save_time": 0.02, "gateway_setup_time": 0.85, "conversation_ended_at": 50}
	calls := 0
	in := LineInput{Model: "claude-opus-5-5", ContextTokens: 1900, ContextLength: 10000,
		TurnTime: fp(15.4), APITime: fp(11), ToolTime: fp(0), ToolCalls: &calls, APICalls: 1}
	if got, want := FooterLine(in), "opus-5-5 · 19% · 15s · api 11s · tools 0 · other 4.4s · 1 call"; got != want {
		t.Errorf("legacy FooterLine = %q, want %q", got, want)
	}
	in.Phases = FooterPhases(result, fp(51.4))
	want := "opus-5-5 · 19% · 15s · api 11s · tools 0 · mem 1.9s · setup 0.9s · save 1.4s · other 0.2s · 1 call"
	if got := FooterLine(in); got != want {
		t.Errorf("FooterLine = %q, want %q", got, want)
	}
}

func TestMergePhasesFeedsPhaseTail(t *testing.T) {
	// The merged map attributes the gateway's spans; only the true residual
	// stays "other" (15.4 turn - 11 api - 0 tools - 1.9 - 0.9 - 1.42).
	phases := MergePhases(map[string]float64{"setup": 0.85, "save": 1.4},
		map[string]float64{"mem": 1.9, "setup": 0.05, "compress": 0, "save": 0.02})
	want := "mem 1.9s · setup 0.9s · save 1.4s · other 0.2s"
	if got := PhaseTail(fp(4.4), phases); got != want {
		t.Errorf("PhaseTail = %q, want %q", got, want)
	}
}
