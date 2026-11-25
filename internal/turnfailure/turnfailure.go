// Package turnfailure is the Go twin of the terminal failed-turn copy
// contract in agent/turn_failure_copy.py (plus the two gateway boundary
// helpers in gateway/run_turn.py and the shutdown tool-category mapping in
// gateway/run_shutdown.py). Every surface that reports a dead turn — the
// chat loop's result dict, the gateway's failed-turn transcript boundary,
// cron and subagent notices — must agree on WHY a turn died and WHAT the
// user should do next, keyed off the same failure_reason taxonomy. This is
// the canonical statement of that contract; the Python module is what the
// runtime executes (deployments carry no Go toolchain, the same arrangement
// as every other internal/* twin). cmd/turnfailure is the executable form:
// the Python contract test (tests/gateway/test_turn_failure_go_contract.py)
// builds it and asserts identical answers for every shared-table record in
// testdata/turnfailure_cases.json; the Go tests run the same table as a
// second cross-check. Production Python never shells out to it.
//
// Contract (shared with the Python twin):
//
//   - SITE_FAILURE_CODES are the failure codes minted by loop sites that
//     are not provider verdicts. empty_response and session_busy carry no
//     site copy here (turn_explainers.py and the lease word them), every
//     other site code has a row in the failure-code copy table.
//   - failed_turn_notice never claims "not processed" once a tool row or a
//     tool-calling assistant row exists in the turn: the effects may have
//     run, so the copy switches to the verify-before-resend wording.
//   - _hmwa_close_failed_turn appends the boundary row iff the durable
//     transcript tail is an open user row; a redelivered or already-closed
//     turn must never stack a second assistant row.
//   - _hmwa_add_failed_turn_notice appends the boundary notice to a
//     non-empty response and never produces a leading blank block.
//   - exit_reason_failure prefix-matches in table order: several reasons
//     carry a parenthesised detail (local_processing_error(...)).
//     fails_turn=false rows are advisory: the descriptor fields are stamped
//     but the loop's failed/completed choice stands.
//   - invalid_response_failure_reason maps an embedded provider error code
//     to the classifier reason (429→rate_limit, 500/502→server_error,
//     503/529→overloaded, 504/524→timeout) via int() coercion: "429" and
//     429.7 both coerce to 429, anything unparseable falls back to
//     "invalid_response".
//   - The shutdown interrupt tool category is slug-safe for
//     interrupt_issuer attribution: a reason mentioning "restart" maps to
//     "gateway restart", everything else to "gateway shutdown".
package turnfailure

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

// SiteFailureCodes mirrors SITE_FAILURE_CODES: failure codes minted by loop
// sites that are not provider verdicts.
var SiteFailureCodes = map[string]bool{
	"context_overflow":     true,
	"truncated":            true,
	"invalid_response":     true,
	"empty_response":       true,
	"loop_error":           true,
	"interpreter_shutdown": true,
	"session_busy":         true,
}

const (
	// FailedTurnNotice mirrors FAILED_TURN_NOTICE: the boundary copy for a
	// turn nothing ran in.
	FailedTurnNotice = "Your request was not processed. Send it again if you still want me to carry it out."
	// PartialFailedTurnNotice mirrors PARTIAL_FAILED_TURN_NOTICE: the
	// boundary copy for a turn whose tool effects may already have run.
	PartialFailedTurnNotice = "This turn did not complete. Some actions may already have run; verify their effects before resending."
)

// TurnMessage is the (role, tool-calling?) projection of a transcript row.
type TurnMessage struct {
	Role      string
	ToolCalls bool
}

// FailedTurnNoticeFor mirrors failed_turn_notice: partial wording the moment
// any tool row or tool-calling assistant row appears in the turn.
func FailedTurnNoticeFor(msgs []TurnMessage) string {
	for _, m := range msgs {
		if m.Role == "tool" || (m.Role == "assistant" && m.ToolCalls) {
			return PartialFailedTurnNotice
		}
	}
	return FailedTurnNotice
}

// AddFailedTurnNotice mirrors gateway/run_turn.py
// GatewayTurnMixin._hmwa_add_failed_turn_notice: append the boundary notice
// to a non-empty (stripped) response; an empty response yields the notice
// alone so no leading blank block ships to the platform.
func AddFailedTurnNotice(response, notice string) string {
	response = strings.TrimSpace(response)
	if response == "" {
		return notice
	}
	return response + "\n\n" + notice
}

// CloseFailedTurnBoundary mirrors the tail gate of
// gateway/run_turn.py GatewayTurnMixin._hmwa_close_failed_turn: the boundary
// row is appended iff the durable transcript tail is an open user row.
func CloseFailedTurnBoundary(tailRole string) bool {
	return tailRole == "user"
}

