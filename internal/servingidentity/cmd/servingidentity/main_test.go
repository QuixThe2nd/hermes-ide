package main

import (
	"encoding/json"
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/QuixThe2nd/hermes-ide/internal/servingidentity"
)

// The cmd layer maps wire requests onto the library twin. These tests pin
// that mapping directly (no subprocess): the shared table (testdata/
// serving_cases.json — generated from the Python twin's real output) flows
// through the same request shape the binary reads on stdin, so byte parity
// is proven at both layers.

type sharedCase struct {
	Name        string   `json:"name"`
	Op          string   `json:"op"`
	Input       any      `json:"input"`
	Expect      any      `json:"expect"`
	ExpectLines []string `json:"expect_lines"`
}

func loadSharedCases(t *testing.T) []sharedCase {
	t.Helper()
	raw, err := os.ReadFile("../../testdata/serving_cases.json")
	if err != nil {
		t.Fatalf("read shared table: %v", err)
	}
	var table struct {
		Cases []sharedCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parse shared table: %v", err)
	}
	return table.Cases
}

func str(v any) string {
	s, _ := v.(string)
	return s
}

// TestRequestMappingMatchesSharedTable drives every table op through the
// cmd layer's real dispatch helpers and compares against the recorded
// expectations — the same proof the Python contract test runs against the
// running binary, executed in-process here.
func TestRequestMappingMatchesSharedTable(t *testing.T) {
	for _, c := range loadSharedCases(t) {
		input, _ := c.Input.(map[string]any)
		request := map[string]any{"op": c.Op}
		for k, v := range input {
			request[k] = v
		}
		switch c.Op {
		case "status":
			got := statusFromRequest(request)
			if !reflect.DeepEqual(got, c.ExpectLines) {
				t.Errorf("%s:\n got: %q\nwant: %q", c.Name, got, c.ExpectLines)
			}
		case "record":
			if got := recordVerdictFromRequest(request); got != str(c.Expect) {
				t.Errorf("%s: got %q want %q", c.Name, got, str(c.Expect))
			}
		case "owner_guard":
			if got := servingidentity.OwnerAction(intValue(request["serving_pid"]),
				identifyFromRecord(request["identify"])); got != str(c.Expect) {
				t.Errorf("%s: got %q want %q", c.Name, got, str(c.Expect))
			}
		case "owner_refusal":
			if got := servingidentity.OwnerRefusal(intValue(request["serving_pid"]),
				identifyFromRecord(request["identify"])); got != str(c.Expect) {
				t.Errorf("%s: got %q want %q", c.Name, got, str(c.Expect))
			}
		case "turn_notes":
			got := servingidentity.TurnNotes(
				snapshotFromIdentityRecord(request), str(request["stored"]))
			if !reflect.DeepEqual(got, c.ExpectLines) {
				t.Errorf("%s:\n got: %q\nwant: %q", c.Name, got, c.ExpectLines)
			}
		}
	}
}

// TestIdentifyMapping pins the wire→Identify mapping: absent/invalid pid
// answers become nil (the "no answer" rung), overlap fields survive the
// any-typed decode.
func TestIdentifyMapping(t *testing.T) {
	if identifyFromRecord(nil) != nil {
		t.Error("absent identify must map to nil")
	}
	if identifyFromRecord("junk") != nil {
		t.Error("non-object identify must map to nil")
	}
	if identifyFromRecord(map[string]any{"pid": 0}) != nil {
		t.Error("pid 0 is not a process — must map to nil")
	}
	got := identifyFromRecord(map[string]any{
		"pid": 709505,
		"overlap": map[string]any{
			"role": "draining-predecessor", "successor_pid": 1531038.0,
			"successor_ready": true,
		},
	})
	// The owner-op mapping reads only what the restart gate consumes; the
	// status op maps start-time fingerprints separately.
	if got == nil || got.PID != 709505 ||
		got.Role != servingidentity.RoleDrainingPredecessor ||
		got.SuccessorPID != 1531038 || !got.SuccessorReady {
		t.Errorf("overlap mapping: %+v", got)
	}
	if got.StartTime != 0 {
		t.Errorf("owner mapping must not invent a start fingerprint: %+v", got)
	}
	if got := pidsFromRequest([]any{709505.0, "junk", 1531038.0}); len(got) != 2 ||
		got[0] != 709505 || got[1] != 1531038 {
		t.Errorf("scan decode: %v", got)
	}
	if got := pidsFromRequest(nil); len(got) != 0 {
		t.Errorf("absent scan: %v", got)
	}
	if got := pidsFromRequest("not-a-list"); len(got) != 0 {
		t.Errorf("junk scan: %v", got)
	}
}

// TestStampAndFallbackHelpers pins the env-stamp parser wiring and the
// tolerance helpers the request mapping leans on.
func TestStampAndFallbackHelpers(t *testing.T) {
	if snap, ok := servingidentity.SessionStamp("1531038", "3005678"); !ok ||
		snap.PID != 1531038 || snap.StartTime != 3005678 {
		t.Errorf("valid stamp: %+v %v", snap, ok)
	}
	if _, ok := servingidentity.SessionStamp("", "3005678"); ok {
		t.Error("absent pid must not parse")
	}
	if _, ok := servingidentity.SessionStamp("1531038", "not-a-number"); ok {
		t.Error("garbage start must not parse")
	}
	if got := orInt(709505, 1531038); got != 709505 {
		t.Errorf("primary wins: %d", got)
	}
	if got := orInt(0, 1531038); got != 1531038 {
		t.Errorf("fallback wins: %d", got)
	}
	if got := stringValue("ok"); got != "ok" {
		t.Errorf("string passthrough: %q", got)
	}
	if got := stringValue(7); got != "" {
		t.Errorf("non-string must map empty: %q", got)
	}
}

// TestStatusFromRequestDegenerateShapes pins the request-mapper rungs the
// table exercises only via the happy path: all-absent inputs must render
// the no-evidence rung, never an empty answer.
func TestStatusFromRequestDegenerateShapes(t *testing.T) {
	lines := statusFromRequest(map[string]any{"op": "status"})
	if len(lines) == 0 || !strings.Contains(lines[0], "Serving gateway") {
		t.Errorf("no-source request must still render a rung: %q", lines)
	}
	junk := statusFromRequest(map[string]any{
		"op": "status", "identify": "not-an-object", "state_file": 7,
	})
	if len(junk) == 0 || !strings.Contains(junk[0], "Serving gateway") {
		t.Errorf("junk sources must degrade to a rung: %q", junk)
	}
}
