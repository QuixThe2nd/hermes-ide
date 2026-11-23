// Package startrecovery is the Go contract for hermes_starts' anchor
// recovery, mirroring plugins/hermes_starts/recovery.py in the Python
// tree. The Python plugin still executes the Python copy; this package is
// the Go contract, the same arrangement as internal/cronunreachable — it
// is not imported by Python.
//
// The contract covers the decision half of recovery only — everything that
// must be settled before the first Discord write: which reservation an
// anchor belongs to, whether a finished start makes the call a no-op, which
// thread the start lands in, and which tail parts still need posting. The
// executing side (REST calls, state mutation, session seeding) stays in the
// Python plugin, which owns the state file format.
package startrecovery

import (
	"strings"
)

// Bounds mirroring the Python constants. RequestTimeoutSeconds mirrors
// _DISCORD_REQUEST_TIMEOUT_SECONDS in plugins/hermes_starts/__init__.py:
// every Discord REST call is bounded so a call stuck on a dead peer fails
// into its caller's error path instead of pinning the state lock until the
// tool-level timeout abandons the worker (pc_c965dac61862).
// LockTimeoutSeconds mirrors _RECOVER_LOCK_TIMEOUT_SECONDS: recovery takes
// the same cross-process lock as a live start, with its own shorter budget,
// and fails cleanly when a start still holds it.
const (
	RequestTimeoutSeconds = 30.0
	LockTimeoutSeconds    = 10.0
)

// Reservation stages, mirroring the milestones _reserve_start and
// _update_reservation persist: claimed before any POST, anchor id recorded
// the moment the opening lands, thread id recorded before the tail posts.
const (
	StagePending       = "pending"
	StageAnchorPosted  = "anchor_posted"
	StageThreadCreated = "thread_created"
)

// Decision statuses, in the order recovery checks them.
const (
	// StatusAlreadyRecovered: the anchor's id is already a committed
	// history entry — the call is a no-op that must not touch the network
	// or the state file.
	StatusAlreadyRecovered = "already_recovered"
	// StatusAdopt: the anchor belongs to a pending reservation and may be
	// reconciled to a finished start.
	StatusAdopt = "adopt"
	// StatusRefuse: nothing matches, or the anchor cannot be proven ours.
	// No write of any kind may happen.
	StatusRefuse = "refuse"
)

// Reservation is one in-flight start: a "pending_start" row of
// hermes_starts/state.json. AnchorID and ThreadID are empty until the
// corresponding milestone was persisted before the interruption.
type Reservation struct {
	Number   int
	Kind     string
	Text     string
	Stage    string
	AnchorID string
	ThreadID string
}

// HistoryEntry is one committed start: a "start_history" row. ID is the
// anchor message id, which is what makes recovery idempotent.
type HistoryEntry struct {
	ID       string
	Number   int
	ThreadID string
}

// AnchorMessage is the Discord message recovery considers adopting as the
// opening. ThreadID is set when the anchor already carries its thread.
type AnchorMessage struct {
	ID       string
	Content  string
	ThreadID string
}

// Decision is recovery's verdict for one anchor, settled before any write.
type Decision struct {
	Status string
	Reason string
	// Matched is the index into the reservations the decision adopted, or
	// -1 when no reservation was adopted.
	Matched int
	// Entry carries the history row for StatusAlreadyRecovered.
	Entry HistoryEntry
}

// AlreadyRecovered reports the committed history entry whose anchor id
// equals anchorID, and whether one exists. History is checked before any
// reservation or network access: an anchor already in history means the
// start finished, and a second recover must post nothing.
func AlreadyRecovered(history []HistoryEntry, anchorID string) (HistoryEntry, bool) {
	for _, entry := range history {
		if entry.ID == anchorID {
			return entry, true
		}
	}
	return HistoryEntry{}, false
}

// Match finds the reservation an anchor belongs to, mirroring
// _match_reservation: a recorded anchor id is authoritative and wins over
// everything else; without one, only a caller who supplied the reserved
// number may adopt, and only a reservation of that number whose own anchor
// id is still unknown (the interruption happened during the anchor POST
// itself). Returns the matched index or -1.
func Match(reservations []Reservation, anchorID string, startNumber int, hasNumber bool) int {
	if anchorID != "" {
		for i := range reservations {
			if reservations[i].AnchorID != "" && reservations[i].AnchorID == anchorID {
				return i
			}
		}
	}
	if !hasNumber {
		return -1
	}
	for i := range reservations {
		if reservations[i].AnchorID == "" && reservations[i].Number == startNumber {
			return i
		}
	}
	return -1
}

// FirstPartOf is the content an anchor must carry to be adopted by number:
// the reservation's first delivery part (plugins/hermes_starts
// _split_delivery(text, mentionUID)[0]). The splitter itself stays in the
// Python plugin; the contract takes its output.
func FirstPartOf(splitParts []string) string {
	if len(splitParts) == 0 {
		return ""
	}
	return splitParts[0]
}

