# GA4 setup and first-run verification

Done once, by whoever owns the analytics property. Everyone else just installs the plugin and never reads this.

Roughly 20 minutes, most of it clicking through the same GA4 dialog fifteen times.

---

## Step 1 — Run the self-test

Before any Google configuration, confirm the code is sound on your machine.

```bash
python3 selftest.py
```

It runs in a temp directory, sends no network traffic, and does not touch your real `~/.claude` or `~/.growisto-claude-telemetry`. A failure here is a bug in the repo, not a setup problem — stop and fix it rather than pushing on.

## Step 2 — Create a GA4 property

Go to [analytics.google.com](https://analytics.google.com) and sign in with the account that should own this. Use a company-owned account, not a personal one, or the property disappears with the login.

Admin (bottom-left gear) → Create → Property. You'll be asked for a name, a time zone, and a currency. Set the time zone correctly — it decides where GA4 draws day boundaries in every report. Industry category and business size affect nothing here.

When it offers to create a data stream, choose **Web**. Not iOS, not Android — Web is the only stream type with a Measurement Protocol endpoint you can post to from a script. It asks for a website URL and a stream name; `https://growisto.com` and `laptops` are fine. The URL is never contacted and never checked. It exists because GA4 assumes you're tagging a website.

Copy the **Measurement ID** — `G-` followed by ten or so characters.

## Step 3 — Create the API secret

Admin → Data collection and modification → Data streams → click your stream → scroll to **Measurement Protocol API secrets** → Create. Give it a nickname and copy the value.

You need Editor or Administrator on the property to see this section. If it isn't there, you're a Viewer or Analyst.

This secret is a write credential: anyone holding it can inject arbitrary events into the property. In the default setup a copy lands on every laptop that installs the plugin. That is the argument for running the optional collector once this outgrows a pilot.

## Step 4 — Register the custom dimensions

Easy to skip, expensive to skip. GA4 accepts and stores custom parameters immediately but will not show them in any report until each is registered by name, and **registration is not retroactive** — events that arrive before you register a dimension keep that value out of reports permanently. Do this before sending anything real.

Admin → Data display → Custom definitions → Create custom dimensions. For each: **Scope = Event**, **Event parameter** set to exactly the name below, and the same string as the dimension name.

```
user_email      team            cc_session_id   folder_name     folder_path
repo            skill           tool_name       model           session_source
permission_mode prompt_preview  prompt_hash     os              hook_version
```

Then Custom metrics → Create custom metrics, twice, Scope = Event, Unit = Standard:

```
prompt_chars    prompt_words
```

Fifteen dimensions of your 50 event-scoped allowance, two metrics of 50. There's no bulk import. Copy-paste the parameter names rather than typing them — a typo produces a dimension that is permanently empty and gives no error.

## Step 5 — Install the plugin and verify

```
/plugin marketplace add growisto-neel/claude-telemetry
/plugin install growisto-claude-telemetry@growisto
```

Enter the measurement ID and API secret when prompted, then restart Claude Code.

```
/growisto-telemetry
```

You want `enabled: True`, your email with a plausible source, `prompt capture: preview`, your `G-` ID present, and `pending events: 0`.

If it reports **not configured**, the values you entered at install did not reach the hook as environment variables. `/growisto-telemetry` will offer to write `config.json` directly, which fixes it. Tell Neel if this happens — it means the plugin manifest needs adjusting for everyone, not just you.

If it reports **no Python**, that machine cannot run the hook at all. Install Python 3.8+ (`winget install Python.Python.3.12` on Windows, `brew install python` on macOS), open a new terminal, and start a fresh session.

## Step 6 — Confirm events arrive

Send two or three prompts, invoke a skill, exit. That should produce `cc_session_start`, one `cc_prompt` per prompt, one `cc_skill` per skill or subagent, and `cc_session_end`.

Then in GA4: **Reports → Realtime**. Allow a few minutes; Measurement Protocol events are not instant. Look for the event names in the "Event count by Event name" card. Clicking an event name opens its parameters, which is the fastest way to confirm `prompt_preview` and `skill` are arriving with values.

**DebugView will be empty, and that's expected.** GA4 only routes Measurement Protocol events there if each event carries `debug_mode: 1`, and the hook deliberately never sends it — leaving that on would route production traffic into a debug stream and distort reports.

Standard reports and Explore lag 24–48 hours on a new property, and registered custom dimensions need that long to start populating. Realtime is the only same-day view. Don't conclude anything is broken on day one because Explore looks empty.

## Troubleshooting

**Events accepted but reports stay empty.** Almost always Step 4 not done, done with a typo, or done after the events arrived. Registration isn't retroactive — send fresh events after fixing the names.

**`pending events` climbing.** Read `telemetry.log` in the data directory. A `403` is a bad API secret. A connection error is a proxy or firewall between the laptop and `google-analytics.com`.

**Prompts sent but no events at all.** You're probably in a session that started before the plugin was installed. Hooks load at session start; restart Claude Code.

**Skill events missing, prompt events fine.** Skills are captured through `PreToolUse` filtered to `Skill`, `Task`, and `SlashCommand`. A skill Claude loaded without a tool call won't produce one.

## Before rolling out to the team

Two things. Get an answer on `user_email` in GA4 — it's PII under Google's terms and `user_email_sha256` is the compliant alternative. And send the notice in [PRIVACY_NOTICE.md](PRIVACY_NOTICE.md); it's written to be edited, and the bracketed parts need real answers first.

Also set GA4's own retention deliberately (Admin → Data collection and modification → Data retention). It defaults to two months for event-level data, and in direct mode that is your only retention control.
