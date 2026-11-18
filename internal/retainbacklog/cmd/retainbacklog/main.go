// Command retainbacklog is the durable backlog runtime invoked by the
// Hindsight memory provider bridge (plugins/memory/hindsight/retain_backlog.py).
//
// All queue state, persistence, and reconcile decisions live in this binary;
// the host (Python) drives it over line-delimited JSON on stdio and owns the
// network half of the drain (SDK calls under the caller's profile secret
// scope). The binary holds the cross-process flock for the whole command.
//
// Subcommands:
//
//	retainbacklog enqueue --home DIR
//	      stdin:  one entry JSON (id may be empty — minted here before any
//	              submission; retry_after required)
//	      stdout: {"id": "<uuid>"}
//
//	retainbacklog drain --home DIR
//	      protocol per due entry (FIFO, each gated on its own retry_after):
//	        -> {"type":"status","id":..,"bank_id":..}
//	        <- {"id":..,"status":"completed|pending|processing|failed|cancelled|not_found"}
//	         | {"id":..,"error":"rate","retry_after":"<rfc3339>"}
//	         | {"id":..,"error":"other"}
//	        -> {"type":"retry_op","id":..,"bank_id":..}          (failed/cancelled)
//	        <- {"id":..,"result":"ok|conflict|gone|permanent"} | {"id":..,"error":...}
//	      final: {"type":"done","next_retry":"<rfc3339>|null","remaining":N}
//
//	retainbacklog import-spool --home DIR
//	      folds <home>/hindsight/retain-backlog-spool.jsonl into the queue
//	      under the same flock: FIFO, id honored, dedupe by id; corrupt lines
//	      quarantine to <spool>.corrupt; unimported remainder stays spooled
//	      stdout: {"imported":N,"quarantined":M}
//
// Exit codes: 0 ok, 2 usage, 1 runtime failure (message on stderr). A
// runtime failure after partial progress leaves a consistent persisted
// queue — every mutation is saved before the next step.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"time"

	"github.com/QuixThe2nd/hermes-ide/internal/retainbacklog"
)

func main() {
	if err := run(os.Args[1:], os.Stdin, os.Stdout); err != nil {
		fmt.Fprintf(os.Stderr, "retainbacklog: %v\n", err)
		os.Exit(exitCodeFor(err))
	}
}

func exitCodeFor(err error) int {
	if errors.Is(err, flag.ErrHelp) {
		return 0
	}
	var usage *usageError
	if errors.As(err, &usage) {
		return 2
	}
	return 1
}

type usageError struct{ err error }

func (u *usageError) Error() string { return u.err.Error() }
func (u *usageError) Unwrap() error { return u.err }

func run(args []string, stdin io.Reader, stdout io.Writer) error {
	if len(args) < 1 {
		return &usageError{errors.New("usage: retainbacklog <enqueue|drain|peek|import-spool> --home DIR")}
	}
	fs := flag.NewFlagSet("retainbacklog "+args[0], flag.ContinueOnError)
	home := fs.String("home", "", "HERMES_HOME of the owning profile")
	if err := fs.Parse(args[1:]); err != nil {
		return &usageError{err}
	}
	if *home == "" {
		return &usageError{errors.New("--home is required")}
	}
	switch args[0] {
	case "enqueue":
		return cmdEnqueue(*home, stdin, stdout)
	case "drain":
		return cmdDrain(*home, stdin, stdout)
	case "peek":
		return cmdPeek(*home, stdout)
	case "import-spool":
		return cmdImportSpool(*home, stdout)
	default:
		return &usageError{fmt.Errorf("unknown subcommand %q", args[0])}
	}
}

func openLocked(home string) (*retainbacklog.LockedQueue, error) {
	return retainbacklog.OpenLocked(home)
}

func cmdEnqueue(home string, stdin io.Reader, stdout io.Writer) error {
	lock, err := openLocked(home)
	if err != nil {
		return err
	}
	defer lock.Close()
	var entry retainbacklog.Entry
	if err := json.NewDecoder(stdin).Decode(&entry); err != nil {
		return fmt.Errorf("decode entry: %w", err)
	}
	id, _, err := lock.Queue().Add(entry)
	if err != nil {
		return err
	}
	out, _ := json.Marshal(map[string]any{"id": id})
	fmt.Fprintln(stdout, string(out))
	return nil
}

func cmdPeek(home string, stdout io.Writer) error {
	lock, err := openLocked(home)
	if err != nil {
		return err
	}
	defer lock.Close()
	emitDone(stdout, lock.Queue().NextRetry(), lock.Queue().Len())
	return nil
}

