// Command goalrun is the executable boundary for the quiet goal run: the
// gateway writes one turn's goal facts as JSON on stdin and reads the delivery
// plan plus the statusbar line back as JSON on stdout.
//
// The production gateway resolves this binary from a content-hash-keyed cache
// under $HERMES_HOME/cache/goalrun and builds it from the repo checkout when
// missing (see gateway/goal_run_plan.py).
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/QuixThe2nd/hermes-ide/internal/goalrun"
)

func main() {
	os.Exit(run(os.Stdin, os.Stdout, os.Stderr))
}

// run is main over explicit streams: one Turn in, one Plan out (exit 2 on an
// unreadable record).
func run(stdin io.Reader, stdout, stderr io.Writer) int {
	data, err := io.ReadAll(io.LimitReader(stdin, (1<<20)+1))
	if err != nil || len(data) > 1<<20 {
		fmt.Fprintln(stderr, "goalrun: unreadable turn")
		return 2
	}
	var turn goalrun.Turn
	if err := json.Unmarshal(data, &turn); err != nil {
		fmt.Fprintf(stderr, "goalrun: unreadable turn: %v\n", err)
		return 2
	}
	if err := json.NewEncoder(stdout).Encode(goalrun.PlanForTurn(turn)); err != nil {
		fmt.Fprintln(stderr, "goalrun: could not encode plan")
		return 2
	}
	return 0
}
