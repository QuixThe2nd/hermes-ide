package responsessystemitem

import (
	"reflect"
	"strings"
	"testing"
)

// The routing matrix mirrors tests/agent/transports/test_codex_transport.py::
// TestBridgedResponsesSystemPromptAsInputItem — every first-party origin keeps
// the historical instructions wire; everything else with a custom base URL is
// bridged and carries the system prompt as the leading input item.
func TestSystemPromptAsInputItemRoutingMatrix(t *testing.T) {
	cases := []struct {
		name string
		req  RouteRequest
		want bool
	}{
		{"LiteLLM gateway", RouteRequest{BaseURL: "http://192.168.30.21:4000/v1"}, true},
		{"LiteLLM by hostname", RouteRequest{BaseURL: "https://litellm.internal.example/v1"}, true},
		{"custom corporate proxy", RouteRequest{BaseURL: "https://llm.corp.example/openai/v1"}, true},
		{"port-only custom origin", RouteRequest{BaseURL: "http://localhost:8788/v1"}, true},
		{"OpenAI first-party", RouteRequest{BaseURL: "https://api.openai.com/v1"}, false},
		{"xAI flagged route", RouteRequest{BaseURL: "https://api.x.ai/v1", IsXAIResponses: true}, false},
		{"SDK default origin (no base URL)", RouteRequest{}, false},
		{"GitHub flagged route", RouteRequest{BaseURL: "https://models.github.ai/v1", IsGitHubResponses: true}, false},
		{"Azure per-resource host", RouteRequest{BaseURL: "https://my-resource.openai.azure.com/openai"}, false},
		{"Azure AI Foundry host", RouteRequest{BaseURL: "https://my-service.services.ai.azure.com/v1"}, false},
		{"OpenCode host", RouteRequest{BaseURL: "https://opencode.ai/v1"}, false},
		{"OpenCode provider family", RouteRequest{BaseURL: "https://gateway.example/v1", Provider: "opencode-zen"}, false},
		{"Perplexity host", RouteRequest{BaseURL: "https://api.perplexity.ai/v1"}, false},
		{"Meta host", RouteRequest{BaseURL: "https://api.meta.ai/v1"}, false},
	}
	for _, tc := range cases {
		if got := SystemPromptAsInputItem(tc.req); got != tc.want {
			t.Errorf("%s: SystemPromptAsInputItem(%+v) = %v, want %v", tc.name, tc.req, got, tc.want)
		}
	}
}

// Lookalike hosts must never inherit a native classification: a subdomain of
// evil.example that merely contains a vendor string stays bridged (the safe
// layout), and HostMatches must not substring-match.
func TestLookalikeHostsStayBridged(t *testing.T) {
	lookalikes := []string{
		"https://api.openai.com.evil.example/v1",
		"https://evil.example/api.openai.com/v1",
		"https://openai.azure.com.evil.example/v1",
		"https://api.meta.ai.example/v1",
	}
	for _, base := range lookalikes {
		if !SystemPromptAsInputItem(RouteRequest{BaseURL: base}) {
			t.Errorf("lookalike %q classified native; want bridged", base)
		}
	}
	if HostMatches("evil.com/openai.azure.com", "openai.azure.com") {
		t.Error("HostMatches must not match paths")
	}
}

