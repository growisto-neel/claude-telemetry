# Claude Code usage telemetry

A Claude Code plugin that records how the team uses Claude Code and reports it to Google Analytics 4.

Two commands to install, nothing to download, no admin rights. Works on Linux, macOS, and native Windows. The only requirement is Python 3.8+ on the machine.

---

## Install

```
/plugin marketplace add growisto-neel/claude-telemetry
/plugin install growisto-claude-telemetry@growisto
```

Claude Code prompts for the GA4 measurement ID and API secret during install. Get both from Neel. Restart Claude Code afterwards — hooks only load when a session starts.

To check it worked, run `/growisto-telemetry`. That reports whether telemetry is enabled, which email it resolved, and whether anything is stuck unsent. It also repairs the configuration if the values you entered at install did not reach the hook.

To remove it: `/plugin uninstall growisto-claude-telemetry`.

## What it records

One event each time a session starts, a prompt is sent, a skill or subagent runs, and a session ends.

| Field | Example |
|---|---|
| `user_email` | `neel.thakkar@growisto.com` |
| `prompt_preview` | first 100 characters of the prompt, secrets scrubbed |
| `prompt_chars`, `prompt_words` | `847`, `132` |
| `skill`, `tool_name` | `growisto-presentation`, `Skill` |
| `folder_path`, `folder_name`, `repo` | working directory and git repo |
| `cc_session_id`, `model`, `session_source` | session metadata |
| `os`, `hook_version` | `linux`, `1.4.0` |

**The full text of a prompt is never recorded or transmitted.** Only the first 100 characters, plus the length. There is no field anywhere in the pipeline that could hold more — not in the hook, not in the spool file, not in GA4. Restoring full-prompt capture would mean editing the hook and the collector model, which is the intended amount of friction.

It does not record Claude's responses, file contents, diffs, terminal output, keystrokes, or anything outside Claude Code.

## How it works

```
Claude Code hook  →  bin/growisto-hook  →  hooks/growisto_telemetry_hook.py  →  spool file  →  GA4
                     (bash launcher)  (append one line, exit)       (background sender)
```

The hook's only job on the critical path is appending one line to a local file, which takes under a millisecond. A detached background process does the network I/O. If the network is down the events stay spooled and go out next time. Every path is wrapped so the process always exits 0 and prints nothing, because a `UserPromptSubmit` hook that exits non-zero can block the prompt, and anything it prints on stdout gets injected into Claude's context.

`bin/growisto-hook` exists because hook commands are shell commands and Claude Code bundles no interpreter. It finds a Python 3.8+ interpreter — trying `python3`, `python`, `py -3`, then the directories the Windows installer actually uses — and execs the hook. One bash script covers all three platforms: Claude Code runs hook commands through bash on Linux and macOS and Git Bash on Windows, and since installing a plugin means cloning a git repo, anyone who can install this has Git Bash.

If no interpreter is found, the launcher writes one line to the log, drops a marker so it never complains again, and exits 0. Nobody's session breaks — but telemetry is silently inactive on that machine. See Known limitations.

## Privacy dials

Set at install time, changeable by reinstalling or by editing `config.json`.

`promptCapture` is `preview` (first 100 characters) or `hash` (a one-way hash and no readable text at all).

`pathCapture` is `full` (the working directory path), `basename` (just the folder name), or `none` (no location at all — this suppresses `folder_name` as well as `folder_path`).

Both are free text, because plugin `userConfig` fields have no enum validation. Case and surrounding whitespace don't matter, but anything else unrecognised fails closed — an unreadable `promptCapture` becomes `hash` and an unreadable `pathCapture` becomes `none`, so a typo can only ever narrow what's collected. Leaving a field blank keeps the defaults above.

## Opting out

Anyone can opt out without telling anyone or explaining why.

```bash
export GROWISTO_TELEMETRY=0          # Linux / macOS, add to ~/.bashrc or ~/.zshrc
setx GROWISTO_TELEMETRY 0            # Windows, then open a new terminal
```

Or create an empty `DISABLED` file in the data directory. Or just uninstall the plugin.

## Where things live

```
~/.growisto-claude-telemetry/          %USERPROFILE%\.growisto-claude-telemetry\ on Windows
├── config.json                  credentials and settings; owner-readable only
├── spool.ndjson                 events waiting to be sent
├── telemetry.log                errors only; empty is good
└── client_id                    random per-machine ID for GA4
```

Everything queued is plain text on the employee's own machine, so anyone can read exactly what is being reported about them. That is deliberate.

## Repo layout

```
.claude-plugin/plugin.json       manifest, including the install-time prompts
.claude-plugin/marketplace.json  marketplace definition
hooks/hooks.json                 the four hook registrations
hooks/growisto_telemetry_hook.py       all the logic; stdlib only
bin/growisto-hook                      cross-platform launcher
commands/growisto-telemetry.md         the /growisto-telemetry status and repair command
collector/                       optional service, see below
selftest.py                      the test suite
```

## GA4 setup

Only needed once, by whoever owns the property. Full walkthrough in [TESTING.md](TESTING.md) — create a Web data stream, create a Measurement Protocol API secret, and **register the custom dimensions**. That last step is the one that silently ruins things: GA4 stores custom parameters immediately but will not show them in any report until each is registered by name, and registration is not retroactive.

## Optional collector

`collector/` is a small FastAPI service that receives events and forwards them to GA4. It is not part of the default deployment and nothing needs it.

The one reason to run it: the GA4 API secret is a write credential, and in direct mode a copy sits on every laptop. The collector keeps it in one place instead. It logs each event as a structured line; there is no warehouse writer, and the BigQuery sink that used to be here was removed because nobody was running it.

## Testing

```bash
python3 selftest.py        # or ./selftest.sh
```

Runs in a temp directory, sends no network traffic, and does not touch your real `~/.claude` or `~/.growisto-claude-telemetry`. Twelve areas: syntax, event capture, noise filtering, secret redaction, capture modes, opt-out, resilience and latency, GA4 payload shape, crash recovery, duplicate suppression, platform behaviour, and paths containing spaces.

The platform section is the one worth knowing about. On POSIX the standard way to ask "is process N alive?" is `os.kill(pid, 0)`; on Windows that same call *terminates* the process, and Windows recycles PIDs. A naive port would have had telemetry killing unrelated programs. The suite starts a real child, probes it, and asserts it survived.

## Known limitations

**Python is a hard requirement and is not guaranteed.** On a machine without it, telemetry is silently inactive. For a telemetry system this is the worst kind of gap, because the dashboard looks healthy while systematically missing whichever population is least likely to have Python installed. The permanent fix is porting the hook to a compiled binary shipped inside the plugin.

**The Windows path has never been executed.** It was written against the documented behaviour of `os.kill`, `os.open`, `icacls`, and Git Bash, and reviewed carefully, but reading code is not running it. Run `selftest.py` on a Windows machine before trusting it.

**`user_email` in GA4 is a Google terms violation.** GA4 is not covered by a BAA and Google's terms prohibit sending PII. The `user_email_sha256` field exists as the compliant alternative — keep a lookup table on your side and let dashboards operate on the hash. Worth doing if GA4 becomes load-bearing.

**Employee monitoring notice requirements vary by jurisdiction.** A prompt fragment tied to a named individual is personal data under GDPR. See [PRIVACY_NOTICE.md](PRIVACY_NOTICE.md), which is written to be sent to the team and has bracketed parts that need real answers first.

**The marketplace repo is a supply-chain path into every developer machine.** Every install and update pulls from it. It should live in a company-owned org with managed access, not a personal account.
