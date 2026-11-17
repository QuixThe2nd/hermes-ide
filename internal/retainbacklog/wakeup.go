package retainbacklog

import (
	"sync"
	"time"
)

// MinWakeupDelay floors every armed wakeup: an earliest-retry in the past
// (an entry kept after a non-rate-limit settle failure) must not arm an
// immediately-due timer and hot-loop the drain. The writer's interval pass
// backstops that case.
const MinWakeupDelay = 1 * time.Second

// Wakeup is the exact-wakeup contract mirroring the Python provider's
// _arm_backlog_wakeup/_backlog_wakeup_loop pair: ONE re-armable slot armed
// with the earliest future-due backlog retry (the value Drain returns), so
// entries drain without incoming chat or the interval backstop.
//
// Guarantees (mirrored exactly):
//
//   - arming an EARLIER deadline re-arms the waiting waiter;
//   - arming a LATER deadline is ignored (the waiter re-reads the shared
//     deadline after every wake);
//   - delays are floored at MinWakeupDelay;
//   - firing queues exactly one drain callback, then clears the slot;
//   - Stop releases the waiter immediately; arming after Stop is a no-op
//     (the Python side guards on its shutdown event).
type Wakeup struct {
	mu       sync.Mutex
	deadline time.Time // monotonic comparison base captured at arm time
	armed    bool
	stopped  bool
	wake     chan struct{}
	onFire   func()
	nowFunc  func() time.Time // injectable clock (tests)
	sleep    func(time.Duration)
}

// NewWakeup builds a wakeup around *onFire* (queued exactly once per armed
// slot when its deadline passes).
func NewWakeup(onFire func()) *Wakeup {
	return &Wakeup{
		wake:    make(chan struct{}, 1),
		onFire:  onFire,
		nowFunc: time.Now,
		sleep:   time.Sleep,
	}
}

// Arm (re)arms the single slot for dueAt, clamped by the MinWakeupDelay
// floor. Earlier wins; later is ignored. Returns the effective delay.
func (w *Wakeup) Arm(dueAt time.Time) time.Duration {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.stopped {
		return 0
	}
	delay := dueAt.Sub(w.nowFunc())
	if delay < MinWakeupDelay {
		delay = MinWakeupDelay
	}
	effective := w.nowFunc().Add(delay)
	if w.armed && !effective.Before(w.deadline) {
		return w.deadline.Sub(w.nowFunc()) // existing slot fires sooner
	}
	w.deadline = effective
	w.armed = true
	select {
	case w.wake <- struct{}{}:
	default: // a wake signal is already pending; the loop re-reads the slot
	}
	return delay
}

// Deadline reports the armed deadline (zero when the slot is clear).
func (w *Wakeup) Deadline() time.Time {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.deadline
}

// Stop releases the waiter permanently; subsequent Arm calls are no-ops.
func (w *Wakeup) Stop() {
	w.mu.Lock()
	w.stopped = true
	w.mu.Unlock()
	select {
	case w.wake <- struct{}{}:
	default:
	}
}

// Run is the waiter loop: blocks until the armed deadline passes (or a
// re-arm wake), fires the callback exactly once per slot, and returns when
// the slot is clear and no re-arm arrived, or when stopped. Callers run it
// on its own goroutine, the way the Python provider spawns its wakeup
// thread.
func (w *Wakeup) Run() {
	for {
		w.mu.Lock()
		stopped, armed, deadline := w.stopped, w.armed, w.deadline
		w.mu.Unlock()
		if stopped {
			return
		}
		if !armed {
			// Slot clear: park until a re-arm or Stop.
			<-w.wake
			continue
		}
		if remaining := time.Until(deadline); remaining > 0 {
			select {
			case <-w.wake: // re-arm (earlier) or Stop: re-read the slot
				continue
			case <-time.After(remaining):
			}
		}
		w.mu.Lock()
		w.armed = false
		w.deadline = time.Time{}
		onFire := w.onFire
		w.mu.Unlock()
		if onFire != nil {
			onFire()
		}
		return // single-slot: the next Arm spawns the next waiter
	}
}
