package retainbacklog

import "fmt"

// LockedQueue is an opened backlog holding its cross-process lock. Every
// command (enqueue, drain, peek) runs under exactly one of these; the lock
// spans the whole read-modify-write pass so independent processes on the
// same home serialize. Failing to take the lock fails the command —
// proceeding unlocked would let two processes interleave passes and
// silently drop entries.
type LockedQueue struct {
	q    *Queue
	lock *fileLock
}

// OpenLocked acquires the exclusive lock on <home>/hindsight/retain-backlog.jsonl.lock
// (blocking) and loads the queue under it.
func OpenLocked(home string) (*LockedQueue, error) {
	path := BacklogPath(home)
	lock, err := acquireLock(path + ".lock")
	if err != nil {
		return nil, err
	}
	q, err := Open(path)
	if err != nil {
		lock.release()
		return nil, fmt.Errorf("open backlog %s: %w", path, err)
	}
	return &LockedQueue{q: q, lock: lock}, nil
}

// Queue is the locked queue. Mutations go through it.
func (l *LockedQueue) Queue() *Queue { return l.q }

// Close releases the lock.
func (l *LockedQueue) Close() { l.lock.release() }
