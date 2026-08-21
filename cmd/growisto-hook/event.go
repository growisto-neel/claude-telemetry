package main

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"time"
	"unicode/utf8"
)

// --------------------------------------------------------------------------
// identity
// --------------------------------------------------------------------------

// osIdentity is the last-resort identity: user@hostname. It is never cached,
// because caching it would poison the cache — resolveUserEmail short-circuits
// on the cache, so a cached fallback means the real git email is never found.
func osIdentity() (string, string) {
	host, err := os.Hostname()
	if err != nil || host == "" {
		return "unknown", "none"
	}
	user := os.Getenv("USER")
	if user == "" {
		user = os.Getenv("USERNAME")
	}
	if user == "" {
		return "unknown", "none"
	}
	return user + "@" + host, "os"
}

type identityCache struct {
	Email  string `json:"email"`
	Source string `json:"source"`
}

// resolveUserEmail resolves the employee's identity, most-trusted source first.
//
// The install writes the email into config.json, so the common path is a struct
// field read with no subprocess. The git fallback is cached on disk because the
// synchronous hook path must not spawn a process per prompt — hence
// allowSubprocess, which is false on that path and true in the flusher.
func resolveUserEmail(cfg Config, allowSubprocess bool) (string, string) {
	if cfg.UserEmail != "" {
		return strings.ToLower(strings.TrimSpace(cfg.UserEmail)), "config"
	}

	if raw, err := os.ReadFile(identPath); err == nil {
		var cached identityCache
		if json.Unmarshal(raw, &cached) == nil && cached.Email != "" {
			source := cached.Source
			if source == "" {
				source = "cache"
			}
			return cached.Email, source
		}
	}

	if !allowSubprocess {
		return osIdentity()
	}

	out, err := runCommand(2*time.Second, "git", "config", "--get", "user.email")
	if err == nil {
		candidate := strings.ToLower(strings.TrimSpace(out))
		if strings.Contains(candidate, "@") {
			if raw, err := json.Marshal(identityCache{Email: candidate, Source: "git"}); err == nil {
				if os.MkdirAll(baseDir, 0o700) == nil {
					_ = os.WriteFile(identPath, raw, 0o600)
				}
			}
			return candidate, "git"
		}
	}

	return osIdentity()
}

// stableClientID is one random ID per install, used as the GA4 client_id. It is
// deliberately not derived from identity: GA4 needs a stable device key, and
// deriving it from the email would make the two impossible to separate later.
func stableClientID() string {
	if raw, err := os.ReadFile(clientPath); err == nil {
		if cid := strings.TrimSpace(string(raw)); cid != "" {
			return cid
		}
	}
	cid := newUUID()
	if os.MkdirAll(baseDir, 0o700) == nil {
		_ = os.WriteFile(clientPath, []byte(cid), 0o600)
	}
	return cid
}

func newUUID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// A UUID is only ever an opaque key here, so a time-derived fallback is
		// acceptable; failing would be worse than a slightly weaker ID.
		now := uint64(time.Now().UnixNano())
		for i := 0; i < 8; i++ {
			b[i] = byte(now >> (8 * i))
			b[i+8] = byte(now>>(8*i)) ^ 0xa5
		}
	}
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // variant 10
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

func sha256Hex(text string) string {
	sum := sha256.Sum256([]byte(text))
	return hex.EncodeToString(sum[:])
}

// --------------------------------------------------------------------------
// rune-safe string helpers
//
// Go indexes strings by byte and Python by character. Slicing by byte would cut
// a multi-byte rune in half and put invalid UTF-8 into the spool and into GA4,
// so every truncation here counts runes — as the Python original did implicitly.
// --------------------------------------------------------------------------

func runeLen(s string) int { return utf8.RuneCountInString(s) }

// clipRunes returns the first n runes of s.
func clipRunes(s string, n int) string {
	if runeLen(s) <= n {
		return s
	}
	count := 0
	for i := range s {
		if count == n {
			return s[:i]
		}
		count++
	}
	return s
}

// tailRunes returns the last n runes of s.
func tailRunes(s string, n int) string {
	total := runeLen(s)
	if total <= n {
		return s
	}
	count := 0
	for i := range s {
		if count == total-n {
			return s[i:]
		}
		count++
	}
	return s
}

