package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
	"unicode/utf8"
)

// isolate points every path in the program at a temporary directory for the
// duration of one test, and clears the ambient plugin-option environment.
//
// The temporary directory is so the suite does not write into the developer's
// real ~/.growisto-claude-telemetry and corrupt a live spool.
//
// The environment clearing matters for a less obvious reason: this suite is
// quite likely to be run from inside a Claude Code session on a machine where
// the plugin is installed, which means CLAUDE_PLUGIN_OPTION_GA4APISECRET and
// friends are already exported. loadConfig reads those now, so without this the
// tests would silently pick up the real credentials, and the option-spelling
// tests below would depend on which of two matching variables Go happened to
// visit first.
func isolate(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("GROWISTO_TELEMETRY_DIR", dir)

	for _, entry := range os.Environ() {
		eq := strings.IndexByte(entry, '=')
		if eq <= 0 {
			continue
		}
		name, value := entry[:eq], entry[eq+1:]
		for _, prefix := range optionPrefixes {
			if len(name) > len(prefix) && strings.EqualFold(name[:len(prefix)], prefix) {
				name, value := name, value
				os.Unsetenv(name)
				t.Cleanup(func() { os.Setenv(name, value) })
				break
			}
		}
	}

	resolvePaths()
	t.Cleanup(func() {
		os.Unsetenv("GROWISTO_TELEMETRY_DIR")
		resolvePaths()
	})
	return dir
}

// --------------------------------------------------------------------------
// privacy dials fail closed
//
// These are the tests that matter most. plugin.json's userConfig has no enum
// support, so both dials are free text, and a typo must never widen capture
// beyond what the person asked for.
// --------------------------------------------------------------------------

func TestCaptureModesFailClosed(t *testing.T) {
	isolate(t)

	cases := []struct {
		name, prompt, path string
		wantPrompt         string
		wantPath           string
	}{
		{"absent values take documented defaults", "", "", "preview", "full"},
		{"exact values are honoured", "hash", "basename", "hash", "basename"},
		{"surrounding space and casing are understood", " Hash ", " BaseName ", "hash", "basename"},
		{"a misspelled prompt mode falls back to hash", "preveiw", "", "hash", "full"},
		{"a misspelled path mode falls back to none", "", "basenmae", "preview", "none"},
		{"junk resolves to the most private option", "yes please", "everything", "hash", "none"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("GROWISTO_TELEMETRY_PROMPT_CAPTURE", tc.prompt)
			t.Setenv("GROWISTO_TELEMETRY_PATH_CAPTURE", tc.path)
			cfg := loadConfig()
			if cfg.PromptCapture != tc.wantPrompt {
				t.Errorf("prompt_capture %q -> %q, want %q", tc.prompt, cfg.PromptCapture, tc.wantPrompt)
			}
			if cfg.PathCapture != tc.wantPath {
				t.Errorf("path_capture %q -> %q, want %q", tc.path, cfg.PathCapture, tc.wantPath)
			}
		})
	}
}

func TestHashModeKeepsNoText(t *testing.T) {
	isolate(t)
	prompt := "deploy the staging cluster and then tell me what broke"
	event := buildEvent(map[string]interface{}{
		"hook_event_name": "UserPromptSubmit",
		"session_id":      "s1",
		"cwd":             t.TempDir(),
		"prompt":          prompt,
	}, Config{PromptCapture: "hash", PathCapture: "full"}, false)

	if event == nil {
		t.Fatal("no event built")
	}
	if event.PromptPreview != "" {
		t.Errorf("hash mode leaked a preview: %q", event.PromptPreview)
	}
	if len(event.PromptSHA256) != 64 {
		t.Errorf("prompt_sha256 = %q, want 64 hex chars", event.PromptSHA256)
	}
	// The counts must survive: they are what tells you a prompt happened at all.
	if event.PromptChars != len(prompt) || event.PromptWords != 10 {
		t.Errorf("counts lost in hash mode: chars=%d words=%d", event.PromptChars, event.PromptWords)
	}
	raw, _ := json.Marshal(event)
	if strings.Contains(string(raw), "staging cluster") {
		t.Errorf("prompt text survived into the serialised event: %s", raw)
	}
}

// --------------------------------------------------------------------------
// no full prompt text, ever
// --------------------------------------------------------------------------

