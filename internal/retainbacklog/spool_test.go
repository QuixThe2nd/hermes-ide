package retainbacklog

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func spoolLine(t *testing.T, id, content string) string {
	t.Helper()
	line := map[string]any{
		"id": id, "bank_id": "b", "item": map[string]any{"content": content},
		"document_id": nil,
		"queued_at":   "2026-11-17T05:00:00Z", "retry_after": "2026-11-17T06:00:00Z",
	}
	return mustMarshal(t, line)
}

func mustMarshal(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func spoolQueue(t *testing.T, dir string) *Queue {
	t.Helper()
	return &Queue{path: filepath.Join(dir, BacklogFilename), quarantined: map[string]bool{}}
}

func writeSpool(t *testing.T, dir, text string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(dir, "hindsight"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "hindsight", SpoolFilename), []byte(text), 0o600); err != nil {
		t.Fatal(err)
	}
}

func spoolText(t *testing.T, dir string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(dir, "hindsight", SpoolFilename))
	if os.IsNotExist(err) {
		return ""
	}
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// The happy path every restart takes: all spool lines become queue entries
// in FIFO order with their ids honored, and the spool is gone.
func TestImportSpoolFoldsEntriesHonoringIDsAndClearsSpool(t *testing.T) {
	dir := t.TempDir()
	id1, id2 := "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "11111111-2222-4333-8444-555555555555"
	writeSpool(t, dir, spoolLine(t, id1, "one")+"\n"+spoolLine(t, id2, "two")+"\n")

	q := spoolQueue(t, dir)
	imported, quarantined, err := q.ImportSpool(filepath.Join(dir, "hindsight", SpoolFilename))
	if err != nil || imported != 2 || quarantined != 0 {
		t.Fatalf("imported=%d quarantined=%d err=%v", imported, quarantined, err)
	}
	if got := q.Len(); got != 2 {
		t.Fatalf("queue len=%d, want 2", got)
	}
	entries := q.Entries()
	if entries[0].ID != id1 || entries[1].ID != id2 {
		t.Fatalf("FIFO order broken: %s then %s", entries[0].ID, entries[1].ID)
	}
	if spoolText(t, dir) != "" {
		t.Fatalf("spool not cleared: %q", spoolText(t, dir))
	}
}

// Crash window between import and spool-clear: re-presenting the same spool
// line must not add a second queue entry (id dedupe) and still clears.
func TestImportSpoolDedupesByIDOnReplay(t *testing.T) {
	dir := t.TempDir()
	id := "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
	line := spoolLine(t, id, "keep")
	writeSpool(t, dir, line+"\n")
	q := spoolQueue(t, dir)
	if _, _, err := q.ImportSpool(filepath.Join(dir, "hindsight", SpoolFilename)); err != nil {
		t.Fatal(err)
	}
	writeSpool(t, dir, line+"\n") // the crash: line re-presented verbatim

	imported, _, err := q.ImportSpool(filepath.Join(dir, "hindsight", SpoolFilename))
	if err != nil || imported != 1 {
		t.Fatalf("imported=%d err=%v, want the replay counted once", imported, err)
	}
	if q.Len() != 1 {
		t.Fatalf("queue len=%d, want dedupe to 1", q.Len())
	}
	if spoolText(t, dir) != "" {
		t.Fatal("replayed spool not cleared")
	}
}

// Corrupt lines quarantine next to the spool and never strand the entries
// behind them; the good lines still import and the spool clears.
func TestImportSpoolQuarantinesCorruptLinesNotTheRest(t *testing.T) {
	dir := t.TempDir()
	id := "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
	good := spoolLine(t, id, "good")
	torn := `{"id": "%s", "bank_id"`
	writeSpool(t, dir, good+"\n"+torn+"\n"+"just garbage\n")

	q := spoolQueue(t, dir)
	imported, quarantined, err := q.ImportSpool(filepath.Join(dir, "hindsight", SpoolFilename))
	if err != nil || imported != 1 || quarantined != 2 {
		t.Fatalf("imported=%d quarantined=%d err=%v", imported, quarantined, err)
	}
	if q.Len() != 1 || q.Entries()[0].ID != id {
		t.Fatalf("the good line must import, len=%d", q.Len())
	}
	corrupt, rerr := os.ReadFile(filepath.Join(dir, "hindsight", SpoolFilename+CorruptSuffix))
	if rerr != nil || !strings.Contains(string(corrupt), "just garbage") || !strings.Contains(string(corrupt), torn) {
		t.Fatalf("corrupt sidecar=%q err=%v, want both bad lines", corrupt, rerr)
	}
	if strings.Contains(string(corrupt), `"good"`) {
		t.Fatal("the intact good line must never be quarantined")
	}
	if spoolText(t, dir) != "" {
		t.Fatal("spool not cleared after quarantine import")
	}
}

