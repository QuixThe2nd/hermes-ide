package retainbacklog

import (
	"fmt"
	"path/filepath"

	"os"
	"syscall"
)

// fileLock is an exclusive advisory lock on the backlog's .lock sidecar,
// held for a command's whole pass. Independent processes (CLI + gateway on
// one home) serialize on it; an in-process mutex alone cannot.
type fileLock struct {
	f *os.File
}

// acquireLock opens (creating) the lock sidecar and blocks until the
// exclusive lock is held. Failing to take the lock must fail the command —
// proceeding unlocked would let two processes interleave read-modify-write
// passes on the same file and silently drop entries.
func acquireLock(path string) (*fileLock, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, fmt.Errorf("create backlog lock dir: %w", err)
	}
	f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, fmt.Errorf("open backlog lock %s: %w", path, err)
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
		f.Close()
		return nil, fmt.Errorf("flock %s: %w", path, err)
	}
	return &fileLock{f: f}, nil
}

func (l *fileLock) release() {
	if l == nil || l.f == nil {
		return
	}
	syscall.Flock(int(l.f.Fd()), syscall.LOCK_UN)
	l.f.Close()
	l.f = nil
}

// fsyncDir persists a rename/unlink within *directory* so a crash right
// after it cannot lose or resurrect the file name.
func fsyncDir(directory string) error {
	fd, err := os.Open(directory)
	if err != nil {
		return nil // e.g. the dir vanished with the last entry; nothing to sync
	}
	defer fd.Close()
	if err := fd.Sync(); err != nil {
		return nil // some filesystems refuse directory fsync; best effort
	}
	return nil
}
