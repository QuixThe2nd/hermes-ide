// Package stateerrors is the Go contract twin of the state store's
// persistence-error classification in hermes_state_errors.py.
//
// The Python module is the canonical source the gateway, SessionDB, and the
// turn-completion explainer classify through; this twin exists so the fork's
// Go-native statement of the same contract is executable and tested, per the
// Go-only feature policy (go-line-balance gate). Production Python never
// calls this package; both sides' tests consume testdata/classify_cases.json,
// following the internal/forcebg shared-contract pattern.
//
// The contract has two layers:
//
//   - Diagnostic predicates (IsTransient, IsMalformedDB, IsMalformedSchema,
//     IsDiskFull, IsFTSScopedCorruption) answer one narrow question each and
//     are type-gated exactly the way the Python sqlite3 isinstance checks are:
//     the malformed predicates require the DatabaseError family, IsTransient
//     requires OperationalError, and only IsDiskFull has an errno shortcut.
//   - Classify maps an error to one coarse user-guidance bucket (Cause). Its
//     precedence is load-bearing and mirrored exactly: typed Hermes errors
//     first, then FTS-scoped corruption (a known result code outranks prose),
//     then the ordered phrase table, then the disk bucket. Corruption must be
//     decided before the disk bucket because "database disk image is
//     malformed" contains the word "disk" (#77386).
//
// Errors cross RPC and logging boundaries as plain strings, so the unit of
// classification is ErrorView: the exception-type tag, the message text, the
// sqlite result code when the raising runtime attached one, and the ENOSPC
// flag an OSError carries. The Python contract test
// (tests/hermes_state/test_error_classification_go_contract.py) builds the
// real exceptions for each shared-table case and requires identical outputs.
package stateerrors

import (
	"regexp"
	"strings"
)

// Cause is one classify_persistence_error bucket. The string values are the
// contract: they appear verbatim in user-facing recovery guidance.
type Cause string

// The buckets of PERSISTENCE_ERROR_CAUSES, in the Python tuple's order.
const (
	CauseLocked            Cause = "locked"
	CauseCompression       Cause = "compression"
	CauseCompressionClosed Cause = "compression_closed"
	CauseTurnLease         Cause = "turn_lease"
	CauseCorrupt           Cause = "corrupt"
	CauseFTSIndex          Cause = "fts_index"
	CauseReplaced          Cause = "replaced"
	CauseDeletedWal        Cause = "deleted_wal"
	CauseDisk              Cause = "disk"
	CauseUnknown           Cause = "unknown"
)

// Causes enumerates every bucket Classify can return, mirroring
// PERSISTENCE_ERROR_CAUSES. Consumers range over it rather than hardcoding
// the set.
var Causes = []Cause{
	CauseLocked, CauseCompression, CauseCompressionClosed, CauseTurnLease,
	CauseCorrupt, CauseFTSIndex, CauseReplaced, CauseDeletedWal, CauseDisk,
	CauseUnknown,
}

// Kind tags which Python exception type raised the error. The isinstance
// gates in hermes_state_errors.py make type identity part of the contract,
// so the view carries it explicitly instead of guessing from the text.
type Kind string

const (
	// KindNone is a nil error: every predicate is false, Classify is unknown.
	KindNone Kind = "none"
	// KindString is a bare string — the shape errors take after RPC wrapping
	// or when recovered from log text. No isinstance gate can fire.
	KindString Kind = "string"
	// KindException is a plain Exception: phrase classification applies, but
	// the sqlite3-type-gated predicates stay closed.
	KindException Kind = "exception"
	// KindOSError is an OSError; ENOSPC on the view is the errno shortcut.
	KindOSError Kind = "os_error"
	// KindOperationalError is sqlite3.OperationalError — the only kind the
	// transient ("store BUSY, not gone") predicate accepts.
	KindOperationalError Kind = "operational_error"
	// KindDatabaseError is sqlite3.DatabaseError — the floor of the malformed
	// predicates' isinstance gate.
	KindDatabaseError Kind = "database_error"
	// The typed Hermes errors. The Python classification table is an ordered
	// isinstance ladder; the kinds below are already collapsed to leaves, so
	// the subclass relationships (SessionCompressionInProgressError is a
	// CompressionSessionBusyError, DeletedWalGenerationError is a
	// StateDbReplacedError, StateDbCorruptError is a sqlite3.DatabaseError)
	// are baked into the kind itself.
	KindTurnLease             Kind = "turn_lease"
	KindCompressionClosed     Kind = "compression_closed"
	KindCompressionBusy       Kind = "compression_busy"
	KindCompressionInProgress Kind = "compression_in_progress"
	KindReplaced              Kind = "replaced"
	KindDeletedWal            Kind = "deleted_wal"
	KindCorrupt               Kind = "corrupt"
)

