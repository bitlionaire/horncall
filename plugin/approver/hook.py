"""Permission hook for every supported AI agent (SPEC section 3.4; adapters in agents.py).

Contract: never prompt, always exit 0. Printing nothing leaves the agent's normal permission
dialog in charge; printing a decision object answers the request.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import TextIO

from . import agents, compat, crypto, phones, protocol, vitals
from .config import Config, State, home_dir, log_path
from .link import Link


def decision_output(behavior: str, message: str | None = None) -> dict:
    """Claude Code's answer shape (kept for callers and tests that use it directly)."""
    claude = agents.get("claude")
    return claude.allow() if behavior == "allow" else claude.deny(message or "")


def log(event: str, **fields) -> None:
    """Metadata-only log (no commands or file contents) — useful to verify the hook fires."""
    try:
        line = json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields})
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --- Capture mode (HORNCALL_CAPTURE=1): real input samples for tests/fixtures/agents (docs/AGENTS.md) ---

# Values worth keeping: they name things, they don't carry the user's commands, paths or text.
CAPTURE_KEEP = {"tool_name", "toolName", "hook_event_name", "hookEventName", "hookName", "permission_mode",
                "agentType", "stopReason", "kind", "tool", "type", "model", "stop_hook_active"}


def _redact(value, key: str = ""):
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, key) for v in value]
    if isinstance(value, str) and key not in CAPTURE_KEEP:
        return f"<redacted:{len(value)}>"
    return value


def _agent_version(agent: str) -> str:
    try:
        return compat.installed_version(compat.load()[agent]) or "unknown"
    except Exception:
        return "unknown"


