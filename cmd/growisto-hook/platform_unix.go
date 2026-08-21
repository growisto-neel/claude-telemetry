//go:build !windows

// The Linux and macOS half of the three things that genuinely differ by
// platform: testing whether a process is alive, detaching a background child,
// and restricting a file to its owner. Everything else in this program is
// portable.

package main

import (
	"fmt"
	"os"
	"syscall"
)

// processAlive reports whether pid is a live process.
func processAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	err := syscall.Kill(pid, 0)
	switch err {
	case nil:
		return true
	case syscall.ESRCH:
		return false
	case syscall.EPERM:
		return true // exists, owned by another user
	default:
		return true // unknown: assume alive, which is the safe direction
	}
}

// detachedSysProcAttr fully detaches the background flusher, so it survives the
// hook process exiting and is not killed with the terminal.
func detachedSysProcAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{Setsid: true}
}

// noWindowSysProcAttr has no POSIX equivalent; console windows are a Windows
// problem only.
func noWindowSysProcAttr() *syscall.SysProcAttr { return nil }

// secureFile restricts a file to its owner, and returns a description of what
// was actually applied — the guarantee differs by platform, and the caller
// prints this so nobody has to guess which one they got.
func secureFile(path string) string {
	if err := os.Chmod(path, 0o600); err != nil {
		return fmt.Sprintf("chmod failed: %v", err)
	}
	return "chmod 600"
}
