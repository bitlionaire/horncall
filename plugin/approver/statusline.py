"""Claude Code status line (opt-in, `approver install claude-statusline`).

Claude Code gives its plan limits (5 h / 7 days) and context use only to the status line, not to
hooks. We note them per session for the phone's HP bar, then show the user's own status line if
they had one (wrapped), or a short one of ours. Stdlib only: it runs after every reply, and the
plugin's launcher imports it with the system Python.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from .config import home_dir


def main() -> int:
    home = home_dir()
    raw = sys.stdin.buffer.read()
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        data = {}
    limits = data.get("rate_limits") or {}
    ctx = data.get("context_window") or {}
    if data.get("session_id"):
        path = home / "claude-vitals.json"
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        saved[str(data["session_id"])] = {"t": int(time.time()), "five_hour": limits.get("five_hour"),
                                          "seven_day": limits.get("seven_day"), "ctx_left": ctx.get("remaining_percentage")}
        newest = sorted(saved.items(), key=lambda kv: kv[1].get("t", 0) if isinstance(kv[1], dict) else 0)[-50:]
        try:
            home.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(dict(newest)), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass
    try:
        wrapped = json.loads((home / "statusline.json").read_text(encoding="utf-8")).get("wrap")
    except (OSError, ValueError):
        wrapped = None
    if wrapped:
        try:
            out = subprocess.run(wrapped, input=raw, capture_output=True, shell=True, timeout=10).stdout
            sys.stdout.buffer.write(out)
        except (OSError, subprocess.SubprocessError):
            pass
        return 0
    parts = []
    five = limits.get("five_hour") or {}
    if isinstance(five.get("used_percentage"), (int, float)):
        parts.append(f"5h {round(five['used_percentage'])}%")
    if isinstance(ctx.get("used_percentage"), (int, float)):
        parts.append(f"ctx {round(ctx['used_percentage'])}%")
    model = (data.get("model") or {}).get("display_name")
    text = "⚔ " + " · ".join(([model] if model else []) + parts) if (parts or model) else "⚔ Horncall"
    sys.stdout.buffer.write(text.encode("utf-8"))  # Windows pipes default to a legacy code page
    return 0