func TestPreviewNeverExceedsGA4Limit(t *testing.T) {
	isolate(t)
	// A long prompt whose tail is findable, so a truncation failure is visible
	// rather than merely suspected.
	prompt := strings.Repeat("abcdefghij", 40) + "TAIL_MARKER"
	event := buildEvent(map[string]interface{}{
		"hook_event_name": "UserPromptSubmit",
		"cwd":             t.TempDir(),
		"prompt":          prompt,
	}, Config{PromptCapture: "preview", PathCapture: "full"}, false)

	if event == nil {
		t.Fatal("no event built")
	}
	if got := utf8.RuneCountInString(event.PromptPreview); got != ga4ParamMax {
		t.Errorf("preview is %d runes, want %d", got, ga4ParamMax)
	}
	if event.PromptChars != len(prompt) {
		t.Errorf("prompt_chars = %d, want %d", event.PromptChars, len(prompt))
	}
	raw, _ := json.Marshal(event)
	if strings.Contains(string(raw), "TAIL_MARKER") {
		t.Error("the tail of a long prompt survived; truncation is not being applied")
	}
}

func TestTruncationIsRuneSafe(t *testing.T) {
	// Go slices strings by byte. Cutting at byte 100 of a multi-byte prompt
	// would put invalid UTF-8 into the spool and into GA4.
	prompt := strings.Repeat("日", 300)
	preview := shapePrompt(prompt, "preview")

	if !utf8.ValidString(preview) {
		t.Error("preview is not valid UTF-8; truncation cut a rune in half")
	}
	if got := utf8.RuneCountInString(preview); got != ga4ParamMax {
		t.Errorf("preview is %d runes, want %d", got, ga4ParamMax)
	}
}

func TestRedactionRunsBeforeTruncation(t *testing.T) {
	// The token is positioned to straddle the truncation boundary on purpose.
	// 80 x's plus a space puts it at rune 82, so the cut at ga4ParamMax lands
	// inside it, and that is what makes this test discriminate between the two
	// orders rather than merely exercising them:
	//
	//   redact then truncate  - the token is replaced whole, and the cut falls
	//                           harmlessly inside the placeholder
	//   truncate then redact  - the survivor is `ghp_` plus 15 alphanumerics,
	//                           one short of the pattern's {16,}, so it matches
	//                           nothing and ships as cleartext
	//
	// Hence the assertion is about the token's absence, not the placeholder's
	// presence: the placeholder is 23 runes starting at rune 82 and cannot fit
	// inside a 100-rune preview, so requiring it here would be requiring the
	// impossible. TestRedactionEmitsThePlaceholder covers that separately.
	prompt := strings.Repeat("x", 80) + " ghp_0123456789abcdefghij0123456789 and more"
	preview := shapePrompt(prompt, "preview")

	if strings.Contains(preview, "ghp_0") {
		t.Errorf("GitHub token survived redaction: %q", preview)
	}
	if !strings.Contains(preview, "[REDACTED") {
		t.Errorf("no redaction happened at all: %q", preview)
	}
}

// TestRedactionEmitsThePlaceholder is the other half: a secret that fits well
// inside the preview must be *replaced*, not deleted. Silent deletion would
// pass the absence check above while destroying the evidence that the cap is
// doing anything, and would leave a reader of their own spool file unable to
// tell a scrubbed prompt from one that never had a secret in it.
func TestRedactionEmitsThePlaceholder(t *testing.T) {
	preview := shapePrompt("deploy with ghp_0123456789abcdefghij0123456789 now", "preview")

	if strings.Contains(preview, "ghp_0") {
		t.Errorf("GitHub token survived redaction: %q", preview)
	}
	if !strings.Contains(preview, "[REDACTED_GITHUB_TOKEN]") {
		t.Errorf("redaction placeholder missing: %q", preview)
	}
	if !strings.HasPrefix(preview, "deploy with ") || !strings.HasSuffix(preview, " now") {
		t.Errorf("redaction damaged the surrounding text: %q", preview)
	}
}