// ErrorView is the transportable view of one raised error: everything the
// classification contract reads, nothing it doesn't.
type ErrorView struct {
	Kind Kind
	// Text is str(exc).lower() before lowering — predicates lowercase it
	// themselves, mirroring the Python substring checks.
	Text string
	// Code is sqlite_errorcode when the raising runtime attached one
	// (SQLite-raised errors on Python 3.11+). Nil means "no code known" —
	// manually constructed exceptions and RPC-wrapped strings land here,
	// which is what opens the text-provenance path in IsFTSScopedCorruption.
	Code *int
	// ENOSPC marks OSError(errno.ENOSPC): the disk-full shortcut that does
	// not depend on message wording.
	ENOSPC bool
}

// sqliteCorruptVtab is SQLITE_CORRUPT_VTAB: SQLite itself attributing the
// corruption to a virtual table. The value is stable across SQLite releases;
// Python reads it off the sqlite3 module with a 267 fallback.
const sqliteCorruptVtab = 267

// Marker tables, mirrored verbatim from hermes_state_errors.py. All matches
// are lowercase substring checks so wrapped RPC strings and logged message
// text classify the same as live exceptions.

// malformedSchemaMarkers: sqlite_master itself is inconsistent. Only PRAGMA
// writable_schema surgery still works; recovery rebuilds just the FTS layer.
var malformedSchemaMarkers = []string{"malformed database schema"}

// malformedDBMarkers add the generic corrupt-image text. Broad by design:
// this pair feeds diagnostics and offline recovery dispatch, never the
// runtime repair gate (a generic corrupt image does not identify the
// damaged object — runtime repair must use the schema-only predicate).
var malformedDBMarkers = []string{
	"malformed database schema",
	"database disk image is malformed",
}

// diskIOErrorMarker is SQLITE_IOERR as a substring so wrapped strings still
// classify. Shared by the read-only open retry and the write-path retry.
const diskIOErrorMarker = "disk i/o error"

// transientSQLiteMarkers mean "store BUSY, not gone": HTTP callers map them
// to 503 instead of 500. Corruption is deliberately absent — a malformed
// store must surface, not be retried into a timeout.
var transientSQLiteMarkers = []string{
	diskIOErrorMarker,
	"database is locked",
	"database table is locked",
	"busy",
}

// diskFullMarkers are the "filesystem cannot accept another write"
// substrings: SQLITE_FULL wording, ENOSPC spellings, and common phrasings.
var diskFullMarkers = []string{
	"no space left on device",
	"not enough space",
	"database or disk is full", // SQLITE_FULL
	"disk full",
	"full disk",
	"enospc",
}

// dbCorruptionMarkers mark the database FILE as structurally damaged.
// "database disk image is malformed" contains "disk", so the corruption
// bucket is checked before the disk bucket in Classify or B-tree corruption
// reads as "free some disk space" (#77386).
var dbCorruptionMarkers = []string{
	"malformed",
	"file is not a database",
	"not a database",
	"database corruption",
}

// ftsObjectRE names the FTS layer's objects: the virtual tables and their
// _data/_idx/_content/_docsize/_config shadow b-trees. FTS5 names the table
// in its own corruption reports.
var ftsObjectRE = regexp.MustCompile(`\bmessages_fts\w*`)

// causeByTypedKind mirrors _PERSISTENCE_CAUSE_BY_TYPE: the isinstance ladder
// runs before any text check because lease refusals contain neither "locked"
// nor "busy". Subclass collapsing is already applied at the Kind level:
// KindCompressionInProgress maps to CauseCompression the way
// SessionCompressionInProgressError subclasses CompressionSessionBusyError,
// and KindDeletedWal is distinct from KindReplaced the way
// DeletedWalGenerationError is checked before its StateDbReplacedError
// parent.
var causeByTypedKind = map[Kind]Cause{
	KindTurnLease:             CauseTurnLease,
	KindCompressionClosed:     CauseCompressionClosed,
	KindCompressionBusy:       CauseCompression,
	KindCompressionInProgress: CauseCompression,
	KindDeletedWal:            CauseDeletedWal,
	KindReplaced:              CauseReplaced,
	KindCorrupt:               CauseCorrupt,
}

// phraseCause is one row of _PERSISTENCE_CAUSE_BY_PHRASE: first matching row
// wins, so row order IS the precedence contract for RPC-wrapped strings.
type phraseCause struct {
	markers []string
	cause   Cause
}

// causeByPhrase mirrors _PERSISTENCE_CAUSE_BY_PHRASE row for row, in order:
// lease fencing and compression states before the sidecar/main-file split,
// corruption before locked/busy (both orderings load-bearing).
var causeByPhrase = []phraseCause{
	{[]string{"turn lease"}, CauseTurnLease},
	{[]string{"closed by compression"}, CauseCompressionClosed},
	{[]string{"being compressed", "compression lease"}, CauseCompression},
	// RPC-wrapped errors lose their exception type; the sidecar/main-file
	// split must survive stringification, same as the typed ladder.
	{[]string{"deleted state.db-wal", "deleted state.db-shm"}, CauseDeletedWal},
	{[]string{"was replaced underneath"}, CauseReplaced},
	{dbCorruptionMarkers, CauseCorrupt},
	{[]string{"locked", "busy"}, CauseLocked},
}

