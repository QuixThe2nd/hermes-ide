package servingidentity

import (
	"strings"
	"testing"
)

// Build shas for the code-delta tests — exact 40-hex forms, mirroring the
// Python twin's test constants (tests/gateway/test_serving_codedelta.py).
const (
	codedeltaOldSHA = "a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4"
	codedeltaNewSHA = "e5f60718e5f60718e5f60718e5f60718e5f60718"
)

// staticRender is a DeltaRender returning fixed commits/stat (the shape the
// shared table's code_delta_note cases describe).
func staticRender(commits, stat string) DeltaRender {
	return func(oldSHA, newSHA string) (string, string, bool) {
		return commits, stat, true
	}
}

// TestCodeDeltaDecisionMatrix pins the decision ladder: a non-record and an
// unreadable CURRENT build are silence ("none" — a build whose identity
// cannot be read can never name the delta, and pip installs must stay quiet
// every turn, not choke on a could-not-compute repeat nobody can settle); an
// unrecorded or unreadable PREDECESSOR build is "unprovable" (the honest
// could-not-compute note); a matching sha is "none"; a differing sha is the
// "delta".
func TestCodeDeltaDecisionMatrix(t *testing.T) {
	legacy := `{"pid":709505,"start_time":3001234}`
	cases := []struct {
		name       string
		stored     string
		currentSHA string
		want       string
	}{
		{"no record", "", codedeltaNewSHA, "none"},
		{"corrupt record", "not-json{", codedeltaNewSHA, "none"},
		{"same build", recordWithSHA(t, codedeltaOldSHA), codedeltaOldSHA, "none"},
		{"changed build", recordWithSHA(t, codedeltaOldSHA), codedeltaNewSHA, "delta"},
		{"pre-feature record without sha", legacy, codedeltaNewSHA, "unprovable"},
		{"null sha", `{"pid":1,"start_time":2,"code_sha":null}`, codedeltaNewSHA, "unprovable"},
		{"blank sha", `{"pid":1,"start_time":2,"code_sha":""}`, codedeltaNewSHA, "unprovable"},
		{"short sha", `{"pid":1,"start_time":2,"code_sha":"abc"}`, codedeltaNewSHA, "unprovable"},
		{"non-hex sha", `{"pid":1,"start_time":2,"code_sha":"nothex!"}`, codedeltaNewSHA, "unprovable"},
		{"non-string sha", `{"pid":1,"start_time":2,"code_sha":12345}`, codedeltaNewSHA, "unprovable"},
		{"65-char sha", `{"pid":1,"start_time":2,"code_sha":"` + strings.Repeat("a", 65) + `"}`, codedeltaNewSHA, "unprovable"},
		{"unreadable current build", recordWithSHA(t, codedeltaOldSHA), "", "none"},
		{"short current build", recordWithSHA(t, codedeltaOldSHA), "short", "none"},
		{"legacy record, unreadable current", legacy, "", "none"},
	}
	for _, c := range cases {
		if got := CodeDeltaDecision(c.stored, c.currentSHA); got != c.want {
			t.Errorf("%s: got %q, want %q", c.name, got, c.want)
		}
	}
}

// recordWithSHA renders a parseable predecessor record carrying a build sha.
func recordWithSHA(t *testing.T, sha string) string {
	t.Helper()
	return EncodeRecord(Snapshot{PID: 709505, StartTime: 3001234}, "", sha)
}

// TestCodeDeltaNoteFraming pins the delta note bytes: it names both builds'
// short shas, the commit subjects, the diff stat, and the one-line terminal
// pointer carrying the FULL shas.
func TestCodeDeltaNoteFraming(t *testing.T) {
	note := CodeDeltaNote(recordWithSHA(t, codedeltaOldSHA), codedeltaNewSHA,
		staticRender("abc123 fix gateway\ndef456 fix tests", " 2 files changed, 3 insertions(+)"))
	if !strings.HasPrefix(note, "[System note: The Hermes gateway build changed") {
		t.Errorf("framing: %q", note)
	}
	for _, want := range []string{
		codedeltaOldSHA[:8], codedeltaNewSHA[:8],
		"abc123 fix gateway", "def456 fix tests", "2 files changed",
		"git diff " + codedeltaOldSHA + ".." + codedeltaNewSHA,
	} {
		if !strings.Contains(note, want) {
			t.Errorf("delta note must carry %q: %q", want, note)
		}
	}
}

