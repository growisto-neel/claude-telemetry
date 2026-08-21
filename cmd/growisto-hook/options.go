package main

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"unicode"
)

// --------------------------------------------------------------------------
// install-time plugin options
// --------------------------------------------------------------------------
//
// Claude Code prompts for the values declared in .claude-plugin/plugin.json and
// exports them to hook processes as CLAUDE_PLUGIN_OPTION_<KEY>, with the key
// uppercased. What "uppercased" does to a camelCase key like ga4MeasurementId is
// not something the documentation pins down -- GA4MEASUREMENTID and
// GA4_MEASUREMENT_ID are both plausible -- and guessing wrong is expensive,
// because the failure is silent: events spool forever with nowhere to send them.
//
// So the match ignores case and separators, which is right for every spelling
// rather than for one of them.
//
// This logic used to live in the bash launcher. It had to move here because the
// launcher no longer runs everywhere: Claude Code execs the Windows binary
// directly, with nothing in front of it. Anything the launcher did that the
// binary depends on would have been silently absent on exactly the platform
// that has been hardest to get working.

// optionPrefixes are the environment prefixes Claude Code has used for
// userConfig values. Both are checked rather than one, for the same reason the
// key match is fuzzy: a wrong guess here produces no error, just no data.
var optionPrefixes = []string{
	"CLAUDE_PLUGIN_OPTION_",
	"CLAUDE_PLUGIN_CONFIG_",
}

// canonicalOptionName reduces a name to something comparable across spellings:
// lowercase, with the separators that distinguish GA4_MEASUREMENT_ID from
// GA4MEASUREMENTID removed.
func canonicalOptionName(name string) string {
	var b strings.Builder
	for _, r := range name {
		if r == '_' || r == '-' || r == ' ' {
			continue
		}
		b.WriteRune(unicode.ToLower(r))
	}
	return b.String()
}

// pluginOptions returns the install-time values, keyed by canonical name.
//
// Only prefixed variables are collected. Scanning the whole environment for
// anything that canonicalises to a userConfig key would be a wider net than it
// looks: a machine with an unrelated TEAM or USER_EMAIL exported for some other
// tool would start reporting it as a telemetry dimension.
func pluginOptions() map[string]string {
	found := map[string]string{}
	for _, entry := range os.Environ() {
		eq := strings.IndexByte(entry, '=')
		if eq <= 0 {
			continue
		}
		name, value := entry[:eq], entry[eq+1:]
		if value == "" {
			continue
		}
		for _, prefix := range optionPrefixes {
			if len(name) > len(prefix) && strings.EqualFold(name[:len(prefix)], prefix) {
				found[canonicalOptionName(name[len(prefix):])] = value
				break
			}
		}
	}
	return found
}

// optionNames lists the option keys this process can see, sorted, for
// diagnostics. Names only: one of these values is a write credential, and this
// string ends up in a log file that people are encouraged to read and forward.
func optionNames() []string {
	var names []string
	for name := range pluginOptions() {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

func optionSummary() string {
	names := optionNames()
	if len(names) == 0 {
		return "(none visible)"
	}
	return strings.Join(names, ", ")
}

// --------------------------------------------------------------------------
// the "nowhere to send" diagnostic
// --------------------------------------------------------------------------

func noDestMarker() string { return filepath.Join(baseDir, "NO_DEST") }

// warnIfNoDestination records, once, that this machine has no destination, and
// names the plugin options that were actually visible.
//
// Without this the failure is invisible from outside the hook process: events
// pile up in the spool and there is nothing to distinguish an option-naming
// mismatch from a machine nobody ever configured. Once, not every prompt,
// because a log line per prompt would bury the rest of the log.
func warnIfNoDestination(cfg Config) {
	if cfg.hasDestination() {
		// The marker is cleared by the flusher, which is where a destination is
		// known to be usable and where the extra syscalls are free.
		return
	}
	if _, err := os.Stat(configPath); err == nil {
		// A config file exists, so the install-time values are not the story;
		// --status reports which half is missing.
		return
	}
	if _, err := os.Stat(noDestMarker()); err == nil {
		return
	}
	if err := os.MkdirAll(baseDir, 0o700); err != nil {
		return
	}
	logf("no GA4 destination. plugin options visible: %s", optionSummary())
	if fh, err := os.Create(noDestMarker()); err == nil {
		fh.Close()
	}
}

// clearNoDestMarker is called once a destination is known to be configured, so
// the log stops describing a problem that has been fixed and complains again if
// it ever recurs.
func clearNoDestMarker() {
	_ = os.Remove(noDestMarker())
}
