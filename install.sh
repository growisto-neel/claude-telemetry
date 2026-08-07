#!/usr/bin/env bash
#
# Qualified Health - Claude Code telemetry installer
#
# Installs a hook that records Claude Code usage (who, what prompt, which
# skill, which folder) and ships it to QH analytics.
#
#   ./install.sh                          # interactive
#   ./install.sh --non-interactive        # for MDM / scripted rollout
#   ./install.sh --collector-url URL --collector-token TOKEN
#   ./install.sh --ga4-measurement-id G-XXXX --ga4-api-secret SECRET
#   ./install.sh --prompt-capture hash --path-capture basename
#   ./install.sh --email you@qualifiedhealthai.com --team platform
#
# Safe to re-run: it replaces its own hook entries and leaves any other
# hooks you have configured untouched.

set -euo pipefail

BASE_DIR="${QH_TELEMETRY_DIR:-$HOME/.qh-claude-telemetry}"
SETTINGS_PATH="${CLAUDE_SETTINGS_PATH:-$HOME/.claude/settings.json}"
HOOK_DEST="$BASE_DIR/qh_telemetry_hook.py"
CONFIG_PATH="$BASE_DIR/config.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK_SRC="$SCRIPT_DIR/hooks/qh_telemetry_hook.py"
# The install/uninstall logic that can damage a file the user cares about lives
# in one Python module shared with install.ps1, rather than being written twice.
CONFIGURE="$SCRIPT_DIR/tools/qh_configure.py"

COLLECTOR_URL=""
COLLECTOR_TOKEN=""
GA4_MEASUREMENT_ID=""
GA4_API_SECRET=""
USER_EMAIL=""
TEAM=""
PROMPT_CAPTURE="preview"
PATH_CAPTURE="full"
INTERACTIVE=1

need_val() {
  # Guard against a trailing flag with no value, which would otherwise abort
  # with a bare "unbound variable" under `set -u`.
  if [[ -z "${2:-}" ]]; then
    echo "ERROR: $1 requires a value." >&2
    exit 1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --collector-url)       need_val "$1" "${2:-}"; COLLECTOR_URL="$2"; shift 2 ;;
    --collector-token)     need_val "$1" "${2:-}"; COLLECTOR_TOKEN="$2"; shift 2 ;;
    --ga4-measurement-id)  need_val "$1" "${2:-}"; GA4_MEASUREMENT_ID="$2"; shift 2 ;;
    --ga4-api-secret)      need_val "$1" "${2:-}"; GA4_API_SECRET="$2"; shift 2 ;;
    --email)               need_val "$1" "${2:-}"; USER_EMAIL="$2"; shift 2 ;;
    --team)                need_val "$1" "${2:-}"; TEAM="$2"; shift 2 ;;
    --prompt-capture)      need_val "$1" "${2:-}"; PROMPT_CAPTURE="$2"; shift 2 ;;
    --path-capture)        need_val "$1" "${2:-}"; PATH_CAPTURE="$2"; shift 2 ;;
    --non-interactive)     INTERACTIVE=0; shift ;;
    -h|--help)             sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

case "$PROMPT_CAPTURE" in preview|hash) ;; *)
  echo "ERROR: --prompt-capture must be preview or hash." >&2
  echo "  preview  first 100 chars + length, word count, hash (default)" >&2
  echo "  hash     length, word count, hash only; no prompt text at all" >&2
  echo "There is no full-text mode: 100 characters is the maximum retained." >&2
  exit 1 ;; esac
case "$PATH_CAPTURE" in full|basename|none) ;; *)
  echo "ERROR: --path-capture must be full, basename, or none." >&2; exit 1 ;; esac

# With no TTY (piped installer, CI, MDM), prompting would read EOF and abort
# silently under `set -e`. Fall back to non-interactive instead.
if [[ ! -t 0 ]]; then
  INTERACTIVE=0
fi

# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
      PY="$(command -v "$candidate")"; break
    fi
  fi
done
if [[ -z "$PY" ]]; then
  echo "ERROR: Python 3.8+ is required but was not found." >&2
  echo "  macOS:  xcode-select --install" >&2
  echo "  Ubuntu: sudo apt-get install -y python3" >&2
  exit 1
fi

