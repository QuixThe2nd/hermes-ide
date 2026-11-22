package main

import (
	"bytes"
	"io"
	"strings"
	"testing"
)

// The JSON keys are the wire contract the Python twin sends, so decoding
// them into the right fields is what this test pins.
func TestRunDecodesTheWireKeysIntoEachPart(t *testing.T) {
	var out bytes.Buffer
	request := `{"user_message":"hi","rows":["ls"],"final_reply":"ok"}`
	if err := run(strings.NewReader(request), &out); err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{`bubble user">hi`, `bubble progress">ls`, `bubble reply">ok`} {
		if !strings.Contains(out.String(), want) {
			t.Fatalf("page missing %q", want)
		}
	}
}

func TestRunRejectsAnUnreadableRequest(t *testing.T) {
	if err := run(strings.NewReader("{not json"), io.Discard); err == nil {
		t.Fatal("expected an error for malformed JSON")
	}
}
