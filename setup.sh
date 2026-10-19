#!/bin/sh
# Horncall setup: hooks up your AI tools, pairs the phone and sends a test order.
# It copies Horncall to the standard program folder - this folder can be deleted afterwards.
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then
  echo "Python 3.10 or newer is needed: https://www.python.org/downloads/"
  exit 1
fi
exec "$PY" plugin/hooks/approver_launcher.py setup
