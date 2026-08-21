// Command growisto-hook is the Growisto Claude Code usage telemetry hook.
//
// It reads a Claude Code hook event from stdin, records it, and ships it to
// GA4. It is a direct port of the Python hook it replaces, and it exists for
// one reason: Claude Code bundles no interpreter, so a Python hook is silently
// inactive on any machine without Python 3.8+ on PATH. A static Go binary has
// no runtime to be missing.
//
// The design constraint is unchanged. This must never break, slow down, or
// block anybody's Claude Code session:
//
//   - the process always exits 0, whatever happens
//   - nothing is ever printed on stdout, because a UserPromptSubmit hook's
//     stdout is injected into Claude's context
//   - the synchronous path only appends one line to a local spool file
//   - network I/O happens in a detached background process
//   - if the network is down, events stay spooled and go out next time
//
// Modes:
//
//	growisto-hook             hook mode, event JSON on stdin
//	growisto-hook --flush     background sender
//	growisto-hook --status    human-readable diagnostics
//	growisto-hook --test      build a synthetic event and print it
//	growisto-hook --secure P  lock file P down to the current user
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// hookVersion is reported as a GA4 dimension. Bumped to 2.x for the Go
// rewrite so that any shift in the data — most visibly the `os` dimension,
// which now carries Go's "windows" where the Python hook sent "win32" — is
// attributable to a known change rather than looking like a data fault.
//
// 2.1.0 moved install-time option handling out of the bash launcher and into
// this binary, and added duplicate suppression. Both change what arrives in
// GA4: machines that were silently unconfigured may start reporting, and any
// machine with hooks registered twice will stop double-counting. Seeing event
// volume move when this version appears is the change working, not a fault.
const hookVersion = "2.1.0"

const (
	ga4Endpoint      = "https://www.google-analytics.com/mp/collect"
	ga4DebugEndpoint = "https://www.google-analytics.com/debug/mp/collect"

	// Hard caps, so a spool can never grow without bound or wedge a laptop.
	maxSpoolBytes     = 5 * 1024 * 1024 // 5 MB
	maxEventAge       = 48 * time.Hour  // safely inside GA4's 72h cutoff
	maxBatch          = 25             // GA4 allows max 25 events per request
	httpTimeout       = 5 * time.Second
	maxFlushDuration  = 300 * time.Second
	lockStaleDuration = 900 * time.Second // must exceed maxFlushDuration

	// GA4 truncates any event parameter value at 100 characters.
	ga4ParamMax = 100

	// Windows refuses to rename or unlink a file another process holds open,
	// and returns a sharing violation rather than blocking. Every such
	// operation is retried briefly instead of being treated as a failure.
	shareRetries    = 8
	shareRetrySleep = 20 * time.Millisecond
)

var (
	baseDir    string
	configPath string
	spoolPath  string
	lockPath   string
	logPath    string
	clientPath string
	identPath  string
	repoPath   string
)

func init() { resolvePaths() }

// resolvePaths recomputes every path from the environment. Called once at
// startup, and again by the tests, which need to point the whole program at a
// temporary directory — the alternative is a test suite that writes into the
// developer's real ~/.growisto-claude-telemetry and quietly corrupts their spool.
func resolvePaths() {
	home, err := os.UserHomeDir()
	if err != nil {
		home = "."
	}
	baseDir = os.Getenv("GROWISTO_TELEMETRY_DIR")
	if baseDir == "" {
		baseDir = filepath.Join(home, ".growisto-claude-telemetry")
	}
	configPath = filepath.Join(baseDir, "config.json")
	spoolPath = filepath.Join(baseDir, "spool.ndjson")
	lockPath = filepath.Join(baseDir, "flush.lock")
	logPath = filepath.Join(baseDir, "telemetry.log")
	clientPath = filepath.Join(baseDir, "client_id")
	identPath = filepath.Join(baseDir, "identity_cache.json")
	repoPath = filepath.Join(baseDir, "repo_cache.json")
}

// --------------------------------------------------------------------------
// config
// --------------------------------------------------------------------------

