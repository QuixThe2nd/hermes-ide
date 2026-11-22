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
	if err := run(os.Stdin, os.Stdout); err != nil {
		fmt.Fprintf(os.Stderr, "turnreceipt: %v\n", err)
		os.Exit(2)
	}
}

func run(stdin io.Reader, stdout io.Writer) error {
	var in turnreceipt.Input
	if err := json.NewDecoder(stdin).Decode(&in); err != nil {
		return fmt.Errorf("unreadable request: %w", err)
	}
	_, err := io.WriteString(stdout, turnreceipt.Render(in))
	return err
}
