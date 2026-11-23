package injectionlabels

import (
	"strconv"
	"strings"
)

// Injection kinds produced by the card builders below. They are the keys of
// KindLabels; repeated here as typed constants so a builder can never emit a
// kind the display table does not know (and the fixture test pins the set).
const (
	KindSession      = "session"
	KindGoal         = "goal"
	KindSkill        = "skill"
	KindCompaction   = "compaction"
	KindSteer        = "steer"
	KindSubdir       = "subdirectory"
	KindSpillover    = "spillover"
	SourceUser       = "user"
	SourceSession    = "session"
	SourceToolResult = "tool_result"
)

// Card is the “context.injected“ progress-card payload every producer
// emits through the agent's tool-progress callback: additive display
// metadata naming WHAT was injected into the model's context — never the
// API-bound bytes themselves. Mirrors the payload shape assembled by
// agent/context_injection_cards.py (event_card / emit_card).
//
// The payload contract (shared with the Python twin and the shared table
// in testdata/labels.json):
//   - content — the injected bytes (or the composed inventory for the
//     session card), for the display layer's fenced preview;
//   - injected_chars — len(content) in Unicode characters, the display
//     heading's char count;
//   - sources — provenance of the injection (who caused it), defaulting
//     to a single source named after the first kind;
//   - kinds — the classification tags that pick the heading lines;
//   - meta — optional per-kind detail naming WHAT a kind carried (the
//     invoked skill's name).
type Card struct {
	Kinds         []string          `json:"kinds"`
	Content       string            `json:"content"`
	InjectedChars int               `json:"injected_chars"`
	Sources       []string          `json:"sources"`
	Meta          map[string]string `json:"meta,omitempty"`
}

func newCard(kind, content, source string, meta map[string]string) Card {
	return Card{
		Kinds:         []string{kind},
		Content:       content,
		InjectedChars: len([]rune(content)),
		Sources:       []string{source},
		Meta:          meta,
	}
}

// SteerCard cards a mid-turn steering row the user sent while a tool round
// was in flight: injected as an out-of-band user message after the newest
// tool result, so it diverges from nothing the diff card can see. Source
// is the user — the steer text is theirs even though the gateway staged it.
func SteerCard(text string) Card {
	return newCard(KindSteer, text, SourceUser, nil)
}

// SpilloverCard cards the persisted-output stub an oversized tool result
// left behind: the model reads the stub, not the spilled bytes, so the
// stub IS the injection. Source is the tool result it replaced.
func SpilloverCard(stub string) Card {
	return newCard(KindSpillover, stub, SourceToolResult, nil)
}

// SubdirectoryCard cards a directory AGENTS.md hint appended to a tool
// result on the agent's first file touch in a new subdirectory: context
// beyond the tool's own output, delivered inside the tool result bytes.
// detail names the loaded file(s) (comma-joined display paths) and lands in
// meta; a blank detail keeps the bare card.
func SubdirectoryCard(hints, detail string) Card {
	var meta map[string]string
	if detail != "" {
		meta = map[string]string{KindSubdir: detail}
	}
	return newCard(KindSubdir, hints, SourceToolResult, meta)
}

// CompactionCard cards the conversation summary a committed context
// compression inserted: it replaced pruned history as a synthetic user
// row, invisible to every diff-based card.
func CompactionCard(summary string) Card {
	return newCard(KindCompaction, summary, SourceSession, nil)
}

// AssembledUserMessageCard cards a user message that was assembled by a
// subsystem rather than typed by the user — a standing-goal continuation
// (kind "goal", no detail) or a skill slash-command body (kind "skill",
// detail = the invoked skill's name). Source is the session: the bytes
// were injected AS the user message by the session's own machinery.
//
// kind must be a ClassifyUserMessage result ("goal", "skill", or "" for a
// typed message — the caller does not card those at all).
func AssembledUserMessageCard(kind, detail, content string) Card {
	var meta map[string]string
	if detail != "" {
		meta = map[string]string{kind: detail}
	}
	return newCard(kind, content, SourceSession, meta)
}

