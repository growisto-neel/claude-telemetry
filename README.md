# Claude Code usage telemetry

A Claude Code plugin that records how the team uses Claude Code and reports it to Google Analytics 4.

Two commands to install, nothing to download, no admin rights, and no runtime to install. Works on Linux, macOS, and native Windows.

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

One event each time a session starts, a prompt is sent, a skill or subagent runs, and a session ends. An identical event seen twice within five seconds is recorded once, so two things wired to the same hook cannot quietly double somebody's numbers.

| Field | Example |
|---|---|
| `user_email` | `neel.thakkar@growisto.com` |
| `prompt_preview` | first 100 characters of the prompt, secrets scrubbed |
| `prompt_chars`, `prompt_words` | `847`, `132` |
| `skill`, `tool_name` | `growisto-presentation`, `Skill` |
| `folder_path`, `folder_name`, `repo` | working directory and git repo |
| `cc_session_id`, `model`, `session_source` | session metadata |
| `os`, `hook_version` | `linux`, `2.1.0` |

**The full text of a prompt is never recorded or transmitted.** Only the first 100 characters, plus the length. There is no field anywhere in the pipeline that could hold more — not in the hook, not in the spool file, not in GA4. Restoring full-prompt capture would mean editing the hook itself and shipping new binaries, which is the intended amount of friction.

It does not record Claude's responses, file contents, diffs, terminal output, keystrokes, or anything outside Claude Code.

## How it works

```
Claude Code hook  →  bin/growisto-hook  →  bin/growisto-hook-<os>-<arch>  →  spool file  →  GA4
                     (bash launcher)      (static binary: append one line, exit)   (background sender)
```

The hook's only job on the critical path is appending one line to a local file, which takes under a millisecond. A detached background process does the network I/O. If the network is down the events stay spooled and go out next time. Every path is wrapped so the process always exits 0 and prints nothing, because a `UserPromptSubmit` hook that exits non-zero can block the prompt, and anything it prints on stdout gets injected into Claude's context.

`bin/growisto-hook` exists because a hook command is a string handed to a shell, not a path with arguments. It reads `uname` to derive an OS and CPU pair, execs the matching `bin/growisto-hook-<os>-<arch>` binary, and does nothing else of consequence. Four binaries are committed to the repo — linux-amd64, darwin-amd64, darwin-arm64, windows-amd64 — because `/plugin marketplace add` clones the repo and runs what it finds, with no build step it could trigger.

The launcher being a bash script is a deliberate choice rather than an accepted risk, and the reasoning is worth stating because it looks like a dependency and isn't one. Claude Code runs a hook command through `sh -c` on Linux and macOS, and on Windows through Git Bash, falling back to PowerShell when Git Bash is absent. Installing this plugin means `/plugin marketplace add` cloning a private repository, so anyone who can install it has git — and on Windows a default Git for Windows install includes Git Bash. The set of machines that can install the plugin and the set that can run the launcher are the same set.

What that does *not* cover is a machine with a bare `git.exe` and no Git Bash, or one where `bash` resolves to `C:\Windows\System32\bash.exe` — the WSL launcher, which is not a shell and fails without WSL installed. There the hook process never starts, and because every diagnostic in this system is written by code inside that process, the result is total silence: no log, no marker, and nothing for `/growisto-telemetry` to report. It is the one failure the plugin cannot describe about itself. See Known limitations.

The binaries are Go, built with `CGO_ENABLED=0`, so they link nothing and need no runtime installed. `GROWISTO_TELEMETRY_BINARY` overrides the choice if you need to point at a local build.

If no binary matches the machine, the launcher writes one line to the log naming both the derived target and the raw `uname` output, drops a `NO_BINARY` marker so it never complains again, and exits 0. Nobody's session breaks — but telemetry is silently inactive on that machine until another target is added to `build.sh`. See Known limitations.

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
cmd/growisto-hook/               all the logic; Go standard library only
  main.go                        config, dials, the four modes, exit-0 guarantee
  event.go                       identity, redaction, event shape, GA4 projection
  spool.go                       spool file, locking, background sender
  options.go                     install-time userConfig values, NO_DEST diagnostic
  dedupe.go                      suppress the same event seen twice in five seconds
  platform_unix.go               build-tagged: process liveness, detach, chmod
  platform_windows.go            build-tagged: OpenProcess, creation flags, icacls
  hook_test.go                   the test suite
