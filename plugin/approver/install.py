"""`approver install <agent>`: registers our hook in another AI agent's own configuration.

Claude Code gets its hooks from the plugin's hooks.json — or, for a pip/pipx install without the
plugin, from `install claude` (~/.claude/settings.json). Every change is idempotent, keeps the
agent's other settings untouched and makes a one-time backup of a file it edits. `preview(agent)`
shows exactly what an install would write, without writing anything (`install <agent> --print`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = PLUGIN_ROOT / "hooks" / "approver_launcher.py"
MARK = "horncall"  # our hook entries carry this name, so we find (and only touch) our own
# Our commands, also where an entry can't carry a name (Claude Code settings): launcher or `horncall`.
OUR_COMMAND = re.compile(r"(approver_launcher\.py|horncall(\.exe)?)[\"']?\s+(hook|event|posttool|statusline)\b", re.I)


def packaged() -> bool:
    """Installed with pip/pipx (`horncall` command) rather than as the plugin folder with its launcher."""
    return not LAUNCHER.exists()


def target() -> Path:
    """What our hooks run: the plugin's launcher, or the `horncall` command of this pip/pipx install."""
    if not packaged():
        return LAUNCHER
    scripts = Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")
    exe = scripts / ("horncall.exe" if os.name == "nt" else "horncall")
    return exe if exe.exists() else Path(shutil.which("horncall") or exe)


def hook_command(agent: str, args: str | None = None, powershell: bool = False) -> str:
    """The command line the agent runs: the system Python (not our venv) + the stdlib launcher, or
    the `horncall` command of a pip/pipx install.

    Paths are quoted only when they contain spaces. For agents that run hooks through PowerShell
    (Gemini CLI on Windows), a quoted first word is just a string there, so it gets the `&` call operator.
    """
    quote = lambda p: f'"{p}"' if " " in str(p) else str(p)  # noqa: E731
    if packaged():
        line = f"{quote(target())} {args or f'hook {agent}'}"
    else:
        python = getattr(sys, "_base_executable", None) or sys.executable
        line = f"{quote(python)} {quote(LAUNCHER)} {args or f'hook {agent}'}"
    if powershell and os.name == "nt" and line.startswith('"'):
        line = "& " + line
    return line


def posix_command(args: str) -> str:
    """The command line for agents that run hooks through a POSIX shell, also on Windows (Crush's own shell, Git Bash
    for CodeBuddy): there a bare backslash escapes the next letter, so paths go in single quotes with forward slashes."""
    sq = lambda p: "'" + str(p).replace("\\", "/").replace("'", "'\\''") + "'"  # noqa: E731
    if packaged():
        return f"{sq(target())} {args}"
    python = getattr(sys, "_base_executable", None) or sys.executable
    return f"{sq(python)} {sq(LAUNCHER)} {args}"


def points_here(command: str) -> bool:
    """Our hook entry runs THIS installation (not an older copy somewhere else)."""
    return str(target()).lower().replace("\\", "/") in command.lower().replace("\\", "/")


def _ours(hook: dict) -> bool:
    return hook.get("name") == MARK or bool(OUR_COMMAND.search(str(hook.get("command", ""))))


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".horncall-backup")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".horncall-tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _without_ours(entries: list) -> list:
    """Drops our hook definitions from an event's list (and definitions left empty by that)."""
    out = []
    for entry in entries or []:
        hooks = [h for h in entry.get("hooks", []) if not _ours(h)]
        if hooks or not entry.get("hooks"):
            out.append({**entry, "hooks": hooks} if "hooks" in entry else entry)
    return out


def _our_commands(entries_by_event: dict) -> list[str]:
    return [h.get("command", "") for entries in entries_by_event.values() for e in entries or []
            for h in e.get("hooks", []) if _ours(h)]


def _merge(path: Path, ours: dict) -> dict:
    """The settings file with our definitions (event → definition) replacing any older ones of ours."""
    data = _load_json(path)
    hooks = data.setdefault("hooks", {})
    for event, definition in ours.items():
        hooks[event] = _without_ours(hooks.get(event, [])) + [definition]
    return data


def _remove(path: Path, events) -> bool:
    data = _load_json(path)
    hooks = data.get("hooks", {})
    changed = False
    for event in events:
        if event in hooks:
            kept = _without_ours(hooks[event])
            if kept != hooks[event]:
                changed = True
                if kept:
                    hooks[event] = kept
                else:
                    del hooks[event]
    if changed:
        _save_json(path, data)
    return changed


# --- Gemini CLI: ~/.gemini/settings.json → hooks -------------------------------------------------

