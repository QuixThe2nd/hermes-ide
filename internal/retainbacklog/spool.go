package retainbacklog

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// SpoolFilename is the LEGACY emergency spool inside <home>/hindsight/: a
// single JSONL file the Python bridge appended to before per-entry files
// existed. It stays readable forever so data spooled by any older build
// still imports; new writers publish into SpoolDirName instead.
const SpoolFilename = "retain-backlog-spool.jsonl"

// SpoolDirName is the per-entry emergency spool directory inside
// <home>/hindsight/. When the runtime binary itself cannot be produced
// (no per-home cache, no process-global build, no prebuilt binary, no Go
// toolchain), the Python bridge emergency-spools would-be backlog entries
// here as IMMUTABLE per-entry files published atomically (write tmp +
// fsync + rename). The writer never mutates or removes a published file
// and the importer never rewrites or unlinks a file it has not fully
// imported, so the unlocked cross-process Python writer cannot lose an
// entry to the importer the way an append into a rewritten/unlinked
// shared file could. Names are "<19-digit ns timestamp>-<entry id
// hex>.entry": lexicographic order preserves FIFO; anything not ending in
// .entry (a live tmp, a quarantined .corrupt) is invisible to the import.
const SpoolDirName = "retain-backlog-spool"

// SpoolEntrySuffix marks one importable per-entry spool file.
const SpoolEntrySuffix = ".entry"

// SpoolPath returns the legacy emergency spool path for a home directory.
func SpoolPath(home string) string {
	return filepath.Join(home, "hindsight", SpoolFilename)
}

// SpoolDirPath returns the per-entry emergency spool directory for a home.
func SpoolDirPath(home string) string {
	return filepath.Join(home, "hindsight", SpoolDirName)
}

// SpoolStallError reports that an import stopped at an entry whose Add
// failed (I/O): that line and everything after it remain in the spool, in
// order, for the next pass. Not a command failure — the queue is healthy
// and every Add before the failing one already persisted.
type SpoolStallError struct {
	Err error
}

func (e *SpoolStallError) Error() string {
	return fmt.Sprintf("spool import stalled: %v", e.Err)
}

func (e *SpoolStallError) Unwrap() error { return e.Err }

// ImportSpool folds every emergency-spooled entry into the queue — FIFO,
// id honored, dedupe by id (the same Add the enqueue command uses) —
// under the caller's cross-process flock, as one locked transaction.
// Legacy spool-file lines come first (they predate the per-entry
// protocol), then per-entry spool files in name order. A legacy line that
// does not parse as an Entry is quarantined to <spool>.corrupt and a
// per-entry file that does not parse is renamed to <name>.corrupt — never
// silently dropped. The first Add failure stalls the import
// (SpoolStallError): the failing legacy line onward stays spooled and
// later entry files stay put, in order, for the next pass. A missing or
// blank spool is a no-op that removes nothing and leaves no backlog file
// behind.
//
// Concurrency contract with the unlocked Python writer: published entry
// files are immutable and an entry file is unlinked only AFTER its Add
// persisted, so a writer interleaving with this pass cannot lose data — a
// file it publishes after this pass's listing simply stays for the next
// one, and a file unlinked mid-crash-window re-imports and dedupes by id.
// Returns the counts imported (added or deduped) and quarantined.
func (q *Queue) ImportSpool(home string) (imported, quarantined int, err error) {
	spoolPath := SpoolPath(home)
	spoolDir := SpoolDirPath(home)
	raw, err := os.ReadFile(spoolPath)
	if err != nil {
		if !os.IsNotExist(err) {
			return 0, 0, err
		}
		raw = nil
	}
	files, ferr := spoolEntryFiles(spoolDir)
	if ferr != nil {
		return 0, 0, ferr
	}
	lines := nonblankLines(raw)
	var (
		remainder   []string
		stalled     error
		seenCorrupt = map[string]bool{}
	)
	for i, line := range lines {
		var e Entry
		if jerr := json.Unmarshal([]byte(line), &e); jerr != nil {
			if seenCorrupt[line] {
				continue
			}
			seenCorrupt[line] = true
			if qerr := appendQuarantine(spoolPath+CorruptSuffix, line); qerr != nil {
				return imported, quarantined, qerr
			}
			quarantined++
			continue
		}
		if _, _, aerr := q.Add(e); aerr != nil {
			stalled = aerr
			remainder = lines[i:]
			break
		}
		imported++
	}
	if stalled == nil {
		for _, name := range files {
			raw, rerr := os.ReadFile(filepath.Join(spoolDir, name))
			if rerr != nil {
				if os.IsNotExist(rerr) {
					continue // a concurrent importer already took it
				}
				stalled = rerr
				break
			}
			var e Entry
			if jerr := json.Unmarshal(bytes.TrimSpace(raw), &e); jerr != nil {
				if qerr := renameCorrupt(spoolDir, name); qerr != nil {
					return imported, quarantined, qerr
				}
				quarantined++
				continue
			}
			if _, _, aerr := q.Add(e); aerr != nil {
				stalled = aerr
				break
			}
			if rerr := os.Remove(filepath.Join(spoolDir, name)); rerr != nil && !os.IsNotExist(rerr) {
				return imported, quarantined, rerr
			}
			imported++
		}
	}
	if werr := rewriteSpool(spoolPath, remainder); werr != nil {
		return imported, quarantined, werr
	}
	if len(files) > 0 {
		if serr := fsyncDir(spoolDir); serr != nil {
			return imported, quarantined, serr
		}
	}
	if stalled != nil {
		return imported, quarantined, &SpoolStallError{Err: stalled}
	}
	return imported, quarantined, nil
}

