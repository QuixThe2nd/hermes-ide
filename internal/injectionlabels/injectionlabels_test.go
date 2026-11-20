package injectionlabels

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

// The shared contract table (testdata/labels.json) is asserted from BOTH
// sides: these tests and the Python contract test
// (tests/gateway/test_context_injection_labels.py) read the same bytes, so
// the Go twin and gateway/context_injection_labels.py cannot drift apart.

// sharedTable mirrors testdata/labels.json.
type sharedTable struct {
	GenericLabel string            `json:"generic_label"`
	KindLabels   map[string]string `json:"kind_labels"`
	Memory       struct {
		Plain string `json:"plain"`
		One   string `json:"one"`
		Many  string `json:"many"`
	} `json:"memory"`
	Cases            []sharedCase      `json:"cases"`
	ClassifierCases  []classifierCase  `json:"classifier_cases"`
	SessionCardCases []sessionCardCase `json:"session_card_cases"`
	EventCardCases   []eventCardCase   `json:"event_card_cases"`
	CompactionCases  []compactionCase  `json:"compaction_cases"`
	SpilloverCases   []spilloverCase   `json:"spillover_cases"`
	SummaryPrefixes  []string          `json:"summary_prefixes"`
	PersistedTag     string            `json:"persisted_output_tag"`
}

// sessionCardCase is one manifest→(content, sha) expectation for
// SessionInventoryCard: the composition of the once-per-shape inventory
// and the sha gating key derived from it.
type sessionCardCase struct {
	Name          string    `json:"name"`
	Sections      []Section `json:"sections"`
	Ephemeral     bool      `json:"ephemeral"`
	ToolCount     int       `json:"tool_count"`
	ExpectContent string    `json:"expect_content"`
	ExpectSHA     string    `json:"expect_sha"`
	ExpectCard    Card      `json:"expect_card"`
}

// eventCardCase is one per-event payload-builder expectation: the exact
// card payload (kinds, content, char count, sources, meta) a producer
// owes for one injected fact.
type eventCardCase struct {
	Name    string `json:"name"`
	Op      string `json:"op"`
	Text    string `json:"text"`
	Stub    string `json:"stub"`
	Hints   string `json:"hints"`
	Summary string `json:"summary"`
	Kind    string `json:"kind"`
	Detail  string `json:"detail"`
	Content string `json:"content"`
	Expect  Card   `json:"expect"`
}

// compactionCase is one (before, after)→summary expectation for
// NewCompactionSummary: the skip/silent paths and the shapes that must
// classify.
type compactionCase struct {
	Name          string    `json:"name"`
	Before        []Message `json:"before"`
	After         []Message `json:"after"`
	ExpectSummary *string   `json:"expect_summary"`
}

// spilloverCase is one tool-result→stub expectation for the
// persisted-output detection: string results and multimodal parts lists.
type spilloverCase struct {
	Name       string     `json:"name"`
	Op         string     `json:"op"`
	Text       string     `json:"text"`
	Parts      []TextPart `json:"parts"`
	ExpectStub *string    `json:"expect_stub"`
}

// sharedCase is one kinds→heading-lines expectation. An absent kinds array
// is the legacy payload shape; an absent recalled_count means no usable
// count (plain memory heading); an absent meta map means no kind carried a
// detail.
type sharedCase struct {
	Name          string            `json:"name"`
	Kinds         []string          `json:"kinds"`
	MemorySourced bool              `json:"memory_sourced"`
	RecalledCount int               `json:"recalled_count"`
	Meta          map[string]string `json:"meta"`
	ExpectLines   []string          `json:"expect_lines"`
}

// classifierCase is one clean-user-message→(kind, detail) expectation for
// ClassifyUserMessage: goal continuations, skill invocations, and the
// negative shapes that must stay unclassified.
type classifierCase struct {
	Name         string `json:"name"`
	Text         string `json:"text"`
	ExpectKind   string `json:"expect_kind"`
	ExpectDetail string `json:"expect_detail"`
}

func loadTable(t *testing.T) sharedTable {
	t.Helper()
	raw, err := os.ReadFile("testdata/labels.json")
	if err != nil {
		t.Fatalf("read shared table: %v", err)
	}
	var table sharedTable
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parse shared table: %v", err)
	}
	return table
}

// TestLabelsMatchSharedTable pins the label table itself: the generic label,
// every per-kind label, and the three memory heading forms must equal the
// shared table the Python contract test also reads.
func TestLabelsMatchSharedTable(t *testing.T) {
	table := loadTable(t)
	if GenericLabel != table.GenericLabel {
		t.Errorf("GenericLabel = %q, want %q", GenericLabel, table.GenericLabel)
	}
	if !reflect.DeepEqual(KindLabels, table.KindLabels) {
		t.Errorf("KindLabels = %q, want %q", KindLabels, table.KindLabels)
	}
	if got := MemoryHeading(0); got != table.Memory.Plain {
		t.Errorf("MemoryHeading(0) = %q, want %q", got, table.Memory.Plain)
	}
	if got := MemoryHeading(1); got != table.Memory.One {
		t.Errorf("MemoryHeading(1) = %q, want %q", got, table.Memory.One)
	}
	if got := MemoryHeading(3); got != table.Memory.Many {
		t.Errorf("MemoryHeading(3) = %q, want %q", got, table.Memory.Many)
	}
}

