package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// runCommand runs a short-lived helper and returns its stdout.
//
// Every subprocess in this program goes through here so that the Windows
// no-console-window flag is applied in exactly one place. The flusher runs
// detached with no console of its own, so a git or icacls child would otherwise
// allocate and briefly flash one. On a machine where somebody prompts Claude
// fifty times a day that is fifty visible flickers, which is precisely the kind
// of thing that gets telemetry uninstalled.
func runCommand(timeout time.Duration, name string, args ...string) (string, error) {
	cmd := exec.Command(name, args...)
	cmd.SysProcAttr = noWindowSysProcAttr()
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = nil

	if err := cmd.Start(); err != nil {
		return "", err
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case err := <-done:
		return out.String(), err
	case <-time.After(timeout):
		_ = cmd.Process.Kill()
		<-done
		return "", fmt.Errorf("%s timed out after %s", name, timeout)
	}
}

// --------------------------------------------------------------------------
// transport
// --------------------------------------------------------------------------

var httpClient = &http.Client{Timeout: httpTimeout}

// postJSON returns the status code and a bounded slice of the response body.
// A transport failure is reported as status 0, which the callers treat exactly
// like a 5xx: keep the events and try again later.
func postJSON(url string, body interface{}, headers map[string]string) (int, string) {
	raw, err := json.Marshal(body)
	if err != nil {
		return 0, err.Error()
	}
	req, err := http.NewRequest("POST", url, bytes.NewReader(raw))
	if err != nil {
		return 0, err.Error()
	}
	req.Header.Set("Content-Type", "application/json")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return 0, err.Error()
	}
	defer resp.Body.Close()
	buf := make([]byte, 4096)
	n, _ := resp.Body.Read(buf)
	return resp.StatusCode, string(buf[:n])
}

// sendToGA4 posts each event to the Measurement Protocol.
//
// An unconfigured destination reports success, because the caller's contract is
// "did delivery to a configured destination fail?" and there is nothing to fail
// against. The events are held back by hasDestination() upstream instead, which
// is what keeps them spooled rather than discarded.
func sendToGA4(events []*Event, cfg Config, debug bool) bool {
	if cfg.GA4MeasurementID == "" || cfg.GA4APISecret == "" {
		return true
	}
	endpoint := ga4Endpoint
	if debug {
		endpoint = ga4DebugEndpoint
	}
	url := fmt.Sprintf("%s?measurement_id=%s&api_secret=%s",
		endpoint, cfg.GA4MeasurementID, cfg.GA4APISecret)

	allOK := true
	for _, e := range events {
		status, body := postJSON(url, toGA4Payload(e), nil)
		if debug {
			// --test only. The hook path prints nothing, ever.
			fmt.Printf("GA4 %s -> %d %s\n", e.EventName, status, body)
		}
		if status < 200 || status >= 300 {
			allOK = false
			logf("ga4 failed status=%d body=%s", status, clipRunes(body, 200))
		}
	}
	return allOK
}

// --------------------------------------------------------------------------
// spool
// --------------------------------------------------------------------------

// appendLines appends events to the ndjson spool in a single write.
//
// One write of a complete buffer is the closest thing to an atomic append
// available without taking a lock, which matters when two Claude Code sessions
// emit at the same moment. The residual risk is that two interleaved writes
// corrupt one line; the reader skips lines it cannot parse, so the failure mode
// is losing a single event rather than losing the file.
func appendLines(path string, events []*Event) error {
	var buf bytes.Buffer
	for _, e := range events {
		raw, err := json.Marshal(e)
		if err != nil {
			continue
		}
		buf.Write(raw)
		buf.WriteByte('\n')
	}
	if buf.Len() == 0 {
		return nil
	}
	var fh *os.File
	if err := retryShared(func() error {
		var err error
		fh, err = os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
		return err
	}); err != nil {
		return err
	}
	defer fh.Close()
	_, err := fh.Write(buf.Bytes())
	return err
}

