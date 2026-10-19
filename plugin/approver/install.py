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


GEMINI_MATCHER = "run_shell_command|write_file|replace"

# event → (matcher, command args after the launcher, timeout ms, description)
GEMINI_HOOKS = {
    "BeforeTool": (GEMINI_MATCHER, "hook gemini", 1_900_000, "Ask the phone (Horncall) before risky tools"),
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
    # Hooks are on by default since Gemini CLI 0.6x (toggle: hooksConfig.enabled); we don't touch it.
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
    def entry(args: str, timeout: int) -> list:
        return [{"type": "command", "command": hook_command("copilot", args), "timeoutSec": timeout}]

    return {"version": 1, "hooks": {
        "permissionRequest": entry("hook copilot", 1900),
        # Task start/end for the work timer and HP; after-tool drops a phone card answered in the terminal.
        "userPromptSubmitted": entry("event copilot working", 15),
        "agentStop": entry("event copilot idle", 15),
        "postToolUse": entry("posttool copilot", 15),
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
    return [e.get("command", "") for entries in hooks.values() for e in entries or []]


def copilot_installed() -> bool:
    return copilot_hook_file().exists()


# --- OpenAI Codex CLI: ~/.codex/hooks.json (Claude Code's hook format) ---------------------------

def codex_hooks_file() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "hooks.json"


CODEX_HOOKS = {
    "PermissionRequest": ("*", "hook codex", 600),
    "UserPromptSubmit": (None, "event codex working", 15),
    "Stop": (None, "event codex idle", 15),
}


def codex_definitions() -> dict:
    out = {}
    for event, (matcher, args, timeout) in CODEX_HOOKS.items():
        definition = {"hooks": [{"type": "command", "name": MARK, "command": hook_command("codex", args), "timeout": timeout}]}
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


INSTALLERS = {
    "claude": (install_claude, uninstall_claude, claude_installed),
    "gemini": (install_gemini, uninstall_gemini, gemini_installed),
    "copilot": (install_copilot, uninstall_copilot, copilot_installed),
    "codex": (install_codex, uninstall_codex, codex_installed),
    "claude-statusline": (install_claude_statusline, uninstall_claude_statusline, claude_statusline_installed),
}

# Our commands currently in each tool's configuration (to tell "installed here" from "an older copy").
COMMANDS = {"claude": claude_commands, "gemini": gemini_commands, "copilot": copilot_commands, "codex": codex_commands,
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
    elif agent == "copilot":
        changes = [(copilot_hook_file(), copilot_file_content())]
    elif agent == "claude-statusline":
        changes = [(claude_settings(), claude_statusline_definition())]
    else:
        raise ValueError(agent)
    return [(path, json.dumps(part, indent=2, ensure_ascii=False)) for path, part in changes]