// Section is one row of the session-inventory manifest the prompt builder
// records: the kind of a system-prompt section that actually landed, with
// an optional detail (identity carries "soul"/"default"; counted sections
// carry their count). Mirrors the (kind, detail) tuples
// agent/system_prompt.py appends to agent._system_prompt_section_manifest.
type Section struct {
	Kind   string `json:"kind"`
	Detail string `json:"detail,omitempty"`
}

// SectionRowLabels maps a session-inventory manifest kind to the
// human-readable row the card shows for it: emoji + words, like the other
// injection headings. A "<kind>:<detail>" key wins over the bare kind
// (identity's soul/default split); "{detail}" in a template substitutes
// the manifest row's detail. Mirrors SECTION_ROW_LABELS in
// agent/context_injection_cards.py — the shared table
// (testdata/labels.json, "section_row_labels") pins both twins
// byte-for-byte so they cannot drift.
var SectionRowLabels = map[string]string{
	"identity:soul": "🪪 SOUL.md",
	"identity":      "🪪 Default identity",
	"docs":          "📖 Hermes docs pointer",
	"guidance":      "📏 Guidance ({detail} rules)",
	"skills_index":  "📚 Skills index",
	"auto_skills":   "📚 Auto-loaded skills ({detail})",
	"coding_brief":  "💻 Coding brief",
	"caller_system": "🧵 Caller system prompt",
	"context_files": "🗂️ Project context (AGENTS.md)",
	"workspace":     "🏗️ Workspace snapshot",
	"environment":   "🌍 Environment probe",
	"bot_mode":      "🤖 Bot-mode protocol",
	"profile":       "👤 Profile",
	"platform":      "💬 Platform ({detail})",
	"plugins":       "🧩 Plugin prompt sections ({detail})",
	"timestamp":     "🕐 Timestamp",
	"runtime_env":   "⚙️ Runtime environment",
}

// RenderSectionRow renders one manifest (kind, detail) pair through
// SectionRowLabels. Unknown kinds keep the raw "kind" / "kind (detail)"
// rendering so future manifest rows stay additive-safe.
func RenderSectionRow(kind, detail string) string {
	if detail != "" {
		if label, ok := SectionRowLabels[kind+":"+detail]; ok {
			return label
		}
		if template, ok := SectionRowLabels[kind]; ok {
			return strings.ReplaceAll(template, "{detail}", detail)
		}
		return kind + " (" + detail + ")"
	}
	if label, ok := SectionRowLabels[kind]; ok {
		return label
	}
	return kind
}

// Session-inventory rows that are not prompt sections: the per-request
// ephemeral session context and the tool-schema count ride the same card
// because they are injected on every call but change with the same shape
// as the manifest, so the same sha gate honestly covers them. The Python
// twin's EPHEMERAL_ROW / TOOL_SCHEMAS_ROW constants mirror these.
const (
	EphemeralRow   = "🧵 Session context (ephemeral)"
	ToolSchemasRow = "🔧 {count} tool schemas"
)

// SessionInventoryCard composes the once-per-shape session-context card:
// one line per manifest section that landed, in prompt order, then the
// ephemeral/tool-schema rows when present. The joined content doubles as
// the sha-gating key — sha256 of the UTF-8 bytes — so the first turn and
// the first turn after the prompt's SHAPE changes (skills reload, config
// change, ephemeral block appearing) each fire once and steady-state
// turns stay silent; memory sections are excluded (they keep their own
// card), and an agent with no manifest and no per-request additions gets
// no card at all.
//
// Mirrors session_inventory_card in agent/context_injection_cards.py; the
// shared table pins both sides' composition and sha derivation.
func SessionInventoryCard(sections []Section, ephemeral bool, toolCount int) (Card, string) {
	lines := make([]string, 0, len(sections)+2)
	for _, s := range sections {
		lines = append(lines, RenderSectionRow(s.Kind, s.Detail))
	}
	if ephemeral {
		lines = append(lines, EphemeralRow)
	}
	if toolCount > 0 {
		lines = append(lines, strings.ReplaceAll(ToolSchemasRow, "{count}", strconv.Itoa(toolCount)))
	}
	content := joinLines(lines)
	return newCard(KindSession, content, SourceSession, nil), sha256Hex(content)
}

func joinLines(lines []string) string {
	out := ""
	for i, line := range lines {
		if i > 0 {
			out += "\n"
		}
		out += line
	}
	return out
}
