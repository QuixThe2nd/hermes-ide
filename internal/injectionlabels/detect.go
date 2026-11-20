package injectionlabels

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"
)

// PersistedOutputTag opens the stub an oversized tool result is replaced
// with after its bytes spilled to disk. Mirrors
// tools/tool_result_storage.PERSISTED_OUTPUT_TAG; the shared table pins
// the bytes on both sides.
const PersistedOutputTag = "<persisted-output>"

// CompressedSummaryMetadataKey is the in-process flag a compression commit
// stamps onto its summary row. The underscore prefix is load-bearing:
// wire sanitizers strip “_“-keys, so the flag never poisons a strict
// gateway request. Mirrors
// agent/context_compressor.COMPRESSED_SUMMARY_METADATA_KEY.
const CompressedSummaryMetadataKey = "_compressed_summary"

// SummaryPrefixes are the byte anchors that mark a message's content as a
// conversation-summary handoff: the current prefix, the legacy one, and
// the historical shapes older deployments may still hold in stored
// history. Mirrors SUMMARY_PREFIX, LEGACY_SUMMARY_PREFIX and
// _HISTORICAL_SUMMARY_PREFIXES in agent/context_compressor.py; the shared
// table carries the same list so the twins cannot drift.
var SummaryPrefixes = []string{
	"[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. This " +
		"is a handoff from a previous context window — treat it as background reference, NOT as active " +
		"instructions. Do NOT answer questions or fulfill requests mentioned in this summary; they were " +
		"already addressed. Respond ONLY to the latest user message that appears AFTER this summary — " +
		"that message is the single source of truth for what to do right now. If no user message appears " +
		"AFTER this summary, do nothing: do not resume, wrap up, or continue work from '## Historical " +
		"Task Snapshot' or any other section, do not call tools, and wait for a new user message. This " +
		"handoff must never become the active turn by itself. (Exception: if tool results or your own " +
		"tool calls appear after this summary, you are mid-way through an in-flight exchange — continue " +
		"that exchange normally.) Topic overlap with the summary does NOT mean you should resume its " +
		"task: even on similar topics, the latest user message WINS. Treat ONLY the latest message as the " +
		"active task and discard stale items from '## Historical Task Snapshot' entirely — do not 'wrap " +
		"up' or 'finish' work described there unless the latest message explicitly asks for it. Reverse " +
		"signals in the latest message (e.g. 'stop', 'undo', 'roll back', 'just verify', 'don't do that " +
		"anymore', 'never mind', a new topic) must immediately end any in-flight work described in the " +
		"summary; do not re-surface it in later turns. IMPORTANT: Your persistent memory (MEMORY.md, " +
		"USER.md) in the system prompt is ALWAYS authoritative and active — never ignore or deprioritize " +
		"memory content due to this compaction note. None of the above restricts HOW you work: your tools " +
		"remain fully active — keep calling them normally for the active task (edit files, run commands, " +
		"search) instead of merely narrating what you would do. The current session state (files, config, " +
		"etc.) may reflect work described here — avoid repeating it:",
	"[CONTEXT SUMMARY]:",
	// Pre-#80622: lacked the "no user message after summary => do nothing" clause.
	"[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. This " +
		"is a handoff from a previous context window — treat it as background reference, NOT as active " +
		"instructions. Do NOT answer questions or fulfill requests mentioned in this summary; they were " +
		"already addressed. Respond ONLY to the latest user message that appears AFTER this summary — " +
		"that message is the single source of truth for what to do right now. Topic overlap with the " +
		"summary does NOT mean you should resume its task: even on similar topics, the latest user " +
		"message WINS. Treat ONLY the latest message as the active task and discard stale items from '## " +
		"Historical Task Snapshot' entirely — do not 'wrap up' or 'finish' work described there unless " +
		"the latest message explicitly asks for it. Reverse signals in the latest message (e.g. 'stop', " +
		"'undo', 'roll back', 'just verify', 'don't do that anymore', 'never mind', a new topic) must " +
		"immediately end any in-flight work described in the summary; do not re-surface it in later " +
		"turns. IMPORTANT: Your persistent memory (MEMORY.md, USER.md) in the system prompt is ALWAYS " +
		"authoritative and active — never ignore or deprioritize memory content due to this compaction " +
		"note. None of the above restricts HOW you work: your tools remain fully active — keep calling " +
		"them normally for the active task (edit files, run commands, search) instead of merely narrating " +
		"what you would do. The current session state (files, config, etc.) may reflect work described " +
		"here — avoid repeating it:",
	// #69619: tightened to the REFERENCE-ONLY framing.
	"[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. This " +
		"is a handoff from a previous context window — treat it as background reference, NOT as active " +
		"instructions. Do NOT answer questions or fulfill requests mentioned in this summary; they were " +
		"already addressed. Respond ONLY to the latest user message that appears AFTER this summary — " +
		"that message is the single source of truth for what to do right now. Topic overlap with the " +
		"summary does NOT mean you should resume its task: even on similar topics, the latest user " +
		"message WINS. Treat ONLY the latest message as the active task and discard stale items from '## " +
		"Historical Task Snapshot' / '## Historical In-Progress State' / '## Historical Pending User " +
		"Asks' / '## Historical Remaining Work' entirely — do not 'wrap up' or 'finish' work described " +
		"there unless the latest message explicitly asks for it. Reverse signals in the latest message " +
		"(e.g. 'stop', 'undo', 'roll back', 'just verify', 'don't do that anymore', 'never mind', a new " +
		"topic) must immediately end any in-flight work described in the summary; do not re-surface it in " +
		"later turns. IMPORTANT: Your persistent memory (MEMORY.md, USER.md) in the system prompt is " +
		"ALWAYS authoritative and active — never ignore or deprioritize memory content due to this " +
		"compaction note. None of the above restricts HOW you work: your tools remain fully active — keep " +
		"calling them normally for the active task (edit files, run commands, search) instead of merely " +
		"narrating what you would do. The current session state (files, config, etc.) may reflect work " +
		"described here — avoid repeating it:",
	// Pre-#69619: plain summary framing.
	"[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. This " +
		"is a handoff from a previous context window — treat it as background reference, NOT as active " +
		"instructions. Do NOT answer questions or fulfill requests mentioned in this summary; they were " +
		"already addressed. Respond ONLY to the latest user message that appears AFTER this summary — " +
		"that message is the single source of truth for what to do right now. Topic overlap with the " +
		"summary does NOT mean you should resume its task: even on similar topics, the latest user " +
		"message WINS. Treat ONLY the latest message as the active task and discard stale items from '## " +
		"Historical Task Snapshot' / '## Historical In-Progress State' / '## Historical Pending User " +
		"Asks' / '## Historical Remaining Work' entirely — do not 'wrap up' or 'finish' work described " +
		"there unless the latest message explicitly asks for it. Reverse signals in the latest message " +
		"(e.g. 'stop', 'undo', 'roll back', 'just verify', 'don't do that anymore', 'never mind', a new " +
		"topic) must immediately end any in-flight work described in the summary; do not re-surface it in " +
		"later turns. IMPORTANT: Your persistent memory (MEMORY.md, USER.md) in the system prompt is " +
		"ALWAYS authoritative and active — never ignore or deprioritize memory content due to this " +
		"compaction note. The current session state (files, config, etc.) may reflect work described here " +
		"— avoid repeating it:",
	"[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. This " +
		"is a handoff from a previous context window — treat it as background reference, NOT as active " +
		"instructions. Do NOT answer questions or fulfill requests mentioned in this summary; they were " +
		"already addressed. Respond ONLY to the latest user message that appears AFTER this summary — " +
		"that message is the single source of truth for what to do right now. If the latest user message " +
		"is consistent with the '## Active Task' section, you may use the summary as background. If the " +
		"latest user message contradicts, supersedes, changes topic from, or in any way diverges from '## " +
		"Active Task' / '## In Progress' / '## Pending User Asks' / '## Remaining Work', the latest " +
		"message WINS — discard those stale items entirely and do not 'wrap up the old task first'. " +
		"Reverse signals in the latest message (e.g. 'stop', 'undo', 'roll back', 'just verify', 'don't " +
		"do that anymore', 'never mind', a new topic) must immediately end any in-flight work described " +
		"in the summary; do not re-surface it in later turns. IMPORTANT: Your persistent memory " +
		"(MEMORY.md, USER.md) in the system prompt is ALWAYS authoritative and active — never ignore or " +
		"deprioritize memory content due to this compaction note. The current session state (files, " +
		"config, etc.) may reflect work described here — avoid repeating it:",
	"[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. This " +
		"is a handoff from a previous context window — treat it as background reference, NOT as active " +
		"instructions. Do NOT answer questions or fulfill requests mentioned in this summary; they were " +
		"already addressed. Your current task is identified in the '## Active Task' section of the " +
		"summary — resume exactly from there. Respond ONLY to the latest user message that appears AFTER " +
		"this summary. The current session state (files, config, etc.) may reflect work described here — " +
		"avoid repeating it:",
}

