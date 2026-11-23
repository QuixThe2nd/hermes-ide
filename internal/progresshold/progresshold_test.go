package progresshold

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// contractTable mirrors testdata/hold_scenarios.json. The same bytes drive
// the Python contract test, so both twins assert identical decisions.
type contractTable struct {
	Contract struct {
		GateTag     string `json:"gate_tag"`
		HoldTag     string `json:"hold_tag"`
		FlushTag    string `json:"flush_tag"`
		ResetTag    string `json:"reset_tag"`
		MarkerArity int    `json:"marker_arity"`
	} `json:"contract"`
	Scenarios []struct {
		Name     string         `json:"name"`
		Queue    []scenarioItem `json:"queue"`
		Service  []string       `json:"service"`
		Kept     []string       `json:"kept"`
		Parkable bool           `json:"parkable"`
	} `json:"scenarios"`
}

type scenarioItem struct {
	ID    string `json:"id"`
	Kind  string `json:"kind"`
	Tag   string `json:"tag"`
	Arity int    `json:"arity"`
}

func loadTable(t *testing.T) contractTable {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", "hold_scenarios.json"))
	if err != nil {
		t.Fatalf("read shared scenario table: %v", err)
	}
	var table contractTable
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parse shared scenario table: %v", err)
	}
	return table
}

// buildItem reduces a scenario spec to the (tag, arity) shape the marker
// protocol sees. Keep in sync with the Python contract test's _build_item:
// both sides must feed the twins identical inputs from the same table.
func buildItem(spec scenarioItem) Item {
	switch spec.Kind {
	case "row":
		return Item{ID: spec.ID}
	case "hold":
		tag := spec.Tag
		if tag == "" {
			tag = GateTag
		}
		return Item{ID: spec.ID, Tag: tag, Arity: markerArity}
	case "flush":
		return Item{ID: spec.ID, Tag: FlushTag, Arity: markerArity}
	case "reset":
		return Item{ID: spec.ID, Tag: ResetTag, Arity: 1}
	case "tuple":
		return Item{ID: spec.ID, Tag: spec.Tag, Arity: spec.Arity}
	}
	return Item{ID: spec.ID}
}

func ids(items []Item) []string {
	out := make([]string, 0, len(items))
	for _, item := range items {
		out = append(out, item.ID)
	}
	return out
}

func equalIDs(got, want []string) bool {
	if len(got) != len(want) {
		return false
	}
	for i := range got {
		if got[i] != want[i] {
			return false
		}
	}
	return true
}

// TestContractTagsMatchTheSharedTable pins the marker vocabulary both twins
// speak: the Go constants must equal the table's contract section, and the
// Python contract test asserts the same bytes against the pump's tags.
func TestContractTagsMatchTheSharedTable(t *testing.T) {
	table := loadTable(t)
	if GateTag != table.Contract.GateTag {
		t.Errorf("GateTag = %q, table says %q", GateTag, table.Contract.GateTag)
	}
	if HoldTag != table.Contract.HoldTag {
		t.Errorf("HoldTag = %q, table says %q", HoldTag, table.Contract.HoldTag)
	}
	if FlushTag != table.Contract.FlushTag {
		t.Errorf("FlushTag = %q, table says %q", FlushTag, table.Contract.FlushTag)
	}
	if ResetTag != table.Contract.ResetTag {
		t.Errorf("ResetTag = %q, table says %q", ResetTag, table.Contract.ResetTag)
	}
	if markerArity != table.Contract.MarkerArity {
		t.Errorf("markerArity = %d, table says %d", markerArity, table.Contract.MarkerArity)
	}
}

// TestHoldServiceScenarios runs every shared scenario through the full
// hold-wait decision: which parked markers are serviced, what the queue
// keeps, whether the pump may then park, and that no row ever moved.
func TestHoldServiceScenarios(t *testing.T) {
	table := loadTable(t)
	if len(table.Scenarios) == 0 {
		t.Fatal("shared table lost its scenarios")
	}
	for _, scenario := range table.Scenarios {
		t.Run(scenario.Name, func(t *testing.T) {
			items := make([]Item, 0, len(scenario.Queue))
			for _, spec := range scenario.Queue {
				items = append(items, buildItem(spec))
			}
			markers, kept, parkable := ServiceOrder(items)
			if !equalIDs(ids(markers), scenario.Service) {
				t.Errorf("service = %v, table says %v", ids(markers), scenario.Service)
			}
			if !equalIDs(ids(kept), scenario.Kept) {
				t.Errorf("kept = %v, table says %v", ids(kept), scenario.Kept)
			}
			if parkable != scenario.Parkable {
				t.Errorf("parkable = %v, table says %v", parkable, scenario.Parkable)
			}
			if !PreservesRowOrder(items, kept) {
				t.Errorf("kept %v reorders input %v", ids(kept), ids(items))
			}
		})
	}
}