// Host parsing mirrors utils._parse_base_url: scheme-less bare hosts, ports,
// paths, trailing dots and uppercase all resolve; empties stay empty.
func TestHostParsingTolerances(t *testing.T) {
	cases := []struct{ in, want string }{
		{"", ""},
		{"   ", ""},
		{"https://API.OpenAI.COM/v1", "api.openai.com"},
		{"//192.168.30.21:4000/v1", "192.168.30.21"},
		{"192.168.30.21:4000/v1", "192.168.30.21"},
		{"http://litellm.internal:4000", "litellm.internal"},
		{"https://api.openai.com.", "api.openai.com"},
	}
	for _, tc := range cases {
		if got := Host(tc.in); got != tc.want {
			t.Errorf("Host(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

// The bridged wire shape: exactly one system row, at input[0], non-blank, with
// the instructions field absent. A second system row — the failure mode the
// empty-instructions default-identity fallback would create — must be countable
// so the invariant is assertable.
func TestBridgedWireShapeInvariants(t *testing.T) {
	system := WireItem{Role: "system", Content: "You are Hermes Agent."}
	bridged := WirePayload{Input: []WireItem{system, {Role: "user", Content: "Ping"}}}
	if bridged.SystemRowCount() != 1 {
		t.Errorf("bridged SystemRowCount() = %d, want 1", bridged.SystemRowCount())
	}
	if !bridged.HasLeadingSystem() {
		t.Error("bridged payload must have the system row at input[0]")
	}
	doubled := WirePayload{Input: []WireItem{system, system}}
	if doubled.SystemRowCount() != 2 {
		t.Errorf("doubled SystemRowCount() = %d, want 2 (detectable, not silently accepted)", doubled.SystemRowCount())
	}
	if (WirePayload{Input: []WireItem{{Role: "system", Content: "   "}}}).HasLeadingSystem() {
		t.Error("blank leading system content must not count as a leading system row")
	}
}

// Preflight contract: leading system row present -> empty/absent instructions
// dropped (never defaulted, which would stack a second system row); no leading
// system row -> instructions required and defaulted when empty; system rows
// outside input[0] or blank are hard errors; normalization is idempotent.
func TestPreflightLayoutContract(t *testing.T) {
	identity := "Default identity."
	system := WireItem{Role: "system", Content: "You are Hermes Agent."}
	user := WireItem{Role: "user", Content: "hello"}
	empty := ""

	normalized, err := Preflight(WirePayload{Instructions: &empty, Input: []WireItem{system, user}}, identity)
	if err != nil {
		t.Fatalf("leading system + empty instructions: %v", err)
	}
	if normalized.Instructions != nil {
		t.Errorf("instructions = %q, want dropped", *normalized.Instructions)
	}
	again, err := Preflight(normalized, identity)
	if err != nil || !reflect.DeepEqual(normalized, again) {
		t.Errorf("preflight not idempotent: %v, %+v vs %+v", err, normalized, again)
	}

	missing, err := Preflight(WirePayload{Input: []WireItem{system}}, identity)
	if err != nil || missing.Instructions != nil {
		t.Errorf("absent instructions with leading system: err=%v instructions=%v, want nil/nil", err, missing.Instructions)
	}

	if _, err := Preflight(WirePayload{Input: []WireItem{user, system}}, identity); err == nil {
		t.Error("system row outside input[0] must be rejected")
	}
	if _, err := Preflight(WirePayload{Input: []WireItem{{Role: "system", Content: "  "}, user}}, identity); err == nil {
		t.Error("blank system row must be rejected")
	}

	if _, err := Preflight(WirePayload{Input: []WireItem{user}}, identity); err == nil {
		t.Error("missing instructions without leading system must be rejected")
	}
	native, err := Preflight(WirePayload{Instructions: &empty, Input: []WireItem{user}}, identity)
	if err != nil {
		t.Fatalf("empty instructions native route: %v", err)
	}
	if native.Instructions == nil || *native.Instructions != identity {
		t.Errorf("native empty instructions = %v, want default identity fallback", native.Instructions)
	}
}

// Prefix stability across turns: turn 2's payload must begin with exactly
// turn 1's system row, and a changed system prompt must break stability —
// otherwise a stale prefix would poison the bridge cache silently.
func TestBridgedPrefixStability(t *testing.T) {
	system := WireItem{Role: "system", Content: "You are Hermes Agent."}
	first := WirePayload{Input: []WireItem{system, {Role: "user", Content: "Ping"}}}
	second := WirePayload{Input: []WireItem{system, {Role: "user", Content: "Ping"}, {Role: "assistant", Content: "Pong"}, {Role: "user", Content: "Again"}}}
	if !BridgedPrefixStable(first, second) {
		t.Error("same system prompt across turns must keep a stable leading row")
	}
	changed := WirePayload{Input: []WireItem{{Role: "system", Content: "v2"}, {Role: "user", Content: "Ping"}}}
	if BridgedPrefixStable(first, changed) {
		t.Error("a changed system prompt must not read as prefix-stable")
	}
	if BridgedPrefixStable(second, WirePayload{Input: []WireItem{{Role: "user", Content: "Ping"}}}) {
		t.Error("payload without a leading system row is not prefix-stable")
	}
}

// Every native Responses host in the spec set must actually classify native
// through SystemPromptAsInputItem (relationship between the host table and the
// classifier, not a frozen count).
func TestSpecHostTablesDriveTheClassifier(t *testing.T) {
	for host := range NativeResponsesHosts {
		if SystemPromptAsInputItem(RouteRequest{BaseURL: "https://" + host + "/v1"}) {
			t.Errorf("native host %q classified bridged", host)
		}
	}
	if !strings.HasPrefix(OpenCodeProviderPrefix, "opencode-") {
		t.Error("OpenCodeProviderPrefix must keep the opencode- family shape")
	}
}