def gemini_settings() -> Path:
    return Path(os.environ.get("GEMINI_CLI_HOME") or Path.home() / ".gemini") / "settings.json"


GEMINI_MATCHER = "run_shell_command|write_file|replace|mcp_.*"  # MCP: agents.Gemini decides which ones

# event → (matcher, command args after the launcher, timeout ms, description)
GEMINI_HOOKS = {
    "BeforeTool": (GEMINI_MATCHER, "hook gemini", 1_900_000, "Ask the phone (Horncall) before risky tools"),
    # After a tool ran: a question answered in the terminal leaves the phone at once, not at the end of the task.
    "AfterTool": (GEMINI_MATCHER, "posttool gemini", 15_000, "Drop the phone card (Horncall) of a question answered here"),
    "BeforeAgent": (None, "event gemini working", 15_000, "Tell the phone (Horncall) a task started"),
    "AfterAgent": (None, "event gemini idle", 15_000, "Tell the phone (Horncall) a task finished"),
}


def gemini_definitions() -> dict:
    out = {}
    for event, (matcher, args, timeout, description) in GEMINI_HOOKS.items():
        definition = {"hooks": [{"type": "command", "name": MARK, "command": hook_command("gemini", args, powershell=True),
                                 "timeout": timeout, "description": description}]}
        out[event] = {"matcher": matcher, **definition} if matcher else definition
    return out


def install_gemini() -> Path:
    # Hooks are on by default since Gemini CLI 0.27.0 (toggle: hooksConfig.enabled); we don't touch it.
    path = gemini_settings()
    _save_json(path, _merge(path, gemini_definitions()))
    return path


def uninstall_gemini() -> bool:
    return _remove(gemini_settings(), GEMINI_HOOKS)


def gemini_commands() -> list[str]:
    try:
        return _our_commands(_load_json(gemini_settings()).get("hooks", {}))
    except ValueError:
        return []


def gemini_installed() -> bool:
    return bool(gemini_commands())


# --- GitHub Copilot CLI: our own file in ~/.copilot/hooks/ ---------------------------------------

def copilot_hook_file() -> Path:
    home = Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")
    return home / "hooks" / "horncall.json"


def copilot_file_content() -> dict:
    # Copilot runs `command` through PowerShell on Windows, where a quoted path (C:\Program Files\…) is just
    # a string: separate bash / powershell lines, the PowerShell one with the `&` call operator.
    def entry(args: str, timeout: int) -> list:
        return [{"type": "command", "bash": hook_command("copilot", args),
                 "powershell": hook_command("copilot", args, powershell=True), "timeoutSec": timeout}]

    return {"version": 1, "hooks": {
        "permissionRequest": entry("hook copilot", 1900),
        # Task start/end for the work timer and HP; after-tool drops a phone card answered in the terminal.
        "userPromptSubmitted": entry("event copilot working", 15),
        "agentStop": entry("event copilot idle", 15),
        "postToolUse": entry("posttool copilot", 15),
        "postToolUseFailure": entry("posttool copilot", 15),
    }}


def install_copilot() -> Path:
    path = copilot_hook_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(copilot_file_content(), indent=2) + "\n", encoding="utf-8")
    return path


def uninstall_copilot() -> bool:
    path = copilot_hook_file()
    if not path.exists():
        return False
    path.unlink()
    return True


def copilot_commands() -> list[str]:
    try:
        hooks = _load_json(copilot_hook_file()).get("hooks", {})
    except ValueError:
        return []
    return [e.get("powershell") or e.get("bash") or e.get("command", "") for entries in hooks.values() for e in entries or []]


def copilot_installed() -> bool:
    return copilot_hook_file().exists()


# --- OpenAI Codex CLI: ~/.codex/hooks.json (Claude Code's hook format) ---------------------------

def codex_hooks_file() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "hooks.json"


CODEX_HOOKS = {
    "PermissionRequest": ("*", "hook codex", 600),
    # Its questions to the user need no approval: PreToolUse sends the card and returns at once (hook.run).
    "PreToolUse": ("request_user_input|request_user_input_async", "hook codex", 15),
    # Also runs after a command that exits non-zero (docs): a question answered in the terminal leaves the phone.
    "PostToolUse": ("*", "posttool codex", 15),
    "UserPromptSubmit": (None, "event codex working", 15),
    "Stop": (None, "event codex idle", 15),
}