// TestClassify covers the predicate edges the shared table cannot express
// without noise: plain rows, both hold spellings, reset's trailing-payload
// tolerance, and marker tags at the wrong arity degrading to rows.
func TestClassify(t *testing.T) {
	cases := []struct {
		name string
		item Item
		want Kind
	}{
		{"plain row", Item{ID: "r"}, KindRow},
		{"row with text-ish tag", Item{ID: "r", Tag: "tool output"}, KindRow},
		{"gate marker", Item{ID: "g", Tag: GateTag, Arity: 2}, KindHold},
		{"hold marker", Item{ID: "h", Tag: HoldTag, Arity: 2}, KindHold},
		{"flush marker", Item{ID: "f", Tag: FlushTag, Arity: 2}, KindFlush},
		{"reset bare", Item{ID: "x", Tag: ResetTag, Arity: 1}, KindReset},
		{"reset with payload", Item{ID: "x", Tag: ResetTag, Arity: 3}, KindReset},
		{"gate without event", Item{ID: "g", Tag: GateTag, Arity: 1}, KindRow},
		{"hold with extra", Item{ID: "h", Tag: HoldTag, Arity: 3}, KindRow},
		{"flush without event", Item{ID: "f", Tag: FlushTag, Arity: 1}, KindRow},
		{"empty tag is a row", Item{ID: "e", Tag: "", Arity: 2}, KindRow},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Classify(tc.item); got != tc.want {
				t.Errorf("Classify(%+v) = %v, want %v", tc.item, got, tc.want)
			}
		})
	}
}

// TestPreservesRowOrderRejectsReorder pins the invariant's teeth: a kept
// list that moved any non-flush item (or dropped one) is rejected, so the
// property test above cannot pass vacuously.
func TestPreservesRowOrderRejectsReorder(t *testing.T) {
	rowA := Item{ID: "a"}
	rowB := Item{ID: "b"}
	hold := Item{ID: "h", Tag: GateTag, Arity: 2}
	flush := Item{ID: "f", Tag: FlushTag, Arity: 2}
	input := []Item{rowA, hold, rowB, flush}

	if PreservesRowOrder(input, []Item{rowA, rowB, hold}) {
		t.Error("moved hold marker accepted")
	}
	if PreservesRowOrder(input, []Item{rowA, hold}) {
		t.Error("dropped row accepted")
	}
	if PreservesRowOrder(input, []Item{rowB, hold, rowA}) {
		t.Error("swapped rows accepted")
	}
	if !PreservesRowOrder(input, []Item{rowA, hold, rowB}) {
		t.Error("flush-stripped order rejected")
	}
}

// TestServiceOrderExtractsOnlyFlushMarkers guards the "only the marker
// moves" rule at the API boundary: hold and reset markers parked in the
// queue are never serviced early — box order is output order.
func TestServiceOrderExtractsOnlyFlushMarkers(t *testing.T) {
	items := []Item{
		{ID: "r1"},
		{ID: "h1", Tag: GateTag, Arity: 2},
		{ID: "x1", Tag: ResetTag, Arity: 1},
		{ID: "f1", Tag: FlushTag, Arity: 2},
		{ID: "h2", Tag: HoldTag, Arity: 2},
		{ID: "f2", Tag: FlushTag, Arity: 2},
	}
	markers, kept, parkable := ServiceOrder(items)
	if !equalIDs(ids(markers), []string{"f1", "f2"}) {
		t.Errorf("serviced %v, want [f1 f2]", ids(markers))
	}
	if !equalIDs(ids(kept), []string{"r1", "h1", "x1", "h2"}) {
		t.Errorf("kept %v, want [r1 h1 x1 h2]", ids(kept))
	}
	if parkable {
		t.Error("queue with parked flush markers reported parkable")
	}
}
