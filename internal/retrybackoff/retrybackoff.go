// Package retrybackoff is the Go twin of the retry/backoff decision core in
// agent/retry_utils.py plus the wait-and-surface choice of
// agent/turn_recovery.py::compute_error_backoff. Every API retry — the
// conversation loop, the credential pool's cooldown, batch runs — must agree
// on how long to wait (Retry-After first, jittered exponential backoff
// otherwise, the adaptive Z.AI overload schedule for coding-plan 429s) and
// on whether the wait surfaces to the user immediately or stays buffered
// until every retry fails. This is the canonical statement of that
// contract; the Python module is what the runtime executes (deployments
// carry no Go toolchain, the same arrangement as every other internal/*
// twin). cmd/retrybackoff is the executable form: the Python contract test
// (tests/agent/test_retry_backoff_go_contract.py) builds it and asserts
// identical answers for every shared-table record in
// testdata/retrybackoff_cases.json; the Go tests run the same table as a
// second cross-check. Production Python never shells out to it.
//
// Contract (shared with the Python twin):
//
//   - parse_retry_after_seconds accepts a numeric value, a numeric string,
//     a headers mapping (both casings tried), or an RFC 7231 HTTP-date; the
//     result is clamped at 0.0, None when absent/unparseable. Bools are
//     None (never coerced to 1.0).
//   - compute_error_backoff respects Retry-After on EVERY retryable error,
//     not just 429s: header first, then the structured body field
//     (error.retry_after nested, or top-level retry_after), capped at 600s
//     (Anthropic Tier 1 buckets reset in ~171s, so a 120s cap re-tripped
//     the limit, #26293). A zero/expired cooldown is treated as absent so
//     the loop never hot-loops the provider.
//   - jittered_backoff is min(base·2^(attempt-1), max) plus uniform jitter
//     in [0, jitter_ratio·delay]; attempt is 1-based, exponent saturates at
//     63, a non-positive base saturates at max_delay. The twin pins the
//     deterministic base and the jitter bounds (the draw itself is random).
//   - The Z.AI Coding GLM-5.2 overload shape is narrow on purpose (429 +
//     code 1305 / "temporarily overloaded" + the coding endpoint + glm-5.2)
//     so ordinary quota 429s still fail fast. Adaptive policy: short
//     retries keep the default wait, then 30→60→90→120s with light jitter,
//     index-clamped at the last entry.
//   - Surface rule: adaptive waits surface only on the long Z.AI policy;
//     non-adaptive waits surface only when the applied Retry-After exceeds
//     60s (a 5xx Retry-After can reach the 600s cap; buffering that would
//     leave the user silent for minutes). Everything else buffers.
//   - zai_coding_overload_retry_ceiling = short_attempts + 4 + 1: the loop
//     gives up when retry_count >= ceiling BEFORE computing that attempt's
//     backoff.
//   - Free-text reset grammars are tried in table order — quotaResetDelay
//     (ms|s) beats an explicit "retry after Ns", which beats the
//     resets_in_seconds field, which beats "resets in 4hr 5min": a body
//     carrying both describes a short throttle inside a long quota window.
package retrybackoff

