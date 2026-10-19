"""A unit's "HP": how much of its AI plan limit is left, and how full its chat memory (context) is.

Read from the files each AI already writes on this PC — only numbers and times, never the
conversation. Every reader looks at the END of a file only and returns None on any doubt: a
missing bar on the phone is better than a wrong one, and nothing here may slow down or break a hook.

Shape sent to the phone (all keys optional):
    {"plan_left": 97, "plan_window": "5h", "plan_resets": 1794777480, "ctx_left": 52, "exhausted": true}
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from .config import home_dir

TAIL_BYTES = 256 * 1024
CLAUDE_STATUS_MAX_AGE_S = 6 * 3600  # plan numbers from the status line older than this are dropped


def tail_lines(path: str | os.PathLike | None, size: int = TAIL_BYTES) -> list[str]:
    """Last lines of a (JSONL) file, newest last; [] if unreadable."""
    if not path:
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            f.seek(max(0, end - size))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    return lines[1:] if end > size else lines  # the first line may be cut


def _last_json(lines: list[str], needle: str) -> dict | None:
    for line in reversed(lines):
        if needle in line:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def _pct_left(used: float, total: float) -> int | None:
    if not total or total <= 0:
        return None
    return max(0, min(100, round(100 - used * 100 / total)))


def window_label(minutes: float | None) -> str | None:
    if not minutes:
        return None
    if minutes >= 28 * 24 * 60:
        return "month"
    if minutes >= 24 * 60:
        return f"{round(minutes / 1440)}d"
    return f"{round(minutes / 60)}h"


def _clean(v: dict) -> dict | None:
    v = {k: x for k, x in v.items() if x is not None}
    return v or None


# --- Codex: rollout JSONL (transcript_path), event_msg/token_count ------------------------------

def codex(raw: dict) -> dict | None:
    event = _last_json(tail_lines(raw.get("transcript_path")), '"token_count"')
    payload = (event or {}).get("payload") or {}
    info, limits = payload.get("info") or {}, payload.get("rate_limits") or {}
    last = info.get("last_token_usage") or {}
    vitals = {"ctx_left": _pct_left(last.get("input_tokens", 0), info.get("model_context_window", 0))
              if last else None}
    # The tighter of the short and the long window counts.
    windows = [w for w in (limits.get("primary"), limits.get("secondary")) if isinstance(w, dict)
               and isinstance(w.get("used_percent"), (int, float))]
    if windows:
        tight = max(windows, key=lambda w: w["used_percent"])
        vitals.update(plan_left=max(0, round(100 - tight["used_percent"])),
                      plan_window=window_label(tight.get("window_minutes")), plan_resets=tight.get("resets_at"))
    if limits.get("rate_limit_reached_type"):
        vitals.update(plan_left=0, exhausted=True)
    return _clean(vitals)


# --- GitHub Copilot CLI: ~/.copilot/session-state/<sessionId>/events.jsonl ------------------------

def copilot(raw: dict) -> dict | None:
    session = raw.get("sessionId") or raw.get("session_id")
    if not session or any(c in str(session) for c in "/\\."):
        return None
    home = Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")
    events = home / "session-state" / str(session) / "events.jsonl"
    # Quota comes with model calls, which can sit far from the end of a long session file.
    event = _last_json(tail_lines(events), '"quotaSnapshots"') or _last_matching_line(events, '"quotaSnapshots"')
    if not event:
        return None
    snapshots = _find_key(event, "quotaSnapshots") or {}
    chat = snapshots.get("chat") or {}
    if chat.get("isUnlimitedEntitlement") or not chat.get("entitlementRequests"):
        return None
    left = chat.get("remainingPercentage")
    if not isinstance(left, (int, float)):
        return None
    return _clean({"plan_left": max(0, min(100, round(left))), "plan_window": "month",
                   "plan_resets": _iso_to_unix(chat.get("resetDate")), "exhausted": True if left <= 0 else None})


def _last_matching_line(path: Path, needle: str, max_bytes: int = 32 * 1024 * 1024) -> dict | None:
    """Whole-file scan (bounded) for the last JSON line containing needle."""
    try:
        if path.stat().st_size > max_bytes:
            return None
        found = None
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if needle in line:
                    found = line
        return json.loads(found) if found else None
    except (OSError, ValueError):
        return None


def _find_key(obj, key: str, depth: int = 0):
    if depth > 6:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _find_key(value, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj[:50]:
            found = _find_key(value, key, depth + 1)
            if found is not None:
                return found
    return None


def _iso_to_unix(text) -> int | None:
    try:
        return int(datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


# --- Gemini CLI: chat JSONL (transcript_path) -----------------------------------------------------

GEMINI_WINDOW = 1_048_576  # every current Gemini model (flash, pro)


def gemini(raw: dict) -> dict | None:
    lines = tail_lines(raw.get("transcript_path"))
    if not lines:
        return None
    last_tokens = last_error = -1
    tokens = None
    for i, line in enumerate(lines):
        if '"tokens"' in line:
            try:
                tokens = json.loads(line).get("tokens") or tokens
                last_tokens = i
            except ValueError:
                pass
        elif "exhausted your daily quota" in line or "RESOURCE_EXHAUSTED" in line:
            last_error = i
    vitals = {"ctx_left": _pct_left(tokens.get("input", 0), GEMINI_WINDOW) if isinstance(tokens, dict) else None}
    if last_error > last_tokens:  # the newest thing it said is "out of quota"
        vitals.update(plan_left=0, plan_window="1d", exhausted=True)
    return _clean(vitals)


# --- Claude Code: transcript usage (context) + our status line (plan) -----------------------------

CLAUDE_WINDOW = 200_000


def claude_status_file() -> Path:
    return home_dir() / "claude-vitals.json"


def claude(raw: dict) -> dict | None:
    vitals: dict = {}
    session = raw.get("session_id")
    status = None
    try:
        status = json.loads(claude_status_file().read_text(encoding="utf-8")).get(str(session))
    except (OSError, ValueError, AttributeError):
        pass
    fresh = isinstance(status, dict) and time.time() - status.get("t", 0) < CLAUDE_STATUS_MAX_AGE_S
    if fresh:
        windows = [(name, w) for name, w in (("5h", status.get("five_hour")), ("7d", status.get("seven_day")))
                   if isinstance(w, dict) and isinstance(w.get("used_percentage"), (int, float))]
        if windows:
            label, tight = max(windows, key=lambda nw: nw[1]["used_percentage"])
            vitals.update(plan_left=max(0, round(100 - tight["used_percentage"])), plan_window=label,
                          plan_resets=tight.get("resets_at"))
        if isinstance(status.get("ctx_left"), (int, float)) and time.time() - status.get("t", 0) < 600:
            vitals["ctx_left"] = round(status["ctx_left"])
    if "ctx_left" not in vitals:
        # Internal transcript format (may change): the last reply's prompt size.
        entry = _last_json(tail_lines(raw.get("transcript_path")), '"usage"')
        usage = ((entry or {}).get("message") or {}).get("usage") or {}
        if usage:
            used = sum(usage.get(k, 0) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                                     "cache_read_input_tokens"))
            model = str(((entry or {}).get("message") or {}).get("model", ""))
            window = 1_000_000 if "[1m]" in model or used > CLAUDE_WINDOW else CLAUDE_WINDOW
            vitals["ctx_left"] = _pct_left(used, window)
    return _clean(vitals)


READERS = {"claude": claude, "codex": codex, "copilot": copilot, "gemini": gemini}


def read(agent: str, raw: dict) -> dict | None:
    """Never raises: a failed read just means no HP bar."""
    try:
        reader = READERS.get(agent)
        return reader(raw) if reader and isinstance(raw, dict) else None
    except Exception:
        return None
