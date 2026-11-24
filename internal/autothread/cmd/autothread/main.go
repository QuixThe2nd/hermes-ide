// Command autothread is the executable form of internal/autothread: it
// reads one JSON request on stdin and writes the classification as JSON on
// stdout.
//
//	{"status": 400, "code": 160004, "message": "A thread has already been ..."}
//	  → {"decision": "adopt"}   (exit 0)
//
// The Python bridge (gateway/auto_thread_errors.py) invokes this binary
// only when message.create_thread has already failed, never on the success
// path, so a process spawn never sits in the hot message path.
//
// Exit codes:
//
//	0  decision written to stdout ("adopt" | "rate_limited" | "retry" | "fail")
//	2  unreadable/invalid request envelope
//
// Missing fields decode as zero values, which Classify treats as unknown —
// an envelope naming no recognizable failure class answers "fail".
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/QuixThe2nd/hermes-ide/internal/autothread"
)

type request struct {
	Status  int    `json:"status"`
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type response struct {
	Decision autothread.Decision `json:"decision"`
}

// run is the whole command, extracted for tests: read one request from
// stdin, write one response to stdout. The process exits with the returned
// code.
func run() int {
	var req request
	if err := json.NewDecoder(os.Stdin).Decode(&req); err != nil {
		fmt.Fprintf(os.Stderr, "autothread: invalid request: %v\n", err)
		return 2
	}
	out, err := json.Marshal(response{
		Decision: autothread.Classify(req.Status, req.Code, req.Message),
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "autothread: encode response: %v\n", err)
		return 2
	}
	fmt.Println(string(out))
	return 0
}

func main() {
	os.Exit(run())
}
