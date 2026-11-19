// Command profileprovision is the executable form of the provisioning
// decision table: it reads one record as JSON on stdin and writes the
// decision as JSON on stdout.
//
// Record shapes (the same records the shared table in testdata/
// provision_cases.json pins, and which plugins/deep_research/provision.py
// pipes through this binary):
//
//	{"op":"plan","name":"researcher","exists":false,
//	 "active_profile":"default","active_is_named":false}
//	  → {"action":"create","clone_config":true,"clone_from":"","code":"","reason":""}
//
//	{"op":"classify","name":"researcher","race":false,"error":"..."}
//	  → {"outcome":"failed","code":"worker_profile_provision_failed","message":"..."}
//
// Exit codes: 0 decision rendered; 2 unreadable or unknown record.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/QuixThe2nd/hermes-ide/internal/profileprovision"
)

func main() {
	os.Exit(run(os.Stdin, os.Stdout, os.Stderr))
}

// run is main over explicit streams: one record in, one decision out, and
// the process exit code returned (2 = unreadable or unknown record).
func run(stdin io.Reader, stdout, stderr io.Writer) int {
	var record map[string]any
	if err := json.NewDecoder(stdin).Decode(&record); err != nil {
		fmt.Fprintf(stderr, "profileprovision: unreadable record: %v\n", err)
		return 2
	}
	decision, err := decide(record)
	if err != nil {
		fmt.Fprintf(stderr, "profileprovision: %v\n", err)
		return 2
	}
	encoded, err := json.Marshal(decision)
	if err != nil {
		fmt.Fprintf(stderr, "profileprovision: cannot encode decision: %v\n", err)
		return 2
	}
	fmt.Fprintln(stdout, string(encoded))
	return 0
}

// decide dispatches one record onto the table. Extra fields are ignored so
// the Python side can pass a superset without a versioned protocol.
func decide(record map[string]any) (any, error) {
	switch str(record["op"]) {
	case "plan":
		return profileprovision.PlanProvision(profileprovision.State{
			Name:          str(record["name"]),
			Exists:        boolean(record["exists"]),
			ActiveProfile: str(record["active_profile"]),
			ActiveIsNamed: boolean(record["active_is_named"]),
		}), nil
	case "classify":
		return profileprovision.ClassifyFailure(profileprovision.Failure{
			Name:  str(record["name"]),
			Race:  boolean(record["race"]),
			Error: str(record["error"]),
		}), nil
	default:
		return nil, fmt.Errorf("unknown op %q (want \"plan\" or \"classify\")", record["op"])
	}
}

// str coerces a decoded JSON value to string (missing → "", non-strings →
// their default; a wrong-typed field must degrade, not crash, the caller).
func str(v any) string {
	s, _ := v.(string)
	return s
}

// boolean coerces a decoded JSON value to bool (missing/non-bool → false).
func boolean(v any) bool {
	b, _ := v.(bool)
	return b
}