def codex_definitions() -> dict:
    out = {}
    for event, (matcher, args, timeout) in CODEX_HOOKS.items():
        hook = {"type": "command", "name": MARK, "command": hook_command("codex", args), "timeout": timeout}
        if event == "PermissionRequest":
            # Shown by Codex instead of "Running hook" while the phone is asked (it shows no question of its own then).
            hook["statusMessage"] = "Horncall: answer on your phone or in the Horncall window"
        definition = {"hooks": [hook]}
        out[event] = {"matcher": matcher, **definition} if matcher else definition
    return out


def install_codex() -> Path:
    path = codex_hooks_file()
    _save_json(path, _merge(path, codex_definitions()))
    return path


def uninstall_codex() -> bool:
    return _remove(codex_hooks_file(), CODEX_HOOKS)


def codex_commands() -> list[str]:
    try:
        return _our_commands(_load_json(codex_hooks_file()).get("hooks", {}))
    except ValueError:
        return []


def codex_installed() -> bool:
    return bool(codex_commands())


# --- Claude Code status line: plan limits for the HP bar (opt-in) ----------------------------------
# Claude Code gives the 5 h / 7 day limits only to the status line. Ours notes them and shows the
# user's previous status line unchanged (wrapped), or a short one of its own.

def claude_settings() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"


def _wrap_file() -> Path:
    from .config import home_dir
    return home_dir() / "statusline.json"


def _is_ours(status_line) -> bool:
    return isinstance(status_line, dict) and bool(OUR_COMMAND.search(str(status_line.get("command", "")))) \
        and "statusline" in str(status_line.get("command", ""))


def claude_statusline_definition() -> dict:
    return {"statusLine": {"type": "command", "command": hook_command("claude", "statusline")}}


def install_claude_statusline() -> Path:
    path = claude_settings()
    data = _load_json(path)
    current = data.get("statusLine")
    if not _is_ours(current) or not points_here(str(current.get("command", ""))):
        if not _is_ours(current):
            wrap = current.get("command") if isinstance(current, dict) and current.get("type") == "command" else None
            target = _wrap_file()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"wrap": wrap, "previous": current}), encoding="utf-8")
        data.update(claude_statusline_definition())
        _save_json(path, data)
    return path


def uninstall_claude_statusline() -> bool:
    path = claude_settings()
    data = _load_json(path)
    if not _is_ours(data.get("statusLine")):
        return False
    try:
        previous = json.loads(_wrap_file().read_text(encoding="utf-8")).get("previous")
    except (OSError, ValueError):
        previous = None
    if previous:
        data["statusLine"] = previous
    else:
        data.pop("statusLine", None)
    _save_json(path, data)
    _wrap_file().unlink(missing_ok=True)
    return True


def claude_statusline_commands() -> list[str]:
    try:
        line = _load_json(claude_settings()).get("statusLine")
    except ValueError:
        return []
    return [str(line.get("command", ""))] if _is_ours(line) else []


def claude_statusline_installed() -> bool:
    return bool(claude_statusline_commands())


# --- Claude Code without the plugin (pip/pipx install): hooks in ~/.claude/settings.json ------------
# The same four hooks as the plugin's hooks.json. Claude's settings have no name field for a hook, so
# ours are recognised by their command (OUR_COMMAND).

CLAUDE_HOOKS = {
    "PermissionRequest": ("*", "hook claude", 1900),
    "PostToolUse": ("*", "posttool claude", 15),
    "PostToolUseFailure": ("*", "posttool claude", 15),  # a failed command is answered too: the card must go
    "UserPromptSubmit": (None, "event claude working", 15),
    "Stop": (None, "event claude idle", 15),
}


def claude_definitions() -> dict:
    out = {}
    for event, (matcher, args, timeout) in CLAUDE_HOOKS.items():
        definition = {"hooks": [{"type": "command", "command": hook_command("claude", args), "timeout": timeout}]}
        out[event] = {"matcher": matcher, **definition} if matcher else definition
    return out


def install_claude() -> Path:
    path = claude_settings()
    _save_json(path, _merge(path, claude_definitions()))
    return path


def uninstall_claude() -> bool:
    return _remove(claude_settings(), CLAUDE_HOOKS)


def claude_commands() -> list[str]:
    try:
        hooks = _load_json(claude_settings()).get("hooks", {})
    except ValueError:
        return []
    return _our_commands({event: hooks.get(event) for event in CLAUDE_HOOKS})


def claude_installed() -> bool:
    return bool(claude_commands())


# --- CodeBuddy Code: hooks in ~/.codebuddy/settings.json, Claude Code's format -------------------------
# It snapshots hooks at startup and applies changed ones only after a review in its /hooks panel.

