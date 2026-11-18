// Package responsessystemitem pins the Responses-route system-prompt layout
// contract that agent/transports/codex.py::_system_prompt_as_input_item_route
// and agent/codex_responses_adapter.py implement.
//
// LiteLLM's /v1/responses -> Anthropic bridge applies cache_control injection
// only to role:system message rows; the instructions field is never marked, so
// every Hermes call through LiteLLM->Claude was cache-cold (measured 0/0 cached
// tokens on a ~3.6k-token prompt as instructions, 3626/3626 cached as a leading
// system input item, 7-24s to first token on the 40-110k production prompts).
//
// This package is the executable spec for that layout; the Python transport
// mirrors the decision and tests/agent/transports/test_codex_transport.py
// asserts the same relationships from the caller's side.
package responsessystemitem

import (
	"fmt"
	"net/url"
	"strings"
)

// NativeResponsesHosts are the exact hostnames whose /v1/responses endpoints
// accept and cache an “instructions“ payload natively. Membership is exact
// (not subdomain): “api.openai.com.evil.example“ must NOT be treated as
// native — sending it the bridged layout is safe, sending a lookalike host the
// native layout would silently drop the system prompt's cacheability.
var NativeResponsesHosts = map[string]bool{
	"api.openai.com": true,
	"chatgpt.com":    true,
}

// MetaHost is Meta's first-party Responses origin; it keeps “instructions“
// like the OpenAI-native set but is matched by its own hostname.
const MetaHost = "api.meta.ai"

// AzureHostSuffixes identify Azure OpenAI / Azure AI Foundry Responses
// endpoints. Unlike the OpenAI-native set these match subdomains: Azure
// per-resource hosts are “<resource>.openai.azure.com“.
var AzureHostSuffixes = []string{"openai.azure.com", "services.ai.azure.com"}

// OpenCodeHost and PerplexityHost are first-party hosts of other vendors whose
// Responses-compatible surfaces keep the historical “instructions“ wire.
const (
	OpenCodeHost   = "opencode.ai"
	PerplexityHost = "api.perplexity.ai"
)

// OpenCodeProviderPrefix marks provider slugs whose routes are OpenCode-backed
// regardless of host (mirror of hermes_cli.models.opencode_provider_family).
const OpenCodeProviderPrefix = "opencode-"

// RouteRequest mirrors the params the Python gate reads from the transport
// call: a base URL plus the route flags the caller already classified.
type RouteRequest struct {
	// BaseURL is the endpoint the SDK posts to. Empty means the SDK default
	// origin (api.openai.com) — native by definition.
	BaseURL string
	// Provider is the provider slug ("" when unknown); only OpenCode
	// families influence the route.
	Provider string
	// IsCodexBackend, IsXAIResponses and IsGitHubResponses are route flags
	// the caller derived from config/URL shape before the gate runs.
	IsCodexBackend    bool
	IsXAIResponses    bool
	IsGitHubResponses bool
}

// Host returns the lowercased hostname of a base URL, tolerating a bare
// “host[:port][/path]“ with no scheme (mirror of utils._parse_base_url).
// Empty string when no host can be parsed.
func Host(baseURL string) string {
	raw := strings.TrimSpace(baseURL)
	if raw == "" {
		return ""
	}
	if !strings.Contains(raw, "://") && !strings.HasPrefix(raw, "//") {
		raw = "//" + raw
	}
	parsed, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	return strings.ToLower(strings.TrimSuffix(parsed.Hostname(), "."))
}

// HostMatches reports whether host equals domain or is a subdomain of it.
// Safer than substring matching: “evil.com/openai.azure.com“ and
// “openai.azure.com.evil.com“ must both stay unmatched.
func HostMatches(host, domain string) bool {
	host = strings.ToLower(strings.TrimSuffix(strings.TrimSpace(host), "."))
	domain = strings.ToLower(strings.TrimSuffix(strings.TrimSpace(domain), "."))
	if host == "" || domain == "" {
		return false
	}
	return host == domain || strings.HasSuffix(host, "."+domain)
}

// IsAzureResponses reports whether the request targets an Azure Responses
// endpoint (per-resource subdomains included).
func (r RouteRequest) IsAzureResponses() bool {
	host := Host(r.BaseURL)
	for _, suffix := range AzureHostSuffixes {
		if HostMatches(host, suffix) {
			return true
		}
	}
	return false
}

// IsOpenCodeResponses reports whether the provider slug or the host identifies
// an OpenCode-backed route.
func (r RouteRequest) IsOpenCodeResponses() bool {
	if strings.HasPrefix(strings.ToLower(r.Provider), OpenCodeProviderPrefix) {
		return true
	}
	return Host(r.BaseURL) == OpenCodeHost
}