// TestRenderCodeDeltaNoteIsTheFramingTwin pins the pure framing builder both
// the runtime and the shared table render through.
func TestRenderCodeDeltaNoteIsTheFramingTwin(t *testing.T) {
	note := RenderCodeDeltaNote(codedeltaOldSHA, codedeltaNewSHA,
		"abc123 fix gateway", " 2 files changed")
	if !strings.HasPrefix(note, "[System note: The Hermes gateway build changed") {
		t.Errorf("framing: %q", note)
	}
	if !strings.Contains(note, "git diff "+codedeltaOldSHA+".."+codedeltaNewSHA) {
		t.Errorf("terminal pointer: %q", note)
	}
}

// TestCodeDeltaNoteGitFailureIsCouldNotCompute pins the never-fabricate rule:
// a failing or missing git render names both builds, says the delta could not
// be computed, and never carries commit or diff lines — but still points at
// the terminal command that would show it.
func TestCodeDeltaNoteGitFailureIsCouldNotCompute(t *testing.T) {
	note := CodeDeltaNote(recordWithSHA(t, codedeltaOldSHA), codedeltaNewSHA, nil)
	if !strings.Contains(note, "could not be computed") {
		t.Errorf("git failure must say so: %q", note)
	}
	if strings.Contains(note, "Commits between the two builds") {
		t.Errorf("no fabricated commits: %q", note)
	}
	if !strings.Contains(note, "git diff "+codedeltaOldSHA+".."+codedeltaNewSHA) {
		t.Errorf("git failure still points at the terminal: %q", note)
	}
}

// TestUnavailableNoteReasons pins the three could-not-compute forms: git
// failure names both builds and the terminal command; an unrecorded or
// unreadable predecessor names only this build and the reason.
func TestUnavailableNoteReasons(t *testing.T) {
	notRecorded := UnavailableNote("", codedeltaNewSHA, ReasonPreviousNotRecorded)
	for _, want := range []string{"could not be computed", "was not recorded", codedeltaNewSHA[:8]} {
		if !strings.Contains(notRecorded, want) {
			t.Errorf("not-recorded form must carry %q: %q", want, notRecorded)
		}
	}
	unreadable := UnavailableNote(codedeltaOldSHA, codedeltaNewSHA, ReasonPreviousUnreadable)
	for _, want := range []string{"could not be computed", "could not be read"} {
		if !strings.Contains(unreadable, want) {
			t.Errorf("unreadable form must carry %q: %q", want, unreadable)
		}
	}
	gitFailed := UnavailableNote(codedeltaOldSHA, codedeltaNewSHA, ReasonGitFailed)
	for _, want := range []string{
		"could not be computed", codedeltaOldSHA[:8], codedeltaNewSHA[:8],
		"git diff " + codedeltaOldSHA + ".." + codedeltaNewSHA,
	} {
		if !strings.Contains(gitFailed, want) {
			t.Errorf("git-failure form must carry %q: %q", want, gitFailed)
		}
	}
}

// TestCodeDeltaNoteUnprovableRendersTheMatchingUnavailableNote pins the
// reason split: a record with no sha at all was never recorded; a record
// with a junk sha could not be read. Both are could-not-compute cards.
func TestCodeDeltaNoteUnprovableRendersTheMatchingUnavailableNote(t *testing.T) {
	absent := CodeDeltaNote(`{"pid":1,"start_time":2}`, codedeltaNewSHA, nil)
	if !strings.Contains(absent, "was not recorded") {
		t.Errorf("absent sha must say not recorded: %q", absent)
	}
	corrupt := CodeDeltaNote(`{"pid":1,"start_time":2,"code_sha":"garbage"}`,
		codedeltaNewSHA, nil)
	if !strings.Contains(corrupt, "could not be read") {
		t.Errorf("junk sha must say could not be read: %q", corrupt)
	}
}