def codebuddy_settings() -> Path:
    # CODEBUDDY_CONFIG_DIR seen in its npm bundle, not in its docs: the fixed home path is the documented one.
    return Path(os.environ.get("CODEBUDDY_CONFIG_DIR") or Path.home() / ".codebuddy") / "settings.json"


CODEBUDDY_HOOKS = {
    "PermissionRequest": ("*", "hook codebuddy", 1900),
    "PostToolUse": ("*", "posttool codebuddy", 15),
    "UserPromptSubmit": (None, "event codebuddy working", 15),
    "Stop": (None, "event codebuddy idle", 15),
}


def codebuddy_definitions() -> dict:
    # It runs hook commands through Git Bash on Windows: plain backslash paths broke (seen 2026-11-05).
    out = {}
    for event, (matcher, args, timeout) in CODEBUDDY_HOOKS.items():
        definition = {"hooks": [{"type": "command", "command": posix_command(args), "timeout": timeout}]}
        out[event] = {"matcher": matcher, **definition} if matcher else definition
    return out


def install_codebuddy() -> Path:
    path = codebuddy_settings()
    _save_json(path, _merge(path, codebuddy_definitions()))
    return path


def uninstall_codebuddy() -> bool:
    return _remove(codebuddy_settings(), CODEBUDDY_HOOKS)


def codebuddy_commands() -> list[str]:
    try:
        hooks = _load_json(codebuddy_settings()).get("hooks", {})
    except ValueError:
        return []
    return _our_commands({event: hooks.get(event) for event in CODEBUDDY_HOOKS})


def codebuddy_installed() -> bool:
    return bool(codebuddy_commands())


# --- Crush: `hook add` lines in its Bash-like ~/.config/crush/crushrc ------------------------------------
# Crush runs a hook's command through its own POSIX shell (posix_command: quoted forward-slash paths).
# Our lines sit between two marker comments, so install replaces and uninstall removes exactly them.

CRUSH_BEGIN = "# >>> horncall: phone approvals (remove with: horncall uninstall crush)"
CRUSH_END = "# <<< horncall"
# Tools that change things (reads and lookups never go to the phone); `question` gets a card only.
CRUSH_MATCHER = "^(bash|edit|multiedit|write|download|fetch|web_fetch|lsp_rename|lsp_replace_symbol|question|mcp_.*)$"
CRUSH_HOOKS = {  # event: (matcher, args, timeout)
    "PreToolUse": (CRUSH_MATCHER, "hook crush", 1900),
    "PostToolUse": ("^question$", "posttool crush", 15),  # its question answered: the card leaves the phone
    "TurnStart": (None, "event crush working", 15),
    "TurnEnd": (None, "event crush idle", 15),
    "StopFailure": (None, "event crush idle", 15),  # a turn ended by a provider error: no TurnEnd comes
}


def crush_rc() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "crush" / "crushrc"


def crush_block() -> str:
    lines = [CRUSH_BEGIN]
    for event, (matcher, args, timeout) in CRUSH_HOOKS.items():
        match = f" --matcher '{matcher}'" if matcher else ""
        # Crush shows a hook's name while it runs: say what it waits for (our lines are found by the markers).
        name = "'horncall: answer on your phone'" if event == "PreToolUse" else MARK
        command = re.sub(r'(["$`\\])', r"\\\1", posix_command(args))  # it sits inside crushrc's double quotes
        lines.append(f'hook add {event} --name {name}{match} --command "{command}" --timeout {timeout}')
    return "\n".join(lines + [CRUSH_END]) + "\n"


def _crush_without_ours(text: str) -> str:
    start, end = text.find(CRUSH_BEGIN), text.find(CRUSH_END)
    if start < 0 or end < start:
        return text
    return (text[:start] + text[end + len(CRUSH_END):].lstrip("\n")).rstrip("\n") + ("\n" if text[:start].strip() else "")


def install_crush() -> Path:
    path = crush_rc()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".horncall-backup")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    rest = _crush_without_ours(text).rstrip("\n")
    path.write_text((rest + "\n\n" if rest else "") + crush_block(), encoding="utf-8")
    return path


def uninstall_crush() -> bool:
    path = crush_rc()
    if not path.exists() or CRUSH_BEGIN not in path.read_text(encoding="utf-8"):
        return False
    path.write_text(_crush_without_ours(path.read_text(encoding="utf-8")), encoding="utf-8")
    return True


def crush_commands() -> list[str]:
    path = crush_rc()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    start, end = text.find(CRUSH_BEGIN), text.find(CRUSH_END)
    if start < 0 or end < start:
        return []
    return [re.sub(r"\\(.)", r"\1", c) for c in re.findall(r'--command "((?:[^"\\]|\\.)*)"', text[start:end])]


