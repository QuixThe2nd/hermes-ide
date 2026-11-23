// Command mentionrender is the executable form of internal/mentionrender: it
// reads one JSON render request on stdin and writes the rendered text as JSON
// on stdout.
//
//	{"content": "ask <@111> about it", "bot_id": "222", "bot_name": "Hermes",
//	 "names": {"111": "Alice"}, "mode": "body"}
//	  → {"text": "ask @Alice (<@111>) about it"}   (exit 0)
//
// The Python bridge (plugins/platforms/discord/mention_render.py) invokes
// this binary only for content that carries a mention token and falls back
// to the historical mention handling when the binary is unavailable — a
// message is never dropped over a rendering problem.
//
// Exit codes:
//
//	0  rendered text written to stdout
//	2  unreadable/invalid request envelope (bad JSON or unknown mode)
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/QuixThe2nd/hermes-ide/internal/mentionrender"
)

type response struct {
	Text string `json:"text"`
}

// run is the whole command, extracted for tests: read one request from
// stdin, write one response to stdout. The process exits with the returned
// code.
func run() int {
	var req mentionrender.Request
	if err := json.NewDecoder(os.Stdin).Decode(&req); err != nil {
		fmt.Fprintf(os.Stderr, "mentionrender: invalid request: %v\n", err)
		return 2
	}
	if req.Mode != mentionrender.ModeBody && req.Mode != mentionrender.ModeTitle {
		fmt.Fprintf(os.Stderr, "mentionrender: unknown mode %q\n", req.Mode)
		return 2
	}
	out, err := json.Marshal(response{Text: mentionrender.Render(req)})
	if err != nil {
		fmt.Fprintf(os.Stderr, "mentionrender: encode response: %v\n", err)
		return 2
	}
	fmt.Println(string(out))
	return 0
}

func main() {
	os.Exit(run())
}
