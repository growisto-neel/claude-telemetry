# QH Claude Code Telemetry

Tracks Claude Code usage across every employee's machine — who prompted, the first 100 characters of what they prompted, which skill fired, and which folder they were in — and lands it in GA4, optionally also in a warehouse you control.

Employees run one script. Hooks do the rest.

Runs natively on Linux, macOS, and Windows. Python 3.8+ and nothing else — no third-party packages, no WSL, no admin rights.

---

## Read this before you deploy

**No full prompt text is stored anywhere.** This is the central design decision and it shapes everything else. The most that is ever written or transmitted is the first 100 characters of a prompt, scrubbed of common secret shapes, alongside `prompt_chars` and `prompt_words` so you can still see that the real prompt was longer. There is no full-text mode, no full-text column in BigQuery, and no code path that sends one. 100 characters happens to be exactly GA4's per-parameter limit, so nothing is silently truncated in transit either — what you see in GA4 is the whole record.

**The collector is optional.** Because every field fits inside GA4's limits, GA4 direct mode is a complete deployment. Run the collector only if you want the api_secret off employee laptops, or SQL over the raw events. Start without it.

**The GA4 API secret is a write credential.** In direct mode it sits in a plaintext config file on every laptop. Anyone who reads it can inject arbitrary events into your property and corrupt your reporting. That's an acceptable risk for a pilot on machines you trust and a reason to move to collector mode for a company-wide rollout.

**One compliance question remains.** Qualified Health is a healthcare company and a prompt is free text, so even 100 characters can contain a fragment of a patient record or customer data. That's a much smaller exposure than full prompts but not zero, and GA4 is not covered by a Google BAA. Separately, GA4 receives `user_email`, which is PII that Google's terms prohibit sending. Both are worth ten minutes with whoever owns compliance before this goes company-wide. `--prompt-capture hash` drops prompt text entirely while keeping length, word count, and a hash, and `user_email_sha256` is available if you'd rather dashboards operate on a hash.

---

## Architecture

**Direct mode — start here**

```
Claude Code session
  └─ hook (UserPromptSubmit / SessionStart / SessionEnd / PreToolUse)
       └─ local spool file          ← instant, survives offline
            └─ detached sender
                 └─ GA4 Measurement Protocol
```

**Collector mode — optional, for company-wide rollout**

```
hook → local spool → detached sender → QH collector (Cloud Run)
                                            ├─ BigQuery  ← SQL, retention policy
                                            └─ GA4       ← same 100-char record
```

Both modes carry identical data. The collector adds SQL access and keeps the GA4 api_secret server-side; it does not unlock any additional field.

The hook never blocks. It parses stdin, appends one line to a spool file, spawns a detached sender, and exits 0. Every code path is wrapped so a network outage, an expired token, or a bug in this repo cannot slow down or break anyone's Claude Code session. Failed sends stay spooled and retry on the next event, so a laptop on a plane doesn't lose a day of data.

---

## What gets collected

| Field | Example | Goes to GA4 | Goes to warehouse |
|---|---|---|---|
| `user_email` | `neel.thakkar@qualifiedhealthai.com` | yes | yes |
| `user_email_sha256` | `9f2c…` | no | yes |
| `team` | `platform` | yes | yes |
| `prompt_preview` | first 100 chars, secrets scrubbed | yes | yes |
| `prompt_chars` / `prompt_words` | `412` / `68` | yes | yes |
| `prompt_sha256` | dedupe / repeat-prompt analysis | first 16 chars | yes |
| `skill` | `qh-prototypes:create-backend` | yes | yes |
| `tool_name` | `Skill`, `Task`, `SlashCommand` | yes | yes |
| `folder_path` | `/Users/neel/src/qh-platform` | last 100 chars | yes |
| `folder_name` / `repo` | `qh-platform` | yes | yes |
| `cc_session_id` | Claude session UUID | yes | yes |
| `session_source` | `startup` / `resume` / `clear` / `compact` / `fork` | yes | yes |
| `model`, `permission_mode`, `os`, `hook_version` | | yes | yes |

A 412-character prompt is recorded as its first 100 characters plus `prompt_chars: 412`. You can tell that more was said, and you can group repeat prompts by `prompt_sha256`, but the remaining 312 characters are not retained.

Not collected: full prompt text, Claude's responses, file contents, terminal output, keystrokes, diffs, git history, or anything happening outside Claude Code.

Events emitted: `cc_prompt`, `cc_skill`, `cc_session_start`, `cc_session_end`.

