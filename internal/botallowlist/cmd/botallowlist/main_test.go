package main

import (
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// binary builds cmd/botallowlist into a temp dir so the tests exercise the
// real stdin/stdout/exit-code contract, not a reimplementation.
func binary(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "botallowlist")
	build := exec.Command("go", "build", "-o", path, ".")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("go build: %v\n%s", err, out)
	}
	return path
}

// query pipes one request through the binary and returns the decoded
// verdict plus the exit code.
func query(t *testing.T, bin, request string) (response, int) {
	t.Helper()
	cmd := exec.Command(bin)
	cmd.Stdin = bytes.NewBufferString(request)
	var stdout bytes.Buffer
	cmd.Stdout = &stdout
	err := cmd.Run()
	code := 0
	if exitErr, ok := err.(*exec.ExitError); ok {
		code = exitErr.ExitCode()
	} else if err != nil {
		t.Fatalf("run: %v", err)
	}
	var resp response
	if code == 0 {
		if err := json.Unmarshal(stdout.Bytes(), &resp); err != nil {
			t.Fatalf("request %s: invalid stdout %q: %v", request, stdout.String(), err)
		}
	}
	return resp, code
}

func TestVerdicts(t *testing.T) {
	bin := binary(t)
	cases := []struct {
		name    string
		request string
		listed  bool
		count   int
	}{
		{"listed via JSON array", `{"allowed_bots": ["111", "222"], "author_id": "222"}`, true, 2},
		{"unlisted via JSON array", `{"allowed_bots": ["111"], "author_id": "222"}`, false, 1},
		{"listed via CSV", `{"allowed_bots": "111, 222", "author_id": "111"}`, true, 2},
		{"listed via JSON array string", `{"allowed_bots": "[\"111\"]", "author_id": "111"}`, true, 1},
		// Snowflake IDs as JSON numbers: float64 decoding would corrupt them.
		{"snowflake number", `{"allowed_bots": [123456789012345678901], "author_id": "123456789012345678901"}`, true, 1},
		{"empty list narrows nothing", `{"allowed_bots": [], "author_id": "111"}`, false, 0},
		{"missing author ID never listed", `{"allowed_bots": ["111"], "author_id": ""}`, false, 1},
		{"null list", `{"allowed_bots": null, "author_id": "111"}`, false, 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			resp, code := query(t, bin, tc.request)
			if code != 0 {
				t.Fatalf("exit code = %d, want 0", code)
			}
			if resp.Listed != tc.listed || resp.Count != tc.count {
				t.Fatalf("response = %+v, want listed=%v count=%d", resp, tc.listed, tc.count)
			}
		})
	}
}

func TestMalformedExitsOne(t *testing.T) {
	bin := binary(t)
	for _, request := range []string{
		`{"allowed_bots": {"id": "111"}, "author_id": "111"}`,
		`{"allowed_bots": true, "author_id": "111"}`,
		`{"allowed_bots": [["nested"]], "author_id": "111"}`,
	} {
		if _, code := query(t, bin, request); code != 1 {
			t.Fatalf("request %s: exit code = %d, want 1 (fail closed)", request, code)
		}
	}
}

func TestInvalidRequestExitsTwo(t *testing.T) {
	bin := binary(t)
	if _, code := query(t, bin, `not json`); code != 2 {
		t.Fatalf("exit code = %d, want 2", code)
	}
}

// admissionCase is one row of the shared table
// (internal/botallowlist/testdata/admission_cases.json) that the Python
// bridge test also consumes.
type admissionCase struct {
	Name      string `json:"name"`
	Raw       any    `json:"raw"`
	AuthorID  string `json:"author_id"`
	Listed    bool   `json:"listed"`
	Count     int    `json:"count"`
	Malformed bool   `json:"malformed"`
}

// TestAdmissionTableThroughBinary pipes every shared-table row through the
// real binary: malformed rows must exit 1 (the bridge fails closed), the
// rest must exit 0 with the table's verdict. The table decodes with
// UseNumber so numeric IDs re-encode with every digit intact.
func TestAdmissionTableThroughBinary(t *testing.T) {
	bin := binary(t)
	data, err := os.ReadFile(filepath.Join("..", "..", "testdata", "admission_cases.json"))
	if err != nil {
		t.Fatalf("read table: %v", err)
	}
	var table struct {
		Cases []admissionCase `json:"cases"`
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	if err := decoder.Decode(&table); err != nil {
		t.Fatalf("parse table: %v", err)
	}
	if len(table.Cases) == 0 {
		t.Fatal("table is empty")
	}
	for _, tc := range table.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			request, err := json.Marshal(map[string]any{
				"allowed_bots": tc.Raw,
				"author_id":    tc.AuthorID,
			})
			if err != nil {
				t.Fatalf("marshal request: %v", err)
			}
			resp, code := query(t, bin, string(request))
			if tc.Malformed {
				if code != 1 {
					t.Fatalf("exit code = %d, want 1 (fail closed)", code)
				}
				return
			}
			if code != 0 {
				t.Fatalf("exit code = %d, want 0", code)
			}
			if resp.Listed != tc.Listed || resp.Count != tc.Count {
				t.Fatalf("response = %+v, want listed=%v count=%d", resp, tc.Listed, tc.Count)
			}
		})
	}
}