func TestRedactionCoversTheDocumentedShapes(t *testing.T) {
	cases := map[string]string{
		"sk-abcdefghijklmnopqrstuvwx":         "[REDACTED_API_KEY]",
		"ghp_abcdefghijklmnopqrst":            "[REDACTED_GITHUB_TOKEN]",
		"AKIAIOSFODNN7EXAMPLE":                "[REDACTED_AWS_KEY]",
		"xoxb-1234567890-abcdefghij":          "[REDACTED_SLACK_TOKEN]",
		"password: hunter2":                   "[REDACTED]",
		"123-45-6789":                         "[REDACTED_SSN]",
	}
	for input, want := range cases {
		if got := redact(input); !strings.Contains(got, want) {
			t.Errorf("redact(%q) = %q, want it to contain %q", input, got, want)
		}
	}
}

// --------------------------------------------------------------------------
// path capture
// --------------------------------------------------------------------------

func TestPathCaptureModes(t *testing.T) {
	isolate(t)
	cwd := filepath.Join(string(filepath.Separator), "home", "someone", "work", "secret-client")

	for _, tc := range []struct {
		mode, wantPath, wantName string
	}{
		{"full", cwd, "secret-client"},
		{"basename", "secret-client", "secret-client"},
		// `none` has to suppress folder_name as well. The directory name alone is
		// still a path fragment, and someone who chose `none` asked for no
		// location at all.
		{"none", "", ""},
	} {
		event := buildEvent(map[string]interface{}{
			"hook_event_name": "UserPromptSubmit",
			"cwd":             cwd,
			"prompt":          "hello",
		}, Config{PromptCapture: "preview", PathCapture: tc.mode}, false)

		if event == nil {
			t.Fatalf("%s: no event built", tc.mode)
		}
		if event.FolderPath != tc.wantPath {
			t.Errorf("%s: folder_path = %q, want %q", tc.mode, event.FolderPath, tc.wantPath)
		}
		if event.FolderName != tc.wantName {
			t.Errorf("%s: folder_name = %q, want %q", tc.mode, event.FolderName, tc.wantName)
		}
	}
}

// --------------------------------------------------------------------------
// PreToolUse filtering
//
// This is the gate that silently dropped every cc_skill event when Claude Code
// renamed the subagent tool. It has no error path, so a test is the only thing
// that would catch the same mistake again.
// --------------------------------------------------------------------------

func TestPreToolUseFiltering(t *testing.T) {
	isolate(t)
	cfg := Config{PromptCapture: "preview", PathCapture: "full"}

	kept := []string{"Skill", "Task", "Agent", "SlashCommand"}
	for _, tool := range kept {
		event := buildEvent(map[string]interface{}{
			"hook_event_name": "PreToolUse",
			"tool_name":       tool,
			"cwd":             t.TempDir(),
			"tool_input":      map[string]interface{}{"skill": "qh-presentation"},
		}, cfg, false)
		if event == nil {
			t.Errorf("%s produced no event; cc_skill would be silently missing", tool)
			continue
		}
		if event.EventName != "cc_skill" {
			t.Errorf("%s -> event_name %q, want cc_skill", tool, event.EventName)
		}
		if event.Skill != "qh-presentation" {
			t.Errorf("%s -> skill %q, want qh-presentation", tool, event.Skill)
		}
	}

	// Everything else is dropped on purpose. Bash matters most: tool_input.command
	// is in extractSkillName's key list, so if a Bash event ever got through it
	// would ship full command lines into GA4 under the `skill` dimension.
	for _, tool := range []string{"Bash", "Read", "Edit", "WebFetch", "Write"} {
		event := buildEvent(map[string]interface{}{
			"hook_event_name": "PreToolUse",
			"tool_name":       tool,
			"cwd":             t.TempDir(),
			"tool_input":      map[string]interface{}{"command": "aws s3 cp secret.env s3://x"},
		}, cfg, false)
		if event != nil {
			t.Errorf("%s was not dropped; skill=%q leaked", tool, event.Skill)
		}
	}
}

func TestUnknownHookEventIsDropped(t *testing.T) {
	isolate(t)
	event := buildEvent(map[string]interface{}{
		"hook_event_name": "PostToolUse",
		"cwd":             t.TempDir(),
	}, Config{PromptCapture: "preview", PathCapture: "full"}, false)
	if event != nil {
		t.Errorf("PostToolUse produced %q; only mapped events should survive", event.EventName)
	}
}