// spoolEntryFiles lists the importable per-entry spool files in name (FIFO)
// order. A missing directory is an empty list, not an error; tmp and
// quarantined files do not match the suffix and are left untouched.
func spoolEntryFiles(dir string) ([]string, error) {
	ents, err := os.ReadDir(dir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	names := make([]string, 0, len(ents))
	for _, e := range ents {
		if !e.IsDir() && strings.HasSuffix(e.Name(), SpoolEntrySuffix) {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	return names, nil
}

// renameCorrupt quarantines one unrecoverable per-entry spool file in
// place: <name> -> <name>.corrupt. Recoverable by an operator, never
// replayed, never silently dropped — the file-level twin of the legacy
// .corrupt sidecar.
func renameCorrupt(dir, name string) error {
	if err := os.Rename(filepath.Join(dir, name), filepath.Join(dir, name+CorruptSuffix)); err != nil {
		return err
	}
	return fsyncDir(dir)
}

// nonblankLines splits spool bytes into the non-blank lines the import
// considers; blank padding is dropped, as the rewrite it replaces did.
func nonblankLines(raw []byte) []string {
	var lines []string
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSuffix(line, "\r")
		if strings.TrimSpace(line) != "" {
			lines = append(lines, line)
		}
	}
	return lines
}

// rewriteSpool persists the remaining spool lines atomically (tmp + fsync +
// rename + dir fsync); an empty remainder removes the spool so a drained
// file never lingers. Mirrors Queue.save's durability rules.
func rewriteSpool(path string, lines []string) error {
	if len(lines) == 0 {
		if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
			return err
		}
		return fsyncDir(filepath.Dir(path))
	}
	var buf bytes.Buffer
	for _, line := range lines {
		buf.WriteString(line)
		buf.WriteByte('\n')
	}
	tmp := path + ".tmp"
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	if _, werr := f.Write(buf.Bytes()); werr != nil {
		f.Close()
		os.Remove(tmp)
		return werr
	}
	if werr := f.Sync(); werr != nil {
		f.Close()
		os.Remove(tmp)
		return werr
	}
	if werr := f.Close(); werr != nil {
		os.Remove(tmp)
		return werr
	}
	if err := os.Rename(tmp, path); err != nil {
		os.Remove(tmp)
		return err
	}
	return fsyncDir(filepath.Dir(path))
}

// appendQuarantine appends one unrecoverable line to a .corrupt sidecar.
func appendQuarantine(sidecar, line string) error {
	if err := os.MkdirAll(filepath.Dir(sidecar), 0o755); err != nil {
		return err
	}
	f, err := os.OpenFile(sidecar, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := f.WriteString(line + "\n"); err != nil {
		return err
	}
	return f.Sync()
}