// InterruptToolReason mirrors gateway/run_shutdown.py
// _interrupt_tool_reason: the system tool-cancellation category for a
// shutdown-path interrupt reason.
func InterruptToolReason(reason string) string {
	if strings.Contains(strings.ToLower(reason), "restart") {
		return "gateway restart"
	}
	return "gateway shutdown"
}

// ExitFailure mirrors agent/turn_failure_copy.py ExitFailure.
type ExitFailure struct {
	Reason    string
	Retryable bool
	FailsTurn bool
}

// exitReasonPrefix mirrors one row of _EXIT_REASON_FAILURES: (exit-reason
// prefix, failure_reason, retryable, fails_turn) in table order — prefix
// match, several reasons carry a parenthesised detail.
type exitReasonPrefix struct {
	Prefix    string
	Reason    string
	Retryable bool
	FailsTurn bool
}

var exitReasonPrefixes = []exitReasonPrefix{
	{"empty_response_exhausted", "empty_response", true, false},
	{"all_retries_exhausted_no_response", "server_error", true, true},
	{"interpreter_shutdown", "interpreter_shutdown", false, true},
	{"local_processing_error", "loop_error", false, false},
	{"repeated_outer_errors", "loop_error", true, true},
	{"error_near_max_iterations", "loop_error", true, true},
	{"context_compression_timeout", "context_overflow", false, true},
	{"context_compression_exhausted", "context_overflow", false, true},
	{"ollama_runtime_context_too_small", "context_overflow", false, true},
	{"redirect_restart_limit_exceeded", "loop_error", true, false},
	{"rebuilt_restart_limit_exceeded", "loop_error", true, false},
}

// ExitReasonFailure mirrors exit_reason_failure: the verdict for a loop exit
// that carries one, else nil.
func ExitReasonFailure(turnExitReason string) *ExitFailure {
	for _, row := range exitReasonPrefixes {
		if strings.HasPrefix(turnExitReason, row.Prefix) {
			return &ExitFailure{Reason: row.Reason, Retryable: row.Retryable, FailsTurn: row.FailsTurn}
		}
	}
	return nil
}

// invalidResponseCodes mirrors _INVALID_RESPONSE_CODES: provider error code
// carried inside an HTTP-200 body → classifier reason.
var invalidResponseCodes = map[int64]string{
	429: "rate_limit",
	500: "server_error",
	502: "server_error",
	503: "overloaded",
	529: "overloaded",
	504: "timeout",
	524: "timeout",
}

// InvalidResponseFailureReason mirrors invalid_response_failure_reason's
// code coercion and lookup. int() semantics: numeric values truncate toward
// zero, numeric strings parse after whitespace strip, everything else
// (bools, lists, dicts, unparseable strings) fails to "invalid_response".
func InvalidResponseFailureReason(code any) string {
	n, ok := coerceInt(code)
	if !ok {
		return "invalid_response"
	}
	if reason, hit := invalidResponseCodes[n]; hit {
		return reason
	}
	return "invalid_response"
}

func coerceInt(v any) (int64, bool) {
	switch t := v.(type) {
	case json.Number:
		if n, err := t.Int64(); err == nil {
			return n, true
		}
		if f, err := t.Float64(); err == nil {
			return int64(f), true // int(429.7) == 429: truncate toward zero
		}
		return 0, false
	case string:
		n, err := strconv.ParseInt(strings.TrimSpace(t), 10, 64)
		if err != nil {
			return 0, false
		}
		return n, true
	default:
		return 0, false
	}
}

const nextStepsRetry = "Wait a minute and send /retry, or switch models with /model."

const nextStepsLoop = "Your message is saved. Send `continue` to try again, or start a new session with /new. " +
	"If it happens again, run `hermes doctor` and share the error details."

// exhaustedLeads mirrors _EXHAUSTED_LEADS; exhaustedDefaultLead mirrors
// _EXHAUSTED_DEFAULT_LEAD.
var exhaustedLeads = map[string]string{
	"rate_limit":          "{label} rate-limited every one of {attempts} attempts",
	"upstream_rate_limit": "{label} rate-limited every one of {attempts} attempts",
	"overloaded":          "{label} reported it was overloaded on all {attempts} attempts",
	"server_error":        "{label} returned a server error on all {attempts} attempts",
	"timeout":             "{label} didn't respond in time on any of {attempts} attempts",
}

const exhaustedDefaultLead = "{label} didn't answer after {attempts} attempts"

// FormatResetWindow mirrors agent/retry_utils.py format_reset_window:
// "~9h" / "~45 min" for chat copy naming when a quota window reopens
// (ceilinged).
func FormatResetWindow(seconds float64) string {
	s := int(seconds)
	if s >= 3600 {
		return fmt.Sprintf("~%dh", (s+3599)/3600)
	}
	return fmt.Sprintf("~%d min", (s+59)/60)
}

