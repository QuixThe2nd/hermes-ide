package startrecovery

import (
	"strings"
	"testing"
)

// The fixtures mirror the Python pins in
// tests/plugins/hermes_starts/test_hermes_starts.py: an inbox channel, a
// reservation whose opening splits into parts (the composed text is the
// two-paragraph 2400-char body of _RECOVERY_MESSAGE), and anchors at the
// milestones the Python suite arranges.

func anchorPostedReservation(number int) Reservation {
	return Reservation{
		Number:   number,
		Kind:     "feedback",
		Text:     "A" + strings.Repeat("x", 1200) + "\n\nB" + strings.Repeat("y", 1200),
		Stage:    StageAnchorPosted,
		AnchorID: "msg-A",
	}
}

func pendingReservation(number int) Reservation {
	row := anchorPostedReservation(number)
	row.Stage = StagePending
	row.AnchorID = ""
	return row
}

func firstPartOf(r Reservation) string {
	// The Python splitter prefixes the owner mention; the contract only
	// needs the exact first part the reservation would deliver.
	return "<@123456789012345678>\n" + strings.SplitN(r.Text, "\n\n", 2)[0]
}

func TestTimeoutBounds(t *testing.T) {
	// Mirrors TestBoundedDiscordSends (pin 1) and TestRecoverLockBusy
	// (pin 9): both budgets are finite and small enough that an abandoned
	// worker cannot pin the lock for long.
	cases := []struct {
		name string
		got  float64
	}{
		{"RequestTimeoutSeconds", RequestTimeoutSeconds},
		{"LockTimeoutSeconds", LockTimeoutSeconds},
	}
	for _, tc := range cases {
		if tc.got <= 0 || tc.got > 60 {
			t.Errorf("%s = %v, want 0 < value <= 60", tc.name, tc.got)
		}
	}
}

func TestAlreadyRecovered(t *testing.T) {
	history := []HistoryEntry{
		{ID: "msg-9", Number: 4, ThreadID: "thread-4"},
		{ID: "msg-A", Number: 5, ThreadID: "thread-1"},
	}
	// Pin 5 (TestRecoverIdempotent): an anchor whose id is already a
	// history entry is a finished start; the second recover posts nothing.
	entry, ok := AlreadyRecovered(history, "msg-A")
	if !ok || entry.Number != 5 || entry.ThreadID != "thread-1" {
		t.Fatalf("AlreadyRecovered(msg-A) = %+v, %v; want history entry for start 5", entry, ok)
	}
	if entry, ok := AlreadyRecovered(history, "msg-Z"); ok {
		t.Fatalf("AlreadyRecovered(msg-Z) = %+v, true; want miss", entry)
	}
}

func TestMatchByRecordedAnchor(t *testing.T) {
	// Pin 4 (TestRecoverHappyPath): the anchor id recorded at the
	// anchor_posted milestone is authoritative.
	reservations := []Reservation{
		pendingReservation(2),
		anchorPostedReservation(5),
		{Number: 7, Kind: "idea", Stage: StageThreadCreated, AnchorID: "msg-7", ThreadID: "thread-7"},
	}
	if got := Match(reservations, "msg-A", 0, false); got != 1 {
		t.Errorf("Match(anchor msg-A) = %d, want 1", got)
	}
	// A reservation whose anchor was recorded is matched by its own anchor
	// id even when a caller also passes a number.
	if got := Match(reservations, "msg-7", 5, true); got != 2 {
		t.Errorf("Match(anchor msg-7, number 5) = %d, want 2", got)
	}
	// Unknown anchor, no number: refused before any fetch.
	if got := Match(reservations, "msg-Z", 0, false); got != -1 {
		t.Errorf("Match(anchor msg-Z) = %d, want -1", got)
	}
}