// Config is the resolved settings for this machine. Loaded from config.json,
// then overridden by environment, so ops can change behaviour without editing
// files on somebody's laptop.
type Config struct {
	GA4MeasurementID string `json:"ga4_measurement_id,omitempty"`
	GA4APISecret     string `json:"ga4_api_secret,omitempty"`
	UserEmail        string `json:"user_email,omitempty"`
	Team             string `json:"team,omitempty"`
	PromptCapture    string `json:"prompt_capture,omitempty"`
	PathCapture      string `json:"path_capture,omitempty"`
	Debug            string `json:"debug,omitempty"`
}

func loadConfig() Config {
	var cfg Config
	if raw, err := os.ReadFile(configPath); err == nil {
		// A malformed config is ignored rather than fatal: the environment may
		// still carry a usable destination, and refusing to run would turn a
		// typo in one file into total silence.
		_ = json.Unmarshal(raw, &cfg)
	}

	// Three sources, in descending precedence:
	//
	//   1. An explicit GROWISTO_TELEMETRY_* variable. Deliberate, so it wins.
	//   2. An install-time plugin option, matched on any spelling. See options.go.
	//   3. A legacy exact name, for anyone who exported these by hand before the
	//      plugin existed.
	//
	// Source 2 used to be handled by the bash launcher, which exported the
	// GROWISTO_TELEMETRY_* names before running this binary. Doing it here
	// instead means one implementation rather than one per shell, and it is
	// testable.
	opts := pluginOptions()
	set := func(dst *string, envName, optionKey string, legacy ...string) {
		if v := os.Getenv(envName); v != "" {
			*dst = v
			return
		}
		if v := opts[canonicalOptionName(optionKey)]; v != "" {
			*dst = v
			return
		}
		for _, name := range legacy {
			if v := os.Getenv(name); v != "" {
				*dst = v
				return
			}
		}
	}

	// The legacy candidates are all SCREAMING_SNAKE and specific to this plugin,
	// which is not cosmetic. os.Getenv is case-insensitive on Windows, so
	// accepting a bare `team` or `userEmail` would silently adopt an unrelated
	// TEAM=... exported by some other tool — on Windows only, and discovered
	// months later from a dashboard nobody could explain.
	set(&cfg.GA4MeasurementID, "GROWISTO_TELEMETRY_GA4_MEASUREMENT_ID", "ga4MeasurementId", "GA4_MEASUREMENT_ID")
	set(&cfg.GA4APISecret, "GROWISTO_TELEMETRY_GA4_API_SECRET", "ga4ApiSecret", "GA4_API_SECRET")
	set(&cfg.UserEmail, "GROWISTO_TELEMETRY_USER_EMAIL", "userEmail")
	set(&cfg.Team, "GROWISTO_TELEMETRY_TEAM", "team")
	set(&cfg.PromptCapture, "GROWISTO_TELEMETRY_PROMPT_CAPTURE", "promptCapture")
	set(&cfg.PathCapture, "GROWISTO_TELEMETRY_PATH_CAPTURE", "pathCapture")
	set(&cfg.Debug, "GROWISTO_TELEMETRY_DEBUG", "debug")

	// Both of these are privacy dials, and neither source that sets them can
	// validate them: plugin.json's userConfig has no enum support, and an
	// environment variable is free text by definition. So normalise here, and
	// fail *closed* — an absent value keeps the documented default, but a value
	// that was clearly meant to be something and came out wrong ("Hash",
	// "basename ", "nonw") resolves to the most private option rather than
	// quietly widening capture beyond what the person asked for.
	cfg.PromptCapture = pick(cfg.PromptCapture, []string{"preview", "hash"}, "preview", "hash")
	cfg.PathCapture = pick(cfg.PathCapture, []string{"full", "basename", "none"}, "full", "none")
	return cfg
}

// pick resolves a free-text capture-mode setting to one of allowed.
func pick(value string, allowed []string, absent, invalid string) string {
	normalised := strings.ToLower(strings.TrimSpace(value))
	if normalised == "" {
		return absent
	}
	for _, a := range allowed {
		if normalised == a {
			return normalised
		}
	}
	return invalid
}

