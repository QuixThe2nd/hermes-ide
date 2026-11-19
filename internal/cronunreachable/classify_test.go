package cronunreachable

import (
	"strings"
	"testing"
)

// TestModelUnreachable walks the full contract: the reached-model veto, the
// structured failure_reason verdict, the turn-exit-reason mapping, and the
// transport-prose fallback — including every negative the Python copy
// (cron/unreachable_retry.py) pins down.
func TestModelUnreachable(t *testing.T) {
	tests := []struct {
		name         string
		apiCalls     int
		reason       string
		hasRetryable bool
		retryable    bool
		exit         string
		text         string
		want         bool
	}{
		// A run that reached the model must never be auto-retried, whatever
		// the rest of the fields claim.
		{
			name:     "model reached: transport prose ignored",
			apiCalls: 1, text: "Connection error.", want: false,
		},
		{
			name:     "model reached: transient reason ignored",
			apiCalls: 2, reason: "timeout", want: false,
		},
		{
			name:     "model reached: retryable exit reason ignored",
			apiCalls: 1, exit: "all_retries_exhausted_no_response", want: false,
		},
		{
			name:     "model reached: everything transient at once",
			apiCalls: 3, reason: "server_error", exit: "all_retries_exhausted_no_response",
			text: "Codex stream produced no parsed stream event within 120s (TTFB threshold: 120s)", want: false,
		},

		// failure_reason is authoritative: transient reasons with the
		// retryable flag matrix. An absent flag defaults to retryable.
		{
			name: "timeout without retry flag", reason: "timeout", want: true,
		},
		{
			name: "server_error without retry flag", reason: "server_error", want: true,
		},
		{
			name: "overloaded without retry flag", reason: "overloaded", want: true,
		},
		{
			name: "timeout explicitly retryable", reason: "timeout", hasRetryable: true, retryable: true, want: true,
		},
		{
			name: "server_error explicitly retryable", reason: "server_error", hasRetryable: true, retryable: true, want: true,
		},
		{
			name: "overloaded explicitly retryable", reason: "overloaded", hasRetryable: true, retryable: true, want: true,
		},
		{
			name: "timeout with retryable=false", reason: "timeout", hasRetryable: true, retryable: false, want: false,
		},
		{
			name: "server_error with retryable=false", reason: "server_error", hasRetryable: true, retryable: false, want: false,
		},
		{
			name: "overloaded with retryable=false", reason: "overloaded", hasRetryable: true, retryable: false, want: false,
		},

		// Every wall a re-run cannot move returns false even with no retry
		// flag: the reason alone disqualifies.
		{
			name: "rate_limit", reason: "rate_limit", want: false,
		},
		{
			name: "billing", reason: "billing", want: false,
		},
		{
			name: "auth", reason: "auth", want: false,
		},
		{
			name: "auth_permanent", reason: "auth_permanent", want: false,
		},
		{
			name: "content_policy_blocked", reason: "content_policy_blocked", want: false,
		},
		{
			name: "context_overflow", reason: "context_overflow", want: false,
		},
		{
			name: "model_not_found", reason: "model_not_found", want: false,
		},
		{
			name: "format_error", reason: "format_error", want: false,
		},
		{
			name: "empty_response", reason: "empty_response", want: false,
		},
		{
			name: "loop_error", reason: "loop_error", want: false,
		},
		{
			name: "interpreter_shutdown", reason: "interpreter_shutdown", want: false,
		},
		{
			name: "invalid_response", reason: "invalid_response", want: false,
		},

		// Structured verdict wins over prose: transport needles in the error
		// text must not rescue a non-transient reason.
		{
			name:   "rate_limit beats connection-error prose",
			reason: "rate_limit", text: "Connection error.", want: false,
		},
		{
			name:   "billing beats timed-out prose",
			reason: "billing", text: "Read timed out (read timeout=120s)", want: false,
		},
		{
			name:   "auth beats TTFB prose",
			reason: "auth", text: "Codex stream produced no parsed stream event within 120s (TTFB threshold: 120s)", want: false,
		},
		{
			name:   "context_overflow beats refused prose",
			reason: "context_overflow", text: "connection refused by upstream", want: false,
		},

		// failure_reason also wins over the exit descriptor: the same mapped
		// code would have said otherwise, but the stamped reason governs.
		{
			name:   "stamped timeout beats context-overflow exit",
			reason: "timeout", exit: "context_compression_timeout", want: true,
		},
		{
			name:   "stamped empty_response beats transient-looking exit",
			reason: "empty_response", exit: "all_retries_exhausted_no_response", want: false,
		},

		// turn_exit_reason mapping (failure_reason empty): only
		// all_retries_exhausted_no_response lands on a transient code, and
		// its table row is retryable, so it is the sole true verdict.
		{
			name: "all_retries_exhausted_no_response", exit: "all_retries_exhausted_no_response", want: true,
		},
		{
			name: "all_retries_exhausted_no_response with detail",
			exit: "all_retries_exhausted_no_response (5 upstream attempts)", want: true,
		},
		{
			name: "empty_response_exhausted not transient", exit: "empty_response_exhausted", want: false,
		},
		{
			name: "interpreter_shutdown not transient", exit: "interpreter_shutdown", want: false,
		},
		{
			name: "local_processing_error maps to loop_error", exit: "local_processing_error", want: false,
		},
		{
			name: "local_processing_error with parenthesised detail",
			exit: "local_processing_error(retry wrapper failed)", want: false,
		},
		{
			name: "repeated_outer_errors maps to loop_error", exit: "repeated_outer_errors", want: false,
		},
		{
			name: "error_near_max_iterations maps to loop_error", exit: "error_near_max_iterations", want: false,
		},
		{
			name: "context_compression_timeout maps to context_overflow", exit: "context_compression_timeout", want: false,
		},
		{
			name: "context_compression_exhausted maps to context_overflow", exit: "context_compression_exhausted", want: false,
		},
		{
			name: "ollama_runtime_context_too_small maps to context_overflow", exit: "ollama_runtime_context_too_small", want: false,
		},
		{
			name: "redirect_restart_limit_exceeded maps to loop_error", exit: "redirect_restart_limit_exceeded", want: false,
		},
		{
			name: "rebuilt_restart_limit_exceeded maps to loop_error", exit: "rebuilt_restart_limit_exceeded", want: false,
		},

		// The mapped row's retryable flag governs the exit path; the
		// caller's hasRetryable/retryable pair belongs to failure_reason only.
		{
			name: "exit path ignores retryable=false argument",
			exit: "all_retries_exhausted_no_response", hasRetryable: true, retryable: false, want: true,
		},

		// Unknown exit reason is not a verdict: fall through to text match.
		{
			name: "unknown exit reason with no text", exit: "something_the_table_never_saw", want: false,
		},
		{
			name: "unknown exit reason falls through to transport prose",
			exit: "novel_exit_reason", text: "connection reset by peer", want: true,
		},
		{
			name: "exit reasons match case-sensitively like Python startswith",
			exit: "ALL_RETRIES_EXHAUSTED_NO_RESPONSE", want: false,
		},

		// Transport-prose fallback, no verdict at all. Case-insensitive.
		{
			name: "codex TTFB stall at zero calls",
			text: "Codex stream produced no parsed stream event within 120s (TTFB threshold: 120s)", want: true,
		},
		{
			name: "lowercase connection error", text: "connection error", want: true,
		},
		{
			name: "shouted connection reset", text: "CONNECTION RESET BY PEER", want: true,
		},
		{
			name: "mixed case network unreachable", text: "Network Is Unreachable", want: true,
		},
		{
			name: "read timed out is not the timeout needle word", text: "Read timed out", want: true,
		},
		{
			name: "request timeout", text: "request timeout after 30s", want: true,
		},
		{
			name: "dns resolution failure", text: "temporary failure in name resolution for api.example.com", want: true,
		},
		{
			name: "provider 500 without transport wording", text: "POST https://api.example.com/v1/chat: 500 internal server error", want: false,
		},
		{
			name: "model rejected the request", text: "model returned 400 bad request: invalid schema", want: false,
		},
		{
			name: "empty everything", want: false,
		},
		{
			name: "empty text with transient-looking exit absent", text: "", want: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := ModelUnreachable(tt.apiCalls, tt.reason, tt.hasRetryable, tt.retryable, tt.exit, tt.text)
			if got != tt.want {
				t.Errorf("ModelUnreachable(%d, %q, %t, %t, %q, %q) = %t, want %t",
					tt.apiCalls, tt.reason, tt.hasRetryable, tt.retryable, tt.exit, tt.text, got, tt.want)
			}
		})
	}
}