for required in "$HOOK_SRC" "$CONFIGURE"; do
  if [[ ! -f "$required" ]]; then
    echo "ERROR: cannot find $required" >&2
    echo "Run this script from inside a complete checkout of qh-claude-telemetry." >&2
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# disclosure - employees see exactly what is collected before anything runs
# ---------------------------------------------------------------------------

cat <<'NOTICE'
------------------------------------------------------------------
 Qualified Health - Claude Code usage telemetry
------------------------------------------------------------------
This records, for each Claude Code session on this machine:

   * your work email address
   * the FIRST 100 CHARACTERS of each prompt you send to Claude,
     with common secret shapes scrubbed, plus how long the prompt
     was in characters and words
   * which skill or subagent was invoked
   * the folder path / repo you were working in
   * session start & end, model, and timestamps

The full text of your prompts is never recorded or transmitted.
Nothing longer than 100 characters of prompt text is stored anywhere.

It does NOT record Claude's responses, your file contents, your
keystrokes, your terminal output, or anything outside Claude Code.

You can turn it off at any time:   export QH_TELEMETRY=0
Local log of everything sent:      ~/.qh-claude-telemetry/
------------------------------------------------------------------
NOTICE

if [[ "$INTERACTIVE" -eq 1 ]]; then
  read -r -p "Install? [y/N] " reply || reply=""
  case "$reply" in [yY]*) ;; *) echo "Aborted."; exit 0 ;; esac
fi

# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------

if [[ -z "$USER_EMAIL" ]]; then
  USER_EMAIL="$(git config --get user.email 2>/dev/null || true)"
fi
if [[ "$INTERACTIVE" -eq 1 ]]; then
  read -r -p "Work email [${USER_EMAIL:-none detected}]: " entered || entered=""
  if [[ -n "$entered" ]]; then USER_EMAIL="$entered"; fi
fi
if [[ -z "$USER_EMAIL" ]]; then
  echo "WARNING: no email resolved; events will fall back to OS username@hostname." >&2
fi

# ---------------------------------------------------------------------------
# install files
# ---------------------------------------------------------------------------

mkdir -p "$BASE_DIR" "$(dirname "$SETTINGS_PATH")"
cp "$HOOK_SRC" "$HOOK_DEST"
chmod 0755 "$HOOK_DEST"
# Keep a copy next to the hook so uninstall.sh / uninstall.ps1 still work after
# the checkout this was installed from has been deleted or moved.
cp "$CONFIGURE" "$BASE_DIR/qh_configure.py"
chmod 0644 "$BASE_DIR/qh_configure.py"

# Secrets go through the environment, not argv: the argument list of a running
# process is readable by other users on this machine.
umask 077
COLLECTOR_URL="$COLLECTOR_URL" COLLECTOR_TOKEN="$COLLECTOR_TOKEN" \
GA4_MEASUREMENT_ID="$GA4_MEASUREMENT_ID" GA4_API_SECRET="$GA4_API_SECRET" \
USER_EMAIL="$USER_EMAIL" TEAM="$TEAM" \
PROMPT_CAPTURE="$PROMPT_CAPTURE" PATH_CAPTURE="$PATH_CAPTURE" \
"$PY" "$CONFIGURE" write-config --config "$CONFIG_PATH" --hook "$HOOK_DEST"
umask 022

# ---------------------------------------------------------------------------
# merge hooks into settings.json (idempotent, backed up, non-destructive)
# ---------------------------------------------------------------------------

"$PY" "$CONFIGURE" install-hooks \
  --settings "$SETTINGS_PATH" --hook "$HOOK_DEST" --python "$PY"

# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

echo
echo "Verifying with a synthetic event..."
"$PY" "$CONFIGURE" verify --hook "$HOOK_DEST" --python "$PY" \
  || echo "  Check $BASE_DIR/telemetry.log." >&2

echo
"$PY" "$HOOK_DEST" --status

cat <<'DONE'

Installed. Hooks take effect in newly started Claude Code sessions.

  Check status:   python3 ~/.qh-claude-telemetry/qh_telemetry_hook.py --status
  Dry-run event:  python3 ~/.qh-claude-telemetry/qh_telemetry_hook.py --test
  Opt out:        export QH_TELEMETRY=0      (add to your shell profile)
  Remove:         ./uninstall.sh
DONE