// TestHeadingLinesMatchSharedTable drives HeadingLines through every case in
// the shared table: legacy payloads, unknown/blank kinds, the memory
// special case with and without counts, meta-suffixed headings, payload
// ordering, and output-line deduplication.
func TestHeadingLinesMatchSharedTable(t *testing.T) {
	for _, tc := range loadTable(t).Cases {
		got := HeadingLines(tc.Kinds, tc.MemorySourced, tc.RecalledCount, tc.Meta)
		if !reflect.DeepEqual(got, tc.ExpectLines) {
			t.Errorf("%s: HeadingLines(%q, memorySourced=%v, recalledCount=%d, meta=%q) = %q, want %q",
				tc.Name, tc.Kinds, tc.MemorySourced, tc.RecalledCount, tc.Meta, got, tc.ExpectLines)
		}
	}
}

// TestClassifierMatchSharedTable drives ClassifyUserMessage through every
// anchored expectation in the shared table, both positive and negative.
func TestClassifierMatchSharedTable(t *testing.T) {
	for _, tc := range loadTable(t).ClassifierCases {
		kind, detail := ClassifyUserMessage(tc.Text)
		if kind != tc.ExpectKind || detail != tc.ExpectDetail {
			t.Errorf("%s: ClassifyUserMessage(%q) = (%q, %q), want (%q, %q)",
				tc.Name, tc.Text, kind, detail, tc.ExpectKind, tc.ExpectDetail)
		}
	}
}

// TestEveryKnownKindAppearsInSharedTable guards the fixture against drift:
// a kind added to KindLabels without a table entry (or vice versa) fails
// here and in the Python twin's same-shaped check.
func TestEveryKnownKindAppearsInSharedTable(t *testing.T) {
	table := loadTable(t)
	if len(KindLabels) != len(table.KindLabels) {
		t.Errorf("KindLabels has %d entries, shared table has %d", len(KindLabels), len(table.KindLabels))
	}
	for kind := range KindLabels {
		if _, ok := table.KindLabels[kind]; !ok {
			t.Errorf("kind %q missing from shared table", kind)
		}
	}
}

// TestNegativeCountRendersPlain pins the guard behind the counted form: only
// a positive discrete count is ever spoken.
func TestNegativeCountRendersPlain(t *testing.T) {
	if got := MemoryHeading(-2); got != "🧠 Injecting memory" {
		t.Errorf("MemoryHeading(-2) = %q, want the plain heading", got)
	}
}

// TestClassifierAnchorsMatchProducerBytes pins the anchors against the exact
// template bytes the producers emit, so a template edit upstream fails HERE
// (and in the Python twin) rather than silently un-classifying every goal
// or skill turn.
func TestClassifierAnchorsMatchProducerBytes(t *testing.T) {
	// hermes_cli/goals.py CONTINUATION_PROMPT_TEMPLATE renders the prefix
	// followed by "]" before the goal line.
	if kind, _ := ClassifyUserMessage("[Continuing toward your standing goal]\nGoal: ship"); kind != "goal" {
		t.Errorf("plain continuation template did not classify as goal: %q", kind)
	}
	// agent/skill_commands.py build_skill_invocation_message embeds the
	// note mid-message; the first quoted name is the skill.
	if kind, detail := ClassifyUserMessage(
		`[IMPORTANT: The user has invoked the "dev-lanes" skill, indicating they want you to follow its instructions.]`,
	); kind != "skill" || detail != "dev-lanes" {
		t.Errorf("skill note classified as (%q, %q), want (skill, dev-lanes)", kind, detail)
	}
}

// TestSessionCardMatchSharedTable drives SessionInventoryCard through every
// composition case: manifest line rendering (bare and detailed), the
// ephemeral and tool-schema rows, the blank composition, and the sha gating
// key derived from the composed content — including that a shape change
// produces a different key.
func TestSessionCardMatchSharedTable(t *testing.T) {
	for _, c := range loadTable(t).SessionCardCases {
		card, got := SessionInventoryCard(c.Sections, c.Ephemeral, c.ToolCount)
		if got != c.ExpectSHA {
			t.Errorf("%s: sha = %s, want %s", c.Name, got, c.ExpectSHA)
		}
		if !reflect.DeepEqual(card, c.ExpectCard) {
			t.Errorf("%s: card = %+v, want %+v", c.Name, card, c.ExpectCard)
		}
	}
}