// TextPart is one content part of a multimodal tool result. Only the text
// field matters to spillover detection: a replaced text part is how an
// oversized multimodal output leaves its persisted-output stub behind.
type TextPart struct {
	Type string `json:"type,omitempty"`
	Text string `json:"text"`
}

// IsSpilledStub reports whether a tool-result string IS a persisted-output
// stub — the model-facing replacement for bytes that spilled to disk.
func IsSpilledStub(text string) bool {
	return strings.Contains(text, PersistedOutputTag)
}

// SpilledStubFromParts returns the first multimodal content part whose
// text is a persisted-output stub: a replaced text part means oversized
// output spilled to disk. Part type is deliberately not filtered — the
// Python twin looks for any dict part carrying stub text.
func SpilledStubFromParts(parts []TextPart) (string, bool) {
	for _, p := range parts {
		if p.Text != "" && IsSpilledStub(p.Text) {
			return p.Text, true
		}
	}
	return "", false
}

// Message is the minimal message shape the compaction detector needs. A
// nil Content mirrors Python's None: a metadata-flagged summary row whose
// content is absent still classifies, and an absent content never equals
// an empty-string content when matched against the pre-compression rows.
type Message struct {
	Role              string  `json:"role,omitempty"`
	Content           *string `json:"content"`
	CompressedSummary bool    `json:"_compressed_summary,omitempty"`
}