// hasDestination reports whether this machine has anywhere to send events.
// GA4 is the only destination; both halves of the credential are required,
// because a measurement ID without a secret is rejected silently by the
// Measurement Protocol, which returns 2xx for almost everything.
func (c Config) hasDestination() bool {
	return c.GA4MeasurementID != "" && c.GA4APISecret != ""
}

// telemetryDisabled reports the employee opt-out. Any of these switches turns
// the hook into a no-op, and nobody has to explain why they set one.
func telemetryDisabled() bool {
	off := map[string]bool{"0": true, "false": true, "off": true, "no": true}
	for _, name := range []string{"GROWISTO_TELEMETRY", "GROWISTO_TELEMETRY_ENABLED"} {
		if off[strings.ToLower(strings.TrimSpace(os.Getenv(name)))] {
			return true
		}
	}
	on := map[string]bool{"1": true, "true": true, "yes": true, "on": true}
	if on[strings.ToLower(strings.TrimSpace(os.Getenv("GROWISTO_TELEMETRY_DISABLE")))] {
		return true
	}
	if _, err := os.Stat(filepath.Join(baseDir, "DISABLED")); err == nil {
		return true
	}
	return false
}

// --------------------------------------------------------------------------
// log
// --------------------------------------------------------------------------

// logf appends a line to telemetry.log, rotating at 1 MB. Every error in this
// program ends up here and nowhere else: the process cannot print to stdout and
// must not fail, so the log is the only way any of this is ever debuggable.
func logf(format string, args ...interface{}) {
	if err := os.MkdirAll(baseDir, 0o700); err != nil {
		return
	}
	if st, err := os.Stat(logPath); err == nil && st.Size() > 1024*1024 {
		_ = os.Rename(logPath, logPath+".1")
	}
	fh, err := os.OpenFile(logPath, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return
	}
	defer fh.Close()
	fmt.Fprintf(fh, "%s %s\n", time.Now().Format("2006-01-02T15:04:05"),
		fmt.Sprintf(format, args...))
}

// retryShared runs fn, retrying briefly on the Windows "file in use" errors.
//
// On POSIX this is one call: renaming and unlinking an open file are both
// legal. On Windows a concurrent hook appending to the spool makes either fail,
// and the right response is to wait rather than to treat the spool as
// unreadable. Retrying on any error is deliberate over inspecting the errno —
// the retry is harmless when the error is permanent, and getting the Windows
// error-code check subtly wrong would be silent.
func retryShared(fn func() error) error {
	var err error
	for attempt := 0; attempt < shareRetries; attempt++ {
		if err = fn(); err == nil {
			return nil
		}
		if runtime.GOOS != "windows" {
			return err
		}
		time.Sleep(shareRetrySleep * time.Duration(attempt+1))
	}
	return err
}

// --------------------------------------------------------------------------
// modes
// --------------------------------------------------------------------------

func main() {
	// Nothing below is allowed to take down a session. A panic anywhere becomes
	// a log line, and the exit code is 0 on every path out of this function.
	defer func() {
		if r := recover(); r != nil {
			logf("fatal: %v", r)
		}
		os.Exit(0)
	}()

	arg := ""
	if len(os.Args) > 1 {
		arg = os.Args[1]
	}

	switch arg {
	case "--secure":
		// The install scripts this was written for are gone. The caller now is
		// /growisto-telemetry, when it writes config.json. Keeping it is still
		// worth it: the alternative is that command choosing between chmod and
		// icacls by hand, and config.json holds a GA4 write credential, so a
		// permissions step that quietly does nothing is the failure that
		// matters here.
		if len(os.Args) < 3 {
			fmt.Fprintln(os.Stderr, "usage: growisto-hook --secure <path>")
			return
		}
		fmt.Println(secureFile(os.Args[2]))
		return
	case "--status":
		// Deliberately before the opt-out check: somebody who has opted out
		// still deserves a straight answer about what is and is not running.
		statusMode()
		return
	}

	if telemetryDisabled() {
		return
	}

	switch arg {
	case "--flush":
		flush()
	case "--test":
		testMode()
	default:
		hookMode()
	}
}

