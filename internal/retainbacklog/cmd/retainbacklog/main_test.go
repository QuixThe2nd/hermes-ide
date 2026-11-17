package main

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/QuixThe2nd/hermes-ide/internal/retainbacklog"
)

const opID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

// seedEntry writes one due, submitted entry directly so drain tests start
// from a known on-disk shape.
func seedEntry(t *testing.T, home string, e retainbacklog.Entry) {
	t.Helper()
	e.ID = opID
	e.BankID = "bank"
	if len(e.Item) == 0 {
		e.Item = json.RawMessage(`{"content":"x"}`)
	}
	if e.RetryAfter.IsZero() {
		e.RetryAfter = time.Now().Add(-time.Minute)
	}
	q, err := retainbacklog.Open(retainbacklog.BacklogPath(home))
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := q.Add(e); err != nil {
		t.Fatal(err)
	}
}

func backlogPath(home string) string {
	return retainbacklog.BacklogPath(home)
}

func TestDrainProtocolCompletedRemovesEntry(t *testing.T) {
	home := t.TempDir()
	seedEntry(t, home, retainbacklog.Entry{Submitted: true})

	inR, inW, _ := os.Pipe()
	outR, outW, _ := os.Pipe()
	errCh := make(chan error, 1)
	go func() { errCh <- run([]string{"drain", "--home", home}, inR, outW) }()

	scanner := bufio.NewScanner(outR)
	if !scanner.Scan() {
		t.Fatal("no status request emitted")
	}
	var req map[string]any
	if err := json.Unmarshal([]byte(scanner.Text()), &req); err != nil {
		t.Fatal(err)
	}
	if req["type"] != "status" || req["id"] != opID || req["bank_id"] != "bank" {
		t.Fatalf("request=%v, want status probe for the entry", req)
	}
	inW.WriteString(`{"id":"` + opID + `","status":"completed"}` + "\n")
	if !scanner.Scan() {
		t.Fatal("no done line emitted")
	}
	var done map[string]any
	if err := json.Unmarshal([]byte(scanner.Text()), &done); err != nil {
		t.Fatal(err)
	}
	if done["type"] != "done" || done["next_retry"] != nil || done["remaining"] != float64(0) {
		t.Fatalf("done=%v, want removal and empty queue", done)
	}
	inW.Close()
	outW.Close()
	if err := <-errCh; err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(backlogPath(home)); !os.IsNotExist(err) {
		t.Fatalf("backlog file must be unlinked, err=%v", err)
	}
}

func TestDrainProtocolFailedGoesThroughRetryOp(t *testing.T) {
	home := t.TempDir()
	seedEntry(t, home, retainbacklog.Entry{Submitted: true})

	inR, inW, _ := os.Pipe()
	outR, outW, _ := os.Pipe()
	errCh := make(chan error, 1)
	go func() { errCh <- run([]string{"drain", "--home", home}, inR, outW) }()

	scanner := bufio.NewScanner(outR)
	readOne := func() string {
		if !scanner.Scan() {
			t.Fatal("binary closed stdout early")
		}
		return scanner.Text()
	}
	if line := readOne(); !strings.Contains(line, `"type":"status"`) {
		t.Fatalf("first line=%s, want status probe", line)
	}
	// failed -> the server's own retry endpoint, never delete+resubmit.
	inW.WriteString(`{"id":"` + opID + `","status":"failed"}` + "\n")
	if line := readOne(); !strings.Contains(line, `"type":"retry_op"`) {
		t.Fatalf("second line=%s, want retry_op request", line)
	}
	inW.WriteString(`{"id":"` + opID + `","result":"ok"}` + "\n")
	doneLine := readOne()
	if !strings.Contains(doneLine, `"type":"done"`) {
		t.Fatalf("third line=%s, want done", doneLine)
	}
	inW.Close()
	outW.Close()
	if err := <-errCh; err != nil {
		t.Fatal(err)
	}
	var done map[string]any
	if err := json.Unmarshal([]byte(doneLine), &done); err != nil {
		t.Fatal(err)
	}
	if done["remaining"] != float64(1) {
		t.Fatalf("done=%v, want the entry kept for polling", done)
	}
	if done["next_retry"] == nil {
		t.Fatalf("done=%v, want a next_retry for the re-queued op", done)
	}
	if _, err := os.Stat(backlogPath(home)); err != nil {
		t.Fatalf("entry file must persist, err=%v", err)
	}
}