func TestMatchByNumberOnlyWithEmptyAnchor(t *testing.T) {
	// Pin 8 (TestRecoverAdoptsByNumber): without a recorded anchor id,
	// only a caller who names the reserved number may adopt.
	reservations := []Reservation{
		pendingReservation(5),
		func() Reservation {
			// Its own anchor: only the pending reservation has an
			// unrecorded one.
			row := anchorPostedReservation(6)
			row.AnchorID = "msg-6"
			return row
		}(),
	}
	if got := Match(reservations, "msg-A", 5, true); got != 0 {
		t.Errorf("Match(number 5) = %d, want 0", got)
	}
	// Without the number the match must not happen: content proof is only
	// attempted when the caller claims ownership of the reservation.
	if got := Match(reservations, "msg-A", 0, false); got != -1 {
		t.Errorf("Match(no number) = %d, want -1", got)
	}
	// A reservation whose anchor id was recorded is not adoptable by
	// number through the unrecorded path.
	if got := Match(reservations, "msg-A", 6, true); got != -1 {
		t.Errorf("Match(number 6) = %d, want -1", got)
	}
}

func TestAdoptByContent(t *testing.T) {
	reservation := pendingReservation(5)
	want := firstPartOf(reservation)
	// Matching content adopts.
	if !AdoptByContent(want, want) {
		t.Errorf("AdoptByContent(equal content) = false, want true")
	}
	// Pin 8 refusal: someone else's message never adopts.
	if AdoptByContent("Someone else's message entirely.", want) {
		t.Errorf("AdoptByContent(different content) = true, want false")
	}
	// An unfetchable anchor (pin 7 shape: empty content) never adopts.
	if AdoptByContent("", want) || AdoptByContent("", "") {
		t.Errorf("AdoptByContent(empty) = true, want false")
	}
	// The empty first part of an empty reservation never adopts either.
	if AdoptByContent("", FirstPartOf(nil)) {
		t.Errorf("AdoptByContent(empty, no parts) = true, want false")
	}
}

func TestChooseThread(t *testing.T) {
	// Pin 4: an anchor with no thread gets one created.
	if choice := ChooseThread(AnchorMessage{ID: "msg-A", Content: "c"}); !choice.MustCreate || choice.ThreadID != "" {
		t.Errorf("ChooseThread(no thread) = %+v, want MustCreate", choice)
	}
	// Pin 6 (TestRecoverExistingThread): a thread already on the anchor is
	// reused — no create call, tails land in the existing thread.
	if choice := ChooseThread(AnchorMessage{ID: "msg-A", Content: "c", ThreadID: "thr-X"}); choice.MustCreate || choice.ThreadID != "thr-X" {
		t.Errorf("ChooseThread(thr-X) = %+v, want reuse thr-X", choice)
	}
}

func TestMissingTailParts(t *testing.T) {
	parts := []string{"part-0-anchor", "part-1", "part-2", "part-3"}
	// Pin 4: nothing in the thread yet — every tail part is owed, in order.
	if got := MissingTailParts(parts, nil); len(got) != 3 || got[0] != "part-1" || got[2] != "part-3" {
		t.Errorf("MissingTailParts(empty thread) = %v, want [part-1 part-2 part-3]", got)
	}
	// Pin 6: the first tail is already present — only the later parts post.
	if got := MissingTailParts(parts, []string{"part-1"}); len(got) != 2 || got[0] != "part-2" || got[1] != "part-3" {
		t.Errorf("MissingTailParts(part-1 present) = %v, want [part-2 part-3]", got)
	}
	// Everything present (a prior recover got further than its milestone):
	// nothing posts.
	if got := MissingTailParts(parts, []string{"part-3", "part-1", "part-2"}); len(got) != 0 {
		t.Errorf("MissingTailParts(all present) = %v, want none", got)
	}
	// Presence is compared by content, never by position or count.
	if got := MissingTailParts(parts, []string{"part-1", "part-1"}); len(got) != 2 || got[0] != "part-2" {
		t.Errorf("MissingTailParts(duplicate present) = %v, want [part-2 part-3]", got)
	}
}

