# Testing this on one machine, from an empty Google account

This is the walkthrough for a first run on your own laptop: prove the code works offline, stand up a real GA4 property, install in GA4-direct mode, and watch your own prompts arrive in a dashboard. No GCP project, no billing, no Cloud Run, no BigQuery.

It covers Linux, macOS, and native Windows. Where the commands differ, both are given — pick the one for your machine and ignore the other. Windows here means real Windows PowerShell, not WSL; if you are in WSL, follow the Linux commands, because as far as this code is concerned WSL *is* Linux.

Roughly 15 minutes of your time plus a wait for GA4 to catch up.

At the end you will have a GA4 property receiving one event per prompt from one machine, and a clean uninstall path. Nothing in here touches anyone else's laptop.

---

## Step 0 — Prerequisites

**Linux / macOS**

```bash
python3 --version        # need 3.8 or newer; Ubuntu 22.04+ ships 3.10+
git --version
which claude             # Claude Code must be installed for the end-to-end part
```

```bash
cd ~/Projects/claude-project/qh-claude-telemetry
chmod +x install.sh uninstall.sh selftest.sh
```

**Windows**

```powershell
py -3 --version          # need 3.8 or newer
git --version
Get-Command claude       # Claude Code must be installed for the end-to-end part
cd $HOME\Projects\claude-project\qh-claude-telemetry
```

There is no `chmod` step on Windows; the execute bit doesn't exist there. What does get in the way is the execution policy, which blocks downloaded `.ps1` files by default. Rather than changing a machine-wide setting, run each script with a per-invocation override:

```powershell
powershell -ExecutionPolicy Bypass -File .\selftest.ps1
```

If `py -3 --version` prints nothing useful, or `python` opens the Microsoft Store, you have the Store stub rather than a real interpreter. Install Python with `winget install Python.Python.3.12` and open a new terminal so PATH picks it up.

Everything the hook itself needs is in the Python standard library — no `pip install` at all, on any platform. Steps 1 through 8 stay inside stdlib. The optional local collector in Step 9 is the only part that installs anything.

## Step 1 — Run the self-test before anything else

The self-test is the first thing that should run this code on your machine.

**Linux / macOS**

```bash
./selftest.sh
```

**Windows**

```powershell
powershell -ExecutionPolicy Bypass -File .\selftest.ps1
```

Both wrappers do the same thing: find a Python 3.8+ interpreter and hand off to `selftest.py`, which is the actual suite. There is one suite rather than one per platform, deliberately — a Windows run and a Linux run produce output you can diff line for line, and checks that don't apply print `SKIP` instead of quietly vanishing.

It works in a temp directory, sends no network traffic, and does not touch your real `~/.claude` (`%USERPROFILE%\.claude`) or `~/.qh-claude-telemetry`. It covers 16 areas: syntax across Python, bash, and PowerShell; settings-merge safety; installer idempotency; event capture for all four hook types; noise filtering; secret redaction; capture modes; both opt-out paths; malformed-input resilience; hook latency; GA4 payload limits; crash recovery; duplicate suppression; uninstall; platform-specific behaviour; paths containing spaces; and a full run of the real installer and uninstaller for your platform.

The platform section is the one worth knowing about. On POSIX, the standard way to ask "is process N still alive?" is `os.kill(pid, 0)`. On Windows that same call terminates the process, and Windows recycles PIDs aggressively — a naive port would have had telemetry killing employees' unrelated programs. The suite starts a real child process, runs the liveness probe against it, and asserts the child is still running afterwards.

Read the output rather than just the exit code. If anything fails, stop here — a failure at this stage is a bug in the repo, not a setup problem on your machine, and pushing past it just moves the failure somewhere harder to see. Send me the failing check name.

A clean run is the real green light. Everything after this point is Google configuration.

## Step 2 — Create a GA4 property