// AdoptByContent reports whether an anchor matched only by number may be
// adopted: its content must equal the reservation's first delivery part
// byte for byte. An empty anchor content (the message could not be fetched)
// never adopts — an unprovable anchor is someone else's message until
// proven otherwise.
func AdoptByContent(anchorContent, firstPart string) bool {
	return anchorContent != "" && anchorContent == firstPart
}

// MaxMessageLen mirrors _MAX_MESSAGE_LEN in plugins/hermes_starts: every
// delivered part must fit within Discord's message limit with room for
// the mention prefix on the first part.
const MaxMessageLen = 1950

// ComposeMessage mirrors _compose_message: the optional next-move footer
// is appended as its own italic-labelled paragraph.
func ComposeMessage(message, nextMove string) string {
	if nextMove == "" {
		return message
	}
	return message + "\n\n*Where I'd take this:* " + nextMove
}

// runeLen mirrors Python's len(): the splitter's budget is characters,
// not bytes.
func runeLen(s string) int {
	n := 0
	for range s {
		n++
	}
	return n
}

// SplitMessage mirrors _split_message: a greedy paragraph join under the
// budget, never splitting inside a paragraph that already fits, falling
// back to hard-wrapping a paragraph that alone exceeds the budget.
func SplitMessage(content string, maxLen int) []string {
	if runeLen(content) <= maxLen {
		return []string{content}
	}
	var chunks []string
	current := ""
	for _, paragraph := range strings.Split(content, "\n\n") {
		candidate := paragraph
		if current != "" {
			candidate = current + "\n\n" + paragraph
		}
		if runeLen(candidate) <= maxLen {
			current = candidate
			continue
		}
		if current != "" {
			chunks = append(chunks, current)
		}
		if runeLen(paragraph) <= maxLen {
			current = paragraph
		} else {
			// A paragraph larger than the whole budget is one chunk; the
			// wrap pass below splits it.
			current = ""
			chunks = append(chunks, paragraph)
		}
	}
	if current != "" {
		chunks = append(chunks, current)
	}
	var messages []string
	for _, chunk := range chunks {
		if runeLen(chunk) <= maxLen {
			messages = append(messages, chunk)
			continue
		}
		messages = append(messages, WrapOversizedPiece(chunk, maxLen)...)
	}
	return messages
}

// WrapOversizedPiece mirrors _wrap_oversized_piece: cut at the last
// newline inside the window, else the last space or tab, else hard-cut.
// Never returns an empty slice for non-empty input.
func WrapOversizedPiece(text string, maxLen int) []string {
	if runeLen(text) <= maxLen {
		return []string{text}
	}
	runes := []rune(text)
	var chunks []string
	start := 0
	for start < len(runes) {
		remaining := len(runes) - start
		if remaining <= maxLen {
			chunks = append(chunks, string(runes[start:]))
			break
		}
		window := string(runes[start : start+maxLen])
		if splitAt := strings.LastIndex(window, "\n"); splitAt > 0 {
			cut := start + splitAt + 1
			chunks = append(chunks, string(runes[start:cut]))
			start = cut
			continue
		}
		spaceAt := strings.LastIndexAny(window, " \t")
		if spaceAt > 0 {
			cut := start + spaceAt + 1
			chunks = append(chunks, string(runes[start:cut]))
			start = cut
			continue
		}
		chunks = append(chunks, string(runes[start:start+maxLen]))
		start += maxLen
	}
	return chunks
}

// SplitDelivery mirrors _split_delivery: the mention prefix rides the
// first part only — the anchor always carries opening text beside the
// ping, never the ping alone — after the body was split with the prefix's
// budget subtracted. An empty mention uid delivers the body unchanged.
func SplitDelivery(content, mentionUID string) []string {
	if mentionUID == "" {
		return SplitMessage(content, MaxMessageLen)
	}
	prefix := "<@" + mentionUID + ">\n"
	parts := SplitMessage(content, MaxMessageLen-runeLen(prefix))
	parts[0] = prefix + parts[0]
	return parts
}

// PendingRow is one in-flight start as a failure result reports it,
// mirroring _pending_start_summary: number, milestone stage, recorded
// anchor id, and thread id only once a milestone recorded one.
type PendingRow struct {
	Number   int
	Stage    string
	AnchorID string
	ThreadID string
}

// PendingSummary mirrors _pending_start_summary. The Python side also
// skips rows that are not dicts or whose number is not an integer; those
// never survive into a []Reservation, so the Go mirror receives already
// well-formed rows.
func PendingSummary(reservations []Reservation) []PendingRow {
	rows := make([]PendingRow, 0, len(reservations))
	for _, r := range reservations {
		rows = append(rows, PendingRow{
			Number:   r.Number,
			Stage:    r.Stage,
			AnchorID: r.AnchorID,
			ThreadID: r.ThreadID,
		})
	}
	return rows
}