// TestSessionCardShapeChangeRekeys proves the gate the sha derivation buys:
// two manifests that differ by one section derive different keys, so the
// second shape fires its own card.
func TestSessionCardShapeChangeRekeys(t *testing.T) {
	table := loadTable(t).SessionCardCases
	var a, b *sessionCardCase
	for i := range table {
		switch table[i].Name {
		case "shape_change_alters_the_sha_a":
			a = &table[i]
		case "shape_change_alters_the_sha_b":
			b = &table[i]
		}
	}
	if a == nil || b == nil {
		t.Fatal("shared table lost the shape-change pair")
	}
	_, shaA := SessionInventoryCard(a.Sections, a.Ephemeral, a.ToolCount)
	_, shaB := SessionInventoryCard(b.Sections, b.Ephemeral, b.ToolCount)
	if shaA == shaB {
		t.Error("manifests differing by one section derived the same gating key")
	}
}

// TestEventCardsMatchSharedTable drives every per-event payload builder
// through its shared-table expectation: kinds, content, char count,
// sources, and the goal/skill meta detail.
func TestEventCardsMatchSharedTable(t *testing.T) {
	for _, c := range loadTable(t).EventCardCases {
		var card Card
		switch c.Op {
		case "steer":
			card = SteerCard(c.Text)
		case "spillover":
			card = SpilloverCard(c.Stub)
		case "subdirectory":
			card = SubdirectoryCard(c.Hints)
		case "compaction":
			card = CompactionCard(c.Summary)
		case "assembled":
			card = AssembledUserMessageCard(c.Kind, c.Detail, c.Content)
		default:
			t.Fatalf("unknown event op %q", c.Op)
		}
		if !reflect.DeepEqual(card, c.Expect) {
			t.Errorf("%s: card = %+v, want %+v", c.Name, card, c.Expect)
		}
	}
}

// TestCompactionMatchSharedTable drives NewCompactionSummary through every
// commit/skip shape: new summaries extract, skip paths and byte-identical
// preserved summaries stay silent, metadata flags and legacy prefixes
// classify, and the FIRST new summary row wins.
func TestCompactionMatchSharedTable(t *testing.T) {
	for _, c := range loadTable(t).CompactionCases {
		summary, changed := NewCompactionSummary(c.Before, c.After)
		if c.ExpectSummary == nil {
			if changed {
				t.Errorf("%s: extracted %q, want silence", c.Name, summary)
			}
			continue
		}
		if !changed {
			t.Errorf("%s: stayed silent, want summary %q", c.Name, *c.ExpectSummary)
			continue
		}
		if summary != *c.ExpectSummary {
			t.Errorf("%s: summary = %q, want %q", c.Name, summary, *c.ExpectSummary)
		}
	}
}

// TestSpilloverMatchSharedTable drives the persisted-output detection: a
// string stub, a plain string, multimodal parts with and without a
// replaced text part.
func TestSpilloverMatchSharedTable(t *testing.T) {
	for _, c := range loadTable(t).SpilloverCases {
		var stub string
		var found bool
		switch c.Op {
		case "string":
			stub, found = c.Text, IsSpilledStub(c.Text)
		case "parts":
			stub, found = SpilledStubFromParts(c.Parts)
		default:
			t.Fatalf("unknown spillover op %q", c.Op)
		}
		if c.ExpectStub == nil {
			if found {
				t.Errorf("%s: detected stub %q, want none", c.Name, stub)
			}
			continue
		}
		if !found {
			t.Errorf("%s: missed stub %q", c.Name, *c.ExpectStub)
		} else if stub != *c.ExpectStub {
			t.Errorf("%s: stub = %q, want %q", c.Name, stub, *c.ExpectStub)
		}
	}
}

// TestSummaryPrefixesMatchSharedTable pins the compaction anchors: the Go
// twin's prefix set must equal the shared table the Python contract test
// checks against context_compressor's own constants, and the persisted-
// output tag likewise.
func TestSummaryPrefixesMatchSharedTable(t *testing.T) {
	table := loadTable(t)
	if !reflect.DeepEqual(SummaryPrefixes, table.SummaryPrefixes) {
		t.Error("SummaryPrefixes drifted from the shared table")
	}
	if PersistedOutputTag != table.PersistedTag {
		t.Errorf("PersistedOutputTag = %q, want %q", PersistedOutputTag, table.PersistedTag)
	}
}

// TestNewCompactionSummaryAbsentContentPin documents the degenerate row the
// Python twin can produce (a metadata-flagged summary with content None):
// it reports changed with an empty summary, never inventing bytes.
func TestNewCompactionSummaryAbsentContentPin(t *testing.T) {
	after := []Message{{Role: "user", CompressedSummary: true}}
	if summary, changed := NewCompactionSummary(nil, after); !changed || summary != "" {
		t.Errorf("absent-content summary = (%q, %v), want (\"\", true)", summary, changed)
	}
}