def capture(agent: str, kind: str, raw: dict) -> None:
    """Save the input's shape (values redacted) with the agent's version. Off unless HORNCALL_CAPTURE=1."""
    if os.environ.get("HORNCALL_CAPTURE") != "1":
        return
    try:
        version = _agent_version(agent)
        name = str(raw.get("tool_name") or raw.get("toolName") or kind)
        folder = home_dir() / "captures" / agent / version
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{kind}-{re.sub(r'[^A-Za-z0-9_.-]', '_', name)[:40]}-{time.strftime('%Y%m%d-%H%M%S')}.json"
        sample = {"agent": agent, "agent_version": version, "kind": kind,
                  "captured": time.strftime("%Y-%m-%d"), "source": "capture", "input": _redact(raw)}
        path.write_text(json.dumps(sample, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass  # capturing must never break the agent


def run(stdin: TextIO, stdout: TextIO, link_factory=Link, agent: str = "claude") -> int:
    adapter = agents.get(agent)
    try:
        raw = json.loads(stdin.read() or "{}")
    except ValueError:
        log("bad-input", agent=adapter.name)
        return 0
    if not isinstance(raw, dict):
        return 0
    capture(adapter.name, "permission", raw)
    if adapter.name != "claude":
        # Field names only (never values): lets us confirm each agent's input shape from a real run.
        tool_args = raw.get("toolInput") or raw.get("toolArgs") or raw.get("tool_input")
        log("input-shape", agent=adapter.name, keys=sorted(raw)[:30],
            tool=str(raw.get("toolName") or raw.get("tool_name") or "")[:40],
            input_keys=sorted(tool_args)[:20] if isinstance(tool_args, dict) else type(tool_args).__name__)

    state = State.load()
    config = Config.load()
    if not state.is_on() or config is None:
        log("skip", agent=adapter.name, mode=state.mode, paired=config is not None)
        return 0
    hook_input = adapter.parse(raw)
    if hook_input is None:
        return 0  # not a question for the phone (e.g. Gemini reading a file)

    wait_s = min(config.timeout_s, adapter.max_wait_s)
    request = protocol.build_request(hook_input.as_claude(), config.device_name, wait_s,
                                     config.max_preview_chars, config.danger_patterns,
                                     labels=config.labels, agent=adapter.name)
    unit_vitals = vitals.read(adapter.name, raw)
    if unit_vitals:
        request["vitals"] = unit_vitals  # the unit's HP: plan limit left, chat memory left
    if state.on_until:
        # The phone shows the remaining duty time as a mana bar.
        request["duty_until"] = int(state.on_until)
        request["duty_since"] = int(state.on_since or request["ts"])
    link = link_factory(config.key, config.server)
    deadline = time.time() + wait_s
    log("request", id=request["id"], agent=adapter.name, tool=hook_input.tool, risk=request["risk"],
        session=request.get("session"), fp=request["fp"])

    def accept(response: dict) -> bool:
        verdict = phones.judge(config, response)
        if verdict == "joined":
            log("phone-joined", key=crypto.fingerprint(response["pub"]))
        elif verdict != "ok":
            log("rejected", id=request["id"], reason=verdict)  # e.g. someone with a copy of the key
        return verdict in ("ok", "joined")

    # While we wait, the same question is open on the PC. If it's answered there, the agent may kill
    # this process (the marker stays and the PostToolUse / Stop hook tells the phone, see posttool) or
    # leave it running (Claude Code does): then posttool / run_event removes the marker and we give up.
    marker = _pending_marker(request["fp"], hook_input.session_id, request["id"], hook_input.tool)
    answered_on_pc = False
    try:
        response = link.request_and_wait(request, deadline, accept=accept,
                                         stop=(lambda: not marker.exists()) if marker is not None else None)
        answered_on_pc = response is None and marker is not None and not marker.exists()
    except Exception as exc:  # network trouble must never break the agent
        log("error", id=request["id"], error=exc.__class__.__name__)
        response = None
    finally:
        _drop(marker)

    if answered_on_pc:
        # The card is already gone from the phone (it was told "resolved"): nothing to cancel, nothing to say.
        log("answered-on-pc", id=request["id"])
        return 0

    if response is None:
        log("timeout", id=request["id"], on_timeout=config.on_timeout)
        try:
            link.send_cancel(request["id"], "timeout")  # the card leaves the phone too
        except Exception:
            pass
        if config.on_timeout == "deny":
            _emit(stdout, adapter.deny("No answer from the phone (Horncall timeout)."))
        return 0

    log("response", id=request["id"], decision=response["decision"], via=response.get("via"))
    if response["decision"] == "pass":
        # The phone is muted ("ask me at the PC"): no opinion, the agent's own dialog decides. The phone
        # showed no card, so there is nothing to cancel.
        log("passed-by-phone", id=request["id"], via=response.get("via"))
        return 0
    if response["decision"] == "allow" and request.get("needs_pc"):
        # A choice or a plan decision: Claude Code needs the answer itself, a bare allow answers nothing.
        # Say nothing (the question stays on the PC) and take the card off the phone quietly.
        log("needs-pc", id=request["id"], tool=hook_input.tool)
        try:
            link.send_resolved(request["fp"], request["id"])
        except Exception:
            pass
        return 0
    if response["decision"] == "allow":
        _emit(stdout, adapter.allow())
    else:
        via = response.get("via", "phone")
        _emit(stdout, adapter.deny(f"The user denied this from their phone ({via})."))
    return 0


MAX_TASK_S = 6 * 3600  # longer "tasks" are sessions that died mid-task: no worked_s is sent
TASK_FORGET_S = 12 * 3600  # task starts older than this are dropped from tasks.json silently


def run_event(stdin: TextIO, agent: str, state_name: str, link_factory=Link) -> int:
    """Task start/end hooks (Claude: UserPromptSubmit / Stop): tell the phone the unit's work state.

    Only while the mode is on — at the desk you see it yourself, and it would only use up messages.
    Never prints anything and always exits 0, so the agent carries on untouched.
    """
    adapter = agents.get(agent)
    try:
        raw = json.loads(stdin.read() or "{}")
    except ValueError:
        return 0
    if isinstance(raw, dict):
        capture(adapter.name, state_name, raw)
    state = State.load()
    config = Config.load()
    if not isinstance(raw, dict) or not state.is_on() or config is None:
        return 0
    session_id = raw.get("session_id") or raw.get("sessionId")
    if adapter.name != "claude":
        # Field names and a short session id only: shows whether start and end report the same chat.
        log("event-shape", agent=adapter.name, state=state_name, keys=sorted(raw)[:30],
            session=str(session_id or "")[:8], transcript=Path(str(raw.get("transcript_path") or "")).name[:60])
    if state_name == "working":
        _resolve_session_markers(str(session_id) if session_id else None, adapter.name, config, link_factory)
    now = int(time.time())
    transcript = str(raw.get("transcript_path") or "")
    started, task_session = _task_clock(str(session_id or "?"), state_name, now, transcript)
    if task_session and task_session != str(session_id):
        # The end came with another session id than the start (seen with Gemini CLI) but from the same
        # chat file: report it for the unit that started, so its timer stops on the phone.
        log("session-matched-by-transcript", agent=adapter.name)
        session_id = task_session
    if state_name == "idle":
        _clear_session_markers(session_id)
    hook_input = {"session_id": session_id, "cwd": raw.get("cwd")}
    worked = now - started if state_name == "idle" and started else None
    if worked is not None and worked > MAX_TASK_S:
        # A session killed mid-task never says idle; a late idle would count the whole gap as work.
        log("worked-capped", agent=adapter.name, worked_s=worked)
        worked = None
    status = protocol.make_status(hook_input, config.device_name, adapter.name, state_name, now, worked, config.labels)
    unit_vitals = vitals.read(adapter.name, raw)
    if unit_vitals:
        status["vitals"] = unit_vitals
    try:
        link_factory(config.key, config.server).send_status(status)
        log("status", agent=adapter.name, state=state_name, worked_s=worked, session=status.get("session"))
    except Exception as exc:
        log("error", event="status", error=exc.__class__.__name__)
    return 0


def _task_clock(session_id: str, state_name: str, now: int, transcript: str = "") -> tuple[int | None, str | None]:
    """Remembers when each session's current task started. On idle returns (start, session it belongs to)
    and forgets it — matched by session id, else by the chat's transcript file."""
    path = log_path().with_name("tasks.json")
    try:
        tasks = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        tasks = {}
    # Entries were plain start times before 0.5.13 of the plugin; now {"t": start, "tr": transcript}.
    tasks = {k: (v if isinstance(v, dict) else {"t": v, "tr": ""}) for k, v in tasks.items()}
    owner: str | None = session_id
    if state_name == "working":
        tasks.setdefault(session_id, {"t": now, "tr": transcript})
        started = tasks[session_id]["t"]
    else:
        entry = tasks.pop(session_id, None)
        if entry is None and transcript:
            owner = next((k for k, v in tasks.items() if v.get("tr") == transcript), None)
            entry = tasks.pop(owner, None) if owner else None
        started = entry["t"] if entry else None
    tasks = {k: v for k, v in tasks.items() if now - v["t"] < TASK_FORGET_S}  # forget abandoned sessions
    try:
        path.write_text(json.dumps(tasks), encoding="utf-8")
    except OSError:
        pass
    return started, (owner if started is not None else None)


def pending_dir():
    return log_path().with_name("pending")


def _pending_marker(fp: str, session_id: str | None, request_id: str, tool: str | None = None):
    # One marker per request: two identical calls running at once each keep their own.
    path = pending_dir() / f"{fp}.{request_id}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"session": session_id, "id": request_id, "t": int(time.time()), "tool": tool, "fp": fp}),
                        encoding="utf-8")
    except OSError:
        return None
    return path


def _drop(path) -> None:
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def run_posttool(stdin: TextIO, agent: str, link_factory=Link) -> int:
    """PostToolUse: if this tool call was still waiting on the phone, it was answered on the PC —
    tell the phone to drop the card. Silent, always exits 0."""
    adapter = agents.get(agent)
    try:
        raw = json.loads(stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(raw, dict):
        return 0
    capture(adapter.name, "posttool", raw)
    hook_input = adapter.parse(raw)
    config = Config.load()
    if hook_input is None or config is None:
        return 0
    fp = protocol.order_fp(hook_input.session_id, hook_input.tool, hook_input.tool_input)
    folder = pending_dir()
    markers = sorted(folder.glob(f"{fp}.*"), key=_mtime) if folder.exists() else []
    if not markers:
        marker = _lone_marker(hook_input.session_id, hook_input.tool)
        if marker is not None:
            # Some tools come back with other input than they were asked with (AskUserQuestion adds the
            # answers): the only waiting call of this chat and tool is the one that just ran.
            _resolve(marker, adapter.name, config, link_factory, via="same-tool")
            return 0
        # Runs only while some order waits (the launcher skips it otherwise): a call that is not one of
        # them is normal (parallel tools), but this line tells a real mismatch apart when a card stays.
        waiting = len(list(folder.glob("*"))) if folder.exists() else 0
        if waiting:
            log("posttool-other", agent=adapter.name, tool=hook_input.tool, fp=fp, waiting=waiting)
        return 0
    _resolve(markers[0], adapter.name, config, link_factory)  # the oldest identical call is the one that just ran
    return 0


def _mtime(path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0  # removed meanwhile by the hook that owned it


def _marker_data(path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _lone_marker(session_id: str | None, tool: str):
    """The one waiting order of this chat for this tool, or None (none, or several: no guessing).
    Never for shell commands: a wrong guess would take a card down while the agent still waits for it."""
    folder = pending_dir()
    if tool in ("Bash", "PowerShell") or not folder.exists():
        return None
    same = [p for p in folder.iterdir() if (d := _marker_data(p)).get("session") == session_id and d.get("tool") == tool]
    return same[0] if len(same) == 1 else None


def _resolve(marker, agent: str, config, link_factory, via: str = "fp") -> None:
    """The call behind this marker was answered on the PC: drop the marker and the phone's card."""
    fp, _, request_id = marker.name.partition(".")
    data = _marker_data(marker)
    request_id = data.get("id") or request_id
    _drop(marker)
    try:
        link_factory(config.key, config.server).send_resolved(fp, request_id)
        log("resolved-on-pc", agent=agent, fp=fp, id=request_id, via=via)
    except Exception as exc:
        log("error", event="resolved", error=exc.__class__.__name__)


def _resolve_session_markers(session_id: str | None, agent: str, config, link_factory) -> None:
    """A new task started in this chat: whatever it still "asked" was answered on the PC (or refused there,
    which no hook reports). The phone drops those cards."""
    folder = pending_dir()
    if not folder.exists() or not session_id:
        return
    for marker in sorted(folder.iterdir(), key=_mtime):
        if _marker_data(marker).get("session") == session_id:
            _resolve(marker, agent, config, link_factory, via="new-task")


def _clear_session_markers(session_id: str | None) -> None:
    """The chat finished its turn: nothing of it is waiting any more (the phone clears its cards itself)."""
    folder = pending_dir()
    if not folder.exists():
        return
    now = time.time()
    for path in folder.iterdir():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("session") == session_id or now - data.get("t", 0) > 2 * 3600:
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


def _emit(stdout: TextIO, payload: dict) -> None:
    stdout.write(json.dumps(payload))
    stdout.flush()


def main(agent: str = "claude") -> int:
    stdin = open(sys.stdin.fileno(), encoding="utf-8", closefd=False)
    stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", closefd=False)
    return run(stdin, stdout, agent=agent)


def main_posttool(agent: str) -> int:
    return run_posttool(open(sys.stdin.fileno(), encoding="utf-8", closefd=False), agent)


def main_event(agent: str, state_name: str) -> int:
    return run_event(open(sys.stdin.fileno(), encoding="utf-8", closefd=False), agent, state_name)
