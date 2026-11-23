package servingidentity

import (
	"encoding/json"
	"strings"
)

// The code-delta note for a serving-identity transition, the Go twin of
// gateway/serving_codedelta.py: a process handoff whose predecessor ran a
// DIFFERENT build tells the conversation what changed between the two
// builds — the commit subjects and the diff stat — beside the process
// transition note. The delta is never fabricated: an unrecorded/unreadable
// predecessor build or a failing git render renders a short
// "could not be computed" note instead.
//
// Contract (shared with the Python twin, pinned by the shared honesty
// table):
//   - CodeDeltaDecision: "none" for no parseable predecessor record or an
//     unusable CURRENT build (silence — a build whose identity cannot be
//     read can never name the delta, and pip installs without git would run
//     this branch every turn, choking on a could-not-compute repeat nobody
//     can settle); "unprovable" when the record's code_sha is absent/blank
//     (a pre-feature record) or present but unusable; "none" when the
//     record's sha equals the current one (same build, or the delta channel
//     is already settled); "delta" otherwise.
//   - The decision keys on the record's sha proof, not on transition state:
//     the stage step rewrites pid/start to this process while the sha proof
//     stays at its previous value, so a turn that crashes before its
//     api_content stamp re-emits the delta next turn (the same ack-gated
//     proof discipline as the note proof).
//   - The whole assembled note is capped at MaxNoteChars with head+tail
//     truncation and an explicit marker, so the leading framing and the
//     trailing terminal pointer both survive an enormous diff. The budget
//     is CODE POINTS (rune slicing), matching the Python twin's len()-based
//     slicing byte for byte.
const (
	// MaxNoteChars is the hard cap for the whole code-delta note.
	MaxNoteChars = 4000

	// TruncationMarker is the explicit elision marker a capped note carries.
	TruncationMarker = "[… truncated — the full diff is available via terminal …]"

	// ReasonPreviousNotRecorded: the record predates sha recording.
	ReasonPreviousNotRecorded = "the previous build was not recorded"
	// ReasonPreviousUnreadable: the record's sha exists but is unusable.
	ReasonPreviousUnreadable = "the previous build's recorded identity could not be read"
	// ReasonGitFailed: git could not read the repository.
	ReasonGitFailed = "git could not read this repository"
)

// DeltaRender is the injectable git provider: the commits and diff stat
// between two build shas, or ok=false when git could not read the repo.
type DeltaRender func(oldSHA, newSHA string) (commits, stat string, ok bool)

// usableSHA reports a provable build identity: exactly 40 lowercase hex
// characters.
func usableSHA(sha string) bool {
	if len(sha) != 40 {
		return false
	}
	for _, char := range sha {
		if !strings.ContainsRune("0123456789abcdef", char) {
			return false
		}
	}
	return true
}

// parseRecordObject leniently decodes ANY JSON object, mirroring the Python
// twin's isinstance(previous, dict) gate: unlike parseStoredRecord it does
// not require a positive pid, because the delta decision only needs the
// code_sha field to exist — a record too corrupt to prove a process can
// still prove a build.
func parseRecordObject(raw string) (map[string]any, bool) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, false
	}
	var record map[string]any
	if err := json.Unmarshal([]byte(raw), &record); err != nil {
		return nil, false
	}
	return record, true
}

// recordCodeSHA extracts the stored record's build sha field: "" when
// absent, null, or non-string (the reason split below treats those as
// not-recorded vs could-not-read exactly as the Python twin does).
func recordCodeSHA(record map[string]any) string {
	value, present := record["code_sha"]
	if !present || value == nil {
		return ""
	}
	sha, _ := value.(string)
	return sha
}

// CodeDeltaDecision renders "none", "unprovable", or "delta" for this turn
// from the stored served-by record and the current build's sha. Pure: no
// git, no store. Parity twin of
// gateway.serving_codedelta.code_delta_decision, pinned by the shared
// honesty table.
func CodeDeltaDecision(storedRaw, currentSHA string) string {
	record, ok := parseRecordObject(storedRaw)
	if !ok {
		return "none"
	}
	if !usableSHA(currentSHA) {
		return "none"
	}
	oldSHA := recordCodeSHA(record)
	if oldSHA == "" {
		return "unprovable"
	}
	if !usableSHA(oldSHA) {
		return "unprovable"
	}
	if oldSHA == currentSHA {
		return "none"
	}
	return "delta"
}

