// Command stateerrors replays the shared classification table
// (testdata/classify_cases.json) through the Go twin of the persistence-error
// classification contract and prints every case's outputs as JSON. The
// Python contract test
// (tests/hermes_state/test_error_classification_go_contract.py) feeds the
// same table to this binary and requires its outputs to match both the
// table and the live hermes_state_errors.py predicates — the twin is the
// canonical statement, the runtime never shells out to it.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/QuixThe2nd/hermes-ide/internal/stateerrors"
)

// caseError is the error spec of one shared-table case. Keep the JSON field
// names identical to the Python test's builder and to the Go package's own
// table test so the three consumers can never drift on the wire shape.
type caseError struct {
	Kind   string `json:"kind"`
	Text   string `json:"text"`
	Code   *int   `json:"sqlite_errorcode,omitempty"`
	ENOSPC bool   `json:"enospc,omitempty"`
}

type caseInput struct {
	Name  string    `json:"name"`
	Error caseError `json:"error"`
}

type table struct {
	Cases []caseInput `json:"cases"`
}

// caseOutputs is the observable result of one case: the cause bucket plus
// every diagnostic predicate, keyed exactly as the shared table's want block.
type caseOutputs struct {
	Name            string `json:"name"`
	Classify        string `json:"classify"`
	Transient       bool   `json:"transient"`
	MalformedDB     bool   `json:"malformed_db"`
	MalformedSchema bool   `json:"malformed_schema"`
	DiskFull        bool   `json:"disk_full"`
	FTSScoped       bool   `json:"fts_scoped"`
}

// knownKinds enumerates the kind tags the wire format accepts; an unknown
// kind is a harness error (exit 2), never a silent KindNone classification.
var knownKinds = map[string]stateerrors.Kind{
	"none":                    stateerrors.KindNone,
	"string":                  stateerrors.KindString,
	"exception":               stateerrors.KindException,
	"os_error":                stateerrors.KindOSError,
	"operational_error":       stateerrors.KindOperationalError,
	"database_error":          stateerrors.KindDatabaseError,
	"turn_lease":              stateerrors.KindTurnLease,
	"compression_closed":      stateerrors.KindCompressionClosed,
	"compression_busy":        stateerrors.KindCompressionBusy,
	"compression_in_progress": stateerrors.KindCompressionInProgress,
	"replaced":                stateerrors.KindReplaced,
	"deleted_wal":             stateerrors.KindDeletedWal,
	"corrupt":                 stateerrors.KindCorrupt,
}

func viewOf(spec caseError) (stateerrors.ErrorView, error) {
	kind, ok := knownKinds[spec.Kind]
	if !ok {
		return stateerrors.ErrorView{}, fmt.Errorf("unknown error kind %q", spec.Kind)
	}
	return stateerrors.ErrorView{
		Kind:   kind,
		Text:   spec.Text,
		Code:   spec.Code,
		ENOSPC: spec.ENOSPC,
	}, nil
}

func replay(t table) ([]caseOutputs, error) {
	results := make([]caseOutputs, 0, len(t.Cases))
	for _, c := range t.Cases {
		view, err := viewOf(c.Error)
		if err != nil {
			return nil, fmt.Errorf("case %q: %w", c.Name, err)
		}
		results = append(results, caseOutputs{
			Name:            c.Name,
			Classify:        string(stateerrors.Classify(view)),
			Transient:       stateerrors.IsTransient(view),
			MalformedDB:     stateerrors.IsMalformedDB(view),
			MalformedSchema: stateerrors.IsMalformedSchema(view),
			DiskFull:        stateerrors.IsDiskFull(view),
			FTSScoped:       stateerrors.IsFTSScopedCorruption(view),
		})
	}
	return results, nil
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

func run(argv []string, stdin io.Reader, stdout, stderr io.Writer) int {
	for _, arg := range argv {
		if arg == "--help" || arg == "-h" {
			fmt.Fprintln(stdout, "usage: stateerrors < classify_cases.json")
			return 0
		}
	}
	raw, err := io.ReadAll(stdin)
	if err != nil {
		fmt.Fprintf(stderr, "stateerrors: read stdin: %v\n", err)
		return 1
	}
	var input table
	if err := json.Unmarshal(raw, &input); err != nil {
		fmt.Fprintf(stderr, "stateerrors: bad table JSON: %v\n", err)
		return 2
	}
	results, err := replay(input)
	if err != nil {
		fmt.Fprintf(stderr, "stateerrors: %v\n", err)
		return 2
	}
	out, _ := json.Marshal(map[string]any{"results": results})
	fmt.Fprintln(stdout, string(out))
	return 0
}
