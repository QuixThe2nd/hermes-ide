// Command sparkscron is the stdio bridge the hermes_starts plugin calls once
// per profile home at gateway start: one JSON request on stdin (the facts
// sparkscron.Decide needs) → one JSON verdict on stdout (the create/skip
// decision, plus the job spec when creating).
//
// Exit codes: 0 verdict delivered (including skip verdicts); 2 unreadable or
// malformed request — the bridge treats that as "do nothing this pass".
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/QuixThe2nd/hermes-ide/internal/sparkscron"
)

func main() {
	os.Exit(run(os.Stdin, os.Stdout, os.Stderr))
}

func run(stdin io.Reader, stdout, stderr io.Writer) int {
	raw, err := io.ReadAll(stdin)
	if err != nil {
		fmt.Fprintf(stderr, "sparkscron: reading stdin: %v\n", err)
		return 2
	}
	var in sparkscron.Inputs
	if err := json.Unmarshal(raw, &in); err != nil {
		fmt.Fprintf(stderr, "sparkscron: parsing request: %v\n", err)
		return 2
	}
	out, err := json.Marshal(sparkscron.Decide(in))
	if err != nil {
		fmt.Fprintf(stderr, "sparkscron: encoding verdict: %v\n", err)
		return 2
	}
	if _, err := stdout.Write(append(out, '\n')); err != nil {
		fmt.Fprintf(stderr, "sparkscron: writing verdict: %v\n", err)
		return 2
	}
	return 0
}
