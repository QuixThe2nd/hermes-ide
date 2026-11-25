// Command retrybackoff is the executable form of the internal/retrybackoff
// Go twin: read one shared-table case (a JSON object) on stdin, print the
// twin's answer as a JSON object on stdout. Exit codes: 0 answer, 2 bad
// case/op. The Python contract test (tests/agent/test_retry_backoff_go_contract.py)
// builds and drives it; production Python never shells out to it.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"time"

	"github.com/QuixThe2nd/hermes-ide/internal/retrybackoff"
)

// fixedNow is the deterministic clock both engines evaluate HTTP-date
// Retry-After values against (the Python twin patches datetime).
var fixedNow = time.Date(2026, time.November, 24, 12, 0, 0, 0, time.UTC)

func main() {
	var c map[string]any
	dec := json.NewDecoder(os.Stdin)
	dec.UseNumber()
	if err := dec.Decode(&c); err != nil {
		fmt.Fprintf(os.Stderr, "bad case json: %v\n", err)
		os.Exit(2)
	}
	answer, err := answer(c)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	out, err := json.Marshal(answer)
	if err != nil {
		fmt.Fprintf(os.Stderr, "marshal answer: %v\n", err)
		os.Exit(2)
	}
	fmt.Println(string(out))
}

func answer(c map[string]any) (map[string]any, error) {
	str := func(k string) string {
		v, _ := c[k].(string)
		return v
	}
	intOf := func(k string, def int) int {
		if n, ok := c[k].(json.Number); ok {
			if v, err := n.Int64(); err == nil {
				return int(v)
			}
		}
		return def
	}
	floatOf := func(k string) (float64, bool) {
		if n, ok := c[k].(json.Number); ok {
			if v, err := n.Float64(); err == nil {
				return v, true
			}
		}
		return 0, false
	}
	floatPtr := func(k string) *float64 {
		if v, ok := floatOf(k); ok {
			return &v
		}
		return nil
	}
	boolOf := func(k string) bool {
		v, _ := c[k].(bool)
		return v
	}

	switch op := str("op"); op {
	case "parse_retry_after":
		wait := retrybackoff.ParseRetryAfter(normalize(c["raw"]), fixedNow)
		if wait == nil {
			return map[string]any{"wait": nil}, nil
		}
		return map[string]any{"wait": *wait}, nil

	case "jittered_base":
		base, _ := floatOf("base_delay")
		max, _ := floatOf("max_delay")
		return map[string]any{"base": retrybackoff.JitteredBase(intOf("attempt", 1), base, max)}, nil

	case "jitter_bounds":
		base, _ := floatOf("base_delay")
		max, _ := floatOf("max_delay")
		ratio, _ := floatOf("jitter_ratio")
		lo, hi := retrybackoff.JitterBounds(intOf("attempt", 1), base, max, ratio)
		return map[string]any{"lo": lo, "hi": hi}, nil

	case "zai_overload":
		status := 0
		if n, ok := c["status_code"].(json.Number); ok {
			if v, err := n.Int64(); err == nil {
				status = int(v)
			}
		}
		return map[string]any{"overload": retrybackoff.ZaiOverload(
			str("base_url"), str("model"), status, str("text"),
		)}, nil

	case "adaptive_policy":
		wait, policy := retrybackoff.AdaptivePolicy(
			intOf("attempt", 1), boolOf("zai"), intOf("short_attempts", retrybackoff.ZaiShortAttempts),
			valOr(c, "default_wait", 0),
		)
		var policyOut any
		if policy != "" {
			policyOut = policy
		}
		return map[string]any{"wait": wait, "policy": policyOut}, nil

	case "zai_ceiling":
		return map[string]any{"ceiling": retrybackoff.ZaiCeiling(intOf("short_attempts", retrybackoff.ZaiShortAttempts))}, nil

	case "format_reset_window":
		seconds, _ := floatOf("seconds")
		return map[string]any{"window": retrybackoff.FormatResetWindow(seconds)}, nil

	case "reset_delay_from_message":
		seconds := retrybackoff.ResetDelayFromMessage(str("message"))
		if seconds == nil {
			return map[string]any{"seconds": nil}, nil
		}
		return map[string]any{"seconds": *seconds}, nil

	case "surface":
		wait, surface := retrybackoff.ComputeWaitSurface(
			intOf("retry_count", 1), floatPtr("retry_after"), boolOf("is_rate_limited"), boolOf("zai"),
		)
		return map[string]any{"wait": wait, "surface": surface}, nil

	default:
		return nil, fmt.Errorf("unknown op %q", op)
	}
}

func valOr(c map[string]any, k string, def float64) float64 {
	if n, ok := c[k].(json.Number); ok {
		if v, err := n.Float64(); err == nil {
			return v
		}
	}
	return def
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
