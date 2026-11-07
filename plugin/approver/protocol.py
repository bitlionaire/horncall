"""Protocol messages (SPEC section 5): building requests from hook input, risk rating, validation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import PROTOCOL_VERSION

MAX_AGE_S = 600  # messages older than this are rejected (replay window)
MAX_SKEW_S = 120  # clocks may differ a little; messages further "from the future" are rejected
NTFY_MAX_BODY = 4096
BODY_BUDGET = 3900  # leave headroom below the ntfy limit

# (pattern, reason shown on the phone's red card). Case-insensitive, searched in the command text.
DANGER_RULES: list[tuple[str, str]] = [
    # Unix tools are case-sensitive (chmod -R recurses, -r takes read away; git branch -D forces, -d is safe), so
    # their rules are wrapped in (?-i:...); Windows, PowerShell and SQL stay case-insensitive (GitHub issue #1).
    (r"(?-i:\brm\s+(-\w*[rRf]\w*\s+)+)", "Deletes files (rm -r/-f)"),
    (r"(?-i:\brm\b[^\n]*\s--(recursive|force)\b)", "Deletes files (rm --recursive/--force)"),
    (r"\b(rd|rmdir|del|erase)\b[^\n]*\s/[sq]\b", "Deletes files (Windows /s /q)"),
    (r"\b(Remove-Item|ri)\b[^\n]*\s-r(ecurse)?\b", "Deletes folders recursively (PowerShell)"),
    (r"(?-i:\bfind\b[^\n]*\s-delete\b|\bfind\b[^\n]*-exec\s+rm\b|\bxargs\s+rm\b|\bshred\b)", "Deletes files in bulk"),
    (r"(?-i:\bgit\s+push\b[^\n]*(--force\b|\s-f\b|--force-with-lease\b|\s\+\S))", "Force push: rewrites history on the server"),
    (r"(?-i:\bgit\s+push\b[^\n]*(--delete\b|\s:\S))", "Deletes a branch or tag on the server"),
    (r"(?-i:\bgit\s+reset\s+--hard\b)", "Discards uncommitted work (reset --hard)"),
    (r"(?-i:\bgit\s+clean\b[^\n]*(\s-\w*f|--force))", "Deletes untracked files (git clean)"),
    # A delete (-d, --delete, or -D) plus a force (-f, --force, or -D), in any order or cluster: -D, -d -f, -fd, --delete --force.
    (r"(?-i:\bgit\s+branch\b(?=[^\n]*\s(--delete|-[A-Za-z]*[dD][A-Za-z]*)(\s|$))(?=[^\n]*\s(--force|-[A-Za-z]*[fD][A-Za-z]*)(\s|$)))",
     "Deletes a branch with unmerged work"),
    (r"(?-i:\bgit\s+(checkout\s+--|restore)\s+[.*](\s|$))", "Discards all local changes"),
    (r"(?-i:\bgit\s+stash\s+(clear|drop)\b)", "Deletes stashed work"),
    (r"\b(drop|truncate)\s+(table|database|schema|index|user|view|function)\b|\btruncate\s+\w", "Deletes database objects or data"),
    (r"\bdelete\s+from\b(?![^\n]*\bwhere\b)", "Deletes all rows (DELETE without WHERE)"),
    (r"\bupdate\s+\w+\s+set\b(?![^\n]*\bwhere\b)", "Changes all rows (UPDATE without WHERE)"),
    (r"\bmkfs\b|\bformat\s+[a-z]:|\b(Format-Volume|Clear-Disk|Initialize-Disk|diskpart)\b", "Formats or wipes a disk"),
    (r"\bdd\s+if=|>\s*/dev/(sd|nvme|hd)", "Writes raw data to a disk"),
    (r"(?-i:\bchmod\b[^\n]*(\s-R\b[^\n]*\b777\b|\b777\b[^\n]*\s-R\b)|\bchown\s+-R\b)", "Changes permissions recursively"),
    (r"\b(shutdown|reboot|Stop-Computer|Restart-Computer)\b", "Shuts down or restarts the computer"),
    (r"\breg\s+delete\b|\bSet-ExecutionPolicy\b", "Changes system settings"),
    (r"(?-i:\b(curl|wget)\b[^\n]*\|\s*(sudo\s+)?((ba|z)?sh|python3?)\b)", "Runs a script downloaded from the internet"),
    (r"\b(iex|Invoke-Expression)\b", "Runs code from a string (Invoke-Expression)"),
    (r"(?-i:\b(npm\s+(publish|unpublish)|twine\s+upload|gh\s+repo\s+delete)\b)", "Publishes or deletes a public package/repo"),
    (r"(?-i:\b(kubectl\s+delete|terraform\s+(destroy|apply\s+-auto-approve)|helm\s+(uninstall|delete)|docker\s+(system\s+prune|rm\s+-f))\b)", "Destroys infrastructure or containers"),
    (r"(?-i:\baws\s+s3\s+(rm|rb)\b[^\n]*--recursive)", "Deletes cloud storage"),
]
DEFAULT_DANGER_PATTERNS = [pattern for pattern, _ in DANGER_RULES]

# Files whose change can switch off safety (Claude's own permissions, hooks, CI, secrets).
SENSITIVE_PATH = re.compile(
    r"(^|/)(\.claude/settings[^/]*\.json|\.mcp\.json|\.git/(hooks/|config$)|\.github/workflows/|\.env[^/]*$"
    r"|\.gemini/settings[^/]*\.json|\.codebuddy/settings[^/]*\.json|crush/crushrc$|opencode/(plugins/|opencode\.jsonc?$)|\.copilot/(hooks/|config\.json$|mcp-config\.json$)|\.codex/(hooks\.json|config\.toml)$)",
    re.IGNORECASE)

# Tools we know: anything else (MCP servers, other agents' extras) is judged by the verbs in its name.
KNOWN_TOOLS = {"Bash", "PowerShell", "Write", "Edit", "MultiEdit", "NotebookEdit", "Read", "Glob", "Grep",
               "LS", "WebFetch", "WebSearch", "Task", "Agent", "TodoWrite", "NotebookRead",
               "AskUserQuestion", "ExitPlanMode"}
RISKY_TOOL_WORDS = {
    "delete", "remove", "rm", "drop", "destroy", "purge", "wipe", "truncate", "erase", "kill", "terminate",
    "revoke", "reset", "force", "exec", "execute", "eval", "shell", "command", "deploy", "publish", "push",
    "merge", "send", "transfer", "pay", "grant", "permission", "permissions", "admin", "uninstall",
}



RANKS = ["Grunt", "Peon", "Scout", "Ranger", "Knight", "Sapper", "Medic", "Engineer",
         "Archer", "Paladin", "Raider", "Shaman", "Sentry", "Pilot", "Marine", "Tracker"]
NAMES = ["Ironjaw", "Stonefist", "Quickblade", "Redbeard", "Ashbringer", "Grimtooth", "Swiftfoot",
         "Thunderhoof", "Bonecrusher", "Nightshade", "Frostmane", "Stormeye", "Blackthorn",
         "Rustgear", "Copperhelm", "Sparkplug", "Wolfbane", "Emberclaw", "Skullsplitter", "Moonblade"]


# Each AI is a faction with its own ranks, so the barracks show at a glance who asks.
# Claude keeps the original list: existing chats keep their names.
FACTION_RANKS = {
    # Each faction after its AI's character or brand (asked Codex, Gemini and Copilot, 2026-11-05).
    "claude": ["Scribe", "Scholar", "Editor", "Curator", "Archivist", "Chronicler", "Reviewer", "Lorekeeper"],
    "gemini": ["Stargazer", "Astromancer", "Seer", "Oracle", "Comet", "Nova", "Pulsar", "Starwarden"],
    "copilot": ["Pilot", "Navigator", "Gunner", "Wingman", "Aviator", "Spotter", "Bombardier", "Skyguard"],
    "codex": ["Tinker", "Welder", "Mechanic", "Machinist", "Engineer", "Artificer", "Gearwright", "Forgemaster"],
    "opencode": ["Node", "Peer", "Sysop", "Root", "Daemon", "Kernelist", "Patcher", "Forker"],
    "crush": ["Corsair", "Buccaneer", "Deckhand", "Boatswain", "Quartermaster", "Swabbie", "Lookout", "Harpooner"],
    "codebuddy": ["Tamer", "Falconer", "Houndmaster", "Wrangler", "Packleader", "Beastcaller", "Trapper", "Herder"],
}


def codename(session_id: str, agent: str = "claude") -> str:
    """Stable, fun name for an agent session (same session → same name), ranked by its faction."""
    digest = hashlib.sha256(session_id.encode("utf-8")).digest()
    ranks = FACTION_RANKS.get(agent, RANKS)
    return f"{ranks[digest[0] % len(ranks)]} {NAMES[digest[1] % len(NAMES)]}"


def path_key(path: str) -> str:
    """Normalised folder path used to look up project labels."""
    key = os.path.normpath(os.path.abspath(path)).replace("\\", "/").rstrip("/")
    return key.lower() if os.name == "nt" else key


def new_id() -> str:
    return f"{int(time.time() * 1000):x}-{os.urandom(5).hex()}"


def _short(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _is_outside(path: str, cwd: str | None) -> bool:
    if not path or not cwd:
        return False
    try:
        # Gemini sends relative paths ("notes.txt"): they are relative to the project, not to this process.
        Path(cwd, path).resolve().relative_to(Path(cwd).resolve())
        return False
    except ValueError:
        return True
    except OSError:
        return False


def tool_words(tool: str) -> set[str]:
    """The words of a tool name: mcp_github_deleteFile → {mcp, github, delete, file}."""
    return set(re.findall(r"[a-z]+", re.sub(r"([a-z])([A-Z])", r"\1_\2", tool).lower()))


def risk_reasons(tool: str, tool_input: dict, cwd: str | None, patterns: list[str] | None = None) -> list[str]:
    """Why an order is dangerous (empty = normal). `patterns` (config.danger_patterns) replaces the built-ins."""
    reasons: list[str] = []
    if tool in ("Bash", "PowerShell"):
        command = str(tool_input.get("command", ""))
        rules = DANGER_RULES if patterns is None else [(p, "Matches your custom danger rule") for p in patterns]
        for pattern, reason in rules:
            if re.search(pattern, command, re.IGNORECASE) and reason not in reasons:
                reasons.append(reason)
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        # A patch (Codex apply_patch) lists every file it touches; judge them all.
        paths = [str(p) for p in tool_input.get("patch_files") or []] or [str(path)]
        if path == PATCH_UNKNOWN:
            reasons.append("Patch whose files cannot be checked")
            paths = []
        if tool_input.get("patch_deletes"):
            reasons.append("Deletes files")
        if any(_is_outside(p, cwd) for p in paths):
            reasons.append("Writes outside the project folder")
        if any(SENSITIVE_PATH.search(p.replace("\\", "/")) for p in paths):
            reasons.append("Changes permissions, hooks, CI or secrets")
    elif tool not in KNOWN_TOOLS:
        if tool_words(tool) & RISKY_TOOL_WORDS:
            reasons.append(f"Tool may change or delete data ({_short(tool, 40)})")
    return reasons[:4]


PATCH_UNKNOWN = "(patch)"
_PATCH_FILE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.MULTILINE)


def patch_files(patch: str, cwd: str | None) -> tuple[list[str], list[str]]:
    """Files an apply_patch text touches, and the ones it deletes (paths resolved against cwd)."""
    files: list[str] = []
    deletes: list[str] = []
    for op, path, moved in _PATCH_FILE.findall(patch):
        path = path or moved
        full = path if os.path.isabs(path) or not cwd else os.path.join(cwd, path)
        if full not in files:
            files.append(full)
        if op == "Delete":
            deletes.append(full)
    return files, deletes


def rate_risk(tool: str, tool_input: dict, cwd: str | None, patterns: list[str] | None = None) -> str:
    return "danger" if risk_reasons(tool, tool_input, cwd, patterns) else "normal"


def describe(tool: str, tool_input: dict, limit: int) -> tuple[str, str]:
    """Return (title, preview) for the phone card."""
    if tool in ("Bash", "PowerShell"):
        command = str(tool_input.get("command", ""))
        first = command.strip().splitlines()[0] if command.strip() else ""
        return f"{tool}: {_short(first, 60)}", _short(command, limit)
    if tool == "Write":
        path = tool_input.get("file_path", "")
        return f"Write: {Path(path).name}", _short(f"{path}\n\n{tool_input.get('content', '')}", limit)
    if tool == "Edit":
        path = tool_input.get("file_path", "")
        body = f"{path}\n\n- {tool_input.get('old_string', '')}\n+ {tool_input.get('new_string', '')}"
        return f"Edit: {Path(path).name}", _short(body, limit)
    if tool == "MultiEdit":
        path = tool_input.get("file_path", "")
        edits = tool_input.get("edits") or []
        parts = [f"- {e.get('old_string', '')}\n+ {e.get('new_string', '')}" for e in edits]
        return f"MultiEdit: {Path(path).name} ({len(edits)})", _short(path + "\n\n" + "\n\n".join(parts), limit)
    if tool in ("WebFetch", "WebSearch"):
        target = tool_input.get("url") or tool_input.get("query") or ""
        return f"{tool}: {_short(str(target), 60)}", _short(json.dumps(tool_input, ensure_ascii=False, indent=1), limit)
    if tool.startswith("mcp__"):
        _, server, name = (tool.split("__", 2) + ["", ""])[:3]
        return f"MCP {server}: {name}", _short(json.dumps(tool_input, ensure_ascii=False, indent=1), limit)
    if tool == "AskUserQuestion":
        return describe_questions(tool_input, limit)
    if tool == "ExitPlanMode":
        plan = str(tool_input.get("plan") or "").strip()
        return "Plan ready for approval", _short(plan or json.dumps(tool_input, ensure_ascii=False, indent=1), limit)
    return tool, _short(json.dumps(tool_input, ensure_ascii=False, indent=1), limit)


# Tools whose "allow" cannot be answered from the phone: the agent wants a choice or a plan decision, and
# Claude Code needs the answer itself (`updatedInput`), which Horncall does not send. The phone shows them
# readably and offers no approve button; an "allow" that still arrives is dropped by hook.run.
NEEDS_PC_TOOLS = {"AskUserQuestion", "ExitPlanMode"}


def describe_questions(tool_input: dict, limit: int) -> tuple[str, str]:
    """AskUserQuestion as plain text: the question, then its options (label – description)."""
    questions = [q for q in (tool_input.get("questions") or []) if isinstance(q, dict)]
    if not questions:
        return "Question", _short(json.dumps(tool_input, ensure_ascii=False, indent=1), limit)
    blocks = []
    for q in questions:
        head = f"[{q['header']}] " if q.get("header") else ""
        lines = [f"{head}{q.get('question', '')}"]
        for option in q.get("options") or []:
            if not isinstance(option, dict):
                continue
            label, detail = option.get("label", ""), option.get("description", "")
            lines.append(f" • {label}" + (f" – {detail}" if detail else ""))
        if q.get("multiSelect"):
            lines.append(" (several answers allowed)")
        blocks.append("\n".join(lines))
    return f"Question: {_short(str(questions[0].get('question', '')), 60)}", _short("\n\n".join(blocks), limit)


def build_request(hook_input: dict, device: str, timeout_s: int, limit: int,
                  patterns: list[str] | None = None, now: float | None = None,
                  labels: dict[str, str] | None = None, agent: str = "claude") -> dict:
    now = int(now or time.time())
    tool = str(hook_input.get("tool_name", "?"))
    tool_input = hook_input.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {"input": tool_input}
    cwd = hook_input.get("cwd")
    title, preview = describe(tool, tool_input, limit)
    reasons = risk_reasons(tool, tool_input, cwd, patterns)
    request = {
        "v": PROTOCOL_VERSION,
        "type": "request",
        "id": new_id(),
        "ts": now,
        "expires_at": now + timeout_s,
        "device": device,
        "project": Path(cwd).name if cwd else "",
        "tool": tool,
        "agent": agent,
        "risk": "danger" if reasons else "normal",
        "title": title,
        "preview": preview,
    }
    if reasons:
        request["reasons"] = reasons
    if tool in NEEDS_PC_TOOLS:
        request["needs_pc"] = True  # the phone offers no approve button for these
    description = tool_input.get("description")
    if description:
        request["description"] = _short(str(description), 200)
    session_id = hook_input.get("session_id")
    if session_id:
        request["session"] = codename(str(session_id), agent)
    request["fp"] = order_fp(str(session_id) if session_id else None, tool, hook_input.get("tool_input"))
    label = (labels or {}).get(path_key(cwd)) if cwd else None
    if label:
        request["label"] = _short(label, 40)
    return request


def fit_to_budget(request: dict, seal_fn, budget: int = BODY_BUDGET) -> str:
    """Seal the request, shrinking the preview until the envelope fits into one ntfy message."""
    while True:
        body = seal_fn(request)
        if len(body.encode("utf-8")) <= budget or not request.get("preview"):
            return body
        preview = request["preview"]
        request = {**request, "preview": _short(preview, int(len(preview) * 0.8))}


# "pass" = the phone is muted and has no opinion: the agent asks at the PC (an older plugin ignores it and keeps waiting).
DECISIONS = ("allow", "deny", "pass")


def make_response(request_id: str, decision: str, via: str) -> dict:
    return {"v": PROTOCOL_VERSION, "type": "response", "id": request_id,
            "ts": int(time.time()), "decision": decision, "via": via}


def signed_payload(response: dict) -> bytes:
    """What the phone signs: every field that matters for the decision (mirrored in Protocol.kt)."""
    return (f"horncall-sig/1|{response.get('id')}|{response.get('decision')}|{response.get('ts')}"
            f"|{response.get('via')}").encode("utf-8")


def make_status(hook_input: dict, device: str, agent: str, state: str, since: int, worked_s: int | None = None,
                labels: dict[str, str] | None = None) -> dict:
    """A unit started ("working") or finished ("idle") a task — the barracks show it with a timer."""
    cwd = hook_input.get("cwd")
    message = {"v": PROTOCOL_VERSION, "type": "status", "id": new_id(), "ts": int(time.time()),
               "device": device, "agent": agent, "state": state, "since": int(since),
               "project": Path(cwd).name if cwd else ""}
    session_id = hook_input.get("session_id")
    if session_id:
        message["session"] = codename(str(session_id), agent)
    label = (labels or {}).get(path_key(cwd)) if cwd else None
    if label:
        message["label"] = _short(label, 40)
    if worked_s is not None:
        message["worked_s"] = int(worked_s)
    return message


def order_fp(session_id: str | None, tool: str, tool_input) -> str:
    """Same tool call → same fingerprint, so the PostToolUse hook can tell the phone it was answered on the PC.

    A shell call is identified by its command alone: some agents (Copilot) send extra fields, such as a
    description, with the permission question but not after the tool ran.
    """
    if tool in ("Bash", "PowerShell") and isinstance(tool_input, dict):
        tool_input = {"command": tool_input.get("command", "")}
    canon = json.dumps([session_id or "", tool, tool_input], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:20]


def make_resolved(fp: str, request_id: str | None = None) -> dict:
    """The tool call was answered on the PC. `request` names the one order (identical calls may wait
    side by side); phones before 0.5.14 match by `fp` only."""
    message = {"v": PROTOCOL_VERSION, "type": "resolved", "id": f"res-{request_id or fp}", "ts": int(time.time()), "fp": fp}
    if request_id:
        message["request"] = request_id
    return message


def make_cancel(request_id: str, reason: str) -> dict:
    return {"v": PROTOCOL_VERSION, "type": "cancel", "id": request_id,
            "ts": int(time.time()), "reason": reason}


def is_fresh(message: dict, now: float | None = None) -> bool:
    if message.get("v") != PROTOCOL_VERSION:
        return False
    ts = message.get("ts")
    if not isinstance(ts, (int, float)):
        return False
    age = (now or time.time()) - ts
    return -MAX_SKEW_S <= age <= MAX_AGE_S