func cmdImportSpool(home string, stdout io.Writer) error {
	lock, err := openLocked(home)
	if err != nil {
		return err
	}
	defer lock.Close()
	imported, quarantined, err := lock.Queue().ImportSpool(retainbacklog.SpoolPath(home))
	out, _ := json.Marshal(map[string]any{"imported": imported, "quarantined": quarantined})
	fmt.Fprintln(stdout, string(out))
	var stall *retainbacklog.SpoolStallError
	if errors.As(err, &stall) {
		fmt.Fprintf(os.Stderr, "retainbacklog: %v\n", stall)
		return nil // not a command failure: the remainder stays spooled, next pass retries
	}
	return err
}

// hostRemote adapts the stdio protocol to retainbacklog.Remote: every Remote
// call writes one request line and blocks for one reply line from the host.
type hostRemote struct {
	out  *json.Encoder
	in   *json.Decoder
	logf func(format string, args ...any)
}

type hostReply struct {
	ID         string `json:"id"`
	Status     string `json:"status"`
	Result     string `json:"result"`
	Error      string `json:"error"`
	RetryAfter string `json:"retry_after"`
}

func (h *hostRemote) ask(req map[string]any, id string) (hostReply, error) {
	if err := h.out.Encode(req); err != nil {
		return hostReply{}, err
	}
	var reply hostReply
	if err := h.in.Decode(&reply); err != nil {
		return hostReply{}, err
	}
	if reply.ID != "" && reply.ID != id {
		return hostReply{}, fmt.Errorf("protocol: reply id %q does not match request %q", reply.ID, id)
	}
	return reply, nil
}

func (h *hostRemote) classify(err error, reply hostReply, what, id string) error {
	switch reply.Error {
	case "rate":
		hint := time.Now().UTC().Add(retainbacklog.BackoffMax)
		if t, perr := time.Parse(time.RFC3339, reply.RetryAfter); perr == nil {
			hint = t
		}
		return &retainbacklog.RateLimitedError{RetryAt: hint}
	default:
		h.logf("%s for %s failed host-side: %s", what, id, reply.Error)
		return fmt.Errorf("host %s failed: %s", what, reply.Error)
	}
}

func (h *hostRemote) Status(ctx context.Context, bankID, opID string) (string, error) {
	reply, err := h.ask(map[string]any{"type": "status", "id": opID, "bank_id": bankID}, opID)
	if err != nil {
		return "", err
	}
	if reply.Error != "" {
		return "", h.classify(err, reply, "status", opID)
	}
	return reply.Status, nil
}

func (h *hostRemote) RetryOperation(ctx context.Context, bankID, opID string) error {
	reply, err := h.ask(map[string]any{"type": "retry_op", "id": opID, "bank_id": bankID}, opID)
	if err != nil {
		return err
	}
	if reply.Error != "" {
		return h.classify(err, reply, "retry_op", opID)
	}
	switch reply.Result {
	case "ok":
		return nil
	case "conflict":
		return retainbacklog.ErrRetryConflict
	case "gone":
		return retainbacklog.ErrGoneFromRetry
	case "permanent":
		return retainbacklog.ErrRetryPermanent
	default:
		return fmt.Errorf("protocol: retry_op reply result %q", reply.Result)
	}
}

func (h *hostRemote) Submit(ctx context.Context, entry retainbacklog.Entry) error {
	if err := h.out.Encode(map[string]any{"type": "submit", "entry": entry}); err != nil {
		return err
	}
	var reply hostReply
	if err := h.in.Decode(&reply); err != nil {
		return err
	}
	if reply.Error != "" {
		return h.classify(nil, reply, "submit", entry.ID)
	}
	return nil
}

func cmdDrain(home string, stdin io.Reader, stdout io.Writer) error {
	lock, err := openLocked(home)
	if err != nil {
		return err
	}
	defer lock.Close()
	reader := bufio.NewReaderSize(stdin, 16*1024*1024)
	remote := &hostRemote{
		out:  json.NewEncoder(stdout),
		in:   json.NewDecoder(reader),
		logf: func(format string, args ...any) { fmt.Fprintf(os.Stderr, "retainbacklog: "+format+"\n", args...) },
	}
	next, err := lock.Queue().Drain(time.Now().UTC(), remote)
	// Non-rate drain errors are already applied to the queue (entry kept,
	// backoff persisted); report the outcome and still emit done so the host
	// can arm the wakeup.
	emitDone(stdout, next, lock.Queue().Len())
	if err != nil {
		var rl *retainbacklog.RateLimitedError
		if errors.As(err, &rl) {
			fmt.Fprintf(os.Stderr, "retainbacklog: provider rate limited; retry at %s\n", rl.RetryAt.Format(time.RFC3339))
			return nil // not a command failure: the queue is healthy and scheduled
		}
		return err
	}
	return nil
}

func emitDone(w io.Writer, next time.Time, remaining int) {
	var nextAny any
	if !next.IsZero() {
		nextAny = next.UTC().Format(time.RFC3339Nano)
	}
	out, _ := json.Marshal(map[string]any{"type": "done", "next_retry": nextAny, "remaining": remaining})
	fmt.Fprintln(w, string(out))
}