Claude Code has no dedicated "skill invoked" hook. Skill and subagent use is captured through `PreToolUse` filtered to the `Skill`, `Task`, and `SlashCommand` tools, with the skill name pulled out of `tool_input`.

---

## GA4 setup

For a first run on one machine, follow `TESTING.md` instead — it walks the whole thing from an empty Google account. This is the short version.

Create a **Web** data stream in your GA4 property: Admin → Data collection and modification → Data streams. Copy the Measurement ID (`G-XXXXXXX`), then open the stream → Measurement Protocol API secrets → Create, and copy that secret.

Custom parameters do not appear in GA4 reports until you register them. Admin → Data display → Custom definitions → Create custom dimension, scope Event, with the event parameter name matching exactly:

`user_email`, `team`, `cc_session_id`, `folder_name`, `folder_path`, `repo`, `skill`, `tool_name`, `model`, `session_source`, `permission_mode`, `prompt_preview`, `prompt_hash`, `os`, `hook_version`

Then two custom metrics: `prompt_chars` and `prompt_words`.

That's 15 of your 50 event-scoped dimensions. Registration is not retroactive — data arriving before you register a dimension is not backfilled into reports, so do this before rollout.

---

## Deploy the collector (optional)

Skip this for a pilot. Add it when you want the api_secret off laptops, or SQL over the events.

```bash
cd collector

# 1. BigQuery sink, partitioned by day
bq mk --table \
  --time_partitioning_field event_time \
  --time_partitioning_expiration 7776000 \
  YOUR_PROJECT:analytics.claude_code_events \
  bigquery_schema.json

# 2. Secrets
printf '%s' "$(openssl rand -hex 32)" | gcloud secrets create cc-telemetry-token --data-file=-
printf '%s' 'YOUR_GA4_API_SECRET'     | gcloud secrets create ga4-api-secret     --data-file=-

# 3. Deploy
gcloud run deploy qh-cc-telemetry \
  --source . --region us-central1 --allow-unauthenticated \
  --set-env-vars GA4_MEASUREMENT_ID=G-XXXXXXX,BQ_DATASET=analytics,BQ_TABLE=claude_code_events \
  --set-secrets GA4_API_SECRET=ga4-api-secret:latest,INGEST_TOKEN=cc-telemetry-token:latest
```

`--allow-unauthenticated` is deliberate: laptops can't hold GCP service-account credentials, so the service is public at the network layer and gates on the bearer token instead. There is no full-text column to protect, but `user_email` and `prompt_preview` are still worth restricting with BigQuery column-level access control so they aren't readable by everyone with dataset access.

The `--time_partitioning_expiration 7776000` above is a 90-day retention window. Set it to whatever your data retention policy actually says.

Once BigQuery is in place, prefer SQL over the GA4 UI for anything analytically interesting. Enabling GA4's native BigQuery export as well is reasonable and now loses nothing, since GA4 holds the complete record.

---

## Roll out to employees

Distribute the repo (internal git, or an installer on a signed internal URL) and have each person run the installer for their platform. The two installers take the same options, print the same disclosure, and produce the same result.

**macOS / Linux**

```bash
./install.sh \
  --ga4-measurement-id G-XXXXXXX \
  --ga4-api-secret SECRET \
  --team platform
```

**Windows** (PowerShell, no admin rights needed)

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 `
  -Ga4MeasurementId G-XXXXXXX `
  -Ga4ApiSecret SECRET `
  -Team platform
```

`-ExecutionPolicy Bypass` is there because the default Windows policy refuses to run unsigned local scripts. If you sign the scripts as part of your internal distribution, drop it.

The installer prints exactly what is collected and asks for confirmation before touching anything. It resolves the employee's email from `git config user.email` and lets them correct it, then merges the hooks into `~/.claude/settings.json` (`%USERPROFILE%\.claude\settings.json` on Windows) — backing up the existing file first, preserving any hooks already configured, and stripping its own previous entries so re-running never double-counts events.

Both installers are thin front ends over `tools/qh_configure.py`, which owns the config write and the settings merge. That is deliberate: the merge is the one operation that can damage a file the employee cares about, and a second copy of it written in PowerShell would be the copy nobody tested.

If you deployed the collector, point at it instead — the api_secret then stays server-side and nothing sensitive lands in the laptop config:

```bash
./install.sh \
  --collector-url https://qh-cc-telemetry-xxxx.run.app/v1/events \
  --collector-token "$QH_CC_TELEMETRY_TOKEN" \
  --team platform
```

```powershell
.\install.ps1 `
  -CollectorUrl https://qh-cc-telemetry-xxxx.run.app/v1/events `
  -CollectorToken $env:QH_CC_TELEMETRY_TOKEN `
  -Team platform
```

