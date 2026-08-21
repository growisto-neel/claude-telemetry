# Install and verify

Two procedures. [Part A](#part-a--clean-teardown-and-negative-test) is for whoever owns the analytics property and only needs doing once, to prove the plugin is the only thing sending data. [Part B](#part-b--the-sequence-to-send-to-the-team) is the sequence to send everyone else.

---

## Prerequisites

Check these before touching Claude Code. Every one of them fails silently if it's missing — the plugin installs fine and simply never sends anything.

**Access to the repo.** `growisto-neel/claude-telemetry` is private. `/plugin marketplace add` clones it over your existing git credentials, so you need to already be able to reach it:

```bash
git ls-remote https://github.com/growisto-neel/claude-telemetry >/dev/null && echo ok
```

If that prompts for a password or 404s, stop and get access first. A 404 on a private repo means "no access", not "no repo".

**No runtime to install.** The hook is a static binary, built for each platform and committed to this repo, so there is nothing to install and nothing that can be missing. This was not always true: earlier versions were a Python script, and on a machine without Python 3.8+ on PATH they installed cleanly and then recorded nothing at all. That failure mode is gone.

**Git Bash, on Windows only.** Claude Code runs hook commands through Git Bash when it's present and PowerShell when it isn't, and the launcher that picks the right binary is a bash script. This is not an extra requirement — Git for Windows ships Git Bash, and you already have Git for Windows if the repo check above passed.

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

You want `enabled: True`, your email, `prompt capture: preview`, a `G-` ID, and `pending events: 0`.

If it reports **not configured**, the values from B2 didn't reach the hook. The command will offer to write `config.json` itself — say yes, then restart Claude Code again and re-run it. Tell Neel it happened; it means the plugin needs fixing for everyone, not just you.

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

**No log and no data directory** — the hook never ran. The session predates the install; quit Claude Code and reopen it.

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
