---
description: Check Growisto telemetry status, or finish configuring it if credentials did not carry over from install
---

Report the state of the Growisto Claude Code telemetry plugin for this user, and repair its configuration if it is incomplete.

## 1. Report status

Run the hook's own status command and show the user the output verbatim:

```
bash "${CLAUDE_PLUGIN_ROOT}"/bin/growisto-hook --status
```

If that produces nothing useful, the launcher could not find a binary for this machine. Gather what would name the gap, rather than guessing at it:

```
uname -s; uname -m; ls "${CLAUDE_PLUGIN_ROOT}"/bin/
```

The lines that matter are `enabled`, `platform`, `hook version`, the resolved email and where it came from, `prompt capture`, whether a GA4 destination is configured, `plugin options`, and `pending events`.

## 2. Diagnose

Read the status output and tell the user plainly which of these they are in:

**Working.** `enabled: true`, a `G-` ID present, `pending events: 0`. Say so and stop. Do not change anything.

**No binary for this platform.** The status command printed nothing, or a `NO_BINARY` marker exists in the data directory. Telemetry is inactive on this machine and no amount of configuration will change that, because the repo carries no build for this OS and CPU. Report the `uname -s` and `uname -m` values and what `bin/` actually contains, and tell them to send those three things to Neel — the fix is another GOOS/GOARCH pair in `build.sh`, which has to happen in the repo. Do not try to build one locally for them.

**Configured but not sending.** A `G-` ID is present and `pending events` is above zero. Read the last few lines of `telemetry.log` in the data directory and report what the send actually failed with — a 403 is a wrong API secret, a connection error is a proxy or firewall.

**Nothing at all.** No status output, no data directory, no log, and `bin/` contains the binaries it should. The hook process is never being started, which means `bash` is not resolving — the launcher is a shell script, and none of the diagnostics exist until it runs. On Windows this is Git Bash missing or shadowed by `C:\Windows\System32\bash.exe`, the WSL launcher; check `where bash` and point them at Git for Windows. Do not attempt to repair the configuration, and do not write `config.json`: there is nothing wrong with it, and doing so would hide the real problem behind a file that looks correct.

**Not configured.** No GA4 destination — either half missing counts, since a measurement ID without a secret fails every send while looking healthy, because the Measurement Protocol returns 2xx for a request it discards. The `plugin options` line names the install-time options the hook could actually see: `(none visible)` means Claude Code exported nothing, while a list that does not include `ga4measurementid` means they arrived under names the hook does not recognise, which is a bug to report rather than something to work around. A `NO_DEST` marker in the data directory and a `no GA4 destination` line in `telemetry.log` say the same thing. This is the case step 3 handles.

**Opted out.** `enabled: false`. Say so and leave it alone. Do not re-enable telemetry for someone who turned it off, and do not ask them why.

## 3. Repair configuration, only if step 2 landed on "not configured"

Ask the user for the GA4 measurement ID and API secret, telling them to get both from Neel if they do not have them. Ask for their work email and team, offering their `git config user.email` as the default.

Then write `config.json` into the telemetry data directory — `$GROWISTO_TELEMETRY_DIR` if set, otherwise `~/.growisto-claude-telemetry/` on Linux and macOS or `%USERPROFILE%\.growisto-claude-telemetry\` on Windows — with exactly these keys:

```json
{
  "ga4_measurement_id": "G-...",
  "ga4_api_secret": "...",
  "user_email": "...",
  "team": "...",
  "prompt_capture": "preview",
  "path_capture": "full"
}
```

Create the directory first if it does not exist. Then lock the file down by asking the hook to do it, rather than choosing between `chmod` and `icacls` yourself:

```
bash "${CLAUDE_PLUGIN_ROOT}"/bin/growisto-hook --secure ~/.growisto-claude-telemetry/config.json
```

It prints what it did. That file holds a credential that can write arbitrary events into the analytics property, so it must not be group- or world-readable — if the command reports it could not restrict the file, say so plainly rather than moving on.

Never echo the API secret back into the conversation after writing it, and never write it anywhere other than that one file.

Finally, re-run `--status` to confirm the configuration took, and tell the user that hooks only load at session start, so they need to restart Claude Code before events begin flowing.