// IsPerplexityResponses reports whether the host is Perplexity's
// Responses-compatible Agent API origin.
func (r RouteRequest) IsPerplexityResponses() bool {
	return Host(r.BaseURL) == PerplexityHost
}

// SystemPromptAsInputItem reports whether the system prompt must ride as a
// leading “role: system“ input item with “instructions“ omitted, instead of
// the historical “instructions“ wire.
//
// True exactly for bridged (non-OpenAI-native) endpoints: a non-empty custom
// base URL that is not a known first-party Responses origin. Every first-party
// origin — OpenAI (including the SDK default), ChatGPT Codex, xAI, GitHub,
// Azure, OpenCode, Perplexity, Meta — keeps “instructions“ byte-for-byte;
// changing their wire would churn provider contracts that already cache
// correctly.
func SystemPromptAsInputItem(req RouteRequest) bool {
	if strings.TrimSpace(req.BaseURL) == "" {
		return false
	}
	if req.IsCodexBackend || req.IsXAIResponses || req.IsGitHubResponses {
		return false
	}
	if req.IsAzureResponses() || req.IsOpenCodeResponses() || req.IsPerplexityResponses() {
		return false
	}
	host := Host(req.BaseURL)
	if NativeResponsesHosts[host] || host == MetaHost {
		return false
	}
	return true
}

// WireItem is one input-array row of a Responses request.
type WireItem struct {
	Role    string
	Content string
}

// WirePayload is the normalized request shape the preflight produces:
// an optional instructions string plus the input rows.
type WirePayload struct {
	// Instructions is nil when the payload carries no instructions field
	// (bridged layout); a non-nil pointer to a possibly-empty string when
	// the field is present.
	Instructions *string
	Input        []WireItem
}

// SystemRowCount counts the input rows carrying role system. The bridged
// layout allows exactly one; more than one means a caller stacked the layout
// (the second row would capture the bridge's cache_control injection point
// and leave the real system prompt cache-cold).
func (p WirePayload) SystemRowCount() int {
	n := 0
	for _, item := range p.Input {
		if item.Role == "system" {
			n++
		}
	}
	return n
}

// HasLeadingSystem reports whether input[0] is a system row with non-blank
// content: the position the bridge's cache_control injection matches, and the
// non-blank rule that keeps the cacheable row from being an empty stub.
func (p WirePayload) HasLeadingSystem() bool {
	return len(p.Input) > 0 && p.Input[0].Role == "system" && strings.TrimSpace(p.Input[0].Content) != ""
}

// Preflight normalizes a raw payload the way agent/codex_responses_adapter.py
// does, enforcing the single-owner layout contract:
//
//   - A system row is legal ONLY at input[0] and must be non-blank.
//   - With a leading system row, an absent or empty instructions field is
//     dropped entirely — never swapped for a default identity, which would
//     emit a second system row (see SystemRowCount).
//   - Without a leading system row, instructions is required; an empty value
//     falls back to defaultIdentity (the historical native-route contract).
//
// Retry and fallback paths re-preflight already-normalized payloads, so the
// function must be idempotent.
func Preflight(payload WirePayload, defaultIdentity string) (WirePayload, error) {
	leading := payload.HasLeadingSystem()
	for idx, item := range payload.Input {
		if item.Role != "system" {
			continue
		}
		if idx != 0 {
			return WirePayload{}, fmt.Errorf("input[%d] system message is only legal as the leading item", idx)
		}
		if strings.TrimSpace(item.Content) == "" {
			return WirePayload{}, fmt.Errorf("input[%d] system message requires non-blank text content", idx)
		}
	}
	normalized := WirePayload{Input: append([]WireItem(nil), payload.Input...)}
	if leading {
		if payload.Instructions == nil || strings.TrimSpace(*payload.Instructions) == "" {
			// Dropped: the system prompt rides in the leading item.
			return normalized, nil
		}
	} else {
		if payload.Instructions == nil {
			return WirePayload{}, fmt.Errorf("request missing required field(s): instructions")
		}
		if strings.TrimSpace(*payload.Instructions) == "" {
			identity := defaultIdentity
			normalized.Instructions = &identity
			return normalized, nil
		}
	}
	if payload.Instructions != nil {
		instructions := *payload.Instructions
		normalized.Instructions = &instructions
	}
	return normalized, nil
}

// BridgedPrefixStable reports whether turn-after-turn payloads share the exact
// leading system row. Per-conversation prompt caching requires the wire prefix
// to be byte-stable: no per-turn content may land ahead of the system item.
func BridgedPrefixStable(first, second WirePayload) bool {
	if !first.HasLeadingSystem() || !second.HasLeadingSystem() {
		return false
	}
	return first.Input[0] == second.Input[0]
}
