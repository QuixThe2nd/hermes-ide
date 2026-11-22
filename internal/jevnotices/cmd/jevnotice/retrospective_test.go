package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/QuixThe2nd/hermes-ide/internal/jevnotices"
)

func TestRunRetrospectiveWithoutCredentialFailsClosed(t *testing.T) {
	input := `{"op":"classify_retrospective","message":"fix the proxy","final_response":"I explained DNS.","initial_label":"chat","initial_confidence":0.4}`
	var stdout, stderr bytes.Buffer
	if code := run(strings.NewReader(input), &stdout, &stderr); code != 0 {
		t.Fatalf("exit = %d, stderr = %q", code, stderr.String())
	}
	var result jevnotices.RetrospectiveResult
	if err := json.Unmarshal(stdout.Bytes(), &result); err != nil {
		t.Fatalf("stdout is not a retrospective result: %q: %v", stdout.String(), err)
	}
	if result.Label != "unknown" || result.Recover || result.Error != "missing_credential" {
		t.Fatalf("result = %+v", result)
	}
	if result.Notice != "```\n🔍 Retrospective Intent: failed (missing_credential)\n```" {
		t.Fatalf("notice = %q", result.Notice)
	}
}