// TestCapHeadTailMarker pins the hard cap: short text passes through
// untouched; long text keeps head AND tail around an explicit truncation
// marker, and the result never exceeds the cap (code-point budget, so the
// bytes match the Python twin's len()-based slicing exactly).
func TestCapHeadTailMarker(t *testing.T) {
	if got := CapHeadTail("short note", MaxNoteChars); got != "short note" {
		t.Errorf("short text must pass through: %q", got)
	}
	exact := strings.Repeat("x", MaxNoteChars)
	if got := CapHeadTail(exact, MaxNoteChars); got != exact {
		t.Errorf("exactly-at-cap text must pass through (got %d runes)", len([]rune(got)))
	}
	body := "HEAD" + strings.Repeat("a", 3000) + strings.Repeat("b", 3000) + "TAIL"
	capped := CapHeadTail(body, MaxNoteChars)
	if runes := len([]rune(capped)); runes > MaxNoteChars {
		t.Errorf("cap exceeded: %d runes", runes)
	}
	if !strings.HasPrefix(capped, "HEAD") || !strings.HasSuffix(capped, "TAIL") {
		t.Errorf("both ends must survive: %q…%q", capped[:12], capped[len(capped)-12:])
	}
	if !strings.Contains(capped, "truncated") || !strings.Contains(capped, "terminal") {
		t.Errorf("explicit truncation marker missing: %q", capped)
	}
	if !strings.Contains(capped, "aaa") || !strings.Contains(capped, "bbb") {
		t.Errorf("content from before AND after the elided middle must survive")
	}
}

// TestCappedDeltaNoteWithinHardCap pins the whole-note cap: a huge git log
// and stat still render a note within the budget, with the marker present
// and the terminal pointer (the tail) intact.
func TestCappedDeltaNoteWithinHardCap(t *testing.T) {
	var commits, stat strings.Builder
	for i := 0; i < 400; i++ {
		commits.WriteString("commit subject line\n")
		stat.WriteString(" src/file changed | 10 ++++++ \n")
	}
	note := CodeDeltaNote(recordWithSHA(t, codedeltaOldSHA), codedeltaNewSHA,
		staticRender(commits.String(), stat.String()))
	if runes := len([]rune(note)); runes > MaxNoteChars {
		t.Errorf("note exceeded the hard cap: %d runes", runes)
	}
	if !strings.Contains(note, "truncated") {
		t.Errorf("truncation marker missing")
	}
	if !strings.HasSuffix(note,
		"git diff "+codedeltaOldSHA+".."+codedeltaNewSHA+").]") {
		t.Errorf("the terminal pointer must survive the cap: …%q", note[len(note)-80:])
	}
}

// TestStageKeepsRecordedSHAUntilAck pins the ack-gated delta proof: the stage
// write rewrites pid/start to the serving process but KEEPS the
// predecessor's recorded sha, so a turn that crashes before its sidecar stamp
// re-emits the delta on the next turn; only the stamp ack settles the sha.
func TestStageKeepsRecordedSHAUntilAck(t *testing.T) {
	predecessor := Snapshot{PID: 709505, StartTime: 3001234}
	successor := Snapshot{PID: 1531038, StartTime: 3005678, BuildSHA: codedeltaNewSHA}
	store := newMapStore(map[string]string{
		MetaKey("s"): EncodeRecord(predecessor, "", codedeltaOldSHA),
	})
	rendered := staticRender("abc123 fix", " 1 file changed")
	_, delta, pending := StageServedBy("s", successor, store.get, store.set, rendered)
	if delta == "" || !strings.Contains(delta, codedeltaOldSHA[:8]) {
		t.Fatalf("handoff with a changed build must render the delta, got %q", delta)
	}
	record, ok := parseStoredRecord(store.records[MetaKey("s")])
	if !ok || record.CodeSHA == nil || *record.CodeSHA != codedeltaOldSHA {
		t.Fatalf("stage must keep the predecessor's sha until the ack, got %+v", record)
	}
	// The turn never reaches its stamp: the next stage re-emits the delta.
	_, again, _ := StageServedBy("s", successor, store.get, store.set, rendered)
	if again != delta {
		t.Fatalf("unacked delta must re-emit, got %q", again)
	}
	// The stamp ack lands: the record settles on this build's sha and the
	// delta goes quiet.
	if !ConfirmServedNote("s", pending, codedeltaNewSHA, store.get, store.set) {
		t.Fatal("the stamp ack must settle the delta proof")
	}
	if _, quiet, _ := StageServedBy("s", successor, store.get, store.set, rendered); quiet != "" {
		t.Fatalf("settled build must ride no delta, got %q", quiet)
	}
}