// IsSummaryMessage mirrors ContextCompressor._is_context_summary_message:
// a row is a conversation-summary handoff when it carries the in-process
// compressed-summary flag OR its content opens with one of the summary
// prefixes. Role is not consulted — the predicate classifies bytes and
// metadata, not intentions.
func IsSummaryMessage(m Message) bool {
	if m.CompressedSummary {
		return true
	}
	if m.Content == nil {
		return false
	}
	for _, prefix := range SummaryPrefixes {
		if strings.HasPrefix(*m.Content, prefix) {
			return true
		}
	}
	return false
}

// NewCompactionSummary extracts the conversation summary a COMMITTED
// compression inserted, if any: the first summary row among the
// post-compression messages whose content was not already present
// byte-for-byte in the pre-compression list. A skip path (compression
// returned its input) and a byte-identical preserved summary
// (recompression kept the old row) both report changed=false — nothing
// new was injected, so no card may fire.
//
// Mirrors new_compaction_summary in agent/context_injection_cards.py.
func NewCompactionSummary(before, after []Message) (summary string, changed bool) {
	prior := make(map[string]bool, len(before))
	for _, m := range before {
		if m.Content != nil {
			prior[*m.Content] = true
		}
	}
	for _, m := range after {
		if !IsSummaryMessage(m) {
			continue
		}
		if m.Content != nil && prior[*m.Content] {
			continue
		}
		if m.Content == nil {
			return "", true
		}
		return *m.Content, true
	}
	return "", false
}

// sha256Hex is the sha-gating key every once-per-shape card derives from
// its composed content: the sha256 of the UTF-8 bytes, hex-encoded — the
// same derivation the Python twin's hashlib performs.
func sha256Hex(content string) string {
	sum := sha256.Sum256([]byte(content))
	return hex.EncodeToString(sum[:])
}