func TestThreadName(t *testing.T) {
	cases := []struct {
		number int
		kind   string
		want   string
	}{
		{5, "feedback", "Start #5 — feedback"},
		{12, "idea", "Start #12 — idea"},
		{0, "observation", "Start #0 — observation"},
	}
	for _, tc := range cases {
		if got := ThreadName(tc.number, tc.kind); got != tc.want {
			t.Errorf("ThreadName(%d, %q) = %q, want %q", tc.number, tc.kind, got, tc.want)
		}
	}
}

func TestDecide(t *testing.T) {
	happy := anchorPostedReservation(5)
	pending := pendingReservation(5)
	history := []HistoryEntry{{ID: "msg-9", Number: 4, ThreadID: "thread-4"}}
	cases := []struct {
		name         string
		history      []HistoryEntry
		reservations []Reservation
		anchorID     string
		startNumber  int
		hasNumber    bool
		message      AnchorMessage
		parts        []string
		wantStatus   string
		wantMatched  int
		wantReason   string
	}{
		{
			// Pin 5: already committed — no-op, no network, no state write.
			name:         "already recovered",
			history:      []HistoryEntry{{ID: "msg-A", Number: 5, ThreadID: "thread-1"}},
			reservations: []Reservation{happy},
			anchorID:     "msg-A",
			wantStatus:   StatusAlreadyRecovered,
			wantMatched:  -1,
		},
		{
			// Pin 4: recorded anchor — the one-call happy path.
			name:         "adopt by recorded anchor",
			history:      history,
			reservations: []Reservation{pendingReservation(2), happy},
			anchorID:     "msg-A",
			message:      AnchorMessage{ID: "msg-A", Content: firstPartOf(happy)},
			parts:        []string{firstPartOf(happy), "tail"},
			wantStatus:   StatusAdopt,
			wantMatched:  1,
		},
		{
			// Pin 8: number-matched adoption gated on exact content.
			name:         "adopt by number with matching content",
			history:      history,
			reservations: []Reservation{pending},
			anchorID:     "msg-A",
			startNumber:  5,
			hasNumber:    true,
			message:      AnchorMessage{ID: "msg-A", Content: firstPartOf(pending)},
			parts:        []string{firstPartOf(pending)},
			wantStatus:   StatusAdopt,
			wantMatched:  0,
		},
		{
			// Pin 8 refusal: content differs — refusing to adopt.
			name:         "refuse differing content",
			history:      history,
			reservations: []Reservation{pending},
			anchorID:     "msg-A",
			startNumber:  5,
			hasNumber:    true,
			message:      AnchorMessage{ID: "msg-A", Content: "Someone else's message entirely."},
			parts:        []string{firstPartOf(pending)},
			wantStatus:   StatusRefuse,
			wantMatched:  -1,
			wantReason:   "does not match",
		},
		{
			// Pin 8 refusal: no number, no recorded anchor — the caller
			// must name the reservation before any content check runs.
			name:         "refuse missing start number",
			history:      history,
			reservations: []Reservation{pending},
			anchorID:     "msg-A",
			wantStatus:   StatusRefuse,
			wantMatched:  -1,
			wantReason:   "start_number",
		},
		{
			// Pin 8 refusal: nothing pending matches the anchor and it is
			// in no history entry.
			name:         "refuse unknown anchor",
			history:      history,
			reservations: []Reservation{happy},
			anchorID:     "msg-Z",
			startNumber:  7,
			hasNumber:    true,
			wantStatus:   StatusRefuse,
			wantMatched:  -1,
			wantReason:   "no pending start",
		},
		{
			// Pin 7 shape: the anchor could not be fetched (empty content),
			// so adoption by number must not happen — an unprovable anchor
			// is refused before any write.
			name:         "refuse unfetched anchor",
			history:      history,
			reservations: []Reservation{pending},
			anchorID:     "msg-A",
			startNumber:  5,
			hasNumber:    true,
			parts:        []string{firstPartOf(pending)},
			wantStatus:   StatusRefuse,
			wantMatched:  -1,
			wantReason:   "does not match",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := Decide(tc.history, tc.reservations, tc.anchorID, tc.startNumber, tc.hasNumber, tc.message, tc.parts)
			if got.Status != tc.wantStatus || got.Matched != tc.wantMatched {
				t.Fatalf("Decide() = %+v, want status %s matched %d", got, tc.wantStatus, tc.wantMatched)
			}
			if tc.wantReason != "" && !strings.Contains(got.Reason, tc.wantReason) {
				t.Errorf("Decide().Reason = %q, want it to contain %q", got.Reason, tc.wantReason)
			}
			if tc.wantStatus == StatusAlreadyRecovered && (got.Entry.ID != tc.anchorID || got.Entry.Number != 5) {
				t.Errorf("Decide().Entry = %+v, want the anchor's history row", got.Entry)
			}
		})
	}
}