// TestStageEstablishesFirstTurnSHA pins the first-turn rule: with no
// parseable predecessor the stage write establishes THIS build's sha (there
// is no predecessor delta to carry), and a same-process repeat never renders
// a delta.
func TestStageEstablishesFirstTurnSHA(t *testing.T) {
	current := Snapshot{PID: 1531038, StartTime: 3005678, BuildSHA: codedeltaNewSHA}
	store := newMapStore(nil)
	_, delta, _ := StageServedBy("s", current, store.get, store.set, staticRender("", ""))
	if delta != "" {
		t.Fatalf("first turn rides the delta-free path, got %q", delta)
	}
	record, ok := parseStoredRecord(store.records[MetaKey("s")])
	if !ok || record.CodeSHA == nil || *record.CodeSHA != codedeltaNewSHA {
		t.Fatalf("first turn must establish the build sha, got %+v", record)
	}
}

// TestStageUnusableCurrentSHAStaysDeltaFree pins the pip-install rule: a
// predecessor sha IS recorded but this build's identity is unreadable — the
// delta stays silent (no could-not-compute card that could never settle),
// and the record keeps the sha it had.
func TestStageUnusableCurrentSHAStaysDeltaFree(t *testing.T) {
	store := newMapStore(map[string]string{
		MetaKey("s"): EncodeRecord(Snapshot{PID: 709505, StartTime: 3001234}, "", codedeltaOldSHA),
	})
	current := Snapshot{PID: 1531038, StartTime: 3005678} // no BuildSHA: no git
	_, delta, _ := StageServedBy("s", current, store.get, store.set, staticRender("", ""))
	if delta != "" {
		t.Fatalf("unreadable current build must stay quiet, got %q", delta)
	}
	record, _ := parseStoredRecord(store.records[MetaKey("s")])
	if record.CodeSHA == nil || *record.CodeSHA != codedeltaOldSHA {
		t.Fatalf("record must keep the sha it had, got %+v", record)
	}
}

// TestStageLegacyRecordRendersCouldNotCompute pins the pre-feature record
// handoff: the predecessor predates sha recording, so the delta is the honest
// not-recorded card — and the ack settles it (the conversation WAS told).
func TestStageLegacyRecordRendersCouldNotCompute(t *testing.T) {
	store := newMapStore(map[string]string{
		MetaKey("s"): `{"pid":709505,"start_time":3001234}`,
	})
	current := Snapshot{PID: 1531038, StartTime: 3005678, BuildSHA: codedeltaNewSHA}
	_, delta, pending := StageServedBy("s", current, store.get, store.set, staticRender("", ""))
	if !strings.Contains(delta, "was not recorded") {
		t.Fatalf("legacy record must render the not-recorded card, got %q", delta)
	}
	if !ConfirmServedNote("s", pending, codedeltaNewSHA, store.get, store.set) {
		t.Fatal("the ack must settle even a could-not-compute card")
	}
	if _, quiet, _ := StageServedBy("s", current, store.get, store.set, staticRender("", "")); quiet != "" {
		t.Fatalf("settled could-not-compute must go quiet, got %q", quiet)
	}
}
