// Package autothread is the single classification oracle for Discord
// auto-thread creation failures: given the shape of the error Discord
// returned to message.create_thread (HTTP status, JSON error code, message
// text), it answers what the adapter should do next.
//
// The decision exists because several Hermes bots can share one Discord
// server. When a user @-mentions two bots in one message, each bot's
// adapter tries to auto-thread the mention, but Discord allows exactly one
// thread per message: the first bot wins and every other bot receives HTTP
// 400 with JSON code 160004 ("A thread has already been created for this
// message"). That failure is not a failure — the thread exists, its id
// equals the source message id, and the losing bot should ADOPT it and
// answer there instead of dropping the request or spawning a second,
// unwatched thread from a seed placeholder.
//
// Decisions:
//
//   - Adopt:       the thread already exists (code 160004, or the canonical
//     "thread has already been created" text for duck-typed errors that lost
//     the code). The caller must resolve the existing thread — never post a
//     seed placeholder and never create a second thread on this path.
//   - RateLimited: HTTP 429, "Too many requests", or a bare "429" anywhere
//     in the message. The caller waits out the reported retry_after.
//   - Retry:       a transient transport failure (connect errors, resets,
//     timeouts, 5xx gateways). Worth the caller's standard short-backoff
//     retry. Descriptive only: the adapter retries every non-adopt,
//     non-rate-limit failure once anyway, so Retry and Fail take the same
//     path there.
//   - Fail:        anything else — a real rejection (permissions, invalid
//     form body) that retrying will not fix.
//
// Precedence is Adopt > RateLimited > Retry > Fail: a 160004 payload whose
// text happens to mention a number containing "429" still means the thread
// exists.
//
// The Python bridge (gateway/auto_thread_errors.py) invokes the
// cmd/autothread binary and treats an unavailable helper as "not adopt", so
// a missing Go toolchain degrades to the historical failure behavior.
package autothread

import "strings"

// Decision is the recommended handling for one thread-create error.
type Decision string

const (
	// Adopt: the thread already exists; resolve it and answer there.
	Adopt Decision = "adopt"
	// RateLimited: Discord asked the client to wait (HTTP 429).
	RateLimited Decision = "rate_limited"
	// Retry: transient transport failure; a short backoff may succeed.
	Retry Decision = "retry"
	// Fail: durable rejection; retrying will not help.
	Fail Decision = "fail"
)

// ThreadAlreadyCreatedCode is Discord's JSON error code for "A thread has
// already been created for this message" — the second create_thread call
// against a message that already anchors a thread.
const ThreadAlreadyCreatedCode = 160004

// alreadyCreatedText is the human-readable form of code 160004. Duck-typed
// or re-wrapped errors can lose the numeric code but keep the text.
const alreadyCreatedText = "thread has already been created"

// rateLimitText is discord.py's rendering of a 429 ("Too many requests.
// Retry in X seconds."), matched case-insensitively.
const rateLimitText = "too many requests"

// transientMarkers are lowercased substrings that mark a transport-level
// failure worth one short-backoff retry. They mirror the connect errors the
// adapter's retry loop was built for (#20243): "Cannot connect to host
// discord.com:443", resets, timeouts, and 5xx gateway text.
var transientMarkers = []string{
	"cannot connect to host",
	"connection reset",
	"connection refused",
	"connection aborted",
	"connect call failed",
	"timed out",
	"timeout",
	"temporary failure",
	"temporarily unavailable",
	"service unavailable",
	"bad gateway",
	"gateway timeout",
	"eof",
}

// transientStatuses are HTTP statuses whose failure is worth one retry.
var transientStatuses = map[int]bool{
	500: true, // Internal Server Error
	502: true, // Bad Gateway
	503: true, // Service Unavailable
	504: true, // Gateway Timeout
}

// Classify maps one thread-create error to its handling decision. status is
// the HTTP status (0 when unknown), code the Discord JSON error code (0 when
// absent), message the error's text form. All three are best-effort extracts
// from a duck-typed exception; Classify never panics on zero values.
func Classify(status int, code int, message string) Decision {
	text := strings.ToLower(message)
	switch {
	case code == ThreadAlreadyCreatedCode,
		strings.Contains(text, alreadyCreatedText):
		return Adopt
	case status == 429,
		strings.Contains(text, rateLimitText),
		strings.Contains(message, "429"):
		return RateLimited
	case transientStatuses[status], hasTransientMarker(text):
		return Retry
	default:
		return Fail
	}
}

// hasTransientMarker reports whether the lowercased error text carries any
// known transient-transport marker.
func hasTransientMarker(text string) bool {
	for _, marker := range transientMarkers {
		if strings.Contains(text, marker) {
			return true
		}
	}
	return false
}

// Decisions returns every Decision in precedence order (Adopt first). It is
// the exhaustive set the wire protocol and the Python bridge validate
// against; tests pin it so a renamed or dropped decision cannot drift
// between the two sides.
func Decisions() []Decision {
	return []Decision{Adopt, RateLimited, Retry, Fail}
}