// --------------------------------------------------------------------------
// redaction
// --------------------------------------------------------------------------

// Deliberately conservative: these patterns catch the credential shapes that
// most often end up pasted into a prompt. This is damage limitation, not a
// guarantee. See PRIVACY_NOTICE.md.
//
// Ported to RE2, which has no backreferences in patterns — none were used — and
// spells replacement groups ${1} rather than \1.
var redactions = []struct {
	pattern     *regexp.Regexp
	replacement string
}{
	{regexp.MustCompile(`\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}\b`), "[REDACTED_API_KEY]"},
	{regexp.MustCompile(`\bgh[pousr]_[A-Za-z0-9]{16,}\b`), "[REDACTED_GITHUB_TOKEN]"},
	{regexp.MustCompile(`\bAKIA[0-9A-Z]{16}\b`), "[REDACTED_AWS_KEY]"},
	{regexp.MustCompile(`\bxox[baprs]-[A-Za-z0-9\-]{10,}\b`), "[REDACTED_SLACK_TOKEN]"},
	{regexp.MustCompile(`\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b`), "[REDACTED_JWT]"},
	{regexp.MustCompile(`(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----`), "[REDACTED_PRIVATE_KEY]"},
	{regexp.MustCompile(`\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b`), "[REDACTED_SSN]"},
	{regexp.MustCompile(`\b(?:\d[ -]*?){13,16}\b`), "[REDACTED_CARD_NUMBER]"},
	{regexp.MustCompile(`(?i)\b(password|passwd|secret|api[_-]?key|token|bearer)\b\s*[:=]\s*\S+`), "${1}=[REDACTED]"},
}

func redact(text string) string {
	if text == "" {
		return text
	}
	for _, r := range redactions {
		text = r.pattern.ReplaceAllString(text, r.replacement)
	}
	return text
}

// shapePrompt returns the prompt preview, or "".
//
// There is deliberately no mode that returns full prompt text. The most that is
// ever retained anywhere is ga4ParamMax characters, scrubbed of the common
// secret shapes. prompt_chars on the event records how long the real prompt
// was, so you can still see that there was more.
//
//	preview  first 100 chars, plus length, word count and hash (default)
//	hash     length, word count and hash only; no text at all
//
// Redaction runs before truncation, not after. The other order would cut a
// secret in half and ship the surviving half unrecognised.
func shapePrompt(prompt, mode string) string {
	if mode == "hash" || prompt == "" {
		return ""
	}
	return clipRunes(redact(prompt), ga4ParamMax)
}

// --------------------------------------------------------------------------
// event construction
// --------------------------------------------------------------------------

// skillTools are the PreToolUse tools worth an event. Keep in step with the
// `matcher` in hooks/hooks.json — the matcher decides whether this process runs
// at all, and this set decides whether the event survives. A tool missing from
// either one produces silence, not an error.
//
// `Agent` and `Task` are the same thing under two names: Claude Code renamed
// the subagent tool, and which name a given install sends depends on its
// version, so both are listed rather than guessing.
var skillTools = map[string]bool{
	"Skill": true, "Task": true, "Agent": true, "SlashCommand": true,
}

var eventNameMap = map[string]string{
	"UserPromptSubmit":     "cc_prompt",
	"SessionStart":         "cc_session_start",
	"SessionEnd":           "cc_session_end",
	"PreToolUse":           "cc_skill",
	"UserPromptExpansion":  "cc_slash_command",
}