func TestDecideMatchesPythonRefusalPhrases(t *testing.T) {
	// The refusal phrases are contract: the Python result's error field
	// must carry the same reason strings the Go contract states, so a
	// consumer can match on them without knowing which twin produced it.
	pending := pendingReservation(5)
	noNumber := Decide(nil, []Reservation{pending}, "msg-A", 0, false, AnchorMessage{}, nil)
	if !strings.Contains(noNumber.Reason, "content is verified before adopting") {
		t.Errorf("no-number refusal = %q, want the verification note", noNumber.Reason)
	}
	unknown := Decide(nil, []Reservation{pending}, "msg-Z", 7, true, AnchorMessage{}, nil)
	if !strings.Contains(unknown.Reason, "matches no pending start and no history entry") {
		t.Errorf("unknown-anchor refusal = %q, want the no-match phrase", unknown.Reason)
	}
	mismatch := Decide(
		nil, []Reservation{pending}, "msg-A", 5, true,
		AnchorMessage{ID: "msg-A", Content: "other"}, []string{"first"},
	)
	if !strings.Contains(mismatch.Reason, "content does not match the first delivery part of start #5") {
		t.Errorf("mismatch refusal = %q, want the mismatch phrase with the number", mismatch.Reason)
	}
}

func TestStages(t *testing.T) {
	// The milestone ladder the Python _update_reservation persists; a
	// stage that appears outside it is a contract break, not a new state.
	want := map[string]string{
		StagePending:       "pending",
		StageAnchorPosted:  "anchor_posted",
		StageThreadCreated: "thread_created",
	}
	for got, expected := range want {
		if got != expected {
			t.Errorf("stage %q != %q", got, expected)
		}
	}
}