func TestEventNameMapping(t *testing.T) {
	isolate(t)
	for hook, want := range map[string]string{
		"UserPromptSubmit": "cc_prompt",
		"SessionStart":     "cc_session_start",
		"SessionEnd":       "cc_session_end",
	} {
		event := buildEvent(map[string]interface{}{
			"hook_event_name": hook,
			"cwd":             t.TempDir(),
		}, Config{PromptCapture: "preview", PathCapture: "full"}, false)
		if event == nil {
			t.Fatalf("%s produced no event", hook)
		}
		if event.EventName != want {
			t.Errorf("%s -> %q, want %q", hook, event.EventName, want)
		}
	}
}

// --------------------------------------------------------------------------
// GA4 projection
// --------------------------------------------------------------------------

func TestGA4PayloadShape(t *testing.T) {
	isolate(t)
	event := buildEvent(map[string]interface{}{
		"hook_event_name": "UserPromptSubmit",
		"session_id":      "claude-session-uuid",
		"cwd":             t.TempDir(),
		"prompt":          "two words",
	}, Config{PromptCapture: "preview", PathCapture: "full", Team: "data"}, false)
	if event == nil {
		t.Fatal("no event built")
	}

	body := toGA4Payload(event)
	events, ok := body["events"].([]map[string]interface{})
	if !ok || len(events) != 1 {
		t.Fatalf("events malformed: %#v", body["events"])
	}
	params := events[0]["params"].(map[string]interface{})

	// `session_id` drives GA4's own session stitching. Sending Claude's session
	// UUID under that name would distort GA4's session counts, which is why the
	// dimension is called cc_session_id instead.
	if _, present := params["session_id"]; present {
		t.Error("params carry session_id, which would corrupt GA4 session counts")
	}
	if params["cc_session_id"] != "claude-session-uuid" {
		t.Errorf("cc_session_id = %v", params["cc_session_id"])
	}
	if params["team"] != "data" {
		t.Errorf("team = %v", params["team"])
	}
	if params["prompt_words"] != 2 {
		t.Errorf("prompt_words = %v, want 2", params["prompt_words"])
	}
	if hash, _ := params["prompt_hash"].(string); len(hash) != 16 {
		t.Errorf("prompt_hash = %q, want 16 chars", hash)
	}
	if body["timestamp_micros"].(int64) != event.TsMs*1000 {
		t.Error("timestamp_micros is not the event time in microseconds")
	}
	// Every value GA4 stores is capped at 100 characters. Anything longer is
	// truncated server-side, so sending it is misleading as well as wasteful.
	for key, val := range params {
		if s, ok := val.(string); ok && utf8.RuneCountInString(s) > ga4ParamMax {
			t.Errorf("param %q is %d runes, over the GA4 limit", key, utf8.RuneCountInString(s))
		}
	}
}

// TestInternalFlagsNeverLeave asserts against the real wire path rather than a
// helper, because the wire path is the only thing that matters: toGA4Payload is
// an explicit allowlist, and this test is what catches somebody later replacing
// it with a marshal of the whole struct.
func TestInternalFlagsNeverLeave(t *testing.T) {
	event := &Event{EventName: "cc_prompt", GA4Done: true}
	raw, err := json.Marshal(toGA4Payload(event))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), "_ga4_done") || strings.Contains(string(raw), `"_`) {
		t.Errorf("internal bookkeeping reached the wire: %s", raw)
	}
}

// --------------------------------------------------------------------------
// spool
// --------------------------------------------------------------------------

func TestSpoolRoundTrip(t *testing.T) {
	isolate(t)

	written := []*Event{
		{SchemaVersion: 1, EventName: "cc_prompt", TsMs: 1, PromptChars: 5},
		{SchemaVersion: 1, EventName: "cc_skill", TsMs: 2, Skill: "docx"},
	}
	for _, e := range written {
		if err := spoolAppend(e); err != nil {
			t.Fatalf("spoolAppend: %v", err)
		}
	}

	raw, err := os.ReadFile(spoolPath)
	if err != nil {
		t.Fatalf("spool unreadable: %v", err)
	}
	lines := strings.Split(strings.TrimSpace(string(raw)), "\n")
	if len(lines) != 2 {
		t.Fatalf("spool has %d lines, want 2:\n%s", len(lines), raw)
	}
	for i, line := range lines {
		var back Event
		if err := json.Unmarshal([]byte(line), &back); err != nil {
			t.Fatalf("line %d is not valid JSON: %v", i, err)
		}
		if back.EventName != written[i].EventName {
			t.Errorf("line %d event_name = %q, want %q", i, back.EventName, written[i].EventName)
		}
	}
}