def crush_installed() -> bool:
    return bool(crush_commands())


# --- opencode 2: our plugin file in its config's plugins/ folder (loaded automatically) -----------------
# opencode has no hook processes: the plugin (opencode_plugin.ts, a template) runs our launcher per request.

OPENCODE_TEMPLATE = Path(__file__).with_name("opencode_plugin.ts")
OPENCODE_PLACEHOLDER = "__HORNCALL_COMMAND__"


def opencode_plugin_file() -> Path:
    if os.environ.get("OPENCODE_CONFIG_DIR"):
        base = Path(os.environ["OPENCODE_CONFIG_DIR"])
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode"
    return base / "plugins" / "horncall.ts"


def _opencode_parts() -> list[str]:
    if packaged():
        return [str(target())]
    return [getattr(sys, "_base_executable", None) or sys.executable, str(LAUNCHER)]


def opencode_plugin_text() -> str:
    return OPENCODE_TEMPLATE.read_text(encoding="utf-8").replace(OPENCODE_PLACEHOLDER, json.dumps(_opencode_parts()))


def install_opencode() -> Path:
    path = opencode_plugin_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(opencode_plugin_text(), encoding="utf-8")
    return path


def uninstall_opencode() -> bool:
    path = opencode_plugin_file()
    if not opencode_commands():
        return False
    path.unlink()
    return True


def opencode_commands() -> list[str]:
    """The command our plugin file runs, as one line (so `points_here` and OUR_COMMAND read it like the others)."""
    path = opencode_plugin_file()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    found = re.search(r"const COMMAND: string\[\] = (\[.*?\])", text)
    if not found or "Horncall" not in text:
        return []
    try:
        parts = json.loads(found.group(1))
    except ValueError:
        return []
    return [" ".join(parts) + " hook opencode"]


def opencode_installed() -> bool:
    return bool(opencode_commands())


INSTALLERS = {
    "claude": (install_claude, uninstall_claude, claude_installed),
    "opencode": (install_opencode, uninstall_opencode, opencode_installed),
    "crush": (install_crush, uninstall_crush, crush_installed),
    "codebuddy": (install_codebuddy, uninstall_codebuddy, codebuddy_installed),
    "gemini": (install_gemini, uninstall_gemini, gemini_installed),
    "copilot": (install_copilot, uninstall_copilot, copilot_installed),
    "codex": (install_codex, uninstall_codex, codex_installed),
    "claude-statusline": (install_claude_statusline, uninstall_claude_statusline, claude_statusline_installed),
}

# Our commands currently in each tool's configuration (to tell "installed here" from "an older copy").
COMMANDS = {"claude": claude_commands, "gemini": gemini_commands, "copilot": copilot_commands, "codex": codex_commands,
            "codebuddy": codebuddy_commands, "crush": crush_commands, "opencode": opencode_commands,
            "claude-statusline": claude_statusline_commands}


def state(agent: str) -> str:
    """'missing', 'here' (runs this installation) or 'elsewhere' (hooks of an older copy in another folder)."""
    commands = COMMANDS[agent]()
    if not commands:
        return "missing"
    return "here" if all(points_here(c) for c in commands) else "elsewhere"


def preview(agent: str) -> list[tuple[Path, str]]:
    """What `install <agent>` would write: (file, the part we add or replace, as JSON). Writes nothing."""
    if agent == "claude":
        changes = [(claude_settings(), {"hooks": {e: [d] for e, d in claude_definitions().items()}})]
    elif agent == "gemini":
        changes = [(gemini_settings(), {"hooks": {e: [d] for e, d in gemini_definitions().items()}})]
    elif agent == "codex":
        changes = [(codex_hooks_file(), {"hooks": {e: [d] for e, d in codex_definitions().items()}})]
    elif agent == "codebuddy":
        changes = [(codebuddy_settings(), {"hooks": {e: [d] for e, d in codebuddy_definitions().items()}})]
    elif agent == "crush":
        return [(crush_rc(), crush_block())]
    elif agent == "opencode":
        return [(opencode_plugin_file(), opencode_plugin_text())]
    elif agent == "copilot":
        changes = [(copilot_hook_file(), copilot_file_content())]
    elif agent == "claude-statusline":
        changes = [(claude_settings(), claude_statusline_definition())]
    else:
        raise ValueError(agent)
    return [(path, json.dumps(part, indent=2, ensure_ascii=False)) for path, part in changes]
