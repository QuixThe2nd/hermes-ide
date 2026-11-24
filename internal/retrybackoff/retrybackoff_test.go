package retrybackoff

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

var fixedNow = time.Date(2026, time.November, 24, 12, 0, 0, 0, time.UTC)

type sharedCase map[string]any

func loadSharedCases(t *testing.T) []sharedCase {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", "retrybackoff_cases.json"))
	if err != nil {
		t.Fatalf("read shared table: %v", err)
	}
	var doc struct {
		Cases []sharedCase `json:"cases"`
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&doc); err != nil {
		t.Fatalf("decode shared table: %v", err)
	}
	return doc.Cases
}

func str(v any) string {
	s, _ := v.(string)
	return s
}

func intOf(t *testing.T, v any, def int) int {
	t.Helper()
	n, ok := v.(json.Number)
	if !ok {
		return def
	}
	i, err := n.Int64()
	if err != nil {
		return def
	}
	return int(i)
}

func floatOf(t *testing.T, v any) (float64, bool) {
	t.Helper()
	n, ok := v.(json.Number)
	if !ok {
		return 0, false
	}
	f, err := n.Float64()
	if err != nil {
		return 0, false
	}
	return f, true
}

func boolOf(v any) bool {
	b, _ := v.(bool)
	return b
}

// TestSharedTableMatchesPythonExpectations runs the same
// testdata/retrybackoff_cases.json the Python contract test drives: every
// case's expect was computed by the real Python implementation (datetime
// pinned, the jitter draw patched to its base for the deterministic ops), so
// a failure here means the Go twin drifted from the runtime contract.
func TestSharedTableMatchesPythonExpectations(t *testing.T) {
	for _, c := range loadSharedCases(t) {
		name := str(c["name"])
		t.Run(name, func(t *testing.T) {
			answer := goAnswer(t, c)
			if !jsonEqual(answer, c["expect"]) {
				t.Fatalf("go answer ≠ python expect\n go:   %#v\n want: %#v", answer, c["expect"])
			}
		})
	}
}

func goAnswer(t *testing.T, c sharedCase) any {
	switch op := str(c["op"]); op {
	case "parse_retry_after":
		wait := ParseRetryAfter(normalize(c["raw"]), fixedNow)
		if wait == nil {
			return map[string]any{"wait": nil}
		}
		return map[string]any{"wait": *wait}
	case "jittered_base":
		base, _ := floatOf(t, c["base_delay"])
		max, _ := floatOf(t, c["max_delay"])
		return map[string]any{"base": JitteredBase(intOf(t, c["attempt"], 1), base, max)}
	case "jitter_bounds":
		base, _ := floatOf(t, c["base_delay"])
		max, _ := floatOf(t, c["max_delay"])
		ratio, _ := floatOf(t, c["jitter_ratio"])
		lo, hi := JitterBounds(intOf(t, c["attempt"], 1), base, max, ratio)
		return map[string]any{"lo": lo, "hi": hi}
	case "zai_overload":
		status := 0
		if n, ok := c["status_code"].(json.Number); ok {
			if v, err := n.Int64(); err == nil {
				status = int(v)
			}
		}
		return map[string]any{"overload": ZaiOverload(str(c["base_url"]), str(c["model"]), status, str(c["text"]))}
	case "adaptive_policy":
		wait, policy := AdaptivePolicy(
			intOf(t, c["attempt"], 1), boolOf(c["zai"]), intOf(t, c["short_attempts"], ZaiShortAttempts), numOr(t, c, "default_wait"),
		)
		var policyOut any
		if policy != "" {
			policyOut = policy
		}
		return map[string]any{"wait": wait, "policy": policyOut}
	case "zai_ceiling":
		return map[string]any{"ceiling": ZaiCeiling(intOf(t, c["short_attempts"], ZaiShortAttempts))}
	case "format_reset_window":
		seconds, _ := floatOf(t, c["seconds"])
		return map[string]any{"window": FormatResetWindow(seconds)}
	case "reset_delay_from_message":
		seconds := ResetDelayFromMessage(str(c["message"]))
		if seconds == nil {
			return map[string]any{"seconds": nil}
		}
		return map[string]any{"seconds": *seconds}
	case "surface":
		var ra *float64
		if v, ok := floatOf(t, c["retry_after"]); ok {
			ra = &v
		}
		wait, surface := ComputeWaitSurface(intOf(t, c["retry_count"], 1), ra, boolOf(c["is_rate_limited"]), boolOf(c["zai"]))
		return map[string]any{"wait": wait, "surface": surface}
	default:
		t.Fatalf("unknown op %q", op)
		return nil
	}
}

func numOr(t *testing.T, c sharedCase, k string) float64 {
	t.Helper()
	if v, ok := floatOf(t, c[k]); ok {
		return v
	}
	return 0
}

func jsonEqual(a, b any) bool {
	ab, err := json.Marshal(canonical(a))
	if err != nil {
		return false
	}
	bb, err := json.Marshal(canonical(b))
	if err != nil {
		return false
	}
	return bytes.Equal(ab, bb)
}

