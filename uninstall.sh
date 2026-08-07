#!/usr/bin/env bash
#
# Removes the QH Claude Code telemetry hook and (optionally) its local data.
# Leaves any other hooks in settings.json untouched.
#
#   ./uninstall.sh            # unwire hooks, keep the local spool and log
#   ./uninstall.sh --purge    # unwire hooks and delete ~/.qh-claude-telemetry

set -euo pipefail

BASE_DIR="${QH_TELEMETRY_DIR:-$HOME/.qh-claude-telemetry}"
SETTINGS_PATH="${CLAUDE_SETTINGS_PATH:-$HOME/.claude/settings.json}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The same module install.sh and install.ps1 use. The removal logic is the
# mirror image of the install merge, so it lives beside it rather than being
# written a second time here and a third time in PowerShell.
CONFIGURE="$SCRIPT_DIR/tools/qh_configure.py"

PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
      PY="$(command -v "$candidate")"; break
    fi
  fi
done
if [[ -z "$PY" ]]; then
  echo "Python 3.8+ not found, so settings.json cannot be edited automatically." >&2
  echo "Remove any hook entry mentioning qh_telemetry_hook.py from $SETTINGS_PATH by hand." >&2
  exit 1
fi

# Fall back to the copy installed alongside the hook, so uninstall still works
# from a checkout that has been moved or partially deleted.
if [[ ! -f "$CONFIGURE" ]]; then
  if [[ -f "$BASE_DIR/qh_configure.py" ]]; then
    CONFIGURE="$BASE_DIR/qh_configure.py"
  else
    echo "ERROR: cannot find qh_configure.py (looked in $SCRIPT_DIR/tools and $BASE_DIR)." >&2
    echo "Remove any hook entry mentioning qh_telemetry_hook.py from $SETTINGS_PATH by hand." >&2
    exit 1
  fi
fi

"$PY" "$CONFIGURE" remove-hooks --settings "$SETTINGS_PATH"

if [[ "$PURGE" -eq 1 ]]; then
  rm -rf "$BASE_DIR"
  echo "Deleted $BASE_DIR"
else
  echo "Left local data in $BASE_DIR (re-run with --purge to delete it)."
fi

echo "Done. Restart any open Claude Code sessions."