Scripted or MDM rollout — add `--non-interactive` / `-NonInteractive`, and put the employee notice in the deployment ticket instead:

```bash
./install.sh --non-interactive --ga4-measurement-id G-XXXXXXX --ga4-api-secret SECRET \
  --email "$USER_EMAIL" --team "$TEAM"
```

```powershell
.\install.ps1 -NonInteractive -Ga4MeasurementId G-XXXXXXX -Ga4ApiSecret SECRET `
  -Email $env:USER_EMAIL -Team $env:TEAM
```

Neither installer ever puts a secret on a command line of its own: values are handed to `qh_configure.py` through the environment, because the argument list of a running process is readable by other accounts on both Windows and Linux. Your own invocation above is still visible in shell history, so prefer an environment variable there too.

For guaranteed coverage that users can't remove, write the same `hooks` block into managed settings instead: `/Library/Application Support/ClaudeCode/managed-settings.json` on macOS, `/etc/claude-code/managed-settings.json` on Linux, `C:\ProgramData\ClaudeCode\managed-settings.json` on Windows. That removes the opt-out, which raises the disclosure bar — see `PRIVACY_NOTICE.md`.

### Windows notes

The hook command written into `settings.json` is a fully-quoted absolute path pair — `"C:\Program Files\Python312\python.exe" "C:\Users\Firstname Lastname\.qh-claude-telemetry\qh_telemetry_hook.py"` — because both halves normally contain spaces and an unquoted command fails silently at hook time.

The installer resolves the interpreter through the `py` launcher when it can, then records `sys.executable` rather than `py -3`, so the hook keeps working for any process that runs it regardless of PATH. It rejects the Microsoft Store `python` stub automatically.

`chmod 600` does nothing useful on Windows — the mode bits only toggle the read-only attribute. On Windows the installer runs `icacls` to drop inherited permissions and grant the current user alone, which is the real equivalent for `config.json` in direct mode. `--status` reports which of the two was applied.

WSL counts as Linux, not Windows. If someone runs Claude Code inside WSL, install with `install.sh` from inside WSL; a native Windows install will not see those sessions, and vice versa. Someone who uses both needs both, and will show up as two installs with two `client_id`s and one email.

Privacy dials, if you want less than the default:

```bash
--prompt-capture preview|hash    # 100 scrubbed chars (default) | length + hash, no text
--path-capture   full|basename|none    # full path | folder name only | nothing
```

There is no full-text mode. `preview` is the most permissive setting the installer accepts.

On Windows the same dials are `-PromptCapture` and `-PathCapture`, with the same accepted values.

Both dials also read from the environment (`QH_TELEMETRY_PROMPT_CAPTURE`, `QH_TELEMETRY_PATH_CAPTURE`) so you can tighten a machine without rewriting its config. Note that `repo` is resolved by running git against `folder_path`, so it only populates in `full` path mode — under `basename` or `none` the hook has no path to hand to git, and deliberately doesn't keep a private copy of one. `basename` keeps `folder_name`; `none` suppresses `folder_path`, `folder_name`, and `repo` alike, on the grounds that a bare directory name is still a location.

---

## Verify and operate

**Run the self-test first, on every platform you plan to ship to.**

```bash
./selftest.sh                                       # macOS / Linux
```

```powershell
powershell -ExecutionPolicy Bypass -File .\selftest.ps1    # Windows
```

Both are thin wrappers around `selftest.py`, which is the real suite — one file, so a Windows run and a Linux run produce output you can diff line for line. Checks that only make sense on one platform print `SKIP` on the others rather than vanishing.

It runs in a temp directory with no network traffic and no changes to your real `~/.claude`, and covers: syntax of every script including a PowerShell parse of the `.ps1` files, settings-merge safety, installer idempotency, event capture for all four hook types, noise filtering, secret redaction, all capture modes, both opt-out paths, malformed-input resilience, hook latency, GA4 payload limits, crash recovery, duplicate suppression, uninstall, platform behaviour, installation under a path containing spaces, and a full run of the platform's own installer and uninstaller. Treat a clean run as the real green light.

The platform section is the one worth understanding, because it guards a genuinely dangerous difference. On Windows, `os.kill(pid, 0)` — the ordinary POSIX way to ask "is this process alive?" — calls `TerminateProcess` instead. Windows also recycles PIDs aggressively, so a stale lock file can name a process that has nothing to do with telemetry. Telemetry that kills unrelated processes on employee laptops would be an unrecoverable trust failure, so the suite starts a real child process, asks the liveness probe about it, and asserts the child is still running afterwards.

```bash
python3 ~/.qh-claude-telemetry/qh_telemetry_hook.py --status   # config, identity, pending count
python3 ~/.qh-claude-telemetry/qh_telemetry_hook.py --test     # dry-run event + GA4 debug validation
cat ~/.qh-claude-telemetry/telemetry.log                       # local send errors
```

```powershell
py -3 $env:USERPROFILE\.qh-claude-telemetry\qh_telemetry_hook.py --status
py -3 $env:USERPROFILE\.qh-claude-telemetry\qh_telemetry_hook.py --test
Get-Content $env:USERPROFILE\.qh-claude-telemetry\telemetry.log
```

`--status` opens with the platform and the exact interpreter in use, which is the first thing to check when a Windows machine reports nothing.

`--test` posts to GA4's `/debug/mp/collect` endpoint, which validates the payload and returns schema errors without recording anything. Use it to confirm your measurement ID and secret before rollout.

Hooks only apply to sessions started after installation. Existing sessions need a restart.

Confirm data is arriving: GA4 `Reports → Realtime` (allow up to a few minutes), or query BigQuery directly. DebugView will stay empty — it only shows events sent with `debug_mode`, which this hook deliberately never sets.

Layout on an employee machine — `~/.qh-claude-telemetry/` on macOS and Linux, `%USERPROFILE%\.qh-claude-telemetry\` on Windows:

```
qh_telemetry_hook.py   the hook
qh_configure.py        copy of the install/uninstall core, so removal works
                       even if the original checkout is gone
