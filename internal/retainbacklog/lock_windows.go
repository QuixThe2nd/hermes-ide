//go:build windows

package retainbacklog

import (
	"fmt"
	"os"
	"syscall"
	"unsafe"
)

var (
	kernel32       = syscall.NewLazyDLL("kernel32.dll")
	procLockFileEx = kernel32.NewProc("LockFileEx")
)

const (
	lockfileExclusiveLock   = 0x2
	lockfileFailImmediately = 0x1
)

// fileLock mirrors lock_unix.go: an exclusive lock on the .lock sidecar held
// for a command's whole pass, via LockFileEx on the first byte.
type fileLock struct {
	f *os.File
}

func acquireLock(path string) (*fileLock, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, fmt.Errorf("create backlog lock dir: %w", err)
	}
	f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, fmt.Errorf("open backlog lock %s: %w", path, err)
	}
	// LockFileEx waits (no FAIL_IMMEDIATELY) for the exclusive range lock,
	// matching the blocking flock on POSIX.
	r1, _, err := procLockFileEx.Call(
		f.Fd(),
		lockfileExclusiveLock,
		0,
		1, 0, // lock 1 byte
		0, 0, 0, 0, // OVERLAPPED: zeroed is valid for an unnamed wait
	)
	if r1 == 0 {
		f.Close()
		return nil, fmt.Errorf("LockFileEx %s: %w", path, err)
	}
	return &fileLock{f: f}, nil
}

func (l *fileLock) release() {
	if l == nil || l.f == nil {
		return
	}
	handle := l.f.Fd()
	// UnlockFileEx on the same 1-byte range.
	kernel32.NewProc("UnlockFileEx").Call(handle, 0, 1, 0, 0, 0, 0, 0)
	l.f.Close()
	l.f = nil
}

func fsyncDir(directory string) error {
	// Windows has no directory-handle fsync; NTFS metadata is journaled and
	// the rename is atomic at the filesystem level.
	_ = directory
	return nil
}
