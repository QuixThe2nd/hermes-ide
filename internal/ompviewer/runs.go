// Package ompviewer is the Go twin of the OMP delegate-lane viewer's
// run-registry and watch-URL contract, implemented by
// plugins/omp_viewer/runs.py, plugins/omp_viewer/url.py, and (reader side)
// plugins/omp_viewer/viewer/server.py. Same rules, independently tested.
// Production Python never imports or shells out to it; it exists so the
// contract has a second, line-balance-accounted implementation.
//
// Contract (shared with the Python twin):
//   - Registry: <HERMES_HOME>/omp-lane-runs/<sha256(workdir)>.json, one
//     JSON entry per run workdir, atomically replaced, mode 0600.
//   - An entry counts only with version 1 and an absolute, traversal-free
//     workdir; the server additionally requires the workdir to exist and
//     carry a .hermes-lane directory.
package ompviewer

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"
	"time"
)

const (
	// SchemaVersion is the only registry entry version the reader accepts.
	SchemaVersion = 1
	// RunsDirName is the registry directory under HERMES_HOME.
	RunsDirName = "omp-lane-runs"
	// MaxEntryBytes bounds one entry file; the reader refuses larger files
	// without parsing them.
	MaxEntryBytes = 4096
	// MaxFieldChars clamps every string field (in runes, mirroring the
	// Python twin's character-based clamp).
	MaxFieldChars = 1024
)

// RunEntry is one registry record. Field names are the wire contract with
// the stdlib-only viewer server, which re-reads this directory directly.
type RunEntry struct {
	Version    int    `json:"version"`
	Workdir    string `json:"workdir"`
	SessionDir string `json:"session_dir"`
	Model      string `json:"model"`
	RecordedAt string `json:"recorded_at"`
}

// EntryFileName maps a workdir to its deterministic registry file name:
// sha256 hex of the exact workdir string, plus ".json".
func EntryFileName(workdir string) string {
	sum := sha256.Sum256([]byte(workdir))
	return hex.EncodeToString(sum[:]) + ".json"
}

// ValidateWorkdir enforces the registry shape: non-empty, no NUL,
// absolute (POSIX semantics, like pathlib), and no ".." path part.
func ValidateWorkdir(workdir string) error {
	if workdir == "" {
		return errors.New("workdir is empty")
	}
	if strings.ContainsRune(workdir, 0) {
		return errors.New("workdir contains NUL")
	}
	if !strings.HasPrefix(workdir, "/") {
		return errors.New("workdir is not absolute")
	}
	for _, part := range strings.Split(workdir, "/") {
		if part == ".." {
			return errors.New("workdir contains traversal")
		}
	}
	return nil
}

// clamp mirrors the Python twin's character-based field cap.
func clamp(value string) string {
	runes := []rune(value)
	if len(runes) > MaxFieldChars {
		return string(runes[:MaxFieldChars])
	}
	return value
}

// NewRunEntry builds a validated entry; now is stamped second-precision
// UTC in the same ISO-8601 shape the Python twin writes.
func NewRunEntry(workdir, sessionDir, model string, now time.Time) (RunEntry, error) {
	if err := ValidateWorkdir(workdir); err != nil {
		return RunEntry{}, err
	}
	return RunEntry{
		Version:    SchemaVersion,
		Workdir:    clamp(workdir),
		SessionDir: clamp(sessionDir),
		Model:      clamp(model),
		RecordedAt: now.UTC().Truncate(time.Second).Format("2006-01-02T15:04:05-07:00"),
	}, nil
}

// Marshal renders the entry exactly as the Python writer persists it:
// compact JSON plus a trailing newline.
func Marshal(entry RunEntry) ([]byte, error) {
	raw, err := json.Marshal(entry)
	if err != nil {
		return nil, err
	}
	return append(raw, '\n'), nil
}