config.json            endpoints + capture settings
                       (chmod 600 on POSIX; icacls owner-only on Windows)
spool.ndjson           unsent events, LF-terminated on every platform
client_id              random per-install GA4 client id
telemetry.log          local errors
DISABLED               create this file to opt out
```

## Opt out and removal

```bash
export QH_TELEMETRY=0            # per shell; add to ~/.zshrc to persist
touch ~/.qh-claude-telemetry/DISABLED
./uninstall.sh                   # remove hooks, keep local data
./uninstall.sh --purge           # remove hooks and delete local data
```

```powershell
$env:QH_TELEMETRY = "0"          # this session only
setx QH_TELEMETRY 0              # persists; takes effect in new terminals
New-Item $env:USERPROFILE\.qh-claude-telemetry\DISABLED -ItemType File
.\uninstall.ps1                  # remove hooks, keep local data
.\uninstall.ps1 -Purge           # remove hooks and delete local data
```

`--status` keeps working while disabled, so people can confirm it's actually off.

## Known limitations

The Linux path has been run end to end against a real GA4 property. The Windows path has been written carefully against the documented behaviour of `os.kill`, `os.open`, `icacls`, and PowerShell 5.1, but has not yet been executed on a Windows machine — run `selftest.ps1` there before trusting it, and treat the first Windows install as a test.

Data lands in GA4 within minutes, not in real time. Secret redaction is pattern-based damage limitation, not a guarantee — it catches common API key, token, JWT, private key, SSN, and card-number shapes, and will miss novel ones, so a secret pasted in the first 100 characters of a prompt can still land in the preview.

The 100-character preview cuts at a character boundary, not a word or token boundary, so previews end mid-word. That's cosmetic in reports but worth knowing before you build string matching on top of them.

Delivery is at-least-once, not exactly-once. The spool is designed so that a crash, a killed process, or a full disk loses nothing, which means the failure mode is a duplicate rather than a gap. Deduplicate on `prompt_sha256` plus `ts_ms` in your queries if exact counts matter. There is also a narrow race where two background senders could run concurrently if one is killed at the moment its lock is being judged stale; the per-destination delivery flags make this mostly harmless, but it's the remaining known sharp edge.

An employee who removes the hook from `settings.json` by hand will stop reporting silently, so reconcile the set of reporting employees against your roster periodically rather than assuming coverage.

Claude Code's hook payload field names are read from the current docs. If a future version renames a field, events keep flowing but the affected column goes null rather than breaking — worth a glance at self-test output after Claude Code upgrades.

Windows process creation is several times more expensive than `fork`/`exec`, and the hook pays for it twice per prompt: once for itself, once for the detached sender. Expect a few hundred milliseconds per prompt rather than a few tens. The self-test budgets 900ms on Windows against 400ms elsewhere and prints the measured figure either way.

The `os` field reports `sys.platform`, so Windows machines appear as `win32` regardless of architecture, macOS as `darwin`, and Linux as `linux`. There is no separate field distinguishing WSL from native Linux; both report `linux`, and you would have to infer the difference from `folder_path`.
