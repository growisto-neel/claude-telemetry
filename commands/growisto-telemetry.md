---
description: Check Growisto telemetry status, or finish configuring it if credentials did not carry over from install
---

Report the state of the Growisto Claude Code telemetry plugin for this user, and repair its configuration if it is incomplete.

## 1. Report status

Run the hook's own status command and show the user the output verbatim:

```
bash "${CLAUDE_PLUGIN_ROOT}"/bin/growisto-hook --status
```

If that produces nothing useful, fall back to invoking the Python directly, trying `python3`, then `python`, then `py -3`:

```
python3 "${CLAUDE_PLUGIN_ROOT}"/hooks/growisto_telemetry_hook.py --status
```

The lines that matter are `enabled`, the resolved email and where it came from, `prompt capture`, whether a GA4 destination is configured, and `pending events`.

## 2. Diagnose

Read the status output and tell the user plainly which of these they are in:

**Working.** `enabled: True`, a `G-` ID present, `pending events: 0`. Say so and stop. Do not change anything.

**No Python.** The launcher reported no interpreter, or a `NO_PYTHON` marker exists in the data directory. Telemetry is inactive on this machine and no amount of configuration will change that. Tell them to install Python 3.8+ (`winget install Python.Python.3.12` on Windows, `brew install python` on macOS, already present on Linux), then open a new terminal and start a fresh Claude Code session. Do not attempt to install it for them.

**Configured but not sending.** A `G-` ID is present and `pending events` is above zero. Read the last few lines of `telemetry.log` in the data directory and report what the send actually failed with — a 403 is a wrong API secret, a connection error is a proxy or firewall.

**Not configured.** No GA4 destination. A `NO_DEST` marker in the data directory and a `no GA4 destination` line in `telemetry.log` confirm it; that log line also names the `CLAUDE_PLUGIN_OPTION_*` variables the hook could see, which is what tells you whether the install-time values reached it at all. This is the case step 3 handles.

**Opted out.** `enabled: False`. Say so and leave it alone. Do not re-enable telemetry for someone who turned it off, and do not ask them why.

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

Create the directory first if it does not exist. On Linux and macOS, `chmod 600` the file afterwards. On Windows, run `icacls` to grant the current user alone access. That file holds a credential that can write arbitrary events into the analytics property, so it must not be group- or world-readable.

Never echo the API secret back into the conversation after writing it, and never write it anywhere other than that one file.

Finally, re-run `--status` to confirm the configuration took, and tell the user that hooks only load at session start, so they need to restart Claude Code before events begin flowing.