bin/growisto-hook                cross-platform launcher
bin/growisto-hook-<os>-<arch>    the committed binaries, four of them
build.sh                         builds all four
go.mod                           module definition; no dependencies
.github/workflows/build.yml      vets, tests, and checks bin/ is current; commits nothing
commands/growisto-telemetry.md   the /growisto-telemetry status and repair command
```

## GA4 setup

Only needed once, by whoever owns the property. Full walkthrough in [TESTING.md](TESTING.md) — create a Web data stream, create a Measurement Protocol API secret, and **register the custom dimensions**. That last step is the one that silently ruins things: GA4 stores custom parameters immediately but will not show them in any report until each is registered by name, and registration is not retroactive.

## Why there is no server

Every laptop posts straight to GA4. There is no service in the middle, and an earlier FastAPI collector was deleted rather than maintained.

The argument for a proxy was that the GA4 API secret is a write credential and direct mode puts a copy on every machine. That is true, and it is a smaller problem than it sounds: the secret is write-only and scoped to one analytics property, so the worst an attacker does with it is inject junk events into a usage dashboard, and the fix is rotating one value. Weighed against running, deploying, and securing a service in a second language, it does not justify itself.

The other reasons a proxy would earn its place are worth knowing, because they are what would change the answer. Central control over the payload — hashing `user_email` before it leaves the machine, or moving off GA4 entirely — is currently a code change plus a push to sixty laptops rather than a config change in one place. And GA4's own event-level retention caps at fourteen months, so anything longer needs a warehouse behind a collector. If either becomes a real requirement, the pipeline should be designed for it rather than reassembled from the deleted service.

## Testing

```bash
go vet ./...
go test ./...
./build.sh
```

Only whoever edits the hook needs Go. Colleagues installing the plugin build nothing.

The tests run in a temp directory, send no network traffic, and do not touch your real `~/.claude` or `~/.growisto-claude-telemetry` — every one of them repoints the data directory at `t.TempDir()` first, and unsets any ambient `CLAUDE_PLUGIN_OPTION_*` variables, because the suite is usually run inside a Claude Code session where the real install has already exported live credentials.

They cover the things that fail silently in production: the capture-mode dials resolving to the most private option on a typo, prompt previews never exceeding 100 characters or cutting a multi-byte character in half, redaction running before truncation, `PreToolUse` keeping the four skill tools and dropping everything else, internal bookkeeping fields never reaching the wire, and one corrupt spool line costing one event rather than the whole file.

Two groups are newer. The install-time options are matched on a canonical spelling, so `GA4MEASUREMENTID`, `ga4-measurement-id`, and the `CLAUDE_PLUGIN_CONFIG_` prefix all resolve to the same setting, while an unprefixed `TEAM` in the environment is deliberately *not* adopted — `os.Getenv` is case-insensitive on Windows, so a bare lowercase candidate would silently pick up an unrelated variable on one platform only. And the `no GA4 destination` diagnostic is asserted to name the options it saw without ever writing their values, and to be written once rather than on every event. The duplicate-suppression tests assert that two occurrences of the same event produce the same fingerprint, that a repeat outside the five-second window is recorded (backdated with `os.Chtimes` rather than by sleeping), and that a clock moving backwards drops nothing.

The platform split is the part worth knowing about. On POSIX the standard way to ask "is process N alive?" is `kill(pid, 0)`; on Windows that same call *terminates* the process, and Windows recycles PIDs. `platform_windows.go` uses `OpenProcess` plus `GetExitCodeProcess` instead, and treats access-denied as "exists, not ours". A naive port would have had telemetry killing unrelated programs.

## Known limitations

**The binaries have never been executed.** Not on Windows, not anywhere. The Go code was written against the documented behaviour of `OpenProcess`, `GetExitCodeProcess`, `icacls`, and Git Bash and reviewed carefully, but reading code is not running it. Run `go test ./...` and a real install on each platform before trusting any of it.

**Windows needs Git Bash, and says nothing if it is missing.** The hook command is `bash "${CLAUDE_PLUGIN_ROOT}"/bin/growisto-hook`, and Claude Code hands that string to Git Bash on Windows — falling back to PowerShell, where `bash` is not a command, when Git Bash is absent. This is an accepted dependency rather than an unmet one: installing the plugin means cloning a private repo, which needs git, and a default Git for Windows install includes Git Bash. See How it works.

What makes it worth listing anyway is the shape of the failure on a machine that slipped through — a bare `git.exe` with no Git Bash, or `bash` resolving to `C:\Windows\System32\bash.exe` without WSL installed. The hook process never starts, so none of the diagnostics inside it run: no log line, no `NO_BINARY` marker, no `pending events`, and nothing for `/growisto-telemetry` to report. Total silence with no local evidence. Confirm `where bash` prints a Git Bash path before rolling out to a Windows machine, and treat an empty data directory after a restart as this case rather than as a configuration problem.

**A missing target is a silent gap by design.** The Python requirement that used to sit here is gone — the hook is a static binary, so there is no runtime to be absent. What replaced it is narrower: `build.sh` ships four targets and deliberately omits linux-arm64 and windows-arm64, so a Raspberry Pi or ARM server would record nothing. Unlike the Windows case above this one announces itself, because the launcher does run — it writes a `NO_BINARY` marker naming the target it wanted, and `/growisto-telemetry` reports it. The fix is one more line in `build.sh`.

**`user_email` in GA4 is a Google terms violation.** GA4 is not covered by a BAA and Google's terms prohibit sending PII. The `user_email_sha256` field exists as the compliant alternative — keep a lookup table on your side and let dashboards operate on the hash. Worth doing if GA4 becomes load-bearing.

**Employee monitoring notice requirements vary by jurisdiction.** A prompt fragment tied to a named individual is personal data under GDPR. See [PRIVACY_NOTICE.md](PRIVACY_NOTICE.md), which is written to be sent to the team and has bracketed parts that need real answers first.

**The marketplace repo is a supply-chain path into every developer machine.** Every install and update pulls from it. It should live in a company-owned org with managed access, not a personal account.
