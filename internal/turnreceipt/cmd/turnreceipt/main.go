// Command turnreceipt is the executable form of the canonical renderer: it
// reads one turn as JSON on stdin and writes the HTML page to stdout.
//
// Production never runs it; the gateway renders through gateway/turn_receipt.py.
// tests/gateway/test_turn_receipt.py pipes the shared fixture through this
// binary and asserts the Python twin produces the same bytes.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/QuixThe2nd/hermes-ide/internal/turnreceipt"
)

func main() {
	var in turnreceipt.Input
	if err := json.NewDecoder(os.Stdin).Decode(&in); err != nil {
		fmt.Fprintf(os.Stderr, "turnreceipt: unreadable request: %v\n", err)
		os.Exit(2)
	}
	if _, err := io.WriteString(os.Stdout, turnreceipt.Render(in)); err != nil {
		fmt.Fprintf(os.Stderr, "turnreceipt: write failed: %v\n", err)
		os.Exit(2)
	}
}