// Envelope edges the verdict tables do not cover: missing keys, empty or
// whitespace stdin, trailing data after the one request, and unknown
// fields. The binary reads exactly ONE request from stdin.
func TestEnvelopeEdges(t *testing.T) {
	bin := binary(t)
	cases := []struct {
		name    string
		request string
		code    int
		listed  bool
		count   int
	}{
		{"missing allowed_bots narrows nothing", `{"author_id": "111"}`, 0, false, 0},
		{"missing author_id is never listed", `{"allowed_bots": ["111"]}`, 0, false, 1},
		{"empty object narrows nothing", `{}`, 0, false, 0},
		{"unknown fields are ignored",
			`{"allowed_bots": ["111"], "author_id": "111", "extra": true}`, 0, true, 1},
		// The one-request contract: the decoder reads a single JSON value;
		// anything after it is not this process's concern.
		{"trailing data after the request is ignored",
			`{"allowed_bots": ["111"], "author_id": "111"} garbage`, 0, true, 1},
		{"a second request is never read",
			`{"allowed_bots": ["111"], "author_id": "111"}` +
				` {"allowed_bots": [], "author_id": "999"}`, 0, true, 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			resp, code := query(t, bin, tc.request)
			if code != tc.code {
				t.Fatalf("exit code = %d, want %d", code, tc.code)
			}
			if code == 0 && (resp.Listed != tc.listed || resp.Count != tc.count) {
				t.Fatalf("response = %+v, want listed=%v count=%d", resp, tc.listed, tc.count)
			}
		})
	}
}

// Unreadable stdin is exit 2, not a silent empty verdict.
func TestUnreadableRequestExitsTwo(t *testing.T) {
	bin := binary(t)
	for _, stdin := range []string{"", "   ", "\n\t ", `{"allowed_bots":`} {
		if _, code := query(t, bin, stdin); code != 2 {
			t.Fatalf("stdin %q: exit code = %d, want 2", stdin, code)
		}
	}
}

// A JSON-array STRING whose entries are non-scalar is malformed end to
// end: the bridge fails closed (exit 1), exactly like a nested YAML list.
func TestMalformedJSONArrayStringExitsOne(t *testing.T) {
	bin := binary(t)
	for _, raw := range []string{
		`["111", true]`,
		`["111", null]`,
		`[["111"]]`,
		`["111", {"id": "222"}]`,
	} {
		request, err := json.Marshal(map[string]any{
			"allowed_bots": raw,
			"author_id":    "111",
		})
		if err != nil {
			t.Fatalf("marshal request: %v", err)
		}
		if _, code := query(t, bin, string(request)); code != 1 {
			t.Fatalf("raw %q: exit code = %d, want 1 (fail closed)", raw, code)
		}
	}
}

// The count in the verdict is the parsed set's size: duplicates collapse,
// and a snowflake given as a top-level JSON number keeps every digit.
func TestVerdictCountAndSnowflakeBoundaries(t *testing.T) {
	bin := binary(t)
	cases := []struct {
		name    string
		request string
		listed  bool
		count   int
	}{
		{"duplicate entries collapse in the count",
			`{"allowed_bots": ["111", "111", "222"], "author_id": "111"}`, true, 2},
		{"duplicate CSV entries collapse in the count",
			`{"allowed_bots": "111,111,222", "author_id": "222"}`, true, 2},
		{"top-level snowflake number keeps every digit",
			`{"allowed_bots": 123456789012345678901, "author_id": "123456789012345678901"}`,
			true, 1},
		{"top-level snowflake number denies the corrupted neighbour",
			`{"allowed_bots": 123456789012345678901, "author_id": "123456789012345680000"}`,
			false, 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			resp, code := query(t, bin, tc.request)
			if code != 0 {
				t.Fatalf("exit code = %d, want 0", code)
			}
			if resp.Listed != tc.listed || resp.Count != tc.count {
				t.Fatalf("response = %+v, want listed=%v count=%d", resp, tc.listed, tc.count)
			}
		})
	}
}