// Event is one recorded hook occurrence. It is the spool line format and the
// input to the GA4 projection.
//
// A struct rather than a map, for two reasons: JSON field order is then
// deterministic, so spool files diff cleanly and are readable by eye; and the
// set of fields that can ever reach a destination is stated in one place where
// it can be reviewed against the privacy notice.
//
// The `_`-prefixed field is internal bookkeeping. It is never part of the GA4
// projection, which toGA4Payload builds field by field, so it cannot leave the
// machine by accident: a field is only ever sent if someone adds it there.
type Event struct {
	SchemaVersion int    `json:"schema_version"`
	EventName     string `json:"event_name"`
	HookEventName string `json:"hook_event_name"`
	TsMs          int64  `json:"ts_ms"`
	TzOffsetMin   int    `json:"tz_offset_min"`

	// identity
	UserEmail       string `json:"user_email,omitempty"`
	UserEmailSHA256 string `json:"user_email_sha256,omitempty"`
	UserIDSource    string `json:"user_id_source,omitempty"`
	Team            string `json:"team,omitempty"`
	ClientID        string `json:"client_id,omitempty"`

	// location. `repo` is filled in later by the background flusher so the hook
	// path never shells out to git; it resolves the repo from folder_path, which
	// means repo detection only works in `full` path mode. That is deliberate —
	// carrying the real cwd in a side field so the flusher could use it would
	// write the full path into the spool even when the operator configured
	// path_capture to suppress it.
	FolderPath string `json:"folder_path,omitempty"`
	FolderName string `json:"folder_name,omitempty"`
	Repo       string `json:"repo,omitempty"`

	// session
	SessionID       string `json:"session_id,omitempty"`
	SessionSource   string `json:"session_source,omitempty"`
	SessionEndReason string `json:"session_end_reason,omitempty"`
	Model           string `json:"model,omitempty"`
	PermissionMode  string `json:"permission_mode,omitempty"`
	AgentID         string `json:"agent_id,omitempty"`

	// skill / tool
	Skill    string `json:"skill,omitempty"`
	ToolName string `json:"tool_name,omitempty"`

	// prompt. There is no full-text field by design; prompt_chars is what tells
	// you the real prompt was longer than the preview. Neither count carries
	// omitempty: a genuine zero is information, and dropping it would make a
	// session-start event indistinguishable from a broken one.
	PromptPreview string `json:"prompt_preview,omitempty"`
	PromptChars   int    `json:"prompt_chars"`
	PromptWords   int    `json:"prompt_words"`
	PromptSHA256  string `json:"prompt_sha256,omitempty"`

	// environment
	OS          string `json:"os,omitempty"`
	HookVersion string `json:"hook_version,omitempty"`

	// Internal delivery bookkeeping. Written to the spool file so that a flush
	// interrupted halfway does not resend what already arrived, and never sent
	// anywhere — toGA4Payload is an explicit allowlist, so this cannot leak by
	// being forgotten. The `_` prefix marks it as local-only to anyone reading
	// their own spool file.
	GA4Done bool `json:"_ga4_done,omitempty"`
}

func (e *Event) delivered() bool { return e.GA4Done }

func str(payload map[string]interface{}, key string) string {
	if v, ok := payload[key].(string); ok {
		return v
	}
	return ""
}

// extractSkillName finds the skill identifier inside a PreToolUse payload.
//
// Claude Code has no dedicated skill hook event. A skill invocation shows up as
// PreToolUse with tool_name == "Skill" (and Task/Agent for subagents), with the
// identifier inside tool_input.
//
// Note that `command` and `description` are in this list. That is safe only
// because the caller has already restricted tool_name to skillTools. Widening
// the hooks.json matcher without revisiting this function would ship full bash
// command lines, file paths and fetched URLs into GA4 under the `skill`
// dimension.
func extractSkillName(payload map[string]interface{}) string {
	toolInput, ok := payload["tool_input"].(map[string]interface{})
	if !ok {
		return ""
	}
	for _, key := range []string{"skill", "skill_name", "name", "command", "subagent_type", "description"} {
		if val, ok := toolInput[key].(string); ok {
			if trimmed := strings.TrimSpace(val); trimmed != "" {
				return clipRunes(trimmed, 120)
			}
		}
	}
	return ""
}

// localUTCOffsetMinutes is minutes east of UTC, DST-aware by virtue of being
// taken from the current instant rather than from a zone table.
func localUTCOffsetMinutes() int {
	_, offset := time.Now().Zone()
	return offset / 60
}

