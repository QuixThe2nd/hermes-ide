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
	imported, quarantined, err := q.ImportSpool(dir)
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
	if _, _, err := q.ImportSpool(dir); err != nil {
		t.Fatal(err)
	}
	writeSpool(t, dir, line+"\n") // the crash: line re-presented verbatim

	imported, _, err := q.ImportSpool(dir)
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
	imported, quarantined, err := q.ImportSpool(dir)
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
	imported, quarantined, err := q.ImportSpool(dir)
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
	imported, _, err := q.ImportSpool(dir)
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
	if _, _, err := q.ImportSpool(dir); err != nil {
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

	imported, _, err := q.ImportSpool(dir)
	var stall *SpoolStallError
	if !errors.As(err, &stall) || imported != 1 {
		t.Fatalf("imported=%d err=%v, want dedupe in then stall", imported, err)
	}
	if got := spoolText(t, dir); got != out+"\n" {
		t.Fatalf("remainder=%q, want only the failing line kept", got)
	}
}

// -- per-entry spool directory ------------------------------------------------

func spoolDirPathFor(dir string) string {
	return filepath.Join(dir, "hindsight", SpoolDirName)
}

func writeSpoolEntryFile(t *testing.T, dir, name, body string) {
	t.Helper()
	if err := os.MkdirAll(spoolDirPathFor(dir), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(spoolDirPathFor(dir), name), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

func spoolDirFileExists(t *testing.T, dir, name string) bool {
	t.Helper()
	_, err := os.Stat(filepath.Join(spoolDirPathFor(dir), name))
	return err == nil
}

// Per-entry files import in name (FIFO) order with ids honored, and each
// is unlinked only after its Add persisted.
func TestImportSpoolDirFoldsEntryFilesFIFOAndUnlinks(t *testing.T) {
	dir := t.TempDir()
	id1, id2 := "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "11111111-2222-4333-8444-555555555555"
	writeSpoolEntryFile(t, dir,
		"0000000000000000001-aaaabbbb4ccc8dddeeeeeeeeeeee.entry", spoolLine(t, id1, "one"))
	writeSpoolEntryFile(t, dir,
		"0000000000000000002-11111111222243338444555555555555.entry", spoolLine(t, id2, "two"))

	q := spoolQueue(t, dir)
	imported, quarantined, err := q.ImportSpool(dir)
	if err != nil || imported != 2 || quarantined != 0 {
		t.Fatalf("imported=%d quarantined=%d err=%v", imported, quarantined, err)
	}
	entries := q.Entries()
	if len(entries) != 2 || entries[0].ID != id1 || entries[1].ID != id2 {
		t.Fatalf("entries=%+v, want FIFO by file name", entries)
	}
	for _, name := range []string{
		"0000000000000000001-aaaabbbb4ccc8dddeeeeeeeeeeee.entry",
		"0000000000000000002-11111111222243338444555555555555.entry",
	} {
		if spoolDirFileExists(t, dir, name) {
			t.Fatalf("%s must be unlinked after import", name)
		}
	}
}

// A live tmp file and an already-quarantined file are invisible to the
// import: not read, not imported, not removed.
func TestImportSpoolDirIgnoresTmpAndCorruptFiles(t *testing.T) {
	dir := t.TempDir()
	id := "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
	writeSpoolEntryFile(t, dir,
		"0000000000000000001-aaaabbbb4ccc8dddeeeeeeeeeeee.entry", spoolLine(t, id, "keep"))
	writeSpoolEntryFile(t, dir, "tmp-abc123", spoolLine(t, "in-flight", "half written"))
	writeSpoolEntryFile(t, dir, "0000000000000000000-old.entry.corrupt", "not json")

	q := spoolQueue(t, dir)
	imported, quarantined, err := q.ImportSpool(dir)
	if err != nil || imported != 1 || quarantined != 0 {
		t.Fatalf("imported=%d quarantined=%d err=%v", imported, quarantined, err)
	}
	for _, name := range []string{"tmp-abc123", "0000000000000000000-old.entry.corrupt"} {
		if !spoolDirFileExists(t, dir, name) {
			t.Fatalf("%s must be left untouched", name)
		}
	}
}

// An unparseable entry file is quarantined by rename, never silently
// dropped, and does not strand the files behind it.
func TestImportSpoolDirQuarantinesUnparseableFile(t *testing.T) {
	dir := t.TempDir()
	id := "11111111-2222-4333-8444-555555555555"
	writeSpoolEntryFile(t, dir,
		"0000000000000000001-badbadbadbadbadbadbadbadbadbad01.entry", "not json\n")
	writeSpoolEntryFile(t, dir,
		"0000000000000000002-11111111222243338444555555555555.entry", spoolLine(t, id, "good"))

	q := spoolQueue(t, dir)
	imported, quarantined, err := q.ImportSpool(dir)
	if err != nil || imported != 1 || quarantined != 1 {
		t.Fatalf("imported=%d quarantined=%d err=%v", imported, quarantined, err)
	}
	if !spoolDirFileExists(t, dir,
		"0000000000000000001-badbadbadbadbadbadbadbadbadbad01.entry.corrupt") {
		t.Fatal("bad file must be renamed to .corrupt, not dropped")
	}
	if spoolDirFileExists(t, dir,
		"0000000000000000002-11111111222243338444555555555555.entry") {
		t.Fatal("good file must be imported and unlinked")
	}
}

// A stall mid-directory keeps the failing file and every later one, in
// order; files already imported stay imported (their Adds persisted).
func TestImportSpoolDirStallKeepsFailingAndLaterFiles(t *testing.T) {
	dir := t.TempDir()
	seeded := Entry{ID: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
		BankID: "b", Item: item(t, "seed"), RetryAfter: time.Now()}
	q := spoolQueue(t, dir)
	if _, _, err := q.Add(seeded); err != nil { // seeds the backlog file
		t.Fatal(err)
	}
	if err := os.Remove(q.path); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(q.path, 0o755); err != nil { // later saves fail
		t.Fatal(err)
	}
	// Same id as the seeded entry: dedupes without saving, so it imports
	// and unlinks even while saves fail.
	writeSpoolEntryFile(t, dir,
		"0000000000000000001-aaaabbbb4ccc8dddeeeeeeeeeeee.entry", spoolLine(t, seeded.ID, "dupe"))
	writeSpoolEntryFile(t, dir,
		"0000000000000000002-11111111222243338444555555555555.entry",
		spoolLine(t, "11111111-2222-4333-8444-555555555555", "stalls"))
	writeSpoolEntryFile(t, dir,
		"0000000000000000003-33333333444445555666677777777777.entry",
		spoolLine(t, "33333333-4444-4555-5666-777777777777", "after"))

	imported, _, err := q.ImportSpool(dir)
	var stall *SpoolStallError
	if !errors.As(err, &stall) || imported != 1 {
		t.Fatalf("imported=%d err=%v, want dedupe in then stall", imported, err)
	}
	if spoolDirFileExists(t, dir,
		"0000000000000000001-aaaabbbb4ccc8dddeeeeeeeeeeee.entry") {
		t.Fatal("deduped file must be unlinked")
	}
	for _, name := range []string{
		"0000000000000000002-11111111222243338444555555555555.entry",
		"0000000000000000003-33333333444445555666677777777777.entry",
	} {
		if !spoolDirFileExists(t, dir, name) {
			t.Fatalf("%s must stay spooled after the stall", name)
		}
	}
}

// Legacy spool-file lines import BEFORE per-entry files: a home upgraded
// mid-outage keeps one global FIFO order.
func TestImportSpoolLegacyLinesImportBeforeDirEntries(t *testing.T) {
	dir := t.TempDir()
	idFile := "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
	idDir := "11111111-2222-4333-8444-555555555555"
	writeSpool(t, dir, spoolLine(t, idFile, "older")+"\n")
	writeSpoolEntryFile(t, dir,
		"0000000000000000001-11111111222243338444555555555555.entry", spoolLine(t, idDir, "newer"))

	q := spoolQueue(t, dir)
	imported, _, err := q.ImportSpool(dir)
	if err != nil || imported != 2 {
		t.Fatalf("imported=%d err=%v", imported, err)
	}
	entries := q.Entries()
	if len(entries) != 2 || entries[0].ID != idFile || entries[1].ID != idDir {
		t.Fatalf("entries=%+v, want legacy line first", entries)
	}
	if spoolText(t, dir) != "" {
		t.Fatalf("legacy spool must clear: %q", spoolText(t, dir))
	}
}