func spoolAppend(event *Event) error {
	if err := os.MkdirAll(baseDir, 0o700); err != nil {
		return err
	}
	if st, err := os.Stat(spoolPath); err == nil && st.Size() > maxSpoolBytes {
		if retryShared(func() error {
			return os.Rename(spoolPath, spoolPath+".dropped")
		}) == nil {
			logf("spool exceeded %d bytes; rotated to spool.ndjson.dropped "+
				"(previous .dropped overwritten)", maxSpoolBytes)
		}
	}
	return appendLines(spoolPath, []*Event{event})
}

// spawnFlush detaches a background sender so the hook returns immediately.
func spawnFlush() {
	self, err := os.Executable()
	if err != nil {
		logf("spawnFlush could not find own path: %v", err)
		return
	}
	devnull, err := os.OpenFile(os.DevNull, os.O_RDWR, 0)
	if err != nil {
		logf("spawnFlush could not open %s: %v", os.DevNull, err)
		return
	}
	// The child receives its own duplicates of these handles, so the parent's
	// copy is closed immediately. Leaving it open would leak a descriptor per
	// prompt.
	defer devnull.Close()

	cmd := exec.Command(self, "--flush")
	cmd.Stdin, cmd.Stdout, cmd.Stderr = devnull, devnull, devnull
	cmd.SysProcAttr = detachedSysProcAttr()
	if err := cmd.Start(); err != nil {
		logf("spawnFlush failed: %v", err)
		return
	}
	// Not waited on, by design: this process is about to exit and the child is
	// detached. Release returns the OS resources this side was holding.
	_ = cmd.Process.Release()
}

// --------------------------------------------------------------------------
// lock
// --------------------------------------------------------------------------

func lockHolderAlive() bool {
	raw, err := os.ReadFile(lockPath)
	if err != nil {
		return false // unreadable or corrupt lock is treated as abandoned
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(raw)))
	if err != nil || pid <= 0 || pid == os.Getpid() {
		return false
	}
	return processAlive(pid)
}

func acquireLock() bool {
	// A lock is only stale if it is BOTH old and its owner is gone. Age alone is
	// not enough: a flush against a hung network can legitimately run for a long
	// time, and stealing its lock would let a second flusher clobber the
	// in-flight .sending file.
	if before, err := os.Stat(lockPath); err == nil {
		age := time.Since(before.ModTime())
		if age > lockStaleDuration && !lockHolderAlive() {
			// Re-stat and compare identity immediately before unlinking, so a
			// *fresh* lock created by another flusher in the intervening gap is
			// not deleted.
			if after, err := os.Stat(lockPath); err == nil && os.SameFile(before, after) {
				if retryShared(func() error { return os.Remove(lockPath) }) == nil {
					logf("removed stale lock (age %s, owner gone)", age.Round(time.Second))
				}
			}
		}
	}

	fh, err := os.OpenFile(lockPath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return false
	}
	pid := os.Getpid()
	_, _ = fh.WriteString(strconv.Itoa(pid))
	fh.Close()

	// Read back before proceeding: if two flushers both raced through a stale
	// steal, only the one whose pid is actually in the file continues.
	raw, err := os.ReadFile(lockPath)
	if err != nil {
		return false
	}
	got, err := strconv.Atoi(strings.TrimSpace(string(raw)))
	return err == nil && got == pid
}

// touchLock keeps the lock fresh so a long but healthy flush is never judged
// stale by a concurrent flusher.
func touchLock() {
	now := time.Now()
	_ = os.Chtimes(lockPath, now, now)
}

func releaseLock() {
	_ = retryShared(func() error { return os.Remove(lockPath) })
}

// --------------------------------------------------------------------------
// enrichment
// --------------------------------------------------------------------------

// gitRepoName resolves the repo name for a directory. Called only from the
// background flusher, never from the hook path, and memoised on disk so a given
// directory costs one `git` invocation ever.
func gitRepoName(cwd string) string {
	if cwd == "" {
		return ""
	}
	cache := map[string]string{}
	if raw, err := os.ReadFile(repoPath); err == nil {
		if json.Unmarshal(raw, &cache) == nil {
			if repo, ok := cache[cwd]; ok {
				return repo // "" is a cached negative, and is honoured as one
			}
		} else {
			cache = map[string]string{}
		}
	}

	repo := ""
	if out, err := runCommand(2*time.Second, "git", "-C", cwd, "rev-parse", "--show-toplevel"); err == nil {
		if top := strings.TrimSpace(out); top != "" {
			repo = filepath.Base(top)
		}
	}

	if len(cache) > 500 {
		cache = map[string]string{}
	}
	cache[cwd] = repo
	if raw, err := json.Marshal(cache); err == nil {
		if os.MkdirAll(baseDir, 0o700) == nil {
			_ = os.WriteFile(repoPath, raw, 0o600)
		}
	}
	return repo
}