func TestItoa(t *testing.T) {
	cases := []struct {
		in   int
		want string
	}{
		{0, "0"}, {1, "1"}, {5, "5"}, {12, "12"}, {407, "407"}, {-5, "-5"},
	}
	for _, tc := range cases {
		if got := itoa(tc.in); got != tc.want {
			t.Errorf("itoa(%d) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

func TestComposeMessage(t *testing.T) {
	cases := []struct {
		message  string
		nextMove string
		want     string
	}{
		{"Hello.", "Stay bounded.", "Hello.\n\n*Where I'd take this:* Stay bounded."},
		{"Hello.", "", "Hello."},
	}
	for _, tc := range cases {
		if got := ComposeMessage(tc.message, tc.nextMove); got != tc.want {
			t.Errorf("ComposeMessage(%q, %q) = %q, want %q", tc.message, tc.nextMove, got, tc.want)
		}
	}
}

func TestSplitMessage(t *testing.T) {
	longPara := func(fill string, n int) string {
		return fill + strings.Repeat("x", n-1)
	}
	twoParas := longPara("A", 1200) + "\n\n" + longPara("B", 1200)
	cases := []struct {
		name     string
		content  string
		maxLen   int
		wantLen  int
		wantBody []string
	}{
		{
			// The pin fixtures: two 1200-char paragraphs under a 1950
			// budget split into one part per paragraph.
			name:     "two paragraphs",
			content:  twoParas,
			maxLen:   1928,
			wantLen:  2,
			wantBody: []string{longPara("A", 1200), longPara("B", 1200)},
		},
		{
			// Three paragraphs, same budget: greedy join keeps the last
			// two together when they fit; here none fit pairwise.
			name:     "three paragraphs none join",
			content:  longPara("A", 1200) + "\n\n" + longPara("B", 1200) + "\n\n" + longPara("C", 1200),
			maxLen:   1928,
			wantLen:  3,
			wantBody: []string{longPara("A", 1200), longPara("B", 1200), longPara("C", 1200)},
		},
		{
			// Short paragraphs join greedily into one part.
			name:     "short paragraphs join",
			content:  "one\ntwo\n\nthree",
			maxLen:   50,
			wantLen:  1,
			wantBody: []string{"one\ntwo\n\nthree"},
		},
		{
			// A single paragraph over the budget wraps at line breaks
			// first, never mid-line while a newline fits.
			name:     "oversized paragraph wraps",
			content:  strings.Repeat("line\n", 500),
			maxLen:   40,
			wantLen:  63,
			wantBody: nil,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := SplitMessage(tc.content, tc.maxLen)
			if len(got) != tc.wantLen {
				t.Fatalf("SplitMessage produced %d parts, want %d", len(got), tc.wantLen)
			}
			for i, part := range got {
				if runeLen(part) > tc.maxLen {
					t.Errorf("part %d has %d runes, budget %d", i, runeLen(part), tc.maxLen)
				}
			}
			if tc.wantBody != nil {
				for i, want := range tc.wantBody {
					if got[i] != want {
						t.Errorf("part %d = %q..., want %q...", i, got[i][:20], want[:20])
					}
				}
			}
		})
	}
	// A single oversized paragraph — no "\n\n" boundaries — reassembles
	// byte-for-byte: wrap cuts keep their separators inside the chunk.
	// (Across paragraph boundaries the separator is the boundary, exactly
	// as in the Python splitter.)
	for _, content := range []string{strings.Repeat("line\n", 500), strings.Repeat("x", 250)} {
		parts := SplitMessage(content, 40)
		if len(parts) < 2 {
			t.Fatalf("SplitMessage(%q...) = %d parts, want a split", content[:12], len(parts))
		}
		if strings.Join(parts, "") != content {
			t.Errorf("wrapped parts do not reassemble the body: %d parts", len(parts))
		}
	}
	// Multi-paragraph splits keep every paragraph's text intact even
	// though the separator lives only at the boundary.
	joined := strings.Join(SplitMessage(twoParas, 1928), "\n\n")
	if joined != twoParas {
		t.Errorf("paragraph parts joined with the separator drifted")
	}
}

func TestWrapOversizedPiece(t *testing.T) {
	// Newline wins over space when both are in the window.
	if got := WrapOversizedPiece("aaaa\nbbbb cccc\ndddd", 9); len(got) == 0 || got[0] != "aaaa\n" {
		t.Errorf("wrap at newline = %v, want first chunk %q", got, "aaaa\n")
	}
	// No newline: last space in the window.
	if got := WrapOversizedPiece("aa bb cc dd ee ff gg hh", 10); len(got) == 0 || got[0] != "aa bb cc " {
		t.Errorf("wrap at space = %v, want first chunk %q", got, "aa bb cc ")
	}
	// No separator at all: hard cut at the budget.
	if got := WrapOversizedPiece(strings.Repeat("x", 25), 10); len(got) != 3 {
		t.Errorf("hard cut = %v, want 3 chunks", got)
	}
	// Within budget: untouched.
	if got := WrapOversizedPiece("short", 10); len(got) != 1 || got[0] != "short" {
		t.Errorf("short = %v, want [short]", got)
	}
}

func TestSplitDelivery(t *testing.T) {
	prefix := "<@123456789012345678>\n"
	body := strings.Repeat("A", 1200) + "\n\n" + strings.Repeat("B", 1200) + "\n\n" + strings.Repeat("C", 1200)
	parts := SplitDelivery(body, "123456789012345678")
	if len(parts) != 3 {
		t.Fatalf("SplitDelivery produced %d parts, want 3", len(parts))
	}
	if parts[0] != prefix+strings.Repeat("A", 1200) {
		t.Errorf("first part does not carry the mention prefix beside opening text")
	}
	for i, part := range parts {
		if runeLen(part) > MaxMessageLen {
			t.Errorf("part %d has %d runes, over the message limit", i, runeLen(part))
		}
	}
	// Empty mention: body delivered unchanged, no prefix.
	if got := SplitDelivery("plain", ""); len(got) != 1 || got[0] != "plain" {
		t.Errorf("SplitDelivery(no mention) = %v, want [plain]", got)
	}
	// The first part is the anchor content adoption verifies against.
	if got := SplitDelivery(body, "123456789012345678"); got[0] != prefix+strings.Repeat("A", 1200) {
		t.Errorf("anchor part drifted: %q...", got[0][:30])
	}
}

func TestPendingSummaryAndRecoverHint(t *testing.T) {
	rows := PendingSummary([]Reservation{
		{Number: 1, Kind: "idea", Stage: StagePending},
		{Number: 3, Kind: "feedback", Stage: StageAnchorPosted, AnchorID: "msg-77"},
		{Number: 4, Kind: "idea", Stage: StageThreadCreated, AnchorID: "msg-8", ThreadID: "thread-8"},
	})
	if len(rows) != 3 || rows[0].AnchorID != "" || rows[2].ThreadID != "thread-8" {
		t.Fatalf("PendingSummary = %+v", rows)
	}
	// Pin 2 phrasing: an anchor id answers with the one-call recover.
	hint := RecoverHint(rows)
	if !strings.Contains(hint, "action='recover' and anchor_id='msg-77'") {
		t.Errorf("hint = %q, want the anchor-id recover call", hint)
	}
	// Pin 3 phrasing: no anchor recorded yet — number plus inbox id.
	noAnchor := RecoverHint(rows[:1])
	if !strings.Contains(noAnchor, "start_number=1") || !strings.Contains(noAnchor, "content is verified before adopting") {
		t.Errorf("hint = %q, want the start_number recover call", noAnchor)
	}
	if RecoverHint(nil) != "" {
		t.Errorf("empty summary must give an empty hint")
	}
}

func TestEnrichFailure(t *testing.T) {
	base := map[string]any{"success": false, "error": "HTTP error: 500"}
	// Pin: unlocked failures stay unchanged — enrichment only when a
	// pending summary exists.
	untouched := EnrichFailure(map[string]any{"success": false, "error": "HTTP error: 500"}, nil)
	if len(untouched) != 2 {
		t.Errorf("empty summary enriched the result: %v", untouched)
	}
	enriched := EnrichFailure(base, []PendingRow{{Number: 1, Stage: StageAnchorPosted, AnchorID: "msg-1"}})
	if _, ok := enriched["pending_start"]; !ok {
		t.Fatalf("pending_start missing: %v", enriched)
	}
	if hint, _ := enriched["recover_hint"].(string); !strings.Contains(hint, "anchor_id='msg-1'") {
		t.Errorf("recover_hint = %v, want the anchor phrasing", enriched["recover_hint"])
	}
	// thread_id appears only once a milestone recorded one.
	withThread := EnrichFailure(map[string]any{}, []PendingRow{{Number: 2, Stage: StageThreadCreated, ThreadID: "thread-2"}})
	row := withThread["pending_start"].([]map[string]any)[0]
	if _, has := row["thread_id"]; !has {
		t.Errorf("thread_id missing from a thread_created row: %v", row)
	}
}
