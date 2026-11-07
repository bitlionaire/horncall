"""The start of the AI's last answer, shown with "mission complete" on the phone (opt-in: Config.send_replies).

Unlike vitals.py this reads what the AI said, so it runs only when the user switched it on. It is read from the
end of the chat file each AI already writes, plain text only (no code blocks, no markdown), cut to MAX_CHARS,
and sent encrypted like everything else. Any doubt (unknown format, empty answer) returns None: no preview.
"""

from __future__ import annotations

import json
import re

from .vitals import copilot_events, tail_lines

MAX_CHARS = 240


def _last(lines: list[str], needle: str, pick) -> str | None:
    """Newest line containing needle whose pick(entry) gives text."""
    for line in reversed(lines):
        if needle not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        text = pick(entry) if isinstance(entry, dict) else None
        if isinstance(text, str) and text.strip():
            return text
    return None


def _text_blocks(content, kind: str) -> str | None:
    if not isinstance(content, list):
        return None
    parts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == kind]
    return "\n".join(p for p in parts if isinstance(p, str)) or None


# --- per AI: where the last answer sits (checked on real files 2026-11-02) ---------------------------

def claude(raw: dict) -> str | None:
    # {"type": "assistant", "message": {"content": [{"type": "text", "text": ...}, {"type": "tool_use"}...]}}
    return _last(tail_lines(raw.get("transcript_path")), '"assistant"',
                 lambda e: _text_blocks((e.get("message") or {}).get("content"), "text")
                 if e.get("type") == "assistant" else None)


def codex(raw: dict) -> str | None:
    # {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text"}]}}
    def pick(e):
        p = e.get("payload") or {}
        if e.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "assistant":
            return _text_blocks(p.get("content"), "output_text")
        if p.get("type") == "task_complete":
            return p.get("last_agent_message")
        return None
    # Its Stop hook hands the answer over itself (seen 2026-11-05); the chat file is the fallback.
    given = raw.get("last_assistant_message")
    if isinstance(given, str) and given.strip():
        return given
    return _last(tail_lines(raw.get("transcript_path")), '"payload"', pick)  # the newer of the two kinds


def gemini(raw: dict) -> str | None:
    # {"type": "gemini", "content": "...", "thoughts": [...], "tokens": {...}}; empty content = a tool call
    return _last(tail_lines(raw.get("transcript_path")), '"gemini"',
                 lambda e: e.get("content") if e.get("type") == "gemini" else None)


def copilot(raw: dict) -> str | None:
    # ~/.copilot/session-state/<id>/events.jsonl: {"type": "assistant.message", "data": {"content": "..."}}
    session = raw.get("sessionId") or raw.get("session_id")
    if not session or any(c in str(session) for c in "/\\."):
        return None
    return _last(tail_lines(copilot_events(session)), '"assistant.message"',
                 lambda e: (e.get("data") or {}).get("content") if e.get("type") == "assistant.message" else None)


def given_answer(raw: dict) -> str | None:
    """Crush (TurnEnd) and CodeBuddy (Stop) hand the final answer over in the hook input itself."""
    given = raw.get("last_assistant_message")
    return given if isinstance(given, str) and given.strip() else None


READERS = {"claude": claude, "codex": codex, "copilot": copilot, "gemini": gemini, "crush": given_answer,
           "codebuddy": given_answer}


def plain(text: str, limit: int = MAX_CHARS) -> str | None:
    """Markdown answer -> one short plain line: code blocks dropped, marks removed, whitespace folded."""
    text = re.sub(r"(```|~~~).*?(\1|$)", " ", text, flags=re.S)  # fenced code, either fence
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [label](link) -> label
    text = re.sub(r"^\s{0,3}(#{1,6}|>|[-*+]|\d+\.)\s+", "", text, flags=re.M)  # headings, quotes, list marks
    text = re.sub(r"(?<!\w)_{1,2}(\S(?:.*?\S)?)_{1,2}(?!\w)", r"\1", text)  # _emphasis_, not snake_case
    text = re.sub(r"[*`~|]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] if " " in text[:limit] else text[:limit]
    return cut.rstrip(" ,;:-") + "…"


def read(agent: str, raw: dict) -> str | None:
    """Never raises: a failed read just means no preview."""
    try:
        reader = READERS.get(agent)
        text = reader(raw) if reader and isinstance(raw, dict) else None
        return plain(text) if text else None
    except Exception:
        return None
