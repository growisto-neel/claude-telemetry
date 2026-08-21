//go:build windows

// The Windows half of the three things that genuinely differ by platform.

package main

import (
	"fmt"
	"os"
	"strings"
	"syscall"
	"time"
)

const (
	processQueryLimitedInformation = 0x1000
	stillActive                    = 259

	detachedProcess       = 0x00000008
	createNewProcessGroup = 0x00000200
	createNoWindow        = 0x08000000
)

// processAlive reports whether pid is a live process.
//
// Deliberately not the POSIX idiom. On Windows, sending a signal to test for
// liveness calls TerminateProcess, so the equivalent of kill(pid, 0) would
// actually kill the process — and because Windows recycles pids aggressively, a
// stale lock file can easily name an unrelated process belonging to the user.
// Telemetry must never do that.
func processAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	handle, err := syscall.OpenProcess(processQueryLimitedInformation, false, uint32(pid))
	if err != nil {
		// ERROR_ACCESS_DENIED means the process exists but is not ours.
		return err == syscall.ERROR_ACCESS_DENIED
	}
	defer syscall.CloseHandle(handle)

	var code uint32
	if err := syscall.GetExitCodeProcess(handle, &code); err != nil {
		return true // cannot tell: assume alive, so we never steal a live lock
	}
	return code == stillActive
}

// detachedSysProcAttr fully detaches the background flusher.
//
// DETACHED_PROCESS is what stops the sender being tied to the Claude Code
// console, and CREATE_NO_WINDOW is what stops it flashing a console window on
// every prompt.
func detachedSysProcAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{
		CreationFlags: detachedProcess | createNewProcessGroup | createNoWindow,
		HideWindow:    true,
	}
}

// noWindowSysProcAttr stops a short-lived child (git, icacls) allocating and
// briefly showing a console window.
func noWindowSysProcAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{CreationFlags: createNoWindow, HideWindow: true}
}

// secureFile restricts a file to its owner.
//
// chmod is meaningless here: the mode bits only toggle the read-only attribute
// and do nothing about who can read the file. config.json holds the GA4 API
// secret in direct mode, so drop inherited ACEs and grant the current user
// alone, which is the real equivalent of chmod 600.
func secureFile(path string) string {
	user := os.Getenv("USERNAME")
	domain := os.Getenv("USERDOMAIN")
	principal := user
	if domain != "" && user != "" {
		principal = domain + `\` + user
	}
	if principal == "" {
		return "no ACL applied: could not determine current user"
	}
	out, err := runCommand(15*time.Second, "icacls", path,
		"/inheritance:r", "/grant:r", principal+":F")
	if err != nil {
		return fmt.Sprintf("icacls failed: %v %s", err, clipRunes(strings.TrimSpace(out), 200))
	}
	return "ACL restricted to " + principal
}
