# Install and verify

Two procedures. [Part A](#part-a--clean-teardown-and-negative-test) is for whoever owns the analytics property and only needs doing once, to prove the plugin is the only thing sending data. [Part B](#part-b--the-sequence-to-send-to-the-team) is the sequence to send everyone else.

---

## Prerequisites

Check these before touching Claude Code. Every one of them fails silently if it's missing — the plugin installs fine and simply never sends anything.

**git, and access to the repo.** `/plugin marketplace add` clones the repository, so git has to be installed and has to be able to reach a private repo. One command checks both:

```bash
git ls-remote https://github.com/growisto-neel/claude-telemetry >/dev/null && echo ok
```

`ok` means you're fine. "command not found" means install git first. A password prompt or a 404 means you don't have access — on a private repo a 404 means "no access", not "no repo" — so stop and get it before going further.

On Windows, install [Git for Windows](https://git-scm.com/download/win) with the default options. That covers this prerequisite and the next one at the same time.

**No runtime to install.** The hook is a static binary, built for each platform and committed to this repo, so there is nothing to install and nothing that can be missing. This was not always true: earlier versions were a Python script, and on a machine without Python 3.8+ on PATH they installed cleanly and then recorded nothing at all. That failure mode is gone.

**bash, which on Windows means Git Bash.** The launcher that picks the right binary for your machine is a shell script, and Claude Code runs hook commands through Git Bash on Windows. This is not an extra requirement on top of the one above: Git for Windows installs Git Bash as part of a default install, so anyone who can install the plugin already has it.

Worth knowing what the failure looks like anyway, because it is completely silent. If `bash` cannot be resolved, the hook process never starts — so nothing is written, not even `telemetry.log`, and `/growisto-telemetry` has nothing to report. It is the one failure the plugin cannot diagnose about itself, because the code that writes the diagnostics is the code that didn't run.

If you installed git some other way — a bare `git.exe`, or a package that skips Git Bash — check in Command Prompt:

```
where bash
```

If that prints only `C:\Windows\System32\bash.exe`, that is the WSL launcher, not a shell, and it will fail on a machine without WSL. Install Git for Windows with the default options.

**Network to `google-analytics.com`.** A corporate proxy that intercepts outbound HTTPS will show up as connection errors in `telemetry.log` and a spool that never drains.

**Credentials.** The GA4 measurement ID (`G-…`) and API secret, from Neel. Don't paste the secret into a group chat — it's a write credential for the analytics property.

---

## Part A — clean teardown and negative test

Only for the person verifying the pipeline. The point is to reach a state where the plugin is provably the only thing capturing anything, so that when data appears afterwards you know where it came from.

### A1. Remove the plugin and the marketplace

```
/plugin uninstall growisto-claude-telemetry@growisto
/plugin marketplace remove growisto
```

Removing the marketplace uninstalls anything installed from it, so the second command alone would do — running both makes the intent explicit.

### A2. Quit Claude Code completely

Not a new tab, not `/clear`. Hooks are loaded once at session start and stay loaded, so a running session keeps firing hooks from a plugin you just uninstalled.

### A3. Remove local state

```bash
rm -rf ~/.growisto-claude-telemetry ~/.qh-claude-telemetry
```

The second path is the pre-rename directory. It's harmless if it's already gone.

### A4. Check for hooks left behind by the old installer

This is the step that actually matters, and it's the one that causes double-counting if skipped. Before the plugin existed, `install.sh` wired hooks directly into your settings files. Those entries survive a plugin uninstall because the plugin never put them there.

```bash
grep -rn -e telemetry -e qh- -e growisto- \
  ~/.claude/settings.json ~/.claude/settings.local.json .claude/settings.json 2>/dev/null
```

Any hit under a `"hooks"` key is a leftover — edit the file and delete that entry. Expect no output on a clean machine.

The hook now suppresses an identical event seen twice within five seconds, so a leftover entry no longer doubles anybody's numbers. Delete it anyway: the suppression is a safety net, not a reason to leave two things wired to the same event.

```bash
ls ~/.claude/plugins/ 2>/dev/null
```

Should list no growisto or qh telemetry directory. Leave anything else alone.

### A5. Prove nothing is being captured

Start Claude Code, send three or four ordinary prompts, invoke a skill, then:

```bash
ls ~/.growisto-claude-telemetry 2>/dev/null || echo "nothing captured — correct"
```

The data directory is created on the first event, so its absence is the proof. This is a better signal than watching GA4, which lags and which colleagues may also be writing to.

If the directory *does* reappear, something is still wired up — go back to A4.

---

## Part B — the sequence to send to the team

### B1. Check the prerequisites above

Particularly repo access — it's the one remaining quiet failure.

### B2. Add the marketplace and install

```
/plugin marketplace add growisto-neel/claude-telemetry
/plugin install growisto-claude-telemetry@growisto
```

You'll be prompted for the GA4 measurement ID, the API secret, your work email, your team, and two privacy settings. Email can be left blank to use your `git config user.email`; team is optional; leave both privacy settings blank unless you have a reason not to.

### B3. Restart Claude Code

Quit and reopen. Hooks only load at session start, so nothing is recorded until you do.

### B4. Verify

```
/growisto-telemetry
```

You want `hook version: 2.1.0`, `enabled: true`, your email, `prompt capture: preview`, a `G-` ID, `ga4 secret: set`, and `pending events: 0`.

Check `ga4 secret` specifically. A measurement ID with no secret is the worst state to be in, because the Measurement Protocol answers `2xx` to a request it then discards — everything looks like it's working and no data ever appears.

If it reports **not configured**, the values from B2 didn't reach the hook. The `plugin options` line says which ones the hook could actually see, and that is the useful detail: `(none visible)` means Claude Code exported nothing, while a list that doesn't include `ga4measurementid` means they arrived under names the hook doesn't recognise. The command will offer to write `config.json` itself — say yes, then restart Claude Code again and re-run it. Either way tell Neel, because the second case is a bug affecting everyone rather than something about your machine.

If it prints **nothing at all** and there's no data directory, the hook process never started. That's the `bash` prerequisite above, not anything you did. Send Neel the output of `where bash`.

If it reports **no binary for this platform**, the repo carries no build for your OS and CPU. Send Neel the `uname -s` and `uname -m` values from the log line; the fix is one more target in `build.sh`, not anything you can do locally.

### B5. Confirm data is arriving

Send a few prompts and invoke a skill. Then, in GA4, **Reports → Realtime**, and look for `cc_prompt`, `cc_session_start`, and `cc_skill` in the event-count card. Allow a few minutes — Measurement Protocol events are not instant, and DebugView will stay empty by design.

Standard reports and Explore lag 24–48 hours on a new property. Don't conclude anything is broken on day one because Explore looks empty.

---

## When nothing arrives

Read the log first. It is written to answer exactly this question.

```bash
tail -20 ~/.growisto-claude-telemetry/telemetry.log
wc -l ~/.growisto-claude-telemetry/spool.ndjson
```

**A line saying `no GA4 destination`** — the install-time values never reached the hook. That same line lists the `CLAUDE_PLUGIN_OPTION_*` variables the hook could actually see, which tells you whether Claude Code passed anything at all. Fix with `/growisto-telemetry`.

**A growing spool and `403`** — wrong API secret.

**A growing spool and a connection error** — proxy or firewall between you and `google-analytics.com`.

**No log and no data directory** — the hook never ran. Usually the session predates the install, so quit Claude Code and reopen it. If it persists after a restart, `bash` isn't resolving and the hook process is never being started at all; on Windows check `where bash` and see the prerequisites.

**A `NO_BINARY` file in the data directory** — the repo has no build for your OS and CPU. The log line names both, and `/growisto-telemetry` reports it.

**Events accepted but reports empty** — the custom dimensions in [TESTING.md](TESTING.md) step 4 aren't registered, or were registered after the events arrived. Registration is not retroactive; fix the names and send fresh events.

---

## Opting out

Anyone can, without telling anyone or explaining why.

```bash
export GROWISTO_TELEMETRY=0     # Linux / macOS, add to ~/.bashrc or ~/.zshrc
setx GROWISTO_TELEMETRY 0       # Windows, then open a new terminal
```

To remove it entirely: `/plugin uninstall growisto-claude-telemetry@growisto`.

Send [PRIVACY_NOTICE.md](PRIVACY_NOTICE.md) before Part B, not after. It explains what's collected, and it has bracketed sections that need real answers first.
