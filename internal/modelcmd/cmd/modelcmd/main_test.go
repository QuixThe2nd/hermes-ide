package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

// TestRunOpsDispatch: every op answers on stdout with exit 0. This is the
// exact entry the Python contract test drives via the built binary, so a
// regression here breaks the twin contract loudly.
func TestRunOpsDispatch(t *testing.T) {
	cases := []struct {
		name    string
		req     string
		wantOut string // prefix check for listing, JSON for the others
	}{
		{
			name: "parse",
			req:  `{"op":"parse","raw":"x --once --global"}`,
			wantOut: `{"raw":"x --once --global","target":"x","explicit_provider":"",` +
				`"reasoning_effort":"","is_global":true,"is_session":false,"is_once":true,` +
				`"force_refresh":false,"scope":"once","errors":[{"code":"once_with_global",` +
				`"message":"/model --once cannot be combined with --global"}]}`,
		},
		{
			name:    "listing",
			req:     `{"op":"listing","current_model":"m","current_provider_label":"P","providers":[]}`,
			wantOut: "Current: `m` on P",
		},
		{
			name:    "picker",
			req:     `{"op":"picker","providers":[{"slug":"a","models":["m"]}],"current_provider":"a"}`,
			wantOut: `[{"is_current":true,"models":["m"],"slug":"a"}]`,
		},
	}
	for _, tc := range cases {
		var out, errBuf bytes.Buffer
		code := run(strings.NewReader(tc.req), &out, &errBuf)
		if code != 0 {
			t.Errorf("%s: exit %d (stderr %q)", tc.name, code, errBuf.String())
			continue
		}
		if tc.name == "listing" {
			if !strings.Contains(out.String(), tc.wantOut) {
				t.Errorf("%s: output missing %q:\n%s", tc.name, tc.wantOut, out.String())
			}
			continue
		}
		var got any
		if err := json.Unmarshal(out.Bytes(), &got); err != nil {
			t.Errorf("%s: stdout is not JSON: %v (%q)", tc.name, err, out.String())
		}
	}
}

// TestRunParseExitCodeIsSuccessOnParseErrors: parse ERRORS are answers,
// not failures — the exit code stays 0 so callers distinguish "bad
// request" from "parser disagreed".
func TestRunParseExitCodeIsSuccessOnParseErrors(t *testing.T) {
	var out, errBuf bytes.Buffer
	code := run(strings.NewReader(`{"op":"parse","raw":"x --once --global"}`), &out, &errBuf)
	if code != 0 {
		t.Fatalf("exit = %d, want 0 (parse errors are valid answers)", code)
	}
	if !strings.Contains(out.String(), "once_with_global") {
		t.Fatalf("error code missing from answer: %q", out.String())
	}
}

// TestRunRejectsBadInput: unreadable JSON and unknown ops are protocol
// errors — nonzero exit, nothing on stdout, a reason on stderr.
func TestRunRejectsBadInput(t *testing.T) {
	cases := []struct {
		name    string
		req     string
		wantCmd string
	}{
		{"garbage", "not json", "unreadable"},
		{"unknown_op", `{"op":"explode"}`, "unknown op"},
	}
	for _, tc := range cases {
		var out, errBuf bytes.Buffer
		code := run(strings.NewReader(tc.req), &out, &errBuf)
		if code == 0 || out.Len() != 0 || !strings.Contains(errBuf.String(), tc.wantCmd) {
			t.Errorf("%s: exit=%d stdout=%q stderr=%q; want nonzero exit, empty stdout, %q on stderr",
				tc.name, code, out.String(), errBuf.String(), tc.wantCmd)
		}
	}
}