func shortSHA(sha string) string {
	if len(sha) > 8 {
		return sha[:8]
	}
	return sha
}

// RenderCodeDeltaNote renders the delta framing: both builds' short shas,
// the commits, the diff stat, and the one-line terminal pointer carrying
// the FULL shas. Parity twin of render_code_delta_note (byte-identical).
func RenderCodeDeltaNote(oldSHA, newSHA, commits, stat string) string {
	return "[System note: The Hermes gateway build changed since the previous turns of " +
		"this conversation: they ran build " + shortSHA(oldSHA) + ", this process runs build " +
		shortSHA(newSHA) + ". Commits between the two builds:\n" + commits + "\n" +
		"Changed files:\n" + stat + "\n" +
		"The full diff is available via terminal (git diff " + oldSHA + ".." + newSHA + ").]"
}

// UnavailableNote renders a could-not-compute card — truthful about WHY,
// never a fabricated diff. A git failure still names both builds and points
// at the terminal command; an unprovable predecessor names only this build
// (there is nothing to diff against). Parity twin of unavailable_note
// (byte-identical).
func UnavailableNote(oldSHA, newSHA, reason string) string {
	if reason == ReasonGitFailed {
		return "[System note: The Hermes gateway build changed since the previous turns of " +
			"this conversation: they ran build " + shortSHA(oldSHA) + ", this process runs build " +
			shortSHA(newSHA) + ". The code delta between the two builds could not be " +
			"computed: " + reason + ". The full diff is available via terminal " +
			"(git diff " + oldSHA + ".." + newSHA + ").]"
	}
	return "[System note: The code delta between the build that served the previous turns " +
		"and this process's build (" + shortSHA(newSHA) + ") could not be computed: " +
		reason + ".]"
}

// CapHeadTail truncates text to at most limit CODE POINTS, keeping head and
// tail around the explicit truncation marker: head keeps 2/3 of the room,
// tail 1/3, so the leading framing and the trailing terminal pointer both
// survive. Rune slicing matches the Python twin's len()-based slicing byte
// for byte.
func CapHeadTail(text string, limit int) string {
	runes := []rune(text)
	if len(runes) <= limit {
		return text
	}
	marker := []rune(TruncationMarker)
	keep := limit - len(marker)
	if keep < 0 {
		keep = 0
	}
	head := keep * 2 / 3
	tail := keep - head
	return string(runes[:head]) + TruncationMarker + string(runes[len(runes)-tail:])
}

// CodeDeltaNote renders the code-delta note for this turn, or "" when
// nothing rides. render is the injectable git provider (nil behaves as a
// failed git read: the honest could-not-compute form). Parity twin of
// gateway.serving_codedelta.code_delta_note, pinned by the shared honesty
// table.
func CodeDeltaNote(storedRaw, currentSHA string, render DeltaRender) string {
	decision := CodeDeltaDecision(storedRaw, currentSHA)
	if decision == "none" {
		return ""
	}
	oldSHA := ""
	if record, ok := parseRecordObject(storedRaw); ok {
		oldSHA = recordCodeSHA(record)
	}
	if decision == "unprovable" {
		reason := ReasonPreviousUnreadable
		if oldSHA == "" {
			reason = ReasonPreviousNotRecorded
		}
		return UnavailableNote(oldSHA, currentSHA, reason)
	}
	var commits, stat string
	rendered := false
	if render != nil {
		commits, stat, rendered = render(oldSHA, currentSHA)
	}
	if !rendered {
		return UnavailableNote(oldSHA, currentSHA, ReasonGitFailed)
	}
	return CapHeadTail(RenderCodeDeltaNote(oldSHA, currentSHA, commits, stat), MaxNoteChars)
}
