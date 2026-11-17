package retainbacklog

import (
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestWakeupFiresCallbackWhenDeadlinePasses(t *testing.T) {
	var fired atomic.Int32
	w := NewWakeup(func() { fired.Add(1) })
	done := make(chan struct{})
	go func() { w.Run(); close(done) }()

	delay := w.Arm(time.Now().Add(10 * time.Millisecond))
	if delay < MinWakeupDelay {
		t.Fatalf("Arm returned %v, want at least the %v floor", delay, MinWakeupDelay)
	}
	select {
	case <-time.After(5 * time.Second):
		t.Fatal("wakeup never fired")
	case <-done:
	}
	if fired.Load() != 1 {
		t.Fatalf("callback fired %d times, want exactly 1", fired.Load())
	}
	if !w.Deadline().IsZero() {
		t.Fatal("slot must clear after firing")
	}
}

func TestWakeupFloorsPastDueDeadlines(t *testing.T) {
	w := NewWakeup(func() {})
	// A past-due earliest retry (entry kept after a non-rate-limit settle
	// failure) must be floored, never armed as an immediate hot loop.
	delay := w.Arm(time.Now().Add(-time.Hour))
	if delay != MinWakeupDelay {
		t.Fatalf("past-due Arm delay = %v, want the floor %v", delay, MinWakeupDelay)
	}
}

func TestWakeupRearmsForEarlierDeadlineOnly(t *testing.T) {
	w := NewWakeup(func() {})
	later := time.Now().Add(2 * time.Hour)
	w.Arm(later)
	laterDeadline := w.Deadline()

	// A later arm must NOT move the slot.
	w.Arm(later.Add(time.Hour))
	if !w.Deadline().Equal(laterDeadline) {
		t.Fatalf("later arm moved the deadline: %v -> %v", laterDeadline, w.Deadline())
	}

	// An earlier arm MUST move the slot.
	w.Arm(time.Now().Add(3 * time.Minute))
	if !w.Deadline().Before(laterDeadline) {
		t.Fatalf("earlier arm did not move the deadline: %v", w.Deadline())
	}
	if got := time.Until(w.Deadline()); got > 4*time.Minute {
		t.Fatalf("earlier deadline is not actually earlier: %v away", got)
	}
}

func TestWakeupStopReleasesWaiterAndBlocksArms(t *testing.T) {
	var fired atomic.Int32
	w := NewWakeup(func() { fired.Add(1) })
	done := make(chan struct{})
	go func() { w.Run(); close(done) }()
	w.Arm(time.Now().Add(time.Hour))

	w.Stop()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Stop did not release the waiter")
	}
	if delay := w.Arm(time.Now().Add(time.Minute)); delay != 0 {
		t.Fatalf("Arm after Stop returned %v, want 0 (no-op)", delay)
	}
	if fired.Load() != 0 {
		t.Fatalf("callback fired %d times after Stop, want 0", fired.Load())
	}
}

// TestWakeupDrainsBacklogWithoutChat is the end-to-end contract the timer
// exists for: a future-due entry, a wakeup armed with the retry Drain
// returned, and the backlog empties with no external trigger.
func TestWakeupDrainsBacklogWithoutChat(t *testing.T) {
	path := filepath.Join(t.TempDir(), "retain-backlog.jsonl")
	q := openQueue(t, path)
	ops := newFakeOps()
	r := &Reconciler{Ops: ops}

	due := time.Now().Add(5 * time.Millisecond)
	e := Entry{ID: newOperationID(), BankID: "test-bank",
		Item: item("wake me"), QueuedAt: time.Now().UTC(), RetryAfter: due}
	if _, err := q.Add(e); err != nil {
		t.Fatalf("Add: %v", err)
	}

	var w *Wakeup
	drainOnce := func() {
		next, err := q.Drain(time.Now().UTC(), r.Settle)
		if err != nil {
			t.Fatalf("Drain: %v", err)
		}
		if !next.IsZero() {
			w.Arm(next) // the drain's returned retry arms the wakeup
		}
	}
	fireGate := make(chan struct{})
	var once sync.Once
	w = NewWakeup(func() {
		once.Do(func() { close(fireGate) })
		drainOnce()
	})
	go w.Run()

	// Initial arm from the enqueue-side retry hint.
	w.Arm(due)
	select {
	case <-fireGate:
	case <-time.After(5 * time.Second):
		t.Fatal("wakeup never fired for the future-due entry")
	}
	deadline := time.After(5 * time.Second)
	for len(q.Entries()) != 0 {
		select {
		case <-deadline:
			t.Fatalf("backlog never emptied without external trigger; entries=%d", len(q.Entries()))
		case <-time.After(2 * time.Millisecond):
		}
	}
	if got := ops.submitCount(e.ID); got != 1 {
		t.Fatalf("submissions = %d, want 1", got)
	}
}