Go to [analytics.google.com](https://analytics.google.com) and sign in with whichever Google account should own this. For a personal test your own account is fine; for anything that outlives the test, create it under a company-owned account so it doesn't disappear with your login.

If you've never used Analytics, it walks you through account creation. If you have, use Admin (bottom left gear) → Create → Property.

You'll be asked for a property name (`QH Claude Code Telemetry`), a time zone, and a currency — set the time zone correctly, because it decides where GA4 draws day boundaries in every report. Then it asks for industry category and business size, which affect nothing here; answer anything.

When it offers to create a data stream, choose **Web**. Not iOS, not Android — Web is the only stream type with a Measurement Protocol endpoint you can post to from a script. It asks for a website URL and stream name; put in `https://qualifiedhealthai.com` and `laptops`. The URL is never contacted and never checked. It exists because GA4 assumes you're tagging a website.

Copy the **Measurement ID** it shows you — `G-` followed by ten or so characters. That's the first of the two values you need.

## Step 3 — Create the Measurement Protocol API secret

Admin → Data collection and modification → Data streams → click your stream → scroll to **Measurement Protocol API secrets** → Create. Give it a nickname (`laptop hook`) and copy the secret value.

You need Editor or Administrator on the property to see this option. If the section isn't there, you're a Viewer or an Analyst — that's the reason, not a UI change.

This secret is a write credential: anyone holding it can inject arbitrary events into this property. On your own test machine that's fine. It's the main argument for collector mode when you roll out company-wide.

## Step 4 — Register the custom dimensions

This step is easy to skip and expensive to skip. GA4 accepts and stores your custom parameters immediately, but will not show them in any report until you register each one by name. Registration is **not retroactive** — events that arrive before you register a dimension keep that value out of reports permanently. Do it now, before you send anything real.

Admin → Data display → Custom definitions → Create custom dimensions. For each one below: set **Scope = Event**, then set **Event parameter** to exactly the name given, and use the same string as the dimension name so you can find it later.

```
user_email      team            cc_session_id   folder_name     folder_path
repo            skill           tool_name       model           session_source
permission_mode prompt_preview  prompt_hash     os              hook_version
```

Then the Custom metrics tab → Create custom metrics, twice, Scope = Event, Unit of measurement = Standard:

```
prompt_chars    prompt_words
```

Fifteen dimensions out of your 50 event-scoped allowance, two metrics out of 50. There is no bulk import in the UI; it's fifteen trips through the same dialog. Copy-paste the parameter names rather than typing them — a typo produces a dimension that is permanently empty and gives no error.

## Step 5 — Install in GA4-direct mode

**Linux / macOS**

```bash
./install.sh \
  --ga4-measurement-id G-XXXXXXXXXX \
  --ga4-api-secret YOUR_SECRET \
  --team platform
```

**Windows**

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 `
  -Ga4MeasurementId G-XXXXXXXXXX `
  -Ga4ApiSecret YOUR_SECRET `
  -Team platform
```

It prints exactly what will be collected and waits for confirmation — the same words on every platform, because an employee's understanding of what is collected should not depend on which operating system they happen to use. Then it reads your email from `git config user.email` and lets you correct it, copies the hook to `~/.qh-claude-telemetry/` (`%USERPROFILE%\.qh-claude-telemetry\`), writes `config.json` with owner-only permissions, and merges four hook entries into `~/.claude/settings.json` — backing up the existing file with a timestamp first and leaving any hooks you already had alone.

The two installers are thin front ends over `tools\qh_configure.py`, which owns both operations that can damage a file you care about. There is one implementation of that logic rather than one per platform, for the same reason there is one self-test.

It's safe to re-run. It strips its own previous entries each time, so re-running never double-counts events.

On Windows, the permission story is worth one sentence: `chmod` there only toggles the read-only bit and does nothing about who can read a file, so the installer uses `icacls` to grant the current user alone access to `config.json`. That file holds your GA4 api_secret, which is a write credential for the property.

## Step 6 — Confirm the local install

**Linux / macOS**

```bash
python3 ~/.qh-claude-telemetry/qh_telemetry_hook.py --status
```

**Windows**

```powershell
py -3 $env:USERPROFILE\.qh-claude-telemetry\qh_telemetry_hook.py --status
```

You want `enabled: True`, your email with a plausible source, `prompt capture: preview`, your `G-` ID under `ga4 direct`, and `pending events: 0`. On Windows also glance at the `platform` and `python` lines — they should say `win32` and the interpreter the installer recorded, which is the one baked into the hook command in `settings.json`.

Then, same file, `--test` instead of `--status`:

```bash
python3 ~/.qh-claude-telemetry/qh_telemetry_hook.py --test
```

This prints the exact event that would be recorded, prints the GA4 payload, then posts to GA4's `/debug/mp/collect` endpoint. Two things to understand about the response:

The debug endpoint validates the payload and returns `{"validationMessages": []}` on success. An empty message list is the pass. It **does not record the event** — nothing from `--test` will ever appear in a report, so don't go looking for it.

A `2xx` status with validation messages inside is still a failure. Read the messages; a wrong measurement ID or secret shows up here rather than as an HTTP error.

Look at the printed event while it's in front of you. Confirm `prompt_preview` is 44 characters of the synthetic prompt and that there is no `prompt` field anywhere in the JSON. That's the design claim, checked with your own eyes on your own machine.

## Step 7 — Send one real event

`--test` deliberately doesn't record, so send one event through the actual hook path:

**Linux / macOS**

```bash
echo '{"hook_event_name":"UserPromptSubmit","session_id":"manual-1","cwd":"'"$PWD"'","prompt":"first real telemetry event, checking that the preview cuts at one hundred characters exactly here"}' \
  | python3 ~/.qh-claude-telemetry/qh_telemetry_hook.py
```

**Windows**

```powershell
$payload = @{
    hook_event_name = "UserPromptSubmit"
    session_id      = "manual-1"
    cwd             = (Get-Location).Path
    prompt          = "first real telemetry event from windows, checking that the preview cuts at one hundred characters exactly here"
} | ConvertTo-Json -Compress

$payload | py -3 $env:USERPROFILE\.qh-claude-telemetry\qh_telemetry_hook.py
```

Build the JSON with `ConvertTo-Json` rather than typing a quoted literal. PowerShell's own quoting rules and the backslashes in a Windows path fight each other, and a hand-written literal is the single most likely thing to fail here for reasons that have nothing to do with the hook.

No output means success — the hook is silent by design, because on `UserPromptSubmit` anything it prints on stdout gets injected into Claude's context.

**Linux / macOS**

```bash
sleep 5
python3 ~/.qh-claude-telemetry/qh_telemetry_hook.py --status   # pending events: 0
cat ~/.qh-claude-telemetry/telemetry.log                       # should not exist, or be empty
```

**Windows**

```powershell
Start-Sleep 8
py -3 $env:USERPROFILE\.qh-claude-telemetry\qh_telemetry_hook.py --status
Get-Content $env:USERPROFILE\.qh-claude-telemetry\telemetry.log -ErrorAction SilentlyContinue
```

`pending events: 0` means the background sender ran and GA4 accepted it. A non-zero count with an empty log means the sender hasn't finished yet; wait and check again. Give Windows a little longer than Linux — process creation there costs roughly twice as much, which is also why the self-test allows a 900ms hook budget on Windows against 400ms elsewhere. A non-zero count with entries in the log means the send failed; the log line has the status and response body.

## Step 8 — End to end through Claude Code

Hooks only load when a session starts, so your currently-running Claude Code sessions will not report anything. Exit them and start a fresh one.

```bash
cd ~/Projects/claude-project
claude
```

Send two or three prompts, invoke a skill, and exit. That should produce `cc_session_start`, a `cc_prompt` per prompt, a `cc_skill` per skill or subagent invocation, and `cc_session_end`.

Then in GA4: **Reports → Realtime**. Allow a few minutes; Measurement Protocol events are not instant. You're looking for your event names in the "Event count by Event name" card. Clicking an event name opens its parameters, which is the fastest way to confirm `prompt_preview` and `skill` are actually arriving with values.

**DebugView will be empty, and that's expected.** GA4 only routes Measurement Protocol events to DebugView if each event carries `debug_mode: 1` in its params, and the hook doesn't send it. Use Realtime instead. If you specifically want DebugView, add `"debug_mode": 1,` immediately after `"engagement_time_msec": 1,` in `to_ga4_payload` in `~/.qh-claude-telemetry/qh_telemetry_hook.py`, and take it back out afterwards — leaving it on routes production traffic into a debug stream and distorts your reports.

Standard reports and the Explore tool lag by up to 24–48 hours on a new property, and your registered custom dimensions need that long to start populating. Realtime is the only same-day view. Don't conclude anything is broken on day one because Explore looks empty.

## Step 9 — Optional: run the collector locally

Skip this unless you want to see the collector path work. It changes nothing about what's collected and it isn't needed for a GA4 pilot. The commands below are Linux/macOS; on Windows the only differences are `py -3 -m venv .venv` and `.venv\Scripts\Activate.ps1` to activate, then `$env:INGEST_TOKEN = "localtest"` instead of `export`, and `.\install.ps1 -CollectorUrl ... -CollectorToken localtest -Team platform`.

Ubuntu 24.04 blocks system-wide `pip install` (PEP 668), so use a virtualenv:

```bash
cd ~/Projects/claude-project/qh-claude-telemetry/collector
python3 -m venv .venv && source .venv/bin/activate
pip install fastapi uvicorn httpx pydantic
```

Run it with no BigQuery configured, which puts it in logs-only mode — events are printed as structured log lines instead of being written to a warehouse:

```bash
export INGEST_TOKEN=localtest
export GA4_MEASUREMENT_ID=G-XXXXXXXXXX
export GA4_API_SECRET=YOUR_SECRET
uvicorn main:app --port 8080
```

In another terminal, re-point the hook at it. Note there is no `--ga4-*` flag this time: that's the whole point, the secret now lives only in the collector's environment.

```bash
cd ~/Projects/claude-project/qh-claude-telemetry
./install.sh --collector-url http://127.0.0.1:8080/v1/events \
             --collector-token localtest --team platform

curl -s localhost:8080/healthz    # expect warehouse: "logs-only"
python3 ~/.qh-claude-telemetry/qh_telemetry_hook.py --test
```

The collector should log a `cc_event {...}` line and forward to GA4, and `--test` should print `ok`. Adding BigQuery from here means a GCP project with billing enabled, which is out of scope for this guide — the deployment steps are in `README.md`.

## Troubleshooting

**`--status` shows `config: MISSING`.** The installer didn't complete, or you're running the copy in the repo rather than the installed copy at `~/.qh-claude-telemetry/` (`%USERPROFILE%\.qh-claude-telemetry\`). Use the full path.

**`--status` shows `enabled: False`.** `QH_TELEMETRY=0` is set, or a `DISABLED` file exists in the data directory. On Linux/macOS, `unset QH_TELEMETRY` and `rm` the file. On Windows, `setx QH_TELEMETRY 1` (or delete the variable in System → Environment Variables) and `Remove-Item $env:USERPROFILE\.qh-claude-telemetry\DISABLED` — remembering that `setx` only affects terminals opened afterwards, never the one you typed it in.

**Windows: `.\selftest.ps1` refuses to run, "running scripts is disabled on this system".** That's the execution policy, not a problem with the script. Use `powershell -ExecutionPolicy Bypass -File .\selftest.ps1`, which overrides it for that one invocation and leaves the machine's setting alone.

**Windows: the installer says Python was not found, but you have Python.** You most likely have the Microsoft Store stub, which prints an advert and exits non-zero. `winget install Python.Python.3.12`, then open a new terminal. The installer probes `py -3`, then `python3`, then `python`, and records the real `sys.executable` rather than `py -3`, because the launcher may not be on PATH for whichever process ends up running the hook.

**Windows: `--status` is fine but Claude Code produces no events.** Look at `%USERPROFILE%\.claude\settings.json` and check the recorded interpreter path still exists — a Python upgrade that moves the install directory will break a command string written before it. Re-run `install.ps1` to re-record it.

**`--test` returns validation messages about the measurement ID.** The ID and secret must come from the same data stream. Re-copy both from Admin → Data streams rather than assuming.

**Events accepted but reports stay empty.** Almost always Step 4 not done, or done with a typo, or done after the events arrived. Registration isn't retroactive — send fresh events after fixing the dimension names.

**`pending events` climbing and nothing arriving.** Read `~/.qh-claude-telemetry/telemetry.log`. A `403` is a bad api_secret; a network error is a proxy or firewall between you and `google-analytics.com`.

**Claude Code sends prompts but no events appear.** Confirm the hooks landed. On Linux/macOS, `python3 -c "import json;print(json.load(open('$HOME/.claude/settings.json'))['hooks'].keys())"`; on Windows, `py -3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude/settings.json')))['hooks'].keys())"`. Either should list `UserPromptSubmit`, `SessionStart`, `SessionEnd`, `PreToolUse`. If they're there, you're in a session that started before installation; restart it.

**Skill events missing but prompt events fine.** Skills are captured through `PreToolUse` filtered to the `Skill`, `Task`, and `SlashCommand` tools. A skill that Claude loaded without a tool call won't produce one.

## Clean up

**Linux / macOS**

```bash
./uninstall.sh --purge
```

**Windows**

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -Purge
```

Removes the hook entries from `settings.json` and deletes the data directory including the spool. Drop the purge flag to remove the hooks but keep the local data for inspection. Either way, any hooks you configured yourself are left exactly as they were, and `settings.json` is backed up before it's touched.

Both uninstallers fall back to the copy of `qh_configure.py` that the installer left beside the hook, so removal still works if you've since deleted or moved the checkout you installed from. If even that is gone, they print the two things to delete by hand rather than leaving you stuck with telemetry you can't switch off.

The GA4 property is separate — delete it in Admin → Property settings → Property details → Move to trash if this was a throwaway. It sits in the trash for 35 days before it's actually gone.

## Once this passes

The Linux path has been run end to end against a real GA4 property. The Windows path has not been executed anywhere yet — it was written against the documented behaviour of `os.kill`, `os.open`, `icacls`, and PowerShell 5.1, and reviewed carefully, but reading code is not running it. Treat the first Windows run of `selftest.ps1` as the thing that decides whether the Windows support is real, and the first Windows install as a test rather than a rollout.

Then two decisions before anyone else runs the installer. Get an answer on `user_email` in GA4, which is PII under Google's terms and has `user_email_sha256` as the compliant alternative. And send the notice in `PRIVACY_NOTICE.md` to the team — it's written to be edited, and the bracketed parts need real answers before it goes out.
