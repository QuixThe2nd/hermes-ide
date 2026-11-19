package modelcmd

import (
	"encoding/json"
	"os"
	"testing"
)

// table is the shared contract table (testdata/cases.json) — the SAME
// records the Python contract test pipes through the built binary, so a
// rule change must be made in both implementations at once or the suites
// disagree loudly.
type table struct {
	Parse []struct {
		Raw  string `json:"raw"`
		Want Parsed `json:"want"`
	} `json:"parse"`
	Listing []struct {
		Name   string        `json:"name"`
		Record ListingRecord `json:"record"`
		Want   string        `json:"want"`
	} `json:"listing"`
	Picker []struct {
		Name            string           `json:"name"`
		Providers       []map[string]any `json:"providers"`
		CurrentProvider string           `json:"current_provider"`
		MaxProviders    int              `json:"max_providers"`
		MaxModels       int              `json:"max_models"`
		Want            []map[string]any `json:"want"`
	} `json:"picker"`
	Confirmation []struct {
		Name   string             `json:"name"`
		Record ConfirmationRecord `json:"record"`
		Want   string             `json:"want"`
	} `json:"confirmation"`
}

func loadTable(t *testing.T) table {
	t.Helper()
	raw, err := os.ReadFile("testdata/cases.json")
	if err != nil {
		t.Fatalf("read shared table: %v", err)
	}
	var tbl table
	if err := json.Unmarshal(raw, &tbl); err != nil {
		t.Fatalf("decode shared table: %v", err)
	}
	if len(tbl.Parse) == 0 || len(tbl.Listing) == 0 || len(tbl.Picker) == 0 || len(tbl.Confirmation) == 0 {
		t.Fatalf("shared table missing sections: parse=%d listing=%d picker=%d confirmation=%d",
			len(tbl.Parse), len(tbl.Listing), len(tbl.Picker), len(tbl.Confirmation))
	}
	return tbl
}

// asJSON renders any value through encoding/json so deep comparisons use
// JSON equality (nil vs empty, key order, float formatting).
func asJSON(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal for comparison: %v", err)
	}
	return string(b)
}