func buildEvent(payload map[string]interface{}, cfg Config, allowSubprocess bool) *Event {
	hookEvent := str(payload, "hook_event_name")
	if hookEvent == "" {
		hookEvent = "Unknown"
	}
	toolName := str(payload, "tool_name")

	// Only forward the tool events that tell us something about skill adoption.
	if hookEvent == "PreToolUse" && !skillTools[toolName] {
		// Every other tool is dropped on purpose — see README, "Known
		// limitations". Record the name locally anyway: if a future Claude Code
		// renames these tools again, telemetry goes quiet with no error, and
		// this log line is the only thing that would say why.
		logf("dropped PreToolUse for tool_name=%q", toolName)
		return nil
	}

	name, known := eventNameMap[hookEvent]
	if !known {
		return nil
	}

	email, idSource := resolveUserEmail(cfg, allowSubprocess)

	cwd := str(payload, "cwd")
	if cwd == "" {
		cwd, _ = os.Getwd()
	}
	prompt := str(payload, "prompt")

	// `none` has to suppress folder_name as well as folder_path. The directory
	// name on its own is still a path fragment, it is still mapped into GA4, and
	// an operator who chose `none` asked for no location at all.
	folderName := ""
	if cwd != "" {
		folderName = filepath.Base(cwd)
	}
	folderPath := cwd
	switch cfg.PathCapture {
	case "none":
		folderPath, folderName = "", ""
	case "basename":
		folderPath = folderName
	}

	event := &Event{
		SchemaVersion: 1,
		EventName:     name,
		HookEventName: hookEvent,
		TsMs:          time.Now().UnixMilli(),
		TzOffsetMin:   localUTCOffsetMinutes(),

		UserEmail:       email,
		UserEmailSHA256: sha256Hex(email),
		UserIDSource:    idSource,
		Team:            cfg.Team,
		ClientID:        stableClientID(),

		FolderPath: folderPath,
		FolderName: folderName,

		SessionID:        str(payload, "session_id"),
		SessionSource:    str(payload, "source"),
		SessionEndReason: str(payload, "reason"),
		Model:            str(payload, "model"),
		PermissionMode:   str(payload, "permission_mode"),
		AgentID:          str(payload, "agent_id"),

		ToolName: toolName,

		PromptPreview: shapePrompt(prompt, cfg.PromptCapture),
		PromptChars:   runeLen(prompt),
		PromptWords:   len(strings.Fields(prompt)),

		OS:          runtime.GOOS,
		HookVersion: hookVersion,
	}
	if hookEvent == "PreToolUse" {
		event.Skill = extractSkillName(payload)
	}
	if prompt != "" {
		event.PromptSHA256 = sha256Hex(prompt)
	}
	return event
}

// --------------------------------------------------------------------------
// GA4 mapping
// --------------------------------------------------------------------------

// toGA4Payload builds a deliberately small, dashboard-friendly projection of
// the event. GA4 allows 25 params per event and truncates every value at 100
// characters, so anything sent beyond that is wasted and misleading.
func toGA4Payload(e *Event) map[string]interface{} {
	clip := func(v string) string { return clipRunes(v, ga4ParamMax) }

	params := map[string]interface{}{
		"engagement_time_msec": 1,
		"prompt_chars":         e.PromptChars,
		"prompt_words":         e.PromptWords,
	}
	set := func(key, val string) {
		if val != "" {
			params[key] = val
		}
	}
	// Deliberately NOT named `session_id`: that param drives GA4's own session
	// stitching, and a Claude session UUID there would distort GA4's session
	// counts. Keep Claude's session as its own dimension.
	set("cc_session_id", clip(e.SessionID))
	set("user_email", clip(e.UserEmail))
	set("team", clip(e.Team))
	set("folder_name", clip(e.FolderName))
	// The tail of a path is the informative half once it exceeds 100 chars.
	set("folder_path", tailRunes(e.FolderPath, ga4ParamMax))
	set("repo", clip(e.Repo))
	set("skill", clip(e.Skill))
	set("tool_name", clip(e.ToolName))
	set("model", clip(e.Model))
	set("session_source", clip(e.SessionSource))
	set("permission_mode", clip(e.PermissionMode))
	set("prompt_preview", clip(e.PromptPreview))
	set("prompt_hash", clipRunes(e.PromptSHA256, 16))
	set("os", clip(e.OS))
	set("hook_version", clip(e.HookVersion))

	clientID := e.ClientID
	if clientID == "" {
		clientID = stableClientID()
	}
	ts := e.TsMs
	if ts == 0 {
		ts = time.Now().UnixMilli()
	}

	body := map[string]interface{}{
		"client_id":            clientID,
		"timestamp_micros":     ts * 1000,
		"non_personalized_ads": true,
		"events": []map[string]interface{}{
			{"name": e.EventName, "params": params},
		},
	}
	if e.UserEmail != "" {
		body["user_id"] = e.UserEmail
	}
	return body
}
