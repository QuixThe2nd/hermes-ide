package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/QuixThe2nd/hermes-ide/internal/stateerrors"
)

// TestRunReplaysTheSharedTable feeds the real shared table through the
// binary's run function and requires every case's outputs to match the
// table's want block — the same equality the Python contract test asserts
// against the built binary.
func TestRunReplaysTheSharedTable(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "testdata", "classify_cases.json"))
	if err != nil {
		t.Fatalf("read shared table: %v", err)
	}
	var stdout, stderr bytes.Buffer
	if code := run(nil, bytes.NewReader(raw), &stdout, &stderr); code != 0 {
		t.Fatalf("run exited %d: %s", code, stderr.String())
	}
	var answer struct {
		Results []struct {
			Name            string `json:"name"`
			Classify        string `json:"classify"`
			Transient       bool   `json:"transient"`
			MalformedDB     bool   `json:"malformed_db"`
			MalformedSchema bool   `json:"malformed_schema"`
			DiskFull        bool   `json:"disk_full"`
			FTSScoped       bool   `json:"fts_scoped"`
		} `json:"results"`
	}
	if err := json.Unmarshal(stdout.Bytes(), &answer); err != nil {
		t.Fatalf("parse run output: %v", err)
	}
	var table struct {
		Cases []struct {
			Name string `json:"name"`
			Want struct {
				Classify        string `json:"classify"`
				Transient       bool   `json:"transient"`
				MalformedDB     bool   `json:"malformed_db"`
				MalformedSchema bool   `json:"malformed_schema"`
				DiskFull        bool   `json:"disk_full"`
				FTSScoped       bool   `json:"fts_scoped"`
			} `json:"want"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parse shared table: %v", err)
	}
	if len(answer.Results) != len(table.Cases) {
		t.Fatalf("replayed %d cases, table holds %d", len(answer.Results), len(table.Cases))
	}
	for i, c := range table.Cases {
		got := answer.Results[i]
		if got.Name != c.Name {
			t.Fatalf("result %d is %q, table case is %q — results must stay in table order", i, got.Name, c.Name)
		}
		if got.Classify != c.Want.Classify ||
			got.Transient != c.Want.Transient ||
			got.MalformedDB != c.Want.MalformedDB ||
			got.MalformedSchema != c.Want.MalformedSchema ||
			got.DiskFull != c.Want.DiskFull ||
			got.FTSScoped != c.Want.FTSScoped {
			t.Errorf("case %q: outputs %+v, want %+v", c.Name, got, c.Want)
		}
	}
}

// TestRunRejectsUnknownKind: an unrecognized kind tag is a harness error,
// never a silent none-classification — a typo in the table must fail loudly.
func TestRunRejectsUnknownKind(t *testing.T) {
	input := `{"cases": [{"name": "bad", "error": {"kind": "explode", "text": "x"}}]}`
	var stdout, stderr bytes.Buffer
	if code := run(nil, strings.NewReader(input), &stdout, &stderr); code != 2 {
		t.Fatalf("run exited %d, want 2", code)
	}
	if !strings.Contains(stderr.String(), "unknown error kind") {
		t.Errorf("stderr %q does not name the failure", stderr.String())
	}
}

// TestRunRejectsMalformedJSON: bad input exits 2 with a parse diagnostic.
func TestRunRejectsMalformedJSON(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := run(nil, strings.NewReader("{not json"), &stdout, &stderr); code != 2 {
		t.Fatalf("run exited %d, want 2", code)
	}
	if !strings.Contains(stderr.String(), "bad table JSON") {
		t.Errorf("stderr %q does not name the failure", stderr.String())
	}
}

// TestRunHelp: --help prints usage and exits 0.
func TestRunHelp(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := run([]string{"--help"}, strings.NewReader(""), &stdout, &stderr); code != 0 {
		t.Fatalf("run --help exited %d", code)
	}
	if !strings.Contains(stdout.String(), "usage: stateerrors") {
		t.Errorf("help output %q lacks usage", stdout.String())
	}
}

// TestViewOfCoversEveryKind: the harness must accept every Kind the package
// defines, so a new contract case can never be stranded without a wire tag.
func TestViewOfCoversEveryKind(t *testing.T) {
	for tag, kind := range knownKinds {
		view, err := viewOf(caseError{Kind: tag})
		if err != nil {
			t.Errorf("kind tag %q rejected: %v", tag, err)
		}
		if view.Kind != kind {
			t.Errorf("kind tag %q mapped to %q", tag, view.Kind)
		}
	}
	for _, kind := range []stateerrors.Kind{
		stateerrors.KindNone, stateerrors.KindString, stateerrors.KindException,
		stateerrors.KindOSError, stateerrors.KindOperationalError, stateerrors.KindDatabaseError,
		stateerrors.KindTurnLease, stateerrors.KindCompressionClosed,
		stateerrors.KindCompressionBusy, stateerrors.KindCompressionInProgress,
		stateerrors.KindReplaced, stateerrors.KindDeletedWal, stateerrors.KindCorrupt,
	} {
		if _, err := viewOf(caseError{Kind: string(kind)}); err != nil {
			t.Errorf("package kind %q has no wire tag: %v", kind, err)
		}
	}
}
