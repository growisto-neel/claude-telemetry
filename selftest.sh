#!/usr/bin/env bash
#
# Self-test for the QH Claude Code telemetry hook.
#
# The suite itself lives in selftest.py so that Windows, macOS, and Linux all
# run exactly the same checks. This wrapper exists so the shell command people
# already have in their notes keeps working.
#
#   ./selftest.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
  exit 1
fi

exec "$PY" "$SCRIPT_DIR/selftest.py" "$@"