func hookMode() {
	raw, err := io.ReadAll(os.Stdin)
	if err != nil {
		logf("stdin read failed: %v", err)
		return
	}
	var payload map[string]interface{}
	if err := json.Unmarshal(raw, &payload); err != nil {
		logf("payload parse failed: %v", err)
		return
	}
	cfg := loadConfig()
	warnIfNoDestination(cfg)
	// allowSubprocess=false: nothing on the synchronous hook path may shell
	// out. If identity is not in config or cache, the flusher resolves it.
	event := buildEvent(payload, cfg, false)
	if event == nil {
		return
	}
	// Before the spool, not after: the point is that a second hook entry firing
	// for the same occurrence never becomes a second row in GA4.
	if alreadySeen(event) {
		logf("suppressed duplicate %s (session %s)", event.EventName, event.SessionID)
		return
	}
	if err := spoolAppend(event); err != nil {
		logf("spool append failed: %v", err)
		return
	}
	spawnFlush()
}

func statusMode() {
	cfg := loadConfig()
	email, source := resolveUserEmail(cfg, true)

	pending := 0
	if raw, err := os.ReadFile(spoolPath); err == nil {
		pending = strings.Count(strings.TrimRight(string(raw), "\n"), "\n")
		if len(strings.TrimSpace(string(raw))) > 0 {
			pending++
		}
	}

	self, _ := os.Executable()
	configState := "MISSING"
	if _, err := os.Stat(configPath); err == nil {
		configState = "found"
	}
	orNone := func(s string) string {
		if s == "" {
			return "(not configured)"
		}
		return s
	}
	// The API secret is a write credential, so status reports only whether one
	// is present. Half-configured — measurement ID set, secret missing — is a
	// real and otherwise invisible failure, because the Measurement Protocol
	// answers 2xx for a request it silently discards.
	presence := func(s string) string {
		if s == "" {
			return "(not configured)"
		}
		return "set"
	}

	fmt.Println("Growisto Claude telemetry status")
	fmt.Printf("  platform:        %s/%s\n", runtime.GOOS, runtime.GOARCH)
	fmt.Printf("  binary:          %s\n", self)
	fmt.Printf("  hook version:    %s\n", hookVersion)
	fmt.Printf("  enabled:         %t\n", !telemetryDisabled())
	fmt.Printf("  config:          %s (%s)\n", configPath, configState)
	fmt.Printf("  identity:        %s (via %s)\n", email, source)
	fmt.Printf("  prompt capture:  %s\n", cfg.PromptCapture)
	fmt.Printf("  path capture:    %s\n", cfg.PathCapture)
	fmt.Printf("  ga4 property:    %s\n", orNone(cfg.GA4MeasurementID))
	fmt.Printf("  ga4 secret:      %s\n", presence(cfg.GA4APISecret))
	// Which install-time options this process can see. Without this line, the
	// difference between "Claude Code exported nothing" and "it exported them
	// under names this binary does not recognise" is only discoverable by
	// knowing to read a log file.
	fmt.Printf("  plugin options:  %s\n", optionSummary())
	fmt.Printf("  pending events:  %d\n", pending)
	fmt.Printf("  log:             %s\n", logPath)
	fmt.Println("\nTo opt out at any time:  export GROWISTO_TELEMETRY=0")
}

func testMode() {
	cfg := loadConfig()
	cwd, _ := os.Getwd()
	payload := map[string]interface{}{
		"hook_event_name": "UserPromptSubmit",
		"session_id":      "test-session-" + newUUID()[:8],
		"cwd":             cwd,
		"prompt":          "This is a synthetic test prompt from --test.",
		"permission_mode": "default",
	}
	event := buildEvent(payload, cfg, true)
	if event == nil {
		fmt.Println("no event was built for the synthetic payload")
		return
	}
	enrichInPlace(event, cfg)

	pretty, _ := json.MarshalIndent(event, "", "  ")
	fmt.Printf("Event that would be recorded:\n\n%s\n", pretty)
	ga4, _ := json.MarshalIndent(toGA4Payload(event), "", "  ")
	fmt.Printf("\nGA4 payload:\n\n%s\n", ga4)

	if cfg.GA4MeasurementID != "" {
		fmt.Println("\nSending to GA4 debug endpoint...")
		sendToGA4([]*Event{event}, cfg, true)
	}
}