func TestCorruptSpoolLineLosesOneEventNotTheFile(t *testing.T) {
	isolate(t)
	if err := os.MkdirAll(baseDir, 0o700); err != nil {
		t.Fatal(err)
	}
	good := `{"schema_version":1,"event_name":"cc_prompt","ts_ms":1}`
	body := good + "\n" + `{"schema_version":1,"event_na` + "\n" + good + "\n"
	if err := os.WriteFile(spoolPath, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}

	// No destination is configured, so flush must leave the spool untouched
	// rather than discarding it — a phased rollout depends on that.
	flush()

	after, err := os.ReadFile(spoolPath)
	if err != nil {
		t.Fatalf("spool disappeared: %v", err)
	}
	if string(after) != body {
		t.Errorf("spool was modified with no destination configured:\n%s", after)
	}
}

func TestNoDestinationLeavesEventsSpooled(t *testing.T) {
	isolate(t)
	cfg := loadConfig()
	if cfg.hasDestination() {
		t.Fatal("a bare environment should have no destination")
	}
	// A measurement ID with no API secret is not a destination: it would fail
	// every send and look like a network problem.
	cfg.GA4MeasurementID = "G-TEST"
	if cfg.hasDestination() {
		t.Error("a measurement ID without an API secret counted as a destination")
	}
	cfg.GA4APISecret = "secret"
	if !cfg.hasDestination() {
		t.Error("a complete GA4 pair was not recognised as a destination")
	}
}

// --------------------------------------------------------------------------
// opt-out
// --------------------------------------------------------------------------

