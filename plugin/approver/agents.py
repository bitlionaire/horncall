"""Adapters for the AI coding agents Horncall can answer for.

Each agent calls our hook with its own JSON on stdin and expects its own JSON on stdout. An adapter
turns the input into the common shape `protocol.build_request` takes (Claude Code's: tool_name,
tool_input, cwd, session_id) and the phone's decision back into the agent's answer. Printing nothing
always means "no opinion": the agent then asks the user its normal way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import protocol


@dataclass
class HookInput:
    tool: str            # our tool name: Bash, Write, Edit, … (Claude Code's vocabulary)
    tool_input: dict
    cwd: str | None
    session_id: str | None

    def as_claude(self) -> dict:
        return {"tool_name": self.tool, "tool_input": self.tool_input, "cwd": self.cwd, "session_id": self.session_id}


class Agent:
    name = "?"
    title = "?"
    # The agent kills the hook after its configured timeout; wait a bit less than that.
    max_wait_s = 1850

    def parse(self, raw: dict) -> HookInput | None:
        """None = not something to ask the phone about (let the agent decide)."""
        raise NotImplementedError

    def allow(self) -> dict:
        raise NotImplementedError

    def deny(self, message: str) -> dict:
        raise NotImplementedError


class Claude(Agent):
    """Claude Code `PermissionRequest` hook (the plugin's hooks.json)."""
    name, title = "claude", "Claude Code"

    def parse(self, raw):
        tool_input = raw.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {"input": tool_input}
        return HookInput(str(raw.get("tool_name", "?")), tool_input, raw.get("cwd"), raw.get("session_id"))

    def allow(self):
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}

    def deny(self, message):
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                       "decision": {"behavior": "deny", "message": message}}}


class Gemini(Agent):
    """Gemini CLI `BeforeTool` hook. It fires for every tool, so only risky ones go to the phone."""
    name, title = "gemini", "Gemini CLI"

    # Gemini tool → (our tool, how to map its arguments)
    TOOLS = {
        "run_shell_command": "Bash",
        "write_file": "Write",
        "replace": "Edit",
    }

    def parse(self, raw):
        tool = str(raw.get("tool_name", ""))
        ours = self.TOOLS.get(tool)
        if ours is None:
            return None  # reads, searches, web fetches: Gemini's own rules decide
        args = raw.get("tool_input") or {}
        if not isinstance(args, dict):
            args = {"input": args}
        if ours == "Bash":
            tool_input = {"command": args.get("command", ""), "description": args.get("description")}
            if args.get("dir_path") or args.get("directory"):
                tool_input["description"] = (tool_input["description"] or "") + f" (in {args.get('dir_path') or args.get('directory')})"
        elif ours == "Write":
            tool_input = {"file_path": args.get("file_path") or args.get("path", ""), "content": args.get("content", "")}
        else:
            tool_input = {"file_path": args.get("file_path") or args.get("path", ""),
                          "old_string": args.get("old_string", ""), "new_string": args.get("new_string", "")}
        tool_input = {k: v for k, v in tool_input.items() if v is not None}
        return HookInput(ours, tool_input, raw.get("cwd"), raw.get("session_id"))

    def allow(self):
        return {"decision": "allow"}

    def deny(self, message):
        return {"decision": "deny", "reason": message}


class Copilot(Agent):
    """GitHub Copilot CLI `permissionRequest` hook (runs only when Copilot would ask the user).

    Its input schema isn't documented; this accepts the camelCase and snake_case shapes Copilot uses
    for its other hooks. `hook.py` logs the input's field NAMES (never values) so a real sample can
    confirm the mapping.
    """
    name, title = "copilot", "GitHub Copilot CLI"

    SHELL = {"bash", "shell", "powershell", "run", "terminal", "run_in_terminal"}
    WRITE = {"write", "create", "create_file", "write_file"}
    EDIT = {"edit", "str_replace", "str_replace_editor", "replace", "edit_file"}

    def parse(self, raw):
        tool = str(raw.get("toolName") or raw.get("tool_name") or raw.get("kind") or raw.get("tool") or "?")
        args = (raw.get("toolInput") or raw.get("toolArgs") or raw.get("tool_input") or raw.get("arguments")
                or raw.get("args") or {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"command": args}
        if not isinstance(args, dict):
            args = {"input": args}
        low = tool.lower()
        if low in self.SHELL or "command" in args and low not in self.WRITE | self.EDIT:
            ours = "PowerShell" if low == "powershell" else "Bash"
            tool_input = {"command": args.get("command") or args.get("fullCommandText") or "",
                                        "description": args.get("description")}
        elif low in self.WRITE:
            ours, tool_input = "Write", {"file_path": args.get("path") or args.get("file_path", ""),
                                         "content": args.get("content") or args.get("file_text", "")}
        elif low in self.EDIT:
            ours, tool_input = "Edit", {"file_path": args.get("path") or args.get("file_path", ""),
                                        "old_string": args.get("old_str") or args.get("old_string", ""),
                                        "new_string": args.get("new_str") or args.get("new_string", "")}
        else:
            ours, tool_input = tool, args
        tool_input = {k: v for k, v in tool_input.items() if v is not None}
        return HookInput(ours, tool_input, raw.get("cwd"), raw.get("sessionId") or raw.get("session_id"))

    def allow(self):
        return {"behavior": "allow"}

    def deny(self, message):
        return {"behavior": "deny", "message": message}


class Codex(Claude):
    """OpenAI Codex CLI `PermissionRequest` hook (~/.codex/hooks.json): Claude Code's wire format.

    Its shell tool may pass the command as an argv list, and edits come as one apply_patch text.
    """
    name, title = "codex", "OpenAI Codex CLI"
    # Its hooks.json keeps timeout 600: changing a trusted hook makes the user re-approve it in /hooks.
    max_wait_s = 580

    SHELL = {"shell", "bash", "exec_command", "local_shell", "container.exec", "unified_exec"}

    def parse(self, raw):
        hi = super().parse(raw)
        low = hi.tool.lower()
        args = hi.tool_input
        if low in self.SHELL or isinstance(args.get("command"), list):
            command = args.get("command") or args.get("cmd") or ""
            if isinstance(command, list):
                # ["bash", "-lc", "npm test"] → the script; anything else → joined argv
                command = command[-1] if len(command) >= 3 and command[1] in ("-lc", "-c") else " ".join(map(str, command))
            hi = HookInput("Bash", {"command": str(command), "description": args.get("justification")},
                           hi.cwd, hi.session_id)
        elif low in ("apply_patch", "applypatch"):
            patch = str(args.get("input") or args.get("patch") or args.get("command") or json.dumps(args))
            files, deletes = protocol.patch_files(patch, hi.cwd)
            hi = HookInput("Edit", {"file_path": files[0] if files else protocol.PATCH_UNKNOWN, "old_string": "",
                                    "new_string": patch, "patch_files": files, "patch_deletes": deletes},
                           hi.cwd, hi.session_id)
        hi.tool_input = {k: v for k, v in hi.tool_input.items() if v is not None}
        return hi


AGENTS: dict[str, Agent] = {a.name: a for a in (Claude(), Gemini(), Copilot(), Codex())}


def get(name: str | None) -> Agent:
    return AGENTS.get((name or "claude").lower(), AGENTS["claude"])
