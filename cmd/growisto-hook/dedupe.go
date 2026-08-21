package main

import (
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

// --------------------------------------------------------------------------
// duplicate suppression
// --------------------------------------------------------------------------
//
// One hook occurrence should produce one event. Two things can break that, and
// neither is hypothetical:
//
//   - Hooks registered twice. Before this was a plugin, install.sh wrote hook
//     entries directly into ~/.claude/settings.json, and those survive a plugin
//     uninstall because the plugin never put them there. The same happens if the
//     plugin is installed at both user and project level.
//   - More than one hooks.json entry for the same event. Nothing here does that
//     today, but it is the shape any future per-platform registration would
//     take, and it would be indistinguishable in the data from genuine use.
//
// Double-counted events are worse than missing ones, because they look
// plausible. An adoption dashboard would simply read high, with nothing
// anywhere to suggest it was wrong.
//
// The check is one file per event fingerprint, created with O_EXCL. That is the
// only atomic primitive available to two processes that may be running in the
// same instant on both POSIX and Windows; anything built on read-then-write of
// a shared file loses precisely the race it exists to prevent.

const (
	// Long enough to cover two hook entries firing for one occurrence, short
	// enough that a person genuinely resending the same prompt is not eaten.
	dedupeWindow = 5 * time.Second

	// How long a fingerprint is kept before pruning, and the cap on how many.
	// Both only bound the directory; neither affects correctness.
	dedupeKeep     = 30 * time.Minute
	dedupeMaxFiles = 500
)

func seenDir() string { return filepath.Join(baseDir, "seen") }

// fingerprint identifies an occurrence rather than an event.
//
// The timestamp is deliberately excluded: two entries firing for the same
// occurrence build their events milliseconds apart, so including ts_ms would
// make every duplicate unique and the whole check a no-op. Everything included
// here is either supplied by Claude Code in the payload or derived from it, so
// two processes handed the same payload agree.
func fingerprint(e *Event) string {
	parts := []string{
		e.HookEventName,
		e.SessionID,
		e.ToolName,
		e.Skill,
		e.PromptSHA256,
		strconv.Itoa(e.PromptChars),
		e.SessionEndReason,
		e.AgentID,
	}
	return sha256Hex(strings.Join(parts, "\x00"))[:32]
}

// alreadySeen reports whether an identical occurrence was recorded within the
// last dedupeWindow, and claims the fingerprint if not.
//
// It fails open. If the directory cannot be created or the file cannot be
// stat'd, the event is recorded: suppression is insurance against a
// misconfiguration most machines do not have, while dropping events on a
// filesystem error would break the thing the plugin exists to do.
func alreadySeen(e *Event) bool {
	if e == nil {
		return false
	}
	dir := seenDir()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return false
	}
	path := filepath.Join(dir, fingerprint(e))

	fh, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err == nil {
		fh.Close()
		return false
	}

	st, err := os.Stat(path)
	if err != nil {
		return false
	}
	age := time.Since(st.ModTime())
	if age < 0 {
		// The clock moved backwards under us -- a laptop waking in another
		// timezone, or an NTP correction. Only the magnitude is meaningful, and
		// treating a future timestamp as "just now" would drop real events for
		// as long as the skew lasted.
		age = -age
	}
	if age < dedupeWindow {
		return true
	}

	// Same shape, long enough ago to be a real second occurrence -- somebody
	// sending an identical prompt again. Re-stamp, so the next duplicate is
	// measured from now rather than from the first time this was ever seen.
	now := time.Now()
	_ = os.Chtimes(path, now, now)
	return false
}

// pruneSeen bounds the fingerprint directory. Called from the background
// flusher, never from the hook path: the hook does one file create and nothing
// that walks a directory.
func pruneSeen() {
	dir := seenDir()
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}

	type stamped struct {
		name string
		when time.Time
	}
	var files []stamped
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		info, err := entry.Info()
		if err != nil {
			continue
		}
		files = append(files, stamped{entry.Name(), info.ModTime()})
	}

	// Oldest first, so the cap deletes the fingerprints least likely to still
	// be inside anybody's dedupe window. Deleting an arbitrary subset would
	// occasionally delete one created moments ago and reopen the gap.
	sort.Slice(files, func(i, j int) bool { return files[i].when.Before(files[j].when) })

	excess := len(files) - dedupeMaxFiles
	for i, f := range files {
		if time.Since(f.when) > dedupeKeep || i < excess {
			_ = os.Remove(filepath.Join(dir, f.name))
		}
	}
}