// enrichInPlace does the background-only work. Anything that costs a subprocess
// or real time belongs here rather than on the synchronous hook path.
func enrichInPlace(e *Event, cfg Config) *Event {
	// Only an absolute folder_path can be handed to git, which confines repo
	// detection to `full` path mode. In basename/none mode folder_path is a bare
	// name or absent, so repo stays empty rather than the hook keeping a shadow
	// copy of the path the operator asked us not to keep.
	if e.Repo == "" && e.FolderPath != "" && filepath.IsAbs(e.FolderPath) {
		if repo := gitRepoName(e.FolderPath); repo != "" {
			e.Repo = repo
		}
	}

	// The hook path cannot run `git`, so identity may have fallen back to
	// user@hostname. Upgrade it here, where a subprocess is free.
	if e.UserIDSource == "os" || e.UserIDSource == "none" {
		email, source := resolveUserEmail(cfg, true)
		if email != "" && (source == "config" || source == "git" || source == "cache") {
			e.UserEmail = email
			e.UserEmailSHA256 = sha256Hex(email)
			e.UserIDSource = source
		}
	}
	return e
}

// --------------------------------------------------------------------------
// flush
// --------------------------------------------------------------------------

// recoverOrphans re-queues any `.sending` file left behind by a flusher that
// died mid-send (laptop sleep, SIGKILL, crash). Without this those events are
// stranded, and because the working filename is unique per flush they would
// never be clobbered either — they would simply sit there forever.
func recoverOrphans() {
	paths, err := filepath.Glob(spoolPath + ".sending.*")
	if err != nil {
		return
	}
	recovered := 0
	for _, path := range paths {
		raw, err := os.ReadFile(path)
		if err != nil {
			logf("orphan recovery failed for %s: %v", path, err)
			continue
		}
		var buf bytes.Buffer
		lines := 0
		for _, line := range strings.Split(string(raw), "\n") {
			if line = strings.TrimSpace(line); line != "" {
				buf.WriteString(line)
				buf.WriteByte('\n')
				lines++
			}
		}
		if lines > 0 {
			var fh *os.File
			if err := retryShared(func() error {
				var err error
				fh, err = os.OpenFile(spoolPath, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
				return err
			}); err != nil {
				logf("orphan recovery failed for %s: %v", path, err)
				continue
			}
			_, err = fh.Write(buf.Bytes())
			fh.Close()
			if err != nil {
				logf("orphan recovery failed for %s: %v", path, err)
				continue
			}
			recovered += lines
		}
		_ = retryShared(func() error { return os.Remove(path) })
	}
	if recovered > 0 {
		logf("recovered %d orphaned event(s)", recovered)
	}
}

// sendBatch delivers a batch to GA4 and records success on each event, so an
// event that already arrived is never sent twice.
//
// The per-event flag is kept — rather than treating the batch as a unit —
// because delivery is per-request, and a batch-level flag would resend every
// event in the batch whenever one of them was rejected.
func sendBatch(batch []*Event, cfg Config) []*Event {
	// GA4 is one HTTP request per event: the Measurement Protocol carries
	// timestamp_micros at request level, so events cannot share a request
	// without losing their real timestamps. Marked per event so one rejection
	// does not force a resend of its neighbours.
	for _, e := range batch {
		if e.GA4Done {
			continue
		}
		if sendToGA4([]*Event{e}, cfg, false) {
			e.GA4Done = true
		}
	}

	var unsent []*Event
	for _, e := range batch {
		if !e.delivered() {
			unsent = append(unsent, e)
		}
	}
	return unsent
}

// requeue puts undelivered events back on the spool. False means nothing was
// written, which is the signal to keep the working file rather than delete it.
func requeue(events []*Event) bool {
	if len(events) == 0 {
		return true
	}
	if err := appendLines(spoolPath, events); err != nil {
		logf("requeue failed: %v", err)
		return false
	}
	logf("requeued %d event(s)", len(events))
	return true
}

func discard(path string) {
	_ = retryShared(func() error { return os.Remove(path) })
}

// flushOnce sends one spool generation. It returns true if there may be more to
// send, which is what drives the second and third passes in flush.
func flushOnce(cfg Config, deadline time.Time) bool {
	if st, err := os.Stat(spoolPath); err != nil || st.Size() == 0 {
		return false
	}

	// Unique working name: a crashed flush leaves a recoverable file rather than
	// one the next flush silently overwrites.
	working := fmt.Sprintf("%s.sending.%d.%d", spoolPath, os.Getpid(), time.Now().Unix())
	// On Windows this fails outright if a hook is mid-append, so it is retried
	// rather than treated as "nothing to send".
	if err := retryShared(func() error { return os.Rename(spoolPath, working) }); err != nil {
		return false
	}

	// From here on `working` holds the only copy of these events. It is deleted
	// only after every undelivered event is safely back on the spool. On any
	// other outcome it is left in place for recoverOrphans to pick up, so a
	// failure can duplicate at worst — it can never lose data.
	fh, err := os.Open(working)
	if err != nil {
		logf("spool open error: %v; left %s for recovery", err, working)
		return false
	}
	var events []*Event
	nowMs := time.Now().UnixMilli()
	scanner := bufio.NewScanner(fh)
	// Default token size is 64 KB. A prompt preview is capped at 100 chars, but
	// a corrupted interleaved line could be longer, and a scanner that stops at
	// the first oversized line would strand every event after it.
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var e Event
		if json.Unmarshal([]byte(line), &e) != nil {
			continue // corrupt line: lose one event, not the file
		}
		if e.TsMs > 0 && nowMs-e.TsMs > maxEventAge.Milliseconds() {
			continue // too old for GA4 to accept
		}
		events = append(events, enrichInPlace(&e, cfg))
	}
	scanErr := scanner.Err()
	fh.Close()
	if scanErr != nil {
		logf("spool read error: %v; left %s for recovery", scanErr, working)
		return false
	}

	var unsent []*Event
	for i := 0; i < len(events); i += maxBatch {
		if time.Now().After(deadline) {
			// Out of budget: everything not yet attempted goes back on the spool
			// for the next run rather than being dropped.
			unsent = append(unsent, events[i:]...)
			logf("flush budget exhausted; requeuing %d event(s)", len(events[i:]))
			break
		}
		end := i + maxBatch
		if end > len(events) {
			end = len(events)
		}
		unsent = append(unsent, sendBatch(events[i:end], cfg)...)
		touchLock()
	}

	if !requeue(unsent) {
		return false // spool unwritable: keep `working` so nothing is lost
	}
	discard(working)
	return len(unsent) == 0
}

