// Command botallowlist is the executable form of internal/botallowlist: it
// reads one JSON request on stdin and writes the verdict as JSON on stdout.
//
//	{"allowed_bots": <raw setting value>, "author_id": "424242424242424242"}
//	  → {"listed": true, "count": 3}   (exit 0)
//
// The Python bridge (gateway/bot_allowlist.py) invokes this binary only
// when the list is configured and non-empty, and caches each
// (list, author ID) verdict in-process — the binary answers cold-cache
// lookups, not every message.
//
// Exit codes:
//
//	0  verdict written to stdout ("listed" plus the parsed list size)
//	1  malformed allowed_bots shape — the bridge fails closed
//	2  unreadable/invalid request envelope
//
// The request decodes with UseNumber so list entries given as JSON numbers
// keep their exact digits (user IDs exceed float64 precision).
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"

	"github.com/QuixThe2nd/hermes-ide/internal/botallowlist"
)

type request struct {
	AllowedBots any    `json:"allowed_bots"`
	AuthorID    string `json:"author_id"`
}

type response struct {
	Listed bool `json:"listed"`
	Count  int  `json:"count"`
}

// run is the whole command, extracted for tests: read one request from
// stdin, write one response to stdout. The process exits with the returned
// code.
func run() int {
	var req request
	decoder := json.NewDecoder(os.Stdin)
	decoder.UseNumber()
	if err := decoder.Decode(&req); err != nil {
		fmt.Fprintf(os.Stderr, "botallowlist: invalid request: %v\n", err)
		return 2
	}
	ids, err := botallowlist.Parse(req.AllowedBots)
	if err != nil {
		fmt.Fprintf(os.Stderr, "botallowlist: %v\n", err)
		if errors.Is(err, botallowlist.ErrMalformed) {
			return 1
		}
		return 2
	}
	out, err := json.Marshal(response{
		Listed: botallowlist.Member(ids, req.AuthorID),
		Count:  len(ids),
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "botallowlist: encode response: %v\n", err)
		return 2
	}
	fmt.Println(string(out))
	return 0
}

func main() {
	os.Exit(run())
}