// TestEveryTransportNeedleMatches pins the needle list itself: each needle
// must fire both embedded in prose and fully uppercased, proving the match is
// a case-insensitive substring test.
func TestEveryTransportNeedleMatches(t *testing.T) {
	for _, needle := range wrappedTransportNeedles {
		t.Run(needle, func(t *testing.T) {
			embedded := "upstream said " + needle + " while streaming"
			if !ModelUnreachable(0, "", false, false, "", embedded) {
				t.Errorf("embedded needle %q did not match %q", needle, embedded)
			}
			shouted := strings.ToUpper("prefix " + needle + " suffix")
			if !ModelUnreachable(0, "", false, false, "", shouted) {
				t.Errorf("uppercased needle %q did not match %q", needle, shouted)
			}
		})
	}
}

// TestEveryExitReasonRowMapped asserts the table exhaustively from the
// consumer side: every row prefix produces a verdict (never falls through to
// text), and only all_retries_exhausted_no_response yields true.
func TestEveryExitReasonRowMapped(t *testing.T) {
	for _, row := range exitReasonFailures {
		got := ModelUnreachable(0, "", false, false, row.prefix, "no transport wording here")
		want := transientReason(row.reason, true, row.retryable)
		if got != want {
			t.Errorf("exit prefix %q mapped to %t, want %t (reason %q, retryable %t)",
				row.prefix, got, want, row.reason, row.retryable)
		}
	}
}
