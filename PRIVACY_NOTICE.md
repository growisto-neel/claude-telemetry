# Claude Code usage telemetry — what we collect and why

*Draft notice to send to the team before rollout. Edit the bracketed parts and have whoever owns privacy or compliance read it first.*

---

We're turning on usage tracking for Claude Code so we can see how the tooling is actually being used — which skills earn their keep, where people hit friction, and whether the platform investment is paying off. Being straightforward about the scope up front, because some of this is more than the usual anonymous product analytics.

**What we record, every time you send a prompt to Claude Code:**

- your work email address
- the **first 100 characters** of the prompt you sent, and how long the whole prompt was in characters and words
- which skill or subagent ran
- the folder path and repository you were working in
- when the session started and ended, and which model was used

**What we do not record:** the full text of your prompts, Claude's responses, the contents of your files, your diffs, your terminal output, your keystrokes, your git history, or anything you do outside Claude Code.

The prompt part is worth being precise about. We keep the opening 100 characters and nothing more. If you send a 900-character prompt, what's stored is the first 100 characters plus the number 900 — the other 800 characters are not written down anywhere, not in the dashboard and not in any database. There is no setting that turns full-prompt capture on; the code has no field to put it in.

Those 100 characters are still free text, so whatever you happen to open with ends up in the log. The tool scrubs the common shapes of secrets — API keys, tokens, JWTs, private keys, SSNs, card numbers — but that's pattern matching, not a guarantee. Treat the start of a prompt the way you'd treat a message in a monitored work channel.

**If you work with customer data or PHI:** don't paste real records into a prompt. Use synthetic or de-identified examples. That was already the rule. A 100-character window is a much smaller exposure than a whole prompt, but it isn't zero — and pasted records tend to start with the identifying fields.

**Where it goes.** The 100-character preview, prompt length, folder path, and the rest of the fields above go to a Google Analytics property owned by [Growisto], and nowhere else. There is no data warehouse and no second copy. Retention is whatever GA4 is set to, currently [set this deliberately and state the number here].

**What it's used for:** understanding adoption and improving the tooling. It is not used for individual performance evaluation. [Confirm this is actually true and that the people who own headcount decisions agree, or delete this line — a promise like this is worse than no promise if it isn't held.]

**Turning it off.** You can opt out at any time, and you don't need to tell anyone or explain why.

On Linux or macOS:

```bash
export GROWISTO_TELEMETRY=0     # add to your ~/.zshrc or ~/.bashrc to make it stick
```

On Windows:

```powershell
setx GROWISTO_TELEMETRY 0       # then open a new terminal; setx never affects the current one
```

To remove it entirely, run `/plugin uninstall growisto-claude-telemetry`.

Everything the tool has queued to send lives in plain text on your own machine, so you can read exactly what it's reporting — `~/.growisto-claude-telemetry/` on Linux and macOS, `%USERPROFILE%\.growisto-claude-telemetry\` on Windows. Running `/growisto-telemetry` inside Claude Code prints the current configuration and whether anything is stuck unsent.

If you use both Windows and WSL, note that they are separate installs with separate data directories. Turning telemetry off in one does not turn it off in the other.

Questions, or think we've drawn the line in the wrong place — talk to Neel (neel.thakkar@growisto.com).

---

## Notes for whoever is rolling this out

A few things to settle before this goes out, in rough order of how much trouble they cause if skipped.

Google Analytics is not covered by a Google BAA, and Google's terms prohibit sending personally identifiable information to it. This deployment sends `user_email` to GA4 as a custom dimension, which is PII under those terms. It's common practice and rarely enforced, but it is a terms violation and the property is technically at risk. The `user_email_sha256` field exists as the compliant alternative — you'd keep a lookup table on your own side to resolve identities and let GA4 dashboards operate only on the hash. Worth doing if GA4 becomes load-bearing.

Employee monitoring notice requirements vary by jurisdiction. If you have staff in the EU or UK, a prompt fragment tied to a named individual is still personal data under GDPR and needs a lawful basis and a record in your processing register; the 100-character cap makes a DPIA easier to argue your way out of than full-prompt capture would, but the identity field is what triggers most of the obligation, not the prompt length. Illinois, California, Colorado, and Connecticut have their own notice rules. Anyone in Germany with a works council needs consultation before this ships. Get an answer for the jurisdictions you actually employ in rather than assuming the US default.

If you deploy through managed settings so the hook can't be removed, the opt-out paragraph above becomes false and needs to come out — and mandatory monitoring generally carries a higher disclosure bar than opt-in. Don't leave a stale opt-out promise in a notice about a system that doesn't honor it.

Decide the retention period on purpose rather than accumulating previews indefinitely by default. GA4's setting is at Admin → Data collection and modification → Data retention, and it defaults to two months for event-level data. Since GA4 is the only destination, that setting is your only retention control, and whatever you choose is the number that belongs in the notice above.

If someone asks you to raise the 100-character limit, treat that as a policy decision rather than a config change. The cap is load-bearing for every claim in the employee notice above, and the code has no full-text path to re-enable — restoring one means editing the hook and shipping new binaries to everyone, which is the right amount of friction.

Last thing, and it's the one most likely to bite: if prompt fragments are logged with names attached and people believe the logs might be read by their manager, they will change what they ask Claude. You'll lose exactly the honest usage signal you built this to capture, and you won't be able to tell that it happened. Consider whether team-level rollups on the hashed identity would answer your actual questions — most adoption questions don't need per-person prompt text, and the version people trust is the version that keeps working.