import (
	"math"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// ZaiLongBackoff mirrors _ZAI_CODING_OVERLOAD_LONG_BACKOFF; ZaiShortAttempts
// mirrors _ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS (shared by the adaptive policy
// and the retry ceiling so the two cannot silently desync).
var ZaiLongBackoff = []float64{30, 60, 90, 120}

const ZaiShortAttempts = 3

// ZaiCodingEndpoint is the substring the matcher requires in base_url.
const ZaiCodingEndpoint = "api.z.ai/api/coding/paas/v4"

// ZaiModel is the substring the matcher requires in model.
const ZaiModel = "glm-5.2"

// RetryAfterCap is the compute_error_backoff ceiling on Retry-After (#26293).
const RetryAfterCap = 600.0

// DefaultBaseDelay / DefaultMaxDelay mirror compute_error_backoff's defaults.
const (
	DefaultBaseDelay = 2.0
	DefaultMaxDelay  = 60.0
)

// Surface values for ComputeWaitSurface.
const (
	SurfaceEmit   = "emit"
	SurfaceBuffer = "buffer"
)

// ParseRetryAfter mirrors parse_retry_after_seconds against a fixed clock
// (the caller supplies now; the Python twin pins datetime under test). raw
// may be nil, bool, a JSON number, a string, or a headers mapping.
func ParseRetryAfter(raw any, now time.Time) *float64 {
	switch t := raw.(type) {
	case nil:
		return nil
	case bool:
		return nil
	case float64:
		return clampNonNegative(t)
	case int:
		return clampNonNegative(float64(t))
	case map[string]any:
		v, ok := t["Retry-After"]
		if !ok || v == nil {
			v = t["retry-after"]
		}
		return ParseRetryAfter(v, now)
	case string:
		return parseRetryAfterString(t, now)
	default:
		return nil
	}
}

func clampNonNegative(v float64) *float64 {
	if v < 0 {
		v = 0
	}
	return &v
}

func parseRetryAfterString(text string, now time.Time) *float64 {
	text = strings.TrimSpace(text)
	if text == "" {
		return nil
	}
	if f, err := strconv.ParseFloat(text, 64); err == nil {
		return clampNonNegative(f)
	}
	// RFC 7231 HTTP-date: seconds until that instant, clamped at 0.
	when, ok := parseHTTPDate(text)
	if !ok {
		return nil
	}
	return clampNonNegative(when.Sub(now).Seconds())
}

// parseHTTPDate covers the RFC 2822 IMF-fixdate forms
// parsedate_to_datetime accepts for Retry-After values ("...GMT" and
// numeric offsets).
func parseHTTPDate(text string) (time.Time, bool) {
	for _, layout := range []string{time.RFC1123, time.RFC1123Z} {
		if when, err := time.Parse(layout, text); err == nil {
			return when.UTC(), true
		}
	}
	return time.Time{}, false
}

// JitteredBase mirrors the deterministic core of jittered_backoff (the
// Python twin reads it with jitter_ratio=0, where the draw is exactly the
// base). attempt is 1-based.
func JitteredBase(attempt int, baseDelay, maxDelay float64) float64 {
	exponent := attempt - 1
	if exponent < 0 {
		exponent = 0
	}
	if exponent >= 63 || baseDelay <= 0 {
		return maxDelay
	}
	return math.Min(baseDelay*math.Pow(2, float64(exponent)), maxDelay)
}

// JitterBounds mirrors the envelope of jittered_backoff's uniform draw:
// [base, base·(1+jitter_ratio)].
func JitterBounds(attempt int, baseDelay, maxDelay, jitterRatio float64) (lo, hi float64) {
	lo = JitteredBase(attempt, baseDelay, maxDelay)
	return lo, lo * (1 + jitterRatio)
}

// ZaiOverload mirrors is_zai_coding_overload_error against the flattened
// error text: 429 + the coding endpoint + glm-5.2 + (code 1305 or
// "temporarily overloaded").
func ZaiOverload(baseURL, model string, statusCode int, text string) bool {
	return statusCode == 429 &&
		strings.Contains(strings.ToLower(baseURL), ZaiCodingEndpoint) &&
		strings.Contains(strings.ToLower(model), ZaiModel) &&
		(strings.Contains(text, "1305") || strings.Contains(text, "temporarily overloaded"))
}

// AdaptivePolicy mirrors adaptive_rate_limit_backoff with the jitter draw
// pinned to its base (the Python contract patches jittered_backoff to the
// identity for these rows). attempt is 1-based; empty policy means "not a
// Z.AI coding overload: keep the default wait".
func AdaptivePolicy(attempt int, zai bool, shortAttempts int, defaultWait float64) (wait float64, policy string) {
	if !zai {
		return defaultWait, ""
	}
	if attempt <= shortAttempts {
		return defaultWait, "zai_coding_overload_short"
	}
	idx := attempt - shortAttempts - 1
	if idx > len(ZaiLongBackoff)-1 {
		idx = len(ZaiLongBackoff) - 1
	}
	return ZaiLongBackoff[idx], "zai_coding_overload_long"
}

// ZaiCeiling mirrors zai_coding_overload_retry_ceiling: one past the last
// long entry, because the loop gives up when retry_count >= ceiling BEFORE
// computing the attempt's backoff.
func ZaiCeiling(shortAttempts int) int {
	return shortAttempts + len(ZaiLongBackoff) + 1
}

// FormatResetWindow mirrors format_reset_window: "~9h" / "~45 min" for chat
// copy naming when a quota window reopens (ceilinged).
func FormatResetWindow(seconds float64) string {
	s := int(seconds)
	if s >= 3600 {
		return "~" + strconv.Itoa((s+3599)/3600) + "h"
	}
	return "~" + strconv.Itoa((s+59)/60) + " min"
}
func ComputeWaitSurface(retryCount int, retryAfter *float64, rateLimited, zai bool) (wait float64, surface string) {
	applied := applyRetryAfter(retryAfter)
	if applied != nil {
		wait = *applied
	} else {
		wait = JitteredBase(retryCount, DefaultBaseDelay, DefaultMaxDelay)
	}
	adaptive := rateLimited || zai
	policy := ""
	if adaptive && applied == nil {
		wait, policy = AdaptivePolicy(retryCount, zai, ZaiShortAttempts, wait)
	}
	if adaptive {
		// Adaptive waits surface only on the long Z.AI policy — a long
		// Retry-After inside an adaptive turn still buffers (the Python
		// twin pins this asymmetry on purpose).
		if policy == "zai_coding_overload_long" {
			return wait, SurfaceEmit
		}
		return wait, SurfaceBuffer
	}
	if applied != nil && *applied > 60 {
		// A 5xx Retry-After can reach the 600s cap; buffering that wait
		// would leave the user silent for minutes.
		return wait, SurfaceEmit
	}
	return wait, SurfaceBuffer
}

var (
	quotaResetDelayRe = regexp.MustCompile(`(?i)quotaResetDelay[:\s"]+(\d+(?:\.\d+)?)(ms|s)`)
	resetsInRe        = regexp.MustCompile(`(?i)resets?\s+in\s+` +
		`(?:(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b\s*)?` +
		`(?:(\d+(?:\.\d+)?)\s*(?:m|min|mins|minute|minutes)\b\s*)?` +
		`(?:(\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)\b)?`)
	retryAfterSecondsRe  = regexp.MustCompile(`(?i)retry\s+(?:after\s+)?(\d+(?:\.\d+)?)\s*(?:sec|secs|seconds|s\b)`)
	resetsInSecondsField = regexp.MustCompile(`(?i)resets_in_seconds\W{1,4}(\d+(?:\.\d+)?)`)
)

// ResetDelayFromMessage mirrors reset_delay_from_message: the first grammar
// in table order that yields a value wins (quotaResetDelay beats an
// explicit "retry after Ns" — a body carrying both describes a short
// throttle inside a long quota window).
func ResetDelayFromMessage(message string) *float64 {
	if message == "" {
		return nil
	}
	if m := quotaResetDelayRe.FindStringSubmatch(message); m != nil {
		v, _ := strconv.ParseFloat(m[1], 64)
		if strings.EqualFold(m[2], "ms") {
			v /= 1000
		}
		return &v
	}
	if m := retryAfterSecondsRe.FindStringSubmatch(message); m != nil {
		v, _ := strconv.ParseFloat(m[1], 64)
		return &v
	}
	if m := resetsInSecondsField.FindStringSubmatch(message); m != nil {
		v, _ := strconv.ParseFloat(m[1], 64)
		return &v
	}
	if m := resetsInRe.FindStringSubmatch(message); m != nil {
		if m[1] == "" && m[2] == "" && m[3] == "" {
			return nil // "resets in" with no unit-bearing number
		}
		total := 0.0
		for i, mult := range []float64{3600, 60, 1} {
			if m[i+1] != "" {
				v, _ := strconv.ParseFloat(m[i+1], 64)
				total += v * mult
			}
		}
		return &total
	}
	return nil
}

// applyRetryAfter mirrors the consumer's post-processing: cap at 600s, and a
// zero/expired cooldown is treated as absent (never hot-loop the provider).
func applyRetryAfter(ra *float64) *float64 {
	if ra == nil {
		return nil
	}
	v := math.Min(*ra, RetryAfterCap)
	if v <= 0 {
		return nil
	}
	return &v
}