// ExhaustedCopy mirrors exhausted_copy: chat copy once retries + fallback
// are exhausted. A rate limit whose reset window is known (and is at least
// two minutes out) names it: an 8.6h plan quota is not "wait a minute".
func ExhaustedCopy(reason, label string, attempts json.Number, summary string, resetSeconds *float64) string {
	tpl, ok := exhaustedLeads[reason]
	if !ok {
		tpl = exhaustedDefaultLead
	}
	lead := strings.ReplaceAll(
		strings.ReplaceAll(tpl, "{label}", label),
		"{attempts}", attempts.String(),
	)
	situation := "it looks temporarily unavailable. " + nextStepsRetry
	if resetSeconds != nil && *resetSeconds >= 120 {
		situation = fmt.Sprintf(
			"its usage limit resets in %s. Send /retry after that, or switch models with /model.",
			FormatResetWindow(*resetSeconds),
		)
	}
	return fmt.Sprintf(
		"%s — %s To avoid this in future, add a backup provider with `hermes fallback add`.\n\nProvider said: %s",
		lead, situation, summary,
	)
}

// failureCauseGloss mirrors FAILURE_CAUSE_GLOSS: one reason → "what
// happened" clause (no HTTP codes, no "provider" jargon), shared by cron,
// subagent and chat notices. Reasons absent here are not provider-shaped;
// callers fall back to the raw error text.
var failureCauseGlossTable = map[string]string{
	"timeout":                "the AI model service did not respond in time",
	"rate_limit":             "the AI model service was rate-limited (too many requests)",
	"upstream_rate_limit":    "the AI model service was rate-limited (too many requests)",
	"overloaded":             "the AI model service is overloaded right now",
	"server_error":           "the AI model service returned an internal error",
	"billing":                "the AI model service says the account's usage or credit limit is reached",
	"billing_unverified":     "the AI model service says the account's usage or credit limit is reached",
	"auth":                   "the AI model service rejected the sign-in",
	"auth_permanent":         "the AI model service rejected the sign-in",
	"upstream_blocked":       "a firewall/CDN in front of the AI model service blocked the request",
	"model_not_found":        "the model {subject} uses was not found at the AI model service",
	"content_policy_blocked": "the AI model service's safety filter rejected the request",
	"context_overflow":       "{possessive} request grew too large for the model",
	"payload_too_large":      "{possessive} request grew too large for the model",
}

// FailureCauseGloss mirrors failure_cause_gloss: the formatted clause, or ""
// when the reason has no gloss (the Python twin returns None; the cmd layer
// distinguishes the two).
func FailureCauseGloss(reason, subject, possessive string) (string, bool) {
	tpl, ok := failureCauseGlossTable[reason]
	if !ok {
		return "", false
	}
	out, _ := formatTemplate(tpl, map[string]any{"subject": subject, "possessive": possessive})
	return out, true
}

// failureCodeCopy mirrors _FAILURE_CODE_COPY: chat copy for the codes in
// SiteFailureCodes that a loop site renders itself (empty_response is worded
// by turn_explainers.py, session_busy by the lease).
var failureCodeCopy = map[string]string{
	"context_overflow": "This conversation has grown too long for {model} to read, and Hermes couldn't shrink " +
		"it enough automatically. Start a new session with /new (your history is kept), or try " +
		"/compress once more. Switching to a model with a bigger context window also works.",
	"truncated": "The model's reply was cut off before it finished (it hit its output length limit), so " +
		"Hermes didn't run the incomplete action. Nothing was changed. Send `continue`, ask for " +
		"the work in smaller steps, or raise max_tokens for this model.",
	"invalid_response": "{label} sent back an empty or broken reply {attempts} times — it is probably overloaded " +
		"or rate-limiting you. " + nextStepsRetry + "\n\nDetails: {detail}",
	"loop_error": "Hermes hit repeated errors and stopped this turn so it wouldn't keep retrying. " +
		nextStepsLoop + "\n\nDetails: {detail}",
	"interpreter_shutdown": "Hermes was shutting down and stopped this turn. Your conversation is saved — reopen " +
		"it{resume} and send your message again.",
}