// containsAny reports whether text holds any marker. Single helper so every
// predicate shares the same substring semantics as the Python any(...in...).
func containsAny(text string, markers []string) bool {
	for _, marker := range markers {
		if strings.Contains(text, marker) {
			return true
		}
	}
	return false
}

func (v ErrorView) lowerText() string { return strings.ToLower(v.Text) }

// isDatabaseErrorFamily mirrors isinstance(exc, sqlite3.DatabaseError). The
// sqlite3 hierarchy subclasses DatabaseError widely: OperationalError IS a
// DatabaseError (alongside IntegrityError and the rest), and the typed
// StateDbCorruptError is one too. KindString/KindException/KindOSError and
// the RuntimeError-based typed lease errors stay outside the gate.
func (v ErrorView) isDatabaseErrorFamily() bool {
	switch v.Kind {
	case KindDatabaseError, KindOperationalError, KindCorrupt:
		return true
	}
	return false
}

// IsMalformedDB mirrors is_malformed_db_error: malformed-schema OR generic
// corrupt-image text, gated on the DatabaseError family. Diagnostics and
// explicit offline recovery only — runtime repair must use
// IsMalformedSchema, because a generic corrupt image does not identify the
// damaged object.
func IsMalformedDB(v ErrorView) bool {
	return v.isDatabaseErrorFamily() && containsAny(v.lowerText(), malformedDBMarkers)
}

// IsMalformedSchema mirrors is_malformed_schema_error: only SQLite's explicit
// malformed-schema text. A generic "disk image is malformed" may come from
// any B-tree or freelist page, so runtime repair fails closed on it.
func IsMalformedSchema(v ErrorView) bool {
	return v.isDatabaseErrorFamily() && containsAny(v.lowerText(), malformedSchemaMarkers)
}

// IsTransient mirrors is_transient_sqlite_error: "busy right now", not
// "damaged". One predicate so the read-path retry and the HTTP 503-vs-500
// split cannot drift apart on what counts as recoverable. Type-gated on
// OperationalError — a bare string that happens to say "busy" is not proof
// the store is contended.
func IsTransient(v ErrorView) bool {
	return v.Kind == KindOperationalError && containsAny(v.lowerText(), transientSQLiteMarkers)
}

// IsDiskFull mirrors is_disk_full_error: ENOSPC via the errno shortcut, or
// any disk-full wording in the message. KindNone is false — Python's
// explicit None guard.
func IsDiskFull(v ErrorView) bool {
	if v.Kind == KindNone {
		return false
	}
	if v.Kind == KindOSError && v.ENOSPC {
		return true
	}
	return containsAny(v.lowerText(), diskFullMarkers)
}

// IsFTSScopedCorruption mirrors is_fts_scoped_corruption_error: corruption
// SQLite itself attributes to the FTS index layer — the ONE provenance rule
// shared by the write-repair gate, the gateway transcript retry, and
// Classify (#96038, #97794).
//
// A known result code outranks prose: SQLITE_CORRUPT_VTAB is FTS-scoped even
// with the generic malformed-image text older SQLite builds emit, while any
// other known code contradicts FTS-looking prose and fails closed. Only
// without a code (Python < 3.11, RPC-wrapped strings) does the text decide,
// and then only an "fts5:" corruption report or a corruption marker that
// names a messages_fts* object counts.
func IsFTSScopedCorruption(v ErrorView) bool {
	if v.Kind == KindNone {
		return false
	}
	if v.Code != nil {
		return *v.Code == sqliteCorruptVtab
	}
	text := v.lowerText()
	if !ftsObjectRE.MatchString(text) {
		return false
	}
	if strings.HasPrefix(text, "fts5:") && strings.Contains(text, "corrupt") {
		return true
	}
	return containsAny(text, dbCorruptionMarkers)
}

// Classify mirrors classify_persistence_error: one coarse Cause bucket so
// the user's recovery guidance matches the failure. Precedence, each step
// load-bearing:
//
//  1. Typed Hermes errors. Lease refusals contain neither "locked" nor
//     "busy", and a typed answer must beat any prose the message happens to
//     carry — including an FTS-scoped result code riding on a typed error.
//  2. FTS-scoped corruption. Index damage is never whole-file corruption:
//     the transcript store is intact and the repair is a rebuild (#97794).
//  3. The ordered phrase table, for errors that survived RPC wrapping as
//     plain strings.
//  4. The disk bucket: disk-full wording, or the broad "disk" / "readonly" /
//     "read-only" fallback. Checked last among named buckets because the
//     corruption text contains the substring "disk" (#77386).
//  5. unknown.
func Classify(v ErrorView) Cause {
	if v.Kind == KindNone {
		return CauseUnknown
	}
	if cause, ok := causeByTypedKind[v.Kind]; ok {
		return cause
	}
	if IsFTSScopedCorruption(v) {
		return CauseFTSIndex
	}
	text := v.lowerText()
	for _, row := range causeByPhrase {
		if containsAny(text, row.markers) {
			return row.cause
		}
	}
	if IsDiskFull(v) ||
		strings.Contains(text, "disk") ||
		strings.Contains(text, "readonly") ||
		strings.Contains(text, "read-only") {
		return CauseDisk
	}
	return CauseUnknown
}