// RecoverHint mirrors _recover_hint: how to finish the first half-sent
// start without reposting it. An anchor id is the one-call answer; a row
// still pending its anchor asks for the number plus the inbox message id,
// with content verified before adopting. Empty summary, empty hint.
func RecoverHint(rows []PendingRow) string {
	for _, row := range rows {
		if row.AnchorID != "" {
			return "a start did not finish: call start_conversation with " +
				"action='recover' and anchor_id='" + row.AnchorID + "' to " +
				"reconcile its thread, listener mark, session seed and " +
				"history without reposting the opening"
		}
	}
	if len(rows) > 0 {
		return "a start did not finish before its anchor id was recorded: " +
			"find the posted opening's message id in the inbox channel, " +
			"then call start_conversation with action='recover', " +
			"start_number=" + itoa(rows[0].Number) + " and that anchor_id " +
			"(content is verified before adopting)"
	}
	return ""
}

// EnrichFailure mirrors _with_pending_details: a failed or deferred send
// reports what is known about in-flight starts, but only when something
// is actually in flight — an unlocked failure result stays byte-identical
// to what it was before recovery existed.
func EnrichFailure(result map[string]any, rows []PendingRow) map[string]any {
	if len(rows) == 0 {
		return result
	}
	summary := make([]map[string]any, 0, len(rows))
	for _, row := range rows {
		entry := map[string]any{
			"number":    row.Number,
			"stage":     row.Stage,
			"anchor_id": row.AnchorID,
		}
		if row.ThreadID != "" {
			entry["thread_id"] = row.ThreadID
		}
		summary = append(summary, entry)
	}
	result["pending_start"] = summary
	if hint := RecoverHint(rows); hint != "" {
		result["recover_hint"] = hint
	}
	return result
}

// ThreadChoice says where a recovered start's thread lives.
type ThreadChoice struct {
	ThreadID   string
	MustCreate bool
}

// ChooseThread reuses the thread already anchored on the message (the
// interruption happened after thread creation, or a prior recover created
// it); only an anchor with no thread gets a fresh one, named by ThreadName.
func ChooseThread(message AnchorMessage) ThreadChoice {
	if message.ThreadID != "" {
		return ThreadChoice{ThreadID: message.ThreadID}
	}
	return ThreadChoice{MustCreate: true}
}

// MissingTailParts returns the delivery parts after the anchor that the
// thread does not already hold, compared by content and in delivery order.
// The abandoned worker may have gotten further than its last persisted
// milestone, so presence — not the milestone stage — decides what is owed.
func MissingTailParts(parts []string, threadContents []string) []string {
	present := make(map[string]bool, len(threadContents))
	for _, content := range threadContents {
		present[content] = true
	}
	var missing []string
	for _, part := range parts[1:] {
		if !present[part] {
			missing = append(missing, part)
		}
	}
	return missing
}

// ThreadName mirrors the name every recovered start's thread carries:
// f"Start #{number} — {kind}" in the Python plugin.
func ThreadName(number int, kind string) string {
	return "Start #" + itoa(number) + " — " + kind
}

// Decide runs the pre-write decision chain mirroring _recover_locked:
// committed history first (idempotence), then the reservation match, then —
// only for a match by number — first-part content verification. The
// anchor's thread field decides reuse versus creation and is reported for
// StatusAdopt; splitParts is _split_delivery(reservation.Text, mention).
// Decide is pure: a StatusRefuse outcome must leave state and Discord
// untouched, which is why the Python side runs it before its first GET.
func Decide(history []HistoryEntry, reservations []Reservation, anchorID string, startNumber int, hasNumber bool, message AnchorMessage, splitParts []string) Decision {
	if entry, ok := AlreadyRecovered(history, anchorID); ok {
		return Decision{
			Status:  StatusAlreadyRecovered,
			Matched: -1,
			Entry:   entry,
		}
	}
	matched := Match(reservations, anchorID, startNumber, hasNumber)
	if matched < 0 {
		if hasNumber {
			return Decision{
				Status:  StatusRefuse,
				Reason:  "anchor " + anchorID + " matches no pending start and no history entry",
				Matched: -1,
			}
		}
		return Decision{
			Status: StatusRefuse,
			Reason: "anchor " + anchorID + " matches no reservation with a recorded " +
				"anchor id; retry with the start_number of the reservation whose " +
				"opening it is (content is verified before adopting)",
			Matched: -1,
		}
	}
	reservation := reservations[matched]
	if reservation.AnchorID == "" && !AdoptByContent(message.Content, FirstPartOf(splitParts)) {
		return Decision{
			Status: StatusRefuse,
			Reason: "anchor " + anchorID + " content does not match the first " +
				"delivery part of start #" + itoa(reservation.Number) +
				"; refusing to adopt",
			Matched: -1,
		}
	}
	return Decision{Status: StatusAdopt, Matched: matched}
}

// itoa is the minimal integer formatter this contract needs; it stays a
// hand-rolled helper so the refusal phrases read identically to the
// Python f-strings they mirror.
func itoa(number int) string {
	if number == 0 {
		return "0"
	}
	negative := number < 0
	if negative {
		number = -number
	}
	var digits []byte
	for number > 0 {
		digits = append(digits, byte('0'+number%10))
		number /= 10
	}
	if negative {
		digits = append(digits, '-')
	}
	for i, j := 0, len(digits)-1; i < j; i, j = i+1, j-1 {
		digits[i], digits[j] = digits[j], digits[i]
	}
	return string(digits)
}