func TestDrainProtocolNotFoundSubmitsVerbatimEntry(t *testing.T) {
	home := t.TempDir()
	seedEntry(t, home, retainbacklog.Entry{DocumentID: "doc-1", Submitted: true})

	inR, inW, _ := os.Pipe()
	outR, outW, _ := os.Pipe()
	errCh := make(chan error, 1)
	go func() { errCh <- run([]string{"drain", "--home", home}, inR, outW) }()

	// Read the status request, answer not_found, then read the submit
	// request and ack it.
	scanner := bufio.NewScanner(outR)
	scanner.Buffer(make([]byte, 0, 64*1024), 64*1024*1024)
	var lines []string
	readOne := func() string {
		if !scanner.Scan() {
			t.Fatal("binary closed stdout early")
		}
		line := scanner.Text()
		lines = append(lines, line)
		return line
	}
	if line := readOne(); !strings.Contains(line, `"type":"status"`) {
		t.Fatalf("first line=%s, want status probe", line)
	}
	inW.WriteString(`{"id":"` + opID + `","status":"not_found"}` + "\n")
	if line := readOne(); !strings.Contains(line, `"type":"submit"`) {
		t.Fatalf("second line=%s, want submit request", line)
	}
	var submitReq map[string]any
	if err := json.Unmarshal([]byte(lines[len(lines)-1]), &submitReq); err != nil {
		t.Fatal(err)
	}
	entry, _ := submitReq["entry"].(map[string]any)
	if entry["id"] != opID || entry["document_id"] != "doc-1" || entry["bank_id"] != "bank" {
		t.Fatalf("submit entry=%v, want the verbatim replay payload", entry)
	}
	if _, ok := entry["item"].(map[string]any); !ok {
		t.Fatalf("submit entry=%v, want the item payload", entry)
	}
	inW.WriteString(`{"id":"` + opID + `","result":"ok"}` + "\n")
	doneLine := readOne()
	if !strings.Contains(doneLine, `"type":"done"`) {
		t.Fatalf("last line=%s, want done", doneLine)
	}
	var done map[string]any
	if err := json.Unmarshal([]byte(doneLine), &done); err != nil {
		t.Fatal(err)
	}
	if done["remaining"] != float64(1) || done["next_retry"] == nil {
		t.Fatalf("done=%v, want kept entry with a poll deadline", done)
	}
	inW.Close()
	outW.Close()
	if err := <-errCh; err != nil {
		t.Fatal(err)
	}
}

func TestDrainProtocolRateErrorSchedulesWithoutFailing(t *testing.T) {
	home := t.TempDir()
	seedEntry(t, home, retainbacklog.Entry{Submitted: true})

	inR, inW, _ := os.Pipe()
	outR, outW, _ := os.Pipe()
	errCh := make(chan error, 1)
	go func() { errCh <- run([]string{"drain", "--home", home}, inR, outW) }()

	scanner := bufio.NewScanner(outR)
	if !scanner.Scan() {
		t.Fatal("no status request")
	}
	hint := time.Now().Add(2 * time.Hour).UTC().Format(time.RFC3339)
	inW.WriteString(`{"id":"` + opID + `","error":"rate","retry_after":"` + hint + `"}` + "\n")
	if !scanner.Scan() {
		t.Fatal("no done line")
	}
	var done map[string]any
	if err := json.Unmarshal([]byte(scanner.Text()), &done); err != nil {
		t.Fatal(err)
	}
	if done["type"] != "done" || done["remaining"] != float64(1) {
		t.Fatalf("done=%v, want scheduled retry with kept entry", done)
	}
	if got, _ := done["next_retry"].(string); !strings.HasPrefix(got, hint[:16]) {
		t.Fatalf("next_retry=%v, want the provider hint %s", done["next_retry"], hint)
	}
	inW.Close()
	outW.Close()
	if err := <-errCh; err != nil {
		t.Fatalf("a rate-limited drain must not fail the command: %v", err)
	}
}

func TestEnqueueMintsIDAndPersists(t *testing.T) {
	home := t.TempDir()
	entry := `{"bank_id":"bank","item":{"content":"queued"},"document_id":"doc-1","retry_after":"` +
		time.Now().Add(time.Hour).UTC().Format(time.RFC3339) + `"}`
	var out strings.Builder
	if err := run([]string{"enqueue", "--home", home}, strings.NewReader(entry), &out); err != nil {
		t.Fatal(err)
	}
	var reply map[string]any
	if err := json.Unmarshal([]byte(out.String()), &reply); err != nil {
		t.Fatal(err)
	}
	id, _ := reply["id"].(string)
	if id == "" {
		t.Fatalf("reply=%v, want a minted id", reply)
	}

	q, err := retainbacklog.Open(backlogPath(home))
	if err != nil {
		t.Fatal(err)
	}
	entries := q.Entries()
	if len(entries) != 1 || entries[0].ID != id || entries[0].DocumentID != "doc-1" {
		t.Fatalf("entries=%+v, want the persisted entry under its minted id", entries)
	}
}

func TestPeekReportsDeadlineAndCount(t *testing.T) {
	home := t.TempDir()
	seedEntry(t, home, retainbacklog.Entry{Submitted: true, RetryAfter: time.Now().Add(90 * time.Minute)})
	var out strings.Builder
	if err := run([]string{"peek", "--home", home}, strings.NewReader(""), &out); err != nil {
		t.Fatal(err)
	}
	var done map[string]any
	if err := json.Unmarshal([]byte(out.String()), &done); err != nil {
		t.Fatal(err)
	}
	if done["remaining"] != float64(1) || done["next_retry"] == nil {
		t.Fatalf("peek=%v, want one entry with a future deadline", done)
	}
}

func TestLockFailClosedWhenUnwritable(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root bypasses directory permissions")
	}
	home := t.TempDir()
	blocked := filepath.Join(home, "hindsight")
	if err := os.Mkdir(blocked, 0o500); err != nil {
		t.Fatal(err)
	}
	// The lock file itself is inside a read-only dir: acquiring it must FAIL,
	// never proceed unlocked.
	var out strings.Builder
	entry := `{"bank_id":"b","item":{"content":"x"},"retry_after":"2026-01-01T00:00:00Z"}`
	if err := run([]string{"enqueue", "--home", home}, strings.NewReader(entry), &out); err == nil {
		t.Fatal("enqueue must fail when the backlog lock cannot be taken")
	}
	if _, err := os.Stat(filepath.Join(blocked, "retain-backlog.jsonl")); !os.IsNotExist(err) {
		t.Fatalf("no queue file may appear without the lock, err=%v", err)
	}
}