func flush() {
	cfg := loadConfig()

	// With no destination configured, events stay spooled rather than being
	// thrown away, so a phased rollout can backfill once the endpoint is live.
	// The size and age caps still stop the spool growing without bound.
	if !cfg.hasDestination() {
		logf("no destination configured; leaving events spooled")
		return
	}

	// A destination exists, so any earlier complaint about not having one is
	// stale. Cleared here rather than on the hook path because this runs once per
	// flush instead of once per prompt, and because reaching this line is the
	// best evidence available that configuration really did arrive.
	clearNoDestMarker()

	if !acquireLock() {
		return
	}
	defer releaseLock()

	// A panic mid-send must still release the lock, hence the defer above and
	// the recover here rather than relying on main's.
	defer func() {
		if r := recover(); r != nil {
			logf("flush panicked: %v", r)
		}
	}()

	recoverOrphans()
	// Housekeeping for the duplicate-suppression directory. Here because it walks
	// a directory, and the hook path must never do anything that scales with how
	// long telemetry has been installed.
	pruneSeen()
	deadline := time.Now().Add(maxFlushDuration)
	// Up to three passes: the second and third pick up anything appended while
	// we were sending, so the tail of a session is not left waiting for the next
	// session's event to trigger a flush.
	for i := 0; i < 3; i++ {
		if time.Now().After(deadline) || !flushOnce(cfg, deadline) {
			break
		}
	}
}