// oneOffCopy mirrors _ONE_OFF_COPY: deterministic loop exits that are NOT
// failure codes (the result they ride carries a code from the table above,
// or none at all). server_context_rejection's wording deliberately avoids
// the overflow phrases gateway/run_turn.py matches on: this failure is
// transient, so the user's message must stay in the transcript and the
// session must not be auto-reset.
var oneOffCopy = map[string]string{
	"payload_too_large": "This conversation (including attachments) has grown too large to send to {model}, and " +
		"Hermes couldn't shrink it enough automatically. Start a new session with /new (your " +
		"history is kept), or try /compress once more.",
	"compression_disabled": "This conversation is too long for {model} and automatic shrinking is turned off in " +
		"your settings (compression.enabled). Run /compress to shrink it now, /new to start " +
		"fresh, or pick a model with a bigger context window.",
	"server_context_rejection": "The model server rejected this request as too large, but this conversation is only " +
		"about {tokens:,} tokens — well under the {window:,}-token window Hermes knows for " +
		"{model} — so shrinking it would not help. Another request on the same server (for " +
		"example a background memory review from an earlier session) was probably holding its " +
		"capacity, or the server runs {model} with a smaller window than Hermes assumes. Wait a " +
		"moment and send /retry; if it keeps happening, check the server's context setting.",
	"stream_dropped_tool_call": "The connection to {label} kept dropping while the model was writing a large action, " +
		"so nothing was run. Check your network and send /retry; asking for the file in smaller " +
		"pieces also helps.",
	"local_processing_error": "Hermes hit an internal error while handling the model's reply and stopped this turn. " +
		nextStepsLoop + "\n\nDetails: {detail}",
	"reasoning_only": "⚠️ {model} spent all of its output budget thinking and never wrote an answer. Lower " +
		"its reasoning effort with `/reasoning low`, or switch to a different model with /model. " +
		"Its last thoughts, which may contain the answer:\n\n{preview}",
	"max_iterations_no_summary": "I ran out of steps for this turn ({limit} tool calls) before finishing, and couldn't " +
		"produce a summary. Send `continue` to keep going, or raise `max_iterations` in your config.",
}

// SiteCopyTable mirrors _SITE_COPY = _FAILURE_CODE_COPY ∪ _ONE_OFF_COPY.
func SiteCopyTable() map[string]string {
	out := make(map[string]string, len(failureCodeCopy)+len(oneOffCopy))
	for k, v := range failureCodeCopy {
		out[k] = v
	}
	for k, v := range oneOffCopy {
		out[k] = v
	}
	return out
}

// ErrUnknownSiteCode mirrors site_copy's KeyError on a code with no copy row.
type ErrUnknownSiteCode struct{ Code string }

func (e *ErrUnknownSiteCode) Error() string { return "no site copy for code " + e.Code }

// SiteCopy mirrors site_copy: chat copy for a failure code or one-off loop
// outcome; unknown fields default to empty strings. Field values may be
// strings or JSON numbers; the "{field:,}" form comma-groups an integer
// (Python's format spec on an int — the shared table never exercises it on
// a string, where Python would raise).
func SiteCopy(code string, fields map[string]any) (string, error) {
	tpl, ok := SiteCopyTable()[code]
	if !ok {
		return "", &ErrUnknownSiteCode{code}
	}
	return formatTemplate(tpl, fields)
}

// formatTemplate mirrors str.format_map(_Defaults(fields)): {name} and
// {name:,} substitutions, missing keys render as the empty string.
func formatTemplate(tpl string, fields map[string]any) (string, error) {
	var b strings.Builder
	for i := 0; i < len(tpl); i++ {
		if tpl[i] != '{' {
			b.WriteByte(tpl[i])
			continue
		}
		j := strings.IndexByte(tpl[i:], '}')
		if j < 0 {
			return "", fmt.Errorf("unterminated {{ in template %q", tpl)
		}
		name := tpl[i+1 : i+j]
		i += j
		spec := ""
		if k := strings.IndexByte(name, ':'); k >= 0 {
			spec = name[k+1:]
			name = name[:k]
		}
		b.WriteString(renderField(fields[name], spec))
	}
	return b.String(), nil
}

func renderField(v any, spec string) string {
	if v == nil {
		return ""
	}
	if spec != "," {
		return fmt.Sprint(v)
	}
	switch t := v.(type) {
	case json.Number:
		if n, err := t.Int64(); err == nil {
			return commaGroup(n)
		}
		if f, err := t.Float64(); err == nil {
			return commaGroup(int64(f))
		}
		return t.String()
	default:
		return fmt.Sprint(v)
	}
}

func commaGroup(n int64) string {
	neg := n < 0
	digits := strconv.FormatInt(func() int64 {
		if neg {
			return -n
		}
		return n
	}(), 10)
	var parts []string
	for len(digits) > 3 {
		parts = append([]string{digits[len(digits)-3:]}, parts...)
		digits = digits[:len(digits)-3]
	}
	if digits != "" {
		parts = append([]string{digits}, parts...)
	}
	out := strings.Join(parts, ",")
	if neg {
		return "-" + out
	}
	return out
}

// StampFailure mirrors stamp_failure: the UI verdict fields every terminal
// result dict must carry (error_surface.py keys off them).
func StampFailure(reason string, retryable bool) map[string]any {
	return map[string]any{"failure_reason": reason, "failure_retryable": retryable}
}