// A missing spool is a no-op that must not materialize a backlog file.
func TestImportSpoolMissingIsNoOp(t *testing.T) {
	dir := t.TempDir()
	q := spoolQueue(t, dir)
	imported, quarantined, err := q.ImportSpool(filepath.Join(dir, "hindsight", SpoolFilename))
	if err != nil || imported != 0 || quarantined != 0 {
		t.Fatalf("imported=%d quarantined=%d err=%v, want no-op", imported, quarantined, err)
	}
	if _, serr := os.Stat(q.path); !os.IsNotExist(serr) {
		t.Fatalf("no-op import must not create a backlog file, stat err=%v", serr)
	}
}

// A blank-only spool is removed (nothing to import), matching the rewrite
// it replaces; no backlog file appears.
func TestImportSpoolBlankOnlyRemovesSpool(t *testing.T) {
	dir := t.TempDir()
	writeSpool(t, dir, "\n   \n\r\n")
	q := spoolQueue(t, dir)
	imported, _, err := q.ImportSpool(filepath.Join(dir, "hindsight", SpoolFilename))
	if err != nil || imported != 0 {
		t.Fatalf("imported=%d err=%v, want no-op", imported, err)
	}
	if spoolText(t, dir) != "" {
		t.Fatal("blank spool must be removed")
	}
	if _, serr := os.Stat(q.path); !os.IsNotExist(serr) {
		t.Fatal("blank spool import must not create a backlog file")
	}
}

// Spool parity with enqueue: an entry whose id is empty gets one minted
// before any submission could reference it.
func TestImportSpoolMintsMissingIDLikeEnqueue(t *testing.T) {
	dir := t.TempDir()
	writeSpool(t, dir, spoolLine(t, "", "mint me")+"\n")
	q := spoolQueue(t, dir)
	if _, _, err := q.ImportSpool(filepath.Join(dir, "hindsight", SpoolFilename)); err != nil {
		t.Fatal(err)
	}
	entries := q.Entries()
	if len(entries) != 1 || !uuidRe.MatchString(entries[0].ID) {
		t.Fatalf("entries=%+v, want one minted uuid id", entries)
	}
}

// A stall after progress keeps only the tail: the first line dedupes
// against the seeded queue entry (no save, no error), the second line's
// Add hits the directory-backed path and stalls — only it stays spooled.
func TestImportSpoolStallAfterProgressKeepsOnlyTail(t *testing.T) {
	dir := t.TempDir()
	in := Entry{ID: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
		BankID: "b", Item: item(t, "in"), RetryAfter: time.Now()}
	out := spoolLine(t, "11111111-2222-4333-8444-555555555555", "out")
	writeSpool(t, dir, mustMarshal(t, in)+"\n"+out+"\n")
	q := spoolQueue(t, dir)
	if _, _, err := q.Add(in); err != nil { // seeds the backlog file
		t.Fatal(err)
	}
	if err := os.Remove(q.path); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(q.path, 0o755); err != nil { // later saves fail
		t.Fatal(err)
	}

	imported, _, err := q.ImportSpool(filepath.Join(dir, "hindsight", SpoolFilename))
	var stall *SpoolStallError
	if !errors.As(err, &stall) || imported != 1 {
		t.Fatalf("imported=%d err=%v, want dedupe in then stall", imported, err)
	}
	if got := spoolText(t, dir); got != out+"\n" {
		t.Fatalf("remainder=%q, want only the failing line kept", got)
	}
}
