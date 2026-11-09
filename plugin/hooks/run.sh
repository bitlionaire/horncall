#!/bin/sh
# Starts the Horncall launcher with a real Python 3, for Claude Code's hooks (hooks.json).
#
# A bare "python" is not enough: on Windows the python.org installer adds only the `py` launcher to
# PATH by default, and `python` then opens the Microsoft Store stub ("Python was not found…"), so
# every hook failed (2026-11-09, a user's PC). Claude Code runs this through Git Bash on Windows
# and sh on macOS/Linux. stdin (the hook's JSON) passes through `exec` untouched.
launcher="$(dirname "$(printf '%s' "$0" | tr '\\' '/')")/approver_launcher.py"

# Store aliases live in ...\Microsoft\WindowsApps: there `python` may be the stub (or a real Store
# Python), so those are tried last and only after a check that does not touch stdin.
for name in python3 python; do
    path=$(command -v "$name" 2>/dev/null) || continue
    case "$path" in *WindowsApps*) continue ;; esac
    exec "$name" "$launcher" "$@"
done
if command -v py >/dev/null 2>&1; then
    exec py -3 "$launcher" "$@"
fi
for name in python3 python; do
    if command -v "$name" >/dev/null 2>&1 && "$name" -c "import sys" </dev/null >/dev/null 2>&1; then
        exec "$name" "$launcher" "$@"
    fi
done
echo "Horncall: Python 3 not found. Install it from python.org (tick \"Add python.exe to PATH\")." >&2
exit 1
