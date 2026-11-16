// Package cronunreachable is the Go contract for the cron scheduler's
// model-unreachable recognition, mirroring cron/unreachable_retry.py in the
// Python tree. The Python cron scheduler still executes the Python copy;
// this package is the Go contract, the same arrangement as internal/moaroster
// — it is not imported by Python.
package cronunreachable

import "strings"

// transientFailureReasons holds the FailoverReason verdicts
// (agent/error_classifier.py) — and the identical codes the exit-reason table
// in agent/turn_failure_copy.py mints — meaning "transient provider/transport":
// the run never got a completed model response for a reason a re-run can fix
// (network blip, provider 5xx, stream TTFB stall). Every wall a re-run cannot
// move — rate_limit, billing, auth, content_policy_blocked, context_overflow… —
// deliberately stays out.
var transientFailureReasons = map[string]bool{
	"timeout":      true,
	"server_error": true,
	"overloaded":   true,
}

// exitFailure mirrors one row of _EXIT_REASON_FAILURES
// (agent/turn_failure_copy.py): a turn-loop exit-reason prefix, the
// failure_reason the finalizer maps it to, and that verdict's retryable flag.
type exitFailure struct {
	prefix    string
	reason    string
	retryable bool
}

// exitReasonFailures keeps the Python table's row order so prefix matching
// preserves its precedence. Prefix match: several reasons carry a
// parenthesised detail (local_processing_error(...)).
var exitReasonFailures = []exitFailure{
	{"empty_response_exhausted", "empty_response", true},
	{"all_retries_exhausted_no_response", "server_error", true},
	{"interpreter_shutdown", "interpreter_shutdown", false},
	{"local_processing_error", "loop_error", false},
	{"repeated_outer_errors", "loop_error", true},
	{"error_near_max_iterations", "loop_error", true},
	{"context_compression_timeout", "context_overflow", false},
	{"context_compression_exhausted", "context_overflow", false},
	{"ollama_runtime_context_too_small", "context_overflow", false},
	{"redirect_restart_limit_exceeded", "loop_error", true},
	{"rebuilt_restart_limit_exceeded", "loop_error", true},
}

// wrappedTransportNeedles holds the transport texts for the scheduler's
// RuntimeError(<error text>) shape: the transport exception TYPE and its
// httpx/httpcore/aiohttp module are gone, so the preflight matcher's raw
// exceptions never fire — only the prose needles do. Mirrors the classifier's
// transport family wording (agent/error_classifier.py) plus the Codex TTFB
// stall message (agent/chat_completion_nonstream.py). All lowercase: matched
// against case-folded prose.
var wrappedTransportNeedles = []string{
	"connection error",
	"connection refused",
	"connection reset",
	"connection closed",
	"server disconnected",
	"network is unreachable",
	"network connection lost",
	"temporary failure in name resolution",
	"failed to resolve",
	"timed out",
	"timeout",
	"ttfb",
}

// ModelUnreachable reports whether a failed scheduled fire never reached the
// model and a bounded re-run may be scheduled for it.
//
// apiCalls is the run's completed model-call count: a run that reached the
// model consumed spend and executed work, so it must never be auto-retried
// whatever the failure looks like. failureReason is the agent result's
// structured failure_reason and is authoritative when stamped — prose and the
// exit descriptor are ignored. hasRetryable/retryable carry the result's
// failure_retryable flag; an absent flag (hasRetryable false) means the
// classifier default, which treats transport/server errors as retryable.
// turnExitReason is the result's exit descriptor, consulted only when
// failureReason is empty and mapped through the same verdict table the
// finalizer stamps; an unknown exit reason is not a verdict and falls through
// to text matching. errorText is the failure prose — the scheduler raises it
// as a bare RuntimeError, so only its text survives — and is matched
// case-insensitively for transport needles when no structured verdict exists.
func ModelUnreachable(apiCalls int, failureReason string, hasRetryable, retryable bool, turnExitReason, errorText string) bool {
	if apiCalls > 0 {
		return false
	}
	if failureReason != "" {
		return transientReason(failureReason, hasRetryable, retryable)
	}
	if turnExitReason != "" {
		for _, row := range exitReasonFailures {
			if strings.HasPrefix(turnExitReason, row.prefix) {
				return transientReason(row.reason, true, row.retryable)
			}
		}
		// Unknown exit reason: not a verdict, fall through to the prose.
	}
	return matchesTransportNeedle(errorText)
}

// transientReason applies the reason rule shared by both structured paths:
// only transient reasons qualify, an explicit retryable=false veto wins, and
// a missing retry flag defaults to retryable.
func transientReason(reason string, hasRetryable, retryable bool) bool {
	if !transientFailureReasons[reason] {
		return false
	}
	if !hasRetryable {
		return true
	}
	return retryable
}

// matchesTransportNeedle reports whether the case-folded error prose carries
// any transport needle.
func matchesTransportNeedle(errorText string) bool {
	msg := strings.ToLower(errorText)
	for _, needle := range wrappedTransportNeedles {
		if strings.Contains(msg, needle) {
			return true
		}
	}
	return false
}