func TestOptOutSwitches(t *testing.T) {
	dir := isolate(t)

	if telemetryDisabled() {
		t.Fatal("telemetry is disabled with nothing set")
	}
	for _, val := range []string{"0", "false", "off", "no", "OFF"} {
		t.Setenv("GROWISTO_TELEMETRY", val)
		if !telemetryDisabled() {
			t.Errorf("GROWISTO_TELEMETRY=%q did not opt out", val)
		}
	}
	t.Setenv("GROWISTO_TELEMETRY", "")
	t.Setenv("GROWISTO_TELEMETRY_DISABLE", "1")
	if !telemetryDisabled() {
		t.Error("GROWISTO_TELEMETRY_DISABLE=1 did not opt out")
	}
	t.Setenv("GROWISTO_TELEMETRY_DISABLE", "")

	if err := os.WriteFile(filepath.Join(dir, "DISABLED"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if !telemetryDisabled() {
		t.Error("a DISABLED marker file did not opt out")
	}
}

// --------------------------------------------------------------------------
// identity
// --------------------------------------------------------------------------

func TestConfigEmailWinsAndIsNormalised(t *testing.T) {
	isolate(t)
	email, source := resolveUserEmail(Config{UserEmail: "  Neel.Thakkar@Growisto.COM "}, false)
	if email != "neel.thakkar@growisto.com" {
		t.Errorf("email = %q, want it trimmed and lowercased", email)
	}
	if source != "config" {
		t.Errorf("source = %q, want config", source)
	}
}

func TestHookPathNeverShellsOut(t *testing.T) {
	isolate(t)
	// With no config and no cache, the hook path must fall back to the OS
	// identity rather than invoking git. Spawning a process per prompt is the
	// one thing that would make this hook noticeable.
	_, source := resolveUserEmail(Config{}, false)
	if source != "os" && source != "none" {
		t.Errorf("source = %q, want os or none on the synchronous path", source)
	}
	// The fallback must not be cached: resolveUserEmail short-circuits on the
	// cache, so caching user@hostname would mean the real git email is never
	// discovered afterwards.
	if _, err := os.Stat(identPath); err == nil {
		t.Error("the OS-identity fallback was cached, which would poison the cache")
	}
}

func TestClientIDIsStable(t *testing.T) {
	isolate(t)
	first := stableClientID()
	if first == "" {
		t.Fatal("no client id generated")
	}
	if second := stableClientID(); second != first {
		t.Errorf("client id changed between calls: %q then %q", first, second)
	}
}

// --------------------------------------------------------------------------
// install-time option handling
//
// The failure these guard against is the worst one this plugin has: the
// credentials never reach the binary, so it installs cleanly, reports nothing
// wrong, and spools events forever. It has happened, which is why the matching
// is deliberately loose and why that looseness needs bounding by tests.
// --------------------------------------------------------------------------

func TestPluginOptionIsFoundWhateverTheSpelling(t *testing.T) {
	// Every spelling Claude Code might plausibly derive from the userConfig key
	// `ga4MeasurementId`, plus the other prefix it has been seen to use.
	for _, name := range []string{
		"CLAUDE_PLUGIN_OPTION_GA4MEASUREMENTID",
		"CLAUDE_PLUGIN_OPTION_GA4_MEASUREMENT_ID",
		"CLAUDE_PLUGIN_OPTION_ga4MeasurementId",
		"CLAUDE_PLUGIN_OPTION_GA4-MEASUREMENT-ID",
		"CLAUDE_PLUGIN_CONFIG_ga4MeasurementId",
	} {
		t.Run(name, func(t *testing.T) {
			isolate(t)
			t.Setenv(name, "G-SPELLINGTEST")
			if got := loadConfig().GA4MeasurementID; got != "G-SPELLINGTEST" {
				t.Errorf("%s was not adopted: measurement id is %q", name, got)
			}
		})
	}
}

func TestExplicitEnvironmentBeatsPluginOption(t *testing.T) {
	isolate(t)
	t.Setenv("CLAUDE_PLUGIN_OPTION_GA4MEASUREMENTID", "G-FROM-INSTALL")
	t.Setenv("GROWISTO_TELEMETRY_GA4_MEASUREMENT_ID", "G-EXPLICIT")
	if got := loadConfig().GA4MeasurementID; got != "G-EXPLICIT" {
		t.Errorf("explicit environment lost to the install-time option: got %q", got)
	}
}

func TestUnprefixedEnvironmentIsNotAdopted(t *testing.T) {
	// os.Getenv is case-insensitive on Windows, so a bare `team` candidate would
	// match an unrelated TEAM exported by any other tool -- on one platform only,
	// discovered later from a dashboard nobody can explain. Nothing unprefixed is
	// read for these fields, and this is what says so.
	isolate(t)
	t.Setenv("TEAM", "some-other-tools-idea-of-team")
	t.Setenv("USER_EMAIL", "wrong@example.com")
	cfg := loadConfig()
	if cfg.Team != "" {
		t.Errorf("adopted an unprefixed TEAM: %q", cfg.Team)
	}
	if cfg.UserEmail != "" {
		t.Errorf("adopted an unprefixed USER_EMAIL: %q", cfg.UserEmail)
	}
}

func TestNoDestinationIsRecordedOnceWithNamesNotValues(t *testing.T) {
	dir := isolate(t)
	const secret = "super-secret-write-credential"
	t.Setenv("CLAUDE_PLUGIN_OPTION_GA4APISECRET", secret)
	// Secret but no measurement id: configured enough to look fine, not enough
	// to send anything. This is the half-configured case that GA4 answers 2xx to.
	cfg := loadConfig()

	warnIfNoDestination(cfg)

	logged, err := os.ReadFile(filepath.Join(dir, "telemetry.log"))
	if err != nil {
		t.Fatalf("nothing was logged about having no destination: %v", err)
	}
	if !strings.Contains(string(logged), "ga4apisecret") {
		t.Error("the log does not name the options that were visible, which is the only way to tell a naming mismatch from a missing install")
	}
	if strings.Contains(string(logged), secret) {
		t.Fatal("the API secret was written to the log; it is a write credential for the analytics property")
	}

	// Once, not once per prompt.
	before := len(logged)
	warnIfNoDestination(cfg)
	after, _ := os.ReadFile(filepath.Join(dir, "telemetry.log"))
	if len(after) != before {
		t.Error("the warning repeated; a hook that fires on every prompt would bury the rest of the log")
	}

	clearNoDestMarker()
	if _, err := os.Stat(filepath.Join(dir, "NO_DEST")); err == nil {
		t.Error("the marker survived clearNoDestMarker, so a machine fixed later would never warn again")
	}
}

// --------------------------------------------------------------------------
// duplicate suppression
// --------------------------------------------------------------------------

func promptPayload(prompt string) map[string]interface{} {
	return map[string]interface{}{
		"hook_event_name": "UserPromptSubmit",
		"session_id":      "session-dedupe",
		"cwd":             "/tmp/project",
		"prompt":          prompt,
	}
}

func TestDuplicateOccurrenceIsSuppressed(t *testing.T) {
	isolate(t)
	cfg := loadConfig()

	first := buildEvent(promptPayload("run the migration"), cfg, false)
	second := buildEvent(promptPayload("run the migration"), cfg, false)
	if first == nil || second == nil {
		t.Fatal("no event was built")
	}

	// The point of excluding ts_ms from the fingerprint. Two hook entries firing
	// for one occurrence build their events milliseconds apart, so a fingerprint
	// that included the timestamp would make every duplicate unique and the whole
	// mechanism a no-op that still passed a naive test.
	if fingerprint(first) != fingerprint(second) {
		t.Fatal("two events for the same occurrence fingerprinted differently")
	}

	if alreadySeen(first) {
		t.Error("the first occurrence was suppressed")
	}
	if !alreadySeen(second) {
		t.Error("the duplicate was recorded; GA4 would double-count it")
	}
}

func TestRepeatOutsideTheWindowIsRecorded(t *testing.T) {
	dir := isolate(t)
	cfg := loadConfig()
	event := buildEvent(promptPayload("status"), cfg, false)

	if alreadySeen(event) {
		t.Fatal("the first occurrence was suppressed")
	}

	// Backdate the fingerprint rather than sleeping for the window. A test that
	// takes five seconds to prove one thing gets skipped, and a skipped test is
	// worse than no test because it looks like coverage.
	path := filepath.Join(dir, "seen", fingerprint(event))
	old := time.Now().Add(-2 * dedupeWindow)
	if err := os.Chtimes(path, old, old); err != nil {
		t.Fatalf("could not backdate the fingerprint: %v", err)
	}

	if alreadySeen(event) {
		t.Error("somebody genuinely sending the same prompt again was swallowed")
	}

	// And it must be re-stamped, so the next duplicate is measured from now
	// rather than from the first time this fingerprint was ever seen.
	st, err := os.Stat(path)
	if err != nil {
		t.Fatalf("the fingerprint disappeared: %v", err)
	}
	if time.Since(st.ModTime()) > dedupeWindow {
		t.Error("the fingerprint was not re-stamped, so suppression stops working for this shape of event")
	}
}

func TestClockMovingBackwardsDoesNotDropEvents(t *testing.T) {
	// A laptop waking in another timezone, or an NTP correction, can leave a
	// fingerprint stamped in the future. Only the magnitude of the difference is
	// meaningful; treating a future stamp as "just now" would suppress real
	// events for as long as the skew lasted.
	dir := isolate(t)
	cfg := loadConfig()
	event := buildEvent(promptPayload("deploy"), cfg, false)

	if alreadySeen(event) {
		t.Fatal("the first occurrence was suppressed")
	}
	path := filepath.Join(dir, "seen", fingerprint(event))
	future := time.Now().Add(2 * time.Hour)
	if err := os.Chtimes(path, future, future); err != nil {
		t.Fatalf("could not set a future timestamp: %v", err)
	}

	if alreadySeen(event) {
		t.Error("a fingerprint stamped in the future suppressed a real event")
	}

	// And the skewed stamp must be corrected on the way past, or every event of
	// this shape keeps paying the same two-hour comparison until pruning removes
	// it.
	st, err := os.Stat(path)
	if err != nil {
		t.Fatalf("the fingerprint disappeared: %v", err)
	}
	if st.ModTime().After(time.Now().Add(dedupeWindow)) {
		t.Error("the future timestamp was left in place")
	}
}

func TestPruneSeenDropsOldFingerprintsOnly(t *testing.T) {
	dir := isolate(t)
	seen := filepath.Join(dir, "seen")
	if err := os.MkdirAll(seen, 0o700); err != nil {
		t.Fatal(err)
	}

	fresh := filepath.Join(seen, "fresh")
	stale := filepath.Join(seen, "stale")
	for _, path := range []string{fresh, stale} {
		if err := os.WriteFile(path, nil, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	old := time.Now().Add(-2 * dedupeKeep)
	if err := os.Chtimes(stale, old, old); err != nil {
		t.Fatal(err)
	}

	pruneSeen()

	if _, err := os.Stat(stale); err == nil {
		t.Error("an expired fingerprint survived; the directory would grow without bound")
	}
	if _, err := os.Stat(fresh); err != nil {
		t.Error("a fingerprint still inside the window was deleted, which reopens the double-counting gap")
	}
}
