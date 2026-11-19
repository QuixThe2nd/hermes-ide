// Command modelcmd is the executable form of the canonical /model logic:
// it reads one JSON request on stdin (the same shapes as
// testdata/cases.json) and writes the answer to stdout.
//
// "op" selects the operation:
//
//	{"op":"parse","raw":"/model sonnet --provider litellm"}
//	  → the Parsed wire object as JSON (errors included, verbatim copy).
//
//	{"op":"listing","current_model":"…","current_provider_label":"…",
//	 "labels":{…},"providers":[…]}
//	  → the no-arg /model reply text.
//
//	{"op":"confirmation","model":"…","provider":"…","tokens":272000,
//	 "warning":"","scope":"global","global_error":"","labels":{…}}
//	  → the post-switch confirmation text.
//
// Production surfaces do not shell out to this binary: hermes_cli/modelcmd.py
// is the twin the runtime calls (pip/docker deployments carry no Go
// toolchain). The contract test in tests/hermes_cli/test_modelcmd_contract.py
// pipes every shared-table record through this binary and asserts the twin
// answers identically — the same arrangement as internal/servingidentity.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/QuixThe2nd/hermes-ide/internal/modelcmd"
)

func main() {
	os.Exit(run(os.Stdin, os.Stdout, os.Stderr))
}

// run is main over explicit streams: one request in, one answer out, and
// the process exit code returned (2 = unreadable request, 3 = unknown op).
func run(stdin io.Reader, stdout, stderr io.Writer) int {
	var req map[string]any
	if err := json.NewDecoder(stdin).Decode(&req); err != nil {
		fmt.Fprintf(stderr, "modelcmd: unreadable request: %v\n", err)
		return 2
	}
	enc := json.NewEncoder(stdout)
	switch stringValue(req["op"]) {
	case "parse":
		return emit(enc, modelcmd.Parse(stringValue(req["raw"])))
	case "listing":
		rec, err := decodeListing(req)
		if err != nil {
			fmt.Fprintf(stderr, "modelcmd: bad listing record: %v\n", err)
			return 2
		}
		fmt.Fprintln(stdout, modelcmd.RenderListing(rec))
		return 0
	case "confirmation":
		rec, err := decodeConfirmation(req)
		if err != nil {
			fmt.Fprintf(stderr, "modelcmd: bad confirmation record: %v\n", err)
			return 2
		}
		fmt.Fprintln(stdout, modelcmd.RenderConfirmation(rec))
		return 0
	case "picker":
		providers, _ := req["providers"].([]any)
		rows := make([]map[string]any, 0, len(providers))
		for _, p := range providers {
			if m, ok := p.(map[string]any); ok {
				rows = append(rows, m)
			}
		}
		return emit(enc, modelcmd.PickerOptions(
			rows, stringValue(req["current_provider"]),
			intValue(req["max_providers"]), intValue(req["max_models"])))
	default:
		fmt.Fprintf(stderr, "modelcmd: unknown op %q\n", req["op"])
		return 3
	}
}

// emit writes one JSON answer; a marshal failure is a caller bug, reported
// as exit 2 so the contract test fails loudly instead of silently passing.
func emit(enc *json.Encoder, v any) int {
	if err := enc.Encode(v); err != nil {
		fmt.Fprintf(os.Stderr, "modelcmd: encode failed: %v\n", err)
		return 2
	}
	return 0
}

// decodeListing maps the loose JSON request onto ListingRecord; providers
// decode through JSON so unknown fields are ignored rather than fatal.
func decodeListing(req map[string]any) (modelcmd.ListingRecord, error) {
	raw, err := json.Marshal(req)
	if err != nil {
		return modelcmd.ListingRecord{}, err
	}
	var rec modelcmd.ListingRecord
	if err := json.Unmarshal(raw, &rec); err != nil {
		return modelcmd.ListingRecord{}, err
	}
	return rec, nil
}

// decodeConfirmation maps the loose JSON request onto ConfirmationRecord.
func decodeConfirmation(req map[string]any) (modelcmd.ConfirmationRecord, error) {
	raw, err := json.Marshal(req)
	if err != nil {
		return modelcmd.ConfirmationRecord{}, err
	}
	var rec modelcmd.ConfirmationRecord
	if err := json.Unmarshal(raw, &rec); err != nil {
		return modelcmd.ConfirmationRecord{}, err
	}
	return rec, nil
}

// stringValue coerces a JSON-decoded scalar to a string.
func stringValue(v any) string {
	s, _ := v.(string)
	return s
}

// intValue coerces a JSON-decoded number to an int (0 when absent).
func intValue(v any) int {
	f, ok := v.(float64)
	if !ok {
		return 0
	}
	return int(f)
}