func canonical(v any) any {
	switch t := v.(type) {
	case json.Number:
		if f, err := t.Float64(); err == nil {
			return f
		}
		return t.String()
	case float64:
		return t
	case map[string]any:
		for k, item := range t {
			t[k] = canonical(item)
		}
		return t
	case []any:
		for i, item := range t {
			t[i] = canonical(item)
		}
		return t
	default:
		return v
	}
}

// TestQuotaGrammarBeatsExplicitRetryAfter pins the credential-pool
// precedence: a body carrying both describes a short throttle inside a long
// quota window, and the quota grammar is tried first.
func TestQuotaGrammarBeatsExplicitRetryAfter(t *testing.T) {
	v := ResetDelayFromMessage("quotaResetDelay: 200ms; retry after 30s")
	if v == nil || *v != 0.2 {
		t.Fatalf("quotaResetDelay must win, got %v", v)
	}
	v = ResetDelayFromMessage("retry after 30s; usage resets in 4hr")
	if v == nil || *v != 30 {
		t.Fatalf("explicit retry-after must beat resets-in, got %v", v)
	}
}

// TestRetryAfterCapAndZeroClamp pins the consumer's post-processing: 600s
// cap, and a zero/expired cooldown treated as absent.
func TestRetryAfterCapAndZeroClamp(t *testing.T) {
	seven := 700.0
	wait, surface := ComputeWaitSurface(1, &seven, false, false)
	if wait != 600 || surface != SurfaceEmit {
		t.Fatalf("700s Retry-After must cap to 600s and emit, got %v/%v", wait, surface)
	}
	zero := 0.0
	wait, surface = ComputeWaitSurface(2, &zero, false, false)
	if wait != JitteredBase(2, DefaultBaseDelay, DefaultMaxDelay) || surface != SurfaceBuffer {
		t.Fatalf("zero Retry-After must fall back to jittered backoff, got %v/%v", wait, surface)
	}
}

// TestAdaptiveLongSurfacesAndNothingElse pins the surface asymmetry: only
// the long Z.AI policy emits; an adaptive turn with a long Retry-After
// still buffers (the table pins the same, this states it as a law).
func TestAdaptiveLongSurfacesAndNothingElse(t *testing.T) {
	ra := 200.0
	if _, surface := ComputeWaitSurface(1, &ra, true, false); surface != SurfaceBuffer {
		t.Fatal("adaptive with Retry-After must buffer")
	}
	if _, surface := ComputeWaitSurface(4, nil, false, true); surface != SurfaceEmit {
		t.Fatal("long Z.AI policy must emit")
	}
	if _, surface := ComputeWaitSurface(2, nil, false, true); surface != SurfaceBuffer {
		t.Fatal("short Z.AI policy must buffer")
	}
	if _, surface := ComputeWaitSurface(1, nil, false, false); surface != SurfaceBuffer {
		t.Fatal("plain retry must buffer")
	}
}

// TestCeilingCoversTheFullSchedule pins the relationship between the retry
// ceiling and the long schedule: the last long entry is attempt
// ceiling-1, and one past it the loop has already given up.
func TestCeilingCoversTheFullSchedule(t *testing.T) {
	ceiling := ZaiCeiling(ZaiShortAttempts)
	if ceiling != ZaiShortAttempts+len(ZaiLongBackoff)+1 {
		t.Fatalf("ceiling = %d", ceiling)
	}
	wait, _ := AdaptivePolicy(ceiling-1, true, ZaiShortAttempts, 2)
	if wait != ZaiLongBackoff[len(ZaiLongBackoff)-1] {
		t.Fatalf("attempt ceiling-1 must sit on the last long entry, got %v", wait)
	}
}

// TestJitterEnvelopeContainsBase pins that the base is the envelope floor
// and the ratio scales the ceiling (the Python twin draws uniformly inside
// this envelope; the twin states it, the contract test exercises the draw).
func TestJitterEnvelopeContainsBase(t *testing.T) {
	lo, hi := JitterBounds(3, 5, 120, 0.5)
	if lo != 20 || hi != 30 {
		t.Fatalf("envelope = [%v, %v], want [20, 30]", lo, hi)
	}
}

// TestBoolAndGarbageRetryAfterRefuse pins the parser's refusals: bools are
// not numbers, garbage is not an HTTP-date.
func TestBoolAndGarbageRetryAfterRefuse(t *testing.T) {
	if v := ParseRetryAfter(true, fixedNow); v != nil {
		t.Fatalf("bool must parse to nil, got %v", *v)
	}
	if v := ParseRetryAfter("abc", fixedNow); v != nil {
		t.Fatalf("garbage must parse to nil, got %v", *v)
	}
	if v := ParseRetryAfter("Wed, 21 Oct 2015 07:28:00 GMT", fixedNow); v == nil || *v != 0 {
		t.Fatalf("past HTTP-date must clamp to 0, got %v", v)
	}
}

// normalize converts decoded JSON numbers (json.Number under UseNumber) to
// float64 so the twin's plain-Go signatures see them.
func normalize(v any) any {
	switch t := v.(type) {
	case json.Number:
		if f, err := t.Float64(); err == nil {
			return f
		}
		return t.String()
	case map[string]any:
		for k, item := range t {
			t[k] = normalize(item)
		}
		return t
	default:
		return v
	}
}
