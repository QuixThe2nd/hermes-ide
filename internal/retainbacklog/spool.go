package retainbacklog

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// SpoolFilename is the emergency spool inside <home>/hindsight/. When the
// runtime binary itself cannot be produced (no per-home cache, no
// process-global build, no prebuilt binary, no Go toolchain), the Python
// bridge appends would-be backlog entries here with fsync instead of losing
// them (RetainBacklog._spool in retain_backlog.py). ImportSpool folds those
// lines back into the real queue as soon as a runtime is producible again.
const SpoolFilename = "retain-backlog-spool.jsonl"

// SpoolPath returns the emergency spool path for a home directory.
func SpoolPath(home string) string {
	return filepath.Join(home, "hindsight", SpoolFilename)
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

// ImportSpool folds every parseable spool line into the queue — FIFO, id
// honored, dedupe by id (the same Add the enqueue command uses) — and
// rewrites the spool atomically with what remains. Lines that do not parse
// as an Entry are quarantined to <spool>.corrupt, never silently dropped.
// The first Add failure stalls the import (SpoolStallError): the failing
// line onward stays spooled. A missing or blank spool is a no-op that
// removes nothing and leaves no backlog file behind. The caller holds the
// cross-process flock: the whole import runs as one locked transaction,
// unlike the per-line enqueue loop it replaces.
// Returns the line counts imported (added or deduped) and quarantined.
func (q *Queue) ImportSpool(spoolPath string) (imported, quarantined int, err error) {
	raw, err := os.ReadFile(spoolPath)
	if err != nil {
		if os.IsNotExist(err) {
			return 0, 0, nil
		}
		return 0, 0, err
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
	if werr := rewriteSpool(spoolPath, remainder); werr != nil {
		return imported, quarantined, werr
	}
	if stalled != nil {
		return imported, quarantined, &SpoolStallError{Err: stalled}
	}
	return imported, quarantined, nil
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
